"""UI sweep P5: the server polls behind Settings, Intake and the phone page,
and the extension windows that poll it.

- GET /api/fleet reads fleet.json / fleet_update.json OFF the event loop.
- GET /api/mindflock/status reuses one reading for a moment (every window
  polls it every 4 s); a start or stop clears it, and so does a settings save.
- The Optimizer, Workflow and To-do windows skip their timer while hidden; the
  Optimizer and Workflow windows say the real reason a load failed, with Retry.
- The Database window asks about unsaved edits on its own confirm bar, never
  window.confirm (the desktop app has none).
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.config import settings as S
from backend.web.addons import fleet as fleet_addon
from backend.web.addons import ticket_ingestion as ti

EXT = Path(__file__).resolve().parents[2] / "backend" / "web" / "static" / "extensions"


def _js(rel: str) -> str:
    return (EXT / rel).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# GET /api/fleet
# --------------------------------------------------------------------------- #
def test_get_fleet_reads_its_files_off_the_event_loop(monkeypatch):
    calls: list = []
    real = asyncio.to_thread

    async def spy(fn, *a, **kw):
        calls.append(fn)
        return await real(fn, *a, **kw)

    async def no_refresh():
        return None

    async def privileged(_request):
        return True

    seen: dict = {}

    def fleet_status(privileged=False):
        seen["fleet"] = privileged
        return {"members": []}

    def update_status():
        seen["update"] = True
        return {"state": "idle"}

    monkeypatch.setattr(fleet_addon.asyncio, "to_thread", spy)
    monkeypatch.setattr(fleet_addon.fleet, "refresh_exposure", no_refresh)
    monkeypatch.setattr(fleet_addon.fleet, "status", fleet_status)
    monkeypatch.setattr(fleet_addon.fleet_update, "status", update_status)
    monkeypatch.setattr(fleet_addon, "_privileged", privileged)
    app = FastAPI()
    app.include_router(fleet_addon.FleetAddon().router)
    r = TestClient(app).get("/api/fleet")
    assert r.status_code == 200
    assert r.json() == {"members": [], "update": {"state": "idle"}}
    # Both readings ran, inside the one function handed to to_thread.
    assert seen == {"fleet": True, "update": True}
    assert len(calls) == 1 and calls[0].__name__ == "_read"


# --------------------------------------------------------------------------- #
# GET /api/mindflock/status
# --------------------------------------------------------------------------- #
@pytest.fixture
def status_client(monkeypatch, tmp_path, isolate_settings_store):
    addon = ti.TicketIngestionAddon()
    addon.ctrl._repo_root = tmp_path
    addon.ctrl._log_path = tmp_path / "logs" / "ticket-ingestion.log"
    readings: list = []

    def status():
        readings.append(1)
        return {"running": False, "n": len(readings)}

    monkeypatch.setattr(addon.ctrl, "status", status)
    app = FastAPI()
    app.include_router(addon.router)
    return TestClient(app), addon, readings


def test_two_polls_within_the_window_read_once(status_client):
    client, _addon, readings = status_client
    a = client.get("/api/mindflock/status").json()
    b = client.get("/api/mindflock/status").json()
    assert a == b == {"running": False, "n": 1}
    assert len(readings) == 1


def test_the_reading_expires(status_client, monkeypatch):
    client, _addon, readings = status_client
    client.get("/api/mindflock/status")
    now = ti.time.monotonic()
    monkeypatch.setattr(ti.time, "monotonic", lambda: now + ti.STATUS_CACHE_S + 1)
    client.get("/api/mindflock/status")
    assert len(readings) == 2


def test_start_and_stop_clear_the_reading(status_client, monkeypatch):
    client, addon, readings = status_client
    monkeypatch.setattr(ti, "_ingestion_repo_available", lambda: True)
    monkeypatch.setattr(ti, "_record_desired_running", lambda on: None)
    monkeypatch.setattr(ti, "_desired_running", lambda: False)
    monkeypatch.setattr(addon.ctrl, "_own_running", lambda: False)
    # The real start()/stop() wrappers (which clear the reading) around a
    # body that spawns nothing.
    monkeypatch.setattr(addon.ctrl, "_start", lambda: True)
    monkeypatch.setattr(addon.ctrl, "_stop", lambda: True)

    client.get("/api/mindflock/status")
    assert client.post("/api/mindflock/start").status_code == 200
    n = len(readings)
    client.get("/api/mindflock/status")
    assert len(readings) == n + 1  # read fresh, not the pre-start reading

    assert client.post("/api/mindflock/stop").status_code == 200
    n = len(readings)
    client.get("/api/mindflock/status")
    assert len(readings) == n + 1


def test_a_settings_save_clears_the_reading(status_client):
    client, _addon, readings = status_client
    client.get("/api/mindflock/status")
    S.update_settings(github={"automation_device": "laptop"})
    client.get("/api/mindflock/status")
    assert len(readings) == 2


def test_a_reading_taken_across_a_start_is_not_kept(status_client, monkeypatch):
    """A status read while start() runs must not be stored as fresh."""
    _client, addon, readings = status_client
    ctrl = addon.ctrl

    def slow_status():
        readings.append(1)
        ctrl.forget_status()  # a start/stop landed mid-reading
        return {"running": False}

    monkeypatch.setattr(ctrl, "status", slow_status)
    ctrl.cached_status()
    ctrl.cached_status()
    assert len(readings) == 2


# --------------------------------------------------------------------------- #
# Extension windows (hand-written JS, served as is)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "rel", ["optimizer/view.js", "workflow/view.js", "todo/view.js"]
)
def test_extension_polls_skip_a_hidden_window(rel):
    code = _js(rel)
    m = re.search(r"setInterval\(\(\) => \{\s*if \(document\.hidden\) return;", code)
    assert m, rel + ": the poll must skip ticks while the window is hidden"


@pytest.mark.parametrize("rel", ["optimizer/view.js", "workflow/view.js"])
def test_extension_load_failure_says_why_with_retry(rel):
    code = _js(rel)
    assert "Could not reach the server." not in code
    assert (
        '(err && err.message) || "Couldn\'t reach MindFlock. Is it still running?"'
        in code
    )
    assert 'text: "Retry", onClick: () => load(true)' in code


def test_dbclient_unsaved_edits_ask_on_the_confirm_bar():
    code = _js("dbclient/explorer.js")
    assert "window.confirm" not in code
    # The navigation is held and runs only on "Discard".
    assert "function confirmDiscardEmbed(then)" in code
    assert "if (!dirty) return then();" in code
    assert "if (yes && !disposed) then();" in code
    assert 'confirmLabel: "Discard"' in code
    # No caller still treats it as a synchronous yes/no.
    assert "if (!confirmDiscardEmbed())" not in code
    assert code.count("confirmDiscardEmbed(() =>") == 3
