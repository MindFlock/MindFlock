'use strict'
// First-launch helpers for the desktop shell: pure functions main.js calls
// and `node --test electron/test` exercises without Electron. Everything that
// touches the OS (fs, child processes, timers) is injected so the tests can
// stand in for a Mac without Homebrew, a server that died on boot, or a
// Command Line Tools install that finishes on the third poll.

const ANSI_RE = /\x1b\[[0-9;]*[A-Za-z]/g

// How long "Starting…" may stay silent before the page shows the server log.
const TAIL_AFTER_MS = 10000
// A bootstrap that exits 0 before the port answers is usually benign (another
// server already owned the port and the script just said so) — give the port
// this long to prove it before calling the start failed.
const CLEAN_EXIT_GRACE_MS = 4000
const TAIL_LINES = 15

// The manual install one-liner, pinned to the given ref (the app's own version
// tag in a packaged build, `main` for an unpackaged dev run). Used by the
// bootstrap's "not installed" log line and the offline page's escape hatch, so
// the two can't drift again.
function installCommand (ref) {
  return 'curl -LsSf https://raw.githubusercontent.com/MindFlock/MindFlock/'
    + (ref || 'main') + '/install.sh | sh'
}

// Last `n` non-blank lines of a log, ANSI colour codes stripped, CR/LF
// normalised, trailing whitespace trimmed. Boot banners are kept: the date
// line tells the reader WHICH start they are looking at.
function tailLines (text, n) {
  n = n || TAIL_LINES
  const lines = String(text || '')
    .replace(ANSI_RE, '')
    .replace(/\r\n?/g, '\n')
    .split('\n')
    .map((l) => l.trimEnd())
    .filter((l) => l !== '')
  return lines.slice(-n)
}

// Read the tail of a possibly large log without loading all of it. `fsLike`
// needs openSync/fstatSync/readSync/closeSync (node's fs does). Returns [] when
// the file is missing or unreadable — "no log yet" is a normal state.
function readLogTail (fsLike, file, n, maxBytes) {
  maxBytes = maxBytes || 64 * 1024
  let fd = null
  try {
    fd = fsLike.openSync(file, 'r')
    const size = fsLike.fstatSync(fd).size
    const start = Math.max(0, size - maxBytes)
    const buf = Buffer.alloc(size - start)
    fsLike.readSync(fd, buf, 0, buf.length, start)
    let text = buf.toString('utf8')
    // Drop a partial first line when we started mid-file.
    if (start > 0) text = text.slice(text.indexOf('\n') + 1)
    return tailLines(text, n)
  } catch (e) {
    return []
  } finally {
    if (fd != null) { try { fsLike.closeSync(fd) } catch (e) {} }
  }
}

// Which view the offline page shows while nothing answers on the port, from
// what main.js knows about the server process it spawned.
//
//   diagState   the environment probe's verdict ('starting', 'not-installed', …)
//   start       { startedAt, answered, exited: null | { code, signal, at } }
//   now         Date.now() (injected for tests)
//
// Returns { state, showTail }:
//   * anything but 'starting' is the probe's business — passed through, no tail
//     (a missing engine must read "install", never "the server crashed").
//   * the spawned process exited before the port ever answered → failed-to-start,
//     at once for a nonzero exit, after a short grace for a clean one.
//   * otherwise 'starting', with the log tail once it has been quiet for a bit.
function resolveStartView (diagState, start, now) {
  if (diagState !== 'starting') return { state: diagState, showTail: false }
  const s = start || {}
  now = now == null ? Date.now() : now
  if (s.exited && !s.answered) {
    const clean = s.exited.code === 0 && !s.exited.signal
    if (!clean || now - (s.exited.at || 0) >= CLEAN_EXIT_GRACE_MS) {
      return { state: 'failed-to-start', showTail: true }
    }
  }
  const since = s.startedAt || 0
  return { state: 'starting', showTail: since > 0 && now - since >= TAIL_AFTER_MS }
}

// Homebrew's two install prefixes (Apple silicon, then Intel). `fsLike` needs
// existsSync. Returns the brew binary's path, or null.
const BREW_PATHS = ['/opt/homebrew/bin/brew', '/usr/local/bin/brew']
function findBrew (fsLike, paths) {
  for (const p of (paths || BREW_PATHS)) {
    try { if (fsLike.existsSync(p)) return p } catch (e) { /* unreadable: treat as absent */ }
  }
  return null
}

// Apple's Command Line Tools are present when `xcode-select -p` exits 0.
// `exec(cmd, args)` returns the exit status (or throws); spawnSync-shaped.
function hasXcodeCLT (exec) {
  try { return exec('xcode-select', ['-p']) === 0 } catch (e) { return false }
}

// Poll `check()` every `intervalMs` until it returns true (resolve true) or
// `timeoutMs` passes (resolve false). `stop()` on the returned promise cancels
// the poll (resolves false). Timers are injectable for the tests.
function pollUntil (check, opts) {
  opts = opts || {}
  const intervalMs = opts.intervalMs || 5000
  const timeoutMs = opts.timeoutMs || 30 * 60 * 1000
  const now = opts.now || Date.now
  const setT = opts.setTimeout || setTimeout
  const clearT = opts.clearTimeout || clearTimeout
  let timer = null
  let done = false
  let resolveFn = null
  const p = new Promise((resolve) => { resolveFn = resolve })
  const finish = (v) => {
    if (done) return
    done = true
    if (timer) { clearT(timer); timer = null }
    resolveFn(v)
  }
  const since = now()
  const step = () => {
    if (done) return
    let ok = false
    try { ok = !!check() } catch (e) { ok = false }
    if (ok) return finish(true)
    if (now() - since >= timeoutMs) return finish(false)
    timer = setT(step, intervalMs)
  }
  step()
  p.stop = () => finish(false)
  return p
}

// The first-launch marker: once the app page has loaded, later launches open
// `/` plain instead of the setup hand-off. One small JSON file in userData.
function readFirstLaunchDone (fsLike, file) {
  try {
    const o = JSON.parse(fsLike.readFileSync(file, 'utf8'))
    return !!(o && o.firstLaunchDone)
  } catch (e) {
    return false
  }
}

function writeFirstLaunchDone (fsLike, file, now) {
  try {
    fsLike.writeFileSync(file, JSON.stringify({
      firstLaunchDone: true,
      at: new Date(now == null ? Date.now() : now).toISOString(),
    }) + '\n')
    return true
  } catch (e) {
    return false
  }
}

// The query the app opens with. The first-run hand-off (`?setup=1`, which
// opens the web UI's setup checklist) happens exactly once: after an in-app
// engine install succeeded on a launch that never reached the app before.
function setupQuery (firstRunInstall, firstLaunchDone) {
  return firstRunInstall && !firstLaunchDone ? { setup: '1' } : {}
}

// Plain-text diagnostics for the "Copy diagnostics" button.
function formatDiagnostics (info) {
  info = info || {}
  const out = []
  out.push('MindFlock desktop diagnostics')
  out.push('app: ' + (info.version || '?') + ' on ' + (info.platform || '?')
    + (info.arch ? '/' + info.arch : ''))
  out.push('time: ' + new Date(info.now == null ? Date.now() : info.now).toISOString())
  out.push('url: ' + (info.appUrl || ''))
  out.push('probe: ' + (info.diagState || '?') + (info.diagDetail ? ' — ' + info.diagDetail : ''))
  const s = info.start || {}
  if (s.startedAt) {
    out.push('server spawned: ' + new Date(s.startedAt).toISOString())
    if (s.exited) {
      out.push('server exited: code ' + s.exited.code
        + (s.exited.signal ? ' signal ' + s.exited.signal : '')
        + ' at ' + new Date(s.exited.at).toISOString())
    } else {
      out.push('server exited: no' + (s.answered ? ' (answered on the port)' : ''))
    }
  }
  if (info.logPath) out.push('server log: ' + info.logPath)
  if (info.mainLog) out.push('app log: ' + info.mainLog)
  const tail = info.tail || []
  out.push('')
  out.push('--- server log (last ' + tail.length + ' lines) ---')
  out.push.apply(out, tail.length ? tail : ['(no log yet)'])
  const inst = info.installLines || []
  if (inst.length) {
    out.push('')
    out.push('--- engine install (last ' + inst.length + ' lines) ---')
    out.push.apply(out, inst)
  }
  return out.join('\n') + '\n'
}

module.exports = {
  TAIL_AFTER_MS,
  CLEAN_EXIT_GRACE_MS,
  TAIL_LINES,
  BREW_PATHS,
  installCommand,
  tailLines,
  readLogTail,
  resolveStartView,
  findBrew,
  hasXcodeCLT,
  pollUntil,
  readFirstLaunchDone,
  writeFirstLaunchDone,
  setupQuery,
  formatDiagnostics,
}
