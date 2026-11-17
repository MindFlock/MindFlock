"""Finished sub-sessions close themselves (``backend.web.core.auto_close``).

Pinned here, one rule at a time:

* only a sub-session: agent-spawned and under a live parent (or orphaned by
  one) — never a person's own session, an adopted one, an intake session, an
  in-place / peer-shared one, a team-run member, one on a running route;
* finished: done/failed closes, blocked and no report stay, and anything sent
  to it since its report keeps it;
* accounted for: no commits of its own, merged into the parent's HEAD, or a
  merged/closed PR — unmerged and dirty work stays (real git repos);
* an orphan (parent gone, work unmerged) is kept and flagged;
* the grace window, typing, an attached terminal; Keep open; the setting;
* one server per config dir (the lease), and disk state, not "missing from
  my ENGINE", decides that a session or parent is gone;
* the pass: closes, logs, announces once per parent, Undo reopens + keeps.

No tmux, no real agent, no real ~/.mindflock (conftest points the stores at
tmp files): the server slice is faked.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from backend.config import settings as S
from backend.session.storage import Status
from backend.web.core import auto_close as ac
from backend.web.core import engine as engine_mod
from backend.web.core import events as events_mod
from backend.web.core import mailbox
from backend.web.core import sleep
from backend.web.core import team_runs

NOW = 1_800_000_000.0
GRACE = 10 * 60.0
CREATED = NOW - 3600.0


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #
class _GW:
    def __init__(self, base=""):
        self.base = base

    def GetBaseCommitSHA(self):
        return self.base


class _Inst:
    def __init__(
        self,
        title="w",
        *,
        parent="lead",
        spawned=True,
        branch="mf/w",
        status=Status.Ready,
        started=True,
        in_place=False,
        peer="",
        wt="/wt",
        base="",
        created=CREATED,
    ):
        self.Title = title
        self.Parent = parent
        self.Spawned = spawned
        self.Branch = branch
        self.Status = status
        self._started = started
        self.InPlace = in_place
        self.PeerShare = peer
        self._wt = wt
        self.Path = wt
        self._gw = _GW(base)
        self.Program = "claude"
        self.CreatedAt = (
            _dt.datetime.fromtimestamp(created).astimezone() if created else None
        )

    def Started(self):
        return self._started

    def GetWorktreePath(self):
        return self._wt

    def GetGitWorktree(self):
        return self._gw


class _Srv(SimpleNamespace):
    def __init__(self, *insts):
        super().__init__()
        self.ENGINE = SimpleNamespace(
            instances={i.Title: i for i in insts}, lock=threading.RLock()
        )
        self._HUMAN_INPUT_AT = {}
        self._QUEUE_STATE = {}
        self._MAIL_STATE = {}
        self.autopilot = set()
        self.tmux_typed = set()
        self.live = set()
        self.activity = {}
        self.pr = {}
        self.closed_store = []

    def _autopilot_running(self, title):
        return title in self.autopilot

    def _tmux_client_input_recent(self, title, within):
        return title in self.tmux_typed

    def _live_session_name(self, name):
        return name if name in {"mindflock_" + t for t in self.live} else None

    def _agent_activity(self, inst, title):
        return self.activity.get(title, "idle")

    def _background_jobs(self, inst, title):
        return None

    def _dialog_on_screen(self, inst, name):
        return False

    def _pr_info(self, wt, branch, force=False):
        return self.pr.get(branch)

    def _load_recently_closed(self):
        return list(self.closed_store)


def _row(title="w", **kw):
    r = {
        "title": title,
        "activity": "idle",
        "activity_since": NOW - 2 * GRACE,
        "last_report": {
            "id": "m1",
            "status": "done",
            "summary": "ok",
            "ts": NOW - 2 * GRACE,
        },
        "background": None,
        "asking": None,
        "sleeping": None,
        "workspace_missing": False,
        "hidden_by": "",
        "branch": "mf/" + title,
    }
    r.update(kw)
    return r


def _disk(*live, tombstoned=None):
    return engine_mod.DiskView(frozenset(live), dict(tombstoned or {}))


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    """No tmux, no team runs, no routes; git accounted unless a test says."""
    monkeypatch.setattr(sleep, "_outside_clients", lambda name: 0)
    monkeypatch.setattr(team_runs, "owner_of_title", lambda title, exclude_run="": None)
    monkeypatch.setattr(ac, "_route_running", lambda title, parent, created=None: False)
    monkeypatch.setattr(
        ac,
        "work_accounted",
        lambda srv, inst, title, row, parent, orphan: (True, ac.W_MERGED),
    )
    ac._VERDICTS.clear()
    yield
    ac._VERDICTS.clear()


def _world(*extra, worker=None):
    w = worker or _Inst()
    srv = _Srv(_Inst("lead", parent="", spawned=False, branch="mf/lead"), w, *extra)
    return srv, w


def _judge(srv, inst, row=None, *, now=NOW, disk=None, store=None):
    titles = tuple(srv.ENGINE.instances)
    return ac.judge(
        srv,
        inst,
        inst.Title,
        row if row is not None else _row(inst.Title),
        now,
        GRACE,
        disk=disk if disk is not None else _disk(*titles),
        store=store,
    )


# --------------------------------------------------------------------------- #
# 1. a sub-session
# --------------------------------------------------------------------------- #
def test_a_finished_merged_idle_worker_past_its_window_closes():
    srv, w = _world()
    assert _judge(srv, w) == (ac.CLOSE, ac.W_MERGED, None)


@pytest.mark.parametrize(
    "inst, why",
    [
        # The person's own session (no parent at all).
        (_Inst(parent="", spawned=False), ac.R_NOT_SUB),
        # A session the person started and an agent adopted (set_parent).
        (_Inst(spawned=False), ac.R_NOT_SUB),
        # An intake session: the pipeline's ticket branch.
        (_Inst("sc-123", branch="feature/sc-123/fix-it"), ac.R_INTAKE),
        (_Inst("pr-foo-12"), ac.R_INTAKE),
        (_Inst(in_place=True), ac.R_IN_PLACE),
        (_Inst(peer="link-1"), ac.R_PEER),
    ],
)
def test_only_agent_spawned_sub_sessions_are_closed(inst, why):
    srv, _ = _world(worker=inst)
    assert _judge(srv, inst)[:2] == (ac.KEEP, why)


def test_a_root_spawned_session_with_no_parent_record_is_not_a_sub_session():
    srv, w = _world(worker=_Inst(parent=""))
    assert _judge(srv, w)[:2] == (ac.KEEP, ac.R_NOT_SUB)


def test_a_features_hidden_helper_is_left_to_its_feature():
    srv, w = _world()
    assert _judge(srv, w, _row(hidden_by="verify"))[:2] == (ac.KEEP, ac.R_HIDDEN)


def test_a_team_run_member_and_a_routed_session_stay(monkeypatch):
    srv, w = _world()
    monkeypatch.setattr(
        team_runs, "owner_of_title", lambda title, exclude_run="": ({"id": "r"}, {})
    )
    assert _judge(srv, w)[:2] == (ac.KEEP, ac.R_TEAM)
    monkeypatch.setattr(team_runs, "owner_of_title", lambda title, exclude_run="": None)
    monkeypatch.setattr(ac, "_route_running", lambda title, parent, created=None: True)
    assert _judge(srv, w)[:2] == (ac.KEEP, ac.R_ROUTE)


def _flight(*steps):
    """A parent's route: ``steps`` are ``(id, kind, state, since, children)``."""
    return {
        "id": "f",
        "session": "lead",
        "state": "running",
        "cursor": next(
            (n for n, s in enumerate(steps) if s[2] in ("running", "waiting_you")),
            len(steps) - 1,
        ),
        "steps": [{"id": i, "kind": k, "config": {}} for i, k, *_ in steps],
        "step_states": {
            i: {"state": st, "since": since, "detail": {"children": kids}}
            for i, _k, st, since, kids in steps
        },
    }


