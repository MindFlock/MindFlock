"""The pipeline's GitHub token: the web resolver, memoized for a minute.

There is ONE GitHub credential chain, :func:`backend.web.core.github_auth.
resolve_token` — Settings (Connect GitHub) → ``$GH_TOKEN`` / ``$GITHUB_TOKEN``
→ ``gh auth token`` → ``[github].token`` in ``config.toml`` (deprecated, last).
This module only adapts it for the pipeline's async callers (PR review, issue
handling, PR comments) and memoizes the answer for :data:`MEMO_TTL` seconds,
so a poll loop does not shell out to ``gh`` every few seconds but a token
pasted into Connect GitHub reaches the loops within a minute even when nobody
calls :func:`invalidate` (the settings hooks do, when they can — the pipeline
is a separate process, so its cache cannot be cleared from the web server
without a restart; the short memo is what makes that restart optional).

Pre-history: this module used to be its own resolver with ``[github].token``
FIRST and a cache for the life of the process, so Setup could say "connected
as @x" while the loops used another token until a restart.
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

from backend.web.core import github_auth as _web

#: How long a resolved token is reused before the chain is walked again.
MEMO_TTL = 60.0

_cached_token: Optional[str] = None
_cached_at: float = 0.0
#: Where the memoized token came from (``settings``, ``env:GH_TOKEN``, …).
_cached_source: str = ""


class GithubAuthError(Exception):
    pass


def invalidate() -> None:
    """Forget the memoized token so the next resolve walks the chain again.

    Called when ``github.token`` is written (the settings addon / sync hooks)
    and by the per-repo access test, whose entire job is to answer "does the
    credential I just pasted work" — a memoized answer from the previous
    token would be worse than no test at all.
    """
    global _cached_token, _cached_at, _cached_source
    _cached_token = None
    _cached_at = 0.0
    _cached_source = ""


def cached_source() -> str:
    """The source of the token in use (``""`` when nothing is memoized)."""
    return _cached_source


def _toml_token(config) -> str:
    """The raw ``[github].token`` from ``config.toml`` carried on the pipeline
    config (``None`` or a config without one → ``""``)."""
    return str(getattr(config, "toml_token", "") or "").strip()


async def resolve_token(config=None) -> str:
    """The token every pipeline consumer calls GitHub with.

    ``config`` is the pipeline's ``GithubConfig`` (its ``toml_token`` is the
    deprecated last rung) or ``None`` for a caller without one, such as a
    ticketing provider. Raises :class:`GithubAuthError` when the whole chain
    is empty.
    """
    global _cached_token, _cached_at, _cached_source
    now = time.monotonic()
    if _cached_token and now - _cached_at < MEMO_TTL:
        return _cached_token

    # The pipeline is its own process: its settings cache would otherwise
    # hold the token it booted with forever, and the memo would re-resolve
    # to the same stale value. Drop it before every re-walk of the chain.
    try:
        from backend.config import settings as _settings_store

        _settings_store.invalidate()
    except Exception:  # noqa: BLE001 — a cache miss is the worst case
        pass
    # The web resolver is synchronous (it may run `gh auth token`, a real
    # subprocess with a 5 s cap): keep the event loop free while it does.
    source, token = await asyncio.to_thread(_web.resolve_token, _toml_token(config))
    if token:
        _cached_token = token
        _cached_at = now
        _cached_source = source
        return token

    raise GithubAuthError(
        "No GitHub token available. Connect GitHub under Settings → "
        "Connections → GitHub → Connect (device flow, gh, or a pasted token), "
        "or export GH_TOKEN / GITHUB_TOKEN, or run `gh auth login` once."
    )
