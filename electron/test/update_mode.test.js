'use strict'
// node --test: the update mode (auto | ask | off) now lives on the server
// (GET /api/update/check `mode` / `mode_set` / `managed_by_shell`, POST
// /api/update/mode). The pure rules (electron/update.js), then the main.js
// wiring that applies them. An older engine answers without `mode`: then the
// shell must behave exactly as before (its own `autoUpdate` switch).
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')

const {
  MODES, resolveMode, legacyMode, modeToMigrate, modeForSwitch, offerToast, localPort,
} = require('../update')

// --- resolveMode ---------------------------------------------------------------

test('the three modes', () => {
  assert.deepEqual(MODES, ['auto', 'ask', 'off'])
})

test('a server that knows the mode decides it', () => {
  for (const m of MODES) {
    assert.equal(resolveMode({ mode: m, mode_set: true }, { autoUpdate: true, mode: 'off' }), m)
  }
  // Never chosen: the server's effective mode (ask) still wins over the switch.
  assert.equal(resolveMode({ mode: 'ask', mode_set: false }, {}), 'ask')
})

test('an older engine (no mode in its answer) behaves exactly as before', () => {
  // Before: automatic updates ON unless the switch was turned off.
  assert.equal(legacyMode({}), 'auto')
  assert.equal(legacyMode({ autoUpdate: true }), 'auto')
  assert.equal(legacyMode({ autoUpdate: false }), 'ask')
  const old = { kind: 'uv-tool', available: false }
  assert.equal(resolveMode(old, {}), 'auto')
  assert.equal(resolveMode(old, { autoUpdate: false }), 'ask')
  // A cached mode from a newer engine is NOT used once an older one answers.
  assert.equal(resolveMode(old, { mode: 'off' }), 'auto')
  // Garbage is not a mode.
  assert.equal(resolveMode({ mode: 'sometimes' }, { autoUpdate: false }), 'ask')
})

test('server down: the cached mode, else the switch', () => {
  assert.equal(resolveMode(null, { mode: 'off', autoUpdate: true }), 'off')
  assert.equal(resolveMode(null, { mode: 'ask' }), 'ask')
  assert.equal(resolveMode(null, {}), 'auto')
  assert.equal(resolveMode(null, { autoUpdate: false }), 'ask')
  assert.equal(resolveMode(null, null), 'auto')
})

// --- modeToMigrate -------------------------------------------------------------

test('migration: an explicit switch carries over once, while nobody chose a mode', () => {
  const fresh = { mode: 'ask', mode_set: false, managed_by_shell: true }
  assert.equal(modeToMigrate(fresh, { autoUpdate: true }), 'auto')
  assert.equal(modeToMigrate(fresh, { autoUpdate: false }), 'ask')
  // Also when the server drives its own update: it was the person's choice.
  assert.equal(modeToMigrate({ mode: 'ask', mode_set: false, managed_by_shell: false }, { autoUpdate: true }), 'auto')
})

test('migration: never twice, never over a chosen mode, never to an older engine', () => {
  assert.equal(modeToMigrate({ mode: 'ask', mode_set: false }, { autoUpdate: true, modeMigrated: true }), '')
  assert.equal(modeToMigrate({ mode: 'off', mode_set: true }, { autoUpdate: true }), '')
  assert.equal(modeToMigrate({ kind: 'uv-tool' }, { autoUpdate: true }), '')
  // mode_set missing is not "never chosen".
  assert.equal(modeToMigrate({ mode: 'ask' }, { autoUpdate: true }), '')
  assert.equal(modeToMigrate(null, { autoUpdate: true }), '')
})

test('migration: no switch at all = the desktop default (on), only where the app manages the engine', () => {
  // The packaged app over its own engine kept automatic updates by default.
  assert.equal(modeToMigrate({ mode: 'ask', mode_set: false, managed_by_shell: true }, {}), 'auto')
  // A server that drives itself (a development checkout) is not switched to
  // auto by a default nobody chose.
  assert.equal(modeToMigrate({ mode: 'ask', mode_set: false, managed_by_shell: false }, {}), '')
  assert.equal(modeToMigrate({ mode: 'ask', mode_set: false }, {}), '')
})

// --- modeForSwitch -------------------------------------------------------------

test('the old on/off switch maps onto the three modes without undoing an explicit off', () => {
  assert.equal(modeForSwitch(true, 'ask'), 'auto')
  assert.equal(modeForSwitch(true, 'off'), 'auto')
  assert.equal(modeForSwitch(true, 'auto'), 'auto')
  assert.equal(modeForSwitch(false, 'auto'), 'ask')
  assert.equal(modeForSwitch(false, 'ask'), 'ask')
  assert.equal(modeForSwitch(false, 'off'), 'off')
  assert.equal(modeForSwitch(false, undefined), 'ask')
})

