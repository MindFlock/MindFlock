"""Force-start issues (Settings -> Git issues) + the issue monitor's scan.

Two layers, both network-free:

* ``issue_start.skip_reasons`` — mirrors the auto monitor's filters exactly, so
  the UI's "why isn't this issue being handled?" chips are truthful. Sibling of
  ``test_pr_force_review``.
* ``IssueMonitor.scan`` — the author / min-age / already-processed filtering the
  automated loop applies. Sibling of ``TestPRMonitorScan`` in
  ``test_pr_pipeline``. ``_list_issues`` is mocked and all state writes are
  confined to ``tmp_path``.
"""

from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from backend.ticket_ingestion import issue_monitor as issue_monitor_mod
from backend.ticket_ingestion.config import GithubConfig
from backend.ticket_ingestion.issue_monitor import IssueMonitor
from backend.ticket_ingestion.models import Issue
from backend.ticket_ingestion.state import load_processed_issues
from backend.web.core import issue_start


def _gh(**over) -> GithubConfig:
    base = dict(
        base_branch="",
        min_age_minutes=15,
        poll_interval_seconds=60,
        enabled=True,
        skip_authors=[],
        repos=["acme/app"],
        issues_enabled=True,
        issue_repos=["acme/app"],
        issue_min_age_minutes=15,
        issue_skip_authors=[],
    )
    base.update(over)
    return GithubConfig(**base)


def _issue(**over) -> Issue:
    base = dict(
        number=7,
        title="Bug: the thing is broken",
        body="Steps to reproduce ...",
        url="https://github.com/acme/app/issues/7",
        author="octocat",
        created_at=datetime.now(timezone.utc) - timedelta(hours=2),
        repo="acme/app",
        clone_url="https://github.com/acme/app.git",
    )
    base.update(over)
    return Issue(**base)


# --------------------------------------------------------------------------- #
# issue_start.skip_reasons
# --------------------------------------------------------------------------- #
def test_eligible_issue_has_no_skip_reasons():
    assert issue_start.skip_reasons(_issue(), set(), _gh()) == []


def test_processed_issue_is_flagged():
    reasons = issue_start.skip_reasons(_issue(), {("acme/app", 7)}, _gh())
    assert any("already handled" in r for r in reasons)


def test_processed_entry_is_repo_scoped():
    # acme/app#7 in the ledger must not flag other/repo#7.
    processed = {("acme/app", 7)}
    other = _issue(repo="other/repo")
    assert not any(
        "already handled" in r
        for r in issue_start.skip_reasons(other, processed, _gh())
    )


def test_skip_list_author_is_flagged_case_insensitively():
    issue = _issue(author="Dependabot")
    reasons = issue_start.skip_reasons(
        issue, set(), _gh(issue_skip_authors=["dependabot"])
    )
    assert any("skip list" in r for r in reasons)


def test_min_age_grace_is_flagged_with_minutes_left():
    issue = _issue(created_at=datetime.now(timezone.utc) - timedelta(minutes=5))
    reasons = issue_start.skip_reasons(issue, set(), _gh(issue_min_age_minutes=15))
    assert any("min-age grace" in r for r in reasons)


def test_multiple_reasons_accumulate():
    issue = _issue(
        author="bot", created_at=datetime.now(timezone.utc) - timedelta(minutes=1)
    )
    reasons = issue_start.skip_reasons(
        issue,
        {("acme/app", 7)},
        _gh(issue_skip_authors=["bot"], issue_min_age_minutes=15),
    )
    assert len(reasons) == 3


# --------------------------------------------------------------------------- #
# session_title / workspace_mode / _resolve_repo_root / _load_config
# --------------------------------------------------------------------------- #
def test_session_title_is_issue_prefixed_slug():
    assert issue_start.session_title(_issue(repo="acme/app", number=7)) == "issue-app-7"


def test_resolve_repo_root_honors_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MINDFLOCK_REPO_ROOT", str(tmp_path))
    assert issue_start._resolve_repo_root() == tmp_path.resolve()


