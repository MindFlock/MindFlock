"""Fetch actionable PR comments via the GitHub GraphQL API.

Review comments come from GraphQL so we can drop resolved/outdated threads.
Comments authored by the PR author or any user in `skip_authors` are dropped.

PR intake (docs/pr-review-rounds.md) reads more than the review threads:
:func:`fetch_pr_requests` adds the PR's conversation comments and every review
whose body says something, then drops pure noise (auto-generated summaries,
status/approval-only notes, anything the PR author already answered).
"""

import asyncio
import logging
import re
from datetime import datetime

import aiohttp

from backend.ticket_ingestion.config import GithubConfig
from backend.ticket_ingestion.github_auth import resolve_token
from backend.ticket_ingestion.models import PRComment, PullRequest

_logger = logging.getLogger(__name__)
# Total wall-clock budget for any single HTTP request/response.
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=30)
_GITHUB_API = "https://api.github.com"


class PRCommentsFetchError(RuntimeError):
    """The review-comment fetch failed (HTTP/GraphQL/network error).

    Distinct from a genuinely-empty comment list: callers must NOT record the
    PR as processed on this error, so the next poll retries — a transient 429/
    5xx used to read as "no comments" and permanently skip real feedback."""


_REVIEW_THREADS_QUERY = """
query($owner:String!, $name:String!, $number:Int!) {
  repository(owner:$owner, name:$name) {
    pullRequest(number:$number) {
      reviewThreads(first:100) {
        nodes {
          isResolved
          isOutdated
          comments(first:50) {
            nodes {
              databaseId
              author { login }
              body
              path
              line
              diffHunk
              createdAt
              url
            }
          }
        }
      }
    }
  }
}
"""


_AI_PROMPT_RE = re.compile(
    r"<details>\s*<summary>[^<]*Prompt for AI Agents[^<]*</summary>"
    r"\s*(?P<body>.*?)\s*</details>",
    re.IGNORECASE | re.DOTALL,
)


def _extract_ai_prompt(body: str, *haystacks: str) -> str:
    """If `coderabbitai` appears anywhere in `body` or any of `haystacks` and
    `body` contains a `Prompt for AI Agents` <details> block, return just the
    inner content of that block; otherwise return body unchanged."""
    combined = " ".join((body, *haystacks)).lower()
    if "coderabbitai" not in combined:
        return body
    match = _AI_PROMPT_RE.search(body)
    if not match:
        return body
    inner = match.group("body").strip()
    inner = inner.strip("`").strip()
    return inner or body


# --------------------------------------------------------------------------- #
# Noise: what a reviewer said that asks for nothing
# --------------------------------------------------------------------------- #
#: A bot's own HTML-comment marker (``<!-- … auto-generated … -->``).
_AUTOGEN_MARKER_RE = re.compile(r"<!--(?P<text>.*?)-->", re.DOTALL)
_AUTOGEN_RE = re.compile(r"auto[- ]?generated", re.IGNORECASE)
#: …saying what it generated is a summary, not findings.
_SUMMARY_RE = re.compile(
    r"summar|walkthrough|release[ _-]?notes|overview|status", re.IGNORECASE
)
#: Findings a summary can still carry: those keep it.
_FINDINGS_RE = re.compile(
    r"outside (?:of )?(?:the )?diff(?:'s)? range|```suggestion", re.IGNORECASE
)
#: Words that ask for something. Generous on purpose: keeping a comment costs
#: one reply, dropping a real request loses it.
_REQUEST_RE = re.compile(
    r"\?|\b(?:please|pls|should(?:n'?t)?|must|need(?:s|ed)? to|have to|"
    r"consider|suggest\w*|could you|can you|would you|why|instead|rename|"
    r"remove|add|fix|change|update|missing|wrong|bug|broken|fail(?:s|ed|ing)?|"
    r"error|nit|todo|typo|revert|don'?t|do not)\b",
    re.IGNORECASE,
)
#: Status / approval vocabulary.
_STATUS_RE = re.compile(
    r"\b(?:lgtm|looks? good|approved?|approving|ship ?it|thanks?|thank you|thx|"
    r"great|nice|awesome|well done|good (?:job|work)|pass(?:ed|ing)?|"
    r"succeeded|successful(?:ly)?|success|green|deployed|deployment|preview|"
    r"ready|merged?|rebased?|done|resolved|ack(?:nowledged)?)\b"
    r"|:\+1:|:shipit:|:tada:|:white_check_mark:|\U0001F44D|\u2705|\U0001F389"
    r"|\U0001F680|\u2714",
    re.IGNORECASE,
)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_MD_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_URL_RE = re.compile(r"https?://\S+")
#: A status note is short; anything longer is read as saying something.
_STATUS_MAX_CHARS = 300


