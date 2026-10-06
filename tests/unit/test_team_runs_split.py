"""Splits and one-for-all groups, the PURE half (core.team_runs).

Plan validation (overlap over ``git ls-files``, a literal new path another
piece's glob covers, red-zone-only pieces, the cap, bad pieces), the piece
titles, the briefs' budgets, the release PR's title and body, and every
transition the planner makes for a group whose members merge into ONE
branch: members committing → the merge queue (one at a time, oldest first,
only into a free lead), a conflict holding the queue and escalating after
two hand-offs, a dirty lead escalating after a while, all merged → the
check → the release (yours, or ``release: auto``) → done; the lead removed
or never started; starts gated on the lead. No I/O: a run dict and an
observation dict in, actions out.
"""

from __future__ import annotations

import pytest

from backend.config import red_zones as rz
from backend.web.core import team_runs as tr

NOW = 10_000.0
FILES = [
    "quickpay/auth/tokens.py",
    "quickpay/auth/tokens_test.py",
    "quickpay/auth/session.py",
    "quickpay/auth/session_scopes.py",
    "quickpay/auth/scopes.py",
    "secrets/keys.py",
]


def _piece(title, paths, prompt="do it"):
    return {"title": title, "prompt": prompt, "paths": paths}


def _validate(pieces, red=(), cap=8):
    return tr.validate_plan(pieces, FILES, [rz.compile_pattern(p) for p in red], cap)


# --------------------------------------------------------------------------- #
# Plan validation
# --------------------------------------------------------------------------- #
class TestValidatePlan:
    def test_disjoint_pieces_pass(self):
        pieces, problems = _validate(
            [
                _piece("tokens", ["quickpay/auth/tokens*"]),
                _piece("sessions", ["quickpay/auth/session.py"]),
                _piece("scopes", ["quickpay/auth/scopes.py"]),
            ]
        )
        assert problems == []
        assert [p["title"] for p in pieces] == ["tokens", "sessions", "scopes"]

    def test_two_pieces_sharing_a_file_overlap(self):
        _pieces, problems = _validate(
            [
                _piece("sessions", ["quickpay/auth/session*"]),
                _piece("scopes", ["quickpay/auth/*scopes*"]),
            ]
        )
        assert problems == [
            {
                "piece": "scopes",
                "error": "overlaps sessions on quickpay/auth/session_scopes.py",
            }
        ]

    def test_a_new_file_one_piece_names_and_another_covers_overlaps(self):
        _pieces, problems = _validate(
            [
                _piece("api", ["quickpay/api/**"]),
                _piece("routes", ["quickpay/api/routes.py"]),  # not in ls-files yet
            ]
        )
        assert (
            problems
            and "overlaps api on quickpay/api/routes.py" in problems[0]["error"]
        )

    def test_a_glob_matching_nothing_yet_is_allowed(self):
        _pieces, problems = _validate(
            [
                _piece("new", ["quickpay/billing/**"]),
                _piece("tokens", ["quickpay/auth/tokens.py"]),
            ]
        )
        assert problems == []

    def test_a_piece_wholly_in_a_red_zone_is_refused(self):
        _pieces, problems = _validate(
            [
                _piece("keys", ["secrets/**"]),
                _piece("tokens", ["quickpay/auth/tokens.py"]),
            ],
            red=["secrets/"],
        )
        assert problems == [
            {"piece": "keys", "error": "every file it covers is in a red zone"}
        ]

    def test_a_piece_partly_in_a_red_zone_is_fine(self):
        _pieces, problems = _validate(
            [
                _piece("auth", ["quickpay/auth/tokens.py", "secrets/keys.py"]),
                _piece("scopes", ["quickpay/auth/scopes.py"]),
            ],
            red=["secrets/"],
        )
        assert problems == []

    def test_the_cap_and_the_minimum(self):
        many = [_piece("p%d" % i, ["x%d/**" % i]) for i in range(4)]
        assert any("at most 3 pieces" in p["error"] for p in _validate(many, cap=3)[1])
        assert any("at least 2" in p["error"] for p in _validate(many[:1])[1])
        assert any("at least 2" in p["error"] for p in _validate("nope")[1])

    @pytest.mark.parametrize(
        "bad, error",
        [
            (_piece("", ["a/**"]), "needs a title"),
            (_piece("t", ["a/**"], prompt=""), "needs a prompt"),
            (_piece("t", []), "needs the paths"),
            (_piece("t", ["../escape"]), "bad path glob"),
        ],
    )
    def test_incomplete_pieces(self, bad, error):
        _pieces, problems = _validate([bad, _piece("ok", ["quickpay/auth/scopes.py"])])
        assert any(error in p["error"] for p in problems), problems

    def test_repeated_titles(self):
        _p, problems = _validate([_piece("auth", ["a/**"]), _piece("Auth", ["b/**"])])
        assert any("share this title" in p["error"] for p in problems)

    def test_paths_as_a_lone_string_are_read_as_one(self):
        pieces, problems = _validate(
            [
                {"title": "a", "prompt": "x", "paths": "quickpay/auth/scopes.py"},
                _piece("b", ["quickpay/auth/tokens.py"]),
            ]
        )
        assert problems == [] and pieces[0]["paths"] == ["quickpay/auth/scopes.py"]


