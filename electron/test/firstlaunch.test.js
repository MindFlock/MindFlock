'use strict'
// `node --test electron/test` — the first-launch helpers, with the OS injected.
const test = require('node:test')
const assert = require('node:assert/strict')
const fl = require('../firstlaunch')

// ---------------------------------------------------------------- log tail
test('tailLines keeps the last N non-blank lines, stripped of ANSI and CR', () => {
  const text = 'one\r\n\x1b[31mtwo\x1b[0m\n\n   \nthree   \rfour\n'
  assert.deepEqual(fl.tailLines(text, 10), ['one', 'two', 'three', 'four'])
  assert.deepEqual(fl.tailLines(text, 2), ['three', 'four'])
  assert.deepEqual(fl.tailLines('', 5), [])
  assert.deepEqual(fl.tailLines(null, 5), [])
})

test('tailLines defaults to 15 lines', () => {
  const lines = Array.from({ length: 40 }, (_, i) => 'line ' + i)
  const out = fl.tailLines(lines.join('\n'))
  assert.equal(out.length, 15)
  assert.equal(out[0], 'line 25')
  assert.equal(out[14], 'line 39')
})

// A fake fs that serves one file from a Buffer with the sync fd API readLogTail uses.
function fsOf(files) {
  const fds = new Map()
  let next = 3
  return {
    openSync (p) {
      if (!(p in files)) { const e = new Error('ENOENT'); e.code = 'ENOENT'; throw e }
      fds.set(next, Buffer.from(files[p], 'utf8'))
      return next++
    },
    fstatSync (fd) { return { size: fds.get(fd).length } },
    readSync (fd, buf, off, len, pos) { return fds.get(fd).copy(buf, off, pos, pos + len) },
    closeSync (fd) { fds.delete(fd) },
    existsSync (p) { return p in files },
    readFileSync (p) {
      if (!(p in files)) { const e = new Error('ENOENT'); e.code = 'ENOENT'; throw e }
      return files[p]
    },
    writeFileSync (p, data) { files[p] = String(data) },
  }
}

test('readLogTail returns the last lines of a log and [] for a missing file', () => {
  const fs = fsOf({ '/l/server.log': '===== boot =====\nimporting\nTraceback\nValueError: boom\n' })
  assert.deepEqual(fl.readLogTail(fs, '/l/server.log', 2), ['Traceback', 'ValueError: boom'])
  assert.deepEqual(fl.readLogTail(fs, '/l/server.log', 15),
    ['===== boot =====', 'importing', 'Traceback', 'ValueError: boom'])
  assert.deepEqual(fl.readLogTail(fs, '/l/missing.log', 15), [])
})

test('readLogTail reads only the end of a big log and drops the cut first line', () => {
  const big = Array.from({ length: 5000 }, (_, i) => 'row-' + i + ' ' + 'x'.repeat(40)).join('\n') + '\n'
  const fs = fsOf({ '/big.log': big })
  const out = fl.readLogTail(fs, '/big.log', 3, 1024)
  assert.equal(out.length, 3)
  assert.equal(out[2], 'row-4999 ' + 'x'.repeat(40))
  // every returned line is a whole line (none starts mid-row)
  for (const l of out) assert.match(l, /^row-\d+ x+$/)
})

// --------------------------------------------------- start-state selection
test('resolveStartView passes any non-starting probe verdict through, without a tail', () => {
  const dead = { startedAt: 0, answered: false, exited: { code: 1, signal: null, at: 0 } }
  assert.deepEqual(fl.resolveStartView('not-installed', dead, 99999), { state: 'not-installed', showTail: false })
  assert.deepEqual(fl.resolveStartView('wsl-down', dead, 99999), { state: 'wsl-down', showTail: false })
  assert.deepEqual(fl.resolveStartView('checking', null, 99999), { state: 'checking', showTail: false })
})

