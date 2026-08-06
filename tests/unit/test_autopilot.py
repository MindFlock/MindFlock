"""Autopilot: the depth ladder, the pure decision function, and the store.

``next_action`` is pure by design, so the whole ladder-and-retry policy is
table-tested here with no git, no tmux and no network.
"""

import os
import tempfile

import pytest

from backend.web.core import autopilot as ap


@pytest.fixture(autouse=True)
def _isolated_store(monkeypatch):
    """Point the store at a tmp file — without this the tests would write the
    user's real ~/.mindflock/autopilot.json."""
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", os.path.join(d, "ap.json"))
        yield


def _rec(**over):
    r = ap._blank()
    r.update({"depth": "pr", "state": "running"})
    r.update(over)
    return r


def _snap(**over):
    s = {
        "stage": "agent",
        "failed_step": "",
        "failed_hook": "",
        "dirty": True,
        "beyond_base": 0,
        "activity": "idle",
        "limited": False,
        "queue_pending": False,
        "check": None,
        "check_required": False,
        "pr_checks": "ok",
        "merge_blockers": [],
        "on_base_branch": False,
        "branch": "feature/x",
        "has_origin": True,
        "now": 10_000.0,
    }
    s.update(over)
    return s


# --- The ladder --------------------------------------------------------------
def test_depth_order_is_the_single_source_of_rank():
    assert ap.DEPTH_ORDER == ("off", "agent", "commit", "push", "pr", "merge")
    assert ap.DEPTHS == ("agent", "commit", "push", "pr", "merge")


def test_source_depths_exclude_merge():
    # A per-source default applies to every future item with no human in the
    # loop, and merge is the one rung that cannot be undone.
    assert "merge" not in ap.SOURCE_DEPTHS
    assert "pr" in ap.SOURCE_DEPTHS


@pytest.mark.parametrize(
    "stage,depth,expected",
    [
        ("committed", "commit", True),
        ("committed", "push", False),
        ("pushed", "commit", True),
        ("pushed", "push", True),
        ("pushed", "pr", False),
        ("pr", "pr", True),
        ("pr", "push", True),
        # No "merged" stage exists — the merge rung completes when the merge
        # call returns ok, never by observation.
        ("pr", "merge", False),
        ("agent", "commit", False),
        ("interrupt", "commit", False),
        ("precommit", "commit", False),
    ],
)
def test_reaches(stage, depth, expected):
    assert ap.reaches(stage, depth) is expected


def test_normalize_depth_rejects_junk():
    assert ap.normalize_depth("PR") == "pr"
    assert ap.normalize_depth("  merge ") == "merge"
    assert ap.normalize_depth("off") == "off"
    assert ap.normalize_depth("") == ""
    assert ap.normalize_depth("nonsense") == ""
    assert ap.normalize_depth(None) == ""


# --- The happy ladder --------------------------------------------------------
def test_idle_dwell_is_required_before_the_first_commit():
    rec = _rec()
    # First idle sighting only marks the clock.
    action, detail = ap.next_action(rec, _snap())
    assert action == "wait"
    assert detail.get("mark_idle") is True

    rec = _rec(idle_since=10_000.0)
    action, _ = ap.next_action(rec, _snap(now=10_000.0 + ap.IDLE_SETTLE_S - 1))
    assert action == "wait", "must not commit before the dwell elapses"

    action, _ = ap.next_action(rec, _snap(now=10_000.0 + ap.IDLE_SETTLE_S + 1))
    assert action == "commit"


def test_a_working_agent_is_never_committed_over():
    rec = _rec(idle_since=1.0)
    for activity in ("working", "clarify", "offline"):
        action, _ = ap.next_action(rec, _snap(activity=activity))
        assert action == "wait", activity


def test_usage_limit_holds_rather_than_halts():
    rec = _rec(idle_since=1.0)
    action, detail = ap.next_action(rec, _snap(activity="limit", limited=True))
    assert action == "wait"
    assert "usage limit" in detail["reason"]


