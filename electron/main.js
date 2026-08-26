'use strict'
// MindFlock desktop shell (Electron) — THE MindFlock client, all platforms.
//
// A single frameless BrowserWindow loads the MindFlock UI from the local
// server over localhost, auto-starting that server when nothing answers:
//   * Windows — the engine lives in WSL2 (tmux/PTYs don't exist natively);
//     the server is started hidden inside the distro via wsl.exe.
//   * Linux / macOS — the server runs natively; it's spawned directly.
// We inject a slim title strip + thin scrollbars. Electron gives frameless
// windows native drag (-webkit-app-region) and native edge-resize, so none of
// that is hand-rolled (which is what fought us under WSLg/Wayland).

const { app, BrowserWindow, ipcMain, shell, Menu, dialog } = require('electron')
const path = require('path')
const net = require('net')
const http = require('http')
const https = require('https')
const fs = require('fs')
const { spawn, spawnSync } = require('child_process')
const logger = require('./logger')

// Windows attributes toast notifications by AppUserModelID. Without setting it,
// toasts are labeled "electron.app.MindFlock" with no icon. The packaged app
// must use the installer's appId (the NSIS Start-menu shortcut carries that
// AUMID, which is where Windows gets the display name + icon); dev runs fall
// back to the exe path so toasts still appear at all.
if (process.platform === 'win32') {
  app.setAppUserModelId(app.isPackaged ? 'ai.mindflock.desktop' : process.execPath)
}

// Dev sandbox — the dev launcher (run-dev.bat) sets MINDFLOCK_DEV=1 so this
// shell runs FULLY ISOLATED from the installed prod app: its own userData
// (config, logs, window state, per-origin localStorage under a separate
// "MindFlock (dev)" dir) and a distinct taskbar identity + icon. The server —
// and therefore sessions — stays shared (same MINDFLOCK_URL). Entirely inert
// when the env var is unset, so packaged prod builds are unaffected.
const DEV = process.env.MINDFLOCK_DEV === '1' || process.argv.includes('--mindflock-dev')
const DEV_ICON = DEV
  ? (process.env.MINDFLOCK_DEV_ICON ||
     path.join(__dirname, process.platform === 'win32' ? 'dev-icon.ico' : 'dev-icon.png'))
  : null
// The dev shell's own taskbar/toast identity. Kept STABLE: it is what a pinned
// taskbar button and any already-registered shortcut are keyed on, so renaming
// it silently orphans both.
const DEV_AUMID = 'ai.mindflock.desktop.dev'
// What Windows should PRINT above a dev notification: prod's product name with
// the suffix this build already wears everywhere else. See ensureDevToastName().
const DEV_TOAST_NAME = 'MindFlock-dev'
if (DEV) {
  app.setAppUserModelId(DEV_AUMID) // own taskbar group + icon
  try {
    app.setPath('userData', path.join(app.getPath('appData'), 'MindFlock (dev)'))
  } catch (e) { /* fall back to the shared dir if this platform disallows it */ }
}

// Give the dev AUMID a NAME, or Windows prints the id itself.
//
// A toast is headed by the display name of the Start-menu shortcut registered
// for the AUMID that raised it — that is the whole reason the packaged app sets
// the installer's appId (the NSIS shortcut carries it). Dev sets an AUMID of its
// own for a separate taskbar group, but nothing had ever written a shortcut for
// it, so Windows fell back to printing the raw string and every dev notification
// was headed "ai.mindflock.desktop.dev" instead of a name.
//
// So write that shortcut ourselves, once, into the per-user Start menu. It
// targets electron.exe DIRECTLY with the app dir + the dev flag (the same shape
// the README's pin-to-taskbar recipe uses — a .bat would make Windows attribute
// the icon to cmd.exe), which also makes it a working launcher rather than a
// decoy that exists only to be read.
//
// Windows-only, and only in dev: prod already has the installer's shortcut, and
// no other platform resolves notification identity this way.
function ensureDevToastName () {
  if (!DEV || process.platform !== 'win32') return
  try {
    const programs = path.join(
      app.getPath('appData'), 'Microsoft', 'Windows', 'Start Menu', 'Programs'
    )
    const link = path.join(programs, DEV_TOAST_NAME + '.lnk')
    // A shortcut's icon must be an .ico; MINDFLOCK_DEV_ICON may legitimately be
    // a .png (that override is shared with the window/dock icon, which takes
    // either), so fall back to the exe rather than writing a broken IconLocation.
    const icon = DEV_ICON && DEV_ICON.toLowerCase().endsWith('.ico') ? DEV_ICON : process.execPath
    const want = {
      target: process.execPath,
      args: '"' + app.getAppPath() + '" --mindflock-dev',
      cwd: path.dirname(process.execPath),
      icon,
      iconIndex: 0,
      appUserModelId: DEV_AUMID,
      description: 'MindFlock dev shell (isolated profile, shared server)'
    }
    // Rewrite only when it is missing or has drifted — an electron.exe that
    // moved, an icon override that changed. Touching the .lnk on every launch
    // would churn the Start menu's index for nothing.
    let have = null
    try { have = shell.readShortcutLink(link) } catch (e) { /* not there yet */ }
    if (have &&
        have.target === want.target &&
        have.args === want.args &&
        have.icon === want.icon &&
        have.appUserModelId === want.appUserModelId) return
    fs.mkdirSync(programs, { recursive: true })
    shell.writeShortcutLink(link, 'create', want)
    console.log('[mindflock] dev toast identity registered:', link)
  } catch (e) {
    // Cosmetic, and the Start menu can be locked down by policy: a dev run must
    // still start (with the old raw-id heading) rather than fail over a label.
    console.warn('[mindflock] could not register the dev toast name:', e && e.message)
  }
}

// Log the auto-started server appends to (stdout+stderr), so a startup crash
// BEFORE the Python log module initialises is still captured. Lives in the
// server-side home (inside WSL on Windows; the user's home on Linux/macOS);
// surfaced in the offline page / README for support.
const WSL_SERVER_LOG = process.env.MINDFLOCK_WSL_LOG || '~/.mindflock/desktop-server.log'

const APP_URL = process.env.MINDFLOCK_URL || 'http://localhost:8765'
// The only origin the window may navigate to (the local server). Everything
// else opens in the system browser — see the will-navigate guard below.
const APP_ORIGIN = (() => { try { return new URL(APP_URL).origin } catch (e) { return null } })()
const RETRY_MS = 2500
// After we kick off a server spawn, suppress further spawns for this long so the
// 2.5s retry loop (which now re-ensures the server on every failed load) does not
// launch a second python while the first is still booting -- they would fight
// over port 8765. Comfortably longer than a cold boot (venv + imports).
const SPAWN_COOLDOWN_MS = 15000
let lastSpawnAt = 0

// Auto-start the MindFlock server in WSL if it isn't already up, so the
// installed app is self-contained (no separate launcher). Overridable by env.
const PORT = (() => { try { return Number(new URL(APP_URL).port) || 8765 } catch (e) { return 8765 } })()
// Empty means "whatever `wsl.exe` picks", i.e. the user's DEFAULT distro --
// which is the one the Windows installer (build/installer.nsh) puts the CLI
// into, and matches the backend's own default (settings.platform.wsl_distro).
// Hardcoding `Ubuntu` here would send the app looking in a distro the
// installer never touched. Set MINDFLOCK_WSL_DISTRO to pin one (`wsl -l -v`).
const WSL_DISTRO = process.env.MINDFLOCK_WSL_DISTRO || ''
// The `-d <distro>` fragment (with its trailing space) spliced into the
// wsl.exe command lines below, or nothing at all when unpinned.
const WSL_D = WSL_DISTRO ? '-d ' + WSL_DISTRO + ' ' : ''
// What the offline page calls it. It substitutes this into "runs inside WSL
// (...)", so an unpinned distro needs a phrase, not an empty string.
const WSL_DISTRO_LABEL = WSL_DISTRO || 'your default distro'
// Optional path to a MindFlock SOURCE CHECKOUT inside WSL (developer mode:
// runs `.venv/bin/python backend/web/run.py` from that checkout). When
// unset, the default is the INSTALLED CLI: the bootstrap script looks for
// `mindflock` on the login PATH (falling back to ~/.local/bin/mindflock, where
// the curl installer puts it) and runs `mindflock serve`. If neither exists,
// the script logs the install one-liner and the offline page guides the user.
const WSL_REPO = process.env.MINDFLOCK_REPO || ''

