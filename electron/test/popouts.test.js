'use strict'
// `node --test electron/test` — pop-out windows (electron/popouts.js): where
// a new one opens and a saved one comes back, the saved list, and the
// registry with fake windows and timers; then the main.js / preload.js
// wiring, pinned by shape (main.js is not requirable: electron).
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const { EventEmitter } = require('node:events')
const pop = require('../popouts')

const root = path.join(__dirname, '..')
const mainJs = fs.readFileSync(path.join(root, 'main.js'), 'utf8')
const preloadJs = fs.readFileSync(path.join(root, 'preload.js'), 'utf8')
const pkg = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'))

const WA = { x: 0, y: 0, width: 1920, height: 1040 }
const RIGHT = { x: 1920, y: 0, width: 1280, height: 1000 }
const at = (wa) => () => wa

// --- placement ---------------------------------------------------------------

test('the URL and window title follow the page\'s contract', () => {
  assert.equal(pop.popoutUrl('http://localhost:8765', 'fix bug #2'), 'http://localhost:8765/?popout=fix%20bug%20%232')
  assert.equal(pop.popoutUrl('http://localhost:8765/', 'a&b'), 'http://localhost:8765/?popout=a%26b')
  assert.equal(pop.popoutUrl('http://h:1', 'a', { token: 't k' }), 'http://h:1/?popout=a&token=t%20k')
  assert.equal(pop.windowTitle('api'), 'api — MindFlock')
  assert.equal(pop.windowTitle('api', true), 'api — MindFlock (dev)')
})

test('a title is one non-empty line', () => {
  assert.ok(pop.isTitle('my session'))
  for (const bad of ['', '   ', 'a\nb', null, 3, 'x'.repeat(513)]) assert.ok(!pop.isTitle(bad), String(bad))
})

test('a drop point is the new window\'s top-left, kept on the work area', () => {
  assert.deepEqual(pop.placeNew({ x: 100, y: 50, width: 800, height: 600 }, null, at(WA)),
    { x: 100, y: 50, width: 800, height: 600 })
  // Dropped near the bottom-right corner: pulled back so all of it shows.
  assert.deepEqual(pop.placeNew({ x: 1800, y: 1000 }, null, at(WA)),
    { x: 1920 - 900, y: 1040 - 640, width: 900, height: 640 })
  // Above / left of the work area (a taskbar on top, say).
  assert.deepEqual(pop.placeNew({ x: -50, y: -10 }, null, at({ x: 0, y: 40, width: 1920, height: 1000 })),
    { x: 0, y: 40, width: 900, height: 640 })
})

test('the display is the one nearest the drop point', () => {
  const seen = []
  const workAreaAt = (pt) => { seen.push(pt); return pt.x >= 1920 ? RIGHT : WA }
  const b = pop.placeNew({ x: 2500.4, y: 900 }, null, workAreaAt)
  assert.deepEqual(seen, [{ x: 2500, y: 900 }])
  assert.deepEqual(b, { x: 1920 + 1280 - 900, y: 1000 - 640, width: 900, height: 640 })
})

test('no drop point: it opens under the cursor, at the default size', () => {
  const b = pop.placeNew({}, { x: 960, y: 300 }, at(WA))
  assert.deepEqual(b, { x: 960 - 450, y: 280, width: 900, height: 640 })
})

test('sizes stay between the minimum and the work area', () => {
  assert.deepEqual(pop.placeNew({ x: 0, y: 0, width: 100, height: 50 }, null, at(WA)),
    { x: 0, y: 0, width: pop.MIN_SIZE.width, height: pop.MIN_SIZE.height })
  assert.deepEqual(pop.placeNew({ x: 0, y: 0, width: 5000, height: 5000 }, null, at(WA)),
    { x: 0, y: 0, width: 1920, height: 1040 })
  assert.deepEqual(pop.placeNew({ x: NaN, y: 4, width: -1 }, { x: 10, y: 20 }, at(WA)),
    { x: 0, y: 0, width: 900, height: 640 })
})

test('a saved pop-out comes back where it was while that is on a display', () => {
  const e = { title: 'a', bounds: { x: 2000, y: 100, width: 700, height: 500 }, maximized: true }
  assert.deepEqual(pop.restorePlacement(e, [{ workArea: WA }, { workArea: RIGHT }], at(WA)),
    { bounds: { x: 2000, y: 100, width: 700, height: 500 }, maximized: true })
})

