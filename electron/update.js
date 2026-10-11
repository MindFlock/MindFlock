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
//
// `s` fields: source, enabled, running (bools); appCurrent, appState
// ('off'|'idle'|'downloading'|'ready'|'error'), appVersion (the downloaded /
// downloading one), release (latest release seen), engineCurrent,
// engineLatest, skipped, tried (versions), idleSec, locked, hiddenSec,
// working (count, or null = unknown), waitedMs (how long `working` has held
// an otherwise-ready update), idleNeedSec, workingCapMs.
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

  const cap = st.workingCapMs == null ? AUTO_WORKING_CAP_MS : st.workingCapMs
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
// the version now running — an app swap that didn't land says nothing.
function updatedNotice (rec, running) {
  if (!rec || !rec.to) return null
  if (rec.kind === 'app' && cmpVersion(running, rec.to) < 0) return null
  const what = rec.kind === 'engine' ? 'the MindFlock engine' : 'MindFlock'
  return {
    version: String(rec.to),
    auto: !!rec.auto,
    message: 'Updated ' + what + ' to ' + rec.to +
      (rec.auto ? ' while you were away.' : '.'),
  }
}

module.exports = {
  cmpVersion, versionSkew, skewNotice, runState,
  autoUpdateVerdict, workingCount, updatedNotice, AUTO_IDLE_SEC, AUTO_WORKING_CAP_MS,
}
