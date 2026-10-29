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
    # A start reads (and may disarm) the session's fast-track record.
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
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
        D._autopilot,
        "get",
        lambda t: {
            "state": "done",
            "lane": "pr",
            "depth": "pr",
            "source": "flight",
            "item": f["id"],
        },
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
        async def list_states(self):
            return [
                {"id": "500", "name": "Dev · Done", "label": "Done"},
                {"id": "400", "name": "In progress"},
            ]

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
    # "Done" is a label; set_state gets the provider's ID.
    assert calls == [("state", "42", "500"), ("comment", "42", "Merged 42")]
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
    # No 501 any more: a subject of any kind is recorded on the flight.
    srv.ENGINE.instances["t1"] = FakeInst()
    tf = run(
        D.start(
            {
                "plan_id": plan["id"],
                "title": "t1",
                "subject": {
                    "kind": "ticket",
                    "source": "sc",
                    "id": "1",
                    "title": "Fix",
                },
            }
        )
    )
    assert tf["subject"]["kind"] == "ticket" and tf["subject"]["id"] == "1"

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


# --------------------------------------------------------------------------- #
# Review fixes
# --------------------------------------------------------------------------- #
def test_pause_during_an_effect_neither_repeats_nor_unpauses(srv, row, monkeypatch):
    """Review #1: a pause landing between the intent and the outcome keeps
    the effect's outcome, stays paused, and the prompt is sent ONCE."""
    sent = []

    def deliver(title, text):
        sent.append(text)
        # A person presses Pause while the prompt is being delivered.
        D.operate(f["id"], "pause")

    monkeypatch.setattr(D, "_deliver", deliver)
    f = fly({"kind": "agent", "config": {"prompt": "go"}})
    run(D.step_flight(f["id"]))
    got = F.get(f["id"])
    assert sent == ["go"] and got["state"] == "paused"
    ss = got["step_states"]["s1"]
    assert ss["state"] == "running" and ss["detail"] == {"sent": True}
    D.operate(f["id"], "resume")
    run(D.step_flight(f["id"]))
    run(D.step_flight(f["id"]))
    assert sent == ["go"]


def test_cancel_during_an_effect_drops_the_outcome(srv, row, monkeypatch):
    """Review #1: the flight was cancelled mid-effect — nothing is applied on
    top of it and nothing runs again."""
    from backend.web.core import lanes

    armed = []

    def fake_arm(title, lane, **kw):
        armed.append(lane)
        D.operate(f["id"], "cancel")
        return {"started": 1.0}

    monkeypatch.setattr(lanes, "arm_session", fake_arm)
    f = fly({"kind": "ship", "config": {"lane": "push"}})
    run(D.step_flight(f["id"]))
    got = F.get(f["id"])
    assert armed == ["push"] and got["state"] == "cancelled"
    assert "performing" not in got["step_states"]["s1"]["detail"]
    run(D.step_flight(f["id"]))
    assert armed == ["push"]


def test_a_flight_moved_before_the_intent_is_not_performed(srv, row, monkeypatch):
    """Review #1: the intent is only recorded if the flight is as it was read."""
    sent = []
    monkeypatch.setattr(D, "_deliver", lambda t, x: sent.append(x))
    f = fly({"kind": "agent", "config": {"prompt": "go"}})
    real = D.observe

    async def observe_then_skip(flight, now):
        out = await real(flight, now)
        D.operate(f["id"], "skip", "s1")
        return out

    monkeypatch.setattr(D, "observe", observe_then_skip)
    run(D.step_flight(f["id"]))
    assert sent == [] and F.get(f["id"])["state"] == "done"


def test_an_unchanged_wait_writes_nothing(srv, row, monkeypatch):
    """Review #6: an hour-long agent step must not rewrite flights.json (nor
    announce) every pass once its wait has stopped changing."""
    monkeypatch.setattr(D, "_deliver", lambda t, x: None)
    f = fly({"kind": "agent", "config": {"prompt": "go"}})
    run(D.step_flight(f["id"]))
    row["activity"] = "working"
    run(D.step_flight(f["id"]))  # records worked_at
    saves = []
    real = F._save_flights_locked
    monkeypatch.setattr(
        F, "_save_flights_locked", lambda fl: saves.append(1) or real(fl)
    )
    run(D.step_flight(f["id"]))
    run(D.step_flight(f["id"]))
    # Only the lease refresh writes (claim); the step's wait does not.
    assert len(saves) == 2


