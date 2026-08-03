# Web API reference

FastAPI app: `backend.web.server:app`. All routes are registered before the static
mount at `/`, so `/api/*` always wins. UI pages (`/`, `/m`, `*.html/.css/.js`) are
served with `Cache-Control: no-cache`.

Conventions: JSON bodies in, JSON out. Errors use standard status codes with
`{"detail": ...}` or `{"error": ...}`. `{title}` is the session title.

## Instances

### `GET /api/instances`

List all sessions. Each item:

```jsonc
{
  "title": "sc-19815", "branch": "feature/sc-19815/…", "repo": "…",
  "folder": "/abs/workspace", "folder_label": "…", "program": "claude",
  "path": "…", "status": "running|ready|loading|paused", "started": true,
  "tmux_name": "mindflock_sc-19815",
  "provisioned": true, "workspace_strategy": "worktree", "in_place": false,
  "stage": "provisioning|agent|precommit|interrupt|committed|pushed|pr|merged",
  "pr_url": "…",              // when a PR exists
  "failed_step": "…",         // when stage == "interrupt" (pre-commit ✗)
  "activity": "working|clarify|idle|offline",
  "tokens": 0, "tokens_in": 0, "tokens_cache_read": 0, "tokens_cache_write": 0,
  "tokens_ctx": 0, "tokens_ctx_window": 200000, "tokens_cost": 0.0,
  "tokens_model": "…",
  "diff_stat": {"files": 2, "additions": 42, "deletions": 7},  // or null
  "workspace_missing": false,  // L1(c): started but its directory vanished
  "has_origin": true,          // L2: workspace has an `origin` remote (cached ~30s)
  "last_turn": "…"             // L3: ≤120-char snippet of the latest agent turn, or null
}
```

Stage is inferred from git (upstream, origin SHA, commit lock files) plus a
GitHub lookup for the PR stages — `gh` when it is installed and authenticated,
otherwise the REST API with a resolved token. With neither credential the badge
still advances all the way to **pushed** (that part is pure git) but cannot see
an open PR, so it stops there. Because stage comes from git, work done outside
MindFlock (e.g. in Cursor) moves the badge too. The origin
branch SHA is a network `git ls-remote`, cached ~45 s — a push made outside
MindFlock can take up to ~45 s to advance the badge and enable **Make PR**
(MindFlock-initiated pushes bypass the cache via a pending window). The base
branch used for stage/diff/PR is **per session**: the branch the worktree was
cut from, recorded at creation (`base_branch` in `state.json`); sessions that
predate the field resolve `origin/HEAD` → `main`/`master` → the configured
provision base (only when the session's repo IS the configured repo). Existing-PR
detection (what advances the chip past `pushed` to `pr`) looks up the PR by
**head branch only** and is intentionally base-agnostic — Make PR can target any
base (a configured default like `staging`, or one chosen in the dialog the
server never sees), so a base-scoped lookup would miss the real PR and wedge the
chip on `pushed`. The base still keys the "is there work to push" (`beyond`)
test; only the PR lookup drops the base filter.
`activity` is layered: the provider's own authoritative signal is preferred —
per-session `{state, ts}` markers written by the CLI's lifecycle hooks, or
Claude's live `claude agents --json` report (see
[providers.md](providers.md)) — then CPU sampling of the pane's process tree
(a cached `/proc` scan, 2.0 s TTL kept deliberately below the server's 2.5 s
probe memo so two consecutive activity computations never share a snapshot and
read a zero CPU delta — phantom idle), then the pane-hash heuristic: changing
= `working`, provider "waiting" patterns = `clarify`, static ≥ 3 s = `idle`,
no tmux = `offline`.

`diff_stat` (J3) is the total change the session has produced vs its
per-session base — committed-beyond-base **plus** uncommitted tracked changes,
one `git diff --shortstat <merge-base(base, HEAD)>` cached ~10 s per session.
Untracked files aren't counted (counting them would mutate the index on every
poll). `null` when unavailable (paused / loading / no worktree / git failure).

