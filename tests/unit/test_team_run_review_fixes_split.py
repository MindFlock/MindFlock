"""Regression tests for the team-runs review (2026-10-05): one-for-all groups
and splits, against REAL git worktrees (the split driver's ``env``).

Pinned here: the release ships exactly what the card and the check showed;
"Open the PR" never merges; a check of an older HEAD is never credited to a
newer one; an adopted lead's own lane is turned off; the merge queue never
hangs silently (a stale MERGE_HEAD, a lead that wandered off its branch); a
pause really stops a releasing lead.
"""

from __future__ import annotations

import os
import time

import pytest

from backend.web.core import autopilot as ap
from backend.web.core import git_merge as gm
from backend.web.core import lanes
from backend.web.core import team_run_driver as drv
from backend.web.core import team_runs as tr
from tests.unit.test_team_run_split_driver import (  # noqa: F401 — the fixture
    PLAN,
    _approved,
    _commit,
    _done,
    _git,
    _lead_idle,
    _split,
    _step,
    _wt,
    _WtInst,
    env,
)


def _release_ready(env, rid, lead):
    """Every piece merged, checked at the lead's HEAD, the card prepared."""
    head = gm.rev_parse(_wt(env, lead), "HEAD")
    with tr.edit(rid) as r:
        for t in r["tasks"]:
            t["state"] = "integrated"
        r["state"] = "release_ready"
        r["check"] = tr._normalize_check({"state": "ok", "sha": head})
        r["release"] = tr._normalize_release(
            {"state": "ready", "title": "T", "body": "B", "head_sha": head}
        )
    _lead_idle(env, lead)
    return head


def _clock(monkeypatch, offset):
    real = time.time
    monkeypatch.setattr(drv.time, "time", lambda: real() + offset)


# --------------------------------------------------------------------------- #
# Finding 2: "Open the PR" never merges
# --------------------------------------------------------------------------- #
class TestOpenThePrNeverMerges:
    def test_open_the_pr_on_a_merge_lane_group_opens_a_pr_only(self, env):
        run = _split(env, policy={"lane": "merge", "release": "ask"})
        lead = run["lead"]["title"]
        drv.propose_plan(run["id"], {"pieces": PLAN, "from": lead})
        drv.approve_plan(run["id"])
        _release_ready(env, run["id"], lead)
        drv.release(run["id"], merge_when_green=False)
        assert ap.get(lead)["lane"] == "pr"

    def test_the_explicit_merge_choice_merges(self, env):
        rid, lead = _approved(env)
        _release_ready(env, rid, lead)
        drv.release(rid, merge_when_green=True)
        assert ap.get(lead)["lane"] == "merge"

    def test_a_push_group_pushes_and_opens_nothing(self):
        assert drv.release_lane({"policy": {"lane": "push"}}) == "push"
        assert drv.release_lane({"policy": {"lane": "merge"}}) == "pr"
        assert drv.release_lane({"policy": {"lane": "pr"}}, True) == "merge"


# --------------------------------------------------------------------------- #
# Finding 7: the release ships exactly what was checked and shown
# --------------------------------------------------------------------------- #
class TestReleaseShipsWhatWasChecked:
    def test_a_dirty_lead_is_refused_and_nothing_is_armed(self, env):
        rid, lead = _approved(env)
        _release_ready(env, rid, lead)
        with open(os.path.join(_wt(env, lead), "auth/tokens.py"), "w") as f:
            f.write("half-done edit\n")
        with pytest.raises(drv.RunError) as err:
            drv.release(rid)
        assert err.value.status == 409 and "uncommitted" in err.value.message
        assert ap.get(lead) is None
        assert tr.load(rid)["state"] == "release_ready"

    def test_a_working_lead_is_refused(self, env):
        rid, lead = _approved(env)
        _release_ready(env, rid, lead)
        env.rows[lead] = {"title": lead, "activity": "working", "activity_since": 1.0}
        with pytest.raises(drv.RunError) as err:
            drv.release(rid)
        assert "mid-turn" in err.value.message
        assert ap.get(lead) is None

    def test_a_head_that_moved_since_the_check_goes_back_to_checking(self, env):
        rid, lead = _approved(env)
        _release_ready(env, rid, lead)
        _commit(env, lead, "auth/tokens.py", "T = 9\n", "lead tweak")
        with pytest.raises(drv.RunError) as err:
            drv.release(rid)
        assert "moved since it was checked" in err.value.message
        r = tr.load(rid)
        assert r["state"] == "checking" and r["check"]["state"] == "pending"
        assert r["release"]["state"] == "none"
        assert ap.get(lead) is None

    def test_a_paused_group_is_not_released(self, env):
        rid, lead = _approved(env)
        _release_ready(env, rid, lead)
        drv.pause(rid)
        with pytest.raises(drv.RunError) as err:
            drv.release(rid)
        assert "paused" in err.value.message
        assert ap.get(lead) is None


