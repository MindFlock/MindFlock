"""Hermetic tests for the PR pipeline auth + fetch helpers.

Covers:
  - github_auth.resolve_token precedence (config.token -> env -> `gh auth token`)
    with process-global caching, mocking subprocess + env.
  - pr_comments._headers / _extract_ai_prompt and _fetch_review_comments
    parsing (aiohttp mocked).
  - pr_monitor.PRMonitor._list_prs / _authenticated_user_login / scan parsing
    and _parse_iso (aiohttp mocked).

No network, no real `gh`, no tmux. All subprocess/aiohttp calls are mocked and
all state writes are confined to pytest tmp_path.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.ticket_ingestion import github_auth, pr_comments, pr_monitor
from backend.ticket_ingestion.config import GithubConfig
from backend.ticket_ingestion.github_auth import (
    GithubAuthError,
    resolve_token,
)
from backend.ticket_ingestion.models import PRComment, PullRequest

# A dummy commit SHA used across the PR-checkout fixtures/assertions. Named so
# detect-secrets' hex-entropy heuristic sees one allowlisted definition instead
# of a literal at every use site.
_FAKE_SHA = "abcdef1234"  # pragma: allowlist secret

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def make_config(**overrides) -> GithubConfig:
    base = dict(
        repos=["org/example-bot"],
        base_branch="staging",
        min_age_minutes=15,
        poll_interval_seconds=60,
        enabled=True,
        skip_authors=[],
        token="",
    )
    base.update(overrides)
    return GithubConfig(**base)


class _FakeResponse:
    """Stand-in for an aiohttp response used as an async context manager."""

    def __init__(self, status=200, json_data=None, text_data="", headers=None):
        self.status = status
        self._json_data = json_data
        self._text_data = text_data
        self.headers = headers or {}

    async def json(self):
        return self._json_data

    async def text(self):
        return self._text_data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _FakeSession:
    """Stand-in for aiohttp.ClientSession.

    `get_responses` / `post_responses` are lists consumed in order so that
    paginated GETs can be simulated. Records the calls for assertions.
    """

    def __init__(self, get_responses=None, post_responses=None):
        self._get_responses = list(get_responses or [])
        self._post_responses = list(post_responses or [])
        self.get_calls = []
        self.post_calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return self._get_responses.pop(0)

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return self._post_responses.pop(0)


def patch_session(session):
    """Patch aiohttp.ClientSession so `async with aiohttp.ClientSession()`
    yields the given fake session."""
    return patch("aiohttp.ClientSession", return_value=session)


@pytest.fixture(autouse=True)
def _reset_token_cache():
    """resolve_token caches in a module global; reset around every test so
    tests do not leak the cache into one another."""
    github_auth._cached_token = None
    yield
    github_auth._cached_token = None


@pytest.fixture(autouse=True)
def _clear_gh_env(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


# --------------------------------------------------------------------------
# github_auth.resolve_token
# --------------------------------------------------------------------------


class TestResolveTokenPrecedence:
    async def test_config_token_wins(self, monkeypatch):
        monkeypatch.setenv("GH_TOKEN", "env-token")
        cfg = make_config(token="config-token")
        # gh must never be shelled out to when config.token is set.
        with patch.object(github_auth, "_gh_auth_token", new_callable=AsyncMock) as gh:
            token = await resolve_token(cfg)
        assert token == "config-token"
        gh.assert_not_called()

    async def test_env_gh_token_used_when_no_config(self, monkeypatch):
        monkeypatch.setenv("GH_TOKEN", "env-token")
        cfg = make_config(token="")
        with patch.object(github_auth, "_gh_auth_token", new_callable=AsyncMock) as gh:
            token = await resolve_token(cfg)
        assert token == "env-token"
        gh.assert_not_called()

    async def test_github_token_env_used_when_no_gh_token(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "gh-env")
        cfg = make_config(token="")
        token = await resolve_token(cfg)
        assert token == "gh-env"

    async def test_gh_token_preferred_over_github_token(self, monkeypatch):
        # GH_TOKEN is checked before GITHUB_TOKEN.
        monkeypatch.setenv("GH_TOKEN", "primary")
        monkeypatch.setenv("GITHUB_TOKEN", "secondary")
        cfg = make_config(token="")
        token = await resolve_token(cfg)
        assert token == "primary"

    async def test_falls_back_to_gh_cli(self):
        cfg = make_config(token="")
        with patch.object(
            github_auth,
            "_gh_auth_token",
            new_callable=AsyncMock,
            return_value="cli-token",
        ):
            token = await resolve_token(cfg)
        assert token == "cli-token"

    async def test_raises_when_nothing_available(self):
        cfg = make_config(token="")
        with patch.object(
            github_auth,
            "_gh_auth_token",
            new_callable=AsyncMock,
            return_value=None,
        ):
            with pytest.raises(GithubAuthError):
                await resolve_token(cfg)

    async def test_result_is_cached_across_calls(self):
        cfg = make_config(token="")
        with patch.object(
            github_auth,
            "_gh_auth_token",
            new_callable=AsyncMock,
            return_value="cli-token",
        ) as gh:
            first = await resolve_token(cfg)
            # A second call with a different config still returns the cached
            # value and does not re-shell.
            second = await resolve_token(make_config(token="ignored-now"))
        assert first == "cli-token"
        assert second == "cli-token"
        gh.assert_awaited_once()


class TestGhAuthTokenSubprocess:
    async def test_returns_stripped_token_on_success(self):
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"  ghp_abc123\n", b""))
        with patch(
            "asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=proc,
        ) as spawn:
            token = await github_auth._gh_auth_token()
        assert token == "ghp_abc123"
        # Confirm we invoked `gh auth token`, not anything else.
        args = spawn.await_args.args
        assert args[:3] == ("gh", "auth", "token")

    async def test_returns_none_on_nonzero_exit(self):
        proc = MagicMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b"not logged in"))
        with patch(
            "asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=proc,
        ):
            token = await github_auth._gh_auth_token()
        assert token is None

    async def test_returns_none_on_empty_output(self):
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"   \n", b""))
        with patch(
            "asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=proc,
        ):
            token = await github_auth._gh_auth_token()
        assert token is None

    async def test_returns_none_when_gh_missing(self):
        with patch(
            "asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            side_effect=FileNotFoundError,
        ):
            token = await github_auth._gh_auth_token()
        assert token is None


# --------------------------------------------------------------------------
# pr_comments pure helpers
# --------------------------------------------------------------------------


class TestHeaders:
    def test_headers_shape(self):
        h = pr_comments._headers("tok123")
        assert h["Authorization"] == "Bearer tok123"
        assert h["Accept"] == "application/vnd.github+json"
        assert h["X-GitHub-Api-Version"] == "2022-11-28"


class TestExtractAiPrompt:
    def test_non_coderabbit_body_unchanged(self):
        body = "<details><summary>Prompt for AI Agents</summary>do X</details>"
        # coderabbitai not present anywhere -> unchanged.
        assert pr_comments._extract_ai_prompt(body) == body

    def test_coderabbit_extracts_inner_prompt(self):
        body = (
            "intro\n<details>\n<summary>🤖 Prompt for AI Agents</summary>\n"
            "Fix the null check on line 5.\n</details>\ntrailer"
        )
        out = pr_comments._extract_ai_prompt(body, "coderabbitai[bot]")
        assert out == "Fix the null check on line 5."

    def test_coderabbit_detected_via_haystack(self):
        body = (
            "<details><summary>Prompt for AI Agents</summary>"
            "`inner instruction`</details>"
        )
        # coderabbitai only appears in a haystack (e.g. the comment URL).
        out = pr_comments._extract_ai_prompt(
            body, "someuser", "https://github.com/x/coderabbitai/1"
        )
        assert out == "inner instruction"

    def test_coderabbit_without_details_block_unchanged(self):
        body = "coderabbitai says: just a plain comment, no details"
        assert pr_comments._extract_ai_prompt(body) == body

    def test_empty_inner_falls_back_to_body(self):
        body = (
            "coderabbitai\n<details><summary>Prompt for AI Agents</summary>"
            "</details>"
        )
        # Inner strips to empty -> return original body.
        assert pr_comments._extract_ai_prompt(body) == body


# --------------------------------------------------------------------------
# pr_comments._fetch_review_comments
# --------------------------------------------------------------------------


def _thread(comments, resolved=False, outdated=False):
    return {
        "isResolved": resolved,
        "isOutdated": outdated,
        "comments": {"nodes": comments},
    }


def _review_comment(
    db_id, login, body="body", url="http://x", path="a.py", line=3, hunk="@@"
):
    return {
        "databaseId": db_id,
        "author": {"login": login},
        "body": body,
        "path": path,
        "line": line,
        "diffHunk": hunk,
        "url": url,
    }


def _graphql_data(threads):
    return {
        "data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": threads}}}}
    }


class TestFetchReviewComments:
    async def _run(self, resp, skip=None):
        session = _FakeSession(post_responses=[resp])
        return (
            await pr_comments._fetch_review_comments(
                session, "tok", "owner", "name", 7, skip or set()
            ),
            session,
        )

    async def test_parses_review_comment(self):
        data = _graphql_data([_thread([_review_comment(101, "alice")])])
        resp = _FakeResponse(status=200, json_data=data)
        out, session = await self._run(resp)
        assert len(out) == 1
        c = out[0]
        assert isinstance(c, PRComment)
        assert c.id == 101
        assert c.kind == "review"
        assert c.author == "alice"
        assert c.path == "a.py"
        assert c.line == 3
        assert c.diff_hunk == "@@"
        # Correct GraphQL endpoint hit.
        assert session.post_calls[0][0].endswith("/graphql")

    async def test_skips_resolved_and_outdated_threads(self):
        threads = [
            _thread([_review_comment(1, "a")], resolved=True),
            _thread([_review_comment(2, "b")], outdated=True),
            _thread([_review_comment(3, "c")]),
        ]
        resp = _FakeResponse(status=200, json_data=_graphql_data(threads))
        out, _ = await self._run(resp)
        assert [c.id for c in out] == [3]

    async def test_skips_authors_in_skip_set(self):
        threads = [_thread([_review_comment(1, "author"), _review_comment(2, "bob")])]
        resp = _FakeResponse(status=200, json_data=_graphql_data(threads))
        out, _ = await self._run(resp, skip={"author"})
        assert [c.id for c in out] == [2]

    async def test_skips_comment_without_int_database_id(self):
        c = _review_comment(1, "x")
        c["databaseId"] = None
        resp = _FakeResponse(status=200, json_data=_graphql_data([_thread([c])]))
        out, _ = await self._run(resp)
        assert out == []

    async def test_non_int_line_becomes_none(self):
        c = _review_comment(5, "x", line="not-an-int")
        resp = _FakeResponse(status=200, json_data=_graphql_data([_thread([c])]))
        out, _ = await self._run(resp)
        assert out[0].line is None

    async def test_non_200_raises_fetch_error(self):
        # A transient 5xx must NOT read as "no comments" (which would make the
        # caller permanently record the PR as processed) — it raises instead.
        resp = _FakeResponse(status=502, text_data="bad gateway")
        with pytest.raises(pr_comments.PRCommentsFetchError, match="502"):
            await self._run(resp)

    async def test_graphql_errors_raise_fetch_error(self):
        resp = _FakeResponse(status=200, json_data={"errors": [{"message": "boom"}]})
        with pytest.raises(pr_comments.PRCommentsFetchError, match="boom"):
            await self._run(resp)

    async def test_coderabbit_body_extracted(self):
        body = (
            "<details><summary>Prompt for AI Agents</summary>" "Do the thing.</details>"
        )
        c = _review_comment(9, "coderabbitai[bot]", body=body)
        resp = _FakeResponse(status=200, json_data=_graphql_data([_thread([c])]))
        out, _ = await self._run(resp)
        assert out[0].body == "Do the thing."


# --------------------------------------------------------------------------
# pr_comments.fetch_actionable_comments
# --------------------------------------------------------------------------


class TestFetchActionableComments:
    async def test_uses_pr_author_as_skip_and_returns_review(self):
        pr = PullRequest(
            number=7,
            head_ref="feature",
            head_sha="abc",
            base_ref="staging",
            title="t",
            url="http://pr/7",
            author="prauthor",
            created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            repo="org/example-bot",
        )
        cfg = make_config(token="config-token", skip_authors=["bot"])
        threads = [
            _thread(
                [
                    _review_comment(1, "prauthor"),  # PR author -> skipped
                    _review_comment(2, "bot"),  # skip_authors -> skipped
                    _review_comment(3, "reviewer"),  # kept
                ]
            )
        ]
        resp = _FakeResponse(status=200, json_data=_graphql_data(threads))
        session = _FakeSession(post_responses=[resp])
        with patch_session(session):
            out = await pr_comments.fetch_actionable_comments(pr, cfg)
        assert [c.id for c in out] == [3]

    async def test_network_error_wrapped_in_fetch_error(self):
        # aiohttp/timeout failures surface as PRCommentsFetchError so callers
        # can retry on the next poll instead of recording the PR processed.
        import aiohttp

        pr = PullRequest(
            number=8,
            head_ref="feature",
            head_sha="abc",
            base_ref="staging",
            title="t",
            url="http://pr/8",
            author="prauthor",
            created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            repo="org/example-bot",
        )
        cfg = make_config(token="config-token")
        with patch(
            "aiohttp.ClientSession",
            side_effect=aiohttp.ClientError("connection reset"),
        ):
            with pytest.raises(pr_comments.PRCommentsFetchError):
                await pr_comments.fetch_actionable_comments(pr, cfg)


# --------------------------------------------------------------------------
# pr_runner._wait_for_done_markers deadline
# --------------------------------------------------------------------------


class TestWaitForDoneMarkersDeadline:
    async def test_deadline_kills_sessions_and_returns(self, tmp_path):
        """Without xdotool/done markers the wait used to spin forever; the
        wall-clock deadline bails out and kills the sessions it waited on."""
        from backend.ticket_ingestion import pr_runner

        marker = tmp_path / "never.done"  # never created -> never "done"
        killed = []

        async def fake_kill(session):
            killed.append(session)

        with (
            patch.object(pr_runner, "_kill_session", side_effect=fake_kill),
            patch.object(
                pr_runner,
                "_cursor_window_alive",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            await pr_runner._wait_for_done_markers(
                [marker],
                "workspace-name",
                sessions=["pr-1-c-2"],
                deadline_seconds=0.0,
            )
        assert killed == ["pr-1-c-2"]


# --------------------------------------------------------------------------
# pr_monitor
# --------------------------------------------------------------------------


class TestParseIso:
    def test_z_suffix_parsed_as_utc(self):
        ts = pr_monitor._parse_iso("2025-01-15T10:00:00Z")
        assert ts.tzinfo is not None
        assert ts == datetime(2025, 1, 15, 10, 0, tzinfo=timezone.utc)

    def test_naive_timestamp_gets_utc(self):
        ts = pr_monitor._parse_iso("2025-01-15T10:00:00")
        assert ts.tzinfo == timezone.utc

    def test_offset_preserved(self):
        ts = pr_monitor._parse_iso("2025-01-15T10:00:00+02:00")
        assert ts.utcoffset() == timedelta(hours=2)


def _pr_item(number, login, created_at, draft=False, state="open"):
    return {
        "number": number,
        "head": {"ref": f"feat-{number}", "sha": f"sha{number}"},
        "base": {"ref": "staging"},
        "title": f"PR {number}",
        "html_url": f"http://pr/{number}",
        "user": {"login": login},
        "created_at": created_at,
        "draft": draft,
        "state": state,
    }


class TestPRMonitorListPrs:
    async def _list(self, raw):
        monitor = pr_monitor.PRMonitor(make_config(token="tok"))
        resp = _FakeResponse(status=200, json_data=raw)
        session = _FakeSession(get_responses=[resp])
        with patch_session(session):
            return await monitor._list_prs("org/example-bot"), session

    async def test_parses_open_pr(self):
        raw = [_pr_item(5, "alice", "2025-01-15T10:00:00Z")]
        out, session = await self._list(raw)
        assert len(out) == 1
        pr = out[0]
        assert pr.number == 5
        assert pr.head_ref == "feat-5"
        assert pr.head_sha == "sha5"
        assert pr.base_ref == "staging"
        assert pr.author == "alice"
        assert pr.created_at == datetime(2025, 1, 15, 10, 0, tzinfo=timezone.utc)
        # Query params include base/state/per_page.
        params = session.get_calls[0][1]["params"]
        assert params["base"] == "staging"
        assert params["state"] == "open"

    async def test_draft_pr_skipped(self):
        raw = [_pr_item(5, "a", "2025-01-15T10:00:00Z", draft=True)]
        out, _ = await self._list(raw)
        assert out == []

    async def test_non_open_state_skipped(self):
        raw = [_pr_item(5, "a", "2025-01-15T10:00:00Z", state="closed")]
        out, _ = await self._list(raw)
        assert out == []

    async def test_malformed_entry_skipped_others_kept(self):
        good = _pr_item(5, "a", "2025-01-15T10:00:00Z")
        bad = {"number": 6}  # missing head/base/created_at
        out, _ = await self._list([bad, good])
        assert [p.number for p in out] == [5]

    async def test_non_200_returns_empty(self):
        monitor = pr_monitor.PRMonitor(make_config(token="tok"))
        resp = _FakeResponse(status=500, text_data="err")
        session = _FakeSession(get_responses=[resp])
        with patch_session(session):
            out = await monitor._list_prs("org/example-bot")
        assert out == []


class TestPRMonitorAuthenticatedUser:
    async def test_returns_login_and_caches(self):
        monitor = pr_monitor.PRMonitor(make_config(token="tok"))
        resp = _FakeResponse(status=200, json_data={"login": "octocat"})
        session = _FakeSession(get_responses=[resp])
        with patch_session(session):
            login = await monitor._authenticated_user_login()
        assert login == "octocat"
        assert monitor._my_login == "octocat"
        # Cached: a second call must not touch the network.
        login2 = await monitor._authenticated_user_login()
        assert login2 == "octocat"

    async def test_non_200_raises(self):
        monitor = pr_monitor.PRMonitor(make_config(token="tok"))
        resp = _FakeResponse(status=401, json_data={})
        session = _FakeSession(get_responses=[resp])
        with patch_session(session):
            with pytest.raises(RuntimeError):
                await monitor._authenticated_user_login()

    async def test_empty_login_raises(self):
        monitor = pr_monitor.PRMonitor(make_config(token="tok"))
        resp = _FakeResponse(status=200, json_data={"login": "  "})
        session = _FakeSession(get_responses=[resp])
        with patch_session(session):
            with pytest.raises(RuntimeError):
                await monitor._authenticated_user_login()


class TestPRMonitorScan:
    async def test_filters_by_author_age_and_processed(self, tmp_path, monkeypatch):
        # Redirect the module's state dir into tmp_path (empty -> nothing
        # processed) so scan never reads the real repo's state.json.
        monkeypatch.setattr(pr_monitor, "_STATE_DIR", tmp_path)

        now = datetime.now(timezone.utc)

        prs = [
            PullRequest(
                1, "f1", "s1", "staging", "t", "u", "me", now - timedelta(minutes=60)
            ),
            PullRequest(
                2, "f2", "s2", "staging", "t", "u", "other", now - timedelta(minutes=60)
            ),
            PullRequest(
                3, "f3", "s3", "staging", "t", "u", "me", now - timedelta(minutes=1)
            ),
        ]
        monitor = pr_monitor.PRMonitor(make_config(token="tok", min_age_minutes=15))
        monitor._list_prs = AsyncMock(return_value=prs)
        monitor._authenticated_user_login = AsyncMock(return_value="me")

        eligible = await monitor.scan()
        # PR 2 wrong author; PR 3 too fresh; only PR 1 remains.
        assert [p.number for p in eligible] == [1]

    async def test_processed_prs_excluded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pr_monitor, "_STATE_DIR", tmp_path)
        # Seed a processed PR via the real state writer inside tmp_path.
        from backend.ticket_ingestion.state import record_processed_pr
        from backend.ticket_ingestion.models import ProcessedPR

        now = datetime.now(timezone.utc)
        record_processed_pr(
            tmp_path,
            ProcessedPR(number=1, head_sha="s1", processed_at=now),
        )
        prs = [
            PullRequest(
                1, "f1", "s1", "staging", "t", "u", "me", now - timedelta(minutes=60)
            ),
            PullRequest(
                2, "f2", "s2", "staging", "t", "u", "me", now - timedelta(minutes=60)
            ),
        ]
        monitor = pr_monitor.PRMonitor(make_config(token="tok", min_age_minutes=15))
        monitor._list_prs = AsyncMock(return_value=prs)
        monitor._authenticated_user_login = AsyncMock(return_value="me")

        eligible = await monitor.scan()
        assert [p.number for p in eligible] == [2]

    async def test_scans_multiple_repos_and_dedups_by_repo(self, tmp_path, monkeypatch):
        # Multi-repo review: scan every configured repo, and key the
        # already-processed filter on (repo, number) so a PR-number collision
        # across repos doesn't drop a genuinely new PR.
        monkeypatch.setattr(pr_monitor, "_STATE_DIR", tmp_path)
        from backend.ticket_ingestion.state import record_processed_pr
        from backend.ticket_ingestion.models import ProcessedPR

        now = datetime.now(timezone.utc)
        old = now - timedelta(minutes=60)
        # org/a PR#5 already handled; org/b PR#5 is a different PR entirely.
        record_processed_pr(
            tmp_path,
            ProcessedPR(number=5, head_sha="sa", processed_at=now, repo="org/a"),
        )
        by_repo = {
            "org/a": [
                PullRequest(5, "f", "sa", "main", "t", "u", "me", old, repo="org/a")
            ],
            "org/b": [
                PullRequest(5, "f", "sb", "main", "t", "u", "me", old, repo="org/b")
            ],
        }
        monitor = pr_monitor.PRMonitor(
            make_config(token="tok", repos=["org/a", "org/b"], min_age_minutes=15)
        )

        async def fake_list(repo):
            return by_repo[repo]

        monitor._list_prs = fake_list
        monitor._authenticated_user_login = AsyncMock(return_value="me")

        eligible = await monitor.scan()
        # org/a#5 excluded (processed); org/b#5 kept despite the shared number.
        assert [(p.repo, p.number) for p in eligible] == [("org/b", 5)]

    async def test_sorted_by_created_at(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pr_monitor, "_STATE_DIR", tmp_path)
        now = datetime.now(timezone.utc)
        prs = [
            PullRequest(
                2, "f", "s", "staging", "t", "u", "me", now - timedelta(minutes=30)
            ),
            PullRequest(
                1, "f", "s", "staging", "t", "u", "me", now - timedelta(minutes=90)
            ),
        ]
        monitor = pr_monitor.PRMonitor(make_config(token="tok", min_age_minutes=15))
        monitor._list_prs = AsyncMock(return_value=prs)
        monitor._authenticated_user_login = AsyncMock(return_value="me")
        eligible = await monitor.scan()
        assert [p.number for p in eligible] == [1, 2]

    async def test_empty_pr_list_short_circuits(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pr_monitor, "_STATE_DIR", tmp_path)
        monitor = pr_monitor.PRMonitor(make_config(token="tok"))
        monitor._list_prs = AsyncMock(return_value=[])
        # Should not need to resolve the authenticated user at all.
        monitor._authenticated_user_login = AsyncMock(
            side_effect=AssertionError("should not be called")
        )
        eligible = await monitor.scan()
        assert eligible == []


# --------------------------------------------------------------------------
# pr_runner._strip_media (keeps binary/large media out of agent prompts)
# --------------------------------------------------------------------------


class TestStripMedia:
    def _strip(self, text):
        from backend.ticket_ingestion.pr_runner import _strip_media

        return _strip_media(text)

    def test_none_and_empty_become_empty_string(self):
        assert self._strip(None) == ""
        assert self._strip("") == ""

    def test_markdown_image_omitted(self):
        assert (
            self._strip("before ![alt](http://x/y.png) after")
            == "before [image omitted] after"
        )

    def test_html_video_omitted_across_newlines(self):
        # DOTALL: the <video>…</video> block spans newlines.
        body = "a <video src='x'>\nfallback text\n</video> b"
        assert self._strip(body) == "a [video omitted] b"

    def test_html_img_tag_omitted(self):
        assert (
            self._strip("<img src='http://x/a.png' alt='z'> text")
            == "[image omitted] text"
        )

    def test_github_attachment_urls_omitted(self):
        url = "https://github.com/user-attachments/assets/abc-123"
        assert self._strip(f"see {url} here") == "see [attachment omitted] here"
        url2 = "https://user-images.githubusercontent.com/1/2.png"
        assert self._strip(f"see {url2}") == "see [attachment omitted]"

    def test_data_uri_omitted(self):
        body = "img data:image/png;base64,AAAABBBB end"
        assert self._strip(body) == "img [data-uri omitted] end"

    def test_plain_text_passes_through_unchanged(self):
        assert (
            self._strip("just words, no media at all") == "just words, no media at all"
        )


# --------------------------------------------------------------------------
# pr_provisioner: clone/fetch/checkout recovery branches (subprocess-scripted)
# --------------------------------------------------------------------------


class _ScriptedRun:
    """Stand-in for pr_provisioner._run: records the git argv of each call and
    returns scripted ``(rc, stdout, stderr)`` tuples (stderr non-empty on
    failure so callers' ``.decode()`` handling runs)."""

    def __init__(self, rcs):
        self._rcs = list(rcs)
        self.calls = []

    async def __call__(self, *args, cwd=None, timeout=None):
        self.calls.append(args)
        rc = self._rcs.pop(0)
        return rc, b"", (b"" if rc == 0 else b"boom")


class TestPRProvisionerGitFlow:
    def _pr(self):
        return PullRequest(
            number=9,
            head_ref="feat",
            head_sha=_FAKE_SHA,
            base_ref="main",
            title="t",
            url="u",
            author="a",
            created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            repo="org/repo",
            clone_url="https://github.com/org/repo.git",
        )

    def _provisioner(self, tmp_path):
        from backend.ticket_ingestion.config import PipelineConfig
        from backend.ticket_ingestion.pr_provisioner import PRProvisioner

        cfg = PipelineConfig(
            repo_url="https://github.com/org/repo.git",
            workspace_dir=tmp_path / "ws",
        )
        return PRProvisioner(cfg)

    async def test_blobless_clone_falls_back_to_full_clone(self, tmp_path, monkeypatch):
        from backend.ticket_ingestion import pr_provisioner

        prov = self._provisioner(tmp_path)
        directory = tmp_path / "pr-ws"
        # blobless clone fails; full clone ok; fetch ok; checkout ok; reset ok.
        scripted = _ScriptedRun([1, 0, 0, 0, 0])
        rmtreed = []
        monkeypatch.setattr(pr_provisioner, "_run", scripted)
        monkeypatch.setattr(
            pr_provisioner.shutil, "rmtree", lambda p, **k: rmtreed.append(str(p))
        )
        await prov._clone(self._pr(), directory)

        argvs = scripted.calls
        assert argvs[0] == (
            "git",
            "clone",
            "--filter=blob:none",
            "--no-tags",
            "https://github.com/org/repo.git",
            str(directory),
        )
        # Full-clone retry taken after the partial-clone directory was wiped.
        assert str(directory) in rmtreed
        assert argvs[1] == (
            "git",
            "clone",
            "https://github.com/org/repo.git",
            str(directory),
        )
        assert argvs[2] == ("git", "fetch", "origin", "pull/9/head")
        assert argvs[3] == ("git", "checkout", "-B", "feat", _FAKE_SHA)
        assert argvs[4] == ("git", "reset", "--hard", _FAKE_SHA)

    async def test_clone_total_failure_raises(self, tmp_path, monkeypatch):
        from backend.ticket_ingestion import pr_provisioner

        prov = self._provisioner(tmp_path)
        scripted = _ScriptedRun([1, 1])  # both blobless and full clone fail
        monkeypatch.setattr(pr_provisioner, "_run", scripted)
        monkeypatch.setattr(pr_provisioner.shutil, "rmtree", lambda p, **k: None)
        with pytest.raises(pr_provisioner.PRProvisioningError, match="git clone"):
            await prov._clone(self._pr(), tmp_path / "pr-ws")
        # Never reached checkout: only the two clone attempts ran.
        assert len(scripted.calls) == 2

    async def test_fetch_failure_raises(self, tmp_path, monkeypatch):
        from backend.ticket_ingestion import pr_provisioner

        prov = self._provisioner(tmp_path)
        scripted = _ScriptedRun([1])  # git fetch fails immediately
        monkeypatch.setattr(pr_provisioner, "_run", scripted)
        with pytest.raises(pr_provisioner.PRProvisioningError, match="git fetch"):
            await prov._checkout_pr_head(self._pr(), tmp_path / "pr-ws")

    async def test_checkout_head_sha_falls_back_to_fetch_head(
        self, tmp_path, monkeypatch
    ):
        from backend.ticket_ingestion import pr_provisioner

        prov = self._provisioner(tmp_path)
        directory = tmp_path / "pr-ws"
        # fetch ok; checkout head_sha fails; checkout FETCH_HEAD ok; reset ok.
        scripted = _ScriptedRun([0, 1, 0, 0])
        monkeypatch.setattr(pr_provisioner, "_run", scripted)
        await prov._checkout_pr_head(self._pr(), directory)

        argvs = scripted.calls
        assert argvs[0] == ("git", "fetch", "origin", "pull/9/head")
        assert argvs[1] == ("git", "checkout", "-B", "feat", _FAKE_SHA)
        assert argvs[2] == ("git", "checkout", "-B", "feat", "FETCH_HEAD")
        # reset re-targets FETCH_HEAD once the recorded SHA is unreachable.
        assert argvs[3] == ("git", "reset", "--hard", "FETCH_HEAD")

    async def test_both_checkouts_fail_raises(self, tmp_path, monkeypatch):
        from backend.ticket_ingestion import pr_provisioner

        prov = self._provisioner(tmp_path)
        # fetch ok; checkout head_sha fails; checkout FETCH_HEAD also fails.
        scripted = _ScriptedRun([0, 1, 1])
        monkeypatch.setattr(pr_provisioner, "_run", scripted)
        with pytest.raises(pr_provisioner.PRProvisioningError, match="git checkout"):
            await prov._checkout_pr_head(self._pr(), tmp_path / "pr-ws")

    async def test_reset_failure_raises(self, tmp_path, monkeypatch):
        from backend.ticket_ingestion import pr_provisioner

        prov = self._provisioner(tmp_path)
        # fetch ok; checkout head_sha ok; reset --hard fails.
        scripted = _ScriptedRun([0, 0, 1])
        monkeypatch.setattr(pr_provisioner, "_run", scripted)
        with pytest.raises(pr_provisioner.PRProvisioningError, match="git reset"):
            await prov._checkout_pr_head(self._pr(), tmp_path / "pr-ws")

    async def test_blobless_clone_success_no_fallback(self, tmp_path, monkeypatch):
        from backend.ticket_ingestion import pr_provisioner

        prov = self._provisioner(tmp_path)
        directory = tmp_path / "pr-ws"
        # blobless clone ok; fetch ok; checkout ok; reset ok — no full-clone retry.
        scripted = _ScriptedRun([0, 0, 0, 0])
        rmtreed = []
        monkeypatch.setattr(pr_provisioner, "_run", scripted)
        monkeypatch.setattr(
            pr_provisioner.shutil, "rmtree", lambda p, **k: rmtreed.append(str(p))
        )
        await prov._clone(self._pr(), directory)
        # Only 4 calls: no directory wipe / full-clone retry happened.
        assert len(scripted.calls) == 4
        assert rmtreed == []


# --------------------------------------------------------------------------- #
# pr_runner pure prompt builders + small helpers
# --------------------------------------------------------------------------- #
def _pr_comment(cid, kind="review", author="rev", body="fix this", path="a.py", line=3):
    return PRComment(
        id=cid,
        kind=kind,
        author=author,
        body=body,
        url=f"http://c/{cid}",
        path=path,
        line=line,
        diff_hunk="@@ -1 +1 @@",
    )


def _pr_obj():
    return PullRequest(
        number=7,
        head_ref="feature",
        head_sha=_FAKE_SHA,
        base_ref="main",
        title="My PR",
        url="http://pr/7",
        author="prauthor",
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        repo="org/repo",
    )


class TestBuildConsolidatedPRPrompt:
    def test_includes_all_comments_and_header(self):
        from backend.ticket_ingestion.pr_runner import build_consolidated_pr_prompt

        comments = [_pr_comment(1), _pr_comment(2, author="bob")]
        out = build_consolidated_pr_prompt(
            comments=comments, pr=_pr_obj(), workspace_dir="/ws"
        )
        assert "# PR #7: My PR" in out
        assert "## Review comments to address (2)" in out
        assert "### 1. review comment by rev" in out
        assert "### 2. review comment by bob" in out
        assert "`a.py:3`" in out
        assert "/ws" in out

    def test_strips_media_from_body_and_hunk(self):
        from backend.ticket_ingestion.pr_runner import build_consolidated_pr_prompt

        c = _pr_comment(1, body="see ![x](http://y/z.png) here")
        out = build_consolidated_pr_prompt(
            comments=[c], pr=_pr_obj(), workspace_dir="/ws"
        )
        assert "[image omitted]" in out
        assert "z.png)" not in out

    def test_empty_body_renders_placeholder(self):
        from backend.ticket_ingestion.pr_runner import build_consolidated_pr_prompt

        c = _pr_comment(1, body="", path=None, line=None)
        out = build_consolidated_pr_prompt(
            comments=[c], pr=_pr_obj(), workspace_dir="/ws"
        )
        assert "_(empty)_" in out


class TestBuildPrompt:
    def test_includes_flock_lock_and_media_stripped(self, tmp_path):
        from backend.ticket_ingestion.pr_runner import _build_prompt
        from backend.ticket_ingestion.models import ProvisionedPRWorkspace

        ws = ProvisionedPRWorkspace(
            directory=tmp_path / "ws", head_ref="feature", head_sha=_FAKE_SHA
        )
        lock_path = tmp_path / "the lock"  # a space -> exercises shlex.quote
        c = _pr_comment(1, body="do <img src='x.png'> it")
        out = _build_prompt(_pr_obj(), ws, c, lock_path)
        assert "# PR #7: My PR" in out
        assert "## Comment to address (review)" in out
        assert "[image omitted]" in out
        # The lock path is shell-quoted into the flock instruction (whole path
        # is wrapped because it contains a space).
        import shlex

        assert f"flock {shlex.quote(str(lock_path))} --" in out

    def test_empty_body_placeholder(self, tmp_path):
        from backend.ticket_ingestion.pr_runner import _build_prompt
        from backend.ticket_ingestion.models import ProvisionedPRWorkspace

        ws = ProvisionedPRWorkspace(
            directory=tmp_path / "ws", head_ref="feature", head_sha=_FAKE_SHA
        )
        c = _pr_comment(1, body="", path=None, line=None)
        # No diff hunk section header when there is none.
        c.diff_hunk = None
        out = _build_prompt(_pr_obj(), ws, c, tmp_path / "lock")
        assert "_(empty)_" in out
        assert "### Diff context" not in out


class TestSessionNameAndTmpFile:
    def test_session_name_format(self):
        from backend.ticket_ingestion.pr_runner import _session_name

        assert _session_name(7, 42) == "pr-7-c-42"

    def test_pr_tmp_file_created_in_prompt_dir(self, tmp_path, monkeypatch):
        from backend.ticket_ingestion import pr_runner

        monkeypatch.setenv("MINDFLOCK_ASSISTANT_DIR", str(tmp_path))
        path = pr_runner._pr_tmp_file(7, 42, "prompt", ".md")
        try:
            assert path.exists()
            assert path.suffix == ".md"
            assert "pr_prompt_7_c_42_" in path.name
            # Lives under the prunable prompts dir.
            assert path.parent == tmp_path / "prompts"
        finally:
            path.unlink(missing_ok=True)


class _FakeProc:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self):
        return self._stdout, self._stderr

    async def wait(self):
        return self.returncode


