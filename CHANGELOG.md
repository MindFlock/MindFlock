# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/MindFlock/MindFlock/compare/v0.1.4...HEAD
[0.1.4]: https://github.com/MindFlock/MindFlock/releases/tag/v0.1.4
[0.1.3]: https://github.com/MindFlock/MindFlock/releases/tag/v0.1.3
[0.1.2]: https://github.com/MindFlock/MindFlock/releases/tag/v0.1.2
[0.1.1]: https://github.com/MindFlock/MindFlock/releases/tag/v0.1.1
[0.1.0]: https://github.com/MindFlock/MindFlock/releases/tag/v0.1.0
