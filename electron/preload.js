'use strict'
// Bridges the title-bar buttons to the main process. contextIsolation is on, so
// the renderer only sees this narrow, safe surface (no Node).
const { contextBridge, ipcRenderer, clipboard } = require('electron')

// Shell identity: lets the (server-served, hence shared) frontend know it is
// running inside the isolated dev shell, so the UI can label itself
// "MindFlock-DEV". Undefined in a plain browser or the prod shell → the UI
// shows the normal wordmark.
contextBridge.exposeInMainWorld('mfshell', {
  // main forwards '--mindflock-dev' via webPreferences.additionalArguments
  // whenever dev mode is on (from either the env var or the CLI flag), so a
  // single argv check here covers both launch styles.
  dev: process.argv.includes('--mindflock-dev') || process.env.MINDFLOCK_DEV === '1',
  // The UI shows an in-app uninstall only inside the desktop shell, and its
  // wording differs per OS (Windows routes to Add/Remove Programs), so surface
  // the platform. Undefined in a plain browser → the UI hides the control.
  platform: process.platform,
})

// App lifecycle from the UI: the in-app "Uninstall MindFlock" control
// (Settings → Advanced) invokes this. main runs the engine teardown and, on
// mac/Linux, removes the shell and quits; on Windows it routes to Add/Remove
// Programs (whose customUnInstall clears the engine).
contextBridge.exposeInMainWorld('mfapp', {
  uninstall: () => ipcRenderer.invoke('app:uninstall'),
})

contextBridge.exposeInMainWorld('winctl', {
  minimize: () => ipcRenderer.send('win:minimize'),
  toggleMaximize: () => ipcRenderer.send('win:toggle-maximize'),
  close: () => ipcRenderer.send('win:close'),
  onMaximizedChanged: (cb) =>
    ipcRenderer.on('maximized-changed', (_e, isMax) => cb(isMax)),
})

// Offline-page diagnostics: lets offline.html explain WHY the UI can't load
// (WSL down vs mindflock not installed vs server booting) and offer a
// one-click WSL restart instead of telling the user to run shell commands.
// It also drives the one-click engine install. Note that installState() is
// POLLED rather than pushed: the main process owns the installer's output
// buffer, because an install outlives any single render of this page and
// anything held in the renderer would go with it.
contextBridge.exposeInMainWorld('mfdiag', {
  get: () => ipcRenderer.invoke('diag:get'),
  restartWsl: () => ipcRenderer.invoke('diag:restart-wsl'),
  install: () => ipcRenderer.invoke('install:start'),
  installState: () => ipcRenderer.invoke('install:state'),
})

// Update bridge: the injected bottom-right toast (see UPDATE_JS in main.js)
// uses this to learn about a newer release, open the download page, or skip a
// version. `get()` pulls the current state (covers a push that fired before
// this page's listener existed); `onAvailable` receives later pushes.
contextBridge.exposeInMainWorld('mfupdate', {
  get: () => ipcRenderer.invoke('update:get'),
  onAvailable: (cb) => ipcRenderer.on('update:available', (_e, info) => cb(info)),
  openDownload: (url) => ipcRenderer.send('update:open', url),
  skip: (version) => ipcRenderer.send('update:skip', version),
})

// Engine-drift bridge: the shell pins the engine to its own version at install
// time but only installs when the engine is missing, so app-only updates leave
// an old engine running (and `curl install.sh | sh` can leave a newer one).
// `install()` re-runs the pinned installer; `installState()` is polled for its
// output, for the same reason mfdiag polls — the install outlives the page.
contextBridge.exposeInMainWorld('mfengine', {
  get: () => ipcRenderer.invoke('engine:get'),
  onNotice: (cb) => ipcRenderer.on('engine:notice', (_e, info) => cb(info)),
  install: () => ipcRenderer.invoke('engine:install'),
  installState: () => ipcRenderer.invoke('engine:install-state'),
  dismiss: () => ipcRenderer.send('engine:dismiss'),
  // { available, current, latest } — Settings → Advanced polls this to show an
  // "update engine" control when the installed engine is behind the latest
  // released one.
  updateInfo: () => ipcRenderer.invoke('engine:update-info'),
  // Called by the drift toast once an engine update finishes: stop the old
  // server, respawn the new one, and reconnect — so the update self-completes
  // instead of asking the user to quit and reopen.
  restart: () => ipcRenderer.invoke('engine:restart'),
})

// Native clipboard bridge. Electron blocks navigator.clipboard.readText() in
// the renderer by default, so terminal right-click paste reads/writes through
// this instead (the web build falls back to navigator.clipboard).
contextBridge.exposeInMainWorld('mfclip', {
  readText: () => clipboard.readText(),
  writeText: (t) => clipboard.writeText(String(t == null ? '' : t)),
  // Clipboard image as base64 PNG ('' = no image) — powers ctrl+V image paste
  // in terminal panes (navigator.clipboard.read() is blocked like readText).
  readImagePNG: () => {
    const img = clipboard.readImage()
    return img.isEmpty() ? '' : img.toPNG().toString('base64')
  },
})
