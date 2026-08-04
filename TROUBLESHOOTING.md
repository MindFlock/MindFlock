# Troubleshooting

Search this page for the **exact error text** MindFlock printed (Ctrl-F).
First move in every case: run `mindflock doctor` — it checks every dependency
and prints the fix command for your platform. Include its output when filing
an issue.

---

## Install / startup

### `native Windows is not a supported MindFlock host`

The engine needs tmux and Unix PTYs, which don't exist on native Windows.
Install [WSL2](https://learn.microsoft.com/windows/wsl/install), open your
distro's shell, and run the installer there. The optional
[Electron shell](electron/README.md) then gives you a native-Windows window
onto the WSL-hosted server.

### `MindFlock: the WSL setup did not finish` (Windows installer)

The `.exe` installs the app **and** runs `install.sh` inside your default WSL
distro. That second half is deliberately non-fatal, so the app is installed
either way — click **Show details** in the installer to see why it stopped.
The usual causes:

- **No WSL.** Run `wsl --install` in PowerShell, reboot, then re-run the
  MindFlock installer (re-running is a safe in-place upgrade).
- **WSL only half-set-up.** `wsl --install` needs a **reboot** to finish, and a
  distro must actually be installed and launched once. Run `wsl -l -v`: if it
  lists no distro, run `wsl --install -d Ubuntu`, reboot if asked, then open the
  distro once (it creates your Linux user) before re-running. A partially set-up
  WSL is the most common Windows install failure.
- **No `curl` in the distro.** `sudo apt install curl`, then re-run.
- **It installed into the wrong distro.** The installer uses your *default*
  distro, and so does the app — but only if `MINDFLOCK_WSL_DISTRO` is unset.
  `wsl -l -v` shows which is default; `wsl -s <name>` changes it.

You never have to re-run the installer to fix this. Opening a shell in the
right distro and running what it would have run is equivalent:

```bash
curl -LsSf https://raw.githubusercontent.com/MindFlock/MindFlock/main/install.sh | sh
```

### `install finished but 'mindflock' is not on PATH`

uv links tools into `~/.local/bin`. Add it to your PATH (uv prints the exact
line for your shell, typically):

```sh
export PATH="$HOME/.local/bin:$PATH"
```

Put that in your `~/.bashrc` / `~/.zshrc`, open a new shell, and re-run
`mindflock doctor`.

### `Web dependencies not installed — run: uv sync --group web`

You're running from a source checkout without the web dependency group.
From the repo root: `uv sync --group web`, then `uv run mindflock serve`.
(Installer-based installs never hit this — they install `mindflock[web]`.)

### `uv did not land on PATH`

The uv installer finished but your shell can't see it. Open a new shell, or
`export PATH="$HOME/.local/bin:$PATH"` and re-run the installer. Manual
install docs: <https://docs.astral.sh/uv/getting-started/installation/>

---

## Doctor failures (`mindflock doctor` / the ✗ lines at `serve` startup)

### `git` — `not found on PATH — sessions fork git worktrees, so git is required`

Install git: `sudo apt install git` (Debian/Ubuntu), `sudo dnf install git`
(Fedora), `sudo pacman -S git` (Arch), `xcode-select --install` or
`brew install git` (macOS). Doctor prints the right one for your machine.

### `git … is too old — 'git worktree remove' needs git ≥ 2.17`

MindFlock manages sessions as git worktrees and needs `git worktree remove`
(added in git 2.17, 2018). Upgrade git via your package manager.

### `tmux` — `not found on PATH — sessions cannot start without it`

Every agent runs inside a detached tmux session. Install tmux:
`sudo apt install tmux` / `sudo dnf install tmux` / `sudo pacman -S tmux` /
`brew install tmux`.

### `tmux … is too old — MindFlock's copy-mode scroll control needs tmux ≥ 2.4`

The terminal scroll-speed feature uses `send-keys -X -N` (tmux 2.4, 2017).
Upgrade tmux via your package manager; on old LTS distros use the
[tmux appimage/backport](https://github.com/tmux/tmux/wiki/Installing).

### `GitHub CLI (gh)` — `not found (optional — only PR create/merge and PR review need it; pushing uses plain git)`

Informational, not a failure — nothing about this line stops a session, a
commit or a push. **Push** is plain `git push -u origin <branch>` over whatever
remote your repo already has (SSH or HTTPS); `gh` is never in that path.

`gh` only makes **Make PR** and **Merge** one click. Without it MindFlock falls
back to the GitHub REST API using a token (Intake → Pull requests), and without a
token to a prefilled compare URL it hands your browser. The PR-review poller —
the one that turns reviewed PRs back into sessions — likewise runs on a token
when `gh` is absent.

Want the one-click path anyway? Install: <https://cli.github.com>, then
`gh auth login`.

### `installed but not authenticated` (gh)

Run `gh auth login` and follow the prompts. This affects **Make PR** / **Merge**
and the PR-review poller only — an unauthenticated `gh` never blocks a push,
because pushing uses your own git remote and your own git credentials (SSH key
or credential helper), not `gh`.

### `agent CLI (claude)` — `` `claude` not found on PATH ``

The default coding agent is Claude Code:
`npm install -g @anthropic-ai/claude-code`
(docs: <https://docs.anthropic.com/en/docs/claude-code/setup>). Using a
different CLI? Set its binary path in Settings → Agent CLI, or add a
provider TOML ([docs/providers.md](docs/providers.md)).

### `configured binary … is missing or not executable`

Settings → Agent CLI has a binary-path override pointing at a file that
doesn't exist (or isn't executable). Fix the path there, or clear it to fall
back to PATH lookup.

### `CLI is installed but no sign of a login was found`

Claude Code is installed but has never logged in. Run `claude` once in any
terminal and complete the login; MindFlock then finds the credential state in
`~/.claude.json` / `~/.claude/.credentials.json` (or set `ANTHROPIC_API_KEY`).

---

## Pushing and pull requests

**The one thing to know:** pushing is plain git — `git push --no-verify -u
origin HEAD` from the **Push** button, `git push -u origin <branch>` from the
engine — run in the session's own worktree against whatever remote that repo
already has. MindFlock reads your remote URL and uses it verbatim: it never
rewrites it, never converts SSH to HTTPS or back, and never routes a push
through `gh`. So a push fails here for exactly the reasons it would fail in your
own terminal, and the fix is the same one. If `git push` works in your terminal,
it works here.

### `Permission denied (publickey)`

Your SSH remote can't authenticate. Test it directly:

```bash
ssh -T git@github.com          # expects "Hi <you>! You've successfully authenticated"
```

If that fails, your key isn't loaded or isn't on your GitHub account:

```bash
ssh-add -l                     # empty / "Could not open a connection"? start the agent:
eval "$(ssh-agent -s)" && ssh-add ~/.ssh/id_ed25519
```

Two MindFlock-specific wrinkles. First, sessions run under **tmux**, and a tmux
server that outlived your ssh-agent is still holding the *old* `SSH_AUTH_SOCK`;
if pushing works in a fresh terminal but not in a session, `tmux kill-server`
(this kills running sessions — pause first) and start them again. Second, on
Windows the engine lives in
**WSL2** — the key has to exist inside the WSL filesystem (or be forwarded
there); a key that only Windows' OpenSSH agent holds is invisible to it.

### `could not read Username for 'https://github.com'`

An HTTPS remote with no credential helper: git has nowhere to get a password
from and there is no terminal to prompt on. Either give git a credential source:

```bash
gh auth setup-git                          # if you have gh
git config --global credential.helper store   # or your OS keychain helper
```

…or switch that repo's remote to SSH, which MindFlock will then use as-is:

```bash
git remote set-url origin git@github.com:Org/repo.git
```

Do it in your own clone — the session worktree inherits the repository's
remotes, so the change applies to sessions already running.

### **Make PR** / **Merge** unavailable, or "no way to reach GitHub"

Opening and merging a PR is the one part of the flow that has to talk to the
GitHub *API*, and MindFlock needs one of two credentials to do it: an
authenticated `gh`, or a GitHub token. The remedy is the sentence the app itself
prints, and it is an either/or:

> add a GitHub token in Intake → Pull requests, or install the GitHub CLI

With neither, nothing is lost and nothing errors out: **Make PR** hands your
browser a prefilled compare URL (base…head, PR form already open) and **Merge**
opens the pull request page. You finish the click on github.com.

### `Git clone failed for story <id>: … [cloned over SSH … / cloned over HTTPS …]`

The ingestion pipeline finds work by `owner/repo` **slug**, so it has to build a
clone URL itself, and the bracketed hint tells you which transport it chose and
what to check:

- *cloned over SSH* → `ssh -T git@<host>` and `ssh-add -l`, as above. The clone
  runs headless with prompts disabled and stdin closed, so a credential helper
  that would have asked you something gets EOF instead of hanging the poll loop.
- *cloned over HTTPS* → your git credential helper or token for that host.

By default (`git_transport = "auto"`) it copies the spelling of your own
`[repository].url` whenever that names the same repo, so setting that to your
SSH URL is usually the whole fix. To force it either way, set
`[repository].git_transport = "ssh"` (or `"https"`) — see
[configuration.md](docs/configuration.md). If you already have an
`insteadOf` rule in `~/.gitconfig`, you need none of this: git rewrites the URL
before it dials out, because MindFlock passes URLs to git untouched.

### The stage chip is stuck on `pushed` after you opened the PR

Stage detection asks GitHub whether a PR exists for the branch, and that query
needs the same credential as above. With `gh` or a token, the chip advances to
**PR open** within a poll or two. With neither, MindFlock can see that your
branch is pushed (that is pure git) but cannot see the PR, so the chip stays on
`pushed` even though the PR is open — the branch and the PR are fine, only the
badge is blind. Add a token in Intake → Pull requests to light it up.

Unrelated but commonly confused: a push made **outside** MindFlock can take up
to ~45 s to move the badge, because the origin-branch SHA is a cached
`git ls-remote`.

---

## Creating sessions

### `a rebase is in progress in <repo> — finish or abort it first`
### `a merge is in progress in <repo> — finish or abort it first`
### `a cherry-pick is in progress in <repo> — finish or abort it first`
### `a bisect is in progress in <repo> — finish or abort it first`

MindFlock refuses to fork a session off a half-finished HEAD. Nothing was
changed in your repo. Finish or abort the operation (the error includes the
exact command, e.g. `git -C <repo> rebase --abort`) and create the session
again.

### `<path> is not a git repository — sessions fork a git worktree off the repo's HEAD`

Point the session at a git repo, or (in the web UI's + New dialog, under
"More options") tick **Create a git repo in this folder** — MindFlock runs
`git init` + an initial commit for you. A plain folder also works: the session
then runs in-place with git features off.

### `<repo> has no commits yet — a worktree needs a HEAD commit to fork from`

Make any first commit: `git -C <repo> commit --allow-empty -m "initial commit"`.
(The web UI's create flow usually does this automatically.)

### `this appears to be a brand new repository: please create an initial commit before creating an instance`

Same as above — the repo has no HEAD yet.

### `<repo> is on a detached HEAD — the session forks the current commit, not a branch tip`

A warning, not an error. The session still starts, based on the commit you're
on. If you meant a branch: `git -C <repo> switch <branch>` first.

### `<repo> is a shallow clone — worktrees work, but diff bases and merge-base-dependent flows may misbehave`

A warning. For full functionality: `git -C <repo> fetch --unshallow`.

### `instance <title> already exists`

Session titles are unique. Pick another name, or remove the old session
(`mindflock rm <title>`, or the web UI's ✕ — the worktree is kept and
recoverable from Recently closed).

### `session <title> failed to start — check the server logs`

The background start (worktree + provisioning + tmux) failed after creation
was accepted. The server also emits a `session.create_failed` event with the
reason — watch `mindflock events --follow` while retrying, or check the
`serve` terminal output.

### I pressed Ctrl-C while a session was being created

Safe: worktree creation rolls itself back on interruption (partial worktree
removed, the just-created branch deleted, `git worktree prune` run). Your
repo is untouched; just create the session again.

---

## Attach / terminal

### `attach needs a real terminal (running in a script? use 'mindflock ls --json')`

`mindflock attach` replaces your terminal with `tmux attach` — it can't work
inside a pipe or a non-TTY script. For scripting, read state from
`mindflock ls --json`.

### `tmux not found on PATH — run 'mindflock doctor' for install hints`

See the tmux entry under Doctor failures above.

### `no session named '<title>' (run 'mindflock ls')`

The title doesn't match any session (prefixes work when unambiguous).
`mindflock ls` shows what exists.

### `ambiguous title '<prefix>' — matches: …`

Your prefix matches several sessions; type more of the name.

---

## CLI ↔ server

### `no MindFlock server found (tried 127.0.0.1:8765)` / connection errors from `mindflock new`/`ls`/`rm`

The session commands are thin clients over a *running* server. Start one
(`mindflock serve` from your repo), or point the CLI at it with
`--host`/`--port` or `MINDFLOCK_HOST`/`MINDFLOCK_PORT`.

### `'mindflock events' needs the websockets package`

From a source checkout: `uv sync --group web`. (Installer installs include it.)

---

## Phone UI (`/m`)

### Phone shows the UI but every request fails with 401

The auth gate is on (it auto-enables when the server binds non-locally). Use
the tokened URL / QR printed in the startup banner — scanning the QR carries
the token. Force the gate: `MINDFLOCK_AUTH=1` (on) / `MINDFLOCK_AUTH=0` (off).

### Startup banner shows no phone URL / QR

`tailscale` isn't installed or isn't up — phone access needs the machine on a
tailnet (`curl -fsSL https://tailscale.com/install.sh | sh`, then
`sudo tailscale up`). The desktop app works without it.

---

## Desktop app (Electron)

### Window stuck on the offline page

The app connects to the server at `http://localhost:8765` and auto-starts the
*installed* `mindflock serve` when nothing answers (directly on Linux/macOS;
inside WSL on Windows). The offline page diagnoses itself and tells you which
case you're in:

- **"WSL isn't responding"** (Windows) — WSL is hung (a genuinely wedged VM).
  Click the **Restart WSL** button on the page (it runs `wsl --shutdown` and
  relaunches the server; anything else running in WSL is closed). If that
  doesn't help, restart the computer.
- **"Finish setting up WSL"** (Windows) — WSL is present but has no working
  distro (only partially set up — a reboot after `wsl --install` may still be
  pending). Run `wsl --install`, reboot, then run `wsl` once to create your
  Linux user; reopen MindFlock when it opens a shell. Restarting WSL can't fix
  this, which is why this is its own screen rather than "WSL isn't responding".
- **"WSL isn't installed"** (Windows) — run `wsl --install` in PowerShell,
  reboot, reopen MindFlock.
- **"One more step" / the engine isn't installed** — click **Install the
  engine**. The app runs the bundled `install.sh` for you (inside WSL on
  Windows, on this machine otherwise) and streams the output into the window;
  it opens by itself when the install finishes. If it fails, the page keeps
  the transcript, offers **Try again**, and shows the manual command as a last
  resort. On macOS the usual first-run failure is the missing Xcode Command
  Line Tools (they provide `git`) — the app opens Apple's installer for you;
  finish it and press **Install** again.
- **"Starting the MindFlock server…" that never connects** — the server is
  crashing on boot; check the logs below. On Windows also check the distro:
  the app launches into your **default** WSL distro; if MindFlock lives in
  another one, set `MINDFLOCK_WSL_DISTRO` (`wsl -l -v` lists them, and
  `wsl -s <name>` changes which is default).

Logs: the app log folder (press `Ctrl+Shift+L` in the app —
`%APPDATA%\MindFlock\logs\main.log` on Windows) and
`~/.mindflock/desktop-server.log` where the engine runs (each boot is
banner-stamped).

### `mindflock is not installed in this WSL distro (<distro>).`
### `mindflock is not installed on this machine.`

(Found in `~/.mindflock/desktop-server.log`.) The app probed the login PATH
and `~/.local/bin/mindflock` and found nothing. Run the install one-liner
printed on the next log line, then let the app reconnect (it retries
automatically).
