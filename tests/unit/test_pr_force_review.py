"""Force PR review (Settings → PR review → Open pull requests).

Covers the three layers of the feature without touching the network:

* ``pr_review.skip_reasons`` — mirrors the auto monitor's filters exactly, so
  the UI's "why isn't this PR being reviewed?" chips are truthful;
* the ``/api/github/prs`` + ``/api/github/prs/review`` routes are registered
  ahead of the static mount;
* the frontend ships the panel (ids + API paths present in the static files).
"""

from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from backend.ticket_ingestion.config import GithubConfig
from backend.ticket_ingestion.models import PullRequest
from backend.ticket_ingestion.state import load_processed_prs
from backend.web.core import pr_review


def _gh(**over) -> GithubConfig:
    base = dict(
        base_branch="",
        min_age_minutes=15,
        poll_interval_seconds=60,
        enabled=True,
        skip_authors=[],
        repos=["acme/app"],
    )
    base.update(over)
    return GithubConfig(**base)


def _pr(**over) -> PullRequest:
    base = dict(
        number=7,
        head_ref="feature/x",
        head_sha="abc123",
        base_ref="main",
        title="Fix the thing",
        url="https://github.com/acme/app/pull/7",
        author="me",
        created_at=datetime.now(timezone.utc) - timedelta(hours=2),
        repo="acme/app",
        clone_url="https://github.com/acme/app.git",
    )
    base.update(over)
    return PullRequest(**base)


def test_eligible_pr_has_no_skip_reasons():
    assert pr_review.skip_reasons(_pr(), set(), "me", _gh()) == []


def test_processed_pr_is_flagged():
    reasons = pr_review.skip_reasons(_pr(), {("acme/app", 7)}, "me", _gh())
    assert any("already reviewed" in r for r in reasons)


def test_processed_entry_is_repo_scoped():
    # acme/app#7 in the ledger must not flag other/repo#7.
    processed = {("acme/app", 7)}
    other = _pr(repo="other/repo")
    assert not any(
        "already reviewed" in r
        for r in pr_review.skip_reasons(other, processed, "me", _gh())
    )


def test_foreign_author_is_flagged_only_when_login_known():
    pr = _pr(author="teammate")
    assert any("teammate" in r for r in pr_review.skip_reasons(pr, set(), "me", _gh()))
    # Unknown login (no token yet): don't accuse the PR of being someone else's.
    assert pr_review.skip_reasons(pr, set(), "", _gh()) == []


def test_min_age_grace_is_flagged_with_minutes_left():
    pr = _pr(created_at=datetime.now(timezone.utc) - timedelta(minutes=5))
    reasons = pr_review.skip_reasons(pr, set(), "me", _gh(min_age_minutes=15))
    assert any("min-age grace" in r for r in reasons)


# --------------------------------------------------------------------------- #
# session_title / _resolve_repo_root / _load_config
# --------------------------------------------------------------------------- #
def test_session_title_is_pr_prefixed_slug():
    # pr-<repo-name>-<number>, so #7 in two repos never collide.
    assert pr_review.session_title(_pr(repo="acme/app", number=7)) == "pr-app-7"


def test_resolve_repo_root_honors_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MINDFLOCK_REPO_ROOT", str(tmp_path))
    assert pr_review._resolve_repo_root() == tmp_path.resolve()


def test_resolve_repo_root_finds_config_toml_ancestor(monkeypatch, tmp_path):
    # The resolver walks the module file's ancestors for a config.toml. Point the
    # module __file__ at a synthetic tree so the test verifies the walk itself,
    # not whether the checkout happens to carry a (gitignored) config.toml.
    monkeypatch.delenv("MINDFLOCK_REPO_ROOT", raising=False)
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "config.toml").write_text("[ticketing]\n")
    monkeypatch.setattr(pr_review, "__file__", str(root / "pkg" / "pr_review.py"))
    assert pr_review._resolve_repo_root() == root.resolve()


def test_load_config_reanchors_relative_workspace_dir(monkeypatch, tmp_path):
    import backend.ticket_ingestion.config as cfg_mod

    monkeypatch.setattr(pr_review, "_REPO_ROOT", tmp_path)
    fake = types.SimpleNamespace(workspace_dir=Path("workspaces"))
    monkeypatch.setattr(cfg_mod, "load_config", lambda: fake)
    assert pr_review._load_config().workspace_dir == tmp_path / "workspaces"


# --------------------------------------------------------------------------- #
# list_open_prs (async; PRMonitor mocked)
# --------------------------------------------------------------------------- #
class _FakeMonitor:
    """A PRMonitor stand-in with scripted login + PR listing."""

    login = "me"
    login_exc = None
    prs: list = []

    def __init__(self, gh):
        pass

    async def _authenticated_user_login(self):
        if type(self).login_exc:
            raise type(self).login_exc
        return type(self).login

    async def _list_prs(self, repo):
        return list(type(self).prs)


def _patch_monitor(monkeypatch, *, login="me", login_exc=None, prs=None):
    from backend.ticket_ingestion import pr_monitor

    _FakeMonitor.login = login
    _FakeMonitor.login_exc = login_exc
    _FakeMonitor.prs = prs or []
    monkeypatch.setattr(pr_monitor, "PRMonitor", _FakeMonitor)


async def test_list_open_prs_disabled_when_no_repos(monkeypatch):
    monkeypatch.setattr(
        pr_review, "_load_config", lambda: types.SimpleNamespace(github=_gh(repos=[]))
    )
    out = await pr_review.list_open_prs()
    assert out == {"repos": [], "enabled": False, "login": "", "prs": []}


