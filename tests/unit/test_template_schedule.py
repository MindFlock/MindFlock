"""Template schedules + Run now (core.template_schedule, /api/templates/*/run).

The due-time math and the tick's rules run against an injected clock in a
pinned time zone (America/New_York, so the DST cases are real): a slot is a
local DATE, it fires once, only the latest missed one fires after downtime,
a schedule counts only from when it was set, and only the automation device
fires. Run now and the scheduled fire both create through the injected
``create`` (never the real engine).
"""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from backend.web import server
from backend.web.core import template_schedule as ts

client = TestClient(server.app)


@pytest.fixture
def ny_tz(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.fixture
def stores(monkeypatch, tmp_path):
    monkeypatch.setenv("MINDFLOCK_TEMPLATES_FILE", str(tmp_path / "tpl.json"))
    monkeypatch.setenv("MINDFLOCK_TEMPLATE_RUNS_FILE", str(tmp_path / "runs.json"))
    # The launch preconditions read this machine (is the agent on PATH, is
    # GitHub connected); pinned so these answer the same on a bare CI box.
    from backend.web.addons import templates as _tpl_addon

    monkeypatch.setattr(_tpl_addon, "_agent_installed", lambda program: True)
    monkeypatch.setattr(_tpl_addon, "_github_connected", lambda: True)
    return tmp_path


@pytest.fixture
def fake_create(monkeypatch):
    """Every create lands here: (status 202, the row) and the payload kept."""
    made = []

    async def create(payload):
        made.append(payload)
        return 202, {"title": payload["title"], "prompt_delivery": "seeded"}

    monkeypatch.setattr(ts, "_default_create", create)
    monkeypatch.setattr(ts, "_title_taken", lambda t: False)
    monkeypatch.setattr(ts, "automation_here", lambda: True)
    monkeypatch.setattr(ts, "automation_elsewhere", lambda: "")
    return made


def at(y, mo, d, h, mi=0, s=0) -> float:
    """Local epoch (the pinned zone) for a wall-clock time."""
    return time.mktime((y, mo, d, h, mi, s, 0, 0, -1))


def local(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))


DAILY_21 = {"at": "21:00", "days": "daily"}
WEEKDAYS_21 = {"at": "21:00", "days": "weekdays"}


def tpl(name="nightly", schedule=DAILY_21, **kw):
    t = {"name": name, "repo_path": "/repo", "prompt": "do the thing", **kw}
    if schedule:
        t["schedule"] = schedule
    return t


# --------------------------------------------------------------------------- #
# Schedule shape
# --------------------------------------------------------------------------- #
def test_normalize_schedule():
    assert ts.normalize_schedule(None) is None
    assert ts.normalize_schedule({}) is None
    assert ts.normalize_schedule({"at": "9:05"}) == {"at": "09:05", "days": "daily"}
    assert ts.normalize_schedule({"at": "21:00", "days": "weekdays"}) == WEEKDAYS_21
    for bad in (
        {"at": "25:00"},
        {"at": "9pm"},
        {"at": "21:00", "days": "sundays"},
        "21:00",
    ):
        with pytest.raises(ValueError):
            ts.normalize_schedule(bad)
    assert ts.schedule_or_none({"at": "nope"}) is None


# --------------------------------------------------------------------------- #
# Due-time math
# --------------------------------------------------------------------------- #
def test_latest_and_next_daily(ny_tz):
    now = at(2026, 10, 13, 20, 0)  # Tue 20:00, before tonight's slot
    slot, epoch = ts.latest_due(now, DAILY_21)
    assert slot == "2026-10-12" and local(epoch) == "2026-10-12 21:00"
    assert local(ts.next_due(now, DAILY_21)) == "2026-10-13 21:00"
    # exactly on the minute counts as due
    assert ts.latest_due(at(2026, 10, 13, 21, 0), DAILY_21)[0] == "2026-10-13"


