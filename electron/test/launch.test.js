'use strict'
// electron/launch.js: launch flags, the per-profile dev identity (AUMID +
// Start-menu shortcut), and the bootstrap's server guard — then the main.js
// wiring that uses them.
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const net = require('node:net')
const path = require('node:path')
const { spawnSync } = require('node:child_process')

const l = require('../launch')

const mainJs = fs.readFileSync(path.join(__dirname, '..', 'main.js'), 'utf8')

function fn (name) {
  const start = mainJs.indexOf(name)
  assert.ok(start >= 0, name)
  let depth = 0
  for (let k = mainJs.indexOf('{', start); k < mainJs.length; k++) {
    if (mainJs[k] === '{') depth++
    else if (mainJs[k] === '}' && --depth === 0) return mainJs.slice(start, k + 1)
  }
  throw new Error(name)
}

// What main.js composes (its base literals + the module's suffixes).
const aumid = (profile) => 'ai.mindflock.desktop.dev' + l.devAumidSuffix(profile)
const lnk = (profile) => 'MindFlock-dev' + l.devNameSuffix(profile) + '.lnk'

const WSL_APP = '\\\\wsl.localhost\\Ubuntu\\home\\emandel2630\\MindFlock\\app\\electron'
const WIN_APP = 'C:\\Users\\emand\\MindFlock-win\\electron'

// --- launch flags -------------------------------------------------------------

test('--mindflock-repo= sets MINDFLOCK_REPO, beside the other three flags', () => {
  const env = l.applyLaunchFlags(['electron.exe', 'app', '--mindflock-dev',
    '--mindflock-url=http://localhost:9000', '--mindflock-engine=wsl',
    '--mindflock-profile=MindFlock (native)', '--mindflock-repo=/home/u/app'], {})
  assert.deepEqual(env, {
    MINDFLOCK_URL: 'http://localhost:9000',
    MINDFLOCK_DESKTOP_ENGINE: 'wsl',
    MINDFLOCK_DEV_PROFILE: 'MindFlock (native)',
    MINDFLOCK_REPO: '/home/u/app',
  })
  assert.deepEqual(l.LAUNCH_FLAGS.map(([f]) => f),
    ['--mindflock-url=', '--mindflock-engine=', '--mindflock-profile=', '--mindflock-repo='])
})

test('a flag wins over an inherited variable; an absent flag leaves it alone', () => {
  const env = l.applyLaunchFlags(['--mindflock-repo=C:\\src\\app'],
    { MINDFLOCK_REPO: '/home/other', MINDFLOCK_URL: 'http://localhost:9000' })
  assert.equal(env.MINDFLOCK_REPO, 'C:\\src\\app')
  assert.equal(env.MINDFLOCK_URL, 'http://localhost:9000')
})

// --- one identity per dev profile ---------------------------------------------

test('the default profile keeps the pre-profile AUMID and shortcut (existing pins)', () => {
  for (const p of ['MindFlock (dev)', '', '  ', undefined, ' MindFlock (dev) ']) {
    assert.equal(aumid(p), 'ai.mindflock.desktop.dev', String(p))
    assert.equal(lnk(p), 'MindFlock-dev.lnk', String(p))
  }
  assert.equal(l.devProfile({}), 'MindFlock (dev)')
  assert.equal(l.devProfile({ MINDFLOCK_DEV_PROFILE: '  ' }), 'MindFlock (dev)')
  assert.equal(l.devProfile({ MINDFLOCK_DEV_PROFILE: 'MindFlock (native)' }), 'MindFlock (native)')
})

test('the native profile gets its own AUMID and its own shortcut', () => {
  assert.equal(aumid('MindFlock (native)'), 'ai.mindflock.desktop.dev.native')
  assert.equal(lnk('MindFlock (native)'), 'MindFlock-dev (native).lnk')
})

