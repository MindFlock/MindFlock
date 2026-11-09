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
import os
import re
import sys
import time
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
from backend.ticket_ingestion.start_state import move_started, source_key_of
from backend.ticket_ingestion.tracker_claims import mirror_started

logger = logging.getLogger(__name__)

# Wall-clock cap on Instance.Start (clone/worktree + provision + tmux). The
# worker thread itself can't be killed, but the await must not wedge the main
# processing loop forever — on timeout the story/PR is marked failed and the
# loop moves on.
_INSTANCE_START_TIMEOUT = 900.0

# While the machine's memory reads low (backend.memory_pressure), a launch
# waits here instead of adding another agent to a box that is already
# swapping: re-read every _MEMORY_POLL_SECONDS, one log line every
# _MEMORY_LOG_SECONDS so a held pipeline says why it is quiet. The hold is
# capped at _MEMORY_MAX_WAIT_SECONDS: a misreading must never stall ticket
# intake for good, so past it the launch goes ahead with a warning.
_MEMORY_POLL_SECONDS = 15.0
_MEMORY_LOG_SECONDS = 300.0
_MEMORY_MAX_WAIT_SECONDS = 1800.0


def _held_reason() -> str:
    """Why an automatic launch must wait now ("" = go): memory reads low, or
    the server's launch gate (backend.web.core.launch_gate) says wait. The
    gate counts every MindFlock agent started in the last 90 s as still
    growing (tmux creation times, so the server's launches count too) and
    the machine-wide live-agent cap. Cached reads — cheap to call inline.
    Never raises."""
    from backend import memory_pressure

    if memory_pressure.is_low():
        return memory_pressure.describe() or "memory is low"
    try:
        from backend.web.core import launch_gate

        verdict = launch_gate.decide("auto")
    except Exception:  # noqa: BLE001 — the gate is advisory here
        return ""
    return "" if verdict.get("decision") == "go" else str(verdict.get("reason") or "")


