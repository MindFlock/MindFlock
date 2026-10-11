'use strict'
// The offline page can't run here (no Electron, no display), so pin what CAN
// be checked: its script parses, every state it switches between has a
// section, and the main-process contract it relies on is still there.
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')

const root = path.join(__dirname, '..')
const html = fs.readFileSync(path.join(root, 'offline.html'), 'utf8')
const mainJs = fs.readFileSync(path.join(root, 'main.js'), 'utf8')
const preloadJs = fs.readFileSync(path.join(root, 'preload.js'), 'utf8')

function pageScript() {
  const m = /<script>([\s\S]*?)<\/script>/.exec(html)
  assert.ok(m, 'offline.html has one inline <script>')
  return m[1]
}

test("offline.html's script is valid JavaScript", () => {
  // Compiling without running is the same check `node --check` makes.
  assert.doesNotThrow(() => new vm.Script(pageScript(), { filename: 'offline.html' }))
})

test('every state the page switches between has a matching <section id="s-…">', () => {
  const m = /var sections = \[([\s\S]*?)\]/.exec(pageScript())
  assert.ok(m, 'sections list found')
  const states = Array.from(m[1].matchAll(/'([a-z-]+)'/g), (x) => x[1])
  assert.ok(states.includes('starting') && states.includes('failed-to-start')
    && states.includes('needs-homebrew') && states.includes('not-installed'))
  for (const s of states) {
    assert.ok(html.includes('id="s-' + s + '"'), 'missing <section id="s-' + s + '">')
  }
})

test('the page only uses bridge calls preload exposes, and main handles each action', () => {
  const script = pageScript()
  const used = new Set(Array.from(script.matchAll(/mfdiag\.([a-zA-Z]+)\(/g), (x) => x[1]))
  const bridge = /exposeInMainWorld\('mfdiag',\s*\{([\s\S]*?)\n\}\)/.exec(preloadJs)
  assert.ok(bridge, 'mfdiag bridge in preload.js')
  const exposed = new Set(Array.from(bridge[1].matchAll(/^\s*([a-zA-Z]+):/gm), (x) => x[1]))
  for (const name of used) assert.ok(exposed.has(name), 'preload exposes mfdiag.' + name)

  // Action names reach main two ways: inline `mfdiag.action('x')` and the
  // second argument of wireQuiet('button-id', 'x', …).
  const actions = new Set([
    ...Array.from(script.matchAll(/action\('([a-z-]+)'\)/g), (x) => x[1]),
    ...Array.from(script.matchAll(/wireQuiet\('[a-z-]+', '([a-z-]+)'/g), (x) => x[1]),
  ])
  assert.ok(actions.has('retry-start') && actions.has('copy-diagnostics')
    && actions.has('brew-install') && actions.has('brew-skip'))
  for (const a of actions) assert.ok(mainJs.includes("case '" + a + "':"), 'main.js handles ' + a)
})

test('the install one-liner is pinned everywhere (no /main/install.sh literal left)', () => {
  assert.ok(!mainJs.includes('/main/install.sh'), 'main.js must build the curl from INSTALL_REF')
  assert.ok(mainJs.includes('fl.installCommand(INSTALL_REF)'))
})

test('the first-run hand-off is ?setup=1 behind the first-launch marker', () => {
  assert.ok(mainJs.includes("fl.setupQuery(firstRunDeps, isFirstLaunchDone())"))
  assert.ok(!/searchParams\.set\('setup', 'install'\)/.test(mainJs))
  assert.ok(mainJs.includes("'first-launch.json'"))
})

test('the Gatekeeper note is on the not-installed state and macOS-gated', () => {
  assert.ok(html.includes('id="ni-gatekeeper"'))
  assert.ok(html.includes('Privacy &amp; Security'))
  assert.ok(/ni-gatekeeper'\)\.hidden = \(diag\.platform !== 'darwin'\)/.test(pageScript()))
})
