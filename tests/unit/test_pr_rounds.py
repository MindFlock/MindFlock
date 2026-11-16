"""PR review rounds (docs/pr-review-rounds.md): a new "changes requested"
review or batch of comments on one of your already-handled PRs goes to its
session with a triage prompt, once the reviewer has stopped."""

import asyncio
import json
from datetime import datetime, timezone

import pytest

from backend.config import settings as S
from backend.ticket_ingestion import orchestrator as O
from backend.ticket_ingestion import pr_rounds as R
from backend.ticket_ingestion import state as ST
from backend.ticket_ingestion.config import GithubConfig
from backend.ticket_ingestion.models import PRComment, ProcessedPR, PullRequest

T0 = 1_800_000_000.0


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


def cmt(i, t, path="app.py", author="rev"):
    return PRComment(
        id=i,
        kind="review",
        author=author,
        body="please fix %d" % i,
        url="u%d" % i,
        path=path,
        line=3 if path else None,
        created_at=iso(t),
    )


def review(i, t, author="rev"):
    return PRComment(
        id=i,
        kind="review",
        author=author,
        body="changes needed",
        url="r%d" % i,
        created_at=iso(t),
    )


def _pr():
    return PullRequest(
        number=7,
        head_ref="feature/x",
        head_sha="abc",
        base_ref="staging",
        title="Add parser",
        url="https://github.com/o/r/pull/7",
        author="me",
        created_at=datetime.fromtimestamp(T0 - 9000, timezone.utc),
        repo="o/r",
    )


def _gh(**kw):
    return GithubConfig(
        base_branch="staging",
        min_age_minutes=0,
        poll_interval_seconds=60,
        enabled=True,
        skip_authors=[],
        **kw,
    )


# --------------------------------------------------------------------------- #
# Detecting a round
# --------------------------------------------------------------------------- #
def test_only_requests_after_the_first_pass_and_not_yet_carried():
    old = cmt(1, T0 - 100)
    new = cmt(2, T0 + 100)
    carried = cmt(3, T0 + 200)
    st, items = R.pending_round([old, new, carried], [], {"c:3"}, T0, T0 + 2000)
    assert st == "ready" and [c.id for c in items] == [2]


def test_a_series_waits_for_the_reviewer_to_stop():
    st, items = R.pending_round(
        [cmt(2, T0 + 100), cmt(4, T0 + 900)], [], set(), T0, T0 + 1000
    )
    assert st == "settling" and len(items) == 2
    st, items = R.pending_round(
        [cmt(2, T0 + 100), cmt(4, T0 + 900)], [], set(), T0, T0 + 900 + R.SETTLE_S + 1
    )
    assert st == "ready" and len(items) == 2


def test_a_changes_requested_review_is_a_round_on_its_own():
    st, items = R.pending_round([], [review(55, T0 + 50)], set(), T0, T0 + 5000)
    assert st == "ready" and R.comment_key(items[0]) == "r:55"
    assert R.pending_round([], [], set(), T0, T0 + 5000) == ("none", [])


def test_the_triage_prompt_by_policy():
    pr = _pr()
    items = [review(55, T0), cmt(2, T0, path="parser.py")]
    p = R.build_round_prompt(pr, items, "triage")
    assert "New review round on PR #7: Add parser" in p
    assert "**Do it**" in p and "**Push back**" in p and "**Ask the owner**" in p
    assert "`parser.py:3`" in p and "the whole PR (requested changes)" in p
    assert "commit and push" in p and "never force-push" in p
    a = R.build_round_prompt(pr, items, "ask")
    assert "STOP" in a and "commit and push" not in a


# --------------------------------------------------------------------------- #
# Ledger + setting
# --------------------------------------------------------------------------- #
def test_the_round_ledger(tmp_path):
    ST.record_pr_round(tmp_path, "o/r", 7, ["c:2", "r:55"])
    ST.record_pr_round(tmp_path, "o/r", 7, ["c:2", "c:9"])
    assert ST.load_pr_rounds(tmp_path)["o/r#7"]["items"] == ["c:2", "r:55", "c:9"]


def test_the_repo_setting_round_trips_and_defaults_to_triage():
    gh = S.GithubSettings.from_dict(
        {
            "repo_settings": {
                "o/r": {"changes_requested": "ask"},
                "o/s": {"changes_requested": "bogus"},
            }
        }
    )
    assert gh.repo_settings["o/r"]["changes_requested"] == "ask"
    cfg = _gh(repo_settings=gh.repo_settings)
    assert cfg.changes_requested_for("o/r") == "ask"
    assert cfg.changes_requested_for("o/s") == "triage"
    assert cfg.changes_requested_for("o/t") == "triage"
    cfg.repo_settings["o/t"] = {"changes_requested": "off"}
    assert cfg.changes_requested_for("o/t") == "off"


