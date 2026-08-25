"""Route-precedence + mount-last smoke for the web app.

The app mounts ``StaticFiles`` at ``/`` and relies on every ``/api/*`` route and
``/m`` being registered BEFORE that mount so they win. The core split (Stage C)
and the addon framework (Stage D, which moves routes onto APIRouters and
``include_router``s them) must preserve this. These assertions catch a
regression where the static mount shadows the API.

Imports ``backend.web.server`` (and thus builds the engine + seeds the assistant dir),
mirroring the existing test_mindflock.web.py contract test.
"""

from __future__ import annotations

from starlette.routing import Mount
from fastapi.testclient import TestClient

from backend.web import server

client = TestClient(server.app)


def test_static_mount_is_last_route():
    # The catch-all StaticFiles mount at "/" must be the final route so it never
    # shadows an /api/* or /m handler.
    mounts = [r for r in server.app.routes if isinstance(r, Mount) and r.path == ""]
    assert mounts, "expected a StaticFiles mount at '/'"
    # Its index in the route table is the maximum (registered last).
    last = server.app.routes[-1]
    assert isinstance(last, Mount)


def test_api_routes_resolve_over_static():
    r = client.get("/api/instances")
    assert r.status_code == 200
    assert isinstance(r.json(), list)

    r = client.get("/api/config")
    assert r.status_code == 200
    assert "default_program" in r.json()


def test_mobile_route_resolves():
    r = client.get("/m")
    assert r.status_code == 200
    # /m serves the mobile shell, not the desktop index.
    assert "<!doctype html" in r.text.lower() or "<html" in r.text.lower()


def test_root_serves_spa():
    r = client.get("/")
    assert r.status_code == 200
    assert "/app.js" in r.text


def test_lifespan_starts_and_cancels_background_tasks():
    # Entering the TestClient context manager runs lifespan startup; exiting runs
    # shutdown. The lifespan must start the background loops and cancel them on
    # shutdown (the old @app.on_event hooks never cleaned up).
    assert server._BG_TASKS == []
    with TestClient(server.app) as c:
        c.get("/api/config")
        # Nine long-lived loops always stay registered (they never return):
        # reload loop + instances tick + cursor auto-adopt + prompt-queue drain
        # + autopilot + window-refresh + test-plans due + device discovery +
        # remote instances.
        # A tenth task (startup warmups: scroll speed / paste GC / mobile
        # banner) is also registered, but it is short-lived and removes itself
        # via its done-callback the moment it finishes. On a host where the
        # Tailscale banner probe returns instantly (e.g. CI without tailscale)
        # it can complete before this assertion runs, so tolerate 9 or 10.
        assert 9 <= len(server._BG_TASKS) <= 10
    assert server._BG_TASKS == []  # cancelled + cleared on shutdown


def test_engine_is_a_singleton():
    from backend.web.core.engine import get_engine

    assert get_engine() is get_engine()
    assert server.ENGINE is get_engine()
