'use strict'
// node --test: the packaged-app update path's failure handling (the
// desktop-update audit, docs/desktop-updates.md). The pure parts run; the
// main.js glue is pinned by shape, like update.test.js does.
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')

const { cmpVersion, safeDownloadUrl, autoUpdateVerdict } = require('../update')

const mainJs = fs.readFileSync(path.join(__dirname, '..', 'main.js'), 'utf8')
function fn (name) {
  const start = mainJs.indexOf(name)
  assert.ok(start !== -1, name)
  let depth = 0
  for (let k = mainJs.indexOf('{', start); k < mainJs.length; k++) {
    if (mainJs[k] === '{') depth++
    else if (mainJs[k] === '}' && --depth === 0) return mainJs.slice(start, k + 1)
  }
  throw new Error(name)
}

test('a prerelease or build suffix never ranks above its release', () => {
  // Split on dots first, '0.15.0-beta.1' used to read as 0.15.0.1 — newer
  // than 0.15.0, so a beta build would never take the release.
  assert.equal(cmpVersion('0.15.0-beta.1', '0.15.0'), 0)
  assert.equal(cmpVersion('0.15.0', '0.15.0-rc.2'), 0)
  assert.equal(cmpVersion('1.0.0+build.5', '1.0.0'), 0)
  assert.equal(cmpVersion('0.15.0', '0.14.99-rc.9'), 1)
  assert.equal(cmpVersion(' v0.14.25 ', '0.14.24'), 1)
  // The engine's dev builds keep their extra numeric part.
  assert.equal(cmpVersion('0.14.26.dev0', '0.14.25'), 1)
  assert.equal(cmpVersion('0.14.25', '0.14.25.0'), 0)
})

test('the update chain picks N, never N-1, across a two-digit minor/patch', () => {
  // The versions the release feed and the engine report.
  const seen = ['0.9.9', '0.14.9', '0.14.24', '0.14.25', '0.14.3', 'v0.14.25']
  const newest = seen.reduce((a, b) => (cmpVersion(b, a) > 0 ? b : a))
  assert.equal(cmpVersion(newest, '0.14.25'), 0)
  assert.equal(cmpVersion('0.14.10', '0.14.9'), 1)
})

test('the download page is only ever a web page', () => {
  const fb = 'https://github.com/MindFlock/MindFlock/releases/latest'
  assert.equal(safeDownloadUrl('https://github.com/MindFlock/MindFlock/releases/tag/v0.14.25', fb),
    'https://github.com/MindFlock/MindFlock/releases/tag/v0.14.25')
  for (const bad of ['file:///C:/Windows/System32/calc.exe', 'ms-settings:', 'javascript:alert(1)',
    'smb://host/share/x.exe', '', null, undefined, 'not a url', 42]) {
    assert.equal(safeDownloadUrl(bad, fb), fb, String(bad))
  }
  // Both shell.openExternal calls of the update path go through it.
  assert.match(mainJs, /ipcMain\.on\('update:open'[\s\S]{0,400}upd\.safeDownloadUrl\(url, UPDATE_RELEASES_URL\)/)
  assert.match(fn('async function updateEverything'), /shell\.openExternal\(upd\.safeDownloadUrl\(/)
})

test('a failed background download is caught (no FATAL unhandledRejection in main.log)', () => {
  const body = fn('function checkAppUpdate')
  assert.match(body, /r\.downloadPromise\.catch\(/)
})

test('an errored app download is retried once on click, never by the automatic run', () => {
  const run = fn('async function updateEverything')
  const retry = run.indexOf("appUpd.state === 'error' && !want.auto")
  const wait = run.indexOf('updateRun.step = \'download\'')
  assert.ok(retry !== -1 && retry < wait)
  // And a check nobody answers stops the wait well before the 20 min deadline.
  assert.match(run, /APP_CHECK_SILENT_MS/)
  // The automatic run never starts on an errored app (rule 4), so the retry
  // above can't loop: pinned here too.
  const v = autoUpdateVerdict({
    enabled: true, appCurrent: '0.14.24', release: '0.14.25', appState: 'error',
    appVersion: '0.14.25', idleSec: 9999,
  })
  assert.equal(v.go, false)
})

test('quitAndInstall that does not quit is a failed run, not "Restarting…" forever', () => {
  const run = fn('async function updateEverything')
  const release = run.indexOf('app.releaseSingleInstanceLock()')
  const watchdog = run.indexOf('swapStalled()')
  assert.match(run, /swapPending = \{ engineUpdated \}/)
  const swap = run.indexOf('autoUpdater.quitAndInstall(true, true)')
  assert.ok(release !== -1 && release < swap, 'the lock is let go before the swap (AppImage relaunch)')
  assert.ok(watchdog !== -1 && watchdog < swap)
  const stalled = fn('async function swapStalled')
  assert.match(stalled, /!pending \|\| updateRun\.state !== 'running' \|\| updateRun\.step !== 'restart'/)
  // An updater error during the swap (macOS: Squirrel checks the signature
  // only now) reports at once, not at the timeout — which is longer on macOS.
  assert.match(fn("autoUpdater.on('error'"), /if \(swapPending\) setTimeout\(\(\) => \{ swapStalled\(\)/)
  assert.match(mainJs, /const SWAP_STALL_MS = \(process\.platform === 'darwin' \? 180 : 30\) \* 1000/)
  assert.match(stalled, /app\.requestSingleInstanceLock\(\)/)
  // The server stopped for the swap comes back.
  assert.match(stalled, /if \(engineUpdated && await portFree\(PORT\)\) await restartServer\(\)/)
  assert.match(stalled, /state: 'failed'/)
  assert.match(stalled, /writeUpdateStore\(\{ updated: undefined \}\)/)
})

test('a failed engine update on Linux/macOS says the engine was kept', () => {
  const body = fn('async function installEngineForUpdate')
  assert.match(body, /Your engine was left as it was and keeps running/)
  assert.ok(!/may be partially updated/.test(body))
  // install.sh carries the guarantee the message makes.
  const sh = fs.readFileSync(path.join(__dirname, '..', '..', 'install.sh'), 'utf8')
  assert.match(sh, /uv_install_engine\(\) \{/)
  assert.match(sh, /UV_TOOL_DIR="\$STAGE\/tools" UV_TOOL_BIN_DIR="\$STAGE\/bin"/)
})

test('the toast lets the person click again when the app could not install itself', () => {
  const show = mainJs.slice(mainJs.indexOf('function show(info)'), mainJs.indexOf('// --- what the last update did'))
  assert.match(show, /upd\.disabled = run\.step === 'done'/)
})
