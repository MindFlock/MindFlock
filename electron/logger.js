'use strict'
// File logging for the desktop shell.
//
// The packaged app has no console (it's launched from a shortcut / VBS, not a
// terminal), so console.log() output is lost. This module tees console output
// to a rotating file under the app's userData dir and installs handlers that
// capture the things that actually go wrong in the field: renderer console
// errors, failed loads, GPU/renderer crashes, and unhandled exceptions.
//
// Log locations (Windows):
//   %APPDATA%\MindFlock\logs\main.log        <- this process (rotates to .1)
//   The WSL server writes its own log inside WSL (see main.js / README).

const fs = require('fs')
const path = require('path')

const MAX_BYTES = 2 * 1024 * 1024 // rotate main.log past 2 MB (keep one .1 backup)

let logDir = null
let logPath = null
let written = 0 // bytes in the current main.log — tracked in-process so rotation
                // doesn't depend on any on-disk-size read.
const origConsole = {}

function ts() {
  // Local ISO-ish stamp: 2026-07-06 11:54:03.482
  const d = new Date()
  const p = (n, w = 2) => String(n).padStart(w, '0')
  return (
    d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' +
    p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds()) + '.' +
    p(d.getMilliseconds(), 3)
  )
}

function fmt(args) {
  return args
    .map((a) => {
      if (a instanceof Error) return a.stack || String(a)
      if (typeof a === 'object' && a !== null) {
        try { return JSON.stringify(a) } catch (e) { return String(a) }
      }
      return String(a)
    })
    .join(' ')
}

// Rotate before appending `nextLen` bytes would push main.log past the cap.
// Keeps exactly one previous file (main.log.1) so the log never grows unbounded.
// Synchronous rename with no fd held open, so it can't race a buffered stream.
function rotateIfNeeded(nextLen) {
  if (!logPath) return
  if (written + nextLen <= MAX_BYTES) return
  try {
    fs.renameSync(logPath, logPath + '.1') // overwrites any older .1
  } catch (e) {
    // Nothing to rotate yet (no file) — the next append recreates main.log.
  }
  written = 0
}

// Synchronous append: guarantees the line is on disk before we return, so a
// FATAL line written just before the process dies is never lost to a buffer.
function writeLine(level, args) {
  if (!logPath) return // not initialised — console-only until init()
  const line = ts() + ' [' + level + '] ' + fmt(args) + '\n'
  try {
    const len = Buffer.byteLength(line)
    rotateIfNeeded(len)
    fs.appendFileSync(logPath, line)
    written += len
  } catch (e) {
    // Never let logging crash the app.
  }
}

// Initialise once, as early as possible. `app` is passed in so this module has
// no import-order dependency on electron being ready.
function init(app) {
  if (logPath) return { dir: logDir, file: logPath } // idempotent
  try {
    logDir = path.join(app.getPath('userData'), 'logs')
    fs.mkdirSync(logDir, { recursive: true })
    logPath = path.join(logDir, 'main.log')
    // Continue an existing file across restarts; seed the byte counter from its
    // current size so rotation accounts for what's already there.
    try { written = fs.statSync(logPath).size } catch (e) { written = 0 }
  } catch (e) {
    // If we can't set up the file, fall back to console-only.
    logPath = null
  }

  // Tee console.* to the file while preserving the original behaviour.
  for (const level of ['log', 'info', 'warn', 'error']) {
    origConsole[level] = console[level].bind(console)
    console[level] = (...args) => {
      writeLine(level.toUpperCase(), args)
      try { origConsole[level](...args) } catch (e) {}
    }
  }

  // Last-resort catch-alls for the main process.
  process.on('uncaughtException', (err) => writeLine('FATAL', ['uncaughtException', err]))
  process.on('unhandledRejection', (reason) => writeLine('FATAL', ['unhandledRejection', reason]))

  const banner =
    '===== MindFlock desktop start ' + ts() + ' ===== ' +
    'v' + (app.getVersion ? app.getVersion() : '?') +
    ' electron ' + process.versions.electron +
    ' ' + process.platform + '/' + process.arch
  writeLine('BOOT', [banner])

  return { dir: logDir, file: logPath }
}

// Attach per-window webContents diagnostics (renderer console + crashes).
function attachWindow(win) {
  const wc = win.webContents
  wc.on('console-message', (_e, level, message, line, sourceId) => {
    // level: 0 verbose 1 info 2 warning 3 error — only tee warnings+ to avoid
    // drowning the log in the UI's own debug chatter.
    if (level < 2) return
    const tag = level >= 3 ? 'RENDERER-ERROR' : 'RENDERER-WARN'
    writeLine(tag, [message + ' (' + (sourceId || '?') + ':' + line + ')'])
  })
  wc.on('render-process-gone', (_e, details) =>
    writeLine('FATAL', ['render-process-gone', details]))
  wc.on('unresponsive', () => writeLine('WARN', ['window unresponsive']))
  wc.on('preload-error', (_e, preloadPath, err) =>
    writeLine('ERROR', ['preload-error', preloadPath, err]))
}

function paths() {
  return { dir: logDir, file: logPath }
}

module.exports = { init, attachWindow, paths }
