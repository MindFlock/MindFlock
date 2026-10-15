"""Background jobs: an expected duration and a learned rhythm move "stuck".

Owner's ask (2026-10-15): "If a job is planning to run for a very long time,
estimate how long it should take, and only show *stuck* when it is far from
that. I have a 6-hour job running right now and there is no reason it should
show stuck." Contract: ux-lab round6 JOB-EXPECTATIONS-CONTRACT.md.

Pinned here: ``threshold_s = max(STUCK_AFTER_S, 2 × longest_gap_s, 1.5 ×
expect_s)`` and its reason strings against a fake process table (the style of
test_claude_background.py), the ``MINDFLOCK_EXPECT=`` command convention, the
precedence you > agent > command, duration parsing, the two routes, the MCP
tool, and the once-per-job ``session.job_past_expected`` event.
"""

from __future__ import annotations

import json
import os
import types

import pytest
from fastapi.testclient import TestClient

from backend.mcp.protocol import ToolError
from backend.providers import claude_background as bg

linux_only = pytest.mark.skipif(
    not os.path.isdir("/proc/self"), reason="background-job scan reads /proc"
)

NOW = 1_000_000.0
PID, START = 4242, 500  # START ticks at 100 Hz: the job began 5 s after boot
JOB_ID = "bfake0001"
H = 3600.0


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A scratch ``$MINDFLOCK_HOME`` and empty in-memory tables."""
    monkeypatch.setattr(bg, "_samples", {})
    monkeypatch.setattr(bg, "_last", {})
    monkeypatch.setattr(bg, "_announced", set())
    monkeypatch.setattr(bg, "_store_cache", {"sig": None, "data": None})
    monkeypatch.delenv("MINDFLOCK_JOB_EXPECTATIONS_FILE", raising=False)
    monkeypatch.setenv("MINDFLOCK_HOME", str(tmp_path / "home"))
    return tmp_path / "home" / "job_expectations.json"


@pytest.fixture
def job(tmp_path, monkeypatch, store):
    """One job root (pid 4242, id bfake0001) under pane 100 that started
    ``age`` seconds before ``now``; ``classify(quiet, age)`` seeds a previous
    sample ``quiet`` seconds after its last activity and reads it back."""
    out = tmp_path / "tasks" / (JOB_ID + ".output")
    out.parent.mkdir()
    out.write_text("")
    os.utime(out, (0, 0))
    table = {
        100: {"ppid": 1, "cpu": 0, "start": 1},
        PID: {"ppid": 100, "cpu": 10, "start": START},
    }
    cmd = {"line": "sh -c sleep 600"}
    boot = {"t": NOW}
    monkeypatch.setattr(bg, "_process_table", lambda: table)
    monkeypatch.setattr(bg, "_hz", lambda: 100)
    monkeypatch.setattr(bg, "_boot_time", lambda now: boot["t"])
    monkeypatch.setattr(bg, "_cmdline", lambda pid: cmd["line"])
    monkeypatch.setattr(
        bg, "_stdout_target", lambda pid: str(out) if pid == PID else "/dev/pts/1"
    )

    def started_at(age, now=NOW):
        boot["t"] = now - age - START / 100

    def scan(now=NOW):
        jobs = bg.jobs_under(100, now=now)
        assert [j["id"] for j in jobs] == [JOB_ID]
        return jobs[0]

    def classify(quiet, age, now=NOW, gap=0.0):
        started_at(age, now)
        bg._samples[(PID, START)] = {
            "ts": now - 10,
            "cpu": 0.1,
            "active": now - quiet,
            "gap": gap,
        }
        table[PID]["cpu"] = 10
        return scan(now)

    ns = types.SimpleNamespace(
        classify=classify, scan=scan, started_at=started_at, table=table, cmd=cmd
    )
    return ns


# --- durations ---------------------------------------------------------------
@pytest.mark.parametrize(
    "value,secs",
    [
        ("90s", 90.0),
        ("20m", 1200.0),
        ("6h", 21600.0),
        ("2d", 172800.0),
        ("1h30m", 5400.0),
        ("1h 30m", 5400.0),
        ("1.5h", 5400.0),
        ("6H", 21600.0),
        ("3600", 3600.0),
        (3600, 3600.0),
        (90.5, 90.5),
        ("14d", 14 * 86400.0),
        # Out of range: under a minute, over 14 days.
        ("30s", None),
        (59, None),
        ("15d", None),
        # Not a duration.
        ("abc", None),
        ("", None),
        ("h", None),
        ("1h30", None),
        ("6 hours", None),
        ("-5m", None),
        ("nan", None),
        ("inf", None),
        (True, None),
        (None, None),
        ([1], None),
    ],
)
def test_parse_duration(value, secs):
    assert bg.parse_duration(value) == secs


@pytest.mark.parametrize(
    "secs,text",
    [
        (45, "45s"),
        (41 * 60, "41m"),
        (6 * H, "6h"),
        (2 * H + 600, "2h 10m"),
        (9 * H + 600, "9h 10m"),
        (2 * 86400 + 3 * H, "2d 3h"),
    ],
)
def test_format_duration(secs, text):
    assert bg.format_duration(secs) == text


@pytest.mark.parametrize(
    "command,secs",
    [
        ("MINDFLOCK_EXPECT=90m python train.py", 5400.0),
        ("cd /repo && MINDFLOCK_EXPECT=6h python train.py", 21600.0),
        ("MINDFLOCK_EXPECT='1h30m' make all", 5400.0),
        ('env MINDFLOCK_EXPECT="2d" ./run', 172800.0),
        ("X_MINDFLOCK_EXPECT=6h python train.py", None),
        ("MINDFLOCK_EXPECT=soon python train.py", None),
        ("python train.py", None),
    ],
)
def test_command_expectation(command, secs):
    assert bg.command_expectation(command) == secs


# --- the threshold and its reasons ---------------------------------------------
@linux_only
def test_new_fields_are_neutral_without_a_rhythm_or_expectation(job):
    j = job.classify(bg.STUCK_AFTER_S - 1, age=H)
    assert j["state"] == "waiting"
    assert j["reason"] == "no CPU or output for 15m"
    assert j["expect_s"] is None and j["expected_by"] is None and j["eta"] is None
    assert j["longest_gap_s"] == 0.0
    assert j["threshold_s"] == bg.STUCK_AFTER_S
    assert j["past_expected"] is False
    assert job.classify(bg.STUCK_AFTER_S, age=H)["state"] == "stuck"


@linux_only
def test_a_learned_gap_raises_the_threshold(job):
    """Quiet for 41 min, then it came back: at 20 min quiet it is waiting,
    not stuck — and stuck only past twice that longest gap."""
    gap = 41 * 60.0
    # 41 min into a quiet stretch, then 1 s of CPU: it resumed.
    job.classify(gap, age=2 * H)
    job.table[PID]["cpu"] = 110
    resumed = job.scan(now=NOW + 10)
    assert resumed["state"] == "running"
    assert resumed["longest_gap_s"] == gap + 10

    j = job.scan(now=NOW + 10 + 20 * 60)  # 20 min with nothing since
    assert j["quiet_s"] == 20 * 60
    assert j["state"] == "waiting"
    assert j["reason"] == "quiet 20m — it has resumed after 41m before"
    assert j["threshold_s"] == 2 * (gap + 10)

    j = job.scan(now=NOW + 10 + 2 * (gap + 10))
    assert j["state"] == "stuck"
    assert j["reason"] == "no CPU or output for 82m"


@linux_only
def test_the_same_quiet_job_without_that_history_is_stuck(job):
    assert job.classify(20 * 60, age=2 * H)["state"] == "stuck"


@linux_only
def test_a_first_sightings_guess_is_not_learned_as_a_gap(job):
    """After a restart a quiet job's "active" is a guess (its start time):
    coming back must not teach a gap the server never watched."""
    job.started_at(3 * H)
    first = job.scan()  # no previous sample: judged by the lifetime average
    assert first["quiet_s"] == 3 * H
    job.table[PID]["cpu"] = 10 + 100  # 1 s of CPU in the next 10 s
    j = job.scan(now=NOW + 10)
    assert j["state"] == "running"
    assert j["longest_gap_s"] == 10.0  # only the watched 10 s


@linux_only
def test_an_expectation_of_6h_keeps_it_waiting_until_9h_quiet(job):
    ok, _ = bg.set_expectation(JOB_ID, 6 * H, "you", "train", now=NOW)
    assert ok
    j = job.classify(5 * H, age=5.5 * H)
    assert j["state"] == "waiting"
    assert j["reason"] == "5h 30m of ~6h expected"
    assert j["expect_s"] == 6 * H and j["expected_by"] == "you"
    assert j["eta"] == round(NOW - 5.5 * H + 6 * H, 1)
    assert j["threshold_s"] == 9 * H
    assert j["past_expected"] is False

    j = job.classify(8 * H, age=8.5 * H)
    assert j["state"] == "waiting" and j["past_expected"] is True
    assert j["reason"] == "8h 30m in, past the ~6h expected; quiet 8h"

    j = job.classify(9 * H, age=9 * H + 600)
    assert j["state"] == "stuck"
    assert j["reason"] == "expected ~6h, now 9h 10m in and quiet for 9h"


@linux_only
def test_an_expectation_never_shortens_the_15_minute_floor(job):
    bg.set_expectation(JOB_ID, 120, "agent", now=NOW)
    j = job.classify(10 * 60, age=20 * 60)
    assert j["state"] == "waiting" and j["threshold_s"] == bg.STUCK_AFTER_S


@linux_only
def test_waiting_on_itself_is_stuck_whatever_the_expectation(job, monkeypatch):
    bg.set_expectation(JOB_ID, 6 * H, "you", now=NOW)
    monkeypatch.setattr(bg, "waits_on_itself", lambda command, own: True)
    j = job.classify(bg.ACTIVE_GRACE_S, age=H)
    assert j["state"] == "stuck" and "waits for itself" in j["reason"]


@linux_only
def test_mindflock_expect_in_the_command_needs_no_file(job, store):
    job.cmd["line"] = "zsh -c eval 'MINDFLOCK_EXPECT=90m python train.py' < /dev/null"
    j = job.classify(30 * 60, age=H)
    assert j["expect_s"] == 5400.0 and j["expected_by"] == "command"
    assert j["state"] == "waiting" and j["reason"] == "1h of ~1h 30m expected"
    assert not store.exists()


@linux_only
def test_precedence_you_then_agent_then_command(job, store):
    job.cmd["line"] = "sh -c MINDFLOCK_EXPECT=90m python train.py"
    assert job.classify(H, age=H)["expected_by"] == "command"

    assert bg.set_expectation(JOB_ID, 3 * H, "agent", now=NOW)[0] is True
    j = job.classify(H, age=H)
    assert (j["expect_s"], j["expected_by"]) == (3 * H, "agent")

    assert bg.set_expectation(JOB_ID, 6 * H, "you", now=NOW)[0] is True
    # The agent can't override a person; theirs is what it gets back.
    ok, theirs = bg.set_expectation(JOB_ID, H, "agent", now=NOW)
    assert ok is False and theirs["by"] == "you" and theirs["expect_s"] == 6 * H
    j = job.classify(H, age=H)
    assert (j["expect_s"], j["expected_by"]) == (6 * H, "you")

    assert bg.clear_expectation(JOB_ID, now=NOW) is True
    assert bg.clear_expectation(JOB_ID, now=NOW) is False
    j = job.classify(H, age=H)
    assert (j["expect_s"], j["expected_by"]) == (5400.0, "command")


def test_the_store_file_shape_and_pruning(store):
    bg.set_expectation("old", 6 * H, "you", "s1", now=NOW - 2 * 86400)
    bg.set_expectation("new", 2 * H, "agent", "s2", now=NOW)
    data = json.loads(store.read_text())
    assert data["version"] == 1
    assert set(data["jobs"]) == {"new"}  # gone (unseen) for > 1 day: pruned
    entry = data["jobs"]["new"]
    assert entry["expect_s"] == 2 * H and entry["by"] == "agent"
    assert entry["set_at"] == NOW and entry["title"] == "s2"
    with pytest.raises(ValueError):
        bg.set_expectation("x", H, "command")


@linux_only
def test_a_live_job_keeps_its_expectation_from_being_pruned(job, store):
    bg.set_expectation(JOB_ID, 2 * 86400, "you", now=NOW)
    later = NOW + 1.5 * 86400
    job.classify(H, age=1.6 * 86400, now=later)  # seen: seen_at refreshed
    bg.set_expectation("other", H, "you", now=later + 0.4 * 86400)  # prunes
    data = json.loads(store.read_text())
    assert JOB_ID in data["jobs"]
    assert data["jobs"][JOB_ID]["seen_at"] == later


def test_an_unreadable_store_reads_as_empty(store):
    store.parent.mkdir(parents=True)
    store.write_text("{not json")
    assert bg._effective_expectation(JOB_ID, "sleep 9") == (None, None)


# --- the event ----------------------------------------------------------------
def _past_job(**kw):
    j = {
        "id": JOB_ID,
        "pid": PID,
        "command": "python train.py --epochs 90\nsecond line",
        "state": "waiting",
        "reason": "…",
        "started": NOW - 7 * H,
        "quiet_s": 2 * H,
        "expect_s": 6 * H,
        "expected_by": "agent",
        "eta": NOW - H,
        "longest_gap_s": 0.0,
        "threshold_s": 9 * H,
        "past_expected": True,
    }
    j.update(kw)
    return j


def test_job_past_expected_fires_once_per_job(store, monkeypatch):
    from backend.web import server

    emitted = []
    monkeypatch.setattr(
        server._events.BUS, "emit", lambda event, **kw: emitted.append((event, kw))
    )
    monkeypatch.setattr(bg, "_read_stat", lambda pid: {"start": START, "ppid": 1})
    jobs = {"now": [_past_job(past_expected=False)]}
    prov = types.SimpleNamespace(background_jobs=lambda name: jobs["now"])
    monkeypatch.setattr(server.providers, "resolve", lambda program: prov)
    inst = types.SimpleNamespace(Title="bg-1", Program="claude")

    assert server._background_jobs(inst, "bg-1") == jobs["now"]
    assert emitted == []  # not past it yet

    jobs["now"] = [_past_job()]
    for _ in range(3):  # every poll sees it; one event
        server._background_jobs(inst, "bg-1")
    assert [e for e, _ in emitted] == ["session.job_past_expected"]
    kw = emitted[0][1]
    assert kw["session"] == "bg-1"
    data = kw["data"]
    assert data["title"] == "bg-1" and data["job_id"] == JOB_ID
    assert data["command_head"] == "python train.py --epochs 90"
    assert data["expect_s"] == 6 * H
    assert data["elapsed_s"] > 7 * H - 1
    assert data["detail"] == "Job on bg-1 is past its expected ~6h — still quiet"

    # A server restart forgets the in-memory dedupe; the file remembers.
    bg._announced.clear()
    server._background_jobs(inst, "bg-1")
    assert len(emitted) == 1
    assert JOB_ID in json.loads(store.read_text())["announced"]


def test_announce_skips_a_vanished_process(store, monkeypatch):
    monkeypatch.setattr(bg, "_read_stat", lambda pid: None)
    assert bg.announce_past_expected([_past_job()]) == []
    assert bg.announce_past_expected(None) == []


# --- routes -------------------------------------------------------------------
@pytest.fixture
def client(monkeypatch, store):
    from backend.session.storage import Status as _Status
    from backend.web import server

    class _Inst:
        Status = _Status.Running
        Program = "claude"

    monkeypatch.setattr(server.ENGINE, "instances", {"bg-1": _Inst()})
    asked = []

    def find_job(name, job_id):
        asked.append((name, job_id))
        if job_id != JOB_ID:
            return None
        expect_s, by = bg._effective_expectation(job_id, "python train.py")
        return {
            "id": job_id,
            "state": "waiting",
            "reason": "…",
            "expect_s": expect_s,
            "expected_by": by,
            "eta": NOW + expect_s if expect_s else None,
        }

    monkeypatch.setattr(bg, "find_job", find_job)
    monkeypatch.setattr(bg, "recent", lambda name: None)
    c = TestClient(server.app)
    c.asked = asked
    return c


def test_expect_route_sets_and_clears(client, store):
    url = "/api/instances/bg-1/jobs/%s/expect" % JOB_ID
    r = client.post(url, json={"duration": "6h"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["expect_s"] == 6 * H and body["expected_by"] == "you"
    assert body["eta"] == NOW + 6 * H
    assert json.loads(store.read_text())["jobs"][JOB_ID]["title"] == "bg-1"
    assert all(n.endswith("bg-1") for n, _ in client.asked)

    # The agent's word doesn't override a person's.
    r = client.post(url, json={"duration": 3600, "by": "agent"})
    assert r.status_code == 200
    assert r.json()["ok"] is False and r.json()["expected_by"] == "you"

    r = client.delete(url)
    assert r.status_code == 200
    assert r.json()["ok"] is True and r.json()["expect_s"] is None
    assert client.delete(url).json()["ok"] is False

    r = client.post(url, json={"duration": "1h30m", "by": "agent"})
    assert r.json()["ok"] is True and r.json()["expected_by"] == "agent"
    assert r.json()["expect_s"] == 5400.0


@pytest.mark.parametrize(
    "body",
    [
        {"duration": "abc"},
        {"duration": "30s"},
        {"duration": "15d"},
        {"duration": True},
        {},
        {"duration": "6h", "by": "robot"},
    ],
)
def test_expect_route_refuses_a_bad_body(client, store, body):
    r = client.post("/api/instances/bg-1/jobs/%s/expect" % JOB_ID, json=body)
    assert r.status_code == 400
    assert not store.exists()


def test_expect_route_404s(client):
    assert (
        client.post(
            "/api/instances/nope/jobs/%s/expect" % JOB_ID, json={"duration": "6h"}
        ).status_code
        == 404
    )
    r = client.post("/api/instances/bg-1/jobs/bother/expect", json={"duration": "6h"})
    assert r.status_code == 404
    assert client.delete("/api/instances/bg-1/jobs/bother/expect").status_code == 404
    # A malformed id never reaches the scan.
    n = len(client.asked)
    r = client.post("/api/instances/bg-1/jobs/a.b/expect", json={"duration": "6h"})
    assert r.status_code == 404 and len(client.asked) == n


def test_expect_routes_are_registered():
    from backend.web import server

    paths = server.app.openapi()["paths"]
    p = paths["/api/instances/{title}/jobs/{job_id}/expect"]
    assert "post" in p and "delete" in p
    assert "post" in paths["/api/instances/{title}/background/{pid}/stop"]


# --- the MCP tool -------------------------------------------------------------
def _box():
    from tests.unit._mcp_fakes import make_box, row

    box, api, clock = make_box([row("orch"), row("w1", parent="orch")], me="w1")
    api.routes[("POST", "jobs/%s/expect" % JOB_ID)] = lambda title, payload, q: {
        "ok": True,
        "title": title,
        "expect_s": 21600.0,
        "expected_by": payload.get("by"),
        "eta": NOW,
    }
    return box, api, clock


def test_mcp_expect_background_job_posts_as_the_agent():
    from tests.unit._mcp_fakes import FakeCtx

    box, api, clock = _box()
    out = box.expect_background_job(
        {"job_id": JOB_ID, "duration": "6h"}, FakeCtx(clock)
    )
    assert out["ok"] is True and out["expected_by"] == "agent"
    posts = [c for c in api.calls if c[0] == "POST"]
    assert posts == [
        (
            "POST",
            "/api/instances/w1/jobs/%s/expect" % JOB_ID,
            {"duration": "6h", "by": "agent"},
        )
    ]
    out = box.expect_background_job(
        {"job_id": JOB_ID, "duration": 5400}, FakeCtx(clock)
    )
    assert api.calls[-1][2] == {"duration": 5400, "by": "agent"}


@pytest.mark.parametrize(
    "args",
    [
        {"job_id": "", "duration": "6h"},
        {"job_id": "../x", "duration": "6h"},
        {"job_id": "a b", "duration": "6h"},
        {"job_id": "x" * 65, "duration": "6h"},
        {"job_id": JOB_ID, "duration": ""},
        {"job_id": JOB_ID, "duration": True},
        {"job_id": JOB_ID, "duration": None},
        {"job_id": JOB_ID, "duration": ["6h"]},
    ],
)
def test_mcp_expect_background_job_validates_its_args(args):
    from tests.unit._mcp_fakes import FakeCtx

    box, api, clock = _box()
    with pytest.raises(ToolError):
        box.expect_background_job(args, FakeCtx(clock))
    assert [c for c in api.calls if c[0] == "POST"] == []


def test_mcp_expect_background_job_relays_the_servers_refusal():
    from backend import client as _client
    from tests.unit._mcp_fakes import FakeCtx

    box, api, clock = _box()
    path = "/api/instances/w1/jobs/%s/expect" % JOB_ID
    api.errors[("POST", path)] = _client.ApiError(400, "duration must be like 90m")
    with pytest.raises(ToolError, match="duration must be like 90m"):
        box.expect_background_job({"job_id": JOB_ID, "duration": "6y"}, FakeCtx(clock))


def test_mcp_expect_background_job_needs_a_session():
    from tests.unit._mcp_fakes import FakeCtx, make_box, row

    box, api, clock = make_box([row("orch")], me=None)
    box.identity.resolve = lambda rows: None
    with pytest.raises(ToolError):
        box.expect_background_job({"job_id": JOB_ID, "duration": "6h"}, FakeCtx(clock))


def test_the_instructions_teach_the_long_job_line():
    from backend.mcp import tools

    assert "expect_background_job" in tools.INSTRUCTIONS
    assert "MINDFLOCK_EXPECT=6h" in tools.INSTRUCTIONS
    assert "MINDFLOCK_EXPECT=6h" in tools._D_EXPECT
