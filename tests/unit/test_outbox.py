"""The Outbox (``backend.web.core.outbox.build``) — pure.

One place for "what's waiting on me / what is shipping / what shipped", for
every session, de-duplicated on (repo, branch) so a duplicated window is one
row named after the window that drives it.
"""

from __future__ import annotations

from backend.web.core import outbox
from backend.web.core import team_runs as tr

NOW = 200_000.0
TODAY = NOW - 3600


def _row(title, **kw):
    base = {
        "title": title,
        "repo": "quickpay",
        "branch": "feature/" + title,
        "activity": "working",
        "activity_since": NOW - 60,
        "diff_stat": {"files": 5, "additions": 210, "deletions": 32},
    }
    base.update(kw)
    return base


def _run(*tasks, **kw):
    base = {
        "id": "r_abc123",
        "name": "Q4 payments",
        "policy": {"lane": "pr"},
        "tasks": list(tasks),
    }
    base.update(kw)
    return tr._normalize(base)


def _build(rows=(), runs=(), ap=None, group="all", verify=None):
    return outbox.build(
        list(rows),
        list(runs),
        dict(ap or {}),
        now=NOW,
        today_start=TODAY,
        group=group,
        verify=verify,
    )


def test_a_dialog_waits_on_you_for_any_session():
    out = _build([_row("web-dark-mode", activity="clarify", activity_since=NOW - 9)])
    (w,) = out["groups"]["waiting"]
    assert w["kind"] == "prompt" and w["run"] is None
    assert w["key"] == "quickpay::feature/web-dark-mode"
    assert w["actions"] == ["answer", "open"]
    assert out["counts"]["waiting"] == 1


def test_an_ask_first_lane_shows_what_you_are_approving():
    rec = {
        "state": "done",
        "depth": "agent",
        "lane": "commit",
        "ask_first": True,
        "message": "web: add a dark theme toggle\n\nbody",
        "updated": NOW - 30,
    }
    out = _build([_row("web-dark-mode", activity="idle")], ap={"web-dark-mode": rec})
    (w,) = out["groups"]["waiting"]
    assert w["kind"] == "approve" and w["step"] == "commit"
    # The FULL message: an edit of a multi-line message keeps its body.
    assert w["preview"] == {
        "commit_message": "web: add a dark theme toggle\n\nbody",
        "pr_title": None,
        "files": 5,
        "add": 210,
        "del": 32,
    }
    assert w["actions"] == ["ship", "diff", "edit_message"]


def test_an_approval_never_previews_a_placeholder_as_the_message():
    # A run arms its members with the task line as a PLACEHOLDER
    # (message_auto), replaced at commit time by one written from the diff —
    # found driving a real server: the card showed "Write the delta notes" as
    # the commit message, which is not what gets committed. Only a person's
    # message is previewed; the UI then says it is written from the diff.
    rec = {
        "state": "done",
        "depth": "agent",
        "lane": "pr",
        "ask_first": True,
        "message": "Write the delta notes",
        "message_auto": True,
        "updated": NOW - 30,
    }
    out = _build([_row("web-dark-mode", activity="idle")], ap={"web-dark-mode": rec})
    (w,) = out["groups"]["waiting"]
    assert w["preview"]["commit_message"] is None
    assert w["preview"]["pr_title"] is None


def test_a_pr_lane_held_at_commit_approves_the_push():
    rec = {
        "state": "done",
        "depth": "commit",
        "lane": "pr",
        "ask_first": True,
        "message": "m",
    }
    out = _build([_row("x", activity="idle")], ap={"x": rec})
    (w,) = out["groups"]["waiting"]
    # The PR is filled from the branch's commits when it opens: no title is
    # promised unless someone set one.
    assert w["step"] == "push" and w["preview"]["pr_title"] is None
    out = _build([_row("x", activity="idle")], ap={"x": dict(rec, pr_title="T")})
    assert out["groups"]["waiting"][0]["preview"]["pr_title"] == "T"


def test_a_model_written_subject_is_not_previewed_as_this_works_message():
    rec = {
        "state": "done",
        "depth": "agent",
        "lane": "commit",
        "ask_first": True,
        "message": "auth: fix token refresh race",
        "message_written": True,
        "started": NOW - 99,
    }
    out = _build([_row("x", activity="idle")], ap={"x": rec})
    (w,) = out["groups"]["waiting"]
    assert w["preview"]["commit_message"] is None
    assert w["armed_at"] == NOW - 99


def test_shipping_and_shipped_today():
    ap = {
        "a": {
            "state": "running",
            "depth": "pr",
            "step": "pr",
            "lane": "pr",
            "note": "",
        },
        "b": {
            "state": "done",
            "depth": "pr",
            "step": "pr",
            "url": "https://github.com/q/q/pull/318",
            "message": "refunds: retry webhooks with exponential backoff",
            "updated": NOW - 60,
        },
        "c": {
            "state": "done",
            "depth": "pr",
            "step": "pr",
            "url": "u",
            "updated": TODAY - 5,
        },
    }
    rows = [
        _row("a", activity="idle"),
        _row("b", activity="idle", merge_state={"state": "OPEN", "checks": "ok"}),
        _row("c", activity="idle"),
    ]
    out = _build(rows, ap=ap, verify=lambda r: {"id": "plan-" + r["title"]})
    (s,) = out["groups"]["shipping"]
    assert s == {
        "key": "quickpay::feature/a",
        "title": "a",
        "run": None,
        "step": "make_pr",
        "note": "opening PR",
        "lane": "pr",
        "text": None,
    }
    (done,) = out["groups"]["shipped"]  # c shipped yesterday
    assert done["pr_url"] == "https://github.com/q/q/pull/318"
    assert done["pr_state"] == "open" and done["checks"] == "pass"
    assert done["commit_subject"].startswith("refunds:")
    assert done["verify"] == {"id": "plan-b"}