def test_prompt_queue_gates_the_commit():
    """Composing with the queue instead of racing it: queue the follow-up turns
    you want and autopilot waits for them to drain."""
    rec = _rec(idle_since=1.0)
    action, detail = ap.next_action(rec, _snap(queue_pending=True))
    assert action == "wait"
    assert "prompt queue" in detail["reason"]


def test_clean_tree_after_the_agent_halts_loudly():
    """Only once this run actually SAW the agent work — see the next test."""
    rec = _rec(idle_since=1.0, worked_at=9_000.0)
    action, detail = ap.next_action(rec, _snap(dirty=False, beyond_base=0))
    assert action == "stop"
    assert "without changing anything" in detail["reason"]


def test_a_run_that_already_acted_never_accuses_the_agent():
    """A session on its BASE branch lands in the agent branch permanently —
    `_session_stage` collapses to "agent" whenever origin/<base>..HEAD is 0, which
    it always is once the base branch has been pushed. A run that had committed AND
    pushed therefore reported "the agent finished without changing anything", with
    commits=1 and step="push" recorded right beside it."""
    rec = _rec(commits=1, step="push", idle_since=1.0, worked_at=0.0)
    action, detail = ap.next_action(
        rec, _snap(dirty=False, beyond_base=0, on_base_branch=True, branch="main")
    )
    assert action == "stop"
    assert "without changing anything" not in detail["reason"]
    assert "main" in detail["reason"] and "make a branch" in detail["reason"]


def test_a_run_that_landed_its_work_off_the_base_branch_is_done():
    rec = _rec(commits=1, step="push", idle_since=1.0)
    action, _ = ap.next_action(rec, _snap(dirty=False, beyond_base=0))
    assert action == "done"


def test_a_clean_tree_waits_when_the_agent_never_started():
    """THE "stopping early" REGRESSION. Arming is normally the FIRST thing you do —
    before the agent has written a line, or between turns. The old code halted
    ~35s after every such press with "the agent finished without changing
    anything", about an agent that had not begun."""
    rec = _rec(idle_since=1.0, worked_at=0.0, commits=0)
    action, detail = ap.next_action(rec, _snap(dirty=False, beyond_base=0))
    assert action == "wait", "must never halt before the agent has done anything"
    assert "waiting for the agent to start" in detail["reason"]


def test_an_unmeasurable_commit_count_waits_rather_than_accusing():
    """beyond_base None means "could not measure" (no base branch resolved). That
    is not evidence that nothing happened, and must never be read as failure."""
    rec = _rec(idle_since=1.0, worked_at=9_000.0)
    action, _ = ap.next_action(rec, _snap(dirty=False, beyond_base=None))
    assert action == "wait"


def test_already_committed_work_is_done_not_an_accusation():
    rec = _rec(depth="commit", idle_since=1.0, worked_at=9_000.0)
    action, _ = ap.next_action(rec, _snap(dirty=False, beyond_base=3))
    assert action == "done"


def test_ladder_advances_committed_to_push_to_pr():
    assert ap.next_action(_rec(), _snap(stage="committed"))[0] == "push"
    assert ap.next_action(_rec(), _snap(stage="pushed"))[0] == "make_pr"


def test_target_reached_is_done_not_another_step():
    assert ap.next_action(_rec(depth="commit"), _snap(stage="committed"))[0] == "done"
    assert ap.next_action(_rec(depth="push"), _snap(stage="pushed"))[0] == "done"
    assert ap.next_action(_rec(depth="pr"), _snap(stage="pr"))[0] == "done"


def test_pr_stage_only_merges_at_merge_depth():
    assert ap.next_action(_rec(depth="pr"), _snap(stage="pr"))[0] == "done"
    assert ap.next_action(_rec(depth="merge"), _snap(stage="pr"))[0] == "merge"


