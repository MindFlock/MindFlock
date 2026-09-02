# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] - 2026-09-02

### Added

- **Window rows are first-class sidebar rows.** The Assistant chat, the log
  tails, verify watch windows and extension panes now behave exactly like
  session rows in the rail: they carry the same `Ctrl/Alt+1…9` number badge
  (and those shortcuts focus them), drag to reorder anywhere among the
  sessions — one saved order holds both, and the grid's slot order follows
  it — are toured by `Ctrl+Tab`, and narrow under the sidebar filter. A
  reordered assistant even reopens where you left it. En route, dragging a
  session row no longer silently erases the saved position of rows absent
  from the current snapshot (e.g. a sleeping remote device's sessions): the
  drop merges into the saved order instead of replacing it.

- **"Your PR was approved."** A notification when a reviewer approves the pull
  request of a session you have open — the moment that is actually waiting on a
  human, and the one the app could not see before. The verdict is read from the
  pull request's own review list (latest review per reviewer; a standing
  "changes requested" outranks an approval; a comment is not a decision), since
  GitHub's `mergeable_state` says `blocked` both for a missing review and for a
  failing required check, and says nothing once the approval lands. Off the
  same rule list as every other notification, so it reaches the browser and
  ntfy alike, and there is an opt-in twin for "changes requested". New event:
  `session.pr_review_changed`.
- **Resizable columns in the database client.** Drag a header's right edge in
  any result grid (the SQL results and the table view are the same grid);
  double-click that edge to fit the column to its widest value. Widths are
  keyed by column name, so they survive a sort, a page turn and a reload of the
  same query. Columns now also START at the width of their own NAME rather than
  their content: a header you cannot read is worse than a value you cannot, and
  the value is one double-click away.

- **One page for closed work and disk.** **Recently closed** now lists closed
  sessions *and* the workspace directories no closed session accounts for — the
  separate "Workspaces on disk" manager is gone, and the top bar's **Recent**
  dropdown is a single button. Rows carry both identities (branch/session name,
  what the directory is, when it was last used, its size) with the same filter,
  sort and bulk selection the list dialogs share. Protected base clones and cache
  refreshers are no longer rows at all — they are counted and named in the header
  instead, along with anything a live session is using, which the page leaves to
  the sidebar. New: `GET /api/recent`.
- **Remove unused worktrees**, in that page: one sweep for every worktree no
  session is using and nothing has touched for over a week
  (`POST /api/workspaces/prune-worktrees`). It can only ever remove a worktree
  *git generated* — the test is that the directory's `.git` is a gitdir FILE, as
  `git worktree add` writes it, so a repository, a clone, a `_base_*` mirror, a
  `pr-*` review clone and any folder git did not make are all excluded
  structurally, and nothing outside MindFlock's own worktrees root is looked at.
  The candidate list is resolved on the server (never a path the page sends) and
  re-verified immediately before each delete; the confirmation names every
  directory and says what a removal costs, since a worktree's branch and commits
  live in the repository it came from and survive. Anything holding uncommitted
  work, or a detached HEAD no ref contains, is held back unless a second
  confirmation includes it. Staleness is the newest of the directory's mtime and
  the worktree's own git index/HEAD/reflog — the directory's mtime alone runs days
  behind real use.

- **Extensions (Addon API v3).** An addon can now contribute UI the way a
  VSCode extension does: one draggable/hideable sidebar bar with buttons,
  commands in the command palette, and dialog and grid-window surfaces whose
  bodies it renders into host-owned keep-alive containers — so typed text and
  unsaved edits survive a grid drag, because the drag remounts the window's
  React shell and never touches the extension's DOM. Everything is declared
  in a static manifest served by `GET /api/addons`, which is what lets a bar
  button and a palette entry exist, and a declared dialog open, before the
  extension's ES module has loaded; the module is imported lazily on first
  use, `activate(api)` runs once against a small frozen API object, every
  registration returns a disposable, and every callback runs in a host
  `try/catch` attributed to the extension — a broken extension toasts once
  and shows its error on its Settings row instead of taking the app down.

  User extensions are discovered once at startup from
  `~/.mindflock/extensions/<id>/extension.py` (`build(ctx) -> Addon`; an
  optional `frontend/` is served at `/extensions/<id>/`), with ids that
  collide with the core API refused, and toggled on **Settings →
  Extensions** (`extensions.disabled` in settings.json — the only
  per-extension state that file ever carries; an extension's own
  configuration is its own file). A disable tears down the frontend
  completely; a discovered extension's backend stays loaded until the next
  restart, which the row says out loud. The manifest's `api_version` is the
  minimum host API level the module needs, so an extension written against a
  newer app fails with one clear message rather than a `TypeError` mid-click.
  Existing addons are untouched. Reference: docs/extensions.md § 4, including
  a two-file starter extension.
- **Database Client**, the first extension — a **Database** bar (Explorer,
  SQL) with SQLite, PostgreSQL (psycopg) and MySQL (pymysql) connection
  profiles kept in `~/.mindflock/dbclient.json` (0600, passwords never sent
  back; read-only profiles enforced at connect), a lazy schema tree, an
  editable table grid (sort, filter, paging, insert/update/delete previewed as
  SQL and applied in one transaction with stale-row detection; views and
  tables without a primary key read-only, and say so), a SQL query pad
  (statement at cursor, run all, history, a needs-confirmation guard for a
  no-WHERE `UPDATE`/`DELETE` judged after stripping string literals), and
  CSV/JSON export. Every statement goes through one server-side chokepoint:
  single statement only, 1–300 s timeouts, a 10 000-row cap, identifiers
  validated against the introspected schema, values always bound. Drivers are
  detected, not required — the connection form names a missing one and offers
  an **Install driver** button that puts it into the environment already
  running the app (`uv pip install --python <this interpreter>`, falling back
  to pip), re-checks importability in the live process so nothing restarts,
  and shows the installer's own output when it fails. Where an in-app install
  would mean breaking a system-managed Python it says so and keeps to the
  copyable command.
- **Database Client: an IDE-grade tree, the query unified with the table, and
  in-place table detail.** The explorer tree now groups a schema's objects
  under **Tables (n)** / **Views (n)** nodes, tables unfold into their columns
  (type badges, a key on the primary key), databases and schemas carry a size
  badge where the engine answers it from cheap statistics (`pg_database_size`,
  summed `pg_total_relation_size`, `information_schema` lengths — never a
  scan), and every scope row grows hover actions (New query here, Refresh).
  The table view and the query are one page: a **SQL bar** above the grid
  shows the SELECT behind it and is visibly rewritten by every sort click,
  filter, page turn and page-size change; edit it and Ctrl+Enter runs a
  custom query in the same grid (read-only, with a badge), and any managed
  action — or the Table button — returns to the page. Selecting a table in
  the explorer embeds that unified view right where the column summary used
  to be; the old **View data** button is now **Open as window**, which opens
  the same view as a grid window.
- **Windows in the sidebar.** Every open non-session window — MindFlock
  logs, System logs, the Assistant chat, a verify watch window, an extension
  pane such as a database table — now gets a row under a **Windows** header
  at the tail of the session list, closable with the same ✕ a session row
  has; clicking a row scrolls its window into view. The panes themselves no
  longer draw their own Close buttons — the rail is the one place windows
  are controlled, exactly like sessions.

### Changed

- **Every window closes from its own header.** Each grid window — session panes
  included — now carries a ✕ at its top right, next to the copy-all button. It
  is the same action as the window's sidebar row: session windows hide (the
  session keeps running), special windows close (a verify run keeps going).
- **Window headers scroll sideways instead of clipping.** In a narrow pane the
  tab bar and chips used to be cut off past the right edge — and the old
  narrow-pane rules hid the usage chip and live-step text entirely. Everything
  now stays rendered and reachable by horizontal scroll, while the history /
  copy-all / ✕ cluster stays pinned to the right edge.
- The Database bar no longer has a **SQL** button; the query pad is still one
  step away via the Explorer, the command palette (`Database: SQL query pad`),
  and each table view's SQL bar.
- Deleting a workspace now also has to name a directory the server *lists* as a
  workspace — a flat child of a provisioning root, or a worktree leaf. A deeper
  path (`<workspace_dir>/_base_repo/src`) used to pass every guard, because the
  containment check accepted any descendant while the base-clone protection
  looked only at the last path segment. This is the "direct child" rule the
  endpoint and its docs always claimed.
- Deleting a worktree now prunes the stale registration from the repository the
  worktree's own `gitdir:` pointer names. The old test ("is my parent directory
  called `worktrees`") is false for every branch name containing a slash, so
  nested worktrees left registrations behind, which then made a later
  `git worktree add` for the same branch fail. Empty branch-slug directories left
  under the worktrees root are collected too.
- Wiping a closed session's worktree is refused with **409** when a still-running
  session shares that directory (a session and its copy keep one worktree).
- Non-session windows (the assistant, log tails, verify watches, an extension's
  table) are rows in the sidebar's own list instead of a separate "Windows"
  group, and they take a grid slot like any session: they share one cap and one
  MRU, so at "view: 1" the window on screen is the one you picked, and opening
  one selects it. Previously they were appended after the cap, so "1" meant one
  session plus however many windows happened to be open.
- The database client's CSV and JSON downloads are one control instead of two
  buttons that broke onto separate lines in a narrow toolbar.
- Forgetting a closed session re-reads the store before writing it back, so
  deleting several rows at once no longer resurrects the entries whose
  directories just went (each would have offered a Reopen that could only 410).
- **An ingested run's commits get a written message, not the ticket's title.**
  A fast-track run started from a ticket, PR or issue used to commit under the
  item's own name — the work as *requested*, repeated verbatim on every commit
  it made. The item's name is now the fallback: at commit time, with the diff
  final, the run asks the same generator the ✨ **Write it** button uses, and
  the item's id and name go along as context. If no model answers (a CLI with
  no text-only mode, a logged-out one, a timeout), the commit still lands under
  the item's name exactly as before.
- Cancelling a commit with `Ctrl+C` clears the "pre-commit" chip within a poll
  instead of leaving the session looking wedged for most of a minute. The
  commit chain now drops its own lock from a trap, so an interrupted commit
  cleans up at the moment it is interrupted rather than waiting for the
  stale-lock self-heal (which stays, for what a trap cannot catch — a `SIGKILL`,
  a killed pane).
- The ✨ **Write it** buttons (Commit, and the verify-plan writer) show a
  spinner while a message is being written. "Writing…" alone read as a dead
  button on a cold CLI start.
- **Shift-click extends the selection in the database client's grid.** Tick one
  row's checkbox, shift-click another, and everything between takes the state
  the clicked box just took — so it unticks a run as readily as it ticks one.
  Deleting twenty rows was twenty clicks. The anchor is the last row you ticked
  plainly, and it survives the extend; a new page of rows clears it.
- **The web UI is built with Vite 8** (Rolldown + Oxc, replacing Rollup +
  esbuild), which also unblocks `@vitejs/plugin-react` 6 — the plugin major
  that 0.1.x had to pin away from because it requires vite 8. The shipped
  bundle is smaller for it: 2.00 MB to 1.77 MB, 458 kB to 371 kB gzipped,
  most of it source comments that the old toolchain dropped and the new one
  preserved until told not to. Nothing in the UI changes.

## [0.2.1] - 2026-08-28

### Added

- **Sessions can run as different identities — a second Claude subscription
  beside a work one, a metered API key, an OpenRouter key — without logging any
  CLI out.** An **auth profile** names one identity, and every session runs
  under exactly one of them or under none, which is each CLI's own ambient
  login and remains the default. Three kinds: an `account` is a second login of
  the CLI itself, kept in its own isolated config dir and reached through
  `CLAUDE_CONFIG_DIR` / `CODEX_HOME`; `api_key` and `openrouter` inject a key at
  launch, the latter through OpenRouter's Anthropic-compatible endpoint. Any
  profile can also carry raw `env` overrides, which apply to any CLI at all —
  the escape hatch for a user-defined provider the typed kinds have never heard
  of. A combination with no route (an OpenRouter profile on `cline`, say) is
  reported out loud and left on the CLI's own login rather than launched with
  invented env.

  Set them up in Settings → **Accounts** or with `mindflock accounts`, which is
  the same store and goes through the running server so the app picks a change
  up immediately. An `account` card shows the login command to paste into a
  terminal, because the CLI's OAuth flow is interactive and cannot run through
  an API; an OpenRouter card's **Test key** reports the key's real spend and
  turns its model field into a picker over the models that key can actually
  reach. Pick a session's identity in the **New session** dialog — which also
  steers the agent picker to a CLI the chosen account can route — or swap a
  *live* one from the `@account` chip in the pane header: the agent restarts
  under the new identity while the worktree, diff and shell pane survive. A
  swap starts a fresh conversation, since a transcript belongs to the account
  that created it, so each window keeps a thread per identity.

  Secrets live in `~/.mindflock/settings.json` at mode 0600 and read back
  masked, and a save that resends the mask keeps the stored value. A key
  reaches its CLI through a per-run file rather than argv, so it never lands in
  `/proc/<pid>/cmdline`. A local model still outranks a profile — a session
  pinned to this machine cannot be pulled off it by an account pin — and with
  no profiles configured every overlay is empty and every launch path is
  byte-identical to before the feature existed. Full guide:
  [docs/accounts.md](docs/accounts.md).