def test_weekdays_skip_the_weekend(ny_tz):
    sat = at(2026, 10, 17, 10, 0)
    assert ts.latest_due(sat, WEEKDAYS_21)[0] == "2026-10-16"  # Friday
    assert local(ts.next_due(sat, WEEKDAYS_21)) == "2026-10-19 21:00"  # Monday
    fri_late = at(2026, 10, 16, 22, 0)
    assert local(ts.next_due(fri_late, WEEKDAYS_21)) == "2026-10-19 21:00"


def _fires_over(start: float, hours: float, sched: dict, step_min=10):
    """Tick every ``step_min`` minutes over ``hours`` from ``start``; the slot
    dates fired, in order. Armed at ``start``."""
    runs = {"t": {"sig": ts.signature(sched), "armed_at": start}}
    fired = []
    now = start
    while now < start + hours * 3600:
        to_fire, runs = ts.plan_tick(now, [tpl("t", sched)], runs, True)
        fired += [slot for _t, slot in to_fire]
        now += step_min * 60
    return fired


def test_dst_spring_forward_fires_once_per_day(ny_tz):
    # 2026-03-08 02:00 -> 03:00 in New York: 02:30 doesn't exist that day.
    sched = {"at": "02:30", "days": "daily"}
    fired = _fires_over(at(2026, 3, 7, 12, 0), 48, sched)
    assert fired == ["2026-03-08", "2026-03-09"]


def test_dst_fall_back_fires_once_per_day(ny_tz):
    # 2026-11-01 01:30 happens twice in New York.
    sched = {"at": "01:30", "days": "daily"}
    fired = _fires_over(at(2026, 10, 31, 12, 0), 48, sched)
    assert fired == ["2026-11-01", "2026-11-02"]


# --------------------------------------------------------------------------- #
# The tick's rules
# --------------------------------------------------------------------------- #
def test_a_new_schedule_waits_for_its_next_slot(ny_tz):
    saved_at = at(2026, 10, 13, 22, 0)  # 21:00 already passed today
    fire, runs = ts.plan_tick(saved_at, [tpl()], {}, True)
    assert fire == [] and runs["nightly"]["armed_at"] == saved_at
    fire, runs = ts.plan_tick(at(2026, 10, 14, 20, 59), [tpl()], runs, True)
    assert fire == []
    fire, runs = ts.plan_tick(at(2026, 10, 14, 21, 0, 30), [tpl()], runs, True)
    assert [s for _t, s in fire] == ["2026-10-14"]


def test_never_double_fires(ny_tz):
    runs = {"nightly": {"sig": "21:00/daily", "armed_at": at(2026, 10, 1, 0)}}
    now = at(2026, 10, 13, 21, 1)
    fire, runs = ts.plan_tick(now, [tpl()], runs, True)
    assert len(fire) == 1
    for later in (now + 30, now + 3600, at(2026, 10, 14, 20, 59)):
        fire, runs = ts.plan_tick(later, [tpl()], runs, True)
        assert fire == []


def test_downtime_backfills_at_most_one_run(ny_tz):
    runs = {
        "nightly": {
            "sig": "21:00/daily",
            "armed_at": at(2026, 10, 1, 0),
            "last_slot": "2026-10-09",
        }
    }
    # Down from the 10th to the 13th at 08:00: three slots missed, one fires
    # (the latest, the 12th's), and nothing more until tonight.
    fire, runs = ts.plan_tick(at(2026, 10, 13, 8, 0), [tpl()], runs, True)
    assert [s for _t, s in fire] == ["2026-10-12"]
    fire, runs = ts.plan_tick(at(2026, 10, 13, 8, 1), [tpl()], runs, True)
    assert fire == []
    fire, runs = ts.plan_tick(at(2026, 10, 13, 21, 0, 5), [tpl()], runs, True)
    assert [s for _t, s in fire] == ["2026-10-13"]


def test_only_the_automation_device_fires(ny_tz):
    runs = {"nightly": {"sig": "21:00/daily", "armed_at": at(2026, 10, 1, 0)}}
    now = at(2026, 10, 13, 21, 1)
    fire, runs_b = ts.plan_tick(now, [tpl()], runs, False)
    assert fire == []
    # ...but it spent the slot: taking the role over later the same night
    # doesn't replay the run the other device already started.
    assert runs_b["nightly"]["last_slot"] == "2026-10-13"
    fire, _ = ts.plan_tick(now + 600, [tpl()], runs_b, True)
    assert fire == []


