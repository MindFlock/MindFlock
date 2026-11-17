"""PR intake reads every request on a PR (docs/pr-review-rounds.md "What
counts as a request"): review threads, "changes requested" reviews, AND the
PR's conversation comments and "commented" / "approved" review bodies — with
pure noise dropped (auto-generated summaries, status/approval-only notes,
anything the PR author already answered)."""

import asyncio
from datetime import datetime, timezone

import pytest

from backend.ticket_ingestion import pr_comments as PC
from backend.ticket_ingestion.config import GithubConfig
from backend.ticket_ingestion.models import PullRequest

T0 = 1_800_000_000.0


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


def _pr():
    return PullRequest(
        number=7,
        head_ref="feature/x",
        head_sha="abc",
        base_ref="main",
        title="Add parser",
        url="https://github.com/o/r/pull/7",
        author="me",
        created_at=datetime.fromtimestamp(T0 - 9000, timezone.utc),
        repo="o/r",
    )


def _gh(skip=()):
    return GithubConfig(
        base_branch="main",
        min_age_minutes=0,
        poll_interval_seconds=60,
        enabled=True,
        skip_authors=list(skip),
    )


def _issue(i, who, body, t):
    return {
        "id": i,
        "user": {"login": who},
        "body": body,
        "html_url": "https://github.com/o/r/pull/7#issuecomment-%d" % i,
        "created_at": iso(t),
    }


def _review(i, who, state, body, t):
    return {
        "id": i,
        "user": {"login": who},
        "state": state,
        "body": body,
        "html_url": "https://github.com/o/r/pull/7#pullrequestreview-%d" % i,
        "submitted_at": iso(t),
    }


SUMMARY = (
    "<!-- This is an auto-generated comment: summarize by a review bot -->\n"
    "## Walkthrough\nThe parser now handles nested input and should be faster.\n"
)
FINDINGS = (
    "<!-- This is an auto-generated comment: review summary -->\n"
    "**Actionable comments posted: 0**\n"
    "> [!CAUTION]\n> Some comments are outside the diff range and can't be "
    "posted inline.\n`app.py` 10-12: guard the empty case."
)


# --------------------------------------------------------------------------- #
# Noise
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "body",
    [
        "LGTM",
        "Thanks!",
        "\U0001f44d",
        ":shipit:",
        "Approved",
        "✅ All checks passed",
        "Deploy preview ready! https://preview.example.com/abc",
        SUMMARY,
    ],
)
def test_noise_is_dropped(body):
    assert PC.is_noise(body)


@pytest.mark.parametrize(
    "body",
    [
        "Please rename `parse` to `parse_all`.",
        "Looks good, but why not reuse the helper?",
        "nit: typo in the docstring",
        "This approach looks wrong to me",
        "LGTM once the failing test is fixed",
        FINDINGS,  # a summary that carries findings outside the diff range
        "A long comment " + "with lots of words " * 30,
    ],
)
def test_requests_are_kept(body):
    assert not PC.is_noise(body)


def test_answered_needs_a_newer_reply_and_a_known_time():
    assert PC.answered(iso(T0), [T0 + 1])
    assert not PC.answered(iso(T0), [T0 - 1, T0])
    assert not PC.answered(None, [T0 + 99])  # unknown time: hand it over


# --------------------------------------------------------------------------- #
# fetch_pr_requests: all four kinds, the rules kept
# --------------------------------------------------------------------------- #
def _fetch(monkeypatch, *, inline=(), reviews=(), issue=(), cfg=None):
    async def _inline(pr, config, *, drop_noise=False):
        assert drop_noise is True  # intake always drops noise in threads
        return list(inline)

    async def _reviews(pr, config):
        return list(reviews)

    async def _issue(pr, config):
        return list(issue)

    monkeypatch.setattr(PC, "fetch_actionable_comments", _inline)
    monkeypatch.setattr(PC, "_fetch_reviews_raw", _reviews)
    monkeypatch.setattr(PC, "_fetch_issue_comments_raw", _issue)
    return asyncio.run(PC.fetch_pr_requests(_pr(), cfg or _gh()))


def test_conversation_comments_and_review_bodies_are_fetched(monkeypatch):
    out = _fetch(
        monkeypatch,
        reviews=[
            _review(1, "rev", "CHANGES_REQUESTED", "", T0),
            _review(2, "bot", "COMMENTED", FINDINGS, T0 + 1),
            _review(
                3, "rev", "APPROVED", "Approved, but please drop the debug log", T0 + 2
            ),
            _review(4, "rev", "COMMENTED", "", T0 + 3),  # empty body: nothing to say
            _review(5, "rev", "APPROVED", "LGTM", T0 + 4),  # approval only
            _review(6, "rev", "DISMISSED", "old ask", T0 + 5),
        ],
        issue=[
            _issue(10, "rev", "Can you add a test for the empty case?", T0 + 6),
            _issue(11, "bot", SUMMARY, T0 + 7),  # auto-generated summary
            _issue(12, "ci", "✅ Build succeeded", T0 + 8),  # status only
        ],
    )
    got = [(c.kind, c.id, c.review_state) for c in out]
    assert got == [
        ("review", 1, "CHANGES_REQUESTED"),
        ("review", 2, "COMMENTED"),
        ("review", 3, "APPROVED"),
        ("issue", 10, None),
    ]
    assert out[0].body.startswith("(requested changes")
    assert out[3].url.endswith("#issuecomment-10")


