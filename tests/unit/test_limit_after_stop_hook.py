"""A turn cut short by the account's usage limit must not read as a finished one.

The CLI fires the same Stop hook either way, so the activity marker says "idle"
whether the agent completed the work or ran out of weekly quota mid-thought.
Trusting that verbatim is what let fast-track commit and push a half-finished
session the moment a weekly limit landed on it: the badge said idle, the
auto-resume watcher (which selects on ``activity == "limit"``) never saw it, and
autopilot's usage-limit gate — written for exactly this case — was never armed.

Covers the three halves of the fix: the pane re-check behind an idle marker, the
driver's own confirmation against the usage meter, and the deadline clock that
must not run while a limit holds (a weekly window can be closed for days).
"""

from __future__ import annotations

import types

import pytest

from backend.web import server
from backend.web.core import agent_state
from backend.web.core import autopilot as ap

_BANNER = (
    "> summarise the diff\n"
    "\n"
    "Weekly limit reached ∙ resets Aug 27 at 10:59am (America/New_York)\n"
    "\n"
    "> \n"
)
_CLEAN = "> summarise the diff\n\nDone — the diff touches three files.\n\n> \n"


# --------------------------------------------------------------------------- #
# 1. the pane re-check behind an idle marker
# --------------------------------------------------------------------------- #
class _StopHookProvider:
    """A CLI that reports idle through its own hook marker (Claude Code)."""

    def reports_activity(self):
        return True

    def activity_state(self, name):
        return "idle"

    def activity_state_age(self, name):
        return 0.0

    def record_thread(self, *a, **k):
        return None

    def waiting_prompt_patterns(self):
        return []

    def working_pane_patterns(self):
        return ()

    def progress_token_pattern(self):
        return None


@pytest.fixture()
def harness(monkeypatch):
    pane = {"text": _CLEAN}
    clock = {"t": 1000.0}
    monkeypatch.setattr(server.time, "time", lambda: clock["t"])
    monkeypatch.setattr(server.tmux, "to_mindflock_tmux_name", lambda t: t)
    monkeypatch.setattr(server, "_agent_exited", lambda name, created: False)
    monkeypatch.setattr(server, "_pane_has_agent_process", lambda pid: True)
    monkeypatch.setattr(server.providers, "resolve", lambda prog: _StopHookProvider())
    monkeypatch.setattr(
        server, "_pane_meta", lambda name: ("claude", 1.0, "123", "80x24")
    )
    monkeypatch.setattr(server, "_pane_cpu_jiffies", lambda pid: 0)
    monkeypatch.setattr(server, "_dismiss_trust_prompt", lambda *a, **k: False)

    def fake_run(argv, **kw):
        joined = " ".join(argv)
        if "has-session" in joined:
            return types.SimpleNamespace(returncode=0, stdout=b"")
        if "capture-pane" in joined:
            return types.SimpleNamespace(returncode=0, stdout=pane["text"].encode())
        return types.SimpleNamespace(returncode=0, stdout=b"")

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    agent_state._LIMIT_PROBE.pop("t", None)
    agent_state._ACTIVITY_CACHE.pop("t", None)
    monkeypatch.setattr(server.session, "Paused", "paused", raising=False)
    inst = types.SimpleNamespace(
        Title="t", Program="claude", Status="running", Started=lambda: True
    )
    return inst, pane, clock


def test_stop_hook_idle_on_a_limit_screen_reads_limit(harness):
    inst, pane, _ = harness
    pane["text"] = _BANNER
    assert server._agent_activity(inst, "t") == "limit"


def test_stop_hook_idle_on_an_ordinary_pane_still_reads_idle(harness):
    inst, _, _ = harness
    assert server._agent_activity(inst, "t") == "idle"


def test_the_pane_re_check_is_throttled(harness):
    """The marker path exists to skip the capture, so the re-check is throttled
    per session rather than run on every poll — a limit that lands right after a
    clean probe is seen one window later, well inside autopilot's 30s dwell."""
    inst, pane, clock = harness
    assert server._agent_activity(inst, "t") == "idle"
    pane["text"] = _BANNER
    clock["t"] += agent_state._LIMIT_RECHECK_S - 1
    assert server._agent_activity(inst, "t") == "idle"  # cached verdict
    clock["t"] += 2
    assert server._agent_activity(inst, "t") == "limit"


