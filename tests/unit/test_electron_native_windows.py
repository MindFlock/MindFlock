"""The desktop app on native Windows (docs/windows.md): it drives a native
engine (electron/engine.js decides; its rules are node-tested in
electron/test/engine.test.js), installs it with install.ps1, kills the server
it started on quit, and the NSIS installer no longer requires WSL — while an
existing WSL user keeps the WSL path."""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_MAIN = (_ROOT / "electron" / "main.js").read_text(encoding="utf-8")
_NSH = (_ROOT / "electron" / "build" / "installer.nsh").read_text(encoding="utf-8")
_PKG = json.loads((_ROOT / "electron" / "package.json").read_text(encoding="utf-8"))
_OFFLINE = (_ROOT / "electron" / "offline.html").read_text(encoding="utf-8")


def _fn(src: str, head: str) -> str:
    start = src.index(head)
    nxt = re.search(r"\n(?:async )?function |\nipcMain\.handle\(", src[start + 1 :])
    return src[start : start + 1 + nxt.start()] if nxt else src[start:]


def _macro(name: str) -> str:
    body = _NSH[_NSH.index("!macro " + name) :]
    return body[: body.index("!macroend")]


def test_engine_js_and_install_ps1_ship_with_the_app():
    assert "engine.js" in _PKG["build"]["files"]
    tos = [r["to"] for r in _PKG["build"]["extraResources"]]
    assert "install.sh" in tos and "install.ps1" in tos


def test_the_engine_is_decided_once_by_engine_js():
    assert "const eng = require('./engine')" in _MAIN
    assert "eng.decideEngine({" in _MAIN
    assert "const WIN_NATIVE = ENGINE.engine === 'native'" in _MAIN
    assert "const WIN_WSL = ENGINE.engine === 'wsl'" in _MAIN
    # The WSL traces are what keeps an existing WSL user on WSL.
    assert "eng.WSL_TRACES.some(" in _MAIN


def test_native_server_starts_hidden_and_is_watched():
    start = _fn(_MAIN, "function startServerIfNeeded")
    assert "if (WIN_NATIVE) return startNativeServer()" in start
    native = _fn(_MAIN, "function startNativeServer")
    assert "windowsHide: true" in native
    assert "detached" not in native  # a hidden console its children inherit
    assert "eng.nativeServerEnv(" in native and "eng.nativeServeCommand(" in native
    assert "ownedServer = child" in native and "mine.exited =" in native


def test_quitting_kills_only_the_server_this_app_started():
    quit_ = _MAIN[_MAIN.index("app.on('will-quit'") :]
    quit_ = quit_[: quit_.index("})") + 2]
    assert "!WIN_NATIVE || !ownedServer" in quit_
    assert "eng.taskkillArgs(ownedServer.pid)" in quit_


def test_stopping_the_engine_has_a_native_twin_of_kill_server():
    shell = _fn(_MAIN, "function spawnEngineShell")
    assert shell.index("if (WIN_NATIVE)") < shell.index("if (WIN_WSL)")
    assert "if (scriptText === KILL_SERVER) killNativeServer()" in shell
    kill = _fn(_MAIN, "function killNativeServer")
    assert "eng.killServerScript()" in kill and "eng.taskkillArgs(" in kill


def test_first_launch_install_runs_install_ps1_natively():
    assert "WIN_NATIVE ? 'install.ps1' : 'install.sh'" in _MAIN
    runner = _fn(_MAIN, "function runUnixInstaller")
    assert "spawn('powershell.exe', eng.powershellFileArgs(INSTALL_SCRIPT)" in runner
    assert "if (!WIN_WSL) {" in _fn(_MAIN, "function startInstall")
    assert "command: installCommandLine()," in _MAIN


def test_wsl_only_paths_stay_behind_win_wsl():
    assert "if (WIN_NATIVE) {" in _fn(_MAIN, "function refreshDiag")
    assert "if (!WIN_WSL) return { ok: false }" in _MAIN  # Restart WSL
    assert "canRetry: !WIN_WSL," in _MAIN
    token = _fn(_MAIN, "function readEngineToken")
    assert "eng.nativeTokenCommand(" in token and "} else if (WIN_WSL) {" in token


def test_offline_page_knows_the_native_engine():
    assert "diag.engine === 'native'" in _OFFLINE
    assert 'class="ni-tools"' in _OFFLINE


def test_the_installer_no_longer_requires_wsl():
    init = _macro("customInit")
    assert "wsl" not in init.lower() and "Quit" not in init
    assert "needs WSL" not in _NSH


def test_uninstall_tears_down_a_native_engine_too():
    body = _macro("customUnInstall")
    assert body.strip().splitlines()[1].strip() == "${ifNot} ${isUpdated}"
    native = body.index('IfFileExists "$PROFILE\\.local\\bin\\mindflock.exe"')
    assert native < body.index("wsl.exe")  # both engines, native first
    assert "taskkill /F /T /IM mindflock.exe" in body
    assert '"$PROFILE\\.local\\bin\\mindflock.exe" uninstall --yes' in body
    assert "tool uninstall mindflock" in body
    # NSIS can't jump relatively over plugin calls reliably: labels only.
    assert not re.search(r"^\s*(IfFileExists|StrCmp|Goto)\b.*\s[+-]\d", body, re.M)


def test_the_session_host_kill_blob_is_what_the_comment_says():
    blob = re.search(r"-EncodedCommand ([A-Za-z0-9+/=]+)'", _NSH).group(1)
    script = base64.b64decode(blob).decode("utf-16-le")
    assert "uv\\tools\\mindflock" in script and "ExecutablePath -like" in script
    assert "taskkill /T /F /PID $_.ProcessId" in script
    assert len(blob) < 1000  # NSIS's default string limit is 1024
