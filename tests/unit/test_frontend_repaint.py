"""A repaint never resizes tmux: source-level pins (the vitest side is
frontend/src/__tests__/{repaintNoResize,paneRepaint}.test.ts).

The white-pane fix repainted every terminal on screen on focus / visibility /
pageshow / the desktop shell's `mindflock:repaint`, and each repaint FIT the
terminal first. `useWsTerm` (the Verify watcher) sent a resize frame on every
fit, changed or not; it attaches to a session the grid shows too, at another
size, and sessions run tmux `window-size latest`, so every alt-tab yanked the
window between the two sizes.

Pins: both `trackRepaint` entries redraw rows only (no fit, no resync);
`useWsTerm` sends a size only when it changed on that socket, records it only
once the socket is OPEN, and forgets it on a fresh connect; the paths that
carry a REAL size change (each ResizeObserver) still fit; the committed
bundle and the docs carry the same rules.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "frontend" / "src"


def _read(rel: str) -> str:
    return (_SRC / rel).read_text(encoding="utf-8")


def _code(src: str) -> str:
    """The source with its comments removed, so a comment that NAMES doFit
    (to say why it is not called) can't hide or fake a call."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(^|[^:\"'])//[^\n]*", r"\1", src)


def _tracked_repaints(src: str) -> list[str]:
    """The text of each `trackRepaint({ ... })` call in `src`, braces matched."""
    out: list[str] = []
    i = src.find("trackRepaint({")
    while i != -1:
        depth = 0
        j = src.index("{", i)
        while j < len(src):
            if src[j] == "{":
                depth += 1
            elif src[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        out.append(src[i : j + 1])
        i = src.find("trackRepaint({", j)
    return out


def _between(src: str, start: str, end: str) -> str:
    """`src` from `start` up to the next `end` (both must be there)."""
    i = src.index(start)
    return src[i : src.index(end, i)]


def _use_ws_term() -> str:
    """The code of `useWsTerm`, comments stripped."""
    src = _code(_read("lib/wsTerm.ts"))
    return src[src.index("export function useWsTerm(") :]


def _do_fit() -> str:
    return _between(
        _use_ws_term(), "const doFit = (sendResize: boolean)", "const connect = ()"
    )


# -- window-trigger repaints draw rows only ----------------------------------


def test_pane_repaint_entry_never_fits():
    entries = _tracked_repaints(_code(_read("components/grid/Pane.tsx")))
    assert len(entries) == 1
    assert "if (t) repaintTerm(t.term);" in entries[0]
    assert not re.search(r"doFit|resync|fitSoon", entries[0])


def test_use_ws_term_repaint_entry_never_fits():
    entries = _tracked_repaints(_use_ws_term())
    assert len(entries) == 1
    assert "repaint: () => repaintTerm(term)," in entries[0]
    assert "doFit" not in entries[0]


def test_repaint_term_without_a_fit_only_refreshes_rows():
    src = _code(_read("lib/wsTerm.ts"))
    body = _between(src, "export function repaintTerm(", "\n}\n")
    # The fit is optional, and nothing but it and the row rewrite touch the
    # terminal: called with no fit, a repaint is `refresh(0, rows - 1)` alone.
    assert "fit?: () => void" in body
    assert "fit?.();" in body
    assert "term.refresh(0, rows - 1);" in body
    assert not re.search(r"\bsend\(|resize|\.fit\(\)", body)


# -- real size changes still fit ---------------------------------------------


def test_pane_body_resize_observer_still_fits():
    src = _code(_read("components/grid/Pane.tsx"))
    # The only path left for a real size change: the pane-body observer runs
    # fitSoon, and fitSoon fits the shown terminal before it repaints.
    assert "const obs = new ResizeObserver(fitSoon);" in src
    assert "obs.observe(bodyRef.current);" in src
    fit_soon = _between(src, "const fitSoon = useCallback(", "}, [title, tab]);")
    assert "if (t) repaintTerm(t.term, () => t.doFit());" in fit_soon


def test_use_ws_term_resize_observer_fit_stays_outside_the_hub_entry():
    src = _use_ws_term()
    observer = _between(src, "const obs = new ResizeObserver(", "obs.observe(host);")
    assert re.search(r"doFit\(true\);\s*repaintTerm\(term\);", observer)
    # The observer's fit comes BEFORE the hub entry, not inside it.
    assert src.index("const obs = new ResizeObserver(") < src.index("trackRepaint({")
    assert "doFit" not in _tracked_repaints(src)[0]
    # The first fit after mount is kept too.
    assert "const t = setTimeout(() => doFit(true), 50);" in src


# -- useWsTerm sends a size only when it changed ------------------------------


def test_use_ws_term_guards_the_resize_frame_with_the_last_size_sent():
    body = _do_fit()
    assert 'const size = term.cols + "x" + term.rows;' in body
    assert "if (sentSize === size) return;" in body
    assert body.count('type: "resize"') == 1
    assert (
        body.index("if (sentSize === size) return;")
        < body.index("sentSize = size;")
        < body.index('type: "resize"')
    )
    # The wire format is unchanged: the same frame, only sent less often.
    assert (
        'ws.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));'
        in body
    )


def test_use_ws_term_records_the_size_only_once_the_socket_is_open():
    body = _do_fit()
    gate = "if (!sendResize || !ws || ws.readyState !== WebSocket.OPEN) return;"
    assert gate in body
    # A fit before the socket opens must not swallow the first resize after it.
    # (The only other `sentSize = ` in reach is the declaration after doFit.)
    assert re.findall(r"sentSize = [^;]*;", body) == [
        "sentSize = size;",
        'sentSize = "";',
    ]
    assert (
        body.index(gate)
        < body.index("sentSize === size")
        < body.index("sentSize = size;")
    )


def test_use_ws_term_forgets_the_size_on_a_fresh_socket():
    src = _use_ws_term()
    connect = _between(src, "const connect = ()", "sock.onopen")
    assert "ws = sock;" in connect
    assert 'sentSize = "";' in connect
    # ...and the open handler then sends the real one (the PTY spawns 24x80).
    onopen = _between(src, "sock.onopen = () => {", "sock.onmessage")
    assert "doFit(true);" in onopen
    # One socket-scoped variable, declared in the effect next to doFit.
    assert src.count('let sentSize = "";') == 1


# -- the committed bundle and the docs ----------------------------------------


def test_bundle_carries_the_rows_only_repaint_and_the_size_guard():
    js = (_ROOT / "backend" / "web" / "static" / "app.js").read_text(encoding="utf-8")
    entries = _tracked_repaints(js)
    assert len(entries) == 2
    for entry in entries:
        assert "repaintTerm(" in entry
        assert not re.search(r"doFit|resync", entry), entry
    # useWsTerm's guard, alongside the grid terminals' own.
    assert js.count("if (sentSize === size) return;") >= 2
    assert "repaint: () => repaintTerm(term)" in js
    assert "repaintTerm(term, () => doFit(true))" not in js


def test_docs_name_the_rule_and_its_vitest_file():
    doc = (_ROOT / "docs" / "web-ui.md").read_text(encoding="utf-8")
    assert "repaintNoResize.test.ts" in doc
    assert (_SRC / "__tests__" / "repaintNoResize.test.ts").is_file()
    assert "These window-trigger repaints redraw rows only" in doc
    assert "repainted (fit + a full `refresh`)" not in doc