# --------------------------------------------------------------------------- #
# Finding 8: a check of the old HEAD is never credited to a newer commit
# --------------------------------------------------------------------------- #
def test_a_commit_during_the_check_runs_it_again(env):
    rid, lead = _approved(env)
    wt = _wt(env, lead)
    with open(os.path.join(wt, ".mindflock.toml"), "w") as f:
        f.write('[workspace]\ncheck_command = "sleep 0.3; echo 1 passed"\n')
    _git(wt, "add", ".mindflock.toml")
    _git(wt, "commit", "-qm", "cfg")
    _lead_idle(env, lead)
    with tr.edit(rid) as rr:
        for t in rr["tasks"]:
            t["state"] = "integrated"
    r = _step(rid)
    assert r["state"] == "checking"
    started = r["check"]["sha"]
    new = _commit(env, lead, "auth/tokens.py", "T = 9\n", "lead tweak")
    assert new != started
    for _ in range(40):
        st = (drv._server()._wt_setup.check_status(wt) or {}).get("state")
        if st in ("ok", "failed"):
            break
        time.sleep(0.05)
    r = _step(rid)
    # Not release_ready on a pass that belongs to `started`: it runs again.
    assert r["state"] == "checking"
    assert r["check"]["sha"] == new and r["check"]["state"] == "running"


# --------------------------------------------------------------------------- #
# Finding 9: Split… on an armed session turns its own lane off
# --------------------------------------------------------------------------- #
def test_an_adopted_lead_with_an_armed_lane_is_disarmed(env):
    lead_path = str(env.tmp / "wt" / "mine")
    _git(env.repo, "worktree", "add", "-q", "-b", "me/mine", lead_path, "main")
    env.instances["mine"] = _WtInst("mine", lead_path, "me/mine", env.repo)
    lanes.arm_session("mine", "pr")  # the user's ⏩ on that session
    run = _split(env, repo_path="", lead="mine")
    assert ap.get("mine") is None
    assert any("own lane was turned off" in e["text"] for e in run["events"])


