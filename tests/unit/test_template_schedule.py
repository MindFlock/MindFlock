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
    assert r.status_code == 400 and "folder" in r.json()["error"]
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
# Fast-track on a template
# --------------------------------------------------------------------------- #
def test_fasttrack_round_trips_only_when_set(stores, fake_create):
    r = client.post("/api/templates", json=tpl("ships", fasttrack="pr", ask_first=True))
    assert r.status_code == 200
    saved = r.json()["template"]
    assert saved["fasttrack"] == "pr" and saved["ask_first"] is True
    # Off is no key at all (older devices and settings sync see no edit).
    for off in ("off", "leave", "", None):
        got = client.post("/api/templates", json=tpl("quiet", fasttrack=off))
        assert got.status_code == 200, off
        assert "fasttrack" not in got.json()["template"]
        assert "ask_first" not in got.json()["template"]
    bad = client.post("/api/templates", json=tpl("bad", fasttrack="yolo"))
    assert bad.status_code == 400 and "fasttrack" in bad.json()["error"]


def test_a_fasttrack_template_arms_its_session(stores, fake_create):
    armed = []

    def arm(title, lane, ask_first):
        armed.append((title, lane, ask_first))

    t = tpl(schedule=None, in_place=True, fasttrack="pr", ask_first=True)
    status, body = asyncio.run(
        ts.fire(t, "schedule", now=at(2026, 10, 13, 21, 0), arm=arm)
    )
    assert status == 202
    assert armed == [(body["title"], "pr", True)]
    # It commits for THIS session, so never in a checkout others share.
    assert "in_place" not in fake_create[0]
    # Without one nothing is armed and in_place is kept.
    asyncio.run(ts.fire(tpl("plain", schedule=None, in_place=True), "manual", arm=arm))
    assert len(armed) == 1 and fake_create[1]["in_place"] is True


def test_a_failed_arm_is_recorded_but_the_run_stands(stores, fake_create):
    def arm(title, lane, ask_first):
        raise RuntimeError("workspace gone")

    status, _ = asyncio.run(ts.fire(tpl(fasttrack="commit"), "manual", arm=arm))
    assert status == 202
    rec = ts.load_runs()["nightly"]
    assert rec["last_session"] and "fast-track" in rec["last_error"]


def test_status_names_the_time_zone(ny_tz, stores, fake_create):
    from backend.web.addons import templates

    templates.save_template(tpl())
    st = ts.status_for(templates.list_templates(), now=at(2026, 10, 13, 12, 0))
    assert st["runs"]["nightly"]["tz"] == "EDT"
    assert st["tz"] == "EDT"
    st = ts.status_for(templates.list_templates(), now=at(2026, 12, 1, 12, 0))
    assert st["runs"]["nightly"]["tz"] == "EST"