class TestNamesAndBriefs:
    def test_piece_titles_are_unique_against_taken(self):
        pieces = [{"title": "Tokens"}, {"title": "One session store!"}, {"title": ""}]
        assert tr.piece_titles("auth", pieces, {"auth-tokens"}) == [
            "auth-tokens-2",
            "auth-one-session-store",
            "auth-piece3",
        ]

    def test_briefs_fit_their_budgets_and_name_the_tools_per_cli(self):
        run = tr._normalize(
            {"id": "r_abcdef", "name": "x" * 80, "split": True, "lead": {"title": "l"}}
        )
        claude = tr.lead_brief(run, 8, "claude")
        assert len(claude) <= 600
        assert "mcp__mindflock__propose_run_plan(run_id=r_abcdef" in claude
        codex = tr.lead_brief(run, 8, "codex")
        assert "propose_run_plan(run_id=" in codex and "mcp__" not in codex
        auto = tr.lead_brief(dict(run, optional=True), 8, "claude")
        assert len(auto) <= 600 and "pieces=[]" in auto and "yourself" in auto
        assert len(tr.run_brief(run, "claude")) <= 600
        assert "report_result" in tr.run_brief(run, "claude")
        assert len(tr.piece_brief(run, ["a/**"] * 8, "claude")) <= 800
        assert "`a/**`" in tr.piece_brief(run, ["a/**"], "claude")
        assert "report_integrated" in tr.integrator_brief(run, "claude")

    def test_the_auto_split_brief_names_its_cap_and_keeps_the_trunk_note(self):
        run = tr._normalize(
            {"id": "r_abcdef", "name": "auth", "split": True, "optional": True}
        )
        # The literal {title, prompt, paths} survives .format (escaped braces).
        auto = tr.lead_brief(run, 3, "claude")
        assert "2-3 pieces [{title, prompt, paths}]" in auto
        assert "mcp__mindflock__propose_run_plan(run_id=r_abcdef, pieces=[]" in auto
        assert auto.startswith("---\nMindFlock auto-split:")
        assert "MindFlock auto-split" not in tr.lead_brief(
            dict(run, optional=False), 3, "claude"
        )
        # A lead on its trunk is told to commit nothing there, auto-split or
        # not — the note is purely additive to the 600-char brief.
        run["lead"] = tr._normalize_lead(
            {"title": "l", "trunk": True, "branch": "main"}
        )
        trunk = tr.lead_brief(run, 3, "claude")
        assert trunk == auto + tr.LEAD_TRUNK_CLAUSE.format(branch="main")
        assert len(auto) <= 600

    def test_tests_line_and_count(self):
        assert tr.tests_line("did x\n\nDetails:\nTests: pytest -q — 24 passed") == (
            "pytest -q — 24 passed"
        )
        assert tr.tests_line("- **Tests:** 3 green") == "3 green"
        assert tr.tests_line("no tests here") == ""
        assert tr.tests_passed("1 passed\n...\n== 212 passed in 3.1s ==") == 212
        assert tr.tests_passed("FAILED") is None


