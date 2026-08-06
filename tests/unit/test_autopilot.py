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
    rec = _rec(idle_since=1.0)
    action, detail = ap.next_action(rec, _snap(dirty=False, beyond_base=0))
    assert action == "stop"
    assert "without changing anything" in detail["reason"]


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


def test_agent_depth_completes_at_verified_idle():
    rec = _rec(depth="agent", idle_since=10_000.0)
    action, _ = ap.next_action(rec, _snap(now=10_000.0 + ap.IDLE_SETTLE_S + 1))
    assert action == "done"


def test_precommit_stage_always_waits():
    """A commit is in flight and the shell owns the session."""
    for depth in ap.DEPTHS:
        assert ap.next_action(_rec(depth=depth), _snap(stage="precommit"))[0] == "wait"


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


def test_running_checks_wait():
    action, _ = ap.next_action(
        _rec(), _snap(stage="committed", check={"state": "running"})
    )
    assert action == "wait"


# --- The retry / skip policy ------------------------------------------------
def test_unlisted_hook_failure_stops_with_the_hook_named():
    rec = _rec(retryable=["gitnexus-index"])
    action, detail = ap.next_action(
        rec,
        _snap(stage="interrupt", failed_hook="run-tests", failed_step="Run Tests"),
    )
    assert action == "stop"
    assert "Run Tests" in detail["reason"]


def test_test_hooks_are_never_skippable_even_if_configured():
    """The deny set is enforced at the decision, not only in the UI, so a
    hand-edited settings file cannot route around it."""
    rec = _rec(retryable=["run-tests", "detect-secrets"])
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
    rec = _rec(retryable=["gitnexus-index"])
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


def test_finish_marks_done():
    ap.arm("s1", "pr")
    ap.finish("s1")
    assert ap.get("s1")["state"] == "done"


def test_prune_drops_records_for_dead_sessions():
    """Mandatory, not housekeeping: titles are REUSED after a delete, so a
    recreated session must not inherit the old target or attempt counters."""
    ap.arm("alive", "pr")
    ap.arm("dead", "merge")
    assert ap.prune(["alive"]) == 1
    assert ap.get("dead") is None
    assert ap.get("alive") is not None


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