def test_changing_the_schedule_rearms_it(ny_tz):
    runs = {"nightly": {"sig": "21:00/daily", "armed_at": at(2026, 10, 1, 0)}}
    moved = tpl(schedule={"at": "09:00", "days": "daily"})
    now = at(2026, 10, 13, 10, 0)  # 09:00 passed an hour ago
    fire, runs = ts.plan_tick(now, [moved], runs, True)
    assert fire == [] and runs["nightly"]["sig"] == "09:00/daily"
    # removing the schedule drops the arming, keeps the history
    runs["nightly"]["last_session"] = "x"
    _, runs = ts.plan_tick(now, [tpl(schedule=None)], runs, True)
    assert "sig" not in runs["nightly"] and runs["nightly"]["last_session"] == "x"


def test_tick_fires_and_records(ny_tz, stores, fake_create):
    from backend.web.addons import templates

    templates.save_template(tpl())
    asyncio.run(ts.tick(now=at(2026, 10, 12, 12, 0), here=True))  # arm
    assert fake_create == []
    fired = asyncio.run(ts.tick(now=at(2026, 10, 13, 21, 0, 20), here=True))
    assert fired == ["nightly"]
    assert fake_create[0] == {
        "title": "nightly-oct13",
        "repo_path": "/repo",
        "prompt": "do the thing",
        "workspace_strategy": "worktree",
    }
    rec = ts.load_runs()["nightly"]
    assert rec["last_slot"] == "2026-10-13"
    assert rec["last_session"] == "nightly-oct13" and rec["last_trigger"] == "schedule"
    assert asyncio.run(ts.tick(now=at(2026, 10, 13, 21, 1), here=True)) == []


def test_session_title_is_name_plus_date_and_dodges_taken(
    ny_tz, stores, fake_create, monkeypatch
):
    when = at(2026, 10, 13, 21, 0)
    assert ts.session_title("Rivian update!", when) == "rivian-update-oct13"
    monkeypatch.setattr(ts, "_title_taken", lambda t: t == "nightly-oct13")
    status, body = asyncio.run(ts.fire(tpl(), "manual", now=when))
    assert status == 202 and body["title"] == "nightly-oct13-2"


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
def test_schedule_round_trips_and_validates(stores, fake_create):
    r = client.post(
        "/api/templates", json=tpl(schedule={"at": "9:00", "days": "weekdays"})
    )
    assert r.status_code == 200
    assert r.json()["template"]["schedule"] == {"at": "09:00", "days": "weekdays"}
    # no schedule -> no key (old devices store none; sync must not see an edit)
    r = client.post("/api/templates", json=tpl("plain", schedule=None))
    assert "schedule" not in r.json()["template"]

    bad = client.post("/api/templates", json=tpl(schedule={"at": "9pm"}))
    assert bad.status_code == 400 and "24-hour" in bad.json()["error"]
    nofolder = client.post(
        "/api/templates", json={"name": "x", "prompt": "p", "schedule": DAILY_21}
    )
    assert nofolder.status_code == 400 and "folder" in nofolder.json()["error"]
    noprompt = client.post(
        "/api/templates", json={"name": "x", "repo_path": "/r", "schedule": DAILY_21}
    )
    assert noprompt.status_code == 400 and "prompt" in noprompt.json()["error"]

    got = client.get("/api/templates").json()
    assert got["runs_here"] is True and got["runs_on"] == ""
    assert got["runs"]["nightly"]["next_at"] > time.time()


def test_run_now_route(stores, fake_create):
    client.post("/api/templates", json=tpl("Grafana check", schedule=None))
    r = client.post("/api/templates/grafana%20check/run")
    assert r.status_code == 202
    title = r.json()["title"]
    assert title.startswith("grafana-check-")
    assert fake_create[0]["prompt"] == "do the thing"
    row = client.get("/api/templates").json()["runs"]["grafana check"]
    assert row["last_session"] == title and row["last_trigger"] == "manual"

    assert client.post("/api/templates/nope/run").status_code == 404
    client.post("/api/templates", json={"name": "loose", "prompt": "p"})
    r = client.post("/api/templates/loose/run")
    # Round 5: a launch precondition is a 409 with a sentence and a reason,
    # said before any session is made (and recorded as a failed run).
    assert r.status_code == 409 and "folder" in r.json()["error"]
    assert r.json()["reason"] == "no_folder"
    assert (
        r.json()["detail"]
        == "Couldn't start loose: it has no folder — edit it and pick one"
    )
    assert len(fake_create) == 1


