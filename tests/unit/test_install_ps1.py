"""install.ps1: the native-Windows twin of install.sh (docs/windows.md).

What can be pinned without Windows: it mirrors install.sh's flags, env vars
and steps, its uv pin agrees with the doctor's, it never ``exit``s (under
``irm | iex`` that closes the user's window) and it is plain ASCII (Windows
PowerShell 5.1 reads a BOM-less script as cp1252, where a UTF-8 dash ends a
string). Where PowerShell exists (the Windows CI job, WSL with interop) its
parser checks the whole script too.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_PS1 = (_ROOT / "install.ps1").read_text(encoding="utf-8")
_SH = (_ROOT / "install.sh").read_text(encoding="utf-8")


def _code_lines():
    """install.ps1's lines with comments dropped (good enough for a lint)."""
    for line in _PS1.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            yield line


def test_plain_ascii_for_windows_powershell_5_1():
    _PS1.encode("ascii")


def test_never_exits_so_irm_iex_keeps_the_window():
    assert not [line for line in _code_lines() if re.match(r"\s*exit\b", line)]
    assert "throw " in _PS1  # failures throw instead


def test_no_powershell_7_only_syntax():
    for line in _code_lines():
        code = re.sub(r"'[^']*'|\"[^\"]*\"", "''", line)  # drop string bodies
        assert "??" not in code and "?." not in code, line
        assert not re.search(r"\s(&&|\|\|)\s", code), line
        assert "-AsHashtable" not in code, line


def test_the_uv_pin_moves_with_install_sh_and_the_doctor():
    from backend import _pins

    ver = re.search(r"^\$UvPinnedVersion = '([^']+)'", _PS1, re.M).group(1)
    assert ver == _pins.UV_PINNED_VERSION
    sha = re.search(
        r"^\$UvInstallerSha256 = '([0-9a-f]{64})'  # pragma: allowlist secret$",
        _PS1,
        re.M,
    )
    assert sha, "the sha line keeps its detect-secrets pragma"
    assert f"https://astral.sh/uv/$UvVersion/install.ps1" in _PS1
    wf = (_ROOT / ".github" / "workflows" / "bump-uv-pin.yml").read_text("utf-8")
    assert "install.ps1" in wf and "UvInstallerSha256" in wf


def test_same_env_overrides_as_install_sh():
    for name in (
        "MINDFLOCK_INSTALL_REPO",
        "MINDFLOCK_INSTALL_REF",
        "MINDFLOCK_INSTALL_LOCAL",
        "MINDFLOCK_UV_VERSION",
        "MINDFLOCK_PORT",
        "MINDFLOCK_INSTALL_NO_RESTART",
        "MINDFLOCK_NONINTERACTIVE",
    ):
        assert name in _SH and name in _PS1, name


def test_mirrors_install_sh_steps():
    # Python pin, the release default, --force upgrade, install.json, restart.
    assert re.search(r"INSTALL_PYTHON=\"3\.12\"", _SH)
    assert "$InstallPython = '3.12'" in _PS1
    assert "$Ref = Get-LatestReleaseTag" in _PS1
    assert "uv tool install --force --python $InstallPython @Withs $Spec" in _PS1
    assert "mindflock[web] @ git+$Repo@$Pinned" in _PS1
    assert "refs/tags/$Ref^{}" in _PS1  # peeled tags first, like install.sh
    for key in ("method", "ref", "sha", "python", "at"):
        assert f"{key} = " in _PS1
    assert "/api/remote/hello" in _PS1 and "& $MF restart" in _PS1
    assert "devices bootstrap --join $Join" in _PS1
    assert "& $MF init --steps" in _PS1
    assert "Done. Your agent comes next, in the app." in _PS1


def test_git_for_windows_comes_from_winget():
    assert (
        "winget install --id Git.Git -e --source winget"
        " --accept-package-agreements --accept-source-agreements"
    ) in _PS1
    assert "bash.exe" in _PS1


def test_test_hooks_exist():
    # A local checkout, a dry run of the system installs, no PATH edits —
    # what the Windows proof and CI run with.
    for flag in ("[string]$Source", "[switch]$DryRun", "[switch]$NoPathUpdate"):
        assert flag in _PS1
    assert "MINDFLOCK_INSTALL_DRY_RUN" in _PS1 and "MINDFLOCK_INSTALL_NO_PATH" in _PS1


def test_install_sh_points_git_bash_at_install_ps1():
    case = _SH[_SH.index("MINGW*|MSYS*|CYGWIN*|Windows_NT)") :]
    case = case[: case.index(";;")]
    assert "install.ps1" in case and "irm " in case
    assert "not a supported" not in case


def _powershell() -> str:
    return shutil.which("powershell") or shutil.which("powershell.exe") or ""


@pytest.mark.skipif(
    not _powershell() or not shutil.which("wslpath"),
    reason="needs Windows PowerShell reached from WSL (wslpath); a macOS runner can have a powershell without it",
)
def test_windows_powershell_parses_it():
    path = str(_ROOT / "install.ps1")
    if os.name != "nt":
        path = subprocess.run(
            ["wslpath", "-w", path], capture_output=True, text=True, check=True
        ).stdout.strip()
    probe = (
        "$e=$null;[void][System.Management.Automation.Language.Parser]::ParseFile("
        f"'{path}',[ref]$null,[ref]$e);if($e){{$e|%{{$_.Message}};exit 1}}"
    )
    proc = subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-Command", probe],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_the_engine_is_swapped_never_rebuilt_in_place():
    # 2026-11-13: install.ps1's in-place `uv tool install --force` under the
    # running server left a half-deleted engine. The uv line now only runs
    # inside Install-EngineSwapped (stage beside, move aside, put back).
    from backend.web.core import engine_swap

    uv_line = "& uv tool install --force --python $InstallPython @Withs $Spec"
    assert _PS1.count(uv_line) == 1
    call = "$code = Install-EngineSwapped { " + uv_line + " }"
    assert call in _PS1
    body = _PS1[_PS1.index("function Install-EngineSwapped") :]
    body = body[: body.index("\n}\n")]
    order = [
        "$env:UV_TOOL_DIR = Join-Path $Next 'tools'",  # 1. stage
        "Test-Engine (Join-Path $Next 'tools\\mindflock')",
        "MINDFLOCK_INSTALL_STOP_SERVER",
        "Move-Retrying $Exe $OldExe",  # 2. the shim first …
        "[IO.Directory]::Move($Venv, $Old)",  # … then the venv
        "$why = Test-Engine $Venv $ChkExe",  # 3. install + check
        "[IO.Directory]::Move($Old, $Venv)",  # 4. put back
    ]
    at = [body.index(x) for x in order]
    assert at == sorted(at), order
    # Never an in-place fallback when `uv tool dir` can't answer.
    assert "& $Install | Out-Host; return" not in body
    # Moved-aside copies live BESIDE the tools dir (uv errors inside it).
    assert '"$ToolDir-mindflock-old-$Stamp"' in body
    assert '"$ToolDir-mindflock-next"' in body
    # The same import check as the Python twin.
    assert "$EngineCheck = '%s'" % engine_swap.IMPORT_CHECK in _PS1
    assert engine_swap.STAGE_SUFFIX == "-mindflock-next"
    assert engine_swap.OLD_SUFFIX == "-mindflock-old-"
    # A server stopped for holding the shim starts again (unless the caller
    # — the desktop app — restarts it itself).
    assert "$script:Restart = Stop-EngineServer $Exe" in body
    assert "Start-Process -WindowStyle Hidden -FilePath $Shim -ArgumentList $a" in _PS1
