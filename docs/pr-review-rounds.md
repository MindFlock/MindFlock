# PR review rounds

PR intake (Intake → Pull requests, "auto review") handles **your** open PRs:
the first time one has review comments, a session addresses them. Before this,
that was the end of it — a reviewer's second pass went unnoticed.

Now every PR intake has handled is watched for a new **round**: any
**request** (below) that came after the last pass. A round waits until the reviewer has stopped for 10 minutes
(`pr_rounds.SETTLE_S`) — a series of comments is one round — then goes to the
PR's session: **queued into it** when `pr-<repo>-<n>` still exists (delivered
once the agent is idle), a **new session** on the PR's branch otherwise.

## What counts as a request

`pr_comments.fetch_pr_requests` reads, for both the first pass and every
round:

| Kind | From | Bookkeeping id |
|---|---|---|
| Inline review comment (unresolved, not outdated thread) | GraphQL `reviewThreads` | `c:<comment id>` |
| "Changes requested" review | REST `pulls/{n}/reviews` | `r:<review id>` |
| "Commented" / "approved" review with a body (a bot's findings outside the diff range live there) | REST `pulls/{n}/reviews` | `b:<review id>` |
| PR conversation comment | REST `issues/{n}/comments` | `i:<comment id>` |

Always dropped: the PR author's own, and the repo's `skip_authors`. Also
dropped, as pure noise:

- **auto-generated summaries** — a bot's own `<!-- … auto-generated … -->`
  marker naming a summary, walkthrough or status — unless the body still
  carries findings ("outside the diff range", a suggested change);
- **status / approval only** — short notes like "LGTM", "Thanks!", 👍, a
  build or deploy notice, with no request in them;
- **already answered** — a comment the PR author replied to *after* it: in
  the same thread for an inline comment, with a newer conversation comment
  for a conversation comment or review body. A "changes requested" review
  stays until the reviewer says otherwise.

A failed fetch of any of the three is a `PRCommentsFetchError`, never "no
comments": the PR is retried on the next poll.

## Triage, not blind obedience

The round's prompt (`pr_rounds.build_round_prompt`) lists every request (who,
file:line, link, the words) and makes the session decide, per request, before
changing anything:

| Decision | What it means |
|---|---|
| **Do it** | Right and in scope — make the change. |
| **Push back** | Wrong, out of scope, or would make things worse — reply on the thread saying why; change nothing for it. |
| **Ask the owner** | A judgment call (product decision, big redesign, conflicting asks) — ask you instead of guessing. |

It writes the triage as a table first.

## Per repo: "When changes are requested"

On each PR repo's card (Intake → Pull requests):

| Choice | `github.repo_settings[repo].changes_requested` | Then |
|---|---|---|
| **Triage and handle** (default) | `""` | Does the *Do it* ones, commits and pushes to the PR branch, replies on each thread; asks you about the *Ask* ones. |
| **Triage, then ask me** | `"ask"` | Stops after the triage and asks you to approve it before changing anything or replying. |
| **Leave it to me** | `"off"` | New rounds are ignored. |

Always: stays on the PR's branch, never force-pushes, never merges, never
resolves a reviewer's thread for them.

## A reply to every request

Both the first pass (`pr_runner.build_consolidated_pr_prompt(…,
reply_all=True)`) and every round (`pr_rounds.build_round_prompt`) make the
session reply to **every** request, whatever it decided
(`pr_replies.reply_rules`):

| Decision | The reply |
|---|---|
| Done | what changed, naming the commit sha (committed and pushed first) |
| Pushed back | why |
| Asked the owner | that it is waiting on the owner |

- An **inline comment** gets its reply in its own thread:
  `gh api -X POST repos/<repo>/pulls/<n>/comments/<thread's first comment id>/replies -f body='…' --jq .html_url`.
- **Conversation comments and review bodies** get ONE PR conversation comment
  that quotes or links each:
  `gh api -X POST repos/<repo>/issues/<n>/comments -F body=@<file> --jq .html_url`.
- Without `gh`, the prompt gives the same REST calls with a token.

The session's final summary lists each request, its decision and the link to
its reply. The first pass now commits and pushes like a round does (it used
to leave the changes unstaged). Under "Triage, then ask me" the replies wait
for your answer, then every request still gets one.

## Bookkeeping

`state.json` `pr_rounds`: `{"<repo>#<n>": {"items": ["c:<comment id>",
"r:<review id>", "b:<review id>", "i:<comment id>", …], "at": …}}` — what
the first pass and each round carried (or left to a route), so a request goes
out once. `c:` and `r:` mean what they always did, so older state stays
valid. A request older than the PR's first pass (`processed_prs`) counts as
handled by that pass.

## The same session

PR work goes back to the session that **built the branch** — usually the
ticket's own session — so the agent addressing the review has the ticket, its
plan and its earlier reasoning in context, instead of a fresh session reading
the diff cold. The pipeline asks the server (`POST
/api/sessions/by-branch/hand-off {branch, text}`):

1. a live session on the PR's head branch (the ticket's before a `pr-…` one;
   never a verify checker) gets the review in its prompt queue (typed in once
   its agent is idle) → `queued`;
2. else the newest recently-closed one is **reopened** on its worktree, with
   its conversation, then given the review → `reopened`;
3. else (404, or the server is out of reach) the PR gets a session of its own,
   as before.

Both the first pass on a PR and every later review round go this way.

## Routes

A session flying a route (docs/flights.md) can have a `reviews` stop that
loops on the PR's review comments itself. One owner handles a PR's comments
at a time:

| When | Owner | What the other does |
|---|---|---|
| A live session on the PR's branch flies an unfinished flight whose `reviews` stop is not passed (pending, running, waiting on you, or failed and halted) | **the route** | PR intake hands nothing over and records what it saw as handled |
| That stop is done, skipped, or failed and moved on; the flight finished or was cancelled; or it has no `reviews` stop | **PR intake** | anything the stop delivered (its `seen`) counts as handled and is never sent again |

Both owners read the same requests (`fetch_pr_requests`, same noise rules),
key them the same way (`c:` / `r:` / `b:` / `i:`), send the same prompt
(`build_consolidated_pr_prompt(…, reply_all=True)`) and so reply to every
request. A route's `reviews` stop with `include_bots: false` ignores bots'
requests, and PR intake leaves them alone while the route owns the PR.

How the pipeline knows: before every first pass and every round it asks the
server `GET /api/sessions/by-branch/review-owner?branch=<head>` →
`{"owner": "route" | "intake", "title", "handled": [keys]}`
(`flights.review_owner`, over the live sessions on the branch and their
flights in any state). `route` → it records the requests and hands nothing
over; `intake` → it drops the `handled` keys (recording them) and hands over
the rest. The stop's `seen` ids are read at that moment, so they hold even
when the stop delivered them while the pipeline's round was still settling
or the pipeline wasn't running. As a second line, the hand-off itself
answers `{"title", "how": "flight"}` and queues nothing while the route
owns the PR.

Tolerant both ways: an older server has no `review-owner` (404 → "unknown":
everything is handed over, as before) and never answers `"flight"` (a reply
without `how` reads as `queued`); an older pipeline reads `"flight"`'s
title as handed and starts nothing.
