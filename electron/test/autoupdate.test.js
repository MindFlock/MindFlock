'use strict'
// node --test: automatic updates. The pure "install now?" verdict
// (electron/update.js autoUpdateVerdict) and its helpers, then the main.js
// wiring that keeps a run from source from ever offering an update.
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')

const {
  autoUpdateVerdict, workingCount, updatedNotice, AUTO_IDLE_SEC, AUTO_WORKING_CAP_MS,
} = require('../update')

const MIN = 60
// An app release fully downloaded, the person away 15 min, nothing working.
const ready = (o) => Object.assign({
  source: false, enabled: true, running: false,
  appCurrent: '0.8.0', appState: 'ready', appVersion: '0.8.1', release: '0.8.1',
  engineCurrent: '', engineLatest: '',
  skipped: '', tried: [],
  idleSec: 15 * MIN, locked: false, hiddenSec: 0,
  working: 0, waitedMs: 0,
}, o || {})
// The app current, the engine one release behind.
const engineOnly = (o) => ready(Object.assign({
  appState: 'idle', appVersion: '', release: '',
  appCurrent: '0.8.1', engineCurrent: '0.8.0', engineLatest: '0.8.1',
}, o || {}))

test('defaults: away = 10 min idle, wait for working sessions at most 2 h', () => {
  assert.equal(AUTO_IDLE_SEC, 600)
  assert.equal(AUTO_WORKING_CAP_MS, 2 * 60 * 60 * 1000)
})

test('downloaded + away + nothing working -> install the app (engine rides along)', () => {
  const v = autoUpdateVerdict(ready())
  assert.equal(v.go, true)
  assert.equal(v.kind, 'app')
  assert.equal(v.version, '0.8.1')
})

test('a run from source never auto-updates anything', () => {
  assert.equal(autoUpdateVerdict(ready({ source: true })).go, false)
  assert.equal(autoUpdateVerdict(engineOnly({ source: true })).go, false)
  assert.match(autoUpdateVerdict(ready({ source: true })).why, /from source/)
})

test('turned off -> never', () => {
  assert.equal(autoUpdateVerdict(ready({ enabled: false })).go, false)
  assert.equal(autoUpdateVerdict(engineOnly({ enabled: false })).go, false)
})

test('the skipped version -> never (app or engine)', () => {
  const v = autoUpdateVerdict(ready({ skipped: '0.8.1' }))
  assert.equal(v.go, false)
  assert.match(v.why, /skipped/)
  assert.equal(autoUpdateVerdict(engineOnly({ skipped: '0.8.1' })).go, false)
  // Skipping an OLDER release doesn't hold a newer one back.
  assert.equal(autoUpdateVerdict(ready({ skipped: '0.8.0' })).go, true)
})

test('an update already running, or this version already tried -> no', () => {
  assert.equal(autoUpdateVerdict(ready({ running: true })).go, false)
  const v = autoUpdateVerdict(ready({ tried: ['0.8.1'] }))
  assert.equal(v.go, false)
  assert.match(v.why, /already tried/)
})

test('the person is active -> no; idle long enough or locked -> yes', () => {
  assert.equal(autoUpdateVerdict(ready({ idleSec: 30 })).go, false)
  assert.match(autoUpdateVerdict(ready({ idleSec: 30 })).why, /active/)
  assert.equal(autoUpdateVerdict(ready({ idleSec: 9 * MIN + 59 })).go, false)
  assert.equal(autoUpdateVerdict(ready({ idleSec: 10 * MIN })).go, true)
  assert.equal(autoUpdateVerdict(ready({ idleSec: 0, locked: true })).go, true)
  // A caller-chosen threshold wins.
  assert.equal(autoUpdateVerdict(ready({ idleSec: 2 * MIN, idleNeedSec: MIN })).go, true)
})

test('a hidden window counts as away for the engine, never for an app relaunch', () => {
  // The app swap relaunches the window over whatever the person is doing.
  assert.equal(autoUpdateVerdict(ready({ idleSec: 5, hiddenSec: 30 * MIN })).go, false)
  // The engine restart doesn't touch the window.
  assert.equal(autoUpdateVerdict(engineOnly({ idleSec: 5, hiddenSec: 30 * MIN })).go, true)
  assert.equal(autoUpdateVerdict(engineOnly({ idleSec: 5, hiddenSec: 2 * MIN })).go, false)
})

