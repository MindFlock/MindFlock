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

    async def comments(p, c):
        return [newest]

    async def reviews(p, c):
        return [review(55, T0 + 50)]

    monkeypatch.setattr(O, "fetch_actionable_comments", comments)
    import backend.ticket_ingestion.pr_comments as PC

    monkeypatch.setattr(PC, "fetch_changes_requested", reviews)
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
