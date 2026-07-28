"""List Shortcut workflows and their states (id + name).

Usage:
    uv run python scripts/list_workflows.py
"""

import json
import sys
import urllib.request
from pathlib import Path

import tomli


def main() -> None:
    config_path = Path("config.toml")
    if not config_path.exists():
        sys.exit("config.toml not found in current directory")

    with open(config_path, "rb") as f:
        token = tomli.load(f)["shortcut"]["api_token"]

    req = urllib.request.Request(
        "https://api.app.shortcut.com/api/v3/workflows",
        headers={"Shortcut-Token": token},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        workflows = json.load(resp)

    for wf in workflows:
        print(f"\nWorkflow: {wf['name']} (id={wf['id']})")
        for state in wf.get("states", []):
            print(f"  {state['id']:>12}  {state['name']}  [{state.get('type','')}]")


if __name__ == "__main__":
    main()