// --- offerToast ----------------------------------------------------------------

test('off: no toast, whatever is out', () => {
  assert.equal(offerToast({ mode: 'off', canAuto: false, version: '1.0.0' }), false)
  assert.equal(offerToast({ mode: 'off', canAuto: true, version: '1.0.0', tried: ['1.0.0'] }), false)
})

test('ask: today\'s one toast', () => {
  assert.equal(offerToast({ mode: 'ask', canAuto: true, version: '1.0.0' }), true)
  assert.equal(offerToast({ mode: 'ask', canAuto: false, version: '1.0.0' }), true)
})

test('auto: silent while it can install itself; the toast only once it can\'t', () => {
  assert.equal(offerToast({ mode: 'auto', canAuto: true, version: '1.0.0', tried: [] }), false)
  // The updater can't swap the app here (macOS refusing it, no metadata).
  assert.equal(offerToast({ mode: 'auto', canAuto: false, version: '1.0.0' }), true)
  // Tried and didn't land: now it is the person's to do.
  assert.equal(offerToast({ mode: 'auto', canAuto: true, version: '1.0.0', tried: ['1.0.0'] }), true)
  assert.equal(offerToast({ mode: 'auto', canAuto: true, version: '1.0.1', tried: ['1.0.0'] }), false)
})

// --- localPort -----------------------------------------------------------------

test('the port is the one the window is on', () => {
  assert.equal(localPort('http://localhost:8765'), 8765)
  assert.equal(localPort('http://localhost:9000'), 9000)
  assert.equal(localPort('http://127.0.0.1:9000/?setup=1'), 9000)
  assert.equal(localPort('http://localhost'), 80)
  assert.equal(localPort('https://localhost'), 443)
  assert.equal(localPort('http://[::1]:9100'), 9100)
  // Not a local URL, or not a URL: the engine's default.
  assert.equal(localPort('https://box.example.ts.net'), 8765)
  assert.equal(localPort('not a url'), 8765)
  assert.equal(localPort(undefined), 8765)
})

// --- main.js wiring (main.js is not requirable: electron) ----------------------
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

test('checks run every 30 minutes, not every 6 hours', () => {
  assert.match(mainJs, /const UPDATE_INTERVAL_MS = 30 \* 60 \* 1000/)
  assert.match(fn('function startUpdateChecks'), /setInterval\(\(\) => \{ tick\(\)\.catch\(\(\) => \{\}\) \}, UPDATE_INTERVAL_MS\)/)
})

