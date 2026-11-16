"""Reply to EVERY review request (docs/pr-review-rounds.md "A reply to every
request").

PR intake's prompts — the first pass (``pr_runner.build_consolidated_pr_prompt``
with ``reply_all``) and every later round (``pr_rounds.build_round_prompt``) —
end with the same rules: each request gets a reply on GitHub whatever was
decided (done → what changed + the commit sha; pushed back → why; asked the
owner → waiting on the owner), in the request's own thread for an inline
comment, in ONE PR conversation comment for conversation comments and review
bodies. The exact commands are spelled out so the agent never guesses an
endpoint.
"""

from __future__ import annotations

from typing import List

from backend.ticket_ingestion.models import PRComment, PullRequest


def in_thread(c: PRComment) -> bool:
    """An inline review comment (it has a file): replied to in its thread."""
    return bool(c.path)


def thread_id(c: PRComment) -> int:
    """The comment a thread reply goes to: the thread's first comment."""
    return c.reply_to if isinstance(c.reply_to, int) else c.id


def label(c: PRComment) -> str:
    """What the request is, in words."""
    if in_thread(c):
        return "inline comment"
    if c.kind == "issue":
        return "conversation comment"
    if (c.review_state or "").upper() in ("COMMENTED", "APPROVED"):
        return "review"
    # A review with no file and no other state: "changes requested" (the
    # same reading as pr_rounds.comment_key's ``r:``).
    return "requested changes"


def where_to_reply(c: PRComment) -> str:
    """One line per request: where its reply goes."""
    if in_thread(c):
        return "reply in its own thread (thread comment id `%d`)" % thread_id(c)
    return "reply in your PR conversation comment (quote or link it)"


def _repo(pr: PullRequest) -> str:
    return str(getattr(pr, "repo", "") or "") or "OWNER/REPO"


def reply_rules(pr: PullRequest, items: List[PRComment]) -> List[str]:
    """The "reply to every request" section, commands filled in for ``pr``."""
    repo, n = _repo(pr), pr.number
    threads = [c for c in items if in_thread(c)]
    loose = [c for c in items if not in_thread(c)]
    out = [
        "## Reply to EVERY request — no exceptions",
        "Each request above gets a reply on GitHub, whatever you decided:",
        "- **Done** — say what changed and name the commit: "
        '"Done in `<sha>`: <what changed>." Commit and push first, so the sha '
        "is on the PR.",
        "- **Pushed back** — say why, specifically and politely; change nothing for it.",
        "- **Asked the owner** — say it is waiting on the owner: "
        '"Waiting on the owner: <the question>."',
        "",
    ]
    if threads:
        out += [
            "Inline comments (they have a file) — reply in the comment's OWN "
            "thread, to its thread comment id:",
            "```sh",
            "gh api -X POST repos/%s/pulls/%d/comments/<thread comment id>/replies "
            "-f body='<your reply>' --jq .html_url" % (repo, n),
            "```",
        ]
    if loose:
        out += [
            "Conversation comments and review bodies (no file) — ONE PR "
            "conversation comment that answers all of them, each quoted "
            "(`> …`) or linked, with its reply under it. Write it to a file "
            "outside the repo first, then:",
            "```sh",
            "gh api -X POST repos/%s/issues/%d/comments "
            '-F body=@"${TMPDIR:-/tmp}/pr-%d-reply.md" --jq .html_url' % (repo, n, n),
            "```",
        ]
    out += [
        "Without `gh`, the same REST calls with a token: `curl -sS -X POST "
        '-H "Authorization: Bearer $GITHUB_TOKEN" '
        '-H "Accept: application/vnd.github+json" '
        "https://api.github.com/repos/%s/…` with a JSON body "
        '`{"body": "<your reply>"}` (`pulls/%d/comments/<id>/replies` for a '
        "thread, `issues/%d/comments` for the conversation comment)." % (repo, n, n),
        "Never resolve a reviewer's thread for them — the reviewer does that.",
    ]
    return out


def summary_rule() -> str:
    """The closing ask: every request, its decision, its reply's link."""
    return (
        "Finish with a summary that lists EVERY request: its decision (done "
        "with the commit sha / pushed back / waiting on the owner) and the "
        "link to its reply (the `html_url` each reply call prints)."
    )
