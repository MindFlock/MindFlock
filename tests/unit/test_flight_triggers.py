"""Flight triggers that watch the outside world (``backend/web/core/
flight_triggers.py``, docs/flights.md "Round 15 → Triggers"): the plan's
``main_push`` / ``main_red`` / ``hook`` triggers and their fields, the state
file, the fire rules against a real (tmp) git repo with a faked checks
reader, quiet minutes, the hook route on a bare app, and the auth gate's
public regex."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.web import server
from backend.web.core import auth
from backend.web.core import flight_driver as D
from backend.web.core import flight_triggers as T
from backend.web.core import flights as F
from backend.web.core import template_schedule as ts
from backend.web.routes import flights as routes

_GIT_IDENT = dict(
    GIT_AUTHOR_NAME="t",
    GIT_AUTHOR_EMAIL="t@x",
    GIT_COMMITTER_NAME="t",
    GIT_COMMITTER_EMAIL="t@x",
)
# Never let git be pointed anywhere but the ``-C`` folder. This module used to
# snapshot ``dict(os.environ, ...)`` at IMPORT — before conftest's per-test
# scrub — so under a git hook (which exports GIT_DIR/GIT_INDEX_FILE) every
# ``_git(tmp, "init")`` re-initialised the real repo (core.bare = true) and
# ``_git(tmp, "add", "-A")`` rewrote its index. tests/unit/test_git_env_isolation.py
# pins that this stays fixed.
_GIT_REDIRECT = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
)


def _git_env() -> dict:
    """The env for one git call: built per call, from the current (scrubbed)
    environment, minus anything that redirects git."""
    env = dict(os.environ, **_GIT_IDENT)
    for var in _GIT_REDIRECT:
        env.pop(var, None)
    return env


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    monkeypatch.delenv("CS_WEB_MODE", raising=False)
    monkeypatch.setattr(server.ENGINE, "instances", {})
    monkeypatch.setattr(ts, "_FIRE_LOCK", None)
    monkeypatch.setattr(T, "_last_tick", 0.0)
    events: list = []
    monkeypatch.setattr(
        server._events.BUS, "emit", lambda ev, **kw: events.append((ev, kw))
    )
    yield events


def _git(folder, *args) -> str:
    cp = subprocess.run(
        ["git", "-C", str(folder), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_git_env(),
    )
    return cp.stdout.decode().strip()


def _commit(folder, name: str) -> str:
    (folder / name).write_text(name)
    _git(folder, "add", "-A")
    _git(folder, "commit", "-q", "-m", name)
    return _git(folder, "rev-parse", "HEAD")


@pytest.fixture
def repos(tmp_path):
    """``(upstream, watch)``: a repo with a ``main`` and a clone of it (the
    plan's ``trigger.repo``; its origin is the upstream)."""
    up = tmp_path / "upstream"
    up.mkdir()
    _git(up, "init", "-q", "-b", "main")
    _commit(up, "a.txt")
    watch = tmp_path / "watch"
    _git(tmp_path, "clone", "-q", str(up), str(watch))
    return up, watch


def _plan(kind: str, repo: str, **trig) -> dict:
    return F.save_plan(
        {
            "name": "Guard " + kind,
            "trigger": {"kind": kind, "repo": repo, **trig},
            "session": {"repo_path": repo},
            "steps": [{"kind": "agent", "config": {"prompt": "Look: {subject}"}}],
        }
    )


def _starter(calls: list, session="guard-1"):
    async def start(body):
        calls.append(body)
        plan = F.get_plan(body["plan_id"])
        return F.take_off(plan, session=session, subject=body["subject"], by=body["by"])

    return start


def _reader(answers: list, calls: list):
    async def read(repo, sha):
        calls.append(sha)
        return answers.pop(0) if len(answers) > 1 else answers[0]

    return read


def _tick(now, start=None, reader=None):
    return asyncio.run(T.tick(now=now, start=start, checks_reader=reader))


def _state() -> dict:
    return json.loads(open(T.state_path(), encoding="utf-8").read())


# --------------------------------------------------------------------------- #
# The plan's trigger: kinds, fields, the token
# --------------------------------------------------------------------------- #
def test_trigger_kinds_grew_and_the_message_names_them():
    assert F.TRIGGER_KINDS[-3:] == ("main_push", "main_red", "hook")
    assert F.MAIN_TRIGGERS == ("main_push", "main_red")
    with pytest.raises(F.PlanError) as e:
        F.normalize_plan({"name": "x", "trigger": {"kind": "cron"}, "steps": []})
    assert e.value.field == "trigger.kind" and "main_push" in e.value.message


@pytest.mark.parametrize(
    "repo, why",
    [
        ("", "needed"),
        ("relative/path", "absolute"),
        ("/nowhere/at/all", "not a git repository"),
    ],
)
def test_main_triggers_need_an_absolute_git_repo(repo, why):
    with pytest.raises(F.PlanError) as e:
        F.normalize_plan(
            {"name": "x", "trigger": {"kind": "main_push", "repo": repo}, "steps": []}
        )
    assert e.value.field == "trigger.repo" and why in e.value.message
    # Lenient (a synced plan) keeps the text and never raises.
    plan = F.normalize_plan(
        {"name": "x", "trigger": {"kind": "main_red", "repo": repo}}, strict=False
    )
    assert plan["trigger"]["kind"] == "main_red" and plan["trigger"]["repo"] == repo


def test_a_plain_folder_is_not_a_repo(tmp_path):
    with pytest.raises(F.PlanError):
        F.normalize_plan(
            {
                "name": "x",
                "trigger": {"kind": "main_push", "repo": str(tmp_path)},
                "steps": [],
            }
        )


def test_trigger_fields_are_stored_with_their_defaults(repos):
    _up, watch = repos
    p = _plan("main_push", str(watch))
    assert p["trigger"] == {
        "kind": "main_push",
        "schedule": None,
        "paused": False,
        "repo": str(watch),
        "quiet_minutes": 60,
        "token": "",
    }
    p2 = _plan("main_red", str(watch), quiet_minutes=5)
    assert p2["trigger"]["quiet_minutes"] == 5
    manual = F.save_plan({"name": "m", "steps": [{"kind": "agent"}]})
    assert manual["trigger"]["token"] == "" and manual["trigger"]["repo"] == ""


def test_a_hook_plan_mints_a_token_on_save_and_keeps_it():
    p = F.save_plan(
        {"name": "h", "trigger": {"kind": "hook"}, "steps": [{"kind": "agent"}]}
    )
    tok = p["trigger"]["token"]
    assert len(tok) == 32 and int(tok, 16) >= 0
    # Saved again without the token: the stored one stays.
    again = F.save_plan(
        {
            "id": p["id"],
            "name": "h2",
            "trigger": {"kind": "hook"},
            "steps": [{"kind": "agent"}],
        }
    )
    assert again["trigger"]["token"] == tok
    # Saved with it: kept; a token of another shape is dropped and re-minted.
    same = F.save_plan({**again, "trigger": {**again["trigger"], "token": tok}})
    assert same["trigger"]["token"] == tok
    odd = F.save_plan(
        {
            "name": "o",
            "trigger": {"kind": "hook", "token": "nope"},
            "steps": [{"kind": "agent"}],
        }
    )
    assert len(odd["trigger"]["token"]) == 32 and odd["trigger"]["token"] != "nope"
    # A synced write (put_plan) mints one too.
    synced = F.put_plan(
        "p_sync",
        {"name": "s", "trigger": {"kind": "hook"}, "steps": [{"kind": "agent"}]},
    )
    assert len(synced["trigger"]["token"]) == 32
    assert F.get_plan("p_sync")["trigger"]["token"] == synced["trigger"]["token"]


# --------------------------------------------------------------------------- #
# The state file and the fire rules
# --------------------------------------------------------------------------- #
def test_first_sight_arms_without_firing(repos):
    up, watch = repos
    p = _plan("main_push", str(watch))
    calls: list = []
    assert _tick(1000.0, _starter(calls)) == []
    assert calls == []
    st = _state()
    assert set(st) == {p["id"]}
    assert st[p["id"]]["sha"] == _git(up, "rev-parse", "HEAD")
    assert st[p["id"]]["branch"] == "main" and "fired_at" not in st[p["id"]]
    assert os.path.basename(T.state_path()) == "trigger_state.json"
    assert os.path.dirname(T.state_path()) == F.flights_dir()


def test_main_push_fires_on_a_new_tip(repos):
    up, watch = repos
    p = _plan("main_push", str(watch))
    calls: list = []
    _tick(1000.0, _starter(calls))
    sha = _commit(up, "b.txt")
    assert _tick(1060.0, _starter(calls)) == [p["id"]]
    assert len(calls) == 1
    body = calls[0]
    assert body["plan_id"] == p["id"] and body["by"] == "main_push"
    assert body["subject"] == {"kind": "manual", "title": "main moved to " + sha[:7]}
    row = _state()[p["id"]]
    assert row["sha"] == sha and row["fired_at"] == 1060.0
    f = F.list_flights()[0]
    assert f["by"] == "main_push" and f["subject"]["title"].startswith("main moved to")
    # The schedule record keeps the run under the plan's id, trigger named.
    rec = ts.load_runs(ts.flight_runs_path())[p["id"]]
    assert rec["last_trigger"] == "main_push" and rec["history"][0]["flight"] == f["id"]
    # No further move: nothing fires.
    assert _tick(1120.0, _starter(calls)) == [] and len(calls) == 1


def test_quiet_minutes_hold_a_move_until_the_quiet_is_over(repos):
    up, watch = repos
    p = _plan("main_push", str(watch), quiet_minutes=10)
    calls: list = []
    _tick(0.0, _starter(calls))
    _commit(up, "b.txt")
    assert _tick(60.0, _starter(calls)) == [p["id"]]
    F.delete(F.list_flights()[0]["id"])  # the first flight is out of the way
    c = _commit(up, "c.txt")
    # Inside the quiet: not fired, remembered as pending; the seen sha stays.
    assert _tick(120.0, _starter(calls)) == []
    row = _state()[p["id"]]
    assert row["pending"] == c and row["sha"] != c
    d = _commit(up, "d.txt")
    assert _tick(59.0 + 600.0, _starter(calls)) == []
    # Quiet over: one fire, on the latest tip.
    assert _tick(60.0 + 600.0, _starter(calls), None) == [p["id"]]
    assert calls[-1]["subject"]["title"] == "main moved to " + d[:7]
    assert len(calls) == 2 and "pending" not in _state()[p["id"]]


def test_a_paused_trigger_fetches_and_fires_nothing(repos, monkeypatch):
    up, watch = repos
    _plan("main_push", str(watch), paused=True)
    fetched: list = []
    monkeypatch.setattr(T, "fetch_tip", lambda *a: fetched.append(a) or "")
    assert _tick(0.0) == [] and fetched == []
    assert not os.path.exists(T.state_path())


def test_main_red_fires_once_per_sha_when_the_checks_turn_red(repos):
    up, watch = repos
    p = _plan("main_red", str(watch))
    sha = _git(up, "rev-parse", "HEAD")
    calls: list = []
    reads: list = []
    reader = _reader([("pending", []), ("failed", ["ci", "lint"])], reads)
    assert _tick(0.0, _starter(calls), reader) == []
    assert _state()[p["id"]]["checks"] == {
        "sha": sha,
        "verdict": "pending",
        "names": [],
        "fired": False,
        "read_at": 0.0,
    }
    assert _tick(60.0, _starter(calls), reader) == [p["id"]]
    assert calls[0]["by"] == "main_red"
    assert calls[0]["subject"] == {
        "kind": "manual",
        "title": "main is red at %s (ci, lint)" % sha[:7],
    }
    checks = _state()[p["id"]]["checks"]
    assert checks["fired"] is True and checks["verdict"] == "failed"
    # The verdict is final: no more reads for this sha, no second fire.
    assert _tick(120.0, _starter(calls), reader) == []
    assert _tick(7200.0, _starter(calls), reader) == []
    assert reads == [sha, sha] and len(calls) == 1


def test_main_red_on_a_green_tip_waits_for_the_next(repos):
    up, watch = repos
    p = _plan("main_red", str(watch), quiet_minutes=0)
    calls: list = []
    reads: list = []
    answers = [("ok", []), ("failed", ["tests"])]
    assert _tick(0.0, _starter(calls), _reader(answers, reads)) == []
    assert _tick(60.0, _starter(calls), _reader(answers, reads)) == []  # ok is final
    sha2 = _commit(up, "b.txt")
    assert _tick(120.0, _starter(calls), _reader(answers, reads)) == [p["id"]]
    assert calls[0]["subject"]["title"] == "main is red at %s (tests)" % sha2[:7]
    assert reads[-1] == sha2 and len(reads) == 2


def test_an_unfetchable_repo_is_an_error_row_not_a_crash(tmp_path, repos):
    _up, watch = repos
    p = _plan("main_push", str(watch))
    import shutil

    shutil.rmtree(watch)
    assert _tick(0.0) == []
    assert _state()[p["id"]]["error"] == "no default branch"


def test_one_plans_crash_never_costs_the_rest(repos, monkeypatch):
    up, watch = repos
    bad = _plan("main_push", str(watch))
    good = _plan("main_push", str(watch))
    real = T.default_branch

    def boom(folder):
        if T._boom_once.pop(bad["id"], None):
            raise RuntimeError("kaboom")
        return real(folder)

    T._boom_once = {bad["id"]: True}
    calls: list = []
    _tick(0.0, _starter(calls))
    # Only the good one armed; the bad one has the error noted.
    monkeypatch.setattr(T, "default_branch", boom)
    _commit(up, "b.txt")
    fired = _tick(60.0, _starter(calls))
    assert good["id"] in fired
    st = _state()
    assert st[bad["id"]].get("error") == "kaboom" or st[bad["id"]].get("sha")
    del T._boom_once


def test_rows_of_plans_that_stopped_watching_are_dropped(repos):
    _up, watch = repos
    p = _plan("main_push", str(watch))
    _tick(0.0)
    assert p["id"] in _state()
    F.save_plan({**p, "trigger": {"kind": "manual"}})
    _tick(60.0) if T.watching_plans() else None
    # No watcher left: the tick returns early without touching the file...
    assert p["id"] in _state()
    # ...but with another watcher ticking, the stale row goes.
    _plan("main_red", str(watch))
    asyncio.run(T.tick(now=120.0, checks_reader=_reader([("ok", [])], [])))
    assert p["id"] not in _state()


def test_a_busy_previous_flight_holds_the_move_until_it_lands(repos, _isolated):
    up, watch = repos
    p = _plan("main_push", str(watch))
    calls: list = []
    _tick(0.0, _starter(calls))
    _commit(up, "b.txt")
    assert _tick(60.0, _starter(calls)) == [p["id"]]
    c = _commit(up, "c.txt")
    # The flight from the first fire is still running: the move is HELD —
    # no skipped run, no event, every minute — and fires once it lands.
    assert _tick(60.0 + 3600.0, _starter(calls)) == []
    assert _tick(120.0 + 3600.0, _starter(calls)) == []
    row = _state()[p["id"]]
    assert row["held"] == "the previous flight is still in the air"
    assert row["pending"] == c and row["sha"] != c
    rec = ts.load_runs(ts.flight_runs_path())[p["id"]]
    assert "last_skip" not in rec and len(rec["history"]) == 1
    assert not any(kw["data"].get("skipped") for _ev, kw in _isolated)
    F.delete(F.list_flights()[0]["id"])
    assert _tick(180.0 + 3600.0, _starter(calls, "guard-2")) == [p["id"]]
    assert calls[-1]["subject"]["title"] == "main moved to " + c[:7]
    assert "held" not in _state()[p["id"]] and len(calls) == 2


def test_a_refused_take_off_spends_the_move(repos, _isolated):
    """A plan that cannot take off (no folder) records a failed run once per
    move — never a retry every minute."""
    up, watch = repos
    p = _plan("main_push", str(watch))
    F.save_plan({**p, "session": {**p["session"], "repo_path": ""}})
    _tick(0.0)
    _commit(up, "b.txt")
    assert _tick(60.0) == []
    row = _state()[p["id"]]
    assert row["last_status"] == 400 and "fired_at" not in row
    rec = ts.load_runs(ts.flight_runs_path())[p["id"]]
    assert rec["last_error"] and rec["history"][0]["trigger"] == "main_push"
    assert (
        _tick(120.0) == []
        and len(ts.load_runs(ts.flight_runs_path())[p["id"]]["history"]) == 1
    )


def test_fire_flight_keeps_the_schedule_subject_by_default(repos, monkeypatch):
    """The schedule path is unchanged: no subject → the schedule's own."""
    _up, watch = repos
    p = _plan("main_push", str(watch))
    made: list = []

    async def _create(payload):
        made.append(payload)
        return "s-%d" % len(made)

    monkeypatch.setattr(D, "_create_session", _create)
    status, body = asyncio.run(ts.fire_flight(p, "schedule", now=1.0))
    assert status == 202 and body["flight"]["subject"]["kind"] == "schedule"
    assert body["flight"]["by"] == "schedule"
    status, body = asyncio.run(
        ts.fire_flight(
            p, "manual", now=2.0, subject={"kind": "manual", "title": "by hand"}
        )
    )
    assert body["flight"]["by"] == "user"


def test_the_schedule_loop_ticks_the_triggers_once_a_minute(monkeypatch):
    ticks: list = []

    async def fake(now=None, **kw):
        ticks.append(now)
        return []

    monkeypatch.setattr(T, "tick", fake)
    assert asyncio.run(T.maybe_tick(1000.0)) == []
    assert asyncio.run(T.maybe_tick(1030.0)) == []
    assert asyncio.run(T.maybe_tick(1060.0)) == []
    assert ticks == [1000.0, 1060.0]
    # run_loop's beat includes the triggers' step.
    import inspect

    src = inspect.getsource(ts.run_loop)
    assert '("trigger", _tick_triggers)' in src
    import time

    monkeypatch.setattr(T, "_last_tick", time.time())
    asyncio.run(ts._tick_triggers())
    assert len(ticks) == 2  # inside the minute: paced, not ticked again
    monkeypatch.setattr(T, "_last_tick", time.time() - 61)
    asyncio.run(ts._tick_triggers())
    assert len(ticks) == 3


def test_default_branch_and_fetch_tip(repos, tmp_path):
    up, watch = repos
    assert T.default_branch(str(watch)) == "main"
    assert T.fetch_tip(str(watch), "main") == _git(up, "rev-parse", "HEAD")
    # No origin: the local branch answers.
    assert T.default_branch(str(up)) == "main"
    assert T.fetch_tip(str(up), "main") == _git(up, "rev-parse", "HEAD")
    assert T.fetch_tip(str(up), "") == ""
    plain = tmp_path / "plain"
    plain.mkdir()
    assert T.default_branch(str(plain)) == ""


def test_read_checks_reads_both_github_surfaces(monkeypatch):
    from backend.web.core import github_pr as gh

    monkeypatch.setattr(gh, "repo_ref", lambda wt: type("R", (), {"slug": "o/r"})())

    async def tok():
        return "t"

    monkeypatch.setattr(gh, "api_token", tok)
    answers = {
        "/repos/o/r/commits/abc/check-runs": (
            200,
            {
                "check_runs": [
                    {"name": "ci", "status": "completed", "conclusion": "failure"},
                    {"name": "lint", "status": "completed", "conclusion": "success"},
                ]
            },
        ),
        "/repos/o/r/commits/abc/status": (
            200,
            {
                "total_count": 1,
                "state": "failure",
                "statuses": [{"context": "deploy", "state": "error"}],
            },
        ),
    }

    async def req(method, path, **kw):
        return answers[path]

    monkeypatch.setattr(gh, "_request", req)
    assert asyncio.run(T.read_checks("/w", "abc")) == ("failed", ["ci", "deploy"])
    monkeypatch.setattr(gh, "repo_ref", lambda wt: None)
    assert asyncio.run(T.read_checks("/w", "abc")) == ("unknown", [])


# --------------------------------------------------------------------------- #
# The hook
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "body, title",
    [
        (b"", "hook"),
        (b"   \n ", "hook"),
        (b"deploy  finished\non prod", "hook: deploy finished on prod"),
        (b'{"title": " Grafana   alert "}', "hook: Grafana alert"),
        (b'{"text": "CI is red", "title": ""}', "hook: CI is red"),
        (b'{"other": 1}', "hook"),
        (b"{not json", "hook: {not json"),
        (b"x" * 100, "hook: " + "x" * 80),
        ("a string", "hook: a string"),
    ],
)
def test_hook_title(body, title):
    assert T.hook_title(body) == title


