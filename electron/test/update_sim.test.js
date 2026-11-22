'use strict'
// End-to-end simulation of the PACKAGED Linux app updating itself, with the
// real electron-updater (the version package-lock.json pins) configured the
// way main.js configures it (autoDownload on, autoInstallOnAppQuit off,
// quitAndInstall(true, true)). Only the network transport and the Electron
// `app` object are stand-ins: a local HTTP server plays the GitHub release
// (latest-linux.yml + MindFlock.AppImage), electron's `net` is swapped for
// node's http, and a fake "old" AppImage in a temp dir is what gets replaced.
// Nothing touches GitHub, a real install, or ~/.mindflock.
//
// Needs `npm ci` in electron/ (ELECTRON_SKIP_BINARY_DOWNLOAD=1 is enough);
// skipped without it, and off Linux (the AppImage updater is Linux-only).
// See docs/desktop-updates.md.
const test = require('node:test')
const assert = require('node:assert/strict')
const crypto = require('node:crypto')
const fs = require('node:fs')
const http = require('node:http')
const os = require('node:os')
const path = require('node:path')
const Module = require('node:module')
const { EventEmitter } = require('node:events')

let updaterLib = null
try { updaterLib = require('electron-updater/out/AppImageUpdater') } catch (e) { updaterLib = null }
const skip = !updaterLib ? 'electron-updater not installed (npm ci in electron/)'
  : process.platform !== 'linux' ? 'the AppImage updater runs on Linux only' : false

// BaseUpdater.quitAndInstall does require('electron').autoUpdater.emit(...)
// right before app.quit(); under plain node there is no electron runtime, so
// that one require resolves to a stub.
const electronStub = { autoUpdater: new EventEmitter() }
const origLoad = Module._load
Module._load = function (request, ...rest) {
  if (request === 'electron') return electronStub
  return origLoad.call(this, request, ...rest)
}

function sha512b64 (buf) { return crypto.createHash('sha512').update(buf).digest('base64') }

function appImage (version) {
  // A shell script stands in for the AppImage: when the updater relaunches
  // it, it records which version started and how.
  return Buffer.from('#!/bin/sh\necho "started ' + version +
    ' silent=$APPIMAGE_SILENT_INSTALL" >> "$(dirname "$0")/launches.log"\n' +
    '# padding ' + 'x'.repeat(4096) + '\n')
}

function latestYml (version, buf, shaOf) {
  const sha = sha512b64(shaOf || buf)
  return 'version: ' + version + '\nfiles:\n  - url: MindFlock.AppImage\n    sha512: ' + sha +
    '\n    size: ' + buf.length + '\npath: MindFlock.AppImage\nsha512: ' + sha +
    "\nreleaseDate: '2026-11-21T02:23:22.583Z'\n"
}

// The release feed. `mode`: ok | tampered | truncated | no-yml | same.
function startFeed (mode, newVersion) {
  const body = appImage(newVersion)
  const served = mode === 'tampered' ? Buffer.concat([body.subarray(0, 100), Buffer.from('EVIL'), body.subarray(104)]) : body
  const yml = latestYml(mode === 'same' ? '0.14.24' : newVersion, body)
  const hits = []
  const server = http.createServer((req, res) => {
    hits.push(req.url.split('?')[0])
    const p = req.url.split('?')[0]
    if (p === '/latest-linux.yml') {
      if (mode === 'no-yml') { res.writeHead(404); return res.end('Not Found') }
      res.writeHead(200, { 'Content-Type': 'text/yaml' })
      return res.end(yml)
    }
    if (p === '/MindFlock.AppImage') {
      if (req.headers.range) { res.writeHead(416); return res.end() }
      res.writeHead(200, { 'Content-Length': served.length, 'Content-Type': 'application/octet-stream' })
      if (mode === 'truncated') {
        res.write(served.subarray(0, served.length >> 1))
        return setTimeout(() => res.destroy(), 20)
      }
      return res.end(served)
    }
    res.writeHead(404); res.end()
  })
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () =>
    resolve({ server, port: server.address().port, hits, body })))
}

