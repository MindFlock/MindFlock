"""Unit tests for the settings resolution accessor (env → settings → toml → default)."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.config import settings as S


@pytest.fixture(autouse=True)
def _isolate_store(isolate_settings_store):
    """Delegate to the shared settings-store isolation (tests/conftest.py)."""


def _set_store(**groups):
    S.save_settings(S.Settings.from_dict(groups))


class TestPrecedenceStr:
    def test_default_when_all_unset(self):
        assert (
            S.resolve_str(
                env="FX_TEST_STR",
                settings_getter=lambda s: s.repository.url,
                default="fallback",
            )
            == "fallback"
        )

    def test_toml_beats_default(self):
        assert (
            S.resolve_str(
                env="FX_TEST_STR",
                settings_getter=lambda s: s.repository.url,
                toml_value="from-toml",
                default="fallback",
            )
            == "from-toml"
        )

    def test_settings_beats_toml(self):
        _set_store(repository={"url": "from-settings"})
        assert (
            S.resolve_str(
                env="FX_TEST_STR",
                settings_getter=lambda s: s.repository.url,
                toml_value="from-toml",
                default="fallback",
            )
            == "from-settings"
        )

    def test_env_beats_settings(self, monkeypatch):
        _set_store(repository={"url": "from-settings"})
        monkeypatch.setenv("FX_TEST_STR", "from-env")
        assert (
            S.resolve_str(
                env="FX_TEST_STR",
                settings_getter=lambda s: s.repository.url,
                toml_value="from-toml",
                default="fallback",
            )
            == "from-env"
        )

    def test_empty_env_is_skipped(self, monkeypatch):
        _set_store(repository={"url": "from-settings"})
        monkeypatch.setenv("FX_TEST_STR", "")  # empty = unset, fall through
        assert (
            S.resolve_str(
                env="FX_TEST_STR",
                settings_getter=lambda s: s.repository.url,
                default="fallback",
            )
            == "from-settings"
        )

    def test_empty_settings_falls_through_to_toml(self):
        _set_store(repository={"url": ""})  # cleared -> not persisted -> unset
        assert (
            S.resolve_str(
                env=None,
                settings_getter=lambda s: s.repository.url,
                toml_value="from-toml",
                default="fallback",
            )
            == "from-toml"
        )


class TestPrecedenceInt:
    def test_toml_int(self):
        assert (
            S.resolve_int(
                env="FX_TEST_INT",
                settings_getter=lambda s: s.github.min_age_minutes,
                toml_value=8765,
                default=None,
            )
            == 8765
        )

    def test_settings_int_beats_toml(self):
        _set_store(github={"min_age_minutes": 9000})
        assert (
            S.resolve_int(
                env="FX_TEST_INT",
                settings_getter=lambda s: s.github.min_age_minutes,
                toml_value=8765,
                default=None,
            )
            == 9000
        )

    def test_env_int_coerced(self, monkeypatch):
        monkeypatch.setenv("FX_TEST_INT", "1234")
        assert (
            S.resolve_int(
                env="FX_TEST_INT",
                settings_getter=lambda s: s.github.min_age_minutes,
                toml_value=8765,
                default=None,
            )
            == 1234
        )

    def test_default_none(self):
        assert (
            S.resolve_int(
                env="FX_TEST_INT",
                settings_getter=lambda s: s.github.min_age_minutes,
                default=None,
            )
            is None
        )


class TestPrecedenceBool:
    def test_settings_bool_beats_toml(self):
        _set_store(github={"enabled": False})
        # False is a real value (not "unset"), so it must win over toml=True.
        assert (
            S.resolve_bool(
                env="FX_TEST_BOOL",
                settings_getter=lambda s: s.github.enabled,
                toml_value=True,
                default=None,
            )
            is False
        )

    def test_env_bool_coerced(self, monkeypatch):
        monkeypatch.setenv("FX_TEST_BOOL", "true")
        assert (
            S.resolve_bool(
                env="FX_TEST_BOOL",
                settings_getter=lambda s: s.github.enabled,
                default=None,
            )
            is True
        )

    def test_none_settings_falls_through(self):
        assert (
            S.resolve_bool(
                env=None,
                settings_getter=lambda s: s.github.enabled,
                toml_value=True,
                default=None,
            )
            is True
        )


class TestPrecedencePath:
    def test_returns_path_from_settings(self):
        _set_store(repository={"workspace_dir": "/tmp/ws"})
        got = S.resolve_path(
            env=None,
            settings_getter=lambda s: s.repository.workspace_dir,
            default=None,
        )
        assert got == Path("/tmp/ws")

    def test_none_when_unset(self):
        assert (
            S.resolve_path(
                env=None,
                settings_getter=lambda s: s.repository.workspace_dir,
                default=None,
            )
            is None
        )

    def test_expands_user(self, monkeypatch):
        monkeypatch.setenv("FX_TEST_PATH", "~/ws")
        got = S.resolve_path(
            env="FX_TEST_PATH",
            settings_getter=lambda s: s.repository.workspace_dir,
            default=None,
        )
        assert str(got).startswith(str(Path.home()))