test('its display unplugged: it comes back on the nearest one', () => {
  const e = { title: 'a', bounds: { x: 2000, y: 100, width: 700, height: 500 }, maximized: false }
  let asked = null
  const r = pop.restorePlacement(e, [{ workArea: WA }], (pt) => { asked = pt; return WA })
  assert.deepEqual(asked, { x: 2350, y: 350 })
  assert.deepEqual(r, { bounds: { x: 1920 - 700, y: 100, width: 700, height: 500 }, maximized: false })
})

test('unreadable saved bounds: the default size, centred', () => {
  assert.deepEqual(pop.restorePlacement({ title: 'a', bounds: null }, [{ workArea: WA }], at(WA)),
    { bounds: { x: 510, y: 200, width: 900, height: 640 }, maximized: false })
})

test('the offline page names the session and escapes it', () => {
  const html = pop.offlinePage('<b>&x')
  assert.ok(html.includes('&lt;b&gt;&amp;x — MindFlock'))
  assert.ok(!html.includes('<b>&x'))
  assert.ok(html.includes('-webkit-app-region:drag'))
})

// --- the saved list ----------------------------------------------------------

test('the saved list is cleaned: titled, one per title, capped', () => {
  const raw = [
    { title: 'a', bounds: { x: 1, y: 2, width: 3, height: 4 }, maximized: 1 },
    { title: 'a', bounds: null },
    { title: '' },
    null,
    'junk',
    { title: 'b', bounds: { x: 'no' } },
  ]
  assert.deepEqual(pop.savedPopouts(raw), [
    { title: 'a', bounds: { x: 1, y: 2, width: 3, height: 4 }, maximized: true },
    { title: 'b', bounds: null, maximized: false },
  ])
  assert.deepEqual(pop.savedPopouts({ not: 'a list' }), [])
  const many = Array.from({ length: 100 }, (_, i) => ({ title: 't' + i }))
  assert.equal(pop.savedPopouts(many).length, pop.MAX_SAVED)
})

test('upsert keeps order, remove drops, rename re-keys (and a clash gives way)', () => {
  const a = { title: 'a', bounds: null, maximized: false }
  const b = { title: 'b', bounds: null, maximized: false }
  const list = [a, b]
  assert.deepEqual(pop.upsertSaved(list, { ...a, maximized: true }).map((e) => [e.title, e.maximized]),
    [['a', true], ['b', false]])
  assert.deepEqual(pop.upsertSaved(list, { title: 'c' }).map((e) => e.title), ['a', 'b', 'c'])
  assert.deepEqual(pop.removeSaved(list, 'a').map((e) => e.title), ['b'])
  assert.deepEqual(pop.renameSaved(list, 'a', 'z').map((e) => e.title), ['z', 'b'])
  assert.deepEqual(pop.renameSaved(list, 'a', 'b').map((e) => e.title), ['b'])
  assert.equal(list[0].title, 'a', 'pure: the input is untouched')
})

// --- the registry ------------------------------------------------------------

class FakeWin extends EventEmitter {
  constructor (title, place) {
    super()
    this.title = title
    this.place = place
    this.bounds = place.bounds
    this.max = !!place.maximized
    this.destroyed = false
    this.calls = []
  }
  isDestroyed () { return this.destroyed }
  isMinimized () { return this.calls.includes('minimize') && !this.calls.includes('restore') }
  restore () { this.calls.push('restore') }
  show () { this.calls.push('show') }
  focus () { this.calls.push('focus') }
  close () {
    this.calls.push('close')
    this.destroyed = true
    this.emit('closed')
  }
}

function fakeTimers () {
  const t = { pending: new Map(), n: 0 }
  t.setTimeout = (fn) => { const id = ++t.n; t.pending.set(id, fn); return id }
  t.clearTimeout = (id) => t.pending.delete(id)
  t.run = () => { const fns = [...t.pending.values()]; t.pending.clear(); fns.forEach((f) => f()) }
  return t
}