// Single-quote a string for POSIX sh so paths with spaces/quotes/$ survive
// being spliced into the bootstrap script: close the quote, emit an escaped
// literal quote, reopen ('\'' technique).
function shq(s) { return "'" + String(s).replace(/'/g, "'\\''") + "'" }

// The server-log path as a shell expression. The default is '~/…' and relies
// on tilde expansion, which single-quoting would kill — expand it as "$HOME"
// ourselves and quote only the tail. Anything else is quoted verbatim.
function shLogExpr(p) {
  if (p === '~') return '"$HOME"'
  if (p.startsWith('~/')) return '"$HOME"/' + shq(p.slice(2))
  return shq(p)
}

function startServerIfNeeded() {
  // Probe the port first; only spawn when nothing is answering (so we never
  // kill a server the user already started).
  const sock = net.connect({ host: '127.0.0.1', port: PORT })
  let settled = false
  const finish = (up) => {
    if (settled) return
    settled = true
    sock.destroy()
    if (up) return
    // Nothing is listening. Don't spawn if we already kicked one off recently --
    // it is probably still booting, and a second python would race for the port.
    if (Date.now() - lastSpawnAt < SPAWN_COOLDOWN_MS) return
    lastSpawnAt = Date.now()
    // Redirect the server's stdout+stderr to a WSL-side log (appended, with a
    // boot banner and a 2 MB cap) so a crash before the Python log module comes
    // up -- bad venv, import error, port bind -- is still diagnosable.
    const log = WSL_SERVER_LOG
    // What to launch (identical script on every platform; only the transport
    // into a shell differs):
    //  * developer mode (MINDFLOCK_REPO set): the source checkout's venv+run.py
    //  * default (installed mode): the `mindflock` CLI from the login PATH,
    //    falling back to ~/.local/bin/mindflock (where install.sh puts it).
    //    If it isn't installed, log the install one-liner instead of failing
    //    silently -- the offline page points the user at this log.
    const where = process.platform !== 'win32'
      ? 'on this machine'
      : WSL_DISTRO
        ? 'in this WSL distro (' + WSL_DISTRO + ')'
        : 'in your default WSL distro'
    // Detach the server into its OWN session via `setsid`, so it outlives THIS
    // bootstrap's wsl.exe session. Without it, a bootstrap that LOSES the flock
    // keepalive race below -- because a *previous*, now-serverless keepalive
    // still holds the lock -- exec-fails straight out of `flock -n`, tearing
    // its session down and SIGHUP-killing the serve it just backgrounded. An
    // orphaned keepalive then wedges auto-start permanently: the distro stays
    // up (the stale keepalive holds it) but every new serve dies before it can
    // bind, so the app sits on "Connecting…" forever (2026-07-27 WSL incident).
    // setsid puts serve in a new session immune to that teardown; the stale
    // keepalive now merely keeps the distro alive long enough for it to bind.
    // `command -v setsid` guards it: setsid ships with util-linux (present
    // wherever the flock keepalive is) but NOT on macOS, whose native path has
    // no keepalive and already detaches via spawn({detached}) -- there $SETSID
    // is empty and this collapses back to the original bare exec.
    const setsidPrefix = 'SETSID="$(command -v setsid || true)"; '
    const redirect = ' </dev/null >>"$MF_LOG" 2>&1'
    const launch = WSL_REPO
      ? setsidPrefix + 'cd ' + shq(WSL_REPO)
        // Dev mode: bind the URL's port (from MINDFLOCK_URL, default 8765) and
        // force `local` so a machine whose persisted serve_mode is `tailscale`
        // doesn't silently expose the dev server on the LAN + demand an auth
        // token. A `.bat`/env PORT can't cross the Windows->WSL boundary, so the
        // port has to be spliced into the spawn command itself.
        + ' && exec $SETSID .venv/bin/python backend/web/run.py local ' + PORT + redirect
      : setsidPrefix
        + 'MF="$(command -v mindflock || true)";'
        + ' [ -z "$MF" ] && [ -x "$HOME/.local/bin/mindflock" ] && MF="$HOME/.local/bin/mindflock";'
        // cd "$HOME" first: wsl.exe starts the shell in the Windows cwd of the
        // app (C:\Program Files\MindFlock -> /mnt/c/...), and `mindflock serve`
        // manages whatever repo it is started from.
        + ' if [ -n "$MF" ]; then cd "$HOME"; exec $SETSID "$MF" serve' + redirect + ';'
        + ' else echo "mindflock is not installed ' + where + '."'
        + ' && echo "Install it:  curl -LsSf https://raw.githubusercontent.com/MindFlock/MindFlock/main/install.sh | sh";'
        + ' fi'
    // Single-owner rule: NEVER kill whatever holds the port. The old `fuser -k`
    // here and the systemd unit's ExecStartPre=fuser -k SIGKILLed each other at
    // every distro boot (2026-07-14 reboot-loop incident). If a systemd unit is
    // enabled (dev machines -- checked via its enable symlink, which works even
    // before the user manager is up) or something already answers on the port
    // inside WSL, launch nothing and just hold the distro open.
    // The log path is spliced in ONCE, quoted (shLogExpr), into a shell var;
    // every later use goes through "$MF_LOG" so spaces/quotes in the path
    // can't split words or terminate the script mid-expression.
    // The trailing keepalive is the actual reboot-loop fix: WSL terminates the
    // distro shortly after its last client (this wsl.exe) exits, systemd-run
    // server and all. Parking a flock'ed sleep in the foreground keeps wsl.exe
    // connected so the distro -- and the server -- stay up; `-n` guarantees at
    // most one keepalive per distro (later bootstraps exit immediately).
    const alive = '(exec 3<>/dev/tcp/127.0.0.1/' + PORT + ') 2>/dev/null'
    const script = 'MF_LOG=' + shLogExpr(log) + ';'
      + ' mkdir -p "$(dirname "$MF_LOG")";'
      + ' [ -f "$MF_LOG" ] && [ "$(wc -c < "$MF_LOG")" -gt 2000000 ] && : > "$MF_LOG";'
      + ' { echo "===== desktop bootstrap $(date) (port ' + PORT + ') =====";'
      + ' if [ -e "$HOME/.config/systemd/user/default.target.wants/mindflock.service" ];'
      + ' then echo "systemd mindflock.service owns port ' + PORT + '; not launching a second server.";'
      + ' elif ' + alive + '; then echo "a server is already listening on port ' + PORT + '; not launching another.";'
      + ' else (' + launch + ') & fi;'
      + ' } >> "$MF_LOG" 2>&1;'
      // WSL detection via kernel string, not $WSL_DISTRO_NAME -- the env var is
      // only set in wsl.exe-launched sessions. macOS/native Linux exit here.
      + ' grep -qi microsoft /proc/version 2>/dev/null || exit 0;'
      + ' exec flock -n /tmp/.mindflock-wsl-keepalive sleep 2147483647'
    try {
      if (process.platform === 'win32') {
        // Windows: the engine lives in WSL2. The bootstrap is a real shell
        // script (it quotes paths for dirname/wc and prints a dated banner).
        // Threading its inner quotes through the VBS string literal AND the
        // wsl.exe `-c "..."` command line is hopeless: doubling " for VBS does
        // not escape it for the Windows CreateProcess tokenizer, so wsl.exe
        // closes the -c argument at the first inner quote and bash silently
        // runs a truncated fragment. Instead we base64-encode the whole script
        // and hand wsl.exe a payload that is pure [A-Za-z0-9+/=] with no
        // quotes or spaces, so only the single outer quote pair matters.
        // wsl.exe is also a console app, so spawning it directly (even with
        // windowsHide) flashes a command window -- launch it through
        // wscript.exe, a GUI-subsystem host, running a one-line VBS that
        // starts wsl hidden (window style 0).
        const b64 = Buffer.from(script, 'utf8').toString('base64')
        const cmd = 'echo ' + b64 + ' | base64 -d | bash'
        const vbs =
          'CreateObject("WScript.Shell").Run "wsl.exe ' + WSL_D +
          '-e bash --login -c " & Chr(34) & "' + cmd + '" & Chr(34), 0, False\r\n'
        const vbsPath = path.join(app.getPath('userData'), 'start-server.vbs')
        fs.writeFileSync(vbsPath, vbs, 'utf8')
        spawn('wscript.exe', ['//nologo', vbsPath],
          { windowsHide: true, detached: true, stdio: 'ignore' }).unref()
        console.log('[mindflock] starting WSL server (hidden); server log ->', log)
      } else {
        // Linux / macOS: the server runs natively -- spawn it detached
        // through a login shell so ~/.local/bin lands on PATH.
        spawn('/bin/bash', ['--login', '-c', script],
          { detached: true, stdio: 'ignore' }).unref()
        console.log('[mindflock] starting local server; server log ->', log)
      }
    } catch (e) {
      console.log('[mindflock] could not start server:', e && e.message)
    }
  }
  sock.once('connect', () => finish(true))
  sock.once('error', () => finish(false))
  sock.setTimeout(700, () => finish(false))
}

// ---------------------------------------------------------------------------
// Offline diagnostics. The offline page polls `diag:get` so it can say WHY
// the UI can't load instead of dumping shell commands on the user:
//   checking       probe in flight (or never run)
//   starting       environment is fine -- the server is just booting
//   not-installed  WSL / this machine answers but mindflock isn't installed
//   wsl-down       wsl.exe hung (a genuinely wedged WSL) -- offer a WSL restart
//   wsl-setup      wsl.exe runs but has no usable distro -- WSL is only
//                  partially set up (`wsl --install` done but no distro, or a
//                  reboot still pending); guide the user to FINISH WSL setup
//                  rather than uselessly restarting it
//   wsl-missing    wsl.exe itself is absent (WSL was never installed)
// The probe runs a tiny script that exits 0 (ready) or 42 (not installed).
// On Windows it rides the same hidden wscript + base64 transport as the
// launcher above (spawning wsl.exe directly flashes a console window), and
// the answer comes back through WScript.Quit as the process exit code -- no
// stdout parsing, no temp files. A stopped-but-healthy distro cold-boots
// when probed, so the timeout is generous; only a genuinely wedged WSL
// (hung vmcompute etc.) lands in wsl-down.
const DIAG_PROBE_TIMEOUT_MS = 20000
const DIAG_STALE_MS = 8000
let diag = { state: 'checking', detail: '', distro: WSL_DISTRO_LABEL, platform: process.platform }
let diagAt = 0
let diagInFlight = false

function diagProbeScript() {
  // Mirrors the launcher's lookup: dev checkout when MINDFLOCK_REPO is set,
  // otherwise the installed CLI on the login PATH or ~/.local/bin.
  return WSL_REPO
    ? '[ -x ' + shq(WSL_REPO + '/.venv/bin/python') + ' ] && exit 0; exit 42'
    : 'command -v mindflock >/dev/null 2>&1 && exit 0;'
      + ' [ -x "$HOME/.local/bin/mindflock" ] && exit 0; exit 42'
}

function setDiag(state, detail) {
  diag = { state, detail: detail || '', distro: WSL_DISTRO_LABEL, platform: process.platform }
  diagAt = Date.now()
  console.log('[mindflock] diag:', state, detail || '')
}

function refreshDiag() {
  if (diagInFlight || Date.now() - diagAt < DIAG_STALE_MS) return
  diagInFlight = true
  const done = (state, detail) => { diagInFlight = false; setDiag(state, detail) }
  const win32 = process.platform === 'win32'
  try {
    let child
    if (win32) {
      const b64 = Buffer.from(diagProbeScript(), 'utf8').toString('base64')
      const cmd = 'echo ' + b64 + ' | base64 -d | bash'
      const vbs = 'On Error Resume Next\r\n'
        + 'rc = CreateObject("WScript.Shell").Run("wsl.exe ' + WSL_D
        + '-e bash --login -c " & Chr(34) & "' + cmd + '" & Chr(34), 0, True)\r\n'
        + 'If Err.Number <> 0 Then rc = 43\r\n'
        + 'WScript.Quit rc\r\n'
      const vbsPath = path.join(app.getPath('userData'), 'probe-wsl.vbs')
      fs.writeFileSync(vbsPath, vbs, 'utf8')
      child = spawn('wscript.exe', ['//nologo', vbsPath],
        { windowsHide: true, stdio: 'ignore' })
    } else {
      child = spawn('/bin/bash', ['--login', '-c', diagProbeScript()], { stdio: 'ignore' })
    }
    let settled = false
    const timer = setTimeout(() => {
      if (settled) return
      settled = true
      try { child.kill() } catch (e) {}
      // On Windows a hang here IS the symptom we're diagnosing (wedged WSL
      // never returns). Elsewhere a stuck login shell says nothing useful.
      done(win32 ? 'wsl-down' : 'starting',
        win32 ? 'WSL did not respond within ' + (DIAG_PROBE_TIMEOUT_MS / 1000) + ' seconds.' : '')
    }, DIAG_PROBE_TIMEOUT_MS)
    child.on('error', () => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      done(win32 ? 'wsl-missing' : 'starting')
    })
    child.on('exit', (code) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      if (code === 0) return done('starting')
      if (code === 42) return done('not-installed')
      if (!win32) return done('starting')       // odd login shell; let the retry loop work
      if (code === 43) return done('wsl-missing')
      // wsl.exe ran but our bash never did (nonzero, and not our 42). That
      // almost always means WSL has no usable distro -- it's only partially
      // set up (no distribution installed, or a reboot after `wsl --install`
      // still pending). A genuinely wedged WSL HANGS and is caught by the
      // timeout above (-> wsl-down). So send the user to finish WSL setup,
      // not to the "Restart WSL" button, which can't conjure a distro.
      done('wsl-setup', 'wsl.exe exited with code ' + code + '.')
    })
  } catch (e) {
    done(win32 ? 'wsl-down' : 'starting', e && e.message)
  }
}

