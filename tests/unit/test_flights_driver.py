"""Flights driver (``backend.web.core.flight_driver``): the executors, the
pass, the announcements and the route operations — against fakes (a fake
server namespace, fake rows, a fake autopilot record), never the real engine
or ``~/.mindflock``."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from backend.web.core import events as _events
from backend.web.core import flight_driver as D
from backend.web.core import flights as F

NOW = time.time()


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setattr(D, "_JOBS", {})
    monkeypatch.setattr(D, "_REVIEWS", {})
    monkeypatch.setattr(D, "_CHECKS", {})
    yield


class FakeInst:
    def __init__(self, path="/repo"):
        self.Path = path
        self.Branch = "feat"
        self.Status = SimpleNamespace(name="Running")

    def GetWorktreePath(self):
        return self.Path

    def GetGitWorktree(self):
        return SimpleNamespace(GetRepoPath=lambda: self.Path)


@pytest.fixture
def srv(monkeypatch):
    ns = SimpleNamespace(
        _SERVER_BOOT_ID="boot-1",
        ENGINE=SimpleNamespace(instances={"s": FakeInst()}),
        _pending_rows=lambda: [],
        git_available=lambda: True,
        _peer_guard=SimpleNamespace(refusal=lambda inst, what: None),
        _lane_refusal=lambda title, lane: None,
        _publish_autopilot=lambda title: None,
        published=[],
    )
    monkeypatch.setattr(D, "_server", lambda: ns)
    return ns


@pytest.fixture
def emitted(monkeypatch):
    got = []
    unsub = _events.BUS.subscribe(
        lambda env: (
            got.append(env) if str(env.get("event", "")).startswith("flight.") else None
        )
    )
    yield got
    unsub()


@pytest.fixture
def row(monkeypatch):
    state = {"activity": "idle", "branch": "feat", "folder": "/repo", "pr_url": ""}

    def fake(flight, now, agent):
        out = {
            "present": flight["session"] == "s",
            "pending": False,
            "status": "running",
            "sleeping": False,
        }
        out.update(state)
        return out

    monkeypatch.setattr(D, "_session_obs", fake)
    return state


def fly(*steps, **kw):
    plan = F.save_plan({"name": "P", "steps": list(steps)})
    kw.setdefault("session", "s")
    return F.take_off(plan, **kw)


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Executors
# --------------------------------------------------------------------------- #
def test_agent_step_delivers_the_filled_prompt_through_the_queue(srv, row, monkeypatch):
    sent = []
    monkeypatch.setattr(D, "_deliver", lambda title, text: sent.append((title, text)))
    f = fly(
        {"kind": "agent", "config": {"prompt": "Fix {subject} on {branch}"}},
        subject={"kind": "manual", "title": "login"},
        branch="feat",
    )
    run(D.step_flight(f["id"]))
    assert sent == [("s", "Fix login on feat")]
    got = F.get(f["id"])
    assert got["step_states"]["s1"]["state"] == "running"
    assert got["step_states"]["s1"]["detail"] == {"sent": True}
    assert got["repo_root"] == "/repo"


def test_deliver_uses_the_prompt_queue(srv, monkeypatch):
    calls = []
    monkeypatch.setattr(
        D._prompt_queue, "enqueue", lambda t, x: calls.append(("enq", t, x))
    )
    monkeypatch.setattr(
        D._prompt_queue, "set_flags", lambda t, **kw: calls.append(("flags", t, kw))
    )
    import backend.web.core.sleep as sleep_mod

    monkeypatch.setattr(
        sleep_mod, "ensure_awake", lambda inst, title, why, srv=None: (False, None)
    )
    D._deliver("s", "hello")
    assert calls == [
        ("enq", "s", "hello"),
        ("flags", "s", {"enabled": True, "held": False}),
    ]
    with pytest.raises(D._StepFailed):
        D._deliver("ghost", "x")


def test_ship_step_arms_the_lane_with_source_flight(srv, row, monkeypatch):
    from backend.web.core import lanes

    armed = []

    def fake_arm(title, lane, **kw):
        armed.append((title, lane, kw))
        return {"started": 123.0}

    monkeypatch.setattr(lanes, "arm_session", fake_arm)
    f = fly({"kind": "ship", "config": {"lane": "pr", "ask_first": True}})
    run(D.step_flight(f["id"]))
    ((title, lane, kw),) = armed
    assert (title, lane) == ("s", "pr")
    assert kw["source"] == "flight" and kw["item"] == f["id"]
    assert kw["ask_first"] is True and kw["require_workspace"] is False
    assert F.get(f["id"])["step_states"]["s1"]["detail"] == {"armed_at": 123.0}
    # Observed done on the next pass → step done → flight landed.
    monkeypatch.setattr(
        D._autopilot, "get", lambda t: {"state": "done", "lane": "pr", "depth": "pr"}
    )
    run(D.step_flight(f["id"]))
    assert F.get(f["id"])["state"] == "done"


def test_ship_refusal_fails_the_step(srv, row, monkeypatch):
    from fastapi.responses import JSONResponse

    srv._lane_refusal = lambda title, lane: JSONResponse(
        {"error": "x works in this same folder"}, 409
    )
    f = fly({"kind": "ship", "config": {"lane": "push"}})
    run(D.step_flight(f["id"]))
    got = F.get(f["id"])
    assert got["state"] == "halted" and got["reason"] == "x works in this same folder"


def test_wait_step_sets_its_timer(srv, row):
    f = fly({"kind": "wait", "config": {"minutes": 5}})
    run(D.step_flight(f["id"]))
    until = F.get(f["id"])["step_states"]["s1"]["detail"]["until"]
    assert 290 < until - time.time() <= 300


def test_notify_and_wait_chain_in_one_pass(srv, row, monkeypatch):
    sent = []
    monkeypatch.setattr(D, "_run_notify", lambda f, s: sent.append(s["config"]["text"]))
    f = fly(
        {"kind": "notify", "config": {"text": "one"}},
        {"kind": "notify", "config": {"text": "two"}},
    )
    run(D.step_flight(f["id"]))
    assert sent == ["one", "two"]
    assert F.get(f["id"])["state"] == "done"


def test_command_step_runs_in_a_thread(srv, row, tmp_path):
    row["folder"] = str(tmp_path)
    f = fly(
        {"kind": "command", "config": {"command": "pwd; echo out"}},
        {"kind": "command", "config": {"command": "echo bad; exit 3"}},
    )
    run(D.step_flight(f["id"]))
    for _ in range(100):
        if not D._JOBS[(f["id"], "s1")]["running"]:
            break
        time.sleep(0.02)
    run(D.step_flight(f["id"]))
    got = F.get(f["id"])
    assert got["step_states"]["s1"]["state"] == "done"
    assert str(tmp_path) in got["step_states"]["s1"]["detail"]["output"]
    for _ in range(100):
        job = D._JOBS.get((f["id"], "s2"))
        if job and not job["running"]:
            break
        time.sleep(0.02)
    run(D.step_flight(f["id"]))
    got = F.get(f["id"])
    assert got["state"] == "halted" and got["reason"] == "exited 3: bad"
    assert (f["id"], "s1") not in D._JOBS


def test_ticket_step_calls_the_provider(srv, row, monkeypatch):
    calls = []

    class Prov:
        async def set_state(self, tid, state):
            calls.append(("state", tid, state))

        async def add_comment(self, tid, body):
            calls.append(("comment", tid, body))

    import backend.ticket_ingestion.config as cfg_mod
    import backend.ticket_ingestion.providers as prov_mod

    monkeypatch.setattr(
        cfg_mod,
        "config_for_launch",
        lambda _: SimpleNamespace(source_for=lambda k: object() if k == "sc" else None),
    )
    monkeypatch.setattr(prov_mod, "get_provider", lambda src: Prov())
    f = fly(
        {"kind": "ticket", "config": {"state": "Done", "comment": "Merged {ticket}"}},
        subject={"kind": "ticket", "source": "sc", "id": "42"},
    )
    run(D.step_flight(f["id"]))
    assert calls == [("state", "42", "Done"), ("comment", "42", "Merged 42")]
    assert F.get(f["id"])["state"] == "done"
    g = fly(
        {"kind": "ticket", "config": {"comment": "x"}},
        session="t",
        subject={"kind": "ticket", "source": "gone", "id": "1"},
    )
    srv.ENGINE.instances["t"] = FakeInst()
    run(D.step_flight(g["id"]))
    got = F.get(g["id"])
    assert got["state"] == "done" and got["step_states"]["s1"]["state"] == "skipped"


def test_verify_step_asks_for_the_checklist_then_runs_it(srv, row, monkeypatch):
    asked, ran = [], []
    srv._ensure_test_plan = lambda title, manual=False: asked.append((title, manual))

    async def fake_run(pid, req, payload):
        ran.append(pid)
        return SimpleNamespace(status_code=202, body=b"{}")

    srv.run_test_plan = fake_run
    facts = {}
    monkeypatch.setattr(D, "_verify_obs", lambda f, ss, sess: dict(facts))
    f = fly({"kind": "verify", "config": {"who": "both"}})
    run(D.step_flight(f["id"]))
    assert asked == [("s", True)]
    facts.update(
        plan="s",
        state="generated",
        steps=["s1"],
        settled=False,
        open_human=["look"],
        open_agent=["curl"],
        failed=[],
        live=False,
    )
    run(D.step_flight(f["id"]))
    assert ran == ["s"]
    assert F.get(f["id"])["step_states"]["s1"]["detail"]["run_started"] is True
    facts.update(open_agent=[])
    run(D.step_flight(f["id"]))
    got = F.get(f["id"])
    assert got["state"] == "waiting_you"
    assert got["step_states"]["s1"]["detail"]["items"] == ["look"]


def test_reviews_send_builds_the_consolidated_prompt(srv, row, monkeypatch):
    sent = []
    monkeypatch.setattr(D, "_deliver", lambda title, text: sent.append(text))
    from backend.ticket_ingestion.models import PRComment, PullRequest
    import datetime as dt

    pr = PullRequest(
        1, "feat", "abc", "main", "T", "https://pr/1", "", dt.datetime.now(), repo="o/r"
    )
    c = PRComment(
        id=9, kind="review", author="coderabbitai[bot]", body="rename x", url="u"
    )
    f = fly({"kind": "reviews", "config": {}})
    f = F.mutate(
        f["id"],
        lambda x: F.apply(
            x,
            {"action": "run", "detail": {"phase": "watch", "seeded": True, "seen": []}},
            NOW,
        ),
    )
    D._REVIEWS[f["id"]] = {
        "at": time.time(),
        "ids": ["9"],
        "comments": {"9": c},
        "pr": pr,
    }
    row["pr_url"] = "https://pr/1"
    run(D.step_flight(f["id"]))
    assert len(sent) == 1 and "rename x" in sent[0] and "PR #1" in sent[0]
    d = F.get(f["id"])["step_states"]["s1"]["detail"]
    assert d["phase"] == "agent" and d["seen"] == ["9"] and d["review_rounds"] == 1


def test_on_fail_agent_sends_the_failure_note(srv, row, monkeypatch):
    sent = []
    monkeypatch.setattr(D, "_deliver", lambda title, text: sent.append(text))
    monkeypatch.setattr(
        D, "_checks_obs", lambda *a: _async({"verdict": "failed", "failing": ["lint"]})
    )
    f = fly({"kind": "checks", "title": "CI", "config": {}, "on_fail": "agent"})
    run(D.step_flight(f["id"]))  # pending → run
    run(D.step_flight(f["id"]))  # failed → handed to the agent
    assert sent == [
        "[MindFlock flight] CI failed: failing checks: lint\nFix it; the flight retries the step once you are done."
    ]
    got = F.get(f["id"])
    assert (
        got["step_states"]["s1"]["detail"]["phase"] == "fix"
        and got["step_states"]["s1"]["rounds"] == 1
    )


async def _async(value):
    return value


# --------------------------------------------------------------------------- #
# Announcing, lease, hooks
# --------------------------------------------------------------------------- #
def test_needs_you_is_announced_once_and_survives_a_restart(srv, row, emitted):
    f = fly(
        {"kind": "you", "config": {"text": "Approve"}}, {"kind": "notify", "config": {}}
    )
    run(D.step_flight(f["id"]))
    run(D.step_flight(f["id"]))
    names = [e["event"] for e in emitted]
    assert names.count("flight.needs_you") == 1
    ny = next(e for e in emitted if e["event"] == "flight.needs_you")
    assert (
        ny["session"] == "s"
        and ny["data"]["text"] == "Approve"
        and ny["data"]["items"] == []
    )
    assert ny["data"]["step"]["kind"] == "you"
    assert F.get(f["id"])["announced"] == ["s1:0:you"]
    D._announce(None, F.get(f["id"]))
    assert [e["event"] for e in emitted].count("flight.needs_you") == 1


def test_quiet_you_step_does_not_ring(srv, row, emitted):
    f = fly({"kind": "you", "config": {"text": "x", "notify": False}})
    run(D.step_flight(f["id"]))
    assert "flight.needs_you" not in [e["event"] for e in emitted]


def test_finished_is_announced_once(srv, row, emitted, monkeypatch):
    monkeypatch.setattr(D, "_run_notify", lambda f, s: None)
    f = fly({"kind": "notify", "config": {}})
    run(D.step_flight(f["id"]))
    run(D.step_flight(f["id"]))
    assert [e["event"] for e in emitted].count("flight.finished") == 1
    fin = next(e for e in emitted if e["event"] == "flight.finished")
    assert fin["data"]["state"] == "done" and fin["data"]["detail"] == "P landed"


def test_another_servers_lease_is_respected(srv, row, monkeypatch):
    sent = []
    monkeypatch.setattr(D, "_run_notify", lambda f, s: sent.append(1))
    f = fly({"kind": "notify", "config": {}})
    F.claim(f["id"], "other-boot", time.time())
    run(D.step_flight(f["id"]))
    assert sent == [] and F.get(f["id"])["state"] == "running"


def test_session_removed_cancels_the_flight(srv, row, emitted):
    f = fly({"kind": "you", "config": {"text": "x"}})
    D.on_session_removed("s")
    got = F.get(f["id"])
    assert got["state"] == "cancelled" and got["reason"] == "its session was deleted"
    assert "flight.finished" in [e["event"] for e in emitted]
    D.on_session_removed("nobody")  # no flight: a no-op


def test_session_created_attaches_the_pending_flight(srv, emitted):
    srv.ENGINE.instances["sc-1"] = FakeInst("/main")
    f = F.arm_pending("sc-1", "builtin:ticket", {"kind": "ticket", "id": "1"})
    F.mutate(f["id"], lambda x: x.update(gone_since=5.0))
    got = D.on_session_created("sc-1")
    assert got["repo_root"] == "/main" and got["gone_since"] == 0.0
    assert emitted[-1]["event"] == "flight.changed"
    assert D.on_session_created("nobody") is None


def test_pass_prunes_and_announces_gone_flights(srv, emitted, monkeypatch):
    f = fly({"kind": "notify", "config": {}}, session="ghost")
    F.mutate(f["id"], lambda x: x.update(started=1.0, gone_since=1.0))
    run(D.run_pass())
    assert F.get(f["id"])["state"] == "cancelled"
    assert "flight.finished" in [e["event"] for e in emitted]


# --------------------------------------------------------------------------- #
# Operations
# --------------------------------------------------------------------------- #
def test_start_attach_and_create(srv, monkeypatch):
    plan = F.save_plan(
        {
            "name": "P",
            "steps": [
                {"kind": "agent", "config": {"prompt": "do {subject}"}},
                {"kind": "notify"},
            ],
            "session": {"repo_path": "/r", "model": "opus"},
        }
    )
    f = run(D.start({"plan_id": plan["id"], "title": "s"}))
    assert f["cursor"] == 1 and f["session"] == "s"
    with pytest.raises(F.FlightError) as err:
        run(D.start({"plan_id": plan["id"], "title": "s"}))
    assert err.value.error == "in_flight"
    with pytest.raises(F.FlightError) as err:
        run(D.start({"plan_id": plan["id"], "title": "ghost"}))
    assert err.value.status == 404
    with pytest.raises(F.FlightError) as err:
        run(D.start({"plan_id": "nope"}))
    assert err.value.status == 404
    with pytest.raises(F.FlightError) as err:
        run(D.start({"plan_id": plan["id"], "subject": {"kind": "ticket", "id": "1"}}))
    assert (err.value.status, err.value.error) == (501, "not_wired")

    created = []
    import backend.web.core.session_create as sc

    async def fake_create(payload, **kw):
        created.append(payload)
        return 202, {"title": "new-1"}

    monkeypatch.setattr(sc, "create_result", fake_create)
    g = run(
        D.start(
            {"plan_id": plan["id"], "subject": {"kind": "schedule", "title": "nightly"}}
        )
    )
    assert created == [
        {"repo_path": "/r", "prompt": "do nightly", "profile_model": "opus"}
    ]
    assert g["session"] == "new-1" and g["step_states"]["s1"]["state"] == "running"
    assert g["step_states"]["s1"]["detail"] == {"seeded": True}

    async def refuse(payload, **kw):
        return 409, {"error": "title taken"}

    monkeypatch.setattr(sc, "create_result", refuse)
    with pytest.raises(F.FlightError) as err:
        run(D.start({"plan_id": plan["id"], "repo_path": "/x"}))
    assert (err.value.status, err.value.error) == (409, "title taken")
    bare = F.save_plan({"name": "B", "steps": [{"kind": "notify"}]})
    with pytest.raises(F.FlightError) as err:
        run(D.start({"plan_id": bare["id"]}))
    assert err.value.error == "no_repo"


def test_operate_done_on_verify_records_human_passes(srv, monkeypatch):
    from backend.web.core import test_plans as tp

    recorded = []
    monkeypatch.setattr(tp, "get", lambda pid: {"id": pid, "steps": [], "runs": []})
    monkeypatch.setattr(
        tp, "open_human_steps", lambda plan: [{"id": "s2"}, {"id": "s3"}]
    )
    monkeypatch.setattr(
        tp,
        "record_result",
        lambda pid, sid, res, note="", by="": recorded.append((pid, sid, res, by)),
    )
    f = fly({"kind": "verify", "config": {"who": "you"}})
    F.mutate(
        f["id"],
        lambda x: F.apply(
            x, {"action": "you", "detail": {"plan": "s", "items": ["a"]}}, NOW
        ),
    )
    got = D.operate(f["id"], "done", "s1", "")
    assert recorded == [("s", "s2", "pass", "human"), ("s", "s3", "pass", "human")]
    assert got["state"] == "done"


def test_operate_errors_and_remove(srv):
    with pytest.raises(F.FlightError) as err:
        D.operate("f_nope", "pause")
    assert err.value.status == 404
    f = fly({"kind": "you", "config": {}})
    with pytest.raises(F.FlightError) as err:
        D.remove(f["id"])
    assert err.value.error == "in_flight"
    D.operate(f["id"], "cancel")
    D.remove(f["id"])
    assert F.get(f["id"]) is None


def test_list_dto_keeps_twenty_finished(srv):
    for i in range(23):
        f = fly({"kind": "notify", "config": {}}, session="l%d" % i)
        F.mutate(f["id"], lambda x, i=i: F.op_cancel(x, now=NOW + i))
    live = fly({"kind": "notify", "config": {}}, session="live")
    out = D.list_dto()
    assert out[0]["id"] == live["id"] and len(out) == 21
    assert len(D.list_dto(True)) == 24
