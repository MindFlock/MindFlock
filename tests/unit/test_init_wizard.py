"""Guided first run (`mindflock init`): the three-steps block every terminal door
prints, the read-only report (serve banner, piped stdin, install.sh), the
wizard's steps, the repo picker's three accepted answers, and the
`serve --setup` wiring."""

from __future__ import annotations

import http.server
import io
import json
import os
import subprocess
import sys
import threading
import time
import types

import pytest

import backend.web.run as run
from backend import cli, doctor, init_wizard
from backend.config import settings as settings_mod
from backend.doctor import Check

_TMUX_MISSING = Check(
    "tmux",
    "tmux",
    "fail",
    "not found on PATH — sessions cannot start without it",
    "sudo apt install tmux",
    cmd="sudo apt install tmux",
)
_TMUX_INSTALLABLE = Check(
    "tmux", "tmux", "fail", "not found on PATH", pkg="tmux", install=True
)
_GIT_OK = Check("git", "git", "ok", "git version 2.43.0")
_AGENT_OK = Check(
    "agent-cli", "agent CLI (claude)", "ok", "/usr/bin/claude", provider="claude"
)
_AUTH_WARN = Check(
    "agent-auth",
    "agent auth (claude)",
    "warn",
    "CLI is installed but no sign of a login was found",
    "run `claude` once to log in",
    cmd="claude",
    provider="claude",
)
_AUTH_OK = Check(
    "agent-auth", "agent auth (claude)", "ok", "signed in", provider="claude"
)
_READY = [_GIT_OK, _AGENT_OK, _AUTH_OK]


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """The wizard WRITES settings (general.last_repo_path), so every test in this
    module must be pointed at a throwaway store — otherwise running the suite
    edits the developer's own ~/.mindflock/settings.json."""
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))


@pytest.fixture(autouse=True)
def no_plan(monkeypatch):
    """The first-run plan reads fleet, Tailscale and gh state from the machine,
    and the devices question probes the tailnet: both off by default here
    (TestFirstRunPlan turns canned ones on)."""
    monkeypatch.setattr(init_wizard, "_plan", lambda checks: None)
    monkeypatch.setattr(init_wizard, "_mindflock_peer_nearby", lambda timeout=0: False)


@pytest.fixture()
def no_prompts(monkeypatch):
    """Any call to input() fails the test: the paths that use this fixture claim
    to be non-interactive, and a prompt there is a hang in production."""
    monkeypatch.setattr(
        "builtins.input", lambda prompt="": pytest.fail("must not prompt")
    )


def _checks(monkeypatch, *checks):
    """Pin what the doctor returns (any group argument)."""
    monkeypatch.setattr(doctor, "run_checks", lambda *a, **k: list(checks))


def _candidates(monkeypatch, *entries):
    """Pin the suggestion list so no test depends on what is on this machine."""
    monkeypatch.setattr(init_wizard, "_candidate_repos", lambda limit=8: list(entries))


def _entry(path, is_git=True, source="recent"):
    return {
        "path": str(path),
        "name": os.path.basename(str(path)),
        "is_git": is_git,
        "source": source,
    }


def _answers(monkeypatch, *replies):
    """Feed the picker a scripted stdin, one reply per prompt."""
    it = iter(replies)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(it))


def _git_repo(path):
    """A real repo, not a bare ``.git`` directory: the wizard asks git itself
    whether a folder is a work tree, and an empty ``.git`` is not one."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def _eventually(pred, timeout=5.0):
    """Wait for something a background thread does. run.py prints the three
    steps off the bind path on purpose, so a test that asserts on them has to
    wait rather than assume main() already did it."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


def _drain(capsys, needle, *, err=False, timeout=5.0):
    """Captured output, accumulated until ``needle`` shows up or time runs out.

    A single ``capsys.readouterr()`` can run before the preflight thread has
    written anything; reading in a loop and concatenating rebuilds the stream in
    order. Returns the requested stream only — the other one is drained too, so
    ask for the one you assert on."""
    out = ""
    errs = ""
    deadline = time.time() + timeout
    while True:
        cap = capsys.readouterr()
        out += cap.out
        errs += cap.err
        text = errs if err else out
        if needle in text or time.time() > deadline:
            return text
        time.sleep(0.01)


@pytest.fixture()
def uvicorn_spy(monkeypatch):
    """run.main() without a bind: records the uvicorn.run kwargs instead.

    The double-launch guard probes the real port — a dev machine with a live
    server on 8765 would short-circuit main() before uvicorn.run."""
    import uvicorn

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append(kw))
    monkeypatch.setattr(run, "_port_squatter", lambda host, port: "")
    monkeypatch.setenv("CS_WEB_MODE", "")
    monkeypatch.setenv("UVICORN_PORT", "")
    return calls


@pytest.fixture()
def quiet_checks(monkeypatch):
    """Keep run.py's preflight off this machine's real tmux/agent CLI: it runs on
    a daemon thread that can outlive the test."""
    _checks(monkeypatch, _GIT_OK)


def _state(checks, folder="/home/me/code/foo"):
    return init_wizard.readiness(list(checks), folder)