ipcMain.handle('diag:get', () => {
  refreshDiag()
  return diag
})

// Reveal a file in Finder/Explorer (Settings → System logs "open in Finder").
// showItemInFolder opens the containing folder with the file selected.
ipcMain.handle('shell:show-item', (_e, p) => {
  const target = String(p || '').trim()
  if (!target) return { ok: false, error: 'no path' }
  // The engine can live on a different host than this shell — the common case
  // is the server running inside WSL while the app runs on Windows, so the log
  // path it reports ("/tmp/mindflock.log") is a Linux path Explorer can't reach.
  // showItemInFolder fails SILENTLY there (path.resolve turns it into a bogus
  // C:\tmp\… , nothing opens, and it never throws), which left the click doing
  // nothing at all. Guard on the file actually being reachable from THIS machine
  // and otherwise report failure so the renderer falls back to copying the path.
  let resolved
  try { resolved = path.resolve(target) } catch (e) { return { ok: false, error: 'bad path' } }
  if (!fs.existsSync(resolved)) return { ok: false, error: 'not on this machine' }
  try {
    shell.showItemInFolder(resolved)
    return { ok: true }
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) }
  }
})

// One-click recovery for a wedged WSL: `wsl --shutdown` stops the utility VM
// (the offline page warns that this closes anything else running in WSL),
// then the normal launcher cold-boots the distro and the server again.
ipcMain.handle('diag:restart-wsl', async () => {
  if (process.platform !== 'win32') return { ok: false }
  setDiag('checking', 'Restarting WSL…')
  await new Promise((resolve) => {
    try {
      const vbs = 'On Error Resume Next\r\n'
        + 'CreateObject("WScript.Shell").Run "wsl.exe --shutdown", 0, True\r\n'
        + 'WScript.Quit 0\r\n'
      const vbsPath = path.join(app.getPath('userData'), 'restart-wsl.vbs')
      fs.writeFileSync(vbsPath, vbs, 'utf8')
      const child = spawn('wscript.exe', ['//nologo', vbsPath],
        { windowsHide: true, stdio: 'ignore' })
      const timer = setTimeout(() => { try { child.kill() } catch (e) {}; resolve() }, 15000)
      child.on('exit', () => { clearTimeout(timer); resolve() })
      child.on('error', () => { clearTimeout(timer); resolve() })
    } catch (e) { resolve() }
  })
  lastSpawnAt = 0     // shutdown killed any half-booted server; respawn now
  diagAt = 0          // and re-probe immediately instead of serving stale wsl-down
  startServerIfNeeded()
  refreshDiag()
  return { ok: true }
})

// ---------------------------------------------------------------------------
// One-click engine install. The desktop app is only the client; the engine is
// the `mindflock` CLI that install.sh puts on the machine (uv + the Python
// package). The Windows NSIS installer runs that script at install time, but a
// macOS .dmg is a drag-copy with no post-install hook and an AppImage is never
// "installed" at all -- so on those platforms first launch offers the same
// install as a button instead of printing a curl for the user to paste.
//
// The script is BUNDLED (electron-builder `extraResources`), never fetched:
// the app runs the exact install.sh from its own build, so there is no remote
// shell script to 404, MITM, or drift out of sync with the app.
//
// Output is a ring buffer HERE, in the main process, which the offline page
// polls -- it is not pushed to the renderer. An install runs for minutes, and
// the page can be torn down under it at any point (a renderer crash, or the
// window being reloaded onto the app the moment the server answers), so
// anything the user must keep seeing has to outlive the renderer.
//
// MINDFLOCK_INSTALL_SCRIPT overrides the path -- point it at a stub to
// exercise this flow without actually reinstalling the engine.
const INSTALL_SCRIPT = process.env.MINDFLOCK_INSTALL_SCRIPT || (app.isPackaged
  ? path.join(process.resourcesPath, 'install.sh')
  : path.join(__dirname, '..', 'install.sh'))

// Pin the CLI to this app's version, exactly as build/installer.nsh does, so
// the engine and the shell that talks to it are the same release. An
// unpackaged dev run has no meaningful tag, so it tracks main.
const INSTALL_REF = app.isPackaged ? 'v' + app.getVersion() : 'main'

