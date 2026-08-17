"""Run a session's agent CLI under a chosen authentication identity.

Why this exists: one machine, several identities — a personal Claude
subscription next to a work one, an OpenRouter key next to both — and switching
between them meant logging the CLI out and back in. An **auth profile** names
one identity; a session (or the whole app, via the default profile) picks which
one its CLI runs as, and switching is a relaunch instead of a re-login.

The design copies :mod:`backend.providers.local_models` deliberately: a thin
**overlay**, not a new provider. :func:`overlay_for` maps
``(provider, AuthProfileConfig)`` to ``(env, launch_args)`` that every launch
path applies, and ``({}, ())`` must always mean "behave exactly as before" —
that contract is what keeps the golden-tested launch scripts byte-identical
when no profile is in play.

Three kinds of profile (see ``AUTH_PROFILE_KINDS`` in
:mod:`backend.config.settings`):

* ``account`` — a second login of the CLI itself, isolated in its own config
  dir. claude: ``CLAUDE_CONFIG_DIR`` (Claude Code ≥ 2.1.144 scopes its macOS
  Keychain entry per config dir, and ``/login`` writes into the dir the env var
  names, so two subscriptions stay cleanly apart); codex: ``CODEX_HOME``.
* ``api_key`` — a vendor API key injected for the session's CLI (claude:
  ``ANTHROPIC_API_KEY``; codex/aider/goose: ``OPENAI_API_KEY``).
* ``openrouter`` — route the CLI through OpenRouter under its own key. claude
  speaks OpenRouter's Anthropic-compatible endpoint (``ANTHROPIC_BASE_URL`` +
  ``ANTHROPIC_AUTH_TOKEN``); codex uses its OpenAI-compatible path; aider and
  goose have native OpenRouter support (``OPENROUTER_API_KEY`` + the
  ``openrouter/<model>`` spelling).

A combination without a verified route is reported via
:func:`unsupported_note` rather than launched with invented env — unless the
profile carries explicit ``env`` overrides, which apply to any CLI (the escape
hatch for user-defined providers).

Profile **env** is deliberately never baked into the provisioned launcher
script: it rides on the tmux environment (first start) or an ``export``
preamble (relaunch), so hot-swapping a session's profile only needs an agent
restart, not a rewritten workspace.
"""

from __future__ import annotations

import json
import os
import shlex
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Tuple

__all__ = [
    "AMBIENT_ID",
    "OPENROUTER_BASE_URL",
    "AuthProfileConfig",
    "load_profiles",
    "get_profile",
    "default_profile_id",
    "effective_profile_id",
    "overlay_for",
    "launch_overlay",
    "supported_agents",
    "account_dir",
    "login_env",
    "login_command",
    "unsupported_note",
    "claude_account_roots",
    "claude_account_root_map",
    "probe_openrouter",
]

#: The per-session id meaning "explicitly NO profile" — the CLI's own ambient
#: login — even when a global default profile is set. A session with an empty
#: profile id inherits the global default instead.
AMBIENT_ID = "default"

#: OpenRouter's documented API base (also its Anthropic-compatible endpoint).
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

#: CLIs with a verified OpenRouter route (see the module docstring).
_OPENROUTER_SUPPORTED = ("claude", "codex", "aider", "goose")

#: ``account``-kind isolation env var per CLI. A CLI absent here has no known
#: way to point it at an alternate config dir, so the kind is unsupported for it.
_ACCOUNT_DIR_ENV = {
    "claude": "CLAUDE_CONFIG_DIR",
    "codex": "CODEX_HOME",
}

#: ``api_key``-kind env var per CLI.
_API_KEY_ENV = {
    "claude": "ANTHROPIC_API_KEY",
    "codex": "OPENAI_API_KEY",
    "aider": "OPENAI_API_KEY",
    "goose": "OPENAI_API_KEY",
}

#: Seconds to wait on an OpenRouter probe — it backs a settings screen "Test"
#: button, which must render a failure instead of hanging.
_PROBE_TIMEOUT = 6.0


@dataclass(frozen=True)
class AuthProfileConfig:
    """One auth profile, decoupled from the settings store (same reason
    :class:`~backend.providers.local_models.LocalModelConfig` exists: the
    launch paths read this on every start and a settings quirk must never
    break a launch)."""

    id: str = ""
    label: str = ""
    kind: str = "account"
    provider: str = ""
    config_dir: str = ""
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    env: Dict[str, str] = field(default_factory=dict)

    def display_label(self) -> str:
        return self.label or self.id

    def resolved_provider(self) -> str:
        """The CLI this profile targets. Blank on ``account``/``api_key``
        means claude (the dominant use); blank on ``openrouter`` means "any
        CLI with an OpenRouter route"."""
        name = (self.provider or "").strip().lower()
        if name:
            return name
        return "" if self.kind == "openrouter" else "claude"