test('every local request of the update code uses the window\'s port', () => {
  assert.match(mainJs, /const PORT = upd\.localPort\(APP_URL\)/)
  assert.match(fn('function fetchLocalJSON'), /host: '127\.0\.0\.1', port: PORT,/)
  assert.match(fn('function postLocalJSON'), /host: '127\.0\.0\.1', port: PORT,/)
  assert.match(fn('function postLocalJSON'), /Authorization: 'Bearer ' \+ token/)
  // No other way to the local server, and no hardcoded port.
  assert.ok(!/port: 8765/.test(mainJs))
  assert.equal((mainJs.match(/http\.get\(/g) || []).length, 1, 'fetchLocalJSON is the one GET')
  assert.equal((mainJs.match(/http\.request\(/g) || []).length, 1, 'postLocalJSON is the one POST')
  assert.match(fn('async function stopEngineServer'), /portFree\(PORT\)/)
})

test('the mode comes from the server, is cached, and migrates the switch once', () => {
  const refresh = fn('function refreshServerUpdate')
  assert.match(refresh, /fetchLocalJSON\('\/api\/update\/check\?refresh=0', 8000, token\)/)
  // An older engine: no mode cached, the switch decides.
  assert.match(refresh, /if \(!upd\.MODES\.includes\(chk\.mode\)\) \{[\s\S]*?writeUpdateStore\(\{ mode: undefined \}\)/)
  // The one-time move, never from a run from source.
  assert.match(refresh, /const migrate = SOURCE_RUN \? '' : upd\.modeToMigrate\(chk, store\)/)
  assert.match(refresh, /postLocalJSON\('\/api\/update\/mode', \{ mode: migrate \}, 8000, token\)/)
  assert.ok(refresh.indexOf("writeUpdateStore({ modeMigrated: true })") > refresh.indexOf('postLocalJSON'))
  // Cached for when the server is down.
  assert.match(refresh, /writeUpdateStore\(serverUpd\.mode_set === false \? \{ mode \} : \{ mode, autoUpdate: mode === 'auto' \}\)/)
  // The migration reads the switch BEFORE the cache write can touch it.
  assert.ok(refresh.indexOf('upd.modeToMigrate') < refresh.indexOf('autoUpdate: mode ==='))
})

test('mode off: no checks and no toasts', () => {
  const start = fn('function startUpdateChecks')
  const off = start.indexOf("if (mode === 'off') {")
  assert.ok(off > 0)
  assert.ok(start.indexOf('await refreshServerUpdate()') < off)
  assert.ok(off < start.indexOf('checkForUpdates()'))
  assert.ok(off < start.indexOf('checkAppUpdate()'))
  assert.ok(off < start.indexOf('checkEngineVersion()'))
  // Every toast path asks the mode (upd.offerToast): app, engine, OS notice.
  assert.match(fn('function updatePayload'), /if \(!p \|\| !offerApp\(p\)\) return null/)
  assert.match(fn('function pushEngineNotice'), /offerEngine\(engineNotice\)/)
  assert.match(mainJs, /ipcMain\.handle\('engine:get', \(\) =>\n\s+\(pendingUpdate\(\) \|\| !engineNotice \|\| !offerEngine\(engineNotice\) \? null : engineNotice\)\)/)
  assert.match(fn('async function checkForUpdates'), /if \(offerApp\(updateAvailable\) && claimUpdateNotice\(latest\) && unfocused\)/)
  const notify = mainJs.slice(mainJs.indexOf("ipcMain.handle('notify:show'"))
  assert.ok(notify.indexOf("if (updateMode() === 'off') return { ok: false }") < notify.indexOf('claimUpdateNotice(version)'))
  // The automatic run is the mode too.
  assert.match(fn('function autoUpdateEnabled'), /updateMode\(\) === 'auto'/)
})

test('auto: the toasts stay quiet while it can install itself, and offer it once it couldn\'t', () => {
  assert.match(fn('function offerApp'), /canAuto: !!autoUpdater && appUpd\.state !== 'error'/)
  assert.match(fn('function offerApp'), /tried: autoTried/)
  const tick = fn('async function autoUpdateTick')
  const ran = tick.indexOf('await updateEverything(')
  assert.ok(ran > 0 && ran < tick.indexOf("if (run.state !== 'running') { pushUpdateToRenderer(); pushEngineNotice() }"))
  // A fresh look at the mode before going (another device may have changed it).
  assert.ok(tick.indexOf('freshServerUpdate(') < tick.indexOf('workingSessions()'))
})

test('a server that drives its own update gets no engine toast and no engine install from here', () => {
  assert.match(fn('function serverDrivesEngine'), /serverUpd\.managed_by_shell === false/)
  const check = fn('async function checkEngineVersion')
  const self = check.indexOf('if (serverDrivesEngine()) {')
  assert.ok(self > 0 && self < check.indexOf("engineNotice = { current, latest, ref: 'v' + latest }"))
  const plan = fn('async function enginePlan')
  assert.match(plan, /if \(chk && chk\.managed_by_shell === false\) \{\n\s+return \{ needed: false, selfManaged: true/)
  // Before the release-install check, so it holds for any kind.
  assert.ok(plan.indexOf('managed_by_shell === false') < plan.indexOf("kind !== 'uv-tool'"))
  assert.match(fn('async function updateEverything'), /if \(plan\.selfManaged\) engineNotice = null/)
})

test('IPC: update:get carries the mode; update:set-auto also sets it on the server', () => {
  const get = mainJs.slice(mainJs.indexOf("ipcMain.handle('update:get'"))
  assert.match(get.slice(0, 200), /return Object\.assign\(\{ mode: updateMode\(\) \}, updatePayload\(\) \|\| \{\}\)/)
  const set = mainJs.slice(mainJs.indexOf("ipcMain.handle('update:set-auto'"))
  const body = set.slice(0, set.indexOf('\n})') + 3)
  assert.match(body, /writeUpdateStore\(\{ autoUpdate: !!on \}\)/)
  assert.match(body, /const want = upd\.modeForSwitch\(!!on, srv\.mode\)/)
  assert.match(body, /postLocalJSON\('\/api\/update\/mode', \{ mode: want \}/)
  assert.match(body, /return \{ autoUpdate: autoUpdateEnabled\(\), mode: updateMode\(\) \}/)
  // Settings' engine info carries it too.
  assert.match(fn('function engineUpdateInfo'), /\{ mode: updateMode\(\), selfManaged: serverDrivesEngine\(\) \}/)
})

test('plain copy: no engine-vs-app jargon in the offers, no git told to anyone', () => {
  const toast = mainJs.slice(mainJs.indexOf('const UPDATE_JS = `'), mainJs.indexOf('let win = null'))
  assert.ok(!/Update engine/.test(toast))
  assert.ok(!/Engine update available/.test(toast))
  assert.ok(!/the engine too/.test(toast))
  assert.ok(!/installs itself once you/.test(toast))
  assert.match(toast, /'\. Update installs it and restarts MindFlock\.'/)
  assert.ok(!/update this checkout with git/.test(mainJs))
})
