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
