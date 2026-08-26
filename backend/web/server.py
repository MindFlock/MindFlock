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
This module owns the app assembly (lifespan, middleware, auth route), every
HTTP/websocket route, and the always-on background loops (instances tick,
events state tick, prompt-queue drain, window-refresh keepalive) plus the
short-TTL probe memo they share. Helper logic lives in focused modules under
``backend/web/core/``:

    agent_sessions   tmux plumbing for the agent/shell panes (ensure/send/kill)
    agent_state      activity detection: working/clarify/idle/offline, stage
    auth             the access-token gate
    budget           per-session cost budgets: guardrail event + input lock
    cursor_windows   IDE window adoption/focus/close (Cursor & friends)
    engine           the Engine singleton: instance registry + state reload
    events           the event bus behind /api/events
    git_ops          git primitives (branch/sha/dirty/origin probes, caches)
    ide_launch       launching the configured IDE on a folder
    mobile_access    phone access: tailscale URLs, QR codes, startup banner
    plain_repo       base-folder selection/validation for plain sessions
    ports            per-session port-block reservations
    pr_review        automated PR-review pipeline glue
    prompt_queue     the per-session prompt queue store
    recently_closed  the undo store behind reopen / Ctrl+Z
    remote           tailnet multi-device discovery + proxying
    repo_picker      ranked repo suggestions for the New Session folder field
    session_stats    token/cost telemetry + transcript history rendering
    snapshot         per-session JSON descriptors (labels, diff stat)
    system_logs      log-tail sources for Settings → System logs
    terminal         PTY/tmux attach plumbing shared by the websockets
    uploads          pasted/dropped file storage + retention
    usage_api        per-provider usage descriptors for /api/usage
    window_refresh   window-refresh keepalive config + scheduling
    workspaces       workspace roots, classification, guarded deletion
    worktree_setup   worktree setup/check (verification gate) runners

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
import weakref
from pathlib import Path
from typing import Dict, Optional, Tuple

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

import ptyprocess

from backend import config, log
from backend import providers
from backend import session
from backend.providers import config as provider_config
from backend.providers import effort as _provider_effort
from backend.providers.claude import remove_trust_entry as _remove_trust_entry
from backend.config import ide as ide_cfg
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
from backend.web.core import auth as _auth
from backend.web.core import autopilot as _autopilot
from backend.web.core import commit_message as _commit_message
from backend.web.core import events as _events
from backend.web.core import ports as _ports
from backend.web.core import issue_start as _issue_start
from backend.web.core import github_pr as _github_pr
from backend.web.core import live_stage as _live_stage
from backend.web.core import pr_review as _pr_review
from backend.web.core import reopen as _reopen
from backend.web.core import worktree_reclaim as _worktree_reclaim
from backend.web.core import ticket_start as _ticket_start
from backend.web.core import remote as _remote
from backend.web.core import stage_reset as _stage_reset
from backend.web.core import pending as _pending
from backend.web.core import prompt_queue as _prompt_queue
from backend.web.core import ntfy as _ntfy
from backend.web.core import test_plans as _test_plans
from backend.web.core import window_refresh as _window_refresh
from backend.web.core import worktree_setup as _wt_setup
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
from backend.web.core.workspaces import (
    _base_clone_references,
    _classify_workspace,
    _dir_size_bytes,
    _find_worktrees,
    _remove_worktree_path,
    _strictly_under,
    _workspace_roots,
    _worktree_in_use_by_other,
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
from backend.web.addons import AppContext as _AddonContext, register_addons

# --------------------------------------------------------------------------- #
# Engine bootstrap
# --------------------------------------------------------------------------- #
log.Initialize(False)

_STATIC = Path(__file__).parent / "static"


def _run_capped(args, *, timeout, **kw):
    """``subprocess.run`` with a hard timeout that never raises on expiry.

    A hung child is killed and reported as a *failed* ``CompletedProcess``
    (returncode 124, the timeout note appended to stdout/stderr), so every
    call site's existing non-zero-exit handling applies unchanged.
    """
    try:
        return subprocess.run(args, timeout=timeout, **kw)
    except subprocess.TimeoutExpired as err:
        msg = "timed out after %gs: %s" % (timeout, " ".join(str(a) for a in args))
        text = bool(kw.get("text") or kw.get("universal_newlines"))

        def _coerce(v):
            if v is None:
                return msg if text else msg.encode("utf-8")
            if isinstance(v, bytes):
                return (
                    (v.decode("utf-8", "replace") + "\n" + msg)
                    if text
                    else v + b"\n" + msg.encode("utf-8")
                )
            return v + "\n" + msg

        return subprocess.CompletedProcess(
            args, 124, stdout=_coerce(err.output), stderr=_coerce(err.stderr)
        )


# The Personal Assistant (constants, seed, session, todos REST + chat ws) is now
# the Assistant addon (backend/web/addons/assistant.py). The MindFlock pipeline control
# is the MindFlock addon (backend/web/addons/ticket_ingestion.py). Both self-register via the
# addon registry below.


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
    # Adopt loop: pull in instances written by other processes (TUI/pipeline).
    _register_task(_reload_loop())
    # Instances tick: the side effects that used to piggyback on the browser's
    # GET /api/instances poll (budget-crossing notifications, auto check-run
    # kicks, *_changed events, the addon sessions snapshot). Always-on so they
    # keep firing with zero clients connected; the GET is now read-only.
    _register_task(_instances_tick_loop())
    # Cursor auto-adopt: always runs; checks its runtime flag each tick.
    _register_task(_cursor_autoadopt_loop())
    # Prompt-queue drain: feed queued prompts to idle agents (keeps runs going
    # unattended and resumes them the moment usage returns after an outage).
    _register_task(_prompt_queue_drain_loop())
    _register_task(_autopilot_loop())
    # So the autopilot driver, which runs its blocking half in worker threads, can
    # still start an edge watcher (asyncio.create_task needs the loop).
    _live_stage.set_loop(asyncio.get_running_loop())
    _register_task(_window_refresh_loop())
    # Verify: watch origin until each generated test plan's work reaches the
    # live branch, and poll the verify sessions that are working through one.
    _register_task(_test_plans_due_loop())
    # Tailnet device discovery + remote session snapshots (multi-device mode).
    _register_task(_remote.discovery_loop(_server_port()))
    _register_task(_remote.instances_loop())

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
        # Pasted screenshots are transient: every restart clears them all (each
        # new paste also prunes its directory to the newest _PASTE_KEEP).
        try:
            await asyncio.to_thread(_clear_all_pastes)
        except Exception:  # noqa: BLE001
            pass
        # Mobile/Tailscale banner (probes shell out -> keep off the event loop).
        # stdout gets the full banner (token + QR); the log file gets the
        # redacted copy — mindflock.log is served back out via GET /api/logs, so
        # the access token (or a QR encoding it) must never land there.
        try:
            banner = await asyncio.to_thread(_mobile_banner)
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
        # Close the remote-proxy HTTP session (owned by backend.web.core.remote).
        try:
            await _remote.shutdown()
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


@app.post("/api/auth")
def auth_login(payload: dict) -> JSONResponse:
    """Validate a token and set the ``mf_auth`` cookie (login-page target).

    Always allowed through the gate (public path). Returns 401 on a bad token
    so the login page can show an error; never echoes the token back."""
    token = str((payload or {}).get("token", "") or "").strip()
    if not _auth.token_valid(token):
        return JSONResponse({"error": "invalid token"}, status_code=401)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        key=_auth.COOKIE_NAME,
        value=_auth.get_token(),
        httponly=True,
        samesite="lax",
        path="/",
        max_age=60 * 60 * 24 * 365,
    )
    return resp


# Engine + the adopt/reload loop moved to core.engine. ENGINE stays a module
# global (the process-wide singleton) so the ~50 route call-sites are unchanged.
ENGINE = get_engine()


# TicketIngestionController moved to the Ticket Ingestion addon (backend.web.addons.ticket_ingestion).


# _sync_external_instances + _reload_loop moved to core.engine (imported above).
# The reload loop is started by the lifespan handler.


# --------------------------------------------------------------------------- #
# Startup banner: print the mobile (/m) URL(s) to reach this server.
# --------------------------------------------------------------------------- #
# _server_port / _tailscale_info / _tailscale_serves_port / _qr_lines /
# _local_only_mode / _mobile_banner moved to core.mobile_access (imported above).
# The mobile/Tailscale banner is now printed by the lifespan handler.


# _repo_name / _folder_label / _DIFF_STAT_CACHE / _session_fork_point /
# _parse_shortstat / _session_diff_stat / _instance_json moved to
# core.snapshot (imported above).


# --------------------------------------------------------------------------- #
# REST API
# --------------------------------------------------------------------------- #
# Last status/activity/stage reported per title, so the poll handler and the
# /api/events state tick can emit session.*_changed events (B1/F6) by diffing.
# The lock makes diff+update atomic: the HTTP poll (worker thread) and the tick
# (to_thread) computing the same transition concurrently emit it exactly once —
# the shared snapshot IS the dedupe.
_EVENT_SNAPSHOT: Dict[str, dict] = {}
_EVENT_SNAPSHOT_LOCK = threading.Lock()

# Activity readings flap: one poll can misread a busy pane as "idle" (or
# "clarify"/"limit") and the next reads "working" again. The UI debounces its
# own *display* across 2 polls (frontend lib/stage.ts noteActivity), but events
# used to carry every raw diff — straight into ntfy pushes, desktop
# notifications, clarify toasts and shell hooks, and past the UI debounce via
# forceActivity ("authoritative push"). So transitions INTO these states are
# held until the reading has persisted this long; a flicker that reverts before
# then emits nothing at all. Transitions OUT (to "working"/"offline") stay
# instant — they are cheap to show and wrong to delay. The window is chosen
# > _PROBE_TTL (2.5s) so settling always requires a second INDEPENDENT probe,
# not the same memoized misread served twice; with the ~4s tick cadence a real
# stop settles one tick (~4s) later than it used to.
_SETTLE_ACTIVITIES = frozenset({"idle", "clarify", "limit"})
_ACTIVITY_SETTLE_SECONDS = 3.0

# Boot quiet window: for this long after process start (a restart re-execs, so
# this is also "after every restart") the *_changed diff events are swallowed —
# the snapshot still updates, so post-window transitions diff against the truth.
# Rationale: rediscovered sessions first register as loading/offline and then
# "transition" to whatever state they were parked in before the restart —
# clarify, idle, limit — which re-announced the standing state of every session
# to every channel (ntfy, desktop, toasts, shell hooks) on every launch. Old
# news is not a transition. The UI never depended on these boot events (it
# polls /api/instances); a REAL transition inside the window is lost, which is
# the accepted cost of a quiet launch.
_BOOT_QUIET_SECONDS = 30.0
_BOOT_MONO = time.monotonic()


def _in_boot_quiet() -> bool:
    """Whether the process is still inside its post-launch quiet window."""
    return time.monotonic() - _BOOT_MONO < _BOOT_QUIET_SECONDS


def _emit_state_changes(title: str, status: str, activity: str, stage: str) -> None:
    """Diff a session's freshly computed state against the last snapshot and
    emit ``session.status/activity/stage_changed`` events on the bus. The first
    sighting only seeds the snapshot (creation is announced by its endpoint;
    created sessions are pre-seeded so their first real transition emits, F6).

    Activity transitions into :data:`_SETTLE_ACTIVITIES` are debounced: the
    candidate value parks in the snapshot's ``pending_activity`` (value,
    first-seen monotonic ts) while the snapshot keeps announcing the old
    activity, and only a reading that persists ``_ACTIVITY_SETTLE_SECONDS``
    emits. A flicker that reverts first is dropped without a trace — the whole
    point: a one-tick "idle" misread must not push a phone notification."""
    now = time.monotonic()
    with _EVENT_SNAPSHOT_LOCK:
        prev = _EVENT_SNAPSHOT.get(title)
        snap = {
            "status": status,
            "activity": activity,
            "stage": stage,
        }
        if (
            prev is not None
            and activity != prev.get("activity")
            and activity in _SETTLE_ACTIVITIES
        ):
            pending = prev.get("pending_activity")
            if pending is None or pending[0] != activity:
                # New candidate (or the candidate changed): start settling.
                snap["activity"] = prev.get("activity")
                snap["pending_activity"] = (activity, now)
            elif now - pending[1] < _ACTIVITY_SETTLE_SECONDS:
                snap["activity"] = prev.get("activity")
                snap["pending_activity"] = pending
            # else: settled — snap keeps the new activity and the diff below
            # emits the transition.
        _EVENT_SNAPSHOT[title] = snap
    if prev is None:
        return
    if not _in_boot_quiet():
        for field, event, new in (
            ("status", "session.status_changed", status),
            ("activity", "session.activity_changed", snap["activity"]),
            ("stage", "session.stage_changed", stage),
        ):
            old = prev.get(field)
            if old != new:
                _events.BUS.emit(event, session=title, old=old, new=new)
    # Verify — the SECOND of two triggers that write a test plan for freshly
    # pushed work. The first is the ``session.pushed`` subscriber below, fed by
    # the live_stage push watcher, which is the better signal (it knows the sha
    # actually reached origin) but is not a guarantee: watchers cap at
    # ``live_stage._MAX_WATCHERS`` (4) and expire after 180s, so a flock pushing
    # five sessions at once, or a push whose hooks run past the deadline, simply
    # never announces. The stage ladder has neither limit — it is recomputed for
    # every session on every tick — so it catches what the watcher drops.
    # Deliberately redundant, and safe precisely because it is:
    # ``test_plans.ensure_plan_for`` is idempotent per (session, branch), so the
    # loser of the race does no work at all. Cheap enough to be unconditional:
    # this fires on a stage TRANSITION, not on every tick.
    if prev.get("stage") != stage and stage == "pushed":
        _ensure_test_plan(title)


def _seed_event_snapshot(title: str) -> None:
    """Seed the diff snapshot with a freshly created session's initial state
    (it registers as Loading before Start runs in the background), so the FIRST
    real transition (loading->running, offline->working, …) emits instead of
    being swallowed by first-sighting seeding above (F6)."""
    with _EVENT_SNAPSHOT_LOCK:
        _EVENT_SNAPSHOT.setdefault(
            title,
            {"status": "loading", "activity": "offline", "stage": "provisioning"},
        )


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
    while title in ENGINE.instances:
        title = "untitled-%d" % n
        n += 1
    return title


def _drop_failed_start(title: str, inst) -> bool:
    """Clear the registry entry a failed background ``Start`` left behind, and
    say whether this failure is ours to report.

    BY IDENTITY, NEVER BY NAME, and the difference is a running agent. Every
    creator here registers the instance as ``Loading`` and starts it on a
    background task, so by the time that task fails the title may belong to a
    DIFFERENT, live session — a fast delete-and-retry, a second force-start
    after the row was closed, two creates racing through one title. Popping by
    name then deleted the live session's record: its tmux session and its
    worktree carried on with nothing owning them (invisible in the rail, absent
    from ``ENGINE.save``), which is exactly how the orphan that blocks the next
    run of the same name is minted. Reporting by name is the same mistake one
    layer up — a "couldn't start" event, or a checklist stamped with a failure,
    about a session that is working.

    Returns True when nothing else claims the title (we popped our own entry, or
    it was already gone) — say so. False means a live session owns it: stay
    quiet.
    """
    with ENGINE.lock:
        current = ENGINE.instances.get(title)
        if current is inst:
            ENGINE.instances.pop(title, None)
            return True
        return current is None


def _forget_probes(title: str) -> None:
    """Drop every memoized probe result for one session (kill/delete paths),
    so a session recreated under the same title starts from fresh probes —
    and so the per-title rolling state doesn't accumulate dead entries for
    the server's lifetime under session churn."""
    with _PROBE_CACHE_LOCK:
        for k in [k for k in _PROBE_CACHE if k[1] == title]:
            _PROBE_CACHE.pop(k, None)
    for _d in (_ACTIVITY_CACHE, _LIMIT_PROBE, _THREAD_RECORD_AT, _TRUST_DISMISS_AT):
        _d.pop(title, None)
    _forget_tokens(title)


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


def _session_last_turn_cached(inst) -> Optional[str]:
    """``_session_last_turn(inst)`` memoized per session (last-turn timestamp)."""
    return _probe_cached("last_turn", inst, lambda: _session_last_turn(inst))


def _session_last_prompt_cached(inst) -> Optional[str]:
    """``_session_last_prompt(inst)`` memoized per session."""
    return _probe_cached("last_prompt", inst, lambda: _session_last_prompt(inst))


