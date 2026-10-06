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

import logging
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
@pytest.fixture(autouse=True)
def _no_real_workspaces(tmp_path, monkeypatch):
    """The launch-prompt decoration resolves the provisioning base clone and
    the configured default repo; pin both into tmp so no test ever stats (or
    runs git in) the developer's real workspace dir or configured repo."""
    monkeypatch.setenv("MINDFLOCK_WORKSPACE_DIR", str(tmp_path / "ws-pinned"))
    monkeypatch.setenv("MINDFLOCK_REPO_URL", "git@example.invalid:pinned/none.git")


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
        # How hard to think about this ticket — the source's rung, translated for
        # whichever CLI `passed_agent` resolved to. "" = the CLI's own default.
        passed_effort,
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

    _, _, passed_prompt, _, _, _ = create.call_args.args
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

    _, _, passed_prompt, _, _, _ = create.call_args.args
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


# --------------------------------------------------------------------------- #
# Arming the autopilot for an auto-ingested item                               #
# --------------------------------------------------------------------------- #
def test_arming_marks_the_items_name_as_a_placeholder(config, monkeypatch, tmp_path):
    """The ticket's own name is what the work was ASKED to be — the same sentence
    for every commit the run makes. It is armed as a placeholder so the commit
    step replaces it with a message written from the final diff, keeping the name
    only as the fallback."""
    from backend.web.core import autopilot as ap

    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
    runner = SessionRunner(config)
    monkeypatch.setattr(runner, "_depth_for", lambda lookup: "pr")

    runner._arm_autopilot("sc-42", "tix", "shortcut", "sc-42", "Fix the login loop")

    rec = ap.get("sc-42")
    assert rec["message"] == "Fix the login loop"
    assert rec["message_auto"] is True
    assert rec["source"] == "tix"


def test_arming_without_a_name_does_not_invent_a_placeholder(
    config, monkeypatch, tmp_path
):
    """``message_auto`` says "replace this at commit time". An item with no name
    has no message to replace, so the flag has to be False — otherwise the commit
    step is told a placeholder is waiting and the empty string becomes the
    fallback a failed generation lands on."""
    from backend.web.core import autopilot as ap

    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
    runner = SessionRunner(config)
    monkeypatch.setattr(runner, "_depth_for", lambda lookup: "pr")

    runner._arm_autopilot("sc-43", "tix", "shortcut", "sc-43", "")

    rec = ap.get("sc-43")
    assert rec["message"] == ""
    assert rec["message_auto"] is False


def test_a_failed_arm_is_logged_at_warning_and_does_not_stop_the_run(
    config, monkeypatch, tmp_path, caplog
):
    """This used to fail invisibly — a pipeline predating the feature simply
    never armed anything, and the only symptom was the fast-track toggle sitting
    off with no explanation anywhere. It still must not abort the ingestion: an
    automation preference cannot be allowed to cost the user the ticket."""
    from backend.web.core import autopilot as ap

    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
    runner = SessionRunner(config)
    monkeypatch.setattr(runner, "_depth_for", lambda lookup: "pr")

    def _boom(*a, **k):
        raise OSError("the autopilot store is read-only")

    monkeypatch.setattr(ap, "arm", _boom)

    with caplog.at_level(logging.WARNING):
        runner._arm_autopilot("sc-44", "tix", "shortcut", "sc-44", "Fix it")

    assert ap.get("sc-44") is None
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings and "sc-44" in warnings[-1].getMessage()


# --------------------------------------------------------------------------- #
# start_state: the ticket moves on its board once the session is live          #
# --------------------------------------------------------------------------- #
def test_run_moves_the_ticket_into_its_sources_start_state(config):
    runner = SessionRunner(config)
    story = _make_story()
    fake_inst = MagicMock()
    fake_inst.GetWorktreePath.return_value = None

    with patch.object(runner, "_create_instance", return_value=fake_inst):
        with patch(
            "backend.ticket_ingestion.session_runner.move_started",
            new=AsyncMock(return_value="42"),
        ) as move:
            _run(runner.run(story))

    # After the launch, not before: the state claims a session is working on it.
    move.assert_awaited_once()
    assert move.await_args.args[0] is story


