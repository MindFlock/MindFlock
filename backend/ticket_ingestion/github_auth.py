"""Resolve a GitHub API token via the shared secret resolver.

Resolution order (see :func:`backend.config.secrets.resolve_secret`):
  1. ``[github].token`` in config.toml (explicit — an operator's deliberate paste)
  2. ``github.token`` in the web Settings store (``~/.mindflock/settings.json``)
  3. ``$GH_TOKEN`` / ``$GITHUB_TOKEN``
  4. ``gh auth token`` (OAuth device flow, same as VS Code) as a last resort

The resolved token is cached for the lifetime of the process — pasting a new one
into the UI therefore has to say so (:func:`invalidate`), or every consumer keeps
using the old one until a restart.
"""

import asyncio

from backend.config.secrets import resolve_secret
from backend.ticket_ingestion.config import GithubConfig

_cached_token: str | None = None


class GithubAuthError(Exception):
    pass


def invalidate() -> None:
    """Forget the cached token so the next resolve walks the chain again.

    Called when ``github.token`` is written (the settings addon) and by the
    per-repo access test, whose entire job is to answer "does the credential I
    just pasted work" — a cached answer from the previous token would be worse
    than no test at all.
    """
    global _cached_token
    _cached_token = None


async def resolve_token(config: GithubConfig) -> str:
    global _cached_token
    if _cached_token:
        return _cached_token

    token = await resolve_secret(
        explicit=config.token,
        settings_getter=lambda s: s.github.token,
        env_vars=("GH_TOKEN", "GITHUB_TOKEN"),
        cli_fallback=_gh_auth_token,
    )
    if token:
        _cached_token = token
        return _cached_token

    raise GithubAuthError(
        "No GitHub token available. Either:\n"
        "  - set [github].token in config.toml,\n"
        "  - set the GitHub token in the Settings UI,\n"
        "  - export GH_TOKEN or GITHUB_TOKEN,\n"
        "  - or run `gh auth login` once."
    )


async def _gh_auth_token() -> str | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh",
            "auth",
            "token",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return None
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return None
    token = stdout.decode(errors="replace").strip()
    return token or None
