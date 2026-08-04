"""Fetch actionable PR comments via the GitHub GraphQL API.

Review comments come from GraphQL so we can drop resolved/outdated threads.
Comments authored by the PR author or any user in `skip_authors` are dropped.
"""

import asyncio
import logging
import re

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


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def fetch_actionable_comments(
    pr: PullRequest, config: GithubConfig
) -> list[PRComment]:
    owner, name = pr.repo.split("/", 1)
    # Per-repo skip list: a bot that reviews one repo often doesn't touch the
    # next, and each watched repo edits its own list on its own card.
    skip = {pr.author, *config.skip_authors_for(pr.repo)}
    token = await resolve_token(config)
    try:
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            review = await _fetch_review_comments(
                session, token, owner, name, pr.number, skip
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
        for c in (thread.get("comments") or {}).get("nodes", []):
            if not isinstance(c, dict):
                continue
            author = (c.get("author") or {}).get("login") or ""
            if author in skip:
                continue
            db_id = c.get("databaseId")
            if not isinstance(db_id, int):
                continue
            out.append(
                PRComment(
                    id=db_id,
                    kind="review",
                    author=author,
                    body=_extract_ai_prompt(
                        str(c.get("body", "")), author, str(c.get("url", ""))
                    ),
                    url=str(c.get("url", "")),
                    path=c.get("path"),
                    line=c.get("line") if isinstance(c.get("line"), int) else None,
                    diff_hunk=c.get("diffHunk"),
                )
            )
    return out