def _routes(monkeypatch, by_session):
    from backend.web.core import flights

    monkeypatch.setattr(
        flights, "for_session", lambda t, unfinished_only=True: by_session.get(t)
    )


def test_route_running_reads_its_own_and_its_parents_flight(monkeypatch):
    monkeypatch.undo()  # the real _route_running
    running = _flight(("s1", "agent", "running", NOW - 100, []))
    _routes(monkeypatch, {"lead": running})
    # Spawned during the stop that is running now: the route's.
    assert ac._route_running("w", "lead", NOW - 50) is True
    assert ac._route_running("w", "") is False
    _routes(monkeypatch, {"w": {"id": "own"}})
    assert ac._route_running("w", "") is True


def test_a_worker_of_a_stop_the_route_passed_is_not_held(monkeypatch):
    """The owner's clutter: reviewers an agent stop spawned stayed open for
    hours while the route sat at a later stop waiting on PR comments."""
    monkeypatch.undo()
    later = _flight(
        ("s1c", "agent", "done", NOW - 900, [{"title": "w", "state": "done"}]),
        ("s4r", "reviews", "running", NOW - 300, []),
    )
    _routes(monkeypatch, {"lead": later})
    assert ac._route_running("w", "lead", NOW - 800) is False


def test_a_running_stop_that_names_the_worker_holds_it(monkeypatch):
    monkeypatch.undo()
    for kids in ([{"title": "w", "state": "running"}], ["w"]):
        f = _flight(
            ("s0", "agent", "done", NOW - 900, []),
            ("s1", "agent", "running", NOW - 100, kids),
        )
        _routes(monkeypatch, {"lead": f})
        # Spawned before this stop began, but it waits on it: held.
        assert ac._route_running("w", "lead", NOW - 800) is True
    parked = _flight(("s1", "review", "waiting_you", NOW - 100, ["w"]))
    _routes(monkeypatch, {"lead": parked})
    assert ac._route_running("w", "lead", NOW - 50) is True