# --------------------------------------------------------------------------- #
# The loop: deliver once per round
# --------------------------------------------------------------------------- #
class FakeMonitor:
    def __init__(self, prs):
        self.prs = prs

    async def my_open_prs(self):
        return self.prs


class FakeRunner:
    def __init__(self):
        self.rounds = []

    async def deliver_pr_round(self, pr, prompt):
        self.rounds.append((pr.number, prompt))
        return "queued"


def test_check_rounds_delivers_once(tmp_path, monkeypatch):
    pr = _pr()
    monkeypatch.setattr(O, "_STATE_DIR", tmp_path)
    ST.record_processed_pr(
        tmp_path,
        ProcessedPR(
            number=7,
            head_sha="abc",
            processed_at=datetime.fromtimestamp(T0, timezone.utc),
            repo="o/r",
        ),
    )
    newest = cmt(2, T0 + 100)

    async def requests(p, c):
        return [review(55, T0 + 50), newest]

    monkeypatch.setattr(O, "fetch_pr_requests", requests)
    monkeypatch.setattr("time.time", lambda: T0 + 100 + R.SETTLE_S + 5)
    orch = O.PipelineOrchestrator.__new__(O.PipelineOrchestrator)
    orch.config = type("C", (), {"github": _gh()})()
    orch._pr_monitor = FakeMonitor([pr])
    orch._cs_runner = FakeRunner()
    asyncio.run(orch._check_rounds())
    assert [n for n, _ in orch._cs_runner.rounds] == [7]
    assert set(ST.load_pr_rounds(tmp_path)["o/r#7"]["items"]) == {"c:2", "r:55"}
    asyncio.run(orch._check_rounds())  # nothing new: no second delivery
    assert len(orch._cs_runner.rounds) == 1


def test_off_leaves_rounds_alone(tmp_path, monkeypatch):
    pr = _pr()
    monkeypatch.setattr(O, "_STATE_DIR", tmp_path)
    ST.record_processed_pr(
        tmp_path,
        ProcessedPR(
            number=7,
            head_sha="abc",
            processed_at=datetime.fromtimestamp(T0, timezone.utc),
            repo="o/r",
        ),
    )
    orch = O.PipelineOrchestrator.__new__(O.PipelineOrchestrator)
    orch.config = type(
        "C", (), {"github": _gh(repo_settings={"o/r": {"changes_requested": "off"}})}
    )()
    orch._pr_monitor = FakeMonitor([pr])
    orch._cs_runner = FakeRunner()
    asyncio.run(orch._check_rounds())
    assert orch._cs_runner.rounds == []


# --------------------------------------------------------------------------- #
# Every request kind has its own bookkeeping prefix; old ones stay valid
# --------------------------------------------------------------------------- #
def convo(i, t, author="rev"):
    return PRComment(
        id=i,
        kind="issue",
        author=author,
        body="why?",
        url="i%d" % i,
        created_at=iso(t),
    )


def body_review(i, t, state="COMMENTED"):
    return PRComment(
        id=i,
        kind="review",
        author="bot",
        body="outside the diff range: x",
        url="b%d" % i,
        created_at=iso(t),
        review_state=state,
    )


def test_each_kind_has_its_own_prefix():
    assert R.comment_key(cmt(2, T0)) == "c:2"
    assert R.comment_key(review(55, T0)) == "r:55"  # no state: as before
    cr = review(56, T0)
    cr.review_state = "CHANGES_REQUESTED"
    assert R.comment_key(cr) == "r:56"
    assert R.comment_key(convo(9, T0)) == "i:9"
    assert R.comment_key(body_review(57, T0)) == "b:57"
    assert R.comment_key(body_review(58, T0, "APPROVED")) == "b:58"


def test_old_state_still_marks_its_items_handled():
    # A ledger written before conversation comments were read: c:/r: only.
    handled = {"c:2", "r:55"}
    st, items = R.pending_round(
        [cmt(2, T0 + 10), convo(2, T0 + 10)],
        [review(55, T0 + 10), body_review(55, T0 + 10)],
        handled,
        T0,
        T0 + 5000,
    )
    # Same numeric ids, other kinds: still new.
    assert st == "ready"
    assert sorted(R.comment_key(c) for c in items) == ["b:55", "i:2"]


# --------------------------------------------------------------------------- #
# A reply to every request
# --------------------------------------------------------------------------- #
def _assert_replies_to_all(p):
    assert "Reply to EVERY request" in p
    assert "Done in `<sha>`" in p  # done: what changed + the commit sha
    assert "say why" in p  # pushed back
    assert "Waiting on the owner" in p  # asked the owner
    assert "link to its reply" in p  # the summary lists each reply's link
    assert "never force-push" in p and "never merge" in p
    assert "Never resolve a reviewer's thread" in p