# --------------------------------------------------------------------------- #
# Finding 10: the merge queue never hangs silently
# --------------------------------------------------------------------------- #
class TestNoSilentHang:
    def _conflicted(self, env):
        rid, lead = _approved(env)
        _step(rid)
        r = _step(rid)
        t1, t2 = [t["title"] for t in r["tasks"]]
        _lead_idle(env, lead)
        _commit(env, t1, "auth/session.py", "S = 'a'\n", "a")
        _commit(env, t2, "auth/session.py", "S = 'b'\n", "b")
        _done(env, t1)
        _done(env, t2)
        for _ in range(4):
            r = _step(rid)
            if any(t["reason"] == "conflict" for t in r["tasks"]):
                break
        ci = 0 if r["tasks"][0]["reason"] == "conflict" else 1
        return rid, lead, ci, r["tasks"][ci]["title"]

    def test_a_stale_merge_head_left_by_the_lead_escalates(self, env, monkeypatch):
        rid, lead, ci, t2 = self._conflicted(env)
        wt = _wt(env, lead)
        # The lead starts resolving the hand-off, then stops (asks, gives up).
        _git(wt, "merge", "--no-ff", "mf/" + t2, check=False)
        assert gm.merge_in_progress(wt)
        _lead_idle(env, lead, since=time.time() - 10_000)
        r = _step(rid)
        assert r["tasks"][ci]["state"] == "integrating"  # the clock starts
        _clock(monkeypatch, tr.MERGE_BLOCKED_S + 5)
        r = _step(rid)
        t = r["tasks"][ci]
        assert t["state"] == "needs_you" and t["reason"] == "conflict"
        assert "MERGE_HEAD" in t["detail"]

    def test_a_merge_the_server_died_in_is_unwound(self, env):
        rid, lead = _approved(env)
        wt = _wt(env, lead)
        _git(wt, "checkout", "-q", "-b", "tmp-other")
        (open(os.path.join(wt, "auth/session.py"), "w")).write("S = 'x'\n")
        _git(wt, "commit", "-qam", "x")
        _git(wt, "checkout", "-q", "mf/" + lead)
        (open(os.path.join(wt, "auth/session.py"), "w")).write("S = 'y'\n")
        _git(wt, "commit", "-qam", "y")
        # The server's own merge, interrupted before its --abort.
        marker = gm._git_path(wt, gm._MARKER)
        with open(marker, "w") as f:
            f.write("tmp-other\n")
        _git(wt, "merge", "--no-ff", "tmp-other", check=False)
        assert gm.merge_in_progress(wt)
        assert gm.recover_interrupted(wt) is True
        assert not gm.merge_in_progress(wt) and not os.path.exists(marker)
        # A merge someone ELSE started (no marker) is never touched.
        _git(wt, "merge", "--no-ff", "tmp-other", check=False)
        assert gm.recover_interrupted(wt) is False and gm.merge_in_progress(wt)


# --------------------------------------------------------------------------- #
# Finding 11: the merge queue follows the group's PINNED branch
# --------------------------------------------------------------------------- #
class TestPinnedBranch:
    def _one_done(self, env):
        rid, lead = _approved(env)
        _step(rid)
        r = _step(rid)
        t1 = r["tasks"][0]["title"]
        return rid, lead, t1

    def test_a_detached_lead_takes_no_merges_and_escalates(self, env, monkeypatch):
        rid, lead, t1 = self._one_done(env)
        wt = _wt(env, lead)
        _git(wt, "checkout", "-q", "--detach")
        _lead_idle(env, lead)
        _commit(env, t1, "auth/tokens.py", "T = 2\n", "tok")
        _done(env, t1)
        for _ in range(3):
            r = _step(rid)
        assert r["tasks"][0]["state"] == "integrating"
        assert not gm.merge_in_progress(wt)
        assert r["lead"]["branch"] == "mf/" + lead  # pinned, not followed
        piece = gm.rev_parse(wt, "mf/" + t1)
        assert gm.is_ancestor(wt, piece, "HEAD") is False
        _clock(monkeypatch, tr.MERGE_BLOCKED_S + 5)
        r = _step(rid)
        assert r["tasks"][0]["state"] == "needs_you"
        assert "detached HEAD" in r["tasks"][0]["detail"]

    def test_a_branch_switch_is_not_followed(self, env):
        rid, lead, t1 = self._one_done(env)
        wt = _wt(env, lead)
        _git(wt, "checkout", "-q", "-b", "scratch-experiment")
        _lead_idle(env, lead)
        _commit(env, t1, "auth/tokens.py", "T = 2\n", "tok")
        _done(env, t1)
        for _ in range(3):
            r = _step(rid)
        assert r["lead"]["branch"] == "mf/" + lead
        assert r["tasks"][0]["state"] == "integrating"
        res = gm.merge_into(wt, "mf/" + t1, expect_branch="mf/" + lead)
        assert res["result"] == "refused" and "scratch-experiment" in res["error"]


# --------------------------------------------------------------------------- #
# Finding 15: a pause really stops a releasing lead
# --------------------------------------------------------------------------- #
def test_pause_during_releasing_disarms_the_lead(env):
    rid, lead = _approved(env)
    _release_ready(env, rid, lead)
    drv.release(rid)
    assert ap.get(lead)["state"] == "running"
    drv.pause(rid)
    assert ap.get(lead) is None
    r = tr.load(rid)
    assert r["state"] == "release_ready" and r["release"]["state"] == "ready"


