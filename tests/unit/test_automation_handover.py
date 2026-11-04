"""PR review and issue handling moving between the user's devices.

One device of a group runs them (``settings_hooks.automation_here``). When
that changes — the person picks another device, the runner is removed — the
new runner's processed-PR / processed-issue ledgers don't know what the old
one already handled, so it would review every open PR (and start a session
for every open issue) a second time. The server records where they ran
(``state.note_automation``); the pipeline that becomes the runner seeds its
ledgers with what's open before its first scan.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from backend.ticket_ingestion import orchestrator as orchestrator_mod
from backend.ticket_ingestion import state
from backend.ticket_ingestion.config import (
    GithubConfig,
    PipelineConfig,
    TicketProviderConfig,
)
from backend.ticket_ingestion.models import Issue, ProcessedPR, PullRequest
from backend.ticket_ingestion.orchestrator import PipelineOrchestrator


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    d = tmp_path / "ledger"
    d.mkdir()
    monkeypatch.setattr(orchestrator_mod, "_STATE_DIR", d)
    from backend.ticket_ingestion import issue_monitor, pr_monitor

    monkeypatch.setattr(pr_monitor, "_STATE_DIR", d)
    monkeypatch.setattr(issue_monitor, "_STATE_DIR", d)
    return d


def _pr(n, repo="org/repo"):
    return PullRequest(
        number=n,
        head_ref="feat-%d" % n,
        head_sha="sha%d" % n,
        base_ref="main",
        title="t",
        url="u",
        author="me",
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        repo=repo,
    )


def _issue(n, repo="org/issues"):
    return Issue(
        number=n,
        title="t",
        body="b",
        url="u",
        author="someone",
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        repo=repo,
    )


@pytest.fixture
def orch(tmp_path, ledger):
    config = PipelineConfig(
        ticketing=TicketProviderConfig(
            provider="shortcut", api_token="sc_test_token", member_id="m"
        ),
        repo_url="git@github.com:org/repo.git",
        workspace_dir=tmp_path / "workspaces",
        min_description_length=20,
        log_file=tmp_path / "pipeline.log",
        log_level="INFO",
        github=GithubConfig(
            repos=["org/repo"],
            base_branch="",
            min_age_minutes=0,
            poll_interval_seconds=60,
            enabled=True,
            skip_authors=[],
            token="tok",
            issues_enabled=True,
            issue_repos=["org/issues"],
            issue_min_age_minutes=0,
        ),
    )
    o = PipelineOrchestrator(config)
    o._pr_monitor._list_prs = AsyncMock(return_value=[_pr(1), _pr(2)])
    o._pr_monitor._authenticated_user_login = AsyncMock(return_value="me")
    o._issue_monitor._list_issues = AsyncMock(return_value=[_issue(7)])
    return o


# --------------------------------------------------------------------------- #
# the mark
# --------------------------------------------------------------------------- #
def test_the_server_records_where_it_runs(ledger):
    assert state.load_automation_mark(ledger) is None
    assert state.automation_handover(ledger) is None
    state.note_automation(ledger, True)  # first sight: a baseline, no hand-over
    assert state.load_automation_mark(ledger) == {"here": True, "runner": ""}
    state.note_automation(ledger, False, "Laptop")
    assert state.load_automation_mark(ledger) == {"here": False, "runner": "Laptop"}
    # Back here: the SERVER never flips it — the pipeline does, after seeding.
    state.note_automation(ledger, True)
    assert state.automation_handover(ledger) == "Laptop"
    state.mark_automation_here(ledger)
    assert state.automation_handover(ledger) is None


def test_seeding_skips_what_the_ledger_has(ledger):
    state.record_processed_pr(
        ledger,
        ProcessedPR(
            number=1,
            head_sha="old",
            processed_at=datetime.now(timezone.utc),
            repo="org/repo",
        ),
    )
    assert (
        state.seed_processed_prs(ledger, [("org/repo", 1, "x"), ("org/repo", 2, "y")])
        == 1
    )
    assert state.load_processed_prs(ledger) == {("org/repo", 1), ("org/repo", 2)}
    assert state.seed_processed_issues(ledger, [("o/i", 3), ("o/i", 3)]) == 1
    assert state.load_processed_issues(ledger) == {("o/i", 3)}
    entries = state._read_state(ledger)["processed_issues"]
    assert entries[0]["status"] == state.HANDED_OVER


# --------------------------------------------------------------------------- #
# the pipeline
# --------------------------------------------------------------------------- #
def test_a_handover_seeds_both_ledgers_before_the_first_scan(orch, ledger, caplog):
    state.note_automation(ledger, False, "Laptop")
    with caplog.at_level(logging.INFO):
        asyncio.run(orch._ensure_handover())
    assert state.load_processed_prs(ledger) == {("org/repo", 1), ("org/repo", 2)}
    assert state.load_processed_issues(ledger) == {("org/issues", 7)}
    assert state.automation_handover(ledger) is None  # now it runs here
    assert (
        "PR review moved here — 2 open PRs already handled on Laptop are skipped"
        in caplog.text
    )
    assert "Issue handling moved here — 1 open issues" in caplog.text
    # …so the first scans find nothing to review again.
    assert asyncio.run(orch._pr_monitor.scan()) == []
    assert asyncio.run(orch._issue_monitor.scan()) == []
    # A PR opened after the hand-over is reviewed here.
    orch._pr_monitor._list_prs.return_value = [_pr(1), _pr(2), _pr(3)]
    assert [p.number for p in asyncio.run(orch._pr_monitor.scan())] == [3]


def test_no_handover_no_seeding(orch, ledger):
    """It always ran here (or nothing was recorded — a lone device that
    predates groups): every open PR is reviewed as before."""
    asyncio.run(orch._ensure_handover())
    assert state.load_processed_prs(ledger) == set()
    assert [p.number for p in asyncio.run(orch._pr_monitor.scan())] == [1, 2]
    orch._pr_monitor._list_prs.assert_awaited_once()  # only the scan listed


def test_github_unreachable_seeds_nothing_and_scans_nothing(orch, ledger):
    state.note_automation(ledger, False, "Laptop")
    orch._pr_monitor._list_prs = AsyncMock(side_effect=OSError("offline"))
    with pytest.raises(OSError):
        asyncio.run(orch._ensure_handover())
    assert state.automation_handover(ledger) == "Laptop"  # tried again next poll
    assert state.load_processed_prs(ledger) == set()


def test_each_loop_seeds_before_it_scans(orch, ledger, monkeypatch):
    order = []

    async def handover():
        order.append("handover")

    async def scan():
        order.append("scan")
        return []

    async def stop(_s):
        raise asyncio.CancelledError

    monkeypatch.setattr(orch, "_ensure_handover", handover)
    monkeypatch.setattr(orch._pr_monitor, "scan", scan)
    monkeypatch.setattr(orch._issue_monitor, "scan", scan)
    monkeypatch.setattr(orchestrator_mod.asyncio, "sleep", stop)
    for loop in (orch._pr_loop, orch._issue_loop):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(loop())
    assert order == ["handover", "scan", "handover", "scan"]


# --------------------------------------------------------------------------- #
# the server's side
# --------------------------------------------------------------------------- #
def test_the_server_marks_where_it_runs_as_it_moves(ledger, monkeypatch):
    from backend.web.addons import ticket_ingestion as ti
    from backend.web.core import settings_hooks

    where = {"here": True}
    monkeypatch.setattr(settings_hooks, "automation_here", lambda: where["here"])
    monkeypatch.setattr(settings_hooks, "automation_device", lambda: "Laptop")
    assert ti._automation_status(ledger)["automation_here"] is True
    assert state.load_automation_mark(ledger) == {"here": True, "runner": ""}
    where["here"] = False
    assert ti._automation_status(ledger) == {
        "automation_here": False,
        "automation_device": "Laptop",
    }
    assert state.load_automation_mark(ledger) == {"here": False, "runner": "Laptop"}
    where["here"] = True  # moved here: the pipeline it starts seeds first
    ti._observe_automation(ledger)
    assert state.automation_handover(ledger) == "Laptop"


# --------------------------------------------------------------------------- #
# round 4 A: a loop starting for the first time on a grouped device
# --------------------------------------------------------------------------- #
@pytest.fixture
def grouped(monkeypatch):
    from backend.ticket_ingestion.config import FLEET_ENV

    monkeypatch.setenv(FLEET_ENV, "1")


def test_first_run_in_a_group_seeds_without_a_flip(orch, ledger, grouped, caplog):
    """The [0] order: mini was the chosen device while it was alone (its mark
    says "here" — no flip ever happens), then the laptop's repos reached it
    by settings sync. Its PR review / issue handling start with empty
    ledgers in a group: seed them before the first scan, or it re-reviews
    every open PR the laptop already reviewed."""
    state.note_automation(ledger, True)
    assert state.automation_handover(ledger) is None  # no flip to go by
    with caplog.at_level(logging.INFO):
        asyncio.run(orch._ensure_handover())
    assert state.load_processed_prs(ledger) == {("org/repo", 1), ("org/repo", 2)}
    assert state.load_processed_issues(ledger) == {("org/issues", 7)}
    assert (
        "PR review moved here — 2 open PRs already handled on another device "
        "are skipped" in caplog.text
    )
    assert "Issue handling moved here — 1 open issues" in caplog.text
    assert asyncio.run(orch._pr_monitor.scan()) == []
    assert asyncio.run(orch._issue_monitor.scan()) == []
    assert state.ledger_started(ledger, "prs") and state.ledger_started(
        ledger, "issues"
    )


def test_first_run_with_no_mark_at_all_seeds_too(orch, ledger, grouped):
    """No automation_here.json and no state.json: same answer."""
    assert state.load_automation_mark(ledger) is None
    asyncio.run(orch._ensure_handover())
    assert state.load_processed_prs(ledger) == {("org/repo", 1), ("org/repo", 2)}


def test_a_loop_that_ran_here_is_not_seeded(orch, ledger, grouped):
    """PR review has history here (an entry): only the issue ledger — which
    never ran — is seeded; a PR this device never reviewed is still
    reviewed."""
    state.record_processed_pr(
        ledger,
        ProcessedPR(
            number=1,
            head_sha="sha1",
            processed_at=datetime.now(timezone.utc),
            repo="org/repo",
        ),
    )
    asyncio.run(orch._ensure_handover())
    assert [p.number for p in asyncio.run(orch._pr_monitor.scan())] == [2]
    assert state.load_processed_issues(ledger) == {("org/issues", 7)}


def test_alone_a_first_run_reviews_everything(orch, ledger, monkeypatch):
    from backend.ticket_ingestion.config import FLEET_ENV

    monkeypatch.setenv(FLEET_ENV, "0")
    asyncio.run(orch._ensure_handover())
    assert state.load_processed_prs(ledger) == set()
    assert [p.number for p in asyncio.run(orch._pr_monitor.scan())] == [1, 2]
    # …and it has run here now: joining a group later seeds nothing.
    assert state.ledger_started(ledger, "prs")


def test_the_first_run_seeds_once(orch, ledger, grouped):
    """Nothing open at the first run leaves the ledger empty, but the loop
    has run: a later restart (PRs opened meanwhile were reviewed here) does
    not seed again."""
    orch._pr_monitor._list_prs.return_value = []
    asyncio.run(orch._ensure_handover())
    assert state.load_processed_prs(ledger) == set()
    again = PipelineOrchestrator(orch.config)
    again._pr_monitor._list_prs = AsyncMock(return_value=[_pr(3)])
    again._pr_monitor._authenticated_user_login = AsyncMock(return_value="me")
    again._issue_monitor._list_issues = AsyncMock(return_value=[])
    asyncio.run(again._ensure_handover())
    assert state.load_processed_prs(ledger) == set()
    assert [p.number for p in asyncio.run(again._pr_monitor.scan())] == [3]


def test_ledger_started_reads_the_state_file(ledger):
    assert state.ledger_started(ledger, "prs") is False  # no file
    state.record_pr_attempt(ledger, "o/r", 5)
    assert state.ledger_started(ledger, "prs") is True  # an attempt counts
    assert state.ledger_started(ledger, "issues") is False
    state.mark_ledger_started(ledger, "issues")
    assert state.ledger_started(ledger, "issues") is True


def test_seeding_skips_what_is_still_in_its_grace_period(orch, ledger, grouped):
    """A PR / issue younger than its min age wasn't eligible on the other
    device yet, so nobody handled it: seeding it would mean nobody ever
    does. Only what was already old enough is seeded; the young one is
    reviewed here once it ages in."""
    from datetime import timedelta

    fresh_pr = _pr(3)
    fresh_pr.created_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    fresh_issue = _issue(8)
    fresh_issue.created_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    orch._pr_monitor._list_prs = AsyncMock(return_value=[_pr(1), fresh_pr])
    orch._issue_monitor._list_issues = AsyncMock(return_value=[_issue(7), fresh_issue])
    orch.config.github.min_age_minutes = 30
    orch.config.github.issue_min_age_minutes = 30
    asyncio.run(orch._ensure_handover())
    assert state.load_processed_prs(ledger) == {("org/repo", 1)}
    assert state.load_processed_issues(ledger) == {("org/issues", 7)}
    # Once it ages past the grace period here, it is this device's to handle.
    orch.config.github.min_age_minutes = 0
    orch.config.github.issue_min_age_minutes = 0
    assert [p.number for p in asyncio.run(orch._pr_monitor.scan())] == [3]
    assert [i.number for i in asyncio.run(orch._issue_monitor.scan())] == [8]


# --------------------------------------------------------------------------- #
# a device that can't yet tell which member it is (Tailscale still coming up)
# --------------------------------------------------------------------------- #
@pytest.fixture
def booting_laptop(monkeypatch, isolate_settings_store):
    """A group of two where THIS machine — hostname ``EthanLaptop2026``,
    member key ``ethanlaptopwsl2026`` (its tailnet name) — runs PR review.
    ``me`` is what ``remote.self_identity`` answers: before Tailscale is up
    it falls back to the hostname, which is not a member key."""
    from backend.config import settings as S
    from backend.web.core import fleet, remote

    monkeypatch.setattr(fleet, "in_fleet", lambda: True)
    monkeypatch.setattr(
        fleet,
        "live_members",
        lambda: {
            "ethanlaptopwsl2026": {"host": "EthanLaptop2026"},
            "ethans-mac-mini": {"host": "Ethan's Mac mini"},
        },
    )
    me = {"key": "ethanlaptop2026"}
    monkeypatch.setattr(remote, "self_identity", lambda: dict(me))
    S.update_settings(github={"automation_device": "ethanlaptopwsl2026"})
    return me


def test_the_server_records_nothing_while_it_cannot_tell_which_device_it_is(
    ledger, booting_laptop
):
    """It used to note "runs on EthanLaptop2026" — this machine, by its own
    roster entry's host — and the next pipeline then seeded every open PR as
    handled on that "other" device, so none was ever reviewed."""
    from backend.web.addons import ticket_ingestion as ti
    from backend.web.core import settings_hooks

    assert settings_hooks.automation_known() is False
    assert settings_hooks.automation_here() is False  # the safe guess
    assert settings_hooks.automation_device() == ""  # never itself
    assert ti._observe_automation(ledger) is False
    assert state.load_automation_mark(ledger) is None  # a guess isn't recorded
    # Tailscale is up: it knows it is the runner, and nothing moved.
    booting_laptop["key"] = "ethanlaptopwsl2026"
    assert settings_hooks.automation_known() is True
    assert ti._observe_automation(ledger) is True
    assert state.automation_handover(ledger) is None
    # The person picks the Mac mini: a real "runs elsewhere", by its host…
    from backend.config import settings as S

    S.update_settings(github={"automation_device": "ethans-mac-mini"})
    assert settings_hooks.automation_device() == "Ethan's Mac mini"
    assert ti._observe_automation(ledger) is False
    assert state.load_automation_mark(ledger) == {
        "here": False,
        "runner": "Ethan's Mac mini",
    }
    # …and picking this laptop again is a hand-over FROM the Mac mini.
    S.update_settings(github={"automation_device": "ethanlaptopwsl2026"})
    assert ti._observe_automation(ledger) is True
    assert state.automation_handover(ledger) == "Ethan's Mac mini"


def test_a_boot_window_then_tailscale_seeds_nothing(
    orch, ledger, grouped, booting_laptop
):
    """End to end: the laptop has run PR review all along (its ledgers have
    history), boots before Tailscale, then learns it is the runner. Its
    pipeline must review every open PR — not mark them handled elsewhere."""
    from backend.web.addons import ticket_ingestion as ti

    state.note_automation(ledger, True)  # it ran here before the reboot
    state.mark_ledger_started(ledger, "prs")
    state.mark_ledger_started(ledger, "issues")
    assert ti._observe_automation(ledger) is False  # the boot window
    booting_laptop["key"] = "ethanlaptopwsl2026"  # Tailscale is up
    assert ti._observe_automation(ledger) is True
    assert state.automation_handover(ledger) is None
    asyncio.run(orch._ensure_handover())
    assert state.load_processed_prs(ledger) == set()
    assert state.load_processed_issues(ledger) == set()
    assert [p.number for p in asyncio.run(orch._pr_monitor.scan())] == [1, 2]
    assert [i.number for i in asyncio.run(orch._issue_monitor.scan())] == [7]


def test_a_boot_window_keeps_a_real_mark(ledger, booting_laptop):
    """What another device really ran stays recorded through a boot window:
    the guess neither overwrites nor clears it."""
    from backend.web.addons import ticket_ingestion as ti

    state.note_automation(ledger, False, "Ethan's Mac mini")
    assert ti._observe_automation(ledger) is False
    assert ti._automation_status(ledger) == {
        "automation_here": False,
        "automation_device": "",
    }
    assert state.load_automation_mark(ledger) == {
        "here": False,
        "runner": "Ethan's Mac mini",
    }


def test_an_answer_of_here_is_recorded_whatever_automation_known_says(
    ledger, monkeypatch
):
    """Only a "not here" is a guess worth withholding; "here" is the runner's
    own answer (unreachable with automation_known False in practice) and is
    recorded as the baseline — a "here" mark never seeds anything."""
    from backend.web.addons import ticket_ingestion as ti
    from backend.web.core import settings_hooks

    monkeypatch.setattr(settings_hooks, "automation_known", lambda: False)
    monkeypatch.setattr(settings_hooks, "automation_here", lambda: True)
    assert ti._observe_automation(ledger) is True
    assert state.load_automation_mark(ledger) == {"here": True, "runner": ""}
    assert state.automation_handover(ledger) is None


def test_a_mark_naming_this_machine_is_no_handover(orch, ledger, monkeypatch):
    """A mark an older server already wrote that way seeds nothing either."""
    import socket

    monkeypatch.setattr(socket, "gethostname", lambda: "EthanLaptop2026")
    state.note_automation(ledger, False, "EthanLaptop2026")
    assert state.automation_handover(ledger) is None
    asyncio.run(orch._ensure_handover())
    assert state.load_processed_prs(ledger) == set()
    assert [p.number for p in asyncio.run(orch._pr_monitor.scan())] == [1, 2]
    # Another device's name still hands over.
    state.note_automation(ledger, False, "Ethan's Mac mini")
    assert state.automation_handover(ledger) == "Ethan's Mac mini"


def test_this_machine_is_matched_ignoring_case_and_spaces(ledger, monkeypatch, caplog):
    import socket

    monkeypatch.setattr(socket, "gethostname", lambda: "EthanLaptop2026")
    state.note_automation(ledger, False, " ethanlaptop2026 ")
    with caplog.at_level(logging.WARNING, logger=state.__name__):
        assert state.automation_handover(ledger) is None
    assert "Ignoring an automation hand-over" in caplog.text
    assert "that is this machine" in caplog.text


def test_an_unnamed_runner_is_still_another_device(ledger, monkeypatch):
    import socket

    monkeypatch.setattr(socket, "gethostname", lambda: "EthanLaptop2026")
    assert state._names_this_machine("") is False
    assert state._names_this_machine("   ") is False
    state.note_automation(ledger, False, "")
    assert state.automation_handover(ledger) == "another device"


def test_an_unreadable_hostname_names_no_machine(ledger, monkeypatch):
    import socket

    def boom():
        raise OSError("no hostname")

    monkeypatch.setattr(socket, "gethostname", boom)
    assert state._names_this_machine("EthanLaptop2026") is False
    state.note_automation(ledger, False, "EthanLaptop2026")
    assert state.automation_handover(ledger) == "EthanLaptop2026"


def test_no_mark_or_a_here_mark_is_still_no_handover(ledger, monkeypatch):
    import socket

    monkeypatch.setattr(socket, "gethostname", lambda: "EthanLaptop2026")
    assert state.automation_handover(ledger) is None
    state.note_automation(ledger, True)
    assert state.automation_handover(ledger) is None