class TestThreeSteps:
    """The block every terminal door prints, and the ONE next action each ends on."""

    def test_sign_in_missing_never_says_youre_set(self, monkeypatch):
        # F2: tools ✓ and a folder ✓ are not "set" while the agent sits at a
        # login screen — that is the web checklist's "Needs attention" too.
        monkeypatch.setattr(init_wizard, "_is_git_worktree", lambda p: True)
        st = _state([_GIT_OK, _AGENT_OK, _AUTH_WARN])
        lines = init_wizard.steps_block(st, "init")
        text = "\n".join(lines)
        assert "You're set" not in text
        assert lines[0] == "MindFlock is installed. Three steps to your first agent:"
        sign = next(line for line in lines if "2. Sign in to your agent" in line)
        assert sign.lstrip().startswith("✗")
        assert sign.endswith("← you are here")
        assert text.count("← you are here") == 1
        assert init_wizard.next_action(st, "init") == (
            "Next: sign in to Claude Code — run claude, sign in, then type /exit. "
            "Then run mindflock init again."
        )

    def test_all_three_done_is_the_only_youre_set(self, monkeypatch):
        monkeypatch.setattr(init_wizard, "_is_git_worktree", lambda p: True)
        st = _state(_READY)
        lines = init_wizard.steps_block(st, "init")
        assert lines[0] == "You're set."
        assert "← you are here" not in "\n".join(lines)
        assert [line.lstrip()[:2] for line in lines[1:4]] == ["✓ ", "✓ ", "✓ "]
        assert init_wizard.next_action(st, "init") == (
            'Next: mindflock serve, then open http://127.0.0.1:8765 and click "+ New session"'
        )

    def test_missing_tools_point_at_doctor_fix(self):
        st = _state([_GIT_OK, _TMUX_INSTALLABLE, _AGENT_OK, _AUTH_WARN])
        lines = init_wizard.steps_block(st, "init")
        tools = next(line for line in lines if "1. Install the tools" in line)
        assert "✗" in tools and "missing tmux" in tools and "← you are here" in tools
        assert init_wizard.next_action(st, "doctor") == (
            "Next: mindflock doctor --fix installs the 1 missing tool (tmux)"
        )
        assert init_wizard.next_action(st, "init").endswith(
            "Then run mindflock init again."
        )

    def test_the_optional_line_names_join(self):
        lines = init_wizard.steps_block(_state(_READY), "install")
        assert lines[-1].strip() == (
            "Optional, later: GitHub, ticket sources, phone. "
            "Already use MindFlock elsewhere? mindflock join"
        )

    def test_install_door_always_hands_over_to_init(self):
        st = _state([_GIT_OK, _TMUX_INSTALLABLE], folder="")
        assert init_wizard.next_action(st, "install") == "Next: run mindflock init"

    def test_serve_door_boxes_the_browser_address_as_step_three(self):
        st = _state([_GIT_OK, _AGENT_OK, _AUTH_WARN], folder="")
        lines = init_wizard.steps_block(st, "serve", url="http://127.0.0.1:9999")
        text = "\n".join(lines)
        assert lines[0] == "Three steps to your first agent:"
        assert "│ 3. Open http://127.0.0.1:9999 in your browser" in text
        assert "walks you through the rest" in text
        assert "┌" in text and "┘" in text
        assert "Next:" not in text
        assert "3. Pick a project folder" not in text

    def test_a_non_git_suggestion_says_git_init(self):
        # F13: never hand out a session command for a folder with no git.
        st = _state(_READY, folder="")
        lines = init_wizard.steps_block(
            st, "init", suggestion=_entry("/home/me/emptydir", is_git=False)
        )
        assert any(
            "/home/me/emptydir has no git yet — run git init there, or pick a repo"
            in line
            for line in lines
        )

    def test_a_codex_style_login_is_one_command(self):
        auth = Check("agent-auth", "agent auth (codex)", "warn", cmd="codex login")
        assert init_wizard.sign_in_hint(auth) == "run codex login"

    def test_steps_one_and_two_agree_with_the_web_checklist(self):
        # The web's Get set up reads the same doctor run through
        # setup_steps.doctor_progress; the terminal must tick the same steps.
        from backend.web.core import setup_steps

        for checks in (
            [_GIT_OK, _AGENT_OK, _AUTH_WARN],
            [_GIT_OK, _AGENT_OK, _AUTH_OK],
            [_GIT_OK, _TMUX_MISSING, _AGENT_OK, _AUTH_OK],
            [
                _GIT_OK,
                Check("agent-cli", "agent CLI", "fail", provider="claude"),
                _AUTH_WARN,
            ],
        ):
            st = _state(checks)
            web = setup_steps.doctor_progress(checks)
            assert (st["tools"], st["sign_in"]) == (web["tools"], web["agent_login"])


