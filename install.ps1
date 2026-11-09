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
$UvPinnedVersion = '0.13.7'
$UvInstallerSha256 = 'd9e70de578ea2653b631a5933c0296368fca4c91f412b46680661b81bbc399e0'  # pragma: allowlist secret
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

# --force makes re-runs an in-place upgrade (idempotent). An engine first
# installed with pipx stays a pipx install, as in install.sh.
$InstallMethod = 'uv-tool'
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
  & uv tool install --force --python $InstallPython @Withs $Spec
}
if ($LASTEXITCODE -ne 0) { Fail "the MindFlock install failed (exit $LASTEXITCODE)" }

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
if ((Env 'MINDFLOCK_INSTALL_NO_RESTART') -ne '1') {
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