const INSTALL_LOG_MAX = 400
// Last line the Windows install writes: wscript's exit code is the launcher's,
// not the script's, so the real status has to travel in the log itself.
const INSTALL_SENTINEL = 'MINDFLOCK_INSTALL_EXIT='
const INSTALL_TIMEOUT_MS = 30 * 60 * 1000
const ANSI_RE = /\x1b\[[0-9;]*m/g

// state: idle | running | done | failed
let install = { state: 'idle', code: null, lines: [] }
let installTicker = null

function toLines(s) {
  return String(s).replace(ANSI_RE, '').replace(/\r/g, '\n').split('\n')
    .map((l) => l.trimEnd()).filter((l) => l !== '')
}

function installLog(chunk) {
  install.lines = install.lines.concat(toLines(chunk)).slice(-INSTALL_LOG_MAX)
}

function installFinish(code) {
  if (install.state !== 'running') return
  if (installTicker) { clearInterval(installTicker); installTicker = null }
  install.code = code
  install.state = code === 0 ? 'done' : 'failed'
  console.log('[mindflock] engine install finished, code', code)
  // The CLI exists now, so the cached "not-installed" diagnosis and the spawn
  // cooldown are both stale -- clear them so the very next retry probes fresh
  // and starts the server, instead of sitting on the offline page for another
  // cooldown window after a successful install.
  diagAt = 0
  lastSpawnAt = 0
  // The engine on disk just changed, so the update verdict is stale. Clear it;
  // the next successful load re-checks (until the server restarts it still
  // reports the OLD version, which is what the toast already told the user).
  engineNotice = null
  if (code === 0) startServerIfNeeded()
}

// macOS ships /usr/bin/git as a stub that does nothing but pop Apple's Command
// Line Tools installer, so `command -v git` succeeds on a machine where git
// cannot actually run. xcode-select -p is the honest check and pops nothing.
function hasXcodeCLT() {
  try { return spawnSync('xcode-select', ['-p'], { stdio: 'ignore' }).status === 0 }
  catch (e) { return false }
}

function startInstall(ref) {
  ref = ref || INSTALL_REF   // default: this app's pinned version (first install)
  if (install.state === 'running') return { started: false }
  install = { state: 'running', code: null, lines: [] }
  installLog('=== installing the MindFlock engine (' + ref + ') ===')

  if (!fs.existsSync(INSTALL_SCRIPT)) {
    installLog('install.sh is missing from this build: ' + INSTALL_SCRIPT)
    installFinish(1)
    return { started: true }
  }

  // Environment shared by both transports.
  //   MINDFLOCK_INSTALL_REF   pin the engine to this app's version
  //   MINDFLOCK_NONINTERACTIVE  there is no controlling terminal behind a GUI
  //     app, so force `mindflock doctor`'s read-only report; its --fix mode
  //     would sit waiting on y/n prompts nobody can answer.
  const envAdds = { MINDFLOCK_INSTALL_REF: ref, MINDFLOCK_NONINTERACTIVE: '1' }

  if (process.platform !== 'win32') {
    if (process.platform === 'darwin' && !hasXcodeCLT()) {
      installLog('The Xcode Command Line Tools (which provide git) are not installed,')
      installLog('and the engine cannot be fetched without git.')
      installLog('Opening Apple’s installer now — finish it, then press Install again.')
      try {
        spawn('xcode-select', ['--install'], { stdio: 'ignore', detached: true }).unref()
      } catch (e) { installLog('could not open it: run  xcode-select --install') }
      installFinish(1)
      return { started: true }
    }
    // --login so ~/.local/bin (where uv and the CLI land) is on PATH for the
    // doctor step at the end of the script.
    try {
      const child = spawn('/bin/bash', ['--login', INSTALL_SCRIPT], {
        env: Object.assign({}, process.env, envAdds),
        stdio: ['ignore', 'pipe', 'pipe'],
      })
      child.stdout.on('data', installLog)
      child.stderr.on('data', installLog)
      child.on('error', (e) => {
        installLog('could not run the installer: ' + (e && e.message))
        installFinish(1)
      })
      child.on('exit', (code) => installFinish(code == null ? 1 : code))
      const killAt = setTimeout(() => {
        if (install.state !== 'running') return
        installLog('installer timed out after ' + (INSTALL_TIMEOUT_MS / 60000) + ' minutes.')
        try { child.kill() } catch (e) {}
        installFinish(1)
      }, INSTALL_TIMEOUT_MS)
      child.on('close', () => clearTimeout(killAt))
    } catch (e) {
      installLog('could not run the installer: ' + (e && e.message))
      installFinish(1)
    }
    return { started: true }
  }

  // Windows: the engine installs INSIDE WSL, reached through the same hidden
  // wscript transport the launcher and the probe use (spawning wsl.exe
  // directly flashes a console window). Nothing can be piped back through
  // that, so the WSL side redirects the whole run to a log on the WINDOWS
  // filesystem -- wslpath turns the Windows paths into /mnt/c ones -- and we
  // tail that file below.
  const winLog = path.join(app.getPath('userData'), 'engine-install.log')
  try { fs.writeFileSync(winLog, '') } catch (e) {}
  // A Windows-built bundle can ship install.sh with CRLF line endings, which
  // dash/bash inside WSL refuse to run ("set: Illegal option -" on `set -eu`).
  // Copy it to a temp file with the CRs stripped and run that, so the engine
  // install works even if a build slipped through without LF normalization.
  const script =
    'L="$(wslpath -a ' + shq(winLog) + ')";'
    + ' S="$(wslpath -a ' + shq(INSTALL_SCRIPT) + ')";'
    + ' T="$(mktemp)"; tr -d "\\r" < "$S" > "$T";'
    + ' { MINDFLOCK_INSTALL_REF=' + shq(ref) + ' MINDFLOCK_NONINTERACTIVE=1 sh "$T";'
    + ' echo "' + INSTALL_SENTINEL + '$?"; } > "$L" 2>&1;'
    + ' rm -f "$T"'
  try {
    const b64 = Buffer.from(script, 'utf8').toString('base64')
    const cmd = 'echo ' + b64 + ' | base64 -d | bash'
    const vbs =
      'CreateObject("WScript.Shell").Run "wsl.exe ' + WSL_D +
      '-e bash --login -c " & Chr(34) & "' + cmd + '" & Chr(34), 0, False\r\n'
    const vbsPath = path.join(app.getPath('userData'), 'install-engine.vbs')
    fs.writeFileSync(vbsPath, vbs, 'utf8')
    spawn('wscript.exe', ['//nologo', vbsPath],
      { windowsHide: true, detached: true, stdio: 'ignore' }).unref()
  } catch (e) {
    installLog('could not start the WSL installer: ' + (e && e.message))
    installFinish(1)
    return { started: true }
  }

  // Re-read the log each tick and rebuild the line list rather than tracking a
  // byte offset: the file is small, and a partial trailing line then simply
  // completes itself on the next pass instead of being logged twice.
  const header = install.lines.slice()
  const startedAt = Date.now()
  installTicker = setInterval(() => {
    let text = ''
    try { text = fs.readFileSync(winLog, 'utf8') } catch (e) { text = '' }
    const end = text.indexOf(INSTALL_SENTINEL)
    const body = end >= 0 ? text.slice(0, end) : text
    install.lines = header.concat(toLines(body)).slice(-INSTALL_LOG_MAX)
    if (end >= 0) {
      const m = /MINDFLOCK_INSTALL_EXIT=(-?\d+)/.exec(text)
      return installFinish(m ? Number(m[1]) : 1)
    }
    if (Date.now() - startedAt > INSTALL_TIMEOUT_MS) {
      installLog('installer timed out after ' + (INSTALL_TIMEOUT_MS / 60000) + ' minutes.')
      installFinish(1)
    }
  }, 1000)
  return { started: true }
}

ipcMain.handle('install:start', () => startInstall())
ipcMain.handle('install:state', () => ({
  state: install.state,
  code: install.code,
  lines: install.lines,
  // The manual escape hatch the offline page shows when an install fails.
  command: 'curl -LsSf https://raw.githubusercontent.com/MindFlock/MindFlock/'
    + INSTALL_REF + '/install.sh | sh',
}))

// ---------------------------------------------------------------------------
// Engine lifecycle: restart-to-finish-an-update, and uninstall.
//
// Both stop the running server the same way. Quitting the app does NOT stop
// the server (the WSL keepalive / a detached native process outlives the
// window), so "pick up the new engine" and "remove the engine" both have to
// kill the serve process explicitly. Killing the keepalive too is safe: on
// Windows our OWN wsl.exe session (or the next launcher) holds the distro.
const KILL_SERVER =
  "pkill -f 'mindflock serve' 2>/dev/null || true;"
  + " pkill -f 'backend/web/run.py' 2>/dev/null || true;"
  + " pkill -f 'mindflock-wsl-keepalive' 2>/dev/null || true"

// Run a bash script in the engine's environment — inside WSL on Windows (the
// same hidden wscript+base64 transport startServerIfNeeded uses, so no console
// flashes and no quoting has to survive wsl.exe's tokenizer), natively through
// a login shell elsewhere. Fire-and-forget.
function spawnEngineShell(scriptText) {
  try {
    if (process.platform === 'win32') {
      const b64 = Buffer.from(scriptText, 'utf8').toString('base64')
      const cmd = 'echo ' + b64 + ' | base64 -d | bash'
      const vbs = 'CreateObject("WScript.Shell").Run "wsl.exe ' + WSL_D +
        '-e bash --login -c " & Chr(34) & "' + cmd + '" & Chr(34), 0, False\r\n'
      const vbsPath = path.join(app.getPath('userData'), 'engine-shell.vbs')
      fs.writeFileSync(vbsPath, vbs, 'utf8')
      spawn('wscript.exe', ['//nologo', vbsPath],
        { windowsHide: true, detached: true, stdio: 'ignore' }).unref()
    } else {
      spawn('/bin/bash', ['--login', '-c', scriptText],
        { detached: true, stdio: 'ignore' }).unref()
    }
  } catch (e) {
    console.log('[mindflock] engine shell failed:', e && e.message)
  }
}

// Finish an engine update: stop the old server, then let the normal launcher
// bring a FRESH `mindflock serve` up (resolving the shim -> the upgraded venv;
// a bare re-exec can miss a relocated venv) and reconnect the window. Resetting
// the caches makes the next probe/spawn immediate and re-checks the engine
// version instead of serving the stale drift verdict.
function restartServer() {
  spawnEngineShell(KILL_SERVER)
  setTimeout(() => {
    lastSpawnAt = 0
    engineNotice = null
    startServerIfNeeded()
    loadApp()
  }, 1800)
}

ipcMain.handle('engine:restart', () => { restartServer(); return { ok: true } })

// The teardown `mindflock uninstall` runs. NO --purge unless the user opts in:
// ~/.mindflock[-assistant] (history + settings) is kept so a reinstall resumes.
// The repo footprint (worktrees, hooks) IS reversed, then the uv tool + shim
// go — matching build/installer.nsh's Windows customUnInstall exactly.
function buildTeardownScript(purge) {
  // export PATH first: a macOS GUI app spawns bash with the minimal launchd
  // PATH, and a bash login shell there doesn't read the user's zsh rc, so
  // ~/.local/bin (where install.sh puts BOTH uv and mindflock) is absent —
  // `command -v uv` then fails and the tool is never removed. Prepending it
  // (exactly as install.sh does) fixes that; the absolute fallbacks below are
  // belt-and-suspenders for an unreadable/empty ~/.local/bin resolution.
  return 'export PATH="$HOME/.local/bin:$PATH";'
    + ' MF="$(command -v mindflock || true)";'
    + ' [ -z "$MF" ] && [ -x "$HOME/.local/bin/mindflock" ] && MF="$HOME/.local/bin/mindflock";'
    + ' UV="$(command -v uv || true)";'
    + ' [ -z "$UV" ] && [ -x "$HOME/.local/bin/uv" ] && UV="$HOME/.local/bin/uv";'
    + ' ' + KILL_SERVER + ';'
    + ' sleep 1;'
    + ' if [ -n "$MF" ]; then MINDFLOCK_NONINTERACTIVE=1 "$MF" uninstall --yes'
    + (purge ? ' --purge' : '') + ' || true; fi;'
    + ' [ -n "$UV" ] && "$UV" tool uninstall mindflock 2>/dev/null || true'
}

// mac / Linux uninstall: no OS hook fires when a .app is trashed or an AppImage
// deleted, so the app does the whole thing — tear down the engine, then remove
// its own shell — and quits. (Windows routes to Add/Remove Programs instead;
// installer.nsh's customUnInstall clears the engine there.)
async function runUninstall(purge) {
  await new Promise((resolve) => {
    try {
      const child = spawn('/bin/bash', ['--login', '-c', buildTeardownScript(purge)],
        { stdio: 'ignore' })
      child.on('exit', resolve)
      child.on('error', resolve)
    } catch (e) { resolve() }
  })
  try {
    if (process.platform === 'darwin') {
      // exe -> MindFlock.app/Contents/MacOS/MindFlock; the bundle is 3 up.
      await shell.trashItem(path.resolve(app.getPath('exe'), '..', '..', '..'))
    } else if (process.env.APPIMAGE) {
      await shell.trashItem(process.env.APPIMAGE)
    }
  } catch (e) {
    console.log('[mindflock] could not remove the app shell:', e && e.message)
  }
  app.exit(0)
}

ipcMain.handle('app:uninstall', async () => {
  if (!win) return { ok: false }
  if (process.platform === 'win32') {
    // Removing the NSIS-installed shell from inside itself isn't possible;
    // Add/Remove Programs is the real path and its customUnInstall clears the
    // engine too. Point the user there rather than doing half the job.
    const r = await dialog.showMessageBox(win, {
      type: 'info',
      title: 'Uninstall MindFlock',
      message: 'Uninstall MindFlock from Windows Settings',
      detail: 'Open Settings → Apps → MindFlock → Uninstall. That removes the app AND '
        + 'the engine inside WSL (worktrees, agent hooks, and the mindflock tool) in one '
        + 'step. Your history and settings are kept.',
      buttons: ['Open Apps settings', 'Cancel'],
      defaultId: 0,
      cancelId: 1,
    })
    if (r.response === 0) shell.openExternal('ms-settings:appsfeatures')
    return { ok: true }
  }
  const res = await dialog.showMessageBox(win, {
    type: 'warning',
    title: 'Uninstall MindFlock',
    message: 'Uninstall MindFlock and its engine?',
    detail: 'This stops the server, reverses MindFlock’s changes to your repos (the '
      + 'worktrees and agent hooks it added), and removes the mindflock tool. MindFlock '
      + 'then quits and moves itself to the Trash.',
    checkboxLabel: 'Also delete my history & settings (cannot be undone)',
    checkboxChecked: false,
    buttons: ['Cancel', 'Uninstall'],
    defaultId: 0,
    cancelId: 0,
    noLink: true,
  })
  if (res.response !== 1) return { ok: false }
  await runUninstall(res.checkboxChecked)
  return { ok: true }
})

// Thin dark scrollbars (QtWebEngine/Chromium render fat classic ones otherwise).
const SCROLLBAR_CSS = `
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb {
  background-color: #3a4150; border-radius: 8px;
  border: 2px solid transparent; background-clip: content-box;
}
::-webkit-scrollbar-thumb:hover { background-color: #4b5468; background-clip: content-box; }
::-webkit-scrollbar-corner { background: transparent; }
`

// The web UI now ships its own full-width top bar (#topbar, 40px) which is the
// native drag region (-webkit-app-region: drag). Here we only overlay the three
// window buttons (min/max/close) at the top-right, over the top bar's empty drag
// tail. No grid offset and no full-width drag strip -- that strip used to sit on
// top of the top-bar buttons and swallow their clicks.
const TITLEBAR_CSS = `
#mf-winctl {
  position: fixed; top: 0; right: 0; height: 40px;
  display: flex; align-items: center; justify-content: flex-end;
  -webkit-app-region: no-drag; z-index: 2147483647; background: transparent;
}
#mf-winctl .mf-wbtn {
  -webkit-app-region: no-drag;
  width: 46px; height: 40px; display: flex; align-items: center; justify-content: center;
  border: 0; background: transparent; color: var(--muted, #8a90a2);
  font: 13px system-ui, 'Segoe UI', sans-serif; line-height: 1; cursor: pointer;
  transition: background .12s ease, color .12s ease;
}
#mf-winctl .mf-wbtn:hover { background: rgba(125,86,244,.18); color: var(--text, #d7dae3); }
#mf-winctl .mf-wbtn.mf-close:hover { background: #e5484d; color: #fff; }
`

const TITLEBAR_JS = `
(function () {
  // macOS draws the real traffic lights (titleBarStyle:'hidden' above), so
  // adding ours would give the window two sets of controls.
  if (${process.platform === 'darwin'}) return;
  if (document.getElementById('mf-winctl')) return;
  var bar = document.createElement('div');
  bar.id = 'mf-winctl';
  function mk(cls, glyph, fn) {
    var b = document.createElement('button');
    b.className = 'mf-wbtn ' + cls;
    b.innerHTML = glyph;
    b.addEventListener('click', function (e) { e.stopPropagation(); try { fn(b); } catch (err) {} });
    return b;
  }
  bar.appendChild(mk('mf-min', '&#8211;', function () { window.winctl.minimize(); }));
  var maxBtn = mk('mf-max', '&#9633;', function () { window.winctl.toggleMaximize(); });
  bar.appendChild(maxBtn);
  bar.appendChild(mk('mf-close', '&#10005;', function () { window.winctl.close(); }));
  document.body.appendChild(bar);
  if (window.winctl && window.winctl.onMaximizedChanged) {
    window.winctl.onMaximizedChanged(function (isMax) {
      maxBtn.innerHTML = isMax ? '&#10065;' : '&#9633;';   // ❐ vs □
    });
  }
})();
`

// ---------------------------------------------------------------------------
// Update checks. Poll GitHub Releases for a newer MindFlock desktop build and,
// when one exists, nudge the user with a bottom-right toast (injected into the
// loaded page). Entirely best-effort: any network / parse / rate-limit / 404
// (private repo, no releases yet) error is swallowed so an offline machine
// simply never sees a prompt — "only if present and online" falls out of that.
// This is about the desktop app's OWN version; it is unrelated to whether the
// engine has `gh` or GitHub credentials.
// ---------------------------------------------------------------------------
const UPDATE_REPO = process.env.MINDFLOCK_UPDATE_REPO || 'MindFlock/MindFlock'
const UPDATE_RELEASES_URL = 'https://github.com/' + UPDATE_REPO + '/releases/latest'
// Check shortly after launch (once the window has settled), then on a long
// interval so a long-lived app still learns about releases without nagging.
const UPDATE_FIRST_DELAY_MS = 20 * 1000
const UPDATE_INTERVAL_MS = 6 * 60 * 60 * 1000     // 6h
// Where "skip this version" is remembered across restarts.
const updateStorePath = () => path.join(app.getPath('userData'), 'update-state.json')

let updateAvailable = null   // { version, url, notes } once a newer release is seen
let skippedVersion = ''      // a version the user asked us to stop nudging about

function readUpdateStore() {
  try { return JSON.parse(fs.readFileSync(updateStorePath(), 'utf8')) || {} }
  catch (e) { return {} }
}
function writeUpdateStore(obj) {
  try { fs.writeFileSync(updateStorePath(), JSON.stringify(obj)) } catch (e) {}
}

// Compare dotted numeric versions. >0 if a newer than b, <0 older, 0 equal.
// Prerelease / non-numeric suffixes are ignored — we only nudge on a clear bump.
function cmpVersion(a, b) {
  const pa = String(a).split('.').map((n) => parseInt(n, 10) || 0)
  const pb = String(b).split('.').map((n) => parseInt(n, 10) || 0)
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const d = (pa[i] || 0) - (pb[i] || 0)
    if (d) return d > 0 ? 1 : -1
  }
  return 0
}