def _session_last_prompt_full_cached(inst) -> Optional[str]:
    """``_session_last_prompt_full(inst)`` memoized per session."""
    return _probe_cached(
        "last_prompt_full", inst, lambda: _session_last_prompt_full(inst)
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


# ---- Prompt-queue drain loop (M-series) ----------------------------------- #
# Feeds each session's queued prompts into its agent whenever it is idle, so a
# run keeps rolling unattended — and picks straight back up the moment usage
# returns after an outage. Runs always (not gated on browser clients): draining
# with nobody watching is the entire point.
#
# Per-session gating avoids double-sending and reboot storms:
#   * send only when activity is "idle" (finished a turn / at its prompt) and
#     the session is "armed" — armed starts True, clears on send, and re-arms
#     when we next observe "working" (the agent picked the prompt up). This
#     tracks the real working→idle cycle instead of trusting a fixed cooldown.
#   * "working"/"clarify" are left alone (busy, or waiting on a human decision).
#   * "offline" for a started, non-paused session means the agent tmux died
#     (e.g. the CLI exited when usage ran out) — reboot it (resuming the
#     conversation), rate-limited by _QUEUE_REBOOT_COOLDOWN, and let a later
#     idle tick do the actual send.
_QUEUE_DRAIN_INTERVAL = 5.0
_QUEUE_SEND_COOLDOWN = 8.0  # min seconds between sends to one session
_QUEUE_REARM_IDLE = 300.0  # idle this long after a send -> the send never took; re-arm
_QUEUE_REBOOT_COOLDOWN = 30.0  # min seconds between reboots of a dead session
# Seconds "idle" must PERSIST before the first send. The activity signal (Claude's
# `claude agents --json` / Stop hook, Codex's Stop hook) flips to idle the instant a
# turn ends and is trusted at any age (no dwell in _agent_activity) — so a momentary
# Stop between turns, or the ~1s lull before the next UserPromptSubmit/PreToolUse
# re-marks "working", would otherwise let the drain fire a queued prompt prematurely.
# Requiring idle to hold this long absorbs that flicker while still resuming promptly.
_QUEUE_IDLE_SETTLE = 12.0
_QUEUE_STATE: Dict[str, dict] = (
    {}
)  # title -> {"armed", "sent_at", "rebooted_at", "idle_since"}

# Usage-limit gate (roadmap D): when the agent pane shows a "usage limit reached"
# screen, hold the queue until the limit resets instead of sending into a wall.
# Bounded + self-correcting: a detected limit sets an expiry (the parsed reset
# time, or a fallback), so it can NEVER stall the queue forever.
_LIMIT_STATE: Dict[str, float] = {}  # title -> epoch the limit is known to hold until
_LIMIT_FALLBACK = 600.0  # no reset time parsed -> retry after 10 min
_LIMIT_EXHAUSTED_PCT = 99.0  # live-meter %-used at/above which a window counts as spent


def _meter_is_about_this_session(inst) -> bool:
    """Whether the provider's usage meter describes the identity THIS session
    runs as.

    ``usage_live()`` reads whatever credentials the server process itself sees
    — the CLI's ambient login. A session pinned to an auth profile is metered
    somewhere else entirely, so the ambient reading is a different
    subscription's: trusting it would release a genuinely limited session early
    (burning the queued prompt on the limit screen) or hold a session whose own
    account still has headroom. Detection from the session's own pane text is
    unaffected and remains the gate for profiled sessions.
    """
    try:
        from backend.providers import auth_profiles

        return not auth_profiles.effective_profile_id(
            getattr(inst, "ProfileId", "") or ""
        )
    except Exception:  # noqa: BLE001 — never break the drain over settings
        return True


def _live_limit_reset(provider, now: float, inst=None):
    """Authoritative "limited until" from the provider's own usage meter
    (``usage_live()`` — for Claude that's Anthropic's OAuth usage endpoint, the
    same source as the CLI's ``/usage`` screen), independent of whatever text
    happens to be on the pane.

    Returns ``None`` when no live reading is available (callers fall back to
    the pane text), ``0.0`` when the meter says every window still has
    headroom (a lingering banner must be stale), or the epoch when the LAST
    exhausted window reopens — both the 5-hour and the weekly cap must have
    reset before a send can land.

    ``inst``, when given, scopes the reading: the meter is the *ambient*
    login's, so for a session running under an auth profile there is no live
    reading to be had and ``None`` is the honest answer (see
    :func:`_meter_is_about_this_session`)."""
    if inst is not None and not _meter_is_about_this_session(inst):
        return None
    try:
        live = provider.usage_live()
    except Exception:  # noqa: BLE001 — live usage is enrichment, never a gate
        live = None
    if not isinstance(live, dict) or not live:
        return None
    windows = [live]
    if isinstance(live.get("weekly"), dict):
        windows.append(live["weekly"])
    have_reading = False
    exhausted = False
    ends = []
    for w in windows:
        try:
            pct = float(w["percent_used"])
        except (KeyError, TypeError, ValueError):
            continue
        have_reading = True
        try:
            end = float(w.get("end") or 0.0)
        except (TypeError, ValueError):
            end = 0.0
        if pct >= _LIMIT_EXHAUSTED_PCT:
            exhausted = True
            if end > now:
                ends.append(end)
    if ends:
        return max(ends)
    if exhausted:
        # A window reads spent but the meter gave no usable reset time
        # (``resets_at`` can be null right at exhaustion, or in a between-windows
        # payload — the same transient the fetch layer already tolerates). That is
        # NOT proof the window is open: returning 0.0 here would read as "meter
        # confirms open" and send the queued prompt straight into the wall. Hold on
        # a bounded fallback instead; the active-hold meter check releases it the
        # instant real headroom reappears, so an over-long hold self-corrects while
        # an early send eats a prompt.
        return now + _LIMIT_FALLBACK
    return 0.0 if have_reading else None


def _session_limited_until(title: str) -> float:
    """Epoch until which ``title`` is known usage-limited (0.0 = not). Read-only
    (no subprocess) so the /api/instances poll can surface a countdown cheaply.

    Non-mutating on purpose: an expired hold reports 0 (UI countdown ends) but
    the entry is LEFT for the drain's :func:`_refresh_limit_state` to see, so it
    can tell "hold I set has expired" (window reopened) apart from "brand-new
    limit" — the difference that lets the queue actually resume when the window
    reopens even if the CLI's limit banner is still on screen."""
    until = _LIMIT_STATE.get(title, 0.0)
    if until and time.time() >= until:
        return 0.0
    return until


def _refresh_limit_state(inst, title: str, name: str) -> float:
    """Read the agent pane and update/return the limited-until epoch (0 = clear).

    Only called from the drain loop right before a would-be send, so the pane
    capture happens solely when we're actually about to act."""
    now = time.time()
    try:
        cp = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", name],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        text = cp.stdout.decode("utf-8", "replace") if cp.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        text = ""
    provider = providers.resolve(getattr(inst, "Program", "") or "")
    try:
        state = provider.usage_limit_state(text, now)
    except Exception:  # noqa: BLE001 — detection must never break the drain
        state = {"limited": False, "reset_at": None}
    prev = _LIMIT_STATE.get(title, 0.0)
    if state.get("limited"):
        # An active hold for THIS limit episode is kept stable — never recompute
        # it from the pane. A relative "resets in 2h 30m" re-parses to now+2h30m
        # every 5s pass; recomputing would slide the deadline forward forever and
        # the queue would never send after the window reopened. The provider's
        # own meter may still END it early: if it now shows headroom, the window
        # reopened before our estimate did — release the queue.
        if prev > now:
            if _live_limit_reset(provider, now, inst) == 0.0:
                _LIMIT_STATE.pop(title, None)
                return 0.0
            return prev
        # A hold we set has already EXPIRED, yet the limit banner is still on
        # the pane (CLIs often leave the message up after the window reopens).
        # Ask the meter first: still spent -> re-arm to ITS reset instead of
        # burning a prompt on a send that lands on the limit screen. No meter,
        # or meter says open: treat the window as open so the queue sends now —
        # if the limit really persists a fresh detection re-arms on the next
        # pass (bounded retry), far better than holding forever on stale text.
        if prev:
            live = _live_limit_reset(provider, now, inst)
            if live and live > now:
                _LIMIT_STATE[title] = live
                return live
            _LIMIT_STATE.pop(title, None)
            return 0.0
        # Fresh detection (no prior hold): hold until the limit actually lifts.
        # The meter and the pane text usually agree; with both present take the
        # LATER one — sending early eats the queued prompt (a dud), while an
        # over-long hold self-corrects via the meter check above. A meter that
        # reads "open" is NOT trusted to skip arming: right after a limit hits,
        # the ~60s-cached reading can lag the banner.
        reset_at = state.get("reset_at")
        try:
            parsed = float(reset_at) if reset_at is not None else None
        except (TypeError, ValueError):
            parsed = None
        live = _live_limit_reset(provider, now, inst)
        candidates = [c for c in (parsed, live) if c and c > now]
        if candidates:
            until = max(candidates)
        elif parsed is not None:
            # Reset time already in the past (stale banner) -> window is open.
            _LIMIT_STATE.pop(title, None)
            return 0.0
        else:
            until = now + _LIMIT_FALLBACK
        _LIMIT_STATE[title] = until
        return until
    # No limit BANNER on screen — but the pane text alone isn't proof the window
    # is open. A session that runs out mid-turn usually exits and is rebooted
    # (drain's offline path) back to a FRESH idle prompt with no banner, and many
    # CLIs only reprint the limit after a submit. So consult the provider's own
    # meter, which is independent of whatever text is on the pane.
    if prev and now < prev:
        # Active hold: release early only if the meter says the window reopened.
        if _live_limit_reset(provider, now, inst) == 0.0:
            _LIMIT_STATE.pop(title, None)
            return 0.0
        return prev
    # No prior hold: arm one straight from the meter if a window is genuinely
    # exhausted (>= _LIMIT_EXHAUSTED_PCT used, reset still ahead), so the queue
    # holds instead of burning the queued prompt on a send that lands on the wall
    # while no banner happens to be visible. A meter that reads open — or is
    # unavailable (None) — leaves the queue free to send, exactly as before.
    live = _live_limit_reset(provider, now, inst)
    if live and live > now:
        _LIMIT_STATE[title] = live
        return live
    _LIMIT_STATE.pop(title, None)
    return 0.0


def _send_queued_item(title: str, name: str, nxt: dict, rec: dict, now: float) -> bool:
    """Send one queued item to the agent and record it: pop/requeue in the
    store, disarm + stamp the drain record, and emit ``session.prompt_sent``.
    Shared by the idle send path and the usage-limit auto-resume path. Returns
    whether the send landed (False leaves the item in place to retry)."""
    if not _send_to_agent(name, nxt["text"], submit=True):
        return False
    entry = _prompt_queue.record_sent(title, nxt["id"])
    rec["armed"] = False
    rec["sent_at"] = now
    _events.BUS.emit(
        "session.prompt_sent",
        session=title,
        data={
            "text": nxt["text"][:200],
            "remaining": len(entry["items"]),
            "loop": entry["loop"],
        },
    )
    if log.ErrorLog is not None:
        try:
            log.ErrorLog.Printf(
                "[MONITORING] queue drained a prompt to %s (%d remaining)",
                title,
                len(entry["items"]),
            )
        except Exception:  # noqa: BLE001
            pass
    return True


def _drain_one_queue(title: str) -> None:
    """One drain decision for a single session. Never raises."""
    st = _prompt_queue.get_state(title)
    if not st["enabled"] or not st["items"]:
        return
    inst = ENGINE.instances.get(title)
    if inst is None:
        return
    try:
        if not inst.Started() or inst.Status == session.Paused:
            return
    except Exception:  # noqa: BLE001
        return
    if _budget_locked(title):
        return  # over budget — hold the queue until the user raises it
    # O2: hold queued prompts while the worktree's setup pass is running or
    # failed — the deps the prompt needs aren't there yet (the held initial
    # prompt from create lands here too, so it can't get lost: #2847).
    try:
        _wt = inst.GetWorktreePath()
    except Exception:  # noqa: BLE001
        _wt = ""
    if _wt:
        _setup_st = _wt_setup.setup_status(_wt)
        if _setup_st and _setup_st.get("state") in ("running", "failed"):
            return
    rec = _QUEUE_STATE.setdefault(
        title,
        {"armed": True, "sent_at": 0.0, "rebooted_at": 0.0, "idle_since": None},
    )
    now = time.time()
    # Deliberately UNCACHED: a memoized "idle" from before the last send could
    # double-feed a prompt inside the memo window. The probes are per queued
    # session only, on the 5s drain cadence.
    activity = _agent_activity(inst, title)
    if activity == "working":
        rec["armed"] = True  # agent picked the last prompt up; ok to send again
        rec["idle_since"] = None  # not parked — reset the idle-dwell timer
        return
    if activity == "clarify":
        rec["idle_since"] = None
        return  # waiting on a human — never override a confirmation prompt
    if activity == "limit":
        # A usage-limit banner/menu is on screen. Unlike 'clarify' (a human gate
        # we never override) the queue should ride the window out and resume on
        # its own: hold while the limit is closed, and the moment it reopens send
        # a single Esc to drop the lingering limit menu — the CLI leaves it up
        # until a key is pressed — then submit the next prompt so it lands on a
        # clean input instead of selecting a menu entry.
        rec["idle_since"] = None
        if not st.get("wait_for_limit", True):
            # Not waiting it out: stop the queue so it sits until the user
            # resumes by hand (mirrors the idle-path limit gate).
            if st["enabled"]:
                _prompt_queue.set_flags(title, enabled=False)
                _emit_queue_changed(title)
            return
        name, err = _ensure_agent_session(inst, title)
        if err is not None:
            return
        if _refresh_limit_state(inst, title, name) > now:
            return  # still limited — hold; the UI shows the reset countdown
        # Window reopened (parsed reset passed / live meter shows headroom) but
        # activity=='limit' means the menu is still on the pane this very tick.
        # Gate the escape+send on ``armed`` exactly like the idle path: send at
        # most once, then wait for the agent to actually pick the prompt up (it
        # transitions to 'working', which re-arms) before sending again. Without
        # this a menu that doesn't clear on the first Esc — or a live meter that
        # lags the real reset — would let every 5s pass pop and burn another
        # queued prompt into the limit screen. A send that never took re-arms
        # after _QUEUE_REARM_IDLE so a stuck menu still gets a bounded retry.
        if (
            not rec.get("armed", True)
            and now - rec.get("sent_at", 0.0) >= _QUEUE_REARM_IDLE
        ):
            rec["armed"] = True
        if (
            not rec.get("armed", True)
            or now - rec.get("sent_at", 0.0) < _QUEUE_SEND_COOLDOWN
        ):
            return
        nxt = _prompt_queue.peek_next(title)
        if not nxt:
            return
        _send_escape_to_agent(name)  # single Esc to leave the limit menu
        time.sleep(0.15)  # let the CLI redraw its prompt before we type
        _send_queued_item(title, name, nxt, rec, now)
        return
    if activity == "offline":
        # Agent tmux is gone though the session is live — reboot (resume) it,
        # rate-limited, and send on a later idle tick.
        rec["idle_since"] = None
        if now - rec.get("rebooted_at", 0.0) < _QUEUE_REBOOT_COOLDOWN:
            return
        name, err = _ensure_agent_session(inst, title)
        if err is None:
            rec["rebooted_at"] = now
            rec["armed"] = True
        return
    # activity == "idle": the CLI's idle signal (Claude `agents --json` / Stop hook,
    # Codex Stop hook) flips the instant a turn ends and _agent_activity trusts it at
    # any age — so require idle to PERSIST for _QUEUE_IDLE_SETTLE before the first send.
    # This absorbs a transient Stop between turns / the brief lull before the next
    # working marker, which otherwise let the drain fire a prompt prematurely.
    if rec.get("idle_since") is None:
        rec["idle_since"] = now
        return
    if now - rec["idle_since"] < _QUEUE_IDLE_SETTLE:
        return
    # A send that never started a turn (it landed while the CLI sat on a
    # usage-limit screen, or the turn finished between two 5s polls) leaves
    # ``armed`` False with no "working" transition ever coming — which would
    # stall the queue forever. An agent still idle this long after our send
    # clearly isn't going to pick it up: re-arm so the next pass (behind the
    # usage-limit gate below) can retry.
    if (
        not rec.get("armed", True)
        and now - rec.get("sent_at", 0.0) >= _QUEUE_REARM_IDLE
    ):
        rec["armed"] = True
    # send the next prompt if armed + past the send cooldown.
    if (
        not rec.get("armed", True)
        or now - rec.get("sent_at", 0.0) < _QUEUE_SEND_COOLDOWN
    ):
        return
    # Loop timer: with loop on and an interval set, only send every N minutes
    # (a timed self-improving cycle). The first send is immediate (last_sent 0).
    interval = int(st.get("loop_interval") or 0)
    if st["loop"] and interval > 0:
        last = st.get("last_sent") or 0.0
        if last and now - last < interval * 60:
            return
    nxt = _prompt_queue.peek_next(title)
    if not nxt:
        return
    name, err = _ensure_agent_session(inst, title)
    if err is not None:
        return
    # Usage-limit gate: if the pane shows a limit that hasn't reset yet, either
    # hold (wait_for_limit on — the UI shows the countdown and a later pass sends
    # once the window reopens) or stop the queue (wait_for_limit off — flip
    # auto-run off so it sits until the user resumes it by hand).
    if _refresh_limit_state(inst, title, name) > now:
        if not st.get("wait_for_limit", True) and st["enabled"]:
            _prompt_queue.set_flags(title, enabled=False)
            _emit_queue_changed(title)
        return
    _send_queued_item(title, name, nxt, rec, now)


# --- Usage-limit watch: sessions parked on the limit screen with no queue --- #
# The drain above resumes a limited session that has something QUEUED. A session
# that simply ran out mid-task has an empty queue, so nothing was watching it —
# it sat on the limit screen until a human came back hours later, long after the
# window reopened. This watcher covers exactly that gap, and pays for itself:
# the candidate list comes from the snapshot the /api/events tick already keeps
# (a dict read), so a flock with nothing limited does no work at all.
_LIMIT_RESUME_PROMPT = "continue"

#: "Usage is back" is one fact about the account, not one per session — several
#: sessions typically unblock in the same pass. Announce it once per window.
_LIMIT_RESTORE_QUIET = 300.0
_LAST_RESTORE_EMIT = 0.0


def _resume_on_usage_reset() -> bool:
    """Whether to nudge a limited session once its window reopens
    (``general.resume_on_usage_reset``; unset = on). Never raises."""
    try:
        from backend.config import settings as _settings

        return _settings.load_settings().general.resume_on_usage_reset is not False
    except Exception:  # noqa: BLE001 — an unreadable store keeps the default
        return True


def _limited_titles() -> list:
    """Sessions whose last observed activity was ``limit``, from the event
    snapshot — free (no probes), and refreshed every ~4s by the state tick."""
    with _EVENT_SNAPSHOT_LOCK:
        return [
            t for t, snap in _EVENT_SNAPSHOT.items() if snap.get("activity") == "limit"
        ]


def _watch_one_limited(title: str) -> None:
    """One usage-limit decision for a session sitting on the limit screen.

    Runs only while the session's *observed* activity is ``limit``, so the pane
    capture in :func:`_refresh_limit_state` happens for genuinely stuck sessions
    only. Never raises."""
    global _LAST_RESTORE_EMIT
    st = _prompt_queue.get_state(title)
    if st["enabled"] and st["items"]:
        return  # the drain owns this one — it has a prompt to send on reopening
    inst = ENGINE.instances.get(title)
    if inst is None:
        return
    try:
        if not inst.Started() or inst.Status == session.Paused:
            return
    except Exception:  # noqa: BLE001
        return
    name, err = _ensure_agent_session(inst, title)
    if err is not None:
        return
    now = time.time()
    if _refresh_limit_state(inst, title, name) > now:
        return  # still limited — the UI shows the countdown
    # The window reopened. Reuse the drain's per-session send bookkeeping so the
    # nudge obeys the same "once, then wait to see it picked up" rule: an Esc +
    # prompt that doesn't take (a menu that needs a second key, a meter that
    # lags the real reset) retries after _QUEUE_REARM_IDLE instead of every 5s.
    rec = _QUEUE_STATE.setdefault(
        title,
        {"armed": True, "sent_at": 0.0, "rebooted_at": 0.0, "idle_since": None},
    )
    if (
        not rec.get("armed", True)
        and now - rec.get("sent_at", 0.0) >= _QUEUE_REARM_IDLE
    ):
        rec["armed"] = True
    if (
        not rec.get("armed", True)
        or now - rec.get("sent_at", 0.0) < _QUEUE_SEND_COOLDOWN
    ):
        return
    resume = _resume_on_usage_reset()
    if resume:
        _send_escape_to_agent(name)  # drop the lingering limit menu
        time.sleep(0.15)  # let the CLI redraw its prompt before we type
        if _send_to_agent(name, _LIMIT_RESUME_PROMPT, submit=True):
            rec["armed"] = False
            rec["sent_at"] = now
            if log.ErrorLog is not None:
                try:
                    log.ErrorLog.Printf(
                        "[MONITORING] usage window reopened — resumed %s", title
                    )
                except Exception:  # noqa: BLE001
                    pass
        else:
            return  # tmux gone: no resume, and no "usage is back" to announce
    # Announce the reopening whether or not we acted on it — "your usage is
    # back" is worth knowing even with auto-resume off, and it can only fire for
    # a session that had actually run out (that is what put it on this list).
    if now - _LAST_RESTORE_EMIT >= _LIMIT_RESTORE_QUIET:
        _LAST_RESTORE_EMIT = now
        _events.BUS.emit(
            "session.usage_restored", session=title, data={"resumed": resume}
        )


def _watch_limited_sessions() -> None:
    """One pass over every session parked on a usage-limit screen."""
    for title in _limited_titles():
        try:
            _watch_one_limited(title)
        except Exception:  # noqa: BLE001 — one bad session can't stop the pass
            pass


def _drain_prompt_queues() -> None:
    """One pass over every session with a queue, plus every session parked on a
    usage-limit screen. Runs in a worker thread (it shells out to tmux) so it
    never blocks the event loop."""
    titles = _prompt_queue.all_titles()
    if not titles:
        _watch_limited_sessions()
        return
    _prompt_queue.prune(list(ENGINE.instances.keys()))
    _ports.prune(list(ENGINE.instances.keys()))
    for title in titles:
        try:
            _drain_one_queue(title)
        except Exception:  # noqa: BLE001 — one bad session can't stop the drain
            pass
    _watch_limited_sessions()
    # Forget in-memory drain state for sessions that vanished.
    for gone in [t for t in _QUEUE_STATE if t not in ENGINE.instances]:
        _QUEUE_STATE.pop(gone, None)
    for gone in [t for t in _LIMIT_STATE if t not in ENGINE.instances]:
        _LIMIT_STATE.pop(gone, None)
    # Drop budget overrides for sessions that no longer exist (so a reused title
    # doesn't inherit an old raise).
    for gone in [t for t in _budget_overrides() if t not in ENGINE.instances]:
        _forget_budget(gone)


async def _prompt_queue_drain_loop() -> None:
    """Drive the prompt-queue drain forever (started by the lifespan)."""
    while True:
        try:
            await asyncio.to_thread(_drain_prompt_queues)
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(_QUEUE_DRAIN_INTERVAL)


# --- Autopilot: advance armed sessions toward their target rung -------------- #
# The impure half of backend.web.core.autopilot (the store and the decision
# function live there). Structured exactly like the prompt-queue drain above: a
# 5s pass, one decision per armed session, every step wrapped so a single bad
# session cannot stop the pass.
_AUTOPILOT_INTERVAL = 5.0
#: Floor between two actions on one session, so a stage that has not yet caught
#: up cannot cause a double-push.
_AUTOPILOT_ACTION_COOLDOWN = 15.0
#: Per-step wall clock. Expiry HALTS with a reason — it never silently retries.
_AUTOPILOT_DEADLINES = {
    "agent": 5400.0,  # 90min: an agent working a whole ticket
    "commit": 1800.0,  # 30min: hook stacks can include tests and doc passes
    "check": 1800.0,  # 30min: a verification command may run the whole suite
    # These three were far too tight. A slow remote, a rate-limited API or a
    # required-check queue must read as "still going", not as a failure — the
    # deadline exists to stop a WEDGED chain, not to race the network.
    "push": 900.0,
    "pr": 600.0,
    "merge": 5400.0,  # 90min: the merge rung genuinely waits for CI now
}


def _autopilot_dto(title: str):
    """The session row's autopilot block — see :func:`autopilot.dto`.

    Kept as a thin alias because it is referenced from both snapshot paths and by
    tests; the shaping itself is shared with ``core.pending``.
    """
    return _autopilot.dto(title)


def _fasttrack_depth() -> str:
    """The configured default rung for the fast-track button.

    Read fresh at decision time (never memoized at startup) so changing it in
    Settings takes effect on the next press with no restart — the house rule for
    every settings consumer.
    """
    try:
        from backend.config import settings as _settings

        d = _autopilot.normalize_depth(
            _settings.load_settings().repository.fasttrack_depth
        )
        return d if d in _autopilot.DEPTHS else "pr"
    except Exception:  # noqa: BLE001
        return "pr"


def _precommit_retry_hooks() -> list:
    """Pre-commit hook IDs whose failure the driver may retry, then skip.

    Sourced from settings, never from a client: the value ends up inside a shell
    command, so it is charset-filtered here and anything in
    :data:`autopilot.NEVER_SKIP` (tests, secret scanners) is dropped whatever the
    settings file says.
    """
    try:
        from backend.config import settings as _settings

        raw = _settings.load_settings().repository.precommit_retry_hooks or ""
    except Exception:  # noqa: BLE001
        return []
    out = []
    for part in str(raw).split(","):
        h = part.strip()
        if not h or not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", h):
            continue
        if h in _autopilot.NEVER_SKIP or h in out:
            continue
        out.append(h)
    return out[:8]


def _autopilot_snapshot(inst, title: str, wt: str, stage: dict) -> dict:
    """The observation :func:`autopilot.next_action` decides from.

    Every probe here is deliberately UNCACHED for the same reason the drain reads
    activity uncached: a 2.5s-stale "committed" immediately after a push would
    make the driver push again.
    """
    now = time.time()
    try:
        queue_st = _prompt_queue.get_state(title)
        queue_pending = bool(queue_st.get("enabled") and queue_st.get("items"))
    except Exception:  # noqa: BLE001
        queue_pending = False
    activity = _agent_activity(inst, title)
    # THE MOST IMPORTANT FIELD HERE. "The agent stopped" and "the agent ran out"
    # are the same observation to every cheap probe: a turn the account's usage
    # limit cuts short ends with the CLI's Stop hook, a quiet pane and an idle
    # badge, exactly like a finished one. `_agent_activity` now re-checks the
    # pane before reporting that idle, but the pane is not the whole story —
    # a session that ran out mid-turn can be sitting at a redrawn prompt with no
    # banner in view — so an idle verdict is confirmed against the drain's own
    # limit state, which adds the provider's usage METER (Anthropic's usage
    # endpoint: the weekly cap included, independent of any pane text) and
    # carries a bounded, self-correcting expiry that also feeds the UI countdown.
    # This is what stops the ladder committing and pushing a half-finished
    # session because the weekly window closed under it.
    #
    # Only on idle: a working session is not about to be committed, and the
    # confirmation costs a tmux capture. `_refresh_limit_state` takes the tmux
    # name directly, so there is no `_ensure_agent_session` reboot side effect.
    limited = activity == "limit"
    if not limited and activity == "idle":
        try:
            limited = (
                _refresh_limit_state(inst, title, tmux.to_mindflock_tmux_name(title))
                > now
            )
        except Exception:  # noqa: BLE001 — detection must never break a pass
            limited = False
    # None means "could not measure", which must never be read as "nothing has
    # been committed" — that mistake produced a false "the agent finished without
    # changing anything" on sessions whose work was already committed and pushed.
    beyond = None
    base = ""
    live_branch = ""
    try:
        base = _session_base_branch(inst) or ""
        if base:
            beyond = _commits_beyond_base(wt, base)
    except Exception:  # noqa: BLE001
        beyond = None
    try:
        live_branch = _current_branch(wt) or ""
    except Exception:  # noqa: BLE001
        live_branch = ""
    # Sitting ON the base branch means there is no feature branch: pushing would
    # push the trunk and a PR has nothing to target. Compared case-sensitively
    # against the resolved base, and only when BOTH are known — an unknown base
    # must not be guessed into a refusal.
    on_base = bool(base and live_branch and base == live_branch)
    try:
        check = _wt_setup.check_summary(wt)
    except Exception:  # noqa: BLE001
        check = None
    # Whether this repo GATES the push on a verification run. The push route 409s
    # when a declared check has not passed, so the ladder has to know about the
    # gate up front rather than discovering it as a fatal error.
    try:
        check_required = bool(_wt_setup.load_config(wt).check_command)
    except Exception:  # noqa: BLE001
        check_required = False
    # CI verdict on the PR, only when a merge is actually the target — it is a
    # network round trip and every other rung is indifferent to it.
    # CI verdict + blockers on the PR, only when a merge is actually the target.
    # Reuses the same probe the UI's merge button reads, so the driver and the
    # button can never disagree about whether a merge would go through.
    pr_checks = ""
    merge_blockers: list = []
    try:
        rec = _autopilot.get(title) or {}
        if rec.get("depth") == "merge" and (stage.get("stage") or "") == "pr":
            branch = _current_branch(wt) or ""
            ms = _pr_merge_state(wt, branch) if branch else None
            if ms is None:
                pr_checks = "unknown"
            else:
                pr_checks = str(ms.get("checks") or "unknown")
                if not ms.get("can_merge"):
                    merge_blockers = list(ms.get("blockers") or ["cannot merge yet"])
    except Exception:  # noqa: BLE001
        pr_checks = "unknown"
    return {
        "stage": stage.get("stage") or "",
        "failed_step": stage.get("failed_step") or "",
        "failed_hook": stage.get("failed_hook") or "",
        "dirty": _is_dirty(wt),
        "beyond_base": beyond,
        "activity": activity,
        "limited": limited,
        "queue_pending": queue_pending,
        "check": check,
        "check_required": check_required,
        "pr_checks": pr_checks,
        "merge_blockers": merge_blockers,
        "on_base_branch": on_base,
        "branch": live_branch,
        "has_origin": _has_origin(wt),
        "now": now,
    }


def _autopilot_observe(title: str):
    """Gather everything a decision needs, in a worker thread.

    Split from the decision + action so the blocking half (git, tmux, gh) never
    runs on the event loop while the action half can ``await`` the existing route
    coroutines directly. Returns ``(rec, inst, wt, snap)``, or None to skip this
    session on this pass.
    """
    rec = _autopilot.get(title)
    if rec is None:
        return
    if rec.get("state") != "running":
        # Finished or halted: the run has had its go, either way. It turns itself
        # OFF for that window rather than lingering armed — and the record stays so
        # the outcome (and a halt's reason) is still readable. Pressing the button
        # is how you start another one.
        return
    if not _autopilot.normalize_depth(rec.get("depth")) in _autopilot.DEPTHS:
        return
    inst = ENGINE.instances.get(title)
    if inst is None:
        # Not adopted yet — the normal intake case, since arming happens before the
        # session exists. Say so rather than looking wedged.
        _autopilot_note(title, rec, "waiting for the workspace to be created")
        return
    # Every hold below is a WAIT, not a halt — but it must be VISIBLE. These used
    # to be bare `return`s, so a chain held by a paused session, a budget lock or a
    # failed worktree setup looked identical to one that had silently died.
    try:
        if not inst.Started():
            _autopilot_note(title, rec, "waiting for the session to start")
            return
        if inst.Status == session.Paused:
            _autopilot_note(title, rec, "session is paused — resume it to continue")
            return
    except Exception:  # noqa: BLE001
        return
    if _budget_locked(title):
        _autopilot_note(title, rec, "over the cost budget — raise it to continue")
        return
    try:
        wt = inst.GetWorktreePath()
    except Exception:  # noqa: BLE001
        wt = ""
    if not wt:
        _autopilot_note(title, rec, "waiting for the workspace")
        return
    setup_st = _wt_setup.setup_status(wt)
    if setup_st and setup_st.get("state") == "running":
        _autopilot_note(title, rec, "workspace setup is running")
        return
    if setup_st and setup_st.get("state") == "failed":
        _autopilot_halt(title, "workspace setup failed — fix it and re-arm")
        return

    # ONE DRIVER PER CHAIN. Take (or refresh) the lease before deciding anything.
    # Two servers sharing this store — a dev instance on another port, say — both
    # ran the driver: each saw the other's boot id, treated it as a restart, and
    # reset the idle dwell every pass, so the 30s settle could never elapse and a
    # chain sat at "agent just went idle" indefinitely. Both would also have ACTED,
    # double-committing and double-pushing the same worktree. Taking the lease also
    # re-earns the dwell on a genuine handover (a restart, or a crashed owner), so
    # a chain cannot shortcut the "is the agent really done" wait.
    claimed, took_over = _autopilot.claim(title, _SERVER_BOOT_ID, now=time.time())
    if claimed is None:
        return  # another live server owns this chain
    rec = claimed
    if took_over and rec.get("boot") != _SERVER_BOOT_ID:
        rec = _autopilot.update(title, boot=_SERVER_BOOT_ID) or rec

    # Branch drift: the push/PR/merge routes all resolve the LIVE branch, so a
    # switch mid-run would retarget the chain at different work.
    live_branch = _current_branch(wt) or ""
    armed_branch = rec.get("branch") or ""
    acted = int(rec.get("commits") or 0) > 0 or rec.get("step") in (
        "commit",
        "push",
        "pr",
    )
    if armed_branch and not live_branch:
        # Detached HEAD (a rebase, a bisect, `checkout <sha>`). Pushing from here
        # would target something nobody asked for.
        _autopilot_note(
            title, rec, "workspace is on a detached HEAD — check out a branch"
        )
        return
    if armed_branch and live_branch and live_branch != armed_branch:
        if acted:
            _autopilot_halt(
                title,
                "the workspace switched from %s to %s mid-run"
                % (armed_branch, live_branch),
            )
            return
        # Nothing has been done on the armed branch yet, so this is not "drift" —
        # it is the agent creating its working branch, which is the NORMAL intake
        # sequence. Follow it instead of refusing to work.
        rec = (
            _autopilot.update(
                title,
                branch=live_branch,
                note="following the agent onto " + live_branch,
            )
            or rec
        )
    elif not armed_branch and live_branch:
        rec = _autopilot.update(title, branch=live_branch) or rec

    # The retry allowlist is re-read EVERY pass, never frozen at arm time: intake
    # arms hours before the commit step runs, and a hook id added in Settings must
    # take effect on the next pass rather than needing a disarm/re-arm.
    fresh_hooks = _precommit_retry_hooks()
    if list(rec.get("retryable") or []) != fresh_hooks:
        rec = _autopilot.update(title, retryable=fresh_hooks) or rec

    stage = _session_stage(inst)  # uncached on purpose
    return rec, inst, wt, _autopilot_snapshot(inst, title, wt, stage)


async def _autopilot_step(title: str) -> None:
    """One autopilot decision for a single session. Never raises.

    Async so the action half can ``await`` the existing route coroutines; every
    blocking probe happens inside :func:`_autopilot_observe`'s worker thread.
    """
    seen = await asyncio.to_thread(_autopilot_observe, title)
    if seen is None:
        return
    rec, inst, wt, snap = seen
    now = snap["now"]
    action, detail = _autopilot.next_action(rec, snap)

    if action == "wait":
        await asyncio.to_thread(_autopilot_wait, title, rec, snap, detail, now)
        return
    if action == "done":
        await asyncio.to_thread(_autopilot_finish, title)
        return
    if action == "stop":
        await asyncio.to_thread(
            _autopilot_halt, title, detail.get("reason") or "stopped"
        )
        return
    if now - float(rec.get("acted_at") or 0.0) < _AUTOPILOT_ACTION_COOLDOWN:
        return
    await _autopilot_act(title, wt, rec, snap, action, detail)


def _autopilot_note(title: str, rec: dict, note: str) -> None:
    """Record WHY a pass did nothing, write-on-change.

    The store's whole design is to avoid a write per 5s pass, so this only writes
    when the sentence actually changes — one write per phase transition. Without
    it every hold looked the same to the UI (and to the user) as a dead chain.
    """
    try:
        if (rec or {}).get("note") != note:
            _autopilot.update(title, note=note)
            _emit_autopilot_event(title)
    except Exception:  # noqa: BLE001
        pass


def _autopilot_wait(title, rec, snap, detail, now) -> None:
    """Book-keeping for a pass that decided to do nothing: keep the idle dwell
    honest, publish the reason, and halt if this step outlived its deadline."""
    limited = bool(snap.get("limited"))
    if detail.get("mark_idle") and rec.get("idle_since") is None:
        _autopilot.update(title, idle_since=now)
    elif (limited or snap["activity"] != "idle") and rec.get("idle_since") is not None:
        # A usage-limited session usually LOOKS idle (its turn ended, at the
        # CLI's own hook's word), so the dwell has to be dropped explicitly or it
        # keeps accruing through the outage — and the first pass after the window
        # reopens, before the resume nudge has landed, would find a satisfied
        # 30s settle and commit the half-finished work. The dwell is re-earned
        # after the limit lifts, which is the whole point of it.
        _autopilot.update(title, idle_since=None)
    # Remember that the agent was seen doing something: a clean tree only means
    # "finished with nothing to show" AFTER that, and means "not started yet"
    # before it. Throttled so a working agent costs at most one write per 30s.
    if str(snap.get("activity") or "") in ("working", "clarify"):
        worked = float(rec.get("worked_at") or 0.0)
        if now - worked > 30.0:
            _autopilot.update(title, worked_at=now)
    _autopilot_note(title, rec, detail.get("reason") or "")
    # STOP THE DEADLINE CLOCK WHILE A LIMIT HOLDS. A weekly cap can close the
    # window for days; every step deadline here is minutes-to-hours, so a run
    # that correctly waits out a limit would be halted for "no progress" by its
    # own patience. The stretch is credited back to step_since when the limit
    # lifts (two writes per episode, not one per 5s pass).
    was_limited = float(rec.get("limited_at") or 0.0)
    if limited and not was_limited:
        rec = _autopilot.update(title, limited_at=now) or rec
    elif not limited and was_limited:
        rec = (
            _autopilot.update(
                title,
                limited_at=0.0,
                step_since=float(rec.get("step_since") or now) + (now - was_limited),
            )
            or rec
        )
    step = rec.get("step") or "agent"
    # A run that has not acted yet is WAITING TO BE NEEDED, not making slow progress
    # through a step — so it gets the generous arm budget rather than the agent
    # step's. This is the "armed on a branch whose PR already exists" case: it may
    # legitimately sit idle for a long time before there is anything to carry.
    if not rec.get("step") and int(rec.get("commits") or 0) <= 0:
        deadline = _autopilot.ARM_WAIT_DEADLINE_S
    else:
        deadline = _AUTOPILOT_DEADLINES.get(step, _AUTOPILOT_DEADLINES["agent"])
    since = float(rec.get("step_since") or 0.0)
    if since and not limited and now - since > deadline:
        _autopilot_halt(
            title,
            "gave up waiting at %s after %d min — %s"
            % (step, deadline // 60, detail.get("reason") or "no progress"),
        )


def _autopilot_halt(title: str, reason: str) -> None:
    _autopilot.halt(title, reason)
    _emit_autopilot(title)


def _autopilot_finish(title: str) -> None:
    _autopilot.finish(title)
    _emit_autopilot(title)


async def _autopilot_act(title, wt, rec, snap, action, detail) -> None:
    """Perform one autopilot action by invoking the very code the buttons use.

    Push/PR/merge call the existing route coroutines rather than a second
    implementation of each, so gate order, verbatim error strings and the
    browser-handoff response shapes stay literally one implementation (the repo's
    own tests already drive these coroutines directly, so this is an established
    seam). Only the commit differs: it goes through ``_commit_into_shell`` so a
    SKIP list can be passed — a list that always comes from settings and never
    from a client.
    """
    now = snap["now"]
    fields = {"acted_at": now, "step_since": now}
    # Per-verb attempt cap. Without it a push the remote keeps refusing (protected
    # branch, non-fast-forward, an auth prompt) was re-typed every 15s forever: the
    # stage never moves, so next_action keeps saying "push", and acting re-stamps
    # step_since so the deadline can never fire either.
    issues = dict(rec.get("issues") or {})
    tries = int(issues.get(action) or 0)
    if tries >= _autopilot.MAX_ACTIONS_PER_VERB:
        await asyncio.to_thread(
            _autopilot_halt,
            title,
            "%s did not take after %d attempts — do it by hand"
            % (action.replace("_", " "), tries),
        )
        return
    issues[action] = tries + 1
    fields["issues"] = issues
    try:
        if action == "run_check":
            started = await asyncio.to_thread(_autopilot_run_check, title, wt)
            if not started:
                return
            fields["step"] = "check"
        elif action == "commit":
            done = await asyncio.to_thread(
                _autopilot_commit, title, wt, rec, detail, fields
            )
            if not done:
                return
        elif action == "push":
            resp = await instance_push_branch(title)
            if resp.status_code >= 400:
                await asyncio.to_thread(_autopilot_halt, title, _resp_error(resp))
                return
            fields["step"] = "push"
        elif action == "make_pr":
            base = detail.get("base") or ""
            resp = await instance_make_pr(title, {"base": base} if base else {})
            if resp.status_code >= 400:
                await asyncio.to_thread(_autopilot_halt, title, _resp_error(resp))
                return
            body = _resp_json(resp)
            if body.get("ok") is False:
                # The branch is pushed but the PR needs a human click (no gh, no
                # token) — a stop with a link, not a failure to retry.
                await asyncio.to_thread(
                    _autopilot_halt,
                    title,
                    body.get("message") or "needs gh or a GitHub token to file the PR",
                )
                return
            fields["step"] = "pr"
            # Remember the PR so the client can open it exactly once, the same
            # courtesy the manual "Make PR" button does.
            if body.get("url"):
                fields["url"] = str(body["url"])
        elif action == "merge":
            resp = await instance_merge_pr(title)
            if resp.status_code >= 400:
                msg = _resp_error(resp)
                low = msg.lower()
                # GitHub says "not mergeable" while required checks are still
                # queued. That is a WAIT — halting there raced the CI we are
                # deliberately waiting for.
                if any(
                    k in low
                    for k in (
                        "not mergeable",
                        "required status check",
                        "pending",
                        "waiting",
                        "in progress",
                    )
                ):
                    await asyncio.to_thread(
                        _autopilot_note, title, rec, "GitHub is not ready: " + msg
                    )
                    return
                await asyncio.to_thread(_autopilot_halt, title, msg)
                return
            body = _resp_json(resp)
            if body.get("ok") is False:
                await asyncio.to_thread(
                    _autopilot_halt, title, body.get("message") or "merge it on GitHub"
                )
                return
            _autopilot.update(title, step="merge", **fields)
            await asyncio.to_thread(_autopilot_finish, title)
            return
        else:
            return
    except Exception as err:  # noqa: BLE001 — a failed step halts, never crashes
        await asyncio.to_thread(_autopilot_halt, title, "%s failed: %s" % (action, err))
        return
    _autopilot.update(title, **fields)
    await asyncio.to_thread(_emit_autopilot, title)


def _pending_commit_message(wt: str) -> str:
    """The on-disk commit message, but ONLY when the last attempt failed.

    After a success there is nothing to retry, so a lingering message is stale and
    must not be adopted by the next thing that commits without one.
    """
    try:
        with open(os.path.join(wt, _COMMIT_STATUS_FILE)) as fh:
            if fh.read().strip() in ("", "0"):
                return ""
    except OSError:
        return ""  # no attempt recorded — nothing is pending
    try:
        with open(os.path.join(wt, _COMMIT_MSG_FILE)) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _autopilot_default_message(inst, wt: str) -> str:
    """A commit subject for a fast-track press that carried none.

    Prefers the session's own identity, which for an intake session IS the item
    ("sc-1421-fix-login" / "issue-owner-repo-42"), so the commit reads as the work
    it is rather than as boilerplate.
    """
    try:
        title = str(getattr(inst, "Title", "") or "").strip()
    except Exception:  # noqa: BLE001
        title = ""
    if not title:
        # Called from the commit step, where the instance may not be in hand.
        try:
            title = str(_current_branch(wt) or "").split("/")[-1]
        except Exception:  # noqa: BLE001
            title = ""
    branch = ""
    try:
        branch = _current_branch(wt) or ""
    except Exception:  # noqa: BLE001
        branch = ""
    # Prefer the human name of the work the session came from (a ticket title, a
    # PR title) over the slug: "Add customer-submitted phone numbers to intake"
    # says what landed; "Work on shortcut-21039" says nothing.
    try:
        rec = _autopilot.get(title) or {}
        named = str(rec.get("message") or "").strip()
        if named:
            return named
    except Exception:  # noqa: BLE001
        pass
    subject = title or branch
    return ("Work on %s" % subject) if subject else "Work in progress"


def _autopilot_run_check(title: str, wt: str) -> bool:
    """Start the worktree's verification check. Returns whether one is now running.

    The push route soft-gates on this check, so the driver has to RUN it rather
    than discover the gate as a 409 and halt. Idempotent: an already-running check
    counts as started.
    """
    try:
        if _wt_setup.is_running(wt, "check"):
            return True
        cfg = _wt_setup.load_config(wt)
        if not cfg.check_command:
            return False
        return bool(_wt_setup.start_check(title, wt, cfg.check_command))
    except Exception:  # noqa: BLE001
        return False


def _autopilot_written_message(title: str, wt: str, rec: dict) -> str:
    """A model-written commit message for an autopilot commit, or ``""``.

    This is the fast-track half of the ✨ button: the same generator, at the one
    moment the diff is final. Never raises and never blocks the chain past its own
    timeout — a subject is not worth halting a run for, so every failure is ``""``
    and the caller falls back to the placeholder.
    """
    inst = ENGINE.instances.get(title)
    return (
        _commit_message.suggest_or_none(
            wt,
            program=str(getattr(inst, "Program", "") or ""),
            timeout=_commit_message.TIMEOUT_AUTOPILOT,
            # The intake item this run came from ("sc-1421-fix-login") is real
            # context; the placeholder subject built from it is not, so the hint is
            # the item and never rec["message"].
            hint=str(rec.get("item") or ""),
            branch=_current_branch(wt) or "",
            fallback_program=ENGINE.default_program(),
        )
        or ""
    )


def _autopilot_commit(title, wt, rec, detail, fields) -> bool:
    """Issue an autopilot commit (blocking). Returns whether to record success."""
    msg = rec.get("message") or ""
    if not msg:
        try:
            with open(os.path.join(wt, _COMMIT_MSG_FILE)) as fh:
                msg = fh.read().strip()
        except OSError:
            msg = ""
    # A message the ARM ROUTE invented ("Work on ft-session") is worth replacing
    # now that the diff exists and is final — that placeholder describes the
    # session, not the change. A message a human typed or an intake item named is
    # not touched, and neither is the on-disk one a blocked commit left behind.
    #
    # On success the result is written back to the record so a pre-commit retry
    # commits the same sentence instead of paying for a second turn. A failure
    # leaves the flag up: retries are capped per verb, so at worst this costs one
    # bounded attempt each, and the run still commits under the placeholder.
    if not msg or rec.get("message_auto"):
        written = _autopilot_written_message(title, wt, rec)
        if written:
            msg = written
            fields["message"] = written
            fields["message_auto"] = False
    if not msg:
        # GENERATE one rather than halting. Arming on a CLEAN tree is the natural
        # way to use this — arm the session, let the agent work — and the route
        # records no message then, because there is nothing to describe yet. By the
        # time work exists the record still had none, so the run halted with "no
        # commit message to reuse" at the moment it was finally ready to commit.
        # An honest default subject beats refusing to commit the work.
        msg = _autopilot_default_message(ENGINE.instances.get(title), wt)
    if not msg:
        _autopilot_halt(title, "no commit message to reuse")
        return False
    skip = [h for h in (detail.get("skip") or []) if h not in _autopilot.NEVER_SKIP]
    hook = detail.get("hook") or ""
    err = _commit_into_shell(title, wt, msg, ",".join(skip))
    if err is not None:
        _autopilot_halt(title, "could not start the commit: %s" % err)
        return False
    attempts = dict(rec.get("attempts") or {})
    if hook:
        attempts[hook] = int(attempts.get(hook) or 0) + 1
    fields.update(
        step="commit",
        attempts=attempts,
        commits=int(rec.get("commits") or 0) + 1,
        skipped=skip,
        idle_since=None,
    )
    if detail.get("skipping"):
        fields["reason"] = "skipped %s to get the commit through" % detail["skipping"]
    _forget_probes(title)
    _live_stage.watch(title, wt, "commit")
    return True


def _resp_error(resp) -> str:
    """The server's own error sentence out of a JSONResponse."""
    body = _resp_json(resp)
    return str(body.get("error") or "step failed")


def _resp_json(resp) -> dict:
    try:
        data = json.loads(resp.body)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _publish_autopilot(title: str) -> None:
    """Announce an autopilot change WITHOUT recomputing anything.

    For arming and disarming, which change no git state whatsoever — only a field
    in a small JSON file. The full :func:`_republish_session` was costing ~700ms
    on a cold cache (an ``ls-remote`` network round trip plus a ``gh`` call) and,
    because the routes call this on the event loop, it stalled every other request
    with it. Recording a target depth has no business paying for a PR lookup.

    So: emit the event, then patch JUST the ``autopilot`` block into the published
    row. That last part is what stops the next 4s poll — which serves the stored
    tick snapshot — from reverting the UI's optimistic toggle. Cost is a dict read
    and a short list scan.
    """
    try:
        _emit_autopilot_event(title)
        rows = _events.sessions_snapshot()
        for row in rows:
            if row.get("title") == title:
                row["autopilot"] = _autopilot_dto(title)
                _events.patch_session_snapshot(title, row)
                return
    except Exception:  # noqa: BLE001
        pass


def _emit_autopilot_event(title: str) -> None:
    """The cheap half of :func:`_emit_autopilot`: the event only, no probes."""
    try:
        rec = _autopilot.get(title) or {}
        _events.BUS.emit(
            "session.autopilot_changed",
            session=title,
            new=rec.get("state") or "",
            data={
                "depth": rec.get("depth") or "",
                "step": rec.get("step") or "",
                "state": rec.get("state") or "",
                "reason": rec.get("reason") or "",
                "note": rec.get("note") or "",
                "url": rec.get("url") or "",
                "item": rec.get("item") or "",
                "skipped": list(rec.get("skipped") or []),
            },
        )
    except Exception:  # noqa: BLE001
        pass


def _emit_autopilot(title: str) -> None:
    """Announce an autopilot change AND rebuild the session's published row.

    For the driver, after it has actually done something (a commit, push, PR or
    merge): the git-derived stage really did change, so the full recompute earns
    its cost. Blocking — the driver already calls it from a worker thread.

    Arm/disarm must use :func:`_publish_autopilot` instead; they change no git
    state and the routes that serve them run on the event loop.
    """
    _emit_autopilot_event(title)
    _republish_session(title)


async def _autopilot_pass() -> None:
    """One pass over every armed session.

    Sessions are stepped one at a time rather than concurrently: the steps commit
    and push into a shared worktree, and serialising them is what keeps two chains
    on a copied session (which share one worktree) from racing.
    """
    titles = await asyncio.to_thread(_autopilot.all_titles)
    if not titles:
        return  # a flock with nothing armed costs one file read
    await asyncio.to_thread(_autopilot.prune, list(ENGINE.instances.keys()))
    for title in titles:
        try:
            await _autopilot_step(title)
        except Exception:  # noqa: BLE001 — one bad chain can't stop the pass
            pass


async def _autopilot_loop() -> None:
    """Drive the autopilot forever (started by the lifespan).

    Work first, sleep after — so an armed chain resumes within one interval of a
    server restart rather than idling through the first sleep.
    """
    while True:
        try:
            await _autopilot_pass()
        except Exception:  # noqa: BLE001 — the pass must never die
            pass
        await asyncio.sleep(_AUTOPILOT_INTERVAL)


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


# --- Verify: manual test plans for work that has gone live -------------------- #
# The impure half of :mod:`backend.web.core.test_plans` (the store, the prompt
# building and the generation one-shot live there; the routes are further down).
# Three things live here because they cannot live in a core module: the trigger
# that fires off a push, the loop that watches origin until the work is really
# live, and the run route's use of ``create_instance``.
#
# The whole feature hangs off ONE entry point, :func:`_ensure_test_plan`, called
# from two independent triggers on purpose — see the comment in
# :func:`_emit_state_changes` for why the redundancy is required and why it is
# free.

#: The due loop's cadence. A minute is deliberately unhurried: this waits for a
#: PR to merge, which happens on human time, and every pass costs a ``git fetch``
#: per waiting plan. Nothing in the product degrades if the "it's live" card
#: appears 59 seconds late.
_TEST_PLAN_DUE_INTERVAL = 60.0

#: The cadence while a verify session is actually running. A minute is right for
#: waiting on a merge and badly wrong for waiting on an agent that is finishing
#: on the user's own screen: the run writes its results file, the session goes
#: quiet, and the row goes on saying "an agent is checking the steps it can" for
#: up to another minute — during which the panel's Refresh button, which on every
#: other surface in the app means "go and ask again", cannot change the answer
#: either, because the only thing that reads that file is this loop. So while
#: anything is running the loop drops to a few seconds. It costs nothing: the
#: running phase is purely local (one ``open()`` per running plan), the expensive
#: half stays on its own minute cadence (see ``full`` in
#: :func:`_test_plans_due_pass`), and there is normally nothing running at all.
_TEST_PLAN_RUN_POLL_S = 4.0

#: How long a verify session may sit in ``running`` before the plan is handed
#: back to the user. A run that has not written its results file in two hours is
#: not slow, it is wedged — the agent crashed, the user took the session over and
#: forgot, the CLI hit a usage limit and stopped. Releasing the plan to ``due``
#: is strictly better than leaving it in a state whose only exit is a file that
#: is never going to be written; the user can re-run it, or answer the steps by
#: hand.
_TEST_PLAN_RUN_GIVE_UP_S = 2 * 60 * 60.0

#: How long ONE pass may spend asking origin whether waiting plans have gone
#: live. Half the interval, so a pass normally finishes inside its own tick.
#: The per-call caps in ``test_plans`` (120s for the fetch) stop a single
#: unreachable remote from wedging the loop, but they are per CALL: a flock with
#: a dozen branches waiting to ship, on a laptop whose VPN just dropped so the
#: origin host blackholes TCP rather than refusing, pays that timeout once per
#: plan and the pass runs for half an hour. This is the aggregate cap the
#: per-call ones cannot be. Checked BETWEEN plans, so a pass can overrun by at
#: most one plan's worth of timeouts.
_TEST_PLAN_LIVE_BUDGET_S = 30.0

#: plan id -> epoch when its liveness was last asked. The cursor that makes the
#: budget above fair rather than a guillotine: see :func:`_liveness_order`. In
#: memory on purpose — it is a scheduling hint, and a restart re-asking every
#: plan once is exactly the right behaviour.
_TEST_PLAN_LIVE_CHECKED: Dict[str, float] = {}

#: How long a plan's "where has this landed" answer is good for, and how long
#: ONE pass may spend refreshing them. Minutes rather than the liveness pass's
#: every-tick rotation, because this is a fact somebody READS off a card rather
#: than one the machine acts on: nothing is marked due by it, nothing is pushed
#: to a phone, and a branch name that is five minutes stale costs nobody
#: anything. The cheap half is local (one `for-each-ref` per plan); the fetch it
#: rides on is shared per repository — see ``test_plans.fetch_all_heads``.
_TEST_PLAN_LANDED_TTL_S = 300.0
_TEST_PLAN_LANDED_BUDGET_S = 15.0

#: plan id -> epoch when its landing was last asked. Same cursor trick as
#: :data:`_TEST_PLAN_LIVE_CHECKED`, and in memory for the same reason.
_TEST_PLAN_LANDED_CHECKED: Dict[str, float] = {}


def _verify_enabled() -> bool:
    """The Verify master switch (``repository.verify_enabled``).

    Read fresh rather than cached: it is a switch a person flips expecting the
    next minute's loop to obey it, and ``load_settings`` is already the cheap,
    memoized read every other setting on this path goes through.

    Local import for the reason the module docstring gives and
    ``server-settings-local-import-trap`` learned the hard way: a bare
    ``load_settings()`` here would NameError into the house try/except and
    silently answer "off" forever.

    Fails OPEN. A settings file this cannot read is not a decision to pause —
    defaulting to off would silently stop writing plans for a user who never
    touched the switch, and the failure would look exactly like the feature
    being broken.
    """
    try:
        from backend.config import settings as _settings

        return bool(_settings.load_settings().repository.verify_enabled)
    except Exception:  # noqa: BLE001
        return True


def _verify_auto_for(repo_root: str, wt: str) -> bool:
    """Whether a push in this repo should write a plan by itself.

    The master switch outranks both opt-ins below. It is not a third opt-in but
    a pause over the whole feature: a user who switches Verify off means "stop
    doing this on your own", and a repo's committed ``verify_on_push`` — which
    is a statement about the repo, not about this machine — must not be able to
    override that. Explicit requests (Write plan, Run) are unaffected; see
    ``RepositorySettings.verify_enabled``.

    Two independent opt-ins, OR'd, because they answer different questions. The
    repo's committed ``.mindflock.toml`` says "everyone who clones this should
    get plans" and travels with the code; the list half says "a person typed
    this repo into the Verify dialog on this machine" and never touches a
    tracked file. Neither can switch the other off — a local list that could
    silently override a repo's committed intent (or vice versa) would make both
    untrustworthy.

    MEMBERSHIP IS THE LOCAL OPT-IN. There is no per-repo ``auto`` flag any more:
    a repo gets automatic plans because somebody added it to
    ``repository.verify_repos``, exactly as a repo in ``github.repos`` gets its
    PRs reviewed by virtue of being there. The match is by the ``owner/name``
    behind this checkout's ``origin`` rather than by its path, so one entry
    covers every clone and every worktree of that repo — see
    :func:`test_plans.is_tracked`, which never raises and whose slug lookup is
    memoized, so this costs at most one ``git remote get-url`` per repo per
    minute on the push path.

    The file half is not legacy. It is the ONLY opt-in available to a checkout
    with no GitHub origin — a local-path remote (MindFlock's own provisioned
    clones are exactly that), another forge, or no remote at all — because such
    a repo has no slug to be listed under.

    The ``.mindflock.toml`` is read from ``wt``, the worktree the push came out
    of: that is the checkout whose branch is being verified, and its copy of the
    file is the one that shipped with this branch.
    """
    if not _verify_enabled():
        return False
    try:
        if _wt_setup.load_config(wt).verify_on_push:
            return True
    except Exception:  # noqa: BLE001 — a malformed toml is not an opt-in
        pass
    return _test_plans.is_tracked(repo_root)


def _ensure_test_plan(title: str, manual: bool = False) -> None:
    """Idempotent: create + generate a test plan for a session that just landed
    on origin. **Never raises, never blocks the caller.**

    ``manual`` is the button saying a person asked for this plan by name, and it
    skips the repo's ``verify_on_push`` opt-in (see
    :func:`_ensure_test_plan_blocking`). Nothing else differs: an explicit
    request and an opted-in push produce exactly the same plan, so there is one
    generation path and not two.

    Both triggers (the ``session.pushed`` subscriber and the stage-transition
    fallback) run on a worker thread inside an event emit, and one of them is on
    the path of the state tick that every client's poll depends on. So this
    function does no work at all itself: it hands the whole job — the git
    probes, the store write, and the up-to-``TIMEOUT_GENERATE`` model call — to
    its own daemon thread and returns immediately.

    A daemon thread rather than ``_register_task(asyncio.to_thread(...))``
    because there is no running loop to create a task on: ``_announce_push``
    reaches us through ``asyncio.to_thread`` and ``_emit_state_changes`` through
    the instances tick, both of which are worker threads where
    ``asyncio.create_task`` raises. (This is the same trap ``live_stage.watch``
    documents; here there is nothing to hop back to the loop FOR, so the thread
    just does the work.)

    Racing callers are not a problem and are not guarded against here:
    ``ensure_plan_for`` decides under its own lock and returns ``None`` to
    everyone but the first, so a duplicate trigger costs one store read.
    """
    if not title:
        return
    try:
        threading.Thread(
            target=_ensure_test_plan_blocking,
            args=(title, manual),
            name="mf-testplan-%s" % title[:48],
            daemon=True,
        ).start()
    except Exception:  # noqa: BLE001 — a plan we could not start is not a reason
        # to fail the push that triggered it. The next push tries again.
        pass


def _ensure_test_plan_blocking(title: str, manual: bool = False) -> None:
    """The body of :func:`_ensure_test_plan`, on its own thread.

    Resolves everything the plan must outlive its session with, then generates.
    The one subtle field is ``repo_root``: it is the MAIN repo
    (``GetGitWorktree().GetRepoPath()``, the same accessor
    ``core.workspaces._base_clone_references`` uses to answer "which repo is this
    session's"), never the worktree. A plan comes due when the work merges, by
    which time the session is normally deleted and its worktree reclaimed — a
    plan pointing at the worktree would be a plan that can never be run, and the
    due loop's ``git fetch`` would have nowhere to run either. The worktree is
    still passed to ``generate`` as its cwd, because right now — seconds after
    the push — it is the only place the branch's diff is guaranteed to be
    readable.
    """
    try:
        inst = ENGINE.instances.get(title)
        if inst is None:
            return  # deleted between the push and this thread starting
        wt = inst.GetWorktreePath()
        if not wt:
            return  # a session with no git workspace has nothing to verify
        try:
            repo_root = inst.GetGitWorktree().GetRepoPath() or ""
        except Exception:  # noqa: BLE001 — raises when Start hasn't finished
            repo_root = ""
        # In-place sessions have repoPath == worktreePath, so this fallback only
        # ever fires for a session whose worktree object is not readable yet —
        # and then the worktree path is the best (and correct) guess.
        repo_root = repo_root or wt
        # Explicit opt-in, exactly like the O3 check gate two fields up in the
        # same file: a repo that has not asked for this gets NOTHING on a push.
        # Generating a plan is a real model call of up to TIMEOUT_GENERATE, and
        # a flock pushing across several repos all day would spend it on plans
        # nobody asked for — and worse, fill the Verify badge, which only means
        # anything while it counts things a person actually intends to check.
        # The button on a pushed session (POST /api/instances/{title}/test-plan,
        # which arrives here with manual=True) is the normal way to get a plan;
        # the opt-in is for a repo whose every change warrants a manual test.
        if not manual and not _verify_auto_for(repo_root, wt):
            return
        branch = _current_branch(wt)
        sha = _git_head_sha(wt)
        # ``repo_root``, not nothing: the live branch is a PER-REPO fact, and the
        # plan is stamped with whichever branch it is told at creation — it is
        # what the due loop watches and what the run prompt checks out. Asking
        # for the flock-wide default here would silently ignore this repo's own
        # override, i.e. the whole feature not working for exactly the repo the
        # user bothered to configure.
        plan = _test_plans.ensure_plan_for(
            title,
            branch,
            sha,
            repo_root,
            _test_plans.resolve_live_branch(repo_root),
            intent=_test_plan_intent(title),
        )
        if plan is None:
            # This branch already has a plan — ``ensure_plan_for``'s idempotent
            # no-op, unchanged: five pushes still make ONE plan. What a later
            # push MAY do is refresh that one plan, and only while nobody has
            # answered anything on it. Deliberately after the ``_verify_auto_for``
            # gate above, so a repo nobody tracks still gets nothing.
            if _test_plans.refresh_for_push(title, branch, sha) is not None:
                _generate_test_plan(
                    title, getattr(inst, "Program", "") or "", wt, refresh=True
                )
            return
        _generate_test_plan(title, getattr(inst, "Program", "") or "", wt)
    except Exception as err:  # noqa: BLE001 — nothing above may surface: this
        # thread has no caller left to raise into, and a missing test plan must
        # never look like a failed push.
        if log.ErrorLog is not None:
            try:
                log.ErrorLog.Printf("test plan for %s could not start: %v", title, err)
            except Exception:  # noqa: BLE001
                pass


def _generate_test_plan(
    plan_id: str, program: str, worktree: str, refresh: bool = False
) -> None:
    """Run the generation one-shot and announce the result. Blocking (up to
    ``test_plans.TIMEOUT_GENERATE``); call it on a thread.

    ``generate`` never raises for a *generation* failure — a timeout, a CLI with
    no headless mode, an unparseable answer all land in the plan as
    ``state="failed"`` with a sentence for the user — so the only thing left to
    do here is emit when it actually worked. ``session.test_plan_ready`` is
    deliberately NOT emitted for a failure: the event means "there are steps
    worth showing", and the Verify dialog re-fetches on it.

    The wrapper is for the failures ``generate`` can't own — a store that cannot
    be written, a disk that filled. This is a thread ENTRY POINT (see
    :func:`_start_test_plan_generation`), and an escaping exception there is a
    bare traceback on stderr with nobody's request attached to it.
    """
    try:
        plan = _test_plans.generate(plan_id, program, worktree, refresh) or {}
    except Exception as err:  # noqa: BLE001
        if log.ErrorLog is not None:
            try:
                log.ErrorLog.Printf("test plan %s generation failed: %v", plan_id, err)
            except Exception:  # noqa: BLE001
                pass
        return
    if plan.get("state") != "generated":
        # NOT SILENCE. `test_plan_ready` keeps its narrow meaning — there are
        # steps worth showing, which is what the dialog refetches on — but a
        # rewrite that failed used to emit nothing at all, so the row went on
        # saying "writing…" for up to a poll and then changed with no
        # explanation. A sibling event says what happened.
        if plan.get("error"):
            _events.BUS.emit(
                "session.test_plan_failed",
                session=plan_id,
                data={
                    "plan": plan_id,
                    "error": str(plan.get("error") or ""),
                    "refreshed": refresh,
                },
            )
        return
    _events.BUS.emit(
        "session.test_plan_ready",
        session=plan_id,
        # ``refreshed`` is additive: the Verify dialog already refetches on this
        # event, so nothing in the frontend has to change to see a refreshed
        # checklist — the flag is for anyone reading the bus.
        data={
            "plan": plan_id,
            "steps": len(plan.get("steps") or []),
            "refreshed": refresh,
        },
    )


#: How many generation attempts a plan gets before the due loop stops trying and
#: parks it in ``failed``. Two, i.e. the original plus exactly one automatic
#: retry: the ordinary cause of a stall is the app closing mid-write, which the
#: retry fixes outright, while a machine where generation reliably dies (a CLI
#: that hangs, a repo that is gone) must reach a sentence a person can read
#: instead of re-spending a model call every five minutes forever.
_TEST_PLAN_GEN_ATTEMPTS = 2


def _test_plan_intent(plan_id: str) -> str:
    """What this session was ASKED to do, from wherever it still survives.

    Called at the one moment the answer is knowable — while the session that did
    the work is still alive — so ``test_plans`` can write it onto the plan and
    stop depending on the engine forever after. See the ``intent`` field in
    ``test_plans._blank``.

    THREE SOURCES, and the second one is not a nicety. ``create_instance`` blanks
    ``inst.Prompt`` and hands the text to the prompt queue whenever a repo has
    committed ``[workspace]`` setup commands (see the hold above) — so reading
    only ``Prompt`` loses the ticket entirely for exactly the repos whose owners
    configured them hardest. The third is the transcript: a session whose prompt
    was delivered and consumed still has it written down in its own scrollback.
    """
    inst = ENGINE.instances.get(plan_id)
    if inst is None:
        return ""
    seed = str(getattr(inst, "Prompt", "") or "").strip()
    if not seed:
        try:
            queued = _prompt_queue.list_queue(plan_id) or []
            seed = str((queued[0] or {}).get("text") or "").strip() if queued else ""
        except Exception:  # noqa: BLE001 — best effort, never a failed push
            seed = ""
    if not seed:
        try:
            from backend.web.core.agent_state import _session_find_prompt

            seed = _session_find_prompt(inst, "# Story:") or ""
        except Exception:  # noqa: BLE001
            seed = ""
    return _test_plans.intent_from_prompt(seed)


def _test_plan_session_ctx(plan_id: str) -> tuple:
    """``(program, worktree)`` for generating this plan, from its session.

    Plans are keyed by session title, so the session — when it still exists — is
    where the two things generation wants come from: the CLI that answers the
    question, and the checkout whose branch the diff is readable in. Both are
    optional: ``generate`` falls back to the flock's default program and to the
    plan's ``repo_root``, which is what lets a plan be rewritten long after its
    session was deleted.

    Shared by the regenerate route and the stall recovery below so the two paths
    cannot drift into asking for different trees.
    """
    inst = ENGINE.instances.get(plan_id)
    if inst is None:
        return "", ""
    try:
        worktree = inst.GetWorktreePath() or ""
    except Exception:  # noqa: BLE001 — a session mid-Start has no worktree yet
        worktree = ""
    return getattr(inst, "Program", "") or "", worktree


def _recover_stalled_test_plans(plans: list) -> None:
    """Pick generations back up that nothing is going to finish.

    THE BUG THIS ENDS: generation runs on a daemon thread, so quitting the app
    while a plan is being written kills it mid-answer. Nothing else ever writes
    that plan again — ``generating`` is the one state whose every exit is written
    by the thread that just died — so the card read "Writing the plan from the
    diff — up to three minutes" forever, and the dialog hides the rewrite button
    in that state, so there was no way out of it from inside the product.

    The loop that already exists is the right owner: it wakes every minute, it
    runs its first pass at startup (which is precisely when the abandoned plans
    are), and it is already the place where "a plan is stuck in a state nothing
    will move it out of" is handled for running plans — see
    ``_TEST_PLAN_RUN_GIVE_UP_S``. This is the same watchdog one rung up the
    ladder.

    Retry first, give up second. A stall is usually the app having been closed,
    and the honest fix for that is to write the plan the user is waiting for, not
    to show them an error about a machine that is no longer switched off.
    :data:`_TEST_PLAN_GEN_ATTEMPTS` bounds it, and ``test_plans.is_stalled``
    guarantees we never race a generation that is merely slow.

    Skipped entirely while Verify is paused, exactly like the liveness phase: the
    switch means the feature is quiet, and a paused Verify that fired model calls
    on a timer would be the loudest thing it does. Nothing is lost — a stalled
    plan keeps its state and the next enabled pass recovers it.
    """
    if not _verify_enabled():
        return
    for plan in plans:
        try:
            if not _test_plans.is_stalled(plan):
                continue
            pid = plan["id"]
            if int(plan.get("gen_attempts") or 0) >= _TEST_PLAN_GEN_ATTEMPTS:
                if _test_plans.give_up_generating(pid) is not None and (
                    log.ErrorLog is not None
                ):
                    try:
                        log.ErrorLog.Printf(
                            "test plan %s: generation stalled twice, giving up", pid
                        )
                    except Exception:  # noqa: BLE001
                        pass
                continue
            program, worktree = _test_plan_session_ctx(pid)
            # A plan that already HAS steps is being refreshed, whatever
            # started the generation that stalled — so a second failure must put
            # it back in ``generated`` rather than parking a working checklist in
            # ``failed`` and taking it out of the due loop.
            _start_test_plan_generation(
                pid, program, worktree, refresh=bool(plan.get("steps"))
            )
        except Exception:  # noqa: BLE001 — one bad plan can't stop the pass
            pass


def _start_test_plan_generation(
    plan_id: str, program: str, worktree: str, refresh: bool = False
) -> None:
    """Fire :func:`_generate_test_plan` on a daemon thread and return at once.

    The regenerate route's spawner. Same reasoning as :func:`_ensure_test_plan`'s
    thread: three minutes is far too long for a request (or for anything else
    sharing the event loop) to wait on, and the answer is written to the store,
    so nobody has to be listening when it lands.
    """
    try:
        threading.Thread(
            target=_generate_test_plan,
            args=(plan_id, program, worktree, refresh),
            name="mf-testplan-%s" % plan_id[:48],
            daemon=True,
        ).start()
    except Exception:  # noqa: BLE001 — the plan keeps whatever it already had
        pass


def _on_session_pushed(envelope: dict) -> None:
    """Bus subscriber: a session's branch reached origin, so it needs a plan.

    The PRIMARY trigger. ``live_stage`` emits ``session.pushed`` exactly once per
    watcher, at the instant the remote branch head equals the local one — the
    only moment in the whole process where "that sha is on origin" is a known
    fact rather than an inference. Subscribers run synchronously on the emitting
    thread, so this must return immediately; :func:`_ensure_test_plan` does.
    """
    try:
        if envelope.get("event") != "session.pushed":
            return
        _ensure_test_plan(str(envelope.get("session") or ""))
    except Exception:  # noqa: BLE001 — a subscriber that raises is logged by the
        # bus and skipped, but a push watcher is not the place to find that out.
        pass


# Subscribed at import, NOT in the lifespan, because the bus is process-wide
# while the lifespan is per-ASGI-app: a test (or the CLI) that imports this
# module and drives the engine directly still gets the trigger, and there is
# nothing to tear down — the callback outlives nothing.
_events.BUS.subscribe(_on_session_pushed)


def _merging_is_shipping(live_branch: str) -> bool:
    """Whether, in this flock, "the PR merged" is evidence that work SHIPPED.

    The gate on the squash-merge fallback below, and the reason
    ``repository.live_branch`` exists as its own knob. In most repos a PR merges
    into the branch users get and the two questions are the same one. In a shop
    that PRs into ``develop`` and ships from ``release`` — the exact split the
    setting's own docstring describes — they are not: a merged PR there says the
    work reached ``develop``, which is not what a Verify plan is waiting for.

    ``pr_base_branch or base_branch or "main"`` mirrors
    ``test_plans.resolve_live_branch``'s chain with both of its live-branch links
    removed (this repo's own override and the flock-wide one): it is where a PR
    lands when nobody says otherwise. Equal to the plan's live branch
    means merging IS shipping and a MERGED PR may stand in for ancestry;
    different means it may not, and the plan waits for the real ancestry test
    (which is what "live" means and is never wrong, only sometimes silent).

    THE QUESTION IS ABOUT THE FLOCK'S SHAPE, NOT ABOUT ONE BRANCH NAME, and that
    is why the flock-wide live branch is asked for as well. There is no per-repo
    PR base anywhere — ``verify_repo_settings`` carries ``live_branch`` and
    ``prompt`` and nothing else — so comparing a PER-REPO live branch against the
    FLOCK-WIDE PR target compares two things that were never about the same
    repo. A repo whose
    card says it ships from ``staging`` while the flock configures no PR base at
    all would score ``"main" != "staging"`` and lose the squash-merge fallback
    forever: its plans would sit in ``generated`` and never come due, i.e. the
    feature would break for exactly the repo somebody bothered to configure, and
    would have worked before they configured it. So the split is detected once,
    flock-wide — does the PR target differ from what the flock calls live? — and
    only a flock that really has typed that split (``pr_base_branch=develop``
    with ``live_branch=release``) falls through to the per-plan comparison, which
    still lets a plan whose branch IS the PR target keep the fallback.

    Unreadable settings answer ``True``, i.e. the pre-existing behaviour: the
    divergent configuration is one the user had to type, and a flock that never
    touched the setting must not lose the squash-merge fallback over a failed
    settings read.
    """
    try:
        # Function-local by house rule (see :func:`_configured_pr_base`): a
        # module-level-only reference NameErrors into the except below and
        # silently answers the fallback forever.
        from backend.config.settings import load_settings

        r = load_settings().repository
        target = (r.pr_base_branch or r.base_branch or "main").strip()
    except Exception:  # noqa: BLE001
        return True
    # No repo argument on purpose: this is the flock-wide answer, the one thing
    # ``target`` is comparable with. When they agree, nobody has declared a
    # PR-base/ship split and merging is shipping everywhere in the flock —
    # including in a repo that overrode its own live branch, whose override says
    # where IT ships and says nothing about a develop/release process.
    if target == _test_plans.resolve_live_branch():
        return True
    return target == str(live_branch or "").strip()


def _test_plan_is_live(plan: dict) -> bool:
    """Whether this plan's work has reached the live branch. Blocking (a fetch
    plus, sometimes, a PR lookup) — call it on a thread.

    Two questions, because one of them cannot answer on its own. Ancestry
    (``test_plans.is_live``) is the honest test and the only one that works for a
    repo with no PRs at all. But a **squash merge rewrites the commit**, so the
    sha this plan recorded at push time never becomes an ancestor of anything —
    for the many flocks that squash by default, ancestry alone would mean no plan
    is EVER due, which is the feature silently not existing. So a branch whose
    most recent PR reports ``MERGED`` counts as live too — but only where merging
    IS shipping (:func:`_merging_is_shipping`).

    That gate is not a nicety. ``_pr_info`` matches by head branch alone and is
    deliberately NOT filtered by base (its docstring says why, and it cannot be:
    neither rung even returns the base a PR merged into). So without the gate,
    the fallback reads "this branch's PR merged into *something*" — and in a shop
    that PRs into ``develop`` and ships from ``release`` that fires on every
    merge, marking plans due, pushing "it's live — verify it" to a phone, and
    sending a verify run at a branch that does not contain the change. Worse, it
    is a one-way door: liveness is only re-asked while the plan is ``generated``,
    so the premature ``due`` is never corrected when the work really ships.

    ``_pr_info`` is reused rather than re-asked: it is the same memoized, sticky,
    gh-or-REST lookup the stage machine runs, so the two can never disagree about
    whether a branch's PR merged. It is given the MAIN repo as its cwd — the
    worktree it was written for is usually gone by now, and the main repo has the
    same origin, which is all ``gh`` needs.
    """
    verdict = _test_plans.probe_live(
        plan["repo_root"], plan["sha"], plan["live_branch"]
    )
    if verdict == "live":
        _test_plans.set_live_problem(plan["id"], "")
        return True
    # SAY WHEN THE WAIT CANNOT END. "Not shipped yet" and "waiting for a branch
    # origin does not have" look identical on screen — a row cheerfully saying
    # "it turns up here to check when it ships" — and only one of them is the
    # user's to fix. A checklist that can never come due is worse than no
    # checklist, because the whole promise is that it tells you.
    if verdict == "missing":
        _test_plans.set_live_problem(
            plan["id"],
            "origin has no branch called %s, so this can never come due. Set the "
            "live branch on this repo's card in Verify \u2192 Sources."
            % (plan["live_branch"] or "?"),
        )
    else:
        # "waiting" and "unreachable" are both ordinary; clear any stale
        # diagnosis (the branch may have just been created, or the network came
        # back) rather than leaving a sentence that is no longer true.
        _test_plans.set_live_problem(plan["id"], "")
    info = _pr_info(plan["repo_root"], plan["branch"])
    merged = bool(info) and str(info.get("state") or "").upper() == "MERGED"
    base = str((info or {}).get("base") or "").strip()
    live = str(plan["live_branch"] or "").strip()
    if merged and base:
        # THE EXACT ANSWER, when the PR can give one. A squash merge rewrites the
        # commit, so "this branch's PR merged" is the only evidence left that the
        # work shipped — and it is good evidence precisely when the PR merged
        # into the branch this checklist is waiting for. That is a fact about the
        # PR, and asking it beats inferring it from flock-wide settings, which is
        # what `_merging_is_shipping` has to do when the base is unknown.
        if base == live:
            _test_plans.set_live_problem(plan["id"], "")
            return True
        # Merged, but somewhere else. This is the state that used to wait for
        # ever in silence: a repo that PRs into `staging` and ships from `main`
        # has work that is genuinely merged and genuinely not live, and the row
        # said "it turns up here to check when it ships" indefinitely. Say what
        # happened, and let the deploy question stay open.
        _test_plans.set_live_problem(
            plan["id"],
            "Its pull request merged into %s, not %s. This checklist is waiting "
            "for %s \u2014 change the live branch on this repo's card if %s is "
            "what you ship." % (base, live, live, base),
        )
        return False
    if not _merging_is_shipping(live):
        return False
    return merged


def _test_plan_merged_into(plan: dict) -> dict:
    """Where this plan's work has reached on origin. Blocking — call it on a
    thread. Shaped like ``test_plans.probe_merged_into``.

    THE QUESTION THE CARD COULD NOT ANSWER. A checklist knows the branch it was
    pushed on and the branch it is waiting for; between those two it says nothing
    about the branch the work is actually sitting on right now, which in a repo
    with a develop or a release step is most of a change's life. "Is this in
    staging yet, or already in main?" is the thing somebody scanning the list
    wants, and until this it was a question you answered by leaving the app.

    Two rungs, the same two — and in the same order — as :func:`_test_plan_is_live`:

    * **Ancestry** (``probe_merged_into``) is the honest test, works in a repo
      with no PRs at all, and is the only one that can name a branch nobody
      opened a PR against.
    * **The PR's own base**, when ancestry finds nothing. A SQUASH merge rewrites
      the commit, so the sha this plan recorded never becomes an ancestor of
      anything and ancestry says "nowhere" about work that demonstrably shipped —
      for a flock that squashes by default, that is the feature silently not
      existing. ``_pr_info`` is reused rather than re-asked, so this and the due
      loop can never disagree about where a branch's PR went.

    Unlike the liveness fallback this one needs no ``_merging_is_shipping`` gate:
    it is not deciding whether anything shipped, only reporting the branch a
    merged PR names as its base. A PR that merged into ``develop`` is exactly the
    case this is here to show.
    """
    found = _test_plans.probe_merged_into(
        plan.get("repo_root") or "", plan.get("sha") or "", plan.get("branch") or ""
    )
    if found.get("branch"):
        return found
    info = _pr_info(plan.get("repo_root") or "", plan.get("branch") or "")
    if not info or str(info.get("state") or "").upper() != "MERGED":
        return found
    base = str(info.get("base") or "").strip()
    if not base:
        return found
    # The PR knows WHERE but this rung cannot know WHEN — `_pr_info` does not
    # carry a merge time. `merged_at` is the closest honest stamp (the moment
    # this flock first saw the work merged) and 0.0 says "no idea", which is
    # what the row renders as an unqualified "merged into develop".
    return {
        "branch": base,
        "at": float(plan.get("merged_at") or 0.0),
        "all": [base],
    }


def _notify_test_plan_due(plan: dict) -> None:
    """Push "this is live, go check it" to the user's phone, when ntfy is on.

    AT MOST ONCE PER PLAN — see ``test_plans.mark_notified``. Several things can
    move a plan to ``due`` more than once (pressing "it's out, check it now", a
    rewrite of a plan that had already shipped), and *"sc-1234 shipped to main"*
    arriving days after it shipped is a notification that is not true, about the
    one subject this surface exists to be believed about.

    The one notification this feature sends, and the reason it can be sent at
    all: a plan comes due minutes or days after the session that made it, from a
    merge this machine may have had nothing to do with, so there is frequently
    nobody looking at the UI when it happens. Guarded on ``cfg.active`` (the same
    check every other push path makes) and wrapped, because a notification must
    never be able to stop the loop that produced it.
    """
    try:
        if plan.get("notified_at"):
            return
        cfg = _ntfy.load()
        # Stamped whether or not ntfy is on, and before the send rather than
        # after it: the stamp means "this plan's shipping moment has passed", not
        # "a push succeeded". Switching notifications ON next week must not
        # produce a backlog of announcements about work that shipped last month.
        _test_plans.mark_notified(str(plan.get("id") or ""))
        if not cfg.active:
            return
        steps = plan.get("steps") or []
        mine = sum(1 for s in steps if s.get("actor") == "human")
        _ntfy.publish_soon(
            cfg,
            title="%s shipped to %s"
            % (
                plan.get("title") or plan.get("id"),
                plan.get("live_branch") or "the live branch",
            ),
            # Two numbers, because they are not the same number and the old
            # message implied they were: "4 step(s) to check on main" reads as
            # four things for the reader to do, when three of them are the
            # agent's. It also garden-paths — "check on main" parses as "look in
            # on main" before it parses as "check, on main" — which is why the
            # branch has moved up into the title, where it is a fact about what
            # happened rather than a place to go.
            message="%d step%s in its checklist, %d for you."
            % (len(steps), "" if len(steps) == 1 else "s", mine),
            # 3 = normal. This is news, not an emergency: nothing is broken and
            # nothing is waiting on the user, unlike the rules that ring at 4.
            priority=3,
            tags=["white_check_mark"],
        )
    except Exception as err:  # noqa: BLE001
        _ntfy.log_error("test plan due push failed: %s", err)


def _test_plan_run_started_at(plan: dict) -> float:
    """When the plan's CURRENT run began (0.0 when there isn't one).

    Read off the run record ``start_run`` opened rather than tracked in memory,
    so a server restart mid-run does not reset the give-up clock — a wedged
    verify session must not become immortal by outliving the process watching it.
    """
    for run in reversed(plan.get("runs") or []):
        if run.get("session") == plan.get("run_session"):
            return float(run.get("at") or 0.0)
    return 0.0


def _read_verify_results(plan: dict) -> Optional[dict]:
    """The run session's ``.mindflock_verify.json``, or ``None``.

    This file IS the verify session's return channel: the run is an ordinary
    session in a real workspace with no exit code and no callback (exactly the
    problem ``live_stage`` exists for), so the agent is told to write its answers
    to one git-excluded file and the due loop reads it. Absent, half-written or
    malformed all read the same way — "not finished yet" — because the poller
    runs every 60s and the next pass is a free retry.
    """
    inst = ENGINE.instances.get(plan.get("run_session") or "")
    if inst is None:
        return None  # session deleted; test_plans.prune releases the plan
    try:
        wt = inst.GetWorktreePath()
    except Exception:  # noqa: BLE001
        wt = ""
    if not wt:
        return None
    try:
        with open(
            os.path.join(wt, _test_plans.RESULT_FILE), "r", encoding="utf-8"
        ) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    # IT MUST BE ABOUT THIS PLAN. The prompt asks the agent to echo the plan id
    # and nothing checked it, so a result file left behind by an earlier plan in
    # a reused worktree — or one an agent wrote from an example — could settle a
    # checklist it never read. Blank is tolerated: an older build's file has no
    # id, and refusing those would strand every run in flight across an upgrade.
    claimed = str(data.get("plan") or "")
    if claimed and claimed != str(plan.get("id") or ""):
        return None
    return data


def _free_stale_verify_worktree(repo_root: str, title: str) -> str:
    """Release a worktree a DEAD verify session left holding this run's branch.

    THE TRAP THIS ENDS, and it is a permanent one rather than a flaky one. A
    verify session is named for its plan and its commit (``verify-<plan>-<sha7>``)
    precisely so that "same commit" and "different commit" are different
    sessions — which also makes the branch it wants
    (``<branch_prefix>verify-<plan>-<sha7>``) the SAME name every time. Git will
    not check a branch out in two worktrees at once, so the moment one verify
    worktree is left behind — the app killed mid-run, a session removed from the
    engine without its worktree being reclaimed, a crash — every subsequent run
    of that checklist dies in ``Start`` with::

        fatal: '<branch>' is already used by worktree at '<path>'

    and the plan silently reverts. Not once: forever, for that checklist, with no
    control anywhere in the product that clears it.

    ONLY WHAT NOTHING OWNS. The live sessions' worktree paths are collected
    first and never touched — this must not be able to reclaim the tree an agent
    is working in, which is the one thing worse than the bug it fixes. A
    worktree registered to a path that no longer exists on disk is also fair
    game (that is what ``git worktree prune`` is for) and is the commonest shape
    of the leftover.

    Returns a short description of what it freed, for the log; "" when there was
    nothing to do, which is the normal case.
    """
    repo = str(repo_root or "")
    if not repo or not git_available():
        return ""
    try:
        from backend.config import config as _config
        from backend.session.git.worktree import sanitize_branch_name

        want = sanitize_branch_name(
            "{}{}".format(_config.LoadConfig().branch_prefix, title)
        )
    except Exception:  # noqa: BLE001 — a cleanup that cannot name its target
        return ""  # is a cleanup that must not run

    live: set = set()
    for inst in list(ENGINE.instances.values()):
        try:
            path = inst.GetWorktreePath()
        except Exception:  # noqa: BLE001
            path = ""
        if path:
            live.add(os.path.abspath(path))

    try:
        out = subprocess.run(
            ["git", "-C", repo, "worktree", "list", "--porcelain"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=30,
        )
    except Exception:  # noqa: BLE001
        return ""
    if out.returncode != 0:
        return ""

    def _remove(path: str, branch: str) -> None:
        for argv in (
            ["git", "-C", repo, "worktree", "remove", "--force", path],
            ["git", "-C", repo, "worktree", "prune"],
        ) + ((["git", "-C", repo, "branch", "-D", branch],) if branch else ()):
            try:
                subprocess.run(
                    argv,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    timeout=60,
                )
            except Exception:  # noqa: BLE001 — best effort, in order
                pass

    # The directory name a worktree for THIS session would have:
    # ``<worktrees dir>/<sanitized branch>_<hex>``, so the basename is the last
    # segment of the branch plus an underscore. Matched by name because the
    # branch line cannot be relied on — see the detached case below.
    leaf = want.rsplit("/", 1)[-1] + "_"

    freed = ""
    path = ""
    detached = False
    for line in out.stdout.decode("utf-8", "replace").splitlines() + [""]:
        if line.startswith("worktree "):
            path = line[len("worktree ") :].strip()
            detached = False
            continue
        if line.strip() == "detached":
            detached = True
            continue
        branch = ""
        if line.startswith("branch "):
            branch = line[len("branch ") :].strip().split("refs/heads/", 1)[-1]
        elif line.strip():
            continue
        # End of this worktree's block (a blank line), or its branch line.
        if not path:
            continue
        # A LEFTOVER IS NOT ALWAYS ON ITS BRANCH, and that is the case this
        # feature actually produces: a verify run's first act is to check out the
        # commit it is verifying, so the worktree ends up DETACHED and the branch
        # is often gone entirely. Matching only the branch line therefore missed
        # every leftover from a run that had actually run — they accumulate in
        # `~/.mindflock/worktrees`, a full checkout each, and nothing in the
        # product ever removes them. So a directory named for this session
        # counts too, and it is just as safe: the name carries the plan AND the
        # sha, and a live session's path is never touched.
        mine = branch == want or (
            (detached or not branch) and os.path.basename(path).startswith(leaf)
        )
        if not mine:
            if branch or not line.strip():
                path = ""
                detached = False
            continue
        if os.path.abspath(path) in live:
            # An agent is in there right now. Whatever is wrong, this is not
            # ours to take.
            return ""
        _remove(path, branch if branch == want else "")
        freed = ("%s (%s)" % (branch, path)) if branch else path
        path = ""
        detached = False
    return freed


def _kill_orphan_plan_tmux(title: str) -> str:
    """Kill the tmux sessions a DEAD run of this checklist left behind.

    THE OTHER HALF OF :func:`_free_stale_verify_worktree`, and the same trap
    from the other end. A verify session is named for its plan and its commit,
    so the tmux session it wants (``mindflock_verify-<plan>-<sha7>``) has the
    SAME name every time that checklist is run for that commit. tmux outlives
    this process: kill the app mid-run, delete the session while its window is
    detached, lose the engine's record in a crash, and the tmux session survives
    with nothing owning it. Every later run of that checklist then dies in
    ``Instance.Start`` with

        failed to start new session: tmux session already exists: mindflock_verify-…

    which is exactly what the owner saw. It dies LATE, too — on the background
    start task, after the workspace was provisioned and after the route already
    answered 202 — so the plan is stamped ``running``, then reverts with that
    sentence recorded on it and no control anywhere in the product to clear it.
    Permanent for that checklist, like the worktree case, and invisible: nothing
    in the session list shows an orphan, because nothing in the app owns it.

    ONLY WHAT NOTHING OWNS. Called for a ``verify-`` or ``fix-`` title with no
    live instance, it kills exactly the two sessions named for that title (the
    agent window and its shell) and nothing else. Both are sessions THIS FEATURE
    creates, from a name it derives, for work it is about to start again — which
    is what makes killing them safe in a way it would not be for a session
    somebody named themselves. Note the asymmetry with the worktree: a window is
    a process, and killing an unowned one loses nothing that was not already
    lost, while a fix session's TREE can hold uncommitted work and is only ever
    reclaimed when it is provably pristine (:func:`_reclaim_plan_worktree`).

    Returns what it killed, for the log; "" when there was nothing, which is the
    normal case. Never raises.
    """
    title = str(title or "")
    # Belt and braces: the caller checks both of these, and this must never be
    # reachable for a session a person owns.
    if not title.startswith(("verify-", "fix-")) or title in ENGINE.instances:
        return ""
    killed = []
    try:
        names = [tmux.to_mindflock_tmux_name(title), _shell_tmux_name(title)]
    except Exception:  # noqa: BLE001 — a cleanup that cannot name its target
        return ""  # is a cleanup that must not run
    for name in names:
        try:
            if _live_session_name(name) is None:
                continue
            rc = _run_capped(
                ["tmux", "kill-session", "-t=" + name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
            ).returncode
            if rc == 0:
                killed.append(name)
        except Exception:  # noqa: BLE001 — best effort, one name at a time
            continue
    return ", ".join(killed)


def _reclaim_plan_worktree(repo_root: str, title: str) -> Tuple[str, str]:
    """Free the branch a dead ``fix-`` session is holding — ONLY if it is safe.

    The same permanent wedge as :func:`_free_stale_verify_worktree`, on a
    session with the opposite contract. A fix session's whole job is to CHANGE
    the tree, so its worktree can hold work nobody has committed, and the verify
    reclaim's ``worktree remove --force`` + ``branch -D`` would delete exactly
    that. ``worktree_reclaim.reclaim_for_branch`` refuses anything that is not
    pristine and anything a live session owns, so the wedge clears itself in the
    (common) case where the leftover is empty and stays put in the case where
    removing it would lose work — where the route's own error is then the honest
    answer.

    Returns ``(reclaimed, held)``: the path it freed, or the path it REFUSED to
    free and why the caller must stop. Both empty is the normal case — nothing
    was holding the branch. Never raises.

    THE THIRD ANSWER IS THE POINT. A decline used to look exactly like "nothing
    to do", so the route created the session anyway and the collision surfaced
    minutes later inside ``_bg_start`` as a raw git line in the notifications
    bell — nowhere near the checklist, and with no hint that the fix for it is a
    directory full of somebody's uncommitted work. The route can now say that
    synchronously.
    """
    repo = str(repo_root or "")
    if not repo or not git_available():
        return "", ""
    try:
        from backend.config import config as _config
        from backend.session.git.worktree import sanitize_branch_name
        from backend.session.provisioned import worktree_holding_branch
        from backend.web.core import worktree_reclaim as _reclaim

        branch = sanitize_branch_name(
            "{}{}".format(_config.LoadConfig().branch_prefix, title)
        )
        held_at = worktree_holding_branch(repo, branch) or ""
        if not held_at:
            return "", ""
        live = set()
        for inst in list(ENGINE.instances.values()):
            try:
                path = inst.GetWorktreePath()
            except Exception:  # noqa: BLE001
                path = ""
            if path:
                live.add(os.path.abspath(path))
        freed = _reclaim.reclaim_for_branch(
            repo, branch, lambda path: os.path.abspath(path) in live
        )
        return (freed, "") if freed else ("", held_at)
    except Exception:  # noqa: BLE001 — a cleanup that cannot name its target
        return "", ""  # is a cleanup that must not run


def _verify_session_usable(title: str) -> bool:
    """Whether the open verify session ``title`` can actually be sent work.

    A session record outlives its workspace: the worktree can be reclaimed,
    removed by hand, or lost with the disk it was on, and the engine goes on
    holding the instance. Everything that matters downstream — sending a prompt,
    reading the result file — needs the directory, so "is there a record" is the
    wrong question and was the one being asked.
    """
    inst = ENGINE.instances.get(title)
    if inst is None:
        return False
    try:
        wt = inst.GetWorktreePath()
    except Exception:  # noqa: BLE001
        return False
    return bool(wt) and os.path.isdir(wt)


def _test_plan_row(plan: dict, live_branch: Optional[str] = None) -> dict:
    """One plan, shaped the way every reader of this feature expects it.

    THE TWO EDITS THE STORE DOES NOT DO, in one place because a second reader
    now exists (``POST /run`` answers with the plan it just started, and the
    client REPLACES its whole row with it):

    * ``effective_live_branch`` — what THIS PLAN'S REPO calls live, which is the
      only branch its own stamp can honestly be compared with. Without it a row
      falls back to the flock-wide default and a repo that ships from ``staging``
      starts reading as "written against a branch that has since moved".
    * ``conversation`` — the snapshotted transcript is generation input, never
      UI. Nothing renders it, and it is up to CONV_BUDGET of somebody's session
      text per plan.

    Mutates and returns the same dict, which is what the list route wants.
    """
    root = str(plan.get("repo_root") or "")
    plan["effective_live_branch"] = (
        live_branch
        if live_branch is not None
        else _test_plans.resolve_live_branch(root)
    )
    plan.pop("conversation", None)
    return plan


def _is_verify_repo_usable(repo_root: str) -> bool:
    """Whether a run can actually be started in ``repo_root``.

    A plan records the MAIN repo rather than the worktree, precisely so it
    outlives the session that produced it — and over the weeks a checklist can
    wait, that path can be moved, renamed or deleted. Nothing downstream
    notices: ``_prepare_plain_repo`` creates a missing path rather than
    refusing, so the run gets a real session in an empty non-git folder.

    A directory that exists but is not a git repository counts as unusable for
    the same reason: the run's first instruction is to check out the live
    branch. Never raises — an unreadable path is not a usable one.
    """
    root = str(repo_root or "")
    if not root:
        return False
    try:
        if not os.path.isdir(root):
            return False
        return (
            _run_capped(
                ["git", "-C", root, "rev-parse", "--git-dir"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                timeout=30,
            ).returncode
            == 0
        )
    except Exception:  # noqa: BLE001
        return False


def _clear_verify_results(session_title: str) -> None:
    """Delete a verify session's result file, if it has one. Never raises.

    Keyed by session title rather than by plan, because it runs before the plan
    knows which session it is about to get. A session that does not exist yet
    (the first run) has nothing to clear, which is why every failure here is a
    no-op rather than an error.
    """
    inst = ENGINE.instances.get(session_title)
    if inst is None:
        return
    try:
        wt = inst.GetWorktreePath()
        if wt:
            os.unlink(os.path.join(wt, _test_plans.RESULT_FILE))
    except Exception:  # noqa: BLE001 — no file, no worktree, no permission
        pass


def _announce_test_plan_checked(plan: dict) -> None:
    """Say that a run finished, and whether it left anything.

    Starting a run is loud — a toast, a row that changes, a terminal that opens
    — and finishing one was completely silent: the results landed in a dialog
    the user had almost certainly navigated away from, minutes later. The event
    carries the two numbers that decide whether anything is being asked of them,
    so the client can say "8 passed, 3 need your eyes" without a second fetch.
    """
    try:
        pid = str(plan.get("id") or "")
        run = (plan.get("runs") or [{}])[-1]
        results = run.get("results") or {}
        failed = sum(1 for r in results.values() if r.get("result") == "fail")
        # THE TWO NUMBERS MUST NOT COUNT THE SAME STEP. Written as "not a pass
        # and not answered by a person", this counted every agent-recorded FAIL
        # in both — so a run finding one broken step announced "1 step failed, 1
        # step needs you", which reads as two problems and is one.
        #
        # Mirrors `stepCheck`'s "yours" in verify.ts, which is what the row will
        # say when the user opens the dialog the toast sent them to: a step is
        # yours when nobody has settled it AND either it was always yours (a
        # human actor) or the agent explicitly handed it back (`blocked`).
        # `pass` and `fail` are settled; a `blocked` a PERSON recorded is their
        # "can't check", which is an answer.
        needs_you = 0
        for step in plan.get("steps") or []:
            entry = results.get(step.get("id")) or {}
            result = entry.get("result") or ""
            if result in ("pass", "fail"):
                continue
            if result == "blocked" and entry.get("by") == "human":
                continue
            if step.get("actor") == "human" or result == "blocked":
                needs_you += 1
        _events.BUS.emit(
            "session.test_plan_checked",
            session=pid,
            data={
                "plan": pid,
                "title": plan.get("title") or pid,
                "failed": failed,
                "needs_you": needs_you,
            },
        )
    except Exception:  # noqa: BLE001 — an announcement must never stop the pass
        pass


def _verify_run_trees(plan: dict) -> tuple:
    """``(tested_sha, expected_sha)`` for a run that has just answered.

    THE CLAIM THIS MAKES CHECKABLE. ``build_run_prompt`` spends a paragraph on
    why a verify run must not be able to test the wrong tree, and until this the
    whole mechanism was a sentence asking the agent to check out
    ``origin/<live>``. A fetch that failed quietly left the agent working
    whatever HEAD the worktree was cut from — typically the clone's LOCAL live
    branch, which is behind origin — and the plan then recorded "it works" about
    a tree nobody could name.

    Both answers are best-effort and both may be blank, which
    ``test_plans.run_tree_mismatch`` reads as "unknown", never as "mismatched":
    the cost of a wrong guess here is throwing away a good run's answers.

    A plan run BEFORE it shipped is compared against its own commit, which is
    what that arm of the run prompt asked for and the tree those steps are about.
    """
    inst = ENGINE.instances.get(plan.get("run_session") or "")
    if inst is None:
        return "", ""
    try:
        wt = inst.GetWorktreePath() or ""
    except Exception:  # noqa: BLE001
        wt = ""
    if not wt:
        return "", ""
    tested = _git_head_sha(wt)
    live = str(plan.get("live_branch") or "").strip()
    if not plan.get("live_at"):
        # "Check it early" — the honest expectation is the branch's own tip.
        return tested, str(plan.get("tip_sha") or plan.get("sha") or "")
    root = str(plan.get("repo_root") or "") or wt
    expected = ""
    for ref in ("origin/%s" % live, live):
        if not live:
            break
        out = subprocess.run(
            ["git", "-C", root, "rev-parse", "--verify", "-q", ref],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=15,
        )
        if out.returncode == 0:
            expected = out.stdout.decode("utf-8", "replace").strip()
            break
    return tested, expected


#: When each running verify session was first seen with no tmux window, and how
#: long that has to hold before the run is released. One miss is not proof: tmux
#: can be briefly unreachable, and `create_instance` registers the record a
#: moment before `Start` makes the window.
_VERIFY_DEAD_SINCE: Dict[str, float] = {}
_VERIFY_DEAD_GRACE_S = 120.0


def _verify_window_gone(run_session: str, now: Optional[float] = None) -> bool:
    """Whether ``session``'s agent window has been missing long enough to act on.

    Cheap in the normal case — one ``tmux has-session`` per running plan, and
    there is usually at most one. Never raises: a probe that cannot answer is
    not evidence of death, so it forgets the session and starts the clock again.

    A SESSION THAT HAS NOT COME UP YET IS NOT A DEAD ONE, and this is the trap
    worth naming: `start_run` stamps the plan the moment the route answers 202,
    while the workspace behind it is still being made — a cold base clone plus a
    dependency install runs for MINUTES, with no tmux window for any of it. A
    bare "no window" test would therefore give up on every first run in a new
    repo, halfway through provisioning it. A paused session is the same
    argument: its window is deliberately gone and its run is not. Both are left
    to the clock they already have (the two-hour deadline) and to `prune`, which
    owns the case where the record itself has disappeared.
    """
    ts = float(now if now is not None else time.time())
    inst = ENGINE.instances.get(run_session)
    if inst is None:
        _VERIFY_DEAD_SINCE.pop(run_session, None)
        return False
    try:
        # `Paused` by name rather than `session.Paused`: an earlier draft of
        # this took the title in a parameter called `session`, which shadowed the
        # `session` MODULE — the attribute form then resolved against a string,
        # raised, and the except below read that as "not evidence", so the check
        # could never fire at all. The parameter is `run_session` now; the plain
        # name stays because it cannot be shadowed by accident.
        if not inst.Started() or inst.Status in (Loading, Paused):
            _VERIFY_DEAD_SINCE.pop(run_session, None)
            return False
    except Exception:  # noqa: BLE001 — an unreadable status is not evidence
        _VERIFY_DEAD_SINCE.pop(run_session, None)
        return False
    # THREE ANSWERS, NOT TWO. `tmux has-session` exits 0 for a live session and
    # 1 for a missing one; anything else — a tmux server that is not answering,
    # the 124 `_run_capped` returns when it kills a hung probe — means the
    # question was not answered at all. Folding that into "missing" is how a
    # loaded machine gives up on a run that is working: two minutes of slow
    # probes and the plan is released out from under a live agent.
    try:
        rc = _run_capped(
            ["tmux", "has-session", "-t=" + tmux.to_mindflock_tmux_name(run_session)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        ).returncode
    except Exception:  # noqa: BLE001
        rc = -1
    if rc != 1:
        _VERIFY_DEAD_SINCE.pop(run_session, None)
        return False
    first = _VERIFY_DEAD_SINCE.setdefault(run_session, ts)
    return ts - first >= _VERIFY_DEAD_GRACE_S


def _release_wedged_run(
    pid: str, run_session: str, reason: str, hours: int = 0
) -> None:
    """Put a plan back and SAY WHY, for a run that is never going to report.

    Both callers — the two-hour deadline and the dead window — need the same
    three things to happen together, and the one that mattered most was missing
    from both: the sentence. `mark_due` alone put the row back to "nobody has
    checked it yet", so a session that had been started, been billed and died
    read exactly like a button nobody pressed.
    """
    released = _test_plans.mark_due(pid, reason=reason)
    if released is None:
        return
    _VERIFY_DEAD_SINCE.pop(run_session, None)
    # `mark_due` cleared `run_session` AND dropped the empty run record, which is
    # what actually hands the session to `_sweep_orphan_verify_sessions`: it
    # keeps any session a run record still names, so before that pop the
    # abandoned agent was exempt from the one thing that closes strays.
    _events.BUS.emit(
        "session.test_plan_gave_up",
        session=pid,
        data={
            "plan": pid,
            "title": released.get("title") or pid,
            "run_session": run_session,
            # WHICH release this was. Two things end a run without a report — a
            # window that died in the first minutes and a deadline two hours
            # later — and an event carrying only `hours` (0 for one of them)
            # made them indistinguishable to anyone reading the bus, while the
            # sentence the plan got said exactly which. It travels with it.
            "hours": hours,
            "reason": reason,
        },
    )


def _poll_running_test_plans(plans: list) -> None:
    """Fold in the results of every verify session that has finished.

    Deliberately its own phase, run BEFORE the liveness checks and never behind
    them: this is the half of the pass that is purely local (one ``open()`` per
    running plan) and the half a person is actually waiting on — the verify
    session on their screen has written its answers and the plan still says
    "running". Sharing one loop with the network half meant a plan waiting to go
    live could hold the results of a finished run hostage for as long as origin
    took to answer, which is also how the two-hour give-up clock stopped being
    evaluated. Ordering costs nothing and removes the coupling entirely.
    """
    live_titles = set()
    for plan in plans:
        if plan["state"] != "running":
            continue
        pid = plan["id"]
        # NOT `session`: that is an imported MODULE name, and shadowing it in a
        # function that also asks about instance status is how a guard
        # silently stops guarding (see `_verify_window_gone`).
        run_session = str(plan.get("run_session") or "")
        live_titles.add(run_session)
        try:
            data = _read_verify_results(plan)
            if data and data.get("finished"):
                results = data.get("results")
                tested, expected = _verify_run_trees(plan)
                done = _test_plans.finish_run(
                    pid,
                    results if isinstance(results, list) else [],
                    tested_sha=tested,
                    expected_sha=expected,
                    target=_test_plans.verify_target(plan.get("repo_root") or ""),
                )
                # Only when the store actually took it. `finish_run` answers
                # None for a plan that was deleted while the run was working,
                # and announcing that one would toast about a checklist that is
                # no longer there.
                if done:
                    # THE ANSWER IS EATEN, not left on disk. This file is the
                    # session's whole return channel and the poller believes the
                    # first `finished: true` it sees, so a copy left behind is a
                    # loaded gun: the next run of the same plan is finished by
                    # its predecessor within 60s — before the agent has checked
                    # anything out — and takes the old verdict as the new one.
                    # `/run` clears it too, but only while an engine record still
                    # points at the worktree, which is exactly what a sweep, a
                    # cancel or a restart takes away.
                    _clear_verify_results(run_session)
                    _announce_test_plan_checked(done)
                continue
            started = _test_plan_run_started_at(plan)
            # THE WINDOW IS GONE — the commonest way a run dies, and the one the
            # two-hour clock served worst. An agent that hits a usage limit, is
            # killed in its pane, or whose tmux server went down leaves the
            # engine record intact, so nothing here noticed: the row said "an
            # agent is checking the steps it can" and offered Watch onto a dead
            # pane for two hours. A single miss is not proof (tmux can be
            # briefly unreachable, and a session is created a moment before it
            # is recorded), so the miss has to persist.
            if run_session and _verify_window_gone(run_session):
                _release_wedged_run(
                    pid,
                    run_session,
                    # SHORT FIRST SENTENCE, deliberately: the collapsed row
                    # lifts sentence one and nothing else (`errorHeadline`), so
                    # a paragraph-long opener is a release nobody can see.
                    "The verify session's agent window is gone. Nothing was "
                    "running any more, and nothing it may have found was "
                    "recorded — run it again.",
                )
                continue
            if started and time.time() - started > _TEST_PLAN_RUN_GIVE_UP_S:
                # NOT SILENTLY. This is a real session that was started, was
                # billed for two hours and never wrote an answer; releasing the
                # plan without a word turned that into "nobody has checked it
                # yet", which reads as the button never having been pressed.
                # The sentence lands on the plan (the row says it) and on the
                # bus (so it reaches someone who is not looking at the dialog —
                # which, two hours in, is everyone).
                hours = int(_TEST_PLAN_RUN_GIVE_UP_S // 3600) or 1
                _release_wedged_run(
                    pid,
                    run_session,
                    "The verify run was given up on after %d hours. %s never "
                    "wrote its answers, so nothing it may have found was "
                    "recorded — run it again." % (hours, run_session or "The session"),
                    hours=hours,
                )
        except Exception:  # noqa: BLE001 — one bad plan can't stop the pass
            pass
    # A cursor, not a cache: anything not still running this pass is forgotten,
    # so the map cannot outgrow the runs it is watching.
    for key in [k for k in _VERIFY_DEAD_SINCE if k not in live_titles]:
        _VERIFY_DEAD_SINCE.pop(key, None)


def _liveness_order(plans: list) -> list:
    """The ``generated`` plans, least-recently-asked first.

    The rotation that makes :data:`_TEST_PLAN_LIVE_BUDGET_S` fair. A pass that
    simply stopped at the budget would re-ask the same head of a fixed list
    every minute and never reach the tail, so the plans at the back would wait
    for their work to go live forever. Sorting by when each plan was last asked
    turns the budget into a round-robin: whatever got skipped last time is at
    the front this time. Never-asked plans (0.0) go first, and within a tie the
    store's own newest-first order stands.
    """
    candidates = [p for p in plans if p["state"] == "generated"]
    ids = {p["id"] for p in candidates}
    # Forget plans that have left ``generated`` (marked due, deleted, pruned) so
    # this map cannot outgrow the store it is a cursor into.
    for key in [k for k in _TEST_PLAN_LIVE_CHECKED if k not in ids]:
        _TEST_PLAN_LIVE_CHECKED.pop(key, None)
    return sorted(candidates, key=lambda p: _TEST_PLAN_LIVE_CHECKED.get(p["id"], 0.0))


def _check_test_plans_for_liveness(plans: list) -> None:
    """Ask origin whether any waiting plan's work has shipped, within a budget.

    The expensive phase: each plan costs a ``git fetch`` (up to
    ``test_plans.TIMEOUT_FETCH``, 120s) and sometimes a ``gh`` call on top, and
    those caps are per CALL — a dozen plans waiting on a remote that blackholes
    TCP add up to a pass measured in tens of minutes, which is the whole loop
    gone, not just this half of it. So the phase gets a wall-clock budget and
    stops when it runs out; :func:`_liveness_order` makes sure the plans it did
    not reach are the ones it starts with next minute.

    The "last asked" stamp is written BEFORE the check, not after: a plan whose
    repo hangs for the full timeout has been asked, expensively, and must go to
    the back of the queue rather than monopolize the head of it.
    """
    # Nothing can reach a live branch without git; skip the fetch storm entirely
    # on a machine that hasn't got it.
    if not git_available():
        return
    # Paused. "Off" has to mean the feature is quiet, not merely that no NEW
    # plans get written: a paused Verify that still fetched every minute and
    # kept moving plans into `due` would carry on lighting the top-bar badge
    # with work the user has just said they do not want chased. Nothing is lost
    # — the plans keep their state and the next enabled pass picks them up.
    if not _verify_enabled():
        return
    # A DUE plan follows the setting too — when nobody has answered anything.
    # `_liveness_order` never visits `due` (there is nothing to ask origin
    # about), so without this pass a checklist that went due against the OLD
    # branch — the observed case went due on `main` for a PR that had merged
    # into `staging` — sat in the badge telling the user to "change the live
    # branch on this repo's card" while the changed card changed nothing.
    # `retarget_live_branch` owns the gate (a plan with a settled answer keeps
    # its branch) and puts the mover back to waiting, where the rotation below
    # picks it up on the next pass. Costs a settings read per due plan, no git.
    for plan in plans:
        if plan.get("state") != "due":
            continue
        try:
            _test_plans.retarget_live_branch(
                plan["id"],
                _test_plans.resolve_live_branch(plan.get("repo_root") or ""),
            )
        except Exception:  # noqa: BLE001 — one unreadable plan must not stall the pass
            pass
    deadline = time.monotonic() + _TEST_PLAN_LIVE_BUDGET_S
    for plan in _liveness_order(plans):
        if time.monotonic() >= deadline:
            break
        pid = plan["id"]
        try:
            # WHICH BRANCH ARE WE EVEN WATCHING? Asked every pass, because the
            # answer is a live setting: a repo re-pointed from `staging` to
            # `main` must re-aim the checklists that have not gone due yet, or
            # the setting is a lie for exactly the plans that exist right now.
            # `retarget_live_branch` owns the gate (and clears any merge stamp,
            # which was recorded against the branch we just stopped watching).
            moved = _test_plans.retarget_live_branch(
                pid, _test_plans.resolve_live_branch(plan.get("repo_root") or "")
            )
            if moved is not None:
                plan = moved
            # MERGED AND DEPLOYED ARE TWO FACTS. Ancestry is true the instant a
            # PR lands; what a checklist tests is a running service the pipeline
            # reaches minutes later, and a plan marked due in that window gets
            # answered against the behaviour the change replaces — a FAIL
            # recorded on correct code, which is the one outcome this surface
            # cannot survive. So the merge starts a clock and the clock is what
            # marks it due.
            if not plan.get("merged_at"):
                # Not seen merged yet: this is the expensive question (a fetch,
                # sometimes a `gh` call), so it is the only branch that is
                # rate-limited by the rotation stamp below.
                _TEST_PLAN_LIVE_CHECKED[pid] = time.time()
                if not _test_plan_is_live(plan):
                    continue
                if _test_plans.mark_merged(pid) is None:
                    continue  # deleted while we were asking origin
                plan = _test_plans.get(pid) or plan
            # ...and from here on there is nothing left to ask origin. A plan
            # waiting out its deploy window costs one clock comparison per pass
            # instead of a fetch, so adding the wait made this loop cheaper.
            delay = _test_plans.resolve_deploy_delay(plan.get("repo_root") or "")
            if not _test_plans.deploy_ready(plan, delay):
                continue
            due = _test_plans.mark_due(pid)
            if due is None:
                continue  # deleted while it was waiting
            _events.BUS.emit(
                "session.test_plan_due",
                session=pid,
                data={
                    "plan": pid,
                    "title": due["title"],
                    "live_branch": due["live_branch"],
                },
            )
            _notify_test_plan_due(due)
        except Exception:  # noqa: BLE001 — one bad plan can't stop the pass
            pass


def _check_test_plan_landings(plans: list) -> None:
    """Refresh "which branch has this reached on origin" for the cards that show it.

    A SEPARATE PASS FROM LIVENESS, over a different set of plans, on purpose.
    The liveness rotation visits ``generated`` plans only — everything else has
    nothing left to ask origin *about the branch it ships from*. But every card
    on the surface shows where its work landed, including the ones that are due,
    running or answered, and a checklist written before this existed has no
    answer at all. So this walks all of them.

    What stops it being expensive:

    * **A plan that has reached its own live branch is finished being asked.**
      That is the end of the road this question is tracking; a later landing on
      some other branch is not what the card is for.
    * **:data:`_TEST_PLAN_LANDED_TTL_S`** — minutes, not every tick. Nothing acts
      on this answer, so nothing is hurt by it being a few minutes old.
    * **The fetch is per repository, not per plan** (``fetch_all_heads``), so a
      dozen checklists in one repo cost one fetch between them.
    * **A wall-clock budget**, least-recently-asked first, exactly like the
      liveness pass — and for the same reason: the per-call caps are per CALL,
      and a remote that blackholes TCP would otherwise spend the whole loop here.
    """
    if not git_available() or not _verify_enabled():
        return
    live = {p["id"] for p in plans}
    for key in [k for k in _TEST_PLAN_LANDED_CHECKED if k not in live]:
        _TEST_PLAN_LANDED_CHECKED.pop(key, None)
    now = time.time()
    todo = []
    for plan in plans:
        if not (plan.get("repo_root") and plan.get("branch") and plan.get("sha")):
            continue
        landed = str(plan.get("merged_into") or "")
        if landed and landed == str(plan.get("live_branch") or ""):
            continue
        if (
            now - _TEST_PLAN_LANDED_CHECKED.get(plan["id"], 0.0)
            < _TEST_PLAN_LANDED_TTL_S
        ):
            continue
        todo.append(plan)
    todo.sort(key=lambda p: _TEST_PLAN_LANDED_CHECKED.get(p["id"], 0.0))
    deadline = time.monotonic() + _TEST_PLAN_LANDED_BUDGET_S
    for plan in todo:
        if time.monotonic() >= deadline:
            break
        # Stamped BEFORE the ask, like the liveness cursor: a plan whose repo
        # hangs for the full timeout has been asked, expensively, and must go to
        # the back of the queue rather than monopolize the head of it.
        _TEST_PLAN_LANDED_CHECKED[plan["id"]] = time.time()
        try:
            found = _test_plan_merged_into(plan)
            _test_plans.set_merged_into(
                plan["id"],
                found.get("branch") or "",
                float(found.get("at") or 0.0),
                list(found.get("all") or []),
            )
        except Exception:  # noqa: BLE001 — one bad repo can't stop the pass
            pass


def _test_plans_due_pass(full: bool = True) -> bool:
    """One pass of the due loop. Blocking; runs via ``to_thread``.

    Two jobs on one cadence, deliberately not two loops: both are "look at every
    plan and see whether the world moved", and a second 60s timer would only
    double the wakeups. They are two PHASES rather than one interleaved loop
    because only one of them talks to the network — see
    :func:`_poll_running_test_plans` for what that coupling cost.

    ``full`` is what lets the two phases keep one loop while running at two
    speeds. A pass with ``full=False`` does the local half ONLY: it reads the
    results file of anything that is running, and touches neither the network nor
    the store's housekeeping. That is the pass the loop runs every few seconds
    while a verify session is in flight, so an agent that has just finished on
    the user's screen is reflected almost immediately instead of up to a minute
    later. The expensive half — prune, stalled-generation recovery, and the
    ``git fetch`` per waiting plan — stays strictly on the minute.

    Returns whether anything is still running, which is how the loop picks its
    next sleep without a second store read.

    Every plan is wrapped on its own. A repo that was deleted, a remote that
    hangs, a run session in a broken state — none of them may stop the pass, or
    one bad plan silently switches the feature off for every other one.
    """
    if full:
        # Release plans whose verify session no longer exists (the user deleted
        # it mid-run, so nothing is left to write the results file) and enforce
        # the store's cap. Explicitly NOT a liveness prune of the plans
        # themselves — plans are supposed to outlive their sessions; see
        # test_plans.prune.
        try:
            _test_plans.prune(list(ENGINE.instances.keys()))
        except Exception:  # noqa: BLE001
            pass
    plans = _test_plans.list_plans()
    # Cheap and local (no network, no model call on this thread), so it runs
    # before the phase that can spend the whole pass talking to origin: a plan
    # abandoned mid-generation must not have to wait behind a fetch storm to be
    # picked back up.
    if full:
        _recover_stalled_test_plans(plans)
    _poll_running_test_plans(plans)
    if full:
        _check_test_plans_for_liveness(plans)
        # After liveness, never before: that phase is the one with a deadline
        # the user feels (a plan going due, a push to a phone), and this one is
        # a label on a card. Both are budgeted, so the worst case is that a
        # slow remote costs the label a pass and not the other way round.
        _check_test_plan_landings(plans)
    # Re-read rather than reusing ``plans``: the phase above may have just folded
    # a finished run in, and sleeping four seconds because of the state it held
    # on the way IN would keep the fast cadence one tick longer than it is owed.
    try:
        return any(p["state"] == "running" for p in _test_plans.list_plans())
    except Exception:  # noqa: BLE001 — an unreadable store is not a reason to spin
        return False


# How old an unreferenced verify session must be before the sweeper may close
# it. The window it protects is run_test_plan's: the session exists from
# `create_instance` until `start_run` writes it onto the plan, and a sweep
# landing inside that gap would close a run the user started seconds ago. The
# gap is really milliseconds; fifteen minutes is deliberate overkill, because
# the sessions this exists for have been sitting unreferenced for DAYS.
_VERIFY_ORPHAN_GRACE_S = 15 * 60


async def _sweep_orphan_verify_sessions() -> None:
    """Close verify sessions that no plan remembers any more.

    The symmetric half of ``test_plans.prune``: prune releases a plan whose
    verify session is gone, and this closes a verify session whose plan is
    gone. Plans forget their sessions on paths that never reach
    ``_end_verify_session`` — ``ensure_plan_for`` replaces a plan wholesale
    when its session moves to a new branch, and the store's MAX_PLANS cap
    evicts old plans entirely — and the core module cannot end sessions itself
    (the engine owns them). What that stranded: an agent session, invisible in
    the rail (the sidebar hides ``verify-*`` on purpose), with no card left in
    the Verify dialog offering to end it, alive for days.

    A session is kept while ANY plan references it — the in-flight
    ``run_session`` or any run record's ``session`` (a finished run's session
    deliberately stays open for the user to read). Closing goes through
    ``_end_verify_session``: a close, not a delete, so a mistaken sweep is one
    click from coming back via recently-closed.
    """
    try:
        plans = _test_plans.list_plans()
    except Exception:  # noqa: BLE001 — an unreadable store must not close anything
        return
    referenced = set()
    for plan in plans:
        title = str(plan.get("run_session") or "")
        if title:
            referenced.add(title)
        for run in plan.get("runs") or []:
            title = str((run or {}).get("session") or "")
            if title:
                referenced.add(title)
    for title in list(ENGINE.instances.keys()):
        # The prefix run_test_plan builds titles from ("verify-%s" % plan_id).
        if not title.startswith("verify-") or title in referenced:
            continue
        inst = ENGINE.instances.get(title)
        created = getattr(inst, "CreatedAt", None)
        if created is None:
            # Unknown age — never close on a question we could not ask.
            continue
        try:
            age = (_datetime.datetime.now(created.tzinfo) - created).total_seconds()
        except Exception:  # noqa: BLE001 — a broken timestamp is not evidence of age
            continue
        if age < _VERIFY_ORPHAN_GRACE_S:
            continue
        try:
            if await _end_verify_session(title) and log.ErrorLog is not None:
                log.ErrorLog.Printf(
                    "verify: closed orphaned session %s (no plan references it)",
                    title,
                )
        except Exception:  # noqa: BLE001 — one bad session can't stop the sweep
            pass


async def _test_plans_due_loop() -> None:
    """Watch every waiting test plan until its work goes live, and poll the
    running ones (started by the lifespan).

    Work first, sleep after, like the autopilot loop: a plan whose PR merged
    while the server was down should come due within a minute of startup rather
    than after the first full interval.

    Two cadences, one loop. The full pass runs on its own minute regardless; in
    between, and only while something is actually running, the loop wakes on
    ``_TEST_PLAN_RUN_POLL_S`` to do the local half. The clock for the full pass
    is monotonic and independent of the fast ticks, so a long-running verify
    session cannot starve the liveness checks and a burst of fast ticks cannot
    bring a fetch storm forward.
    """
    next_full = 0.0
    while True:
        running = False
        try:
            full = time.monotonic() >= next_full
            if full:
                next_full = time.monotonic() + _TEST_PLAN_DUE_INTERVAL
            running = await asyncio.to_thread(_test_plans_due_pass, full)
            # On the minute, with the rest of the housekeeping — never on the
            # fast ticks, which exist only to read result files quickly.
            if full:
                await _sweep_orphan_verify_sessions()
        except Exception:  # noqa: BLE001 — the loop must never die
            pass
        await asyncio.sleep(
            _TEST_PLAN_RUN_POLL_S if running else _TEST_PLAN_DUE_INTERVAL
        )


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
    # Epoch when the activity state last changed (from the pane-hash
    # record), so the UI can rank how long a session has been waiting
    # (attention ordering + wedged-session watchdog). 0 = unknown.
    _act_rec = _ACTIVITY_CACHE.get(i.Title) or {}
    d["activity_since"] = float(_act_rec.get("changed_epoch") or 0.0)
    # L3: latest-turn snippet (≤120 chars) for N-session triage, or null.
    d["last_turn"] = _session_last_turn_cached(i)
    # The newest USER prompt (first line) — the panes pin it above the
    # terminal — and its whole body for the pin's hover/click expansion.
    d["last_prompt"] = _session_last_prompt_cached(i)
    d["last_prompt_full"] = _session_last_prompt_full_cached(i)
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
    "last_turn",
    "last_prompt",
    "last_prompt_full",
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
    d["tokens"] = d["tokens_in"] = 0
    d["tokens_cache_read"] = d["tokens_cache_write"] = 0
    d["tokens_ctx"] = d["tokens_ctx_window"] = 0
    d["tokens_cost"] = 0.0
    d["tokens_model"] = ""
    d["budget"] = _budget_status_for(i.Title, 0.0)
    d["activity"] = "idle"
    _act_rec = _ACTIVITY_CACHE.get(i.Title) or {}
    d["activity_since"] = float(_act_rec.get("changed_epoch") or 0.0)
    d["last_turn"] = None
    d["last_prompt"] = None
    d["last_prompt_full"] = None
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
        return [_session_snapshot(i, queues) for i in insts]
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(8, len(insts)), thread_name_prefix="session-probe"
    ) as ex:
        return list(ex.map(lambda i: _session_snapshot(i, queues), insts))


@app.get("/api/instances")
def list_instances() -> JSONResponse:
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
    network on this path)."""
    cached = _events.sessions_snapshot()
    if time.time() - _SNAPSHOT_AT <= _INSTANCES_TICK_INTERVAL * 2.5:
        if {d.get("title") for d in cached} == set(ENGINE.instances.keys()):
            return JSONResponse(cached + _remote.merged_instances() + _pending_rows())
    queues = _prompt_queue.snapshot()
    by_title = {d.get("title"): d for d in cached}
    snap = [
        _session_snapshot_cheap(i, queues, by_title.get(i.Title))
        for i in list(ENGINE.instances.values())
    ]
    return JSONResponse(snap + _remote.merged_instances() + _pending_rows())


# ---- Tailnet multi-device control (backend.web.core.remote) -------------- #
@app.get("/api/remote/hello")
def remote_hello() -> JSONResponse:
    """Public identity ping other MindFlock devices use for discovery."""
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
    """Forget a device's stored token (its sessions drop off the sidebar)."""
    _remote.forget_device(device)
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

    Deliberately does NOT call :func:`_forget_probes`. That would pop
    ``_ACTIVITY_CACHE[title]`` — the only source of ``activity_since``, which
    feeds attention ordering and the wedged-session watchdog — and drop the
    token memo, for no benefit: ``_session_stage_fresh`` already guarantees the
    stage is current. Do not re-add it.

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


# _mobile_svg / _mobile_info moved to core.mobile_access (imported above).


@app.get("/api/mobile")
def get_mobile() -> JSONResponse:
    """Mobile (/m) URLs + a scannable QR for phone access (Settings → Mobile)."""
    try:
        return JSONResponse(_mobile_info())
    except Exception:  # noqa: BLE001 — never 500 the settings screen
        return JSONResponse(
            {"urls": [], "qr_svg": None, "token": "", "note": "unavailable"}
        )


@app.post("/api/server/restart")
def post_server_restart() -> JSONResponse:
    """Re-exec the server process so a changed serve mode (Settings → Mobile
    toggle) takes effect without the user finding the right terminal.

    Safe because the actual work lives outside this process: agent sessions
    are tmux sessions, ingestion is its own process, and state is on disk.
    Clients (desktop app, /m) already retry until the server answers again.

    The re-exec deliberately drops the mode from both places it could linger —
    ``CS_WEB_MODE`` (exported by run.py at boot) and any mode token in argv —
    so the fresh process falls through to the *persisted* general.serve_mode
    instead of resurrecting the mode this process happened to boot with.
    """
    # An explicit restart is a fresh intent: whatever the automatic
    # tailscale-mode retries (core.restart) already spent, this one starts over.
    _restart.reset_tailscale_attempts()
    _restart.reexec_soon()
    return JSONResponse({"ok": True, "restarting": True})


# --------------------------------------------------------------------------- #
# System logs (Settings → System logs)
# --------------------------------------------------------------------------- #
# _LOG_TAIL_MAX / _log_sources / _read_log_tail moved to core.system_logs
# (imported above).


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


# _provider_label / _usage_window_for / _provider_usage_entry moved to
# core.usage_api (imported above).


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
    Probed per-request (cheap) so installing/connecting takes effect on the
    next page load without a server restart.
    """
    return {
        "git": git_available(),
        "tailscale": shutil.which("tailscale") is not None,
        "ticketing": _ticketing_connected(),
        "github": _github_pr_available(),
    }


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

        for src in _settings.load_settings().ticketing.sources:
            if (
                getattr(src, "id", "") == source_id
                or getattr(src, "provider", "") == source_id
            ):
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
    title: str, depth: str, source: str, item: str, message: str = ""
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
            retryable=_precommit_retry_hooks(),
            boot=_SERVER_BOOT_ID,
        )
    except Exception:  # noqa: BLE001 — never fail a launch over the autopilot
        pass


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
    """Persist that first-run is done (idempotent, best-effort)."""
    try:
        from backend.config import settings as _settings

        if not _settings.load_settings().general.onboarded:
            _settings.update_settings(general={"onboarded": True})
    except Exception:  # noqa: BLE001 — never block session creation on this
        pass


# --- Plain (non-provisioned) repo selection -----------------------------------
# _is_git_repo / _git_has_commits / _make_initial_commit moved to core.git_ops.
# _prepare_plain_repo / _raise_on_blocked_repo moved to core.plain_repo
# (imported above).

# _PASTE_KEEP / _paste_dirs / _prune_pastes / _clear_all_pastes /
# _safe_upload_name moved to core.uploads (imported above).


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
        base = None
        if inst is not None:
            try:
                folder = inst.GetWorktreePath() if inst.Started() else (inst.Path or "")
            except Exception:  # noqa: BLE001
                folder = inst.Path or ""
            if folder and os.path.isdir(folder):
                base = os.path.join(folder, ".mindflock_pastes")
                _exclude_artifacts(folder)  # keep pastes out of the agent's commits
        if base is None:
            base = os.path.join(os.path.expanduser("~"), ".mindflock", "pastes")
        os.makedirs(base, exist_ok=True)
        stamp = _datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        # Keep the original (sanitized) name visible so the agent knows what it
        # got; the paste-<stamp>-<hex> prefix keeps retention pruning working.
        tail = "-" + orig if orig else ext
        path = os.path.join(base, "paste-%s-%s%s" % (stamp, os.urandom(3).hex(), tail))
        with open(path, "wb") as f:
            f.write(data)
        # Retention: keep only the newest _PASTE_KEEP pastes in this directory.
        _prune_pastes(base)
        return path

    path = await asyncio.to_thread(_store)
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

    def _touched_at(inst) -> float:
        """Sort key for live sessions: last-touched epoch, 0 when unknown."""
        try:
            return inst.UpdatedAt.timestamp()
        except Exception:  # noqa: BLE001 — an unset/odd timestamp just sorts last
            return 0.0

    def _gather() -> list:
        # Most-recent first: the folder the last session (or wizard run) used,
        # then live sessions newest-touched first, then the closed-session undo
        # store — which is already newest-first. Every tier is best-effort, so a
        # broken one costs its own suggestions and not the whole list.
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
        # Blocking work (a readdir per scan root plus a git probe per surviving
        # candidate) — threaded like paste_image's _store so a cold disk or a
        # stalled network mount can't hold up every other request.
        return suggest_repos(recent_paths=recent, cwd=os.getcwd())

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


# There is deliberately no endpoint for the frontend to set general.onboarded.
# The flag means "has created a session" — create_instance sets it via
# _mark_onboarded, and backend/init_wizard.py pointedly does not — so letting a
# UI surface flip it from outside that one event destroyed both first-run helpers
# (the grid's setup card and the auto-opening dependency checklist) for a user
# who had created nothing yet and still had, say, no tmux to create it with.


# MindFlock automation control (/api/mindflock/*) moved to the MindFlock addon.


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
    the profile's baked-in launch FLAGS (e.g. an OpenRouter model pin).

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
        provisioning.write_launcher(
            wt,
            prompt,
            program=inst.Program or "claude",
            skip_permissions=scs.skip_permissions,
            cache_env=_ws.merged_cache_env(scs.caches),
            launch_args=tuple(prof_args) + tuple(getattr(inst, "LaunchArgs", ()) or ()),
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


@app.post("/api/instances")
async def create_instance(payload: dict) -> JSONResponse:
    """Create a session and Start it in the background (returns 202 immediately).

    Accepted ``payload`` keys:

    * ``title`` — session name (also the ENGINE.instances key). Blank quick-
      launches an auto-numbered ``untitled`` / ``untitled-N``. For a provisioned
      session a title that is itself a branch path (``feature/sc-123/foo``,
      matching ``^[A-Za-z0-9._/-]+$``) is used verbatim as ``new_branch`` and the
      title becomes its last segment.
    * ``program`` — agent CLI to launch (defaults to the engine default).
    * ``provisioned`` — provisioned mode (git base-clone + per-session worktree/
      clone); requires git and either a configured ``[repository].url`` or a
      chosen ``repo_path``.
    * ``workspace_strategy`` — ``"worktree"`` (default) or ``"clone"``.
    * ``story_id`` — ticket id; seeds a default title/branch when the title is
      blank.
    * ``prompt`` — initial prompt (held in the prompt queue instead of seeded
      directly when the worktree declares a setup pass, so it survives setup).
    * ``repo_path`` — a user-chosen local repo to base the session on.
    * ``in_place`` — run directly in ``repo_path`` (no worktree); forced on for a
      non-git folder. Ignored in provisioned mode.
    * ``init_repo`` — ``git init`` an empty folder first (plus an initial commit).
      Combines with ``in_place``: init the folder and then work directly in it.
    * ``launch_args`` — per-session agent flags; absent means inherit the global
      default, present (even ``[]``) means use exactly these.
    * ``profile_id`` — auth profile the agent runs under; absent/blank means
      inherit the global default profile, ``"default"`` pins the CLI's own
      ambient login, anything else must name a configured profile.
    * ``profile_model`` — this session's model override of the profile's own
      model pin (e.g. an OpenRouter model id); blank keeps the pin.

    The three creation modes are provisioned, plain-worktree, and in-place. A
    409 is returned when the title already exists; the instance registers as
    Loading and its real Start (worktree/clone + provisioning + tmux) runs in a
    background task, so a failure is surfaced via a ``session.create_failed``
    event rather than in the 202 response.
    """
    payload = payload or {}
    title = (payload.get("title", "") or "").strip()
    program = (payload.get("program", "") or "").strip() or ENGINE.default_program()

    # --- Optional provisioned mode --------------------------------------------
    is_provisioned = bool(payload.get("provisioned", False))
    workspace_strategy = (payload.get("workspace_strategy") or "worktree").strip()
    story_id = str(payload.get("story_id", "") or "").strip()
    prompt = payload.get("prompt", "") or ""
    repo_path = str(payload.get("repo_path", "") or "")
    new_branch = ""
    # Per-session launch flags (e.g. --dangerously-skip-permissions for just
    # this session), appended after the provider's own saved defaults every time
    # this session's agent (re)starts. The New dialog pre-fills this field with
    # the global default and always sends the key, so what arrives IS the
    # session's flags — a default the user toggled off is honored, not
    # re-applied. When the key is ABSENT (other creators / API callers), we pass
    # None so the session inherits the global default (Settings → Coding CLI).
    # Validated with the same rules as provider-level saved args so a malformed
    # payload never reaches the shell command builder.
    if "launch_args" in payload:
        try:
            launch_args = provider_config.validate_launch_args(
                payload.get("launch_args") or []
            )
        except ValueError as err:
            return JSONResponse({"error": str(err)}, status_code=400)
    else:
        launch_args = None  # not specified -> inherit the global default

    # Auth profile pin. Rejecting an unknown id HERE beats a session that
    # launches half-authenticated and only fails when the CLI does.
    profile_id = str(payload.get("profile_id", "") or "").strip()
    err = _profile_id_error(profile_id)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    profile_model = str(payload.get("profile_model", "") or "").strip()
    err = _profile_model_error(profile_model)
    if err:
        return JSONResponse({"error": err}, status_code=400)

    if is_provisioned:
        # Provisioning is all git (base clone + worktree/clone per session).
        if not git_available():
            return JSONResponse(
                {
                    "error": "provisioned mode needs git installed — install git, "
                    "or start a plain session (any folder works)"
                },
                status_code=400,
            )
        # Provisioning works for the configured [repository].url OR any local
        # repo the user picked (repo_path). Only the no-repo-chosen flow needs
        # the config to resolve.
        if not repo_path and not provisioning.provisioning_available():
            return JSONResponse(
                {
                    "error": "provisioned mode needs a configured repository "
                    "(config.toml [repository].url) or a chosen local repo"
                },
                status_code=400,
            )
        if workspace_strategy not in ("worktree", "clone"):
            return JSONResponse(
                {"error": "workspace_strategy must be 'worktree' or 'clone'"},
                status_code=400,
            )
        # If the Name field is itself a full branch (e.g. a Shortcut ticket
        # branch like "feature/sc-17436/grafana-dashboard-…"), use it verbatim
        # as the branch and set the session name to just its last segment.
        if title and "/" in title and re.match(r"^[A-Za-z0-9._/-]+$", title):
            new_branch = title.strip("/")
            title = new_branch.split("/")[-1]
        else:
            # Default the title from the story id when one is given.
            if not title and story_id:
                title = "sc-%s" % story_id
            if title:
                # Deterministic branch: feature/sc-<id>/<slug> with a story,
                # else mindflock/<title>.
                new_branch = provisioning.branch_name_for(story_id or None, title)

    # Whether WE invented this name. A title the caller typed is theirs and a
    # collision is theirs to hear about; one we generated is ours to make unique,
    # which is what the numbering below is for — and what the claim further down
    # has to keep doing rather than answering 409 for a request in which nobody
    # typed a name at all.
    auto_title = not title
    if not title:
        # Quick launch: an empty Name starts an "untitled" session, numbered to
        # stay unique (titles key ENGINE.instances).
        title = _free_untitled()
        # Provisioned sessions derive their branch from the title; the empty
        # title skipped that above, so derive it from the generated one.
        if is_provisioned and not new_branch:
            new_branch = provisioning.branch_name_for(story_id or None, title)
    if title in ENGINE.instances:
        return JSONResponse(
            {"error": "instance %s already exists" % title}, status_code=409
        )

    # --- Sessions based off a user-chosen local repo --------------------------
    # Without this, plain sessions would default to the server's own cwd (the
    # mindflock repo). The session still uses CS's isolated-worktree model — the
    # worktree is created off the picked repo's HEAD on a fresh branch. A
    # provisioned session with a chosen repo runs the SAME provisioning
    # (setup commands / cache env) against that repo (universal flow).
    plain_path = "."
    provision_repo = ""
    in_place = False
    git_enabled = True
    if repo_path or not is_provisioned:
        in_place = bool(payload.get("in_place", False)) and not is_provisioned
        # Combinable with in_place, deliberately: "git init this folder, then work
        # directly in it" is the natural way to start a brand-new project, and
        # suppressing the init for in-place sessions silently dropped the tick and
        # handed the user a git-less session in the folder they had just asked to
        # make a repo of. _prepare_plain_repo inits + makes the initial commit, so
        # the in-place session comes up on a real branch with git features on.
        # The pair that genuinely cannot coexist is in_place and provisioning
        # (a separate worktree/clone), which the line above already enforces.
        init_repo = bool(payload.get("init_repo", False))
        try:
            plain_path, git_enabled = await asyncio.to_thread(
                _prepare_plain_repo, repo_path, init_repo
            )
        except ValueError as err:
            return JSONResponse({"error": str(err)}, status_code=400)
        # A non-git folder has no HEAD to fork a worktree from and can't be
        # provisioned — run it in-place, with git features simply disabled.
        if not git_enabled:
            if is_provisioned:
                return JSONResponse(
                    {
                        "error": "provisioning needs a git repo — pick a git repo, or "
                        "tick 'Create a git repo in this folder' in Advanced"
                    },
                    status_code=400,
                )
            in_place = True
        if is_provisioned:
            provision_repo = plain_path

    inst = session.NewInstance(
        session.InstanceOptions(
            title=title,
            path=plain_path,
            program=program,
            provisioned=is_provisioned,
            workspace_strategy=workspace_strategy,
            provision_repo=provision_repo,
            new_branch=new_branch,
            prompt=prompt,
            launch_args=launch_args,
            in_place=in_place,
            profile_id=profile_id,
            profile_model=profile_model,
        )
    )
    # O4: every session gets a deterministic dev-server port block, injected
    # into the agent's tmux env at launch (PORT / MINDFLOCK_PORT_BASE).
    inst.ExtraEnv = _ports.env_for(title)

    # O2: per-worktree setup (repo-committed .mindflock.toml [workspace]).
    # Plain worktree sessions only — provisioned workspaces run their own
    # setup, and in-place sessions share the repo dir (deps already there).
    setup_cfg = None
    if git_enabled and not is_provisioned and not in_place:
        setup_cfg = _wt_setup.load_config(plain_path)
    # Start does the heavy lifting (git worktree/clone + provisioning + tmux),
    # which can take minutes on the first worktree run (one-time base clone +
    # uv sync). Register the instance as "loading" and run Start in the
    # background so the create request returns immediately and the session shows
    # as provisioning in the grid instead of freezing the dialog on "Creating…".
    inst.SetStatus(Loading)
    # THE TITLE IS CLAIMED UNDER THE LOCK, AND RE-CHECKED THERE. The 409 above
    # is a read with no claim, and everything between it and here can await —
    # `_prepare_plain_repo` alone is a whole thread hop — so two creates for one
    # title (two tabs, a stale row, the same Run pressed twice) both passed the
    # gate and the second overwrote the first's record. Nothing then owned the
    # first session: its tmux and its worktree carried on, invisible, and the
    # next attempt at that title died in Start with "tmux session already
    # exists". That is where the orphan verify sessions come from, and it is why
    # this re-check is not merely defensive.
    with ENGINE.lock:
        if title in ENGINE.instances:
            # A NAME WE INVENTED IS OURS TO RE-INVENT. The numbering above ran
            # before the thread hop, so two quick launches inside that window
            # both derived "untitled" and the second would have been refused —
            # a hard error for a request in which the user typed nothing at all.
            # Provisioned sessions are excluded because their BRANCH is derived
            # from the title too, and re-naming here would leave the two saying
            # different things.
            if auto_title and not is_provisioned:
                title = _free_untitled()
                inst.Title = title
            else:
                return JSONResponse(
                    {"error": "instance %s already exists" % title}, status_code=409
                )
        ENGINE.instances[title] = inst

    if setup_cfg is not None and setup_cfg.has_setup and prompt:
        # Hold the initial prompt until setup succeeds: deliver it via the
        # prompt queue (drained only once setup is ok + the agent is idle)
        # instead of seeding the agent CLI directly. A failed setup keeps
        # the prompt visible in the queue rather than losing it.
        #
        # AFTER the claim above, not before: a create that loses the race must
        # not leave its prompt in the winner's queue, which is a real prompt
        # sent to a real agent by a request that answered 409.
        try:
            inst.Prompt = ""
            _prompt_queue.enqueue(title, prompt)
            _prompt_queue.set_flags(title, enabled=True)
        except Exception as err:  # noqa: BLE001
            # A FULL OR UNWRITABLE QUEUE MUST NOT COST THE SESSION. Both calls
            # can raise (`prompt_queue._save` re-raises, and `enqueue` refuses a
            # full queue), and this is the one window where an exception is
            # unrecoverable: the title is claimed and `_bg_start` has not been
            # scheduled yet, so the session would sit in the list as Loading for
            # ever. Fall back to seeding the prompt the ordinary way — it loses
            # the "hold it until setup succeeds" guarantee, which is a smaller
            # loss than the session.
            inst.Prompt = prompt
            if log.ErrorLog is not None:
                log.ErrorLog.Printf(
                    "queueing the initial prompt for %s failed (%v) — seeding it "
                    "directly instead",
                    title,
                    err,
                )
    _mark_onboarded()  # first-ever session ends first-run; setup card won't auto-show again
    # Remember the folder this session chose so the NEXT New Session dialog opens
    # on it and the repo suggestions rank it first — the second session in a repo
    # should not be another walk down the folder tree. "." is the server's own
    # cwd (a provisioned session with no chosen repo), which the user never
    # picked, so it isn't worth remembering.
    if plain_path and plain_path != ".":
        try:
            from backend.config import settings as _settings

            _settings.update_settings(general={"last_repo_path": plain_path})
        except Exception:  # noqa: BLE001 — a convenience hint never fails a create
            pass

    async def _bg_start() -> None:
        try:
            await asyncio.to_thread(inst.Start, True)
            ENGINE.save()
            if setup_cfg is not None and setup_cfg.has_setup:
                try:
                    _wt_setup.start_setup(
                        title, plain_path, inst.GetWorktreePath(), setup_cfg
                    )
                except Exception:  # noqa: BLE001 — setup is best-effort
                    pass
        except Exception as err:  # noqa: BLE001
            if log.ErrorLog is not None:
                log.ErrorLog.Printf("failed to create instance %s: %v", title, err)
            # ONLY OUR OWN RECORD, and only OUR failure to report — see
            # :func:`_drop_failed_start`. A title that now belongs to a live
            # session must not be popped (that is how an orphan is minted) and
            # must not be reported (stamping "the verify session couldn't start"
            # on a plan whose agent is working is worse than saying nothing).
            # A title nobody owns is still ours to report: the session was
            # deleted while it provisioned, and the checklist waiting on it has
            # to hear that rather than sit in ``running`` until prune.
            if not _drop_failed_start(title, inst):
                return
            # ...and if a CHECKLIST was waiting on this session, tell the
            # checklist. `POST /run` answered 202 long before this point and
            # stamped the plan `running`, so without this the row goes on saying
            # an agent is checking something for the thirty seconds until
            # `prune` releases it — and then reverts with the reason recorded
            # nowhere. See `test_plans.fail_run`.
            try:
                pid = _test_plans.find_by_run_session(title)
                if pid:
                    _test_plans.fail_run(pid, title, str(err))
            except Exception:  # noqa: BLE001 — never mask the create failure
                pass
            # Surface the failure to watchers (UI toast, `mindflock events`,
            # the CLI's create poll) — a session silently vanishing from the
            # list is the worst failure mode.
            _events.BUS.emit(
                "session.create_failed", session=title, data={"error": str(err)}
            )

    # Tracked in _BG_TASKS so lifespan teardown cancels it (an untracked task
    # can also be GC'd mid-flight).
    _register_task(_bg_start())
    # Seed the *_changed diff snapshot with the initial state so the first real
    # transition (loading->running etc.) emits instead of being swallowed (F6).
    _seed_event_snapshot(title)
    _events.BUS.emit(
        "session.created",
        session=title,
        new="loading",
        data={
            "program": program,
            "provisioned": is_provisioned,
        },
    )
    body = _instance_json(inst)
    # An account with no route for this agent runs the session on the CLI's own
    # login. The New dialog says so at selection time; API and CLI callers had
    # no way to hear it at all, and a session quietly launching as the wrong
    # identity is the one outcome this feature cannot be silent about.
    try:
        from backend.providers import auth_profiles

        note = auth_profiles.unsupported_note(program or "", profile_id)
        if note:
            body["note"] = note
    except Exception:  # noqa: BLE001 — the note is enrichment only
        pass
    return JSONResponse(body, status_code=202)


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
        _aliases.merge(payload["aliases"])
        return JSONResponse({"aliases": _aliases.all_aliases()})
    title = str(payload.get("title") or "").strip()
    if not title:
        return JSONResponse({"error": "title is required"}, status_code=400)
    _aliases.set_alias(title, str(payload.get("alias") or "").strip())
    return JSONResponse({"aliases": _aliases.all_aliases()})


@app.delete("/api/instances/{title}")
async def delete_instance(title: str) -> JSONResponse:
    """Kill the session and remove it (worktree + branch via ``inst.Kill``).

    Drops it from the registry, persists the removal (merge-on-save excluding
    this title), and GCs its per-session state (event snapshot, budget, probes,
    branch baseline, port block). The editor window is closed only when no other
    live session still shares the worktree."""
    inst, err = _inst_or_404(title)
    if err is not None:
        return err
    try:
        wt = inst.GetWorktreePath()
    except Exception:  # noqa: BLE001
        wt = ""
    try:
        await asyncio.to_thread(inst.Kill)
    except Exception as err:  # noqa: BLE001
        # Even if kill partially failed, drop it from the active set.
        if log.ErrorLog is not None:
            log.ErrorLog.Printf("kill error for %s: %v", title, err)
    _kill_shell_session(title)
    # Keep the Cursor window open if another live session (e.g. a copy) still
    # shares this worktree — only close it when this is the last one on the dir.
    if not _worktree_in_use_by_other(wt, title):
        _close_cursor_window(wt)
        # GC the ~/.claude.json trust entry pre_trust_workdir seeded for this
        # worktree (G3) — guarded internally to MindFlock-owned paths only.
        await asyncio.to_thread(_remove_trust_entry, wt)
    with ENGINE.lock:
        ENGINE.instances.pop(title, None)
    # Merge-on-save with the killed title excluded removes it from disk without
    # the stale-set rewrite that storage.DeleteInstance would do (which would
    # both clobber other processes' instances and reconstruct/attach every other
    # session via LoadInstances).
    ENGINE.save(exclude_titles={title})
    _EVENT_SNAPSHOT.pop(title, None)
    _aliases.drop(title)
    _BUDGET_FIRED.pop(title, None)
    _forget_probes(title)
    # A recreated same-title session must start with a fresh branch baseline.
    _LAST_BRANCH.pop(title, None)
    _ports.release(title)
    _events.BUS.emit("session.deleted", session=title)
    return JSONResponse({"ok": True})


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
            ["git", "-C", wtp, "--no-pager", "diff", _session_fork_point(inst, wtp)],
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
    """Pause the session (``inst.Pause`` — detach/stop tmux, keep the worktree),
    persist the new state, and emit ``session.paused``. A pause error is a 400."""
    inst, err = _inst_or_404(title)
    if err is not None:
        return err
    try:
        await asyncio.to_thread(inst.Pause)
    except Exception as err:  # noqa: BLE001
        return JSONResponse({"error": str(err)}, status_code=400)
    ENGINE.save()
    _events.BUS.emit(
        "session.paused",
        session=title,
        new=getattr(inst.Status, "name", str(inst.Status)).lower(),
    )
    return JSONResponse(_instance_json(inst))


@app.post("/api/instances/{title}/resume")
async def instance_resume(title: str) -> JSONResponse:
    """Resume a paused session (``inst.Resume``), re-deriving its port block into
    the fresh tmux env first, then persist and emit ``session.resumed``. A resume
    error is a 400."""
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


@app.post("/api/instances/{title}/send")
async def instance_send(title: str, payload: dict) -> JSONResponse:
    """Send a single message to the session's agent window and (by default)
    submit it. Boots/resumes the agent session first if it isn't running, so a
    just-created session starts working from one call. ``submit=false`` types
    the text without pressing Enter (leave the user to review/edit)."""
    inst, err = _inst_or_404(title)
    if err is not None:
        return err
    text = str((payload or {}).get("text", ""))
    if not text.strip():
        return JSONResponse({"error": "empty message"}, status_code=400)
    submit = bool((payload or {}).get("submit", True))
    if _budget_locked(title):
        return JSONResponse(
            {
                "error": "session is over budget — raise the budget to send",
                "budget_locked": True,
            },
            status_code=409,
        )
    name, err = await asyncio.to_thread(_agent_session_ready, inst, title)
    if err is not None:
        return JSONResponse({"error": err}, status_code=409)
    ok = await asyncio.to_thread(_send_to_agent, name, text, submit)
    if not ok:
        return JSONResponse(
            {"error": "failed to send to agent session"}, status_code=502
        )
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
def post_queue(title: str, payload: dict) -> JSONResponse:
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
async def post_queue_send_now(title: str, payload: dict) -> JSONResponse:
    """Send one queued item to the agent NOW (manual override): skips the
    drain's idle wait, cooldowns, and the usage-limit hold — the user clicked,
    so deliver. ``{"id": "<item id>"}``. The item leaves the queue exactly as
    a drained send would (popped; re-appended when ``loop`` is on)."""
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
    if _budget_locked(title):
        return JSONResponse(
            {
                "error": "session is over budget — raise the budget to send",
                "budget_locked": True,
            },
            status_code=409,
        )
    name, err = await asyncio.to_thread(_agent_session_ready, inst, title)
    if err is not None:
        return JSONResponse({"error": err}, status_code=409)
    ok = await asyncio.to_thread(_send_to_agent, name, it["text"], True)
    if not ok:
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


# _workspace_roots / _dir_size_bytes / _find_worktrees / _classify_workspace
# moved to core.workspaces (imported above).


# _shell_tmux_name / _live_session_name moved to core.agent_sessions
# (imported above).


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


def _configured_pr_base() -> str:
    """The user-configured default PR base branch (``repository.pr_base_branch``),
    or ``""`` when unset.

    Read fresh from settings on each call so a change takes effect without a
    restart. When set it overrides the per-session fork-point as the "Make PR"
    target (see :func:`instance_make_pr`); blank leaves the K1 behaviour intact.
    """
    try:
        from backend.config.settings import load_settings

        return (load_settings().repository.pr_base_branch or "").strip()
    except Exception:  # noqa: BLE001 — settings optional; never break a PR
        return ""


# _ensure_shell_session moved to core.agent_sessions (imported above).


# --------------------------------------------------------------------------- #
# Exit markers: tell a user-intended quit (Ctrl+C / clean exit) apart from an
# unnatural death (kill -9, OOM, crash, tmux kill) so we only auto-resume the
# latter. claude is launched through a wrapper that records its exit code when
# it ends; if the session is gone and the marker says it quit normally, we don't
# relaunch it.
# --------------------------------------------------------------------------- #
# Exit-marker helpers + the exit-recording launch wrapper moved to core.terminal
# (shared by the per-instance agent sessions and addon-owned sessions). Imported
# above as _exit_marker_path / _read_exit_marker / _clear_exit_marker /
# _is_natural_exit / _wrap_launch_cmd.

# _ensure_agent_session / _send_to_shell / _send_to_agent moved to
# core.agent_sessions (imported above).
# _ensure_assistant_session moved to the Assistant addon
# (backend/web/addons/assistant.py).


# _git_count / _commits_beyond_base / _has_upstream / _is_dirty / _git_head_sha /
# _current_branch / _origin_branch_sha (+ _ORIGIN_SHA_CACHE) moved to
# core.git_ops (imported above).


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


# _TOKENS_CACHE / _created_epoch / _session_tokens moved to core.session_stats
# (imported above). The unused local _ts_epoch copy was dropped — the live
# implementations (and their tests) are in providers/claude.py and
# providers/usage_history.py.


# _kill_named_session / _kill_shell_session / _kill_agent_session moved to
# core.agent_sessions (imported above).


def _unique_title(base: str) -> str:
    """A title not currently in use (appends -2, -3, … on collision)."""
    if base not in ENGINE.instances:
        return base
    i = 2
    while ("%s-%d" % (base, i)) in ENGINE.instances:
        i += 1
    return "%s-%d" % (base, i)


# _strictly_under moved to core.workspaces (imported above).


# --------------------------------------------------------------------------- #
# Recently-closed sessions
#
# Ending a session (the ✕ button / Delete key) keeps its worktree on disk and
# stashes the instance's serialized data here, so it can be reopened later as a
# fresh agent session pointed at the same worktree. Persisted next to the engine
# state in ``~/.mindflock/recently_closed.json``.
# --------------------------------------------------------------------------- #
# _recently_closed_path / _load_recently_closed / _save_recently_closed /
# _record_closed moved to core.recently_closed (imported above).


# _remove_worktree_path / _worktree_in_use_by_other moved to core.workspaces
# (imported above).


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
    session. The frontend confirms before calling this.
    """
    inst, err = _inst_or_404(title)
    if err is not None:
        return err
    wt = inst.GetWorktreePath()
    # In-place sessions run in the user's OWN repo — never delete it.
    in_place = getattr(inst, "InPlace", False)

    def _do() -> None:
        try:
            inst.Kill()
        except Exception as err:  # noqa: BLE001
            if log.ErrorLog is not None:
                log.ErrorLog.Printf("cleanup: kill %s failed: %v", title, err)
        if wt and not in_place:
            shutil.rmtree(wt, ignore_errors=True)
        _kill_shell_session(title)
        if not _worktree_in_use_by_other(wt, title):
            _close_cursor_window(wt)
            _remove_trust_entry(wt)  # GC ~/.claude.json trust entry (G3)

    await asyncio.to_thread(_do)
    with ENGINE.lock:
        ENGINE.instances.pop(title, None)
    ENGINE.save(exclude_titles={title})
    _EVENT_SNAPSHOT.pop(title, None)
    _aliases.drop(title)
    _events.BUS.emit("session.deleted", session=title, data={"cleaned": True})
    return JSONResponse({"ok": True})


@app.post("/api/instances/{title}/close")
async def close_instance(title: str) -> JSONResponse:
    """End the agent session but KEEP its worktree on disk.

    Unlike DELETE (which removes the worktree + branch via ``inst.Kill``), this
    only kills the agent + shell tmux sessions and drops the instance from the
    grid. The session is stashed in the recently-closed store so it can be
    reopened later as a fresh agent pointed at the same worktree.
    """
    inst, err = _inst_or_404(title)
    if err is not None:
        return err
    _record_closed(inst)
    try:
        wt = inst.GetWorktreePath()
    except Exception:  # noqa: BLE001
        wt = ""

    def _do() -> None:
        _kill_agent_session(title)
        _kill_shell_session(title)
        # Ending a session closes its editor window too (the worktree stays on
        # disk and can be reopened) — but only if no other live session (e.g. a
        # copy) still shares this worktree.
        if not _worktree_in_use_by_other(wt, title):
            _close_cursor_window(wt)

    await asyncio.to_thread(_do)
    with ENGINE.lock:
        ENGINE.instances.pop(title, None)
    ENGINE.save(exclude_titles={title})
    _EVENT_SNAPSHOT.pop(title, None)
    _aliases.drop(title)
    _events.BUS.emit("session.deleted", session=title, data={"closed": True})
    return JSONResponse({"ok": True})


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
            with ENGINE.lock:
                ENGINE.instances.pop(new_title, None)

    _register_task(_bg_start())
    _events.BUS.emit(
        "session.created",
        session=new_title,
        new="loading",
        data={"program": program, "copied_from": title},
    )
    return JSONResponse(_instance_json(inst), status_code=202)


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
            }
        )
    return JSONResponse(out)


@app.post("/api/recently-closed/{entry_id}/reopen")
async def reopen_recently_closed(entry_id: str) -> JSONResponse:
    """Recreate a closed session as a running instance on its preserved worktree."""
    return await _reopen_closed_entry(entry_id)


async def _reopen_closed_entry(entry_id: str) -> JSONResponse:
    """The reopen itself, callable from anywhere that identifies a closed
    session — the Recent… dialog by entry id, and an intake row that found this
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
        ENGINE.instances[title] = inst
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
        repo_path = ((entry.get("data") or {}).get("worktree") or {}).get(
            "repo_path", ""
        )
        await asyncio.to_thread(_remove_worktree_path, folder, repo_path)
    _save_recently_closed([e for e in items if e.get("id") != entry_id])
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
        'rm -f {status}; touch "$L"; git add -A; n=0; rc=1; '
        "while [ $n -lt 5 ]; do "
        # Refresh the lock each round so a multi-round retry (each hook pass
        # can outlast the liveness grace) can't be self-healed mid-flight.
        'touch "$L"; '
        "{sk}git commit -F {msgf}; rc=$?; "
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
        "; [ $rc -eq 0 ] && rm -f {msgf} || true"
    ).format(
        msgf=shlex.quote(_COMMIT_MSG_FILE),
        status=_COMMIT_STATUS_FILE,
        # Empty by default, so the rendered command is unchanged. shlex.quote
        # leaves a plain id list (letters, digits, '.', '_', '-', ',') unquoted,
        # so `git commit -F .mindflock_commit_msg` survives verbatim as a suffix.
        sk=("SKIP=%s " % shlex.quote(skip)) if skip else "",
    )


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
    wt = inst.GetWorktreePath()
    if not wt:
        return JSONResponse({"error": "workspace not ready"}, status_code=409)
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

    def _do():
        name, err = _ensure_shell_session(title, wt)
        if err is not None:
            return err
        # --no-verify: skip the repo's pre-push hook (which re-runs the whole
        # pre-commit stack). The commit step already ran the hooks.
        _send_to_shell(name, "git push --no-verify -u origin HEAD")
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
    default = _configured_pr_base() or _session_base_branch(inst)
    return JSONResponse({"branches": branches, "current": current, "default": default})


# The one remedy sentence every "MindFlock can't do the GitHub half itself"
# message ends with. Kept in one place because it is asserted verbatim in the
# tests and quoted in the docs: both rungs are optional and either one fixes it.
_PR_REMEDY = "add a GitHub token in Intake → Pull requests, or install the GitHub CLI"


def _pr_browser_fallback(wt: str, base: str, branch: str) -> JSONResponse:
    """200 + a prefilled compare page when neither gh nor a token is around.

    Deliberately NOT an error status. The user's push already worked (pushing
    is always plain ``git push``), and the PR is one click away on the compare
    page GitHub prefills from the branch — so this is a handoff, not a failure,
    and the UI renders it as a link rather than a red toast."""
    return JSONResponse(
        {
            "ok": False,
            "compare_url": _remote_url.compare_url(
                _github_pr.origin_url(wt), base, branch
            ),
            "message": "MindFlock could not open the pull request for you — "
            + _PR_REMEDY
            + ". The compare link opens a pull request prefilled from this "
            "branch.",
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
    the session's fork-point (the prior behaviour)."""
    if not git_available():
        return _no_git_response()
    inst, err = _inst_or_404(title)
    if err is not None:
        return err
    wt = inst.GetWorktreePath()
    if not wt:
        return JSONResponse({"error": "workspace not ready"}, status_code=409)
    # An explicit base from the dialog wins; then a configured default PR base
    # (Settings → "Default PR base branch"), so plain worktree sessions can PR
    # into a fixed branch (e.g. staging) instead of whatever they were cut from;
    # blank falls through to the per-session base (K1) — the prior behaviour.
    chosen = (payload or {}).get("base")
    base = (chosen or "").strip() or _configured_pr_base() or _session_base_branch(inst)
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

    def _do_gh():
        cp = _run_capped(
            ["gh", "pr", "create", "--base", base, "--head", branch, "--fill"],
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
        res = await _github_pr.create_pr(wt, base, branch)
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
async def instance_merge_pr(title: str) -> JSONResponse:
    """Merge the branch's PR into the base (a true merge commit). Confirmed in
    the UI; surfaces the underlying refusal (e.g. if the repo requires
    squash/review).

    Same three rungs as :func:`instance_make_pr` — ``gh``, then REST with a
    resolved token, then a link to the PR so the user can press Merge there."""
    if not git_available():
        return _no_git_response()
    inst, err = _inst_or_404(title)
    if err is not None:
        return err
    wt = inst.GetWorktreePath()
    if not wt:
        return JSONResponse({"error": "workspace not ready"}, status_code=409)
    # Merge the PR of the branch the worktree is *actually* on — the stored
    # inst.Branch can drift when the user switches branches in the workspace,
    # which would merge the wrong branch's PR.
    branch = await asyncio.to_thread(_current_branch, wt) or inst.Branch or ""

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
        base = _configured_pr_base() or _session_base_branch(inst)
        _run_capped(  # best-effort; a failure just leaves the old lag
            ["git", "-C", wt, "fetch", "origin", base],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
        )

    await asyncio.to_thread(_post_merge)
    _forget_probes(title)
    return JSONResponse({"ok": True})


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
    msg = str(body.get("message") or "").strip()
    if not msg:
        # An intake-armed run already carries the ticket / PR / issue NAME as its
        # message. Re-arming with ⏩ used to overwrite that with a generated
        # "Work on <slug>", throwing away the one genuinely descriptive subject
        # available. Prefer it.
        prev = _autopilot.get(title) or {}
        msg = str(prev.get("message") or "").strip()
    if not msg:
        # Only adopt the on-disk message when a FAILED attempt is pending — the
        # same rule GET /commit-message applies. Reusing it unconditionally meant a
        # message left by unrelated work became the subject of whatever was armed
        # next.
        msg = await asyncio.to_thread(_pending_commit_message, wt)
    # Whether ``msg`` is a placeholder this route invented, which is what lets the
    # commit step replace it with a model-written one (see _autopilot_commit).
    msg_auto = False
    if not msg and await asyncio.to_thread(_is_dirty, wt):
        # The ⏩ button presses with no message, and `.mindflock_commit_msg` only
        # exists once something has committed THROUGH MindFlock — so the single most
        # common press ("I have work, carry it to a PR") used to be rejected
        # outright. Generate a subject instead of refusing: a chain that halts
        # because nobody typed a sentence is worse than an honest default one, and
        # the user can always amend it afterwards.
        #
        # Note this stays the CHEAP default rather than asking a model here. Arming
        # is arm-and-wait: the agent may work for another twenty minutes, so a
        # message written from the tree as it looks right now would describe a diff
        # that no longer exists by the time the commit happens — and the press must
        # not wait ~10s on a CLI either. The real message is written at commit time.
        msg = await asyncio.to_thread(_autopilot_default_message, inst, wt)
        msg_auto = bool(msg)
    rec = await asyncio.to_thread(
        lambda: _autopilot.arm(
            title,
            depth,
            source="session",
            message=msg,
            message_auto=msg_auto,
            base=str(body.get("base") or ""),
            branch=_current_branch(wt) or "",
            retryable=_precommit_retry_hooks(),
            boot=_SERVER_BOOT_ID,
        )
    )
    if rec is None:
        return JSONResponse({"error": "could not arm fast-track"}, status_code=500)
    # Cheap: no probes, no network. Arming changes a field in a JSON file, so the
    # press must not wait on a PR lookup (that cost ~700ms on a cold cache, ON the
    # event loop). The response carries the authoritative record so the client can
    # settle its toggle without a follow-up read.
    _publish_autopilot(title)
    return JSONResponse({"ok": True, "autopilot": _autopilot_dto(title)})


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
    await _annotate_workspaces(data.get("tickets", []), _ticket_workspace_args)
    return JSONResponse(data)


@app.post("/api/tickets/start")
async def ticket_force_start(payload: dict) -> JSONResponse:
    """Force-start a coding session for one ticket, bypassing auto filters."""
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

    # Row first, provider fetch second (see the PR endpoint above). The panel's
    # cached list is the title's source: ticket slugs are provider-defined
    # (Shortcut hardcodes sc-<id>), so deriving one here would be a second
    # implementation waiting to drift.
    early = _cached_session_title(
        _ASSIGNED_TICKETS_CACHE,
        "tickets",
        lambda t: t.get("source") == source and str(t.get("id")) == ticket_id,
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
        _pending_add(early, "tix")

    try:
        story = await _ticket_start.find_ticket(source, ticket_id)
    except LookupError as err:
        _pending_drop(early)
        return JSONResponse({"error": str(err)}, status_code=404)
    except Exception as err:  # noqa: BLE001
        _pending_drop(early)
        return JSONResponse({"error": str(err)}, status_code=502)

    # A per-start choice outranks the source's card. Stamped onto the story
    # because that is the field every launch path already consults first.
    if agent_override:
        story.agent = agent_override

    title = _ticket_start.session_title(story)
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
    # The branch is known now, so the row can read as the ticket it is rather
    # than a bare slug (add() keeps the original `since`).
    _pending_add(
        title,
        "tix",
        branch=_ticket_start.branch_for(story),
        workspace_strategy=_ticket_start.workspace_mode(),
    )
    _arm_intake_autopilot(
        title,
        depth_override or _source_intake_depth(source),
        "tix",
        str(getattr(story, "id", "") or ticket_id),
        message=str(getattr(story, "name", "") or ""),
    )

    async def _bg_start() -> None:
        # Same shape as the pipeline's SessionRunner.run, but against THIS
        # server's engine so the session shows up in the grid without a
        # reload. The engine owns provisioning (inside Instance.Start), so
        # unlike the PR path there is no pre-provision step.
        marked = False
        try:
            prompt = _ticket_start.build_prompt(story)
            branch = _ticket_start.branch_for(story)
            # A previous run of this ticket may have left a worktree holding the
            # branch: the session is long gone (nothing blocked the button) but
            # git still refuses to check the branch out twice. Reclaim it when
            # nothing owns it and it holds no work — otherwise the provisioning
            # error stands, unchanged.
            await asyncio.to_thread(
                _worktree_reclaim.reclaim_for_launch,
                getattr(story, "repo_url", "") or "",
                branch,
            )
            # In-flight ledger marker BEFORE the slow launch, so a running
            # pipeline's scans treat the ticket as taken (orchestrator guard).
            _ticket_start.record_started(story)
            marked = True
            # The ticket's source may pin its own agent CLI; empty falls back to
            # this app's default program. Resolved before the options because
            # this start's effort has to be translated into THAT CLI's spelling.
            program = _ticket_start.agent_for(story) or ENGINE.default_program()
            # This start's own rung wins; the source's default is what applies
            # when the row did not pick one. Resolved here rather than in
            # `_start_effort_override` because the source is only knowable once
            # the story is — and an explicit choice on the row must be able to
            # ask for LESS thinking than the queue's default, not just more.
            level = effort_override or _ticket_start.effort_for(story)
            prompt = _provider_effort.decorate_prompt(prompt, program, level)
            inst = session.NewInstance(
                session.InstanceOptions(
                    title=title,
                    path=".",
                    program=program,
                    provisioned=True,
                    workspace_strategy=_ticket_start.workspace_mode(),
                    new_branch=branch,
                    prompt=prompt,
                    provision_repo_url=getattr(story, "repo_url", "") or "",
                    launch_args=_start_launch_args(program, level),
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
                data={
                    "program": inst.Program,
                    "provisioned": True,
                    "ticket": str(story.id),
                },
            )
            try:
                await asyncio.to_thread(inst.Start, True)
                ENGINE.save()
            except Exception:
                # By identity: this task may be the loser of a re-start, and
                # popping by name would delete the LIVE session's record.
                _drop_failed_start(title, inst)
                raise
            # Terminal ledger entry so auto ingestion doesn't run it again.
            _ticket_start.record_result(story, branch=branch)
            await _ticket_start.download_attachments(inst, story)
            if log.InfoLog is not None:
                log.InfoLog.Printf("forced ticket session %s live", title)
        except Exception as err:  # noqa: BLE001
            if marked:
                _ticket_start.record_result(story, error=str(err))
            if log.ErrorLog is not None:
                log.ErrorLog.Printf("forced ticket session %s failed: %v", title, err)
            _events.BUS.emit(
                "session.create_failed", session=title, data={"error": str(err)}
            )
        finally:
            _pending_drop(title)

    _register_task(_bg_start())
    return JSONResponse({"started": True, "title": title}, status_code=202)


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
                "use Begin work to start it fresh"
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
            with ENGINE.lock:
                ENGINE.instances.pop(open_title, None)
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


# The repo list Verify tracks has NO routes of its own, and that absence is the
# design. It is ``repository.verify_repos`` + ``repository.verify_repo_settings``
# — ordinary settings, typed as ``owner/name`` into the same card list Intake
# uses for PR review and issues, and saved through ``POST /api/settings`` like
# every other setting. What used to be here (a GET that DISCOVERED repos from
# open sessions and a POST that patched a block keyed by absolute path) is gone
# along with the discovery: a repo Verify watches is one a person named, not one
# that happened to have a session open, and the name is the GitHub slug because
# that is the only spelling that is the same in every clone and every worktree.


def _closed_session_plan_inputs(title: str) -> tuple:
    """``(branch, sha, repo_root, program)`` for a session that is CLOSED, or
    ``(…, "")`` when there is nothing usable.

    WHY VERIFY HAS TO LOOK HERE. Everything else about this feature is built on
    the fact that a checklist outlives its session — that is why the plan stores
    the main repo rather than the worktree, and why a plan can be rewritten and
    run months after the work merged. Plan CREATION was the one half that still
    demanded a live window, so "write me a checklist for that" stopped being
    possible at exactly the moment people actually do it: after the work is done
    and the window has been closed.

    The recently-closed store already keeps what a plan needs — the title, the
    branch, and the repo the worktree was cut from — because it is the undo store
    for reopening the session, so nothing new has to be recorded.

    THE ONE THING IT CANNOT RECOVER is the intent. The ticket text lives in the
    session's seed prompt, the closed entry does not carry it, and the transcript
    that would hold it lived in a worktree that is reclaimed on close. So a
    checklist written from here is diff-only, and it says so rather than
    pretending: the honest fix for a diff-only draft is the rewrite box, where a
    person can type what it should have been about in one sentence.
    """
    for entry in _load_recently_closed():
        if str(entry.get("title") or "") != title:
            continue
        data = entry.get("data") or {}
        wt = data.get("worktree") or {}
        repo_root = (
            str(wt.get("repo_path") or "")
            or str(entry.get("folder") or "")
            or str(data.get("path") or "")
        )
        branch = str(entry.get("branch") or data.get("branch") or "")
        program = str(data.get("program") or "")
        if not repo_root or not branch:
            return "", "", "", ""
        # The branch may well be gone locally (merged and deleted), so origin's
        # copy is a real answer and not a fallback. Whichever resolves is the
        # commit the checklist is about.
        sha = ""
        for ref in (branch, "origin/%s" % branch):
            out = subprocess.run(
                ["git", "-C", repo_root, "rev-parse", "--verify", "-q", ref],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                timeout=15,
            )
            if out.returncode == 0:
                sha = out.stdout.decode("utf-8", "replace").strip()
                break
        return branch, sha, repo_root, program
    return "", "", "", ""


@app.post("/api/instances/{title}/test-plan")
async def instance_write_test_plan(title: str) -> JSONResponse:
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
    """
    if not git_available():
        return _no_git_response()
    inst, err = _inst_or_404(title)
    if err is not None:
        # ...unless the session was merely CLOSED. A checklist is worth asking
        # for precisely when the work is finished and the window has been put
        # away, so refusing there made the button useless at the only moment
        # people reach for it. See `_closed_session_plan_inputs`.
        return await _write_test_plan_for_closed(title)
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
            return None, JSONResponse(
                {"error": "this session isn't on a branch"}, status_code=409
            )
        if not sha:
            # An unborn HEAD: nothing has been committed, so there is no change
            # to describe and a plan would be a list of steps about nothing.
            return None, JSONResponse(
                {"error": "nothing committed on this branch yet"}, status_code=409
            )
        # One expression, used twice on purpose: the plan's repo and the repo the
        # live branch is resolved FOR must be the same one, or a repo whose
        # override says "staging" gets a plan that waits for "main" forever.
        root = repo_root or wt
        plan = _test_plans.ensure_plan_for(
            title,
            branch,
            sha,
            root,
            _test_plans.resolve_live_branch(root),
            intent=_test_plan_intent(title),
        )
        return plan, None

    plan, resp = await asyncio.to_thread(_prepare)
    if resp is not None:
        return resp
    if plan is None:
        existing = _test_plans.get(title) or {}
        return JSONResponse(
            {
                "ok": True,
                "plan": title,
                "existing": True,
                "state": existing.get("state", ""),
            }
        )
    _start_test_plan_generation(title, getattr(inst, "Program", "") or "", wt)
    return JSONResponse({"ok": True, "plan": title, "existing": False}, status_code=202)


async def _write_test_plan_for_closed(title: str) -> JSONResponse:
    """`instance_write_test_plan` for a session that has been closed.

    The same answers in the same shapes — ``existing: true`` for one that is
    already written, 202 for one being written — because the caller is one
    button and must not have to care which store the session came from.

    Generation runs with NO worktree, which is the path `generate` already takes
    for every rewrite of a plan whose session is gone: it falls back to the
    plan's ``repo_root``, reads the branch's diff there, and asks the repo's
    default CLI. That is exactly the situation here.
    """

    def _prepare():
        branch, sha, repo_root, program = _closed_session_plan_inputs(title)
        if not repo_root:
            return (
                None,
                "",
                JSONResponse({"error": "no such session: %s" % title}, status_code=404),
            )
        if not branch:
            return (
                None,
                "",
                JSONResponse(
                    {"error": "that session wasn't on a branch"}, status_code=409
                ),
            )
        if not sha:
            return (
                None,
                "",
                JSONResponse(
                    {
                        "error": "%s is gone from this repo — nothing left to write a "
                        "checklist from" % branch
                    },
                    status_code=409,
                ),
            )
        plan = _test_plans.ensure_plan_for(
            title,
            branch,
            sha,
            repo_root,
            _test_plans.resolve_live_branch(repo_root),
            # Deliberately empty: the closed entry does not carry the seed
            # prompt and the transcript went with the worktree. Guessing here
            # would be worse than a diff-only draft the user can aim with the
            # rewrite box.
            intent="",
        )
        return plan, program, None

    plan, program, resp = await asyncio.to_thread(_prepare)
    if resp is not None:
        return resp
    if plan is None:
        existing = _test_plans.get(title) or {}
        return JSONResponse(
            {
                "ok": True,
                "plan": title,
                "existing": True,
                "state": existing.get("state", ""),
            }
        )
    _start_test_plan_generation(title, program, "")
    return JSONResponse({"ok": True, "plan": title, "existing": False}, status_code=202)


@app.post("/api/test-plans/{plan_id:path}/run")
async def run_test_plan(plan_id: str, payload: Optional[dict] = None) -> JSONResponse:
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
    else:
        resp = await create_instance(
            {
                "title": title,
                # The MAIN repo, which is why the plan stores it: the worktree
                # this branch was written in is normally reclaimed by the time a
                # plan comes due.
                "repo_path": plan["repo_root"],
                "prompt": prompt,
            }
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


@app.post("/api/test-plans/{plan_id:path}/fix")
async def fix_failed_test_plan_steps(
    plan_id: str, payload: Optional[dict] = None
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
        return JSONResponse(
            {"error": "test plan not found: %s" % plan_id}, status_code=404
        )
    if not git_available():
        return JSONResponse({"error": "git is not available"}, status_code=409)
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
            return JSONResponse(
                {"error": "steps must be a non-empty list of step ids"},
                status_code=400,
            )
        wanted = [str(s) for s in only]
        by_id = {s["id"] for s in plan["steps"]}
        unknown = [s for s in wanted if s not in by_id]
        if unknown:
            return JSONResponse(
                {
                    "error": "no such step: %s" % ", ".join(unknown),
                    "unknown_steps": unknown,
                },
                status_code=400,
            )
        chosen = {f["step"]["id"] for f in failures if f["step"]["id"] in wanted}
        missing = [s for s in wanted if s not in chosen]
        if missing:
            return JSONResponse(
                {
                    "error": "%s didn't fail — there is nothing to fix there"
                    % ", ".join(missing)
                },
                status_code=409,
            )
        failures = [f for f in failures if f["step"]["id"] in chosen]
    if not failures:
        return JSONResponse(
            {"error": "nothing on this checklist failed"}, status_code=409
        )
    if not plan.get("repo_root"):
        return JSONResponse(
            {"error": "this checklist has no repository to work in"}, status_code=409
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
            return JSONResponse(
                {
                    "error": "the repo this checklist was written from is gone "
                    "(%s) — a fix session needs it to work in"
                    % (plan.get("repo_root") or "no path recorded")
                },
                status_code=409,
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
            return JSONResponse(
                {
                    "error": "the last fix session's workspace is still holding "
                    "this branch and has uncommitted work in it (%s) — reopen it "
                    "from Recent to finish or discard that work, then press Fix "
                    "again" % held,
                    "worktree": held,
                },
                status_code=409,
            )
        resp = await create_instance(
            {"title": title, "repo_path": plan["repo_root"], "prompt": prompt}
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
    return JSONResponse({"ok": True, "plan": plan})


async def _end_verify_session(title: str) -> bool:
    """End the verify session ``title`` if it is live. Returns whether it was.

    ``close`` rather than DELETE, deliberately: a verify workspace is cheap but
    it is not ours to destroy — the agent may have left notes, a log tail, a
    half-finished repro in it, and the run being cancelled is often exactly when
    someone wants to look. Closing keeps the worktree and puts the session in
    the recently-closed store, so it is one click from coming back.
    """
    if not title or title not in ENGINE.instances:
        return False
    resp = await close_instance(title)
    return resp.status_code < 400


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

    ``size_bytes`` is only computed when ``?sizes=1``: a ``du`` over a large
    workspace stats every file and can take seconds when the page cache is
    cold, so the UI fetches the instant no-sizes list first and fills sizes
    in from a second request.
    """
    active = _active_worktree_titles()

    def _mtime(path: str) -> float | None:
        """The directory's own mtime, for the UI's "sort by date".

        One stat per entry, unlike the ``du`` behind ``size_bytes`` — cheap
        enough to always include, so the disk manager can sort by age without a
        second round trip. It is the top-level dir's timestamp, so it tracks when
        the workspace was created or had files added/removed at its root, not
        every edit deep inside it. Newest-first over that is still the answer to
        "which of these 40 worktrees is stale".
        """
        try:
            return os.stat(path).st_mtime
        except OSError:
            return None

    def _entry(path: str, name: str, root: str) -> dict:
        return {
            "name": name,
            "path": path,
            "root": root,
            "kind": _classify_workspace(os.path.basename(name) or name, root),
            "size_bytes": None,
            "mtime": _mtime(path),
            "active_session": active.get(os.path.realpath(path)),
        }

    def _scan() -> list:
        out = []
        wt_dir = os.path.realpath(os.path.join(config.GetConfigDir(), "worktrees"))
        for root in _workspace_roots():
            if root == wt_dir:
                # Worktrees are nested under branch-name slashes — list the
                # actual worktree leaf dirs by their path relative to the root.
                for p in _find_worktrees(root):
                    out.append(_entry(p, os.path.relpath(p, root), root))
            else:
                # workspace_dir is flat: clones, _base_*, cache refreshers and
                # pr-* are all direct children.
                try:
                    children = sorted(os.scandir(root), key=lambda e: e.name)
                except OSError:
                    continue
                for e in children:
                    try:
                        if not e.is_dir():
                            continue
                    except OSError:
                        continue
                    out.append(_entry(e.path, e.name, root))
        if sizes and out:
            # One du per entry; a single big tree dominates, so run them
            # concurrently instead of back-to-back.
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                for entry, n in zip(
                    out, pool.map(_dir_size_bytes, [e["path"] for e in out])
                ):
                    entry["size_bytes"] = n
        return out

    entries = await asyncio.to_thread(_scan)
    return JSONResponse({"workspaces": entries, "roots": _workspace_roots()})


# _base_clone_references moved to core.workspaces (imported above).


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

    killed = None
    for title, inst in list(ENGINE.instances.items()):
        try:
            wp = os.path.realpath(inst.GetWorktreePath() or "")
        except Exception:  # noqa: BLE001
            wp = ""
        if wp and wp == real:

            def _kill(inst=inst, title=title) -> None:
                try:
                    inst.Kill()
                except Exception as err:  # noqa: BLE001
                    if log.ErrorLog is not None:
                        log.ErrorLog.Printf("ws-delete kill %s: %v", title, err)

            await asyncio.to_thread(_kill)
            _kill_shell_session(title)
            with ENGINE.lock:
                ENGINE.instances.pop(title, None)
            killed = title
            break

    def _remove() -> None:
        shutil.rmtree(real, ignore_errors=True)
        _close_cursor_window(real)
        _remove_trust_entry(real)  # GC ~/.claude.json trust entry (G3)
        # If we removed a linked worktree, prune the stale registration from the
        # base repo(s) so `git worktree` stays consistent. Base clones are
        # per-repo (`_base_<slug>`) — prune every one under every managed
        # workspace dir (cheap no-op when the worktree wasn't theirs).
        if os.path.dirname(real).endswith("worktrees"):
            _prune_base_clone_worktrees()

    await asyncio.to_thread(_remove)
    if killed:
        ENGINE.save(exclude_titles={killed})
    return JSONResponse({"ok": True, "killed_session": killed})


@app.post("/api/workspaces/clear")
async def clear_workspaces(payload: Optional[dict] = None) -> JSONResponse:
    """Delete every UNPROTECTED, idle workspace under the managed roots in one
    sweep — a bulk "reclaim disk" action.

    Left untouched: protected shared infrastructure (base clones + cache
    refreshers, the same dirs the UI badges "protected") and any workspace that
    is the working directory of a LIVE session. So this never kills a running
    agent — unlike the per-row Delete, it only removes what's already idle.
    Returns the names removed and the live sessions it skipped.
    """
    active = _active_worktree_titles()

    def _sweep() -> dict:
        removed: list = []
        kept_active: list = []
        removed_worktree = False
        roots = _workspace_roots()
        wt_dir = os.path.realpath(os.path.join(config.GetConfigDir(), "worktrees"))
        for root in roots:
            if root == wt_dir:
                entries = [(p, os.path.basename(p)) for p in _find_worktrees(root)]
            else:
                entries = []
                try:
                    for e in sorted(os.scandir(root), key=lambda e: e.name):
                        try:
                            if e.is_dir():
                                entries.append((e.path, e.name))
                        except OSError:
                            continue
                except OSError:
                    continue
            for path, name in entries:
                real = os.path.realpath(path)
                base = os.path.basename(name) or name
                # Protected shared infra: never bulk-delete (matches the UI badge).
                if provisioning.is_base_repo_dirname(base) or _is_refresher_dirname(
                    base
                ):
                    continue
                # Idle only: leave any workspace a live session is using.
                if real in active:
                    kept_active.append(active[real])
                    continue
                # Guard: only ever remove a proper child of a managed root.
                if not real or not os.path.isdir(real):
                    continue
                if not any(_strictly_under(real, r) for r in roots):
                    continue
                shutil.rmtree(real, ignore_errors=True)
                _close_cursor_window(real)
                _remove_trust_entry(real)  # GC ~/.claude.json trust entry (G3)
                removed.append(name)
                if os.path.dirname(real).endswith("worktrees"):
                    removed_worktree = True
        # A removed worktree leaves a stale registration in its base clone(s) —
        # prune every base clone under every managed workspace dir (cheap no-op
        # when the worktree wasn't theirs), as the single-delete path does.
        if removed_worktree:
            _prune_base_clone_worktrees()
        return {"removed": removed, "kept_active": sorted(set(kept_active))}

    result = await asyncio.to_thread(_sweep)
    return JSONResponse({"ok": True, "removed_count": len(result["removed"]), **result})


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
# session, the whole time the server runs. Toggleable at runtime (the sidebar
# switch / the API below); CS_CURSOR_AUTOADOPT=0 just makes it start OFF.
_CURSOR_AUTOADOPT_ENABLED = os.environ.get("CS_CURSOR_AUTOADOPT", "1") != "0"


# The cursor auto-adopt loop is started by the lifespan handler (it checks the
# runtime flag each tick so the sidebar switch can flip it without a restart).


@app.get("/api/cursor/autoadopt")
def cursor_autoadopt_status() -> JSONResponse:
    """Whether folders opened in the IDE are auto-adopted as in-place sessions."""
    return JSONResponse({"enabled": _CURSOR_AUTOADOPT_ENABLED})


@app.post("/api/cursor/autoadopt")
def cursor_autoadopt_set(payload: dict) -> JSONResponse:
    """Toggle IDE-folder auto-adoption. Body: ``{"enabled": <bool>}``."""
    global _CURSOR_AUTOADOPT_ENABLED
    _CURSOR_AUTOADOPT_ENABLED = bool((payload or {}).get("enabled"))
    return JSONResponse({"enabled": _CURSOR_AUTOADOPT_ENABLED})


@app.get("/api/scroll-speed")
def scroll_speed_get() -> JSONResponse:
    """Current terminal mouse-wheel scroll speed (lines per notch)."""
    return JSONResponse({"speed": load_scroll_speed()})


@app.post("/api/scroll-speed")
def scroll_speed_set(payload: dict) -> JSONResponse:
    """Persist the wheel scroll speed and apply it live to running terminals.

    Tunes the tmux copy-mode wheel binding (server-wide), so the change takes
    effect immediately on already-open terminals — no restart needed. The value
    is clamped to a sane range."""
    speed = save_scroll_speed((payload or {}).get("speed"))
    try:
        apply_scroll_speed(speed)
    except (
        Exception
    ):  # noqa: BLE001 — best-effort; persisted value still applies on next session
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
# The PTY<->websocket pump moved to core.terminal.pump_pty (shared with addons).
# Kept as _serve_pty for the existing call sites.
_serve_pty = pump_pty


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
    await _serve_pty(ws, proc, allow_input=True)


# _agent_transcript_text moved to core.session_stats (imported above).


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
    # they proceed concurrently and the loop keeps serving.
    name, err = await asyncio.to_thread(_ensure_agent_session, inst, title)
    if err is not None:
        await ws.send_text(json.dumps({"type": "error", "message": err}))
        await ws.close(code=4409)
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
    out_q: "asyncio.Queue[Optional[bytes]]" = asyncio.Queue()
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
                await ws.send_bytes(data)
            except Exception:  # noqa: BLE001
                break
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass

    sender = asyncio.create_task(_pump_out())
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            b = msg.get("bytes")
            if b is not None:
                # Budget lock: drop keystrokes once the session is over its
                # ceiling (authoritative — the UI overlay is just the visible
                # half). Resize control frames (text/JSON) still pass below.
                if _budget_locked(title):
                    continue
                os.write(fd, b)
                continue
            t = msg.get("text")
            if t is not None:
                # Control frame (resize) or raw input typed as text.
                try:
                    j = json.loads(t)
                except (ValueError, TypeError):
                    if _budget_locked(title):
                        continue
                    os.write(fd, t.encode("utf-8"))
                    continue
                if isinstance(j, dict) and j.get("type") == "resize":
                    try:
                        proc.setwinsize(int(j["rows"]), int(j["cols"]))
                    except Exception:  # noqa: BLE001
                        pass
                elif not _budget_locked(title):
                    os.write(fd, t.encode("utf-8"))
    except WebSocketDisconnect:
        pass
    finally:
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


# The MindFlock logs websocket + the Assistant chat websocket and todos REST
# moved to their addons (backend/web/addons/{ticket_ingestion,assistant}.py).


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
def mobile_view() -> FileResponse:
    """Serve the mobile (``/m``) single-page shell for phone access."""
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
