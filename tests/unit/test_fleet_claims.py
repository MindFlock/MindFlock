"""Fleet ticket claims (:mod:`backend.web.core.fleet_claims`).

One ticket, one device: every start path (Intake's Begin work, the ingestion
pipeline, team runs, the agent tools) asks the user's other paired devices
whether they already hold it. Devices are faked at the ``remote`` seam
(``connected_devices`` + ``get_json``); the engine, pending table and ledger
are the real modules on test-owned state.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.ticket_ingestion import orchestrator
from backend.ticket_ingestion import state as ledger
from backend.ticket_ingestion.models import ProcessingRecord
from backend.web import server
from backend.web.core import fleet_claims, remote
from backend.web.core import pending as pending_mod
from backend.web.core import ticket_start

SELF = "laptop"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setattr(server.ENGINE, "instances", {})
    monkeypatch.setattr(pending_mod, "_PENDING", {})
    monkeypatch.setattr(ticket_start, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        remote, "self_identity", lambda: {"key": SELF, "host": "Laptop"}
    )
    monkeypatch.setattr(remote, "connected_devices", lambda: [])
    fleet_claims.clear_cache()
    yield tmp_path
    fleet_claims.clear_cache()


def _peers(monkeypatch, answers: dict):
    """``answers``: device key -> (status, body); counts the calls made."""
    calls = []
    devs = [
        {
            "key": k,
            "host": k.replace("-", " ").title(),
            "instances": v[2] if len(v) > 2 else [],
        }
        for k, v in answers.items()
    ]
    monkeypatch.setattr(remote, "connected_devices", lambda: [dict(d) for d in devs])

    async def fake_get_json(dev, path, timeout=3.0):
        calls.append((dev["key"], path))
        status, body = answers[dev["key"]][:2]
        return status, body

    monkeypatch.setattr(remote, "get_json", fake_get_json)
    return calls


def _claims(**slugs):
    return 200, {"device": "x", "claims": slugs}


class _Inst:
    def __init__(self, title, branch, created=None):
        self.Title = title
        self.Branch = branch
        self.CreatedAt = created or datetime.now(timezone.utc)


def _mark(root, slug, status="in_flight", at=None, reserved_by=None):
    ledger.record_processed_story(
        root,
        ProcessingRecord(
            story_id=slug,
            branch=slug,
            status=status,
            processed_at=at or datetime.now(timezone.utc),
        ),
    )
    if reserved_by:
        data = ledger._read_state(root)
        data["processed_stories"][-1]["reserved_by"] = reserved_by
        ledger._write_state(root, data)


# --------------------------------------------------------------------------- #
# what this device advertises
# --------------------------------------------------------------------------- #
def test_local_claims_lists_ticket_sessions_starts_and_markers(_env):
    server.ENGINE.instances["sc-1"] = _Inst("sc-1", "feature/sc-1/fix")
    server.ENGINE.instances["notes"] = _Inst("notes", "main")  # not a ticket
    server.ENGINE.instances["pr-9"] = _Inst("pr-9", "feature/pr-9/x")  # a PR session
    pending_mod.add("sc-2", "tix")
    pending_mod.add("pr-3", "pr")
    _mark(_env, "sc-3")
    _mark(_env, "sc-4", reserved_by="run:r1")
    _mark(_env, "sc-5", status="completed")
    got = fleet_claims.local_claims()
    assert {k: v["kind"] for k, v in got.items()} == {
        "sc-1": "session",
        "sc-2": "starting",
        "sc-3": "in_flight",
        "sc-4": "reserved",
    }
    assert all(v["since"] > 0 for v in got.values())


def test_an_old_marker_with_no_session_is_not_advertised(_env):
    """A launch that died without cleaning up must not hold a ticket
    fleet-wide forever; a team run's reservation is held on purpose."""
    old = datetime.now(timezone.utc) - timedelta(hours=3)
    _mark(_env, "sc-1", at=old)
    _mark(_env, "sc-2", at=old, reserved_by="run:r1")
    assert set(fleet_claims.local_claims()) == {"sc-2"}