def test_a_failed_create_is_recorded(stores, monkeypatch, fake_create):
    async def refuse(payload):
        return 400, {"error": "repo_path is not a folder"}

    monkeypatch.setattr(ts, "_default_create", refuse)
    client.post("/api/templates", json=tpl(schedule=None))
    r = client.post("/api/templates/nightly/run")
    assert r.status_code == 400
    row = client.get("/api/templates").json()["runs"]["nightly"]
    assert row["last_error"] == "repo_path is not a folder"


def test_synced_in_template_keeps_its_schedule(stores):
    from backend.web.core import sync_stores

    sync_stores.templates_write("nightly", tpl())
    assert sync_stores.templates_list()["nightly"]["schedule"] == DAILY_21
    # a bad synced schedule drops the schedule, not the template
    sync_stores.templates_write("odd", tpl("odd", schedule={"at": "x"}))
    assert "schedule" not in sync_stores.templates_list()["odd"]


def test_loop_is_off_under_pytest():
    from backend.web.addons.templates import TemplatesAddon

    started = []

    class Ctx:
        def register_task(self, coro):
            started.append(coro)

    asyncio.run(TemplatesAddon().on_startup(Ctx()))
    assert started == []


# --------------------------------------------------------------------------- #
# Autopilot on a template: its run never shares a checkout
# --------------------------------------------------------------------------- #
def test_an_autopilot_template_never_runs_in_place(stores, fake_create, monkeypatch):
    monkeypatch.setattr(ts, "_arm_autopilot", lambda title, depth: depth)
    status, _ = asyncio.run(
        ts.fire(tpl(schedule=None, in_place=True, autopilot="pr"), "manual")
    )
    assert status == 202
    # It commits for THIS session, so never in a checkout others share.
    assert "in_place" not in fake_create[0]
    # Without one in_place is kept.
    asyncio.run(ts.fire(tpl("plain", schedule=None, in_place=True), "manual"))
    assert fake_create[1]["in_place"] is True


def test_status_names_the_time_zone(ny_tz, stores, fake_create):
    from backend.web.addons import templates

    templates.save_template(tpl())
    st = ts.status_for(templates.list_templates(), now=at(2026, 10, 13, 12, 0))
    assert st["runs"]["nightly"]["tz"] == "EDT"
    assert st["tz"] == "EDT"
    st = ts.status_for(templates.list_templates(), now=at(2026, 12, 1, 12, 0))
    assert st["runs"]["nightly"]["tz"] == "EST"


# --------------------------------------------------------------------------- #
# Cron schedules
# --------------------------------------------------------------------------- #
CRON_30 = {"cron": "*/30 * * * *"}


def test_normalize_cron_schedule():
    assert ts.normalize_schedule({"cron": " 0  21 * * 1-5 "}) == {
        "cron": "0 21 * * 1-5"
    }
    assert ts.normalize_schedule({"cron": "@daily"}) == {"cron": "@daily"}
    with pytest.raises(ValueError, match="5 fields"):
        ts.normalize_schedule({"cron": "0 21 * *"})
    with pytest.raises(ValueError, match="every 15 minutes"):
        ts.normalize_schedule({"cron": "*/5 * * * *"})
    assert ts.schedule_or_none({"cron": "nope"}) is None
    assert ts.describe({"cron": "0 21 * * 1-5"}) == "Weekdays at 21:00"
    assert ts.describe(DAILY_21) == "Every day at 21:00"


def test_cron_fires_each_slot_once(ny_tz):
    fired = _fires_over(at(2026, 10, 14, 9, 5), 2, CRON_30, step_min=5)
    assert fired == [
        "2026-10-14T09:30",
        "2026-10-14T10:00",
        "2026-10-14T10:30",
        "2026-10-14T11:00",
    ]


