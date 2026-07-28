"""Unit tests for the shared secret resolver (backend.config.secrets)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from backend.config import secrets
from backend.config import settings as S


@pytest.fixture(autouse=True)
def _isolate_store(isolate_settings_store, monkeypatch):
    """Shared settings-store isolation (tests/conftest.py) plus this module's
    own env-var clearing."""
    monkeypatch.delenv("FX_SEC", raising=False)
    monkeypatch.delenv("FX_SEC2", raising=False)


def _set_github_token(tok: str):
    S.save_settings(S.Settings.from_dict({"github": {"token": tok}}))


class TestResolveSecretSync:
    def test_explicit_wins(self, monkeypatch):
        monkeypatch.setenv("FX_SEC", "env-val")
        _set_github_token("settings-val")
        got = secrets.resolve_secret_sync(
            explicit="explicit-val",
            settings_getter=lambda s: s.github.token,
            env_vars=("FX_SEC",),
        )
        assert got == "explicit-val"

    def test_settings_beats_env(self, monkeypatch):
        monkeypatch.setenv("FX_SEC", "env-val")
        _set_github_token("settings-val")
        got = secrets.resolve_secret_sync(
            explicit="",
            settings_getter=lambda s: s.github.token,
            env_vars=("FX_SEC",),
        )
        assert got == "settings-val"

    def test_env_used_when_no_explicit_or_settings(self, monkeypatch):
        monkeypatch.setenv("FX_SEC2", "second-env")
        got = secrets.resolve_secret_sync(
            explicit="",
            settings_getter=lambda s: s.github.token,
            env_vars=("FX_SEC", "FX_SEC2"),
        )
        assert got == "second-env"

    def test_first_env_var_preferred(self, monkeypatch):
        monkeypatch.setenv("FX_SEC", "primary")
        monkeypatch.setenv("FX_SEC2", "secondary")
        got = secrets.resolve_secret_sync(explicit="", env_vars=("FX_SEC", "FX_SEC2"))
        assert got == "primary"

    def test_empty_when_nothing(self):
        assert secrets.resolve_secret_sync(explicit="", env_vars=("FX_SEC",)) == ""

    def test_bad_getter_never_raises(self):
        def boom(_s):
            raise RuntimeError("nope")

        # A broken getter must fall through, not propagate.
        assert secrets.resolve_secret_sync(explicit="", settings_getter=boom) == ""


class TestResolveSecretAsync:
    async def test_explicit_short_circuits_cli(self):
        cli = AsyncMock(return_value="cli-val")
        got = await secrets.resolve_secret(explicit="x", cli_fallback=cli)
        assert got == "x"
        cli.assert_not_awaited()

    async def test_cli_fallback_last_resort(self):
        cli = AsyncMock(return_value="cli-val")
        got = await secrets.resolve_secret(
            explicit="", settings_getter=lambda s: s.github.token, cli_fallback=cli
        )
        assert got == "cli-val"
        cli.assert_awaited_once()

    async def test_settings_beats_cli(self):
        _set_github_token("settings-val")
        cli = AsyncMock(return_value="cli-val")
        got = await secrets.resolve_secret(
            explicit="", settings_getter=lambda s: s.github.token, cli_fallback=cli
        )
        assert got == "settings-val"
        cli.assert_not_awaited()

    async def test_cli_exception_treated_as_empty(self):
        cli = AsyncMock(side_effect=RuntimeError("gh missing"))
        got = await secrets.resolve_secret(explicit="", cli_fallback=cli)
        assert got == ""

    async def test_empty_when_no_layers(self):
        assert await secrets.resolve_secret(explicit="") == ""