def _settings_profiles():
    """The raw settings group, or None. Best-effort by design — read on every
    launch, so a missing/corrupt store must degrade to "no profiles"."""
    try:
        from backend.config import settings as _settings

        return _settings.load_settings().auth_profiles
    except Exception:  # noqa: BLE001 — never break a launch over settings
        return None


def _from_settings(p) -> AuthProfileConfig:
    return AuthProfileConfig(
        id=p.id,
        label=p.label,
        kind=p.kind,
        provider=p.provider,
        config_dir=p.config_dir,
        api_key=p.api_key,
        base_url=p.base_url,
        model=p.model,
        env=dict(p.env or {}),
    )


def load_profiles() -> List[AuthProfileConfig]:
    """Every configured profile, or ``[]``. Never raises."""
    group = _settings_profiles()
    if group is None:
        return []
    return [_from_settings(p) for p in group.profiles if p.id]


def get_profile(profile_id: str) -> Optional[AuthProfileConfig]:
    """The profile with ``profile_id``, or None."""
    if not profile_id or profile_id == AMBIENT_ID:
        return None
    for p in load_profiles():
        if p.id == profile_id:
            return p
    return None


def default_profile_id() -> str:
    """The app-wide default profile id: ``$MINDFLOCK_AUTH_PROFILE`` →
    settings ``auth_profiles.default_profile`` → ``""`` (no profile)."""
    env = os.environ.get("MINDFLOCK_AUTH_PROFILE")
    if env is not None and env != "":
        return "" if env == AMBIENT_ID else env
    group = _settings_profiles()
    if group is None:
        return ""
    return group.default_profile or ""


def effective_profile_id(session_profile_id: str) -> str:
    """Resolve a session's stored profile id to the one that applies.

    Same tri-state as per-session launch args: ``""`` = not specified →
    inherit the global default; :data:`AMBIENT_ID` = explicitly none (the
    CLI's own ambient login); anything else = pinned to that profile.
    """
    pid = (session_profile_id or "").strip()
    if pid == AMBIENT_ID:
        return ""
    if pid:
        return pid
    return default_profile_id()


def account_dir(profile: AuthProfileConfig) -> str:
    """The isolated config dir an ``account`` profile lives in.

    An explicit ``config_dir`` wins (``~`` expanded); otherwise
    ``~/.mindflock/accounts/<id>`` — under the app's own config dir so an
    uninstall ``--purge`` sweeps it with everything else.
    """
    if profile.config_dir:
        return os.path.expanduser(profile.config_dir)
    try:
        from backend.config.config import GetConfigDir

        base = GetConfigDir()
    except Exception:  # noqa: BLE001
        base = os.path.join(os.path.expanduser("~"), ".mindflock")
    return os.path.join(base, "accounts", profile.id)


def _openai_compatible_env(base_url: str, api_key: str) -> Dict[str, str]:
    """Env for a CLI's OpenAI-compatible path (both spellings of the base URL,
    matching :func:`local_models._openai_compatible_env`)."""
    return {
        "OPENAI_API_BASE": base_url,
        "OPENAI_BASE_URL": base_url,
        "OPENAI_API_KEY": api_key,
    }


