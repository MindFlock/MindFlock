# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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

### Fixed

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

[Unreleased]: https://github.com/MindFlock/MindFlock/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/MindFlock/MindFlock/releases/tag/v0.1.0
