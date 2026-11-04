"""PR reviews and issues share the ticket cap, and automatic launches are spaced.

2026-11-04: one PR scan found several PRs with review comments and launched a
session for each back to back. The ticket queue's ``max_sessions`` cap never
applied to the PR (or GitHub issue) loop, and the memory gate read the machine
before any of the new sessions had grown — so WSL ran out of memory and
restarted. These tests pin both fixes:

  * **counting** — a live PR review / issue session occupies a slot, found
    through its own ledger (an unrelated ``pr-…`` session does not);
  * **holding** — a PR or issue with work to do waits for a slot before its
    launch; a PR with nothing to address never waits;
  * **spacing** — an automatic launch starts at least the spacing after the
    previous one came up, reads memory only after that, and ``0`` turns it off.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.ticket_ingestion import orchestrator as orch
from backend.ticket_ingestion import session_runner as sr
from backend.ticket_ingestion.config import (
    GithubConfig,
    PipelineConfig,
    TicketProviderConfig,
)
from backend.ticket_ingestion.models import (
    Issue,
    ProcessedIssue,
    ProcessedPR,
    PullRequest,
)
from backend.ticket_ingestion.state import record_processed_issue, record_processed_pr

_NOW = datetime(2026, 11, 4, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Counting.
# --------------------------------------------------------------------------- #
def test_live_pr_and_issue_sessions_take_a_slot(tmp_path, monkeypatch):
    record_processed_pr(
        tmp_path,
        ProcessedPR(
            number=3106, head_sha="a", processed_at=_NOW, repo="org/sitecheck-bot"
        ),
    )
    record_processed_pr(  # reviewed again on a new head: still one session
        tmp_path,
        ProcessedPR(
            number=3106, head_sha="b", processed_at=_NOW, repo="org/sitecheck-bot"
        ),
    )
    record_processed_pr(  # its session has closed
        tmp_path, ProcessedPR(number=7, head_sha="a", processed_at=_NOW, repo="org/app")
    )
    record_processed_issue(
        tmp_path, ProcessedIssue(number=12, processed_at=_NOW, repo="org/my.repo")
    )
    live = {
        "mindflock_pr-sitecheck-bot-3106",
        "mindflock_issue-my_repo-12",  # tmux turns "." into "_"
        "mindflock_pr-something-I-named-myself",
    }
    monkeypatch.setattr(orch, "_live_tmux_sessions", lambda: live)
    assert orch.live_ticket_sessions(tmp_path) == 2


# --------------------------------------------------------------------------- #
# Holding PRs and issues for a slot.
# --------------------------------------------------------------------------- #
@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    monkeypatch.setattr(orch, "_STATE_DIR", tmp_path)
    cfg = PipelineConfig(
        ticketing=TicketProviderConfig(provider="github_issues"),
        repo_url="git@github.com:org/repo.git",
        workspace_dir=tmp_path / "workspaces",
    )
    cfg.github = GithubConfig(
        repos=["org/repo"],
        base_branch="main",
        min_age_minutes=15,
        poll_interval_seconds=60,
        enabled=True,
        skip_authors=[],
        token="tok",
    )
    p = orch.PipelineOrchestrator(cfg)
    order: list[str] = []

    async def wait_for_slot(what="the next ticket"):
        order.append("slot:" + what)

    async def run_pr(pr, comments):
        order.append("launch:pr")
        return pr.head_ref

    async def run(story, **kw):
        order.append("launch:issue")
        return "branch"

    p._cs_runner = MagicMock()
    p._cs_runner.run_pr = run_pr
    p._cs_runner.run = run
    p._issue_monitor = MagicMock()
    p._issue_monitor.fetch_comments = AsyncMock(return_value=[])
    monkeypatch.setattr(p, "_wait_for_slot", wait_for_slot)
    p.order = order
    return p


def _pr() -> PullRequest:
    return PullRequest(
        number=9,
        head_ref="feat",
        head_sha="abc",
        base_ref="main",
        title="t",
        url="u",
        author="me",
        created_at=_NOW,
        repo="org/repo",
    )


def test_a_pr_with_comments_waits_for_a_slot_before_its_launch(pipeline):
    with patch.object(
        orch, "fetch_actionable_comments", new=AsyncMock(return_value=[MagicMock()])
    ):
        asyncio.run(pipeline._process_pr(_pr()))
    assert pipeline.order == ["slot:PR #9", "launch:pr"]


def test_a_pr_with_nothing_to_address_never_waits(pipeline):
    with patch.object(
        orch, "fetch_actionable_comments", new=AsyncMock(return_value=[])
    ):
        asyncio.run(pipeline._process_pr(_pr()))
    assert pipeline.order == []


def test_an_issue_waits_for_a_slot_before_its_launch(pipeline):
    issue = Issue(
        number=4,
        title="t",
        body="do the thing with enough description length here",
        url="u",
        author="me",
        created_at=_NOW,
        repo="org/repo",
    )
    asyncio.run(pipeline._process_issue(issue))
    assert pipeline.order == ["slot:issue #4", "launch:issue"]


# --------------------------------------------------------------------------- #
# Spacing launches.
# --------------------------------------------------------------------------- #
@pytest.fixture
def clock(monkeypatch):
    """A fake monotonic clock that ``asyncio.sleep`` advances, recording each
    sleep and each memory read in ``events``."""
    state = {"now": 1000.0, "events": []}

    class _Time:
        @staticmethod
        def monotonic():
            return state["now"]

    real_sleep = asyncio.sleep

    async def fake_sleep(s, *a, **k):
        state["now"] += s
        state["events"].append(("sleep", s))
        await real_sleep(0)

    async def fake_memory(what):
        state["events"].append(("memory", state["now"]))

    monkeypatch.setattr(sr, "time", _Time)
    monkeypatch.setattr(sr.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(sr, "_wait_for_memory", fake_memory)
    monkeypatch.setattr(sr, "_last_launch", 0.0)
    monkeypatch.setenv("MINDFLOCK_LAUNCH_SPACING", "60")
    return state


def test_the_first_launch_goes_at_once(clock):
    asyncio.run(sr._wait_for_launch_room("PR #1"))
    assert clock["events"] == [("memory", 1000.0)]


def test_a_burst_is_spaced_and_memory_is_read_after_the_wait(clock):
    async def burst():
        for n in range(3):
            await sr._wait_for_launch_room("PR #%d" % n)

    asyncio.run(burst())
    reads = [t for kind, t in clock["events"] if kind == "memory"]
    assert reads == [1000.0, 1060.0, 1120.0]


def test_spacing_counts_from_when_the_previous_session_came_up(clock):
    async def two():
        await sr._wait_for_launch_room("PR #1")
        clock["now"] += 45  # provisioning + session start
        sr._note_launch()  # run_pr's finally
        await sr._wait_for_launch_room("PR #2")

    asyncio.run(two())
    reads = [t for kind, t in clock["events"] if kind == "memory"]
    assert reads == [1000.0, 1105.0]


def test_concurrent_loops_do_not_launch_together(clock):
    async def both():
        await asyncio.gather(
            sr._wait_for_launch_room("ticket sc-1"), sr._wait_for_launch_room("PR #2")
        )

    asyncio.run(both())
    reads = [t for kind, t in clock["events"] if kind == "memory"]
    assert reads == [1000.0, 1060.0]


@pytest.mark.parametrize("value", ["0", "0.0"])
def test_zero_turns_spacing_off(clock, monkeypatch, value):
    monkeypatch.setenv("MINDFLOCK_LAUNCH_SPACING", value)

    async def burst():
        for n in range(3):
            await sr._wait_for_launch_room("PR #%d" % n)

    asyncio.run(burst())
    assert [k for k, _ in clock["events"]] == ["memory"] * 3


def test_a_bad_value_keeps_the_default(monkeypatch):
    monkeypatch.setenv("MINDFLOCK_LAUNCH_SPACING", "soon")
    assert sr._launch_spacing() == sr._LAUNCH_SPACING_SECONDS
    monkeypatch.setenv("MINDFLOCK_LAUNCH_SPACING", "-5")
    assert sr._launch_spacing() == 0.0