class TestCursorWindowAlive:
    async def test_no_xdotool_assumed_alive(self, monkeypatch):
        from backend.ticket_ingestion import pr_runner

        monkeypatch.setattr(pr_runner.shutil, "which", lambda name: None)
        assert await pr_runner._cursor_window_alive("search") is True

    async def test_xdotool_match_alive(self, monkeypatch):
        from backend.ticket_ingestion import pr_runner

        monkeypatch.setattr(pr_runner.shutil, "which", lambda name: "/bin/xdotool")
        with patch(
            "asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=_FakeProc(returncode=0, stdout=b"12345\n"),
        ):
            assert await pr_runner._cursor_window_alive("search") is True

    async def test_xdotool_no_match_dead(self, monkeypatch):
        from backend.ticket_ingestion import pr_runner

        monkeypatch.setattr(pr_runner.shutil, "which", lambda name: "/bin/xdotool")
        with patch(
            "asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=_FakeProc(returncode=1, stdout=b""),
        ):
            assert await pr_runner._cursor_window_alive("search") is False


class TestOpenTerminalWindowsLinux:
    async def test_no_sessions_noop(self):
        from backend.ticket_ingestion import pr_runner

        # Should return immediately without touching osenv / subprocess.
        await pr_runner._open_terminal_windows(_pr_obj(), [])

    async def test_linux_no_emulator_logs(self, monkeypatch, caplog):
        from backend.ticket_ingestion import pr_runner

        monkeypatch.setattr(pr_runner.osenv, "is_wsl", lambda: False)
        # No emulator resolvable -> argv None -> nothing opened.
        monkeypatch.setattr(pr_runner, "build_terminal_tab_argv", lambda *a: None)
        sessions = [(_pr_comment(1), "pr-7-c-1")]
        with caplog.at_level("INFO"):
            await pr_runner._open_terminal_windows(_pr_obj(), sessions)
        assert any("no terminal emulator" in r.getMessage() for r in caplog.records)

    async def test_linux_opens_one_per_session(self, monkeypatch):
        from backend.ticket_ingestion import pr_runner

        monkeypatch.setattr(pr_runner.osenv, "is_wsl", lambda: False)
        monkeypatch.setattr(
            pr_runner,
            "build_terminal_tab_argv",
            lambda title, session: ["term", session],
        )
        spawned = []

        async def fake_spawn(*argv, **kw):
            spawned.append(argv)
            return _FakeProc()

        sessions = [(_pr_comment(1), "pr-7-c-1"), (_pr_comment(2), "pr-7-c-2")]
        with patch("asyncio.create_subprocess_exec", side_effect=fake_spawn):
            await pr_runner._open_terminal_windows(_pr_obj(), sessions)
        assert len(spawned) == 2


