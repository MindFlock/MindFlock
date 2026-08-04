"""Automated PR review: the opt-in feature surfaced in Settings → Repo & PR
review. Covers the extended GithubSettings model, the config-layering path that
feeds the PR monitor, the Connections card, and the frontend wiring (markup,
JS handlers, CSS).
"""

import json
import types

import pytest
from starlette.testclient import TestClient

from backend.config import settings as S
from backend.ticket_ingestion.config import load_config
from backend.web import server
from backend.web.addons import connections

client = TestClient(server.app)


@pytest.fixture(autouse=True)
def _isolate_store(isolate_settings_store):
    """Delegate to the shared settings-store isolation (tests/conftest.py) so
    tests never touch the real ~/.mindflock/settings.json."""


# --------------------------------------------------------------------------- #
# GithubSettings model
# --------------------------------------------------------------------------- #
def test_github_settings_roundtrips_new_fields():
    gs = S.GithubSettings(
        repos=["org/repo"],
        enabled=True,
        min_age_minutes=30,
        poll_interval_seconds=120,
        skip_authors=["dependabot", "renovate"],
    )
    back = S.GithubSettings.from_dict(gs.to_dict())
    assert back.min_age_minutes == 30
    assert back.poll_interval_seconds == 120
    assert back.skip_authors == ["dependabot", "renovate"]
    assert back.enabled is True


def test_github_settings_omits_empty_advanced_fields():
    d = S.GithubSettings(repos=["org/repo"]).to_dict()
    assert "min_age_minutes" not in d
    assert "poll_interval_seconds" not in d
    assert "skip_authors" not in d


def test_skip_authors_accepts_csv_and_strips_blanks():
    # The settings form sends a comma-separated string; the API may send a list.
    assert S.GithubSettings.from_dict({"skip_authors": " a , ,b "}).skip_authors == [
        "a",
        "b",
    ]
    assert S.GithubSettings.from_dict(
        {"skip_authors": ["a", "", "b "]}
    ).skip_authors == ["a", "b"]
    assert S.GithubSettings.from_dict({"skip_authors": None}).skip_authors == []


def test_numeric_fields_coerce_from_string():
    # POSTed number inputs arrive as strings; from_dict must coerce them.
    gs = S.GithubSettings.from_dict(
        {"min_age_minutes": "45", "poll_interval_seconds": "90"}
    )
    assert gs.min_age_minutes == 45
    assert gs.poll_interval_seconds == 90


# --------------------------------------------------------------------------- #
# Config layering: settings values must reach the PR monitor's GithubConfig
# --------------------------------------------------------------------------- #
def test_settings_flow_into_github_config():
    S.update_settings(
        repository={"url": "git@github.com:org/repo.git", "workspace_dir": "./ws"},
        github={
            "repos": ["org/repo"],
            "enabled": True,
            "min_age_minutes": 25,
            "poll_interval_seconds": 45,
            "skip_authors": ["bot1", "bot2"],
        },
    )
    cfg = load_config()  # layered: no config.toml, settings only
    assert cfg.github is not None
    assert cfg.github.repo_list() == ["org/repo"]
    assert cfg.github.enabled is True
    assert cfg.github.min_age_minutes == 25
    assert cfg.github.poll_interval_seconds == 45
    assert cfg.github.skip_authors == ["bot1", "bot2"]


def test_disabling_pr_review_persists_false():
    S.update_settings(github={"repos": ["org/repo"], "enabled": False})
    stored = json.loads(S.settings_path().read_text())
    assert stored["github"]["enabled"] is False


# --------------------------------------------------------------------------- #
# Multiple repos
# --------------------------------------------------------------------------- #
def test_github_settings_repos_roundtrip_and_effective_list():
    gs = S.GithubSettings(repos=["org/a", "org/b"])
    back = S.GithubSettings.from_dict(gs.to_dict())
    assert back.repos == ["org/a", "org/b"]
    assert back.repo_list() == ["org/a", "org/b"]


def test_repos_flow_into_github_config():
    S.update_settings(
        repository={"url": "git@github.com:org/repo.git", "workspace_dir": "./ws"},
        github={"repos": ["org/a", "org/b"], "enabled": True},
    )
    cfg = load_config()
    assert cfg.github is not None
    assert cfg.github.repo_list() == ["org/a", "org/b"]


def test_connection_detail_counts_multiple_repos(monkeypatch):
    monkeypatch.setattr(connections, "_github_token_source", lambda: "env:GH_TOKEN")
    monkeypatch.setattr(
        connections.settings_store,
        "load_settings",
        lambda: types.SimpleNamespace(
            github=types.SimpleNamespace(
                repo="", repos=["org/a", "org/b"], enabled=True
            )
        ),
    )
    c = connections._github_connection()
    assert c["pr_review_enabled"] is True
    assert "2 repos" in c["detail"]


# --------------------------------------------------------------------------- #
# Connections card advertises the feature state
# --------------------------------------------------------------------------- #
def test_github_connection_reports_pr_review_off_when_disabled(monkeypatch):
    monkeypatch.setattr(connections, "_github_token_source", lambda: "env:GH_TOKEN")
    monkeypatch.setattr(
        connections.settings_store,
        "load_settings",
        lambda: types.SimpleNamespace(
            github=types.SimpleNamespace(repo="org/repo", enabled=False)
        ),
    )
    c = connections._github_connection()
    assert c["pr_review_enabled"] is False
    assert "PR review off" in c["detail"]
    assert "org/repo" in c["detail"]


