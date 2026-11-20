'use strict'
// Pure helpers for the desktop updater (main.js): the version compare every
// check uses, and the engine/app version-skew verdict. No electron here, so
// `node --test electron/test` can pin them (see electron/test/update.test.js).

// Compare dotted numeric versions. >0 if a newer than b, <0 older, 0 equal.
// Prerelease / non-numeric suffixes are ignored — we only nudge on a clear bump.
function cmpVersion (a, b) {
  const pa = String(a == null ? '' : a).replace(/^v/i, '').split('.').map((n) => parseInt(n, 10) || 0)
  const pb = String(b == null ? '' : b).replace(/^v/i, '').split('.').map((n) => parseInt(n, 10) || 0)
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const d = (pa[i] || 0) - (pb[i] || 0)
    if (d) return d > 0 ? 1 : -1
  }
  return 0
}

// How the running engine relates to this app: 'engine-newer' | 'engine-older'
// | 'same' | 'unknown' (either side unreadable — never a verdict).
function versionSkew (engineVersion, appVersion) {
  if (!engineVersion || !appVersion) return 'unknown'
  const d = cmpVersion(engineVersion, appVersion)
  return d > 0 ? 'engine-newer' : d < 0 ? 'engine-older' : 'same'
}

// The notice to show when the engine has run AHEAD of the app (install.sh
// re-run by hand, `mindflock update` on a machine whose app is behind), or
// null when it hasn't. With a newer app release out (`latestApp`, and not the
// one the user skipped) the app-update toast carries the message — one button
// fixes it; otherwise it is a one-line notice. The engine is never downgraded
// to match the app: that is the state.json downgrade path nobody wants.
function skewNotice (engineVersion, appVersion, latestApp, skippedVersion) {
  if (versionSkew(engineVersion, appVersion) !== 'engine-newer') return null
  const appUpdate = !!latestApp && cmpVersion(latestApp, appVersion) > 0 &&
    String(latestApp) !== String(skippedVersion || '')
  return {
    kind: 'skew',
    engine: String(engineVersion),
    app: String(appVersion),
    appUpdate,
    message: appUpdate
      ? 'Your engine (' + engineVersion + ') is newer than the app (' + appVersion + ') — update the app.'
      : 'Your engine (' + engineVersion + ') is newer than this app (' + appVersion +
        '). Update the app when a release is out; the engine is left as it is.'
  }
}

// One state for every update button (the app toast, the engine toast and
// Settings → Advanced poll the same thing): the run's phase plus the engine
// installer's log. `run` is main.js's updateRun, `install` its install state.
function runState (run, install) {
  const r = run || {}
  const inst = install || {}
  let state = 'idle'
  if (r.state === 'running') state = 'running'
  else if (r.state === 'failed') state = 'failed'
  else if (r.step) state = 'done'
  else if (inst.state === 'running') state = 'running'
  else if (inst.state === 'failed') state = 'failed'
  return {
    state,
    step: r.step || '',
    message: r.message || '',
    code: inst.code == null ? null : inst.code,
    lines: Array.isArray(inst.lines) ? inst.lines : []
  }
}