def test_the_round_prompt_replies_to_every_request():
    thread = cmt(3, T0, path="parser.py")
    thread.reply_to = 1
    items = [review(55, T0), convo(9, T0), body_review(57, T0), thread]
    for policy in ("triage", "ask"):
        p = R.build_round_prompt(_pr(), items, policy)
        _assert_replies_to_all(p)
        # Inline: its own thread, to the thread's first comment.
        assert (
            "gh api -X POST repos/o/r/pulls/7/comments/<thread comment id>/replies" in p
        )
        assert "thread comment id `1`" in p
        # Conversation comments + review bodies: ONE conversation comment.
        assert "gh api -X POST repos/o/r/issues/7/comments" in p
        assert p.count("reply in your PR conversation comment") == 3
        assert "the whole PR (conversation comment)" in p
        assert "the whole PR (review)" in p
    assert "every request still gets its reply" in R.build_round_prompt(
        _pr(), items, "ask"
    )


def test_only_the_reply_kinds_in_play_are_spelled_out():
    p = R.build_round_prompt(_pr(), [cmt(3, T0)], "triage")
    assert "/replies" in p and "issues/7/comments -F" not in p
    p = R.build_round_prompt(_pr(), [convo(9, T0)], "triage")
    assert "issues/7/comments -F" in p and "<thread comment id>/replies -f" not in p


def test_the_first_pass_prompt_replies_to_every_request():
    from backend.ticket_ingestion import pr_runner as P

    thread = cmt(3, T0, path="parser.py")
    items = [convo(9, T0), body_review(57, T0), thread]
    p = P.build_consolidated_pr_prompt(_pr(), items, "/ws", reply_all=True)
    _assert_replies_to_all(p)
    assert "**Do it**" in p and "**Push back**" in p and "**Ask the owner**" in p
    assert "commit them, and push" in p
    assert "unstaged" not in p  # the old "leave it for the human" ending is gone
    assert "### 1. conversation comment by rev" in p
    assert "### 2. review by bot" in p
    assert "### 3. inline comment by rev" in p
    assert "thread comment id `3`" in p  # no reply_to known: its own id
    # Off (a route's Reviews stop, a forced review): the old prompt, unchanged.
    plain = P.build_consolidated_pr_prompt(_pr(), items, "/ws")
    assert "Reply to EVERY request" not in plain and "unstaged" in plain


# --------------------------------------------------------------------------- #
# A route's reviews stop owns the comments: recorded, never handed over
# --------------------------------------------------------------------------- #
class FlightRunner(FakeRunner):
    async def deliver_pr_round(self, pr, prompt):
        self.rounds.append((pr.number, prompt))
        return "flight"


def test_a_round_left_to_a_route_is_recorded_and_never_comes_back(
    tmp_path, monkeypatch
):
    pr = _pr()
    monkeypatch.setattr(O, "_STATE_DIR", tmp_path)
    ST.record_processed_pr(
        tmp_path,
        ProcessedPR(
            number=7,
            head_sha="abc",
            processed_at=datetime.fromtimestamp(T0, timezone.utc),
            repo="o/r",
        ),
    )

    async def requests(p, c):
        return [convo(9, T0 + 50), cmt(2, T0 + 100)]

    monkeypatch.setattr(O, "fetch_pr_requests", requests)
    monkeypatch.setattr("time.time", lambda: T0 + 100 + R.SETTLE_S + 5)
    orch = O.PipelineOrchestrator.__new__(O.PipelineOrchestrator)
    orch.config = type("C", (), {"github": _gh()})()
    orch._pr_monitor = FakeMonitor([pr])
    orch._cs_runner = FlightRunner()
    asyncio.run(orch._check_rounds())
    assert set(ST.load_pr_rounds(tmp_path)["o/r#7"]["items"]) == {"i:9", "c:2"}
    # The flight passes its reviews stop: the server now hands rounds over,
    # but these two were the route's — nothing to deliver.
    orch._cs_runner = FakeRunner()
    asyncio.run(orch._check_rounds())
    assert orch._cs_runner.rounds == []


def test_the_first_pass_records_what_it_carried(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(O, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(
        O,
        "fetch_pr_requests",
        AsyncMock(
            return_value=[
                convo(9, T0),
                cmt(2, T0),
            ]
        ),
    )
    orch = O.PipelineOrchestrator.__new__(O.PipelineOrchestrator)
    orch.config = type("C", (), {"github": _gh()})()
    orch._cs_runner = type("R", (), {"run_pr": AsyncMock(return_value="feature/x")})()

    async def _slot(*a, **k):
        return None

    orch._wait_for_slot = _slot
    asyncio.run(orch._process_pr(_pr()))
    assert ("o/r", 7) in ST.load_processed_prs(tmp_path)
    assert set(ST.load_pr_rounds(tmp_path)["o/r#7"]["items"]) == {"i:9", "c:2"}
