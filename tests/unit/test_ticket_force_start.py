"""Force-start tickets (Work -> Tickets -> Assigned tickets).

Sibling of ``test_pr_force_review``: exercises ``ticket_start.skip_reasons``,
which mirrors the pipeline's own filters (``BackfillScanner.scan`` +
``PipelineOrchestrator.process_story``) so the UI's "why isn't this ticket
being ingested?" chips are truthful. Every skip branch is covered plus the
eligible (no-reasons) case. No network, no config, no filesystem.
"""

from __future__ import annotations

import types
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.ticket_ingestion.models import Attachment
from backend.ticket_ingestion.state import load_processed_story_statuses
from backend.web.core import ticket_start
from tests._factories import make_ticket


def _story(**over):
    """A Ticket assigned to ``member-123`` (the configured member below)."""
    return make_ticket(**over)


# The configured member id used across these tests; matches the default
# ``owner_ids`` of ``_story()`` so the assignee check passes unless overridden.
MEMBER_IDS = ["member-123"]


def test_eligible_ticket_has_no_skip_reasons():
    # Nothing in the ledger, not pending, no branch, assigned, no state filter.
    assert ticket_start.skip_reasons(_story(), {}, set(), set(), MEMBER_IDS) == []


def test_eligible_with_no_member_id_configured():
    # Empty member_ids => AssigneeFilter is a no-op (provider filtered already).
    assert ticket_start.skip_reasons(_story(), {}, set(), set(), []) == []


@pytest.mark.parametrize(
    "status, fragment",
    [
        ("completed", "already ingested"),
        ("in_flight", "in flight"),
        ("failed", "failed earlier"),
        ("skipped", "skipped earlier"),
        ("mystery", "already in the processed ledger (mystery)"),
    ],
)
def test_ledger_status_is_flagged(status, fragment):
    story = _story()
    reasons = ticket_start.skip_reasons(
        story, {story.slug: status}, set(), set(), MEMBER_IDS
    )
    assert any(fragment in r for r in reasons)


def test_ledger_matches_by_id_when_slug_absent():
    # The ledger may be keyed by the provider-native id rather than the slug.
    story = _story(id=1)  # slug defaults to "sc-1"
    reasons = ticket_start.skip_reasons(
        story, {"1": "completed"}, set(), set(), MEMBER_IDS
    )
    assert any("already ingested" in r for r in reasons)


def test_ledger_entry_does_not_flag_a_different_ticket():
    # sc-1 recorded completed must not flag sc-2.
    other = _story(id=2)
    reasons = ticket_start.skip_reasons(
        other, {"sc-1": "completed"}, set(), set(), MEMBER_IDS
    )
    assert not any("already ingested" in r for r in reasons)


def test_pending_ticket_is_flagged():
    story = _story()
    reasons = ticket_start.skip_reasons(story, {}, {story.slug}, set(), MEMBER_IDS)
    assert any("pending" in r for r in reasons)


def test_existing_feature_branch_is_flagged():
    story = _story()  # slug "sc-1"
    branches = {f"feature/{story.slug}/fix-the-thing"}
    reasons = ticket_start.skip_reasons(story, {}, set(), branches, MEMBER_IDS)
    assert any("feature branch" in r for r in reasons)


def test_branch_for_a_different_slug_does_not_flag():
    story = _story(id=1)  # slug "sc-1"
    branches = {"feature/sc-2/other-thing"}
    reasons = ticket_start.skip_reasons(story, {}, set(), branches, MEMBER_IDS)
    assert not any("feature branch" in r for r in reasons)


def test_unassigned_ticket_is_flagged():
    story = _story(owner_ids=["someone-else"])
    reasons = ticket_start.skip_reasons(story, {}, set(), set(), MEMBER_IDS)
    assert any("not assigned" in r for r in reasons)


def test_ingest_state_filter_flags_other_buckets():
    story = _story(state="Backlog")
    reasons = ticket_start.skip_reasons(
        story, {}, set(), set(), MEMBER_IDS, ingest_state=["In Progress"]
    )
    assert any("not in an ingest state" in r for r in reasons)