class TestOpenTerminalWindowsWSL:
    async def test_wsl_delegates(self, monkeypatch):
        from backend.ticket_ingestion import pr_runner

        monkeypatch.setattr(pr_runner.osenv, "is_wsl", lambda: True)
        called = {}

        async def fake_wsl(pr, sessions):
            called["pr"] = pr.number
            called["n"] = len(sessions)

        monkeypatch.setattr(pr_runner, "_open_wsl_terminal_window", fake_wsl)
        sessions = [(_pr_comment(1), "pr-7-c-1")]
        await pr_runner._open_terminal_windows(_pr_obj(), sessions)
        assert called == {"pr": 7, "n": 1}

    async def test_wsl_not_launchable_logs(self, monkeypatch, caplog):
        from backend.ticket_ingestion import pr_runner

        monkeypatch.setattr(pr_runner, "wt_command", lambda: "wt.exe")
        monkeypatch.setattr(pr_runner.shutil, "which", lambda name: None)
        sessions = [(_pr_comment(1), "pr-7-c-1")]
        with caplog.at_level("INFO"):
            await pr_runner._open_wsl_terminal_window(_pr_obj(), sessions)
        assert any("not launchable" in r.getMessage() for r in caplog.records)

    async def test_wsl_builds_one_window_per_tab(self, monkeypatch):
        from backend.ticket_ingestion import pr_runner

        monkeypatch.setattr(pr_runner, "wt_command", lambda: "wt.exe")
        monkeypatch.setattr(pr_runner.shutil, "which", lambda name: "/x/wt.exe")
        monkeypatch.setattr(pr_runner, "wsl_interop_available", lambda: True)
        monkeypatch.setattr(pr_runner, "wsl_distro", lambda: "Ubuntu")
        captured = {}

        async def fake_spawn(*argv, **kw):
            captured["argv"] = argv
            return _FakeProc(returncode=0)

        sessions = [(_pr_comment(1), "pr-7-c-1"), (_pr_comment(2), "pr-7-c-2")]
        with patch("asyncio.create_subprocess_exec", side_effect=fake_spawn):
            await pr_runner._open_wsl_terminal_window(_pr_obj(), sessions)
        argv = captured["argv"]
        # Single window, one extra tab for the 2nd session; both attach to tmux.
        assert argv[:3] == ("wt.exe", "-w", "new")
        assert argv.count("new-tab") == 1
        assert "pr-7-c-1" in argv and "pr-7-c-2" in argv

    async def test_wsl_spawn_oserror_logged(self, monkeypatch, caplog):
        from backend.ticket_ingestion import pr_runner

        monkeypatch.setattr(pr_runner, "wt_command", lambda: "wt.exe")
        monkeypatch.setattr(pr_runner.shutil, "which", lambda name: "/x/wt.exe")
        monkeypatch.setattr(pr_runner, "wsl_interop_available", lambda: True)
        monkeypatch.setattr(pr_runner, "wsl_distro", lambda: "Ubuntu")
        sessions = [(_pr_comment(1), "pr-7-c-1")]
        with patch("asyncio.create_subprocess_exec", side_effect=OSError("no exec")):
            with caplog.at_level("WARNING"):
                await pr_runner._open_wsl_terminal_window(_pr_obj(), sessions)
        assert any("could not launch" in r.getMessage() for r in caplog.records)

    async def test_wsl_nonzero_rc_logged(self, monkeypatch, caplog):
        from backend.ticket_ingestion import pr_runner

        monkeypatch.setattr(pr_runner, "wt_command", lambda: "wt.exe")
        monkeypatch.setattr(pr_runner.shutil, "which", lambda name: "/x/wt.exe")
        monkeypatch.setattr(pr_runner, "wsl_interop_available", lambda: True)
        monkeypatch.setattr(pr_runner, "wsl_distro", lambda: "Ubuntu")
        sessions = [(_pr_comment(1), "pr-7-c-1")]
        with patch(
            "asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=_FakeProc(returncode=1, stderr=b"wt boom"),
        ):
            with caplog.at_level("WARNING"):
                await pr_runner._open_wsl_terminal_window(_pr_obj(), sessions)
        assert any("failed to open window" in r.getMessage() for r in caplog.records)