// Resolve the latest release JSON, or null on ANY failure (offline, non-200,
// bad JSON). Never throws.
function fetchLatestRelease() {
  return new Promise((resolve) => {
    let done = false
    const finish = (v) => { if (!done) { done = true; resolve(v) } }
    const req = https.request(
      {
        method: 'GET',
        host: 'api.github.com',
        path: '/repos/' + UPDATE_REPO + '/releases/latest',
        headers: {
          'User-Agent': 'MindFlock-Desktop',
          Accept: 'application/vnd.github+json',
        },
        timeout: 8000,
      },
      (res) => {
        if (res.statusCode !== 200) { res.resume(); return finish(null) }
        let body = ''
        res.setEncoding('utf8')
        res.on('data', (c) => { body += c; if (body.length > 1_000_000) req.destroy() })
        res.on('end', () => { try { finish(JSON.parse(body)) } catch (e) { finish(null) } })
      }
    )
    req.on('error', () => finish(null))               // offline / DNS / TLS -> silent
    req.on('timeout', () => { req.destroy(); finish(null) })
    req.end()
  })
}

// The pending update to show, or null (nothing newer, or the user skipped it).
function pendingUpdate() {
  if (!updateAvailable) return null
  if (updateAvailable.version === skippedVersion) return null
  return updateAvailable
}

function pushUpdateToRenderer() {
  const p = pendingUpdate()
  if (p && win && !win.isDestroyed() && !win.webContents.isDestroyed()) {
    win.webContents.send('update:available', p)
  }
}