# --- The merge rung waits for CI (the owner's stated requirement) -----------
def test_merge_waits_for_pending_ci():
    action, detail = ap.next_action(
        _rec(depth="merge"), _snap(stage="pr", pr_checks="pending")
    )
    assert action == "wait"
    assert "CI" in detail["reason"]


def test_merge_stops_on_failed_ci():
    action, detail = ap.next_action(
        _rec(depth="merge"), _snap(stage="pr", pr_checks="failed")
    )
    assert action == "stop"
    assert "CI failed" in detail["reason"]


def test_unknown_ci_is_never_a_green_light():
    """No token / API fault must not be mistaken for "nothing to wait for" — that
    is how an autopilot merges unverified work."""
    action, _ = ap.next_action(
        _rec(depth="merge"), _snap(stage="pr", pr_checks="unknown")
    )
    assert action == "wait"


def test_a_named_merge_blocker_stops_the_run_and_says_which():
    """The driver reads the same probe the merge button does, so the two can never
    disagree about whether a merge would go through."""
    action, detail = ap.next_action(
        _rec(depth="merge"),
        _snap(
            stage="pr",
            merge_blockers=["the branch has merge conflicts with its base"],
        ),
    )
    assert action == "stop"
    assert "merge conflicts" in detail["reason"]


def test_a_blocker_that_is_only_a_running_check_waits():
    action, detail = ap.next_action(
        _rec(depth="merge"),
        _snap(
            stage="pr",
            pr_checks="pending",
            merge_blockers=["required checks are still running"],
        ),
    )
    assert action == "wait"
    assert "required checks" in detail["reason"]


def test_a_repo_that_reports_no_checks_may_merge():
    action, _ = ap.next_action(_rec(depth="merge"), _snap(stage="pr", pr_checks="none"))
    assert action == "merge"


def test_agent_depth_completes_at_verified_idle():
    rec = _rec(depth="agent", idle_since=10_000.0)
    action, _ = ap.next_action(rec, _snap(now=10_000.0 + ap.IDLE_SETTLE_S + 1))
    assert action == "done"


def test_precommit_stage_always_waits():
    """A commit is in flight and the shell owns the session."""
    for depth in ap.DEPTHS:
        assert ap.next_action(_rec(depth=depth), _snap(stage="precommit"))[0] == "wait"


def test_a_session_on_its_base_branch_never_pushes():
    """A run on `main` pushed 20 files straight to origin/main and the remote
    reported "Bypassed rule violations … Changes must be made through a pull
    request". There is no feature branch to PR from, so the chain must stop after
    committing and say so."""
    action, detail = ap.next_action(
        _rec(), _snap(stage="committed", on_base_branch=True, branch="main")
    )
    assert action == "stop"
    assert "main" in detail["reason"]
    assert "make a branch" in detail["reason"]


def test_committing_is_still_allowed_on_the_base_branch():
    """Only pushing/PRing needs a branch — the commit itself is fine."""
    assert (
        ap.next_action(
            _rec(depth="commit"), _snap(stage="committed", on_base_branch=True)
        )[0]
        == "done"
    )


def test_an_unknown_base_is_not_guessed_into_a_refusal():
    action, _ = ap.next_action(
        _rec(), _snap(stage="committed", on_base_branch=False, branch="")
    )
    assert action == "push"


def test_a_trunk_branch_is_refused_even_if_the_base_did_not_resolve():
    """Defence in depth. The base-branch guard relies on _session_base_branch; if
    that resolves to "" the comparison silently passes and the trunk gets pushed —
    and a bypass-silent remote ACCEPTS it, merely noting a PR was required. That
    happened twice."""
    for name in ("main", "master", "Main", "develop", "trunk"):
        action, detail = ap.next_action(
            _rec(), _snap(stage="committed", on_base_branch=False, branch=name)
        )
        assert action == "stop", name
        assert name in detail["reason"]


def test_a_feature_branch_still_pushes():
    for name in ("feature/x", "fix/thing", "ethan/main-menu"):
        assert (
            ap.next_action(
                _rec(), _snap(stage="committed", on_base_branch=False, branch=name)
            )[0]
            == "push"
        ), name


