"""Session-start paths and the terminal socket (UI sweep round 3, R5).

* A waiting worker's agent socket closes with 4410 "waiting its turn", not
  4409 (which the pane reads as gone and never reconnects).
* ``POST /api/instances`` reads the ports file and ``.mindflock.toml`` in a
  thread, not on the event loop.
* With a budget set, a keystroke never prices transcripts on the event loop:
  the terminal's input path runs the lock check in a thread when the cost is
  out of the token cache (``core.budget._budget_cost_stale``).

``ENGINE.instances`` is replaced per test (the live engine reads the real
state.json); nothing launches.
"""

from __future__ import annotations

import inspect
import json
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.web import server
from backend.web.core import session_create
from backend.web.core.agent_sessions import WAITING_TURN_REFUSED


class _Inst:
    def __init__(self, title="w", wt="/w/x", started=True):
        self.Title = title
        self._wt = wt
        self._started = started

    def Started(self):  # noqa: N802
        return self._started

    def GetWorktreePath(self):  # noqa: N802
        return self._wt


# --------------------------------------------------------------------------- #
# the waiting worker's socket                                                 #
# --------------------------------------------------------------------------- #
def test_a_waiting_workers_socket_closes_4410(monkeypatch):
    monkeypatch.setattr(server.ENGINE, "instances", {"w": _Inst()})
    monkeypatch.setattr(
        server,
        "ensure_terminal_session",
        lambda inst, title: ("mindflock_w", WAITING_TURN_REFUSED),
    )
    c = TestClient(server.app)
    with c.websocket_connect("/api/instances/w/terminal") as ws:
        frame = json.loads(ws.receive_text())
        assert frame == {"type": "error", "message": WAITING_TURN_REFUSED}
        with pytest.raises(WebSocketDisconnect) as ei:
            ws.receive_text()
    assert ei.value.code == 4410
    assert ei.value.reason == "waiting its turn"


def test_any_other_refusal_still_closes_4409(monkeypatch):
    monkeypatch.setattr(server.ENGINE, "instances", {"w": _Inst()})
    monkeypatch.setattr(
        server,
        "ensure_terminal_session",
        lambda inst, title: ("mindflock_w", "workspace no longer exists"),
    )
    c = TestClient(server.app)
    with c.websocket_connect("/api/instances/w/terminal") as ws:
        ws.receive_text()
        with pytest.raises(WebSocketDisconnect) as ei:
            ws.receive_text()
    assert ei.value.code == 4409


# --------------------------------------------------------------------------- #
# POST /api/instances: file work off the loop                                 #
# --------------------------------------------------------------------------- #
def test_create_reads_ports_and_worktree_config_in_a_thread():
    src = inspect.getsource(session_create._create)
    assert "asyncio.to_thread(srv._ports.env_for, title)" in src
    assert "asyncio.to_thread(srv._wt_setup.load_config, plain_path)" in src
    assert "srv._ports.env_for(title)" not in src
    assert "srv._wt_setup.load_config(plain_path)" not in src


# --------------------------------------------------------------------------- #
# the budget lock on the keystroke path                                       #
# --------------------------------------------------------------------------- #
@pytest.fixture
def budget(monkeypatch):
    limit = {"usd": 5.0}
    monkeypatch.setattr(server, "_effective_budget", lambda t: limit["usd"])
    monkeypatch.setattr(server.ENGINE, "instances", {"s": _Inst("s", "/w/s")})
    monkeypatch.setattr(server, "_TOKENS_CACHE", {})
    return limit


def test_no_budget_never_needs_pricing(budget):
    budget["usd"] = 0.0
    assert server._budget_cost_stale("s") is False


def test_a_cost_out_of_the_cache_needs_pricing(budget):
    assert server._budget_cost_stale("s") is True
    server._TOKENS_CACHE[("s", "/w/s")] = (time.time() - 1, {"cost": 1.0})  # expired
    assert server._budget_cost_stale("s") is True


def test_a_cached_cost_is_read_in_line(budget):
    server._TOKENS_CACHE[("s", "/w/s")] = (time.time() + 20, {"cost": 1.0})
    assert server._budget_cost_stale("s") is False


def test_a_session_not_started_or_gone_needs_nothing(budget, monkeypatch):
    assert server._budget_cost_stale("nobody") is False
    server.ENGINE.instances["s"] = _Inst("s", "/w/s", started=False)
    assert server._budget_cost_stale("s") is False


def test_the_terminal_input_path_prices_in_a_thread():
    src = inspect.getsource(server.terminal_ws)
    assert "if _budget_cost_stale(title):" in src
    assert "await asyncio.to_thread(_budget_locked, title)" in src
    # Every input branch goes through it.
    assert src.count("await over_budget()") == 3
    assert "if _budget_locked(title) or" not in src
    assert "if not _budget_locked(title)" not in src