def _hook_plan(**trig) -> dict:
    return F.save_plan(
        {
            "name": "On call",
            "trigger": {"kind": "hook", **trig},
            "session": {"repo_path": "/repo"},
            "steps": [{"kind": "agent", "config": {"prompt": "Look: {subject}"}}],
        }
    )


@pytest.fixture
def bare(monkeypatch):
    """The flights router on a bare app — no gate, no loops."""
    app = FastAPI()
    app.include_router(routes.router)
    made: list = []

    async def _create(payload):
        made.append(payload)
        return "oncall-%d" % len(made)

    monkeypatch.setattr(D, "_create_session", _create)
    return TestClient(app), made


def test_hook_route_fires_the_plan_by_token(bare):
    client, made = bare
    p = _hook_plan()
    tok = p["trigger"]["token"]
    r = client.post("/api/hooks/flyway/" + tok, json={"title": "Grafana: CPU high"})
    assert r.status_code == 202, r.text
    f = r.json()["flight"]
    assert f["by"] == "hook" and f["subject"] == {
        "kind": "manual",
        "source": "",
        "id": "",
        "title": "hook: Grafana: CPU high",
        "url": "",
    }
    assert made == [{"repo_path": "/repo", "prompt": "Look: hook: Grafana: CPU high"}]
    assert _state()[p["id"]]["fired_at"] > 0
    rec = ts.load_runs(ts.flight_runs_path())[p["id"]]
    assert rec["last_trigger"] == "hook"


