"""Console entry point (C3) + launcher guards (C4): parsing, delegation, doctor
output/exit codes, missing-web-deps hint, and the source-repo CWD guard."""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import types
import urllib.error

import pytest

import backend.web.run as run
from backend import cli, client, doctor
from backend.doctor import Check


class TestServeParsing:
    @pytest.fixture()
    def serve_calls(self, monkeypatch):
        calls = []
        monkeypatch.setattr(run, "main", lambda argv=None: calls.append(argv))
        return calls

    def test_bare_invocation_serves_with_defaults(self, serve_calls):
        assert cli.main([]) == 0
        assert serve_calls == [[]]

    def test_serve_mode_and_port_forwarded(self, serve_calls):
        assert cli.main(["serve", "local", "--port", "9000"]) == 0
        assert serve_calls == [["local", "9000"]]

    def test_serve_tailscale(self, serve_calls):
        cli.main(["serve", "tailscale"])
        assert serve_calls == [["tailscale"]]

    def test_serve_rejects_unknown_mode(self, serve_calls):
        with pytest.raises(SystemExit):
            cli.main(["serve", "bogus"])
        assert serve_calls == []


class TestDoctorCommand:
    def test_exit_zero_when_only_warns(self, monkeypatch, capsys):
        monkeypatch.setattr(
            doctor,
            "run_checks",
            lambda: [
                Check("git", "git", "ok", "git version 2.43.0"),
                Check(
                    "uv", "uv", "warn", "not found", "curl https://astral.sh/uv | sh"
                ),
            ],
        )
        # uv is an optional row: folded into one line by default, listed (with
        # its fix) under --all.
        assert cli.main(["doctor"]) == 0
        out = capsys.readouterr().out
        assert "Optional: 1 check (1 needs attention) — mindflock doctor --all" in out
        assert "astral.sh" not in out
        assert cli.main(["doctor", "--all"]) == 0
        out = capsys.readouterr().out
        assert "✓" in out and "!" in out
        assert "fix: curl https://astral.sh/uv | sh" in out

    def test_exit_one_and_fix_shown_on_failure(self, monkeypatch, capsys):
        monkeypatch.setattr(
            doctor,
            "run_checks",
            lambda: [
                Check(
                    "tmux", "tmux", "fail", "not found on PATH", "sudo apt install tmux"
                )
            ],
        )
        assert cli.main(["doctor"]) == 1
        out = capsys.readouterr().out
        assert "✗" in out
        assert "fix: sudo apt install tmux" in out

    def test_missing_gh_prints_info_not_a_failure(self, monkeypatch, capsys):
        # gh is optional: absent, it must print the `-` info glyph, keep exit 0
        # and never render as ✗. The installer runs this command and honours the
        # exit code, so a ✗ here would make gh a de-facto requirement for people
        # who only ever `git push` over their own SSH remote.
        monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
        monkeypatch.setattr(doctor, "run_checks", lambda: [doctor.CHECKS_BY_ID["gh"]()])
        assert cli.main(["doctor"]) == 0
        out = capsys.readouterr().out
        rows = out.split("\n", 1)[1]  # past the title line's glyph legend
        assert "✗" not in rows
        assert "- GitHub CLI (gh)" in out
        assert "pushing uses plain git" in out
        assert "look good" not in out
        assert out.rstrip().splitlines()[-1].startswith("Next: ")

    def test_help_does_not_list_gh_as_required(self):
        # The subcommand help is the first place anyone learns what MindFlock
        # needs; gh belongs in the optional parenthetical, not the required list.
        # Collapse whitespace: argparse wraps the subcommand help column.
        help_text = " ".join(cli._build_parser().format_help().split())
        doctor_line = help_text.split("doctor ", 1)[1].split("Sessions:", 1)[0]
        assert "git, tmux, your agent and its sign-in" in doctor_line
        assert "gh" not in doctor_line


class TestDoctorFix:
    _FAIL = Check(
        "tmux",
        "tmux",
        "fail",
        "not found on PATH",
        "sudo apt install tmux",
        cmd="sudo apt install tmux",
    )

    @pytest.fixture()
    def tty_stdin(self, monkeypatch):
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    def test_accepted_fix_runs_command_and_rechecks(
        self, monkeypatch, capsys, tty_stdin
    ):
        monkeypatch.setattr(doctor, "run_checks", lambda: [self._FAIL])
        ran = []
        monkeypatch.setattr(
            cli.subprocess,
            "run",
            lambda cmd, shell: (
                ran.append((cmd, shell)),
                type("P", (), {"returncode": 0})(),
            )[1],
        )
        monkeypatch.setattr(
            doctor,
            "CHECKS_BY_ID",
            {"tmux": lambda: Check("tmux", "tmux", "ok", "tmux 3.4")},
        )
        monkeypatch.setattr("builtins.input", lambda prompt="": "y")
        assert cli.main(["doctor", "--fix"]) == 0
        assert ran == [("sudo apt install tmux", True)]
        out = capsys.readouterr().out
        assert "tmux 3.4" in out
        assert "look good" not in out
        assert out.rstrip().splitlines()[-1].startswith("Next: ")

    def test_declined_fix_runs_nothing_and_keeps_exit_one(
        self, monkeypatch, capsys, tty_stdin
    ):
        monkeypatch.setattr(doctor, "run_checks", lambda: [self._FAIL])
        monkeypatch.setattr(
            cli.subprocess,
            "run",
            lambda *a, **k: pytest.fail("declined fix must not run"),
        )
        monkeypatch.setattr("builtins.input", lambda prompt="": "n")
        assert cli.main(["doctor", "--fix"]) == 1
        assert "skipped" in capsys.readouterr().out

    def test_non_tty_prints_hint_instead_of_prompting(self, monkeypatch, capsys):
        monkeypatch.setattr(doctor, "run_checks", lambda: [self._FAIL])
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        monkeypatch.setattr(
            cli.subprocess,
            "run",
            lambda *a, **k: pytest.fail("non-tty must not run fixes"),
        )
        assert cli.main(["doctor", "--fix"]) == 1
        assert "interactive terminal" in capsys.readouterr().out

    def test_everything_missing_installs_after_one_question(
        self, monkeypatch, capsys, tty_stdin
    ):
        # Two missing tools, ONE prompt, ONE script run — the old loop asked
        # (and ran sudo apt) once per tool. The optional sandbox (bwrap) stays
        # out of it: no optional extra rides along on Enter = yes (F4).
        missing = [
            Check("tmux", "tmux", "fail", cmd="x", pkg="tmux", install=True),
            Check("bwrap", "sandbox", "warn", cmd="x", pkg="bubblewrap", install=True),
            Check(
                "agent-cli",
                "agent CLI (codex)",
                "fail",
                cmd="npm install -g @openai/codex",
                install=True,
            ),
        ]
        healthy = [
            Check("tmux", "tmux", "ok", "tmux 3.4"),
            Check("bwrap", "sandbox", "ok", "/usr/bin/bwrap"),
            Check("agent-cli", "agent CLI (codex)", "ok", "/usr/bin/codex"),
        ]
        runs = iter([missing, healthy])
        monkeypatch.setattr(doctor, "run_checks", lambda: next(runs))
        monkeypatch.setattr(doctor, "_linux_pkg_manager", lambda: "apt")
        monkeypatch.setattr(doctor.osenv, "os_kind", lambda: "linux")
        ran = []
        monkeypatch.setattr(
            cli.subprocess,
            "run",
            lambda cmd, shell: (ran.append(cmd), type("P", (), {"returncode": 0})())[1],
        )
        prompts = []
        monkeypatch.setattr(
            "builtins.input", lambda prompt="": (prompts.append(prompt), "")[1]
        )
        assert cli.main(["doctor", "--fix"]) == 0
        assert len(prompts) == 1 and "Install all 2?" in prompts[0]
        assert len(ran) == 1
        assert "sudo apt-get install -y tmux" in ran[0]
        assert "bubblewrap" not in ran[0]
        assert "npm install -g @openai/codex" in ran[0]
        out = capsys.readouterr().out
        assert "/usr/bin/codex" in out
        assert "look good" not in out

    def test_declining_the_install_runs_nothing(self, monkeypatch, capsys, tty_stdin):
        missing = [Check("tmux", "tmux", "fail", cmd="x", pkg="tmux", install=True)]
        monkeypatch.setattr(doctor, "run_checks", lambda: missing)
        monkeypatch.setattr(
            cli.subprocess,
            "run",
            lambda *a, **k: pytest.fail("declined — must not run"),
        )
        monkeypatch.setattr("builtins.input", lambda prompt="": "n")
        assert cli.main(["doctor", "--fix"]) == 1
        assert "skipped" in capsys.readouterr().out

    def test_logins_are_offered_after_the_install(self, monkeypatch, capsys, tty_stdin):
        # Installing an agent CLI is what makes its login check appear — so
        # the per-item login prompts read the RE-PROBED checks.
        before = [
            Check(
                "agent-cli",
                "agent CLI (codex)",
                "fail",
                cmd="npm i codex",
                install=True,
            ),
        ]
        after = [
            Check("agent-cli", "agent CLI (codex)", "ok", "/usr/bin/codex"),
            Check("agent-auth", "agent auth (codex)", "warn", cmd="codex login"),
        ]
        runs = iter([before, after])
        monkeypatch.setattr(doctor, "run_checks", lambda: next(runs))
        monkeypatch.setattr(
            doctor,
            "CHECKS_BY_ID",
            {
                "agent-auth": lambda: Check(
                    "agent-auth", "agent auth (codex)", "ok", "ok"
                )
            },
        )
        ran = []
        monkeypatch.setattr(
            cli.subprocess,
            "run",
            lambda cmd, shell: (ran.append(cmd), type("P", (), {"returncode": 0})())[1],
        )
        monkeypatch.setattr("builtins.input", lambda prompt="": "y")
        assert cli.main(["doctor", "--fix"]) == 0
        assert len(ran) == 2
        assert "npm i codex" in ran[0]
        assert ran[1] == "codex login"

    def test_a_per_provider_login_gets_the_hint_and_a_reprobe(
        self, monkeypatch, capsys, tty_stdin
    ):
        # "agent-auth-<name>" rows (a CLI some other setting launches) get the
        # same "/exit to come back" hint as the default's, and are re-probed
        # one CLI at a time — CHECKS_BY_ID has no entry for them.
        row = Check(
            "agent-auth-codex", "agent auth (codex — PR review)", "warn", cmd="codex"
        )
        monkeypatch.setattr(doctor, "run_checks", lambda: [row])
        probed = []

        def _probe(cid, name, label):
            probed.append((cid, name, label))
            return Check(cid, label, "ok", "signed in")

        monkeypatch.setattr(doctor, "_agent_auth_check", _probe)
        monkeypatch.setattr(
            cli.subprocess,
            "run",
            lambda cmd, shell: type("P", (), {"returncode": 0})(),
        )
        monkeypatch.setattr("builtins.input", lambda prompt="": "y")
        # Another CLI's sign-in is an optional row: offered under --all only.
        cli.main(["doctor", "--fix", "--all"])
        out = capsys.readouterr().out
        assert "to come back here" in out
        assert probed == [("agent-auth-codex", "codex", row.label)]
        assert "signed in" in out

    def test_checks_without_cmd_are_not_offered(self, monkeypatch, capsys, tty_stdin):
        monkeypatch.setattr(
            doctor,
            "run_checks",
            lambda: [Check("agent-cli", "agent CLI", "fail", "gone", "see Settings")],
        )
        monkeypatch.setattr(
            "builtins.input",
            lambda prompt="": pytest.fail("no runnable cmd — must not prompt"),
        )
        assert cli.main(["doctor", "--fix"]) == 1

    def test_eof_at_prompt_stops_the_loop(self, monkeypatch, capsys, tty_stdin):
        # A closed stdin (EOF) mid-loop aborts the remaining prompts cleanly.
        second = Check(
            "gh", "gh", "warn", "no auth", "gh auth login", cmd="gh auth login"
        )
        monkeypatch.setattr(doctor, "run_checks", lambda: [self._FAIL, second])

        def _eof(prompt=""):
            raise EOFError

        monkeypatch.setattr("builtins.input", _eof)
        monkeypatch.setattr(
            cli.subprocess,
            "run",
            lambda *a, **k: pytest.fail("EOF must stop before running anything"),
        )
        assert cli.main(["doctor", "--fix"]) == 1  # tmux still failing

    def test_command_nonzero_and_still_unhealthy_are_reported(
        self, monkeypatch, capsys, tty_stdin
    ):
        monkeypatch.setattr(doctor, "run_checks", lambda: [self._FAIL])
        monkeypatch.setattr(
            cli.subprocess,
            "run",
            lambda cmd, shell: type("P", (), {"returncode": 1})(),
        )
        # Re-probe still finds tmux broken (common when PATH needs a new shell).
        still = Check("tmux", "tmux", "fail", "still missing", docs="https://tmux.io")
        monkeypatch.setattr(doctor, "CHECKS_BY_ID", {"tmux": lambda: still})
        monkeypatch.setattr("builtins.input", lambda prompt="": "y")
        assert cli.main(["doctor", "--fix"]) == 1
        out = capsys.readouterr().out
        assert "command exited 1" in out
        assert "still not healthy" in out
        assert "https://tmux.io" in out  # docs link appended when present

    def test_recheck_missing_id_still_runs_but_skips_reprobe(
        self, monkeypatch, capsys, tty_stdin
    ):
        check = Check("mystery", "mystery", "fail", "gone", "do-thing", cmd="do-thing")
        monkeypatch.setattr(doctor, "run_checks", lambda: [check])
        ran = []
        monkeypatch.setattr(
            cli.subprocess,
            "run",
            lambda cmd, shell: (
                ran.append(cmd),
                type("P", (), {"returncode": 0})(),
            )[1],
        )
        monkeypatch.setattr(doctor, "CHECKS_BY_ID", {})  # id absent → no re-probe
        monkeypatch.setattr("builtins.input", lambda prompt="": "y")
        # An unknown id is an optional row: only `--fix --all` offers it.
        assert cli.main(["doctor", "--fix", "--all"]) == 1  # original fail unchanged
        assert ran == ["do-thing"]  # the fix still ran
        assert "still not healthy" not in capsys.readouterr().out

    def test_reprobe_exception_is_swallowed(self, monkeypatch, capsys, tty_stdin):
        monkeypatch.setattr(doctor, "run_checks", lambda: [self._FAIL])
        monkeypatch.setattr(
            cli.subprocess,
            "run",
            lambda cmd, shell: type("P", (), {"returncode": 0})(),
        )

        def _broken():
            raise RuntimeError("re-probe blew up")

        monkeypatch.setattr(doctor, "CHECKS_BY_ID", {"tmux": _broken})
        monkeypatch.setattr("builtins.input", lambda prompt="": "")  # Enter = yes
        # A broken re-probe must not kill the loop or the command.
        assert cli.main(["doctor", "--fix"]) == 1
        assert "still not healthy" not in capsys.readouterr().out


