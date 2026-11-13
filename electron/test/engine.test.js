'use strict'
// `node --test electron/test` — which engine the Windows app drives, and how
// it runs a native one (electron/engine.js), with the OS injected.
const test = require('node:test')
const assert = require('node:assert/strict')
const eng = require('../engine')

const HOME = 'C:\\Users\\me'
const EXE = 'C:\\Users\\me\\.local\\bin\\mindflock.exe'
const decide = (o) => eng.decideEngine(Object.assign({ platform: 'win32', env: {} }, o))

test('Linux and macOS are untouched: always posix', () => {
  for (const platform of ['linux', 'darwin']) {
    assert.equal(eng.decideEngine({ platform, env: { MINDFLOCK_DESKTOP_ENGINE: 'wsl' } }).engine, 'posix')
  }
})

test('a new Windows install goes native', () => {
  assert.deepEqual(decide({}), { engine: 'native', why: 'new install' })
})

test('an existing WSL user keeps WSL until a native engine exists', () => {
  assert.equal(decide({ wslHistory: true }).engine, 'wsl')
  assert.equal(decide({ wslHistory: true, nativePath: EXE }).engine, 'native')
})

test('the setting wins over everything, then the saved choice', () => {
  assert.equal(decide({ env: { MINDFLOCK_DESKTOP_ENGINE: 'wsl' }, nativePath: EXE }).engine, 'wsl')
  assert.equal(decide({ env: { MINDFLOCK_DESKTOP_ENGINE: ' Native ' }, wslHistory: true }).engine, 'native')
  assert.equal(decide({ saved: 'wsl', nativePath: EXE }).engine, 'wsl')
  assert.equal(decide({ env: { MINDFLOCK_DESKTOP_ENGINE: 'native' }, saved: 'wsl' }).engine, 'native')
  // A typo is no choice at all.
  assert.equal(decide({ env: { MINDFLOCK_DESKTOP_ENGINE: 'wls' }, wslHistory: true }).engine, 'wsl')
})

test('a developer checkout picks its side by its path', () => {
  assert.equal(decide({ env: { MINDFLOCK_REPO: 'C:\\src\\mindflock' } }).engine, 'native')
  assert.equal(decide({ env: { MINDFLOCK_REPO: '/home/me/MindFlock/app' } }).engine, 'wsl')
  assert.equal(decide({ env: { MINDFLOCK_REPO: '/home/me/app' }, nativePath: EXE }).engine, 'wsl')
  assert.equal(decide({ env: { MINDFLOCK_REPO: '\\\\wsl.localhost\\Ubuntu\\home\\me\\app' } }).engine, 'wsl')
  assert.equal(decide({ env: { MINDFLOCK_REPO: '\\\\wsl$\\Ubuntu\\home\\me' } }).engine, 'wsl')
  assert.equal(decide({ env: { MINDFLOCK_REPO: '\\\\nas\\src\\mf' } }).engine, 'native')
})

test('engine.json: only native|wsl count', () => {
  assert.equal(eng.readSavedChoice('{"engine":"wsl"}'), 'wsl')
  assert.equal(eng.readSavedChoice('{"engine":"NATIVE"}'), 'native')
  assert.equal(eng.readSavedChoice('{"engine":"tmux"}'), '')
  assert.equal(eng.readSavedChoice('not json'), '')
  assert.equal(eng.readSavedChoice(null), '')
})

test('the native engine is found in the uv tool dirs, then by where', () => {
  const at = (...paths) => (p) => paths.includes(p)
  assert.equal(eng.findNativeEngine({ env: {}, home: HOME, exists: at(EXE) }), EXE)
  const custom = 'D:\\tools\\bin\\mindflock.exe'
  assert.equal(eng.findNativeEngine({
    env: { UV_TOOL_BIN_DIR: 'D:\\tools\\bin' }, home: HOME, exists: at(EXE, custom),
  }), custom)
  const where = () => 'C:\\Users\\me\\AppData\\Local\\Microsoft\\WindowsApps\\mindflock.exe\r\nC:\\x\\mindflock.exe\r\n'
  assert.equal(eng.findNativeEngine({ env: {}, home: HOME, exists: at(), where }), 'C:\\x\\mindflock.exe')
  assert.equal(eng.findNativeEngine({ env: {}, home: HOME, exists: at(), where: () => { throw new Error('no') } }), '')
  assert.equal(eng.findNativeEngine({ env: {}, home: HOME, exists: at() }), '')
})

