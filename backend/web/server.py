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
from typing import Dict, Optional

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

import ptyprocess

from backend import config, log
from backend import providers
from backend import session
from backend.providers import config as provider_config
from backend.providers.claude import remove_trust_entry as _remove_trust_entry
from backend.config import ide as ide_cfg
from backend.session import provisioned as provisioning
from backend.session import tmux
from backend.session.git import gh_available
from backend.session.git import remote_url as _remote_url
from backend.session.storage import Loading
from backend.workspace_setup import exclude_artifacts as _exclude_artifacts
from backend.workspace_setup import is_refresher_dirname as _is_refresher_dirname

# Core modules the monolith was split into (see backend.web/core/).
from backend.web.core import auth as _auth
from backend.web.core import events as _events
from backend.web.core import ports as _ports
from backend.web.core import issue_start as _issue_start
from backend.web.core import github_pr as _github_pr
from backend.web.core import pr_review as _pr_review
from backend.web.core import ticket_start as _ticket_start
from backend.web.core import remote as _remote
from backend.web.core import pending as _pending
from backend.web.core import prompt_queue as _prompt_queue
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
    _clear_precommit_locks,
    _dismiss_trust_prompt,
    _failed_precommit_step,
    _normalized_pane_hash,
    _pane_cpu_jiffies,
    _pane_has_agent_process,
    _pane_meta,
    _parse_failed_step,
    _parse_progress_tokens,
    _precommit_lock_is_live,
    _precommit_lock_path,
    _proc_cpu_snapshot,
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
    _register_task(_window_refresh_loop())
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


def _emit_state_changes(title: str, status: str, activity: str, stage: str) -> None:
    """Diff a session's freshly computed state against the last snapshot and
    emit ``session.status/activity/stage_changed`` events on the bus. The first
    sighting only seeds the snapshot (creation is announced by its endpoint;
    created sessions are pre-seeded so their first real transition emits, F6)."""
    with _EVENT_SNAPSHOT_LOCK:
        prev = _EVENT_SNAPSHOT.get(title)
        _EVENT_SNAPSHOT[title] = {
            "status": status,
            "activity": activity,
            "stage": stage,
        }
    if prev is None:
        return
    for field, event, new in (
        ("status", "session.status_changed", status),
        ("activity", "session.activity_changed", activity),
        ("stage", "session.stage_changed", stage),
    ):
        old = prev.get(field)
        if old != new:
            _events.BUS.emit(event, session=title, old=old, new=new)


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


def _forget_probes(title: str) -> None:
    """Drop every memoized probe result for one session (kill/delete paths),
    so a session recreated under the same title starts from fresh probes —
    and so the per-title rolling state doesn't accumulate dead entries for
    the server's lifetime under session churn."""
    with _PROBE_CACHE_LOCK:
        for k in [k for k in _PROBE_CACHE if k[1] == title]:
            _PROBE_CACHE.pop(k, None)
    for _d in (_ACTIVITY_CACHE, _THREAD_RECORD_AT, _TRUST_DISMISS_AT):
        _d.pop(title, None)
    _forget_tokens(title)


def _session_stage_cached(inst) -> dict:
    """``_session_stage(inst)`` memoized per session (see ``_probe_cached``)."""
    return _probe_cached("stage", inst, lambda: _session_stage(inst))


def _agent_activity_cached(inst, title: str) -> str:
    """``_agent_activity(inst, title)`` memoized per session (working/idle/…)."""
    return _probe_cached("activity", inst, lambda: _agent_activity(inst, title))


def _session_last_turn_cached(inst) -> Optional[str]:
    """``_session_last_turn(inst)`` memoized per session (last-turn timestamp)."""
    return _probe_cached("last_turn", inst, lambda: _session_last_turn(inst))


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


def _live_limit_reset(provider, now: float):
    """Authoritative "limited until" from the provider's own usage meter
    (``usage_live()`` — for Claude that's Anthropic's OAuth usage endpoint, the
    same source as the CLI's ``/usage`` screen), independent of whatever text
    happens to be on the pane.

    Returns ``None`` when no live reading is available (callers fall back to
    the pane text), ``0.0`` when the meter says every window still has
    headroom (a lingering banner must be stale), or the epoch when the LAST
    exhausted window reopens — both the 5-hour and the weekly cap must have
    reset before a send can land."""
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
            if _live_limit_reset(provider, now) == 0.0:
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
            live = _live_limit_reset(provider, now)
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
        live = _live_limit_reset(provider, now)
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
        if _live_limit_reset(provider, now) == 0.0:
            _LIMIT_STATE.pop(title, None)
            return 0.0
        return prev
    # No prior hold: arm one straight from the meter if a window is genuinely
    # exhausted (>= _LIMIT_EXHAUSTED_PCT used, reset still ahead), so the queue
    # holds instead of burning the queued prompt on a send that lands on the wall
    # while no banner happens to be visible. A meter that reads open — or is
    # unavailable (None) — leaves the queue free to send, exactly as before.
    live = _live_limit_reset(provider, now)
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
    d.update(_session_stage_cached(i))
    d["queue"] = _queue_summary(i.Title, queues)
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
    "pr_url",
    "failed_step",
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
    d["pr_url"] = None
    d["queue"] = _queue_summary(i.Title, queues)
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
    # Publish the freshly computed state so AppContext.sessions() (Addon API
    # v2) and GET /api/instances can serve it without recomputing it.
    _events.set_sessions_snapshot(out)
    global _SNAPSHOT_AT
    _SNAPSHOT_AT = time.time()


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
    no ticketing source -> ingestion surfaces point at Settings → Ticketing;
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


