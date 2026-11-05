"""The red-zone guard on Claude Code's PowerShell tool (native Windows) and on
Git Bash path spellings — :func:`_ps_targets`, :func:`_mf_native` and the
PowerShell branch of ``_classify`` in ``backend.providers._tool_hook_src``."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from backend.config import red_zones as rz
from backend.providers import _tool_hook_src as th
from backend.providers import activity_markers as am


def _w(cmd, cwd="/repo"):
    tags = {}
    w, _r, ok = th._ps_targets(cmd, cwd, tags=tags)
    return ok, sorted(w), {k: sorted(v) for k, v in tags.items()}


@pytest.mark.parametrize(
    "cmd,writes",
    [
        ("Set-Content -Path config.toml -Value 1", ["/repo/config.toml"]),
        ("set-content config.toml 'x'", ["/repo/config.toml"]),
        ("sc -Pa 'my file.txt' -Val 1", ["/repo/my file.txt"]),
        ("Add-Content -LiteralPath:log.txt -Value x", ["/repo/log.txt"]),
        ("'x' | Out-File -FilePath out/a.txt -Encoding utf8", ["/repo/out/a.txt"]),
        ("Get-Date | Tee-Object -FilePath t.txt", ["/repo/t.txt"]),
        ("echo hi > b.txt; echo x 2>> c.log; dir 2>&1", ["/repo/b.txt", "/repo/c.log"]),
        ("echo hi *> all.txt", ["/repo/all.txt"]),
        ("echo hi >quoted.txt", ["/repo/quoted.txt"]),
        ("echo 'a > b'", []),
        ("Remove-Item -Recurse -Force build, dist", ["/repo/build", "/repo/dist"]),
        (
            "rm a.txt; del b.txt; ri c.txt",
            ["/repo/a.txt", "/repo/b.txt", "/repo/c.txt"],
        ),
        ("Move-Item src.txt dst.txt", ["/repo/dst.txt", "/repo/src.txt"]),
        ("Copy-Item -Path a.txt -Destination config.toml", ["/repo/config.toml"]),
        ("cp a.txt b.txt", ["/repo/b.txt"]),
        (
            "Rename-Item -Path d/old.txt -NewName new.txt",
            ["/repo/d/new.txt", "/repo/d/old.txt"],
        ),
        ("New-Item -Path sub -Name f.txt -ItemType File", ["/repo/sub/f.txt"]),
        ("ni x.txt", ["/repo/x.txt"]),
        ("Invoke-WebRequest https://e.x/f -OutFile f.bin", ["/repo/f.bin"]),
        ("[System.IO.File]::WriteAllText('/repo/x.txt', 'hi')", ["/repo/x.txt"]),
        ('[IO.File]::Delete("/repo/y.txt")', ["/repo/y.txt"]),
        ("cd sub; Set-Content f 1", ["/repo/sub/f"]),
        ("Set-Location -Path sub\nSet-Content f 1", ["/repo/sub/f"]),
        (
            "Get-ChildItem *.py | ForEach-Object { Remove-Item $_.FullName }",
            ["/repo/$_.FullName"],
        ),
        ("git checkout -- config.toml", ["/repo/config.toml"]),
        ("& git rm -- old.py", ["/repo/old.py"]),
        ("Remove-Item x.txt -WhatIf", []),
        ("Get-Content config.toml | Select-String x", []),
        ('Write-Output "it\'s fine" # a comment > nope', []),
        ("<# block > comment #> Get-Item a", []),
        ("Set-Content `\n  -Path cont.txt -Value 1", ["/repo/cont.txt"]),
    ],
)
def test_ps_targets(cmd, writes):
    ok, got, _tags = _w(cmd)
    assert ok
    assert got == writes


def test_ps_tags():
    assert _w("Remove-Item a")[2] == {"/repo/a": ["rm"]}
    assert _w("New-Item -ItemType Directory -Path nd")[2] == {"/repo/nd": ["mkdir"]}
    assert _w("git checkout -- a")[2] == {"/repo/a": ["gitrevert"]}
    assert "dyn" in _w("Set-Content -Path $env:TEMP/x -Value 1")[2]["/repo/$env:TEMP/x"]
    assert "dyn" in _w('Set-Content -Path "$HOME/x" -Value 1')[2]["/repo/$HOME/x"]


def test_ps_unparseable():
    assert th._ps_targets("Set-Content 'unterminated", "/repo") == (set(), set(), False)
    assert th._ps_targets("<# never closed", "/repo")[2] is False


def test_classify_powershell_is_a_shell_call():
    kind, w, r, extra = th._classify("PowerShell", {"command": "rm x"})
    assert (kind, w, r) == ("bash", [], [])
    assert extra == {"cmd": "rm x", "ps": True}
    assert "ps" not in th._classify("Bash", {"command": "rm x"})[3]


def test_mf_native_maps_git_bash_paths_on_windows_only():
    assert th._mf_native("/c/Users/x", nt=True) == "C:/Users/x"
    assert th._mf_native("/d", nt=True) == "D:/"
    assert th._mf_native("/tmp/x", nt=True) == "/tmp/x"
    assert th._mf_native("C:/x", nt=True) == "C:/x"
    assert th._mf_native("/c/Users/x", nt=False) == "/c/Users/x"
    assert th._mf_native(None, nt=True) is None


# --------------------------------------------------------------------------- #
# end to end: the real (POSIX) hook command, a PowerShell payload
# --------------------------------------------------------------------------- #
@pytest.fixture
def zoned(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "config.toml").write_text("secret=1\n")
    (repo / "app.py").write_text("x = 1\n")
    rid = rz.repo_identity(str(repo))[0]
    rz.add_zone("repo", rid, "config.toml", name="config", label="r")
    assert rz.sync_guard(os.path.realpath(str(repo)), rid) == "written"
    env = {
        **os.environ,
        "MINDFLOCK_SESSION_NAME": "mindflock_ps1",
        "MINDFLOCK_ACTIVITY_MARKER_DIR": str(tmp_path / "markers"),
        "MINDFLOCK_THREAD_MARKER_DIR": str(tmp_path / "threads"),
    }
    return str(repo), env


def _pre(repo, env, command):
    payload = {
        "session_id": "s",
        "cwd": repo,
        "tool_name": "PowerShell",
        "tool_input": {"command": command},
        "tool_use_id": "tu",
    }
    cp = subprocess.run(
        ["sh", "-c", am.hook_command("working", tool_hook="pre")],
        input=json.dumps(payload).encode(),
        capture_output=True,
        env=env,
        timeout=60,
    )
    assert cp.returncode == 0
    out = cp.stdout.decode().strip()
    return json.loads(out)["hookSpecificOutput"] if out else None


@pytest.mark.parametrize(
    "command",
    [
        "Set-Content -Path config.toml -Value 2",
        "'x' | Out-File config.toml",
        "echo x > config.toml",
        "Remove-Item -Force config.toml",
        "Copy-Item app.py config.toml",
        "Remove-Item -Recurse .",
    ],
)
def test_e2e_powershell_write_into_zone_denied(zoned, command):
    repo, env = zoned
    out = _pre(repo, env, command)
    assert out and out["permissionDecision"] == "deny"
    assert "config.toml" in out["permissionDecisionReason"] or "zone" in (
        out["permissionDecisionReason"].lower()
    )


@pytest.mark.parametrize(
    "command",
    ["Set-Content -Path app.py -Value 2", "Get-Content config.toml", "git status"],
)
def test_e2e_powershell_outside_zone_allowed(zoned, command):
    repo, env = zoned
    assert _pre(repo, env, command) is None


def test_e2e_powershell_protect_marker_denied(zoned):
    repo, env = zoned
    out = _pre(repo, env, "Set-Content .claude/settings.local.json '{}'")
    assert out and out["permissionDecision"] == "deny"


def test_e2e_powershell_feed_record(zoned):
    repo, env = zoned
    _pre(repo, env, "Set-Content -Path app.py -Value 2")
    feed = rz.feed_path("mindflock_ps1")
    rec = [json.loads(x) for x in open(feed).read().splitlines() if x.strip()][-1]
    assert rec["tool"] == "PowerShell" and rec["kind"] == "bash"
    assert rec["writes"] == [os.path.join(repo, "app.py")]
