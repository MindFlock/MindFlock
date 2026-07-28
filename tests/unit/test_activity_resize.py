"""Robust activity detection: an idle agent (parked at its prompt, ~0 CPU) must
read "idle" no matter how its screen redraws, and opening/resizing its pane must
not flip it to "running". A working agent (burning CPU) still reads "working"."""

from __future__ import annotations

import types

import pytest

from backend.web import server


class _FakeProvider:
    def activity_state(self, name):
        return None

    def activity_state_age(self, name):
        return None

    def waiting_prompt_patterns(self):
        return []

    # Status-line signals (thinking fix): this stub reports none, so these tests
    # exercise the pure CPU/hash path with no status-line override.
    def working_pane_patterns(self):
        return ()

    def progress_token_pattern(self):
        return None


@pytest.fixture()
def harness(monkeypatch):
    # Controllable pane (text/size/cpu jiffies) and a fake clock so the 8s
    # idle-hysteresis is deterministic.
    pane = {"text": "l1\nl2\nl3\nl4\nl5\nl6\n", "size": "80x24", "cpu": 0}
    clock = {"t": 1000.0}

    monkeypatch.setattr(server.time, "time", lambda: clock["t"])
    monkeypatch.setattr(server.tmux, "to_mindflock_tmux_name", lambda t: t)
    monkeypatch.setattr(server, "_agent_exited", lambda name, created: False)
    monkeypatch.setattr(server, "_pane_has_agent_process", lambda pid: True)
    monkeypatch.setattr(server.providers, "resolve", lambda prog: _FakeProvider())
    monkeypatch.setattr(
        server, "_pane_meta", lambda name: ("claude", 1.0, "123", pane["size"])
    )
    monkeypatch.setattr(server, "_pane_cpu_jiffies", lambda pid: pane["cpu"])
    monkeypatch.setattr(server, "_dismiss_trust_prompt", lambda *a, **k: False)

    def fake_run(argv, **kw):
        joined = " ".join(argv)
        if "has-session" in joined:
            return types.SimpleNamespace(returncode=0, stdout=b"")
        if "capture-pane" in joined:
            return types.SimpleNamespace(returncode=0, stdout=pane["text"].encode())
        return types.SimpleNamespace(returncode=0, stdout=b"")

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    server._ACTIVITY_CACHE.pop("t", None)
    monkeypatch.setattr(server.session, "Paused", "paused", raising=False)
    inst = types.SimpleNamespace(
        Title="t", Program="claude", Status="running", Started=lambda: True
    )
    return inst, pane, clock


def _seed_idle(clock):
    server._ACTIVITY_CACHE["t"] = {
        "hash": server._normalized_pane_hash("l1\nl2\nl3\nl4\nl5\nl6\n"),
        "changed": clock["t"] - 100,
        "state": "idle",
        "streak": 0,
        "size": "80x24",
        "cpu": 0,
        "cpu_at": clock["t"],
        "busy_at": clock["t"] - 100,
    }


def test_idle_screen_churn_stays_idle(harness):
    """Pane text changes every poll (spinner/status line) but CPU is flat → idle."""
    inst, pane, clock = harness
    _seed_idle(clock)
    for i in range(3):
        clock["t"] += 4
        pane["text"] = (
            f"l1\nl2\nSPIN {i}\nl4\nl5\nl6\n"  # upper line churns, CPU still 0
        )
        assert server._agent_activity(inst, "t") == "idle"


def test_resize_does_not_flip_idle_to_working(harness):
    inst, pane, clock = harness
    _seed_idle(clock)
    clock["t"] += 4
    pane["size"] = "120x30"  # resize (tab opened)
    pane["text"] = "l1 l2\nl3 l4\nl5 l6\nx\ny\nz\n"  # reflowed, CPU still 0
    assert server._agent_activity(inst, "t") == "idle"


def test_cpu_activity_flips_to_working(harness):
    inst, pane, clock = harness
    _seed_idle(clock)
    clock["t"] += 4
    pane["cpu"] = 400  # 400 jiffies over 4s = 100/s ≫ 5
    assert server._agent_activity(inst, "t") == "working"


def test_working_settles_to_idle_after_cpu_quiets(harness):
    inst, pane, clock = harness
    _seed_idle(clock)
    # Go working via a CPU burst...
    clock["t"] += 4
    pane["cpu"] = 400
    assert server._agent_activity(inst, "t") == "working"
    # ...then CPU flatlines; stays working through the hysteresis window, then idle.
    clock["t"] += 4
    assert server._agent_activity(inst, "t") == "working"  # within 8s of busy_at
    clock["t"] += 8
    assert server._agent_activity(inst, "t") == "idle"  # past hysteresis


# --- Layer 4: status-line proof of a live turn at ~0 CPU (extended thinking) --
class _StatusLineProvider(_FakeProvider):
    """Reports a live interrupt hint and a turn-token counter, so the status-line
    proof can keep a CPU-quiet turn reading 'working' (server-side work)."""

    def working_pane_patterns(self):
        return (r"esc to interrupt",)

    def progress_token_pattern(self):
        return r"(\d[\d.,]*[kKmM]?) tokens"