class TestDoctorGroupsAndSummary:
    """F2 + F6: headings, a legend, optional rows folded, and ONE next-action
    line that counts the agent's sign-in as required."""

    _ROWS = [
        Check("git", "git", "ok", "git version 2.43.0"),
        Check("tmux", "tmux", "ok", "tmux 3.4"),
        Check(
            "agent-cli",
            "agent CLI (claude)",
            "ok",
            "/usr/bin/claude",
            provider="claude",
        ),
        Check(
            "agent-auth",
            "agent auth (claude)",
            "warn",
            "CLI is installed but no sign of a login was found",
            "run `claude` once to log in",
            cmd="claude",
            provider="claude",
        ),
        Check(
            "git-identity", "git identity", "warn", "user.name not set", "git config …"
        ),
        Check("tailscale", "tailscale", "warn", "on Windows only", "curl … | sh"),
        Check("tailnet-mtu", "tailnet MTU", "warn", "too small", "sudo ip link …"),
        Check("bwrap", "peer sandbox (bubblewrap)", "ok", "/usr/bin/bwrap"),
        Check("cache-seeds", "warm cache seeds", "info", "none configured"),
    ]

    def test_grouped_with_a_legend_and_optional_folded(self, monkeypatch, capsys):
        monkeypatch.setattr(doctor, "run_checks", lambda *a: list(self._ROWS))
        assert cli.main(["doctor"]) == 0
        out = capsys.readouterr().out
        first = out.splitlines()[0]
        assert first.startswith("MindFlock doctor")
        for glyph in ("✓", "!", "✗", "-"):
            assert glyph in first
        run_at = out.index("Needed to run an agent")
        ship_at = out.index("Needed to ship")
        assert run_at < out.index("  ✓ tmux") < ship_at < out.index("git identity")
        assert "Optional: 4 checks (2 need attention) — mindflock doctor --all" in out
        # Folded means folded: no MTU sermon, no sudo line on a first look.
        assert "tailnet MTU" not in out and "sudo ip link" not in out
        assert "agent sign-in (claude)" in out
        assert "fix: run claude, sign in, then type /exit" in out

    def test_never_says_all_good_while_the_agent_is_signed_out(
        self, monkeypatch, capsys
    ):
        monkeypatch.setattr(doctor, "run_checks", lambda *a: list(self._ROWS))
        cli.main(["doctor"])
        lines = capsys.readouterr().out.rstrip().splitlines()
        assert "look good" not in "\n".join(lines)
        assert lines[-1] == (
            "Next: sign in to Claude Code — run claude, sign in, then type /exit"
        )
        assert sum(1 for line in lines if line.startswith("Next:")) == 1

    def test_missing_tools_name_doctor_fix_with_the_count(self, monkeypatch, capsys):
        rows = [
            Check("git", "git", "ok"),
            Check("tmux", "tmux", "fail", "not found", pkg="tmux", install=True),
            Check(
                "agent-cli",
                "agent CLI (claude)",
                "fail",
                "not found",
                cmd="curl … | bash",
                install=True,
                provider="claude",
            ),
            Check("agent-auth", "agent auth (claude)", "warn", provider="claude"),
        ]
        monkeypatch.setattr(doctor, "run_checks", lambda *a: list(rows))
        assert cli.main(["doctor"]) == 1
        last = capsys.readouterr().out.rstrip().splitlines()[-1]
        assert last == (
            "Next: mindflock doctor --fix installs the 2 missing tools (tmux, Claude Code)"
        )

    def test_all_lists_the_optional_rows_under_their_heading(self, monkeypatch, capsys):
        monkeypatch.setattr(doctor, "run_checks", lambda *a: list(self._ROWS))
        cli.main(["doctor", "--all"])
        out = capsys.readouterr().out
        assert "\nOptional\n" in out
        assert "tailnet MTU" in out and "fix: sudo ip link …" in out
        assert "doctor --all" not in out

    def test_a_failing_optional_row_is_never_folded_away(self, monkeypatch, capsys):
        rows = self._ROWS + [
            Check(
                "local-model",
                "local model (ollama)",
                "fail",
                "not running",
                "ollama serve",
            )
        ]
        monkeypatch.setattr(doctor, "run_checks", lambda *a: rows)
        cli.main(["doctor"])
        out = capsys.readouterr().out
        assert "✗ local model (ollama)" in out
        assert "Optional: 4 more checks" in out


class TestFixLoopGroups:
    """F4: optional extras never ride along on Enter = yes."""

    @pytest.fixture()
    def tty_stdin(self, monkeypatch):
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    _TS = Check(
        "tailscale",
        "tailscale",
        "warn",
        "on Windows only",
        cmd="curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up",
    )
    _GH = Check("gh", "GitHub CLI (gh)", "warn", "not signed in", cmd="gh auth login")

    def test_plain_fix_never_offers_an_optional_extra(
        self, monkeypatch, capsys, tty_stdin
    ):
        monkeypatch.setattr(doctor, "run_checks", lambda *a: [self._TS])
        monkeypatch.setattr(
            "builtins.input", lambda prompt="": pytest.fail("must not offer tailscale")
        )
        cli.main(["doctor", "--fix"])

    def test_fix_all_offers_it_with_enter_meaning_no(
        self, monkeypatch, capsys, tty_stdin
    ):
        monkeypatch.setattr(doctor, "run_checks", lambda *a: [self._TS])
        prompts = []
        monkeypatch.setattr(
            "builtins.input", lambda prompt="": prompts.append(prompt) or ""
        )
        monkeypatch.setattr(
            cli.subprocess, "run", lambda *a, **k: pytest.fail("Enter must not install")
        )
        cli.main(["doctor", "--fix", "--all"])
        assert len(prompts) == 1 and prompts[0].rstrip().endswith("[y/N]")
        assert "skipped" in capsys.readouterr().out

    def test_a_ship_row_asks_with_enter_meaning_no(
        self, monkeypatch, capsys, tty_stdin
    ):
        monkeypatch.setattr(doctor, "run_checks", lambda *a: [self._GH])
        prompts = []
        monkeypatch.setattr(
            "builtins.input", lambda prompt="": prompts.append(prompt) or ""
        )
        cli.main(["doctor", "--fix"])
        assert prompts and prompts[0].rstrip().endswith("[y/N]")

    def test_the_init_loop_is_the_run_group_only(self, monkeypatch, capsys, tty_stdin):
        prompts = []
        monkeypatch.setattr(
            "builtins.input", lambda prompt="": prompts.append(prompt) or "n"
        )
        cli._fix_checks([self._TS, self._GH], groups=("run",))
        assert prompts == []


