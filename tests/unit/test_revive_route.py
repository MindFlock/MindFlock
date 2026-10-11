"""``POST /api/instances/{title}/revive`` — the Offline chip's Restart action.

The route hands the session to the same lazy revive a terminal attach or a
sent prompt uses (``_ensure_agent_session``); these tests monkeypatch that
helper, so nothing here touches tmux, and pin: the 404 for an unknown title,
the 409 for a paused session (resume it instead), the helper's reason as a
409, and the ``{"title", "revived": true}`` body on success.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.session.storage import Paused, Status
from backend.web import server
from backend.web.server import app

client = TestClient(app)


class _Inst:
    def __init__(self, status=Status.Running):
        self.Status = status
        self.Program = "claude"


@pytest.fixture
def instances(monkeypatch):
    """A registry of our own: the live ENGINE may hold the owner's sessions."""
    monkeypatch.setattr(server.ENGINE, "instances", {})
    return server.ENGINE.instances


def test_revive_404_for_unknown(instances):
    r = client.post("/api/instances/nope-revive/revive")
    assert r.status_code == 404
    assert "instance not found" in r.json()["error"]


def test_revive_calls_the_lazy_revive_helper(instances, monkeypatch):
    inst = _Inst()
    instances["rv-1"] = inst
    calls = []

    def fake_ensure(i, title):
        calls.append((i, title))
        return "mindflock-rv-1", None

    monkeypatch.setattr(server, "_ensure_agent_session", fake_ensure)
    r = client.post("/api/instances/rv-1/revive")
    assert r.status_code == 200
    assert r.json() == {"title": "rv-1", "revived": True}
    assert calls == [(inst, "rv-1")]


def test_revive_surfaces_the_helper_reason_as_409(instances, monkeypatch):
    instances["rv-2"] = _Inst()
    monkeypatch.setattr(
        server,
        "_ensure_agent_session",
        lambda i, t: ("mindflock-rv-2", "workspace no longer exists"),
    )
    r = client.post("/api/instances/rv-2/revive")
    assert r.status_code == 409
    assert r.json()["error"] == "workspace no longer exists"


def test_revive_refuses_a_paused_session(instances, monkeypatch):
    instances["rv-3"] = _Inst(status=Paused)
    called = []
    monkeypatch.setattr(
        server, "_ensure_agent_session", lambda i, t: called.append(t) or ("x", None)
    )
    r = client.post("/api/instances/rv-3/revive")
    assert r.status_code == 409
    assert "paused" in r.json()["error"]
    assert called == []
