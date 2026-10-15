"""Headless-Chrome smoke test for the mobile view (``/m``).

This scripts the manual CDP verification that caught the mobile scroll/compose/
keyboard regressions by hand — a real Chromium loads ``/m`` under phone
emulation (touch on), and we assert the structural invariants that broke before:
the compose box, key bar, and terminal all render; the compose box sits ABOVE
the key bar (so neither is buried by the keyboard); a synthetic one-finger
scroll gesture on the terminal runs without throwing; and the page loads with no
uncaught JS exceptions.

Runs a REAL uvicorn server in a subprocess with a fully isolated ``$HOME`` (and
tmp settings / queue files) so it never reads or mutates the developer's live
``~/.mindflock`` state. ``CS_WEB_MODE=local`` keeps the auth gate off.

Self-skipping: skips cleanly when Chrome isn't installed or ``websockets`` is
missing, so the default suite stays green on machines/CI without a browser.
Force-skip with ``MINDFLOCK_SKIP_SMOKE=1``.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest


def _chrome_binary():
    for name in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
    ):
        p = shutil.which(name)
        if p:
            return p
    return None


pytestmark = pytest.mark.skipif(
    os.environ.get("MINDFLOCK_SKIP_SMOKE") == "1" or _chrome_binary() is None,
    reason="mobile smoke test needs Chrome (set MINDFLOCK_SKIP_SMOKE=1 to force-skip)",
)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_http(url: str, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            time.sleep(0.25)
    return False


def _repo_root() -> Path:
    # tests/integration/<this file> -> repo root is three parents up.
    return Path(__file__).resolve().parents[2]


@pytest.fixture
def server(tmp_path):
    """A real uvicorn server on a free port, isolated $HOME, auth off."""
    try:
        import websockets  # noqa: F401
    except Exception:  # noqa: BLE001
        pytest.skip("websockets not installed")

    port = _free_port()
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **os.environ,
        "HOME": str(home),
        "CS_WEB_MODE": "local",  # auth off
        "MINDFLOCK_SETTINGS_FILE": str(tmp_path / "settings.json"),
        "MINDFLOCK_PROMPT_QUEUE_FILE": str(tmp_path / "queues.json"),
        "MINDFLOCK_HOOKS_DIR": str(tmp_path / "hooks"),
        "PORT": str(port),
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "backend.web.server:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=str(_repo_root()),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        if not _wait_http("http://127.0.0.1:%d/m" % port):
            proc.terminate()
            pytest.skip("server did not come up in time")
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture
def chrome(tmp_path):
    """A headless Chrome with remote debugging; yields the debug port."""
    dport = _free_port()
    profile = tmp_path / "chrome-profile"
    proc = subprocess.Popen(
        [
            _chrome_binary(),
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--remote-debugging-port=%d" % dport,
            "--user-data-dir=%s" % profile,
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        if not _wait_http("http://127.0.0.1:%d/json/version" % dport):
            proc.terminate()
            pytest.skip("headless Chrome did not come up in time")
        yield dport
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# --------------------------------------------------------------------------- #
# CDP driver (minimal — mirrors the manual cdp_shots.py scripts)
# --------------------------------------------------------------------------- #
async def _drive_mobile(dport: int, url: str) -> dict:
    import websockets

    # Open a fresh tab at the target URL, then attach to its page websocket.
    # Modern Chrome requires PUT for /json/new.
    req = urllib.request.Request(
        "http://127.0.0.1:%d/json/new?%s" % (dport, url), method="PUT"
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        target = json.load(r)
    ws_url = target["webSocketDebuggerUrl"]

    _id = 0
    exceptions = []

    async with websockets.connect(ws_url, max_size=20_000_000) as ws:

        async def send(method, params=None):
            nonlocal _id
            _id += 1
            want = _id
            await ws.send(
                json.dumps({"id": want, "method": method, "params": params or {}})
            )
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("method") == "Runtime.exceptionThrown":
                    exceptions.append(msg)
                if msg.get("id") == want:
                    return msg.get("result", {})

        async def evaljs(expr):
            r = await send(
                "Runtime.evaluate",
                {"expression": expr, "awaitPromise": True, "returnByValue": True},
            )
            return r.get("result", {}).get("value")

        await send("Page.enable")
        await send("Runtime.enable")
        # Phone emulation with touch on — the whole point.
        await send(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": 390,
                "height": 780,
                "deviceScaleFactor": 2,
                "mobile": True,
                "screenWidth": 390,
                "screenHeight": 780,
            },
        )
        await send(
            "Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5}
        )
        # Reload under emulation so load-time errors are captured.
        await send("Page.navigate", {"url": url})
        await asyncio.sleep(3.0)

        checks = await evaljs(
            "(function(){var q=function(id){return !!document.getElementById(id);};"
            "var composer=document.getElementById('composer'),keys=document.getElementById('keys');"
            "var order=false;"
            "if(composer&&keys){order=(composer.compareDocumentPosition(keys)&"
            "Node.DOCUMENT_POSITION_FOLLOWING)!==0;}"
            "return {compose:q('compose'),keys:q('keys'),term:q('term'),send:q('send'),"
            "composerBeforeKeys:order};})()"
        )
        gesture = await evaljs(
            "(function(){try{var t=document.getElementById('term');var r=t.getBoundingClientRect();"
            "function tev(type,y){var to=new Touch({identifier:1,target:t,clientX:r.left+20,clientY:y});"
            "return new TouchEvent(type,{bubbles:true,cancelable:true,"
            "touches:type==='touchend'?[]:[to],changedTouches:[to],"
            "targetTouches:type==='touchend'?[]:[to]});}"
            "t.dispatchEvent(tev('touchstart',r.top+220));"
            "t.dispatchEvent(tev('touchmove',r.top+120));"
            "t.dispatchEvent(tev('touchend',r.top+120));return true;}catch(e){return String(e);}})()"
        )
        return {"checks": checks or {}, "gesture": gesture, "exceptions": exceptions}


def test_mobile_view_smoke(server, chrome):
    url = "http://127.0.0.1:%d/m" % server
    result = asyncio.run(_drive_mobile(chrome, url))
    checks = result["checks"]

    assert checks.get("compose") is True, "compose box missing"
    assert checks.get("keys") is True, "soft-key bar missing"
    assert checks.get("term") is True, "terminal host missing"
    assert checks.get("send") is True, "Send button missing"
    # M6/M7: compose box must sit ABOVE the key bar so neither is buried.
    assert (
        checks.get("composerBeforeKeys") is True
    ), "compose box is not above the key bar"
    # M1: a one-finger scroll gesture on the terminal must not throw.
    assert result["gesture"] is True, "touch scroll gesture errored: %r" % (
        result["gesture"],
    )
    # The page must load with no uncaught JS exceptions.
    assert not result["exceptions"], "uncaught JS exceptions: %r" % (
        result["exceptions"],
    )


# --------------------------------------------------------------------------- #
# Session actions: picker → row ⋯ → actions sheet (the rail's verbs)
# --------------------------------------------------------------------------- #
# The isolated server has no sessions, so the page's GET /api/instances is
# answered from the page itself: one offline session, which is the state that
# shows every row of the sheet (Restart included).
_FAKE_LIST = (
    "(function(){var real=window.fetch;window.__dialogs=0;"
    "window.confirm=window.alert=window.prompt=function(){window.__dialogs++;return false;};"
    "window.fetch=function(u,o){if(String(u)==='/api/instances'&&!(o&&o.method)){"
    "return Promise.resolve(new Response(JSON.stringify([{title:'demo',status:'running',"
    "activity:'offline',repo:'acme',stage:'agent'}]),{status:200,"
    "headers:{'Content-Type':'application/json'}}));}return real.apply(this,arguments);};})();"
)

_ACTIONS_FLOW = r"""
(async function(){
  var sleep=function(ms){return new Promise(function(r){setTimeout(r,ms);});};
  var vis=function(id){var e=document.getElementById(id);return !!e&&!e.classList.contains('hidden');};
  var out={};
  out.header=document.getElementById('picker-title').textContent;
  out.chip=document.getElementById('state-chip').textContent;
  document.getElementById('picker').click();
  await sleep(50);
  out.sessionsOpen=vis('sessions-sheet');
  var rows=document.querySelectorAll('#session-list .sess-row');
  out.rows=rows.length;
  out.rowState=rows.length?rows[0].querySelector('.sess-state').textContent:'';
  rows[0].querySelector('.sess-more').click();
  await sleep(450);
  out.actOpen=vis('act-sheet');
  out.sessionsClosed=!vis('sessions-sheet');
  out.labels=Array.prototype.map.call(
    document.querySelectorAll('#act-list .act-label'),function(e){return e.textContent;});
  var del=Array.prototype.filter.call(document.querySelectorAll('#act-list .act-row'),
    function(b){return b.querySelector('.act-label').textContent==='Delete…';})[0];
  del.click();
  await sleep(50);
  var strip=document.querySelector('#act-list .delete-strip');
  out.strip=strip?strip.firstChild.textContent:'';
  out.focusIsCancel=document.activeElement&&document.activeElement.textContent==='Cancel';
  out.dialogs=window.__dialogs;
  return out;
})()
"""


async def _drive_actions(dport: int, url: str) -> dict:
    import websockets

    req = urllib.request.Request(
        "http://127.0.0.1:%d/json/new?%s" % (dport, "about:blank"), method="PUT"
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        target = json.load(r)
    _id = 0
    exceptions = []
    async with websockets.connect(
        target["webSocketDebuggerUrl"], max_size=20_000_000
    ) as ws:

        async def send(method, params=None):
            nonlocal _id
            _id += 1
            want = _id
            await ws.send(
                json.dumps({"id": want, "method": method, "params": params or {}})
            )
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("method") == "Runtime.exceptionThrown":
                    exceptions.append(msg)
                if msg.get("id") == want:
                    return msg.get("result", {})

        await send("Page.enable")
        await send("Runtime.enable")
        await send(
            "Emulation.setDeviceMetricsOverride",
            {"width": 390, "height": 780, "deviceScaleFactor": 2, "mobile": True},
        )
        await send(
            "Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5}
        )
        await send("Page.addScriptToEvaluateOnNewDocument", {"source": _FAKE_LIST})
        await send("Page.navigate", {"url": url})
        await asyncio.sleep(3.0)
        r = await send(
            "Runtime.evaluate",
            {"expression": _ACTIONS_FLOW, "awaitPromise": True, "returnByValue": True},
        )
        return {
            "flow": r.get("result", {}).get("value") or {},
            "error": r.get("exceptionDetails"),
            "exceptions": exceptions,
        }


def test_mobile_session_actions_sheet(server, chrome):
    url = "http://127.0.0.1:%d/m" % server
    result = asyncio.run(_drive_actions(chrome, url))
    assert not result["error"], result["error"]
    flow = result["flow"]
    # The header reads like the rail: the name and the state word.
    assert flow["header"] == "demo"
    assert flow["chip"] == "Offline"
    # The picker is the Sessions sheet, its row carries the same word.
    assert flow["sessionsOpen"] is True
    assert flow["rows"] == 1
    assert flow["rowState"] == "Offline"
    # The row's ⋯ opens the actions sheet in place of the list.
    assert flow["actOpen"] is True and flow["sessionsClosed"] is True
    # No Pause on the phone (#210): a running session has none; Resume shows
    # only for a session that is already paused.
    assert flow["labels"] == [
        "Restart",
        "Close",
        "Delete…",
        "Recently closed",
    ]
    # Delete asks on its own inline strip, focus on Cancel; no native dialog.
    # Round 5: the Delete strip says what is lost, like the desktop's.
    assert flow["strip"].startswith(
        "Deletes the folder and branch, including uncommitted changes."
    )
    assert flow["focusIsCancel"] is True
    assert flow["dialogs"] == 0
    assert not result["exceptions"], "uncaught JS exceptions: %r" % (
        result["exceptions"],
    )
