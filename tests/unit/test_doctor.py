"""Dependency doctor (C1): check logic with mocked probes + the /api/doctor contract."""

from __future__ import annotations

import sys
import types

import pytest
from fastapi.testclient import TestClient

from backend import doctor
from backend.doctor import Check
from backend.providers import claude as claude_provider
from backend.providers.base import BaseProvider


@pytest.fixture(autouse=True)
def _linux(monkeypatch):
    """Pin the platform so remediation hints are deterministic."""
    monkeypatch.setattr(doctor.osenv, "os_kind", lambda: "linux")


@pytest.fixture(autouse=True)
def _no_path_probe(monkeypatch):
    """``?refresh=1`` re-reads PATH (a login-shell probe that edits
    os.environ) — record the calls instead."""
    from backend import pathenv

    calls = []
    monkeypatch.setattr(pathenv, "refresh", lambda: calls.append(1) or ())
    return calls


#: codex's install hint (OpenAI's native installer).
CODEX_INSTALL = (
    "curl -fsSL https://chatgpt.com/codex/install.sh | CODEX_NON_INTERACTIVE=1 sh"
)


def _which(mapping):
    """A shutil.which stand-in from a {name: path-or-None} mapping."""
    return lambda name: mapping.get(name)


class TestProbeHelpers:
    def test_run_missing_binary_returns_none_never_raises(self):
        # A missing binary raises FileNotFoundError inside subprocess.run;
        # _run must swallow it and degrade to (None, "").
        code, out = doctor._run(["mindflock-nonexistent-binary-xyz-123"])
        assert code is None
        assert out == ""

    def test_run_merges_stdout_stderr_and_strips(self):
        # A real (guaranteed-present) interpreter: stdout+stderr are concatenated.
        code, out = doctor._run(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('out '); sys.stderr.write('err\\n')",
            ]
        )
        assert code == 0
        assert out == "out err"  # concatenated, trailing whitespace stripped

    def test_parse_version_extracts_tuple(self):
        assert doctor._parse_version("git version 2.43.1") == (2, 43, 1)
        assert doctor._parse_version("tmux 3.4") == (3, 4)

    def test_parse_version_no_match_is_empty(self):
        assert doctor._parse_version("no digits here") == ()

    def test_pkg_fix_windows_points_to_wsl(self, monkeypatch):
        monkeypatch.setattr(doctor.osenv, "os_kind", lambda: "windows")
        fix = doctor._pkg_fix("git")
        assert "WSL" in fix and "not a supported" in fix

    def test_pkg_fix_pacman_and_zypper(self, monkeypatch):
        monkeypatch.setattr(doctor.osenv, "os_kind", lambda: "linux")
        monkeypatch.setattr(
            doctor.shutil, "which", _which({"pacman": "/usr/bin/pacman"})
        )
        assert doctor._pkg_fix("tmux") == "sudo pacman -S tmux"
        monkeypatch.setattr(
            doctor.shutil, "which", _which({"zypper": "/usr/bin/zypper"})
        )
        assert doctor._pkg_fix("tmux") == "sudo zypper install tmux"


class TestDefaultProviderResolution:
    def test_settings_default_provider_wins(self, monkeypatch):
        import backend.config.settings as settings_mod

        fake = types.SimpleNamespace(
            coding_cli=types.SimpleNamespace(default_provider="mycli")
        )
        monkeypatch.setattr(settings_mod, "load_settings", lambda: fake)
        assert doctor._default_provider_name() == "mycli"

    def test_settings_failure_falls_back_to_providers_default(self, monkeypatch):
        import backend.config.settings as settings_mod
        from backend import providers

        def boom():
            raise RuntimeError("settings unavailable")

        monkeypatch.setattr(settings_mod, "load_settings", boom)
        assert doctor._default_provider_name() == providers.DEFAULT_PROVIDER

    def test_both_sources_failing_defaults_to_claude(self, monkeypatch):
        import backend.config.settings as settings_mod
        from backend import providers

        def boom():
            raise RuntimeError("settings unavailable")

        monkeypatch.setattr(settings_mod, "load_settings", boom)
        # With no provider registry default either, the last resort is "claude".
        monkeypatch.delattr(providers, "DEFAULT_PROVIDER", raising=False)
        assert doctor._default_provider_name() == "claude"

    def test_resolve_agent_binary_swallows_registry_errors(self, monkeypatch):
        from backend import providers

        def boom(name):
            raise RuntimeError("provider registry broken")

        monkeypatch.setattr(providers, "get", boom)
        # A broken registry must degrade to the bare provider name, not crash.
        assert doctor._resolve_agent_binary("claude") == "claude"

    def test_resolve_agent_binary_uses_real_registry(self):
        # Happy path through the real provider registry + config resolver.
        result = doctor._resolve_agent_binary("claude")
        assert isinstance(result, str) and result