def test_hook_route_404_for_an_unknown_token(bare):
    client, _made = bare
    _hook_plan()
    r = client.post("/api/hooks/flyway/" + "0" * 32, content=b"x")
    assert r.status_code == 404 and r.json()["error"] == "unknown_token"
    # A manual plan that once was a hook keeps its token but does not answer.
    p = _hook_plan()
    F.save_plan({**p, "trigger": {**p["trigger"], "kind": "manual"}})
    assert F.get_plan(p["id"])["trigger"]["token"] == p["trigger"]["token"]
    r = client.post("/api/hooks/flyway/" + p["trigger"]["token"], content=b"x")
    assert r.status_code == 404


def test_hook_route_429_inside_quiet_minutes(bare):
    client, made = bare
    p = _hook_plan(quiet_minutes=15)
    url = "/api/hooks/flyway/" + p["trigger"]["token"]
    assert client.post(url, content=b"first").status_code == 202
    r = client.post(url, content=b"second")
    assert r.status_code == 429
    body = r.json()
    assert body["error"] == "quiet" and 0 < body["retry_after"] <= 15 * 60
    assert len(made) == 1
    # Quiet 0 = fire every time (a stacked flight is still refused as busy).
    q = _hook_plan(quiet_minutes=0)
    url = "/api/hooks/flyway/" + q["trigger"]["token"]
    assert client.post(url, content=b"a").status_code == 202
    r = client.post(url, content=b"b")
    assert r.status_code == 409 and r.json()["error"] == "busy"