def test_cron_downtime_backfills_at_most_one_run(ny_tz):
    start = at(2026, 10, 14, 9, 0)
    runs = {"t": {"sig": ts.signature(CRON_30), "armed_at": start}}
    fire, runs = ts.plan_tick(at(2026, 10, 14, 13, 40), [tpl("t", CRON_30)], runs, True)
    assert [slot for _t, slot in fire] == ["2026-10-14T13:30"]


def test_a_legacy_slot_never_refires_after_upgrade(ny_tz):
    # A record written by the {at, days} code (slot = a date) still reads as
    # spent for that schedule.
    runs = {"nightly": {"sig": "21:00/daily", "armed_at": 0, "last_slot": "2026-10-13"}}
    fire, _ = ts.plan_tick(at(2026, 10, 13, 22, 0), [tpl()], runs, True)
    assert fire == []


def test_switching_to_cron_rearms(ny_tz):
    runs = {"nightly": {"sig": "21:00/daily", "armed_at": 0, "last_slot": "2026-10-13"}}
    now = at(2026, 10, 13, 22, 10)
    fire, runs = ts.plan_tick(now, [tpl(schedule=CRON_30)], runs, True)
    assert fire == [] and runs["nightly"]["sig"] == "cron:*/30 * * * *"
    fire, _ = ts.plan_tick(now + 25 * 60, [tpl(schedule=CRON_30)], runs, True)
    assert [slot for _t, slot in fire] == ["2026-10-13T22:30"]


def test_many_a_day_titles_carry_the_time(ny_tz, stores, fake_create):
    from backend.web.addons import templates

    templates.save_template(tpl("Deploy watch", schedule={"cron": "0 */2 * * *"}))
    asyncio.run(ts.tick(now=at(2026, 10, 13, 9, 0), here=True))  # arm
    assert asyncio.run(ts.tick(now=at(2026, 10, 13, 10, 0, 20), here=True)) == [
        "Deploy watch"
    ]
    assert fake_create[0]["title"] == "deploy-watch-oct13-1000"
    # Run now keeps the plain date title
    status, body = asyncio.run(ts.run_template("deploy watch"))
    assert status == 202 and body["title"] == ts.session_title(
        "Deploy watch", time.time()
    )


def test_a_closed_runs_branch_is_not_reused(ny_tz, stores, fake_create, monkeypatch):
    seen = []

    def branch_taken(t, title):
        seen.append(title)
        return title in ("nightly-oct13", "nightly-oct13-2")

    monkeypatch.setattr(ts, "_branch_taken", branch_taken)
    status, body = asyncio.run(ts.fire(tpl(), "manual", now=at(2026, 10, 13, 21, 0)))
    assert status == 202 and body["title"] == "nightly-oct13-3"
    assert seen[:3] == ["nightly-oct13", "nightly-oct13-2", "nightly-oct13-3"]


def test_branch_taken_reads_the_repo(tmp_path, monkeypatch):
    import subprocess

    from backend.web import server

    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "x",
        ],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "branch", "mf/nightly-oct13"], check=True)
    monkeypatch.setattr(server, "_session_branch_name", lambda t: "mf/" + t)
    t = tpl(repo_path=str(repo))
    assert ts._branch_taken(t, "nightly-oct13") is True
    assert ts._branch_taken(t, "nightly-oct14") is False
    # in-place and provisioned sessions don't make a branch from the title
    assert ts._branch_taken({**t, "in_place": True}, "nightly-oct13") is False
    assert ts._branch_taken({**t, "provisioned": True}, "nightly-oct13") is False


