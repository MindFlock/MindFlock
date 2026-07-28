# Web UI guide

The frontend is a React + TypeScript app. Source lives in `frontend/` (Vite;
`npm run dev` proxies to the FastAPI server, `npm run build` emits committed,
unminified `app.js` / `style.css` / `index.html` into
`backend/web/static/`). The addon runtime (`core/events.js`,
`core/slots.js`, `core/ws-xterm.js`), the `/m` mobile page (`mobile.*`), and
`theme.css` stay outside the bundle as plain scripts. See
`frontend/README.md` and `backend/web/static/README.md` for the layout
and the contracts to preserve.

## Layout

**Sidebar** — session list with drag-to-reorder, status dots, stage chips, and a
kill ✕ per row (ends the session, keeps the worktree). Long titles ellipsize
(full name on hover); a session cut from a different repo than the one the
server manages shows a compact `⇄` glyph whose tooltip carries the repo name and
full path. Under each row title a muted **context line** shows the session's
total diff stat at a glance — `+120 −8 · 6 files`: everything the session has
produced vs its base branch, committed + uncommitted, untracked files included
(green/red tints; hidden when there are no changes or the backend doesn't
report `diff_stat`; the tooltip splits out the uncommitted slice); the same
line appears in the pane header while the pane is wide enough. Above the list:
**+ New** and a stack of **customizable bars** (see below) — the token/cost
**Usage** readout, the **Assistant** bar (Chat, Todo), the **Ticket Ingestion**
bar (pipeline on/off switch, state dot, Logs pane), the **PR Review** and
**Issue Handling** bars, and one auto-rendered bar per generic addon — e.g.
**Notifications** with its On/Off toggle (disabled with an explanatory tooltip
on plain-http origins, "Blocked" when the browser denies permission).
Below: view-mode buttons (Auto/1/2/4/9), **Recent…** (reopen closed sessions),
**Disk…** (workspace manager), session count, a **Command** button (opens the
command palette), a **⚙ Customize** button (sidebar bars — see below), settings
⚙, and the theme toggle 🌙. The workspace manager
lists each managed workspace with its size and a per-row delete, plus a **Clear**
button that bulk-removes every unprotected, idle workspace in one sweep
(`POST /api/workspaces/clear`) — protected base clones / cache refreshers and any
dir a live session is using are left alone.

**Customizable bars** — the sidebar bars are movable and hideable, driven by a
shared registry (`sidebar/barDefs.ts`: Usage, Ticket Ingestion, PR Review, Issue
Handling, Assistant). A **fresh install starts minimal** — only **Usage** and
**Assistant** show, so a first run isn't overwhelming; the rest start hidden.
The footer **⚙ Customize** popover (`FooterCustomize`) toggles each bar on/off,
and bars drag-to-reorder (the session list is a fixed anchor bars can sit above
or below, but which never itself moves). Order and the hidden set persist per
browser (`localStorage`). Turning a feature's bar on is how you reveal PR
review, ticket ingestion, and issue handling once you've connected them.

**Command palette** — `Ctrl+P` or `Ctrl+Shift+P` (`Cmd` on Mac, or the footer
Command button) opens a fuzzy-filtered palette over everything: jump to
("Focus:") any session, New session, Commit / Push / Create PR / Open in IDE on
the focused session, Open Settings / Doctor / Setup checklist, Toggle sidebar,
and New from Recently closed. Type to filter (subsequence match), `↑`/`↓` to
select, `Enter` to run, `Esc` to close. Both bindings work from anywhere,
including while a terminal has keyboard focus — the same VSCode trade-off as
its quick-open/palette keys.

