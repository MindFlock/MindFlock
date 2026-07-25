"""Unit tests for the configured-IDE resolver (backend.config.ide)."""

from __future__ import annotations

import pytest

from backend.config import ide as I
from backend.config import settings as S


@pytest.fixture(autouse=True)
def _isolate_store(isolate_settings_store, monkeypatch):
    """Shared settings-store isolation (tests/conftest.py) plus clearing the
    ambient MINDFLOCK_IDE override."""
    monkeypatch.delenv("MINDFLOCK_IDE", raising=False)


class TestResolution:
    def test_default_is_cursor(self):
        assert I.ide_command() == "cursor"
        assert I.ide_argv() == ["cursor"]
        assert I.ide_name() == "Cursor"

    def test_settings_store_wins_over_default(self):
        S.update_settings(platform={"ide_command": "code"})
        assert I.ide_command() == "code"
        assert I.ide_name() == "VS Code"

    def test_env_wins_over_settings(self, monkeypatch):
        S.update_settings(platform={"ide_command": "code"})
        monkeypatch.setenv("MINDFLOCK_IDE", "windsurf")
        assert I.ide_command() == "windsurf"
        assert I.ide_name() == "Windsurf"

    def test_cleared_setting_falls_back_to_default(self):
        S.update_settings(platform={"ide_command": "code"})
        S.update_settings(platform={"ide_command": ""})
        assert I.ide_command() == "cursor"

    def test_whitespace_only_falls_back(self, monkeypatch):
        monkeypatch.setenv("MINDFLOCK_IDE", "   ")
        # env var counts as set but blank -> stripped to default
        assert I.ide_command() == "cursor"


class TestArgv:
    def test_command_with_arguments_is_shlex_split(self, monkeypatch):
        monkeypatch.setenv("MINDFLOCK_IDE", "flatpak run com.visualstudio.code")
        assert I.ide_argv() == ["flatpak", "run", "com.visualstudio.code"]

    def test_absolute_path_basename_drives_identity(self, monkeypatch):
        monkeypatch.setenv("MINDFLOCK_IDE", "/usr/local/bin/code")
        assert I.ide_argv() == ["/usr/local/bin/code"]
        assert I.ide_name() == "VS Code"
        assert I.ide_window_needle() == "Visual Studio Code"

    def test_unbalanced_quotes_do_not_raise(self, monkeypatch):
        monkeypatch.setenv("MINDFLOCK_IDE", 'code "unclosed')
        assert I.ide_argv()  # falls back to the raw string as one token


class TestIdentity:
    @pytest.mark.parametrize(
        "cmd,name,needle,storage",
        [
            ("cursor", "Cursor", "Cursor", "Cursor"),
            ("code", "VS Code", "Visual Studio Code", "Code"),
            (
                "code-insiders",
                "VS Code Insiders",
                "Visual Studio Code - Insiders",
                "Code - Insiders",
            ),
            ("windsurf", "Windsurf", "Windsurf", "Windsurf"),
            ("zed", "Zed", "Zed", None),
        ],
    )
    def test_known_editors(self, monkeypatch, cmd, name, needle, storage):
        monkeypatch.setenv("MINDFLOCK_IDE", cmd)
        assert I.ide_name() == name
        assert I.ide_window_needle() == needle
        assert I.ide_storage_dirname() == storage

    def test_unknown_editor_capitalized_name_no_storage(self, monkeypatch):
        monkeypatch.setenv("MINDFLOCK_IDE", "myeditor")
        assert I.ide_name() == "Myeditor"
        assert I.ide_window_needle() == "Myeditor"
        assert I.ide_storage_dirname() is None


class TestPersistence:
    def test_ide_command_round_trips_through_store(self):
        S.update_settings(platform={"ide_command": "windsurf"})
        S.invalidate()
        assert S.load_settings().platform.ide_command == "windsurf"


class TestRegistry:
    def test_known_specs_cover_editor_families(self):
        commands = {s.command for s in I.known_ide_specs()}
        # VS Code family + other GUI + JetBrains + terminal editors.
        assert {"cursor", "code", "codium", "windsurf", "zed", "subl"} <= commands
        assert {"idea", "pycharm", "webstorm", "goland", "clion"} <= commands
        assert {"nvim", "vim", "emacs", "hx", "micro"} <= commands

    def test_specs_have_valid_kinds(self):
        assert all(s.kind in ("gui", "terminal") for s in I.known_ide_specs())

    def test_terminal_editors_have_no_window_or_storage_capabilities(self):
        for s in I.known_ide_specs():
            if s.kind == "terminal":
                assert s.window_needle is None
                assert s.storage_dirname is None

    def test_jetbrains_are_gui_without_needle_or_storage(self):
        spec = I.spec_for("pycharm")
        assert spec is not None
        assert spec.kind == "gui"
        assert spec.window_needle is None
        assert spec.storage_dirname is None
        assert spec.macos_app == "PyCharm"

    def test_spec_for_normalizes_path_and_case(self):
        assert I.spec_for("/usr/bin/NVIM").command == "nvim"
        assert I.spec_for("no-such-editor") is None

    @pytest.mark.parametrize(
        "cmd,kind",
        [
            ("cursor", "gui"),
            ("pycharm", "gui"),
            ("nvim", "terminal"),
            ("vim", "terminal"),
            ("emacs", "terminal"),
            ("hx", "terminal"),
        ],
    )
    def test_ide_kind_known(self, monkeypatch, cmd, kind):
        monkeypatch.setenv("MINDFLOCK_IDE", cmd)
        assert I.ide_kind() == kind

    def test_ide_kind_unknown_defaults_to_gui(self, monkeypatch):
        monkeypatch.setenv("MINDFLOCK_IDE", "myeditor")
        assert I.ide_kind() == "gui"

    def test_ide_spec_synthesized_for_unknown(self, monkeypatch):
        monkeypatch.setenv("MINDFLOCK_IDE", "myeditor")
        spec = I.ide_spec()
        assert spec.name == "Myeditor"
        assert spec.kind == "gui"
        assert spec.window_needle is None
        assert spec.storage_dirname is None
        assert spec.macos_app is None

    def test_terminal_editor_identity(self, monkeypatch):
        monkeypatch.setenv("MINDFLOCK_IDE", "nvim")
        assert I.ide_name() == "Neovim"
        # No usable needle -> falls back to the display name (matches nothing).
        assert I.ide_window_needle() == "Neovim"
        assert I.ide_storage_dirname() is None

    def test_macos_app_bundles_for_gui_editors(self):
        assert I.spec_for("cursor").macos_app == "Cursor"
        assert I.spec_for("code").macos_app == "Visual Studio Code"
        assert I.spec_for("subl").macos_app == "Sublime Text"