def test_a_paused_group_merges_nothing(env):
    rid, lead = _approved(env)
    _step(rid)
    r = _step(rid)
    t1 = r["tasks"][0]["title"]
    _lead_idle(env, lead)
    _commit(env, t1, "auth/tokens.py", "T = 2\n", "tok")
    _done(env, t1)
    with tr.edit(rid) as rr:  # waiting in the merge queue when the pause lands
        rr["tasks"][0].update(state="integrating", ready_at=time.time())
    drv.pause(rid)
    for _ in range(3):
        r = _step(rid)
    assert r["tasks"][0]["state"] == "integrating"
    piece = gm.rev_parse(_wt(env, lead), "mf/" + t1)
    assert gm.is_ancestor(_wt(env, lead), piece, "HEAD") is False


# --------------------------------------------------------------------------- #
# Finding 14: retrying a vanished member never orphans its commits
# --------------------------------------------------------------------------- #
def test_retry_of_a_vanished_member_merges_its_branch_back(env):
    import asyncio

    rid, lead = _approved(env)
    _step(rid)
    r = _step(rid)
    t1 = r["tasks"][0]["title"]
    piece = _commit(env, t1, "auth/tokens.py", "T = 5\n", "tokens: rotate")
    # Its session vanishes (a restart lost it); the branch keeps the commit.
    env.instances.pop(t1)
    with tr.edit(rid) as rr:
        tr.apply(
            rr,
            tr._to(rr["tasks"][0], "needs_you", "restart", "its session is gone"),
            time.time(),
        )
    drv.retry(rid, "t1", fresh=False)
    t = tr.load(rid)["tasks"][0]
    assert t["state"] == "integrating" and t["title"] == t1
    assert t["commits"] == ["tokens: rotate"]
    _lead_idle(env, lead)
    for _ in range(3):
        r = _step(rid)
    assert r["tasks"][0]["state"] == "integrated"
    assert gm.is_ancestor(_wt(env, lead), piece, "HEAD") is True
    assert asyncio  # (imported for parity with the other split tests)


# --------------------------------------------------------------------------- #
# Finding 21: a line added once a group is checking/ready is merged and
# checked before the release — never left queued while it ships without it
# --------------------------------------------------------------------------- #
def test_a_line_added_at_release_ready_reopens_the_group(env):
    import asyncio

    rid, lead = _approved(env)
    _release_ready(env, rid, lead)
    asyncio.run(drv.add_tasks(rid, [{"kind": "task", "text": "late line"}]))
    r = tr.load(rid)
    assert r["state"] == "running"
    assert r["release"]["state"] == "none" and r["check"]["state"] == "pending"
    _lead_idle(env, lead)
    r = _step(rid)
    assert r["tasks"][-1]["state"] == "starting"
    with pytest.raises(drv.RunError):
        drv.release(rid)


# --------------------------------------------------------------------------- #
# Finding 12: report_integrated never marks unfinished work merged back
# --------------------------------------------------------------------------- #
class TestReportIntegrated:
    def test_a_member_still_working_is_refused(self, env):
        rid, lead = _approved(env)
        _step(rid)
        _step(rid)
        with tr.edit(rid) as rr:
            rr["tasks"][0].update(state="needs_you", reason="blocked", detail="asks")
        with pytest.raises(drv.RunError) as err:
            drv.report_integrated(rid, "t1", "HEAD", lead)
        assert "not waiting to be merged" in err.value.message
        assert tr.load(rid)["tasks"][0]["state"] == "needs_you"

    def test_a_member_with_no_commits_is_refused(self, env):
        rid, lead = _approved(env)
        _step(rid)
        r = _step(rid)
        with tr.edit(rid) as rr:
            rr["tasks"][0].update(state="integrating")
        with pytest.raises(drv.RunError) as err:
            drv.report_integrated(rid, "t1", "HEAD", lead)
        assert "no commits of its own" in err.value.message
        assert tr.load(rid)["tasks"][0]["state"] == "integrating"
        assert r  # (the pieces were started)

    def test_no_sender_is_not_the_user(self, env):
        rid, lead = _approved(env)
        with tr.edit(rid) as rr:
            rr["tasks"][0].update(state="integrating")
        with pytest.raises(drv.RunError) as err:
            drv.report_integrated(rid, "t1", "HEAD", "")
        assert "only the group's lead" in err.value.message