class TestAbandonedSignIn:
    """F11: an agent closed before signing in gets [R]etry / [s]kip, not a PATH
    diagnosis."""

    _AUTH = Check(
        "agent-auth",
        "agent auth (claude)",
        "warn",
        "no sign of a login",
        "run `claude` once to log in",
        cmd="claude",
        provider="claude",
    )

    @pytest.fixture()
    def wired(self, monkeypatch):
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(doctor, "run_checks", lambda *a: [self._AUTH])
        ran = []
        monkeypatch.setattr(
            cli.subprocess,
            "run",
            lambda cmd, shell: (ran.append(cmd), type("P", (), {"returncode": 143})())[
                1
            ],
        )
        return ran

    def test_skip_says_how_to_sign_in_later(self, monkeypatch, capsys, wired):
        probes = iter([self._AUTH])
        monkeypatch.setattr(
            doctor, "CHECKS_BY_ID", {"agent-auth": lambda: next(probes)}
        )
        answers = iter(["", "s"])  # Enter = run claude; then skip
        prompts = []
        monkeypatch.setattr(
            "builtins.input", lambda prompt="": prompts.append(prompt) or next(answers)
        )
        cli.main(["doctor", "--fix"])
        out = capsys.readouterr().out
        assert wired == ["claude"]
        assert (
            "Claude Code closed before you signed in. [R]etry / [s]kip:" in prompts[1]
        )
        assert "still not healthy" not in out and "new shell" not in out
        assert "command exited" not in out
        assert "skipped — sign in later: run claude, sign in, then type /exit" in out

    def test_retry_runs_it_again_and_a_sign_in_ticks(self, monkeypatch, capsys, wired):
        ok = Check(
            "agent-auth", "agent auth (claude)", "ok", "signed in", provider="claude"
        )
        probes = iter([self._AUTH, ok])
        monkeypatch.setattr(
            doctor, "CHECKS_BY_ID", {"agent-auth": lambda: next(probes)}
        )
        answers = iter(["", "r"])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
        cli.main(["doctor", "--fix"])
        out = capsys.readouterr().out
        assert wired == ["claude", "claude"]
        assert "✓ signed in to Claude Code" in out


class TestHelpLayout:
    """F10: grouped commands, no ==SUPPRESS== row, a start-here epilog."""

    def test_groups_in_order_and_the_epilog(self):
        text = cli._build_parser().format_help()
        heads = ["Get started:", "Sessions:", "This computer:", "Advanced:"]
        idx = [text.index(h) for h in heads]
        assert idx == sorted(idx)
        assert text.rstrip().endswith("New here? Run mindflock init")
        started = text.split("Get started:", 1)[1].split("Sessions:", 1)[0]
        assert [
            ln.split()[0] for ln in started.strip().splitlines() if ln[:3].strip()
        ] == [
            "init",
            "serve",
            "doctor",
        ]

    def test_git_credential_is_hidden_everywhere(self):
        text = cli._build_parser().format_help()
        assert "SUPPRESS" not in text
        assert "git-credential" not in text
        assert "{" not in text.split("\n", 1)[0]  # no brace list in the usage

    def test_every_visible_command_is_listed_once(self):
        parser = cli._build_parser()
        text = parser.format_help()
        sub = parser._subparsers._group_actions[0]
        for name in sub.choices:
            if name == "git-credential":
                continue
            rows = [ln for ln in text.splitlines() if ln.startswith("  %s " % name)]
            assert len(rows) == 1, name

    def test_git_credential_still_works(self, monkeypatch):
        seen = []
        monkeypatch.setattr(cli, "_cmd_git_credential", lambda op: seen.append(op) or 0)
        assert cli.main(["git-credential", "get"]) == 0
        assert seen == ["get"]


class TestWebDepsGuard:
    def test_missing_uvicorn_prints_hint_not_traceback(self, monkeypatch, capsys):
        class _Block:
            def find_spec(self, name, path=None, target=None):
                if name == "uvicorn":
                    raise ModuleNotFoundError(
                        f"No module named '{name}'", name="uvicorn"
                    )
                return None

        monkeypatch.delitem(sys.modules, "uvicorn", raising=False)
        monkeypatch.setattr(sys, "meta_path", [_Block()] + sys.meta_path)
        with pytest.raises(SystemExit) as exc:
            run.main([])
        assert exc.value.code == 1
        assert "uv sync --group web" in capsys.readouterr().err


class TestLocalModeEnvExport:
    """F7: run.main exports the resolved mode so the server's startup banner
    can suppress the tailnet URLs + QR that a 127.0.0.1 bind can't serve."""

    @pytest.fixture()
    def uvicorn_spy(self, monkeypatch):
        import uvicorn

        calls = []
        monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append(kw))
        # The double-launch guard probes the real port — a dev machine with a
        # live server on 8765 would short-circuit main() before uvicorn.run.
        monkeypatch.setattr(run, "_port_squatter", lambda host, port: "")
        # main() also starts a preflight *daemon thread* (the first-run report /
        # the short check list). Left alone it walks this machine for real: the
        # settings store is a fresh tmp file, so every test here looks like a
        # first run, and the report shells out for the whole doctor and scans the
        # developer's real home for repo suggestions — then prints the banner to
        # a stdout capture that has already been torn down. Give the thread
        # nothing real to do (same isolation as tests/unit/test_init_wizard.py).
        monkeypatch.setattr(run, "_is_onboarded", lambda: True)
        _ok = Check(id="stub", label="stub", status="ok")
        for _name in ("check_git", "check_tmux", "check_agent_cli"):
            monkeypatch.setattr(doctor, _name, lambda: _ok)
        return calls

    def test_default_mode_is_local_bound_loopback(self, uvicorn_spy, monkeypatch):
        """Security default: a bare launch binds 127.0.0.1 — exposure beyond
        this machine (tailscale mode, 0.0.0.0) must be an explicit opt-in."""
        import os

        monkeypatch.setenv("CS_WEB_MODE", "")
        monkeypatch.setenv("UVICORN_PORT", "")
        run.main([])
        assert os.environ.get("CS_WEB_MODE") == "local"
        assert uvicorn_spy[0]["host"] == "127.0.0.1"

    def test_local_mode_exported_and_bound_loopback(self, uvicorn_spy, monkeypatch):
        import os

        # setenv("") registers a restore, so run.main's exports can't leak into
        # other tests ("" is falsy, so the defaults still apply).
        monkeypatch.setenv("CS_WEB_MODE", "")
        monkeypatch.setenv("UVICORN_PORT", "")
        run.main(["local", "9123"])
        assert os.environ.get("CS_WEB_MODE") == "local"
        assert uvicorn_spy[0]["host"] == "127.0.0.1"
        assert uvicorn_spy[0]["port"] == 9123

    def test_cli_arg_overrides_env_mode(self, uvicorn_spy, monkeypatch):
        import os

        monkeypatch.setenv("CS_WEB_MODE", "local")
        monkeypatch.setenv("UVICORN_PORT", "")
        run.main(["tailscale"])
        assert os.environ.get("CS_WEB_MODE") == "tailscale"
        assert uvicorn_spy[0]["host"] == "0.0.0.0"


