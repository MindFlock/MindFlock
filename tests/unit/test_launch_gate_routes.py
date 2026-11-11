"""Every agent-start path asks the launch gate (core.launch_gate).

A person's start (Start, Wake, Duplicate, Resume, Start all) gets a 409
``memory_confirm`` while memory is low and goes with ``start_anyway``.
Automatic starts (a queued prompt relaunching its agent, a mailbox wake)
wait in the gate's queue, which the gate's own routes start or cancel.
Nothing here touches tmux or launches an agent.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend import memory_pressure
from backend.session.storage import Paused, Status
from backend.web import server
from backend.web.core import launch_gate as gate
from backend.web.core import mailbox, prompt_queue, sleep

client = TestClient(server.app)
GiB = 1024**3


def _memory(monkeypatch, level, available=3 * GiB, total=20 * GiB):
    snap = {
        "level": level,
        "available": available,
        "total": total,
        "available_pct": round(available * 100.0 / total, 1),
        "psi": None,
        "at": 0.0,
    }
    monkeypatch.setattr(memory_pressure, "snapshot", lambda **kw: dict(snap))


class _Inst:
    def __init__(self, status=Status.Running):
        self.Status = status
        self.Program = "claude"
        self.Title = "x"
        self.PeerShare = ""
        self.InPlace = False

    def Started(self):
        return True

    def GetWorktreePath(self):
        return "/nonexistent/for/the/copy/route"


@pytest.fixture
def instances(monkeypatch):
    monkeypatch.setattr(server.ENGINE, "instances", {})
    launched = []

    def _ensure(i, title):
        launched.append(title)
        sleep.woke(title)
        return "mindflock_" + title, None

    monkeypatch.setattr(server, "_ensure_agent_session", _ensure)
    monkeypatch.setattr(server, "_republish_session", lambda title: None)
    yield SimpleNamespace(rows=server.ENGINE.instances, launched=launched)
    # A wake stamps the queue's boot grace; the next test starts clean.
    for d in (server._QUEUE_STATE, server._MAIL_STATE):
        for t in ("zz", "s1"):
            d.pop(t, None)


# --------------------------------------------------------------------------- #
# a person's Start
# --------------------------------------------------------------------------- #
def test_start_asks_first_while_memory_is_low(instances, monkeypatch):
    _memory(monkeypatch, "low")
    instances.rows["s1"] = _Inst()
    r = client.post("/api/instances/s1/revive")
    assert r.status_code == 409
    body = r.json()
    assert body["memory_confirm"] is True and body["level"] == "low"
    assert body["message"].startswith("Memory is low: 3.0 GB free.")
    assert instances.launched == []
    r = client.post("/api/instances/s1/revive", json={"start_anyway": True})
    assert r.status_code == 200 and r.json()["revived"] is True
    assert instances.launched == ["s1"]


def test_start_goes_straight_through_when_memory_is_fine(instances, monkeypatch):
    _memory(monkeypatch, "ok", available=12 * GiB)
    instances.rows["s1"] = _Inst()
    assert client.post("/api/instances/s1/revive").status_code == 200
    assert instances.launched == ["s1"]


def test_start_takes_a_waiting_session_out_of_the_queue(instances, monkeypatch):
    _memory(monkeypatch, "ok", available=12 * GiB)
    instances.rows["s1"] = _Inst()
    gate.enqueue("s1", lambda: None, kind="queued prompt")
    assert client.post("/api/instances/s1/revive").status_code == 200
    assert gate.waiting("s1") is None


def test_a_paused_session_is_still_resume_first(instances, monkeypatch):
    _memory(monkeypatch, "critical", available=1 * GiB)
    instances.rows["s1"] = _Inst(status=Paused)
    r = client.post("/api/instances/s1/revive")
    assert r.status_code == 409 and "paused" in r.json()["error"]


def test_wake_asks_first_only_for_a_sleeper(instances, monkeypatch):
    _memory(monkeypatch, "critical", available=1 * GiB)
    instances.rows["zz"] = _Inst()
    # Not asleep: a no-op, nothing to ask.
    assert client.post("/api/instances/zz/wake").json() == {
        "title": "zz",
        "woke": False,
    }
    sleep.mark("zz", sleep.REASON_IDLE)
    try:
        r = client.post("/api/instances/zz/wake")
        assert r.status_code == 409 and r.json()["level"] == "critical"
        r = client.post("/api/instances/zz/wake", json={"start_anyway": True})
        assert r.json() == {"title": "zz", "woke": True}
        assert instances.launched == ["zz"]
    finally:
        sleep.drop("zz")


def test_duplicate_and_resume_ask_first(instances, monkeypatch):
    _memory(monkeypatch, "low")
    instances.rows["s1"] = _Inst()
    r = client.post("/api/instances/s1/copy")
    assert r.status_code == 409 and r.json()["memory_confirm"] is True
    # Past the gate, the copy route goes on to its own checks.
    r = client.post("/api/instances/s1/copy", json={"start_anyway": True})
    assert r.status_code == 409 and r.json()["error"] == "source workspace not ready"
    r = client.post("/api/instances/s1/resume")
    assert r.status_code == 409 and r.json()["memory_confirm"] is True


@pytest.mark.parametrize("body", [None, {"titles": ["a"]}])
def test_start_all_asks_first(instances, monkeypatch, body):
    _memory(monkeypatch, "low")
    seen = []

    async def _sweep(**kw):
        seen.append(kw)
        return {"revived": [], "skipped": {}, "summary": "revived 0"}

    monkeypatch.setattr(server._revive, "sweep", _sweep)
    r = client.post("/api/server/revive-all", json=body)
    assert r.status_code == 409 and r.json()["memory_confirm"] is True
    assert seen == []
    r = client.post("/api/server/revive-all", json=dict(body or {}, start_anyway=True))
    assert r.status_code == 200 and len(seen) == 1


# --------------------------------------------------------------------------- #
# the gate's own routes
# --------------------------------------------------------------------------- #
def test_memory_reading_carries_the_gate(monkeypatch):
    _memory(monkeypatch, "low")
    gate.enqueue("w1", lambda: None, kind="spawn", reason="memory is low")
    g = client.get("/api/system/memory").json()["gate"]
    assert g["holding"] is True and g["queued"][0]["title"] == "w1"
    assert client.get("/api/launch-gate").json()["queued"][0]["kind"] == "spawn"


def test_start_anyway_and_cancel_routes(instances, monkeypatch):
    _memory(monkeypatch, "critical", available=1 * GiB)
    ran = []
    gate.enqueue("w1", lambda: ran.append("w1"), kind="spawn")
    gate.enqueue("w2", lambda: ran.append("w2"), kind="spawn")
    # A person's start like Restart: at critical memory it asks first...
    r = client.post("/api/launch-gate/w1/start")
    assert r.status_code == 409
    assert r.json()["memory_confirm"] is True and r.json()["level"] == "critical"
    assert ran == [] and gate.waiting("w1") is not None
    # ...and goes with the confirm's Start anyway.
    r = client.post("/api/launch-gate/w1/start", json={"start_anyway": True})
    assert r.json() == {"title": "w1", "started": True, "error": ""}
    assert ran == ["w1"]
    assert client.post("/api/launch-gate/w2/cancel").json() == {
        "title": "w2",
        "cancelled": True,
    }
    assert ran == ["w1"] and gate.items() == []
    assert client.post("/api/launch-gate/w2/start").status_code == 404
    assert client.post("/api/launch-gate/w2/cancel").status_code == 404


def test_start_anyway_goes_straight_through_when_memory_is_fine(instances, monkeypatch):
    _memory(monkeypatch, "ok", available=12 * GiB)
    ran = []
    gate.enqueue("w1", lambda: ran.append("w1"), kind="spawn")
    r = client.post("/api/launch-gate/w1/start")
    assert r.status_code == 200 and r.json()["started"] is True and ran == ["w1"]


# --------------------------------------------------------------------------- #
# queue changes are pushed (core.launch_gate_push)
# --------------------------------------------------------------------------- #
@pytest.fixture
def pushed(monkeypatch):
    from backend.web.core import events
    from backend.web.core import launch_gate_push as push

    push._reset_for_tests()
    emitted, republished, patched = [], [], {}
    rows = [{"title": "s1", "waiting_for_memory": None}]
    monkeypatch.setattr(events, "sessions_snapshot", lambda: [dict(r) for r in rows])
    monkeypatch.setattr(
        events, "patch_session_snapshot", lambda t, row: patched.__setitem__(t, row)
    )
    monkeypatch.setattr(
        events.BUS, "emit", lambda event, **kw: emitted.append((event, kw)) or kw
    )
    srv = SimpleNamespace(
        ENGINE=SimpleNamespace(instances={"s1": object()}),
        _republish_session=republished.append,
    )
    yield SimpleNamespace(
        srv=srv, emitted=emitted, republished=republished, patched=patched, push=push
    )
    push._reset_for_tests()


def test_a_queue_change_patches_the_row_and_says_so(pushed, monkeypatch):
    _memory(monkeypatch, "low")
    gate.enqueue("s1", lambda: None, kind="queued prompt", reason="memory is low")
    env = pushed.push.push(pushed.srv)
    # The waiting row is patched in place (no probes), and one event goes out.
    assert pushed.patched["s1"]["waiting_for_memory"]["title"] == "s1"
    assert pushed.republished == []
    assert [e for e, _ in pushed.emitted] == ["system.memory_changed"]
    assert env["old"] == env["new"] == "low"
    assert env["data"]["cause"] == "launch_gate"
    assert env["data"]["gate"]["queued"][0]["title"] == "s1"
    # Nothing new: no second event.
    assert pushed.push.push(pushed.srv) is None and len(pushed.emitted) == 1
    # It leaves the queue: the row is republished in full (status moves too).
    gate.cancel("s1")
    pushed.push.push(pushed.srv)
    assert pushed.republished == ["s1"] and len(pushed.emitted) == 2
    assert pushed.emitted[-1][1]["data"]["gate"]["queued"] == []


def test_the_nudge_is_thread_safe_and_coalesces_bursts(pushed, monkeypatch):
    import asyncio
    import threading

    monkeypatch.setattr(gate, "on_change", None)
    calls = []
    monkeypatch.setattr(pushed.push, "push", lambda srv: calls.append(srv))
    loop = asyncio.new_event_loop()
    try:
        pushed.push.install(pushed.srv, loop, delay=0.05)
        assert gate.on_change is not None

        def burst():
            for _ in range(5):
                gate.on_change()

        th = threading.Thread(target=burst)
        th.start()
        th.join()
        loop.run_until_complete(asyncio.sleep(0.3))
        assert calls == [pushed.srv]
        # A later change is pushed again.
        gate.on_change()
        loop.run_until_complete(asyncio.sleep(0.3))
        assert len(calls) == 2
    finally:
        loop.close()


def test_sleep_idle_route(monkeypatch):
    calls = []
    monkeypatch.setattr(
        server._sleep,
        "sleep_idle_now",
        lambda srv, limit: calls.append(limit) or {"slept": ["a"], "freed_mb": 400},
    )
    r = client.post("/api/server/sleep-idle", json={"limit": 3})
    assert r.json() == {"slept": ["a"], "freed_mb": 400} and calls == [3]


def test_rows_say_waiting_for_memory(monkeypatch):
    gate.enqueue("s1", lambda: None, kind="queued prompt", reason="memory is low")
    rec = gate.waiting("s1")
    assert rec["title"] == "s1" and rec["reason"] == "memory is low"


# --------------------------------------------------------------------------- #
# automatic starts wait in the queue
# --------------------------------------------------------------------------- #
@pytest.fixture
def asleep(instances, monkeypatch, tmp_path):
    monkeypatch.setenv("MINDFLOCK_PROMPT_QUEUE_FILE", str(tmp_path / "pq.json"))
    instances.rows["zz"] = _Inst()
    monkeypatch.setattr(server, "_send_to_agent", lambda *a, **k: True)
    sleep.mark("zz", sleep.REASON_IDLE)
    yield instances
    sleep.drop("zz")
    for d in (server._QUEUE_STATE, server._MAIL_STATE):
        d.pop("zz", None)


def test_a_queued_prompt_waits_for_memory_to_relaunch(asleep, monkeypatch):
    _memory(monkeypatch, "low")
    monkeypatch.setattr(server, "_agent_activity", lambda inst, title: "offline")
    prompt_queue.enqueue("zz", "next")
    server._drain_one_queue("zz")
    assert asleep.launched == [] and sleep.is_sleeping("zz")
    assert gate.waiting("zz")["kind"] == "queued prompt"
    # Another pass doesn't queue it twice or launch it.
    server._QUEUE_STATE["zz"]["rebooted_at"] = 0.0
    server._drain_one_queue("zz")
    assert len(gate.items()) == 1 and asleep.launched == []
    # Memory back: the gate's queue relaunches it.
    _memory(monkeypatch, "ok", available=12 * GiB)
    assert gate.drain_once() == "zz"
    # (The wake, then the relaunch reusing it: the stub counts both.)
    assert set(asleep.launched) == {"zz"} and not sleep.is_sleeping("zz")
    assert server._QUEUE_STATE["zz"]["rebooted_at"] > 0


def test_a_mailbox_wake_waits_for_memory(asleep, monkeypatch, tmp_path):
    monkeypatch.setenv("MINDFLOCK_MAILBOX_FILE", str(tmp_path / "mail.json"))
    _memory(monkeypatch, "critical", available=1 * GiB)
    msg = mailbox.post("zz", "worker done", sender="w1")
    server._drain_one_mailbox("zz")
    assert asleep.launched == [] and gate.waiting("zz")["kind"] == "wake"
    result, detail = server._mail_deliver_now(server.ENGINE.instances["zz"], "zz", msg)
    assert (result, detail) == ("pending", server._MSG_WAITING_FOR_MEMORY)
    _memory(monkeypatch, "ok", available=12 * GiB)
    assert gate.drain_once() == "zz"
    assert asleep.launched == ["zz"]


def test_a_full_launch_queue_keeps_the_prompt_and_says_why(asleep, monkeypatch):
    from backend.web.core import events, queue_driver

    sent = []

    def _emit(event, **kw):
        if event == "session.queue_changed":
            sent.append((event, kw))

    monkeypatch.setattr(events.BUS, "emit", _emit)
    monkeypatch.setenv("MINDFLOCK_LAUNCH_QUEUE_MAX", "1")
    _memory(monkeypatch, "low")
    monkeypatch.setattr(server, "_agent_activity", lambda inst, title: "offline")
    gate.enqueue("other", lambda: None, kind="spawn")
    prompt_queue.enqueue("zz", "next")
    try:
        server._drain_one_queue("zz")
        server._drain_one_queue("zz")  # asked again; said once
        why = queue_driver.gate_full_reason("zz")
        assert "1 launches are already waiting" in why
        assert asleep.launched == [] and gate.waiting("zz") is None
        assert prompt_queue.get_state("zz")["items"]  # the prompt stays queued
        assert len(sent) == 1 and sent[0][1]["data"]["waiting"] == why
        assert server._queue_state_json("zz")["waiting"] == why
        assert server._queue_summary("zz", prompt_queue.snapshot())["waiting"] == why
        # Room again: the relaunch gets in and the reason clears.
        gate.cancel("other")
        _memory(monkeypatch, "ok", available=12 * GiB)
        server._drain_one_queue("zz")
        assert queue_driver.gate_full_reason("zz") == ""
        assert asleep.launched and sent[-1][1]["data"]["waiting"] == ""
        assert "waiting" not in server._queue_summary("zz", prompt_queue.snapshot())
    finally:
        queue_driver._GATE_FULL.pop("zz", None)


def test_a_mailbox_wake_with_the_launch_queue_full_says_so(
    asleep, monkeypatch, tmp_path
):
    monkeypatch.setenv("MINDFLOCK_MAILBOX_FILE", str(tmp_path / "mail.json"))
    monkeypatch.setenv("MINDFLOCK_LAUNCH_QUEUE_MAX", "1")
    _memory(monkeypatch, "critical", available=1 * GiB)
    gate.enqueue("other", lambda: None, kind="spawn")
    msg = mailbox.post("zz", "worker done", sender="w1")
    result, detail = server._mail_deliver_now(server.ENGINE.instances["zz"], "zz", msg)
    assert result == "pending" and "can't wake yet" in detail
    assert "already waiting for memory" in detail
    assert asleep.launched == []


def test_a_person_wake_is_never_queued(asleep, monkeypatch):
    _memory(monkeypatch, "critical", available=1 * GiB)
    woke, err = sleep.ensure_awake(
        server.ENGINE.instances["zz"], "zz", "terminal opened"
    )
    assert (woke, err) == (True, None) and asleep.launched == ["zz"]


def test_the_phone_shows_waiting_for_memory():
    from pathlib import Path

    js = (Path(server.__file__).parent / "static" / "mobile.js").read_text()
    at = js.index("inst.waiting_for_memory")
    assert '"Waiting for memory"' in js[at : at + 600]
    # Before the provisioning row: a queued worker IS a pending row.
    assert at < js.index('return st("starting", { hint: "Provisioning workspace" })')


def test_the_phone_asks_before_a_start_while_memory_is_low():
    """Restart, Wake, Resume and New session on the phone: a 409
    memory_confirm opens the page's own ask sheet with Start anyway, which
    sends the start again with start_anyway (confirm() is a silent no)."""
    from pathlib import Path

    js = (Path(server.__file__).parent / "static" / "mobile.js").read_text()
    at = js.index("function memoryConfirm(err, again)")
    body = js[at : at + 900]
    assert "b.memory_confirm !== true" in body and '"Start anyway"' in body
    call = js[js.index("function sessionCall(") :][:900]
    assert "memoryConfirm(err" in call and "{ start_anyway: true }" in call
    start = js[js.index("function startSession(startAnyway)") :][:6000]
    assert "startAnyway === true ? { start_anyway: true } : {}" in start
    assert "memoryConfirm(err, function () { startSession(true); })" in start
    assert not re.search(r"\bconfirm\(", body)


def test_a_client_without_the_dialog_reads_a_plain_sentence(monkeypatch):
    _memory(monkeypatch, "low")
    body = gate.person_refusal(gate.decide("person"))
    assert body["error"].startswith("Memory is low: 3.0 GB free.")
    assert "start_anyway: true" in body["error"]


def test_a_scheduled_fire_asks_the_gate_and_run_now_does_not(monkeypatch):
    import asyncio

    from backend.web.core import session_create, template_schedule as ts

    seen = []

    async def fake(payload, **kw):
        seen.append(kw.get("auto", ""))
        return 202, {"title": payload["title"]}

    monkeypatch.setattr(session_create, "create_result", fake)

    async def go(trigger):
        ts._TRIGGER.set(trigger)
        return await ts._default_create({"title": "t"})

    asyncio.run(go("schedule"))
    asyncio.run(go("manual"))
    assert seen == ["schedule", ""]


def test_a_spoken_new_session_says_why_it_did_not_start(monkeypatch):
    from backend.web.routes import voice

    _memory(monkeypatch, "low", available=int(3.1 * GiB))
    line = voice._memory_refusal_line()
    assert line.startswith("Memory is low, 3.1 GB free, so I didn't start it.")
    _memory(monkeypatch, "critical", available=1 * GiB)
    assert voice._memory_refusal_line().startswith("Memory is critically low")
    _memory(monkeypatch, "ok", available=12 * GiB)
    assert voice._memory_refusal_line() == ""


def test_a_team_run_holds_on_warming_and_the_cap_not_spacing(monkeypatch):
    from backend.web.core import team_run_driver as trd

    _memory(monkeypatch, "ok", available=12 * GiB)
    assert trd._gate_holds() is False
    monkeypatch.setenv("MINDFLOCK_MAX_LIVE_AGENTS", "1")
    gate.note_start("a1")
    assert trd._gate_holds() is True and trd._gate_room() == 0
    # The run's badge can say why: the cap, not "memory".
    assert trd._gate_why() == "cap"
    monkeypatch.delenv("MINDFLOCK_MAX_LIVE_AGENTS")
    gate._reset_for_tests()
    monkeypatch.setenv("MINDFLOCK_AUTO_LAUNCH_SPACING", "600")
    gate.note_start("a2", auto=True)  # spacing alone never holds a run
    assert trd._gate_holds() is False and trd._gate_why() == ""