# --------------------------------------------------------------------------- #
# The release: one PR, a section per piece
# --------------------------------------------------------------------------- #
def _split_run(**kw):
    base = {
        "id": "r_split1",
        "name": "Auth cleanup",
        "split": True,
        "state": "running",
        "policy": {"lane": "pr", "grouping": "together", "release": "ask"},
        "lead": {"title": "auth-cleanup-lead", "branch": "mf/auth-cleanup-lead"},
        "plan": {
            "state": "approved",
            "pieces": [],
            "why": "Three independent seams in auth.",
        },
        "tasks": [],
    }
    base.update(kw)
    return tr._normalize(base)


def _member(tid, title, state="working", **kw):
    t = {
        "id": tid,
        "kind": "piece",
        "title": title,
        "text": "Rotate refresh tokens on privilege change",
        "state": state,
        "branch": "mf/" + title,
        "started_at": 100.0,
        "incarnation": 100.0,
        "lane": "commit",
        "paths": ["quickpay/auth/tokens*"],
    }
    t.update(kw)
    return t


class TestRelease:
    def _run(self):
        return _split_run(
            tasks=[
                _member(
                    "t1",
                    "auth-cleanup-tokens",
                    "integrated",
                    commits=["auth: rotate refresh tokens on scope change"],
                    report_text="Rotated refresh tokens.\n\nDetails:\nTests: pytest auth — 24 passed",
                    tests="pytest auth — 24 passed",
                ),
                _member(
                    "t2",
                    "auth-cleanup-sessions",
                    "integrated",
                    paths=["quickpay/auth/session*"],
                    commits=["auth: put session storage behind SessionStore"],
                    conflict={"files": ["quickpay/auth/__init__.py"], "attempts": 1},
                    conflict_fixed=True,
                ),
                _member("t3", "auth-cleanup-scopes", "failed"),
            ],
            check={
                "state": "ok",
                "command": "pytest",
                "tests": 212,
                "sha": "abc1234ff",
            },
        )

    def test_title_is_the_name_then_what_each_merged_piece_did(self):
        assert tr.release_title(self._run()) == (
            "Auth cleanup: rotate refresh tokens on scope change, put session "
            "storage behind SessionStore"
        )

    def test_title_is_capped(self):
        run = self._run()
        run["name"] = "N" * 130
        title = tr.release_title(run)
        # Capped at whole words, never with a literal "…" (live L6).
        assert len(title) <= 120 and "…" not in title

    def test_body_has_a_section_per_merged_piece(self):
        body = tr.release_body(self._run(), "main", "mf/auth-cleanup-lead")
        assert body.count("\n## ") == 4  # 2 pieces + conflict fixes + check
        assert "## tokens" in body and "## sessions" in body
        assert "## scopes" not in body  # failed: not in the PR
        assert "Rotated refresh tokens." in body
        assert (
            "Tests: pytest"
            not in body.split("## sessions")[0]
            .split("## tokens")[1]
            .split("**Tests:**")[0]
        )
        assert "**Tests:** pytest auth — 24 passed" in body
        assert "**Tests:** not reported" in body
        assert "- auth: rotate refresh tokens on scope change" in body
        assert "**Paths:** `quickpay/auth/session*`" in body
        assert (
            "- sessions: resolved by auth-cleanup-lead (quickpay/auth/__init__.py)"
            in body
        )
        assert "`pytest` passed (212 tests) on the merged branch (abc1234)." in body
        assert body.rstrip().endswith("Part of “Auth cleanup” · MindFlock")
        assert "Three independent seams in auth." in body

    def test_body_without_a_check_command_says_so(self):
        run = self._run()
        run["check"] = tr._normalize_check({"state": "none"})
        assert "No `check_command` is configured" in tr.release_body(run, "main", "b")

    def test_after_check_state_is_the_lane_policy(self):
        for lane, want in (
            ("leave", "done"),
            ("commit", "done"),
            ("push", "release_ready"),
            ("pr", "release_ready"),
            ("merge", "release_ready"),
        ):
            run = _split_run(policy={"lane": lane, "grouping": "together"})
            assert tr.after_check_state(run) == want, lane


