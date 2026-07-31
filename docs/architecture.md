# Architecture

MindFlock is three cooperating layers over one core idea: **a session = a git
workspace + a tmux session running a coding agent**, all observable and steerable
from a browser.

```
┌─────────────────────────────────────────────────────────────────────────┐
│ Clients                                                                 │
│   browser SPA (static/app.js) · mobile UI (/m) · Electron desktop app   │
│   mindflock CLI (serve/doctor + new/ls/attach/rm/open/events)           │
└──────────────┬────────────────────────────────────────┬─────────────────┘
               │ REST (poll /api/instances every 4s)    │ WebSockets (PTY bytes,
               │ + bearer-token auth (core/auth.py)     │  /api/events bus)
┌──────────────▼────────────────────────────────────────▼─────────────────┐
│ Web server (backend.web.server — FastAPI)                             │
│   routes · stage detection (git_ops) · terminal bridge (pump_pty)       │
│   event bus (core/events.py) · prompt queue · PR auto-review            │
│   remote-device proxy (core/remote.py, tailnet)                         │
│   addons: mindflock (pipeline) · assistant · settings · doctor ·        │
│           connections · templates · notify                              │
└──────────────┬───────────────────────────────────────────────────────────┘
               │ in-process (core/engine.py singleton)
┌──────────────▼───────────────────────────────────────────────────────────┐
│ Session engine (backend.session / config / cmd / log / providers)      │
│   Instance lifecycle · git worktrees · tmux+PTY · workspace provisioning │
│   provider resolution (claude · codex · antigravity · aider · opencode · │
│   cline · goose · user TOMLs)                                            │
└──────────────┬───────────────────────────────────────────────────────────┘
               │ shells out to
        git · tmux · uv · agent CLIs · IDEs (cursor/code/…) · gh (optional)
```

The **ingestion pipeline** (`backend.ticket_ingestion`) is a separate process that
feeds the same engine state: it polls the configured ticketing provider
(Jira, Linear, GitHub Issues, Shortcut, or Asana) and GitHub PRs and creates
sessions, which the web server's 4-second reload loop then adopts into the grid.

## Components

### Session engine (`backend/session`, `config`, `cmd`, `log`)

Responsibilities:

- **`Instance`** (`session/instance.py`) — one session's full lifecycle:
  `Start` → running; `Pause` (remove worktree, keep branch); `Resume`
  (recreate worktree, re-provision); `Kill` (tear down tmux + worktree).
- **Git worktrees** (`session/git/`) — worktree creation under
  `~/.mindflock/worktrees/`, branch naming, diff-vs-base computation, commit/push.
- **tmux** (`session/tmux/`) — every session is a tmux session named
  `mindflock_<sanitized-title>`, attached through a PTY (`ptyprocess`).
- **Provisioned mode** (`session/provisioned.py`) — opt-in heavyweight provisioning
  for the configured repo **or any local repo**: canonical per-repo base clone
  (`_base_<slug>`), `uv sync`, pre-commit install,
  warm testmon seed, launcher script with crash-resume. See
  [session-engine.md](session-engine.md).
- **Config/state** (`config/`) — `~/.mindflock/config.json` + `state.json`
  (instances are embedded in `state.json`).
- **`cmd`** — a Go-style command executor (`run()` returns an error or `None`),
  mockable in tests. **`log`** — Go-style logger writing `{tempdir}/mindflock.log`.

### Providers (`backend/providers`)

Everything CLI-specific — launch command, resume flags, trust-prompt detection,
idle/waiting classification, token telemetry — lives behind a provider
interface so the engine and web server can't drift. `claude` is the default;
`codex`, `antigravity`, `aider`, `opencode`, `cline`, and `goose` are bundled
(codex/antigravity with dedicated subclasses for live usage telemetry, the rest
as data-only configs); new CLIs can be added with a TOML file, zero Python.
Also home to `pricing.py` (model $ rates, cached daily), `usage_history.py`
(rolling day/week/month/year token+cost totals scanned from agent transcripts),
`usage_limits.py`, and the per-provider live usage-window clients
(`claude_usage_api.py`, `codex_usage_api.py`, `antigravity_usage_api.py`).
See [providers.md](providers.md).

### Web server (`backend/web`)

FastAPI app `backend.web.server:app`. Key pieces:

- **`core/engine.py`** — process-wide `Engine` singleton wrapping config + state +
  the in-memory instance map. `save()` merges with on-disk state so it never
  clobbers sessions created by another process (the pipeline). A 4s reload loop
  adopts externally-created sessions with `attach=False` (no server-side PTY until
  a browser connects); the loop re-reads/parses `state.json` only when its
  `(mtime_ns, size)` signature changes, while deletion tombstones are still
  applied to in-memory instances every tick.