def test_github_connection_reports_pr_review_on(monkeypatch):
    monkeypatch.setattr(connections, "_github_token_source", lambda: "env:GH_TOKEN")
    monkeypatch.setattr(
        connections.settings_store,
        "load_settings",
        lambda: types.SimpleNamespace(
            github=types.SimpleNamespace(repo="org/repo", enabled=None)
        ),
    )
    c = connections._github_connection()
    assert c["pr_review_enabled"] is True
    assert "PR review on" in c["detail"]


def test_github_connection_notes_missing_repo(monkeypatch):
    monkeypatch.setattr(connections, "_github_token_source", lambda: "env:GH_TOKEN")
    monkeypatch.setattr(
        connections.settings_store,
        "load_settings",
        lambda: types.SimpleNamespace(
            github=types.SimpleNamespace(repo="", enabled=None)
        ),
    )
    c = connections._github_connection()
    assert c["pr_review_enabled"] is False
    assert "no repos set" in c["detail"]


# --------------------------------------------------------------------------- #
# Frontend: the Intake → Pull requests tab
# --------------------------------------------------------------------------- #
def test_index_has_pr_review_feature_block():
    html = client.get("/").text
    # The tab label in the Intake dialog's strip (it left the Settings nav when
    # the three intake surfaces were unified there). The tab strip IS the
    # heading now, so there is no in-panel "Automated PR review" title — the
    # switch label and its status line are what name the feature.
    js = client.get("/app.js").text
    assert '"Pull requests"' in js
    assert '"Automated review"' in js
    # Legacy id, kept on the work panel for addon CSS that targeted the screen.
    assert '"pr-review-block"' in client.get("/app.js").text
    # Explicit on/off (pause without deleting repos) + a status line reflecting it.
    assert '"gh-pr-enabled"' in client.get("/app.js").text
    assert '"gh-pr-status"' in client.get("/app.js").text
    assert '"gh-skip-authors"' in client.get("/app.js").text
    assert '"min_age_minutes"' in client.get("/app.js").text
    assert '"poll_interval_seconds"' in client.get("/app.js").text
    # The global default repo is gone; workspace settings live on their own
    # screen and each ticketing source names its own repo instead.
    assert '"workspace"' in client.get("/app.js").text
    assert (
        '"workspace_dir"' in client.get("/app.js").text
    )  # workspace settings on their own screen
    assert 'data-field="url"' not in html  # no global repo URL field


def test_index_has_multi_repo_add_remove_controls():
    js = client.get("/app.js").text
    assert '"gh-repos-list"' in js  # where the repo cards render
    assert '"gh-repo-add-btn"' in js
    # A watched repo is a CARD now, not a chip plus one screen-wide set of
    # options: the slug is a field inside the card (alongside that repo's own
    # agent / base branch / grace period), so there is no separate "add a repo"
    # text input any more. `+ Add repository` appends an empty card instead.
    assert '"gh-repo-new"' not in js
    assert '"+ Add repository"' in js
    # JSX props survive the bundler as object keys, hence the `":` spelling.
    for field in ("repo", "agent", "base_branch", "min_age_minutes", "skip_authors"):
        assert 'data-repo-field": "%s"' % field in js, field


def test_app_js_wires_pr_review_repos_and_toggle():
    js = client.get("/app.js").text
    assert "gh-pr-enabled" in js
    assert "gh-skip-authors" in js
    assert "skip_authors" in js
    assert "skip_authors" in js
    # Multi-repo add/remove lives in the shared RepoSourceList (intake/RepoSources
    # .tsx), which PR review and Git issues both drive. Exactly one copy of each
    # guard IS the point: both screens had their own before they were unified.
    assert js.count("Use owner/name") == 1  # owner/name format guard
    assert js.count("is already in the list") == 1  # duplicate guard
    assert "repos:" in js  # POST { github: { repos: [...] } }
    # …and the per-repo override map saved alongside it, which is what makes a
    # card's own agent / base branch / grace period mean anything.
    assert "repo_settings:" in js
    # Explicit pause toggle owns github.enabled, and pausing is what it says in
    # the toast. There is deliberately no "● Active / ‖ Paused" status line under
    # the switch any more: with the switch itself right there and the repos it
    # counts listed below, it only restated what was already on screen (see
    # AutomationSwitch in intake/kit.tsx). The switch's only subtitle is the one
    # state a switch can't show — nothing connected yet.
    assert "Paused —" not in js
    assert "Automated review paused" in js
    assert "Add a repository below and this starts reviewing your PRs on it" in js


def test_style_has_feature_and_repo_rules():
    css = client.get("/style.css").text
    for sel in (
        # Chips still carry the ticketing state picker.
        ".repo-chip",
        ".repo-chip-x",
        # The card the watched repos render as, and the grouped work list under
        # them (one group per repo).
        ".tk-source",
        ".ik-cards",
        ".ik-groups",
    ):
        assert sel in css, sel