class TestGit:
    def test_ok_reports_version(self, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", _which({"git": "/usr/bin/git"}))
        monkeypatch.setattr(doctor, "_run", lambda argv: (0, "git version 2.43.0"))
        c = doctor.check_git()
        assert c.status == "ok"
        assert "2.43.0" in c.detail

    def test_missing_is_optional_info_with_apt_fix(self, monkeypatch):
        # Git is optional: sessions run in plain folders without it, so a
        # missing binary is informational (with the fix), never a failure.
        monkeypatch.setattr(doctor.shutil, "which", _which({}))
        c = doctor.check_git()
        assert c.status == "info"
        assert "optional" in c.detail
        assert "apt install git" in c.fix

    def test_missing_on_macos_suggests_xcode(self, monkeypatch):
        monkeypatch.setattr(doctor.osenv, "os_kind", lambda: "macos")
        monkeypatch.setattr(doctor.shutil, "which", _which({}))
        assert "xcode-select" in doctor.check_git().fix

    def test_too_old_is_fail(self, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", _which({"git": "/usr/bin/git"}))
        monkeypatch.setattr(doctor, "_run", lambda argv: (0, "git version 2.10.0"))
        c = doctor.check_git()
        assert c.status == "fail"
        assert "too old" in c.detail
        assert "2.17" in c.detail  # names the minimum


class TestTmux:
    def test_missing_is_fail(self, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", _which({}))
        c = doctor.check_tmux()
        assert c.status == "fail"
        assert c.fix == "sudo apt install tmux"

    def test_macos_fix_uses_brew(self, monkeypatch):
        monkeypatch.setattr(doctor.osenv, "os_kind", lambda: "macos")
        monkeypatch.setattr(doctor.shutil, "which", _which({}))
        assert doctor.check_tmux().fix == "brew install tmux"

    def test_present_is_ok(self, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", _which({"tmux": "/usr/bin/tmux"}))
        monkeypatch.setattr(doctor, "_run", lambda argv: (0, "tmux 3.4"))
        c = doctor.check_tmux()
        assert c.status == "ok" and c.detail == "tmux 3.4"

    def test_too_old_is_fail(self, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", _which({"tmux": "/usr/bin/tmux"}))
        monkeypatch.setattr(doctor, "_run", lambda argv: (0, "tmux 2.1"))
        c = doctor.check_tmux()
        assert c.status == "fail"
        assert "too old" in c.detail
        assert "2.4" in c.detail


class TestGh:
    def test_missing_is_info_optional(self, monkeypatch):
        # gh is optional (only PR create/merge + PR review need it), so an absent
        # gh is `info` — never `fail`, which would trip the "required dep
        # missing" exit and make gh a de-facto requirement.
        monkeypatch.setattr(doctor.shutil, "which", _which({}))
        c = doctor.check_gh()
        assert c.status == "info"
        assert c.docs  # C6: docs hint for installing gh

    def test_missing_detail_does_not_claim_push_needs_gh(self, monkeypatch):
        # The exact printed wording, asserted here because TROUBLESHOOTING.md is
        # indexed against it — and because the old copy said "GitHub push/PR",
        # which is what made contributors on SSH remotes think gh was mandatory.
        # Pushing is plain `git push`; gh is never in that path.
        monkeypatch.setattr(doctor.shutil, "which", _which({}))
        assert doctor.check_gh().detail == (
            "not found (optional — only PR create/merge and PR review need it; "
            "pushing uses plain git)"
        )

    def test_missing_gh_never_fails_the_payload_ok_flag(self, monkeypatch):
        # Belt-and-braces at the payload level: `ok` is "no fails", and an absent
        # gh must never be one of them. Anything else makes gh a hard requirement
        # in practice — the installer runs `mindflock doctor` and honours exit 1.
        monkeypatch.setattr(doctor.shutil, "which", _which({}))
        gh = doctor.CHECKS_BY_ID["gh"]()
        assert gh.status == "info"
        assert doctor.to_payload([gh])["ok"] is True

    def test_unauthenticated_is_warn_not_fail(self, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", _which({"gh": "/usr/bin/gh"}))
        monkeypatch.setattr(
            doctor,
            "_run",
            lambda argv: (1, "You are not logged into any GitHub hosts."),
        )
        c = doctor.check_gh()
        assert c.status == "warn"
        assert "gh auth login" in c.fix

    def test_authenticated_is_ok(self, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", _which({"gh": "/usr/bin/gh"}))
        monkeypatch.setattr(doctor, "_run", lambda argv: (0, "Logged in to github.com"))
        assert doctor.check_gh().status == "ok"


class TestAgentCli:
    @pytest.mark.parametrize("which", [{"npm": "/usr/bin/npm"}, {}])
    def test_missing_claude_suggests_the_native_installer(self, monkeypatch, which):
        # With or without npm: the native installer, piped to bash (it is a
        # bash script; `| sh` is dash on Debian/Ubuntu/WSL and dies parsing it).
        monkeypatch.setattr(doctor, "_default_provider_name", lambda: "claude")
        monkeypatch.setattr(doctor, "_resolve_agent_binary", lambda name: "claude")
        monkeypatch.setattr(doctor.shutil, "which", _which(which))
        c = doctor.check_agent_cli()
        assert c.status == "fail"
        assert c.fix == "curl -fsSL https://claude.ai/install.sh | bash"
        assert c.cmd == c.fix and c.install is True
        assert c.provider == "claude"

    def test_present_is_ok(self, monkeypatch):
        monkeypatch.setattr(doctor, "_default_provider_name", lambda: "claude")
        monkeypatch.setattr(doctor, "_resolve_agent_binary", lambda name: "claude")
        monkeypatch.setattr(
            doctor.shutil, "which", _which({"claude": "/usr/local/bin/claude"})
        )
        monkeypatch.setattr(doctor, "_run", lambda argv: (None, ""))
        c = doctor.check_agent_cli()
        assert c.status == "ok" and c.detail == "/usr/local/bin/claude"

    def test_present_shows_the_cli_version(self, monkeypatch):
        monkeypatch.setattr(doctor, "_default_provider_name", lambda: "claude")
        monkeypatch.setattr(doctor, "_resolve_agent_binary", lambda name: "claude")
        monkeypatch.setattr(
            doctor.shutil, "which", _which({"claude": "/usr/local/bin/claude"})
        )
        seen = []
        monkeypatch.setattr(
            doctor,
            "_run",
            lambda argv: seen.append(argv) or (0, "2.1.295 (Claude Code)\n"),
        )
        c = doctor.check_agent_cli()
        assert seen == [["/usr/local/bin/claude", "--version"]]
        assert c.detail == "/usr/local/bin/claude — 2.1.295 (Claude Code)"

    @pytest.mark.parametrize(
        "answer", [(1, "2.1.0"), (0, "Welcome! Starting UI"), (None, "")]
    )
    def test_a_version_probe_that_fails_reports_just_the_path(
        self, monkeypatch, answer
    ):
        monkeypatch.setattr(doctor, "_default_provider_name", lambda: "claude")
        monkeypatch.setattr(doctor, "_resolve_agent_binary", lambda name: "claude")
        monkeypatch.setattr(
            doctor.shutil, "which", _which({"claude": "/usr/local/bin/claude"})
        )
        monkeypatch.setattr(doctor, "_run", lambda argv: answer)
        assert doctor.check_agent_cli().detail == "/usr/local/bin/claude"

    def test_a_provider_without_version_args_is_never_run(self, monkeypatch):
        monkeypatch.setattr(doctor, "_default_provider_name", lambda: "antigravity")
        monkeypatch.setattr(doctor, "_resolve_agent_binary", lambda name: "agy")
        monkeypatch.setattr(doctor.shutil, "which", _which({"agy": "/usr/bin/agy"}))

        def _never(argv):
            raise AssertionError("agy must not be run")

        monkeypatch.setattr(doctor, "_run", _never)
        assert doctor.check_agent_cli().detail == "/usr/bin/agy"

    def test_broken_path_override_is_fail(self, monkeypatch, tmp_path):
        monkeypatch.setattr(doctor, "_default_provider_name", lambda: "codex")
        monkeypatch.setattr(
            doctor, "_resolve_agent_binary", lambda name: str(tmp_path / "nope")
        )
        c = doctor.check_agent_cli()
        assert c.status == "fail"
        assert "Settings" in c.fix

    def test_executable_path_override_is_ok(self, monkeypatch, tmp_path):
        binary = tmp_path / "mycli"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        monkeypatch.setattr(doctor, "_default_provider_name", lambda: "custom")
        monkeypatch.setattr(doctor, "_resolve_agent_binary", lambda name: str(binary))
        c = doctor.check_agent_cli()
        assert c.status == "ok"
        assert c.detail == str(binary)

    def test_missing_non_claude_binary_installs_with_its_own_installer(
        self, monkeypatch
    ):
        # Any agent you pick is installable, not just claude: the command comes
        # from the provider (aider's own installer), and it joins the one-shot
        # install plan.
        monkeypatch.setattr(doctor, "_default_provider_name", lambda: "aider")
        monkeypatch.setattr(doctor, "_resolve_agent_binary", lambda name: "aider")
        monkeypatch.setattr(doctor.shutil, "which", _which({}))
        c = doctor.check_agent_cli()
        assert c.status == "fail"
        assert c.cmd == "curl -LsSf https://aider.chat/install.sh | sh"
        assert c.fix == c.cmd
        assert c.install is True
        assert c.docs == ""

    def test_codex_default_installs_codex_not_claude(self, monkeypatch):
        monkeypatch.setattr(doctor, "_default_provider_name", lambda: "codex")
        monkeypatch.setattr(doctor, "_resolve_agent_binary", lambda name: "codex")
        monkeypatch.setattr(doctor.shutil, "which", _which({}))
        c = doctor.check_agent_cli()
        assert c.label == "agent CLI (codex)"
        assert c.cmd == CODEX_INSTALL
        assert "claude" not in c.cmd

    def test_provider_without_installer_has_no_runnable_cmd(self, monkeypatch):
        # A custom CLI that names no installer: we can say what's wrong, but
        # guessing a package for it would install the wrong thing.
        monkeypatch.setattr(doctor, "_default_provider_name", lambda: "mycli")
        monkeypatch.setattr(doctor, "_resolve_agent_binary", lambda name: "mycli")
        monkeypatch.setattr(doctor, "_agent_install_cmd", lambda name, binary: "")
        monkeypatch.setattr(doctor.shutil, "which", _which({}))
        c = doctor.check_agent_cli()
        assert c.status == "fail"
        assert "install `mycli`" in c.fix
        assert c.cmd == "" and c.install is False


class TestAssistantCli:
    def test_unset_or_same_as_default_is_not_reported(self, monkeypatch):
        monkeypatch.setattr(doctor, "_default_provider_name", lambda: "codex")
        monkeypatch.setattr(doctor, "_assistant_provider_name", lambda: "")
        assert doctor.check_assistant_cli() is None
        monkeypatch.setattr(doctor, "_assistant_provider_name", lambda: "codex")
        assert doctor.check_assistant_cli() is None

    def test_a_different_missing_assistant_cli_is_a_warn_install(self, monkeypatch):
        monkeypatch.setattr(doctor, "_default_provider_name", lambda: "claude")
        monkeypatch.setattr(doctor, "_assistant_provider_name", lambda: "codex")
        monkeypatch.setattr(doctor, "_resolve_agent_binary", lambda name: name)
        monkeypatch.setattr(doctor.shutil, "which", _which({}))
        c = doctor.check_assistant_cli()
        assert c.id == "assistant-cli" and c.status == "warn"
        assert c.cmd == CODEX_INSTALL and c.install

    def test_a_none_check_is_left_out_of_the_report(self, monkeypatch):
        monkeypatch.setattr(
            doctor, "_ALL_CHECKS", [lambda: Check("a", "a", "ok"), lambda: None]
        )
        assert [c.id for c in doctor.run_checks()] == ["a"]


class TestAgentAuth:
    @pytest.fixture(autouse=True)
    def _isolated_home(self, monkeypatch, tmp_path):
        # Point every credential candidate away from the real user's login. The
        # non-Anthropic keys matter as much as ANTHROPIC_API_KEY now that the
        # check asks each provider: aider counts any of these as a login, so a
        # developer with OPENAI_API_KEY exported in their shell would see the
        # "looks logged out" tests pass for the wrong reason.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        for var in (
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "GEMINI_API_KEY",
            "DEEPSEEK_API_KEY",
        ):
            monkeypatch.delenv(var, raising=False)
        # A Mac running the suite has a real Claude login in its Keychain, which
        # is genuine evidence — pin it off so the verdicts under test come only
        # from the files/env this fixture controls.
        monkeypatch.setattr(claude_provider, "_keychain_login_evidence", lambda: False)
        monkeypatch.setattr(doctor, "_default_provider_name", lambda: "claude")
        monkeypatch.setattr(doctor, "_resolve_agent_binary", lambda name: "claude")

    def test_oauth_evidence_is_ok(self, monkeypatch, tmp_path):
        cfg = tmp_path / "cfg"
        cfg.mkdir()
        (cfg / ".claude.json").write_text('{"oauthAccount": {"email": "e@x"}}')
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
        c = doctor.check_agent_auth()
        assert c.status == "ok"
        assert ".claude.json" in c.detail

    def test_api_key_env_is_ok(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
        assert doctor.check_agent_auth().status == "ok"

    def test_no_evidence_is_warn_with_login_fix(self, monkeypatch):
        monkeypatch.setattr(
            doctor.shutil, "which", _which({"claude": "/usr/bin/claude"})
        )
        c = doctor.check_agent_auth()
        assert c.status == "warn"
        assert "run `claude` once" in c.fix

    def test_provider_with_declared_credentials_gets_a_real_verdict(self, monkeypatch):
        # aider names the API-key env vars it authenticates with, so it is
        # probeable: no key set and the CLI on PATH is a genuine "looks logged
        # out" warn, not the old "no auth probe for aider — skipped" shrug.
        monkeypatch.setattr(doctor, "_default_provider_name", lambda: "aider")
        monkeypatch.setattr(doctor, "_resolve_agent_binary", lambda name: "aider")
        monkeypatch.setattr(doctor.shutil, "which", _which({"aider": "/usr/bin/aider"}))
        c = doctor.check_agent_auth()
        assert c.status == "warn"
        assert "no sign of a login" in c.detail

    def test_codex_credential_file_is_ok(self, monkeypatch, tmp_path):
        # A provider that declares credential FILES (codex: ~/.codex/auth.json)
        # is answered by the file existing — no env key needed.
        (tmp_path / ".codex").mkdir()
        (tmp_path / ".codex" / "auth.json").write_text("{}")
        monkeypatch.setattr(doctor, "_default_provider_name", lambda: "codex")
        monkeypatch.setattr(doctor, "_resolve_agent_binary", lambda name: "codex")
        monkeypatch.setattr(doctor.shutil, "which", _which({"codex": "/usr/bin/codex"}))
        c = doctor.check_agent_auth()
        assert c.status == "ok"
        assert "auth.json" in c.detail

    def test_provider_declaring_no_credential_sources_never_nags(self, monkeypatch):
        # goose keeps its credentials somewhere MindFlock does not read, so
        # finding nothing proves nothing: info with no fix, even with the CLI off
        # PATH, because a warn here could never be cleared.
        monkeypatch.setattr(doctor, "_default_provider_name", lambda: "goose")
        monkeypatch.setattr(doctor, "_resolve_agent_binary", lambda name: "goose")
        monkeypatch.setattr(doctor.shutil, "which", _which({}))
        c = doctor.check_agent_auth()
        assert c.status == "info"
        assert "no login probe" in c.detail
        assert c.fix == "" and c.cmd == ""

    def test_login_fix_uses_the_providers_own_login_command(self, monkeypatch):
        # codex logs in with `codex login`, so the fix says exactly that —
        # "run `codex login` once to log in" would be redundant — and cmd carries
        # it so `doctor --fix` can offer to run it.
        monkeypatch.setattr(doctor, "_default_provider_name", lambda: "codex")
        monkeypatch.setattr(doctor, "_resolve_agent_binary", lambda name: "codex")
        monkeypatch.setattr(doctor.shutil, "which", _which({"codex": "/usr/bin/codex"}))
        c = doctor.check_agent_auth()
        assert c.status == "warn"
        assert c.fix == "run `codex login`"
        assert c.cmd == "codex login"

    def test_inherited_login_command_is_never_offered_to_run(self, monkeypatch):
        # aider names the API keys it reads but has no login flow, so
        # BaseProvider.login_command hands back the bare program name. Offering
        # THAT is how `doctor --fix` and the init wizard came to print
        # "run `aider`?" and hand their terminal to aider's own REPL, which
        # authenticates nothing: cmd must stay empty while the fix line still
        # says what would clear the warn.
        monkeypatch.setattr(doctor, "_default_provider_name", lambda: "aider")
        monkeypatch.setattr(doctor, "_resolve_agent_binary", lambda name: "aider")
        monkeypatch.setattr(doctor.shutil, "which", _which({"aider": "/usr/bin/aider"}))
        c = doctor.check_agent_auth()
        assert c.status == "warn"
        assert c.cmd == ""
        assert "run `aider`" not in c.fix
        assert "ANTHROPIC_API_KEY" in c.fix

    def test_claude_still_offers_its_first_run_sign_in(self, monkeypatch):
        # The other side of the same rule: ClaudeProvider overrides
        # login_command deliberately because `claude` prompts to sign in on
        # first run, so running it IS the login flow and stays runnable.
        monkeypatch.setattr(
            doctor.shutil, "which", _which({"claude": "/usr/bin/claude"})
        )
        c = doctor.check_agent_auth()
        assert c.status == "warn"
        assert c.cmd == "claude"
        assert "run `claude` once" in c.fix

    def test_config_declared_bare_command_is_still_offered(self, monkeypatch):
        # A config that names its login command as data means it even when the
        # command is just the program (antigravity signs in through Google on
        # first run), so it is offered — the gate is "declared", not "has args".
        provider = types.SimpleNamespace(
            cfg=types.SimpleNamespace(
                login_command="agy", auth_env=("AGY_KEY",), auth_files=()
            ),
            auth_evidence=lambda: "",
        )
        monkeypatch.setattr(doctor, "_default_provider_name", lambda: "antigravity")
        monkeypatch.setattr(doctor, "_resolve_agent_binary", lambda name: "agy")
        monkeypatch.setattr(doctor, "_agent_provider", lambda name: provider)
        monkeypatch.setattr(doctor.shutil, "which", _which({"agy": "/usr/bin/agy"}))
        c = doctor.check_agent_auth()
        assert c.status == "warn"
        assert c.fix == "run `agy` once to log in"
        assert c.cmd == "agy"

    def test_hand_written_provider_declares_by_overriding(self, monkeypatch):
        # Classes carrying no cfg (claude's shape) declare a login flow by
        # overriding the base method: the override is offered, and a sibling
        # that only overrides the auth probe gets no runnable command at all.
        class _Declares(BaseProvider):
            name = "custom"
            program_aliases = ("custom",)

            def auth_evidence(self) -> str:
                return ""

            def login_command(self):
                return "custom signin"

        class _Inherits(BaseProvider):
            name = "custom"
            program_aliases = ("custom",)

            def auth_evidence(self) -> str:
                return ""

        monkeypatch.setattr(doctor, "_default_provider_name", lambda: "custom")
        monkeypatch.setattr(doctor, "_resolve_agent_binary", lambda name: "custom")
        monkeypatch.setattr(
            doctor.shutil, "which", _which({"custom": "/usr/bin/custom"})
        )
        monkeypatch.setattr(doctor, "_agent_provider", lambda name: _Declares())
        declared = doctor.check_agent_auth()
        monkeypatch.setattr(doctor, "_agent_provider", lambda name: _Inherits())
        inherited = doctor.check_agent_auth()
        assert declared.cmd == "custom signin"
        assert declared.fix == "run `custom signin` once to log in"
        assert inherited.status == "warn"
        assert inherited.cmd == ""
        assert inherited.fix == "log `custom` in from inside the CLI itself"

    def test_cli_not_installed_cannot_probe_auth(self, monkeypatch):
        # claude selected but not on PATH and no credential evidence: auth is
        # unknowable, so warn with the "not installed" detail (not a login nudge).
        monkeypatch.setattr(doctor.shutil, "which", _which({}))
        c = doctor.check_agent_auth()
        assert c.status == "warn"
        assert "cannot probe auth" in c.detail

    def test_unregistered_provider_cannot_be_probed(self, monkeypatch):
        # A provider name left in settings after its TOML was deleted: there is
        # nobody to ask, so say so and stay out of the way.
        monkeypatch.setattr(doctor, "_default_provider_name", lambda: "ghost-cli")
        monkeypatch.setattr(doctor, "_resolve_agent_binary", lambda name: "ghost-cli")
        c = doctor.check_agent_auth()
        assert c.status == "info"
        assert "not a registered provider" in c.detail


class TestOptionalDeps:
    def test_uv_missing_is_warn(self, monkeypatch):
        from backend import _pins

        monkeypatch.setattr(doctor.shutil, "which", _which({}))
        c = doctor.check_uv()
        assert c.status == "warn" and c.install is True
        # The pinned installer install.sh uses — never an unpinned curl | sh.
        assert f"astral.sh/uv/{_pins.UV_PINNED_VERSION}/install.sh" in c.cmd
        assert _pins.UV_INSTALLER_SHA256 in c.cmd
        assert "install.sh | sh" not in c.cmd  # downloaded, verified, then run

    @pytest.mark.parametrize("good", [True, False])
    def test_uv_fix_runs_the_installer_only_when_its_checksum_matches(
        self, monkeypatch, tmp_path, good
    ):
        """Run the real fix command under dash/sh with a stub ``curl`` that
        "downloads" a script; it must run exactly when the sha256 matches."""
        import hashlib
        import subprocess

        from backend import _pins

        payload = f"touch {tmp_path}/ran\n"
        sha = hashlib.sha256(payload.encode()).hexdigest()
        monkeypatch.setattr(_pins, "UV_INSTALLER_SHA256", sha if good else "0" * 64)
        bindir = tmp_path / "bin"
        bindir.mkdir()
        src = tmp_path / "installer.sh"
        src.write_text(payload)
        curl = bindir / "curl"
        # curl -LsSf -o <out> <url>
        curl.write_text(f'#!/bin/sh\ncp {src} "$3"\n')
        curl.chmod(0o755)
        cmd = doctor._uv_install_cmd()
        env = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(tmp_path)}
        proc = subprocess.run(
            ["sh", "-c", cmd], env=env, capture_output=True, text=True
        )
        assert (tmp_path / "ran").exists() is good
        assert (proc.returncode == 0) is good
        if not good:
            assert "checksum mismatch" in proc.stderr

    def test_uv_present_is_ok(self, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", _which({"uv": "/usr/bin/uv"}))
        monkeypatch.setattr(doctor, "_run", lambda argv: (0, "uv 0.4.0"))
        c = doctor.check_uv()
        assert c.status == "ok"
        assert c.detail == "uv 0.4.0"

    def test_tailscale_missing_is_info_only(self, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", _which({}))
        c = doctor.check_tailscale()
        assert c.status == "info"
        assert "optional" in c.detail

    def test_tailscale_present_is_ok(self, monkeypatch):
        from backend import tailscale_cli

        monkeypatch.setattr(
            doctor.shutil, "which", _which({"tailscale": "/usr/bin/tailscale"})
        )
        monkeypatch.setattr(
            tailscale_cli,
            "status_json",
            lambda fresh=False: {
                "BackendState": "Running",
                "CurrentTailnet": {"Name": "me@example.com", "MagicDNSEnabled": True},
                "CertDomains": ["box.tail.ts.net"],
                "Self": {"HostName": "box", "DNSName": "box.tail.ts.net."},
            },
        )
        c = doctor.check_tailscale()
        assert c.status == "ok"
        assert c.detail == "/usr/bin/tailscale — me@example.com · box.tail.ts.net"

    def test_tailscale_signed_out_is_warn_not_ok(self, monkeypatch):
        # Present is not working: a logged-out client used to be a ✓.
        from backend import tailscale_cli

        monkeypatch.setattr(
            doctor.shutil, "which", _which({"tailscale": "/usr/bin/tailscale"})
        )
        monkeypatch.setattr(
            tailscale_cli,
            "status_json",
            lambda fresh=False: {"BackendState": "NeedsLogin", "Self": {}},
        )
        c = doctor.check_tailscale()
        assert c.status == "warn"
        assert "signed in" in c.detail
        assert c.fix == "sudo tailscale up --operator=$USER"
        assert not c.install

    def test_tailscale_macos_fix_is_the_app_not_the_daemon(self, monkeypatch):
        monkeypatch.setattr(doctor.osenv, "os_kind", lambda: "macos")
        monkeypatch.setattr(doctor.shutil, "which", _which({}))
        from backend import tailscale_cli

        monkeypatch.setattr(tailscale_cli, "APP_BUNDLE_CLI", "/nonexistent/Tailscale")
        c = doctor.check_tailscale()
        assert c.cmd == "brew install --cask tailscale-app"
        assert "tailscale.com/download/mac" in c.fix
        assert "brew install tailscale " not in c.fix + " "

    def test_tailscale_macos_app_bundle_is_installed(self, monkeypatch, tmp_path):
        # The App Store / Standalone app puts nothing on PATH: its CLI is the
        # bundle binary, which must count as installed (not "Install").
        from backend import tailscale_cli

        cli = tmp_path / "Tailscale"
        cli.write_text("#!/bin/sh\n")
        cli.chmod(0o755)
        monkeypatch.setattr(doctor.osenv, "os_kind", lambda: "macos")
        monkeypatch.setattr(doctor.shutil, "which", _which({}))
        monkeypatch.setattr(tailscale_cli, "APP_BUNDLE_CLI", str(cli))
        monkeypatch.setattr(
            tailscale_cli,
            "status_json",
            lambda fresh=False: {
                "BackendState": "Running",
                "Self": {"HostName": "mac"},
            },
        )
        c = doctor.check_tailscale()
        assert c.status == "ok"
        assert c.detail.startswith(str(cli) + " (Tailscale app)")

    def test_tailscale_wsl_windows_only_warns_with_wsl_guidance(
        self, monkeypatch, tmp_path
    ):
        from backend import tailscale_cli

        exe = tmp_path / "tailscale.exe"
        exe.write_text("")
        monkeypatch.setattr(doctor.osenv, "os_kind", lambda: "wsl")
        monkeypatch.setattr(doctor.shutil, "which", _which({}))
        monkeypatch.setattr(tailscale_cli, "WINDOWS_CANDIDATES", (str(exe),))
        c = doctor.check_tailscale()
        assert c.status == "warn"
        assert "on Windows" in c.detail and "WSL" in c.detail
        assert "tailscale up --hostname=" in c.fix
        assert not c.install  # a second node is offered, never auto-installed

    def test_tailscale_missing_but_wanted_joins_the_install_plan(self, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", _which({}))
        monkeypatch.setattr(doctor, "_tailscale_wanted", lambda: True)
        c = doctor.check_tailscale()
        assert c.status == "warn" and c.install
        assert c.cmd == "curl -fsSL https://tailscale.com/install.sh | sh"


class TestClipboard:
    def test_linux_with_xclip_is_ok(self, monkeypatch):
        # autouse _linux fixture already pins os_kind == "linux".
        monkeypatch.setattr(doctor.shutil, "which", _which({"xclip": "/usr/bin/xclip"}))
        c = doctor.check_clipboard()
        assert c.status == "ok"
        assert c.detail == "/usr/bin/xclip"

    def test_linux_without_backend_is_info(self, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", _which({}))
        c = doctor.check_clipboard()
        assert c.status == "info"
        assert "no xclip/xsel" in c.detail
        assert "xclip" in c.fix  # names the install command

    def test_non_linux_has_builtin_backend(self, monkeypatch):
        monkeypatch.setattr(doctor.osenv, "os_kind", lambda: "macos")
        c = doctor.check_clipboard()
        assert c.status == "ok"
        assert "built-in" in c.detail


class TestCacheSeeds:
    """A seed the refresher stopped publishing sent every ticket workspace
    through the full testmon suite for a month, logged only as an hourly
    ERROR in the ingestion log — the doctor has to say it out loud."""

    def _settings(self, monkeypatch, tmp_path, **cache_kw):
        from backend.session import provisioned
        from backend.workspace_setup import CacheSeed

        kw = dict(
            name="testmon",
            seed_path=tmp_path / "seed",
            workspace_path=".testmondata",
            refresh_branch="staging",
            refresh_interval_seconds=3600,
            refresh_command="pytest --testmon",
        )
        kw.update(cache_kw)
        settings = provisioned.ProvisionSettings(
            repo_url="git@x:o/r.git",
            workspace_dir=tmp_path / "ws",
            caches=[CacheSeed(**kw)],
        )
        monkeypatch.setattr(provisioned, "load_provision_settings", lambda: settings)
        return settings

    def _age(self, path, seconds):
        import os
        import time

        path.write_bytes(b"x")
        t = time.time() - seconds
        os.utime(path, (t, t))

    def test_no_caches_is_info(self, monkeypatch):
        from backend.session import provisioned

        monkeypatch.setattr(provisioned, "load_provision_settings", lambda: None)
        c = doctor.check_cache_seeds()
        assert (c.id, c.status) == ("cache-seeds", "info")

    def test_refresh_disabled_cache_is_not_judged(self, monkeypatch, tmp_path):
        self._settings(monkeypatch, tmp_path, refresh_enabled=False)
        assert doctor.check_cache_seeds().status == "info"

    def test_fresh_seed_is_ok(self, monkeypatch, tmp_path):
        s = self._settings(monkeypatch, tmp_path)
        self._age(s.caches[0].seed_path, 3600)
        c = doctor.check_cache_seeds()
        assert c.status == "ok"
        assert "testmon" in c.detail

    def test_missing_seed_warns(self, monkeypatch, tmp_path):
        self._settings(monkeypatch, tmp_path)
        c = doctor.check_cache_seeds()
        assert c.status == "warn"
        assert "no seed" in c.detail and c.fix

    def test_stale_seed_warns_with_age(self, monkeypatch, tmp_path):
        s = self._settings(monkeypatch, tmp_path)
        self._age(s.caches[0].seed_path, 32 * 86400)
        c = doctor.check_cache_seeds()
        assert c.status == "warn"
        assert "32d old" in c.detail and "full suite" in c.detail

    def test_staleness_has_a_floor_for_short_intervals(self, monkeypatch, tmp_path):
        # 3 x 60s would flag a single slow refresh; the floor keeps it quiet.
        s = self._settings(monkeypatch, tmp_path, refresh_interval_seconds=60)
        self._age(s.caches[0].seed_path, 2 * 3600)
        assert doctor.check_cache_seeds().status == "ok"

    def test_reports_how_far_behind_the_refresher_is(self, monkeypatch, tmp_path):
        import subprocess

        def git(cwd, *args):
            subprocess.run(
                ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                cwd=cwd,
                check=True,
                capture_output=True,
            )

        s = self._settings(monkeypatch, tmp_path)
        self._age(s.caches[0].seed_path, 32 * 86400)
        forge = tmp_path / "forge"
        forge.mkdir()
        git(forge, "init", "-q", "-b", "staging")
        git(forge, "commit", "-q", "--allow-empty", "-m", "one")
        refresher = s.workspace_dir / "_testmon_refresher"
        s.workspace_dir.mkdir()
        git(s.workspace_dir, "clone", "-q", str(forge), str(refresher))
        for msg in ("two", "three"):
            git(forge, "commit", "-q", "--allow-empty", "-m", msg)
        git(refresher, "fetch", "-q", "origin")

        c = doctor.check_cache_seeds()
        assert "2 commits behind origin/staging" in c.detail

    def test_registered_for_run_and_fix(self):
        assert doctor.CHECKS_BY_ID["cache-seeds"] is doctor.check_cache_seeds


class TestRunner:
    def test_a_raising_check_degrades_to_warn(self, monkeypatch):
        def boom():
            raise RuntimeError("kaput")

        monkeypatch.setattr(
            doctor, "_ALL_CHECKS", [lambda: Check("a", "a", "ok"), boom]
        )
        checks = doctor.run_checks()
        assert [c.status for c in checks] == ["ok", "warn"]
        assert "kaput" in checks[1].detail

    def test_payload_ok_flag(self):
        assert (
            doctor.to_payload([Check("a", "a", "ok"), Check("b", "b", "warn")])["ok"]
            is True
        )
        assert doctor.to_payload([Check("a", "a", "fail")])["ok"] is False


class TestDoctorApi:
    def test_endpoint_contract_and_cache(self, monkeypatch):
        from backend.web import server

        fake = [
            Check("git", "git", "ok", "git version 2.43.0"),
            Check("tmux", "tmux", "fail", "not found on PATH", "sudo apt install tmux"),
        ]
        monkeypatch.setattr(doctor, "run_checks", lambda: fake)
        client = TestClient(server.app)

        data = client.get("/api/doctor", params={"refresh": 1}).json()
        assert data["ok"] is False
        assert [c["id"] for c in data["checks"]] == ["git", "tmux"]
        assert set(data["checks"][0]) == {
            "id",
            "label",
            "status",
            "detail",
            "fix",
            "docs",
            "cmd",
            "pkg",
            "install",
            "provider",
        }

        # Without ?refresh the cached payload is served (run_checks not re-run).
        monkeypatch.setattr(doctor, "run_checks", lambda: [Check("x", "x", "ok")])
        again = client.get("/api/doctor").json()
        assert [c["id"] for c in again["checks"]] == ["git", "tmux"]

    def test_addon_in_manifest(self):
        from backend.web import server

        client = TestClient(server.app)
        addons = {a["id"]: a for a in client.get("/api/addons").json()["addons"]}
        assert "doctor" in addons
        assert addons["doctor"]["frontend"][0]["where"] == "settings"

    def test_payload_reports_the_engine_version(self, monkeypatch):
        """The desktop shell reads this to detect app/engine drift."""
        from backend import __version__
        from backend.web import server

        monkeypatch.setattr(doctor, "run_checks", lambda: [])
        client = TestClient(server.app)

        data = client.get("/api/doctor", params={"refresh": 1}).json()

        assert data["version"] == __version__

    def test_ack_clears_the_state_notice_and_the_cache(self, monkeypatch):
        from backend.config import state as state_mod
        from backend.web import server

        monkeypatch.setattr(doctor, "run_checks", lambda: [])
        monkeypatch.setattr(
            state_mod,
            "downgrade_notice",
            lambda: {"file_version": 9, "supported_version": 1, "backup_path": "/x"},
        )
        client = TestClient(server.app)
        assert client.get("/api/doctor", params={"refresh": 1}).json()["state_notice"]

        # Acknowledging must survive a reload, so the cached payload holding the
        # notice has to be dropped along with the notice itself.
        cleared = {"done": False}
        monkeypatch.setattr(
            state_mod,
            "clear_downgrade_notice",
            lambda: cleared.__setitem__("done", True),
        )
        monkeypatch.setattr(state_mod, "downgrade_notice", lambda: None)

        assert client.post("/api/doctor/ack-state-notice").json() == {"ok": True}
        assert cleared["done"] is True
        assert client.get("/api/doctor").json()["state_notice"] is None


class TestDoctorAddonStartup:
    """The best-effort startup print of failed checks (interactive launch only)."""

    async def test_prints_only_failed_checks_on_a_tty(self, monkeypatch, capsys):
        import sys

        from backend.web.addons.doctor import DoctorAddon

        addon = DoctorAddon()
        monkeypatch.setattr(
            addon,
            "_payload",
            lambda: {
                "checks": [
                    {
                        "status": "fail",
                        "label": "tmux",
                        "detail": "not found",
                        "fix": "apt install tmux",
                    },
                    {"status": "ok", "label": "git", "detail": "2.43", "fix": ""},
                ]
            },
        )
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
        await addon.on_startup(None)
        out = capsys.readouterr().out
        assert "doctor: tmux — not found" in out
        assert "(fix: apt install tmux)" in out
        assert "git" not in out  # passing checks are never printed

    async def test_silent_and_skips_probes_when_not_a_tty(self, monkeypatch, capsys):
        import sys

        from backend.web.addons.doctor import DoctorAddon

        addon = DoctorAddon()
        ran = []
        monkeypatch.setattr(addon, "_payload", lambda: ran.append(1) or {"checks": []})
        monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
        await addon.on_startup(None)
        assert capsys.readouterr().out == ""
        assert ran == []  # off a tty the subprocess probes are never run

    async def test_startup_never_raises(self, monkeypatch):
        import sys

        from backend.web.addons.doctor import DoctorAddon

        addon = DoctorAddon()

        def _boom():
            raise RuntimeError("doctor exploded")

        monkeypatch.setattr(addon, "_payload", _boom)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
        await addon.on_startup(None)  # a doctor failure must not break startup


class TestDoctorAddonPayloadCache:
    def test_caches_until_refresh(self, monkeypatch):
        from backend.web.addons.doctor import DoctorAddon

        addon = DoctorAddon()
        runs = []
        monkeypatch.setattr(doctor, "run_checks", lambda: runs.append(1) or [])
        monkeypatch.setattr(doctor, "to_payload", lambda checks: {"checks": []})

        addon._payload()
        addon._payload()
        assert len(runs) == 1  # second call served from cache
        addon._payload(refresh=True)
        assert len(runs) == 2  # refresh forces a re-probe


class TestBwrap:
    @pytest.fixture()
    def sandbox(self, monkeypatch):
        from backend.peer import sandbox as sb

        state = {"available": (False, "nope"), "found": None}
        monkeypatch.setattr(sb, "available", lambda: state["available"])
        monkeypatch.setattr(sb, "find_bwrap", lambda: state["found"])
        return state

    def test_working_sandbox_is_ok(self, monkeypatch, sandbox):
        sandbox["available"] = (True, "/usr/bin/bwrap")
        c = doctor.check_bwrap()
        assert c.status == "ok" and c.detail == "/usr/bin/bwrap"

    def test_missing_with_peer_links_on_is_a_batched_install(
        self, monkeypatch, sandbox
    ):
        monkeypatch.setattr(doctor, "_peer_settings", lambda: {"enabled": True})
        monkeypatch.setattr(doctor, "_linux_pkg_manager", lambda: "apt")
        c = doctor.check_bwrap()
        assert c.status == "warn"
        assert c.pkg == "bubblewrap" and c.install is True
        assert c.cmd == "sudo apt install bubblewrap"

    def test_missing_with_peer_links_off_is_info_and_not_installed(
        self, monkeypatch, sandbox
    ):
        monkeypatch.setattr(doctor, "_peer_settings", lambda: {})
        c = doctor.check_bwrap()
        assert c.status == "info" and c.install is False
        assert "optional" in c.detail

    def test_present_but_broken_offers_no_reinstall(self, monkeypatch, sandbox):
        # Installed, but the kernel/AppArmor refuses user namespaces:
        # reinstalling can't fix that, so there's nothing to run.
        monkeypatch.setattr(doctor, "_peer_settings", lambda: {"enabled": True})
        sandbox["available"] = (False, "bwrap self-test failed: no userns")
        sandbox["found"] = "/usr/bin/bwrap"
        c = doctor.check_bwrap()
        assert c.status == "warn" and "self-test" in c.detail
        assert c.cmd == "" and c.install is False

    def test_macos_is_info_with_nothing_to_install(self, monkeypatch):
        monkeypatch.setattr(doctor.osenv, "os_kind", lambda: "macos")
        c = doctor.check_bwrap()
        assert c.status == "info" and not c.cmd and not c.install

    def test_registered(self):
        assert doctor.CHECKS_BY_ID["bwrap"] is doctor.check_bwrap


class TestCloudflared:
    @pytest.fixture(autouse=True)
    def _absent(self, monkeypatch):
        from backend.peer import tunnel

        monkeypatch.setattr(tunnel, "find_cloudflared", lambda configured="": None)

    def test_apt_installs_cloudflares_own_deb(self, monkeypatch):
        monkeypatch.setattr(doctor, "_linux_pkg_manager", lambda: "apt")
        monkeypatch.setattr("platform.machine", lambda: "x86_64")
        monkeypatch.setattr(doctor, "_peer_settings", lambda: {"relay": "cloudflare"})
        c = doctor.check_cloudflared()
        assert c.status == "warn" and c.install is True and c.pkg == ""
        assert "cloudflared-linux-amd64.deb" in c.cmd
        assert "dpkg -i" in c.cmd
        # Its exit status is the install's: no trailing command can mask a
        # failed download.
        assert c.cmd.endswith("&& rm -f /tmp/cloudflared.deb")

    def test_macos_uses_brew_package(self, monkeypatch):
        monkeypatch.setattr(doctor.osenv, "os_kind", lambda: "macos")
        monkeypatch.setattr(doctor, "_peer_settings", lambda: {"relay": "cloudflare"})
        c = doctor.check_cloudflared()
        assert c.cmd == "brew install cloudflared" and c.pkg == "cloudflared"

    def test_not_wanted_without_the_cloudflare_relay(self, monkeypatch):
        monkeypatch.setattr(doctor, "_peer_settings", lambda: {"relay": "off"})
        c = doctor.check_cloudflared()
        assert c.status == "info" and c.install is False


class TestInstallPlan:
    def test_packages_go_into_one_manager_run_before_the_installers(self, monkeypatch):
        monkeypatch.setattr(doctor, "_linux_pkg_manager", lambda: "apt")
        checks = [
            Check(
                "tmux",
                "tmux",
                "fail",
                cmd="sudo apt install tmux",
                pkg="tmux",
                install=True,
            ),
            Check("uv", "uv", "warn", cmd="curl uv | sh", install=True),
            Check("bwrap", "sandbox", "warn", cmd="x", pkg="bubblewrap", install=True),
        ]
        plan = doctor.install_plan(checks)
        assert [s["id"] for s in plan["steps"]] == ["packages", "uv"]
        assert plan["steps"][0]["cmd"] == (
            "sudo apt-get update; sudo apt-get install -y tmux bubblewrap"
        )
        assert plan["packages"] == ["tmux", "bubblewrap"]

    def test_logins_optional_extras_and_healthy_checks_stay_out(self):
        checks = [
            Check("gh", "gh", "warn", cmd="gh auth login"),  # a login, not an install
            Check("tailscale", "tailscale", "info", cmd="curl ts | sh"),  # optional
            Check("git", "git", "ok", pkg="git", install=True),  # already fine
        ]
        plan = doctor.install_plan(checks)
        assert plan == {"steps": [], "packages": [], "script": ""}

    @pytest.mark.parametrize(
        "mgr,line",
        [
            ("dnf", "sudo dnf install -y a b"),
            ("pacman", "sudo pacman -S --needed --noconfirm a b"),
            ("zypper", "sudo zypper --non-interactive install a b"),
        ],
    )
    def test_each_package_manager_runs_non_interactively(self, monkeypatch, mgr, line):
        monkeypatch.setattr(doctor, "_linux_pkg_manager", lambda: mgr)
        assert doctor._pkg_install_line(["a", "b"]) == line

    def test_script_runs_every_step_and_reports_the_failures(self, tmp_path):
        # A real run: a failing step must not stop the next one, and the script
        # exits non-zero naming exactly what failed.
        import subprocess

        marker = tmp_path / "ran"
        checks = [
            Check("a", "first tool", "fail", cmd="false", install=True),
            Check("b", "second tool", "fail", cmd=f"touch {marker}", install=True),
        ]
        script = doctor.install_plan(checks)["script"]
        proc = subprocess.run(["sh", "-c", script], capture_output=True, text=True)
        assert proc.returncode == 1
        assert marker.exists()
        assert "- first tool" in proc.stdout
        assert "- second tool" not in proc.stdout

    def test_script_succeeds_when_every_step_does(self):
        import subprocess

        checks = [Check("a", "it's a tool", "fail", cmd="true", install=True)]
        script = doctor.install_plan(checks)["script"]
        proc = subprocess.run(["sh", "-c", script], capture_output=True, text=True)
        assert proc.returncode == 0
        assert "everything installed" in proc.stdout

    def test_payload_carries_the_steps_but_never_the_script(self):
        payload = doctor.to_payload(
            [Check("uv", "uv", "warn", cmd="curl uv | sh", install=True)]
        )
        assert payload["install"]["steps"] == [
            {"id": "uv", "label": "uv", "cmd": "curl uv | sh"}
        ]
        assert "script" not in payload["install"]


class TestNode:
    """Node.js is in the plan only as a means to an npm-only agent CLI."""

    def _agent(self, monkeypatch, name="cline", which=None):
        monkeypatch.setattr(doctor, "_default_provider_name", lambda: name)
        monkeypatch.setattr(doctor, "_assistant_provider_name", lambda: "")
        monkeypatch.setattr(doctor, "_resolve_agent_binary", lambda n: n)
        monkeypatch.setattr(doctor.shutil, "which", _which(which or {}))

    def test_no_row_when_no_missing_agent_installs_with_npm(self, monkeypatch):
        self._agent(monkeypatch, "claude")
        assert doctor.check_node() is None
        self._agent(monkeypatch, "cline", {"cline": "/x/cline"})  # installed
        assert doctor.check_node() is None

    def test_missing_npm_joins_the_package_run_before_the_npm_step(self, monkeypatch):
        self._agent(monkeypatch, "cline")
        monkeypatch.setattr(doctor, "_linux_pkg_manager", lambda: "apt")
        node = doctor.check_node()
        assert node.status == "warn" and node.install is True
        assert node.pkg == "nodejs npm" and "cline" in node.detail
        plan = doctor.install_plan([node, doctor.check_agent_cli()])
        assert [s["id"] for s in plan["steps"]] == ["packages", "agent-cli"]
        assert "nodejs npm" in plan["steps"][0]["cmd"]
        assert plan["steps"][1]["cmd"].startswith("npm install -g --prefix ~/.local")

    def test_present_npm_is_ok(self, monkeypatch):
        self._agent(monkeypatch, "cline", {"npm": "/usr/bin/npm"})
        assert doctor.check_node().status == "ok"

    def test_a_windows_npm_seen_through_wsl_does_not_count(self, monkeypatch):
        monkeypatch.setattr(doctor.osenv, "os_kind", lambda: "wsl")
        self._agent(monkeypatch, "cline", {"npm": "/mnt/c/Program Files/nodejs/npm"})
        node = doctor.check_node()
        assert node.status == "warn" and node.install is True
        assert "/mnt/" in node.detail

    def test_macos_installs_node_with_brew(self, monkeypatch):
        monkeypatch.setattr(doctor.osenv, "os_kind", lambda: "macos")
        self._agent(monkeypatch, "cline")
        assert doctor.check_node().pkg == "node"

    def test_the_assistant_cli_counts_too(self, monkeypatch):
        self._agent(monkeypatch, "claude", {"claude": "/x/claude"})
        monkeypatch.setattr(doctor, "_assistant_provider_name", lambda: "cline")
        assert doctor.check_node().install is True


class TestHomebrewBootstrap:
    """A fresh Mac has no Homebrew, and every package is a `brew install`."""

    def _tmux_missing(self):
        return Check("tmux", "tmux", "fail", pkg="tmux", install=True)

    def test_homebrew_goes_first_when_missing(self, monkeypatch):
        monkeypatch.setattr(doctor.osenv, "os_kind", lambda: "macos")
        monkeypatch.setattr(doctor.shutil, "which", _which({}))
        monkeypatch.setattr(doctor.os.path, "isfile", lambda p: False)
        plan = doctor.install_plan([self._tmux_missing()])
        assert [s["id"] for s in plan["steps"]] == ["homebrew", "packages"]
        brew = plan["steps"][0]["cmd"]
        assert "Homebrew/install/HEAD/install.sh" in brew
        assert "NONINTERACTIVE=1" in brew and brew.startswith("sudo -v")
        # The package run finds the brew that step just installed.
        pkgs = plan["steps"][1]["cmd"]
        assert "brew shellenv" in pkgs and pkgs.endswith("brew install tmux")

    def test_no_bootstrap_when_brew_exists_off_path(self, monkeypatch):
        monkeypatch.setattr(doctor.osenv, "os_kind", lambda: "macos")
        monkeypatch.setattr(doctor.shutil, "which", _which({}))
        monkeypatch.setattr(
            doctor.os.path, "isfile", lambda p: p == "/opt/homebrew/bin/brew"
        )
        plan = doctor.install_plan([self._tmux_missing()])
        assert [s["id"] for s in plan["steps"]] == ["packages"]
        assert "brew shellenv" in plan["steps"][0]["cmd"]

    def test_brew_on_path_is_used_plainly(self, monkeypatch):
        monkeypatch.setattr(doctor.osenv, "os_kind", lambda: "macos")
        monkeypatch.setattr(
            doctor.shutil, "which", _which({"brew": "/opt/homebrew/bin/brew"})
        )
        plan = doctor.install_plan([self._tmux_missing()])
        assert plan["steps"] == [
            {
                "id": "packages",
                "label": "system packages: tmux",
                "cmd": "brew install tmux",
            }
        ]

    def test_no_bootstrap_without_a_package_to_install(self, monkeypatch):
        monkeypatch.setattr(doctor.osenv, "os_kind", lambda: "macos")
        monkeypatch.setattr(doctor.shutil, "which", _which({}))
        monkeypatch.setattr(doctor.os.path, "isfile", lambda p: False)
        plan = doctor.install_plan([Check("uv", "uv", "warn", cmd="x", install=True)])
        assert [s["id"] for s in plan["steps"]] == ["uv"]

    def test_linux_never_bootstraps_homebrew(self, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", _which({}))
        plan = doctor.install_plan([self._tmux_missing()])
        assert [s["id"] for s in plan["steps"]] == ["packages"]


class TestPathRefresh:
    """A re-probe after an install re-reads PATH first (pathenv.refresh)."""

    def test_refresh_param_refreshes_path(self, monkeypatch, _no_path_probe):
        from backend.web.addons.doctor import DoctorAddon

        addon = DoctorAddon()
        monkeypatch.setattr(doctor, "run_checks", lambda: [])
        addon._payload()
        assert _no_path_probe == []
        addon._payload(refresh=True)
        assert _no_path_probe == [1]

    def test_install_finishing_refreshes_path_once(self, monkeypatch, _no_path_probe):
        from backend.web import server
        from backend.web.core import setup_install

        st = {"running": True, "exit_code": None}
        monkeypatch.setattr(setup_install, "state", lambda: dict(st))
        c = TestClient(server.app)
        c.get("/api/doctor/install-state")
        assert _no_path_probe == []
        st.update(running=False, exit_code=0)
        c.get("/api/doctor/install-state")
        c.get("/api/doctor/install-state")  # polled again: no second refresh
        assert _no_path_probe == [1]
