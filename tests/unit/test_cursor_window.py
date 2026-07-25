"""Hermetic tests for the cursor-window / agent-activity helpers in
``backend.web.server``.

Every external effect (powershell.exe, xdotool, tmux) is mocked. No real
process is ever launched.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import types

import pytest

from backend import session
from backend.web import server
from backend.web.core import cursor_windows

# --------------------------------------------------------------------------- #
# _cursor_title_terms
# --------------------------------------------------------------------------- #


def test_cursor_title_terms_hex_worktree_yields_slug_and_full():
    # Worktrees are "<slug>_<hex>"; both the full dir name and the bare slug
    # are returned (a title may omit the hash).
    terms = server._cursor_title_terms("/home/u/wt/myproj_deadbeef")
    assert terms == ["myproj_deadbeef", "myproj"]


def test_cursor_title_terms_plain_slug_only_full():
    # No trailing hex suffix -> only the basename.
    terms = server._cursor_title_terms("/home/u/wt/plainname")
    assert terms == ["plainname"]


def test_cursor_title_terms_short_hex_not_split():
    # The regex requires >=8 hex chars; a 4-char suffix must not split.
    terms = server._cursor_title_terms("/home/u/wt/proj_abcd")
    assert terms == ["proj_abcd"]


def test_cursor_title_terms_slug_with_multiple_underscores():
    # The slug portion is greedy: "my_cool_proj" + "_<hex>".
    terms = server._cursor_title_terms("/x/my_cool_proj_0123456789ab")
    assert terms == ["my_cool_proj_0123456789ab", "my_cool_proj"]


def test_cursor_title_terms_empty_path():
    # NOTE: os.path.normpath("") -> "." (never empty), so the ``if not base``
    # guard never fires; the current implementation returns ["."] for "".
    assert server._cursor_title_terms("") == ["."]


def test_cursor_title_terms_none_path():
    # Same normpath("") -> "." behavior for a None path.
    assert server._cursor_title_terms(None) == ["."]


def test_cursor_title_terms_trailing_slash_normalized():
    # normpath strips the trailing slash so the basename is the dir name.
    terms = server._cursor_title_terms("/home/u/wt/proj_aabbccdd/")
    assert terms == ["proj_aabbccdd", "proj"]


# --------------------------------------------------------------------------- #
# _win_title_condition
# --------------------------------------------------------------------------- #


def test_win_title_condition_single_term():
    cond = server._win_title_condition(["foo"])
    assert cond == "$_.MainWindowTitle -like '*foo*'"


def test_win_title_condition_multiple_terms_ored():
    cond = server._win_title_condition(["foo", "bar"])
    assert cond == (
        "$_.MainWindowTitle -like '*foo*' -or $_.MainWindowTitle -like '*bar*'"
    )


def test_win_title_condition_escapes_single_quotes():
    # PowerShell single-quote escaping doubles the quote.
    cond = server._win_title_condition(["it's"])
    assert cond == "$_.MainWindowTitle -like '*it''s*'"


def test_win_title_condition_empty():
    assert server._win_title_condition([]) == ""


# --------------------------------------------------------------------------- #
# _ps_encoded
# --------------------------------------------------------------------------- #


def test_ps_encoded_is_base64_of_utf16le():
    script = "Write-Host 'hi'"
    enc = server._ps_encoded(script)
    # Round-trips: base64 -> utf-16-le bytes -> original script.
    assert base64.b64decode(enc).decode("utf-16-le") == script


def test_ps_encoded_ascii_only_output():
    enc = server._ps_encoded("Get-Process | Where-Object {$_.Id -gt 0}")
    # The payload itself must be pure ASCII (safe to pass on a command line).
    enc.encode("ascii")  # would raise if non-ascii
    assert isinstance(enc, str)


def test_ps_encoded_unicode_content_survives():
    script = "echo '❯ 1.'"
    enc = server._ps_encoded(script)
    assert base64.b64decode(enc).decode("utf-16-le") == script


# --------------------------------------------------------------------------- #
# _powershell
# --------------------------------------------------------------------------- #


def test_powershell_returns_which_result(monkeypatch):
    monkeypatch.setattr(
        server.shutil,
        "which",
        lambda name: "/mnt/c/ps.exe" if name == "powershell.exe" else None,
    )
    assert server._powershell() == "/mnt/c/ps.exe"


def test_powershell_absent(monkeypatch):
    monkeypatch.setattr(server.shutil, "which", lambda name: None)
    assert server._powershell() is None


# --------------------------------------------------------------------------- #
# _find_cursor_windows
# --------------------------------------------------------------------------- #


def test_find_cursor_windows_empty_path_returns_empty(monkeypatch):
    # Guard clause: empty path -> [] without ever touching xdotool.
    called = {"n": 0}

    def _which(_name):
        called["n"] += 1
        return "/usr/bin/xdotool"

    monkeypatch.setattr(server.shutil, "which", _which)
    assert server._find_cursor_windows("") == []
    # short-circuits on the falsy path before calling which
    assert called["n"] == 0


def test_find_cursor_windows_no_xdotool_returns_empty(monkeypatch):
    monkeypatch.setattr(server.shutil, "which", lambda name: None)
    # subprocess.run must NOT be invoked when xdotool is absent.
    monkeypatch.setattr(
        server.subprocess,
        "run",
        lambda *a, **k: pytest.fail("subprocess.run called with no xdotool"),
    )
    assert server._find_cursor_windows("/x/proj_aabbccdd") == []


def test_find_cursor_windows_collects_dedup_ids(monkeypatch):
    monkeypatch.setattr(server.shutil, "which", lambda name: "/usr/bin/xdotool")

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        term = cmd[-1]
        # Both search terms return overlapping window ids -> must dedup.
        mapping = {
            "proj_aabbccdd": b"100\n200\n",
            "proj": b"200\n300\n",
        }
        return types.SimpleNamespace(returncode=0, stdout=mapping.get(term, b""))

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    ids = server._find_cursor_windows("/x/proj_aabbccdd")
    assert ids == ["100", "200", "300"]
    # invoked once per title term
    assert all(c[0] == "xdotool" and c[1] == "search" for c in calls)
    assert [c[-1] for c in calls] == ["proj_aabbccdd", "proj"]


def test_find_cursor_windows_no_matches(monkeypatch):
    monkeypatch.setattr(server.shutil, "which", lambda name: "/usr/bin/xdotool")
    monkeypatch.setattr(
        server.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout=b""),
    )
    assert server._find_cursor_windows("/x/proj_aabbccdd") == []


def test_find_cursor_windows_swallows_subprocess_error(monkeypatch):
    monkeypatch.setattr(server.shutil, "which", lambda name: "/usr/bin/xdotool")

    def boom(*a, **k):
        raise OSError("xdotool blew up")

    monkeypatch.setattr(server.subprocess, "run", boom)
    # Exception is caught internally -> returns [] (best-effort).
    assert server._find_cursor_windows("/x/proj_aabbccdd") == []


# --------------------------------------------------------------------------- #
# _cursor_windows_open
# --------------------------------------------------------------------------- #


def test_cursor_windows_open_empty_terms_false(monkeypatch):
    # _cursor_title_terms can only be empty if we force it; monkeypatch it to []
    # to exercise the ``if not terms`` guard -> False, no subprocess touched.
    monkeypatch.setattr(server, "_cursor_title_terms", lambda p: [])
    monkeypatch.setattr(
        server, "_powershell", lambda: pytest.fail("_powershell called for empty terms")
    )
    assert server._cursor_windows_open("/x/whatever") is False


def test_cursor_windows_open_powershell_yes(monkeypatch):
    monkeypatch.setattr(server, "_powershell", lambda: "/mnt/c/ps.exe")
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return types.SimpleNamespace(returncode=0, stdout=b"YES\r\n")

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    assert server._cursor_windows_open("/x/proj_aabbccdd") is True
    # Uses -EncodedCommand with the powershell exe, and a timeout guard.
    assert seen["cmd"][0] == "/mnt/c/ps.exe"
    assert "-EncodedCommand" in seen["cmd"]
    assert seen["kwargs"].get("timeout") == 15


def test_cursor_windows_open_powershell_no(monkeypatch):
    monkeypatch.setattr(server, "_powershell", lambda: "/mnt/c/ps.exe")
    monkeypatch.setattr(
        server.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout=b"NO\r\n"),
    )
    assert server._cursor_windows_open("/x/proj_aabbccdd") is False


def test_cursor_windows_open_powershell_exception_false(monkeypatch):
    monkeypatch.setattr(server, "_powershell", lambda: "/mnt/c/ps.exe")

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="ps", timeout=15)

    monkeypatch.setattr(server.subprocess, "run", boom)
    assert server._cursor_windows_open("/x/proj_aabbccdd") is False


def test_cursor_windows_open_linux_fallback_true(monkeypatch):
    # No powershell + native Linux -> falls back to xdotool via
    # _find_cursor_windows. Force os_kind so this holds on a macOS CI runner,
    # which would otherwise take the pgrep branch.
    from backend import osenv

    monkeypatch.setattr(server, "_powershell", lambda: None)
    monkeypatch.setattr(osenv, "os_kind", lambda: "linux")
    monkeypatch.setattr(server, "_find_cursor_windows", lambda p: ["42"])
    assert server._cursor_windows_open("/x/proj_aabbccdd") is True


def test_cursor_windows_open_linux_fallback_false(monkeypatch):
    from backend import osenv

    monkeypatch.setattr(server, "_powershell", lambda: None)
    monkeypatch.setattr(osenv, "os_kind", lambda: "linux")
    monkeypatch.setattr(server, "_find_cursor_windows", lambda p: [])
    assert server._cursor_windows_open("/x/proj_aabbccdd") is False


# --------------------------------------------------------------------------- #
# _focus_cursor_window / minimized-window restore (EnumWindows regression)
# --------------------------------------------------------------------------- #


def _decode_encoded_command(cmd):
    """Extract and decode the PowerShell -EncodedCommand payload from an argv."""
    i = cmd.index("-EncodedCommand")
    return base64.b64decode(cmd[i + 1]).decode("utf-16-le")


def test_focus_cursor_window_script_enumerates_all_windows(monkeypatch):
    # Regression: the focus path must enumerate ALL top-level windows, not filter
    # Get-Process by MainWindowTitle (which sees only one window per Electron
    # process, so a minimized workspace window never restored — it only flashed).
    monkeypatch.setattr(server, "_powershell", lambda: "/mnt/c/ps.exe")
    seen = {}

    def fake_popen(cmd, **kwargs):
        seen["cmd"] = cmd
        return types.SimpleNamespace()

    monkeypatch.setattr(server.subprocess, "Popen", fake_popen)
    server._focus_cursor_window("/x/proj_aabbccdd")

    script = _decode_encoded_command(seen["cmd"])
    assert "EnumWindows" in script
    assert "Get-Process" not in script  # no per-process MainWindowHandle filter
    assert "ShowWindow($h,9)" in script  # SW_RESTORE un-minimizes
    # App needle + workspace terms are injected as PowerShell literals, terms
    # most-specific first so the full basename wins over the bare slug.
    assert "$terms=@('proj_aabbccdd','proj')" in script
    assert "$app='Cursor'" in script


def test_cursor_windows_open_script_enumerates_all_windows(monkeypatch):
    monkeypatch.setattr(server, "_powershell", lambda: "/mnt/c/ps.exe")
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return types.SimpleNamespace(returncode=0, stdout=b"YES\r\n")

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    assert server._cursor_windows_open("/x/proj_aabbccdd") is True
    script = _decode_encoded_command(seen["cmd"])
    assert "EnumWindows" in script and "Get-Process" not in script
    assert "[FgWin]::Find($app,$terms)" in script


def _force_macos(monkeypatch, app="Cursor"):
    from backend import osenv
    from backend.config import ide as ide_cfg

    monkeypatch.setattr(server, "_powershell", lambda: None)  # no powershell.exe
    monkeypatch.setattr(osenv, "os_kind", lambda: "macos")
    monkeypatch.setattr(
        ide_cfg,
        "ide_spec",
        lambda: ide_cfg.IdeSpec("cursor", "Cursor", "gui", "Cursor", "Cursor", app),
    )


def test_focus_cursor_window_macos_uses_open_a(monkeypatch):
    # macOS has no powershell and no xdotool — focus must reuse+activate the
    # workspace window via `open -a <App> <path>` (which un-minimizes it).
    _force_macos(monkeypatch)
    seen = {}
    monkeypatch.setattr(
        server.subprocess,
        "run",
        lambda cmd, **k: seen.setdefault("cmd", cmd)
        or types.SimpleNamespace(returncode=0),
    )
    server._focus_cursor_window("/x/proj_aabbccdd")
    assert seen["cmd"] == ["open", "-a", "Cursor", "/x/proj_aabbccdd"]


def test_focus_cursor_window_macos_noop_without_bundle(monkeypatch):
    # No app-bundle name -> we must NOT run `open <dir>` (that opens Finder).
    _force_macos(monkeypatch, app=None)
    called = {"n": 0}
    monkeypatch.setattr(
        server.subprocess,
        "run",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1),
    )
    server._focus_cursor_window("/x/proj_aabbccdd")
    assert called["n"] == 0


def test_cursor_windows_open_macos_uses_pgrep(monkeypatch):
    _force_macos(monkeypatch)
    seen = {}

    def fake_run(cmd, **k):
        seen["cmd"] = cmd
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    assert server._cursor_windows_open("/x/proj_aabbccdd") is True
    assert seen["cmd"] == ["pgrep", "-f", "Cursor.app"]


def test_cursor_windows_open_macos_false_when_not_running(monkeypatch):
    _force_macos(monkeypatch)
    monkeypatch.setattr(
        server.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=1)
    )
    assert server._cursor_windows_open("/x/proj_aabbccdd") is False


def test_focus_cursor_window_linux_activates_single_best(monkeypatch):
    # Native Linux: activate only the most-specific match (first), not every
    # sibling worktree window that happens to share the slug.
    from backend import osenv

    monkeypatch.setattr(server, "_powershell", lambda: None)
    monkeypatch.setattr(osenv, "os_kind", lambda: "linux")
    monkeypatch.setattr(server, "_find_cursor_windows", lambda p: ["11", "22"])
    activated = []
    monkeypatch.setattr(
        server.subprocess,
        "run",
        lambda cmd, **k: activated.append(cmd) or types.SimpleNamespace(returncode=0),
    )
    server._focus_cursor_window("/x/proj_aabbccdd")
    assert activated == [["xdotool", "windowactivate", "--sync", "11"]]


# --------------------------------------------------------------------------- #
# _cursor_uri_to_path (URI -> local path feeding auto-adopt)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "uri, expected",
    [
        # Remote-WSL: the wsl+<distro> netloc is this machine; take the path.
        ("vscode-remote://wsl+Ubuntu/home/u/proj", "/home/u/proj"),
        # A percent-encoded path segment is unquoted.
        ("vscode-remote://wsl+Ubuntu/home/u/my%20proj", "/home/u/my proj"),
        # file:// Windows drive path -> the /mnt/<drive> mount (drive lowercased).
        ("file:///C:/Users/x/repo", "/mnt/c/Users/x/repo"),
        # file:// with a plain POSIX path passes through unchanged.
        ("file:///home/u/p", "/home/u/p"),
        # A non-WSL vscode-remote host (SSH) is not this machine -> "".
        ("vscode-remote://ssh-remote+host/home/u/p", ""),
        # A dev-container remote is likewise not adoptable -> "".
        ("vscode-remote://dev-container+abc123/workspace", ""),
        # Any other scheme -> "".
        ("https://example.com/x", ""),
        # Empty string -> "".
        ("", ""),
    ],
)
def test_cursor_uri_to_path(uri, expected):
    assert server._cursor_uri_to_path(uri) == expected


def test_cursor_uri_to_path_none_and_non_str():
    # Non-string / falsy input is rejected before urlparse ever runs.
    assert server._cursor_uri_to_path(None) == ""
    assert server._cursor_uri_to_path(123) == ""


# --------------------------------------------------------------------------- #
# IDE endpoints (D2 picker feed + D5 generic route)
# --------------------------------------------------------------------------- #


def _route_paths():
    return {getattr(r, "path", None) for r in server.app.routes}


def test_ide_route_is_registered():
    paths = _route_paths()
    assert "/api/instances/{title}/ide" in paths
    assert "/api/instances/{title}/cursor" not in paths


def test_api_ides_route_registered():
    assert "/api/ides" in _route_paths()


def test_list_ides_shape_and_installed_flags(monkeypatch, tmp_path):
    import json

    from backend.config import ide as ide_cfg
    from backend.config import settings as S
    from backend.web.core import ide_launch

    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("MINDFLOCK_IDE", "cursor")
    S.invalidate()
    try:
        monkeypatch.setattr(
            ide_launch,
            "detect_ides",
            lambda: [s for s in ide_cfg.known_ide_specs() if s.command == "code"],
        )
        payload = json.loads(server.list_ides().body)
    finally:
        S.invalidate()

    assert payload["current"] == "cursor"
    assert payload["current_name"] == "Cursor"
    by_cmd = {e["command"]: e for e in payload["ides"]}
    # Known-but-missing IDEs are listed with installed=false (UI grays them out).
    assert by_cmd["code"]["installed"] is True
    assert by_cmd["cursor"]["installed"] is False
    assert by_cmd["code"] == {
        "command": "code",
        "name": "VS Code",
        "kind": "gui",
        "installed": True,
    }
    assert by_cmd["nvim"]["kind"] == "terminal"


# --------------------------------------------------------------------------- #
# _agent_activity
# --------------------------------------------------------------------------- #


class _FakeInst:
    def __init__(self, *, started=True, status=0, program="claude"):
        self._started = started
        self.Status = status
        self.Program = program

    def Started(self):
        return self._started


def _cap(text: str):
    return types.SimpleNamespace(returncode=0, stdout=text.encode("utf-8"))


def _fake_tmux(pane, *, fg="node", created=100.0, pid="", ps=None, calls=None):
    """A subprocess.run stand-in dispatching on the tmux subcommand.

    ``pane`` may be a str or a callable returning the current pane text (so a
    test can mutate it between polls). ``pid`` fills #{pane_pid} (empty = the
    field is missing, as older mocks produced). ``ps`` is the canned
    ``ps -e -o pid=,ppid=,comm=`` output for the process-tree layer (F1); ps
    running with ``ps=None`` fails the test. ``calls`` (a list) records every
    invoked argv for assertions."""

    def fake_run(cmd, **kwargs):
        if calls is not None:
            calls.append(list(cmd))
        if cmd[:2] == ["tmux", "has-session"]:
            return types.SimpleNamespace(returncode=0, stdout=b"")
        if cmd[:2] == ["tmux", "display-message"]:
            return _cap("%s\t%s\t%s" % (fg, created, pid))
        if cmd[:2] == ["tmux", "capture-pane"]:
            return _cap(pane() if callable(pane) else pane)
        if cmd[:2] == ["tmux", "send-keys"]:
            return types.SimpleNamespace(returncode=0, stdout=b"")
        if cmd[0] == "ps":
            if ps is None:
                pytest.fail("unexpected ps call: %r" % (cmd,))
            return _cap(ps)
        pytest.fail("unexpected subprocess: %r" % (cmd,))

    return fake_run


def _tall(body: str) -> str:
    """A pane tall enough that the noise-stripping (bottom 2 lines dropped)
    still leaves distinguishing content in the hash."""
    return body + "\nfiller-1\nfiller-2\nbottom-line-a\nbottom-line-b\n"


@pytest.fixture(autouse=True)
def _activity_isolation(monkeypatch, tmp_path):
    """Hermetic activity state: fresh caches, no real exit/activity markers."""
    server._ACTIVITY_CACHE.clear()
    server._PID_TREE_CACHE.clear()
    server._TRUST_DISMISS_AT.clear()
    monkeypatch.setenv("MINDFLOCK_ACTIVITY_MARKER_DIR", str(tmp_path / "markers"))
    monkeypatch.setattr(server, "_read_exit_marker", lambda name: None)
    yield
    server._ACTIVITY_CACHE.clear()
    server._PID_TREE_CACHE.clear()
    server._TRUST_DISMISS_AT.clear()


def test_agent_activity_offline_when_not_started(monkeypatch):
    monkeypatch.setattr(
        server.subprocess,
        "run",
        lambda *a, **k: pytest.fail("subprocess used for un-started inst"),
    )
    inst = _FakeInst(started=False)
    assert server._agent_activity(inst, "t") == "offline"


def test_agent_activity_offline_when_paused(monkeypatch):
    monkeypatch.setattr(
        server.subprocess,
        "run",
        lambda *a, **k: pytest.fail("subprocess used for paused inst"),
    )
    inst = _FakeInst(status=session.Paused)
    assert server._agent_activity(inst, "t") == "offline"


def test_agent_activity_offline_when_no_tmux_session(monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["tmux", "has-session"]:
            return types.SimpleNamespace(returncode=1, stdout=b"")
        pytest.fail("capture-pane should not run when has-session fails")

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    inst = _FakeInst()
    assert server._agent_activity(inst, "mysess") == "offline"


def test_agent_activity_offline_when_capture_fails(monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["tmux", "has-session"]:
            return types.SimpleNamespace(returncode=0, stdout=b"")
        return types.SimpleNamespace(returncode=1, stdout=b"")

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    inst = _FakeInst()
    assert server._agent_activity(inst, "mysess") == "offline"


def test_agent_activity_clarify_on_waiting_prompt_needs_stable_pane(monkeypatch):
    # A numbered selection cursor "❯ 1." is claude's waiting-prompt signal, but
    # it is only trusted once the pane has been STABLE for a poll — the first
    # sighting (pane just changed) must not read as clarify (A3).
    pane = _tall("Some output\n❯ 1. Yes\n  2. No")
    monkeypatch.setattr(server.subprocess, "run", _fake_tmux(pane))
    inst = _FakeInst(program="claude")
    assert server._agent_activity(inst, "mysess") == "working"
    assert server._agent_activity(inst, "mysess") == "clarify"


def test_agent_activity_clarify_on_phrase_pattern(monkeypatch):
    pane = _tall("Do you want to proceed?\nNo, and tell Claude what to do differently")
    monkeypatch.setattr(server.subprocess, "run", _fake_tmux(pane))
    inst = _FakeInst(program="claude")
    assert server._agent_activity(inst, "mysess") == "working"  # first sight
    assert server._agent_activity(inst, "mysess") == "clarify"  # stable pane


def test_agent_activity_limit_on_usage_limit_banner(monkeypatch):
    # A usage-limit SCREEN is reported as its own 'limit' state, outranking the
    # '❯ 1.' selection menu a limit screen also shows. Like clarify, it needs a
    # STABLE pane first (so a genuinely working turn / scrolling output is never
    # misread), so the first sighting is 'working' and the stable poll is 'limit'.
    pane = _tall("Claude usage limit reached · resets 3am\n❯ 1. Wait\n  2. Switch")
    monkeypatch.setattr(server.subprocess, "run", _fake_tmux(pane))
    inst = _FakeInst(program="claude")
    assert server._agent_activity(inst, "mysess") == "working"  # first sight
    assert server._agent_activity(inst, "mysess") == "limit"  # stable pane


def test_agent_activity_not_limit_on_stray_rate_limit_text(monkeypatch):
    # The loose 'too many requests' / 'rate limited' phrases appear constantly in
    # normal work (HTTP 429s, rate-limiting code, the agent's own prose). They
    # must NOT flip a session to 'limit' — only the CLI's own limit SCREEN does.
    pane = _tall(
        "Traceback:\n  HTTPError: 429 Too Many Requests\n# handle rate limited"
    )
    monkeypatch.setattr(server.subprocess, "run", _fake_tmux(pane))
    inst = _FakeInst(program="claude")
    # Never 'limit' across the first sight or subsequent stable polls.
    for _ in range(3):
        assert server._agent_activity(inst, "mysess") != "limit"


def test_agent_activity_no_clarify_while_pane_scrolls(monkeypatch):
    # A numbered list scrolling by mid-generation contains "❯ 1." but the pane
    # keeps changing -> never clarify, stays working.
    frames = iter([_tall("❯ 1. option\nframe-%d" % i) for i in range(5)])
    monkeypatch.setattr(server.subprocess, "run", _fake_tmux(lambda: next(frames)))
    inst = _FakeInst(program="claude")
    for _ in range(4):
        assert server._agent_activity(inst, "scrolls") == "working"


def test_agent_activity_working_on_first_capture(monkeypatch):
    # No waiting prompt + not seen before -> "working" (pane just observed).
    monkeypatch.setattr(
        server.subprocess, "run", _fake_tmux(_tall("streaming tokens..."))
    )
    inst = _FakeInst(program="claude")
    assert server._agent_activity(inst, "sess-A") == "working"
    # Cache recorded the observation.
    assert "sess-A" in server._ACTIVITY_CACHE


def test_agent_activity_working_when_pane_changes(monkeypatch):
    inst = _FakeInst(program="claude")
    state = {"pane": _tall("frame-1")}
    monkeypatch.setattr(server.subprocess, "run", _fake_tmux(lambda: state["pane"]))
    assert server._agent_activity(inst, "sess-B") == "working"
    # A different pane hash -> still "working" (working is sticky on change).
    state["pane"] = _tall("frame-2")
    assert server._agent_activity(inst, "sess-B") == "working"


def test_agent_activity_idle_when_static_beyond_threshold(monkeypatch):
    inst = _FakeInst(program="claude")
    monkeypatch.setattr(
        server.subprocess, "run", _fake_tmux(_tall("static idle screen"))
    )

    clock = {"t": 1000.0}
    monkeypatch.setattr(server.time, "time", lambda: clock["t"])

    # First observation: working, timer starts at t=1000.
    assert server._agent_activity(inst, "sess-C") == "working"
    # Same pane, but less than idle-after later -> still working.
    clock["t"] = 1000.0 + server._ACTIVITY_IDLE_AFTER - 0.5
    assert server._agent_activity(inst, "sess-C") == "working"
    # Same pane, now beyond the idle threshold -> idle.
    clock["t"] = 1000.0 + server._ACTIVITY_IDLE_AFTER + 0.1
    assert server._agent_activity(inst, "sess-C") == "idle"


def test_agent_activity_single_changed_frame_keeps_idle(monkeypatch):
    # Hysteresis (A1): one changed frame is noise; only two consecutive changed
    # polls flip idle -> working.
    inst = _FakeInst(program="claude")
    state = {"pane": _tall("screen-a")}
    monkeypatch.setattr(server.subprocess, "run", _fake_tmux(lambda: state["pane"]))
    clock = {"t": 1000.0}
    monkeypatch.setattr(server.time, "time", lambda: clock["t"])

    server._agent_activity(inst, "sess-D")  # seed (working)
    clock["t"] += server._ACTIVITY_IDLE_AFTER + 1
    assert server._agent_activity(inst, "sess-D") == "idle"  # settled idle
    state["pane"] = _tall("screen-b")
    clock["t"] += 4
    assert server._agent_activity(inst, "sess-D") == "idle"  # 1 change: still idle
    state["pane"] = _tall("screen-c")
    clock["t"] += 4
    assert server._agent_activity(inst, "sess-D") == "working"  # 2nd change: working


def test_agent_activity_bottom_line_churn_is_ignored(monkeypatch):
    # Spinner / cursor / token-counter churn in the bottom two lines must not
    # count as pane change — the session still settles to (and stays) idle.
    inst = _FakeInst(program="claude")
    n = {"i": 0}

    def pane():
        n["i"] += 1
        return "same body\nsame more\nchurn-%d\nchurn-%d" % (n["i"], n["i"] + 1)

    monkeypatch.setattr(server.subprocess, "run", _fake_tmux(pane))
    clock = {"t": 1000.0}
    monkeypatch.setattr(server.time, "time", lambda: clock["t"])
    assert server._agent_activity(inst, "sess-E") == "working"  # seed
    clock["t"] += server._ACTIVITY_IDLE_AFTER + 1
    assert server._agent_activity(inst, "sess-E") == "idle"
    clock["t"] += 4
    assert server._agent_activity(inst, "sess-E") == "idle"


def test_agent_activity_thinking_interrupt_hint_stays_working(monkeypatch):
    # The core thinking fix: a STATIC body (unchanged hash) that still shows the
    # "esc to interrupt" status line is a live turn — extended thinking runs
    # server-side at ~0 local CPU, so CPU/hash alone would wrongly settle to idle
    # past the threshold. The status-line proof keeps it working.
    inst = _FakeInst(program="claude")

    def pane():
        # Upper (hashed) lines never change; the interrupt hint lives on the
        # bottom lines the hash strips as noise but the raw scan still sees.
        return (
            "Analyzing the request\nstill working it out\n"
            "⠋ Thinking… (esc to interrupt)\ncursor-here"
        )

    monkeypatch.setattr(server.subprocess, "run", _fake_tmux(pane))
    clock = {"t": 1000.0}
    monkeypatch.setattr(server.time, "time", lambda: clock["t"])
    assert server._agent_activity(inst, "sess-think") == "working"  # seed
    # Well past the idle window with a byte-identical body: without the status
    # line this is exactly the churn test (-> idle); here it must hold working.
    clock["t"] += server._ACTIVITY_IDLE_AFTER + 5
    assert server._agent_activity(inst, "sess-think") == "working"
    clock["t"] += server._ACTIVITY_IDLE_AFTER + 5
    assert server._agent_activity(inst, "sess-think") == "working"


def test_agent_activity_climbing_token_counter_is_working(monkeypatch):
    # A static body whose only change is a climbing turn-token counter (no
    # interrupt hint) reads as working; a flat counter settles to idle.
    inst = _FakeInst(program="claude")
    state = {"tok": "5.0k"}

    def pane():
        return "static body one\nstatic body two\n" "%s tokens\nfooter" % state["tok"]

    monkeypatch.setattr(server.subprocess, "run", _fake_tmux(pane))
    clock = {"t": 1000.0}
    monkeypatch.setattr(server.time, "time", lambda: clock["t"])
    assert server._agent_activity(inst, "sess-tok") == "working"  # seed (5.0k)
    # Counter flat past the idle window -> idle (climb is the signal, not mere
    # presence of a number).
    clock["t"] += server._ACTIVITY_IDLE_AFTER + 2
    assert server._agent_activity(inst, "sess-tok") == "idle"
    # Counter climbs -> working, even though the hashed body is unchanged.
    state["tok"] = "9.0k"
    clock["t"] += 4
    assert server._agent_activity(inst, "sess-tok") == "working"


def test_agent_activity_waiting_prompt_beats_stale_interrupt_hint(monkeypatch):
    # A real permission box on a stable pane is 'clarify' even if a leftover
    # interrupt hint is still on screen — clarify is checked before the
    # status-line working proof (mirrors the CPU branch ordering).
    inst = _FakeInst(program="claude")

    def pane():
        return (
            "some output above\nmore output\n"
            "No, and tell Claude what to do differently\n"
            "(esc to interrupt)"
        )

    monkeypatch.setattr(server.subprocess, "run", _fake_tmux(pane))
    clock = {"t": 1000.0}
    monkeypatch.setattr(server.time, "time", lambda: clock["t"])
    assert server._agent_activity(inst, "sess-clar") == "working"  # seed
    # Stable pane on the next poll -> the waiting prompt wins over the hint.
    assert server._agent_activity(inst, "sess-clar") == "clarify"


def test_agent_activity_exit_marker_wins(monkeypatch):
    # Layer 0: the exit-recording wrapper says the agent command ended -> idle
    # regardless of what the pane looks like.
    monkeypatch.setattr(server.subprocess, "run", _fake_tmux(_tall("anything")))
    monkeypatch.setattr(server, "_agent_exited", lambda name, created: True)
    inst = _FakeInst(program="claude")
    assert server._agent_activity(inst, "sess-F") == "idle"


def test_agent_activity_bare_shell_foreground_is_idle(monkeypatch):
    # Layer 1: a bare shell holding the pane means the agent isn't running.
    monkeypatch.setattr(
        server.subprocess, "run", _fake_tmux(_tall("scrollback text"), fg="bash")
    )
    inst = _FakeInst(program="claude")
    assert server._agent_activity(inst, "sess-G") == "idle"


def test_agent_activity_provider_marker_is_authoritative(monkeypatch):
    # Layer 2 (A2): a fresh hook-written marker outvotes pane hashing entirely.
    from backend.providers.claude import ClaudeProvider

    monkeypatch.setattr(server.subprocess, "run", _fake_tmux(_tall("static pane")))
    monkeypatch.setattr(ClaudeProvider, "activity_state", lambda self, name: "working")
    inst = _FakeInst(program="claude")
    clock = {"t": 1000.0}
    monkeypatch.setattr(server.time, "time", lambda: clock["t"])
    assert server._agent_activity(inst, "sess-H") == "working"
    clock["t"] += server._ACTIVITY_IDLE_AFTER + 5
    # Pane is long-static, but the CLI itself says it is working.
    assert server._agent_activity(inst, "sess-H") == "working"


def test_agent_activity_offline_on_unexpected_exception(monkeypatch):
    # Started() raising -> caught by the broad except -> "offline".
    class Bad:
        Status = 0
        Program = "claude"

        def Started(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(
        server.subprocess,
        "run",
        lambda *a, **k: pytest.fail("should not reach subprocess"),
    )
    assert server._agent_activity(Bad(), "t") == "offline"


# --------------------------------------------------------------------------- #
# _agent_activity — process-tree layer (F1): the launcher script holds the
# pane as bash with claude as a CHILD, so a bare-shell foreground alone
# must not read as idle.
# --------------------------------------------------------------------------- #

# ps -e snapshot: pane shell 4242 (bash) -> claude 4300 (via a sub-shell 4250).
_PS_WITH_CLAUDE = (
    "  100     1 systemd\n 4242  4000 bash\n 4250  4242 sh\n 4300  4250 claude\n"
)
# pane shell 4242 with only shell descendants -> the agent really exited.
_PS_SHELLS_ONLY = "  100     1 systemd\n 4242  4000 bash\n 4250  4242 sh\n"


def test_agent_activity_wrapper_shell_with_claude_child_not_idle(monkeypatch):
    # fg is bash (the launcher wrapper) but claude is alive underneath -> falls
    # through to pane inspection (first sighting => working), NOT idle.
    monkeypatch.setattr(
        server.subprocess,
        "run",
        _fake_tmux(
            _tall("esc to interrupt"), fg="bash", pid="4242", ps=_PS_WITH_CLAUDE
        ),
    )
    inst = _FakeInst(program="claude")
    assert server._agent_activity(inst, "sess-wrap") == "working"


def test_agent_activity_wrapper_shell_clarify_flows_through(monkeypatch):
    # F1 regression: the trust/permission prompt under a wrapper shell used to be
    # invisible (bare shell => idle). With the claude child alive, a stable
    # pane showing the waiting-prompt cursor now reads clarify.
    pane = _tall("Do you want to proceed?\n❯ 1. Yes\n  2. No")
    monkeypatch.setattr(
        server.subprocess,
        "run",
        _fake_tmux(pane, fg="bash", pid="4242", ps=_PS_WITH_CLAUDE),
    )
    inst = _FakeInst(program="claude")
    assert server._agent_activity(inst, "sess-wrap2") == "working"  # first sight
    server._PID_TREE_CACHE.clear()
    assert server._agent_activity(inst, "sess-wrap2") == "clarify"  # stable pane


def test_agent_activity_bare_shell_no_agent_descendant_is_idle(monkeypatch):
    monkeypatch.setattr(
        server.subprocess,
        "run",
        _fake_tmux(_tall("scrollback"), fg="bash", pid="4242", ps=_PS_SHELLS_ONLY),
    )
    inst = _FakeInst(program="claude")
    assert server._agent_activity(inst, "sess-exited") == "idle"


def test_agent_activity_bare_shell_unknown_pid_is_idle(monkeypatch):
    # No #{pane_pid} (or ps unavailable): conservative pre-F1 behaviour — a
    # bare shell reads idle without ever invoking ps (ps=None would fail).
    monkeypatch.setattr(
        server.subprocess, "run", _fake_tmux(_tall("text"), fg="bash", pid="")
    )
    inst = _FakeInst(program="claude")
    assert server._agent_activity(inst, "sess-nopid") == "idle"


def test_agent_activity_marker_outvotes_bare_shell(monkeypatch):
    # Layer order (F1): a fresh provider hook marker sits ABOVE the shell
    # heuristic — the CLI's own "working" wins even when the pane fg is bash
    # (no ps / capture-pane needed at all).
    from backend.providers.claude import ClaudeProvider

    monkeypatch.setattr(ClaudeProvider, "activity_state", lambda self, name: "working")

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["tmux", "has-session"]:
            return types.SimpleNamespace(returncode=0, stdout=b"")
        if cmd[:2] == ["tmux", "display-message"]:
            return _cap("bash\t100.0\t4242")
        pytest.fail("marker should short-circuit before %r" % (cmd,))

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    inst = _FakeInst(program="claude")
    assert server._agent_activity(inst, "sess-marker") == "working"


def test_pane_has_agent_process_caches_ps_snapshot(monkeypatch):
    # One ps call per TTL window, however many lookups happen (poll + tick).
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return _cap(_PS_WITH_CLAUDE)

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    assert server._pane_has_agent_process("4242") is True
    assert server._pane_has_agent_process("4242") is True
    assert len(calls) == 1


def test_pane_has_agent_process_ps_failure_is_false(monkeypatch):
    def boom(*a, **k):
        raise OSError("no ps")

    monkeypatch.setattr(server.subprocess, "run", boom)
    assert server._pane_has_agent_process("4242") is False


# --------------------------------------------------------------------------- #
# Trust-gate auto-answer (F2): a visible trust prompt is dismissed with the
# provider's keystroke and reported as clarify.
# --------------------------------------------------------------------------- #


def test_agent_activity_trust_prompt_auto_answered_as_clarify(monkeypatch):
    pane = _tall("Do you trust the files in this folder?\n❯ 1. Yes, proceed")
    calls = []
    monkeypatch.setattr(
        server.subprocess,
        "run",
        _fake_tmux(pane, fg="bash", pid="4242", ps=_PS_WITH_CLAUDE, calls=calls),
    )
    inst = _FakeInst(program="claude")
    assert server._agent_activity(inst, "sess-trust") == "clarify"
    sent = [c for c in calls if c[:2] == ["tmux", "send-keys"]]
    assert len(sent) == 1
    assert sent[0][-1] == "\r"  # the Claude TrustSpec keystroke, sent literally


def test_agent_activity_trust_dismiss_rate_limited(monkeypatch):
    # The prompt still on screen next poll (slow redraw) must not be spammed
    # with a second keystroke inside the cooldown.
    pane = _tall("Do you trust the files in this folder?\n❯ 1. Yes, proceed")
    calls = []
    monkeypatch.setattr(
        server.subprocess,
        "run",
        _fake_tmux(pane, fg="bash", pid="4242", ps=_PS_WITH_CLAUDE, calls=calls),
    )
    inst = _FakeInst(program="claude")
    assert server._agent_activity(inst, "sess-trust2") == "clarify"
    assert server._agent_activity(inst, "sess-trust2") == "clarify"
    sent = [c for c in calls if c[:2] == ["tmux", "send-keys"]]
    assert len(sent) == 1


# --------------------------------------------------------------------------- #
# Startup banner in local mode (F7)
# --------------------------------------------------------------------------- #


def test_mobile_banner_local_mode_suppresses_tailnet_block(monkeypatch):
    monkeypatch.setenv("CS_WEB_MODE", "local")
    monkeypatch.setattr(
        server, "_tailscale_info", lambda: pytest.fail("tailscale probed in local mode")
    )
    banner = server._mobile_banner()
    assert "127.0.0.1" in banner
    assert "Tailscale:" not in banner  # no unusable tailnet URLs
    assert "Scan from a phone" not in banner  # no QR block
    assert "tailscale" in banner  # ...but points at the fix


def test_mobile_banner_tailscale_mode_unchanged(monkeypatch):
    monkeypatch.delenv("CS_WEB_MODE", raising=False)
    monkeypatch.setattr(
        server, "_tailscale_info", lambda: ("myhost.tail.net", "100.1.2.3")
    )
    monkeypatch.setattr(server, "_tailscale_serves_port", lambda port: False)
    banner = server._mobile_banner()
    assert "Tailscale:  http://myhost.tail.net" in banner


def test_mobile_banner_log_copy_redacts_token_and_qr(monkeypatch):
    """The copy written to mindflock.log (served back via GET /api/logs) must
    carry neither the access token nor the QR that encodes it; stdout keeps
    the full banner."""
    monkeypatch.delenv("CS_WEB_MODE", raising=False)
    monkeypatch.setenv("MINDFLOCK_AUTH_TOKEN", "sekret-token-xyz")  # gate on
    monkeypatch.setattr(
        server, "_tailscale_info", lambda: ("myhost.tail.net", "100.1.2.3")
    )
    monkeypatch.setattr(server, "_tailscale_serves_port", lambda port: False)
    full = server._mobile_banner()
    logged = server._mobile_banner(for_log=True)
    assert "sekret-token-xyz" in full
    assert "sekret-token-xyz" not in logged
    assert "redacted" in logged
    assert "Scan from a phone" not in logged  # the QR block-art encodes ?token=…


# --------------------------------------------------------------------------- #
# _under_managed_workspace (auto-adopt exclusion)
# --------------------------------------------------------------------------- #
def test_under_managed_workspace_nested_true():
    assert cursor_windows._under_managed_workspace("/ws/feature-x", ["/ws"]) is True


def test_under_managed_workspace_exact_root_true():
    assert cursor_windows._under_managed_workspace("/ws", ["/ws"]) is True


def test_under_managed_workspace_outside_false():
    assert cursor_windows._under_managed_workspace("/home/u/proj", ["/ws"]) is False


def test_under_managed_workspace_sibling_prefix_false():
    # commonpath (not a naive string prefix) keeps "/ws2" out of "/ws".
    assert cursor_windows._under_managed_workspace("/ws2/x", ["/ws"]) is False


def test_under_managed_workspace_mixed_abs_rel_skips_root():
    # commonpath raises ValueError mixing absolute/relative -> that root is
    # skipped rather than fatal, and nothing else matches.
    assert cursor_windows._under_managed_workspace("/abs/x", ["rel/root"]) is False


def test_under_managed_workspace_second_root_matches():
    assert cursor_windows._under_managed_workspace("/b/x", ["/a", "/b"]) is True


# --------------------------------------------------------------------------- #
# _cursor_storage_path
# --------------------------------------------------------------------------- #
def test_cursor_storage_path_none_without_dirname(monkeypatch):
    monkeypatch.setattr(cursor_windows.ide_cfg, "ide_storage_dirname", lambda: "")
    assert server._cursor_storage_path() is None


def test_cursor_storage_path_returns_first_existing(monkeypatch):
    monkeypatch.setattr(cursor_windows.ide_cfg, "ide_storage_dirname", lambda: "Cursor")
    monkeypatch.setattr(cursor_windows.glob, "glob", lambda pat: [])  # no WSL mounts
    target = os.path.expanduser("~/.config/Cursor/User/globalStorage/storage.json")
    monkeypatch.setattr(server.os.path, "isfile", lambda p: p == target)
    assert server._cursor_storage_path() == target


def test_cursor_storage_path_none_when_no_candidate_exists(monkeypatch):
    monkeypatch.setattr(cursor_windows.ide_cfg, "ide_storage_dirname", lambda: "Cursor")
    monkeypatch.setattr(cursor_windows.glob, "glob", lambda pat: [])
    monkeypatch.setattr(server.os.path, "isfile", lambda p: False)
    assert server._cursor_storage_path() is None


# --------------------------------------------------------------------------- #
# _cursor_open_folders (storage.json -> local paths, deduped)
# --------------------------------------------------------------------------- #
def test_cursor_open_folders_reads_dedupes_and_orders(monkeypatch, tmp_path):
    storage = tmp_path / "storage.json"
    storage.write_text(
        json.dumps(
            {
                "windowsState": {
                    "lastActiveWindow": {"folder": "file:///home/u/a"},
                    "openedWindows": [
                        {"folder": "file:///home/u/b"},
                        {"folder": "file:///home/u/a"},  # dup of lastActive
                        {"noFolder": True},  # no folder -> skipped
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cursor_windows, "_cursor_storage_path", lambda: str(storage))
    # lastActive first, then opened; the duplicate is dropped, order preserved.
    assert server._cursor_open_folders() == ["/home/u/a", "/home/u/b"]


def test_cursor_open_folders_no_storage_is_empty(monkeypatch):
    monkeypatch.setattr(cursor_windows, "_cursor_storage_path", lambda: None)
    assert server._cursor_open_folders() == []


def test_cursor_open_folders_bad_json_is_empty(monkeypatch, tmp_path):
    storage = tmp_path / "storage.json"
    storage.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(cursor_windows, "_cursor_storage_path", lambda: str(storage))
    assert server._cursor_open_folders() == []


def test_cursor_open_folders_skips_non_wsl_remotes(monkeypatch, tmp_path):
    storage = tmp_path / "storage.json"
    storage.write_text(
        json.dumps(
            {
                "windowsState": {
                    "openedWindows": [
                        {"folder": "vscode-remote://ssh-remote+box/home/u/x"},
                        {"folder": "file:///home/u/local"},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cursor_windows, "_cursor_storage_path", lambda: str(storage))
    # The SSH remote maps to "" (not this machine) and is dropped.
    assert server._cursor_open_folders() == ["/home/u/local"]


# --------------------------------------------------------------------------- #
# _close_cursor_window
# --------------------------------------------------------------------------- #
def test_close_cursor_window_closes_each_found_window(monkeypatch):
    monkeypatch.setattr(server, "_find_cursor_windows", lambda p: ["100", "200"])
    calls = []
    monkeypatch.setattr(server.subprocess, "run", lambda cmd, **k: calls.append(cmd))
    server._close_cursor_window("/ws/proj")
    assert calls == [
        ["xdotool", "windowclose", "100"],
        ["xdotool", "windowclose", "200"],
    ]


def test_close_cursor_window_swallows_subprocess_error(monkeypatch):
    monkeypatch.setattr(server, "_find_cursor_windows", lambda p: ["100"])

    def boom(*a, **k):
        raise OSError("xdotool gone")

    monkeypatch.setattr(server.subprocess, "run", boom)
    server._close_cursor_window("/ws/proj")  # no raise


# --------------------------------------------------------------------------- #
# _maximize_x11
# --------------------------------------------------------------------------- #
def test_maximize_x11_prefers_wmctrl(monkeypatch):
    monkeypatch.setattr(
        server.shutil,
        "which",
        lambda name: "/usr/bin/wmctrl" if name == "wmctrl" else None,
    )
    calls = []
    monkeypatch.setattr(server.subprocess, "run", lambda cmd, **k: calls.append(cmd))
    server._maximize_x11("123")
    assert calls[0][0] == "wmctrl"
    assert "123" in calls[0]
    assert "add,maximized_vert,maximized_horz" in calls[0]


def test_maximize_x11_falls_back_to_xdotool(monkeypatch):
    monkeypatch.setattr(server.shutil, "which", lambda name: None)  # no wmctrl
    calls = []
    monkeypatch.setattr(server.subprocess, "run", lambda cmd, **k: calls.append(cmd))
    server._maximize_x11("123")
    assert calls[0][0] == "xdotool"
    assert "super+Up" in calls[0]


def test_maximize_x11_swallows_errors(monkeypatch):
    monkeypatch.setattr(server.shutil, "which", lambda name: None)

    def boom(*a, **k):
        raise OSError("no X server")

    monkeypatch.setattr(server.subprocess, "run", boom)
    server._maximize_x11("123")  # no raise


# --------------------------------------------------------------------------- #
# _maximize_new_cursor_window
# --------------------------------------------------------------------------- #
def test_maximize_new_window_empty_terms_noop(monkeypatch):
    monkeypatch.setattr(server, "_cursor_title_terms", lambda p: [])
    monkeypatch.setattr(
        server, "_powershell", lambda: pytest.fail("should not probe powershell")
    )
    server._maximize_new_cursor_window("")


def test_maximize_new_window_powershell_launches_background(monkeypatch):
    monkeypatch.setattr(server, "_cursor_title_terms", lambda p: ["proj"])
    monkeypatch.setattr(server, "_powershell", lambda: "/mnt/c/powershell.exe")
    popen = []
    monkeypatch.setattr(server.subprocess, "Popen", lambda cmd, **k: popen.append(cmd))
    server._maximize_new_cursor_window("/ws/proj")
    assert popen and popen[0][0] == "/mnt/c/powershell.exe"
    assert "-EncodedCommand" in popen[0]


def test_maximize_new_window_linux_no_xdotool_noop(monkeypatch):
    monkeypatch.setattr(server, "_cursor_title_terms", lambda p: ["proj"])
    monkeypatch.setattr(server, "_powershell", lambda: None)
    monkeypatch.setattr(server.shutil, "which", lambda name: None)  # no xdotool
    monkeypatch.setattr(
        server, "_find_cursor_windows", lambda p: pytest.fail("must not poll")
    )
    server._maximize_new_cursor_window("/ws/proj")


def test_maximize_new_window_linux_polls_and_maximizes(monkeypatch):
    monkeypatch.setattr(server, "_cursor_title_terms", lambda p: ["proj"])
    monkeypatch.setattr(server, "_powershell", lambda: None)
    monkeypatch.setattr(server.shutil, "which", lambda name: "/usr/bin/xdotool")
    monkeypatch.setattr(server, "_find_cursor_windows", lambda p: ["55"])
    maxed = []
    # _maximize_x11 is called module-locally, so patch it on cursor_windows.
    monkeypatch.setattr(cursor_windows, "_maximize_x11", lambda wid: maxed.append(wid))
    monkeypatch.setattr(server.time, "sleep", lambda s: None)  # no real waiting
    # Collapse both timing loops: find succeeds immediately, apply runs once.
    clock = iter([0, 0, 0, 0, 100, 100])
    monkeypatch.setattr(server.time, "time", lambda: next(clock))
    server._maximize_new_cursor_window("/ws/proj")
    assert "55" in maxed