# --------------------------------------------------------------------------- #
# Overlap guard, history, autopilot
# --------------------------------------------------------------------------- #
def test_a_busy_previous_run_skips_the_slot(ny_tz, stores, fake_create, monkeypatch):
    from backend.web.addons import templates

    events = []
    monkeypatch.setattr(ts, "_emit", lambda ev, tpl, trig, **d: events.append((ev, d)))
    templates.save_template(tpl())
    asyncio.run(ts.tick(now=at(2026, 10, 12, 12, 0), here=True))  # arm
    assert asyncio.run(ts.tick(now=at(2026, 10, 12, 21, 0, 10), here=True)) == [
        "nightly"
    ]

    monkeypatch.setattr(
        ts, "_session_activity", lambda t: "working" if t == "nightly-oct12" else ""
    )
    assert asyncio.run(ts.tick(now=at(2026, 10, 13, 21, 0, 10), here=True)) == []
    assert len(fake_create) == 1
    rec = ts.load_runs()["nightly"]
    assert rec["last_slot"] == "2026-10-13"  # spent: never retried late
    assert rec["history"][0]["skipped"] == "the previous run is still working"
    assert events[-1][0] == "template.run_skipped"
    row = ts.status_for(templates.list_templates(), now=at(2026, 10, 13, 21, 5))[
        "runs"
    ]["nightly"]
    assert row["last_skip"] == "the previous run is still working"
    assert [h.get("session") for h in row["history"]] == [
        "nightly-oct12",
        "nightly-oct12",
    ]

    # Run now is never held back
    status, _ = asyncio.run(ts.run_template("nightly"))
    assert status == 202 and len(fake_create) == 2

    # idle (or gone) lets the next slot through
    monkeypatch.setattr(ts, "_session_activity", lambda t: "idle")
    assert asyncio.run(ts.tick(now=at(2026, 10, 14, 21, 0, 10), here=True)) == [
        "nightly"
    ]


def test_busy_reason_reads_activity(monkeypatch):
    for act, words in (
        ("working", "working"),
        ("clarify", "question"),
        ("limit", "usage limit"),
    ):
        monkeypatch.setattr(ts, "_session_activity", lambda t, a=act: a)
        assert words in ts.busy_reason("x")
    for act in ("idle", "offline", "unknown", ""):
        monkeypatch.setattr(ts, "_session_activity", lambda t, a=act: a)
        assert ts.busy_reason("x") == ""
    assert ts.busy_reason("") == ""


def test_history_is_capped_newest_first(stores):
    for i in range(ts.HISTORY_MAX + 5):
        ts.record_run("k", {"at": i, "trigger": "manual", "session": "s%d" % i})
    hist = ts.load_runs()["k"]["history"]
    assert len(hist) == ts.HISTORY_MAX and hist[0]["session"] == "s%d" % (
        ts.HISTORY_MAX + 4
    )


def test_autopilot_is_armed_after_the_create(stores, fake_create, monkeypatch):
    armed = []
    monkeypatch.setattr(
        ts, "_arm_autopilot", lambda title, depth: armed.append((title, depth)) or depth
    )
    status, body = asyncio.run(
        ts.fire(tpl(autopilot="pr"), "manual", now=at(2026, 10, 13, 21, 0))
    )
    assert status == 202 and armed == [(body["title"], "pr")]
    assert "autopilot" not in fake_create[0]  # not a create field
    assert ts.load_runs()["nightly"]["history"][0]["autopilot"] == "pr"

    async def refuse(payload):
        return 400, {"error": "nope"}

    asyncio.run(ts.fire(tpl(autopilot="pr"), "manual", create=refuse))
    assert len(armed) == 1  # a failed create arms nothing


def test_arm_autopilot_uses_the_lane(monkeypatch):
    from backend.web.core import lanes

    calls = []
    monkeypatch.setattr(
        lanes, "arm_session", lambda title, lane, **kw: calls.append((title, lane, kw))
    )
    assert ts._arm_autopilot("s1", "push") == "push"
    assert calls[0][:2] == ("s1", "push") and calls[0][2]["require_workspace"] is False

    def boom(*a, **k):
        raise lanes.LaneError("bad")

    monkeypatch.setattr(lanes, "arm_session", boom)
    assert ts._arm_autopilot("s1", "push") == ""


def test_normalize_autopilot():
    assert ts.normalize_autopilot(None) == ""
    assert ts.normalize_autopilot("off") == ""
    assert ts.normalize_autopilot(" PR ") == "pr"
    with pytest.raises(ValueError):
        ts.normalize_autopilot("merge")


