# Ticket-ingestion pipeline

Package: `backend.ticket_ingestion` (the module name is retained for
compatibility; the pipeline is now provider-agnostic). Turns tickets from any
connected ticketing platform — **Jira, Linear, GitHub Issues, Shortcut, or
Asana** — plus GitHub PR reviews and newly opened GitHub issues into running
Claude sessions, hands-free.

## Untrusted input (prompt injection)

Ticket, issue, and PR-review text is fed **straight to a coding agent as its
prompt**, and anyone with access to your tracker can author it — so treat it as
**untrusted, a prompt-injection vector** (see [SECURITY.md](../SECURITY.md)).
What bounds it:

- **Nothing is pushed automatically.** The pipeline provisions a workspace and
  runs the agent; commit / push / PR / merge stay explicit human actions.
  Review every diff before pushing — the output is as untrusted as the input.
- **Worktree isolation** (and per-session cost budgets) bound a single run's
  blast radius, but do **not** sandbox filesystem or network access within your
  own user account. There is no separate ingestion sandbox — isolation is the
  per-session worktree/workspace only.
- Provisioned agents launch with `--dangerously-skip-permissions`
  (`[mindflock].skip_permissions`, **default on**) so a fresh worktree doesn't
  re-prompt the per-folder trust gate — which also means the agent acts on
  instructions embedded in the ticket without asking. Set
  `[mindflock].skip_permissions = false` to make it re-prompt on sensitive
  actions, and only enable ingestion for repos and trackers you control.

## Providers