class TestSourceRepoGuard:
    def test_detected_by_package_layout(self, tmp_path):
        (tmp_path / "src" / "mindflock").mkdir(parents=True)
        assert run._is_mindflock_source_repo(tmp_path) is True

    def test_detected_by_pyproject_name(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "mindflock"\n')
        assert run._is_mindflock_source_repo(tmp_path) is True

    def test_other_project_not_flagged(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "someapp"\n')
        assert run._is_mindflock_source_repo(tmp_path) is False

    def test_empty_dir_not_flagged(self, tmp_path):
        assert run._is_mindflock_source_repo(tmp_path) is False


# --------------------------------------------------------------------------- #
# J1 — terminal-first session commands (new/ls/attach/open/events)
# --------------------------------------------------------------------------- #
def _fake_response(payload, code=200):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(payload).encode()

    return _Resp()


class TestClientProbe:
    def test_mindflock_fingerprint_accepted(self, monkeypatch):
        # Fingerprint = default_program + caps (repo_root was dropped from
        # /api/config in the 2026-07 legacy cleanup).
        cfg = {"default_program": "claude", "caps": {"git": True}, "home": "/h"}
        monkeypatch.setattr(
            "urllib.request.urlopen", lambda req, timeout=None: _fake_response(cfg)
        )
        assert client.probe("http://127.0.0.1:8765") == cfg

    def test_non_mindflock_service_rejected(self, monkeypatch):
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda req, timeout=None: _fake_response({"hello": "world"}),
        )
        assert client.probe("http://127.0.0.1:8765") is None

    def test_dead_port_is_none(self, monkeypatch):
        def _boom(req, timeout=None):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr("urllib.request.urlopen", _boom)
        assert client.probe("http://127.0.0.1:8765") is None

    def test_http_error_body_surfaced_as_api_error(self, monkeypatch):
        err = urllib.error.HTTPError(
            "http://x",
            409,
            "Conflict",
            {},
            io.BytesIO(b'{"error": "instance foo already exists"}'),
        )

        def _boom(req, timeout=None):
            raise err

        monkeypatch.setattr("urllib.request.urlopen", _boom)
        with pytest.raises(client.ApiError) as exc:
            client.post("http://127.0.0.1:8765", "/api/instances", {"title": "foo"})
        assert exc.value.status == 409
        assert "already exists" in exc.value.message

    def test_non_json_error_body_falls_back_to_status_reason(self, monkeypatch):
        # A proxy/500 page that isn't the usual {"error": ...} JSON: the message
        # degrades to "<code> <reason>" instead of leaking the HTML body.
        err = urllib.error.HTTPError(
            "http://x",
            500,
            "Internal Server Error",
            {},
            io.BytesIO(b"<html>Internal Server Error</html>"),
        )

        def _boom(req, timeout=None):
            raise err

        monkeypatch.setattr("urllib.request.urlopen", _boom)
        with pytest.raises(client.ApiError) as exc:
            client.get("http://127.0.0.1:8765", "/api/instances")
        assert exc.value.status == 500
        assert exc.value.message == "500 Internal Server Error"


class TestServerDiscovery:
    @pytest.fixture()
    def probed(self, monkeypatch):
        """Record probed base URLs; answer with the MindFlock config shape."""
        calls = []

        def _probe(base, timeout=1.0):
            calls.append(base)
            return {"default_program": "claude", "repo_root": "/x"}

        monkeypatch.setattr(client, "probe", _probe)
        return calls

    def test_explicit_flags_win_over_env(self, probed, monkeypatch):
        monkeypatch.setenv("MINDFLOCK_PORT", "9999")
        assert client.discover("example.com", 9000) == "http://example.com:9000"
        assert probed == ["http://example.com:9000"]

    def test_env_port_used(self, probed, monkeypatch):
        monkeypatch.setenv("MINDFLOCK_PORT", "9001")
        monkeypatch.delenv("MINDFLOCK_HOST", raising=False)
        assert client.discover() == "http://127.0.0.1:9001"

    def test_env_host_used(self, probed, monkeypatch):
        monkeypatch.setenv("MINDFLOCK_HOST", "example.com")
        monkeypatch.delenv("MINDFLOCK_PORT", raising=False)
        assert client.discover() == "http://example.com:8765"
        assert probed == ["http://example.com:8765"]

    def test_default_probe(self, probed, monkeypatch):
        monkeypatch.delenv("MINDFLOCK_PORT", raising=False)
        monkeypatch.delenv("MINDFLOCK_HOST", raising=False)
        assert client.discover() == "http://127.0.0.1:8765"

    def test_no_server_raises_with_hint(self, monkeypatch):
        monkeypatch.delenv("MINDFLOCK_PORT", raising=False)
        monkeypatch.delenv("MINDFLOCK_HOST", raising=False)
        monkeypatch.setattr(client, "probe", lambda base, timeout=1.0: None)
        with pytest.raises(client.ServerNotFound) as exc:
            client.discover()
        assert "mindflock serve" in str(exc.value)

    def test_explicit_dead_address_named_in_error(self, monkeypatch):
        monkeypatch.setattr(client, "probe", lambda base, timeout=1.0: None)
        with pytest.raises(client.ServerNotFound) as exc:
            client.discover(None, 9000)
        assert "http://127.0.0.1:9000" in str(exc.value)

    def test_bad_env_port_rejected(self, monkeypatch):
        monkeypatch.setenv("MINDFLOCK_PORT", "banana")
        with pytest.raises(client.ServerNotFound):
            client.discover()


class _FakeApi:
    """Canned client.get/post with call recording, for the command tests."""

    def __init__(self, monkeypatch, listings, post_result=None):
        self.listings = list(listings)  # successive GET /api/instances answers
        self.posts = []
        monkeypatch.setattr(client, "discover", lambda *a, **k: "http://127.0.0.1:8765")
        monkeypatch.setattr(client, "get", self._get)
        monkeypatch.setattr(client, "post", self._post)
        self._post_result = post_result if post_result is not None else {}
        # Make the ~15s readiness poll instant.
        monkeypatch.setattr(cli.time, "sleep", lambda s: None)

    def _get(self, base, path, timeout=None):
        assert path == "/api/instances"
        return self.listings.pop(0) if len(self.listings) > 1 else self.listings[0]

    def _post(self, base, path, payload=None, timeout=None):
        self.posts.append((path, payload))
        return self._post_result


class TestNewCommand:
    def test_payload_and_ready_report(self, monkeypatch, capsys, tmp_path):
        api = _FakeApi(
            monkeypatch,
            listings=[
                [],  # pre-create listing (auto-title)
                [{"title": "myrepo", "status": "running"}],  # readiness poll
            ],
            post_result={"title": "myrepo", "status": "loading"},
        )
        repo = tmp_path / "myrepo"
        repo.mkdir()
        assert cli.main(["new", str(repo), "-p", "fix the tests"]) == 0
        path, payload = api.posts[0]
        assert path == "/api/instances"
        assert payload["title"] == "myrepo"
        assert payload["repo_path"] == str(repo)
        assert payload["prompt"] == "fix the tests"
        assert "provisioned" not in payload
        out = capsys.readouterr().out
        assert "mindflock attach myrepo" in out
        assert "ready (status: running)" in out

    def test_auto_title_suffix_on_collision(self, monkeypatch, capsys, tmp_path):
        api = _FakeApi(
            monkeypatch,
            listings=[
                [{"title": "myrepo"}, {"title": "myrepo-2"}],
                [{"title": "myrepo-3", "status": "running"}],
            ],
            post_result={"title": "myrepo-3"},
        )
        repo = tmp_path / "myrepo"
        repo.mkdir()
        assert cli.main(["new", str(repo)]) == 0
        assert api.posts[0][1]["title"] == "myrepo-3"

    def test_provision_flags_forwarded(self, monkeypatch, capsys, tmp_path):
        api = _FakeApi(
            monkeypatch,
            listings=[[], [{"title": "r", "status": "running"}]],
            post_result={"title": "r"},
        )
        repo = tmp_path / "r"
        repo.mkdir()
        assert (
            cli.main(
                [
                    "new",
                    str(repo),
                    "--provision",
                    "--strategy",
                    "clone",
                    "-t",
                    "r",
                    "--program",
                    "codex",
                ]
            )
            == 0
        )
        payload = api.posts[0][1]
        assert payload["provisioned"] is True
        assert payload["workspace_strategy"] == "clone"
        assert payload["program"] == "codex"

    def test_repo_defaults_to_cwd(self, monkeypatch, capsys, tmp_path):
        api = _FakeApi(
            monkeypatch,
            listings=[[], [{"title": "proj", "status": "running"}]],
            post_result={"title": "proj"},
        )
        proj = tmp_path / "proj"
        proj.mkdir()
        monkeypatch.chdir(proj)
        assert cli.main(["new"]) == 0
        assert api.posts[0][1]["repo_path"] == os.path.realpath(str(proj))

    def test_vanished_session_reports_failure(self, monkeypatch, capsys, tmp_path):
        _FakeApi(
            monkeypatch,
            listings=[[], []],  # session gone after create → bg Start failed
            post_result={"title": "gone"},
        )
        repo = tmp_path / "gone"
        repo.mkdir()
        assert cli.main(["new", str(repo)]) == 1
        assert "failed to start" in capsys.readouterr().err

    def test_no_server_exits_one_with_hint(self, monkeypatch, capsys):
        monkeypatch.setattr(
            client,
            "discover",
            lambda *a, **k: (_ for _ in ()).throw(client.ServerNotFound()),
        )
        assert cli.main(["new"]) == 1
        assert "mindflock serve" in capsys.readouterr().err


class TestNewCommandPolling:
    """The post-create readiness poll: a transient GET error is tolerated, and a
    session that never leaves 'loading' by the deadline reports 'still
    provisioning' (not a failure)."""

    def _wire(self, monkeypatch, get_fn, monotonic_values):
        monkeypatch.setattr(client, "discover", lambda *a, **k: "http://x")
        monkeypatch.setattr(client, "get", get_fn)
        monkeypatch.setattr(
            client,
            "post",
            lambda base, path, payload=None, timeout=None: {"title": "t"},
        )
        monkeypatch.setattr(cli.time, "sleep", lambda s: None)
        seq = iter(monotonic_values)
        monkeypatch.setattr(cli.time, "monotonic", lambda: next(seq))

    def test_still_loading_after_deadline(self, monkeypatch, capsys, tmp_path):
        def _get(base, path, timeout=None):
            return [{"title": "t", "status": "loading"}]

        # First monotonic() sets the deadline; the loop-condition read is already
        # past it, so the poll body never runs and status stays "loading".
        self._wire(monkeypatch, _get, [0.0, 999.0])
        repo = tmp_path / "t"
        repo.mkdir()
        assert cli.main(["new", str(repo), "-t", "t"]) == 0
        assert "still provisioning" in capsys.readouterr().out

    def test_transient_get_error_during_poll_is_tolerated(
        self, monkeypatch, capsys, tmp_path
    ):
        calls = {"n": 0}

        def _get(base, path, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return []  # pre-create listing (auto-title)
            raise client.ClientError("transient network blip")

        # Enter the loop once (5 < 15), hit the error, then exit (999 >= 15).
        self._wire(monkeypatch, _get, [0.0, 5.0, 999.0])
        repo = tmp_path / "t"
        repo.mkdir()
        assert cli.main(["new", str(repo), "-t", "t"]) == 0
        assert calls["n"] == 2  # pre-create + one polled (errored) read
        assert "still provisioning" in capsys.readouterr().out


class TestLsCommand:
    _LISTING = [
        {
            "title": "fix-auth",
            "repo": "webapp",
            "status": "running",
            "activity": "working",
            "stage": "coding",
            "diff_stat": {"files": 3, "additions": 120, "deletions": 8},
            "tokens_cost": 0.42,
        },
        {
            "title": "old-server",
            "repo": "api",
            "status": "paused",
            "activity": "idle",
            "stage": "",
            # no diff_stat / tokens_cost → columns must be blank, not crash
        },
    ]

    def test_table_with_feature_detected_columns(self, monkeypatch, capsys):
        _FakeApi(monkeypatch, listings=[self._LISTING])
        assert cli.main(["ls"]) == 0
        out = capsys.readouterr().out
        lines = out.splitlines()
        assert lines[0].split() == [
            "TITLE",
            "REPO",
            "STATUS",
            "ACTIVITY",
            "STAGE",
            "DIFF",
            "COST",
        ]
        assert "+120 −8" in out
        assert "$0.42" in out
        row2 = [ln for ln in lines if ln.startswith("old-server")][0]
        assert "None" not in row2 and "+" not in row2 and "$" not in row2

    def test_json_flag_dumps_raw(self, monkeypatch, capsys):
        _FakeApi(monkeypatch, listings=[self._LISTING])
        assert cli.main(["ls", "--json"]) == 0
        assert json.loads(capsys.readouterr().out) == self._LISTING

    def test_empty_listing_hint(self, monkeypatch, capsys):
        _FakeApi(monkeypatch, listings=[[]])
        assert cli.main(["ls"]) == 0
        assert "mindflock new" in capsys.readouterr().out


class TestAttachCommand:
    _LISTING = [
        {"title": "fix-auth", "tmux_name": "mindflock_fix-auth"},
        {"title": "fix-tests", "tmux_name": "mindflock_fix-tests"},
        {"title": "docs", "tmux_name": "mindflock_docs"},
    ]

    @pytest.fixture()
    def execvp_spy(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            cli.os, "execvp", lambda prog, argv: calls.append((prog, argv))
        )
        monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/tmux")
        # Under pytest stdout is captured (not a TTY); attach pre-checks it (L10).
        monkeypatch.setattr(cli, "_stdout_is_tty", lambda: True)
        return calls

    def test_exact_title_attaches(self, monkeypatch, execvp_spy):
        _FakeApi(monkeypatch, listings=[self._LISTING])
        cli.main(["attach", "fix-auth"])
        assert execvp_spy == [
            ("tmux", ["tmux", "attach-session", "-t", "mindflock_fix-auth"])
        ]

    def test_unambiguous_prefix_resolves(self, monkeypatch, execvp_spy):
        _FakeApi(monkeypatch, listings=[self._LISTING])
        cli.main(["attach", "do"])
        assert execvp_spy[0][1][-1] == "mindflock_docs"

    def test_ambiguous_prefix_lists_candidates(self, monkeypatch, execvp_spy, capsys):
        _FakeApi(monkeypatch, listings=[self._LISTING])
        assert cli.main(["attach", "fix"]) == 1
        err = capsys.readouterr().err
        assert "fix-auth" in err and "fix-tests" in err
        assert execvp_spy == []

    def test_unknown_title_errors(self, monkeypatch, execvp_spy, capsys):
        _FakeApi(monkeypatch, listings=[self._LISTING])
        assert cli.main(["attach", "nope"]) == 1
        assert "no session named" in capsys.readouterr().err

    def test_missing_tmux_binary(self, monkeypatch, capsys):
        _FakeApi(monkeypatch, listings=[self._LISTING])
        monkeypatch.setattr(cli.shutil, "which", lambda name: None)
        monkeypatch.setattr(cli, "_stdout_is_tty", lambda: True)
        assert cli.main(["attach", "docs"]) == 1
        assert "tmux not found" in capsys.readouterr().err

    def test_tmux_name_derived_when_field_missing(self, monkeypatch, execvp_spy):
        _FakeApi(monkeypatch, listings=[[{"title": "a b.c"}]])
        cli.main(["attach", "a b.c"])
        assert execvp_spy[0][1][-1] == "mindflock_ab_c"

    def test_non_tty_refused_with_scripting_hint(self, monkeypatch, capsys):
        """L10: attach inside a pipe/script fails fast with a friendly pointer
        (before any server discovery — no server needed to see the message)."""
        monkeypatch.setattr(cli, "_stdout_is_tty", lambda: False)
        monkeypatch.setattr(
            client,
            "discover",
            lambda *a, **k: pytest.fail("discover must not run for a non-TTY attach"),
        )
        assert cli.main(["attach", "docs"]) == 1
        err = capsys.readouterr().err
        assert "attach needs a real terminal" in err
        assert "mindflock ls --json" in err


class TestStdoutTty:
    def test_true_for_a_real_tty(self, monkeypatch):
        monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
        assert cli._stdout_is_tty() is True

    def test_false_when_isatty_raises(self, monkeypatch):
        # An exotic stdout replacement whose isatty() blows up must degrade to
        # False, never propagate (attach relies on this guard).
        class _Boom:
            def isatty(self):
                raise ValueError("no isatty here")

        monkeypatch.setattr(cli.sys, "stdout", _Boom())
        assert cli._stdout_is_tty() is False


class TestRmCommand:
    """`mindflock rm TITLE... [--yes]` → POST /api/instances/{title}/close
    (agent stops, worktree + branch kept); `--wipe` → POST .../cleanup."""

    _LISTING = [
        {"title": "fix-auth"},
        {"title": "fix-tests"},
        {"title": "docs"},
    ]

    @pytest.fixture()
    def posted(self, monkeypatch):
        """Record every POST path; DELETE must never be used by rm any more."""
        calls = []
        monkeypatch.setattr(client, "discover", lambda *a, **k: "http://127.0.0.1:8765")
        monkeypatch.setattr(
            client, "get", lambda base, path, timeout=None: self._LISTING
        )
        monkeypatch.setattr(
            client,
            "post",
            lambda base, path, payload=None, timeout=None: calls.append(path)
            or {"ok": True},
        )
        monkeypatch.setattr(
            client,
            "delete",
            lambda *a, **k: pytest.fail("rm must not DELETE (that wipes the worktree)"),
        )
        return calls

    @pytest.fixture()
    def tty_stdin(self, monkeypatch):
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    @pytest.fixture()
    def no_tty_stdin(self, monkeypatch):
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    # -- default: close (keeps the worktree + branch) -------------------------

    def test_yes_flag_closes_without_prompt(self, posted, monkeypatch, capsys):
        monkeypatch.setattr(
            "builtins.input", lambda *a: pytest.fail("must not prompt with --yes")
        )
        assert cli.main(["rm", "docs", "--yes"]) == 0
        assert posted == ["/api/instances/docs/close"]
        out = capsys.readouterr().out
        assert "closed session docs" in out
        assert "worktree and branch kept" in out
        assert "Recently closed" in out

    def test_prompt_says_what_happens_and_accepts_y(self, posted, monkeypatch):
        seen = {}

        def _input(prompt):
            seen["prompt"] = prompt
            return "y"

        monkeypatch.setattr("builtins.input", _input)
        assert cli.main(["rm", "fix-auth"]) == 0
        assert posted == ["/api/instances/fix-auth/close"]
        assert "'fix-auth'" in seen["prompt"]
        assert "worktree and branch are kept" in seen["prompt"]
        assert "[y/N]" in seen["prompt"]

    def test_prompt_declined_ends_nothing(self, posted, monkeypatch, capsys):
        monkeypatch.setattr("builtins.input", lambda prompt: "n")
        assert cli.main(["rm", "fix-auth"]) == 0
        assert posted == []
        assert "aborted" in capsys.readouterr().out

    def test_prompt_eof_aborts_with_error(self, posted, monkeypatch, capsys):
        def _eof(prompt):
            raise EOFError

        monkeypatch.setattr("builtins.input", _eof)
        assert cli.main(["rm", "fix-auth"]) == 1
        assert posted == []
        assert "aborted" in capsys.readouterr().err

    def test_several_titles_one_prompt(self, posted, monkeypatch):
        prompts = []

        def _input(prompt):
            prompts.append(prompt)
            return "y"

        monkeypatch.setattr("builtins.input", _input)
        assert cli.main(["rm", "docs", "fix-auth"]) == 0
        assert posted == ["/api/instances/docs/close", "/api/instances/fix-auth/close"]
        assert len(prompts) == 1
        assert "'docs'" in prompts[0] and "'fix-auth'" in prompts[0]

    def test_unambiguous_prefix_resolves(self, posted, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda prompt: "yes")
        assert cli.main(["rm", "do"]) == 0
        assert posted == ["/api/instances/docs/close"]

    def test_duplicate_titles_collapse(self, posted):
        assert cli.main(["rm", "do", "docs", "--yes"]) == 0
        assert posted == ["/api/instances/docs/close"]

    def test_ambiguous_prefix_lists_candidates(self, posted, capsys):
        assert cli.main(["rm", "fix", "--yes"]) == 1
        err = capsys.readouterr().err
        assert "fix-auth" in err and "fix-tests" in err
        assert posted == []

    def test_unknown_title_friendly_error(self, posted, capsys):
        assert cli.main(["rm", "nope", "--yes"]) == 1
        err = capsys.readouterr().err
        assert "no session named" in err and "mindflock ls" in err
        assert posted == []

    def test_one_bad_title_ends_none_of_them(self, posted, capsys):
        # All titles resolve before anything is ended: a typo costs nothing.
        assert cli.main(["rm", "docs", "nope", "--yes"]) == 1
        assert posted == []

    def test_no_server_exits_one_with_hint(self, monkeypatch, capsys):
        monkeypatch.setattr(
            client,
            "discover",
            lambda *a, **k: (_ for _ in ()).throw(client.ServerNotFound()),
        )
        assert cli.main(["rm", "docs", "--yes"]) == 1
        assert "mindflock serve" in capsys.readouterr().err

    def test_help_text_names_both_outcomes(self):
        parser = cli._build_parser()
        rm = next(
            a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
        ).choices["rm"]
        text = " ".join(rm.format_help().split())
        assert "worktree and branch stay on disk" in text
        assert "Recently closed" in text
        assert "--wipe" in text and "delete" in text
        # …and the one-liner in the top-level command list says the same.
        top = " ".join(parser.format_help().split())
        assert "keep the worktree and branch" in top

    # -- --wipe: cleanup (deletes the worktree + branch) ----------------------

    def test_wipe_yes_hits_cleanup_route(self, posted, monkeypatch, capsys):
        monkeypatch.setattr(
            "builtins.input", lambda *a: pytest.fail("must not prompt with --yes")
        )
        assert cli.main(["rm", "--wipe", "docs", "--yes"]) == 0
        assert posted == ["/api/instances/docs/cleanup"]
        out = capsys.readouterr().out
        assert "deleted session docs" in out

    def test_wipe_on_tty_asks_y_n_naming_the_loss(self, posted, monkeypatch, tty_stdin):
        seen = {}

        def _input(prompt):
            seen["prompt"] = prompt
            return "y"

        monkeypatch.setattr("builtins.input", _input)
        assert cli.main(["rm", "--wipe", "fix-auth", "docs"]) == 0
        assert posted == [
            "/api/instances/fix-auth/cleanup",
            "/api/instances/docs/cleanup",
        ]
        assert "[y/N]" in seen["prompt"]
        assert "DELETES the worktree and branch" in seen["prompt"]
        assert "'fix-auth'" in seen["prompt"] and "'docs'" in seen["prompt"]

    def test_wipe_declined_wipes_nothing(self, posted, monkeypatch, tty_stdin, capsys):
        monkeypatch.setattr("builtins.input", lambda prompt: "")
        assert cli.main(["rm", "--wipe", "docs"]) == 0
        assert posted == []
        assert "aborted" in capsys.readouterr().out

    def test_wipe_without_tty_and_without_yes_refuses(
        self, posted, monkeypatch, no_tty_stdin, capsys
    ):
        monkeypatch.setattr(
            "builtins.input", lambda *a: pytest.fail("must not prompt off a TTY")
        )
        assert cli.main(["rm", "--wipe", "docs"]) == 1
        assert posted == []
        err = capsys.readouterr().err
        assert "--yes" in err and "terminal" in err and "'docs'" in err

    def test_wipe_without_tty_but_with_yes_proceeds(self, posted, no_tty_stdin):
        assert cli.main(["rm", "--wipe", "docs", "--yes"]) == 0
        assert posted == ["/api/instances/docs/cleanup"]

    def test_wipe_409_prints_sessions_and_exits_one(self, monkeypatch, capsys):
        calls = []
        monkeypatch.setattr(client, "discover", lambda *a, **k: "http://127.0.0.1:8765")
        monkeypatch.setattr(
            client, "get", lambda base, path, timeout=None: self._LISTING
        )

        def _post(base, path, payload=None, timeout=None):
            calls.append(path)
            if path == "/api/instances/docs/cleanup":
                raise client.ApiError(
                    409,
                    "folder in use",
                    {"error": "folder in use", "sessions": ["docs-copy", "docs-2"]},
                )
            return {"ok": True}

        monkeypatch.setattr(client, "post", _post)
        # The other title is still wiped; the exit code reports the refusal.
        assert cli.main(["rm", "--wipe", "docs", "fix-auth", "--yes"]) == 1
        assert calls == [
            "/api/instances/docs/cleanup",
            "/api/instances/fix-auth/cleanup",
        ]
        captured = capsys.readouterr()
        assert "docs-copy" in captured.err and "docs-2" in captured.err
        assert "'docs'" in captured.err
        assert "deleted session fix-auth" in captured.out

    def test_wipe_409_with_title_rows(self, monkeypatch, capsys):
        monkeypatch.setattr(client, "discover", lambda *a, **k: "http://127.0.0.1:8765")
        monkeypatch.setattr(
            client, "get", lambda base, path, timeout=None: self._LISTING
        )

        def _post(base, path, payload=None, timeout=None):
            raise client.ApiError(409, "in use", {"sessions": [{"title": "docs-copy"}]})

        monkeypatch.setattr(client, "post", _post)
        assert cli.main(["rm", "--wipe", "docs", "--yes"]) == 1
        assert "docs-copy" in capsys.readouterr().err

    def test_wipe_other_api_error_is_a_plain_error(self, monkeypatch, capsys):
        monkeypatch.setattr(client, "discover", lambda *a, **k: "http://127.0.0.1:8765")
        monkeypatch.setattr(
            client, "get", lambda base, path, timeout=None: self._LISTING
        )

        def _post(base, path, payload=None, timeout=None):
            raise client.ApiError(404, "no such session")

        monkeypatch.setattr(client, "post", _post)
        assert cli.main(["rm", "--wipe", "docs", "--yes"]) == 1
        assert "no such session" in capsys.readouterr().err


class TestCloseDeleteCommands:
    """`close` / `delete` are the named commands; `rm` / `rm --wipe` stay as
    their aliases (the TestRmCommand cases above still run through rm)."""

    _LISTING = TestRmCommand._LISTING
    posted = TestRmCommand.posted
    no_tty_stdin = TestRmCommand.no_tty_stdin

    def test_close_hits_close_route(self, posted, monkeypatch, capsys):
        monkeypatch.setattr(
            "builtins.input", lambda *a: pytest.fail("must not prompt with --yes")
        )
        assert cli.main(["close", "docs", "--yes"]) == 0
        assert posted == ["/api/instances/docs/close"]
        assert "closed session docs" in capsys.readouterr().out

    def test_close_takes_no_wipe_flag(self):
        with pytest.raises(SystemExit):
            cli.main(["close", "--wipe", "docs", "--yes"])

    def test_delete_hits_cleanup_route(self, posted, capsys):
        assert cli.main(["delete", "do", "--yes"]) == 0
        assert posted == ["/api/instances/docs/cleanup"]
        assert "deleted session docs" in capsys.readouterr().out

    def test_delete_without_tty_names_close_not_wipe(
        self, posted, no_tty_stdin, capsys
    ):
        assert cli.main(["delete", "docs"]) == 1
        assert posted == []
        err = capsys.readouterr().err
        assert "delete deletes" in err and "use `close`" in err
        assert "--wipe" not in err

    def test_rm_help_says_it_is_an_alias(self):
        parser = cli._build_parser()
        choices = next(
            a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
        ).choices
        rm_help = " ".join(choices["rm"].format_help().split())
        assert "Alias of `close`" in rm_help and "`delete`" in rm_help
        for name in ("close", "delete", "pause", "resume"):
            assert name in choices


class TestPauseResumeCommands:
    _LISTING = [{"title": "fix-auth"}, {"title": "docs"}]

    @pytest.fixture()
    def posted(self, monkeypatch):
        calls = []
        monkeypatch.setattr(client, "discover", lambda *a, **k: "http://127.0.0.1:8765")
        monkeypatch.setattr(
            client, "get", lambda base, path, timeout=None: self._LISTING
        )
        monkeypatch.setattr(
            client,
            "post",
            lambda base, path, payload=None, timeout=None: calls.append(path) or {},
        )
        return calls

    def test_pause_posts_pause_route(self, posted, capsys):
        assert cli.main(["pause", "fix", "docs"]) == 0
        assert posted == [
            "/api/instances/fix-auth/pause",
            "/api/instances/docs/pause",
        ]
        assert "paused session fix-auth" in capsys.readouterr().out

    def test_resume_posts_resume_route(self, posted, capsys):
        assert cli.main(["resume", "docs"]) == 0
        assert posted == ["/api/instances/docs/resume"]
        assert "resumed session docs" in capsys.readouterr().out

    def test_refusal_is_reported_and_others_still_run(self, monkeypatch, capsys):
        calls = []
        monkeypatch.setattr(client, "discover", lambda *a, **k: "http://127.0.0.1:8765")
        monkeypatch.setattr(
            client, "get", lambda base, path, timeout=None: self._LISTING
        )

        def _post(base, path, payload=None, timeout=None):
            calls.append(path)
            if "fix-auth" in path:
                raise client.ApiError(409, "peer shared sessions cannot be paused")
            return {}

        monkeypatch.setattr(client, "post", _post)
        assert cli.main(["pause", "fix-auth", "docs"]) == 1
        assert calls == [
            "/api/instances/fix-auth/pause",
            "/api/instances/docs/pause",
        ]
        assert "cannot be paused" in capsys.readouterr().err


class TestSleepWakeCommands:
    """``mindflock sleep|wake`` (core.sleep): the per-session verbs, like
    pause/resume; ``attach`` wakes a sleeper before landing in its tmux."""

    _LISTING = [{"title": "fix-auth"}, {"title": "docs"}]

    @pytest.fixture()
    def posted(self, monkeypatch):
        calls = []
        monkeypatch.setattr(client, "discover", lambda *a, **k: "http://127.0.0.1:8765")
        monkeypatch.setattr(
            client, "get", lambda base, path, timeout=None: self._LISTING
        )

        def _post(base, path, payload=None, timeout=None):
            calls.append(path)
            if path.endswith("/sleep"):
                return {"sleeping": {"since": 1.0, "reason": "manual", "freed_mb": 780}}
            return {"woke": "docs" in path}

        monkeypatch.setattr(client, "post", _post)
        return calls

    def test_sleep_posts_the_sleep_route_and_says_what_it_freed(self, posted, capsys):
        assert cli.main(["sleep", "fix", "docs"]) == 0
        assert posted == ["/api/instances/fix-auth/sleep", "/api/instances/docs/sleep"]
        assert "fix-auth is asleep (freed ~780 MB)" in capsys.readouterr().out

    def test_wake_posts_the_wake_route(self, posted, capsys):
        assert cli.main(["wake", "fix-auth", "docs"]) == 0
        assert posted == ["/api/instances/fix-auth/wake", "/api/instances/docs/wake"]
        out = capsys.readouterr().out
        assert "fix-auth was not asleep" in out and "woke docs" in out

    def test_a_refused_sleep_exits_1(self, monkeypatch, capsys):
        monkeypatch.setattr(client, "discover", lambda *a, **k: "http://127.0.0.1:8765")
        monkeypatch.setattr(
            client, "get", lambda base, path, timeout=None: self._LISTING
        )

        def _post(base, path, payload=None, timeout=None):
            raise client.ApiError(409, "can't sleep docs: not idle")

        monkeypatch.setattr(client, "post", _post)
        assert cli.main(["sleep", "docs"]) == 1
        assert "not idle" in capsys.readouterr().err

    def test_attach_wakes_a_sleeper_first(self, monkeypatch):
        calls = []
        monkeypatch.setattr(client, "discover", lambda *a, **k: "http://127.0.0.1:8765")
        monkeypatch.setattr(
            client,
            "get",
            lambda base, path, timeout=None: [
                {
                    "title": "docs",
                    "tmux_name": "mindflock_docs",
                    "sleeping": {"since": 1},
                }
            ],
        )
        monkeypatch.setattr(
            client,
            "post",
            lambda base, path, payload=None, timeout=None: calls.append(path) or {},
        )
        monkeypatch.setattr(cli, "_stdout_is_tty", lambda: True)
        monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/tmux")
        execd = []
        monkeypatch.setattr(cli.os, "execvp", lambda f, argv: execd.append(argv))
        cli.main(["attach", "docs"])
        assert calls == ["/api/instances/docs/wake"]
        assert execd == [["tmux", "attach-session", "-t", "mindflock_docs"]]


class TestClientDelete:
    def test_delete_uses_http_delete(self, monkeypatch):
        seen = {}

        def _urlopen(req, timeout=None):
            seen["method"] = req.get_method()
            seen["url"] = req.full_url
            return _fake_response({"ok": True})

        monkeypatch.setattr("urllib.request.urlopen", _urlopen)
        assert client.delete("http://127.0.0.1:8765", "/api/instances/docs") == {
            "ok": True
        }
        assert seen["method"] == "DELETE"
        assert seen["url"].endswith("/api/instances/docs")


class TestOpenCommand:
    def test_posts_ide_endpoint(self, monkeypatch, capsys):
        api = _FakeApi(
            monkeypatch,
            listings=[[{"title": "fix-auth"}]],
            post_result={"ok": True, "opened_new": True},
        )
        assert cli.main(["open", "fix"]) == 0
        assert api.posts == [("/api/instances/fix-auth/ide", None)]
        assert "opened fix-auth" in capsys.readouterr().out

    def test_focus_message_when_already_open(self, monkeypatch, capsys):
        _FakeApi(
            monkeypatch,
            listings=[[{"title": "docs"}]],
            post_result={"ok": True, "opened_new": False},
        )
        assert cli.main(["open", "docs"]) == 0
        assert "focused" in capsys.readouterr().out

    def test_api_error_printed(self, monkeypatch, capsys):
        monkeypatch.setattr(client, "discover", lambda *a, **k: "http://x")
        monkeypatch.setattr(client, "get", lambda *a, **k: [{"title": "docs"}])

        def _post(*a, **k):
            raise client.ApiError(409, "workspace not ready")

        monkeypatch.setattr(client, "post", _post)
        assert cli.main(["open", "docs"]) == 1
        assert "workspace not ready" in capsys.readouterr().err


class TestEventFormatting:
    def test_status_change_line(self):
        line = cli._format_event(
            {
                "seq": 42,
                "event": "session.status_changed",
                "session": "my-title",
                "old": "loading",
                "new": "running",
                "ts": 0.0,
                "data": {},
            }
        )
        assert "session.status_changed" in line
        assert "my-title" in line
        assert "loading -> running" in line

    def test_data_payload_rendered(self):
        line = cli._format_event(
            {
                "event": "session.created",
                "session": "s",
                "new": "loading",
                "data": {"program": "claude"},
            }
        )
        assert '"program":"claude"' in line


class TestAutoTitle:
    """_auto_title sanitizes the repo basename into a tmux/branch-safe name and
    falls back to 'session' when nothing usable survives."""

    def test_special_chars_sanitized(self):
        assert cli._auto_title("/tmp/My Repo!", []) == "My-Repo"

    def test_root_path_falls_back_to_session(self):
        assert cli._auto_title("/", []) == "session"

    def test_all_special_sanitizes_to_session(self):
        assert cli._auto_title("/x/@@@", []) == "session"

    def test_collision_suffixes(self):
        assert cli._auto_title("proj", ["proj", "proj-2"]) == "proj-3"


class _FakeWs:
    """Context-manager websocket whose recv() replays a scripted sequence.

    Each item is ``("msg", raw_json)`` to return or ``("raise", exc)`` to raise
    (used to simulate the TimeoutError stop and a mid-stream ConnectionClosed)."""

    def __init__(self, script):
        self._script = list(script)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def recv(self, timeout=None):
        kind, payload = self._script.pop(0)
        if kind == "raise":
            raise payload
        return payload


class TestEventsCommand:
    """`mindflock events` — the missing-websockets guard, the backlog-then-stop
    (no --follow) path, the OSError failure, and the clean exit on a server
    close. No real socket is opened: a fake `websockets` is injected into
    sys.modules so the optional dependency and the network are both stubbed."""

    @pytest.fixture()
    def events_env(self, monkeypatch):
        monkeypatch.setattr(client, "discover", lambda *a, **k: "http://127.0.0.1:8765")

        class Ctl:
            ws = None
            connect_error = None
            url = None

        ctl = Ctl()
        ctl.ConnectionClosed = type("ConnectionClosed", (Exception,), {})

        def _connect(url):
            ctl.url = url
            if ctl.connect_error is not None:
                raise ctl.connect_error
            return ctl.ws

        sync_client = types.ModuleType("websockets.sync.client")
        sync_client.connect = _connect
        sync_mod = types.ModuleType("websockets.sync")
        sync_mod.client = sync_client
        exc_mod = types.ModuleType("websockets.exceptions")
        exc_mod.ConnectionClosed = ctl.ConnectionClosed
        pkg = types.ModuleType("websockets")

        monkeypatch.setitem(sys.modules, "websockets", pkg)
        monkeypatch.setitem(sys.modules, "websockets.sync", sync_mod)
        monkeypatch.setitem(sys.modules, "websockets.sync.client", sync_client)
        monkeypatch.setitem(sys.modules, "websockets.exceptions", exc_mod)
        return ctl

    def test_backlog_prints_then_stops_without_follow(self, events_env, capsys):
        env1 = {
            "event": "session.created",
            "session": "a",
            "new": "loading",
            "ts": 0.0,
        }
        env2 = {
            "event": "session.status_changed",
            "session": "a",
            "old": "loading",
            "new": "running",
            "ts": 0.0,
        }
        events_env.ws = _FakeWs(
            [
                ("msg", json.dumps(env1)),
                ("msg", json.dumps(env2)),
                ("raise", TimeoutError()),  # first quiet second -> stop
            ]
        )
        assert cli.main(["events"]) == 0
        out = capsys.readouterr().out
        assert "session.created" in out
        assert "loading -> running" in out
        # ws_url is otherwise untested: confirm the scheme swap end to end.
        assert events_env.url == "ws://127.0.0.1:8765/api/events"

    def test_missing_websockets_prints_hint(self, monkeypatch, capsys):
        monkeypatch.setattr(client, "discover", lambda *a, **k: "http://127.0.0.1:8765")

        class _Block:
            def find_spec(self, name, path=None, target=None):
                if name == "websockets":
                    raise ModuleNotFoundError(
                        f"No module named '{name}'", name="websockets"
                    )
                return None

        for mod in list(sys.modules):
            if mod == "websockets" or mod.startswith("websockets."):
                monkeypatch.delitem(sys.modules, mod, raising=False)
        monkeypatch.setattr(sys, "meta_path", [_Block()] + sys.meta_path)
        assert cli.main(["events"]) == 1
        err = capsys.readouterr().err
        assert "websockets" in err
        # The remediation now points at the uv-tool reinstall form, not the
        # bare `uv sync --group web` (which only helps in a source checkout).
        assert 'uv tool install --force "mindflock[web]' in err

    def test_connect_oserror_reports_failure(self, events_env, capsys):
        events_env.connect_error = OSError("connection refused")
        assert cli.main(["events"]) == 1
        assert "event stream failed" in capsys.readouterr().err

    def test_server_close_during_follow_exits_clean(self, events_env, capsys):
        # A peer-initiated close mid-stream must be a clean exit, not a traceback.
        events_env.ws = _FakeWs([("raise", events_env.ConnectionClosed())])
        assert cli.main(["events", "--follow"]) == 0

    def test_keyboard_interrupt_exits_clean(self, events_env):
        # Ctrl-C while streaming is a normal stop, not an error exit.
        events_env.ws = _FakeWs([("raise", KeyboardInterrupt())])
        assert cli.main(["events", "--follow"]) == 0

    def test_ws_url_scheme_swap(self):
        assert (
            client.ws_url("http://127.0.0.1:8765", "/api/events")
            == "ws://127.0.0.1:8765/api/events"
        )
        assert client.ws_url("https://host", "/api/events") == "wss://host/api/events"


class TestVersionFlag:
    def test_version_prints_and_exits_zero(self, capsys):
        import backend

        with pytest.raises(SystemExit) as exc:
            cli.main(["--version"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert out.strip() == f"mindflock {backend.__version__}"

    def test_version_single_sourced_from_pyproject(self):
        """pyproject.toml [project] version is canonical; __version__ mirrors it."""
        import tomllib
        from pathlib import Path

        import backend

        pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        assert backend.__version__ == data["project"]["version"]


class TestUninstallCommand:
    """`mindflock uninstall` — the guards, not the filesystem work (that's
    covered end-to-end in test_uninstall.py)."""

    @pytest.fixture(autouse=True)
    def _no_server(self, monkeypatch):
        from backend import uninstall

        monkeypatch.setattr(uninstall, "server_is_running", lambda *a, **k: False)

    @pytest.fixture
    def empty_plan(self, monkeypatch):
        from backend import uninstall

        plan = uninstall.Plan()
        monkeypatch.setattr(uninstall, "build_plan", lambda: plan)
        return plan

    def test_refuses_while_a_server_is_running(self, monkeypatch, capsys):
        from backend import uninstall

        monkeypatch.setattr(uninstall, "server_is_running", lambda *a, **k: True)
        called = []
        monkeypatch.setattr(uninstall, "execute", lambda *a, **k: called.append(1))

        assert cli.main(["uninstall", "--yes"]) == 1
        assert not called, "must not touch anything while a server is up"
        assert "stop it first" in capsys.readouterr().err

    def test_dry_run_is_allowed_while_a_server_is_running(self, monkeypatch, capsys):
        """Previewing changes nothing, and is exactly when you'd want to look."""
        from backend import uninstall

        monkeypatch.setattr(uninstall, "server_is_running", lambda *a, **k: True)
        monkeypatch.setattr(uninstall, "build_plan", lambda: uninstall.Plan())

        assert cli.main(["uninstall", "--dry-run"]) == 0
        out = capsys.readouterr().out
        assert "a server is running" in out
        assert "nothing was changed" in out

    def test_prompt_declined_aborts(self, monkeypatch, capsys, empty_plan):
        from backend import uninstall

        monkeypatch.setattr("builtins.input", lambda prompt="": "n")
        called = []
        monkeypatch.setattr(uninstall, "execute", lambda *a, **k: called.append(1))

        assert cli.main(["uninstall"]) == 0
        assert not called
        assert "aborted" in capsys.readouterr().out

    def test_purge_prompt_names_the_stakes(self, monkeypatch, empty_plan):
        from backend import uninstall

        seen = []
        monkeypatch.setattr(
            "builtins.input", lambda prompt="": seen.append(prompt) or "n"
        )
        monkeypatch.setattr(uninstall, "execute", lambda *a, **k: uninstall.Report())

        cli.main(["uninstall", "--purge"])

        assert "usage history" in seen[0]

    def test_yes_skips_the_prompt_and_passes_flags(
        self, monkeypatch, capsys, empty_plan
    ):
        from backend import uninstall

        monkeypatch.setattr(
            "builtins.input", lambda prompt="": pytest.fail("should not prompt")
        )
        got = {}

        def fake_execute(plan, purge=False, dry_run=False, keep_worktrees=False):
            got.update(purge=purge, dry_run=dry_run, keep_worktrees=keep_worktrees)
            return uninstall.Report()

        monkeypatch.setattr(uninstall, "execute", fake_execute)

        assert cli.main(["uninstall", "--yes", "--purge", "--keep-worktrees"]) == 0
        assert got == {"purge": True, "dry_run": False, "keep_worktrees": True}

    def test_prints_the_final_uv_step(self, monkeypatch, capsys, empty_plan):
        from backend import uninstall

        monkeypatch.setattr(uninstall, "execute", lambda *a, **k: uninstall.Report())

        cli.main(["uninstall", "--yes"])

        assert "uv tool uninstall mindflock" in capsys.readouterr().out

    def test_dry_run_does_not_print_the_uv_step(self, monkeypatch, capsys, empty_plan):
        from backend import uninstall

        monkeypatch.setattr(uninstall, "execute", lambda *a, **k: uninstall.Report())

        cli.main(["uninstall", "--dry-run"])

        assert "uv tool uninstall" not in capsys.readouterr().out

    def test_errors_produce_a_nonzero_exit(self, monkeypatch, capsys, empty_plan):
        from backend import uninstall

        report = uninstall.Report()
        report.failed("could not delete /x")
        monkeypatch.setattr(uninstall, "execute", lambda *a, **k: report)

        assert cli.main(["uninstall", "--yes"]) == 1
        assert "could not delete /x" in capsys.readouterr().err

    def test_plan_warnings_go_to_stderr(self, monkeypatch, capsys):
        from backend import uninstall

        plan = uninstall.Plan()
        plan.warnings.append("could not read state.json")
        monkeypatch.setattr(uninstall, "build_plan", lambda: plan)
        monkeypatch.setattr(uninstall, "execute", lambda *a, **k: uninstall.Report())

        cli.main(["uninstall", "--yes"])

        assert "could not read state.json" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Bearer token + text responses in backend.client
# --------------------------------------------------------------------------- #
class TestClientAuth:
    @pytest.fixture(autouse=True)
    def _fresh_token_cache(self):
        client.reset_auth_token()
        yield
        client.reset_auth_token()

    @staticmethod
    def _capture(monkeypatch, responses):
        """urlopen stand-in: records each request's auth header and answers
        with the next scripted payload (an exception instance raises)."""
        seen = []

        def _urlopen(req, timeout=None):
            seen.append(req.get_header("Authorization"))
            step = responses.pop(0)
            if isinstance(step, Exception):
                raise step
            return _fake_response(step)

        monkeypatch.setattr("urllib.request.urlopen", _urlopen)
        return seen

    @staticmethod
    def _401():
        return urllib.error.HTTPError(
            "http://x",
            401,
            "Unauthorized",
            {},
            io.BytesIO(b'{"error": "unauthorized"}'),
        )

    def test_no_token_sends_no_header(self, monkeypatch):
        seen = self._capture(monkeypatch, [{"ok": 1}])
        assert client.get("http://127.0.0.1:8765", "/api/x") == {"ok": 1}
        assert seen == [None]

    def test_env_token_wins(self, monkeypatch, tmp_path):
        settings = tmp_path / "s.json"
        settings.write_text(json.dumps({"general": {"auth_token": "from-file"}}))
        monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(settings))
        monkeypatch.setenv("MINDFLOCK_AUTH_TOKEN", "from-env")
        seen = self._capture(monkeypatch, [{"ok": 1}])
        client.get("http://127.0.0.1:8765", "/api/x")
        assert seen == ["Bearer from-env"]

    def test_settings_file_token_read_only(self, monkeypatch, tmp_path):
        settings = tmp_path / "s.json"
        settings.write_text(json.dumps({"general": {"auth_token": "from-file"}}))
        before = settings.read_bytes()
        monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(settings))
        seen = self._capture(monkeypatch, [{"ok": 1}])
        client.get("http://127.0.0.1:8765", "/api/x")
        assert seen == ["Bearer from-file"]
        assert settings.read_bytes() == before  # never minted or rewritten

    def test_missing_or_garbled_settings_file_means_no_token(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "absent.json"))
        assert client.auth_token() == ""
        client.reset_auth_token()
        settings = tmp_path / "s.json"
        settings.write_text("{not json")
        monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(settings))
        assert client.auth_token() == ""

    def test_token_is_cached(self, monkeypatch, tmp_path):
        settings = tmp_path / "s.json"
        settings.write_text(json.dumps({"general": {"auth_token": "one"}}))
        monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(settings))
        assert client.auth_token() == "one"
        settings.write_text(json.dumps({"general": {"auth_token": "two"}}))
        assert client.auth_token() == "one"

    def test_401_rereads_a_rotated_token_and_retries_once(self, monkeypatch, tmp_path):
        settings = tmp_path / "s.json"
        settings.write_text(json.dumps({"general": {"auth_token": "old"}}))
        monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(settings))
        assert client.auth_token() == "old"
        settings.write_text(json.dumps({"general": {"auth_token": "new"}}))
        seen = self._capture(monkeypatch, [self._401(), {"ok": 1}])
        assert client.get("http://127.0.0.1:8765", "/api/x") == {"ok": 1}
        assert seen == ["Bearer old", "Bearer new"]

    def test_401_with_unchanged_token_raises(self, monkeypatch):
        seen = self._capture(monkeypatch, [self._401()])
        with pytest.raises(client.ApiError) as exc:
            client.get("http://127.0.0.1:8765", "/api/x")
        assert exc.value.status == 401
        assert len(seen) == 1

    def test_probe_succeeds_behind_the_gate(self, monkeypatch):
        monkeypatch.setenv("MINDFLOCK_AUTH_TOKEN", "t")
        cfg = {"default_program": "claude", "caps": {}}

        def _urlopen(req, timeout=None):
            if req.get_header("Authorization") != "Bearer t":
                raise self._401()
            return _fake_response(cfg)

        monkeypatch.setattr("urllib.request.urlopen", _urlopen)
        assert client.probe("http://127.0.0.1:8765") == cfg

    def test_get_text_returns_the_body(self, monkeypatch):
        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return "## User\nhi — ünïcode".encode("utf-8")

        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: _Resp())
        assert client.get_text("http://x", "/history") == "## User\nhi — ünïcode"

    def test_read_timeout_is_request_timeout(self, monkeypatch):
        def _slow(req, timeout=None):
            raise TimeoutError("timed out")

        monkeypatch.setattr("urllib.request.urlopen", _slow)
        with pytest.raises(client.RequestTimeout):
            client.post("http://x", "/api/y", {})

    def test_refused_is_server_not_found_not_timeout(self, monkeypatch):
        def _refused(req, timeout=None):
            raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))

        monkeypatch.setattr("urllib.request.urlopen", _refused)
        with pytest.raises(client.ServerNotFound) as exc:
            client.get("http://x", "/api/y")
        assert not isinstance(exc.value, client.RequestTimeout)