# --------------------------------------------------------------------------- #
# The planner
# --------------------------------------------------------------------------- #
def _lead_obs(**kw):
    lo = {
        "present": True,
        "ready": True,
        "activity": "idle",
        "activity_since": NOW - 500,
        "clean": True,
        "merging": False,
        "shipping": False,
        "head": "leadhead",
        "branch": "mf/auth-cleanup-lead",
    }
    lo.update(kw)
    return lo


def _obs(lead=None, **tasks):
    return {"lead": lead if lead is not None else _lead_obs(), "tasks": tasks}


def _ops(acts, op=None):
    return [a for a in acts if op is None or a["op"] == op]


def _states(acts):
    return {a["task"]: (a["state"], a["reason"]) for a in acts if a["op"] == "state"}


class TestMembersReachTheMergeQueue:
    def test_a_member_that_committed_itself_and_said_done_is_queued(self):
        run = _split_run(tasks=[_member("t1", "a", report_seen=0.0)])
        o = {
            "present": True,
            "created_at": 100.0,
            "activity": "idle",
            "stage": "committed",
            "report": {"status": "done", "summary": "did it", "ts": 200.0},
            "autopilot": {"state": "running", "step": "", "depth": "commit"},
        }
        acts = tr.plan_actions(run, _obs(t1=o), NOW)
        assert _states(acts)["t1"] == ("integrating", "")
        assert {"op": "disarm", "task": "t1"} in acts

    def test_a_turn_end_on_a_committed_tree_is_done_too(self):
        run = _split_run(tasks=[_member("t1", "a")])
        o = {
            "present": True,
            "created_at": 100.0,
            "activity": "idle",
            "stage": "committed",
            "turn_ended_at": 150.0,
            "autopilot": {"state": "running", "depth": "commit"},
        }
        assert _states(tr.plan_actions(run, _obs(t1=o), NOW))["t1"][0] == "integrating"

    def test_uncommitted_work_is_left_to_the_autopilot(self):
        run = _split_run(tasks=[_member("t1", "a")])
        o = {
            "present": True,
            "created_at": 100.0,
            "activity": "idle",
            "stage": "agent",
            "turn_ended_at": 150.0,
            "autopilot": {"state": "running", "depth": "commit"},
        }
        assert "t1" not in _states(tr.plan_actions(run, _obs(t1=o), NOW))

    def test_the_autopilot_finishing_its_commit_queues_it(self):
        run = _split_run(tasks=[_member("t1", "a", state="shipping")])
        o = {
            "present": True,
            "created_at": 100.0,
            "activity": "idle",
            "stage": "committed",
            "autopilot": {"state": "done", "depth": "commit", "step": "commit"},
        }
        acts = tr.plan_actions(run, _obs(t1=o), NOW)
        assert _states(acts)["t1"] == ("integrating", "")

    def test_members_are_never_shipped_on_their_own(self):
        run = _split_run(tasks=[_member("t1", "a", state="shipping")])
        o = {
            "present": True,
            "created_at": 100.0,
            "activity": "idle",
            "autopilot": {"state": "done", "depth": "commit", "step": "commit"},
        }
        assert all(
            a.get("state") != "shipped" for a in tr.plan_actions(run, _obs(t1=o), NOW)
        )

    def test_every_member_lane_is_commit_whatever_the_group_ships(self):
        run = _split_run()
        assert tr.task_lane(run, _member("t1", "a", lane="pr")) == "commit"

    def test_a_present_unfenced_piece_gets_fenced(self):
        run = _split_run(tasks=[_member("t1", "a")])
        o = {"present": True, "created_at": 100.0, "activity": "working"}
        assert {"op": "fence", "task": "t1"} in tr.plan_actions(run, _obs(t1=o), NOW)
        run["tasks"][0]["fenced"] = True
        assert not _ops(tr.plan_actions(run, _obs(t1=o), NOW), "fence")