def test_ticket_state_must_exist(srv, row, monkeypatch):
    """Review #2: an unknown state fails clearly, naming the real ones."""

    class Prov:
        async def list_states(self):
            return [{"id": "1", "name": "Started"}, {"id": "2", "name": "Shipped"}]

        async def set_state(self, tid, state):
            raise AssertionError("never called")

        async def add_comment(self, tid, body):
            raise AssertionError("never called")

    import backend.ticket_ingestion.config as cfg_mod
    import backend.ticket_ingestion.providers as prov_mod

    monkeypatch.setattr(
        cfg_mod,
        "config_for_launch",
        lambda _: SimpleNamespace(source_for=lambda k: object()),
    )
    monkeypatch.setattr(prov_mod, "get_provider", lambda src: Prov())
    f = fly(
        {"kind": "ticket", "config": {"state": "Done"}},
        subject={"kind": "ticket", "source": "sc", "id": "9"},
    )
    run(D.step_flight(f["id"]))
    got = F.get(f["id"])
    assert got["state"] == "halted"
    assert got["reason"] == "the tracker has no state 'Done' (it has: Started, Shipped)"
    assert "performing" not in got["step_states"]["s1"]["detail"]


def test_ticket_with_no_state_only_comments(srv, row, monkeypatch):
    calls = []

    class Prov:
        async def list_states(self):
            raise AssertionError("not needed")

        async def set_state(self, tid, state):
            calls.append("state")

        async def add_comment(self, tid, body):
            calls.append(body)

    import backend.ticket_ingestion.config as cfg_mod
    import backend.ticket_ingestion.providers as prov_mod

    monkeypatch.setattr(
        cfg_mod,
        "config_for_launch",
        lambda _: SimpleNamespace(
            source_for=lambda k: SimpleNamespace(start_state="x")
        ),
    )
    monkeypatch.setattr(prov_mod, "get_provider", lambda src: Prov())
    f = fly(
        *F.get_plan("builtin:ticket")["steps"][-1:],
        subject={"kind": "ticket", "source": "sc", "id": "9"},
    )
    run(D.step_flight(f["id"]))
    assert calls == ["Merged by MindFlock"] and F.get(f["id"])["state"] == "done"


def test_start_with_a_bad_step_creates_no_session(srv, monkeypatch):
    """Review #3."""
    import backend.web.core.session_create as sc

    created = []

    async def fake_create(payload, **kw):
        created.append(payload)
        return 202, {"title": "orphan"}

    monkeypatch.setattr(sc, "create_result", fake_create)
    plan = F.save_plan(
        {"name": "P", "steps": [{"kind": "notify"}], "session": {"repo_path": "/r"}}
    )
    with pytest.raises(F.FlightError) as err:
        run(D.start({"plan_id": plan["id"], "start_at": "nope"}))
    assert err.value.error == "unknown_step" and created == []


def test_start_announces_the_halted_flight_it_replaces(srv, emitted):
    """Review #4."""
    plan = F.save_plan({"name": "P", "steps": [{"kind": "notify"}]})
    old = run(D.start({"plan_id": plan["id"], "title": "s", "start_at": "s1"}))
    F.mutate(
        old["id"],
        lambda f: F.apply(f, {"action": "fail", "reason": "x", "then": "halt"}, NOW),
    )
    emitted.clear()
    run(D.start({"plan_id": plan["id"], "title": "s", "start_at": "s1"}))
    fin = [e for e in emitted if e["event"] == "flight.finished"]
    assert [e["data"]["id"] for e in fin] == [old["id"]]
    assert fin[0]["data"]["reason"] == "replaced by a new flight"
    assert any(k.startswith("finished:") for k in F.get(old["id"])["announced"])


def test_the_pass_sweeps_a_finish_nobody_announced(srv, emitted):
    """Review #4: intake's arm_pending replaces without announcing; the pass
    says it once."""
    f = F.arm_pending("s", "builtin:schedule", {"kind": "schedule"})
    F.mutate(
        f["id"],
        lambda x: F.apply(x, {"action": "fail", "reason": "x", "then": "halt"}, NOW),
    )
    F.arm_pending("s", "builtin:schedule", {"kind": "schedule"})
    emitted.clear()
    run(D.run_pass())
    run(D.run_pass())
    fin = [
        e
        for e in emitted
        if e["event"] == "flight.finished" and e["data"]["id"] == f["id"]
    ]
    assert len(fin) == 1


