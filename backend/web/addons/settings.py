"""Settings addon: the productization control panel.

Exposes the user settings store (:mod:`backend.config.settings`) and coding-CLI
provider management over ``/api/settings`` + ``/api/providers*`` so a new user
configures everything (API keys, binary paths, repo + ticketing config, custom
providers) from the web Settings dialog — no file editing, no matching the
original developer's machine.

Secrets are never returned in the clear: ``GET /api/settings`` reports a secret
as ``"•••set"`` when present or ``""`` when unset, and a ``POST`` that
sends an empty string for a secret leaves the stored value untouched.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil  # noqa: F401 — tests patch settings_addon.shutil.which (shared module)
from pathlib import Path
from typing import Optional, Tuple

from fastapi import APIRouter, Depends, Request, WebSocket
from fastapi.responses import JSONResponse

from backend import doctor, providers
from backend.config import settings as settings_store
from backend.providers import config as provider_config
from backend.web.core import auth as _web_auth
from backend.web.core import mobile_announce, restart, shared_link
from backend.web.core import settings_hooks as _settings_hooks

from .base import SECRET_MASK, Addon, AppContext, FrontendDescriptor

# Field paths (group, field) that hold secrets — masked on read, keep-on-empty
# on write.
# Flat (group, field) secrets. Ticketing tokens live inside a *list* of sources,
# so they're masked separately (see _mask_ticketing).
_SECRET_FIELDS = {
    ("github", "token"),
    ("general", "auth_token"),
    ("notifications", "ntfy_token"),
}
_MASK = SECRET_MASK  # the one sentinel, defined in addons/base.py


def _mask_ticketing(d: dict) -> None:
    """Mask the api_token of every ticketing source in-place."""
    tk = d.get("ticketing")
    if not isinstance(tk, dict):
        return
    for src in tk.get("sources", []) or []:
        if isinstance(src, dict):
            src["api_token"] = _MASK if src.get("api_token") else ""


def _mask_profile_dict(prof: dict) -> None:
    """Mask one auth profile's secrets in-place: ``api_key``, and every value
    in its raw ``env`` overrides — the env map is the documented escape hatch
    for carrying credentials the typed kinds don't know, so its VALUES are
    secrets even though its keys are not."""
    prof["api_key"] = _MASK if prof.get("api_key") else ""
    env = prof.get("env")
    if isinstance(env, dict):
        prof["env"] = {k: _MASK for k in env}


def _sessions_pinned_to(profile_ids: set) -> list:
    """Titles of live sessions whose stored pin names one of ``profile_ids``.

    Only an EXPLICIT pin counts: a session inheriting the app-wide default
    ("" pin) follows whatever the default becomes, which is the behaviour it
    asked for. Best-effort — the engine is not this addon's to depend on.
    """
    try:
        from backend.web import server as _srv

        return sorted(
            title
            for title, inst in (getattr(_srv.ENGINE, "instances", {}) or {}).items()
            if (getattr(inst, "ProfileId", "") or "") in profile_ids
        )
    except Exception:  # noqa: BLE001
        return []


def _mask_auth_profiles(d: dict) -> None:
    """Mask the secrets of every auth profile in-place (the profiles twin of
    :func:`_mask_ticketing` — secrets inside a list need their own walk)."""
    ap = d.get("auth_profiles")
    if not isinstance(ap, dict):
        return
    for prof in ap.get("profiles", []) or []:
        if isinstance(prof, dict):
            _mask_profile_dict(prof)


def _masked_view() -> dict:
    """The current settings as a grouped dict, with secrets masked.

    Secrets become ``"•••set"`` when present / ``""`` when unset, so the UI can
    show "a value is saved" without ever transmitting it.
    """
    d = settings_store.load_settings().to_dict()
    for group, fld in _SECRET_FIELDS:
        present = bool(d.get(group, {}).get(fld))
        d.setdefault(group, {})
        d[group][fld] = _MASK if present else ""
    _mask_ticketing(d)
    _mask_auth_profiles(d)
    return d


#: The only ``group.field``s ``POST /api/settings`` takes from a caller that
#: :func:`backend.web.core.auth.may_configure` refuses (an anonymous tailnet
#: caller of a gate-off, reachable device): bookkeeping about this machine
#: that runs nothing. Everything else — every synced field (settings sync
#: would spread it to the owner's other devices as this one's edit), the
#: gate/bind/remote-control switches, agent binaries and accounts, the IDE
#: and terminal commands, peer links — needs the owner. An allow-list, so a
#: new field is guarded until someone decides otherwise.
_OPEN_FIELDS = frozenset({"general.onboarded", "general.last_repo_path", "ui.surface"})


#: ``prefs`` fields ``POST /api/prefs`` takes only from a caller
#: ``auth.may_configure`` allows: what a key or a click sends to an agent.
_GUARDED_PREFS = frozenset({"keymap", "prompt_presets"})


def _guarded_paths(payload: dict) -> list:
    """The ``group.field``s ``payload`` would write that aren't in
    :data:`_OPEN_FIELDS` (a group that isn't a dict counts as itself)."""
    out = []
    for group, fields in (payload or {}).items():
        if not isinstance(fields, dict):
            out.append(str(group))
            continue
        out.extend(
            "%s.%s" % (group, f)
            for f in fields
            if "%s.%s" % (group, f) not in _OPEN_FIELDS
        )
    return out


# The "is this CLI installed" probes live in settings_hooks now (settings sync
# asks too, before adopting a default agent); these names stay because the
# routes below — and their tests — patch them here.
_installed_path = _settings_hooks.installed_path
_provider_installed = _settings_hooks.provider_installed


def _apply_post(payload: dict) -> list:
    """Apply a partial ``{group: {field: value}}`` update to the store.

    An empty string clears a normal field (falls through the resolution chain);
    for a *secret* an empty string / the mask sentinel means "keep the existing
    value" (so re-saving the form doesn't wipe a token the UI never received).

    The default agent provider is guarded: it may only be set to a CLI that is
    actually installed — you can never make an absent CLI the launch default.

    Returns the ``group.field`` paths it wrote (what :func:`_stamp_for_sync`
    may count as the person's own edit).
    """
    patches: dict = {}
    for group, fields in (payload or {}).items():
        if group == "ticketing":
            continue  # a list of sources — managed via the dedicated CRUD endpoints
        if not isinstance(fields, dict):
            continue
        if group == "auth_profiles":
            # The profiles LIST is managed via its dedicated CRUD endpoint;
            # the group's scalars (default_profile) stay settable here.
            fields = {k: v for k, v in fields.items() if k != "profiles"}
            dp = fields.get("default_profile")
            if isinstance(dp, str) and dp.strip():
                known = {
                    p.id for p in settings_store.load_settings().auth_profiles.profiles
                }
                if dp.strip() not in known:
                    raise ValueError(
                        "unknown account '%s' — add it under Settings → Accounts "
                        "before making it the default" % dp.strip()
                    )
        if group == "coding_cli":
            dp = fields.get("default_provider")
            if (
                isinstance(dp, str)
                and dp.strip()
                and not _provider_installed(dp.strip())
            ):
                raise ValueError(
                    "%s is not installed — install its CLI before making it the "
                    "default agent provider" % dp.strip()
                )
        clean: dict = {}
        for fld, val in fields.items():
            if (group, fld) in _SECRET_FIELDS and (val in ("", _MASK, None)):
                continue  # keep existing secret
            clean[fld] = val
        if clean:
            patches[group] = clean
    if patches:
        settings_store.update_settings(**patches)
    # A new GitHub token has to reach the code that uses it. github_auth caches
    # the resolved token for the life of the process, so without this every
    # consumer (PR review, issue handling, Make PR, the per-repo access test)
    # kept using the old one until a restart — which reads exactly like the
    # paste not having been saved.
    if "token" in (patches.get("github") or {}):
        try:
            from backend.ticket_ingestion import github_auth

            github_auth.invalidate()
        except Exception:  # noqa: BLE001 — a settings save must never fail on this
            pass
    return ["%s.%s" % (g, f) for g, fields in patches.items() for f in fields]


def _stamp_for_sync(*paths: str) -> None:
    """Stamp a just-saved shared field now, rather than at the next sync pass,
    so "last edit wins" orders by when the user actually changed it — and
    nudge the user's other devices to pull it now. ``paths`` are the bases
    this save wrote (``github.token``, ``ticketing.sources``,
    ``store:providers``): only what it clears under them is the person's own
    edit — anything else the scan finds cleared (settings.json replaced or
    deleted meanwhile) can still pause sync."""
    try:
        from backend.web.core import settings_sync

        settings_sync.local_change(paths)
    except Exception:  # noqa: BLE001 — a settings save must never fail on this
        pass


def _fleet_key_presented(request: Request) -> bool:
    """Whether the request's bearer is this fleet's key (member-to-member
    routes). Not the device token: those routes are for devices, and a
    browser never calls them. Never raises."""
    auth = request.headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        return False
    try:
        from backend.web.core import fleet as _fleet

        return bool(_fleet.key_valid(auth[7:].strip()))
    except Exception:  # noqa: BLE001 — no fleet = no member
        return False


def _fleet_401(error: str) -> dict:
    """A member route's 401 body: the error plus this device's fleet id and
    epoch (non-secret), so a calling device can tell "I am behind" from "it is
    behind" (backend.web.core.fleet.unauthorized_body). Never raises."""
    try:
        from backend.web.core import fleet as _fleet

        return {**_fleet.unauthorized_body(), "error": error}
    except Exception:  # noqa: BLE001 — no fleet store: just the error
        return {"error": error}


def _prefs_fields() -> Tuple[str, ...]:
    import dataclasses

    return tuple(f.name for f in dataclasses.fields(settings_store.PrefsSettings))


def _unreadable_response() -> JSONResponse:
    """409 for a read or save refused because settings.json exists but can't
    be read: a save would replace it with defaults (and settings sync would
    spread "everything deleted"), and defaults served as the stored prefs
    would overwrite what a browser still has."""
    return JSONResponse({"error": settings_store.UNREADABLE_HINT}, status_code=409)


def _prefs_view() -> dict:
    """The ``prefs`` group with EVERY field present (defaults filled). Raises
    ``SettingsUnreadable`` while settings.json can't be read — never defaults
    in place of a file that's only broken."""
    import dataclasses

    settings_store.invalidate()
    prefs = settings_store.load_settings_strict().prefs
    return {
        f.name: json.loads(json.dumps(getattr(prefs, f.name)))
        for f in dataclasses.fields(prefs)
    }


# --------------------------------------------------------------------------- #
# Provider management
# --------------------------------------------------------------------------- #
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _provider_view(p) -> dict:
    """Serialize a registered provider to a manage-view dict."""
    cfg = getattr(p, "cfg", None)
    is_builtin = p.name in providers.BUILTIN_NAMES
    view = {
        "name": p.name,
        "aliases": list(getattr(p, "program_aliases", ()) or ()),
        "source": "builtin" if is_builtin else "user",
        "editable": not is_builtin,
    }
    if cfg is not None:
        view.update(
            {
                "command": cfg.command,
                "binary_path": cfg.binary_path,
                "resume_flag": cfg.resume_flag,
                "skip_perms_flag": cfg.skip_perms_flag,
                "launch_args": list(cfg.launch_args),
                "trust_patterns": list(cfg.trust_patterns),
                "idle_pattern": cfg.idle_pattern,
            }
        )
    # Per-provider binary override currently in effect (settings/env), if any.
    view["binary_override"] = provider_config.binary_override(p.name)
    # Whether the Map can show this CLI's plan and send it "Go" — the New
    # Session dialog offers "Plan first" only when it can (the server drops
    # plan-first for the rest: the agent would wait for a Go nobody can send).
    try:
        view["plan_supported"] = bool(p.plan_supported())
    except Exception:  # noqa: BLE001 — a broken provider just has no plans
        view["plan_supported"] = False
    return view


def _default_provider_name() -> str:
    """The configured default provider (falls back to the registry default)."""
    try:
        name = settings_store.load_settings().coding_cli.default_provider
        if name:
            return name
    except Exception:  # noqa: BLE001 — settings are optional
        pass
    return providers.DEFAULT_PROVIDER


def _provider_status(p, default_name: str) -> dict:
    """Connection status for one provider: is its binary installed, does it look
    logged in, and how to install / log into it. Drives Settings → Providers."""
    name = p.name
    cfg = getattr(p, "cfg", None)
    binary = provider_config.resolve_provider_binary(name, cfg)
    path = _installed_path(binary)
    installed = bool(path)

    def _safe(call, fallback=""):
        try:
            return call() or fallback
        except Exception:  # noqa: BLE001 — one provider must not break the list
            return fallback

    evidence = _safe(p.auth_evidence)
    return {
        "name": name,
        "aliases": list(getattr(p, "program_aliases", ()) or ()),
        "binary": binary,
        "installed": installed,
        "path": path,
        # Auth probing is best-effort (many CLIs hide credentials): a miss means
        # "unknown", never "logged out" — the UI phrases it that way.
        "authenticated": bool(evidence),
        "auth_detail": evidence,
        "auth_known": bool(evidence),
        "login_command": _safe(p.login_command),
        "install_hint": _safe(p.install_hint),
        # Why a CUSTOM provider's CLI can't be found (built-ins ship an install
        # command instead, which is the more useful answer for them).
        "launch_hint": (
            ""
            if installed or name in providers.BUILTIN_NAMES
            else _binary_warning(binary, os.sep in binary)
        ),
        "is_default": name == default_name,
    }


def _provider_toml(body: dict) -> str:
    """Render a provider-management request body to a provider TOML document."""
    name = str(body.get("name", "")).strip()
    program = str(body.get("program", "") or name).strip()
    lines = [
        "[provider]",
        f"name = {json.dumps(name)}",
        f"program = {json.dumps(program)}",
    ]
    for key in ("command", "binary_path"):
        val = str(body.get(key, "") or "").strip()
        if val:
            lines.append(f"{key} = {json.dumps(val)}")
    launch = []
    for key in ("resume_flag", "skip_perms_flag"):
        val = str(body.get(key, "") or "").strip()
        if val:
            launch.append(f"{key} = {json.dumps(val)}")
    args = provider_config.validate_launch_args(body.get("launch_args", ()))
    if args:
        launch.append("args = [%s]" % ", ".join(json.dumps(a) for a in args))
    if "resume_fallback" in body:
        launch.append(f"resume_fallback = {str(bool(body['resume_fallback'])).lower()}")
    if launch:
        lines.append("")
        lines.append("[launch]")
        lines.extend(launch)
    classify = []
    patterns = body.get("trust_patterns")
    if isinstance(patterns, (list, tuple)) and patterns:
        rendered = ", ".join(json.dumps(str(p)) for p in patterns)
        classify.append(f"trust_patterns = [{rendered}]")
    idle = str(body.get("idle_pattern", "") or "").strip()
    if idle:
        classify.append(f"idle_pattern = {json.dumps(idle)}")
    ks = str(body.get("trust_keystroke", "") or "").strip()
    if ks:
        classify.append(f"trust_keystroke = {json.dumps(ks)}")
    if classify:
        lines.append("")
        lines.append("[classify]")
        lines.extend(classify)
    return "\n".join(lines) + "\n"


def _provider_body_error(body: dict) -> str:
    try:
        provider_config.validate_launch_args((body or {}).get("launch_args", ()))
    except ValueError as err:
        return str(err)
    return ""


def _provider_launch_warning(body: dict) -> str:
    """Why a saved provider won't be able to start, or ``""``.

    A provider whose executable can't be resolved is accepted (the CLI may not be
    installed yet) but is dead on arrival, and the failure surfaces far away: the
    pane just prints "command not found" and dies. The common cause is a SHELL
    ALIAS — sessions launch through ``sh -c``, which reads no shell rc file and
    has no aliases or functions, so a name that works in your terminal can be
    invisible here. Saying so at save time, next to the Binary path field that
    fixes it, is the difference between a one-field correction and a mystery.
    """
    body = body or {}
    name = str(body.get("name", "") or "").strip()
    explicit = str(body.get("binary_path", "") or "").strip()
    return _binary_warning(
        explicit or str(body.get("command", "") or "").strip() or name, bool(explicit)
    )


def _binary_warning(binary: str, explicit: bool) -> str:
    """The unresolvable-executable explanation for ``binary``, or ``""`` when it
    resolves. ``explicit`` marks a binary that came from the binary-path field
    (a wrong path, not a missing alias). See :func:`_provider_launch_warning`."""
    binary = shlex.split(binary)[0] if binary.strip() else ""
    if not binary or _installed_path(binary):
        return ""
    if explicit:
        return f"{binary!r} is not an executable file — check the binary path."
    return (
        f"{binary!r} was not found on PATH. Sessions start through a "
        "non-interactive shell, which has no aliases or shell functions — if "
        f"{binary!r} is one of those, put the real executable it points at "
        f"(what `type {binary}` prints) in the binary-path field."
    )


# --------------------------------------------------------------------------- #
# Account-attach validation ("Test" buttons — C5). Network/CLI probes live in
# module-level helpers so tests can monkeypatch them; endpoints always answer
# HTTP 200 with an ``ok`` flag (a failed probe is a *result*, not a 4xx/5xx)
# and never echo a token back.
# --------------------------------------------------------------------------- #
_SHORTCUT_MEMBER_URL = "https://api.app.shortcut.com/api/v3/member"


async def _fetch_shortcut_member(token: str) -> Tuple[Optional[dict], str]:
    """GET the Shortcut ``/member`` endpoint with ``token``.

    Returns ``(member_dict, "")`` on success or ``(None, error)`` on any
    failure (bad token, network trouble). Never raises.
    """
    import aiohttp

    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                _SHORTCUT_MEMBER_URL, headers={"Shortcut-Token": token}
            ) as resp:
                if resp.status == 200:
                    return await resp.json(), ""
                if resp.status in (401, 403):
                    return None, "Shortcut rejected the token (HTTP %d)" % resp.status
                return None, f"Shortcut API returned HTTP {resp.status}"
    except asyncio.TimeoutError:
        return None, "Shortcut API timed out"
    except aiohttp.ClientError as err:
        return None, f"network error reaching Shortcut: {err}"


