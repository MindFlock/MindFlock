'use strict'
// macOS first launch: the Homebrew and Apple developer-tools waits have a way
// out. "Skip for now" works while Homebrew's installer runs in Terminal (not
// only on the question screen), and the developer-tools wait can open Apple's
// installer again. main.js needs Electron, so its functions are cut out by
// name and run against stubs; the offline page's showInstall the same way.
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')

const root = path.join(__dirname, '..')
const mainJs = fs.readFileSync(path.join(root, 'main.js'), 'utf8')
const html = fs.readFileSync(path.join(root, 'offline.html'), 'utf8')

function cut (src, name) {
  const start = src.indexOf(name)
  assert.ok(start >= 0, name)
  let depth = 0
  for (let k = src.indexOf('{', start); k < src.length; k++) {
    if (src[k] === '{') depth++
    else if (src[k] === '}' && --depth === 0) return src.slice(start, k + 1)
  }
  throw new Error(name)
}

function mainSandbox (waiting) {
  const ctx = {
    install: { state: 'running', waiting, lines: [] },
    brewSkipped: false,
    stopped: 0,
    preflights: 0,
    spawned: [],
    logged: [],
  }
  ctx.installPoll = { stop () { ctx.stopped++ } }
  ctx.installLog = (l) => ctx.logged.push(l)
  ctx.darwinPreflight = () => { ctx.preflights++ }
  ctx.spawn = (cmd, args) => { ctx.spawned.push([cmd, args]); return { unref () {} } }
  vm.createContext(ctx)
  vm.runInContext(['function skipHomebrew', 'function openXcodeInstaller', 'function reopenXcodeInstaller']
    .map((n) => cut(mainJs, n)).join('\n'), ctx)
  return ctx
}

test('Skip for now works while Homebrew installs in Terminal, and stops its wait', () => {
  const ctx = mainSandbox('homebrew-run')
  assert.equal(vm.runInContext('skipHomebrew()', ctx), true)
  assert.equal(ctx.stopped, 1)
  assert.equal(ctx.installPoll, null)
  assert.equal(ctx.brewSkipped, true)
  assert.equal(ctx.install.waiting, '')
  assert.equal(ctx.preflights, 1)
})

test('Skip still works on the Homebrew question, and nowhere else', () => {
  const asked = mainSandbox('homebrew')
  assert.equal(vm.runInContext('skipHomebrew()', asked), true)
  assert.equal(asked.preflights, 1)
  for (const waiting of ['xcode', '']) {
    const ctx = mainSandbox(waiting)
    assert.equal(vm.runInContext('skipHomebrew()', ctx), false)
    assert.equal(ctx.preflights, 0)
    assert.equal(ctx.stopped, 0)
  }
  const done = mainSandbox('homebrew-run')
  done.install.state = 'failed'
  assert.equal(vm.runInContext('skipHomebrew()', done), false)
})

test("Open Apple's installer again re-runs xcode-select --install during that wait only", () => {
  const ctx = mainSandbox('xcode')
  assert.equal(vm.runInContext('reopenXcodeInstaller()', ctx), true)
  assert.deepEqual(JSON.parse(JSON.stringify(ctx.spawned)), [['xcode-select', ['--install']]])
  assert.equal(ctx.install.waiting, 'xcode')   // the running wait carries on
  assert.equal(ctx.stopped, 0)
  const other = mainSandbox('homebrew-run')
  assert.equal(vm.runInContext('reopenXcodeInstaller()', other), false)
  assert.equal(other.spawned.length, 0)
})

test('main.js answers the new action, and the first open uses the same trigger', () => {
  assert.ok(mainJs.includes("case 'xcode-reopen':\n      return { ok: reopenXcodeInstaller() }"))
  assert.ok(cut(mainJs, 'function darwinPreflight').includes('openXcodeInstaller()'))
})

function pageScript () {
  return /<script>([\s\S]*?)<\/script>/.exec(html)[1]
}

function showInstallFor (inst) {
  const els = {}
  const document = {
    getElementById (id) {
      if (!els[id]) els[id] = { id, hidden: true, textContent: '' }
      return els[id]
    },
  }
  const ctx = { document, shown: null }
  ctx.show = (s) => { ctx.shown = s }
  ctx.renderLog = () => {}
  vm.createContext(ctx)
  vm.runInContext(cut(pageScript(), 'function showInstall') + '\nshowInstall(' + JSON.stringify(inst) + ')', ctx)
  return { shown: ctx.shown, els }
}

test('the installing screen shows Skip during the Homebrew run and the reopen during Xcode', () => {
  const brew = showInstallFor({ state: 'running', waiting: 'homebrew-run', lines: [] })
  assert.equal(brew.shown, 'installing')
  assert.equal(brew.els['inst-brew-skip'].hidden, false)
  assert.equal(brew.els['xcode-reopen'].hidden, true)

  const xcode = showInstallFor({ state: 'running', waiting: 'xcode', lines: [] })
  assert.equal(xcode.els['inst-brew-skip'].hidden, true)
  assert.equal(xcode.els['xcode-reopen'].hidden, false)

  const plain = showInstallFor({ state: 'running', waiting: '', lines: [] })
  assert.equal(plain.els['inst-brew-skip'].hidden, true)
  assert.equal(plain.els['xcode-reopen'].hidden, true)

  // A failed install offers Try again, not the waits' buttons.
  const failed = showInstallFor({ state: 'failed', waiting: 'xcode', lines: [] })
  assert.equal(failed.els['xcode-reopen'].hidden, true)
  assert.equal(failed.els['do-retry'].hidden, false)
})

test('the buttons exist and are wired to the actions', () => {
  assert.ok(html.includes('<button class="action quiet" id="inst-brew-skip" hidden>Skip for now</button>'))
  assert.ok(html.includes('<button class="action quiet" id="xcode-reopen" hidden>Open Apple’s installer again</button>'))
  const script = pageScript()
  assert.ok(script.includes("wire('inst-brew-skip', function () { return window.mfdiag.action('brew-skip') })"))
  assert.ok(script.includes("wireQuiet('xcode-reopen', 'xcode-reopen', 'Opened')"))
})
