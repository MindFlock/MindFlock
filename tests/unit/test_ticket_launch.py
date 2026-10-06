"""``ticket_start.launch`` — the ticket start, callable without HTTP.

``POST /api/tickets/start`` is a thin wrapper around it (its own tests live in
test_mcp_ship_routes / test_ticket_force_start and pass unchanged); a team run
calls it directly for each ticket task. These pin the knobs only the run uses:
a title/branch override (retry fresh), ``depth="off"`` (the run arms its own
lane), the run's brief appended to the prompt, and the ledger reservation a
queued ticket holds before it launches.

The live engine is the developer's real state.json: every test owns
``ENGINE.instances`` and nothing is provisioned.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from backend.ticket_ingestion import state as ledger
from backend.web import server
from backend.web.core import pending as pending_mod
from backend.web.core import ticket_start
from tests.unit.test_mcp_ship_routes import _Inst, _stub_ticket


class _Stop(Exception):
    pass


@pytest.fixture
def launch_env(monkeypatch):
    instances: dict = {}
    monkeypatch.setattr(server.ENGINE, "instances", instances)
    monkeypatch.setattr(server.ENGINE, "save", lambda **kw: None)
    monkeypatch.setattr(pending_mod, "_PENDING", {})
    tasks: list = []
    seen: list = []
    armed: list = []
    events: list = []
    monkeypatch.setattr(server, "_register_task", tasks.append)
    monkeypatch.setattr(
        server, "_arm_intake_autopilot", lambda *a, **k: armed.append((a, k))
    )

    def _new(opts):
        seen.append(opts)
        inst = _Inst(opts.title)

        def _start(*_a):
            raise _Stop("not in a test")

        inst.Start = _start
        inst.SetStatus = lambda s: None
        inst.ExtraEnv = {}
        return inst

    monkeypatch.setattr(server.session, "NewInstance", _new)
    monkeypatch.setattr(server, "_seed_event_snapshot", lambda t: None)
    monkeypatch.setattr(server, "_drop_failed_start", lambda t, i: None)
    monkeypatch.setattr(
        server._events.BUS, "emit", lambda ev, **kw: events.append((ev, kw))
    )
    _stub_ticket(monkeypatch)
    yield SimpleNamespace(
        instances=instances, tasks=tasks, seen=seen, armed=armed, events=events
    )
    for coro in tasks:
        coro.close()


def _drain(tasks):
    while tasks:
        asyncio.run(tasks.pop(0))


def test_the_route_and_a_direct_call_answer_the_same(launch_env):
    body = asyncio.run(ticket_start.launch("sc", "23588"))
    assert body == {"started": True, "title": "sc-23588"}
    _drain(launch_env.tasks)
    (opts,) = launch_env.seen
    assert opts.title == "sc-23588"
    assert opts.new_branch == "feature/sc-23588/fix-it"
    assert opts.prompt == "TICKET TEXT"


def test_a_title_and_branch_override_start_a_fresh_attempt(launch_env):
    """Retry fresh: a new title on a new branch, the old ones kept."""
    body = asyncio.run(
        ticket_start.launch(
            "sc", "23588", title="sc-23588-2", branch="feature/sc-23588/fix-it-2"
        )
    )
    assert body["title"] == "sc-23588-2"
    assert pending_mod.has("sc-23588-2")
    _drain(launch_env.tasks)
    (opts,) = launch_env.seen
    assert (opts.title, opts.new_branch) == (
        "sc-23588-2",
        "feature/sc-23588/fix-it-2",
    )


def test_depth_off_leaves_arming_to_the_caller(launch_env):
    asyncio.run(ticket_start.launch("sc", "23588", depth="off"))
    ((args, _kw),) = launch_env.armed
    assert args[0] == "sc-23588" and args[1] == "off"


def test_the_run_brief_is_appended_after_the_ticket_and_tagged(launch_env):
    asyncio.run(
        ticket_start.launch(
            "sc", "23588", extra_prompt="MindFlock runs this.", run_id="r_x"
        )
    )
    _drain(launch_env.tasks)
    (opts,) = launch_env.seen
    assert opts.prompt == "TICKET TEXT\n\nMindFlock runs this."
    created = [kw for ev, kw in launch_env.events if ev == "session.created"]
    assert created and created[0]["data"]["run"] == "r_x"


def test_a_live_title_is_a_409(launch_env):
    launch_env.instances["sc-23588"] = _Inst("sc-23588")
    with pytest.raises(ticket_start.LaunchError) as err:
        asyncio.run(ticket_start.launch("sc", "23588"))
    assert err.value.status == 409
    assert err.value.body["title"] == "sc-23588"


def test_an_unknown_source_is_a_404(launch_env, monkeypatch):
    async def _missing(source, tid):
        raise LookupError("No ticketing source 'x' is configured")

    monkeypatch.setattr(ticket_start, "find_ticket", _missing)
    with pytest.raises(ticket_start.LaunchError) as err:
        asyncio.run(ticket_start.launch("x", "1"))
    assert err.value.status == 404
    assert not pending_mod.has("sc-23588")


class TestReservation:
    @pytest.fixture(autouse=True)
    def _ledger(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ticket_start, "_REPO_ROOT", tmp_path)
        self.root = tmp_path

    def _statuses(self):
        return ledger.load_processed_story_statuses(self.root)

    def test_reserve_marks_in_flight_once(self):
        ticket_start.reserve("PAY-412")
        ticket_start.reserve("PAY-412")
        assert self._statuses() == {"PAY-412": "in_flight"}
        data = json.loads((self.root / "state.json").read_text())
        assert len(data["processed_stories"]) == 1

    def test_release_hands_a_never_started_ticket_back(self):
        ticket_start.reserve("PAY-412")
        assert ticket_start.release_reservation("PAY-412") is True
        assert self._statuses() == {}

    def test_release_never_erases_a_real_outcome(self):
        ticket_start.reserve("PAY-412")
        ledger.update_processed_story(self.root, "PAY-412", status="completed")
        assert ticket_start.release_reservation("PAY-412") is False
        assert self._statuses() == {"PAY-412": "completed"}