# --------------------------------------------------------------------------- #
# mindflock msg / inbox / mcp
# --------------------------------------------------------------------------- #
class _FakeMailApi:
    def __init__(self, monkeypatch, listing, post_result=None, get_result=None):
        self.gets = []
        self.posts = []
        self._listing = listing
        self._post_result = post_result or {}
        self._get_result = get_result or {"messages": [], "unread": 0}
        monkeypatch.setattr(client, "discover", lambda *a, **k: "http://127.0.0.1:8765")
        monkeypatch.setattr(client, "get", self._get)
        monkeypatch.setattr(client, "post", self._post)

    def _get(self, base, path, timeout=None):
        self.gets.append(path)
        return self._listing if path == "/api/instances" else self._get_result

    def _post(self, base, path, payload=None, timeout=None):
        self.posts.append((path, payload))
        return self._post_result


class TestMsgCommand:
    def test_sends_from_outside_the_flock(self, monkeypatch, capsys):
        api = _FakeMailApi(
            monkeypatch,
            [{"title": "fix-auth"}],
            post_result={"message": {"id": "m1_1"}, "delivery": "pending"},
        )
        assert cli.main(["msg", "fix", "please", "rebase"]) == 0
        assert api.posts == [
            (
                "/api/instances/fix-auth/messages",
                {"text": "please rebase", "from": "", "delivery": "auto"},
            )
        ]
        assert "sent m1_1 to fix-auth (pending)" in capsys.readouterr().out

    def test_delivery_flag_and_detail(self, monkeypatch, capsys):
        api = _FakeMailApi(
            monkeypatch,
            [{"title": "docs"}],
            post_result={
                "message": {"id": "m2"},
                "delivery": "held",
                "detail": "rate limit",
            },
        )
        assert cli.main(["msg", "docs", "hi", "--delivery", "inbox"]) == 0
        assert api.posts[0][1]["delivery"] == "inbox"
        captured = capsys.readouterr()
        assert "(held)" in captured.out and "rate limit" in captured.err

    def test_stdin_text_and_quoted_title(self, monkeypatch):
        api = _FakeMailApi(monkeypatch, [{"title": "my session"}])
        monkeypatch.setattr("sys.stdin", io.StringIO("line one\nline two\n"))
        assert cli.main(["msg", "my session", "-"]) == 0
        path, payload = api.posts[0]
        assert path == "/api/instances/my%20session/messages"
        assert payload["text"] == "line one\nline two"

    def test_empty_text_refused(self, monkeypatch, capsys):
        api = _FakeMailApi(monkeypatch, [{"title": "docs"}])
        assert cli.main(["msg", "docs", "  "]) == 1
        assert api.posts == []
        assert "empty message" in capsys.readouterr().err

    def test_api_error_is_one_line(self, monkeypatch, capsys):
        _FakeMailApi(monkeypatch, [{"title": "docs"}])

        def _post(*a, **k):
            raise client.ApiError(400, "message text is too long")

        monkeypatch.setattr(client, "post", _post)
        assert cli.main(["msg", "docs", "x"]) == 1
        assert "error: message text is too long" in capsys.readouterr().err


