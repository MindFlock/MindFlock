'use strict'
// node --test: the pure updater helpers (electron/update.js) — the version
// compare every check uses, the engine/app skew verdict, and the one run
// state every update button polls.
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')

const { cmpVersion, versionSkew, skewNotice, runState } = require('../update')

test('cmpVersion compares numerically, ignores a v prefix and non-numeric tails', () => {
  assert.equal(cmpVersion('0.10.0', '0.9.9'), 1)
  assert.equal(cmpVersion('0.9.9', '0.10.0'), -1)
  assert.equal(cmpVersion('1.2', '1.2.0'), 0)
  assert.equal(cmpVersion('v0.7.5', '0.7.5'), 0)
  assert.equal(cmpVersion('0.7.5-rc1', '0.7.5'), 0)
  assert.equal(cmpVersion('', '0.1'), -1)
})

test('versionSkew: newer / older / same / unknown', () => {
  assert.equal(versionSkew('0.8.0', '0.7.5'), 'engine-newer')
  assert.equal(versionSkew('0.7.4', '0.7.5'), 'engine-older')
  assert.equal(versionSkew('0.7.5', '0.7.5'), 'same')
  assert.equal(versionSkew('', '0.7.5'), 'unknown')
  assert.equal(versionSkew('0.7.5', ''), 'unknown')
})

test('skewNotice is null unless the engine is ahead of the app', () => {
  assert.equal(skewNotice('0.7.4', '0.7.5', '0.7.5', ''), null)
  assert.equal(skewNotice('0.7.5', '0.7.5', '0.7.6', ''), null)
  assert.equal(skewNotice('', '0.7.5', '0.7.6', ''), null)
})

test('skewNotice: with an app release out the app toast carries it', () => {
  const n = skewNotice('0.8.0', '0.7.5', '0.8.0', '')
  assert.equal(n.kind, 'skew')
  assert.equal(n.appUpdate, true)
  assert.match(n.message, /newer than the app/)
  assert.match(n.message, /update the app/)
  assert.equal(n.engine, '0.8.0')
  assert.equal(n.app, '0.7.5')
})

test('skewNotice: no app release (or the one the user skipped) is a one-line notice', () => {
  const none = skewNotice('0.8.0', '0.7.5', '', '')
  assert.equal(none.appUpdate, false)
  assert.match(none.message, /newer than this app/)
  assert.match(none.message, /engine is left as it is/)
  // A skipped release doesn't count as "an app update is out".
  const skipped = skewNotice('0.8.0', '0.7.5', '0.8.0', '0.8.0')
  assert.equal(skipped.appUpdate, false)
  // A release the app is already on isn't an update either.
  assert.equal(skewNotice('0.8.0', '0.7.5', '0.7.5', '').appUpdate, false)
})

test('runState: the run wins, then the installer; done only once a step finished', () => {
  const idle = { state: 'idle', step: '', message: '' }
  const inst = { state: 'idle', code: null, lines: [] }
  assert.equal(runState(idle, inst).state, 'idle')
  assert.equal(runState({ state: 'running', step: 'engine', message: 'x' }, inst).state, 'running')
  assert.equal(runState({ state: 'failed', step: 'engine', message: 'x' }, inst).state, 'failed')
  assert.equal(runState({ state: 'idle', step: 'done', message: 'Engine 0.8.0 is running.' }, inst).state, 'done')
  // The first-run installer (no run) still reports through the same shape.
  assert.equal(runState(idle, { state: 'running', code: null, lines: ['a'] }).state, 'running')
  assert.equal(runState(idle, { state: 'failed', code: 7, lines: [] }).code, 7)
  const st = runState({ state: 'running', step: 'restart', message: 'Restarting…' }, { state: 'done', code: 0, lines: ['l1', 'l2'] })
  assert.deepEqual(st, { state: 'running', step: 'restart', message: 'Restarting…', code: 0, lines: ['l1', 'l2'] })
  assert.deepEqual(runState(undefined, undefined), { state: 'idle', step: '', message: '', code: null, lines: [] })
})