def test_missing_origin_stops_before_pushing():
    action, detail = ap.next_action(_rec(), _snap(stage="committed", has_origin=False))
    assert action == "stop"
    assert "origin" in detail["reason"]


# --- Checks gate (the owner's "if tests fail, stop") ------------------------
def test_failed_checks_stop_the_run():
    action, detail = ap.next_action(
        _rec(), _snap(stage="committed", check={"state": "failed"})
    )
    assert action == "stop"
    assert "checks failed" in detail["reason"]


def test_a_repo_with_no_check_command_pushes_immediately():
    """The common case, and it must not wait on a check that will never run."""
    action, _ = ap.next_action(
        _rec(), _snap(stage="committed", check=None, check_required=False)
    )
    assert action == "push"


def test_a_declared_check_is_STARTED_not_discovered_as_a_409():
    """The push route soft-gates on the verification check: in a repo that declares
    check_command it answers 409 "checks haven't passed for this commit" when the
    result is missing or stale, and the driver turned any 4xx into a permanent
    halt. So the ladder asks for the check instead of pushing into a refusal."""
    action, _ = ap.next_action(
        _rec(), _snap(stage="committed", check=None, check_required=True)
    )
    assert action == "run_check"


def test_a_stale_check_result_is_re_run_not_trusted():
    action, _ = ap.next_action(
        _rec(),
        _snap(
            stage="committed",
            check={"state": "ok", "stale": True},
            check_required=True,
        ),
    )
    assert action == "run_check"


def test_a_fresh_green_check_pushes():
    action, _ = ap.next_action(
        _rec(),
        _snap(
            stage="committed",
            check={"state": "ok", "stale": False},
            check_required=True,
        ),
    )
    assert action == "push"


def test_agent_depth_never_commits_even_on_an_interrupt():
    """A target of "agent only" must not run git commit. The interrupt branch used
    to be dispatched first, so an allowlisted hook made it commit anyway."""
    rec = _rec(depth="agent", retryable=["gitnexus-index"], commits=0)
    action, _ = ap.next_action(
        rec, _snap(stage="interrupt", failed_hook="gitnexus-index")
    )
    assert action != "commit"


def test_running_checks_wait():
    action, _ = ap.next_action(
        _rec(), _snap(stage="committed", check={"state": "running"})
    )
    assert action == "wait"


# --- The retry / skip policy ------------------------------------------------
def test_unlisted_hook_failure_stops_with_the_hook_named():
    rec = _rec(retryable=["gitnexus-index"], commits=1)
    action, detail = ap.next_action(
        rec,
        _snap(stage="interrupt", failed_hook="run-tests", failed_step="Run Tests"),
    )
    assert action == "stop"
    assert "Run Tests" in detail["reason"]


def test_the_first_commit_attempt_is_always_spent():
    """A run that has not committed yet is looking at an EARLIER attempt's
    interrupt — very often a stale .mindflock_commit_status from a manual commit.
    Halting there aborted the press within seconds, blaming a failure the user
    pressed the button to get past. Spend our own attempt first (exactly what the
    manual Re-commit button does)."""
    rec = _rec(retryable=[], commits=0)
    action, _ = ap.next_action(
        rec, _snap(stage="interrupt", failed_hook="run-tests", failed_step="Run Tests")
    )
    assert action == "commit"


def test_test_hooks_are_never_skippable_even_if_configured():
    """The deny set is enforced at the decision, not only in the UI, so a
    hand-edited settings file cannot route around it."""
    rec = _rec(retryable=["run-tests", "detect-secrets"], commits=1)
    for hook in ("run-tests", "detect-secrets"):
        action, detail = ap.next_action(rec, _snap(stage="interrupt", failed_hook=hook))
        assert action == "stop", hook
        assert hook in ap.NEVER_SKIP


