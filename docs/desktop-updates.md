# How the desktop app updates itself

This covers the **packaged** app: the installer someone downloads from the
releases page (`MindFlock-Setup.exe`, `MindFlock.dmg`, `MindFlock.AppImage`).
A run from source never updates itself (see `electron/README.md`,
[Updating](../electron/README.md#updating), which also covers the update
mode and the automatic-update rules). To check an update by hand on a real
Windows PC or Mac, follow [desktop-update-test.md](desktop-update-test.md).

## Two parts, one button

A packaged install is two things, and an update moves both:

| Part | What it is | Where it lives | Updated by |
|---|---|---|---|
| **The app** (the shell) | the Electron window | by default `%LOCALAPPDATA%\Programs\MindFlock` (Windows), `/Applications/MindFlock.app` (macOS), wherever the `.AppImage` was saved (Linux) | `electron-updater`, inside the app |
| **The engine** | the `mindflock` CLI and its server, a Python tool venv | `~/.local/share/uv/tools/mindflock` + `~/.local/bin/mindflock` (Linux, macOS, and inside WSL); `%APPDATA%\uv\tools\mindflock` (native Windows) | the installer bundled in the app: `install.sh`, or `install.ps1` on native Windows |

The app installs the engine on first launch, pinned to its own version
(`v<app version>`). Every update button (the toast, Settings → Advanced) and
the automatic run call one function, `updateEverything()` in
`electron/main.js`: **engine first, then the app.**

## How an update happens

1. **Learn of it.** 20 s after launch and every 30 min after, the app asks
   `api.github.com/repos/MindFlock/MindFlock/releases/latest` for `tag_name`
   and compares it with its own version (`cmpVersion` in `electron/update.js`:
   dotted numbers, `v` and any `-beta`/`+build` suffix ignored). The engine's
   version comes from the local server (`GET /api/remote/hello`) and is
   compared with the same tag. Offline or any non-200 is silent; it tries
   again next time. Update mode **Off** skips all of this.
2. **Download the app** — in the background, as soon as it is seen.
   `electron-updater` reads the release's `latest.yml` (Windows),
   `latest-mac.yml` (macOS) or `latest-linux.yml` (Linux), then downloads
   `MindFlock-Setup.exe`, `MindFlock.zip` (macOS, universal) or
   `MindFlock.AppImage`, using the `.blockmap` files to fetch only what
   changed when it can. Nothing on disk changes yet.
3. **Verify the app.** The download's SHA-512 must match the one in
   `latest*.yml`, or it is thrown away (`sha512 checksum mismatch`). On macOS,
   Squirrel.Mac also refuses a build not signed by the same (self-signed)
   identity as the running app — but only at install time, after the click
   (the app never installs on quit, so Squirrel isn't handed the update
   before then). Windows builds are not code-signed, so there is no
   Authenticode check — the SHA-512 is all there is.
4. **Update the engine** (when it is behind the target and is a release
   install). The app runs its bundled installer with
   `MINDFLOCK_INSTALL_REF=v<target>`. The installer resolves the tag to a
   commit with `git ls-remote` and has uv build exactly that commit from
   GitHub — it reads no release asset. It first **builds the new engine
   beside the old one** (a scratch tool dir), so a network drop or a broken
   build changes nothing; only then does it move the old venv aside, install
   for real from the warm cache, and put the old one back if that fails.
   (`install.sh` since this release; `install.ps1` always has.) The running
   server keeps serving meanwhile.
5. **Apply.** With an app update: stop the server if the engine changed,
   then `quitAndInstall` — Windows runs the NSIS installer silently and
   restarts the app, macOS swaps the bundle and relaunches, Linux replaces
   the `.AppImage` file in place and starts it. The new app starts the new
   engine's server. Engine only: stop the server, wait for the port to free
   up, start the new one, and wait (90 s) for it to answer with the new
   version.
6. **Tell the person.** The next page shows *Updated to MindFlock X.* once.

Release assets the updater reads: the GitHub API's `releases/latest`,
`latest.yml`, `latest-mac.yml`, `latest-linux.yml`, `MindFlock-Setup.exe` (+
`.blockmap`), `MindFlock.zip` (+ `.blockmap`), `MindFlock.AppImage`. The
`.dmg`, the `*.sha256` files, the wheel, the sdist and `SHA256SUMS` are for
people and download buttons. **Never rename an asset**: the README buttons
use `/releases/latest/download/<name>` and the `latest*.yml` files name them.

## Running sessions

Agent sessions live in tmux (or the Windows session host), not in the server
or the app, so they keep running through both an engine restart and an app
swap; the window reconnects to them. Turns that are mid-way survive, but
their live events pause while the server restarts, which is why the
automatic run waits until nobody is working (rules in
`electron/README.md`, [Automatic updates](../electron/README.md#automatic-updates)).

## When something goes wrong

| What happens | What the person sees | What is left |
|---|---|---|
| Offline / GitHub unreachable | nothing (tries again in 30 min) | everything as it was |
| App download cut off, or its checksum is wrong | the toast offers **Update**; a click retries the download once, then opens the release page | the installed app untouched |
| A release whose `latest*.yml` isn't uploaded yet (the first minutes after a tag) | same as above | untouched; the next check finds it |
| Engine install fails (network, a bad build, an unknown tag) | *The engine update failed (exit N). Your engine was left as it was…* — **Retry** | the old engine, still running |
| App swap fails (the updater reports an error, or the app is still running 30 s later — 3 min on macOS, where Squirrel checks the signature only now) | *MindFlock X downloaded but couldn't install itself…* — **Retry**, or the download page; the server is started again if it was stopped | the old app, the new engine |
| The new engine doesn't answer within 90 s (engine-only update) | *The engine was installed, but the server didn't come back…* | the new engine; the offline page shows its log and **Try again** |
| The engine no longer runs at all (native Windows only detects this) | the **Repair the engine** page | a one-click reinstall |
| The app is behind the engine | *Your engine is newer than the app — update the app* | the engine is never downgraded |

There is **no automatic rollback of a new engine that installs fine but
crashes on start**: the person gets the server's log, **Try again**, and the
manual line on the offline page. A failed *install* never gets that far — the
old engine stays.

## Testing

- `node --test` in `electron/` — the pure rules (`update.test.js`,
  `autoupdate.test.js`, `update_mode.test.js`, `update_hardening.test.js`).
- `electron/test/update_sim.test.js` (Linux, after `npm ci` in `electron/`;
  `ELECTRON_SKIP_BINARY_DOWNLOAD=1` is enough) — the real `electron-updater`
  against a local feed: a verified download replaces a fake installed
  AppImage and relaunches it; a wrong checksum, a cut-off download, a
  missing `latest-linux.yml` and no network leave it untouched; each OS
  picks its own `latest*.yml` and installer.
- `tests/integration/test_engine_update_sim.py` — the real `install.sh` in a
  sandbox `$HOME` against a local git repo and a local package index: an
  update lands; a cut-off download, an unresolvable release, an unknown tag
  and a failed final install all leave the old engine working.
- Not covered by any automated test: the Windows NSIS swap and the macOS
  Squirrel swap themselves. [desktop-update-test.md](desktop-update-test.md)
  is the manual check.
