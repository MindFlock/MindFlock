"""tmux session plumbing for a session's two panes (agent CLI + shell):
``backend.web.core.agent_sessions``.

Every ``tmux`` invocation is routed through ``server._run_capped``, which is
replaced here with a scripted recorder so the branches (attach-existing,
create, the duplicate-session race, kill/teardown, typed input) are exercised
with zero real processes. The agent-launch branch mocks the provider and the
exit-marker / launcher plumbing it consults.
"""

from __future__ import annotations

import pytest

from backend.web import server
from backend.web.core import agent_sessions


class FakeProc:
    """Minimal ``subprocess.CompletedProcess`` stand-in."""

    def __init__(self, returncode: int = 0, stderr: bytes = b""):
        self.returncode = returncode
        self.stderr = stderr


class Recorder:
    """Records every ``_run_capped`` argv and answers via a handler keyed on the
    tmux subcommand. ``responses`` maps a subcommand (``has-session``,
    ``new-session``, …) to a FakeProc or a list of FakeProcs consumed in order."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    def _subcmd(self, argv):
        # argv[0] == "tmux"; argv[1] is the tmux subcommand.
        return argv[1] if len(argv) > 1 else ""

    def __call__(self, args, **kw):
        argv = list(args)
        self.calls.append(argv)
        resp = self.responses.get(self._subcmd(argv), FakeProc(0))
        if isinstance(resp, list):
            return resp.pop(0) if resp else FakeProc(0)
        return resp

    def argvs(self, subcmd):
        return [c for c in self.calls if self._subcmd(c) == subcmd]


@pytest.fixture
def rec(monkeypatch):
    r = Recorder()
    monkeypatch.setattr(server, "_run_capped", r)
    return r


# --------------------------------------------------------------------------- #
# name helpers
# --------------------------------------------------------------------------- #
def test_shell_tmux_name_sanitizes_and_suffixes():
    # to_mindflock_tmux_name strips whitespace + prepends the prefix; _sh suffix.
    assert agent_sessions._shell_tmux_name("My Title") == "mindflock_MyTitle_sh"


def test_shell_tmux_name_dots_become_underscores():
    assert agent_sessions._shell_tmux_name("a.b") == "mindflock_a_b_sh"


# --------------------------------------------------------------------------- #
# _live_session_name
# --------------------------------------------------------------------------- #
def test_live_session_name_present(rec):
    rec.responses = {"has-session": FakeProc(0)}
    assert agent_sessions._live_session_name("mindflock_x") == "mindflock_x"
    # has-session is asked with the -t= form.
    assert rec.calls[0][:2] == ["tmux", "has-session"]
    assert "-t=mindflock_x" in rec.calls[0]


def test_live_session_name_absent(rec):
    rec.responses = {"has-session": FakeProc(1)}
    assert agent_sessions._live_session_name("mindflock_x") is None


# --------------------------------------------------------------------------- #
# _ensure_shell_session
# --------------------------------------------------------------------------- #
def test_ensure_shell_attaches_existing_without_creating(rec):
    rec.responses = {"has-session": FakeProc(0)}
    name, err = agent_sessions._ensure_shell_session("Title", "/wt")
    assert (name, err) == ("mindflock_Title_sh", None)
    assert rec.argvs("new-session") == []  # never created


def test_ensure_shell_creates_and_sets_options(rec, monkeypatch):
    applied = []
    monkeypatch.setattr(agent_sessions, "apply_scroll_speed", lambda: applied.append(1))
    # Not live, create succeeds.
    rec.responses = {"has-session": FakeProc(1), "new-session": FakeProc(0)}
    name, err = agent_sessions._ensure_shell_session("Title", "/wt")
    assert (name, err) == ("mindflock_Title_sh", None)
    new = rec.argvs("new-session")[0]
    assert new[:3] == ["tmux", "new-session", "-d"]
    assert "-c" in new and "/wt" in new
    # Options tuned: set-option is ["tmux","set-option","-t",name,opt,val].
    opts = {(c[-2], c[-1]) for c in rec.argvs("set-option")}
    assert ("mouse", "on") in opts
    assert ("history-limit", "100000") in opts
    assert ("window-size", "latest") in opts
    assert ("alternate-screen", "off") in opts
    assert applied == [1]  # scroll speed re-asserted


def test_ensure_shell_create_race_treated_as_success(rec):
    # new-session fails ("duplicate session"), but a follow-up has-session shows
    # it now exists (the websocket + commit both raced to create it).
    rec.responses = {
        "has-session": [FakeProc(1), FakeProc(0)],
        "new-session": FakeProc(1, stderr=b"duplicate session"),
    }
    name, err = agent_sessions._ensure_shell_session("Title", "/wt")
    assert (name, err) == ("mindflock_Title_sh", None)


def test_ensure_shell_create_failure_returns_stderr(rec):
    rec.responses = {
        "has-session": [FakeProc(1), FakeProc(1)],
        "new-session": FakeProc(1, stderr=b"  no server running  "),
    }
    name, err = agent_sessions._ensure_shell_session("Title", "/wt")
    assert name == "mindflock_Title_sh"
    assert err == "no server running"  # decoded + stripped


# --------------------------------------------------------------------------- #
# _send_to_shell / _send_to_agent
# --------------------------------------------------------------------------- #
def test_send_to_shell_types_then_enter(rec):
    agent_sessions._send_to_shell("sh1", "ls -la")
    sends = rec.argvs("send-keys")
    assert len(sends) == 2
    assert sends[0][-2:] == ["-l", "ls -la"]  # literal text
    assert sends[1][-1] == "Enter"  # then a separate Enter


def test_send_to_agent_empty_name_is_false(rec):
    assert agent_sessions._send_to_agent("", "hi") is False
    assert rec.calls == []  # short-circuits before touching tmux


def test_send_to_agent_missing_session_is_false(rec):
    rec.responses = {"has-session": FakeProc(1)}
    assert agent_sessions._send_to_agent("s", "hi") is False


def test_send_to_agent_type_failure_is_false(rec):
    rec.responses = {"has-session": FakeProc(0), "send-keys": FakeProc(1)}
    assert agent_sessions._send_to_agent("s", "hi") is False


def test_send_to_agent_submit_sends_enter_after_pause(rec, monkeypatch):
    slept = []
    monkeypatch.setattr(agent_sessions.time, "sleep", lambda s: slept.append(s))
    rec.responses = {"has-session": FakeProc(0), "send-keys": FakeProc(0)}
    assert agent_sessions._send_to_agent("s", "hi", submit=True) is True
    sends = rec.argvs("send-keys")
    assert sends[0][-2:] == ["-l", "hi"]
    assert sends[-1][-1] == "Enter"
    assert slept == [0.15]  # ended the paste burst before Enter


def test_send_to_agent_no_submit_skips_enter(rec, monkeypatch):
    monkeypatch.setattr(agent_sessions.time, "sleep", lambda s: None)
    rec.responses = {"has-session": FakeProc(0), "send-keys": FakeProc(0)}
    assert agent_sessions._send_to_agent("s", "hi", submit=False) is True
    sends = rec.argvs("send-keys")
    assert len(sends) == 1  # only the literal text; no Enter
    assert sends[0][-2:] == ["-l", "hi"]


# --------------------------------------------------------------------------- #
# kill helpers
# --------------------------------------------------------------------------- #
def test_kill_named_session(rec):
    agent_sessions._kill_named_session("mindflock_x")
    kills = rec.argvs("kill-session")
    assert kills == [["tmux", "kill-session", "-t", "mindflock_x"]]


def test_kill_shell_session_targets_sh_name(rec):
    agent_sessions._kill_shell_session("Title")
    assert rec.argvs("kill-session")[0][-1] == "mindflock_Title_sh"


def test_kill_agent_session_targets_agent_name(rec):
    agent_sessions._kill_agent_session("Title")
    assert rec.argvs("kill-session")[0][-1] == "mindflock_Title"


# --------------------------------------------------------------------------- #
# _ensure_agent_session
# --------------------------------------------------------------------------- #
class FakeInst:
    def __init__(self, wt="/wt", program="claude", in_place=False, launch_args=()):
        self._wt = wt
        self.Program = program
        self.InPlace = in_place
        self.LaunchArgs = launch_args

    def GetWorktreePath(self):
        return self._wt


class FakeProvider:
    def __init__(self, natural=False, cmd="claude --continue"):
        self._natural = natural
        self._cmd = cmd
        self.hooks_installed = []

    def is_natural_exit(self, code):
        return self._natural

    def build_launch_command(self, ctx):
        self.last_ctx = ctx
        return self._cmd

    def install_activity_hooks(self, wt, name):
        self.hooks_installed.append((wt, name))


def _wire_agent(monkeypatch, provider, *, marker=None, isfile=False):
    monkeypatch.setattr(agent_sessions.providers, "resolve", lambda prog: provider)
    monkeypatch.setattr(agent_sessions, "_read_exit_marker", lambda name: marker)
    monkeypatch.setattr(agent_sessions, "_clear_exit_marker", lambda name: None)
    monkeypatch.setattr(
        agent_sessions, "_wrap_launch_cmd", lambda cmd, name: "WRAP<%s>" % cmd
    )
    monkeypatch.setattr(agent_sessions, "apply_scroll_speed", lambda: None)
    monkeypatch.setattr(agent_sessions.os.path, "isfile", lambda p: isfile)
    monkeypatch.setattr(agent_sessions.os.path, "isdir", lambda p: True)


def test_ensure_agent_attaches_existing(rec, monkeypatch):
    rec.responses = {"has-session": FakeProc(0)}
    prov = FakeProvider()
    _wire_agent(monkeypatch, prov)
    name, err = agent_sessions._ensure_agent_session(FakeInst(), "Title")
    assert (name, err) == ("mindflock_Title", None)
    assert rec.argvs("new-session") == []
    assert prov.hooks_installed == []  # never reached launch


def test_ensure_agent_missing_workspace(rec, monkeypatch):
    rec.responses = {"has-session": FakeProc(1)}
    _wire_agent(monkeypatch, FakeProvider())
    monkeypatch.setattr(agent_sessions.os.path, "isdir", lambda p: False)
    name, err = agent_sessions._ensure_agent_session(FakeInst(wt=""), "Title")
    assert name == "mindflock_Title"
    assert err == "workspace no longer exists"


def test_ensure_agent_fresh_start_clears_thread_marker(rec, monkeypatch):
    # Natural exit -> fresh start -> the recorded thread id is dropped so a later
    # crash-resume can't target a conversation this run never had.
    rec.responses = {"has-session": FakeProc(1), "new-session": FakeProc(0)}
    prov = FakeProvider(natural=True, cmd="claude")
    _wire_agent(monkeypatch, prov, marker=0, isfile=False)
    cleared = []
    from backend.providers import thread_markers

    monkeypatch.setattr(thread_markers, "clear", lambda name: cleared.append(name))

    name, err = agent_sessions._ensure_agent_session(FakeInst(), "Title")
    assert (name, err) == ("mindflock_Title", None)
    assert cleared == ["mindflock_Title"]
    # build_launch_command saw resume=False (natural exit).
    assert prov.last_ctx.resume is False
    # The wrapped command is what tmux new-session runs (sh -c <wrapped>).
    new = rec.argvs("new-session")[0]
    assert new[-1] == "WRAP<claude>"
    assert prov.hooks_installed == [("/wt", "mindflock_Title")]


def test_ensure_agent_resume_keeps_thread_marker(rec, monkeypatch):
    # Unnatural death (marker 137) -> resume -> thread marker is NOT cleared.
    rec.responses = {"has-session": FakeProc(1), "new-session": FakeProc(0)}
    prov = FakeProvider(natural=False, cmd="claude --continue")
    _wire_agent(monkeypatch, prov, marker=137, isfile=False)
    cleared = []
    from backend.providers import thread_markers

    monkeypatch.setattr(thread_markers, "clear", lambda name: cleared.append(name))

    agent_sessions._ensure_agent_session(FakeInst(), "Title")
    assert cleared == []  # resume keeps the id
    assert prov.last_ctx.resume is True


def test_ensure_agent_uses_launcher_when_present(rec, monkeypatch):
    # A provisioned worktree with a launcher script: re-run it verbatim (it
    # handles --continue), don't ask the provider to build a command.
    rec.responses = {"has-session": FakeProc(1), "new-session": FakeProc(0)}
    prov = FakeProvider(natural=False)
    _wire_agent(monkeypatch, prov, marker=None, isfile=True)
    monkeypatch.setattr(
        agent_sessions.os.path, "join", lambda *a: "/wt/.mindflock_launch.sh"
    )

    agent_sessions._ensure_agent_session(FakeInst(in_place=False), "Title")
    new = rec.argvs("new-session")[0]
    assert new[-1] == "WRAP</wt/.mindflock_launch.sh>"
    assert not hasattr(prov, "last_ctx")  # provider command path not taken


def test_ensure_agent_inplace_ignores_launcher(rec, monkeypatch):
    # In-place sessions borrow a worktree and must never reuse its owner's
    # launcher — they always build a fresh provider command.
    rec.responses = {"has-session": FakeProc(1), "new-session": FakeProc(0)}
    prov = FakeProvider(natural=False, cmd="claude")
    _wire_agent(monkeypatch, prov, marker=None, isfile=True)
    agent_sessions._ensure_agent_session(FakeInst(in_place=True), "Title")
    assert prov.last_ctx.in_place is True  # provider path taken despite launcher


def test_ensure_agent_none_command_falls_back_to_bare_program(rec, monkeypatch):
    # A custom program whose provider returns None -> run the bare program.
    rec.responses = {"has-session": FakeProc(1), "new-session": FakeProc(0)}
    prov = FakeProvider(natural=False, cmd=None)
    _wire_agent(monkeypatch, prov, marker=None, isfile=False)
    agent_sessions._ensure_agent_session(
        FakeInst(program="mytool", in_place=True), "Title"
    )
    new = rec.argvs("new-session")[0]
    assert new[-1] == "WRAP<mytool>"  # bare Program was wrapped


def test_ensure_agent_hook_install_failure_is_swallowed(rec, monkeypatch):
    # install_activity_hooks is best-effort; a raise must not abort the launch.
    rec.responses = {"has-session": FakeProc(1), "new-session": FakeProc(0)}
    prov = FakeProvider(natural=False, cmd="claude")

    def boom(wt, name):
        raise RuntimeError("hooks broken")

    prov.install_activity_hooks = boom
    _wire_agent(monkeypatch, prov, marker=137, isfile=False)
    name, err = agent_sessions._ensure_agent_session(FakeInst(in_place=True), "Title")
    assert (name, err) == ("mindflock_Title", None)  # launch still succeeded


def test_ensure_agent_create_race_treated_as_success(rec, monkeypatch):
    rec.responses = {
        "has-session": [FakeProc(1), FakeProc(0)],
        "new-session": FakeProc(1, stderr=b"duplicate session"),
    }
    prov = FakeProvider(natural=True, cmd="claude")
    _wire_agent(monkeypatch, prov, marker=0, isfile=False)
    from backend.providers import thread_markers

    monkeypatch.setattr(thread_markers, "clear", lambda name: None)
    name, err = agent_sessions._ensure_agent_session(FakeInst(), "Title")
    assert (name, err) == ("mindflock_Title", None)


def test_ensure_agent_create_failure_returns_stderr(rec, monkeypatch):
    rec.responses = {
        "has-session": [FakeProc(1), FakeProc(1)],
        "new-session": FakeProc(1, stderr=b"boom"),
    }
    prov = FakeProvider(natural=True, cmd="claude")
    _wire_agent(monkeypatch, prov, marker=0, isfile=False)
    from backend.providers import thread_markers

    monkeypatch.setattr(thread_markers, "clear", lambda name: None)
    name, err = agent_sessions._ensure_agent_session(FakeInst(), "Title")
    assert (name, err) == ("mindflock_Title", "boom")
