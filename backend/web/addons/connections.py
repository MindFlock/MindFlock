"""Connections addon: the dashboard of every outside thing MindFlock talks to.

``GET /api/connections`` answers, one row each and in three states
(connected / needs attention / off), for the agent CLIs (one per provider),
GitHub, each ticket source, your devices, your phone, people (peer links), the
MindFlock MCP auto-attach, the database client's saved connections, ntfy push,
extensions (loaded and failed), and the git / tailscale system dependencies.

Every row is a cheap, network-free read of an EXISTING signal — the provider
status Settings → Agent providers shows, :mod:`backend.doctor` checks, the
settings store, the device/peer/MCP/ntfy/dbclient/extension readers — so this
never duplicates a probe or holds its own source of truth beyond remembering
the last verdict of a Test (GitHub) or a phone load. The live "does it
actually work" check is each row's ``test`` (the existing ``/api/settings/test/*``
and friends), which the frontend calls directly. The row contract is
:func:`_row`; docs/web-api.md has the table.
"""

from __future__ import annotations

import os
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


# The row contract's three states (``status`` / ``connection``):
#   connected       — configured and seen working (green)
#   needs_attention — configured but something's off, with the reason (amber)
#   off             — not set up (calm/gray)
STATE_CONNECTED = "connected"
STATE_NEEDS_ATTENTION = "needs_attention"
STATE_OFF = "off"
STATES = (STATE_CONNECTED, STATE_NEEDS_ATTENTION, STATE_OFF)

# The status vocabulary the old client styled into a pill. Every row still
# carries it as ``legacy_status`` (one release), projected from ``status``.
CONNECTED = "connected"
ATTENTION = "attention"
NOT_CONNECTED = "not_connected"
LEGACY_STATUS = {
    STATE_CONNECTED: CONNECTED,
    STATE_NEEDS_ATTENTION: ATTENTION,
    STATE_OFF: NOT_CONNECTED,
}

#: Row kinds, in the order the screen groups them (and the list is sorted).
KINDS = (
    "agent",
    "github",
    "tracker",
    "devices",
    "mcp",
    "database",
    "notifications",
    "extension",
    "system",
)

#: ``action.kind`` values: ``dialog`` (a named in-app dialog: ``connect-github``,
#: ``agent-sign-in``, ``intake``, ``db-explorer``), ``screen`` (a Settings
#: screen key), ``route`` (an in-app URL), ``copy`` (a command to copy).
ACTION_KINDS = ("dialog", "screen", "route", "copy")