- **`core/terminal.py`** — the one PTY↔WebSocket bridge (`pump_pty`), tmux attach
  spawning, exit-marker bookkeeping (distinguishes clean quit from crash so a
  restart can `--continue`), and the tmux scroll-speed binding.
- **`core/git_ops.py`** — pure git queries (dirty? commits beyond base? upstream?
  origin SHA?) used to compute each session's **workflow stage**:
  `provisioning → agent → pre-commit → committed → pushed → PR open → merged`
  (+ `pre-commit ✗` on hook failure).
- **`core/events.py`** — the server-side session event bus: `session.*` events
  broadcast over `WS /api/events`, delivered to in-process addons via
  `AppContext.subscribe`, and to user shell hooks under `~/.mindflock/hooks/`.
  Note the asymmetry: websocket clients and shell hooks are decoupled from the
  emitter, but `subscribe` callbacks run **synchronously on the emitting thread**,
  so every subscriber is on `emit()`'s critical path and must neither block nor
  raise. Anything slow gets offloaded — `ntfy.publish_soon` is the reference
  pattern, handing its HTTP call to the server loop via the same trampoline
  `EventBus._dispatch_hooks` uses for shell hooks.
- **`core/ntfy.py`** — the optional [ntfy](https://ntfy.sh) push channel: the
  transport (JSON publish, rate cap, last-result reporting) behind the notify
  addon's server-side bus subscriber, so a "needs your input" reaches a phone
  with no browser tab open. Off until configured; see
  [web-ui.md](web-ui.md#notifications-).
- **`core/auth.py`** — shared bearer-token ASGI middleware gating HTTP + websockets
  whenever the server is exposed beyond localhost (cookie / header / `?token=`,
  QR deep-link for the mobile page).
- **`core/remote.py`** — tailnet multi-device control: discovers other MindFlock
  servers via `tailscale status`, namespaces their sessions `<device>::<title>`,
  and proxies every per-session route to the owning device (gated by the
  `general.remote_control` setting, tokens in `~/.mindflock/remote_devices.json`).
- **`core/prompt_queue.py`** — per-session FIFO of prompts drained into idle
  agents by a background loop (self-driving runs; state in
  `~/.mindflock/prompt_queues.json`).
- **`core/pr_review.py`** — the forced-PR-review path behind Settings → PR
  review: lists open PRs (with why auto-review did/didn't take them) and can
  start a review session in-process.
- **`_cached_fanout` (in `server.py`)** — the one place stale-while-revalidate
  caching lives, shared by the three settings-panel routes (`/api/tickets`,
  `/api/github/prs`, `/api/github/issues`). Each is an upstream fan-out the
  panels poll while open, so a payload is fresh for `_FANOUT_TTL` (20 s), then
  served *stale* for up to `_FANOUT_MAX_STALE` (5 min) with `stale: true` while
  `_schedule_fanout_refresh` sweeps in a single-flight background task — the
  request never waits on GitHub/the ticket sources, and a failed sweep leaves
  the last known list in place instead of emptying the panel. Two guards keep a
  sick upstream from being hit hardest: a failure starts a
  `_FANOUT_ERROR_BACKOFF` (30 s) during which reads still report `stale` but arm
  no sweep — otherwise the client's re-poll would re-arm one every couple of
  seconds for the whole stale window — and each sweep is bounded by
  `_FANOUT_SWEEP_TIMEOUT` (60 s), so a hung `git ls-remote` can't hold the
  single-flight slot and freeze that panel's list. Remaining sharp edge: the
  tasks are untracked at lifespan shutdown (fire-and-forget). See
  [web-api.md](web-api.md#assigned-tickets-pr-auto-review--issue-handling).
- **The rest of `core/`** — `server.py` keeps only the app assembly, the
  routes, and the always-on background loops; every other helper cluster is a
  focused module: `agent_sessions` (tmux ensure/send/kill for the agent+shell
  panes), `agent_state` (working/clarify/idle/offline detection), `snapshot`
  (per-session JSON descriptors + diff stat), `session_stats` (token/cost
  telemetry + transcript history), `budget` (cost guardrail + input lock),
  `usage_api` (/api/usage provider descriptors), `mobile_access` (tailnet
  URLs/QR/banner), `plain_repo` (base-folder selection), `workspaces` (roots,
  classification, guarded deletion), `recently_closed` (reopen/Ctrl+Z store),
  `uploads` (paste retention), `system_logs` (log tails), `cursor_windows`,
  `ide_launch`, `ports`, `window_refresh`, `worktree_setup`. Two load-bearing
  conventions keep this decomposition black-box-equivalent — hold them when
  extracting more: **(1) no routes in `core/`** — every `APIRouter`/`@app`
  handler stays in `server.py`; the modules are pure helpers. **(2) re-import
  for the monkeypatch seam** — a core module calls back through the server
  namespace (`_server()._foo(...)`) for anything tests monkeypatch, and
  `server.py` re-imports the module's names, so
  `monkeypatch.setattr(server, "_foo", …)` works no matter where `_foo` lives.
- **Startup `PATH` enrichment** (`backend.pathenv`) — the server `lifespan`
  runs `pathenv.ensure_enriched()` (on a worker thread) **once before serving
  any request**. A GUI-launched backend (Electron, a `.desktop` file,
  `launchd`/systemd) inherits a minimal `PATH` — it never sources the user's
  shell profile — so install detection (Settings → Agent CLI) and every CLI the
  server later spawns would miss tools that work fine in the terminal. The fix
  probes the user's login+interactive shell (`$SHELL -ilc env`, 4 s timeout,
  delimiter-framed parse) and unions its `PATH` plus well-known per-user bin dirs
  (`~/.local/bin`, `~/.cargo/bin`, Homebrew, nvm/asdf shims, …) into
  `os.environ['PATH']`. It is idempotent, `lru_cache`d, never raises, and only
  *adds* directories (existing entries keep priority), guarded by
  `MINDFLOCK_NO_PATH_ENRICH` (disable) and the internal `MINDFLOCK_PATH_PROBE`
  reentrancy sentinel. Because it mutates the process environment, the enriched
  `PATH` is inherited by **every** downstream subprocess — tmux, provider CLIs,
  git, `gh`, node. It generalises the same "works in the terminal, not in the
  GUI app" workaround `core/ide_launch` already applies to editor CLI shims.
- **Addons** (`web/addons/`) — self-registering feature modules with their own
  `APIRouter` and optional managed subprocess. Seven ship today: **mindflock**
  (start/stop/tail the ingestion pipeline), **assistant** (a long-lived
  repo-independent Claude chat + todo list), **settings**, **doctor**,
  **connections**, **templates**, and **notify** (the reference addon for the
  extension path — see [extensions.md](extensions.md)).

### Frontend (`backend/web/static`)

A React + TypeScript SPA (source in `frontend/`, built with Vite into
`static/app.js` with stable names). It renders a draggable terminal grid
(1/2/4/9/auto view modes), a sidebar with per-session actions and stage chips,
Agent/Terminal/Diff tabs per pane, guided next-step buttons, token/cost popups,
voice input, and a settings dialog. `core/ws-xterm.js` is the shared
xterm↔WebSocket wiring; `mobile.*` is a single-terminal phone UI at `/m`.
See [web-ui.md](web-ui.md).

`frontend/src/state/queries.ts` is the shared server-state layer (TanStack
Query): every hook that reads the HTTP API lives there, including the
settings-panel fan-outs (`usePanelQuery`, the `PANELS` map, and
`prefetchSettingsPanels`, called when the dialog opens). Those live in the query
client *precisely because* the settings dialog and each screen unmount on
close/switch — component state meant every visit paid the full upstream sweep
again. Those queries raise `gcTime` to an hour (`PANEL_GC_MS`): on
TanStack's 5-minute default, "cached across dialog opens" would quietly become a
cold load again after a short break, which is when the wait feels worst.

### Ingestion pipeline (`backend/ticket_ingestion`)

`python -m backend.ticket_ingestion`, configured by `./config.toml`, singleton
per directory via `.mindflock-pipeline.lock`. Despite the historical package
name it is multi-provider: tickets can come from **Jira, Linear, Shortcut,
GitHub Issues, or Asana** (`ticket_ingestion/providers/`). Two loops:

- **Tickets** — poll the configured provider(s) for tickets assigned to you
  (updated since the last run), validate them, and launch an agent session
  seeded with the ticket (branch `feature/<slug>/<name>`, slug prefixes
  `sc-`/`jira-`/`lin-`/`gh-`/`asana-` per source).
- **PRs** — poll GitHub for your open PRs with unresolved review comments and
  launch one consolidated session per PR that addresses all comments (changes left
  unstaged for human review).

Plus a **testmon refresher** that keeps a warm `.testmondata` seed so provisioned
workspaces only run diff-impacted tests, and a startup **workspace cleanup** that
prunes workspaces untouched for 3 days. See
[ingestion-pipeline.md](ingestion-pipeline.md).

### CLI (`backend/cli.py`)

The `mindflock` console entry point: host commands (`serve`, `doctor [--fix]`)
plus session commands (`new`, `ls`, `attach`, `rm`, `open`, `events`) that are
thin clients over a running server's HTTP API — the terminal and the browser
drive the same server. `doctor` (`backend/doctor.py`, also surfaced as a
web addon) preflights git/tmux/agent-CLI and can install missing deps. `gh` is
preflighted too but reported as *optional* (`info`, never `fail`): it is not on
any required path — pushing is plain `git push`.
See [cli.md](cli.md).

### Desktop app (`electron/`)

An Electron shell that auto-starts the server (through WSL on Windows) and
loads the web UI in a native window, with an offline/diagnostics page while the
server boots. Window chrome is platform-conditional — frameless with injected
– □ ✕ on Windows/Linux, `titleBarStyle: 'hidden'` with the OS traffic lights on
macOS — and the UI adapts to it through **capability flags on the preload
bridge** (`mfshell.nativeTitleBar`, read via `frontend/src/lib/shell.ts`) rather
than platform sniffing: the shell and the engine that serves the UI ship on
independent cadences, so each side has to tolerate the other being older. See
`electron/README.md`.

## Data flow: one story, end to end

1. Pipeline poll finds ticket 19815 (a Shortcut story here; Jira/Linear/GitHub
   Issues/Asana work the same) assigned to you; no `feature/sc-19815/*` branch
   exists on the remote; not in `state.json`.
2. Validation passes (description length, acceptance criteria present) — otherwise
   a *clarification session* is launched instead.
3. A workspace is provisioned (worktree off `workspaces/_base_<repo-slug>`, or a
   standalone clone) with deps synced, pre-commit installed, testmon seeded.
4. A tmux session `mindflock_sc-19815` starts Claude seeded with the full ticket
   (description, acceptance criteria, comments, downloaded attachments).
5. The web server's reload loop adopts the session; it appears in the grid within
   ~4 seconds with a stage badge. You watch/steer it from the browser or phone.
6. The guided flow walks it forward: **Commit…** (runs pre-commit in the visible
   Terminal tab) → **Push** (plain `git push` over the repo's own remote, SSH or
   HTTPS) → **Make PR** → **Merge** (`gh` when available, else the GitHub REST
   API with a token, else a prefilled browser URL). Stage detection is inferred
   from git plus whichever GitHub credential exists, so commits made in Cursor
   also move the badge.

## On-disk state map

| Path | Owner | Contents |
|---|---|---|
| `~/.mindflock/config.json` | engine | `default_program`, `branch_prefix`, profiles |
| `~/.mindflock/state.json` | engine | serialized instances + help-screen bitmask |
| `~/.mindflock/worktrees/` | engine | worktree-mode session directories |
| `~/.mindflock/recently_closed.json` | web | reopenable closed sessions (cap 50) |
| `~/.mindflock/prompt_queues.json` | web | per-session prompt queues (items + loop/enabled flags) |
| `~/.mindflock/session_templates.json` | web (templates addon) | saved new-session templates |
| `~/.mindflock/remote_devices.json` | web (remote) | paired tailnet devices + tokens |
| `~/.mindflock/settings.json` | web (settings addon) | the web settings store |
| `~/.mindflock/hooks/` | user | shell hooks run on session events ([extensions.md](extensions.md)) |
| `~/.mindflock-assistant/` | providers/web | assistant instructions + todos, `providers/*.toml`, `pricing.json`, `usage-history.json`, `scroll-speed`, `.exit-markers/`, `prompts/` |
| `./config.toml` | pipeline + provisioning | all configuration (gitignored — holds tokens) |
| `./state.json` | pipeline | `last_run_timestamp`, `processed_stories`, `processed_prs` |
| `./.mindflock-pipeline.lock` | pipeline | singleton flock, holds winner PID |
| `./workspaces/` | pipeline/engine | story/PR workspaces, `_base_*`, `_testmon_refresher` |
| `./logs/` | pipeline/web | `pipeline.log` (pipeline logger), `ticket-ingestion.log` (subprocess stdout) |
| `{tempdir}/mindflock.log` | engine `log` | low-level engine log |
| per-workspace `.mindflock_*` files | engine/web | launcher, prompt, commit msg/status, pre-commit lock (git-excluded) |

> **Two `state.json` files.** The repo-root `state.json` is pipeline dedup state.
> The engine's session state is `~/.mindflock/state.json`. Same filename, unrelated
> schemas.

## Process model

- **Web server** — one uvicorn process; terminals are `tmux attach` PTYs bridged
  over WebSockets, so sessions survive server restarts (tmux is the source of
  truth).
- **Pipeline** — one process per directory (flock-enforced); can run standalone or
  as a child of the web server (Ticket Ingestion addon), in its own process
  group so stop kills the whole tree.
- **Agents** — tmux sessions, independent of both. Launchers write an exit-code
  marker on exit; an unnatural exit (not 0/130) makes the next start resume the
  conversation with `--continue`.