// --- wiring pins (main.js is not requirable: electron) ------------------------
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

test('main.js uses the shared helpers, not a second copy', () => {
  assert.match(mainJs, /const upd = require\('\.\/update'\)/)
  assert.match(mainJs, /const cmpVersion = upd\.cmpVersion/)
  assert.ok(!/function cmpVersion\(/.test(mainJs))
  assert.match(fn('async function checkEngineVersion'), /upd\.skewNotice\(current, app\.getVersion\(\), latest, skippedVersion\)/)
  assert.match(fn('function engineRunState'), /upd\.runState\(updateRun, install\)/)
})

test('every update button runs updateEverything; no second installer call site', () => {
  assert.match(mainJs, /ipcMain\.handle\('update:install', \(\) => updateEverything\(\{ engine: true, app: true \}\)\)/)
  assert.match(mainJs, /ipcMain\.handle\('engine:install', \(\) => updateEverything\(\{ engine: true, app: false \}\)\)/)
  assert.match(mainJs, /ipcMain\.handle\('engine:install-state', \(\) => engineRunState\(\)\)/)
  // startInstall is reached from the run (and the first-run offline page) only.
  const sites = mainJs.match(/startInstall\(/g) || []
  assert.deepEqual(sites.length, 3, 'definition + first-run + updateEverything')
  assert.ok(!/engineNotice \? engineNotice\.ref : INSTALL_REF/.test(mainJs))
  // The engine toast no longer restarts the server itself.
  const toast = mainJs.slice(mainJs.indexOf('function showEngine(info)'), mainJs.indexOf('// Cover the case where'))
  assert.ok(!/mfengine\.restart/.test(toast))
})

test('an engine-only update stops the server, waits for the port, then starts and reports', () => {
  const run = fn('async function updateEverything')
  const engine = run.indexOf('startInstall(plan.ref)')
  const stop = run.indexOf('await stopEngineServer()')
  const swap = run.indexOf('autoUpdater.quitAndInstall(true, true)')
  assert.ok(engine < stop && stop < swap)
  const restart = run.indexOf('await restartServer()', swap)
  assert.ok(restart > swap)
  assert.match(run, /await waitForEngine\(target, 90 \* 1000\)/)
  assert.match(run, /message: 'Engine ' \+ v \+ ' is running\.'/)
  const stopFn = fn('async function stopEngineServer')
  assert.match(stopFn, /spawnEngineShell\(KILL_SERVER\)/)
  assert.match(stopFn, /await portFree\(PORT\)/)
  const restartFn = fn('async function restartServer')
  assert.match(restartFn, /await stopEngineServer\(\)/)
  assert.ok(restartFn.indexOf('stopEngineServer') < restartFn.indexOf('startServerIfNeeded()'))
})

test('the skew guard never downgrades and never stays silent', () => {
  const plan = fn('async function enginePlan')
  assert.match(plan, /not downgraded/)
  const check = fn('async function checkEngineVersion')
  assert.match(check, /if \(skew\.appUpdate\) \{ pushUpdateToRenderer\(\); return \}/)
  assert.match(check, /if \(skewChanged\) pushEngineNotice\(\)/)
  // ...and a skipped app release never hides an engine-behind-latest toast
  assert.match(check, /fall through so that toast is\n/)
  // The app toast shows the reason; the renderer draws the one-liner.
  assert.match(mainJs, /skew: engineSkew && engineSkew\.appUpdate \? engineSkew\.message : ''/)
  assert.match(mainJs, /if \(info\.kind === 'skew'\) return showSkew\(info\);/)
  assert.ok(!/\(unsigned, no-auto-update\)/.test(mainJs))
})

test('update.js ships with the app', () => {
  const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'package.json'), 'utf8'))
  assert.ok(pkg.build.files.includes('update.js'))
})