def test_claims_route_answers_device_and_claims():
    server.ENGINE.instances["sc-7"] = _Inst("sc-7", "feature/sc-7/x")
    body = TestClient(server.app).get("/api/tickets/claims").json()
    assert body["device"] == SELF
    assert body["claims"]["sc-7"]["kind"] == "session"


# --------------------------------------------------------------------------- #
# asking the fleet
# --------------------------------------------------------------------------- #
def test_no_devices_means_nobody_holds_it():
    assert asyncio.run(fleet_claims.holder("sc-1")) is None


def test_a_peer_session_is_found_and_described(monkeypatch):
    _peers(
        monkeypatch, {"mac-mini": _claims(**{"sc-1": {"kind": "session", "since": 5}})}
    )
    got = asyncio.run(fleet_claims.holder("sc-1"))
    assert got["device"] == "mac-mini" and got["kind"] == "session"
    assert fleet_claims.describe(got) == "running on Mac Mini"
    assert asyncio.run(fleet_claims.holder("sc-2")) is None


def test_an_unreachable_device_holds_nothing(monkeypatch):
    _peers(monkeypatch, {"rig": (0, None)})
    assert asyncio.run(fleet_claims.holder("sc-1")) is None


def test_a_device_without_the_claims_route_is_read_from_its_sessions(monkeypatch):
    rows = [
        {"title": "sc-1", "branch": "feature/sc-1/x"},
        {"title": "scratch", "branch": "main"},
        {"title": "sc-9", "branch": "feature/sc-9/x", "device": "third"},  # echoed
    ]
    _peers(monkeypatch, {"old-mac": (404, None, rows)})
    assert asyncio.run(fleet_claims.holder("sc-1"))["kind"] == "session"
    assert asyncio.run(fleet_claims.holder("scratch")) is None
    assert asyncio.run(fleet_claims.holder("sc-9")) is None


def test_listing_reuses_answers_but_a_launch_asks_again(monkeypatch):
    calls = _peers(monkeypatch, {"rig": _claims()})
    asyncio.run(fleet_claims.fleet_claims())
    asyncio.run(fleet_claims.fleet_claims())
    assert len(calls) == 1
    asyncio.run(fleet_claims.holder("sc-1", fresh=True))
    assert len(calls) == 2


# --------------------------------------------------------------------------- #
# the race: two devices marking the same ticket at once
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "theirs, ours, blocked",
    [
        (100.0, 200.0, True),  # they marked first: we back off
        (300.0, 200.0, False),  # we marked first: they back off, we go
    ],
)
def test_marker_race_older_marker_wins(monkeypatch, theirs, ours, blocked):
    _peers(
        monkeypatch,
        {"rig": _claims(**{"sc-1": {"kind": "in_flight", "since": theirs}})},
    )
    got = asyncio.run(fleet_claims.holder("sc-1", own_since=ours, fresh=True))
    assert (got is not None) is blocked


def test_marker_race_exact_tie_breaks_on_device_key(monkeypatch):
    # "laptop" < "rig": the laptop keeps it, and the rig (asking the same
    # question with the keys swapped) would back off.
    _peers(
        monkeypatch, {"rig": _claims(**{"sc-1": {"kind": "in_flight", "since": 100.0}})}
    )
    assert asyncio.run(fleet_claims.holder("sc-1", own_since=100.0)) is None
    _peers(
        monkeypatch,
        {"alpha": _claims(**{"sc-1": {"kind": "in_flight", "since": 100.0}})},
    )
    fleet_claims.clear_cache()
    assert asyncio.run(fleet_claims.holder("sc-1", own_since=100.0)) is not None


def test_a_peer_session_or_start_always_wins_the_race(monkeypatch):
    _peers(
        monkeypatch,
        {
            "rig": _claims(**{"sc-1": {"kind": "session", "since": 999.0}}),
            "mac": _claims(**{"sc-2": {"kind": "starting", "since": 999.0}}),
        },
    )
    assert asyncio.run(fleet_claims.holder("sc-1", own_since=1.0)) is not None
    assert asyncio.run(fleet_claims.holder("sc-2", own_since=1.0)) is not None