def test_a_duplicated_window_is_one_row_named_after_its_driver():
    rows = [
        _row("foo-copy", branch="feat/x", activity="clarify"),
        _row("foo", branch="feat/x", activity="clarify"),
    ]
    ap = {"foo": {"state": "running", "depth": "pr", "step": ""}}
    out = _build(rows, ap=ap)
    assert [w["title"] for w in out["groups"]["waiting"]] == ["foo"]


def test_run_escalations_queue_and_group_filter():
    run = _run(
        {
            "id": "t1",
            "title": "jira-PAY-419",
            "state": "needs_you",
            "reason": "ship_halted",
            "detail": "mypy failed twice",
        },
        {
            "id": "t2",
            "kind": "ticket",
            "ticket_id": "PAY-421",
            "text": "Stripe 2026-09",
            "state": "queued",
        },
        {"id": "t3", "title": "jira-PAY-412", "state": "failed", "detail": "Jira 502"},
    )
    rows = [_row("jira-PAY-419", activity="idle"), _row("own", activity="clarify")]
    out = _build(rows, runs=[run])
    kinds = {w["title"]: w["kind"] for w in out["groups"]["waiting"]}
    assert kinds == {
        "jira-PAY-419": "ship_halted",
        "jira-PAY-412": "failed",
        "own": "prompt",
    }
    halted = next(w for w in out["groups"]["waiting"] if w["kind"] == "ship_halted")
    assert halted["reason"] == "mypy failed twice"
    assert halted["run"] == {"id": "r_abc123", "name": "Q4 payments", "task": "t1"}
    assert halted["actions"] == ["retry", "open", "skip"]
    (q,) = out["groups"]["queued"]
    assert q["run"] == {"id": "r_abc123", "name": "Q4 payments", "task": "t2"}
    assert q["ref"] == "PAY-421" and q["text"] == "Stripe 2026-09"
    only = _build(rows, runs=[run], group="r_abc123")
    assert {w["title"] for w in only["groups"]["waiting"]} == {
        "jira-PAY-419",
        "jira-PAY-412",
    }
    own = _build(rows, runs=[run], group="own")
    assert [w["title"] for w in own["groups"]["waiting"]] == ["own"]
    assert own["groups"]["queued"] == []


def test_a_run_member_on_a_dialog_is_listed_once():
    run = _run({"id": "t1", "title": "m", "state": "needs_you", "reason": "prompt"})
    out = _build([_row("m", activity="clarify")], runs=[run])
    assert [w["kind"] for w in out["groups"]["waiting"]] == ["prompt"]


def test_a_budget_pause_asks_to_raise_it():
    run = _run(
        {"id": "t1", "title": "m", "state": "working"},
        paused=True,
        pause_reason="budget",
        budget_usd=20,
    )
    (w,) = _build([], runs=[run])["groups"]["waiting"]
    assert w["kind"] == "budget" and w["reason"] == "Q4 payments hit $20.00"
    assert w["actions"] == ["raise_budget", "stop"]


def test_finished_groups_leave_a_summary_card_for_a_week():
    run = _run(
        {"id": "t1", "title": "m", "state": "shipped"},
        state="done",
        finished_at=NOW - 100,
    )
    run["summary"] = {"text_md": "## Q4 payments\n"}
    out = _build([], runs=[run])
    assert out["summaries"] == [
        {
            "run": "r_abc123",
            "name": "Q4 payments",
            "state": "done",
            "finished_at": NOW - 100,
            "text_md": "## Q4 payments\n",
        }
    ]
    old = _run(
        {"id": "t1", "state": "shipped"}, state="done", finished_at=NOW - 8 * 86400
    )
    old["summary"] = {"text_md": "x"}
    assert _build([], runs=[old])["summaries"] == []


def test_counts_match_what_each_group_shows():
    run = _run({"id": "t1", "kind": "ticket", "ticket_id": "P-1", "state": "queued"})
    out = _build([_row("a", activity="clarify")], runs=[run])
    assert out["counts"] == {k: len(v) for k, v in out["groups"].items()}


def test_items_say_what_the_work_is_and_shipped_rows_count_files():
    run = _run(
        {
            "id": "t1",
            "title": "jira-PAY-412",
            "text": "Retry refund webhooks",
            "state": "shipped",
            "finished_at": NOW - 5,
            "pr_url": "u",
        },
    )
    out = _build(
        [_row("jira-PAY-412", activity="idle"), _row("own", activity="clarify")],
        runs=[run],
    )
    (shipped,) = out["groups"]["shipped"]
    assert shipped["text"] == "Retry refund webhooks" and shipped["files"] == 5
    (own,) = out["groups"]["waiting"]
    assert own["text"] is None


def test_a_commit_approval_counts_what_will_be_committed():
    """Live L7: the card said "3 files +3" for a one-file change — the total
    since the fork point also counted local base commits never pushed. A
    commit approval shows the working tree's change."""
    rec = {"state": "done", "depth": "agent", "lane": "commit", "ask_first": True}
    ds = {
        "files": 3,
        "additions": 3,
        "deletions": 0,
        "uncommitted": {"files": 1, "additions": 1, "deletions": 0},
    }
    out = _build([_row("x", activity="idle", diff_stat=ds)], ap={"x": rec})
    p = out["groups"]["waiting"][0]["preview"]
    assert (p["files"], p["add"], p["del"]) == (1, 1, 0)