def test_hook_route_409_when_paused(bare):
    client, _made = bare
    p = _hook_plan(paused=True)
    r = client.post("/api/hooks/flyway/" + p["trigger"]["token"], content=b"x")
    assert r.status_code == 409 and r.json()["error"] == "paused"


def test_hook_body_is_capped(bare):
    client, _made = bare
    p = _hook_plan()
    big = b"y" * (T.HOOK_BODY_MAX + 10)
    r = client.post("/api/hooks/flyway/" + p["trigger"]["token"], content=big)
    assert r.status_code == 202
    assert r.json()["flight"]["subject"]["title"] == "hook: " + "y" * 80


# --------------------------------------------------------------------------- #
# The gate: public for POST on exactly the token's shape
# --------------------------------------------------------------------------- #
_HOOK = "/api/hooks/flyway/" + "ab" * 16


@pytest.mark.parametrize(
    "scope, public",
    [
        ({"type": "http", "method": "POST", "path": _HOOK}, True),
        ({"type": "http", "method": "post", "path": _HOOK}, True),
        ({"type": "http", "method": "GET", "path": _HOOK}, False),
        ({"type": "http", "method": "DELETE", "path": _HOOK}, False),
        ({"type": "websocket", "method": "POST", "path": _HOOK}, False),
        ({"type": "http", "method": "POST", "path": _HOOK + "\n"}, False),
        ({"type": "http", "method": "POST", "path": _HOOK + "/"}, False),
        (
            {
                "type": "http",
                "method": "POST",
                "path": "/api/hooks/flyway/" + "ab" * 15 + "a",
            },
            False,
        ),
        (
            {
                "type": "http",
                "method": "POST",
                "path": "/api/hooks/flyway/" + "AB" * 16,
            },
            False,
        ),
        (
            {
                "type": "http",
                "method": "POST",
                "path": "/api/hooks/flyway/" + "zz" * 16,
            },
            False,
        ),
        (
            {
                "type": "http",
                "method": "POST",
                "path": "/api/hooks/flight/" + "ab" * 16,
            },
            False,
        ),
        ({"type": "http", "method": "POST", "path": "/api/flights/plans"}, False),
    ],
)
def test_public_flyway_hook_route_matching(scope, public):
    assert auth._public_flyway_hook_route(scope) is public
    assert auth._PUBLIC_FLYWAY_HOOK.pattern == r"/api/hooks/flyway/[a-f0-9]{32}"


