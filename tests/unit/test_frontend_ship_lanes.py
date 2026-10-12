"""Ship lanes, UI part 2: the backend↔frontend contract.

The structural checks (the rail's run group headers, queued lines and
lane-first status lines, their ⋯ menu, the bell's waiting rows and run rows,
and the Outbox's absence) are pinned against the source in
frontend/src/__tests__/shipLanesWiring.test.ts. What stays here is the half
only the server can show: every route the ship-lanes UI calls is in the
served bundle AND exists on the server.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.web import server

client = TestClient(server.app)

#: (a literal the bundle sends, the server route it reaches). The run routes
#: are built as ``"/api/runs/" + id + <suffix>`` (lib/runsApi.ts runPath /
#: taskPath), so the suffix is what the bundle carries.
_CALLS = [
    ('"/api/outbox?group=all"', "/api/outbox"),
    ('"/api/runs/"', "/api/runs/{run_id}"),
    ('"/ship-now"', "/api/instances/{title}/ship-now"),
    ('"/resume"', "/api/runs/{run_id}/resume"),
    ('"/cancel"', "/api/runs/{run_id}/cancel"),
    ('"/plan/approve"', "/api/runs/{run_id}/plan/approve"),
    ('"/plan/reject"', "/api/runs/{run_id}/plan/reject"),
    ('"/release"', "/api/runs/{run_id}/release"),
    ('"/check"', "/api/runs/{run_id}/check"),
    ('"/lead/branch"', "/api/runs/{run_id}/lead/branch"),
    ('"start-now"', "/api/runs/{run_id}/tasks/{task_id}/start-now"),
    ('"skip"', "/api/runs/{run_id}/tasks/{task_id}/skip"),
]


@pytest.mark.parametrize(("literal", "route"), _CALLS, ids=[r for _, r in _CALLS])
def test_every_route_the_ship_lanes_ui_calls_exists(literal, route):
    assert literal in client.get("/app.js").text
    assert route in server.app.openapi()["paths"]
