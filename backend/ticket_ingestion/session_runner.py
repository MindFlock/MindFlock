"""Run a story session through the MindFlock engine instead of spawning a
local tmux + Windows Terminal tab.

This is the in-process bridge for "use MindFlock to manage MindFlock's
terminals": when ``[mindflock].enabled`` is set, the orchestrator hands each
story to :class:`SessionRunner`, which creates a MindFlock ``Instance``
in *provisioned mode*. MindFlock then owns the whole session — it provisions
the workspace (worktree off a canonical clone, or a full clone, per
``[mindflock].mode``), seeds the agent with the ticket prompt, and runs
``claude`` inside a ``mindflock_<title>`` tmux session that the MindFlock
web UI / TUI can attach to.

Because the workspace provisioning lives inside MindFlock's
``Instance.Start`` (see ``backend.session.provisioned``), MindFlock no longer
runs its own ``EnvironmentProvisioner`` for these stories — there is a single
provisioning path, owned by MindFlock.

No server required: this bridge is in-process only — it imports
``backend.session`` and calls ``Instance.Start`` directly, so there is no HTTP
call, no host/port, and nothing to be "unreachable". Created instances are
persisted to ``~/.mindflock/state.json`` (best-effort) and a running web server
adopts them into its grid within ~4s via ``backend.web.core.engine.
_sync_external_instances``; a server started later picks them up on boot. With
no UI running at all the session still exists as a worktree + branch + tmux
session, so a headless/CI pipeline behaves identically minus the GUI.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

from backend.ticket_ingestion.claude_runner import ClaudeCodeRunner
from backend.ticket_ingestion.config import PipelineConfig
from backend.ticket_ingestion.models import (
    PRComment,
    PullRequest,
    Ticket,
    pr_slug,
)
from backend.ticket_ingestion.pr_provisioner import PRProvisioner
from backend.ticket_ingestion.pr_runner import build_consolidated_pr_prompt
from backend.ticket_ingestion.provisioner import _branch_name_for

logger = logging.getLogger(__name__)

# Wall-clock cap on Instance.Start (clone/worktree + provision + tmux). The
# worker thread itself can't be killed, but the await must not wedge the main
# processing loop forever — on timeout the story/PR is marked failed and the
# loop moves on.
_INSTANCE_START_TIMEOUT = 900.0


def _ensure_engine_on_path() -> None:
    """Put the repo root (containing the ``backend`` package) on ``sys.path``."""
    src_root = (
        Path(__file__).resolve().parents[2]
    )  # backend/ticket_ingestion/… -> repo root
    p = str(src_root)
    if src_root.is_dir() and p not in sys.path:
        sys.path.insert(0, p)


def engine_bridge_error() -> str | None:
    """Why the in-process engine bridge is unusable here, or ``None`` if it works.

    Engine mode is the shipped default, so the orchestrator needs a cheap way to
    check it up front instead of discovering the problem one failed ticket at a
    time (a launch failure marks the story terminally ``failed``, which needs a
    manual ledger edit to retry). The only thing that can genuinely be missing is
    the engine half of the package: ``backend.ticket_ingestion`` can be installed
    and importable in an environment where ``backend.session`` /
    ``backend.config`` are not (a partial install, or a venv synced with only the
    ingestion dependency group). Importing is the whole check — there is no
    server to reach.
    """
    _ensure_engine_on_path()
    try:
        from backend import config as _cs_config  # noqa: F401
        from backend import session as _cs_session  # noqa: F401
    except Exception as e:  # noqa: BLE001 — any import-time failure disqualifies it
        return f"{type(e).__name__}: {e}"
    return None


def _resolve_program(agent: str = "") -> str:
    """The agent program a MindFlock instance should run.

    ``agent`` is the ingestion pipeline's choice for this session (the ticket's
    source ``agent``, else ``[mindflock].agent``); empty falls through to the
    user's configured default — Settings → Coding provider first, then the
    engine config, then ``claude``. Single source for both engine-launch paths
    so they can't diverge.
    """
    if agent:
        return agent
    from backend.config.program import resolve_default_program

    return resolve_default_program()


class SessionRunner:
    """Launches a story as an engine provisioned ``Instance`` (in-process)."""

    def __init__(self, config: PipelineConfig) -> None:
        self.config = config
        # Reuse the ingestion prompt + attachment-download logic verbatim.
        self._prompt_helper = ClaudeCodeRunner(config)
        self._pr_provisioner = PRProvisioner(config)
        self._mode = config.engine.mode if config.engine else "worktree"
        _ensure_engine_on_path()

    async def run(
        self,
        story: Ticket,
        supplemental_context: str | None = None,
    ) -> str:
        """Provision + launch ``story`` via MindFlock; return the branch name."""
        prompt = self._prompt_helper._build_prompt(story, supplemental_context, None)
        if story.attachments:
            names = ", ".join(
                a.name for a in story.attachments if getattr(a, "name", None)
            )
            # The workspace is created by MindFlock during launch, so
            # attachments are downloaded just AFTER the session starts — do not
            # claim they are already present; tell the agent they will appear.
            prompt += (
                "\n\n## Attached Files\n\n"
                f"This ticket has {len(story.attachments)} attachment(s)"
                + (f" ({names})" if names else "")
                + ". They are being downloaded into `.ticket_attachments/` in "
                "this workspace and should appear within a few seconds — check "
                "that directory (re-list if it is empty at first) and read them "
                "as part of the ticket context.\n"
            )

        branch = _branch_name_for(story)
        title = story.slug
        agent = self._agent_for(story)
        self._arm_autopilot(
            title,
            getattr(story, "provider", "") or "",
            str(getattr(story, "id", "") or story.slug),
            str(getattr(story, "name", "") or ""),
        )
        logger.info(
            "Launching ticket %s via MindFlock (agent=%s, mode=%s, branch=%s, session=%s)",
            story.slug,
            agent or "engine default",
            self._mode,
            branch,
            title,
        )

        try:
            inst = await asyncio.wait_for(
                asyncio.to_thread(
                    self._create_instance,
                    title,
                    branch,
                    prompt,
                    getattr(story, "repo_url", ""),
                    agent,
                ),
                timeout=_INSTANCE_START_TIMEOUT,
            )
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"MindFlock instance start for ticket {story.slug} timed out "
                f"after {_INSTANCE_START_TIMEOUT:.0f}s (the worker thread may "
                "still be running; the story is marked failed)"
            ) from None

        # Best-effort post-launch: drop attachments into the live workspace.
        await self._post_start(inst, story)
        logger.info(
            "Ticket %s is live in MindFlock session 'mindflock_%s'. "
            "Attach via the MindFlock UI or: tmux attach -t mindflock_%s",
            story.slug,
            title,
            title,
        )
        return branch

    async def run_pr(self, pr: PullRequest, comments: list[PRComment]) -> str:
        """Address all PR comments in ONE MindFlock session; return head ref.

        Reuses PRProvisioner (correct fork / pull-head fetch) to materialize the
        PR's branch in a workspace, then adopts that workspace as a single CS
        session driven by one consolidated prompt — instead of a tmux tab per
        comment. Cursor launch is skipped; the CS web terminal is the surface.
        """
        workspace = await self._pr_provisioner.provision(pr, launch_cursor=False)
        prompt = build_consolidated_pr_prompt(pr, comments, workspace.directory)
        title = f"pr-{pr_slug(pr)}"
        self._arm_autopilot(
            title,
            str(getattr(pr, "repo", "") or ""),
            "%s#%s" % (getattr(pr, "repo", ""), pr.number),
            str(getattr(pr, "title", "") or ""),
        )
        logger.info(
            "Launching PR #%d via MindFlock (%d comments, session=%s, branch=%s)",
            pr.number,
            len(comments),
            title,
            pr.head_ref,
        )
        try:
            await asyncio.wait_for(
                asyncio.to_thread(
                    self._create_pr_instance,
                    title,
                    pr.head_ref,
                    str(workspace.directory),
                    prompt,
                    pr.repo,
                ),
                timeout=_INSTANCE_START_TIMEOUT,
            )
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"MindFlock instance start for PR #{pr.number} timed out "
                f"after {_INSTANCE_START_TIMEOUT:.0f}s (the worker thread may "
                "still be running)"
            ) from None
        logger.info(
            "PR #%d is live in MindFlock session 'mindflock_%s'.",
            pr.number,
            title,
        )
        return pr.head_ref

    # --- internals (run off the event loop) --------------------------------
    def _agent_for(self, story) -> str:
        """The agent CLI this story's session runs (``""`` = engine default).

        Precedence: the ticket's own stamp (the orchestrator copies it from the
        source that produced the ticket) → the config **as it is on disk right
        now** → the config this runner was constructed with.

        The re-read is what makes a provider switched in the UI apply to the
        next ticket instead of the next pipeline restart, since ``__main__``
        loads the config once and hands that snapshot over for the life of the
        process. The injected config stays the fallback rather than being
        discarded: it is a constructor argument and tests (and any caller that
        builds a config by hand) are entitled to have it honoured when the
        on-disk config expresses no opinion for this story's source.
        """
        from backend.ticket_ingestion.config import fresh_agent

        stamped = getattr(story, "agent", "")
        if stamped:
            return stamped
        provider = getattr(story, "provider", "")
        return fresh_agent(lambda c: c.agent_for(provider), self.config)

    def _arm_autopilot(self, title: str, source: str, item: str, message: str) -> None:
        """Record how far an AUTO-ingested item should carry itself.

        The auto path runs in a separate OS process from the web server, so this
        cannot call the server's helper — but it does not need to. The autopilot
        store is a small, lock-guarded, atomically-written file keyed by SESSION
        TITLE, and the title an ingested item produces here is byte-identical to
        the one a forced start from the Intake dialog produces. So the child
        writes the record and the server's driver picks it up on its next pass,
        with no IPC and no shared memory.

        Armed BEFORE the instance is created, deliberately: the target then
        survives a provisioning crash or a server restart mid-launch. A record
        whose session never appears is dropped by the driver's prune.

        Depth is read from settings on every call (not cached at construction),
        the same reason :meth:`_agent_for` re-reads: a change made in the UI must
        apply to the NEXT item, not the next pipeline restart. Never raises — an
        automation preference must not be able to stop an ingestion.
        """
        try:
            from backend.web.core import autopilot as _autopilot

            depth = _autopilot.normalize_depth(self._depth_for(source))
            if depth in ("", "off") or depth not in _autopilot.SOURCE_DEPTHS:
                return
            _autopilot.arm(
                title, depth, source=source, item=item, message=message or ""
            )
        except Exception:  # noqa: BLE001
            logger.debug("autopilot arm skipped for %s", title, exc_info=True)

    def _depth_for(self, source: str) -> str:
        """The configured autopilot depth for a ticketing source / repo slug.

        Reads the same ``settings.json`` the web server reads, so the per-source
        default has exactly one definition regardless of which process acts on it.
        """
        try:
            from backend.config.settings import load_settings

            st = load_settings()
            for src in st.ticketing.sources:
                if source and source in (
                    getattr(src, "id", ""),
                    getattr(src, "provider", ""),
                ):
                    return str(getattr(src, "depth", "") or "")
            for table in (st.github.repo_settings, st.github.issue_repo_settings):
                block = (table or {}).get(source) or {}
                if block.get("depth"):
                    return str(block["depth"])
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _create_instance(
        self,
        title: str,
        branch: str,
        prompt: str,
        repo_url: str = "",
        agent: str = "",
    ):
        from backend import session as cs_session

        program = _resolve_program(agent)

        opts = cs_session.InstanceOptions(
            title=title,
            path=".",
            program=program,
            provisioned=True,
            workspace_strategy=self._mode,
            new_branch=branch,
            prompt=prompt,
            # Multi-repo ingestion: provision from the ticket's own source repo
            # when set, else the engine's configured [repository].url.
            provision_repo_url=repo_url,
        )
        inst = cs_session.NewInstance(opts)
        inst.Start(True)  # clone/worktree + provision + tmux + launch claude
        self._persist(inst)
        return inst

    def _create_pr_instance(
        self, title: str, head_ref: str, directory: str, prompt: str, repo: str = ""
    ):
        from backend import session as cs_session

        # PR review has no ticketing source, so it runs the repo's own card
        # first, then review's screen-wide agent ([github].agent), before
        # falling back to the ingestion-wide one. Re-read at launch, and with
        # an empty on-disk answer honoured as an answer: the pipeline's startup
        # snapshot would otherwise pin whatever provider was configured when the
        # process began, so clearing the field would silently do nothing.
        from backend.ticket_ingestion.config import agent_now

        program = _resolve_program(
            agent_now(
                lambda c: c.pr_agent(repo),
                self.config.pr_agent(repo) if self.config else "",
            )
        )

        # Adopt the already-provisioned PR workspace: clone-style worktree at the
        # exact directory, on the PR's existing head branch (verbatim, no fork).
        opts = cs_session.InstanceOptions(
            title=title,
            path=".",
            program=program,
            provisioned=True,
            workspace_strategy="clone",
            new_branch=head_ref,
            prompt=prompt,
            workspace_path=directory,
        )
        inst = cs_session.NewInstance(opts)
        inst.Start(True)
        self._persist(inst)
        return inst

    def _persist(self, inst) -> None:
        """Append the instance to MindFlock's state.json (best-effort).

        Avoids ``Storage.LoadInstances`` on purpose: that reconstructs every
        stored instance via ``FromInstanceData``, which would try to restore /
        attach *other* sessions' tmux inside this process. Instead we parse the
        raw JSON into pure ``InstanceData`` (no side effects), replace any entry
        with the same title, and re-marshal.
        """
        try:
            from backend import config as cs_config
            from backend.session.storage import InstanceData, _marshal_instances

            state = cs_config.LoadState()
            raw = state.GetInstances()
            if isinstance(raw, (bytes, bytearray)):
                raw = bytes(raw).decode("utf-8")
            existing = json.loads(raw) if raw else []
            datas = [
                InstanceData.from_dict(x)
                for x in existing
                if x.get("title") != inst.Title
            ]
            datas.append(inst.ToInstanceData())
            state.SaveInstances(_marshal_instances(datas))
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Could not persist MindFlock instance to state.json "
                "(session still running): %s",
                e,
            )

    async def _post_start(self, inst, story: Ticket) -> None:
        if not story.attachments:
            return
        try:
            wp = inst.GetWorktreePath()
            if not wp:
                return
            await self._prompt_helper._download_attachments(Path(wp), story)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Attachment download for ticket %s failed (continuing): %s",
                story.slug,
                e,
            )
