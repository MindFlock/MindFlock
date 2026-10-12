"""Browser notifications: the served notify addon (/addons/notify.js).

The addon is hand-written and served as-is, so it is checked here. The bell
itself (markup, event curation, styling) is React and is pinned in vitest:
frontend/src/__tests__/notifyWiring.test.ts.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.web import server

client = TestClient(server.app)


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
