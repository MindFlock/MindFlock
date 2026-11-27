'use strict'
// Pop-out windows: a session in an OS window of its own, torn off the main
// window (frontend/src/lib/popout.ts is the page's half, preload.js `mfwin`
// the bridge). Pure helpers (no Electron; windows, timers and the disk are
// injected), shared with electron/test:
//
//   - placement: where a new pop-out goes (clamped onto a display's work
//     area) and where a saved one comes back (its own place while that is
//     still on a connected display, else the nearest display);
//   - the saved list, `popouts: [{title, bounds, maximized}]` in
//     userData/window-state.json beside the main window's own entry;
//   - the registry: which title is in which window, kept in step with the
//     saved list, and told of every change.
//
// A pop-out the person closes (its ✕, the OS close, or the page's "Back")
// leaves the saved list; pop-outs that close because the main window closed
// or the app quit stay in it and reopen at the next launch.

// A pop-out's size when the page gives none, and the smallest it may be.
const DEFAULT_SIZE = { width: 900, height: 640 }
const MIN_SIZE = { width: 420, height: 300 }
// The page's query parameter (popout.ts POPOUT_PARAM).
const POPOUT_PARAM = 'popout'
// More than anyone tears off; a cap so a corrupt file can't open hundreds.
const MAX_SAVED = 24
// Moves and resizes arrive continuously while dragging: one write per burst.
const SAVE_DEBOUNCE_MS = 400

const finite = (n) => typeof n === 'number' && Number.isFinite(n)
const clamp = (n, lo, hi) => Math.min(Math.max(n, lo), hi)

// A session title as the page names one: non-empty, one line, bounded (the
// same rule as main.js's `session:<title>` notify target).
function isTitle (t) {
  return typeof t === 'string' && t.trim() !== '' && t.length <= 512 && !/[\n\r\0]/.test(t)
}

// The page a pop-out of `title` loads: the app itself with ?popout=<title>
// (popout.ts popoutUrl). `extra` adds query parameters (a sign-in token).
function popoutUrl (origin, title, extra) {
  let u = String(origin || '').replace(/\/+$/, '') + '/?' + POPOUT_PARAM + '=' + encodeURIComponent(title)
  for (const k of Object.keys(extra || {})) u += '&' + encodeURIComponent(k) + '=' + encodeURIComponent(extra[k])
  return u
}

// The OS window title (taskbar, alt-tab, macOS Window menu).
function windowTitle (title, dev) {
  return title + ' — MindFlock' + (dev ? ' (dev)' : '')
}

const escapeHtml = (s) => String(s).replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c])

// The page a pop-out shows while the server doesn't answer (main.js loads it
// as a data: URL and retries the real one with backoff). The main window has
// the diagnostics (offline.html); this one only says it is waiting, and is
// draggable by its top strip — the shell's – □ ✕ are injected over it.
function offlinePage (title) {
  const t = escapeHtml(title)
  return '<!doctype html><html><head><meta charset="utf-8"><title>' + t + ' — MindFlock</title>' +
    '<style>html,body{margin:0;height:100%;background:#0f1117;color:#d7dae3;' +
    'font:14px system-ui,"Segoe UI",sans-serif}' +
    '#bar{height:40px;-webkit-app-region:drag;display:flex;align-items:center;padding:0 14px;color:#8a90a2}' +
    '#msg{display:flex;flex-direction:column;align-items:center;justify-content:center;height:calc(100% - 80px);gap:6px}' +
    '#msg small{color:#8a90a2}</style></head><body><div id="bar">' + t + '</div>' +
    '<div id="msg"><div>Waiting for MindFlock…</div><small>This window reconnects by itself.</small></div>' +
    '</body></html>'
}

// A rectangle moved and sized so all of it is on `wa` (a display's work
// area), no smaller than `min`. A missing size is the default; a missing
// place is the middle of the work area.
function clampToWorkArea (b, wa, min) {
  const m = min || MIN_SIZE
  const r = b || {}
  const width = clamp(Math.round(finite(r.width) && r.width > 0 ? r.width : DEFAULT_SIZE.width),
    m.width, Math.max(m.width, wa.width))
  const height = clamp(Math.round(finite(r.height) && r.height > 0 ? r.height : DEFAULT_SIZE.height),
    m.height, Math.max(m.height, wa.height))
  const x0 = finite(r.x) ? Math.round(r.x) : wa.x + Math.round((wa.width - width) / 2)
  const y0 = finite(r.y) ? Math.round(r.y) : wa.y + Math.round((wa.height - height) / 2)
  // A window wider than the work area (only when min is) keeps its left edge on it.
  const x = clamp(x0, wa.x, Math.max(wa.x, wa.x + wa.width - width))
  const y = clamp(y0, wa.y, Math.max(wa.y, wa.y + wa.height - height))
  return { x, y, width, height }
}

