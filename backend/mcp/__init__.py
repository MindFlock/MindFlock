"""MindFlock's MCP server: lets the agent inside one session talk to the others.

A stdio Model Context Protocol server (stdlib only) that an agent CLI starts
as a child process — auto-attached by MindFlock for Claude/Codex sessions, or
registered by hand for an external client (``mindflock mcp --print-config``).
It is a thin client of the running MindFlock server's HTTP API; it never
touches the engine directly.

Layout:

* :mod:`backend.mcp.protocol` — JSON-RPC over stdio, lifecycle, tool calls,
  cancellation and progress;
* :mod:`backend.mcp.tools` — the 14 tools (schemas, descriptions, handlers)
  and the server ``instructions``;
* :mod:`backend.mcp.identity` — which session this process runs in;
* :mod:`backend.mcp.policy` — scopes and the "managed" set (a guard-rail,
  not a security boundary);
* :mod:`backend.mcp.api` — retrying wrapper over :mod:`backend.client`;
* :mod:`backend.mcp.gitlocal` — read-only local git probes.

Run it as ``python -P -m backend.mcp`` (``-P``: the agent's cwd may contain a
different ``backend/`` package, e.g. a MindFlock worktree) or ``mindflock mcp``.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import BinaryIO, Callable, List, Mapping, Optional

__all__ = ["SERVER_NAME", "build_server", "main", "parser"]

SERVER_NAME = "mindflock"


def parser(prog: str = "python -P -m backend.mcp") -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=prog,
        description="MindFlock MCP stdio server (speaks MCP on stdin/stdout).",
    )
    p.add_argument(
        "--scope",
        choices=("readonly", "children", "all"),
        default=None,
        help="what this server may steer (default: $MINDFLOCK_MCP_SCOPE or children)",
    )
    p.add_argument(
        "--host",
        default=None,
        help="server host (default: $MINDFLOCK_HOST or 127.0.0.1)",
    )
    p.add_argument(
        "--port",
        type=int,
        default=None,
        help="server port (default: $MINDFLOCK_PORT or 8765)",
    )
    return p


def build_server(
    *,
    stdin: BinaryIO,
    stdout: BinaryIO,
    scope: Optional[str] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
    env: Optional[Mapping[str, str]] = None,
    on_eof: Optional[Callable[[], None]] = None,
):
    """Wire API client + identity + policy + tools into an :class:`McpServer`."""
    from backend import __version__
    from backend.mcp.api import Api
    from backend.mcp.identity import Identity
    from backend.mcp.policy import Policy
    from backend.mcp.protocol import McpServer
    from backend.mcp.tools import INSTRUCTIONS, Toolbox, build_tools

    env = os.environ if env is None else env
    identity = Identity(env)
    configured = scope if scope is not None else env.get("MINDFLOCK_MCP_SCOPE")
    policy = Policy(configured, identity.managed)
    if policy.invalid_scope:
        logging.getLogger(__name__).warning(
            "unknown MINDFLOCK_MCP_SCOPE %r; running readonly", configured
        )
    api = Api(host, port, env)
    box = Toolbox(api, identity, policy)
    return McpServer(
        build_tools(box),
        stdin,
        stdout,
        name=SERVER_NAME,
        version=__version__,
        instructions=INSTRUCTIONS,
        on_eof=on_eof,
    )


def main(
    argv: Optional[List[str]] = None, prog: str = "python -P -m backend.mcp"
) -> int:
    """Serve MCP on this process's stdin/stdout until stdin closes.

    The real stdout is duplicated for the protocol and fd 1 is then pointed at
    stderr, so a stray ``print`` (ours or a library's, or a child process
    inheriting fd 1) can never corrupt the JSON-RPC stream."""
    args = parser(prog).parse_args(argv)
    proto_out = os.fdopen(os.dup(sys.stdout.fileno()), "wb")
    sys.stdout.flush()
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    level = (os.environ.get("MINDFLOCK_MCP_LOG") or "WARNING").upper()
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, level, logging.WARNING),
        format="mindflock-mcp %(levelname)s %(name)s: %(message)s",
    )
    server = build_server(
        stdin=sys.stdin.buffer,
        stdout=proto_out,
        scope=args.scope,
        host=args.host,
        port=args.port,
    )
    server.run()  # returns only when on_eof doesn't exit (never, by default)
    return 0