// ---------------------------------------------------------------------------
// Automatic install: "should the shell run updateEverything() by itself NOW?"
//
// main.js polls this once a minute (autoUpdateTick) with what it can see and
// does what it says. Pure on purpose: every rule below is a promise to the
// person at the keyboard, and each one is pinned in electron/test.
//
// The rules, in order (the first that applies decides):
//   1. A run from source (dev shell / unpackaged) never updates anything —
//      it is a git checkout; a release build would replace it.
//   2. The person turned automatic updates off (Settings → Advanced).
//   3. An update run is already going (theirs, or ours).
//   4. What would we install? An app release that electron-updater has FULLY
//      downloaded ('ready') — the engine moves with it inside the same run.
//      An app release that is still downloading, or that the updater can't
//      apply here (state 'error': the self-signed mac refusing the swap, a
//      build without update metadata), waits for the person: the manual
//      toast's fallback (the download page) is theirs to choose, and
//      updating the engine alone would push it AHEAD of the app. With the
//      app current, an engine behind the newest release (and not past the
//      app's own version) is the target.
//   5. Not the version they skipped, and not one we already tried this run of
//      the app (a failed or no-op attempt must not retry every minute).
//   6. Never while they're working: the screen is locked, or the system has
//      been idle (no keyboard/mouse anywhere) for `idleNeedSec`. An
//      engine-only update also goes when the window has been minimized or
//      hidden that long — it restarts the server, not the window; an app
//      update relaunches the window, which would pop up over whatever they
//      are doing, so it waits for a real idle.
//   7. Agent turns running ('working' sessions on this machine) hold it off:
//      they survive a server restart (tmux), but a turn's events, the ticket
//      pipeline and team runs are driven by the server, so wait for a quiet
//      moment — up to `workingCapMs` of waiting while away, then go anyway
//      (an overnight loop would otherwise block every update forever).
//      Not on native Windows (`holdWhileWorking`): there the engine swap
//      moves the live venv aside under the running sessions (install.ps1,
//      docs/windows.md), and for those seconds a hook or MCP server an agent
//      starts finds no engine — so a working turn holds the update off with
//      no cap, and sessions it can't count (`working` null) hold it too.
//
// `s` fields: source, enabled, running (bools); appCurrent, appState
// ('off'|'idle'|'downloading'|'ready'|'error'), appVersion (the downloaded /
// downloading one), release (latest release seen), engineCurrent,
// engineLatest, skipped, tried (versions), idleSec, locked, hiddenSec,
// working (count, or null = unknown), waitedMs (how long `working` has held
// an otherwise-ready update), idleNeedSec, workingCapMs, holdWhileWorking.
// Returns { go, kind: 'app'|'engine'|'', version, wait, why }.
const AUTO_IDLE_SEC = 10 * 60
const AUTO_WORKING_CAP_MS = 2 * 60 * 60 * 1000

function autoUpdateVerdict (s) {
  const st = s || {}
  const no = (why, extra) => Object.assign({ go: false, kind: '', version: '', wait: false, why }, extra || {})
  if (st.source) return no('running from source — update it with git')
  if (!st.enabled) return no('automatic updates are off')
  if (st.running) return no('an update is already running')

  const current = String(st.appCurrent || '')
  const appNewer = (!!st.release && cmpVersion(st.release, current) > 0) ||
    (!!st.appVersion && cmpVersion(st.appVersion, current) > 0)
  let kind = ''
  let version = ''
  if (appNewer) {
    if (st.appState !== 'ready') {
      return no(st.appState === 'error'
        ? "the app can't update itself here — the toast offers the download"
        : 'the app update is not downloaded yet')
    }
    kind = 'app'
    version = String(st.appVersion || st.release)
  } else if (st.engineLatest && st.engineCurrent && cmpVersion(st.engineLatest, st.engineCurrent) > 0 &&
      cmpVersion(st.engineLatest, current) <= 0) {
    // Only up to the app's own version: an engine release newer than this
    // app means the app is behind too (its release just not seen yet) —
    // updating the engine alone would run it ahead of the app.
    kind = 'engine'
    version = String(st.engineLatest)
  } else {
    return no('nothing to update')
  }
  const at = { kind, version }
  if (st.skipped && String(st.skipped) === version) return no('the person skipped ' + version, at)
  if ((st.tried || []).map(String).indexOf(version) !== -1) return no('already tried ' + version + ' this run', at)

  const need = st.idleNeedSec == null ? AUTO_IDLE_SEC : st.idleNeedSec
  const away = !!st.locked || (st.idleSec || 0) >= need ||
    (kind === 'engine' && (st.hiddenSec || 0) >= need)
  if (!away) return no('the person is active', at)

  if (st.holdWhileWorking && st.working == null) {
    return no("couldn't tell whether a session is working", Object.assign({ wait: true }, at))
  }
  const cap = st.holdWhileWorking ? Infinity
    : st.workingCapMs == null ? AUTO_WORKING_CAP_MS : st.workingCapMs
  const working = st.working == null ? 0 : st.working
  if (working > 0 && (st.waitedMs || 0) < cap) {
    return no(working + ' session' + (working === 1 ? ' is' : 's are') + ' working', Object.assign({ wait: true }, at))
  }
  return {
    go: true, kind, version, wait: false,
    why: working > 0 ? 'away, still working after the cap — updating anyway' : 'away and nothing working',
  }
}

// How many of this machine's sessions have an agent turn running, from a
// GET /api/instances body (null when it couldn't be read). Rows mirrored from
// other devices (`<device>::<title>`) don't count: this restart doesn't touch
// them.
function workingCount (rows) {
  if (!Array.isArray(rows)) return null
  return rows.filter((r) => r && r.activity === 'working' &&
    String(r.title || '').indexOf('::') === -1).length
}