def _typed_overlay(
    name: str, p: AuthProfileConfig
) -> Optional[Tuple[Dict[str, str], Tuple[str, ...]]]:
    """The kind-specific ``(env, launch_args)`` for CLI ``name``, or None when
    this profile has no verified route for it (the caller then falls back to
    the profile's raw ``env`` overrides, if any)."""
    target = p.resolved_provider()
    model = (p.model or "").strip()

    if p.kind == "account":
        var = _ACCOUNT_DIR_ENV.get(name)
        if name != target or not var:
            return None
        env = {var: account_dir(p)}
        if name == "claude" and model:
            env["ANTHROPIC_MODEL"] = model
        return env, ()

    if p.kind == "api_key":
        var = _API_KEY_ENV.get(name)
        if name != target or not var or not p.api_key:
            return None
        env = {var: p.api_key}
        if name == "claude":
            if model:
                env["ANTHROPIC_MODEL"] = model
            return env, ()
        if name == "codex":
            return env, (("-m", model) if model else ())
        if name == "aider":
            return env, (("--model", model) if model else ())
        if name == "goose":
            env["GOOSE_PROVIDER"] = "openai"
            if model:
                env["GOOSE_MODEL"] = model
            return env, ()
        return None

    if p.kind == "openrouter":
        if not p.api_key or name not in _OPENROUTER_SUPPORTED:
            return None
        if target and name != target:
            return None
        base = (p.base_url or "").strip() or OPENROUTER_BASE_URL
        if name == "claude":
            # OpenRouter's Anthropic-compatible endpoint, per its own Claude
            # Code cookbook. The SDK appends /v1/… itself, so the base must
            # NOT carry the OpenAI-style /v1 suffix (…/api/v1/v1/messages
            # 404s — verified) — strip it from the stored base, which the
            # OpenAI-compatible routes below and the /key probe do need.
            # AUTH_TOKEN (a Bearer credential) is the gateway credential;
            # ANTHROPIC_API_KEY is pinned EMPTY per the cookbook so an
            # ambient key can't fight the gateway auth. Gateway model
            # discovery makes the CLI's own /model menu fetch a curated
            # picker from the gateway (refreshed at startup — which every
            # (re)launch and profile swap is). NOTE: a pinned ANTHROPIC_MODEL
            # bypasses that picker, per Claude Code's own precedence.
            anth_base = base.rstrip("/")
            if anth_base.endswith("/v1"):
                anth_base = anth_base[: -len("/v1")]
            env = {
                "ANTHROPIC_BASE_URL": anth_base,
                "ANTHROPIC_AUTH_TOKEN": p.api_key,
                "ANTHROPIC_API_KEY": "",
                "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
            }
            if model:
                env["ANTHROPIC_MODEL"] = model
            return env, ()
        if name == "codex":
            return (
                _openai_compatible_env(base, p.api_key),
                ("-m", model) if model else (),
            )
        if name == "aider":
            return (
                {"OPENROUTER_API_KEY": p.api_key},
                ("--model", f"openrouter/{model}") if model else (),
            )
        if name == "goose":
            env = {"GOOSE_PROVIDER": "openrouter", "OPENROUTER_API_KEY": p.api_key}
            if model:
                env["GOOSE_MODEL"] = model
            return env, ()

    return None


def overlay_for(
    provider_name: str, profile: Optional[AuthProfileConfig]
) -> Tuple[Dict[str, str], Tuple[str, ...]]:
    """``(env, launch_args)`` that run ``provider_name`` as ``profile``.

    Returns ``({}, ())`` when there is no profile or no route — callers apply
    the result unconditionally, so "no overlay" must mean "behave exactly as
    before". The profile's raw ``env`` overrides are merged last (and are the
    only thing applied to a CLI without a typed route, so an env-only profile
    works for any user-defined provider).
    """
    if profile is None:
        return {}, ()
    name = (provider_name or "").strip().lower()
    typed = _typed_overlay(name, profile)
    env, args = typed if typed is not None else ({}, ())
    if profile.env:
        env = {**env, **{k: str(v) for k, v in profile.env.items() if k}}
    if not env and not args:
        return {}, ()
    return env, tuple(args)


def launch_overlay(
    program: str, profile_id: str = "", model: str = ""
) -> Tuple[Dict[str, str], Tuple[str, ...]]:
    """``(env, launch_args)`` for ``program`` under the profile a session with
    ``profile_id`` resolves to (see :func:`effective_profile_id`).

    ``model`` is a per-session override of the profile's own model pin (the
    New dialog's Model picker) — blank keeps the profile's pin, which blank in
    turn means the CLI's own default.

    The single entry point the launch paths call. ``({}, ())`` when no profile
    applies.
    """
    profile = get_profile(effective_profile_id(profile_id))
    if profile is None:
        return {}, ()
    if (model or "").strip():
        profile = replace(profile, model=model.strip())
    try:
        from . import resolve

        name = resolve(program).name
    except Exception:  # noqa: BLE001
        name = (program or "").strip()
    return overlay_for(name, profile)


def supported_agents(profile: AuthProfileConfig) -> List[str]:
    """The CLIs this profile has a verified (typed) route for — what the New
    dialog uses to steer the Agent picker when an account is chosen, so a
    combination that would silently fall back to the CLI's own login is caught
    at selection time instead of at launch. A profile with raw ``env``
    overrides applies to every CLI regardless; callers check ``profile.env``
    for that case."""
    known = sorted(
        set(_ACCOUNT_DIR_ENV) | set(_API_KEY_ENV) | set(_OPENROUTER_SUPPORTED)
    )
    return [n for n in known if _typed_overlay(n, profile) is not None]


