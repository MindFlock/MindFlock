"""Bulk session actions (sidebar multi-select) frontend wiring.

Checkboxes drive a batch bar that fans out over the existing per-session
endpoints. Live behaviour verified with CDP; these pin the wiring + CSS.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.web import server

client = TestClient(server.app)


def test_index_has_bulk_bar():
    assert '"bulk-bar"' in client.get("/app.js").text


def test_app_js_has_no_per_row_bulk_checkbox():
    # The per-row bulk-select checkbox was removed from the sidebar (visual
    # clutter); sessions are acted on individually via the row ⋯ menu.
    js = client.get("/app.js").text
    assert 'class="bulk-cb"' not in js


def test_style_css_has_bulk_rules():
    css = client.get("/style.css").text
    for sel in ("#bulk-bar", ".bulk-acts"):
        assert sel in css, sel