// The one-line "what happened" notice after an update, or null. `rec` is what
// main.js persisted before the swap ({ kind, to, from, auto }); `running` is
// the version now running — an app swap that didn't land says nothing. One
// plain line whatever moved (the app, the engine, or both): the person sees
// MindFlock, not its parts.
function updatedNotice (rec, running) {
  if (!rec || !rec.to) return null
  if (rec.kind === 'app' && cmpVersion(running, rec.to) < 0) return null
  return {
    version: String(rec.to),
    auto: !!rec.auto,
    message: 'Updated to MindFlock ' + rec.to + '.',
  }
}

// ---------------------------------------------------------------------------
// The update mode: ONE setting, kept by the server (`updates.mode`, read from
// GET /api/update/check as `mode` / `mode_set` / `managed_by_shell`).
//   auto  install by itself (here: autoUpdateVerdict decides when)
//   ask   offer it: one toast, one click
//   off   no checks, no toasts
const MODES = ['auto', 'ask', 'off']
function validMode (m) { return MODES.indexOf(m) !== -1 }

// Today's behaviour, before the server knew about a mode: the shell's own
// `autoUpdate` switch in update-state.json, ON unless turned off.
function legacyMode (store) {
  return (store || {}).autoUpdate === false ? 'ask' : 'auto'
}

// The mode the shell acts on. `server` is the last /api/update/check answer
// (null while none has come back); `store` is update-state.json.
//   - the server answered with a mode            -> that mode
//   - the server answered WITHOUT one (an older  -> exactly today's switch
//     engine)
//   - no answer yet (server down / booting)      -> the mode cached from the
//                                                   last answer, else today's
function resolveMode (server, store) {
  const st = store || {}
  if (server) return validMode(server.mode) ? server.mode : legacyMode(st)
  return validMode(st.mode) ? st.mode : legacyMode(st)
}

// The one-time move of the shell's own switch onto the server: the mode to
// POST, or '' for none. Only while the server says nobody chose a mode yet
// (`mode_set: false`), and only once (`modeMigrated`). An explicit switch
// carries over as it was (on -> auto, off -> ask). No switch at all was ON
// by default in the desktop app, so a shell that manages its engine keeps
// automatic updates; a server that drives itself (a development checkout
// under this window) is not switched to auto by a default nobody chose.
function modeToMigrate (server, store) {
  const s = server || {}
  const st = store || {}
  if (!validMode(s.mode) || s.mode_set !== false || st.modeMigrated) return ''
  if (st.autoUpdate === true) return 'auto'
  if (st.autoUpdate === false) return 'ask'
  return s.managed_by_shell === true ? 'auto' : ''
}

// What the old "Install updates automatically" switch (update:set-auto) means
// now that the mode has three values: on -> auto; off -> ask when it was
// auto, otherwise unchanged (turning auto off must not undo an explicit off).
function modeForSwitch (on, current) {
  if (on) return 'auto'
  return current === 'auto' || !validMode(current) ? 'ask' : current
}

// Should the shell show its "update available" toast (app or engine) for
// `version`? `mode` is resolveMode's; `canAuto` whether the automatic path can
// install it here at all; `tried` the versions auto-attempted this run.
//   off  -> never
//   ask  -> yes
//   auto -> only when it can't install itself (the updater can't swap the app
//           here) or already tried and didn't finish: then the person is the
//           only way it lands. Otherwise they hear about it once, after.
function offerToast (o) {
  const s = o || {}
  if (s.mode === 'off') return false
  if (s.mode !== 'auto') return true
  if (!s.canAuto) return true
  return (s.tried || []).map(String).indexOf(String(s.version || '')) !== -1
}

// The local server's port, from the URL the window loads: the one every local
// request of the update code (and the launcher's probe) must use. An explicit
// port wins; a local URL without one is the scheme's default; anything else
// is the engine's default, 8765.
function localPort (url) {
  let u
  try { u = new URL(String(url)) } catch (e) { return 8765 }
  if (u.port) return Number(u.port)
  const local = ['localhost', '127.0.0.1', '[::1]'].indexOf(u.hostname) !== -1
  if (!local) return 8765
  return u.protocol === 'https:' ? 443 : 80
}

module.exports = {
  cmpVersion, versionSkew, skewNotice, runState,
  autoUpdateVerdict, workingCount, updatedNotice, AUTO_IDLE_SEC, AUTO_WORKING_CAP_MS,
  MODES, resolveMode, legacyMode, modeToMigrate, modeForSwitch, offerToast, localPort,
}
