"""Scheduled flight plans (docs/flights.md "Scheduled plans").

A plan whose trigger is ``schedule`` fires like a template: the same pure
``plan_tick`` (armed-at, one missed slot, paused, automation device only) over
its own run record ``flights/schedule_runs.json``, a skip while the last
flight is still in the air, and a take-off through the same call ``POST
/api/flights/start`` makes, with ``by="schedule"``. Clock injected, stores in
tmp, the session create stubbed.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from backend.web import server
from backend.web.core import flight_driver as D
from backend.web.core import flights as F
from backend.web.core import template_schedule as ts

client = TestClient(server.app)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("MINDFLOCK_TEMPLATE_RUNS_FILE", str(tmp_path / "tpl_runs.json"))
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    monkeypatch.delenv("CS_WEB_MODE", raising=False)
    monkeypatch.setattr(server.ENGINE, "instances", {})
    # A fresh fire lock per test (it binds to the loop of its first use).
    monkeypatch.setattr(ts, "_FIRE_LOCK", None)
    events: list = []
    monkeypatch.setattr(
        server._events.BUS, "emit", lambda ev, **kw: events.append((ev, kw))
    )
    yield events


def _at(day: int, hh: int, mm: int = 0) -> float:
    return time.mktime(dt.datetime(2026, 10, day, hh, mm).timetuple())


def _scheduled(at="09:00", paused=False, repo="/repo", **trig) -> dict:
    return F.save_plan(
        {
            "name": "Nightly",
            "trigger": {
                "kind": "schedule",
                "schedule": {"at": at, "days": "daily"},
                "paused": paused,
                **trig,
            },
            "session": {"repo_path": repo},
            "steps": [{"kind": "agent", "config": {"prompt": "Tidy {subject}"}}],
        }
    )


def _starter(calls: list, session="nightly-1"):
    async def start(body):
        calls.append(body)
        plan = F.get_plan(body["plan_id"])
        return F.take_off(plan, session=session, subject=body["subject"], by=body["by"])

    return start


def _tick(now, here=True, start=None):
    return asyncio.run(ts.tick_flights(now=now, here=here, start=start))


def test_rows_are_the_scheduled_plans_in_template_shape():
    p = _scheduled(paused=True)
    F.save_plan({"name": "Manual", "steps": [{"kind": "notify"}]})
    rows = ts.flight_plan_rows(F.list_plans())
    # builtin:schedule has a schedule trigger but no schedule: a row all the same
    by_id = {r["name"]: r for r in rows}
    # Nightly chores (round 15) is seeded with a daily schedule, paused.
    assert set(by_id) == {"builtin:schedule", "builtin:nightly", p["id"]}
    assert by_id[p["id"]]["schedule"] == {
        "at": "09:00",
        "days": "daily",
        "paused": True,
    }
    assert by_id["builtin:schedule"]["schedule"] is None


def test_plan_tick_arms_then_fires_with_by_schedule(tmp_path):
    p = _scheduled()
    calls: list = []
    start = _starter(calls)
    # First sight arms it: 08:00 sees nothing missed.
    assert _tick(_at(29, 8), start=start) == []
    assert _tick(_at(29, 9, 1), start=start) == [p["id"]]
    (body,) = calls
    assert body["plan_id"] == p["id"] and body["by"] == "schedule"
    assert body["subject"] == {"kind": "schedule", "title": "Nightly"}
    # The same slot never fires twice.
    assert _tick(_at(29, 9, 2), start=start) == []
    runs = ts.load_runs(ts.flight_runs_path())
    rec = runs[p["id"]]
    assert rec["last_session"] == "nightly-1" and rec["last_flight"].startswith("f_")
    assert rec["history"][0]["trigger"] == "schedule"
    # Its own file, beside the flights store — never the templates' record.
    assert ts.flight_runs_path() == str(tmp_path / "flights" / "schedule_runs.json")
    assert not os.path.exists(tmp_path / "tpl_runs.json")


def test_a_busy_previous_flight_skips_the_slot(_isolated):
    events = _isolated
    p = _scheduled()
    calls: list = []
    start = _starter(calls)
    _tick(_at(28, 8), start=start)
    _tick(_at(28, 9, 1), start=start)
    assert len(calls) == 1  # its flight is still running
    events.clear()
    assert _tick(_at(29, 9, 1), start=start) == []
    assert len(calls) == 1
    rec = ts.load_runs(ts.flight_runs_path())[p["id"]]
    assert rec["last_skip"] == "the previous flight is still in the air"
    assert rec["history"][0]["skipped"] == rec["last_skip"]
    ((ev, kw),) = [e for e in events if e[0] == "flight.changed"]
    assert kw["data"]["plan_id"] == p["id"] and kw["data"]["skipped"]
    assert kw["session"] == "nightly-1"
    # Landed: the next slot takes off again.
    F.mutate(rec["last_flight"], lambda f: F.op_cancel(f, "done with it"))
    assert _tick(_at(30, 9, 1), start=start) == [p["id"]]


def test_paused_and_other_devices_fire_nothing():
    _scheduled(paused=True)
    calls: list = []
    _tick(_at(29, 8), start=_starter(calls))
    assert _tick(_at(29, 9, 1), start=_starter(calls)) == []
    F.save_plan(
        dict(
            F.list_plans()[-1],
            trigger={"kind": "schedule", "schedule": {"at": "10:00"}},
        )
    )
    _tick(_at(29, 9, 30), here=False, start=_starter(calls))
    assert _tick(_at(29, 10, 1), here=False, start=_starter(calls)) == []
    assert calls == []


def test_fire_goes_through_the_flights_start_call(monkeypatch):
    """The default starter is flight_driver.start — POST /api/flights/start's
    own body — so the scheduled flight's session is created from the plan."""
    p = _scheduled()
    made: list = []

    async def _create(payload):
        made.append(payload)
        return "nightly-oct29"

    monkeypatch.setattr(D, "_create_session", _create)
    status, body = asyncio.run(ts.fire_flight(p, "schedule", now=_at(29, 9)))
    assert status == 202
    f = body["flight"]
    assert f["by"] == "schedule" and f["session"] == "nightly-oct29"
    assert f["subject"]["kind"] == "schedule"
    assert made == [{"repo_path": "/repo", "prompt": "Tidy Nightly"}]