// A packaged MindFlock 0.14.24 on disk: the AppImage, the app-update.yml
// electron-builder puts in resources/, userData and the updater cache.
function oldInstall (port) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'mf-appimage-'))
  const appImagePath = path.join(root, 'Applications', 'MindFlock.AppImage')
  fs.mkdirSync(path.dirname(appImagePath), { recursive: true })
  fs.writeFileSync(appImagePath, appImage('0.14.24'), { mode: 0o755 })
  const cfg = path.join(root, 'resources', 'app-update.yml')
  fs.mkdirSync(path.dirname(cfg), { recursive: true })
  fs.writeFileSync(cfg, 'provider: generic\nurl: http://127.0.0.1:' + port +
    '/\nupdaterCacheDirName: mindflock-desktop-updater\n')
  return { root, appImagePath, cfg }
}

function makeUpdater (inst) {
  const { AppImageUpdater } = updaterLib
  const { ElectronHttpExecutor } = require('electron-updater/out/electronHttpExecutor')
  // electron's net, swapped for node's http: the network boundary.
  class NodeExecutor extends ElectronHttpExecutor {
    createRequest (options, callback) {
      const req = http.request(options)
      req.on('response', callback)
      return req
    }

    addRedirectHandlers () {}
  }
  const quits = []
  const app = {
    version: '0.14.24',
    name: 'MindFlock',
    isPackaged: true,
    appUpdateConfigPath: inst.cfg,
    userDataPath: path.join(inst.root, 'userData'),
    baseCachePath: path.join(inst.root, 'cache'),
    whenReady: () => Promise.resolve(),
    relaunch: () => {},
    quit: () => quits.push(Date.now()),
    onQuit: () => {},
  }
  const u = new AppImageUpdater(null, app)
  u.httpExecutor = new NodeExecutor()
  u.logger = null
  // Exactly main.js's initAutoUpdater settings.
  u.autoDownload = true
  u.autoInstallOnAppQuit = false
  // main.js's appUpd state machine, driven by the same events.
  const appUpd = { state: 'idle', version: '', error: '' }
  u.on('update-available', (info) => { appUpd.state = 'downloading'; appUpd.version = info.version })
  u.on('update-not-available', () => { appUpd.checked = true; if (appUpd.state !== 'ready') appUpd.state = 'idle' })
  u.on('update-downloaded', (info) => { appUpd.state = 'ready'; appUpd.version = info.version })
  u.on('error', (err) => { appUpd.state = 'error'; appUpd.error = String(err && err.message || err) })
  return { u, appUpd, quits }
}

// What main.js's checkAppUpdate does: the 'error' event carries the reason,
// so the background download's own promise is caught, not left unhandled.
async function check (u) {
  const res = await u.checkForUpdates().catch(() => null)
  if (res && res.downloadPromise) res.downloadPromise.catch(() => {})
  return res
}

async function settle (appUpd, ms) {
  const end = Date.now() + (ms || 15000)
  while (Date.now() < end && !appUpd.checked && (appUpd.state === 'idle' || appUpd.state === 'downloading')) {
    await new Promise((r) => setTimeout(r, 50))
  }
}

async function run (mode, t) {
  const feed = await startFeed(mode, '0.14.25')
  const inst = oldInstall(feed.port)
  const saved = process.env.APPIMAGE
  process.env.APPIMAGE = inst.appImagePath
  t.after(() => {
    feed.server.close()
    if (saved === undefined) delete process.env.APPIMAGE; else process.env.APPIMAGE = saved
    fs.rmSync(inst.root, { recursive: true, force: true })
  })
  const before = fs.readFileSync(inst.appImagePath)
  const h = makeUpdater(inst)
  await check(h.u)
  await settle(h.appUpd)
  // A failed check can land a beat after the promise; let it.
  await new Promise((r) => setTimeout(r, 100))
  return Object.assign(h, { feed, inst, before })
}

