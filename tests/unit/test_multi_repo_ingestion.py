"""Per-source repo for ticket ingestion: each ticketing source can target its
own repo, so ingestion spans many repos instead of one global one. Covers the
settings round-trip, config layering, the engine repo-URL override, and the
source-card UI wiring.
"""

import pytest
from starlette.testclient import TestClient

from backend.config import settings as S
from backend.ticket_ingestion.config import load_config
from backend.session import provisioned
from backend.web import server

client = TestClient(server.app)


@pytest.fixture(autouse=True)
def _isolate_store(isolate_settings_store):
    """Delegate to the shared settings-store isolation (tests/conftest.py)."""


# --------------------------------------------------------------------------- #
# Settings model
# --------------------------------------------------------------------------- #
def test_source_repo_url_roundtrips():
    src = S.TicketingSource(provider="shortcut", repo_url="git@github.com:org/api.git")
    back = S.TicketingSource.from_dict(src.to_dict())
    assert back.repo_url == "git@github.com:org/api.git"


def test_source_repo_url_omitted_when_empty():
    assert "repo_url" not in S.TicketingSource(provider="shortcut").to_dict()


# --------------------------------------------------------------------------- #
# Config layering: a source's repo_url must reach the pipeline config
# --------------------------------------------------------------------------- #
def test_source_repo_url_flows_into_config():
    S.set_ticketing_sources(
        [
            {
                "provider": "shortcut",
                "api_token": "t1",
                "member_id": "m1",
                "repo_url": "git@github.com:org/api.git",
            },
            {
                "provider": "shortcut",
                "api_token": "t2",
                "member_id": "m2",
                "repo_url": "git@github.com:org/web.git",
            },
        ]
    )
    S.update_settings(
        repository={"url": "git@github.com:org/default.git", "workspace_dir": "./ws"},
    )
    cfg = load_config()
    repos = {s.repo_url for s in cfg.ticketing_sources}
    assert "git@github.com:org/api.git" in repos
    assert "git@github.com:org/web.git" in repos


def test_source_without_repo_url_falls_back_to_default_at_provision():
    # A source with no repo_url leaves it "" — downstream (provisioner / engine)
    # substitutes the global repository.url.
    S.update_settings(
        repository={"url": "git@github.com:org/default.git", "workspace_dir": "./ws"},
    )
    S.set_ticketing_sources(
        [
            {"provider": "shortcut", "api_token": "t", "member_id": "m"},
        ]
    )
    cfg = load_config()
    assert cfg.ticketing_sources[0].repo_url == ""


# --------------------------------------------------------------------------- #
# Engine path: an explicit repo-URL override wins over the configured repo
# --------------------------------------------------------------------------- #
def test_provision_settings_repo_url_override_wins():
    S.update_settings(
        repository={"url": "git@github.com:org/default.git", "workspace_dir": "./ws"},
    )
    s = provisioned.load_provision_settings(
        repo_url_override="git@github.com:org/api.git"
    )
    assert s is not None
    assert s.repo_url == "git@github.com:org/api.git"


def test_provision_settings_no_override_uses_configured_repo():
    S.update_settings(
        repository={"url": "git@github.com:org/default.git", "workspace_dir": "./ws"},
    )
    s = provisioned.load_provision_settings()
    assert s is not None
    assert s.repo_url == "git@github.com:org/default.git"


# --------------------------------------------------------------------------- #
# Frontend: each ticketing source card exposes a Repo URL field
# --------------------------------------------------------------------------- #
def test_source_card_js_has_repo_field():
    js = client.get("/app.js").text
    assert '"repo_url"' in js
    # saveAll() persists any [data-tk-field], and cards render for the sources.
    # Repo URL is now required per source (there is no global default repo).
    assert "Repo URL" in js


def test_source_card_is_collapsible():
    js = client.get("/app.js").text
    # Collapsible header + body wired, and saved sources fold by default.
    assert "setCollapsed" in js
    assert "tk-head" in js and "tk-body" in js
    css = client.get("/style.css").text
    body = css[css.index(".tk-source.tk-collapsed .tk-body") :]
    assert "display: none" in body[: body.index("}")]
