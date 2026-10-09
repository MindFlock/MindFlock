"""The uv installer pin exists twice — ``install.sh`` (runs before any Python)
and :mod:`backend._pins` (the doctor's uv fix). The bump-uv-pin workflow
rewrites both; this fails the moment they disagree."""

from __future__ import annotations

import re
from pathlib import Path

from backend import _pins

ROOT = Path(__file__).resolve().parents[2]


def _install_sh_value(name: str) -> str:
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    m = re.search(r'^%s="([^"]*)"' % name, text, re.M)
    assert m, f"{name} not found in install.sh"
    return m.group(1)


def test_uv_pin_matches_install_sh():
    assert _pins.UV_PINNED_VERSION == _install_sh_value("UV_PINNED_VERSION")
    assert _pins.UV_INSTALLER_SHA256 == _install_sh_value("UV_INSTALLER_SHA256")


def test_the_workflow_rewrites_both_files():
    wf = (ROOT / ".github" / "workflows" / "bump-uv-pin.yml").read_text()
    assert "backend/_pins.py" in wf and "install.sh" in wf