def test_ingest_state_filter_passes_matching_bucket():
    story = _story(state="In Progress")
    reasons = ticket_start.skip_reasons(
        story, {}, set(), set(), MEMBER_IDS, ingest_state=["In Progress"]
    )
    assert not any("ingest state" in r for r in reasons)


def test_unknown_state_is_never_flagged_by_the_filter():
    # A provider that doesn't annotate state already filtered server-side.
    story = _story(state="")
    reasons = ticket_start.skip_reasons(
        story, {}, set(), set(), MEMBER_IDS, ingest_state=["In Progress"]
    )
    assert not any("ingest state" in r for r in reasons)


def test_multiple_reasons_accumulate():
    story = _story(state="Backlog", owner_ids=["someone-else"])
    reasons = ticket_start.skip_reasons(
        story,
        {story.slug: "failed"},
        {story.slug},
        {f"feature/{story.slug}/x"},
        MEMBER_IDS,
        ingest_state=["In Progress"],
    )
    # All five independent filters fire at once.
    assert len(reasons) == 5


# --------------------------------------------------------------------------- #
# _resolve_repo_root / _load_config
# --------------------------------------------------------------------------- #
def test_resolve_repo_root_honors_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MINDFLOCK_REPO_ROOT", str(tmp_path))
    assert ticket_start._resolve_repo_root() == tmp_path.resolve()


def test_resolve_repo_root_finds_config_toml_ancestor(monkeypatch, tmp_path):
    # The resolver walks the module file's ancestors for a config.toml. Point the
    # module __file__ at a synthetic tree so the test verifies the walk itself,
    # not whether the checkout happens to carry a (gitignored) config.toml.
    monkeypatch.delenv("MINDFLOCK_REPO_ROOT", raising=False)
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "config.toml").write_text("[ticketing]\n")
    monkeypatch.setattr(ticket_start, "__file__", str(root / "pkg" / "ticket_start.py"))
    assert ticket_start._resolve_repo_root() == root.resolve()


def test_load_config_reanchors_relative_workspace_dir(monkeypatch, tmp_path):
    from pathlib import Path

    import backend.ticket_ingestion.config as cfg_mod

    monkeypatch.setattr(ticket_start, "_REPO_ROOT", tmp_path)
    fake = types.SimpleNamespace(workspace_dir=Path("workspaces"))
    monkeypatch.setattr(cfg_mod, "load_config", lambda: fake)
    out = ticket_start._load_config()
    assert out.workspace_dir == tmp_path / "workspaces"  # anchored at repo root


def test_load_config_leaves_absolute_workspace_dir(monkeypatch, tmp_path):
    from pathlib import Path

    import backend.ticket_ingestion.config as cfg_mod

    monkeypatch.setattr(ticket_start, "_REPO_ROOT", tmp_path)
    abs_dir = Path("/var/lib/mindflock/ws")
    fake = types.SimpleNamespace(workspace_dir=abs_dir)
    monkeypatch.setattr(cfg_mod, "load_config", lambda: fake)
    assert ticket_start._load_config().workspace_dir == abs_dir  # untouched


# --------------------------------------------------------------------------- #
# session_title / branch_for / workspace_mode
# --------------------------------------------------------------------------- #
def test_session_title_is_the_slug():
    # Matches SessionRunner so the panel's has-session check collides with a
    # pipeline-launched session (one session per ticket either way).
    assert ticket_start.session_title(_story(id=42)) == "sc-42"


def test_branch_for_uses_pipeline_naming():
    story = _story(id=7, name="Fix the thing")
    assert ticket_start.branch_for(story) == "feature/sc-7/fix-the-thing"


class _Cfg:
    def __init__(self, mode="worktree"):
        self.engine = type("E", (), {"mode": mode})()


def test_workspace_mode_reads_engine_mode(monkeypatch):
    monkeypatch.setattr(ticket_start, "_load_config", lambda: _Cfg(mode="clone"))
    assert ticket_start.workspace_mode() == "clone"


