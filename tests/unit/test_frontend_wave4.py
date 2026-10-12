"""Frontend wave 4 (J5 budget guardrail): the server side of the budget toast.

The palette, context line, presets, budget toast and row-layout checks of this
wave are pinned against their source modules in
frontend/src/__tests__/wave4Wiring.test.ts. What stays here is the notify rule
the server ships for the event the frontend toasts.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.web import server

client = TestClient(server.app)


def test_notify_config_covers_budget_exceeded():
    rules = client.get("/api/notify/config").json()["rules"]
    budget = [r for r in rules if r["event"] == "session.budget_exceeded"]
    assert len(budget) == 1
    # No old/new constraint: the event itself is the signal.
    assert budget[0]["old"] is None and budget[0]["new"] is None
    assert "{session}" in budget[0]["title"]