# --------------------------------------------------------------------------- #
# Finding 29: fencing holes
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "a,b",
    [
        ("src/newmod/**", "src/newmod/*.py"),
        ("src/**", "src/x/*.py"),
        ("tests/**", "**/*.py"),
    ],
)
def test_two_globs_over_files_not_created_yet_overlap(a, b):
    pieces = [
        {"title": "a", "prompt": "x", "paths": [a]},
        {"title": "b", "prompt": "y", "paths": [b]},
    ]
    _out, problems = tr.validate_plan(pieces, ["README.md"], [], 8)
    assert any("overlaps" in p["error"] for p in problems)


def test_disjoint_globs_still_pass():
    pieces = [
        {"title": "a", "prompt": "x", "paths": ["auth/tokens*"]},
        {"title": "b", "prompt": "y", "paths": ["auth/session*"]},
    ]
    assert tr.validate_plan(pieces, ["README.md"], [], 8)[1] == []


def test_a_piece_fenced_after_it_started_gets_no_exemption(env, monkeypatch):
    from types import SimpleNamespace

    rid, lead = _approved(env)
    seen = []

    async def _route(title, payload):
        seen.append(payload)
        return SimpleNamespace(status_code=200, body=b"{}")

    monkeypatch.setattr(drv._server(), "instance_red_zones_add", _route)
    _step(rid)
    _step(rid)
    assert seen and all(p["exempt"] is False for p in seen)


def test_a_piece_that_could_not_be_fenced_stops_and_asks(env, monkeypatch):
    from types import SimpleNamespace

    async def _refuse(title, payload):
        return SimpleNamespace(status_code=400, body=b'{"error":"bad pattern"}')

    monkeypatch.setattr(drv._server(), "instance_red_zones_add", _refuse)
    rid, lead = _approved(env)
    _step(rid)
    r = _step(rid)
    r = _step(rid)
    t = r["tasks"][0]
    assert t["state"] == "needs_you" and t["reason"] == "blocked"
    assert "could not fence" in t["detail"]
    assert ap.get(t["title"]) is None


# --------------------------------------------------------------------------- #
# Finding 30: a member skipped after its work merged is listed in the PR
# --------------------------------------------------------------------------- #
def test_a_member_skipped_after_merging_is_in_the_release_card(env):
    rid, lead = _approved(env)
    _step(rid)
    r = _step(rid)
    t1, t2 = [t["title"] for t in r["tasks"]]
    _lead_idle(env, lead)
    _commit(env, t1, "auth/tokens.py", "T = 2\n", "tokens: rotate them")
    _commit(env, t2, "auth/session.py", "S = 2\n", "sessions: a store")
    _done(env, t1)
    _done(env, t2)
    for _ in range(4):
        r = _step(rid)
        if all(t["state"] == "integrated" for t in r["tasks"]):
            break
    # The user moves t1 out of the group after it merged back.
    with tr.edit(rid) as rr:
        rr["tasks"][0].update(state="skipped", detail="detached")
        rr["state"] = "release_ready"
        rr["check"] = tr._normalize_check({"state": "none"})
    drv._release_prepare(rid, time.time())
    rel = tr.load(rid)["release"]
    assert "tokens: rotate them" in rel["body"] or "rotate them" in rel["title"]


# --------------------------------------------------------------------------- #
# Live L1: a worker that COMMITS but never reports is still recognised as done
# (its armed commit record mutes turn_ended — the run must not rely on it)
# --------------------------------------------------------------------------- #
def test_a_worker_that_committed_and_went_quiet_is_merged_back(env):
    rid, lead = _approved(env)
    _step(rid)
    r = _step(rid)
    t1 = r["tasks"][0]["title"]
    _lead_idle(env, lead)
    _commit(env, t1, "auth/tokens.py", "T = 3\n", "tokens: rotate")
    # No report, no turn_ended event: just a clean tree, idle for a while.
    env.rows[t1] = {
        "title": t1,
        "activity": "idle",
        "activity_since": time.time() - tr.DONE_QUIET_S - 5,
        "stage": "committed",
        "repo": "repo",
        "branch": "mf/" + t1,
    }
    r = _step(rid)
    assert r["tasks"][0]["state"] == "integrating"
    for _ in range(2):
        r = _step(rid)
    assert r["tasks"][0]["state"] == "integrated"