def test_workspace_mode_defaults_to_worktree_when_unset(monkeypatch):
    monkeypatch.setattr(ticket_start, "_load_config", lambda: _Cfg(mode=""))
    assert ticket_start.workspace_mode() == "worktree"


def test_workspace_mode_swallows_config_errors(monkeypatch):
    def boom():
        raise RuntimeError("no config")

    monkeypatch.setattr(ticket_start, "_load_config", boom)
    assert ticket_start.workspace_mode() == "worktree"


# --------------------------------------------------------------------------- #
# build_prompt
# --------------------------------------------------------------------------- #
def test_build_prompt_without_attachments(monkeypatch):
    # _load_config is only used to construct the (stateless) runner; a dummy is
    # enough since _build_prompt reads solely from the story.
    monkeypatch.setattr(ticket_start, "_load_config", lambda: object())
    prompt = ticket_start.build_prompt(_story(id=5, name="Add feature"))
    assert "# Story: Add feature" in prompt
    assert "## Attached Files" not in prompt  # no attachments -> no note


def test_build_prompt_appends_attachment_note(monkeypatch):
    monkeypatch.setattr(ticket_start, "_load_config", lambda: object())
    story = _story(
        attachments=[
            Attachment(name="spec.pdf", url="http://x/spec.pdf"),
            Attachment(name="diagram.png", url="http://x/diagram.png"),
        ]
    )
    prompt = ticket_start.build_prompt(story)
    assert "## Attached Files" in prompt
    assert "2 attachment(s)" in prompt
    assert "spec.pdf, diagram.png" in prompt
    assert ".ticket_attachments/" in prompt


# --------------------------------------------------------------------------- #
# record_started / record_result ledger round-trip
# --------------------------------------------------------------------------- #
@pytest.fixture
def ledger_root(monkeypatch, tmp_path):
    """Anchor the module's state.json at an empty tmp dir."""
    monkeypatch.setattr(ticket_start, "_REPO_ROOT", tmp_path)
    return tmp_path


def test_record_started_marks_in_flight(ledger_root):
    story = _story(id=1)
    ticket_start.record_started(story)
    assert load_processed_story_statuses(ledger_root)[story.slug] == "in_flight"


def test_record_result_flips_in_flight_to_completed(ledger_root):
    story = _story(id=1)
    ticket_start.record_started(story)
    ticket_start.record_result(story, branch="feature/sc-1/x")
    assert load_processed_story_statuses(ledger_root)[story.slug] == "completed"


def test_record_result_marks_failed_on_error(ledger_root):
    story = _story(id=1)
    ticket_start.record_started(story)
    ticket_start.record_result(story, error="clone failed")
    assert load_processed_story_statuses(ledger_root)[story.slug] == "failed"


# --------------------------------------------------------------------------- #
# find_ticket (async; provider mocked)
# --------------------------------------------------------------------------- #
async def test_find_ticket_returns_provider_story(monkeypatch):
    from backend.ticket_ingestion import providers as providers_mod

    src = types.SimpleNamespace(id="shortcut", provider="shortcut", repo_url="acme/app")
    monkeypatch.setattr(
        ticket_start,
        "_load_config",
        lambda: types.SimpleNamespace(ticketing_sources=[src]),
    )
    story = _story(id=9)
    prov = types.SimpleNamespace(fetch=AsyncMock(return_value=story))
    monkeypatch.setattr(providers_mod, "get_provider", lambda s: prov)

    result = await ticket_start.find_ticket("shortcut", "9")
    assert result is story
    assert result.repo_url == "acme/app"  # tagged from the matching source
    prov.fetch.assert_awaited_once_with("9")


async def test_find_ticket_unknown_source_raises(monkeypatch):
    monkeypatch.setattr(
        ticket_start,
        "_load_config",
        lambda: types.SimpleNamespace(ticketing_sources=[]),
    )
    with pytest.raises(LookupError, match="No ticketing source"):
        await ticket_start.find_ticket("nope", "1")