def test_a_scheduled_failure_is_announced(stores, fake_create, monkeypatch):
    events = []
    monkeypatch.setattr(
        ts, "_emit", lambda ev, tpl, trig, **d: events.append((ev, trig, d))
    )
    status, body = asyncio.run(ts.fire({"name": "loose", "prompt": "p"}, "schedule"))
    # Refused up front, the same 409 Run now answers — never a 202
    assert (status, body["reason"]) == (409, "no_folder")
    assert len(events) == 1
    ev, trig, data = events[0]
    assert (ev, trig, data["error"]) == (
        "template.run_failed",
        "schedule",
        ts.run_problem({}),
    )
    assert (
        data["detail"]
        == "Couldn't start loose: it has no folder — edit it and pick one"
    )
    entry = ts.load_runs()["loose"]["history"][0]
    assert entry["error"] and entry["status"] == "failed"
    assert entry["trigger"] == "schedule"

    async def refuse(payload):
        return 400, {"error": "nope"}

    # Run now's failure is the clicker's toast, not a bell row / push
    asyncio.run(ts.fire(tpl(), "manual", create=refuse))
    assert len(events) == 1
    assert ts.load_runs()["nightly"]["last_error"] == "nope"


# --------------------------------------------------------------------------- #
# Routes: cron + autopilot
# --------------------------------------------------------------------------- #
def test_cron_and_autopilot_round_trip(stores, fake_create):
    r = client.post(
        "/api/templates",
        json=tpl(schedule={"cron": "0 21 * * mon-fri"}, autopilot="push"),
    )
    assert r.status_code == 200, r.text
    saved = r.json()["template"]
    assert (
        saved["schedule"] == {"cron": "0 21 * * mon-fri"}
        and saved["autopilot"] == "push"
    )
    row = client.get("/api/templates").json()["runs"]["nightly"]
    assert row["schedule_label"] == "Weekdays at 21:00"
    plain = client.post("/api/templates", json=tpl("plain", schedule=None)).json()[
        "template"
    ]
    assert "autopilot" not in plain

    bad = client.post("/api/templates", json=tpl(schedule={"cron": "* * * * *"}))
    assert bad.status_code == 400 and "15 minutes" in bad.json()["error"]
    bad = client.post("/api/templates", json=tpl(autopilot="merge"))
    assert bad.status_code == 400 and "autopilot" in bad.json()["error"]


def test_cron_preview_route():
    r = client.get(
        "/api/templates/cron-preview", params={"expr": "*/30 * * * *"}
    ).json()
    assert r["ok"] and r["label"] == "Every 30 minutes" and len(r["next"]) == 3
    assert r["next"][1] - r["next"][0] == 1800
    r = client.get("/api/templates/cron-preview", params={"expr": "61 * * * *"}).json()
    assert r == {"ok": False, "error": "minute: 61 is outside 0-59"}


# --------------------------------------------------------------------------- #
# Pause and skip next (the Schedule surface's per-row switches)
# --------------------------------------------------------------------------- #
def test_pause_and_skip_are_stored_only_when_set():
    assert ts.normalize_schedule({"at": "21:00", "paused": False}) == DAILY_21
    assert ts.normalize_schedule({"at": "21:00", "paused": True}) == {
        **DAILY_21,
        "paused": True,
    }
    assert ts.normalize_schedule(
        {"cron": "0 9 * * 1-5", "skip_until": 1700000000.7}
    ) == {
        "cron": "0 9 * * 1-5",
        "skip_until": 1700000000,
    }
    assert ts.normalize_schedule({"at": "21:00", "skip_until": "junk"}) == DAILY_21
    # Neither re-arms: the cadence is what the signature is about.
    assert ts.signature(ts.normalize_schedule({"at": "21:00", "paused": True})) == (
        ts.signature(DAILY_21)
    )