def test_resolve_repo_root_finds_config_toml_ancestor(monkeypatch, tmp_path):
    # The resolver walks the module file's ancestors for a config.toml. Point the
    # module __file__ at a synthetic tree so the test verifies the walk itself,
    # not whether the checkout happens to carry a (gitignored) config.toml.
    monkeypatch.delenv("MINDFLOCK_REPO_ROOT", raising=False)
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "config.toml").write_text("[ticketing]\n")
    monkeypatch.setattr(issue_start, "__file__", str(root / "pkg" / "issue_start.py"))
    assert issue_start._resolve_repo_root() == root.resolve()


def test_load_config_reanchors_relative_workspace_dir(monkeypatch, tmp_path):
    import backend.ticket_ingestion.config as cfg_mod

    monkeypatch.setattr(issue_start, "_REPO_ROOT", tmp_path)
    fake = types.SimpleNamespace(workspace_dir=Path("workspaces"))
    monkeypatch.setattr(cfg_mod, "load_config", lambda: fake)
    assert issue_start._load_config().workspace_dir == tmp_path / "workspaces"


def test_workspace_mode_reads_engine_mode(monkeypatch):
    engine = types.SimpleNamespace(mode="clone")
    monkeypatch.setattr(
        issue_start, "_load_config", lambda: types.SimpleNamespace(engine=engine)
    )
    assert issue_start.workspace_mode() == "clone"


def test_workspace_mode_defaults_and_swallows_errors(monkeypatch):
    def boom():
        raise RuntimeError("no config")

    monkeypatch.setattr(issue_start, "_load_config", boom)
    assert issue_start.workspace_mode() == "worktree"


# --------------------------------------------------------------------------- #
# list_open_issues / find_issue / prepare_start / record_handled (async)
# --------------------------------------------------------------------------- #
class _FakeIssueMonitor:
    issues: list = []
    comments: list = []

    def __init__(self, gh):
        pass

    async def _list_issues(self, repo):
        return list(type(self).issues)

    async def fetch_comments(self, issue):
        return list(type(self).comments)


def _patch_monitor(monkeypatch, *, issues=None, comments=None):
    _FakeIssueMonitor.issues = issues or []
    _FakeIssueMonitor.comments = comments or []
    monkeypatch.setattr(issue_monitor_mod, "IssueMonitor", _FakeIssueMonitor)


async def test_list_open_issues_disabled_when_no_repos(monkeypatch):
    monkeypatch.setattr(
        issue_start,
        "_load_config",
        lambda: types.SimpleNamespace(github=_gh(issue_repos=[])),
    )
    out = await issue_start.list_open_issues()
    assert out == {"repos": [], "enabled": False, "issues": []}


async def test_list_open_issues_annotates(monkeypatch, tmp_path):
    monkeypatch.setattr(issue_start, "_REPO_ROOT", tmp_path)  # empty ledger
    monkeypatch.setattr(
        issue_start, "_load_config", lambda: types.SimpleNamespace(github=_gh())
    )
    _patch_monitor(monkeypatch, issues=[_issue(number=1, author="octocat")])
    out = await issue_start.list_open_issues()
    assert out["enabled"] is True
    assert out["issues"][0]["session"] == "issue-app-1"
    assert out["issues"][0]["eligible"] is True


async def test_find_issue_returns_match_and_missing_raises(monkeypatch):
    monkeypatch.setattr(
        issue_start, "_load_config", lambda: types.SimpleNamespace(github=_gh())
    )
    _patch_monitor(monkeypatch, issues=[_issue(number=3), _issue(number=7)])
    assert (await issue_start.find_issue("acme/app", 7)).number == 7
    with pytest.raises(LookupError, match="No open issue #99"):
        await issue_start.find_issue("acme/app", 99)


async def test_find_issue_unconfigured_raises(monkeypatch):
    monkeypatch.setattr(
        issue_start, "_load_config", lambda: types.SimpleNamespace(github=None)
    )
    with pytest.raises(LookupError, match="not configured"):
        await issue_start.find_issue("acme/app", 1)


async def test_prepare_start_builds_story_prompt_and_branch(monkeypatch):
    # IssueMonitor.fetch_comments is mocked; issue_to_ticket / _build_prompt /
    # _branch_name_for are the real (pure) pipeline pieces.
    monkeypatch.setattr(
        issue_start, "_load_config", lambda: types.SimpleNamespace(github=_gh())
    )
    _patch_monitor(monkeypatch, comments=["[t by u] please fix"])
    issue = _issue(number=7, title="Bug: broken thing")
    story, prompt, branch = await issue_start.prepare_start(issue)
    assert story.slug  # a Ticket was synthesized from the issue
    assert "# Story: Bug: broken thing" in prompt
    assert branch.startswith(f"feature/{story.slug}/")


