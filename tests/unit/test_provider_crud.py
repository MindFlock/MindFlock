"""Provider binary-override + registry-rebuild tests (the config/registry layer).

Covers the pieces the Settings provider-management UI builds on:
  * ``binary_override`` precedence (env > settings) and empty-when-unset,
  * ``ProviderConfig.resolved_binary`` (override > binary_path > base_command),
  * ``rebuild_registry`` picking up / dropping a user TOML,
  * the guarantee that ``provisioned._apply_binary_override`` is a no-op with no
    override set (so the launch-parity goldens hold).

HTTP CRUD round-trips live in test_settings_api.py (they exercise the endpoints).
"""

from __future__ import annotations

import os

import pytest

from backend import providers
from backend.providers.base import LaunchContext
from backend.providers.config import (
    ProviderConfig,
    binary_override,
    resolve_provider_binary,
    validate_launch_args,
)
from backend.config import settings as S


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("MINDFLOCK_PROVIDERS_DIR", str(tmp_path / "providers"))
    for var in list(os.environ):
        if var.startswith("MINDFLOCK_PROVIDER_BIN_"):
            monkeypatch.delenv(var, raising=False)
    S.invalidate()
    yield
    S.invalidate()
    providers.rebuild_registry()  # restore a clean registry for other tests


class TestBinaryOverride:
    def test_empty_when_unset(self):
        assert binary_override("aider") == ""

    def test_settings_override(self):
        S.save_settings(
            S.Settings.from_dict(
                {"coding_cli": {"binary_paths": {"aider": "/opt/aider"}}}
            )
        )
        assert binary_override("aider") == "/opt/aider"

    def test_env_beats_settings(self, monkeypatch):
        S.save_settings(
            S.Settings.from_dict(
                {"coding_cli": {"binary_paths": {"aider": "/opt/aider"}}}
            )
        )
        monkeypatch.setenv("MINDFLOCK_PROVIDER_BIN_AIDER", "/env/aider")
        assert binary_override("aider") == "/env/aider"

    def test_env_var_name_slugifies(self, monkeypatch):
        monkeypatch.setenv("MINDFLOCK_PROVIDER_BIN_MY_CLI", "/x/my-cli")
        assert binary_override("my-cli") == "/x/my-cli"


class TestResolvedBinary:
    def test_override_wins(self):
        S.save_settings(
            S.Settings.from_dict(
                {"coding_cli": {"binary_paths": {"codex": "/o/codex"}}}
            )
        )
        cfg = ProviderConfig(
            name="codex", program_aliases=("codex",), binary_path="/toml/codex"
        )
        assert cfg.resolved_binary() == "/o/codex"

    def test_binary_path_beats_base(self):
        cfg = ProviderConfig(
            name="codex", program_aliases=("codex",), binary_path="/toml/codex"
        )
        assert cfg.resolved_binary() == "/toml/codex"

    def test_base_command_default(self):
        cfg = ProviderConfig(
            name="codex", program_aliases=("codex",), command="codex-cli"
        )
        assert cfg.resolved_binary() == "codex-cli"

    def test_name_when_nothing(self):
        cfg = ProviderConfig(name="codex", program_aliases=("codex",))
        assert cfg.resolved_binary() == "codex"

    def test_resolve_provider_binary_by_name(self, monkeypatch):
        monkeypatch.setenv("MINDFLOCK_PROVIDER_BIN_MYCLI", "/m/mycli")
        assert resolve_provider_binary("mycli") == "/m/mycli"
        assert resolve_provider_binary("mycli-unset") == "mycli-unset"