def test_a_paused_schedule_spends_its_slots_without_firing(ny_tz):
    runs = {"nightly": {"sig": "21:00/daily", "armed_at": at(2026, 10, 1, 0)}}
    paused = tpl(schedule={**DAILY_21, "paused": True})
    fire, runs = ts.plan_tick(at(2026, 10, 13, 21, 1), [paused], runs, True)
    assert fire == [] and runs["nightly"]["last_slot"] == "2026-10-13"
    # Resumed later that night: tonight's slot is NOT replayed...
    fire, runs = ts.plan_tick(at(2026, 10, 13, 22, 0), [tpl()], runs, True)
    assert fire == []
    # ...and tomorrow's fires as usual.
    fire, runs = ts.plan_tick(at(2026, 10, 14, 21, 0, 30), [tpl()], runs, True)
    assert [s for _t, s in fire] == ["2026-10-14"]


def test_skip_next_passes_one_slot_and_says_so(ny_tz):
    runs = {"nightly": {"sig": "21:00/daily", "armed_at": at(2026, 10, 1, 0)}}
    skip = tpl(schedule={**DAILY_21, "skip_until": int(at(2026, 10, 13, 21, 0))})
    fire, runs = ts.plan_tick(at(2026, 10, 13, 21, 1), [skip], runs, True)
    assert fire == []
    assert runs["nightly"]["history"][0]["skipped"] == ts.SKIPPED_BY_HAND
    fire, runs = ts.plan_tick(at(2026, 10, 14, 21, 1), [skip], runs, True)
    assert [s for _t, s in fire] == ["2026-10-14"]


def test_status_shows_paused_and_the_skipped_slot(ny_tz, stores, fake_create):
    from backend.web.addons import templates

    now = at(2026, 10, 13, 12, 0)
    templates.save_template(tpl(schedule={**DAILY_21, "paused": True}))
    row = ts.status_for(templates.list_templates(), now=now)["runs"]["nightly"]
    assert row["paused"] is True and row["next_at"] is None
    templates.save_template(
        tpl(schedule={**DAILY_21, "skip_until": int(at(2026, 10, 13, 21, 0))})
    )
    row = ts.status_for(templates.list_templates(), now=now)["runs"]["nightly"]
    assert "paused" not in row
    assert local(row["skipping_at"]) == "2026-10-13 21:00"
    assert local(row["next_at"]) == "2026-10-14 21:00"
    # A skip that already passed is nothing.
    row = ts.status_for(templates.list_templates(), now=at(2026, 10, 14, 12, 0))[
        "runs"
    ]["nightly"]
    assert "skipping_at" not in row and local(row["next_at"]) == "2026-10-14 21:00"


def test_pause_round_trips_through_the_route(stores, fake_create):
    r = client.post("/api/templates", json=tpl(schedule={**DAILY_21, "paused": True}))
    assert r.status_code == 200
    assert r.json()["template"]["schedule"] == {**DAILY_21, "paused": True}
    got = client.get("/api/templates").json()["runs"]["nightly"]
    assert got["paused"] is True and got["next_at"] is None


def test_a_scheduled_fire_with_a_missing_agent_records_a_failed_run(
    stores, fake_create, monkeypatch
):
    """A nightly run of a template whose agent isn't installed never makes a
    session and never reads as a success: 409 agent_missing, a failed run in
    the history (the card's "Last run failed" + the top-bar badge) and a bell
    event — every night, not once."""
    from backend.web.addons import templates as _tpl_addon

    monkeypatch.setattr(_tpl_addon, "_agent_installed", lambda program: False)
    events = []
    monkeypatch.setattr(
        ts, "_emit", lambda ev, tpl, trig, **d: events.append((ev, trig, d))
    )
    status, body = asyncio.run(ts.fire(tpl("pr-bot", program="codex"), "schedule"))
    assert status == 409
    assert body["reason"] == "agent_missing" and body["agent"] == "codex"
    assert fake_create == []
    row = ts.load_runs()["pr-bot"]
    assert row["last_error"] == "codex isn't installed"
    assert row["last_reason"] == "agent_missing"
    assert row["history"][0]["status"] == "failed"
    assert row["history"][0]["trigger"] == "schedule"
    assert [(e[0], e[2]["reason"]) for e in events] == [
        ("template.run_failed", "agent_missing")
    ]
    assert events[0][2]["detail"] == "Couldn't start pr-bot: codex isn't installed"