test('resolveStartView: a quiet boot shows the log tail only after 10 s', () => {
  const t0 = 1000000
  const start = { startedAt: t0, answered: false, exited: null }
  assert.deepEqual(fl.resolveStartView('starting', start, t0 + 3000), { state: 'starting', showTail: false })
  assert.deepEqual(fl.resolveStartView('starting', start, t0 + fl.TAIL_AFTER_MS), { state: 'starting', showTail: true })
  // nothing spawned yet (startedAt 0): no tail to show
  assert.deepEqual(fl.resolveStartView('starting', { startedAt: 0 }, t0 + 60000), { state: 'starting', showTail: false })
})

test('resolveStartView: a process that exited nonzero before answering is failed-to-start', () => {
  const t0 = 1000000
  const start = { startedAt: t0, answered: false, exited: { code: 1, signal: null, at: t0 + 800 } }
  assert.deepEqual(fl.resolveStartView('starting', start, t0 + 900), { state: 'failed-to-start', showTail: true })
  const killed = { startedAt: t0, answered: false, exited: { code: null, signal: 'SIGSEGV', at: t0 + 800 } }
  assert.equal(fl.resolveStartView('starting', killed, t0 + 900).state, 'failed-to-start')
})

test('resolveStartView: a clean exit gets a grace period, then counts as failed', () => {
  const t0 = 1000000
  const start = { startedAt: t0, answered: false, exited: { code: 0, signal: null, at: t0 + 500 } }
  assert.equal(fl.resolveStartView('starting', start, t0 + 1000).state, 'starting')
  assert.equal(fl.resolveStartView('starting', start, t0 + 500 + fl.CLEAN_EXIT_GRACE_MS).state, 'failed-to-start')
})

test('resolveStartView: an exit AFTER the port answered is never a failure', () => {
  const t0 = 1000000
  const start = { startedAt: t0, answered: true, exited: { code: 1, signal: null, at: t0 + 500 } }
  assert.deepEqual(fl.resolveStartView('starting', start, t0 + 600), { state: 'starting', showTail: false })
})

// ------------------------------------------------------- macOS preflight
test('findBrew checks the Apple-silicon prefix, then Intel, else null', () => {
  assert.equal(fl.findBrew(fsOf({ '/opt/homebrew/bin/brew': '' })), '/opt/homebrew/bin/brew')
  assert.equal(fl.findBrew(fsOf({ '/usr/local/bin/brew': '' })), '/usr/local/bin/brew')
  assert.equal(fl.findBrew(fsOf({ '/opt/homebrew/bin/brew': '', '/usr/local/bin/brew': '' })), '/opt/homebrew/bin/brew')
  assert.equal(fl.findBrew(fsOf({})), null)
  assert.equal(fl.findBrew({ existsSync () { throw new Error('EACCES') } }), null)
})

test('hasXcodeCLT is true only when `xcode-select -p` exits 0', () => {
  const calls = []
  const exec = (cmd, args) => { calls.push([cmd, args]); return 0 }
  assert.equal(fl.hasXcodeCLT(exec), true)
  assert.deepEqual(calls, [['xcode-select', ['-p']]])
  assert.equal(fl.hasXcodeCLT(() => 2), false)
  assert.equal(fl.hasXcodeCLT(() => null), false)
  assert.equal(fl.hasXcodeCLT(() => { throw new Error('ENOENT') }), false)
})

// Manual timers: setTimeout queues, `fire()` runs the next one; `clock` is now().
function fakeTimers() {
  let clock = 0
  const queue = []
  return {
    now: () => clock,
    setTimeout: (fn, ms) => { const t = { fn, at: clock + ms }; queue.push(t); return t },
    clearTimeout: (t) => { const i = queue.indexOf(t); if (i >= 0) queue.splice(i, 1) },
    fire () { const t = queue.shift(); if (!t) return false; clock = t.at; t.fn(); return true },
    pending: () => queue.length,
  }
}

test('pollUntil re-checks on the interval and resolves true when the check passes', async () => {
  const t = fakeTimers()
  let n = 0
  const exec = () => (++n >= 3 ? 0 : 69)   // CLT appears on the third poll
  const p = fl.pollUntil(() => fl.hasXcodeCLT(exec), { intervalMs: 5000, timeoutMs: 60000, ...t })
  assert.equal(n, 1)                 // checked once immediately
  assert.equal(t.pending(), 1)
  t.fire(); assert.equal(n, 2)
  t.fire(); assert.equal(n, 3)
  assert.equal(await p, true)
  assert.equal(t.pending(), 0)       // nothing left scheduled
  assert.equal(t.now(), 10000)       // two intervals of 5 s
})

