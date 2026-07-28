; MindFlock — NSIS installer customization (auto-included by electron-builder
; as buildResources/installer.nsh; no package.json wiring needed).
;
; The Windows app is only half of MindFlock: the engine needs tmux and Unix
; PTYs, so it runs inside WSL2 and the app talks to it over localhost.
;
; The engine (a private Python toolchain + the mindflock CLI) is installed by
; the APP ON FIRST LAUNCH, where its offline page shows a live progress
; transcript and can retry on failure. The installer USED to run that step here
; via `install.sh`, but a multi-minute network install inside the setup wizard
; gave no feedback, couldn't be cancelled (NSIS disables Cancel inside a
; section), and looked hung — so the wizard now only checks that WSL is present
; and hands off to the app.
;
; Deliberately written in core NSIS only (no LogicLib/FileFunc macros) so it
; cannot collide with whatever electron-builder's own template already included.

!macro customInstall
  Push $0
  Push $1

  SetDetailsPrint both

  ; wsl.exe lives in the real System32. A 32-bit installer on 64-bit Windows
  ; sees SysWOW64 as $SYSDIR (no wsl.exe there), so try the $WINDIR\Sysnative
  ; alias first, then the plain path (64-bit installer, or 32-bit Windows).
  StrCpy $1 "$WINDIR\Sysnative\wsl.exe"
  IfFileExists "$1" mf_have_wsl 0
  StrCpy $1 "$SYSDIR\wsl.exe"
  IfFileExists "$1" mf_have_wsl 0
    DetailPrint "MindFlock: WSL was not found. The engine runs inside WSL2 —"
    DetailPrint "MindFlock: run 'wsl --install' in PowerShell and reboot, then open MindFlock to finish setup."
    Goto mf_end

  mf_have_wsl:
  DetailPrint "MindFlock: installed. It finishes setting up its engine inside WSL the first time you open it."

  mf_end:
  Pop $1
  Pop $0
!macroend

; Uninstall — take the WSL engine with the app, so removing MindFlock from
; Add/Remove Programs clears the whole product. The shell alone would strand
; the `uv` tool + ~/.local/bin/mindflock shim, the git worktrees MindFlock
; registered inside the user's own repos, and the activity hooks it merged
; into their repo settings.
;
; GUARDED by ${isUpdated}: electron-builder runs THIS uninstaller during an
; UPDATE too (uninstall old → install new), passing --updated. Tearing the
; engine down then would wipe it on every auto-update, so only a genuine,
; user-initiated uninstall (no --updated) touches it. LogicLib + ${isUpdated}
; are in scope here — uninstaller.nsh guards its own file removal the same way.
;
; SAFE default: `mindflock uninstall --yes` runs WITHOUT --purge, so the user's
; ~/.mindflock[-assistant] history + settings survive and a reinstall resumes.
; The repo footprint (worktrees, hooks) IS reversed — that must not be stranded
; — then the uv tool + CLI shim are removed as a separate process (the CLI only
; PRINTS that line itself: it can't delete the venv it is running from).
;
; The teardown is a base64-encoded bash script decoded + run inside WSL — the
; exact transport main.js uses — chosen so no shell quoting has to survive the
; NSIS → CreateProcess → wsl.exe → bash gauntlet (the blob is pure
; [A-Za-z0-9+/=], so only the outer quote pair matters). Decoded it is:
;   export PATH="$HOME/.local/bin:$PATH"  # where install.sh puts uv AND mindflock
;   MF="$(command -v mindflock || true)"
;   [ -z "$MF" ] && [ -x "$HOME/.local/bin/mindflock" ] && MF="$HOME/.local/bin/mindflock"
;   UV="$(command -v uv || true)"
;   [ -z "$UV" ] && [ -x "$HOME/.local/bin/uv" ] && UV="$HOME/.local/bin/uv"
;   pkill -f 'mindflock serve'          # stop the server (uninstall refuses while it answers)
;   pkill -f 'backend/web/run.py'
;   pkill -f 'mindflock-wsl-keepalive'  # our own wsl.exe session keeps the distro up meanwhile
;   sleep 1
;   MINDFLOCK_NONINTERACTIVE=1 "$MF" uninstall --yes   # reverse repo footprint, keep home dirs
;   "$UV" tool uninstall mindflock      # remove the venv + ~/.local/bin shim
; Kept identical to main.js buildTeardownScript(). To change it: edit the
; script, re-run `base64 -w0`, and replace the blob below.
!macro customUnInstall
  ${ifNot} ${isUpdated}
    Push $0
    Push $1

    SetDetailsPrint both

    ; wsl.exe: Sysnative alias first (32-bit uninstaller on 64-bit Windows),
    ; then plain System32 — the same lookup customInstall uses.
    StrCpy $1 "$WINDIR\Sysnative\wsl.exe"
    IfFileExists "$1" mf_un_have_wsl 0
    StrCpy $1 "$SYSDIR\wsl.exe"
    IfFileExists "$1" mf_un_have_wsl 0
      DetailPrint "MindFlock: WSL not found — leaving the WSL engine (if any) in place."
      Goto mf_un_end

    mf_un_have_wsl:
    DetailPrint "MindFlock: removing the engine inside WSL (worktrees + hooks + the mindflock tool)…"
    nsExec::ExecToLog '"$1" -e bash --login -c "echo ZXhwb3J0IFBBVEg9IiRIT01FLy5sb2NhbC9iaW46JFBBVEgiCk1GPSIkKGNvbW1hbmQgLXYgbWluZGZsb2NrIHx8IHRydWUpIgpbIC16ICIkTUYiIF0gJiYgWyAteCAiJEhPTUUvLmxvY2FsL2Jpbi9taW5kZmxvY2siIF0gJiYgTUY9IiRIT01FLy5sb2NhbC9iaW4vbWluZGZsb2NrIgpVVj0iJChjb21tYW5kIC12IHV2IHx8IHRydWUpIgpbIC16ICIkVVYiIF0gJiYgWyAteCAiJEhPTUUvLmxvY2FsL2Jpbi91diIgXSAmJiBVVj0iJEhPTUUvLmxvY2FsL2Jpbi91diIKcGtpbGwgLWYgJ21pbmRmbG9jayBzZXJ2ZScgMj4vZGV2L251bGwgfHwgdHJ1ZQpwa2lsbCAtZiAnYmFja2VuZC93ZWIvcnVuLnB5JyAyPi9kZXYvbnVsbCB8fCB0cnVlCnBraWxsIC1mICdtaW5kZmxvY2std3NsLWtlZXBhbGl2ZScgMj4vZGV2L251bGwgfHwgdHJ1ZQpzbGVlcCAxCmlmIFsgLW4gIiRNRiIgXTsgdGhlbiBNSU5ERkxPQ0tfTk9OSU5URVJBQ1RJVkU9MSAiJE1GIiB1bmluc3RhbGwgLS15ZXMgfHwgdHJ1ZTsgZmkKWyAtbiAiJFVWIiBdICYmICIkVVYiIHRvb2wgdW5pbnN0YWxsIG1pbmRmbG9jayAyPi9kZXYvbnVsbCB8fCB0cnVlCmVjaG8gIk1pbmRGbG9jayBlbmdpbmUgdGVhcmRvd24gY29tcGxldGUuIgo= | base64 -d | bash"'
    Pop $0
    DetailPrint "MindFlock: engine teardown finished (exit $0)."

    mf_un_end:
    Pop $1
    Pop $0
  ${endIf}
!macroend
