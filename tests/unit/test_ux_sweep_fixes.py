"""The UI problems a ux-sweep run found (2026-11-25) stay fixed: controls in
the app's font, symbol-only buttons named, phone keys named, phone sheets
modal and closable with Escape, tiny controls with a 24px hit area."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FE = ROOT / "frontend" / "src"
STATIC = ROOT / "backend" / "web" / "static"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_controls_inherit_the_app_font():
    css = _read(FE / "styles" / "base.css")
    assert re.search(
        r"button,\s*input,\s*select,\s*textarea\s*\{\s*font-family:\s*inherit;", css
    )


def test_symbol_only_buttons_carry_a_name():
    assert (
        'aria-label={pinOpen ? "Collapse the prompt to one line" : "Show the whole prompt"}'
        in _read(FE / "components" / "grid" / "Pane.tsx")
    )
    assert 'aria-label="Notification settings"' in _read(
        FE / "components" / "NotificationsBell.tsx"
    )
    assert (
        _read(FE / "components" / "AnswerStrip.tsx").count('aria-label="Open its pane"')
        == 2
    )


def test_settings_has_one_spelling():
    assert 'aria-label="Open Settings"' in _read(FE / "components" / "TopBar.tsx")


def test_phone_keys_are_named():
    html = _read(STATIC / "mobile.html")
    for key, label in (
        ("up", "Arrow up"),
        ("down", "Arrow down"),
        ("left", "Arrow left"),
        ("right", "Arrow right"),
        ("enter", "Enter"),
    ):
        assert f'data-key="{key}" aria-label="{label}"' in html


def test_phone_sheets_are_modal_and_escape_closes_them():
    html = _read(STATIC / "mobile.html")
    for sid in (
        "sessions-sheet",
        "act-sheet",
        "hub-sheet",
        "recent-sheet",
        "ask-sheet",
    ):
        tag = re.search(r'<div id="%s"[^>]*>' % sid, html).group(0)
        assert 'aria-modal="true"' in tag, sid
    assert 'id="sessions-done"' in html
    js = _read(STATIC / "mobile.js")
    assert "function dismissSheet(el)" in js
    assert re.search(
        r'ev\.key !== "Escape"[\s\S]{0,1200}dismissSheet\(open\[open\.length - 1\]\)',
        js,
    )


def test_tiny_controls_get_a_24px_hit_area():
    for path, sel in (
        (
            FE / "components" / "sidebar" / "Sidebar.css",
            "#sidebar-footer .foot-link::after",
        ),
        (FE / "components" / "sidebar" / "SidebarRow.css", ".inst .fold-kids::after"),
        (FE / "components" / "TopBar.css", "#topbar .tb-version::after"),
        (
            FE / "components" / "grid" / "Pane.css",
            ".prompt-pin .prompt-pin-mark::after",
        ),
    ):
        assert sel + " {" in _read(path), sel
