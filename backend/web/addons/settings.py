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
import shutil
from pathlib import Path
from typing import Optional, Tuple

from fastapi import APIRouter, WebSocket
from fastapi.responses import JSONResponse

from backend import doctor, providers
from backend.config import settings as settings_store
from backend.providers import config as provider_config
from backend.web.core import mobile_announce, restart

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


def _installed_path(binary: str) -> str:
    """Resolve a CLI ``binary`` to the executable path in effect, or ``""``.

    An explicit path override (contains ``os.sep``) is used directly when it is
    an executable file; otherwise the name is looked up on ``$PATH``. An empty
    ``binary`` (or an unresolved one) yields ``""`` — i.e. "not installed"."""
    if not binary:
        return ""
    if os.sep in binary:  # explicit path override — check the file directly
        return binary if (os.path.isfile(binary) and os.access(binary, os.X_OK)) else ""
    return shutil.which(binary) or ""


def _provider_installed(name: str) -> bool:
    """Whether provider ``name``'s CLI binary is present (same check as the
    Settings → Agent providers status list). Never raises."""
    try:
        p = providers.resolve(name)
        cfg = getattr(p, "cfg", None)
        binary = provider_config.resolve_provider_binary(getattr(p, "name", name), cfg)
        return bool(_installed_path(binary))
    except Exception:  # noqa: BLE001 — a probe failure is "not installed", not a crash
        return False