class TestKillAndStartSession:
    async def test_kill_session_issues_tmux_kill(self):
        from backend.ticket_ingestion import pr_runner

        with patch(
            "asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=_FakeProc(returncode=0),
        ) as spawn:
            await pr_runner._kill_session("pr-7-c-1")
        args = spawn.await_args.args
        assert args[:3] == ("tmux", "kill-session", "-t")
        assert args[3] == "pr-7-c-1"

    async def test_start_tmux_session_success_sets_options(self, tmp_path, monkeypatch):
        from backend.ticket_ingestion import pr_runner

        monkeypatch.setattr(pr_runner.shutil, "which", lambda name: "/bin/bash")
        calls = []

        async def fake_spawn(*argv, **kw):
            calls.append(argv)
            return _FakeProc(returncode=0)

        with patch("asyncio.create_subprocess_exec", side_effect=fake_spawn):
            await pr_runner._start_tmux_session(
                "pr-7-c-1",
                tmp_path,
                tmp_path / "prompt.md",
                tmp_path / "done.done",
            )
        verbs = [a[1] for a in calls]
        # new-session, then the three set-option calls, then the set-hook.
        assert verbs[0] == "new-session"
        assert verbs.count("set-option") == 3
        assert "set-hook" in verbs


class TestWaitForDoneMarkersLoop:
    async def test_completes_when_markers_appear(self, tmp_path, monkeypatch):
        from backend.ticket_ingestion import pr_runner

        marker = tmp_path / "done.done"
        marker.touch()  # already "done" -> the loop drains and returns

        async def no_sleep(_):
            return None

        monkeypatch.setattr(pr_runner.asyncio, "sleep", no_sleep)
        with patch.object(
            pr_runner, "_cursor_window_alive", new_callable=AsyncMock, return_value=True
        ):
            await pr_runner._wait_for_done_markers([marker], "ws", sessions=["s"])
        # Cleaned up afterward.
        assert not marker.exists()

    async def test_cursor_close_ends_wait(self, tmp_path, monkeypatch):
        from backend.ticket_ingestion import pr_runner

        marker = tmp_path / "never.done"  # never created

        async def no_sleep(_):
            return None

        monkeypatch.setattr(pr_runner.asyncio, "sleep", no_sleep)
        # Alive on the first poll, gone on the second -> "window closed" break.
        alive_states = iter([True, False])

        async def fake_alive(_):
            return next(alive_states)

        with patch.object(pr_runner, "_cursor_window_alive", side_effect=fake_alive):
            await pr_runner._wait_for_done_markers([marker], "ws", sessions=["s"])


