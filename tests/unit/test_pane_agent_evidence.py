"""Positive evidence that an AGENT holds a pane (``agent_state._pane_runs_agent``).

The mailbox lane and ``delivery: "now"`` type a peer's message, with Enter,
into a session's pane. After a deliberate quit a provisioned launcher runs
``exec bash -i`` and the user may start anything there; "any non-shell
foreground is the agent" typed messages into vim, ``ssh prod`` and REPLs.
These pin the matcher against a scripted ``ps`` snapshot.
"""

from __future__ import annotations

import subprocess

import pytest

from backend.web import server
from backend.web.core import agent_state


class _Proc:
    def __init__(self, out: str, rc: int = 0):
        self.stdout = out.encode()
        self.returncode = rc


def _ps(monkeypatch, rows, rc=0):
    out = "\n".join("%s %s %s" % r for r in rows)
    calls = []

    def run(args, **kw):
        calls.append(list(args))
        return _Proc(out, rc)

    monkeypatch.setattr(server, "_run_capped", run)
    return calls


NAMES = agent_state.agent_process_names("claude")


def test_names_cover_every_provider_and_never_a_shell_or_runner():
    assert {"claude", "codex", "aider"} <= NAMES
    assert not NAMES & {"bash", "sh", "zsh", "node", "python3", "env"}
    custom = agent_state.agent_process_names("/home/u/bin/ccc --kiro")
    assert "ccc" in custom and "claude" in custom
    assert "bash" not in agent_state.agent_process_names("bash")


def test_plain_session_agent_under_the_exit_wrapper(monkeypatch):
    calls = _ps(
        monkeypatch,
        [
            ("100", "1", "sh -c rm -f /m/exit; claude --model x; echo $? > /m/exit"),
            ("101", "100", "claude --model x"),
            ("999", "1", "claude elsewhere"),
        ],
    )
    assert agent_state._pane_runs_agent("100", NAMES) is True
    assert calls[0][:3] == ["ps", "-e", "-o"] and "args=" in calls[0][3]


@pytest.mark.parametrize(
    "child",
    [
        "vim notes.md",
        "ssh prod",
        "ssh claude",  # an argument is not the executable
        "python3",
        "psql app",
        "docker exec -it app sh",
        "sudo -s",
    ],
)
def test_after_a_quit_nothing_else_reads_as_the_agent(monkeypatch, child):
    # The launcher exec'd ``bash -i`` (same pid), the user started ``child``;
    # an unrelated claude elsewhere on the machine must not count.
    _ps(
        monkeypatch,
        [
            ("200", "1", "bash -i"),
            ("201", "200", child),
            ("300", "1", "claude --continue"),
        ],
    )
    assert agent_state._pane_runs_agent("200", NAMES) is False


@pytest.mark.parametrize(
    "agent",
    [
        "node /home/u/.npm-global/bin/claude --continue",
        "node /usr/lib/node_modules/@anthropic-ai/claude-code/cli.js",
        "/usr/bin/env node /usr/local/bin/codex",
        "/home/u/.local/share/uv/tools/aider-chat/bin/python /home/u/.local/bin/aider",
        "python3.12 -u /opt/aider/aider.py",
    ],
)
def test_script_run_agents_are_recognised(monkeypatch, agent):
    _ps(
        monkeypatch,
        [("400", "1", "bash /ws/.mindflock/launch.sh"), ("401", "400", agent)],
    )
    assert agent_state._pane_runs_agent("400", NAMES) is True


def test_unreadable_process_table_is_unknown(monkeypatch):
    _ps(monkeypatch, [], rc=1)
    assert agent_state._pane_runs_agent("1", NAMES) is None

    def boom(args, **kw):
        raise OSError("no ps")

    monkeypatch.setattr(server, "_run_capped", boom)
    assert agent_state._pane_runs_agent("1", NAMES) is None
    assert agent_state._pane_runs_agent(None, NAMES) is None


def test_pane_holds_agent_needs_the_agent_when_the_program_is_known(monkeypatch):
    monkeypatch.setattr(server, "_pane_meta", lambda n: ("vim", 1.0, "200", "80x24"))
    _ps(monkeypatch, [("200", "1", "bash -i"), ("201", "200", "vim x")])
    assert server._pane_holds_agent("s", "claude") is False
    # The old, looser reading (no program) still treats vim as "not a shell".
    assert server._pane_holds_agent("s") is True
    _ps(monkeypatch, [("200", "1", "bash -i"), ("201", "200", "claude")])
    assert server._pane_holds_agent("s", "claude") is True
    # ps unreadable: don't type blind.
    _ps(monkeypatch, [], rc=1)
    assert server._pane_holds_agent("s", "claude") is False


def test_real_ps_sees_this_process_tree():
    """Against the real ``ps``: a child running a script named like an agent
    is found under its parent; a plain ``sleep`` is not."""
    import os
    import sys
    import tempfile

    if sys.platform.startswith("win"):
        pytest.skip("posix ps only")
    d = tempfile.mkdtemp()
    script = os.path.join(d, "aider")
    with open(script, "w") as fh:
        fh.write("import time\ntime.sleep(30)\n")
    agent = subprocess.Popen([sys.executable, script])
    other = subprocess.Popen(["sleep", "30"])
    try:
        import time

        time.sleep(0.3)
        names = agent_state.agent_process_names("aider")
        assert agent_state._pane_runs_agent(str(agent.pid), names) is True
        assert agent_state._pane_runs_agent(str(other.pid), names) is False
    finally:
        for p in (agent, other):
            p.kill()
            p.wait()