`workspace_missing` (L1c) is `true` for a started, non-paused session whose
workspace directory no longer exists (wiped outside MindFlock). The UI renders
these as a muted row + placeholder pane with a single **Clean up** action
(`DELETE /api/instances/{title}`, which tolerates the missing dir).
`has_origin` (L2) tells the UI to replace **Push** with setup guidance when the
workspace has no `origin` remote — pushing would only fail in the shell.
`last_turn` (L3) is a one-line, markdown-stripped snippet of the session's
latest conversational turn (provider-dependent; `null` when the provider
doesn't expose one) for at-a-glance triage across many sessions.

### `POST /api/instances` → **202**

Create + start a session. Provisioning runs in the background; the session
appears as `loading` until ready.

```jsonc
{
  "title": "sc-19815",          // optional with story_id (defaults to sc-<id>)
  "program": "claude",          // optional; default from ~/.mindflock/config.json
  "repo_path": "/path/to/repo", // base repo (plain sessions; optional for provisioned)
  "in_place": false,            // run directly in repo_path, no worktree
  "init_repo": false,           // git init + initial commit if needed
  "provisioned": true,          // provision a fully-loaded workspace
  "workspace_strategy": "worktree", // or "clone"
  "story_id": "19815",          // → branch feature/sc-19815/<slug>
  "prompt": "…ticket text…",    // seeds the agent on first launch
  "launch_args": ["--dangerously-skip-permissions"] // optional per-session flags
}
```

A full slash-path in the title (e.g. `feature/sc-1/foo`) is used verbatim as the
branch. `provisioned` + `repo_path` provisions **that local repo** (setup
commands auto-detected, no shared cache seeds); `provisioned` without
`repo_path` requires the configured `[repository].url`. Errors: 400 (empty
title, bad strategy/config), 409 (title exists).

`launch_args` (optional) are extra CLI flags appended on **every** (re)start of
this session's agent, after the provider's own saved flags. They are validated
with the same rules as provider `[launch] args` (a list of non-empty tokens; no
newlines/NULs; ≤512 chars each) → **400** on invalid input. Omitting the key
means "not specified", so the session inherits the global default for its
provider (`coding_cli.default_launch_args`, see
[configuration.md](configuration.md)); an explicit list — **even `[]`** — is used
verbatim, so a default the caller toggled off is honored, not re-applied.

### Lifecycle

| Method | Path | Effect |
|---|---|---|
| DELETE | `/api/instances/{title}` | Kill session, remove worktree + branch |
| POST | `/api/instances/{title}/close` | End tmux, **keep** worktree; recorded in recently-closed |
| POST | `/api/instances/{title}/cleanup` | Kill + permanently delete the workspace dir (+ close its Cursor window) |
| POST | `/api/instances/{title}/copy` → 202 | New in-place session `<title>-copy` sharing the same worktree |
| POST | `/api/instances/{title}/pause` | Pause (commit, detach, remove worktree, keep branch) |
| POST | `/api/instances/{title}/resume` | Resume a paused session |

### Diff

| Method | Path | Returns |
|---|---|---|
| GET | `/api/instances/{title}/diff?base=fork\|head` | `{added, removed, content, base, error}` — `base=fork` (default) diffs vs the session's fork point, `base=head` vs the current HEAD |
| GET | `/api/instances/{title}/file-diff?path=<rel>&base=fork\|head` | Whole-file unified diff `{content, error}` |

### Guided workflow (commit → push → PR → merge)

Commit and push are pure git: they run in the session's own shell against the
remote the repo already has, **SSH or HTTPS, used verbatim**, with the user's
own git credentials. The GitHub CLI is not involved and no remote URL is ever
rewritten. Only the two PR endpoints need to reach the GitHub API, and each
resolves a credential in the same order — `gh` (when installed *and*
authenticated) → the REST API with a token
(`backend.ticket_ingestion.github_auth.resolve_token`) → a browser URL. There is
no response whose only content is "gh is not installed".

| Method | Path | Behavior |
|---|---|---|
| POST | `/api/instances/{title}/commit` | Body `{message}`. Runs `git add -A` + `git commit` **in the session's shell tmux** (watch pre-commit hooks in the Terminal tab), retrying up to 5× when hooks auto-fix files. Works for every session type (plain, in-place, provisioned). Writes `.mindflock_commit_status` (exit code) and `.mindflock_commit_msg` (reused on empty re-commit). |
| POST | `/api/instances/{title}/push-branch` | `git push --no-verify -u origin HEAD` in the shell (hooks already ran on commit). **O3 soft gate:** when the repo's `.mindflock.toml` declares `check_command` and no check run passed against the current HEAD, returns `409 {error, check_required: true, check}`; re-POST with body `{"force": true}` to push anyway. |
| GET | `/api/instances/{title}/branches` | `{branches, current, default}` — the branch list backing the **Make PR** dialog's base picker. `branches` are `origin`'s remote heads (falling back to local heads when origin is unreachable, so it's never blank); `current` is the session's own branch (never a valid PR target); `default` is the pre-selected base (`repository.pr_base_branch` → the session's fork base). 404 unknown title, 409 workspace not ready |
| POST | `/api/instances/{title}/make-pr` | Opens a PR → `{ok: true, url}` (or `note: "PR already open"`). Three tiers, in order: `gh pr create --base <base> --fill` when `gh` is installed **and** authenticated; else the GitHub REST API with a token from the usual resolution chain; else **`200 {ok: false, compare_url}`** — a prefilled compare URL the UI opens in the browser, plus the remedy sentence "add a GitHub token in Settings → PR review, or install the GitHub CLI". A missing `gh` is never an error status. The UI's Make-PR dialog collects `<base>` from the branch picker above (and the frontend remembers the last base per repo — `prBaseByRepo` in `localStorage`); an omitted base falls back to the session's base branch |
| POST | `/api/instances/{title}/merge-pr` | Merges the branch's PR, same three tiers: `gh pr merge <branch> --merge`; else the REST API with a token; else **`200 {ok: false, pr_url}`** so the UI can send you to the PR page to merge it yourself |

### Assigned tickets, PR auto-review + issue handling

| Method | Path | Behavior |
|---|---|---|
| GET | `/api/tickets` | Assigned tickets on the configured ticketing sources, each annotated with auto-ingest eligibility → `{tickets, buckets, done_buckets, ingest_states, errors[], stale}` (per ticket: `eligible`, `reasons`, `has_session`). The slowest of the three panel fan-outs (~3 s: one provider search per source + a `git ls-remote` per repo); per-source failures come back in `errors[]` rather than failing the call. Powers Settings → Ticketing → **Assigned tickets** |
| POST | `/api/tickets/start` | Body `{source, id}` — force-start a coding session for one ticket, bypassing the auto-ingest filters. 400 missing `source`/`id`, 404 ticket gone, 409 a session for it already exists |
| GET | `/api/github/prs` | Open PRs on the configured repo(s), each annotated with why auto-review did / didn't pick it up |
| POST | `/api/github/prs/review` | Force-start a review session for a PR (Settings → PR review screen) |
| GET | `/api/github/issues` | Open issues on the issue-handling repos (`github.issue_repos`), each annotated with auto-handling eligibility (`eligible`, `reasons`, `has_session`). PRs filtered out. Powers the Settings → Git issues screen |
| POST | `/api/github/issues/start` | Body `{repo, number}` — force-start a coding session for one open issue on a fresh branch, bypassing the age / already-handled filters. 400 bad `owner/name` or number, 404 issue gone, 409 a session for it already exists |

The three **GET** panel routes above share one caching contract
(`_cached_fanout` in `server.py`), because each is an upstream fan-out the
settings panels poll while open:

- **≤20 s old** → the cached payload, `stale: false`.
- **20 s – 5 min old** → the cached payload is returned *immediately* with
  `stale: true`, and a single-flight background refresh sweeps upstream. Clients
  use `stale` to come back for the fresh copy in a moment (the UI re-polls every
  2 s while it is set) instead of sitting on data they know is being replaced.
  Those re-polls are cheap: a failed sweep backs off for 30 s, so they don't each
  turn into another request to an upstream that is already failing.
- **Older than 5 min, nothing cached, or `?fresh=1`** → the request awaits a real
  sweep. `fresh=1` is what the panels' **Refresh** button sends. (Note the
  spelling: these routes take `?fresh=1`, while `/api/doctor` and
  `/api/connections` take `?refresh=1`.)

**502** `{error}` is therefore returned only when there is no usable cached
payload — or when `fresh=1` asked for a real sweep. Once a panel has any payload
inside the 5-minute stale window, an upstream failure is logged and the last
known list keeps being served, so a GitHub/provider blip can't empty the panel;
the flip side is that a persistently failing upstream stays invisible to the
client for up to 5 minutes. `has_session` is annotated on a per-request copy, so
it stays live on cache hits.

### Worktree setup + verification gate (O2/O3)

Configured per repo by a committed `.mindflock.toml` (see
[configuration.md](configuration.md#mindflocktoml--per-repo-workspace-config)).
Status lives in marker files inside the worktree
(`.mindflock_setup.json` / `.mindflock_check.json`, git-excluded), so it
survives server restarts. Events: `session.setup_started/finished`,
`session.check_started/finished`.

| Method | Path | Behavior |
|---|---|---|
| GET | `/api/instances/{title}/setup?lines=200` | `{status, log}` — setup state (`running/ok/failed`, rc, copied files) + log tail |
| POST | `/api/instances/{title}/setup/rerun` | Re-run the setup pass (copy `copy_untracked` + `setup_commands`) → 202; 400 without config, 409 while running |
| GET | `/api/instances/{title}/check?lines=200` | `{status, log}` — check state incl. `sha` it ran against and `stale` vs current HEAD |
| POST | `/api/instances/{title}/check` | Run `check_command` in the worktree → 202; 400 without config, 409 while running. Also auto-runs when a session reaches the `committed`/`pushed` stage with no (or a stale) result. |

### IDE

| Method | Path | Behavior |
|---|---|---|
| POST | `/api/instances/{title}/ide` | Open/focus the workspace in the configured IDE (Settings → Advanced; Cursor by default). GUI editors get new windows maximized + existing ones focused; terminal editors open in a new terminal window → `{ok, opened_new}`; 400 if the IDE isn't launchable |
| GET | `/api/ides` | The known-IDE registry: `{ides: [{command, name, kind, installed}], current, current_name}` — for the Settings detected-IDE picker |
| GET/POST | `/api/cursor/autoadopt` | Get/set `{enabled}` — auto-adopt Cursor-opened workspaces as sessions |

### Send a message + prompt queue

| Method | Path | Behavior |
|---|---|---|
| POST | `/api/instances/{title}/send` | Body `{text, submit?}`. Types `text` into the **agent** window and (default) presses Enter, booting/resuming the agent tmux first if it isn't running — so one call kicks a fresh session into motion (max token use). `submit:false` types without submitting. Enter is a separate keystroke a beat after the text so an agent TUI doesn't read the burst as a paste. → `{sent, submitted}` (409 if the workspace is gone or `{budget_locked: true}` when the session is over budget, 502 if the send fails) |
| GET | `/api/instances/{title}/queue` | `{items: [{id, text, added}], pending, enabled, loop, loop_interval, wait_for_limit, limited_until, last_sent}` |
| POST | `/api/instances/{title}/queue` | Body `{text}` — append a prompt; enqueuing re-enables draining. → queue state |
| POST | `/api/instances/{title}/queue/flags` | Body `{enabled?, loop?, loop_interval?, wait_for_limit?}` — `enabled` gates auto-draining; `loop` re-queues each sent prompt so a self-improving prompt cycles forever; `wait_for_limit` holds draining until the usage window resets |
| POST | `/api/instances/{title}/queue/reorder` | Body `{id, direction}` (`up`/`down`) |
| POST | `/api/instances/{title}/queue/edit` | Body `{id, text}` — rewrite a queued prompt in place |
| DELETE | `/api/instances/{title}/queue?item=<id>` | Remove one item; omit `item` to clear the whole queue |

A background drain loop feeds the queue into the agent whenever it is **idle**
(finished a turn / at its prompt): it never interrupts `working`/`clarify`, and
if a started session's agent tmux has died (e.g. the CLI exited when usage ran
out) it reboots it — rate-limited — so a queued run resumes on its own the
moment usage returns. Before each would-be send the drain re-checks the
usage-limit hold: a hold is armed from the provider's own usage meter **even
when no limit banner is on the pane** (covering a session that ran out mid-turn
and rebooted to a fresh idle prompt), and a window that reads spent but carries
no usable reset time holds on a bounded fallback rather than sending. A meter
that reads open — or is unavailable — leaves the queue free to send. `GET
/api/instances` carries a per-session
`queue: {pending, enabled, loop}` summary for the UI badge. Each auto-send emits
`session.prompt_sent`; queue edits emit `session.queue_changed`.

### Session budget (J5)

| Method | Path | Behavior |
|---|---|---|
| GET | `/api/instances/{title}/budget` | The session's effective budget + current estimated cost |
| POST | `/api/instances/{title}/budget/raise` | Body `{limit, hours}` — raise the budget for this session (unlocks a `budget_locked` send); emits `session.budget_raised` |

### Terminals (WebSocket)

| Path | What you get |
|---|---|
| `WS /api/instances/{title}/terminal` | The **agent** terminal — attaches (or restarts) the session's tmux |
| `WS /api/instances/{title}/shell` | An interactive **shell** in the workspace (separate tmux `<name>_sh`, created on demand, `history-limit 100000`) |
| `GET /api/instances/{title}/history` | The full tmux scrollback as text (the UI's "Copy all") |

Protocol (shared by all terminal sockets, implemented in
`static/core/ws-xterm.js` / `core/terminal.py::pump_pty`):

- binary frames server→client: raw PTY bytes (feed to xterm.js)
- text frames client→server: `{"type":"resize","cols":C,"rows":R}`
- text frames server→client: `{"type":"error","message":…}` on spawn failure
- close codes: **4404** instance gone, **4409** workspace not ready, **4500**
  spawn failure — clients stop reconnecting on 4404/4409, otherwise retry ~2.5 s
- auth close codes (any websocket, from the middleware): **4401**
  unauthenticated — the SPA/mobile head reload to the login page; **4403**
  cross-origin refusal — a hard refusal, not a login prompt (don't retry or
  re-prompt)

Detaching a socket never kills the tmux session.

## Workspaces, recently closed

| Method | Path | Behavior |
|---|---|---|
| GET | `/api/workspaces?sizes=1` | `{workspaces: [{name, path, root, kind, size_bytes, active_session}], roots}`; `kind` ∈ `base·refresher·pr·tmp·worktree·workspace` (`size_bytes` computed only with `?sizes=1`) |
| POST | `/api/workspaces/delete` | Body `{path}` (must be a direct child of a managed root; cache refreshers always protected). A base clone (`_base_*`) is deletable ONLY when nothing references it — no attached worktrees and no active session based on it; otherwise 400 naming the holders. Kills any active session on the dir first. |
| POST | `/api/workspaces/clear` | **Bulk reclaim**: sweep every managed root and delete each workspace in one pass → `{ok, removed_count, removed: [name…], kept_active: [title…]}`. Only **unprotected, idle** dirs go: protected shared infra (base clones + cache refreshers) and any dir a **live** session is using are skipped (the latter listed in `kept_active`). Unlike per-row delete it never kills a running agent. Also GCs each removed worktree's `~/.claude.json` trust entry and prunes stale worktree registrations from base clones. |
| GET | `/api/recently-closed` | `[{id, title, branch, folder, in_place, provisioned, closed_at, exists}]` |
| POST | `/api/recently-closed/{id}/reopen` | Recreate a session on the preserved worktree (410 if the dir is gone) |
| POST | `/api/recently-closed/{id}/forget` | Body `{wipe}` — drop the entry, optionally delete the worktree |

## Remote devices (tailnet)

Multi-device control (gated by the `general.remote_control` setting): other
MindFlock servers on your tailnet appear as device groups in the sidebar, their
sessions namespaced `<device>::<title>`, and every per-session route proxies to
the owning device.

| Method | Path | Behavior |
|---|---|---|
| GET | `/api/remote/hello` | Identity/permission handshake target for other devices |
| GET | `/api/devices` | Tailnet devices running MindFlock + their connection state |
| POST | `/api/devices/{device}/connect` | Pair with a device (token exchange, persisted in `~/.mindflock/remote_devices.json`) |
| POST | `/api/devices/{device}/disconnect` | Drop the pairing |

## Config, providers, usage, settings

| Method | Path | Returns / accepts |
|---|---|---|
| GET | `/api/config` | `{default_program, provisioning_available, caps: {git, tailscale, ticketing, github}, home, repo_root, ide_name, onboarded, auth_mode, auth_enabled}` — `caps` reports which optional integrations are usable right now; the UI hides absent features and shows "connect X" guidance on their settings screens. `caps.github` is true when **either** credential exists (`gh` authenticated **or** a token resolves) and is cached ~60 s (unlike its PATH-stat siblings it shells out to `gh auth status`, and this endpoint is hit on every page load); it gates one-click **Make PR** / **Merge**, and when false those buttons take the browser-URL path rather than disappearing — pushing is unaffected either way |
| GET | `/api/providers` | `{providers: [{name, aliases, profiles: [{id, label}], default_selector}], default}` |
| GET | `/api/usage` | Rolling day/week/month/year token+cost totals per provider (Claude, Codex, …) |
| GET/POST | `/api/scroll-speed` | `{speed}` 1–20, applied live to tmux |
| GET/POST | `/api/window-refresh` | The scheduled coding-CLI keepalive: config + per-provider `last_fired` (Settings → Agent CLI) |
| GET | `/api/browse?path=` | Directory listing `{path, parent, is_git, entries}` for the New-session dialog |
| POST | `/api/mkdir` | Body `{path, name}` (single path segment) → `{path}` |
| POST | `/api/paste-image?name=` | Save a pasted/dropped file for a session (transient retention) → its path |
| GET | `/api/logs` | Tail of the server log (the UI's System-logs pane, 3 s poll) |
| GET | `/api/addons` | Addon manifests `{addons: [{id, label, managed, frontend}]}` |
| GET | `/api/doctor` | Dependency preflight: git/tmux/agent-CLI/uv checks with per-platform fixes, plus `gh` reported as **optional** (status `info`, detail "not found (optional — only PR create/merge and PR review need it; pushing uses plain git)" — never `fail`, so it can't trip the required-dependency exit); cached ~30 s, `?refresh=1` re-probes. Also carries `version` (the running engine's version) and `state_notice` |
| POST | `/api/doctor/ack-state-notice` | Dismiss the downgrade notice; clears it and the cached payload |
| GET | `/api/mobile` | Mobile URLs + QR payload (Settings → Mobile) |
| GET | `/m` | The mobile UI page |

## Session events (WebSocket)

### `WS /api/events`

Live stream of the server-side session event bus (see
[docs/extensions.md](extensions.md)). Every frame is one JSON envelope:

```jsonc
{"seq": 42, "event": "session.status_changed", "session": "sc-19815",
 "old": "loading", "new": "running", "ts": 1719900000.0, "data": {}}
```

Events: `session.created|create_failed|deleted|paused|resumed|status_changed|
activity_changed|stage_changed|setup_started|setup_finished|check_started|
check_finished|budget_exceeded|budget_raised|prompt_sent|queue_changed|
usage_restored`
(plus addon-originated `addon.*`). `session.budget_exceeded` (J5) fires when a
session's estimated cost first crosses the configured
`general.session_budget_usd` — `data: {"cost": <float>, "budget": <float>}` —
once per session until the server restarts or the budget is changed.
`session.prompt_sent` fires when the drain loop auto-sends a queued prompt
(`data: {"text", "remaining", "loop"}`); `session.queue_changed` fires on any
queue edit (`data: {"pending", "enabled", "loop"}`). `session.usage_restored`
fires once per reopening of a provider's usage window — emitted by the same
drain-loop pass that nudges sessions parked on a limit screen to carry on
(`data: {"resumed": <bool>}`, false when `general.resume_on_usage_reset` is
off). It only ever fires for sessions that had actually run out, so it is the
"your usage is back" signal; running *out* is `session.activity_changed` with
`new == "limit"`. The notification-center
bell (frontend) curates these into a "what happened while I was away" feed.

On connect the server sends a **hello frame first** (L4): `seq: 0, event:
"hello"` with a `server_time` field (the server's clock), so clients can tell
replayed one-shot events (envelope `ts` < `server_time`) from live ones without
trusting their own clock — `mindflock.events.isReplay(env)` wraps this on the
frontend (L6). Then the ring-buffer backlog (~100 envelopes) is replayed;
`?since=<seq>` skips envelopes already seen, making reconnects lossless within
the buffer. Delivery is exactly-once per connection: an event emitted while the
backlog is being sent reaches the client from the backlog only, never twice
(seq-tracked). Clients only listen; a slow client loses events (bounded queue)
rather than blocking the server.

## Addon routes

**Ticket Ingestion** (addon id `mindflock`, prefix `/api/mindflock`) — controls the
pipeline as a managed subprocess (`python -m backend.ticket_ingestion` from the
repo root, own process group, singleton via `.mindflock-pipeline.lock`; also detects
and can stop an externally-started pipeline):

| Method | Path | Returns |
|---|---|---|
| GET | `/api/mindflock/status` | `{running, pid, since, log, available}` |
| POST | `/api/mindflock/start` | starts it (400 if no `config.toml`) |
| POST | `/api/mindflock/stop` | stops it (SIGTERM → SIGKILL of the process group) |
| WS | `/api/mindflock/logs` | read-only `tail -F` of `logs/ticket-ingestion.log` |

**Assistant** (addon id `assistant`, prefix `/api/assistant`) — one long-lived
`claude` tmux session in `~/.mindflock-assistant` (repo-independent), plus a todo
store:

| Method | Path | Returns |
|---|---|---|
| WS | `/api/assistant/terminal` | Interactive chat PTY |
| GET/PUT | `/api/assistant/instructions` | The assistant's standing instructions file |
| POST | `/api/assistant/restart` | Kill + relaunch the assistant tmux |
| GET | `/api/assistant/todos` | `{todos: [{id, text, done}]}` |
| PUT | `/api/assistant/todos` | Replace the full (reordered) todo array |

**Settings** (addon id `settings`, prefix `/api/settings`) — besides GET/POST
of the masked settings store (groups include `general`, e.g.
`general.session_budget_usd`, the J5 per-session cost guardrail; `0`/absent =
off), the account-attach "Test" validations (C5):

**The secret convention** (cross-cutting, not settings-only): every field the
server treats as a secret — API tokens, the ntfy access token — reads back as the
sentinel `•••set` when one is stored and `""` when none is, and a write of either
`""` **or** the sentinel *keeps* the saved value rather than clearing it. Only a
different non-empty string replaces a secret; clearing one is a deliberate
product decision per field (the ntfy token, for instance, is cleared by
retargeting the server, or explicitly with `{"clear_token": true}` — see Notify
below). The sentinel is one shared constant,
`SECRET_MASK` in `web/addons/base.py`, with a hand-mirrored counterpart in the
frontend's `settings/useSettings.tsx`; changing the string means changing both,
or the UI starts writing the literal mask into the store as a password.

| Method | Path | Returns |
|---|---|---|
| GET/POST | `/api/settings` | The masked settings store (secrets never echoed). POST **rejects** `coding_cli.default_provider` when that CLI is not installed (a `ValueError`-derived 400) — an absent CLI can never become the launch default |
| GET | `/api/settings/auth-token` | The active web-auth token (for the QR / copy button) |
| POST | `/api/settings/test/shortcut` | Validate a Shortcut token (body `{api_token}` or the stored one) → `{ok, member_id, name, mention_name}` for auto-fill, or `{ok: false, error}` |
| POST | `/api/settings/test/github` | `{ok, token_source: "settings·env·gh-cli·none", gh_installed, gh_authenticated, detail}` |
| POST | `/api/settings/test/agent` | Probe the configured agent CLI → `{ok, cli, auth}` (binary resolvable + login evidence) |
| POST | `/api/settings/test/local-model` | Probe a local model server (body `{runtime, base_url, model}`, each falling back to the stored value — so it can be tested *before* saving) → `{ok, runtime, base_url, models, error, supported_agents, default_base_urls}`. `models` turns the model field into a dropdown; `supported_agents` lists the installed CLIs that can actually be pointed at it (never `claude`) |
| GET | `/api/settings/providers/ticketing` | The ticketing-provider registry (fields per provider for the Settings form) |
| POST | `/api/settings/test/ticketing` | Validate the active ticketing connection |
| POST | `/api/settings/ticketing/states` | Live workflow-state list for a ticketing source |
| GET/PUT | `/api/settings/ticketing/sources` | The multi-source ticketing config (per-source provider/repo/state) |
| GET | `/api/providers/manage` | Custom coding-CLI providers (user TOMLs) for the Settings CRUD screen |
| POST/PUT/DELETE | `/api/providers` · `/api/providers/{name}` | Create / update / delete a custom provider TOML. The body may carry `launch_args` (a list of saved flag tokens) alongside `resume_flag`/`skip_perms_flag`/`trust_patterns`/…; it is validated (400 on invalid) and all string values are TOML-escaped via `json.dumps`, so quotes in names/flags/patterns can't corrupt the file. |
| GET | `/api/providers/status` | Per-provider connection status → `{providers: [{name, aliases, binary, installed, path, authenticated, auth_detail, auth_known, login_command, install_hint, is_default}], default}`. The catch-all `generic` provider is omitted. This is the source for the Settings → **Agent CLI** default-provider picker — it reads `installed`/`path` to list only installed CLIs and self-correct a missing default. The `authenticated`/`auth_detail`/`auth_known`/`login_command` fields are still returned but **no longer read by the UI** (sign-in is delegated to each CLI; see [providers.md](providers.md)). |
| WS | `/api/providers/{name}/login-terminal` | **Deprecated / unused by the UI.** PTY↔websocket bridge to a throwaway tmux session running the provider's login flow in `$HOME`. Still served, but no frontend surfaces the one-click login any more (each CLI prompts for sign-in itself). Closes 4500 with an `{type:"error"}` frame for an unknown provider or a spawn failure. |
| POST | `/api/providers/{name}/login-close` | **Deprecated / unused by the UI.** Best-effort teardown of a login session. Always `200 {ok: true}`. |

**Doctor** (addon id `doctor`) — `GET /api/doctor` (listed above). Beyond
`checks` and `ok`, the payload carries two fields that ride along because this
is the endpoint every client already talks to:

```jsonc
{
  "checks": [...], "ok": true,
  "version": "0.1.0",            // the running ENGINE's version
  "state_notice": null           // or {file_version, supported_version, backup_path}
}
```

`version` lets the desktop shell detect app/engine drift — it pins the engine
to its own version at install time but only installs when the engine is
*absent*, so an app-only update would otherwise leave an old engine running
indefinitely. Serving it over HTTP keeps one code path across macOS, Linux and
Windows/WSL.

`state_notice` is non-null only after this build refused to read a `state.json`
written by a **newer** MindFlock: `LoadState` preserves that file as
`state.json.newer-<ts>` and starts with an empty session list, so the UI needs
to explain why every session disappeared and where the file went. `POST
/api/doctor/ack-state-notice` dismisses it (server-side, so it stays dismissed
across reloads).

**Connections** (addon id `connections`) — `GET /api/connections?refresh=1`:
one-call status of every external integration (GitHub, active ticketing
provider, agent CLI, tailscale) for the Settings → Connections screen.

**Templates** (addon id `templates`, prefix `/api/templates`) — saved
new-session templates (`~/.mindflock/session_templates.json`):

| Method | Path | Returns |
|---|---|---|
| GET | `/api/templates` | `{templates: [...]}` |
| POST | `/api/templates` | Save/overwrite a template |
| DELETE | `/api/templates/{name}` | Remove a template |

**Notify** (addon id `notify`, prefix `/api/notify`) — the reference addon for
the generic extension path (see [docs/extensions.md](extensions.md)):

| Method | Path | Returns |
|---|---|---|
| GET | `/api/notify/config` | `{rules: [{id, label, event, old, new, title, body, enabled}]}` — the event → notification rules, applied client-side by `static/addons/notify.js` and server-side by the ntfy channel |
| POST | `/api/notify/rules/{rule_id}` | Enable/disable one rule (for **both** channels) |
| GET | `/api/notify/ntfy` | The ntfy channel's state: `{enabled, server, server_default, topic, has_token, click_url, configured, active, public_server, subscribe_url, qr_svg, suggested_topic, last}`. Never the token — only `has_token`; `suggested_topic` is a fresh random name, `last` is `{ts, ok, error}` of the most recent push |
| POST | `/api/notify/ntfy` | Save `{enabled?, server?, topic?, token?, click_url?, clear_token?}` (only the keys present are touched); returns the `GET` view, plus a `note` when something was rewritten. `clear_token: true` removes the saved token — the escape hatch from "empty = keep", and it wins over a `token` in the same payload. `400 {error}` on an invalid topic/server URL |
| POST | `/api/notify/ntfy/test` | Send one test push — `{ok, error}`. Takes its config from the body when supplied, so a topic can be verified before it is saved; exempt from the rate cap |

The ntfy channel is a **server-side** delivery path for the same rules
(`web/core/ntfy.py`): the server publishes to the topic over ntfy's JSON publish
API (POST to the server root, topic in the body — session titles are arbitrary
UTF-8, which an `X-Title` header could not carry), so an alert arrives with no
browser tab open. It is off until configured, resolves through
env → `settings.json` → defaults (`MINDFLOCK_NTFY_ENABLED` / `_SERVER` /
`_TOPIC` / `_TOKEN` / `_CLICK_URL`, where an env-supplied topic is an implicit
opt-in for headless boxes), and is capped at 60 pushes/hour per process.

Two write-path guards worth knowing: the token follows the store's secret
convention (empty or the `•••set` sentinel keeps the saved one) **and** is
dropped when the server URL is retargeted at a different host without a fresh
token, so server A's credential is never sent to server B; and a `token=` query
parameter in `click_url` is stripped, since that URL is stored on the ntfy
server.

Unlike the app's other secrets, this one can also be *removed*, with
`{"clear_token": true}`. The reason is specific to ntfy: the token is optional
(public topics need none), and a wrong token is strictly worse than no token —
ntfy answers a bad credential with `401 unauthorized` rather than ignoring it,
so a stray value breaks a publish that would have succeeded unauthenticated.
Without an explicit clear, "empty = keep" would make a mistyped token permanent
short of retargeting the server or hand-editing `settings.json`.

**Errors: branch on the body, not the status.** The two write endpoints report
failure differently on purpose. `POST /api/notify/ntfy` is a validating write, so
a bad topic or server URL is a `400 {error}` and nothing is saved.
`POST /api/notify/ntfy/test` is a *probe* — every outcome it produces itself is a
`200` with `{ok, error}`, including an invalid config and a send that failed
outright (DNS, TLS, timeout, a `403` from ntfy); only a malformed request body gets
a status error, from FastAPI's own validation. A client that branches on HTTP
status therefore reads every failed test as a success: branch on `ok` and display
`error`, which carries the ntfy server's own error sentence when it sent one.

**Rate cap** — pushes are capped at **60 per rolling hour, per server process**,
shared across every session and every rule (not per-session, not per-rule). It is
a runaway guard, not a tunable: no env var or setting changes it. `POST
/api/notify/ntfy/test` is **exempt**, so a test still reports a true verdict while
the event channel is being throttled.

A throttled push is **not** observable over the API. The cap is checked before the
HTTP attempt and returns `"Rate limit: too many ntfy pushes this hour"` to its
caller, but the event path is fire-and-forget and discards that return value, and
the drop is deliberately *not* recorded as a `last` result — so `last` keeps
showing the last real attempt rather than being buried under throttle noise. The
only trace is one server-log line per window (`ntfy: over 60 pushes/hour …`). If a
client needs to explain missing pushes, that log line is the evidence; `last: {ok:
true}` alongside silent phones is the symptom.

**Outbound reach** — `POST /api/notify/ntfy/test` publishes to the `server` in the
*request body*, so an authenticated caller can make the MindFlock process issue
one outbound `http(s)` POST (with a JSON body they largely control) to a host of
their choosing, and — when that host answers `4xx`/`5xx` — read back the first
~200 characters of its response body, surfaced as `error`. That is inherent to
"test before you save", and it is bounded (one call, 10 s total timeout, nothing
echoed back on a `2xx`), but a request-forgery probe is a fair way to describe it.
Hence two things worth not undoing: this endpoint takes the same auth middleware as
everything else, and the gate switches itself on for any non-local `CS_WEB_MODE`
(see [Authentication](#authentication)). An exposed server with `MINDFLOCK_AUTH=0`
would hand this probe to the network.

## Authentication

A single shared bearer token gates the whole server — HTTP routes and
websockets alike — via one ASGI middleware (`web/core/auth.py`).

- **On when exposed.** Enabled when `CS_WEB_MODE` is a non-local mode (e.g.
  tailscale — an explicit opt-in; `run.py` defaults to local), OR
  `MINDFLOCK_AUTH_TOKEN` is set, OR `MINDFLOCK_AUTH=1`. Off for a plain
  localhost run, a bare `uvicorn`, and the test suite (all leave `CS_WEB_MODE`
  local/unset). `MINDFLOCK_AUTH=0` forces off. A *persisted* token never flips
  the gate on by itself.
- **Token.** `MINDFLOCK_AUTH_TOKEN` env → `general.auth_token` setting →
  auto-generated + persisted on first exposed start. Printed in the startup
  banner and baked into the `/m?token=…` QR so a phone lands signed in.
- **Proving it.** An `mf_auth` cookie, `Authorization: Bearer <token>`, or
  `?token=` (which redirects to set the cookie and strip the token from the
  URL). A browser navigation without a token gets a tiny inline login page; an
  API call gets `401`; a websocket is closed with code **4401** (the SPA/mobile
  head reload to the login page on 401/4401).

Independent of the token gate — enforced even when it's off — the middleware
refuses browser cross-origin requests and DNS-rebinding hosts. These checks
run **before everything else**, public paths included: a cross-site
`POST /api/auth` is refused too.

- **Origin check (all modes).** A request carrying an `Origin` header whose
  host is neither loopback nor the request's own `Host` is refused (HTTP 403 /
  WS close **4403**). WebSocket handshakes ignore CORS, so this is what stops
  a malicious webpage from opening `ws://127.0.0.1:8765/...` and driving the
  agent terminals. Non-browser clients (curl, the CLI, other MindFlock
  servers) send no `Origin` and are unaffected.
- **Host check (local mode).** With `CS_WEB_MODE=local` only loopback `Host`
  headers are answered — a public domain rebound to 127.0.0.1 gets 403.

| Method | Path | Behavior |
|---|---|---|
| POST | `/api/auth` | Body `{token}` — validate + set the `mf_auth` cookie (login-page target; always allowed through the gate). `200 {ok}` or `401`; never echoes the token |
| GET | `/api/settings/auth-token` | This device's token in the clear (behind the gate) for Settings → Security |
| POST | `/api/settings/auth-token/rotate` | Mint + persist a NEW token (compromise recovery): every issued cookie/QR/paired device is invalidated; the response re-issues the caller's cookie. `409` when `MINDFLOCK_AUTH_TOKEN` pins the token; `500` when persisting the new token fails (the old token stays valid) |

## Server lifecycle

Startup (`lifespan`): a 4 s reload loop adopts sessions created by other
processes; the Cursor auto-adopt loop starts (disable initial state with
`CS_CURSOR_AUTOADOPT=0`); the prompt-queue drain loop starts (feeds queued
prompts to idle agents); the persisted scroll speed is applied; a banner with
the local + tailnet mobile URLs (the access token + a QR code if `segno` is
installed) is printed; each addon's `on_startup` runs. Shutdown reverses addon
hooks and cancels background tasks. tmux sessions are *not* touched — they
outlive the server.

## Launching the server

```bash
mindflock serve [local|tailscale] [--port N]   # default local (127.0.0.1)
./backend/web/run.sh [tailscale|local]   # same, from a source checkout; PORT=… to override
python -m backend.web.run [local|tailscale] [port]
```

The desktop app (see `electron/README.md`) auto-starts the server itself, so
manual launching is a headless/dev concern. `run.py` accepts mode/port CLI
tokens in either order and honors `CS_WEB_MODE`,
`PORT`/`UVICORN_PORT`. The default (local) mode binds `127.0.0.1` — nothing
off the machine can reach the server. Tailscale mode is an explicit opt-in
that binds all interfaces (`0.0.0.0`), so the port is reachable from your LAN
as well as your tailnet — every non-local bind is protected by the auth token
printed at startup (unauthenticated clients get 401). Nothing is exposed to
the public internet unless you forward the port yourself. For HTTPS run
`tailscale serve --bg 8765` once and use `./run.sh local`.
