# MindFlock on native Windows

Goal: `mindflock serve`, the desktop app, the phone page and the CLI run on a
default Windows 10/11 machine — no WSL, no tmux, no Cygwin — with the same
features as on Linux and macOS. WSL keeps working as before.

## What a default Windows machine has

Python (or `uv`, which fetches 3.12), **Git for Windows** (required — it is
also what Claude Code on Windows needs, and it brings `bash.exe`), the agent
CLIs as native `.exe`/`.cmd` (claude, codex, …), PowerShell 5.1, ConPTY
(Windows 10 1809+). `mindflock doctor --fix` installs what is missing with
`winget` (Git.Git, astral-sh.uv, the CLIs' own installers).

## The design, in one line per piece

| Piece | Linux / macOS | Native Windows |
|---|---|---|
| Sessions (persistent named terminals) | tmux | the **session host** `backend/session/winmux/`: a per-user daemon holding each session's ConPTY (pywinpty) and a screen model (pyte), answering the tmux subcommands MindFlock uses with tmux's output and exit codes |
| Every tmux call | `backend.session.mux.run(["tmux", …])` → `subprocess.run` | same call → the host |
| A terminal in the browser / CLI attach | `mux.attach()` → `tmux attach` in a PTY | `mux.attach()` → a host client: `fd` is `None`, read from a thread |
| Launch scripts, exit marker, `.env` seeding | bash | the same bash, run by Git for Windows' `bash.exe` (paths as `C:/…`) |
| Session identity in hooks / MCP (`$TMUX`, `$TMUX_PANE`) | tmux sets them | the host sets `TMUX=winmux,<host pid>,0` and `TMUX_PANE=%<n>` in each session, so `display-message -t $TMUX_PANE '#{session_name}'` answers |
| File locks | `fcntl.flock` | `backend.compat.flock` (LockFileEx, shared + exclusive) |
| Process groups, killing a tree | `start_new_session`, `os.killpg` | `compat.detached_kwargs()`, `compat.kill_group()` (taskkill /T) |
| Process tables, memory, `/proc` | `/proc`, `ps` | `psutil` (both, where it replaces `ps`; `/proc` reads stay on Linux) |
| Agent memory scopes | systemd-run user scope | a Job Object with a memory limit (later phase) |
| Event loop | selector (`add_reader`) | Proactor: no `add_reader` — PTY pumps read in a thread |
| Desktop app | Electron → server in WSL | Electron → server on Windows itself (`mindflock serve`), WSL as a fallback |

## The session host (`backend/session/winmux/`)

* `host.py` — the daemon: started on demand by the first client (detached,
  no console window), one per user, survives the MindFlock server's
  restarts like a tmux server. Listens on `127.0.0.1:<random>`; its port,
  pid and a random token go to `~/.mindflock/run/winmux.json` (0600-ish:
  the user's profile). Exits when its last session ends.
* `client.py` — `run(argv, **subprocess_kwargs)`, `popen`, `attach`,
  `console_attach`: the `mux` functions. One request per call, JSON lines;
  a `;`-chained argv (tmux's command sequences) runs in order in the host.
* `screen.py` — the pyte screen + history behind `capture-pane` (`-p`,
  `-e` SGR, `-J` joined wraps, `-S`/`-E` ranges incl. `-`, `-N`),
  `#{history_size}`, cursor and size formats, copy-mode/scroll position.
* `keys.py` — `send-keys` names (Enter, Escape, C-c, C-Space, BSpace, Up,
  Down, Left, Right, Tab, BTab, PageUp/Down, Home, End, F1-F12, M-x, `-l`
  literal, `-H` hex) → the bytes a terminal sends.
* Attach: a new client gets a repaint of the screen (as tmux redraws on
  attach), then the live output; input from writable clients goes to the
  ConPTY; the session's size follows the latest client resize.

## Phases

1. Contracts: `backend/session/mux.py`, `backend/compat.py`, this page.
2. In parallel: the session host; tmux call sites → `mux` and PTY pumps →
   threads where `fd` is None (web/core + server); the rest of the backend
   (locks, process groups, `/proc` → psutil, import-time POSIX modules).
3. Launchers + hooks under Git Bash; install (doctor/winget, `install.ps1`,
   Electron native mode, NSIS without the WSL requirement) and a Windows CI
   job. **Install side done** (see "Installing" below); launchers + hooks are
   their own piece.
4. End to end on a real Windows box: serve, New session (claude), type,
   capture, attach from the browser, restart the server, sessions survive.

## Installing (phase 3, done)

* `install.ps1` (repo root) — `irm …/install.ps1 | iex`, PowerShell 5.1: ConPTY
  check (build 17763+), Git for Windows via `winget` when missing, uv (Astral's
  pinned, sha256-verified `install.ps1`), `uv tool install mindflock[web]`
  (newest release → a pinned SHA, or `-Source <folder>`), voice packages
  carried, `install.json`, a running server restarted, `-Join`, and
  `mindflock init --steps`. `install.sh` under Git Bash/MSYS points here.
* `mindflock doctor` on Windows: `git` = Git for Windows (`git.exe` AND its
  `bash.exe`, required), `session-host` (ConPTY + pywinpty/pyte) instead of
  `tmux`, `python`, the agents' own Windows installers (claude:
  `irm https://claude.ai/install.ps1 | iex`; npm CLIs as is); `--fix` is a
  PowerShell plan, winget first, handed out as one `-EncodedCommand` line.
* Desktop app: `electron/engine.js` picks native or WSL
  (`MINDFLOCK_DESKTOP_ENGINE`, `engine.json`, an existing WSL engine stays);
  native mode spawns `mindflock serve` hidden, installs with the bundled
  `install.ps1`, and kills the server it started on quit (the session host,
  and so the sessions, live on). NSIS no longer requires WSL.
* CI: `test (windows-latest)` (docs/development.md, "How CI runs the suite").

Not in the first cut: the peer links' sandboxed shared folder (bwrap /
seatbelt have no Windows equivalent yet — Windows refuses it with a
sentence) and the WSL-only tailnet MTU clamp.