def _plain(body: str) -> str:
    """``body`` without HTML comments, tags, link targets or bare URLs."""
    out = _HTML_COMMENT_RE.sub(" ", body or "")
    out = _TAG_RE.sub(" ", out)
    out = _MD_LINK_RE.sub(r"\1", out)
    out = _URL_RE.sub(" ", out)
    return " ".join(out.split())


def is_auto_summary(body: str) -> bool:
    """A bot's auto-generated summary / walkthrough / status comment that
    holds no request: its own ``<!-- … auto-generated … -->`` marker says it
    is a summary, and it carries no findings (an "outside the diff range"
    finding or a suggested change keeps it)."""
    marked = any(
        _AUTOGEN_RE.search(m.group("text")) and _SUMMARY_RE.search(m.group("text"))
        for m in _AUTOGEN_MARKER_RE.finditer(body or "")
    )
    return marked and not _FINDINGS_RE.search(body or "")


def is_status_only(body: str) -> bool:
    """A status or approval note with no request ("LGTM", "Thanks!", a
    build/deploy notice): short, in that vocabulary, and asking nothing."""
    text = _plain(body)
    if not text or len(text) > _STATUS_MAX_CHARS:
        return False
    return bool(_STATUS_RE.search(text)) and not _REQUEST_RE.search(text)


def is_noise(body: str) -> bool:
    """Either of the above: nothing in it to act on or answer."""
    return is_auto_summary(body) or is_status_only(body)


def _ts(iso) -> float:
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def answered(created_at, replies: list[float]) -> bool:
    """Whether the PR author replied after a comment said at ``created_at``
    (``replies``: epoch seconds of the author's replies). An unknown time is
    never answered — handing it over again is the safe side."""
    said = _ts(created_at)
    return bool(said) and any(r > said for r in replies)


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def fetch_actionable_comments(
    pr: PullRequest, config: GithubConfig, *, drop_noise: bool = False
) -> list[PRComment]:
    """Unresolved, non-outdated review-thread comments. ``drop_noise`` (PR
    intake) also drops status/approval-only ones and any the PR author
    already answered in the same thread (:func:`is_noise`, :func:`answered`).
    """
    owner, name = pr.repo.split("/", 1)
    # Per-repo skip list: a bot that reviews one repo often doesn't touch the
    # next, and each watched repo edits its own list on its own card.
    skip = {pr.author, *config.skip_authors_for(pr.repo)}
    token = await resolve_token(config)
    try:
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            review = await _fetch_review_comments(
                session,
                token,
                owner,
                name,
                pr.number,
                skip,
                author=pr.author if drop_noise else "",
                drop_noise=drop_noise,
            )
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        raise PRCommentsFetchError(
            f"review-comment fetch failed for PR #{pr.number}: {e}"
        ) from e
    return review


