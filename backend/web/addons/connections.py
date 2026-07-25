"""Connections addon: one place to see and attach the outside services.

MindFlock talks to a few external systems — the coding-agent CLI, GitHub (PRs
and PR-review ingestion), the active ticketing provider (Shortcut / Jira /
Linear / GitHub Issues / Asana ticket ingestion), and Tailscale (phone access).
Until now their state was scattered across separate Settings tabs, so a newcomer
had no single answer to "what am I connected to, and what still needs setting up?".

This addon aggregates that into ``GET /api/connections``: a cheap, network-free
status read (is a credential configured? where does it resolve from? is the CLI
authenticated?) built entirely from the *existing* signals —
:mod:`backend.doctor` checks and the settings store — so it never duplicates a
probe or holds its own source of truth. The live "does the token actually work"
check stays with the Settings addon's ``/api/settings/test/*`` endpoints, which
the frontend calls directly; "Configure" just opens the relevant Settings
screen. New files only: no edits to the core server or ``app.js``.
"""

from __future__ import annotations

import re
import time
from typing import List, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from backend import doctor
from backend.config import settings as settings_store

from .base import Addon, AppContext
from .settings import _github_token_source

# Commands we recognize as directly runnable, so a doctor `fix` string with no
# backtick-quoted span can still offer a copyable command.
_RUNNABLE = re.compile(r"^(sudo |brew |gh |uv |xcode-select|tailscale )")


def _fix_command(fix: str) -> str:
    """The copyable command inside a doctor ``fix`` hint (backtick span, or the
    whole hint when it's plainly a command); ``""`` when there's nothing to run."""
    if not fix:
        return ""
    m = re.search(r"`([^`]+)`", fix)
    if m:
        return m.group(1).strip()
    return fix.strip() if _RUNNABLE.match(fix.strip()) else ""


def _fix_fields(check, connected: bool) -> dict:
    """Remediation fields for a connection, sourced from a doctor Check.

    Only surfaced when NOT connected (a healthy service needs no fix)."""
    fix = "" if connected else (getattr(check, "fix", "") or "")
    return {
        "fix": fix.replace("`", ""),  # human hint, backticks stripped
        "fix_command": _fix_command(fix),  # copyable command ("" if none)
        "docs": getattr(check, "docs", "") or "",
    }


# Status vocabulary the frontend styles into a pill:
#   connected     — configured and healthy (green)
#   attention     — configured but something's off, or required and missing (amber)
#   not_connected — optional and not set up yet (calm/gray)
CONNECTED = "connected"
ATTENTION = "attention"
NOT_CONNECTED = "not_connected"


def _agent_connection() -> dict:
    """The coding-agent CLI new sessions launch (claude by default)."""
    cli = doctor.check_agent_cli()
    auth = doctor.check_agent_auth()
    ok = cli.status == "ok" and auth.status in ("ok", "info")
    if cli.status != "ok":
        detail = cli.detail or "agent CLI not found on PATH"
    elif auth.status not in ("ok", "info"):
        detail = auth.detail or "installed, but not signed in"
    else:
        detail = auth.detail or cli.detail or "installed and signed in"
    # The fix comes from whichever layer is failing: install first, then auth.
    fix_src = cli if cli.status != "ok" else auth
    return {
        "id": "agent",
        "name": "Coding agent",
        "purpose": "Runs your sessions — the CLI each agent window drives.",
        "required": True,
        "status": CONNECTED if ok else ATTENTION,
        "detail": detail,
        "settings_screen": "coding",
        "test_endpoint": "/api/settings/test/agent",
        **_fix_fields(fix_src, ok),
    }