def test_allowlisted_hook_retries_plain_then_skips():
    rec = _rec(retryable=["gitnexus-index"], commits=1)
    # First failure: a plain retry, no skip list yet (catches a transient failure).
    action, detail = ap.next_action(
        rec, _snap(stage="interrupt", failed_hook="gitnexus-index")
    )
    assert action == "commit"
    assert detail["skip"] == []
    assert detail["hook"] == "gitnexus-index"

    # Second failure of the SAME hook: bypass it so the commit can land.
    rec = _rec(retryable=["gitnexus-index"], commits=2, attempts={"gitnexus-index": 1})
    action, detail = ap.next_action(
        rec, _snap(stage="interrupt", failed_hook="gitnexus-index")
    )
    assert action == "commit"
    assert detail["skip"] == ["gitnexus-index"]
    assert detail["skipping"] == "gitnexus-index"


def test_allowlisted_hook_gives_up_after_the_skip_attempt():
    rec = _rec(retryable=["gitnexus-index"], commits=3, attempts={"gitnexus-index": 2})
    action, detail = ap.next_action(
        rec, _snap(stage="interrupt", failed_hook="gitnexus-index")
    )
    assert action == "stop"
    assert "kept failing" in detail["reason"]


def test_commit_attempts_are_bounded_overall():
    rec = _rec(retryable=["gitnexus-index"], commits=ap.MAX_COMMIT_ATTEMPTS)
    action, detail = ap.next_action(
        rec, _snap(stage="interrupt", failed_hook="gitnexus-index")
    )
    assert action == "stop"
    assert "too many commit attempts" in detail["reason"]


def test_unparseable_hook_id_stops_rather_than_guessing():
    """pre-commit's display `name:` is free text and does not map back to an id
    ("Black format" is not `black`), so a missing id can never be guessed."""
    rec = _rec(retryable=["gitnexus-index"], commits=1)
    action, detail = ap.next_action(
        rec, _snap(stage="interrupt", failed_hook="", failed_step="Black format")
    )
    assert action == "stop"
    assert "Black format" in detail["reason"]


# --- Store round-trips -------------------------------------------------------
def test_arm_get_and_disarm():
    assert ap.get("s1") is None
    rec = ap.arm("s1", "pr", message="fix it", retryable=["gitnexus-index"])
    assert rec["depth"] == "pr"
    got = ap.get("s1")
    assert got["depth"] == "pr" and got["message"] == "fix it"
    assert got["retryable"] == ["gitnexus-index"]
    assert ap.disarm("s1") is True
    assert ap.get("s1") is None
    assert ap.disarm("s1") is False


def test_arm_with_off_disarms():
    ap.arm("s1", "pr")
    assert ap.arm("s1", "off") is None
    assert ap.get("s1") is None


def test_arm_refuses_to_store_a_never_skip_hook():
    rec = ap.arm("s1", "pr", retryable=["gitnexus-index", "run-tests"])
    assert rec["retryable"] == ["gitnexus-index"]


def test_halt_records_a_reason_and_stops_the_run():
    ap.arm("s1", "pr")
    ap.halt("s1", "checks failed")
    got = ap.get("s1")
    assert got["state"] == "halted" and got["reason"] == "checks failed"
    # A halted run is inert.
    assert ap.next_action(got, _snap())[0] == "stop"


def test_finish_marks_done_and_clears_the_wait_note():
    """`note` holds what the run was WAITING on, which is history the moment it
    stops. Left behind, a finished record still read "pre-commit hooks are
    running" right next to its own completion."""
    ap.arm("s1", "pr")
    ap.update("s1", note="pre-commit hooks are running")
    ap.finish("s1")
    got = ap.get("s1")
    assert got["state"] == "done"
    assert got["note"] == ""


def test_halt_clears_the_wait_note_too():
    ap.arm("s1", "pr")
    ap.update("s1", note="waiting for checks to finish")
    ap.halt("s1", "checks failed")
    got = ap.get("s1")
    assert got["reason"] == "checks failed", "the halt reason is what a human reads"
    assert got["note"] == "", "the stale wait reason must not sit beside it"


