"""Auth profiles: the routes the UI calls exist on the server.

A backend↔frontend contract, so it reads the bundle the server actually serves
AND the server's own route table. The UI surfaces themselves (the Accounts
screen, the New dialog's account/model pickers, the pane's swap chip) are
pinned against the source in ``frontend/src/__tests__/accountsUi.test.ts``.
"""

from starlette.testclient import TestClient

from backend.web import server

client = TestClient(server.app)


def _js() -> str:
    return client.get("/app.js").text


def _paths() -> dict:
    # Included routers are opaque in app.routes; the OpenAPI map flattens them.
    return server.app.openapi()["paths"]


def test_accounts_screen_talks_to_the_profiles_api():
    js = _js()
    assert "/api/settings/auth-profiles" in js
    assert "/api/settings/test/openrouter" in js
    paths = _paths()
    assert "/api/settings/auth-profiles" in paths
    assert "/api/settings/test/openrouter" in paths


def test_pane_chip_swaps_through_the_per_instance_profile_route():
    # The swap posts to the per-instance profile endpoint.
    assert "/profile" in _js()
    assert "/api/instances/{title}/profile" in _paths()
