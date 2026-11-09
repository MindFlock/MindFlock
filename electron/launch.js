'use strict'
// How this shell is launched, and how it launches a WSL / POSIX server —
// pure helpers (no Electron), shared with electron/test.
//
//   * LAUNCH_FLAGS / applyLaunchFlags: the `--mindflock-…=` flags a Windows
//     shortcut passes instead of environment variables.
//   * devProfile / devAumidSuffix / devNameSuffix / devShortcutArgs: one dev
//     PROFILE = one taskbar identity (AUMID) + one Start-menu shortcut that
//     relaunches exactly this run. Two dev shells (WSL and native Windows)
//     sharing one AUMID shared one taskbar group and one shortcut, so the pin
//     opened whichever shell had run last.
//   * serverGuard: the if/elif/else/fi that decides whether the bootstrap
//     launches a server at all.

const crypto = require('crypto')

// Flag -> environment variable. Order is the order devShortcutArgs writes
// them in. A shortcut can pass arguments but not environment, and a .bat
// (the only way to set it) opens a console window every time.
const LAUNCH_FLAGS = [
  ['--mindflock-url=', 'MINDFLOCK_URL'],
  ['--mindflock-engine=', 'MINDFLOCK_DESKTOP_ENGINE'],
  ['--mindflock-profile=', 'MINDFLOCK_DEV_PROFILE'],
  ['--mindflock-repo=', 'MINDFLOCK_REPO'],
]

// The flag wins over an inherited variable: it is what this shortcut asked
// for, while the environment may be another launcher's.
function applyLaunchFlags (argv, env) {
  for (const [flag, name] of LAUNCH_FLAGS) {
    const arg = (argv || []).find((a) => typeof a === 'string' && a.startsWith(flag))
    if (arg) env[name] = arg.slice(flag.length)
  }
  return env
}

// The default dev profile: its identity is the one that predates profiles
// (ai.mindflock.desktop.dev, MindFlock-dev.lnk), so the owner's existing pin
// and shortcut keep working. main.js holds those base literals; every other
// profile appends a suffix derived from its name.
const DEFAULT_DEV_PROFILE = 'MindFlock (dev)'

function devProfile (env) {
  return String((env && env.MINDFLOCK_DEV_PROFILE) || '').trim() || DEFAULT_DEV_PROFILE
}

function isDefaultProfile (profile) {
  const s = String(profile || '').trim()
  return !s || s === DEFAULT_DEV_PROFILE
}

// What distinguishes a profile: "MindFlock (native)" -> "native"; any other
// name is taken whole.
function profileLabel (profile) {
  const s = String(profile || '').trim()
  const m = /^mindflock\s*\((.*)\)$/i.exec(s)
  return (m ? m[1] : s).trim()
}

// Lowercase, non-alphanumerics -> '-', trimmed. A name with nothing usable
// left (all punctuation, or non-Latin) still needs a stable, distinct id.
function profileSlug (profile) {
  const slug = profileLabel(profile).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '')
  return slug || 'p' + crypto.createHash('sha1').update(String(profile)).digest('hex').slice(0, 8)
}

// Appended to the dev AUMID: '' for the default profile, '.native' for
// "MindFlock (native)". STABLE per profile: a pinned taskbar button is keyed
// on the AUMID.
function devAumidSuffix (profile) {
  return isDefaultProfile(profile) ? '' : '.' + profileSlug(profile)
}

// Appended to the Start-menu shortcut's base name (also the name Windows
// prints above that profile's toasts): '' for the default profile,
// ' (native)' for "MindFlock (native)". Filesystem-safe on Windows.
function devNameSuffix (profile) {
  if (isDefaultProfile(profile)) return ''
  const label = profileLabel(profile)
    // eslint-disable-next-line no-control-regex
    .replace(/[<>:"/\\|?*\x00-\x1f]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/[. ]+$/, '')
  return ' (' + (label || profileSlug(profile)) + ')'
}

// Quote one argument for the Windows command line (CommandLineToArgvW
// rules): wrap it when it has whitespace or a quote (or `force`), escape a
// quote as \", and double the backslashes that precede a quote or the
// closing quote.
function quoteWinArg (arg, force) {
  const s = String(arg)
  if (!force && s !== '' && !/[\s"]/.test(s)) return s
  let out = '"'
  let slashes = 0
  for (const ch of s) {
    if (ch === '\\') { slashes++; continue }
    if (ch === '"') { out += '\\'.repeat(slashes * 2 + 1) + '"' } else { out += '\\'.repeat(slashes) + ch }
    slashes = 0
  }
  return out + '\\'.repeat(slashes * 2) + '"'
}

// The shortcut's Arguments: the app dir, the dev flag (main.js passes
// --mindflock-dev) and every launch flag this run has,
// from its own flags or its inherited environment (applyLaunchFlags has
// already folded the flags into env), so the shortcut reproduces the launch
// without a .bat.
function devShortcutArgs (appPath, devFlag, env) {
  const parts = [quoteWinArg(appPath, true), devFlag]
  for (const [flag, name] of LAUNCH_FLAGS) {
    const v = String((env && env[name]) || '').trim()
    if (v) parts.push(quoteWinArg(flag + v))
  }
  return parts.join(' ')
}

// The bootstrap's decision, as one balanced shell `if … fi;` that starts with
// a space (it is spliced after the banner):
//   * an ENABLED systemd user unit owns the port (the installed setup; the
//     2026-07-14 reboot-loop incident: never race it) -> launch nothing;
//     checked via its enable symlink, which exists before the user manager is
//     up. NOT for a developer checkout (devRepo): that serves its OWN port
//     (e.g. :9000) from the checkout, which the unit never binds, so the check
//     only ever wedged the dev server off after a WSL restart;
//   * something already answers on the port -> launch nothing;
//   * otherwise run `launch` — in the foreground on a native host (its exit
//     reaches our 'exit' handler), in the background under WSL (a keepalive
//     follows).
function serverGuard ({ port, devRepo, launch, nativeHost }) {
  const clauses = []
  if (!devRepo) {
    clauses.push(['[ -e "$HOME/.config/systemd/user/default.target.wants/mindflock.service" ]',
      'echo "systemd mindflock.service owns port ' + port + '; not launching a second server."'])
  }
  clauses.push(['(exec 3<>/dev/tcp/127.0.0.1/' + port + ') 2>/dev/null',
    'echo "a server is already listening on port ' + port + '; not launching another."'])
  return clauses.map(([cond, then], i) => (i ? ' elif ' : ' if ') + cond + '; then ' + then + ';').join('')
    + (nativeHost ? ' else ' + launch + '; fi;' : ' else (' + launch + ') & fi;')
}

module.exports = {
  LAUNCH_FLAGS,
  applyLaunchFlags,
  DEFAULT_DEV_PROFILE,
  devProfile,
  devAumidSuffix,
  devNameSuffix,
  devShortcutArgs,
  quoteWinArg,
  serverGuard,
}