**Grid** — terminal panes in a draggable grid (grip `⠿` to rearrange; layout is
persisted). The view mode caps how many panes are visible; the most recently used
sessions win a slot and the rest keep running hidden. A session still
provisioning shows a placeholder pane ("the first provisioned run clones the
base repo…") until its terminal is live.

**Missing workspace** — a session whose workspace directory vanished (wiped
outside MindFlock; the backend flags it `workspace_missing`) renders as a
muted/hollow sidebar row with a **missing** chip (the pane placeholder tooltip
still spells out "workspace directory no longer exists") and a single **Clean
up** action (also on its pane placeholder), which removes the dead session via
the existing DELETE. Diff and context line are suppressed. Clean up is
**optimistic**: the row and pane drop the instant you click (single row or the
bulk **Clean up missing**), while the DELETE — tmux kill + worktree GC — runs
behind them; the 4 s poll suppresses still-listed pending sessions so they
aren't resurrected mid-delete, and a failed DELETE restores the row with a
toast.

**No origin remote** — when a `committed` session's repo has no `origin`
(backend `has_origin: false`), the pane's next-step button becomes a
non-destructive "No remote — add origin…" hint: the tooltip shows the
`git remote add origin <url>` command and clicking copies it. Push errors from
the API (including the friendly 400 in this case) surface as toasts.

**Per-pane tabs**

- **Agent** — the coding agent's terminal (WebSocket → tmux).
- **Terminal** — a real shell in the workspace (separate tmux, lazy-created).
  Commit runs here so you watch pre-commit hooks live.
- **Diff** — the session's changes, grouped per file with collapsible
  sections; clicking a file loads its whole-file diff. Two baselines (toggle
  persisted): **All changes** (default) diffs against the fork point from the
  base branch — committed + uncommitted, the same total as the header badge —
  and **Uncommitted** shows only working-tree changes since the last commit.
  Split/unified view toggle is persisted too.

Terminals keep tmux mouse scrolling (speed configurable in settings);
**Shift+drag** (Alt+drag on macOS) selects text and auto-copies it.

## Workflow stages and the guided next step

Each session shows a stage badge (sidebar + pane) computed from git/`gh`:

```
provisioning → agent → pre-commit → committed → pushed → PR open → merged
                          └─ pre-commit ✗ (hooks blocked the commit)
```

The pane shows a single **guided next-step button**:

| Stage | Button | What it does |
|---|---|---|
| agent | **Commit…** | Prompts for a message; runs `git commit` in the Terminal tab (pre-commit hooks visible; auto-fix retries up to 5×) |
| pre-commit ✗ | **Re-commit** | Restages the hook auto-fixes and retries with the same message |
| committed | **Push** | `git push --no-verify -u origin HEAD` (hooks already ran) |
| pushed | **Make PR** | `gh pr create --base <staging> --fill` |
| PR open | **Merge** | `gh pr merge --merge` (confirmed) |
| merged | **Open PR ↗** | Opens the PR page |

Stages are detected best-effort from git state, so a commit made in Cursor also
advances the badge within a few seconds. Live agent **activity** overlays the
stage chip: `running`, `clarify` (the agent is asking you something), `idle`,
`offline`, `paused` — detected from the CLI's own activity hooks where
available, with CPU/pane-hash fallback (see [providers.md](providers.md)).

## Session row actions (expand a sidebar row with ›)

Copy path · **Commit…** · **Push** · **Make PR** · **Merge to staging** ·
**Open PR ↗** · **Copy window** (a second in-place session on the same worktree) ·
**Open/focus Cursor** (row double-click does the same) · **Hide/Show window**
(session keeps running) · **Pause/Resume** · **Delete + wipe worktree** (confirmed).

## Send a message / prompt queue (✉ per pane)

Each session pane header has an **✉** button (a badge shows the pending-queue
count). It opens a popover with two ways to drive the agent:

- **Send now** types a message into the agent window and submits it — booting or
  resuming the session first if it isn't running, so one line kicks a fresh
  session into motion (maximizing token use). Independent of the Shortcut
  pipeline.
- **Add to queue** appends to a FIFO the server drains into the agent whenever
  it's idle, so a run keeps going unattended and **resumes on its own the moment
  usage returns** after an outage (the drain reboots a session whose agent CLI
  exited on a usage limit). **Loop** re-queues each sent prompt so a single
  self-improving prompt ("keep improving the repo") cycles forever; **Auto-run**
  toggles draining. Items reorder (↑/↓), edit (✎), delete, or **send now (▶)** —
  ▶ delivers that one queued item immediately, skipping the idle wait and any
  usage-limit hold; **Clear all** empties it.

While the agent is usage-limited, **Wait out usage limits** holds the queue and
auto-resumes when the window reopens. The hold is anchored to the provider's own
usage meter when available (for Claude, the same data as the CLI's `/usage`
screen — 5-hour *and* weekly windows), falling back to the reset time parsed
from the CLI's banner; if the meter shows the window reopened early, the queue
resumes immediately. The meter is consulted **even when no limit banner is on
the pane** — so a session that runs out mid-turn, exits, and reboots to a fresh
idle prompt (no banner) still arms a hold straight from the meter rather than
firing the next queued prompt into the wall. A window that reads *spent* but
carries **no usable reset time** (a null/absent/past reset) holds on a bounded
fallback instead of sending. A meter that reads *open* — or is *unavailable* —
leaves the queue free to send, so healthy sessions are never over-held.

The command palette has **Send message…** and **Queue prompt…** for the focused
session (keyboard-only via a prompt).

## Notifications (🔔)

The header bell keeps a running feed of notable session events — finished/idle,
needs-input, stage changes, cost-over-budget, auto-sent queued prompts — fed by
the events bus, **including the backlog replayed on connect** so it answers
"what happened while I was away." An unread badge counts events since you last
opened it (keyed on timestamp so it survives server restarts); clicking an entry
focuses that session.

## Diff → instruction

Select any text in a pane's **Diff** tab and a floating **✦ Ask agent** button
appears. It sends a one-line message (the file + a short excerpt + your note)
into that session's agent and flips the pane to the Agent tab — acting on a
review finding without leaving the diff.

## Bulk session actions

Each sidebar row has a checkbox; ticking any shows a batch bar: **End** (keep
worktrees), **Hide**, **Clean up missing** (rows whose workspace vanished),
**Delete + wipe** (confirmed), **Clear**. Actions fan out over the per-session
endpoints and tolerate individual failures.

## Signing in

When the server is exposed beyond localhost (tailscale mode) it requires the
access token from the startup banner: a browser hits a small login page, or scan
the banner QR (`/m?token=…`) to land signed in. See the API doc's
Authentication section. If the token leaks, regenerate it from Settings →
**Security** (below).

## Finding, renaming & attention

- **Filter** — once you have a handful of sessions, a filter box appears above
  the sidebar list (`/` focuses it, Esc clears). It matches title, alias,
  branch, and repo, and only narrows the sidebar — the grid is untouched. It
  stays hidden while you have few sessions, so newcomers never see it.
- **Rename** — give a session a friendly display label (row menu → *Rename…*,
  or the palette). It's a client-side alias shown everywhere the title appears
  (italic, real title in the tooltip); the underlying session/tmux/worktree are
  never renamed, so nothing can break.
- **Attention** — sessions waiting on input badge the tab **title** (`● (n)`)
  *and* the **favicon** (a red dot), so a backgrounded tab is noticeable. The
  🔔 bell keeps the durable list.
- **Undo** — reversible actions (hide, bulk hide) show a *click to undo* toast
  instead of a modal; only truly destructive wipes still confirm.
- **Hidden sessions stick** — a session you hide stays hidden across reloads
  (persisted per browser); showing it again clears that. Expanded action menus
  are transient (a reload starts them collapsed).

## Keyboard shortcuts

Bindings follow VSCode wherever one exists. The desktop (Electron) app can
claim combos browsers reserve (`Ctrl+W`, `Ctrl+N`, `Ctrl+Tab`, `Ctrl+1…9`,
`Ctrl+Shift+T`); in a plain browser use the `Alt+…` / `Delete` aliases
instead. `Ctrl` means `Cmd` on Mac throughout. Press `?` in the app for the
live cheat-sheet — it is generated from the same table that drives the
bindings.

The sheet is also the editor: rebindable rows highlight on hover — click one,
press the new combo (`Esc` cancels), and it takes effect immediately. The
row's `+` button adds an *extra* combo instead of replacing (starting from
the defaults, so an action can answer to several combos). Chord rows rebind
their second key (the `Ctrl+K` prefix is fixed). A `↺` next to a customized
shortcut restores that one default; **Restore defaults** in the sheet header
clears every customization. Overrides are saved per browser/device (in
localStorage), since a Mac laptop and a Windows desktop usually want
different combos. Combos already in use are refused, bare letter keys are
refused (they'd fire while you type), and customizing a shortcut retires its
built-in alias.

| Keys | Action |
|---|---|
| `Ctrl+P` / `Ctrl+Shift+P` | Command palette (fuzzy: focus session, rename, send/queue, commit/push/PR/IDE, settings, doctor, shortcuts…) |
| `Ctrl+B` | Toggle sidebar |
| `Ctrl+N` / `Alt+N` | New-session dialog |
| `Ctrl+Tab` / `Ctrl+Shift+Tab` (also `Ctrl+PgDn` / `Ctrl+PgUp`) | Next / previous session |
| `Ctrl+1…9` / `Alt+1…9` | Focus the Nth sidebar session |
| `/` | Focus the sidebar session filter (when it's showing) |
| `?` | Keyboard-shortcut cheat-sheet |
| `Ctrl+R` | Reload the app — only while no terminal is focused; in a terminal it stays shell reverse-i-search |
| `Ctrl+K C` | Commit… (chord: `Ctrl+K`, then `C`) |
| `Ctrl+K P` | Push |
| `Ctrl+K R` | Make PR |
| `Ctrl+K O` | Open/focus IDE |
| `Ctrl+K D` | Duplicate session |
| `Ctrl+K H` | Hide/show window |
| `Ctrl+W` / `Delete` | Close the focused window (`Delete` only when not typing) |
| `Ctrl+Shift+T` | Reopen the last-closed session |
| `Ctrl+Enter` | Submit the commit dialog |

Outward-facing git verbs (push, PR) sit behind `Ctrl+K` chords on purpose: a
chord takes two deliberate keystrokes, so no single mistyped combo can push a
branch — the old `Ctrl+Alt` single-stroke family collided with AltGr on
Windows international layouts. Like VSCode (whose
`terminal.integrated.allowChords` defaults to true), the `Ctrl+K` prefix is
claimed even while a terminal is focused; readline's kill-line is the accepted
cost (`Ctrl+U` still kills the line, `Esc` cancels a pending chord). Merge to
staging is deliberately unbound — palette/menu only, behind a confirm.

## New-session dialog

Newcomers see just **Name**, **Prompt**, and **Create** — the defaults do the
right thing (blank repo → the configured `[repository].url` is provisioned; program prefilled). Program,
repo folder, the *create new repo* / *work in place* checkboxes, the folder
browser, and a **Launch flags** field live under a collapsed **Advanced options**
fold (its open/closed state is remembered). **Ctrl/Cmd+Enter** submits the dialog
from anywhere in it (the handler is dialog-level, not tied to the prompt field).

**Launch flags** are extra CLI flags appended to the agent on every start/resume
of the session. The field is pre-filled from the global per-provider default
(`coding_cli.default_launch_args`, Settings → Agent CLI) and its value is
*always sent* with the create request — so clearing it for one session creates
that session with no flags rather than re-inheriting the default.

## New-session dialog: prompt presets

The dialog carries an optional **Prompt** textarea (sent to the agent at
launch) with a **Preset…** select beside it. Built-ins ship for the common
loops — *Fix failing tests*, *Address PR review comments*, *Write tests for
recent changes*, *Refactor for clarity — no behavior change* — and **Save…**
stores the current prompt under a name of your own (✕ deletes a saved one).
Picking a preset fills the textarea; edit freely before creating. Saved
presets live in this browser's `localStorage` under `mindflock.prompt_presets`
(`[{name, prompt}]`).

## First-run onboarding

Two coordinated pieces help a new user find their way, backed by UI store state
(`state/store.ts`: `tourDone`, `hintsEnabled`, `dismissedHints`, persisted in
`localStorage`).

- **Welcome tour** (`onboarding/WelcomeTour.tsx`) — a replayable slideshow that
  covers the basics (sessions, the grid, the customizable sidebar, the
  assistant) and then walks through the one-time account hookups (coding
  provider, ticket ingestion, PR review, issue handling, linked IDE, mobile).
  Setup slides carry a **Set up now →** button that jumps straight to the
  matching Settings screen. It **opens automatically on first run** — when
  `tourDone` is `false` *and* `hintsEnabled` is `true` — and is replayable any
  time from **Settings → General** (`openTour`). Finishing or skipping sets
  `tourDone`.
- **Hints** (`onboarding/Hint.tsx`) — small dismissible 💡 inline callouts that
  nudge toward a feature. Each needs a **stable `id`**; dismissing one remembers
  that id (`dismissHint` → `dismissedHints`). A master switch (`hintsEnabled`,
  Settings → General) hides all of them at once; turning hints **back on
  re-arms** every previously dismissed hint (clears `dismissedHints`).

Both are reset/replayed from the **Onboarding** block at the bottom of
**Settings → General**.

## Make-PR dialog

Clicking **Make PR** opens a branch-picker dialog (`dialogs/MakePrDialog.tsx`)
before the PR is created: a combobox whose dropdown filters the repo's real
branches (from `GET /api/instances/{title}/branches`) as you type, and you can
also just type a name. The session's own branch is flagged and can't be the
target. The **last base chosen per repo is remembered** (`prBaseByRepo`,
persisted) and pre-selected next time (falling back to the server-computed
default); submitting calls `submitMakePr` → `POST /api/instances/{title}/make-pr`.

## Settings (⚙)

- **Cursor auto-adopt** — adopt Cursor-opened workspaces as sessions.
- **Per-session budget (USD, 0 = off)** — cost guardrail: when a session's
  estimated cost crosses this figure the server emits a one-shot
  `session.budget_exceeded` event → warning toast (click focuses the session),
  desktop notification (notify addon), and shell hooks. An over-budget session's
  pane shows a lock overlay with a **Raise budget** action
  (`POST /api/instances/{title}/budget/raise`); sends are refused (409) until
  raised.
- **Connections** — one-screen status of every external integration (GitHub,
  ticketing provider, agent CLI, tailscale) with re-test buttons
  (`GET /api/connections`).
- **PR review** — the automated-PR-review screen: open PRs on the configured
  repo(s), each annotated with why auto-review did / didn't take it, plus a
  force-review action (`/api/github/prs`).
- **Git issues** (screen key `issues`, gated on the `git` + `ticketing` caps) —
  the issue-handling twin of PR review: its own opt-in **Automated handling**
  switch and its own repo list (`github.issue_repos`, independent of PR
  review's), plus an open-issues panel with skip-reason chips and a **Start
  work** force-start (`/api/github/issues`, `/api/github/issues/start`). Reveal
  its sidebar bar via ⚙ Customize.
- **General → Onboarding** — the master **getting-started hints** switch and a
  **Replay tour** button (see [First-run onboarding](#first-run-onboarding)).
- **Agent CLI → scheduled window refresh** — a keepalive that periodically
  pokes each provider's CLI so usage windows stay warm
  (`GET/POST /api/window-refresh`). Its **Default provider** picker reads
  `GET /api/providers/status` and lists **only installed CLIs** — you can't make
  a CLI that isn't there the launch default; if the stored default is missing it
  falls back to the first installed CLI and **persists the correction** (the
  backend rejects the save otherwise — see [web-api.md](web-api.md)).
- **Agent providers** — leads with a **connection view**
  (`GET /api/providers/status`): every registered provider shows an
  **installed** dot and, when a CLI is missing, a copy-to-clipboard **install
  command**. Sign-in is **not** surfaced here — each CLI prompts you to
  authenticate on its own the first time a session launches it, so MindFlock
  never sees your credentials (see [providers.md](providers.md)). Below the
  connection view is the custom-provider **manager**: add/edit/delete user-TOML
  providers via the `/api/providers` CRUD, including each provider's saved
  **launch flags** (`[launch] args`). The per-provider **default launch flags**
  (`coding_cli.default_launch_args`) that pre-fill the New-session dialog are
  also edited here.
- **Mobile** — the `/m` URLs and QR code (`GET /api/mobile`).
- **Security** — view/copy this device's web-auth token, plus a **Regenerate**
  button (`POST /api/settings/auth-token/rotate`) for compromise recovery: it
  mints a new token and invalidates every issued cookie, QR code, and paired
  device at once. The browser you regenerate from stays signed in (its cookie
  is re-issued). Unavailable (409) when `MINDFLOCK_AUTH_TOKEN` pins the token —
  change the env var instead.
- **Remote devices** — when `general.remote_control` is on, other MindFlock
  servers on your tailnet appear as sidebar device groups
  (sessions namespaced `<device>::<title>`); pair/unpair via
  `/api/devices/{device}/connect|disconnect`.
- **Session templates** — save a New-session dialog configuration under a name
  and refill it later (templates addon, `/api/templates`).
- **System logs** — a grid pane tailing the server log (`GET /api/logs`,
  3 s poll).
- **Terminal scroll speed** — 1–20 lines per wheel notch, applied live.
- **Appearance** — theme sets (surface: backgrounds/panels/borders/text) and
  accent presets, mixable freely; every set ships its own light-mode palette so
  the 🌙 toggle works inside any theme. One bird per set, painted the way the bird
  is: most are **multi-color**, giving the top bar, the sidebar and the window a
  hue each — Scarlet Macaw is a cobalt bar over an emerald sidebar over a scarlet
  window; Goldfinch is brilliant lemon over a black cap. Swallow (the default),
  Raven and Heron are the quiet single-neutral ones. Multi-color sets steer the
  `--topbar-*` / `--sidebar-*` region tokens, which
  `frontend/src/styles/regions.css` rebinds onto each region so the controls
  inside it follow along with no per-component CSS; top-bar icons are monochrome
  `currentColor` SVGs so they survive a bright bar (an emoji 🌙/🔔 would not).
  Accent keys are shared with the surface of the same name where one exists.
  `tests/unit/test_appearance_accent.py` asserts a contrast floor for every
  region of every set, so a louder repaint can't strand the text.
  The palettes live in `theme.css`
  (shared with the mobile page, which renders their window palette only); the
  choice persists server-side
  (`ui.surface` / `ui.accent` via `/api/settings`) so desktop and `/m` match,
  with `localStorage` (`cs_surface` / `cs_accent`) as a pre-paint cache.

Theme (dark/light), sidebar visibility, view mode, pane order/layout, diff mode,
last-used tab, and prompt presets are persisted in `localStorage` (`cs_*` /
`mindflock.*` keys).

## Token / cost usage

Each pane's usage trigger opens a per-session popup (input/output/cache tokens,
context-window fill, estimated cost, model); the sidebar readout aggregates
across sessions with a period selector (session / day / week / month / year,
backed by `/api/usage`). Figures come from Claude Code transcripts and a daily
pricing feed — rough estimates for awareness, not billing.

## Voice input

The mic button (🎙, Chromium's `webkitSpeechRecognition`) dictates into the
focused terminal or text box, with a live caption bar.

## Mobile UI (`/m`)

A single full-screen terminal for phones: session picker, activity dot,
Agent/Shell tabs, and a soft-key bar (esc, sticky ctrl, tab, arrows, enter — the
sticky ctrl folds the next key into a control code). It uses the same WebSockets
and instance list as the desktop UI, remembers your last session, and accepts
`?s=<title>` to deep-link one. The server prints the `/m` URL (and a QR code in
tailscale mode) shortly after startup — the banner probe (like the other
non-critical warmups: paste cleanup, scroll-speed apply) runs as a background
task so the server answers its first request immediately instead of blocking
on shell-outs that can hang; the banner may therefore print after the server
is already serving. The copy of that banner written to the server log
(the Settings → System logs pane, `GET /api/logs`) is redacted — no token, no
QR; the full banner appears only on the operator's console.

**Scrolling** — swipe up/down on the terminal to scroll history, natural
direction (content follows the finger). The agent TUI lives on tmux's alt
screen, so there is no local xterm scrollback to swipe through; instead a
vertical one-finger drag is translated into SGR mouse-wheel ticks sent down the
PTY and tmux (`mouse on`) scrolls its copy-mode history — the same mechanism
the desktop grid uses for the mouse wheel (and the desktop grid now accepts
touch drags the same way, for tablets). Taps, pinches, and horizontal gestures
pass through untouched, so tapping the terminal still raises the keyboard for
raw keystroke mode.

**Compose box** — a normal phone text field between the terminal and the
soft-key bar: type or paste with full native autocorrect/clipboard support,
then **Send** (or Enter) delivers the whole draft to the PTY followed by Enter.
Shift+Enter inserts a newline in the draft, an empty Send just presses Enter
(confirming TUI prompts), and the field keeps focus after sending so the
keyboard stays up. The soft-key bar still acts on the PTY directly while
composing — arrows and sticky ctrl work mid-draft.

**Git workflow action bar** — a **Commit / Push / PR / Merge** button row that
brings the desktop pane header's guided flow to a phone, so the whole git
loop is now drivable from mobile (previously sessions could only be created
and driven from the desktop view). The buttons hit the same
`/api/instances/<title>/{commit,push-branch,make-pr,merge-pr}` endpoints the
desktop uses, and `nextAct()` mirrors the desktop's stage logic to **highlight
the one recommended next step** for the session's current stage (agent →
Commit, committed → Push, pushed → PR, PR open → Merge). **Commit** opens a
bottom-sheet commit-message dialog; **Merge** confirms first. **Push** honors
the O3 soft gate exactly like the desktop `pushSession` flow: if checks
haven't passed for the commit the push comes back `409`, and a `confirm()`
offers to re-push with `{force: true}`.

## Addons in the frontend

`GET /api/addons` describes each addon's frontend mounts (`FrontendDescriptor`:
where to render, which JS module, which WebSocket, poll interval).
`core/slots.js` auto-renders a sidebar bar for any addon **not** marked
`builtin_ui` and, for descriptors carrying a `module` URL, dynamically imports
that ES module (served from `static/addons/`) and calls
`window.mindflockAddons[<id>].init(ctx)` — handing it the descriptor plus the
client event bus (`window.mindflock.events`, fed by `WS /api/events`), the
`window.mindflock.sessions()` snapshot accessor, and a toast helper when the
SPA provides one. A module that fails to load is skipped with a console warning.
The hand-wired built-ins (MindFlock, Assistant, Settings, Doctor) are
`builtin_ui: true` with `module: null`; the **notify** addon
(`static/addons/notify.js`, desktop notifications on clarify / PR close /
budget exceeded — rules are data-driven from `GET /api/notify/config`) is the
reference for this generic path — the full contract lives in
[docs/extensions.md](extensions.md). `slots.js` also feeds the provider list
into the New-session dialog's Program field.

`core/ws-xterm.js` exports `WsXterm`, the shared xterm↔WebSocket bridge used by
addon panes (binary PTY frames in, resize JSON out, auto-reconnect except on
4404/4409).
