# MindFlock — the desktop app (Electron)

**This is the MindFlock client** — the one supported way to use MindFlock on a
desktop, on every platform. A single window renders the UI served by the local
FastAPI server, auto-starting that server when it isn't running. The chrome is
platform-conditional: **frameless** (`frame: false`) on Windows/Linux with our
own injected – □ ✕, and on **macOS** `titleBarStyle: 'hidden'` with
`trafficLightPosition: { x: 13, y: 13 }`, keeping the OS traffic lights
top-left (see [Window chrome](#window-chrome-and-the-preload-bridge)).

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
   build per OS (the README's download buttons point at them), or
   `npm run dist` in this folder produces the one for the OS you run it on
   (NSIS `.exe` on Windows, universal `.dmg` on macOS, AppImage on Linux;
   `dist:win` / `dist:mac` / `dist:linux` force a target). None are notarized,
   so first launch shows an "unverified developer" warning (the top-level
   README walks through clearing it). The macOS build is self-signed only —
   enough for macOS to remember folder-access grants and for Squirrel.Mac to
   accept updates, not enough for Gatekeeper; see
   [Versioning & releases](../docs/development.md#versioning--releases) for
   the cert setup. After that first approval, updates are one click from
   inside the app (since 0.7.2 — see [Updating](#updating)); you never go
   back to the download page.
2. **Once**: nothing to do. First launch offers **Set up MindFlock on this
   computer**, which installs the engine (inside your default WSL distro on
   Windows — install WSL2 first:
   [learn.microsoft.com/windows/wsl/install](https://learn.microsoft.com/windows/wsl/install))
   and then carries on into tmux and your agent CLI (see
   [First launch installs the engine](#first-launch-installs-the-engine)).
   The Windows `.exe` installs only the app; it does not install the engine
   (see [the installer checks WSL](#windows-the-installer-checks-wsl)).
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

- **On a Mac without Apple's developer tools** (git), the button opens Apple's
  installer and the install *waits* for it (polling `xcode-select -p`, up to
  30 minutes), then continues by itself — the page says so meanwhile.
- **It doesn't stop at the engine.** `install.sh` runs the read-only doctor
  here (no terminal behind a GUI), so tmux and the agent CLI are still missing
  when it finishes. After a first-run install the app opens with
  `?setup=install`: the Setup dialog comes up on Dependencies with one
  **Install …** button (one password prompt, in a real terminal), and then
  **Sign in to <agent>**. Updates never take this path.

Point `MINDFLOCK_INSTALL_SCRIPT` at a stub to exercise the flow without
actually reinstalling anything.

## Windows: the installer checks WSL

The app alone is a shell with nothing behind it, and its engine lives in WSL.
[`build/installer.nsh`](build/installer.nsh) — picked up automatically by
electron-builder as the NSIS `customInstall` hook — only checks that WSL is
present and hands off to the app, whose first launch installs the engine with
a live transcript (a multi-minute network install inside the setup wizard gave
no feedback and couldn't be cancelled). It reaches `wsl.exe` through
`$WINDIR\Sysnative` because the 32-bit NSIS installer can't see the real
`System32`.

Overrides:

- `MINDFLOCK_URL` — point at a different server
  (e.g. `MINDFLOCK_URL=http://localhost:9000`).
- `MINDFLOCK_WSL_DISTRO` — Windows only: pins which distro the **app**
  launches and probes in. Unset (the default), `wsl.exe` picks your default
  distro. It steers only the app: the first-launch engine install goes into
  whichever distro the app is looking in, so set it before that first launch
  if you don't want the default distro. `wsl -l -v` lists them.
- `MINDFLOCK_REPO` — **developer mode**: path of a MindFlock *source checkout*
  (inside WSL on Windows); the app then runs that checkout's `.venv` server
  instead of the installed CLI.
- `MINDFLOCK_UPDATE_REPO` — `owner/name` of the GitHub repo whose Releases the
  app polls for update notifications (default `MindFlock/MindFlock`). The check
  is best-effort: offline or a non-200 (e.g. a private repo's 404) is silent.
- `MINDFLOCK_UPDATE_FEED` — point the in-app updater at a plain directory URL
  holding a `latest*.yml` and the build it names (staging, or an end-to-end
  test) instead of the GitHub release. `MINDFLOCK_UPDATE_AUTOINSTALL=1` installs
  as soon as the download finishes (unattended tests only).
  `MINDFLOCK_DISABLE_AUTOUPDATE=1` turns the in-app updater off.

## Updating

One path, one click. The app toast's **Update**, the engine toast's **Update
engine** and the Settings → Advanced button all run the same
`updateEverything({engine, app})` in `main.js`, and all poll the same run
state, so none of them can drift from the others (Settings → Advanced used to
have its own copy that stopped polling on "done" and never restarted the
server, so the button stuck on "Updating…").

The run, in order:

1. **Engine.** If the engine is behind the target release and is a release
   install (`/api/update/check` says `uv-tool` or `pipx`; install.sh reinstalls
   it the way install.json records), the pinned `install.sh` updates it. A
   developer checkout (`MINDFLOCK_REPO`, or an `editable` engine) is left
   alone, and an engine that is already at or ahead of the target is never
   downgraded.
2. **App.** When a release is out, electron-updater has already downloaded it
   in the background (from the GitHub release's `latest*.yml` + blockmaps): the
   run stops the old server, waits until the port is actually released, then
   installs the app silently and relaunches it; the relaunch starts the new
   engine.
3. **Engine only** (the app is current): the run stops the old server, waits
   for the port to be released (`stopEngineServer`, the single-owner rule in
   the launcher means it never spawns onto a port that still answers), starts
   the new one, and reports "Engine vX is running" once the server answers on
   it — only then is the run done.

Version skew the other way — the engine newer than the app (install.sh re-run
by hand, `mindflock update` on a machine whose app is behind) — is never
silent and never a downgrade: when an app release is out, the app-update toast
says "Your engine is newer than the app — update the app"; when none is, a
one-line notice says so and the engine stays as it is. An engine that is older
than the app keeps today's engine-update toast.

- **Windows / Linux** (NSIS / AppImage) update in place.
- **macOS** updates through Squirrel.Mac, which accepts an update only when it
  is signed by the same identity as the running app. Every release is signed
  with the same self-signed MindFlock certificate (`MAC_CSC_*` secrets; a
  tagged build without them fails), so you approve "unverified developer" once
  when you first install, and updates after that need nothing. The `.zip` on
  the release is what it swaps in; the `.dmg` stays the download.
- Wherever the app can't swap itself (a dev run, a copy run from the mounted
  dmg, a failed signature check) the button still updates the engine and then
  opens the download page.

The pure helpers (`cmpVersion`, the skew verdict, the shared run state) live in
`electron/update.js` and are pinned by `node --test` in `electron/test/`.
Outside the app, `mindflock update` is the same path for the engine alone (see
[docs/cli.md](../docs/cli.md)).

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

The window opens with our own title bar in place of the OS one; **drag** the bar
to move, **drag any edge** to resize (native), and the **□ / ❐** button toggles
maximize. On **macOS** there is no injected – □ ✕ at all: minimize / zoom / close
are the native red-yellow-green buttons top-left, so the glyph swap is a
Windows/Linux detail; drag and edge-resize behave the same. If the server isn't
up yet you'll see a "waiting…" page that auto-reconnects.

## Window chrome and the preload bridge

`main.js` branches on `process.platform` when it creates the window:

| | Windows / Linux | macOS |
|---|---|---|
| Frame | `frame: false` | `titleBarStyle: 'hidden'`, `trafficLightPosition: {x:13,y:13}` |
| Controls | injected `#mf-winctl` – □ ✕ (top-right) | the OS traffic lights (top-left) |
| `TITLEBAR_JS` | injects the buttons | returns early — two sets of controls otherwise |

The UI has to know, because it draws its own top bar in that same strip: on
macOS it reserves ~78px top-left and mirrors its logo/theme/bell cluster to the
right (`docs/web-ui.md` → Layout → Top bar). It learns this from the bridge, not
from the platform — `preload.js` exposes:

| Member | Meaning | Undefined in a browser (or an older shell) |
|---|---|---|
| `mfshell.platform` | `process.platform` | UI hides platform-specific controls |
| `mfshell.nativeTitleBar` | **capability**: the OS draws this window's controls, top-left | falsy → the UI keeps the injected-controls layout |
| `mfshell.dev` | dev build (red `-DEV` wordmark) | no badge |
| `mfshell.showItem(p)` | reveal a path in Finder/Explorer → `{ ok }` | UI falls back to copying the path |
| `winctl.minimize/maximize/close` | window commands for the injected bar | no-op; the browser has its own chrome |
| `winctl.onMaximizedChanged(cb)` | `maximized-changed` pushes; returns nothing | never fires |
| `winctl.onFullScreenChanged(cb)` | `fullscreen-changed` on enter/leave-full-screen; **returns an unsubscribe** | returns a no-op unsubscribe |
| `winctl.isFullScreen()` | current state via `win:is-fullscreen` — the event above only carries transitions, so a window already fullscreen at load needs this | resolves false |
| `mfdiag.*` | offline-page diagnostics | offline page isn't reachable |

`nativeTitleBar` is deliberately a *capability* flag and not `platform ===
'darwin'`: the shell and the engine-served frontend ship on independent
cadences, so a Mac shell built before this change still injects its own controls
top-right, and a platform-keyed layout would stack the frontend's cluster on top
of them. The flag is absent there, so that build keeps the layout it was built
for. New bridge members must stay optional the same way — see
[shell ↔ engine skew](../docs/development.md#shell--engine-skew).

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
- **Notifications headed `MindFlock-dev`** (Windows). A toast is headed by the
  display name of the Start-menu shortcut registered for the AppUserModelID that
  raised it — prod gets one from its installer, which is why its notifications
  read *MindFlock*. Dev has its own AUMID and no installer, so Windows used to
  print the raw id (*ai.mindflock.desktop.dev*) across the top of every toast.
  The shell now writes that shortcut itself on a dev run, into
  `%APPDATA%\Microsoft\Windows\Start Menu\Programs\MindFlock-dev.lnk`,
  pointing at `electron.exe` with the app dir and `--mindflock-dev` — so it is
  also a working launcher, and the thing to pin (see below). It is rewritten
  only when missing or stale, and a Start menu locked down by policy costs you
  the label, not the app.

  Its icon is **staged onto the local disk** first (into the dev profile dir).
  A shortcut's `IconLocation` is read by the Windows shell, not by us, and the
  shell will not extract an icon from the WSL share the checkout lives on — so
  pointing it at `dev-icon.ico` in place left the toast headed correctly and
  badged with the blank white document tile. The window and taskbar icons are
  unaffected either way: Electron loads those itself, with an ordinary file
  read. A checkout on a local drive keeps using its own file.

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

A dev run already writes one such shortcut — `MindFlock-dev.lnk` in your Start
menu, for the notification name above — so the quickest path is to right-click
that and **Pin to taskbar**. Roll your own only if you want it elsewhere or with
different arguments; it has to be a shortcut that (a) targets `electron.exe`
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

- `main.js` — the single `BrowserWindow` loading the UI + injected chrome
  (scrollbar/title-strip CSS, `#mf-winctl` buttons), window-control IPC,
  auto-start of the hidden WSL server, offline/retry, log wiring. Window chrome
  is platform-branched (`frame: false` vs. darwin's `titleBarStyle: 'hidden'` +
  `trafficLightPosition`), `TITLEBAR_JS` returns early on darwin so the OS keeps
  the only set of controls, `enter-full-screen` / `leave-full-screen` push a
  `fullscreen-changed` event to the renderer (guarded like `sendMax`), and
  `win:is-fullscreen` answers the state query the top bar makes on mount.
- `logger.js` — file logging for the main process (rotation + crash/renderer
  capture); `init(app)` / `attachWindow(win)` / `paths()`.
- `preload.js` — `contextBridge` exposing `window.mfshell` (`dev`, `platform`,
  `nativeTitleBar`, `showItem`, …) to the UI, `window.winctl` to the injected bar
  (plus `onFullScreenChanged`, which unlike `onMaximizedChanged` returns an
  unsubscribe), and `window.mfdiag` to the offline page. See
  [the bridge contract](#window-chrome-and-the-preload-bridge).
- `offline.html` — shown until the server answers. Polls `diag:get` (a hidden
  `wsl.exe` probe on Windows) to say *why* nothing is answering: server
  booting, MindFlock not installed, or WSL down — with a one-click
  **Restart WSL** button for the last case.

## First launch: never silent, finishes the job

Helpers live in `firstlaunch.js` (pure, OS injected) with tests in `test/` —
`npm test` or `node --test electron/test/*.test.js`.

- **Starting… shows the server log after 10 s** (last 15 lines of
  `~/.mindflock/desktop-server.log`). On Linux/macOS the bootstrap runs the
  server in the *foreground* so the exec'd server is the process `main.js`
  spawned; if it exits before the port answers, the page switches to
  **failed-to-start** with the log tail, **Try again** and **Copy diagnostics**.
  Inside WSL the bootstrap keeps its background-and-keepalive shape, so
  Windows never reaches failed-to-start and the tail is unavailable there.
- **macOS preflight**, in order, before `install.sh`: no Homebrew →
  **needs-homebrew**: **Install Homebrew** opens the official installer in
  Terminal (it asks for the password there), then polls for `brew` every 5 s;
  **Skip** continues without it. Then no Command Line Tools →
  `xcode-select --install` and the same 5 s poll until it lands. Both wait up
  to 30 minutes before failing with "Try again".
- **Gatekeeper on the page.** The app is self-signed and stays so; the
  not-installed screen on macOS carries the two-line *System Settings →
  Privacy & Security → Open Anyway* note.
- **Hand-off.** After a first-run install succeeds and the server answers, the
  app opens at `/?setup=1` (the web UI's setup checklist) — once. The first
  load of the app page writes `first-launch.json` into userData, and every
  later launch opens `/`. (Replaces `?setup=install`.)
- **Windows preflight** (`build/installer.nsh`, `customInit`): before any file
  is copied, no `wsl.exe` or a `wsl -l -q` that exits nonzero / lists nothing
  aborts with one message box offering the setup guide, and quits. Skipped for
  silent runs and the updater's `--updated` reinstall.
- **Bridge:** `mfdiag.action(name)` → `diag:action` with `retry-start`,
  `copy-diagnostics`, `brew-install`, `brew-skip`; `diag:get` also returns
  `start` (`startedAt`, `answered`, `exited`, `view`, `showTail`, `tail`,
  `logPath`, `canRetry`).
