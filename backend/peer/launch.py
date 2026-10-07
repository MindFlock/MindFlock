"""Launching a shared-folder (``PeerShare``) session's agent — sandboxed or not at all.

Every launch site that starts the agent of a session whose ``PeerShare`` is set
(the engine's first :meth:`Instance.Start`, its :meth:`Instance.Resume`, and the
web relaunch in ``agent_sessions._ensure_agent_session``) builds its command
HERE, through :func:`build_command`, and nowhere else. The command is::

    env PYTHONPATH=<dir> <python> -P -m backend.peer.sandbox_exec \\
        --share <id> --provider <p> -- sh -c '<the provider's launch command>'

FAIL CLOSED: anything that stops the sandbox wrapper from being built — no
bubblewrap, a provider without a sandbox profile, the peer service not running
for this share (no agent-socket token), an unwritable MCP run file — raises
:class:`PeerLaunchError`, and the caller must then NOT start the session. There
is no fallback to an unsandboxed command anywhere in this module.

The per-share agent-socket token lives in memory only: the peer service mints a
fresh one whenever it starts a share's :class:`AgentApi` and registers it here,
so the MCP config written at launch carries the token the socket expects.
"""

from __future__ import annotations

import os
import shlex
import threading
from typing import Optional, Sequence

from backend.peer import paths

__all__ = [
    "ALLOWED_PROVIDERS",
    "PeerLaunchError",
    "register_token",
    "forget_token",
    "token_for",
    "provider_name",
    "check_sandbox",
    "sandbox_command",
    "build_command",
]

#: The CLIs that have a sandbox profile (``sandbox.prepare_home``). Anything
#: else is refused before a share session is even created.
ALLOWED_PROVIDERS = ("claude", "codex")


class PeerLaunchError(RuntimeError):
    """The shared-folder agent cannot be launched sandboxed — so it must not
    be launched at all."""


_TOKENS: dict = {}
_TOKENS_LOCK = threading.Lock()


def register_token(share_id: str, token: str) -> None:
    """Remember the agent-socket token the peer service just minted."""
    paths.share_root(share_id)  # validates the id (raises ValueError)
    if not token:
        raise ValueError("empty token")
    with _TOKENS_LOCK:
        _TOKENS[share_id] = token


def forget_token(share_id: str) -> None:
    with _TOKENS_LOCK:
        _TOKENS.pop(share_id or "", None)


def token_for(share_id: str) -> str:
    """The live agent-socket token for ``share_id``, or ``""``."""
    with _TOKENS_LOCK:
        return _TOKENS.get(share_id or "", "")


def provider_name(program: str) -> str:
    """The provider that would run ``program``; raises :class:`PeerLaunchError`
    unless it is one with a sandbox profile."""
    from backend import providers

    try:
        name = providers.resolve(program or "").name
    except Exception as err:  # noqa: BLE001
        raise PeerLaunchError("unknown agent CLI: %s" % err) from err
    if name not in ALLOWED_PROVIDERS:
        raise PeerLaunchError(
            "shared-folder sessions support only %s (got %s)"
            % (" and ".join(ALLOWED_PROVIDERS), name or "?")
        )
    return name


def check_sandbox() -> None:
    """Raise :class:`PeerLaunchError` unless the bubblewrap sandbox works here."""
    try:
        from backend.peer import sandbox

        ok, reason = sandbox.available()
    except Exception as err:  # noqa: BLE001 — a broken probe is "no sandbox"
        raise PeerLaunchError("peer sandbox unavailable: %s" % err) from err
    if not ok:
        raise PeerLaunchError("peer sandbox unavailable: %s" % (reason or "unknown"))


def sandbox_command(share_id: str, provider: str, inner_cmd: str) -> str:
    """``inner_cmd`` (a shell string) wrapped in ``sandbox_exec`` — one shell
    string for tmux. Raises :class:`PeerLaunchError` (never returns the inner
    command bare)."""
    try:
        paths.share_root(share_id)
    except ValueError as err:
        raise PeerLaunchError("bad share id") from err
    if provider not in ALLOWED_PROVIDERS:
        raise PeerLaunchError("provider %s has no sandbox profile" % provider)
    if not inner_cmd or not str(inner_cmd).strip():
        raise PeerLaunchError("empty launch command")
    check_sandbox()
    from backend.providers import mcp_attach

    argv = [
        "env",
        "PYTHONPATH=" + mcp_attach.mcp_pythonpath(),
        mcp_attach.mcp_python(),
        "-P",
        "-m",
        "backend.peer.sandbox_exec",
        "--share",
        share_id,
        "--provider",
        provider,
        "--",
        "sh",
        "-c",
        str(inner_cmd),
    ]
    return " ".join(shlex.quote(a) for a in argv)


def build_command(
    *,
    program: str,
    share_id: str,
    session_name: str,
    launch_args: Sequence[str] = (),
    resume: bool = False,
) -> str:
    """The complete, sandboxed launch command for a PeerShare session's agent.

    The provider builds its ordinary command with the PEER-mode MCP attach args
    in front of the session's own launch args, with NO workdir (so nothing is
    installed into the shared folder or the host's CLI config on its behalf)
    and NO seeded prompt (the seed file lives outside the sandbox — the peer
    service queues the intro prompt instead). Raises :class:`PeerLaunchError`.
    """
    from backend import providers
    from backend.providers import mcp_attach

    name = provider_name(program)
    work = paths.share_paths(share_id)["work"]  # validates the id
    if not os.path.isdir(work):
        raise PeerLaunchError("shared folder is missing")
    token = token_for(share_id)
    if not token:
        raise PeerLaunchError("the peer service is not running for this share")
    provider = providers.resolve(program or "")
    try:
        mcp_args = mcp_attach.peer_attach_args(provider, share_id=share_id, token=token)
    except Exception as err:  # noqa: BLE001
        raise PeerLaunchError("peer MCP config: %s" % err) from err
    if not mcp_args:
        raise PeerLaunchError("peer MCP config: provider cannot attach")
    ctx = providers.LaunchContext(
        program=program or "",
        workdir="",
        prompt="",
        resume=bool(resume),
        skip_permissions=False,
        in_place=True,
        session_name=session_name,
        launch_args=tuple(mcp_args) + tuple(launch_args or ()),
    )
    try:
        cmd: Optional[str] = provider.build_launch_command(ctx)
    except Exception as err:  # noqa: BLE001
        raise PeerLaunchError("launch command: %s" % err) from err
    if not cmd:
        raise PeerLaunchError("provider built no launch command")
    return sandbox_command(share_id, name, cmd)
