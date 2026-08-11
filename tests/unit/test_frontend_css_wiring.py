"""Every component stylesheet reaches the shipped bundle.

``frontend/src/styles/index.css`` is a hand-kept, ordered ``@import`` list (the
numbers in its comments ARE the cascade), rather than each component importing
its own CSS. That keeps the order explicit and reviewable, at the cost of one
failure mode with no natural alarm: write ``Foo.tsx`` and ``Foo.css``, forget
the ``@import`` line, and the component compiles, the build succeeds, CI stays
green — and the component renders completely unstyled.

That is exactly how the full-history overlay shipped: ``position: absolute;
inset: 0; z-index: 4`` never reached the page, so opening it produced an
invisible zero-size block, and every route into the feature (the drag gesture,
the header button, Ctrl+Up) looked broken.

Two guards, deliberately at different levels: the built ``/style.css`` really
carries the overlay's rules, and no source stylesheet is orphaned.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from backend.web import server

client = TestClient(server.app)

_FRONTEND_SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"

#: Sheets a component imports for itself (``import "./Hint.css"``), so they
#: legitimately never appear in the aggregator.
_SELF_IMPORTED = {"Hint.css", "WelcomeTour.css"}


def test_style_css_ships_the_history_overlay_rules():
    """The overlay must be positioned and painted, or it opens invisible."""
    css = client.get("/style.css").text
    assert ".hist-overlay" in css, "HistoryOverlay.css never reached the bundle"
    # The rules that make it a visible layer over the pane rather than an
    # unstyled block appended below the terminal.
    block = css[css.index(".hist-overlay") : css.index(".hist-overlay") + 400]
    assert "position: absolute" in block or "position:absolute" in block
    assert "inset:" in block.replace(" ", "") or "inset:0" in block.replace(" ", "")
    assert "z-index" in block
    # The scroller and its header ride along with it.
    assert ".hist-scroll" in css
    assert ".hist-bar" in css


def test_no_orphaned_component_stylesheet():
    """Each src/**/*.css is either in the aggregator or self-imported."""
    index = (_FRONTEND_SRC / "styles" / "index.css").read_text(encoding="utf-8")
    modules = "\n".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in _FRONTEND_SRC.rglob("*.ts*")
    )
    sheets = [
        p
        for p in _FRONTEND_SRC.rglob("*.css")
        if p != _FRONTEND_SRC / "styles" / "index.css"
    ]
    assert sheets, "no stylesheets found — the glob is wrong, not the wiring"

    orphans = sorted(
        str(p.relative_to(_FRONTEND_SRC))
        for p in sheets
        if p.name not in index
        and p.name not in _SELF_IMPORTED
        and ('"./%s"' % p.name) not in modules
    )
    assert orphans == [], "stylesheets nothing imports: " + ", ".join(orphans)
