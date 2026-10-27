"""Web UI backend for mindflock (Python port).

A FastAPI app that drives the *same* ported engine the TUI uses
(``backend.session`` / ``config`` / ``tmux``) but exposes it over HTTP +
websockets so a browser can show a sidebar of sessions and a live ``xterm.js``
terminal for each — connected straight to the underlying tmux sessions.

Run from inside a git repository (same requirement as the ``mindflock`` CLI):

    cd /path/to/your/repo
    uvicorn backend.web.server:app --port 8765       # (run from the src/ dir)

then open http://127.0.0.1:8765 .

The design is intentionally a thin "head" on the engine: instance lifecycle,
git worktrees, tmux sessions and JSON state are all reused unchanged, so the web
UI and the TUI (and the Go build) share the same ``~/.mindflock`` state.

What lives HERE vs in ``backend.web.core``
--------------------------------------------
This module owns the app assembly (lifespan, middleware, auth route), the
HTTP/websocket routes (except the route groups below), and the always-on
background loops (instances tick, events state tick, window-refresh keepalive)
plus the short-TTL probe memo they share. Helper logic lives in focused
modules under
``backend/web/core/``:

    agent_io         another agent's view of a session: /output views (last
                     reply, transcript, screen), /dialog (the parsed dialog)
                     and /answer (dialog keys)
    agent_sessions   tmux plumbing for the agent/shell panes (ensure/send/kill)
    agent_state      activity detection: working/clarify/idle/offline, stage
    auth             the access-token gate
    autopilot_driver the autopilot (Fast-track) loop: snapshot, act (commit,
                     check, push/PR/merge), the autopilot.* event, row lane
    budget           per-session cost budgets: guardrail event + input lock
    code_map         Code Map analysis: files, change set, import graph, blast
                     radius, tool feed, declared plans, zone dry-run
    code_outline     Code Map outlines: symbol outlines, the file view, symbol
                     search
    cronspec         5-field cron expressions for template schedules: parse,
                     slots (DST-safe), describe
    cursor_windows   IDE window adoption/focus/close (Cursor & friends)
    engine           the Engine singleton: instance registry + state reload
    events           the event bus behind /api/events
    git_ops          git primitives (branch/sha/dirty/origin probes, caches)
    ide_launch       launching the configured IDE on a folder
    lanes            ship lanes: how far a session is carried (the /fast-track
                     body, /lane, /ship-now), the row's lane, copy windows
    lineage          parent/child sessions: chain walks, spawn limits, base_ref
    thread           a session's family thread (members, spawn records, the
                     mail between them) and the row's last_report
    mailbox          inter-agent messages: store, exactly-once states, the
                     typed delivery line (the lane + routes live here)
    mobile_access    phone access: tailscale URLs, QR codes, startup banner
    pane_find        in-place Ctrl+F in a live pane (tmux copy-mode search)
    pane_scroll_find in-place Ctrl+F for apps that scroll themselves (wheel + look)
    plain_repo       base-folder selection/validation for plain sessions
    ports            per-session port-block reservations
    pr_review        automated PR-review pipeline glue
    proc             subprocess helpers: run_capped (a hard timeout that
                     never raises; re-exported here as _run_capped)
    queue_driver     the prompt-queue drain loop: per-session sends, the
                     usage-limit gate + resume watch, the mailbox lane
    prompt_queue     the per-session prompt queue store
    recently_closed  the undo store behind reopen / Ctrl+Z
    red_zone_monitor red-zone reconcile loop: guard files, hook health,
                     blocked/breached/tampered events, the row summary
    red_zone_driver  red-zone helpers for the zone routes and push/PR gate:
                     zones doc, breaches (live/uncommitted/committed),
                     green preview + exemptions, guard resync
    remote           tailnet multi-device discovery + proxying
    repo_picker      ranked repo suggestions for the New Session folder field
    revive           background revive: relaunch agents after a restart
    outbox           what is waiting on you / shipping / shipped / queued
                     (GET /api/outbox)
    session_create   the create route's body (POST /api/instances)
    session_branch   Branch session (POST /api/instances/{title}/branch): a new
                     worktree on a new branch from a session's HEAD, carrying
                     its uncommitted changes
    session_end      ending a session: DELETE / /close / /cleanup / the
                     workspace delete share one path (folder-in-use 409,
                     Recently closed, children re-parented)
    session_removal  the teardown every removal runs (_on_session_removed +
                     hooks), failed-Start records, per-title state forgetting
    session_stats    token/cost telemetry + transcript history rendering
    sleep            sleeping agents: stop an idle agent's CLI to free memory
                     (the sweep, the rules), wake it on demand (ensure_awake)
    snapshot         per-session JSON descriptors (labels, diff stat)
    state_events     session.*_changed diffing: settle + boot quiet windows,
                     session.turn_ended gates, human-input presence
    task_headlines   the row's task_headline: newest MEANINGFUL prompt as a
                     verb phrase (backend/task_headline.py), remembered per
                     session + conversation
    system_logs      log-tail sources for Settings → System logs
    team_runs        team runs: the record store, goal parser, pure planner
    team_run_driver  team runs: the loop, reconcile, effects, run.* events,
                     the operations behind /api/runs*
    template_schedule templates' Run now + schedules: due-time math, the
                     per-device run record, the loop (started by the
                     templates addon)
    test_plans_driver Verify: the push trigger, the due loop (liveness,
                     running plans, landings, orphan sweep), run-route helpers
    terminal         PTY/tmux attach plumbing shared by the websockets
    uploads          pasted/dropped file storage + retention
    usage_api        per-provider usage descriptors for /api/usage
    window_refresh   window-refresh keepalive config + scheduling
    workspaces       workspace roots, classification, guarded deletion
    worktree_setup   worktree setup/check (verification gate) runners

Route groups live in ``backend/web/routes/<name>.py``: one module-level
``router = APIRouter()`` per module, full paths (no prefix), routes that only
translate HTTP into core calls. This module ``app.include_router``s each one
AT THE POSITION its routes held here — route order decides path matching —
and re-imports its helpers, so tests that patch ``server._x`` still reach it.

    peer             peer links: /api/peer* (backend.peer.service does the work)
    team_runs        team runs /api/runs* and GET /api/outbox

Convention: extracted modules call back through the server namespace for
anything monkeypatched in tests (``_server()._foo(...)``), and this module
re-imports their private names, so ``monkeypatch.setattr(server, "_foo", …)``
keeps working no matter where ``_foo`` physically lives. When adding a helper
here, prefer growing (or adding) a core module and re-importing its names.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import datetime as _datetime
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import traceback
import weakref
from pathlib import Path
from typing import Dict, Optional, Tuple

from fastapi import Depends, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

import ptyprocess

from backend import config, log
from backend import providers
from backend import session
from backend import tailscale_cli as _tailscale_cli
from backend.providers import config as provider_config
from backend.providers import effort as _provider_effort
from backend.providers.claude import remove_trust_entry as _remove_trust_entry
from backend.config import ide as ide_cfg
from backend.config import red_zones as _red_zones
from backend.config import repo_zones as _repo_zones
from backend.session import instance as _instance
from backend.session import provisioned as provisioning
from backend.session import tmux
from backend.session.git import gh_available
from backend.session.git import remote_url as _remote_url
from backend.session.storage import Loading, Paused
from backend.workspace_setup import exclude_artifacts as _exclude_artifacts
from backend.workspace_setup import is_refresher_dirname as _is_refresher_dirname

# Core modules the monolith was split into (see backend.web/core/).
from backend.web.core import aliases as _aliases
from backend.web.core import hidden_sessions as _hidden_sessions
from backend.web.core import person_sessions as _person_sessions
from backend.web.core import shared_branch as _shared_branch
from backend.web.core import ownership as _ownership
from backend.web.core import person as _person
from backend.web.core import peer_guard as _peer_guard
from backend.web.core import session_end as _session_end
from backend.web.core import auth as _auth
from backend.web.core import tailnet_bind as _tailnet_bind
from backend.web.core import tailnet_trust as _tailnet_trust
from backend.web.core import fleet_claims as _fleet_claims
from backend.web.core import settings_sync as _settings_sync
from backend.web.core import setup_steps as _setup_steps
from backend.web.core import autopilot as _autopilot
from backend.web.core import lanes as _lanes
from backend.web.core import team_runs as _team_runs
from backend.web.core import team_run_driver as _team_run_driver
from backend.web.core import worker_order_driver as _worker_order_driver
from backend.web.core import code_map as _code_map
from backend.web.core import code_outline as _code_outline
from backend.web.core import commit_message as _commit_message
from backend.web.core import events as _events
from backend.web.core import ports as _ports
from backend.web.core import issue_start as _issue_start
from backend.web.core import github_pr as _github_pr
from backend.web.core import live_stage as _live_stage
from backend.web.core import mailbox as _mailbox
from backend.web.core import pr_review as _pr_review
from backend.web.core import reopen as _reopen
from backend.web.core import shared_link as _shared_link
from backend.web.core import worktree_reclaim as _worktree_reclaim
from backend.web.core import ticket_start as _ticket_start
from backend.web.core import ticket_merge as _ticket_merge
from backend.web.core import ticket_compose as _ticket_compose
from backend.web.core import ticket_draft as _ticket_draft
from backend.web.core import remote as _remote
from backend.web.core import stage_reset as _stage_reset
from backend.web.core import pending as _pending
from backend.web.core import prompt_queue as _prompt_queue
from backend.web.core import red_zone_monitor as _red_zone_monitor
from backend.web.core import ntfy as _ntfy
from backend.web.core import find_query as _find_query
from backend.web.core import pane_find as _pane_find
from backend.web.core import pane_scroll_find as _pane_scroll_find
from backend.web.core import session_plan as _session_plan
from backend.web.core import session_branch as _session_branch
from backend.web.core import session_create as _session_create
from backend.web.core import ship_status as _ship_status
from backend.web.core import agent_io as _agent_io
from backend.web.core import lineage as _lineage
from backend.web.core import thread as _thread
from backend.web.core import finished_children as _finished_children
from backend.mcp import playbooks as _playbooks
from backend.web.core import test_plans as _test_plans
from backend.web.core import window_refresh as _window_refresh
from backend.web.core import worktree_setup as _wt_setup
from backend.web.core.proc import run_capped as _run_capped
from backend.web.core import agent_state as _agent_state
from backend.web.core import task_headlines as _task_headlines
from backend.web.core.agent_state import (
    _ACTIVITY_CACHE,
    _ACTIVITY_CONFIRM_POLLS,
    _ACTIVITY_IDLE_AFTER,
    _ACTIVITY_NOISE_LINES,
    _BARE_SHELLS,
    _COMMIT_PLUMBING_RE,
    _CPU_ACTIVE_JIFFIES_PER_S,
    _LAST_BRANCH,
    _LIMIT_PROBE,
    _MARKER_TRUST_WINDOW_S,
    _PID_TREE_CACHE,
    _PID_TREE_TTL,
    _PRECOMMIT_LOCK_GRACE_S,
    _PROC_SNAPSHOT_CACHE,
    _PROC_SNAPSHOT_TTL,
    _SHELL_PROMPT_RE,
    _THREAD_RECORD_AT,
    _THREAD_RECORD_EVERY_S,
    _TRUST_DISMISS_AT,
    _TRUST_DISMISS_COOLDOWN,
    _TRUST_GATE_STARTUP_WINDOW_S,
    _agent_activity,
    _agent_exited,
    _capture_shell_pane,
    _clear_precommit_locks,
    _dismiss_trust_prompt,
    _failed_precommit_hook,
    _failed_precommit_step,
    _normalized_pane_hash,
    _pane_cpu_jiffies,
    _pane_has_agent_process,
    _pane_meta,
    _parse_failed_hook_id,
    _parse_failed_step,
    _parse_progress_tokens,
    _precommit_lock_is_live,
    _precommit_lock_path,
    _proc_cpu_snapshot,
    _session_conversation,
    _session_find_prompt,
    _session_last_prompt,
    _session_last_prompt_full,
    _session_last_turn,
    _session_stage,
)
from backend.web.core.cursor_windows import (
    _WIN_CURSOR_FOCUS_PS,
    _WIN_CURSOR_MAXIMIZE_PS,
    _activate_x11,
    _close_cursor_window,
    _cursor_autoadopt_loop,
    _cursor_autoadopt_tick,
    _cursor_open_folders,
    _cursor_storage_path,
    _cursor_title_terms,
    _cursor_uri_to_path,
    _cursor_windows_open,
    _find_cursor_windows,
    _focus_cursor_window,
    _maximize_new_cursor_window,
    _maximize_x11,
    _powershell,
    _ps_encoded,
    _win_app_condition,
    _win_title_condition,
)
from backend.web.core.agent_sessions import (
    PAUSED_REFUSED,
    _ensure_agent_session,
    _ensure_shell_session,
    _kill_agent_session,
    _kill_named_session,
    _kill_shell_session,
    _live_session_name,
    _send_escape_to_agent,
    _send_to_agent,
    _send_to_shell,
    _shell_tmux_name,
)
from backend.web.core.plain_repo import (
    _prepare_plain_repo,
    _raise_on_blocked_repo,
)
from backend.web.core.recently_closed import (
    _load_recently_closed,
    _record_closed,
    _recently_closed_path,
    _save_recently_closed,
)
from backend.web.core.repo_picker import (
    check_repo,
    search_repos,
    suggest_repos,
)
from backend.web.core.repo_branches import list_branches as _list_branches
from backend.web.core.workspaces import (
    STALE_WORKTREE_DAYS,
    _base_clone_references,
    _classify_workspace,
    _dir_size_bytes,
    _find_worktrees,
    _prune_empty_worktree_dirs,
    _remove_worktree_path,
    _strictly_under,
    _workspace_roots,
    _worktree_gitdir,
    _worktree_in_use_by_other,
    _worktree_repo_path,
    prune_stale_worktrees,
    recent_rows,
    scan_workspaces,
)
from backend.web.core.budget import (
    _BUDGET_FIRED,
    _budget_locked,
    _budget_overrides,
    _budget_overrides_path,
    _budget_status_for,
    _check_session_budget,
    _effective_budget,
    _forget_budget,
    _persist_budget_overrides,
    _session_budget_usd,
    _set_budget_override,
    _window_budget_usd,
)
from backend.web.core.session_stats import (
    _TOKENS_CACHE,
    _agent_transcript_text,
    _created_epoch,
    _session_tokens,
    forget_tokens as _forget_tokens,
)
from backend.web.core.snapshot import (
    _DIFF_STAT_CACHE,
    _folder_label,
    _instance_json,
    _live_parent,
    _parse_shortstat,
    _repo_name,
    _session_diff_stat,
    _session_fork_point,
)
from backend.web.core.engine import (
    Engine,
    get_engine,
    _load_disk_tombstones,
    _reload_loop,
    _sync_external_instances,
)
from backend.web.core import mobile_announce
from backend.web.core import restart as _restart
from backend.web.core import revive as _revive
from backend.web.core import sleep as _sleep
from backend.web.core import self_update as _self_update
from backend.web.core import spoken as _spoken
from backend.web.core import asking as _asking
from backend.web.core import update_watch as _update_watch
from backend.web.core import voice as _voice
from backend.web.core.mobile_access import (
    _local_only_mode,
    _mobile_banner,
    _mobile_info,
    _mobile_svg,
    _qr_lines,
    _server_port,
    _tailscale_info,
    _tailscale_serves_port,
)
from backend.web.core.system_logs import (
    _LOG_TAIL_MAX,
    _log_sources,
    _read_log_tail,
)
from backend.web.core.uploads import (
    _PASTE_KEEP,
    _clear_all_pastes,
    _paste_dirs,
    _prune_pastes,
    _safe_upload_name,
    write_workspace_paste as _write_workspace_paste,
)
from backend.web.core.usage_api import (
    _PROVIDER_LABELS,
    _provider_label,
    _provider_usage_entry,
    _usage_window_for,
)
from backend.web.core.git_ops import (
    git_available,
    _commits_beyond_base,
    _current_branch,
    _git_count,
    _git_has_commits,
    _git_head_sha,
    _has_origin,
    _has_upstream,
    _is_dirty,
    _is_git_repo,
    _make_initial_commit,
    _origin_branch_sha,
    _ORIGIN_SHA_CACHE,
    mark_origin_push_pending,
)
from backend.web.core.terminal import (
    apply_scroll_speed,
    is_terminal_reply,
    load_scroll_speed,
    pump_pty,
    save_scroll_speed,
    spawn_tail,
    spawn_tmux_attach,
    _clear_exit_marker,
    _exit_marker_path,
    _is_natural_exit,
    _read_exit_marker,
    _wrap_launch_cmd,
)
from backend.web.core.autopilot_driver import (
    _AUTOPILOT_ACTION_COOLDOWN,
    _AUTOPILOT_DEADLINES,
    _AUTOPILOT_INTERVAL,
    _autopilot_act,
    _autopilot_commit,
    _autopilot_default_message,
    _autopilot_draft_for_approval,
    _autopilot_dto,
    _autopilot_finish,
    _autopilot_halt,
    _autopilot_loop,
    _autopilot_note,
    _autopilot_observe,
    _autopilot_pass,
    _autopilot_run_check,
    _autopilot_snapshot,
    _autopilot_step,
    _autopilot_tell_agent,
    _autopilot_wait,
    _autopilot_written_message,
    _emit_autopilot,
    _emit_autopilot_event,
    _fasttrack_default,
    _fasttrack_depth,
    _pending_commit_message,
    _precommit_retry_hooks,
    _publish_autopilot,
    _resp_error,
    _resp_json,
    _row_lane,
    _row_run,
    _run_member,
)
from backend.web.core.test_plans_driver import (
    _TEST_PLAN_DUE_INTERVAL,
    _TEST_PLAN_GEN_ATTEMPTS,
    _TEST_PLAN_LANDED_BUDGET_S,
    _TEST_PLAN_LANDED_CHECKED,
    _TEST_PLAN_LANDED_SETTLE_S,
    _TEST_PLAN_LANDED_TTL_S,
    _TEST_PLAN_LIVE_BUDGET_S,
    _TEST_PLAN_LIVE_CHECKED,
    _TEST_PLAN_RUN_GIVE_UP_S,
    _TEST_PLAN_RUN_POLL_S,
    _VERIFY_DEAD_GRACE_S,
    _VERIFY_DEAD_SINCE,
    _VERIFY_ORPHAN_GRACE_S,
    _announce_test_plan_checked,
    _check_test_plan_landings,
    _check_test_plans_for_liveness,
    _clear_verify_results,
    _closed_session_plan_inputs,
    _end_verify_session,
    _ensure_test_plan,
    _ensure_test_plan_blocking,
    _free_stale_verify_worktree,
    _generate_test_plan,
    _is_verify_repo_usable,
    _kill_orphan_plan_tmux,
    _liveness_order,
    _merging_is_shipping,
    _notify_test_plan_due,
    _on_session_pushed,
    _poll_running_test_plans,
    _read_verify_results,
    _reclaim_plan_worktree,
    _recover_stalled_test_plans,
    _release_wedged_run,
    _start_test_plan_generation,
    _sweep_orphan_verify_sessions,
    _test_plan_intent,
    _test_plan_is_live,
    _test_plan_merged_into,
    _test_plan_row,
    _test_plan_run_started_at,
    _test_plan_session_ctx,
    _test_plans_due_loop,
    _test_plans_due_pass,
    _verify_auto_for,
    _verify_enabled,
    _verify_run_trees,
    _verify_session_usable,
    _verify_window_gone,
    _write_test_plan_for_closed,
)
from backend.web.core.state_events import (
    _ACTIVITY_SETTLE_SECONDS,
    _ATTACH_PROBE_GRACE_S,
    _BOOT_MONO,
    _BOOT_QUIET_SECONDS,
    _EVENT_SNAPSHOT,
    _EVENT_SNAPSHOT_LOCK,
    _HUMAN_HOLD_S,
    _HUMAN_INPUT_AT,
    _QUEUE_SEND_GRACE_S,
    _SETTLE_ACTIVITIES,
    _STAGE_SETTLE_SECONDS,
    _TRANSIENT_STAGES,
    _TURN_END_DWELLS,
    _TURN_END_DWELL_MARKER_S,
    _TURN_END_DWELL_S,
    _TURN_END_DWELL_STATUS_S,
    _autopilot_running,
    _emit_state_changes,
    _in_boot_quiet,
    _note_human_input,
    _note_turn_boundary,
    _seed_event_snapshot,
    _tmux_client_input_recent,
)
from backend.web.core.session_removal import (
    _CREATE_FAILURES,
    _CREATE_FAILURES_LOCK,
    _CREATE_FAILURES_MAX,
    _CREATE_FAILURE_TTL_S,
    _SESSION_REMOVED_HOOKS,
    _closed_just_now,
    _disarm_removed_session,
    _drop_failed_start,
    _forget_mcp_run_file,
    _forget_session_state,
    _note_create_failure,
    _on_session_removed,
    _orphan_children,
    _orphan_dangling_parents,
    _prune_session_state,
    _release_tracker_claim,
    _remember_finished_child,
)
from backend.web.core.queue_driver import (
    _LIMIT_EXHAUSTED_PCT,
    _LIMIT_FALLBACK,
    _LIMIT_RESTORE_QUIET,
    _LIMIT_RESUME_PROMPT,
    _LIMIT_STATE,
    _MAIL_ON_PROMPT,
    _MAIL_PRUNE_AFTER_S,
    _MAIL_STATE,
    _QUEUE_BOOT_GRACE,
    _QUEUE_DRAIN_INTERVAL,
    _QUEUE_IDLE_SETTLE,
    _QUEUE_IDLE_SETTLE_MARKER,
    _QUEUE_REARM_IDLE,
    _QUEUE_REBOOT_COOLDOWN,
    _QUEUE_SEND_COOLDOWN,
    _QUEUE_STATE,
    _dialog_on_screen,
    _drain_mailboxes,
    _drain_one_mailbox,
    _drain_one_queue,
    _drain_prompt_queues,
    _human_at_keys,
    _idle_settled,
    _limited_titles,
    _live_limit_reset,
    _mail_provider_name,
    _mail_type,
    _meter_is_about_this_session,
    _pane_holds_agent,
    _prompt_queue_drain_loop,
    _queue_wants_turn,
    _refresh_limit_state,
    _resume_on_usage_reset,
    _send_consumed,
    _send_queued_item,
    _session_limited_until,
    _typing_held,
    _watch_limited_sessions,
    _watch_one_limited,
)
from backend.web.core.red_zone_driver import (
    _RZ_SYNC_CAP,
    _clean_rel_arg,
    _green_preview,
    _live_breaches,
    _live_repo_roots,
    _pr_committed_changed,
    _pr_content_ref,
    _red_zone_breaches_for,
    _rz_changed_paths,
    _rz_doc,
    _rz_exempt_ids,
    _rz_green_exempt,
    _rz_hard,
    _rz_repo,
    _rz_resync_after,
    _sessions_on_worktree,
    _shared_zone_author,
    _shared_zones_body,
    _uncommitted_zone_breaches,
    _zone_breach_message,
)
from backend.web.addons import AppContext as _AddonContext, register_addons
from backend.web.routes import peer as _peer_routes
from backend.web.routes import team_runs as _team_runs_routes
from backend.web.core import voice_actions as _voice_actions
from backend.web.routes import speech as _speech_routes
from backend.web.routes import voice as _voice_routes
from backend.web.routes.peer import (
    _peer_body,
    _peer_call,
    _peer_remote_refusal,
    _peer_service,
)
from backend.web.routes.team_runs import _run_error

# --------------------------------------------------------------------------- #
# Engine bootstrap
# --------------------------------------------------------------------------- #
log.Initialize(False)

_STATIC = Path(__file__).parent / "static"


# Background asyncio tasks started by the lifespan; cancelled on shutdown.
_BG_TASKS: list = []

# Set near the static mount once `app` + `ENGINE` exist; the lifespan reads them
# at startup (by which point they're populated).
ADDONS: list = []
_ADDON_CTX = None


def _register_task(coro):
    """Track a background asyncio task so the lifespan cancels it on shutdown.

    Finished tasks remove themselves so short-lived per-request tasks (session
    starts, PR reviews) don't accumulate for the life of the server.
    """
    t = asyncio.create_task(coro)
    _BG_TASKS.append(t)

    def _discard(task):
        try:
            _BG_TASKS.remove(task)
        except ValueError:  # already cleared by lifespan teardown
            pass

    t.add_done_callback(_discard)
    return t


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    """Start background loops + addon startup hooks; tear everything down on exit.

    Replaces the old scattered ``@app.on_event("startup")`` hooks with one
    deterministic place that also CLEANS UP (the old code never cancelled the
    reload / cursor-adopt loops, and leaked the MindFlock child). The referenced
    loops/banner are module-level functions defined further down; they exist by
    the time this runs.
    """
    # Repair PATH first: a GUI-launched backend inherits a minimal PATH (no
    # shell profile), so install detection (Settings → Agent CLIs) and every
    # CLI we spawn would otherwise miss tools that work fine in the terminal.
    # Idempotent + never raises; runs before we serve any request.
    try:
        from backend import pathenv

        await asyncio.to_thread(pathenv.ensure_enriched)
    except Exception:  # noqa: BLE001 — PATH repair must never block startup
        pass
    # Give the event bus the server loop so user shell hooks (B3) can be
    # spawned fire-and-forget even when emit() happens on a worker thread.
    _events.BUS.set_loop(asyncio.get_running_loop())
    # The person this device belongs to (core.person): an existing store gets
    # its id on this first load, before anything stamps a session with it.
    try:
        await asyncio.to_thread(_person.ensure_id)
    except Exception:  # noqa: BLE001 — never block startup on a label
        pass
    # Voice: the local whisper models configured for recognition are loaded a
    # few seconds after boot, and the voice a few after that (daemon threads;
    # each a no-op when there is nothing to warm), so the first press of the
    # mic — or the first line read aloud — after a restart is not a wait.
    # MINDFLOCK_VOICE_WARM=0 skips it (the test suite: a warm-up thread left
    # by one app's lifespan would otherwise load a model into the next test).
    if os.environ.get("MINDFLOCK_VOICE_WARM", "1") != "0":
        try:
            from backend.web.core import speech as _speech_warm

            _speech_warm.warm_in_background("stt", delay_s=3.0)
            _speech_warm.warm_in_background("tts", delay_s=6.0)
        except Exception:  # noqa: BLE001 — warming is best effort
            pass
    # "Read everything" survived the restart: the live sessions' hooks must
    # know the marker (a session launched by an older build does not).
    try:
        if _voice.say_all():
            await asyncio.to_thread(repin_voice_hooks)
    except Exception:  # noqa: BLE001 — enrichment
        pass
    # Adopt loop: pull in instances written by other processes (TUI/pipeline).
    _register_task(_reload_loop())
    # Instances tick: the side effects that used to piggyback on the browser's
    # GET /api/instances poll (budget-crossing notifications, auto check-run
    # kicks, *_changed events, the addon sessions snapshot). Always-on so they
    # keep firing with zero clients connected; the GET is now read-only.
    _register_task(_instances_tick_loop())
    # Red zones: keep every live worktree's guard file current, re-arm hooks
    # that were disarmed, and turn the tool feed into blocked/breached/
    # tampered events. Its own loop (not a piggyback on the instances tick) so
    # its git work can never slow the sidebar snapshot. Off under pytest (see
    # _RED_ZONE_LOOP_ENABLED): ENGINE is the developer's real state there.
    if _red_zone_loop_enabled():
        _register_task(_red_zone_loop())
    # Cursor auto-adopt: always runs; checks its runtime flag each tick.
    _register_task(_cursor_autoadopt_loop())
    # Prompt-queue drain: feed queued prompts to idle agents (keeps runs going
    # unattended and resumes them the moment usage returns after an outage).
    _register_task(_prompt_queue_drain_loop())
    _register_task(_autopilot_loop())
    # Team runs: start queued work as slots free up, carry each member along
    # its lane (through the autopilot above), surface what needs you. Off
    # under pytest like the red-zone loop — ENGINE is the developer's real
    # state there.
    if _team_run_loop_enabled():
        _team_run_driver.subscribe()
        _register_task(_team_run_driver.run_loop())
    # Worker order: release an orchestrator's held workers when their turn
    # comes, land their fences first (core.worker_order). Off under pytest
    # like the team-run loop.
    if _team_run_loop_enabled():
        _register_task(_worker_order_driver.run_loop())
    # So the autopilot driver, which runs its blocking half in worker threads, can
    # still start an edge watcher (asyncio.create_task needs the loop).
    _live_stage.set_loop(asyncio.get_running_loop())
    _register_task(_window_refresh_loop())
    # Ctrl+F ahead of time: keep agent panes' find indexes current, build
    # missing ones where nobody is looking (see _find_index_tick).
    _register_task(_find_index_loop())
    # Verify: watch origin until each generated test plan's work reaches the
    # live branch, and poll the verify sessions that are working through one.
    _register_task(_test_plans_due_loop())
    # Tailnet device discovery + remote session snapshots (multi-device mode).
    _register_task(_remote.discovery_loop(_server_port()))
    _register_task(_remote.instances_loop())
    # Settings sync across the paired devices (a no-op tick while it's off).
    _register_task(_settings_sync.sync_loop())
    # Engine updates: re-exec onto a finished install with no browser tab
    # open (core.update_watch), and announce a newer release (update.available).
    _register_task(_update_watch.watch_loop())
    # Shared phone link: keep the Tailscale Service advertisement applied —
    # re-serve when its serve config vanished or this device's tags changed
    # (a no-op tick while general.shared_link is off).
    _register_task(_shared_link.recheck_loop(_server_port))
    # Tailscale mode came up bound to every interface because tailscaled
    # wasn't running yet (run.py's fallback): narrow the bind once it is
    # (core.tailnet_bind). Never registered otherwise — nor in tests.
    if _tailnet_bind.fell_back():
        _register_task(_tailnet_bind.rebind_loop())
    # Peer links: a no-op unless enabled in settings (no identity, no socket).
    try:
        await _peer_service().start()
    except Exception as err:  # noqa: BLE001 — never block startup
        if log.ErrorLog is not None:
            log.ErrorLog.Printf("peer service start failed: %v", err)

    # Non-critical warmups run in the background so the server answers its
    # first request immediately instead of waiting on shell-outs (the
    # Tailscale banner probe alone can hang for seconds on a machine without
    # tailscale).
    async def _startup_warmups() -> None:
        # Tailscale mode is on but this process came up bound to 127.0.0.1 (a
        # `serve local` that predates the toggle, or a CS_WEB_MODE inherited
        # from somewhere): the setting isn't in effect, and the fix is a
        # restart — so take it, up to core.restart's attempt cap. Everything
        # below would be work done by a process on its way out.
        if await asyncio.to_thread(_restart.auto_restart_for_tailscale):
            return
        # Apply the persisted terminal scroll speed to already-running sessions.
        try:
            await asyncio.to_thread(apply_scroll_speed)
        except Exception:  # noqa: BLE001
            pass
        # Verify runs created before the `hidden_by` flag existed were kept off
        # the rail by their title alone; flag the ones a checklist still names
        # so upgrading does not list every old run as work (idempotent).
        try:
            await asyncio.to_thread(_backfill_hidden_verify_sessions)
        except Exception:  # noqa: BLE001
            pass
        # Pasted screenshots are transient: every restart clears them all (each
        # new paste also prunes its directory to the newest _PASTE_KEEP).
        try:
            await asyncio.to_thread(_clear_all_pastes)
        except Exception:  # noqa: BLE001
            pass
        # The shared phone link (general.shared_link): advertise this device as
        # a host of the fleet's one Tailscale Service before the banner, which
        # then prints that URL instead of this machine's own.
        try:
            await asyncio.to_thread(_shared_link.apply, _server_port())
        except Exception:  # noqa: BLE001
            pass
        # Mobile/Tailscale banner (probes shell out -> keep off the event loop).
        # stdout gets the full banner (token + QR); the log file gets the
        # redacted copy — mindflock.log is served back out via GET /api/logs, so
        # the access token (or a QR encoding it) must never land there.
        try:
            banner = await asyncio.to_thread(_mobile_banner)
            if banner:  # local mode prints no mobile box (see _mobile_banner)
                print(banner, flush=True)
            if log.ErrorLog is not None:
                try:
                    log.ErrorLog.Printf(
                        "%s",
                        await asyncio.to_thread(lambda: _mobile_banner(for_log=True)),
                    )
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass
        # Same URL, second channel: if ntfy is configured and Tailscale is up,
        # the phone gets the /m URL as a push (without the token) so reaching
        # the mobile view doesn't require being at this console to read it.
        await mobile_announce.announce(mobile_announce.REASON_STARTUP)

    _register_task(_startup_warmups())
    # Background revive (core.revive): after the boot quiet window, relaunch
    # every session whose agent was running at shutdown, so nothing waits on
    # Offline until someone clicks it. Off under pytest — ENGINE is the
    # developer's real state there, and this LAUNCHES agents.
    if _revive_on_start_loop_enabled():
        _register_task(_revive.run_on_start())
    # Sleeping agents (core.sleep): stop the CLI (and its MCP servers) of an
    # agent idle and unwatched for general.sleep_idle_minutes, to give its
    # memory back. Off under pytest — ENGINE is the developer's real state,
    # and this KILLS agents.
    if _sleep_loop_enabled():
        _register_task(_sleep.run_loop())
    _register_task(_memory_watch_loop())
    # The Map's snapshot warmer reads every open session's worktree — never
    # under pytest (ENGINE is the developer's real state there).
    if _code_map_warm_on_start_enabled():
        _register_task(_code_map_warm_on_start())
    # Addon startup hooks (e.g. the Assistant seeds its working dir here).
    for _addon in ADDONS:
        try:
            await _addon.on_startup(_ADDON_CTX)
        except Exception as err:  # noqa: BLE001
            if log.ErrorLog is not None:
                log.ErrorLog.Printf("addon %s startup failed: %v", _addon.id, err)
    try:
        yield
    finally:
        # Addon shutdown hooks (reverse order), then cancel background tasks.
        for _addon in reversed(ADDONS):
            try:
                await _addon.on_shutdown(_ADDON_CTX)
            except Exception as err:  # noqa: BLE001
                if log.ErrorLog is not None:
                    log.ErrorLog.Printf("addon %s shutdown failed: %v", _addon.id, err)
        for _t in _BG_TASKS:
            _t.cancel()
        _BG_TASKS.clear()
        # Hand back this server's team-run leases: a restart drives (and
        # reconciles) its runs on the first pass instead of waiting out them.
        try:
            _team_runs.release_leases(_SERVER_BOOT_ID)
        except Exception:  # noqa: BLE001
            pass
        # Stop answering the shared phone link: a machine that is on but not
        # running MindFlock must not keep drawing the phone's traffic.
        try:
            await asyncio.to_thread(_shared_link.withdraw)
        except Exception:  # noqa: BLE001
            pass
        # Close the remote-proxy HTTP session (owned by backend.web.core.remote).
        try:
            await _remote.shutdown()
        except Exception:  # noqa: BLE001
            pass
        # Peer links: close the transport, the share sockets and proxies.
        try:
            await _peer_service().stop()
        except Exception:  # noqa: BLE001
            pass
        # The /api/events state ticker (F6) normally ends itself when the last
        # client disconnects; don't leave it running through shutdown.
        if _EVENTS_TICK_TASK is not None:
            _EVENTS_TICK_TASK.cancel()


app = FastAPI(title="MindFlock", lifespan=lifespan)


@app.middleware("http")
async def _revalidate_ui_assets(request, call_next):
    """Make browsers revalidate the UI assets on every load.

    Starlette's StaticFiles sends an etag/last-modified but no Cache-Control, so
    browsers apply *heuristic* caching and can serve a stale style.css/app.js
    after an edit (the page's HTML refreshes but its subresources don't). Tagging
    our own assets ``no-cache`` forces a cheap conditional GET (304 when
    unchanged), so edits always show up without a manual hard-refresh.
    """
    response = await call_next(request)
    path = request.url.path
    # Third-party vendor bundles (xterm.js ~283KB etc.) are versioned and never
    # edited in place — cache them hard so repeat loads don't refetch/revalidate
    # them. Our own hand-edited assets stay no-cache so edits show up on reload.
    if path.startswith("/vendor/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif path in ("/", "/m") or path.endswith((".html", ".css", ".js")):
        response.headers["Cache-Control"] = "no-cache"
    return response


# Endpoints the UI polls on a timer. Quiet by DEFAULT: every client polls
# /api/instances every 4s, and each log line is a lock + write + flush on the
# event loop, so logging them stalls concurrent requests and grows the log
# without bound. Errors and slow requests are still always logged. Set
# MINDFLOCK_LOG_QUIET=0 to log every request (comprehensive).
_LOG_QUIET = os.environ.get("MINDFLOCK_LOG_QUIET", "1").lower() not in (
    "0",
    "false",
    "no",
)
_LOG_QUIET_PREFIXES = ("/vendor/", "/static/")
_LOG_QUIET_PATHS = frozenset({"/api/instances", "/api/events", "/favicon.ico"})
_LOG_TOKEN_RE = re.compile(r"(token=)[^&]*")  # keep access tokens out of the log
_LOG_SLOW_MS = 1500.0


@app.middleware("http")
async def _checklist_store_errors(request, call_next):
    """Answer a checklist request whose STORE WRITE failed in words.

    ``test_plans._save`` re-raises — correctly: a write that did not land must
    not be reported as one — and every route below catches ``ValueError`` only.
    So a full disk, a read-only home or a permissions change came back as
    Starlette's plain-text 500, which the client renders as
    ``/api/test-plans/sc-1/result -> 500``: no sentence, nothing to act on, and
    an answer the person just recorded silently gone.

    Scoped to this feature's own paths, and to ``OSError`` — the one exception
    class the store raises for "the filesystem said no" — so nothing else in the
    app changes shape. 503, not 500: the request was fine and would work again
    once there is somewhere to write.
    """
    if not request.url.path.startswith("/api/test-plans"):
        return await call_next(request)
    try:
        return await call_next(request)
    except OSError as err:
        try:
            if log.ErrorLog is not None:
                log.ErrorLog.Printf(
                    "checklist store write failed on %s: %v", request.url.path, err
                )
        except Exception:  # noqa: BLE001
            pass
        return JSONResponse(
            {
                "error": "couldn't save the checklist — %s. Nothing was recorded; "
                "try again once there is somewhere to write." % err
            },
            status_code=503,
        )


@app.middleware("http")
async def _activity_log(request, call_next):
    """Log every request to ``mindflock.log`` (Settings → System logs) so normal
    activity — not just errors — is visible: ``METHOD /path -> status ms client``.

    By default it logs everything. Set ``MINDFLOCK_LOG_QUIET=1`` to drop the UI's
    high-frequency poll endpoints (still logging them when they error or run
    slow). Logging is best-effort and never affects the response."""
    start = time.monotonic()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        try:
            if log.InfoLog is not None:
                path = request.url.path
                ms = (time.monotonic() - start) * 1000.0
                quiet = _LOG_QUIET and (
                    path in _LOG_QUIET_PATHS or path.startswith(_LOG_QUIET_PREFIXES)
                )
                # A quiet endpoint is still logged when it errors or drags.
                if not quiet or status >= 400 or ms >= _LOG_SLOW_MS:
                    q = request.url.query
                    q = ("?" + _LOG_TOKEN_RE.sub(r"\1<redacted>", q)) if q else ""
                    client = request.client.host if request.client else "-"
                    log.InfoLog.Printf(
                        "%s %s%s -> %d %.0fms %s",
                        request.method,
                        path,
                        q,
                        status,
                        ms,
                        client,
                    )
        except Exception:  # noqa: BLE001 — logging must never break a request
            pass


# Compress text responses (the big win on cold load: app.js + style.css +
# xterm.js are ~510KB of uncompressed text → ~4x smaller gzipped). Added before
# the auth gate so auth stays OUTERMOST.
app.add_middleware(GZipMiddleware, minimum_size=1024)

# Remote-device proxy: requests for `<device>::<title>` sessions are forwarded
# to the MindFlock server that owns them (backend.web.core.remote). Added
# BEFORE the auth gate so auth stays outermost — a request must pass the LOCAL
# token check before it can be proxied anywhere.
app.add_middleware(_remote.RemoteProxyMiddleware)

# Bearer-token auth gate (covers HTTP + websockets). Added AFTER the cache
# middleware so it sits OUTERMOST — auth is checked before anything else runs.
# No-op unless enabled (tailnet exposure / configured token / MINDFLOCK_AUTH=1).
app.add_middleware(_auth.AuthMiddleware)

# Outermost of all: record the transport peer, then apply the proxy-headers
# rewrite uvicorn would have (run.py turns uvicorn's own off). The Tailscale
# trust check reads the recorded peer — see backend.web.core.tailnet_trust.
app.add_middleware(_tailnet_trust.PeerCaptureMiddleware)


@app.post("/api/auth")
def auth_login(payload: dict) -> JSONResponse:
    """Validate a token and set the ``mf_auth`` cookie (login-page target).

    Always allowed through the gate (public path). Returns 401 on a bad token
    so the login page can show an error; never echoes the token back."""
    token = str((payload or {}).get("token", "") or "").strip()
    if not _auth.token_valid(token):
        return JSONResponse({"error": "invalid token"}, status_code=401)
    # The plain cookie plus this token's keyed copy, so signing in here never
    # signs the browser out of another device on the shared phone link.
    return _auth.set_auth_cookies(JSONResponse({"ok": True}))


# The process-wide Engine singleton (core.engine).
ENGINE = get_engine()


# --------------------------------------------------------------------------- #
# REST API
# --------------------------------------------------------------------------- #


# ---- Short-TTL memo for the expensive per-session probes ------------------- #
# GET /api/instances is polled every ~4s by EVERY client (desktop + phone +
# the /api/events tick + the background instances tick), and the per-session
# probes it runs shell out to git/tmux or scan transcript files. The memo
# collapses those concurrent callers into at most one probe run per session
# per _PROBE_TTL (same pattern as _DIFF_STAT_CACHE: module dict + timestamps +
# a lock, since the HTTP poll runs in worker threads while the ticks run via
# to_thread). Keyed (probe, title) and guarded by a weakref to the instance
# (not id(), which the allocator reuses) so a recreated same-title session
# never serves the old instance's answer. TTL < the 4s poll, so any single
# poller still recomputes every tick; only extra concurrent pollers hit the
# memo (≤ ~2.5s-stale data, by design).
_PROBE_CACHE: Dict[tuple, tuple] = (
    {}
)  # (probe, title) -> (expires_mono, inst_ref, value)
_PROBE_CACHE_LOCK = threading.Lock()
_PROBE_TTL = 2.5


def _probe_cached(probe: str, inst, compute):
    """``compute()`` memoized ~_PROBE_TTL seconds per (probe, session)."""
    title = getattr(inst, "Title", "") or ""
    key = (probe, title)
    now = time.monotonic()
    with _PROBE_CACHE_LOCK:
        hit = _PROBE_CACHE.get(key)
        if hit and hit[0] > now and hit[1]() is inst:
            return hit[2]
    value = compute()
    try:
        ref = weakref.ref(inst)
    except TypeError:  # non-weakref-able stand-in (tests): don't memoize
        return value
    with _PROBE_CACHE_LOCK:
        _PROBE_CACHE[key] = (time.monotonic() + _PROBE_TTL, ref, value)
    return value


def _probe_seed(probe: str, inst, value):
    """Publish an already-computed probe result into the memo.

    The counterpart to :func:`_probe_cached`, for the caller that computed the
    value ITSELF and wants everyone else to reuse it. Same key, same TTL, same
    weakref guard — a non-weakref-able stand-in (tests) is simply not memoized,
    exactly as on the read path.

    This exists because the memo is shared by two 4s tickers that are NOT
    phase-locked (``_tick_state_changes`` and ``_instances_tick``). Whichever
    ran first filled the entry, so the *publishing* tick could SERVE a stage up
    to ``_PROBE_TTL`` (2.5s) older than the moment it published it — injecting a
    whole extra publish period into a stage flip, systematically, for the life
    of a server run depending on the startup offset. Seeding inverts that: the
    publisher computes and donates, every other reader still gets the memo's
    cost collapse.
    """
    title = getattr(inst, "Title", "") or ""
    try:
        ref = weakref.ref(inst)
    except TypeError:  # non-weakref-able stand-in (tests): don't memoize
        return value
    with _PROBE_CACHE_LOCK:
        _PROBE_CACHE[(probe, title)] = (time.monotonic() + _PROBE_TTL, ref, value)
    return value


def _free_untitled() -> str:
    """The next free ``untitled`` name (titles key ``ENGINE.instances``).

    Its own function because it is needed twice: once early, to name the
    instance, and again inside the registration lock — where the first answer
    may have gone stale while a repo was being prepared on another thread.
    """
    title = "untitled"
    n = 2
    branches = _session_branches()
    while title in ENGINE.instances or _aliases.owner_of(title, branches):
        title = "untitled-%d" % n
        n += 1
    return title


def _provider_seeds_prompt(program: str) -> bool:
    """Whether a plain session's initial prompt must NOT be held in the prompt
    queue: the provider declares a ``prompt_arg`` (the launch command carries
    it), or the program is a bare shell — whose "prompt" the queue drain would
    type into bash, which runs it. An unknown program resolves to ``generic``
    with no ``prompt_arg`` and so reads as "no" (held, typed once idle); only
    a provider lookup that RAISES reads as yes."""
    try:
        exe = os.path.basename((program or "").split()[0]).lower()
    except IndexError:
        exe = ""
    if exe in _BARE_SHELLS:
        return True
    try:
        return bool(providers.resolve(program or "").launcher_spec().prompt_arg)
    except Exception:  # noqa: BLE001
        return True


def _session_branch_name(title: str) -> str:
    """The branch a plain worktree session titled ``title`` gets
    (``<branch_prefix><title>``, sanitized exactly as ``new_git_worktree``
    builds it); "" when the config cannot be read."""
    try:
        from backend.config import config as _config
        from backend.session.git.worktree import sanitize_branch_name

        return sanitize_branch_name(
            "{}{}".format(_config.LoadConfig().branch_prefix, title)
        )
    except Exception:  # noqa: BLE001 — the engine's own check is the backstop
        return ""


# The engine drops instances on its own too (another MindFlock deleted them —
# tombstone convergence); those removals get the same teardown
# (core.session_removal; registered here, where ENGINE exists).
ENGINE.add_removal_listener(_on_session_removed)


def _forget_probes(title: str) -> None:
    """Drop the memoized probe RESULTS for one session, so the next read
    recomputes instead of serving a value from before an action.

    The freshness half of what used to be one function. Its callers are the
    workflow verbs — commit, push, make-PR, merge, and autopilot's own commit —
    which all want the same thing: "I just changed this session's git state,
    don't hand anyone a 2.5s-old stage". None of them wants the session's
    ROLLING STATE forgotten, and forgetting it was actively harmful: it wiped
    the record's ``worked_at``, so a session that had genuinely been working
    lost the evidence behind its turn-end announcement the moment you pressed
    Commit. That lives in :func:`_forget_session_state` now, which only the
    delete path calls.
    """
    with _PROBE_CACHE_LOCK:
        for k in [k for k in _PROBE_CACHE if k[1] == title]:
            _PROBE_CACHE.pop(k, None)
    _forget_tokens(title)


def _backfill_hidden_verify_sessions() -> int:
    """Flag the verify runs that predate ``hidden_by`` (startup, once).

    A live ``verify-…`` session that a checklist still names — as its current
    run or any run in its history — gets ``hidden_by: "verify"``, which is what
    kept it off the rail by title before the flag existed. The association is
    required: the prefix alone is the heuristic the flag replaces.
    """
    sessions: set = set()
    for plan in _test_plans.list_plans():
        sessions.add(str(plan.get("run_session") or ""))
        for run in plan.get("runs") or ():
            if isinstance(run, dict):
                sessions.add(str(run.get("session") or ""))
    sessions.discard("")
    return _hidden_sessions.backfill_verify(list(ENGINE.instances.keys()), sessions)


def _session_stage_cached(inst) -> dict:
    """``_session_stage(inst)`` memoized per session (see ``_probe_cached``)."""
    return _probe_cached("stage", inst, lambda: _session_stage(inst))


def _session_stage_fresh(inst) -> dict:
    """``_session_stage(inst)`` computed NOW, then donated to the memo.

    For the snapshot PUBLISHER (and the on-demand single-session read), which
    must never hand out a stage older than the publish it stamps. Everyone else
    keeps using :func:`_session_stage_cached`. See :func:`_probe_seed` for why
    the distinction is worth a second function.
    """
    return _probe_seed("stage", inst, _session_stage(inst))


def _agent_activity_cached(inst, title: str) -> str:
    """``_agent_activity(inst, title)`` memoized per session (working/idle/…)."""
    return _probe_cached("activity", inst, lambda: _agent_activity(inst, title))


def _background_jobs(inst, title: str) -> Optional[list]:
    """The background jobs the session's CLI left running past its turn, as
    the activity probe last classified them (running / waiting / stuck), or
    None. Read after the activity probe, never a fresh process walk.

    Runs on every snapshot, so it is also where a job that first passes its
    expected duration is announced: ``session.job_past_expected``, once per
    job (claude_background.announce_past_expected dedupes it)."""
    try:
        provider = providers.resolve(getattr(inst, "Program", "") or "")
        jobs = provider.background_jobs(tmux.to_mindflock_tmux_name(title)) or None
    except Exception:  # noqa: BLE001 — a probe never fails a poll
        return None
    if jobs:
        _announce_jobs_past_expected(title, jobs)
    return jobs


def _announce_jobs_past_expected(title: str, jobs: list) -> None:
    """Emit ``session.job_past_expected`` for each job in ``jobs`` that just
    passed its expected duration (never twice for one job). Never raises."""
    try:
        from backend.providers import claude_background as _bg

        due = _bg.announce_past_expected(jobs)
    except Exception:  # noqa: BLE001 — a probe never fails a poll
        return
    for job in due:
        expect_s = float(job.get("expect_s") or 0)
        elapsed = max(0.0, time.time() - float(job.get("started") or 0))
        head = (job.get("command") or "").strip().splitlines()[:1]
        _events.BUS.emit(
            "session.job_past_expected",
            session=title,
            data={
                "title": title,
                "job_id": job.get("id"),
                "command_head": (head[0] if head else "")[:80],
                "expect_s": expect_s,
                "elapsed_s": round(elapsed, 1),
                "state": job.get("state"),
                "detail": "Job on %s is past its expected ~%s — %s"
                % (
                    title,
                    _bg.format_duration(expect_s),
                    "still running" if job.get("state") == "running" else "still quiet",
                ),
            },
        )


def _session_last_turn_cached(inst) -> Optional[str]:
    """``_session_last_turn(inst)`` memoized per session (last-turn timestamp)."""
    return _probe_cached("last_turn", inst, lambda: _session_last_turn(inst))


def _session_last_say(inst) -> Optional[str]:
    """The session's spoken line (``core.spoken.spoken_for``: the reply's
    ``Say:`` line, else a digest), ≤ 320 chars, or None. One stat on the
    reply file; the provider's transcript reader behind the digest fallback
    is mtime-cached. Never raises."""
    try:
        return _spoken.spoken_for(getattr(inst, "Title", "") or "", inst).get("say")
    except Exception:  # noqa: BLE001 — the words are enrichment only
        return None


def _session_last_say_cached(inst) -> Optional[str]:
    """``_session_last_say(inst)`` memoized per session."""
    return _probe_cached("last_say", inst, lambda: _session_last_say(inst))


def _session_asking_cached(inst, activity: str) -> Optional[dict]:
    """The row's ``asking`` (core.asking.current: the dialog on ``clarify``,
    a prose question on ``idle``, else None), memoized per session. Cached
    reads only — the probe's own screen, the reply file, the provider's
    mtime-cached transcript — never a capture."""
    title = getattr(inst, "Title", "") or ""
    return _probe_cached("asking", inst, lambda: _asking.current(title, inst, activity))


def _voice_on(title: str) -> bool:
    """Whether voice mode is on for ``title`` (``core.voice``; one stat)."""
    try:
        return _voice.is_on(tmux.to_mindflock_tmux_name(title))
    except Exception:  # noqa: BLE001
        return False


def _session_last_prompt_cached(inst) -> Optional[str]:
    """``_session_last_prompt(inst)`` memoized per session."""
    return _probe_cached("last_prompt", inst, lambda: _session_last_prompt(inst))


def _session_last_prompt_full_cached(inst) -> Optional[str]:
    """``_session_last_prompt_full(inst)`` memoized per session."""
    return _probe_cached(
        "last_prompt_full", inst, lambda: _session_last_prompt_full(inst)
    )


def _session_task_headline_cached(
    inst, prompt_full: Optional[str], prompt: Optional[str]
) -> "_task_headlines.TaskHeadline":
    """The sidebar's task line (core.task_headlines): the newest MEANINGFUL
    prompt as a short verb phrase plus that prompt, memoized per session like
    the prompt it is read from."""
    return _probe_cached(
        "task_headline",
        inst,
        lambda: _task_headlines.for_session(
            inst.Title,
            prompt_full,
            prompt,
            getattr(inst, "Task", "") or None,
            _session_conversation(inst),
        ),
    )


# ---- /api/events state tick (F6) ------------------------------------------ #
# *_changed events used to exist only as a side effect of GET /api/instances,
# so a headless websocket consumer (no browser polling) saw nothing but
# lifecycle events. While at least one /api/events client is connected, this
# ticker recomputes status/activity/stage every ~4s and pushes diffs through
# the same snapshot. The always-on _instances_tick_loop (P1) now drives the
# same emissions regardless of clients; the shared _EVENT_SNAPSHOT dedupe plus
# the ~2.5s probe memo keep the two tickers from double-emitting or doubling
# the git/tmux probe cost.
_EVENTS_TICK_INTERVAL = 4.0
_EVENTS_WS_CLIENTS = 0
_EVENTS_TICK_TASK = None

# Identifies THIS server process instance. Sent in the /api/events hello frame
# so a client can detect a server restart — the event seq counter resets to 0 on
# restart, so a client reconnecting with a stale ``?since=<high seq>`` from the
# previous instance would be skipped past every fresh event (server: seq <=
# last_seq is treated as already-delivered) and go silent. On a boot_id change
# the client resets its cursor and reconnects clean.
_SERVER_BOOT_ID = f"{int(time.time())}-{os.getpid()}"


def _tick_state_changes() -> None:
    """One pass of the /api/instances state computation, minus the token
    telemetry (the tick only needs what feeds *_changed events)."""
    for i in list(ENGINE.instances.values()):
        try:
            status = getattr(i.Status, "name", str(i.Status)).lower()
            stage = _session_stage_cached(i).get("stage") or "agent"
            activity = _agent_activity_cached(i, i.Title)
            _emit_state_changes(i.Title, status, activity, stage)
        except Exception:  # noqa: BLE001 — one bad session can't stop the tick
            pass


async def _events_state_tick_loop() -> None:
    """Drive *_changed events while /api/events has clients; ends itself once
    the last client disconnects (restarted by the next connect)."""
    while _EVENTS_WS_CLIENTS > 0:
        try:
            await asyncio.to_thread(_tick_state_changes)
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(_EVENTS_TICK_INTERVAL)


def _ensure_state_ticker() -> None:
    """Start the state tick task if it isn't already running (called on the
    event loop by the websocket handler, so no thread races on the globals)."""
    global _EVENTS_TICK_TASK
    if _EVENTS_TICK_TASK is None or _EVENTS_TICK_TASK.done():
        _EVENTS_TICK_TASK = asyncio.create_task(_events_state_tick_loop())


# --- Window-refresh keepalive (roadmap E) ------------------------------------
# When enabled, sends a 1-token ping to a dedicated, connection-free (no-MCP)
# session per provider every N hours, to anchor that provider's rolling usage
# window on a schedule. Config + scheduling live in core.window_refresh; the
# tmux/launch machinery lives here.
_WINDOW_REFRESH_TICK = 60.0  # how often the loop checks whether a provider is due


def _window_session_name(program: str) -> str:
    """Deterministic tmux session name for ``program``'s window-refresh
    keepalive (``mf-window-<slug>``)."""
    slug = (
        re.sub(r"[^A-Za-z0-9_-]+", "-", program or "claude").strip("-")[:40] or "claude"
    )
    return "mf-window-" + slug


def _fire_window_refresh(program: str) -> bool:
    """Best-effort: ensure a minimal (no-MCP) session for ``program`` and send it
    a 1-token ping to anchor the usage window. Never raises."""
    prog = program or ENGINE.default_program()
    try:
        provider = providers.resolve(prog)
        name = _window_session_name(prog)
        scratch = os.path.join(config.GetConfigDir(), "window", name)
        os.makedirs(scratch, exist_ok=True)
        exists = (
            subprocess.run(
                ["tmux", "has-session", "-t=" + name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            ).returncode
            == 0
        )
        if not exists:
            cmd = provider.minimal_launch_command(scratch, name) or prog
            # An agent CLI like any other: the OOM killer's pick before us.
            cmd = providers.launch_script.oom_score_prefix() + cmd
            r = subprocess.run(
                ["tmux", "new-session", "-d", "-s", name, "-c", scratch, cmd],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if r.returncode != 0:
                if log.ErrorLog is not None:
                    log.ErrorLog.Printf(
                        "window-refresh: failed to start %s: %s",
                        name,
                        (r.stderr or r.stdout).strip(),
                    )
                return False
            time.sleep(4.0)  # let the CLI reach its prompt before we type
        # A single "." is a ~1-token nudge that anchors the window.
        ok = _send_to_agent(name, ".", submit=True)
        if ok and log.ErrorLog is not None:
            log.ErrorLog.Printf("[MONITORING] window-refresh pinged %s", prog)
        return ok
    except Exception as err:  # noqa: BLE001 — keepalive must never crash the loop
        if log.ErrorLog is not None:
            try:
                log.ErrorLog.Printf("window-refresh error for %s: %v", prog, err)
            except Exception:  # noqa: BLE001
                pass
        return False


async def _window_refresh_loop() -> None:
    """Fire scheduled window-refresh pings forever (started by the lifespan)."""
    while True:
        try:
            due = _window_refresh.due_providers(time.time())
            for prog in due:
                ok = await asyncio.to_thread(_fire_window_refresh, prog)
                if ok:
                    _window_refresh.record_fired(prog, time.time())
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(_WINDOW_REFRESH_TICK)


# The per-session budget guardrail + lock (J5) — _session_budget_usd /
# _window_budget_usd / _check_session_budget / _budget_overrides /
# _set_budget_override / _forget_budget / _effective_budget /
# _budget_status_for / _budget_locked — moved to core.budget (imported above).


def _inst_or_404(title: str):
    """``(inst, error_response_or_None)`` — the shared instance-not-found gate.

    Mirrors :func:`_wt_or_409`: look ``title`` up in ``ENGINE.instances`` and,
    when it is missing, hand back the standard 404 ``JSONResponse`` for the
    route to early-return, keeping the not-found message + status identical
    across every handler."""
    inst = ENGINE.instances.get(title)
    if inst is None:
        return None, JSONResponse(
            {"error": "instance not found: %s" % title}, status_code=404
        )
    return inst, None


@app.get("/api/instances/{title}/budget")
def get_budget(title: str) -> JSONResponse:
    """This session's cost-budget snapshot (cost / base / effective limit /
    raise expiry / locked flag) — what the pane's budget lock reads."""
    inst, err = _inst_or_404(title)
    if err is not None:
        return err
    try:
        cost = float(_session_tokens(inst).get("cost", 0.0) or 0.0)
    except Exception:  # noqa: BLE001
        cost = 0.0
    return JSONResponse(_budget_status_for(title, cost))


@app.post("/api/instances/{title}/budget/raise")
def raise_budget(title: str, payload: dict) -> JSONResponse:
    """Raise this session's budget. Body: ``{"limit": <usd>, "hours": <n>}`` —
    ``hours`` omitted / 0 / null means forever; otherwise the raise expires after
    that many hours (then the global default applies again)."""
    inst, err = _inst_or_404(title)
    if err is not None:
        return err
    payload = payload or {}
    try:
        limit = float(payload.get("limit") or 0.0)
    except (TypeError, ValueError):
        limit = 0.0
    if limit <= 0:
        return JSONResponse(
            {"error": "limit must be a positive dollar amount"}, status_code=400
        )
    hours_raw = payload.get("hours")
    try:
        hours = float(hours_raw) if hours_raw not in (None, "", 0, "0") else 0.0
    except (TypeError, ValueError):
        hours = 0.0
    expires = None if hours <= 0 else time.time() + hours * 3600.0
    _set_budget_override(title, limit, expires)
    try:
        cost = float(_session_tokens(inst).get("cost", 0.0) or 0.0)
    except Exception:  # noqa: BLE001
        cost = 0.0
    _events.BUS.emit(
        "session.budget_raised",
        session=title,
        data={"limit": limit, "expires": expires},
    )
    return JSONResponse(_budget_status_for(title, cost))


def _queue_summary(title: str, queues: dict) -> dict:
    """One session's prompt-queue summary (badge + drain state) — cheap."""
    q = queues.get(title)
    if not q:
        return {
            "pending": 0,
            "enabled": True,
            "loop": False,
            "wait_for_limit": True,
            "limited_until": 0.0,
        }
    return {
        "pending": len(q["items"]),
        "enabled": q["enabled"],
        "loop": q["loop"],
        "wait_for_limit": q["wait_for_limit"],
        # Epoch the queue is holding until (0 = not usage-limited). Lets
        # the pane's Queue tab show a live "resumes in MM:SS" badge.
        "limited_until": _session_limited_until(title),
    }


def _session_snapshot(i, queues: dict) -> dict:
    """One session's full /api/instances entry — runs every probe (git/tmux
    shell-outs plus a PR lookup when GitHub is reachable, transcript scans),
    each behind its ~2.5s memo."""
    d = _instance_json(i)
    # The publisher COMPUTES the stage and donates it to the memo; it must not
    # serve one the other 4s ticker filled up to 2.5s ago (see _probe_seed).
    d.update(_session_stage_fresh(i))
    # Mergeability, ONLY at the PR rung: it is two network round trips, and every
    # other stage is indifferent to it. None = "could not find out", which the UI
    # must treat as "leave the button alone", never as "cannot merge".
    d["merge_state"] = None
    if d.get("stage") == "pr":
        try:
            _wt_ms = i.GetWorktreePath()
            _br_ms = _current_branch(_wt_ms) if _wt_ms else ""
            if _br_ms:
                d["merge_state"] = _pr_merge_state(_wt_ms, _br_ms)
        except Exception:  # noqa: BLE001 — a probe never fails a poll
            d["merge_state"] = None
    d["queue"] = _queue_summary(i.Title, queues)
    # A dict read off an already-loaded store, so it is computed fresh on BOTH
    # snapshot paths and therefore never needs a _SNAPSHOT_PROBE_KEYS carry-over.
    d["autopilot"] = _autopilot_dto(i.Title)
    # A killed agent coming back by itself, or left Offline and why
    # (core.revive.auto_state) — a dict read, fresh on both snapshot paths.
    d["auto_revive"] = _revive.auto_state(i.Title)
    tok = _session_tokens(i)
    d["tokens"] = tok.get("out", 0)  # output tokens
    d["tokens_in"] = tok.get("in", 0)  # real input only (no cache)
    d["tokens_cache_read"] = tok.get("cache_read", 0)
    d["tokens_cache_write"] = tok.get("cache_write", 0)
    d["tokens_ctx"] = tok.get("ctx", 0)  # newest turn's context fill
    d["tokens_ctx_window"] = tok.get("ctx_window", 0)  # that model's window limit
    d["tokens_cost"] = tok.get("cost", 0.0)  # est. USD, priced from feed
    d["tokens_model"] = tok.get("model", "")
    # Per-session budget lock: when at/over the effective ceiling, the UI
    # blocks typing and offers a raise (temporary N hours / forever).
    d["budget"] = _budget_status_for(i.Title, d["tokens_cost"])
    d["activity"] = _agent_activity_cached(
        i, i.Title
    )  # working | clarify | limit | idle | offline
    # Epoch when the reported activity last changed, so the UI can rank how
    # long a session has been waiting (attention ordering + wedged-session
    # watchdog). 0 = unknown — a session with no live reading, e.g. offline.
    # This read used to name a key (``changed_epoch``) that nothing has ever
    # written, so it answered 0.0 for every session and the watchdog branch it
    # feeds never rendered once; ``agent_state.state_since`` is the real thing,
    # and every layer now writes it (see agent_state._verdict).
    d["activity_since"] = _agent_state.state_since(i.Title)
    # "It is asking you something" (core.asking): ``{kind: "dialog" |
    # "question", text, ts, dialog_id, parsed, source}`` or null — a dialog
    # while the activity reads clarify, a question the newest reply ended
    # on while it reads idle and nobody has answered.
    d["asking"] = _session_asking_cached(i, d["activity"])
    # Jobs the agent backgrounded and left running after its turn ended: why
    # an idle session is not finished yet (waiting), or what is wedged (stuck).
    d["background"] = _background_jobs(i, i.Title)
    # L3: latest-turn snippet (≤120 chars) for N-session triage, or null.
    d["last_turn"] = _session_last_turn_cached(i)
    # Voice: the line the phone speaks when this session's turn ends (the
    # reply's ``Say:`` line, else a digest; ≤320 chars, or null) and whether
    # the session is being talked to (its prompts carry the instruction).
    d["last_say"] = _session_last_say_cached(i)
    d["voice"] = _voice_on(i.Title)
    # The newest USER prompt (first line) — the panes pin it above the
    # terminal — and its whole body for the pin's hover/click expansion.
    d["last_prompt"] = _session_last_prompt_cached(i)
    d["last_prompt_full"] = _session_last_prompt_full_cached(i)
    # That prompt as a short verb phrase ("Fix pin bar below terminal") for
    # the sidebar's task line; "continue"/"yes"/MindFlock's own notes keep the
    # last real task (core.task_headlines). None when there is nothing.
    # task_headline_prompt is the prompt it was read from — the row's tooltip
    # (a later "continue" must not stand in for it).
    d["task_headline"], d["task_headline_prompt"] = _session_task_headline_cached(
        i, d["last_prompt_full"], d["last_prompt"]
    )
    # What it was asked to do when it was created (Instance.Task) — the
    # pane's "Task:" line until the transcript names a newer prompt, and the
    # only one for an agent whose transcript MindFlock can't read.
    d["task"] = getattr(i, "Task", "") or None
    # O2/O3/O4: worktree setup + verification-gate state and the port block.
    try:
        wt = i.GetWorktreePath()
    except Exception:  # noqa: BLE001
        wt = ""
    d["setup"] = _wt_setup.setup_summary(wt) if wt else None
    d["check"] = _wt_setup.check_summary(wt) if wt else None
    pb = _ports.get(i.Title)
    d["ports"] = {"base": pb, "count": _ports.BLOCK_SIZE} if pb else None
    return d


# Probe-derived entry fields that the cheap (no-shell-out) path may carry over
# stale from the last published snapshot; the 4s tick recomputes them.
_SNAPSHOT_PROBE_KEYS = (
    "diff_stat",
    "has_origin",
    "stage",
    "stage_reset",
    "pr_url",
    "failed_step",
    "failed_hook",
    "merge_state",
    "tokens",
    "tokens_in",
    "tokens_cache_read",
    "tokens_cache_write",
    "tokens_ctx",
    "tokens_ctx_window",
    "tokens_cost",
    "tokens_model",
    "budget",
    "activity",
    "activity_since",
    "asking",
    "background",
    "last_turn",
    "last_say",
    "last_prompt",
    "last_prompt_full",
    "task_headline",
    "task_headline_prompt",
    "setup",
    "check",
)


def _session_snapshot_cheap(i, queues: dict, prev: Optional[dict] = None) -> dict:
    """One session's /api/instances entry WITHOUT the expensive probes.

    Same key set as :func:`_session_snapshot`, but nothing here shells out to
    git/tmux (plus a PR lookup when GitHub is reachable) or scans transcripts:
    identity/status/queue/ports fields are
    computed fresh, and every probe-derived field is carried over from
    ``prev`` (this title's entry in the last published tick snapshot) when
    available — otherwise a null/zero placeholder the UI already renders (a
    paused session has ``diff_stat: null`` too). The always-on 4s tick fills
    in real values on the next poll; this keeps GET /api/instances instant on
    a cold boot instead of blocking seconds on the first full probe run."""
    d = _instance_json(i, cheap=True)
    d["stage"] = (
        "provisioning" if (not i.Started() and i.Status == Loading) else "agent"
    )
    d["stage_reset"] = False
    d["pr_url"] = None
    d["merge_state"] = None
    d["queue"] = _queue_summary(i.Title, queues)
    d["autopilot"] = _autopilot_dto(i.Title)
    d["auto_revive"] = _revive.auto_state(i.Title)
    d["tokens"] = d["tokens_in"] = 0
    d["tokens_cache_read"] = d["tokens_cache_write"] = 0
    d["tokens_ctx"] = d["tokens_ctx_window"] = 0
    d["tokens_cost"] = 0.0
    d["tokens_model"] = ""
    d["budget"] = _budget_status_for(i.Title, 0.0)
    d["activity"] = "idle"
    d["activity_since"] = _agent_state.state_since(i.Title)
    d["asking"] = None
    d["background"] = None
    d["last_turn"] = None
    d["last_say"] = None
    d["voice"] = _voice_on(i.Title)  # one stat: fresh on both paths
    d["last_prompt"] = None
    d["last_prompt_full"] = None
    d["task"] = getattr(i, "Task", "") or None
    # No transcript read on this path: the remembered headline, else the
    # creation task's (the tick fills in the real one; ``prev`` wins below).
    d["task_headline"], d["task_headline_prompt"] = _task_headlines.for_session(
        i.Title, None, None, d["task"]
    )
    d["setup"] = None
    d["check"] = None
    pb = _ports.get(i.Title)
    d["ports"] = {"base": pb, "count": _ports.BLOCK_SIZE} if pb else None
    if prev:
        for k in _SNAPSHOT_PROBE_KEYS:
            if k in prev:
                d[k] = prev[k]
    return d


# ---- Force-starts that have not registered a session yet ------------------- #
# The registry lives in core.pending: the ingestion addon reads it too (its
# provisioning_kinds() is what greens the sidebar bars for UI-started work), so
# it can't live in this module without an import cycle. These aliases keep the
# call sites below (and the tests) reading the same as before.
_pending_add = _pending.add
_pending_drop = _pending.drop
_pending_rows = _pending.rows
_pending_has = _pending.has
_cached_session_title = _pending.cached_session_title


def _build_instances_snapshot() -> list:
    """The full sessions snapshot — READ-ONLY (the tick's producer path).

    All side effects that historically piggybacked on this computation (budget
    crossings, auto check-run kicks, *_changed events, the addon snapshot) live
    in :func:`_instances_tick`, driven by the always-on background loop. The
    expensive per-session probes go through the ~2.5s memo, so N concurrent
    pollers cost one probe run (values may be ≤ ~2.5s stale — by design).

    Sessions are probed in PARALLEL (small thread pool): each probe shells out
    to git/tmux (plus a PR lookup when GitHub is reachable), so the sequential
    cost was the SUM over sessions (seconds
    on big worktrees); now it's the slowest single session. Concurrent probing
    was already the steady state (N pollers + the tick race the same memo), so
    the pool introduces no new concurrency class."""
    # Per-session prompt-queue summary in one read (badge + drain state).
    queues = _prompt_queue.snapshot()
    # Snapshot: this runs in a worker thread while the auto-adopt loop /
    # background Start tasks mutate ENGINE.instances on the event loop —
    # iterating the live dict races ("changed size during iteration").
    insts = list(ENGINE.instances.values())
    if len(insts) <= 1:
        rows = [_session_snapshot(i, queues) for i in insts]
    else:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(8, len(insts)), thread_name_prefix="session-probe"
        ) as ex:
            rows = list(ex.map(lambda i: _session_snapshot(i, queues), insts))
    # A copy window shows the lane of the window that drives its branch.
    _lanes.fill_duplicates(rows)
    return rows


def _fresh_lineage(rows: list) -> None:
    """Overwrite each cached row's ``parent`` with the live value, in place.

    Lineage is authority (the MCP's kill / answer / report routing all read it
    from this listing), and a re-parent changes no title, so the tick-snapshot
    fast path would otherwise serve the OLD parent for up to a tick — a
    just-detached former parent could still kill the session. It is a dict
    lookup per row, nothing like the probes the snapshot exists to avoid."""
    for d in rows:
        inst = ENGINE.instances.get(d.get("title"))
        if inst is not None:
            d["parent"] = _live_parent(inst)


@app.get("/api/instances")
def list_instances(request: Request) -> JSONResponse:
    """Hot path: polled every ~4s by every client. Read-only on purpose — the
    side effects it used to fire live in :func:`_instances_tick` (always-on).

    Serves the background tick's snapshot when it is fresh, so N polling
    clients cost ZERO extra per-session probes (the tick is the sole
    producer). When the snapshot is stale (tick not finished yet / wedged) or
    the title set changed (session created/deleted since the last tick), it
    NEVER rebuilds the expensive probes inline — a cold full build blocks
    seconds per big worktree, which made server boot look like a hang.
    Instead each session's cheap fields are computed fresh (title, status,
    queue, ports) and the probe-derived fields are carried over per title
    from the last published snapshot; a brand-new session still appears on
    the very next poll, with placeholder probe fields the ≤4s tick fills in.

    Connected tailnet devices' sessions ride along, title-namespaced as
    ``<device>::<title>`` (cached by :func:`_remote.instances_loop` — no
    network on this path). Another MindFlock asking (the remote header) gets
    this device's OWN sessions only: the rows it mirrors belong to their own
    devices, and handing them on is how two paired devices echoed each
    other's sessions back as ``a::b::title``."""
    remote = [] if _remote.from_remote(request) else _remote.merged_instances()
    cached = _events.sessions_snapshot()
    if time.time() - _SNAPSHOT_AT <= _INSTANCES_TICK_INTERVAL * 2.5:
        if {d.get("title") for d in cached} == set(ENGINE.instances.keys()):
            _fresh_lineage(cached)
            _ownership.overlay(cached)
            _aliases.overlay(cached)
            return JSONResponse(cached + remote + _pending_rows())
    queues = _prompt_queue.snapshot()
    by_title = {d.get("title"): d for d in cached}
    snap = [
        _session_snapshot_cheap(i, queues, by_title.get(i.Title))
        for i in list(ENGINE.instances.values())
    ]
    _lanes.fill_duplicates(snap)
    _ownership.overlay(snap)
    _aliases.overlay(snap)
    return JSONResponse(snap + remote + _pending_rows())


# ---- Tailnet multi-device control (backend.web.core.remote) -------------- #
@app.get("/api/remote/hello")
def remote_hello(request: Request) -> JSONResponse:
    """Public identity ping other MindFlock devices use for discovery.

    The desktop shell's own engine check sends ``X-MindFlock-Shell: <its
    version>`` from this machine; that is remembered and reported here as
    ``shell_version`` (see :func:`backend.web.core.remote.note_shell_version`)."""
    shell = request.headers.get(_remote.SHELL_HEADER)
    if shell and _auth._from_this_machine(request.scope):
        _remote.note_shell_version(shell)
    return JSONResponse(_remote.hello_json())


@app.get("/api/devices")
def list_devices() -> JSONResponse:
    """Discovered MindFlock devices on the tailnet (for the sidebar groups)."""
    return JSONResponse(_remote.devices_json())


@app.post("/api/devices/{device}/connect")
async def connect_device(device: str, payload: Optional[dict] = None) -> JSONResponse:
    """Pair with a device: validate its access token against it, persist it."""
    token = str((payload or {}).get("token", "") or "").strip()
    ok, err = await _remote.connect_device(device, token)
    if not ok:
        return JSONResponse({"error": err or "could not connect"}, status_code=400)
    return JSONResponse(_remote.devices_json())


@app.post("/api/devices/{device}/disconnect")
def disconnect_device(device: str) -> JSONResponse:
    """Forget a device's stored token (its sessions drop off the sidebar).

    409 for one of "Your devices": the fleet key keeps it connected whatever
    happens to a pasted token, so forgetting the token would say
    "disconnected" and change nothing — it is removed in Settings → Your devices."""
    dev = _remote._DEVICES.get(device)
    if dev is not None and _remote._member_device(dev):
        host = dev.get("host") or device
        return JSONResponse(
            {
                "error": "%s is one of your devices — remove it in Settings → Your devices"
                % host
            },
            status_code=409,
        )
    _remote.forget_device(device)
    return JSONResponse(_remote.devices_json())


@app.post("/api/devices/refresh")
async def refresh_devices() -> JSONResponse:
    """Sweep the tailnet now instead of on the next discovery tick (Settings →
    Devices' Refresh) and return the fresh ``GET /api/devices`` payload."""
    await _remote.discover_now()
    return JSONResponse(_remote.devices_json())


# ---- Always-on instances tick ---------------------------------------------- #
# These side effects used to run inside GET /api/instances, i.e. only while a
# browser was polling (and once PER CLIENT per poll). They now run here every
# _INSTANCES_TICK_INTERVAL regardless of connected clients — the same cadence
# the old single-client 4s poll gave them.
_INSTANCES_TICK_INTERVAL = 4.0
# Epoch of the last snapshot publish by _instances_tick — GET /api/instances
# serves the published snapshot only while this is fresh.
_SNAPSHOT_AT = 0.0


def _instances_tick() -> None:
    """One background pass: recompute the sessions snapshot and fire the side
    effects that used to piggyback on GET /api/instances."""
    out = _build_instances_snapshot()
    for d in out:
        title = d.get("title") or ""
        inst = ENGINE.instances.get(title)
        if inst is None:  # deleted while the snapshot was being built
            continue
        try:
            # J5: announce the first crossing of the per-session cost budget.
            _check_session_budget(title, d.get("tokens_cost") or 0.0)
            try:
                wt = inst.GetWorktreePath()
            except Exception:  # noqa: BLE001
                wt = ""
            # O3 self-driving gate: a repo that declares a check_command gets a
            # run kicked automatically for every fresh commit (no result — or a
            # stale one — for the current HEAD). Failed results match HEAD, so
            # they never re-trigger; is_running + the status file stop storms.
            if (
                wt
                and d.get("stage") in ("committed", "pushed")
                and (d["check"] is None or d["check"].get("stale"))
                and not _wt_setup.is_running(wt, "check")
            ):
                _cfg = _wt_setup.load_config(wt)
                if _cfg.check_command:
                    _wt_setup.start_check(title, wt, _cfg.check_command)
                    d["check"] = _wt_setup.check_summary(wt)
            # Before the emit: an agent the kernel just killed is queued to
            # come back by itself (core.revive), and its Offline goes
            # unannounced while it does.
            _revive.observe(sys.modules[__name__], title, inst, d["activity"])
            d["auto_revive"] = _revive.auto_state(title)
            _emit_state_changes(title, d["status"], d["activity"], d["stage"])
        except Exception:  # noqa: BLE001 — one bad session can't stop the tick
            pass
    # Titles are REUSED after a delete, so a "back to idle" pin left behind by a
    # deleted session could be inherited by its namesake — see stage_reset.prune.
    # One set difference over an almost-always-empty dict.
    try:
        _stage_reset.prune(list(ENGINE.instances.keys()))
    except Exception:  # noqa: BLE001 — housekeeping can't fail the tick
        pass
    # Same sweep for the per-title probe state. Dropping a record used to be a
    # side effect of the offline poll, and before that of `_forget_probes` on
    # every commit — both gone, deliberately, because both destroyed a live
    # session's history. Route-level teardown covers delete/close/cleanup, but
    # not every way a session leaves: a workspace deleted from Settings, or a
    # tombstone converged from another MindFlock on the same state file, pops
    # the instance without passing through a route here. One set difference per
    # tick over dicts that are normally the size of the flock.
    try:
        _prune_session_state(list(ENGINE.instances.keys()))
    except Exception:  # noqa: BLE001 — housekeeping can't fail the tick
        pass
    # And for parent links: every removal path orphans its children, but a
    # parent that never came back after a restart (seeded as dead) leaves a
    # link nobody removed. Lazy validity already hides it from the rows; this
    # erases it before a namesake can make it live again.
    try:
        _orphan_dangling_parents()
    except Exception:  # noqa: BLE001 — housekeeping can't fail the tick
        pass
    # Relaunch (at most) one agent the kernel killed, once memory allows — on
    # its own thread, so it never holds up this tick (core.revive.auto_tick).
    _revive.auto_tick(sys.modules[__name__])
    # Publish the freshly computed state so AppContext.sessions() (Addon API
    # v2) and GET /api/instances can serve it without recomputing it.
    _events.set_sessions_snapshot(out)
    global _SNAPSHOT_AT
    _SNAPSHOT_AT = time.time()


def _republish_session(title: str):
    """Recompute ONE session's row, publish it through, and emit its changes.

    The freshness escape hatch for the moments right after an action (commit,
    push, PR, merge), where waiting out the 4s tick is exactly the lag the
    guided workflow was criticised for. Bounded to a single worktree, so it
    never pays the whole flock's probe cost.

    Publish BEFORE emit is load-bearing: ``_instances_tick`` emits state changes
    and only then publishes, so a client that reacts to ``session.stage_changed``
    by re-reading races the publish and sees the PREVIOUS snapshot. Here the row
    is in place before anyone is told to look.

    Deliberately does NOT call :func:`_forget_probes` — it would drop the token
    memo for no benefit, since ``_session_stage_fresh`` already guarantees the
    stage is current. Do not re-add it. (Its original reason, that popping
    ``_ACTIVITY_CACHE`` destroys ``activity_since``, is now handled at the
    source: only :func:`_forget_session_state`, on the delete path, touches the
    rolling record at all.)

    Returns the fresh row, or None if the session is gone. Never raises.
    """
    try:
        inst = ENGINE.instances.get(title)
        if inst is None:
            return None
        d = _session_snapshot(inst, _prompt_queue.snapshot())
        _events.patch_session_snapshot(title, d)
        _emit_state_changes(title, d["status"], d["activity"], d["stage"])
        return d
    except Exception:  # noqa: BLE001 — a freshness nicety must never 500
        return None


async def _instances_tick_loop() -> None:
    """Drive :func:`_instances_tick` forever (started by the lifespan)."""
    while True:
        try:
            await asyncio.to_thread(_instances_tick)
        except Exception:  # noqa: BLE001 — the tick must never die
            pass
        await asyncio.sleep(_INSTANCES_TICK_INTERVAL)


# ---- Red-zone reconcile loop ------------------------------------------------ #
_RED_ZONE_INTERVAL = 4.0
#: None = automatic: on in a real server, OFF under pytest. The loop WRITES
#: into worktrees (it re-arms a disarmed hooks file) and ``ENGINE`` is the
#: developer's REAL state.json in a test run, so every ``with TestClient(...)``
#: would otherwise re-arm the owner's live sessions and point them at a tmp
#: guard dir. Tests that exercise the loop set this to True with an isolated
#: ``ENGINE.instances``.
_RED_ZONE_LOOP_ENABLED: Optional[bool] = None


# Ctrl+F ahead of time (core.pane_scroll_find): keep every indexed agent pane's
# index current, and build the first one for a pane nobody is using. None =
# on unless under pytest (ENGINE is the developer's real state there).
_FIND_INDEX_LOOP_ENABLED: Optional[bool] = None
_FIND_INDEX_INTERVAL = 1.5
_FIND_INDEX_QUIET_S = 20.0  # the pane's screen unchanged this long
_FIND_INDEX_HANDS_OFF_S = 30.0  # no human input into it this long


def _find_index_loop_enabled() -> bool:
    if _FIND_INDEX_LOOP_ENABLED is not None:
        return bool(_FIND_INDEX_LOOP_ENABLED)
    return not _restart._under_pytest()


def _find_index_tick() -> None:
    """One pass: observe() every scroll-mode agent pane (one capture each —
    folds new output into existing indexes), then build at most ONE missing
    index, only where it can't disturb anyone: the agent idle (never while
    it waits on a question — paging keys could move a dialog's selection),
    no input from a person for a while, the screen still, and at most one
    screen attached (the browser freezes its view on the notify event;
    other screens wouldn't)."""
    activity: Dict[str, str] = {}
    try:
        for row in _events.sessions_snapshot():
            if isinstance(row, dict) and row.get("title"):
                activity[row["title"]] = str(row.get("activity") or "")
    except Exception:  # noqa: BLE001
        pass
    now = time.time()
    pick = None
    for title in list(ENGINE.instances):
        name = _live_session_name(tmux.to_mindflock_tmux_name(title))
        if not name or _pane_find.find_mode(name) != "scroll":
            continue
        still = _pane_scroll_find.observe(name)
        if (
            pick is None
            and _pane_scroll_find.needs_index(name)
            and activity.get(title) == "idle"
            and now - _HUMAN_INPUT_AT.get(title, 0.0) > _FIND_INDEX_HANDS_OFF_S
            and still >= _FIND_INDEX_QUIET_S
        ):
            out = _pane_find._tmux(
                "display-message", "-p", "-t", name, "#{session_attached}"
            )
            if out.returncode == 0 and int(out.stdout.strip() or 0) <= 1:
                pick = (title, name)
    if pick is not None:
        title, name = pick
        _pane_scroll_find.background_index(
            name,
            lambda on: _events.BUS.emit(
                "pane.find_index", session=title, data={"active": bool(on)}
            ),
        )


async def _find_index_loop() -> None:
    """Drive :func:`_find_index_tick` forever (started by the lifespan)."""
    if not _find_index_loop_enabled():
        return
    while True:
        try:
            await asyncio.to_thread(_find_index_tick)
        except Exception:  # noqa: BLE001 — the loop must never die
            pass
        await asyncio.sleep(_FIND_INDEX_INTERVAL)


#: Test seam, like _RED_ZONE_LOOP_ENABLED: None = on except under pytest.
_TEAM_RUN_LOOP_ENABLED: Optional[bool] = None


def _team_run_loop_enabled() -> bool:
    if _TEAM_RUN_LOOP_ENABLED is not None:
        return bool(_TEAM_RUN_LOOP_ENABLED)
    return not _restart._under_pytest()


def _red_zone_loop_enabled() -> bool:
    if _RED_ZONE_LOOP_ENABLED is not None:
        return bool(_RED_ZONE_LOOP_ENABLED)
    return not _restart._under_pytest()


def _red_zone_tick() -> None:
    """One red-zone reconcile pass (see core.red_zone_monitor). Activity comes
    from the published sessions snapshot — an in-memory read, never a probe."""
    activity = {}
    try:
        for row in _events.sessions_snapshot():
            if isinstance(row, dict) and row.get("title"):
                activity[row["title"]] = str(row.get("activity") or "")
    except Exception:  # noqa: BLE001
        pass
    _red_zone_monitor.tick(dict(ENGINE.instances), activity)


async def _red_zone_loop() -> None:
    """Drive :func:`_red_zone_tick` forever (started by the lifespan)."""
    if not _red_zone_loop_enabled():
        return
    while True:
        try:
            await asyncio.to_thread(_red_zone_tick)
        except Exception:  # noqa: BLE001 — the loop must never die
            pass
        await asyncio.sleep(_RED_ZONE_INTERVAL)


@app.get("/api/mobile")
def get_mobile(request: Request) -> JSONResponse:
    """Mobile (/m) URLs + a scannable QR for phone access (Settings → Your phone).

    The QR's sign-in tokens (this device's own, the devices' key, every
    paired device's) and ``token`` go only to a caller that may see this
    device's own token (:func:`backend.web.core.auth.may_see_own_token`):
    anyone else — a member relaying, a phone signed in with the devices' key —
    gets the bare URLs, ``token: null``."""
    try:
        mine = _auth.may_see_own_token(request.scope)
        info = _mobile_info(include_tokens=mine)
        # The first-run checklist marks "Your phone" only when ITS "Show QR"
        # asked (?setup=1): Settings and the restart poll also read this route.
        if mine and info.get("qr_svg") and request.query_params.get("setup") == "1":
            _setup_steps.mark("phone")
        return JSONResponse(info)
    except Exception:  # noqa: BLE001 — never 500 the settings screen
        return JSONResponse(
            {"urls": [], "qr_svg": None, "token": "", "note": "unavailable"}
        )


@app.post("/api/mobile/shared/recheck")
def recheck_shared_link(request: Request) -> JSONResponse:
    """Settings → Your phone's Re-check: re-read the shared link's setup from
    Tailscale, re-apply what drifted (and re-advertise a tagged host that
    still isn't approved), then return the refreshed ``/api/mobile`` payload."""
    try:
        _shared_link.reconcile(_server_port(), nudge=True)
    except Exception:  # noqa: BLE001 — best-effort, like the module itself
        pass
    return get_mobile(request)


@app.post("/api/server/restart")
def post_server_restart(payload: Optional[dict] = None) -> JSONResponse:
    """Re-exec the server process so a changed serve mode (Settings → Your phone
    toggle) takes effect without the user finding the right terminal.

    Safe because the actual work lives outside this process: agent sessions
    are tmux sessions, ingestion is its own process, and state is on disk.
    Clients (desktop app, /m) already retry until the server answers again.

    By default the re-exec deliberately drops the mode from both places it
    could linger — ``CS_WEB_MODE`` (exported by run.py at boot) and any mode
    token in argv — so the fresh process falls through to the *persisted*
    general.serve_mode instead of resurrecting the mode this process happened
    to boot with. ``{"keep_mode": true}`` (``mindflock restart``, install.sh)
    comes back in the mode it runs in instead: that restart is about picking
    up a new engine, not about a changed setting.
    """
    keep_mode = bool((payload or {}).get("keep_mode"))
    # An explicit restart is a fresh intent: whatever the automatic
    # tailscale-mode retries (core.restart) already spent, this one starts over.
    _restart.reset_tailscale_attempts()
    if keep_mode:
        _restart.reexec_soon(keep_mode=True)
    else:
        _restart.reexec_soon()
    return JSONResponse({"ok": True, "restarting": True})


#: None = automatic: on in a real server, OFF under pytest (the startup sweep
#: launches agents for every session in ENGINE, the developer's real state).
_REVIVE_ON_START_ENABLED: Optional[bool] = None


def _revive_on_start_loop_enabled() -> bool:
    if _REVIVE_ON_START_ENABLED is not None:
        return bool(_REVIVE_ON_START_ENABLED)
    # MINDFLOCK_REVIVE_ON_START=0 forces it OFF at boot regardless of the
    # setting (sandboxed servers: the e2e journeys, a scratch `serve`).
    if os.environ.get("MINDFLOCK_REVIVE_ON_START", "1") == "0":
        return False
    return not _restart._under_pytest()


#: None = automatic: on in a real server, OFF under pytest (the sleep sweep
#: stops agents of sessions in ENGINE, the developer's real state).
_SLEEP_LOOP_ENABLED: Optional[bool] = None


def _sleep_loop_enabled() -> bool:
    if _SLEEP_LOOP_ENABLED is not None:
        return bool(_SLEEP_LOOP_ENABLED)
    # MINDFLOCK_SLEEP=0 forces it OFF at boot regardless of the setting
    # (sandboxed servers: the e2e journeys, a scratch `serve`).
    if os.environ.get("MINDFLOCK_SLEEP", "1") == "0":
        return False
    return not _restart._under_pytest()


@app.post("/api/server/revive-all")
async def post_server_revive_all(request: Request) -> JSONResponse:
    """Run the background-revive sweep now (:mod:`backend.web.core.revive`):
    relaunch every session whose agent should be running but isn't, two at a
    time, through the same path as ``/api/instances/{title}/revive``. Owner
    only (``auth.may_configure``, 403) — it launches agents. Waits for the
    sweep and returns ``{"revived": [...], "continued": [...], "skipped":
    {title: reason}, "already_running": n, "summary": "revived N, skipped M:
    ..."}``. Body ``{"titles": [...]}`` (the rail's "Restart all") sweeps
    just those sessions, including agents the user quit — every row it names
    showed a Restart chip — which the plain sweep leaves alone."""
    if not await _auth.may_configure(request.scope):
        return _auth.configure_refused()
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 — no body = the plain sweep
        payload = None
    only = None
    if isinstance(payload, dict) and payload.get("titles") is not None:
        titles = payload.get("titles")
        if not isinstance(titles, list) or not all(isinstance(t, str) for t in titles):
            return JSONResponse(
                {"error": "titles must be a list of session titles"}, status_code=400
            )
        only = titles
    return JSONResponse(await _revive.sweep(trigger="on demand", only=only))


@app.get("/api/system/memory")
def get_system_memory() -> JSONResponse:
    """The machine's memory headroom (:mod:`backend.memory_pressure`):
    ``{"level": "ok"|"low"|"critical"|"unknown", "available", "total",
    "available_pct", "swap_total", "swap_free", "psi", "at", "message"}``.
    While ``level`` is low or critical, agent-spawned sessions are refused,
    team runs start no new lines and the revive sweep waits; ``message`` is
    the one-line reason (empty when ok)."""
    from backend import memory_pressure as _mem

    snap = _mem.snapshot()
    snap["message"] = _mem.describe(snap)
    return JSONResponse(snap)


@app.get("/api/system/slots")
def get_system_slots() -> JSONResponse:
    """Heavy-job slots (``mindflock slot``, :mod:`backend.slot`): ``{"slots":
    N, "source": "env"|"setting"|"default", "pools": [{"name", "holders":
    [{"slot", "pid", "cmd", "cwd", "since", "max", "session"}], "waiters":
    [{..., "position"}]}], "running": n, "waiting": n}``. ``session`` is the
    MindFlock session a holder or waiter runs in ("" when none), matched by
    its tmux pane, then its working folder. Read from the lock dir: works
    for jobs started with no server, and is empty when nobody uses slots."""
    from backend import slot as _slot

    sessions = []
    for inst in list(ENGINE.instances.values()):
        try:
            folder = inst.GetWorktreePath() if inst.Started() else (inst.Path or "")
        except Exception:  # noqa: BLE001 — a half-built row maps by name only
            folder = getattr(inst, "Path", "") or ""
        sessions.append(
            {
                "title": inst.Title,
                "tmux_name": tmux.to_mindflock_tmux_name(inst.Title),
                "folder": folder,
            }
        )
    return JSONResponse(_slot.status(sessions))


#: How often the memory watch re-reads (backend.memory_pressure caches 2s).
_MEMORY_WATCH_SECONDS = 10.0


async def _memory_watch_loop() -> None:
    """Log and emit ``system.memory_changed`` when the memory level changes,
    so a user event hook (``~/.mindflock/hooks/system.memory_changed/``) can
    page someone before the OOM killer does. The UI polls the route."""
    from backend import memory_pressure as _mem

    last = "ok"
    while True:
        try:
            snap = await asyncio.to_thread(_mem.snapshot)
            lvl = str(snap.get("level") or "unknown")
            if lvl != "unknown" and lvl != last:
                msg = _mem.describe(snap) or "memory recovered"
                if log.InfoLog is not None:
                    log.InfoLog.Printf("memory: %s -> %s (%s)", last, lvl, msg)
                _events.BUS.emit(
                    "system.memory_changed",
                    old=last,
                    new=lvl,
                    data={"message": msg, "memory": snap},
                )
                last = lvl
        except Exception:  # noqa: BLE001 — a watch must never die
            pass
        await asyncio.sleep(_MEMORY_WATCH_SECONDS)


# --------------------------------------------------------------------------- #
# Engine updates (Settings → Advanced)
# --------------------------------------------------------------------------- #
# The desktop shell has its own updater (electron/main.js `engine:install`), and
# keeps it: it can install while nothing of its own is being replaced. These
# routes are for every OTHER client — a browser on the tailnet, /m, a second
# machine — which until now could be told it was behind and do nothing about it.
# See core.self_update for why the install is detached and its progress is a
# file rather than a variable in this process.


@app.get("/api/update/check")
async def get_update_check(refresh: int = 0) -> JSONResponse:
    """What is installed, what is released, and whether this install can update.

    Never 500s and never turns "couldn't reach GitHub" into "up to date": a
    failed lookup answers ``latest: null``, which the screen renders as exactly
    that. ``refresh=1`` bypasses the 15-minute cache behind the Check button.
    """
    current = _self_update.installed_version()
    try:
        release = await _self_update.latest_release(force=bool(refresh))
    except Exception:  # noqa: BLE001 — a version line never fails a screen
        release = None
    latest = (release or {}).get("version", "")
    blocked = _self_update.blocked_reason()
    kind = _self_update.install_kind()
    # Settles a dead installer as interrupted before the state is read.
    await asyncio.to_thread(_self_update.running)
    st = _self_update.read_state()
    tag = (release or {}).get("tag", "")
    return JSONResponse(
        {
            "current": current,
            "commit": _self_update.installed_commit(),
            "latest": latest,
            "tag": tag,
            "release_url": (release or {}).get("url", ""),
            "notes": (release or {}).get("notes", ""),
            "checked": release is not None,
            "available": _self_update.is_newer(latest, current),
            "kind": kind,
            # Where `kind` came from: install.sh's install.json, or the path
            # of the running package. A dev checkout always wins the path.
            "method_source": (
                "install.json"
                if kind != "editable"
                and _self_update.read_install_json().get("method") == kind
                else "detected"
            ),
            # For a development checkout: where it is and the exact commands
            # that update it — the screen shows them instead of a button.
            "checkout": _self_update.install_checkout(),
            "editable_commands": (
                _self_update.editable_update_commands(tag) if kind == "editable" else []
            ),
            "blocked": blocked,
            "repo": _self_update.UPDATE_REPO,
            "state": st.get("state", "idle"),
            # The install finished but this process isn't running it yet: the
            # screen says "Installed — restarting…", never "Update to vX" again.
            "restart_pending": _self_update.restart_pending(st),
            # The last update's outcome, for "was interrupted — Try again" and
            # "rolled back to vX (see log)".
            "last": {
                k: st[k]
                for k in ("ref", "version", "from_version", "error", "code", "healthy")
                if k in st
            },
        }
    )


@app.post("/api/update/start")
async def post_update_start(
    request: Request, payload: Optional[dict] = None
) -> JSONResponse:
    """Install the newest release (or an explicit ``ref``) and report back.

    The ref is resolved server-side by default rather than taken from the
    client: the button says "update to the newest version", and the newest
    version is not something a stale settings screen should get to decide.

    Only the owner may start one (:func:`backend.web.core.auth.privileged` —
    a credential, this machine, or a trusted tailnet account; never a request
    another MindFlock relays): with the gate off, any tailnet node could
    otherwise reinstall this engine. And an explicit ``ref`` from anywhere
    but this machine must be a published release tag at or above the running
    version (:func:`~backend.web.core.self_update.check_remote_ref`) — a
    branch or a downgrade is a developer's move, made at this keyboard.
    """
    try:
        allowed = bool(await _auth.privileged(request.scope))
    except Exception:  # noqa: BLE001 — fail closed
        allowed = False
    if not allowed:
        return JSONResponse(
            {
                "ok": False,
                "error": "updates can only be started by this device's owner",
            },
            status_code=403,
        )
    ref = str((payload or {}).get("ref", "") or "").strip()
    if ref and not _auth._from_this_machine(request.scope):
        reason, status = await _self_update.check_remote_ref(ref)
        if reason:
            return JSONResponse({"ok": False, "error": reason}, status_code=status)
    if not ref:
        try:
            release = await _self_update.latest_release()
        except Exception:  # noqa: BLE001
            release = None
        if not release:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "could not reach GitHub to find the newest release",
                },
                status_code=502,
            )
        ref = release["tag"]
    result = await asyncio.to_thread(_self_update.start_update, ref)
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return JSONResponse(result)


@app.get("/api/update/state")
def get_update_state() -> JSONResponse:
    """Progress of the running (or last) update, plus the restart it earns.

    The restart is triggered from HERE, on the first poll that sees a finished
    install, rather than by the installer itself — see core.self_update for why
    that is the cheaper half of the deal. ``restarting`` tells the UI to start
    waiting for the server to come back, which is the same wait the Restart
    button already does.
    """
    state, restart_now = _self_update.finish_state()
    if restart_now:
        _restart.reset_tailscale_attempts()
        # Same mode as now (see core.update_watch).
        _restart.reexec_soon(keep_mode=True)
    return JSONResponse({**state, "restarting": restart_now})


# --------------------------------------------------------------------------- #
# System logs (Settings → System logs)
# --------------------------------------------------------------------------- #


@app.get("/api/logs")
def get_logs(name: str = "server") -> JSONResponse:
    """Server (and, when present, ingestion) log tail for Settings → System logs.

    Returns the source list plus the tail of the selected log. Never 500s — the
    settings screen shows whatever it can."""
    try:
        sources = _log_sources()
        by_name = {s["name"]: s for s in sources}
        selected = name if name in by_name else sources[0]["name"]
        text, size, exists = "", 0, False
        chosen = by_name.get(selected)
        if chosen is not None:
            p = Path(chosen["path"])
            try:
                if p.exists():
                    exists = True
                    text, size = _read_log_tail(p)
            except OSError as err:
                text = "could not read log: %s" % err
        return JSONResponse(
            {
                "sources": sources,
                "selected": selected,
                "text": text,
                "size": size,
                "exists": exists,
                "truncated": size > _LOG_TAIL_MAX,
            }
        )
    except Exception:  # noqa: BLE001 — never break the settings screen
        return JSONResponse(
            {
                "sources": [],
                "selected": "",
                "text": "",
                "size": 0,
                "exists": False,
                "truncated": False,
            }
        )


@app.get("/api/usage")
def usage_windows() -> JSONResponse:
    """Rolling-window (24h/7d/30d/365d) token + cost totals across all sessions,
    plus how each active CLI's usage is PAID for and — on subscription plans —
    the state of its active window.

    Extra keys on top of the period totals:
      * ``providers``: one ``{name, label, mode, window, window_note}`` entry
        per provider shown in the cost panel. ``mode`` is ``"metered"`` (own
        API key — dollar estimates are real marginal spend) or ``"windowed"``
        (plan — lead with percent/reset). ``window`` is ``{anchor, end, cost,
        tokens, budget, percent_used, source, …}`` for the active window, or
        ``null`` when idle/metered. ``default`` names the default provider.
      * ``year_estimated_before``: ``"YYYY-MM-DD"`` when the year total includes
        approximate days before that date (also per provider as
        ``estimated_before``); absent when the year is exact.
    """
    try:
        from backend.providers import usage_history

        out: dict = dict(usage_history.windows())
        out["providers"] = []
        out["default"] = ""
        try:
            default_p = providers.resolve(ENGINE.default_program())
            out["default"] = default_p.name
            # The cost panel shows only the providers that opted into it (the
            # bundled default CLIs: claude/codex/antigravity/aider) — always,
            # running or not — so it isn't cluttered by every provider that
            # happens to back a session. Others opt in via usage_panel_visible
            # (user TOMLs: [usage] visible = true).
            order: dict = {}
            entries = []
            for p in providers.all_providers():
                try:
                    if not p.usage_panel_visible():
                        continue
                except Exception:  # noqa: BLE001
                    continue
                order[p.name] = len(order)
                entries.append(_provider_usage_entry(p))
            # Stable order: default first, then registry (registration) order —
            # claude, codex, antigravity, aider.
            entries.sort(
                key=lambda e: (e["name"] != default_p.name, order.get(e["name"], 99))
            )
            out["providers"] = entries
            # Top-level day/week/month/year is now the COMBINED total across
            # every reported provider (was Claude-only). Each provider's own
            # breakdown rides along in its `periods`; the Combined tab sums them.
            keys = ("in", "out", "cache_read", "cache_write", "cost")
            for period in ("day", "week", "month", "year"):
                combined = {k: 0 for k in keys}
                for e in entries:
                    pt = (e.get("periods") or {}).get(period) or {}
                    for k in keys:
                        combined[k] += pt.get(k, 0) or 0
                out[period] = combined
            # The year includes approximate days (an older ledger's figures)
            # before this date in at least one provider — the UI notes it.
            ests = sorted(
                e["estimated_before"] for e in entries if e.get("estimated_before")
            )
            if ests:
                out["year_estimated_before"] = ests[-1]
        except Exception:  # noqa: BLE001 — providers are enrichment only
            pass
        return JSONResponse(out)
    except Exception:  # noqa: BLE001 — history is optional; never 500 the UI
        return JSONResponse({})


def _ticketing_connected() -> bool:
    """True when at least one ticketing source is usable (token present, plus
    the project/scope for providers that need one) — mirrors the Connections
    addon's per-source CONNECTED logic without importing the addon."""
    try:
        from backend.config import settings as _settings

        for s in _settings.load_settings().ticketing.sources:
            provider = (s.provider or "").strip().lower()
            has_secret = bool(s.api_token) or provider == "github_issues"
            needs_scope = provider in ("github_issues", "asana")
            scope_ok = bool(s.project) if needs_scope else True
            if has_secret and scope_ok:
                return True
    except Exception:  # noqa: BLE001 — capability probes must never 500
        pass
    return False


_GITHUB_CAP_CACHE: list = [0.0, False]  # [fresh_until_epoch, value]
_GITHUB_CAP_TTL = 60.0


def _github_pr_available() -> bool:
    """Whether MindFlock can open/merge a PR itself right now (cached 60s).

    True when EITHER rung of the PR ladder is usable: an authenticated ``gh``,
    or a resolvable GitHub token for the REST path. False only means the user
    would land on the browser fallback (a prefilled compare page) — pushing is
    unaffected either way, since that is always plain ``git push``.

    Cached because the sibling probes are a PATH stat and this one is not:
    ``gh auth status`` is a subprocess, and /api/config is hit on every page
    load. 60s is short enough that installing gh or pasting a token shows up
    almost immediately, without a restart.
    """
    now = time.time()
    if _GITHUB_CAP_CACHE[0] > now:
        return bool(_GITHUB_CAP_CACHE[1])
    try:
        val = gh_available() or _github_pr.has_token_sync()
    except Exception:  # noqa: BLE001 — capability probes must never 500
        val = False
    _GITHUB_CAP_CACHE[0] = now + _GITHUB_CAP_TTL
    _GITHUB_CAP_CACHE[1] = val
    return val


def _capabilities() -> dict:
    """Which optional integrations are usable right now.

    Only the coding agent is required to run MindFlock; everything else is
    progressive: no git -> sessions run in-place and diff/commit/PR surfaces
    hide; no tailscale -> the Mobile screen explains how to get phone access;
    no ticketing source -> ingestion surfaces point at Intake → Tickets;
    no github -> Make PR / Merge hand the user a prefilled compare page
    instead of opening the PR themselves (they never fail outright).
    ``agent_mcp`` is the one non-boolean: see :func:`_agent_mcp_caps`.
    ``team_runs`` says which team-run shapes the routes accept yet
    (``{"split", "together"}``, from ``core.team_runs.CAPABILITIES``) and
    ``max_pieces``, the most pieces a split may have (MINDFLOCK_MAX_CHILDREN).
    Probed per-request (cheap) so installing/connecting takes effect on the
    next page load without a server restart.
    """
    return {
        "git": git_available(),
        # A CLI this machine can use (PATH, the macOS app bundle, or
        # $MINDFLOCK_TAILSCALE_BIN) — not Windows Tailscale seen from WSL.
        "tailscale": _tailscale_cli.available(),
        "ticketing": _ticketing_connected(),
        "github": _github_pr_available(),
        "agent_mcp": _agent_mcp_caps(),
        "team_runs": dict(
            _team_runs.CAPABILITIES, max_pieces=_team_run_driver._max_pieces()
        ),
        "orchestration": _orchestration_caps(),
    }


def _orchestration_caps() -> dict:
    """The spawn guard-rails as they apply right now, for Settings → Agent
    orchestration: each cap's effective value, built-in default and where it
    comes from (``env`` — an env var overrides the setting — ``settings`` or
    ``default``)."""
    out = {}
    for key, env, default in (
        ("max_children", _lineage.MAX_CHILDREN_ENV, _lineage.DEFAULT_MAX_CHILDREN),
        (
            "max_spawn_depth",
            _lineage.MAX_SPAWN_DEPTH_ENV,
            _lineage.DEFAULT_MAX_SPAWN_DEPTH,
        ),
        ("max_spawned", _lineage.MAX_SPAWNED_ENV, _lineage.DEFAULT_MAX_SPAWNED),
    ):
        out[key] = {
            "value": _lineage.limit(env, default),
            "default": default,
            "source": _lineage.source(env),
            "env": env,
        }
    return out


def _agent_mcp_caps() -> dict:
    """Whether new launches attach the MindFlock MCP, and to which CLIs.

    ``{"enabled": bool, "providers": [names]}`` — what the MCP's spawn tool
    reads to decide whether a child can report back (its report-back footer
    names an MCP tool, which only exists when the child's CLI is attached).
    Never raises: an unreadable state reports attach as off."""
    try:
        from backend.providers import mcp_attach as _mcp_attach

        return {
            "enabled": bool(_mcp_attach.enabled()),
            "providers": list(_mcp_attach.supported_providers()),
        }
    except Exception:  # noqa: BLE001 — a capability probe must never 500
        return {"enabled": False, "providers": []}


def _start_agent_override(payload: dict) -> str:
    """A validated per-start Agent CLI from a force-start body, or ``""``.

    Every Work row can start on a CLI other than the one its source / repo card
    is configured for — you notice mid-review that this one wants a different
    model, and re-configuring the whole queue to run one item is the wrong shape
    of action. ``""`` (the default) means "use the configured chain", so an old
    client that sends nothing behaves exactly as before.

    Raises :class:`ValueError` for a name no provider answers to, rather than
    silently launching the default: a typo that quietly ran the wrong CLI is
    worse than a rejected request.
    """
    name = str((payload or {}).get("agent", "") or "").strip()
    if not name:
        return ""
    # The same set /api/providers offers the picker, `generic` excluded: it is
    # the fallback for an arbitrary typed-in program, not a CLI anyone means to
    # choose, and advertising it in the error would send people to a dead end.
    known = {p.name for p in providers.all_providers() if p.name != "generic"}
    if name not in known:
        raise ValueError(
            "unknown agent %r — pick one of: %s" % (name, ", ".join(sorted(known)))
        )
    return name


def _start_depth_override(payload: dict) -> str:
    """A validated per-start autopilot depth from a force-start body, or ``""``.

    The intake twin of :func:`_start_agent_override`, and deliberately the same
    shape: ``""`` means "use the configured chain", so an old client that sends
    nothing behaves exactly as before, and an unknown rung is rejected rather
    than silently downgraded — an item that quietly stopped at the wrong rung is
    worse than a refused request.

    Unlike a per-SOURCE default, an individual item MAY choose ``merge``: the
    person picking it is looking at the one thing it will merge.
    """
    raw = str((payload or {}).get("depth", "") or "").strip()
    if not raw:
        return ""
    depth = _autopilot.normalize_depth(raw)
    if depth == "off":
        return "off"
    if depth not in _autopilot.DEPTHS:
        raise ValueError(
            "unknown depth %r — pick one of: %s" % (raw, ", ".join(_autopilot.DEPTHS))
        )
    return depth


def _start_effort_override(payload: dict) -> str:
    """A validated per-start thinking-effort rung from a force-start body, or ``""``.

    The third member of the per-item override family (:func:`_start_agent_override`,
    :func:`_start_depth_override`) and the same shape: ``""`` means "whatever the
    CLI does by default", so an old client that sends nothing behaves exactly as
    before, and a junk rung is refused rather than silently downgraded.

    The rungs are neutral; :mod:`backend.providers.effort` translates them into
    whichever CLI is about to run (and clamps a rung above that CLI's ceiling),
    which is why validation here is against the ladder and not against flags.
    """
    return _provider_effort.validate((payload or {}).get("effort", ""))


def _start_launch_args(program: str, effort: str):
    """Per-session launch args for an intake start, or ``None``.

    ``None`` = this start adds nothing, so the session inherits the global
    default launch flags exactly as it always has (``InstanceOptions.launch_args``
    treats an explicit value — even an empty one — as "use verbatim"). When an
    effort rung DOES contribute flags, the provider's configured defaults are
    folded back in first, so asking for more thinking never costs a session the
    flags the user set in Settings → Coding CLI.
    """
    args = _provider_effort.launch_args(program, effort)
    if not args:
        return None
    return _instance.merge_launch_args(
        _instance.provider_default_launch_args(program), args
    )


def _cap_source_depth(depth: str) -> str:
    """Clamp a per-SOURCE default to the rungs a source may choose.

    A source default applies to every future item with no human in the loop, so
    it may not be ``merge`` however the settings file was edited. An individual
    item can still choose it — the person picking it is looking at the one thing
    it will merge.
    """
    d = _autopilot.normalize_depth(depth)
    if d in ("", "off"):
        return ""
    return d if d in _autopilot.SOURCE_DEPTHS else "pr"


def _source_intake_depth(source_id: str) -> str:
    """The configured autopilot depth for a ticketing source, or ``""``."""
    try:
        from backend.config import settings as _settings

        sources = list(_settings.load_settings().ticketing.sources)
        # An exact source key first: two sources may share a provider, and a
        # provider match on the first must not answer for the second.
        for src in sources:
            if getattr(src, "id", "") == source_id:
                return _cap_source_depth(getattr(src, "depth", ""))
        for src in sources:
            if getattr(src, "provider", "") == source_id:
                return _cap_source_depth(getattr(src, "depth", ""))
    except Exception:  # noqa: BLE001
        pass
    return ""


def _repo_intake_depth(repo: str, kind: str) -> str:
    """The configured autopilot depth for a GitHub repo's PRs or issues.

    ``kind`` is ``"prs"`` or ``"issues"`` — the two have separate override maps
    because a repo whose PRs you want reviewed automatically is not necessarily
    one whose issues you want carried to a PR.
    """
    try:
        from backend.config import settings as _settings

        gh = _settings.load_settings().github
        table = gh.issue_repo_settings if kind == "issues" else gh.repo_settings
        block = (table or {}).get(repo) or {}
        return _cap_source_depth(block.get("depth", ""))
    except Exception:  # noqa: BLE001
        return ""


def _arm_intake_autopilot(
    title: str, depth: str, source: str, item: str, message: str = "", by: str = "user"
) -> None:
    """Arm the autopilot for a session an intake start is about to create.

    Called BEFORE the launch task, on purpose: the record is keyed by title, and
    the title an ingested item produces is deterministic, so arming first means
    the target survives a provisioning crash or a server restart mid-launch. A
    record whose session never appears is dropped by the driver's prune.
    """
    if not title or not depth or depth == "off":
        return
    try:
        _autopilot.arm(
            title,
            depth,
            source=source,
            item=item,
            message=message,
            # The intake message is the ITEM'S name ("Fix login redirect loop"),
            # which is the work as REQUESTED, not the change as MADE — and it is
            # identical for every commit a multi-commit run makes. So it is a
            # placeholder like the ⏩ button's: good enough to commit under if
            # nothing better arrives, and replaced at commit time by a message
            # written from the final diff (the ✨ button's generator).
            message_auto=bool(message),
            retryable=_precommit_retry_hooks(),
            boot=_SERVER_BOOT_ID,
            # An agent's spawn_ticket_session(autopilot=...) is the AGENT's
            # choice (it may change it again), never mistaken for the user's.
            by=by,
        )
    except Exception:  # noqa: BLE001 — never fail a launch over the autopilot
        pass


#: The longest orchestrator ``note`` an intake start accepts.
_INTAKE_NOTE_MAX = 4000


async def _intake_lineage(payload: dict):
    """The lineage an agent asks for when it starts an intake item as its own
    worker (the MindFlock MCP's ``spawn_ticket_session``) →
    ``(lineage, None)`` or ``(None, error_response)``.

    ``lineage`` is ``{"parent", "spawned", "report_back", "note"}``. A payload
    that names none of them gets the all-empty lineage, and the start behaves
    exactly as it always did — the Intake panel sends none of them.

    Validated the way ``POST /api/instances`` validates the same fields:
    ``spawned`` and ``report_back`` strictly boolean (``spawned`` unlocks
    agent-driven deletion, so a string "false" must not read as True),
    ``parent`` a live session that is not over budget, and the spawn limits
    checked NOW, so the caller gets its 409 naming the knob instead of a 202
    and a failed background start. They are checked once more under the
    registry lock when the launch claims the title
    (:func:`_intake_claim_error`)."""
    parent = str(payload.get("parent", "") or "").strip()
    spawned = payload.get("spawned", False)
    if spawned is None:
        spawned = False
    if not isinstance(spawned, bool):
        return None, JSONResponse(
            {"error": "spawned must be a boolean"}, status_code=400
        )
    report_back = payload.get("report_back", False)
    if report_back is None:
        report_back = False
    if not isinstance(report_back, bool):
        return None, JSONResponse(
            {"error": "report_back must be a boolean"}, status_code=400
        )
    note = payload.get("note", "") or ""
    if not isinstance(note, str):
        return None, JSONResponse({"error": "note must be a string"}, status_code=400)
    note = note.strip()
    if len(note) > _INTAKE_NOTE_MAX:
        return None, JSONResponse(
            {"error": "note is longer than %d characters" % _INTAKE_NOTE_MAX},
            status_code=400,
        )
    if parent:
        if parent not in ENGINE.instances:
            return None, JSONResponse(
                {"error": "unknown parent session: %s" % parent}, status_code=400
            )
        if await asyncio.to_thread(_budget_locked, parent):
            return None, JSONResponse(
                {
                    "error": "parent session %s is over budget — raise its budget "
                    "before it spawns more sessions" % parent,
                    "budget_locked": True,
                },
                status_code=409,
            )
    if parent or spawned:
        limit_err = _lineage.spawn_limit_error(ENGINE.instances, parent, spawned)
        if limit_err:
            return None, JSONResponse({"error": limit_err}, status_code=409)
    lineage = {
        "parent": parent,
        "spawned": spawned,
        "report_back": report_back,
        "note": note,
    }
    return lineage, None


def _intake_claim_error(lineage: dict) -> Optional[str]:
    """Why an intake start's background launch may not claim its title after
    all — its parent went away, or a concurrent spawn took the last slot — or
    None. Called with ``ENGINE.lock`` held, like the create route's re-check."""
    parent = lineage.get("parent") or ""
    spawned = bool(lineage.get("spawned"))
    if not parent and not spawned:
        return None
    if parent and parent not in ENGINE.instances:
        return "parent session %s is gone" % parent
    return _lineage.spawn_limit_error(ENGINE.instances, parent, spawned)


def _intake_prompt_tail(lineage: dict, title: str, program: str):
    """What an agent-spawned intake start appends to its prompt →
    ``(text, report_back, reason)``.

    The orchestrator's ``note`` (scoping the worker: "only the API half"), then
    the MCP's report-back footer — the same text ``spawn_session`` appends —
    when the caller asked for it, has a parent to report to, and the worker's
    CLI will actually get a ``report_result`` tool. ``reason`` says why there is
    no footer when one was asked for."""
    parent = lineage.get("parent") or ""
    text = ""
    if lineage.get("note"):
        text += "\n\n## Note from session %s\n\n%s" % (
            parent or "outside the flock",
            lineage["note"],
        )
    if not lineage.get("report_back"):
        return text, False, ""
    if not parent:
        return text, False, "no parent to report to"
    why = _mcp_unattachable(program)
    if why is not None:
        return text, False, why
    from backend.mcp.tools import report_footer

    try:
        provider = providers.resolve(program or "").name
    except Exception:  # noqa: BLE001
        provider = ""
    return text + report_footer(title, parent, provider), True, ""


def _no_git_response() -> JSONResponse:
    """The uniform 409 for git-only endpoints when git isn't installed."""
    return JSONResponse(
        {
            "error": "git is not installed on this machine — install git to use "
            "diffs, commits, branches and PRs (see Settings → Doctor)"
        },
        status_code=409,
    )


@app.get("/api/config")
def get_config() -> JSONResponse:
    """Client bootstrap config: default program, optional-integration
    capabilities, home dir, linked IDE name, first-run + auth-gate state."""
    return JSONResponse(
        {
            # Folded to the provider name when the stored value is a resolved
            # path to a known CLI: an existing config.toml carrying
            # "/opt/homebrew/bin/claude" (what a first run used to write) would
            # otherwise seed the New Session dialog with a program it doesn't
            # recognise, which it renders as an extra agent-dropdown entry.
            # Launch paths keep using the stored value — this is what the UI
            # shows and preselects.
            "default_program": providers.normalize_program(ENGINE.default_program()),
            "provisioning_available": provisioning.provisioning_available(),
            # Optional-integration availability (git / tailscale / ticketing):
            # the UI hides absent features and shows "connect X" guidance on
            # their settings screens instead.
            "caps": _capabilities(),
            "home": os.path.expanduser("~"),
            # Display name of the linked IDE (Settings → IDE) so the UI
            # can label its open/focus actions ("Cursor", "VS Code", …).
            "ide_name": ide_cfg.ide_name(),
            # First-run gate: the setup checklist auto-shows only until the user
            # has ever created a session (or finished setup). See _mark_onboarded.
            "onboarded": _is_onboarded(),
            # Auth gate state so Settings can show the current effective mode.
            "auth_mode": _auth.effective_mode(),
            "auth_enabled": _auth.auth_enabled(),
            # The resolved fast-track rung, so the ⏩ button can NAME where it will
            # stop. Display only — the server still decides the actual depth when a
            # request omits one, which is what keeps this setting authoritative
            # instead of shadowed by a sticky client value.
            "fasttrack_depth": _fasttrack_depth(),
            # THE default every fast-track picker starts on (the New dialog,
            # Start together): a rung, or "off" — which the resolved rung above
            # can never say.
            "fasttrack_default": _fasttrack_default(),
        }
    )


def _is_onboarded() -> bool:
    """True once the user has ever created a session / finished first-run setup."""
    try:
        from backend.config import settings as _settings

        if _settings.load_settings().general.onboarded:
            return True
    except Exception:  # noqa: BLE001
        pass
    # A returning user is obviously past first-run even if the flag predates this
    # field: they have live sessions, or a history of closed ones.
    if ENGINE.instances:
        return True
    try:
        return bool(_load_recently_closed())
    except Exception:  # noqa: BLE001
        return False


def _mark_onboarded() -> None:
    """The first-run checklist's "Pick a repo" step: done on the first session
    create (idempotent, best-effort). ``general.onboarded`` is derived from it
    by the settings layer — nothing writes that flag directly any more."""
    try:
        _setup_steps.mark("repo")
    except Exception:  # noqa: BLE001 — never block session creation on this
        pass


# --- First-run checklist (core/setup_steps.py) -------------------------------
def _doctor_payload_cached(refresh: bool = False) -> Optional[dict]:
    """The doctor addon's cached ``/api/doctor`` body (its 30 s cache; a
    ``refresh`` re-probes), or a direct probe when the addon isn't up."""
    try:
        for addon in ADDONS:
            if getattr(addon, "id", "") == "doctor":
                return addon._payload(refresh=refresh)
    except Exception:  # noqa: BLE001 — fall through to a direct probe
        pass
    try:
        from backend import doctor as _doctor

        return _doctor.to_payload(_doctor.run_checks())
    except Exception:  # noqa: BLE001 — the checklist works without a doctor
        return None


@app.get("/api/setup")
def get_setup(refresh: bool = False) -> JSONResponse:
    """The first-run checklist and everything the UI draws it from:
    ``{steps: [{id, title, required, group, done, at, skipped, status}], summary:
    {done, total, required_done, complete, next}, doctor: {ok, checks,
    install} | null, tailscale: {status, detail, fix, docs} | null, logins:
    [{name, installed, logged_in, login_cmd, default}], default_provider,
    report}``. ``report`` is the first-run text ``mindflock serve`` printed at
    boot (``""`` on a later boot). The doctor result is the cached one;
    ``?refresh=1`` re-probes (after an install or a sign-in). sync def: the
    probe shells out, so FastAPI runs it off the event loop."""
    return JSONResponse(_setup_steps.status(_doctor_payload_cached(refresh=refresh)))


@app.post("/api/setup/{step}/skip")
async def post_setup_skip(step: str, request: Request) -> JSONResponse:
    """Skip an OPTIONAL step (``github`` / ``tracker`` / ``phone``): ``{steps, summary}``
    after. 400 for a required or unknown step; owner only
    (``auth.may_configure``, 403)."""
    if not await _auth.may_configure(request.scope):
        return _auth.configure_refused()
    if step not in _setup_steps.OPTIONAL:
        return JSONResponse(
            {
                "error": "only an optional step can be skipped: %s"
                % ", ".join(_setup_steps.OPTIONAL)
            },
            status_code=400,
        )
    _setup_steps.skip(step)
    rows = _setup_steps.checklist()
    return JSONResponse({"steps": rows, "summary": _setup_steps.summary(rows)})


@app.post("/api/setup/{step}/unskip")
async def post_setup_unskip(step: str, request: Request) -> JSONResponse:
    """Undo a skip (the step shows as to-do again). Same rules as skip."""
    if not await _auth.may_configure(request.scope):
        return _auth.configure_refused()
    if step not in _setup_steps.OPTIONAL:
        return JSONResponse({"error": "not an optional step"}, status_code=400)
    _setup_steps.unskip(step)
    rows = _setup_steps.checklist()
    return JSONResponse({"steps": rows, "summary": _setup_steps.summary(rows)})


@app.post("/api/paste-image")
async def paste_image(request: Request) -> JSONResponse:
    """Save a file pasted or dropped in the browser so the session's CLI can read it.

    The agent CLIs run on this machine and cannot see the *browser's*
    clipboard or filesystem, so ctrl+V / drag-drop of an image or file in a
    terminal pane uploads the bytes here; the UI then pastes the returned
    absolute path into the PTY. ``?session=<title>`` drops the file inside
    that session's workspace (git-excluded via ``.mindflock_pastes/``) so the
    agent needs no out-of-tree read; otherwise it lands under
    ``~/.mindflock/pastes``. ``?name=<filename>`` (dropped files) keeps a
    sanitized copy of the original name so the agent sees "report.pdf", not
    an anonymous blob.
    """
    data = await request.body()
    if not data:
        return JSONResponse({"error": "empty body"}, status_code=400)
    if len(data) > 20 * 1024 * 1024:
        return JSONResponse({"error": "file too large (max 20MB)"}, status_code=413)
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    ext = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
    }.get(ctype, ".png" if ctype.startswith("image/") else ".bin")
    orig = _safe_upload_name(request.query_params.get("name") or "")
    title = request.query_params.get("session") or ""
    inst = ENGINE.instances.get(title) if title else None

    def _store() -> str:
        # Blocking work (git shell-out, up-to-20MB write, retention scan) —
        # runs in a thread so the event loop stays responsive.
        stamp = _datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        # Keep the original (sanitized) name visible so the agent knows what it
        # got; the paste-<stamp>-<hex> prefix keeps retention pruning working.
        tail = "-" + orig if orig else ext
        fname = "paste-%s-%s%s" % (stamp, os.urandom(3).hex(), tail)
        if inst is not None:
            try:
                folder = inst.GetWorktreePath() if inst.Started() else (inst.Path or "")
            except Exception:  # noqa: BLE001
                folder = inst.Path or ""
            if folder and os.path.isdir(folder):
                _exclude_artifacts(folder)  # keep pastes out of the agent's commits
                # Through O_NOFOLLOW dir fds: the workspace's agent (a shared
                # folder's is untrusted) may have made the paste dir a symlink.
                return _write_workspace_paste(folder, fname, data)
        base = os.path.join(os.path.expanduser("~"), ".mindflock", "pastes")
        os.makedirs(base, exist_ok=True)
        path = os.path.join(base, fname)
        with open(path, "wb") as f:
            f.write(data)
        # Retention: keep only the newest _PASTE_KEEP pastes in this directory.
        _prune_pastes(base)
        return path

    try:
        path = await asyncio.to_thread(_store)
    except OSError as err:
        return JSONResponse(
            {"error": "could not store the paste: %s" % (err.strerror or err)},
            status_code=409,
        )
    return JSONResponse({"path": path})


@app.post("/api/mkdir")
def mkdir(payload: dict) -> JSONResponse:
    """Create a new folder ``name`` under ``path`` and return its absolute path.

    ``name`` is a single path segment (no separators / ``..``) so the new folder
    always lands directly under the browsed directory.
    """
    payload = payload or {}
    parent = str(payload.get("path", "") or "").strip()
    name = str(payload.get("name", "") or "").strip()
    if not parent:
        return JSONResponse({"error": "a parent folder is required"}, status_code=400)
    if not name:
        return JSONResponse({"error": "a folder name is required"}, status_code=400)
    if "/" in name or "\\" in name or name in (".", ".."):
        return JSONResponse(
            {"error": "folder name must not contain a path"}, status_code=400
        )
    parent_abs = os.path.realpath(os.path.expanduser(parent))
    if not os.path.isdir(parent_abs):
        return JSONResponse(
            {"error": "not a directory: %s" % parent_abs}, status_code=400
        )
    target = os.path.join(parent_abs, name)
    if os.path.exists(target):
        return JSONResponse({"error": "already exists: %s" % target}, status_code=400)
    try:
        os.mkdir(target)
    except OSError as err:
        return JSONResponse(
            {"error": "could not create folder: %s" % err}, status_code=400
        )
    return JSONResponse({"path": target})


@app.get("/api/browse")
def browse(path: str = "") -> JSONResponse:
    """List immediate subdirectories of ``path`` (default: home) for the picker.

    Each entry flags whether it is itself a git repo. Dotfolders are hidden.
    """
    raw = path.strip() if path else ""
    base = os.path.realpath(os.path.expanduser(raw)) if raw else os.path.expanduser("~")
    base = os.path.realpath(base)
    if not os.path.isdir(base):
        return JSONResponse({"error": "not a directory: %s" % base}, status_code=400)
    entries = []
    try:
        for name in sorted(os.listdir(base), key=str.lower):
            if name.startswith("."):
                continue
            full = os.path.join(base, name)
            if not os.path.isdir(full):
                continue
            entries.append({"name": name, "path": full, "is_git": _is_git_repo(full)})
    except PermissionError:
        return JSONResponse({"error": "permission denied: %s" % base}, status_code=400)
    parent = os.path.dirname(base)
    return JSONResponse(
        {
            "path": base,
            "parent": parent if parent and parent != base else None,
            "is_git": _is_git_repo(base),
            "entries": entries,
        }
    )


def _recent_repo_paths() -> list:
    """Folders recent work happened in, newest first.

    Most-recent first: the folder the last session (or wizard run) used
    (``settings.general.last_repo_path``), then live sessions newest-touched
    first, then the closed-session undo store — which is already newest-first.
    Every tier is best-effort, so a broken one costs its own suggestions and not
    the whole list.

    Module-level rather than the closure it used to be inside
    :func:`repo_suggestions`, because ``POST /api/session-plan`` has to offer a
    model the *same* folders the dialog's own suggestion chips show. A second
    copy of this ladder is a second ladder that drifts, and it would drift
    silently: the plan route's note tells the user the folder was picked from a
    list, and only this function decides what is on it.

    Blocking (a settings read plus a state-store read), so call it off the loop.
    """

    def _touched_at(inst) -> float:
        """Sort key for live sessions: last-touched epoch, 0 when unknown."""
        try:
            return inst.UpdatedAt.timestamp()
        except Exception:  # noqa: BLE001 — an unset/odd timestamp just sorts last
            return 0.0

    recent: list = []
    try:
        from backend.config import settings as _settings

        recent.append(_settings.load_settings().general.last_repo_path)
    except Exception:  # noqa: BLE001 — no settings store yet is not an error
        pass
    try:
        for inst in sorted(
            list(ENGINE.instances.values()), key=_touched_at, reverse=True
        ):
            recent.append(getattr(inst, "Path", "") or "")
    except Exception:  # noqa: BLE001 — a registry mutating mid-read just skips it
        pass
    try:
        for closed in _load_recently_closed():
            if not isinstance(closed, dict):
                continue
            # The session's repo lives in the serialized instance data; the
            # top-level "folder" is its worktree, which is only the same
            # directory for an in-place session — hence the fallback order.
            data = closed.get("data")
            path = (data or {}).get("path") if isinstance(data, dict) else ""
            recent.append(str(path or closed.get("folder") or ""))
    except Exception:  # noqa: BLE001 — an unreadable history simply adds nothing
        pass
    return recent


@app.get("/api/repos/suggest")
async def repo_suggestions() -> JSONResponse:
    """Folders the New Session dialog can offer instead of a bare folder tree.

    The dialog used to open on HOME and leave a first-time user to Browse down
    to their own project, so this answers "which repo did you mean?" up front:
    the repos recent sessions ran in, the repo the server was launched from, then
    a shallow sweep of the usual code directories (see
    :mod:`backend.web.core.repo_picker`). ``home`` rides along because the picker
    renders paths relative to it and falls back to it when nothing is suggested.
    """

    def _gather() -> list:
        # Blocking work (a readdir per scan root plus a git probe per surviving
        # candidate, on top of the recency read) — threaded like paste_image's
        # _store so a cold disk or a stalled network mount can't hold up every
        # other request.
        return suggest_repos(recent_paths=_recent_repo_paths(), cwd=os.getcwd())

    return JSONResponse(
        {
            "suggestions": await asyncio.to_thread(_gather),
            "home": os.path.expanduser("~"),
        }
    )


@app.get("/api/repos/search")
async def repo_search(q: str = "", limit: int = 20) -> JSONResponse:
    """Find a folder by NAME, for the picker's third way into the folder field.

    :func:`repo_suggestions` guesses (depth-1, so it cannot see a repo nested
    under ``~/code/acme/services``), :func:`repo_check` reports on a path already
    typed in full, and Browse walks a tree one click at a time. None of those is
    "I know it's called ``api``, find it", which is what this answers — a bounded
    depth-3 walk of the usual code directories (see
    :func:`backend.web.core.repo_picker.search_repos`, which owns every budget
    and ranking decision).

    A query under two characters is not an error, just an empty ``matches``: the
    dialog calls this while the user types, the same way it calls
    ``/api/repos/check``, and a 4xx per keystroke lights the field up red at
    someone who is mid-word. ``truncated`` says the walk hit its cap, its 1.5s
    deadline, or ``limit`` — so the UI can offer "narrow the search / use
    Browse…" rather than implying the folder is not there. ``home`` rides along
    for the same reason ``/api/repos/suggest`` returns it: the picker renders
    paths relative to it. Threaded because the walk is thousands of stats plus a
    git probe per surviving match, and a cold mount must not stall the loop.
    """
    home = os.path.expanduser("~")
    # Clamp rather than reject: this is a convenience list, and the only harm a
    # silly limit does is to the scan budget, which the caller does not pay for.
    capped = max(1, min(int(limit or 0), 50))
    result = await asyncio.to_thread(search_repos, q, home, capped)
    return JSONResponse({**result, "home": home})


@app.get("/api/repos/check")
async def repo_check(path: str = "") -> JSONResponse:
    """Report what the folder the user typed into the picker actually is.

    Called on every keystroke, so a folder that does not exist yet answers 200
    with ``exists: false`` — a 4xx per character would light the dialog up red
    while someone is still typing ``/home/me/co``, and the folder may well be one
    MindFlock is about to create. A blank path is the one real error: there is
    nothing to answer about. Threaded for the same reason as
    :func:`repo_suggestions` — the git probes are subprocesses.
    """
    if not (path or "").strip():
        return JSONResponse({"error": "a path is required"}, status_code=400)
    return JSONResponse(await asyncio.to_thread(check_repo, path))


@app.get("/api/repos/branches")
async def repo_branches(path: str = "") -> JSONResponse:
    """The New-session dialog's Base branch menu for the folder in its Folder
    field: local branches (the checked-out one first) then remote-tracking
    refs, capped at 200 and memoised 10 s per folder. A folder that is not a
    git repo answers ``is_git: false`` with an empty list, never a 4xx — the
    picker simply has nothing to offer. See :mod:`backend.web.core.repo_branches`.
    """
    if not (path or "").strip():
        return JSONResponse({"error": "a path is required"}, status_code=400)
    # A paired device reaches this through the forward list: git runs only in
    # a folder this server already works in (home tree, recent repos, live
    # sessions, the workspaces dir), never in an arbitrary path a caller names.
    if not _branches_path_allowed(path):
        return JSONResponse(
            {
                "path": path,
                "is_git": False,
                "current": "",
                "default": "",
                "branches": [],
            }
        )
    return JSONResponse(await asyncio.to_thread(_list_branches, path))


def _branches_path_allowed(path: str) -> bool:
    full = os.path.realpath(os.path.expanduser(str(path or "")))
    roots = [os.path.realpath(os.path.expanduser("~"))]
    try:
        roots.extend(os.path.realpath(p) for p in _recent_repo_paths() if p)
    except Exception:  # noqa: BLE001
        pass
    try:
        with ENGINE.lock:
            roots.extend(
                os.path.realpath(getattr(i, "Path", "") or "")
                for i in ENGINE.instances.values()
                if getattr(i, "Path", "")
            )
    except Exception:  # noqa: BLE001
        pass
    for root in roots:
        if root and (full == root or full.startswith(root.rstrip(os.sep) + os.sep)):
            return True
    return False


@app.post("/api/session-plan")
async def session_plan(payload: dict) -> JSONResponse:
    """Fill in the New Session form from one sentence. Creates nothing.

    The model's whole job is to fill the form the user is already looking at: it
    picks a folder BY NUMBER from a menu this route built by walking the
    filesystem, writes a name and a first prompt, and says worktree or not. It
    never writes a path, and this route would not accept one if it did — a bare
    name reaching ``POST /api/instances`` is realpath'd against the SERVER's cwd
    and then ``makedirs``'d (``plain_repo.py:41,70``), and the only guard against
    that lives in TypeScript (``isNameQuery``, which exists because typing ``api``
    once created a ``MindFlock/api`` directory). Answering with a path we chose
    from a list we built ourselves means that guard can never even fire.

    502 with a sentence when there is no model to ask, or when the answer can't
    be read: the form is still there, still empty, still usable, and the person
    has lost one keystroke. That is the no-model fallback this whole layer
    contracts for — here it is literally the surface the box sits on.

    The folder menu comes from :func:`_recent_repo_paths` so the model chooses
    from the same rows the dialog's own suggestion chips show; the note the
    answer carries is composed in :mod:`backend.web.core.session_plan` from
    resolved facts, never by the model, because a model-written note is a
    sentence that can disagree with the form printed under it.
    """
    raw = str((payload or {}).get("text") or "")
    # Contract tokens go BY LINE, not by substring: substring-stripping silently
    # corrupts ordinary prose ("the code that handles <commit> hooks"), and a
    # sentence that is nothing but a format instruction has to be refused out
    # loud rather than sent on as an empty request.
    text = _session_plan.strip_contract_lines(raw).strip()[: _session_plan.MAX_SENTENCE]
    if not raw.strip():
        return JSONResponse({"error": "say what you want to work on"}, status_code=400)
    if not text:
        return JSONResponse(
            {
                "error": "that reads like an answer format rather than a request — "
                "say what you want to work on"
            },
            status_code=400,
        )
    home = os.path.expanduser("~")

    def _work() -> dict:
        return _session_plan.plan(
            text,
            # The flock's own default CLI, read fresh per request rather than
            # captured once: this is the only program the plan has to go on (no
            # session exists yet to inherit one from), and it goes into
            # pick_argv's FIRST slot. Passing "" there would be a silent no-op —
            # providers.resolve("") always answers claude — which is how a
            # codex-only machine would get "claude is not installed" forever,
            # naming a CLI its owner never chose.
            program=ENGINE.default_program(),
            recent_paths=_recent_repo_paths(),
            cwd=os.getcwd(),
            home=home,
            git_ok=git_available(),
        )

    try:
        # Threaded for both halves: the candidate menu walks the filesystem (up
        # to three search_repos calls, each capped at 1.5s) and the one-shot
        # blocks for the whole model turn — the same posture the ✨ commit-message
        # route uses. A client that aborts just stops waiting; the subprocess
        # stays bounded by session_plan.TIMEOUT_PLAN either way.
        return JSONResponse(await asyncio.to_thread(_work))
    except _session_plan.SessionPlanError as err:
        return JSONResponse({"error": str(err)}, status_code=502)
    except Exception as err:  # noqa: BLE001 — never a 500 for a convenience
        return JSONResponse({"error": str(err)}, status_code=502)


# There is deliberately no endpoint for the frontend to set general.onboarded
# or to mark a checklist step DONE: every step is marked by the backend hook
# that observes the thing itself (the doctor, the first session create, the
# first ticket source, a phone QR fetch — see core/setup_steps.py). The one
# write the UI may ask for is a SKIP of an optional step (POST /api/setup/
# <step>/skip, below) — the web UI's "Set up now" buttons used to mark
# onboarded for anyone who merely clicked past the welcome tour.


def _profile_id_error(profile_id: str) -> str:
    """Validate a session's auth-profile pin: ``""`` (inherit) and
    ``"default"`` (explicitly none) are always fine; anything else must name a
    configured profile. Returns an error message, or ``""`` when valid."""
    from backend.providers import auth_profiles

    if not profile_id or profile_id == auth_profiles.AMBIENT_ID:
        return ""
    if auth_profiles.get_profile(profile_id) is None:
        return (
            "unknown account '%s' — configure it under Settings → Accounts first"
            % profile_id
        )
    return ""


def _rewrite_launcher_for_profile(inst, wt: str) -> bool:
    """Rewrite a provisioned session's launcher so a profile swap also updates
    the profile's baked-in launch FLAGS (e.g. an OpenRouter model pin). See
    :func:`_rewrite_provisioned_launcher`."""
    return _rewrite_provisioned_launcher(inst, wt)


def _rewrite_provisioned_launcher(inst, wt: str, title: str = "") -> bool:
    """Rewrite a provisioned session's launcher from the session's current
    state: the MindFlock MCP attach args (for ``title``, default
    ``inst.Title``), the auth profile's flags, then the session's own launch
    args — the order the engine's first write uses. A profile swap needs it for
    the profile flags; a relaunch needs it when the baked MCP attach went stale
    (a reopen under a de-duplicated title, the attach toggle turned off).

    Only when the worktree carries its own ``_provision_settings`` — the exact
    ``skip_permissions``/cache-env the original write used. Restored sessions
    re-attach them too (``_worktree_from_data`` → ``settings_for_workspace``),
    so the no-settings path is rare: the provisioning config genuinely gone.
    Without them those values are NOT guessable (the configured-repo flavor
    resolves the flag from user settings, the local flavor pins it True), and
    a rewrite that guesses could silently hand a session
    ``--dangerously-skip-permissions`` its owner turned off. No settings, no
    rewrite: the env half of the swap still lands via the relaunch exports,
    only flag-level model routing stays stale. Returns whether the launcher
    was rewritten. Best-effort: any failure is swallowed — flags are
    secondary to env.
    """
    if not getattr(inst, "Provisioned", False):
        return False
    if not os.path.isfile(os.path.join(wt, provisioning.LAUNCHER_BASENAME)):
        return False
    scs = getattr(getattr(inst, "_git_worktree", None), "_provision_settings", None)
    if scs is None:
        return False
    try:
        from backend import workspace_setup as _ws

        prompt_path = os.path.join(wt, provisioning.PROMPT_BASENAME)
        prompt = ""
        if os.path.isfile(prompt_path):
            with open(prompt_path, encoding="utf-8") as f:
                prompt = f.read()
        _, prof_args = providers.launch_script.profile_overlay(
            inst.Program or "",
            getattr(inst, "ProfileId", "") or "",
            getattr(inst, "ProfileModel", "") or "",
        )
        name_title = title or getattr(inst, "Title", "") or ""
        # Without these the rewritten launcher silently dropped the MCP for the
        # rest of the session's life (report_result gone, the parent's wait
        # only ever seeing idle). () when off / unsupported / no title.
        mcp_args = (
            providers.mcp_attach.attach_args(
                providers.resolve(inst.Program or ""),
                title=name_title,
                tmux_name=tmux.to_mindflock_tmux_name(name_title),
                workdir=wt,
            )
            if name_title
            else ()
        )
        provisioning.write_launcher(
            wt,
            prompt,
            program=inst.Program or "claude",
            skip_permissions=scs.skip_permissions,
            cache_env=_ws.merged_cache_env(scs.caches),
            launch_args=tuple(mcp_args)
            + tuple(prof_args)
            + tuple(getattr(inst, "LaunchArgs", ()) or ()),
        )
        return True
    except Exception:  # noqa: BLE001 — flags are secondary to env
        return False


def _session_overlay_env(inst) -> dict:
    """The env a session's tmux needs in order to come back up as ITSELF.

    ``ExtraEnv`` is deliberately not persisted — it is re-derived from settings
    on every start — so any path that hands tmux a *fresh* env dict has to
    rebuild these or the session quietly returns on the CLI's ambient login
    (auth profile) and its hosted API (local models). Local wins on a key
    collision, exactly as it does at launch.
    """
    env: dict = {}
    program = getattr(inst, "Program", "") or ""
    try:
        prof_env, _ = providers.launch_script.profile_overlay(
            program,
            getattr(inst, "ProfileId", "") or "",
            getattr(inst, "ProfileModel", "") or "",
        )
        env.update(prof_env)
    except Exception:  # noqa: BLE001 — never block a resume over settings
        pass
    try:
        local_env, _ = providers.launch_script.local_overlay(program)
        env.update(local_env)
    except Exception:  # noqa: BLE001
        pass
    return env


def _profile_model_error(model: str) -> str:
    """Validate a per-session model override. Model ids are free-form (each
    gateway names its own), so only shell-hostile shapes are rejected — the
    value ends up in an env var / launch flag."""
    if not model:
        return ""
    if len(model) > 200 or "\n" in model or "\x00" in model:
        return "profile_model must be a single line under 200 chars"
    return ""


def _repo_url_workdirs(repo_url: str) -> list:
    """Local checkouts that share ``repo_url``'s repo identity, for decorating
    a provisioned start's prompt BEFORE its worktree exists — the shared
    :func:`provisioning.launch_workdirs`, which the ingestion pipeline's
    SessionRunner uses too, so a ticket gets the same decoration whichever
    path launches it. Never raises."""
    return provisioning.launch_workdirs(repo_url)


def _red_zone_prompt(
    prompt: str, program: str, workdirs, plan_first: Optional[bool]
) -> str:
    """``red_zones.decorate_prompt`` against the first existing dir in
    ``workdirs`` (``""`` when none — plan-first can still apply, zones can't).
    ``plan_first=None`` lets the repo's Plan-first flag decide (intake
    starts); True/False is the New Session checkbox. ``hard_guard`` is the
    provider's: a detect-only CLI is told its zones are flagged, not blocked.

    Plan-first applies only to a provider with ``plan_supported()``: the
    instruction ends "then wait for my go-ahead", and the Go button that
    gives it lives in the Map's Plan section, which exists for those CLIs
    only — any other agent would sit waiting for a button nobody can press.
    Idempotent and best-effort — decoration never fails a start. Blocking."""
    if not prompt:
        return prompt
    try:
        wd = next((d for d in workdirs or () if d and os.path.isdir(d)), "")
        prov = providers.resolve(program)
        hard = bool(prov.red_zone_guard())
        if not _provider_plans(prov):
            plan_first = False
        return _red_zones.decorate_prompt(
            prompt, wd, hard_guard=hard, plan_first=plan_first
        )
    except Exception:  # noqa: BLE001
        return prompt


def _provider_plans(prov) -> bool:
    """``prov.plan_supported()``, False on any doubt."""
    try:
        return bool(prov.plan_supported())
    except Exception:  # noqa: BLE001
        return False


@app.post("/api/instances")
async def create_instance(payload: dict, request: Request) -> JSONResponse:
    """Create a session and Start it in the background (returns 202 immediately).

    The whole of it lives in :func:`core.session_create.create` (accepted keys,
    the three creation modes, lineage and spawn limits, the claim under the
    registry lock) so a team run creates its sessions through the same code.
    The session records the person the request is for (core.person_sessions).
    """
    return await _session_create.create(payload, person=_auth.request_person(request))


@app.get("/api/create_failures")
def list_create_failures(title: str = "") -> JSONResponse:
    """Why recent background creates failed: ``{"failures": {title: {"error",
    "ts"}}}`` for the last ten minutes, or just ``title``'s entry when given
    (``{"failures": {}}`` when there is none). Lets a poller that saw a
    Loading row vanish say WHY instead of "it disappeared"."""
    now = time.time()
    with _CREATE_FAILURES_LOCK:
        items = {
            t: dict(rec)
            for t, rec in _CREATE_FAILURES.items()
            if now - rec.get("ts", 0.0) <= _CREATE_FAILURE_TTL_S
            and (not title or t == title)
        }
    return JSONResponse({"failures": items})


@app.get("/api/aliases")
def get_aliases() -> JSONResponse:
    """The synced session display aliases (title -> label). See core.aliases:
    the browser owns renames; this mirror only feeds server-originated text
    (ntfy pushes) so a phone notification names the tab the way the sidebar
    does."""
    return JSONResponse({"aliases": _aliases.all_aliases()})


@app.post("/api/aliases")
def post_aliases(payload: dict) -> JSONResponse:
    """Record renames. Two shapes:

    * ``{"title": ..., "alias": ...}`` — one rename delta (empty or missing
      ``alias`` clears it). The per-rename path: deltas, not whole maps, so
      browsers with different localStorage don't clobber each other.
    * ``{"aliases": {title: label, ...}}`` — the SPA's boot-time seed, folded
      in merge-only (set/overwrite, never delete): it exists for renames made
      before the server mirror did, and must not erase another browser's.
    """
    payload = payload or {}
    if isinstance(payload.get("aliases"), dict):
        _aliases.merge(
            {
                t: a
                for t, a in payload["aliases"].items()
                if not _peer_guard.is_peer_session(ENGINE.instances.get(t))
            }
        )
        return JSONResponse({"aliases": _aliases.all_aliases()})
    title = str(payload.get("title") or "").strip()
    if not title:
        return JSONResponse({"error": "title is required"}, status_code=400)
    peer_err = _peer_guard.title_refusal(title, "renaming")
    if peer_err is not None:
        return peer_err
    alias = str(payload.get("alias") or "").strip()
    taken = _aliases.owner_of(alias, _session_branches(), exclude=title)
    if taken:
        return JSONResponse(
            {
                "error": "another session is already named “%s”" % alias,
                "session": taken,
            },
            status_code=409,
        )
    _aliases.set_alias(title, alias)
    return JSONResponse({"aliases": _aliases.all_aliases()})


def _session_branches() -> Dict[str, str]:
    """``title -> branch`` of every live session: what
    :func:`core.aliases.owner_of` needs to know which names are taken."""
    return {
        t: str(getattr(i, "Branch", "") or "")
        for t, i in list(ENGINE.instances.items())
    }


@app.delete("/api/instances/{title}")
async def delete_instance(title: str) -> JSONResponse:
    """Kill the session and remove it (worktree + branch via ``inst.Kill``).

    One of the four doors through :func:`core.session_end.end_session`
    (``wipe=True``): the session is recorded in Recently closed, its children
    move up to its own parent and are reported, and the per-session state
    (event snapshot, budget, probes, branch baseline, port block, red zones)
    is GC'd. **409** ``{"error": "folder in use", "sessions": [...]}`` — and
    nothing changes — when another live session (a copy made by Duplicate
    session) works in the same folder. → ``{ok, title, wiped, children}``."""
    inst, err = _inst_or_404(title)
    if err is not None:
        return err
    return await _end_session_response(title, wipe=True)


async def _end_session_response(title: str, *, wipe: bool, scrub: bool = False):
    """Run :func:`core.session_end.end_session` off the event loop and shape
    its answer for the DELETE / /close / /cleanup routes."""
    try:
        result = await asyncio.to_thread(
            _session_end.end_session, ENGINE, title, wipe=wipe, scrub=scrub
        )
    except _session_end.FolderInUse as err:
        return JSONResponse(err.to_json(), status_code=409)
    except _session_end.SessionNotFound:
        return JSONResponse(
            {"error": "instance not found: %s" % title}, status_code=404
        )
    return JSONResponse({"ok": True, **result})


#: The summary diff's shape must not depend on the user's gitconfig: per-file
#: paths are parsed out of it (the MCP's get_diff ``files=[...]``), and
#: ``diff.noprefix`` / ``diff.mnemonicPrefix`` / C-quoted non-ASCII paths / an
#: external diff tool / colour each broke that parse.
_DIFF_STABLE_CONFIG = (
    "-c",
    "core.quotePath=false",
    "-c",
    "diff.noprefix=false",
    "-c",
    "diff.mnemonicPrefix=false",
)
_DIFF_STABLE_FLAGS = (
    "--no-ext-diff",
    "--no-color",
    "--src-prefix=a/",
    "--dst-prefix=b/",
)


@app.get("/api/instances/{title}/diff")
async def instance_diff(title: str, base: str = "fork") -> JSONResponse:
    """Diff for the pane's Diff tab.

    ``base=fork`` (default): everything the session has produced vs its fork
    point from the base branch — committed + uncommitted, the same baseline as
    the header's ``diff_stat`` badge, so the tab always matches the number the
    user clicked. ``base=head``: only working-tree changes since the last
    commit. Both stage untracked files with ``add -N`` so new files show.
    """
    if not git_available():
        return _no_git_response()
    inst, err = _inst_or_404(title)
    if err is not None:
        return err
    mode = "head" if base == "head" else "fork"
    if not inst.Started():
        return JSONResponse(
            {"added": 0, "removed": 0, "content": "", "error": None, "base": mode}
        )

    def _compute() -> dict:
        if mode == "head":
            wt = inst.GetGitWorktree()
            stats = wt.Diff()
            err = getattr(stats, "Error", None)
            return {
                "added": stats.Added,
                "removed": stats.Removed,
                "content": stats.Content,
                "error": str(err) if err else None,
                "base": mode,
            }
        wtp = inst.GetWorktreePath()
        subprocess.run(
            ["git", "-C", wtp, "add", "-N", "."],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
        cp = subprocess.run(
            [
                "git",
                "-C",
                wtp,
                *_DIFF_STABLE_CONFIG,
                "--no-pager",
                "diff",
                *_DIFF_STABLE_FLAGS,
                _session_fork_point(inst, wtp),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
        if cp.returncode != 0:
            err = cp.stderr.decode("utf-8", "replace").strip() or "git diff failed"
            return {"added": 0, "removed": 0, "content": "", "error": err, "base": mode}
        content = cp.stdout.decode("utf-8", "replace")
        added = removed = 0
        for line in content.split("\n"):
            if line.startswith("+") and not line.startswith("+++"):
                added += 1
            elif line.startswith("-") and not line.startswith("---"):
                removed += 1
        return {
            "added": added,
            "removed": removed,
            "content": content,
            "error": None,
            "base": mode,
        }

    try:
        return JSONResponse(await asyncio.to_thread(_compute))
    except Exception as err:  # noqa: BLE001
        return JSONResponse({"error": str(err)}, status_code=400)


@app.get("/api/instances/{title}/file-diff")
async def instance_file_diff(
    title: str, path: str = "", base: str = "fork"
) -> JSONResponse:
    """Whole-file diff for one file: the full file content with added/removed
    lines marked (huge ``--unified`` so every line is shown, not just hunks).

    Same base modes + ``add -N`` as the summary diff (``fork`` default /
    ``head``), so it lines up with the file headers shown there. Driven by
    clicking a file in the Diff tab.
    """
    if not git_available():
        return _no_git_response()
    inst, err = _inst_or_404(title)
    if err is not None:
        return err
    if not inst.Started():
        return JSONResponse({"content": "", "error": None})
    rel = (path or "").strip()
    if not rel:
        return JSONResponse({"error": "path is required"}, status_code=400)

    def _compute() -> dict:
        wt = inst.GetGitWorktree()
        wtp = wt.GetWorktreePath()
        base_ref = "HEAD" if base == "head" else _session_fork_point(inst, wtp)
        subprocess.run(
            ["git", "-C", wtp, "add", "-N", "."],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
        cp = subprocess.run(
            [
                "git",
                "-C",
                wtp,
                "--no-pager",
                "diff",
                "--unified=100000",
                base_ref,
                "--",
                rel,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
        if cp.returncode != 0:
            return {
                "content": "",
                "error": cp.stderr.decode("utf-8", "replace").strip()
                or "git diff failed",
            }
        return {"content": cp.stdout.decode("utf-8", "replace"), "error": None}

    try:
        return JSONResponse(await asyncio.to_thread(_compute))
    except Exception as err:  # noqa: BLE001
        return JSONResponse({"error": str(err)}, status_code=400)


@app.post("/api/instances/{title}/pause")
async def instance_pause(title: str) -> JSONResponse:
    """Pause the session: ``inst.Pause`` stops the agent (kills its tmux
    session and PTY) and keeps the worktree and branch exactly as they are —
    no commit, no removal; status becomes ``paused``. Persists the new state
    and emits ``session.paused``. A pause error is a 400."""
    inst, err = _inst_or_404(title)
    if err is not None:
        return err
    if getattr(inst, "PeerShare", ""):
        # A shared-folder session runs sandboxed; stopping and relaunching it
        # is the peer service's job (unshare), not pause/resume.
        return JSONResponse(
            {
                "error": "peer shared sessions cannot be paused — unshare instead",
                "peer_share": True,
            },
            status_code=409,
        )
    try:
        await asyncio.to_thread(inst.Pause)
    except Exception as err:  # noqa: BLE001
        return JSONResponse({"error": str(err)}, status_code=400)
    # Paused outranks asleep: Resume relaunches through the engine, which
    # never reads a sleep record, so none may outlive the pause.
    _sleep.drop(title)
    ENGINE.save()
    _events.BUS.emit(
        "session.paused",
        session=title,
        new=getattr(inst.Status, "name", str(inst.Status)).lower(),
    )
    return JSONResponse(_instance_json(inst))


@app.post("/api/instances/{title}/resume")
async def instance_resume(title: str) -> JSONResponse:
    """Resume a paused session: ``inst.Resume`` relaunches the agent in the same
    worktree (a session paused under the old scheme, whose worktree was removed,
    is rebuilt first — the engine's legacy path), re-deriving its port block
    into the fresh tmux env first, then persist and emit ``session.resumed``.
    A resume error is a 400."""
    inst, err = _inst_or_404(title)
    if err is not None:
        return err
    # O4: ExtraEnv isn't persisted — re-derive the port block so a resumed
    # session's fresh tmux gets the same env its worktree was set up with.
    # The auth-profile / local-model overlay has to be re-derived here for the
    # same reason: this assignment REPLACES the dict, so a bare port block
    # would resume a profiled session on the CLI's ambient login and a
    # local-model session against its hosted API.
    inst.ExtraEnv = {**_session_overlay_env(inst), **_ports.env_for(title)}
    try:
        await asyncio.to_thread(inst.Resume)
    except Exception as err:  # noqa: BLE001
        return JSONResponse({"error": str(err)}, status_code=400)
    ENGINE.save()
    _events.BUS.emit(
        "session.resumed",
        session=title,
        new=getattr(inst.Status, "name", str(inst.Status)).lower(),
    )
    return JSONResponse(_instance_json(inst))


@app.post("/api/instances/{title}/background/{pid}/stop")
async def instance_background_stop(title: str, pid: int) -> JSONResponse:
    """Stop one background job the session's agent left running (the row's
    stuck-job chip). Only a CURRENT job root of THIS session can be signalled:
    anything else is a **409**. The agent hears about it the way it hears
    about any job ending, so it may take a short turn to report it.
    → ``{"title", "pid", "stopped": true}``."""
    inst, err = _inst_or_404(title)
    if err is not None:
        return err
    from backend.providers import claude_background as _bg

    ok = await asyncio.to_thread(_bg.stop, tmux.to_mindflock_tmux_name(title), pid)
    if not ok:
        return JSONResponse(
            {"error": "no such background job in this session"}, status_code=409
        )
    return JSONResponse({"title": title, "pid": pid, "stopped": True})


_JOB_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


async def _current_job(title: str, job_id: str):
    """``(inst, job, error response)`` for one background job of ``title``
    (a fresh scan): 404 for an unknown session or a job it isn't running."""
    inst, err = _inst_or_404(title)
    if err is not None:
        return None, None, err
    from backend.providers import claude_background as _bg

    job = None
    if _JOB_ID_RE.fullmatch(job_id or ""):
        job = await asyncio.to_thread(
            _bg.find_job, tmux.to_mindflock_tmux_name(title), job_id
        )
    if job is None:
        return (
            inst,
            None,
            JSONResponse(
                {"error": "no such background job in this session"}, status_code=404
            ),
        )
    return inst, job, None


def _job_expect_answer(title: str, job_id: str, ok: bool) -> dict:
    """The route's answer from a fresh look at the job, which also refreshes
    the session's cached scan so the next snapshot carries the change."""
    from backend.providers import claude_background as _bg

    name = tmux.to_mindflock_tmux_name(title)
    if _bg.recent(name) is not None:
        _bg.scan(name)
    job = _bg.find_job(name, job_id) or {}
    return {
        "ok": ok,
        "title": title,
        "job_id": job_id,
        "expect_s": job.get("expect_s"),
        "expected_by": job.get("expected_by"),
        "eta": job.get("eta"),
        "state": job.get("state"),
        "reason": job.get("reason"),
    }


@app.post("/api/instances/{title}/jobs/{job_id}/expect")
async def instance_job_expect(
    title: str, job_id: str, payload: Optional[dict] = None
) -> JSONResponse:
    """Say how long one background job should take, so it isn't called stuck
    while inside that window: ``{"duration": "6h" | "1h30m" | "2d" | seconds,
    "by": "you" (default) | "agent"}``. The job id is the one Claude Code
    printed when it backgrounded the command (``background[].id``). Its stuck
    threshold becomes at least 1.5 × the duration (claude_background). A
    person's expectation stands against the agent's: the agent's write is
    then ``ok: false`` and the answer carries the person's. → ``{ok, title,
    job_id, expect_s, expected_by, eta, state, reason}``; 404 unknown session
    or job, 400 bad duration (1 min to 14 days)."""
    from backend.providers import claude_background as _bg

    body = payload or {}
    by = body.get("by") or "you"
    if by not in ("you", "agent"):
        return JSONResponse({"error": "by must be 'you' or 'agent'"}, status_code=400)
    _inst, job, err = await _current_job(title, job_id)
    if err is not None:
        return err
    secs = _bg.parse_duration(body.get("duration"))
    if secs is None:
        return JSONResponse(
            {
                "error": "duration must be like 90m, 6h, 1h30m or 2d (or seconds), "
                "between 1 minute and 14 days"
            },
            status_code=400,
        )
    try:
        ok, _entry = await asyncio.to_thread(
            _bg.set_expectation, job_id, secs, by, title
        )
    except OSError as e:
        return JSONResponse(
            {"error": "couldn't save the expectation: %s" % e}, status_code=500
        )
    return JSONResponse(await asyncio.to_thread(_job_expect_answer, title, job_id, ok))


@app.delete("/api/instances/{title}/jobs/{job_id}/expect")
async def instance_job_expect_clear(title: str, job_id: str) -> JSONResponse:
    """Clear a background job's expected duration (the person's or the
    agent's; a ``MINDFLOCK_EXPECT=`` in its command still applies). → the
    same shape as POST, ``ok`` false when nothing was set; 404 unknown
    session or job."""
    from backend.providers import claude_background as _bg

    _inst, job, err = await _current_job(title, job_id)
    if err is not None:
        return err
    try:
        ok = await asyncio.to_thread(_bg.clear_expectation, job_id)
    except OSError as e:
        return JSONResponse(
            {"error": "couldn't save the expectation: %s" % e}, status_code=500
        )
    return JSONResponse(await asyncio.to_thread(_job_expect_answer, title, job_id, ok))


@app.post("/api/instances/{title}/revive")
async def instance_revive(title: str) -> JSONResponse:
    """Restart an offline agent: the Offline chip's Restart action.

    Calls the same lazy revive a terminal attach or a sent prompt does
    (:func:`_ensure_agent_session`): a live agent tmux is reused, a dead one
    is relaunched — resuming the conversation after a crash, fresh after a
    clean quit. A paused session is **409** (resume it instead); a workspace
    that is gone, or a shared folder the agent may not launch in, is **409**
    with the helper's reason.

    An agent that died mid-turn also gets a "continue where you left off" at
    the front of its prompt queue (:func:`core.revive.relaunch`). A person's
    Restart is never held for memory, but the answer says when it is low.
    → ``{"title", "revived": true, "continued": bool, "memory_warning": str}``."""
    inst, err = _inst_or_404(title)
    if err is not None:
        return err
    if getattr(inst, "Status", None) == Paused:
        return JSONResponse(
            {"error": "session is paused — resume it instead"}, status_code=409
        )
    _, rerr, continued = await asyncio.to_thread(_revive.relaunch, inst, title)
    if rerr is not None:
        return JSONResponse({"error": rerr}, status_code=409)
    from backend import memory_pressure as _mem

    return JSONResponse(
        {
            "title": title,
            "revived": True,
            "continued": bool(continued),
            "memory_warning": await asyncio.to_thread(_mem.describe),
        }
    )


@app.post("/api/instances/{title}/sleep")
async def instance_sleep(title: str) -> JSONResponse:
    """Put the session's agent to sleep now (:mod:`backend.web.core.sleep`):
    stop its CLI and MCP servers to give their memory back, keeping the
    session, worktree and conversation. The sweep's safety rules hold — its
    turn has ended (idle, no dialog, no background job), nothing is queued
    for it, no terminal is attached — but not its timing ones (idle long
    enough, nobody typed lately). **409** ``{"error", "veto"}`` when one
    refuses. → ``{"title", "sleeping": {since, reason, freed_mb}}``."""
    inst, err = _inst_or_404(title)
    if err is not None:
        return err
    why = await asyncio.to_thread(_sleep.sleep_now, inst, title)
    if why is not None:
        return JSONResponse(
            {"error": "can't sleep %s: %s" % (title, why), "veto": why},
            status_code=409,
        )
    return JSONResponse({"title": title, "sleeping": _sleep.info(title)})


@app.post("/api/instances/{title}/wake")
async def instance_wake(title: str) -> JSONResponse:
    """Wake a sleeping session's agent: relaunch it, resuming its
    conversation (the rail's Wake chip; opening it or sending it anything
    does the same). Not asleep is a no-op (``woke: false``). **409** with the
    reason when its workspace is gone. → ``{"title", "woke": bool}``."""
    inst, err = _inst_or_404(title)
    if err is not None:
        return err
    woke, werr = await asyncio.to_thread(
        _sleep.ensure_awake, inst, title, "woken on request"
    )
    if werr is not None:
        return JSONResponse({"error": werr}, status_code=409)
    return JSONResponse({"title": title, "woke": woke})


# --------------------------------------------------------------------------- #
# O2/O3: worktree setup + verification-gate endpoints. Status lives in marker
# files inside the worktree (.mindflock_setup.json / .mindflock_check.json),
# so it survives server restarts with zero extra state.
# --------------------------------------------------------------------------- #
def _wt_or_409(title: str):
    """``(inst, wt_path, error_response_or_None)`` for the endpoints below."""
    inst = ENGINE.instances.get(title)
    if inst is None:
        return (
            None,
            "",
            JSONResponse({"error": "instance not found: %s" % title}, status_code=404),
        )
    try:
        wt = inst.GetWorktreePath()
    except Exception:  # noqa: BLE001
        wt = ""
    if not wt or not os.path.isdir(wt):
        return inst, "", JSONResponse({"error": "workspace not ready"}, status_code=409)
    return inst, wt, None


@app.get("/api/instances/{title}/setup")
async def instance_setup_status(title: str, lines: int = 200) -> JSONResponse:
    """The worktree's O2 setup summary plus the tail of the setup log (``lines``),
    read off the event loop — this endpoint is polled while a setup runs."""
    _inst, wt, err = _wt_or_409(title)
    if err is not None:
        return err
    # File reads + summary run off the event loop — this endpoint is polled
    # while a setup runs, and blocking here stalls every concurrent request.
    payload = await asyncio.to_thread(
        lambda: {
            "status": _wt_setup.setup_summary(wt),
            "log": _wt_setup.log_tail(wt, _wt_setup.SETUP_LOG, lines),
        }
    )
    return JSONResponse(payload)


@app.post("/api/instances/{title}/setup/rerun")
async def instance_setup_rerun(title: str) -> JSONResponse:
    """Re-run the worktree's O2 setup pass (Settings action).

    400 when no ``[workspace]`` setup is configured, 409 when one is already
    running, else 202 with the fresh setup summary.
    """
    inst, wt, err = _wt_or_409(title)
    if err is not None:
        return err
    peer_err = _peer_guard.refusal(inst, "worktree setup")
    if peer_err is not None:
        return peer_err
    cfg = _wt_setup.load_config(wt)  # .mindflock.toml is committed → in the worktree
    if not cfg.has_setup:
        return JSONResponse(
            {"error": "no [workspace] setup configured in .mindflock.toml"},
            status_code=400,
        )
    started = _wt_setup.start_setup(title, inst.Path or wt, wt, cfg)
    if not started:
        return JSONResponse({"error": "setup already running"}, status_code=409)
    return JSONResponse(
        {"ok": True, "status": _wt_setup.setup_summary(wt)}, status_code=202
    )


@app.get("/api/instances/{title}/check")
async def instance_check_status(title: str, lines: int = 200) -> JSONResponse:
    """The worktree's O3 verification-gate summary plus the tail of the check log
    (``lines``), read off the event loop — this endpoint is polled during a run."""
    _inst, wt, err = _wt_or_409(title)
    if err is not None:
        return err
    # check_summary shells out to `git rev-parse` and log_tail reads the log
    # file — both blocking, and this endpoint is polled while a check runs.
    payload = await asyncio.to_thread(
        lambda: {
            "status": _wt_setup.check_summary(wt),
            "log": _wt_setup.log_tail(wt, _wt_setup.CHECK_LOG, lines),
        }
    )
    return JSONResponse(payload)


@app.post("/api/instances/{title}/check")
async def instance_check_run(title: str) -> JSONResponse:
    """Kick off the worktree's O3 verification check.

    400 when no ``check_command`` is configured, 409 when a check is already
    running, else 202 with the fresh check summary.
    """
    _inst, wt, err = _wt_or_409(title)
    if err is not None:
        return err
    peer_err = _peer_guard.refusal(_inst, "the verification check")
    if peer_err is not None:
        return peer_err
    cfg = _wt_setup.load_config(wt)
    if not cfg.check_command:
        return JSONResponse(
            {"error": "no [workspace] check_command configured in .mindflock.toml"},
            status_code=400,
        )
    started = _wt_setup.start_check(title, wt, cfg.check_command)
    if not started:
        return JSONResponse({"error": "check already running"}, status_code=409)
    return JSONResponse(
        {"ok": True, "status": _wt_setup.check_summary(wt)}, status_code=202
    )


# --------------------------------------------------------------------------- #
# Code Map + red zones. The analysis lives in core.code_map, the store and the
# guard file in config.red_zones, the reconcile loop + events in
# core.red_zone_monitor; these routes are the thin HTTP skin over them. Every
# git/store touch runs off the event loop — the Map polls /live every 2 s.
# --------------------------------------------------------------------------- #


def _code_map_fp(inst, wt: str, zones, fp: Optional[str] = None) -> Optional[str]:
    """The Map's snapshot fingerprint: the worktree fingerprint (``fp`` when
    the caller already has it) plus a digest of the zone set, because a new
    zone changes the snapshot (the zone-matched git-ignored files it adds)
    without touching git at all."""
    if fp is None:
        fp = _code_map.fingerprint(inst, wt)
    if fp is None or not zones:
        return fp
    import hashlib

    sig = "\n".join(
        sorted("%s %s %s" % (z.get("id"), z.get("kind"), z.get("re")) for z in zones)
    )
    return fp + "." + hashlib.sha1(sig.encode("utf-8", "replace")).hexdigest()[:10]


def _snapshot_activity(title: str) -> str:
    """The session's activity off the published snapshot (in-memory; the Map
    polls too often to pay for a pane probe)."""
    try:
        for row in _events.sessions_snapshot():
            if isinstance(row, dict) and row.get("title") == title:
                return str(row.get("activity") or "offline")
    except Exception:  # noqa: BLE001
        pass
    return "offline"


def _feed_out(rec: dict, wt: str, doc: Optional[dict] = None) -> dict:
    """A feed record for the browser: writes/reads made worktree-relative
    (paths outside the worktree dropped — a plan file under ~/.claude/plans is
    not a tile), the transcript path and plan text left out (the plan travels
    as ``plan``). With a green scope in force (``doc``), reads outside it are
    listed as ``peek`` — "peeked outside scope", advisory: reads are never
    blocked (a shell ``cat`` can't be, and blinding the agent to its callers
    breaks them)."""
    out = {k: v for k, v in rec.items() if k not in ("tp", "plan", "writes", "reads")}
    for key in ("writes", "reads"):
        rels = []
        for p in rec.get(key) or []:
            rel = _code_map._rel_in_wt(p, wt)
            if rel and rel not in rels:
                rels.append(rel)
        if rels:
            out[key] = rels
    if doc and doc.get("green") and out.get("reads"):
        ci = bool(doc.get("ci"))
        peek = [
            r
            for r in out["reads"]
            if _red_zones.classify(r, None, doc, ci) == "outside"
        ]
        if peek:
            out["peek"] = peek[:40]
    if isinstance(rec.get("artifact"), list):
        arts = []
        for p in rec["artifact"]:
            p = str(p)
            if p and p not in arts:
                arts.append(p)
        if arts:
            out["artifact"] = arts[:50]
    return out


#: Appended to a snapshot fingerprint whose import graph is still partial,
#: so it never equals the live poll's and the client refetches (see below).
_PARTIAL_FP_SUFFIX = ".partial"


@app.get("/api/instances/{title}/code-map")
async def instance_code_map(title: str, fp: str = "") -> JSONResponse:
    """The Map's structural snapshot: the worktree's files (``[rel, size,
    flags]``; flag 1 = zone-matched git-ignored file, 2 = test file) and the
    import graph over them (``edges`` = ``[src, dst]`` indices, "src imports
    dst"). ``?fp=`` = the fingerprint the client already holds; when it is
    still current the answer is just ``{"unchanged": true, "fingerprint"}``.

    A PARTIAL import graph (the read budget ran out on a big/cold repo) is
    never "unchanged": the next build resumes from the per-file memo, but
    only if a next build happens, and an idle worktree (the agent waiting on
    plan review) never moves the fingerprint. So while the graph is partial
    the route skips the shortcut, and the snapshot's fingerprint carries a
    ``.partial`` suffix — it then differs from the live poll's, which is what
    makes the client come back for the rest until the graph is complete."""
    if not git_available():
        return _no_git_response()
    inst, wt, err = _wt_or_409(title)
    if err is not None:
        return err
    try:
        return JSONResponse(await asyncio.to_thread(_code_map_snapshot, inst, wt, fp))
    except Exception as err:  # noqa: BLE001
        return JSONResponse({"error": str(err)}, status_code=500)


def _code_map_snapshot(inst, wt: str, fp: str = "") -> dict:
    """The snapshot ``GET /code-map`` answers (see the route), built off the
    event loop. Every piece is cached on the worktree fingerprint, so calling
    it for a worktree whose snapshot is current costs one fingerprint."""
    repo_id, repo = _rz_repo(wt)
    zones = _red_zones.effective_zones(wt, repo_id)
    raw = _code_map.fingerprint(inst, wt)
    cur = _code_map_fp(inst, wt, zones, raw)
    if fp and cur is not None and fp == cur and not _code_map.last_graph_partial(wt):
        return {"unchanged": True, "fingerprint": cur}
    extra: list = []
    red = [z for z in zones if z.get("kind", "red") == "red"]
    if red:
        # Git-ignored files are shown for RED zones only ("my config
        # file"); a green scope never makes an ignored file interesting.
        _files, _dirs, extra, _trunc = _red_zones.zone_files(wt, red)
    files, truncated = _code_map.list_files(wt, extra, fp=raw)
    graph = _code_map.build_graph(wt, files, fp=raw)
    partial = bool(graph.get("partial"))
    return {
        "root": wt,
        "repo": repo,
        "fingerprint": (cur + _PARTIAL_FP_SUFFIX) if (cur and partial) else cur,
        "files": files,
        "truncated": truncated,
        "edges": graph.get("edges") or [],
        "graph_partial": partial,
        "langs": graph.get("langs") or {},
    }


# The Map's first open used to pay for everything at once: the file list and
# the import graph (3 s on a 3k-file repo after a restart), then the layout.
# Now the snapshot is warmed in the background — once at boot for every open
# session (serialized, one worktree at a time, starting after the boot quiet
# window AND the background revive's start, so it never competes with the
# terminals and agents coming up), and the page prefetches it for the session
# you are looking at — so opening the tab is a cache hit.
_CODE_MAP_WARM_DELAY_S = _BOOT_QUIET_SECONDS + 15.0
_CODE_MAP_WARM_GAP_S = 0.5
_CODE_MAP_WARM_BUDGET_S = 90.0


def _code_map_warm_all(budget_s: float = _CODE_MAP_WARM_BUDGET_S) -> dict:
    """Build (or confirm) the snapshot of every session that has a worktree.
    One at a time with a breather between, within ``budget_s`` in all; a
    worktree whose build fails is skipped. Returns ``{"warmed": n,
    "skipped": n, "partial": [titles]}`` — the log line. Never raises."""
    out = {"warmed": 0, "skipped": 0, "partial": []}
    if not git_available():
        return out
    deadline = time.monotonic() + max(0.0, budget_s)
    for title in list(ENGINE.instances):
        if time.monotonic() > deadline:
            out["skipped"] += 1
            continue
        inst = ENGINE.instances.get(title)
        try:
            wt = inst.GetWorktreePath() if inst is not None else ""
            if not wt or not os.path.isdir(wt):
                out["skipped"] += 1
                continue
            snap = _code_map_snapshot(inst, wt, "")
            if snap.get("graph_partial"):
                out["partial"].append(title)
            out["warmed"] += 1
        except Exception:  # noqa: BLE001
            out["skipped"] += 1
        time.sleep(_CODE_MAP_WARM_GAP_S)
    return out


def _code_map_warm_on_start_enabled() -> bool:
    """``MINDFLOCK_CODE_MAP_WARM=0`` switches the boot warm-up off (a scratch
    ``serve`` on a slow disk); it is always off under pytest."""
    if os.environ.get("MINDFLOCK_CODE_MAP_WARM", "1") == "0":
        return False
    return not _restart._under_pytest()


async def _code_map_warm_on_start() -> None:
    """Boot: wait out the quiet window (and the revive sweep's start), then
    warm every session's snapshot."""
    try:
        await asyncio.sleep(_CODE_MAP_WARM_DELAY_S)
        res = await asyncio.to_thread(_code_map_warm_all)
        if log.ErrorLog is not None and (res["warmed"] or res["partial"]):
            log.ErrorLog.Printf(
                "code map warmed for %d session(s)%s",
                res["warmed"],
                (
                    (" (partial: %s)" % ", ".join(res["partial"]))
                    if res["partial"]
                    else ""
                ),
            )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        pass


def _child_fences(title: str) -> list:
    """The fences on ``title``'s workers — what the orchestrator's Map lists
    as "Workers' fences": ``[{"session", "only", "keep_out", "reason",
    "by", "last"?}]``; ``last`` = the newest change after the fence was set
    (``red_zones.fence_log``), and a live worker whose fence went is listed
    with empty paths and that ``last`` (*fence lifted by …: why*). Never
    raises."""
    out = []
    try:
        for c in _lineage.children_of(ENGINE.instances, title):
            child = ENGINE.instances.get(c)
            cwt = child.GetWorktreePath() if child is not None else ""
            if not cwt:
                continue
            tname = tmux.to_mindflock_tmux_name(c)
            f = _red_zones.session_fence(cwt, tname)
            log = [
                r
                for r in _red_zones.fence_log(cwt, tname, 5)
                # "ended" belongs to an earlier session that had this name
                if r.get("event") not in ("set", "ended")
            ]
            if not f and not log:
                continue
            row = {
                "session": c,
                "only": [z.get("pattern") for z in (f or {}).get("green") or []],
                "keep_out": [z.get("pattern") for z in (f or {}).get("red") or []],
                "reason": (f or {}).get("reason") or "",
                "by": (f or {}).get("by") or "",
            }
            if log:
                row["last"] = log[-1]
            out.append(row)
    except Exception:  # noqa: BLE001
        return out
    return out


@app.get("/api/instances/{title}/code-map/live")
async def instance_code_map_live(title: str, since: float = 0.0) -> JSONResponse:
    """The Map's 2-second poll: change set, the tool feed since ``since``
    (paths worktree-relative), the agent's current plan and off-plan edits,
    zones (waived included, flagged), breaches, guard state (``detail`` is
    an explanatory sentence for the pill's tooltip), activity, and other
    sessions' recent edits on the same repo. ``fingerprint`` moves when the
    snapshot (``GET /code-map``) is worth refetching. ``ci`` = the worktree's
    filesystem is case-insensitive: the hook matches zones with IGNORECASE
    there, so the Map's zone matchers must too."""
    if not git_available():
        return _no_git_response()
    inst, wt, err = _wt_or_409(title)
    if err is not None:
        return err

    def _build() -> dict:
        now = time.time()
        repo_id, repo = _rz_repo(wt)
        tmux_name = tmux.to_mindflock_tmux_name(title)
        zones = _red_zones.effective_zones(wt, repo_id)
        # This session's own fence (an orchestrator set it): drawn and
        # classified like zones, scope "session", locked.
        zones = zones + _red_zones.session_fence_zones(
            _red_zones.session_fence(wt, tmux_name) or {}
        )
        ci = _red_zone_monitor.root_ci(wt)
        doc = _rz_doc(wt, repo_id, zones, ci)
        enforced_n = len(doc["red"]) + len(doc["green"])
        changed = _code_map.changed_files(inst, wt)
        records = _code_map.read_feed(tmux_name, 0.0, 500)
        plan = _code_map.current_plan(inst, tmux_name, wt, records)
        if doc["mode"] and isinstance(plan, dict):
            # Plan items outside the scope (or in a red zone) are flagged
            # every poll — the scope can change after the plan was written.
            items = []
            for it in plan.get("items") or []:
                if isinstance(it, dict) and it.get("path"):
                    v = _red_zones.classify(it["path"], None, doc, ci)
                    it = dict(it)
                    if v == "outside":
                        it["outside"] = True
                    elif v == "blocked":
                        it["blocked"] = True
                items.append(it)
            plan = dict(plan, items=items)
        prov = providers.resolve(getattr(inst, "Program", "") or "")
        try:
            plan_supported = bool(prov.plan_supported())
        except Exception:  # noqa: BLE001
            plan_supported = False
        return {
            "now": now,
            # After changed_files: its `add -N` is part of the state this
            # fingerprint describes, so the next snapshot read agrees with it.
            "fingerprint": _code_map_fp(inst, wt, zones),
            "repo": repo,
            "changed": changed,
            "feed": [
                _feed_out(r, wt, doc)
                for r in records
                if float(r.get("ts") or 0) > since
            ][-300:],
            "plan": plan,
            "off_plan": _code_map.off_plan(plan, changed, records, wt=wt),
            "zones": zones,
            "breaches": _live_breaches(inst, wt, changed, doc, ci),
            "guard": _red_zone_monitor.guard_info(title, inst, enforced_n, doc["mode"]),
            "mode": doc["mode"],
            "exempt": doc["exempt"],
            "companions": [
                {"pattern": c["pattern"], "re": c["re"], "source": c.get("source")}
                for c in doc["companions"]
            ],
            "companion_files": [
                c["pattern"][1:]
                for c in doc["companions"]
                if c.get("source") == "tests"
            ],
            "activity": _snapshot_activity(title),
            "plan_supported": plan_supported,
            "ci": ci,
            "others": _code_map.others(inst, repo_id, 0.0),
            "fences": _child_fences(title),
        }

    try:
        return JSONResponse(await asyncio.to_thread(_build))
    except Exception as err:  # noqa: BLE001
        return JSONResponse({"error": str(err)}, status_code=500)


# --------------------------------------------------------------------------- #
# Code Map file view + search. The analysis — per-file outlines, the file
# view and search — lives in core.code_outline and is keyed on the worktree's
# content fingerprint, so these routes are plain reads: git gate, worktree
# check, off the event loop (the first call on a big repo builds the index for
# a few seconds), and a bad ``path`` (absolute / ``..`` / not a file) is a
# 400, not a 500.
# --------------------------------------------------------------------------- #
async def _atlas_call(title: str, fn, *args) -> JSONResponse:
    """Run ``fn(inst, wt, *args)`` for ``title``'s worktree off the loop."""
    if not git_available():
        return _no_git_response()
    inst, wt, err = _wt_or_409(title)
    if err is not None:
        return err
    try:
        return JSONResponse(await asyncio.to_thread(fn, inst, wt, *args))
    except ValueError as err:
        return JSONResponse({"error": str(err)}, status_code=400)
    except Exception as err:  # noqa: BLE001
        return JSONResponse({"error": str(err)}, status_code=500)


@app.get("/api/instances/{title}/code-map/file")
async def instance_code_map_file(title: str, path: str = "") -> JSONResponse:
    """The file view: detailed outline, imports, entry points, used-by and
    what changed vs the session's fork point (``code_outline.file_view``).
    400 for an absolute / ``..`` / escaping / missing path."""
    return await _atlas_call(title, _code_outline.file_view, path)


@app.get("/api/instances/{title}/code-map/search")
async def instance_code_map_search(
    title: str, q: str = "", limit: int = 40
) -> JSONResponse:
    """Files, folders, symbols and routes matching ``q`` (camel/snake-aware,
    ranked; ``code_outline.search``)."""
    return await _atlas_call(title, lambda inst, wt: _code_outline.search(wt, q, limit))


def _told_body(told, reason) -> dict:
    body = {"ok": told is not False, "told": told}
    if reason:
        body["reason"] = reason
    return body


@app.post("/api/instances/{title}/code-map/ask-plan")
async def instance_code_map_ask_plan(
    title: str, payload: Optional[dict] = None
) -> JSONResponse:
    """Ask the agent for its file plan: ``{"mode": "plan"}`` (before it starts)
    or ``"remaining"`` (mid-flight: what it changed + what it still intends).
    Typed when the agent is idle, QUEUED while it works or waits on a prompt —
    ``{"ok", "told": "sent"|"queued"|false, "reason"?}``; never reboots it."""
    if not git_available():
        return _no_git_response()
    inst, wt, err = _wt_or_409(title)
    if err is not None:
        return err
    mode = str((payload or {}).get("mode") or "plan")
    if mode not in ("plan", "remaining"):
        return JSONResponse(
            {"error": "mode must be plan or remaining"}, status_code=400
        )
    text = _red_zones.PLAN_PROMPT if mode == "plan" else _red_zones.REMAINING_PROMPT
    told, reason = await asyncio.to_thread(lambda: _deliver_to_agent(inst, title, text))
    return JSONResponse(_told_body(told, reason))


def _plan_scope_patterns(inst, title: str, wt: str) -> list:
    """Anchored green patterns for the agent's current plan: an existing
    file or dir → its exact path; a NEW file → its parent directory (the
    agent must be able to create it; a new file at the root → itself).
    Every path is :func:`red_zones.glob_escape`-d — it is literal."""
    tmux_name = tmux.to_mindflock_tmux_name(title)
    records = _code_map.read_feed(tmux_name, 0.0, 500)
    plan = _code_map.current_plan(inst, tmux_name, wt, records)
    out: list = []
    for it in (plan or {}).get("items") or []:
        rel = str((it or {}).get("path") or "").strip().strip("/")
        if not rel or rel.startswith("..") or os.path.isabs(rel):
            continue
        abs_p = os.path.join(wt, rel)
        # Literal paths: glob-escaped, or `app/[slug]/page.tsx` would be a
        # character class that misses the planned file itself.
        esc = _red_zones.glob_escape
        if os.path.exists(abs_p) or not it.get("new"):
            pat = "/" + esc(rel)
        else:
            parent = os.path.dirname(rel)
            pat = ("/" + esc(parent) + "/") if parent else ("/" + esc(rel))
        try:
            _red_zones.normalize_pattern(pat)
        except ValueError:
            continue
        if pat not in out:
            out.append(pat)
    return out


@app.post("/api/instances/{title}/code-map/go")
async def instance_code_map_go(
    title: str, payload: Optional[dict] = None
) -> JSONResponse:
    """The plan loop's **Go**: one message that green-lights the plan and names
    the zones staged during review (``{"zone_ids": [...]}`` — ids of this
    worktree's effective zones; unknown/waived ids are ignored).

    ``{"scope_to_plan": true}`` is **Go — only the planned files**: every
    plan item becomes an anchored worktree GREEN zone (exact paths; the
    parent dir of a new file), files already changed outside that scope are
    exempted, the guard is synced, and the go message names the scope. The
    answer then also carries ``zones`` (the created green zones) and
    ``exempt``; 409 when the plan names no usable path."""
    if not git_available():
        return _no_git_response()
    inst, wt, err = _wt_or_409(title)
    if err is not None:
        return err
    peer_err = _peer_guard.refusal(inst, "the code-map go (it writes guard files)")
    if peer_err is not None:
        return peer_err
    p = payload or {}
    ids = [str(i) for i in (p.get("zone_ids") or []) if i]
    scope_to_plan = bool(p.get("scope_to_plan"))

    def _go():
        repo_id, _repo = _rz_repo(wt)
        by_id = {
            z["id"]: z
            for z in _red_zones.effective_zones(wt, repo_id)
            if not z.get("waived")
        }
        chosen = [by_id[i] for i in dict.fromkeys(ids) if i in by_id]
        created: list = []
        exempt: list = []
        if scope_to_plan:
            pats = _plan_scope_patterns(inst, title, wt)
            if not pats:
                return None
            ci = _red_zone_monitor.root_ci(wt)
            old_doc = _rz_doc(wt, repo_id, ci=ci)
            with _red_zone_monitor.route_write():
                for pat in pats:
                    try:
                        created.append(
                            _red_zones.add_zone(
                                "worktree",
                                os.path.realpath(wt),
                                pat,
                                # Unnamed on purpose: the agent's deny reason
                                # and the UI name a zone by its name, and
                                # "planned" x N says nothing; the path does.
                                note="from the plan (Go — only the planned files)",
                                repo_id=repo_id,
                                kind="green",
                            )
                        )
                    except (ValueError, LookupError):
                        continue  # a red zone on the same path wins
                new_doc = _rz_doc(wt, repo_id, ci=ci)
                exempt, _n = _rz_green_exempt(inst, wt, old_doc, new_doc)
            _red_zone_monitor.resync([(os.path.realpath(wt), wt)], repo_id)
        msg = _red_zones.go_message(chosen, scope=created or None, hard=_rz_hard(inst))
        told, reason = _deliver_to_agent(inst, title, msg)
        if told is not False:
            _red_zone_monitor.note_go(title)  # the row's "plan · n" goes out
        return told, reason, created, exempt

    res = await asyncio.to_thread(_go)
    if res is None:
        return JSONResponse(
            {"error": "the plan names no file to scope the session to"},
            status_code=409,
        )
    told, reason, created, exempt = res
    body = _told_body(told, reason)
    if scope_to_plan:
        body["zones"] = created
        body["exempt"] = exempt
    return JSONResponse(body)


@app.get("/api/instances/{title}/red-zones")
async def instance_red_zones(title: str) -> JSONResponse:
    """This worktree's effective zones (repo + worktree scope; waived repo zones
    included and flagged; green ones ``kind: "green"``), its repo's Plan-first
    flag, the zone ``mode``, the green exemption set and the companion rules
    in force."""
    if not git_available():
        return _no_git_response()
    _inst, wt, err = _wt_or_409(title)
    if err is not None:
        return err

    def _read():
        repo_id, repo = _rz_repo(wt)
        zones = _red_zones.effective_zones(wt, repo_id)
        doc = _rz_doc(wt, repo_id, zones)
        return {
            "repo": repo,
            "plan_first": _red_zones.plan_first(repo_id),
            "zones": zones,
            "mode": doc["mode"],
            "exempt": doc["exempt"],
            "companions": _red_zones.companions_config(repo_id),
            "sessions_here": _sessions_on_worktree(wt),
            # The repo's committed zones (.mindflock/zones.toml in this
            # worktree) as the file has them — the Zones dialog's "Shared
            # with the repo" section; `zones` above already enforces them.
            **_shared_zones_body(wt),
        }

    return JSONResponse(await asyncio.to_thread(_read))


@app.get("/api/instances/{title}/shared-zones")
async def instance_shared_zones(title: str) -> JSONResponse:
    """The repo's committed zones as this session's worktree has them
    (``.mindflock/zones.toml``, so they follow its branch)."""
    _inst, wt, err = _wt_or_409(title)
    if err is not None:
        return err
    return JSONResponse(await asyncio.to_thread(_shared_zones_body, wt))


@app.post("/api/instances/{title}/shared-zones")
async def instance_shared_zones_add(
    title: str, request: Request, payload: Optional[dict] = None
) -> JSONResponse:
    """ "Add to repo": ``{"pattern", "kind": "red"|"green", "note", "name"}``
    appended to ``.mindflock/zones.toml`` in THIS session's worktree — the
    file rides the session's branch, so teammates get the zone when it merges.
    Nothing is committed here. ``author`` = the person's name, ``added`` =
    today. Repo policy, so the owner only (``auth.may_configure``, 403); 400
    on a bad pattern or an unusable file, 409 when the pattern is already the
    other kind there. Answers the shared-zones body + ``zone`` / ``created``,
    after re-syncing the guard so the zone reaches the next tool call."""
    if not await _auth.may_configure(request.scope):
        return _auth.configure_refused()
    _inst, wt, err = _wt_or_409(title)
    if err is not None:
        return err
    payload = payload if isinstance(payload, dict) else {}

    def _add():
        zone, created = _repo_zones.add(
            wt,
            str(payload.get("pattern") or ""),
            kind=str(payload.get("kind") or "red"),
            note=str(payload.get("note") or ""),
            name=str(payload.get("name") or ""),
            author=_shared_zone_author(),
        )
        repo_id, _repo = _rz_repo(wt)
        _red_zone_monitor.resync([(os.path.realpath(wt), wt)], repo_id)
        return {"zone": zone, "created": created, **_shared_zones_body(wt)}

    try:
        body = await asyncio.to_thread(_add)
    except _red_zones.ZoneConflict as err2:
        return JSONResponse({"error": str(err2)}, status_code=409)
    except (ValueError, OSError) as err2:
        return JSONResponse({"error": str(err2)}, status_code=400)
    return JSONResponse(body)


@app.post("/api/instances/{title}/shared-zones/remove")
async def instance_shared_zones_remove(
    title: str, request: Request, payload: Optional[dict] = None
) -> JSONResponse:
    """``{"id"}`` — take one shared zone out of this worktree's
    ``.mindflock/zones.toml`` (its block only; every other byte stays). The
    owner only (403); 404 when it isn't there, 400 when the file is
    unusable."""
    if not await _auth.may_configure(request.scope):
        return _auth.configure_refused()
    _inst, wt, err = _wt_or_409(title)
    if err is not None:
        return err
    zid = str((payload or {}).get("id") or "").strip()

    def _rm():
        gone = _repo_zones.remove(wt, zid)
        if gone is None:
            return None
        repo_id, _repo = _rz_repo(wt)
        _red_zone_monitor.resync([(os.path.realpath(wt), wt)], repo_id)
        return {"zone": gone, **_shared_zones_body(wt)}

    try:
        body = await asyncio.to_thread(_rm)
    except (ValueError, OSError) as err2:
        return JSONResponse({"error": str(err2)}, status_code=400)
    if body is None:
        return JSONResponse({"error": "unknown shared zone: %s" % zid}, status_code=404)
    return JSONResponse(body)


@app.post("/api/instances/{title}/red-zones")
async def instance_red_zones_add(
    title: str, request: Request, payload: Optional[dict] = None
) -> JSONResponse:
    """Add a zone from a session: ``{"pattern", "name", "note", "kind":
    "red"|"green", "scope": "repo"|"worktree", "tell_agent", "exempt"}``.

    RED (default): as v2 — the guard files it reaches are synced before this
    returns, so the agent's very next tool call is checked against it; files
    already changed inside it come back as ``already_changed`` and are
    recorded as pre-existing (never announced as a fresh breach).

    GREEN ("only here"): worktree scope only (``scope: "repo"`` → 400; the
    default scope is the worktree). Every path already changed outside the
    new scope is EXEMPT by default (``"exempt": false`` = treat them as
    breaches) — ``exempt`` / ``committed_outside`` in the answer. The same
    pattern as both kinds → 409. ``tell_agent`` sends the notice (queued
    mid-turn); a green notice never says "revert".

    A REPO-scope zone reaches every worktree of the repo (and settings sync
    spreads it), so it is the owner's only (``auth.may_configure``, 403) —
    as on ``POST /api/red-zones``; a worktree zone is part of driving this
    session."""
    if not git_available():
        return _no_git_response()
    inst, wt, err = _wt_or_409(title)
    if err is not None:
        return err
    p = payload or {}
    pattern = str(p.get("pattern") or "")
    kind = str(p.get("kind") or "red")
    if kind not in ("red", "green"):
        return JSONResponse({"error": "kind must be 'red' or 'green'"}, status_code=400)
    scope = str(p.get("scope") or ("worktree" if kind == "green" else "repo"))
    if scope not in ("repo", "worktree"):
        return JSONResponse(
            {"error": "scope must be 'repo' or 'worktree'"}, status_code=400
        )
    if scope == "repo" and not await _auth.may_configure(request.scope):
        return _auth.configure_refused()
    if kind == "green" and scope != "worktree":
        return JSONResponse(
            {
                "error": "green zones are worktree-scope only — they scope a task, "
                "not the repo (a repo-wide one would leak into every session of it)"
            },
            status_code=400,
        )
    want_exempt = p.get("exempt", True) is not False

    def _add():
        repo_id, repo = _rz_repo(wt)
        if scope == "repo" and not repo_id:
            raise LookupError(
                "couldn't identify this repo (git failed) — try again, or add "
                "the zone to this worktree only"
            )
        ci = _red_zone_monitor.root_ci(wt)
        old_doc = _rz_doc(wt, repo_id, ci=ci) if kind == "green" else None
        with _red_zone_monitor.route_write():
            zone = _red_zones.add_zone(
                scope,
                repo_id if scope == "repo" else os.path.realpath(wt),
                pattern,
                name=str(p.get("name") or ""),
                note=str(p.get("note") or ""),
                label=(repo or {}).get("label", ""),
                repo_id=repo_id,
                kind=kind,
            )
            exempt, committed_outside = [], 0
            if kind == "green" and want_exempt:
                new_doc = _rz_doc(wt, repo_id, ci=ci)
                exempt, committed_outside = _rz_green_exempt(inst, wt, old_doc, new_doc)
        already: list = []
        if kind == "red":
            already = _code_map.changed_matching(
                inst, wt, _red_zones.compile_pattern(zone["pattern"])
            )
            _red_zone_monitor.seed_breaches(wt, already)
        if scope == "repo":
            targets = _live_repo_roots(repo_id, wt)
        else:
            targets = [(os.path.realpath(wt), wt)]
        _red_zone_monitor.resync(targets, repo_id)
        zones = _red_zones.effective_zones(wt, repo_id)
        green = [z for z in zones if z.get("kind") == "green"]
        return zone, zones, already, exempt, committed_outside, green

    try:
        zone, zones, already, exempt, committed_outside, green = (
            await asyncio.to_thread(_add)
        )
    except ValueError as err:
        return JSONResponse({"error": str(err)}, status_code=400)
    except LookupError as err:  # incl. ZoneConflict (same pattern, other kind)
        return JSONResponse({"error": str(err)}, status_code=409)
    told, reason = False, None
    if p.get("tell_agent"):
        msg = _red_zones.zone_added_message(
            dict(zone, kind=kind), hard=_rz_hard(inst), green=green
        )
        told, reason = await asyncio.to_thread(
            lambda: _deliver_to_agent(inst, title, msg)
        )
    body = {
        "ok": True,
        "zone": dict(zone, kind=kind),
        "zones": zones,
        "told": told,
        "already_changed": already,
    }
    if kind == "green":
        body["exempt"] = exempt
        body["committed_outside"] = committed_outside
    if reason:
        body["reason"] = reason
    return JSONResponse(body)


@app.post("/api/instances/{title}/red-zones/allow")
async def instance_red_zones_allow(
    title: str, payload: Optional[dict] = None
) -> JSONResponse:
    """**Allow this file** on a green-zone scope request: ``{"path"}`` →
    an anchored worktree green zone for exactly that path, synced now, and
    the agent told it may edit it (``tell_agent: false`` to skip). 409 when
    the worktree has no green scope (a first green zone would suddenly
    scope the whole session to one file) or a red zone covers the path
    (red wins); 400 for an absolute / ``..`` path."""
    if not git_available():
        return _no_git_response()
    inst, wt, err = _wt_or_409(title)
    if err is not None:
        return err
    p = payload or {}
    rel = _clean_rel_arg(p.get("path"))
    if rel is None:
        return JSONResponse({"error": "path must be repo-relative"}, status_code=400)

    def _allow():
        repo_id, _repo = _rz_repo(wt)
        ci = _red_zone_monitor.root_ci(wt)
        doc = _rz_doc(wt, repo_id, ci=ci)
        if not doc["green"]:
            return 409, "this worktree has no green zones — nothing to allow", None
        if _red_zones.classify(rel, None, doc, ci) == "blocked":
            return 409, "%s is in a red zone — remove that zone first" % rel, None
        with _red_zone_monitor.route_write():
            zone = _red_zones.add_zone(
                "worktree",
                os.path.realpath(wt),
                "/" + _red_zones.glob_escape(rel),  # exactly this path
                note="allowed from a scope request",
                repo_id=repo_id,
                kind="green",
            )
        _red_zone_monitor.resync([(os.path.realpath(wt), wt)], repo_id)
        return 200, zone, _red_zones.effective_zones(wt, repo_id)

    try:
        status, zone, zones = await asyncio.to_thread(_allow)
    except ValueError as err:
        return JSONResponse({"error": str(err)}, status_code=400)
    except LookupError as err:
        return JSONResponse({"error": str(err)}, status_code=409)
    if status != 200:
        return JSONResponse({"error": zone}, status_code=status)
    told, reason = False, None
    if p.get("tell_agent", True) is not False:
        msg = (
            "MindFlock: the user allowed `%s` — you may now edit it (it is part "
            "of your green-zone scope)." % rel
        )
        told, reason = await asyncio.to_thread(
            lambda: _deliver_to_agent(inst, title, msg)
        )
    body = {"ok": True, "zone": dict(zone, kind="green"), "zones": zones, "told": told}
    if reason:
        body["reason"] = reason
    return JSONResponse(body)


@app.post("/api/instances/{title}/red-zones/exempt")
async def instance_red_zones_exempt(
    title: str, payload: Optional[dict] = None
) -> JSONResponse:
    """The green exemption choice: ``{"exempt": false, "paths"?: [...]}`` =
    "Treat as breaches" (drop those / all exemptions); ``{"exempt": true,
    "paths": [...]}`` = exempt those paths at their current content.
    Answers ``{"ok", "exempt": {rel: sha}}``."""
    if not git_available():
        return _no_git_response()
    _inst, wt, err = _wt_or_409(title)
    if err is not None:
        return err
    p = payload or {}
    raw = p.get("paths")
    paths = None
    if raw is not None:
        if not isinstance(raw, list):
            return JSONResponse({"error": "paths must be a list"}, status_code=400)
        paths = [r for r in (_clean_rel_arg(x) for x in raw) if r]

    def _set():
        repo_id, _repo = _rz_repo(wt)
        with _red_zone_monitor.route_write():
            if p.get("exempt", True) is False:
                left = _red_zones.drop_green_exempt(wt, paths)
            else:
                blobs = _rz_exempt_ids(os.path.realpath(wt), paths or [])
                left = _red_zones.set_green_exempt(wt, blobs)
        _red_zone_monitor.resync([(os.path.realpath(wt), wt)], repo_id)
        return left

    return JSONResponse({"ok": True, "exempt": await asyncio.to_thread(_set)})


@app.post("/api/instances/{title}/red-zones/preview")
async def instance_red_zones_preview(
    title: str, payload: Optional[dict] = None
) -> JSONResponse:
    """Dry run a pattern over this worktree (nothing is saved): ``{"re",
    "count", "sample", "ignored_count", "changed", "truncated"}``; 400 for a
    pattern that could never be a zone.

    ``"kind": "green"`` adds what a SCOPE needs to be checked against:
    ``writable_files`` (files it covers), ``changed_outside`` (changed paths
    that would be outside the scope with it added), ``committed_outside``
    (how many of those are committed), ``roots`` (the distinct top paths it
    matches — an unanchored ``tests`` matching ``tests/`` AND
    ``backend/tests/`` shows both), ``unanchored`` + ``anchored`` (the
    anchored twin, offered when it matches exactly one root) and
    ``warnings``."""
    if not git_available():
        return _no_git_response()
    inst, wt, err = _wt_or_409(title)
    if err is not None:
        return err
    pattern = str((payload or {}).get("pattern") or "")
    kind = str((payload or {}).get("kind") or "red")
    try:
        res = await asyncio.to_thread(_code_map.preview, wt, pattern, inst)
    except ValueError as err:
        return JSONResponse({"error": str(err)}, status_code=400)
    if kind == "green":
        try:
            res = dict(res)
            res.update(await asyncio.to_thread(_green_preview, inst, wt, pattern, res))
        except Exception as err:  # noqa: BLE001 — the base preview still stands
            res.setdefault("warnings", []).append("green check failed: %s" % err)
    return JSONResponse(res)


@app.delete("/api/instances/{title}/red-zones/{zone_id}")
async def instance_red_zones_delete(
    title: str, zone_id: str, request: Request, tell_agent: Optional[bool] = None
) -> JSONResponse:
    """Remove a zone (whatever its scope) and re-sync the guards it reached.
    Removing a GREEN zone narrows the scope: work already done inside it is
    exempted (it was legitimate), and the agent is told (``?tell_agent=0``
    to skip). A red removal never messages the agent (as in v2).

    Only a zone scoped to THIS session's worktree is part of driving the
    session; any other (a repo zone, another worktree's) is the owner's only
    (``auth.may_configure``, 403)."""
    if not git_available():
        return _no_git_response()
    inst, wt, err = _wt_or_409(title)
    if err is not None:
        return err
    where = await asyncio.to_thread(_red_zones.find_zone, zone_id)
    if where is None:
        return JSONResponse({"error": "unknown zone: %s" % zone_id}, status_code=404)
    own = where["scope"] == "worktree" and os.path.realpath(
        where["owner"]
    ) == os.path.realpath(wt)
    if not own and not await _auth.may_configure(request.scope):
        return _auth.configure_refused()

    def _del():
        repo_id, _repo = _rz_repo(wt)
        ci = _red_zone_monitor.root_ci(wt)
        old_doc = _rz_doc(wt, repo_id, ci=ci)
        with _red_zone_monitor.route_write():
            res = _red_zones.remove_zone(zone_id)
            if res is not None and res.get("kind") == "green":
                new_doc = _rz_doc(wt, repo_id, ci=ci)
                if new_doc["green"]:
                    _rz_green_exempt(inst, wt, old_doc, new_doc)
        if res is None:
            return None
        _rz_resync_after(res, wt)
        return res, _red_zones.effective_zones(wt, repo_id)

    out = await asyncio.to_thread(_del)
    if out is None:
        return JSONResponse({"error": "unknown zone: %s" % zone_id}, status_code=404)
    res, zones = out
    green = [z for z in zones if z.get("kind") == "green"]
    told = False
    tell = tell_agent if tell_agent is not None else res.get("kind") == "green"
    body: dict = {"ok": True, "zones": zones}
    if tell and res.get("kind") == "green":
        msg = _red_zones.green_scope_message(res["zone"], green, hard=_rz_hard(inst))
        told, reason = await asyncio.to_thread(
            lambda: _deliver_to_agent(inst, title, msg)
        )
        if reason:
            body["reason"] = reason
    body["told"] = told
    return JSONResponse(body)


@app.post("/api/instances/{title}/red-zones/{zone_id}/waive")
async def instance_red_zones_waive(
    title: str, zone_id: str, payload: Optional[dict] = None
) -> JSONResponse:
    """Waive ("Allow here") — or un-waive, ``{"waived": false}`` — a REPO zone
    for this worktree only — shown on the map, not enforced here. A worktree zone
    is simply removed instead (400)."""
    if not git_available():
        return _no_git_response()
    _inst, wt, err = _wt_or_409(title)
    if err is not None:
        return err
    waived = bool((payload or {}).get("waived", True))

    def _waive():
        repo_id, _repo = _rz_repo(wt)
        zone = next(
            (
                z
                for z in _red_zones.effective_zones(wt, repo_id)
                if z.get("id") == zone_id
            ),
            None,
        )
        if zone is None:
            return 404, None
        if zone.get("scope") != "repo":
            return 400, None
        with _red_zone_monitor.route_write():
            _red_zones.set_waiver(wt, zone_id, waived)
        _red_zone_monitor.resync([(os.path.realpath(wt), wt)], repo_id)
        return 200, _red_zones.effective_zones(wt, repo_id)

    status, zones = await asyncio.to_thread(_waive)
    if status == 404:
        return JSONResponse({"error": "unknown zone: %s" % zone_id}, status_code=404)
    if status == 400:
        return JSONResponse(
            {"error": "only repo zones can be waived — remove a worktree zone"},
            status_code=400,
        )
    return JSONResponse({"ok": True, "zones": zones})


@app.get("/api/red-zones")
async def red_zones_all() -> JSONResponse:
    """Every repo's zones + Plan-first flag, for Red zones… (outside sessions)."""
    return JSONResponse({"repos": await asyncio.to_thread(_red_zones.all_repos)})


@app.post("/api/red-zones")
async def red_zones_add(
    request: Request, payload: Optional[dict] = None
) -> JSONResponse:
    """Add a REPO zone by repo id (``{"repo_id", "pattern", "name", "note",
    "label"}``) and re-sync every live worktree of that repo.

    The repo-scope routes here change what agents may edit in every worktree
    of the repo, and settings sync spreads them: the owner's only
    (``auth.may_configure``). A session's own zones (``/api/instances/…``)
    are part of driving that session."""
    if not await _auth.may_configure(request.scope):
        return _auth.configure_refused()
    p = payload or {}
    repo_id = str(p.get("repo_id") or "").strip()
    if not repo_id:
        return JSONResponse({"error": "repo_id is required"}, status_code=400)
    if str(p.get("kind") or "red") != "red":
        return JSONResponse(
            {
                "error": "green zones are worktree-scope only — add one from the "
                "session's Map, not for the whole repo"
            },
            status_code=400,
        )

    def _add():
        with _red_zone_monitor.route_write():
            zone = _red_zones.add_zone(
                "repo",
                repo_id,
                str(p.get("pattern") or ""),
                name=str(p.get("name") or ""),
                note=str(p.get("note") or ""),
                label=str(p.get("label") or ""),
            )
        _red_zone_monitor.resync(_live_repo_roots(repo_id), repo_id)
        return zone, _red_zones.all_repos()

    try:
        zone, repos = await asyncio.to_thread(_add)
    except ValueError as err:
        return JSONResponse({"error": str(err)}, status_code=400)
    except LookupError as err:  # ZoneConflict: already green in a worktree
        return JSONResponse({"error": str(err)}, status_code=409)
    return JSONResponse({"ok": True, "zone": zone, "repos": repos})


@app.get("/api/red-zones/companions")
async def red_zones_companions(repo_id: str = "") -> JSONResponse:
    """A repo's companion patterns — "derived outputs" an agent may write
    outside its green scope (a built bundle, generated clients) — plus the
    built-in defaults (lockfiles, snapshots) for display."""
    rid = str(repo_id or "").strip()
    if not rid:
        return JSONResponse({"error": "repo_id is required"}, status_code=400)
    pats = await asyncio.to_thread(_red_zones.companions_config, rid)
    return JSONResponse(
        {
            "repo_id": rid,
            "patterns": pats,
            "defaults": list(_red_zones.DEFAULT_COMPANIONS),
        }
    )


@app.put("/api/red-zones/companions")
async def red_zones_companions_set(
    request: Request, payload: Optional[dict] = None
) -> JSONResponse:
    """Replace a repo's companion patterns: ``{"repo_id", "patterns": [...],
    "label"?}``. 400 on a missing repo id or any invalid pattern (nothing is
    saved). Live worktrees of the repo are re-synced at once."""
    if not await _auth.may_configure(request.scope):
        return _auth.configure_refused()
    p = payload or {}
    rid = str(p.get("repo_id") or "").strip()
    pats = p.get("patterns")
    if not rid:
        return JSONResponse({"error": "repo_id is required"}, status_code=400)
    if not isinstance(pats, list) or not all(isinstance(x, str) for x in pats):
        return JSONResponse(
            {"error": "patterns must be a list of strings"}, status_code=400
        )

    def _set():
        with _red_zone_monitor.route_write():
            out = _red_zones.set_companions(rid, pats, label=str(p.get("label") or ""))
        _red_zone_monitor.resync(_live_repo_roots(rid), rid)
        return out

    try:
        stored = await asyncio.to_thread(_set)
    except ValueError as err:
        return JSONResponse({"error": str(err)}, status_code=400)
    return JSONResponse(
        {
            "ok": True,
            "repo_id": rid,
            "patterns": stored,
            "defaults": list(_red_zones.DEFAULT_COMPANIONS),
        }
    )


@app.post("/api/red-zones/plan-first")
async def red_zones_plan_first(
    request: Request, payload: Optional[dict] = None
) -> JSONResponse:
    """Turn a repo's Plan-first flag on/off: its intake sessions (tickets,
    issues, PR reviews) open with the plan-first instruction."""
    if not await _auth.may_configure(request.scope):
        return _auth.configure_refused()
    p = payload or {}
    repo_id = str(p.get("repo_id") or "").strip()
    if not repo_id:
        return JSONResponse({"error": "repo_id is required"}, status_code=400)

    def _set():
        with _red_zone_monitor.route_write():
            _red_zones.set_plan_first(
                repo_id, bool(p.get("on")), label=str(p.get("label") or "")
            )
        return _red_zones.all_repos()

    return JSONResponse({"ok": True, "repos": await asyncio.to_thread(_set)})


@app.delete("/api/red-zones/{zone_id}")
async def red_zones_delete(zone_id: str, request: Request) -> JSONResponse:
    """Remove a zone by id (any scope) and re-sync the guards it reached."""
    if not await _auth.may_configure(request.scope):
        return _auth.configure_refused()

    def _del():
        with _red_zone_monitor.route_write():
            res = _red_zones.remove_zone(zone_id)
        if res is None:
            return None
        _rz_resync_after(res)
        return _red_zones.all_repos()

    repos = await asyncio.to_thread(_del)
    if repos is None:
        return JSONResponse({"error": "unknown zone: %s" % zone_id}, status_code=404)
    return JSONResponse({"ok": True, "repos": repos})


# --------------------------------------------------------------------------- #
# Send a message to an agent + the per-session prompt queue (M-series).
#
# ``/send`` is the one-off primitive: type a message into an agent window and
# submit it — independent of the ticket-ingestion pipeline, so you can kick a
# fresh session into motion (and start spending its token budget) with a single
# line. The queue endpoints build on the same send primitive: a FIFO the drain
# loop feeds in the background so a run keeps rolling across usage outages.
# --------------------------------------------------------------------------- #
def _agent_session_ready(inst, title: str):
    """Ensure the agent tmux session exists (rebooting a dead one), returning
    ``(name, error_or_None)``. Shared by the /send endpoint (instance_send) and
    the queue /send_now endpoint (post_queue_send_now); the drain loop calls
    _ensure_agent_session directly."""
    return _ensure_agent_session(inst, title)


#: Activities in which typing into the agent's window lands in the wrong place:
#: a clarify/permission prompt (the text would ANSWER it), the usage-limit menu
#: (it would pick a menu item), or a turn in progress (it would interleave with
#: the running turn). The message is queued instead and the drain delivers it
#: the moment the agent is idle.
_DELIVER_QUEUE_ACTIVITIES = ("clarify", "limit", "working")
_SEND_FAILED = "failed to send to agent session"


#: ``_deliver_to_agent``'s reason when a ``dialog_safe`` paste found the
#: agent on a prompt: an unsubmitted paste can't be queued (the drain would
#: submit it), so nothing happens and the caller says why.
_SEND_IN_DIALOG = "Answer its prompt first — typing now would answer the dialog"
#: ``_deliver_to_agent``'s reason when the agent was asleep and the text
#: could not wait on its queue (an unsubmitted paste, or a looping queue that
#: would re-send it forever): it is waking, and the caller sends again.
_SEND_WAKING = "It was asleep — waking it now; send again in a few seconds"


def _say_kw(say: bool) -> dict:
    """``enqueue``'s ``say`` keyword, passed only when true: a queued text
    asks for a spoken reply only when its ``/send`` did."""
    return {"say": True} if say else {}


def _deliver_to_sleeper(
    inst, title: str, text: str, *, boot: bool, submit: bool, say: bool = False
):
    """``_deliver_to_agent`` for a SLEEPING session (core.sleep): wake it,
    then put the text on its prompt queue — at the front for a person's
    ``/send``, at the back for a MindFlock-composed message — because a CLI
    that is still booting loses whatever is typed at it. The wake opened the
    queue's boot grace, so the drain sends it once the CLI has drawn its
    prompt and settled idle. Not queueable (an unsubmitted paste; a looping
    queue, which would re-send it forever): it just wakes →
    ``(False, _SEND_WAKING)``. ``say`` rides the queue entry (armed when the
    drain types it)."""
    _woke, err = _sleep.ensure_awake(
        inst,
        title,
        "a prompt was sent" if boot else "MindFlock sent it a message",
    )
    if err is not None:
        return False, err
    if not submit or _prompt_queue.get_state(title)["loop"]:
        return False, _SEND_WAKING
    try:
        _prompt_queue.enqueue(title, text, index=0 if boot else None, **_say_kw(say))
    except ValueError as qerr:
        return False, str(qerr)
    _emit_queue_changed(title)
    return "queued", None


def _deliver_to_agent(
    inst,
    title: str,
    text: str,
    *,
    boot: bool = False,
    submit: bool = True,
    dialog_safe: bool = False,
    say: bool = False,
):
    """Type ``text`` into ``title``'s agent → ``(told, reason)``.

    ``told`` is ``"sent"``, ``"queued"`` or ``False`` (with ``reason``).

    ``boot=True`` is ``/send``'s contract: (re)boot a dead agent session, then
    type unconditionally — the human at the send box is looking at the pane.
    ``boot=False`` is for messages MindFlock composes on a button press (the
    Code Map's plan/go/zone notices): it never reboots an agent (a message that
    silently relaunches a session the user closed is worse than "not running"),
    reports an over-budget session instead of 409ing, and QUEUES instead of
    typing while the agent is mid-turn or waiting on a prompt.

    ``dialog_safe`` (with ``boot=True``: ``/send``'s opt-in for the UI's own
    buttons) keeps ``/send``'s boot and its typing into a working agent, but
    re-probes the activity UNCACHED first — and checks the SCREEN for a
    dialog, which outranks the reading — and never types into a prompt or
    the usage-limit menu: a submitted message is queued instead (the drain
    delivers it once the agent is free), an unsubmitted paste is refused
    with :data:`_SEND_IN_DIALOG` (queueing it would submit it later). The
    browser's view of the agent lags by a poll or two; a digit or an Enter
    typed into a permission prompt approves it.

    A SLEEPING agent (core.sleep) is woken either way, and the text queued
    rather than typed into the booting CLI (:func:`_deliver_to_sleeper`).

    A PAUSED session is never typed into nor relaunched, either way:
    ``(False, PAUSED_REFUSED)`` — Resume starts its agent again.

    ``say`` (``/send``'s, for a submitted text) asks for THIS reply's
    ``Say:`` line (:func:`_voice_say`) at the last moment — right before the
    keystrokes, after every dialog/queue decision; a queued text carries it
    in its queue entry instead, and a failed send disarms it again.

    Blocking (tmux + an activity probe) — call it via ``asyncio.to_thread``.
    Every collaborator is read off this module so tests patch the server.
    """
    if getattr(inst, "Status", None) == Paused:
        return False, PAUSED_REFUSED
    say = bool(say and submit)
    if _sleep.is_sleeping(title):
        if not boot and _budget_locked(title):
            return False, "over budget"
        return _deliver_to_sleeper(inst, title, text, boot=boot, submit=submit, say=say)
    if boot:
        name, err = _agent_session_ready(inst, title)
        if err is not None:
            return False, err
        if dialog_safe:
            try:
                activity = _agent_activity(inst, title)
            except Exception:  # noqa: BLE001 — unknown reads as "don't type blind"
                activity = "unknown"
            if (
                activity in _PB_DIALOG_ACTIVITIES
                or activity == "unknown"
                or _dialog_on_screen(inst, name)
            ):
                if not submit:
                    return False, _SEND_IN_DIALOG
                try:
                    _prompt_queue.enqueue(title, text, **_say_kw(say))
                except ValueError as err:
                    return False, str(err)
                _emit_queue_changed(title)
                return "queued", None
    else:
        if _budget_locked(title):
            return False, "over budget"
        name = _live_session_name(tmux.to_mindflock_tmux_name(title))
        if name is None:
            return False, "the agent isn't running — open the session to start it"
        try:
            activity = _agent_activity(inst, title)
        except Exception:  # noqa: BLE001 — unknown reads as "don't type blind"
            activity = "working"
        if activity in _DELIVER_QUEUE_ACTIVITIES or _dialog_on_screen(inst, name):
            try:
                _prompt_queue.enqueue(title, text, **_say_kw(say))
            except ValueError as err:
                return False, str(err)
            _emit_queue_changed(title)
            return "queued", None
    # Only reachable from human surfaces (the send box, the palette, the
    # prompts dialog, the Map's buttons) — automation types via
    # _send_queued_item and the limit watcher, never through here.
    _note_human_input(title)
    typed = _voice_say(inst, title, text) if say else text
    if not _send_to_agent(name, typed, submit):
        if say:
            _voice_say_cancel(title)
        return False, _SEND_FAILED
    return "sent", None


#: ``_enter_on_agent``'s reason for a sleeping session: an Enter has no text
#: to queue, and the draft it would submit was typed into a CLI that is gone.
_ENTER_ASLEEP = "It is asleep — open it, then dictate again"
_ENTER_NOT_RUNNING = "the agent isn't running — open the session to start it"


def _enter_on_agent(inst, title: str, *, say: bool = False):
    """Press Enter on whatever is in ``title``'s input field → ``(told,
    reason)`` like :func:`_deliver_to_agent` (``told`` is ``"sent"`` or
    False). ``/send``'s ``{"enter": true}`` form: the desktop app types live
    dictation straight into the terminal over its own websocket, so the
    server never sees the draft — only the closing "submit".

    The checks of a ``dialog_safe`` send, minus everything that needs text:
    paused → :data:`PAUSED_REFUSED`; asleep or not running → refused, never
    queued nor booted (there is nothing to queue, and a fresh CLI has no
    draft); a dialog or the usage-limit menu on screen (or an unreadable
    activity) → :data:`_SEND_IN_DIALOG` with nothing pressed — an Enter
    there would answer it. ``say`` arms the one-shot ``Say:`` request
    (:func:`_voice_say` on an empty text: Claude's ``.next``, or the
    instruction typed after the draft for a hook-less CLI) right before the
    keystroke; the Enter is :func:`_send_to_agent`'s own, so a TUI sees a
    real Enter a beat after the last key. Blocking — call via
    ``asyncio.to_thread``."""
    if getattr(inst, "Status", None) == Paused:
        return False, PAUSED_REFUSED
    if _sleep.is_sleeping(title):
        return False, _ENTER_ASLEEP
    name = _live_session_name(tmux.to_mindflock_tmux_name(title))
    if name is None:
        return False, _ENTER_NOT_RUNNING
    try:
        activity = _agent_activity(inst, title)
    except Exception:  # noqa: BLE001 — unknown reads as "don't press blind"
        activity = "unknown"
    if (
        activity in _PB_DIALOG_ACTIVITIES
        or activity == "unknown"
        or _dialog_on_screen(inst, name)
    ):
        return False, _SEND_IN_DIALOG
    _note_human_input(title)
    typed = _voice_say(inst, title, "") if say else ""
    if not _send_to_agent(name, typed, True):
        if say:
            _voice_say_cancel(title)
        return False, _SEND_FAILED
    return "sent", None


def _driven_elsewhere(request, title: str, payload) -> Optional[JSONResponse]:
    """409 ``{"error": "driven by <name>", "driver"}`` when another person
    holds ``title``'s keyboard (core.ownership) — the gate on every route that
    types into the terminal for a person (``/send``, the prompt queue's add and
    Send now, ``/answer``). ``{"take": true}`` in the body takes the keyboard
    first; the driver themself (and anyone while nobody drives) passes. None
    to proceed — always for an in-process call (``request`` None: the
    server's own automation, never a person's keyboard)."""
    if request is None:
        return None
    person, device = _ownership.caller(request)
    want = isinstance(payload, dict) and payload.get("take") is True
    body = _ownership.gate(title, person, device, want=want)
    return None if body is None else JSONResponse(body, status_code=409)


@app.post("/api/instances/{title}/driver")
async def instance_driver(title: str, request: Request, payload: dict = None):
    """Take or release the session's keyboard (core.ownership).
    ``{"take": true}`` makes the caller's person the driver — anyone who can
    reach the session may; the previous driver is told by a
    ``session.driver_changed`` event. ``{"release": true}`` gives it up (409
    when someone else drives). → ``{owner, driver, driver_name, viewers,
    previous?}``; 404 unknown, 400 neither."""
    if ENGINE.instances.get(title) is None:
        return JSONResponse({"error": "instance not found"}, status_code=404)
    payload = payload or {}
    person, device = _ownership.caller(request)
    if payload.get("take") is True:
        prev = _ownership.take(title, person, device)
        return JSONResponse({**_ownership.row_fields(title), "previous": prev})
    if payload.get("release") is True:
        cur = _ownership.driver(title)
        if cur and cur != person:
            body = _ownership.busy(title, person) or {"error": "not the driver"}
            return JSONResponse(body, status_code=409)
        _ownership.release(title, person)
        return JSONResponse(_ownership.row_fields(title))
    return JSONResponse(
        {"error": 'pass {"take": true} or {"release": true}'}, status_code=400
    )


@app.post("/api/instances/{title}/owner")
async def instance_owner(title: str, request: Request, payload: dict = None):
    """Give the session to another person: ``{"person": "<id>"}``. The owner
    otherwise never changes (it is the person the session was created for).
    The device owner's only (``auth.may_configure``, 403). → the presence
    fields; 404 unknown, 400 not a person id."""
    if not await _auth.may_configure(request.scope):
        return JSONResponse(
            {"error": "only this device's owner can change a session's owner"},
            status_code=403,
        )
    if ENGINE.instances.get(title) is None:
        return JSONResponse({"error": "instance not found"}, status_code=404)
    try:
        _ownership.set_owner(title, str((payload or {}).get("person") or ""))
    except ValueError as err:
        return JSONResponse({"error": str(err)}, status_code=400)
    return JSONResponse(_ownership.row_fields(title))


def repin_voice_hooks() -> int:
    """Re-pin the activity hooks of every live session whose CLI has a prompt
    hook (Claude), so a session launched by an older build picks up the hook
    that reads the standing ``say-all`` marker (Claude Code hot-reloads the
    file). Idempotent and best-effort; returns how many were re-pinned. Run
    when the marker is turned on, and at boot while it is on."""
    done = 0
    for title, inst in list(ENGINE.instances.items()):
        try:
            prov = providers.resolve(getattr(inst, "Program", "") or "")
            if not prov.voice_prompt_hook():
                continue
            wt = inst.GetWorktreePath() or ""
            if not wt:
                continue
            prov.install_activity_hooks(wt, tmux.to_mindflock_tmux_name(title))
            done += 1
        except Exception:  # noqa: BLE001 — one session's hooks never block the rest
            continue
    return done


def _voice_suffix(text: str) -> str:
    """``text`` with the voice instruction typed after it — how a CLI without
    a prompt hook (Codex, the config-driven ones) is asked for a ``Say:``
    line. Idempotent: a text that already ends with it is left alone."""
    if text.rstrip().endswith(_spoken.INSTRUCTION):
        return text
    return text.rstrip("\n") + "\n\n" + _spoken.INSTRUCTION


def _voice_say(inst, title: str, text: str) -> str:
    """Ask ``title``'s agent to end the reply to ``text`` with a ``Say:``
    line → the text to type. Called right before the keystrokes (the
    delivery, the queue drain, Send now) for a prompt sent with ``say:
    true`` — never at request time, so a queued prompt does not arm early.

    A CLI whose provider answers ``voice_prompt_hook()`` (Claude) gets the
    ONE-SHOT ``.next`` request its UserPromptSubmit hook consumes
    (``core.voice.arm_next``), after a re-pin of its hooks so a session
    launched by an older build (whose hook still reads only the sticky
    ``.on``) gets the hook that knows ``.next`` — Claude Code hot-reloads
    the file. Every other CLI, or an arm that fails, gets the instruction
    typed after the prompt (:func:`_voice_suffix`). Never raises: the prompt
    is never lost over the request."""
    try:
        prov = providers.resolve(getattr(inst, "Program", "") or "")
        if not prov.voice_prompt_hook():
            return _voice_suffix(text)
        name = tmux.to_mindflock_tmux_name(title)
        try:
            wt = inst.GetWorktreePath() or ""
            if wt:
                # Idempotent (the merge skips an unchanged file), best-effort.
                prov.install_activity_hooks(wt, name)
        except Exception:  # noqa: BLE001 — the hooks are enrichment
            pass
        try:
            _voice.arm_next(name)
        except OSError:
            return _voice_suffix(text)  # unarmable: say it in the prompt
        return text
    except Exception:  # noqa: BLE001 — never lose the prompt over the request
        return text


def _voice_say_cancel(title: str) -> None:
    """Undo :func:`_voice_say`'s arm after a send that never landed, so the
    request cannot ride the NEXT prompt. Never raises."""
    try:
        _voice.disarm_next(tmux.to_mindflock_tmux_name(title))
    except Exception:  # noqa: BLE001
        pass


@app.post("/api/instances/{title}/send")
async def instance_send(
    title: str, payload: dict, request: Request = None
) -> JSONResponse:
    """Send a single message to the session's agent window and (by default)
    submit it. Boots/resumes the agent session first if it isn't running, so a
    just-created session starts working from one call. ``submit=false`` types
    the text without pressing Enter (leave the user to review/edit).

    ``dialog_safe=true`` (the UI's Send now and every paste): re-check the
    agent's live activity first and never type into a prompt or the
    usage-limit menu — a submitted message is queued instead (→ ``{"sent":
    false, "queued": true, "submitted": false}``), an unsubmitted paste is
    409 ``{"in_dialog": true}`` with nothing typed.

    Another person holding the keyboard (core.ownership) is 409 ``{"error":
    "driven by <name>", "driver"}`` unless ``"take": true``. A paused session
    is 409 ``{"error": "session is paused — resume it first", "paused":
    true}``: nothing is typed and the agent is not relaunched.

    ``say=true`` (with ``submit``) asks for THIS reply's ``Say:`` line — the
    client decides per prompt (phone dictation yes, typing no): Claude's
    prompt hook gets a one-shot request armed right before the keystrokes,
    any other CLI the instruction typed after the text (:func:`_voice_say`).
    A queued message keeps it in its queue entry and is armed when the
    drain types it. Absent/false, or ``submit=false``: never asked for.

    ``{"enter": true}`` with NO (or empty) ``text``: press Enter on what is
    already in the input field — the desktop app types live dictation into
    the terminal itself, then submits through here so ``say`` is armed the
    same way (:func:`_enter_on_agent`). Always dialog-safe: a dialog or the
    usage-limit menu on screen is 409 ``{"in_dialog": true}`` with nothing
    pressed; asleep or not running is 409 (never queued, never booted);
    paused, over budget and driven-elsewhere as above. → ``{"sent": true,
    "submitted": true, "entered": true}``. ``enter`` with text, or with
    ``submit: false``, is 400."""
    inst, err = _inst_or_404(title)
    if err is not None:
        return err
    text = str((payload or {}).get("text", "") or "")
    enter = (payload or {}).get("enter") is True
    submit = bool((payload or {}).get("submit", True))
    if enter and (text.strip() or not submit):
        return JSONResponse(
            {"error": "enter presses Enter on the draft: no text, submit not false"},
            status_code=400,
        )
    if not enter and not text.strip():
        return JSONResponse({"error": "empty message"}, status_code=400)
    dialog_safe = bool((payload or {}).get("dialog_safe", False))
    # "Read everything" on: every prompt asks for its Say line, not only the
    # ones a client marks (core.voice.say_all).
    say = submit and ((payload or {}).get("say") is True or _voice.say_all())
    busy = _driven_elsewhere(request, title, payload)
    if busy is not None:
        return busy
    if _budget_locked(title):
        return JSONResponse(
            {
                "error": "session is over budget — raise the budget to send",
                "budget_locked": True,
            },
            status_code=409,
        )
    asleep = _sleep.is_sleeping(title)
    if enter:
        told, reason = await asyncio.to_thread(
            lambda: _enter_on_agent(inst, title, say=say)
        )
    else:
        told, reason = await asyncio.to_thread(
            lambda: _deliver_to_agent(
                inst,
                title,
                text,
                boot=True,
                submit=submit,
                dialog_safe=dialog_safe,
                say=say,
            )
        )
    if told is False:
        body = {"error": reason}
        if reason == _SEND_IN_DIALOG:
            body["in_dialog"] = True
        if reason == _SEND_WAKING:
            body["waking"] = True
        if reason == PAUSED_REFUSED:
            body["paused"] = True
        return JSONResponse(body, status_code=502 if reason == _SEND_FAILED else 409)
    if told == "queued":
        body = {"sent": False, "queued": True, "submitted": False}
        if asleep:
            body["woke"] = True  # it was asleep: it goes in once the agent is back
        return JSONResponse(body)
    if enter:
        return JSONResponse({"sent": True, "submitted": True, "entered": True})
    return JSONResponse({"sent": True, "submitted": submit})


def _queue_state_json(title: str) -> dict:
    """The queue entry shaped for the API/UI (pending items + flags)."""
    st = _prompt_queue.get_state(title)
    return {
        "items": st["items"],
        "loop": st["loop"],
        # Minutes between looped sends (0 = immediate). Powers the timed loop.
        "loop_interval": st["loop_interval"],
        "enabled": st["enabled"],
        # Hold + auto-resume across usage limits (True) vs. stop when limited.
        "wait_for_limit": st["wait_for_limit"],
        "last_sent": st["last_sent"],
        "pending": len(st["items"]),
        # Epoch the queue is holding until because the agent is usage-limited
        # (0 = not limited). Lets the panel show a "resets in MM:SS" countdown.
        "limited_until": _session_limited_until(title),
    }


def _emit_queue_changed(title: str) -> None:
    """Emit ``session.queue_changed`` with the pending count + drain/loop flags."""
    st = _prompt_queue.get_state(title)
    _events.BUS.emit(
        "session.queue_changed",
        session=title,
        data={
            "pending": len(st["items"]),
            "enabled": st["enabled"],
            "loop": st["loop"],
        },
    )


@app.get("/api/instances/{title}/queue")
def get_queue(title: str) -> JSONResponse:
    """The session's pending prompt queue and its drain/loop flags."""
    return JSONResponse(_queue_state_json(title))


@app.post("/api/instances/{title}/queue")
def post_queue(title: str, payload: dict, request: Request = None) -> JSONResponse:
    """Add prompts to the queue. ``{"text": "..."}`` appends one; an optional
    ``index`` (0-based, clamped) inserts it at that position instead — the
    "add above/below this item" path. ``{"texts": ["...", ...]}`` bulk-appends
    (one write) — the drop-a-CSV path — skipping blank rows and dropping
    whatever exceeds the queue cap; the response then carries ``added`` and
    ``skipped`` counts on top of the queue state."""
    if ENGINE.instances.get(title) is None:
        return JSONResponse(
            {"error": "instance not found: %s" % title}, status_code=404
        )
    payload = payload or {}
    busy = _driven_elsewhere(request, title, payload)
    if busy is not None:
        return busy
    texts = payload.get("texts")
    if isinstance(texts, list):
        entry, added, skipped = _prompt_queue.enqueue_many(
            title, [str(t) for t in texts]
        )
        if not added and not skipped:
            return JSONResponse({"error": "no prompts in payload"}, status_code=400)
        if added:
            _emit_queue_changed(title)
        body = _queue_state_json(title)
        body["added"] = added
        body["skipped"] = skipped
        return JSONResponse(body)
    text = str(payload.get("text", ""))
    if not text.strip():
        return JSONResponse({"error": "empty prompt"}, status_code=400)
    index = payload.get("index")
    if index is not None:
        try:
            index = int(index)
        except (TypeError, ValueError):
            index = None
    try:
        _prompt_queue.enqueue(title, text, index=index)
    except ValueError as err:
        return JSONResponse({"error": str(err)}, status_code=400)
    _emit_queue_changed(title)
    return JSONResponse(_queue_state_json(title))


@app.post("/api/instances/{title}/queue/flags")
def post_queue_flags(title: str, payload: dict) -> JSONResponse:
    """Toggle ``enabled`` (auto-drain on/off), ``loop`` (re-queue sent prompts so
    a self-improving prompt cycles forever), ``loop_interval`` (minutes between
    looped sends; 0 = immediate), and/or ``wait_for_limit`` (hold + auto-resume
    across usage limits vs. stop when the agent is limited)."""
    payload = payload or {}
    enabled = payload.get("enabled")
    loop = payload.get("loop")
    loop_interval = payload.get("loop_interval")
    if loop_interval is not None:
        try:
            loop_interval = max(0, int(loop_interval))
        except (TypeError, ValueError):
            loop_interval = None
    wait_for_limit = payload.get("wait_for_limit")
    _prompt_queue.set_flags(
        title,
        enabled=None if enabled is None else bool(enabled),
        loop=None if loop is None else bool(loop),
        loop_interval=loop_interval,
        wait_for_limit=None if wait_for_limit is None else bool(wait_for_limit),
    )
    _emit_queue_changed(title)
    return JSONResponse(_queue_state_json(title))


@app.post("/api/instances/{title}/queue/reorder")
def post_queue_reorder(title: str, payload: dict) -> JSONResponse:
    """Reorder one item: ``{"id", "index": N}`` moves it to an absolute
    0-based position (clamped) — the drag-and-drop path — while
    ``{"id", "direction": "up"|"down"}`` nudges it one slot (kept for older
    clients)."""
    payload = payload or {}
    item_id = str(payload.get("id", ""))
    index = payload.get("index")
    if index is not None:
        try:
            _prompt_queue.move_item_to(title, item_id, int(index))
        except (TypeError, ValueError):
            return JSONResponse({"error": "bad index"}, status_code=400)
    else:
        _prompt_queue.move_item(title, item_id, str(payload.get("direction", "")))
    _emit_queue_changed(title)
    return JSONResponse(_queue_state_json(title))


@app.post("/api/instances/{title}/queue/edit")
def post_queue_edit(title: str, payload: dict) -> JSONResponse:
    """Replace one queued item's text in place. ``{"id": "...", "text": "..."}``.
    The item keeps its id and position, so editing never loses its turn."""
    payload = payload or {}
    text = str(payload.get("text", ""))
    if not text.strip():
        return JSONResponse({"error": "empty prompt"}, status_code=400)
    _prompt_queue.update_item(title, str(payload.get("id", "")), text)
    _emit_queue_changed(title)
    return JSONResponse(_queue_state_json(title))


@app.post("/api/instances/{title}/queue/send_now")
async def post_queue_send_now(
    title: str, payload: dict, request: Request = None
) -> JSONResponse:
    """Send one queued item to the agent NOW (manual override): skips the
    drain's idle wait, cooldowns, and the usage-limit hold — the user clicked,
    so deliver. ``{"id": "<item id>"}``. The item leaves the queue exactly as
    a drained send would (popped; re-appended when ``loop`` is on). A paused
    session is 409 ``{"paused": true}`` and the item stays queued."""
    inst, err = _inst_or_404(title)
    if err is not None:
        return err
    item_id = str((payload or {}).get("id", ""))
    it = next(
        (i for i in _prompt_queue.get_state(title)["items"] if i["id"] == item_id),
        None,
    )
    if it is None:
        return JSONResponse({"error": "queued item not found"}, status_code=404)
    busy = _driven_elsewhere(request, title, payload)
    if busy is not None:
        return busy
    if getattr(inst, "Status", None) == Paused:
        # Pause stopped the agent on purpose: Send now neither types into it
        # nor relaunches it (the item stays queued for after Resume).
        return JSONResponse({"error": PAUSED_REFUSED, "paused": True}, status_code=409)
    if _budget_locked(title):
        return JSONResponse(
            {
                "error": "session is over budget — raise the budget to send",
                "budget_locked": True,
            },
            status_code=409,
        )
    if _sleep.is_sleeping(title):
        # Asleep (core.sleep): wake it and put this item first in line — the
        # drain sends it once the CLI has drawn its prompt (typing into a
        # booting CLI loses it). A queue the user paused would not send it:
        # say so, and Send now again sends it straight away.
        _woke, err = await asyncio.to_thread(
            _sleep.ensure_awake, inst, title, "Send now"
        )
        if err is not None:
            return JSONResponse({"error": err}, status_code=409)
        st = _prompt_queue.get_state(title)
        if not st["enabled"] or st.get("held"):
            return JSONResponse(
                {"error": _SEND_WAKING, "waking": True}, status_code=409
            )
        _prompt_queue.move_item_to(title, item_id, 0)
        _emit_queue_changed(title)
        return JSONResponse({**_queue_state_json(title), "woke": True})
    name, err = await asyncio.to_thread(_agent_session_ready, inst, title)
    if err is not None:
        return JSONResponse({"error": err}, status_code=409)
    _note_human_input(title)  # the user clicked Send now
    say = it.get("say") is True or _voice.say_all()
    typed = it["text"]
    if say:
        typed = await asyncio.to_thread(_voice_say, inst, title, typed)
    ok = await asyncio.to_thread(_send_to_agent, name, typed, True)
    if not ok:
        if say:
            _voice_say_cancel(title)
        return JSONResponse(
            {"error": "failed to send to agent session"}, status_code=502
        )
    entry = _prompt_queue.record_sent(title, item_id)
    # Sync the drain's in-memory pacing so the next pass doesn't double-send
    # on top of this one (armed flips back once the agent is seen working).
    rec = _QUEUE_STATE.setdefault(
        title, {"armed": True, "sent_at": 0.0, "rebooted_at": 0.0, "idle_since": None}
    )
    rec["armed"] = False
    rec["sent_at"] = time.time()
    rec["sent_text"] = typed
    rec["idle_since"] = None
    _events.BUS.emit(
        "session.prompt_sent",
        session=title,
        data={
            "text": it["text"][:200],
            "remaining": len(entry["items"]),
            "loop": entry["loop"],
            "manual": True,
        },
    )
    _emit_queue_changed(title)
    return JSONResponse(_queue_state_json(title))


@app.delete("/api/instances/{title}/queue")
def delete_queue(title: str, item: str = "") -> JSONResponse:
    """Remove one item (``?item=<id>``) or clear the whole queue (no ``item``)."""
    if item:
        _prompt_queue.remove_item(title, item)
    else:
        _prompt_queue.clear(title)
    _emit_queue_changed(title)
    return JSONResponse(_queue_state_json(title))


# --------------------------------------------------------------------------- #
# Inter-agent messages (core.mailbox).
#
# One session's agent (via the mindflock MCP server), the CLI or any client
# leaves a message for another session. It is stored in the recipient's
# mailbox and reaches it exactly once: typed into its pane by the delivery
# lane (see _drain_one_mailbox) or fetched from its inbox, whichever is first.
# --------------------------------------------------------------------------- #
_MSG_MAX_CHARS = 20000
_MSG_DATA_MAX_BYTES = 8192
_MSG_LIST_DEFAULT = 50
_MSG_LIST_MAX = 200
_MSG_WAIT_MAX_S = 30.0
_MSG_POLL_S = 0.5
#: ``delivery: "now"`` types immediately only into these activities: an idle
#: prompt, or a running turn (the CLI queues the text in its input box). Never
#: clarify (the text would ANSWER the permission prompt) or limit (it would
#: pick a menu item), and never an offline agent (it is not booted for this).
_MSG_NOW_ACTIVITIES = ("idle", "working")
_MSG_NOT_RUNNING = (
    "the recipient's agent isn't running — it is typed in once it starts, and "
    "stays in its inbox meanwhile"
)
_MSG_STARTING = (
    "the recipient's agent is still starting — it is typed in once it is ready"
)
_MSG_WAKING = (
    "the recipient was asleep — it is waking up, and the message is typed in "
    "once it is ready"
)
_MSG_HUMAN_TYPING = (
    "someone is typing in the recipient's pane — it is typed in once they stop"
)
_MSG_WHY_WAITING = {
    "working": "the recipient is mid-turn",
    "clarify": "the recipient is waiting on a prompt",
    "limit": "the recipient is on a usage-limit screen",
    "offline": "the recipient's agent isn't running",
}


def _is_descendant(title: str, ancestor: str) -> bool:
    """Whether ``ancestor`` is on ``title``'s live ``Parent`` chain. Lineage is
    another unit's field, read defensively; a cycle or a link to a session
    that is not live ends the walk."""
    inst = ENGINE.instances.get(title)
    parent = str(getattr(inst, "Parent", "") or "") if inst is not None else ""
    seen = {title}
    while parent and parent not in seen:
        if parent == ancestor:
            return True
        seen.add(parent)
        nxt = ENGINE.instances.get(parent)
        if nxt is None:
            return False
        parent = str(getattr(nxt, "Parent", "") or "")
    return False


def _mail_deliver_now(inst, title: str, msg: dict, activities=_MSG_NOW_ACTIVITIES):
    """``delivery: "now"`` — type ``msg`` immediately when it is safe to →
    ``(result, detail)`` with result ``"delivered"`` or ``"pending"`` (left for
    the lane). Unlike the lane there is no idle settle (the sender asked for
    now, and a working agent just queues the text), but every safety gate
    still holds: no boot (nor typing inside the queue's boot grace), no
    clarify/limit, no typing over a long-poll, into a pane whose agent has
    quit (a bare shell), or over a human mid-composition. ``activities``
    narrows when "now" may type (Fast-track's failure note: ``("idle",)``).
    Blocking (tmux + an activity probe) — call it via ``asyncio.to_thread``."""
    if _typing_held(inst, title):
        return "pending", (
            "the recipient can't take input now (paused, over budget or still "
            "setting up) — it is typed in once it can"
        )
    if _mailbox.waiter_active(title):
        return "pending", "the recipient is waiting on its inbox and gets it there"
    if _sleep.is_sleeping(title):
        # Asleep (core.sleep): wake it; the lane types the message once the
        # relaunched CLI has drawn its prompt (the wake opened the boot grace).
        _woke, werr = _sleep.ensure_awake(inst, title, "a message arrived")
        if werr is not None:
            return "pending", _MSG_NOT_RUNNING
        return "pending", _MSG_WAKING
    q = _QUEUE_STATE.get(title) or {}
    if time.time() - q.get("rebooted_at", 0.0) < _QUEUE_BOOT_GRACE:
        # The queue just relaunched the CLI: a quiet relaunching pane reads
        # idle, and text typed before the input box is drawn is lost — while
        # the claim would already have said "delivered".
        return "pending", _MSG_STARTING
    if _human_at_keys(title, probe_tmux=False):
        return "pending", _MSG_HUMAN_TYPING
    name = _live_session_name(tmux.to_mindflock_tmux_name(title))
    if name is None:
        return "pending", _MSG_NOT_RUNNING
    if not _pane_holds_agent(name, getattr(inst, "Program", "") or ""):
        return "pending", _MSG_NOT_RUNNING
    try:
        activity = _agent_activity(inst, title)
    except Exception:  # noqa: BLE001 — unknown reads as "don't type blind"
        activity = "unknown"
    if activity not in activities:
        why = _MSG_WHY_WAITING.get(activity, "the recipient's state is unknown")
        return "pending", "%s — typed in once it is idle" % why
    if _tmux_client_input_recent(title, _HUMAN_HOLD_S):
        return "pending", _MSG_HUMAN_TYPING
    state = _mail_type(inst, title, name, msg, time.time())
    if state == _MAIL_ON_PROMPT:
        return "pending", "%s — typed in once it is idle" % _MSG_WHY_WAITING["clarify"]
    if state == "delivered":
        return "delivered", ""
    if state == "held":
        return "delivered", _mailbox.long_notice_detail()
    cur = _mailbox.get(title, msg["id"]) or {}
    if cur.get("state") == "read":
        return "delivered", "already read from the recipient's inbox"
    return "pending", "could not type it now — the delivery lane retries"


def _fresh_session_diff_stat(inst):
    """``_session_diff_stat`` with this session's cache entry dropped first, so
    the answer reflects the worktree as it is now. None when unavailable."""
    if inst is None:
        return None
    try:
        wt = inst.GetWorktreePath() if inst.Started() else ""
    except Exception:  # noqa: BLE001
        wt = ""
    if wt:
        _DIFF_STAT_CACHE.pop(wt, None)
    try:
        return _session_diff_stat(inst)
    except Exception:  # noqa: BLE001 — enrichment only
        return None


def _query_bool(raw, default: bool) -> bool:
    if raw is None:
        return default
    return str(raw).strip().lower() not in ("", "0", "false", "no", "off")


@app.post("/api/instances/{title}/messages")
async def post_message(title: str, payload: dict) -> JSONResponse:
    """Leave a message for ``title``'s agent.

    Body ``{"text", "from"?, "reply_to"?, "delivery"?: auto|inbox|now,
    "kind"?: message|result, "data"?: object}``. ``from`` is a live session's
    title, or "" (default) for the CLI / an external client. ``auto`` types it
    in when the recipient is next stably idle; ``inbox`` only stores it;
    ``now`` (only into your own descendants, or from "") types it immediately
    when the recipient is idle or working. Safety rules may hold a push in the
    inbox instead (reply chain / rate limit; ``detail`` says which).

    A ``kind: "result"`` from a session gets ``data.diff_stat`` re-measured
    from the sender's worktree at post time (the listing's value lags).

    201 ``{"message", "delivery": delivered|pending|held, "detail"?}`` and a
    ``session.message`` event on the recipient."""
    inst, err = _inst_or_404(title)
    if err is not None:
        return err
    payload = payload or {}

    def bad(msg: str) -> JSONResponse:
        return JSONResponse({"error": msg}, status_code=400)

    text = payload.get("text", "")
    if not isinstance(text, str):
        return bad("text must be a string")
    if not text.strip():
        return bad("empty message")
    if len(text) > _MSG_MAX_CHARS:
        return bad("message too long (%d chars; max %d)" % (len(text), _MSG_MAX_CHARS))
    sender = payload.get("from") or ""
    if not isinstance(sender, str):
        return bad("from must be a session title")
    if _mailbox.is_peer_sender(sender):
        # Only the peer service speaks for a remote collaborator; a client
        # forging "peer:<name>" would borrow the peer framing.
        return bad('"from" may not start with "peer:"')
    if sender == title:
        return bad("a session cannot message itself")
    if sender and sender not in ENGINE.instances:
        return bad("unknown sender session: %s" % sender)
    reply_to = payload.get("reply_to") or None
    if reply_to is not None and not isinstance(reply_to, str):
        return bad("reply_to must be a message id")
    delivery = payload.get("delivery") or "auto"
    if delivery not in _mailbox.DELIVERIES:
        return bad("bad delivery: %r (auto, inbox or now)" % (delivery,))
    kind = payload.get("kind") or "message"
    if kind not in _mailbox.KINDS:
        return bad("bad kind: %r (message or result)" % (kind,))
    data = payload.get("data")
    if data is not None:
        if not isinstance(data, dict):
            return bad("data must be an object")
        size = len(json.dumps(data, ensure_ascii=False).encode("utf-8"))
        if size > _MSG_DATA_MAX_BYTES:
            return bad(
                "data too large (%d bytes; max %d)" % (size, _MSG_DATA_MAX_BYTES)
            )
    if kind == "result" and sender:
        # The sender's diff stat, measured NOW: the row's value is the tick's,
        # cached up to ~14s, and a worker reports seconds after its last edit.
        stat = await asyncio.to_thread(
            _fresh_session_diff_stat, ENGINE.instances.get(sender)
        )
        if stat is not None:
            data = dict(data or {})
            data["diff_stat"] = stat
    detail = ""
    if delivery == "now" and sender and not _is_descendant(title, sender):
        delivery = "auto"
        detail = 'delivery "now" is only for your own descendants — sent as auto'
    try:
        msg = await asyncio.to_thread(
            lambda: _mailbox.post(
                title,
                text,
                sender=sender,
                kind=kind,
                data=data,
                reply_to=reply_to,
                delivery=delivery,
                detail=detail,
            )
        )
    except ValueError as err:
        return bad(str(err))
    if msg["state"] == "held":
        result, detail = "held", msg["detail"]
    elif msg["delivery"] == "now":
        result, now_detail = await asyncio.to_thread(
            _mail_deliver_now, inst, title, msg
        )
        detail = now_detail or detail
    else:
        result = "pending"
        live = await asyncio.to_thread(
            _live_session_name, tmux.to_mindflock_tmux_name(title)
        )
        if live is None:
            detail = detail or _MSG_NOT_RUNNING
    msg = _mailbox.get(title, msg["id"]) or msg
    event_data = {
        "id": msg["id"],
        "from": sender,
        "kind": kind,
        "text": _mailbox.sanitize(text)[:200],
        "delivery": result,
    }
    if kind == "result":
        # A worker's report: the UI says done / blocked / failed without
        # fetching the message.
        status = (msg.get("data") or {}).get("status")
        if isinstance(status, str) and status:
            event_data["status"] = _mailbox.sanitize(status)[:24]
    _events.BUS.emit("session.message", session=title, data=event_data)
    body = {"message": msg, "delivery": result}
    if detail:
        body["detail"] = detail
    return JSONResponse(body, status_code=201)


@app.get("/api/instances/{title}/messages")
async def get_messages(title: str, request: Request) -> JSONResponse:
    """``title``'s inbox → ``{"messages": [...oldest→newest], "unread",
    "version"}``.

    Query: ``unread`` (default 1: pending + held only), ``include_consumed``
    (1 = also delivered/read; the newest ``limit``), ``after`` (a message id),
    ``from``, ``kind``, ``limit`` (default 50, max 200), ``mark_read`` (1 =
    atomically mark the returned messages read — a pending one is then never
    typed), ``wait`` (seconds, max 30: long-poll until a matching message
    arrives). While a long-poll runs, the delivery lane holds this recipient's
    pending messages — the poll is about to hand them over itself."""
    if ENGINE.instances.get(title) is None:
        return JSONResponse(
            {"error": "instance not found: %s" % title}, status_code=404
        )
    q = request.query_params
    try:
        limit = int(q.get("limit") or _MSG_LIST_DEFAULT)
        wait = float(q.get("wait") or 0.0)
    except ValueError:
        return JSONResponse({"error": "bad limit or wait"}, status_code=400)
    limit = max(1, min(limit, _MSG_LIST_MAX))
    wait = max(0.0, min(wait, _MSG_WAIT_MAX_S))  # NaN lands on 0.0 too
    kind = q.get("kind") or None
    if kind is not None and kind not in _mailbox.KINDS:
        return JSONResponse({"error": "bad kind: %r" % kind}, status_code=400)
    unread_only = _query_bool(q.get("unread"), True) and not _query_bool(
        q.get("include_consumed"), False
    )
    query = dict(
        unread_only=unread_only,
        after=q.get("after") or None,
        sender=q.get("from"),
        kind=kind,
        limit=limit,
        mark_read=_query_bool(q.get("mark_read"), False),
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait
    if wait > 0:
        _mailbox.waiter_begin(title)
    try:
        while True:
            try:
                result = await asyncio.to_thread(lambda: _mailbox.fetch(title, **query))
            except ValueError as err:
                return JSONResponse({"error": str(err)}, status_code=400)
            if result["messages"] or loop.time() >= deadline:
                return JSONResponse(result)
            # Nothing yet: watch the box's version (one fstat while the file
            # is unchanged) on the event loop's clock — never a parked thread.
            seen = result["version"]
            while loop.time() < deadline:
                await asyncio.sleep(min(_MSG_POLL_S, max(0.0, deadline - loop.time())))
                if ENGINE.instances.get(title) is None:
                    return JSONResponse(result)
                if await request.is_disconnected():
                    return JSONResponse(result)
                if await asyncio.to_thread(_mailbox.version, title) != seen:
                    break
    finally:
        if wait > 0:
            _mailbox.waiter_end(title)


@app.post("/api/instances/{title}/messages/read")
def post_messages_read(title: str, payload: dict) -> JSONResponse:
    """Mark messages read: ``{"ids": [...]}`` or ``{"all": true}`` →
    ``{"marked", "unread"}``. A pending message marked read is never typed."""
    if ENGINE.instances.get(title) is None:
        return JSONResponse(
            {"error": "instance not found: %s" % title}, status_code=404
        )
    payload = payload or {}
    ids = payload.get("ids")
    every = payload.get("all") is True
    if not every and not (
        isinstance(ids, list) and all(isinstance(i, str) for i in ids)
    ):
        return JSONResponse(
            {"error": 'pass {"ids": [...]} or {"all": true}'}, status_code=400
        )
    marked, unread = _mailbox.mark_read(title, ids or (), all_unread=every)
    return JSONResponse({"marked": marked, "unread": unread})


# K1: the diff/stage/PR base branch is resolved PER SESSION, not from the
# global provision settings (whose base — e.g. "staging" — is wrong for every
# session on any other repo and used to snap the guided workflow back to
# "agent" right after a successful commit). Fallback resolution (for sessions
# persisted before ``base_branch`` was recorded) is cached briefly per
# worktree so the 4s poll doesn't pay 3-4 git calls per session.
_BASE_BRANCH_CACHE: Dict[str, tuple] = {}  # worktree -> (expires_epoch, branch)
_BASE_BRANCH_TTL = 30.0


def _git_ref_exists(wt: str, ref: str) -> bool:
    """True when ``ref`` resolves in worktree ``wt`` (``git show-ref --verify``)."""
    return (
        _run_capped(
            ["git", "-C", wt, "show-ref", "--verify", "--quiet", ref],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
        ).returncode
        == 0
    )


def _resolve_fallback_base_branch(wt: str) -> str:
    """Base branch for a session that predates the stored ``base_branch``.

    Chain: the repo's ``origin/HEAD`` -> a ``main``/``master`` probe (local
    head, then remote-tracking) -> the configured provision base ONLY when this
    session's repo IS the configured repo -> ``main``.
    """
    # 1. origin/HEAD — what the remote calls its default branch.
    cp = _run_capped(
        [
            "git",
            "-C",
            wt,
            "symbolic-ref",
            "--quiet",
            "--short",
            "refs/remotes/origin/HEAD",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=60,
    )
    if cp.returncode == 0:
        ref = cp.stdout.decode("utf-8", "replace").strip()  # "origin/main"
        if ref.startswith("origin/"):
            ref = ref[len("origin/") :]
        if ref:
            return ref
    # 2. main / master probe (local branch, then remote-tracking).
    for cand in ("main", "master"):
        if _git_ref_exists(wt, "refs/heads/" + cand) or _git_ref_exists(
            wt, "refs/remotes/origin/" + cand
        ):
            return cand
    # 3. The configured provision base — but only when this session's repo
    #    matches the configured repo (a global "staging" must never leak onto
    #    an unrelated repo; that's the K1 bug).
    try:
        s = provisioning.load_provision_settings()
        if s is not None:
            origin = provisioning._git_origin_url(wt)
            if origin and provisioning._same_repo_url(origin, s.repo_url):
                return s.base_branch
    except Exception:  # noqa: BLE001
        pass
    return "main"


def _session_base_branch(inst) -> str:
    """The branch ``inst``'s work is measured against (per-session base, K1).

    Managed worktree/provisioned sessions prefer the value recorded on the
    instance at creation. In-place sessions deliberately do NOT: the user
    switches branches in series there, and the creation-time value (which
    equals the creation branch) would measure later branches against the
    wrong base — so their base is resolved live via
    :func:`_resolve_fallback_base_branch` (cached ~30s per worktree; the
    branch-drift handler pops the cache on checkout).
    """
    stored = getattr(inst, "BaseBranch", "") or ""
    in_place = bool(getattr(inst, "InPlace", False))
    if stored and not in_place:
        return stored
    try:
        wt = inst.GetWorktreePath() or ""
    except Exception:  # noqa: BLE001
        wt = ""
    if not wt or not os.path.isdir(wt):
        return stored or "main"
    now = time.time()
    cached = _BASE_BRANCH_CACHE.get(wt)
    if cached and cached[0] > now:
        return cached[1]
    branch = _resolve_fallback_base_branch(wt)
    if in_place and not (
        _git_ref_exists(wt, "refs/heads/" + branch)
        or _git_ref_exists(wt, "refs/remotes/origin/" + branch)
    ):
        # Degenerate repo (no origin/HEAD, no main/master): fall back to the
        # branch-is-its-own-base semantics instead of wedging at "agent".
        branch = _current_branch(wt) or stored or branch
    _BASE_BRANCH_CACHE[wt] = (now + _BASE_BRANCH_TTL, branch)
    return branch


def _configured_pr_base(wt: str = "") -> str:
    """The user-configured PR base branch for the repo at ``wt``, or ``""``.

    The repo's own ``repository.repo_branches`` block first (its
    ``pr_base_branch``, else its ``base_branch``), then the flock-wide
    ``repository.pr_base_branch``. Read fresh from settings on each call so a
    change takes effect without a restart. When set it overrides the
    per-session fork-point as the "Make PR" target (see
    :func:`instance_make_pr`); blank leaves the K1 behaviour intact.
    """
    try:
        from backend.config.settings import load_settings, repo_branches_for

        st = load_settings()
        if wt:
            pr = repo_branches_for(provisioning.repo_slug_for(wt), st)[1]
            if pr:
                return pr
        return (st.repository.pr_base_branch or "").strip()
    except Exception:  # noqa: BLE001 — settings optional; never break a PR
        return ""


_PR_CACHE: Dict[str, tuple] = {}  # branch -> (expires_epoch, info_or_None, last_good)
#: How long a previously-seen PR survives lookups that come back empty. Long
#: enough to ride out a rate limit or a network blip, short enough that a PR which
#: really was deleted stops being reported within a few minutes.
_PR_STICKY_S = 600.0
# Mergeability is a SECOND network round trip (a single-PR read, which is also what
# makes GitHub compute `mergeable` at all) plus the check rollup, so it gets its own
# shorter-lived memo: blockers change while you watch — a review lands, a check goes
# green — and a stale "cannot merge" is worse than a slightly late one.
_MERGE_STATE_CACHE: Dict[str, tuple] = {}  # branch -> (expires_epoch, state_or_None)
_MERGE_STATE_TTL = 20.0

#: Review verdict per branch — "approved" / "changes_requested" / "". Cached far
#: longer than the merge state because nothing acts on it: it exists to be told
#: to a human once, and one GitHub call per open PR every two minutes is a
#: budget a flock of sessions can afford.
_REVIEW_CACHE: Dict[str, tuple] = {}  # branch -> (expires_epoch, verdict)
_REVIEW_TTL = 120.0


def _pr_merge_state(wt: str, branch: str, force: bool = False):
    """Whether ``branch``'s PR can be merged and what is blocking it, memoized.

    None means "could not find out" (no token, no GitHub origin, no open PR, or a
    network fault) — never "no". The UI must leave the merge affordance alone in
    that case rather than claim knowledge it does not have.
    """
    if not branch:
        return None
    now = time.time()
    cached = _MERGE_STATE_CACHE.get(branch)
    if not force and cached and cached[0] > now:
        return cached[1]
    state = _github_pr.pr_merge_state_sync(wt, branch)
    _MERGE_STATE_CACHE[branch] = (now + _MERGE_STATE_TTL, state)
    return state


def _pr_number(info) -> Optional[int]:
    """A PR's number from whichever rung answered: the REST lookup carries it
    outright, the ``gh`` one only in the URL's ``/pull/<n>`` tail."""
    if not isinstance(info, dict):
        return None
    raw = info.get("number")
    if isinstance(raw, int) and raw > 0:
        return raw
    match = re.search(r"/pull/(\d+)", str(info.get("url") or ""))
    return int(match.group(1)) if match else None


def _pr_review_state(wt: str, branch: str, info, force: bool = False) -> str:
    """Where ``branch``'s open PR stands with its reviewers, memoized.

    ``""`` means "nobody has decided, or we could not ask" — never "not
    approved". The caller announces an approval to a human, so an empty answer
    has to stay indistinguishable from silence.
    """
    number = _pr_number(info)
    if not branch or not number:
        return ""
    now = time.time()
    cached = _REVIEW_CACHE.get(branch)
    if not force and cached and cached[0] > now:
        return cached[1]
    verdict = _github_pr.pr_review_state_sync(wt, number)
    _REVIEW_CACHE[branch] = (now + _REVIEW_TTL, verdict)
    return verdict


def _pr_info(wt: str, branch: str, force: bool = False):
    """Most recent PR for ``branch`` (any base) as ``{url, state, base}`` (cached
    60s), or None. ``state`` is OPEN / MERGED / CLOSED; ``base`` is the branch it
    targets, or ``""`` when the rung could not say.

    Matched by head branch alone — deliberately NOT filtered by base. make-pr
    can target any base (a configured default like ``staging``, or one picked
    in the dialog), and the server never learns the dialog choice; filtering
    the lookup by a guessed base misses the real PR and wedges the stage chip
    on "pushed". A branch's most recent PR is the one this workflow cares
    about, whatever it merges into. (The cache is already keyed by branch, so
    this matches the cache's granularity too.)

    ``gh`` is preferred but optional: without it the same question is asked
    over the REST API instead. That matters more here than anywhere else —
    this is the ONLY PR signal the stage machine has, so a gh-less user whose
    lookup returned None used to be stuck on "pushed" forever, still being
    offered "Make PR" for a PR they had already opened by hand."""
    if not branch:
        return None
    now = time.time()
    cached = _PR_CACHE.get(branch)
    if not force and cached and cached[0] > now:
        return cached[1]
    info = (
        _gh_pr_info(wt, branch)
        if gh_available()
        else _github_pr.find_pr_sync(wt, branch)
    )
    # STICKY ON FAILURE, bounded. None means BOTH "there is no PR" and "we could not
    # ask" (a rate limit, a network blip, a slow `gh`), and caching it flapped the
    # stage off "pr" and back every minute — which fired the "PR merged or closed"
    # toast over and over for a PR that was open the whole time. So a previously
    # known PR survives a failed lookup for _PR_STICKY_S, and is retried sooner than
    # the normal TTL; only a persistent absence is finally believed. Same reasoning
    # as _origin_branch_sha's "keep the previous answer rather than flapping".
    if info is None and cached is not None and cached[1] is not None:
        last_good = cached[2] if len(cached) > 2 else now
        if now - last_good < _PR_STICKY_S:
            _PR_CACHE[branch] = (now + 15, cached[1], last_good)
            return cached[1]
    _PR_CACHE[branch] = (now + 60, info, now if info is not None else 0.0)
    return info


def _gh_pr_info(wt: str, branch: str):
    """The ``gh`` rung of :func:`_pr_info` — ``{url, state, base}`` or None.

    ``baseRefName`` is asked for because Verify's squash-merge fallback needs it:
    "the PR merged" is only evidence that work SHIPPED if it merged into the
    branch the checklist is waiting for, and that is a fact about the PR rather
    than something to infer from settings.
    """
    cp = _run_capped(
        [
            "gh",
            "pr",
            "list",
            "--head",
            branch,
            "--state",
            "all",
            "--json",
            "url,state,baseRefName",
            "--limit",
            "1",
        ],
        cwd=wt,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=120,
    )
    if cp.returncode != 0:
        return None
    try:
        arr = json.loads(cp.stdout.decode("utf-8", "replace") or "[]")
    except (ValueError, KeyError, IndexError):
        return None
    if not arr:
        return None
    return {
        "url": arr[0].get("url"),
        "state": arr[0].get("state"),
        "base": arr[0].get("baseRefName") or "",
    }


def _unique_title(base: str) -> str:
    """A title not currently in use (appends -2, -3, … on collision)."""
    if base not in ENGINE.instances:
        return base
    i = 2
    while ("%s-%d" % (base, i)) in ENGINE.instances:
        i += 1
    return "%s-%d" % (base, i)


@app.get("/api/ides")
def list_ides() -> JSONResponse:
    """The known-IDE registry with per-host installed flags, plus the currently
    configured editor — so Settings can render a detected-IDE picker (installed
    entries selectable, missing ones grayed out) with a custom-command escape
    hatch."""
    from backend.web.core import ide_launch as _ide_launch

    installed = {spec.command for spec in _ide_launch.detect_ides()}
    return JSONResponse(
        {
            "ides": [
                {
                    "command": spec.command,
                    "name": spec.name,
                    "kind": spec.kind,
                    "installed": spec.command in installed,
                }
                for spec in ide_cfg.known_ide_specs()
            ],
            "current": ide_cfg.ide_command(),
            "current_name": ide_cfg.ide_name(),
        }
    )


@app.post("/api/instances/{title}/ide")
async def instance_cursor(title: str) -> JSONResponse:
    """Open (or focus) the workspace in the configured IDE (Settings → Advanced;
    Cursor by default). `<ide> <dir>` reuses an existing window for that folder
    in every VS Code-family editor, so this both opens and focuses. Terminal
    editors (nvim/vim/…) open inside a new terminal window instead.

    When we OPEN a new GUI window we maximize it (on its current monitor); when
    the workspace is already open we just focus it and leave it as-is.
    """
    from backend.web.core import ide_launch as _ide_launch

    inst, err = _inst_or_404(title)
    if err is not None:
        return err
    peer_err = _peer_guard.refusal(inst, "opening in an editor")
    if peer_err is not None:
        return peer_err
    wt = inst.GetWorktreePath()
    if not wt:
        if log.ErrorLog is not None:
            log.ErrorLog.Printf("ide: %s open refused: workspace not ready", title)
        return JSONResponse({"error": "workspace not ready"}, status_code=409)
    # Window focus/maximize ops only apply to GUI editors — a terminal editor
    # always opens a fresh terminal window (no title needle to match).
    is_gui = ide_cfg.ide_kind() != "terminal"
    # A window already showing this workspace → this call just focuses it, so
    # leave its size alone. Otherwise we're opening fresh and resize the new one.
    already_open = is_gui and await asyncio.to_thread(_cursor_windows_open, wt)
    try:
        await asyncio.to_thread(_ide_launch.launch_ide, wt)
    except _ide_launch.IdeLaunchError as err:
        return JSONResponse({"error": str(err)}, status_code=400)
    except Exception as err:  # noqa: BLE001
        return JSONResponse(
            {"error": "failed to launch %s: %s" % (ide_cfg.ide_name(), err)},
            status_code=500,
        )
    opened_new = not already_open
    if opened_new and is_gui:
        asyncio.create_task(asyncio.to_thread(_maximize_new_cursor_window, wt))
    elif already_open:
        # Already open — `cursor <path>` switches the folder but won't restore a
        # minimized window or steal focus, so do the Win32 restore + raise here.
        asyncio.create_task(asyncio.to_thread(_focus_cursor_window, wt))
    return JSONResponse({"ok": True, "opened_new": opened_new})


@app.post("/api/instances/{title}/cleanup")
async def instance_cleanup(title: str) -> JSONResponse:
    """Kill the session AND fully remove its workspace directory (destructive).

    Belt-and-suspenders over delete: after Kill (which removes the worktree /
    branch or rmtrees the clone), force-rmtree anything left and kill the shell
    session. The frontend confirms before calling this. Same door as DELETE
    (:func:`core.session_end.end_session`, ``wipe=True, scrub=True``): **409**
    ``{"error": "folder in use", "sessions": [...]}`` and nothing changes
    while another live session works in the folder; the session lands in
    Recently closed; its children move up and are reported.
    → ``{ok, title, wiped, children}``. In-place sessions run in the user's
    OWN repo — it is never deleted.
    """
    inst, err = _inst_or_404(title)
    if err is not None:
        return err
    peer_err = _peer_guard.refusal(inst, "cleanup (unshare the folder instead)")
    if peer_err is not None:
        return peer_err
    return await _end_session_response(title, wipe=True, scrub=True)


@app.post("/api/instances/{title}/close")
async def close_instance(title: str) -> JSONResponse:
    """End the agent session but KEEP its worktree on disk.

    Unlike DELETE (which removes the worktree + branch via ``inst.Kill``), this
    only kills the agent + shell tmux sessions and drops the instance from the
    grid (:func:`core.session_end.end_session`, ``wipe=False``). The session
    is stashed in the recently-closed store so it can be reopened later as a
    fresh agent pointed at the same worktree; its children keep running under
    its own parent and are reported. → ``{ok, title, wiped: false, children}``.
    """
    inst, err = _inst_or_404(title)
    if err is not None:
        return err
    return await _end_session_response(title, wipe=False)


@app.post("/api/instances/{title}/copy")
async def copy_instance(title: str) -> JSONResponse:
    """Open a NEW agent session sharing the source session's worktree.

    Runs in-place in the source's worktree directory — same files, same branch,
    a second live Claude session. Cleanup of the copy never deletes the shared
    worktree (in-place semantics), so the original session keeps owning it.
    """
    src, err = _inst_or_404(title)
    if err is not None:
        return err
    peer_err = _peer_guard.refusal(src, "copying")
    if peer_err is not None:
        return peer_err
    try:
        wt = src.GetWorktreePath()
    except Exception:  # noqa: BLE001
        wt = ""
    if not wt or not os.path.isdir(wt):
        return JSONResponse({"error": "source workspace not ready"}, status_code=409)
    new_title = _unique_title(title + "-copy")
    program = src.Program or ENGINE.default_program()
    inst = session.NewInstance(
        session.InstanceOptions(
            title=new_title,
            path=wt,
            program=program,
            in_place=True,
            # A copy is "another one of THIS session" — same CLI, same identity.
            profile_id=getattr(src, "ProfileId", "") or "",
            profile_model=getattr(src, "ProfileModel", "") or "",
        )
    )
    inst.SetStatus(Loading)
    with ENGINE.lock:
        ENGINE.instances[new_title] = inst

    async def _bg_start() -> None:
        try:
            await asyncio.to_thread(inst.Start, True)
            ENGINE.save()
        except Exception as err:  # noqa: BLE001
            if log.ErrorLog is not None:
                log.ErrorLog.Printf("failed to copy instance %s: %v", new_title, err)
            # By identity, with the shared removal teardown — see
            # :func:`_drop_failed_start`.
            _drop_failed_start(new_title, inst)

    _register_task(_bg_start())
    _events.BUS.emit(
        "session.created",
        session=new_title,
        new="loading",
        data={"program": program, "copied_from": title},
    )
    return JSONResponse(_instance_json(inst), status_code=202)


@app.post("/api/instances/{title}/branch")
async def branch_instance(title: str, request: Request) -> JSONResponse:
    """Branch a session: a NEW session in its own worktree, on a new branch
    (``<branch_prefix><title>-branch``, numbered ``-branch-2``… past taken
    names or branches) cut from the source's current HEAD, with a copy of the
    source's uncommitted changes (tracked edits, deletions and untracked,
    non-ignored files — left uncommitted, nothing staged) laid over it before
    its agent starts. The source is untouched. Same agent CLI, launch flags,
    auth profile and model as the source; no parent; a fresh conversation (as
    Duplicate). The recorded base is the source's branch.

    → **202** the new row plus ``branched_from``, ``base_branch`` and
    ``carried`` (whether there were uncommitted changes to copy); the worktree
    is built in the background like any create. **409** with ``{"error"}``:
    a remote ``device::title`` row or a relayed request, a shared-folder
    session, one still starting, a folder that isn't a git repo ("Branch needs
    a git repository — <folder> isn't one") or has no commit, a clone-strategy
    workspace, or changes git can't snapshot. **404** unknown session. See
    :mod:`core.session_branch`.
    """
    return await _session_branch.branch(
        title,
        person=_auth.request_person(request),
        relayed=bool(request.headers.get(_remote.REMOTE_HEADER)),
    )


@app.post("/api/instances/{title}/parent")
async def set_instance_parent(title: str, payload: dict = None) -> JSONResponse:
    """Re-parent a session: ``{"parent": "<title>"}`` adopts it under that live
    session, ``{"parent": ""}`` detaches it to a root.

    404 for an unknown session; 400 for an unknown parent, the session itself,
    or a parent that is the session's own descendant (a cycle); 409 when the
    adoption would break a spawn limit (``MINDFLOCK_MAX_CHILDREN`` /
    ``MINDFLOCK_MAX_SPAWN_DEPTH``, named in the error). ``Spawned`` is
    untouched — it records who created the session, not who manages it now.
    Persists, and answers the updated row (``_instance_json``).
    """
    inst, err = _inst_or_404(title)
    if err is not None:
        return err
    peer_err = _peer_guard.refusal(inst, "re-parenting")
    if peer_err is not None:
        return peer_err
    raw = (payload or {}).get("parent", "")
    if raw is None:
        raw = ""
    if not isinstance(raw, str):
        return JSONResponse({"error": "parent must be a string"}, status_code=400)
    parent = raw.strip()
    with ENGINE.lock:
        if ENGINE.instances.get(title) is not inst:
            return JSONResponse(
                {"error": "instance not found: %s" % title}, status_code=404
            )
        if parent and _peer_guard.is_peer_session(ENGINE.instances.get(parent)):
            return JSONResponse(
                {"error": "a shared-folder (peer) session can't be a parent"},
                status_code=409,
            )
        if parent:
            perr = _lineage.parent_error(ENGINE.instances, title, parent)
            if perr:
                return JSONResponse({"error": perr}, status_code=400)
            # Adopting is a second way to grow a fan-out: it answers to the
            # same caps as a create (detaching is never limited).
            if parent != _lineage.parent_of(inst):
                lerr = _lineage.adopt_limit_error(ENGINE.instances, title, parent)
                if lerr:
                    return JSONResponse({"error": lerr}, status_code=409)
        inst.Parent = parent
    ENGINE.save()
    return JSONResponse(_instance_json(inst))


@app.post("/api/instances/{title}/profile")
async def set_instance_profile(title: str, payload: dict) -> JSONResponse:
    """Hot-swap which auth profile a session's agent runs under.

    Body: ``{"profile_id": "<id>" | "default" | ""}`` (same tri-state as
    session creation). The pin is persisted, the agent tmux session is killed,
    and a fresh one is started immediately — the relaunch path re-derives the
    profile overlay from the stored pin, so the CLI comes back as the new
    identity. The kill reads as an unnatural death, so the relaunch takes the
    CLI's resume path, and the thread it names is re-pointed first: a
    conversation belongs to the account that created it, so
    ``thread_markers.switch_profile`` files the outgoing identity's thread
    under its own name and restores whatever the incoming identity last had in
    this window. A swap back therefore reopens the conversation you left; a
    first swap to an identity has nothing to restore and starts fresh
    (``resumed`` in the response says which). The worktree, shell pane and diff
    state are untouched.
    """
    inst, err = _inst_or_404(title)
    if err is not None:
        return err
    payload = payload or {}
    profile_id = str(payload.get("profile_id", "") or "").strip()
    perr = _profile_id_error(profile_id)
    if perr:
        return JSONResponse({"error": perr}, status_code=400)
    prev_profile_id = getattr(inst, "ProfileId", "") or ""
    prev_profile_model = getattr(inst, "ProfileModel", "") or ""
    # The model override rides along only when the caller sends the key — a
    # model-only change (same identity) never has to restate the identity. An
    # IDENTITY change without an explicit model drops the pin: it belonged to
    # the old identity's catalog, and carrying e.g. "openai/gpt-5" onto a
    # Claude-subscription account would launch its CLI pinned to a model its
    # API has never heard of.
    #
    # Resolved BEFORE anything is mutated, so a 400 from this route means "the
    # session is exactly as you left it" — the same contract the auth-profiles
    # PUT keeps.
    if "profile_model" in payload:
        profile_model = str(payload.get("profile_model", "") or "").strip()
        merr = _profile_model_error(profile_model)
        if merr:
            return JSONResponse({"error": merr}, status_code=400)
    elif profile_id != prev_profile_id:
        profile_model = ""
    else:
        profile_model = prev_profile_model

    # Re-picking what is already running is a no-op, not a restart. The popover
    # lists the ACTIVE identity as a clickable row like every other, so the
    # cheapest possible misclick would otherwise hard-kill a working agent and
    # spend its resume position to arrive back where it started.
    if profile_id == prev_profile_id and profile_model == prev_profile_model:
        return JSONResponse(
            {"ok": True, "profile_id": profile_id, "note": "", "unchanged": True}
        )

    inst.ProfileId = profile_id
    inst.ProfileModel = profile_model
    ENGINE.save()

    # A conversation belongs to the account that started it: its transcript
    # lives under that identity's config dir and the other one cannot open it.
    # File the outgoing identity's thread under its own name and restore
    # whatever the incoming identity last had here, so the relaunch resumes a
    # thread it actually owns — and a swap back returns you to the conversation
    # you left instead of a third fresh one.
    restored = ""
    try:
        from backend.providers import auth_profiles as _ap
        from backend.providers import thread_markers as _tm

        restored = _tm.switch_profile(
            tmux.to_mindflock_tmux_name(title),
            _ap.effective_profile_id(prev_profile_id),
            _ap.effective_profile_id(profile_id),
        )
    except Exception:  # noqa: BLE001 — markers are enrichment only
        pass

    # Provisioned launcher scripts carry profile launch FLAGS baked in at write
    # time (env never is — it rides the relaunch exports). Rewrite the script so
    # a swap also updates flag-level routing (e.g. an OpenRouter model pin).
    # Best-effort: a rewrite failure still leaves the env swap working.
    def _restart() -> str:
        try:
            wt = inst.GetWorktreePath()
        except Exception:  # noqa: BLE001
            wt = ""
        if wt:
            _rewrite_launcher_for_profile(inst, wt)
        if inst.Started() and inst.Status == session.Running:
            _kill_agent_session(title)
            _, rerr = _ensure_agent_session(inst, title)
            if rerr is not None:
                return str(rerr)
        return ""

    # The kill already happened, so a relaunch failure leaves the session with
    # no agent at all. Answering {ok: true} there would put a cheerful "Now
    # running as work" toast on top of a dead pane; report it instead.
    restart_err = await asyncio.to_thread(_restart)
    if restart_err:
        return JSONResponse(
            {
                "error": "account saved, but the agent did not come back up: %s"
                % restart_err
            },
            status_code=500,
        )
    note = ""
    try:
        from backend.providers import auth_profiles

        note = auth_profiles.unsupported_note(inst.Program or "", profile_id)
    except Exception:  # noqa: BLE001 — the note is enrichment only
        pass
    _events.BUS.emit(
        "session.profile_changed",
        session=title,
        data={"profile_id": profile_id, "resumed": bool(restored)},
    )
    return JSONResponse(
        {
            "ok": True,
            "profile_id": profile_id,
            "note": note,
            # Whether the incoming identity had a conversation here to go back
            # to. The UI says which of the two things just happened rather than
            # leaving the user to discover it in the pane.
            "resumed": bool(restored),
        }
    )


# --- Recently-closed: list / reopen / forget ---------------------------------
@app.get("/api/recently-closed")
def recently_closed() -> JSONResponse:
    """The recently-closed session store (the reopen / Ctrl+Z undo targets),
    each annotated with whether its preserved folder still exists on disk."""
    out = []
    for e in _load_recently_closed():
        folder = e.get("folder") or ""
        out.append(
            {
                "id": e.get("id"),
                "title": e.get("title"),
                "branch": e.get("branch"),
                "folder": folder,
                "in_place": bool(e.get("in_place")),
                "provisioned": bool(e.get("provisioned")),
                "closed_at": e.get("closed_at"),
                "exists": bool(folder and os.path.isdir(folder)),
                "wiped": bool(e.get("wiped")),
                # Why it left when the user didn't close it ("workspace
                # missing": dropped at load), and whether its branch survives.
                "reason": e.get("reason") or "",
                "branch_exists": e.get("branch_exists"),
            }
        )
    return JSONResponse(out)


@app.post("/api/recently-closed/{entry_id}/reopen")
async def reopen_recently_closed(entry_id: str) -> JSONResponse:
    """Recreate a closed session as a running instance on its preserved worktree."""
    return await _reopen_closed_entry(entry_id)


async def _reopen_closed_entry(entry_id: str) -> JSONResponse:
    """The reopen itself, callable from anywhere that identifies a closed
    session — the Recently closed dialog by entry id, and an intake row that found this
    entry still holding its work (see :func:`intake_reopen`)."""
    from backend.session.instance import FromInstanceData
    from backend.session.storage import InstanceData

    items = _load_recently_closed()
    entry = next((e for e in items if e.get("id") == entry_id), None)
    if entry is None:
        return JSONResponse({"error": "entry not found"}, status_code=404)
    data_dict = dict(entry.get("data") or {})
    wt = (
        (data_dict.get("worktree") or {}).get("worktree_path")
        or entry.get("folder")
        or ""
    )
    if not wt or not os.path.isdir(wt):
        return JSONResponse(
            {"error": "workspace directory no longer exists: %s" % wt},
            status_code=410,
        )
    base_title = entry.get("title") or data_dict.get("title") or "session"
    title = _unique_title(base_title)
    data_dict["title"] = title
    data_dict["status"] = int(session.Running)
    # Freshen the activity stamp to NOW. Closing wrote a deletion tombstone for
    # this title (L1 convergence); the reopened session must carry a newer
    # last-seen than that tombstone or engine.save() treats it as still-deleted
    # and drops it right back out of memory (window "reopens" then vanishes).
    # This is the "re-created session has newer timestamps and survives" path
    # that _is_tombstoned() is written to expect.
    data_dict["updated_at"] = _datetime.datetime.now().astimezone().isoformat()
    if isinstance(data_dict.get("worktree"), dict):
        data_dict["worktree"]["session_name"] = title
    try:
        inst = await asyncio.to_thread(
            lambda: FromInstanceData(InstanceData.from_dict(data_dict), attach=False)
        )
    except Exception as err:  # noqa: BLE001
        return JSONResponse({"error": "failed to reopen: %s" % err}, status_code=500)
    with ENGINE.lock:
        # The parent it was closed under may be gone by now — a reopened
        # session comes back a root unless that parent is still live. Spawned
        # is the session's own history and stays.
        parent = getattr(inst, "Parent", "") or ""
        if parent and (parent == title or parent not in ENGINE.instances):
            inst.Parent = ""
        ENGINE.instances[title] = inst
    # A hidden helper (a Verify run) comes back hidden: the flag was stashed
    # with the entry because `_forget_session_state` dropped the live one.
    _shared = entry.get("shared_branch") or {}
    if isinstance(_shared, dict) and _shared.get("branch"):
        try:
            from backend.web.core import shared_branch as _sb

            _sb.set_shared(
                title, str(_shared.get("remote") or ""), str(_shared["branch"])
            )
        except Exception:  # noqa: BLE001
            pass
    _hidden = str(entry.get("hidden_by") or "")
    if _hidden and _hidden_sessions.validate(_hidden) is None:
        try:
            _hidden_sessions.set_hidden_by(title, _hidden)
        except Exception:  # noqa: BLE001
            pass
    # …and keeps the person it was created for (stashed the same way).
    try:
        _person_sessions.set_person(title, str(entry.get("person") or ""))
    except Exception:  # noqa: BLE001
        pass
    # Eagerly boot the agent tmux session on the preserved worktree instead of
    # leaving a phantom "running" instance whose agent only starts lazily when a
    # terminal happens to attach. Otherwise a reopen in single-pane view (where
    # the pane may not mount) looks like nothing happened. Idempotent and shares
    # the exact launch path used on terminal-connect / queue-reboot; a boot
    # failure is non-fatal — the session still surfaces and can boot lazily.
    _name, boot_err = await asyncio.to_thread(_ensure_agent_session, inst, title)
    if boot_err is not None and log.ErrorLog is not None:
        log.ErrorLog.Printf("reopen %s: agent boot deferred: %s", title, boot_err)
    ENGINE.save()
    _save_recently_closed([e for e in items if e.get("id") != entry_id])
    return JSONResponse(_instance_json(inst))


@app.post("/api/recently-closed/{entry_id}/forget")
async def forget_recently_closed(entry_id: str, payload: dict = None) -> JSONResponse:
    """Drop a recently-closed entry. With ``{"wipe": true}`` also delete its
    worktree directory from disk (skipped for in-place sessions)."""
    payload = payload or {}
    wipe = bool(payload.get("wipe"))
    items = _load_recently_closed()
    entry = next((e for e in items if e.get("id") == entry_id), None)
    if entry is None:
        return JSONResponse({"error": "entry not found"}, status_code=404)
    if wipe and not entry.get("in_place"):
        folder = entry.get("folder") or ""
        # A closed session can SHARE its worktree with one that is still running
        # (a copy and its origin keep the same directory — see
        # _worktree_in_use_by_other). Deleting it here would pull the rug out
        # from under the live session, which is not what "wipe the thing I
        # closed" can possibly mean.
        if folder and _worktree_in_use_by_other(folder, ""):
            return JSONResponse(
                {
                    "error": "a running session is still working in %s — close it "
                    "first" % folder
                },
                status_code=409,
            )
        repo_path = ((entry.get("data") or {}).get("worktree") or {}).get(
            "repo_path", ""
        )
        await asyncio.to_thread(_remove_worktree_path, folder, repo_path)
    # Re-read: `items` was loaded before the delete above, and a page deleting
    # several rows at once fires these concurrently — writing the stale snapshot
    # back would resurrect the sibling entries whose directories just went, each
    # then offering a Reopen that can only 410.
    _save_recently_closed(
        [e for e in _load_recently_closed() if e.get("id") != entry_id]
    )
    return JSONResponse({"ok": True})


# --- Guided workflow: commit -> push -> PR -----------------------------------
# Worktree-relative scratch files the commit flow writes: the commit message
# (read back with ``git commit -F`` so multiline messages can't break the tmux
# command) and the exit-status marker ``_session_stage`` reads to show the
# pre-commit result. Both are swept out of ``git add -A`` via _exclude_artifacts.
_COMMIT_MSG_FILE = ".mindflock_commit_msg"
_COMMIT_STATUS_FILE = ".mindflock_commit_status"


def _commit_shell_command(skip: str = "") -> str:
    """The POSIX shell one-liner the commit endpoint types into the session's
    interactive shell (so the user watches the pre-commit hooks run live).

    ``skip`` is a comma-separated list of pre-commit hook IDs to bypass on this
    attempt, rendered as a one-shot ``SKIP=<ids>`` environment prefix. When it is
    empty this function returns the ORIGINAL string byte for byte — the retry
    feature must not perturb the ordinary commit path, and several tests pin
    literal substrings of it.

    Only the environment prefix is added: no ``tee``, no log file, no extra
    markers. Piping the commit through ``tee`` to let the shell inspect hook
    output would strip the tty (changing pytest/black colour behaviour and
    breaking any interactive GPG or credential prompt for ``commit.gpgsign``
    users) and would make ``rc`` depend on parsing a stream the hooks also write
    to. The retry decision is made in Python instead, which already reads the
    pane; see :func:`_failed_precommit_hook`.

    ``SKIP`` is pre-commit's own mechanism, read once per run as a comma-separated
    set matched against each hook's id or alias. A wrapper hook script that
    composes an inbound value (``export SKIP="${SKIP:+$SKIP,}<id>"``) keeps
    working, because this prefix is what it sees on the way in.

    lock (stage=precommit) -> stage all -> commit (runs hooks) -> record the
    exit code (non-zero => pre-commit interrupt) -> drop the lock.

    The lock lives in the PRIVATE git dir (``git rev-parse --absolute-git-dir``),
    NOT the worktree root: a root-level scratch file gets swept mid-commit by
    in-tree tooling (``git add -A`` skips it, but ``git clean -fdx`` and agents
    running in the tree delete it), which would strip ``_session_stage``'s only
    "commit running" signal and drop the pill to "idle". The git dir is never
    staged, cleaned, or traversed there.

    Auto-fix hooks (black, gitnexus, end-of-file/trailing-whitespace, ruff
    --fix, …) reformat/regenerate files and exit non-zero, leaving the fixes
    UNSTAGED. Re-staging + committing again then passes. So we loop: if a failed
    commit left unstaged changes (an auto-fixer ran), re-add and retry; if
    nothing was modified (a genuine lint/test failure), stop and surface it as
    the pre-commit interrupt. Bounded to a few rounds. POSIX-only constructs so
    it works under bash or zsh.
    """
    # Read the message from the (git-excluded) message file via -F rather than
    # -m, so a multiline message can't break the shell command typed into tmux
    # (literal newlines in -m would terminate the command early).
    return (
        # Resolve the lock path once; keep it out of the worktree so nothing
        # running in the tree can sweep it while the hooks run.
        'L="$(git rev-parse --absolute-git-dir)/mindflock_precommit.lock"; '
        # Drop any stale status marker from a PRIOR attempt up front: a
        # re-commit that is still running must never be read as the old
        # failure (nonzero status + dirty tree -> a false "interrupt" chip
        # while the hooks are live). The real result is written at the end.
        'rm -f {status}; touch "$L"; '
        # Everything below runs in a SUBSHELL whose INT/TERM/HUP trap drops the
        # lock. Ctrl+C on a running hook kills `git` and makes the interactive
        # shell abandon the rest of a `;` list, so the chain's own `rm -f "$L"`
        # never fired: the lock lingered and the pill stayed on "pre-commit"
        # until the stale-lock self-heal noticed (a grace window plus a
        # persistence debounce — most of a minute of a session looking wedged
        # right after the user cancelled it on purpose). The trap turns a cancel
        # back into the "commit" state within a poll. The self-heal stays as the
        # net for what a trap CANNOT catch (SIGKILL, the pane being killed).
        "( trap 'rm -f \"$L\"; exit 130' INT HUP TERM; "
        "git add -A; n=0; rc=1; "
        "while [ $n -lt 5 ]; do "
        # Refresh the lock each round so a multi-round retry (each hook pass
        # can outlast the liveness grace) can't be self-healed mid-flight.
        'touch "$L"; '
        "{sk}git {ident}commit -F {msgf}; rc=$?; "
        "[ $rc -eq 0 ] && break; "
        "git diff --quiet && break; "  # no auto-fix -> real failure
        "git add -A; n=$((n+1)); "
        "done; "
        'echo $rc > {status}; rm -f "$L"'
        # Drop the message file once the commit LANDED. It exists only so a
        # blocked commit can be retried (and re-offered in the dialog) with the
        # same message — but it used to survive success and unrelated work, so the
        # next commit that arrived without a message silently adopted a stale
        # subject. That is not theoretical: a run was caught about to record 19
        # files of one feature under a message describing a database migration.
        # Now a leftover message can only exist while a failure is genuinely
        # pending, which is exactly the case reuse is for.
        "; [ $rc -eq 0 ] && rm -f {msgf} || true )"
    ).format(
        msgf=shlex.quote(_COMMIT_MSG_FILE),
        status=_COMMIT_STATUS_FILE,
        # Empty by default, so the rendered command is unchanged. shlex.quote
        # leaves a plain id list (letters, digits, '.', '_', '-', ',') unquoted,
        # so `git commit -F .mindflock_commit_msg` survives verbatim as a suffix.
        sk=("SKIP=%s " % shlex.quote(skip)) if skip else "",
        # The person's git identity (Settings → Your devices), when set: this
        # commit is MindFlock's, made on their behalf. Empty otherwise — the
        # repo's git config decides and the command is unchanged.
        ident=_commit_identity(),
    )


def _commit_identity() -> str:
    """``-c user.name=… -c user.email=… `` (shell-quoted, trailing space) for
    :func:`_commit_shell_command`, or ``""``. Never raises."""
    try:
        from backend.web.core import person as _person

        args = _person.git_identity_args()
    except Exception:  # noqa: BLE001
        return ""
    return "".join(shlex.quote(a) + " " for a in args)


def _commit_into_shell(title: str, wt: str, msg: str, skip: str = "") -> Optional[str]:
    """Write the message file and type the commit one-liner into the session's
    shell. Returns an error string, or None on success. Blocking — call it in a
    worker thread.

    Extracted from ``instance_commit`` so the fast-track driver can issue a
    commit with a ``skip`` list WITHOUT the list ever coming from a client: the
    route always passes ``skip=""``, and the driver resolves it from settings.
    """
    # Keep our scratch files out of `git add -A`. Works for ANY session
    # type (plain / in-place / provisioned) — a neutral util, no
    # provisioning involved.
    try:
        _exclude_artifacts(Path(wt))
    except Exception:  # noqa: BLE001
        pass
    try:
        with open(os.path.join(wt, _COMMIT_MSG_FILE), "w") as f:
            f.write(msg)
    except OSError:
        pass
    name, err = _ensure_shell_session(title, wt)
    if err is not None:
        return err
    _send_to_shell(name, _commit_shell_command(skip))
    return None


def _is_human_session(inst) -> bool:
    """A person's own window: created by a human, not spawned by an agent
    (no ``Spawned`` flag, no ``Parent``)."""
    return not getattr(inst, "Spawned", False) and not (
        getattr(inst, "Parent", "") or ""
    )


@app.post("/api/instances/{title}/commit")
async def instance_commit(title: str, payload: dict) -> JSONResponse:
    """Stage everything and commit (running the pre-commit hooks) inside the
    session's interactive shell, so the user can watch the hooks live in the
    Terminal tab. A lock file marks the 'pre-commit running' stage."""
    if not git_available():
        return _no_git_response()
    inst, err = _inst_or_404(title)
    if err is not None:
        return err
    peer_err = _peer_guard.refusal(inst, "commit (use the peer checkpoint)")
    if peer_err is not None:
        return peer_err
    wt = inst.GetWorktreePath()
    if not wt:
        return JSONResponse({"error": "workspace not ready"}, status_code=409)
    msg = ((payload or {}).get("message", "") or "").strip()
    msg_file = os.path.join(wt, _COMMIT_MSG_FILE)
    # On a re-commit no message is sent — reuse the last one so a pre-commit
    # failure can be retried (re-staging the auto-fixes) without re-typing.
    if not msg and os.path.isfile(msg_file):
        try:
            with open(msg_file) as f:
                msg = f.read().strip()
        except OSError:
            msg = ""
    if not msg:
        return JSONResponse({"error": "commit message required"}, status_code=400)
    # Zones apply to your own windows too — as a WARNING. An agent is stopped
    # at the edit (the guard); a person typing is never blocked, so their
    # commit is where they hear about it: 409 + the same breach list the push
    # gate answers, and the dialog's "Commit anyway" re-POSTs proceed_anyway.
    if not (payload or {}).get("proceed_anyway") and _is_human_session(inst):
        try:
            hits = await asyncio.to_thread(_uncommitted_zone_breaches, wt)
        except Exception:  # noqa: BLE001 — a warning never blocks on its bugs
            hits = []
        if hits:
            return JSONResponse(
                {
                    "error": _zone_breach_message(hits),
                    "red_zone_breaches": hits,
                    "zone_warning": True,
                },
                status_code=409,
            )

    err = await asyncio.to_thread(_commit_into_shell, title, wt, msg)
    if err is not None:
        return JSONResponse({"error": err}, status_code=500)
    # The commit is about to change the stage — drop the probe memo so the
    # client's follow-up refresh sees fresh data instead of the 2.5s cache.
    _forget_probes(title)
    # …and watch for the hooks finishing, so "committed" is published the moment
    # it is true instead of at the next 4s tick.
    _live_stage.watch(title, wt, "commit")
    return JSONResponse({"ok": True, "tmux_name": _shell_tmux_name(title)})


@app.get("/api/instances/{title}/commit-message")
async def instance_commit_message(title: str) -> JSONResponse:
    """The message of a commit that the pre-commit hooks blocked, so the dialog
    can offer it back instead of making the user retype it.

    The message already survives on disk — ``git commit -F`` reads it from a
    git-excluded file in the worktree, and a re-commit with no message reuses it.
    What was missing is any way for the UI to *see* it: the dialog remembered the
    last message in a JS Map, which a page reload (or the server restart that
    prompts one) empties, while the file it was a copy of sat right there.

    Returned only when the recorded exit status is a failure — the same condition
    that raises the "interrupt" stage. After a commit succeeds its message is
    history, and pre-filling the next commit with it would be worse than empty.
    """
    if not git_available():
        return _no_git_response()
    inst, err = _inst_or_404(title)
    if err is not None:
        return err
    wt = inst.GetWorktreePath()
    if not wt:
        return JSONResponse({"error": "workspace not ready"}, status_code=409)

    def _read() -> str:
        try:
            with open(os.path.join(wt, _COMMIT_STATUS_FILE)) as f:
                if f.read().strip() in ("", "0"):
                    return ""
        except OSError:
            return ""  # no attempt recorded, or unreadable — nothing pending
        try:
            with open(os.path.join(wt, _COMMIT_MSG_FILE)) as f:
                return f.read().strip()
        except OSError:
            return ""

    return JSONResponse({"message": await asyncio.to_thread(_read)})


@app.post("/api/instances/{title}/commit-message/suggest")
async def instance_suggest_commit_message(
    title: str, payload: Optional[dict] = None
) -> JSONResponse:
    """Ask a model to write the commit message for this session's work (✨).

    Runs the session's own coding CLI headlessly against the diff — see
    :mod:`backend.web.core.commit_message` for why that is the model access this
    app has. 502 with a sentence when it can't: the dialog shows the reason and
    the message box stays exactly as the user left it, because a failed
    suggestion must not cost anyone a typed message.

    Nothing is committed, staged or written here. It reads the diff and answers.
    """
    if not git_available():
        return _no_git_response()
    inst, err = _inst_or_404(title)
    if err is not None:
        return err
    peer_err = _peer_guard.refusal(inst, "commit-message suggestions")
    if peer_err is not None:
        return peer_err
    wt = inst.GetWorktreePath()
    if not wt:
        return JSONResponse({"error": "workspace not ready"}, status_code=409)
    body = payload or {}
    # Whatever is already in the box travels as context, so a half-written subject
    # ("fix the token refresh thing") shapes the answer instead of being ignored.
    hint = str(body.get("hint") or "")[:500]
    program = str(getattr(inst, "Program", "") or "")

    def _work() -> str:
        return _commit_message.suggest(
            wt,
            program=program,
            hint=hint,
            branch=_current_branch(wt) or "",
            fallback_program=ENGINE.default_program(),
        )

    try:
        message = await asyncio.to_thread(_work)
    except _commit_message.CommitMessageError as err:  # noqa: BLE001
        return JSONResponse({"error": str(err)}, status_code=502)
    except Exception as err:  # noqa: BLE001 — never a 500 for a convenience
        return JSONResponse({"error": str(err)}, status_code=502)
    return JSONResponse({"message": message})


@app.get("/api/instances/{title}/ship-status")
async def instance_ship_status(title: str, tail: int = 0) -> JSONResponse:
    """The commit / push facts a caller of ``/commit`` and ``/push-branch``
    polls to learn whether its step finished — see
    :mod:`backend.web.core.ship_status`.

    Those two routes type into the session's shell and return at once, so
    "did my commit land" is only answerable by looking: the commit one-liner's
    exit status and when it was written, the commit lock, ``HEAD`` against the
    local ``origin/<branch>`` ref, and — with ``?tail=N`` (≤ 200) — the last
    ``N`` lines of the shell pane, which is where a failed hook or a refused
    push says why. Local git only (no network), so it is cheap to poll."""
    if not git_available():
        return _no_git_response()
    inst, err = _inst_or_404(title)
    if err is not None:
        return err
    wt = inst.GetWorktreePath()
    if not wt or not os.path.isdir(wt):
        return JSONResponse({"error": "workspace not ready"}, status_code=409)
    lines = max(0, min(int(tail or 0), _ship_status.MAX_TAIL_LINES))
    doc = await asyncio.to_thread(_ship_status.snapshot, inst, title, wt, lines)
    return JSONResponse(doc)


@app.get("/api/instances/{title}/stage")
async def instance_stage(title: str) -> JSONResponse:
    """ONE session's row, recomputed right now and published through.

    The freshness escape hatch ``GET /api/instances`` structurally cannot be:
    that route serves the tick's published snapshot for as long as
    ``time.time() - _SNAPSHOT_AT <= _INSTANCES_TICK_INTERVAL * 2.5`` (10s) and
    deliberately never rebuilds the expensive probes inline, because a cold full
    build blocks for seconds per big worktree and made server boot look hung. So
    a client invalidate within 10s of a publish provably returns the identical
    stale row — which is why the old post-action ``refreshInstances()`` could not
    make "Push" appear any sooner.

    Bounded to a single worktree and on-demand ONLY: never called on a schedule.
    Returns the same shape as one element of ``GET /api/instances``, so a client
    can merge it wholesale into its cached list.
    """
    if not git_available():
        return _no_git_response()
    _inst, err = _inst_or_404(title)
    if err is not None:
        return err
    row = await asyncio.to_thread(_republish_session, title)
    if row is None:
        return JSONResponse(
            {"error": "instance not found: %s" % title}, status_code=404
        )
    return JSONResponse(row)


@app.post("/api/instances/{title}/reset-stage")
async def instance_reset_stage(title: str) -> JSONResponse:
    """Put this window's guided cycle back to the start ("keep working here").

    The stage is git-derived, so it already returns to ``agent`` the moment the
    tree goes dirty. What this answers is the CLEAN-tree case: a branch that is
    committed / pushed / PR'd, where every control on the window is about
    advancing a cycle its owner considers finished, and the next thing they want
    to do is write more code on the same branch.

    NOTHING GIT-FACING HAPPENS HERE. No reset, no revert, no PR close: the pin is
    a display note (:mod:`backend.web.core.stage_reset`) that the UI's guided
    ladder honours and the published ``stage`` deliberately does not, so the
    autopilot driver and the verification-check kicker keep reading git truth. It
    releases itself as soon as the worktree moves — a new commit or a dirty tree.

    Two leftovers from the finished cycle go with it, because "back to idle" that
    leaves the previous run's badges up is not back to idle:
      * a HALTED fast-track record (a live one is left strictly alone — stopping
        someone's running chain is not what this button says it does), and
      * a STALE verification result, whose sha is not HEAD. A current failure is
        never touched: the push gate reads it.
    """
    if not git_available():
        return _no_git_response()
    inst, err = _inst_or_404(title)
    if err is not None:
        return err
    wt = inst.GetWorktreePath()
    if not wt:
        return JSONResponse({"error": "workspace not ready"}, status_code=409)

    def _work() -> dict:
        head = _git_head_sha(wt)
        dirty = _is_dirty(wt)
        # A dirty tree is ALREADY at the start of the ladder, so there is nothing
        # to pin — say so rather than store a pin the next stage read discards.
        pinned = (not dirty) and _stage_reset.pin(title, head)
        cleared = []
        run = _autopilot.get(title)
        if run and run.get("state") == "halted" and _autopilot.disarm(title):
            cleared.append("fast-track")
        chk = _wt_setup.check_summary(wt)
        if (
            chk
            and chk.get("stale")
            and not _wt_setup.is_running(wt, "check")
            and _wt_setup.clear_check(wt)
        ):
            cleared.append("checks")
        return {"pinned": bool(pinned), "dirty": bool(dirty), "cleared": cleared}

    res = await asyncio.to_thread(_work)
    # Same freshness contract as GET /stage: hand back the recomputed row so the
    # presser's window flips now instead of on the next 4s tick, and every other
    # client gets it through the publish.
    row = await asyncio.to_thread(_republish_session, title)
    return JSONResponse({"ok": True, "row": row, **res})


async def _red_zone_gate(
    inst, wt: str, payload: Optional[dict], branch: Optional[str] = None
) -> Optional[JSONResponse]:
    """409 when red-zone files are committed on this branch, else None.

    ``branch=None`` gates a push (``fork..HEAD``); a branch name gates a PR or
    merge (``<base>...origin/<branch>`` when pushed — what the forge will
    diff, see :func:`_pr_committed_changed`). ``override_red_zones: true``
    in the payload is the UI's explicit "push anyway". The autopilot calls
    these routes without it, so a fast-track chain halts here with the error
    as its reason — which is the point."""
    if (payload or {}).get("override_red_zones"):
        return None

    def _hits():
        target = "HEAD" if branch is None else _pr_content_ref(wt, branch)
        return _red_zone_breaches_for(inst, wt, target)

    try:
        hits = await asyncio.to_thread(_hits)
    except Exception:  # noqa: BLE001 — the gate fails open on its own bugs
        return None
    if not hits:
        return None
    if hits[0].get("kind") == "green":
        msg = "outside green zone: %s" % hits[0]["path"]
    else:
        msg = "red zone breached: %s (%s)" % (hits[0]["path"], hits[0]["pattern"])
    if len(hits) > 1:
        msg += " and %d more" % (len(hits) - 1)
    return JSONResponse({"error": msg, "red_zone_breaches": hits}, status_code=409)


@app.post("/api/instances/{title}/push-branch")
async def instance_push_branch(
    title: str, payload: Optional[dict] = None
) -> JSONResponse:
    """Push the branch from inside the shell (so pre-push hooks are visible)."""
    if not git_available():
        return _no_git_response()
    inst, err = _inst_or_404(title)
    if err is not None:
        return err
    peer_err = _peer_guard.refusal(inst, "push")
    if peer_err is not None:
        return peer_err
    wt = inst.GetWorktreePath()
    if not wt:
        return JSONResponse({"error": "workspace not ready"}, status_code=409)
    # Red zones: a zoned file committed on this branch blocks the push (409 +
    # red_zone_breaches; the UI re-POSTs override_red_zones to push anyway).
    # Before the O3 check gate — the graver of the two refusals comes first.
    gate = await _red_zone_gate(inst, wt, payload)
    if gate is not None:
        return gate
    # O3 verification gate (soft): a repo that declares a check_command wants
    # a passing run against the current HEAD before pushing. 409 carries the
    # check state; the UI offers "push anyway" which re-POSTs {"force": true}.
    if not (payload or {}).get("force"):
        cfg = _wt_setup.load_config(wt)
        if cfg.check_command:
            st = _wt_setup.check_summary(wt)
            if st is None or st.get("state") != "ok" or st.get("stale"):
                return JSONResponse(
                    {
                        "error": "checks haven't passed for this commit",
                        "check_required": True,
                        "check": st,
                    },
                    status_code=409,
                )
    # L2: without an origin remote the shell push dies with "fatal: 'origin'
    # does not appear..." while this endpoint used to report ok:true and the
    # stage stayed 'committed' forever. Fail loudly instead (fresh check —
    # the user may have just added the remote).
    if not await asyncio.to_thread(_has_origin, wt, True):
        return JSONResponse(
            {"error": "no origin remote — add one with: git remote add origin <url>"},
            status_code=400,
        )
    # The remote branch moved on (a teammate, or this person on another
    # machine, pushed to it): say so — who, how many — instead of letting the
    # shell push be rejected where nobody reads it. Every session, not only
    # shared ones; bounded fetch, fails open. Never a force-push: the way out
    # is POST /rebase-remote.
    push_branch = await asyncio.to_thread(_current_branch, wt)
    if push_branch:
        div = await asyncio.to_thread(
            _shared_branch.divergence, wt, "origin", push_branch
        )
        if div and div["behind"] > 0:
            return JSONResponse(
                {
                    "error": _shared_branch.diverged_message(div),
                    "behind": div["behind"],
                    "authors": div["authors"],
                    "remote": div["remote"],
                    "branch": div["branch"],
                    "diverged": True,
                },
                status_code=409,
            )

    def _do():
        name, err = _ensure_shell_session(title, wt)
        if err is not None:
            return err
        # --no-verify: skip the repo's pre-push hook (which re-runs the whole
        # pre-commit stack). The commit step already ran the hooks.
        # GIT_TERMINAL_PROMPT=0: a missing HTTPS credential fails at once
        # instead of waiting on a Username prompt nobody may be watching; the
        # output is kept in the private git dir, where the push watcher reads
        # an auth failure and says how to fix it (session.push_failed).
        _send_to_shell(name, _live_stage.push_command(wt))
        # The push is fire-and-forget into the shell — the branch is NOT on
        # origin yet when we return here. A one-shot cache pop would just let the
        # next poll re-cache the stale/None SHA for ~10s, stalling the Make PR
        # button. Instead mark the branch pending so every poll re-queries origin
        # until the push actually lands. Keyed by the LIVE branch — the stored
        # inst.Branch can drift when the user switches branches in the workspace.
        mark_origin_push_pending(wt, _current_branch(wt) or inst.Branch or "")
        return None

    err = await asyncio.to_thread(_do)
    if err is not None:
        return JSONResponse({"error": err}, status_code=500)
    _forget_probes(title)
    # Watch for the branch reaching origin, so "pushed" (and with it the Make PR
    # step) appears as soon as it is true.
    _live_stage.watch(title, wt, "push")
    return JSONResponse({"ok": True, "tmux_name": _shell_tmux_name(title)})


@app.post("/api/instances/{title}/rebase-remote")
async def instance_rebase_remote(
    title: str, payload: Optional[dict] = None
) -> JSONResponse:
    """``git pull --rebase <remote> <branch>`` in the worktree — the way out
    of a push refused as ``diverged`` (see core.shared_branch). ``remote``
    (optional, default ``origin`` — where the Push button pushes) must be a
    remote of the worktree; the branch is the one checked out.

    200 ``{"ok", "head", "behind", "row"}``. On a conflict the rebase is
    aborted (the tree and HEAD are exactly as before) and the answer is 409
    ``{"error", "conflicts": [paths]}`` so the person or the agent resolves
    it; 409 too for uncommitted tracked changes or a rebase already in
    progress. Never pushes, never force-pushes."""
    if not git_available():
        return _no_git_response()
    inst, err = _inst_or_404(title)
    if err is not None:
        return err
    peer_err = _peer_guard.refusal(inst, "push")
    if peer_err is not None:
        return peer_err
    wt = inst.GetWorktreePath()
    if not wt or not os.path.isdir(wt):
        return JSONResponse({"error": "workspace not ready"}, status_code=409)
    if _snapshot_activity(title) == "working":
        # A rebase (and its abort, a hard reset) under an agent that is editing
        # files would throw its edits away.
        return JSONResponse(
            {"error": "the agent is working — rebase once its turn ends", "busy": True},
            status_code=409,
        )
    branch = await asyncio.to_thread(_current_branch, wt)
    if not branch:
        return JSONResponse(
            {"error": "no branch checked out (detached HEAD)"}, status_code=409
        )
    remote = str((payload or {}).get("remote") or "origin").strip()
    if remote not in await asyncio.to_thread(_shared_branch.remote_names, wt):
        return JSONResponse({"error": "unknown remote: %s" % remote}, status_code=400)
    status, body = await asyncio.to_thread(
        _shared_branch.rebase_remote, wt, remote, branch
    )
    _forget_probes(title)
    if status == 200:
        body["row"] = await asyncio.to_thread(_republish_session, title)
    return JSONResponse(body, status_code=status)


@app.get("/api/instances/{title}/branches")
async def instance_branches(title: str) -> JSONResponse:
    """Branches this session can open a PR into — the Make-PR dialog's dropdown.

    Remote heads on ``origin`` (authoritative for "a branch that exists to merge
    into"); falls back to local branches when origin is unreachable so the list
    is never blank. Also returns the session's own branch (never a valid target)
    and the current default base, so the dialog can pre-select and warn."""
    if not git_available():
        return _no_git_response()
    inst, err = _inst_or_404(title)
    if err is not None:
        return err
    wt = inst.GetWorktreePath()
    if not wt:
        return JSONResponse({"error": "workspace not ready"}, status_code=409)

    def _list_branches():
        names: list = []
        cp = _run_capped(
            ["git", "-C", wt, "ls-remote", "--heads", "origin"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=20,
        )
        if cp.returncode == 0:
            prefix = "refs/heads/"
            for line in cp.stdout.decode("utf-8", "replace").splitlines():
                ref = line.split("\t")[-1].strip()
                if ref.startswith(prefix):
                    names.append(ref[len(prefix) :])
        if not names:  # offline / no origin — fall back to local heads
            cp2 = _run_capped(
                [
                    "git",
                    "-C",
                    wt,
                    "for-each-ref",
                    "--format=%(refname:short)",
                    "refs/heads",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            if cp2.returncode == 0:
                names = [
                    b.strip()
                    for b in cp2.stdout.decode("utf-8", "replace").splitlines()
                    if b.strip()
                ]
        return sorted(set(names))

    branches = await asyncio.to_thread(_list_branches)
    current = await asyncio.to_thread(_current_branch, wt) or inst.Branch or ""
    default = _configured_pr_base(wt) or _session_base_branch(inst)
    return JSONResponse({"branches": branches, "current": current, "default": default})


# The one remedy sentence every "MindFlock can't do the GitHub half itself"
# message ends with. Kept in one place because it is asserted verbatim in the
# tests and quoted in the docs: both rungs are optional and either one fixes it.
_PR_REMEDY = (
    "add a GitHub token in Settings → Connections → GitHub → Connect, "
    "or install the GitHub CLI"
)
#: Caps on make-pr's optional title / body overrides (GitHub's own body limit
#: is 65536 characters).
_PR_TITLE_MAX = 256
_PR_BODY_MAX = 60000


def _pr_browser_fallback(wt: str, base: str, branch: str) -> JSONResponse:
    """200 + a prefilled compare page when neither gh nor a token is around.

    Deliberately NOT an error status. The user's push already worked (pushing
    is always plain ``git push``), and the PR is one click away on the compare
    page GitHub prefills from the branch — so this is a handoff, not a failure,
    and the UI renders it as a link rather than a red toast."""
    url = _remote_url.compare_url(_github_pr.origin_url(wt), base, branch)
    # Only promise the compare link when there IS one (a non-GitHub origin
    # has none: the branch is pushed, and the PR is opened on its host).
    tail = (
        ". The compare link opens a pull request prefilled from this branch."
        if url
        else ". The branch is pushed — open the pull request on its host."
    )
    return JSONResponse(
        {
            "ok": False,
            "compare_url": url,
            "message": "MindFlock could not open the pull request for you — "
            + _PR_REMEDY
            + tail,
        }
    )


@app.post("/api/instances/{title}/make-pr")
async def instance_make_pr(title: str, payload: Optional[dict] = None) -> JSONResponse:
    """Open a PR into the base branch (auto title/body from the branch's commits).

    Three rungs, in order: ``gh`` when it is installed and authenticated, then
    the GitHub REST API with a resolved token, then the browser (a prefilled
    compare URL). gh stays first because it carries the user's own credentials
    and needs nothing configured — but its ABSENCE is never an error, which is
    the whole point: an SSH-remote user who never installed it can still open
    PRs from here.

    ``payload.base`` (the branch chosen in the Make-PR dialog) wins over every
    other source; blank/absent falls through to the configured default and then
    the session's fork-point (the prior behaviour).

    ``payload.title`` / ``payload.body`` (optional strings, capped at 256 /
    60000 chars) replace the commit-derived title / body on both the gh and
    the REST rung; the half not given still comes from the commits. The
    MindFlock MCP's ``ship_session`` sends a worker's report as the body."""
    if not git_available():
        return _no_git_response()
    inst, err = _inst_or_404(title)
    if err is not None:
        return err
    peer_err = _peer_guard.refusal(inst, "opening a PR")
    if peer_err is not None:
        return peer_err
    wt = inst.GetWorktreePath()
    if not wt:
        return JSONResponse({"error": "workspace not ready"}, status_code=409)
    # An explicit base from the dialog wins; then a configured default PR base
    # (Settings → "Default PR base branch"), so plain worktree sessions can PR
    # into a fixed branch (e.g. staging) instead of whatever they were cut from;
    # blank falls through to the per-session base (K1) — the prior behaviour.
    chosen = (payload or {}).get("base")
    base = (
        (chosen or "").strip() or _configured_pr_base(wt) or _session_base_branch(inst)
    )
    # PR the branch the worktree is *actually* on — the stored inst.Branch can
    # drift when the worktree is switched, which would PR the wrong branch.
    branch = _current_branch(wt) or inst.Branch or ""
    if not branch or branch == base:
        return JSONResponse(
            {
                "error": "cannot open a PR: this session is on the base branch "
                "'%s' — a branch can't be PR'd into itself. Switch the "
                "session to a feature branch first." % base
            },
            status_code=409,
        )
    gate = await _red_zone_gate(inst, wt, payload, branch)
    if gate is not None:
        return gate
    # Optional title / body overrides (the MindFlock MCP passes a worker's
    # report as the body). Absent, both come from the branch's commits, as
    # before. Strings only — they go to gh as single argv items, never a shell.
    pr_title = (payload or {}).get("title") or ""
    pr_body = (payload or {}).get("body") or ""
    if not isinstance(pr_title, str) or not isinstance(pr_body, str):
        return JSONResponse(
            {"error": "title and body must be strings"}, status_code=400
        )
    pr_title = pr_title.strip()[:_PR_TITLE_MAX]
    pr_body = pr_body.strip()[:_PR_BODY_MAX]

    def _do_gh():
        argv = ["gh", "pr", "create", "--base", base, "--head", branch]
        if pr_title or pr_body:
            # gh's --fill can't be mixed with an explicit body, so fill the
            # other half the same way the REST rung does.
            filled = _github_pr._fill(wt, base, branch)
            if filled is None:
                return 1, "no commits between %s and %s" % (base, branch)
            argv += [
                "--title",
                pr_title or filled[0],
                "--body",
                pr_body or filled[1],
            ]
        else:
            argv.append("--fill")
        cp = _run_capped(
            argv,
            cwd=wt,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=120,
        )
        out = cp.stdout.decode("utf-8", "replace").strip()
        return cp.returncode, out

    # (a) gh, (b) REST, (c) the browser — first rung that can run, wins.
    url, rc, out, handoff = None, 0, "", None
    if gh_available():
        rc, out = await asyncio.to_thread(_do_gh)
        if rc == 0 and out:
            url = out.splitlines()[-1]
    else:
        overrides = {k: v for k, v in (("title", pr_title), ("body", pr_body)) if v}
        res = await _github_pr.create_pr(wt, base, branch, **overrides)
        if res.unavailable:
            # No gh AND no token: hand the browser the prefilled compare page
            # rather than telling the user to go install something.
            handoff = _pr_browser_fallback(wt, base, branch)
        else:
            rc, out, url = (0 if res.ok else 1), res.error, res.url

    _PR_CACHE.pop(branch, None)  # force a fresh stage read next poll
    _MERGE_STATE_CACHE.pop(branch, None)
    _forget_probes(title)
    if handoff is not None:
        return handoff
    if rc != 0:
        # A currently-open PR is the expected bounce — surface its URL. A
        # merged/closed PR is *not* a bounce: the refusal has another reason
        # (usually no new commits), so fall through to the error message.
        existing = await asyncio.to_thread(_pr_info, wt, branch, True)
        if existing and existing.get("state") == "OPEN" and existing.get("url"):
            return JSONResponse(
                {"ok": True, "url": existing["url"], "note": "PR already open"}
            )
        # gh says "no commits between", the API says "No commits between" —
        # match either, so the friendly message doesn't depend on which rung ran.
        low = out.lower()
        if "could not find any commits" in low or "no commits between" in low:
            msg = (
                "nothing to PR: no commits between %s and %s — this branch's "
                "work may already be merged." % (base, branch)
            )
        else:
            # Transport-agnostic lead-in, then whatever the tool actually said.
            msg = "failed to open a pull request"
            if out:
                msg += ": " + out
        return JSONResponse({"error": msg}, status_code=400)
    return JSONResponse({"ok": True, "url": url})


def _merge_browser_fallback(
    wt: str, branch: str, pr_url: Optional[str]
) -> JSONResponse:
    """200 + a link to the PR when neither gh nor a token can merge it.

    Same handoff shape as :func:`_pr_browser_fallback`: merging is one click on
    the PR page, so the UI links out instead of reporting a failure. When the
    PR itself couldn't be looked up either, the repo's PR list filtered to this
    branch is the closest thing we can point at."""
    return JSONResponse(
        {
            "ok": False,
            "pr_url": pr_url
            or _remote_url.pr_list_url(_github_pr.origin_url(wt), branch),
            "message": "MindFlock could not merge the pull request for you — "
            + _PR_REMEDY
            + ". The link opens the pull request on GitHub.",
        }
    )


@app.post("/api/instances/{title}/merge-pr")
async def instance_merge_pr(title: str, payload: Optional[dict] = None) -> JSONResponse:
    """Merge the branch's PR into the base (a true merge commit). Confirmed in
    the UI; surfaces the underlying refusal (e.g. if the repo requires
    squash/review).

    Same three rungs as :func:`instance_make_pr` — ``gh``, then REST with a
    resolved token, then a link to the PR so the user can press Merge there.
    A red-zone file committed on the pushed branch refuses with 409 unless the
    payload carries ``override_red_zones: true``."""
    if not git_available():
        return _no_git_response()
    inst, err = _inst_or_404(title)
    if err is not None:
        return err
    peer_err = _peer_guard.refusal(inst, "merging a PR")
    if peer_err is not None:
        return peer_err
    wt = inst.GetWorktreePath()
    if not wt:
        return JSONResponse({"error": "workspace not ready"}, status_code=409)
    # Merge the PR of the branch the worktree is *actually* on — the stored
    # inst.Branch can drift when the user switches branches in the workspace,
    # which would merge the wrong branch's PR.
    branch = await asyncio.to_thread(_current_branch, wt) or inst.Branch or ""
    gate = await _red_zone_gate(inst, wt, payload, branch)
    if gate is not None:
        return gate

    def _do_gh():
        cp = _run_capped(
            ["gh", "pr", "merge", branch, "--merge"],
            cwd=wt,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=120,
        )
        return cp.returncode, cp.stdout.decode("utf-8", "replace").strip()

    if gh_available():
        rc, out = await asyncio.to_thread(_do_gh)
    else:
        # REST merges by PR NUMBER, so the branch has to be resolved to its PR
        # first — the same lookup the stage chip already uses.
        found = await _github_pr.find_pr(wt, branch)
        number = (found or {}).get("number")
        res = (
            await _github_pr.merge_pr(wt, number)
            if number
            else _github_pr.PRResult(unavailable=True)
        )
        if res.unavailable:
            _PR_CACHE.pop(branch, None)
            return _merge_browser_fallback(wt, branch, (found or {}).get("url"))
        rc, out = (0 if res.ok else 1), res.error

    _PR_CACHE.pop(branch, None)
    if rc != 0:
        msg = "failed to merge the pull request"
        return JSONResponse(
            {"error": (msg + ": " + out) if out else msg}, status_code=400
        )

    def _post_merge():
        # The flow for this branch is done — reset it to the top. Clear any
        # commit-status leftover, drop the branch's remote-SHA answer, and
        # refresh origin/<base> so _commits_beyond_base stops counting the
        # just-merged commits (otherwise the pill lands back on "pushed",
        # offering Make PR again, until the user happens to fetch).
        try:
            os.unlink(os.path.join(wt, _COMMIT_STATUS_FILE))
        except OSError:
            pass
        _ORIGIN_SHA_CACHE.pop((wt, branch), None)
        base = _configured_pr_base(wt) or _session_base_branch(inst)
        _run_capped(  # best-effort; a failure just leaves the old lag
            ["git", "-C", wt, "fetch", "origin", base],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
        )
        return base

    base = await asyncio.to_thread(_post_merge)
    _forget_probes(title)
    # Shipped: the ticket's claim label comes off (tracker_claims; the
    # assignment stays). A no-op for a session that mirrored nothing.
    _release_tracker_claim(title)
    # `base` names where it landed, for the UI's "Merged x into main".
    return JSONResponse({"ok": True, "base": base or ""})


@app.post("/api/instances/{title}/fast-track")
async def instance_fast_track(
    title: str, payload: Optional[dict] = None
) -> JSONResponse:
    """Arm the autopilot for this session: carry it to ``depth`` and stop.

    Arm-and-WAIT, deliberately: pressing this while the agent is still working
    records the target and lets the driver commit once the agent is verifiably
    done, rather than committing a half-written tree. That is also what makes this
    button and the intake option the same mechanism — the only difference is
    whether the session exists yet.

    ``depth`` defaults to the configured rung (Settings → Workspace, itself
    defaulting to "pr"). A commit message is required only when there is
    uncommitted work AND nothing is on disk to reuse — the same rule
    ``POST /commit`` already applies.
    """
    if not git_available():
        return _no_git_response()
    inst, err = _inst_or_404(title)
    if err is not None:
        return err
    peer_err = _peer_guard.refusal(inst, "autopilot")
    if peer_err is not None:
        return peer_err
    wt = inst.GetWorktreePath()
    if not wt:
        return JSONResponse({"error": "workspace not ready"}, status_code=409)
    body = payload or {}
    raw_depth = body.get("depth") or _fasttrack_depth()
    depth = _autopilot.normalize_depth(raw_depth)
    if depth not in _autopilot.DEPTHS:
        return JSONResponse({"error": "unknown depth: %s" % raw_depth}, status_code=400)
    if depth == "agent":
        return JSONResponse(
            {"error": "the agent rung is for intake — this session already exists"},
            status_code=400,
        )
    lane = _lanes.lane_of_depth(depth)
    refused = await asyncio.to_thread(_lane_refusal, title, lane)
    if refused is not None:
        return refused
    by = _lane_chooser(body.get("by"))
    # The compatible alias never LOWERS a guard: an "ask me before it ships"
    # someone chose stays on (unticking it is a /lane call with ask_first).
    prev = await asyncio.to_thread(_autopilot.get, title)
    ask_first = bool(prev.get("ask_first")) if prev else None
    try:
        held, ask_first = await asyncio.to_thread(
            _team_run_driver.user_lane, title, lane, ask_first
        )
    except _team_run_driver.RunError as err:
        return JSONResponse({"error": err.message}, status_code=err.status)
    if held:
        return JSONResponse(
            {
                "error": "its team run is paused — the fast-track is kept and armed on resume",
                "held": True,
            },
            status_code=409,
        )
    # The lane machinery owns the arm (message rules included) — this route is
    # its oldest caller and stays as the compatible alias of POST /lane.
    try:
        rec = await asyncio.to_thread(
            lambda: _lanes.arm_session(
                title,
                lane,
                ask_first=bool(ask_first),
                message=str(body.get("message") or ""),
                base=str(body.get("base") or ""),
                by=by,
            )
        )
    except _lanes.LaneError as err:
        return JSONResponse({"error": err.message}, status_code=err.status)
    if rec is None:
        return JSONResponse({"error": "could not arm fast-track"}, status_code=500)
    # Cheap: no probes, no network. Arming changes a field in a JSON file, so the
    # press must not wait on a PR lookup (that cost ~700ms on a cold cache, ON the
    # event loop). The response carries the authoritative record so the client can
    # settle its toggle without a follow-up read.
    _publish_autopilot(title)
    return JSONResponse({"ok": True, "autopilot": _autopilot_dto(title)})


@app.post("/api/instances/{title}/lane")
async def instance_set_lane(title: str, payload: Optional[dict] = None) -> JSONResponse:
    """Choose this session's ship lane: ``{"lane": "leave"|"commit"|"push"|
    "pr"|"merge", "ask_first": bool}`` → ``{"ok", "lane", "autopilot"}``.

    The lane is carried out by the autopilot (``core.lanes``): arm-and-wait,
    exactly like ``/fast-track``, which stays as its compatible alias.
    ``leave`` disarms (``lane`` comes back null). ``ask_first`` holds the run
    one rung short of its first outward step and parks it in the bell for
    your go (``POST /ship-now``)."""
    peer_err = _peer_guard.title_refusal(title, "ship lanes")
    if peer_err is not None:
        return peer_err
    if not git_available():
        return _no_git_response()
    body = payload or {}
    lane = _lanes.normalize_lane(body.get("lane"))
    if not lane:
        return JSONResponse(
            {"error": "unknown lane — pick one of: " + ", ".join(_lanes.LANES)},
            status_code=400,
        )
    ask_first = body.get("ask_first", False)
    if not isinstance(ask_first, bool):
        return JSONResponse({"error": "ask_first must be a boolean"}, status_code=400)
    refused = await asyncio.to_thread(_lane_refusal, title, lane)
    if refused is not None:
        return refused
    try:
        held, _ask = await asyncio.to_thread(
            _team_run_driver.user_lane, title, lane, ask_first
        )
    except _team_run_driver.RunError as err:
        return JSONResponse({"error": err.message}, status_code=err.status)
    if held:
        # A paused group's member: the choice is on the task and is armed on
        # resume — nothing ships while the group is paused.
        return JSONResponse(
            {
                "ok": True,
                "held": True,
                "lane": _row_lane(title),
                "autopilot": _autopilot_dto(title),
            }
        )
    try:
        await asyncio.to_thread(
            lambda: _lanes.arm_session(
                title,
                lane,
                ask_first=ask_first,
                message=str(body.get("message") or ""),
                base=str(body.get("base") or ""),
                by=_lane_chooser(body.get("by")),
            )
        )
    except _lanes.LaneError as err:
        return JSONResponse({"error": err.message}, status_code=err.status)
    _publish_autopilot(title)
    return JSONResponse(
        {
            "ok": True,
            "lane": _lanes.lane_dto(title),
            "autopilot": _autopilot_dto(title),
        }
    )


def _lane_chooser(raw) -> str:
    """Who is choosing a lane: ``"agent:<title>"`` when the MindFlock MCP says
    so for a live session (its ``set_autopilot``), else the user."""
    by = str(raw or "").strip()
    if by.startswith("agent:") and by[len("agent:") :] in ENGINE.instances:
        return by
    return "user"


def _lane_refusal(title: str, lane: str, ship: bool = False):
    """409 when ``title`` is a COPY window of a branch another window drives:
    arming it would put two drivers on one tree. "Leave it" (which only
    disarms) is always allowed. None = go ahead."""
    if lane == "leave" and not ship:
        return None
    try:
        sharer = _lanes.shared_checkout(title)
    except Exception:  # noqa: BLE001
        sharer = ""
    if sharer:
        return JSONResponse(
            {
                "error": "%s works in this same folder — fast-tracking here would commit "
                "both sessions' work as one; start this session in its own "
                "worktree to fast-track it" % sharer,
                "shared_with": sharer,
            },
            status_code=409,
        )
    try:
        driver = _lanes.branch_driver(title)
    except Exception:  # noqa: BLE001 — no answer: do not block
        driver = ""
    if not driver:
        return None
    return JSONResponse(
        {
            "error": "%s drives this branch — set its lane or ship it from that "
            "window (a copy window is never armed)" % driver,
            "driver": driver,
        },
        status_code=409,
    )


#: Why ``ship-now`` refuses, by the agent's activity: shipping now would take
#: half-finished work (or answer a dialog by committing under it).
_SHIP_NOW_BLOCKED = {
    "working": "the agent is mid-turn — stop it first",
    "clarify": "the agent is waiting on a prompt — answer it first",
    "limit": "the agent stopped at its usage limit — its work may be unfinished",
}


@app.post("/api/instances/{title}/ship-now")
async def instance_ship_now(title: str, payload: Optional[dict] = None) -> JSONResponse:
    """Ship what is there now: arm the session's lane (or ``{"lane": ...}``)
    WITHOUT the idle dwell and without "ask first" — the approval behind the
    bell's Commit / Open the PR (``{"commit_message": ...}`` commits the
    card's edited message as written), and "take what's there now" from the
    pane menu. 409 while the agent is mid-turn."""
    if not git_available():
        return _no_git_response()
    inst, err = _inst_or_404(title)
    if err is not None:
        return err
    peer_err = _peer_guard.refusal(inst, "shipping")
    if peer_err is not None:
        return peer_err
    try:
        wt = inst.GetWorktreePath()
    except Exception:  # noqa: BLE001
        wt = ""
    if not wt:
        return JSONResponse({"error": "workspace not ready"}, status_code=409)
    activity = await asyncio.to_thread(_agent_activity, inst, title)
    if activity in _SHIP_NOW_BLOCKED:
        return JSONResponse(
            {"error": _SHIP_NOW_BLOCKED[activity], "activity": activity},
            status_code=409,
        )
    body = payload or {}
    message = body.get("commit_message", "")
    if not isinstance(message, str):
        return JSONResponse(
            {"error": "commit_message must be a string"}, status_code=400
        )
    refused = await asyncio.to_thread(_lane_refusal, title, "", True)
    if refused is not None:
        return refused
    try:
        await asyncio.to_thread(_team_run_driver.user_lane, title, "", None, True)
    except _team_run_driver.RunError as err:
        return JSONResponse({"error": err.message}, status_code=err.status)
    try:
        await asyncio.to_thread(
            lambda: _lanes.ship_now(
                title, str(body.get("lane") or ""), message=message.strip()[:5000]
            )
        )
    except _lanes.LaneError as err:
        return JSONResponse({"error": err.message}, status_code=err.status)
    _publish_autopilot(title)
    return JSONResponse(
        {"ok": True, "lane": _lanes.lane_dto(title), "autopilot": _autopilot_dto(title)}
    )


@app.delete("/api/instances/{title}/fast-track")
async def instance_fast_track_cancel(title: str) -> JSONResponse:
    """Disarm the autopilot. Anything already typed into the shell keeps running —
    this stops the driver from taking the NEXT step, which is the only thing it
    controls."""
    inst, err = _inst_or_404(title)
    if err is not None:
        return err
    stopped = await asyncio.to_thread(_autopilot.disarm, title)
    _publish_autopilot(title)
    return JSONResponse({"ok": True, "stopped": bool(stopped)})


# --- Team runs + the outbox: routes/team_runs.py ------------------------------
app.include_router(_team_runs_routes.router)
app.include_router(_speech_routes.router)

# --- Voice mode (talking to a session from the phone): routes/voice.py --------
app.include_router(_voice_routes.router)


# --- Forced PR review (Intake → Pull requests) ----------------------------------
# The automated monitor silently skips PRs that are already in the processed
# ledger, not yours, or too young. These endpoints let the Settings screen show
# every open PR with the skip reason and force-start a review session for one,
# bypassing those filters (see backend.web.core.pr_review).

# PRs/issues/tickets with a force-start currently provisioning live in
# core.pending, which guards a double-click from provisioning the same
# workspace twice, renders the sidebar's provisioning row, and greens the
# sidebar bar's dot while the work is being brought in.


_OPEN_PRS_CACHE: dict = {}  # "v" -> (fresh_until_mono, payload); "task" -> refresh


# How long a fan-out payload counts as fresh, and how far past that it may
# still be SERVED (while a refresh runs behind the request). The settings
# panels unmount on close, so every visit used to pay the full sweep —
# measured at ~3s for the ticket fan-out. Anything older than the stale
# window is worth waiting for instead of showing.
_FANOUT_TTL = 20.0
_FANOUT_MAX_STALE = 300.0
# A sick upstream must not be hit harder than a healthy one: a client that is
# handed a stale payload comes back for the replacement, so without a backoff
# every one of those polls would re-arm a sweep for the whole stale window.
_FANOUT_ERROR_BACKOFF = 30.0
# One sweep may not wedge the panel forever. Without this a hung ls-remote
# holds the single-flight slot indefinitely, so no later refresh can start.
_FANOUT_SWEEP_TIMEOUT = 60.0


def _schedule_fanout_refresh(cache: dict, loader, ttl: float) -> None:
    """Refresh ``cache`` in the background, at most one sweep in flight.

    A failure leaves the stale payload in place (and expires nothing): the
    panel keeps showing the last known list rather than emptying itself
    because GitHub blipped. It also starts a backoff, so the next few reads
    serve that payload without asking the failing upstream again."""
    task = cache.get("task")
    if task is not None and not task.done():
        return

    async def _refresh() -> None:
        try:
            data = await asyncio.wait_for(loader(), _FANOUT_SWEEP_TIMEOUT)
            cache["v"] = (time.monotonic() + ttl, data)
            cache.pop("retry_after", None)
        except Exception as err:  # noqa: BLE001 — token / network / timeout
            cache["retry_after"] = time.monotonic() + _FANOUT_ERROR_BACKOFF
            if log.ErrorLog is not None:
                log.ErrorLog.Printf("background fan-out refresh failed: %v", err)
        finally:
            cache.pop("task", None)

    cache["task"] = asyncio.create_task(_refresh())


async def _cached_fanout(
    cache: dict,
    loader,
    *,
    fresh: bool = False,
    ttl: float = _FANOUT_TTL,
    max_stale: float = _FANOUT_MAX_STALE,
) -> tuple[dict, bool]:
    """A settings panel's upstream fan-out, kept off the request path.

    Returns ``(payload, stale)``. Fresh hit → the cached payload. Past the TTL
    but inside the stale window → the stale payload plus a background refresh,
    so opening the panel does not wait on GitHub/the ticket sources. Nothing
    usable cached (or ``fresh``, from an explicit Refresh click) → await the
    sweep and let the loader's exception reach the 502 path.

    ``stale`` rides along in the response so the client can pull the fresh
    copy in a moment rather than sit on data it knows is being replaced."""
    if not fresh:
        now = time.monotonic()
        cached = cache.get("v")
        if cached is not None:
            if cached[0] > now:
                return cached[1], False
            if cached[0] + max_stale > now:
                # Still marked stale during a backoff — the payload really is
                # old — but no sweep is armed until the upstream gets a rest.
                if cache.get("retry_after", 0.0) <= now:
                    _schedule_fanout_refresh(cache, loader, ttl)
                return cached[1], True
    data = await loader()
    cache["v"] = (time.monotonic() + ttl, data)
    return data, False


# How each panel's rows describe the workspace a run of them would own. ONE
# definition per kind: the listing probes with it (to decide whether the row
# gets a Reopen button) and the reopen endpoint re-resolves with it (to find the
# directory the click meant). Two copies of these would let the button and the
# action disagree about which workspace a row is talking about.
def _ticket_workspace_args(row: dict) -> dict:
    return {
        "title": str(row.get("session") or ""),
        "branch": str(row.get("branch") or ""),
        "repo_url": str(row.get("repo_url") or ""),
        "strategy": str(row.get("strategy") or ""),
    }


def _pr_workspace_args(row: dict) -> dict:
    return {
        "title": str(row.get("session") or ""),
        "branch": str(row.get("head_ref") or ""),
        "workspace_path": str(row.get("workspace_path") or ""),
        # PR review always provisions its own clone of the head.
        "strategy": "clone",
    }


def _issue_workspace_args(row: dict) -> dict:
    return {
        "title": str(row.get("session") or ""),
        "branch": str(row.get("branch") or ""),
        "strategy": str(row.get("strategy") or ""),
    }


async def _annotate_workspaces(rows: list, resolve) -> None:
    """Stamp ``workspace`` onto the rows of a panel response that can be
    reopened rather than started over (see :mod:`backend.web.core.reopen`).

    Annotated here, on the per-request copies, for the same reason
    ``has_session`` is: the listing itself is cached for minutes, while a
    workspace can be deleted or a session closed at any moment, and a Reopen
    button pointing at a directory that is gone is worse than no button.

    Off the event loop, because it is blocking work whose size is the *panel's*,
    not the machine's: a thousand assigned tickets means a thousand ``isdir``
    probes and, when a base clone is involved, a ``git worktree list``. Run
    inline, that stalls every other request the open dialog fires alongside this
    one — which is most of what made opening Intake feel slow even on a warm
    cache.
    """
    try:
        await asyncio.to_thread(_reopen.annotate, rows, resolve)
    except Exception as err:  # noqa: BLE001 — never fail a listing over a probe
        if log.ErrorLog is not None:
            log.ErrorLog.Printf("workspace annotation failed: %v", err)


@app.get("/api/github/prs")
async def github_open_prs(fresh: bool = False) -> JSONResponse:
    """Open PRs on the review repos, annotated with auto-review eligibility.

    The GitHub fan-out (user login + one list call per repo) is cached and
    served stale-while-revalidate (see ``_cached_fanout``); ``fresh=1`` is the
    Refresh button, which means what it says and waits for a real sweep.
    ``has_session`` is annotated on a per-request copy so it stays live even
    on cache hits."""
    try:
        data, stale = await _cached_fanout(
            _OPEN_PRS_CACHE, _pr_review.list_open_prs, fresh=fresh
        )
    except Exception as err:  # noqa: BLE001 — unconfigured / token / network
        return JSONResponse({"error": str(err)}, status_code=502)
    data = {**data, "stale": stale, "prs": [dict(p) for p in data.get("prs", [])]}
    for p in data.get("prs", []):
        p["has_session"] = p.get("session") in ENGINE.instances or _pending_has(
            p.get("session")
        )
    await _annotate_workspaces(data.get("prs", []), _pr_workspace_args)
    return JSONResponse(data)


@app.post("/api/github/prs/review")
async def github_force_review(payload: dict) -> JSONResponse:
    """Force-start a review session for one open PR, bypassing auto filters."""
    if not git_available():
        return _no_git_response()
    payload = payload or {}
    repo = str(payload.get("repo", "") or "").strip()
    if not re.match(r"^[^\s/]+/[^\s/]+$", repo):
        return JSONResponse({"error": "repo must be owner/name"}, status_code=400)
    try:
        number = int(payload.get("number"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "number must be an integer"}, status_code=400)
    try:
        agent_override = _start_agent_override(payload)
        depth_override = _start_depth_override(payload)
        effort_override = _start_effort_override(payload)
    except ValueError as err:
        return JSONResponse({"error": str(err)}, status_code=400)

    # The provisioning row goes up BEFORE the GitHub lookup below, which is the
    # last thing standing between the click and the sidebar showing anything.
    # The panel's cached list already carries the title this PR maps to, so no
    # slug re-derivation is involved; a cold cache just falls back to the
    # post-lookup registration further down.
    early = _cached_session_title(
        _OPEN_PRS_CACHE,
        "prs",
        lambda p: p.get("repo") == repo and p.get("number") == number,
    )
    if early:
        if early in ENGINE.instances or _pending_has(early):
            return JSONResponse(
                {
                    "error": "session %s already exists — close it to re-review"
                    % early,
                    "title": early,
                },
                status_code=409,
            )
        _pending_add(early, "pr", repo=repo)

    try:
        pr = await _pr_review.find_pr(repo, number)
    except LookupError as err:
        _pending_drop(early)
        return JSONResponse({"error": str(err)}, status_code=404)
    except Exception as err:  # noqa: BLE001
        _pending_drop(early)
        return JSONResponse({"error": str(err)}, status_code=502)

    title = _pr_review.session_title(pr)
    if title != early:
        _pending_drop(early)  # stale cache entry — keep only the real title
        if title in ENGINE.instances or _pending_has(title):
            return JSONResponse(
                {
                    "error": "session %s already exists — close it to re-review"
                    % title,
                    "title": title,
                },
                status_code=409,
            )
    # Re-registered with the branch now that it's known (add() keeps `since`).
    _pending_add(title, "pr", branch=pr.head_ref, repo=repo)
    _arm_intake_autopilot(
        title,
        depth_override or _repo_intake_depth(repo, "prs"),
        "pr",
        "%s#%s" % (repo, number),
        message=str(getattr(pr, "title", "") or ""),
    )

    async def _bg_review() -> None:
        # Provision first (slow: clone/fetch of the PR head), then register the
        # adopted workspace as a live session — same shape as the pipeline's
        # SessionRunner.run_pr, but against THIS server's engine so the session
        # shows up in the grid without a reload.
        try:
            directory, prompt, n_comments = await _pr_review.prepare_review(pr)
            # This start's own pick, then this repo's card, then Intake → Pull
            # requests' screen-wide Agent CLI (same chain the auto monitor uses),
            # then the app-wide default. Mirrors the issue force-start below,
            # which reads the same chain. Resolved first because this start's
            # effort has to be translated into THAT CLI's spelling.
            program = (
                agent_override
                or _pr_review.review_agent(repo)
                or ENGINE.default_program()
            )
            prompt = _provider_effort.decorate_prompt(prompt, program, effort_override)
            # The review workspace already exists here: decorate against it.
            prompt = await asyncio.to_thread(
                _red_zone_prompt, prompt, program, [str(directory)], None
            )
            inst = session.NewInstance(
                session.InstanceOptions(
                    title=title,
                    path=".",
                    program=program,
                    provisioned=True,
                    workspace_strategy="clone",
                    new_branch=pr.head_ref,
                    prompt=prompt,
                    workspace_path=str(directory),
                    launch_args=_start_launch_args(program, effort_override),
                )
            )
            inst.ExtraEnv = _ports.env_for(title)
            inst.SetStatus(Loading)
            with ENGINE.lock:
                ENGINE.instances[title] = inst
            _seed_event_snapshot(title)
            _events.BUS.emit(
                "session.created",
                session=title,
                new="loading",
                data={"program": inst.Program, "provisioned": True, "pr": pr.number},
            )
            try:
                await asyncio.to_thread(inst.Start, True)
                ENGINE.save()
            except Exception:
                # By identity: this task may be the loser of a re-start, and
                # popping by name would delete the LIVE session's record.
                _drop_failed_start(title, inst)
                raise
            # Ledger the PR so auto review doesn't run it a second time.
            _pr_review.record_reviewed(pr)
            if log.InfoLog is not None:
                log.InfoLog.Printf(
                    "forced PR review %s live (%d comments)", title, n_comments
                )
        except Exception as err:  # noqa: BLE001
            if log.ErrorLog is not None:
                log.ErrorLog.Printf("forced PR review %s failed: %v", title, err)
            _events.BUS.emit(
                "session.create_failed", session=title, data={"error": str(err)}
            )
        finally:
            _pending_drop(title)

    _register_task(_bg_review())
    return JSONResponse({"started": True, "title": title}, status_code=202)


# --- Ticket force-start (Intake → Tickets) --------------------------------
# The ticket twin of the PR force-review endpoints above: list every ticket
# assigned to you on the configured sources with the reason auto ingestion has
# or hasn't picked it up, and force-start a session for one, bypassing those
# filters (see backend.web.core.ticket_start).

_ASSIGNED_TICKETS_CACHE: dict = {}  # "v" -> (fresh_until_mono, payload)


@app.get("/api/tickets")
async def assigned_tickets(fresh: bool = False) -> JSONResponse:
    """Assigned tickets on the configured sources, annotated with auto-ingest
    eligibility. The provider fan-out (one search per source + a ls-remote per
    repo) is the slowest of the three panels — cached and served
    stale-while-revalidate, like the open-PRs panel; ``has_session`` is
    annotated on a per-request copy so it stays live even on cache hits."""
    try:
        data, stale = await _cached_fanout(
            _ASSIGNED_TICKETS_CACHE, _ticket_start.list_assigned_tickets, fresh=fresh
        )
    except Exception as err:  # noqa: BLE001 — unconfigured / network
        return JSONResponse({"error": str(err)}, status_code=502)
    data = {
        **data,
        "stale": stale,
        "tickets": [dict(t) for t in data.get("tickets", [])],
    }
    for t in data.get("tickets", []):
        t["has_session"] = t.get("session") in ENGINE.instances or _pending_has(
            t.get("session")
        )
    await _annotate_fleet_holders(data.get("tickets", []))
    await _annotate_workspaces(data.get("tickets", []), _ticket_workspace_args)
    data["source_status"] = await asyncio.to_thread(_ticket_source_status, data)
    return JSONResponse(data)


def _ticket_source_status(listing: dict) -> dict:
    """``{source key: {"connect_error", "last_test_ok", "last_test_at"}}`` for
    every CONFIGURED ticket source — so a source that cannot connect is never
    shown as "No tickets are assigned to you" (round 5).

    ``connect_error`` is ``"can't connect — <reason>"`` or ``None``. The reason
    is the listing's own error for that source; a source the listing did not
    reach (the cached fan-out predates it) falls back to its stored Test
    verdict, which is read fresh here, while one the listing reached cleanly
    is connected whatever an older Test said. ``last_test_ok`` /
    ``last_test_at`` restate that stored verdict (``None`` = never tested)."""
    try:
        from backend.config import settings as _settings

        sources = list(_settings.load_settings().ticketing.sources)
    except Exception:  # noqa: BLE001 — no settings yet: nothing configured
        return {}
    errors: dict = {}
    for e in listing.get("errors") or []:
        if isinstance(e, dict) and e.get("source"):
            errors.setdefault(str(e["source"]), str(e.get("error") or ""))
    listed = {str(k) for k in (listing.get("sources") or [])}
    out: dict = {}
    for src in sources:
        key = str(src.id or src.provider or "")
        if not key:
            continue
        test = src.last_test if isinstance(src.last_test, dict) else {}
        test_ok = bool(test["ok"]) if "ok" in test else None
        reason = None
        if key in errors:
            reason = errors[key] or "the tracker refused the request"
        elif key not in listed and test_ok is False:
            reason = str(test.get("message") or "") or "the last test failed"
        out[key] = {
            "connect_error": ("can't connect — %s" % reason) if reason else None,
            "last_test_ok": test_ok,
            "last_test_at": str(test.get("at") or "") or None,
        }
    return out


async def _annotate_fleet_holders(tickets: list) -> None:
    """Mark rows another of the user's devices already holds (fleet_claims):
    ``elsewhere`` names the device, the reason chip says so, and the row is no
    longer eligible — the queue view and the agent tools skip it."""
    try:
        fleet = await _fleet_claims.fleet_claims()
    except Exception:  # noqa: BLE001 — the listing never fails on a peer
        return
    if not fleet:
        return
    for t in tickets:
        if t.get("has_session"):
            continue
        claims = fleet.get(str(t.get("session") or ""))
        if not claims:
            continue
        claim = sorted(claims, key=lambda c: c["kind"] != "session")[0]
        t["elsewhere"] = {
            "device": claim["device"],
            "label": claim["label"],
            "kind": claim["kind"],
        }
        reason = _fleet_claims.describe(claim)
        t["reasons"] = [reason, *[r for r in (t.get("reasons") or []) if r != reason]]
        t["eligible"] = False


@app.get("/api/person")
def get_person() -> JSONResponse:
    """The person this device belongs to (core.person; docs/people.md):
    ``{"id", "name", "git_name", "git_email"}``. Shared by all of the
    person's devices; an identifier, never a credential."""
    return JSONResponse(_person.current())


@app.post("/api/person")
async def post_person(payload: dict, request: Request) -> JSONResponse:
    """``{name?, git_name?, git_email?}`` (``""`` clears; absent = unchanged)
    → the person. The id can't be set here: devices agree on it (pairing,
    then settings sync). Synced fields, so it needs what any synced setting
    needs (:func:`core.auth.may_configure`); 400 on a git email that isn't."""
    from backend.config import settings as _settings

    if not await _auth.may_configure(request.scope):
        return _auth.configure_refused()
    payload = payload if isinstance(payload, dict) else {}
    fields = {
        k: payload[k]
        for k in ("name", "git_name", "git_email")
        if k in payload and payload[k] is not None
    }
    try:
        out = await asyncio.to_thread(_person.update, **fields)
    except ValueError as err:
        return JSONResponse({"error": str(err)}, status_code=400)
    except _settings.SettingsUnreadable as err:
        return JSONResponse({"error": str(err)}, status_code=409)
    try:
        _settings_sync.local_change(["person.%s" % k for k in fields])
    except Exception:  # noqa: BLE001 — the save stands without the nudge
        pass
    return JSONResponse(out)


@app.get("/api/tickets/claims")
def ticket_claims() -> JSONResponse:
    """The tickets THIS device holds — what another of the user's devices asks
    before starting one (backend.web.core.fleet_claims)."""
    return JSONResponse(_fleet_claims.claims_json())


@app.get("/api/tickets/fleet-holder")
async def ticket_fleet_holder(
    request: Request, slug: str, since: float = 0.0
) -> JSONResponse:
    """Which other device holds ``slug`` (``{"holder": null}`` when none) —
    the ingestion pipeline's pre-launch check, asked of its own server. With
    ``since`` (this device's own in_flight marker time) only an older peer
    marker counts, which is what breaks a simultaneous start. Local callers
    only: a device relaying another's question would make it two hops."""
    if _remote.from_remote(request):
        return JSONResponse({"error": "local callers only"}, status_code=403)
    claim = await _fleet_claims.holder(slug, own_since=since or None, fresh=True)
    return JSONResponse(
        {"holder": claim, "reason": _fleet_claims.describe(claim) if claim else ""}
    )


@app.post("/api/tickets/start")
async def ticket_force_start(payload: dict, request: Request) -> JSONResponse:
    """Force-start a coding session for one ticket, bypassing auto filters.

    409 with ``claimed_by`` when the tracker says a teammate is on it
    (tracker_claims); ``override_claim: true`` starts it anyway — the owner's
    call only: never for an agent's start (lineage), and only with what any
    owner setting needs (``auth.may_configure``, else 403)."""
    if not git_available():
        return _no_git_response()
    payload = payload or {}
    source = str(payload.get("source", "") or "").strip()
    ticket_id = str(payload.get("id", "") or "").strip()
    if not source or not ticket_id:
        return JSONResponse({"error": "source and id are required"}, status_code=400)
    try:
        agent_override = _start_agent_override(payload)
        depth_override = _start_depth_override(payload)
        effort_override = _start_effort_override(payload)
    except ValueError as err:
        return JSONResponse({"error": str(err)}, status_code=400)
    # A multi-repo source's ticket, started in a repo picked on the row
    # (validated against the source's repos by the launch).
    repo = payload.get("repo") or ""
    if not isinstance(repo, str):
        return JSONResponse({"error": "repo must be a string"}, status_code=400)
    # An agent starting the ticket as its own worker (parent / spawned /
    # report_back / note); all-empty for the Intake panel.
    lineage, lin_err = await _intake_lineage(payload)
    if lin_err is not None:
        return lin_err
    override_claim = bool(payload.get("override_claim"))
    if override_claim and (
        lineage.get("parent")
        or lineage.get("spawned")
        or not await _auth.may_configure(request.scope)
    ):
        return JSONResponse(
            {"error": "only the owner can start a ticket someone else is on"},
            status_code=403,
        )
    # The launch itself lives in core.ticket_start so a team run starts its
    # ticket tasks through the very same code.
    try:
        body = await _ticket_start.launch(
            source,
            ticket_id,
            depth=depth_override,
            agent=agent_override,
            effort=effort_override,
            lineage=lineage,
            override_claim=override_claim,
            repo=repo,
        )
    except _ticket_start.LaunchError as err:
        return JSONResponse(err.body, status_code=err.status)
    return JSONResponse(body, status_code=202)


# --- Ticket merge (Intake → Tickets) --------------------------------------
# Duplicate tickets are a tracker problem, so the fix writes to the tracker:
# everything on ticket A is appended to ticket B and A is deleted. The step
# order, and why a failed delete comes back in the payload instead of as a 500,
# are in backend.web.core.ticket_merge — read that before changing this.


def _notify_ticket_merged(result: dict) -> None:
    """Push "A is gone, its content is on B" to the user's phone, when ntfy is on.

    Deleting a ticket is the least reversible thing this app does to a system
    it does not own, and the person who pressed the button is frequently not the
    only person who cared about that ticket — so the confirmation is worth
    carrying past the tab that started it. Best effort and fully wrapped: a
    notification must never be able to fail a merge that has already happened.
    """
    try:
        cfg = _ntfy.load()
        if not cfg.active:
            return
        gone = result.get("from") or {}
        kept = result.get("into") or {}
        _ntfy.publish_soon(
            cfg,
            title="%s merged into %s" % (gone.get("slug", "?"), kept.get("slug", "?")),
            message=(
                "%s — %s\n%s"
                % (
                    gone.get("slug", "?"),
                    gone.get("name") or "untitled",
                    (
                        "Deleted; its description, comments and files are on %s."
                        % (kept.get("slug", "?"))
                        if result.get("deleted")
                        # The half-done case says so rather than claiming the tidy
                        # one: the content moved, the original is still sitting
                        # there, and somebody has to go and delete it by hand.
                        else "Copied onto %s, but it could NOT be deleted: %s"
                        % (
                            kept.get("slug", "?"),
                            result.get("delete_error") or "no reason given",
                        )
                    ),
                )
            ),
            # 3 = normal when it worked (news, nothing is waiting on anyone),
            # 4 = high when the original survived, because that one IS a job.
            priority=3 if result.get("deleted") else 4,
            tags=(
                ["twisted_rightwards_arrows"] if result.get("deleted") else ["warning"]
            ),
            click=kept.get("url") or None,
        )
    except Exception as err:  # noqa: BLE001
        _ntfy.log_error("ticket merge push failed: %s", err)


@app.post("/api/tickets/merge")
async def ticket_merge(payload: dict) -> JSONResponse:
    """Merge one ticket into another on the same source and delete the first."""
    payload = payload or {}
    source = str(payload.get("source", "") or "").strip()
    from_id = str(payload.get("from", "") or "").strip()
    into_id = str(payload.get("into", "") or "").strip()
    if not source or not from_id or not into_id:
        return JSONResponse(
            {"error": "source, from and into are required"}, status_code=400
        )
    try:
        result = await _ticket_merge.merge_tickets(source, from_id, into_id)
    except ValueError as err:
        return JSONResponse({"error": str(err)}, status_code=400)
    except LookupError as err:
        return JSONResponse({"error": str(err)}, status_code=404)
    except Exception as err:  # noqa: BLE001 — token / network / provider refusal
        # Nothing was written: merge_tickets only lets the FIRST write raise, and
        # by then both tickets are still exactly as they were.
        return JSONResponse({"error": str(err)}, status_code=502)

    # The panel's cached fan-out still lists the ticket that no longer exists.
    # Dropped rather than refreshed: a re-sweep here would put a provider search
    # per source on this request, and the client refetches immediately anyway.
    _ASSIGNED_TICKETS_CACHE.pop("v", None)
    _notify_ticket_merged(result)
    if log.InfoLog is not None:
        log.InfoLog.Printf(
            "merged ticket %s into %s (deleted: %v)",
            (result.get("from") or {}).get("slug", "?"),
            (result.get("into") or {}).get("slug", "?"),
            result.get("deleted"),
        )
    return JSONResponse(result)


# --- File a ticket from one sentence (New → Ticket) ----------------------
# The one write MindFlock makes INTO a tracker that is not bookkeeping about
# work already happening. Two routes: which sources will accept a ticket, and
# the compose that drafts one and files it.
#
# There is deliberately no route that takes ticket FIELDS. The tracker already
# has a form for filing a ticket by hand, and a worse copy of it living inside
# a session dialog would earn nothing; describing the work is the only thing
# MindFlock can do here that the tracker cannot. See
# :mod:`backend.web.core.ticket_compose`.


@app.get("/api/tickets/sources")
async def ticket_sources() -> JSONResponse:
    """Configured ticketing sources, each with whether it can be filed into.

    Answered per source rather than as a filtered list: a source that cannot
    accept a ticket is usually one field short of being able to (Jira's project
    key, Linear's team), and dropping it from the picker turns a fixable
    misconfiguration into an apparently missing feature. The blocker sentence
    names the field.

    Threaded: resolving the sources reads the layered pipeline config off disk.
    No provider is contacted — ``create_blocker`` is contracted offline
    (``providers/base.py``), so opening the dialog costs no API calls at all.
    """
    try:
        return JSONResponse(await asyncio.to_thread(_ticket_compose.creatable_sources))
    except Exception as err:  # noqa: BLE001 — unconfigured / unreadable config
        return JSONResponse({"error": str(err)}, status_code=502)


@app.post("/api/tickets/compose")
async def ticket_compose(payload: dict) -> JSONResponse:
    """Draft a ticket from one sentence, file it on one source, return the link.

    The one route in this file that creates something on a server MindFlock does
    not own, so the failure reporting is not the usual shape: a 502 here may
    carry a ``draft`` alongside the error, which is the ticket text the model
    already wrote. That is the difference between a failed token costing a
    button press and costing the whole 25-second turn again.

    ``text`` is stripped of contract lines the same way ``/api/session-plan``
    strips them, and by the same code — ``newticket`` is registered in
    ``session_plan._CONTRACT_NAMES``, so a sentence carrying a forged
    ``<newticket>`` block is neutralised by the one stripper both routes share.
    """
    payload = payload or {}
    source = str(payload.get("source", "") or "").strip()
    raw = str(payload.get("text", "") or "")
    if not source:
        return JSONResponse(
            {"error": "pick a source to file the ticket on"}, status_code=400
        )
    text = _session_plan.strip_contract_lines(raw).strip()[: _ticket_draft.MAX_SENTENCE]
    if not raw.strip():
        return JSONResponse({"error": "say what the ticket is for"}, status_code=400)
    if not text:
        return JSONResponse(
            {
                "error": "that reads like an answer format rather than a request — "
                "say what the ticket is for"
            },
            status_code=400,
        )
    try:
        row = await _ticket_compose.compose(
            source,
            text,
            # The flock's own default CLI, read fresh per request and passed in
            # pick_argv's FIRST slot — passing "" there resolves to claude
            # unconditionally, which is how a codex-only machine ends up being
            # told a CLI it never chose is not installed. Same reasoning as
            # /api/session-plan, which is where this pattern is documented.
            program=ENGINE.default_program(),
        )
    except LookupError as err:
        return JSONResponse({"error": str(err)}, status_code=404)
    except _ticket_compose.ComposeError as err:
        body: dict = {"error": str(err)}
        if err.draft is not None:
            body["draft"] = err.draft.as_dict()
        return JSONResponse(body, status_code=502)
    except Exception as err:  # noqa: BLE001 — never a 500 for a convenience
        return JSONResponse({"error": str(err)}, status_code=502)

    # The assigned-tickets panel's cached fan-out predates this ticket by up to
    # its whole TTL, so Intake would not show the thing the user just filed.
    # Dropped rather than refreshed, exactly as the merge route does it: a
    # re-sweep here would put a provider search per source on this request, and
    # the panel refetches on its own.
    _ASSIGNED_TICKETS_CACHE.pop("v", None)
    if log.InfoLog is not None:
        log.InfoLog.Printf(
            "filed ticket %s on %s (%s)",
            row.get("slug") or row.get("id", "?"),
            row.get("source", "?"),
            row.get("url", ""),
        )
    return JSONResponse(row)


# --- Issue force-start (Intake → Issues) ---------------------------------
# The issue twin of the PR force-review endpoints above: list every open issue
# on the issue-handling repos with the reason auto handling has or hasn't
# picked it up, and force-start a session for one, bypassing those filters
# (see backend.web.core.issue_start).

_OPEN_ISSUES_CACHE: dict = {}  # "v" -> (fresh_until_mono, payload)


@app.get("/api/github/issues")
async def github_open_issues(fresh: bool = False) -> JSONResponse:
    """Open issues on the issue-handling repos, annotated with auto-handling
    eligibility. The GitHub fan-out (one list call per repo) is cached and
    served stale-while-revalidate, like the open-PRs panel; ``has_session`` is
    annotated on a per-request copy so it stays live even on cache hits."""
    try:
        data, stale = await _cached_fanout(
            _OPEN_ISSUES_CACHE, _issue_start.list_open_issues, fresh=fresh
        )
    except Exception as err:  # noqa: BLE001 — unconfigured / token / network
        return JSONResponse({"error": str(err)}, status_code=502)
    data = {**data, "stale": stale, "issues": [dict(i) for i in data.get("issues", [])]}
    for i in data.get("issues", []):
        i["has_session"] = i.get("session") in ENGINE.instances or _pending_has(
            i.get("session")
        )
    await _annotate_workspaces(data.get("issues", []), _issue_workspace_args)
    return JSONResponse(data)


@app.post("/api/github/issues/start")
async def github_issue_force_start(payload: dict) -> JSONResponse:
    """Force-start a coding session for one open issue, bypassing auto filters."""
    if not git_available():
        return _no_git_response()
    payload = payload or {}
    repo = str(payload.get("repo", "") or "").strip()
    if not re.match(r"^[^\s/]+/[^\s/]+$", repo):
        return JSONResponse({"error": "repo must be owner/name"}, status_code=400)
    try:
        number = int(payload.get("number"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "number must be an integer"}, status_code=400)
    try:
        agent_override = _start_agent_override(payload)
        depth_override = _start_depth_override(payload)
        effort_override = _start_effort_override(payload)
    except ValueError as err:
        return JSONResponse({"error": str(err)}, status_code=400)

    # Row first, GitHub lookup second (see the PR endpoint above).
    early = _cached_session_title(
        _OPEN_ISSUES_CACHE,
        "issues",
        lambda i: i.get("repo") == repo and i.get("number") == number,
    )
    if early:
        if early in ENGINE.instances or _pending_has(early):
            return JSONResponse(
                {
                    "error": "session %s already exists — close it to re-run" % early,
                    "title": early,
                },
                status_code=409,
            )
        _pending_add(early, "iss", repo=repo)

    try:
        issue = await _issue_start.find_issue(repo, number)
    except LookupError as err:
        _pending_drop(early)
        return JSONResponse({"error": str(err)}, status_code=404)
    except Exception as err:  # noqa: BLE001
        _pending_drop(early)
        return JSONResponse({"error": str(err)}, status_code=502)

    title = _issue_start.session_title(issue)
    if title != early:
        _pending_drop(early)  # stale cache entry — keep only the real title
        if title in ENGINE.instances or _pending_has(title):
            return JSONResponse(
                {
                    "error": "session %s already exists — close it to re-run" % title,
                    "title": title,
                },
                status_code=409,
            )
    _pending_add(
        title,
        "iss",
        branch=_issue_start.branch_for(issue),
        repo=repo,
        workspace_strategy=_issue_start.workspace_mode(),
    )
    _arm_intake_autopilot(
        title,
        depth_override or _repo_intake_depth(repo, "issues"),
        "iss",
        "%s#%s" % (repo, number),
        message=str(getattr(issue, "title", "") or ""),
    )

    async def _bg_start() -> None:
        # Same shape as the pipeline's issue loop (issue → Ticket → engine
        # session on a fresh branch), but against THIS server's engine so the
        # session shows up in the grid without a reload. The engine owns
        # provisioning (inside Instance.Start), like the ticket path.
        try:
            story, prompt, branch = await _issue_start.prepare_start(issue)
            # Same leftover-worktree reclaim as the ticket path above.
            await asyncio.to_thread(
                _worktree_reclaim.reclaim_for_launch,
                getattr(story, "repo_url", "") or "",
                branch,
            )
            # This start's own pick outranks the repo card's Agent CLI, which
            # prepare_start already resolved onto the story. Resolved before the
            # options because this start's effort has to be translated into THAT
            # CLI's spelling.
            program = (
                agent_override
                or getattr(story, "agent", "")
                or ENGINE.default_program()
            )
            prompt = _provider_effort.decorate_prompt(prompt, program, effort_override)
            prompt = await asyncio.to_thread(
                lambda p=prompt: _red_zone_prompt(
                    p,
                    program,
                    _repo_url_workdirs(getattr(story, "repo_url", "") or ""),
                    None,
                )
            )
            inst = session.NewInstance(
                session.InstanceOptions(
                    title=title,
                    path=".",
                    program=program,
                    provisioned=True,
                    workspace_strategy=_issue_start.workspace_mode(),
                    new_branch=branch,
                    prompt=prompt,
                    provision_repo_url=getattr(story, "repo_url", "") or "",
                    launch_args=_start_launch_args(program, effort_override),
                )
            )
            inst.ExtraEnv = _ports.env_for(title)
            inst.SetStatus(Loading)
            with ENGINE.lock:
                ENGINE.instances[title] = inst
            _seed_event_snapshot(title)
            _events.BUS.emit(
                "session.created",
                session=title,
                new="loading",
                data={"program": inst.Program, "provisioned": True, "issue": number},
            )
            try:
                await asyncio.to_thread(inst.Start, True)
                ENGINE.save()
            except Exception:
                # By identity: this task may be the loser of a re-start, and
                # popping by name would delete the LIVE session's record.
                _drop_failed_start(title, inst)
                raise
            # Ledger the issue so auto handling doesn't run it a second time.
            _issue_start.record_handled(issue)
            if log.InfoLog is not None:
                log.InfoLog.Printf("forced issue session %s live", title)
        except Exception as err:  # noqa: BLE001
            if log.ErrorLog is not None:
                log.ErrorLog.Printf("forced issue session %s failed: %v", title, err)
            _events.BUS.emit(
                "session.create_failed", session=title, data={"error": str(err)}
            )
        finally:
            _pending_drop(title)

    _register_task(_bg_start())
    return JSONResponse({"started": True, "title": title}, status_code=202)


# --- Intake reopen (all three panels) -------------------------------------
# The other half of "Begin work": an item that has been worked once usually
# still HAS its workspace on this machine (ending a session keeps the worktree;
# a restart loses the session but not the directory). Starting over would
# collide with that worktree or duplicate it, so every row whose work is still
# on disk carries a `workspace` annotation (see backend.web.core.reopen) and
# this endpoint puts a window back on it.
#
# The client sends only the item's identity — the same fields it posts to the
# matching /start route — and the workspace is re-resolved HERE, live. Taking a
# path from the payload would let a stale panel (or anything else) name a
# directory the server never offered.

#: The intake kinds a reopen can be asked for. Each names the cached listing its
#: rows come from and the resolver that reads a row — the SAME resolver the
#: listing annotated with, so the button and the action can't disagree.
_INTAKE_KINDS = ("tickets", "prs", "issues")


def _intake_row(kind: str, payload: dict):
    """``(row, workspace_args)`` for one intake item, read from the cached
    listing its panel is showing. ``row`` is ``None`` when the panel's list no
    longer holds the item (a stale tab, or a server restarted since). Raises
    ``ValueError`` for a payload that doesn't identify an item at all."""
    if kind == "tickets":
        source = str(payload.get("source", "") or "").strip()
        ticket_id = str(payload.get("id", "") or "").strip()
        if not source or not ticket_id:
            raise ValueError("source and id are required")
        row = _pending.cached_row(
            _ASSIGNED_TICKETS_CACHE,
            "tickets",
            lambda t: t.get("source") == source and str(t.get("id")) == ticket_id,
        )
        return row, _ticket_workspace_args
    repo = str(payload.get("repo", "") or "").strip()
    if not re.match(r"^[^\s/]+/[^\s/]+$", repo):
        raise ValueError("repo must be owner/name")
    try:
        number = int(payload.get("number"))
    except (TypeError, ValueError):
        raise ValueError("number must be an integer") from None

    def _match(row: dict) -> bool:
        return row.get("repo") == repo and row.get("number") == number

    if kind == "prs":
        return _pending.cached_row(_OPEN_PRS_CACHE, "prs", _match), _pr_workspace_args
    return (
        _pending.cached_row(_OPEN_ISSUES_CACHE, "issues", _match),
        _issue_workspace_args,
    )


@app.post("/api/intake/reopen")
async def intake_reopen(payload: dict) -> JSONResponse:
    """Put a session back on the workspace an earlier run of an intake item
    left on this machine, instead of starting the item over.

    A closed session is restored from its recently-closed entry (branch,
    program, prompt and provisioning flags intact). A workspace with no such
    entry — the run whose session a restart lost — gets a fresh in-place
    session on the directory: in-place because the directory is not ours to
    delete, so ending this session must leave the work exactly where it is.
    """
    payload = payload or {}
    kind = str(payload.get("kind", "") or "").strip()
    if kind not in _INTAKE_KINDS:
        return JSONResponse(
            {"error": "kind must be one of: " + ", ".join(sorted(_INTAKE_KINDS))},
            status_code=400,
        )
    try:
        row, resolve = _intake_row(kind, payload)
    except ValueError as err:
        return JSONResponse({"error": str(err)}, status_code=400)
    if row is None:
        return JSONResponse(
            {"error": "that item is not in the panel's current list — Refresh it"},
            status_code=409,
        )

    title = str(row.get("session") or "")
    if title in ENGINE.instances or _pending_has(title):
        return JSONResponse(
            {"error": "session %s is already open" % title, "title": title},
            status_code=409,
        )

    found = await asyncio.to_thread(lambda: _reopen.find_workspace(**resolve(row)))
    if found is None:
        return JSONResponse(
            {
                "error": "no workspace for this item is left on this machine — "
                "use Start to start it fresh"
            },
            status_code=410,
        )
    if found.get("kind") == "closed" and found.get("entry_id"):
        return await _reopen_closed_entry(str(found["entry_id"]))

    folder = str(found.get("path") or "")
    open_title = _unique_title(title or os.path.basename(folder) or "session")
    program = ENGINE.default_program()
    inst = session.NewInstance(
        session.InstanceOptions(
            title=open_title,
            path=folder,
            program=program,
            in_place=True,
        )
    )
    inst.ExtraEnv = _ports.env_for(open_title)
    inst.SetStatus(Loading)
    with ENGINE.lock:
        ENGINE.instances[open_title] = inst

    async def _bg_open() -> None:
        try:
            await asyncio.to_thread(inst.Start, True)
            ENGINE.save()
        except Exception as err:  # noqa: BLE001
            if log.ErrorLog is not None:
                log.ErrorLog.Printf("failed to reopen %s: %v", open_title, err)
            if not _drop_failed_start(open_title, inst):
                return
            _events.BUS.emit(
                "session.create_failed", session=open_title, data={"error": str(err)}
            )

    _register_task(_bg_open())
    _seed_event_snapshot(open_title)
    _events.BUS.emit(
        "session.created",
        session=open_title,
        new="loading",
        data={"program": program, "reopened": folder},
    )
    return JSONResponse(_instance_json(inst), status_code=202)


# --- Verify (test plans) ------------------------------------------------------
# The read/act surface over backend.web.core.test_plans, driving the Verify
# dialog. The plans themselves are written by the push triggers and matured by
# _test_plans_due_loop, both further up; nothing here generates on the request
# path — the one-shot takes up to three minutes.
#
# ``{plan_id:path}`` rather than the usual ``{plan_id}``: a plan is keyed by its
# session title, and a session title can be a whole branch path
# (``feature/sc-412/queue-badges`` — create_instance accepts exactly that shape).
# With the default converter those plans would 404 at the router before any
# handler ran, which is the kind of bug that only shows up for the users with the
# tidiest branch names. The converter is greedy, but every route below is pinned
# by a literal suffix (or by its method), so there is nothing for it to swallow.


@app.get("/api/test-plans")
def list_test_plans() -> JSONResponse:
    """Every test plan, newest first, plus the branch that counts as live.

    ``live_branch`` is resolved fresh per request and returned alongside rather
    than read off the plans: a plan carries the branch it was created against,
    and the dialog needs to be able to say "waiting to reach main" for a plan
    that has not been near a settings change.

    Each plan also carries ``effective_live_branch`` — the same question asked
    FOR THAT PLAN'S REPO — and the two are not interchangeable. The top-level one
    is the flock-wide default, which is the right thing for a header and for a
    card's placeholder, and the wrong thing to compare a plan against: plans are
    stamped with the PER-REPO answer at creation
    (``resolve_live_branch(repo_root)``), so a repo that overrides its live
    branch would have every one of its plans read as "written against a branch
    that has since moved" when nothing has moved at all. Resolved here rather than
    in ``test_plans.list_plans`` because it is a derived, settings-dependent view
    of a plan and not part of what the store persists; memoized per repo because
    the chain is one settings read and a list is routinely a hundred plans over a
    handful of repos.
    """
    plans = _test_plans.list_plans()
    resolved: Dict[str, str] = {}
    for plan in plans:
        root = str(plan.get("repo_root") or "")
        if root not in resolved:
            resolved[root] = _test_plans.resolve_live_branch(root)
        _test_plan_row(plan, resolved[root])
    return JSONResponse(
        {
            "plans": plans,
            "live_branch": _test_plans.resolve_live_branch(),
        }
    )


def _known_repo_roots() -> list[dict]:
    """``[{name, root}]`` — the local repos Verify can write a checklist in by
    branch: every checklist's repo, every open session's, every recently closed
    one's. One entry per repo (``norm_repo``), only checkouts still on disk.
    Blocking (a session's repo path may ask git)."""
    roots: list[str] = [str(p.get("repo_root") or "") for p in _test_plans.list_plans()]
    for inst in list(ENGINE.instances.values()):
        try:
            roots.append(inst.GetGitWorktree().GetRepoPath() or "")
        except Exception:  # noqa: BLE001 — not a git session, or not started
            continue
    for entry in _load_recently_closed():
        data = entry.get("data") or {}
        wt = data.get("worktree") or {}
        roots.append(str(wt.get("repo_path") or ""))
    out: list[dict] = []
    seen: set = set()
    for raw in roots:
        root = _test_plans.norm_repo(raw)
        if not root or root in seen:
            continue
        seen.add(root)
        if os.path.isdir(root) and os.path.exists(os.path.join(root, ".git")):
            out.append({"name": os.path.basename(root.rstrip(os.sep)), "root": root})
    out.sort(key=lambda r: (r["name"].lower(), r["root"]))
    return out


@app.get("/api/test-plans/repos")
async def list_test_plan_repos() -> JSONResponse:
    """The repos Verify → New checklist → *Another branch…* can pick from."""
    return JSONResponse({"repos": await asyncio.to_thread(_known_repo_roots)})


@app.post("/api/test-plans/branch")
async def write_test_plan_for_branch(payload: dict) -> JSONResponse:
    """Ask the agent for a checklist for a BRANCH no session has been on.

    ``{repo, branch}``: ``repo`` is one of :func:`_known_repo_roots` (its root,
    or its name when that is unambiguous). The same answers in the same shapes
    as ``POST /api/instances/<title>/test-plan`` — 200 ``existing: true`` naming
    the checklist that already covers the branch, else 202 while it is written —
    so Verify's one button does not care which it asked. Written diff-only, from
    the repo's own checkout (there is no session, so no ticket text): the
    rewrite box is where to aim it. 400 a missing/unknown repo or a bad branch
    name, 409 a branch the repo doesn't have or the branch it ships from.

    ``empty: true`` creates the checklist EMPTY instead (``origin: "manual"``,
    201) and starts no generation: a person fills it with Add a step."""
    if not git_available():
        return _no_git_response()
    payload = payload or {}
    repo = payload.get("repo")
    branch = payload.get("branch")
    if not isinstance(repo, str) or not repo.strip():
        return JSONResponse({"error": "pick a repo"}, status_code=400)
    if not isinstance(branch, str) or not branch.strip():
        return JSONResponse({"error": "type a branch name"}, status_code=400)
    repo, branch = repo.strip(), branch.strip()
    empty = payload.get("empty") is True

    def _prepare():
        known = _known_repo_roots()
        want = _test_plans.norm_repo(repo)
        hits = [r for r in known if r["root"] == want] or [
            r for r in known if r["name"].lower() == repo.lower()
        ]
        if len(hits) != 1:
            return (
                None,
                "",
                JSONResponse(
                    {"error": "%s isn't one of your repos here" % repo}, status_code=400
                ),
            )
        root = hits[0]["root"]
        if not _test_plans.valid_branch_name(branch):
            return (
                None,
                "",
                JSONResponse(
                    {"error": "%r isn't a branch name" % branch}, status_code=400
                ),
            )
        live = _test_plans.resolve_live_branch(root)
        if branch == live:
            return (
                None,
                "",
                JSONResponse(
                    {
                        "error": "%s is the branch this repo ships from — nothing to compare"
                        % branch
                    },
                    status_code=409,
                ),
            )
        sha = _test_plans.resolve_branch_sha(root, branch)
        if not sha:
            return (
                None,
                "",
                JSONResponse(
                    {
                        "error": "no branch %s in %s (here or on origin)"
                        % (branch, hits[0]["name"])
                    },
                    status_code=409,
                ),
            )
        if not empty and _test_plans.branch_has_no_commits(root, live, sha):
            return None, "", _no_commits_response()
        plan_id = _test_plans.branch_plan_id(root, branch)
        plan = _test_plans.ensure_plan_for(
            plan_id, branch, sha, root, live, intent="", manual=empty
        )
        owner = (
            ""
            if plan is not None
            else _test_plans.owner_for_branch(plan_id, root, branch)
        )
        return plan, owner or plan_id, None

    plan, plan_id, resp = await asyncio.to_thread(_prepare)
    if resp is not None:
        return resp
    if plan is None:
        existing = _test_plans.get(plan_id) or {}
        return JSONResponse(
            {
                "ok": True,
                "plan": plan_id,
                "existing": True,
                "state": existing.get("state", ""),
            }
        )
    if empty:
        # Empty by choice: nothing to write, so nothing is started.
        return JSONResponse(
            {"ok": True, "plan": plan_id, "existing": False, "empty": True},
            status_code=201,
        )
    # No session: the repo's default CLI, reading the diff in its checkout.
    _start_test_plan_generation(plan_id, "", "")
    return JSONResponse(
        {"ok": True, "plan": plan_id, "existing": False}, status_code=202
    )


# The repo list Verify tracks has NO routes of its own, and that absence is the
# design. It is ``repository.verify_repos`` + ``repository.verify_repo_settings``
# — ordinary settings, typed as ``owner/name`` into the same card list Intake
# uses for PR review and issues, and saved through ``POST /api/settings`` like
# every other setting. What used to be here (a GET that DISCOVERED repos from
# open sessions and a POST that patched a block keyed by absolute path) is gone
# along with the discovery: a repo Verify watches is one a person named, not one
# that happened to have a session open, and the name is the GitHub slug because
# that is the only spelling that is the same in every clone and every worktree.


def _no_commits_response() -> JSONResponse:
    """409 for "Ask the agent" on a branch with nothing on it yet. ``detail``
    is the sentence the card shows; ``reason`` lets the picker say it too."""
    return JSONResponse(
        {
            "error": "nothing committed on this branch yet",
            "detail": _test_plans.NO_COMMITS_DETAIL + ".",
            "reason": "no_commits",
        },
        status_code=409,
    )


def _empty_plan_for_closed(title: str) -> JSONResponse:
    """``empty: true`` for a CLOSED session: the checklist is made empty
    (``origin: "manual"``) from what the closed entry remembers, and nothing is
    generated — the closed-session writer would start a model call."""
    branch, sha, repo_root, _program = _closed_session_plan_inputs(title)
    if not repo_root:
        return JSONResponse({"error": "no such session: %s" % title}, status_code=404)
    if not branch or not sha:
        return JSONResponse(
            {
                "error": "that session's branch is gone",
                "detail": "That session's branch is gone, so there is nothing to attach a checklist to.",
            },
            status_code=409,
        )
    plan = _test_plans.ensure_plan_for(
        title,
        branch,
        sha,
        repo_root,
        _test_plans.resolve_live_branch(repo_root),
        intent="",
        manual=True,
    )
    if plan is None:
        owner = _test_plans.owner_for_branch(title, repo_root, branch) or title
        existing = _test_plans.get(owner) or {}
        return JSONResponse(
            {
                "ok": True,
                "plan": owner,
                "existing": True,
                "state": existing.get("state", ""),
            }
        )
    return JSONResponse(
        {"ok": True, "plan": title, "existing": False, "empty": True}, status_code=201
    )


@app.post("/api/instances/{title}/test-plan")
async def instance_write_test_plan(
    title: str, payload: Optional[dict] = None
) -> JSONResponse:
    """Write a test plan for this session, because a person asked for one.

    The normal way to get a plan. Automatic generation is opt-in per repo (on
    the Verify list, or ``[workspace] verify_on_push`` in ``.mindflock.toml`` —
    see :func:`_verify_auto_for`) and off everywhere
    else, so without this button the feature would only exist for repos that had
    already been configured for it — and you cannot configure a repo for
    something you have never seen work.

    Unlike the push trigger this answers synchronously about everything it can
    know quickly (is there a session, does it have a workspace, is there already
    a plan) and only then hands the model call to a thread, because a button
    needs to say what happened. An existing plan for the branch is reported as
    ``existing: true`` with a 200 rather than an error: the honest answer to
    "write a plan for this" when one is already written is to point at it, and
    the dialog opens it. Regenerating is a different button and says so.

    ``{"empty": true}`` makes the checklist EMPTY instead (``origin:
    "manual"``, 201) and starts no generation — "Start an empty checklist".
    Without it, a branch with no commits (and a clean tree) is refused with
    409 ``reason: "no_commits"``: there is nothing for the agent to read.
    """
    if not git_available():
        return _no_git_response()
    empty = isinstance(payload, dict) and payload.get("empty") is True
    inst, err = _inst_or_404(title)
    if err is not None:
        if empty:
            return await asyncio.to_thread(_empty_plan_for_closed, title)
        # ...unless the session was merely CLOSED. A checklist is worth asking
        # for precisely when the work is finished and the window has been put
        # away, so refusing there made the button useless at the only moment
        # people reach for it. See `_closed_session_plan_inputs`.
        return await _write_test_plan_for_closed(title)
    peer_err = _peer_guard.refusal(inst, "test plans")
    if peer_err is not None:
        return peer_err
    wt = inst.GetWorktreePath()
    if not wt:
        return JSONResponse({"error": "workspace not ready"}, status_code=409)

    def _prepare():
        try:
            repo_root = inst.GetGitWorktree().GetRepoPath() or ""
        except Exception:  # noqa: BLE001 — raises before Start has finished
            repo_root = ""
        branch = _current_branch(wt)
        sha = _git_head_sha(wt)
        if not branch:
            # A detached HEAD. ``ensure_plan_for`` refuses a branchless plan (it
            # has nothing to watch for going live), and it says so by returning
            # None — which is the SAME answer it gives for "there is already a
            # plan for this branch". Without this the route reports the cheerful
            # one: "already has a checklist — it is in the list below", pointing
            # at a checklist that does not exist and never will.
            return (
                None,
                "",
                JSONResponse(
                    {"error": "this session isn't on a branch"}, status_code=409
                ),
            )
        if not sha:
            # An unborn HEAD: nothing has been committed, so there is no change
            # to describe and a plan would be a list of steps about nothing.
            return (
                None,
                "",
                JSONResponse(
                    {"error": "nothing committed on this branch yet"}, status_code=409
                ),
            )
        # One expression, used twice on purpose: the plan's repo and the repo the
        # live branch is resolved FOR must be the same one, or a repo whose
        # override says "staging" gets a plan that waits for "main" forever.
        root = repo_root or wt
        live = _test_plans.resolve_live_branch(root)
        # A session ON the live branch (this folder directly) is left alone:
        # the writer reads its newest commit instead of a range.
        if not empty and branch != live and _test_plans.branch_has_no_commits(wt, live):
            return None, "", _no_commits_response()
        plan = _test_plans.ensure_plan_for(
            title,
            branch,
            sha,
            root,
            live,
            intent="" if empty else _test_plan_intent(title),
            manual=empty,
        )
        # Which plan the "there is already one" answer is ABOUT. Usually this
        # session's own, but a branch open in two windows has one checklist
        # between them and it is keyed by whichever window got there first —
        # so the id is looked up rather than assumed, or the dialog would point
        # at a plan that does not exist.
        owner = (
            ""
            if plan is not None
            else _test_plans.owner_for_branch(title, root, branch)
        )
        return plan, owner, None

    plan, owner, resp = await asyncio.to_thread(_prepare)
    if resp is not None:
        return resp
    if plan is None:
        existing = _test_plans.get(owner) or {}
        return JSONResponse(
            {
                "ok": True,
                "plan": owner or title,
                "existing": True,
                "state": existing.get("state", ""),
            }
        )
    if empty:
        return JSONResponse(
            {"ok": True, "plan": title, "existing": False, "empty": True},
            status_code=201,
        )
    _start_test_plan_generation(title, getattr(inst, "Program", "") or "", wt)
    return JSONResponse({"ok": True, "plan": title, "existing": False}, status_code=202)


@app.post("/api/test-plans/{plan_id:path}/run")
async def run_test_plan(
    plan_id: str, request: Request, payload: Optional[dict] = None
) -> JSONResponse:
    """Start a real session that works through this plan's agent steps.

    Deliberately a full session and not a headless one-shot (the asymmetry the
    core module's docstring is built around): checking a feature means checking
    out the live branch, pulling, starting things and reading logs — minutes of
    real work the user must be able to watch, interrupt and take over. So it goes
    through :func:`create_instance` like anything else, which also means its
    failures (a repo that has moved, a title collision, a folder that is no
    longer a git repo) are reported in that endpoint's own words instead of a
    second, subtly different set.

    An optional ``{"steps": ["s3"]}`` narrows the run to those steps — the
    per-step Run button, for re-checking one thing after a fix without paying
    for the whole plan again. **A human step can never be named there.** It is
    refused with a 400 rather than quietly dropped, because the request is a
    category error and silently running nothing would look like it worked: a
    step is ``human`` precisely because no shell can observe what it asks about.
    """
    payload = payload or {}
    if not git_available():
        return _no_git_response()
    plan = _test_plans.get(plan_id)
    if plan is None:
        return JSONResponse(
            {"error": "test plan not found: %s" % plan_id}, status_code=404
        )
    # A RECORD IS NOT A SESSION, here as much as below: a run whose workspace has
    # gone (cleared by hand, a disk that went away) keeps its record and its
    # agent window, so `_verify_window_gone` never fires and the two-hour clock
    # was the only way out — while every press of Run answered "already
    # verifying this plan" and never reached the husk-closing path below.
    if plan["state"] == "running" and await asyncio.to_thread(
        _verify_session_usable, plan["run_session"]
    ):
        return JSONResponse(
            {
                "error": "%s is already verifying this plan" % plan["run_session"],
                "session": plan["run_session"],
            },
            status_code=409,
        )
    if not plan["steps"]:
        # A plan still generating, or one that failed to: there is nothing for a
        # session to do, and starting one would burn a workspace to print an
        # empty checklist. 409 (not 400) — the request is fine, the plan is not
        # ready, and the regenerate button is the fix.
        return JSONResponse(
            {"error": "this plan has no steps yet — regenerate it first"},
            status_code=409,
        )
    if not any(s["actor"] != "human" for s in plan["steps"]):
        # Every step is a person's own. An agent is FORBIDDEN from settling one
        # (see ``finish_run``, which coerces any answer it gives to a human step
        # back to "blocked"), so this would provision a workspace and minutes of
        # a billed session for a run that is not allowed to answer anything and
        # hands the whole checklist straight back. Not an exotic case either:
        # ``parse_plan`` defaults an unrecognised actor to "human", so a model
        # that omits the key produces exactly this plan. 409 for the same reason
        # as above — the request is fine, the plan is not one an agent can work.
        return JSONResponse(
            {
                "error": "every step in this checklist is for a person to check — "
                "there is nothing here an agent can settle"
            },
            status_code=409,
        )
    # THE REPO HAS TO STILL BE THERE. `create_instance` CREATES a missing
    # repo_path and falls back to a git-less in-place session, so a plan whose
    # repo has moved or been deleted did not fail — it made an empty folder,
    # started a real (billed) agent in it, told it to check out `origin/<live>`,
    # and stamped the plan `running` for the two hours until the give-up clock
    # noticed nothing had been written. The route's own docstring promised this
    # was reported "in that endpoint's own words"; now it is.
    repo_root = plan.get("repo_root") or ""
    if not await asyncio.to_thread(_is_verify_repo_usable, repo_root):
        return JSONResponse(
            {
                "error": "the repo this checklist was written from is gone (%s) — "
                "a run needs it to check out the live branch"
                % (repo_root or "no path recorded")
            },
            status_code=409,
        )
    only = payload.get("steps")
    if only is not None:
        if not isinstance(only, list) or not only:
            return JSONResponse(
                {"error": "steps must be a non-empty list of step ids"},
                status_code=400,
            )
        by_id = {s["id"]: s for s in plan["steps"]}
        unknown = [str(s) for s in only if str(s) not in by_id]
        if unknown:
            return JSONResponse(
                {"error": "no such step: %s" % ", ".join(unknown)}, status_code=400
            )
        human = [str(s) for s in only if by_id[str(s)]["actor"] == "human"]
        if human:
            return JSONResponse(
                {
                    "error": "%s %s for a person to check, not an agent"
                    % (
                        ", ".join(human),
                        "is" if len(human) == 1 else "are",
                    ),
                    "human_steps": human,
                },
                status_code=400,
            )
        only = [str(s) for s in only]
    # The session is named for the COMMIT, not just the plan, so every new
    # branch gets a fresh one.
    #
    # A plan is keyed by session title and is REPLACED when that session moves
    # to a different branch (test_plans.ensure_plan_for), but the run session's
    # name was derived from the plan id alone — so the next branch's run reused
    # the previous branch's workspace. Reuse is deliberate and right for
    # re-checking one step of the SAME commit (below), and exactly wrong across
    # commits: that session is checked out at the old sha, its result file holds
    # the old answers, and it is sitting in a worktree cut for work that has
    # already shipped. Putting the sha in the name makes "same commit" and
    # "different commit" different sessions, which is the distinction that
    # actually matters, and it does it without any bookkeeping.
    sha7 = str(plan.get("sha") or "")[:7]
    title = "verify-%s%s" % (plan_id, ("-" + sha7) if sha7 else "")
    # Has this change actually shipped? Not a gate — you may run a plan whenever
    # you like, and wanting to check your own work before it ships is a normal
    # thing to want. It decides WHICH TREE the session checks out; see
    # build_run_prompt. "done" counts as live: it got there and was verified.
    # The repo's standing instructions and its deployed target reach the RUNNER,
    # not only the writer. The settings field's own placeholder is "The UI runs
    # on :3000 — check there, not :8080" — a sentence written for the agent that
    # works the checklist, which until now only the model that wrote it ever saw.
    prompt = _test_plans.build_run_prompt(
        plan,
        only,
        live=plan["state"] in ("due", "running", "done"),
        repo_notes=_test_plans.repo_notes(repo_root),
        target=_test_plans.verify_target(repo_root),
    )
    # Clear the previous run's answers BEFORE starting another one. The result
    # file is the session's only return channel and the poller believes the
    # first `finished: true` it sees: left in place, a re-run (or a one-step
    # re-check) would be finished by its predecessor's file within 60s — before
    # the agent had so much as checked out the branch — and the plan would take
    # the old verdict as the new one. Silent, and wrong in the direction that
    # matters, since the usual reason to re-run is that something failed.
    await asyncio.to_thread(_clear_verify_results, title)
    # A RECORD IS NOT A USABLE SESSION. The reuse path below tests only for a
    # record, and a verify session whose workspace has since been cleared keeps
    # one — so `instance_send` answered 409 "workspace no longer exists" and the
    # route returned before `start_run`, forever, for that checklist. There is
    # no control anywhere that ends that session either (the rail hides
    # `verify-*`, and the dialog's own End is gated on `plan.run_session`), so
    # it was a permanent dead end reachable by pressing Run twice. Closing the
    # husk drops it back into the reclaim-and-create path below.
    if title in ENGINE.instances and not await asyncio.to_thread(
        _verify_session_usable, title
    ):
        await _end_verify_session(title)
    if title not in ENGINE.instances:
        # Clear this run's own leftovers before creating anything. Both of these
        # prevent a failure that happens on a background task, minutes after
        # this route has answered 202 — which is why they run here and not in an
        # error handler. tmux first: the orphan's shell is sitting IN the
        # worktree the next call reclaims.
        killed = await asyncio.to_thread(_kill_orphan_plan_tmux, title)
        if killed and log.ErrorLog is not None:
            log.ErrorLog.Printf("verify: killed orphan tmux session %s", killed)
        # Free a branch a dead run of this same checklist is still holding.
        freed = await asyncio.to_thread(
            _free_stale_verify_worktree, plan.get("repo_root") or "", title
        )
        if freed and log.ErrorLog is not None:
            log.ErrorLog.Printf("verify: reclaimed stale worktree %s", freed)
    if title in ENGINE.instances:
        # A verify session for this plan is already open — the normal state of
        # things once you have run the plan and are now re-checking one step.
        # Send it the new instructions instead of calling create_instance, which
        # would 409 on the duplicate title and leave "Run step" permanently
        # broken for exactly the case it exists for. Reuse is also the better
        # behaviour on its own terms: the workspace is already provisioned and
        # already sitting on the live branch.
        resp = await instance_send(title, {"text": prompt})
        if resp.status_code >= 400:
            return resp
        # A run that predates the flag (hidden by its name alone until now)
        # is flagged the first time it is reused. Idempotent.
        await asyncio.to_thread(_hidden_sessions.set_hidden_by, title, "verify")
    else:
        resp = await create_instance(
            {
                "title": title,
                # The MAIN repo, which is why the plan stores it: the worktree
                # this branch was written in is normally reclaimed by the time a
                # plan comes due.
                "repo_path": plan["repo_root"],
                "prompt": prompt,
                # What keeps it off the rail (the row's `hidden_by`). The
                # title prefix is NOT the signal: a user may name a session
                # `verify-anything` and it must stay listed.
                "hidden_by": "verify",
            },
            request,
        )
        if resp.status_code >= 400:
            return resp
    # Only now: start_run stamps the plan "running" and opens the run record the
    # due loop's give-up clock reads, and a plan claiming a session that was
    # never created would be stuck there for two hours.
    started = await asyncio.to_thread(_test_plans.start_run, plan_id, title)
    if started is None:
        # The plan was deleted while the session was being provisioned. Answering
        # `ok` here left a real, billed agent working a checklist that no longer
        # exists — and one nothing would ever collect, since the sweep's grace
        # window measures from the session and the poller only looks at plans.
        await _end_verify_session(title)
        return JSONResponse(
            {"error": "test plan not found: %s" % plan_id}, status_code=404
        )
    # The plan comes back with it. The row needs the run record `start_run` just
    # opened (the "last checked" line, the give-up clock, the carried human
    # answers), and the client was hand-synthesising a state without it.
    #
    # SHAPED THE WAY THE LIST ROUTE SHAPES IT, because the client REPLACES its
    # whole row with this object: a raw store record has no
    # `effective_live_branch`, so a repo that ships from `staging` would have
    # its row fall back to the flock-wide branch and start claiming the plan was
    # measured against the wrong one — and it would carry the conversation blob
    # the list route drops on purpose.
    return JSONResponse(
        {"ok": True, "session": title, "plan": _test_plan_row(started)},
        status_code=202,
    )


def _fix_refusal(status: int, error: str, detail: str, **extra) -> JSONResponse:
    """A Fix… refusal: the legacy ``error`` plus ``detail``, the one plain
    sentence the dialog shows as-is (no API path, no traceback, no folder)."""
    body = {"error": error, "detail": detail}
    body.update(extra)
    return JSONResponse(body, status_code=status)


_FIX_GENERIC_DETAIL = (
    "Couldn't start a fix session — MindFlock hit an error (details in the log)."
)


def _fix_detail_for(resp: JSONResponse) -> JSONResponse:
    """Give a downstream failure (create / send) the route's ``detail``.

    Those routes answer in their own ``error`` words, which may name a folder or
    a git line; the sentence is passed through only when it carries neither."""
    try:
        body = json.loads(bytes(resp.body).decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        body = {}
    if not isinstance(body, dict):
        body = {}
    if body.get("detail"):
        return resp
    err = str(body.get("error") or "").strip().splitlines()
    first = err[0].rstrip(".") if err else ""
    if first and "/" not in first and "Traceback" not in first and len(first) < 200:
        body["detail"] = "Couldn't start a fix session — %s." % first
    else:
        body["detail"] = _FIX_GENERIC_DETAIL
    body.setdefault("error", first or "fix session failed")
    return JSONResponse(body, status_code=resp.status_code)


@app.post("/api/test-plans/{plan_id:path}/fix")
async def fix_failed_test_plan_steps(
    plan_id: str, request: Request, payload: Optional[dict] = None
) -> JSONResponse:
    """Open a session to fix what a check found → **202** ``{session}``.

    Every refusal carries ``detail``: one plain sentence the dialog shows
    verbatim (round 5 — the toast used to print the raw API path of a 500).
    An unexpected failure is a 500 with the same shape, never a traceback.
    """
    try:
        resp = await _fix_failed_test_plan_steps(plan_id, request, payload)
    except Exception:  # noqa: BLE001 — answered as a sentence, logged in full
        if log.ErrorLog is not None:
            log.ErrorLog.Printf("fix %s: %s", plan_id, traceback.format_exc())
        return _fix_refusal(500, "fix session failed", _FIX_GENERIC_DETAIL)
    if resp.status_code >= 400:
        return _fix_detail_for(resp)
    return resp


async def _fix_failed_test_plan_steps(
    plan_id: str, request: Request, payload: Optional[dict]
) -> JSONResponse:
    """Open a session to fix what a check found → **202** ``{session}``.

    THE LOOP THIS CLOSES. A red checklist is the most valuable thing this feature
    produces — the work shipped, it does not do what it was for, and somebody
    observed exactly how — and until this route it was a dead end: the row's
    button opened the evidence and there was nothing to press next.

    An ORDINARY session, not the verify one. A verify run's entire posture is
    "report, never fix" (the single-file output contract, the git-excluded
    result file, the rule that it may write nothing else); reusing it to make a
    change would dismantle the property that makes its report readable as
    evidence in the first place. So this creates a normal session in the plan's
    repo, named for the plan, and hands it the failures.

    ``{"steps": ["s3"]}`` narrows it; the default is every step whose newest
    answer is ``fail``. 409 when nothing failed — a button that opens an empty
    session is worse than no button.
    """
    plan = _test_plans.get(plan_id)
    if plan is None:
        return _fix_refusal(
            404,
            "test plan not found: %s" % plan_id,
            "That checklist no longer exists.",
        )
    if not git_available():
        return _fix_refusal(
            409,
            "git is not available",
            "Git isn't installed, so MindFlock can't open a fix session.",
        )
    failures = _test_plans.failed_steps(plan)
    only = (payload or {}).get("steps")
    if only is not None:
        # VALIDATED LIKE /run'S, because the two silent answers here were both
        # wrong in the direction that wastes a session. A non-list (``"s3"``)
        # failed the isinstance test and silently widened the request to every
        # failure on the checklist; an id that does not exist, or one that
        # exists and did not fail, answered "nothing on this checklist failed"
        # on a checklist with three red steps — which reads as the feature being
        # broken rather than the request being wrong.
        if not isinstance(only, list) or not only:
            return _fix_refusal(
                400,
                "steps must be a non-empty list of step ids",
                "Pick at least one failed step to fix.",
            )
        wanted = [str(s) for s in only]
        by_id = {s["id"] for s in plan["steps"]}
        unknown = [s for s in wanted if s not in by_id]
        if unknown:
            return _fix_refusal(
                400,
                "no such step: %s" % ", ".join(unknown),
                "That step isn't on this checklist any more — refresh and try again.",
                unknown_steps=unknown,
            )
        chosen = {f["step"]["id"] for f in failures if f["step"]["id"] in wanted}
        missing = [s for s in wanted if s not in chosen]
        if missing:
            return _fix_refusal(
                409,
                "%s didn't fail — there is nothing to fix there" % ", ".join(missing),
                "That step didn't fail, so there is nothing to fix there.",
            )
        failures = [f for f in failures if f["step"]["id"] in chosen]
    if not failures:
        return _fix_refusal(
            409,
            "nothing on this checklist failed",
            "Nothing on this checklist failed, so there is nothing to fix.",
        )
    if not plan.get("repo_root"):
        return _fix_refusal(
            409,
            "this checklist has no repository to work in",
            "This checklist isn't tied to a repo, so a fix session has nowhere to work.",
        )
    title = "fix-%s" % plan_id
    prompt = _test_plans.build_fix_prompt(plan, failures)
    reclaimed = ""
    if title in ENGINE.instances and await asyncio.to_thread(
        _verify_session_usable, title
    ):
        # Already open — the normal state once you have pressed this and are
        # coming back with a second failure. Sending beats 409ing on the title.
        # "Open" means a workspace, not merely a record: a fix session whose
        # worktree has gone answers 409 "workspace no longer exists" for ever
        # otherwise, exactly as the run route's reuse path did.
        resp = await instance_send(title, {"text": prompt})
    else:
        if title in ENGINE.instances:
            await _end_verify_session(title)
        # THE REPO HAS TO STILL BE THERE, the same preflight `/run` does and for
        # a worse reason: `create_instance` CREATES a missing `repo_path` and
        # falls back to a git-less in-place session, so a checklist whose clone
        # has been moved or deleted started a real, billed agent in a brand-new
        # empty folder — holding instructions to reproduce and repair code that
        # is not there. Scoped to this branch, because a fix session's own
        # worktree can legitimately outlive the main repo.
        if not await asyncio.to_thread(
            _is_verify_repo_usable, plan.get("repo_root") or ""
        ):
            return _fix_refusal(
                409,
                "the repo this checklist was written from is gone "
                "(%s) — a fix session needs it to work in"
                % (plan.get("repo_root") or "no path recorded"),
                "The repo this checklist was written from is gone — "
                "a fix session needs it to work in.",
                repo_root=plan.get("repo_root") or "",
            )
        # THE SAME WEDGE /run CLEARS, and this route had none of it. The title
        # is derived from the plan, so the branch and the tmux name repeat on
        # every press: one leftover — and `close_instance` KEEPS the worktree on
        # purpose, so pressing Fix, ending the session and pressing Fix again is
        # enough — made every later press die inside `_bg_start`, minutes after
        # this route answered 202, with the raw git or tmux line landing in the
        # notifications bell and nowhere near the checklist.
        #
        # Cleared where that is safe, REPORTED where it is not: an empty
        # leftover is removed, a leftover with work in it is named in a 409
        # below. What this must never do is what the run route's own reclaim
        # does — force-remove and delete the branch — because the whole point of
        # a fix session is the changes in that tree.
        killed = await asyncio.to_thread(_kill_orphan_plan_tmux, title)
        if killed and log.ErrorLog is not None:
            log.ErrorLog.Printf("fix: killed orphan tmux session %s", killed)
        reclaimed, held = await asyncio.to_thread(
            _reclaim_plan_worktree, plan.get("repo_root") or "", title
        )
        if reclaimed and log.ErrorLog is not None:
            log.ErrorLog.Printf("fix: reclaimed empty worktree %s", reclaimed)
        if held:
            # DECLINED, which is a different thing from "nothing to do" and the
            # commonest case here: this session's job is to change the tree, so
            # the leftover from the last one usually HAS changes, and taking it
            # would delete them. Said now, naming the directory, instead of
            # dying in `_bg_start` minutes later with a raw git line that lands
            # in the notifications bell and never reaches this checklist.
            return _fix_refusal(
                409,
                "the last fix session's workspace is still holding "
                "this branch and has uncommitted work in it (%s) — reopen it "
                "from Recently closed to finish or discard that work, then "
                "press Fix again" % held,
                "The last fix session still has unsaved work on this branch — "
                "reopen it from Recently closed to finish or discard it, then "
                "press Fix again.",
                worktree=held,
            )
        resp = await create_instance(
            {"title": title, "repo_path": plan["repo_root"], "prompt": prompt},
            request,
        )
    if resp.status_code >= 400:
        return resp
    return JSONResponse(
        {"ok": True, "session": title, "reclaimed": bool(reclaimed)}, status_code=202
    )


@app.post("/api/test-plans/{plan_id:path}/regenerate")
async def regenerate_test_plan(
    plan_id: str, payload: Optional[dict] = None
) -> JSONResponse:
    """Ask the model for this plan's steps again (202 — it lands in the store).

    The escape hatch for every generation failure: a timeout, a CLI with no
    headless mode, an unparseable answer, a worktree that had already gone. It
    is also how a plan whose steps read wrong gets a second draft.

    Runs against the session's own worktree and CLI when the session is still
    alive, and against the plan's repo otherwise (``generate`` falls back to
    ``repo_root`` as its cwd) — so a plan can still be regenerated long after the
    work that produced it has merged.

    ``{"focus": "…"}`` IS THE POINT OF THE SECOND DRAFT. This route used to take
    no body at all, so pressing Rewrite re-ran the identical prompt and hoped for
    a different answer — while the person pressing it had just read the weak
    checklist and knew exactly what should have been checked instead, which is
    the highest-signal input available anywhere in this feature and was being
    thrown away. It is stored on the plan rather than passed through, so a later
    push that re-reads the branch keeps honouring it: a correction you have to
    type twice is one you stop typing.

    409 WHILE A RUN IS IN FLIGHT. ``generate`` sets ``generating``
    unconditionally, and the run poller only ever looks at plans in ``running``
    — so rewriting mid-run orphaned a real, billed session forever: its result
    file was never read, the give-up clock never ran, and Cancel disappeared from
    the row along with the state that offered it.
    """
    plan = _test_plans.get(plan_id)
    if plan is None:
        return JSONResponse(
            {"error": "test plan not found: %s" % plan_id}, status_code=404
        )
    if plan["state"] == "running":
        return JSONResponse(
            {
                "error": "an agent is checking this checklist right now — "
                "cancel the run first"
            },
            status_code=409,
        )
    focus = str((payload or {}).get("focus") or "").strip()
    if focus:
        _test_plans.set_focus(plan_id, focus)
    program, worktree = _test_plan_session_ctx(plan_id)
    _start_test_plan_generation(plan_id, program, worktree)
    return JSONResponse({"ok": True}, status_code=202)


@app.post("/api/test-plans/{plan_id:path}/steps")
async def add_test_plan_step(plan_id: str, payload: dict) -> JSONResponse:
    """Append a step a person wrote to the end of a plan.

    The generator knows what the diff changed and nothing about what the team
    knows: the flow that always breaks, the report nobody remembers to open. A
    checklist that cannot take those is a checklist people keep a second copy of
    somewhere else, and the second copy is the one that never gets run.

    ``actor`` defaults to ``"agent"`` here and only here. Everywhere else in this
    feature an unknown actor becomes ``"human"``, because there the actor is a
    MODEL's guess about a step it invented and the cost of being wrong is an
    agent silently passing something it could not observe. This is a person
    typing a step they have just decided to add; "run this for me" is what they
    are asking for, and they can see the toggle that says otherwise.
    """
    payload = payload or {}
    plan = _test_plans.get(plan_id)
    if plan is None:
        return JSONResponse(
            {"error": "test plan not found: %s" % plan_id}, status_code=404
        )
    try:
        updated = await asyncio.to_thread(
            _test_plans.add_step,
            plan_id,
            str(payload.get("text") or ""),
            str(payload.get("expect") or ""),
            str(payload.get("actor") or "agent"),
        )
    except ValueError as err:
        return JSONResponse({"error": str(err)}, status_code=400)
    if updated is None:
        return JSONResponse(
            {"error": "test plan not found: %s" % plan_id}, status_code=404
        )
    return JSONResponse({"ok": True, "plan": updated})


@app.delete("/api/test-plans/{plan_id:path}/steps/{step_id}")
async def remove_test_plan_step(plan_id: str, step_id: str) -> JSONResponse:
    """Delete a step a person added. 400 for a generated one, 404 for neither.

    Only manual steps: a generated step comes back on the next regeneration, so
    a button that removed one would be a button whose effect quietly undoes
    itself. See ``test_plans.remove_step``.
    """
    try:
        updated = await asyncio.to_thread(_test_plans.remove_step, plan_id, step_id)
    except ValueError as err:
        return JSONResponse({"error": str(err)}, status_code=400)
    if updated is None:
        return JSONResponse(
            {"error": "no such step on %s: %s" % (plan_id, step_id)}, status_code=404
        )
    return JSONResponse({"ok": True, "plan": updated})


@app.patch("/api/test-plans/{plan_id:path}/steps/{step_id}")
async def edit_test_plan_step(
    plan_id: str, step_id: str, payload: dict
) -> JSONResponse:
    """Fix one step in place — ``{"text"?, "expect"?, "actor"?}``.

    The proportionate alternative to Rewrite. Correcting a single wrong sentence
    used to cost a model call, three minutes and every recorded answer on the
    plan; this costs the answers to the one step whose QUESTION changed, and
    nothing at all when only the actor moved. See ``test_plans.edit_step`` for
    why an edited step then counts as yours.

    An absent key means "leave it alone", which is what makes the actor toggle a
    one-field request rather than a read-modify-write of the whole step.
    """
    fields = {
        key: payload[key]
        for key in ("text", "expect", "actor")
        if isinstance(payload, dict) and key in payload
    }
    if not fields:
        return JSONResponse(
            {"error": "nothing to change — send text, expect or actor"},
            status_code=400,
        )
    try:
        updated = await asyncio.to_thread(
            _test_plans.edit_step, plan_id, step_id, **fields
        )
    except ValueError as err:
        return JSONResponse({"error": str(err)}, status_code=400)
    if updated is None:
        return JSONResponse(
            {"error": "no such step on %s: %s" % (plan_id, step_id)}, status_code=404
        )
    return JSONResponse({"ok": True, "plan": updated})


@app.post("/api/test-plans/{plan_id:path}/result")
def record_test_plan_result(plan_id: str, payload: dict) -> JSONResponse:
    """Record one step's outcome — the human half of a run.

    ``{"step_id", "result", "note"}``. Unlike the file a verify session writes
    (whose values are coerced, because a model cannot be told it sent something
    wrong), an unknown ``result`` here is a 400: this comes from our own UI, and
    quietly turning a typo into "blocked" would hide the bug.
    """
    payload = payload or {}
    step_id = str(payload.get("step_id", "") or "").strip()
    result = str(payload.get("result", "") or "").strip()
    note = str(payload.get("note", "") or "")
    was_failing = bool(_test_plans.failed_steps(_test_plans.get(plan_id) or {}))
    try:
        plan = _test_plans.record_result(plan_id, step_id, result, note, by="human")
    except ValueError as err:
        return JSONResponse({"error": str(err)}, status_code=400)
    if plan is None:
        # record_result returns None for both "no such plan" and "no such step"
        # — from the caller's side they are the same mistake: it named something
        # that isn't there.
        return JSONResponse(
            {"error": "test plan or step not found: %s / %s" % (plan_id, step_id)},
            status_code=404,
        )
    if result == "fail" and not was_failing:
        _announce_step_failed(plan, step_id)
    return JSONResponse({"ok": True, "plan": plan})


def _announce_step_failed(plan: dict, step_id: str) -> None:
    """``session.test_plan_step_failed``: a person's Fail just turned this
    checklist from not-failed to failed. The agent's own runs announce through
    ``session.test_plan_checked``; a hand-recorded Fail was silent, so the bell
    said "No notifications yet." beside a red Verify badge. Once per
    transition — a second Fail on an already-failed checklist is not news."""
    steps = plan.get("steps") or []
    idx = next((i for i, st in enumerate(steps) if st.get("id") == step_id), -1)
    try:
        _events.BUS.emit(
            "session.test_plan_step_failed",
            session=str(plan.get("id") or ""),
            new="failed",
            data={
                "plan": str(plan.get("id") or ""),
                "title": plan.get("title") or plan.get("id") or "",
                "branch": plan.get("branch") or "",
                "step": idx + 1,
                "text": str(steps[idx].get("text") or "")[:200] if idx >= 0 else "",
            },
        )
    except Exception:  # noqa: BLE001 — an announcement never fails the answer
        pass


@app.post("/api/test-plans/{plan_id:path}/deployed")
def mark_test_plan_deployed(plan_id: str) -> JSONResponse:
    """ "It's out — check it now." Skip the rest of the deploy wait.

    The manual counterpart to the clock in :func:`_check_test_plans_for_liveness`.
    A per-repo delay is a good guess and never a fact: a pipeline that usually
    takes fifteen minutes sometimes takes three, and a person watching it land
    should not have to wait out a timer that has already been proved wrong. So
    the wait has a door, and it is the only thing on this surface that moves a
    plan to ``due`` by hand.

    Deliberately NOT the inverse ("it hasn't deployed, wait longer"): the clock
    already errs long, and a plan that goes due early is recoverable — the steps
    are answerable whenever you like — while one nobody can release is not.

    Refuses anything that is not still waiting, rather than being idempotent
    about it: pressed on a plan that is already ``due`` this would be a no-op the
    UI never offers, and pressed on a ``done`` one it would silently reopen a
    checklist somebody had finished.
    """
    plan = _test_plans.get(plan_id)
    if plan is None:
        return JSONResponse(
            {"error": "test plan not found: %s" % plan_id}, status_code=404
        )
    if plan["state"] != "generated" or not plan["merged_at"]:
        return JSONResponse(
            {"error": "this checklist is not waiting on a deploy"}, status_code=409
        )
    updated = _test_plans.mark_due(plan_id)
    if updated is None:
        return JSONResponse(
            {"error": "test plan not found: %s" % plan_id}, status_code=404
        )
    _events.BUS.emit(
        "session.test_plan_due",
        session=plan_id,
        data={
            "plan": plan_id,
            "title": updated["title"],
            "live_branch": updated["live_branch"],
        },
    )
    return JSONResponse({"ok": True, "plan": updated})


@app.post("/api/test-plans/{plan_id:path}/cancel")
async def cancel_test_plan_run(plan_id: str) -> JSONResponse:
    """Stop the verify session working this plan and put the plan back.

    Without this, a run you started by mistake — on the wrong commit, or one
    that has wedged — could only be stopped by hunting down its session in the
    sidebar, and the plan itself stayed ``running`` until the due loop's
    two-hour give-up clock expired. Both halves happen here: the session ends
    and the plan returns to the state it was in before the run.

    Idempotent on purpose: a plan whose session has already gone (the user
    ended it themselves) still gets its bookkeeping cleared, which is precisely
    the wedge this is meant to be able to clear.
    """
    plan = _test_plans.get(plan_id)
    if plan is None:
        return JSONResponse(
            {"error": "test plan not found: %s" % plan_id}, status_code=404
        )
    session_title = plan["run_session"]
    closed = await _end_verify_session(session_title)
    updated = await asyncio.to_thread(_test_plans.cancel_run, plan_id)
    return JSONResponse(
        {
            "ok": True,
            "session": session_title,
            "closed": closed,
            "plan": updated,
        }
    )


@app.delete("/api/test-plans/{plan_id:path}")
async def delete_test_plan(plan_id: str) -> JSONResponse:
    """Forget one plan. The dismiss button — a plan the user has decided not to
    run is noise, and there is no other way to clear one (they deliberately
    outlive their sessions, so deleting the session does not take the plan with
    it).

    A plan being verified right now takes its verify session with it. Nothing
    would be left to write the results into, so an agent left running would be
    burning minutes to answer a checklist that no longer exists — and the
    session would linger in the grid with a name pointing at a deleted plan.
    """
    plan = _test_plans.get(plan_id)
    if plan is None or not _test_plans.delete(plan_id):
        return JSONResponse(
            {"error": "test plan not found: %s" % plan_id}, status_code=404
        )
    closed = await _end_verify_session(plan["run_session"])
    return JSONResponse({"ok": True, "closed": closed})


# --- Workspace disk management ------------------------------------------------
def _active_worktree_titles() -> dict:
    """``realpath(worktree) -> session title`` for every live instance.

    The worktree-in-use map the workspace list + bulk-clear handlers both need
    to tell an idle workspace apart from one a running session owns."""
    active = {}
    for inst in list(ENGINE.instances.values()):
        try:
            wp = inst.GetWorktreePath()
        except Exception:  # noqa: BLE001
            wp = ""
        if wp:
            active[os.path.realpath(wp)] = inst.Title
    return active


def _prune_base_clone_worktrees() -> None:
    """``git worktree prune`` every base clone under the managed workspace dirs.

    A removed linked worktree leaves a stale registration in its base repo(s);
    base clones are per-repo (``_base_<slug>``) so we prune every one under
    every managed workspace dir (a cheap no-op when the worktree wasn't theirs).
    Idempotent — shared by the single-delete and bulk-clear workspace paths."""
    ws_dirs = []
    s = provisioning.load_provision_settings()
    if s is not None:
        ws_dirs.append(str(s.workspace_dir))
    try:
        ws_dirs.append(str(provisioning.default_workspace_dir()))
    except Exception:  # noqa: BLE001
        pass
    seen = set()
    for ws in ws_dirs:
        ws = os.path.realpath(ws)
        if ws in seen or not os.path.isdir(ws):
            continue
        seen.add(ws)
        for entry in os.listdir(ws):
            if not provisioning.is_base_repo_dirname(entry):
                continue
            base = os.path.join(ws, entry)
            if os.path.isdir(base):
                _run_capped(
                    ["git", "-C", base, "worktree", "prune"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=60,
                )


@app.get("/api/workspaces")
async def list_workspaces(sizes: int = 0) -> JSONResponse:
    """List every workspace directory on disk under the managed roots.

    The raw disk listing (the merged Recently-closed page uses ``/api/recent``,
    which filters and annotates it). ``size_bytes`` is only computed when
    ``?sizes=1``: a ``du`` over a large workspace stats every file and can take
    seconds when the page cache is cold.
    """
    entries = await asyncio.to_thread(scan_workspaces, None, bool(sizes))
    return JSONResponse({"workspaces": entries, "roots": _workspace_roots()})


@app.post("/api/workspaces/delete")
async def delete_workspace(payload: dict) -> JSONResponse:
    """Delete a workspace directory (destructive). Path must be a *direct child*
    of a managed root — this is the guard against traversal / deleting the root
    or arbitrary paths. If an active session lives there, it is killed first."""
    path = (payload or {}).get("path", "") or ""
    real = os.path.realpath(path) if path else ""
    roots = _workspace_roots()
    # realpath has already collapsed any `..`, so the proper-ancestor check
    # (core.workspaces._strictly_under) can't be escaped via traversal.
    is_allowed = (
        bool(real)
        and os.path.isdir(real)
        and any(_strictly_under(real, root) for root in roots)
    )
    if not is_allowed:
        return JSONResponse(
            {"error": "path is not within a managed workspace root"}, status_code=400
        )
    # ...and it has to be a directory this server actually LISTS as a workspace:
    # a flat child of a provisioning root, or a worktree leaf. _strictly_under
    # alone accepts any descendant at any depth, so without this a path like
    # `<workspace_dir>/_base_repo/src` passed every guard below (the base-clone
    # protection tests only the basename) and deleted a subtree inside a
    # protected clone. This is the "direct child" rule the docstring above and
    # docs/web-api.md have always claimed.
    listed = {
        os.path.realpath(e["path"])
        for e in await asyncio.to_thread(scan_workspaces, {}, False)
    }
    if real not in listed:
        return JSONResponse(
            {
                "error": "path is not a workspace directory (only a listed workspace or worktree can be deleted)"
            },
            status_code=400,
        )
    # Protect shared infrastructure dirs — deleting them is expensive to rebuild.
    base = os.path.basename(real)
    if _is_refresher_dirname(base):
        return JSONResponse(
            {"error": "this workspace is protected and cannot be deleted"},
            status_code=400,
        )
    if provisioning.is_base_repo_dirname(base):
        # K4: a base clone is only *shared infrastructure* while something
        # still points at it. With no attached worktrees and no active session
        # based on it, it's just disk — deletable.
        holders, worktrees = _base_clone_references(real)
        if holders or worktrees:
            parts = []
            if holders:
                parts.append("sessions: %s" % ", ".join(sorted(holders)))
            if worktrees:
                parts.append("%d attached worktree(s)" % len(worktrees))
            return JSONResponse(
                {
                    "error": "this base clone is still in use by %s — close/delete "
                    "those first" % "; ".join(parts)
                },
                status_code=400,
            )

    # The live session working in this directory ends through the one
    # end-of-session path (wipe): recorded in Recently closed, its children
    # re-parented. Two or more sessions on the dir (a copy and its origin) is
    # the folder-in-use case — refused, nothing changes.
    holders = [
        t
        for t, inst in list(ENGINE.instances.items())
        if _realpath_or_empty(inst) == real
    ]
    if len(holders) > 1:
        return JSONResponse(
            {"error": "folder in use", "sessions": sorted(holders)}, status_code=409
        )
    killed = None
    children: list = []
    if holders:
        try:
            ended = await asyncio.to_thread(
                _session_end.end_session, ENGINE, holders[0], wipe=True
            )
        except _session_end.FolderInUse as err:
            return JSONResponse(err.to_json(), status_code=409)
        except _session_end.SessionNotFound:
            ended = None
        if ended is not None:
            killed = ended["title"]
            children = ended["children"]

    # Read before the directory goes: a linked worktree's `.git` FILE names the
    # repo that holds its registration, which is the only way to prune a NESTED
    # worktree (`worktrees/feature/sc-1/slug`) — the old "is my parent named
    # worktrees" test is false for every branch name with a slash in it, so those
    # registrations were never pruned and later made `git worktree add` for the
    # same branch fail.
    wt_repo = _worktree_repo_path(_worktree_gitdir(real))

    def _remove() -> None:
        shutil.rmtree(real, ignore_errors=True)
        _close_cursor_window(real)
        _remove_trust_entry(real)  # GC ~/.claude.json trust entry (G3)
        # Prune the stale registration so `git worktree` stays consistent: from
        # the repo the worktree itself pointed at when we know it, else the old
        # sweep over every base clone under every managed workspace dir.
        if wt_repo and os.path.isdir(wt_repo):
            _run_capped(
                ["git", "-C", wt_repo, "worktree", "prune"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
            )
            _prune_empty_worktree_dirs()
        elif os.path.dirname(real).endswith("worktrees"):
            _prune_base_clone_worktrees()

    await asyncio.to_thread(_remove)
    return JSONResponse({"ok": True, "killed_session": killed, "children": children})


def _realpath_or_empty(inst) -> str:
    """``realpath`` of the instance's worktree, or "" when it has none."""
    try:
        wp = inst.GetWorktreePath() or ""
    except Exception:  # noqa: BLE001
        return ""
    return os.path.realpath(wp) if wp else ""


@app.get("/api/recent")
async def recent_list(
    sizes: int = 0, days: float = STALE_WORKTREE_DAYS
) -> JSONResponse:
    """The Recently-closed page: closed sessions AND what is left on disk, in
    one list.

    One page rather than two, because "can I have this work back" and "can I
    have this disk back" are the same question asked about the same directory.
    Rows carry both identities — the closed session's title/branch/closed_at and
    the directory's kind/size/age — plus ``stale``, which marks the rows the
    unused-worktree sweep would take. Not shown, but counted in ``hidden``:
    protected base clones / cache refreshers, and any workspace a LIVE session
    is using (that one is in the sidebar).

    ``?sizes=1`` adds ``size_bytes`` (a ``du`` per row — seconds on a cold
    cache), which is why the page loads without it first. ``?days=`` previews a
    different staleness window than the sweep's default, so the marks and the
    sweep can be lined up on the same number instead of two.
    """
    if days < 0:
        return JSONResponse({"error": "days must be >= 0"}, status_code=400)
    data = await asyncio.to_thread(recent_rows, bool(sizes), days)
    return JSONResponse(data)


@app.post("/api/workspaces/prune-worktrees")
async def prune_worktrees(payload: Optional[dict] = None) -> JSONResponse:
    """Delete every unused git WORKTREE — and nothing else.

    "Unused" is: nothing has touched it for ``days`` (default 7), no live
    session is working in it, and no second session shares it. "Worktree" is
    load-bearing and checked per directory, twice (at scan time and again
    immediately before the delete): only a directory whose ``.git`` is a gitdir
    FILE, i.e. one ``git worktree add`` generated under a managed root, is ever
    a candidate. A repository, a clone, a ``_base_*`` mirror, a ``pr-*`` review
    clone and any plain folder are all excluded by that test — this route cannot
    delete a repo, whatever it is pointed at.

    ``{"dry_run": true}`` (the default) resolves the candidate list and returns
    it with sizes and a ``dirty`` flag per row, without touching anything: the
    UI shows that list in its confirmation, because the commits in a worktree
    live in the base repository and survive, while uncommitted changes, a stash
    or a detached HEAD do not. ``{"dry_run": false}`` performs the removal — of
    the clean rows only, unless ``{"include_dirty": true}``, which is the second
    thing the UI asks about — forgets the closed entries whose worktree just went
    (their Reopen could only 410 from then on), prunes the stale registration
    from each worktree's own base repo, and collects the empty branch-slug
    directories the removals leave behind.

    The candidate set is always recomputed HERE — no path a client sends is ever
    deleted.
    """
    payload = payload or {}
    raw = payload.get("days", STALE_WORKTREE_DAYS)
    try:
        days = float(raw)
    except (TypeError, ValueError):
        return JSONResponse({"error": "days must be a number"}, status_code=400)
    if days < 0:
        return JSONResponse({"error": "days must be >= 0"}, status_code=400)
    data = await asyncio.to_thread(
        prune_stale_worktrees,
        days,
        bool(payload.get("dry_run", True)),
        bool(payload.get("include_dirty", False)),
    )
    return JSONResponse(data)


# --------------------------------------------------------------------------- #
# IDE session discovery: adopt folders currently open in the configured IDE
# (opened outside MindFlock) as in-place sessions.
# --------------------------------------------------------------------------- #
# Realpaths already auto-adopted from Cursor, so the poll doesn't re-adopt a
# folder whose session the user killed (it stays memoized until the folder
# leaves Cursor's open list, i.e. the window is closed).
_CURSOR_SEEN: set = set()


def _session_for_path(abs_path: str) -> Optional[str]:
    """Title of an existing instance whose worktree is ``abs_path``, else None."""
    for inst in list(ENGINE.instances.values()):
        try:
            wp = inst.GetWorktreePath()
        except Exception:  # noqa: BLE001
            wp = ""
        if wp and os.path.realpath(wp) == abs_path:
            return inst.Title
    return None


def _create_inplace_session(abs_path: str):
    """Register an in-place session for ``abs_path`` and start it in the
    background. Caller must have validated it's a git repo with commits that is
    not already a session. Returns the (loading) instance."""
    title = _unique_title(os.path.basename(os.path.normpath(abs_path)) or "session")
    inst = session.NewInstance(
        session.InstanceOptions(
            title=title,
            path=abs_path,
            program=ENGINE.default_program(),
            in_place=True,
        )
    )
    inst.SetStatus(Loading)
    with ENGINE.lock:
        ENGINE.instances[title] = inst

    async def _bg_start() -> None:
        try:
            await asyncio.to_thread(inst.Start, True)
            ENGINE.save()
        except Exception as err:  # noqa: BLE001
            if log.ErrorLog is not None:
                log.ErrorLog.Printf("failed to start in-place %s: %v", title, err)
            _drop_failed_start(title, inst)
            # The auto-adopt loop memoizes this folder's realpath in
            # ``_CURSOR_SEEN`` right after we return, so a failed start would
            # otherwise wedge the folder forever (every tick skips it) until the
            # Cursor window is closed. Drop it from the memo so the next tick
            # re-adopts and retries.
            try:
                _CURSOR_SEEN.discard(os.path.realpath(abs_path))
            except OSError:
                pass

    _register_task(_bg_start())
    return inst


# Continuous auto-adopt: turn every folder open in Cursor into an in-place
# session, the whole time the server runs. Toggleable at runtime (Settings →
# IDE / the API below) and persisted as ``ui.cursor_autoadopt``, so switching it
# off survives a server restart — it used to be memory-only and every restart
# (self-update, Restart server) silently switched it back on.
# CS_CURSOR_AUTOADOPT=0 forces it OFF at boot regardless of the setting.
def _cursor_autoadopt_boot_value() -> bool:
    if os.environ.get("CS_CURSOR_AUTOADOPT", "1") == "0":
        return False
    try:
        from backend.config import settings as _settings

        return _settings.load_settings().ui.cursor_autoadopt is not False
    except Exception:  # noqa: BLE001 — an unreadable store keeps the old default
        return True


_CURSOR_AUTOADOPT_ENABLED = _cursor_autoadopt_boot_value()


# The cursor auto-adopt loop is started by the lifespan handler (it checks the
# runtime flag each tick so the sidebar switch can flip it without a restart).


@app.get("/api/cursor/autoadopt")
def cursor_autoadopt_status() -> JSONResponse:
    """Whether folders opened in the IDE are auto-adopted as in-place sessions."""
    return JSONResponse({"enabled": _CURSOR_AUTOADOPT_ENABLED})


@app.post("/api/cursor/autoadopt")
def cursor_autoadopt_set(
    payload: dict, allowed: bool = Depends(_auth.configure_allowed)
) -> JSONResponse:
    """Toggle IDE-folder auto-adoption. Body: ``{"enabled": <bool>}``. Owner
    only (``auth.may_configure``): on, it launches an agent in every folder
    opened in the IDE."""
    if not allowed:
        return _auth.configure_refused()
    global _CURSOR_AUTOADOPT_ENABLED
    _CURSOR_AUTOADOPT_ENABLED = bool((payload or {}).get("enabled"))
    try:
        from backend.config import settings as _settings

        _settings.update_settings(ui={"cursor_autoadopt": _CURSOR_AUTOADOPT_ENABLED})
    except Exception as err:  # noqa: BLE001 — the live toggle still applies
        if log.ErrorLog is not None:
            log.ErrorLog.Printf("cursor auto-adopt: persisting failed: %v", err)
    return JSONResponse({"enabled": _CURSOR_AUTOADOPT_ENABLED})


@app.get("/api/ide/open-on-ticket")
def ide_open_on_ticket_status() -> JSONResponse:
    """Whether a newly provisioned ticket/issue workspace is opened in the IDE.

    The EFFECTIVE value (env → settings.json → config.toml ``open_cursor``), so
    the switch shows what will actually happen, not just what settings.json
    holds."""
    from backend.session import provisioned as _prov

    return JSONResponse({"enabled": _prov.open_ide_on_ticket()})


@app.post("/api/ide/open-on-ticket")
def ide_open_on_ticket_set(payload: dict) -> JSONResponse:
    """Persist ``engine.open_cursor``. Body: ``{"enabled": <bool>}``. Saved in
    settings.json, which outranks a ``config.toml`` ``open_cursor = true``."""
    from backend.config import settings as _settings
    from backend.session import provisioned as _prov

    want = bool((payload or {}).get("enabled"))
    try:
        _settings.update_settings(engine={"open_cursor": want})
    except _settings.SettingsUnreadable:
        # Saving defaults over a file that didn't parse would lose it (and
        # settings sync would spread "everything deleted").
        return JSONResponse({"error": _settings.UNREADABLE_HINT}, status_code=409)
    return JSONResponse({"enabled": _prov.open_ide_on_ticket()})


@app.get("/api/scroll-speed")
def scroll_speed_get() -> JSONResponse:
    """Current terminal mouse-wheel scroll speed (lines per notch)."""
    return JSONResponse({"speed": load_scroll_speed()})


@app.post("/api/scroll-speed")
def scroll_speed_set(payload: dict) -> JSONResponse:
    """Persist the wheel scroll speed and apply it live to running terminals.

    Tunes the tmux copy-mode wheel binding (server-wide), so the change takes
    effect immediately on already-open terminals — no restart needed. The value
    is clamped to a sane range. Also written to settings ``ui.scroll_speed``
    (and stamped) so settings sync carries it to the user's other devices —
    the file stays the live value; the setting is what travels."""
    speed = save_scroll_speed((payload or {}).get("speed"))
    try:
        apply_scroll_speed(speed)
    except (
        Exception
    ):  # noqa: BLE001 — best-effort; persisted value still applies on next session
        pass
    try:
        from backend.config import settings as _settings

        _settings.update_settings(ui={"scroll_speed": speed})
        _settings_sync.local_change()
    except Exception:  # noqa: BLE001 — the live speed is set; sync is a bonus
        pass
    return JSONResponse({"speed": speed})


@app.get("/api/providers")
def list_providers() -> JSONResponse:
    """The registered coding-CLI providers.

    Lets the UI offer a provider picker for new sessions (set as the session's
    ``program``). The catch-all ``generic`` provider is omitted — it's the
    fallback for an arbitrary typed-in program, not a choice.
    """
    out = []
    for p in providers.all_providers():
        if p.name == "generic":
            continue
        out.append(
            {
                "name": p.name,
                "aliases": list(getattr(p, "program_aliases", ()) or ()),
                # Usage-window knowledge (E): how this CLI's limits reset in time, so
                # the UI can explain it and the scheduled refresh can pick a cadence.
                "usage_window": p.usage_window(),
                # Which rungs of the neutral thinking-effort ladder this CLI can
                # actually distinguish, so a per-item Effort picker can say where
                # a given queue's CLI tops out instead of offering six rungs that
                # silently collapse into three (see providers/effort.py).
                "effort": _provider_effort.capability(p),
            }
        )
    return JSONResponse({"providers": out, "default": ENGINE.default_program()})


@app.get("/api/window-refresh")
def get_window_refresh() -> JSONResponse:
    """The scheduled window-refresh config + per-provider window knowledge and
    next-fire time (roadmap E)."""
    cfg = _window_refresh.get_config()
    opts = []
    for p in providers.all_providers():
        if p.name == "generic":
            continue
        opts.append(
            {
                "name": p.name,
                "window": p.usage_window(),
                "next_fire": _window_refresh.next_fire_at(p.name, cfg),
                "last_fired": cfg["last_fired"].get(p.name, 0.0),
            }
        )
    return JSONResponse(
        {
            "enabled": cfg["enabled"],
            "interval_hours": cfg["interval_hours"],
            "anchor_time": cfg["anchor_time"],
            "providers": cfg["providers"],
            "options": opts,
        }
    )


@app.post("/api/window-refresh")
def set_window_refresh(payload: dict) -> JSONResponse:
    """Enable/disable the schedule, set the interval OR a daily anchor time, and
    pick which providers to keep warm. Only the fields present in the body are
    changed. ``anchor_time`` ("HH:MM", or "" to clear) fires a fresh window
    daily at that time and takes precedence over the interval."""
    payload = payload or {}
    cfg = _window_refresh.set_config(
        enabled=payload.get("enabled"),
        interval_hours=payload.get("interval_hours"),
        anchor_time=payload.get("anchor_time"),
        providers=payload.get("providers"),
    )
    return JSONResponse(
        {
            "enabled": cfg["enabled"],
            "interval_hours": cfg["interval_hours"],
            "anchor_time": cfg["anchor_time"],
            "providers": cfg["providers"],
        }
    )


# --------------------------------------------------------------------------- #
# Terminal websocket: bridge xterm.js <-> a `tmux attach-session` PTY
# --------------------------------------------------------------------------- #
# The PTY<->websocket pump is core.terminal.pump_pty (shared with addons),
# under the name the call sites use.
_serve_pty = pump_pty


# --------------------------------------------------------------------------- #
# Peer links (docs/peer-link.md): routes/peer.py
# --------------------------------------------------------------------------- #
app.include_router(_peer_routes.router)


@app.websocket("/api/instances/{title}/shell")
async def shell_ws(ws: WebSocket, title: str) -> None:
    """Interactive shell in the session's workspace dir (a separate tmux session
    from the agent, created on demand and persisted across reconnects)."""
    await ws.accept()
    inst = ENGINE.instances.get(title)
    if inst is None:
        await ws.close(code=4404)
        return
    wt = inst.GetWorktreePath()
    if not wt:
        await ws.send_text(
            json.dumps({"type": "error", "message": "workspace not ready yet"})
        )
        await ws.close(code=4409)
        return

    # Off the event loop (see terminal_ws): creating/attaching the shell tmux
    # session shells out, so running it inline serialized concurrent restores.
    name, err = await asyncio.to_thread(_ensure_shell_session, title, wt)
    if err is not None:
        await ws.send_text(
            json.dumps({"type": "error", "message": "failed to start shell: " + err})
        )
        await ws.close(code=4500)
        return

    try:
        proc = ptyprocess.PtyProcess.spawn(
            ["tmux", "attach-session", "-t", name],
            dimensions=(24, 80),
            env={**os.environ, "TERM": "xterm-256color"},
        )
    except Exception as err:  # noqa: BLE001
        await ws.send_text(json.dumps({"type": "error", "message": str(err)}))
        await ws.close(code=4500)
        return
    # Typing in the session's shell is presence at the window too — a person
    # running git commands there does not need a push about this session. Same
    # post-attach grace as the agent terminal: tmux's client probes are
    # answered by the emulator, not the human.
    attached_at = time.time()
    # The shell is the session's keyboard too (core.ownership): read-only
    # unless this person drives, exactly like the agent terminal.
    person, device = _ownership.caller(ws)
    loop = asyncio.get_running_loop()
    out_q: "asyncio.Queue" = asyncio.Queue()

    def _tell(frame: dict) -> None:  # any thread
        try:
            loop.call_soon_threadsafe(out_q.put_nowait, json.dumps(frame))
        except RuntimeError:
            pass

    conn = _ownership.attach(title, person, device, notify=_tell)
    out_q.put_nowait(json.dumps(_ownership.mode(title, person)))

    def _gate(_data):
        ok, notice = _ownership.may_type(conn)
        return None if ok else notice

    try:
        await _serve_pty(
            ws,
            proc,
            allow_input=True,
            on_input=lambda: (
                time.time() - attached_at > _ATTACH_PROBE_GRACE_S
                and _note_human_input(title, person, device)
            ),
            gate=_gate,
            out_q=out_q,
        )
    finally:
        _ownership.detach(conn)


@app.get("/api/instances/{title}/prompt")
def pane_prompt(title: str, q: str = "") -> JSONResponse:
    """Full body of the newest USER prompt starting with ``q`` (the TUI's
    width-truncated pinned row) — the desktop prompt bar's expansion for
    prompts older than the latest. ``{"text": null}`` on no match."""
    inst = ENGINE.instances.get(title)
    if inst is None:
        return JSONResponse({"error": "instance not found"}, status_code=404)
    return JSONResponse({"text": _session_find_prompt(inst, q)})


@app.get("/api/instances/{title}/history")
def pane_history(title: str, pane: str = "agent") -> PlainTextResponse:
    """Full tmux scrollback of the agent/shell pane as plain text.

    The web terminals attach to tmux, so xterm.js only ever holds one screen —
    the real history lives in tmux on the server. The pane-header "Copy all"
    button copies this to the clipboard, and the history overlay
    (frontend HistoryOverlay.tsx) renders it as a scrollable, selectable page
    (drag-selection can't scroll through tmux history in the live terminal).
    """
    inst = ENGINE.instances.get(title)
    if inst is None:
        return PlainTextResponse("instance not found", status_code=404)
    if pane != "shell":
        # The agent TUI redraws in place, so its tmux capture holds only
        # scroll-off fragments plus the current frame — incomplete, and laced
        # with the TUI's chrome (input box, status rows). The provider
        # transcript is the complete conversation; ALWAYS prefer it, falling
        # through to the capture only when none exists (non-Claude providers,
        # fresh sessions).
        # By tmux session name, not just the worktree: sibling windows on the
        # same directory share a transcript dir, and only the window's own
        # thread marker says which conversation is THIS pane's.
        text = _agent_transcript_text(
            inst.GetWorktreePath(), tmux.to_mindflock_tmux_name(title)
        )
        if text:
            return PlainTextResponse(text)
    base = (
        _shell_tmux_name(title)
        if pane == "shell"
        else tmux.to_mindflock_tmux_name(title)
    )
    name = _live_session_name(base)
    if name is None:
        return PlainTextResponse("no live session", status_code=404)
    try:
        out = subprocess.run(
            # -p print to stdout, -J join wrapped lines, -S - from the very
            # start of the scrollback. Targets the session's active pane.
            ["tmux", "capture-pane", "-p", "-J", "-t", name, "-S", "-"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return PlainTextResponse("capture timed out", status_code=500)
    if out.returncode != 0:
        return PlainTextResponse(
            out.stderr.strip() or "capture failed", status_code=500
        )
    return PlainTextResponse(out.stdout.rstrip("\n") + "\n")


def _agent_output(inst, title: str, view: str):
    """The text behind one ``/output`` view → ``(view, text, fallback, error,
    status)``. Blocking (transcript reads, tmux) — call via
    ``asyncio.to_thread``. See :mod:`backend.web.core.agent_io`."""
    wt = ""
    try:
        wt = inst.GetWorktreePath() or ""
    except Exception:  # noqa: BLE001
        wt = ""
    tmux_name = tmux.to_mindflock_tmux_name(title)
    fallback = False
    if view == "last_reply":
        text = None
        try:
            prov = providers.resolve(getattr(inst, "Program", "") or "")
            text = prov.last_assistant_text(tmux_name, wt)
        except Exception:  # noqa: BLE001 — no reply reads as "use the screen"
            text = None
        if isinstance(text, str) and text.strip():
            return "last_reply", text, False, None, 200
        # No transcript for this provider (or no reply yet): the screen is the
        # closest thing to "what it said last" — and saying so is the contract.
        view, fallback = "screen", True
    if view == "transcript":
        # Exactly /history?pane=agent's source chain: the provider transcript,
        # else the pane's whole scrollback.
        text = _agent_transcript_text(wt, tmux_name) if wt else ""
        if text:
            return "transcript", text, False, None, 200
        name = _live_session_name(tmux_name)
        if name is None:
            return view, "", False, "no live session", 409
        text, cap_err = _agent_io.capture_scrollback(name)
        if cap_err is not None:
            return view, "", False, cap_err, 500
        return "transcript", text, False, None, 200
    name = _live_session_name(tmux_name)
    if name is None:
        return view, "", fallback, "no live session", 409
    text, cap_err = _agent_io.capture_screen(name)
    if cap_err is not None:
        return view, "", fallback, cap_err, 500
    return "screen", text, fallback, None, 200


@app.get("/api/instances/{title}/output")
async def instance_output(
    title: str, view: str = "last_reply", max_chars: Optional[str] = None
) -> JSONResponse:
    """What the session's agent produced, for another agent to read.

    ``view``: ``last_reply`` (default — the provider's newest assistant
    message; falls back to ``screen`` with ``"fallback": true`` when the
    provider has none), ``transcript`` (the text ``/history?pane=agent``
    serves) or ``screen`` (the visible pane only — what a dialog looks like).
    ``max_chars`` (default 6000, capped at 50000) keeps the TAIL.

    → ``{"view", "text", "truncated", "activity"}`` (+ ``"fallback": true``).
    404 unknown session, 400 bad view/max_chars, 409 ``no live session`` when
    the view needs the tmux pane and it is gone.
    """
    inst, err = _inst_or_404(title)
    if err is not None:
        return err
    if view not in _agent_io.OUTPUT_VIEWS:
        return JSONResponse(
            {"error": "view must be one of: %s" % ", ".join(_agent_io.OUTPUT_VIEWS)},
            status_code=400,
        )
    limit_chars, perr = _agent_io.parse_max_chars(max_chars)
    if perr:
        return JSONResponse({"error": perr}, status_code=400)
    if view == "say":
        # The spoken line (core.spoken): ≤320 chars by construction, so never
        # truncated; ``source`` says where it came from ("hook" = the reply's
        # own Say: line, "digest" = made from the reply, "none" = nothing).
        sp = await asyncio.to_thread(_spoken.spoken_for, title, inst)
        try:
            activity = await asyncio.to_thread(_agent_activity_cached, inst, title)
        except Exception:  # noqa: BLE001 — enrichment only
            activity = ""
        return JSONResponse(
            {
                "view": "say",
                "text": sp.get("say") or "",
                "source": sp.get("source") or "none",
                "has_question": bool(sp.get("has_question")),
                "truncated": False,
                "activity": activity,
            }
        )
    got_view, text, fallback, oerr, status = await asyncio.to_thread(
        _agent_output, inst, title, view
    )
    if oerr is not None:
        return JSONResponse({"error": oerr}, status_code=status)
    text, truncated = _agent_io.tail(text, limit_chars)
    try:
        activity = await asyncio.to_thread(_agent_activity_cached, inst, title)
    except Exception:  # noqa: BLE001 — enrichment only
        activity = ""
    body = {
        "view": got_view,
        "text": text,
        "truncated": truncated,
        "activity": activity,
    }
    if fallback:
        body["fallback"] = True
    return JSONResponse(body)


@app.post("/api/instances/{title}/answer")
async def instance_answer(
    title: str, request: Request, payload: dict = None
) -> JSONResponse:
    """Answer a dialog the session's agent is blocked on.

    Body ``{"text"?: str, "keys"?: [..]}``: ``text`` (≤ 2000 chars, control
    characters stripped) is typed literally WITHOUT an Enter, then each key
    (allow-list: ``Enter Escape Up Down Left Right Tab BTab Space 1..9 y n``) is
    pressed in order. Allowed only while the agent's live (uncached) activity
    is ``clarify`` or ``limit``, or while its provider PARSES a dialog on the
    visible screen whatever the reading says (screen evidence beats the
    activity layer) — anything else is 409, because typing into a working or
    idle agent is a prompt, not an answer (that is ``/send``).

    ``dialog_id`` (optional, from ``GET /dialog``) pins the answer to the
    dialog it was meant for: when the dialog on screen now has another id —
    the prompt was answered meanwhile and the NEXT one is up — nothing is
    typed and the answer is 409 ``{"error": "the prompt changed",
    "dialog_changed": true}``. One answer per dialog: a settling answer
    (a digit, Enter, Escape, y/n) to a ``dialog_id`` that got one less than
    a few seconds ago is 409 ``{"error": "that prompt was just answered",
    "dialog_answered": true}`` (a double click, or a person and an
    orchestrator answering the same prompt). ``by`` (``"agent"`` default, or
    ``"user"``): an agent's answer does not count as human presence, a
    person's click in the UI does (it is stamped like ``/send``).
    → ``{"ok": true, "activity_before"}``; 404 unknown, 400 bad body, 409 not
    waiting / over budget / dialog changed / just answered / driven by
    another person (``{"take": true}`` takes the keyboard first), 502 when
    tmux refused the keys. A ``dialog_id`` of a prose question (``q:…``, from
    ``/dialog``'s ``kind: "question"``) is 409 ``{"error": "not a dialog",
    "no_dialog": true}`` — answer it with ``/send``.
    """
    inst, err = _inst_or_404(title)
    if err is not None:
        return err
    text, keys, perr = _agent_io.parse_answer(payload)
    if perr:
        return JSONResponse({"error": perr}, status_code=400)
    dialog_id, by, perr = _agent_io.parse_answer_meta(payload)
    if perr:
        return JSONResponse({"error": perr}, status_code=400)
    if dialog_id is not None and dialog_id.startswith("q:"):
        # A question asked in prose (GET /dialog's ``kind: "question"``):
        # nothing on screen to press — the answer is a prompt, via /send.
        return JSONResponse(
            {"error": "not a dialog", "no_dialog": True}, status_code=409
        )
    busy = _driven_elsewhere(request, title, payload)
    if busy is not None:
        return busy
    if await asyncio.to_thread(_budget_locked, title):
        return JSONResponse(
            {
                "error": "session is over budget — raise the budget to send",
                "budget_locked": True,
            },
            status_code=409,
        )
    body, status = await asyncio.to_thread(
        _answer_dialog, inst, title, text, keys, dialog_id, by
    )
    return JSONResponse(body, status_code=status)


def _answer_dialog(inst, title: str, text: str, keys, dialog_id, by: str):
    """``/answer``'s check-and-type → ``(body, status)``. Blocking.

    Holds the session's answer lock from the activity probe through the
    keys, so two answers to one prompt can't both pass the dialog check
    before either has typed. A settling answer pinned to a ``dialog_id`` is
    remembered: the same id answered again within
    ``_agent_io.ANSWERED_HOLD_S`` is 409 ``dialog_answered`` — the CLI may
    not have redrawn yet, so the old dialog still being on screen proves
    nothing."""
    with _agent_io.answer_lock(title):
        try:
            activity = _agent_activity(inst, title)
        except Exception:  # noqa: BLE001 — unknown reads as "don't type blind"
            activity = "unknown"
        name = tmux.to_mindflock_tmux_name(title)
        program = getattr(inst, "Program", "") or ""

        def read_screen():
            live = _live_session_name(name)
            if live is None:
                return None, "no live session"
            return _agent_io.current_dialog(live, program)

        body = derr = None
        read = False
        if activity not in _agent_io.ANSWERABLE_ACTIVITIES:
            # Screen evidence beats the reading: a dialog the provider PARSES
            # on the visible screen is a prompt to answer, whatever the
            # activity says (a background sub-agent's tool events rewrite
            # the hook marker while its permission prompt is still up — a
            # click on a visible dialog was refused as "not waiting on a
            # prompt (activity: working)"). A paused/offline session is
            # never typed into.
            if activity != "offline":
                body, derr = read_screen()
                read = True
            if derr is not None or not (body or {}).get("parsed"):
                return {
                    "error": "session is not waiting on a prompt (activity: %s)"
                    % activity
                }, 409
            activity = "clarify"
        settles = dialog_id is not None and _agent_io.settles(keys)
        if dialog_id is not None:
            # The dialog the click was meant for must still be the one on
            # screen: a click on a strip a poll behind would otherwise answer
            # the NEXT prompt with a key chosen for the previous one. A screen
            # that can't be read can't be vouched for either.
            if not read:
                body, derr = read_screen()
            if derr is not None or body is None or body.get("id") != dialog_id:
                if derr is None and body is not None:
                    _agent_io.note_dialog_seen(title, body.get("id"))
                return {"error": "the prompt changed", "dialog_changed": True}, 409
            if settles and _agent_io.answered_recently(title, dialog_id):
                return {
                    "error": "that prompt was just answered",
                    "dialog_answered": True,
                }, 409
        if by == "user":
            # A person clicked the dialog's button: presence, exactly like /send.
            _note_human_input(title)
        if not _agent_io.send_answer(name, text, keys):
            return {"error": _SEND_FAILED}, 502
        if settles:
            _agent_io.note_answered(title, dialog_id)
            # The push's Yes/No buttons were for THIS dialog: spent with it.
            _voice_actions.forget(title, dialog_id)
        return {"ok": True, "activity_before": activity}, 200


@app.get("/api/instances/{title}/dialog")
async def instance_dialog(title: str, quiet: Optional[str] = None):
    """The dialog the session's agent is blocked on, as data — for the UI's
    in-place answer buttons (rail strip, bell, Thread tab).

    → ``{"id", "kind": "dialog", "parsed", "question", "command",
    "options": [{"key", "label", "kind"}]}``. The session's provider parses
    its visible screen
    (``parse_dialog``; Claude Code and Codex so far): ``kind`` is ``yes`` /
    ``always`` / ``no`` / ``other``, ``key`` the digit that picks the option
    (post it to ``/answer`` with this ``id`` as ``dialog_id``). Unparsed →
    ``parsed: false``, the best-effort question line and no options. ``id``
    hashes the dialog's screen region (cursor-free), so it holds while the
    same dialog is up.

    Served while the agent's live (uncached) activity is ``clarify`` — or,
    whatever the reading, while its provider PARSES a dialog on the visible
    screen (screen evidence beats the activity layer: a background
    sub-agent's prompt can read ``working`` or ``idle`` for a poll or two).
    ``limit`` / ``offline``, or another reading with no parsed dialog on
    screen, is 409 ``session is not waiting on a prompt (activity: X)``. 404
    unknown; 409 ``no live session``; 500 when the capture fails. ``source``
    (only when set): who raised it, e.g. ``"general-purpose agent"`` for a
    Claude background sub-agent's prompt.

    ``?quiet=1`` (the UI's answer strips): "not waiting on a prompt" is
    **204** with no body instead of the 409 — an expected answer for a strip
    whose row reading is a poll behind, which a browser would otherwise log
    as a failed request every time.

    A question asked in PROSE (activity idle, the row's ``asking.kind`` is
    ``"question"`` — core.asking) is 200 ``{"id": "q:<ts>", "kind":
    "question", "parsed": false, "question", "options": [], "ts"}``, so one
    fetch covers both; it is answered with ``/send`` (``/answer`` on a
    ``q:`` id is 409 ``no_dialog``).
    """
    inst, err = _inst_or_404(title)
    if err is not None:
        return err

    def not_waiting(activity: str):
        if activity == "idle":
            got = _asking.current(title, inst, "idle")
            if got and got.get("kind") == "question":
                return JSONResponse(
                    {
                        "id": _asking.question_id(got["ts"]),
                        "kind": "question",
                        "parsed": False,
                        "question": got.get("text"),
                        "options": [],
                        "ts": got["ts"],
                    }
                )
        if quiet in ("1", "true"):
            return Response(status_code=204)
        return JSONResponse(
            {"error": "session is not waiting on a prompt (activity: %s)" % activity},
            status_code=409,
        )

    try:
        activity = await asyncio.to_thread(_agent_activity, inst, title)
    except Exception:  # noqa: BLE001 — unknown reads as "nothing to show"
        activity = "unknown"
    if activity in ("limit", "offline"):
        return not_waiting(activity)
    name = await asyncio.to_thread(
        _live_session_name, tmux.to_mindflock_tmux_name(title)
    )
    if name is None:
        if activity != "clarify":
            return not_waiting(activity)
        return JSONResponse({"error": "no live session"}, status_code=409)
    body, derr = await asyncio.to_thread(
        _agent_io.current_dialog, name, getattr(inst, "Program", "") or ""
    )
    if activity != "clarify" and (derr is not None or not body.get("parsed")):
        return not_waiting(activity)
    if derr is not None:
        return JSONResponse({"error": derr}, status_code=500)
    _agent_io.note_dialog_seen(title, body.get("id"))
    # A parser may name its own kind (``select`` for Claude's question prompt);
    # ``dialog`` is the default for a permission / choice prompt.
    return JSONResponse({"kind": "dialog", **body})


@app.post("/api/instances/{title}/asking/dismiss")
async def instance_asking_dismiss(title: str, payload: Optional[dict] = None):
    """Dismiss the question ``title``'s newest reply asks (core.asking): the
    row's ``asking`` stops carrying it and no push repeats it; a NEWER reply
    that asks shows again. ``{}`` → 200 ``{"ok": true, "dismissed": bool}``
    (``dismissed`` false when nothing was asking); 404 unknown session. A
    dialog (``kind: "dialog"``) is not dismissed — it blocks the agent until
    it is answered."""
    inst, err = _inst_or_404(title)
    if err is not None:
        return err
    dismissed = await asyncio.to_thread(_asking.dismiss, title)
    with _PROBE_CACHE_LOCK:  # the next row read sees it, not a 2.5s-old memo
        _PROBE_CACHE.pop(("asking", title), None)
    return JSONResponse({"ok": True, "dismissed": dismissed})


@app.post("/api/instances/{title}/fence")
async def instance_fence(title: str, payload: Optional[dict] = None) -> JSONResponse:
    """Fence one session: ``{"only": [glob], "keep_out": [glob], "reason",
    "by", "clear"}``. A per-session fence (core.worker_order_driver) — "only
    here" and/or "keep out" for this session alone, enforced by the guard
    hook on every edit; never the folder's zones, so an orchestrator fencing
    an in-place worker does not fence itself. Replaces the session's fence;
    ``clear: true`` removes it. A held worker gets it in front of its task;
    a running one is told. → ``{ok, fence, applied, held, told?,
    shared_folder?, problems?, note?}``; 404 unknown session, 400 bad paths."""
    peer_err = _peer_guard.title_refusal(title, "fences")
    if peer_err is not None:
        return peer_err
    code, body = await asyncio.to_thread(
        _worker_order_driver.set_fence, title, payload or {}
    )
    return JSONResponse(body, status_code=code)


@app.get("/api/instances/{title}/order")
async def instance_order_get(title: str) -> JSONResponse:
    """The order ``title``'s workers run in (``{"order": view | null}``) —
    the Thread's diagram: ``{mode, max_parallel, cap, steps: [{n, workers:
    [{title, state, word, detail, after, why, fence, planned, …}]}]}``."""
    if ENGINE.instances.get(title) is None:
        return JSONResponse(
            {"error": "instance not found: %s" % title}, status_code=404
        )
    view = await asyncio.to_thread(_worker_order_driver.order_view, title)
    return JSONResponse({"order": view})


@app.post("/api/instances/{title}/order")
async def instance_order_set(
    title: str, payload: Optional[dict] = None
) -> JSONResponse:
    """Set how ``title``'s workers run: ``{"mode": "parallel"|"serial",
    "max_parallel": 0..16 (0 = no limit), "steps": [[titles], …], "after":
    {worker: [titles]}, "start_now": [titles]}`` — any subset. A cycle, an
    unknown worker or a started one in ``after`` refuses the whole change
    (400 with ``problems``). → ``{ok, order}``."""
    peer_err = _peer_guard.title_refusal(title, "worker order")
    if peer_err is not None:
        return peer_err
    code, body = await asyncio.to_thread(
        _worker_order_driver.set_order, title, payload or {}
    )
    return JSONResponse(body, status_code=code)


@app.get("/api/instances/{title}/thread")
async def instance_thread(
    title: str, limit: Optional[str] = None, before: Optional[str] = None
) -> JSONResponse:
    """The session's family thread — read-only, never marks mail read.

    → ``{"title", "parent", "members": [{"title", "role": self|parent|child,
    "status", "activity", "activity_since", "branch", "diff_stat",
    "created_at", "last_report", "base_sha"}], "items": [{"type":
    spawn|message|result, "id", "ts", "from", "to", "text", "status",
    "state", "base_sha"}], "more"}``. Members: the session, its live parent,
    its live children. Items, oldest first: a spawn record per parent→child
    edge (``ts`` = the child's creation, ``text`` = the start of its seed
    prompt when known, ``base_sha`` = the commit its worktree was cut from)
    and every message between two members, both directions, consumed or
    not. ``limit`` (default 50, max 200) keeps the newest; ``before`` (an
    item id) pages back, ``more`` says older items exist.

    404 unknown session; 400 bad ``limit`` or an unknown ``before`` id.
    """
    if ENGINE.instances.get(title) is None:
        return JSONResponse(
            {"error": "instance not found: %s" % title}, status_code=404
        )
    n, perr = _thread.parse_limit(limit)
    if perr:
        return JSONResponse({"error": perr}, status_code=400)
    body, terr = await asyncio.to_thread(_thread.thread, title, n, before or None)
    if terr is not None:
        status = 404 if terr.startswith("instance not found") else 400
        return JSONResponse({"error": terr}, status_code=status)
    return JSONResponse(body)


# --------------------------------------------------------------------------- #
# Playbooks: named orchestration prompts the UI pastes (backend.mcp.playbooks)
# --------------------------------------------------------------------------- #
#: Why a playbook can't be pasted into a session right now.
_PB_NO_TOOLS = "This CLI doesn't get the MindFlock tools"
_PB_NOT_ATTACHED = "Restart this agent to give it the MindFlock tools"
_PB_IN_DIALOG = "Answer its prompt first — pasting now would answer the dialog"
#: Activities in which pasted text would land in a dialog, not the input box.
_PB_DIALOG_ACTIVITIES = ("clarify", "limit")


def _mcp_unattachable(program: str) -> Optional[str]:
    """Why ``program``'s agent gets no MindFlock tools (the feature is off, or
    its CLI has no auto-attach), or None when new launches attach it."""
    caps = _agent_mcp_caps()
    try:
        name = providers.resolve(program or "").name
    except Exception:  # noqa: BLE001 — an unresolvable CLI can't attach
        name = ""
    if not caps.get("enabled") or name not in (caps.get("providers") or ()):
        return _PB_NO_TOOLS
    return None


def _playbook_ctx(inst, title: str) -> dict:
    """``playbooks.render``'s context for ``title``: its provider (tool
    spelling), branch (what ``wrapup`` merges into) and live children with
    whether each has reported."""
    try:
        provider = providers.resolve(getattr(inst, "Program", "") or "").name
    except Exception:  # noqa: BLE001
        provider = ""
    with ENGINE.lock:
        instances = dict(ENGINE.instances)
    children = [
        {
            "title": c,
            "reported": _thread.last_report(instances, c) is not None,
        }
        for c in _lineage.children_of(instances, title)
    ]
    return {
        "provider": provider,
        "branch": getattr(inst, "Branch", "") or "",
        "children": children,
    }


def _playbook_blocked(inst, title: str, *, live: bool = False) -> Optional[str]:
    """Why a playbook can't be pasted into ``title``'s agent now, or None.

    The CLI gets no MindFlock tools, this launch of it didn't
    (``mcp_attached`` false), or it is on a prompt / the usage-limit menu
    (pasted text would answer the dialog). ``live``: probe the activity
    uncached (the render route, right before a paste) rather than the
    memoized read the menu uses. Blocking."""
    reason = _mcp_unattachable(getattr(inst, "Program", "") or "")
    if reason is None:
        attached = providers.mcp_attach.launch_attached(
            tmux.to_mindflock_tmux_name(title)
        )
        if attached is False:
            reason = _PB_NOT_ATTACHED
    if reason is None:
        probe = _agent_activity if live else _agent_activity_cached
        try:
            activity = probe(inst, title)
        except Exception:  # noqa: BLE001 — unknown never blocks the menu
            activity = ""
        if activity in _PB_DIALOG_ACTIVITIES:
            reason = _PB_IN_DIALOG
    if reason is None and live:
        # Right before a paste: the screen outranks the reading.
        name = _live_session_name(tmux.to_mindflock_tmux_name(title))
        if name is not None and _dialog_on_screen(inst, name):
            reason = _PB_IN_DIALOG
    return reason


def _playbook_menu(inst, title: str) -> list:
    """The playbook list for ``title``'s menu: ``has_children`` ones omitted
    without live children, each with ``available`` / ``disabled_reason``.
    Blocking (the activity probe is the memoized one)."""
    reason = _playbook_blocked(inst, title)
    with ENGINE.lock:
        instances = dict(ENGINE.instances)
    has_children = bool(_lineage.children_of(instances, title))
    out = []
    for pb in _playbooks.registry(title):
        if not _playbooks.visible(pb["id"], has_children):
            continue
        pb["available"] = reason is None
        pb["disabled_reason"] = reason
        out.append(pb)
    return out


@app.get("/api/playbooks")
async def list_playbooks(title: Optional[str] = None) -> JSONResponse:
    """The named orchestration prompts → ``{"playbooks": [{id, label, desc,
    letter, args: [{name, label, kind, required}], when, available,
    disabled_reason}]}``, in menu order.

    With ``title``: the menu for that session. A ``when: has_children``
    playbook is left out entirely while it has no live children; every item
    is ``available: false`` with the reason when the session's CLI doesn't
    get the MindFlock tools, when this launch of it didn't
    (``mcp_attached`` false — a restart fixes it), or while it is in
    ``clarify`` / ``limit`` (pasted text would answer the dialog). 404 for an
    unknown title. Without ``title``: the whole registry, all available (the
    New Session dialog's list)."""
    if title is None or title == "":
        items = []
        for pb in _playbooks.registry():
            pb["available"] = True
            pb["disabled_reason"] = None
            items.append(pb)
        return JSONResponse({"playbooks": items})
    inst, err = _inst_or_404(title)
    if err is not None:
        return err
    items = await asyncio.to_thread(_playbook_menu, inst, title)
    return JSONResponse({"playbooks": items})


@app.post("/api/playbooks/{playbook_id}/render")
async def render_playbook(playbook_id: str, payload: dict = None) -> JSONResponse:
    """One playbook as prompt text for ``title``'s agent → ``{"text"}``.

    Body ``{"title", "args": {}}``. The text is ONE paragraph that names the
    session's own spelling of the MindFlock tools; a text argument left
    empty ends it on its lead-in ("The task: ") so the user types straight
    on. The UI pastes it with ``/send {"submit": false}`` — nothing runs
    until the user presses Enter. A ``session`` argument must name a live
    session (``ask``: any other session; ``wrapup``'s ``only``: one of
    ``title``'s live children) — matched exactly, however long or oddly
    spaced the title.

    409 ``{"error", "disabled_reason"}`` when the paste can't go in now —
    the menu's reasons, with the activity probed LIVE: the agent is on a
    prompt or the usage-limit menu (the pasted text would answer it), this
    launch of it has no MindFlock tools (``mcp_attached`` false), or its CLI
    gets none. 400 unknown playbook / bad args (a text argument that would
    push the paste past 600 characters included), 404 unknown title.
    """
    payload = payload if isinstance(payload, dict) else {}
    if _playbooks.get(playbook_id) is None:
        return JSONResponse(
            {"error": "unknown playbook: %s" % playbook_id}, status_code=400
        )
    title = payload.get("title")
    if not isinstance(title, str) or not title:
        return JSONResponse({"error": "title is required"}, status_code=400)
    inst, err = _inst_or_404(title)
    if err is not None:
        return err
    try:
        args = _playbooks.validate_args(playbook_id, payload.get("args"))
    except _playbooks.PlaybookError as perr:
        return JSONResponse({"error": str(perr)}, status_code=400)
    ask = args.get("session") if playbook_id == "ask" else ""
    if ask and (ask == title or ask not in ENGINE.instances):
        return JSONResponse(
            {"error": "session must name another live session: %s" % ask},
            status_code=400,
        )
    only = args.get("only") if playbook_id == "wrapup" else ""
    if only and only not in _lineage.children_of(ENGINE.instances, title):
        return JSONResponse(
            {"error": "only must name one of %s's workers: %s" % (title, only)},
            status_code=400,
        )
    blocked = await asyncio.to_thread(_playbook_blocked, inst, title, live=True)
    if blocked is not None:
        return JSONResponse(
            {"error": blocked, "disabled_reason": blocked}, status_code=409
        )
    ctx = await asyncio.to_thread(_playbook_ctx, inst, title)
    try:
        text = _playbooks.render(playbook_id, args, ctx)
    except _playbooks.PlaybookError as perr:
        return JSONResponse({"error": str(perr)}, status_code=400)
    return JSONResponse({"text": text})


@app.get("/api/instances/{title}/find")
def pane_find_mode(title: str, pane: str = "agent") -> JSONResponse:
    """How Ctrl+F searches this pane: ``{"mode": "tmux" | "scroll" |
    "overlay"}`` — whichever scrollback its mouse wheel actually moves (see
    pane_find.find_mode). ``overlay`` also when there's no live session."""
    if ENGINE.instances.get(title) is None:
        return JSONResponse({"error": "instance not found"}, status_code=404)
    name = _live_session_name(_find_base(title, pane))
    mode = (_pane_find.find_mode(name) if name else None) or "overlay"
    return JSONResponse({"mode": mode})


def _find_base(title: str, pane: Optional[str]) -> str:
    return (
        _shell_tmux_name(title)
        if pane == "shell"
        else tmux.to_mindflock_tmux_name(title)
    )


@app.post("/api/instances/{title}/find")
async def pane_find_step(title: str, payload: dict) -> JSONResponse:
    """One in-place find step in the live agent/shell pane, run the way the
    pane scrolls (tmux copy-mode search, or indexing the app's own scrollback
    and jumping to hits): ``op`` is ``prepare`` (build the index now — the
    bar just opened; a no-op in tmux mode), ``search`` (fresh query, lands on
    the newest hit), ``older`` / ``newer``, ``close`` (back to live) or
    ``cancel`` (stop a running step). Returns the engine's result plus
    ``mode``."""
    if ENGINE.instances.get(title) is None:
        return JSONResponse({"error": "instance not found"}, status_code=404)
    op = str(payload.get("op") or "search")
    if op not in ("prepare", "search", "older", "newer", "close", "cancel"):
        return JSONResponse({"error": "bad op"}, status_code=400)
    query = _find_query.Query.from_payload(payload)
    name = _live_session_name(_find_base(title, payload.get("pane")))
    if name is None:
        return JSONResponse({"error": "no live session"}, status_code=404)
    if op == "cancel":
        # Never queued behind the walk it cancels.
        return JSONResponse(_pane_scroll_find.find(name, "", "cancel"))
    mode = await asyncio.to_thread(_pane_find.find_mode, name)
    # Off the loop: a tmux step is several round trips; a scroll walk can
    # take seconds.
    if mode == "scroll":
        res = await asyncio.to_thread(_pane_scroll_find.find, name, query, op)
    elif op == "prepare":
        res = {"status": "ready", "mode": "tmux"}  # tmux search needs no index
    else:
        res = await asyncio.to_thread(_pane_find.find, name, query, op)
        if res is not None:
            res = {**res, "mode": "tmux"}
    if res is None:
        return JSONResponse({"error": "tmux unreachable"}, status_code=500)
    return JSONResponse(res)


@app.websocket("/api/instances/{title}/terminal")
async def terminal_ws(ws: WebSocket, title: str) -> None:
    """Live agent terminal: attach a per-connection PTY to the session's tmux
    agent pane, rebooting the agent session first if it died. Streams tmux
    output to the browser and browser keystrokes/resizes back into the pane."""
    await ws.accept()
    inst = ENGINE.instances.get(title)
    if inst is None:
        await ws.close(code=4404)
        return

    # Ensure the agent session exists, rebooting it if it died. It always
    # restarts; _ensure_agent_session decides whether to resume the prior
    # conversation (unnatural kill) or start fresh (clean Ctrl+C quit).
    # Off the event loop: this shells out to tmux (and, for a dead session,
    # relaunches the agent CLI). Run inline it serialized every pane's attach
    # and stalled polling/other sockets — on a multi-pane restore with dead
    # sessions that turned N reboots into one long blocking chain. In a thread
    # they proceed concurrently and the loop keeps serving. A SLEEPING agent
    # wakes here (core.sleep): opening its terminal is the wake. A PAUSED one
    # does not: both helpers refuse to relaunch it (PAUSED_REFUSED), and the
    # socket closes with 4423 so the pane shows its Paused placeholder and
    # does not reconnect. (Its agent still alive somehow → just attached.)
    _woke, err = await asyncio.to_thread(
        _sleep.ensure_awake, inst, title, "terminal opened"
    )
    if err is None:
        name, err = await asyncio.to_thread(_ensure_agent_session, inst, title)
    if err is not None:
        paused = err == PAUSED_REFUSED
        frame = {"type": "error", "message": err}
        if paused:
            frame["paused"] = True
        await ws.send_text(json.dumps(frame))
        await ws.close(code=4423 if paused else 4409)
        return
    # tmux mouse stays ON (wheel scroll / copy-mode). Browser selection is via
    # Shift+drag (xterm.js shouldForceSelection), copied by copy-on-select.
    # Each browser connection gets its own attach client (tmux multiplexes).
    try:
        proc = ptyprocess.PtyProcess.spawn(
            ["tmux", "attach-session", "-t", name],
            dimensions=(24, 80),
            env={**os.environ, "TERM": "xterm-256color"},
        )
    except Exception as err:  # noqa: BLE001
        await ws.send_text(json.dumps({"type": "error", "message": str(err)}))
        await ws.close(code=4500)
        return

    loop = asyncio.get_event_loop()
    out_q: "asyncio.Queue[Optional[bytes | str]]" = asyncio.Queue()
    fd = proc.fd

    def _on_readable() -> None:
        try:
            data = os.read(fd, 65536)
        except OSError:
            data = b""
        if not data:
            loop.remove_reader(fd)
            out_q.put_nowait(None)  # EOF sentinel
            return
        out_q.put_nowait(data)

    loop.add_reader(fd, _on_readable)

    async def _pump_out() -> None:
        while True:
            data = await out_q.get()
            if data is None:
                break
            try:
                if isinstance(data, str):  # a control frame (core.ownership)
                    await ws.send_text(data)
                else:
                    await ws.send_bytes(data)
            except Exception:  # noqa: BLE001
                break
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass

    sender = asyncio.create_task(_pump_out())
    # Presence stamps get a short post-attach grace: attaching a tmux client
    # makes tmux probe the terminal's capabilities, and xterm.js auto-answers
    # (DA1/DSR/CPR replies) through the SAME data path as typing — so every
    # pane mount, wifi blip, or laptop wake would otherwise stamp "a human is
    # here" for every open pane with nobody at any keyboard, deferring
    # announcements it was supposed to speed up.
    attached_at = time.time()
    # One keyboard per session (core.ownership): this terminal is read-only
    # unless its person drives; the first to attach to a session nobody
    # drives takes it. Terminal replies to tmux's probes always pass.
    person, device = _ownership.caller(ws)

    def _tell(frame: dict) -> None:  # any thread
        try:
            loop.call_soon_threadsafe(out_q.put_nowait, json.dumps(frame))
        except RuntimeError:
            pass  # loop closing

    conn = _ownership.attach(title, person, device, notify=_tell)
    out_q.put_nowait(json.dumps(_ownership.mode(title, person)))

    def note_presence(data=b"") -> None:
        if is_terminal_reply(data):
            return
        if time.time() - attached_at > _ATTACH_PROBE_GRACE_S:
            _note_human_input(title, person, device)

    def driving(data) -> bool:
        if is_terminal_reply(data):
            return True
        ok, notice = _ownership.may_type(conn)
        if not ok and notice:
            out_q.put_nowait(json.dumps({"type": "readonly", "message": notice}))
        return ok

    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            b = msg.get("bytes")
            if b is not None:
                # A human is at this window (stamped even when the budget lock
                # drops the keystroke below — presence is about the person, not
                # about whether the bytes landed).
                note_presence(b)
                # Budget lock: drop keystrokes once the session is over its
                # ceiling (authoritative — the UI overlay is just the visible
                # half). Resize control frames (text/JSON) still pass below.
                if _budget_locked(title) or not driving(b):
                    continue
                os.write(fd, b)
                continue
            t = msg.get("text")
            if t is not None:
                # Control frame (resize) or raw input typed as text.
                try:
                    j = json.loads(t)
                except (ValueError, TypeError):
                    note_presence(t)
                    if _budget_locked(title) or not driving(t):
                        continue
                    os.write(fd, t.encode("utf-8"))
                    continue
                if isinstance(j, dict) and j.get("type") == "resize":
                    # NOT presence: resizes fire on layout changes with nobody
                    # at the keyboard.
                    try:
                        proc.setwinsize(int(j["rows"]), int(j["cols"]))
                    except Exception:  # noqa: BLE001
                        pass
                else:
                    # A keystroke that happens to parse as JSON ("1", "true",
                    # "{}"). Stamp before the budget gate, same as the other
                    # two input branches — presence is about the person.
                    note_presence(t)
                    if not _budget_locked(title) and driving(t):
                        os.write(fd, t.encode("utf-8"))
    except WebSocketDisconnect:
        pass
    finally:
        _ownership.detach(conn)
        try:
            loop.remove_reader(fd)
        except Exception:  # noqa: BLE001
            pass
        out_q.put_nowait(None)
        sender.cancel()
        # Detach this client only (terminate the attach process); the tmux
        # session itself keeps running so the agent isn't interrupted.
        try:
            proc.terminate(force=True)
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# Session event stream (roadmap B2): pushes the core.events bus over a
# websocket so the SPA and extensions can react instead of poll-diffing.
# Envelope: {"seq", "event", "session", "old", "new", "ts", "data"}.
# --------------------------------------------------------------------------- #
async def _events_ws_reader(ws: WebSocket) -> None:
    """Drain incoming frames until the client disconnects (clients only listen,
    but reading is the only way to notice a peer that vanished silently)."""
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                return
    except Exception:  # noqa: BLE001
        return


@app.websocket("/api/events")
async def events_ws(ws: WebSocket) -> None:
    """Live session-event stream. On connect the ring-buffer backlog is sent
    first (``?since=<seq>`` skips events the client already saw), then events
    stream as they are emitted. Any number of clients may connect; a slow
    client just loses events (bounded queue) and a dead one is dropped.

    While ≥1 client is connected a background tick recomputes session state
    every ~4s (F6), so *_changed events flow even with no /api/instances
    poller anywhere."""
    await ws.accept()
    global _EVENTS_WS_CLIENTS
    _EVENTS_WS_CLIENTS += 1
    _ensure_state_ticker()
    try:
        since = int(ws.query_params.get("since") or 0)
    except (TypeError, ValueError):
        since = 0
    loop = asyncio.get_running_loop()
    queue: "asyncio.Queue[dict]" = asyncio.Queue(maxsize=256)

    def _enqueue(env: dict) -> None:
        # Called on whatever thread emits; trampoline onto this socket's loop.
        # A full queue means a client too slow to matter — drop the event
        # rather than ever blocking emit().
        def _put() -> None:
            try:
                queue.put_nowait(env)
            except asyncio.QueueFull:
                pass

        try:
            loop.call_soon_threadsafe(_put)
        except RuntimeError:
            pass  # loop already closing

    unsubscribe = _events.BUS.subscribe(_enqueue)
    reader = asyncio.create_task(_events_ws_reader(ws))
    getter = None
    try:
        # L4: hello frame FIRST (before the backlog) carrying the server's
        # clock, so clients can robustly tell replayed one-shot events (their
        # envelope ts predates server_time) from live ones — client clocks
        # can't be trusted for that comparison.
        now = time.time()
        await ws.send_json(
            {
                "seq": 0,
                "event": "hello",
                "session": "",
                "old": None,
                "new": None,
                "ts": now,
                "data": {},
                "server_time": now,
                "boot_id": _SERVER_BOOT_ID,
            }
        )
        # The live subscription starts BEFORE the backlog is fetched (so no
        # event can fall between them and be lost) — which means an event
        # emitted in that window sits in BOTH the backlog and the queue. Track
        # the highest seq already sent and skip queued frames at or below it,
        # so each event reaches the client exactly once.
        last_seq = since
        for env in _events.BUS.backlog(since):
            await ws.send_json(env)
            last_seq = max(last_seq, int(env.get("seq") or 0))
        while True:
            getter = asyncio.create_task(queue.get())
            done, _pending = await asyncio.wait(
                {getter, reader}, return_when=asyncio.FIRST_COMPLETED
            )
            if reader in done:
                break  # client went away
            env = getter.result()
            getter = None
            seq = int(env.get("seq") or 0)
            if seq and seq <= last_seq:
                continue  # already delivered via the backlog replay
            last_seq = max(last_seq, seq)
            await ws.send_json(env)
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 — dead client mid-send: drop quietly
        pass
    finally:
        _EVENTS_WS_CLIENTS -= 1  # ticker loop ends itself once this hits 0
        unsubscribe()
        if getter is not None:
            getter.cancel()
        reader.cancel()
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# Mobile single-terminal view
#
# A stripped-down page (one full-screen xterm.js + a session picker + a
# soft-key bar) meant to be opened from a phone over Tailscale. It reuses the
# same ``/api/instances`` list and the same ``/terminal`` + ``/shell`` terminal
# websockets the desktop grid uses — just a different, touch-friendly head.
# Served on an explicit route so the URL stays a clean ``/m`` (the static mount
# below would otherwise only expose it as ``/mobile.html``).
# --------------------------------------------------------------------------- #
@app.get("/m")
def mobile_view(request: Request) -> FileResponse:
    """Serve the mobile (``/m``) single-page shell for phone access.

    A load from off this machine is a phone (or another device) that made it
    here, which is what the first-run checklist's "Your phone" step means."""
    try:
        host = (request.client.host if request.client else "") or ""
        if host and host not in ("127.0.0.1", "::1", "localhost", "testclient"):
            _setup_steps.mark("phone")
            # The setup step is forward-only (stamped once); Connections'
            # "Your phone" row wants the LAST load.
            from backend.web.addons import connections as _connections

            _connections.note_phone_load()
    except Exception:  # noqa: BLE001 — bookkeeping only
        pass
    return FileResponse(str(_STATIC / "mobile.html"))


# --------------------------------------------------------------------------- #
# Addons: self-register their routers (+ the /api/addons manifest) BEFORE the
# static mount so their /api/* and websocket routes win over the catch-all.
# --------------------------------------------------------------------------- #
_ADDON_CTX = _AddonContext(engine=ENGINE, register_task=_register_task, log=log)
ADDONS = register_addons(app, _ADDON_CTX)


# --------------------------------------------------------------------------- #
# Static frontend (mounted last so /api/* takes precedence)
# --------------------------------------------------------------------------- #
app.mount("/", StaticFiles(directory=str(_STATIC), html=True), name="static")
