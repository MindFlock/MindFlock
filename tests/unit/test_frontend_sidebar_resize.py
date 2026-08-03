"""Resizable sidebar + tighter session rows: structural contract checks.

Same style as test_frontend_wave4.py — the live UI is verified with
screenshots/CDP; these pin the markup hooks, JS wiring, and CSS rules so a
future refactor can't silently give the fixed-260px sidebar back.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.web import server

client = TestClient(server.app)


def _rule(css: str, selector: str) -> str:
    """The declarations of `selector`'s block (the bundle is unminified).

    Anchored at a line start so a short selector can't match the tail of a
    longer one (plain "body" would otherwise land in ".pane-body").
    """
    start = css.index("\n" + selector + " {")
    return css[start : css.index("}", start)]


# --------------------------------------------------------------------------- #
# The sidebar column is user-sized (Belisa: "allow me to resize this left bar")
# --------------------------------------------------------------------------- #


def test_body_grid_reads_the_sidebar_width_var():
    """The column width is a CSS var, not the old hard-coded 260px. The literal
    survives only as the pre-first-paint fallback."""
    css = client.get("/style.css").text
    assert "grid-template-columns: var(--sidebar-w, 260px) 1fr" in css
    # The old fixed column is gone from the page skeleton.
    assert "grid-template-columns: 260px 1fr" not in css


def test_sidebar_has_a_resize_handle():
    css = client.get("/style.css").text
    # The handle is positioned against the sidebar, so the sidebar anchors it.
    assert "position: relative" in _rule(css, "#sidebar")
    handle = _rule(css, ".sidebar-resizer")
    assert "cursor: col-resize" in handle
    assert "position: absolute" in handle
    # Overhangs into the grid's padding instead of covering the session list's
    # scrollbar, and paints above #grid (a later sibling).
    assert "left: 100%" in handle
    assert "z-index: 40" in handle


def test_resize_handle_is_wired_to_the_persisted_store():
    js = client.get("/app.js").text
    assert "sidebar-resizer" in js
    assert "setSidebarWidth" in js
    assert "mf_sidebar_w" in js  # survives a reload
    assert "--sidebar-w" in js
    # Keyboard-operable and announced, not mouse-only.
    assert '"Resize sidebar"' in js
    assert "aria-orientation" in js or "ariaOrientation" in js


def test_collapsed_sidebar_hides_the_handle():
    """Ctrl+B drops the whole column; a handle floating over the grid's left
    edge with no sidebar behind it would resize nothing."""
    css = client.get("/style.css").text
    assert "display: none" in _rule(css, "body.sidebar-collapsed .sidebar-resizer")


def test_drag_locks_the_cursor_and_selection():
    css = client.get("/style.css").text
    dragging = _rule(css, "body.sidebar-resizing,\nbody.sidebar-resizing *")
    assert "cursor: col-resize !important" in dragging
    assert "user-select: none !important" in dragging


# --------------------------------------------------------------------------- #
# Row density: more of the name fits on the line
# --------------------------------------------------------------------------- #


def test_row_title_is_smaller_than_body_text():
    """13px → 12px: the row carries a grip, a number, a dot, a chevron, the
    stage chip and ✕ alongside the name, and the name is what gets ellipsized."""
    css = client.get("/style.css").text
    assert "font-size: 12px" in _rule(css, ".inst .title")
    # The inline rename input must match, or the row height jumps mid-edit.
    assert "font-size: 12px" in _rule(css, ".inst input.title-edit")


def test_row_grip_is_hover_only_and_out_of_flow():
    """The row grip behaves exactly like the movable bars' .bar-grip: pinned to
    the hard left edge, absolutely positioned so it costs the row no width, and
    invisible until the row is hovered. The whole <li> is draggable, so hiding
    the grip at rest costs no function."""
    css = client.get("/style.css").text
    grip = _rule(css, ".inst .grip")
    assert "position: absolute" in grip
    assert "left: 1px" in grip
    assert "opacity: 0;" in grip  # not 0.35 — fully hidden at rest
    assert "opacity: 0.85" in _rule(css, ".inst-row:hover .grip")
    assert '"grip"' in client.get("/app.js").text


def test_row_reserves_a_lane_for_the_grip():
    """The grip fades in over the row's left padding, not over the Alt+N
    number, and the row anchors it (not .inst, which also holds the expanded
    actions menu)."""
    row = _rule(client.get("/style.css").text, ".inst-row")
    assert "padding: 9px 8px 9px 12px" in row
    assert "position: relative" in row


def test_sidebar_pills_size_to_content_not_a_fixed_cap():
    """A pill is never cut while the row still has room: the sidebar chips drop
    the base 96px cap and size to their content. They stay shrinkable, which is
    what keeps the row from overflowing — the title (far wider, same shrink
    factor) gives way first and stops at its floor."""
    css = client.get("/style.css").text
    chip = _rule(css, ".inst .stagechip")
    assert "max-width: none" in chip
    assert "flex: 0 1 auto" in chip
    assert "min-width: 0" in chip
    # The shared base still caps chips OUTSIDE the sidebar (pane headers).
    assert "max-width: 96px" in _rule(css, ".stagechip")
    # If a pill does have to shrink, it ellipsizes rather than hard-clipping.
    base = _rule(css, ".stagechip")
    assert "text-overflow: ellipsis" in base
    assert "white-space: nowrap" in base


def test_row_title_has_no_javascript_character_budget():
    """sessionLabel used to clip the feature name at 20 chars, which put a "…"
    in names that had room to spare once the sidebar could be widened. Only CSS
    knows the current width, so only CSS truncates."""
    js = client.get("/app.js").text
    assert "MAX_NAME" not in js
    title = _rule(client.get("/style.css").text, ".inst .title")
    assert "text-overflow: ellipsis" in title
    assert "overflow: hidden" in title


def test_grip_matches_the_bar_grip_pattern():
    """If the bars' grip stops being hover-only, the rows should follow — this
    pins the two together so they can't drift apart visually."""
    bar = _rule(client.get("/style.css").text, ".bar-grip")
    assert "position: absolute" in bar
    assert "opacity: 0;" in bar
