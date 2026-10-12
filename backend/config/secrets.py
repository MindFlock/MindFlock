"""Shared secret / credential resolver for secrets with no chain of their own.

Every credential WITHOUT a dedicated resolver (the Shortcut API token, …) is
resolved the same way, so a new user can supply it wherever is most
convenient. The GitHub token is NOT resolved here: it has its own chain,
:func:`backend.web.core.github_auth.resolve_token` (Settings → env →
``gh auth token`` → config.toml, LAST), and every GitHub consumer — including
the cheap "is there a token" probes — goes through that one.

Resolution order for a secret::

    explicit (config.toml value the caller already parsed)
      →  settings.json  (the web Settings UI writes here)
      →  environment variable(s)
      →  CLI fallback     (async only)

Note the order differs from the *general* config resolver
(:func:`backend.config.settings.resolve`, which is ``env → settings → toml``):
for these secrets an **explicit** value the operator deliberately pasted into
``config.toml`` wins first. Everything below that is the user-friendly
fallback chain.

This module is intentionally cache-free: callers that want to memoize a resolved
secret keep their own process cache.
"""

from __future__ import annotations

import os
from typing import Awaitable, Callable, Optional, Sequence

from backend.config.settings import Settings, load_settings

__all__ = [
    "resolve_secret",
    "resolve_secret_sync",
]


def _static_secret(
    explicit: str,
    settings_getter: Optional[Callable[[Settings], str]],
    env_vars: Sequence[str],
) -> str:
    """Resolve the non-CLI layers: explicit → settings.json → env vars.

    Returns the first non-empty value, or ``""`` if none supply one.
    """
    if explicit:
        return explicit

    if settings_getter is not None:
        try:
            sv = settings_getter(load_settings())
        except Exception:  # noqa: BLE001 — a bad getter must not break resolution
            sv = ""
        if sv:
            return str(sv)

    for name in env_vars:
        v = os.environ.get(name)
        if v:
            return v

    return ""


def resolve_secret_sync(
    *,
    explicit: str = "",
    settings_getter: Optional[Callable[[Settings], str]] = None,
    env_vars: Sequence[str] = (),
) -> str:
    """Resolve a secret without any subprocess fallback (sync).

    Order: explicit → settings.json → env vars. Returns ``""`` when unresolved
    (the caller decides whether an empty secret is fatal). Use this for secrets
    with no CLI helper (e.g. the Shortcut API token).
    """
    return _static_secret(explicit, settings_getter, env_vars)


async def resolve_secret(
    *,
    explicit: str = "",
    settings_getter: Optional[Callable[[Settings], str]] = None,
    env_vars: Sequence[str] = (),
    cli_fallback: Optional[Callable[[], Awaitable[Optional[str]]]] = None,
) -> str:
    """Resolve a secret, optionally shelling out to a CLI as the last resort.

    Order: explicit → settings.json → env vars → ``cli_fallback()``. The CLI
    fallback is only awaited when every earlier layer is empty, so a configured
    value never triggers a subprocess. Returns ``""`` when unresolved.
    """
    val = _static_secret(explicit, settings_getter, env_vars)
    if val:
        return val

    if cli_fallback is not None:
        try:
            got = await cli_fallback()
        except (
            Exception
        ):  # noqa: BLE001 — CLI helpers signal "no token" by returning None
            got = None
        if got:
            return got

    return ""
