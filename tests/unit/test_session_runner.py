"""Hermetic tests for ``SessionRunner`` (ticket_ingestion.session_runner).

CRITICAL SAFETY GOAL: prove the runner cannot spawn a real MindFlock/tmux/
claude session in tests. Every path that would reach ``cs_session.NewInstance``
/ ``inst.Start`` is mocked, and we assert those mocks are used correctly:

  * ``run`` / ``run_pr`` derive the session title as ``sc-<id>`` / ``pr-<n>``.
  * the branch name is derived via ``_branch_name_for`` for stories and taken
    verbatim from ``pr.head_ref`` for PRs.
  * the prompt built by the ingestion/PR helpers is passed through unchanged to
    the instance-creation seam.
  * ``_create_instance`` / ``_create_pr_instance`` build the correct
    ``InstanceOptions`` and call ``inst.Start`` exactly once, with persistence
    stubbed — a leaked "sc-<id>" tmux session is the exact bug we eliminate.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from backend.ticket_ingestion.config import (
    EngineConfig,
    PipelineConfig,
    TicketProviderConfig,
)
from backend.ticket_ingestion.models import (
    Attachment,
    PRComment,
    ProvisionedPRWorkspace,
    PullRequest,
    Ticket,
)
from backend.ticket_ingestion.provisioner import _branch_name_for
from backend.ticket_ingestion.session_runner import SessionRunner
from tests._factories import make_ticket


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #
@pytest.fixture
def config(tmp_path) -> PipelineConfig:
    return PipelineConfig(
        ticketing=TicketProviderConfig(
            provider="shortcut",
            api_token="sc_test_token",
            member_id="member-uuid-123",
        ),
        repo_url="git@github.com:org/repo.git",
        workspace_dir=tmp_path / "workspaces",
        min_description_length=20,
        log_file=tmp_path / "pipeline.log",
        log_level="INFO",
        engine=EngineConfig(enabled=True, mode="worktree"),
    )


def _make_story(story_id: int = 42, name: str = "Fix the Flux Capacitor") -> Ticket:
    return make_ticket(
        id=story_id,
        name=name,
        description="A" * 50,
        acceptance_criteria=["works"],
        owner_ids=["u1"],
        app_url="https://app.shortcut.com/org/story/42",
        created_at=datetime(2026, 1, 1),
    )


def _make_pr(number: int = 7, head_ref: str = "feature/pr-branch") -> PullRequest:
    return PullRequest(
        number=number,
        head_ref=head_ref,
        head_sha="deadbeefcafebabe",
        base_ref="staging",
        title="My PR title",
        url="https://github.com/org/repo/pull/7",
        author="octocat",
        created_at=datetime(2026, 1, 2),
        repo="org/repo",
    )


def _make_comment(cid: int = 1) -> PRComment:
    return PRComment(
        id=cid,
        kind="review",
        author="reviewer",
        body="please fix this",
        url="https://github.com/org/repo/pull/7#c1",
        path="src/foo.py",
        line=10,
    )


def _run(coro):
    import asyncio

    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Construction is side-effect free                                            #
# --------------------------------------------------------------------------- #
def test_init_reads_engine_mode(config):
    runner = SessionRunner(config)
    assert runner._mode == "worktree"


def test_init_defaults_mode_when_no_engine(config):
    config.engine = None
    runner = SessionRunner(config)
    assert runner._mode == "worktree"


def test_init_honors_clone_mode(config):
    config.engine = EngineConfig(enabled=True, mode="clone")
    runner = SessionRunner(config)
    assert runner._mode == "clone"


# --------------------------------------------------------------------------- #
# run_story: title / branch / prompt passthrough (internals mocked)           #
# --------------------------------------------------------------------------- #
def test_run_story_title_branch_and_prompt_passthrough(config):
    runner = SessionRunner(config)
    story = _make_story()

    fake_inst = MagicMock()
    # _post_start walks GetWorktreePath; return None so it is a no-op (no attachments anyway).
    fake_inst.GetWorktreePath.return_value = None

    with patch.object(runner, "_create_instance", return_value=fake_inst) as create:
        branch = _run(runner.run(story))

    # Branch is derived via _branch_name_for and returned.
    expected_branch = _branch_name_for(story)
    assert branch == expected_branch

    create.assert_called_once()
    (
        passed_title,
        passed_branch,
        passed_prompt,
        passed_repo,
        passed_agent,
    ) = create.call_args.args
    assert passed_title == "sc-42"
    assert passed_branch == expected_branch

    # The prompt handed to the instance is exactly what the ingestion helper built.
    expected_prompt = runner._prompt_helper._build_prompt(story, None, None)
    assert passed_prompt == expected_prompt


def test_run_story_passes_supplemental_context_into_prompt(config):
    runner = SessionRunner(config)
    story = _make_story()
    supplemental = "EXTRA_CONTEXT_MARKER_xyz"

    with patch.object(runner, "_create_instance", return_value=MagicMock()) as create:
        _run(runner.run(story, supplemental_context=supplemental))

    _, _, passed_prompt, _, _ = create.call_args.args
    assert supplemental in passed_prompt
    # And it matches the helper output for the same supplemental context.
    assert passed_prompt == runner._prompt_helper._build_prompt(
        story, supplemental, None
    )


def test_run_story_appends_attachment_notice_to_prompt(config):
    runner = SessionRunner(config)
    story = _make_story()
    story.attachments = [
        Attachment(name="diagram.png", url="https://x/diagram.png"),
        Attachment(name="spec.pdf", url="https://x/spec.pdf"),
    ]

    fake_inst = MagicMock()
    fake_inst.GetWorktreePath.return_value = None  # skip download path

    with patch.object(runner, "_create_instance", return_value=fake_inst) as create:
        _run(runner.run(story))

    _, _, passed_prompt, _, _ = create.call_args.args
    base_prompt = runner._prompt_helper._build_prompt(story, None, None)
    # Attachment notice is appended AFTER the base prompt.
    assert passed_prompt.startswith(base_prompt)
    assert "## Attached Files" in passed_prompt
    assert "2 attachment(s)" in passed_prompt
    assert "diagram.png" in passed_prompt and "spec.pdf" in passed_prompt
    assert ".ticket_attachments/" in passed_prompt


def test_run_story_offloads_creation_to_thread(config):
    """_create_instance is dispatched via asyncio.to_thread (never inline).

    We stub to_thread so the real _create_instance body (which would import
    the cs engine and start tmux) is NEVER executed — proving the runner
    hands creation off to a worker thread rather than doing it inline.
    """
    runner = SessionRunner(config)
    story = _make_story()

    dispatched: dict = {}

    async def fake_to_thread(fn, *args, **kwargs):
        dispatched["fn"] = fn
        dispatched["args"] = args
        return MagicMock(GetWorktreePath=MagicMock(return_value=None))

    # NOTE: we do NOT patch _create_instance — because to_thread is stubbed,
    # its real body is never executed, so no cs engine import / tmux launch
    # can happen. We assert the exact bound method was the dispatched callable.
    with patch(
        "backend.ticket_ingestion.session_runner.asyncio.to_thread",
        side_effect=fake_to_thread,
    ):
        _run(runner.run(story))

    # The callable dispatched to the worker thread is _create_instance,
    # invoked with (title, branch, prompt).
    assert dispatched["fn"].__func__ is SessionRunner._create_instance
    assert dispatched["fn"].__self__ is runner
    assert dispatched["args"][0] == "sc-42"
    assert dispatched["args"][1] == _branch_name_for(story)


# --------------------------------------------------------------------------- #
# run_pr: title / branch / prompt passthrough (internals mocked)              #
# --------------------------------------------------------------------------- #
def test_run_pr_title_branch_and_prompt_passthrough(config):
    runner = SessionRunner(config)
    pr = _make_pr()
    comments = [_make_comment(1), _make_comment(2)]
    workspace = ProvisionedPRWorkspace(
        directory=Path("/abs/workspaces/pr-7"),
        head_ref=pr.head_ref,
        head_sha=pr.head_sha,
    )

    async def fake_provision(pr_arg, launch_cursor=True):
        # Provisioning is mocked: no real git/clone happens.
        assert launch_cursor is False
        return workspace

    with (
        patch.object(runner._pr_provisioner, "provision", side_effect=fake_provision),
        patch.object(runner, "_create_pr_instance") as create,
    ):
        head = _run(runner.run_pr(pr, comments))

    # run_pr returns the PR head ref verbatim.
    assert head == pr.head_ref

    create.assert_called_once()
    title, head_ref, directory, prompt, repo = create.call_args.args
    assert title == "pr-repo-7"
    assert head_ref == pr.head_ref
    assert directory == str(workspace.directory)
    # The repo rides along so the launch can resolve that repo's own Agent CLI
    # card (github.repo_settings) before falling back to the screen-wide one.
    assert repo == pr.repo

    from backend.ticket_ingestion.pr_runner import build_consolidated_pr_prompt

    expected_prompt = build_consolidated_pr_prompt(pr, comments, workspace.directory)
    assert prompt == expected_prompt
    assert "PR #7" in prompt


def test_run_pr_provision_launch_cursor_is_false(config):
    runner = SessionRunner(config)
    pr = _make_pr(number=99, head_ref="hotfix/thing")
    workspace = ProvisionedPRWorkspace(
        directory=Path("/abs/pr-99"), head_ref=pr.head_ref, head_sha=pr.head_sha
    )

    async def fake_provision(pr_arg, launch_cursor=True):
        fake_provision.launch_cursor = launch_cursor
        return workspace

    with (
        patch.object(runner._pr_provisioner, "provision", side_effect=fake_provision),
        patch.object(runner, "_create_pr_instance"),
    ):
        head = _run(runner.run_pr(pr, []))

    assert head == "hotfix/thing"
    # launch_cursor MUST be False (the CS web terminal is the surface, not Cursor).
    assert fake_provision.launch_cursor is False


# --------------------------------------------------------------------------- #
# _create_instance / _create_pr_instance: NO real session ever starts         #
# --------------------------------------------------------------------------- #
def _install_fake_cs_modules(monkeypatch):
    """Install fake ``backend.config`` / ``backend.session`` shims.

    Returns (fake_session_module, created_instances_list, options_seen_list).
    ``NewInstance`` records the options and hands back a MagicMock whose
    ``.Start`` is a mock — so nothing real (tmux/claude/git) is ever launched.
    """
    options_seen: list = []
    created: list = []

    class FakeInstanceOptions:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            self.kwargs = kwargs

    def fake_new_instance(opts):
        options_seen.append(opts)
        inst = MagicMock(name="FakeInstance")
        inst.Title = opts.kwargs["title"]
        created.append(inst)
        return inst

    class FakeLoaded:
        def GetProgram(self):
            return "claude"

    # Patch ATTRIBUTES on the real modules — NOT a sys.modules swap. ``_create_instance``
    # does ``from backend import session/config`` inside the function, which resolves
    # the submodule attribute already bound on the ``backend`` package once it has been
    # imported anywhere. A ``sys.modules`` setitem is bypassed in that case, so it only
    # "works" when the module hasn't been imported yet (true in isolation, false in the
    # full suite — the source of the order-dependent failures). setattr is deterministic.
    monkeypatch.setattr("backend.session.InstanceOptions", FakeInstanceOptions)
    monkeypatch.setattr("backend.session.NewInstance", fake_new_instance)
    monkeypatch.setattr("backend.config.LoadConfig", lambda: FakeLoaded())

    import backend.session as fake_session  # real module, now attribute-patched

    return fake_session, created, options_seen


def test_create_instance_builds_options_and_starts_without_spawning(
    config, monkeypatch
):
    runner = SessionRunner(config)
    _install_fake_cs_modules(monkeypatch)

    # _persist is stubbed: it must never touch the real ~/.mindflock state.
    with patch.object(runner, "_persist") as persist:
        inst = runner._create_instance("sc-42", "feature/sc-42/x", "THE PROMPT")

    # Options passed to NewInstance carry title/branch/prompt/provisioned flags.
    assert inst.Title == "sc-42"
    # Start called exactly once with first_time_setup=True.
    inst.Start.assert_called_once_with(True)
    persist.assert_called_once_with(inst)


def test_create_instance_option_fields(config, monkeypatch):
    runner = SessionRunner(config)
    _, _, options_seen = _install_fake_cs_modules(monkeypatch)

    with patch.object(runner, "_persist"):
        runner._create_instance("sc-42", "feature/sc-42/x", "THE PROMPT")

    assert len(options_seen) == 1
    opts = options_seen[0].kwargs
    assert opts["title"] == "sc-42"
    assert opts["new_branch"] == "feature/sc-42/x"
    assert opts["prompt"] == "THE PROMPT"
    assert opts["provisioned"] is True
    assert opts["workspace_strategy"] == "worktree"
    assert opts["program"] == "claude"
    assert opts["path"] == "."


def test_create_instance_uses_configured_clone_mode(config, monkeypatch):
    config.engine = EngineConfig(enabled=True, mode="clone")
    runner = SessionRunner(config)
    _, _, options_seen = _install_fake_cs_modules(monkeypatch)

    with patch.object(runner, "_persist"):
        runner._create_instance("sc-1", "b", "p")

    assert options_seen[0].kwargs["workspace_strategy"] == "clone"


def test_create_instance_falls_back_to_claude_when_config_raises(config, monkeypatch):
    runner = SessionRunner(config)
    fake_session, _, options_seen = _install_fake_cs_modules(monkeypatch)

    fake_config = sys.modules["backend.config"]

    def boom():
        raise RuntimeError("no config")

    monkeypatch.setattr(fake_config, "LoadConfig", boom)

    with patch.object(runner, "_persist"):
        runner._create_instance("sc-1", "b", "p")

    assert options_seen[0].kwargs["program"] == "claude"


def test_create_pr_instance_builds_clone_options_and_starts(config, monkeypatch):
    runner = SessionRunner(config)
    _, created, options_seen = _install_fake_cs_modules(monkeypatch)

    with patch.object(runner, "_persist") as persist:
        inst = runner._create_pr_instance(
            "pr-7", "feature/pr-branch", "/abs/pr-7", "PR PROMPT"
        )

    opts = options_seen[0].kwargs
    assert opts["title"] == "pr-7"
    assert opts["new_branch"] == "feature/pr-branch"
    assert opts["prompt"] == "PR PROMPT"
    # PR workspaces are adopted verbatim: clone strategy + explicit workspace path.
    assert opts["provisioned"] is True
    assert opts["workspace_strategy"] == "clone"
    assert opts["workspace_path"] == "/abs/pr-7"
    inst.Start.assert_called_once_with(True)
    persist.assert_called_once_with(inst)


def test_create_instance_never_imports_real_tmux(config, monkeypatch):
    """Guard: creating an instance must not spawn a real subprocess.

    We poison the low-level subprocess spawns so that ANY attempt to launch a
    real process (tmux/claude/git) during _create_instance raises loudly.
    """
    runner = SessionRunner(config)
    _install_fake_cs_modules(monkeypatch)

    import subprocess

    def _blow_up(*a, **k):  # pragma: no cover - only fires on a real spawn
        raise AssertionError("a real subprocess was spawned in a test!")

    monkeypatch.setattr(subprocess, "Popen", _blow_up)
    monkeypatch.setattr(subprocess, "run", _blow_up)
    monkeypatch.setattr(subprocess, "call", _blow_up)

    with patch.object(runner, "_persist"):
        inst = runner._create_instance("sc-42", "b", "p")

    inst.Start.assert_called_once_with(True)


# --------------------------------------------------------------------------- #
# End-to-end run() with the cs shims: still no real spawn                      #
# --------------------------------------------------------------------------- #
def test_run_story_end_to_end_through_shims(config, monkeypatch):
    """Full run() path with fake cs modules — proves the whole story flow is
    inert: title/branch derived correctly and Start is a mock."""
    runner = SessionRunner(config)
    _, created, options_seen = _install_fake_cs_modules(monkeypatch)
    story = _make_story(story_id=101, name="End To End")

    with patch.object(runner, "_persist"):
        branch = _run(runner.run(story))

    assert branch == _branch_name_for(story)
    assert len(created) == 1
    inst = created[0]
    assert inst.Title == "sc-101"
    inst.Start.assert_called_once_with(True)
    assert options_seen[0].kwargs["new_branch"] == branch