test('pollUntil resolves false at the timeout and on stop()', async () => {
  const t = fakeTimers()
  const p = fl.pollUntil(() => false, { intervalMs: 5000, timeoutMs: 12000, ...t })
  t.fire(); t.fire(); t.fire()       // 5 s, 10 s, 15 s ≥ 12 s → give up
  assert.equal(await p, false)
  assert.equal(t.pending(), 0)

  const t2 = fakeTimers()
  const p2 = fl.pollUntil(() => false, { intervalMs: 5000, timeoutMs: 60000, ...t2 })
  p2.stop()
  assert.equal(await p2, false)
  assert.equal(t2.pending(), 0)      // the pending timer was cleared
})

test('pollUntil treats a throwing check as "not yet"', async () => {
  const t = fakeTimers()
  let n = 0
  const p = fl.pollUntil(() => { if (++n < 2) throw new Error('no'); return true },
    { intervalMs: 1000, timeoutMs: 60000, ...t })
  t.fire()
  assert.equal(await p, true)
})

// ------------------------------------------------- first-launch hand-off
test('first-launch marker: absent → false, written → true, garbage → false', () => {
  const fs = fsOf({})
  assert.equal(fl.readFirstLaunchDone(fs, '/u/first-launch.json'), false)
  assert.equal(fl.writeFirstLaunchDone(fs, '/u/first-launch.json', 0), true)
  assert.equal(fl.readFirstLaunchDone(fs, '/u/first-launch.json'), true)
  const o = JSON.parse(fs.readFileSync('/u/first-launch.json'))
  assert.equal(o.firstLaunchDone, true)
  assert.equal(o.at, '1970-01-01T00:00:00.000Z')
  fs.writeFileSync('/u/first-launch.json', 'not json')
  assert.equal(fl.readFirstLaunchDone(fs, '/u/first-launch.json'), false)
  assert.equal(fl.writeFirstLaunchDone({ writeFileSync () { throw new Error('EROFS') } }, '/x'), false)
})

test('setupQuery hands off with ?setup=1 only on the first-run install of a first launch', () => {
  assert.deepEqual(fl.setupQuery(true, false), { setup: '1' })
  assert.deepEqual(fl.setupQuery(true, true), {})
  assert.deepEqual(fl.setupQuery(false, false), {})
  assert.deepEqual(fl.setupQuery(false, true), {})
})

// ------------------------------------------------------------ misc text
test('installCommand pins the ref (and falls back to main)', () => {
  assert.equal(fl.installCommand('v0.7.5'),
    'curl -LsSf https://raw.githubusercontent.com/MindFlock/MindFlock/v0.7.5/install.sh | sh')
  assert.match(fl.installCommand(''), /\/main\/install\.sh \| sh$/)
})

test('formatDiagnostics carries the probe, the exit, both logs and the tails', () => {
  const text = fl.formatDiagnostics({
    version: '0.7.5', platform: 'darwin', arch: 'arm64', now: 0, appUrl: 'http://localhost:8765',
    diagState: 'starting', diagDetail: '',
    start: { startedAt: 1000, answered: false, exited: { code: 1, signal: null, at: 2000 } },
    tail: ['Traceback', 'boom'], logPath: '/Users/x/.mindflock/desktop-server.log',
    mainLog: '/Users/x/Library/Logs/main.log', installLines: ['=== installing ==='],
  })
  for (const needle of ['0.7.5 on darwin/arm64', 'probe: starting', 'server exited: code 1',
    '/Users/x/.mindflock/desktop-server.log', 'main.log', 'Traceback\nboom', '=== installing ===']) {
    assert.ok(text.includes(needle), 'missing ' + JSON.stringify(needle) + ' in\n' + text)
  }
  assert.ok(fl.formatDiagnostics({}).includes('(no log yet)'))
})
