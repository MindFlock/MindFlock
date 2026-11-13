# MindFlock installer for native Windows - the twin of install.sh. Safe to
# re-run, no admin, no repo clone, no Python setup needed beforehand.
#
#   irm https://raw.githubusercontent.com/MindFlock/MindFlock/main/install.ps1 | iex
#
# With options (a downloaded copy, or the scriptblock form):
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/MindFlock/MindFlock/main/install.ps1))) -Join 'DEVICE CODE'
#
#   -Join 'DEVICE CODE'  after installing, join DEVICE's devices with the code
#                        it showed (Settings -> Devices -> Add a device).
#   -Source <folder>     install from a local checkout instead of git (tests,
#                        CI); same as MINDFLOCK_INSTALL_LOCAL.
#   -DryRun              print the system installs it would run (winget, the
#                        uv installer) instead of running them, and go on;
#                        same as MINDFLOCK_INSTALL_DRY_RUN=1.
#   -NoPathUpdate        leave the user PATH in the registry alone; same as
#                        MINDFLOCK_INSTALL_NO_PATH=1.
#
# What it does (and prints as it goes), step for step what install.sh does:
#   1. Checks the platform: Windows 10 1809 (build 17763) or newer - sessions
#      run in ConPTY (docs/windows.md). PowerShell 5.1 is enough.
#   2. Git for Windows (git.exe AND its bash.exe, which runs every session's
#      launch script): installed with winget if missing.
#   3. uv (Astral's Python manager): its official installer if missing, into
#      ~\.local\bin, no admin. Version-pinned and sha256-verified before it
#      runs, like install.sh's.
#   4. `uv tool install "mindflock[web]"` straight from GitHub - the newest
#      RELEASE by default (MINDFLOCK_INSTALL_REF picks another tag or a
#      branch), first resolved to a full commit SHA (printed, and pinned for
#      the install). uv fetches Python 3.12 by itself and links mindflock.exe
#      into ~\.local\bin. The local speech models Settings -> Voice installed
#      (~\.mindflock\voice\packages.json) are carried into the new venv. What
#      was installed is recorded in ~\.mindflock\install.json so
#      `mindflock update` follows the same path.
#   5. Restarts a MindFlock server already running here so it serves the new
#      version.
#   6. Ends on the same steps `mindflock init` prints (`mindflock init
#      --steps`) and ONE next action. The desktop app's run
#      (MINDFLOCK_NONINTERACTIVE=1) gets one line instead.
#
# Overrides (env vars) - the same names install.sh reads:
#   MINDFLOCK_INSTALL_REPO   git URL to install from
#   MINDFLOCK_INSTALL_REF    branch / tag / commit (default: the newest release)
#   MINDFLOCK_INSTALL_LOCAL  path to a local checkout (= -Source)
#   MINDFLOCK_UV_VERSION     uv version to install (overriding skips the checksum)
#   MINDFLOCK_PORT           port of a running server to restart (default 8765)
#   MINDFLOCK_INSTALL_NO_RESTART  1: leave a running server alone
#   MINDFLOCK_NONINTERACTIVE 1: nobody can answer a prompt (the desktop app)
#   UV_TOOL_DIR / UV_TOOL_BIN_DIR  where uv puts the venv / mindflock.exe
param(
  [string]$Join = '',
  [string]$Source = '',
  [switch]$DryRun,
  [switch]$NoPathUpdate
)

# Continue, not Stop: Windows PowerShell 5.1 turns a native command's stderr
# into error records, and under Stop the first `git ls-remote` warning would
# end the install. Failures are checked explicitly ($LASTEXITCODE, Fail).
$ErrorActionPreference = 'Continue'

# The uv installer run when uv is absent: version-pinned + checksum-verified.
# Bump both together (sha256 of https://astral.sh/uv/<version>/install.ps1);
# .github/workflows/bump-uv-pin.yml rewrites them with install.sh's pair.
$UvPinnedVersion = '0.11.29'
$UvInstallerSha256 = 'd40be32be5121f25fd433cdc5786549b53aad072947674c2818a9c31a7912b4d'  # pragma: allowlist secret
# The interpreter uv pins the tool venv to (INSTALL_PYTHON in install.sh).
$InstallPython = '3.12'
$GitWinget = 'winget install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements'