async function checkForUpdates() {
  const rel = await fetchLatestRelease()
  if (!rel || !rel.tag_name) return
  const latest = String(rel.tag_name).replace(/^v/i, '').trim()
  if (!latest) return
  if (cmpVersion(latest, app.getVersion()) <= 0) { updateAvailable = null; return }
  updateAvailable = {
    version: latest,
    current: app.getVersion(),
    url: rel.html_url || UPDATE_RELEASES_URL,
    notes: String(rel.name || rel.body || '').slice(0, 280),
  }
  console.log('[mindflock] update available:', latest, '(current', app.getVersion() + ')')
  pushUpdateToRenderer()
}

function startUpdateChecks() {
  skippedVersion = readUpdateStore().skippedVersion || ''
  const tick = () => {
    checkForUpdates().catch(() => {})     // desktop-shell release (notify-only)
    checkEngineVersion().catch(() => {})  // engine release (offer to update)
  }
  setTimeout(tick, UPDATE_FIRST_DELAY_MS)
  setInterval(tick, UPDATE_INTERVAL_MS)
}

// Renderer bridge (see preload's `mfupdate`). `get` lets a freshly-loaded page
// pull the current state even if the push fired before its listener existed.
ipcMain.handle('update:get', () => pendingUpdate())
ipcMain.on('update:open', (_e, url) => {
  const dest = typeof url === 'string' && url ? url : UPDATE_RELEASES_URL
  shell.openExternal(dest).catch(() => {})
})
ipcMain.on('update:skip', (_e, version) => {
  skippedVersion = String(version || (updateAvailable && updateAvailable.version) || '')
  writeUpdateStore({ skippedVersion })
})

// ---------------------------------------------------------------------------
// Engine / app version drift.
//
// The shell pins the engine to its own version at install time (INSTALL_REF),
// but it only ever RUNS that install when the engine is ABSENT -- the offline
// page's 'not-installed' state. So updating the app alone leaves the old
// engine in place indefinitely, and `curl install.sh | sh` (which defaults to
// main) can push the engine AHEAD of the app instead. Nothing checked either
// direction, and the engine-ahead case can trip the backend's state.json
// downgrade path, where the session list comes up empty until the versions
// line up again.
//
// The engine reports its version through GET /api/doctor, which every client
// already talks to -- so one HTTP code path covers macOS, Linux and
// Windows/WSL identically (no wsl.exe transport, no stdout parsing).
// Engine-update state: set when the INSTALLED engine is older than the latest
// RELEASED engine. Release-anchored (not app-anchored): the engine tracks the
// latest release independently of whether the desktop shell was updated, which
// is what lets an engine-only version bump reach users who never update the
// (unsigned, no-auto-update) shell. null = up to date / unknown.
let engineNotice = null      // { current, latest, ref } | null

// GET a JSON document from the local server, or null on ANY failure. Never
// throws: an engine check must never be able to break app startup.
function fetchLocalJSON(pathname, timeoutMs) {
  return new Promise((resolve) => {
    let done = false
    const finish = (v) => { if (!done) { done = true; resolve(v) } }
    let req
    try {
      req = http.get(
        { host: '127.0.0.1', port: PORT, path: pathname, timeout: timeoutMs || 4000 },
        (res) => {
          if (res.statusCode !== 200) { res.resume(); return finish(null) }
          let body = ''
          res.setEncoding('utf8')
          res.on('data', (c) => { body += c; if (body.length > 1_000_000) req.destroy() })
          res.on('end', () => { try { finish(JSON.parse(body)) } catch (e) { finish(null) } })
        }
      )
    } catch (e) { return finish(null) }
    req.on('error', () => finish(null))
    req.on('timeout', () => { req.destroy(); finish(null) })
  })
}

async function checkEngineVersion() {
  // Compare the INSTALLED engine (GET /api/doctor) against the latest RELEASED
  // engine (GitHub /releases/latest). Re-runnable (no one-shot latch): a new
  // release can drop mid-session, and a re-check after an update clears the
  // notice. Every failure path is silent — an engine check must never break
  // startup, and "offline / no release / server booting" simply tries later.
  if (!app.isPackaged) return          // dev: Electron's own version is noise
  const doc = await fetchLocalJSON('/api/doctor')
  const current = doc && typeof doc.version === 'string' ? doc.version.trim() : ''
  if (!current) return                 // server down, or an engine too old to report it
  const rel = await fetchLatestRelease()
  const latest = rel && rel.tag_name ? String(rel.tag_name).replace(/^v/i, '').trim() : ''
  if (!latest || cmpVersion(latest, current) <= 0) {
    engineNotice = null                // up to date (or couldn't tell) — no nag
    return
  }
  engineNotice = { current, latest, ref: 'v' + latest }
  console.log('[mindflock] engine update available:', current, '->', latest)
  pushEngineNotice()
}

function pushEngineNotice() {
  if (engineNotice && win && !win.isDestroyed() && !win.webContents.isDestroyed()) {
    win.webContents.send('engine:notice', engineNotice)
  }
}

ipcMain.handle('engine:get', () => engineNotice)
// What Settings → Advanced polls to show/hide its engine-update control.
ipcMain.handle('engine:update-info', () => (engineNotice
  ? { available: true, current: engineNotice.current, latest: engineNotice.latest }
  : { available: false }))
ipcMain.handle('engine:install', () => {
  // Install the LATEST released engine when an update is pending; fall back to
  // this app's pinned version otherwise (e.g. a manual reinstall with no notice
  // set). Same installer the offline page uses, just a different pinned ref.
  return startInstall(engineNotice ? engineNotice.ref : INSTALL_REF)
})
ipcMain.handle('engine:install-state', () => ({
  state: install.state, code: install.code, lines: install.lines,
}))
ipcMain.on('engine:dismiss', () => { engineNotice = null })

// Bottom-right "update available" toast. Injected into whatever page is loaded
// (same mechanism as the title-bar buttons), styled to match the app's dark
// chrome. It talks to the main process through the preload `mfupdate` bridge.
const UPDATE_CSS = `
/* Both notices (a new app release, and engine/shell version drift) live in one
   bottom-right column so a machine showing both doesn't stack them on top of
   each other. */
#mf-toast-stack {
  position: fixed; right: 18px; bottom: 18px; z-index: 2147483646;
  display: flex; flex-direction: column-reverse; gap: 10px;
  align-items: flex-end; pointer-events: none;
}
#mf-toast-stack > * { pointer-events: auto; }
#mf-update-toast, #mf-engine-toast {
  position: relative;
  width: 300px; max-width: calc(100vw - 36px);
  background: #171b24; color: #d7dae3;
  border: 1px solid #2a3140; border-radius: 12px;
  box-shadow: 0 12px 40px rgba(0,0,0,.45);
  padding: 14px 16px 12px; font: 13px system-ui, 'Segoe UI', sans-serif;
  animation: mf-up-in .18s ease-out;
}
/* Drift is a "your install is inconsistent" state, not a new-release nudge. */
#mf-engine-toast { border-left: 3px solid #d6a53c; }
#mf-engine-toast .mf-up-title { color: #e2b658; }
#mf-engine-toast .mf-up-log {
  margin: 0 0 10px; padding: 8px 10px; max-height: 120px; overflow-y: auto;
  background: #10131a; border: 1px solid #232a37; border-radius: 8px;
  font: 11px ui-monospace, Menlo, Consolas, monospace; color: #97a0b5;
  white-space: pre-wrap; word-break: break-word;
}
@keyframes mf-up-in { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }
#mf-update-toast .mf-up-close, #mf-engine-toast .mf-up-close {
  position: absolute; top: 8px; right: 8px; width: 24px; height: 24px;
  border: 0; background: transparent; color: #8a90a2; cursor: pointer;
  border-radius: 6px; font-size: 11px; line-height: 1;
}
#mf-toast-stack .mf-up-close:hover { background: rgba(255,255,255,.08); color: #d7dae3; }
#mf-toast-stack .mf-up-title { font-weight: 600; font-size: 14px; margin-bottom: 4px; padding-right: 20px; }
#mf-toast-stack .mf-up-body { color: #b6bccb; margin-bottom: 12px; line-height: 1.4; }
#mf-toast-stack .mf-up-row { display: flex; gap: 8px; }
#mf-toast-stack .mf-up-btn {
  flex: 1; padding: 7px 10px; border-radius: 8px; cursor: pointer;
  border: 1px solid #333c4d; background: #232a37; color: #d7dae3;
  font: 500 13px system-ui, 'Segoe UI', sans-serif; transition: background .12s ease;
}
#mf-toast-stack .mf-up-btn:hover { background: #2c3444; }
#mf-toast-stack .mf-up-btn:disabled { opacity: .6; cursor: default; }
#mf-toast-stack .mf-up-primary { background: #7d56f4; border-color: #7d56f4; color: #fff; }
#mf-toast-stack .mf-up-primary:hover { background: #6b45e0; }
#mf-toast-stack .mf-up-skip {
  display: block; margin: 10px 0 0; padding: 0; border: 0; background: transparent;
  color: #8a90a2; font-size: 11px; cursor: pointer; text-decoration: underline;
}
#mf-toast-stack .mf-up-skip:hover { color: #b6bccb; }
`

