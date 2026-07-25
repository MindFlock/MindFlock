"""Send / prompt-queue frontend wiring (M-series): structural contract checks.

The live UI is verified with screenshots/CDP; these pin the markup hooks, JS
wiring, and CSS so the Queue tab, its inline console, and the palette actions
can't silently regress. Same style as test_frontend_wave4.py.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.web import server

client = TestClient(server.app)


def test_app_js_pane_has_queue_tab():
    js = client.get("/app.js").text
    # Queue is a first-class pane tab (beside Agent/Terminal/Diff), with a badge.
    assert '"data-tab": "queue"' in js
    assert "queue-tab-badge" in js
    assert '"pane-queue"' in js
    # Selecting the tab loads the inline console.
    assert "QueueTab" in js
    # Badge painted from the /api/instances queue summary each poll.
    assert "inst.queue" in js and "queue-tab-badge" in js


def test_app_js_queue_console_uses_backend_endpoints():
    js = client.get("/app.js").text
    assert "Add to queue" in js
    assert "/queue/reorder" in js
    # Send-now hits /send; add hits /queue; flags + reorder + delete wired.
    assert '"/send"' in js
    assert '"/queue"' in js
    assert '"/queue/flags"' in js
    assert '"/queue/reorder"' in js
    # Per-item ▶ fires one queued entry immediately via the send_now endpoint.
    assert '"/queue/send_now"' in js
    assert "qi-send" in js
    # Loop + auto-run + wait-out-usage-limit toggles present.
    assert "queue-loop" in js and "queue-enabled" in js and "queue-wait" in js
    # The wait-out toggle posts the wait_for_limit flag.
    assert "wait_for_limit" in js
    # Timed loop: an interval input that posts loop_interval (minutes).
    assert "queue-loop-interval" in js
    assert "loop_interval" in js


def test_app_js_palette_has_send_and_queue_actions():
    js = client.get("/app.js").text
    assert "Send message… — " in js
    assert "Queue prompt… — " in js
    assert "function sendMessagePrompt(" in js
    assert "function queuePromptPrompt(" in js


def test_style_css_has_queue_rules():
    css = client.get("/style.css").text
    for sel in (
        ".queue-tab",
        ".queue-tab-badge",
        ".pane-queue",
        ".queue-input",
        ".queue-item",
        ".queue-flags",
        ".queue-status.queue-stopped",
    ):
        assert sel in css, sel