test('any other profile: a stable, distinct, filesystem-safe identity', () => {
  assert.equal(aumid('Work Box'), 'ai.mindflock.desktop.dev.work-box')
  assert.equal(lnk('Work Box'), 'MindFlock-dev (Work Box).lnk')
  assert.equal(aumid('My: Profile?.'), 'ai.mindflock.desktop.dev.my-profile')
  assert.equal(lnk('My: Profile?.'), 'MindFlock-dev (My Profile).lnk')
  // Nothing alphanumeric left: still an id, stable across launches, and
  // different for different names.
  const a = aumid('???'); const b = aumid('!!!')
  assert.match(a, /^ai\.mindflock\.desktop\.dev\.p[0-9a-f]{8}$/)
  assert.equal(a, aumid('???'))
  assert.notEqual(a, b)
  assert.match(lnk('???'), /^MindFlock-dev \(p[0-9a-f]{8}\)\.lnk$/)
  for (const p of ['a<b>c', 'x/y\\z', 'q"uote|pipe*', 'tab\there', 'trail. ']) {
    const name = lnk(p)
    assert.ok(!/[<>:"/\\|?*\x00-\x1f]/.test(name), name)
    assert.ok(!/[. ]\)\.lnk$/.test(name), name)
    assert.notEqual(aumid(p), 'ai.mindflock.desktop.dev', p)
  }
})

// --- the shortcut records the whole launch ------------------------------------

test('the WSL dev shell (run-dev.bat env) records its URL and repo', () => {
  const env = l.applyLaunchFlags(['electron.exe', WSL_APP, '--mindflock-dev'], {
    MINDFLOCK_URL: 'http://localhost:9000',
    MINDFLOCK_REPO: '/home/emandel2630/MindFlock/app',
    PATH: 'C:\\Windows',
  })
  assert.equal(l.devShortcutArgs(WSL_APP, '--mindflock-dev', env),
    '"\\\\wsl.localhost\\Ubuntu\\home\\emandel2630\\MindFlock\\app\\electron" --mindflock-dev'
    + ' --mindflock-url=http://localhost:9000 --mindflock-repo=/home/emandel2630/MindFlock/app')
})

test('the native dev shell records its flags, quoting the one with a space', () => {
  const env = l.applyLaunchFlags(['electron.exe', WIN_APP, '--mindflock-dev',
    '--mindflock-url=http://127.0.0.1:8766', '--mindflock-engine=native',
    '--mindflock-profile=MindFlock (native)'], {})
  assert.equal(l.devShortcutArgs(WIN_APP, '--mindflock-dev', env),
    '"C:\\Users\\emand\\MindFlock-win\\electron" --mindflock-dev'
    + ' --mindflock-url=http://127.0.0.1:8766 --mindflock-engine=native'
    + ' "--mindflock-profile=MindFlock (native)"')
})

test('no launch flags or env: the args are exactly what they always were', () => {
  assert.equal(l.devShortcutArgs(WSL_APP, '--mindflock-dev', { PATH: 'x', MINDFLOCK_URL: '  ' }),
    '"' + WSL_APP + '" --mindflock-dev')
})

// CommandLineToArgvW, enough of it to round-trip what quoteWinArg writes.
function argvW (cmd) {
  const out = []; let cur = ''; let inQ = false; let have = false; let i = 0
  while (i < cmd.length) {
    const c = cmd[i]
    if (c === '\\') {
      let n = 0; while (cmd[i] === '\\') { n++; i++ }
      if (cmd[i] === '"') { cur += '\\'.repeat(n >> 1); if (n & 1) { cur += '"'; i++ } } else cur += '\\'.repeat(n)
      have = true; continue
    }
    if (c === '"') { inQ = !inQ; have = true; i++; continue }
    if (!inQ && (c === ' ' || c === '\t')) { if (have) { out.push(cur); cur = ''; have = false } i++; continue }
    cur += c; have = true; i++
  }
  if (have) out.push(cur)
  return out
}

test('quoting survives Windows command-line parsing', () => {
  for (const s of ['plain', 'has space', 'say "hi"', 'C:\\dir with space\\', 'a\\"b', '', '\\\\srv\\share\\x']) {
    assert.deepEqual(argvW(l.quoteWinArg(s)), [s], s)
    assert.deepEqual(argvW(l.quoteWinArg(s, true)), [s], s)
  }
  const env = { MINDFLOCK_REPO: 'C:\\My Repos\\app\\', MINDFLOCK_DEV_PROFILE: 'He said "x"' }
  assert.deepEqual(argvW(l.devShortcutArgs('C:\\a b\\electron', '--mindflock-dev', env)), [
    'C:\\a b\\electron', '--mindflock-dev',
    '--mindflock-profile=He said "x"', '--mindflock-repo=C:\\My Repos\\app\\',
  ])
})

// --- main.js wiring -----------------------------------------------------------

test('main.js folds the flags in through the shared table, before reading env', () => {
  assert.match(mainJs, /const lnch = require\('\.\/launch'\)/)
  assert.match(mainJs, /lnch\.applyLaunchFlags\(process\.argv, process\.env\)/)
  assert.ok(!mainJs.includes("['--mindflock-url=', 'MINDFLOCK_URL']"), 'no second flag table')
  assert.ok(mainJs.indexOf('lnch.applyLaunchFlags(') < mainJs.indexOf('const DEV ='))
  assert.ok(mainJs.indexOf('lnch.applyLaunchFlags(') < mainJs.indexOf('const WSL_REPO ='))
})

test('main.js: one AUMID and one shortcut per dev profile', () => {
  assert.match(mainJs, /const DEV_PROFILE = lnch\.devProfile\(process\.env\)/)
  assert.match(mainJs, /const DEV_AUMID = 'ai\.mindflock\.desktop\.dev' \+ lnch\.devAumidSuffix\(DEV_PROFILE\)/)
  assert.match(mainJs, /const DEV_TOAST_NAME = 'MindFlock-dev' \+ lnch\.devNameSuffix\(DEV_PROFILE\)/)
  assert.match(mainJs, /app\.setAppUserModelId\(DEV_AUMID\)/)
  assert.match(mainJs, /app\.setPath\('userData', path\.join\(app\.getPath\('appData'\), DEV_PROFILE\)\)/)
  const body = fn('function ensureDevToastName')
  assert.match(body, /if \(!DEV \|\| process\.platform !== 'win32'\) return/)
  assert.match(body, /DEV_TOAST_NAME \+ '\.lnk'/)
  assert.match(body, /appUserModelId: DEV_AUMID/)
  assert.match(body, /args: lnch\.devShortcutArgs\(app\.getAppPath\(\), '--mindflock-dev', process\.env\)/)
  assert.match(body, /have\.args === want\.args/, 'a changed launch rewrites the shortcut')
  // Packaged/prod runs still take the installer's id.
  assert.match(mainJs, /app\.setAppUserModelId\(app\.isPackaged \? 'ai\.mindflock\.desktop' : process\.execPath\)/)
})

test('the packaged app ships launch.js', () => {
  const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'package.json'), 'utf8'))
  assert.ok(pkg.build.files.includes('launch.js'))
})