def test_a_worker_with_open_workers_of_its_own_stays():
    sub = _Inst("w2", parent="w")
    srv, w = _world(sub)
    assert _judge(srv, w)[:2] == (ac.KEEP, ac.R_WORKERS)


# --------------------------------------------------------------------------- #
# 2. finished
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "report, verdict, why",
    [
        ({"status": "failed", "ts": NOW - 2 * GRACE}, ac.CLOSE, ac.W_MERGED),
        ({"status": "blocked", "ts": NOW - 2 * GRACE}, ac.KEEP, ac.R_BLOCKED),
        (None, ac.KEEP, ac.R_NO_REPORT),
    ],
)
def test_done_or_failed_closes_blocked_and_unreported_stay(report, verdict, why):
    srv, w = _world()
    assert _judge(srv, w, _row(last_report=report))[:2] == (verdict, why)


@pytest.mark.parametrize("clock", ["_MAIL_STATE", "_QUEUE_STATE", "_HUMAN_INPUT_AT"])
def test_anything_sent_since_its_report_keeps_it(clock):
    srv, w = _world()
    at = NOW - GRACE  # after the report (NOW - 2 * GRACE)
    getattr(srv, clock)["w"] = at if clock == "_HUMAN_INPUT_AT" else {"sent_at": at}
    assert _judge(srv, w)[:2] == (ac.KEEP, ac.R_NEW_WORK)


