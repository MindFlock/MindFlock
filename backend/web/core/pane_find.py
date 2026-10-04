"""In-place find for a live terminal pane (Ctrl+F) — tmux history.

The browser terminals only mirror tmux's screen; scrolling back through a pane
is tmux copy-mode (see core/terminal.py). So "find and teleport" in the LIVE
pane reads tmux's whole history (one capture), matches it with the shared
find query (core/find_query: match case, whole word, regex, proximity — more
than tmux's own copy-mode search can do), and moves tmux's copy-mode view so
the hit sits mid-screen. The frontend paints the hits over the terminal
(FindHighlights), as it does for the scroll mode.

Panes whose app owns scrolling can't be searched this way. A TUI that grabs
the mouse (Claude Code, opencode, cline…) scrolls ITSELF on the wheel, so
tmux's history is never what the reader sees — and it is worse than stale:
every in-place redraw pushes a copy of the old frame into it, so a search
there finds the same line many times over, in frames that were never on
screen. Those get pane_scroll_find (scroll the app, read its screen) instead;
:func:`find_mode` picks, by the one rule that holds for every CLI: search
the scrollback the mouse wheel scrolls.
"""

from __future__ import annotations

import subprocess
from typing import Optional

from backend.web.core.find_query import Hit, Query, find_hits

# The tmux invocation; tests point it at a private server (``-L``).
TMUX = ["tmux"]

_CURSOR_FMT = (
    "#{pane_in_mode}\t#{history_size}\t#{scroll_position}"
    "\t#{copy_cursor_y}\t#{copy_cursor_x}"
)


