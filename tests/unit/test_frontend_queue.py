"""Send / prompt-queue: the backend<->frontend route contract.

The Queue tab's markup, wiring and CSS are pinned at the source by vitest
(frontend/src/__tests__/queueTabWiring.test.ts). What stays here is the half
only the server can answer: every queue endpoint the shipped bundle calls
exists on the server, with the method the console uses.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.web import server

client = TestClient(server.app)

_INSTANCE = "/api/instances/{title}"

# Suffix the console passes to instApi/qApi -> methods it calls it with.
_QUEUE_ROUTES = {
    "/send": {"post"},
    "/queue": {"get", "post", "delete"},
    "/queue/flags": {"post"},
    "/queue/reorder": {"post"},
    "/queue/send_now": {"post"},
}


def test_queue_console_routes_exist_on_the_server():
    js = client.get("/app.js").text
    paths = server.app.openapi()["paths"]
    for suffix, methods in _QUEUE_ROUTES.items():
        # The bundle calls it ...
        assert f'"{suffix}"' in js, suffix
        # ... and the server answers it.
        route = _INSTANCE + suffix
        assert route in paths, route
        assert methods <= set(paths[route]), (route, sorted(paths[route]))
    # The flags the toggles post are the ones the console sends.
    assert "wait_for_limit" in js
    assert "loop_interval" in js