def test_a_refused_take_off_is_a_failed_run(_isolated):
    events = _isolated
    p = _scheduled(repo="")
    status, body = asyncio.run(ts.fire_flight(p, "schedule", now=_at(29, 9)))
    assert status == 400 and body["error"] == "no_repo"
    rec = ts.load_runs(ts.flight_runs_path())[p["id"]]
    assert rec["last_error"] and rec["history"][0]["error"] == rec["last_error"]
    assert any(
        ev == "flight.changed" and kw["data"].get("error") == "no_repo"
        for ev, kw in events
    )


def test_the_plans_route_carries_runs_for_scheduled_plans():
    p = _scheduled()
    calls: list = []
    _tick(time.time() - 60, start=_starter(calls))
    plans = {x["id"]: x for x in client.get("/api/flights/plans").json()["plans"]}
    runs = plans[p["id"]]["runs"]
    assert runs["next_at"] and runs["next_at"] > time.time()
    assert runs["schedule_label"]
    for key in ("last_run_at", "last_session", "last_error", "history", "runs_here"):
        assert key in runs
    assert "runs" not in plans["builtin:ticket"]
    # The editor's previews stay the templates' route.
    r = client.get("/api/templates/cron-preview", params={"expr": "0 9 * * 1-5"})
    assert r.status_code == 200 and r.json()["ok"]


def test_templates_keep_their_own_record(tmp_path):
    ts.record_run("tpl", {"at": 1.0, "trigger": "manual"}, last_run_at=1.0)
    assert ts.load_runs()["tpl"]["last_run_at"] == 1.0
    assert ts.load_runs(ts.flight_runs_path()) == {}
    assert json.loads((tmp_path / "tpl_runs.json").read_text())["runs"]["tpl"]


def test_one_plans_crash_never_costs_the_rest_of_the_tick():
    a = _scheduled()
    b = F.save_plan(dict(F.get_plan(a["id"]), id=None, name="Second"))
    calls: list = []
    good = _starter(calls)

    async def start(body):
        if body["plan_id"] == a["id"]:
            raise RuntimeError("the engine fell over")
        return await good(body)

    _tick(_at(29, 8), start=start)
    fired = _tick(_at(29, 9, 1), start=start)
    assert sorted(fired) == sorted([a["id"], b["id"]])
    assert [c["plan_id"] for c in calls] == [b["id"]]
    runs = ts.load_runs(ts.flight_runs_path())
    assert runs[a["id"]]["last_error"] == "the engine fell over"
    assert runs[a["id"]]["history"][0]["error"] == "the engine fell over"
    assert runs[b["id"]]["last_flight"]
