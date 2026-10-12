"""Visibility-aware polling on the phone page.

The rest of the progressive-disclosure UX round moved to vitest
(frontend/src/__tests__/uxRound.test.ts), against the source modules. What
stays here is ``mobile.js``: it is hand-written and served as-is, not built
from ``frontend/src``, so the served file is the thing to check.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.web import server

client = TestClient(server.app)


def test_mobile_polls_back_off_when_tab_hidden():
    # The phone's poll is a self-rescheduling timeout that stretches while the
    # tab is hidden, never a fixed 4s interval.
    mjs = client.get("/mobile.js").text
    assert "document.hidden" in mjs
    assert "setInterval(poll, 4000)" not in mjs