The ticket source is an interchangeable **`TicketProvider`** adapter
(`backend.ticket_ingestion.providers`). Each adapter turns its platform's API
into the pipeline's normalized `Ticket`, and everything downstream (validation,
prompt building, provisioning, runners) is provider-agnostic. The active
provider is chosen by `[ticketing].provider` (see
[configuration.md](configuration.md#ticketing-providers)).

| Provider | How "assigned to me" is fetched |
|---|---|
| `shortcut` | `POST /stories/search` by `owner_id`, then per-story hydration |
| `jira` | `POST /rest/api/3/search/jql`, `assignee = currentUser()` (description flattened from ADF — headings keep their `#` level, which is what acceptance-criteria mining matches on) |
| `linear` | GraphQL `viewer.assignedIssues(filter: {updatedAt})` |
| `github_issues` | `GET /repos/{owner}/{repo}/issues?assignee=<you>` (PRs filtered out) |
| `asana` | `GET /tasks?assignee=me&workspace=<gid>` |

Adding a provider = one new module implementing `search_assigned` / `fetch` /
`test_connection`, plus a `PROVIDER_REGISTRY` + `PROVIDER_META` entry. Acceptance-
criteria mining and link/attachment extraction are shared in `providers/base.py`.

Optionally an adapter also implements **`search_assigned_all()`** (`base.py`),
which backs Settings → Ticketing → *Assigned tickets*: every ticket assigned to
you with no age cutoff and no workflow-state filter, each annotated with its
state. Shortcut, Jira and Linear override it; GitHub Issues and Asana keep the
base implementation (an epoch-anchored `search_assigned`) because they expose no
workflow-state model — so their tickets land in the panel's `No state` bucket.
That asymmetry is visible in the panel itself.

**Multiple sources.** You can configure more than one source at once — different
providers *and* several of the same provider with different credentials (two Jira
sites, two GitHub repos, …) — via an array of `[[ticketing.source]]` entries (or
the Settings → Ticketing "Add source" list). The orchestrator runs one poller per
source, each on its own cadence and its own `last_run_timestamps` checkpoint, all
feeding the one processing queue. Each source has a stable `id` that becomes its
slug/branch prefix, so tickets from different sources never collide.

## Running it

```bash
python -m backend.ticket_ingestion   # from the repo root; reads ./config.toml
```

No CLI arguments — everything comes from `config.toml`
([configuration.md](configuration.md)). Or toggle it from the web UI's
**Ticket Ingestion** sidebar bar, which runs it as a managed subprocess and
tails `logs/ticket-ingestion.log`.

**Repo root.** The web server (the ingestion addon and the PR-review flow)
resolves the pipeline's repo root — where `config.toml` and `state.json` live —
as `MINDFLOCK_REPO_ROOT` env → nearest ancestor directory containing
`config.toml` → cwd. Getting this wrong (as installed uv-tool/pipx copies once
did) splits the processed-story ledger across two `state.json` files, so
already-done tickets get re-ingested; set `MINDFLOCK_REPO_ROOT` explicitly if
the auto-detection can't see your `config.toml`.

**Singleton per directory**: the process takes an exclusive `flock` on
`.mindflock-pipeline.lock` (writing its PID into it). A second copy exits **0**
with a message — deliberately not an error, so the web controller doesn't flag
it. Two pipelines against one repo would collide on `workspaces/pr-<n>` dirs and
tmux session names.

On startup the orchestrator prunes workspaces untouched for 3 days
(`_<name>_refresher` dirs are preserved), runs an initial story scan, then starts its
loops: the story poller (always), the PR poller (if `[github]` is present and
enabled), and one cache refresher per refresh-enabled `[[workspace.cache]]`
entry.

## Story flow

```
Shortcut search ──► dedup ──► validate ──┬─ valid ──► provision ──► launch Claude
 (poll, 20s)      (branch +   (length,   │
                   state.json) AC)       └─ invalid ─► clarification session
```

1. **Poll** — every `poll_interval_seconds`, `POST /stories/search` for stories
   owned by `member_id` and updated since the last run (optionally restricted to
   `workflow_state_id`). Search results lack descriptions, so each story is
   re-fetched by id. Retries with exponential backoff; a failed scan never kills
   the pipeline.
2. **Dedup** — a story is skipped if a remote branch `feature/sc-<id>/…` already
   exists (`git ls-remote`) or its id is in `state.json`'s `processed_stories`.
   Survivors are processed oldest-first.
3. **Parse** — the description is mined for an `## Acceptance Criteria` section
   (bullets, numbered items, or WHEN/THEN/AND blocks); comments and attachments
   (Shortcut-hosted files get authenticated downloads) are collected.
4. **Validate** — description non-empty, at least `min_description_length` chars,
   and acceptance criteria present.
5. **Invalid → clarification session** — instead of skipping, a workspace is
   provisioned and Claude is launched with a "Clarification Needed" prompt listing
   what's missing, so you can fix the ticket interactively.
6. **Valid → launch** (two paths, chosen by `[mindflock].enabled`):
   - **Engine path** (enabled — **the default**, also when `[mindflock]` is
     absent entirely) — `SessionRunner` creates an engine instance
     (`title sc-<id>`, provisioned workspace, branch
     `feature/sc-<id>/<slug>`, ticket text as the seed prompt). The session
     shows up in the web grid as `mindflock_sc-<id>` within seconds. Attachments
     are downloaded into the live worktree's `.ticket_attachments/`.
     The bridge is **in-process** (`session_runner` imports `backend.session`
     and calls `Instance.Start` directly): no HTTP, no host/port, nothing that
     can be "unreachable", so it works headless too. Instances are written to
     `~/.mindflock/state.json`; a running server adopts them into its grid
     within ~4s (`web/core/engine._sync_external_instances`) and a server
     started later picks them up on boot. The orchestrator falls back to the
     standalone path only when `backend.session` / `backend.config` cannot be
     imported at all (partial install), and logs a `WARNING` naming the reason.
   - **Standalone path** (`enabled = false`) — `EnvironmentProvisioner` makes a
     full `git clone` workspace (deps synced, pre-commit installed, testmon
     seeded, opened in Cursor), then `ClaudeCodeRunner` starts a bare tmux
     session `sc-<id>` running `claude "$(cat <prompt>)"` and opens a terminal
     tab attached to it. Opening that tab is **strictly best-effort**
     (`terminal_launch.build_terminal_tab_argv`): Windows Terminal on WSL,
     `gnome-terminal`/`konsole`/`xterm` on Linux, `Terminal.app` on macOS. On
     WSL with no `wt.exe` on `PATH` (e.g. a stripped systemd/service
     environment), or with Windows interop flushed from `binfmt_misc` (a
     Docker or qemu install can reset it — detected by
     `terminal_launch.wsl_interop_available()`), it degrades to a no-op, and
     any spawn failure is logged (with a `tmux attach -t <session>` hint)
     rather than raised — so the launch never fails the invoke (this covers
     the story runner, PR runner, and IDE launch alike) and the session is
     always reachable over tmux even when no GUI tab opens.
7. **Record** — the story id, branch, and status land in `state.json`.

The prompt contains the story name, Shortcut URL, description, acceptance
criteria, comments, local attachment paths, and an instruction to advance the
ticket in-repo and state out-of-scope work explicitly.

## PR-review flow

Every `github.poll_interval_seconds`, the monitor lists open non-draft PRs into
`base_branch` **authored by the authenticated GitHub user**, at least
`min_age_minutes` old, and not yet in `state.json`'s `processed_prs`.

For each PR, **actionable comments** are unresolved, non-outdated review-thread
comments not written by the PR author or anyone in `skip_authors`. CodeRabbit
comments are reduced to their embedded "Prompt for AI Agents" block when present.
A PR with no actionable comments is recorded and skipped without provisioning.

- **Engine path** — one consolidated session per PR (`pr-<n>` →
  `mindflock_pr-<n>`): the PR's head is checked out via `refs/pull/<n>/head` (fork
  PRs work) into `workspaces/pr-<n>`, provisioned, and adopted by the engine.
  One prompt lists every comment (author, file:line, diff hunk, body) and
  instructs Claude to work through all of them in one pass and **leave changes
  unstaged** — no commit/push — for human review.
- **Standalone path** — one Windows Terminal window per PR with one
  tmux tab per comment, sharing the workspace; a `flock` on `.pr-edit-lock`
  serializes their write phases, and completion is detected via per-comment done
  markers.

A PR is processed **once per head**; to re-run it, delete its entry from
`state.json`'s `processed_prs`.

GitHub auth resolution: `[github].token` → `$GH_TOKEN` → `$GITHUB_TOKEN` →
`gh auth token`.

## Issue-handling flow

A sibling of the PR flow that turns **newly opened GitHub issues** into sessions
(`ticket_ingestion/issue_monitor.py`, `IssueMonitor`). It is **opt-in and off
by default**: the orchestrator only starts the issue loop when `[github]` is
present, `issues_enabled` is true, and `issue_repos` is non-empty (its **own**
repo list, independent of PR review's `repos`).

Every `github.issue_poll_interval_seconds` (default 60), `IssueMonitor.scan()`
lists open issues in each `issue_repos` entry (the GitHub issues endpoint also
returns PRs — those are filtered out), keeping ones at least
`issue_min_age_minutes` old (default 15), not by an author in
`issue_skip_authors`, and whose `(repo, number)` isn't already in the
processed-issues ledger. Each survivor is normalized to the pipeline's `Ticket`
(`issue_to_ticket` — title, body, comments) and run through the same engine
launch path as a story, on a fresh branch (`gh-<n>`). Its `(repo, number)` is
then recorded so it isn't re-ingested.

The **Git issues** Settings screen surfaces this live (`/api/github/issues`),
and **Start work** (`/api/github/issues/start`) force-starts one issue against
the running server's engine — bypassing the age / already-handled filters — so
the session shows up in the grid without a reload.

## Cache refreshers

Each `[[workspace.cache]]` entry with a `refresh_command` gets a background
refresher that keeps its warm seed fresh, so provisioned workspaces start warm
instead of cold. A persistent workspace at `workspaces/_<name>_refresher`
tracks the cache's `refresh_branch`: each cycle it hard-resets to origin, runs
the workspace setup commands, runs `refresh_command` (with the cache's `env`
exported), and atomically publishes the resulting `workspace_path` artifact to
`seed_path`. Failures are logged and the loop continues; cycle cadence is
`refresh_interval_seconds` (default hourly).

The canonical example is a `testmon` cache (`workspaces/_testmon_refresher`,
artifact `.testmondata`, refresh command `pytest --testmon` with
`TESTMON_ENV=shared`) — workspaces seeded from it only run diff-impacted tests.

## State and files

| File | Contents |
|---|---|
| `./state.json` | `{last_run_timestamp, processed_stories: [{story_id, branch, status, processed_at}], processed_prs: [{number, head_sha, processed_at}]}` — dedup/resume state. Corrupt/missing → starts fresh. |
| `./.mindflock-pipeline.lock` | Singleton flock; contains the winner's PID |
| `./logs/pipeline.log` | The pipeline's own log (`[logging]` config) |
| `./logs/ticket-ingestion.log` | stdout/stderr when run under the web UI addon |
| `./workspaces/` | Per-story/PR workspaces, `_base_*`, `_testmon_refresher` |
| `<workspace>/.ticket_attachments/` | Downloaded story attachments (100 MiB/file cap) |
| `~/.mindflock-assistant/prompts/` | Generated prompt files (pruned after 1 h) |

Story dedup is by id, so a `skipped`/`completed` story is never retried without
hand-editing `state.json`.

## Current-behavior caveats

- **Engine routing is on by default** (`[mindflock].enabled`, default `true`,
  including when the section is absent) — but the section must be named
  `[mindflock]`: a section left over from an older project name is ignored, so
  an `enabled = false` in it does **not** take effect (and neither do
  `mode`/`open_cursor`/`skip_permissions`; see
  [configuration.md](configuration.md)).
- **Ingestion is polling-only** — there is no webhook listener.
- Only review-thread comments are actioned on PRs; top-level issue comments are
  fetched by dead code and ignored.
- The clarification handler always continues with the original story after
  launching the clarification session (its "skip" branch is unreachable), and
  the standalone story runner does **not** pass
  `--dangerously-skip-permissions` (the standalone PR runner and the engine path,
  when configured, do).