class TestMergeQueue:
    def _two(self, **kw):
        return _split_run(
            tasks=[
                _member("t1", "a", "integrating", ready_at=300.0, **kw),
                _member("t2", "b", "integrating", ready_at=200.0, **kw),
            ]
        )

    def test_one_merge_per_pass_oldest_ready_first(self):
        acts = tr.plan_actions(self._two(), _obs(t1={}, t2={}), NOW)
        assert _ops(acts, "merge") == [{"op": "merge", "task": "t2"}]

    @pytest.mark.parametrize(
        "lead",
        [
            {"activity": "working"},
            {"clean": False},
            {"clean": None},
            {"merging": True},
            {"shipping": True},
            {"ready": False},
        ],
    )
    def test_nothing_merges_into_a_lead_that_is_not_free(self, lead):
        acts = tr.plan_actions(self._two(), _obs(_lead_obs(**lead), t1={}, t2={}), NOW)
        assert not _ops(acts, "merge")

    def test_ancestry_marks_it_merged_back_with_its_facts(self):
        run = self._two()
        acts = tr.plan_actions(
            run,
            _obs(
                t1={},
                t2={
                    "merged": True,
                    "head": "piecehead",
                    "commits": ["auth: x"],
                    "report_text": "Did b.\nTests: pytest — 3 passed",
                },
            ),
            NOW,
        )
        st = [a for a in acts if a["op"] == "state" and a["task"] == "t2"][0]
        assert st["state"] == "integrated"
        f = st["fields"]
        assert f["merged_sha"] == "leadhead" and f["head_sha"] == "piecehead"
        assert f["commits"] == ["auth: x"] and f["tests"] == "pytest — 3 passed"
        assert f["conflict_fixed"] is False
        # and the other one is merged in the same pass
        assert _ops(acts, "merge") == [{"op": "merge", "task": "t1"}]

    def test_a_conflict_holds_the_queue(self):
        run = self._two()
        run["tasks"][0].update(
            reason="conflict",
            conflict={"files": ["x.py"], "attempts": 1, "at": NOW - 10},
        )
        acts = tr.plan_actions(
            run, _obs(_lead_obs(activity="working"), t1={}, t2={}), NOW
        )
        assert not _ops(acts, "merge")

    def test_the_lead_going_idle_without_merging_is_a_failed_hand_off(self):
        run = self._two()
        run["tasks"][0].update(
            reason="conflict",
            conflict={"files": ["x.py"], "attempts": 1, "at": NOW - 600},
        )
        lead = _lead_obs(activity_since=NOW - 300)  # worked after, idle since
        acts = tr.plan_actions(run, _obs(lead, t1={}, t2={}), NOW)
        assert _ops(acts, "merge") == [{"op": "merge", "task": "t1"}]  # hand-off 2
        run["tasks"][0]["conflict"]["attempts"] = 2
        acts = tr.plan_actions(run, _obs(lead, t1={}, t2={}), NOW)
        assert _states(acts)["t1"] == ("needs_you", "conflict")
        assert not _ops(acts, "merge")

    def test_an_idle_lead_that_never_took_its_turn_is_not_a_failure(self):
        run = self._two()
        run["tasks"][0].update(
            reason="conflict",
            conflict={"files": ["x.py"], "attempts": 2, "at": NOW - 600},
        )
        lead = _lead_obs(activity_since=NOW - 900)  # idle since BEFORE the hand-off
        assert not _states(tr.plan_actions(run, _obs(lead, t1={}, t2={}), NOW))

    def test_the_lead_resolving_it_is_seen_by_ancestry(self):
        run = self._two()
        run["tasks"][0].update(
            reason="conflict",
            conflict={"files": ["x.py"], "attempts": 1, "at": NOW - 10},
        )
        acts = tr.plan_actions(run, _obs(t1={"merged": True, "head": "h"}, t2={}), NOW)
        st = [a for a in acts if a["op"] == "state" and a["task"] == "t1"][0]
        assert st["state"] == "integrated" and st["fields"]["conflict_fixed"] is True

    def test_a_lead_left_dirty_blocks_then_asks_you(self):
        run = self._two()
        lead = _lead_obs(clean=False)
        acts = tr.plan_actions(run, _obs(lead, t1={}, t2={}), NOW)
        seen = [a for a in acts if a["op"] == "seen"]
        assert seen and seen[0]["fields"] == {"blocked_since": NOW}
        run["tasks"][1]["blocked_since"] = NOW - tr.MERGE_BLOCKED_S - 1
        acts = tr.plan_actions(run, _obs(lead, t1={}, t2={}), NOW)
        assert _states(acts)["t2"] == ("needs_you", "conflict")