class TestInboxCommand:
    _MSGS = {
        "messages": [
            {
                "id": "m1",
                "ts": 0,
                "from": "w1",
                "kind": "result",
                "state": "held",
                "text": "done:\nall tests pass",
            },
            {
                "id": "m2",
                "ts": 0,
                "from": "",
                "kind": "message",
                "state": "pending",
                "text": "x" * 300,
            },
        ],
        "unread": 2,
    }

    def test_lists_without_marking_read(self, monkeypatch, capsys):
        api = _FakeMailApi(monkeypatch, [{"title": "orch"}], get_result=self._MSGS)
        assert cli.main(["inbox", "orch"]) == 0
        query = api.gets[-1]
        assert query.startswith("/api/instances/orch/messages?")
        assert "mark_read=0" in query and "unread=1" in query
        out = capsys.readouterr().out.splitlines()
        assert "[held] result from w1: done: all tests pass" in out[0]
        assert "from (outside the flock)" in out[1] and out[1].endswith("…")

    def test_all_and_json(self, monkeypatch, capsys):
        api = _FakeMailApi(monkeypatch, [{"title": "orch"}], get_result=self._MSGS)
        assert cli.main(["inbox", "orch", "--all", "--json"]) == 0
        assert "include_consumed=1" in api.gets[-1]
        assert "mark_read=0" in api.gets[-1]
        assert json.loads(capsys.readouterr().out)["unread"] == 2

    def test_empty(self, monkeypatch, capsys):
        _FakeMailApi(monkeypatch, [{"title": "orch"}])
        assert cli.main(["inbox", "orch"]) == 0
        assert "no unread messages for orch" in capsys.readouterr().out


