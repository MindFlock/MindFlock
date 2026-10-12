"""UI polish round 3: the parts the server serves as-is.

The CSS rules and markup hooks of this round (addon-bar styling, session-row
chips, provisioning copy, pane-header shrink order, the ingestion dot, the
draggable booting pane) are pinned against their source modules in
frontend/src/__tests__/polishWiring.test.ts. What stays here: core/slots.js and
addons/notify.js (hand-written, served as-is), the served index.html, and
GET /api/config.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.web import server

client = TestClient(server.app)


# --------------------------------------------------------------------------- #
# Generic addon bars (core/slots.js).
# --------------------------------------------------------------------------- #


def test_slots_js_renders_addon_bar_markup():
    js = client.get("/core/slots.js").text
    assert 'bar.className = "addon-bar"' in js
    assert "bar.dataset.addon" in js
    assert 'label.className = "addon-label"' in js


# --------------------------------------------------------------------------- #
# Notify addon toggle: honest about why it can't work.
# --------------------------------------------------------------------------- #


def test_notify_js_toggle_contract():
    js = client.get("/addons/notify.js").text
    # State persists in localStorage; permission is requested lazily on enable.
    assert "mindflock.notify.enabled" in js
    assert "requestPermission" in js
    # notify.js has no visible surface of its own anymore — the on/off toggle
    # lives in the bell dropdown + Settings. It exposes the shared API those
    # UIs drive, and a browser-side denial reports tri-state "blocked".
    assert '"blocked"' in js
    assert "window.mindflockAddons.notify.enable = enable" in js
    assert "window.mindflockAddons.notify.disable = disable" in js
    assert "window.mindflockAddons.notify.state = state" in js
    # Both mirrors (bell + Settings) stay in sync via this broadcast.
    assert "mf-notify-state" in js
    # No-Notification-API environments (plain-http origins) expose an
    # "unsupported" state + the reason WHY, so the toggles render disabled.
    assert "isSecureContext" in js
    assert "unsupported" in js


# --------------------------------------------------------------------------- #
# Provisioning fields: no sitecheck-era spellings in what the server serves.
# --------------------------------------------------------------------------- #


def test_index_has_no_sitecheck_mentions():
    html = client.get("/").text
    assert "sitecheck" not in html.lower()


def test_backend_selector_removed():
    # There is no per-session backend selector in the UI.
    html = client.get("/").text
    assert "Backend for new sessions" not in html
    assert 'id="backend-mode"' not in html


def test_api_config_has_no_deprecated_alias():
    cfg = client.get("/api/config").json()
    assert "provisioning_available" in cfg
    assert "sitecheck_available" not in cfg
