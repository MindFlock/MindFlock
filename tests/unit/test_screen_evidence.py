"""Screen evidence outranks the activity reading (live MCP E2E, 2026-10-05).

A Claude Code session that runs background sub-agents reported its state
through hook markers those sub-agents keep rewriting: a sub-agent's
permission prompt sat on screen while the session read ``working`` (its
siblings' tool events) or ``idle`` (the main turn's Stop hook) — defect A —
and a worker's prompt read ``idle`` for 4-6 s at a time once its clarify
marker aged past the trust window (defect D). The screens below are the run's
real captures (``tests/unit/data/dialogs/claude2_*``).
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from backend.providers.claude import ClaudeProvider
from backend.web import server
from backend.web.core import agent_state

DATA = Path(__file__).parent / "data" / "dialogs"


def _screen(name: str) -> str:
    return (DATA / ("%s.screen.txt" % name)).read_text(encoding="utf-8")


class _Inst:
    Status = 0
    Program = "claude"

    def Started(self):  # noqa: N802
        return True

    def GetWorktreePath(self):  # noqa: N802
        return ""


def _cap(text: str):
    return types.SimpleNamespace(returncode=0, stdout=text.encode("utf-8"))


class _Pane:
    """``subprocess.run`` for the activity probe: a live tmux session (fg
    claude, no pane pid — the hash fallback) showing ``screen``."""

    def __init__(self, screen):
        self.screen = screen
        self.captures = 0

    def __call__(self, cmd, **kwargs):
        if cmd[:2] == ["tmux", "has-session"]:
            return types.SimpleNamespace(returncode=0, stdout=b"")
        if cmd[:2] == ["tmux", "display-message"]:
            return _cap("claude\t100.0\t")
        if cmd[:2] == ["tmux", "capture-pane"]:
            self.captures += 1
            s = self.screen() if callable(self.screen) else self.screen
            return _cap(s)
        return types.SimpleNamespace(returncode=1, stdout=b"")


@pytest.fixture
def probe(monkeypatch, tmp_path):
    """``probe(screen, marker, marker_at)`` → a callable returning the
    activity at a given clock time, the marker frozen at ``marker`` written
    at ``marker_at``."""
    server._ACTIVITY_CACHE.clear()
    server._PID_TREE_CACHE.clear()
    agent_state._LIMIT_PROBE.clear()
    monkeypatch.setenv("MINDFLOCK_ACTIVITY_MARKER_DIR", str(tmp_path / "markers"))
    monkeypatch.setattr(server, "_read_exit_marker", lambda name: None)
    clock = {"t": 10_000.0}
    monkeypatch.setattr(server.time, "time", lambda: clock["t"])
    state = types.SimpleNamespace(marker=None, marker_at=0.0, pane=None)
    monkeypatch.setattr(
        ClaudeProvider, "activity_state", lambda self, name: state.marker
    )
    monkeypatch.setattr(
        ClaudeProvider,
        "activity_state_age",
        lambda self, name: (clock["t"] - state.marker_at) if state.marker else None,
    )

    def setup(screen, marker=None, marker_at=None):
        state.marker = marker
        state.marker_at = clock["t"] if marker_at is None else marker_at
        state.pane = _Pane(screen)
        monkeypatch.setattr(server.subprocess, "run", state.pane)

        def at(t):
            clock["t"] = t
            return server._agent_activity(_Inst(), "sess")

        at.pane = state.pane
        at.clock = clock
        return at

    yield setup
    server._ACTIVITY_CACHE.clear()


# --------------------------------------------------------------------------- #
# Defect A: a sub-agent's prompt reads clarify whatever the marker says        #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("marker", ["working", "idle", None])
@pytest.mark.parametrize(
    "name", ["claude2_tool_use_tab", "claude2_tool_use_wide", "claude2_bash_subagent"]
)
def test_a_dialog_on_screen_reads_clarify_over_the_marker(probe, marker, name):
    at = probe(_screen(name), marker=marker)
    assert at(10_000.0) == "clarify"
    rec = server._ACTIVITY_CACHE["sess"]
    assert rec["reading"] == ("clarify", "screen")
    # A prompt is not work: nothing is armed for a turn-end announcement.
    assert rec.get("worked_at") is None
    # One capture serves the whole probe.
    assert at.pane.captures == 1


def test_screen_clarify_is_not_authoritative_for_the_announce_path(probe):
    """A frame is a frame: a screen-read clarify still takes the emitter's
    settle (``reading_is_authoritative`` is False)."""
    at = probe(_screen("claude2_tool_use_tab"), marker="working")
    assert at(10_000.0) == "clarify"
    assert agent_state.reading_is_authoritative("sess", "clarify") is False


def test_no_dialog_on_screen_leaves_the_marker_in_charge(probe):
    at = probe(_screen("claude2_working"), marker="working")
    assert at(10_000.0) == "working"
    assert server._ACTIVITY_CACHE["sess"]["reading"] == ("working", "marker")


def test_the_orchestrators_answer_prompt_dialog_reads_clarify_after_its_stop(probe):
    """Defect F's precondition: the orchestrator's Stop hook (its main turn
    ended at 10:05:25) left an idle marker while a sub-agent's answer_prompt
    permission (raised 10:04:24) was still up — and idle is what let the
    mailbox lane type into it."""
    at = probe(_screen("claude2_answer_prompt_tab"), marker="idle", marker_at=9_000.0)
    for t in (10_000.0, 10_004.0, 10_008.0):
        assert at(t) == "clarify"


def test_a_limit_menu_is_not_claimed_as_a_dialog(probe, monkeypatch):
    """The usage-limit menu has numbered options too; 'limit' outranks
    'clarify' and stays the marker path's call."""
    monkeypatch.setattr(agent_state, "is_limit_screen", lambda text: True)
    at = probe(_screen("claude2_tool_use_tab"), marker="clarify")
    assert at(10_000.0) == "limit"


