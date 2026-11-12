"""Routes on rails: a disrupted route picks up where it left off, and a route
can start in the middle (docs/flights.md "Contract: the HTTP API").

* ``flights.resume_point`` (pure): where a new flight of a route carries on
  from the earlier flights on the same session / branch / ticket;
* ``start_at: "resume"`` on a start, and the dry run's ``resume``;
* ``flight_driver.again`` (``POST /api/flights/{id}/again``): fly an ended
  route again from the stop it had reached;
* ``flight_driver.carry_on_intake``: an Intake restart whose branch is
  already checked out carries on in that work instead of dying at stop 1;
* a server restart mid-route re-runs no finished stop and re-sends no
  prompt.

Against fakes and a temp flights store only — never the real engine or
``~/.mindflock``."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from backend.web.core import flight_driver as D
from backend.web.core import flights as F
from backend.web.core import worktree_reclaim as R

NOW = time.time()


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
    monkeypatch.setattr(D, "_JOBS", {})
    monkeypatch.setattr(D, "_REVIEWS", {})
    monkeypatch.setattr(D, "_CHECKS", {})
    monkeypatch.setattr(D, "wake", lambda: None)
    monkeypatch.setattr(D, "_disk_holder", lambda real: "")
    yield


class FakeInst:
    def __init__(self, path="/repo", branch="feat", repo="/repo"):
        self.Path = path
        self.Branch = branch
        self.Repo = repo
        self.Status = SimpleNamespace(name="Running")

    def GetWorktreePath(self):
        return self.Path

    def GetGitWorktree(self):
        return SimpleNamespace(GetRepoPath=lambda: self.Repo)


class Resp:
    def __init__(self, body, status=200):
        self.body = json.dumps(body).encode()
        self.status_code = status


@pytest.fixture
def srv(monkeypatch):
    ns = SimpleNamespace(
        _SERVER_BOOT_ID="boot-1",
        ENGINE=SimpleNamespace(instances={"s": FakeInst()}),
        _pending_rows=lambda: [],
        git_available=lambda: True,
        _peer_guard=SimpleNamespace(refusal=lambda inst, what: None),
        _lane_refusal=lambda title, lane: None,
        _publish_autopilot=lambda title: None,
        _load_recently_closed=lambda: [],
        reopened=[],
    )

    async def reopen(entry_id):
        ns.reopened.append(entry_id)
        title = "sc-1"
        ns.ENGINE.instances[title] = FakeInst("/wt/sc-1", "feature/sc-1/x", "/base")
        return Resp({"title": title})

    ns._reopen_closed_entry = reopen
    monkeypatch.setattr(D, "_server", lambda: ns)
    monkeypatch.setattr(D, "preflight", lambda *a, **kw: [])
    return ns


@pytest.fixture
def row(monkeypatch):
    state = {"activity": "idle", "branch": "feat", "folder": "/repo", "pr_url": ""}

    def fake(flight, now, agent):
        out = {
            "present": flight["session"] in D._server().ENGINE.instances,
            "pending": False,
            "status": "running",
            "sleeping": False,
        }
        out.update(state)
        return out

    monkeypatch.setattr(D, "_session_obs", fake)
    return state


def run(coro):
    return asyncio.run(coro)


ROUTE = [
    {"kind": "agent", "title": "Work it"},
    {"kind": "ship", "title": "Commit", "config": {"lane": "commit"}},
    {"kind": "ship", "title": "Push", "config": {"lane": "push"}},
    {"kind": "you", "title": "Approve", "config": {"text": "ok?"}},
]


def plan(steps=ROUTE, name="Ticket"):
    return F.save_plan({"name": name, "steps": [dict(s) for s in steps]})


def stopped_at(p, k, *, session="s", reason=F.SESSION_REMOVED, **kw):
    """A flight of ``p`` on ``session`` that got past its first ``k`` stops
    and was then cancelled (by default: its session was deleted)."""
    f = F.take_off(p, session=session, **kw)

    def fn(x):
        for s in x["steps"][:k]:
            x["step_states"][s["id"]]["state"] = "done"
        x["cursor"] = k
        if k < len(x["steps"]):
            x["step_states"][x["steps"][k]["id"]]["state"] = "running"
        F.op_cancel(x, reason)

    return F.mutate(f["id"], fn)


# --------------------------------------------------------------------------- #
# resume_point (pure)
# --------------------------------------------------------------------------- #
def test_reached_is_the_first_stop_not_done_from_the_cursor():
    f = {
        "cursor": 1,
        "steps": [{"id": "s1"}, {"id": "s2"}, {"id": "s3"}],
        "step_states": {"s2": {"state": "skipped"}, "s3": {"state": "running"}},
    }
    assert F.reached(f) == 2
    f["step_states"]["s3"]["state"] = "done"
    assert F.reached(f) == 3


def test_same_plan_carries_on_at_the_first_unfinished_stop_by_id():
    p = plan()
    old = stopped_at(p, 2)
    got = F.resume_point(p, [old])
    assert got == {
        "stop": "s3",
        "index": 2,
        "title": "Push",
        "from_flight": old["id"],
        "session": "s",
        "state": "cancelled",
        "reason": "its session was deleted",
        "at": old["finished"],
        "in_progress": False,
    }


def test_the_newest_flight_decides_and_a_landed_one_means_start_over():
    p = plan()
    old = stopped_at(p, 2)
    assert F.resume_point(p, [old])["stop"] == "s3"
    newer = dict(F.take_off(p, session="s2"), state="done", started=old["started"] + 5)
    assert F.resume_point(p, [old, newer]) is None


def test_nothing_done_or_nothing_matching_is_the_normal_start():
    p = plan()
    assert F.resume_point(p, [stopped_at(p, 0)]) is None
    other = plan([{"kind": "notify", "title": "Ping"}, {"kind": "wait"}], "Other")
    assert F.resume_point(p, [stopped_at(other, 1, session="x")]) is None
    assert F.resume_point(p, []) is None


def test_another_route_matches_by_kind_and_title_in_order():
    ladder = plan(
        [
            {"kind": "agent", "title": "Work it"},
            {"kind": "ship", "title": "Commit", "config": {"lane": "commit"}},
            {"kind": "ship", "title": "Push", "config": {"lane": "push"}},
        ],
        "Fast-track",
    )
    old = stopped_at(ladder, 2)
    team = plan(
        [
            {"kind": "agent", "title": "Work it"},
            {"kind": "ship", "title": "commit", "config": {"lane": "commit"}},
            {"kind": "ship", "title": "Push", "config": {"lane": "push"}},
            {"kind": "you", "title": "Approve", "config": {"text": "ok?"}},
        ],
        "Team",
    )
    got = F.resume_point(team, [old])
    assert got["stop"] == "s3" and got["index"] == 2 and got["title"] == "Push"


def test_another_route_never_skips_a_stop_the_old_one_did_not_fly():
    """Review fix 3: Lint (and an unreviewed PR) must not be skipped because
    an older route of other stops got further."""
    ladder = plan(
        [
            {"kind": "agent", "title": "Work it"},
            {"kind": "ship", "title": "Commit", "config": {"lane": "commit"}},
            {"kind": "ship", "title": "Push", "config": {"lane": "push"}},
        ],
        "Fast-track",
    )
    old = stopped_at(ladder, 2)
    team = plan(
        [
            {"kind": "agent", "title": "Work it"},
            {"kind": "command", "title": "Lint", "config": {"command": "true"}},
            {"kind": "ship", "title": "commit", "config": {"lane": "commit"}},
            {"kind": "ship", "title": "Push", "config": {"lane": "push"}},
        ],
        "Team",
    )
    assert F.resume_point(team, [old]) is None
    adhoc = F.take_off(
        F.adhoc_plan(
            [
                {"kind": "agent", "title": "Work it"},
                {"kind": "ship", "title": "Open PR", "config": {"lane": "pr"}},
            ]
        ),
        session="s",
    )
    F.mutate(adhoc["id"], lambda x: x["step_states"]["s1"].update(state="done"))
    halted = F.mutate(
        adhoc["id"], lambda x: x.update(cursor=1, state="halted", reason="no PR")
    )
    review = plan(
        [
            {"kind": "agent", "title": "Work it"},
            {"kind": "you", "title": "Review", "config": {"text": "look"}},
            {"kind": "verify"},
            {"kind": "ship", "title": "Open PR", "config": {"lane": "pr"}},
        ],
        "Reviewed",
    )
    assert F.resume_point(review, [halted]) is None


def test_halted_and_still_current_flights_are_sources_too():
    p = plan()
    f = F.take_off(p, session="s", start_at="s3")
    assert F.resume_point(p, [f])["state"] == "running"
    halted = F.mutate(f["id"], lambda x: x.update(state="halted", reason="CI red"))
    got = F.resume_point(p, [halted])
    assert (got["stop"], got["state"], got["reason"]) == ("s3", "halted", "CI red")


def test_resume_candidates_by_session_branch_or_subject():
    tix = {"kind": "ticket", "source": "sc", "id": "12"}
    a = {"id": "a", "session": "s", "branch": "x", "repo_root": "/r"}
    b = {"id": "b", "session": "t", "branch": "feat", "repo_root": "/r/"}
    c = {"id": "c", "session": "u", "branch": "main", "repo_root": "/r"}
    d = {"id": "d", "session": "v", "subject": dict(tix)}
    e = {"id": "e", "session": "w", "branch": "feat", "repo_root": "/other"}
    every = [a, b, c, d, e]
    got = F.resume_candidates(
        every, session="s", repo_root="/r", branch="feat", subject=tix
    )
    assert [f["id"] for f in got] == ["a", "b", "d"]
    # main / master are everybody's: never a reason to carry on.
    got = F.resume_candidates(every, repo_root="/r", branch="main")
    assert got == []
    got = F.resume_candidates(every, session="s", exclude=("a",))
    assert got == []


def test_resume_at_keeps_an_explicit_stop_and_resolves_resume():
    p = plan()
    old = stopped_at(p, 2)
    assert F.resume_at(p, "s2", [old]) == ("s2", None)
    at, point = F.resume_at(p, "resume", [old])
    assert at == "s3" and point["from_flight"] == old["id"]
    assert F.resume_at(p, "resume", []) == ("", None)
    named = dict(p, steps=[dict(p["steps"][0], id="resume")] + p["steps"][1:])
    assert F.resume_at(named, "resume", [old]) == ("resume", None)


def test_a_deleted_sessions_route_stays_a_source():
    p = plan()
    f = F.take_off(p, session="s")
    F.mutate(f["id"], lambda x: x["step_states"]["s1"].update(state="done"))
    F.mutate(f["id"], lambda x: x.update(cursor=1))
    after = F.mutate(f["id"], lambda x: F.op_cancel(x, F.SESSION_REMOVED))
    assert after["state"] == "cancelled" and after["reason"] == F.SESSION_REMOVED
    assert F.resume_point(p, [after])["stop"] == "s2"


def test_again_plan_refuses_a_route_still_going_or_never_flown():
    p = plan()
    live = F.take_off(p, session="s")
    with pytest.raises(F.FlightError) as e:
        F.again_plan(live)
    assert e.value.error == "in_flight" and e.value.status == 409
    with pytest.raises(F.FlightError) as e:
        F.again_plan(dict(live, state="grounded"))
    assert e.value.error == "not_flown"


def test_again_plan_starts_where_it_stopped_or_where_asked():
    p = plan()
    old = stopped_at(p, 2)
    again, at, _sent = F.again_plan(old)
    assert at == "s3" and [s["id"] for s in again["steps"]] == ["s1", "s2", "s3", "s4"]
    assert again["id"] == p["id"] and again["name"] == "Ticket"
    assert F.again_plan(old, "s1")[1] == "s1"
    assert F.again_plan(old, "resume")[1] == "s3"
    with pytest.raises(F.FlightError) as e:
        F.again_plan(old, "s9")
    assert e.value.error == "unknown_step" and e.value.status == 400
    landed = dict(old, state="done", cursor=4)
    landed["step_states"] = {
        k: dict(v, state="done") for k, v in old["step_states"].items()
    }
    assert F.again_plan(landed)[1] == "s1"


def test_op_carry_on_resets_the_armed_flight_onto_the_holder():
    p = plan()
    armed = F.arm_pending(
        "sc-1", p["id"], {"kind": "ticket", "source": "sc", "id": "1"}
    )
    assert armed["step_states"]["s1"]["state"] == "running"
    after = F.mutate(armed["id"], lambda x: F.op_carry_on(x, "pr-1", 2, "why"))
    assert after["session"] == "pr-1" and after["cursor"] == 2
    assert [after["step_states"][s]["state"] for s in ("s1", "s2", "s3")] == [
        "skipped",
        "skipped",
        "pending",
    ]
    assert after["step_states"]["s1"]["detail"] == {}
    assert after["history"][-1]["event"] == "jumped"
    assert after["history"][-1]["note"] == "why"


# --------------------------------------------------------------------------- #
# Start: start_at "resume"
# --------------------------------------------------------------------------- #
def test_start_resume_carries_on_where_the_last_run_stopped(srv, row):
    p = plan()
    old = stopped_at(p, 2)
    f = run(D.start({"plan_id": p["id"], "title": "s", "start_at": "resume"}))
    assert f["cursor"] == 2 and f["state"] == "running"
    assert f["step_states"]["s1"]["state"] == "skipped"
    jumped = [h for h in f["history"] if h["event"] == "jumped"]
    assert jumped[-1]["note"] == (
        "carried on where route %s stopped (its session was deleted), at Push"
        % old["id"]
    )


def test_start_resume_with_nothing_earlier_is_the_normal_start(srv, row):
    p = plan()
    f = run(D.start({"plan_id": p["id"], "title": "s", "start_at": "resume"}))
    # The normal attach: the Agent stop with no prompt is the work so far.
    assert f["cursor"] == 1


def test_an_explicit_stop_still_wins(srv, row):
    p = plan()
    stopped_at(p, 2)
    f = run(D.start({"plan_id": p["id"], "title": "s", "start_at": "s4"}))
    assert f["cursor"] == 3


# --------------------------------------------------------------------------- #
# Fly again
# --------------------------------------------------------------------------- #
def test_again_flies_a_cancelled_route_on_its_session_from_its_stop(srv, row):
    p = plan()
    old = stopped_at(p, 2, reason="cancelled by you")
    out = run(D.again(old["id"]))
    f = out["flight"]
    assert out["from"] == old["id"] and out["session"] == "s" and out["how"] == "same"
    assert f["id"] != old["id"] and f["cursor"] == 2 and f["state"] == "running"
    assert f["plan_id"] == p["id"] and f["plan_name"] == "Ticket"
    assert f["history"][-1]["note"] == "flown again from route %s, at Push" % old["id"]
    assert F.get(old["id"])["state"] == "cancelled"


def test_again_at_an_explicit_stop_and_on_another_session(srv, row):
    srv.ENGINE.instances["t"] = FakeInst("/t", "other")
    old = stopped_at(plan(), 2)
    out = run(D.again(old["id"], {"at": "s1", "title": "t"}))
    assert out["how"] == "chosen" and out["flight"]["session"] == "t"
    assert out["flight"]["cursor"] == 0
    assert out["flight"]["history"][-1]["event"] == "noted"


def test_again_on_a_halted_route_replaces_it(srv, row):
    p = plan()
    f = F.take_off(p, session="s", start_at="s2")
    F.mutate(f["id"], lambda x: x.update(state="halted", reason="CI red"))
    out = run(D.again(f["id"]))
    assert out["flight"]["cursor"] == 1
    assert F.get(f["id"])["state"] == "cancelled"


def test_again_refuses_a_route_still_going(srv, row):
    f = F.take_off(plan(), session="s")
    with pytest.raises(F.FlightError) as e:
        run(D.again(f["id"]))
    assert e.value.error == "in_flight"
    with pytest.raises(F.FlightError) as e:
        run(D.again("f_nope"))
    assert e.value.status == 404


def test_again_with_its_session_gone_uses_the_one_holding_its_branch(srv, row):
    old = stopped_at(plan(), 1, session="gone", repo_root="/repo", branch="feat")
    out = run(D.again(old["id"]))
    assert out["session"] == "s" and out["how"] == "branch"
    assert "which holds its branch" in out["flight"]["history"][-1]["note"]


def test_again_with_its_session_closed_reopens_it(srv, row):
    old = stopped_at(
        plan(), 1, session="sc-1", repo_root="/base", branch="feature/sc-1/x"
    )
    srv._load_recently_closed = lambda: [
        {
            "id": "rc1",
            "title": "sc-1",
            "branch": "feature/sc-1/x",
            "data": {"worktree": {"repo_path": "/base"}},
        }
    ]
    out = run(D.again(old["id"]))
    assert srv.reopened == ["rc1"]
    assert out["how"] == "reopened" and out["flight"]["session"] == "sc-1"
    assert out["flight"]["cursor"] == 1


def test_again_with_nothing_left_says_what_to_do(srv, row, monkeypatch):
    old = stopped_at(plan(), 1, session="gone", repo_root="/r", branch="feature/x")
    monkeypatch.setattr(D, "_ref_ok", lambda folder, ref: True)
    with pytest.raises(F.FlightError) as e:
        run(D.again(old["id"]))
    assert e.value.error == "session_gone" and e.value.status == 409
    assert e.value.detail == (
        "gone is gone, but its branch feature/x is still in /r — start a "
        "session on that branch, then fly it again on that session"
    )
    monkeypatch.setattr(D, "_ref_ok", lambda folder, ref: False)
    with pytest.raises(F.FlightError) as e:
        run(D.again(old["id"]))
    assert e.value.detail == (
        "gone is gone and nothing on this machine holds its work — fly it again "
        "on another session"
    )


# --------------------------------------------------------------------------- #
# Intake restart: carry on in the work that is there
# --------------------------------------------------------------------------- #
TIX = {"kind": "ticket", "source": "sc", "id": "1", "title": "Fix it"}
BRANCH = "feature/sc-1/x"


@pytest.fixture
def held(monkeypatch):
    where = {"path": "/wt/sc-1"}
    monkeypatch.setattr(R, "worktree_for_launch", lambda url, branch: where["path"])
    return where


def test_a_free_branch_creates_as_usual(srv, held):
    held["path"] = ""
    F.arm_pending("sc-1", plan()["id"], TIX, branch=BRANCH)
    assert run(D.carry_on_intake("sc-1", "git@x:r", BRANCH)) == ""
    assert F.for_session("sc-1")["cursor"] == 0


def test_no_armed_route_is_left_alone(srv, held):
    assert run(D.carry_on_intake("sc-1", "git@x:r", BRANCH)) == ""


def test_a_live_holder_takes_the_route_past_the_agent_stop(srv, held, row):
    srv.ENGINE.instances["pr-1"] = FakeInst("/wt/sc-1", BRANCH, "/base")
    armed = F.arm_pending("sc-1", plan()["id"], TIX, branch=BRANCH)
    assert run(D.carry_on_intake("sc-1", "git@x:r", BRANCH)) == "pr-1"
    f = F.get(armed["id"])
    assert f["session"] == "pr-1" and f["state"] == "running" and f["cursor"] == 1
    assert f["repo_root"] == "/base" and f["branch"] == BRANCH
    assert f["history"][-1]["note"] == (
        "carried on in pr-1 (already had this branch): the work already on its "
        "branch counts as its Agent stop"
    )


def test_a_live_holder_carries_on_at_the_resume_point(srv, held, row):
    srv.ENGINE.instances["pr-1"] = FakeInst("/wt/sc-1", BRANCH, "/base")
    p = plan()
    old = stopped_at(p, 2, session="sc-1", subject=TIX)
    armed = F.arm_pending("sc-1", p["id"], TIX, branch=BRANCH)
    assert run(D.carry_on_intake("sc-1", "git@x:r", BRANCH)) == "pr-1"
    f = F.get(armed["id"])
    assert f["cursor"] == 2
    assert f["history"][-1]["note"].startswith(
        "carried on where route %s stopped (its session was deleted), at Push"
        % old["id"]
    )


def test_a_holder_already_on_a_route_keeps_it(srv, held, row):
    srv.ENGINE.instances["pr-1"] = FakeInst("/wt/sc-1", BRANCH, "/base")
    theirs = F.take_off(plan(name="Review"), session="pr-1")
    armed = F.arm_pending("sc-1", plan()["id"], TIX, branch=BRANCH)
    assert run(D.carry_on_intake("sc-1", "git@x:r", BRANCH)) == "pr-1"
    f = F.get(armed["id"])
    assert f["state"] == "cancelled"
    assert f["reason"] == (
        "pr-1 already holds this branch and is on its route (Review) — that "
        "route carries on there"
    )
    assert F.get(theirs["id"])["state"] == "running"


def test_a_closed_sessions_kept_worktree_is_reopened(srv, held, row):
    srv._load_recently_closed = lambda: [
        {"id": "rc9", "title": "sc-1", "folder": "/wt/sc-1", "data": {}}
    ]
    armed = F.arm_pending("sc-1", plan()["id"], TIX, branch=BRANCH)
    assert run(D.carry_on_intake("sc-1", "git@x:r", BRANCH)) == "sc-1"
    assert srv.reopened == ["rc9"]
    f = F.get(armed["id"])
    assert f["session"] == "sc-1" and f["cursor"] == 1
    assert "reopened: it had kept this branch's work" in f["history"][-1]["note"]


def test_a_folder_no_session_accounts_for_is_opened_in_place(
    srv, held, row, monkeypatch
):
    opened = []

    async def fake_open(title, folder):
        opened.append((title, folder))
        srv.ENGINE.instances[title] = FakeInst(folder, BRANCH, "/base")
        return title

    monkeypatch.setattr(D, "_open_in_place", fake_open)
    armed = F.arm_pending("sc-1", plan()["id"], TIX, branch=BRANCH)
    assert run(D.carry_on_intake("sc-1", "git@x:r", BRANCH)) == "sc-1"
    assert opened == [("sc-1", "/wt/sc-1")]
    assert F.get(armed["id"])["cursor"] == 1


def test_when_nothing_can_take_it_one_sentence_names_what_to_close(
    srv, held, monkeypatch
):
    async def no_open(title, folder):
        return ""

    monkeypatch.setattr(D, "_open_in_place", no_open)
    F.arm_pending("sc-1", plan()["id"], TIX, branch=BRANCH)
    with pytest.raises(D.CarryOnRefused) as e:
        run(D.carry_on_intake("sc-1", "git@x:r", BRANCH))
    assert str(e.value) == (
        "branch feature/sc-1/x is already checked out at /wt/sc-1 and no session "
        "here could take it over — close /wt/sc-1, then Run it again"
    )


def test_the_ticket_start_carries_on_instead_of_creating(srv, held, row, monkeypatch):
    """ticket_start's background start: a carried route creates no session,
    marks the ticket started, and never cancels the route."""
    from backend.web.core import ticket_start as T

    srv.ENGINE.instances["pr-1"] = FakeInst("/wt/sc-1", BRANCH, "/base")
    armed = F.arm_pending("sc-1", plan()["id"], TIX, branch=BRANCH)
    calls = []
    tasks = []
    story = SimpleNamespace(
        id="1", name="Fix it", repo_url="git@x:r", app_url="", slug="sc-1"
    )

    async def found(source, ident):
        return story

    fake = SimpleNamespace(
        ENGINE=srv.ENGINE,
        _cached_session_title=lambda *a: "sc-1",
        _ASSIGNED_TICKETS_CACHE={},
        _pending_has=lambda t: False,
        _pending_add=lambda *a, **kw: None,
        _pending_drop=lambda t: calls.append(("drop", t)),
        _source_intake_depth=lambda s: "",
        _arm_intake=lambda *a, **kw: "",
        _intake_plan=lambda *a: "",
        _source_intake_plan=lambda s: "",
        _intake_prompt_tail=lambda lineage, title, program: ("", False, ""),
        _worktree_reclaim=SimpleNamespace(reclaim_for_launch=lambda url, b: ""),
        _cancel_intake_flight=lambda t, why: calls.append(("cancel", why)),
        _register_task=lambda coro: tasks.append(coro),
        log=SimpleNamespace(InfoLog=None, ErrorLog=None),
        session=SimpleNamespace(
            NewInstance=lambda *a, **kw: calls.append("created"),
            InstanceOptions=lambda **kw: kw,
        ),
    )
    fake.ENGINE.default_program = lambda: "claude"
    monkeypatch.setattr(T, "_server", lambda: fake)
    monkeypatch.setattr(T, "find_ticket", found)
    monkeypatch.setattr(T, "route_story", lambda *a: None)
    monkeypatch.setattr(T, "build_prompt", lambda story: "do it")
    monkeypatch.setattr(T, "agent_for", lambda story: "")
    monkeypatch.setattr(T, "branch_for", lambda story: BRANCH)
    monkeypatch.setattr(T, "record_started", lambda story: calls.append("started"))
    monkeypatch.setattr(
        T, "record_result", lambda story, **kw: calls.append(("result", kw))
    )
    from backend.ticket_ingestion import tracker_claims
    from backend.web.core import fleet_claims

    async def nobody(*a, **kw):
        return None

    monkeypatch.setattr(fleet_claims, "holder", nobody)
    monkeypatch.setattr(tracker_claims, "tracker_holder", nobody)
    body = run(T.launch("sc", "1", plan="x"))
    assert body["title"] == "sc-1"
    run(tasks[0])
    assert "created" not in calls
    assert not [c for c in calls if isinstance(c, tuple) and c[0] == "cancel"]
    assert "started" in calls and ("result", {"branch": BRANCH}) in calls
    assert F.get(armed["id"])["session"] == "pr-1"


# --------------------------------------------------------------------------- #
# A server restart mid-route
# --------------------------------------------------------------------------- #
def restart(srv, monkeypatch, boot):
    """What a new process sees: no cached store, a new boot id, an expired
    lease of the old one."""
    monkeypatch.setattr(F, "_CACHE", {"key": None, "flights": {}, "digest": None})
    srv._SERVER_BOOT_ID = boot
    real = time.time

    # The old server's lease ran out (it died more than LEASE_STALE_S ago).
    monkeypatch.setattr(
        F.time, "time", lambda: real() + F.LEASE_STALE_S + 1, raising=True
    )


def test_a_running_agent_stop_is_not_sent_again_after_a_restart(srv, row, monkeypatch):
    sent = []
    monkeypatch.setattr(D, "_deliver", lambda title, text: sent.append(text))
    p = plan([{"kind": "agent", "config": {"prompt": "do it"}}, {"kind": "wait"}])
    f = F.take_off(p, session="s")
    run(D.step_flight(f["id"]))
    assert sent == ["do it"]
    before = F.get(f["id"])
    assert before["step_states"]["s1"]["state"] == "running"
    restart(srv, monkeypatch, "boot-2")
    row["activity"] = "working"
    run(D.step_flight(f["id"]))
    run(D.step_flight(f["id"]))
    after = F.get(f["id"])
    assert sent == ["do it"]
    assert after["owner"] == "boot-2" and after["cursor"] == 0
    assert after["step_states"]["s1"]["state"] == "running"
    assert after["step_states"]["s1"]["since"] == before["step_states"]["s1"]["since"]


def test_a_route_waiting_on_you_still_waits_and_rings_once_after_a_restart(
    srv, row, monkeypatch
):
    from backend.web.core import events as _events

    rang = []
    unsub = _events.BUS.subscribe(
        lambda env: (
            rang.append(env) if env.get("event") == "flight.needs_you" else None
        )
    )
    try:
        p = plan(
            [
                {"kind": "notify", "config": {}},
                {"kind": "you", "config": {"text": "ok?"}},
            ]
        )
        monkeypatch.setattr(D, "_run_notify", lambda f, s: {"note": "sent"})
        f = F.take_off(p, session="s")
        run(D.step_flight(f["id"]))
        run(D.step_flight(f["id"]))
        assert F.get(f["id"])["state"] == "waiting_you" and len(rang) == 1
        notified = F.get(f["id"])["step_states"]["s1"]
        restart(srv, monkeypatch, "boot-2")
        run(D.step_flight(f["id"]))
        after = F.get(f["id"])
        assert after["state"] == "waiting_you" and after["cursor"] == 1
        assert after["step_states"]["s1"] == notified
        assert len(rang) == 1
    finally:
        unsub()


def test_a_fix_round_is_not_sent_again_after_a_restart(srv, row, monkeypatch):
    sent = []
    monkeypatch.setattr(D, "_deliver", lambda title, text: sent.append(text))

    async def red(*a):
        return {"verdict": "failed", "failing": ["lint"]}

    monkeypatch.setattr(D, "_checks_obs", red)
    p = plan([{"kind": "checks", "title": "CI", "config": {}, "on_fail": "agent"}])
    f = F.take_off(p, session="s")
    run(D.step_flight(f["id"]))
    run(D.step_flight(f["id"]))
    assert len(sent) == 1
    mid = F.get(f["id"])["step_states"]["s1"]
    assert mid["detail"]["phase"] == "fix" and mid["rounds"] == 1
    restart(srv, monkeypatch, "boot-2")
    row["activity"] = "working"
    run(D.step_flight(f["id"]))
    after = F.get(f["id"])
    assert len(sent) == 1
    ss = after["step_states"]["s1"]
    assert ss["state"] == "running" and ss["rounds"] == 1
    assert ss["detail"]["phase"] == "fix"
    assert after["state"] == "running"


def test_again_whose_reopen_brings_the_route_itself_back(srv, row):
    old = stopped_at(
        plan(), 1, session="sc-1", repo_root="/base", branch="feature/sc-1/x"
    )
    srv._load_recently_closed = lambda: [
        {"id": "rc1", "title": "sc-1", "flight": {"id": old["id"]}}
    ]

    async def reopen(entry_id):
        srv.reopened.append(entry_id)
        srv.ENGINE.instances["sc-1"] = FakeInst("/wt/sc-1", "feature/sc-1/x", "/base")
        # What the reopen hook does with the entry's own chain.
        D.restore_on_reopen({"id": old["id"], "state": "running"}, "sc-1")
        return Resp({"title": "sc-1"})

    srv._reopen_closed_entry = reopen
    out = run(D.again(old["id"]))
    assert out["how"] == "restored" and out["flight"]["id"] == old["id"]
    assert out["flight"]["state"] == "running" and out["flight"]["cursor"] == 1
    assert len(F.list_flights()) == 1


# --------------------------------------------------------------------------- #
# Review round: one test per fix
# --------------------------------------------------------------------------- #
def test_fix1_resume_without_a_session_is_the_normal_start(srv, row, monkeypatch):
    """A start that CREATES its session gets a fresh branch: "resume" must
    not start it halfway down the route with no first prompt."""
    import backend.web.core.session_create as sc

    made = []

    async def fake_create(payload, **kw):
        made.append(payload)
        srv.ENGINE.instances["new-1"] = FakeInst()
        return 202, {"title": "new-1"}

    monkeypatch.setattr(sc, "create_result", fake_create)
    p = plan([dict(ROUTE[0], config={"prompt": "do the ticket"})] + ROUTE[1:])
    stopped_at(p, 2, session="old", subject=TIX)
    f = run(
        D.start(
            {
                "plan_id": p["id"],
                "repo_path": "/repo",
                "start_at": "resume",
                "subject": TIX,
            }
        )
    )
    assert made[0]["prompt"] == "do the ticket"
    assert f["session"] == "new-1" and f["cursor"] == 0
    assert f["step_states"]["s1"]["state"] == "running"
    assert D.resume_start_at(p, "resume", session="", subject=TIX) == ("", None)


def _owing(p, cursor_on_perch=True):
    """A flight of ``p`` (Agent, Verify ask:end, Notify) whose Verify stop
    owed the person a check, cancelled on its added "Your checks" perch (or,
    with ``cursor_on_perch`` False, on the Notify stop before it)."""
    f = F.take_off(p, session="s")

    def fn(x):
        x["steps"].append(
            {
                "id": "s4",
                "kind": "you",
                "title": F.OWED_PERCH_TITLE,
                "config": {"text": "Before this route finishes: 1 check for you"},
                "on_fail": "halt",
            }
        )
        x["step_states"]["s4"] = F._blank_step_state()
        x["owed_perch"] = "s4"
        x["owed"] = [{"step": "s2", "title": "Verify", "items": ["login works"]}]
        upto = 3 if cursor_on_perch else 2
        for s in x["steps"][:upto]:
            x["step_states"][s["id"]]["state"] = "done"
        x["cursor"] = upto
        x["step_states"][x["steps"][upto]["id"]]["state"] = (
            "waiting_you" if cursor_on_perch else "running"
        )
        F.op_cancel(x, "cancelled by you")

    return F.mutate(f["id"], fn)


OWE_ROUTE = [
    {"kind": "agent", "title": "Work it"},
    {"kind": "verify", "title": "Verify", "config": {"ask": "end"}},
    {"kind": "notify", "title": "Ping", "config": {"text": "hi"}},
]


def test_fix2_the_owed_perch_never_leaks_into_again_or_resume():
    p = plan(OWE_ROUTE, "Owes")
    old = _owing(p)
    assert old["owed_perch"] == "s4" and old["owed"][0]["step"] == "s2"
    again, at, sent = F.again_plan(old)
    # Its own stops only, and the Verify stop that still owed runs again.
    assert [s["id"] for s in again["steps"]] == ["s1", "s2", "s3"]
    assert at == "s2" and sent is False
    assert F.resume_point(p, [old])["stop"] == "s2"
    # Cancelled before the perch, the owed Verify stop is still the point.
    mid = _owing(p, cursor_on_perch=False)
    assert F.again_plan(mid)[1] == "s2"
    # A landed flight that once added the perch: flying again never parks on
    # a phantom question — it is not among the stops.
    landed = F.mutate(
        old["id"],
        lambda x: x.update(state="done", owed=[], cursor=4)
        or [x["step_states"][k].update(state="done") for k in x["step_states"]],
    )
    again, at, _ = F.again_plan(landed)
    assert [s["title"] for s in again["steps"]] == ["Work it", "Verify", "Ping"]
    assert at == "s1"


def test_fix4_again_never_reopens_or_flies_on_another_repo(srv, row, monkeypatch):
    monkeypatch.setattr(D, "_ref_ok", lambda folder, ref: False)
    old = stopped_at(plan(), 1, session="sc-1", repo_root="/base", branch="feature/x")
    srv._load_recently_closed = lambda: [
        {
            "id": "rcX",
            "title": "sc-1",
            "branch": "feature/x",
            "data": {"worktree": {"repo_path": "/other-repo"}},
        }
    ]
    srv.ENGINE.instances["elsewhere"] = FakeInst(
        "/other-repo", "feature/x", "/other-repo"
    )
    with pytest.raises(F.FlightError) as e:
        run(D.again(old["id"]))
    assert e.value.error == "session_gone" and srv.reopened == []
    # No repository on record: a branch of that name anywhere is no match.
    bare = stopped_at(plan(), 1, session="gone2", repo_root="", branch="feature/x")
    with pytest.raises(F.FlightError):
        run(D.again(bare["id"]))
    # staging / develop / dev / trunk are everybody's, like main.
    srv.ENGINE.instances["stg"] = FakeInst("/repo", "staging", "/repo")
    shared = stopped_at(plan(), 1, session="gone3", repo_root="/repo", branch="staging")
    with pytest.raises(F.FlightError):
        run(D.again(shared["id"]))
    assert {"staging", "develop", "dev", "trunk"} <= F.SHARED_BRANCHES
    assert (
        F.resume_candidates(
            [{"id": "a", "branch": "develop", "repo_root": "/r"}],
            repo_root="/r",
            branch="develop",
        )
        == []
    )


def _sent_then_cancelled(p, session="s"):
    """A flight whose Agent stop s1 (with a prompt) was running — its prompt
    sent — when it was cancelled."""
    f = F.take_off(p, session=session)

    def fn(x):
        x["step_states"]["s1"].update(state="running", detail={"seeded": True})
        F.op_cancel(x, "cancelled by you")

    return F.mutate(f["id"], fn)


PROMPTED = [
    {"kind": "agent", "title": "Work it", "config": {"prompt": "do it"}},
    {"kind": "notify", "title": "Ping", "config": {"text": "hi"}},
]


def test_fix5_again_never_types_a_sent_prompt_twice(srv, row, monkeypatch):
    sent = []
    monkeypatch.setattr(D, "_deliver", lambda title, text: sent.append(text))
    monkeypatch.setattr(D, "_run_notify", lambda f, s: {"note": "sent"})
    p = plan(PROMPTED, "Prompted")
    # Idle: the work it did is the stop; the route goes on after it.
    monkeypatch.setattr(D, "_session_busy", lambda title: "")
    old = _sent_then_cancelled(p)
    assert F.again_plan(old)[1:] == ("s1", True)
    f = run(D.again(old["id"]))["flight"]
    assert f["cursor"] == 1 and f["step_states"]["s1"]["state"] == "skipped"
    assert "its prompt went out already" in f["history"][-1]["note"]
    run(D.step_flight(f["id"]))
    assert sent == []
    # Busy: it is that work in progress — running, nothing sent.
    monkeypatch.setattr(D, "_session_busy", lambda title: "working")
    old2 = _sent_then_cancelled(p)
    g = run(D.again(old2["id"]))["flight"]
    assert g["cursor"] == 0 and g["step_states"]["s1"]["state"] == "running"
    assert g["step_states"]["s1"]["detail"]["seeded"] is True
    row["activity"] = "working"
    run(D.step_flight(g["id"]))
    assert sent == []


def test_fix5_resume_never_types_a_sent_prompt_twice(srv, row, monkeypatch):
    sent = []
    monkeypatch.setattr(D, "_deliver", lambda title, text: sent.append(text))
    monkeypatch.setattr(D, "_session_busy", lambda title: "")
    p = plan(
        [{"kind": "notify", "title": "Hello", "config": {"text": "x"}}] + PROMPTED,
        "Lead",
    )
    f = F.take_off(p, session="s")

    def fn(x):
        x["step_states"]["s1"]["state"] = "done"
        x["cursor"] = 1
        x["step_states"]["s2"].update(state="running", detail={"sent_at": 1.0})
        F.op_cancel(x, F.SESSION_REMOVED)

    old = F.mutate(f["id"], fn)
    assert F.resume_point(p, [old])["in_progress"] is True
    g = run(D.start({"plan_id": p["id"], "title": "s", "start_at": "resume"}))
    assert g["cursor"] == 2 and g["step_states"]["s2"]["state"] == "skipped"
    assert sent == []


def test_fix6_carry_on_without_a_point_starts_where_an_attach_does(srv, held, row):
    srv.ENGINE.instances["pr-1"] = FakeInst("/wt/sc-1", BRANCH, "/base")
    p = plan(PROMPTED + [{"kind": "ship", "title": "Push", "config": {"lane": "push"}}])
    armed = F.arm_pending("sc-1", p["id"], TIX, branch=BRANCH)
    assert run(D.carry_on_intake("sc-1", "git@x:r", BRANCH)) == "pr-1"
    # The author's Agent stop has a prompt: it is sent, not skipped.
    f = F.get(armed["id"])
    assert f["cursor"] == 0 and f["step_states"]["s1"]["state"] == "pending"


def test_fix6_a_leftover_folder_with_changes_is_never_shipped_unseen(
    srv, held, row, monkeypatch
):
    async def fake_open(title, folder):
        srv.ENGINE.instances[title] = FakeInst(folder, BRANCH, "/base")
        return title

    monkeypatch.setattr(D, "_open_in_place", fake_open)
    monkeypatch.setattr(D, "_uncommitted", lambda folder: True)
    armed = F.arm_pending("sc-1", plan()["id"], TIX, branch=BRANCH)
    assert run(D.carry_on_intake("sc-1", "git@x:r", BRANCH)) == "sc-1"
    f = F.get(armed["id"])
    assert f["state"] == "paused" and f["cursor"] == 1
    assert f["reason"] == (
        "sc-1 was opened on /wt/sc-1, which has uncommitted changes no earlier "
        "run accounts for — look at them, then Carry on"
    )
    # Carry on is the person's go.
    after = F.mutate(armed["id"], F.op_resume)
    assert after["state"] == "running" and after["reason"] == ""
    # A clean leftover flies on.
    monkeypatch.setattr(D, "_uncommitted", lambda folder: False)
    F.mutate(armed["id"], lambda x: F.op_cancel(x, "x"))
    srv.ENGINE.instances.pop("sc-1")
    armed2 = F.arm_pending("sc-1", plan()["id"], TIX, branch=BRANCH)
    run(D.carry_on_intake("sc-1", "git@x:r", BRANCH))
    assert F.get(armed2["id"])["state"] == "running"


def test_a_perch_added_in_front_of_a_merge_is_not_one_of_the_routes_own_stops():
    from backend.web.core import flights as F

    steps = [
        {"id": "s1", "kind": "verify", "title": "Test locally"},
        {"id": "s2", "kind": "ship", "title": "Open PR"},
        {"id": "s4", "kind": "you", "title": "Your checks"},
        {"id": "s3", "kind": "ship", "title": "Merge"},
    ]
    flight = {
        "steps": steps,
        "cursor": 2,
        "step_states": {
            "s1": {"state": "done"},
            "s2": {"state": "done"},
            "s4": {"state": "waiting_you"},
            "s3": {"state": "pending"},
        },
        "owed_perch": "",
        "owed_added": ["s4"],
        "owed": [],
    }
    assert [s["id"] for s in F.own_steps(flight)] == ["s1", "s2", "s3"]
    # Standing on the added stop with nothing owed: the merge is next.
    assert F.carry_index(flight) == 2
    # Still owing the check: back to the Verify stop, which asks again.
    owing = dict(flight, owed=[{"step": "s1", "items": ["look"]}])
    assert F.carry_index(owing) == 0
