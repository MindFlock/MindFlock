"""The MCP features' per-title state is torn down through the one removal hook.

``server._on_session_removed(title)`` is the single door every removal path
goes through (DELETE, /close, /cleanup, failed starts, engine convergence).
The mailbox and the auto-attach run file each keep per-title state elsewhere;
these tests pin that both are registered on the hook, so a reused title never
inherits the old inbox or a stale ``--mcp-config`` file.
"""

from __future__ import annotations

import os

from backend.providers import mcp_attach
from backend.session import tmux
from backend.web import server
from backend.web.core import mailbox


def test_removal_hook_drops_the_inbox():
    mailbox.post("gone-agent", "hello", sender="")
    assert mailbox.unread_count("gone-agent") == 1

    server._on_session_removed("gone-agent")

    assert mailbox.unread_count("gone-agent") == 0
    assert mailbox.fetch("gone-agent", unread_only=False)["messages"] == []


def test_removal_hook_deletes_the_mcp_run_file():
    name = tmux.to_mindflock_tmux_name("gone agent.v2")
    path = mcp_attach.write_claude_config(mcp_attach.build_spec("gone agent.v2", name))
    assert os.path.exists(path)

    server._on_session_removed("gone agent.v2")

    assert not os.path.exists(path)


def test_removal_hook_tolerates_a_title_with_no_state():
    # Nothing stored for this title anywhere: every hook is a quiet no-op.
    server._on_session_removed("never-existed")


def test_both_hooks_are_registered():
    assert mailbox.drop in server._SESSION_REMOVED_HOOKS
    assert server._forget_mcp_run_file in server._SESSION_REMOVED_HOOKS


def test_removal_hook_drops_the_budget_raise(monkeypatch, tmp_path):
    """A raised budget goes with its session: titles come straight back
    (``untitled-2``), and a namesake must start at the default cap — not the
    dead session's raise. The prompt-queue drain's sweep was the only cleanup,
    and it returns early whenever no session has a queued prompt."""
    from backend.web.core import budget as budget_core

    monkeypatch.setattr(
        server, "_budget_overrides_path", lambda: str(tmp_path / "bo.json")
    )
    monkeypatch.setattr(budget_core, "_BUDGET_OV", None)
    server._set_budget_override("gone-agent", 99.0, None)
    budget_core._BUDGET_FIRED["gone-agent"] = 1
    assert "gone-agent" in server._budget_overrides()

    server._on_session_removed("gone-agent")

    assert "gone-agent" not in server._budget_overrides()
    assert "gone-agent" not in budget_core._BUDGET_FIRED


def test_ending_a_session_cancels_its_queued_launch():
    """A relaunch, wake or revive waiting in the launch gate for a session that
    is then deleted or closed must leave the queue with it: a stale "waiting"
    entry otherwise sat there until it drained and failed. end_session runs
    ``_forget_session_state`` on both doors (close keeps the worktree, delete
    wipes it), so that is where it goes."""
    from backend.web.core import launch_gate

    launch_gate._reset_for_tests()
    try:
        for title in ("deleted-one", "closed-one"):
            launch_gate.enqueue(title, lambda: None, kind="relaunch", why="memory")
            assert launch_gate.waiting(title) is not None
            server._forget_session_state(title)
            assert launch_gate.waiting(title) is None
        assert launch_gate.items() == []
    finally:
        launch_gate._reset_for_tests()