class TestReport:
    def test_piped_summary_ends_on_one_next_action(
        self, monkeypatch, capsys, no_prompts
    ):
        _checks(monkeypatch, _GIT_OK, _TMUX_MISSING)
        _candidates(monkeypatch, _entry("/home/me/code/foo"))
        init_wizard.report()
        out = capsys.readouterr().out
        assert "✗ 1. Install the tools" in out and "missing tmux" in out
        assert "e.g. /home/me/code/foo" in out
        assert out.rstrip().splitlines()[-1].startswith("Next: ")
        assert sum(1 for line in out.splitlines() if line.startswith("Next:")) == 1
        assert "You're set" not in out

    def test_serving_runs_only_the_run_probes(self, monkeypatch, no_prompts):
        seen = []
        monkeypatch.setattr(
            doctor, "run_checks", lambda *a: seen.append(a) or [_GIT_OK]
        )
        init_wizard.report(io.StringIO(), serving=True)
        assert seen == [("run",)]

    def test_serving_with_everything_done_is_just_the_address(
        self, monkeypatch, tmp_path, no_prompts
    ):
        _checks(monkeypatch, *_READY)
        settings_mod.update_settings(general={"last_repo_path": str(tmp_path)})
        buf = io.StringIO()
        init_wizard.report(buf, serving=True, url="http://127.0.0.1:9000")
        out = buf.getvalue()
        assert "Open http://127.0.0.1:9000 in your browser" in out
        assert "Three steps" not in out and "You're set" not in out

    def test_doctor_that_raises_still_prints_the_steps(
        self, monkeypatch, capsys, no_prompts
    ):
        def _boom(*a):
            raise RuntimeError("doctor blew up")

        monkeypatch.setattr(doctor, "run_checks", _boom)
        _candidates(monkeypatch, _entry("/home/me/code/foo"))
        init_wizard.report()  # must not raise
        out = capsys.readouterr().out
        assert "the check could not run" in out
        assert "Next: run mindflock doctor again" in out

    def test_writes_to_the_given_stream(self, monkeypatch, no_prompts):
        _checks(monkeypatch, _TMUX_MISSING)
        _candidates(monkeypatch, _entry("/home/me/code/foo"))
        buf = io.StringIO()
        init_wizard.report(buf)
        assert "missing tmux" in buf.getvalue()

    def test_a_broken_stream_never_raises(self, monkeypatch, no_prompts):
        # run.py prints this on the boot path: an unwritable stream must cost the
        # hint, never the server.
        class _Broken:
            def write(self, _text):
                raise OSError("stream is gone")

        _checks(monkeypatch, _TMUX_MISSING)
        _candidates(monkeypatch, _entry("/home/me/code/foo"))
        init_wizard.report(_Broken())

    def test_report_is_the_one_layer_that_degrades(self, monkeypatch):
        # Every caller (run.py's boot path, the non-TTY wizard) relies on this
        # swallow and keeps no second copy of the wording, so a printer that blows
        # up mid-way has to end here.
        def _boom(out, **kw):
            raise RuntimeError("printer exploded")

        monkeypatch.setattr(init_wizard, "_print_report", _boom)
        assert init_wizard.report() is None

    def test_install_door_via_init_steps(self, monkeypatch, capsys, no_prompts):
        _checks(monkeypatch, _GIT_OK, _TMUX_INSTALLABLE, _AGENT_OK, _AUTH_WARN)
        assert cli.main(["init", "--steps"]) == 0
        out = capsys.readouterr().out
        assert out.startswith(
            "MindFlock is installed. Three steps to your first agent:"
        )
        assert out.rstrip().splitlines()[-1] == "Next: run mindflock init"


class TestSharedDoctorPrinting:
    """The glyph table has exactly one implementation (cli.print_checks); the
    wizard no longer prints it at all — its block says where things stand."""

    @pytest.fixture()
    def printed(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            cli,
            "print_checks",
            lambda checks, stream=None, **kw: calls.append(list(checks)),
        )
        return calls

    def test_doctor_command_uses_it(self, monkeypatch, printed):
        _checks(monkeypatch, _GIT_OK)
        assert cli.main(["doctor"]) == 0
        assert printed == [[_GIT_OK]]

    def test_the_wizard_prints_no_table(
        self, monkeypatch, printed, tmp_path, no_prompts
    ):
        _checks(monkeypatch, _GIT_OK)
        _candidates(monkeypatch, _entry(tmp_path))
        assert init_wizard.run(assume_yes=True) == 0
        assert printed == []

    def test_empty_list_prints_nothing(self, capsys):
        cli.print_checks([])
        assert capsys.readouterr().out == ""


class TestNonTtyDegradation:
    def test_piped_stdin_explains_itself_first(self, monkeypatch, capsys, no_prompts):
        # F7: the explanation is the FIRST line, not the last.
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        _checks(monkeypatch, _TMUX_MISSING)
        _candidates(monkeypatch, _entry("/home/me/code/foo"))
        assert init_wizard.run() == 0
        lines = capsys.readouterr().out.rstrip().splitlines()
        assert lines[0] == (
            "No terminal attached, so this is a read-only summary — run "
            "mindflock init in a terminal to be walked through it."
        )
        assert lines[-1].startswith("Next: ")


class TestServingFlag:
    """`mindflock serve --setup` runs the wizard inside the process that binds the
    port a moment later, so it never signs off with "mindflock serve"."""

    def test_a_non_tty_setup_run_shows_the_browser_address(
        self, monkeypatch, capsys, no_prompts
    ):
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        _checks(monkeypatch, _GIT_OK)
        _candidates(monkeypatch, _entry("/home/me/code/foo"))
        assert init_wizard.run(serving=True) == 0
        out = capsys.readouterr().out
        assert "mindflock serve" not in out
        assert "in your browser" in out

    def test_a_terminal_setup_run_leaves_the_block_to_the_banner(
        self, monkeypatch, capsys, tmp_path, no_prompts
    ):
        _checks(monkeypatch, *_READY)
        _candidates(monkeypatch, _entry(_git_repo(tmp_path / "webapp")))
        assert init_wizard.run(assume_yes=True, serving=True) == 0
        out = capsys.readouterr().out
        assert "Next:" not in out and "You're set" not in out
        assert "mindflock serve" not in out

    def test_plain_init_ends_on_serve_and_the_address(
        self, monkeypatch, capsys, tmp_path, no_prompts
    ):
        _checks(monkeypatch, *_READY)
        _candidates(monkeypatch, _entry(_git_repo(tmp_path / "webapp")))
        assert init_wizard.run(assume_yes=True) == 0
        lines = capsys.readouterr().out.rstrip().splitlines()
        assert "You're set." in lines
        assert lines[-1] == (
            'Next: mindflock serve, then open http://127.0.0.1:8765 and click "+ New session"'
        )


