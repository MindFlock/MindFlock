'use strict'
// Which engine the Windows desktop app drives, and how it runs a NATIVE one —
// pure helpers (no Electron, the OS injected), shared with electron/test.
//
// On Windows the engine is either native (docs/windows.md: `mindflock serve`
// on Windows itself, sessions in the MindFlock session host) or the older one
// inside WSL2. The rule (decideEngine):
//   1. MINDFLOCK_DESKTOP_ENGINE=native|wsl wins — the explicit setting;
//   2. then the saved choice in <userData>/engine.json ({"engine": "wsl"});
//   3. a developer checkout (MINDFLOCK_REPO) is native when it is a Windows
//      path (C:\…) and WSL when it is a Linux one (/home/…);
//   4. a native engine installed (mindflock.exe) -> native;
//   5. this app drove a WSL engine before (its WSL launcher files are in
//      userData) -> WSL: an existing WSL user keeps their engine;
//   6. otherwise — a new install — native.
// Linux and macOS are always 'posix' (the server runs natively, as before).

const path = require('path')

const ENGINES = ['native', 'wsl']

// What this app writes into userData while it drives a WSL engine: any of
// them means an earlier version already ran one here.
const WSL_TRACES = ['start-server.vbs', 'install-engine.vbs', 'probe-wsl.vbs', 'read-token.vbs']

function pick (value) {
  const v = String(value || '').trim().toLowerCase()
  return ENGINES.includes(v) ? v : ''
}

// The saved choice in engine.json's text ('' when absent or unreadable).
function readSavedChoice (text) {
  try { return pick(JSON.parse(String(text || '')).engine) } catch (e) { return '' }
}

// A Windows path (C:\x, C:/x, \\server\share) rather than a Linux one. A
// WSL share (\\wsl.localhost\<distro>\…, \\wsl$\…) is a Linux checkout.
function isWindowsPath (p) {
  const s = String(p || '')
  if (/^\\\\wsl(\.localhost|\$)\\/i.test(s)) return false
  return /^[a-zA-Z]:[\\/]/.test(s) || /^\\\\/.test(s)
}

// Where `uv tool install` puts mindflock.exe: UV_TOOL_BIN_DIR, else
// ~\.local\bin (uv's default on Windows, where install.ps1 looks too).
function nativeEngineCandidates (env, home) {
  const out = []
  if (env.UV_TOOL_BIN_DIR) out.push(path.win32.join(env.UV_TOOL_BIN_DIR, 'mindflock.exe'))
  if (env.XDG_BIN_HOME) out.push(path.win32.join(env.XDG_BIN_HOME, 'mindflock.exe'))
  out.push(path.win32.join(home, '.local', 'bin', 'mindflock.exe'))
  return out
}

// A desktop app's own exe, never the engine: Windows names are
// case-insensitive, so `where mindflock` matches the app's MindFlock.exe (the
// current folder comes first, and the shortcut starts the app in its install
// folder). Spawning that as the server only opens a second app window, which
// quits on the single-instance lock, so the server never answers. Matched as
// this app's own exe/folder (`self`), or any folder holding an Electron
// app (resources\app.asar): another copy of the desktop app on PATH.
function isDesktopApp (file, self, exists) {
  const dir = path.win32.dirname(file).toLowerCase()
  if (self) {
    if (file.toLowerCase() === self.toLowerCase()) return true
    if (dir === path.win32.dirname(self).toLowerCase()) return true
  }
  try {
    return !!(exists && exists(path.win32.join(path.win32.dirname(file), 'resources', 'app.asar')))
  } catch (e) { return false }
}

// The native mindflock.exe, or '' — the uv tool dirs first, then `where`
// (a function returning `where mindflock`'s stdout; it may throw). `self` is
// the running app's exe (process.execPath), which `where` must not return.
function findNativeEngine ({ env, home, exists, where, self }) {
  for (const p of nativeEngineCandidates(env || {}, home)) {
    try { if (exists(p)) return p } catch (e) { /* unreadable: next */ }
  }
  let out = ''
  try { out = where ? String(where() || '') : '' } catch (e) { out = '' }
  const line = out.split(/\r?\n/).map((l) => l.trim())
    .find((l) => /\.(exe|cmd|bat)$/i.test(l) && !/\\WindowsApps\\/i.test(l)
      && !isDesktopApp(l, self, exists))
  return line || ''
}

function decideEngine ({ platform, env, saved, nativePath, wslHistory }) {
  env = env || {}
  if (platform !== 'win32') return { engine: 'posix', why: platform }
  const forced = pick(env.MINDFLOCK_DESKTOP_ENGINE)
  if (forced) return { engine: forced, why: 'MINDFLOCK_DESKTOP_ENGINE=' + forced }
  const kept = pick(saved)
  if (kept) return { engine: kept, why: 'saved choice (engine.json)' }
  if (env.MINDFLOCK_REPO) {
    return isWindowsPath(env.MINDFLOCK_REPO)
      ? { engine: 'native', why: 'developer checkout on Windows (MINDFLOCK_REPO)' }
      : { engine: 'wsl', why: 'developer checkout in WSL (MINDFLOCK_REPO)' }
  }
  if (nativePath) return { engine: 'native', why: 'native engine installed (' + nativePath + ')' }
  if (wslHistory) return { engine: 'wsl', why: 'this app ran a WSL engine before' }
  return { engine: 'native', why: 'new install' }
}

