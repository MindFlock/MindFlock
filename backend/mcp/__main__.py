"""Entry point for ``python -P -m backend.mcp`` (the MindFlock MCP stdio server).

``MINDFLOCK_MCP_MODE=peer`` (a sandboxed shared-folder session) serves the
peer toolset instead — see :mod:`backend.mcp.peer_tools`."""

import os

if (os.environ.get("MINDFLOCK_MCP_MODE") or "").strip().lower() == "peer":
    from backend.mcp.peer_tools import main
else:
    from backend.mcp import main

if __name__ == "__main__":
    raise SystemExit(main())
