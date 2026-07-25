"""Unit tests for the Settings addon's pure helpers (no HTTP layer).

Targets the masking / resolution helpers in ``backend.web.addons.settings`` that
the endpoint contract tests don't reach directly: binary resolution, ticketing
token masking, the masked settings view, the ``config``-from-body builder shared
by the Test/States endpoints, the gh-CLI status mapping, and ``_apply_post``'s
ticketing-skip + non-dict-group guards.
"""

from __future__ import annotations

import pytest

from backend.config import settings as S
from backend.doctor import Check
from backend.web.addons import settings as SA


@pytest.fixture(autouse=True)
def _iso(isolate_settings_store):
    """Shared settings-store isolation (tests/conftest.py)."""


class TestInstalledPath:
    def test_empty_binary_is_not_installed(self):
        assert SA._installed_path("") == ""

    def test_explicit_executable_path_used_directly(self, tmp_path):
        exe = tmp_path / "mycli"
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        assert SA._installed_path(str(exe)) == str(exe)

    def test_explicit_non_executable_path_rejected(self, tmp_path):
        plain = tmp_path / "plain"
        plain.write_text("x")  # no +x bit
        assert SA._installed_path(str(plain)) == ""

    def test_bare_name_resolved_on_path(self, monkeypatch):
        monkeypatch.setattr(
            SA.shutil, "which", lambda n: "/usr/bin/git" if n == "git" else None
        )
        assert SA._installed_path("git") == "/usr/bin/git"
        assert SA._installed_path("nope") == ""


class TestMaskTicketing:
    def test_non_dict_ticketing_is_noop(self):
        d = {"ticketing": "not-a-dict"}
        SA._mask_ticketing(d)  # must not raise
        assert d["ticketing"] == "not-a-dict"

    def test_present_token_masked_absent_blanked(self):
        d = {
            "ticketing": {
                "sources": [{"api_token": "sec"}, {"api_token": ""}, {"id": "x"}]
            }
        }
        SA._mask_ticketing(d)
        toks = [s.get("api_token") for s in d["ticketing"]["sources"]]
        assert toks == [SA._MASK, "", ""]


class TestMaskedView:
    def test_secrets_and_ticketing_tokens_masked(self):
        S.save_settings(
            S.Settings.from_dict(
                {
                    "github": {"token": "ghp_secret"},
                    "ticketing": {
                        "sources": [
                            {"id": "s", "provider": "shortcut", "api_token": "sc_sec"}
                        ]
                    },
                }
            )
        )
        v = SA._masked_view()
        assert v["github"]["token"] == SA._MASK
        assert v["ticketing"]["sources"][0]["api_token"] == SA._MASK
        assert "ghp_secret" not in str(v) and "sc_sec" not in str(v)

    def test_unset_secrets_are_empty_strings(self):
        v = SA._masked_view()
        assert v["github"]["token"] == ""
        assert v["general"]["auth_token"] == ""


class TestSourceCfgFromBody:
    def test_defaults_to_shortcut_when_nothing_configured(self):
        cfg = SA._source_cfg_from_body({})
        assert cfg.provider == "shortcut"
        assert cfg.api_token == ""

    def test_fills_missing_fields_from_stored_source_by_id(self):
        S.set_ticketing_sources(
            [
                {
                    "id": "j",
                    "provider": "jira",
                    "api_token": "tok",
                    "base_url": "https://j",
                    "email": "e@x",
                    "member_id": "m",
                }
            ]
        )
        cfg = SA._source_cfg_from_body({"id": "j"})
        assert cfg.provider == "jira"
        assert cfg.api_token == "tok"
        assert cfg.base_url == "https://j"
        assert cfg.email == "e@x"
        assert cfg.member_id == "m"

    def test_masked_token_falls_back_to_stored(self):
        S.set_ticketing_sources(
            [{"id": "j", "provider": "jira", "api_token": "stored"}]
        )
        cfg = SA._source_cfg_from_body({"id": "j", "api_token": SA._MASK})
        assert cfg.api_token == "stored"

    def test_body_fields_override_stored(self):
        S.set_ticketing_sources([{"id": "j", "provider": "jira", "api_token": "old"}])
        cfg = SA._source_cfg_from_body(
            {"id": "j", "provider": "linear", "api_token": "new"}
        )
        assert cfg.provider == "linear"
        assert cfg.api_token == "new"


class TestGhCliStatus:
    def test_ok_means_installed_and_authenticated(self, monkeypatch):
        monkeypatch.setattr(
            SA.doctor, "check_gh", lambda: Check("gh", "gh", "ok", "authed")
        )
        assert SA._gh_cli_status() == (True, True, "authed")

    def test_warn_means_installed_but_not_authenticated(self, monkeypatch):
        monkeypatch.setattr(
            SA.doctor, "check_gh", lambda: Check("gh", "gh", "warn", "no login")
        )
        assert SA._gh_cli_status() == (True, False, "no login")

    def test_fail_means_not_installed(self, monkeypatch):
        monkeypatch.setattr(
            SA.doctor, "check_gh", lambda: Check("gh", "gh", "fail", "missing")
        )
        assert SA._gh_cli_status() == (False, False, "missing")

    def test_info_means_not_installed(self, monkeypatch):
        # gh is optional: absent gh is `info`, which must still read as
        # not-installed (not "installed" just because it isn't a hard fail).
        monkeypatch.setattr(
            SA.doctor,
            "check_gh",
            lambda: Check("gh", "gh", "info", "not found (optional)"),
        )
        assert SA._gh_cli_status() == (False, False, "not found (optional)")


class TestApplyPost:
    def test_ticketing_group_is_skipped(self):
        # ticketing is a list managed via the CRUD endpoints; _apply_post must
        # never fold it into a field-merge update.
        SA._apply_post(
            {
                "ticketing": {"sources": [{"provider": "jira"}]},
                "github": {"base_branch": "main"},
            }
        )
        s = S.load_settings()
        assert s.ticketing.sources == []  # untouched
        assert s.github.base_branch == "main"

    def test_non_dict_group_is_ignored(self):
        SA._apply_post({"github": "not-a-dict", "repository": {"url": "u"}})
        s = S.load_settings()
        assert s.repository.url == "u"
        assert s.github.base_branch == ""