function launches (inst) {
  try { return fs.readFileSync(path.join(path.dirname(inst.appImagePath), 'launches.log'), 'utf8') } catch (e) { return '' }
}

test('a verified download replaces the AppImage in place and relaunches it', { skip }, async (t) => {
  const r = await run('ok', t)
  assert.equal(r.appUpd.state, 'ready', r.appUpd.error)
  assert.equal(r.appUpd.version, '0.14.25')
  // Nothing on disk changes until the person (or the away-from-keyboard
  // run) installs it: autoInstallOnAppQuit is off.
  assert.deepEqual(fs.readFileSync(r.inst.appImagePath), r.before)
  r.u.quitAndInstall(true, true)
  await new Promise((res) => setTimeout(res, 500))
  const now = fs.readFileSync(r.inst.appImagePath)
  assert.deepEqual(now, r.feed.body, 'the AppImage on disk is the new release')
  assert.ok(fs.statSync(r.inst.appImagePath).mode & 0o100, 'still executable')
  assert.equal(r.quits.length, 1, 'the old app quits')
  assert.match(launches(r.inst), /started 0\.14\.25 silent=true/, 'the new AppImage was started')
})

test('a download whose sha512 does not match latest-linux.yml is refused', { skip }, async (t) => {
  const r = await run('tampered', t)
  assert.equal(r.appUpd.state, 'error')
  assert.match(r.appUpd.error, /sha512 checksum mismatch/)
  assert.deepEqual(fs.readFileSync(r.inst.appImagePath), r.before, 'the installed app is untouched')
  // Nothing downloaded: an install now has nothing to apply and says so.
  r.u.quitAndInstall(true, true)
  await new Promise((res) => setTimeout(res, 100))
  assert.equal(r.quits.length, 0, 'the app does not quit')
  assert.deepEqual(fs.readFileSync(r.inst.appImagePath), r.before)
})

test('a download cut off halfway is an error, never a half-written app', { skip }, async (t) => {
  const r = await run('truncated', t)
  assert.equal(r.appUpd.state, 'error')
  assert.deepEqual(fs.readFileSync(r.inst.appImagePath), r.before)
  r.u.quitAndInstall(true, true)
  await new Promise((res) => setTimeout(res, 100))
  assert.equal(r.quits.length, 0)
})

test('a release without latest-linux.yml yet (assets still uploading) is an error, app untouched', { skip }, async (t) => {
  const r = await run('no-yml', t)
  assert.equal(r.appUpd.state, 'error')
  assert.ok(!r.feed.hits.includes('/MindFlock.AppImage'), 'nothing downloaded')
  assert.deepEqual(fs.readFileSync(r.inst.appImagePath), r.before)
})

test('the same version is no update', { skip }, async (t) => {
  const r = await run('same', t)
  assert.equal(r.appUpd.state, 'idle')
  assert.ok(!r.feed.hits.includes('/MindFlock.AppImage'))
})

test('offline (nothing listening) is an error, app untouched', { skip }, async (t) => {
  const inst = oldInstall(1)   // port 1: connection refused
  const saved = process.env.APPIMAGE
  process.env.APPIMAGE = inst.appImagePath
  t.after(() => {
    if (saved === undefined) delete process.env.APPIMAGE; else process.env.APPIMAGE = saved
    fs.rmSync(inst.root, { recursive: true, force: true })
  })
  const before = fs.readFileSync(inst.appImagePath)
  const h = makeUpdater(inst)
  await check(h.u)
  await settle(h.appUpd)
  assert.equal(h.appUpd.state, 'error')
  assert.deepEqual(fs.readFileSync(inst.appImagePath), before)
})