def test_a_provider_without_a_parser_takes_no_extra_capture(probe, monkeypatch):
    monkeypatch.setattr(ClaudeProvider, "parses_dialogs", lambda self: False)
    at = probe(_screen("claude2_tool_use_tab"), marker="working")
    assert at(10_000.0) == "working"
    assert at.pane.captures == 0


# --------------------------------------------------------------------------- #
# Defect D: no idle flicker while the dialog is up                             #
# --------------------------------------------------------------------------- #
def _blinking(screen: str):
    """``screen`` with Claude's pending-tool bullet blinking: every other
    capture shows it, the others a blank (what changed the pane hash)."""
    if "● " not in screen:  # the glitch capture starts at the dialog
        screen = "● Bash(cat > /tmp/list_sessions.py)\n\n" + screen
    frames = [screen, screen.replace("● ", "  ")]
    assert frames[0] != frames[1]
    n = {"i": 0}

    def next_frame():
        n["i"] += 1
        return frames[n["i"] % 2]

    return next_frame


@pytest.mark.parametrize(
    "name",
    [
        "claude2_bash",  # parsed: the worker's commit prompt
        "claude2_redraw_glitch",  # unparsed: "3. Nock/worktrees…" redraw glitch
    ],
)
def test_replay_a_parked_dialog_through_the_marker_trust_window(probe, name):
    """The activity timeline of the live run: hello-worker's clarify marker
    written at 10:04:19, the dialog untouched until 10:07:17, polled every
    4 s. Once the marker aged past the 45 s trust window the pane layer took
    over: idle on its first poll, then idle on every frame that caught the
    blinking bullet (10:05:09 idle, :13 clarify, :17 idle, :21 clarify …).
    Every poll must read clarify."""
    t0 = 10_000.0
    at = probe(_blinking(_screen(name)), marker="clarify", marker_at=t0)
    readings = [at(t0 + 4.0 * i) for i in range(45)]  # 3 minutes
    assert set(readings) == {"clarify"}, readings


def test_a_stale_clarify_marker_with_no_dialog_left_goes_to_the_pane(probe):
    """The corroboration needs the dialog: once it is gone, a stale clarify
    marker is re-verified against the pane exactly as before."""
    at = probe(_screen("claude2_working"), marker="clarify", marker_at=0.0)
    assert at(10_000.0) != "clarify"