def _tmux(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([*TMUX, *args], capture_output=True, text=True, timeout=10)


def _cursor(name: str) -> Optional[tuple[bool, int, int]]:
    """(in copy-mode, absolute cursor line counted from the oldest history
    line, cursor column), or None when tmux can't answer."""
    out = _tmux("display-message", "-p", "-t", name, _CURSOR_FMT)
    if out.returncode != 0:
        return None
    try:
        # rstrip("\n"), not strip(): out of copy-mode the cursor fields are
        # empty, and strip() would eat their tab separators.
        fields = out.stdout.rstrip("\n").split("\t")
        mode, hist, pos, cy, cx = (int(v or 0) for v in fields)
    except ValueError:
        return None
    return bool(mode), hist - pos + cy, cx


def _capture(name: str) -> Optional[list[str]]:
    # No -J: lines must stay one-per-grid-row so a line index is a row the
    # copy-mode cursor can report.
    out = _tmux("capture-pane", "-p", "-t", name, "-S", "-", "-E", "-")
    if out.returncode != 0:
        return None
    return out.stdout.split("\n")


def find_mode(name: str) -> Optional[str]:
    """How Ctrl+F searches this pane — whichever scrollback its wheel moves:

    * ``tmux``: the app left the mouse to tmux, so the wheel scrolls tmux's
      history — search it with copy-mode (this module);
    * ``scroll``: the app grabbed the mouse (any mode) and scrolls itself —
      drive it and read its screen (pane_scroll_find);
    * ``overlay``: alternate screen without the mouse (a pager, an editor):
      neither scrolls from here, so the history view's find.

    Asked of tmux, not of the provider, so it holds for every CLI — and for a
    TUI run in the shell tab. None when tmux can't answer."""
    # session_name too: for an unknown target tmux prints empty fields and
    # still exits 0.
    out = _tmux(
        "display-message",
        "-p",
        "-t",
        name,
        "#{session_name}\t#{mouse_any_flag}#{alternate_on}",
    )
    sess, _, flags = out.stdout.rstrip("\n").partition("\t")
    if out.returncode != 0 or not sess:
        return None
    if flags.startswith("1"):
        return "scroll"
    return "overlay" if flags == "01" else "tmux"


def close(name: str) -> None:
    """Back to the live screen (leave copy-mode, dropping its highlights)."""
    out = _tmux("display-message", "-p", "-t", name, "#{pane_in_mode}")
    if out.returncode == 0 and out.stdout.strip() == "1":
        _tmux("send-keys", "-t", name, "-X", "cancel")


# Per tmux session: the query being stepped through, its hits, the current one.
_steps: dict[str, tuple[Query, list[Hit], int]] = {}


def _view(name: str) -> Optional[tuple[bool, int, int, int]]:
    """(in copy-mode, history lines, scroll position, pane height)."""
    out = _tmux(
        "display-message",
        "-p",
        "-t",
        name,
        "#{session_name}\t#{pane_in_mode}\t#{history_size}\t#{scroll_position}\t#{pane_height}",
    )
    try:
        sess, mode, hist, pos, h = out.stdout.rstrip("\n").split("\t")
        if out.returncode != 0 or not sess:
            return None
        return mode == "1", int(hist or 0), int(pos or 0), int(h or 0)
    except ValueError:
        return None


def find(name: str, query, op: str) -> Optional[dict]:
    """Run one find step in tmux session ``name``'s active pane.

    ``query`` is a find_query.Query (a plain string means one with no
    options). ``op``: ``search`` (fresh query — lands on the newest hit at or
    above the bottom of the view), ``older`` / ``newer`` (step, wrapping), or
    ``close``. Returns ``{"total", "index", "row", "col", "len", "spans",
    "region"}`` (index 1-based, row on the pane's screen), ``{"status":
    "error"}`` for an invalid pattern, or None when the session is
    unreachable.
    """
    q = query if isinstance(query, Query) else Query(text=str(query or ""))
    if op == "close" or not q.text:
        _steps.pop(name, None)
        close(name)
        return {"total": 0, "index": 0}
    lines = _capture(name)
    view = _view(name)
    if lines is None or view is None:
        return None
    try:
        hits = find_hits(lines, q)
    except ValueError as err:
        return {"status": "error", "error": str(err), "total": 0, "index": 0}
    if not hits:
        _steps.pop(name, None)
        close(name)
        return {"total": 0, "index": 0}
    in_mode, hist, pos, h = view
    prev = _steps.get(name)
    if op == "search" or prev is None or prev[0] != q:
        # The bottom line of what's on screen now.
        bottom = hist - pos + h - 1 if in_mode else len(lines) - 1
        cur = -1
        for i, hit in enumerate(hits):
            if hit.line <= bottom:
                cur = i
        cur = max(cur, 0)
    else:
        cur = (prev[2] + (1 if op == "newer" else -1)) % len(hits)
    _steps[name] = (q, hits, cur)
    hit = hits[cur]
    # Top screen line = hist - pos. Leave the view alone when the hit is
    # already comfortably on it; otherwise put it mid-screen.
    top = hist - pos if in_mode else hist
    if not (top + 1 <= hit.line <= top + h - 2):
        want = max(0, min(hist, hist - (hit.line - h // 2)))
        if want > 0 and not in_mode:
            # -e: scrolling back down to the bottom returns to live, as with
            # a wheel-entered copy-mode.
            _tmux("copy-mode", "-e", "-t", name)
            in_mode, pos = True, 0
        if in_mode:
            delta = want - pos
            if delta > 0:
                _tmux("send-keys", "-t", name, "-X", "-N", str(delta), "scroll-up")
            elif delta < 0:
                _tmux("send-keys", "-t", name, "-X", "-N", str(-delta), "scroll-down")
        view = _view(name)
        if view is None:
            return None
        in_mode, hist, pos, h = view
        top = hist - pos if in_mode else hist
    row = hit.line - top
    spans = [[row, hit.col, hit.length]]
    if hit.partner is not None and 0 <= hit.partner[0] - top < h:
        spans.append([hit.partner[0] - top, hit.partner[1], hit.partner[2]])
    return {
        "total": len(hits),
        "index": cur + 1,
        "row": row,
        "col": hit.col,
        "len": hit.length,
        "spans": spans,
        "region": [0, max(0, h - 1)],
    }
