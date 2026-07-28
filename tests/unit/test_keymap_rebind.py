"""User-rebindable keyboard shortcuts: structural contract checks.

The "?" cheat-sheet doubles as the editor — click a rebindable row, press the
new combo — with per-row reset (↺) and a Restore-defaults button. Overrides
live per-device in localStorage ("mf_keymap"). Live behaviour verified
headlessly; these pin the JS/CSS hooks so they can't silently regress. Same
style as test_frontend_ux.py.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.web import server

client = TestClient(server.app)


def test_app_js_keymap_overrides_store():
    js = client.get("/app.js").text
    # Per-device persistence, defaults on garbage.
    assert '"mf_keymap"' in js
    assert "_saveKeyOv" in js
    # One resolver turns an entry + overrides into the effective triggers
    # (plural — an action can hold several combos); both the dispatcher and
    # the sheet go through it.
    assert "function effBindings(" in js
    assert "effBindings(" in js
    # "+" appends a combo, seeded from the defaults so they keep working.
    assert "function defaultCombosFor(" in js
    assert "shortcut-add" in js


def test_app_js_keymap_entries_carry_rebind_ids():
    js = client.get("/app.js").text
    # Primary bindings are addressable for overrides…
    for ident in (
        'id: "palette"',
        'id: "sidebar"',
        'id: "new"',
        'id: "cycle"',
        'id: "close"',
        'id: "reopen"',
        'id: "reload"',
    ):
        assert ident in js, ident
    # …browser-safe duplicates retire when their primary is customized, and
    # Shift-inverse partners follow the primary's custom combo.
    assert 'aliasOf: "new"' in js and 'aliasOf: "close"' in js
    assert 'pairOf: "cycle"' in js


def test_app_js_chords_rebind_second_key():
    js = client.get("/app.js").text
    # Chord actions stay keyed by their default letter; the effective second
    # key goes through one helper used by dispatch and the sheet alike.
    assert "function chordKeyFor(" in js
    assert "_keyOv.chords" in js


def test_app_js_sheet_is_the_editor():
    js = client.get("/app.js").text
    # Click-to-rebind capture (window-level, capture phase) + cancel path.
    assert "Press the new keys…" in js and "Press the combo to add…" in js
    assert "setRebindCapturing" in js
    # The dispatcher must stand down while a combo is being recorded.
    assert "setRebindCapturing" in js
    # Rejections: bare printable keys, Shift on paired actions, collisions.
    assert "function comboProblem(" in js
    # Restore paths: per-row reset and the restore-all button.
    assert "shortcut-reset" in js
    assert "shortcuts-restore" in js
    assert "Restore defaults" in js


def test_app_js_custom_bare_keys_guarded_while_typing():
    js = client.get("/app.js").text
    # A custom bare-key combo (F-key, Insert, …) never fires in inputs or
    # terminals — overrides can't inherit the default keys' editing guards.
    assert (
        "t !== b && !t.mod && !t.alt && isEditingTarget(document.activeElement)" in js
    )


def test_style_css_has_rebind_rules():
    css = client.get("/style.css").text
    for sel in (
        ".shortcut-row.rebindable",
        ".shortcut-row.capturing",
        ".shortcut-reset",
        ".shortcut-add",
        ".shortcut-controls",
        ".shortcuts-restore",
        ".shortcuts-hint",
    ):
        assert sel in css, sel
    # Rows are real boxes on shared tracks (subgrid), so the whole row can
    # hover-highlight as one clickable unit — not display:contents.
    assert "grid-template-columns: subgrid" in css
    assert ".shortcut-row.rebindable:hover" in css


def test_docs_describe_rebinding():
    text = open("docs/web-ui.md", encoding="utf-8").read()
    assert "Restore defaults" in text
    assert "localStorage" in text