async def _fetch_review_comments(
    session: aiohttp.ClientSession,
    token: str,
    owner: str,
    name: str,
    number: int,
    skip: set[str],
    *,
    author: str = "",
    drop_noise: bool = False,
) -> list[PRComment]:
    payload = {
        "query": _REVIEW_THREADS_QUERY,
        "variables": {"owner": owner, "name": name, "number": number},
    }
    async with session.post(
        f"{_GITHUB_API}/graphql", json=payload, headers=_headers(token)
    ) as resp:
        if resp.status != 200:
            text = await resp.text()
            raise PRCommentsFetchError(
                f"GraphQL review-threads returned {resp.status} for "
                f"PR #{number}: {text[:200]}"
            )
        data = await resp.json()

    if data.get("errors"):
        raise PRCommentsFetchError(f"GraphQL errors for PR #{number}: {data['errors']}")

    threads = (
        (((data.get("data") or {}).get("repository") or {}).get("pullRequest") or {})
        .get("reviewThreads", {})
        .get("nodes", [])
    )

    out: list[PRComment] = []
    for thread in threads:
        if not isinstance(thread, dict):
            continue
        if thread.get("isResolved") or thread.get("isOutdated"):
            continue
        nodes = [
            c
            for c in (thread.get("comments") or {}).get("nodes", [])
            if isinstance(c, dict)
        ]
        root = next(
            (c["databaseId"] for c in nodes if isinstance(c.get("databaseId"), int)),
            None,
        )
        # The PR author's own replies in this thread: a comment older than one
        # of them was answered there.
        replies = [
            _ts(c.get("createdAt"))
            for c in nodes
            if author and ((c.get("author") or {}).get("login") or "") == author
        ]
        for c in nodes:
            login = (c.get("author") or {}).get("login") or ""
            if login in skip:
                continue
            db_id = c.get("databaseId")
            if not isinstance(db_id, int):
                continue
            if drop_noise and (
                is_noise(str(c.get("body") or ""))
                or answered(c.get("createdAt"), replies)
            ):
                continue
            out.append(
                PRComment(
                    id=db_id,
                    kind="review",
                    author=login,
                    body=_extract_ai_prompt(
                        str(c.get("body", "")), login, str(c.get("url", ""))
                    ),
                    url=str(c.get("url", "")),
                    path=c.get("path"),
                    line=c.get("line") if isinstance(c.get("line"), int) else None,
                    diff_hunk=c.get("diffHunk"),
                    created_at=str(c.get("createdAt") or "") or None,
                    reply_to=root,
                )
            )
    return out