function makeReg (opts) {
  const o = opts || {}
  const made = []
  const writes = []
  const pushes = []
  const titles = []
  const timers = fakeTimers()
  const reg = pop.createRegistry({
    create: (title, place) => {
      if (o.fail) throw new Error('no window')
      const w = new FakeWin(title, place)
      made.push(w)
      return w
    },
    boundsOf: (w) => (w.destroyed ? null : { bounds: w.bounds, maximized: w.max }),
    write: (list) => writes.push(list),
    changed: (t) => pushes.push(t),
    retitle: (w, t) => titles.push([w, t]),
    timers,
  })
  return { reg, made, writes, pushes, titles, timers, last: () => writes[writes.length - 1] }
}

const B1 = { x: 10, y: 20, width: 900, height: 640 }

test('open makes a window, saves it and tells every window', () => {
  const r = makeReg()
  assert.equal(r.reg.open('api', B1), true)
  assert.equal(r.made.length, 1)
  assert.deepEqual(r.made[0].place, { bounds: B1, maximized: false, restored: false })
  assert.deepEqual(r.reg.list(), ['api'])
  assert.deepEqual(r.last(), [{ title: 'api', bounds: B1, maximized: false }])
  assert.deepEqual(r.pushes, [['api']])
})

test('opening a title that is out already focuses it instead', () => {
  const r = makeReg()
  r.reg.open('api', B1)
  r.made[0].calls.push('minimize')
  assert.equal(r.reg.open('api', { x: 0, y: 0, width: 1, height: 1 }), true)
  assert.equal(r.made.length, 1)
  assert.deepEqual(r.made[0].calls, ['minimize', 'restore', 'show', 'focus'])
  assert.equal(r.reg.focus('nope'), false)
  assert.equal(r.reg.open('', B1), false)
})

test('a window that fails to build is not counted', () => {
  const r = makeReg({ fail: true })
  assert.equal(r.reg.open('api', B1), false)
  assert.deepEqual(r.reg.list(), [])
  assert.deepEqual(r.pushes, [])
})

test('closed by the person (✕ or the OS): it leaves the saved list', () => {
  const r = makeReg()
  r.reg.open('a', B1)
  r.reg.open('b', B1)
  r.made[0].close()
  assert.deepEqual(r.reg.list(), ['b'])
  assert.deepEqual(r.last().map((e) => e.title), ['b'])
  assert.deepEqual(r.pushes[r.pushes.length - 1], ['b'])
})

test('close(title) closes it and forgets its place; an unknown title is false', () => {
  const r = makeReg()
  r.reg.open('a', B1)
  assert.equal(r.reg.close('a'), true)
  assert.ok(r.made[0].calls.includes('close'))
  assert.deepEqual(r.reg.list(), [])
  assert.deepEqual(r.last(), [])
  // A saved-but-not-open one (its session went away before restore) is forgotten too.
  const s = makeReg()
  s.reg.load([{ title: 'gone', bounds: B1 }])
  assert.equal(s.reg.close('gone'), false)
  assert.deepEqual(s.last(), [])
})

test('rename re-keys the window and its saved place, without reopening it', () => {
  const r = makeReg()
  r.reg.open('a', B1)
  assert.equal(r.reg.rename('a', 'b'), true)
  assert.equal(r.made.length, 1)
  assert.deepEqual(r.reg.list(), ['b'])
  assert.equal(r.reg.titleOf(r.made[0]), 'b')
  assert.deepEqual(r.titles, [[r.made[0], 'b']])
  assert.deepEqual(r.last().map((e) => e.title), ['b'])
  assert.deepEqual(r.pushes[r.pushes.length - 1], ['b'])
  // Closing it afterwards removes the NEW title.
  r.made[0].close()
  assert.deepEqual(r.last(), [])
})

test('rename refuses a clash and a no-op', () => {
  const r = makeReg()
  r.reg.open('a', B1)
  r.reg.open('b', B1)
  assert.equal(r.reg.rename('a', 'b'), false)
  assert.equal(r.reg.rename('a', 'a'), false)
  assert.equal(r.reg.rename('zz', 'yy'), false)
  assert.deepEqual(r.reg.list(), ['a', 'b'])
})