class TestMcpCommand:
    def test_print_config_snippets(self, capsys):
        import tomllib

        argv = ["mcp", "--print-config", "--scope", "all", "--port", "9100"]
        assert cli.main(argv) == 0
        out = capsys.readouterr().out
        add = next(ln for ln in out.splitlines() if ln.startswith("claude mcp add"))
        # The name must precede the variadic --env options.
        assert add.startswith("claude mcp add mindflock --scope user")
        assert " -- " in add and add.endswith("-P -m backend.mcp")
        assert "MINDFLOCK_MCP_SCOPE=all" in add and "MINDFLOCK_PORT=9100" in add
        start = out.index("{")
        end = out.index("# Codex")
        entry = json.loads(out[start:end])["mcpServers"]["mindflock"]
        assert entry["type"] == "stdio" and entry["command"] == sys.executable
        assert entry["args"] == ["-P", "-m", "backend.mcp"]
        assert entry["env"]["MINDFLOCK_MCP_SCOPE"] == "all"
        toml = tomllib.loads(out[end:])["mcp_servers"]["mindflock"]
        assert toml["command"] == sys.executable
        assert toml["tool_timeout_sec"] == 1620
        assert toml["startup_timeout_sec"] == 30
        assert "TMUX_PANE" in toml["env_vars"]
        assert toml["env"]["MINDFLOCK_PORT"] == "9100"

    def test_pythonpath_only_when_not_installed(self, monkeypatch):
        import sysconfig

        import backend

        root = os.path.dirname(os.path.dirname(os.path.abspath(backend.__file__)))
        monkeypatch.setattr(
            sysconfig, "get_paths", lambda: {"purelib": root, "platlib": root}
        )
        _, _, env = cli._mcp_launch()
        assert "PYTHONPATH" not in env
        monkeypatch.setattr(sysconfig, "get_paths", lambda: {"purelib": "/nowhere"})
        _, _, env = cli._mcp_launch()
        assert env["PYTHONPATH"] == root

    def test_serve_delegates_with_flags(self, monkeypatch):
        from backend import mcp as mcp_server

        seen = {}

        def _main(argv, prog):
            seen["argv"], seen["prog"] = argv, prog
            return 0

        monkeypatch.setattr(mcp_server, "main", _main)
        monkeypatch.setattr(
            client,
            "discover",
            lambda *a, **k: pytest.fail("mcp must not discover up front"),
        )
        assert cli.main(["mcp", "--scope", "readonly", "--port", "9"]) == 0
        assert seen == {
            "argv": ["--scope", "readonly", "--port", "9"],
            "prog": "mindflock mcp",
        }