class TestCheckAndRelease:
    def _done(self, lane="pr", release="ask", **kw):
        return _split_run(
            policy={"lane": lane, "grouping": "together", "release": release},
            tasks=[
                _member("t1", "a", "integrated"),
                _member("t2", "b", "failed"),
            ],
            **kw,
        )

    def test_all_terminal_with_one_merged_starts_the_check(self):
        acts = tr.plan_actions(self._done(), _obs(), NOW)
        runs = _ops(acts, "run")
        assert runs[0]["state"] == "checking"
        assert _ops(acts, "check_start")
        assert not _ops(acts, "finish")

    def test_nothing_merged_finishes_with_failures(self):
        run = _split_run(tasks=[_member("t1", "a", "failed")])
        assert {"op": "finish", "state": "done_with_failures"} in tr.plan_actions(
            run, _obs(), NOW
        )

    def test_a_split_never_checks_before_its_plan_is_approved(self):
        run = _split_run(state="running", plan={"state": "proposed", "pieces": []})
        run["tasks"] = []
        assert not _ops(tr.plan_actions(run, _obs(), NOW), "check_start")

    def test_an_ok_check_on_this_head_goes_to_release_ready(self):
        run = self._done(
            state="checking", check={"state": "running", "sha": "leadhead"}
        )
        lead = _lead_obs(check={"state": "ok", "sha": "leadhead"}, check_tests=24)
        acts = tr.plan_actions(run, _obs(lead), NOW)
        r = _ops(acts, "run")[0]
        assert r["state"] == "release_ready" and r["check"]["tests"] == 24
        assert _ops(acts, "release_prepare")

    def test_a_check_for_an_older_head_is_not_the_answer(self):
        """The lead committed after the check started: the pass belongs to a
        commit that is no longer HEAD — run it again, never credit it."""
        run = self._done(state="checking", check={"state": "running", "sha": "old"})
        lead = _lead_obs(check={"state": "ok", "sha": "old"})
        acts = tr.plan_actions(run, _obs(lead), NOW)
        runs = _ops(acts, "run")
        assert runs and runs[0]["check"]["state"] == "pending"
        assert "state" not in runs[0]  # still checking: never release_ready
        assert _ops(acts, "check_start") and not _ops(acts, "release_prepare")

    def test_a_check_stamped_with_a_newer_head_than_it_started_on_reruns(self):
        """Review finding 8: the status is stamped with HEAD when the check
        FINISHES — a commit landing mid-check made the old code credit the
        untested new HEAD with the pass."""
        run = self._done(state="checking", check={"state": "running", "sha": "old"})
        lead = _lead_obs(check={"state": "ok", "sha": "leadhead"})
        acts = tr.plan_actions(run, _obs(lead), NOW)
        assert _ops(acts, "check_start")
        assert all(a.get("state") != "release_ready" for a in _ops(acts, "run"))

    def test_a_commit_lane_is_done_after_the_check(self):
        run = self._done(lane="commit", state="checking", check={"state": "running"})
        lead = _lead_obs(check={"state": "ok", "sha": "leadhead"})
        acts = tr.plan_actions(run, _obs(lead), NOW)
        assert _ops(acts, "run")[0]["state"] == "done"
        assert {"op": "finish", "state": "done_with_failures"} in acts

    def test_a_failed_check_goes_to_the_lead_twice_then_to_you(self):
        run = self._done(state="checking", check={"state": "running", "attempts": 0})
        lead = _lead_obs(
            check={"state": "failed", "sha": "leadhead"}, check_tail="E boom"
        )
        assert _ops(tr.plan_actions(run, _obs(lead), NOW), "check_fix") == [
            {"op": "check_fix", "tail": "E boom"}
        ]
        run["check"]["attempts"] = tr.MAX_CHECK_FIXES
        acts = tr.plan_actions(run, _obs(lead), NOW)
        assert _ops(acts, "run")[0]["check"]["state"] == "failed"
        assert not _ops(acts, "check_fix")

    def test_after_a_fix_the_check_runs_again_once_the_lead_is_done(self):
        run = self._done(
            state="checking", check={"state": "fixing", "fix_sent_at": NOW - 600}
        )
        busy = _lead_obs(activity="working", activity_since=NOW - 30)
        assert not _ops(tr.plan_actions(run, _obs(busy), NOW), "check_start")
        idle = _lead_obs(activity_since=NOW - 120)
        assert _ops(tr.plan_actions(run, _obs(idle), NOW), "check_start")

    def test_release_ready_prepares_then_waits_for_you(self):
        run = self._done(state="release_ready")
        assert _ops(tr.plan_actions(run, _obs(), NOW), "release_prepare")
        run["release"]["state"] = "ready"
        assert tr.plan_actions(run, _obs(), NOW) == []

    def test_release_auto_releases_on_its_own(self):
        run = self._done(state="release_ready", release="auto")
        run["release"]["state"] = "ready"
        assert tr.plan_actions(run, _obs(), NOW) == [{"op": "release", "merge": False}]

    def test_releasing_follows_the_leads_autopilot(self):
        run = self._done(state="releasing")
        done = _lead_obs(autopilot={"state": "done", "url": "https://x/pull/9"})
        acts = tr.plan_actions(run, _obs(done), NOW)
        assert _ops(acts, "run")[0]["release"]["pr_url"] == "https://x/pull/9"
        assert _ops(acts, "finish")
        handoff = _lead_obs(
            autopilot={
                "state": "halted",
                "reason": "MindFlock could not open the pull request for you — …",
            }
        )
        assert _ops(tr.plan_actions(run, _obs(handoff), NOW), "release_handoff")
        halted = _lead_obs(autopilot={"state": "halted", "reason": "push rejected"})
        r = _ops(tr.plan_actions(run, _obs(halted), NOW), "run")[0]
        assert r["state"] == "release_ready" and r["release"]["state"] == "failed"
        gone = _lead_obs(autopilot=None)
        assert _ops(tr.plan_actions(run, _obs(gone), NOW), "run")[0]["state"] == (
            "release_ready"
        )


