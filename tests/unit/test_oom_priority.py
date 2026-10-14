"""Agents volunteer for the OOM killer ahead of the server, and a SIGKILL is
never read as "the resume failed".

Measured 2026-10-14: under a global OOM the kernel killed the 2.1 GB MindFlock
server (the biggest single process, ``oom_score_adj`` 0) instead of the 1.3 GB
pytest workers agents had started, and every agent went down with it. Pins:

* the one statement every launch path runs first
  (``launch_script.oom_score_line``) — its value, the
  ``MINDFLOCK_OOM_SCORE_ADJ`` override / off switch, and that it really lands
  on the shell and its children (Linux only);
* every launch path carries it: the exit-marker wrapper, the provisioned
  launcher, the standalone pipeline's ``launch_command``, the shell tab, the
  engine's tmux start (pinned in test_tmux_session);
* ``unless_killed``: a resume chain's fallbacks pass 137 through instead of
  relaunching straight back into the squeeze, and still run on any other
  failure — executed in a real ``sh``.

No tmux, no agent CLI: the shell checks run ``sh -c`` on fake commands.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

import pytest

from backend.providers import launch_script
from backend.session import provisioned
from backend.web.core import terminal

LINE = "{ echo 500 >/proc/self/oom_score_adj; } 2>/dev/null || :"

_HAS_PROC_OOM = os.path.exists("/proc/self/oom_score_adj")


@pytest.fixture(autouse=True)
def _default_env(monkeypatch):
    monkeypatch.delenv("MINDFLOCK_OOM_SCORE_ADJ", raising=False)


# --------------------------------------------------------------------------- #
# the statement and its knob
# --------------------------------------------------------------------------- #
def test_the_line_raises_to_the_named_constant():
    assert launch_script.OOM_SCORE_ADJ == 500
    assert launch_script.oom_score_line() == LINE
    assert launch_script.oom_score_prefix() == LINE + "; "


@pytest.mark.parametrize(
    "raw,value",
    [("0", 0), ("-300", 0), ("300", 300), ("5000", 1000), ("lots", 500), ("", 500)],
)
def test_env_overrides_clamps_and_turns_off(monkeypatch, raw, value):
    monkeypatch.setenv("MINDFLOCK_OOM_SCORE_ADJ", raw)
    assert launch_script.oom_score_adj() == value
    if value:
        assert launch_script.oom_score_line() == LINE.replace("500", str(value))
    else:
        assert launch_script.oom_score_line() == ""
        assert launch_script.oom_score_prefix() == ""


@pytest.mark.skipif(not _HAS_PROC_OOM, reason="Linux /proc only")
def test_the_line_lands_on_the_shell_and_everything_it_starts():
    """A builtin echo writes from the shell itself, and the score is inherited
    across fork + exec — so the CLI and whatever it runs carry it."""
    out = subprocess.run(
        ["sh", "-c", LINE + "; sh -c 'cat /proc/$$/oom_score_adj'"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert out.returncode == 0
    assert out.stdout.strip() == "500"
    assert out.stderr == ""


@pytest.mark.parametrize("shell", ["sh", "bash", "zsh"])
def test_the_line_is_silent_where_it_cannot_apply(shell):
    """No /proc (macOS) or a refused write: no error text, and the command
    after it still runs. (The naive ``echo … > f 2>/dev/null`` prints the
    failed redirect before ``2>`` applies.)"""
    if subprocess.run(["which", shell], capture_output=True).returncode != 0:
        pytest.skip("%s not installed" % shell)
    line = LINE.replace("/proc/self/oom_score_adj", "/nonexistent/oom_score_adj")
    out = subprocess.run(
        [shell, "-c", line + "; echo after"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert out.stdout == "after\n"
    assert out.stderr == ""


# --------------------------------------------------------------------------- #
# every launch path carries it
# --------------------------------------------------------------------------- #
def test_the_exit_marker_wrapper_raises_first(tmp_path, monkeypatch):
    monkeypatch.setattr(terminal, "_EXIT_MARKER_DIR", tmp_path)
    q = shlex.quote(str(terminal._exit_marker_path("s")))
    assert terminal._wrap_launch_cmd("claude", "s") == (
        "rm -f %s; %s; claude; echo $? > %s" % (q, LINE, q)
    )
    # Off: byte-identical to the wrapper before this existed.
    monkeypatch.setenv("MINDFLOCK_OOM_SCORE_ADJ", "0")
    assert terminal._wrap_launch_cmd("claude", "s") == (
        "rm -f %s; claude; echo $? > %s" % (q, q)
    )


def test_the_provisioned_launcher_raises_before_its_exec(tmp_path, monkeypatch):
    path = provisioned.write_launcher(str(tmp_path), "", program="claude")
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    assert lines[2] == LINE
    assert lines[3].startswith("exec bash -ilc ")
    monkeypatch.setenv("MINDFLOCK_OOM_SCORE_ADJ", "0")
    path = provisioned.write_launcher(str(tmp_path), "", program="claude")
    assert LINE not in Path(path).read_text(encoding="utf-8")


def test_the_standalone_pipeline_launch_raises_first(monkeypatch):
    """claude_runner / pr_runner (no engine) run ``preamble + command``."""
    preamble, command = launch_script.launch_command("claude", "/tmp/p.md")
    assert preamble.startswith(LINE + "\n")
    assert command.startswith("claude ")
    monkeypatch.setenv("MINDFLOCK_OOM_SCORE_ADJ", "0")
    preamble, _ = launch_script.launch_command("claude", "/tmp/p.md")
    assert LINE not in preamble


def test_the_shell_tab_starts_through_the_line(monkeypatch):
    from backend.web.core import agent_sessions

    calls = []

    class _Srv:
        def _shell_tmux_name(self, title):
            return "mindflock_shell_" + title

        def _live_session_name(self, name):
            return None

    def fake_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(agent_sessions, "_server", lambda: _Srv())
    monkeypatch.setattr(agent_sessions, "_peer_shell_refused", lambda t, w: False)
    monkeypatch.setattr(agent_sessions._proc, "run_capped", fake_run)
    monkeypatch.setattr(agent_sessions, "apply_scroll_speed", lambda: None)
    monkeypatch.setenv("SHELL", "/bin/zsh")
    name, err = agent_sessions._ensure_shell_session("t", "/tmp")
    assert err is None
    new = [c for c in calls if c[:2] == ["tmux", "new-session"]][0]
    assert new[-1] == LINE + "; exec /bin/zsh"


# --------------------------------------------------------------------------- #
# unless_killed: SIGKILL passes through, any other failure still falls back
# --------------------------------------------------------------------------- #
def _chain_rc(first: str) -> tuple:
    chain = "%s || %s || %s; echo rc=$?" % (
        first,
        launch_script.unless_killed("echo retry; false"),
        launch_script.unless_killed("echo fresh"),
    )
    out = subprocess.run(
        ["sh", "-c", chain], capture_output=True, text=True, timeout=10
    )
    return out.stdout.split()


def test_a_sigkilled_cli_is_not_retried_or_restarted_fresh():
    assert _chain_rc("sh -c 'kill -9 $$'") == ["rc=137"]


def test_a_failed_resume_still_retries_then_starts_fresh():
    assert _chain_rc("sh -c 'exit 1'") == ["retry", "fresh", "rc=0"]


def test_the_wrapper_still_records_137(tmp_path, monkeypatch):
    """``(exit 137)`` sets the status without leaving the wrapper shell, so
    its trailing ``echo $? > marker`` runs — the auto-revive keys on it."""
    monkeypatch.setattr(terminal, "_EXIT_MARKER_DIR", tmp_path)
    cmd = "sh -c 'kill -9 $$' || %s" % launch_script.unless_killed("echo fresh")
    subprocess.run(
        ["sh", "-c", terminal._wrap_launch_cmd(cmd, "k")],
        capture_output=True,
        timeout=10,
    )
    assert terminal._read_exit_marker("k") == launch_script.KILLED_EXIT_CODE
    assert not terminal._is_natural_exit(launch_script.KILLED_EXIT_CODE)


def test_the_provisioned_launcher_ends_on_sigkill(tmp_path):
    """137 ends the script with that code (the pane closes, the server
    revives it); the old in-script loop would have relaunched it at once."""
    path = provisioned.write_launcher(str(tmp_path), "", program="claude")
    text = Path(path).read_text(encoding="utf-8")
    assert "fi\nif [ $? -eq 137 ]; then exit 137; fi\nwhile true; do\n" in text