// --- the bootstrap's server guard ---------------------------------------------

const SYSTEMD = '"$HOME/.config/systemd/user/default.target.wants/mindflock.service"'

test('installed mode: the guard is byte-for-byte the old systemd-then-port check', () => {
  for (const nativeHost of [false, true]) {
    const launch = 'exec serve'
    assert.equal(l.serverGuard({ port: 8765, devRepo: '', launch, nativeHost }),
      ' if [ -e ' + SYSTEMD + ' ];'
      + ' then echo "systemd mindflock.service owns port 8765; not launching a second server.";'
      + ' elif (exec 3<>/dev/tcp/127.0.0.1/8765) 2>/dev/null; then echo "a server is already listening on port 8765; not launching another.";'
      + (nativeHost ? ' else ' + launch + '; fi;' : ' else (' + launch + ') & fi;'))
  }
})

test('a developer checkout skips the systemd check but keeps the port check', () => {
  const g = l.serverGuard({ port: 9000, devRepo: '/home/u/app', launch: 'exec serve', nativeHost: false })
  assert.ok(!g.includes('systemd'), g)
  assert.ok(!g.includes('elif'), g)
  assert.equal(g, ' if (exec 3<>/dev/tcp/127.0.0.1/9000) 2>/dev/null;'
    + ' then echo "a server is already listening on port 9000; not launching another.";'
    + ' else (exec serve) & fi;')
})

test('main.js builds its bootstrap with the guard, keyed on WSL_REPO', () => {
  const body = fn('function startServerIfNeeded')
  assert.match(body, /lnch\.serverGuard\(\{ port: PORT, devRepo: WSL_REPO, launch, nativeHost: NATIVE_HOST \}\)/)
  assert.ok(!body.includes('default.target.wants'), 'one copy of the check, in launch.js')
})

const bash = (() => {
  const r = spawnSync('bash', ['-c', 'echo ok'], { encoding: 'utf8' })
  return r.status === 0 ? 'bash' : null
})()

// The script main.js wraps the guard in, minus the keepalive.
const wrap = (guard) => '{ echo banner;' + guard + ' } 2>&1;'

test('every guard parses: if/elif/else/fi balanced in both modes', { skip: !bash && 'no bash' }, () => {
  for (const devRepo of ['', '/home/u/app']) {
    for (const nativeHost of [false, true]) {
      const g = l.serverGuard({ port: 9000, devRepo, launch: 'cd /tmp && exec true', nativeHost })
      assert.equal((g.match(/\bif\b/g) || []).length, (g.match(/\bfi\b/g) || []).length, g)
      const r = spawnSync('bash', ['-n', '-c', wrap(g)], { encoding: 'utf8' })
      assert.equal(r.status, 0, g + '\n' + r.stderr)
    }
  }
})

test('the guard does what it says, run for real', { skip: !bash && 'no bash' }, async () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'mf-guard-'))
  const wants = path.join(home, '.config', 'systemd', 'user', 'default.target.wants')
  fs.mkdirSync(wants, { recursive: true })
  fs.writeFileSync(path.join(wants, 'mindflock.service'), '')
  const free = await new Promise((resolve) => {
    const s = net.createServer().listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => resolve(p)) })
  })
  const run = (port, devRepo) => spawnSync('bash',
    ['-c', wrap(l.serverGuard({ port, devRepo, launch: 'echo LAUNCHED', nativeHost: true }))],
    { encoding: 'utf8', env: { ...process.env, HOME: home } }).stdout
  try {
    // The reported bug: an enabled unit (prod, :8765) wedged the dev server
    // on :9000 off for good.
    assert.match(run(free, '/home/u/app'), /LAUNCHED/)
    // Installed mode keeps the 2026-07-14 protection.
    const prod = run(free, '')
    assert.match(prod, /systemd mindflock.service owns port/)
    assert.doesNotMatch(prod, /LAUNCHED/)
    // Something on the port: neither mode launches a second copy.
    const srv = net.createServer().listen(0, '127.0.0.1')
    await new Promise((resolve) => srv.on('listening', resolve))
    try {
      const busy = run(srv.address().port, '/home/u/app')
      assert.match(busy, /already listening/)
      assert.doesNotMatch(busy, /LAUNCHED/)
    } finally { srv.close() }
  } finally {
    fs.rmSync(home, { recursive: true, force: true })
  }
})
