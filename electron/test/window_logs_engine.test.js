'use strict'
// UI sweep round 2, package Q5, the desktop shell:
//  - the window remembers its size, place and maximized state
//    (restorableBounds runs as written; the save/restore wiring is pinned);
//  - the offline page's starting / failed-to-start / engine-broken states
//    have an "Open log folder" button, handled by main's diag:action;
//  - engine:update-info re-runs a failed engine check before answering, so
//    Settings → Advanced's "Check again" really checks.
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')

const root = path.join(__dirname, '..')
const mainJs = fs.readFileSync(path.join(root, 'main.js'), 'utf8')
const html = fs.readFileSync(path.join(root, 'offline.html'), 'utf8')

// A top-level `function name(…) { … }` from main.js, as written.
function topFn(name) {
  const start = mainJs.indexOf('function ' + name + '(')
  assert.ok(start >= 0, name + ' found')
  const end = mainJs.indexOf('\n}\n', start)
  return mainJs.slice(start, end + 2)
}
function topConst(name) {
  const line = mainJs.split('\n').find((l) => l.startsWith('const ' + name + ' = '))
  assert.ok(line, name + ' found')
  return line
}

const restorableBounds = vm.runInNewContext(
  [topConst('WINDOW_MIN'), topFn('restorableBounds'), 'restorableBounds'].join('\n')
)
const PRIMARY = [{ workArea: { x: 0, y: 0, width: 1920, height: 1040 } }]

test('a saved window on a connected display is restored where it was', () => {
  const saved = { bounds: { x: 100, y: 80, width: 1400, height: 900 }, maximized: false }
  assert.deepEqual({ ...restorableBounds(saved, PRIMARY) }, { x: 100, y: 80, width: 1400, height: 900 })
})

test('a window saved on a monitor that is gone opens at the defaults', () => {
  const saved = { bounds: { x: 2500, y: 100, width: 1280, height: 820 } }
  assert.deepEqual({ ...restorableBounds(saved, PRIMARY) }, {})
  // ...but on that monitor, when it is plugged in again, it goes back there.
  const two = PRIMARY.concat([{ workArea: { x: 1920, y: 0, width: 2560, height: 1400 } }])
  assert.equal(restorableBounds(saved, two).x, 2500)
})

test('a sliver left on screen is not enough to restore to', () => {
  const saved = { bounds: { x: 1900, y: 100, width: 1280, height: 820 } }
  assert.deepEqual({ ...restorableBounds(saved, PRIMARY) }, {})
})

test('unreadable or too-small saved state falls back or is clamped', () => {
  assert.deepEqual({ ...restorableBounds(null, PRIMARY) }, {})
  assert.deepEqual({ ...restorableBounds({ bounds: { x: 'a' } }, PRIMARY) }, {})
  assert.deepEqual({ ...restorableBounds({ bounds: { x: 0, y: 0, width: 10, height: 10 } }, PRIMARY) },
    { x: 0, y: 0, width: 900, height: 600 })
})

test('the window saves on close and is created from what was saved', () => {
  const create = topFn('createWindow')
  assert.ok(create.includes('restorableBounds(saved, require(\'electron\').screen.getAllDisplays())'))
  assert.ok(/new BrowserWindow\(\{\s*\.\.\.WINDOW_DEFAULT,\s*\.\.\.place,/.test(create))
  assert.ok(create.includes('if (saved && saved.maximized) win.maximize()'))
  assert.ok(create.includes("win.on('close', saveWindowState)"))
  const save = topFn('saveWindowState')
  assert.ok(save.includes('win.getNormalBounds()') && save.includes('win.isMaximized()'))
  // userData: the dev build moves it, so dev and prod keep separate windows.
  assert.ok(topConst('windowStatePath').includes("app.getPath('userData')"))
})

test('the offline page offers Open log folder in its three log-pointing states', () => {
  for (const [section, id] of [
    ['s-starting', 'starting-open-logs'],
    ['s-failed-to-start', 'failed-open-logs'],
    ['s-engine-broken', 'broken-open-logs'],
  ]) {
    const start = html.indexOf('<section id="' + section + '"')
    const body = html.slice(start, html.indexOf('</section>', start))
    assert.ok(body.includes('<button class="action quiet" id="' + id + '">Open log folder</button>'), id)
    assert.ok(html.includes("wireQuiet('" + id + "', 'open-logs', 'Opened')"), id + ' wired')
  }
  assert.ok(mainJs.includes("case 'open-logs':"))
})

test('Open log folder and Ctrl+Shift+L open the same folder the same way', () => {
  const open = topFn('openLogFolder')
  assert.ok(open.includes('logger.paths().dir') && open.includes('shell.openPath(dir)'))
  assert.ok(mainJs.includes("case 'open-logs':\n      return openLogFolder()"))
  const shortcut = mainJs.slice(mainJs.indexOf('// Ctrl+Shift+L opens the logs folder'))
  assert.ok(shortcut.slice(0, 200).includes('openLogFolder()'))
})

test('engine:update-info re-runs a failed check before answering', () => {
  const start = mainJs.indexOf("ipcMain.handle('engine:update-info'")
  const handler = mainJs.slice(start, mainJs.indexOf('\n})\n', start))
  assert.ok(handler.includes('!engineCheck.checked'))
  assert.ok(handler.includes('await checkEngineVersion()'))
  assert.ok(handler.indexOf('await checkEngineVersion()') < handler.indexOf('return engineUpdateInfo()'))
  // Never for a source run (no check runs there) or with an update known.
  assert.ok(handler.includes('!SOURCE_RUN && !engineNotice'))
})