class TestTemplateCommands:
    """`mindflock run TEMPLATE` (Run now from a shell) and `mindflock templates`."""

    _TEMPLATES = {
        "templates": [
            {
                "name": "Rivian update",
                "repo_path": "/r",
                "prompt": "p",
                "autopilot": "pr",
                "schedule": {"cron": "0 21 * * 1-5"},
            },
            {"name": "docs sweep", "repo_path": "/d", "prompt": "q"},
        ],
        "runs": {
            "rivian update": {
                "schedule_label": "Weekdays at 21:00",
                "next_at": 1792000000,
                "last_at": 1791900000,
                "last_session": "rivian-update-oct13",
            },
            "docs sweep": {"last_error": "repo_path is not a folder"},
        },
        "runs_here": False,
        "runs_on": "mac-mini",
    }

    @pytest.fixture()
    def server(self, monkeypatch):
        calls = []
        monkeypatch.setattr(client, "discover", lambda *a, **k: "http://127.0.0.1:8765")
        monkeypatch.setattr(
            client, "get", lambda base, path, timeout=None: self._TEMPLATES
        )

        def _post(base, path, payload=None, timeout=None):
            calls.append(path)
            if "nope" in path:
                raise client.ApiError(404, "no template named nope")
            return {"title": "rivian-update-oct14"}

        monkeypatch.setattr(client, "post", _post)
        return calls

    def test_run_posts_the_run_route(self, server, capsys):
        assert cli.main(["run", "Rivian update"]) == 0
        assert server == ["/api/templates/Rivian%20update/run"]
        assert (
            "started rivian-update-oct14 from template Rivian update"
            in capsys.readouterr().out
        )

    def test_run_unknown_template_is_one_line_error(self, server, capsys):
        assert cli.main(["run", "nope"]) == 1
        assert "no template named nope" in capsys.readouterr().err

    def test_templates_lists_schedule_and_last_run(self, server, capsys):
        assert cli.main(["templates"]) == 0
        out = capsys.readouterr().out
        assert "Weekdays at 21:00" in out and "rivian-update-oct13" in out
        assert "failed: repo_path is not a folder" in out
        assert "schedules fire on mac-mini" in out

    def test_templates_json(self, server, capsys):
        assert cli.main(["templates", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["runs_on"] == "mac-mini"
