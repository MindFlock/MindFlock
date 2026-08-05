"""Guided first run (`mindflock init`): the non-interactive report, the wizard's
steps, the non-TTY degradation, the repo picker's three accepted answers, and
the `serve --setup` wiring."""

from __future__ import annotations

import io
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
_GIT_OK = Check("git", "git", "ok", "git version 2.43.0")


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """The wizard WRITES settings (general.last_repo_path), so every test in this
    module must be pointed at a throwaway store — otherwise running the suite
    edits the developer's own ~/.mindflock/settings.json."""
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))


@pytest.fixture()
def no_prompts(monkeypatch):
    """Any call to input() fails the test: the paths that use this fixture claim
    to be non-interactive, and a prompt there is a hang in production."""
    monkeypatch.setattr(
        "builtins.input", lambda prompt="": pytest.fail("must not prompt")
    )


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
    """Wait for something a background thread does. run.py runs its first-run
    report and its dependency checks off the bind path on purpose, so a test that
    asserts on either has to wait rather than assume main() already did it."""
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
    a daemon thread that can outlive the test, and a failing check there writes to
    a stderr capture that has already been torn down."""
    for name in ("check_git", "check_tmux", "check_agent_cli"):
        monkeypatch.setattr(doctor, name, lambda: _GIT_OK)


class TestReport:
    def test_missing_dependency_and_its_fix_are_named(
        self, monkeypatch, capsys, no_prompts
    ):
        monkeypatch.setattr(doctor, "run_checks", lambda: [_GIT_OK, _TMUX_MISSING])
        _candidates(monkeypatch, _entry("/home/me/code/foo"))
        init_wizard.report()
        out = capsys.readouterr().out
        assert "✗" in out
        assert "fix: sudo apt install tmux" in out
        assert "/home/me/code/foo" in out
        assert 'mindflock new /home/me/code/foo -p "' in out
        assert "mindflock init" in out
        # A healthy check is exactly what a first-run summary shouldn't spend a
        # line on — the report is about what needs doing.
        assert "git version 2.43.0" not in out

    def test_healthy_machine_gets_one_line_not_a_table(
        self, monkeypatch, capsys, no_prompts
    ):
        monkeypatch.setattr(doctor, "run_checks", lambda: [_GIT_OK])
        _candidates(monkeypatch, _entry("/home/me/code/foo"))
        init_wizard.report()
        out = capsys.readouterr().out
        assert "Dependencies look good" in out
        assert "✗" not in out and "!" not in out

    def test_doctor_that_raises_still_prints_the_next_commands(
        self, monkeypatch, capsys, no_prompts
    ):
        def _boom():
            raise RuntimeError("doctor blew up")

        monkeypatch.setattr(doctor, "run_checks", _boom)
        _candidates(monkeypatch, _entry("/home/me/code/foo"))
        init_wizard.report()  # must not raise
        out = capsys.readouterr().out
        assert "Dependency check unavailable" in out
        assert "mindflock new /home/me/code/foo" in out

    def test_serving_drops_the_serve_line(self, monkeypatch, capsys, no_prompts):
        monkeypatch.setattr(doctor, "run_checks", lambda: [_GIT_OK])
        _candidates(monkeypatch, _entry("/home/me/code/foo"))
        init_wizard.report()
        assert "mindflock serve" in capsys.readouterr().out
        init_wizard.report(serving=True)
        assert "mindflock serve" not in capsys.readouterr().out

    def test_non_git_folder_is_flagged_not_hidden(
        self, monkeypatch, capsys, no_prompts
    ):
        monkeypatch.setattr(doctor, "run_checks", lambda: [_GIT_OK])
        _candidates(monkeypatch, _entry("/home/me/notes", is_git=False))
        init_wizard.report()
        assert "no git yet" in capsys.readouterr().out

    def test_writes_to_the_given_stream(self, monkeypatch, no_prompts):
        monkeypatch.setattr(doctor, "run_checks", lambda: [_TMUX_MISSING])
        _candidates(monkeypatch, _entry("/home/me/code/foo"))
        buf = io.StringIO()
        init_wizard.report(buf)
        assert "sudo apt install tmux" in buf.getvalue()

    def test_a_broken_stream_never_raises(self, monkeypatch, no_prompts):
        # run.py prints this on the boot path: an unwritable stream must cost the
        # hint, never the server.
        class _Broken:
            def write(self, _text):
                raise OSError("stream is gone")

        monkeypatch.setattr(doctor, "run_checks", lambda: [_TMUX_MISSING])
        _candidates(monkeypatch, _entry("/home/me/code/foo"))
        init_wizard.report(_Broken())

    def test_report_is_the_one_layer_that_degrades(self, monkeypatch):
        # Every caller (run.py's boot path, the non-TTY wizard) relies on this
        # swallow and keeps no second copy of the wording, so a printer that blows
        # up mid-way has to end here.
        def _boom(out, *, serving):
            raise RuntimeError("printer exploded")

        monkeypatch.setattr(init_wizard, "_print_report", _boom)
        init_wizard.report()


class TestSharedDoctorPrinting:
    """The glyph table has exactly one implementation (cli.print_checks), so the
    doctor command and the wizard can never drift into two dialects of it."""

    @pytest.fixture()
    def printed(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            cli, "print_checks", lambda checks, stream=None: calls.append(list(checks))
        )
        return calls

    def test_doctor_command_uses_it(self, monkeypatch, printed):
        monkeypatch.setattr(doctor, "run_checks", lambda: [_GIT_OK])
        assert cli.main(["doctor"]) == 0
        assert printed == [[_GIT_OK]]

    def test_report_uses_it(self, monkeypatch, printed, no_prompts):
        monkeypatch.setattr(doctor, "run_checks", lambda: [_TMUX_MISSING])
        _candidates(monkeypatch, _entry("/home/me/code/foo"))
        init_wizard.report()
        assert printed == [[_TMUX_MISSING]]

    def test_wizard_uses_it(self, monkeypatch, printed, tmp_path, no_prompts):
        monkeypatch.setattr(doctor, "run_checks", lambda: [_GIT_OK])
        _candidates(monkeypatch, _entry(tmp_path))
        assert init_wizard.run(assume_yes=True) == 0
        assert printed == [[_GIT_OK]]

    def test_empty_list_prints_nothing(self, capsys):
        cli.print_checks([])
        assert capsys.readouterr().out == ""


class TestNonTtyDegradation:
    def test_piped_stdin_reports_instead_of_prompting(
        self, monkeypatch, capsys, no_prompts
    ):
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        monkeypatch.setattr(doctor, "run_checks", lambda: [_TMUX_MISSING])
        _candidates(monkeypatch, _entry("/home/me/code/foo"))
        assert init_wizard.run() == 0
        out = capsys.readouterr().out
        assert "First run" in out
        assert "run `mindflock init` in a terminal" in out


class TestServingFlag:
    """`mindflock serve --setup` runs the wizard inside the process that binds the
    port a moment later, so neither of the wizard's two exits may sign off by
    telling the user to start a server."""

    def test_a_non_tty_setup_run_omits_the_start_the_server_line(
        self, monkeypatch, capsys, no_prompts
    ):
        # The desktop app's spawn, an installer script and CI all land here: stdin
        # can't be prompted, so run() degrades to the report — which without the
        # flag prints "mindflock serve  # start the server" one breath before this
        # same process binds it.
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        monkeypatch.setattr(doctor, "run_checks", lambda: [_GIT_OK])
        _candidates(monkeypatch, _entry("/home/me/code/foo"))
        assert init_wizard.run(serving=True) == 0
        out = capsys.readouterr().out
        assert "mindflock serve" not in out
        assert "mindflock new /home/me/code/foo" in out

    def test_a_terminal_setup_run_ends_on_the_session_command(
        self, monkeypatch, capsys, tmp_path, no_prompts
    ):
        monkeypatch.setattr(doctor, "run_checks", lambda: [_GIT_OK])
        _candidates(monkeypatch, _entry(_git_repo(tmp_path / "webapp")))
        assert init_wizard.run(assume_yes=True, serving=True) == 0
        out = capsys.readouterr().out
        assert "You're set. One command:" in out
        assert "mindflock serve" not in out
        assert 'mindflock new %s -p "' % (tmp_path / "webapp") in out

    def test_plain_init_still_names_the_server_command(
        self, monkeypatch, capsys, tmp_path, no_prompts
    ):
        # `mindflock init` has no server behind it, so dropping the serve line
        # there would leave the setup with no way to finish.
        monkeypatch.setattr(doctor, "run_checks", lambda: [_GIT_OK])
        _candidates(monkeypatch, _entry(_git_repo(tmp_path / "webapp")))
        assert init_wizard.run(assume_yes=True) == 0
        out = capsys.readouterr().out
        assert "You're set. Two commands:" in out
        assert "mindflock serve" in out


class TestAssumeYes:
    def test_takes_the_top_suggestion_and_remembers_it(
        self, monkeypatch, capsys, tmp_path, no_prompts
    ):
        monkeypatch.setattr(doctor, "run_checks", lambda: [_GIT_OK])
        repo = _git_repo(tmp_path / "webapp")
        _candidates(monkeypatch, _entry(repo), _entry(tmp_path / "other"))
        patches = []
        monkeypatch.setattr(
            settings_mod, "update_settings", lambda **kw: patches.append(kw)
        )
        assert init_wizard.run(assume_yes=True) == 0
        assert patches == [{"general": {"last_repo_path": str(repo)}}]
        out = capsys.readouterr().out
        assert "Working folder: %s" % repo in out
        assert 'mindflock new %s -p "' % repo in out

    def test_the_choice_survives_into_the_settings_store(
        self, monkeypatch, tmp_path, no_prompts
    ):
        # The round trip through the real store (isolated by
        # MINDFLOCK_SETTINGS_FILE): general.last_repo_path is what the next New
        # Session dialog and the /api/repos/suggest ranking read.
        monkeypatch.setattr(doctor, "run_checks", lambda: [_GIT_OK])
        repo = _git_repo(tmp_path / "webapp")
        _candidates(monkeypatch, _entry(repo))
        assert init_wizard.run(assume_yes=True) == 0
        assert settings_mod.load_settings().general.last_repo_path == str(repo)
        assert init_wizard._remembered_repo() == str(repo)

    def test_never_runs_an_installer_unwatched(
        self, monkeypatch, capsys, tmp_path, no_prompts
    ):
        # --yes means "don't ask me", not "install things nobody is watching":
        # the fix lines are printed and the exit code stays honest.
        monkeypatch.setattr(doctor, "run_checks", lambda: [_TMUX_MISSING])
        monkeypatch.setattr(
            cli, "_fix_checks", lambda checks: pytest.fail("--yes must not run fixes")
        )
        _candidates(monkeypatch, _entry(tmp_path))
        assert init_wizard.run(assume_yes=True) == 1
        out = capsys.readouterr().out
        assert "sudo apt install tmux" in out
        assert "never runs an installer unwatched" in out
        assert "1 required dependency still missing" in out

    def test_a_non_git_folder_is_said_out_loud(
        self, monkeypatch, capsys, tmp_path, no_prompts
    ):
        monkeypatch.setattr(doctor, "run_checks", lambda: [_GIT_OK])
        plain = tmp_path / "notes"
        plain.mkdir()
        _candidates(monkeypatch, _entry(plain, is_git=False, source="cwd"))
        assert init_wizard.run(assume_yes=True) == 0
        out = capsys.readouterr().out
        assert "Not a git repo" in out
        assert "Create a git repo in this folder" in out

    def test_no_candidates_still_finishes_with_a_usable_command(
        self, monkeypatch, capsys, no_prompts
    ):
        monkeypatch.setattr(doctor, "run_checks", lambda: [_GIT_OK])
        _candidates(monkeypatch)
        assert init_wizard.run(assume_yes=True) == 0
        out = capsys.readouterr().out
        assert "No folder picked" in out
        assert "mindflock new /path/to/repo" in out

    def test_an_unwritable_settings_store_is_not_a_setup_failure(
        self, monkeypatch, tmp_path, no_prompts
    ):
        def _boom(**kw):
            raise OSError("read-only file system")

        monkeypatch.setattr(doctor, "run_checks", lambda: [_GIT_OK])
        monkeypatch.setattr(settings_mod, "update_settings", _boom)
        _candidates(monkeypatch, _entry(tmp_path))
        assert init_wizard.run(assume_yes=True) == 0


class TestFixLoopReuse:
    """Installing tmux, installing the agent CLI and logging it in are all doctor
    checks carrying runnable commands, so the wizard hands the whole set to the
    doctor's own fix loop rather than re-implementing any of them."""

    def test_interactive_run_delegates_to_the_doctor_fix_loop(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(doctor, "run_checks", lambda: [_TMUX_MISSING])
        seen = []

        def _fix(checks):
            seen.append(list(checks))
            return [Check("tmux", "tmux", "ok", "tmux 3.4")]

        monkeypatch.setattr(cli, "_fix_checks", _fix)
        _candidates(monkeypatch, _entry(tmp_path))
        _answers(monkeypatch, "")  # Enter at the repo prompt = top suggestion
        # The re-probed 'ok' from the fix loop is what decides the exit code.
        assert init_wizard.run() == 0
        assert seen == [[_TMUX_MISSING]]


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
        monkeypatch.setattr(doctor, "run_checks", lambda: [_GIT_OK])
        _candidates(monkeypatch, _entry(sub))
        assert init_wizard.run(assume_yes=True) == 0
        out = capsys.readouterr().out
        assert "Working folder: %s" % sub in out
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
        monkeypatch.setattr(doctor, "run_checks", lambda: [_GIT_OK])
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

    def test_help_describes_the_guided_setup(self):
        help_text = " ".join(cli._build_parser().format_help().split())
        assert (
            "guided first-run setup: check dependencies, log in your agent CLI, "
            "pick your repo" in help_text
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
        monkeypatch.setattr(init_wizard, "report", lambda *a, **kw: reported.append(kw))
        import uvicorn

        monkeypatch.setattr(
            uvicorn, "run", lambda app, **kw: order.append("bind") or uvicorn_spy
        )
        monkeypatch.setattr(run, "_is_onboarded", lambda: False)
        run.main(["--setup"])
        # serving=True: the wizard is running inside the process that binds the
        # port next, so it must not sign off by telling the user to start one.
        assert order == [("wizard", True), "bind"]
        assert not _eventually(
            lambda: reported, timeout=0.3
        ), "--setup already covered the report"

    def test_a_wizard_that_blows_up_still_serves(
        self, monkeypatch, uvicorn_spy, quiet_checks
    ):
        def _boom(assume_yes=False, serving=False):
            raise RuntimeError("wizard exploded")

        monkeypatch.setattr(init_wizard, "run", _boom)
        monkeypatch.setattr(run, "_is_onboarded", lambda: True)
        run.main(["--setup"])
        assert uvicorn_spy, "a broken setup must not stop the server"


class TestFirstRunBanner:
    def test_first_serve_reports_and_never_prompts(
        self, monkeypatch, uvicorn_spy, quiet_checks, no_prompts
    ):
        seen = []
        monkeypatch.setattr(run, "_is_onboarded", lambda: False)
        monkeypatch.setattr(
            init_wizard,
            "report",
            lambda stream=None, serving=False: seen.append(serving),
        )
        monkeypatch.setattr(
            init_wizard,
            "run",
            lambda assume_yes=False, serving=False: pytest.fail(
                "a first serve must not prompt"
            ),
        )
        run.main([])
        assert uvicorn_spy
        assert _eventually(lambda: seen)
        assert seen == [True]  # serving=True: don't tell them to start what started

    def test_the_report_never_delays_the_bind(
        self, monkeypatch, uvicorn_spy, quiet_checks
    ):
        # The report walks the full doctor and probes git per suggested folder. On
        # the very launch it exists for — the desktop app's own first start, which
        # spawns this server and polls the port — running it before uvicorn binds
        # is time the app spends retrying onto offline.html.
        order = []
        release = threading.Event()

        def _slow_report(stream=None, serving=False):
            order.append("report-start")
            release.wait(5.0)
            order.append("report-done")

        import uvicorn

        monkeypatch.setattr(uvicorn, "run", lambda app, **kw: order.append("bind"))
        monkeypatch.setattr(run, "_is_onboarded", lambda: False)
        monkeypatch.setattr(init_wizard, "report", _slow_report)
        run.main([])
        assert "bind" in order, "the bind must not wait on the first-run report"
        release.set()
        assert _eventually(lambda: "report-done" in order)
        assert order.index("bind") < order.index("report-done")

    def test_the_report_lands_after_the_banner_in_one_piece(
        self, monkeypatch, capsys, uvicorn_spy, quiet_checks
    ):
        # The banner's closing line is racing the preflight thread, so the report
        # is rendered into a buffer and written once — printed line by line it
        # would end up with "Press Ctrl-C to stop." in the middle of it.
        def _report(stream=None, serving=False):
            stream.write("First run — where this machine stands:\nsecond line\n")

        monkeypatch.setattr(run, "_is_onboarded", lambda: False)
        monkeypatch.setattr(init_wizard, "report", _report)
        run.main([])
        out = _drain(capsys, "second line")
        assert "First run — where this machine stands:\nsecond line\n" in out
        assert out.index("Press Ctrl-C") < out.index("First run")

    def test_a_first_run_does_not_walk_the_doctor_twice(self, monkeypatch, uvicorn_spy):
        # The report's own doctor pass already covers git, tmux and the agent CLI
        # with their fix lines, so the short check list must not shell out for the
        # same three a second time on the same boot.
        probed = []
        for name in ("check_git", "check_tmux", "check_agent_cli"):
            monkeypatch.setattr(
                doctor, name, lambda _n=name: probed.append(_n) or _GIT_OK
            )
        reported = []
        monkeypatch.setattr(run, "_is_onboarded", lambda: False)
        monkeypatch.setattr(
            init_wizard, "report", lambda stream=None, serving=False: reported.append(1)
        )
        run.main([])
        assert _eventually(lambda: reported)
        assert not _eventually(lambda: probed, timeout=0.3)

    def test_a_later_serve_still_gets_the_short_check_list(
        self, monkeypatch, capsys, uvicorn_spy
    ):
        # Nothing about moving the report off the bind path may cost the boot its
        # "tmux is missing, here is the command" line.
        monkeypatch.setattr(doctor, "check_git", lambda: _GIT_OK)
        monkeypatch.setattr(doctor, "check_tmux", lambda: _TMUX_MISSING)
        monkeypatch.setattr(doctor, "check_agent_cli", lambda: _GIT_OK)
        reported = []
        monkeypatch.setattr(run, "_is_onboarded", lambda: True)
        monkeypatch.setattr(init_wizard, "report", lambda *a, **kw: reported.append(kw))
        run.main([])
        err = _drain(capsys, "fix: sudo apt install tmux", err=True)
        assert "! tmux:" in err
        assert not reported, "only a first run gets the report"

    def test_second_serve_says_nothing(self, monkeypatch, uvicorn_spy, quiet_checks):
        reported = []
        monkeypatch.setattr(run, "_is_onboarded", lambda: True)
        monkeypatch.setattr(init_wizard, "report", lambda *a, **kw: reported.append(kw))
        run.main([])
        assert uvicorn_spy
        assert not _eventually(
            lambda: reported, timeout=0.3
        ), "only a first run gets the report"

    def test_run_py_degrades_to_silence_not_to_a_second_copy_of_the_text(
        self, monkeypatch, capsys, uvicorn_spy, quiet_checks
    ):
        # report() swallows its own failure (see TestReport), so any fallback
        # wording here could only ever be unreachable copy rotting out of sync
        # with the wizard it duplicated. What the boot path still owes when the
        # hint dies is booting anyway.
        blew_up = []

        def _boom(stream=None, serving=False):
            blew_up.append(serving)
            raise RuntimeError("report exploded")

        monkeypatch.setattr(run, "_is_onboarded", lambda: False)
        monkeypatch.setattr(init_wizard, "report", _boom)
        run.main([])
        assert uvicorn_spy, "a broken first-run hint must not stop the server"
        assert _eventually(lambda: blew_up)
        assert blew_up == [True]
        out = capsys.readouterr().out
        assert "Three steps" not in out
        assert "click" not in out
