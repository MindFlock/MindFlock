"""The desktop app's engine-update check works with the access-token gate on.

``electron/main.js`` asks the local server which engine version is running
before offering **Update engine**. It used to ask ``/api/doctor``, which the
gate 401s for a tokenless request — so with Tailscale mode on, every check
learned nothing and Settings reported "The engine is up to date" on a stale
engine. It now asks the public ``/api/remote/hello`` first (``/api/doctor`` is
the fallback for engines that predate it), and tells Settings whether the
check succeeded so "couldn't tell" no longer reads as "up to date".
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from backend import __version__
from backend.web import server

_REPO = Path(__file__).resolve().parents[2]
_MAIN_JS = _REPO / "electron" / "main.js"
_ADVANCED = (
    _REPO / "frontend" / "src" / "components" / "settings" / "screens" / "Advanced.tsx"
)


def _fn(js: str, name: str) -> str:
    start = js.index(name)
    depth = 0
    for k in range(js.index("{", start), len(js)):
        if js[k] == "{":
            depth += 1
        elif js[k] == "}":
            depth -= 1
            if depth == 0:
                return js[start : k + 1]
    raise AssertionError(name)


def test_hello_reports_the_version_without_a_token(monkeypatch):
    monkeypatch.setenv("MINDFLOCK_AUTH_TOKEN", "gate-is-on")
    monkeypatch.delenv("MINDFLOCK_AUTH", raising=False)
    client = TestClient(server.app)
    assert client.get("/api/doctor").status_code == 401  # why the old check failed
    resp = client.get("/api/remote/hello")
    assert resp.status_code == 200
    assert resp.json()["version"] == __version__


def test_engine_version_asks_the_public_ping_first():
    body = _fn(
        _MAIN_JS.read_text(encoding="utf-8"), "async function localEngineVersion"
    )
    assert re.search(r"\[\s*'/api/remote/hello'\s*,\s*'/api/doctor'\s*\]", body)


def test_check_no_longer_reads_api_doctor_directly():
    body = _fn(
        _MAIN_JS.read_text(encoding="utf-8"), "async function checkEngineVersion"
    )
    assert "fetchLocalJSON('/api/doctor')" not in body
    assert "localEngineVersion()" in body


def test_update_info_says_whether_the_check_succeeded():
    js = _MAIN_JS.read_text(encoding="utf-8")
    start = js.index("ipcMain.handle('engine:update-info'")
    handler = js[start : js.index("ipcMain.handle(", start + 1)]
    assert "checked: engineCheck.checked" in handler


def test_settings_does_not_call_a_failed_check_up_to_date():
    tsx = _ADVANCED.read_text(encoding="utf-8")
    failed = tsx.index("info?.checked === false")
    assert failed < tsx.index("The engine is up to date")
    assert "Couldn’t check for engine updates" in tsx
