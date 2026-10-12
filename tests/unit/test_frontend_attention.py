"""O1–O4 frontend wiring: the backend↔frontend contract.

The bundle's structure (the bell's needs-attention section, the setup/check
chips and their menu actions, the push gate override, the preview link) is
pinned against the source in frontend/src/__tests__/attentionWiring.test.ts.
What stays here is what only the server side can show: the route the
"Re-run worktree setup" action calls exists, and the hand-written mobile view
(``mobile.js``, never bundled) sorts attention first.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.web import server

client = TestClient(server.app)


def test_setup_rerun_action_calls_a_live_route():
    js = client.get("/app.js").text
    assert '"/setup/rerun"' in js
    assert "/api/instances/{title}/setup/rerun" in server.app.openapi()["paths"]


def test_mobile_js_attention_picker():
    js = client.get("/mobile.js").text
    assert "function attnRank(" in js and "function attnMark(" in js
    # Attention-first sort + rank in the poll signature so markers repaint.
    assert "attnRank(a) - attnRank(b)" in js
    assert "attnRank(i) + i.title" in js