- **Notifications carry the session's rail slot: "[3] sitecheck-bot7 has
  finished."** The sidebar numbers its first nine rows, and those numbers are
  how people locate a window — so the desktop notification, the bell feed, and
  the in-app toasts now lead with the same number the rail shows at that
  moment (drag order and the live filter applied, resolved client-side at
  render time, because the order is per-browser state the server never sees).
  Rows past the ninth, filtered-out sessions, and phone pushes — which have no
  rail to agree with — carry no number rather than a wrong one. Extension
  pages get the same resolver as `window.mindflock.slotNumber(title)`.

### Changed

- **The whole window state flow got significantly tighter — where the evidence
  earns it.** "Your agent has finished" used to take ~55 seconds from the last
  keystroke of a turn, for every agent, in every situation: a flat 45-second
  dwell chosen as a blanket over three different hazards (a human typing a
  follow-up, a queued prompt in flight, a fast-track chain mid-step), plus the
  activity settle and a poll tick. Each of those hazards is now checked
  *exactly*, so the padding survives only where the evidence is weak.

  A hook CLI's finished run (Claude, Codex) now announces in **~15 seconds**;
  a pane CLI's in ~35; the CPU backstop keeps the full 45. The chip — and the
  default-on "needs your input" push — react within **one tick** when the
  CLI's own hook reported the state, because a hook marker cannot misread a
  frame the way a pane capture can (the 3s settle stays for pane-derived
  readings, and for "ran out of usage", whose banner detection is a pane fact
  whatever triggered it). Queued prompts go out one drain pass sooner on a
  hook-reported idle. The exact gates that replaced the padding: recent human
  input (the terminals, /send, send-now — and tmux's own client activity, so a
  conversation held in a raw tmux attach doesn't buzz per turn), a send-grace
  covering the window where the last queued prompt is typed but not yet picked
  up (previously covered only by the blanket — announcing there was possible
  before this release), and fast-track's own record, bounded by its driver
  lease so a wedged chain cannot mute a session forever.

  The turn's END also got a harder look at *why* it ended: the working→idle
  hook transition now forces a fresh usage-limit probe (a miss never refreshes
  the probe throttle), closing a window where a turn cut short by the cap
  could be announced as finished — including the variant where the banner left
  the screen before the throttled re-check ever saw it, which no amount of
  dwell used to fix. And the pane layer's proven-busy bookkeeping is cleared
  by every non-working reading, so a busy run can no longer straddle an agent
  death or a relaunch and re-arm off stale evidence.

### Fixed

- **Database Client: the tree indents for real.** Row depth was set as a CSS
  custom property through `Object.assign(el.style, …)`, which is a silent
  no-op for `--` properties (no named setter on `CSSStyleDeclaration`) — the
  entire tree had been rendering flat at one padding since it shipped. The
  element builder now routes `--` keys through `style.setProperty`.
- **Database Client: "Add connection" opens the form instead of failing.**
  Opening the explorer on the new-connection form (the palette command, or a
  saved link with `ref="new"`) called into the form before the dialog's root
  element existed — a temporal-dead-zone `ReferenceError` the host reported
  as "surface failed to render". The jump now happens after the dialog is
  built.
- **Database Client: hidden means hidden.** Elements toggled with the
  `hidden` attribute but styled with their own `display` rule (the grid's
  "No rows" note, the read-only badge) stayed visible — author display beats
  the UA's `[hidden]` rule — so an empty lock chip sat in the tabs row and
  "No rows" floated over full grids. One scoped `[hidden]{display:none
  !important}` guard restores the attribute for the whole extension.
- **Database Client: no more silent edit loss.** Typing a per-column filter,
  and (new) switching tables in the explorer while the embedded grid holds
  unsaved changes, now ask before discarding — the same confirm every other
  managed action already had; declining the page-size confirm also snaps the
  selector back. Saving with a cell editor still open now commits that cell
  into the batch instead of dropping it, and typing in a filter box no longer
  loses focus after every pause while the page reloads under it.
- **Database Client: stale-response and stale-cache holes.** A slow page
  response can no longer repaint over a newer one (or over a custom result) —
  loads carry a generation token; Refresh after a custom *write* statement
  reloads the table instead of silently executing the statement again;
  refreshing a scope in the tree also drops the cached table_info beneath it
  (stale column leaves); and refreshing schema "auth" no longer also forgets
  sibling "auth_archive" (prefix over-match). The Postgres/MySQL size
  queries run under a 5 s statement timeout and fall back to plain names.
- **Sessions no longer read "idle" while their agent is visibly working.** The
  activity hooks MindFlock installs into a repo's `.claude/settings.local.json`
  resolved their marker *directory* at install time — baked in as an absolute
  path — while resolving the session name at fire time. One sandboxed install
  poisoned the well: a Verify run executes with `HOME` redirected into a
  scratchpad, and when its MindFlock instance re-pinned the SHARED repo's
  hooks file, every cohabiting session's hooks began writing markers into a
  dead sandbox under /tmp. The chips froze on whatever the last pre-poison
  reading was — "idle", for a session that then worked for fifteen straight
  minutes — and the resume-thread binding drifted the same way, so the live
  `claude agents --json` signal (which knew the truth: `busy`) was consulted
  under a dead conversation id and answered nothing. Both directories are now
  resolved *inside the firing hook*, from the CLI's own environment: a
  sandboxed CLI writes to its sandbox, a real one to the real home, no matter
  who installed the hooks or where.

- **Archived Shortcut work no longer fills the Tickets panel.** A workflow state
  showing three stories in Shortcut listed ten in MindFlock, and the extra seven
  could not be found in Shortcut at all — because Shortcut hides them. Six were
  archived stories, which `/stories/search` returns and boards do not; the
  seventh was a live story under an archived epic, which disappears from the
  board with the epic it belongs to. Both are now filtered out of every Shortcut
  listing.

  The panel was the visible half. The same search feeds the ingestion pipeline,
  so a story archived while it sat in the configured ingest state could still be
  picked up and worked — archiving it being, presumably, how you meant to stop
  that. The epic lookup is one extra call per search, skipped entirely when no
  story carries an epic, and best-effort: if it fails you get the old, wider
  listing rather than an empty one.

- **The dev shell's notifications get their icon back.** A dev toast came up
  headed *MindFlock-dev* and badged with the blank white document tile — the one
  Windows draws when it cannot read the icon it was pointed at. The `.ico` was
  fine; the *path* was not. A shortcut's `IconLocation` is resolved by the
  Windows shell, later, in whatever process happens to be drawing a tile — and
  on the supported Windows shape the checkout lives on a WSL share, which the
  shell's icon extraction will not read. The window and taskbar icons were right
  all along, because Electron loads those itself with an ordinary file read,
  which is exactly why only the toast looked broken.

  The dev shortcut now keeps a copy of its icon on the local disk, in the dev
  profile beside the rest of the throwaway dev state, and points at that. It is
  byte-compared rather than rewritten, so changing `MINDFLOCK_DEV_ICON` is
  picked up and leaving it alone costs nothing; a checkout on a local drive
  keeps using its own file. The shortcut already on disk repairs itself on the
  next dev run.

- **"Your agent has finished" is now a claim MindFlock can back up.** The idle
  notification fired on the wrong fact. A session's chip goes grey when its CLI
  reports a turn ended, and a coding CLI reports that at the end of *every*
  assistant reply — so a ten-turn conversation announced "finished" ten times,
  a queue draining ten prompts announced it between each pair, and a window you
  merely clicked back open announced it for an agent that had not run anything
  at all.

  That last one is the one people saw most, and it was two bugs standing on each
  other. Opening a pane relaunches an agent whose tmux session has died — and
  the activity layer *threw away everything it knew about a session* on every
  poll while it was offline, so the relaunched CLI arrived as a stranger. Facing
  a pane it had never seen, the classifier assumed it was working, on the theory
  that a fresh agent usually is. It held the badge at **running** for eight
  seconds, decayed to **idle**, and rang the bell — a whole work cycle
  hallucinated out of a mouse click. The stale hook marker left behind by the
  *previous* run could do the same thing on its own: nothing deletes those, and
  they are trusted for six hours.

  Three changes, one per layer. The classifier **no longer guesses**: a pane it
  has never seen is read for the two things a single frame can actually prove —
  a usage-limit banner, and the interrupt hint that means a turn is live right
  now — and anything else is reported as idle, because nothing on that screen
  says otherwise. Its memory is now scoped to a tmux **incarnation** rather than
  wiped on every hiccup, so a session that briefly went offline comes back with
  its history intact, while a genuinely relaunched one starts clean — and a hook
  marker written before the current session existed no longer speaks for it.
  Finally, "the agent has finished" became its own event, `session.turn_ended`,
  which fires only once MindFlock has **watched that agent work**, seen it stay
  idle for 45 seconds, and confirmed nothing is queued to wake it. Once per
  cycle of real work, however long the session then sits there. A turn the
  usage cap cut short does not count — it did not finish, so the evidence is
  dropped rather than left armed to claim "done" the moment the banner scrolls
  off the pane.

  One more way to hallucinate a work cycle turned up after that, and it needed
  no mouse click: a session parked untouched for nineteen hours announced
  "finished". Its own auto-updater burned a single four-second poll's worth of
  CPU, which the classifier reads as **working** — a busy process tree is how it
  tells a thinking agent from a parked one — and twelve seconds later it was
  idle again with a full work cycle behind it. A `/clear`, a garbage collection
  pause, a stray compile: all of them look the same from outside. So "watched
  that agent work" now means **corroborated**, not merely *looked busy*. The
  CLI's own report counts, at any duration — a hook fires because a prompt was
  submitted or a tool ran. So does the interrupt hint on the pane, which is
  there *because* a turn is running, and which covers a long turn whose marker
  goes stale mid-thought. A busy process tree on its own no longer does: it
  still turns the chip green, and it no longer earns the right to interrupt you.
  This is not a Claude-only rule. Codex reports through hooks the same way;
  aider, antigravity, cline, goose and opencode have no hooks, so their status
  line is the signal — and because a regex against someone else's UI can simply
  be wrong, those keep a backstop: twenty unbroken seconds of CPU still counts,
  which no spike lasts. The two CLIs that report for themselves get no backstop,
  since a spike on a parked session of exactly that kind is the bug being fixed.
  A test pins which provider sits on which rung, so a CLI that quietly stops
  matching its own hint shows up as a failure rather than as silence.

  Everything that has to stay fast stayed fast. **Needs your input** and **ran
  out of usage** still fire on the instant signal — those are "come here now",
  and a dwell on them would be a bug of its own. The chip still flips the moment
  the state does. What changed is only which of those moments is worth
  interrupting you for.

  Two things fixed themselves on the way. The bell's own feed had no rule and no
  rate limit at all, so it logged "finished — now idle" even for people who had
  never turned the notification on; it now follows the same rule as every other
  channel. And the sidebar's **"idle 25m with unfinished work — possibly stuck"**
  warning turns out never to have appeared once: it reads a timestamp the app
  had never written under that name. It works now — and, on its first outing,
  it means what it says: a *committed* branch is finished work waiting for a
  push, not a stuck session, so only uncommitted output nobody is touching
  earns the row.

- **A "ran out of usage" push no longer swallows a "needs your input" push.**
  Both channels collapsed repeat notifications per *(session, event)* for five
  seconds, and three different rules ride the same activity event — so an agent
  that hit its usage cap and then asked a question inside that window sent one
  alert, not two, and it was the less actionable one. Worse in the browser: that
  same key is the notification's `tag`, which Windows and macOS treat as
  "replace the one already on screen", so a popup you were still reading was
  overwritten. Both are keyed by **rule** now, so each rule speaks for itself.

- **The break clock no longer counts the hours you were not here.** A deadline
  is wall-clock, so on its own it kept running through a closed window, a
  shut-down machine and a slept laptop — and whoever opened MindFlock in the
  morning was met immediately by the break card, its away-clock already at
  nine hours and still climbing. Time away from the app is time away from the
  desk, which is the break itself.

  The app now stamps a heartbeat while it is running, and a gap of more than
  five minutes starts the interval over rather than resuming it: on opening, and
  also mid-run, which is the only thing that notices a machine that slept with
  the window open — that case takes the stale card down with it. **Opening the
  app starts the countdown**, whatever yesterday left behind. A refresh still
  cannot dodge a break: the two are told apart by navigation type, so every
  reload path (F5, the palette, the reload every tab does when the server
  restarts, the API client's reload on a lost session) keeps its deadline, while
  a shell launch or a new tab is an open. The five-minute tolerance is wide
  because a hidden tab's timer is throttled to about one tick a minute, which
  must not read as an absence.

- **Windows dev builds head their notifications with a name.** A toast is
  labelled with the display name of the Start-menu shortcut registered for the
  app id that raised it. The prod app gets one from its installer; the isolated
  dev shell has its own app id and no installer, so Windows printed the raw
  `ai.mindflock.desktop.dev` across the top of every toast. A dev run now writes
  that shortcut itself — `MindFlock-dev.lnk`, rewritten only when missing or
  stale, best-effort — and the toasts read **MindFlock-dev**. It doubles as a
  working launcher, so it is also the thing to pin to the taskbar. Prod is
  untouched. Development-facing only; see `electron/README.md` and
  [docs/development.md](docs/development.md).

## [0.2.0] - 2026-08-25

### Added

- **Verify — "it merged" is not "it works".** The pipeline's last honest
  checkpoint was the merge, and merged is not verified: nobody has opened the
  product and looked at the thing. The agent's own green suite doesn't answer it
  either — that ran *in a worktree*, against *its* half of the repo, before
  anyone else's work landed on top. Nobody writes the check down, because at the
  moment you could write it the change isn't live yet, and at the moment you
  could run it the diff is a week old and the session has been deleted. MindFlock
  is the one process that sees both moments.

  So it writes the checklist at push time, holds it, and hands it back when the
  work is really live: top-bar **Verify** (`Alt+V`), with a badge for what is
  waiting on you. Track a repo (Verify → **Sources**, or its committed
  `.mindflock.toml`), and the first push of each session branch turns that
  branch's diff into steps. Each step carries an **actor**: `agent` for anything a
  shell can settle, `human` for visual judgement, a real browser, or an external
  service — and anything unrecognised becomes `human`, because an agent silently
  passing something it had no way to observe destroys the whole point. **Run**
  starts a real session that checks out the live branch and works its half, with
  the stream inline in the dialog, so watching the agent and answering the steps
  it leaves you are one screen instead of two windows and a round trip.

  **Merged is not deployed**, so ancestry starts a clock rather than handing you
  the list: the row reads *"Merged 4m ago — waiting for it to deploy"* until the
  repo's deploy window passes (**Deploy takes (minutes)**, default 5; set it to 0
  where merging is shipping, or skip the rest with **⋯ → It's deployed — check it
  now**). Checking too early is not merely early — you see the behaviour the
  change replaced and record a failure against correct code, which is the one
  outcome this surface cannot survive.

  Two model postures, deliberately opposite: writing a plan is an unattended
  read-only one-shot through the session's own CLI (`claude -p`, `codex exec`,
  no PTY, no skip-permissions flag — a question about the tree has no business
  editing it), while running one is an ordinary session you can watch, interrupt
  and take over. Plans live in their own `~/.mindflock/test_plans.json` because
  they **outlive their sessions**, and they are keyed per session branch: five
  pushes make one checklist, and a later push may rewrite it from the newest
  commit only while nobody has answered anything — an append-only checklist could
  never retract a step that a later commit made impossible to pass.

