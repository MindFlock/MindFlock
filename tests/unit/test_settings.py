"""Unit tests for the user settings store (backend.config.settings)."""

from __future__ import annotations

import json
import os
import stat

import pytest

from backend.config import settings as S


@pytest.fixture(autouse=True)
def _isolate_store(isolate_settings_store):
    """Delegate to the shared settings-store isolation (tests/conftest.py).

    These tests read/write the store at its exact path (``tmp_path /
    "settings.json"``), which the shared fixture guarantees."""


class TestLoadEmpty:
    def test_missing_file_yields_all_empty(self):
        s = S.load_settings()
        assert s.ticketing.sources == []
        assert s.repository.url == ""
        assert s.coding_cli.binary_paths == {}
        assert s.ui.scroll_speed is None

    def test_corrupt_file_yields_all_empty(self, tmp_path):
        (tmp_path / "settings.json").write_text("this is not json {{{")
        S.invalidate()
        s = S.load_settings()
        assert s.ticketing.sources == []
        assert s.engine.enabled is None

    def test_non_object_root_yields_all_empty(self, tmp_path):
        (tmp_path / "settings.json").write_text("[1, 2, 3]")
        S.invalidate()
        s = S.load_settings()
        assert s.repository.url == ""


class TestRoundTrip:
    def test_save_then_load(self):
        s = S.Settings()
        s.ticketing.sources = [
            S.TicketingSource(
                id="sc",
                provider="shortcut",
                api_token="sc_secret",
                member_id="member-123",
            )
        ]
        s.repository.url = "git@github.com:org/repo.git"
        s.coding_cli.binary_paths = {"claude": "/usr/bin/claude"}
        s.ui.scroll_speed = 7
        s.engine.enabled = True
        S.save_settings(s)

        S.invalidate()
        got = S.load_settings()
        assert got.ticketing.sources[0].api_token == "sc_secret"
        assert got.ticketing.sources[0].member_id == "member-123"
        assert got.repository.url == "git@github.com:org/repo.git"
        assert got.coding_cli.binary_paths == {"claude": "/usr/bin/claude"}
        assert got.ui.scroll_speed == 7
        assert got.engine.enabled is True

    def test_to_dict_omits_empty_groups(self):
        s = S.Settings()
        s.github.token = "x"
        d = s.to_dict()
        assert d == {"github": {"token": "x"}}
        # empty groups absent entirely
        assert "repository" not in d
        assert "coding_cli" not in d

    def test_empty_binary_path_dropped(self):
        s = S.Settings()
        s.coding_cli.binary_paths = {"claude": "", "codex": "/bin/codex"}
        d = s.to_dict()
        assert d["coding_cli"]["binary_paths"] == {"codex": "/bin/codex"}

    def test_default_launch_args_round_trip(self):
        # Per-provider map: each CLI's flags are stored under its provider name.
        s = S.Settings()
        s.coding_cli.default_launch_args = {
            "claude": "--dangerously-skip-permissions --foo",
            "codex": "--search",
        }
        S.save_settings(s)
        S.invalidate()
        got = S.load_settings()
        assert got.coding_cli.default_launch_args == {
            "claude": "--dangerously-skip-permissions --foo",
            "codex": "--search",
        }
        assert (
            got.coding_cli.launch_args_for("claude")
            == "--dangerously-skip-permissions --foo"
        )
        assert got.coding_cli.launch_args_for("codex") == "--search"
        assert got.coding_cli.launch_args_for("aider") == ""  # unset provider
        # Empty map is omitted from the on-disk shape.
        assert "default_launch_args" not in S.Settings().to_dict().get("coding_cli", {})

    def test_default_launch_args_update_via_settings(self):
        S.update_settings(coding_cli={"default_launch_args": {"claude": "--yolo"}})
        assert S.load_settings().coding_cli.default_launch_args == {"claude": "--yolo"}
        # A blank flags string for a provider drops that entry on the round trip.
        S.update_settings(coding_cli={"default_launch_args": {"claude": ""}})
        assert S.load_settings().coding_cli.default_launch_args == {}

    def test_written_file_is_0600(self, tmp_path):
        s = S.Settings()
        s.github.token = "sec"
        S.save_settings(s)
        path = tmp_path / "settings.json"
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600, "settings.json must be owner-only (holds secrets)"

    def test_no_tmp_leftovers(self, tmp_path):
        S.save_settings(S.Settings.from_dict({"github": {"repos": ["o/r"]}}))
        leftovers = [
            p.name for p in tmp_path.iterdir() if p.name.startswith(".settings.")
        ]
        assert leftovers == []

    def test_content_is_valid_json_with_trailing_newline(self, tmp_path):
        S.save_settings(S.Settings.from_dict({"github": {"repos": ["o/r"]}}))
        raw = (tmp_path / "settings.json").read_text()
        assert raw.endswith("\n")
        assert json.loads(raw) == {"github": {"repos": ["o/r"]}}


