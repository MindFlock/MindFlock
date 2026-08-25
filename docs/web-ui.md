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

**Top bar** — on the left: the brand logo, the sidebar toggle (`Ctrl+B` / `⌘B`),
the theme toggle 🌙 and the notifications 🔔 bell. Then the menu — **New**,
**Intake** (`Alt+I`; see [Intake](#intake)), **Recent ▾** (Recently closed… /
Workspaces on disk…), **Prompts**, **Command**, **Settings**. **Intake** sits
beside **New** because both are about starting sessions — one from scratch, one
from what came in. The **MindFlock** wordmark (carrying the *running engine's*
version, plus a red `-DEV` badge under a dev shell) sits centered, and the empty
strip beside it is the drag region that moves the desktop shell's window.

In the desktop shell **on macOS** the bar mirrors. The window keeps the OS
traffic lights top-left (`titleBarStyle: 'hidden'` — the shell draws no – □ ✕
there), so the bar reserves ~78px in that corner and moves the logo / theme /
bell cluster to the right, where a Mac has nothing else; the sidebar toggle and
the menu stay where they are. It is the same elements with the same behaviour,
mirrored — the one difference is DOM order: the mirrored cluster sits after the
drag region, so `Tab` reaches the theme toggle and bell last there instead of
first. The reserved gap collapses in fullscreen, because macOS hides the lights then:
the shell pushes transitions over a `fullscreen-changed` IPC event, and the bar
also *pulls* the current state (`win:is-fullscreen`) when it mounts — a window
that is already fullscreen when the UI loads or reloads never sees a transition,
so without the pull it would hold the gap open until you toggled fullscreen.

The **same** UI in Safari or Chrome on macOS keeps the standard left-hand
layout: the decision reads the preload bridge's `nativeTitleBar` capability flag
(`frontend/src/lib/shell.ts`), never the user agent, so the layout only shifts
where something really draws native controls. Two DOM hooks come with it, for
theme and addon authors styling against the bar: `#topbar[data-mac]` (native
title bar) and `#topbar[data-mac-lights]` (…and the lights are visible right
now).

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
Below: view-mode buttons (Auto/1/2/4/9), the session count, a **⚙ Customize**
button (sidebar bars — see below) and **⌨ Shortcuts**. (Recently closed, the
workspace manager, the command palette, Intake and settings live in
the top bar's menu, not down here.) The workspace manager lists each managed
workspace with its size and a per-row delete, plus a **Clear** button that
bulk-removes every unprotected, idle workspace in one sweep
(`POST /api/workspaces/clear`) — protected base clones / cache refreshers and any
dir a live session is using are left alone.

**Customizable bars** — the sidebar bars are movable and hideable, driven by a
shared registry (`sidebar/barDefs.ts`: Usage, Ticket Ingestion, PR Review, Issue
Handling, Assistant). A **fresh install shows three** (`DEFAULT_VISIBLE_BARS`) —
**Usage**, **Ticket Ingestion** and **Assistant** — so the flagship
ticket → session flow is reachable out of the box without being overwhelming;
**PR Review** and **Issue Handling** start hidden. The footer **⚙ Customize**
popover (`FooterCustomize`) toggles each bar on/off, and bars drag-to-reorder
(the session list is a fixed anchor bars can sit above or below, but which never
itself moves). Order and the hidden set persist per browser (`localStorage`).
Each feature bar's own button deep-links into the matching **Intake** tab
(Tickets / PRs / Issues), so a bar is a status light plus a switch rather than
the only door — Intake is in the top bar whether or not its bar is showing.
Turning a feature's bar on is still how you get an at-a-glance dot for PR review
and issue handling once you've connected them; the first-run footer hint points
only at those still-hidden bars.

**Command palette** — `Ctrl+P` or `Ctrl+Shift+P` (`Cmd` on Mac, or the top bar's
**Command** button) opens a fuzzy-filtered palette over everything: jump to
("Focus:") any session, New session, Commit / Push / Create PR / Open in IDE on
the focused session, Open Intake (plus **Intake: Tickets** / **Intake: Pull requests**
/ **Intake: Issues**, so typing "issues" lands on that queue instead of on a
dialog you then have to navigate), Open Settings / Doctor / Setup checklist,
Toggle sidebar, and New from Recently closed. Type to filter (subsequence
match), `↑`/`↓` to select, `Enter` to run, `Esc` to close. Both bindings work
from anywhere, including while a terminal has keyboard focus — the same VSCode
trade-off as its quick-open/palette keys.

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

Each session shows a stage badge (sidebar + pane) computed from git, plus a
GitHub lookup (`gh` or a token) for the two PR stages:

```
provisioning → agent → pre-commit → committed → pushed → PR open → merged
                          └─ pre-commit ✗ (hooks blocked the commit)
```

The pane shows a single **guided next-step button**:

| Stage | Button | What it does |
|---|---|---|
| agent | **Commit…** | Prompts for a message; runs `git commit` in the Terminal tab (pre-commit hooks visible; auto-fix retries up to 5×) |
| pre-commit ✗ | **Re-commit** | Restages the hook auto-fixes and retries with the same message |
| committed | **Push** | `git push --no-verify -u origin HEAD` (hooks already ran) — plain git over your own remote, SSH or HTTPS |
| pushed | **Make PR** | `gh pr create --base <base> --fill` when `gh` is authenticated; else the GitHub REST API with a token; else opens a prefilled compare URL in your browser |
| PR open | **Merge** | `gh pr merge --merge` (confirmed); else the REST API with a token; else opens the PR page |
| merged | **Open PR ↗** | Opens the PR page |

Stages are detected best-effort from git state, so a commit made in Cursor also
advances the badge within a few seconds. Everything up to and including
**pushed** is pure git and needs no GitHub credential at all; the `PR open` and
`merged` stages do (an authenticated `gh`, or a token from Intake → Pull requests
→ Advanced options), so with neither the chip parks on `pushed` while the buttons
keep working through the browser. Live agent **activity** overlays the stage chip:
`running`, `clarify` (the agent is asking you something), `idle`,
`offline`, `paused` — detected from the CLI's own activity hooks where
available, with CPU/pane-hash fallback (see [providers.md](providers.md)).

## Session row actions (expand a sidebar row with ›)

Copy path · **Commit…** · **Push** · **Make PR** · **Merge to staging** ·
**Open PR ↗** · **Copy window** (a second in-place session on the same worktree) ·
**Open/focus Cursor** (row double-click does the same) · **Hide/Show window**
(session keeps running) · **Pause/Resume** · **Delete + wipe worktree** (confirmed).

A copy is filed **directly beneath the one it was copied from**, not at the
bottom of the rail — the provisioning row lands there too, so it never appears
at the end and then jumps. It stays put after that: the sidebar order is yours,
and nothing but a drag moves a row again.

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
  toggles draining. Items reorder by dragging the **⋮⋮** handle, edit (✎),
  insert a new prompt above/below any item (**+↑** / **+↓** open an inline
  composer at that slot), delete, or **send now (▶)**. Dropping a `.csv` file
  anywhere on the tab appends one prompt per row to the end of the queue
  (quoted fields respected, an obvious header row skipped; any other text
  file queues one prompt per line) —
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

**A session that ran out with nothing queued** used to fall outside all of that:
an empty queue meant nothing was watching, so it sat on the CLI's limit screen
until a human came back, often hours after the window reopened. The same drain
pass now also walks the sessions whose activity is `limit` (a free read of the
state snapshot — no probes when nothing has run out), waits out the window with
the same meter/banner logic, then sends **Esc + `continue`** so the agent picks
its task back up. Settings → General's **Resume sessions when usage comes back**
turns the nudge off (default on); either way the reopening emits
`session.usage_restored` once, which is what the "Usage comes back" notification
rule rides on. Sessions with a queued prompt are left to the drain — its prompt
is the better thing to send — and the nudge obeys the drain's own
send-once-then-wait rule, so a menu that doesn't clear gets a bounded retry
rather than one every five seconds.

**Running out looks exactly like finishing**, which is the trap under all of the
above. A turn the weekly cap cuts short ends the way a completed one does — the
CLI fires its Stop hook, the pane goes quiet — so the activity badge read `idle`,
the watcher above (which selects on `limit`) never saw the session, and
fast-track's usage-limit gate was never armed: it counted its 30-second settle
over a half-finished session and committed, hooks and all. An `idle` reported by
the CLI's own hook is now re-checked against the pane for a limit screen
(throttled per session, since that path exists to avoid a capture on every poll),
and before fast-track commits anything it confirms an idle agent against the
usage meter — the pane may carry no banner at all. While a limit holds, the
settle is dropped and has to be re-earned once the window reopens, and the step
deadline stops running: a weekly window can be shut for days, and a run that
waits it out correctly must not then be halted for "no progress".

The command palette has **Send message…** and **Queue prompt…** for the focused
session (keyboard-only via a prompt).

## Notifications (🔔)

The header bell keeps a running feed of notable session events — finished/idle,
needs-input, stage changes, cost-over-budget, auto-sent queued prompts — fed by
the events bus, **including the backlog replayed on connect** so it answers
"what happened while I was away." An unread badge counts events since you last
opened it (keyed on timestamp so it survives server restarts); clicking an entry
focuses that session.

**One rule list, two delivery channels.** Settings → Notifications is split that
way on purpose: *What triggers a notification* (needs-input, PR merged/closed,
budget exceeded, pre-commit failed, ran out of usage / usage came back — plus
the noisy opt-ins) governs **both** channels, and each channel decides only
where an alert lands.

The two usage rules are a pair: **"A session runs out of usage"** fires on the
activity flip to `limit` (the agent is parked on its CLI's usage-limit screen),
and **"Usage comes back after running out"** fires on `session.usage_restored`,
which is emitted once per reopening by the watcher that resumes those
sessions — so it cannot fire for a window that merely rolled over while nothing
was blocked, and several sessions unblocking together still make one
notification. Whether those sessions are actually nudged to carry on is
Settings → General's **Resume sessions when usage comes back**; the
notification fires either way.

- **Browser / desktop** — the notify addon's `Notification` popup. Needs a tab
  open on a secure origin (HTTPS or localhost), so it is silent on a plain-http
  tailnet URL and says so in the toggle's status line.
- **Phone push (ntfy)** — optional, off until you turn it on. The *server*
  publishes to an [ntfy](https://ntfy.sh) topic your phone subscribes to, so an
  alert reaches you with MindFlock closed and no tab anywhere. Set a topic
  (**Generate** offers a random one), scan the QR into the ntfy app, and
  **Send a test** confirms the round trip; the row afterwards reports the last
  push (or why it failed). Point **Server** at your own ntfy instance to keep
  everything in-house, and give **Access token** a value only if the topic is
  protected — **Clear** beside it removes a saved one, since blanking the field
  means "keep" and a *wrong* token is worse than none (ntfy 401s a bad
  credential on a topic that needed no credential at all).
  *Tapping opens* is an optional URL the notification opens. You usually do not
  need it: when Tailscale is up every push already carries this machine's
  tailnet `/m` URL — in the message text and as the tap target — deep-linked to
  the session the alert is about (`/m?s=<title>`), so a tap lands on that
  session's terminal. Set the field only to send taps somewhere else; MindFlock
  strips an `?token=` from whatever you paste, since that URL is stored on the
  ntfy server. (The automatic URL is bare for the same reason.)

  Priority is per rule: "needs your input", "budget exceeded" and "pre-commit
  failed" go out at ntfy priority 4 (buzzes through most do-not-disturb
  setups), PR merged/closed and the two usage rules at 3, and the ambient
  opt-ins (idle, pre-commit running) at 2 so they arrive quietly.

  You also get one push carrying the phone URL whenever it becomes newly
  reachable — at server start, when you switch this channel on, and when you
  turn on tailscale mode — so a phone that has never heard from this machine
  learns where it is without a QR scan at the desk. It is deduplicated (turning
  on push and tailscale mode back to back is one intent), and a URL that is not
  live until the server restarts says so.

  On the **public ntfy.sh server the topic name is the credential** — anyone who
  knows it can read your session titles or send you fakes — so keep the
  generated random name, or self-host. The screen shows that warning while the
  channel is on and pointed at ntfy.sh.

**Diagnosing a channel you can't see.** A push channel is invisible when it works
and baffling when it doesn't, so the row beside **Send a test** is the whole
diagnostic surface — no log reading required:

- Straight after a test it shows that test's verdict: *"Sent — check your phone."*
  or the failure reason. The reason is ntfy's **own** sentence when the server
  sent one (`{"error": …}` from its JSON body — "topic is reserved", an auth
  refusal), otherwise the transport error (DNS, TLS, timeout).
- Otherwise it shows the last *real* push, either **"Last push sent 4 min ago"**
  (relative: "just now" under a minute, then minutes, hours, days) or
  **"Last push failed: …"** with the same surfaced reason. Blank means nothing has
  been attempted yet this server run — the record lives in memory, so it resets on
  restart, and a fresh reload of a long-running server shows the real history.
- A missing QR is not a failure: it means the optional `segno` package isn't
  installed, and the subscribe URL printed beside it is the intended fallback.
- One failure mode this row will *not* show you: the 60-pushes/hour runaway cap.
  Throttled pushes are dropped before any HTTP attempt and deliberately don't
  overwrite the last result, so the symptom is a cheerful "Last push sent …"
  next to a silent phone. **Send a test** is exempt from the cap and will still
  succeed; the evidence is a single line in Settings → System logs
  (`ntfy: over 60 pushes/hour — dropping further pushes this window`).

Delivery is best-effort in one direction only: **a failed push never touches the
session it was reporting on.** The event that triggered it has already been
emitted, the websocket clients and shell hooks have already seen it, and the
browser channel fires independently. A wrong token or an unreachable ntfy server
costs you the phone alert and nothing else.

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
  never renamed, so nothing can break. **Notifications use the name you see**:
  a desktop notification and an ntfy push both call a session by its rename, or
  — for the ones nobody renamed — by the label the rail shows (`(tix)
  add-dark-mode/sc-12345`), never by the machine slug the event carries. A push
  about a window you cannot find in the rail is worse than no push.
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
| `Ctrl+P` / `Ctrl+Shift+P` | Command palette (fuzzy: focus session, rename, send/queue, commit/push/PR/IDE, work, settings, doctor, shortcuts…) |
| `Ctrl+B` | Toggle sidebar |
| `Ctrl+N` / `Alt+N` | New-session dialog |
| `Alt+I` | Intake — tickets, PRs and issues waiting to become sessions |
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
  matching surface: three of the six — ticket ingestion, PR review, issue
  handling — open the **Intake** dialog on their tab, the other three a Settings
  screen (`LEGACY_SCREEN_TABS` decides which from the slide's `screen` key). The
  tour pauses behind either dialog rather than ending, so closing it lands you
  back on the same slide. It **opens automatically on first run** — when
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

## Intake

**Where your sessions come from.** Ticketing, PR review and issue handling used
to be three screens in Settings, and they never belonged there: Settings is
set-and-forget, while these three are somewhere you *visit* — to see what came
in, start something by hand, or pause a queue. They now have their own top-bar
entry (**Intake**, `Alt+I`, or the palette's Open Intake), dialog `#intake-dialog` /
panel `#intake-panel`, built from
`frontend/src/components/intake/{IntakeDialog,TicketsTab,PullRequestsTab,IssuesTab,RepoSources,kit}.tsx`.

**Every work list has the same Ctrl+F filter.** Tickets, Pull requests, Issues
and the auto-start roll-up all grow without bound — every open PR on every
watched repo, every ticket in every workflow state — so each toolbar carries the
box Recently closed and Verify already use, with the same rule: every
whitespace-separated token has to appear somewhere in the row, so `sitecheck
4217` and `feature coupon` narrow without anyone remembering the separator.
Ctrl+F focuses it; Escape clears it before it closes the dialog. What each list
searches is the row's own words plus the two things the row shows as *shape*
rather than text — the state/branch it is grouped under, and whether auto
ingestion has queued it, so `queued` narrows a repo to what is about to start on
its own and `already handled` finds what it skipped. **Filtering happens before
grouping**, so every heading's count describes what is under it and a repo or a
workflow state with no match drops out instead of leaving an empty group behind.
The one number that ignores the filter is the **+ Add bucket…** menu's per-state
count, because that menu is a standing choice about which states this panel
shows at all (`intake/search.ts` holds the four rules, one per list).

**Tab strip with live counts** — **Tickets**, **Pull requests**, **Issues** (keys
`tickets` | `prs` | `issues`), a horizontal strip rather than Settings' long left
nav because the three are peers you flip between while reading. Each badge is the
number of rows that tab will actually show you — not a total. That distinction is
the whole of it: counting every ticket the provider ever assigned you read `1221`
over a list of 52, because done states are parked behind **+ Add bucket…**, so
the badge and the panel go through one shared bucket filter (`intake/buckets.ts`)
and cannot disagree. A tab with nothing (or an unconfigured integration, which
502s) shows no badge at all rather than a `0` that turns into `12` a second
later. The
two GitHub tabs are gated on the `git` + `ticketing` caps and explain themselves
in place when either is missing. The three retired Settings screen keys
(`ticketing`, `repo`, `issues`) still route — `LEGACY_SCREEN_TABS` in
`IntakeDialog.tsx` maps them onto tabs, and the Settings dialog hands off to Intake
when it is deep-linked one — so every old link keeps working, including the
**Configure** button on a Connections card (the server's `settings_screen`
values) and the welcome tour's setup slides.

**One anatomy, three tabs** (`components/intake/kit.tsx`): a master switch → a
list of collapsible **source cards** with **+ Add** → a **work list grouped by
source**, each row carrying why auto-pickup did or didn't take it. Each of the
three is a titled section, and the switch is a banded row; the switch used to be
followed by a status sentence ("● Active — polling 1 source…", "‖ Paused — 1
source kept…") and no longer is — with the toggle beside it and the sources it
counted listed directly below, the line only restated what was on screen. Its one
subtitle is the state a switch cannot show: nothing connected yet. They used to be three dialects of that shape; ticketing had the good one
(add/remove cards, each with its own credentials, repo and agent), so the other
two were rebuilt onto it — a ticketing source and a watched repo are both just
*sources* now. What legitimately differs stays in the tabs: only tickets have
workflow-state buckets inside a source, and only the two GitHub tabs share one
credential.

- **Tickets** — the master **Automated ingestion** switch (the same
  `/api/mindflock/status` + `start`/`stop` contract, query key and 4 s interval as
  the sidebar's Ticket Ingestion bar, so the two never disagree), one card per
  connected source (provider, optional label, Repo URL, its own **Agent CLI**,
  its **Thinking effort**, ingest-state picker, credentials, **Test connection**,
  **Remove**), then
  **Assigned tickets** — the slowest of the three fan-outs (~3 s: a provider
  search per source plus a `git ls-remote` per repo). A source's **Agent CLI**
  lists `GET /api/providers` (so a provider you defined yourself is selectable
  too), its unset option names the app default rather than showing a blank, and
  the collapsed card always states which CLI the queue runs — the difference
  between a queue on a hosted CLI and one on a local model is worth seeing
  without expanding. **Thinking effort** sits directly under it, because the two
  are one decision: a rung means different things on different CLIs (each spells
  it its own way and clamps its own ceiling), so the picker names *that* CLI's
  ceiling and is disabled outright for a CLI with no effort control. It applies
  to every ticket from the queue — ingested automatically or started by hand —
  and an individual ticket can still override it on its row, where the empty
  choice now reads **Configured (…)** naming the queue's rung. Unset means
  whatever the CLI does on its own; there is deliberately no flock-wide default,
  because how hard to think is a property of the work rather than of the
  installation. Unset **Agent CLI** falls back to `[mindflock].agent`, then the app
  default (see
  [ingestion-pipeline.md](ingestion-pipeline.md#which-agent-cli-a-ticket-runs)).
- **Pull requests** — **Automated review** (absent = on once repos exist), the
  repository cards, then **Open pull requests** with **Begin review**. This tab
  also owns the shared GitHub token, under **Advanced options**.
- **Issues** — **Automated handling** (opt-in: absent = off), its own repository
  cards (`github.issue_repos`, independent of review's), then **Open issues**
  with **Start work**. It links to the Pull requests tab for the credential,
  which authenticates the same account.

**Per-repo cards, with inherited defaults** — the two GitHub tabs no longer show
a flat `owner/name` chip list. Each watched repository is its own
collapsible card carrying its **Agent CLI**, **base branch** (PR only — issue
work always branches off the repo's own default), **min age**, **skip authors**,
a **Test access** button and **Remove**; the collapsed header always names the
CLI that repo's sessions will run. The chip list could not express what people
actually want from a multi-repo setup — "watch this one too, but give it half an
hour of grace and run it on codex" — because there was nowhere to hang a per-repo
value. The tab-wide fields moved into an **Advanced options** fold and are now
the **defaults a blank card field inherits** (the placeholder shows the inherited
value, so empty reads as "inherit 15" rather than "no grace period"), alongside
the genuinely one-per-app settings: the poll interval and the GitHub token.
Storage is `github.repo_settings` / `github.issue_repo_settings` — maps keyed by
`owner/name` whose block may carry `agent`, `base_branch` (PR only),
`min_age_minutes`, `skip_authors`; an absent key or blank value inherits.
Renaming a card carries its settings across, and removing one drops them. See
`backend/config/settings.py` (`REPO_OVERRIDE_KEYS`, `_repo_overrides`,
`GithubSettings`) for the shape and `backend/ticket_ingestion/config.py`
(`GithubConfig.min_age_for` / `base_branch_for` / `skip_authors_for` /
`agent_for_repo` and the `issue_*` twins, plus `pr_agent(repo)` /
`issue_agent(repo)`) for how the monitors resolve them — every filter goes
through one of those rather than reading the flat field.

**Test access** (`POST /api/settings/test/github-repo`, body `{repo}` →
`{ok, name, private, default_branch, can_push}`, always HTTP 200) is the
per-repo twin of `/api/settings/test/github`: that one answers "is there a
credential", this one "does it reach *this* repo", which is the failure people
actually hit — a typo'd slug, or a private repo the PAT has no scope for (GitHub
404s both, so the message covers both readings). On the Issues tab a read-only
token is called out, since issue work has to push a branch.

**Work lists group by source — always, on every tab.** "Which provider is this
from?" is the question a queue has to answer before any other, and a heading
answers it once for however many rows sit under it. A per-row label was the
alternative and it does not scale: the same name repeated down a column of 500.

**Tickets add a third level, because the providers have one.** A source with
several workflows/boards has to qualify its state names to keep them unique —
Shortcut returns `Product Development · Deferred`, `DevOps · Unscheduled` — so a
flat list wrote `PRODUCT DEVELOPMENT ·` onto seven consecutive headings. The
workflow becomes a level of its own and the qualifier is written once:

```
▾ Allure Security          52   auto-ingests Product Development · Will do
   ▾ Product Development   48
      ▸ Deferred           20  ✕
      ▸ Unscheduled        13  ✕
      ▸ Will do             4  ✕
   ▾ DevOps                 3
      ▸ Unscheduled         1  ✕
   ▾ Creative               1
      ▸ Unstarted           1  ✕
```

Every heading in that tree is a press-target and is styled as one: a hit area, a
hover fill, a caret that *rotates* rather than swapping ▸ for ▾, and — on the
source, which is a section header as well as a control — a filled band. As plain
text beside a small caret, sitting under a *bordered* source card, the top-level
heading read as a stray line of copy and people did not find the tickets under it.

`GET /api/tickets` carries a `bucket_meta` map (`{bucket: {group, label}}`) so the
UI nests without splitting strings: the provider reports the workflow (`group`)
and the unqualified state (`label`) alongside the composite `name`, which stays
the unique bucket key that `+ Add bucket…` / ✕ and `done_buckets` are indexed by
(see `TicketProvider.list_states`). A provider with one workflow — or none, like
Jira and Linear — reports an empty `group`, the level doesn't render, and the
bucket keeps its own name. The two GitHub tabs have no analogous middle level:
their hierarchy is repository → rows.

The
source heading carries a count and what that source auto-ingests ("auto-ingests
In Progress, Ready" / "auto-ingests every state" / "could not be reached"), which
is also why `GET /api/tickets` gained a top-level `source_labels` map covering
**every** configured source — one that returned nothing, or errored, still needs
a heading to say so under. Bucket **show/hide** is app-wide by name (choosing
"I don't care about Completed" means it everywhere; done-type buckets start
parked behind **+ Add bucket…**), while a bucket's **open/closed** state is per
source, keyed `source::bucket`. Both live in `localStorage`
(`mf_ticket_buckets`, `mf_ticket_buckets_open`, `mf_work_ticket_sources`;
`mf_work_pr_groups` / `mf_work_issue_groups` for the repo groups) rather than
settings.json — "I collapsed Backlog on this laptop" is not a property of the
flock. Source groups default open (the rows are why you came), state buckets
default closed (the headings with their counts are the overview), and both are
stored as the set of exceptions so a fresh install writes nothing. The panel
still lists work you are about to move *into* an ingest state, not only what
already matches the source's filters — Jira, Linear and Shortcut annotate each
ticket with its workflow state, while GitHub Issues and Asana expose no
workflow-state model and land in `No state`.

**Every row can be started on a different coding CLI, for that one launch** — a
small picker beside **Begin work** / **Begin review** / **Start work**, whose
empty choice names what the row's source or repo card would use ("Configured
(codex)"), so it is never a mystery. You notice mid-review that this one wants a
different model; re-configuring the whole queue to run one item is the wrong
shape of action, so the override is not persisted. All three start routes
(`POST /api/tickets/start`, `/api/github/prs/review`, `/api/github/issues/start`)
accept an optional `agent`; it outranks the source/repo card's own agent, and an
unknown name is a **400** rather than a silent fall back to the default
(`_start_agent_override` in `server.py` — a typo that quietly ran the wrong CLI
is worse than a rejected request).

Beside it sit the other two per-launch pickers, on the same line and with the
same "just this item" lifetime: **how far** to carry it (the autopilot depth —
an individual item may choose *Merge*, which a source default may not) and **how
hard to think** about it. The effort ladder is neutral — Low, Medium, High, Extra
high, Max, Ultra — and the server translates the rung into whichever CLI the row
launches, so the picker labels what will actually happen: a rung above that CLI's
ceiling reads "Max (→ Extra high)", the top rung is named the way the CLI names
it ("Ultra (ultracode)" on Claude Code, which starts the session with `--effort
ultracode` rather than at Max), and a CLI with no effort setting disables the control
outright ("No effort control (aider)") rather than leaving one that quietly does
nothing. The rungs come from `GET /api/providers` (`effort.levels`); the
translation table is in [providers.md](providers.md). All three pickers share one
line-and-a-half above a button that hugs its label: they used to stretch the
primary button to their own width, which turned **Begin work** into a bar.

**Failures pop up bottom-right**, as a card with a red rule — the corner the
connection-lost card already owns (`lib/errorPop.ts`), not the 1.4s bottom-centre
confirmation strip `toast()` uses. A refused start answers with a paragraph of git
output whose remedy is its last sentence ("… is already checked out at `<path>`.
Kill that session first"), and it has to survive long enough to be read: the cards
stack, newest nearest the corner, and stay until dismissed. The same card is where
a *recorded* failure reason goes — the chip shows the front of the sentence and
opens the rest on click, instead of wrapping three lines of git output into the
row and reformatting the list around it.

Two related fixes live behind this surface: a ticketing source's **Agent CLI is
re-read from disk when a ticket is stamped** (`source_agent_now`), so switching
it in the UI applies to the next ticket rather than the next pipeline restart —
and *clearing* it now clears it, instead of the boot snapshot's value living on;
and a **forced** start resolves the per-repo / per-source agent rather than
jumping straight to the app default.

`GET /api/github/prs` also lists PRs into **every** base branch now and explains
a non-matching one as a skip reason (`"targets X, not the watched base (Y)"`)
instead of filtering the row out server-side — a PR sitting there with no chips
read as "queued" when the monitor would never see it. **Begin review** works on
those too.

### Panel lists are cached, not refetched per visit

The three list panels — **Assigned tickets**, **Pull requests**' open PRs,
**Issues**' open issues — each fan out to a slow upstream. They no longer live in
per-tab React state (which the dialog threw away on close, and a tab switch
unmounts anyway); they're held in the query client
(`frontend/src/state/queries.ts`), so reopening the dialog or switching away and
back shows the last list **immediately** while a refresh runs behind it. The
panel's note area says `Loading…` on a cold panel and `Refreshing…` over rows
already on screen. Opening the Intake dialog **prefetches all three**
(`prefetchIntakePanels()`), so clicking through to one finds it loaded — and it is
what fills the tab strip's counts, which read the same cached query the tab does
and therefore can never disagree with the list underneath.
**Refresh** sends `?fresh=1`, which skips the server's cache and waits for a real
sweep, so the click means what it says. A force-start row action deliberately
does *not*: it re-lists from the cache a few seconds later, because `has_session`
is annotated live on every response — even a cached one — so the cheap re-list
already shows the session it just created.

Two consequences worth knowing:

- A failed load now **keeps the previous rows** and adds an error banner instead
  of emptying the panel, and the server keeps serving its last known list for up
  to 5 minutes through an upstream blip (see [web-api.md](web-api.md)). A
  per-source failure on the Tickets tab is reported on that source's own heading
  and in the note line, not by failing the whole panel.
- Because opening Intake warms all three, it costs up to three ticket/GitHub
  fan-outs even if you only glance at the counts — and an integration that isn't
  configured yet will have its `Could not list …` error ready the moment you
  first open that tab.

## Verify

**What shipped, and does it work?** Intake is the front of the pipeline; Verify
is the back of it. The pipeline's last honest checkpoint is "the PR merged", and
merged is not verified — nobody has opened the thing and looked at it. So work
that reaches the branch you actually ship from comes back here as a **checklist**,
an agent works the steps it can from a shell, and whatever needs a pair of eyes
is handed to you as a short list. Top-bar entry **Verify** (`Alt+V`), dialog
`#verify-dialog` / panel `#verify-panel`, from
`frontend/src/components/dialogs/{VerifyDialog.tsx,verify.ts}`; the sidebar bar
is `frontend/src/components/sidebar/VerifyBar.tsx` and the store is
`backend/web/core/test_plans.py`.

**One noun, one verb.** The feature is **Verify**, the artifact is a
**checklist**, the act is to **check**. "Test plan" is the name in the API paths,
the store file and the settings keys — it never appears on screen, because to a
developer it reads as pytest.

**The chain, in order.** Nothing appears here until three things have happened,
and every "why is nothing showing up?" is a question about which one hasn't:

1. **Track a repository** (Verify → **Sources**, or the repo's own committed
   `.mindflock.toml`). Membership *is* the opt-in; there is
   no second on/off per repo. Writing a checklist costs a real model call, so
   nothing happens in a repo nobody named.
2. **Push a session branch in it.** The **first** push of each session branch
   writes a checklist — from that branch's diff, and from what the session itself
   was doing. It sits in **Not shipped yet** while the branch is reviewed and
   merged. A later push always records the newest commit, and — *only while
   nobody has answered anything yet* — rewrites the checklist from the branch's
   whole diff at that commit, at most **three** times and never within five
   minutes of the last one. The moment you answer one step the checklist is yours
   and nothing rewrites it under you; **Rewrite the checklist** is the button for
   that, and it always works from the newest pushed commit.

   It rewrites rather than appending, deliberately: a later commit that reverses
   or renames what an earlier one added would otherwise leave a step that *must*
   fail against correct shipped code, and nothing could retract it.
3. **That commit reaches the live branch, and the deploy lands.** A background
   pass asks `origin` whether each waiting checklist's sha is an ancestor of the
   live branch. **Merged is not deployed**, so that starts a clock rather than
   handing you the checklist: the row reads *"Merged 4m ago — waiting for it to
   deploy"* until the repo's deploy window has passed, and only then does it move
   into **Not checked yet**, raise the badge and (if ntfy is configured) push to
   your phone.

   The window is **Deploy takes (minutes)** on the repo's Sources card, inheriting
   the flock-wide `repository.deploy_delay_minutes`, which defaults to **5**. Set
   it to `0` where merging *is* shipping. It exists because checking too early is
   not merely early — you see the behaviour the change replaced and record a
   **failure against correct code**, which is the one outcome this surface cannot
   survive, so the wait deliberately errs long. When a deploy lands sooner than
   usual, **⋯ → It's deployed — check it now** skips the rest of it.

**When the wait cannot end, the row says so.** Two things used to make a
checklist wait for ever in silence, and both looked identical to "not merged
yet":

- **origin has no such branch.** `git fetch origin <branch>` only writes
  `refs/remotes/origin/<branch>` when the remote's refspec covers it — and
  MindFlock's own provisioned base clones are cloned narrow, so `origin/main`
  never existed locally and the ancestry test failed against a ref that could not
  be resolved. The fetch now names its destination explicitly, and a branch
  origin genuinely does not have is reported on the row instead of waited on.
- **the PR merged somewhere else.** A repo that PRs into `staging` but ships from
  `main` has work that is genuinely merged and genuinely not live. The row now
  reads *"Its pull request merged into staging, not main"* and points at the
  repo's card.

Both clear themselves the moment the branch shows up. Neither is an error — the
row shows them where you are already looking, under the waiting sentence, because
they are yours to fix rather than something that went wrong.

**Every row says where the work actually is.** Beside the branch and the sha, a
card carries a chip reading **merged into `staging`** — the branch on `origin`
this work most recently reached, tinted when that branch is the one the repo
ships from. It is the fact neither of the other two names: `branch` is where the
work was *pushed* and the live branch is what the checklist is *waiting for*, so
in a repo that ships through a `staging` or `develop` step a change spends most
of its life merged somewhere the row could not say. Hovering gives when it landed
and the trail it took (`staging` on the day it merged, `main` on the day somebody
promoted it). No chip means it has reached nothing but the branch it was pushed
to, which is the ordinary state of a checklist whose PR is still open.

The answer comes from ancestry — every `origin` branch that contains the commit,
ranked by which one it reached most recently, with branches that arrived in the
*same merge* folded together (every branch cut from `main` afterwards contains
that merge, and listing them all would answer with four names when one thing
happened). Where a **squash merge** left no ancestry to find, the branch its pull
request merged into is used instead. It refreshes every few minutes per plan on
one shared fetch per repository, and stops being asked once the work has reached
the branch its repo ships from.

**A checklist nobody has answered follows its repo's live branch**, so
re-pointing a repo from `staging` to `main` re-aims every unanswered checklist
within a minute — including forgetting a merge it had already seen into the old
branch, and pulling one that had gone **due** against the old branch back to
waiting (its due-ness was a fact about a branch you just stopped shipping
from). Once you have answered a step, or a run is in flight or finished, it
keeps the branch its answers were measured against, and the row says so.
Answers you clicked and clicked straight back off don't count as answers.

**What "live" means is per repo**, resolved first-non-empty:
`repository.verify_repo_settings[owner/name].live_branch` → `repository.live_branch`
→ `pr_base_branch` → `base_branch` → `main`. The dialog header names the flock-wide
answer and adds *· some repos differ* when any tracked repo overrides it; each row
carries the branch it was actually measured against. A checkout with **no GitHub
origin** cannot be named on the list (there is no slug to type) — its only opt-in
is its own committed `.mindflock.toml`:

```toml
[workspace]
verify_on_push = true
```

That is a team-wide, committed decision, it is OR'd with the tracked-repo list,
and neither half can switch the other off.

**The panel is Intake's anatomy, in Intake's order**: an intro line, the master
switch, **Sources**, the work list, then a fold for what you touch once — the
same shape as Intake → Pull requests / Issues, using the same classes, because
it is the fourth surface of that kind. The master switch
(`repository.verify_enabled`) pauses the **automatic** half only: no checklist
written on a push, and nothing new moved into the list. Repositories, checklists
and every recorded answer are kept, and writing one by hand, running one and
answering a step all keep working — exactly as a forced PR review still runs with
automated review switched off. **Sources** is the repository list; **Write a
checklist by hand** is the fold under the work list, for asking for exactly one
checklist by name with nothing configured.

**It offers sessions you have already CLOSED**, labelled `(closed)`, alongside
the open ones. A checklist outlives its session everywhere else in this feature —
the plan stores the main repo rather than the worktree, precisely so it can be
run and rewritten months later — and creation was the one half that still
demanded a live window, which made the button useless at the moment people
actually reach for it: after the work is finished and the window has been put
away. The branch has to still exist somewhere in the repo (locally or on
`origin`); merged-and-deleted says so rather than writing a checklist about
nothing. The one thing a closed session cannot supply is the **intent** — the
ticket text lives in the session's seed prompt and the transcript went with the
reclaimed worktree — so a checklist written this way is diff-only, and the
rewrite box is how you aim it.

**Each row is headed by what shipped, not by a branch name.** The model writes a
one-sentence summary alongside the steps — *"Search results can be filtered by
owner"* — and it sits under the session's title. A checklist coming due three
weeks later used to be `sc-1234-fix-filters` over a list of imperatives, leaving
the reader to reconstruct the change from the steps.

**Five groups, most urgent first.** **Not checked yet** (shipped, nobody has
finished checking it — this is the badge, by construction), **Steps failed**,
**Not shipped yet**, **Checked**, **Couldn't be written**. Each row carries one
plain-English sentence saying whose turn it is and one button that does what the
sentence says; everything rare is in the row's ⋯ menu.

**Finding one, and acting on several.** The list grows by one checklist per
session branch per repo, so it has Recently-closed's two controls, reused rather
than re-cut: a **Ctrl+F filter** (every whitespace-separated token has to appear
somewhere in the row — `sitecheck grafana` narrows without anyone remembering
the separator) and a **checkbox per row** with a tri-state select-all that
applies to what the filter is *showing*. The filter searches what the row shows
— ticket, summary, branch, sha — plus the repo path and **what the steps say**,
because a checklist is remembered as "the one about the Grafana collage board"
long after its number has stopped meaning anything.

Selecting rows raises the bulk bar: **Run N** (only the ones a run would
actually take — a checklist that is still generating, one an agent is already
working, or one with no agent step in it is counted out in the label rather than
discovered as a wall of 409s) and **Delete selected**, which asks first and lists
what it is about to destroy. Each acts through the same per-plan route the row
does, so every refusal a single press would have earned still applies, and the
bar reports the tally: *"Started 3 of 5"*, with the other two's reasons in an
error card. Shift-click extends a range; a selection the filter is hiding is
counted in the bar so a bulk delete can never quietly reach past the screen.

**Where each checklist has got to, before you open it.** A row's sentence says
whose turn it is; the **tally** beside its branch and sha says how far along it
is — `✓5 ✗1 ●2 ○1`, the same glyphs and colours the step rows and the roll-up
inside the plan use, so the three renderings cannot drift (and the whole thing
is announced as a sentence: *"9 checks: 5 passed, 1 failed, 2 need you"*). The
two groups that are an ask carry the same arithmetic on their **heading** —
*"Not checked yet · 7 · 3 steps need you"* — because a collapsed group was a
number of checklists and said nothing about how much work was in them, or
whether any of it was red. Triage used to mean expanding all seven.

**A run that died says so on the row.** Starting a run answers before anything
has been provisioned — the worktree, the branch and tmux all happen afterwards —
so a failure arrives minutes later, on a background task. It lands on the plan,
and the row now carries the first sentence of it (*"The verify session couldn't
start."*) after whatever it says next, with the raw git or tmux line kept for
the expanded body where there is room for it. Before that, `fail_run` put the
plan back to **Not checked yet** and the row read exactly like one nobody had
pressed Run on. The failure is retracted when a later run reports, when the run
is cancelled, and when the next one starts.

**The sidebar bar says when something shipped broken.** The **Verify** dot goes
red with a `✗N` count beside the due one when a checklist has a recorded
failure. It is deliberately a separate number rather than added to the due
count: *"3 to check"* and *"1 of them is broken"* are different questions with
different urgencies, and a badge that answered both would answer neither. A
failure is an answer, so it stays until the thing is fixed and re-checked.

**Answering steps.** Expand a row and each step is labelled **you** or **agent**:
`agent` is anything settleable from a shell or the agent's own tools — log
searches, dashboard panels and metric queries included, via its Grafana MCP
tooling — `you` is visual judgement, a real browser, or a service the agent has
no tool for. Three answers, and all three count as *your* answer:

- **Pass** — it did what's expected.
- **Fail** — it didn't.
- **Can't check** — you went and looked and could not get to it (staging is down,
  you have no account on it). Recorded with a reason, and **never** counted as a
  pass: the checklist stops asking you, but its verdict stays *partial* and the
  **Checked** heading says so (`N you couldn't check`).

Pressing an answer is idempotent; **Undo** beside it is the way back. An answer
the *agent* recorded shows as `blocked · agent`, which is it saying "a person has
to do this" — that one keeps the row open. You can answer your own steps while an
agent is still running the rest: your answers win, and the plan closes when the
agent's run lands rather than stranding it.

**A checklist reads like a checks panel.** Above the steps is one roll-up line —
`8 checks · 5 passed · 1 failed · 2 need you` — and every step carries its state
as a mark in a fixed column on the left (`✓` passed, `✗` failed, `●` waiting on
you, `–` you couldn't check, `○` not checked yet). The roll-up is *counted from*
the marks rather than computed beside them, so the summary and the column cannot
disagree.

**Answering is a keyboard job.** **Answer N steps** scrolls to the first step
that is yours *and focuses it*; on a focused step, `1`/`p` pass, `2`/`f` fail,
`3`/`b` can't check, `u` undo, `n` note. The legend under each checklist says so,
because none of it is discoverable. Keys typed inside the note box are left
alone.

**Running a checklist costs a real session.** **Run with an agent** provisions a
worktree, checks out the live branch and works every step it can — minutes, not
seconds. The row says the split before you press (`An agent can check 8 of 11;
the rest are yours`), and a checklist whose every step is yours offers **Answer**
instead, because an agent is forbidden from settling one. **The run is watched
inside the checklist**: its terminal streams into the expanded row, read-only, so
the steps it hands back are answerable on the same screen you are watching. ⋯ →
**Open the session in its own window** puts it on the grid instead, for keeping
it while you work elsewhere; closing either does not stop the run.

**A run that cannot start, or cannot finish, ends in words.** Everything about
this step happens after the request has been answered, which is why each of
these used to be silent:

- **A leftover from the last run of the same checklist.** The session's name is
  derived from the plan and the commit, so its tmux session and its branch have
  the *same* name every time — and one orphan (the app killed mid-run, a window
  deleted while detached, a lost record) made every later run die with `tmux
  session already exists` or `already used by worktree`, permanently, for that
  checklist. Both are now cleared before the session is created. **Fix what
  failed** got the same treatment with one deliberate difference: a fix
  session's whole job is to change its tree, so its leftover is reclaimed only
  when it is provably pristine — and when it is not, the press is **refused in
  words naming the directory** ("reopen it from Recent to finish or discard that
  work") rather than dying minutes later with a raw git line in the
  notifications bell.
- **Two presses at once.** Run and a per-step Re-check start the same request, so
  two of them for one plan raced through a single session title: one create won,
  the loser's failure was recorded against the winner's plan, and the winner's
  agent was left running with nothing owning it — which is where the orphan
  above comes from. The title is now claimed under the engine's lock, a
  background start only ever clears *its own* record, and the buttons withhold
  themselves while a start is in flight.
- **A repo that has moved.** A checklist stores the main repo so it outlives its
  session; when that path is gone the run — and **Fix what failed**, which had
  it worse, since it would have started an agent to repair code that was not
  there — is refused in words, rather than creating an empty folder and starting
  a real agent in it.
- **An open verify session whose workspace was cleared.** The record outlives the
  worktree, so "re-check one step" answered *workspace no longer exists* for ever;
  the husk is closed and a fresh session takes its place.
- **A run that never comes back.** The agent's window dying (a usage limit, a
  killed pane) is noticed within a couple of minutes instead of waiting out the
  two-hour deadline, and either way the plan is released **with the reason on
  it** and a `session.test_plan_gave_up` event — a billed session that ran for
  two hours and reported nothing used to be indistinguishable from a button
  nobody pressed. The abandoned session is then closed by the sweep like any
  other stray.

**What the agent is told about the environment** is now true, which it was not
before. A fresh worktree has nothing running in it, so the run prompt says so and
tells the agent to start the product (this session's own port block, or whatever
the repo's standing instructions name). And *"I could not reach the product"* is
**blocked**, never **fail** — a fail recorded against working code is the one
thing that makes a checklist stop being believed.

**Where "it works" is checked** is the repo's **Where it runs** field on its
Sources card. Set it to the deployment your users reach and both the checklist
and the agent working it are aimed at that running system. Leave it blank — the
right answer for a library, a CLI, or anything with no environment to point at —
and the run exercises a fresh checkout of the live branch on this machine, which
is what a green checklist then means.

**Two one-way doors**, both confirmed inline before they happen: running a
checklist **before it ships** (⋯ → **Check it early**) checks out its own commit
rather than what users have, and hand-answering every step of a checklist that
has not shipped closes it. Either way it will not come back to **Not checked
yet** when the branch does ship.

**The checklist is written for what the work was ASKED to do.** The ticket — its
title, description and acceptance criteria — is snapshotted onto the checklist at
push time and is the thing the steps are judged against; the diff supplies the
mechanism (the exact route, flag, button label, field). That ordering is the
whole point: the earlier version treated the diff as the only authority, which
made checklists that described a patch rather than testing a feature. The
snapshot matters as much as the ordering — read live off the session, the ticket
was gone by the time anyone pressed **Rewrite**, so the second draft ran on less
evidence than the first.

**Only the part of the change worth reading reaches the model.** The diff is
selected rather than truncated: files are ranked by how much they actually
changed, generated bundles and lockfiles are dropped by name, and about ten files
are shown properly with the rest listed as omitted. Before this it was the first
24,000 characters of `git diff` — which git emits in *path order*, so on a wide
branch the model read the alphabetically-first handful of files and never saw the
feature at all.

**The session's own conversation is used too** — filtered hard: the most recent
turns only, whole turns dropped if they are too big to be speech, if they carry an
output contract (a previous MindFlock one-shot looks exactly like one), or if they
match a credential shape. Your own turns are read as the sharpest statement of
what mattered; none of it is treated as evidence that anything exists, so a
feature discussed and abandoned cannot become a step. It is snapshotted with the
checklist, so a rewrite sees what the first draft saw. Turn it off flock-wide with
`repository.verify_use_conversation`.

**Every step is an input and an output.** A step names the exact input — the
request and its body, the values typed in, the message sent — and the exact
observable result: a status code and body, a number on screen, a row, a log line.
It never spends itself getting the product running: whoever works the checklist
starts it first, and that is not a check. Where a step needs the system in a
particular state it says so as a *condition* (“on an order that already has
SAVE10 applied”), never as a command to run first. Before this rule, real
checklists opened with `docker build …`, `uv run pytest …` and
“start the service on port 8080” — environment plumbing dressed up as
verification, and a list that asked a person to launch processes rather than to
compare a result.

**Every step is written to stand alone** — it names the endpoint, the payload,
the URL or the account it needs, rather than saying "repeat the above". That is a
rule in the generator's prompt, and it exists because nothing reads a checklist
top to bottom: **Answer N steps** scrolls straight to the first step that is
yours, **Re-check this step** runs one of them on its own months later, and a
step that leaned on a value the agent held while it ran is not recoverable from
the checklist at all. Checklists written before this rule keep their old steps —
**Rewrite the checklist…** re-asks the model about the same diff.

**You can add steps of your own, and fix the ones the model wrote.** ⋯ → **Edit
this step** changes the wording in place; ⋯ → **Let the agent check this** / **I'll
check this one** moves it between lanes. Editing a step makes it yours, so the
next rewrite keeps your wording — and changing what a step *asks* drops any answer
already recorded against it, while changing only who answers keeps every one.
Before this, correcting a single wrong sentence meant rewriting the whole
checklist: a model call, three minutes, and every answer against every step that
changed. The actor toggle is also the only way out of a checklist an agent cannot
run at all — an unrecognised actor is read as `human`, and a run is refused when
every step is a person's.

**Rewriting asks for a second draft, and lets you say what the first got wrong.**
⋯ → **Rewrite the checklist** opens a box in the row — *"What should it check
instead? — e.g. focus on the coupon flow at checkout, ignore the settings
refactor"*. That note is stored on the checklist, so a later push keeps honouring
it, and it outranks the model's own judgement about which part of the change
matters; it can never change the format the model has to answer in. Steps you
wrote or edited are kept. A rewrite **cannot un-ship a checklist** (one that has
already gone live comes back where it was, and its "it shipped" push is never
re-sent), **cannot destroy one** (a rewrite that fails leaves the steps you had,
in the queue, with the reason shown above them), and is **refused while an agent
is running it**.

## Settings (⚙)

- **Cursor auto-adopt** — adopt Cursor-opened workspaces as sessions.
- **Per-session budget (USD, 0 = off)** — cost guardrail: when a session's
  estimated cost crosses this figure the server emits a one-shot
  `session.budget_exceeded` event → warning toast (click focuses the session),
  a notification on every enabled channel (desktop and/or ntfy — see
  [Notifications](#notifications-)), and shell hooks. An over-budget session's
  pane shows a lock overlay with a **Raise budget** action
  (`POST /api/instances/{title}/budget/raise`); sends are refused (409) until
  raised.
- **Connections** — one-screen status of every external integration (GitHub,
  ticketing provider, agent CLI, tailscale) with re-test buttons
  (`GET /api/connections`). A card's **Configure** button opens the screen its
  `settings_screen` names — for GitHub and ticketing that is now an [Intake](#intake)
  tab, via the legacy-key hand-off.
- **Local model** (screen key `localmodel`) — run sessions against a model served
  on this machine: an on/off switch, the server (**Ollama** / **LM Studio** /
  any OpenAI-compatible), its base URL (blank = that runtime's default) and the
  model. **Test connection** (`POST /api/settings/test/local-model`) does three
  things at once: confirms the server answers, lists the models it actually serves
  — which turns the model field from "type the exact tag" into a dropdown — and
  names which installed CLIs can be pointed at it. It also states plainly that
  Claude Code has no local route, because a session silently using its hosted API
  is the one outcome the privacy claim cannot afford to be quiet about.
- **Advanced → Engine → Ticket sessions in MindFlock** (`engine.enabled`,
  **default on**) — where ingested tickets land. On: each one becomes a MindFlock
  session with its own worktree, branch, seeded agent, stage badge and guided git
  bar. Off: a detached tmux session plus an OS terminal tab, with no session in
  the app. Takes effect the next time the ingestion pipeline starts; it is the
  same switch as `[mindflock].enabled` in `config.toml` and overrides that file
  (see [configuration.md](configuration.md)).
- **General → Resume sessions when usage comes back**
  (`general.resume_on_usage_reset`, **default on**) — whether a session parked
  on its CLI's usage-limit screen with an empty queue is told to `continue` once
  the provider's window reopens (see
  [Send a message / prompt queue](#send-a-message--prompt-queue--per-pane) for
  the gate it shares with `wait_for_limit`). Off leaves it parked; the "Usage comes back"
  notification fires either way.
- **General → Take a break** (`mf_break_on` / `mf_break_every`, **off by
  default**) — one switch whose whole description is the sentence it configures:
  *Reminder to take a break every `[N]` minutes*, with N editable in place
  (5–480, default 60). When it fires, a card asks you to get up, with the
  murmuration from mindflock.ai flying over your grid.
  **Snooze 5 min** pushes it back five minutes; **Resumed work** (or
  Escape) restarts the whole interval. The clock is wall-clock and survives a
  reload — a refresh is not a way to dodge a break — but changing the interval
  re-arms it from now. **There is no scrim**: your grid, sidebar and panes stay
  exactly as you left them and the flock flies over them, the same treatment the
  idle overlay gets; the card is the only new thing on screen. The overlay still
  swallows the pointer and the keymap stands down with it, so you can watch your
  agents work but can't join in until you answer — a break rather than a
  suggestion. Sessions keep running throughout, and the card hands the keyboard
  back to whatever had it. The flock is a round trip through the logo, and a
  lopsided one. It streams OUT of the MindFlock mark in the top bar over
  **twenty seconds** — birds leaving one at a time, each flying its own short
  arc and joining the live flock the moment it lands, so the window keeps
  filling for as long as you stay away from it — and folds back INTO the mark in
  under a second when you dismiss it. Dismissing mid-stream takes over from the
  emergence rather than queueing behind it. The idle overlay arrives and leaves
  the same way; birds that simply materialise everywhere read as a bug, birds
  that come out of the logo read as the app's own.
- **General → Idle flock** (`mf_idle_flock` / `mf_idle_after`, **on by
  default**) — **The idle flock** is what MindFlock does when nobody is
  looking. The row is the same sentence-with-a-field shape as the break row
  above: *Fly the flock over your grid after `[N]` minutes with no click,
  keystroke or scroll in this window*, with N editable in place (1–480,
  **default 10**). Unlike the break card this one is on out of the box,
  because it can only appear once you have already walked away from the
  window. The flock flies across the whole
  screen, ignoring the sidebar and pane boundaries entirely. It is
  `pointer-events: none`, so nothing is covered or blocked, and the first click
  or keystroke sends it home. Two things stand it down: the switch, and the
  break card, which brings its own flock. Turning the switch off mid-flight
  sends the birds home through the logo rather than cutting them off. Three
  things deliberately do not count as you being present:
  - **Moving the mouse.** `pointermove` is not in the activity list at all, so a
    cursor drifting over the window — or parked on it while you read something
    else — neither delays the flock nor dismisses it.
  - **Agent output.** That is the machine working, not you.
  - **Anything you do in another app.** Neither `focus` nor `visibilitychange`
    resets the clock, so MindFlock idles behind you while you work elsewhere — a
    second monitor fills with birds, which is the whole point — and the click or
    keystroke that brings you back is what wakes it.

  Under `prefers-reduced-motion` both surfaces show a settled flock rather than a
  moving one.
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
- **Mobile** — the `/m` URLs and QR code (`GET /api/mobile`), plus the
  **tailscale mode** toggle. Which interface uvicorn binds is fixed at process
  start, so that toggle only means something after a restart — and turning it
  **on** now takes that restart itself: `POST /api/settings` answers
  `{"restarting": true}`, the screen waits for the server to come back and
  refreshes the URLs/QR in place. It gives up after three attempts (counted in
  the environment, since each attempt is a new process image) and leaves the
  manual **Restart server to apply** button, rather than restart-looping a
  server that isn't going to come up on the tailnet. The same check runs at
  every boot, so a server started in local mode while the setting says tailscale
  corrects itself. The tailnet URL here is also the natural paste target for
  *Tapping opens* on the [Notifications](#notifications-) screen — though pushes
  now carry it automatically, so that field is only needed to send taps
  somewhere else. One wrinkle is worth knowing before you blame the link: the
  URL is offered
  with `?token=…` baked in so a scan lands signed in, and MindFlock **strips that
  token** when saving it as an ntfy click URL (it would otherwise be stored on the
  ntfy server). A tap therefore only lands signed in if that device already holds
  the `mf_auth` cookie — i.e. you scanned the QR there once before. Otherwise it
  lands on the login prompt, which is the intended trade: one extra tap on a new
  device instead of this machine's token sitting on a third party's server.
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

**Diff tab** — a third tab beside Agent/Shell showing the session's diff, so a
phone can *approve* work and not just unblock it. It reads the same
`GET /api/instances/<title>/diff` the desktop Diff tab does, with the same two
baselines behind one button: **All changes** (`base=fork` — everything vs the
branch's fork point, matching the header badge) and **Uncommitted**
(`base=head`), persisted per browser under the same `mf_diffbase` key. Unified
only — a split view needs width a phone hasn't got — colorized like the desktop
(`@@` cyan, `+` green, `-` red), with each file's name lifted out of its
`diff --git` line into a header that sticks while its hunks scroll past. Long
lines scroll sideways inside the panel rather than wrapping (a wrapped diff line
loses its shape) and the page itself never scrolls horizontally. Very large
diffs are truncated with a note pointing at the desktop. The Diff panel is a
*view*, not another terminal: the PTY stays attached behind it, so switching
back is instant, and the git action bar stays visible (with its status toast
following you into the tab) because reading the diff is exactly when you decide
to push.

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
budget exceeded — rules are data-driven from `GET /api/notify/config`, and the
same rules drive the server-side ntfy push in `web/core/ntfy.py`) is the
reference for this generic path — the full contract lives in
[docs/extensions.md](extensions.md). `slots.js` also feeds the provider list
into the New-session dialog's Program field.

`core/ws-xterm.js` exports `WsXterm`, the shared xterm↔WebSocket bridge used by
addon panes (binary PTY frames in, resize JSON out, auto-reconnect except on
4404/4409).