class TestAssumeYes:
    def test_takes_the_top_suggestion_and_remembers_it(
        self, monkeypatch, capsys, tmp_path, no_prompts
    ):
        _checks(monkeypatch, _GIT_OK)
        repo = _git_repo(tmp_path / "webapp")
        _candidates(monkeypatch, _entry(repo), _entry(tmp_path / "other"))
        patches = []
        monkeypatch.setattr(
            settings_mod, "update_settings", lambda **kw: patches.append(kw)
        )
        assert init_wizard.run(assume_yes=True) == 0
        assert patches == [{"general": {"last_repo_path": str(repo)}}]
        out = capsys.readouterr().out
        assert "✓ 3. Pick a project folder  %s" % repo in out

    def test_the_choice_survives_into_the_settings_store(
        self, monkeypatch, tmp_path, no_prompts
    ):
        # The round trip through the real store (isolated by
        # MINDFLOCK_SETTINGS_FILE): general.last_repo_path is what the next New
        # Session dialog and the /api/repos/suggest ranking read.
        _checks(monkeypatch, _GIT_OK)
        repo = _git_repo(tmp_path / "webapp")
        _candidates(monkeypatch, _entry(repo))
        assert init_wizard.run(assume_yes=True) == 0
        assert settings_mod.load_settings().general.last_repo_path == str(repo)
        assert init_wizard._remembered_repo() == str(repo)

    def test_never_runs_an_installer_unwatched(
        self, monkeypatch, capsys, tmp_path, no_prompts
    ):
        # --yes means "don't ask me", not "install things nobody is watching":
        # the block names what's missing and the exit code stays honest.
        _checks(monkeypatch, _TMUX_MISSING)
        monkeypatch.setattr(
            cli, "_fix_checks", lambda *a, **k: pytest.fail("--yes must not run fixes")
        )
        _candidates(monkeypatch, _entry(tmp_path))
        assert init_wizard.run(assume_yes=True) == 1
        out = capsys.readouterr().out
        assert "✗ 1. Install the tools" in out and "missing tmux" in out
        assert "You're set" not in out
        assert out.rstrip().splitlines()[-1].startswith("Next: mindflock doctor")

    def test_sign_in_missing_is_not_set_but_exits_zero(
        self, monkeypatch, capsys, tmp_path, no_prompts
    ):
        # F2: no "You're set" before the agent is signed in; the exit code still
        # follows the doctor's (a sign-in is a warning there, not a failure).
        _checks(monkeypatch, _GIT_OK, _AGENT_OK, _AUTH_WARN)
        _candidates(monkeypatch, _entry(_git_repo(tmp_path / "webapp")))
        assert init_wizard.run(assume_yes=True) == 0
        out = capsys.readouterr().out
        assert "You're set" not in out
        assert out.rstrip().splitlines()[-1].startswith("Next: sign in to Claude Code")

    def test_a_non_git_folder_is_said_out_loud(
        self, monkeypatch, capsys, tmp_path, no_prompts
    ):
        _checks(monkeypatch, _GIT_OK)
        plain = tmp_path / "notes"
        plain.mkdir()
        _candidates(monkeypatch, _entry(plain, is_git=False, source="cwd"))
        assert init_wizard.run(assume_yes=True) == 0
        out = capsys.readouterr().out
        assert "%s has no git yet — run git init there, or pick a repo" % plain in out

    def test_no_candidates_still_ends_on_one_action(
        self, monkeypatch, capsys, no_prompts
    ):
        _checks(monkeypatch, _GIT_OK)
        _candidates(monkeypatch)
        assert init_wizard.run(assume_yes=True) == 0
        out = capsys.readouterr().out
        assert "✗ 3. Pick a project folder" in out
        assert out.rstrip().splitlines()[-1] == (
            "Next: run mindflock init again and type the path to your project folder"
        )

    def test_an_unwritable_settings_store_is_not_a_setup_failure(
        self, monkeypatch, tmp_path, no_prompts
    ):
        def _boom(**kw):
            raise OSError("read-only file system")

        _checks(monkeypatch, _GIT_OK)
        monkeypatch.setattr(settings_mod, "update_settings", _boom)
        _candidates(monkeypatch, _entry(tmp_path))
        assert init_wizard.run(assume_yes=True) == 0


