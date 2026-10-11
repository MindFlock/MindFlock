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

module.exports = { cmpVersion, versionSkew, skewNotice, runState }