function Say([string]$m) { Write-Host "[mindflock] $m" -ForegroundColor Cyan }
# throw, never exit: under `irm | iex` an exit closes the user's window.
function Fail([string]$m) {
  Write-Host "[mindflock] ERROR: $m" -ForegroundColor Red
  throw "MindFlock install failed: $m"
}
function Has([string]$name) { [bool](Get-Command $name -ErrorAction SilentlyContinue) }
function Env([string]$name, [string]$default = '') {
  $v = [Environment]::GetEnvironmentVariable($name)
  if ($v) { return $v } else { return $default }
}
# Re-read PATH from the registry after an installer changed it (winget's Git,
# the uv installer), keeping what this process already had in front.
function Update-PathFromRegistry {
  $env:Path = (@($env:Path,
    [Environment]::GetEnvironmentVariable('Path', 'Machine'),
    [Environment]::GetEnvironmentVariable('Path', 'User')) | Where-Object { $_ }) -join ';'
}

# mindflock prints check marks; a cp1252 pipe (the desktop app reading this
# run) would make Python refuse them.
if (-not $env:PYTHONIOENCODING) { $env:PYTHONIOENCODING = 'utf-8' }
if (-not $Source) { $Source = Env 'MINDFLOCK_INSTALL_LOCAL' }
$DryRun = $DryRun -or ((Env 'MINDFLOCK_INSTALL_DRY_RUN') -eq '1')
$NoPathUpdate = $NoPathUpdate -or ((Env 'MINDFLOCK_INSTALL_NO_PATH') -eq '1')
$NonInteractive = (Env 'MINDFLOCK_NONINTERACTIVE') -eq '1'
$Repo = Env 'MINDFLOCK_INSTALL_REPO' 'https://github.com/MindFlock/MindFlock'
$Ref = Env 'MINDFLOCK_INSTALL_REF'
# PowerShell 5.1 may not offer TLS 1.2 by default; GitHub and astral.sh need it.
try {
  [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch { }

# --- 1. platform check -------------------------------------------------------
if ($env:OS -ne 'Windows_NT') {
  Fail "this is the Windows installer. On Linux, macOS or WSL run:  curl -LsSf https://raw.githubusercontent.com/MindFlock/MindFlock/main/install.sh | sh"
}
$Build = 0
try {
  $Build = [int](Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' -Name CurrentBuildNumber -ErrorAction Stop).CurrentBuildNumber
} catch { $Build = [Environment]::OSVersion.Version.Build }
if ($Build -lt 17763) {
  Fail "Windows 10 1809 (build 17763) or newer is needed: sessions run in ConPTY. This is build $Build - update Windows and re-run."
}
Say "platform: Windows build $Build ($env:PROCESSOR_ARCHITECTURE), PowerShell $($PSVersionTable.PSVersion)"
if ($DryRun) { Say 'dry run: system installs are printed, not run' }

# --- 2. Git for Windows ------------------------------------------------------
# git.exe AND its bash.exe (never System32\bash.exe, the WSL launcher) - the
# same lookup as backend.compat.posix_shell.
function Find-GitBash {
  foreach ($v in @('MINDFLOCK_GIT_BASH', 'CLAUDE_CODE_GIT_BASH_PATH')) {
    $p = Env $v
    if ($p -and (Test-Path -LiteralPath $p -PathType Leaf)) { return $p }
  }
  $git = Get-Command git -ErrorAction SilentlyContinue
  if ($git) {
    $root = Split-Path -Parent (Split-Path -Parent $git.Source)
    foreach ($p in @((Join-Path $root 'bin\bash.exe'), (Join-Path $root 'usr\bin\bash.exe'))) {
      if (Test-Path -LiteralPath $p -PathType Leaf) { return $p }
    }
  }
  foreach ($base in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, (Join-Path (Env 'LOCALAPPDATA') 'Programs'))) {
    if ($base) {
      $p = Join-Path $base 'Git\bin\bash.exe'
      if (Test-Path -LiteralPath $p -PathType Leaf) { return $p }
    }
  }
  return ''
}

$GitBash = Find-GitBash
if ((Has 'git') -and $GitBash) {
  Say "Git for Windows: $(git --version), bash: $GitBash"
} elseif ($DryRun) {
  Say 'Git for Windows is missing - would run:'
  Say "  $GitWinget"
} else {
  if (-not (Has 'winget')) {
    Fail "Git for Windows is required (its bash.exe runs every session). winget isn't available here - install Git from https://git-scm.com/download/win, then re-run."
  }
  Say 'installing Git for Windows with winget...'
  Say "  $GitWinget"
  & winget install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements
  if ($LASTEXITCODE -ne 0) { Fail "winget could not install Git for Windows (exit $LASTEXITCODE)" }
  Update-PathFromRegistry
  $GitBash = Find-GitBash
  if (-not ((Has 'git') -and $GitBash)) {
    Fail 'Git for Windows was installed but is not found yet - open a new PowerShell window and re-run.'
  }
  Say "Git for Windows installed: $(git --version)"
}

# --- 3. uv -------------------------------------------------------------------
# ~\.local\bin is where both uv and mindflock.exe land (uv's own default on
# Windows). The PATH this shell came with is kept: the closing lines say
# whether a NEW window is needed before `mindflock` is found.
$OrigPath = $env:Path
$LocalBin = Join-Path $env:USERPROFILE '.local\bin'
$ToolBin = Env 'UV_TOOL_BIN_DIR' $LocalBin
$env:Path = "$ToolBin;$LocalBin;$env:Path"

if (Has 'uv') {
  Say "uv already installed: $(uv --version)"
} else {
  $UvVersion = Env 'MINDFLOCK_UV_VERSION' $UvPinnedVersion
  $UvUrl = "https://astral.sh/uv/$UvVersion/install.ps1"
  if ($DryRun) {
    Say "uv is missing - would run Astral's installer $UvUrl (sha256-verified)"
  } else {
    Say "installing uv $UvVersion (Astral's official installer, no admin, into ~\.local\bin)..."
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("uv-install-" + [guid]::NewGuid().ToString('N') + '.ps1')
    try {
      Invoke-WebRequest -UseBasicParsing -Uri $UvUrl -OutFile $tmp -ErrorAction Stop
      if ($UvVersion -eq $UvPinnedVersion) {
        $got = (Get-FileHash -Algorithm SHA256 -LiteralPath $tmp).Hash.ToLower()
        if ($got -ne $UvInstallerSha256) {
          Fail "uv installer checksum mismatch`n  expected: $UvInstallerSha256`n  got:      $got`nThe download may be corrupted or tampered with - not running it."
        }
        Say 'uv installer sha256 verified'
      } else {
        Say "WARNING: MINDFLOCK_UV_VERSION=$UvVersion overrides the pinned version - no checksum on file, skipping verification"
      }
      & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $tmp
      if ($LASTEXITCODE -ne 0) { Fail "the uv installer failed (exit $LASTEXITCODE)" }
    } finally {
      Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    }
    $env:Path = "$LocalBin;$env:Path"
    if (-not (Has 'uv')) { Fail 'uv did not land on PATH - see https://docs.astral.sh/uv/getting-started/installation/' }
    Say "uv installed: $(uv --version)"
  }
}

# --- 4. mindflock ------------------------------------------------------------
# Shaped like a release tag (v0.7.5) - is_release_tag in install.sh.
function Test-ReleaseTag([string]$t) { return $t -match '^v?[0-9]+(\.[0-9]+){1,3}$' }

# The newest published release tag ('' when none can be found): the GitHub
# API, then the releases/latest redirect, then the highest v* tag on the remote.
function Get-LatestReleaseTag {
  $slug = ''
  if ($Repo -match '^https://github\.com/(.+?)(\.git)?/?$') { $slug = $Matches[1] }
  $tag = ''
  if ($slug) {
    try {
      $rel = Invoke-RestMethod -UseBasicParsing -Headers @{ Accept = 'application/vnd.github+json' } `
        -Uri "https://api.github.com/repos/$slug/releases/latest" -TimeoutSec 20
      $tag = [string]$rel.tag_name
    } catch { $tag = '' }
    if (-not $tag) {
      try {
        $req = [Net.HttpWebRequest]::Create("https://github.com/$slug/releases/latest")
        $req.AllowAutoRedirect = $false
        $req.Method = 'HEAD'
        $resp = $req.GetResponse()
        $loc = [string]$resp.Headers['Location']
        $resp.Close()
        if ($loc -match '/releases/tag/([^/]+)$') { $tag = $Matches[1] }
      } catch { $tag = '' }
    }
  }
  if (-not $tag -and (Has 'git')) {
    # Release tags only (vX.Y.Z), highest first.
    $tags = & git ls-remote --tags --refs $Repo 'v*' 2>$null | ForEach-Object { ($_ -split 'refs/tags/')[-1] } |
      Where-Object { $_ -match '^v[0-9]+\.[0-9]+\.[0-9]+$' } |
      Sort-Object { [version]($_.Substring(1)) } -Descending
    if ($tags) { $tag = @($tags)[0] }
  }
  if ($tag -match '^v[0-9]') { return $tag }
  return ''
}

$Pinned = ''
if ($Source) {
  $rp = Resolve-Path -LiteralPath $Source -ErrorAction SilentlyContinue
  if (-not $rp) { Fail "no such folder: $Source" }
  $Source = $rp.Path
  $Spec = "mindflock[web] @ file:///" + ($Source -replace '\\', '/')
  $Ref = 'local'
  if (Has 'git') {
    $Pinned = [string](& git -C $Source rev-parse HEAD 2>$null)
    if ($LASTEXITCODE -ne 0) { $Pinned = '' }
  }
  $SourceKey = 'path'; $SourceVal = $Source
  Say "installing MindFlock from local checkout $Source..."
} else {
  if (-not (Has 'git')) { Fail "git is required to install from $Repo - install Git for Windows ($GitWinget) and re-run." }
  if (-not $Ref) {
    $Ref = Get-LatestReleaseTag
    if ($Ref) {
      Say "newest release: $Ref (set MINDFLOCK_INSTALL_REF=main for unreleased code)"
    } else {
      $Ref = 'main'
      Say "WARNING: couldn't find the newest release - installing main instead:"
      Say '         an unreleased build; the desktop app pins releases'
    }
  } elseif (-not (Test-ReleaseTag $Ref)) {
    Say "WARNING: MINDFLOCK_INSTALL_REF=$Ref is not a release tag - installing an unreleased build; the desktop app pins releases"
  }
  $SourceKey = 'repo'; $SourceVal = $Repo
  if ($Ref -match '^[0-9a-f]{40}$') { $Pinned = $Ref }
  if (-not $Pinned) {
    # Peeled tags first (an ANNOTATED tag's own ref is the tag object).
    $line = & git ls-remote $Repo "refs/tags/$Ref^{}" 2>$null | Select-Object -First 1
    if (-not $line) { $line = & git ls-remote $Repo "refs/heads/$Ref" "refs/tags/$Ref" 2>$null | Select-Object -First 1 }
    if ($line) { $Pinned = ([string]$line -split '\s+')[0] }
    if (-not $Pinned) { Fail "could not resolve '$Ref' on $Repo (branch or tag?) - check MINDFLOCK_INSTALL_REF" }
  }
  $Spec = "mindflock[web] @ git+$Repo@$Pinned"
  Say "installing MindFlock from $Repo@$Ref (commit $Pinned)..."
}

# The engine's own folder: MINDFLOCK_HOME, else ~\.mindflock (HOME when set -
# the engine's config dir honours it - else the user profile).
$MfHome = Env 'MINDFLOCK_HOME'
if (-not $MfHome) { $MfHome = Join-Path (Env 'HOME' $env:USERPROFILE) '.mindflock' }

# The local speech models' packages Settings -> Voice installed, carried into
# the rebuilt venv (one --with each), as the in-app updater does.
$Withs = @()
$VoiceRecord = Join-Path $MfHome 'voice\packages.json'
if (Test-Path -LiteralPath $VoiceRecord) {
  try {
    $rec = Get-Content -Raw -LiteralPath $VoiceRecord -ErrorAction Stop | ConvertFrom-Json
    foreach ($p in @($rec.packages)) {
      if ($p -is [string] -and $p -match '^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9._,-]+\])?$') {
        $Withs += @('--with', $p)
      }
    }
    if ($Withs.Count) { Say "keeping the local speech models: $(@($rec.packages) -join ', ')" }
  } catch { Say "WARNING: couldn't read $VoiceRecord - the speech models reinstall themselves later" }
}

# --- the engine swap (Windows' answer to an in-place upgrade) -----------------
# `uv tool install --force` rebuilds the tool venv IN PLACE. Windows refuses to
# delete a file a process has mapped - and the server, its ticket pipeline, the
# session host and every session's MCP server all run from that venv - so when
# uv recreates it, it deletes what it can and stops: Lib gone, Scripts left,
# "Failed find package `mindflock` in tool environment", and the running
# server failing on its next import. Renaming a folder (or an exe) a process
# runs from IS allowed, and the process keeps going. So (docs/windows.md,
# "Updating the engine on Windows"; backend/web/core/engine_swap.py is the
# Python twin that `mindflock update` and the in-app updater run):
#   1. stage: the same install into <tools>-mindflock-next while the engine
#      keeps serving (download + build happen here), then an import check -
#      a failure stops here, nothing touched;
#   2. move mindflock.exe to mindflock.exe.old-<stamp> and the live venv to
#      <tools>-mindflock-old-<stamp> (beside the tools dir: uv errors on
#      strangers inside it); running sessions keep their files. The shim goes
#      first: a 0.14.15-or-older server keeps it open (and only it), so
#      such a server is stopped for the swap and started again afterwards;
#   3. the real install from the warm cache, then the same check;
#   4. on any failure the half-built venv goes and the old one is put back.
# MINDFLOCK_INSTALL_STOP_SERVER=1 (the desktop app) stops every running
# server between 1 and 2; the app starts it again afterwards.
$EngineCheck = 'import backend.cli, backend.session, backend.session.preflight, backend.web.core.self_update'

function Remove-Quietly([string]$p) {
  if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Recurse -Force -ErrorAction SilentlyContinue }
  return -not (Test-Path -LiteralPath $p)
}

# '' when the engine at $Venv (and its shim $Exe) works, else why not.
function Test-Engine([string]$Venv, [string]$Exe) {
  $py = Join-Path $Venv 'Scripts\python.exe'
  if (-not (Test-Path -LiteralPath $py -PathType Leaf)) { return "$py is missing" }
  $out = & $py -c $EngineCheck 2>&1
  if ($LASTEXITCODE -ne 0) { return "the engine's packages don't import: $(@($out)[-1])" }
  if ($Exe) {
    $out = & $Exe --version 2>&1
    if ($LASTEXITCODE -ne 0) { return "$Exe --version fails: $(@($out)[-1])" }
  }
  return ''
}

# Stops MindFlock servers: every one, or ($Exe) only those started through
# that shim. Returns the shim-started ones' argument strings, to start again.
function Stop-EngineServer([string]$Exe = '') {
  $again = @()
  Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'mindflock(\.exe)?"?\s+serve|backend[\\/]web[\\/]run\.py' } |
    ForEach-Object {
      $mine = $_.ExecutablePath -and $Exe -and ($_.ExecutablePath -eq $Exe)
      if ($Exe -and -not $mine) { return }
      if ($mine) { $again += ($_.CommandLine -replace '^\s*("[^"]*"|\S+)\s*', '') }
      taskkill /T /F /PID $_.ProcessId 2>$null | Out-Null
    }
  Start-Sleep -Seconds 1
  return ,$again
}

# Renames $From to $To, retrying while something holds it; $true when done.
function Move-Retrying([string]$From, [string]$To, [int]$Tries = 15) {
  for ($i = 0; $i -lt $Tries; $i++) {
    try { [IO.File]::Move($From, $To); return $true } catch { Start-Sleep -Seconds 1 }
  }
  return $false
}

# The servers Install-EngineSwapped stopped because they held the shim.
$script:Restart = @()

# Runs $Install (the uv line) so the engine is replaced whole or not at all.
# Returns the exit code; on failure the engine that was there is back.
function Install-EngineSwapped([scriptblock]$Install) {
  # Where uv keeps tool venvs. Never an in-place fallback: that IS the bug.
  # (No Select-Object -First here: stopping the pipeline early can leave a
  # non-zero $LASTEXITCODE behind a perfectly good answer.)
  $ToolDir = ''
  $out = @(& uv tool dir 2>$null)
  if ($LASTEXITCODE -eq 0 -and $out.Count) { $ToolDir = [string]$out[0] }
  if (-not $ToolDir) { $ToolDir = Env 'UV_TOOL_DIR' (Join-Path $env:APPDATA 'uv\tools') }
  $ToolDir = $ToolDir.Trim().TrimEnd('\')
  $Venv = Join-Path $ToolDir 'mindflock'
  $Exe = Join-Path $ToolBin 'mindflock.exe'
  $Stamp = Get-Date -Format 'yyyyMMddHHmmss'
  $Next = "$ToolDir-mindflock-next"
  $Old = "$ToolDir-mindflock-old-$Stamp"
  $OldExe = "$Exe.old-$Stamp"
  # Earlier swaps' leftovers, once nothing runs from them any more.
  Get-Item -Path "$ToolDir-mindflock-old-*", "$ToolDir-mindflock-failed-*", "$Exe.old-*", "$Exe.failed-*" -Force -ErrorAction SilentlyContinue |
    ForEach-Object { Remove-Quietly $_.FullName | Out-Null }
  $Live = Test-Path -LiteralPath $Venv -PathType Container
  if ($Live) {
    Say "building the new engine beside the running one ($Next)..."
    Remove-Quietly $Next | Out-Null
    $SavedDir = $env:UV_TOOL_DIR; $SavedBin = $env:UV_TOOL_BIN_DIR
    $env:UV_TOOL_DIR = Join-Path $Next 'tools'
    $env:UV_TOOL_BIN_DIR = Join-Path $Next 'bin'
    & $Install | Out-Host
    $code = [int]$LASTEXITCODE
    $env:UV_TOOL_DIR = $SavedDir; $env:UV_TOOL_BIN_DIR = $SavedBin
    $why = ''
    if ($code -ne 0) { $why = "the install failed (exit $code)" }
    else { $why = Test-Engine (Join-Path $Next 'tools\mindflock') (Join-Path $Next 'bin\mindflock.exe') }
    if ($why) {
      Remove-Quietly $Next | Out-Null
      Say "$why - nothing was changed; the engine you have keeps running"
      if ($code -eq 0) { $code = 1 }
      return $code
    }
    if ((Env 'MINDFLOCK_INSTALL_STOP_SERVER') -eq '1') {
      Say 'stopping the MindFlock server for the swap (it starts again on the new engine)...'
      Stop-EngineServer | Out-Null
    }
  }
  # The shim first: if it can't move, nothing has changed yet.
  $MovedExe = $false
  if (Test-Path -LiteralPath $Exe) {
    $MovedExe = Move-Retrying $Exe $OldExe 3
    if (-not $MovedExe) {
      Say "$Exe is in use - stopping the MindFlock server that runs from it for the swap..."
      $script:Restart = Stop-EngineServer $Exe
      $MovedExe = Move-Retrying $Exe $OldExe
    }
    if (-not $MovedExe) {
      Remove-Quietly $Next | Out-Null
      Say "$Exe is in use by a running MindFlock - nothing was changed; close it and run this again"
      return 75
    }
  }
  if ($Live) {
    try { [IO.Directory]::Move($Venv, $Old) } catch {
      if ($MovedExe) { try { [IO.File]::Move($OldExe, $Exe) } catch { } }
      Remove-Quietly $Next | Out-Null
      Say "couldn't move the running engine aside ($($_.Exception.Message)) - nothing was changed"
      return 75
    }
  }
  & $Install | Out-Host
  $code = [int]$LASTEXITCODE
  $why = ''
  if ($code -ne 0) { $why = "the install failed (exit $code)" }
  else {
    $ChkExe = ''
    if (Test-Path -LiteralPath $Exe -PathType Leaf) { $ChkExe = $Exe }
    $why = Test-Engine $Venv $ChkExe
  }
  Remove-Quietly $Next | Out-Null
  if (-not $why) {
    if ($Live -and -not (Remove-Quietly $Old)) { Say "the previous engine stays at $Old until the sessions using it end" }
    if ($MovedExe) { Remove-Quietly $OldExe | Out-Null }
    return 0
  }
  Say "$why - putting the previous engine back"
  if ($code -eq 0) { $code = 1 }
  $Back = $true
  if ($Live) {
    if (Test-Path -LiteralPath $Venv) {
      $Failed = "$ToolDir-mindflock-failed-$Stamp"
      try { [IO.Directory]::Move($Venv, $Failed) } catch { $Failed = $Venv }
      Remove-Quietly $Failed | Out-Null
    }
    $Back = $false
    if (-not (Test-Path -LiteralPath $Venv)) {
      try { [IO.Directory]::Move($Old, $Venv); $Back = $true } catch { Say "couldn't put $Old back: $($_.Exception.Message)" }
    }
  }
  if ($MovedExe) {
    if ((Test-Path -LiteralPath $Exe) -and -not (Remove-Quietly $Exe)) {
      try { [IO.File]::Move($Exe, "$Exe.failed-$Stamp") } catch { }
    }
    try { [IO.File]::Move($OldExe, $Exe) } catch { Say "couldn't put $Exe back: $($_.Exception.Message)"; $Back = $false }
  }
  if (-not $Live) { Say 'there was no engine here before; nothing to put back' }
  elseif ($Back) { Say 'the previous engine is back in place' }
  else { Say 'the previous engine could NOT be put back - run this installer again' }
  return $code
}

# --force makes re-runs an in-place upgrade (idempotent). An engine first
# installed with pipx stays a pipx install, as in install.sh.
$InstallMethod = 'uv-tool\'
$RecordPath = Join-Path $MfHome 'install.json'
$PrevMethod = ''
if (Test-Path -LiteralPath $RecordPath) {
  try { $PrevMethod = [string](Get-Content -Raw -LiteralPath $RecordPath -ErrorAction Stop | ConvertFrom-Json).method } catch { }
}
if ($PrevMethod -eq 'pipx' -and (Has 'pipx')) {
  Say 'this engine was installed with pipx - keeping it that way'
  & pipx install --force $Spec
  $InstallMethod = 'pipx'
} else {
  $code = Install-EngineSwapped { & uv tool install --force --python $InstallPython @Withs $Spec }
  # A server stopped because it held mindflock.exe starts again - on the new
  # engine, or on the old one put back - with the arguments it had.
  if ($script:Restart.Count -and (Env 'MINDFLOCK_INSTALL_NO_RESTART') -ne '1') {
    $Shim = Join-Path $ToolBin 'mindflock.exe'
    foreach ($a in $script:Restart) {
      Say "starting the MindFlock server again: mindflock $a"
      try { Start-Process -WindowStyle Hidden -FilePath $Shim -ArgumentList $a | Out-Null } catch { Say "couldn't start it - run: mindflock $a" }
    }
  }
  if ($code -ne 0) { Fail "the MindFlock install failed (exit $code)" }
}
if ($LASTEXITCODE -ne 0 -and $InstallMethod -eq 'pipx') { Fail "the MindFlock install failed (exit $LASTEXITCODE)" }

$MF = ''
foreach ($p in @((Join-Path $ToolBin 'mindflock.exe'), (Join-Path $LocalBin 'mindflock.exe'))) {
  if (Test-Path -LiteralPath $p -PathType Leaf) { $MF = $p; break }
}
if (-not $MF) {
  $c = Get-Command mindflock -ErrorAction SilentlyContinue
  if ($c) { $MF = $c.Source }
}
if (-not $MF) { Fail "install finished but mindflock.exe is not found. Add $ToolBin to PATH (uv tool update-shell does), then run: mindflock init" }
Say "installed: $MF ($(& $MF --version 2>$null))"

# Record what was installed (install.sh's install.json contract).
try {
  New-Item -ItemType Directory -Force -Path $MfHome -ErrorAction Stop | Out-Null
  $record = [ordered]@{
    method = $InstallMethod; ref = $Ref; sha = $Pinned; python = $InstallPython
    at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
  }
  $record[$SourceKey] = $SourceVal
  # No BOM: the engine's json.loads refuses one.
  [IO.File]::WriteAllText($RecordPath, ($record | ConvertTo-Json -Compress) + "`n", (New-Object Text.UTF8Encoding $false))
  Say "recorded the install in $RecordPath"
} catch {
  Say "WARNING: couldn't write $RecordPath - mindflock update will detect this install instead"
}

# Make sure FUTURE windows find it too: `uv tool update-shell` adds the tool
# bin dir to the user PATH if (and only if) it is missing. Best-effort.
if ($NoPathUpdate) {
  Say "leaving the user PATH alone (-NoPathUpdate); mindflock is at $MF"
} else {
  try { & uv tool update-shell *> $null } catch { }
}

# --- 5. a server already running here ------------------------------------------
$Port = Env 'MINDFLOCK_PORT' '8765'
$Running = $false
if ((Env 'MINDFLOCK_INSTALL_NO_RESTART') -ne '1' -and -not $script:Restart.Count) {
  try {
    Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri "http://127.0.0.1:$Port/api/remote/hello" | Out-Null
    $Running = $true
  } catch { $Running = $false }
}
if ($Running) {
  Say "restarting the MindFlock server on port $Port onto the new version..."
  $env:MINDFLOCK_PORT = $Port
  & $MF restart
  if ($LASTEXITCODE -eq 0) {
    if (-not $Join) {
      Say ''
      Say "Done - the server on http://127.0.0.1:$Port runs the new version."
      return
    }
  } else {
    Say "couldn't restart it - it runs the previous version until you run: $MF restart"
    if (-not $Join) {
      Say ''
      Fail "Installed. The server on http://127.0.0.1:$Port still runs the previous version."
    }
  }
}

# --- 6. join your other computer (-Join) ---------------------------------------
if ($Join) {
  Say "joining your devices ($Join)..."
  & $MF devices bootstrap --join $Join
  if ($LASTEXITCODE -eq 0) {
    Say 'joined - this computer is one of your devices now'
  } else {
    Say "joining didn't finish - once the above is fixed, run:"
    Say "  $MF devices bootstrap --join '$Join'"
  }
}

# --- 7. the next steps ---------------------------------------------------------
Say ''
if ($NonInteractive) {
  Say 'Done. Your agent comes next, in the app.'
} else {
  $env:Path, $now = $OrigPath, $env:Path
  $found = Has 'mindflock'
  $env:Path = $now
  if (-not $found) { Say "Open a new PowerShell window first - this one doesn't find mindflock yet." }
  & $MF init --steps
  if ($LASTEXITCODE -ne 0) { Say 'Next: run mindflock init' }
}