# --------------------------------------------------------------------------- #
# list_assigned_tickets (async; providers + branch listing mocked)
# --------------------------------------------------------------------------- #
async def test_list_assigned_tickets_annotates_and_orders_buckets(
    monkeypatch, tmp_path
):
    from backend.ticket_ingestion import backfill
    from backend.ticket_ingestion import providers as providers_mod
    from backend.ticket_ingestion.providers import base as providers_base

    monkeypatch.setattr(ticket_start, "_REPO_ROOT", tmp_path)  # empty ledger/pending
    src = types.SimpleNamespace(
        id="shortcut",
        provider="shortcut",
        repo_url="acme/app",
        member_id="member-123",
        workflow_state_id=None,
    )
    monkeypatch.setattr(
        ticket_start,
        "_load_config",
        lambda: types.SimpleNamespace(ticketing_sources=[src], repo_url="acme/app"),
    )
    s1 = _story(id=1, state="In Progress")  # assigned to member-123 -> eligible
    s2 = _story(id=2, state="Backlog", owner_ids=["someone-else"])  # unassigned
    prov = types.SimpleNamespace(
        label="Shortcut",
        search_assigned_all=AsyncMock(return_value=[s1, s2]),
        list_states=AsyncMock(
            return_value=[
                {"id": "1", "name": "Backlog", "type": "unstarted"},
                {"id": "2", "name": "In Progress", "type": "started"},
                {"id": "3", "name": "Done", "type": "done"},
            ]
        ),
    )
    monkeypatch.setattr(providers_mod, "get_provider", lambda s: prov)
    monkeypatch.setattr(providers_base, "workflow_state_list", lambda s: [])

    async def _no_branches(repo):
        return set()

    monkeypatch.setattr(backfill, "_get_existing_branches", _no_branches)

    out = await ticket_start.list_assigned_tickets()
    assert out["sources"] == ["shortcut"]
    # Buckets follow workflow order, restricted to those that hold tickets;
    # the empty "Done" bucket is dropped.
    assert out["buckets"] == ["Backlog", "In Progress"]
    assert out["done_buckets"] == []  # Done holds no tickets
    by_slug = {t["slug"]: t for t in out["tickets"]}
    assert by_slug["sc-1"]["eligible"] is True
    assert by_slug["sc-1"]["reasons"] == []
    assert by_slug["sc-2"]["eligible"] is False
    assert any("not assigned" in r for r in by_slug["sc-2"]["reasons"])
    assert out["errors"] == []


async def test_list_assigned_tickets_on_an_any_assignee_source(monkeypatch, tmp_path):
    """A QA queue's tickets belong to whoever wrote the code, so "not assigned
    to you" stops being a reason to skip one — and the panel has to say whose
    each row is, since they are no longer all yours."""
    from backend.ticket_ingestion import backfill
    from backend.ticket_ingestion import providers as providers_mod

    monkeypatch.setattr(ticket_start, "_REPO_ROOT", tmp_path)
    src = types.SimpleNamespace(
        id="shortcut",
        provider="shortcut",
        repo_url="acme/app",
        member_id="member-123",
        workflow_state="2",
        workflow_state_id=None,
        assignee_scope="anyone",
    )
    monkeypatch.setattr(
        ticket_start,
        "_load_config",
        lambda: types.SimpleNamespace(ticketing_sources=[src], repo_url="acme/app"),
    )
    mine = _story(id=1, state="In Progress")
    theirs = _story(
        id=2, state="In Progress", owner_ids=["someone-else"], owner_names=["Mauricio"]
    )
    prov = types.SimpleNamespace(
        label="Shortcut",
        search_assigned_all=AsyncMock(return_value=[mine, theirs]),
        list_states=AsyncMock(
            return_value=[{"id": "2", "name": "In Progress", "type": "started"}]
        ),
    )
    monkeypatch.setattr(providers_mod, "get_provider", lambda s: prov)

    async def _no_branches(repo):
        return set()

    monkeypatch.setattr(backfill, "_get_existing_branches", _no_branches)

    out = await ticket_start.list_assigned_tickets()
    by_slug = {t["slug"]: t for t in out["tickets"]}
    assert by_slug["sc-2"]["eligible"] is True
    assert by_slug["sc-2"]["reasons"] == []
    assert by_slug["sc-2"]["mine"] is False
    assert by_slug["sc-2"]["assignee"] == "Mauricio"
    assert by_slug["sc-1"]["mine"] is True


