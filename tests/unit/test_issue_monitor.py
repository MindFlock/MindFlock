"""Hermetic tests for the GitHub issue monitor.

Covers:
  - IssueMonitor.scan filtering (author skip, min-age cutoff, already-processed
    ledger, empty repos / empty issue list short-circuits, chronological sort).
  - IssueMonitor._list_issues parsing (PR entries and non-open issues skipped,
    malformed entries dropped, non-200 -> empty list).
  - IssueMonitor.fetch_comments rendering + error wrapping (non-200 and
    aiohttp.ClientError both surface as IssueCommentsFetchError).
  - issue_to_ticket normalization and _parse_iso.

No network: aiohttp.ClientSession is patched to a fake, and the state dir is
redirected into pytest tmp_path.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from backend.ticket_ingestion import issue_monitor
from backend.ticket_ingestion.config import GithubConfig
from backend.ticket_ingestion.issue_monitor import (
    IssueCommentsFetchError,
    IssueMonitor,
    issue_to_ticket,
)
from backend.ticket_ingestion.models import Issue


def make_config(**overrides) -> GithubConfig:
    base = dict(
        base_branch="main",
        min_age_minutes=15,
        poll_interval_seconds=60,
        enabled=True,
        skip_authors=[],
        token="tok",
        issue_repos=["org/repo"],
        issue_min_age_minutes=15,
        issue_skip_authors=[],
    )
    base.update(overrides)
    return GithubConfig(**base)


class _FakeResponse:
    def __init__(self, status=200, json_data=None, text_data=""):
        self.status = status
        self._json_data = json_data
        self._text_data = text_data

    async def json(self):
        return self._json_data

    async def text(self):
        return self._text_data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _FakeSession:
    def __init__(self, get_responses=None):
        self._get = list(get_responses or [])
        self.get_calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return self._get.pop(0)


def _patch_session(session):
    return patch("aiohttp.ClientSession", return_value=session)


@pytest.fixture(autouse=True)
def _clean_transport_env(monkeypatch):
    """The clone URL now resolves through the repository settings layer, so a
    developer shell that exports either of these must not steer the assertions."""
    monkeypatch.delenv("MINDFLOCK_REPO_URL", raising=False)
    monkeypatch.delenv("MINDFLOCK_GIT_TRANSPORT", raising=False)


@pytest.fixture(autouse=True)
def _headers_no_network(monkeypatch):
    """_headers awaits resolve_token (which may shell to gh). Stub it so no
    test ever touches the auth chain."""

    async def fake_headers(self):
        return {"Authorization": "Bearer x"}

    monkeypatch.setattr(IssueMonitor, "_headers", fake_headers)


def _issue_item(number, login, created_at, state="open", pull_request=False):
    item = {
        "number": number,
        "title": f"Issue {number}",
        "body": "some body",
        "html_url": f"http://gh/{number}",
        "user": {"login": login},
        "created_at": created_at,
        "state": state,
    }
    if pull_request:
        item["pull_request"] = {"url": "x"}
    return item


# --------------------------------------------------------------------------- #
# _parse_iso
# --------------------------------------------------------------------------- #
class TestParseIso:
    def test_z_suffix_parsed_as_utc(self):
        ts = issue_monitor._parse_iso("2025-01-15T10:00:00Z")
        assert ts == datetime(2025, 1, 15, 10, 0, tzinfo=timezone.utc)

    def test_naive_gets_utc(self):
        ts = issue_monitor._parse_iso("2025-01-15T10:00:00")
        assert ts.tzinfo == timezone.utc

    def test_offset_preserved(self):
        ts = issue_monitor._parse_iso("2025-01-15T10:00:00+02:00")
        assert ts.utcoffset() == timedelta(hours=2)


# --------------------------------------------------------------------------- #
# _list_issues
# --------------------------------------------------------------------------- #
class TestListIssues:
    async def _list(self, raw, status=200):
        monitor = IssueMonitor(make_config())
        resp = _FakeResponse(status=status, json_data=raw, text_data="err")
        session = _FakeSession(get_responses=[resp])
        with _patch_session(session):
            return await monitor._list_issues("org/repo"), session

    async def test_parses_issue_and_sets_repo_clone_url(self):
        raw = [_issue_item(5, "alice", "2025-01-15T10:00:00Z")]
        out, session = await self._list(raw)
        assert len(out) == 1
        issue = out[0]
        assert issue.number == 5
        assert issue.author == "alice"
        assert issue.repo == "org/repo"
        # Nothing configured -> HTTPS, the historic synthesized URL.
        assert issue.clone_url == "https://github.com/org/repo.git"
        assert issue.created_at == datetime(2025, 1, 15, 10, 0, tzinfo=timezone.utc)
        params = session.get_calls[0][1]["params"]
        assert params["state"] == "open"

    @pytest.mark.parametrize(
        "transport,expected",
        [
            ("ssh", "git@github.com:org/repo.git"),
            ("https", "https://github.com/org/repo.git"),
        ],
    )
    async def test_clone_url_follows_configured_transport(
        self, monkeypatch, transport, expected
    ):
        monkeypatch.setenv("MINDFLOCK_GIT_TRANSPORT", transport)
        out, _ = await self._list([_issue_item(5, "alice", "2025-01-15T10:00:00Z")])
        assert out[0].clone_url == expected

    async def test_auto_transport_keeps_the_users_own_ssh_spelling(self, monkeypatch):
        """The contributor bug: an SSH-only checkout must not be handed HTTPS."""
        monkeypatch.setenv("MINDFLOCK_REPO_URL", "git@github.com:org/repo.git")
        out, _ = await self._list([_issue_item(5, "alice", "2025-01-15T10:00:00Z")])
        assert out[0].clone_url == "git@github.com:org/repo.git"

    async def test_auto_transport_ignores_a_url_for_another_repo(self, monkeypatch):
        # The global repo URL names a DIFFERENT repo, so it must not be cloned
        # in place of the one the issue actually lives in.
        monkeypatch.setenv("MINDFLOCK_REPO_URL", "git@github.com:org/other.git")
        out, _ = await self._list([_issue_item(5, "alice", "2025-01-15T10:00:00Z")])
        assert out[0].clone_url == "https://github.com/org/repo.git"

    async def test_repository_payload_ssh_url_is_used_when_present(self, monkeypatch):
        monkeypatch.setenv("MINDFLOCK_GIT_TRANSPORT", "ssh")
        item = _issue_item(5, "alice", "2025-01-15T10:00:00Z")
        item["repository"] = {
            "clone_url": "https://ghe.corp/org/repo.git",
            "ssh_url": "git@ghe.corp:org/repo.git",
        }
        out, _ = await self._list([item])
        assert out[0].clone_url == "git@ghe.corp:org/repo.git"

    async def test_pull_requests_skipped(self):
        raw = [_issue_item(5, "a", "2025-01-15T10:00:00Z", pull_request=True)]
        out, _ = await self._list(raw)
        assert out == []

    async def test_non_open_state_skipped(self):
        raw = [_issue_item(5, "a", "2025-01-15T10:00:00Z", state="closed")]
        out, _ = await self._list(raw)
        assert out == []

    async def test_malformed_entry_skipped_others_kept(self):
        good = _issue_item(5, "a", "2025-01-15T10:00:00Z")
        # state=open so it reaches the int() conversion, which raises on the
        # non-numeric number and exercises the except branch.
        bad = {
            "number": "not-an-int",
            "state": "open",
            "created_at": "2025-01-15T10:00:00Z",
        }
        out, _ = await self._list([bad, good])
        assert [i.number for i in out] == [5]

    async def test_non_200_returns_empty(self):
        out, _ = await self._list([], status=500)
        assert out == []


# --------------------------------------------------------------------------- #
# fetch_comments
# --------------------------------------------------------------------------- #
class TestFetchComments:
    def _issue(self):
        return Issue(
            number=7,
            title="t",
            body="b",
            url="u",
            author="a",
            created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            repo="org/repo",
        )

    async def _fetch(self, resp):
        monitor = IssueMonitor(make_config())
        session = _FakeSession(get_responses=[resp])
        with _patch_session(session):
            return await monitor.fetch_comments(self._issue())

    async def test_renders_comments_and_skips_empty(self):
        raw = [
            {"body": "first", "user": {"login": "bob"}, "created_at": "2025-01-02"},
            {"body": "   ", "user": {"login": "x"}},  # whitespace-only -> skipped
            {"body": "no author"},  # missing user -> "unknown"
        ]
        out = await self._fetch(_FakeResponse(status=200, json_data=raw))
        assert out == [
            "[2025-01-02 by bob] first",
            "[ by unknown] no author",
        ]

    async def test_non_200_raises_fetch_error(self):
        with pytest.raises(IssueCommentsFetchError, match="502"):
            await self._fetch(_FakeResponse(status=502, text_data="bad gateway"))

    async def test_client_error_wrapped(self):
        import aiohttp

        monitor = IssueMonitor(make_config())
        with patch(
            "aiohttp.ClientSession",
            side_effect=aiohttp.ClientError("connection reset"),
        ):
            with pytest.raises(IssueCommentsFetchError, match="connection reset"):
                await monitor.fetch_comments(self._issue())


# --------------------------------------------------------------------------- #
# scan
# --------------------------------------------------------------------------- #
class TestScan:
    async def test_empty_repos_short_circuits(self):
        monitor = IssueMonitor(make_config(issue_repos=[]))
        monitor._list_issues = AsyncMock(side_effect=AssertionError("should not list"))
        assert await monitor.scan() == []

    async def test_filters_author_age_and_processed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(issue_monitor, "_STATE_DIR", tmp_path)
        now = datetime.now(timezone.utc)
        issues = [
            Issue(1, "t", "b", "u", "me", now - timedelta(minutes=60), repo="org/repo"),
            Issue(
                2, "t", "b", "u", "skipme", now - timedelta(minutes=60), repo="org/repo"
            ),
            Issue(3, "t", "b", "u", "me", now - timedelta(minutes=1), repo="org/repo"),
        ]
        monitor = IssueMonitor(
            make_config(issue_skip_authors=["SkipMe"], issue_min_age_minutes=15)
        )
        monitor._list_issues = AsyncMock(return_value=issues)
        eligible = await monitor.scan()
        # #2 skipped author (case-insensitive), #3 too fresh -> only #1.
        assert [i.number for i in eligible] == [1]

    async def test_processed_issues_excluded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(issue_monitor, "_STATE_DIR", tmp_path)
        from backend.ticket_ingestion.models import ProcessedIssue
        from backend.ticket_ingestion.state import record_processed_issue

        now = datetime.now(timezone.utc)
        record_processed_issue(
            tmp_path, ProcessedIssue(number=1, processed_at=now, repo="org/repo")
        )
        issues = [
            Issue(1, "t", "b", "u", "me", now - timedelta(minutes=60), repo="org/repo"),
            Issue(2, "t", "b", "u", "me", now - timedelta(minutes=60), repo="org/repo"),
        ]
        monitor = IssueMonitor(make_config(issue_min_age_minutes=15))
        monitor._list_issues = AsyncMock(return_value=issues)
        eligible = await monitor.scan()
        assert [i.number for i in eligible] == [2]

    async def test_sorted_by_created_at(self, tmp_path, monkeypatch):
        monkeypatch.setattr(issue_monitor, "_STATE_DIR", tmp_path)
        now = datetime.now(timezone.utc)
        issues = [
            Issue(2, "t", "b", "u", "me", now - timedelta(minutes=30), repo="org/repo"),
            Issue(1, "t", "b", "u", "me", now - timedelta(minutes=90), repo="org/repo"),
        ]
        monitor = IssueMonitor(make_config(issue_min_age_minutes=15))
        monitor._list_issues = AsyncMock(return_value=issues)
        eligible = await monitor.scan()
        assert [i.number for i in eligible] == [1, 2]

    async def test_no_issues_short_circuits(self, tmp_path, monkeypatch):
        monkeypatch.setattr(issue_monitor, "_STATE_DIR", tmp_path)
        monitor = IssueMonitor(make_config())
        monitor._list_issues = AsyncMock(return_value=[])
        assert await monitor.scan() == []


# --------------------------------------------------------------------------- #
# issue_to_ticket
# --------------------------------------------------------------------------- #
class TestIssueToTicket:
    def test_normalizes_issue_into_ticket(self):
        issue = Issue(
            number=42,
            title="Fix the bug",
            body="## Acceptance Criteria\n- no crash",
            url="http://gh/42",
            author="alice",
            created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            repo="org/repo",
            clone_url="https://github.com/org/repo.git",
        )
        t = issue_to_ticket(issue, ["a comment"])
        assert t.id == 42
        assert t.name == "Fix the bug"
        assert t.provider == "github_issues"
        assert t.slug == "issue-repo-42"
        assert t.source_label == "GitHub Issue"
        assert t.acceptance_criteria == ["no crash"]
        assert t.comments == ["a comment"]
        assert t.repo_url == "https://github.com/org/repo.git"
        assert t.app_url == "http://gh/42"

    def test_empty_body_falls_back_to_title(self):
        issue = Issue(
            number=9,
            title="Only a title",
            body="",
            url="u",
            author="a",
            created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            repo="org/repo",
        )
        t = issue_to_ticket(issue, [])
        assert t.description == "Only a title"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
