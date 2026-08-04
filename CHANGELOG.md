# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/MindFlock/MindFlock/compare/v0.1.10...HEAD
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