def test_record_handled_marks_and_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setattr(issue_start, "_REPO_ROOT", tmp_path)
    issue = _issue(number=7, repo="acme/app")
    issue_start.record_handled(issue)
    assert ("acme/app", 7) in load_processed_issues(tmp_path)
    before = len(load_processed_issues(tmp_path))
    issue_start.record_handled(issue)  # already present -> no duplicate
    assert len(load_processed_issues(tmp_path)) == before


def test_record_handled_records_failure_status(monkeypatch, tmp_path):
    monkeypatch.setattr(issue_start, "_REPO_ROOT", tmp_path)
    issue_start.record_handled(_issue(number=8, repo="acme/app"), error="boom")
    assert ("acme/app", 8) in load_processed_issues(tmp_path)


# --------------------------------------------------------------------------- #
# IssueMonitor.scan
# --------------------------------------------------------------------------- #
class TestIssueMonitorScan:
    async def test_filters_by_author_age_and_processed(self, tmp_path, monkeypatch):
        # Empty state dir => nothing processed; never touches the real state.json.
        monkeypatch.setattr(issue_monitor_mod, "_STATE_DIR", tmp_path)
        now = datetime.now(timezone.utc)
        issues = [
            _issue(number=1, author="me", created_at=now - timedelta(minutes=60)),
            _issue(number=2, author="bot", created_at=now - timedelta(minutes=60)),
            _issue(number=3, author="me", created_at=now - timedelta(minutes=1)),
        ]
        monitor = IssueMonitor(
            _gh(issue_skip_authors=["bot"], issue_min_age_minutes=15)
        )
        monitor._list_issues = AsyncMock(return_value=issues)

        eligible = await monitor.scan()
        # #2 skip-listed author; #3 too fresh; only #1 remains.
        assert [i.number for i in eligible] == [1]

    async def test_processed_issues_excluded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(issue_monitor_mod, "_STATE_DIR", tmp_path)
        from backend.ticket_ingestion.models import ProcessedIssue
        from backend.ticket_ingestion.state import record_processed_issue

        now = datetime.now(timezone.utc)
        record_processed_issue(
            tmp_path,
            ProcessedIssue(number=1, processed_at=now, repo="acme/app"),
        )
        issues = [
            _issue(number=1, author="me", created_at=now - timedelta(minutes=60)),
            _issue(number=2, author="me", created_at=now - timedelta(minutes=60)),
        ]
        monitor = IssueMonitor(_gh(issue_min_age_minutes=15))
        monitor._list_issues = AsyncMock(return_value=issues)

        eligible = await monitor.scan()
        assert [i.number for i in eligible] == [2]

    async def test_sorted_by_created_at(self, tmp_path, monkeypatch):
        monkeypatch.setattr(issue_monitor_mod, "_STATE_DIR", tmp_path)
        now = datetime.now(timezone.utc)
        issues = [
            _issue(number=2, created_at=now - timedelta(minutes=30)),
            _issue(number=1, created_at=now - timedelta(minutes=90)),
        ]
        monitor = IssueMonitor(_gh(issue_min_age_minutes=15))
        monitor._list_issues = AsyncMock(return_value=issues)
        eligible = await monitor.scan()
        assert [i.number for i in eligible] == [1, 2]

    async def test_empty_issue_list_short_circuits(self, tmp_path, monkeypatch):
        monkeypatch.setattr(issue_monitor_mod, "_STATE_DIR", tmp_path)
        monitor = IssueMonitor(_gh())
        monitor._list_issues = AsyncMock(return_value=[])
        assert await monitor.scan() == []

    async def test_no_repos_short_circuits(self, tmp_path, monkeypatch):
        monkeypatch.setattr(issue_monitor_mod, "_STATE_DIR", tmp_path)
        monitor = IssueMonitor(_gh(issue_repos=[]))
        # _list_issues must never be reached when no repos are configured.
        monitor._list_issues = AsyncMock(
            side_effect=AssertionError("should not be called")
        )
        assert await monitor.scan() == []