test('moves are saved once per burst, with the window\'s place then', () => {
  const r = makeReg()
  r.reg.open('a', B1)
  const before = r.writes.length
  r.made[0].bounds = { x: 300, y: 200, width: 1000, height: 700 }
  r.reg.moved()
  r.reg.moved()
  r.reg.moved()
  assert.equal(r.writes.length, before, 'debounced')
  assert.equal(r.timers.pending.size, 1)
  r.made[0].max = true
  r.timers.run()
  assert.equal(r.writes.length, before + 1)
  assert.deepEqual(r.last(), [{ title: 'a', bounds: { x: 300, y: 200, width: 1000, height: 700 }, maximized: true }])
})

test('the main window closing closes every pop-out and keeps them saved', () => {
  const r = makeReg()
  r.reg.open('a', B1)
  r.reg.open('b', B1)
  r.made[1].bounds = { x: 5, y: 5, width: 500, height: 400 }
  r.reg.moved()   // pending: closeAll flushes it
  r.reg.closeAll()
  assert.ok(r.made.every((w) => w.destroyed))
  assert.deepEqual(r.reg.list(), [])
  assert.deepEqual(r.last(), [
    { title: 'a', bounds: B1, maximized: false },
    { title: 'b', bounds: { x: 5, y: 5, width: 500, height: 400 }, maximized: false },
  ])
  assert.equal(r.timers.pending.size, 0)
  // Quitting: nothing new opens.
  assert.equal(r.reg.open('c', B1), false)
})

test('a quit (before-quit) keeps the list even as the windows close', () => {
  const r = makeReg()
  r.reg.open('a', B1)
  r.reg.keepAll()
  r.made[0].close()
  assert.deepEqual(r.last().map((e) => e.title), ['a'])
})

test('restore reopens the saved ones once, placed by the caller', () => {
  const r = makeReg()
  r.reg.load([{ title: 'a', bounds: B1, maximized: true }, { title: 'b', bounds: null }])
  const placed = []
  const n = r.reg.restore((e) => { placed.push(e.title); return { bounds: B1, maximized: e.maximized } })
  assert.equal(n, 2)
  assert.deepEqual(placed, ['a', 'b'])
  assert.deepEqual(r.made.map((w) => [w.title, w.place.restored, w.place.maximized]),
    [['a', true, true], ['b', true, false]])
  assert.deepEqual(r.pushes, [['a', 'b']])
  // Again (a second main load): nothing new.
  assert.equal(r.reg.restore(() => ({ bounds: B1 })), 0)
  // load() is ignored once windows are open.
  r.reg.load([])
  assert.deepEqual(r.reg.saved().map((e) => e.title), ['a', 'b'])
})

test('a restored one the page docks (its session is gone) leaves the list', () => {
  const r = makeReg()
  r.reg.load([{ title: 'gone', bounds: B1 }, { title: 'kept', bounds: B1 }])
  r.reg.restore((e) => ({ bounds: e.bounds }))
  assert.equal(r.reg.close('gone'), true)
  assert.deepEqual(r.last().map((e) => e.title), ['kept'])
})

// --- main.js / preload.js wiring ---------------------------------------------

// From `start` to the matching close of its first `{` block.
function block (src, start) {
  const i = src.indexOf(start)
  assert.ok(i >= 0, start)
  let depth = 0
  for (let k = src.indexOf('{', i); k < src.length; k++) {
    if (src[k] === '{') depth++
    else if (src[k] === '}' && --depth === 0) return src.slice(i, k + 1)
  }
  throw new Error(start)
}

test('the – □ ✕ buttons act on the window that sent them', () => {
  for (const ch of ['win:minimize', 'win:toggle-maximize', 'win:close']) {
    const h = block(mainJs, "ipcMain.on('" + ch + "'")
    assert.match(h, /senderWindow\(e\)/, ch)
    assert.ok(!/\bwin\./.test(h), ch + ' must not touch the main window')
  }
  assert.match(block(mainJs, "ipcMain.handle('win:is-fullscreen'"), /senderWindow\(e\)/)
  assert.match(block(mainJs, 'function senderWindow('), /BrowserWindow\.fromWebContents\(e\.sender\)/)
})

