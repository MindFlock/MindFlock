# MindFlock — the desktop app (Electron)

**This is the MindFlock client** — the one supported way to use MindFlock on a
desktop, on every platform. A single frameless window renders the UI served by
the local FastAPI server, auto-starting that server when it isn't running:

- **Linux / macOS** — the server runs natively; the app spawns the installed
  `mindflock serve` directly (login PATH, falling back to
  `~/.local/bin/mindflock`).
- **Windows** — the engine lives in **WSL2** (it needs tmux and Unix PTYs —
  see "Why not fully native?" below); the app starts `mindflock serve` hidden
  inside the distro via `wsl.exe` and connects over `http://localhost:8765`
  (WSL2 forwards localhost to Windows).

(The phone UI at `/m` is the one non-desktop surface — served by the same
server, reached by scanning the startup QR over your tailnet.)

## Deployment (the intended experience)

1. **Once**: install the desktop app. Every tagged release attaches one
   unsigned build per OS (the README's download buttons point at them), or
   `npm run dist` in this folder produces the one for the OS you run it on
   (NSIS `.exe` on Windows, universal `.dmg` on macOS, AppImage on Linux;
   `dist:win` / `dist:mac` / `dist:linux` force a target).
2. **Once**: install the server/CLI where the engine runs.
   - **Windows** — nothing to do: the NSIS installer runs the step below for
     you inside your default WSL distro (see
     [Windows: the installer does both](#windows-the-installer-does-both)).
     Install WSL2 first if you don't have it:
     [learn.microsoft.com/windows/wsl/install](https://learn.microsoft.com/windows/wsl/install).
   - **Linux / macOS** — nothing to do either: a `.dmg` is a drag-copy with no
     post-install hook and an AppImage is never "installed" at all, so first
     launch offers an **Install the engine** button instead (see
     [First launch installs the engine](#first-launch-installs-the-engine)).
3. **Every time after**: open MindFlock. The app probes port 8765 and, when
   nothing answers, silently starts `mindflock serve`. No terminal windows, no
   manual steps.

## First launch installs the engine

When the probe reports the CLI is missing, the offline page offers one button.
`main.js` runs the **bundled** `install.sh` — electron-builder copies it in via
`extraResources`, so the app runs the script from its own build rather than
curling one at runtime: nothing to 404, nothing to drift out of sync, and the
engine is pinned to the app's own version tag (same rule as the NSIS hook).

Two details worth knowing before editing this path:

- **The output buffer lives in the main process**, and the page polls it. The
  retry loop can replace the offline page underneath a running install, so
  anything the user has to keep seeing cannot live in the renderer.
- **Windows has no pipe.** The engine installs inside WSL through the hidden
  `wscript` transport (spawning `wsl.exe` directly flashes a console window),
  which nothing can be piped back through — so the WSL side redirects the run
  to a log on the *Windows* filesystem via `wslpath` and the main process tails
  it, with a sentinel last line carrying the real exit code.

Point `MINDFLOCK_INSTALL_SCRIPT` at a stub to exercise the flow without
actually reinstalling anything.

## Windows: the installer does both

The app alone is a shell with nothing behind it, so
[`build/installer.nsh`](build/installer.nsh) — picked up automatically by
electron-builder as the NSIS `customInstall` hook — finishes setup by running
`install.sh` inside the default WSL distro, pinned to the same version tag as
the app. Watch it happen via **Show details** during install.

It is deliberately non-fatal: no WSL, a wedged distro, or no network leaves
the app installed and the offline page explains what's left. Set
`MINDFLOCK_NO_WSL=1` before launching the installer to skip it entirely, and
re-running the installer is a safe in-place upgrade of the CLI.

Two things that look like bugs but aren't: it targets your **default** distro
(`wsl -l -v`; `MINDFLOCK_WSL_DISTRO` only steers the *app*, not the
installer), and it reaches `wsl.exe` through `$WINDIR\Sysnative` because the
32-bit NSIS installer can't see the real `System32`.

Overrides:

- `MINDFLOCK_URL` — point at a different server
  (e.g. `MINDFLOCK_URL=http://localhost:9000`).
- `MINDFLOCK_WSL_DISTRO` — Windows only: pins which distro to launch in.
  Unset (the default), `wsl.exe` picks your default distro — the same one the
  installer put the CLI into. `wsl -l -v` lists them.
- `MINDFLOCK_REPO` — **developer mode**: path of a MindFlock *source checkout*
  (inside WSL on Windows); the app then runs that checkout's `.venv` server
  instead of the installed CLI.
- `MINDFLOCK_UPDATE_REPO` — `owner/name` of the GitHub repo whose Releases the
  app polls for update notifications (default `MindFlock/MindFlock`). The check
  is best-effort: offline or a non-200 (e.g. a private repo's 404) is silent.

## Run from source (developers)

In **Windows PowerShell** (with Node installed):

```powershell
# pushd maps the WSL UNC path to a temp drive so npm has a normal CWD
pushd \\wsl.localhost\<Distro>\home\<user>\path\to\MindFlock\electron
npm install      # first time only — downloads Windows Electron
npm start
popd
```

Substitute `<Distro>`, `<user>`, and `path\to\MindFlock`. If `npm install`
misbehaves over the UNC path, copy this `electron/` folder to a Windows-local
dir (e.g. `C:\mindflock-desktop`) and run it there instead.

The window opens frameless with our title bar; **drag** the bar to move,
**drag any edge** to resize (native), and the **□ / ❐** button toggles maximize.
If the server isn't up yet you'll see a "waiting…" page that auto-reconnects.

## Dev loop (no rebuilds)

The packaged app freezes only the four shell files (`main.js`, `preload.js`,
`logger.js`, `offline.html`). Everything else is loaded live from the server,
so day-to-day changes never need `npm run dist`:

| You changed… | To see it |
|---|---|
| Frontend (`static/app.js`, `index.html`, `style.css`, addons) | **Ctrl+Shift+R** in the app (reloads from the server; also escapes the offline page) |
| Python (server / engine / providers) | restart the server (`systemctl --user restart mindflock`, or Ctrl-C + `mindflock serve`) — the app auto-reconnects in ~2.5s |
| The shell files themselves | `npm start` (runs unpackaged from this folder) |
| Nothing — refresh the *installed* double-click app | `npm run dist` (the only rebuild case) |

Plain Ctrl+R is deliberately left alone — inside the terminal panes it's bash
reverse-i-search. Devtools are hard-disabled in packaged builds (users can't
open them); `npm start` dev runs keep them available programmatically.

## Dev build alongside the installed app (isolated)

You can run a **dev** copy of the shell next to the installed **prod** app, to
experiment without touching your real install. Turn on dev mode with either:

- the env var `MINDFLOCK_DEV=1`, or
- the CLI flag `--mindflock-dev` (convenient for a desktop shortcut — see below).

Dev mode only changes cosmetics and *where files live* — the server (and
therefore your sessions) stays shared, which is usually what you want:

- **Isolated profile** — its own config, logs, window-state and `localStorage`
  under a separate `MindFlock (dev)` userData dir:
  - Windows: `%APPDATA%\MindFlock (dev)`
  - macOS: `~/Library/Application Support/MindFlock (dev)`
  - Linux: `~/.config/MindFlock (dev)`
- **Red "dev" icon** — the window + taskbar (Windows/Linux) and dock (macOS)
  use `dev-icon.*` (the normal logo with a bright-red **dev**). Override with
  `MINDFLOCK_DEV_ICON=/path/to/icon` (`.ico` on Windows; `.png` elsewhere).
- **`MindFlock-DEV` wordmark** in the title bar.
- A distinct taskbar/dock identity so it never merges with the prod app.

Prod is untouched: with neither the env var nor the flag set, every one of
these is a no-op, so it is safe to ship in the packaged build.

### Run it

**macOS / Linux** — from this `electron/` folder:

```bash
MINDFLOCK_DEV=1 npm start
```

**Windows** (PowerShell) — the engine lives in WSL, so run Windows Electron
against the WSL checkout:

```powershell
pushd \\wsl.localhost\<Distro>\home\<user>\path\to\MindFlock\electron
npm install                       # first time — downloads Windows Electron
$env:MINDFLOCK_DEV = "1"; npm start
popd
```

Want dev **fully** isolated, sessions included? Give it its own server on
another port instead of sharing prod's:

```bash
mindflock serve --port 9000                                   # separate backend + state.json
MINDFLOCK_DEV=1 MINDFLOCK_URL=http://localhost:9000 npm start
```

### Pinning the dev icon to the Windows taskbar

The taskbar draws its icon from **two different places**, which is why the
normal (installed) app pins with its icon but an ad-hoc dev launch may not:

- While the app is **running**, the taskbar button uses the *window* icon — dev
  mode already sets this to the red badge.
- A **pinned** icon comes from the shortcut/executable you pinned, *not* from
  the running window. The prod app pins cleanly because its installer
  registered a Start-menu shortcut carrying the app's AppUserModelID and icon.

So to pin dev with the red icon, pin a shortcut that (a) targets `electron.exe`
**directly** — a `.bat` makes Windows pin `cmd.exe` with cmd's icon instead —
(b) passes the app dir plus `--mindflock-dev`, and (c) sets its `IconLocation`
to a `.ico` of the dev badge. For example:

```powershell
$ws  = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut("$([Environment]::GetFolderPath('Desktop'))\MindFlock (dev).lnk")
$lnk.TargetPath       = "C:\path\to\node_modules\electron\dist\electron.exe"
$lnk.Arguments        = '"\\wsl.localhost\<Distro>\home\<user>\path\to\MindFlock\electron" --mindflock-dev'
$lnk.IconLocation     = "C:\path\to\dev-icon.ico,0"
$lnk.WorkingDirectory = "C:\path\to"
$lnk.Save()
```

Then right-click that shortcut (or the running window) → **Pin to taskbar**.

## Package the double-click installer

```powershell
npm run dist        # electron-builder -> dist\  (NSIS on Windows, dmg on macOS, AppImage on Linux)
```

## Why not fully native (no WSL)?

The session engine is built on tmux (detached, persistent agent sessions;
pane capture; keystroke injection), Unix PTYs (`ptyprocess`), `fcntl` locks,
and bash launcher scripts written into each worktree. None of those exist on
native Windows; a port would mean rebuilding the session layer on ConPTY plus
a tmux replacement — a rewrite, not a packaging change. WSL2 provides all of
it with near-native performance, so the supported Windows shape is:
Windows UI (this shell) + WSL2 engine.

## Logs

The installed app is self-contained and runs the WSL server hidden, so there's
no console to watch when something breaks. Two logs capture everything:

- **App (Electron main process)** — `%APPDATA%\MindFlock\logs\main.log`
  (rotates to `main.log.1` past 2 MB). Tees `console.*`, renderer errors, failed
  loads, and renderer/main crashes. **Press `Ctrl+Shift+L` in the app** to open
  this folder.
- **WSL server startup** — `~/.mindflock/desktop-server.log` inside WSL
  (appended, capped at 2 MB, each boot banner-stamped). Captures the server's
  stdout+stderr — including a crash *before* Python's own `/tmp/mindflock.log`
  logger initialises (bad venv, import error, port bind). Override the path with
  `MINDFLOCK_WSL_LOG`.

## Files

- `main.js` — single frameless `BrowserWindow` loading the UI + injected chrome
  (scrollbar/title-strip CSS, `#mf-winctl` buttons), window-control IPC,
  auto-start of the hidden WSL server, offline/retry, log wiring.
- `logger.js` — file logging for the main process (rotation + crash/renderer
  capture); `init(app)` / `attachWindow(win)` / `paths()`.
- `preload.js` — `contextBridge` exposing `window.winctl` to the injected bar
  and `window.mfdiag` to the offline page.
- `offline.html` — shown until the server answers. Polls `diag:get` (a hidden
  `wsl.exe` probe on Windows) to say *why* nothing is answering: server
  booting, MindFlock not installed, or WSL down — with a one-click
  **Restart WSL** button for the last case.