def test_a_failed_move_does_not_fail_the_launch(config):
    # move_started swallows its own failures; this pins that run() does not add
    # a path around it (e.g. awaiting it before the instance exists).
    runner = SessionRunner(config)
    story = _make_story()
    fake_inst = MagicMock()
    fake_inst.GetWorktreePath.return_value = None

    with patch.object(runner, "_create_instance", return_value=fake_inst):
        with patch(
            "backend.ticket_ingestion.session_runner.move_started",
            new=AsyncMock(return_value=""),
        ):
            assert _run(runner.run(story)) == _branch_name_for(story)


def test_the_board_move_happens_after_the_post_launch_work(config):
    """Ordering. ``start_state`` means "a session is working on this", so it may
    only be written once the session exists — and it is written last, after the
    attachments are on their way into the workspace."""
    runner = SessionRunner(config)
    story = _make_story()
    order: list[str] = []

    fake_inst = MagicMock()
    fake_inst.GetWorktreePath.return_value = None

    def _create(*a, **kw):
        order.append("create")
        return fake_inst

    async def _post_start(inst, s):
        order.append("post_start")

    async def _move(s, cfg):
        order.append("move")
        return "42"

    with patch.object(runner, "_create_instance", _create):
        with patch.object(runner, "_post_start", _post_start):
            with patch("backend.ticket_ingestion.session_runner.move_started", _move):
                _run(runner.run(story))

    assert order == ["create", "post_start", "move"]


def test_a_failed_attachment_download_does_not_skip_the_board_move(config):
    """The two post-launch chores are independent bookkeeping about a session
    that is already live, so neither may take the other down with it. Driven
    through the REAL ``_post_start`` — the swallow that makes this true lives
    inside it, and stubbing it out would prove nothing.
    """
    runner = SessionRunner(config)
    story = _make_story()
    story.attachments = [Attachment(name="spec.pdf", url="https://x/spec.pdf")]
    fake_inst = MagicMock()
    fake_inst.GetWorktreePath.return_value = "/tmp/does-not-matter"

    async def _boom(path, s):
        raise RuntimeError("the attachment host is down")

    with patch.object(runner, "_create_instance", return_value=fake_inst):
        with patch.object(runner._prompt_helper, "_download_attachments", _boom):
            with patch(
                "backend.ticket_ingestion.session_runner.move_started",
                new=AsyncMock(return_value="42"),
            ) as move:
                assert _run(runner.run(story)) == _branch_name_for(story)

    move.assert_awaited_once()


def test_a_launch_that_never_produced_a_session_moves_nothing(config):
    """The ticket stays where it is when there is no session to justify moving
    it. A board that says "in progress" for work that failed to start is worse
    than one that says nothing — nobody goes looking for the session that isn't
    there."""
    runner = SessionRunner(config)
    story = _make_story()

    with patch.object(
        runner, "_create_instance", side_effect=RuntimeError("tmux is wedged")
    ):
        with patch(
            "backend.ticket_ingestion.session_runner.move_started",
            new=AsyncMock(return_value="42"),
        ) as move:
            with pytest.raises(RuntimeError, match="tmux is wedged"):
                _run(runner.run(story))

    move.assert_not_awaited()


def test_the_move_is_handed_the_runners_own_config_as_the_fallback(config):
    """The mover prefers the config on DISK and falls back to the snapshot it
    was passed. Passing the runner's own config is what makes that fallback
    mean anything in the pipeline process, which loaded its config at boot and
    may be the only thing still holding a readable copy."""
    runner = SessionRunner(config)
    story = _make_story()
    fake_inst = MagicMock()
    fake_inst.GetWorktreePath.return_value = None

    with patch.object(runner, "_create_instance", return_value=fake_inst):
        with patch(
            "backend.ticket_ingestion.session_runner.move_started",
            new=AsyncMock(return_value=""),
        ) as move:
            _run(runner.run(story))

    assert move.await_args.args == (story, config)


# --------------------------------------------------------------------------- #
# Launch-prompt decoration: red zones + the repo's Plan-first flag             #
# --------------------------------------------------------------------------- #
_REMOTE = "git@github.com:rzorg/rzrepo.git"