class TestUpdateSettings:
    def test_partial_update_preserves_other_groups(self):
        S.save_settings(
            S.Settings.from_dict(
                {"general": {"auth_token": "keep"}, "repository": {"url": "u"}}
            )
        )
        S.update_settings(github={"token": "ghp_x"})
        got = S.load_settings()
        assert got.general.auth_token == "keep"  # untouched
        assert got.repository.url == "u"  # untouched
        assert got.github.token == "ghp_x"  # added

    def test_empty_value_clears_field(self):
        S.save_settings(S.Settings.from_dict({"github": {"token": "old"}}))
        S.update_settings(github={"token": ""})
        assert S.load_settings().github.token == ""

    def test_clearing_last_field_drops_group(self):
        S.save_settings(S.Settings.from_dict({"github": {"token": "old"}}))
        S.update_settings(github={"token": ""})
        # group with no remaining fields should not persist
        assert "github" not in json.loads(S.settings_path().read_text())


class TestOptionalFieldSerialization:
    """Every optional field must serialize when set and survive a round trip —
    these lock the on-disk shape of the less-exercised groups."""

    def test_coding_cli_launch_args_for_blank_provider(self):
        assert S.CodingCliSettings().launch_args_for("") == ""

    def test_ticketing_source_numeric_fields_round_trip(self):
        src = S.TicketingSource(
            id="j", provider="jira", workflow_state_id=42, poll_interval_seconds=30
        )
        d = src.to_dict()
        assert d["workflow_state_id"] == 42 and d["poll_interval_seconds"] == 30
        assert S.TicketingSource.from_dict(d).workflow_state_id == 42

    def test_repository_branch_fields(self):
        d = S.RepositorySettings(base_branch="main", pr_base_branch="staging").to_dict()
        assert d == {"base_branch": "main", "pr_base_branch": "staging"}

    def test_github_issue_tuning_fields(self):
        d = S.GithubSettings(
            issue_min_age_minutes=5,
            issue_poll_interval_seconds=15,
            issue_skip_authors=["bot"],
        ).to_dict()
        assert d["issue_min_age_minutes"] == 5
        assert d["issue_poll_interval_seconds"] == 15
        assert d["issue_skip_authors"] == ["bot"]

    def test_engine_fields(self):
        d = S.EngineSettings(
            enabled=True, mode="worktree", open_cursor=False, skip_permissions=True
        ).to_dict()
        assert d == {
            "enabled": True,
            "mode": "worktree",
            "open_cursor": False,
            "skip_permissions": True,
        }

    def test_ui_and_platform_fields(self):
        assert S.UiSettings(
            scroll_speed=3, cursor_autoadopt=True, accent="a", surface="s"
        ).to_dict() == {
            "scroll_speed": 3,
            "cursor_autoadopt": True,
            "accent": "a",
            "surface": "s",
        }
        assert S.PlatformSettings(
            wsl_distro="Ubuntu", wt_command="wt.exe", ide_command="code"
        ).to_dict() == {
            "wsl_distro": "Ubuntu",
            "wt_command": "wt.exe",
            "ide_command": "code",
        }

    def test_general_fields_round_trip(self):
        g = S.GeneralSettings(
            session_budget_usd=1.5,
            window_budget_usd=20.0,
            auth_mode="on",
            onboarded=True,
            remote_control="on",
            serve_mode="tailscale",
            ingestion_autostart=False,
        )
        d = g.to_dict()
        assert d["session_budget_usd"] == 1.5 and d["window_budget_usd"] == 20.0
        assert d["auth_mode"] == "on" and d["onboarded"] is True
        assert d["remote_control"] == "on" and d["serve_mode"] == "tailscale"
        assert d["ingestion_autostart"] is False
        back = S.GeneralSettings.from_dict(d)
        assert back.serve_mode == "tailscale" and back.ingestion_autostart is False

    def test_set_ticketing_sources_empty_drops_group(self):
        S.save_settings(
            S.Settings.from_dict(
                {"ticketing": {"sources": [{"id": "x", "provider": "jira"}]}}
            )
        )
        S.set_ticketing_sources([])  # empty list clears the whole group
        assert S.load_settings().ticketing.sources == []
        assert "ticketing" not in json.loads(S.settings_path().read_text())