class TestPRClaudeRunnerLaunch:
    async def test_no_comments_returns_early(self, caplog):
        from backend.ticket_ingestion.pr_runner import PRClaudeRunner
        from backend.ticket_ingestion.models import ProvisionedPRWorkspace

        ws = ProvisionedPRWorkspace(
            directory="/nonexistent", head_ref="feature", head_sha=_FAKE_SHA
        )
        with caplog.at_level("INFO"):
            # No comments -> no lock file touched, no tmux, returns immediately.
            await PRClaudeRunner().launch(_pr_obj(), ws, [])
        assert any("nothing to launch" in r.getMessage() for r in caplog.records)


class TestStartTmuxSessionFailure:
    async def test_new_session_failure_raises(self, tmp_path, monkeypatch):
        from backend.ticket_ingestion import pr_runner

        monkeypatch.setattr(pr_runner.shutil, "which", lambda name: "/bin/bash")
        with patch(
            "asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=_FakeProc(returncode=1, stderr=b"no server"),
        ):
            with pytest.raises(RuntimeError, match="tmux new-session"):
                await pr_runner._start_tmux_session(
                    "pr-7-c-1",
                    tmp_path,
                    tmp_path / "prompt.md",
                    tmp_path / "done.done",
                )


# --------------------------------------------------------------------------- #
# pr_provisioner.PRProvisioner.provision orchestration + _launch_cursor
# --------------------------------------------------------------------------- #
class TestPRProvisionerProvision:
    def _pr(self):
        return PullRequest(
            number=9,
            head_ref="feat",
            head_sha=_FAKE_SHA,
            base_ref="main",
            title="t",
            url="u",
            author="a",
            created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            repo="org/repo",
            clone_url="https://github.com/org/repo.git",
        )

    def _prov(self, tmp_path):
        from backend.ticket_ingestion.config import PipelineConfig
        from backend.ticket_ingestion.pr_provisioner import PRProvisioner

        cfg = PipelineConfig(
            repo_url="https://github.com/org/repo.git",
            workspace_dir=tmp_path / "ws",
        )
        return PRProvisioner(cfg)

    def _stub_workspace_setup(self, monkeypatch):
        from backend.ticket_ingestion import pr_provisioner

        async def fake_setup(*a, **k):
            return None

        monkeypatch.setattr(pr_provisioner, "run_setup_commands_async", fake_setup)
        monkeypatch.setattr(pr_provisioner, "resolve_setup_commands", lambda c, d: [])
        monkeypatch.setattr(pr_provisioner, "pin_cache_env", lambda d, c: None)
        monkeypatch.setattr(pr_provisioner, "seed_caches", lambda c, d, **k: None)

    async def test_fresh_clone_flow(self, tmp_path, monkeypatch):
        prov = self._prov(tmp_path)
        self._stub_workspace_setup(monkeypatch)
        cloned, refreshed = [], []

        async def fake_clone(pr, directory):
            cloned.append(directory)
            directory.mkdir(parents=True, exist_ok=True)

        async def fake_refresh(pr, directory):
            refreshed.append(directory)

        monkeypatch.setattr(prov, "_clone", fake_clone)
        monkeypatch.setattr(prov, "_refresh", fake_refresh)
        ws = await prov.provision(self._pr(), launch_cursor=False)
        assert ws.head_ref == "feat" and ws.head_sha == _FAKE_SHA
        assert ws.directory.name == "pr-repo-9"
        assert cloned and not refreshed  # fresh clone (no existing checkout)

    async def test_existing_checkout_refreshes(self, tmp_path, monkeypatch):
        prov = self._prov(tmp_path)
        self._stub_workspace_setup(monkeypatch)
        # Pre-create the workspace as an existing git checkout.
        directory = (tmp_path / "ws" / "pr-repo-9").resolve()
        (directory / ".git").mkdir(parents=True)
        # A stale lock left by a prior run must be removed during provision.
        (directory / ".pr-edit-lock").touch()
        refreshed = []

        async def fake_refresh(pr, d):
            refreshed.append(d)

        async def fake_clone(pr, d):
            raise AssertionError("should refresh, not clone")

        monkeypatch.setattr(prov, "_refresh", fake_refresh)
        monkeypatch.setattr(prov, "_clone", fake_clone)
        await prov.provision(self._pr(), launch_cursor=False)
        assert refreshed  # refresh path taken
        assert not (directory / ".pr-edit-lock").exists()  # stale lock cleared

    async def test_refresh_failure_falls_back_to_reclone(self, tmp_path, monkeypatch):
        from backend.ticket_ingestion.pr_provisioner import PRProvisioningError

        prov = self._prov(tmp_path)
        self._stub_workspace_setup(monkeypatch)
        directory = (tmp_path / "ws" / "pr-repo-9").resolve()
        (directory / ".git").mkdir(parents=True)
        cloned = []

        async def failing_refresh(pr, d):
            raise PRProvisioningError("corrupt checkout")

        async def fake_clone(pr, d):
            cloned.append(d)
            d.mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(prov, "_refresh", failing_refresh)
        monkeypatch.setattr(prov, "_clone", fake_clone)
        await prov.provision(self._pr(), launch_cursor=False)
        assert cloned  # fell back to a fresh clone after refresh failed

    async def test_launch_cursor_best_effort_swallows_errors(
        self, tmp_path, monkeypatch
    ):
        from backend.web.core import ide_launch

        prov = self._prov(tmp_path)
        # launch_ide is fire-and-forget; a failure must NOT propagate.
        monkeypatch.setattr(
            ide_launch,
            "launch_ide",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no display")),
        )
        # Should not raise despite launch_ide throwing.
        await prov._launch_cursor(self._pr(), tmp_path)