def _stored_shortcut_token() -> str:
    """The Shortcut token from the resolution chain (never echoed to clients)."""
    from backend.config.secrets import resolve_secret_sync

    def _from_ticketing(s) -> str:
        for src in s.ticketing.sources:
            if src.provider == "shortcut" and src.api_token:
                return src.api_token
        return ""

    return resolve_secret_sync(
        settings_getter=_from_ticketing,
        env_vars=("SHORTCUT_API_TOKEN",),
    )


def _github_token_source() -> str:
    """Where a GitHub token would come from, mirroring
    :mod:`backend.ticket_ingestion.github_auth` (settings → env → gh CLI)
    without ever returning the token itself."""
    if settings_store.load_settings().github.token:
        return "settings"
    for var in ("GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(var):
            return f"env:{var}"
    return ""


def _repo_test_config():
    """A minimal ``GithubConfig`` carrying only the stored token, for the
    per-repo access test.

    The shared resolver wants a config object to read ``[github].token`` from
    and then walks env → ``gh auth token`` on its own. Building a bare one here
    (rather than loading the whole pipeline config) means the test works on a
    machine that has never configured ticket ingestion — which is exactly the
    machine someone is testing a repo on.
    """
    from backend.ticket_ingestion.config import GithubConfig

    return GithubConfig(
        base_branch="",
        min_age_minutes=15,
        poll_interval_seconds=60,
        enabled=True,
        skip_authors=[],
        token=(settings_store.load_settings().github.token or "").strip(),
    )


def _gh_cli_status() -> Tuple[bool, bool, str]:
    """``(installed, authenticated, detail)`` for the local ``gh`` CLI."""
    check = doctor.check_gh()
    # Absent gh is reported as ``info`` (optional dep) or legacy ``fail``; both
    # mean "not installed". Present-but-unauthenticated is ``warn``; ``ok`` is
    # installed + signed in.
    installed = check.status in ("ok", "warn")
    authenticated = check.status == "ok"
    return installed, authenticated, check.detail


async def _stored_secret_refused(request, body: Optional[dict], field: str) -> bool:
    """Whether a Test / list route must refuse because it would use a STORED
    secret (``field`` blank or masked in ``body``) for a caller
    ``auth.may_configure`` refuses. The body may also name the endpoint
    (``base_url``), so an anonymous tailnet caller of a gate-off device could
    otherwise have this device send its saved token to a server of theirs.
    Inline credentials stay testable by anyone. Never raises (fails closed)."""
    try:
        v = str((body or {}).get(field, "") or "").strip()
        if v and v != _MASK:
            return False
        return not await _web_auth.may_configure(request.scope)
    except Exception:  # noqa: BLE001
        return True


def _source_cfg_from_body(body: dict):
    """Build a :class:`TicketProviderConfig` from a request body, filling any
    missing/masked field from the stored source matched by ``id`` (else the
    primary source). Lets the UI test or list-states for a saved source without
    re-sending its secret, or use inline creds for a brand-new source. Shared by
    the ``/test/ticketing`` and ``/ticketing/states`` endpoints."""
    from backend.ticket_ingestion.config import TicketProviderConfig

    body = body or {}
    all_sources = settings_store.load_settings().ticketing.sources
    src_id = str(body.get("id", "") or "")
    stored = next((s for s in all_sources if s.id == src_id), None)
    if stored is None:
        stored = all_sources[0] if all_sources else None

    def sp(attr: str) -> str:
        return getattr(stored, attr, "") if stored else ""

    provider = str(body.get("provider") or sp("provider") or "shortcut").strip().lower()

    def pick(key: str, fallback: str, secret: bool = False) -> str:
        v = str(body.get(key, "") or "").strip()
        if secret and v in ("", _MASK):
            return fallback
        return v or fallback

    return TicketProviderConfig(
        provider=provider,
        api_token=pick("api_token", sp("api_token"), secret=True),
        base_url=pick("base_url", sp("base_url")),
        email=pick("email", sp("email")),
        member_id=pick("member_id", sp("member_id")),
        project=pick("project", sp("project")),
        workflow_state=pick("workflow_state", sp("workflow_state")),
        # Round-tripped so the states endpoint answers for the source as the
        # card currently has it — the start-state picker reads the same list.
        start_state=pick("start_state", sp("start_state")),
        # Carried so Test exercises the query the pipeline will actually run:
        # an any-assignee source searches by state, not by member id.
        assignee_scope=pick("assignee_scope", sp("assignee_scope")),
        # Carried for the same reason: a label can bound an any-assignee search.
        ingest_labels=pick("ingest_labels", sp("ingest_labels")),
        # Carried so a provider that derives its scope from the repo (GitHub
        # Issues auto-detects owner/repo from repo_url) can Test with nothing but
        # a repo filled in, and so the agent shows up in the round-tripped config.
        repo_url=pick("repo_url", sp("repo_url")),
        agent=pick("agent", sp("agent")),
    )


class SettingsAddon(Addon):
    id = "settings"
    label = "Settings"

    def __init__(self, ctx: Optional[AppContext] = None) -> None:
        super().__init__(ctx)
        self._router = self._build_router()

    # --- routes ----------------------------------------------------------- #
    def _providers_dir(self) -> Path:
        return provider_config._providers_dir()

    def _build_router(self) -> APIRouter:
        router = APIRouter(prefix="/api")

        @router.get("/settings")
        def get_settings() -> JSONResponse:
            return JSONResponse({"settings": _masked_view()})

        @router.get("/settings/auth-token")
        def get_auth_token(request: Request) -> JSONResponse:
            """This device's access token in the clear — what another MindFlock
            device enters to remote-control this one, and what the browser
            sign-in page asks for. Generates + persists a token on first use so
            the Security screen always has one to show.

            Only to a caller that already holds it (this device's OWN token as
            cookie or bearer), to this machine itself (unproxied, not relayed),
            or — gate off — to any direct caller of a server nothing beyond
            this machine can reach. Not to an anonymous tailnet caller of a
            gate-off, reachable one: holding the token would make it
            privileged (``auth.may_configure``). NOT to a caller that got
            past the gate with the fleet key: a member (or a phone signed in
            with the devices' key) must not be able to harvest every device's
            own token, which would outlive its removal from the group."""
            from backend.web.core import auth as web_auth

            if web_auth.may_see_own_token(request.scope, open_gate=True):
                return JSONResponse(
                    {
                        "token": web_auth.get_token(),
                        "auth_enabled": web_auth.auth_enabled(),
                    }
                )
            return JSONResponse(
                {
                    "token": None,
                    "auth_enabled": web_auth.auth_enabled(),
                    "reason": "signed in with your devices' key",
                }
            )

        @router.get("/settings/tailnet-trust")
        def get_tailnet_trust() -> JSONResponse:
            """Settings → Security's "Trusted Tailscale accounts": the logins
            that own untagged devices on this tailnet (the choices), this
            node's own, and whether shared-link requests can be vouched for
            here (:func:`backend.web.core.tailnet_trust.status`)."""
            from backend.web.core import tailnet_trust

            return JSONResponse(tailnet_trust.status())

        @router.get("/settings/sync")
        def get_settings_sync() -> JSONResponse:
            """Settings → Devices → Settings sync: on/off, where this device
            started from, each fleet device's last pass, what's pinned here,
            what's held back (an agent CLI not installed) and the pinnable
            choices."""
            from backend.web.core import settings_sync

            return JSONResponse(settings_sync.status())

        @router.post("/settings/sync")
        async def post_settings_sync(payload: dict, request: Request) -> JSONResponse:
            """``{enabled: true, from?: <device key>}`` turns sync on — from
            THIS device (``from`` empty) or by first adopting one of your
            devices' shareable settings; ``{enabled: false}`` turns it off.
            This device's own choice: refused for a request relayed by
            another MindFlock, or from an anonymous caller of a gate-off,
            reachable device (``auth.may_configure``). 409 when this device
            isn't in a fleet yet."""
            from backend.web.core import auth as web_auth
            from backend.web.core import settings_sync

            if not await web_auth.may_configure(request.scope):
                return JSONResponse(
                    {"error": "settings sync can only be changed on this device"},
                    status_code=403,
                )
            payload = payload or {}
            if not payload.get("enabled"):
                await asyncio.to_thread(settings_sync.disable)
                return JSONResponse(await asyncio.to_thread(settings_sync.status))
            try:
                result = await settings_sync.enable(str(payload.get("from") or ""))
            except LookupError as err:
                return JSONResponse({"error": str(err)}, status_code=409)
            status = await asyncio.to_thread(settings_sync.status)
            return JSONResponse({**status, **result})

        @router.post("/settings/sync/now")
        async def post_settings_sync_now(request: Request) -> JSONResponse:
            """Run one sync pass now (the "Sync now" button) and return the
            status plus what was adopted. This device's own button: refused
            for a relayed or anonymous request (``auth.may_configure``)."""
            from backend.web.core import auth as web_auth
            from backend.web.core import settings_sync

            if not await web_auth.may_configure(request.scope):
                return JSONResponse(
                    {"error": "settings sync can only be run on this device"},
                    status_code=403,
                )
            adopted = await settings_sync.sync_once()
            status = await asyncio.to_thread(settings_sync.status)
            return JSONResponse({**status, "adopted": adopted})

        @router.post("/settings/sync/resume")
        async def post_settings_sync_resume(
            payload: dict, request: Request
        ) -> JSONResponse:
            """``{keep: "theirs"|"mine"}`` — sync paused because this device's
            settings look reset: take the other devices' values back, or
            spread this device's. Returns the status plus what was adopted.
            This device's own choice: refused for a relayed or anonymous
            request (``auth.may_configure``)."""
            from backend.web.core import auth as web_auth
            from backend.web.core import settings_sync

            if not await web_auth.may_configure(request.scope):
                return JSONResponse(
                    {"error": "settings sync can only be changed on this device"},
                    status_code=403,
                )
            try:
                result = await settings_sync.resume(
                    str((payload or {}).get("keep") or "")
                )
            except ValueError as err:
                return JSONResponse({"error": str(err)}, status_code=400)
            except LookupError as err:
                return JSONResponse({"error": str(err)}, status_code=409)
            status = await asyncio.to_thread(settings_sync.status)
            return JSONResponse({**status, **result})

        @router.post("/settings/sync/pin")
        async def post_settings_sync_pin(
            payload: dict, request: Request
        ) -> JSONResponse:
            """``{path, pinned}`` — keep ``path`` (``group.field`` or
            ``store:<name>``) different on this device, or stop. Un-pinning
            lets the fleet's value win on the next pass. Refused for a relayed
            or anonymous request (``auth.may_configure``)."""
            from backend.web.core import auth as web_auth
            from backend.web.core import settings_sync

            if not await web_auth.may_configure(request.scope):
                return JSONResponse(
                    {"error": "settings sync can only be changed on this device"},
                    status_code=403,
                )
            payload = payload or {}
            try:
                await asyncio.to_thread(
                    settings_sync.set_pinned,
                    str(payload.get("path") or ""),
                    bool(payload.get("pinned")),
                )
            except ValueError as err:
                return JSONResponse({"error": str(err)}, status_code=400)
            return JSONResponse(await asyncio.to_thread(settings_sync.status))

        @router.post("/settings/sync/nudge")
        async def post_settings_sync_nudge(request: Request) -> JSONResponse:
            """Another of your devices changed a shared setting: pull from it
            (and the rest) in a second rather than at the next pass. Only a
            fleet member may ask — the bearer must be the fleet key."""
            from backend.web.core import settings_sync

            if not _fleet_key_presented(request):
                return JSONResponse(
                    _fleet_401("only one of your devices can ask this"),
                    status_code=401,
                )
            return JSONResponse({"ok": True, "scheduled": settings_sync.nudged()})

        @router.get("/settings/sync/export")
        def get_settings_sync_export(request: Request) -> JSONResponse:
            """This device's shareable settings + their stamps, for the user's
            other devices. 401 unless the request itself carries a credential
            (the fleet key, or this device's token — a bearer header or the
            sign-in cookie): with the gate off any tailnet caller reaches the
            route, and the export holds secrets (backend.web.core.settings_sync)."""
            from backend.web.core import auth as web_auth
            from backend.web.core import settings_sync

            if not web_auth.presented_token(request.scope):
                return JSONResponse(
                    _fleet_401("present this device's token or your devices' key"),
                    status_code=401,
                )
            try:
                return JSONResponse(settings_sync.export())
            except settings_store.SettingsUnreadable as err:
                # Never an empty export: the other devices would read it as
                # "everything was deleted here" (or, paused, start from a
                # reset file).
                why = (
                    settings_sync.PAUSED
                    if str(err) == settings_sync.PAUSED
                    else settings_sync.UNREADABLE
                )
                return JSONResponse({"error": why}, status_code=503)

        # --- UI preferences that follow the person ------------------------ #
        @router.get("/prefs")
        def get_prefs() -> JSONResponse:
            """Every ``prefs`` field (unset ones at their defaults) — the
            browser's localStorage is a cache of this, so a keymap or prompt
            preset set on one device shows up on the others. 409 while
            settings.json can't be read (the browser keeps its own copy)."""
            try:
                return JSONResponse(_prefs_view())
            except settings_store.SettingsUnreadable:
                return _unreadable_response()

        @router.post("/prefs")
        def post_prefs(
            payload: dict, allowed: bool = Depends(_web_auth.configure_allowed)
        ) -> JSONResponse:
            """``{field: value, …}`` (a partial update; ``null`` clears a
            field). Unknown fields are ignored. Stamped for sync at once.
            409 while settings.json can't be read — nothing is saved over it.
            403 for :data:`_GUARDED_PREFS` from a caller
            ``auth.may_configure`` refuses: a preset is text sent to an agent
            in one click, on every device sync reaches."""
            known = _prefs_fields()
            clean = {k: v for k, v in (payload or {}).items() if k in known}
            if not allowed and _GUARDED_PREFS.intersection(clean):
                return _web_auth.configure_refused()
            if clean:
                try:
                    settings_store.update_settings(prefs=clean)
                except settings_store.SettingsUnreadable:
                    return _unreadable_response()
                except Exception as err:  # noqa: BLE001
                    return JSONResponse({"error": str(err)}, status_code=400)
                _stamp_for_sync(*("prefs." + k for k in clean))
                # Every other browser on this server (the desktop app beside a
                # tab) keeps its own localStorage copy: tell them to re-pull,
                # or the next whole-list save from a stale one drops this
                # write (and sync spreads the drop as a delete).
                try:
                    from backend.web.core import settings_hooks

                    settings_hooks.after_settings_change(
                        ["prefs.%s" % k for k in clean], source=""
                    )
                except Exception:  # noqa: BLE001 — a save never fails on this
                    pass
            try:
                return JSONResponse(_prefs_view())
            except settings_store.SettingsUnreadable:
                return _unreadable_response()

        @router.post("/settings/auth-token/rotate")
        async def rotate_auth_token(request: Request) -> JSONResponse:
            """Invalidate the current access token and mint a fresh one
            (compromise recovery). Every signed-in browser cookie, ``/m`` QR
            code, and paired device's stored token stops working immediately.
            In a group of your devices the devices' shared key is replaced
            too, and every member's own access token
            (:func:`backend.web.core.fleet.rotate_key`) — the phone QR
            carries both, so a lost phone is signed out of every member
            that was reached; ``rekeyed`` / ``missed`` say which members took
            the new key now and which get it when they're back, ``rotated``
            / ``rotate_failed`` whose own token was replaced and whose must
            be rotated there by hand (the note names them). Phones scan the
            QR again.

            The new token goes back (and THIS caller's cookie is re-issued so
            it stays signed in) only to a caller that may see it
            (:func:`backend.web.core.auth.may_see_own_token`): a caller signed
            in with the devices' key gets ``token: null``. 409 when the token
            is pinned by ``MINDFLOCK_AUTH_TOKEN`` (the env var always wins, so
            rotating the setting would be a lie). 403 for a caller
            ``auth.may_configure`` refuses: a rotation signs every phone and
            paired device out, on every member it reaches."""
            from backend.web.core import auth as web_auth
            from backend.web.core import fleet as _fleet

            if not await web_auth.may_configure(request.scope):
                return web_auth.configure_refused()
            mine = web_auth.may_see_own_token(request.scope)
            try:
                token = web_auth.rotate_token()
            except RuntimeError as err:
                return JSONResponse({"error": str(err)}, status_code=409)
            except settings_store.SettingsUnreadable:
                return _unreadable_response()  # the old token still stands
            except Exception as err:  # noqa: BLE001 — settings store failure
                return JSONResponse(
                    {"error": "could not persist the new token: %s" % err},
                    status_code=500,
                )
            out = {
                "token": token if mine else None,
                "auth_enabled": web_auth.auth_enabled(),
                "rekeyed": [],
                "missed": [],
                "rotated": [],
                "rotate_failed": [],
                "in_fleet": False,
                "note": "Phones signed in before need to scan the QR again.",
            }
            try:
                if _fleet.in_fleet():
                    out.update(await _fleet.rotate_key(), in_fleet=True)
                    out["note"] = (
                        "Phones signed in before need to scan the QR again — "
                        "on every one of your devices: their shared key and "
                        "their own tokens were replaced too."
                    )
                    if out["rotate_failed"]:
                        members = _fleet.live_members()
                        names = [
                            (members.get(k) or {}).get("host") or k
                            for k in out["rotate_failed"]
                        ]
                        out["note"] += (
                            " Not reached: %s — rotate the token on %s too "
                            "(Settings → Security there), or a lost phone "
                            "stays signed in on %s."
                            % (
                                ", ".join(names),
                                "it" if len(names) == 1 else "each",
                                "it" if len(names) == 1 else "them",
                            )
                        )
            except Exception as err:  # noqa: BLE001 — the token rotated anyway
                out["fleet_error"] = "couldn't replace your devices' key: %s" % (
                    err or "error"
                )
            resp = JSONResponse(out)
            return web_auth.set_auth_cookies(resp) if mine else resp

        @router.post("/settings")
        async def post_settings(payload: dict, request: Request) -> JSONResponse:
            """A partial ``{group: {field: value}}`` save (:func:`_apply_post`).

            Who may: anything but :data:`_OPEN_FIELDS` needs
            :func:`backend.web.core.auth.may_configure` — never another
            MindFlock relaying (peer links, the gate, launch flags are this
            device's own business), and never an anonymous tailnet caller of
            a gate-off device: a synced field it wrote would be stamped as
            this device's edit and spread to every device holding the fleet
            key (``coding_cli.default_launch_args`` prefixes every new
            session there). The rest runs in a worker thread (it may call
            ``tailscale serve``)."""
            from starlette.concurrency import run_in_threadpool

            from backend.web.core import auth as web_auth

            payload = payload or {}
            if _guarded_paths(payload) and not await web_auth.may_configure(
                request.scope
            ):
                return web_auth.configure_refused()
            return await run_in_threadpool(_save_settings, payload)

        def _save_settings(payload: dict) -> JSONResponse:
            # The automated-PR-review / issue-handling toggles (github.enabled,
            # github.issues_enabled) are only read when the pipeline process
            # boots, so snapshot them before applying and, on a real flip, emit
            # an event the ingestion addon uses to restart a live pipeline.
            # enabled is Optional[bool]; the UI treats unset/None as "on" for
            # PR review but as "off" for issue handling (opt-in), so compare
            # the normalized on/off states (not raw values).
            # github.automation_device (synced: which of your devices runs
            # them) gates both halves the same way, so moving it reconciles too.
            def _toggle_states() -> tuple[bool, bool, bool]:
                from backend.web.core import settings_hooks

                gh = settings_store.load_settings().github
                return (
                    gh.enabled is not False,
                    gh.issues_enabled is True,
                    settings_hooks.automation_here(),
                )

            gh_in = payload.get("github")
            watch_toggle = isinstance(gh_in, dict) and (
                "enabled" in gh_in
                or "issues_enabled" in gh_in
                or "automation_device" in gh_in
            )
            before = _toggle_states() if watch_toggle else None

            # Settings → Mobile's tailscale-mode switch (general.serve_mode).
            # Turning it ON is the moment a phone URL starts to exist, so it is
            # also the moment worth pushing that URL to the phone.
            def _serve_mode() -> str:
                return (
                    (settings_store.load_settings().general.serve_mode or "")
                    .strip()
                    .lower()
                )

            gen_in = payload.get("general")
            watch_serve = isinstance(gen_in, dict) and "serve_mode" in gen_in
            serve_before = _serve_mode() if watch_serve else None
            # Settings → Mobile's shared phone link (general.shared_link): the
            # name must be a valid Tailscale Service name, and a change takes
            # effect right here — `tailscale serve` needs no restart.
            watch_shared = isinstance(gen_in, dict) and "shared_link" in gen_in
            if watch_shared:
                raw = gen_in.get("shared_link")
                name = shared_link.normalize(raw)
                if str(raw or "").strip() and not name:
                    return JSONResponse(
                        {
                            "error": "a shared link name is one DNS label — "
                            "lowercase letters, digits and dashes"
                        },
                        status_code=400,
                    )
                payload = {**payload, "general": {**gen_in, "shared_link": name}}
            shared_before = shared_link.configured_name() if watch_shared else None
            try:
                wrote = _apply_post(payload)
            except settings_store.SettingsUnreadable:
                return _unreadable_response()
            except Exception as err:  # noqa: BLE001
                return JSONResponse({"error": str(err)}, status_code=400)
            _stamp_for_sync(*wrote)
            if watch_toggle and self.ctx is not None:
                if _toggle_states() != before:
                    try:
                        self.ctx.emit("settings.github_toggled")
                    except Exception:  # noqa: BLE001 — never let the bus break a save
                        pass

            view = {"settings": _masked_view()}
            # A changed name applies; the same one saved again reconciles —
            # re-serving when the live serve config no longer matches (what
            # this process remembers can be stale: the config may have been
            # cleared behind its back) or the host still isn't approved.
            if watch_shared and (shared_before or shared_link.configured_name()):
                from backend.web.core import mobile_access

                port = mobile_access._server()._server_port()
                url_before = shared_link.advertised_url()
                if shared_link.configured_name() != shared_before:
                    state = shared_link.apply(port)
                else:
                    state = shared_link.reconcile(port, nudge=True)
                view["shared_link"] = state
                if shared_link.advertised_url() != url_before:
                    # Notification taps point at the phone URL — it changed.
                    mobile_announce.refresh_url()
                    if state.get("advertised"):
                        # A new phone URL exists — the moment to push it.
                        mobile_announce.announce_soon(mobile_announce.REASON_SHARED)
            if (
                watch_serve
                and serve_before != "tailscale"
                and _serve_mode() == "tailscale"
            ):
                # A hand-flipped toggle is a fresh intent — it gets the full
                # retry budget back even if an earlier one was spent giving up.
                restart.reset_tailscale_attempts()
                if restart.auto_restart_for_tailscale(delay=0.5):
                    # Which bind uvicorn holds is fixed at boot, so the toggle
                    # only means something after a restart — take it here rather
                    # than leaving the user a button to press. `restarting` tells
                    # the client to wait for the server to come back instead of
                    # reporting the dropped connection as a failure.
                    view["restarting"] = True
                else:
                    # Already listening on the tailnet (or we've given up
                    # restarting): the URL is as live as it is going to get, so
                    # push it now. When a restart IS coming, the fresh process
                    # announces instead — it can say the URL works.
                    mobile_announce.announce_soon(mobile_announce.REASON_MOBILE)
            return JSONResponse(view)

        # --- account-attach validation (C5): "Test" buttons ----------------- #
        @router.post("/settings/test/shortcut")
        async def test_shortcut(body: Optional[dict] = None) -> JSONResponse:
            """Validate a Shortcut token (request-supplied or stored) against
            ``/api/v3/member``. Returns the member id so the UI can auto-fill
            ``shortcut.member_id`` — no more hunting for a raw UUID."""
            body = body or {}
            token = str(body.get("api_token", "") or "").strip()
            if token in ("", _MASK):
                token = _stored_shortcut_token()
            if not token:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": "no Shortcut token configured — paste one first",
                    }
                )
            member, error = await _fetch_shortcut_member(token)
            if member is None:
                return JSONResponse({"ok": False, "error": error})
            profile = member.get("profile") or {}
            return JSONResponse(
                {
                    "ok": True,
                    "member_id": str(member.get("id", "")),
                    "name": str(member.get("name") or profile.get("name") or ""),
                    "mention_name": str(
                        member.get("mention_name") or profile.get("mention_name") or ""
                    ),
                }
            )

        @router.post("/settings/test/local-model")
        def test_local_model(body: Optional[dict] = None) -> JSONResponse:
            """Probe a local model server and list what it serves.

            Uses the request-supplied runtime/base_url when present (so the user
            can Test before saving) and otherwise the stored config. The model
            list is what makes this more than a ping: it turns "type the exact
            tag your server uses" into picking from a dropdown.
            """
            from backend.providers import local_models

            body = body or {}
            stored = local_models.load_config()
            runtime = (
                str(body.get("runtime", "") or "").strip().lower() or stored.runtime
            )
            cfg = local_models.LocalModelConfig(
                # Probe on demand regardless of the saved on/off state — the
                # whole point is to check the server BEFORE switching it on.
                enabled=True,
                runtime=runtime if runtime in local_models.RUNTIMES else "ollama",
                base_url=str(body.get("base_url", "") or "").strip() or stored.base_url,
                model=str(body.get("model", "") or "").strip() or stored.model,
            )
            result = local_models.probe(cfg)
            return JSONResponse(
                {
                    "ok": bool(result.get("running")),
                    "runtime": cfg.runtime,
                    "base_url": result.get("base_url", ""),
                    "models": result.get("models", []),
                    "error": result.get("error", ""),
                    # Which of the installed CLIs can actually be pointed at it,
                    # so the screen can say so instead of failing at launch.
                    "supported_agents": [
                        p.name
                        for p in providers.all_providers()
                        if local_models.supported(p.name)
                    ],
                    "default_base_urls": {
                        r: local_models.default_base_url(r)
                        for r in local_models.RUNTIMES
                    },
                }
            )

        @router.get("/settings/providers/ticketing")
        def ticketing_providers() -> JSONResponse:
            """The provider catalog (id/label/blurb + credential fields) the
            Ticket Ingestion settings screen renders.

            Each entry carries the provider's ``slug_prefix`` (``sc`` for
            Shortcut): the UI seeds a new source's ``id`` from it, and that id
            IS the branch prefix, so seeding from the provider name branched
            Shortcut tickets as ``feature/shortcut-<id>/`` instead of
            ``feature/sc-<id>/``."""
            from backend.ticket_ingestion.providers import (
                PROVIDER_META,
                provider_slug_prefix,
            )

            return JSONResponse(
                {
                    "providers": [
                        {**m, "slug_prefix": provider_slug_prefix(m["id"])}
                        for m in PROVIDER_META
                    ]
                }
            )

        @router.post("/settings/test/ticketing")
        async def test_ticketing(
            request: Request, body: Optional[dict] = None
        ) -> JSONResponse:
            """Validate the active (or request-supplied) ticketing provider's
            credentials via its own ``test_connection``. Returns the resolved
            member id so the UI can auto-fill it. Never echoes a token. With
            the STORED token, owner only (:func:`_stored_secret_refused`)."""
            from backend.ticket_ingestion.providers import (
                ProviderError,
                get_provider,
            )

            if await _stored_secret_refused(request, body, "api_token"):
                return _web_auth.configure_refused()
            cfg = _source_cfg_from_body(body or {})
            try:
                prov = get_provider(cfg)
            except ProviderError as err:
                return JSONResponse({"ok": False, "error": str(err)})
            identity, error = await prov.test_connection()
            if identity is None:
                return JSONResponse({"ok": False, "error": error})
            return JSONResponse(
                {
                    "ok": True,
                    "member_id": str(identity.get("member_id", "") or ""),
                    "name": str(identity.get("name") or ""),
                }
            )

        @router.post("/settings/ticketing/states")
        async def ticketing_states(
            request: Request, body: Optional[dict] = None
        ) -> JSONResponse:
            """The workflow states/statuses a ticket can be in, for the "ingest
            only when the ticket is in state X" picker. Uses the request-supplied
            or stored credentials (never echoes a token; the stored ones owner
            only — :func:`_stored_secret_refused`). Providers without
            workflow states return ``{"states": []}``."""
            from backend.ticket_ingestion.providers import (
                ProviderError,
                get_provider,
            )

            if await _stored_secret_refused(request, body, "api_token"):
                return _web_auth.configure_refused()
            cfg = _source_cfg_from_body(body or {})
            try:
                prov = get_provider(cfg)
                states = await prov.list_states()
            except ProviderError as err:
                return JSONResponse({"ok": False, "error": str(err), "states": []})
            except Exception as err:  # noqa: BLE001
                return JSONResponse(
                    {"ok": False, "error": f"{type(err).__name__}: {err}", "states": []}
                )
            return JSONResponse({"ok": True, "states": states})

        # --- ticketing sources CRUD (multiple providers / same-provider dupes) --
        def _masked_sources() -> list:
            out = []
            for s in settings_store.load_settings().ticketing.sources:
                d = s.to_dict()
                d["api_token"] = _MASK if d.get("api_token") else ""
                out.append(d)
            return out

        @router.get("/settings/ticketing/sources")
        def get_ticketing_sources() -> JSONResponse:
            return JSONResponse({"sources": _masked_sources()})

        @router.put("/settings/ticketing/sources")
        def put_ticketing_sources(
            body: dict, allowed: bool = Depends(_web_auth.configure_allowed)
        ) -> JSONResponse:
            """Replace the whole sources list. A source whose ``api_token`` is
            empty or the mask sentinel keeps its previously-stored token (matched
            by ``id``), so re-saving the form never wipes a secret the UI never
            received. Blank ``provider`` entries are dropped. Owner only
            (``auth.may_configure``): a source decides which tickets start
            agent sessions, and settings sync spreads it."""
            if not allowed:
                return _web_auth.configure_refused()
            incoming = (body or {}).get("sources")
            if not isinstance(incoming, list):
                return JSONResponse(
                    {"error": 'expected {"sources": [...]}'}, status_code=400
                )
            prev = {
                s.id: s.api_token
                for s in settings_store.load_settings().ticketing.sources
            }
            clean: list = []
            for raw in incoming:
                if not isinstance(raw, dict) or not raw.get("provider"):
                    continue
                s = dict(raw)
                tok = str(s.get("api_token", "") or "").strip()
                if tok in ("", _MASK):
                    s["api_token"] = prev.get(str(s.get("id", "")), "")
                clean.append(s)
            try:
                settings_store.set_ticketing_sources(clean)
            except settings_store.SettingsUnreadable:
                return _unreadable_response()
            except Exception as err:  # noqa: BLE001
                return JSONResponse({"error": str(err)}, status_code=400)
            _stamp_for_sync("ticketing.sources")
            return JSONResponse({"sources": _masked_sources()})

        # --- auth profiles CRUD (multiple identities per CLI) ---------------
        def _masked_profiles() -> list:
            from backend.providers import auth_profiles as ap

            out = []
            for p in settings_store.load_settings().auth_profiles.profiles:
                d = p.to_dict()
                _mask_profile_dict(d)
                # Read-only enrichment the Accounts screen renders: where an
                # account profile's isolated login lives, the command that logs
                # its CLI in there, and which CLIs the profile can route — what
                # the New dialog uses to steer the Agent picker so a
                # no-route combination is caught at selection time.
                try:
                    cfg = ap.get_profile(p.id)
                    if cfg is not None:
                        d["supported_agents"] = ap.supported_agents(cfg)
                        if p.kind == "account":
                            d["resolved_config_dir"] = ap.account_dir(cfg)
                            d["login_command"] = ap.login_command(cfg)
                except Exception:  # noqa: BLE001 — enrichment only
                    pass
                out.append(d)
            return out

        def _auth_profiles_view() -> dict:
            s = settings_store.load_settings().auth_profiles
            view = {
                "profiles": _masked_profiles(),
                "default_profile": s.default_profile,
                "kinds": list(settings_store.AUTH_PROFILE_KINDS),
            }
            # $MINDFLOCK_AUTH_PROFILE beats the stored default at launch time,
            # and it is read from the server process's own env — invisible to
            # this endpoint unless it is reported. Without this the screen and
            # `accounts ls` name one identity while every session runs as
            # another, and the "Make default" button appears to do nothing.
            try:
                from backend.providers import auth_profiles as _ap

                pinned = os.environ.get("MINDFLOCK_AUTH_PROFILE")
                if pinned:
                    view["default_profile"] = _ap.default_profile_id()
                    view["default_profile_env"] = pinned
                    view["default_profile_locked"] = True
            except Exception:  # noqa: BLE001 — enrichment only
                pass
            return view

        @router.get("/settings/auth-profiles")
        def get_auth_profiles() -> JSONResponse:
            return JSONResponse(_auth_profiles_view())

        @router.put("/settings/auth-profiles")
        def put_auth_profiles(
            body: dict, allowed: bool = Depends(_web_auth.configure_allowed)
        ) -> JSONResponse:
            """Replace the whole profiles list (same contract as the ticketing
            sources CRUD: an ``api_key`` that is empty or the mask sentinel
            keeps the previously-stored key, matched by ``id``). A
            ``default_profile`` key in the body updates the app-wide default in
            the same save; account-kind profiles get their isolated config dir
            created here so a login can land in it. Owner only
            (``auth.may_configure``): a profile's env and config dir reach
            every agent launched with it."""
            if not allowed:
                return _web_auth.configure_refused()
            body = body or {}
            incoming = body.get("profiles")
            if not isinstance(incoming, list):
                return JSONResponse(
                    {"error": 'expected {"profiles": [...]}'}, status_code=400
                )
            stored_profiles = settings_store.load_settings().auth_profiles.profiles
            stored_profiles_raw = [
                {"id": getattr(p, "id", "")} for p in stored_profiles
            ]
            prev = {p.id: p.api_key for p in stored_profiles}
            prev_env = {p.id: dict(p.env or {}) for p in stored_profiles}
            clean: list = []
            seen: set = set()
            for raw in incoming:
                if not isinstance(raw, dict):
                    continue
                pid = str(raw.get("id", "") or "").strip().lower()
                if not pid:
                    continue
                if not _NAME_RE.match(pid):
                    return JSONResponse(
                        {
                            "error": "account id '%s' must be lowercase "
                            "letters/digits/-/_ (max 64)" % pid
                        },
                        status_code=400,
                    )
                if pid == "default":
                    # Reserved: "default" is the AMBIENT_ID sentinel meaning
                    # "the CLI's own login" (backend.providers.auth_profiles).
                    # A profile so named would be accepted everywhere and
                    # resolve to NO overlay — sessions silently on the ambient
                    # login while the UI shows the profile selected.
                    return JSONResponse(
                        {
                            "error": "'default' is reserved (it means the "
                            "CLI's own login) — pick another id"
                        },
                        status_code=400,
                    )
                if pid in seen:
                    return JSONResponse(
                        {"error": "duplicate account id '%s'" % pid},
                        status_code=400,
                    )
                seen.add(pid)
                kind = str(raw.get("kind", "") or "account").strip().lower()
                if kind not in settings_store.AUTH_PROFILE_KINDS:
                    return JSONResponse(
                        {
                            "error": "unknown account kind '%s' (expected one "
                            "of %s)"
                            % (kind, ", ".join(settings_store.AUTH_PROFILE_KINDS))
                        },
                        status_code=400,
                    )
                p = dict(raw)
                p["id"] = pid
                p["kind"] = kind
                key = str(p.get("api_key", "") or "").strip()
                if key in ("", _MASK):
                    p["api_key"] = prev.get(pid, "")
                # env values are masked on read (they carry credentials for
                # CLIs the typed kinds don't know), so the mask sentinel here
                # means "keep the stored value" — same rule as api_key, per
                # env KEY. A key absent from the stored env resolves to ""
                # and is dropped by the store's serializer.
                if isinstance(p.get("env"), dict):
                    kept = prev_env.get(pid, {})
                    lost = [
                        k
                        for k, v in p["env"].items()
                        if isinstance(k, str) and v == _MASK and k not in kept
                    ]
                    if lost:
                        # Same shape as the api_key rule below and for the same
                        # reason: the keep-secret map is keyed by id, so an id
                        # RENAME cannot resolve the mask. Blanking these
                        # silently is worse than a key going missing — an empty
                        # credential in the env can break the CLI's auth
                        # outright.
                        return JSONResponse(
                            {
                                "error": "account '%s': re-enter %s (renaming an "
                                "id requires re-entering its secrets)"
                                % (pid, ", ".join(sorted(lost))),
                            },
                            status_code=400,
                        )
                    p["env"] = {
                        k: (kept.get(k, "") if v == _MASK else v)
                        for k, v in p["env"].items()
                        if isinstance(k, str)
                    }
                if kind in ("api_key", "openrouter") and not p["api_key"]:
                    # The keep-secret map is keyed by id, so this is exactly
                    # what an id RENAME with the mask sentinel produces — and a
                    # keyless key-profile would later launch sessions silently
                    # on the CLI's own login. Fail loudly instead.
                    return JSONResponse(
                        {
                            "error": "account '%s' (%s) has no API key — paste "
                            "one (renaming an id requires re-entering its key)"
                            % (pid, kind)
                        },
                        status_code=400,
                    )
                clean.append(p)
            # EVERYTHING is validated before ANYTHING is written: a 400 from
            # this endpoint must mean "nothing changed", and the default has to
            # be checked against the INCOMING list — validating after the list
            # replacement half-applied the request (and the dangling-default
            # cleanup could silently clear the app default on the way).
            default = str(body.get("default_profile", "") or "").strip()
            if "default_profile" in body and default and default not in seen:
                return JSONResponse(
                    {
                        "error": "unknown account '%s' — it is not in the "
                        "profiles list being saved" % default
                    },
                    status_code=400,
                )
            # Removing a profile that sessions are PINNED to is an identity
            # change for each of them: the overlay resolves to nothing and they
            # come back on the CLI's own login — the one thing this feature is
            # not allowed to do quietly. Name them and refuse; `force: true`
            # (the UI's "remove anyway") proceeds, because a user who has read
            # the list is entitled to.
            gone = {
                (p.get("id") or "")
                for p in stored_profiles_raw
                if (p.get("id") or "") and (p.get("id") or "") not in seen
            }
            if gone and not bool(body.get("force")):
                pinned = _sessions_pinned_to(gone)
                if pinned:
                    return JSONResponse(
                        {
                            "error": "still in use by %s — swap %s off it first "
                            "(the pane's @account chip), or resend with "
                            "force to run %s on the CLI's own login"
                            % (
                                ", ".join("'%s'" % t for t in pinned[:5])
                                + (
                                    " and %d more" % (len(pinned) - 5)
                                    if len(pinned) > 5
                                    else ""
                                ),
                                "them" if len(pinned) > 1 else "it",
                                "them" if len(pinned) > 1 else "it",
                            ),
                            "in_use": pinned,
                        },
                        status_code=409,
                    )
            try:
                settings_store.set_auth_profiles(clean)
                if "default_profile" in body:
                    settings_store.update_settings(
                        auth_profiles={"default_profile": default}
                    )
            except settings_store.SettingsUnreadable:
                return _unreadable_response()
            except Exception as err:  # noqa: BLE001
                return JSONResponse({"error": str(err)}, status_code=400)
            # Create each account profile's isolated dir now (0700, like the
            # settings dir) so the login flow has somewhere to land.
            try:
                from backend.providers import auth_profiles as ap

                for cfg in ap.load_profiles():
                    if cfg.kind == "account" and ap.login_env(cfg):
                        os.makedirs(ap.account_dir(cfg), mode=0o700, exist_ok=True)
            except Exception:  # noqa: BLE001 — the dir is created again at login
                pass
            return JSONResponse(_auth_profiles_view())

        @router.post("/settings/test/openrouter")
        def test_openrouter(
            body: Optional[dict] = None,
            allowed: bool = Depends(_web_auth.configure_allowed),
        ) -> JSONResponse:
            """Validate an OpenRouter key (request-supplied, or the one stored
            on ``profile_id`` — owner only, see :func:`_stored_secret_refused`)
            and report its spend + the models it can reach — the
            account-level usage story for key profiles, and the source for
            the model-picker dropdown. Never echoes the key."""
            from backend.providers import auth_profiles as ap

            body = body or {}
            key = str(body.get("api_key", "") or "").strip()
            base_url = str(body.get("base_url", "") or "").strip()
            if key in ("", _MASK) and not allowed:
                return _web_auth.configure_refused()
            if key in ("", _MASK):
                pid = str(body.get("profile_id", "") or "").strip()
                for p in settings_store.load_settings().auth_profiles.profiles:
                    if p.id == pid:
                        key = p.api_key
                        base_url = base_url or p.base_url
                        break
                else:
                    key = ""
            if not key:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": "no OpenRouter key configured — paste one first",
                    }
                )
            return JSONResponse(ap.probe_openrouter(key, base_url))

        @router.post("/settings/test/github")
        def test_github() -> JSONResponse:
            """Report where a GitHub token would come from (settings / env /
            gh CLI, per the github_auth resolution order) and whether the gh
            CLI is installed + authenticated. Never returns the token."""
            source = _github_token_source()
            gh_installed, gh_authenticated, gh_detail = _gh_cli_status()
            if not source and gh_authenticated:
                source = "gh-cli"
            return JSONResponse(
                {
                    "ok": bool(source),
                    "token_source": source or "none",
                    "gh_installed": gh_installed,
                    "gh_authenticated": gh_authenticated,
                    "detail": gh_detail,
                }
            )

        @router.post("/settings/test/github-repo")
        async def test_github_repo(body: Optional[dict] = None) -> JSONResponse:
            """Can the resolved GitHub token actually see ``owner/name``?

            The per-repo twin of ``/settings/test/github``: that one answers
            "is there a credential", this one answers "does it reach THIS
            repo" — which is the failure people actually hit (a typo'd slug, a
            private repo the PAT has no scope for). Each repo card in the Work
            surface has its own Test button for exactly this, mirroring the
            per-source Test on a ticketing card.
            """
            repo = str((body or {}).get("repo", "") or "").strip()
            if not re.match(r"^[^\s/]+/[^\s/]+$", repo):
                return JSONResponse({"ok": False, "error": "repo must be owner/name"})
            from backend.ticket_ingestion import github_auth

            # Resolve afresh: a cached token from before the user pasted a new
            # one would make this button answer about the wrong credential.
            github_auth.invalidate()
            try:
                token = (await github_auth.resolve_token(_repo_test_config())).strip()
            except github_auth.GithubAuthError:
                # Its message is a five-line config walkthrough aimed at
                # config.toml; on a card, one sentence naming this screen is
                # more use.
                return JSONResponse(
                    {
                        "ok": False,
                        "error": "no GitHub token available — set one under "
                        "Advanced options, or sign in with the gh CLI",
                    }
                )
            except Exception as err:  # noqa: BLE001 — never 500 a probe
                return JSONResponse({"ok": False, "error": str(err)})
            import aiohttp

            url = "https://api.github.com/repos/{}".format(repo)
            headers = {
                "Authorization": "Bearer {}".format(token),
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            try:
                timeout = aiohttp.ClientTimeout(total=15)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url, headers=headers) as resp:
                        status = resp.status
                        data = await resp.json(content_type=None)
            except Exception as err:  # noqa: BLE001 — offline / DNS / TLS
                return JSONResponse(
                    {"ok": False, "error": "could not reach api.github.com: %s" % err}
                )
            if status == 404:
                # 404 is also what GitHub returns for a private repo the token
                # can't see, so the message has to cover both readings.
                return JSONResponse(
                    {
                        "ok": False,
                        "error": "no such repo, or this token cannot see it "
                        "(private repos need the repo scope)",
                    }
                )
            if status != 200 or not isinstance(data, dict):
                msg = ""
                if isinstance(data, dict):
                    msg = str(data.get("message") or "").strip()
                return JSONResponse(
                    {"ok": False, "error": msg or "GitHub returned HTTP %d" % status}
                )
            perms = data.get("permissions") or {}
            return JSONResponse(
                {
                    "ok": True,
                    "name": data.get("full_name") or repo,
                    "private": bool(data.get("private")),
                    "default_branch": data.get("default_branch") or "",
                    # Reviewing pushes nothing, but issue handling needs to push
                    # a branch — so "read-only" is worth saying out loud.
                    "can_push": bool(perms.get("push")),
                }
            )

        @router.post("/settings/test/agent")
        def test_agent() -> JSONResponse:
            """Probe the configured agent CLI: binary resolvable + (for the
            claude family) best-effort login evidence."""
            cli = doctor.check_agent_cli()
            auth = doctor.check_agent_auth()
            ok = cli.status == "ok" and auth.status in ("ok", "info")
            return JSONResponse(
                {"ok": ok, "cli": cli.to_dict(), "auth": auth.to_dict()}
            )

        @router.get("/providers/manage")
        def list_providers_manage() -> JSONResponse:
            out = [
                _provider_view(p)
                for p in providers.all_providers()
                if p.name != "generic"  # the catch-all fallback isn't a real choice
            ]
            return JSONResponse({"providers": out})

        @router.get("/providers/status")
        def providers_status() -> JSONResponse:
            """Per-provider connection status (installed / logged-in / how to
            install + log in) for the Settings → Providers panel."""
            default_name = _default_provider_name()
            out = [
                _provider_status(p, default_name)
                for p in providers.all_providers()
                if p.name != "generic"  # the catch-all fallback isn't a choice
            ]
            return JSONResponse({"providers": out, "default": default_name})

        @router.websocket("/providers/{name}/login-terminal")
        async def provider_login_terminal(
            ws: WebSocket, name: str, profile: str = ""
        ) -> None:
            """Open a browser terminal running provider ``name``'s login flow so
            the user authenticates the CLI through the CLI itself. With
            ``?profile=<id>`` the login runs under that auth profile's isolated
            config dir, so a second (work) account signs in without touching
            the first. Setup's and the doctor's "Sign in to <agent>" open it
            too. Works without tmux (a plain PTY).

            Only for the person at this device (:func:`privileged`): a sign-in
            lands credentials HERE, so an anonymous caller of an exposed
            gate-off server, or a relaying MindFlock, must not drive one."""
            from backend.web.core import auth, provider_login, pty_run

            await ws.accept()
            if not await auth.privileged(ws.scope):
                await ws.send_text(
                    json.dumps(
                        {
                            "type": "error",
                            "message": "signing in is only allowed from this "
                            "computer or a signed-in device",
                        }
                    )
                )
                await ws.close(code=4403)
                return
            session, err = await asyncio.to_thread(
                provider_login.ensure_login_session, name, profile
            )
            if err is not None:
                await ws.send_text(json.dumps({"type": "error", "message": err}))
                await ws.close(code=4500)
                return
            await pty_run.serve(ws, session)

        @router.post("/providers/{name}/login-close")
        def provider_login_close(
            name: str,
            profile: str = "",
            allowed: bool = Depends(_web_auth.configure_allowed),
        ) -> JSONResponse:
            """Tear down a provider's login terminal (called when the UI closes
            the modal), so a completed login doesn't leave a stray tmux session.
            Owner only, like the terminal itself: a stranger mustn't kill a
            sign-in in progress."""
            from backend.web.core import provider_login

            if not allowed:
                return _web_auth.configure_refused()

            provider_login.kill_login_session(name, profile)
            return JSONResponse({"ok": True})

        @router.post("/providers")
        def create_provider(
            body: dict, allowed: bool = Depends(_web_auth.configure_allowed)
        ) -> JSONResponse:
            # A custom agent is a command line MindFlock runs, and settings
            # sync spreads it: owner only (auth.may_configure). So are its
            # edit and delete below.
            if not allowed:
                return _web_auth.configure_refused()
            body = body or {}
            name = str(body.get("name", "")).strip().lower()
            if not _NAME_RE.match(name):
                return JSONResponse(
                    {"error": "name must be lowercase letters/digits/-/_ (max 64)"},
                    status_code=400,
                )
            if name in providers.BUILTIN_NAMES:
                return JSONResponse(
                    {"error": f"'{name}' is a built-in provider; pick another name"},
                    status_code=400,
                )
            err = _provider_body_error(body)
            if err:
                return JSONResponse({"error": err}, status_code=400)
            d = self._providers_dir()
            d.mkdir(parents=True, exist_ok=True)
            target = d / f"{name}.toml"
            if target.exists():
                return JSONResponse(
                    {"error": f"provider '{name}' already exists"}, status_code=409
                )
            body["name"] = name
            target.write_text(_provider_toml(body), encoding="utf-8")
            providers.rebuild_registry()
            _stamp_for_sync("store:providers")  # custom providers sync across devices
            p = providers.get(name)
            return JSONResponse(
                {
                    "provider": _provider_view(p) if p else None,
                    # Saved, but it may not be launchable — say so now rather than
                    # letting the pane die with "command not found" later.
                    "warning": _provider_launch_warning(body),
                }
            )

        @router.put("/providers/{name}")
        def update_provider(
            name: str, body: dict, allowed: bool = Depends(_web_auth.configure_allowed)
        ) -> JSONResponse:
            if not allowed:
                return _web_auth.configure_refused()
            name = (name or "").strip().lower()
            if name in providers.BUILTIN_NAMES:
                return JSONResponse(
                    {"error": f"'{name}' is built-in and cannot be edited"},
                    status_code=400,
                )
            d = self._providers_dir()
            d.mkdir(parents=True, exist_ok=True)
            target = d / f"{name}.toml"
            body = dict(body or {})
            body["name"] = name
            err = _provider_body_error(body)
            if err:
                return JSONResponse({"error": err}, status_code=400)
            target.write_text(_provider_toml(body), encoding="utf-8")
            providers.rebuild_registry()
            _stamp_for_sync("store:providers")
            p = providers.get(name)
            return JSONResponse(
                {
                    "provider": _provider_view(p) if p else None,
                    "warning": _provider_launch_warning(body),
                }
            )

        @router.delete("/providers/{name}")
        def delete_provider(
            name: str, allowed: bool = Depends(_web_auth.configure_allowed)
        ) -> JSONResponse:
            if not allowed:
                return _web_auth.configure_refused()
            name = (name or "").strip().lower()
            if name in providers.BUILTIN_NAMES:
                return JSONResponse(
                    {"error": f"'{name}' is built-in and cannot be deleted"},
                    status_code=400,
                )
            target = self._providers_dir() / f"{name}.toml"
            existed = target.exists()
            if existed:
                try:
                    target.unlink()
                except OSError as err:
                    return JSONResponse({"error": str(err)}, status_code=500)
            providers.rebuild_registry()
            _stamp_for_sync("store:providers")
            return JSONResponse({"deleted": existed})

        return router

    @property
    def router(self) -> APIRouter:
        return self._router

    # --- frontend --------------------------------------------------------- #
    def frontend(self):
        return [
            FrontendDescriptor(
                id="settings",
                label="Settings",
                where="settings",
                module=None,  # rendered inline (index.html #settings-dialog)
                api_base="/api/settings",
                order=5,
                # The SPA renders the settings UI inline (index.html #settings-dialog)
                # rather than via the generic slot renderer.
                builtin_ui=True,
            )
        ]
