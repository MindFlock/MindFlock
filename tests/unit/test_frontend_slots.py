"""Frontend addon-slots + provider picker wiring (Stage E).

Structural checks (the live UI is verified manually in a browser): the new
core/ JS modules serve, the bundle loads the one the server serves, and the
addon manifest carries the builtin_ui flag the slot renderer keys off. The
source-side wiring is pinned by vitest (frontend addonSlots.test.ts).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.web import server

client = TestClient(server.app)


def test_core_js_modules_serve():
    for p in ("/core/slots.js", "/core/ws-xterm.js"):
        r = client.get(p)
        assert r.status_code == 200, p
        assert "javascript" in r.headers["content-type"]
        assert len(r.content) > 0


def test_the_bundle_loads_the_served_slots_module():
    # The bundle injects /core/slots.js after its first paint (frontend
    # main.tsx; the agent picker and #addon-bars mount it needs are pinned by
    # vitest, addonSlots.test.ts), so the server must serve that exact path.
    assert "/core/slots.js" in client.get("/app.js").text
    r = client.get("/core/slots.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]


def test_manifest_flags_builtin_addons_and_keeps_modules_honest():
    addons = {a["id"]: a for a in client.get("/api/addons").json()["addons"]}
    builtin = ("mindflock", "assistant", "settings", "doctor")
    for aid in builtin:
        fe = addons[aid]["frontend"][0]
        # Hand-wired UIs -> builtin_ui True (slots skips them) and no module
        # URL (there is no ES file to load; the manifest must not lie).
        assert fe["builtin_ui"] is True, aid
        assert fe["module"] is None, aid
    # notify is the generic path: slots renders its bar AND loads its module.
    fe = addons["notify"]["frontend"][0]
    assert fe["builtin_ui"] is False
    assert fe["module"] == "/addons/notify.js"


def test_addon_module_serves_and_registers_init():
    r = client.get("/addons/notify.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    # The module contract: register { init(ctx) } under window.mindflockAddons.
    assert "window.mindflockAddons" in r.text
    assert "init(ctx)" in r.text


def test_slots_js_loads_addon_modules():
    js = client.get("/core/slots.js").text
    # The generic loader: dynamic import of descriptor.module, then init via
    # the window.mindflockAddons registry, fed the client event bus.
    assert "import(desc.module)" in js
    assert "window.mindflockAddons" in js
    assert "window.mindflock" in js
