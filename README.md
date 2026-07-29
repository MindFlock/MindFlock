<div align="center">

# 🐦‍⬛ MindFlock

**Run a flock of AI coding agents — each in its own git worktree and tmux
session — supervised from one desktop app.**

[![CI](https://github.com/MindFlock/MindFlock/actions/workflows/ci.yml/badge.svg)](https://github.com/MindFlock/MindFlock/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/MindFlock/MindFlock?include_prereleases)](https://github.com/MindFlock/MindFlock/releases)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](pyproject.toml)
[![Platforms](https://img.shields.io/badge/platform-Linux%20%7C%20macOS%20%7C%20Windows%20(WSL2)-lightgrey)](#requirements)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen)](CONTRIBUTING.md)

[What is it?](#what-is-mindflock) •
[How is this different?](#how-is-this-different) •
[Quick Start](#quick-start) •
[Download](#download) •
[How It Works](#how-it-works) •
[Documentation](#documentation)

</div>

<div align="center">

![MindFlock demo — four coding agents running in parallel, each in its own git worktree and a different state: one working, one waiting for your answer, one running commit hooks, one reviewed and ready to push](docs/demo.gif)

<sub>Four agents · four git worktrees, side by side — one working, one waiting for your input, one running commit hooks, one reviewed with checks green.</sub>

</div>

## Why

Staring at a terminal waiting for an AI agent to finish is mind-numbingly
boring — so you run several at once, and then you have six terminal windows and
no idea which one needs you. MindFlock exists to answer one question at a
glance: **which agent needs me right now?**

## What is MindFlock?

MindFlock turns one repository into a fleet of parallel, isolated AI coding
sessions. Each session is a **git worktree** (or clone) plus a **tmux session**
running the coding-agent CLI of your choice — **any agent, declared in a TOML
file** — surfaced in the desktop app as a live terminal with a guided
**commit → push → PR → merge** workflow. An optional ingestion pipeline can
watch your issue tracker (Shortcut, Jira, Linear, GitHub Issues, Asana) and
reviewed pull requests, and spin up a ready-to-work session for each —
automatically.

## How is this different?

Parallel-agent orchestrators are a crowded category, and several of them are
good. Here's an honest read on where MindFlock actually differs.

| | **MindFlock** | **Claude Squad** | **Conductor** | **Claude Code Agent Teams** |
|---|---|---|---|---|
| Interface | Cross-platform desktop app **+ phone UI** | Terminal TUI | Native macOS app | Built into the CLI |
| Platforms | Linux, macOS, Windows (WSL2) | Linux, macOS, WSL | macOS only | Anywhere Claude Code runs |
| Agent CLI | **Any — declared in a TOML file** | Several bundled (Claude Code, Codex, Gemini, aider, OpenCode) | Claude Code | Claude only |
| Session isolation | git worktree + tmux | git worktree + tmux | git worktree | git worktree |
| Guided git workflow | **one-click commit → push → PR → merge** | — | — | — |
| Ticket → session ingestion | **Shortcut · Jira · Linear · GitHub Issues · Asana** | — | — | — |
| PR-review → session ingestion | **reviewed PRs become sessions** | — | — | — |
| Hook-based session state | **provider hooks: working / idle / needs-input** | — | — | — |
| Token & cost tracking | **per provider, for every CLI** | — | — | per Claude session |
| Remote / phone control | **tailnet + QR, full action bar** | — | — | — |

MindFlock is a ground-up parallel-agent workspace. A few things set it apart:

- **Provider-agnostic by design.** A coding agent in MindFlock is just a TOML
  file — binary, launch args, prompt seeding, activity-detection hooks, model
  pricing — so it drives *any* CLI (Claude Code, Codex, Gemini, aider,
  OpenCode, or one nobody's heard of) with the same working / idle /
  needs-input detection and token + cost tracking. Adding an agent is a config
  change, not a patch.
- **Work comes to the agents.** MindFlock can turn tickets and code review into
  sessions on its own: it watches your issue tracker (Shortcut, Jira, Linear,
  GitHub Issues, Asana) for work assigned to you, and GitHub for PRs that came
  back with review comments, then provisions a worktree and launches a seeded
  agent for each — nothing typed.
- **The whole git loop is guided.** Every session carries a one-click
  commit → push → PR → merge action bar (via `gh`) and live workflow-stage
  badges, so you drive the change home without leaving the app.
- **Supervise from anywhere.** `mindflock serve tailscale` prints a QR code;
  the mobile UI carries the same guided action bar, so you can unblock an agent
  from your phone.

**When another tool may fit better:**

- **[Conductor](https://www.conductor.build/)** is a native macOS (SwiftUI)
  app — if you're Mac-only and specifically want a native client, it's a solid
  pick.
- **Claude Code's built-in Agent Teams + worktrees** are free and already
  installed. If you only ever use Claude and live in the terminal, you may not
  need a separate app.
- **[Claude Squad](https://github.com/smtg-ai/claude-squad)** is a mature
  multi-agent TUI that also drives several CLIs. If you'd rather stay in the
  terminal than run a desktop app, it's the closer fit.

## Features

- 🎫 **Ticket & PR-review ingestion** — watches your issue tracker (Shortcut,
  Jira, Linear, GitHub Issues, Asana) for assigned work and GitHub for reviewed
  PRs, then provisions a workspace and launches a seeded agent session for each.
- 🔌 **Provider-agnostic** — every coding-agent CLI is just a TOML file (Claude
  Code, Codex, Gemini, aider, OpenCode bundled; add your own). Shared
  hook-based activity detection (working / idle / needs-input) and token & cost
  tracking apply to all of them.
- 🖥️ **Desktop app** (Electron) — a draggable terminal grid with Agent /
  Terminal / Diff / Queue tabs per session, workflow-stage badges, and guided
  next-step buttons.
- 📱 **Phone UI** — `mindflock serve tailscale` prints a QR code; the mobile
  UI at `/m` carries the same guided git action bar, so the full flow drives
  from a phone. Auth-token protected — never open to the LAN unauthenticated.
- 🌳 **Isolated workspaces** — every session gets its own git worktree, so
  agents never step on each other (or on you).
- 🔀 **Guided git workflow** — one-click commit → push → PR → merge, driven by
  the `gh` CLI.
- ⚡ **Terminal-first, too** — the `mindflock` CLI drives the same sessions as
  the app (`new`, `ls`, `attach`, `rm`, `open`, `events`), so terminal and UI
  stay one system.
- 🧩 **Extensible** — shell hooks on every session event, a `WS /api/events`
  stream, and in-process Python + ES-module addons.

## Quick Start

[Install first](#download) — one command — then, from zero to a supervised
agent session (this CLI flow is verified in CI on every push by
[`scripts/quickstart-verify.sh`](scripts/quickstart-verify.sh)):

```bash
mindflock doctor                       # 1. everything installed + authenticated?
cd ~/code/your-repo                    # 2. the repo you want the agents to work on
mindflock serve                        # 3. start the server (the app can also do this for you)
mindflock new . -p "fix the failing tests"   # 4. (new terminal) spawn a session
mindflock ls                           # 5. watch it: TITLE REPO STATUS ACTIVITY STAGE DIFF COST
```

Open the **MindFlock desktop app** — the session is a live terminal; each one
is an isolated git worktree, with Diff view and guided
commit → push → PR → merge buttons. `mindflock attach <title>` drops your
terminal into the same tmux session.

`mindflock serve` binds localhost only; run `mindflock serve tailscale` to opt
into phone/tailnet access — an auth token + QR code are printed, and scanning
the QR opens the phone UI at `/m`.

## Download

<div align="center">

[![Download for Windows](https://img.shields.io/badge/Download-Windows%20(WSL2)-0078D6?style=for-the-badge&logo=windows&logoColor=white)](https://github.com/MindFlock/MindFlock/releases/latest/download/MindFlock-Setup.exe)
[![Download for macOS](https://img.shields.io/badge/Download-macOS-000000?style=for-the-badge&logo=apple&logoColor=white)](https://github.com/MindFlock/MindFlock/releases/latest/download/MindFlock.dmg)
[![Download for Linux](https://img.shields.io/badge/Download-Linux-FCC624?style=for-the-badge&logo=linux&logoColor=black)](https://github.com/MindFlock/MindFlock/releases/latest/download/MindFlock.AppImage)

<sub>Each button downloads the newest release · [all versions, checksums, and the Python wheel](https://github.com/MindFlock/MindFlock/releases)</sub>

</div>

| | What you get | Anything else? |
|---|---|---|
| **Windows** | `MindFlock-Setup.exe` — the app **and** the engine (which runs inside WSL2). | **Set up WSL2 first — see the note below.** With a working WSL2 distro in place, the installer does the rest. |
| **macOS** | `MindFlock.dmg` (universal — Apple silicon & Intel). Drag to Applications. | Nothing. First launch offers **Install the engine** — one click, no terminal. |
| **Linux** | `MindFlock.AppImage`. `chmod +x` it and run. | Same — first launch installs the engine for you. |

The app auto-starts the engine every time after that — no terminal, no manual
steps. If the engine is missing, the app's waiting page says so and shows the
exact command.

<details>
<summary><b>⚠️ Windows: finish setting up WSL2 <i>before</i> you run the installer</b></summary>

The engine runs inside WSL2, so a Linux distribution must be **fully installed
and launchable first** — not just `wsl --install` half-run. In PowerShell:

```powershell
wsl --install     # if WSL isn't set up yet — then REBOOT your PC
wsl -l -v         # verify a distro (e.g. Ubuntu) is listed
wsl               # verify this drops you into a Linux shell (first run asks you to create a user), then type: exit
```

Only once `wsl` opens a Linux shell should you run `MindFlock-Setup.exe`. A
**partially set-up WSL** — installed but with no distro, or with a reboot still
pending — is the single most common Windows install failure.

</details>

<details>
<summary>These builds aren't from a paid developer account — what you'll see on first launch</summary>

MindFlock has no Apple Developer ID or Windows Authenticode certificate (both
are paid, per-year subscriptions). The macOS build *is* signed, but with a
free self-signed certificate — enough to keep macOS from re-asking for folder
permission on every launch, not enough to satisfy Gatekeeper. So on first
launch:

**macOS — "Apple could not verify MindFlock is free of malware."**

That dialog has no **Open** button, and the old Control-click → **Open**
shortcut was removed in macOS Sequoia. To open it anyway (you only do this
once):

1. Drag **MindFlock** into your **Applications** folder and try to open it. The
   warning appears — click **Done** to dismiss it.
2. Open the  menu → **System Settings…** → **Privacy & Security**.
3. Scroll down to the **Security** section. You'll see a line like
   *"MindFlock was blocked to protect your Mac."* with an **Open Anyway**
   button next to it. Click it.
4. Confirm with **Open Anyway** again and authenticate with Touch ID or your
   password. MindFlock launches, and macOS remembers the choice — you won't be
   asked again.

Prefer the terminal? This does the same thing in one line:

```sh
xattr -dr com.apple.quarantine /Applications/MindFlock.app
```

The **first time** MindFlock reads a folder under Documents, Desktop,
Downloads, or an external drive, macOS asks *"MindFlock would like to access
files…"* — click **Allow**. Because the app is signed, that grant sticks; it
won't ask again for that folder.

- **Windows** — SmartScreen's "Windows protected your PC". Click **More
  info** → **Run anyway**.
- **Linux** — no prompt; AppImages aren't signed by convention.

Every release ships a `.sha256` beside each installer (and `SHA256SUMS` for
the Python artifacts), and the builds are produced in public by
[the Release workflow](.github/workflows/release.yml) straight from the tag —
so you can check both the bytes and what produced them.

</details>

## Installation

Most people want the [download buttons above](#download). This section is the
same thing spelled out, plus every other way in.

Two pieces: the **server/CLI** (runs the engine) and the **desktop app**
(the one client, Electron — [electron/README.md](electron/README.md)). On
Windows the `.exe` installs both; elsewhere it's two commands.

### 1. Server + CLI

On Linux, macOS, or inside WSL on Windows (the Windows installer runs this for
you, inside your default distro):

```bash
curl -LsSf https://raw.githubusercontent.com/MindFlock/MindFlock/main/install.sh | sh
```

No repo clone, no Python setup needed — the installer brings
[uv](https://docs.astral.sh/uv/) (no root), installs the `mindflock` command,
and finishes with `mindflock doctor` so anything still missing (git, tmux,
`claude`) is listed with the exact install command for your platform.

> **Note on `curl | sh`:** it isn't a blind one — the uv installer it fetches
> is version-pinned and sha256-verified before it runs, and the requested
> branch/tag is resolved to a full commit SHA that is printed and pinned for
> the install, an audit trail for what actually ran. Threat model and
> disclosure contact: [SECURITY.md](SECURITY.md).

<details>
<summary>Prefer your own tooling? (uv / pipx / from source)</summary>

```bash
# uv
uv tool install "mindflock[web] @ git+https://github.com/MindFlock/MindFlock"

# pipx
pipx install "mindflock[web] @ git+https://github.com/MindFlock/MindFlock"

# from a clone (contributors)
git clone https://github.com/MindFlock/MindFlock
cd MindFlock
uv sync --group web
```

</details>

### 2. Desktop app

Use the [download buttons](#download), grab any past build from
[Releases](https://github.com/MindFlock/MindFlock/releases), or build it
yourself:

```bash
cd electron && npm install && npm run dist
```

It finds — and auto-starts — the server by itself.

### Requirements

| Requirement | Notes |
|---|---|
| **OS** | Linux, macOS (Apple silicon & Intel), Windows **via WSL2** (the app runs natively on Windows; the engine lives in WSL2 — native Windows has no tmux/PTYs) |
| `git` ≥ 2.17, `tmux` ≥ 2.4 | On `PATH`; checked, with versions, by `mindflock doctor` |
| A coding-agent CLI | `claude` (Claude Code) by default |
| `gh` (GitHub CLI) | For the push/PR/merge workflow |
| Optional | `cursor` (IDE integration), `tailscale` (phone access) |

## How It Works

```
 Shortcut stories ─┐                            ┌─ desktop app (Electron)
 GitHub PR reviews ┴─► ingestion pipeline ─┐    ├─ phone UI at /m (tailnet QR)
                                           ▼    ▼
                                    ┌──────────────────┐
                                    │  session engine  │  git worktrees + tmux
                                    │  (backend.*)     │  + provider launch
                                    └──────────────────┘
                                           ▲
                            FastAPI server (backend.web)
```

| Component | Package | What it does |
|---|---|---|
| **Session engine** | `backend.session`, `backend.config`, `backend.cmd`, `backend.log` | Instance lifecycle (start/pause/resume/kill), git worktree management, tmux/PTY plumbing, persisted state in `~/.mindflock/`. |
| **Server + UI** | `backend.web` | FastAPI server + the UI the desktop app renders: draggable terminal grid, Agent/Terminal/Diff tabs per session, workflow-stage badges with guided next-step buttons, token/cost usage, Cursor integration, phone UI at `/m`, addon framework. |
| **Ingestion pipeline** | `backend.ticket_ingestion` | Polls Shortcut for assigned stories and GitHub for reviewed PRs; validates, provisions a workspace, and launches a seeded Claude session per story / per PR. |
| **Provider framework** | `backend.providers` | Pluggable coding-agent CLIs (Claude built in; aider/codex and others bundled; add your own via TOML). Shared hooks-based activity detection, model pricing, and rolling token/cost usage history. |

Something not working? Run `mindflock doctor` and check
[TROUBLESHOOTING.md](TROUBLESHOOTING.md) — it's indexed by the exact error
text the tool prints.

## Usage

### Running the server

The desktop app auto-starts the server when it isn't running; these commands
are for headless/manual use:

```bash
mindflock serve              # localhost only (127.0.0.1) — the default
mindflock serve tailscale    # bind 0.0.0.0: tailnet mobile URL + QR + auth token
mindflock serve --port 9000  # custom port (default 8765)
```

Run `mindflock serve` from inside the git repository you want to manage — the
startup banner prints which repo that is (and warns if it's the MindFlock
checkout itself). `mindflock doctor` (also served at `GET /api/doctor`) checks
every dependency and prints a platform-appropriate fix for anything missing.

In the app, **+ New** creates a session (worktree + tmux + agent); click a
session to type into its live terminal. The phone UI lives at `/m` (scan the
startup QR).

### Terminal session control

With a server running, the same sessions can be driven straight from any
terminal — the CLI talks to the server's API (auto-discovered on port 8765, or
`--host`/`--port` / `MINDFLOCK_HOST`/`MINDFLOCK_PORT`):

```bash
mindflock new                    # session on the current repo (title = basename)
mindflock new ~/code/webapp -p "fix the failing tests"
mindflock ls                     # TITLE REPO STATUS ACTIVITY STAGE DIFF COST (--json for scripts)
mindflock attach webapp          # tmux attach to the agent's terminal (prefix ok; needs a real TTY)
mindflock rm webapp --yes        # end a session, keep its worktree (prompts without --yes)
mindflock open webapp            # open the workspace in the configured IDE
mindflock events --follow        # live event stream (great for hook debugging)
```

See [docs/cli.md](docs/cli.md) for the full command reference.

### Ticket-ingestion pipeline

Configure it from the web UI's ⚙ **Settings** dialog (Ticketing / Repository /
GitHub sections) — values are saved to `~/.mindflock/settings.json` (mode
`0600`, never committed). No file editing needed.

```bash
python -m backend.ticket_ingestion  # run from the repo root
```

For headless/scripted runs you can instead use a `config.toml` (an optional
advanced override): copy [`config.toml.example`](config.toml.example) to
`config.toml` and fill in your values. Every field resolves through
`env var → ~/.mindflock/settings.json → config.toml → default`, so the
Settings UI, an environment variable, or the file all work.

Or toggle it from the web UI sidebar (**Ticket Ingestion** bar), which runs
it as a managed subprocess and tails its log. The pipeline is a singleton per
directory (`.mindflock-pipeline.lock`); a second copy exits cleanly.

### Extensions & hooks

Every session event (created, status/activity/stage changed, paused, deleted…)
flows through a server-side event bus with three extension seams:

1. **Shell hooks** — drop an executable in `~/.mindflock/hooks/<event>/`
   (env vars + JSON envelope on stdin — e.g. a desktop notification when an
   agent needs input).
2. **WebSocket** — subscribe an external tool to the `WS /api/events` stream.
3. **In-process addons** — a Python `Addon` + an ES module the UI loads
   generically (the bundled **notify** addon is the worked example).

See [docs/extensions.md](docs/extensions.md) for the full guide.

## Configuration & state

| Path | Contents |
|---|---|
| `~/.mindflock/` | Engine config + session state (`config.json`, `state.json`, `worktrees/`) **and `settings.json`** (the web Settings store: API keys, repo/ticketing config, provider + platform settings; mode `0600`, never committed) |
| `~/.mindflock-assistant/` | Assistant workspace, provider TOMLs, pricing/usage caches, scroll-speed, exit markers |
| `./config.toml` | Optional advanced override for the pipeline + workspace provisioning (gitignored; superseded by `settings.json` / env vars — see [`config.toml.example`](config.toml.example)) |
| `./state.json` | Pipeline dedup state (processed stories/PRs; **different file** from the engine's `~/.mindflock/state.json`) |
| `./workspaces/` | Provisioned story/PR workspaces, `_base_*` canonical clones, `_testmon_refresher` |
| `./logs/` | `pipeline.log`, `ticket-ingestion.log` |

See [docs/configuration.md](docs/configuration.md) for the full reference.

### Uninstalling

`uv tool uninstall mindflock` removes the venv and the `mindflock` shim, but
not the worktrees MindFlock registered *inside your repositories*, nor the
activity hooks it merged into their `.claude`/`.codex` settings — which keep
firing (and keep re-creating `~/.mindflock-assistant`) after the engine is
gone. Undo those first:

```bash
mindflock uninstall --dry-run   # see exactly what would be removed
mindflock uninstall             # worktrees, hooks, scratch files (keeps settings + history)
mindflock uninstall --purge     # …and delete ~/.mindflock + ~/.mindflock-assistant
uv tool uninstall mindflock     # finally, the engine itself
```

On macOS the desktop app additionally leaves `/Applications/MindFlock.app`,
`~/Library/Application Support/MindFlock`, `~/Library/Logs/MindFlock` and
`~/Library/Preferences/ai.mindflock.desktop.plist`. Full details in
[docs/cli.md](docs/cli.md#mindflock-uninstall---purge---keep-worktrees---dry-run---yes).

## Documentation

| Doc | Contents |
|---|---|
| [docs/architecture.md](docs/architecture.md) | System overview, components, data flow, on-disk state map |
| [docs/extensions.md](docs/extensions.md) | Extension guide: shell hooks, `/api/events` WebSocket, in-process addons, `window.mindflock` client API |
| [docs/configuration.md](docs/configuration.md) | `config.toml` reference, `~/.mindflock/` + `~/.mindflock-assistant/`, environment variables |
| [docs/session-engine.md](docs/session-engine.md) | Instance lifecycle, git worktrees, tmux/PTY, provisioned mode |
| [docs/cli.md](docs/cli.md) | `mindflock` CLI: serve, doctor, uninstall, and terminal session control (new/ls/attach/rm/open/events) |
| [docs/web-api.md](docs/web-api.md) | Complete HTTP + WebSocket API reference |
| [docs/web-ui.md](docs/web-ui.md) | Frontend guide: grid, tabs, stages, shortcuts, mobile, addons |
| [docs/providers.md](docs/providers.md) | Provider framework, adding a CLI via TOML, pricing & usage tracking |
| [docs/ingestion-pipeline.md](docs/ingestion-pipeline.md) | Story + PR ingestion flows, state, testmon refresher |
| [docs/development.md](docs/development.md) | Dev setup, test suite, project layout, known issues |

## Development

```bash
git clone https://github.com/MindFlock/MindFlock
cd MindFlock
uv sync --group web --group dev   # web = FastAPI server deps, dev = pytest
uv run mindflock doctor           # checks git/tmux/gh/agent CLI
uv run mindflock serve            # localhost:8765, from a repo you want to manage
```

Run the test suite (unit + property-based [hypothesis] + integration):

```bash
uv run pytest
```

See [docs/development.md](docs/development.md) for the full test map and
project layout.

## Contributing

Contributions are welcome — bug reports, docs fixes, and code alike. Please
read [CONTRIBUTING.md](CONTRIBUTING.md) first. There's no CLA to sign: just
add a `Signed-off-by` line to your commits with `git commit -s`, certifying
you wrote the patch under the
[Developer Certificate of Origin](https://developercertificate.org/).

> **Project status — solo maintainer.** MindFlock is built and maintained by one
> person in evenings and weekends. It is offered as-is under Apache-2.0; issues
> and pull requests are read and reviewed on a best-effort basis, and there is no
> support SLA. Bug reports (with `mindflock doctor` output) and PRs are very
> welcome — just expect a hobby-project response time.

- 🐛 [Report a bug](https://github.com/MindFlock/MindFlock/issues)
- 💡 [Request a feature](https://github.com/MindFlock/MindFlock/issues)
- 🔒 [Report a security issue](SECURITY.md)

If MindFlock is useful to you, a ⭐ helps other people find it.

## License

Licensed under the [Apache License 2.0](LICENSE) — free and open source.

The **MindFlock** name and logo are trademarks; a license to the code is not a
license to the marks. See [TRADEMARKS.md](TRADEMARKS.md) for the trademark
policy.
