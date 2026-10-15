"""One GitHub credential path.

Pins the contract that there is ONE resolver (backend.web.core.github_auth
.resolve_token) with one precedence everywhere — Settings → $GH_TOKEN →
$GITHUB_TOKEN → gh CLI → config.toml [github].token (deprecated, last, warned
about once) — that the pipeline delegates to it with a short memo instead of a
process-lifetime cache, that the Connect GitHub dialog's Test really asks
GitHub, that every remedy string names the one place the credential lives, and
that the raw token fields are gone from the Intake tabs and the GitHub Issues
card. No real GitHub or gh is ever reached: gh is stubbed, HTTP goes through
the module's _http hook.
"""

from __future__ import annotations

import logging
import pathlib

import pytest
from fastapi.testclient import TestClient

from backend.config import settings as S
from backend.ticket_ingestion import github_auth as pipe_auth
from backend.ticket_ingestion.config import GithubConfig
from backend.web.core import github_auth as web_auth

ROOT = pathlib.Path(__file__).resolve().parents[2]
ONE_PLACE = "Settings → Connections → GitHub → Connect"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Scratch settings, no env tokens, no gh, no memo, warning flags reset."""
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    for var in ("GH_TOKEN", "GITHUB_TOKEN", web_auth.CLIENT_ID_ENV):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(web_auth, "_gh_token", lambda: "")
    monkeypatch.setattr(web_auth, "_TOML_WARNED", False)
    S.invalidate()
    pipe_auth.invalidate()
    yield
    pipe_auth.invalidate()
    S.invalidate()


def _cfg(**kw) -> GithubConfig:
    base = dict(
        base_branch="main",
        min_age_minutes=15,
        poll_interval_seconds=60,
        enabled=True,
        skip_authors=[],
    )
    base.update(kw)
    return GithubConfig(**base)


# --------------------------------------------------------------------------- #
# 1. One resolver, one precedence
# --------------------------------------------------------------------------- #
class TestPrecedence:
    def test_settings_beats_everything(self, monkeypatch):
        S.update_settings(github={"token": "from-settings"})
        monkeypatch.setenv("GH_TOKEN", "from-env")
        monkeypatch.setattr(web_auth, "_gh_token", lambda: "from-gh")
        assert web_auth.resolve_token("from-toml") == ("settings", "from-settings")

    def test_gh_token_env_beats_github_token_env(self, monkeypatch):
        monkeypatch.setenv("GH_TOKEN", "primary")
        monkeypatch.setenv("GITHUB_TOKEN", "secondary")
        monkeypatch.setattr(web_auth, "_gh_token", lambda: "from-gh")
        assert web_auth.resolve_token("from-toml") == ("env:GH_TOKEN", "primary")

    def test_github_token_env_beats_gh_cli(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "secondary")
        monkeypatch.setattr(web_auth, "_gh_token", lambda: "from-gh")
        assert web_auth.resolve_token("from-toml") == ("env:GITHUB_TOKEN", "secondary")

    def test_gh_cli_beats_config_toml(self, monkeypatch):
        monkeypatch.setattr(web_auth, "_gh_token", lambda: "from-gh")
        assert web_auth.resolve_token("from-toml") == ("gh-cli", "from-gh")

    def test_config_toml_is_last_and_still_works(self):
        assert web_auth.resolve_token("from-toml") == (
            web_auth.SOURCE_CONFIG_TOML,
            "from-toml",
        )

    def test_nothing_is_none(self):
        assert web_auth.resolve_token() == ("none", "")
        assert web_auth.resolve_token("   ") == ("none", "")

    def test_a_blank_settings_token_is_skipped(self, monkeypatch):
        S.update_settings(github={"token": "   "})
        monkeypatch.setenv("GITHUB_TOKEN", "secondary")
        assert web_auth.resolve_token()[0] == "env:GITHUB_TOKEN"


class TestConfigTomlDeprecation:
    def test_warns_once_per_process_whenever_a_toml_token_is_present(self, caplog):
        caplog.set_level(logging.WARNING, logger=web_auth.__name__)
        S.update_settings(github={"token": "from-settings"})
        # Present but outranked: still worth the one warning (it is dead weight).
        web_auth.resolve_token("from-toml")
        web_auth.resolve_token("from-toml")
        web_auth.resolve_token("from-toml")
        msgs = [
            r.getMessage() for r in caplog.records if "config.toml" in r.getMessage()
        ]
        assert len(msgs) == 1
        assert "deprecated" in msgs[0]
        assert ONE_PLACE in msgs[0]

    def test_no_toml_token_no_warning(self, caplog):
        caplog.set_level(logging.WARNING, logger=web_auth.__name__)
        web_auth.resolve_token("")
        assert not [r for r in caplog.records if "config.toml" in r.getMessage()]


# --------------------------------------------------------------------------- #
# 2. The pipeline delegates, with a 60 s memo instead of a lifetime cache
# --------------------------------------------------------------------------- #
class TestPipelineDelegation:
    async def test_pipeline_uses_the_web_chain_not_its_own(self, monkeypatch):
        S.update_settings(github={"token": "from-settings"})
        # The merged config.token is what the OLD resolver put first.
        tok = await pipe_auth.resolve_token(
            _cfg(token="stale-merged", toml_token="toml")
        )
        assert tok == "from-settings"
        assert pipe_auth.cached_source() == "settings"

    async def test_pipeline_falls_through_to_the_toml_token(self):
        tok = await pipe_auth.resolve_token(_cfg(token="x", toml_token="from-toml"))
        assert tok == "from-toml"
        assert pipe_auth.cached_source() == web_auth.SOURCE_CONFIG_TOML

    async def test_a_caller_without_a_config_still_resolves(self, monkeypatch):
        monkeypatch.setenv("GH_TOKEN", "from-env")
        assert await pipe_auth.resolve_token(None) == "from-env"

    async def test_nothing_raises_with_the_one_place_named(self):
        with pytest.raises(pipe_auth.GithubAuthError) as err:
            await pipe_auth.resolve_token(_cfg())
        assert ONE_PLACE in str(err.value)

    async def test_memo_expires_after_the_ttl(self, monkeypatch):
        clock = {"t": 1000.0}
        monkeypatch.setattr(pipe_auth.time, "monotonic", lambda: clock["t"])
        calls = []

        def fake_resolve(toml_token=""):
            calls.append(toml_token)
            return "settings", "tok-%d" % len(calls)

        monkeypatch.setattr(web_auth, "resolve_token", fake_resolve)
        assert await pipe_auth.resolve_token(_cfg()) == "tok-1"
        clock["t"] += 30
        assert await pipe_auth.resolve_token(_cfg()) == "tok-1"  # memoized
        clock["t"] += 31  # past MEMO_TTL
        assert await pipe_auth.resolve_token(_cfg()) == "tok-2"
        assert len(calls) == 2

    async def test_invalidate_forces_a_fresh_walk(self, monkeypatch):
        calls = []

        def fake_resolve(toml_token=""):
            calls.append(1)
            return "settings", "tok"

        monkeypatch.setattr(web_auth, "resolve_token", fake_resolve)
        await pipe_auth.resolve_token(_cfg())
        await pipe_auth.resolve_token(_cfg())
        pipe_auth.invalidate()
        await pipe_auth.resolve_token(_cfg())
        assert len(calls) == 2
        assert pipe_auth.MEMO_TTL == 60.0

    def test_the_layered_config_keeps_the_raw_toml_token_apart(self, monkeypatch):
        """The settings merge used to fold the toml token into ``token``,
        which is how the pipeline put config.toml first: the raw value now
        rides along separately as the deprecated last rung."""
        from backend.ticket_ingestion import config as cfgmod

        S.update_settings(github={"token": "from-settings"})
        merged = cfgmod._merge_layers(
            {"github": {"token": "from-toml", "repos": ["o/r"]}}
        )
        assert merged["github"]["toml_token"] == "from-toml"
        assert merged["github"]["token"] == "from-settings"
        merged = cfgmod._merge_layers({"github": {"repos": ["o/r"]}})
        assert merged["github"]["toml_token"] == ""
        assert merged["github"]["token"] == "from-settings"

    def test_an_explicit_config_file_token_is_the_toml_token(self, tmp_path):
        from backend.ticket_ingestion import config as cfgmod

        toml = tmp_path / "config.toml"
        toml.write_text(
            "[ticketing]\nprovider = 'linear'\napi_token = 'x'\n"
            "[repository]\nurl = 'git@github.com:o/r.git'\nworkspace_dir = './w'\n"
            "[github]\ntoken = 'from-toml'\nrepos = ['o/r']\n"
        )
        cfg = cfgmod.load_config(toml)
        assert cfg.github is not None
        assert cfg.github.toml_token == "from-toml"
        assert cfg.github.token == "from-toml"


class TestProbesAgreeWithTheTokenUsed:
    """The cheap "is there a token / which one" probes (``has_token_sync`` for
    the capability flags, ``_github_token_source`` for the Connections row)
    walk the same chain as the token the PR buttons call GitHub with. The old
    ``has_token_sync`` went through ``resolve_secret_sync`` with config.toml
    FIRST, so it could vouch for a token the real chain never uses."""

    @pytest.fixture
    def toml_cfg(self, monkeypatch):
        from backend.web.core import github_pr

        holder = {"cfg": _cfg()}
        monkeypatch.setattr(github_pr, "_github_config", lambda: holder["cfg"])
        return holder

    async def test_config_toml_and_gh_token_agree_on_gh_token(
        self, monkeypatch, toml_cfg
    ):
        from backend.web.addons import settings as settings_addon
        from backend.web.core import github_pr

        toml_cfg["cfg"] = _cfg(token="from-toml", toml_token="from-toml")
        monkeypatch.setenv("GH_TOKEN", "from-env")
        assert github_pr.has_token_sync() is True
        assert settings_addon._github_token_source() == "env:GH_TOKEN"
        assert await github_pr.api_token() == "from-env"
        assert pipe_auth.cached_source() == "env:GH_TOKEN"

    async def test_config_toml_alone_is_found_by_both(self, toml_cfg):
        from backend.web.core import github_pr

        toml_cfg["cfg"] = _cfg(token="from-toml", toml_token="from-toml")
        assert github_pr.has_token_sync() is True
        assert await github_pr.api_token() == "from-toml"

    async def test_a_merged_token_without_its_toml_rung_counts_for_neither(
        self, toml_cfg
    ):
        from backend.web.core import github_pr

        toml_cfg["cfg"] = _cfg(token="stale-merged")
        assert github_pr.has_token_sync() is False
        assert await github_pr.api_token() == ""

    def test_the_probes_never_run_gh(self, monkeypatch, toml_cfg):
        from backend.web.addons import settings as settings_addon
        from backend.web.core import github_pr

        def boom():
            raise AssertionError("a capability probe ran gh auth token")

        monkeypatch.setattr(web_auth, "_gh_token", boom)
        assert github_pr.has_token_sync() is False
        assert settings_addon._github_token_source() == ""
        assert web_auth.resolve_token("from-toml", gh=False) == (
            web_auth.SOURCE_CONFIG_TOML,
            "from-toml",
        )


class TestConfigTomlOnlyIsSeenEverywhere:
    """A user whose ONLY credential is ``[github].token`` in config.toml: the
    PR buttons (``api_token``) use it, so Connect GitHub's status, its Test,
    git's credential helper and the per-repo Test must find it too instead of
    reporting "none"."""

    @pytest.fixture(autouse=True)
    def toml_only(self, monkeypatch):
        from backend.web.core import github_pr

        cfg = _cfg(token="from-toml", toml_token="from-toml")
        monkeypatch.setattr(github_pr, "_github_config", lambda: cfg)
        monkeypatch.setattr(web_auth, "gh_path", lambda: "")
        monkeypatch.setattr(web_auth, "credential_helper", lambda: "")
        web_auth._USERS.clear()

    async def test_pr_buttons_use_it(self):
        from backend.web.core import github_pr

        assert await github_pr.api_token() == "from-toml"

    def test_status_reports_it(self):
        st = web_auth.status(check_user=False)
        assert st["connected"] is True
        assert st["source"] == web_auth.SOURCE_CONFIG_TOML
        assert "from-toml" not in str(st)

    def test_connection_test_uses_it(self, monkeypatch):
        calls = []
        monkeypatch.setattr(web_auth, "_http", _fake_http(calls))
        out = web_auth.test_connection()
        assert out["token_source"] == web_auth.SOURCE_CONFIG_TOML
        assert out["login"] == "octo"

    def test_git_credential_helper_answers_with_it(self):
        out = web_auth.credential_answer("protocol=https\nhost=github.com\n")
        assert out == "username=x-access-token\npassword=from-toml\n"

    def test_per_repo_test_gets_past_the_token_check(self, monkeypatch):
        import aiohttp

        from backend.web import server

        def offline(*_a, **_k):
            raise OSError("offline")

        monkeypatch.setattr(aiohttp, "ClientSession", offline)
        with TestClient(server.app) as client:
            body = client.post(
                "/api/settings/test/github-repo", json={"repo": "o/r"}
            ).json()
        assert body["ok"] is False
        assert "could not reach api.github.com" in body["error"]


# --------------------------------------------------------------------------- #
# 3. The device-flow client id ships as a constant (the MindFlock OAuth App)
# --------------------------------------------------------------------------- #
class TestClientId:
    def test_env_then_settings_then_the_build_constant(self, monkeypatch):
        # The shipped id is the registered "MindFlock" OAuth App: every install
        # gets "Sign in with GitHub" without configuring anything. It is a
        # public identifier (no secret ships), never a token.
        assert web_auth.DEFAULT_GITHUB_OAUTH_CLIENT_ID == "Ov23liG6WFfcz4gXLwks"
        assert web_auth.client_id() == "Ov23liG6WFfcz4gXLwks"
        monkeypatch.setattr(web_auth, "DEFAULT_GITHUB_OAUTH_CLIENT_ID", "")
        assert web_auth.client_id() == ""
        monkeypatch.setattr(web_auth, "DEFAULT_GITHUB_OAUTH_CLIENT_ID", "Iv1.built")
        assert web_auth.client_id() == "Iv1.built"
        S.update_settings(github={"oauth_client_id": "Iv1.settings"})
        assert web_auth.client_id() == "Iv1.settings"
        monkeypatch.setenv(web_auth.CLIENT_ID_ENV, "Iv1.env")
        assert web_auth.client_id() == "Iv1.env"

    def test_status_says_whether_the_device_flow_is_configured(self, monkeypatch):
        monkeypatch.setattr(web_auth, "gh_path", lambda: "")
        monkeypatch.setattr(web_auth, "credential_helper", lambda: "")
        monkeypatch.setattr(web_auth, "DEFAULT_GITHUB_OAUTH_CLIENT_ID", "")
        st = web_auth.status(check_user=False)
        assert st["device_flow_configured"] is False
        assert "device" not in st["methods"]
        monkeypatch.setattr(web_auth, "DEFAULT_GITHUB_OAUTH_CLIENT_ID", "Iv1.built")
        st = web_auth.status(check_user=False)
        assert st["device_flow_configured"] is True
        assert st["methods"][0] == "device"

    def test_the_constant_comment_names_the_github_settings_page(self):
        src = (ROOT / "backend/web/core/github_auth.py").read_text()
        assert "Developer settings → OAuth Apps" in src
        assert "Enable Device" in src

    def test_serve_takes_a_github_client_id_flag(self):
        src = (ROOT / "backend/cli.py").read_text()
        assert '"--github-client-id"' in src
        assert 'os.environ["MINDFLOCK_GITHUB_CLIENT_ID"]' in src


# --------------------------------------------------------------------------- #
# 4. Connect GitHub's Test: a real /user call plus one repo-access check
# --------------------------------------------------------------------------- #
def _fake_http(calls, *, user=(200, {"login": "octo", "id": 7}), repo=None):
    repo = repo or (
        200,
        {
            "full_name": "org/one",
            "private": True,
            "default_branch": "dev",
            "permissions": {"push": True},
        },
    )

    def _http(method, url, *, headers=None, data=None, timeout=10.0):
        calls.append((method, url))
        if url.endswith("/user"):
            status, body = user
            return status, {"X-OAuth-Scopes": "repo, read:org"}, body
        if "/repos/" in url:
            status, body = repo
            return status, {}, body
        raise AssertionError("unexpected call " + url)

    return _http


class TestConnectionTest:
    def test_user_then_first_configured_repo(self, monkeypatch):
        S.update_settings(github={"token": "ghp_x", "repos": ["org/one", "org/two"]})
        calls = []
        monkeypatch.setattr(web_auth, "_http", _fake_http(calls))
        web_auth._USERS.clear()
        out = web_auth.test_connection()
        assert out["ok"] is True
        assert out["token_source"] == "settings"
        assert out["login"] == "octo"
        assert out["scopes"] == ["repo", "read:org"]
        assert out["repo"] == "org/one" and out["repo_ok"] is True
        assert out["can_push"] is True and out["private"] is True
        assert "org/one" in out["detail"] and "@octo" in out["detail"]
        assert [u.rsplit("/", 2)[-2:] for _m, u in calls] == [
            ["api.github.com", "user"],
            ["org", "one"],
        ]
        assert "ghp_x" not in str(out)

    def test_issue_repos_count_when_no_review_repo(self, monkeypatch):
        S.update_settings(github={"token": "ghp_x", "issue_repos": ["org/issues"]})
        calls = []
        monkeypatch.setattr(web_auth, "_http", _fake_http(calls))
        web_auth._USERS.clear()
        out = web_auth.test_connection()
        assert out["repo"] == "org/issues"

    def test_no_repo_configured_stops_at_user(self, monkeypatch):
        S.update_settings(github={"token": "ghp_x"})
        calls = []
        monkeypatch.setattr(web_auth, "_http", _fake_http(calls))
        web_auth._USERS.clear()
        out = web_auth.test_connection()
        assert out["ok"] is True and out["repo"] == "" and out["repo_ok"] is None
        assert len(calls) == 1
        assert "no repository configured" in out["detail"]

    def test_an_unreachable_repo_is_a_failed_test(self, monkeypatch):
        S.update_settings(github={"token": "ghp_x", "repos": ["org/private"]})
        calls = []
        monkeypatch.setattr(web_auth, "_http", _fake_http(calls, repo=(404, {})))
        web_auth._USERS.clear()
        out = web_auth.test_connection()
        assert out["ok"] is False and out["repo_ok"] is False
        assert "cannot see it" in out["repo_error"]

    def test_a_rejected_token_is_a_failed_test(self, monkeypatch):
        S.update_settings(github={"token": "ghp_x", "repos": ["org/one"]})
        calls = []
        monkeypatch.setattr(web_auth, "_http", _fake_http(calls, user=(401, {})))
        web_auth._USERS.clear()
        out = web_auth.test_connection()
        assert out["ok"] is False and out["login"] == ""
        assert "expired or revoked" in out["user_error"]
        assert len(calls) == 1  # no repo check without a working token

    def test_no_token_makes_no_call(self, monkeypatch):
        calls = []
        monkeypatch.setattr(web_auth, "_http", _fake_http(calls))
        out = web_auth.test_connection()
        assert out["ok"] is False and out["token_source"] == "none"
        assert calls == []

    def test_the_route_adds_gh_and_keeps_the_old_keys(self, monkeypatch):
        from backend.web import server
        from backend.web.addons import settings as settings_addon

        S.update_settings(github={"token": "ghp_x", "repos": ["org/one"]})
        calls = []
        monkeypatch.setattr(web_auth, "_http", _fake_http(calls))
        monkeypatch.setattr(
            settings_addon, "_gh_cli_status", lambda: (True, False, "d")
        )
        web_auth._USERS.clear()
        with TestClient(server.app) as client:
            body = client.post("/api/settings/test/github").json()
        assert body["ok"] is True
        for key in ("token_source", "gh_installed", "gh_authenticated", "detail"):
            assert key in body
        assert body["login"] == "octo" and body["repo_ok"] is True
        assert body["gh_installed"] is True and body["gh_authenticated"] is False
        assert "ghp_x" not in str(body)


# --------------------------------------------------------------------------- #
# 5. Every remedy names the one place
# --------------------------------------------------------------------------- #
class TestRemedies:
    def test_backend_remedies_name_the_one_place(self):
        from backend.web import server
        from backend.web.addons import connections

        assert ONE_PLACE in server._PR_REMEDY
        assert ONE_PLACE in connections._NO_CREDENTIAL_FIX
        assert server._PR_REMEDY == connections._NO_CREDENTIAL_FIX

    @pytest.mark.parametrize(
        "rel",
        [
            "backend/web/server.py",
            "backend/web/core/autopilot_driver.py",
            "backend/web/core/test_plans_driver.py",
            "backend/web/addons/connections.py",
            "backend/web/addons/settings.py",
            "backend/ticket_ingestion/providers/github_issues.py",
            "backend/ticket_ingestion/github_auth.py",
            "backend/web/static/mobile.js",
        ],
    )
    def test_no_backend_file_sends_people_to_the_old_doors(self, rel):
        src = (ROOT / rel).read_text()
        assert "Intake → Pull requests, or install" not in src, rel
        assert "set one under Advanced options" not in src, rel
        assert "Advanced options, export" not in src, rel

    def test_the_repo_test_names_the_one_place(self, monkeypatch):
        from backend.web import server

        async def _no_token(_cfg=None):
            raise pipe_auth.GithubAuthError("none")

        monkeypatch.setattr(pipe_auth, "resolve_token", _no_token)
        with TestClient(server.app) as client:
            body = client.post(
                "/api/settings/test/github-repo", json={"repo": "o/r"}
            ).json()
        assert body["ok"] is False and ONE_PLACE in body["error"]

    def test_connections_github_configure_opens_the_dialog(self):
        from backend.web.addons import connections

        row = connections._github_connection()
        assert row["configure_dialog"] == "connect-github"
        assert row["action"]["kind"] == "dialog"
        assert row["action"]["target"] == "connect-github"
        assert ONE_PLACE in connections._NO_CREDENTIAL_FIX


# --------------------------------------------------------------------------- #
# 6. The per-source GitHub Issues token: no field, read-only for one release
# --------------------------------------------------------------------------- #
class TestPerSourceToken:
    def test_the_card_offers_no_token_field(self):
        from backend.ticket_ingestion.providers import PROVIDER_META

        meta = next(m for m in PROVIDER_META if m["id"] == "github_issues")
        assert "api_token" not in [f["key"] for f in meta["fields"]]
        assert ONE_PLACE in meta["blurb"]

    async def test_a_stored_token_is_still_read_last_with_one_notice(
        self, monkeypatch, caplog
    ):
        from backend.ticket_ingestion.config import TicketProviderConfig
        from backend.ticket_ingestion.providers import github_issues as ghm

        caplog.set_level(logging.WARNING, logger=ghm.__name__)
        monkeypatch.setattr(ghm, "_PER_SOURCE_TOKEN_NOTICED", False)
        prov = ghm.GithubIssuesProvider(
            TicketProviderConfig(
                provider="github_issues", project="o/r", api_token="per-source"
            )
        )
        assert await prov._resolve_token() == "per-source"
        assert await prov._resolve_token() == "per-source"
        notices = [r for r in caplog.records if "deprecated" in r.getMessage()]
        assert len(notices) == 1
        assert ONE_PLACE in notices[0].getMessage()

    async def test_the_one_chain_beats_a_stored_per_source_token(self, monkeypatch):
        from backend.ticket_ingestion.config import TicketProviderConfig
        from backend.ticket_ingestion.providers import github_issues as ghm

        monkeypatch.setattr(ghm, "_PER_SOURCE_TOKEN_NOTICED", True)
        S.update_settings(github={"token": "from-settings"})
        prov = ghm.GithubIssuesProvider(
            TicketProviderConfig(
                provider="github_issues", project="o/r", api_token="per-source"
            )
        )
        assert await prov._resolve_token() == "from-settings"

    async def test_no_token_anywhere_names_the_one_place(self):
        from backend.ticket_ingestion.config import TicketProviderConfig
        from backend.ticket_ingestion.providers import github_issues as ghm

        prov = ghm.GithubIssuesProvider(
            TicketProviderConfig(provider="github_issues", project="o/r")
        )
        with pytest.raises(ghm.ProviderError, match="No GitHub token") as err:
            await prov._resolve_token()
        assert ONE_PLACE in str(err.value)

    def test_connections_flags_a_source_that_still_carries_its_own_token(self):
        from backend.web.addons import connections
        from backend.ticket_ingestion.config import TicketProviderConfig

        src = TicketProviderConfig(
            provider="github_issues", project="o/r", api_token="old"
        )
        row = connections._source_connection(src, only=True)
        assert row["status"] == connections.STATE_NEEDS_ATTENTION
        assert row["legacy_status"] == connections.ATTENTION
        assert ONE_PLACE in row["detail"]
        # Without its own token the row follows the tested-state rule: never
        # tested yet reads as attention ("run Test"), not as connected.
        src = TicketProviderConfig(provider="github_issues", project="o/r")
        row = connections._source_connection(src, only=True)
        assert row["status"] == connections.STATE_NEEDS_ATTENTION
        assert row["legacy_status"] == connections.ATTENTION
        assert "not tested" in row["detail"]


# --------------------------------------------------------------------------- #
# 7. The Intake tabs no longer render the raw field (source-level pin; the
#    vitest twin reads the same files)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "rel",
    [
        "frontend/src/components/intake/PullRequestsTab.tsx",
        "frontend/src/components/intake/IssuesTab.tsx",
    ],
)
def test_intake_tabs_offer_connect_github_not_a_raw_token(rel):
    src = (ROOT / rel).read_text()
    assert 'field="token"' not in src
    assert "Test GitHub" not in src
    assert "runGithubTest" not in src
    # The credential's one line, with its real state, opens the one dialog.
    assert "<GithubStatusLine" in src
    kit = (ROOT / "frontend/src/components/intake/kit.tsx").read_text()
    assert "openConnectGitHub()" in kit
    assert '"Connect GitHub"' in kit


def test_connect_github_dialog_leads_with_the_token_without_a_device_flow():
    src = (ROOT / "frontend/src/components/dialogs/ConnectGitHub.tsx").read_text()
    lib = (ROOT / "frontend/src/lib/githubConnect.ts").read_text()
    # Round 2: the build-internals sentence no longer shows in the dialog; with
    # no device flow the token path leads and names its scopes.
    assert "DEVICE_FLOW_UNCONFIGURED" not in src
    assert 'id="gh-token-lead"' in src
    assert "<code>repo</code> and <code>read:org</code>" in src
    assert "mf:open-connect-github" in lib
    assert "export function ConnectGitHubDialog" in src
    assert '"/api/settings/test/github"' in src
