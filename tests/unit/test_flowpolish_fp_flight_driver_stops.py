"""flow-polish (fp-flight-driver): the driver never leaves a stop silently
stuck and never claims what did not happen — an executor's unexpected error
fails the stop saying so, Verify's observation says when its check is
unreadable or gone, a blocked/memory park takes Done as "check again", a stop
parked again rings again, memory waits say why and park on you after 5 min,
a notify stop says where it really went, and routine outcomes log at INFO.
Against fakes only (a fake server namespace, fake rows, fake test plans)."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from backend.web.core import events as _events
from backend.web.core import flight_driver as D
from backend.web.core import flights as F
from backend.web.core import test_plans as T

NOW = time.time()


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
    monkeypatch.setattr(D, "_JOBS", {})
    monkeypatch.setattr(D, "_REVIEWS", {})
    monkeypatch.setattr(D, "_CHECKS", {})
    monkeypatch.setattr(D, "wake", lambda: None)


class FakeInst:
    def __init__(self, path="/repo", branch="feat"):
        self.Path = path
        self.Branch = branch
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
    )
    monkeypatch.setattr(D, "_server", lambda: ns)
    return ns


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


@pytest.fixture
def emitted():
    got = []
    unsub = _events.BUS.subscribe(
        lambda env: (
            got.append(env) if str(env.get("event", "")).startswith("flight.") else None
        )
    )
    yield got
    unsub()


def fly(*steps, **kw):
    plan = F.save_plan({"name": "P", "steps": list(steps)})
    kw.setdefault("session", "s")
    return F.take_off(plan, **kw)


def run(coro):
    return asyncio.run(coro)


def park(f, detail, sid="s1"):
    """Park the current stop on the person with ``detail`` (as a judge does)."""
    return F.mutate(
        f["id"],
        lambda x: F.apply(x, {"action": "you", "note": "why", "detail": detail}, NOW),
    )


# --------------------------------------------------------------------------- #
# 1. An executor's unexpected error fails the stop, saying so
# --------------------------------------------------------------------------- #
def test_an_unexpected_executor_error_fails_the_stop_and_clears_the_mark(
    srv, row, monkeypatch
):
    def boom(flight, step):
        raise KeyError("lane")

    monkeypatch.setattr(D, "_arm", lambda *a, **k: boom(None, None))
    logged = []
    monkeypatch.setattr(D, "_log", lambda fmt, *a, **k: logged.append((fmt, a, k)))
    f = fly({"kind": "ship", "title": "Push", "config": {"lane": "push"}})
    run(D.step_flight(f["id"]))
    got = F.get(f["id"])
    ss = got["step_states"]["s1"]
    assert got["state"] == "halted"
    assert ss["state"] == "failed"
    assert "Push hit an error: 'lane'" in got["reason"]
    # The in-progress mark is gone: no "carrying out Push" for 10 minutes.
    assert "performing" not in ss["detail"]
    # The traceback went to the log, at ERROR.
    assert any("hit an error" in fmt and "Traceback" in a[-1] for fmt, a, k in logged)
    # The next pass does not carry the effect out again.
    calls = []
    monkeypatch.setattr(D, "_arm", lambda *a, **k: calls.append(1) or {})
    run(D.step_flight(f["id"]))
    assert calls == []


def test_an_unexpected_error_is_never_handed_to_the_agent(srv, row, monkeypatch):
    sent = []
    monkeypatch.setattr(D, "_deliver", lambda title, text: sent.append(text))

    async def bad(flight, step):
        raise RuntimeError("tracker exploded\nwith a second line")

    monkeypatch.setattr(D, "_run_ticket", bad)
    f = fly(
        {"kind": "ticket", "on_fail": "agent", "config": {"state": "Done"}},
        subject={"kind": "ticket", "source": "sc", "id": "7", "title": "t"},
    )
    run(D.step_flight(f["id"]))
    got = F.get(f["id"])
    assert sent == []  # a server error is not the agent's to fix
    assert got["state"] == "halted"
    assert "hit an error: tracker exploded with a second line" in got["reason"]


# --------------------------------------------------------------------------- #
# 2. Verify's observation: unreadable / gone is said, not "getting ready"
# --------------------------------------------------------------------------- #
def test_verify_obs_names_a_check_that_is_gone(srv, monkeypatch):
    monkeypatch.setattr(D, "_verify_env", lambda flight, where: (None, False))
    monkeypatch.setattr(T, "get", lambda pid: None)
    f = fly({"kind": "verify"})
    assert D._verify_obs(f, {"detail": {"plan": "p9"}}, {}) == {"plan_gone": "p9"}


def test_verify_obs_with_no_check_yet_is_still_empty(srv, monkeypatch):
    monkeypatch.setattr(D, "_verify_env", lambda flight, where: (None, False))
    monkeypatch.setattr(T, "owner_for_branch", lambda title, repo, branch: "")
    f = fly({"kind": "verify"})
    assert D._verify_obs(f, {"detail": {}}, {"branch": "feat"}) == {}


def test_an_unreadable_check_is_an_error_in_the_observation(srv, row, monkeypatch):
    def boom(flight, ss, sess):
        raise OSError("disk on fire")

    monkeypatch.setattr(D, "_verify_obs", boom)
    f = fly({"kind": "verify"})
    f = F.mutate(
        f["id"],
        lambda x: F.apply(x, {"action": "run", "detail": {"plan": "p"}}, NOW),
    )
    obs = run(D.observe(f, NOW))
    assert obs["verify"] == {"error": "couldn't read the check: disk on fire"}


# --------------------------------------------------------------------------- #
# 3. The Verify perch: Done on blocked / memory = verify again
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("ask", ["blocked", "memory"])
def test_done_on_a_blocked_or_memory_perch_verifies_again(srv, ask):
    f = fly({"kind": "verify"})
    f = park(f, {"plan": "p", "ask": ask, "items": ["x — press Retry, or Skip"]})
    assert D._needs_you_perch(f, "s1") is True
    after = D.operate(f["id"], "done", "s1")
    ss = after["step_states"]["s1"]
    assert (after["state"], ss["state"]) == ("running", "pending")
    assert after["history"][-1]["note"] == "retry"


def test_retry_on_a_blocked_perch_is_never_refused(srv):
    f = fly({"kind": "verify"})
    f = park(f, {"plan": "p", "ask": "blocked", "items": ["press Retry"]})
    after = D.operate(f["id"], "retry", "s1")
    assert after["state"] == "running"
    assert after["step_states"]["s1"]["state"] == "pending"


def test_needs_you_is_no_longer_a_retry_ask(srv):
    f = fly({"kind": "verify"})
    f = park(f, {"plan": "p", "ask": "needs_you"})
    assert D._needs_you_perch(f, "s1") is False


def test_the_done_refusal_names_what_works(srv):
    f = fly({"kind": "verify", "title": "Verify"})
    f = park(f, {"plan": "p", "items": ["a"]})
    with pytest.raises(F.FlightError) as err:
        D.operate(f["id"], "done", "s1")
    assert err.value.error == "answer_behaviors"  # clients read the word
    assert (
        err.value.detail == "Verify isn't verified yet — Retry to verify again, or Skip"
    )
    assert "behavior" not in err.value.detail


def test_a_refused_verify_start_uses_the_vocabulary(monkeypatch):
    async def refuse(pid, req, payload):
        return SimpleNamespace(status_code=500, body=b"{}")

    monkeypatch.setattr(D, "_server", lambda: SimpleNamespace(run_test_plan=refuse))
    with pytest.raises(D._StepFailed) as err:
        run(D._start_verify_run("p"))
    assert str(err.value) == "couldn't start Verify: the server answered 500"


# --------------------------------------------------------------------------- #
# 4. Verify start / fix round / confirm items
# --------------------------------------------------------------------------- #
def test_agent_only_sends_walk_all_and_no_dead_extra_notes(srv, monkeypatch):
    got = []

    async def fake_run(pid, where="branch", notes="", walk="agent", confirm=False):
        got.append((pid, where, notes, walk, confirm))
        return {"started": True, "reason": "", "detail": ""}

    monkeypatch.setattr(D, "_start_verify_run", fake_run)
    f = fly({"kind": "verify", "config": {"who": "agent", "notes": "log in as qa"}})
    f = F.mutate(
        f["id"],
        lambda x: F.apply(
            x,
            {"action": "run", "detail": {"plan": "p", "extra_notes": "stale"}},
            NOW,
        ),
    )
    action = {"action": "run", "phase": "agent_run", "detail": {"plan": "p"}}
    run(D._perform(f, action, NOW))
    assert got == [("p", "branch", "log in as qa", "all", False)]


def test_the_run_request_carries_walk(monkeypatch):
    bodies = []

    async def route(pid, req, payload):
        bodies.append(payload)
        return SimpleNamespace(status_code=202, body=b"{}")

    monkeypatch.setattr(D, "_server", lambda: SimpleNamespace(run_test_plan=route))
    run(D._start_verify_run("p", "branch", "", D._verify_walk({"who": "agent"})))
    assert bodies[0]["walk"] == "all"


def _plan_with(results):
    return T._normalize(
        "p",
        {
            "id": "p",
            "branch": "feat",
            "state": "done",
            "steps": [
                {
                    "id": "a1",
                    "text": "Log in",
                    "expect": "the dashboard",
                    "actor": "agent",
                },
                {"id": "a2", "text": "Open billing", "expect": "", "actor": "agent"},
                {"id": "h1", "text": "Check the email", "actor": "human"},
            ],
            "runs": [{"at": NOW, "results": results, "outcome": "failed"}],
        },
    )


def test_failed_steps_carry_what_was_seen(srv, monkeypatch):
    plan = _plan_with(
        {
            "a1": {"result": "fail", "note": "got a 500\non submit", "by": "agent"},
            "a2": {"result": "pass", "note": "ok", "by": "agent"},
        }
    )
    monkeypatch.setattr(D, "_verify_env", lambda flight, where: (None, False))
    monkeypatch.setattr(T, "get", lambda pid: plan)
    f = fly({"kind": "verify"})
    v = D._verify_obs(f, {"detail": {"plan": "p"}}, {})
    assert v["failed"] == ["Log in — saw: got a 500 on submit"]
    # The agent's fix round is told what was expected and what was seen.
    f = F.mutate(
        f["id"],
        lambda x: F.apply(x, {"action": "run", "detail": {"plan": "p"}}, NOW),
    )
    note = D._fix_note(f["steps"][0], {"reason": "Failed · 1: Log in"}, f)
    assert "What Verify saw:" in note
    assert "- Log in\n  expected: the dashboard\n  seen: got a 500 on submit" in note
    assert note.endswith("the route retries the stop once you are done.")


def test_the_fix_handoff_to_the_agent_carries_the_evidence(srv, row, monkeypatch):
    plan = _plan_with({"a1": {"result": "fail", "note": "blank page", "by": "agent"}})
    monkeypatch.setattr(T, "get", lambda pid: plan)
    sent = []
    monkeypatch.setattr(D, "_deliver", lambda title, text: sent.append(text))
    f = fly({"kind": "verify", "on_fail": "agent"})
    f = F.mutate(
        f["id"],
        lambda x: F.apply(x, {"action": "run", "detail": {"plan": "p"}}, NOW),
    )
    run(D._perform(f, {"action": "fail", "then": "agent", "reason": "Failed"}, NOW))
    assert len(sent) == 1 and "seen: blank page" in sent[0]


def test_confirm_items_say_why_the_agent_could_not(srv, monkeypatch):
    plan = _plan_with(
        {
            "a1": {"result": "pass", "note": "ok", "by": "agent"},
            "a2": {"result": "blocked", "note": "needs a card", "by": "agent"},
            "h1": {"result": "", "by": ""},
        }
    )
    plan["runs"][-1]["outcome"] = "needs_you"
    monkeypatch.setattr(D, "_verify_env", lambda flight, where: (None, False))
    monkeypatch.setattr(T, "get", lambda pid: plan)
    f = fly({"kind": "verify"})
    v = D._verify_obs(f, {"detail": {"plan": "p"}}, {})
    assert v["confirm_items"] == [
        "Open billing — the agent couldn't: needs a card",
        "Check the email",
    ]
    # A ``why`` on the result wins over its note.
    plan["runs"][-1]["results"]["a2"]["why"] = "no test card in this env"
    v = D._verify_obs(f, {"detail": {"plan": "p"}}, {})
    assert (
        v["confirm_items"][0]
        == "Open billing — the agent couldn't: no test card in this env"
    )


# --------------------------------------------------------------------------- #
# 7. A stop that parks again rings again
# --------------------------------------------------------------------------- #
def test_a_stop_parked_again_after_retry_rings_again(srv, row, emitted, monkeypatch):
    clock = [NOW]
    monkeypatch.setattr(D.time, "time", lambda: clock[0])
    f = fly({"kind": "you", "config": {"text": "Approve"}})
    run(D.step_flight(f["id"]))
    run(D.step_flight(f["id"]))
    assert [e["event"] for e in emitted].count("flight.needs_you") == 1
    # Retry starts it afresh (rounds back to 0, a new since): it rings again.
    clock[0] = NOW + 30

    def again(x):
        x["step_states"]["s1"]["state"] = "failed"  # as from a halt
        F.op_retry(x, "s1", now=NOW + 30)

    F.mutate(f["id"], again)
    run(D.step_flight(f["id"]))
    assert [e["event"] for e in emitted].count("flight.needs_you") == 2


# --------------------------------------------------------------------------- #
# 10. Memory waits say why at once, and park on you after 5 minutes
# --------------------------------------------------------------------------- #
def _memory_refusal(monkeypatch):
    async def fake_run(pid, where="branch", notes="", walk="agent", confirm=False):
        return {"started": False, "reason": "memory", "detail": "2.1 GB free"}

    monkeypatch.setattr(D, "_start_verify_run", fake_run)


def test_a_verify_memory_wait_says_why_at_once(srv, monkeypatch):
    _memory_refusal(monkeypatch)
    f = fly({"kind": "verify"})
    action = {"action": "run", "phase": "agent_run", "detail": {"plan": "p"}}
    out = run(D._perform(f, action, NOW))
    assert out["action"] == "wait"
    assert out["note"] == "waiting for memory to start Verify: 2.1 GB free"
    assert out["detail"]["run_wait"] == "memory"
    assert out["detail"]["memory_since"] == NOW


def test_a_verify_memory_wait_says_how_long(srv, monkeypatch):
    _memory_refusal(monkeypatch)
    f = fly({"kind": "verify"})
    F.mutate(
        f["id"],
        lambda x: x["step_states"]["s1"]["detail"].update(memory_since=NOW - 130),
    )
    f = F.get(f["id"])
    out = run(D._perform(f, {"action": "run", "phase": "agent_run"}, NOW))
    assert out["note"].endswith(" · waited 2 min")


def test_a_verify_memory_wait_parks_on_you_after_five_minutes(srv, monkeypatch):
    _memory_refusal(monkeypatch)
    f = fly({"kind": "verify"})
    F.mutate(
        f["id"],
        lambda x: x["step_states"]["s1"]["detail"].update(memory_since=NOW - 301),
    )
    f = F.get(f["id"])
    out = run(D._perform(f, {"action": "run", "phase": "agent_run"}, NOW))
    assert out["action"] == "you"
    assert out["note"] == "couldn't start Verify: 2.1 GB free (waited 5 min)"
    assert out["detail"]["ask"] == "memory"
    assert "Retry" in out["detail"]["items"][0] and "Skip" in out["detail"]["items"][0]


def _review(srv, kid="review-1"):
    f = fly({"kind": "review"})
    return F.mutate(
        f["id"],
        lambda x: F.apply(x, {"action": "run", "detail": {"children": [kid]}}, NOW),
    )


def test_a_reviewer_waiting_for_memory_says_so(srv, monkeypatch):
    from backend.web.core import launch_gate as LG

    monkeypatch.setattr(
        LG,
        "waiting",
        lambda t: {
            "title": t,
            "since": NOW - 90,
            "reason": "memory is low",
            "parent": "s",
        },
    )
    f = _review(srv)
    ss = f["step_states"]["s1"]
    launch = D._launch_obs(ss)
    assert launch["waiting"] and launch["why"] == "memory is low"
    # The review judge reads a not-yet-launched reviewer as gone → "did not
    # finish": the driver says what is really happening instead.
    gone = {"action": "fail", "reason": "the reviewer did not finish"}
    out = D._launch_verdict(f, {"launch": launch}, gone, NOW)
    assert out == {
        "action": "wait",
        "note": "waiting for memory to start the reviewer: memory is low · waited 1 min",
        "detail": {},
    }
    late = D._launch_verdict(f, {"launch": launch}, gone, NOW + 300)
    assert late["action"] == "you" and late["detail"]["ask"] == "memory"
    assert late["note"].startswith("couldn't start the reviewer: memory is low")


def test_a_reviewer_that_never_launched_is_named(srv, monkeypatch):
    from backend.web.core import launch_gate as LG

    monkeypatch.setattr(LG, "waiting", lambda t: None)
    monkeypatch.setattr(
        LG,
        "last_outcome",
        lambda t: {"title": t, "started": False, "error": "no such repo"},
    )
    f = _review(srv)
    launch = D._launch_obs(f["step_states"]["s1"])
    out = D._launch_verdict(
        f, {"launch": launch, "session": {"present": True}}, {"action": "wait"}, NOW
    )
    assert out["action"] == "fail"
    assert out["reason"] == "couldn't start the reviewer: no such repo"


def test_a_started_reviewer_is_left_to_its_judge(srv, monkeypatch):
    from backend.web.core import launch_gate as LG

    monkeypatch.setattr(LG, "waiting", lambda t: None)
    srv.ENGINE.instances["review-1"] = FakeInst()
    f = _review(srv)
    assert D._launch_obs(f["step_states"]["s1"]) is None


def test_retry_on_a_reviewer_memory_park_drops_the_queued_reviewer(srv, monkeypatch):
    from backend.web.core import launch_gate as LG

    queue = {"review-1": {"title": "review-1", "parent": "s", "since": NOW}}
    monkeypatch.setattr(LG, "waiting", lambda t: queue.get(t))
    monkeypatch.setattr(LG, "cancel", lambda t: queue.pop(t, None) is not None)
    f = _review(srv)
    f = F.mutate(
        f["id"],
        lambda x: F.apply(
            x,
            {"action": "you", "note": "couldn't start", "detail": {"ask": "memory"}},
            NOW,
        ),
    )
    assert D._needs_you_perch(f, "s1") is True
    after = D.operate(f["id"], "retry", "s1")
    assert after["step_states"]["s1"]["state"] == "pending"
    assert queue == {}


# --------------------------------------------------------------------------- #
# 9. Notify says where it went
# --------------------------------------------------------------------------- #
def _ntfy(monkeypatch, active, result=(True, "")):
    from backend.web.core import ntfy as N

    monkeypatch.setattr(N, "load", lambda: SimpleNamespace(active=active, topic="t"))

    async def publish(cfg, **kw):
        return result

    monkeypatch.setattr(N, "publish", publish)


def test_notify_with_the_phone_off_is_shown_in_the_bell(srv, row, emitted, monkeypatch):
    _ntfy(monkeypatch, False)
    f = fly({"kind": "notify", "config": {"text": "deployed"}})
    run(D.step_flight(f["id"]))
    got = F.get(f["id"])
    receipt = got["step_states"]["s1"]["detail"]["receipt"]
    assert receipt == "shown in the bell — phone notifications are off"
    bell = [e for e in emitted if e["event"] == "flight.notified"]
    assert len(bell) == 1
    assert bell[0]["data"]["text"] == "deployed" and bell[0]["data"]["phone"] is False


def test_notify_with_the_phone_on_says_sent_to_your_phone(
    srv, row, emitted, monkeypatch
):
    _ntfy(monkeypatch, True)
    f = fly({"kind": "notify", "config": {"text": "x"}})
    run(D.step_flight(f["id"]))
    ss = F.get(f["id"])["step_states"]["s1"]
    assert ss["detail"]["receipt"] == "sent to your phone"
    assert [e["data"]["phone"] for e in emitted if e["event"] == "flight.notified"] == [
        True
    ]


def test_a_failed_push_is_never_called_sent(srv, row, monkeypatch):
    _ntfy(monkeypatch, True, (False, "ntfy server returned 403: topic reserved"))
    f = fly({"kind": "notify", "config": {"text": "x"}})
    run(D.step_flight(f["id"]))
    ss = F.get(f["id"])["step_states"]["s1"]
    assert (
        ss["detail"]["receipt"] == "not sent: ntfy server returned 403: topic reserved"
    )
    assert ss["note"].startswith("not sent to your phone: ")


# --------------------------------------------------------------------------- #
# 11. Routine outcomes log at INFO, handled failures at WARNING
# --------------------------------------------------------------------------- #
def test_log_levels(monkeypatch):
    from backend import log

    lines = {"info": [], "warning": [], "error": []}

    def logger(level):
        return SimpleNamespace(Printf=lambda fmt, *a: lines[level].append(fmt % a))

    monkeypatch.setattr(log, "InfoLog", logger("info"))
    monkeypatch.setattr(log, "WarningLog", logger("warning"))
    monkeypatch.setattr(log, "ErrorLog", logger("error"))
    D._log("posted %s", "x", level="info")
    D._warn("unreadable %s", "y")
    D._log("broke %s", "z")
    assert lines == {
        "info": ["posted x"],
        "warning": ["unreadable y"],
        "error": ["broke z"],
    }
