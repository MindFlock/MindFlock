"""MCP ``whoami`` / ``spawn_session`` / ``start_team_run`` carry the launch
gate's capacity (``GET /api/memory/capacity``), so an orchestrating agent
plans its worker count from how many sessions the machine has memory for —
and a server without the route simply leaves the field out."""

from __future__ import annotations

from tests.unit._mcp_fakes import FakeCtx
from tests.unit.test_mcp_tools import _tree
from tests.unit._mcp_fakes import make_box

CAP = {
    "room": 2,
    "session_mb": 950,
    "short_mb": 0,
    "available_mb": 9000,
    "reserve_mb": 3000,
    "hint": "room for 2 more sessions now (~950 MB each); more will queue",
}


def test_whoami_carries_the_capacity():
    box, api, clock = make_box(_tree())
    api.top_routes[("GET", "/api/memory/capacity")] = dict(CAP)
    out = box.whoami({}, FakeCtx(clock))
    assert out["capacity"] == {
        "room": 2,
        "session_mb": 950,
        "hint": "room for 2 more sessions now (~950 MB each); more will queue",
    }


def test_an_older_server_has_no_capacity():
    box, _api, clock = make_box(_tree())
    assert "capacity" not in box.whoami({}, FakeCtx(clock))


def test_a_spawn_reports_the_room_left():
    box, api, clock = make_box(_tree())
    api.created_status = "running"
    api.top_routes[("GET", "/api/memory/capacity")] = dict(CAP, room=1)
    out = box.spawn_session({"prompt": "p", "wait_ready": False}, FakeCtx(clock))
    assert out["capacity"]["room"] == 1 and out["capacity"]["session_mb"] == 950


def test_a_queued_spawn_says_what_one_more_needs():
    box, api, clock = make_box(_tree())

    def queued(payload, query):
        return {
            "title": payload["title"],
            "queued": True,
            "status": "queued: waiting for memory",
            "reason": "one more session needs ~0.9 GB",
            "position": 1,
        }

    api.top_routes[("POST", "/api/instances")] = queued
    api.top_routes[("GET", "/api/memory/capacity")] = dict(
        CAP, room=0, short_mb=400, hint="no room"
    )
    out = box.spawn_session({"prompt": "p"}, FakeCtx(clock))
    assert out["status"] == "queued: waiting for memory"
    assert out["capacity"] == {
        "room": 0,
        "session_mb": 950,
        "short_mb": 400,
        "hint": "no room",
    }
    assert "One more session needs ~950 MB; 400 MB more would have to be free." in (
        out["hint"]
    )