def _github_connection() -> dict:
    """GitHub: opening PRs from a session and the automated PR-review loop."""
    source = _github_token_source()
    gh = doctor.check_gh()
    authenticated = gh.status == "ok" or bool(source)
    if authenticated:
        where = source or "gh-cli"
        detail = f"authenticated (token from {where})"
        status = CONNECTED
    elif gh.status == "warn":
        # gh is installed but not signed in — a nudge worth surfacing.
        detail = gh.detail or "gh installed, not authenticated — run `gh auth login`"
        status = ATTENTION
    else:
        # gh not installed (info/fail) and no token: GitHub is optional, so this
        # is the calm gray "off" state, not an attention-seeking failure.
        detail = "no token and gh CLI not installed — GitHub features off"
        status = NOT_CONNECTED

    # Surface the automated PR-review state so the feature is discoverable here,
    # not just buried in Settings. Off when explicitly disabled or no repo set.
    # Read defensively (getattr) so partial settings stubs stay supported.
    gh_settings = settings_store.load_settings().github
    repos = [r for r in (getattr(gh_settings, "repos", None) or []) if r]
    if not repos and getattr(gh_settings, "repo", ""):
        repos = [gh_settings.repo]
    pr_on = bool(repos) and getattr(gh_settings, "enabled", None) is not False
    if repos:
        where = repos[0] if len(repos) == 1 else f"{len(repos)} repos"
        pr_review = f"PR review {'on' if pr_on else 'off'} · {where}"
    else:
        pr_review = "PR review: no repos set"
    detail = f"{detail} · {pr_review}"

    return {
        "id": "github",
        "name": "GitHub",
        "purpose": "Push/open PRs, and auto-review your own open PRs.",
        "required": False,
        "status": status,
        "detail": detail,
        "settings_screen": "repo",
        "test_endpoint": "/api/settings/test/github",
        "pr_review_enabled": pr_on,
        **_fix_fields(gh, status == CONNECTED),
    }


_PROVIDER_LABELS = {
    "shortcut": "Shortcut",
    "jira": "Jira",
    "linear": "Linear",
    "github_issues": "GitHub Issues",
    "asana": "Asana",
}


def _source_connection(source, only: bool) -> dict:
    """A connection card for one ticketing source. ``only`` keeps the id/name
    generic ("ticketing"/"Ticketing") when it's the sole source, so the
    single-source UI is unchanged; extra sources get id ``ticketing:<id>``."""
    provider = (source.provider or "").strip().lower()
    base_label = _PROVIDER_LABELS.get(provider, "Ticketing")
    label = source.label or base_label

    has_secret = bool(source.api_token) or provider == "github_issues"
    needs_scope = provider in ("github_issues", "asana")
    scope_ok = bool(source.project) if needs_scope else True
    if has_secret and scope_ok:
        status, detail = CONNECTED, f"{label} configured"
    elif has_secret:
        status, detail = ATTENTION, f"{label} — set the project/scope to finish"
    else:
        status, detail = ATTENTION, f"{label} selected — add credentials"

    sid = source.id or provider
    return {
        "id": "ticketing" if only else f"ticketing:{sid}",
        "name": "Ticketing" if only else f"Ticketing · {label}",
        "purpose": "Auto-create a session for each ticket assigned to you.",
        "required": False,
        "status": status,
        "detail": detail,
        "provider": provider,
        "source_id": sid,
        "settings_screen": "ticketing",
        "test_endpoint": "/api/settings/test/ticketing",
        # A token/credential, not a CLI — the remedy is Configure, not a command.
        "fix": "",
        "fix_command": "",
        "docs": "",
    }


def _ticketing_connections() -> list[dict]:
    """One connection card per configured ticketing source (or a single
    'not configured' card when there are none)."""
    sources = settings_store.load_settings().ticketing.sources
    if not sources:
        return [
            {
                "id": "ticketing",
                "name": "Ticketing",
                "purpose": "Auto-create a session for each ticket assigned to you.",
                "required": False,
                "status": NOT_CONNECTED,
                "detail": "no ticketing provider configured",
                "provider": "",
                "source_id": "",
                "settings_screen": "ticketing",
                "test_endpoint": "/api/settings/test/ticketing",
                "fix": "",
                "fix_command": "",
                "docs": "",
            }
        ]
    only = len(sources) == 1
    return [_source_connection(s, only) for s in sources]


