"""Entry point for ``python -P -m backend.mcp`` (the MindFlock MCP stdio server)."""

from backend.mcp import main

if __name__ == "__main__":
    raise SystemExit(main())