def test_the_pr_author_and_skip_authors_are_dropped(monkeypatch):
    out = _fetch(
        monkeypatch,
        reviews=[_review(1, "me", "COMMENTED", "please fix", T0)],
        issue=[
            _issue(10, "me", "please fix", T0),
            _issue(11, "bot", "please fix", T0),
            _issue(12, "rev", "please fix", T0),
        ],
        cfg=_gh(skip=["bot"]),
    )
    assert [c.id for c in out] == [12]


def test_what_the_pr_author_answered_since_is_dropped(monkeypatch):
    out = _fetch(
        monkeypatch,
        reviews=[
            _review(1, "rev", "COMMENTED", "please fix the loop", T0),
            # "changes requested" stays until the reviewer says otherwise
            _review(2, "rev", "CHANGES_REQUESTED", "rename it", T0),
            _review(3, "rev", "COMMENTED", "and the docs too, please", T0 + 20),
        ],
        issue=[
            _issue(10, "rev", "why not a dict?", T0 + 1),
            _issue(11, "me", "Answered both above.", T0 + 10),
            _issue(12, "rev", "one more: add a test?", T0 + 30),
        ],
    )
    assert sorted((c.kind, c.id) for c in out) == [
        ("issue", 12),
        ("review", 2),
        ("review", 3),
    ]


def test_a_failed_part_is_a_fetch_error_not_none(monkeypatch):
    async def _boom(pr, config):
        raise PC.PRCommentsFetchError("502")

    monkeypatch.setattr(PC, "_fetch_issue_comments_raw", _boom)

    async def _ok(*a, **k):
        return []

    monkeypatch.setattr(PC, "fetch_actionable_comments", _ok)
    monkeypatch.setattr(PC, "_fetch_reviews_raw", _ok)
    with pytest.raises(PC.PRCommentsFetchError):
        asyncio.run(PC.fetch_pr_requests(_pr(), _gh()))


def test_a_non_200_conversation_fetch_raises(monkeypatch):
    class _Resp:
        status = 502

        async def text(self):
            return "bad gateway"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Session:
        def __init__(self, *a, **k):
            pass

        def get(self, *a, **k):
            return _Resp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    async def _token(config):
        return "t"

    monkeypatch.setattr(PC, "resolve_token", _token)
    monkeypatch.setattr(PC.aiohttp, "ClientSession", _Session)
    with pytest.raises(PC.PRCommentsFetchError):
        asyncio.run(PC._fetch_issue_comments_raw(_pr(), _gh()))


# --------------------------------------------------------------------------- #
# Review threads: the thread's first comment, answered + noise dropped
# --------------------------------------------------------------------------- #
def _node(i, who, body, t):
    return {
        "databaseId": i,
        "author": {"login": who},
        "body": body,
        "path": "app.py",
        "line": 3,
        "diffHunk": "@@",
        "createdAt": iso(t),
        "url": "https://github.com/o/r/pull/7#discussion_r%d" % i,
    }


class _GQLSession:
    def __init__(self, threads):
        self.threads = threads

    def post(self, *a, **k):
        data = {
            "data": {
                "repository": {
                    "pullRequest": {"reviewThreads": {"nodes": self.threads}}
                }
            }
        }

        class _R:
            status = 200

            async def json(self_inner):
                return data

            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *a):
                return False

        return _R()


def _threads():
    return [
        {
            "isResolved": False,
            "isOutdated": False,
            "comments": {
                "nodes": [
                    _node(1, "rev", "please guard None", T0),
                    _node(2, "me", "Done in abc1234.", T0 + 10),
                    _node(3, "rev", "also the empty list, please", T0 + 20),
                ]
            },
        },
        {
            "isResolved": False,
            "isOutdated": False,
            "comments": {"nodes": [_node(5, "rev", "LGTM \U0001f44d", T0)]},
        },
    ]


def test_threads_keep_old_behaviour_without_drop_noise():
    out = asyncio.run(
        PC._fetch_review_comments(_GQLSession(_threads()), "t", "o", "r", 7, {"me"})
    )
    assert [c.id for c in out] == [1, 3, 5]
    assert [c.reply_to for c in out] == [1, 1, 5]


def test_intake_drops_answered_and_noise_in_threads():
    out = asyncio.run(
        PC._fetch_review_comments(
            _GQLSession(_threads()),
            "t",
            "o",
            "r",
            7,
            {"me"},
            author="me",
            drop_noise=True,
        )
    )
    assert [c.id for c in out] == [3]  # 1 answered by the author, 5 is LGTM
    assert out[0].reply_to == 1  # a reply goes to the thread's first comment