def _apply_post(payload: dict) -> None:
    """Apply a partial ``{group: {field: value}}`` update to the store.

    An empty string clears a normal field (falls through the resolution chain);
    for a *secret* an empty string / the mask sentinel means "keep the existing
    value" (so re-saving the form doesn't wipe a token the UI never received).

    The default agent provider is guarded: it may only be set to a CLI that is
    actually installed — you can never make an absent CLI the launch default.
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
        # Carried so Test exercises the query the pipeline will actually run:
        # an any-assignee source searches by state, not by member id.
        assignee_scope=pick("assignee_scope", sp("assignee_scope")),
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
        def get_auth_token() -> JSONResponse:
            """This device's access token in the clear — what another MindFlock
            device enters to remote-control this one, and what the browser
            sign-in page asks for. Serving it behind the auth gate is safe: any
            caller that reached this route either already presented the token
            (the cookie IS the token) or the gate is off, in which case the
            whole server is open anyway. Generates + persists a token on first
            use so the Security screen always has one to show."""
            from backend.web.core import auth as web_auth

            return JSONResponse(
                {"token": web_auth.get_token(), "auth_enabled": web_auth.auth_enabled()}
            )

        @router.post("/settings/auth-token/rotate")
        def rotate_auth_token() -> JSONResponse:
            """Invalidate the current access token and mint a fresh one
            (compromise recovery). Every signed-in browser cookie, ``/m`` QR
            code, and paired device's stored token stops working immediately;
            the response re-issues THIS caller's cookie so the device that
            rotated stays signed in. 409 when the token is pinned by
            ``MINDFLOCK_AUTH_TOKEN`` (the env var always wins, so rotating the
            setting would be a lie)."""
            from backend.web.core import auth as web_auth

            try:
                token = web_auth.rotate_token()
            except RuntimeError as err:
                return JSONResponse({"error": str(err)}, status_code=409)
            except Exception as err:  # noqa: BLE001 — settings store failure
                return JSONResponse(
                    {"error": "could not persist the new token: %s" % err},
                    status_code=500,
                )
            resp = JSONResponse(
                {"token": token, "auth_enabled": web_auth.auth_enabled()}
            )
            resp.set_cookie(
                key=web_auth.COOKIE_NAME,
                value=token,
                httponly=True,
                samesite="lax",
                path="/",
                max_age=60 * 60 * 24 * 365,
            )
            return resp

        @router.post("/settings")
        def post_settings(payload: dict) -> JSONResponse:
            payload = payload or {}

            # The automated-PR-review / issue-handling toggles (github.enabled,
            # github.issues_enabled) are only read when the pipeline process
            # boots, so snapshot them before applying and, on a real flip, emit
            # an event the ingestion addon uses to restart a live pipeline.
            # enabled is Optional[bool]; the UI treats unset/None as "on" for
            # PR review but as "off" for issue handling (opt-in), so compare
            # the normalized on/off states (not raw values).
            def _toggle_states() -> tuple[bool, bool]:
                gh = settings_store.load_settings().github
                return (gh.enabled is not False, gh.issues_enabled is True)

            gh_in = payload.get("github")
            watch_toggle = isinstance(gh_in, dict) and (
                "enabled" in gh_in or "issues_enabled" in gh_in
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
            try:
                _apply_post(payload)
            except Exception as err:  # noqa: BLE001
                return JSONResponse({"error": str(err)}, status_code=400)
            if watch_toggle and self.ctx is not None:
                if _toggle_states() != before:
                    try:
                        self.ctx.emit("settings.github_toggled")
                    except Exception:  # noqa: BLE001 — never let the bus break a save
                        pass

            view = {"settings": _masked_view()}
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
            Ticket Ingestion settings screen renders."""
            from backend.ticket_ingestion.providers import PROVIDER_META

            return JSONResponse({"providers": PROVIDER_META})

        @router.post("/settings/test/ticketing")
        async def test_ticketing(body: Optional[dict] = None) -> JSONResponse:
            """Validate the active (or request-supplied) ticketing provider's
            credentials via its own ``test_connection``. Returns the resolved
            member id so the UI can auto-fill it. Never echoes a token."""
            from backend.ticket_ingestion.providers import (
                ProviderError,
                get_provider,
            )

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
        async def ticketing_states(body: Optional[dict] = None) -> JSONResponse:
            """The workflow states/statuses a ticket can be in, for the "ingest
            only when the ticket is in state X" picker. Uses the request-supplied
            or stored credentials (never echoes a token). Providers without
            workflow states return ``{"states": []}``."""
            from backend.ticket_ingestion.providers import (
                ProviderError,
                get_provider,
            )

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
        def put_ticketing_sources(body: dict) -> JSONResponse:
            """Replace the whole sources list. A source whose ``api_token`` is
            empty or the mask sentinel keeps its previously-stored token (matched
            by ``id``), so re-saving the form never wipes a secret the UI never
            received. Blank ``provider`` entries are dropped."""
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
            except Exception as err:  # noqa: BLE001
                return JSONResponse({"error": str(err)}, status_code=400)
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
            return {
                "profiles": _masked_profiles(),
                "default_profile": s.default_profile,
                "kinds": list(settings_store.AUTH_PROFILE_KINDS),
            }

        @router.get("/settings/auth-profiles")
        def get_auth_profiles() -> JSONResponse:
            return JSONResponse(_auth_profiles_view())

        @router.put("/settings/auth-profiles")
        def put_auth_profiles(body: dict) -> JSONResponse:
            """Replace the whole profiles list (same contract as the ticketing
            sources CRUD: an ``api_key`` that is empty or the mask sentinel
            keeps the previously-stored key, matched by ``id``). A
            ``default_profile`` key in the body updates the app-wide default in
            the same save; account-kind profiles get their isolated config dir
            created here so a login can land in it."""
            body = body or {}
            incoming = body.get("profiles")
            if not isinstance(incoming, list):
                return JSONResponse(
                    {"error": 'expected {"profiles": [...]}'}, status_code=400
                )
            stored_profiles = settings_store.load_settings().auth_profiles.profiles
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
                    p["env"] = {
                        k: (prev_env.get(pid, {}).get(k, "") if v == _MASK else v)
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
            try:
                settings_store.set_auth_profiles(clean)
                if "default_profile" in body:
                    settings_store.update_settings(
                        auth_profiles={"default_profile": default}
                    )
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
        def test_openrouter(body: Optional[dict] = None) -> JSONResponse:
            """Validate an OpenRouter key (request-supplied, or the one stored
            on ``profile_id``) and report its spend + the models it can reach —
            the account-level usage story for key profiles, and the source for
            the model-picker dropdown. Never echoes the key."""
            from backend.providers import auth_profiles as ap

            body = body or {}
            key = str(body.get("api_key", "") or "").strip()
            base_url = str(body.get("base_url", "") or "").strip()
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
            the first."""
            from backend.web.core import provider_login
            from backend.web.core.terminal import pump_pty, spawn_tmux_attach

            await ws.accept()
            session, err = await asyncio.to_thread(
                provider_login.ensure_login_session, name, profile
            )
            if err is not None:
                await ws.send_text(json.dumps({"type": "error", "message": err}))
                await ws.close(code=4500)
                return
            try:
                proc = spawn_tmux_attach(session)
            except Exception as exc:  # noqa: BLE001
                await ws.send_text(json.dumps({"type": "error", "message": str(exc)}))
                await ws.close(code=4500)
                return
            await pump_pty(ws, proc, allow_input=True)

        @router.post("/providers/{name}/login-close")
        def provider_login_close(name: str, profile: str = "") -> JSONResponse:
            """Tear down a provider's login terminal (called when the UI closes
            the modal), so a completed login doesn't leave a stray tmux session."""
            from backend.web.core import provider_login

            provider_login.kill_login_session(name, profile)
            return JSONResponse({"ok": True})

        @router.post("/providers")
        def create_provider(body: dict) -> JSONResponse:
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
        def update_provider(name: str, body: dict) -> JSONResponse:
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
            p = providers.get(name)
            return JSONResponse(
                {
                    "provider": _provider_view(p) if p else None,
                    "warning": _provider_launch_warning(body),
                }
            )

        @router.delete("/providers/{name}")
        def delete_provider(name: str) -> JSONResponse:
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