def _git_connection() -> dict:
    """Git: optional — unlocks isolated worktrees, diffs, commits and PRs."""
    git = doctor.check_git()
    connected = git.status == "ok"
    return {
        "id": "git",
        "name": "Git",
        "purpose": "Isolated worktrees per session, plus diff / commit / PR.",
        "required": False,
        "status": CONNECTED if connected else NOT_CONNECTED,
        "detail": git.detail
        or (
            "git present"
            if connected
            else "git not installed (optional — sessions run in plain folders)"
        ),
        # System-level dependency — no in-app credential. Point at Doctor.
        "settings_screen": "doctor",
        "test_endpoint": None,
        **_fix_fields(git, connected),
    }


def _tailscale_connection() -> dict:
    """Tailscale: reach the web UI from your phone at ``/m``."""
    ts = doctor.check_tailscale()
    connected = ts.status == "ok"
    return {
        "id": "tailscale",
        "name": "Phone access",
        "purpose": "Reach MindFlock from your phone over your tailnet.",
        "required": False,
        "status": CONNECTED if connected else NOT_CONNECTED,
        "detail": ts.detail
        or ("tailscale present" if connected else "tailscale not installed (optional)"),
        # System-level dependency — no in-app credential. Point at Doctor.
        "settings_screen": "doctor",
        "test_endpoint": None,
        **_fix_fields(ts, connected),
    }


def build_connections() -> List[dict]:
    """The ordered connection list. Required-but-not-connected sorts first so
    the newcomer's eye lands on what's actually blocking them."""
    conns = [
        _agent_connection(),
        _git_connection(),
        _github_connection(),
        *_ticketing_connections(),
        _tailscale_connection(),
    ]

    def _rank(c: dict) -> tuple:
        # 0: required & needs attention, 1: other attention, 2: connected, 3: calm
        if c["status"] == ATTENTION:
            return (0 if c["required"] else 1, c["name"])
        if c["status"] == CONNECTED:
            return (2, c["name"])
        return (3, c["name"])

    return sorted(conns, key=_rank)


# Short cache so the frontend's while-open auto-refresh doesn't re-run the
# doctor subprocess probes (gh auth status, tailscale, agent) on every poll.
# Kept small so "did my fix land?" stays responsive; `refresh=1` bypasses it.
_CACHE_TTL_S = 4.0
_cache = {"at": 0.0, "payload": None}


def _build_payload() -> dict:
    conns = build_connections()
    summary = {
        "connected": sum(1 for c in conns if c["status"] == CONNECTED),
        "attention": sum(1 for c in conns if c["status"] == ATTENTION),
        "total": len(conns),
    }
    return {"connections": conns, "summary": summary}


def cached_payload(refresh: bool = False) -> dict:
    now = time.monotonic()
    if (
        not refresh
        and _cache["payload"] is not None
        and now - _cache["at"] < _CACHE_TTL_S
    ):
        return _cache["payload"]
    payload = _build_payload()
    _cache["payload"] = payload
    # Stamp AFTER the build: the doctor probes above take seconds, and dating
    # the payload from before them silently shrinks its TTL (to nothing, when
    # a slow `gh`/network probe exceeds it — the very next poll rebuilt).
    _cache["at"] = time.monotonic()
    return payload


class ConnectionsAddon(Addon):
    id = "connections"
    label = "Connections"

    def __init__(self, ctx: Optional[AppContext] = None) -> None:
        super().__init__(ctx)
        self._router = self._build_router()

    def _build_router(self) -> APIRouter:
        router = APIRouter(prefix="/api/connections")

        @router.get("")
        def get_connections(refresh: bool = False) -> JSONResponse:
            # sync def → FastAPI threadpool, so the bounded doctor subprocess
            # probes never block the event loop. Cached ~4s (refresh=1 bypasses).
            return JSONResponse(cached_payload(refresh=refresh))

        return router

    @property
    def router(self) -> APIRouter:
        return self._router

    # No frontend module: the connection list is rendered inline inside the
    # Settings → Connections screen (app.js), reading this addon's
    # ``GET /api/connections``. There is no separate modal / "manager" surface.
