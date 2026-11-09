# PR review rounds

PR intake (Intake → Pull requests, "auto review") handles **your** open PRs:
the first time one has review comments, a session addresses them. Before this,
that was the end of it — a reviewer's second pass went unnoticed.

Now every PR intake has handled is watched for a new **round**: a
**"changes requested"** review, or review comments, that came after the last
pass. A round waits until the reviewer has stopped for 10 minutes
(`pr_rounds.SETTLE_S`) — a series of comments is one round — then goes to the
PR's session: **queued into it** when `pr-<repo>-<n>` still exists (delivered
once the agent is idle), a **new session** on the PR's branch otherwise.

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

## Bookkeeping

`state.json` `pr_rounds`: `{"<repo>#<n>": {"items": ["c:<comment id>",
"r:<review id>", …], "at": …}}` — what each round carried, so a request goes
out once. A request older than the PR's first pass (`processed_prs`) counts as
handled by that pass.
