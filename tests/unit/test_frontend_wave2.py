"""Frontend wave-2 wiring (A4, C2, D2/D5, E1–E3): backend↔frontend contracts.

What stays here is what the server owns: core/events.js (hand-written, served
as-is), the served index.html and its load order, and the API routes the UI
calls. The markup hooks, JS wiring and CSS rules are pinned against their
source modules in frontend/src/__tests__/wave2Wiring.test.ts.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.web import server

client = TestClient(server.app)


def test_events_js_serves():
    r = client.get("/core/events.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert len(r.content) > 0


def test_events_js_public_contract():
    js = client.get("/core/events.js").text
    # The documented window.mindflock extension API (matched by docs/extensions.md).
    assert "window.mindflock = window.mindflock || {}" in js
    assert "subscribe" in js and "onStatus" in js
    assert "lastSeq" in js and "connected" in js
    assert "mf.sessions" in js and "__setSessions" in js
    assert "/api/events" in js
    assert "?since=" in js  # reconnect dedupe


def test_index_loads_events_before_app_and_slots():
    html = client.get("/").text
    assert "/core/events.js" in html
    # app.js is a deferred module and slots.js is injected after first paint,
    # so the classic events.js script still EXECUTES first.
    assert 'type="module"' in html and "/app.js" in html


def test_the_ui_calls_routes_the_server_serves():
    """The API paths the bundle calls — D5 open-in-IDE, C2 the doctor-backed
    checklist, E3 the account tests, D2 the detected-IDE picker — exist on the
    server. Reads the served bundle because the contract is between the two."""
    js = client.get("/app.js").text
    paths = server.app.openapi()["paths"]
    for path in (
        "/api/doctor",
        "/api/settings/test/shortcut",
        "/api/settings/test/github",
        "/api/settings/test/agent",
        "/api/ides",
    ):
        assert path in js, path
        assert path in paths, path
    # D5: the open-in-IDE action hits the primary /ide route.
    assert '"/ide"' in js
    assert "/api/instances/{title}/ide" in paths


# --------------------------------------------------------------------------- #
# Wave 3 (F3/F4/F5/F8): same structural-contract style as above.
# --------------------------------------------------------------------------- #


def test_config_has_no_repo_root():
    # F5 was retired: the server has no meaningful "managed repo" (it's a
    # global tool whose cwd is arbitrary; sessions pick their own repos), so
    # /api/config no longer advertises one.
    r = client.get("/api/config")
    assert r.status_code == 200
    assert "repo_root" not in r.json()


def test_index_has_wave3_markup():
    html = client.get("/").text
    # F5: the managed-repo path line under the title was removed (visual clutter).
    assert 'id="repo-root"' not in html
