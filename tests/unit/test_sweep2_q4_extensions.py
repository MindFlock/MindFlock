"""UI sweep round 2, package Q4: the Database and To-do windows.

- #13a Typing in a Database column filter keeps the caret: every header
  rebuild (setData, setSort, setFilters — the debounced reload runs all
  three) puts focus back in the filter input, and keeps keys typed while
  the reload was in flight.
- #13b The SQL pad's CSV / JSON export shares Run's busy guard: a double-click
  runs one export, and an export can't overlap a Run.
- #35a To-do "Details…" clears the quick-add box only once the dialog adds;
  Escape or ✕ in the dialog leaves the text where it was.
- #35b Ctrl/Cmd+F focuses the To-do filter, from a text box too, and its
  placeholder says so.
"""

from __future__ import annotations

import re
from pathlib import Path

EXT = Path(__file__).resolve().parents[2] / "backend" / "web" / "static" / "extensions"


def _js(rel: str) -> str:
    return (EXT / rel).read_text(encoding="utf-8")


def _fn(src: str, name: str) -> str:
    """The body of ``function name(`` up to the next top-level function."""
    start = src.index("function " + name + "(")
    nxt = re.compile(r"\n  (?:async )?function \w+\(").search(src, start + 1)
    return src[start : nxt.start() if nxt else len(src)]


# --------------------------------------------------------------------------- #
# #13a — the filter keeps focus across every header rebuild
# --------------------------------------------------------------------------- #
def test_every_header_rebuild_keeps_the_filter_focus():
    grid = _js("dbclient/grid.js")
    head = _fn(grid, "renderHead")
    # Captured before the header is cleared, restored after it is rebuilt.
    assert head.index("const refocus = filterFocus();") < head.index(
        "thead.replaceChildren();"
    )
    assert head.index("restoreFilterFocus(refocus);") > head.index("renderCols();")
    # setSort / setFilters rebuild through renderHead, so they keep it too;
    # setData no longer carries its own copy of the step.
    for fn in ("setSort", "setFilters", "setData"):
        assert "renderHead();" in _fn(grid, fn), fn
    assert "refocus" not in _fn(grid, "setData")


def test_keys_typed_during_the_reload_survive_it():
    grid = _js("dbclient/grid.js")
    focus = _fn(grid, "filterFocus")
    assert "value: active.value" in focus
    restore = _fn(grid, "restoreFilterFocus")
    assert (
        "if (input.value !== at.value && !input.disabled) input.value = at.value;"
        in restore
    )
    assert restore.index("input.value = at.value") < restore.index("input.focus();")


def test_the_table_view_still_rebuilds_three_times_per_load():
    # The reason the step had to move: loadData calls all three in a row.
    load = _fn(_js("dbclient/tableview.js"), "loadData")
    order = [
        load.index(s) for s in ("grid.setData(", "grid.setSort(", "grid.setFilters(")
    ]
    assert order == sorted(order)


# --------------------------------------------------------------------------- #
# #13b — export goes through Run's busy guard
# --------------------------------------------------------------------------- #
def test_export_shares_runs_busy_guard():
    pad = _js("dbclient/querypad.js")
    busy = _fn(pad, "setBusy")
    assert "csvBtn.disabled = jsonBtn.disabled = on;" in busy
    exp = _fn(pad, "exportAtCursor")
    assert exp.index("if (running) return;") < exp.index("setBusy(true);")
    assert "await exportOne(stmt, s, format);" in exp
    assert "if (!disposed) setBusy(false);" in exp
    # The buttons are named, so setBusy can reach them.
    assert re.search(r'const csvBtn = button\("CSV",.*exportAtCursor\("csv"\)', pad)
    assert re.search(r'const jsonBtn = button\("JSON",.*exportAtCursor\("json"\)', pad)
    # A Run waits for an export as it does for another Run.
    assert "if (running) return;" in _fn(pad, "runAtCursor")
    assert "if (running) return;" in _fn(pad, "runAll")


# --------------------------------------------------------------------------- #
# #35a — Details… keeps the quick-add text until the dialog adds
# --------------------------------------------------------------------------- #
def test_details_clears_the_quick_add_only_when_the_dialog_adds():
    view = _js("todo/view.js")
    i = view.index('text: "Details…"')
    more = view[i : view.index("\n  });", i)]
    assert (
        'api.ui.openDialog("add", undefined, { text: moved.trim(), onAdded });' in more
    )
    assert 'if (!disposed && addInput.value === moved) addInput.value = "";' in more
    # Never cleared on open any more.
    assert not re.search(r'openDialog\([^)]*\);\s*addInput\.value = "";', more)

    add = _fn(_js("todo/add.js"), "add")
    # Called on success only — after the request, never in the failure branch.
    ok = add.index("ctx.onAdded();")
    assert (
        add.index('await call(api, "/api/todo/items"')
        < ok
        < add.index('api.log.error("add", err);')
    )


# --------------------------------------------------------------------------- #
# #35b — Ctrl+F focuses the filter
# --------------------------------------------------------------------------- #
def test_ctrl_f_focuses_the_todo_filter():
    view = _js("todo/view.js")
    assert 'placeholder: "Filter  ( Ctrl+F )",' in view
    key = _fn(view, "onKey")
    f = key.index('(e.key === "f" || e.key === "F")')
    # Before the text-entry early return, so it works from the quick-add box.
    assert f < key.index("if (isTextEntry(t)) return;")
    assert "(e.ctrlKey || e.metaKey)" in key[key.rindex("\n", 0, f) : f]
    block = key[f : key.index("return;", f)]
    assert "e.preventDefault();" in block and "filter.focus();" in block


def test_no_browser_dialogs_in_the_touched_windows():
    # Electron has no window.prompt / alert / confirm.
    for rel in (
        "dbclient/grid.js",
        "dbclient/querypad.js",
        "todo/view.js",
        "todo/add.js",
    ):
        src = _js(rel)
        assert not re.search(r"\b(?:window\.)?(?:prompt|alert|confirm)\(", src), rel