# --------------------------------------------------------------------------- #
# 2. the driver confirms an idle verdict against the usage meter
# --------------------------------------------------------------------------- #
@pytest.fixture()
def snapshot_harness(monkeypatch, tmp_path):
    """`_autopilot_snapshot` with every probe stubbed but the limit gate."""
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "ap.json"))
    limited_until = {"v": 0.0}
    monkeypatch.setattr(server.tmux, "to_mindflock_tmux_name", lambda t: t)
    monkeypatch.setattr(
        server, "_refresh_limit_state", lambda inst, title, name: limited_until["v"]
    )
    monkeypatch.setattr(server._prompt_queue, "get_state", lambda t: {"items": []})
    monkeypatch.setattr(server, "_is_dirty", lambda wt: True)
    monkeypatch.setattr(server, "_has_origin", lambda wt: True)
    monkeypatch.setattr(server, "_session_base_branch", lambda inst: "main")
    monkeypatch.setattr(server, "_commits_beyond_base", lambda wt, base: 0)
    monkeypatch.setattr(server, "_current_branch", lambda wt: "feature/x")
    monkeypatch.setattr(server._wt_setup, "check_summary", lambda wt: None)
    inst = types.SimpleNamespace(Title="t", Program="claude")
    return inst, limited_until


def test_an_idle_agent_whose_meter_is_spent_is_limited_not_done(
    monkeypatch, snapshot_harness
):
    """The pane may carry no banner at all — a session that ran out mid-turn is
    often back at a freshly drawn prompt — so the meter is the deciding vote."""
    inst, limited_until = snapshot_harness
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "idle")
    limited_until["v"] = server.time.time() + 3600
    snap = server._autopilot_snapshot(inst, "t", "/wt", {"stage": "agent"})
    assert snap["activity"] == "idle"
    assert snap["limited"] is True
    action, detail = ap.next_action(
        ap._normalize({"depth": "pr", "idle_since": 1.0}), snap
    )
    assert action == "wait" and "usage limit" in detail["reason"]


def test_an_idle_agent_with_an_open_window_is_not_held(monkeypatch, snapshot_harness):
    inst, limited_until = snapshot_harness
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "idle")
    limited_until["v"] = 0.0
    snap = server._autopilot_snapshot(inst, "t", "/wt", {"stage": "agent"})
    assert snap["limited"] is False


def test_a_working_agent_never_pays_for_the_limit_probe(monkeypatch, snapshot_harness):
    inst, _ = snapshot_harness
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "working")

    def _boom(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("the limit probe must only run on an idle verdict")

    monkeypatch.setattr(server, "_refresh_limit_state", _boom)
    snap = server._autopilot_snapshot(inst, "t", "/wt", {"stage": "agent"})
    assert snap["limited"] is False


# --------------------------------------------------------------------------- #
# 3. the deadline clock must not run while a limit holds
# --------------------------------------------------------------------------- #
@pytest.fixture()
def wait_harness(monkeypatch, tmp_path):
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "ap.json"))
    monkeypatch.setattr(server, "_emit_autopilot", lambda title: None)
    monkeypatch.setattr(server, "_emit_autopilot_event", lambda title: None)
    return None


def test_a_weekly_limit_outlasts_the_step_deadline_without_halting(wait_harness):
    t0 = 100_000.0
    ap.arm("t", "pr", now=t0)
    ap.update("t", step="agent", step_since=t0 - 3000, idle_since=t0 - 3000)
    limited = {"activity": "idle", "limited": True}

    server._autopilot_wait("t", ap.get("t"), limited, {"reason": "usage limit"}, t0)
    rec = ap.get("t")
    assert rec["limited_at"] == t0
    # The dwell is dropped: a limited session LOOKS idle, and a settle earned
    # during the outage would commit the half-finished work the instant the
    # window reopened, before the resume nudge had landed.
    assert rec["idle_since"] is None

    # Three hours later, still limited — twice the 90-minute agent deadline.
    t1 = t0 + 10_800
    server._autopilot_wait("t", ap.get("t"), limited, {"reason": "usage limit"}, t1)
    assert ap.get("t")["state"] == "running", "a limit hold is not 'no progress'"

    # The window reopens: the whole stretch is credited back to the step clock.
    server._autopilot_wait(
        "t", ap.get("t"), {"activity": "idle", "limited": False}, {"reason": ""}, t1
    )
    rec = ap.get("t")
    assert rec["state"] == "running"
    assert rec["limited_at"] == 0.0
    assert rec["step_since"] == pytest.approx(t0 - 3000 + 10_800)


def test_an_ordinary_stall_still_halts_on_the_deadline(wait_harness):
    """The freeze is scoped to a limit hold — a wedged step must still give up."""
    t0 = 100_000.0
    ap.arm("t", "pr", now=t0)
    ap.update("t", step="agent", step_since=t0 - 10_800, idle_since=None)
    server._autopilot_wait(
        "t",
        ap.get("t"),
        {"activity": "idle", "limited": False},
        {"reason": "confirming the agent is done"},
        t0,
    )
    rec = ap.get("t")
    assert rec["state"] == "halted"
    assert "gave up waiting" in rec["reason"]
