"""Dependency doctor (C1): check logic with mocked probes + the /api/doctor contract."""

from __future__ import annotations

import sys
import types

import pytest
from fastapi.testclient import TestClient

from backend import doctor
from backend.doctor import Check


@pytest.fixture(autouse=True)
def _linux(monkeypatch):
    """Pin the platform so remediation hints are deterministic."""
    monkeypatch.setattr(doctor.osenv, "os_kind", lambda: "linux")


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
        # gh is optional (only GitHub push/PR + PR review need it), so an absent
        # gh is `info` — never `fail`, which would trip the "required dep
        # missing" exit and make gh a de-facto requirement.
        monkeypatch.setattr(doctor.shutil, "which", _which({}))
        c = doctor.check_gh()
        assert c.status == "info"
        assert c.docs  # C6: docs hint for installing gh

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
    def test_missing_claude_suggests_npm_install_when_npm_present(self, monkeypatch):
        monkeypatch.setattr(doctor, "_default_provider_name", lambda: "claude")
        monkeypatch.setattr(doctor, "_resolve_agent_binary", lambda name: "claude")
        monkeypatch.setattr(doctor.shutil, "which", _which({"npm": "/usr/bin/npm"}))
        c = doctor.check_agent_cli()
        assert c.status == "fail"
        assert "npm install -g @anthropic-ai/claude-code" in c.fix
        assert c.cmd == c.fix

    def test_missing_claude_suggests_native_installer_without_npm(self, monkeypatch):
        monkeypatch.setattr(doctor, "_default_provider_name", lambda: "claude")
        monkeypatch.setattr(doctor, "_resolve_agent_binary", lambda name: "claude")
        monkeypatch.setattr(doctor.shutil, "which", _which({}))
        c = doctor.check_agent_cli()
        assert c.status == "fail"
        assert c.fix == "curl -fsSL https://claude.ai/install.sh | sh"
        assert c.cmd == c.fix

    def test_present_is_ok(self, monkeypatch):
        monkeypatch.setattr(doctor, "_default_provider_name", lambda: "claude")
        monkeypatch.setattr(doctor, "_resolve_agent_binary", lambda name: "claude")
        monkeypatch.setattr(
            doctor.shutil, "which", _which({"claude": "/usr/local/bin/claude"})
        )
        c = doctor.check_agent_cli()
        assert c.status == "ok" and c.detail == "/usr/local/bin/claude"

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

    def test_missing_non_claude_binary_has_no_runnable_cmd(self, monkeypatch):
        # A non-claude provider off PATH: we can name the fix but not auto-run it
        # (no vendored installer), so cmd is empty and there is no docs link.
        monkeypatch.setattr(doctor, "_default_provider_name", lambda: "aider")
        monkeypatch.setattr(doctor, "_resolve_agent_binary", lambda name: "aider")
        monkeypatch.setattr(doctor.shutil, "which", _which({}))
        c = doctor.check_agent_cli()
        assert c.status == "fail"
        assert "install `aider`" in c.fix
        assert c.cmd == ""
        assert c.docs == ""


class TestAgentAuth:
    @pytest.fixture(autouse=True)
    def _isolated_home(self, monkeypatch, tmp_path):
        # Point every credential candidate away from the real user's login.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
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

    def test_non_claude_provider_is_info(self, monkeypatch):
        monkeypatch.setattr(doctor, "_default_provider_name", lambda: "aider")
        monkeypatch.setattr(doctor, "_resolve_agent_binary", lambda name: "aider")
        assert doctor.check_agent_auth().status == "info"

    def test_cli_not_installed_cannot_probe_auth(self, monkeypatch):
        # claude selected but not on PATH and no credential evidence: auth is
        # unknowable, so warn with the "not installed" detail (not a login nudge).
        monkeypatch.setattr(doctor.shutil, "which", _which({}))
        c = doctor.check_agent_auth()
        assert c.status == "warn"
        assert "cannot probe auth" in c.detail


class TestOptionalDeps:
    def test_uv_missing_is_warn(self, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", _which({}))
        c = doctor.check_uv()
        assert c.status == "warn"
        assert "astral.sh/uv" in c.fix

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
        monkeypatch.setattr(
            doctor.shutil, "which", _which({"tailscale": "/usr/bin/tailscale"})
        )
        c = doctor.check_tailscale()
        assert c.status == "ok"
        assert c.detail == "/usr/bin/tailscale"


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