async def _get_list(config: GithubConfig, url: str, what: str, number: int) -> list:
    """GET a REST list (first 100). Raises :class:`PRCommentsFetchError` on
    any failure — a failed fetch never reads as "none"."""
    token = await resolve_token(config)
    try:
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            async with session.get(
                url, params={"per_page": "100"}, headers=_headers(token)
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise PRCommentsFetchError(
                        f"{what} returned {resp.status} for PR #{number}: {text[:200]}"
                    )
                data = await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        raise PRCommentsFetchError(f"{what} fetch failed for PR #{number}: {e}") from e
    return data if isinstance(data, list) else []


async def _fetch_reviews_raw(pr: PullRequest, config: GithubConfig) -> list:
    """The PR's reviews (REST ``/pulls/{n}/reviews``), as GitHub sends them."""
    url = f"{_GITHUB_API}/repos/{pr.repo}/pulls/{pr.number}/reviews"
    return await _get_list(config, url, "review", pr.number)


async def _fetch_issue_comments_raw(pr: PullRequest, config: GithubConfig) -> list:
    """The PR's conversation comments (REST ``/issues/{n}/comments``)."""
    url = f"{_GITHUB_API}/repos/{pr.repo}/issues/{pr.number}/comments"
    return await _get_list(config, url, "conversation-comment", pr.number)


def _changes_requested(data: list, skip: set[str]) -> list[PRComment]:
    out: list[PRComment] = []
    for r in data:
        if not isinstance(r, dict) or r.get("state") != "CHANGES_REQUESTED":
            continue
        author = (r.get("user") or {}).get("login") or ""
        if author in skip or not isinstance(r.get("id"), int):
            continue
        out.append(
            PRComment(
                id=r["id"],
                kind="review",
                author=author,
                body=str(r.get("body") or "").strip()
                or "(requested changes — see the inline comments)",
                url=str(r.get("html_url") or ""),
                created_at=str(r.get("submitted_at") or "") or None,
                review_state="CHANGES_REQUESTED",
            )
        )
    return out


async def fetch_changes_requested(
    pr: PullRequest, config: GithubConfig
) -> list[PRComment]:
    """The PR's "changes requested" reviews (REST ``/pulls/{n}/reviews``), as
    comments (``kind="review"``, ``id`` = the review id, ``path`` None): a
    review round's top-level asks (docs/pr-review-rounds.md). The PR author's
    own and ``skip_authors``' are dropped, like inline comments. Raises
    :class:`PRCommentsFetchError` on a failed fetch (never reads as "none")."""
    skip = {pr.author, *config.skip_authors_for(pr.repo)}
    return _changes_requested(await _fetch_reviews_raw(pr, config), skip)


def _author_replies(issue_comments: list, author: str) -> list[float]:
    return [
        _ts(c.get("created_at"))
        for c in issue_comments
        if isinstance(c, dict)
        and author
        and ((c.get("user") or {}).get("login") or "") == author
    ]


def review_bodies(
    reviews: list, skip: set[str], replies: list[float]
) -> list[PRComment]:
    """ "Commented" / "approved" reviews whose body says something: a bot's
    findings outside the diff range live there. Noise and bodies the PR author
    answered later in the conversation are dropped."""
    out: list[PRComment] = []
    for r in reviews:
        if not isinstance(r, dict):
            continue
        state = str(r.get("state") or "")
        if state not in ("COMMENTED", "APPROVED"):
            continue
        author = (r.get("user") or {}).get("login") or ""
        body = str(r.get("body") or "").strip()
        if author in skip or not body or not isinstance(r.get("id"), int):
            continue
        said = str(r.get("submitted_at") or "") or None
        if is_noise(body) or answered(said, replies):
            continue
        out.append(
            PRComment(
                id=r["id"],
                kind="review",
                author=author,
                body=body,
                url=str(r.get("html_url") or ""),
                created_at=said,
                review_state=state,
            )
        )
    return out


def conversation_comments(
    comments: list, skip: set[str], replies: list[float]
) -> list[PRComment]:
    """The PR's conversation comments (``kind="issue"``), less noise and those
    the PR author answered with a newer conversation comment."""
    out: list[PRComment] = []
    for c in comments:
        if not isinstance(c, dict) or not isinstance(c.get("id"), int):
            continue
        author = (c.get("user") or {}).get("login") or ""
        body = str(c.get("body") or "").strip()
        if author in skip or not body:
            continue
        said = str(c.get("created_at") or "") or None
        if is_noise(body) or answered(said, replies):
            continue
        out.append(
            PRComment(
                id=c["id"],
                kind="issue",
                author=author,
                body=body,
                url=str(c.get("html_url") or ""),
                created_at=said,
            )
        )
    return out


async def fetch_pr_requests(pr: PullRequest, config: GithubConfig) -> list[PRComment]:
    """Everything on the PR that asks for something (docs/pr-review-rounds.md
    "What counts as a request"): "changes requested" reviews, "commented" /
    "approved" reviews with a body, conversation comments, and unresolved
    review-thread comments — the PR author's and ``skip_authors``' dropped,
    noise dropped, and anything the PR author already answered dropped
    ("changes requested" stays until the reviewer says otherwise). Raises
    :class:`PRCommentsFetchError` when any of the three fetches fails: a part
    missing never reads as "no comments"."""
    skip = {pr.author, *config.skip_authors_for(pr.repo)}
    inline, reviews, issue = await asyncio.gather(
        fetch_actionable_comments(pr, config, drop_noise=True),
        _fetch_reviews_raw(pr, config),
        _fetch_issue_comments_raw(pr, config),
    )
    replies = _author_replies(issue, pr.author)
    return (
        _changes_requested(reviews, skip)
        + review_bodies(reviews, skip, replies)
        + conversation_comments(issue, skip, replies)
        + list(inline)
    )