# --------------------------------------------------------------------------- #
# the start paths
# --------------------------------------------------------------------------- #
def test_intake_rows_held_elsewhere_are_marked_and_not_eligible(monkeypatch):
    _peers(
        monkeypatch, {"mac-mini": _claims(**{"sc-1": {"kind": "session", "since": 5}})}
    )
    rows = [
        {"session": "sc-1", "eligible": True, "reasons": []},
        {"session": "sc-2", "eligible": True, "reasons": []},
        {"session": "sc-3", "has_session": True, "eligible": False, "reasons": []},
    ]
    asyncio.run(server._annotate_fleet_holders(rows))
    assert rows[0]["elsewhere"]["label"] == "Mac Mini"
    assert rows[0]["eligible"] is False
    assert rows[0]["reasons"] == ["running on Mac Mini"]
    assert "elsewhere" not in rows[1] and rows[1]["eligible"] is True
    assert "elsewhere" not in rows[2]


def test_fleet_holder_route_is_local_only(monkeypatch):
    monkeypatch.setattr(remote, "remote_control_enabled", lambda: True)
    c = TestClient(server.app)
    r = c.get(
        "/api/tickets/fleet-holder?slug=sc-1", headers={"x-mindflock-remote": "rig"}
    )
    assert r.status_code == 403
    assert c.get("/api/tickets/fleet-holder?slug=sc-1").json() == {
        "holder": None,
        "reason": "",
    }


def test_fleet_holder_route_applies_the_tie_break(monkeypatch):
    _peers(
        monkeypatch, {"rig": _claims(**{"sc-1": {"kind": "in_flight", "since": 100.0}})}
    )
    c = TestClient(server.app)
    assert (
        c.get("/api/tickets/fleet-holder?slug=sc-1&since=50").json()["holder"] is None
    )
    body = c.get("/api/tickets/fleet-holder?slug=sc-1&since=150").json()
    assert body["holder"]["device"] == "rig"
    assert body["reason"] == "starting on Rig"


def test_begin_work_is_a_409_when_another_device_has_it(monkeypatch):
    from tests.unit.test_mcp_ship_routes import _stub_ticket

    _stub_ticket(monkeypatch)
    _peers(
        monkeypatch,
        {"mac-mini": _claims(**{"sc-23588": {"kind": "session", "since": 5}})},
    )
    with pytest.raises(ticket_start.LaunchError) as err:
        asyncio.run(ticket_start.launch("sc", "23588"))
    assert err.value.status == 409
    assert "running on Mac Mini" in err.value.body["error"]
    assert not pending_mod.has("sc-23588")


def test_pipeline_backs_off_and_hands_its_marker_back(monkeypatch, _env):
    """process_story writes its marker, asks the server, and on a holder
    removes the marker and launches nothing."""
    monkeypatch.setattr(orchestrator, "_STATE_DIR", _env)
    monkeypatch.setattr(
        orchestrator, "_fleet_holder", lambda slug, since: "running on Rig"
    )
    orch = orchestrator.PipelineOrchestrator.__new__(orchestrator.PipelineOrchestrator)
    orch._assignee_filter = SimpleNamespace(is_assigned=lambda s: True)

    async def no_miss(story):
        return ""

    orch._ingest_filter_miss_now = no_miss
    orch._validator = SimpleNamespace(
        validate=lambda s: (_ for _ in ()).throw(AssertionError("launched anyway"))
    )
    from tests._factories import make_ticket

    story = make_ticket(id=77)
    asyncio.run(orch.process_story(story))
    assert ledger.latest_story_entry(_env, story.slug) is None


def test_pipeline_fleet_check_fails_open(monkeypatch):
    monkeypatch.delenv("MINDFLOCK_SERVER_PORT", raising=False)
    assert orchestrator._fleet_holder("sc-1", time.time()) == ""
    monkeypatch.setenv("MINDFLOCK_SERVER_PORT", "1")  # nothing listens there
    assert orchestrator._fleet_holder("sc-1", time.time()) == ""