def test_an_effect_that_fails_with_on_fail_agent_tells_the_agent(srv, row, monkeypatch):
    sent = []
    monkeypatch.setattr(D, "_deliver", lambda t, x: sent.append(x))
    monkeypatch.setattr(
        D,
        "_ensure_checklist",
        lambda f: (_ for _ in ()).throw(D._StepFailed("no checklist")),
    )
    f = fly({"kind": "verify", "title": "Verify", "config": {}, "on_fail": "agent"})
    run(D.step_flight(f["id"]))
    assert sent and sent[0].startswith("[MindFlock flight] Verify failed: no checklist")
    assert F.get(f["id"])["step_states"]["s1"]["detail"]["phase"] == "fix"


# --------------------------------------------------------------------------- #
# Live flights: ad-hoc starts, absorbing a fast-track, steps changed in the air
# --------------------------------------------------------------------------- #
def _own_run(title="s", **kw):
    rec = D._autopilot.arm(
        title, kw.pop("depth", "pr"), source="session", lane="pr", message="Fix it"
    )
    if kw:
        rec = D._autopilot.update(title, **kw)
    return rec


def test_absorbed_ship_steps_drive_lanes_like_normal_ones(srv, row, monkeypatch):
    from backend.web.core import lanes

    srv._session_stage = lambda inst: {"stage": "committed"}
    published = []
    srv._publish_autopilot = published.append
    _own_run()
    plan = F.save_plan({"name": "P", "steps": [{"kind": "agent"}, {"kind": "notify"}]})
    absorbed = []
    f = run(D.start({"plan_id": plan["id"], "title": "s"}, absorbed))
    assert absorbed == ["push", "pr"]
    assert [(s["id"], s["kind"]) for s in f["steps"]] == [
        ("s1", "agent"),
        ("s3", "ship"),
        ("s4", "ship"),
        ("s2", "notify"),
    ]
    assert f["cursor"] == 1 and f["steps"][1]["config"]["message"] == "Fix it"
    assert f["history"][-1]["event"] == "added"
    # The run is disarmed: the flight carries the ladder on, alone.
    assert D._autopilot.get("s") is None and published == ["s"]
    armed = []

    def fake_arm(title, lane, **kw):
        armed.append((title, lane, kw["source"], kw["item"]))
        return {"started": 1.0}

    monkeypatch.setattr(lanes, "arm_session", fake_arm)
    run(D.step_flight(f["id"]))
    assert armed == [("s", "push", "flight", f["id"])]
    rec = {"state": "done", "lane": "push", "depth": "push"}
    rec.update(source="flight", item=f["id"])
    monkeypatch.setattr(D._autopilot, "get", lambda t: dict(rec))
    run(D.step_flight(f["id"]))
    assert armed[-1] == ("s", "pr", "flight", f["id"])
    assert F.get(f["id"])["cursor"] == 2


def test_a_halted_run_is_absorbed_with_its_reason(srv):
    srv._session_stage = lambda inst: {"stage": "agent"}
    _own_run(state="halted", reason="pre-commit failed")
    f = run(D.start({"title": "s", "steps": [{"kind": "notify"}]}))
    assert f["plan_id"] == "adhoc" and f["plan_name"] == "Flight on s"
    assert [(s["id"], s["kind"], s["config"].get("lane")) for s in f["steps"]] == [
        ("s2", "ship", "commit"),
        ("s3", "ship", "push"),
        ("s4", "ship", "pr"),
        ("s1", "notify", None),
    ]
    assert f["cursor"] == 0
    assert f["step_states"][f["steps"][0]["id"]]["state"] == "pending"
    assert "pre-commit failed" in f["history"][-1]["note"]
    assert D._autopilot.get("s") is None