- **Every Verify card says where the work actually is.** Beside the branch and
  the sha, a checklist now carries a chip reading *merged into `staging`* — the
  branch on `origin` this work most recently reached — tinted when that branch is
  the one the repo ships from. It is the fact neither of the other two names:
  `branch` is where the work was pushed and the live branch is what the checklist
  is *waiting for*, so in a repo that ships through a `staging` or `develop` step
  a change spends most of its life merged somewhere the row could not say, and
  answering "is this in staging yet, or already in main?" meant leaving the app.

  Ancestry answers it — every `origin` branch that contains the commit, ranked by
  which it reached most recently, with branches that arrived in the *same merge*
  folded together, since every branch cut from `main` afterwards contains that
  merge and listing them all would answer with four names when one thing
  happened. "Most recently" is measured on each branch's own first-parent chain,
  which is what tells `staging` gaining the work at its merge from `main` gaining
  it at the promotion. Where a **squash merge** left no ancestry to find, the
  branch its pull request merged into is used instead. Refreshed every few
  minutes per checklist on one shared fetch per repository, budgeted like the
  liveness pass, and not asked again once the work has reached the branch its
  repo ships from.

- **Take a break.** Settings → General, right under Reduce motion: one switch
  whose description is the sentence it configures — *Reminder to take a break
  every `[N]` minutes*, with N editable in place (5–480, default 60). When it
  fires, a card asks you to get up, with the murmuration from mindflock.ai flying
  over your actual grid. **Snooze 5 min** pushes it back; **Resume Working** (or
  Escape) restarts the clock. Off by default. The countdown is wall-clock and
  survives a reload — a refresh is not a way to dodge a break — while changing
  the interval re-arms it from now.

  There is no scrim: the view stays exactly as you left it, sessions keep
  running, and you can watch them do it. What the screen does take is the
  pointer and the keyboard — every global shortcut stands down while it is up —
  so it is a break rather than a suggestion. The card hands the keyboard back to
  whatever had it.

  The flock is a round trip through the logo, and a lopsided one. It streams OUT
  of the MindFlock mark over **twenty seconds** — birds leaving the mark one at a
  time, each flying its own short arc and joining the live flock the moment it
  lands, so a few seconds in there is already a real murmuration by the edges and
  a stream still pouring out of the corner — and folds back INTO the mark in
  under a second when you dismiss it. A single twenty-second interpolation would
  have been twenty seconds with the flocking rules switched off, which reads as
  dead.

- **The idle flock.** Ten minutes with no click, keystroke, tap or scroll in
  the MindFlock window and the flock takes the whole screen — over the sidebar,
  the panes and the prompt boxes, with no regard for any of their edges. It is
  `pointer-events: none`, so nothing is covered or blocked, and the first click
  or keystroke sends it home to the logo the same way. It arrives out of the logo
  too, the same round trip the break card's flock makes.

  Its own switch sits under Take a break in Settings → General, with the same
  sentence-with-a-field shape — *Fly the flock over your grid after `[N]`
  minutes* (1–480, default 10). **On** by default, which the break reminder is
  not: this one can only ever appear in a room you have already left, so leaving
  it on costs you no interruptions. Switching it off mid-flight sends the birds
  home through the logo rather than cutting them off mid-air.

  Three things deliberately do not count as you being at the desk: moving the
  mouse, agent output, and anything you do in another app. So a cursor resting on
  the window doesn't hold the birds off, and MindFlock idles behind you while you
  work elsewhere — a second monitor fills with birds, which is the point — with
  the click that brings you back being what wakes it. Under
  `prefers-reduced-motion` both surfaces show a settled flock instead of a moving
  one.

- **Intake → Auto-start**, a fourth tab that answers the other question. The
  three per-source tabs are workbenches — every ticket, every open PR, every open
  issue, grouped by where it lives. This one is not "what is there" but *what
  happens next without me*: only the rows the automations will pick up on their
  own, across all three sources, oldest first, in the order the pipeline will
  draw them. One vocabulary throughout — every section reads *auto-start on* /
  *auto-start off* / *not set up*, whatever the underlying mechanism happens to
  be — and its badge counts the rows it will actually show, like the other three.

- **Reopen the work that is already on this machine.** A row for an item that has
  been worked once already said so in chips (*already ingested*, *a branch for it
  exists on the remote*) and then offered exactly one button: **Begin work**,
  which starts over. But the workspace is usually still right there — ending a
  session keeps its worktree, and a run lost to a restart leaves one behind — so
  starting fresh either collided with it or silently duplicated it. All three
  panels now lead with **Reopen** when the work is findable, resolving it
  most-informative-first: a recently-closed session (whose stashed data restores
  the branch, program, prompt and provisioning rather than approximating them), a
  provisioned clone directory, or a worktree still holding the item's branch.
  Everything about the probe is read-only and best-effort — any failure answers
  "nothing found", which is exactly how the panels behaved before.

- **Every intake row can be started three ways at once, for that one launch** —
  which CLI runs it, how far to carry it, and **how hard to think about it**. The
  effort ladder is neutral (Low, Medium, High, Extra high, Max, Ultra) because no
  two coding CLIs spell it the same way, and the server translates the rung into
  whatever the CLI that actually runs understands: `claude --effort xhigh`,
  `codex -c model_reasoning_effort=high`, `agy --effort high`. A rung above a
  CLI's ceiling **clamps** rather than being forwarded — claude warns and quietly
  uses its default for a level it doesn't know, and codex forwards the string to
  the API, which 400s — so the picker labels what will really happen ("Max (→
  Extra high)"), names the top rung the way that CLI names it ("Ultra
  (ultracode)"), and disables itself outright on a CLI with no effort control
  ("No effort control (aider)") instead of leaving a control that does nothing.
  None of the three is persisted: re-configuring a whole queue to run one item
  differently is the wrong shape of action.

- **↺ — put this window back to idle.** On a clean branch git considers finished
  (`committed`, `pushed`, `pr`) every control in the header is about advancing a
  cycle you may already consider done, so someone who just opened a PR and wants
  to keep writing code on the same branch had no way to say so. ↺ says it.
  Nothing git-facing happens — the commits stay committed, the PR stays open —
  and the pin deliberately does not touch the published stage either, so the
  autopilot, the check kicker and every `*_changed` event keep reading the same
  git-derived truth (a display pin that lied to the autopilot could make an armed
  chain try to commit a clean tree). It releases itself against the worktree
  rather than against the stage label: the moment the tree goes dirty or HEAD
  moves, it is gone.

- **The prompt queue takes a file, and reorders by hand.** Drop a `.csv` on it to
  queue one prompt per record (quoted fields, embedded commas and newlines, `""`
  escapes) or any other file to queue one per line, and drag **⋮⋮** to reorder
  what is waiting.

### Fixed