async def _wait_for_memory(what: str) -> None:
    """Return once memory headroom is back (at once when it never left, or
    the probe has nothing to read), or after _MEMORY_MAX_WAIT_SECONDS."""
    start = time.monotonic()
    logged = 0.0
    while True:
        why = _held_reason()
        if not why:
            return
        now = time.monotonic()
        if now - start >= _MEMORY_MAX_WAIT_SECONDS:
            logger.warning(
                "Starting %s anyway after %d minutes held for memory: %s",
                what,
                int(_MEMORY_MAX_WAIT_SECONDS // 60),
                why,
            )
            return
        if now - logged >= _MEMORY_LOG_SECONDS:
            logger.warning("Holding %s: %s — it starts once that clears", what, why)
            logged = now
        await asyncio.sleep(_MEMORY_POLL_SECONDS)


# Back-to-back automatic launches. A launch returns as soon as the session is
# live, but the agent and its MCP servers take most of a minute to reach full
# size (~0.8-1.3 GB a window), so a memory reading taken right after one launch
# says nothing about it. One PR scan that found six PRs with review comments
# passed _wait_for_memory six times on readings taken before any of them had
# grown, and the machine ran out (WSL 2026-11-04). So each automatic launch
# starts at least _LAUNCH_SPACING_SECONDS after the previous one came up, and
# only then reads memory. ``MINDFLOCK_LAUNCH_SPACING`` (seconds, 0 = off)
# overrides the default.
_LAUNCH_SPACING_SECONDS = 60.0
_SPACING_POLL_SECONDS = 5.0
#: monotonic time the previous automatic launch passed the gate or came up,
#: whichever is later; 0.0 = none yet in this process.
_last_launch = 0.0
#: (event loop, lock) — one gate at a time per loop: the ticket, PR and issue
#: loops run concurrently, and two of them must not read the same clear
#: spacing and memory and both go. Keyed by loop because an asyncio.Lock is
#: bound to the loop it first waits on.
_launch_gate: tuple = (None, None)


def _launch_spacing() -> float:
    raw = os.environ.get("MINDFLOCK_LAUNCH_SPACING", "").strip()
    if not raw:
        return _LAUNCH_SPACING_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _LAUNCH_SPACING_SECONDS


def _note_launch() -> None:
    """Stamp a launch: the next one is spaced from now."""
    global _last_launch
    _last_launch = time.monotonic()


def _gate_lock() -> asyncio.Lock:
    global _launch_gate
    loop = asyncio.get_running_loop()
    if _launch_gate[0] is not loop:
        _launch_gate = (loop, asyncio.Lock())
    return _launch_gate[1]


async def _wait_for_launch_room(what: str) -> None:
    """Hold an automatic launch until the previous one has had
    _LAUNCH_SPACING_SECONDS to grow, then until memory has headroom
    (:func:`_wait_for_memory`), and stamp it. The spacing is re-checked after
    the memory wait: the previous session coming up meanwhile restarts it."""
    async with _gate_lock():
        logged = False
        while True:
            spacing = _launch_spacing()
            wait = (_last_launch + spacing - time.monotonic()) if _last_launch else 0.0
            if wait > 0:
                if not logged:
                    logger.info(
                        "Holding %s ~%ds so the session started before it "
                        "settles and memory can be read honestly",
                        what,
                        int(wait) or 1,
                    )
                    logged = True
                await asyncio.sleep(min(wait, _SPACING_POLL_SECONDS))
                continue
            await _wait_for_memory(what)
            wait = (_last_launch + spacing - time.monotonic()) if _last_launch else 0.0
            if wait <= 0:
                break
        _note_launch()


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


def _launch_workdirs(repo_url: str) -> list:
    """``provisioned.launch_workdirs`` — the local checkouts sharing the
    ticket repo's identity. ``[]`` on any failure."""
    try:
        from backend.session import provisioned

        return provisioned.launch_workdirs(repo_url)
    except Exception:  # noqa: BLE001 — decoration is best-effort
        return []


def _decorate_red_zones(prompt: str, program: str, workdirs) -> str:
    """The launch prompt with the repo's red-zone note and — when the repo's
    Plan-first flag is on AND the CLI supports plans — the plan-first
    instruction, keyed off the first existing dir in ``workdirs``. The same
    decoration the server's intake starts apply (``server._red_zone_prompt``),
    so a ticket reads the same whichever path launches it.

    Plan-first only for ``plan_supported()`` providers: it tells the agent to
    wait for a go-ahead, and the Go button lives in the Map's Plan section,
    which only those CLIs have. Idempotent; best-effort — never fails a
    launch."""
    if not prompt:
        return prompt
    try:
        import os

        from backend import providers as _providers
        from backend.config import red_zones as _red_zones

        provider = _providers.resolve(program)
        try:
            plans = bool(provider.plan_supported())
        except Exception:  # noqa: BLE001
            plans = False
        wd = next((d for d in workdirs or () if d and os.path.isdir(d)), "")
        return _red_zones.decorate_prompt(
            prompt,
            wd,
            hard_guard=bool(provider.red_zone_guard()),
            plan_first=None if plans else False,
        )
    except Exception:  # noqa: BLE001
        return prompt


def _fresh_settings():
    """``settings.json`` as it is on disk NOW. The settings cache is
    per-process and the pipeline is a separate one from the server, so a save
    made in the UI is only seen here when the read drops the cache first."""
    from backend.config import settings as _settings

    _settings.invalidate()
    return _settings.load_settings()


def _issue_repo(story) -> str:
    """``owner/name`` for a ticket the Intake → Issues MONITOR made from a
    GitHub issue (its ``app_url`` is the issue's page), ``""`` for any other
    ticket — a ``github_issues`` TICKETING SOURCE's story included: it carries
    a ``source_key`` and flies that source's plan, as its row start does."""
    if getattr(story, "provider", "") != "github_issues" or getattr(
        story, "source_key", ""
    ):
        return ""
    m = re.match(
        r"^https?://[^/]+/([^/\s]+/[^/\s]+)/issues/",
        str(getattr(story, "app_url", "") or ""),
    )
    return m.group(1) if m else ""


def _disarm_autopilot(title: str) -> None:
    try:
        from backend.web.core import autopilot as _autopilot

        _autopilot.disarm(title)
    except Exception:  # noqa: BLE001
        logger.warning("Could not disarm the autopilot for %s", title, exc_info=True)


def _cancel_flight(title: str, reason: str) -> None:
    """The session for ``title`` could not be created: cancel the flight armed
    for it (if any) and drop its autopilot record, so neither waits for a
    session that won't come."""
    _disarm_autopilot(title)
    try:
        from backend.web.core import flights as _flights

        f = _flights.for_session(title)
        if f is not None and f["state"] != "cancelled":
            _flights.mutate(f["id"], lambda x: _flights.op_cancel(x, reason[:300]))
    except Exception:  # noqa: BLE001
        logger.warning("Could not cancel the flight for %s", title, exc_info=True)


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

        await _wait_for_launch_room("ticket %s" % story.slug)
        branch = _branch_name_for(story)
        title = story.slug
        agent = self._agent_for(story)
        effort = self._effort_for(story)
        # Depth is configured per SOURCE, and two sources can share a provider
        # (two Jira projects, a Shortcut workspace per team). Looking it up by
        # ``story.provider`` matched whichever source of that provider came
        # first, so the second project silently ran at the first one's rung.
        issue_repo = _issue_repo(story)
        prompt += self._arm(
            title,
            "tix",
            source_key_of(story),
            str(getattr(story, "id", "") or story.slug),
            str(getattr(story, "name", "") or ""),
            subject={
                "kind": "issue" if issue_repo else "ticket",
                "source": issue_repo or source_key_of(story),
                "id": str(getattr(story, "id", "") or story.slug),
                "title": str(getattr(story, "name", "") or ""),
                "url": str(getattr(story, "app_url", "") or ""),
            },
            branch=branch,
        )
        logger.info(
            "Launching ticket %s via MindFlock (agent=%s, effort=%s, mode=%s, "
            "branch=%s, session=%s)",
            story.slug,
            agent or "engine default",
            effort or "CLI default",
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
                    effort,
                ),
                timeout=_INSTANCE_START_TIMEOUT,
            )
        except asyncio.TimeoutError:
            _cancel_flight(title, "the session start timed out")
            raise RuntimeError(
                f"MindFlock instance start for ticket {story.slug} timed out "
                f"after {_INSTANCE_START_TIMEOUT:.0f}s (the worker thread may "
                "still be running; the story is marked failed)"
            ) from None
        except Exception as err:
            _cancel_flight(title, "the session could not be created: %s" % err)
            raise
        finally:
            # Spaced from when it came up (or gave up), not from the gate:
            # provisioning a PR's clone can take most of the spacing itself.
            _note_launch()

        # Best-effort post-launch: drop attachments into the live workspace, and
        # move the ticket into its source's start state so the board stops
        # showing it as untouched work. Both are bookkeeping about a session that
        # is already live — neither can fail the launch.
        await self._post_start(inst, story)
        await move_started(story, self.config)
        # …and say on the ticket who is on it, for teammates whose MindFlock
        # isn't paired with this one (tracker_claims; best-effort).
        await mirror_started(story, self.config, title=title)
        logger.info(
            "Ticket %s is live in MindFlock session 'mindflock_%s'. "
            "Attach via the MindFlock UI or: tmux attach -t mindflock_%s",
            story.slug,
            title,
            title,
        )
        return branch

    def _stored_titles(self) -> set:
        """Session titles in MindFlock's state.json (raw JSON, no restores)."""
        try:
            from backend import config as cs_config

            raw = cs_config.LoadState().GetInstances()
            if isinstance(raw, (bytes, bytearray)):
                raw = bytes(raw).decode("utf-8")
            return {str(x.get("title")) for x in (json.loads(raw) if raw else [])}
        except Exception:  # noqa: BLE001
            return set()

    async def hand_to_builder(self, pr: PullRequest, prompt: str) -> str:
        """Give ``prompt`` to the session that BUILT the PR's branch (the
        ticket's own session — live, or reopened from Recently closed) through
        the server (``POST /api/sessions/by-branch/hand-off``) → its title, or
        "" when no session on this machine built it (or the server is out of
        reach): the caller then starts a PR session of its own."""
        port = os.environ.get("MINDFLOCK_SERVER_PORT", "").strip()
        branch = str(getattr(pr, "head_ref", "") or "")
        if not port or not branch:
            return ""
        headers = {"Content-Type": "application/json"}
        token = os.environ.get("MINDFLOCK_AUTH_TOKEN", "").strip()
        if not token:
            try:
                from backend.config import settings as _settings

                token = (_settings.load_settings().general.auth_token or "").strip()
            except Exception:  # noqa: BLE001
                token = ""
        if token:
            headers["Authorization"] = "Bearer " + token

        def _post() -> str:
            import urllib.error
            import urllib.request

            req = urllib.request.Request(
                "http://127.0.0.1:%s/api/sessions/by-branch/hand-off" % port,
                data=json.dumps({"branch": branch, "text": prompt}).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    body = json.loads(resp.read().decode("utf-8") or "{}")
            except (urllib.error.URLError, OSError, ValueError):
                return ""
            return str(body.get("title") or "")

        title = await asyncio.to_thread(_post)
        if title:
            logger.info(
                "PR #%s: handed to %s (the session that built %s)",
                pr.number,
                title,
                branch,
            )
        return title

    async def deliver_pr_round(self, pr: PullRequest, prompt: str) -> str:
        """Hand a review round (pr_rounds) to the PR's session: QUEUED into
        ``pr-<slug>`` when it still exists (the server's prompt-queue drain
        types it in once the agent is idle) → ``"queued"``; otherwise a new PR
        session with this prompt → ``"started"``."""
        if await self.hand_to_builder(pr, prompt):
            return "handed"
        title = f"pr-{pr_slug(pr)}"
        if title in self._stored_titles():
            from backend.web.core import prompt_queue as _pq

            _pq.enqueue(title, prompt)
            _pq.set_flags(title, enabled=True, held=False)
            logger.info("PR #%d: review round queued into %s", pr.number, title)
            return "queued"
        await self.run_pr(pr, [], prompt_override=prompt)
        return "started"

    async def run_pr(
        self, pr: PullRequest, comments: list[PRComment], prompt_override: str = ""
    ) -> str:
        """Address all PR comments in ONE MindFlock session; return head ref.

        Reuses PRProvisioner (correct fork / pull-head fetch) to materialize the
        PR's branch in a workspace, then adopts that workspace as a single CS
        session driven by one consolidated prompt — instead of a tmux tab per
        comment. Cursor launch is skipped; the CS web terminal is the surface.
        """
        if not prompt_override:
            # The session that built this branch takes the review, with its
            # context (docs/pr-review-rounds.md "The same session"); only
            # without one does the PR get a session of its own.
            handed = await self.hand_to_builder(
                pr,
                build_consolidated_pr_prompt(
                    pr, comments, "this session's own workspace"
                ),
            )
            if handed:
                return pr.head_ref
        await _wait_for_launch_room("PR #%s" % pr.number)
        workspace = await self._pr_provisioner.provision(pr, launch_cursor=False)
        prompt = prompt_override or build_consolidated_pr_prompt(
            pr, comments, workspace.directory
        )
        title = f"pr-{pr_slug(pr)}"
        prompt += self._arm(
            title,
            "pr",
            str(getattr(pr, "repo", "") or ""),
            "%s#%s" % (getattr(pr, "repo", ""), pr.number),
            str(getattr(pr, "title", "") or ""),
            subject={
                "kind": "pr",
                "source": str(getattr(pr, "repo", "") or ""),
                "id": str(pr.number),
                "title": str(getattr(pr, "title", "") or ""),
                "url": str(getattr(pr, "url", "") or ""),
            },
            branch=pr.head_ref,
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
            _cancel_flight(title, "the session start timed out")
            raise RuntimeError(
                f"MindFlock instance start for PR #{pr.number} timed out "
                f"after {_INSTANCE_START_TIMEOUT:.0f}s (the worker thread may "
                "still be running)"
            ) from None
        except Exception as err:
            _cancel_flight(title, "the session could not be created: %s" % err)
            raise
        finally:
            # Spaced from when it came up (or gave up), not from the gate:
            # provisioning a PR's clone can take most of the spacing itself.
            _note_launch()
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

    def _effort_for(self, story) -> str:
        """The thinking-effort rung this story's session runs at (``""`` = the
        CLI's own default).

        Same shape and the same re-read as :meth:`_agent_for` — the ticket's own
        stamp first, then the config as it is on disk right now — so changing a
        source's effort in the UI applies to the next ticket rather than the next
        pipeline restart. Unlike the agent chain there is no installation-wide
        fallback: see ``PipelineConfig.effort_for``.
        """
        from backend.ticket_ingestion.config import source_effort_now

        stamped = getattr(story, "effort", "")
        if stamped:
            return stamped
        return source_effort_now(getattr(story, "provider", ""))

    def _arm(
        self,
        title: str,
        kind: str,
        lookup: str,
        item: str,
        message: str,
        subject: dict,
        branch: str = "",
    ) -> str:
        """Arm what an AUTO-ingested item carries itself with, before its
        session exists: the flight its plan resolves to (docs/flights.md), else
        the autopilot depth (:meth:`_arm_autopilot`) — the server's
        ``_arm_intake`` for this process. A flight whose plan has ship steps
        arms its own lanes, so the depth is armed beside one only when its plan
        has none (and a leftover autopilot record on the title is dropped).
        ``branch`` fills the plan's ``{branch}``. Returns the text to append to
        the prompt: the plan's first agent step's own ``prompt``. Never
        raises.

        The flights store is a lock-guarded file keyed by session title, like
        autopilot.json, so arming here is picked up by the server's driver."""
        flight = None
        plan_id = self._plan_for(subject.get("kind") or "ticket", lookup, subject)
        if plan_id:
            try:
                from backend.web.core import flights as _flights

                try:
                    flight = _flights.arm_pending(
                        title, plan_id, subject, by="intake", branch=branch
                    )
                except _flights.FlightError as err:
                    if err.error != "in_flight":
                        raise
                    # A retry of an item whose last attempt died mid-launch:
                    # its flight is still waiting for this very title.
                    flight = _flights.for_session(title)
                logger.info("Flight armed for %s (%s)", title, plan_id)
            except Exception:  # noqa: BLE001 — never stop an ingestion over it
                logger.warning(
                    "Could not arm a flight for %s — falling back to its depth",
                    title,
                    exc_info=True,
                )
        if flight is None or not any(s["kind"] == "ship" for s in flight["steps"]):
            self._arm_autopilot(title, kind, lookup, item, message)
        else:
            _disarm_autopilot(title)
        if flight is None:
            return ""
        from backend.web.core import flights as _flights

        first = next((s for s in flight["steps"] if s["kind"] == "agent"), None)
        text = _flights.fill_prompt(
            (first or {}).get("config", {}).get("prompt") or "", flight
        )
        return "\n\n" + text.strip() if text.strip() else ""

    def _plan_for(self, kind: str, lookup: str, subject: dict) -> str:
        """The flight plan an auto-ingested item flies, ``""`` for none: the
        source's (``TicketingSource.plan``) or repo's (``plan`` in
        ``github.repo_settings`` / ``issue_repo_settings``) setting when that
        plan exists, else the built-in cut at :meth:`_depth_for` — the same
        chain as the server's ``_intake_plan`` minus a per-start pick, which an
        auto start never has. Read fresh (see :func:`_fresh_settings`)."""
        try:
            from backend.web.core import autopilot as _autopilot
            from backend.web.core import flights as _flights

            st = _fresh_settings()
            configured = ""
            depth = None
            if kind == "pr":
                block = (st.github.repo_settings or {}).get(lookup) or {}
                configured = str(block.get("plan") or "")
            elif kind == "issue":
                # A monitor issue: its repo's card, plan AND depth — the
                # lookup is the bare provider name, which must never pick up a
                # same-provider ticketing source's depth.
                repo = str(subject.get("source") or "")
                block = (st.github.issue_repo_settings or {}).get(repo) or {}
                configured = str(block.get("plan") or "")
                depth = str(block.get("depth") or "")
            else:
                sources = list(st.ticketing.sources)
                for match in ("id", "provider"):
                    src = next(
                        (
                            x
                            for x in sources
                            if lookup and getattr(x, match, "") == lookup
                        ),
                        None,
                    )
                    if src is not None:
                        configured = str(getattr(src, "plan", "") or "")
                        break
            configured = configured.strip()
            if configured and _flights.get_plan(configured) is not None:
                return configured
            if configured:
                logger.warning(
                    "Flight plan %s (set for %s) no longer exists — using the depth",
                    configured,
                    lookup,
                )
            if depth is None:
                depth = self._depth_for(lookup)
            depth = _autopilot.normalize_depth(depth)
            if depth not in _autopilot.SOURCE_DEPTHS:
                return ""
            return _flights.plan_for_depth(kind, depth)
        except Exception:  # noqa: BLE001
            logger.warning(
                "Could not resolve a flight plan for %s", lookup, exc_info=True
            )
            return ""

    def _arm_autopilot(
        self, title: str, kind: str, lookup: str, item: str, message: str
    ) -> None:
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

            depth = _autopilot.normalize_depth(self._depth_for(lookup))
            if depth in ("", "off") or depth not in _autopilot.SOURCE_DEPTHS:
                logger.info(
                    "No autopilot depth configured for %s (%s) — %s will not "
                    "fast-track itself",
                    lookup,
                    kind,
                    title,
                )
                return
            # `kind` is the record's own vocabulary (tix/pr/iss); `lookup` is the
            # provider or repo slug the DEPTH is configured against. Passing the
            # lookup as the source silently coerced it to "session", losing which
            # surface the run came from.
            # `message` is the ITEM'S name — the work as requested, not as made,
            # and identical for every commit the run produces. So it is armed as a
            # PLACEHOLDER: the commit step replaces it with a message written from
            # the final diff (the ✨ button's generator) and falls back to this
            # name when no model answers.
            _autopilot.arm(
                title,
                depth,
                source=kind,
                item=item,
                message=message or "",
                message_auto=bool(message),
            )
            logger.info("Autopilot armed for %s: will carry it to %s", title, depth)
        except Exception:  # noqa: BLE001
            # WARNING, not debug. This used to fail invisibly: a long-running
            # pipeline predating the feature simply never armed anything, and the
            # only symptom was the fast-track toggle sitting off with no
            # explanation anywhere.
            logger.warning(
                "Could not arm autopilot for %s — it will not ship itself",
                title,
                exc_info=True,
            )

    def _depth_for(self, source: str) -> str:
        """The configured autopilot depth for a ticketing source / repo slug.

        Reads the same ``settings.json`` the web server reads, so the per-source
        default has exactly one definition regardless of which process acts on it.
        """
        try:
            st = _fresh_settings()
            # The source's own key first: a provider name is only a fallback
            # for an unkeyed source (or a ticket built before ``source_key``
            # existed), and must never shadow a later source's exact id.
            sources = list(st.ticketing.sources)
            for src in sources:
                if source and source == getattr(src, "id", ""):
                    return str(getattr(src, "depth", "") or "")
            for src in sources:
                if source and source == getattr(src, "provider", ""):
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
        effort: str = "",
    ):
        from backend import session as cs_session
        from backend.providers import effort as _effort

        program = _resolve_program(agent)

        # HOW HARD TO THINK, translated for the CLI that is actually going to
        # run. The rungs the source stores are neutral; each provider spells the
        # request differently (a launch flag, a prompt keyword, or nothing at
        # all) and clamps anything above its own ceiling — which is why this has
        # to happen here, after `program` is resolved, rather than where the rung
        # was configured.
        prompt = _effort.decorate_prompt(prompt, program, effort)
        args = _effort.launch_args(program, effort)

        # Red-zone / plan-first decoration (roadmap: Code Map + red zones). The
        # provisioned worktree does not exist yet, so key off a local checkout
        # that shares its repo identity — the same launch_workdirs the server's
        # ticket/issue starts use (the URL itself when local, else the configured
        # default when local, else the provisioning base clone). Unconditional:
        # with no such checkout yet the repo's Plan-first flag can't be read
        # either, and decorate_prompt leaves the prompt alone.
        prompt = _decorate_red_zones(prompt, program, _launch_workdirs(repo_url))

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
            # Only when the rung contributes flags: InstanceOptions treats an
            # explicit value — even an empty tuple — as "use these verbatim",
            # so passing one unconditionally would strip the launch flags the
            # user set in Settings → Coding CLI from every ingested ticket.
            **({"launch_args": args} if args else {}),
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
        # Same decoration as the server's PR-review start: the workspace exists
        # already, so it is the key (zones note + the repo's Plan-first flag).
        prompt = _decorate_red_zones(prompt, program, [directory])

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