def test_runs_that_are_not_absorbed_are_left_alone(srv):
    srv._session_stage = lambda inst: {"stage": ""}
    plan = F.save_plan({"name": "P", "steps": [{"kind": "notify"}]})
    for kw in ({"state": "done"}, {}):
        _own_run(**kw)
        body = {"plan_id": plan["id"], "title": "s"}
        if not kw:
            body["absorb_fasttrack"] = False
        absorbed = []
        f = run(D.start(body, absorbed))
        assert absorbed == [] and [s["kind"] for s in f["steps"]] == ["notify"]
        assert D._autopilot.get("s") is not None
        F.mutate(f["id"], lambda x: F.op_cancel(x))
    # A flight's own record is never taken for the session's run.
    D._autopilot.arm("s", "push", source="flight", item="f_old")
    f = run(D.start({"plan_id": plan["id"], "title": "s"}))
    assert len(f["steps"]) == 1 and D._autopilot.get("s")["item"] == "f_old"


def test_adhoc_start_needs_a_session_or_a_folder(srv, monkeypatch):
    with pytest.raises(F.FlightError) as err:
        run(D.start({"steps": [{"kind": "notify"}]}))
    assert (err.value.status, err.value.error) == (400, "needs_title")
    with pytest.raises(F.PlanError):
        run(D.start({"title": "s", "steps": [{"kind": "moon"}]}))
    with pytest.raises(F.FlightError) as err:
        run(D.start({"plan_id": "p_nope", "title": "s", "steps": []}))
    assert err.value.error == "unknown_plan"
    import backend.web.core.session_create as sc

    created = []

    async def fake_create(payload, **kw):
        created.append(payload)
        return 202, {"title": "new-1"}

    monkeypatch.setattr(sc, "create_result", fake_create)
    f = run(
        D.start(
            {
                "steps": [{"kind": "agent", "config": {"prompt": "go"}}],
                "repo_path": "/r",
            }
        )
    )
    assert created == [{"repo_path": "/r", "prompt": "go"}]
    assert (f["session"], f["plan_name"], f["plan_id"]) == (
        "new-1",
        "Flight on new-1",
        "adhoc",
    )
    assert f["step_states"]["s1"]["detail"] == {"seeded": True}
    # A subject's headline names the flight; plan + steps = both, in order.
    plan = F.save_plan({"name": "P", "steps": [{"kind": "agent"}, {"kind": "notify"}]})
    srv.ENGINE.instances["t"] = FakeInst()
    g = run(
        D.start(
            {
                "plan_id": plan["id"],
                "title": "t",
                "steps": [{"kind": "wait", "config": {"minutes": 1}}],
                "subject": {"kind": "manual", "title": "Login bug"},
            }
        )
    )
    assert g["plan_id"] == plan["id"] and g["plan_name"] == "P"
    assert [s["kind"] for s in g["steps"]] == ["agent", "notify", "wait"]
    srv.ENGINE.instances["u"] = FakeInst()
    h = run(
        D.start(
            {
                "title": "u",
                "steps": [{"kind": "notify"}],
                "subject": {"kind": "manual", "title": "Login bug"},
            }
        )
    )
    assert h["plan_name"] == "Login bug"


def test_change_steps_announces_and_wakes(srv, row, emitted, monkeypatch):
    woke = []
    monkeypatch.setattr(D, "wake", lambda: woke.append(1))
    f = fly({"kind": "agent"}, {"kind": "notify"})
    after = D.change_steps(
        f["id"], "add", raw={"kind": "wait", "config": {"minutes": 2}}
    )
    assert [s["id"] for s in after["steps"]] == ["s1", "s2", "s3"]
    after = D.change_steps(f["id"], "edit", "s3", {"title": "Breathe"})
    assert after["steps"][2]["title"] == "Breathe"
    after = D.change_steps(f["id"], "remove", "s2")
    assert [s["id"] for s in after["steps"]] == ["s1", "s3"]
    assert [e["event"] for e in emitted] == ["flight.changed"] * 3
    assert len(woke) == 3
    with pytest.raises(F.FlightError) as err:
        D.change_steps("f_nope", "add", raw={"kind": "notify"})
    assert err.value.status == 404
    with pytest.raises(F.FlightError) as err:
        D.change_steps(f["id"], "remove", "s1")
    assert err.value.error == "in_progress"


def test_a_step_added_to_a_finishing_flight_is_flown(srv, row, monkeypatch):
    sent = []
    monkeypatch.setattr(D, "_run_notify", lambda flight, step: sent.append(step["id"]))
    f = fly({"kind": "notify"})
    D.change_steps(f["id"], "add", raw={"kind": "notify"})
    run(D.step_flight(f["id"]))
    got = F.get(f["id"])
    assert sent == ["s1", "s2"] and got["state"] == "done"