async def test_list_open_prs_annotates_eligibility(monkeypatch, tmp_path):
    monkeypatch.setattr(pr_review, "_REPO_ROOT", tmp_path)  # empty processed ledger
    monkeypatch.setattr(
        pr_review, "_load_config", lambda: types.SimpleNamespace(github=_gh())
    )
    _patch_monitor(monkeypatch, login="me", prs=[_pr(number=1, author="me")])
    out = await pr_review.list_open_prs()
    assert out["login"] == "me"
    assert out["prs"][0]["eligible"] is True
    assert out["prs"][0]["session"] == "pr-app-1"
    assert out.get("login_error", "") == ""


async def test_list_open_prs_reports_login_error_but_still_lists(monkeypatch, tmp_path):
    monkeypatch.setattr(pr_review, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        pr_review, "_load_config", lambda: types.SimpleNamespace(github=_gh())
    )
    _patch_monitor(
        monkeypatch, login_exc=RuntimeError("no token"), prs=[_pr(author="me")]
    )
    out = await pr_review.list_open_prs()
    assert out["login"] == ""
    assert out["login_error"] == "no token"
    # Unknown login: the foreign-author chip is suppressed, so it stays eligible.
    assert out["prs"][0]["eligible"] is True


# --------------------------------------------------------------------------- #
# find_pr (async)
# --------------------------------------------------------------------------- #
async def test_find_pr_returns_matching_number(monkeypatch):
    monkeypatch.setattr(
        pr_review, "_load_config", lambda: types.SimpleNamespace(github=_gh())
    )
    _patch_monitor(monkeypatch, prs=[_pr(number=3), _pr(number=7)])
    pr = await pr_review.find_pr("acme/app", 7)
    assert pr.number == 7


async def test_find_pr_missing_raises(monkeypatch):
    monkeypatch.setattr(
        pr_review, "_load_config", lambda: types.SimpleNamespace(github=_gh())
    )
    _patch_monitor(monkeypatch, prs=[_pr(number=3)])
    with pytest.raises(LookupError, match="No open PR #99"):
        await pr_review.find_pr("acme/app", 99)


async def test_find_pr_unconfigured_raises(monkeypatch):
    monkeypatch.setattr(
        pr_review, "_load_config", lambda: types.SimpleNamespace(github=None)
    )
    with pytest.raises(LookupError, match="not configured"):
        await pr_review.find_pr("acme/app", 1)


# --------------------------------------------------------------------------- #
# prepare_review (async)
# --------------------------------------------------------------------------- #
def _patch_prepare(monkeypatch, comments):
    from backend.ticket_ingestion import pr_comments, pr_provisioner, pr_runner

    monkeypatch.setattr(
        pr_review, "_load_config", lambda: types.SimpleNamespace(github=_gh())
    )
    monkeypatch.setattr(
        pr_comments, "fetch_actionable_comments", AsyncMock(return_value=comments)
    )

    class _FakeProv:
        def __init__(self, cfg):
            pass

        async def provision(self, pr, launch_cursor=False):
            return types.SimpleNamespace(directory=Path("/ws/pr-app-7"))

    monkeypatch.setattr(pr_provisioner, "PRProvisioner", _FakeProv)
    monkeypatch.setattr(
        pr_runner,
        "build_consolidated_pr_prompt",
        lambda pr, cs, d: "CONSOLIDATED",
    )


async def test_prepare_review_with_comments(monkeypatch):
    _patch_prepare(monkeypatch, comments=["c1", "c2"])
    directory, prompt, n = await pr_review.prepare_review(_pr())
    assert directory == Path("/ws/pr-app-7")
    assert prompt == "CONSOLIDATED"
    assert n == 2


async def test_prepare_review_without_comments_uses_self_review(monkeypatch):
    _patch_prepare(monkeypatch, comments=[])
    directory, prompt, n = await pr_review.prepare_review(_pr())
    assert n == 0
    assert "git diff main...HEAD" in prompt  # the self-review prompt, not comments


# --------------------------------------------------------------------------- #
# record_reviewed ledger round-trip
# --------------------------------------------------------------------------- #
def test_record_reviewed_marks_and_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setattr(pr_review, "_REPO_ROOT", tmp_path)
    pr = _pr(number=7, repo="acme/app")
    pr_review.record_reviewed(pr)
    assert ("acme/app", 7) in load_processed_prs(tmp_path)
    before = len(load_processed_prs(tmp_path))
    pr_review.record_reviewed(pr)  # second call short-circuits (already present)
    assert len(load_processed_prs(tmp_path)) == before


def test_self_review_prompt_targets_the_diff_not_comments():
    prompt = pr_review._self_review_prompt(_pr(), "/tmp/ws/pr-app-7")
    assert "git diff main...HEAD" in prompt
    assert "unstaged" in prompt
    # Same no-commit contract as the comment-driven prompt.
    assert "git push" in prompt


def test_routes_registered():
    from backend.web import server

    paths = {getattr(r, "path", "") for r in server.app.routes}
    assert "/api/github/prs" in paths
    assert "/api/github/prs/review" in paths


def test_frontend_ships_the_open_pr_panel():
    from fastapi.testclient import TestClient

    from backend.web import server

    client = TestClient(server.app)

    html = client.get("/").text
    assert '"gh-prs-list"' in client.get("/app.js").text
    assert '"gh-prs-refresh"' in client.get("/app.js").text

    js = client.get("/app.js").text
    assert '"/api/github/prs"' in js
    assert '"/api/github/prs/review"' in js
    assert "Begin review" in js

    css = client.get("/style.css").text
    assert ".pr-open-item" in css