test('every window is wired the same way, over its own window', () => {
  const wire = block(mainJs, 'function wireWindow(')
  for (const ev of ["'dom-ready'", "'before-input-event'", "'did-fail-load'", "'render-process-gone'",
    "'maximized-changed'", "'fullscreen-changed'", 'setWindowOpenHandler', "'will-navigate'", 'logger.attachWindow(w)']) {
    assert.ok(wire.includes(ev), 'wireWindow: ' + ev)
  }
  assert.ok(!/\bwin\b/.test(wire.replace(/\/\/.*$/gm, '')), 'wireWindow never uses the global win')
  assert.match(block(mainJs, 'function createWindow('), /wireWindow\(win, \{\s*isMain: true/)
  assert.match(block(mainJs, 'function createPopoutWindow('), /wireWindow\(pw, \{ isMain: false/)
  // The update toast is the main window's alone.
  assert.match(block(mainJs, 'function injectChrome('), /isMain \? UPDATE_JS : ''/)
})

test('pop-outs load ?popout=<title>, frameless like main, with a minimum size', () => {
  const create = block(mainJs, 'function createPopoutWindow(')
  assert.match(create, /\.\.\.windowChrome\(\)/)
  assert.match(block(mainJs, 'function createWindow('), /\.\.\.windowChrome\(\)/)
  assert.match(create, /minWidth: pop\.MIN_SIZE\.width/)
  assert.match(block(mainJs, 'function popoutPageUrl('), /pop\.popoutUrl\(APP_ORIGIN \|\| APP_URL, title, extra\)/)
  assert.deepEqual(pop.MIN_SIZE, { width: 420, height: 300 })
  assert.deepEqual(pop.DEFAULT_SIZE, { width: 900, height: 640 })
})

test('the main window: restores pop-outs after its first app load, takes them with it on close', () => {
  const create = block(mainJs, 'function createWindow(')
  const signedIn = create.indexOf('if (win.webContents.getTitle() !== SIGN_IN_TITLE)')
  assert.ok(signedIn > 0 && create.indexOf('restorePopouts()', signedIn) > signedIn)
  assert.match(create, /popouts\.load\(saved && saved\.popouts\)/)
  assert.match(create, /win\.on\('closed', \(\) => \{\s*win = null[\s\S]*popouts\.closeAll\(\)/)
  assert.match(mainJs, /app\.on\('before-quit', \(\) => popouts\.keepAll\(\)\)/)
  assert.match(block(mainJs, 'function restorePopouts('), /if \(popoutsRestored\) return/)
  // Both halves write the one file, each keeping the other's keys.
  assert.match(block(mainJs, 'function saveWindowState('), /writeWindowState\(\{ bounds: win\.getNormalBounds\(\)/)
  assert.match(mainJs, /write: \(list\) => writeWindowState\(\{ popouts: list \}\)/)
})

test('notifications: quiet while ANY app window is focused; a click still goes to main', () => {
  const h = mainJs.slice(mainJs.indexOf("ipcMain.handle('notify:show'"))
  assert.ok(h.indexOf('if (BrowserWindow.getFocusedWindow()) return { ok: false }') > 0)
  const show = block(mainJs, 'function showNotification(')
  assert.match(show, /focusWindow\(\)/)
  assert.match(show, /win\.webContents\.send\('notify:click', target\)/)
})

test('preload exposes mfwin with the WinBridge methods, on the channels main handles', () => {
  const bridge = block(preloadJs, "contextBridge.exposeInMainWorld('mfwin'")
  for (const m of ['open:', 'focus:', 'close:', 'rename:', 'list:', 'onChanged:', 'focusMain:']) {
    assert.ok(bridge.includes(m), 'mfwin.' + m)
  }
  const channels = [...bridge.matchAll(/ipcRenderer\.invoke\('([^']+)'/g)].map((m) => m[1])
  assert.deepEqual(channels.sort(), ['win:focus-main', 'win:popout-close', 'win:popout-focus',
    'win:popout-list', 'win:popout-open', 'win:popout-rename'])
  for (const ch of channels) assert.ok(mainJs.includes("ipcMain.handle('" + ch + "'"), 'main handles ' + ch)
  assert.match(bridge, /ipcRenderer\.on\('win:popouts', h\)/)
  assert.match(bridge, /removeListener\('win:popouts', h\)/)
  assert.match(block(mainJs, 'function broadcastPopouts('), /BrowserWindow\.getAllWindows\(\)/)
  assert.match(block(mainJs, 'function broadcastPopouts('), /send\('win:popouts', titles\)/)
})

test('the packaged app ships popouts.js', () => {
  assert.ok(pkg.build.files.includes('popouts.js'))
})