test("where never picks the desktop app's own MindFlock.exe", () => {
  // The friend's install: no engine yet, `where` run from the app's folder.
  const SELF = 'C:\\Users\\me\\AppData\\Local\\Programs\\MindFlock\\MindFlock.exe'
  const only = (out) => () => out
  assert.equal(eng.findNativeEngine({ env: {}, home: HOME, exists: () => false, self: SELF,
    where: only(SELF + '\r\n') }), '')
  // Case differs (where prints the file's real name; execPath may not match).
  assert.equal(eng.findNativeEngine({ env: {}, home: HOME, exists: () => false, self: SELF,
    where: only(SELF.toLowerCase() + '\r\n') }), '')
  // Anything in the app's folder, then the real engine after it.
  assert.equal(eng.findNativeEngine({ env: {}, home: HOME, exists: () => false, self: SELF,
    where: only(SELF.replace('MindFlock.exe', 'mindflock.cmd') + '\r\nC:\\x\\mindflock.exe\r\n') }),
  'C:\\x\\mindflock.exe')
  // Another copy of the desktop app on PATH: an Electron folder (app.asar).
  const OTHER = 'D:\\Apps\\MindFlock\\MindFlock.exe'
  const asar = (p) => p === 'D:\\Apps\\MindFlock\\resources\\app.asar'
  assert.equal(eng.findNativeEngine({ env: {}, home: HOME, exists: asar, self: SELF,
    where: only(OTHER + '\r\n') }), '')
})

test('serve: the installed CLI from home, or a checkout on the URL port', () => {
  assert.deepEqual(eng.nativeServeCommand({ exe: EXE, port: 8765, home: HOME }),
    { file: EXE, args: ['serve'], cwd: HOME })
  assert.deepEqual(eng.nativeServeCommand({ repo: 'C:\\src\\mf', port: 9000, home: HOME, exists: () => true }), {
    file: 'C:\\src\\mf\\.venv\\Scripts\\python.exe',
    args: ['C:\\src\\mf\\backend\\web\\run.py', 'local', '9000'],
    cwd: 'C:\\src\\mf',
  })
  assert.equal(eng.nativeServeCommand({ repo: 'C:\\src\\mf', port: 1, exists: () => false }), null)
  assert.equal(eng.nativeServeCommand({ exe: '', port: 8765, home: HOME }), null)
  assert.deepEqual(eng.nativeTokenCommand({ exe: EXE }), { file: EXE, args: ['token'] })
})

test("the server's environment: tool bin first, HOME, UTF-8 — the caller's kept", () => {
  const env = eng.nativeServerEnv({ Path: 'C:\\Windows', FOO: '1' }, HOME)
  assert.equal(env.Path, 'C:\\Users\\me\\.local\\bin;C:\\Windows')
  assert.equal(env.HOME, HOME)
  assert.equal(env.PYTHONIOENCODING, 'utf-8')
  assert.equal(env.FOO, '1')
  const kept = eng.nativeServerEnv({ PATH: 'x', HOME: 'H:\\', UV_TOOL_BIN_DIR: 'D:\\b' }, HOME)
  assert.equal(kept.PATH, 'D:\\b;C:\\Users\\me\\.local\\bin;x')
  assert.equal(kept.HOME, 'H:\\')
})

test('killing the server: the tree, forcibly; and the pkill twin by command line', () => {
  assert.deepEqual(eng.taskkillArgs(4242), ['/T', '/F', '/PID', '4242'])
  const ps = eng.killServerScript()
  const re = new RegExp(/-match '([^']+)'/.exec(ps)[1])
  assert.ok(re.test('"C:\\Users\\me\\.local\\bin\\mindflock.exe" serve'))
  assert.ok(re.test('C:\\py\\python.exe C:\\tools\\mindflock\\Scripts\\mindflock serve'))
  assert.ok(re.test('C:\\src\\.venv\\Scripts\\python.exe backend\\web\\run.py local 8765'))
  assert.ok(!re.test('python.exe -m backend.session.winmux.host'))  // sessions survive
  assert.ok(!re.test('mindflock.exe doctor'))
})

test('PowerShell argv: a file, or an encoded script with no quoting to survive', () => {
  assert.deepEqual(eng.powershellFileArgs('C:\\a b\\install.ps1'),
    ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', 'C:\\a b\\install.ps1'])
  const args = eng.powershellEncodedArgs("Write-Host 'hi \"there\"'")
  assert.equal(args[args.length - 2], '-EncodedCommand')
  assert.match(args[args.length - 1], /^[A-Za-z0-9+/=]+$/)
  assert.equal(Buffer.from(args[args.length - 1], 'base64').toString('utf16le'), "Write-Host 'hi \"there\"'")
  assert.equal(eng.installCommandPs('v0.12.1'),
    'irm https://raw.githubusercontent.com/MindFlock/MindFlock/v0.12.1/install.ps1 | iex')
})
