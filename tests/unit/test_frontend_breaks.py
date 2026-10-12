"""Take a break, the idle flock, and "a duplicate lands under its source".

The frontend half (the break card's buttons, the heartbeat and away-gap
numbers, the keymap standing down behind the card, the flock's round trip
through the logo, the idle overlay's CSS and its input list, the copy placed
under its source) is pinned by vitest against the source:
``frontend/src/__tests__/breaksUi.test.ts``.

What stays here is what only the server side can show: the sprite the flock
draws is really served (and ships inside the package), and the docs describe
both features.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from backend.web import server

client = TestClient(server.app)

_ROOT = Path(__file__).resolve().parents[2]


def test_flock_sprite_is_served():
    # lib/flock.ts draws `/bird.png` (pinned in breaksUi.test.ts).
    r = client.get("/bird.png")
    assert r.status_code == 200
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"
    # It ships inside the package, so a wheel install has it too.
    assert (_ROOT / "backend" / "web" / "static" / "bird.png").exists()


def test_web_ui_docs_describe_both_features():
    text = (_ROOT / "docs" / "web-ui.md").read_text(encoding="utf-8")
    assert "Take a break" in text
    assert "The idle flock" in text
    assert "beneath the one it was copied from" in text