def login_env(profile: AuthProfileConfig) -> Dict[str, str]:
    """Env the profile's CLI needs while *logging in*, so the credential lands
    in this profile's isolated store rather than the ambient one. Empty for
    kinds that have nothing to log into (key-based profiles)."""
    if profile.kind != "account":
        return {}
    var = _ACCOUNT_DIR_ENV.get(profile.resolved_provider())
    if not var:
        return {}
    return {var: account_dir(profile)}


def login_command(profile: AuthProfileConfig) -> str:
    """A copy-pasteable shell command that logs the profile's CLI into this
    profile (``""`` when the kind has no login flow). The account dir is
    created by the caller; the CLI's own login flow does the rest."""
    env = login_env(profile)
    if not env:
        return ""
    try:
        from . import resolve

        provider = resolve(profile.resolved_provider())
        cmd = provider.login_command() or profile.resolved_provider()
    except Exception:  # noqa: BLE001
        cmd = profile.resolved_provider()
    exports = " ".join("%s=%s" % (k, shlex.quote(v)) for k, v in sorted(env.items()))
    return f"{exports} {cmd}"


def unsupported_note(program: str, profile_id: str = "") -> str:
    """A one-line explanation when a profile is in play but ``program`` has no
    route for it, else ``""`` — surfaced by the session UI and settings screen
    so the combination fails loudly instead of silently running the CLI on
    whatever it was already logged into (the one outcome an account switcher
    cannot afford to be quiet about)."""
    profile = get_profile(effective_profile_id(profile_id))
    if profile is None:
        return ""
    try:
        from . import resolve

        name = resolve(program).name
    except Exception:  # noqa: BLE001
        name = (program or "").strip()
    env, args = overlay_for(name, profile)
    if env or args:
        return ""
    return (
        f"account '{profile.display_label()}' has no route for "
        f"{name or program!r} — this session will keep using the CLI's own "
        f"login. Pick a different agent or account."
    )


def claude_account_roots() -> List[str]:
    """Config dirs of every claude ``account`` profile — the extra transcript
    roots the usage scanners must include, or a work-account session would
    report zero tokens (its transcripts live outside ``~/.claude*``)."""
    return list(claude_account_root_map())


def claude_account_root_map() -> Dict[str, str]:
    """``{config_dir: profile_id}`` for every claude ``account`` profile — how
    the usage scanners attribute a transcript root to the account it belongs
    to (a root not in this map is the ambient login)."""
    out: Dict[str, str] = {}
    for p in load_profiles():
        if p.kind == "account" and p.resolved_provider() == "claude":
            out[account_dir(p)] = p.id
    return out


def probe_openrouter(api_key: str, base_url: str = "") -> dict:
    """Ask OpenRouter whether ``api_key`` works, what it has spent, and which
    models it can reach — the account-level usage story for key profiles, and
    the source for the model-picker dropdown.

    Returns ``{"ok", "label", "usage", "limit", "models", "error"}``.
    Synchronous ``urllib`` with a short timeout; every failure lands in
    ``error`` rather than raising (this backs a settings "Test" button).
    """
    base = (base_url or "").strip() or OPENROUTER_BASE_URL
    out = {
        "ok": False,
        "label": "",
        "usage": None,
        "limit": None,
        "models": [],
        "error": "",
    }
    if not api_key:
        out["error"] = "no API key configured"
        return out

    def _get(path: str):
        req = urllib.request.Request(
            base.rstrip("/") + path,
            headers={"Authorization": "Bearer %s" % api_key},
        )
        with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8", "replace") or "{}")

    try:
        key_info = _get("/key")
    except urllib.error.HTTPError as err:
        out["error"] = (
            "invalid OpenRouter key" if err.code == 401 else f"HTTP {err.code}"
        )
        return out
    except Exception as err:  # noqa: BLE001 — offline / DNS / TLS
        out["error"] = f"cannot reach {base}: {err}"
        return out
    data = key_info.get("data") if isinstance(key_info, dict) else None
    if isinstance(data, dict):
        out["ok"] = True
        out["label"] = str(data.get("label") or "")
        out["usage"] = data.get("usage")
        out["limit"] = data.get("limit")
    else:
        out["error"] = "unexpected response shape from /key"
        return out
    try:
        models = _get("/models")
        entries = models.get("data") if isinstance(models, dict) else None
        out["models"] = [
            str(e["id"]) for e in entries or () if isinstance(e, dict) and e.get("id")
        ]
    except Exception:  # noqa: BLE001 — the key is valid; the list is a bonus
        pass
    return out