class TestRegistryRebuild:
    def _write_provider(self, tmp_path, name, extra=""):
        d = tmp_path / "providers"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.toml").write_text(
            f'[provider]\nname = "{name}"\nprogram = "{name}"\n{extra}'
        )

    def test_rebuild_picks_up_user_toml(self, tmp_path):
        self._write_provider(tmp_path, "mycli", 'binary_path = "/opt/mycli"\n')
        providers.rebuild_registry()
        p = providers.get("mycli")
        assert p is not None
        assert p.build_launch_command(LaunchContext(program="mycli")) == "/opt/mycli"

    def test_launch_args_are_loaded_and_quoted(self, tmp_path):
        self._write_provider(
            tmp_path,
            "mycli",
            '[launch]\nargs = ["--dangerously-skip-permissions", "--label=a b"]\n',
        )
        providers.rebuild_registry()
        p = providers.get("mycli")
        assert p is not None
        assert (
            p.build_launch_command(LaunchContext(program="mycli"))
            == "mycli --dangerously-skip-permissions '--label=a b'"
        )

    def test_invalid_launch_args_toml_is_skipped(self, tmp_path):
        self._write_provider(tmp_path, "bad", '[launch]\nargs = "--not-a-list"\n')
        providers.rebuild_registry()
        assert providers.get("bad") is None

    def test_per_session_launch_args_follow_provider_defaults(self, tmp_path):
        self._write_provider(tmp_path, "mycli", '[launch]\nargs = ["--default"]\n')
        providers.rebuild_registry()
        p = providers.get("mycli")
        assert p is not None
        cmd = p.build_launch_command(
            LaunchContext(program="mycli", launch_args=("--session-only", "--x=a b"))
        )
        # Provider-level saved defaults come first, then the per-session flags.
        assert cmd == "mycli --default --session-only '--x=a b'"

    def test_rebuild_drops_removed_toml(self, tmp_path):
        self._write_provider(tmp_path, "temp")
        providers.rebuild_registry()
        assert providers.get("temp") is not None
        (tmp_path / "providers" / "temp.toml").unlink()
        providers.rebuild_registry()
        assert providers.get("temp") is None

    def test_order_preserved_fallback_last(self, tmp_path):
        self._write_provider(tmp_path, "zzz")
        providers.rebuild_registry()
        names = [p.name for p in providers.all_providers()]
        assert names[0] == "claude"  # claude first
        assert names[-1] == "generic"  # fallback last
        assert "zzz" in names

    def test_builtin_names_listed(self):
        assert "claude" in providers.BUILTIN_NAMES
        assert "aider" in providers.BUILTIN_NAMES
        assert "generic" in providers.BUILTIN_NAMES


class TestValidateLaunchArgs:
    """validate_launch_args is the security boundary for launch flags: every
    persisted/user-supplied value passes through it before it can reach a shell
    command. It must accept a clean argv list and reject anything ambiguous."""

    def test_accepts_argv_list_and_returns_tuple(self):
        got = validate_launch_args(["--foo", "--label=a b"])
        assert got == ("--foo", "--label=a b")
        assert isinstance(got, tuple)

    def test_none_and_empty_normalize_to_empty_tuple(self):
        assert validate_launch_args(None) == ()
        assert validate_launch_args("") == ()
        assert validate_launch_args([]) == ()
        assert validate_launch_args(()) == ()

    def test_strips_surrounding_whitespace(self):
        assert validate_launch_args(["  --foo  "]) == ("--foo",)

    def test_rejects_bare_string(self):
        # A bare string is ambiguous (word-splitting) — must be a list.
        with pytest.raises(ValueError):
            validate_launch_args("--not-a-list")

    def test_rejects_non_list_shapes(self):
        for bad in (5, {"a": 1}, True):
            with pytest.raises(ValueError):
                validate_launch_args(bad)

    def test_rejects_non_string_element(self):
        with pytest.raises(ValueError):
            validate_launch_args(["--ok", 3])

    def test_rejects_empty_or_whitespace_token(self):
        for bad in (["--ok", ""], ["   "]):
            with pytest.raises(ValueError):
                validate_launch_args(bad)

    def test_rejects_control_characters(self):
        for bad in (["--a\nb"], ["--a\rb"], ["--a\x00b"]):
            with pytest.raises(ValueError):
                validate_launch_args(bad)

    def test_rejects_overly_long_token(self):
        with pytest.raises(ValueError):
            validate_launch_args(["-" + "x" * 512])


class TestLauncherOverrideNoOp:
    """provisioned._apply_binary_override must be a pure no-op with no override —
    this is what keeps the launch-parity goldens byte-identical."""

    def test_no_override_returns_unchanged(self):
        from backend.session import provisioned

        assert provisioned._apply_binary_override("aider --foo") == "aider --foo"
        assert provisioned._apply_binary_override("codex") == "codex"

    def test_override_swaps_only_executable(self, monkeypatch):
        from backend.session import provisioned

        monkeypatch.setenv("MINDFLOCK_PROVIDER_BIN_AIDER", "/opt/aider")
        assert provisioned._apply_binary_override("aider --foo") == "/opt/aider --foo"
