; MindFlock — NSIS installer customization (auto-included by electron-builder
; as buildResources/installer.nsh; no package.json wiring needed).
;
; The Windows app is only half of MindFlock. The engine needs tmux and Unix
; PTYs, so it runs inside WSL2 and the app talks to it over localhost. An app
; installed on its own is a shell with nothing behind it — so this macro
; finishes the job by running the same install.sh one-liner the README gives,
; inside the user's default WSL distro.
;
; Rules it follows:
;   * It never fails the app install. Missing WSL, a broken distro, or no
;     network all leave the app installed; its offline page then diagnoses
;     what's missing and shows the one command to finish.
;   * Opt out by setting MINDFLOCK_NO_WSL=1 before running the installer.
;   * Safe to re-run: install.sh upgrades in place (`uv tool install --force`).
;
; Deliberately written in core NSIS only (no LogicLib/FileFunc macros) so it
; cannot collide with, or depend on, whatever electron-builder's own template
; has already included.

; Pin the CLI to the same version as this app. electron-builder defines
; VERSION; the fallback keeps an unpackaged/manual build compiling.
!ifdef VERSION
  !define MF_REF "v${VERSION}"
!else
  !define MF_REF "main"
!endif
!define MF_INSTALL_URL \
  "https://raw.githubusercontent.com/MindFlock/MindFlock/${MF_REF}/install.sh"

!macro customInstall
  Push $0
  Push $1
  Push $2

  SetDetailsPrint both

  ReadEnvStr $0 "MINDFLOCK_NO_WSL"
  StrCmp $0 "" mf_find_wsl 0
    DetailPrint "MindFlock: MINDFLOCK_NO_WSL is set - skipping the WSL engine setup."
    Goto mf_wsl_end

  mf_find_wsl:
  ; A 32-bit installer on 64-bit Windows sees SysWOW64 as $SYSDIR, and there is
  ; no wsl.exe there; $WINDIR\Sysnative is the alias to the real System32. Try
  ; the alias first, then the plain path (64-bit installer, or 32-bit Windows).
  StrCpy $1 "$WINDIR\Sysnative\wsl.exe"
  IfFileExists "$1" mf_run_wsl 0
  StrCpy $1 "$SYSDIR\wsl.exe"
  IfFileExists "$1" mf_run_wsl 0
    DetailPrint "MindFlock: WSL is not installed, so the engine was not set up."
    DetailPrint "MindFlock: run 'wsl --install' in PowerShell, reboot, then open MindFlock - it will show the one command left to run."
    Goto mf_wsl_end

  mf_run_wsl:
  DetailPrint "MindFlock: installing the mindflock engine inside WSL. This downloads a Python toolchain and can take several minutes - click 'Show details' to watch."
  ; `--` ends wsl.exe's own flags; the rest runs in the default distro as the
  ; default user. A 20-minute ceiling so a wedged distro can't hang the
  ; installer forever.
  nsExec::ExecToLog /TIMEOUT=1200000 '"$1" -- /bin/sh -c "curl -LsSf ${MF_INSTALL_URL} | MINDFLOCK_INSTALL_REF=${MF_REF} sh"'
  Pop $2
  StrCmp $2 "0" 0 mf_wsl_failed
    DetailPrint "MindFlock: WSL engine installed."
    Goto mf_wsl_end

  mf_wsl_failed:
    DetailPrint "MindFlock: the WSL setup did not finish (result: $2)."
    DetailPrint "MindFlock: the app is installed - open it and the waiting page shows the single command that finishes setup."

  mf_wsl_end:
  Pop $2
  Pop $1
  Pop $0
!macroend