test('sessions working -> wait; past the cap -> go anyway', () => {
  const w = autoUpdateVerdict(ready({ working: 2 }))
  assert.equal(w.go, false)
  assert.equal(w.wait, true)
  assert.match(w.why, /2 sessions are working/)
  assert.match(autoUpdateVerdict(ready({ working: 1 })).why, /1 session is working/)
  assert.equal(autoUpdateVerdict(ready({ working: 2, waitedMs: AUTO_WORKING_CAP_MS - 1 })).go, false)
  const capped = autoUpdateVerdict(ready({ working: 2, waitedMs: AUTO_WORKING_CAP_MS }))
  assert.equal(capped.go, true)
  assert.match(capped.why, /after the cap/)
  // Unknown (server unreadable) doesn't block.
  assert.equal(autoUpdateVerdict(ready({ working: null })).go, true)
  // Only an otherwise-ready update "waits" (so the cap clock only runs then).
  assert.equal(autoUpdateVerdict(ready({ working: 2, idleSec: 0 })).wait, false)
})

test('an app release not downloaded yet, or one the updater cannot apply, waits for the person', () => {
  const dl = autoUpdateVerdict(ready({ appState: 'downloading' }))
  assert.equal(dl.go, false)
  assert.match(dl.why, /not downloaded/)
  const err = autoUpdateVerdict(ready({ appState: 'error' }))
  assert.equal(err.go, false)
  assert.match(err.why, /can't update itself/)
  // ...and the engine is NOT updated alone meanwhile (it would run ahead).
  assert.equal(autoUpdateVerdict(ready({ appState: 'error', engineCurrent: '0.8.0', engineLatest: '0.8.1' })).go, false)
  // A release seen on GitHub but no updater at all (state off) waits too.
  assert.equal(autoUpdateVerdict(ready({ appState: 'off', appVersion: '' })).go, false)
})

test('engine-only: the app is current, the engine behind -> engine update', () => {
  const v = autoUpdateVerdict(engineOnly())
  assert.equal(v.go, true)
  assert.equal(v.kind, 'engine')
  assert.equal(v.version, '0.8.1')
  assert.equal(autoUpdateVerdict(engineOnly({ engineLatest: '0.8.0' })).go, false)
  assert.match(autoUpdateVerdict(engineOnly({ engineLatest: '' })).why, /nothing to update/)
  // Never past the app: an engine release newer than this app waits for the app.
  assert.equal(autoUpdateVerdict(engineOnly({ appCurrent: '0.8.0', engineCurrent: '0.7.9', engineLatest: '0.8.1' })).go, false)
  assert.equal(autoUpdateVerdict(engineOnly({ appCurrent: '0.8.1', engineCurrent: '0.7.9', engineLatest: '0.8.1' })).go, true)
})

test('nothing newer -> nothing to do', () => {
  const v = autoUpdateVerdict(ready({ appVersion: '0.8.0', release: '0.8.0' }))
  assert.equal(v.go, false)
  assert.match(v.why, /nothing to update/)
  assert.equal(autoUpdateVerdict(undefined).go, false)
})

test('workingCount: this machine only, null when unreadable', () => {
  assert.equal(workingCount(null), null)
  assert.equal(workingCount({ detail: 'no' }), null)
  assert.equal(workingCount([]), 0)
  assert.equal(workingCount([
    { title: 'a', activity: 'working' },
    { title: 'b', activity: 'idle' },
    { title: 'c', activity: 'clarify' },
    { title: 'rig::d', activity: 'working' },   // another device's session
    { title: 'e', activity: 'working' },
    null,
  ]), 2)
})

test('updatedNotice: says once what landed, nothing for a swap that did not', () => {
  const app = updatedNotice({ kind: 'app', to: '0.8.1', from: '0.8.0', auto: true }, '0.8.1')
  assert.equal(app.message, 'Updated MindFlock to 0.8.1 while you were away.')
  assert.equal(app.auto, true)
  assert.equal(updatedNotice({ kind: 'app', to: '0.8.1', auto: false }, '0.8.1').message, 'Updated MindFlock to 0.8.1.')
  assert.equal(updatedNotice({ kind: 'app', to: '0.8.1' }, '0.8.0'), null)
  const eng = updatedNotice({ kind: 'engine', to: '0.8.1', auto: true }, '0.8.1')
  assert.match(eng.message, /^Updated the MindFlock engine to 0\.8\.1 while you were away\.$/)
  assert.equal(updatedNotice(null, '0.8.1'), null)
  assert.equal(updatedNotice({ kind: 'app' }, '0.8.1'), null)
})

// --- wiring pins (main.js is not requirable: electron) ------------------------
const mainJs = fs.readFileSync(path.join(__dirname, '..', 'main.js'), 'utf8')
const preload = fs.readFileSync(path.join(__dirname, '..', 'preload.js'), 'utf8')

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

test('a run from source is the dev shell OR any unpackaged run', () => {
  assert.match(mainJs, /const SOURCE_RUN = DEV \|\| !app\.isPackaged/)
})

test('a run from source never offers, announces or installs an app update', () => {
  assert.match(fn('function startUpdateChecks'), /if \(SOURCE_RUN\) \{[\s\S]*?return\n/)
  assert.match(fn('async function checkForUpdates'), /if \(SOURCE_RUN\) return/)
  assert.match(fn('function pendingUpdate'), /if \(SOURCE_RUN \|\| !updateAvailable\) return null/)
  assert.match(fn('async function checkEngineVersion'), /if \(SOURCE_RUN\) return/)
  assert.match(fn('async function updateEverything'), /if \(SOURCE_RUN\) \{/)
  assert.match(fn('async function autoUpdateTick'), /if \(autoTicking \|\| SOURCE_RUN\) return/)
  // The page's update.available OS notification is refused too.
  const notify = mainJs.slice(mainJs.indexOf("ipcMain.handle('notify:show'"))
  assert.ok(notify.indexOf('if (SOURCE_RUN) return { ok: false }') < notify.indexOf('claimUpdateNotice(version)'))
  // Settings is told, so it can say "update with git".
  assert.match(mainJs, /SOURCE_RUN \? \{ source: true \} : \{ autoUpdate: autoUpdateEnabled\(\) \}/)
})

test('the tick asks the pure verdict and runs the ONE update path', () => {
  const tick = fn('async function autoUpdateTick')
  assert.match(tick, /upd\.autoUpdateVerdict\(st\)/)
  // The sessions read happens only once everything else says go.
  assert.ok(tick.indexOf('if (v.go) v = upd.autoUpdateVerdict') < tick.indexOf('workingSessions()') + 40)
  assert.match(tick, /autoTried\.push\(v\.version\)/)
  assert.match(tick, /updateEverything\(\{ engine: true, app: v\.kind === 'app', auto: true \}\)/)
  assert.match(fn('function startUpdateChecks'), /setInterval\(\(\) => \{ autoUpdateTick\(\)\.catch/)
})

test('an automatic run never opens a browser tab and never installs on quit', () => {
  const run = fn('async function updateEverything')
  const auto = run.indexOf('if (want.auto) {')
  assert.ok(auto > 0 && auto < run.indexOf('shell.openExternal'))
  assert.match(fn('function initAutoUpdater'), /autoUpdater\.autoInstallOnAppQuit = false/)
})

test('the switch persists without clobbering the skipped version', () => {
  assert.match(fn('function writeUpdateStore'), /Object\.assign\(\{\}, readUpdateStore\(\), patch \|\| \{\}\)/)
  assert.match(fn('function autoUpdateEnabled'), /readUpdateStore\(\)\.autoUpdate !== false/)
  assert.match(mainJs, /ipcMain\.handle\('update:set-auto'/)
  assert.match(preload, /setAuto: \(on\) => ipcRenderer\.invoke\('update:set-auto', !!on\)/)
})

test('after an update the next app page says once what happened', () => {
  const run = fn('async function updateEverything')
  // Recorded before the swap so the relaunched app can read it back.
  assert.ok(run.indexOf("writeUpdateStore({ updated: { kind: 'app'") < run.indexOf('autoUpdater.quitAndInstall(true, true)'))
  assert.match(fn('function startUpdateChecks'), /upd\.updatedNotice\(store\.updated, app\.getVersion\(\)\)/)
  assert.match(fn('function startUpdateChecks'), /writeUpdateStore\(\{ updated: undefined \}\)/)
  assert.match(mainJs, /ipcMain\.handle\('update:done', \(\) => \{ const n = updatedNote; updatedNote = null; return n \}\)/)
  assert.match(preload, /done: \(\) => ipcRenderer\.invoke\('update:done'\)/)
  // Taken only by the app page, not the offline page shown mid-restart.
  assert.match(mainJs, /window\.mfupdate\.done && \/\^https\?:\$\/\.test\(location\.protocol\)/)
})

test("a developer's engine is never offered an engine update", () => {
  const plan = fn('async function enginePlan')
  assert.match(plan, /if \(WSL_REPO\) return \{ needed: false, dev: true/)
  assert.match(plan, /dev: kind === 'editable'/)
  const check = fn('async function checkEngineVersion')
  assert.ok(check.indexOf('if (engineIsDev) {') < check.indexOf("engineNotice = { current, latest, ref: 'v' + latest }"))
  assert.match(fn('async function updateEverything'), /if \(plan\.dev\) \{ engineNotice = null; engineIsDev = true \}/)
})