async def test_list_assigned_tickets_marks_everything_mine_when_scoped_to_me(
    monkeypatch, tmp_path
):
    """Without an any-assignee source the provider already searched by assignee,
    so every row is yours — even when no member id was ever filled in, which
    would otherwise make the whole panel look like other people's work."""
    from backend.ticket_ingestion import backfill
    from backend.ticket_ingestion import providers as providers_mod

    monkeypatch.setattr(ticket_start, "_REPO_ROOT", tmp_path)
    src = types.SimpleNamespace(
        id="linear",
        provider="linear",
        repo_url="acme/app",
        member_id="",
        workflow_state="",
        workflow_state_id=None,
    )
    monkeypatch.setattr(
        ticket_start,
        "_load_config",
        lambda: types.SimpleNamespace(ticketing_sources=[src], repo_url="acme/app"),
    )
    prov = types.SimpleNamespace(
        label="Linear",
        search_assigned_all=AsyncMock(return_value=[_story(id=1, owner_ids=[])]),
        list_states=AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(providers_mod, "get_provider", lambda s: prov)

    async def _no_branches(repo):
        return set()

    monkeypatch.setattr(backfill, "_get_existing_branches", _no_branches)

    out = await ticket_start.list_assigned_tickets()
    assert out["tickets"][0]["mine"] is True


async def test_list_assigned_tickets_reports_per_source_errors(monkeypatch, tmp_path):
    from backend.ticket_ingestion import providers as providers_mod

    monkeypatch.setattr(ticket_start, "_REPO_ROOT", tmp_path)
    src = types.SimpleNamespace(
        id="jira",
        provider="jira",
        repo_url="",
        member_id="",
        workflow_state_id=None,
    )
    monkeypatch.setattr(
        ticket_start,
        "_load_config",
        lambda: types.SimpleNamespace(ticketing_sources=[src], repo_url=""),
    )
    prov = types.SimpleNamespace(
        search_assigned_all=AsyncMock(side_effect=RuntimeError("bad token"))
    )
    monkeypatch.setattr(providers_mod, "get_provider", lambda s: prov)

    out = await ticket_start.list_assigned_tickets()
    assert out["tickets"] == []
    assert out["sources"] == []  # a failing source never gets listed
    assert out["errors"] == [{"source": "jira", "error": "bad token"}]


# --------------------------------------------------------------------------- #
# download_attachments (async; best-effort post-launch drop)
# --------------------------------------------------------------------------- #
async def test_download_attachments_noop_without_attachments(monkeypatch):
    # Guard fires before _load_config / the runner are ever touched.
    monkeypatch.setattr(
        ticket_start,
        "_load_config",
        lambda: (_ for _ in ()).throw(AssertionError("must not load config")),
    )
    inst = types.SimpleNamespace(GetWorktreePath=lambda: "/wt")
    await ticket_start.download_attachments(inst, _story())  # returns, no raise


async def test_download_attachments_swallows_runner_errors(monkeypatch):
    from backend.ticket_ingestion import claude_runner

    monkeypatch.setattr(ticket_start, "_load_config", lambda: object())

    class _FakeRunner:
        def __init__(self, cfg):
            pass

        async def _download_attachments(self, wp, story):
            raise RuntimeError("network down")

    monkeypatch.setattr(claude_runner, "ClaudeCodeRunner", _FakeRunner)
    inst = types.SimpleNamespace(GetWorktreePath=lambda: "/wt")
    story = _story(attachments=[Attachment(name="a", url="u")])
    # The warning is logged; the exception must not escape.
    await ticket_start.download_attachments(inst, story)


async def test_download_attachments_noop_without_worktree(monkeypatch):
    # Attachments present but no worktree yet -> returns before the runner runs.
    monkeypatch.setattr(ticket_start, "_load_config", lambda: object())
    inst = types.SimpleNamespace(GetWorktreePath=lambda: "")
    story = _story(attachments=[Attachment(name="a", url="u")])
    await ticket_start.download_attachments(inst, story)  # no raise


async def test_list_assigned_tickets_workflow_state_id_filter(monkeypatch, tmp_path):
    # A source configured only via the integer workflow_state_id (Shortcut's
    # legacy filter) still resolves to an ingest-state bucket name, and a ticket
    # in a different bucket is flagged "not in an ingest state".
    from backend.ticket_ingestion import backfill
    from backend.ticket_ingestion import providers as providers_mod
    from backend.ticket_ingestion.providers import base as providers_base

    monkeypatch.setattr(ticket_start, "_REPO_ROOT", tmp_path)
    src = types.SimpleNamespace(
        id="shortcut",
        provider="shortcut",
        repo_url="acme/app",
        member_id="member-123",
        workflow_state_id=2,
    )
    monkeypatch.setattr(
        ticket_start,
        "_load_config",
        lambda: types.SimpleNamespace(ticketing_sources=[src], repo_url="acme/app"),
    )
    s1 = _story(id=1, state="Backlog")
    prov = types.SimpleNamespace(
        label="Shortcut",
        search_assigned_all=AsyncMock(return_value=[s1]),
        list_states=AsyncMock(
            return_value=[
                {"id": "1", "name": "Backlog", "type": "unstarted"},
                {"id": "2", "name": "In Progress", "type": "started"},
            ]
        ),
    )
    monkeypatch.setattr(providers_mod, "get_provider", lambda s: prov)
    monkeypatch.setattr(providers_base, "workflow_state_list", lambda s: [])

    async def _no_branches(repo):
        return set()

    monkeypatch.setattr(backfill, "_get_existing_branches", _no_branches)

    out = await ticket_start.list_assigned_tickets()
    assert out["ingest_states"] == {"shortcut": ["In Progress"]}
    ticket = out["tickets"][0]
    assert any("not in an ingest state" in r for r in ticket["reasons"])


async def test_list_assigned_tickets_states_failure_and_branch_error(
    monkeypatch, tmp_path
):
    # list_states blowing up leaves the buckets to be discovered from the
    # tickets themselves; a stateless ticket sinks into the trailing "No state"
    # bucket, and a failing branch lookup degrades to "no branches".
    from backend.ticket_ingestion import backfill
    from backend.ticket_ingestion import providers as providers_mod
    from backend.ticket_ingestion.providers import base as providers_base

    monkeypatch.setattr(ticket_start, "_REPO_ROOT", tmp_path)
    src = types.SimpleNamespace(
        id="gh",
        provider="github_issues",
        repo_url="acme/app",
        member_id="",
        workflow_state_id=None,
    )
    monkeypatch.setattr(
        ticket_start,
        "_load_config",
        lambda: types.SimpleNamespace(ticketing_sources=[src], repo_url="acme/app"),
    )
    s1 = _story(id=1, state="")  # provider doesn't annotate state
    prov = types.SimpleNamespace(
        label="GitHub",
        search_assigned_all=AsyncMock(return_value=[s1]),
        list_states=AsyncMock(side_effect=RuntimeError("states unsupported")),
    )
    monkeypatch.setattr(providers_mod, "get_provider", lambda s: prov)
    monkeypatch.setattr(providers_base, "workflow_state_list", lambda s: [])

    async def _boom(repo):
        raise RuntimeError("git ls-remote failed")

    monkeypatch.setattr(backfill, "_get_existing_branches", _boom)

    out = await ticket_start.list_assigned_tickets()
    assert out["buckets"] == [ticket_start.NO_STATE_BUCKET]
    assert out["tickets"][0]["bucket"] == ticket_start.NO_STATE_BUCKET
    # Branch lookup failed but listing still succeeds (no "feature branch" chip).
    assert not any("feature branch" in r for r in out["tickets"][0]["reasons"])


# --------------------------------------------------------------------------- #
# A failure's chip says what actually went wrong
# --------------------------------------------------------------------------- #
# The old chip read "failed earlier — delete its state.json ledger entry to
# retry". Both halves misled: force-start never consults the ledger (only a live
# session blocks it), and the usual cause is a leftover worktree still holding
# the branch — so clearing the ledger entry drops the RECORD of the failure and
# the retry fails identically.
_REAL_REASON = (
    "failed to create provisioned worktree: branch "
    "'feature/shortcut-19674/config-build-the-orchestrator-wiring-lay' is already "
    "checked out at /home/u/.mindflock/worktrees/feature/shortcut-19674/x_18c8. "
    "Kill that session first, or use a different story id / title."
)


def test_failed_chip_carries_the_recorded_reason():
    story = _story()
    reasons = ticket_start.skip_reasons(
        story,
        {story.slug: "failed"},
        set(),
        set(),
        MEMBER_IDS,
        failures={story.slug: _REAL_REASON},
    )
    (chip,) = [r for r in reasons if r.startswith("failed earlier")]
    assert "already checked out" in chip
    # The remedy that does not fix anything is gone.
    assert "state.json" not in chip


def test_failed_reason_is_never_clipped_by_the_server():
    """The actionable half of these reasons is at the END, so a server-side
    character budget would remove exactly the part worth reading. The chip wraps
    in CSS and keeps the full text in its title."""
    chip = ticket_start._failed_label(_REAL_REASON)
    assert chip.endswith("different story id / title.")
    assert "…" not in chip


def test_failed_reason_whitespace_is_normalized():
    """Captured output arrives with newlines in it; a chip is one line."""
    assert ticket_start._failed_label("line one\n  line two") == (
        "failed earlier: line one line two"
    )


def test_failed_with_no_recorded_reason_still_points_at_the_button():
    chip = ticket_start._failed_label("")
    assert "Run ticket" in chip
    assert "state.json" not in chip


def test_failed_chip_matches_by_id_when_the_slug_is_absent():
    story = _story(id=1)  # slug defaults to "sc-1"
    reasons = ticket_start.skip_reasons(
        story, {"1": "failed"}, set(), set(), MEMBER_IDS, failures={"1": _REAL_REASON}
    )
    assert any("already checked out" in r for r in reasons)


def test_missing_failures_map_degrades_to_the_generic_label():
    """skip_reasons is called from more than one place; the argument is optional
    and its absence must not crash the panel."""
    story = _story()
    reasons = ticket_start.skip_reasons(
        story, {story.slug: "failed"}, set(), set(), MEMBER_IDS
    )
    assert any(r.startswith("failed earlier") for r in reasons)


def test_failure_reasons_loader_keeps_the_reason(tmp_path):
    from backend.ticket_ingestion.state import (
        load_processed_story_failures,
        record_processed_story,
        update_processed_story,
    )
    from backend.ticket_ingestion.models import ProcessingRecord
    from datetime import datetime, timezone

    record_processed_story(
        tmp_path,
        ProcessingRecord(
            story_id="sc-9",
            branch="sc-9",
            status="in_flight",
            processed_at=datetime.now(timezone.utc),
        ),
    )
    update_processed_story(tmp_path, "sc-9", status="failed", failure_reason="boom")
    assert load_processed_story_failures(tmp_path) == {"sc-9": "boom"}


def test_a_later_success_supersedes_an_earlier_failure_reason(tmp_path):
    from backend.ticket_ingestion.state import (
        load_processed_story_failures,
        record_processed_story,
    )
    from backend.ticket_ingestion.models import ProcessingRecord
    from datetime import datetime, timezone

    for status, reason in (("failed", "boom"), ("completed", None)):
        record_processed_story(
            tmp_path,
            ProcessingRecord(
                story_id="sc-9",
                branch="sc-9",
                status=status,
                processed_at=datetime.now(timezone.utc),
                failure_reason=reason,
            ),
        )
    assert load_processed_story_failures(tmp_path) == {}


# --------------------------------------------------------------------------- #
# A source's thinking effort reaches BOTH launch paths
#
# A per-source default whose only effect is on tickets the pipeline happens to
# pick up would be a setting that works or not depending on which button you
# pressed. Both paths resolve the same chain: the ticket's own stamp, then the
# source's rung.
# --------------------------------------------------------------------------- #
def test_a_hand_started_ticket_inherits_its_sources_effort(monkeypatch):
    from backend.web.core import ticket_start
    from types import SimpleNamespace

    class _Cfg:
        def effort_for(self, source_id=""):
            return "xhigh" if source_id == "shortcut" else ""

    monkeypatch.setattr(ticket_start, "_load_config", lambda: _Cfg())

    assert (
        ticket_start.effort_for(SimpleNamespace(provider="shortcut", effort=""))
        == "xhigh"
    )
    assert ticket_start.effort_for(SimpleNamespace(provider="jira", effort="")) == ""
    # The ticket's own stamp wins — the scanner copies it from the source that
    # produced the ticket, and it is the more specific answer.
    assert (
        ticket_start.effort_for(SimpleNamespace(provider="jira", effort="max")) == "max"
    )


def test_effort_for_never_raises_when_the_config_is_unreadable(monkeypatch):
    from backend.web.core import ticket_start
    from types import SimpleNamespace

    def _boom():
        raise RuntimeError("no config")

    monkeypatch.setattr(ticket_start, "_load_config", _boom)
    assert (
        ticket_start.effort_for(SimpleNamespace(provider="shortcut", effort="")) == ""
    )


def test_the_pipeline_launch_translates_the_rung_for_the_cli_it_resolved(monkeypatch):
    """The rungs stored on a source are NEUTRAL. Each provider spells the request
    differently and clamps its own ceiling, so the translation has to happen after
    the program is resolved — not where the rung was configured."""
    from backend.ticket_ingestion import session_runner as sr

    seen: dict = {}

    class _FakeOpts:
        def __init__(self, **kw):
            seen.update(kw)

    class _FakeInst:
        def Start(self, *a):
            pass

    # `from backend import session as cs_session` reads the ATTRIBUTE off the
    # package, so patching sys.modules would not intercept it.
    from backend import session as real

    monkeypatch.setattr(real, "InstanceOptions", _FakeOpts)
    monkeypatch.setattr(real, "NewInstance", lambda opts: _FakeInst())
    monkeypatch.setattr(sr, "_resolve_program", lambda agent: "claude")

    runner = sr.SessionRunner.__new__(sr.SessionRunner)
    runner._mode = "worktree"
    runner._persist = lambda inst: None
    runner._create_instance("t", "b", "do the thing", "", "claude", "ultra")

    # Claude Code spells its top rung `ultracode` and takes it as a launch flag.
    assert "launch_args" in seen and seen["launch_args"]
    assert "ultra" in " ".join(seen["launch_args"]).lower()


def test_no_effort_leaves_the_launch_flags_exactly_as_they_were(monkeypatch):
    """`InstanceOptions` treats an explicit value — even an empty one — as "use
    these verbatim", so passing one unconditionally would strip the flags the
    user set in Settings -> Coding CLI from every ingested ticket."""
    from backend.ticket_ingestion import session_runner as sr

    seen: dict = {}

    class _FakeOpts:
        def __init__(self, **kw):
            seen.update(kw)

    class _FakeInst:
        def Start(self, *a):
            pass

    from backend import session as real

    monkeypatch.setattr(real, "InstanceOptions", _FakeOpts)
    monkeypatch.setattr(real, "NewInstance", lambda opts: _FakeInst())
    monkeypatch.setattr(sr, "_resolve_program", lambda agent: "claude")

    runner = sr.SessionRunner.__new__(sr.SessionRunner)
    runner._mode = "worktree"
    runner._persist = lambda inst: None
    runner._create_instance("t", "b", "do the thing", "", "claude", "")

    assert "launch_args" not in seen
    assert seen["prompt"] == "do the thing"