# MindFlock automation control (/api/mindflock/*) moved to the MindFlock addon.


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
    * ``init_repo`` — ``git init`` an empty folder first (ignored for in-place).
    * ``launch_args`` — per-session agent flags; absent means inherit the global
      default, present (even ``[]``) means use exactly these.

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

    if not title:
        # Quick launch: an empty Name starts an "untitled" session, numbered to
        # stay unique (titles key ENGINE.instances).
        title = "untitled"
        n = 2
        while title in ENGINE.instances:
            title = "untitled-%d" % n
            n += 1
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
        # In-place works directly in an existing repo, so it can't create one.
        init_repo = bool(payload.get("init_repo", False)) and not in_place
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
    if setup_cfg is not None and setup_cfg.has_setup and prompt:
        # Hold the initial prompt until setup succeeds: deliver it via the
        # prompt queue (drained only once setup is ok + the agent is idle)
        # instead of seeding the agent CLI directly. A failed setup keeps
        # the prompt visible in the queue rather than losing it.
        inst.Prompt = ""
        _prompt_queue.enqueue(title, prompt)
        _prompt_queue.set_flags(title, enabled=True)

    # Start does the heavy lifting (git worktree/clone + provisioning + tmux),
    # which can take minutes on the first worktree run (one-time base clone +
    # uv sync). Register the instance as "loading" and run Start in the
    # background so the create request returns immediately and the session shows
    # as provisioning in the grid instead of freezing the dialog on "Creating…".
    inst.SetStatus(Loading)
    with ENGINE.lock:
        ENGINE.instances[title] = inst
    _mark_onboarded()  # first-ever session ends first-run; setup card won't auto-show again

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
            with ENGINE.lock:
                ENGINE.instances.pop(title, None)
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
    return JSONResponse(_instance_json(inst), status_code=202)


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
    inst.ExtraEnv = _ports.env_for(title)
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
    """Append a prompt to the queue. ``{"text": "..."}``."""
    if ENGINE.instances.get(title) is None:
        return JSONResponse(
            {"error": "instance not found: %s" % title}, status_code=404
        )
    text = str((payload or {}).get("text", ""))
    if not text.strip():
        return JSONResponse({"error": "empty prompt"}, status_code=400)
    try:
        _prompt_queue.enqueue(title, text)
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
    """Move one item ``up`` or ``down`` within the queue."""
    payload = payload or {}
    _prompt_queue.move_item(
        title, str(payload.get("id", "")), str(payload.get("direction", ""))
    )
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


_PR_CACHE: Dict[str, tuple] = {}  # branch -> (expires_epoch, info_or_None)


