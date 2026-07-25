"""tmux session-naming + cleanup.

Sessions use the ``mindflock_`` prefix; cleanup kills exactly those.
"""

from __future__ import annotations

from backend import cmd as cmd_pkg
from backend.session.tmux import tmux


def test_to_tmux_name_uses_mindflock_prefix():
    assert tmux.to_mindflock_tmux_name("foo") == "mindflock_foo"
    # whitespace collapsed, dots -> underscores, prefix prepended
    assert tmux.to_mindflock_tmux_name("a sd f . . asdf") == "mindflock_asdf__asdf"
    assert tmux.TmuxPrefix == "mindflock_"


def test_cleanup_kills_only_mindflock_prefixed_sessions():
    listing = (
        "mindflock_sc-20005: 1 windows\n"
        "mindflock_MindFlock: 1 windows\n"
        "my-unrelated-session: 2 windows\n"
        "work: 1 windows\n"
    ).encode("utf-8")

    killed = []

    def output_func(c):
        return listing, None

    def run_func(c):
        killed.append(c.args[-1])  # kill-session -t <name>
        return None

    mock = cmd_pkg.MockCmdExec(run_func=run_func, output_func=output_func)
    err = tmux.cleanup_sessions(mock)
    assert err is None
    assert sorted(killed) == ["mindflock_MindFlock", "mindflock_sc-20005"]