@pytest.mark.parametrize(
    "row, why",
    [
        (_row(activity="working"), ac.R_BUSY),
        (_row(activity="clarify"), ac.R_BUSY),
        (_row(activity="limit"), ac.R_BUSY),
        (_row(activity_since=0), ac.R_BUSY),
        (_row(asking={"kind": "question"}), ac.R_ASKING),
        (_row(background=[{"pid": 1, "state": "running"}]), ac.R_BACKGROUND),
        (_row(workspace_missing=True), ac.R_WORKSPACE),
    ],
)
def test_an_agent_that_is_not_settled_stays(row, why):
    srv, w = _world()
    assert _judge(srv, w, row)[:2] == (ac.KEEP, why)


def test_queued_prompts_keep_it():
    from backend.web.core import prompt_queue

    srv, w = _world()
    prompt_queue.enqueue("w", "and one more thing")
    assert _judge(srv, w)[:2] == (ac.KEEP, ac.R_QUEUE)


def test_waiting_mail_keeps_it(monkeypatch):
    srv, w = _world()
    monkeypatch.setattr(mailbox, "next_pending", lambda title: {"id": "m"})
    assert _judge(srv, w)[:2] == (ac.KEEP, ac.R_MAIL)


def test_shipping_by_itself_keeps_it():
    srv, w = _world()
    srv.autopilot.add("w")
    assert _judge(srv, w)[:2] == (ac.KEEP, ac.R_AUTOPILOT)


def test_an_offline_or_sleeping_finished_worker_still_closes():
    srv, w = _world()
    assert _judge(srv, w, _row(activity="offline"))[0] == ac.CLOSE
    assert (
        _judge(
            srv, w, _row(activity="offline", sleeping={"since": 1}, activity_since=0)
        )[0]
        == ac.CLOSE
    )


def test_paused_or_provisioning_stays():
    srv, w = _world(worker=_Inst(status=Status.Paused))
    assert _judge(srv, w)[:2] == (ac.KEEP, ac.R_STATUS)


