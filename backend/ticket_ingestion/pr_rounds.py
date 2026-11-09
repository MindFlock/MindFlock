"""PR review ROUNDS (docs/pr-review-rounds.md).

PR intake used to handle each of your PRs ONCE: the first time it saw review
comments it started a session to address them, recorded the PR as processed,
and never looked again — so a reviewer's second pass ("changes requested", or
a fresh batch of comments) went unnoticed.

Now every one of your open PRs that intake already handled is watched for a new
ROUND: a "changes requested" review, or review comments, that came after it was
last handed to a session. A round waits until the reviewer has stopped for
:data:`SETTLE_S` (a series of comments is one round, not ten), then goes to the
PR's session — queued into it when it still exists, a new session otherwise —
with a TRIAGE prompt: for each request decide **do it** / **push back** (reply
with why) / **ask the owner**, write that down, and only then act. The repo's
``changes_requested`` setting picks how far it goes: "triage" (do what it
decided, reply to the rest), "ask" (recommend, then wait for your go-ahead),
"off" (leave new rounds to you).
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Tuple

from backend.ticket_ingestion.models import PRComment, PullRequest

#: A round waits this long after its newest request before it goes out —
#: reviewers leave comments one by one; the session should get them together.
SETTLE_S = 10 * 60.0
#: At most this many requests in one round's prompt.
MAX_ITEMS = 40


def _ts(iso: Optional[str]) -> float:
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def comment_key(c: PRComment) -> str:
    """``c:<id>`` an inline review comment, ``r:<id>`` a "changes requested"
    review (it has no file)."""
    return ("c:%d" if c.path else "r:%d") % c.id


def pending_round(
    comments: List[PRComment],
    reviews: List[PRComment],
    handled: set,
    processed_at: float,
    now: float,
) -> Tuple[str, List[PRComment]]:
    """``("none" | "settling" | "ready", new requests)``: the requests said
    after ``processed_at`` (when intake first handled the PR) that no round has
    carried yet; "settling" while the newest is under :data:`SETTLE_S` old."""
    fresh: List[PRComment] = []
    for c in list(reviews) + list(comments):
        if comment_key(c) in handled:
            continue
        said = _ts(c.created_at)
        if said and said <= processed_at:
            continue  # the first pass already had it
        fresh.append(c)
    if not fresh:
        return "none", []
    newest = max((_ts(c.created_at) for c in fresh), default=0.0)
    if newest and now - newest < SETTLE_S:
        return "settling", fresh
    return "ready", fresh


def build_round_prompt(pr: PullRequest, items: List[PRComment], policy: str) -> str:
    """The triage prompt for one round (see the module docstring)."""
    authors = sorted({c.author for c in items if c.author})
    lines = [
        "# New review round on PR #%s: %s" % (pr.number, pr.title or ""),
        str(getattr(pr, "url", "") or ""),
        "",
        "%d new request%s since your last pass, from %s."
        % (
            len(items),
            "" if len(items) == 1 else "s",
            ", ".join(authors) or "reviewers",
        ),
        "",
        "## The requests",
    ]
    for i, c in enumerate(items[:MAX_ITEMS], 1):
        where = (
            "`%s:%s`" % (c.path, c.line)
            if c.path and c.line
            else ("`%s`" % c.path if c.path else "the whole PR (requested changes)")
        )
        lines.append(
            "%d. **%s** on %s — %s" % (i, c.author or "reviewer", where, c.url or "")
        )
        body = (c.body or "").strip()
        lines.append("   > " + body.replace("\n", "\n   > ")[:2000])
    if len(items) > MAX_ITEMS:
        lines.append("… and %d more on the PR." % (len(items) - MAX_ITEMS))
    lines += [
        "",
        "## Triage first — decide before you change anything",
        "For EACH request pick one:",
        "- **Do it** — it is right and in scope: make the change.",
        "- **Push back** — it is wrong, out of scope, or would make things worse: "
        "reply on its thread saying why, specifically and politely, and change nothing for it.",
        "- **Ask the owner** — it is a judgment call (a product decision, a big redesign, "
        "two requests that conflict): don't guess, ask.",
        "Write the triage as a table (request · decision · why) first.",
        "",
    ]
    if policy == "ask":
        lines += [
            "Then STOP: ask the owner to approve the triage (which you'd do, which you'd "
            "push back on). Change nothing and reply to no thread until they answer.",
        ]
    else:
        lines += [
            "Then do the **Do it** ones on this PR's branch, commit and push (the PR "
            "updates), and reply on each thread with what you did or why you didn't. "
            "Ask the owner about the **Ask** ones and wait for that answer before "
            "touching them.",
        ]
    lines += [
        "",
        "Rules: stay on this PR's branch (%s); never force-push; never merge; "
        "don't resolve a reviewer's thread for them."
        % (pr.head_ref or "its head branch"),
    ]
    return "\n".join(lines)