def test_interrupt_hint_keeps_working_when_cpu_quiet(monkeypatch, harness):
    """A visible interrupt hint means the turn is still live even with flat CPU:
    the extended-thinking case (work runs server-side, local process blocks)."""
    inst, pane, clock = harness
    monkeypatch.setattr(server.providers, "resolve", lambda prog: _StatusLineProvider())
    _seed_idle(clock)
    clock["t"] += 4
    pane["cpu"] = 8  # 8 jiffies / 4s = 2/s, well below the active bar
    pane["text"] = "l1\nl2\nl3\nl4\n(thinking… esc to interrupt)\nl6\n"
    assert server._agent_activity(inst, "t") == "working"


def test_climbing_token_counter_keeps_working_when_cpu_quiet(monkeypatch, harness):
    """A climbing turn-token counter is the other Layer-4 signal: even at ~0 CPU,
    tokens increasing across polls proves the turn is still generating."""
    inst, pane, clock = harness
    monkeypatch.setattr(server.providers, "resolve", lambda prog: _StatusLineProvider())
    _seed_idle(clock)
    server._ACTIVITY_CACHE["t"]["tokens"] = 1000  # baseline from the prior poll
    clock["t"] += 4
    pane["cpu"] = 8  # CPU stays quiet
    pane["text"] = "l1\nl2\nl3\nl4\n1.2k tokens\nl6\n"  # 1200 > 1000 -> climbing
    assert server._agent_activity(inst, "t") == "working"


# --- trust-gate auto-dismiss on a freshly-started session (PR-ingestion fix) --
class _ClaudeTrustProvider(_FakeProvider):
    """Real Claude trust patterns so _dismiss_trust_prompt can match the gate."""

    def trust_prompt(self):
        from backend.providers.base import TrustSpec

        return TrustSpec(
            patterns=(
                "Do you trust the files in this folder?",
                "Is this a project you created or one you trust?",
                "new MCP server",
            ),
            keystroke=b"\r",
        )


_TRUST_GATE_PANE = (
    " Quick safety check: Is this a project you created or one you trust?\n"
    " > 1. Yes, I trust this folder\n"
    "   2. No, exit\n"
    " Enter to confirm - Esc to cancel\n"
)


def _young_gate_harness(monkeypatch, gate_text):
    """A just-created session whose pane shows `gate_text`; records send-keys."""
    clock = {"t": 1000.0}
    sent = []
    monkeypatch.setattr(server.time, "time", lambda: clock["t"])
    monkeypatch.setattr(server.tmux, "to_mindflock_tmux_name", lambda t: t)
    monkeypatch.setattr(server, "_agent_exited", lambda name, created: False)
    monkeypatch.setattr(server, "_pane_has_agent_process", lambda pid: True)
    monkeypatch.setattr(
        server.providers, "resolve", lambda prog: _ClaudeTrustProvider()
    )
    # created == now → inside the startup window.
    monkeypatch.setattr(
        server, "_pane_meta", lambda name: ("claude", clock["t"], "123", "80x24")
    )
    monkeypatch.setattr(server, "_pane_cpu_jiffies", lambda pid: 0)

    def fake_run(argv, **kw):
        joined = " ".join(argv)
        if "has-session" in joined:
            return types.SimpleNamespace(returncode=0, stdout=b"")
        if "send-keys" in joined:
            sent.append(argv)
            return types.SimpleNamespace(returncode=0, stdout=b"")
        if "capture-pane" in joined:
            return types.SimpleNamespace(returncode=0, stdout=gate_text.encode())
        return types.SimpleNamespace(returncode=0, stdout=b"")

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    server._ACTIVITY_CACHE.pop("t", None)
    server._TRUST_DISMISS_AT.pop("t", None)
    monkeypatch.setattr(server.session, "Paused", "paused", raising=False)
    inst = types.SimpleNamespace(
        Title="t", Program="claude", Status="running", Started=lambda: True
    )
    return inst, sent, clock


def test_startup_trust_gate_is_auto_dismissed(monkeypatch):
    """A young session parked at the trust gate is answered by the poll (so a
    seeded PR/ticket prompt runs without a human) — reported as clarify."""
    inst, sent, clock = _young_gate_harness(monkeypatch, _TRUST_GATE_PANE)
    assert server._agent_activity(inst, "t") == "clarify"
    assert any(
        "send-keys" in " ".join(a) for a in sent
    ), "no keystroke sent to the gate"


def test_startup_non_gate_pane_not_dismissed(monkeypatch):
    """A young session NOT at a trust gate gets no keystroke (no false dismiss)."""
    inst, sent, clock = _young_gate_harness(monkeypatch, "l1\nl2\nl3\nl4\nl5\nl6\n")
    server._agent_activity(inst, "t")
    assert not any("send-keys" in " ".join(a) for a in sent)


def test_permission_prompt_is_not_trust_dismissed(monkeypatch):
    """A real permission box (not the trust gate) is never auto-answered by the
    trust safety net, even during the startup window."""
    perm = (
        " Do you want to proceed?\n > 1. Yes\n"
        "   2. Yes, and don't ask again\n   3. No, and tell Claude what to do differently\n"
    )
    inst, sent, clock = _young_gate_harness(monkeypatch, perm)
    server._agent_activity(inst, "t")
    assert not any("send-keys" in " ".join(a) for a in sent)