def _iso(ts: Optional[float] = None) -> str:
    """POSIX ``ts`` (now when None) as an ISO-8601 UTC string."""
    from datetime import datetime, timezone

    return (
        datetime.fromtimestamp(time.time() if ts is None else ts, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
    )


def _action(label: str, kind: str, target: str, **extra) -> dict:
    return {"label": label, "kind": kind, "target": target, **extra}


def _test(endpoint: str, body: Optional[dict] = None, method: str = "POST") -> dict:
    out: dict = {"endpoint": endpoint, "method": method}
    if body is not None:
        out["body"] = body
    return out


def _row(
    id: str,
    name: str,
    kind: str,
    status: str,
    detail: str,
    action: Optional[dict],
    test: Optional[dict] = None,
    *,
    purpose: str = "",
    required: bool = False,
    settings_screen: str = "",
    fix: str = "",
    fix_command: str = "",
    docs: str = "",
    updated_at: str = "",
    **extra,
) -> dict:
    """One Connections row in the contract every row shares.

    ``status`` is the three-state answer; ``connection`` repeats it (the name
    the ticket-source rows and the Intake card already used) and
    ``legacy_status`` projects it onto the old client's vocabulary.
    ``settings_screen`` / ``test_endpoint`` / ``purpose`` / ``required`` are
    the old row fields, kept for one release."""
    return {
        "id": id,
        "name": name,
        "kind": kind,
        "status": status,
        "connection": status,
        "legacy_status": LEGACY_STATUS[status],
        "detail": detail,
        "action": action,
        "test": test,
        "fix": fix,
        "fix_command": fix_command,
        "docs": docs,
        "updated_at": updated_at or _iso(),
        "purpose": purpose,
        "required": required,
        "settings_screen": settings_screen,
        "test_endpoint": (test or {}).get("endpoint"),
        **extra,
    }


# --------------------------------------------------------------------------- #
# Agent CLIs — one row per provider
# --------------------------------------------------------------------------- #
def _provider_statuses() -> tuple:
    """``(default_name, [status dicts], [load errors])`` — exactly what
    ``GET /api/providers/status`` answers (Settings → Agent providers), read
    in-process. A seam the tests stub."""
    from backend import providers
    from backend.providers import config as provider_config

    from .settings import _default_provider_name, _provider_status

    default = _default_provider_name()
    rows = [
        _provider_status(p, default)
        for p in providers.all_providers()
        if p.name != "generic"
    ]
    return default, rows, provider_config.load_errors()


def _sign_in_profile(provider: str) -> str:
    """The default auth profile's id when it is an ``account`` login of
    ``provider`` (Sign in then runs in that account's config dir), else ``""``."""
    try:
        ap = settings_store.load_settings().auth_profiles
        prof = ap.get(ap.default_profile) if ap.default_profile else None
    except Exception:  # noqa: BLE001 — a profile is a refinement, never required
        return ""
    if prof is None or prof.kind != "account":
        return ""
    return prof.id if (prof.provider or "claude") == provider else ""


def _agent_label(name: str) -> str:
    return name[:1].upper() + name[1:] if name else "Agent"


#: Each shipped agent's product name, as every surface says it (the frontend's
#: lib/agentNames.ts carries the same map). A custom agent reads as its id.
AGENT_NAMES = {
    "claude": "Claude Code",
    "codex": "Codex",
    "aider": "Aider",
    "antigravity": "Antigravity",
    "opencode": "OpenCode",
    "cline": "Cline",
    "goose": "Goose",
}


def agent_product_name(name: str) -> str:
    return AGENT_NAMES.get(name) or _agent_label(name)


def _agent_account(name: str) -> str:
    """The account an agent is signed in as, when its own config names one
    (Claude Code's ``oauthAccount.emailAddress``), else ``""``. A read of a
    file the CLI already wrote: never a network call, never raises."""
    if name != "claude":
        return ""
    import json
    from pathlib import Path

    paths = []
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    if cfg:
        paths.append(Path(cfg) / ".claude.json")
    paths.append(Path.home() / ".claude.json")
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        acct = data.get("oauthAccount") if isinstance(data, dict) else None
        if isinstance(acct, dict):
            email = str(acct.get("emailAddress") or "").strip()
            if email:
                return email
    return ""


def agent_test_line(
    name: str, cli_status: str, auth_status: str, auth_detail: str
) -> str:
    """The one plain sentence an agent's Test answers — the account it is
    signed in as, never the file the login was found in (that stays in the
    ``auth`` detail for anyone who looks)."""
    label = agent_product_name(name)
    if cli_status != "ok":
        return label + " isn't installed"
    if auth_status == "ok":
        account = _agent_account(name)
        if account:
            return "Signed in as " + account
        if "API_KEY" in auth_detail:
            return "Signed in with an API key"
        return "Signed in"
    if auth_status == "warn":
        return "Installed, not signed in yet"
    return "Installed. MindFlock can't check its sign-in — fine if it works."


def _agent_auth_fix(name: str, binary: str) -> tuple:
    """``(fix line, runnable command)`` the doctor gives a logged-out agent
    (``doctor._login_fix``: its login command, else the API keys it reads).
    ``("", "")`` when the registry can't produce the provider."""
    try:
        from backend import doctor

        provider = doctor._agent_provider(name)
        if provider is None:
            return "", ""
        base = os.path.basename((binary.split() or [name])[0]) or name
        return doctor._login_fix(provider, base)
    except Exception:  # noqa: BLE001 — a fix line must never break the row
        return "", ""


def _agent_row(st: dict) -> dict:
    """One provider's row. Connected = installed and signed in (or nothing to
    sign in to, or no way to tell); needs attention = installed but its login
    probe found nothing; off = not installed. The DEFAULT provider is what
    every new session runs, so it missing reads as needs attention, not off."""
    name = str(st.get("name") or "")
    default = bool(st.get("is_default"))
    label = _agent_label(name)
    installed = bool(st.get("installed"))
    has_login = bool(st.get("has_login"))
    probe = bool(st.get("auth_probe"))
    authed = bool(st.get("authenticated"))
    path = str(st.get("path") or "")
    hint = str(st.get("install_hint") or "")
    login = str(st.get("login_command") or "")
    test = _test("/api/connections/test/agent", {"provider": name})
    screen = "coding" if default else "providers"
    common = dict(
        purpose="Runs your sessions — the CLI each agent window drives.",
        required=default,
        settings_screen=screen,
        test=test,
        provider=name,
        default=default,
    )
    rid = "agent" if default else "agent:" + name
    rname = "Coding agent · " + label
    if not installed:
        # `binary` is the provider's base command ("cline -i", "goose
        # session"): name the program, not its arguments.
        prog = (str(st.get("binary") or "").split() or [name])[0]
        why = str(st.get("launch_hint") or "") or (f"`{prog}` not found on PATH")
        return _row(
            rid,
            rname,
            "agent",
            STATE_NEEDS_ATTENTION if default else STATE_OFF,
            "not installed — " + why,
            (
                # The default agent is in the doctor's install plan: the same
                # one-click Install the Setup checklist offers (the frontend
                # falls back to the copy line when the plan lacks it).
                _action("Install", "dialog", "agent-install", provider=name)
                if hint and default
                else (
                    _action("Copy install command", "copy", hint)
                    if hint
                    else _action("Set up", "screen", "providers")
                )
            ),
            fix=("install it: " + hint) if hint else why,
            fix_command=hint,
            **common,
        )
    where = path or "installed"
    account = ""
    if authed:
        detail = f"signed in ({st.get('auth_detail')}) · {where}"
        account = _agent_account(name)
    elif probe and not has_login:
        # It named where its credentials live (aider: API-key env vars) and
        # none are there. No login flow to offer — a "Sign in" button would
        # only open the agent's REPL — so the doctor's own fix says what to set.
        fix, fix_cmd = _agent_auth_fix(name, str(st.get("binary") or ""))
        return _row(
            rid,
            rname,
            "agent",
            STATE_NEEDS_ATTENTION,
            f"installed, not signed in · {where}",
            _action("Set up", "screen", screen),
            fix=fix or "sign in to " + label,
            fix_command=fix_cmd,
            **common,
        )
    elif not has_login:
        detail = f"no sign-in needed · {where}"
    elif not probe:
        # Doctor doctrine: a CLI that keeps its login where we never look
        # reads as "can't tell", never as logged out.
        detail = f"installed · sign-in can't be checked from here · {where}"
    else:
        profile = _sign_in_profile(name)
        return _row(
            rid,
            rname,
            "agent",
            STATE_NEEDS_ATTENTION,
            f"installed, not signed in · {where}",
            _action(
                "Sign in",
                "dialog",
                "agent-sign-in",
                provider=name,
                **({"profile": profile} if profile else {}),
            ),
            fix=("sign in: " + login) if login else "sign in to " + label,
            fix_command=login,
            **common,
        )
    # Connected: the card's own action is Test. The default agent's row also
    # leads to Settings → Agents, where you change WHICH agent new sessions
    # use — not to a second copy of this same card.
    return _row(
        rid,
        rname,
        "agent",
        STATE_CONNECTED,
        detail,
        _action("Change agent", "screen", "coding") if default else None,
        **common,
        **({"account": account} if account else {}),
    )


def _agent_connections() -> list[dict]:
    """A row per agent provider, the default first, then any custom provider
    TOML that failed to load (needs attention: name the file and why)."""
    default, statuses, errors = _provider_statuses()
    rows = [_agent_row(st) for st in statuses]
    rows.sort(key=lambda r: 0 if r.get("default") else 1)
    for err in errors or []:
        f = str(err.get("file") or "")
        rows.append(
            _row(
                "agent-config:" + f,
                "Coding agent · " + (f or "custom provider"),
                "agent",
                STATE_NEEDS_ATTENTION,
                f"{f}: {err.get('error') or 'did not load'}",
                _action("Open", "screen", "providers"),
                purpose="A custom agent CLI definition (TOML).",
                settings_screen="providers",
                fix="fix the file, then reload providers",
            )
        )
    return rows


#: The remedy we print whenever PR create/merge cannot proceed. A token leads
#: because it is the better answer of the two: nothing to install, it works the
#: same over SSH and HTTPS remotes, and it is what the REST fallback uses. This
#: exact sentence is asserted in tests and mirrored in the docs — keep it verbatim.
_NO_CREDENTIAL_FIX = (
    "add a GitHub token in Settings → Connections → GitHub → Connect, "
    "or install the GitHub CLI"
)
#: What the GitHub row's Configure opens: the Connect GitHub dialog (the
#: frontend's ``openConnectGitHub()`` / the ``mf:open-connect-github`` window
#: event), not a settings screen — the credential has one home now.
CONNECT_GITHUB_DIALOG = "connect-github"


#: The last ``POST /api/settings/test/github`` verdict (the settings addon
#: hands it over through :func:`note_github_test`), in memory: ``{ok, login,
#: token_source, detail, at}``. It answers "token, but the check failed".
_GITHUB_TEST: dict = {}


def forget_github_test() -> None:
    """The credential changed: the remembered Test verdict is about the OLD
    token, so the GitHub row goes back to its live signal until the next Test."""
    _GITHUB_TEST.clear()


def note_github_test(result: dict) -> None:
    """Remember the Connect GitHub Test's answer (never a token: the result
    carries none)."""
    _GITHUB_TEST.clear()
    _GITHUB_TEST.update(
        {
            "ok": bool(result.get("ok")),
            "login": str(result.get("login") or ""),
            "token_source": str(result.get("token_source") or ""),
            "detail": str(result.get("detail") or ""),
            "at": time.time(),
        }
    )


def _github_connection() -> dict:
    """GitHub: opening PRs from a session and the automated PR-review loop.

    Deliberately NOT about pushing. A push is plain ``git push`` over whatever
    remote (SSH or HTTPS) the user configured themselves — it never goes through
    this connection, so an unconfigured GitHub must not read as "you cannot
    push".

    Connected = a credential resolves (a token, or a signed-in gh) and the last
    Test did not fail; needs attention = the last Test with this credential
    failed (a revoked token, a repo it cannot see); off = no credential.
    """
    source = _github_token_source()
    gh = doctor.check_gh()
    authenticated = gh.status == "ok" or bool(source)
    where = source or "gh-cli"
    last = dict(_GITHUB_TEST)
    # A verdict only vouches for the credential it tested.
    if last and last.get("token_source") not in ("", where):
        last = {}
    login = last.get("login") or ""
    if authenticated and last and not last.get("ok"):
        detail = last.get("detail") or f"token from {where} failed its last test"
        status = STATE_NEEDS_ATTENTION
        fix_fields = {
            "fix": "reconnect GitHub (Connect), or fix the repo it cannot reach",
            "fix_command": "",
            "docs": getattr(gh, "docs", "") or "",
        }
    elif authenticated:
        who = f" as @{login}" if login else ""
        detail = f"authenticated{who} (token from {where})"
        status = STATE_CONNECTED
        fix_fields = _fix_fields(gh, True)
    elif gh.status == "warn":
        # gh is installed but not signed in. Still no credential (off), but
        # its own fix (`gh auth login`) is a real one-liner: offer it.
        detail = gh.detail or "gh installed, not authenticated — run `gh auth login`"
        status = STATE_OFF
        fix_fields = _fix_fields(gh, False)
    else:
        # Neither a token nor gh. Say precisely what is lost (PR create/merge and
        # the review loop) and what is not (pushing), then name BOTH remedies with
        # the token first. Offering only gh's `brew install gh` here — which is
        # what the doctor Check carries — would send people to install a CLI they
        # may not want for a job a token already does, over any transport.
        detail = (
            "no GitHub token and no gh CLI — PR create/merge and PR review are off "
            "(pushing still works: it uses plain git over your own remote)"
        )
        status = STATE_OFF
        # No fix_command on purpose: the primary remedy is Connect (which opens
        # the Connect GitHub dialog) or $GH_TOKEN, neither of which is a shell command.
        # The gh install stays a secondary mention inside the hint, not a button.
        fix_fields = {
            "fix": _NO_CREDENTIAL_FIX,
            "fix_command": "",
            "docs": getattr(gh, "docs", "") or "",
        }

    # Surface the automated PR-review state so the feature is discoverable here,
    # not just buried in Settings. Off when explicitly disabled or no repo set.
    # Read defensively (getattr) so partial settings stubs stay supported.
    gh_settings = settings_store.load_settings().github
    repos = [r for r in (getattr(gh_settings, "repos", None) or []) if r]
    if not repos and getattr(gh_settings, "repo", ""):
        repos = [gh_settings.repo]
    pr_on = bool(repos) and getattr(gh_settings, "enabled", None) is not False
    if repos:
        where_repos = repos[0] if len(repos) == 1 else f"{len(repos)} repos"
        pr_review = f"PR review {'on' if pr_on else 'off'} · {where_repos}"
    else:
        pr_review = "PR review: no repos set"
    detail = f"{detail} · {pr_review}"
    # "Docs" is MindFlock's own help for this connection, not the gh CLI's
    # homepage (a token does gh's job; most people never install gh).
    fix_fields["docs"] = GITHUB_HELP_URL

    return _row(
        "github",
        "GitHub",
        "github",
        status,
        detail,
        _action(
            "Manage" if status == STATE_CONNECTED else "Connect",
            "dialog",
            CONNECT_GITHUB_DIALOG,
        ),
        _test("/api/settings/test/github"),
        purpose="Open and merge PRs, and auto-review your own open PRs.",
        # Connect opens the Connect GitHub dialog, not the repo screen: the
        # screen key stays for older frontends that know nothing else.
        settings_screen="repo",
        configure_dialog=CONNECT_GITHUB_DIALOG,
        pr_review_enabled=pr_on,
        token_source=source or ("gh-cli" if gh.status == "ok" else ""),
        account=login,
        updated_at=_iso(last["at"]) if last.get("at") else "",
        **fix_fields,
    )


#: The GitHub row's "Docs": docs/web-ui.md → Connect GitHub, on GitHub.
GITHUB_HELP_URL = (
    "https://github.com/MindFlock/MindFlock/blob/main/docs/web-ui.md#connect-github"
)

_PROVIDER_LABELS = {
    "shortcut": "Shortcut",
    "jira": "Jira",
    "linear": "Linear",
    "github_issues": "GitHub Issues",
    "asana": "Asana",
}


#: How long a passed test vouches for a source. Tokens get revoked and sites
#: move; a green row should mean "seen working today", not "worked once".
TEST_FRESH_SECONDS = 24 * 3600

#: The three states a ticket source can be in, as the Intake card and this
#: endpoint both spell them (the card derives the same answer client-side from
#: the source's ``last_test`` — see frontend/src/components/intake/sourceForm.ts).
SOURCE_CONNECTED = "connected"
SOURCE_NEEDS_ATTENTION = "needs_attention"
SOURCE_OFF = "off"


def _parse_at(raw) -> float:
    """``last_test.at`` (ISO-8601) as a POSIX time; ``0.0`` when unparseable."""
    from datetime import datetime, timezone

    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def source_test_state(source, now: float | None = None) -> tuple[str, str]:
    """``(state, message)`` for one ticketing source — the rule Connections
    and the Intake card share.

    * ``off`` — no credential at all (nothing could be tested).
    * ``connected`` — the stored ``last_test`` passed within
      :data:`TEST_FRESH_SECONDS`, and every field Test is meant to fill in
      (:func:`missing_auto_fields`) is filled.
    * ``needs_attention`` — anything else, with the reason: the test failed
      (its message), it is older than a day, it never ran since this
      configuration was saved, or Test still has a field to fill in.

    ``source`` is a :class:`TicketingSource` or anything with its attributes.
    """
    from backend.ticket_ingestion.providers import missing_auto_fields

    provider = (getattr(source, "provider", "") or "").strip().lower()
    has_secret = bool(getattr(source, "api_token", "")) or provider == "github_issues"
    if not has_secret:
        return SOURCE_OFF, "no credentials yet"
    values = {
        k: getattr(source, k, "")
        for k in ("provider", "api_token", "member_id", "project", "base_url", "email")
    }
    test = getattr(source, "last_test", None) or {}
    if not isinstance(test, dict) or not test:
        return SOURCE_NEEDS_ATTENTION, "not tested yet — run Test"
    message = str(test.get("message") or "")
    if not test.get("ok"):
        return SOURCE_NEEDS_ATTENTION, message or "the last test failed"
    missing = missing_auto_fields(values)
    if missing:
        return (
            SOURCE_NEEDS_ATTENTION,
            message + " — " + ", ".join(missing) + " still to fill in (run Test)",
        )
    at = _parse_at(test.get("at"))
    if (now if now is not None else time.time()) - at > TEST_FRESH_SECONDS:
        return SOURCE_NEEDS_ATTENTION, "last tested over a day ago — retest"
    return SOURCE_CONNECTED, message or "Connected"


def _tracker_action(source_id: str = "") -> dict:
    """Connect for a ticket source: Intake → Tickets (where its card lives)."""
    return _action(
        "Open in Intake", "dialog", "intake", tab="tickets", source_id=source_id
    )


_TRACKER_PURPOSE = (
    "Start sessions from tickets in Shortcut, Jira, Linear, Asana or GitHub Issues."
)


def _source_connection(source, only: bool) -> dict:
    """A connection row for one ticketing source. ``only`` keeps the id/name
    generic ("ticketing"/"Ticketing") when it's the sole source, so the
    single-source UI is unchanged; extra sources get id ``ticketing:<id>``.

    ``connected`` means **tested**: the three-way ``status`` comes from
    :func:`source_test_state`; ``legacy_status`` is its projection
    (connected → connected, needs_attention → attention, off →
    not_connected)."""
    provider = (source.provider or "").strip().lower()
    base_label = _PROVIDER_LABELS.get(provider, "Ticketing")
    label = source.label or base_label

    state, message = source_test_state(source)
    if provider == "github_issues" and source.api_token and state != SOURCE_OFF:
        # Still read as the last fallback this release, then ignored: say so
        # here, where the person looks when something is "not connected".
        state, message = SOURCE_NEEDS_ATTENTION, (
            "still has its own token — move it to Settings → Connections → "
            "GitHub → Connect (read as a fallback this release, ignored from "
            "the next)"
        )
    if state == SOURCE_OFF:
        detail = f"{label} selected — add credentials"
    else:
        detail = f"{label} — {message}"
    test = getattr(source, "last_test", None) or {}
    tested_at = str(test.get("at") or "") if isinstance(test, dict) else ""

    sid = source.id or provider
    return _row(
        "ticketing" if only else f"ticketing:{sid}",
        "Ticketing" if only else f"Ticketing · {label}",
        "tracker",
        state,
        detail,
        _tracker_action(sid),
        _test("/api/settings/test/ticketing", {"id": sid, "record": True}),
        purpose=_TRACKER_PURPOSE,
        settings_screen="ticketing",
        updated_at=tested_at,
        message=message,
        tested_at=tested_at,
        provider=provider,
        source_id=sid,
    )


def _ticketing_connections() -> list[dict]:
    """One connection row per configured ticketing source (or a single
    'not configured' row when there are none)."""
    sources = settings_store.load_settings().ticketing.sources
    if not sources:
        return [
            _row(
                "ticketing",
                "Ticketing",
                "tracker",
                STATE_OFF,
                "no ticketing provider configured",
                _tracker_action(),
                purpose=_TRACKER_PURPOSE,
                settings_screen="ticketing",
                message="",
                tested_at="",
                provider="",
                source_id="",
            )
        ]
    only = len(sources) == 1
    return [_source_connection(s, only) for s in sources]


# --------------------------------------------------------------------------- #
# Your devices · Your phone · People
# --------------------------------------------------------------------------- #
def _paired_devices() -> list[dict]:
    """``[{key, name, reachable}]`` for every device this one is paired with:
    a stored device token, or a live member of this device's group. A seam
    the tests stub; never raises."""
    try:
        from backend.web.core import fleet, remote

        me = str(remote.self_identity().get("key") or "")
        keys = dict.fromkeys(remote.paired_tokens())
        try:
            members = fleet.live_members()
        except Exception:  # noqa: BLE001 — a broken group store lists no members
            members = {}
        for k in members:
            keys.setdefault(k, None)
        out = []
        for key in keys:
            if not key or key == me:
                continue
            dev = remote._DEVICES.get(key) or {}
            host = dev.get("host") or (members.get(key) or {}).get("host") or key
            out.append(
                {"key": key, "name": str(host), "reachable": bool(dev.get("reachable"))}
            )
        return out
    except Exception:  # noqa: BLE001 — a status read never breaks the list
        return []


def _names(items: list[dict], limit: int = 3) -> str:
    names = [str(i.get("name") or "") for i in items][:limit]
    more = len(items) - len(names)
    return ", ".join(names) + (f" +{more} more" if more > 0 else "")


def _devices_connection() -> dict:
    """Your devices: connected when one paired device answers; needs
    attention when every paired one is unreachable (named); off when none."""
    devs = _paired_devices()
    up = [d for d in devs if d.get("reachable")]
    down = [d for d in devs if not d.get("reachable")]
    if up:
        status = STATE_CONNECTED
        detail = f"{len(up)} of {len(devs)} reachable: {_names(up)}"
        if down:
            detail += f" · unreachable: {_names(down)}"
    elif down:
        status = STATE_NEEDS_ATTENTION
        detail = f"unreachable: {_names(down)}"
    else:
        status = STATE_OFF
        detail = "no other computer paired yet"
    return _row(
        "devices",
        "Your devices",
        "devices",
        status,
        detail,
        _action("Manage" if devs else "Add a device", "screen", "devices"),
        purpose="Your other computers, driven from here.",
        settings_screen="devices",
        devices=devs,
    )


#: A phone that opened /m within this long reads as connected.
PHONE_FRESH_SECONDS = 7 * 24 * 3600

#: When a phone last loaded ``/m`` from off this machine (server.py's ``/m``
#: route calls :func:`note_phone_load`). In memory; the setup step's
#: forward-only ``phone.at`` is the fallback across restarts.
_PHONE: dict = {"at": 0.0}


def note_phone_load() -> None:
    _PHONE["at"] = time.time()


def _phone_last_seen() -> float:
    """POSIX time a phone last opened ``/m`` (0.0 = never known). A seam."""
    at = float(_PHONE.get("at") or 0.0)
    try:
        from backend.web.core import setup_steps

        step = setup_steps.state().get("phone") or {}
        if step.get("done") and step.get("at"):
            at = max(at, _parse_at(step["at"]))
    except Exception:  # noqa: BLE001
        pass
    return at


def _ago(seconds: float) -> str:
    if seconds < 3600:
        return "within the hour"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    days = int(seconds // 86400)
    return f"{days} day{'s' if days != 1 else ''} ago"


def _phone_connection() -> dict:
    at = _phone_last_seen()
    age = time.time() - at if at else None
    if age is not None and age <= PHONE_FRESH_SECONDS:
        status, detail = STATE_CONNECTED, f"a phone opened /m {_ago(age)}"
    elif at:
        status, detail = STATE_OFF, f"no phone has opened /m since {_ago(age or 0)}"
    else:
        status, detail = STATE_OFF, "no phone has opened /m yet — scan the QR"
    return _row(
        "phone",
        "Your phone",
        "devices",
        status,
        detail,
        _action("Set up" if status == STATE_OFF else "Manage", "screen", "mobile"),
        purpose="Watch and steer sessions from your phone (/m).",
        settings_screen="mobile",
        updated_at=_iso(at) if at else "",
    )


def _peer_links() -> list[dict]:
    """``[{name, connected}]`` per peer link (People). A seam; never raises."""
    try:
        from backend.peer.service import get_service

        svc = get_service()
        return [
            {
                "name": str(getattr(link, "peer_name", "") or "peer"),
                "connected": bool(svc.is_connected(getattr(link, "link_id", ""))),
            }
            for link in svc.store.list()
        ]
    except Exception:  # noqa: BLE001 — no store yet, or the feature is off
        return []


def _peers_connection() -> dict:
    links = _peer_links()
    up = [lk for lk in links if lk.get("connected")]
    down = [lk for lk in links if not lk.get("connected")]
    if up:
        status = STATE_CONNECTED
        detail = f"{len(up)} of {len(links)} up: {_names(up)}"
        if down:
            detail += f" · down: {_names(down)}"
    elif down:
        status, detail = STATE_NEEDS_ATTENTION, f"down: {_names(down)}"
    else:
        status, detail = STATE_OFF, "no peer links yet"
    return _row(
        "peers",
        "People",
        "devices",
        status,
        detail,
        _action("Manage" if links else "Invite someone", "screen", "peer"),
        purpose="Other people's MindFlock, linked to yours.",
        settings_screen="peer",
    )


# --------------------------------------------------------------------------- #
# MindFlock MCP auto-attach
# --------------------------------------------------------------------------- #
#: The engine (set by :class:`ConnectionsAddon`) — the MCP row looks up which
#: agent the last launch ran.
_ENGINE: dict = {"engine": None}


def _launch_provider(tmux_name: str) -> tuple:
    """``(title, provider)`` of the session whose tmux name is ``tmux_name``
    (``("", "")`` when it is not a known session)."""
    engine = _ENGINE.get("engine")
    try:
        from backend import providers
        from backend.session import tmux

        for inst in list((getattr(engine, "instances", None) or {}).values()):
            title = str(getattr(inst, "Title", "") or "")
            if title and tmux.to_mindflock_tmux_name(title) == tmux_name:
                prog = str(getattr(inst, "Program", "") or "")
                return title, providers.resolve(prog).name if prog else ""
    except Exception:  # noqa: BLE001
        pass
    return "", ""


def _mcp_state() -> tuple:
    """``(enabled, last_launch, supported providers)``. A seam."""
    from backend.providers import mcp_attach

    return (
        mcp_attach.enabled(),
        mcp_attach.last_launch(),
        mcp_attach.supported_providers(),
    )


def _mcp_connection() -> dict:
    on, last, supported = _mcp_state()
    if not on:
        status, detail = (
            STATE_OFF,
            "auto-attach is off — agents launch without the MindFlock tools",
        )
    elif not last:
        status, detail = (
            STATE_CONNECTED,
            "on · no session launched since MindFlock started",
        )
    elif last.get("attached"):
        title, prov = _launch_provider(str(last.get("session") or ""))
        what = f"{prov} in {title}" if title else str(last.get("session") or "")
        status, detail = STATE_CONNECTED, f"on · the last launch ({what}) has the tools"
    else:
        title, prov = _launch_provider(str(last.get("session") or ""))
        who = prov or "the agent"
        where = f" in {title}" if title else ""
        why = (
            f" — {prov} has no MCP attach support"
            if prov and prov not in (supported or [])
            else " — relaunch it to attach"
        )
        status = STATE_NEEDS_ATTENTION
        detail = f"on, but the last launch ({who}{where}) got no MindFlock tools{why}"
    return _row(
        "mcp",
        "Agent orchestration (MCP)",
        "mcp",
        status,
        detail,
        _action("Manage", "screen", "orchestration"),
        purpose="Agents get the MindFlock tools: spawn, message and ship sessions.",
        settings_screen="orchestration",
        updated_at=_iso(last["at"]) if last.get("at") else "",
        provider=_launch_provider(str(last.get("session") or ""))[1] if last else "",
    )


# --------------------------------------------------------------------------- #
# Databases (the dbclient extension's saved profiles)
# --------------------------------------------------------------------------- #
def _db_profiles() -> list[dict]:
    """The dbclient's saved profiles (no credentials). A seam."""
    try:
        from .dbclient import store

        return store.profile_summaries()
    except Exception:  # noqa: BLE001
        return []


def _db_connections() -> list[dict]:
    explorer = _action("Open explorer", "dialog", "db-explorer")
    profiles = _db_profiles()
    if not profiles:
        return [
            _row(
                "databases",
                "Databases",
                "database",
                STATE_OFF,
                "no saved database connections",
                _action("Add a connection", "dialog", "db-explorer"),
                purpose="The Database Client's saved connections.",
            )
        ]
    rows = []
    for p in profiles:
        last = p.get("last_test") or {}
        engine = str(p.get("engine") or "")
        where = " · ".join(x for x in (engine, p.get("host") or p.get("database")) if x)
        if not last:
            status, detail = (
                STATE_NEEDS_ATTENTION,
                f"{where} — not tested yet, run Test",
            )
        elif last.get("ok"):
            server = last.get("server") or "reachable"
            status, detail = STATE_CONNECTED, f"{where} — {server}"
        else:
            status = STATE_NEEDS_ATTENTION
            detail = f"{where} — {last.get('error') or 'the last test failed'}"
        pid = str(p.get("id") or "")
        rows.append(
            _row(
                "database:" + pid,
                "Database · " + str(p.get("name") or pid),
                "database",
                status,
                detail,
                explorer,
                _test(f"/api/dbclient/connections/{pid}/test"),
                purpose="The Database Client's saved connections.",
                updated_at=_iso(last["at"]) if last.get("at") else "",
                profile_id=pid,
            )
        )
    return rows


# --------------------------------------------------------------------------- #
# ntfy push
# --------------------------------------------------------------------------- #
def _ntfy_state() -> dict:
    """``notify.ntfy_status()`` — a seam."""
    from .notify import ntfy_status

    return ntfy_status()


def _ntfy_connection() -> dict:
    try:
        st = _ntfy_state()
    except Exception:  # noqa: BLE001
        st = {}
    last = st.get("last") or {}
    if not st.get("configured"):
        status, detail = STATE_OFF, "no ntfy topic set — phone pushes are off"
    elif not st.get("enabled"):
        status, detail = STATE_OFF, "a topic is set, but ntfy pushes are turned off"
    elif last and not last.get("ok"):
        status = STATE_NEEDS_ATTENTION
        detail = f"the last push failed: {last.get('error') or 'unknown error'}"
    elif last:
        status, detail = (
            STATE_CONNECTED,
            f"on · last push delivered via {st.get('server') or 'ntfy'}",
        )
    else:
        status, detail = (
            STATE_CONNECTED,
            f"on · {st.get('server') or 'ntfy'} (no push sent yet)",
        )
    return _row(
        "ntfy",
        "Phone push (ntfy)",
        "notifications",
        status,
        detail,
        _action(
            "Set up" if status == STATE_OFF else "Manage", "screen", "notifications"
        ),
        _test("/api/notify/ntfy/test") if st.get("configured") else None,
        purpose="Push alerts to your phone when an agent needs you.",
        settings_screen="notifications",
        updated_at=_iso(last["ts"]) if last.get("ts") else "",
    )


# --------------------------------------------------------------------------- #
# Extensions
# --------------------------------------------------------------------------- #
def _extension_state() -> tuple:
    """``([loaded extension addons], [load failures])`` — a seam."""
    from backend.web import addons as _addons

    loaded = [a for a in _addons.registered_addons() if a.extension() is not None]
    return loaded, _addons.load_failures()


def _extension_connections() -> list[dict]:
    try:
        loaded, failed = _extension_state()
    except Exception:  # noqa: BLE001
        loaded, failed = [], []
    disabled = set(
        getattr(settings_store.load_settings().extensions, "disabled", []) or []
    )
    manage = _action("Manage", "screen", "extensions")
    rows = []
    for a in loaded:
        aid = str(getattr(a, "id", "") or "")
        origin = (
            "~/.mindflock/extensions"
            if getattr(a, "origin", "") == "user"
            else "built-in"
        )
        off = aid in disabled
        rows.append(
            _row(
                "extension:" + aid,
                "Extension · " + str(getattr(a, "label", "") or aid),
                "extension",
                STATE_OFF if off else STATE_CONNECTED,
                f"{'disabled' if off else 'loaded'} · {origin}",
                manage,
                purpose="Adds a sidebar bar, commands and windows.",
                settings_screen="extensions",
            )
        )
    for f in failed:
        fid = str(f.get("id") or "")
        rows.append(
            _row(
                "extension-failed:" + fid,
                "Extension · " + fid,
                "extension",
                STATE_NEEDS_ATTENTION,
                f"failed to load: {f.get('file') or fid}: {f.get('error') or 'error'}",
                manage,
                purpose="Adds a sidebar bar, commands and windows.",
                settings_screen="extensions",
                fix="fix the extension, then restart MindFlock (extensions load at startup)",
                updated_at=_iso(f["at"]) if f.get("at") else "",
                file=str(f.get("file") or ""),
                error=str(f.get("error") or ""),
            )
        )
    return rows


def _system_row(id: str, name: str, check, purpose: str, absent: str) -> dict:
    """A system dependency (git, tailscale): no in-app credential, so its
    action is the install command when there is one, else Doctor."""
    connected = check.status == "ok"
    fields = _fix_fields(check, connected)
    cmd = fields["fix_command"]
    action = (
        _action("Copy install command", "copy", cmd)
        if cmd
        else _action("Open Doctor", "screen", "doctor")
    )
    return _row(
        id,
        name,
        "system",
        STATE_CONNECTED if connected else STATE_OFF,
        check.detail or (f"{id} present" if connected else absent),
        action,
        purpose=purpose,
        # System-level dependency — no in-app credential. Point at Doctor.
        settings_screen="doctor",
        **fields,
    )


def _git_connection() -> dict:
    """Git: optional — unlocks isolated worktrees, diffs, commits and PRs."""
    return _system_row(
        "git",
        "Git",
        doctor.check_git(),
        "Isolated worktrees per session, plus diff / commit / PR.",
        "git not installed (optional — sessions run in plain folders)",
    )


def _tailscale_connection() -> dict:
    """Tailscale: your other computers and your phone reach this one."""
    return _system_row(
        "tailscale",
        "Tailscale — your devices & phone",
        doctor.check_tailscale(),
        "Your other computers and your phone reach MindFlock here over your tailnet.",
        "tailscale not installed (optional)",
    )


def _safe_rows(build) -> list[dict]:
    """``build()`` as a list of rows; one broken source costs its own rows,
    never the whole screen."""
    try:
        out = build()
    except Exception as err:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).warning(
            "connections: %s failed: %s", build.__name__, err
        )
        return []
    return out if isinstance(out, list) else [out]


def build_connections() -> List[dict]:
    """Every outside thing MindFlock talks to, one row each, ordered by
    :data:`KINDS` (the screen's groups) and, inside a kind, as built."""
    conns: list[dict] = []
    for build in (
        _agent_connections,
        _github_connection,
        _ticketing_connections,
        _devices_connection,
        _phone_connection,
        _peers_connection,
        _mcp_connection,
        _db_connections,
        _ntfy_connection,
        _extension_connections,
        _git_connection,
        _tailscale_connection,
    ):
        conns.extend(_safe_rows(build))
    order = {k: i for i, k in enumerate(KINDS)}
    return sorted(conns, key=lambda c: order.get(c.get("kind"), len(KINDS)))


def summarize(conns: List[dict]) -> dict:
    """Counts per state, plus the old ``attention`` count (= needs attention)."""
    counts = {st: sum(1 for c in conns if c["status"] == st) for st in STATES}
    return {
        **counts,
        "attention": counts[STATE_NEEDS_ATTENTION],
        "total": len(conns),
    }


# Short cache so the frontend's while-open auto-refresh doesn't re-run the
# doctor subprocess probes (gh auth status, tailscale, agent) on every poll.
# Kept small so "did my fix land?" stays responsive; `refresh=1` bypasses it.
_CACHE_TTL_S = 4.0
_cache = {"at": 0.0, "payload": None}


def _build_payload() -> dict:
    conns = build_connections()
    return {"connections": conns, "summary": summarize(conns)}


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
        # The MCP row maps the last launch's tmux name back to its session.
        _ENGINE["engine"] = getattr(ctx, "engine", None)
        self._router = self._build_router()

    def _build_router(self) -> APIRouter:
        router = APIRouter(prefix="/api/connections")

        @router.get("")
        def get_connections(refresh: bool = False) -> JSONResponse:
            # sync def → FastAPI threadpool, so the bounded doctor subprocess
            # probes never block the event loop. Cached ~4s (refresh=1 bypasses).
            return JSONResponse(cached_payload(refresh=refresh))

        @router.post("/test/agent")
        def test_agent(body: Optional[dict] = None) -> JSONResponse:
            """An agent card's Test: is it installed, is it signed in — as
            one plain ``detail`` ("Signed in as you@example.com"), with the
            doctor's own ``cli`` / ``auth`` rows beside it. ``{"provider":
            name}`` picks one; the default otherwise."""
            name = str((body or {}).get("provider") or "").strip().lower()
            name = name or doctor._default_provider_name()
            cli = doctor.check_agent_cli_for(name)
            auth = doctor.check_agent_auth_for(name)
            ok = cli.status == "ok" and auth.status in ("ok", "info")
            return JSONResponse(
                {
                    "ok": ok,
                    "detail": agent_test_line(
                        name, cli.status, auth.status, auth.detail or ""
                    ),
                    "cli": cli.to_dict(),
                    "auth": auth.to_dict(),
                }
            )

        return router

    @property
    def router(self) -> APIRouter:
        return self._router

    # No frontend module: the connection list is rendered inline inside the
    # Settings → Connections screen (Connections.tsx), reading this addon's
    # ``GET /api/connections``. There is no separate modal / "manager" surface.
