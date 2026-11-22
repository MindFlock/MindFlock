#!/usr/bin/env python3
"""See a UI change running: an isolated scratch MindFlock with sample data,
plus screenshots. From the repo root:

    uv run --group dev --group web python scripts/preview.py up --scenario sky --detach
    uv run --group dev --group web python scripts/preview.py shot --view sky --theme dark -o sky.png
    uv run --group dev --group web python scripts/preview.py down

``shot`` alone starts a preview for the shot and stops it afterwards. See
``--help`` and docs/development.md "Seeing a UI change"; the code is in
``scripts/mfpreview/``.
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from mfpreview.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
