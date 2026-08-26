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
        # "the agent has finished" comes from the turn-boundary event, never
        # from the raw idle flip — that one fires at the end of every assistant
        # turn and for a window that has merely been re-opened.
        "session.turn_ended",
    ):
        assert kind in js, kind
    assert "finished — now idle" not in js


def test_style_css_has_notification_rules():
    css = client.get("/style.css").text
    for sel in ("#notif-btn", ".notif-badge", ".notif-pop", ".notif-item"):
        assert sel in css, sel


def test_bell_renders_the_dwell_the_turn_boundary_carries():
    """The event's whole claim is that the quiet LASTED; a bare "finished"
    throws away the part that makes it trustworthy. And the row keeps the
    ``n-done`` class the bell's attention styling and badge counting key on."""
    js = client.get("/app.js").text
    at = js.index('case "session.turn_ended"')
    block = js[at : at + 400]
    assert "idle_for" in block
    assert "n-done" in block
    # Minutes past a point, so a session left alone all afternoon does not
    # report "finished — idle 9412s".
    assert "/ 60" in block


def test_the_browser_channel_dedupes_per_RULE_like_the_server_does():
    """Same bug, same fix, in the channel where it also swapped a POPUP.

    The key is reused as the Notification ``tag``, so an event-keyed window did
    not merely swallow the second rule's push — the browser replaced the first
    rule's still-visible popup with it.
    """
    js = client.get("/addons/notify.js").text
    assert 'const key = env.session + "|" + rule.id;' in js
    assert "tag: key" in js or "tag:key" in js


def test_the_two_channels_collapse_a_flap_over_the_same_window():
    """A mirrored constant with a comment saying so is a constant that drifts.

    Both channels dedupe the same (session, rule) pair; if only one of them
    widened, a flapping transition would push twice on the phone and once in
    the browser (or the reverse), which reads as a bug in whichever the user
    happens to be looking at.
    """
    from backend.web.addons import notify as notify_addon

    js = client.get("/addons/notify.js").text
    at = js.index("const DEDUPE_MS")
    ms = int(js[at : js.index("\n", at)].split("=")[1].strip().rstrip(";"))
    assert ms == notify_addon._DEDUPE_SECONDS * 1000