const UPDATE_JS = `
(function () {
  if (window.__mfUpdateInit) return; window.__mfUpdateInit = true;
  if (!window.mfupdate && !window.mfengine) return;
  function drop(el) { if (el && el.parentNode) el.parentNode.removeChild(el); }
  function stack() {
    var s = document.getElementById('mf-toast-stack');
    if (!s) {
      s = document.createElement('div');
      s.id = 'mf-toast-stack';
      document.body.appendChild(s);
    }
    return s;
  }
  function card(id) {
    drop(document.getElementById(id));
    var c = document.createElement('div');
    c.id = id;
    stack().appendChild(c);
    return c;
  }
  function closeBtn(c, onClose) {
    var b = document.createElement('button');
    b.className = 'mf-up-close'; b.title = 'Dismiss'; b.innerHTML = '&#10005;';
    b.addEventListener('click', function () { drop(c); if (onClose) onClose(); });
    return b;
  }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  // --- new app release ---------------------------------------------------
  function show(info) {
    if (!info || !info.version) return;
    var c = card('mf-update-toast');
    var row = el('div', 'mf-up-row');
    var upd = el('button', 'mf-up-btn mf-up-primary', 'Update');
    upd.addEventListener('click', function () { window.mfupdate.openDownload(info.url); });
    var later = el('button', 'mf-up-btn', 'Later');
    later.addEventListener('click', function () { drop(c); });
    row.appendChild(upd); row.appendChild(later);
    var skip = el('button', 'mf-up-skip', 'Skip this version');
    skip.addEventListener('click', function () { window.mfupdate.skip(info.version); drop(c); });
    c.appendChild(closeBtn(c));
    c.appendChild(el('div', 'mf-up-title', 'Update available'));
    // Show what they’re on: the wordmark carries the ENGINE version (which
    // updates on its own), so without this a user whose engine already reads
    // 0.1.2 sees "0.1.2 is available" and thinks it’s nagging about the
    // version they’re already running — it’s the desktop app that’s behind.
    c.appendChild(el('div', 'mf-up-body',
      'MindFlock ' + info.version + ' is available'
      + (info.current && info.current !== info.version
          ? ' \\u2014 the desktop app you\\u2019re running is ' + info.current : '')
      + '.'));
    c.appendChild(row); c.appendChild(skip);
  }

  // --- engine update available -------------------------------------------
  // Fires when the INSTALLED engine is older than the latest RELEASED engine
  // (info = { current, latest, ref }). Optional and non-nagging: "Later"
  // dismisses for this run. Updating pulls only the small engine package (uv
  // reuses its dependency cache) — NOT the ~100 MB desktop shell — then the
  // server self-restarts and the window reconnects.
  function showEngine(info) {
    if (!info || !info.latest) return;
    var c = card('mf-engine-toast');
    var body = el('div', 'mf-up-body',
      'A new MindFlock engine (' + info.latest + ') is available \\u2014 you\\u2019re on '
      + info.current + '. This updates just the engine (a small download), not the app.');
    var log = el('pre', 'mf-up-log');
    log.hidden = true;
    var row = el('div', 'mf-up-row');
    var fix = el('button', 'mf-up-btn mf-up-primary', 'Update engine');
    var later = el('button', 'mf-up-btn', 'Later');
    later.addEventListener('click', function () { drop(c); window.mfengine.dismiss(); });

    var polling = null;
    function stopPolling() { if (polling) { clearInterval(polling); polling = null; } }
    fix.addEventListener('click', function () {
      if (fix.disabled) return;
      fix.disabled = true; fix.textContent = 'Updating\\u2026';
      log.hidden = false;
      window.mfengine.install().catch(function () {});
      polling = setInterval(function () {
        window.mfengine.installState().then(function (st) {
          if (!st) return;
          log.textContent = (st.lines || []).slice(-40).join('\\n');
          log.scrollTop = log.scrollHeight;
          if (st.state === 'done') {
            stopPolling();
            fix.textContent = 'Restarting\\u2026';
            body.textContent = 'The engine was updated \\u2014 restarting the server to load '
              + 'it. This window will reconnect on its own.';
            // Self-complete: stop the old server and let the launcher bring the
            // new one up, then reload. No "quit and reopen" step for the user.
            if (window.mfengine.restart) window.mfengine.restart().catch(function () {});
          } else if (st.state === 'failed') {
            stopPolling();
            fix.disabled = false;
            fix.textContent = 'Retry';
            body.textContent = 'The engine update didn\\u2019t finish (exit ' + st.code + '). '
              + 'The log above has the details.';
          }
        }).catch(function () {});
      }, 1000);
    });
    row.appendChild(fix); row.appendChild(later);
    c.appendChild(closeBtn(c, function () { stopPolling(); window.mfengine.dismiss(); }));
    c.appendChild(el('div', 'mf-up-title', 'Engine update available'));
    c.appendChild(body); c.appendChild(log); c.appendChild(row);
  }

  // Cover the case where the main process reached its verdict before this
  // page's listener existed (initial load, or a reload after the check fired).
  if (window.mfupdate) {
    window.mfupdate.onAvailable(show);
    window.mfupdate.get().then(function (info) { if (info) show(info); }).catch(function () {});
  }
  if (window.mfengine) {
    window.mfengine.onNotice(showEngine);
    window.mfengine.get().then(function (info) { if (info) showEngine(info); }).catch(function () {});
  }
})();
`

let win = null
let retryTimer = null
// Cold-start backoff: the server binds ~1-3s after spawn (venv + imports), so
// probe fast at first and ease out to RETRY_MS instead of always waiting a flat
// 2.5s per attempt — that flat wait was pure dead time at the start of a launch.
// Reset to RETRY_MIN_MS on a successful app load (did-finish-load below).
const RETRY_MIN_MS = 500
let retryDelay = RETRY_MIN_MS

function scheduleRetry() {
  if (retryTimer) clearTimeout(retryTimer)
  retryTimer = setTimeout(loadApp, retryDelay)
  retryDelay = Math.min(RETRY_MS, retryDelay * 2)
}

function loadApp() {
  if (retryTimer) { clearTimeout(retryTimer); retryTimer = null }
  win.loadURL(APP_URL).catch(() => {})
}

const OFFLINE_FILE = path.join(__dirname, 'offline.html')

// Show the offline page, but only if we aren't already on it. A failed
// loadURL never commits, so getURL() still reports the last good page -- which
// during an outage is offline.html itself. Reloading it on every 2.5s retry
// re-ran its script from scratch: the page flickered, and (now that it streams
// installer output) a multi-minute install would have its log wiped several
// times a minute. The page polls for its own state, so leaving it alone is
// both calmer and correct.
// `force` skips that check for the one case where the page really must be
// re-loaded even though it is already the current URL: a renderer that died
// while showing it, where the URL is still offline.html but nothing is alive
// to render it.
function showOffline(force) {
  const cur = win.webContents.getURL() || ''
  if (!force && cur.startsWith('file://') && cur.indexOf('offline.html') !== -1) return
  win.loadFile(OFFLINE_FILE).catch(() => {})
}

function injectChrome() {
  win.webContents.insertCSS(SCROLLBAR_CSS + TITLEBAR_CSS + UPDATE_CSS).catch(() => {})
  win.webContents.executeJavaScript(TITLEBAR_JS + UPDATE_JS).catch(() => {})
}