def test_a_worker_idle_but_not_yet_quiet_is_not_rushed(env):
    rid, lead = _approved(env)
    _step(rid)
    r = _step(rid)
    t1 = r["tasks"][0]["title"]
    _commit(env, t1, "auth/tokens.py", "T = 3\n", "tokens: rotate")
    env.rows[t1] = {
        "title": t1,
        "activity": "idle",
        "activity_since": time.time() - 5,
        "stage": "committed",
        "repo": "repo",
    }
    assert _step(rid)["tasks"][0]["state"] == "working"


def test_the_stuck_detail_says_it_committed():
    t = tr._normalize_task(
        {
            "id": "t1",
            "title": "x",
            "state": "working",
            "nudges": tr.MAX_NUDGES,
            "worked": True,
        }
    )
    o = {"activity_since": 1.0, "beyond_base": 2}
    (act,) = tr._plan_stuck(t, o, 1.0 + tr.STUCK_AFTER_S + 5, "idle", 0.0, 0.0)
    assert "committed" in act["detail"] and "no diff" not in act["detail"]


# --------------------------------------------------------------------------- #
# Live L3: a one-for-all group's finish never says "3 PRs"
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "release,lane,want",
    [
        ({"state": "done", "pr_url": "https://x/pull/9"}, "pr", "one PR opened"),
        ({"state": "handoff"}, "pr", "the PR was not opened"),
        ({"state": "none"}, "commit", "nothing pushed"),
    ],
)
def test_a_one_for_all_finish_says_what_it_shipped(release, lane, want):
    run = tr._normalize(
        {
            "id": "r_abcd",
            "policy": {"lane": lane, "grouping": "together"},
            "release": release,
            "tasks": [
                {"id": "t%d" % i, "title": "p%d" % i, "state": "integrated"}
                for i in (1, 2, 3)
            ],
        }
    )
    phrase = tr.finish_phrase(run, 3)
    assert want in phrase and "3 PRs" not in phrase


def test_an_each_group_still_counts_its_prs():
    run = tr._normalize({"id": "r_abcd", "policy": {"lane": "pr", "grouping": "each"}})
    assert tr.finish_phrase(run, 3) == "3 PRs"


# --------------------------------------------------------------------------- #
# Live L6: a clean PR title; the handoff is an end, not a red ✗
# --------------------------------------------------------------------------- #
def test_the_release_title_never_carries_a_cut_name_or_an_ellipsis():
    run = tr._normalize(
        {
            "id": "r_abcd",
            "name": "Create notes/one.md, notes/two.md and…",
            "tasks": [
                {
                    "id": "t1",
                    "title": "a",
                    "state": "integrated",
                    "commits": [
                        "Add paragraph about lighthouses covering historical "
                        "significance, construction, and automation"
                    ],
                },
                {
                    "id": "t2",
                    "title": "b",
                    "state": "integrated",
                    "commits": ["notes: add two"],
                },
            ],
        }
    )
    title = tr.release_title(run)
    assert "…" not in title and " and:" not in title and not title.endswith(" and")
    assert "add two" in title
    assert len(title) <= 110


def test_a_handoff_finishes_the_leads_record(env):
    rid, lead = _approved(env)
    _release_ready(env, rid, lead)
    drv.release(rid)
    ap.update(
        lead,
        state="halted",
        reason="could not open the pull request: needs gh or a token",
    )
    with tr.edit(rid) as r:
        r["state"] = "releasing"
    drv._release_handoff(rid, "needs gh or a token", time.time())
    assert tr.load(rid)["release"]["state"] == "handoff"
    assert ap.get(lead)["state"] == "done"
