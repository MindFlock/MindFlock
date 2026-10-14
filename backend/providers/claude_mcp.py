"""Claude Code's MCP server config, read for shared-MCP launches.

What :mod:`backend.providers.mcp_share` needs from Claude: the user's own
stdio servers (``mcpServers`` in the user config — ``~/.claude.json``, or
``$CLAUDE_CONFIG_DIR/.claude.json``), whether a session's folder redefines one
of them, and the ``--mcp-config`` entries that point the shared ones at the
bridge.

The override, verified against Claude Code 2.1.299 with a scratch HOME and a
stub API (docs/mcp.md, "Shared MCP servers"): a server named in
``--mcp-config`` REPLACES a same-named user, local or ``.mcp.json`` server —
the user's stdio process is never spawned — and the user's own per-project
``disabledMcpServers`` and ``deniedMcpServers`` still apply to it by name. So
the run file only has to carry the shared names; everything else the user has
keeps loading the normal way, with no ``--strict-mcp-config`` and no copy of
their config to keep in sync.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Optional

__all__ = [
    "user_config_path",
    "user_mcp_servers",
    "project_redefinitions",
    "shared_mcp_entries",
    "list_servers",
]


def user_config_path() -> str:
    """The user config Claude reads: ``MINDFLOCK_CLAUDE_JSON`` (tests), else
    ``$CLAUDE_CONFIG_DIR/.claude.json``, else ``~/.claude.json``."""
    override = (os.environ.get("MINDFLOCK_CLAUDE_JSON") or "").strip()
    if override:
        return override
    cfg = (os.environ.get("CLAUDE_CONFIG_DIR") or "").strip()
    if cfg:
        return os.path.join(cfg, ".claude.json")
    return os.path.join(os.path.expanduser("~"), ".claude.json")


def _load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def user_mcp_servers(config: Optional[dict] = None) -> dict:
    """``{name: entry}`` of the user-scope servers (every window loads them)."""
    cfg = _load(user_config_path()) if config is None else config
    servers = cfg.get("mcpServers")
    if not isinstance(servers, dict):
        return {}
    return {str(k): v for k, v in servers.items() if isinstance(v, dict)}


def _git_roots(workdir: str) -> list:
    """The worktree top level and the canonical repo root (Claude keys local
    scope by the latter), best-effort."""
    out = []
    try:
        res = subprocess.run(
            [
                "git",
                "-C",
                workdir,
                "rev-parse",
                "--path-format=absolute",
                "--show-toplevel",
                "--git-common-dir",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode == 0:
            lines = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
            if lines:
                out.append(lines[0])
            if len(lines) > 1:
                common = lines[1]
                out.append(
                    os.path.dirname(common)
                    if os.path.basename(common) == ".git"
                    else common
                )
    except (OSError, subprocess.SubprocessError):
        pass
    return out


def _ancestors(path: str) -> list:
    out = []
    cur = os.path.realpath(path)
    while True:
        out.append(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            return out
        cur = parent


def project_redefinitions(workdir: str, names, config: Optional[dict] = None) -> set:
    """Which of ``names`` this folder redefines DIFFERENTLY from the user
    entry — a local-scope entry (``projects.<path>.mcpServers`` for the folder,
    its ancestors or its repo root) or a ``.mcp.json`` on the way up. Such a
    session keeps its own server: the shared override would replace it."""
    names = set(names or ())
    if not names or not workdir:
        return set()
    cfg = _load(user_config_path()) if config is None else config
    user = user_mcp_servers(cfg)
    paths = []
    for p in [workdir] + _git_roots(workdir):
        for a in _ancestors(p):
            if a not in paths:
                paths.append(a)
    projects = cfg.get("projects") if isinstance(cfg.get("projects"), dict) else {}
    found: set = set()
    for p in paths:
        local = (
            (projects.get(p) or {}).get("mcpServers")
            if isinstance(projects.get(p), dict)
            else None
        )
        shared_file = _load(os.path.join(p, ".mcp.json")).get("mcpServers")
        for scope in (local, shared_file):
            if not isinstance(scope, dict):
                continue
            for name in names & set(scope):
                if _strip(scope[name]) != _strip(user.get(name)):
                    found.add(name)
    return found


def _strip(entry) -> dict:
    """An entry's launch-relevant fields (``type`` defaults to stdio)."""
    if not isinstance(entry, dict):
        return {}
    return {
        "type": str(entry.get("type") or "stdio"),
        "command": entry.get("command"),
        "args": entry.get("args") or [],
        "env": entry.get("env") or {},
        "url": entry.get("url"),
    }


def shared_mcp_entries(workdir: str) -> dict:
    """``{name: http entry}`` to add to a session's ``--mcp-config``: each
    user-scope stdio server the user chose to share, pointed at the bridge.
    ``{}`` when nothing is shared or the bridge cannot be reached — the
    session then spawns its own copies, as without the feature."""
    from . import mcp_share

    names = mcp_share.shared_names()
    if not names:
        return {}
    cfg = _load(user_config_path())
    user = user_mcp_servers(cfg)
    defs = {}
    for name in names:
        d = mcp_share.expand_definition(user.get(name))
        if d is not None:
            defs[name] = d
    if not defs:
        return {}
    skip = project_redefinitions(workdir, defs, cfg)
    bridge = mcp_share.ensure_bridge()
    if bridge is None:
        return {}
    mcp_share.publish(defs)
    out = {}
    for name in defs:
        if name in skip:
            continue
        timeout = user[name].get("timeout")
        out[name] = mcp_share.endpoint_entry(
            bridge, name, timeout if isinstance(timeout, int) and timeout > 0 else None
        )
    return out


def list_servers() -> list:
    """The user-scope servers for Settings: ``[{name, transport, command,
    shareable, caution, reason}]`` — never the env (it holds tokens)."""
    from . import mcp_share

    out = []
    for name, entry in sorted(user_mcp_servers().items()):
        transport = str(entry.get("type") or "stdio").lower()
        row = {
            "name": name,
            "transport": transport,
            "command": os.path.basename(
                str(entry.get("command") or entry.get("url") or "")
            ),
            "shareable": False,
            "caution": "",
            "reason": "",
        }
        if name in mcp_share.RESERVED:
            row["reason"] = "MindFlock's own server carries each session's identity."
        elif transport != "stdio":
            row["reason"] = "Already a remote server — nothing runs per window."
        elif mcp_share.expand_definition(entry) is None:
            row["reason"] = (
                "Its definition uses a variable MindFlock's server doesn't have."
            )
        else:
            row["shareable"] = True
            row["caution"] = mcp_share.share_caution(name, entry)
        out.append(row)
    return out