def _pr_info(wt: str, branch: str, force: bool = False):
    """Most recent PR for ``branch`` (any base) as ``{url, state}`` (cached
    60s), or None. ``state`` is OPEN / MERGED / CLOSED.

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
    _PR_CACHE[branch] = (now + 60, info)
    return info


def _gh_pr_info(wt: str, branch: str):
    """The ``gh`` rung of :func:`_pr_info` — ``{url, state}`` or None."""
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
            "url,state",
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
    return {"url": arr[0].get("url"), "state": arr[0].get("state")}


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


def _commit_shell_command() -> str:
    """The POSIX shell one-liner the commit endpoint types into the session's
    interactive shell (so the user watches the pre-commit hooks run live).

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
        "git commit -F {msgf}; rc=$?; "
        "[ $rc -eq 0 ] && break; "
        "git diff --quiet && break; "  # no auto-fix -> real failure
        "git add -A; n=$((n+1)); "
        "done; "
        'echo $rc > {status}; rm -f "$L"'
    ).format(
        msgf=shlex.quote(_COMMIT_MSG_FILE),
        status=_COMMIT_STATUS_FILE,
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

    def _do():
        # Keep our scratch files out of `git add -A`. Works for ANY session
        # type (plain / in-place / provisioned) — a neutral util, no
        # provisioning involved.
        try:
            _exclude_artifacts(Path(wt))
        except Exception:  # noqa: BLE001
            pass
        try:
            with open(msg_file, "w") as f:
                f.write(msg)
        except OSError:
            pass
        name, err = _ensure_shell_session(title, wt)
        if err is not None:
            return err
        _send_to_shell(name, _commit_shell_command())
        return None

    err = await asyncio.to_thread(_do)
    if err is not None:
        return JSONResponse({"error": err}, status_code=500)
    # The commit is about to change the stage — drop the probe memo so the
    # client's follow-up refresh sees fresh data instead of the 2.5s cache.
    _forget_probes(title)
    return JSONResponse({"ok": True, "tmux_name": _shell_tmux_name(title)})


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
_PR_REMEDY = "add a GitHub token in Settings → PR review, or install the GitHub CLI"


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


# --- Forced PR review (Settings → PR review) ----------------------------------
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

    async def _bg_review() -> None:
        # Provision first (slow: clone/fetch of the PR head), then register the
        # adopted workspace as a live session — same shape as the pipeline's
        # SessionRunner.run_pr, but against THIS server's engine so the session
        # shows up in the grid without a reload.
        try:
            directory, prompt, n_comments = await _pr_review.prepare_review(pr)
            inst = session.NewInstance(
                session.InstanceOptions(
                    title=title,
                    path=".",
                    program=ENGINE.default_program(),
                    provisioned=True,
                    workspace_strategy="clone",
                    new_branch=pr.head_ref,
                    prompt=prompt,
                    workspace_path=str(directory),
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
                with ENGINE.lock:
                    ENGINE.instances.pop(title, None)
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


# --- Ticket force-start (Settings → Ticketing) --------------------------------
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

    async def _bg_start() -> None:
        # Same shape as the pipeline's SessionRunner.run, but against THIS
        # server's engine so the session shows up in the grid without a
        # reload. The engine owns provisioning (inside Instance.Start), so
        # unlike the PR path there is no pre-provision step.
        marked = False
        try:
            prompt = _ticket_start.build_prompt(story)
            branch = _ticket_start.branch_for(story)
            # In-flight ledger marker BEFORE the slow launch, so a running
            # pipeline's scans treat the ticket as taken (orchestrator guard).
            _ticket_start.record_started(story)
            marked = True
            inst = session.NewInstance(
                session.InstanceOptions(
                    title=title,
                    path=".",
                    program=ENGINE.default_program(),
                    provisioned=True,
                    workspace_strategy=_ticket_start.workspace_mode(),
                    new_branch=branch,
                    prompt=prompt,
                    provision_repo_url=getattr(story, "repo_url", "") or "",
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
                with ENGINE.lock:
                    ENGINE.instances.pop(title, None)
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


# --- Issue force-start (Settings → Git issues) ---------------------------------
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

    async def _bg_start() -> None:
        # Same shape as the pipeline's issue loop (issue → Ticket → engine
        # session on a fresh branch), but against THIS server's engine so the
        # session shows up in the grid without a reload. The engine owns
        # provisioning (inside Instance.Start), like the ticket path.
        try:
            story, prompt, branch = await _issue_start.prepare_start(issue)
            inst = session.NewInstance(
                session.InstanceOptions(
                    title=title,
                    path=".",
                    program=ENGINE.default_program(),
                    provisioned=True,
                    workspace_strategy=_issue_start.workspace_mode(),
                    new_branch=branch,
                    prompt=prompt,
                    provision_repo_url=getattr(story, "repo_url", "") or "",
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
                with ENGINE.lock:
                    ENGINE.instances.pop(title, None)
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

    def _entry(path: str, name: str, root: str) -> dict:
        return {
            "name": name,
            "path": path,
            "root": root,
            "kind": _classify_workspace(os.path.basename(name) or name, root),
            "size_bytes": None,
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
            with ENGINE.lock:
                ENGINE.instances.pop(title, None)
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


@app.get("/api/instances/{title}/history")
def pane_history(title: str, pane: str = "agent") -> PlainTextResponse:
    """Full tmux scrollback of the agent/shell pane as plain text.

    The web terminals attach to tmux, so xterm.js only ever holds one screen —
    the real history lives in tmux on the server. The pane-header "Copy all"
    button fetches this and puts it on the clipboard, which is how you capture
    far more than a screenful (drag-selection can't scroll through tmux
    history; see attachDragAutoScroll in app.js).
    """
    inst = ENGINE.instances.get(title)
    if inst is None:
        return PlainTextResponse("instance not found", status_code=404)
    base = (
        _shell_tmux_name(title)
        if pane == "shell"
        else tmux.to_mindflock_tmux_name(title)
    )
    name = _live_session_name(base)
    if name is None:
        if pane != "shell":
            # Session gone but the provider transcript may still exist on disk.
            text = _agent_transcript_text(inst.GetWorktreePath())
            if text:
                return PlainTextResponse(text)
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
    if pane != "shell":
        # Sessions whose TUI sits on tmux's alternate screen have empty
        # history: the capture above only sees the visible frame. Prefer the
        # provider's transcript file for those, when one exists.
        hist = subprocess.run(
            ["tmux", "display-message", "-p", "-t", name, "#{history_size}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if hist.returncode == 0 and hist.stdout.strip() == "0":
            text = _agent_transcript_text(inst.GetWorktreePath())
            if text:
                return PlainTextResponse(text)
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