class TestFixLoopReuse:
    """Installing tmux, installing the agent and signing it in are all doctor
    checks carrying runnable commands, so the wizard hands them to the doctor's
    own fix loop — limited to the ``run`` group, so nothing optional (Tailscale,
    GitHub, the MTU…) is ever offered on a first run (F4)."""

    def test_interactive_run_delegates_to_the_doctor_fix_loop(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        _checks(monkeypatch, _TMUX_MISSING)
        seen = []

        def _fix(checks, groups=None):
            seen.append((list(checks), groups))
            return [Check("tmux", "tmux", "ok", "tmux 3.4")]

        monkeypatch.setattr(cli, "_fix_checks", _fix)
        _candidates(monkeypatch, _entry(tmp_path))
        _answers(monkeypatch, "")  # Enter at the repo prompt = top suggestion
        # The re-probed 'ok' from the fix loop is what decides the exit code.
        assert init_wizard.run() == 0
        assert seen == [([_TMUX_MISSING], ("run",))]

    def test_an_optional_fix_is_never_offered_on_a_first_run(
        self, monkeypatch, tmp_path, capsys
    ):
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        tailscale = Check(
            "tailscale",
            "tailscale",
            "warn",
            "Tailscale is on Windows",
            cmd="curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up",
        )
        _checks(monkeypatch, _GIT_OK, tailscale)
        prompts = []
        monkeypatch.setattr(
            "builtins.input", lambda prompt="": prompts.append(prompt) or ""
        )
        _candidates(monkeypatch, _entry(tmp_path))
        assert init_wizard.run() == 0
        assert not any("tailscale" in p for p in prompts)
        assert len(prompts) == 1  # the folder question only


class TestRepoPicker:
    def _list(self, tmp_path):
        first = tmp_path / "first"
        second = tmp_path / "second"
        for p in (first, second):
            (p / ".git").mkdir(parents=True)
        return [_entry(first), _entry(second, source="nearby")]

    def test_number_picks_from_the_list(self, monkeypatch, tmp_path):
        cands = self._list(tmp_path)
        _answers(monkeypatch, "2")
        assert init_wizard._pick_repo(cands) == cands[1]["path"]

    def test_empty_keeps_the_top_suggestion(self, monkeypatch, tmp_path):
        cands = self._list(tmp_path)
        _answers(monkeypatch, "")
        assert init_wizard._pick_repo(cands) == cands[0]["path"]

    def test_typed_path_wins_over_the_list(self, monkeypatch, tmp_path):
        typed = tmp_path / "elsewhere"
        typed.mkdir()
        _answers(monkeypatch, str(typed))
        assert init_wizard._pick_repo(self._list(tmp_path)) == os.path.realpath(typed)

    def test_typed_path_expands_a_tilde(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / "code").mkdir()
        _answers(monkeypatch, "~/code")
        assert init_wizard._pick_repo(self._list(tmp_path)) == os.path.realpath(
            tmp_path / "code"
        )

    def test_out_of_range_number_asks_again(self, monkeypatch, capsys, tmp_path):
        cands = self._list(tmp_path)
        _answers(monkeypatch, "9", "1")
        assert init_wizard._pick_repo(cands) == cands[0]["path"]
        assert "there is no 9 in that list" in capsys.readouterr().out

    def test_path_that_does_not_exist_asks_again(self, monkeypatch, capsys, tmp_path):
        cands = self._list(tmp_path)
        _answers(monkeypatch, str(tmp_path / "nope"), "2")
        assert init_wizard._pick_repo(cands) == cands[1]["path"]
        assert "is not a folder yet" in capsys.readouterr().out

    def test_nonsense_forever_gives_up_on_the_top_suggestion(
        self, monkeypatch, tmp_path
    ):
        # A scripted stdin that never answers usefully must not trap the wizard.
        cands = self._list(tmp_path)
        monkeypatch.setattr("builtins.input", lambda prompt="": "/nope/nowhere")
        assert init_wizard._pick_repo(cands) == cands[0]["path"]

    def test_eof_keeps_the_top_suggestion(self, monkeypatch, tmp_path):
        def _eof(prompt=""):
            raise EOFError

        monkeypatch.setattr("builtins.input", _eof)
        cands = self._list(tmp_path)
        assert init_wizard._pick_repo(cands) == cands[0]["path"]

    def test_assume_yes_takes_the_top_without_asking(
        self, monkeypatch, tmp_path, no_prompts
    ):
        cands = self._list(tmp_path)
        assert init_wizard._pick_repo(cands, assume_yes=True) == cands[0]["path"]

    def test_numbered_list_marks_git_and_the_recent_folder(
        self, monkeypatch, capsys, tmp_path
    ):
        plain = tmp_path / "notes"
        plain.mkdir()
        _answers(monkeypatch, "")
        init_wizard._pick_repo(
            [_entry(tmp_path / "repo"), _entry(plain, is_git=False, source="nearby")]
        )
        out = capsys.readouterr().out
        assert "1  " in out and "2  " in out
        assert "git repo, used most recently" in out
        assert "no git yet" in out


class TestCandidateRepos:
    def test_picker_suggestions_are_used_and_the_memory_is_passed_in(self, monkeypatch):
        seen = {}

        def _suggest(recent_paths=(), cwd=None, limit=12):
            seen.update(recent=list(recent_paths), cwd=cwd, limit=limit)
            return [_entry("/home/me/code/foo")]

        mod = types.ModuleType("backend.web.core.repo_picker")
        mod.suggest_repos = _suggest
        monkeypatch.setitem(sys.modules, "backend.web.core.repo_picker", mod)
        monkeypatch.setattr(
            init_wizard, "_remembered_repo", lambda: "/home/me/code/foo"
        )
        assert init_wizard._candidate_repos(limit=3) == [_entry("/home/me/code/foo")]
        assert seen["recent"] == ["/home/me/code/foo"]
        assert seen["limit"] == 3

    def test_a_picker_that_blows_up_falls_back_to_the_cwd(self, monkeypatch, tmp_path):
        def _boom(**kwargs):
            raise RuntimeError("bad mount")

        mod = types.ModuleType("backend.web.core.repo_picker")
        mod.suggest_repos = _boom
        monkeypatch.setitem(sys.modules, "backend.web.core.repo_picker", mod)
        monkeypatch.setattr(init_wizard, "_remembered_repo", lambda: "")
        monkeypatch.chdir(tmp_path)
        assert init_wizard._candidate_repos() == [
            {
                "path": os.path.realpath(tmp_path),
                "name": os.path.basename(os.path.realpath(tmp_path)),
                "is_git": False,
                "source": "cwd",
            }
        ]

    def test_fallback_puts_the_remembered_folder_first_and_dedupes(
        self, monkeypatch, tmp_path
    ):
        repo = _git_repo(tmp_path / "webapp")
        monkeypatch.chdir(repo)
        cands = init_wizard._fallback_candidates([str(repo)])
        assert [c["source"] for c in cands] == ["recent"]  # cwd is the same folder
        assert cands[0]["is_git"] is True

    def test_fallback_skips_a_folder_that_is_gone(self, tmp_path):
        cands = init_wizard._fallback_candidates([str(tmp_path / "deleted")])
        assert all(c["path"] != str(tmp_path / "deleted") for c in cands)


class TestGitDetection:
    """Whether a folder is git-backed is asked of git itself, because the answer
    the wizard prints has to be the answer the session-create path will act on."""

    def test_a_subdirectory_of_a_repo_counts_as_a_repo(self, tmp_path):
        # ~/code/foo/src carries no .git of its own, yet a session started there
        # gets the worktree, the diff, the commit and the PR.
        sub = _git_repo(tmp_path / "foo") / "src"
        sub.mkdir()
        assert init_wizard._is_git_worktree(str(sub)) is True

    def test_a_plain_folder_is_still_not_one(self, tmp_path):
        assert init_wizard._is_git_worktree(str(tmp_path / "notes")) is False

    def test_the_wizard_does_not_tell_you_to_git_init_your_own_repo(
        self, monkeypatch, capsys, tmp_path, no_prompts
    ):
        sub = _git_repo(tmp_path / "foo") / "src"
        sub.mkdir()
        _checks(monkeypatch, _GIT_OK)
        _candidates(monkeypatch, _entry(sub))
        assert init_wizard.run(assume_yes=True) == 0
        out = capsys.readouterr().out
        assert "✓ 3. Pick a project folder  %s" % sub in out
        assert "Not a git repo" not in out
        assert "git init" not in out

    def test_without_the_web_extras_the_dot_git_entry_answers(
        self, monkeypatch, tmp_path
    ):
        # An install missing the web extras (the picker and the git helpers both
        # live under them) must still get an answer, not a traceback.
        (tmp_path / ".git").mkdir()
        monkeypatch.setitem(sys.modules, "backend.web.core.git_ops", None)
        assert init_wizard._is_git_worktree(str(tmp_path)) is True
        assert init_wizard._is_git_worktree(str(tmp_path / "nothing")) is False


class TestSettingsMemory:
    def test_unreadable_settings_mean_no_memory_not_a_crash(self, monkeypatch):
        def _boom():
            raise OSError("settings are gone")

        monkeypatch.setattr(settings_mod, "load_settings", _boom)
        assert init_wizard._remembered_repo() == ""

    def test_remembering_a_repo_writes_the_general_group(self, monkeypatch):
        patches = []
        monkeypatch.setattr(
            settings_mod, "update_settings", lambda **kw: patches.append(kw)
        )
        init_wizard._remember_repo("/home/me/code/foo")
        assert patches == [{"general": {"last_repo_path": "/home/me/code/foo"}}]


class TestInitCommand:
    def test_reachable_through_main_and_exits_zero(
        self, monkeypatch, tmp_path, no_prompts
    ):
        _checks(monkeypatch, _GIT_OK)
        _candidates(monkeypatch, _entry(tmp_path))
        assert cli.main(["init", "--yes"]) == 0

    def test_yes_flag_is_passed_through(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            init_wizard, "run", lambda assume_yes=False: seen.append(assume_yes) or 0
        )
        assert cli.main(["init", "--yes"]) == 0
        assert cli.main(["init"]) == 0
        assert seen == [True, False]

    def test_short_yes_flag_works(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            init_wizard, "run", lambda assume_yes=False: seen.append(assume_yes) or 0
        )
        cli.main(["init", "-y"])
        assert seen == [True]

    def test_steps_flag_prints_the_block_and_is_hidden(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            init_wizard, "print_steps", lambda door="install": seen.append(door) or 0
        )
        assert cli.main(["init", "--steps"]) == 0
        assert seen == ["install"]
        sub = cli._build_parser()._subparsers._group_actions[0].choices["init"]
        assert "--steps" not in sub.format_help()

    def test_help_describes_the_guided_setup(self):
        help_text = " ".join(cli._build_parser().format_help().split())
        assert (
            "guided first-run setup: install the tools, sign in to your agent, "
            "pick a project folder" in help_text
        )

    def test_wizard_exit_code_is_the_command_exit_code(self, monkeypatch):
        monkeypatch.setattr(init_wizard, "run", lambda assume_yes=False: 1)
        assert cli.main(["init"]) == 1


class TestServeSetupFlag:
    def test_cli_forwards_the_setup_token(self, monkeypatch):
        calls = []
        monkeypatch.setattr(run, "main", lambda argv=None: calls.append(argv))
        assert cli.main(["serve", "--setup"]) == 0
        assert cli.main(["serve", "tailscale", "--port", "9000", "--setup"]) == 0
        assert calls == [["--setup"], ["tailscale", "9000", "--setup"]]

    def test_setup_runs_the_wizard_before_the_bind(
        self, monkeypatch, uvicorn_spy, quiet_checks
    ):
        order = []
        monkeypatch.setattr(
            init_wizard,
            "run",
            lambda assume_yes=False, serving=False: order.append(("wizard", serving))
            or 0,
        )
        reported = []
        monkeypatch.setattr(
            init_wizard, "report", lambda stream=None, **kw: reported.append(kw)
        )
        import uvicorn

        monkeypatch.setattr(
            uvicorn, "run", lambda app, **kw: order.append("bind") or uvicorn_spy
        )
        monkeypatch.setattr(run, "_is_onboarded", lambda: False)
        run.main(["--setup"])
        # serving=True: the wizard is running inside the process that binds the
        # port next, so it leaves the steps + the address to the banner, which
        # prints them exactly once.
        assert order == [("wizard", True), "bind"]
        assert _eventually(lambda: reported)
        assert [kw["serving"] for kw in reported] == [True]

    def test_a_wizard_that_blows_up_still_serves(
        self, monkeypatch, uvicorn_spy, quiet_checks
    ):
        def _boom(assume_yes=False, serving=False):
            raise RuntimeError("wizard exploded")

        monkeypatch.setattr(init_wizard, "run", _boom)
        monkeypatch.setattr(run, "_is_onboarded", lambda: True)
        run.main(["--setup"])
        assert uvicorn_spy, "a broken setup must not stop the server"


class TestServeBanner:
    """The top of `mindflock serve`: the three steps with step 3 the boxed
    browser address (F3), no duplicated first-run report, no boxed phone URL in
    local mode — one "Phone:" line instead."""

    def test_every_serve_reports_and_never_prompts(
        self, monkeypatch, uvicorn_spy, quiet_checks, no_prompts
    ):
        seen = []
        monkeypatch.setattr(run, "_is_onboarded", lambda: True)
        monkeypatch.setattr(
            init_wizard,
            "report",
            lambda stream=None, **kw: seen.append((kw.get("serving"), kw.get("url"))),
        )
        monkeypatch.setattr(
            init_wizard,
            "run",
            lambda assume_yes=False, serving=False: pytest.fail(
                "serve must not prompt"
            ),
        )
        run.main([])
        assert uvicorn_spy
        assert _eventually(lambda: seen)
        assert seen == [(True, "http://127.0.0.1:8765")]

    def test_the_block_never_delays_the_bind(
        self, monkeypatch, uvicorn_spy, quiet_checks
    ):
        # Even the `run` probes shell out; on the desktop app's own first start
        # (it spawns this server and polls the port) running them before uvicorn
        # binds is time the app spends retrying onto offline.html.
        order = []
        release = threading.Event()

        def _slow_report(stream=None, **kw):
            order.append("report-start")
            release.wait(5.0)
            order.append("report-done")

        import uvicorn

        monkeypatch.setattr(uvicorn, "run", lambda app, **kw: order.append("bind"))
        monkeypatch.setattr(run, "_is_onboarded", lambda: False)
        monkeypatch.setattr(init_wizard, "report", _slow_report)
        run.main([])
        assert "bind" in order, "the bind must not wait on the steps"
        release.set()
        assert _eventually(lambda: "report-done" in order)
        assert order.index("bind") < order.index("report-done")

    def test_the_block_lands_after_the_banner_in_one_piece(
        self, monkeypatch, capsys, uvicorn_spy, quiet_checks
    ):
        # The banner's closing line is racing the preflight thread, so the block
        # is rendered into a buffer and written once — printed line by line it
        # would end up with "Press Ctrl-C to stop." in the middle of it.
        def _report(stream=None, **kw):
            stream.write("Three steps to your first agent:\nsecond line\n")

        monkeypatch.setattr(run, "_is_onboarded", lambda: False)
        monkeypatch.setattr(init_wizard, "report", _report)
        run.main([])
        out = _drain(capsys, "Optional, later")
        assert "Three steps to your first agent:\nsecond line\n" in out
        assert out.index("Press Ctrl-C") < out.index("Three steps")

    def test_the_real_block_on_a_first_serve(self, monkeypatch, capsys, uvicorn_spy):
        from backend.web.core import setup_steps

        _checks(monkeypatch, _GIT_OK, _AGENT_OK, _AUTH_WARN)
        monkeypatch.setattr(run, "_is_onboarded", lambda: False)
        run.main(["9123"])
        out = _drain(capsys, "Optional, later")
        assert "MindFlock is running  ->  http://127.0.0.1:9123" in out
        assert "│ 3. Open http://127.0.0.1:9123 in your browser" in out
        assert "✗ 2. Sign in to your agent" in out
        # Gone: the duplicated first-run report and the misleading folder line.
        assert "First run" not in out and "Managing git repo" not in out
        assert "mindflock new" not in out
        # GET /api/setup shows what the desktop app's log hides.
        assert "Three steps to your first agent:" in setup_steps.first_run_report()

    def test_tailscale_mode_still_gets_the_steps(
        self, monkeypatch, capsys, uvicorn_spy
    ):
        _checks(monkeypatch, *_READY)
        monkeypatch.setattr(run, "_is_onboarded", lambda: True)
        monkeypatch.setattr(run._tailnet_bind, "plan", lambda mode: (["127.0.0.1"], ""))
        run.main(["tailscale"])
        out = _drain(capsys, "in your browser")
        assert "in your browser" in out

    def test_a_later_serve_with_tmux_missing_still_says_so(
        self, monkeypatch, capsys, uvicorn_spy
    ):
        _checks(monkeypatch, _GIT_OK, _TMUX_MISSING)
        monkeypatch.setattr(run, "_is_onboarded", lambda: True)
        run.main([])
        out = _drain(capsys, "Optional, later")
        assert "✗ 1. Install the tools" in out and "missing tmux" in out

    def test_a_set_up_machine_gets_just_the_address(
        self, monkeypatch, capsys, uvicorn_spy, tmp_path
    ):
        _checks(monkeypatch, *_READY)
        settings_mod.update_settings(general={"last_repo_path": str(tmp_path)})
        monkeypatch.setattr(run, "_is_onboarded", lambda: True)
        run.main([])
        out = _drain(capsys, "Optional, later")
        assert "Open http://127.0.0.1:8765 in your browser" in out
        assert "Three steps" not in out

    def test_run_py_degrades_to_silence_not_to_a_second_copy_of_the_text(
        self, monkeypatch, capsys, uvicorn_spy, quiet_checks
    ):
        # report() swallows its own failure (see TestReport), so any fallback
        # wording here could only ever be unreachable copy rotting out of sync
        # with the wizard it duplicated. What the boot path still owes when the
        # hint dies is booting anyway.
        blew_up = []

        def _boom(stream=None, **kw):
            blew_up.append(kw.get("serving"))
            raise RuntimeError("report exploded")

        monkeypatch.setattr(run, "_is_onboarded", lambda: False)
        monkeypatch.setattr(init_wizard, "report", _boom)
        run.main([])
        assert uvicorn_spy, "a broken first-run hint must not stop the server"
        assert _eventually(lambda: blew_up)
        assert blew_up == [True]
        out = capsys.readouterr().out
        assert "Three steps" not in out
        assert "in your browser" not in out


def _canned_plan(choice=""):
    from backend import onboarding

    return onboarding.build_plan(
        {
            "checks": [
                {"id": "tmux", "status": "ok"},
                {
                    "id": "agent-auth",
                    "status": "warn",
                    "provider": "claude",
                    "cmd": "claude",
                },
            ],
            "devices": {"choice": choice},
            "tailscale": {},
            "github": {
                "connected": False,
                "token_url": "https://github.com/settings/tokens/new",
            },
            "repo": {},
        }
    )


class TestFirstRunPlan:
    """`mindflock init` asks "first computer, or join?" BEFORE the agent sign-in
    (joining brings the default agent) — and only when another MindFlock is on
    this tailnet (F12); everyone else gets `mindflock join` in the closing lines."""

    def _wire(self, monkeypatch, tmp_path, *, peer):
        from backend import onboarding

        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        _checks(monkeypatch, _GIT_OK)
        answers = {"choice": ""}
        monkeypatch.setattr(
            init_wizard, "_plan", lambda checks: _canned_plan(answers["choice"])
        )
        monkeypatch.setattr(
            init_wizard, "_mindflock_peer_nearby", lambda timeout=0: peer
        )

        def _set(choice):
            answers["choice"] = choice
            return choice

        monkeypatch.setattr(onboarding, "set_choice", _set)
        order = []

        def _fix(checks, groups=None):
            order.append(("fix", [c.id for c in checks]))
            return checks

        monkeypatch.setattr(cli, "_fix_checks", _fix)
        _candidates(monkeypatch, _entry(tmp_path))
        return order

    def test_asks_first_or_join_before_the_fix_loop_when_a_peer_is_near(
        self, monkeypatch, tmp_path, capsys
    ):
        order = self._wire(monkeypatch, tmp_path, peer=True)
        it = iter(["j", ""])

        def _input(prompt=""):
            order.append(("ask", prompt.strip()[:6]))
            return next(it)

        monkeypatch.setattr("builtins.input", _input)
        assert init_wizard.run() == 0
        assert order[0] == ("ask", "[F]irs")
        assert order[1][0] == "fix"
        out = capsys.readouterr().out
        assert "Another computer on your tailnet runs MindFlock." in out
        assert "mindflock devices join DEVICE CODE" in out

    def test_no_peer_no_question(self, monkeypatch, tmp_path, capsys):
        order = self._wire(monkeypatch, tmp_path, peer=False)
        prompts = []
        monkeypatch.setattr(
            "builtins.input", lambda prompt="": prompts.append(prompt) or ""
        )
        assert init_wizard.run() == 0
        assert not any("[F]irst" in p for p in prompts)
        out = capsys.readouterr().out
        assert "Already use MindFlock elsewhere? mindflock join" in out
        assert order and order[0][0] == "fix"

    def test_joining_holds_the_agent_sign_in_back(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        auth = Check("agent-auth", "agent auth", "warn", "no login", cmd="claude")
        _checks(monkeypatch, _TMUX_MISSING, auth)
        monkeypatch.setattr(init_wizard, "_plan", lambda checks: _canned_plan("join"))
        seen = []
        monkeypatch.setattr(
            cli,
            "_fix_checks",
            lambda cs, groups=None: seen.append([c.id for c in cs]) or cs,
        )
        _candidates(monkeypatch, _entry(tmp_path))
        _answers(monkeypatch, "")
        init_wizard.run()
        assert seen == [["tmux"]]

    def test_assume_yes_never_asks(self, monkeypatch, tmp_path, no_prompts, capsys):
        _checks(monkeypatch, _GIT_OK)
        monkeypatch.setattr(init_wizard, "_plan", lambda checks: _canned_plan())
        monkeypatch.setattr(
            init_wizard,
            "_mindflock_peer_nearby",
            lambda timeout=0: pytest.fail("--yes must not probe for the question"),
        )
        _candidates(monkeypatch, _entry(tmp_path))
        assert init_wizard.run(assume_yes=True) == 0
        assert "✓ 3. Pick a project folder" in capsys.readouterr().out


class TestPeerProbe:
    """The devices question's trigger: one short hello probe per online peer."""

    @pytest.fixture()
    def hello_server(self):
        body = {"app": "mindflock"}

        class _H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 — http.server's name
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        yield srv, body
        srv.shutdown()

    def _peers(self, monkeypatch, bases):
        from backend.web.core import remote

        monkeypatch.setattr(remote, "tailscale_nodes", lambda: (None, [{"ip": "x"}]))
        monkeypatch.setattr(remote, "_candidate_bases", lambda peer: list(bases))

    def test_a_peer_answering_as_mindflock_is_found(self, monkeypatch, hello_server):
        # The autouse fixture stubs the probe for every other test; this one
        # calls the real function directly.
        srv, _body = hello_server
        self._peers(monkeypatch, ["http://127.0.0.1:%d" % srv.server_port])
        probe = TestPeerProbe._real_probe
        assert probe(timeout=2.0) is True

    def test_something_else_answering_is_not(self, monkeypatch, hello_server):
        srv, body = hello_server
        body["app"] = "not-mindflock"
        self._peers(monkeypatch, ["http://127.0.0.1:%d" % srv.server_port])
        assert TestPeerProbe._real_probe(timeout=2.0) is False

    def test_no_tailnet_is_a_quiet_no(self, monkeypatch):
        from backend.web.core import remote

        monkeypatch.setattr(remote, "tailscale_nodes", lambda: (None, []))
        assert TestPeerProbe._real_probe(timeout=0.5) is False


TestPeerProbe._real_probe = staticmethod(init_wizard._mindflock_peer_nearby)