class TestSaveDurability:
    """The atomic write hardens durability by flushing + fsyncing the temp fd
    before the rename, and it must never leave a temp file behind on failure."""

    def test_fsync_called_before_replace(self, monkeypatch):
        # Spy on os.fsync and os.replace to prove the fd is fsynced before the
        # rename swaps it into place (crash-durability of the atomic write).
        order = []
        real_fsync = os.fsync
        real_replace = os.replace

        def _spy_fsync(fd):
            order.append("fsync")
            return real_fsync(fd)

        def _spy_replace(src, dst):
            order.append("replace")
            return real_replace(src, dst)

        monkeypatch.setattr(os, "fsync", _spy_fsync)
        monkeypatch.setattr(os, "replace", _spy_replace)
        S.save_settings(S.Settings.from_dict({"github": {"repos": ["o/r"]}}))
        assert order == ["fsync", "replace"], "fsync must precede the rename"

    def test_write_failure_cleans_up_temp_and_raises(self, tmp_path, monkeypatch):
        # If the rename fails, the temp file is unlinked and the error surfaces
        # (regression guard around the new flush/fsync block).
        def _boom(src, dst):
            raise OSError("replace failed")

        monkeypatch.setattr(os, "replace", _boom)
        with pytest.raises(OSError):
            S.save_settings(S.Settings.from_dict({"github": {"repos": ["o/r"]}}))
        leftovers = [
            p.name for p in tmp_path.iterdir() if p.name.startswith(".settings.")
        ]
        assert leftovers == []  # no temp litter
        assert not (tmp_path / "settings.json").exists()  # no partial target


class TestCoercionHelpers:
    def test_opt_int_blank_is_none(self):
        assert S._opt_int("") is None
        assert S._opt_int("   ") is None
        assert S._opt_int("7") == 7

    def test_opt_float_variants(self):
        assert S._opt_float(None) is None
        assert S._opt_float(True) is None  # a bool is never a numeric value here
        assert S._opt_float(2) == 2.0
        assert S._opt_float("3.5") == 3.5
        assert S._opt_float("") is None
        assert S._opt_float("nope") is None

    def test_opt_bool_numeric_and_unknown(self):
        assert S._opt_bool(1) is True
        assert S._opt_bool(0) is False
        assert S._opt_bool("weird") is None


class TestTolerantParsing:
    def test_unknown_keys_ignored(self):
        s = S.Settings.from_dict(
            {"github": {"token": "t", "bogus": 1}, "mystery_group": {"x": 1}}
        )
        assert s.github.token == "t"

    def test_bad_types_coerce_to_unset(self):
        s = S.Settings.from_dict(
            {
                "github": {"min_age_minutes": "not-a-number"},
                "ui": {"scroll_speed": "abc"},
            }
        )
        assert s.github.min_age_minutes is None
        assert s.ui.scroll_speed is None

    def test_bool_coercion_variants(self):
        s = S.Settings.from_dict({"engine": {"enabled": "yes", "open_cursor": "0"}})
        assert s.engine.enabled is True
        assert s.engine.open_cursor is False