class TestTheLead:
    def test_starts_wait_for_the_lead(self):
        run = _split_run(
            split=False,
            policy={"lane": "pr", "grouping": "together"},
            tasks=[{"id": "t1", "kind": "task", "text": "x", "title": "x"}],
        )
        assert not _ops(
            tr.plan_actions(run, _obs(_lead_obs(ready=False)), NOW), "start"
        )
        assert _ops(tr.plan_actions(run, _obs(), NOW), "start")

    def test_the_lead_removed_by_you_cancels_the_group(self):
        run = _split_run(tasks=[_member("t1", "a")])
        run["lead"]["started_at"] = 50.0
        lead = {"present": False, "deleted_at": 60.0}
        acts = tr.plan_actions(run, _obs(lead, t1={"present": True}), NOW)
        assert acts == [
            {
                "op": "abort",
                "state": "cancelled",
                "detail": "its lead auth-cleanup-lead was removed — the other "
                "sessions were kept",
            }
        ]
        tr.apply(run, acts[0], NOW)
        assert run["state"] == "cancelled"
        assert run["tasks"][0]["state"] == "cancelled"
        # Your own act: nothing to announce, and "announced" lets the loop
        # stop stepping the run (review finding 32).
        assert run["summary"]["announced"] is True

    def test_a_lead_that_never_started_fails_the_group(self):
        run = _split_run(tasks=[])
        run["lead"]["started_at"] = 50.0
        lead = {"present": False, "create_failed": "boom", "create_failed_at": 55.0}
        acts = tr.plan_actions(run, _obs(lead), NOW)
        assert acts[0]["op"] == "abort" and acts[0]["state"] == "done_with_failures"

    def test_the_lead_row_carries_role_lead(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MINDFLOCK_RUNS_DIR", str(tmp_path))
        tr.create(
            {
                "name": "G",
                "policy": {"lane": "pr", "grouping": "together", "release": "ask"},
                "lead": {"title": "g-lead"},
                "tasks": [{"kind": "task", "title": "g-a", "state": "working"}],
            }
        )
        idx = tr.title_index()
        assert idx["g-lead"]["role"] == "lead" and idx["g-lead"]["task"] == ""
        assert idx["g-lead"]["lane"] == "pr" and idx["g-lead"]["ask_first"] is True
        assert idx["g-a"]["lane"] == "commit"


class TestApplyAndViews:
    def test_run_op_merges_blocks_and_logs_state(self):
        run = _split_run()
        tr.apply(
            run, {"op": "run", "state": "checking", "check": {"state": "running"}}, NOW
        )
        assert run["state"] == "checking" and run["check"]["state"] == "running"
        assert run["events"][-1]["text"] == "all merged — running the check"

    def test_integrating_keeps_its_detail(self):
        run = _split_run(tasks=[_member("t1", "a", "integrating")])
        tr.apply(
            run,
            tr._to(run["tasks"][0], "integrating", "conflict", "lead resolving"),
            NOW,
        )
        assert run["tasks"][0]["detail"] == "lead resolving"

    def test_dto_carries_split_fields(self):
        run = _split_run(tasks=[_member("t1", "a", "integrated", commits=["x"])])
        dto = tr.run_dto(run, ["auth-cleanup-lead"])
        assert dto["split"] is True and dto["lead"]["row_present"] is True
        assert "missing_since" not in dto["lead"]
        assert dto["check"]["state"] == "pending" and dto["release"]["state"] == "none"
        assert dto["tasks"][0]["commits"] == ["x"]
        assert dto["optional"] is False and dto["max_pieces"] == 0
        dto = tr.run_dto(_split_run(optional=True, max_pieces=3))
        assert dto["optional"] is True and dto["max_pieces"] == 3

    @pytest.mark.parametrize(
        "raw,optional,max_pieces",
        [
            ({}, False, 0),  # a state.json from before auto-split
            ({"optional": True, "max_pieces": 3}, True, 3),
            ({"optional": 1, "max_pieces": "4"}, True, 4),
            ({"max_pieces": -2}, False, 0),
            ({"max_pieces": "lots"}, False, 0),
            ({"max_pieces": None}, False, 0),
        ],
    )
    def test_auto_split_fields_normalize(self, raw, optional, max_pieces):
        run = tr._normalize(dict({"id": "r_old", "name": "x", "split": True}, **raw))
        assert run["optional"] is optional and run["max_pieces"] == max_pieces

    def test_together_summary_names_the_one_pr(self):
        run = _split_run(tasks=[_member("t1", "a", "integrated")])
        run["release"]["pr_url"] = "https://x/pull/9"
        s = tr.summarize(run, NOW)
        assert "1 of 1 merged back" in s["text_md"]
        assert "One PR: https://x/pull/9" in s["text_md"]
        assert s["prs"] == [
            {
                "task": "",
                "title": "Auth cleanup",
                "pr_url": "https://x/pull/9",
                "flag": "",
            }
        ]


def test_release_title_drops_repeats_and_a_trailing_colon():
    run = _split_run(
        name="Write the notes:",
        tasks=[
            _member("t1", "a", "integrated", commits=["notes: add the summary"]),
            _member("t2", "b", "integrated", commits=["docs: Add the summary"]),
        ],
    )
    assert tr.release_title(run) == "Write the notes: add the summary"


def test_name_suggestion_never_ends_on_punctuation():
    assert tr.name_suggestion(
        [{"kind": "task", "text": "Write the release notes: an intro"}]
    ) == ("Write the release notes…")


def test_a_handoff_summary_never_promises_a_link_it_does_not_have():
    run = _split_run(tasks=[_member("t1", "a", "integrated")])
    run["release"].update(state="handoff", compare_url="")
    assert "no PR yet (no gh or GitHub token here)" in tr.summarize(run, NOW)["text_md"]
    run["release"]["compare_url"] = "https://github.com/o/r/compare/main...b?expand=1"
    assert (
        "open the PR: https://github.com/o/r/compare/"
        in tr.summarize(run, NOW)["text_md"]
    )
