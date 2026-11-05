"""Launch scripts, the exit-marker wrapper, hooks and MCP attach as native
Windows writes them (docs/windows.md), pinned from Linux.

Each test flips ``backend.compat.IS_WINDOWS`` (and ``mux.IS_WINDOWS`` /
``osflags.IS_WINDOWS`` where that module reads its own) instead of running on
Windows; what only a real Windows box can show (Git Bash running the hook,
claude.exe's environment, the session host) was verified there by hand. The
Linux/macOS bytes are pinned unchanged by test_launch_parity's POSIX goldens
and the hook tests."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import zlib

import pytest

from backend import compat, osflags
from backend.providers import activity_markers as am
from backend.providers import launch_script, mcp_attach
from backend.session import mux

#: Tests that pin Linux/macOS bytes, or drive a hook through `sh` with POSIX
#: script shims: the Linux harness. The windows-latest CI job runs this file
#: too (docs/development.md), where they cannot hold.
posix_harness = pytest.mark.skipif(os.name == "nt", reason="the Linux harness")


@pytest.fixture
def win(monkeypatch):
    """Native Windows, as far as the launch/hook/MCP code can tell."""
    monkeypatch.setattr(compat, "IS_WINDOWS", True)
    monkeypatch.setattr(mux, "IS_WINDOWS", True)
    monkeypatch.setattr(osflags, "IS_WINDOWS", True)
    return monkeypatch


def _body(cmd: str) -> str:
    """The Python script inside a Windows hook command."""
    hexed = cmd.split("fromhex('", 1)[1].split("')", 1)[0]
    return zlib.decompress(bytes.fromhex(hexed)).decode("utf-8")


# --------------------------------------------------------------------------- #
# launch_script
# --------------------------------------------------------------------------- #
@posix_harness
def test_shell_path_is_identity_off_windows():
    assert launch_script.shell_path("/a/b c") == "/a/b c"
    assert launch_script.shell_path(r"odd\name") == r"odd\name"


def test_shell_path_forward_slashes_on_windows(win):
    assert launch_script.shell_path(r"C:\Users\me\wt\x.md") == "C:/Users/me/wt/x.md"


def test_no_oom_score_line_on_windows(win):
    assert launch_script.oom_score_line() == ""
    assert launch_script.oom_score_prefix() == ""


def test_standalone_launch_command_on_windows(win):
    pre, cmd = launch_script.launch_command("claude", r"C:\Users\me\p\prompt.md")
    assert "oom_score_adj" not in pre
    assert '"$(cat C:/Users/me/p/prompt.md)"' in cmd


def test_keystroke_seeder_paths_on_windows(win):
    fn = launch_script.seed_by_keys_function(
        r"C:\Users\me\My Work\.mindflock_prompt.md"
    )
    assert "load-buffer -b mf_prompt 'C:/Users/me/My Work/.mindflock_prompt.md'" in fn
    assert "\\" not in fn


# --------------------------------------------------------------------------- #
# provisioned.write_launcher
# --------------------------------------------------------------------------- #
def test_write_launcher_windows_bytes(win, tmp_path):
    from backend.session import provisioned

    path = provisioned.write_launcher(
        str(tmp_path), "line one\nline two", program="claude"
    )
    raw = open(path, "rb").read()
    assert b"\r" not in raw and b"oom_score_adj" not in raw
    assert raw.startswith(b"#!/usr/bin/env bash\ncd ")
    prompt = (tmp_path / provisioned.PROMPT_BASENAME).read_bytes()
    assert prompt == b"line one\nline two"


def test_write_launcher_returns_a_shell_path(win, monkeypatch, tmp_path):
    """The returned path doubles as the launch command, so it is spelled the way
    Git Bash runs it."""
    from backend.session import provisioned

    monkeypatch.setattr(
        launch_script, "shell_path", lambda p: "C:/fake/" + os.path.basename(str(p))
    )
    got = provisioned.write_launcher(str(tmp_path), "", program="claude")
    assert got == "C:/fake/" + provisioned.LAUNCHER_BASENAME


# --------------------------------------------------------------------------- #
# The exit-marker wrapper (backend.web.core.terminal)
# --------------------------------------------------------------------------- #
def test_wrap_launch_cmd_quotes_a_native_launcher_path(win, monkeypatch):
    from backend.web.core import terminal

    native = r"C:\Users\me\.mindflock\worktrees\wl1\.mindflock_launch.sh"
    monkeypatch.setattr(terminal.os.path, "isfile", lambda p: p == native)
    wrapped = terminal._wrap_launch_cmd("export A=1\n" + native, "mindflock_wl1")
    assert "export A=1\nC:/Users/me/.mindflock/worktrees/wl1/.mindflock_launch.sh;" in (
        wrapped
    )
    assert "\\" not in wrapped
    assert "oom_score_adj" not in wrapped
    assert wrapped.startswith("rm -f ") and wrapped.rstrip().split()[-2] == ">"


def test_wrap_launch_cmd_leaves_commands_alone(win):
    from backend.web.core import terminal

    cmd = "claude '--mcp-config=C:\\x\\mcp.json' --continue"
    wrapped = terminal._wrap_launch_cmd(cmd, "mindflock_wl1")
    assert "; " + cmd + "; echo $? > " in wrapped


@posix_harness
def test_wrap_launch_cmd_posix_unchanged(monkeypatch, tmp_path):
    from backend.web.core import terminal

    native = str(tmp_path / "odd\\name.sh")
    open(native, "w").close()
    wrapped = terminal._wrap_launch_cmd(native, "mindflock_x")
    assert "; " + native + "; echo $? > " in wrapped


# --------------------------------------------------------------------------- #
# Hook commands
# --------------------------------------------------------------------------- #
_ALL_HOOKS = (
    lambda: am.hook_command("working"),
    lambda: am.hook_command("working", tool_hook="pre"),
    lambda: am.hook_command("idle", tool_hook="post"),
    lambda: am.hook_command("working", record_thread=False, tool_hook="fail"),
    lambda: am.notification_hook_command(),
)


@pytest.mark.parametrize("make", _ALL_HOOKS)
def test_windows_hook_command_shape(win, make):
    win.setattr(
        sys,
        "executable",
        r"C:\Users\someone-long\AppData\Roaming\uv\tools\mindflock\Scripts\python.exe",
    )
    cmd = make()
    exe = "C:/Users/someone-long/AppData/Roaming/uv/tools/mindflock/Scripts/python.exe"
    assert cmd.startswith(exe + ' -c "exec(')
    assert "python3" not in cmd.split("#", 1)[0]
    # Git Bash cuts a longer quoted argument (measured ~8,190 chars).
    assert len(cmd) <= am.WINDOWS_HOOK_MAX
    # One double-quoted argument whose characters mean the same in bash, cmd
    # and PowerShell; the tag rides after it as a comment.
    arg = cmd.split(' -c "', 1)[1].split('"', 1)[0]
    assert not set(arg) & set('$`\\"%^&|<>!;, \t\r\n/=:')
    assert "# mindflock-activity" in cmd.split('"')[-1]
    body = _body(cmd)
    compile(body, "<hook>", "exec")
    assert body.startswith("try:\n") and body.endswith("except Exception:\n    pass\n")
    assert "__import__('shutil').which('tmux') or 'tmux'" in body
    assert "run(['tmux'," not in body


def test_windows_tool_hook_loads_the_guard_from_its_file(win):
    cmd = am.hook_command("working", tool_hook="pre")
    body = _body(cmd)
    assert "_MF_SRC=''" in body and "def _mf_tool_hook" not in body
    assert "'tool-hook-%s.py'" % am._source_hash8() in body
    assert cmd.endswith(am.TOOL_HOOK_TAG)


def test_windows_exe_with_spaces_is_double_quoted(win):
    win.setattr(sys, "executable", r"C:\Program Files\Py\python.exe")
    assert am.hook_command("idle").startswith('"C:/Program Files/Py/python.exe" -c ')


def test_windows_pythonw_is_swapped_for_python(win, tmp_path):
    (tmp_path / "python.exe").write_text("")
    (tmp_path / "pythonw.exe").write_text("")
    win.setattr(sys, "executable", str(tmp_path / "pythonw.exe"))
    assert am.hook_python() == str(tmp_path / "python.exe").replace("\\", "/")


@posix_harness
def test_posix_hook_command_unchanged():
    assert am.hook_command("working").startswith("python3 -c '")
    assert " || true # mindflock-activity" in am.hook_command("working")


def test_ensure_tool_hook_file_writes_and_heals(tmp_path):
    path = am.tool_hook_file()
    assert os.path.dirname(path) == os.environ["MINDFLOCK_RED_ZONE_DIR"]
    assert am.ensure_tool_hook_file()
    data = open(path, "rb").read()
    assert data == am._tool_hook_source().encode("utf-8")
    with open(path, "ab") as f:
        f.write(b"\n_tampered = 1\n")
    assert am.ensure_tool_hook_file()
    assert open(path, "rb").read() == data


def test_merge_on_windows_writes_the_guard_file(win, tmp_path):
    settings = tmp_path / "wt" / ".claude" / "hooks.json"
    am.merge_activity_hooks(
        settings,
        [("PreToolUse", "working"), ("Stop", "idle")],
        "mindflock_x",
        tool_hook_events={"PreToolUse": "pre"},
    )
    assert os.path.isfile(am.tool_hook_file())
    cmds = [
        h["command"]
        for es in json.loads(settings.read_text())["hooks"].values()
        for e in es
        for h in e["hooks"]
    ]
    assert all(len(c) <= am.WINDOWS_HOOK_MAX for c in cmds)


# --- the Windows-format hook RUN end to end (sh stands in for Git Bash) ----- #
@pytest.fixture
def zoned(tmp_path):
    from backend.config import red_zones as rz

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "secret.txt").write_text("x\n")
    (repo / "app.py").write_text("x = 1\n")
    rid = rz.repo_identity(str(repo))[0]
    rz.add_zone("repo", rid, "secret.txt", name="secret", label="r")
    assert rz.sync_guard(os.path.realpath(str(repo)), rid) == "written"
    env = {
        **os.environ,
        "MINDFLOCK_SESSION_NAME": "mindflock_w1",
        "MINDFLOCK_ACTIVITY_MARKER_DIR": str(tmp_path / "markers"),
        "MINDFLOCK_THREAD_MARKER_DIR": str(tmp_path / "threads"),
    }
    return str(repo), env


def _fire(cmd, payload, env):
    cp = subprocess.run(
        ["sh", "-c", cmd],
        input=json.dumps(payload).encode(),
        capture_output=True,
        env=env,
        timeout=60,
    )
    assert cp.returncode == 0, cp.stderr
    out = cp.stdout.decode().strip()
    return (
        json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out else "allow"
    )


def _payload(repo, tool, ti):
    return {"session_id": "s", "cwd": repo, "tool_name": tool, "tool_input": ti}


@posix_harness
def test_windows_tool_hook_denies_from_its_guard_file(win, zoned):
    repo, env = zoned
    cmd = am.hook_command("working", tool_hook="pre")
    assert am.ensure_tool_hook_file()
    secret = os.path.join(repo, "secret.txt")
    assert _fire(cmd, _payload(repo, "Write", {"file_path": secret}), env) == "deny"
    assert (
        _fire(
            cmd,
            _payload(
                repo, "PowerShell", {"command": "Set-Content -Path secret.txt -Value 1"}
            ),
            env,
        )
        == "deny"
    )
    assert _fire(
        cmd, _payload(repo, "Write", {"file_path": repo + "/app.py"}), env
    ) == ("allow")
    marker = os.path.join(env["MINDFLOCK_ACTIVITY_MARKER_DIR"], "mindflock_w1.json")
    assert json.load(open(marker))["state"] == "working"


@posix_harness
def test_windows_tool_hook_skips_a_tampered_guard_file(win, zoned):
    """Modified bytes are never run: the guard is skipped (fail-open, like any
    guard failure) and the marker is still written; the next install heals."""
    repo, env = zoned
    cmd = am.hook_command("working", tool_hook="pre")
    assert am.ensure_tool_hook_file()
    with open(am.tool_hook_file(), "a", encoding="utf-8") as f:
        f.write("\nraise SystemExit(7)\n")
    secret = os.path.join(repo, "secret.txt")
    assert _fire(cmd, _payload(repo, "Write", {"file_path": secret}), env) == "allow"
    assert os.path.isfile(
        os.path.join(env["MINDFLOCK_ACTIVITY_MARKER_DIR"], "mindflock_w1.json")
    )
    am.ensure_tool_hook_file()
    assert _fire(cmd, _payload(repo, "Write", {"file_path": secret}), env) == "deny"


@posix_harness
def test_windows_hook_resolves_the_session_through_the_tmux_shim(win, tmp_path):
    """No MINDFLOCK_SESSION_NAME: the hook asks the `tmux` on PATH (the session
    host's shim) for its own pane's session."""
    shim = tmp_path / "bin"
    shim.mkdir()
    (shim / "tmux").write_text(
        '#!/bin/sh\n[ "$1 $4" = "display-message %7" ] && echo mindflock_shim\n'
    )
    os.chmod(shim / "tmux", 0o755)
    env = {k: v for k, v in os.environ.items() if k != "MINDFLOCK_SESSION_NAME"} | {
        "PATH": str(shim) + os.pathsep + os.environ.get("PATH", ""),
        "TMUX": "winmux,1,0",
        "TMUX_PANE": "%7",
        "MINDFLOCK_ACTIVITY_MARKER_DIR": str(tmp_path / "markers"),
    }
    subprocess.run(
        ["sh", "-c", am.hook_command("idle", record_thread=False)],
        input=b"{}",
        env=env,
        check=True,
        timeout=60,
    )
    marker = tmp_path / "markers" / "mindflock_shim.json"
    assert json.loads(marker.read_text())["state"] == "idle"


# --------------------------------------------------------------------------- #
# MCP attach
# --------------------------------------------------------------------------- #
@posix_harness
def test_codex_env_vars_posix_unchanged():
    assert mcp_attach.codex_env_vars() == (
        "TMUX",
        "TMUX_PANE",
        "TMUX_TMPDIR",
        "MINDFLOCK_AUTH_TOKEN",
    )


def test_codex_env_vars_windows(win):
    names = mcp_attach.codex_env_vars()
    for k in ("TMUX", "TMUX_PANE", "SystemRoot", "USERPROFILE", "PATH", "APPDATA"):
        assert k in names
    for k in ("LOCALAPPDATA", "COMSPEC", "MINDFLOCK_HOME"):
        assert k in names
    spec = mcp_attach.McpSpec(
        title="t", tmux_name="mindflock_t", python=r"C:\py\python.exe"
    )
    table = mcp_attach.codex_server_table(spec)
    assert '"SystemRoot"' in table and 'command="C:\\\\py\\\\python.exe"' in table


# --------------------------------------------------------------------------- #
# The standalone launcher (backend.ticket_ingestion.claude_runner)
# --------------------------------------------------------------------------- #
def test_claude_runner_goes_through_the_session_host_on_windows(win, tmp_path):
    from backend.ticket_ingestion import claude_runner

    calls = []

    def fake_run(argv, **kw):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    win.setattr(mux, "run", fake_run)
    win.setattr(compat, "posix_shell", lambda bash=False: r"C:\Git\bin\bash.exe")

    async def no_exec(*a, **k):  # the POSIX path must not run
        raise AssertionError("create_subprocess_exec on Windows: %r" % (a,))

    win.setattr(asyncio, "create_subprocess_exec", no_exec)
    runner = claude_runner.AgentCliRunner.__new__(claude_runner.AgentCliRunner)
    prompt = tmp_path / "p.md"
    prompt.write_text("hi")
    asyncio.run(runner._start_tmux_session("tkt", tmp_path, prompt, "claude"))
    new = calls[0]
    assert new[:5] == ["tmux", "new-session", "-d", "-s", "tkt"]
    assert new[7:9] == [r"C:\Git\bin\bash.exe", "-ilc"]
    assert "oom_score_adj" not in new[9]
    assert [c[:2] for c in calls[1:]] == [["tmux", "set-option"]] * 2
    asyncio.run(runner._kill_existing_session("tkt"))
    assert calls[-1] == ["tmux", "kill-session", "-t", "tkt"]


@posix_harness
def test_claude_runner_posix_still_execs_tmux(monkeypatch, tmp_path):
    from backend.ticket_ingestion import claude_runner

    seen = []

    class P:
        returncode = 0

        async def communicate(self):
            return b"", b""

        async def wait(self):
            return 0

    async def fake_exec(*argv, **kw):
        seen.append(argv)
        return P()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    runner = claude_runner.AgentCliRunner.__new__(claude_runner.AgentCliRunner)
    prompt = tmp_path / "p.md"
    prompt.write_text("hi")
    asyncio.run(runner._start_tmux_session("tkt", tmp_path, prompt, "claude"))
    assert seen[0][:9] == (
        "tmux",
        "new-session",
        "-d",
        "-s",
        "tkt",
        "-c",
        str(tmp_path),
        "bash",
        "-ilc",
    )