def test_hook_post_skips_the_gate_but_its_neighbours_stay_gated(monkeypatch):
    monkeypatch.setenv("MINDFLOCK_AUTH_TOKEN", "test-token-abc123")
    monkeypatch.delenv("MINDFLOCK_AUTH", raising=False)
    client = TestClient(server.app)
    # The route answers for itself (404 for a token nobody holds), not 401.
    r = client.post(_HOOK, headers={"accept": "application/json"}, content=b"x")
    assert r.status_code == 404 and r.json()["error"] == "unknown_token"
    for method, path in (
        ("GET", _HOOK),
        ("POST", "/api/flights/plans"),
        ("GET", "/api/flights/plans"),
        ("POST", "/api/hooks/flyway/" + "ab" * 15),
    ):
        r = client.request(method, path, headers={"accept": "application/json"})
        assert r.status_code == 401, (method, path)


def test_a_hook_fired_during_a_tick_keeps_its_quiet_minutes(repos, monkeypatch):
    """The tick reads the state file, fetches every watched repo (up to a
    minute each), then wrote back its own copy — a hook fired meanwhile lost
    its ``fired_at``, so its quiet minutes never held and the next post fired
    a second route. The tick writes only the rows it changed."""
    up, watch = repos
    _plan("main_push", str(watch))
    hook = F.save_plan(
        {
            "name": "Hooked",
            "trigger": {"kind": "hook"},
            "steps": [{"kind": "agent", "config": {"prompt": "go"}}],
        }
    )
    _tick(1000.0)  # arms the watcher: the state file exists
    real = T.fetch_tip

    def fetch_while_a_hook_fires(folder, branch):
        st = T.load_state()
        st[hook["id"]] = {"fired_at": 1050.0, "last_status": 202}
        T.save_state(st)  # what hook_fire writes
        return real(folder, branch)

    monkeypatch.setattr(T, "fetch_tip", fetch_while_a_hook_fires)
    _commit(up, "b.txt")  # main moves: this tick writes its row
    assert len(_tick(1060.0, _starter([]))) == 1
    assert _state()[hook["id"]]["fired_at"] == 1050.0