- **Notifications call a session what the rail calls it.** A desktop
  notification named its window by the machine slug the event carries —
  *"shortcut-21431 needs your input"* — while the sidebar showed either your
  rename or the pipeline label *"(tix) social-scan-noise/shortcut-21431"*. A
  push about a session that does not appear to exist under that name is worse
  than no push. The browser channel now asks the app itself
  (`window.mindflock.displayName`, the sidebar's own resolver), so the two cannot
  drift; the ntfy channel runs the same rule server-side, pinned against the
  TypeScript original's examples. Renames already won — the half that was missing
  is every session nobody renamed.

- **A duplicated window lands under the one it came from**, instead of at the
  bottom of a rail of twelve. The provisioning row is placed there too, so the
  copy never appears at the end and then jumps when the server answers.

## [0.1.17] - 2026-08-11

### Added

- **✨ writes the commit message.** The commit dialog has a button that reads the
  diff and writes the message, and a fast-track press that carried no message now
  gets one the same way instead of committing under "Work on `<session>`" — the
  subject that described the session rather than the change. It asks the session's
  own coding CLI headlessly (`claude -p`, `codex exec`, `agy --print`), so there
  is no API key to configure and no second provider to authenticate; a CLI with no
  text-only mode, a logged-out one, or a timeout all fall back to exactly the old
  behaviour, because a commit that can't be described still has to be committable.
  The dialog reports why it couldn't and leaves your message box untouched.

  Generation happens at COMMIT time, never when the fast-track is armed: arming is
  arm-and-wait, so a message written from the tree as it looked at the press would
  describe a diff that no longer exists. A message you typed, and an intake
  ticket's own title, are never replaced.

- **Intake loads before you open it.** The tickets, PR and issue panels are warmed
  from app startup and kept warm, so Alt+I lands on a list instead of a spinner
  and the tab counts are already filled in. Skipped when no ticketing source is
  connected, and paused while the window is hidden.

### Fixed

- **"PR merged or closed" stopped crying wolf.** Both PR notifications — the
  browser/ntfy alert and the in-app toast — were inferred from the stage pill
  leaving or re-entering the `pr` rung. But the stage ladder drops to `agent` the
  moment the working tree goes dirty, before it has even looked the PR up, so an
  agent iterating on review feedback walked `pr → agent → committed → pushed → pr`
  on every single edit. Each lap announced that the pull request had been merged
  or closed, then that it had opened again, for a PR that never moved.

  A new `session.pr_state_changed` event carries the PR's own state instead, and
  both channels key off that, so each transition is announced once when it really
  happens. A lookup that fails — no `gh`, a rate limit, a network blip — is never
  read as a close; a first sighting seeds silently, so a restart cannot
  re-announce an already-merged PR; and switching branches re-seeds rather than
  reporting the previous branch's PR as closed.

### Changed

- **Expanding a pinned prompt no longer shoves the terminal.** The full prompt
  used to open in flow, which shrank the terminal container, refit xterm and
  resized the real tmux pane — so reading what a session was asked to do reflowed
  the output you were reading it against, and cost a PTY resize on every toggle.
  It now draws over the terminal instead, anchored on the bar itself so the first
  line lands exactly where the collapsed summary was, with no blank row and no
  duplicated line. The bar stays one line tall in both states, so the terminal
  never moves.

## [0.1.16] - 2026-08-10

### Fixed

- **The full-history page was invisible.** Its stylesheet was never added to the
  ordered `@import` list that builds the bundle's CSS, so `position: absolute;
  inset: 0` never reached the page and the overlay opened as an unstyled,
  zero-size block behind the terminal. Every way in — the drag gesture, the
  header button, `Ctrl+↑` — looked broken for the same reason. A test now fails
  on any stylesheet that nothing imports, because "invisible component, green
  CI" should not be able to happen twice.

- **Dragging up out of a terminal reaches the history.** The gesture only armed
  once the pointer was 8px *outside* the top edge, but a drag up ends where the
  selection stops growing — on the top row — and nothing suggests you should
  keep dragging into the pane header. Both edges now arm from just inside, as
  the bottom one already did; the hold delay is still what separates the
  gesture from an ordinary overshoot.

- **A prompt typed while the agent is working counts as a prompt.** Claude Code
  files those as queued entries and never re-files them as messages, so the
  pinned line showed an older prompt, expanding it re-showed the same truncated
  text, and the history page dropped the message outright — exactly the
  follow-ups you go back to read.

### Changed

- **New Session, rearranged.** Name and Folder up top, then three folds: **Git &
  workspace** open — those are the settings the dialog exists to set, and hiding
  the workspace strategy behind a click had people launch with the wrong one —
  with **Prompt** and **Launch flags** shut, since most sessions start with
  neither. The card is sized to hold that without scrolling and no larger.
  Folder suggestions keep one row per source and show whole chips only, instead
  of wrapping to three lines or clipping the last name down the middle. The
  folder browser's parent button is `←` rather than `↑`, matching every other
  file browser. `Ctrl+Enter` still creates; it just no longer says so.

## [0.1.15] - 2026-08-10

### Added

- **The prompt you last typed is pinned above every pane.** Glancing at a grid of
  windows told you what each agent was *saying*, never what it had been *asked*;
  the one line that explains a window is now the one line above it, with an arrow
  that opens the whole prompt. Scroll the agent back and the bar follows the
  section you are reading — Claude Code pins its own contextual `❯ …` row up
  there, and the bar mirrors that instead of the latest turn.

- **A pane's full history is a page you can read.** The agent TUI redraws in
  place, so tmux keeps no usable scrollback and a drag-selection could never
  reach past the top of the screen. `Ctrl+↑`/`Ctrl+↓`, the header's history
  button, or simply dragging past a terminal's edge now opens the whole
  conversation as ordinary text: scroll it, select across screenfuls, release to
  copy. A drag that crosses the edge is continued by the page rather than
  restarted, and it opens parked where you were looking.

- **A maintainer's Site traffic dashboard** (Settings, `--mindflock-dev` shell
  builds only) — GitHub stars over time, forks, per-release download counts, and
  `mindflock.ai/go/*` click totals on one screen, each section degrading on its
  own if an upstream is down.

### Fixed

- **Two windows on one repo no longer read each other's conversation.** Sibling
  sessions share a transcript directory, and everything derived from it — the
  pinned prompt, the latest-turn line, the history page — picked whichever
  conversation had been written to last, so the pair flickered between each
  other's work. Each window now reads the conversation its own activity hooks
  recorded for it.

- **Site traffic finds the GitHub token you already have.** It resolved only a
  pasted token or `$GH_TOKEN`, never `gh auth token`, so a machine authenticated
  the ordinary way (`gh auth login`) called GitHub anonymously — and the star
  history is the one section GitHub refuses to serve anonymously, 401 even for a
  public repo. It now uses the same resolution chain as the PR buttons.

## [0.1.14] - 2026-08-07

### Fixed

- **Fast-track switches itself off once its task ends.** A run that has succeeded
  or failed no longer reads as armed, so the toggle tells you whether anything is
  actually going to happen. The record is kept, so the outcome — and a halt's
  reason — stays readable, and pressing the button starts another run.

  Doing only that would have brought back the problem the run had been made to
  survive: a branch which already has an open PR satisfies a "PR" target the
  *instant* you arm it, so the run finished having accomplished nothing and the
  toggle flipped straight back off. A run is now only **finished** once it has
  actually carried the work — a commit or a step recorded — and one that has not
  acted stays armed and waits. "Already at the target" and "the job is done" had
  been the same state; separating them is what lets both behaviours hold at once.

- **A booting window can be dragged.** Provisioning is the state a window spends
  the longest in — a cold clone runs for minutes — and it was the only one that
  rendered no grip and neither half of the drag contract, so it could be neither
  moved nor dropped onto. Arranging the grid meant waiting it out.

## [0.1.13] - 2026-08-07

### Added

- **Fast-track — carry a session as far down commit → push → PR → merge as you
  want, then stop.** A `⏩` toggle beside each window's guided button arms a
  standing instruction; the Commit dialog gained a "Then keep going" rung so one
  press can commit *and* continue; and **Settings → Workspace** sets where it stops
  by default (Open PR). Pressing it while the agent is still working is the normal
  case: it waits for the agent to genuinely finish rather than committing a
  half-written tree. The chain is driven server-side, so it survives a tab close,
  a page reload and a server restart — and because every step is re-derived from
  real git state rather than replayed from a script, resuming is the same code path
  as starting.

- **The same depth on intake, so a ticket can go from ingestion to a PR
  unattended.** Each source (tickets / PRs / issues) carries a default depth, and
  an individual row can override it before you start it. A whole source cannot
  default to *merge* — that default would apply to every future item with nobody
  watching, and merging is the one rung that cannot be undone — but an individual
  item can.

- **Retryable pre-commit hooks.** Some hooks fail without changing any files and
  will fail identically forever — an index-rebuilding hook with a corrupt index is
  the motivating case — which blocked the commit permanently. Listing a hook **ID**
  in **Settings → Workspace** lets fast-track retry it once and then re-run the
  commit with `SKIP=<id>`, saying which hook it skipped. Test and secret-scanning
  hooks are refused there and enforced in the driver, so a failing test still stops
  the run, which is the whole point of having one.

- **A live step indicator in each window's header.** It names the step that is
  actually happening — setting up, pre-commit, checks, pushing, opening a PR,
  merging — and, when a chain is armed, what it is waiting on in the server's own
  words and which rung it is heading to.

- **The Merge button refuses a merge GitHub would reject.** It reads real
  mergeability and goes unclickable with the blocker named — merge conflicts, a
  branch behind its base, a draft PR, a failing or pending required check, a
  missing review — instead of offering a button that just produces GitHub's error.
  When mergeability cannot be determined at all (no token, no `gh`, a network
  fault) the button is left exactly as it was: not knowing is not the same as no.

### Changed

- **Stage changes show up when they happen instead of at the next poll.** `GET
  /api/instances` serves the background tick's snapshot for up to ten seconds and
  never rebuilds probes inline, so the post-action refresh provably could not
  observe anything — "Push" appeared seconds after a commit finished. The publisher
  now computes its own stage rather than serving a memo another ticker filled, a
  new single-session read publishes through, and a bounded 250 ms watcher republishes
  the moment a commit or push actually lands.

- **Recently closed shows the whole branch name.** The row showed only the session
  title, which for an ingested ticket is the bare slug — so the list said nothing
  about what any of the work was.

- **A long window title truncates instead of clipping the header controls.** A
  60-character session name used to slice the Commit button, the fast-track toggle
  and the step indicator off the right edge. The title yields first now; it is a
  label whose full text is a hover away, and those are controls.

### Fixed

- **The guided button no longer sticks on "Commit…" for the rest of the session.**
  The pin set after a successful **Make PR** had no reconcile, so once a session
  had opened one PR its button never offered Push again for the life of the page.

- **A commit no longer inherits an unrelated message.** The saved message survived
  a *successful* commit, so anything committing without an explicit one adopted a
  stale subject — caught about to record one feature's files under a message
  describing a database migration. It is cleared once a commit lands, and only
  reused while a failure is genuinely pending.

- **Autopilot never pushes the base branch.** A run on `main` pushed straight to
  `origin/main`, which a bypass-silent ruleset accepts while merely noting that a
  PR was required. Committing there is still fine; going further needs a branch.

- **Two servers sharing one machine no longer fight over a run.** Each treated the
  other's presence as a restart and reset the "is the agent done" dwell every pass,
  so a chain could sit forever without acting — and both would otherwise have
  acted, double-committing and double-pushing the same worktree. One lease, one
  driver.

- **A PR no longer flaps in and out of its stage.** A failed lookup was cached as
  "there is no PR", so a single rate limit or network blip dropped the stage and the
  next poll restored it — re-announcing "PR merged or closed" every minute for a PR
  that was open the whole time.

- **The ticket-ingestion dot is a circle when it is red.** Its error state borrowed
  a class name from the app-wide rule for error *text*, whose `min-height` beat the
  dot's own height and rendered it as an oval — in exactly that one state.

## [0.1.12] - 2026-08-05

### Added

- **`mindflock init` — a guided first-run setup.** Getting to a first session
  meant discovering and sequencing four commands from the README. `init` runs the
  doctor, offers to run each fix for you (including logging your agent CLI in),
  then shows the git repos it found on this machine as a numbered list and
  remembers the one you pick, so the New Session dialog opens on it. `mindflock
  serve --setup` runs the same wizard before binding.

- **A first `serve` now prints what this machine actually needs.** It used to
  print the same three-step blurb to everyone; it now names the dependencies that
  are missing with their one-line fixes, lists the folders you could work in, and
  gives you the exact next command. It builds that off the main thread — a first
  run is the desktop app's very first launch, which is polling for the port.

- **The New Session dialog offers the repos you actually use.** A new
  `GET /api/repos/suggest` ranks the folder your last session used, the live
  sessions' repos, the server's own working directory, and a shallow scan for git
  repos under your home — and the dialog pre-selects the top one as a chip row
  instead of dropping you in `$HOME` to go hunting. `GET /api/repos/check` backs a
  quiet line that appears when the chosen folder is *not* a git repo, saying diff,
  commit and PR will be off, with a one-click "create one here" that drives the
  checkbox that was buried in **More options**.

### Changed

- **The folder browser selects like Finder.** A click used to navigate and only
  the per-row **select** button committed a folder, which read backwards. A single
  click now selects (and shows the folder in the field), a double click descends,
  the redundant button is gone, and Escape cancels the whole browse rather than
  leaving the field on the last directory you merely passed through.

- **The doctor's agent-auth check now covers every coding CLI, not just Claude.**
  It reported "no auth probe for `codex` — skipped" for anything else, so a user
  on Codex, opencode or aider learned their CLI was logged out when their first
  session failed. It reads each provider's own declared credential locations now.
  A provider that declares none stays quiet — absence of evidence is not evidence
  of absence — and it only offers to run a login command the provider actually
  declares, so `doctor --fix` can no longer drop you into an agent REPL that
  cannot resolve the check.

- **One first-run signal instead of three.** The welcome tour and the setup
  checklist each kept their own browser-local flag beside the server's
  `onboarded`, so clearing a profile or opening the desktop app on a second
  machine replayed the whole 12-slide tour at someone who plainly was not new. The
  server's flag is the only rule now, and it still means what it always meant: you
  have created a session.

### Fixed

- **A todo you clicked to edit closed again immediately.** The text box opened and
  reverted within a frame: the poll callback's dependencies changed the moment an
  edit began, which re-ran the effect that focuses the Add box, and that blur
  committed the edit before you could type. The initial focus is tied to the
  dialog opening now, not to unrelated re-renders.

- **The doctor told logged-in macOS users they were logged out.** Claude Code
  keeps its credentials in the login Keychain there rather than in
  `~/.claude/.credentials.json`, which the probe never looked at — so every Mac
  saw "CLI is installed but no sign of a login was found" on every run. It checks
  the Keychain now, after the cheap file reads, so it never shells out to
  `security` when a file already answered.

- **The sidebar's resize handle painted over every open dialog.** The `.modal`
  backdrop is fixed with no `z-index` of its own, so the handle's `z-index: 40`
  drew its accent line across the dimmed page and kept answering `:hover` — and
  the drag still worked through the backdrop, resizing a sidebar nobody could
  see. It hides while any modal is mounted, keyed off the overlay element so the
  command palette and the welcome tour are covered too.

## [0.1.11] - 2026-08-04

### Changed

- **The Intake tabs dropped the status line under their master switch, and
  tightened up.** "● Active — polling 1 source for tickets assigned to you" sat in
  a row of its own, under a switch that was already green, directly above the very
  sources it was counting — it restated what was on screen in a third colour and
  cost a full row in each of the three tabs. The switch is a banded row now, and
  says something only when the tab has nothing to switch on yet ("Add a repository
  below and this starts reviewing your PRs on it") — the one state a switch cannot
  show by itself.

  The dialog also sizes to its content instead of a flat `84vh`, which drew an
  empty box under any tab that ended early (Issues is 581px now, not a forced
  760). Each tab's sections — **Sources**, **Repositories**, **Assigned
  tickets**, **Open issues** — read as headings rather than another line of prose,
  and workflow-state rows print in the provider's own casing (`Ready for Review`,
  not `READY FOR REVIEW`), so on this surface uppercase means exactly one thing: a
  section heading.

### Fixed

- **The Intake master switch's label sat 10px above its own toggle.** It reused
  `.set-switch-row`, which nudges its label up to compensate for the desktop app's
  font; on a row whose label is already centered against a 20px switch, the nudge
  just pulled the two apart.

- **The Intake top-bar button announced itself as "Open work"** to a screen
  reader — the surface's name two renames ago, so the spoken label matched nothing
  on screen. The visible "Intake" is its accessible name now.

- **A commit message the pre-commit hooks rejected is offered back, not retyped.**
  It always survived on disk — `.mindflock_commit_msg` is the file
  `git commit -F` reads, and a re-commit with no message reuses it — but the
  dialog pre-filled from an in-memory map that a page reload empties, and a
  server restart prompts exactly that reload. New
  `GET /api/instances/{title}/commit-message` hands it back, gated on
  `.mindflock_commit_status` recording a failure so a *successful* commit's
  message never pre-fills the next one.

- **Clicking outside the Commit dialog closes it**, like Escape already did,
  instead of requiring the Cancel button. It closes on `mousedown` on the
  backdrop itself, so a text selection that starts in the message box and
  overshoots can't discard what you typed.

## [0.1.10] - 2026-08-04

### Added

- **Ticketing, PR review and issue handling moved out of Settings into a new
  top-bar surface: Intake.** They were never settings. Settings is where you
  configure the app once and forget it; these three are somewhere you *visit* —
  to see what came in, start something by hand, or pause a queue. Sitting in the
  same left nav as Appearance and System logs, they were both hard to find and
  mixed in with things that never change.

  **Intake** sits next to **New** in the top bar (`Alt+I`, plus four command-palette
  entries), and inside it a tab strip — **Tickets**, **Pull requests**, **Issues** —
  carries a live count per tab — the number of rows that tab will actually show
  you, not a lifetime total: counting every ticket the provider ever assigned you
  read `1221` over a list of 52, because done states like Completed are parked
  behind the **+ Add bucket…** menu. The badge and the panel now share one bucket
  filter so they cannot disagree. Every
  old deep link still works: the retired screen keys (`ticketing`, `repo`, `issues`)
  route to the tab that replaced them, so the sidebar bars' buttons, the welcome
  tour's "Set up now →", and the server's own `settings_screen` on a Connections
  card all land in the right place.

- **All three tabs are the same screen now.** They had drifted into three dialects
  of one shape, and Ticketing had the good one. The shared anatomy is: master
  switch → status line → a list of collapsible **source cards** with `+ Add` → the
  **work those sources yielded, grouped by source**, each row saying why automatic
  pickup did or didn't take it.

- **Each watched repository is its own card, with its own settings.** PR review and
  issue handling used to show a flat chip list of `owner/name` strings plus one
  screen-wide set of options, so "watch this one too, but give it half an hour of
  grace and run it on codex" was not expressible — there was nowhere to hang a
  per-repo value. Now each card carries its own **Agent CLI**, **base branch** (PR
  review), **min age**, **skip authors**, a **Test access** button and Remove. The
  tab-wide fields moved into *Advanced options* and are the defaults a blank card
  field inherits, shown as its placeholder so "empty" reads as "inherit 15" rather
  than "no grace period at all". Stored as `github.repo_settings` /
  `github.issue_repo_settings`, keyed by repo slug; an absent key inherits. The
  monitors resolve every filter through those (`GithubConfig.min_age_for`,
  `base_branch_for`, `skip_authors_for`, `agent_for_repo` and the issue twins), so a
  card never saves a value the pipeline ignores.

- **Assigned tickets group by ticketing source, then by workflow state.** One flat
  level of buckets merged same-named states from different sources: a Jira site and
  a Linear workspace both have an "In Progress", and under one heading there was no
  way to tell which ticket came from where without reading each row. Bucket
  show/hide stays an app-wide choice ("I don't care about Completed" means it
  everywhere) while open/closed is per source. `GET /api/tickets` gained a
  `source_labels` map covering *every* configured source, including ones that
  returned nothing or failed — deriving labels from the ticket rows alone made
  exactly those sources vanish.

  Grouping is unconditional, on every tab: "which provider is this from?" is the
  question a queue answers before any other, and a heading answers it once for
  however many rows sit under it. The per-row label that was tried instead does
  not scale — the same name repeated down a column of 500.

- **Ticket states nest under their workflow, instead of repeating it.** A source
  with several workflows has to qualify its state names to keep them unique, so
  Shortcut returns `Product Development · Deferred`, `Product Development ·
  Unscheduled`, and five more — and a flat list wrote `PRODUCT DEVELOPMENT ·`
  onto seven consecutive headings. `GET /api/tickets` now carries a `bucket_meta`
  map (`{bucket: {group, label}}`) and the panel renders **source → workflow →
  state**, each name written once. The composite name stays the bucket key, so
  `+ Add bucket…` / ✕ and the done-state filter are unaffected; a provider with
  one workflow (or none — Jira, Linear) reports an empty `group` and the level
  simply doesn't appear. See `TicketProvider.list_states` for the contract.

  Each heading in that tree is also styled as the control it is — hit area, hover
  fill, and a caret that rotates instead of swapping ▸ for ▾. As plain text
  beside a small caret, under a *bordered* source card, the top-level heading
  read as a stray line of copy rather than the way in to the tickets.

- **The tickets panel's hint describes *your* workflow, not an imagined one.** It
  said "done states like Completed start hidden", which is a guess — a Shortcut
  flock has `Product Development · Won't do`, a Jira one has `Closed`. It now
  names the states actually hidden, with matching singular/plural. And the
  auto-ingest sentence no longer merges sources: one source watching `Ready`
  alongside another watching `Todo` was reported as "watches Ready, Todo", which
  was false for both of them, since neither watched both. With several sources it
  points at the per-source headings, which each carry their own.

- **Any queued item can be started on a different coding CLI, just for that
  launch.** A picker beside **Begin work** / **Begin review** / **Start work**, next
  to the button rather than buried in configuration: you notice mid-review that
  this one wants a different model, and re-configuring the whole queue to run one
  item is the wrong shape of action. The three force-start routes take an optional
  `agent`; an unknown name is a 400 rather than a silent fall back to the default.

- **A per-repository GitHub access test** (`POST /api/settings/test/github-repo`),
  behind each repo card's **Test access**. The existing `/settings/test/github`
  answers "is there a credential"; this one answers "does it reach THIS repo",
  which is the failure people actually hit — a typo'd slug, or a private repo the
  token has no scope for. It also reports a read-only token, which matters for
  issue handling (that half has to push a branch).

### Fixed

- **Switching a ticketing source's Agent CLI kept launching the old one.** The
  pipeline reads its config once at boot and the scanners hold that snapshot, so
  stamping a ticket with `source.agent` from it meant a provider switched in the UI
  applied only after a restart — and *clearing* the field did nothing at all. The
  agent is now re-read from disk at stamp time (`source_agent_now`), for both freshly
  scanned tickets and ones replayed from a previous run's pending queue. An on-disk
  config with no opinion is treated as an answer ("use the app default"), which is
  what makes clearing the field work.

- **The same staleness one layer up: clearing PR review's or issue handling's
  Agent CLI did nothing on a running pipeline.** Those chains resolved through
  `fresh_agent`, which falls back to the config the runner was *constructed* with
  whenever the on-disk chain answers "nothing set" — so the boot-time provider
  came back every time. They now use `agent_now`, where an empty on-disk answer is
  an answer ("use the app default") and the snapshot is consulted only when the
  config cannot be read at all.

- **Forced starts ignored the agent their source was configured for.** A review or
  issue started by hand now resolves the same chain the automatic monitor does —
  this start's own pick, then the repo card, then the tab-wide value, then the app
  default — instead of jumping straight to the app default.

- **A newly pasted GitHub token did not take effect until a restart.** The
  resolved token is cached for the life of the process, and nothing invalidated
  it, so PR review, issue handling, **Make PR** and the new per-repo test all kept
  using the old credential — which reads exactly like the paste not having saved.
  Writing `github.token` now clears the cache, and the per-repo **Test access**
  resolves afresh, since answering about the previous token would be worse than
  not testing at all.

- **The open-PR list hid pull requests it should have explained.** With a base
  branch configured, the panel asked GitHub only for PRs into that branch, so a PR
  into any other one was simply absent — indistinguishable from "there are none",
  and impossible to force-review. It now lists every open PR and reports the
  mismatch as a skip-reason chip.

- **PR review's "Skip authors" field described itself wrongly.** It said "GitHub
  logins whose PRs are ignored", but review only ever takes your *own* PRs — the
  list drops review *comments*, which is how you stop a bot's feedback being acted
  on. The label now says what the code does.

- **One long failure reason dragged every Intake row sideways.** A recorded
  failure is a sentence of git output carrying a branch name and an absolute
  worktree path, and the chip meant to contain it capped itself at
  `max-width: 100%` — which is circular: a work row is a grid whose first track
  was `1fr`, whose automatic minimum is its content's min-content width, and a
  `nowrap` chip has no smaller width. The track had already grown, so 100% *was*
  the oversized width and the start buttons ended up off the right edge. The
  track and the meta line may now be narrower than their content, and a long
  reason wraps instead of ellipsizing on one line — the remedy in these messages
  ("Kill that session first") is at the end, so a one-line clip hid the half
  worth reading. The tooltip still carries the raw string.

## [0.1.9] - 2026-08-03

### Fixed

- **The default coding provider was ignored by everything that launches a
  session.** Settings → Coding provider writes `coding_cli.default_provider`
  into `settings.json`, but every launch path read `default_program` out of
  `config.json` — a different store, seeded once on first run by a helper that
  only ever hunts for `claude`, and which the UI never writes to. Nothing
  bridged the two, so choosing a default changed the Providers screen badge and
  `mindflock doctor` and nothing else: ingested tickets, PR-review sessions,
  issue sessions and the New Session dialog all still launched Claude Code.

  `backend.config.program.resolve_default_program` is now the single answer to
  "which CLI, when nobody asked for a specific one" — the chosen provider, then
  the engine config, then `claude` — and the engine bridge, the standalone tmux
  launcher and the web layer all resolve through it. It is also read fresh
  rather than from the config snapshot taken at server start, so changing the
  default no longer needs a restart. An explicitly chosen agent (a ticketing
  source's own, say) still outranks it.

- **Two welcome-tour slides scrolled.** "Welcome to MindFlock" needed 327px and
  "3. PR review" 394px inside a 320px content box, so both got a scrollbar and
  the PR-review slide hid its own *Set up now* button. The card is now 520×490
  with a 48ch body — widening did most of the work, taking that slide from 394px
  to 350px — which fits all twelve with room to spare.

- **The per-surface Agent CLI pickers chose a provider that nothing read.**
  0.1.9 added `github.agent` and `github.issue_agent` so PR review and issue
  handling could each run their own coding CLI, and the settings round-tripped
  correctly — but `_parse_github` built its `GithubConfig` without passing either
  field, so both sat at their `""` dataclass default however they were
  configured. The merged config computed the right value and then threw it away
  one line before anything could read it. `pr_agent()` and `issue_agent()` were
  correct and unreachable, so every review and every issue session fell through
  to the app-wide default provider.

  The gap survived the suite because the precedence logic was tested against a
  hand-built `GithubConfig` and the storage against `GithubSettings`, with
  nothing exercising the parse between them. That seam is now covered.

- **"Begin review" ignored the Agent CLI setting directly above it.** The forced
  PR-review route passed `ENGINE.default_program()` outright, so the dropdown in
  Settings → PR review governed only the auto monitor: clicking the button
  launched the app-wide default. It now resolves through `pr_review.review_agent()`,
  the same `pr_agent()` chain the monitor uses. The issue equivalent had the
  matching bug one level up — `prepare_start` read the ingestion-wide
  `agent_for()`, which skips straight past `github.issue_agent`.

- **Switching provider needed a pipeline restart to take effect.** The ingestion
  pipeline calls `load_config()` once at startup and holds that snapshot for the
  life of the process, which is right for poll intervals and tokens and wrong for
  "which CLI should this session run": a provider chosen in Settings was invisible
  until the pipeline was restarted. Agent choice is now re-read at the moment of
  launch — the rule `resolve_default_program` already followed for the app-wide
  default — via `config_for_launch` / `fresh_agent`, across tickets, issues and
  both PR runners. An injected config stays the fallback rather than being
  discarded, so a caller that builds one by hand still has it honoured.

- **A ticket that had run before could not be run again.** Ending a session
  deliberately keeps its worktree, so a second run of the same ticket met a
  leftover worktree still holding the feature branch and died in
  `git worktree add` with "already checked out at …". Nothing in the UI could see
  it: with no live session the panel enabled **Run ticket**, and the failure was
  then recorded with advice to delete the `state.json` ledger entry — which
  clears the *record* of the failure and leaves its cause in place, so the retry
  failed identically.

  Force-start now reclaims the leftover first, under two rules it never breaks:
  it never touches a worktree a live session owns, and it never removes one
  holding work (uncommitted changes, a stash, or unpushed commits). Anything it
  declines to take keeps the previous behaviour, so the worst case is the error
  message you already got.

- **A recorded failure now says what went wrong.** The ledger stored a
  `failure_reason` — "branch '…' is already checked out at `<path>`" — and the
  loader dropped it, leaving the panel to show a generic chip whose suggested
  remedy fixed nothing. The reason is carried through and shown in full (chip
  ellipsizes, tooltip has all of it; the actionable half of these messages is at
  the end, so clipping server-side removed exactly the part worth reading).

### Added

- **PR review, issue handling and the Assistant each pick their own agent CLI**
  (`github.agent`, `github.issue_agent`, `coding_cli.assistant_provider`), the
  way a ticketing source already could. Issue handling deliberately does *not*
  inherit PR review's choice and vice versa: they are separately configured
  features with separate repo lists and separate toggles, so inheriting would
  surprise anyone who set one and not the other. Both fall back to
  `[mindflock].agent`, then the resolved default.

  The Assistant needed this most — it was hardcoded to `claude` in two places,
  which made it unusable for anyone who had never set Claude up. Its picker
  lives in the Agent file dialog next to the standing instructions.

### Changed

- **PR review, Git issues and Ticketing are one screen with different nouns.**
  They had drifted into three dialects of the same layout, and every incidental
  difference cost a reader a moment working out whether it meant something. All
  three now read the same way top to bottom — switch, status line, what's
  watched, agent CLI, open-work panel, Advanced — from shared pieces in
  `settings/screens/automation.tsx`. Ticketing gains the status line it never
  had (its toggle had no feedback at all) and its per-source field is relabelled
  **Agent CLI** to match its neighbours.

- **The agent picker is a top-level field, not an Advanced one**, on every
  screen that has one. Which CLI does the work is not a tuning knob.

- A collapsed ticketing source now always names the CLI it runs
  (`Shortcut — Allure Security · claude (default)`). Naming it only when it was
  overridden made "on the app default" indistinguishable from "this source has
  no agent setting", which is what sent people hunting for the picker.

## [0.1.8] - 2026-08-03

### Added

- **Ingestion runs any agent CLI, not just Claude Code.** MindFlock has always
  let a hand-started session pick its CLI, but an *ingested* one couldn't:
  the generated workspace launcher hardcoded Claude Code's four spellings, so a
  provisioned session on anything else was started with flags that CLI rejects
  (`aider --dangerously-skip-permissions "<prompt>"`, resumed with
  `aider --continue`). That contradicted the provider-agnostic pitch and made a
  paid Claude subscription a hard dependency of the whole ticket pipeline.

  The launcher is now provider-neutral: the skip-permissions flag, how the seed
  prompt is passed, the resume flag, the entry subcommand and the clean-quit exit
  codes all come from the launching provider (`LauncherSpec`). codex resumes with
  `resume --last`, goose opens `goose session -r`, aider gets `--yes-always` and
  `--restore-chat-history` with no retry chain, and an unrecognized program has
  nothing invented for it. For `claude` the generated bytes are unchanged.

  Each ticketing source picks its own agent (Settings → Ticketing → **Agent**, or
  `[[ticketing.source]].agent`), falling back to a pipeline-wide
  `[mindflock].agent` and then the app default — so an existing install resolves
  exactly as before, and a flock can route one queue to a hosted CLI and another
  to a local model. A typo'd agent name is a config error at load time, listing
  the valid ones, instead of a session dying on a shell "command not found".

  The CLIs that take their first instruction interactively (aider, goose,
  opencode, cline) are seeded by pasting the prompt into the pane once its TUI has
  drawn — through a bracketed tmux paste, so a multi-line ticket arrives as one
  block instead of being submitted a line at a time. Only ever on a first launch;
  a resumed session is never re-seeded.

- **Run everything on a local model — no subscription, nothing leaves the
  machine.** Point sessions at a model you serve yourself (**Ollama**, **LM
  Studio**, or any OpenAI-compatible server such as llama.cpp, vLLM or a LiteLLM
  proxy) from Settings → **Local model**. It is a runtime overlay, not a new
  provider, so it applies to every launch path — a fresh session, an ingested
  ticket, a PR-review session and a post-reboot relaunch alike — and the registry
  and your own provider TOMLs are untouched.

  Per-CLI routing is verified rather than guessed: codex via its own
  `--oss --local-provider`, aider via `OLLAMA_API_BASE` / `LM_STUDIO_API_BASE`
  with the model prefixes its docs specify, goose via `GOOSE_PROVIDER` /
  `GOOSE_MODEL`. **Test connection** proves the server answers, lists the models
  it actually serves (so the model field becomes a dropdown instead of "type the
  exact tag"), and names which of your installed CLIs can be pointed at it.
  Claude Code speaks only the Anthropic API, so it has no local route — the
  screen and `mindflock doctor` both say so out loud, because a session quietly
  using its hosted API is the one outcome a privacy guarantee can't be quiet
  about. Off by default, and off is an exact no-op on every launch path.

### Changed

- **GitHub Issues is the zero-config on-ramp.** It now leads the provider catalog
  (so a newly added source starts there) and needs **no fields at all**: the token
  comes from your existing GitHub connection or `gh auth token`, and the
  repository resolves through the source's Repo URL, then `[repository].url`, then
  this checkout's `origin`. On a machine sitting in a GitHub clone, picking
  "GitHub Issues" and saving is the entire setup. **Test connection** reports the
  repo it resolved to, so "zero config" never means an empty field you have to
  trust; when nothing names a repo, the error says what to fill in instead of
  failing mid-poll.

- **The sidebar is the width you need it to be.** It was a fixed 260px, so long
  session labels ellipsized while the agent panes had width to spare — or the
  reverse on a wide screen. It has a drag handle now (arrow keys too), with the
  width persisted across reloads. Session labels stop being truncated in JS along
  with it: the old 20-character budget on the feature name was always a guess at
  how much fits, which is a CSS question, and one whose answer now changes as you
  drag.

- **The pre-commit stage pill stays a pill.** It badges a `failed_step` only when
  that value is actually pill-sized; the server's generic fallback can return a
  whole line of hook output (up to 80 chars), which either overflowed the row or
  forced the chip itself to be truncated. Long details ride in the tooltip
  instead. That fallback was also picking the wrong text — `capture-pane` without
  `-J` returns one line per pane *row*, so a wrapped error got split and the
  "last error-looking line" was its tail, leaving the badge reading as a mid-word
  fragment (`ffic-reduction'.` out of `…'traffic-reduction'.`).

- **The project says what it is: a _private_ flock, on your own machine.** New
  README section with the full network inventory — everything MindFlock itself
  talks to, when, and what it sends — and the honest boundary stated plainly:
  your agent CLI still calls its own vendor, and with a local model you can close
  even that.

## [0.1.7] - 2026-07-31

### Added

- **Your phone URL now travels with the notifications.** When Tailscale is up,
  every ntfy push carries this machine's tailnet `/m` address — in the message
  and as the tap target — deep-linked to the session it is about (`/m?s=<title>`),
  so a notification is one tap from the thing it's telling you about instead of
  the start of a hunt for the URL. On top of that, one push announces the URL
  whenever it becomes newly reachable: at server start, when the ntfy channel is
  switched on, and when tailscale mode is turned on. The access token is never
  included — the message is stored on a third-party server, so it goes bare and
  says that a new device will meet the sign-in page. Deduplicated (turning on
  push and tailscale mode back to back is one intent), and a URL that isn't live
  until a restart says so. *Tapping opens* stays available for sending taps
  somewhere else entirely.

- **A Diff tab in the phone UI.** `/m` gains a third tab beside Agent and Shell,
  reading the same `GET /api/instances/<title>/diff` as the desktop Diff tab
  with the same two baselines behind one button (**All changes** vs
  **Uncommitted**, persisted under the shared `mf_diffbase` key). Unified and
  colorized like the desktop, with each file's name lifted into a header that
  sticks while its hunks scroll past, sideways scrolling contained inside the
  panel, and a cap for very large diffs. The terminal stays attached behind it,
  so switching back is instant — and the git action bar (with its status toast)
  stays on screen, because reading the diff is exactly when you decide to push.
  A phone can now approve work, not just unblock it.

- **Tailscale mode applies itself.** Which interface uvicorn binds is fixed at
  process start, so the Settings → Mobile toggle only ever meant something after
  a restart. Turning it on now takes that restart: `POST /api/settings` answers
  `{"restarting": true}` and the screen waits for the server to come back and
  refreshes the URLs/QR in place. Bounded at three attempts (counted in the
  environment, since each attempt is a new process image) before it gives up and
  leaves the manual button, rather than restart-looping a server that isn't
  going to come up on the tailnet. The same check runs at every boot, so a
  server started in local mode while the setting says tailscale corrects itself.

- **Sessions that run out of usage get picked back up.** The prompt queue has
  always ridden out a usage limit for sessions with something queued; a session
  that simply ran out mid-task had an empty queue, so nothing was watching it —
  it sat on its CLI's limit screen until a human came back, often hours after
  the window reopened. The drain pass now also walks the sessions whose activity
  is `limit` (a free read of the state snapshot — no probes when nothing has run
  out), waits the window out with the same meter/banner logic, and sends
  Esc + `continue` so the agent resumes its task. New setting
  `general.resume_on_usage_reset` (Settings → General, default on).

  Two notification rules go with it, both default-on and mutable like the rest:
  **"A session runs out of usage"** and **"Usage comes back after running out"**.
  The second rides on a new `session.usage_restored` event, emitted once per
  reopening by that watcher — so it cannot fire for a window that merely rolled
  over while nothing was blocked, and several sessions unblocking together still
  make one notification.

## [0.1.6] - 2026-07-30

### Added

- **Optional ntfy push, so notifications reach your phone.** Until now the only
  notification channel was the browser's `Notification` popup — which needs a
  MindFlock tab open on a secure origin, exactly the situation you're not in when
  you'd most like to know that a session is waiting on you. Settings →
  Notifications gains a **Phone push (ntfy)** section: give it a topic
  (**Generate** offers a random one), scan the QR into the free
  [ntfy](https://ntfy.sh) app, and the **server** publishes matching events to
  it — no tab, no browser, no MindFlock window required. **Send a test**
  confirms the round trip and the row afterwards reports the last push or why it
  failed. Off until you turn it on, and pointable at your own ntfy instance
  (**Server**) with a token for a protected topic.

  The existing per-rule switches are now labelled as governing *both* channels —
  one "what notifies me" list, with channels deciding only where an alert lands.
  Per-rule ntfy priority means the actionable rules (needs-input, budget
  exceeded, pre-commit failed) buzz at priority 4 while the ambient opt-ins
  (idle, pre-commit running) arrive quietly at 2.

  New endpoints `GET|POST /api/notify/ntfy` and `POST /api/notify/ntfy/test`
  (see [docs/web-api.md](docs/web-api.md)); new settings
  `notifications.ntfy_enabled` / `_server` / `_topic` / `_token` / `_click_url`,
  resolved through env → `settings.json` → defaults so a headless box can just
  export `MINDFLOCK_NTFY_TOPIC` (an env topic is an implicit opt-in — there is no
  Settings screen there to flip a switch in).

  Care taken with the parts that could leak: the token is masked on read and
  kept on an empty write like every other secret, **and** is dropped when the
  server URL is retargeted at a different host without a fresh token, so one
  server's credential is never handed to another. A `token=` query parameter in
  the optional tap-to-open URL is stripped, because that URL is stored on the
  ntfy server. Nothing logs the topic name (`mindflock.log` is served back out
  over `GET /api/logs`, and on the public server the topic *is* the credential —
  hence the random default and the on-screen warning while pointed at
  `ntfy.sh`). Pushes are capped at 60/hour per process so a flapping session
  can't burn a quota, and delivery is best-effort throughout: a failed push is
  recorded for the settings screen and never touches the event that triggered it.

## [0.1.5] - 2026-07-30

### Changed

- **SSH remotes work, and the GitHub CLI is genuinely optional.** `gh` was
  declared optional in 0.1.1, but the code and the docs had not caught up: the
  engine's push ran `gh repo sync` with `git push` only as a fallback, the
  make-PR and merge endpoints refused with "GitHub CLI (gh) is not installed",
  and the README listed `gh` above the Optional row while doctor, CONTRIBUTING
  and the installer each said something different. A contributor whose git
  config uses SSH could not push at all. Now: **pushing is always plain
  `git push -u origin <branch>` over whatever remote your repo already has** —
  SSH or HTTPS, used verbatim, with your own git credentials, and MindFlock
  never rewrites a remote URL (so `url.<base>.insteadOf` still applies). Only
  **Make PR** and **Merge** need to reach the GitHub API, and each now resolves
  in three tiers: `gh` when it is installed *and* authenticated, then the GitHub
  REST API with a resolved token, then a prefilled compare/PR URL handed to your
  browser — `POST /api/instances/{title}/make-pr` returns `200 {ok: false,
  compare_url}` rather than a 400, and no response is ever just "gh is not
  installed". When a credential is genuinely needed the app prints one sentence:
  *add a GitHub token in Settings → PR review, or install the GitHub CLI*.
  Doctor's missing-`gh` line now reads
  `not found (optional — only PR create/merge and PR review need it; pushing uses plain git)`
  instead of "push/PR steps will fail"; `GET /api/config` gains
  `caps.github` (true when either credential exists). Remote URLs of every
  spelling — `https://`, `ssh://`, `ssh://host:22/…`, scp-style
  `git@host:owner/repo.git`, `git://`, and local paths — go through one parser
  (`backend/session/git/remote_url.py`), and a new `[repository].git_transport`
  setting (`auto` | `ssh` | `https`, default `auto`) picks the form used when
  MindFlock has to *build* a clone URL from an `owner/repo` slug: `auto` matches
  the transport of your own `[repository].url` for that repo, and an explicit
  value always wins. CI's cold-install job no longer installs `gh`, so a
  gh-forcing regression now fails the build instead of shipping.
- **The pitch, everywhere: MindFlock turns your ticket queue into a queue of
  pull requests.** The README, the website and the package description used to
  lead with parallel agent supervision — a crowded category — and buried ticket
  ingestion in bullet six. They now lead with the thing nothing else does: work
  assigned to you in Jira, Linear, GitHub Issues, Shortcut or Asana becomes an
  isolated session with nothing typed, and you review the diff and drive it
  home. Worktree isolation, tmux and the grid are described as *how* it works.
  The README also states plainly what is **not** automatic (no commit, push, PR
  or merge without your click; no writes to your tracker; polling, not
  webhooks).
- **The evidence is now visual, on both surfaces.** The README and the website's
  numbers section lead with the result — 6.3× more reviewed source per half-hour at
  the keyboard — instead of with the flat metric that makes the *method* interesting,
  and both now carry the charts: a 24-month trend showing the ticket rate holding
  while diff size and test coverage climb, and a second showing that the average day
  never changed while the ceiling on work in flight went from 16 branches to 31. That
  second chart is the clearest argument for the app rather than for agents in general,
  and it was previously nowhere.
- **The first published productivity figures, measured properly.** An earlier
  draft leaned on volume counts, which turned out to prove nothing: pull requests
  per month actually went *down*, and Shortcut's start→done clock got *longer*
  (it measures review, QA and deploy queues that no coding tool touches). Three
  eras of one repository — before agents, one agent at a time, and a flock —
  recomputed from the git graph and the Shortcut API instead: the ticket rate
  barely moved (43 → 55/month) while the median ticket went from **114 source
  lines across 4 files to 979 across 13**, pull requests touching tests went from
  **5% to 88%**, peak branches in flight went from 16 to 31, and reviewed source
  per half-hour-with-a-commit went up **6.3×**. Source files only (lockfiles,
  generated files, images and DB dumps excluded — under 1% of recent lines),
  medians not means, because a single 1.6 M-line bulk import would otherwise
  dominate every average. The method is documented in the README so the figures
  can be re-derived.
- **Ticket sessions land in the app by default.** `[mindflock].enabled` now
  defaults to `true`, and is exposed in Settings → Advanced instead of being
  file-only. Previously a fresh install that connected a tracker got detached
  tmux sessions and OS terminal tabs — no stage badge, no guided git bar — until
  it found an undocumented config flag. The engine bridge is in-process (it does
  not need a running server, and behaves the same headless); if it is ever
  unimportable the pipeline falls back to the standalone path with a warning
  naming what was lost.
- **Both ways in are stated as first-class.** Leading with ingestion is right —
  nothing else does it — but the README now says plainly that MindFlock is also a
  parallel-agent workspace you drive by hand (`+ New`, or `mindflock new`), that a
  tracker is a source of sessions rather than a requirement for them, and that a
  hand-started session runs whichever agent CLI you point it at. The website gets
  a section of its own for it.
- **The Ticket Ingestion bar is visible out of the box** (`DEFAULT_VISIBLE_BARS`),
  so the flagship feature is no longer hidden behind ⚙ Customize. The first-run
  footer hint now points at the bars that are still hidden.
- **The demo shows the pipeline, not the dashboard.** `docs/demo.gif` and the
  site's `demo.mp4` are re-cut from a new `pipeline` scene that opens on the
  ticket queue — a Jira issue assigned to you, then that issue becoming a
  worktree and a seeded agent with nothing typed — before it ever shows the
  grid, and then follows one session through diff → commit → push → PR → merge.
  Every session in it is titled by a tracker slug on a `feature/<slug>/<name>`
  branch, because that is what the pipeline actually produces.

### Fixed

- **Provisioned sessions pushed into your own laptop instead of the forge.**
  Provisioning clones from the repo you picked because a local clone is fast and
  works offline — but that left the workspace's `origin` pointing at a directory
  on your machine. `git push origin <branch>` then *succeeded* into your own
  checkout: the stage chip flipped to `pushed`, `git ls-remote` confirmed the
  branch, and yet nothing ever reached GitHub, so **Make PR** failed against a
  remote that is not a GitHub repo. The clone source and the push target are now
  separate: MindFlock still clones from the local path, then re-points `origin`
  at that repo's own forge URL (copied verbatim — an SSH remote stays SSH). A
  base clone created before this fix is healed on its next use rather than
  needing a manual reset — worktree *and* clone strategies, the latter on
  resume — and a repo with no upstream at all is left exactly as it was, so
  purely local work still provisions offline.

  Only the push destination changes. What a session's base branch *tracks* is
  deliberately untouched: the workspace keeps a `mindflock-source` remote
  pointing at your checkout and still refreshes from it, so committed-but-
  unpushed work reaches every session, not just the first one. Two smaller
  consequences: local clone sources are no longer cloned `--filter=blob:none`
  (a blobless clone defers objects to whatever `origin` points at, which would
  have made them network-only; cloning a local path in full costs nothing since
  git hardlinks the object store), and any leftover partial-clone config from a
  pre-fix base clone is cleared during healing for the same reason. A failed
  refresh fetch no longer resets the base to a stale tracking ref, which could
  freeze it at its first snapshot forever.
- **Jira acceptance criteria were being mined from the wrong bullets.** ADF
  headings were flattened without their `#` markers, so no Jira issue ever
  matched the `## Acceptance Criteria` section the miner looks for — every
  top-level bullet in the description was handed to the agent as a criterion
  instead, and an AC section written as prose yielded none at all (routing the
  ticket to clarification). Headings now keep their level.
- **Jira and Linear reached parity in the Assigned-tickets panel.** Both now
  implement `search_assigned_all()`, so the panel lists work you are about to
  move *into* an ingest state rather than only what already matches; both
  populate `Ticket.state`, so their tickets stop collecting in the `No state`
  bucket; and both emit a state `type`, so Done/Canceled states park correctly.
  (GitHub Issues and Asana expose no comparable state model — unchanged.)
- **String ticket ids no longer break the pipeline's logs.** Jira (`PROJ-42`),
  Linear (`ENG-9`) and Asana ids were formatted with `%d`, so every affected log
  line raised inside logging and was dropped — the provisioning narrative simply
  went missing for three of the five trackers.
- **Claims that were not true, on both surfaces.** Checking every sentence of the
  new copy against the code turned up several the old copy had been making for a
  while: that Gemini ships as a provider (Antigravity replaced that CLI; the real
  bundled set is Claude Code, Codex, Antigravity, aider, OpenCode, Cline, Goose);
  that the Windows installer runs the WSL2 setup for you (it probes for `wsl.exe`
  and tells you what to run); that the 15-minute grace period applies to reviews
  (it is on the pull request's own age, and tracker tickets have no age gate);
  that "every unresolved comment" reaches the review prompt (only unresolved
  *inline* review comments do — outdated threads and top-level PR conversation are
  skipped); and that dependencies are installed for any repo (auto-detected for
  Python/uv; other stacks declare `setup_commands`). All corrected, and the docs
  now say plainly that sessions the pipeline *provisions* launch Claude Code —
  any agent CLI drives sessions you start yourself.
- **Website:** a dead script block threw a `TypeError` on every page load and
  33% of the stylesheet was orphaned markup from a mock the video replaced; both
  are gone. The social card no longer bakes in `mindflock.ai/install` (which
  404s), the video poster is a real frame from the demo instead of that card, and
  the download note no longer claims the builds are unsigned (the macOS build is
  self-signed) or gives the Control-click → Open workaround that macOS Sequoia
  removed. `privacy.html` no longer claims the site makes no third-party
  requests or that the app never contacts GitHub — both were contradicted by the
  version check.

## [0.1.4] - 2026-07-29

### Added

- **Rename a session from the sidebar without a dialog.** Clicking the row that
  is *already* selected turns its name into an input with the text
  pre-selected — renaming no longer means finding "Rename…" in the row's
  actions menu (which still works, as does the command palette). The edit is
  held for the double-click window and cancelled by one, so double-click still
  opens the IDE and never flashes an editor. Enter or clicking away commits,
  Escape cancels, and typing the real title back clears the alias.

- **Ticket / PR / issue sessions read as what they are.** Those sessions are
  titled by their machine slug — `sc-12345`, `pr-app-42` — which says nothing
  about the work. The feature name is already in the branch, so the sidebar now
  shows `(tix) add-dark-mode/sc-12345`, `(pr) login-crash/app-42`,
  `(iss) cant-open/app-77`, with the full name, the real session title and the
  branch on hover. Hand-made session names are untouched, and the title itself
  is unchanged — every API path, tmux name and workspace dir is still keyed by
  it.

- **macOS windows use the OS's own controls.** The top bar drew its own
  – □ ✕ top-*right* on every platform, which is the Windows arrangement; on a
  Mac the red/yellow/green buttons belong top-left. The window now keeps the
  real traffic lights there (`titleBarStyle: 'hidden'`), draws none of its own,
  and the bar mirrors to match — the logo, theme toggle and notification bell
  move to the right, where a Mac has nothing else. The sidebar toggle stays
  left, pointing at the sidebar it controls. Every other platform is unchanged,
  as is a browser tab on macOS. The layout follows a shell *capability* flag
  rather than the platform, so an engine updated ahead of the desktop app keeps
  the layout the installed app actually draws instead of stacking its cluster
  on top of that app's own buttons.

### Fixed

- **A force-started PR review / issue / ticket showed nothing in the sidebar
  for as long as it took to clone.** The request was accepted and then spent
  tens of seconds provisioning before it could register a session, so the
  sidebar stayed empty and the start looked like it had failed. Every accepted
  start is now recorded on arrival — before the upstream lookup, not after it —
  and appears immediately as a provisioning row. It also greens the Ticket
  Ingestion / PR Review / Issue Handling dots: those read the *pipeline's*
  activity beacon, which knows nothing about work started from the UI, so a
  forced ticket used to provision for minutes with the light showing idle.
  Their status poll now matches the sessions poll (4s), since the green window
  can be a single provisioning.

- **A mystery `/opt/homebrew/bin/claude` entry above the agents in the New
  Session dialog** (macOS/Homebrew, and any first run where the CLI was found
  on `PATH`). Detection resolves the binary by shelling out to `which`, so it
  reports an absolute path, and that was stored verbatim as the default
  program; the dialog lists any program it doesn't recognise as an extra
  option, which is right for a custom agent and wrong for a path that *is*
  Claude. A resolved path to a known CLI is now folded back to the provider
  name — both when written and when served, so an existing config is fixed
  without editing it — while a genuinely custom program is left exactly as it
  is, because for those the string is the launch command.

- **No plan-usage percentage on macOS — only the reset countdown.** The live
  reader looked for the Claude OAuth token in
  `~/.claude/.credentials.json` only, but on macOS Claude Code keeps those
  credentials in the login Keychain. The token was never found, live usage was
  permanently dark, and the fallback estimate reports a reset time but no
  percentage unless a window budget is configured. The Keychain is now the
  fallback source (macOS only, timeout-bounded, and any failure means "no live
  data" exactly as before — expect a one-time Keychain permission prompt).
  Separately, a live reading that carried a reset time but *no* utilization was
  taken verbatim and blanked a percentage the estimate could still supply; it
  now falls back and says the number is an estimate.

## [0.1.3] - 2026-07-28

### Fixed

- **Settings → System logs: clicking the log path did nothing on Windows.**
  When the engine runs inside WSL while the desktop app runs on Windows, the
  path it reports (`/tmp/mindflock.log`) is a Linux path Explorer can't reach.
  `showItemInFolder` failed silently there (nothing opened, no error), so the
  click had no effect at all. The shell now only claims success when the file
  is actually reachable from the machine the app is on, and otherwise the UI
  falls back to copying the path to the clipboard.
- **Update toast was confusing when only the desktop app was behind.** The
  wordmark shows the *engine* version, which updates on its own, so a user
  whose engine already read the latest version saw "MindFlock X is available"
  and thought it was nagging about the version they were already running. The
  toast now spells out that it's the *desktop app* that's behind and which
  version it's on.

## [0.1.2] - 2026-07-28

## [0.1.1] - 2026-07-28

### Added

- **`mindflock uninstall`** — undoes what MindFlock wrote *outside* its own
  venv, which `uv tool uninstall mindflock` leaves entirely behind. Two of
  those leftovers were actively harmful: session worktrees under
  `~/.mindflock/worktrees` are live git worktrees **registered inside the
  user's repositories** (deleting the directory strands both the worktree and
  its branch, leaving `git worktree list` pointing at nothing), and the
  activity hooks merged into a repo's `.claude/settings.local.json` /
  `.codex/hooks.json` are self-contained inline `python3` with no dependency
  on the `mindflock` binary — so they kept firing after the engine was gone
  and **re-created `~/.mindflock-assistant` after the user deleted it**. The
  command removes worktrees through git (`worktree remove` → `branch -D` →
  `worktree prune`), strips only MindFlock-tagged hook entries, deletes the
  `.mindflock_*` scratch files and their `.git/info/exclude` lines, and sweeps
  orphaned worktree directories. `--dry-run` previews, `--purge` additionally
  deletes `~/.mindflock` + `~/.mindflock-assistant`, `--keep-worktrees` limits
  it to hooks/scratch. It refuses to run while a server is up, never deletes a
  user directory, never touches a worktree outside `~/.mindflock/worktrees`,
  never removes a user's own hooks or a pre-existing branch, and *prints*
  rather than runs the final `uv tool uninstall` (it executes from the venv
  that command deletes).

- **Engine/app version drift detection** — the desktop shell pins the engine
  to its own version at install time but only ran that install when the engine
  was **absent**, so updating the app alone left the old engine running
  indefinitely; `curl install.sh | sh` (which defaults to `main`) could
  likewise push the engine *ahead* of the app, which can trip the `state.json`
  downgrade path. Nothing checked either direction. The engine now reports its
  version in `GET /api/doctor`, the shell compares it on every successful app
  load, and a mismatch raises a toast that reinstalls the engine at the app's
  version with live installer output — over HTTP, so one code path covers
  macOS, Linux and Windows/WSL. Both toasts now share a stacking container so
  a release notice and a drift notice can't overlap.

- **Visible downgrade notice** — when a `state.json` written by a newer
  MindFlock is refused, `LoadState` preserves it as `state.json.newer-<ts>`
  and starts empty. That's non-destructive but looks exactly like data loss,
  and it was only ever reported to a log. The event is now recorded and
  surfaced two ways: a `state-schema` doctor check (`warn`, so it never makes
  `doctor` exit 1 — the installer runs it) and a UI banner naming the
  preserved file and how to recover it, dismissible via
  `POST /api/doctor/ack-state-notice`.

- **In-app update notifications** — the desktop app checks GitHub Releases
  shortly after launch and every 6 hours, and when a newer version exists it
  shows a small toast in the bottom-right with **Update** (opens the release
  page), **Later** (reappears on the next check/launch), and **Skip this
  version** (persisted, stops the nudges for that version). Entirely
  best-effort: any offline / rate-limited / 404 (private repo, no releases)
  response is swallowed, so a prompt only ever appears when a release is
  actually reachable. Override the source repo with `MINDFLOCK_UPDATE_REPO`.

- **One-click engine install on first launch** — the desktop app no longer
  asks anyone to paste a `curl` command. When the offline page finds the
  engine missing it offers an **Install the engine** button, runs `install.sh`,
  and streams the transcript into the window; the app connects on its own when
  it finishes, and a failure keeps the log, offers **Try again**, and falls
  back to showing the manual command. This closes the gap on macOS and Linux,
  where a `.dmg` (drag-copy, no post-install hook) and an AppImage (never
  "installed") have nowhere for the Windows NSIS hook's equivalent to run.
  The script is **bundled into the app** via `extraResources` rather than
  fetched at runtime, so it can't 404 or drift from the build, and the engine
  is pinned to the app's own version tag.
- macOS first launch detects missing **Xcode Command Line Tools** (which
  provide `git`) and opens Apple's installer, instead of failing several
  minutes later inside uv with an unrelated-looking git error. `install.sh`
  does the same check for terminal installs — `command -v git` had been
  passing on Macs where `/usr/bin/git` is only the stub that pops that dialog.

- **Settings → Advanced → Restart server & UI** — re-execs the server process,
  waits for it to answer again, then reloads the window so both halves come
  back fresh (the reload is deliberately *after* the wait; reloading into a
  dead port would land on the offline page). `POST /api/server/restart`
  already existed for the mobile serve-mode toggle; this exposes it on demand,
  for a config change that needs a fresh boot or a server that has gotten
  stuck. Nothing running is lost — sessions are tmux, ingestion is its own
  process.

### Changed

- **The GitHub CLI (`gh`) is now optional, not a requirement.** MindFlock runs
  fully without it — only the GitHub features (push/open PRs and the automated
  PR-review loop) need `gh`, and those simply stay off when it's absent.
  `mindflock doctor` now reports a missing `gh` as `info` ("optional") instead
  of a hard `fail`, so it no longer trips the "required dependency missing"
  exit, and the GitHub connection card shows a calm "off" state rather than an
  attention prompt.
- Settings → Advanced no longer suggests `Ubuntu` as the WSL distro: empty now
  means "your default distro", matching the app and the Windows installer.
- **The settings panels no longer reload from scratch every time you open
  them.** Assigned tickets, open PRs and open issues each fan out to a slow
  upstream (~3s for the ticket sources), and the dialog threw that data away
  on close, so every visit began with a spinner. The lists are now cached
  client-side and shown immediately while a refresh runs behind them, the
  server serves its cached copy rather than making the request wait on the
  sweep, and opening the dialog warms all three in the background. The Refresh
  button still forces a real sweep. For anyone calling the local API directly,
  `GET /api/tickets`, `/api/github/prs` and `/api/github/issues` now take
  `?fresh=1` (skip the cache, await a real sweep) and carry a `stale` boolean in
  the response body; a cached payload is served for up to 5 minutes past its
  20 s TTL, so those routes no longer 502 on an upstream blip once they have a
  list to show. See [docs/web-api.md](docs/web-api.md).

### Fixed

- The make-PR dialog's branch dropdown no longer opens by itself. The input is
  pre-filled with the remembered base branch and auto-focused, so on a repo
  with several branches sharing a name (`staging`, `staging-2`, …) the filtered
  list covered the buttons on every single PR. It now opens only when you type
  or press ↓.
- The frontend could not be built: `@vitejs/plugin-react` had been bumped to a
  major that imports `vite/internal` (vite 8 only) while the project pins
  vite 6, so `npm ci` failed to resolve and `vite build` crashed on config
  load. `vitest` was also missing from `devDependencies` even though the
  config and eight test files import it, which broke `npm run build`'s
  typecheck and left 118 frontend tests unrunnable.
- The offline page is no longer reloaded on every failed connection retry. A
  failed `loadURL` never commits, so the page was already the current one and
  reloading it just restarted its script — harmless flicker before, but it
  would have wiped a running install's transcript several times a minute.
- `install.sh` detected a usable terminal with `[ -r /dev/tty ]`, which passes
  even with no controlling terminal (`/dev/tty` is mode `crw-rw-rw-`), so the
  guided `mindflock doctor --fix` step failed its redirect and was swallowed
  by `|| true`. It now actually opens the device, and honours a new
  `MINDFLOCK_NONINTERACTIVE=1` override.

## [0.1.0] - 2026-07-27

First public release. MindFlock turns one repository into a fleet of parallel,
isolated AI coding sessions — each a git worktree plus a tmux session running a
coding agent, supervised from one desktop app.

### Added

- **Session engine** — instance lifecycle (start / pause / resume / kill), git
  worktree management, tmux/PTY plumbing, and state persisted in
  `~/.mindflock/`. In-place (workspace) sessions measure their stage and diff
  stats against the repo's live default branch.
- **`mindflock` CLI** — `serve`, `doctor` (with interactive `--fix`), and the
  thin-client session commands `new`, `ls`, `attach`, `rm`, `open`, `events`
  against a running server.
- **Desktop app (Electron)** — the one supported client: a frameless window
  with a draggable terminal grid, Agent / Terminal / Diff tabs per session,
  workflow-stage badges, and guided next-step buttons. It finds and
  auto-starts the server by itself (on Windows, inside WSL2 via `wsl.exe`).
- **Guided git workflow** — one-click commit → push → PR → merge driven by the
  `gh` CLI, plus "needs rebase" awareness (`↓N` on the stage pill when the
  branch is behind its base), an "Update from `<base>`" action that runs
  visibly in the session shell so conflicts stay resolvable, and a toolbar
  "⟳ Update all (N)".
- **Phone UI** — `mindflock serve tailscale` prints a QR code; the mobile UI
  at `/m` carries the same guided git action bar, gated by an access token.
- **Ticket ingestion** — polls Shortcut for assigned stories and GitHub for
  reviewed PRs, provisions a workspace, and launches a seeded agent session
  per story / per PR.
- **Provider framework** — Claude Code built in, with Codex, Antigravity,
  OpenCode, Cline, Goose and Aider bundled; add any coding-agent CLI via a
  TOML file. Shared hooks-based activity detection (working / idle /
  needs-input), model pricing, and rolling token/cost history.
- **Wedged-session watchdog** — a session that looks idle but has been sitting
  on unfinished work (uncommitted diff or unpushed commits) for 20+ minutes
  surfaces in the attention bell as "possibly stuck".
- **Extension points** — shell hooks on every session event, a
  `WS /api/events` stream, and in-process Python + ES-module addons.
- **Installers** — `install.sh` for the server/CLI (Linux, macOS, WSL2), and
  per-OS desktop builds attached to every tagged release: an NSIS `.exe` for
  Windows (which also bootstraps the CLI inside WSL), a universal `.dmg` for
  macOS, and an `AppImage` for Linux.

### Security

- The server binds `127.0.0.1` by default. Phone/tailnet access is an explicit
  opt-in via `mindflock serve tailscale`, which auto-enables the access-token
  gate.
- Browser-attack guards enforced even when the token gate is off: requests
  from a foreign `Origin` are refused (HTTP 403 / WS close 4403 — WebSocket
  handshakes ignore CORS, so this closes the cross-site terminal-hijack
  vector), and in local mode non-loopback `Host` headers are refused (DNS
  rebinding).
- The access token can be regenerated (Settings → Security → Regenerate,
  `POST /api/settings/auth-token/rotate`), invalidating every issued cookie,
  QR code, and paired device at once; the rotating browser stays signed in.
- The startup banner written to `mindflock.log` redacts the access token and
  omits the QR code (which encodes it) — `GET /api/logs` serves that file back
  out.
- `install.sh` pins the `uv` installer version and verifies its sha256 before
  running it, and resolves the requested branch/tag to a full commit SHA
  (printed) before installing.
- Threat model, hardening guidance, and the disclosure contact live in
  [SECURITY.md](SECURITY.md).

### Known limitations

- Desktop installers are **unsigned**: macOS Gatekeeper and Windows SmartScreen
  warn on first launch. The README lists the per-OS bypass.
- Native Windows is not a supported host for the engine (no tmux, no Unix
  PTYs) — WSL2 is required, and the Windows installer bootstraps it.

[Unreleased]: https://github.com/MindFlock/MindFlock/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/MindFlock/MindFlock/releases/tag/v0.3.0
[0.2.1]: https://github.com/MindFlock/MindFlock/releases/tag/v0.2.1
[0.2.0]: https://github.com/MindFlock/MindFlock/releases/tag/v0.2.0
[0.1.17]: https://github.com/MindFlock/MindFlock/releases/tag/v0.1.17
[0.1.16]: https://github.com/MindFlock/MindFlock/releases/tag/v0.1.16
[0.1.15]: https://github.com/MindFlock/MindFlock/releases/tag/v0.1.15
[0.1.14]: https://github.com/MindFlock/MindFlock/releases/tag/v0.1.14
[0.1.13]: https://github.com/MindFlock/MindFlock/releases/tag/v0.1.13
[0.1.12]: https://github.com/MindFlock/MindFlock/releases/tag/v0.1.12
[0.1.11]: https://github.com/MindFlock/MindFlock/releases/tag/v0.1.11
[0.1.10]: https://github.com/MindFlock/MindFlock/releases/tag/v0.1.10
[0.1.9]: https://github.com/MindFlock/MindFlock/releases/tag/v0.1.9
[0.1.8]: https://github.com/MindFlock/MindFlock/releases/tag/v0.1.8
[0.1.7]: https://github.com/MindFlock/MindFlock/releases/tag/v0.1.7
[0.1.6]: https://github.com/MindFlock/MindFlock/releases/tag/v0.1.6
[0.1.5]: https://github.com/MindFlock/MindFlock/releases/tag/v0.1.5
[0.1.4]: https://github.com/MindFlock/MindFlock/releases/tag/v0.1.4
[0.1.3]: https://github.com/MindFlock/MindFlock/releases/tag/v0.1.3
[0.1.2]: https://github.com/MindFlock/MindFlock/releases/tag/v0.1.2
[0.1.1]: https://github.com/MindFlock/MindFlock/releases/tag/v0.1.1
[0.1.0]: https://github.com/MindFlock/MindFlock/releases/tag/v0.1.0