// Where a new pop-out opens. `o` is the page's {x?, y?, width?, height?}:
// x/y the screen point for its top-left (a drag's event.screenX/Y). Without
// them it opens under `cursor`, its header beneath the pointer. `workAreaAt`
// maps a point to the work area of the display nearest it.
function placeNew (o, cursor, workAreaAt) {
  const r = o || {}
  const width = finite(r.width) && r.width > 0 ? r.width : DEFAULT_SIZE.width
  const height = finite(r.height) && r.height > 0 ? r.height : DEFAULT_SIZE.height
  let pt
  let x
  let y
  if (finite(r.x) && finite(r.y)) {
    pt = { x: Math.round(r.x), y: Math.round(r.y) }
    x = pt.x
    y = pt.y
  } else {
    const c = cursor && finite(cursor.x) && finite(cursor.y) ? cursor : { x: 0, y: 0 }
    pt = { x: Math.round(c.x), y: Math.round(c.y) }
    x = pt.x - Math.round(width / 2)
    y = pt.y - 20
  }
  return clampToWorkArea({ x, y, width, height }, workAreaAt(pt))
}

// Enough of `b` on one of `displays` to grab its title strip: 64px each way
// (main.js restorableBounds uses the same rule for the main window).
function onSomeDisplay (b, displays) {
  return (displays || []).some((d) => {
    const a = d && (d.workArea || d.bounds)
    if (!a) return false
    const ix = Math.min(b.x + b.width, a.x + a.width) - Math.max(b.x, a.x)
    const iy = Math.min(b.y + b.height, a.y + a.height) - Math.max(b.y, a.y)
    return ix >= 64 && iy >= 64
  })
}

// Where a saved pop-out comes back: its own place while that is still on a
// connected display, else onto the display nearest it (its own may have been
// unplugged). Returns { bounds, maximized }.
function restorePlacement (entry, displays, workAreaAt) {
  const b = entry && entry.bounds
  const maximized = !!(entry && entry.maximized)
  if (b && [b.x, b.y, b.width, b.height].every(finite)) {
    const sized = {
      x: Math.round(b.x),
      y: Math.round(b.y),
      width: Math.max(MIN_SIZE.width, Math.round(b.width)),
      height: Math.max(MIN_SIZE.height, Math.round(b.height)),
    }
    if (onSomeDisplay(sized, displays)) return { bounds: sized, maximized }
    const centre = { x: Math.round(sized.x + sized.width / 2), y: Math.round(sized.y + sized.height / 2) }
    return { bounds: clampToWorkArea(sized, workAreaAt(centre)), maximized }
  }
  return { bounds: clampToWorkArea({}, workAreaAt({ x: 0, y: 0 })), maximized }
}

// --- the saved list ----------------------------------------------------------

function cleanBounds (b) {
  if (!b || typeof b !== 'object') return null
  const r = { x: b.x, y: b.y, width: b.width, height: b.height }
  return Object.values(r).every(finite) ? r : null
}

// What window-state.json's `popouts` holds, made safe to act on: entries with
// a title, one per title, at most MAX_SAVED.
function savedPopouts (raw) {
  const out = []
  const seen = new Set()
  for (const e of Array.isArray(raw) ? raw : []) {
    if (!e || !isTitle(e.title) || seen.has(e.title)) continue
    seen.add(e.title)
    out.push({ title: e.title, bounds: cleanBounds(e.bounds), maximized: !!e.maximized })
    if (out.length >= MAX_SAVED) break
  }
  return out
}

// The list with `entry` in it (replacing its title's old entry, in place).
function upsertSaved (list, entry) {
  const i = list.findIndex((e) => e.title === entry.title)
  if (i < 0) return list.concat([entry])
  const next = list.slice()
  next[i] = entry
  return next
}

function removeSaved (list, title) {
  return list.filter((e) => e.title !== title)
}

// The list with `from`'s entry now `to`'s (a `to` already there gives way).
function renameSaved (list, from, to) {
  return list.filter((e) => e.title !== to).map((e) => (e.title === from ? Object.assign({}, e, { title: to }) : e))
}

// --- the registry ------------------------------------------------------------