function createWindow() {
  win = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 900,
    minHeight: 600,
    // Custom title strip on every platform; native drag/resize still work.
    // macOS is the exception: `frame:false` there ALSO removes the traffic
    // lights, so the shell drew its own – □ ✕ on the right — the Windows
    // position, which is exactly backwards for a Mac user. `titleBarStyle:
    // 'hidden'` keeps the frame hidden but leaves the REAL red/yellow/green
    // buttons top-left, where they belong; TITLEBAR_JS then skips ours, and the
    // top bar reserves room for them (see TopBar.css `#topbar[data-mac]`).
    ...(process.platform === 'darwin'
      ? { titleBarStyle: 'hidden', trafficLightPosition: { x: 13, y: 13 } }
      : { frame: false }),
    backgroundColor: '#0f1117',
    title: DEV ? 'MindFlock (dev)' : 'MindFlock',
    icon: DEV_ICON || path.join(__dirname, 'icon.ico'),
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      // Forward dev mode to the renderer: the preload runs in a child process
      // that can't see main's argv/env, so pass an explicit marker it reads
      // (from process.argv) to expose window.mfshell.dev for the UI wordmark.
      additionalArguments: DEV ? ['--mindflock-dev'] : [],
      // Explicit security posture (don't rely on Electron defaults):
      // renderer gets no Node, and the preload bridge is the only surface.
      nodeIntegration: false,
      contextIsolation: true,
      // sandbox MUST stay false: preload.js uses the `clipboard` module for
      // the mfclip bridge (terminal copy/paste incl. image paste), and
      // sandboxed preloads only see contextBridge/ipcRenderer/nativeImage/
      // webFrame/webUtils — no clipboard. TODO: route clipboard through
      // ipcMain.handle in the main process, then flip this to true.
      sandbox: false,
      // Devtools are for developing the shell, not for users: hard-disabled
      // in packaged builds (no shortcut, no programmatic open). `npm start`
      // dev runs keep them.
      devTools: !app.isPackaged,
    },
  })

  logger.attachWindow(win)

  win.webContents.on('dom-ready', injectChrome)
  win.webContents.on('did-finish-load', () => {
    // Real app loaded (not the bundled offline page) -> the server answered;
    // reset the cold-start backoff so the next outage probes fast again.
    if (win.webContents.getURL().startsWith(APP_URL)) {
      retryDelay = RETRY_MIN_MS
      // The app page loading IS the proof the server is up, so this is the
      // cheapest reliable moment to compare engine and shell versions.
      checkEngineVersion().catch(() => {})
      // Re-push a verdict reached before this page's listener existed (a
      // reload, or the check winning the race against the renderer).
      pushEngineNotice()
    }
    console.log('[mindflock] loaded:', win.webContents.getURL())
  })
  win.webContents.on('before-input-event', (_e, input) => {
    if (input.type !== 'keyDown') return

    // Fullscreen toggle — F11 everywhere (the Windows/Linux standard) and
    // ⌃⌘F on macOS (the platform standard). Checked before the mod-key gate
    // below because F11 carries no modifier. setFullScreen works with our
    // frameless window; the custom title strip stays draggable in fullscreen.
    const fsMac = process.platform === 'darwin' && input.control && input.meta &&
      (input.key === 'f' || input.key === 'F')
    if (input.key === 'F11' || fsMac) {
      _e.preventDefault()
      win.setFullScreen(!win.isFullScreen())
      return
    }

    const mod = input.control || input.meta        // Ctrl (Win/Linux) or ⌘ (Mac)
    if (!mod) return

    // Ctrl+Shift+L opens the logs folder (support hook).
    if (input.shift && (input.key === 'L' || input.key === 'l')) {
      const dir = logger.paths().dir
      if (dir) shell.openPath(dir).catch(() => {})
      return
    }

    // Dev loop: Ctrl+Shift+R reloads the UI via loadApp (which also escapes
    // the offline page) — frontend/Python edits need no app rebuild, the
    // shell only freezes main/preload/offline. Explicit here so it doesn't
    // depend on Electron's default-menu accelerators surviving a frameless
    // window. Plain Ctrl+R belongs to the web layer's keymap, which reloads
    // only while no terminal pane is focused — inside a terminal it stays
    // bash reverse-i-search. No devtools shortcut on purpose: users
    // shouldn't be poking at the shell's internals.
    if (input.shift && (input.key === 'R' || input.key === 'r')) {
      _e.preventDefault()
      loadApp()
      return
    }
    // Ctrl+Shift+I: devtools in UNPACKAGED (npm start) dev runs only. In the
    // installed app devtools are hard-disabled via webPreferences anyway;
    // this guard just keeps the shortcut itself from existing for users.
    if (!app.isPackaged && input.shift && (input.key === 'I' || input.key === 'i')) {
      _e.preventDefault()
      win.webContents.toggleDevTools()
      return
    }

    // Native page zoom (centered — same as the built-in zoom, unlike CSS zoom).
    // Electron's default menu handles Ctrl+- / Ctrl+0 but its Ctrl+Plus zoom-in
    // accelerator is unreliable, so drive all of it here. Match input.code (the
    // physical key) so Ctrl++ (== Ctrl+Shift+=) zooms in just like Ctrl+=.
    // preventDefault stops the key reaching the page/terminal.
    const wc = win.webContents
    if (input.code === 'Equal' || input.code === 'NumpadAdd') {
      _e.preventDefault()
      wc.setZoomLevel(Math.min(wc.getZoomLevel() + 0.5, 5))
    } else if (input.code === 'Minus' || input.code === 'NumpadSubtract') {
      _e.preventDefault()
      wc.setZoomLevel(Math.max(wc.getZoomLevel() - 0.5, -3))
    } else if (input.code === 'Digit0' || input.code === 'Numpad0') {
      _e.preventDefault()
      wc.setZoomLevel(0)                            // reset to 100%
    }
  })
  win.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url)
    return { action: 'deny' }
  })
  // Navigation guard, mirroring the window-open handler above: the window may
  // only navigate within the local server's origin. Anything else (external
  // links, redirects, dragged files) is cancelled and http(s) URLs open in
  // the system browser instead. Main-process loadURL/loadFile calls (the app
  // itself, the offline page) don't emit will-navigate, so they're unaffected.
  win.webContents.on('will-navigate', (event, url) => {
    let dest = null
    try { dest = new URL(url) } catch (e) {}
    if (dest && APP_ORIGIN && dest.origin === APP_ORIGIN) return
    event.preventDefault()
    if (dest && (dest.protocol === 'http:' || dest.protocol === 'https:')) {
      shell.openExternal(url)
    }
  })
  // Server not up yet -> offline page, retry until it answers.
  win.webContents.on('did-fail-load', (_e, code, desc, _url, isMainFrame) => {
    if (!isMainFrame || code === -3 /* ABORTED */) return
    console.log('[mindflock] load failed', code, desc, '-> offline, retrying')
    showOffline()
    // Re-ensure the server on every failed load so a crash/never-started server
    // (e.g. after a WSL restart) gets relaunched instead of leaving the app
    // stuck on offline forever. Idempotent: it probes 8765 first and the spawn
    // cooldown suppresses duplicates while one is booting.
    startServerIfNeeded()
    // Warm the WSL/install diagnosis so the offline page's first poll has an
    // answer (it also re-polls; refreshDiag throttles itself via DIAG_STALE_MS).
    refreshDiag()
    scheduleRetry()
  })

  // Renderer crash / hang recovery. logger.attachWindow already writes the
  // FATAL/WARN line for these events; here we get the user back to a working
  // window: auto-reload, but at most 3 times per 5 minutes — a renderer that
  // keeps dying is a real bug, and a reload storm would just spin the CPU.
  // Past the cap we fall back to the offline page + retry loop that
  // did-fail-load already uses, so the window never sits on a dead renderer.
  const CRASH_WINDOW_MS = 5 * 60 * 1000
  const CRASH_MAX_RELOADS = 3
  let crashReloadTimes = []
  const recoverRenderer = (why) => {
    if (!win || win.isDestroyed()) return
    const now = Date.now()
    crashReloadTimes = crashReloadTimes.filter((t) => now - t < CRASH_WINDOW_MS)
    if (crashReloadTimes.length >= CRASH_MAX_RELOADS) {
      console.error('[mindflock]', why, '— reload cap reached (' + CRASH_MAX_RELOADS +
        ' per 5 min); showing offline page')
      showOffline(true)
      scheduleRetry()
      return
    }
    crashReloadTimes.push(now)
    console.error('[mindflock]', why, '— auto-reloading (' +
      crashReloadTimes.length + '/' + CRASH_MAX_RELOADS + ')')
    loadApp()
  }
  win.webContents.on('render-process-gone', (_e, details) => {
    const reason = details && details.reason
    if (reason === 'clean-exit') return   // normal teardown, not a crash
    recoverRenderer('renderer gone (' + (reason || 'unknown') + ')')
  })
  win.webContents.on('unresponsive', () => recoverRenderer('window unresponsive'))
  win.webContents.on('responsive', () =>
    console.log('[mindflock] window responsive again'))

  const sendMax = () => {
    if (win && !win.webContents.isDestroyed()) {
      win.webContents.send('maximized-changed', win.isMaximized())
    }
  }
  win.on('maximize', sendMax)
  win.on('unmaximize', sendMax)

  // macOS hides the traffic lights in fullscreen, so the top bar has to give
  // back the room it reserves for them (see TopBar's `data-mac-lights`) or the
  // menu sits behind a 78px hole.
  const sendFullScreen = () => {
    if (win && !win.webContents.isDestroyed()) {
      win.webContents.send('fullscreen-changed', win.isFullScreen())
    }
  }
  win.on('enter-full-screen', sendFullScreen)
  win.on('leave-full-screen', sendFullScreen)

  win.on('closed', () => { win = null })

  // The page sets document.title (with unread-badge counts), which otherwise
  // overrides the "(dev)" window title — keep the marker in alt-tab / taskbar.
  if (DEV) {
    win.on('page-title-updated', (e, t) => {
      e.preventDefault()
      win.setTitle(/\bdev\b/i.test(t) ? t : t + ' — dev')
    })
  }

  startServerIfNeeded()
  loadApp()
}

ipcMain.on('win:minimize', () => { if (win) win.minimize() })
ipcMain.on('win:toggle-maximize', () => {
  if (!win) return
  win.isMaximized() ? win.unmaximize() : win.maximize()
})
ipcMain.on('win:close', () => { if (win) win.close() })
// Current fullscreen state, pulled once when the top bar mounts. The
// 'fullscreen-changed' push above only carries TRANSITIONS, and a window that is
// already fullscreen when the page loads (or reloads — the offline-recovery path
// reloads on its own) never sees one, so it would hold the traffic-light gap
// open until the user toggled fullscreen. A pull can't race the renderer's
// listener registration the way a push at did-finish-load would.
ipcMain.handle('win:is-fullscreen', () => !!(win && win.isFullScreen()))

app.whenReady().then(() => {
  // Drop Electron's default application menu: it carries devtools
  // (Ctrl+Shift+I / F12) and reload (Ctrl+R) accelerators — the first is a
  // user-facing hole, the second steals bash reverse-i-search from the
  // terminal panes. Our own shortcuts (zoom, Ctrl+Shift+R reload,
  // Ctrl+Shift+L logs) are hand-rolled in before-input-event. macOS keeps a
  // minimal menu because Cmd+C/V/Q only work through menu roles there.
  if (process.platform === 'darwin') {
    Menu.setApplicationMenu(Menu.buildFromTemplate([
      { role: 'appMenu' }, { role: 'editMenu' }, { role: 'windowMenu' },
    ]))
  } else {
    Menu.setApplicationMenu(null)
  }
  const lp = logger.init(app)
  console.log('[mindflock] logs:', lp.file, '| WSL server log:', WSL_SERVER_LOG)
  // macOS draws the dock icon from the app bundle, not the BrowserWindow icon,
  // so set it explicitly for an unpackaged dev run to get the red "dev" badge.
  if (DEV && DEV_ICON && process.platform === 'darwin' && app.dock) {
    try { app.dock.setIcon(DEV_ICON) } catch (e) { /* best effort */ }
  }
  // After logger.init: this one logs whether it took, and a first dev run on a
  // fresh machine is exactly when that line is worth having in main.log.
  ensureDevToastName()
  createWindow()
  startUpdateChecks()
})
app.on('window-all-closed', () => app.quit())
app.on('activate', () => { if (!win) createWindow() })