def _git(cwd, *args) -> str:
    import subprocess

    cp = subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        capture_output=True,
        text=True,
    )
    assert cp.returncode == 0, cp.stderr
    return cp.stdout.strip()


def _repo_with_flag(path: Path, origin: str = "") -> str:
    """A git repo at ``path`` (origin ``origin`` when given) whose repo id has
    a red zone and Plan-first on. Returns the repo id."""
    import subprocess

    from backend.config import red_zones

    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    (path / "config").mkdir()
    (path / "config" / "settings.toml").write_text("a = 1\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "init")
    if origin:
        _git(path, "remote", "add", "origin", origin)
    rid = red_zones.repo_identity(str(path))[0]
    red_zones.add_zone("repo", rid, "config/")
    red_zones.set_plan_first(rid, True)
    return rid


def _base_clone(tmp_path, monkeypatch, url: str) -> Path:
    from backend.session import provisioned

    monkeypatch.setenv("MINDFLOCK_WORKSPACE_DIR", str(tmp_path / "ws"))
    s = provisioned.load_provision_settings(repo_url_override=url)
    return provisioned.resolve_base_repo_dir(s)


@pytest.mark.parametrize("case", ["remote-url", "empty-url-remote-default"])
def test_pipeline_start_reads_plan_first_off_the_base_clone(
    config, monkeypatch, tmp_path, case
):
    """F24: a remote repo_url (or none, falling back to a remote
    [repository].url) has no local path, but the provisioning base clone
    shares its identity — the same key the server's Intake starts use."""
    from backend.config import red_zones

    _repo_with_flag(_base_clone(tmp_path, monkeypatch, _REMOTE), _REMOTE)
    repo_url = _REMOTE
    if case == "empty-url-remote-default":
        monkeypatch.setenv("MINDFLOCK_REPO_URL", _REMOTE)
        repo_url = ""
    runner = SessionRunner(config)
    _, _, options_seen = _install_fake_cs_modules(monkeypatch)
    with patch.object(runner, "_persist"):
        runner._create_instance("sc-9", "b", "THE PROMPT", repo_url, "claude")
    prompt = options_seen[0].kwargs["prompt"]
    assert prompt.startswith("THE PROMPT")
    assert red_zones.PLAN_PROMPT in prompt
    assert "`config/`" in prompt
    # The provisioning URL itself is untouched.
    assert options_seen[0].kwargs["provision_repo_url"] == repo_url


def test_pipeline_start_reads_a_local_default_repo(config, monkeypatch, tmp_path):
    """…and a local-path [repository].url with no base clone yet."""
    from backend.config import red_zones

    local = tmp_path / "local-repo"
    _repo_with_flag(local)
    monkeypatch.setenv("MINDFLOCK_REPO_URL", str(local))
    runner = SessionRunner(config)
    _, _, options_seen = _install_fake_cs_modules(monkeypatch)
    with patch.object(runner, "_persist"):
        runner._create_instance("sc-10", "b", "P", "", "claude")
    assert red_zones.PLAN_PROMPT in options_seen[0].kwargs["prompt"]


def test_pipeline_start_never_asks_a_planless_cli_to_wait_for_go(
    config, monkeypatch, tmp_path
):
    """F40: the Go button lives in the Map's Plan section, which only
    plan-capable CLIs have — codex gets the zone note, not the wait."""
    from backend.config import red_zones

    local = tmp_path / "local-repo"
    _repo_with_flag(local)
    runner = SessionRunner(config)
    _, _, options_seen = _install_fake_cs_modules(monkeypatch)
    with patch.object(runner, "_persist"):
        runner._create_instance("sc-11", "b", "P", str(local), "codex")
    prompt = options_seen[0].kwargs["prompt"]
    assert red_zones.PLAN_PROMPT not in prompt
    assert "`config/`" in prompt


def test_pipeline_pr_review_is_decorated_off_its_workspace(
    config, monkeypatch, tmp_path
):
    from backend.config import red_zones

    ws = tmp_path / "pr-ws"
    _repo_with_flag(ws)
    runner = SessionRunner(config)
    _, _, options_seen = _install_fake_cs_modules(monkeypatch)
    monkeypatch.setattr(
        "backend.ticket_ingestion.session_runner._resolve_program", lambda a: "claude"
    )
    with patch.object(runner, "_persist"):
        runner._create_pr_instance("pr-9", "feature/x", str(ws), "REVIEW IT")
    prompt = options_seen[0].kwargs["prompt"]
    assert prompt.startswith("REVIEW IT")
    assert red_zones.PLAN_PROMPT in prompt and "`config/`" in prompt


# --------------------------------------------------------------------------- #
# Depth is configured per SOURCE, not per provider                             #
# --------------------------------------------------------------------------- #
def _two_jira_sources(monkeypatch, tmp_path):
    from backend.config import settings as settings_store

    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    settings_store.invalidate()
    settings_store.update_settings(
        ticketing={
            "sources": [
                {"id": "jira-web", "provider": "jira", "depth": "commit"},
                {"id": "jira-pay", "provider": "jira", "depth": "pr"},
            ]
        }
    )


def test_depth_is_looked_up_by_the_tickets_source_key(config, monkeypatch, tmp_path):
    """Two sources of one provider: the ticket's own source decides its rung.
    Looking it up by ``story.provider`` matched whichever jira source came
    first, so the payments project ran at the web project's rung."""
    from backend.web.core import autopilot as ap

    _two_jira_sources(monkeypatch, tmp_path)
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
    runner = SessionRunner(config)
    story = make_ticket(id=7, provider="jira")
    story.source_key = "jira-pay"
    seen = {}

    def _arm(title, kind, lookup, item, message):
        seen["lookup"] = lookup

    monkeypatch.setattr(runner, "_arm_autopilot", _arm)
    monkeypatch.setattr(
        runner, "_create_instance", lambda *a, **k: MagicMock(GetWorktreePath=str)
    )
    monkeypatch.setattr(runner, "_post_start", AsyncMock())
    with patch("backend.ticket_ingestion.session_runner.move_started", AsyncMock()):
        import asyncio as _asyncio

        _asyncio.run(runner.run(story))
    assert seen["lookup"] == "jira-pay"
    assert runner._depth_for("jira-pay") == "pr"
    assert runner._depth_for("jira-web") == "commit"


def test_a_source_without_its_own_depth_never_falls_back_to_the_global_default(
    config, monkeypatch, tmp_path
):
    """Ticket ingestion is per source. A source with no depth of its own (and
    an unknown source) is Off — never the Settings fast-track default, even
    when that is explicitly "pr": an unattended pipeline is only ever armed by
    the source it came from."""
    from backend.config import settings as settings_store
    from backend.web import server

    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    settings_store.invalidate()
    settings_store.update_settings(
        repository={"fasttrack_depth": "pr"},
        ticketing={"sources": [{"id": "jira-pay", "provider": "jira"}]},
    )
    runner = SessionRunner(config)
    assert runner._depth_for("jira-pay") == ""
    assert runner._depth_for("nowhere") == ""
    assert server._source_intake_depth("jira-pay") == ""
    assert server._fasttrack_default() == "pr"  # the global one is untouched


def test_a_provider_name_still_resolves_an_unkeyed_lookup(
    config, monkeypatch, tmp_path
):
    _two_jira_sources(monkeypatch, tmp_path)
    runner = SessionRunner(config)
    # The provider is only a fallback, so it answers with the first such source.
    assert runner._depth_for("jira") == "commit"


def test_an_exact_key_beats_an_earlier_provider_match(config, monkeypatch, tmp_path):
    """A source keyed by the bare provider name ("jira") must not be shadowed
    by an earlier source whose PROVIDER is jira."""
    from backend.config import settings as settings_store

    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    settings_store.invalidate()
    settings_store.update_settings(
        ticketing={
            "sources": [
                {"id": "jira-web", "provider": "jira", "depth": "commit"},
                {"id": "jira", "provider": "jira", "depth": "push"},
            ]
        }
    )
    runner = SessionRunner(config)
    assert runner._depth_for("jira") == "push"
    from backend.web import server

    assert server._source_intake_depth("jira") == "push"
    assert server._source_intake_depth("jira-web") == "commit"