// How to start the native server: a developer checkout's venv + run.py on
// the URL's port (forcing `local`, as the WSL dev mode does), else the
// installed `mindflock serve` from the user's home. null when neither exists.
function nativeServeCommand ({ exe, repo, port, home, exists }) {
  if (repo) {
    const py = path.win32.join(repo, '.venv', 'Scripts', 'python.exe')
    if (exists && !exists(py)) return null
    return {
      file: py,
      args: [path.win32.join(repo, 'backend', 'web', 'run.py'), 'local', String(port)],
      cwd: repo,
    }
  }
  if (!exe) return null
  return { file: exe, args: ['serve'], cwd: home }
}

// `mindflock token` for the native engine (same lookup as the server).
function nativeTokenCommand ({ exe, repo }) {
  if (repo) {
    return {
      file: path.win32.join(repo, '.venv', 'Scripts', 'python.exe'),
      args: ['-m', 'backend.cli', 'token'],
      cwd: repo,
    }
  }
  return exe ? { file: exe, args: ['token'] } : null
}

// The server's environment: the uv tool bin dir on PATH first (the agent
// CLIs install there too — claude.exe), HOME set (the engine's config dir
// honours it; cmd-launched apps have none), and UTF-8 output (mindflock
// prints check marks; a cp1252 log pipe would refuse them).
function nativeServerEnv (env, home) {
  const out = Object.assign({}, env)
  const bin = path.win32.join(home, '.local', 'bin')
  const key = Object.keys(out).find((k) => k.toLowerCase() === 'path') || 'Path'
  const dirs = [env.UV_TOOL_BIN_DIR, bin].filter(Boolean)
  out[key] = dirs.concat(out[key] ? [out[key]] : []).join(';')
  if (!out.HOME) out.HOME = home
  if (!out.PYTHONIOENCODING) out.PYTHONIOENCODING = 'utf-8'
  return out
}

// Is the native engine whole? A failed update on Windows once left the tool
// venv half-deleted (2026-11-13: no Lib, `mindflock.exe --version` ->
// "ModuleNotFoundError: No module named 'backend'") while the old server kept
// serving from memory. `mindflock --version` is the cheap probe: it starts the
// engine's own python from its venv and imports backend. A developer
// checkout's venv python imports the package instead. null: nothing to probe.
function engineHealthCommand ({ exe, repo }) {
  if (repo) {
    return {
      file: path.win32.join(repo, '.venv', 'Scripts', 'python.exe'),
      args: ['-c', 'import backend.cli'],
      cwd: repo,
    }
  }
  return exe ? { file: exe, args: ['--version'] } : null
}

// The probe's verdict: '' when the engine answered, else one line saying
// why not (its last output line — the ImportError — or the exit code). A
// probe that timed out or couldn't be spawned says nothing (`code` null):
// "broken" sends the person to a reinstall, so it must be a real answer.
function engineHealthVerdict (code, output) {
  if (code === 0 || code == null) return ''
  const last = String(output || '').split(/\r?\n/).map((l) => l.trim()).filter(Boolean).pop()
  return last || ('it exited with code ' + code)
}

// taskkill's arguments for a whole process tree, forcibly.
function taskkillArgs (pid) {
  return ['/T', '/F', '/PID', String(pid)]
}

// PowerShell that ends every MindFlock server on this machine — `pkill -f
// 'mindflock serve'` + `pkill -f backend/web/run.py` (main.js KILL_SERVER).
// The session host (backend.session.winmux) is not matched: sessions outlive
// the server, as they outlive tmux's clients.
function killServerScript () {
  return "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "
    + "'mindflock(\\.exe)?\"?\\s+serve|backend[\\\\/]web[\\\\/]run\\.py' } | "
    + 'ForEach-Object { taskkill /T /F /PID $_.ProcessId 2>$null | Out-Null }'
}

// powershell.exe's arguments to run a script file (install.ps1) unattended.
function powershellFileArgs (file) {
  return ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', file]
}

// powershell.exe's arguments to run an inline script with no quoting to
// survive (Windows command-line rules): UTF-16LE base64, -EncodedCommand.
function powershellEncodedArgs (script) {
  const b64 = Buffer.from(String(script), 'utf16le').toString('base64')
  return ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', b64]
}

// The PowerShell one-liner the offline page offers as the manual escape hatch.
function installCommandPs (ref) {
  return 'irm https://raw.githubusercontent.com/MindFlock/MindFlock/'
    + (ref || 'main') + '/install.ps1 | iex'
}

module.exports = {
  ENGINES,
  WSL_TRACES,
  readSavedChoice,
  isWindowsPath,
  nativeEngineCandidates,
  findNativeEngine,
  decideEngine,
  nativeServeCommand,
  nativeTokenCommand,
  engineHealthCommand,
  engineHealthVerdict,
  nativeServerEnv,
  taskkillArgs,
  killServerScript,
  powershellFileArgs,
  powershellEncodedArgs,
  installCommandPs,
}