def test_prune_drops_records_for_dead_sessions():
    """Mandatory, not housekeeping: titles are REUSED after a delete, so a
    recreated session must not inherit the old target or attempt counters."""
    ap.arm("alive", "pr", now=1_000.0)
    ap.arm("dead", "merge", now=1_000.0)
    # Past the arm grace, an unknown title is dropped.
    assert ap.prune(["alive"], now=1_000.0 + ap.ARM_GRACE_S + 1) == 1
    assert ap.get("dead") is None
    assert ap.get("alive") is not None


def test_prune_never_drops_a_freshly_armed_record():
    """THE BUG THAT DISABLED ALL OF INTAKE. Every intake entry point arms BEFORE
    its session exists (a forced start arms then clones; the pipeline arms from a
    separate process). A prune that demanded the title be live right now deleted
    every such record within one 5s pass, so picking a depth on a ticket silently
    did nothing at all."""
    ap.arm("sc-123-not-created-yet", "pr", source="tix", now=1_000.0)
    assert ap.prune([], now=1_000.0 + 5.0) == 0
    assert ap.get("sc-123-not-created-yet") is not None


def test_normalize_tolerates_a_mangled_record():
    """`_normalize` IS the migration mechanism — there is no version key, so an
    old or hand-edited record must never crash a pass."""
    e = ap._normalize({"depth": "bogus", "attempts": "nope", "commits": "x"})
    assert e["depth"] == "" and e["attempts"] == {} and e["commits"] == 0
    assert ap._normalize(None)["state"] == "running"
    assert ap._normalize(["not", "a", "dict"])["depth"] == ""


def test_store_survives_a_corrupt_file(monkeypatch):
    path = ap.autopilot_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write("{ this is not json")
    assert ap.all_titles() == []
    assert ap.get("anything") is None
    # And a write repairs it.
    ap.arm("s1", "pr")
    assert ap.get("s1")["depth"] == "pr"


# --- One driver per chain ----------------------------------------------------
def test_two_servers_cannot_both_drive_one_chain():
    """THE "stuck at agent just went idle" BUG. Two servers sharing this store (a
    dev instance on another port) both ran the driver: each saw the other's boot id,
    treated it as a restart, and reset the idle dwell EVERY pass, so the 30s settle
    could never elapse. Both would also have acted — double-committing and
    double-pushing the same worktree."""
    ap.arm("s1", "pr", now=1_000.0)
    a, took_a = ap.claim("s1", "server-A", now=1_000.0)
    assert a is not None and took_a is True

    # B is locked out while A's lease is fresh.
    b, took_b = ap.claim("s1", "server-B", now=1_010.0)
    assert b is None and took_b is False

    # A keeps it, and holding it does NOT keep resetting the dwell.
    ap.update("s1", idle_since=1_005.0)
    again, took_again = ap.claim("s1", "server-A", now=1_020.0)
    assert again is not None and took_again is False
    assert again["idle_since"] == 1_005.0, "an owner must not reset its own dwell"


def test_a_stale_lease_is_taken_over_and_the_dwell_re_earned():
    ap.arm("s1", "pr", now=1_000.0)
    ap.claim("s1", "server-A", now=1_000.0)
    ap.update("s1", idle_since=1_005.0)
    # A went away; past the staleness window B may take over.
    b, took = ap.claim("s1", "server-B", now=1_000.0 + ap.LEASE_STALE_S + 1)
    assert b is not None and took is True
    assert b["idle_since"] is None, "a new driver must not inherit a dwell it never saw"


def test_claiming_an_unknown_or_nameless_chain_is_refused():
    assert ap.claim("nope", "server-A") == (None, False)
    ap.arm("s1", "pr")
    assert ap.claim("s1", "") == (None, False)
    assert ap.claim("", "server-A") == (None, False)