test('without APPIMAGE set the updater stays silent: no event at all', { skip }, async (t) => {
  const feed = await startFeed('ok', '0.14.25')
  const inst = oldInstall(feed.port)
  const saved = process.env.APPIMAGE
  delete process.env.APPIMAGE
  t.after(() => {
    feed.server.close()
    if (saved !== undefined) process.env.APPIMAGE = saved
    fs.rmSync(inst.root, { recursive: true, force: true })
  })
  const h = makeUpdater(inst)
  const res = await h.u.checkForUpdates()
  assert.equal(res, null)
  // main.js's appUpd stays 'idle' forever here, so updateEverything must not
  // wait out its whole download deadline for an event that never comes.
  assert.equal(h.appUpd.state, 'idle')
})

// --- which release asset each OS reads and downloads ------------------------
// The v0.14.25 release's own latest*.yml, verbatim (sha512s trimmed). These
// names are the version-less ones the README buttons depend on: the updater
// must keep finding them.
const RELEASE_YML = {
  win32: 'version: 0.14.25\nfiles:\n  - url: MindFlock-Setup.exe\n    sha512: x\n    size: 121854894\npath: MindFlock-Setup.exe\nsha512: x\n',
  darwin: 'version: 0.14.25\nfiles:\n  - url: MindFlock.zip\n    sha512: x\n    size: 217777735\n  - url: MindFlock.dmg\n    sha512: y\n    size: 224827570\npath: MindFlock.zip\nsha512: x\n',
  linux: 'version: 0.14.25\nfiles:\n  - url: MindFlock.AppImage\n    sha512: x\n    size: 126458758\n    blockMapSize: 134042\npath: MindFlock.AppImage\nsha512: x\n',
}

test('each OS reads its own latest*.yml and downloads the right installer', { skip: updaterLib ? false : skip }, () => {
  const { GenericProvider } = require('electron-updater/out/providers/GenericProvider')
  const { findFile } = require('electron-updater/out/providers/Provider')
  const { MacUpdater } = require('electron-updater/out/MacUpdater')
  const yaml = require('js-yaml')
  const cases = [
    // platform, channel file, what the updater class looks for, the asset
    ['win32', 'latest', ['exe'], 'MindFlock-Setup.exe'],
    ['darwin', 'latest-mac', ['zip', ['pkg', 'dmg']], 'MindFlock.zip'],
    ['linux', 'latest-linux', ['AppImage', ['rpm', 'deb', 'pacman']], 'MindFlock.AppImage'],
  ]
  const savedArch = process.env.TEST_UPDATER_ARCH
  process.env.TEST_UPDATER_ARCH = 'x64'
  try {
    for (const [platform, channel, find, asset] of cases) {
      const p = new GenericProvider({ provider: 'generic', url: 'https://example.invalid/dl/' },
        { channel: null, isAddNoCacheQuery: false }, { platform, isUseMultipleRangeRequest: false, executor: null })
      assert.equal(p.channel, channel, platform)
      let files = p.resolveFiles(yaml.load(RELEASE_YML[platform]))
      // macOS: Apple silicon and Intel both keep the universal zip.
      if (platform === 'darwin') {
        assert.deepEqual(MacUpdater.filterFilesForArch(files, true).map((f) => f.url.pathname), ['/dl/MindFlock.zip', '/dl/MindFlock.dmg'])
        files = MacUpdater.filterFilesForArch(files, false)
      }
      const f = findFile(files, find[0], find[1])
      assert.equal(path.basename(f.url.pathname), asset, platform)
    }
    // Linux on arm64 asks for a channel the release doesn't publish (there is
    // no arm64 AppImage): an error, so the toast falls back to the page.
    process.env.TEST_UPDATER_ARCH = 'arm64'
    const arm = new GenericProvider({ provider: 'generic', url: 'https://example.invalid/' },
      { channel: null }, { platform: 'linux', isUseMultipleRangeRequest: false, executor: null })
    assert.equal(arm.channel, 'latest-linux-arm64')
  } finally {
    if (savedArch === undefined) delete process.env.TEST_UPDATER_ARCH; else process.env.TEST_UPDATER_ARCH = savedArch
  }
})