// deps:
//   create(title, { bounds, maximized, restored }) -> window   build + wire one
//   boundsOf(window) -> { bounds, maximized } | null           its place now
//   write(savedList)                                           persist it
//   changed(titles)                                            tell every window
//   retitle(window, title)                                     the OS title
//   timers: { setTimeout, clearTimeout }                       (tests)
// A window here needs on('closed', fn), close(), focus(), show(),
// isMinimized(), restore(), isDestroyed().
function createRegistry (deps) {
  const timers = deps.timers || { setTimeout, clearTimeout }
  const wins = new Map()   // title -> window
  let saved = []           // [{ title, bounds, maximized }]
  let keep = false         // closing for a quit: leave the saved list alone
  let saveTimer = null

  const live = (w) => !!w && !(w.isDestroyed && w.isDestroyed())
  const titles = () => [...wins.keys()]
  const titleOf = (w) => {
    for (const [t, x] of wins) if (x === w) return t
    return null
  }

  // Refresh each open pop-out's saved place, then write the list.
  function saveNow () {
    if (saveTimer) { timers.clearTimeout(saveTimer); saveTimer = null }
    for (const [t, w] of wins) {
      if (!live(w)) continue
      let at = null
      try { at = deps.boundsOf(w) } catch (e) { at = null }
      if (at && at.bounds) saved = upsertSaved(saved, { title: t, bounds: cleanBounds(at.bounds), maximized: !!at.maximized })
    }
    try { deps.write(saved.map((e) => Object.assign({}, e))) } catch (e) { /* best effort */ }
  }

  function saveSoon () {
    if (saveTimer) timers.clearTimeout(saveTimer)
    saveTimer = timers.setTimeout(() => { saveTimer = null; saveNow() }, SAVE_DEBOUNCE_MS)
  }

  function announce () {
    try { deps.changed(titles()) } catch (e) { /* a window mid-teardown */ }
  }

  function focus (title) {
    const w = wins.get(title)
    if (!live(w)) return false
    if (w.isMinimized()) w.restore()
    w.show()
    w.focus()
    return true
  }

  // Build the window for `title` and track it until it closes.
  function spawn (title, place) {
    const w = deps.create(title, place)
    wins.set(title, w)
    w.on('closed', () => {
      const t = titleOf(w)
      if (t == null) return
      wins.delete(t)
      if (!keep) {
        saved = removeSaved(saved, t)
        saveNow()
      }
      announce()
    })
    return w
  }

  return {
    // The saved list read at launch. Ignored once windows are open (a second
    // main window on macOS must not reset what this run knows).
    load (raw) {
      if (wins.size) return
      saved = savedPopouts(raw)
    },
    saved: () => saved.map((e) => Object.assign({}, e)),
    list: titles,
    has: (title) => wins.has(title),
    get: (title) => wins.get(title) || null,
    titleOf,
    focus,

    // Open `title` at `bounds` (or focus it when it is out already).
    open (title, bounds) {
      if (!isTitle(title)) return false
      if (wins.has(title)) return focus(title)
      if (keep) return false
      try {
        spawn(title, { bounds, maximized: false, restored: false })
      } catch (e) {
        return false
      }
      saved = upsertSaved(saved, { title, bounds: cleanBounds(bounds), maximized: false })
      saveNow()
      announce()
      return true
    },

    // The person docked it back: close it, and forget its place.
    close (title) {
      if (!isTitle(title)) return false
      saved = removeSaved(saved, title)
      const w = wins.get(title)
      if (!live(w)) {
        saveNow()
        return false
      }
      w.close()
      return true
    },

    // The session was renamed: re-key the window and its saved place. The
    // page follows the rename itself, so nothing reloads.
    rename (from, to) {
      if (!isTitle(from) || !isTitle(to) || from === to || wins.has(to)) return false
      saved = renameSaved(saved, from, to)
      const w = wins.get(from)
      if (w) {
        wins.delete(from)
        wins.set(to, w)
        try { deps.retitle(w, to) } catch (e) { /* closing */ }
      }
      saveNow()
      if (w) announce()
      return !!w
    },

    // A pop-out moved, resized, maximized or restored.
    moved () { saveSoon() },

    // Reopen every saved pop-out not open already. `place(entry)` gives its
    // { bounds, maximized }.
    restore (place) {
      let n = 0
      for (const e of saved.slice()) {
        if (wins.has(e.title) || keep) continue
        try {
          const at = place(e)
          spawn(e.title, { bounds: at.bounds, maximized: !!at.maximized, restored: true })
          n++
        } catch (err) { /* that one stays saved for next time */ }
      }
      if (n) announce()
      return n
    },

    // The app is quitting (or the main window closed): save where each one
    // is, and from here on a closing pop-out stays in the saved list.
    keepAll () {
      if (!keep) saveNow()
      keep = true
    },

    // The main window closed: every pop-out goes with it, and comes back at
    // the next launch.
    closeAll () {
      this.keepAll()
      for (const w of [...wins.values()]) if (live(w)) w.close()
    },

    // Write any pending move now.
    flush () { if (saveTimer) saveNow() },
  }
}

module.exports = {
  DEFAULT_SIZE,
  MIN_SIZE,
  POPOUT_PARAM,
  MAX_SAVED,
  SAVE_DEBOUNCE_MS,
  isTitle,
  popoutUrl,
  windowTitle,
  offlinePage,
  clampToWorkArea,
  placeNew,
  restorePlacement,
  savedPopouts,
  upsertSaved,
  removeSaved,
  renameSaved,
  createRegistry,
}
