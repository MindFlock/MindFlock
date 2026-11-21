# Manual check: the desktop app updates itself (Windows, macOS)

About ten minutes per machine. It checks what no automated test can: the
Windows installer and the macOS Squirrel swap on real machines, installed
from the real releases page. How it works:
[desktop-updates.md](desktop-updates.md).

**N** is the newest release, **N-1** the one before. The update is run by
the **old** app (N-1) and its bundled installer, so to check a change to the
update code, N-1 must be the first release that contains it.

## Before you start

- Use a **spare user account** (or a VM) on the PC or Mac. The app and its
  engine install per user, so a spare account leaves your everyday MindFlock
  alone. Installing N-1 over your everyday install would downgrade it.
- Have the release pages open:
  `https://github.com/MindFlock/MindFlock/releases/tag/v<N-1>` and
  `…/tag/v<N>`. Take the installer from the **N-1 tag page**; the front-page
  download button always gives N.
- The app's log: press **Ctrl+Shift+L** (Cmd+Shift+L on a Mac) in the app,
  or open `%APPDATA%\MindFlock\logs\main.log` /
  `~/Library/Application Support/MindFlock/logs/main.log`.

## Windows

1. From the N-1 page, download `MindFlock-Setup.exe`. Optional: in
   PowerShell, `Get-FileHash .\MindFlock-Setup.exe` should match
   `MindFlock-Setup.exe.sha256` on the same page.
2. Run it. SmartScreen: **More info → Run anyway** (the build is not
   code-signed). Finish the wizard and open MindFlock.
3. First launch installs the engine (about 3 minutes, with a progress log),
   then the setup checklist. Do enough to start **one session**, and type
   something into it so it has output. Remember its name.
4. **Settings → Show advanced settings → Engine & updates.** Set **Install
   updates** to **Ask me**. The line under it should say *This is N-1* and
   that *N is available*. (If it says it couldn’t check, click **Check
   now**.)
5. Within a minute a card appears at the bottom right: **Update available —
   MindFlock N is ready (you're on N-1).** If it doesn't, restart the app
   (the first check runs 20 s after launch).
6. Click **Update**. The card shows, in order:
   - *Checking the engine…*
   - *Updating the engine to N…* (1–3 minutes)
   - *Downloading MindFlock N — NN%…* (only if the download hadn't finished)
   - *Restarting into MindFlock N…*

   The window closes. The installer runs with no wizard (10–30 s) and
   MindFlock opens again by itself.
7. **Check it worked:**
   - A card says **Updated to MindFlock N.** once.
   - Engine & updates says *This is N* and *It's the newest version*.
   - Windows Settings → Apps → Installed apps → MindFlock shows version N.
   - The session from step 3 is still listed, and its earlier output is
     still there.
   - `main.log` has `engine install finished, code 0` and
     `last update: Updated to MindFlock N.`

## macOS

1. From the N-1 page, download `MindFlock.dmg`. Open it and drag MindFlock
   into **Applications**, then eject the disk image. It must run from
   Applications: a copy run from the disk image can't update itself.
2. Open it from Applications. If macOS blocks it: **System Settings →
   Privacy & Security → Open Anyway** (the app is self-signed; once per
   install).
3. Same as Windows steps 3–6. If it asks to install Homebrew or Apple's
   command line tools, let it. After *Restarting into MindFlock N…* the
   window closes and reopens within a minute or two (macOS checks the
   new build’s signature first).
4. **Check it worked:** the **Updated to MindFlock N.** card;
   **MindFlock → About MindFlock** shows N; Engine & updates says *This is
   N*; in Terminal `~/.local/bin/mindflock --version` prints N and
   `tmux ls` still lists the session from step 3.

## Optional: automatic mode and failures (5 more minutes)

- **Automatic:** start again from N-1 with **Install updates** set to
  **Automatic**. Lock the screen (Win+L / Ctrl+Cmd+Q) for 11 minutes. When
  you unlock, MindFlock should already be on N, with the **Updated to
  MindFlock N.** card. `main.log` shows `auto-update: installing now`.
- **Offline:** quit MindFlock, turn Wi-Fi off, open it. Nothing should pop
  up and nothing should break. Turn Wi-Fi back on and restart the app: the
  card appears.
- **Network drop during the engine step:** click **Update** and turn Wi-Fi
  off while it says *Updating the engine…*. Expected: *The engine update
  failed (exit N). Your engine was left as it was…* with **Retry**, and
  MindFlock keeps working on N-1. Wi-Fi on, **Retry** finishes the update.

## If it goes wrong

Write down what the card said, then copy the end of `main.log`.

- **The app didn't update** (the card says it couldn't install itself, or a
  download page opened instead): install N over it by hand: run N's
  `MindFlock-Setup.exe`, or drag N's `MindFlock.app` into Applications and
  replace the old one. Settings, history and sessions are kept. On a Mac,
  `[updater] error` lines in `main.log` say why (usually the signature).
- **MindFlock opens on a page saying the server didn't start, or the engine
  needs a repair:** click **Try again** or **Repair the engine**. If that
  fails, reinstall the engine at version N by hand:
  - macOS / Linux / WSL:
    `curl -LsSf https://raw.githubusercontent.com/MindFlock/MindFlock/main/install.sh | MINDFLOCK_INSTALL_REF=vN sh`
  - Windows (PowerShell):
    `$env:MINDFLOCK_INSTALL_REF='vN'; irm https://raw.githubusercontent.com/MindFlock/MindFlock/main/install.ps1 | iex`
- **Clean up the spare account afterwards:** Windows: Settings → Apps →
  MindFlock → Uninstall (this removes its engine too). Mac: drag MindFlock
  to the Trash and run `~/.local/bin/uv tool uninstall mindflock`.
