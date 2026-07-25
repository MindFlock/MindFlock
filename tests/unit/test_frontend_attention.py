"""O1–O4 frontend wiring: structural contract checks.

Pins the needs-attention inbox (O1), the setup/check chips + menu actions
(O2/O3), the push soft-gate override flow (O3), and the preview-port link
(O4) in app.js / mobile.js / style.css. Same style as test_frontend_queue.py.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.web import server

client = TestClient(server.app)


# --------------------------------------------------------------------------- #
# O1: needs-attention — surfaced via the notification bell
# --------------------------------------------------------------------------- #
def test_app_js_attention_source_and_priorities():
    js = client.get("/app.js").text
    assert "function attentionItems(" in js
    # Priorities: clarify (with snippet) > broken > checks > ready-for-PR.
    assert 'reason: "needs your answer"' in js
    assert "worktree setup failed" in js
    assert "checks failing" in js
    assert 'reason: "pushed — ready for PR"' in js


def test_app_js_attention_is_in_the_bell_not_the_sidebar():
    """Attention rides the notification bell popover + badge — it touches
    neither the session list nor the pane tabs (no sidebar bar/overlay)."""
    js = client.get("/app.js").text
    html = client.get("/").text
    # Live section pinned in the bell popover + badge/refresh wiring.
    assert "Needs attention" in js
    assert "notif-badge" in js
    assert "attn-item" in js  # rendered inside _renderNotifList
    assert "attentionItems(" in js
    # Badge NUMBER is attention-only (auto-clears on resolve); unread history
    # is demoted to a dot shown only when nothing is actionable.
    assert "attn.length > 99" in js
    assert "attn.length === 0" in js
    assert "has-unread" in js
    # The retired sidebar bar/overlay is fully gone.
    assert "attn-bar" not in html and "attn-panel" not in html
    assert "attn-box" not in js and "updateAttention" not in js
    # Clicking a pinned row focuses the session and closes the popover.
    assert "jump(it.title)" in js


def test_app_js_clarify_notification_carries_snippet():
    js = client.get("/app.js").text
    assert "needs your input" in js


def test_style_css_attention_styles():
    css = client.get("/style.css").text
    # Attention lives in the bell popover now: amber badge + pinned section;
    # unread history is a neutral dot.
    assert ".notif-badge.attn" in css
    assert "#notif-btn.has-unread::after" in css
    assert ".notif-attn" in css and ".notif-attn-head" in css
    assert ".attn-item" in css and ".attn-snippet" in css
    assert ".stagechip.checkchip" in css
    # The retired sidebar bar/overlay CSS is gone.
    assert "#attn-bar" not in css and "#attn-panel" not in css


def test_mobile_js_attention_picker():
    js = client.get("/mobile.js").text
    assert "function attnRank(" in js and "function attnMark(" in js
    # Attention-first sort + rank in the poll signature so markers repaint.
    assert "attnRank(a) - attnRank(b)" in js
    assert "attnRank(i) + i.title" in js


# --------------------------------------------------------------------------- #
# O2/O3: chips, menu actions, gate override
# --------------------------------------------------------------------------- #
def test_app_js_setup_states_in_chip():
    js = client.get("/app.js").text
    assert '"setup ✗"' in js and '"setting up"' in js


def test_app_js_check_chip_and_actions():
    js = client.get("/app.js").text
    assert "function checkChip(" in js
    assert "checkChip(inst)" in js
    assert "Re-run worktree setup" in js and "/setup/rerun" in js
    assert "Run checks now" in js
    assert "session.setup_finished" in js and "session.check_finished" in js


def test_app_js_push_gate_override():
    js = client.get("/app.js").text
    assert "checks haven't passed for this commit" in js
    assert "pushSession(title, true)" in js


# --------------------------------------------------------------------------- #
# O4: preview link
# --------------------------------------------------------------------------- #
def test_app_js_preview_link():
    js = client.get("/app.js").text
    assert "Open preview ↗" in js
    assert "inst.ports.base" in js
