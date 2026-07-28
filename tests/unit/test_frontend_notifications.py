"""Notification center (bell) frontend wiring: structural contract checks.

The bell replays the events-bus backlog ("what happened while I was away") and
badges unread events; the live behaviour is verified with CDP. These pin the
markup + JS + CSS hooks. Same style as test_frontend_wave4.py.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.web import server

client = TestClient(server.app)


def test_index_has_bell_markup():
    html = client.get("/").text
    assert '"notif-btn"' in client.get("/app.js").text
    assert '"notif-badge"' in client.get("/app.js").text


def test_app_js_notification_wiring():
    js = client.get("/app.js").text
    # Fed by the events bus (all events), curated into notifications.
    assert 'subscribe("*"' in js
    assert "function notifFromEvent(" in js
    # Unread is keyed on timestamp (survives server seq resets), not seq.
    assert "NOTIF_SEEN_KEY" in js
    assert "mf_notif_seen_ts" in js
    # Clicking a notification focuses that session.
    assert "selectSession(" in js
    # Curated event kinds surfaced.
    for kind in (
        "session.budget_exceeded",
        "session.prompt_sent",
        "session.activity_changed",
        "session.stage_changed",
    ):
        assert kind in js, kind


def test_style_css_has_notification_rules():
    css = client.get("/style.css").text
    for sel in ("#notif-btn", ".notif-badge", ".notif-pop", ".notif-item"):
        assert sel in css, sel
