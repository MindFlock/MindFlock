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