# --------------------------------------------------------------------------- #
# 3. accounted for — real git
# --------------------------------------------------------------------------- #
def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A repo (the parent's checkout) with one commit, and a worker worktree
    forked from it on branch ``mf/w``."""
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q", "-b", "main")
    _git(main, "config", "user.email", "t@example.com")
    _git(main, "config", "user.name", "t")
    (main / "a.txt").write_text("a\n")
    _git(main, "add", "a.txt")
    _git(main, "commit", "-qm", "base")
    base = _git(main, "rev-parse", "HEAD")
    wt = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", "-b", "mf/w", str(wt))
    return SimpleNamespace(main=str(main), wt=str(wt), base=base)


def _accounted(repo, row=None, srv=None, orphan=None, parent="lead"):
    from backend.web.core.auto_close import work_accounted

    inst = _Inst(wt=repo.wt, base=repo.base)
    lead = _Inst("lead", parent="", spawned=False, wt=repo.main)
    srv = srv or _Srv(lead, inst)
    return work_accounted(srv, inst, "w", row or _row(), parent, orphan)


def test_no_commits_of_its_own_is_accounted_for(repo, monkeypatch):
    monkeypatch.undo()
    assert _accounted(repo) == (True, ac.W_NO_COMMITS)


def test_merged_into_the_parents_head_is_accounted_for(repo, monkeypatch):
    monkeypatch.undo()
    (open(os.path.join(repo.wt, "b.txt"), "w")).write("b\n")
    _git(repo.wt, "add", "b.txt")
    _git(repo.wt, "commit", "-qm", "work")
    assert _accounted(repo) == (False, ac.R_UNMERGED)  # unmerged: stays open
    _git(repo.main, "merge", "-q", "--no-edit", "mf/w")
    assert _accounted(repo) == (True, ac.W_MERGED)


def test_uncommitted_changes_keep_it(repo, monkeypatch):
    monkeypatch.undo()
    with open(os.path.join(repo.wt, "a.txt"), "a") as f:
        f.write("more\n")
    assert _accounted(repo) == (False, ac.R_DIRTY)


def test_a_separate_workers_merged_or_closed_pr_accounts_for_it(repo, monkeypatch):
    monkeypatch.undo()
    with open(os.path.join(repo.wt, "b.txt"), "w") as f:
        f.write("b\n")
    _git(repo.wt, "add", "b.txt")
    _git(repo.wt, "commit", "-qm", "separate idea")
    inst = _Inst(wt=repo.wt, base=repo.base)
    srv = _Srv(_Inst("lead", parent="", spawned=False, wt=repo.main), inst)
    srv.pr["mf/w"] = {"state": "CLOSED"}
    assert _accounted(repo, srv=srv) == (True, ac.W_PR_CLOSED)
    srv.pr["mf/w"] = {"state": "MERGED", "head": _git(repo.wt, "rev-parse", "HEAD")}
    assert _accounted(repo, srv=srv) == (True, ac.W_PR_MERGED)
    srv.pr["mf/w"] = {"state": "OPEN"}
    assert _accounted(repo, srv=srv) == (False, ac.R_UNMERGED)


def test_an_orphans_merge_is_judged_against_its_gone_parents_branch(repo, monkeypatch):
    monkeypatch.undo()
    with open(os.path.join(repo.wt, "b.txt"), "w") as f:
        f.write("b\n")
    _git(repo.wt, "add", "b.txt")
    _git(repo.wt, "commit", "-qm", "work")
    _git(repo.main, "merge", "-q", "--no-edit", "mf/w")
    inst = _Inst(wt=repo.wt, base=repo.base)
    srv = _Srv(inst)  # the parent is gone from the registry
    orphan = {"parent": "lead", "parent_branch": "main", "parent_folder": "/gone"}
    assert _accounted(repo, srv=srv, orphan=orphan, parent="") == (True, ac.W_MERGED)


def test_unmerged_or_dirty_work_keeps_the_worker_open(monkeypatch):
    srv, w = _world()
    for why in (ac.R_UNMERGED, ac.R_DIRTY):
        monkeypatch.setattr(ac, "work_accounted", lambda *a, why=why: (False, why))
        assert _judge(srv, w)[:2] == (ac.KEEP, why)


# --------------------------------------------------------------------------- #
# orphans
# --------------------------------------------------------------------------- #
def test_a_parent_leaving_records_its_spawned_children_with_their_reports():
    w = _Inst()
    mine = _Inst("mine", spawned=False)  # adopted: the person's
    lead = _Inst("lead", parent="", spawned=False, branch="mf/lead", wt="/lead")
    mailbox.post(
        "lead",
        "all done",
        sender="w",
        kind="result",
        data={"status": "done"},
        now=NOW - 100,
    )
    got = ac.note_parent_gone({"lead": lead, "w": w, "mine": mine}, "lead", lead)
    assert got == ["w"]
    rec = ac._load()["orphans"]["w"]
    assert rec["parent"] == "lead" and rec["parent_branch"] == "mf/lead"
    assert rec["parent_folder"] == "/lead" and rec["report"]["status"] == "done"
    assert ac.orphan_of("w", ac._created(w)) == "lead"
    # A namesake created later inherits nothing.
    assert ac.orphan_of("w", ac._created(w) + 100) == ""


def test_an_orphan_with_unmerged_work_is_kept_and_flagged(monkeypatch):
    w = _Inst(parent="")  # its parent's close cleared the link
    srv = _Srv(w)
    lead = _Inst("lead", parent="", spawned=False)
    mailbox.post(
        "lead",
        "done",
        sender="w",
        kind="result",
        data={"status": "done"},
        now=NOW - 2 * GRACE,
    )
    ac.note_parent_gone({"lead": lead, "w": _Inst()}, "lead", lead)
    monkeypatch.setattr(ac, "work_accounted", lambda *a: (False, ac.R_UNMERGED))
    row = _row(last_report=None)  # its report went with the parent's box
    assert _judge(srv, w, row)[:2] == (ac.ORPHAN, ac.R_UNMERGED)
    monkeypatch.setattr(ac, "_server", lambda: srv)
    field = ac.row_field(w)
    assert field["orphan_of"] == "lead" and field["keep_open"] is False


def test_an_orphan_with_nothing_unmerged_closes_on_its_recorded_report():
    w = _Inst(parent="")
    srv = _Srv(w)
    lead = _Inst("lead", parent="", spawned=False)
    mailbox.post(
        "lead",
        "done",
        sender="w",
        kind="result",
        data={"status": "done"},
        now=NOW - 2 * GRACE,
    )
    ac.note_parent_gone({"lead": lead, "w": _Inst()}, "lead", lead)
    assert _judge(srv, w, _row(last_report=None))[0] == ac.CLOSE


def test_a_parent_missing_from_my_engine_but_not_tombstoned_is_not_gone():
    """Two servers: the parent may live in the other one's ENGINE (or be too
    new for state.json). Only a tombstone on disk says it left."""
    w = _Inst(parent="lead")
    srv = _Srv(w)  # "lead" is not in THIS server's ENGINE
    assert _judge(srv, w, disk=_disk("w", "lead"))[:2] == (
        ac.KEEP,
        ac.R_PARENT_ELSEWHERE,
    )
    assert _judge(srv, w, disk=_disk("w"))[:2] == (ac.KEEP, ac.R_PARENT_ELSEWHERE)
    # Tombstoned: gone — an orphan, flagged (its report is unknown).
    verdict, why, _ = _judge(srv, w, disk=_disk("w", tombstoned={"lead": NOW}))
    assert (verdict, why) == (ac.ORPHAN, ac.R_NO_REPORT)


def test_a_worker_not_live_on_disk_is_never_closed():
    """Closed or deleted by the other server meanwhile: leave it."""
    srv, w = _world()
    assert _judge(srv, w, disk=_disk("lead"))[:2] == (ac.KEEP, ac.R_GONE)


# --------------------------------------------------------------------------- #
# 4. the grace window, and nobody looking
# --------------------------------------------------------------------------- #
def test_inside_the_grace_window_it_waits_and_says_when():
    srv, w = _world()
    rep_ts = NOW - 60.0
    row = _row(last_report={"status": "done", "ts": rep_ts}, activity_since=rep_ts)
    assert _judge(srv, w, row) == (ac.WAIT, ac.R_GRACE, rep_ts + GRACE)


def test_the_window_runs_from_its_turn_ending_when_that_is_later():
    srv, w = _world()
    row = _row(activity_since=NOW - 30.0)
    verdict, why, at = _judge(srv, w, row)
    assert (verdict, why, at) == (ac.WAIT, ac.R_GRACE, NOW - 30.0 + GRACE)


def test_a_person_typing_recently_or_a_terminal_attached_holds_it(monkeypatch):
    from backend.web.core import ownership

    srv, w = _world()
    srv.tmux_typed.add("w")
    assert _judge(srv, w)[:2] == (ac.WAIT, ac.R_TYPED)
    srv.tmux_typed.clear()
    monkeypatch.setattr(ownership, "terminals", lambda title: 1)
    assert _judge(srv, w)[:2] == (ac.WAIT, ac.R_VIEWED)
    monkeypatch.setattr(ownership, "terminals", lambda title: 0)
    monkeypatch.setattr(sleep, "_outside_clients", lambda name: 1)
    assert _judge(srv, w)[:2] == (ac.WAIT, ac.R_VIEWED)


# --------------------------------------------------------------------------- #
# opt-outs
# --------------------------------------------------------------------------- #
def test_keep_open_is_honoured_and_reversible():
    srv, w = _world()
    ac.set_keep_open("w", True, ac._created(w))
    assert ac.keep_open("w", ac._created(w))
    assert _judge(srv, w)[:2] == (ac.KEEP, ac.R_KEEP_OPEN)
    ac.set_keep_open("w", False)
    assert _judge(srv, w)[0] == ac.CLOSE


def test_settings_default_on_with_a_ten_minute_window():
    assert ac.enabled() is True
    assert ac.grace_seconds() == 600.0
    g = S.GeneralSettings.from_dict(
        {"auto_close_children": False, "auto_close_grace_minutes": 3}
    )
    assert g.to_dict()["auto_close_children"] is False
    assert g.to_dict()["auto_close_grace_minutes"] == 3
    assert "auto_close_children" not in S.GeneralSettings().to_dict()
    assert (
        S.GeneralSettings.from_dict(
            {"auto_close_grace_minutes": 0}
        ).auto_close_grace_minutes
        is None
    )


# --------------------------------------------------------------------------- #
# the pass
# --------------------------------------------------------------------------- #
def _tick_world(monkeypatch, *workers, rows=None):
    srv = _Srv(_Inst("lead", parent="", spawned=False), *workers)
    rows = rows or [_row(w.Title) for w in workers]
    monkeypatch.setattr(events_mod, "sessions_snapshot", lambda: rows)
    monkeypatch.setattr(engine_mod, "disk_view", lambda: _disk(*srv.ENGINE.instances))
    ended = []

    def _end(engine, title, wipe):
        assert wipe is False  # close, never delete
        ended.append(title)
        srv.closed_store.insert(0, {"id": title + "-1", "title": title})
        engine.instances.pop(title, None)
        return {"title": title, "wiped": False, "children": []}

    from backend.web.core import session_end

    monkeypatch.setattr(session_end, "end_session", _end)
    emitted = []
    monkeypatch.setattr(
        events_mod.BUS, "emit", lambda kind, **kw: emitted.append((kind, kw)) or kw
    )
    srv.ended, srv.emitted = ended, emitted
    return srv


def test_a_pass_closes_finished_workers_and_announces_once_per_parent(monkeypatch):
    srv = _tick_world(monkeypatch, _Inst("w1"), _Inst("w2"), _Inst("w3"))
    srv.activity["w3"] = "working"  # its live probe says a turn began
    srv.live.add("w3")
    assert ac.tick(srv, NOW) == ["w1", "w2"]
    assert srv.ended == ["w1", "w2"]
    notices = [kw for kind, kw in srv.emitted if kind == ac.EVENT]
    assert len(notices) == 1
    data = notices[0]["data"]
    assert notices[0]["session"] == "lead" and data["count"] == 2
    assert data["entries"] == [
        {"title": "w1", "id": "w1-1"},
        {"title": "w2", "id": "w2-1"},
    ]
    assert data["detail"] == "Closed 2 finished workers of lead: w1, w2."
    assert ac._VERDICTS["w3"]["why"] == ac.R_BUSY


def test_a_waiting_worker_shows_when_it_closes_on_its_row(monkeypatch):
    rep = {"status": "done", "ts": NOW - 60.0}
    srv = _tick_world(
        monkeypatch, _Inst(), rows=[_row(last_report=rep, activity_since=NOW - 60.0)]
    )
    monkeypatch.setattr(ac, "_server", lambda: srv)
    assert ac.tick(srv, NOW) == []
    assert ac.row_field(srv.ENGINE.instances["w"])["closes_at"] == NOW - 60.0 + GRACE
    # A person's own session has no auto_close field at all.
    assert ac.row_field(srv.ENGINE.instances["lead"]) is None


def test_the_setting_off_closes_nothing(monkeypatch):
    S.save_settings(S.Settings.from_dict({"general": {"auto_close_children": False}}))
    srv = _tick_world(monkeypatch, _Inst())
    assert ac.enabled() is False
    assert ac.tick(srv, NOW) == [] and srv.ended == []


def test_the_sweep_runs_in_one_server_only(monkeypatch):
    """The other server holds the lease: this one closes nothing."""
    srv = _tick_world(monkeypatch, _Inst())
    os.makedirs(os.path.dirname(sleep._lease_path()), exist_ok=True)
    with open(sleep._lease_path(), "w") as f:
        json.dump(
            {
                ac.LEASE: {
                    "owner": "the other server",
                    "pid": os.getppid(),
                    "until": time.time() + 600,
                }
            },
            f,
        )
    assert ac.tick(srv, NOW) == [] and srv.ended == []
    with open(sleep._lease_path(), "w") as f:
        json.dump({}, f)  # it shut down and handed the lease on
    assert ac.tick(srv, NOW) == ["w"]


def test_a_pass_closes_at_most_max_per_pass(monkeypatch):
    workers = [_Inst("w%d" % i) for i in range(ac.MAX_PER_PASS + 2)]
    srv = _tick_world(monkeypatch, *workers)
    assert len(ac.tick(srv, NOW)) == ac.MAX_PER_PASS


def test_undo_reopens_and_keeps_open():
    import asyncio

    reopened = []

    class _Resp:
        def __init__(self, status, body):
            self.status_code, self.body = status, json.dumps(body).encode()

    async def _reopen(eid):
        if eid == "bad":
            return _Resp(410, {"error": "workspace directory no longer exists"})
        reopened.append(eid)
        return _Resp(200, {"title": "w"})

    srv = _Srv(_Inst())
    srv._reopen_closed_entry = _reopen
    out = asyncio.run(ac.undo(["w-1", "bad"], srv))
    assert out == {
        "reopened": ["w"],
        "failed": {"bad": "workspace directory no longer exists"},
    }
    assert ac.keep_open("w", ac._created(srv.ENGINE.instances["w"]))


def test_rename_and_forget_move_and_drop_the_records():
    ac.set_keep_open("w", True, None)
    ac.rename("w", "w-renamed")
    assert ac.keep_open("w-renamed") and not ac.keep_open("w")
    ac.forget("w-renamed")
    assert not ac.keep_open("w-renamed")


def test_undo_route_wants_ids():
    from fastapi.testclient import TestClient

    from backend.web import server

    r = TestClient(server.app).post("/api/auto-close/undo", json={})
    assert r.status_code == 400


def test_a_store_transaction_that_fails_to_open_lets_go_of_the_lock(monkeypatch):
    """A _Txn whose open raises never reaches __exit__: it must release the
    process lock itself, or that thread keeps the RLock for good and the next
    removal on another thread (session_end.reparent_children →
    note_parent_gone) blocks forever. CI hung exactly so, once a thread that
    had leaked it was gone."""

    def boom(_p):
        raise OSError("disk went away")

    monkeypatch.setattr(ac, "_read", boom)
    with pytest.raises(OSError):
        with ac._Txn():
            pass  # pragma: no cover — never entered
    monkeypatch.undo()

    got = []

    def other_thread():
        ok = ac._LOCK.acquire(timeout=2)
        got.append(ok)
        if ok:
            ac._LOCK.release()

    t = threading.Thread(target=other_thread)
    t.start()
    t.join(5)
    assert got == [True]


def test_a_busy_store_lock_never_hangs_a_removal(monkeypatch):
    """A holder that never lets go costs the orphan record, not the close."""
    monkeypatch.setattr(ac, "_TXN_WAIT_S", 0.2)
    held = threading.Event()
    done = threading.Event()

    def hold():
        with ac._LOCK:
            held.set()
            done.wait(5)

    t = threading.Thread(target=hold)
    t.start()
    try:
        assert held.wait(2)
        kid = SimpleNamespace(Parent="p", Spawned=True, Title="kid")
        start = time.monotonic()
        assert ac.note_parent_gone({"p": object(), "kid": kid}, "p") == []
        assert time.monotonic() - start < 3
    finally:
        done.set()
        t.join(5)
