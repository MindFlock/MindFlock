"""The GitHub Copilot CLI provider (npm ``@github/copilot``).

Config-driven like the other bundled CLIs (launch / resume / trust / effort come
from its :class:`ProviderConfig` in ``config.py``); this subclass adds the two
things that need code: the MindFlock MCP auto-attach and a fresh context.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from .generic import GenericProvider


class CopilotProvider(GenericProvider):
    # --- MindFlock MCP auto-attach ----------------------------------------- #
    def mcp_launch_args(self, spec) -> tuple:
        """ONE ``--additional-mcp-config <json>`` (Copilot: "Additional MCP
        servers configuration as JSON string … augments config from
        ~/.copilot/mcp-config.json for this session"). The JSON is the shape
        ``copilot mcp add`` itself writes: ``{"mcpServers": {"mindflock":
        {"type": "local", "command", "args", "env", "tools": ["*"]}}}`` —
        every MindFlock tool allowed. Per launch, never a config-file edit.

        Plus ``--allow-all-mcp-server-instructions``: Copilot otherwise keeps
        a non-allowlisted server's initialization instructions out of the
        system prompt and finds MCP tools through its own server-side tool
        search, which missed MindFlock's (``start_flight`` /
        ``flight_status`` / ``list_sessions`` "not available to invoke" in 3
        of 5 headless runs, 2026-11-03). With MindFlock's instructions — the
        tool map — in the prompt, 4 of 4 calls went through."""
        from . import mcp_attach

        server = {
            "type": "local",
            "command": spec.command(),
            "args": list(spec.args()),
            "env": {k: str(v) for k, v in sorted(spec.env().items())},
            "tools": ["*"],
        }
        return (
            "--additional-mcp-config",
            json.dumps(
                {"mcpServers": {mcp_attach.SERVER_NAME: server}}, separators=(",", ":")
            ),
            "--allow-all-mcp-server-instructions",
        )

    # --- sign-in evidence ---------------------------------------------------- #
    def auth_evidence(self) -> str:
        """A token in ``COPILOT_GITHUB_TOKEN`` / ``GH_TOKEN`` / ``GITHUB_TOKEN``
        (Copilot reads them first), else the account ``copilot login`` records
        in ``$COPILOT_HOME/config.json`` (default ``~/.copilot``):
        ``loggedInUsers`` / ``lastLoggedInUser``. The file's mere existence
        proves nothing — Copilot writes it on its very first run, signed in or
        not (``{"firstLaunchAt": …}``) — and the token itself usually lives in
        the system keychain, so the account list is the evidence."""
        for var in self.cfg.auth_env:
            if os.environ.get(var):
                return "%s is set" % var
        login = copilot_login()
        return ("signed in as %s" % login) if login else ""

    # --- a fresh context (a flight's Loop stop) ----------------------------- #
    def clear_context_command(self) -> Optional[str]:
        """Copilot's ``/clear`` ("Abandon this session and start fresh",
        captured from the 1.0.105 slash-command menu)."""
        return "/clear"


def copilot_config_path() -> Path:
    """``$COPILOT_HOME/config.json`` (``~/.copilot/config.json`` by default)."""
    home = os.environ.get("COPILOT_HOME") or os.path.join("~", ".copilot")
    return Path(os.path.expanduser(home)) / "config.json"


def copilot_login() -> str:
    """The GitHub login ``copilot login`` recorded, or ``""``. The file is
    JSON under ``//`` comment lines ("This file is managed automatically").
    Never raises."""
    try:
        text = copilot_config_path().read_text(encoding="utf-8")
        data = json.loads(
            "\n".join(
                ln for ln in text.splitlines() if not ln.lstrip().startswith("//")
            )
        )
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    users = data.get("loggedInUsers")
    last = data.get("lastLoggedInUser")
    for cand in ([last] if isinstance(last, dict) else []) + (
        users if isinstance(users, list) else []
    ):
        if isinstance(cand, dict) and str(cand.get("login") or "").strip():
            return str(cand["login"]).strip()
    return ""
