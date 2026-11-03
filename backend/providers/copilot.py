"""The GitHub Copilot CLI provider (npm ``@github/copilot``).

Config-driven like the other bundled CLIs (launch / resume / trust / effort come
from its :class:`ProviderConfig` in ``config.py``); this subclass adds the two
things that need code: the MindFlock MCP auto-attach and a fresh context.
"""

from __future__ import annotations

import json
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
        every MindFlock tool allowed. Per launch, never a config-file edit."""
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
        )

    # --- a fresh context (a flight's Loop stop) ----------------------------- #
    def clear_context_command(self) -> Optional[str]:
        """Copilot's ``/clear`` ("Abandon this session and start fresh",
        captured from the 1.0.105 slash-command menu)."""
        return "/clear"
