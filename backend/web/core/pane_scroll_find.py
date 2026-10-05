"""Find (Ctrl+F) for panes whose app scrolls ITSELF — index once, then jump.

A TUI that grabs the mouse (Claude Code, opencode, cline…) owns its
scrollback: the wheel scrolls the app, and tmux's history holds only redraw
debris (see pane_find). The one thing every such app shares is the wheel and
the screen it draws, so that is all this uses — no per-provider knowledge.

Scrolling a normal-height screen at a time was far too slow (and visible), so
a find first builds an INDEX of everything the app would show, hidden from
the reader (the browser freezes its view while a step runs):

1. run to the bottom, then make the window very TALL at the same width (so
   nothing re-wraps) — an app draws as many rows as the window has: Claude
   Code honours up to 2048 and ignores taller, so heights are tried
   high-to-low until one takes;
2. nudge a notch or three: Claude Code draws only its recent messages while
   parked at the bottom and fills the whole window once the view leaves it;
3. read the screen — for most conversations that is ALL of it — and sweep
   upward in tall screens when it isn't, each move measured by diffing
   screens (apps accelerate wheel bursts non-linearly, so nothing about a
   step's size is assumed; an overshoot is walked back);
4. put the window back.

Only rows seen MOVING with the content make the index: the input box and a
status bar stand still, a pinned prompt row and a floating hint ("Jump to
bottom") change in place — none of them is content, whatever they contain.

With the index, counting is instant ("3 / 10" before anything moves) and a
step is a jump: locate the current screen in the index by its distinctive
lines, move toward the target, locate again. Absolute positioning needs no
overlap between screens, so the moves can be as big as they like.

Wheel scrolling is ANIMATED in Claude Code (a burst glides over 100–400 ms,
~1–2k lines/s at best), so where the app pages with the keyboard —
PageUp/PageDown by half a window, Ctrl+Home/Ctrl+End to the ends, no
animation — the moves use those instead. Keys aren't universal, so a pane's
app earns them by one measured PageUp that scrolled its content; until then,
and for apps where it didn't, everything goes by the wheel.
"""

from __future__ import annotations

import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from backend.web.core import pane_find as _pf
from backend.web.core.find_query import Query, find_hits

POLL_S = 0.01  # screen poll interval while waiting for a redraw
SETTLE_MAX_S = 1.0  # a pane that never stops changing (streaming): go on anyway
TALL_TRY = (2048, 1000, 400, 150)  # window heights to index at, tallest first
INDEX_BUDGET_S = 30.0
NAV_MAX_STEPS = 24
MIN_VOTES = 3  # distinctive lines that must agree on an offset
MARGIN = 2  # keep a landed hit at least this far from the region's edges
JUMP_TICKS = 120  # burst size when running to an end without reading
REDRAW_S = 0.15  # how long an app gets to react to one notch
MAX_CHROME = 10  # rows at the bottom (input box, status) that can be chrome

Hit = tuple[int, int]  # (index line, column)


def _tmux(*args: str):
    return _pf._tmux(*args)


# --------------------------------------------------------------------------- #
# Pure screen logic
# --------------------------------------------------------------------------- #


def find_row_hits(lines: list[str], query: str, rows: range) -> list[Hit]:
    """``(row, col)`` of every hit in ``rows`` of ``lines``, top-down, under
    the same smart-case rule as the tmux path (all-lowercase ignores case)."""
    if not query:
        return []
    fold = query == query.lower()
    q = query.lower() if fold else query
    out: list[Hit] = []
    for r in rows:
        if not 0 <= r < len(lines):
            continue
        hay = lines[r].lower() if fold else lines[r]
        j = hay.find(q)
        while j >= 0:
            out.append((r, j))
            j = hay.find(q, j + 1)
    return out


def _unique(rows: list[str]) -> dict[str, int]:
    """Non-blank rows that occur exactly once → their position."""
    n = Counter(r for r in rows if r.strip())
    return {r: i for i, r in enumerate(rows) if r.strip() and n[r] == 1}


def measure(a: list[str], b: list[str]) -> tuple[int, int]:
    """How far content moved from screen ``a`` to ``b``: ``(d, votes)``,
    ``d > 0`` when it moved DOWN (the app scrolled up). Distinctive lines vote
    for their displacement; rows that didn't move (chrome) don't vote. Linear
    in the screen height — screens here are 2000 rows tall."""
    ua, ub = _unique(a), _unique(b)
    votes = Counter(j - ua[t] for t, j in ub.items() if t in ua and j != ua[t])
    if not votes:
        return 0, 0
    d, v = votes.most_common(1)[0]
    return d, v


def stood_still(a: list[str], b: list[str]) -> bool:
    """True when ``b`` is ``a`` without a scroll: identical, or nearly so with
    no offset that explains the difference (a clock or spinner ticking)."""
    if a == b:
        return True
    rows = [r for r in range(min(len(a), len(b))) if a[r].strip() or b[r].strip()]
    if not rows:
        return True
    same = sum(1 for r in rows if a[r] == b[r])
    return same >= 0.8 * len(rows) and measure(a, b)[1] < MIN_VOTES


def moved_region(a: list[str], b: list[str], d: int) -> Optional[tuple[int, int]]:
    """The scrolling region on ``b`` after content moved by ``d``: the largest
    cluster of non-blank rows that moved with it (blank lines inside content
    can't prove anything, so gaps are allowed), widened by ``d`` at the edge
    new content came in through."""
    n = min(len(a), len(b))
    rows = sorted(
        j
        for j in range(max(0, d), min(n, n + d))
        if b[j].strip() and b[j] == a[j - d] and b[j] != a[j]
    )
    if not rows:
        return None
    groups: list[list[int]] = [[rows[0]]]
    for r in rows[1:]:
        if r - groups[-1][-1] <= 12:
            groups[-1].append(r)
        else:
            groups.append([r])
    g = max(groups, key=len)
    lo, hi = g[0], g[-1]
    if d > 0:
        lo -= d
    else:
        hi -= d
    return max(0, lo), min(n - 1, hi)


def bottom_chrome(normal: list[str], tall: list[str]) -> int:
    """Rows at the bottom that are the same at both heights, bottom-aligned —
    the input box and status bar every such app pins there. Used only when
    nothing could scroll (the whole conversation fits), so motion can't tell
    chrome from content; capped, since content never sits there at both
    heights once the tall one has room to show more."""
    k = 0
    while (
        k < min(MAX_CHROME, len(normal), len(tall))
        and normal[-1 - k] == tall[-1 - k]
        and normal[-1 - k].strip()
    ):
        k += 1
    # Blank separator rows between chrome rows count too.
    while (
        k < min(MAX_CHROME, len(normal), len(tall))
        and not normal[-1 - k].strip()
        and not tall[-1 - k].strip()
    ):
        k += 1
        while (
            k < min(MAX_CHROME, len(normal), len(tall))
            and normal[-1 - k] == tall[-1 - k]
            and normal[-1 - k].strip()
        ):
            k += 1
    return k


def build_index(screens: list[list[str]], ds: list[int], lo: int, hi: int) -> list[str]:
    """Stitch a sweep into the document it shows, top line first.

    ``screens[i+1]`` is ``screens[i]`` with the content moved by ``ds[i]``,
    so every screen row maps to a document line, and — steps being at most a
    little over half a screen — nearly every line is seen at least twice, in
    different rows. The document is the consensus of those sightings:

    * a blank sighting never outweighs a drawn one (Claude Code leaves rows
      blank while parked at an end, and draws rows entering the view a frame
      late);
    * when drawn sightings disagree, the screen rows they came from are
      marked; a row that disagrees nearly every time it's compared is not
      content — a pinned prompt, a floating hint ("Jump to bottom"), chrome
      inside the region — and its sightings lose. (A content row paired with
      such a row disagrees only on those pairings.)

    No text heuristics: only where the sightings came from."""
    tops = [0]
    for d in ds:
        tops.append(tops[-1] - d)
    seen: dict[int, list[tuple[int, int, str]]] = {}  # line -> (screen, row, text)
    for i, sc in enumerate(screens):
        for r in range(lo, min(hi, len(sc) - 1) + 1):
            seen.setdefault(tops[i] + (r - lo), []).append((i, r, sc[r]))
    if not seen:
        return []
    compared: Counter = Counter()
    clashed: Counter = Counter()
    for obs in seen.values():
        drawn = [(r, t) for _i, r, t in obs if t.strip()]
        for x in range(len(drawn)):
            for y in range(x + 1, len(drawn)):
                (ra, ta), (rb, tb) = drawn[x], drawn[y]
                compared[ra] += 1
                compared[rb] += 1
                if ta != tb:
                    clashed[ra] += 1
                    clashed[rb] += 1
    bad = {r for r, n in compared.items() if n >= 3 and clashed[r] >= 0.8 * n}
    lines: list[str] = []
    for pos in range(min(seen), max(seen) + 1):
        obs = seen.get(pos, [])
        drawn = [(i, r, t) for i, r, t in obs if t.strip()]
        good = [o for o in drawn if o[1] not in bad]
        pool = good or ([] if drawn and all(o[1] in bad for o in drawn) else drawn)
        if not pool:
            lines.append("")
            continue
        votes = Counter(t for _i, _r, t in pool)
        top_n = votes.most_common(1)[0][1]
        # Ties: trust the sighting from the screen row that disagrees least
        # often overall (a row that's pinned only once scrolled clashes a
        # lot without crossing the line above), then the latest screen.
        rate = lambda r: clashed[r] / compared[r] if compared[r] else 0.0  # noqa: E731
        best = min(
            (o for o in pool if votes[o[2]] == top_n), key=lambda o: (rate(o[1]), -o[0])
        )
        lines.append(best[2])
    # Blank rows below the last line of content are the gap a short
    # conversation leaves above the input box, not content.
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def locate(index: dict[str, int], screen: list[str], lo: int, hi: int) -> Optional[int]:
    """Which index line sits at the region's top row of ``screen``: each
    distinctive line votes for the offset it implies. None when too few
    agree (the view shows something the index never saw)."""
    votes = Counter(
        index[screen[r]] - (r - lo)
        for r in range(lo, min(hi, len(screen) - 1) + 1)
        if screen[r] in index
    )
    if not votes:
        return None
    top, v = votes.most_common(1)[0]
    return top if v >= min(MIN_VOTES, max(1, len(votes))) else None


def pick_start(hits: list, anchor: int) -> int:
    """Index of the first hit to land on: the last one at or above line
    ``anchor`` (the bottom of what the reader was looking at — terminal
    readers want the most recent), else the first below it. Hits are
    find_query.Hit or (line, col) pairs."""
    best = -1
    for i, h in enumerate(hits):
        ln = h[0] if isinstance(h, tuple) else h.line
        if ln <= anchor:
            best = i
    return best if best >= 0 else 0


def burst_ticks(seen: dict[int, int], n: int, lpn: float) -> int:
    """Notches to send in one burst to move about ``n`` lines, from ``seen``
    (burst size → lines it moved). Bracketed between the biggest burst known
    to fall short and the smallest known to overshoot: apps accelerate a
    burst, so lines grow faster than notches and scaling up from a small burst
    alone over-promises — a burst already seen to overshoot is never resent."""
    under = [k for k, d in seen.items() if d <= n]
    over = [k for k, d in seen.items() if d > n]
    lo = max(under) if under else 0
    if lo:
        ticks = min(lo * 3, max(1, int(lo * n / max(1, seen[lo]))))
    else:
        ticks = max(1, int(n / lpn / 3))
    hi = min(over) if over else 0
    if hi and ticks >= hi:
        if lo >= hi:  # noisy rates (a swallowed notch): trust the overshoot
            lo = 0
        lo_d = seen[lo] if lo else 0
        ticks = lo + int((hi - lo) * (n - lo_d) / max(1, seen[hi] - lo_d))
        ticks = max(1, lo, min(ticks, hi - 1))
    return ticks


# --------------------------------------------------------------------------- #
# Driving a pane
# --------------------------------------------------------------------------- #


@dataclass
class _Index:
    lines: list[str]
    lookup: dict[str, int]
    lo: int  # region top row (rows above it are chrome at any height)
    chrome_below: int  # rows below the region at any height
    width: int
    lpn: float  # lines one lone notch moves
    rates: list[tuple[int, int]]  # (notches in a burst, lines it moved)
    origin_bottom: int  # index line at the bottom of the reader's view at index time
    max_top: int  # index line at the region's top when the view is at the bottom
    bottom_screen: Optional[list[str]] = (
        None  # the bottom view, to tell if the pane changed since
    )
    stale: bool = False  # the pane moved on in a way refresh() couldn't follow

    def region(self, h: int) -> tuple[int, int]:
        return self.lo, max(self.lo, h - 1 - self.chrome_below)


@dataclass
class _State:
    index: Optional[_Index] = None
    query: Optional[Query] = None
    hits: list = field(default_factory=list)  # find_query.Hit
    cur: int = -1
    lock: threading.Lock = field(default_factory=threading.Lock)
    cancel: threading.Event = field(default_factory=threading.Event)
    background: bool = False  # a background index is running (see background_index)
    seen: Optional[list[str]] = None  # last screen observe() saw, and since when
    seen_at: float = 0.0


_states: dict[str, _State] = {}
_states_lock = threading.Lock()
# Does this pane's app page with PageUp/PageDown/Ctrl+Home/Ctrl+End? None =
# not measured yet. Per tmux session, kept across finds.
_keys_ok: dict[str, Optional[bool]] = {}


def _state(name: str) -> _State:
    with _states_lock:
        st = _states.get(name)
        if st is None:
            st = _states[name] = _State()
        return st


class _Cancelled(Exception):
    pass


class _Pane:
    def __init__(self, name: str, cancel: threading.Event):
        self.name = name
        self.cancel = cancel
        self.h = self.w = self.win_h = 0
        self.sgr = True

    def grabbed(self) -> bool:
        """Refresh geometry; True while the app still has a mouse mode on —
        wheel reports written into a pane whose app doesn't expect them would
        land in its input as garbage, so every burst re-checks."""
        out = _tmux(
            "display-message",
            "-p",
            "-t",
            self.name,
            "#{session_name}\t#{pane_height}\t#{pane_width}\t#{window_height}"
            "\t#{mouse_any_flag}\t#{mouse_sgr_flag}\t#{pane_in_mode}",
        )
        try:
            sess, h, w, wh, anyf, sgr, mode = out.stdout.rstrip("\n").split("\t")
            if out.returncode != 0 or not sess:
                return False
            self.h, self.w, self.win_h = int(h), int(w), int(wh)
        except ValueError:
            return False
        self.sgr = sgr == "1"
        if mode == "1":
            # tmux copy-mode would swallow the reports; the app is what scrolls.
            _tmux("send-keys", "-t", self.name, "-X", "cancel")
        return anyf == "1"

    def screen(self) -> list[str]:
        out = _tmux("capture-pane", "-p", "-t", self.name)
        rows = out.stdout.split("\n") if out.returncode == 0 else []
        rows = rows[: self.h] if self.h else rows
        return rows + [""] * max(0, self.h - len(rows))

    def settle(
        self,
        prev: Optional[list[str]] = None,
        wait: float = SETTLE_MAX_S,
        calm: int = 2,
    ) -> list[str]:
        """The screen once it stops changing (``calm`` identical polls). With
        ``prev``, first give the app up to ``wait`` to change away from it (a
        redraw in flight)."""
        last = self.screen()
        changed = prev is None or last != prev
        stable = 0
        t0 = time.monotonic()
        while time.monotonic() - t0 < wait:
            if self.cancel.is_set():
                raise _Cancelled
            time.sleep(POLL_S)
            cur = self.screen()
            if cur == last:
                stable += 1
                # Waiting on a change that may never come (the end, a
                # swallowed notch): don't burn the whole wait on it.
                if stable >= calm and (changed or stable >= 10):
                    break
            else:
                stable = 0
                last = cur
                changed = True
        return last

    def wheel(self, up: bool, n: int) -> None:
        if n <= 0:
            return
        if self.cancel.is_set():
            raise _Cancelled
        if not self.grabbed():
            raise RuntimeError("the pane's app released the mouse")
        x = max(1, self.w // 2)
        y = max(1, self.h // 2)
        btn = 64 if up else 65
        if self.sgr:
            seq = f"\x1b[<{btn};{x};{y}M"
        else:
            seq = (
                "\x1b[M" + chr(32 + btn) + chr(32 + min(x, 223)) + chr(32 + min(y, 223))
            )
        _tmux("send-keys", "-t", self.name, "-l", seq * n)

    def keys(self, key: str, n: int = 1) -> None:
        if n <= 0:
            return
        if self.cancel.is_set():
            raise _Cancelled
        _tmux("send-keys", "-t", self.name, *([key] * n))

    def resize(self, h: int) -> None:
        _tmux("resize-window", "-t", self.name, "-y", str(h))
        self.grabbed()

    def restore(self, h: int) -> None:
        before = self.screen()
        _tmux("resize-window", "-t", self.name, "-y", str(h))
        _tmux("set-option", "-w", "-t", self.name, "window-size", "latest")
        self.grabbed()
        # Wait out the redraw at the old height: until it lands, the pane
        # still shows (the top of) the tall frame, and a locate would place
        # the view wrongly. Putting the window back is never cancelled.
        try:
            self.settle(before[: self.h], wait=0.4)
        except _Cancelled:
            pass


def _nudge(
    p: _Pane, scr: list[str], up: bool, tries: int = 3
) -> tuple[list[str], Optional[int]]:
    """Single notches until the view moves: ``(screen, d)`` — d=0 when it
    never did, None when the screen changed in a way no offset explains (a
    full redraw, not a scroll). "Didn't move" takes several notches to
    believe: apps swallow the first notch after a burst the other way (Claude
    Code cancels its momentum with it)."""
    new = scr
    for _ in range(tries):
        p.wheel(up, 1)
        new = p.settle(scr, wait=REDRAW_S)
        if not stood_still(scr, new):
            d, v = measure(scr, new)
            return new, (d if v >= MIN_VOTES and d else None)
    return new, 0


def _run_to_end(p: _Pane, up: bool, deadline: float) -> list[str]:
    scr = p.settle()
    while time.monotonic() < deadline:
        p.wheel(up, JUMP_TICKS)
        new = p.settle(scr, wait=REDRAW_S)
        if stood_still(scr, new):
            new, d = _nudge(p, new, up, tries=2)
            if d == 0:
                return new
        scr = new
    return scr


def _go_tall(p: _Pane, normal_h: int, bottom: list[str]) -> Optional[int]:
    """Make the window as tall as the app will draw. Returns the height, or
    None when it redraws at none of them (index at the normal height)."""
    for h in TALL_TRY:
        if h <= normal_h:
            break
        p.resize(h)
        if p.h != h:
            continue
        # A resize is taken up in stages (tmux reflows, the app redraws, and
        # Claude Code keeps paging by its OLD height for a beat), so wait for
        # real calm, not the first frame.
        scr = p.settle(bottom, wait=0.6, calm=8)
        # Redrawn at the new height: its bottom chrome moved to the new
        # bottom. An app that ignored the resize left it where it was and
        # tmux padded the rest with blank rows.
        low = max((i for i, r in enumerate(scr) if r.strip()), default=-1)
        if low >= h - MAX_CHROME:
            return h
    p.restore(normal_h)
    return None


def _to_bottom(p: _Pane, deadline: float) -> list[str]:
    if _keys_ok.get(p.name):
        before = p.screen()
        p.keys("C-End")
        return p.settle(before, wait=REDRAW_S)
    return _run_to_end(p, False, deadline)


def _probe_keys(p: _Pane) -> bool:
    """Does this app page with the keyboard? One PageUp at the normal height,
    measured, then undone — about 20 ms where it works. A yes is kept for the
    pane; a no is tried again next time (it may have met the app mid-redraw,
    re-wrapping after a resize), so one unlucky probe can't cost the fast
    path for good."""
    if _keys_ok.get(p.name):
        return True
    for _ in range(2):
        before = p.settle(calm=3)
        p.keys("PPage")
        new = p.settle(before, wait=REDRAW_S)
        d, v = measure(before, new)
        ok = v >= MIN_VOTES and d > 0
        if not stood_still(before, new):
            p.keys("NPage" if ok else "C-End")
            p.settle(new, wait=REDRAW_S)
        if ok:
            _keys_ok[p.name] = True
            return True
    _keys_ok[p.name] = False
    return False


def _sweep_keys(
    p: _Pane, deadline: float
) -> tuple[list[list[str]], list[int], int, int, bool, int]:
    """Read the whole scrollback top-down by PageDown (half a window each, no
    animation), from Ctrl+Home to the bottom. Returns (screens, ds, lo,
    below, complete, strict lo) — complete only when the bottom was really
    reached; the strict lo keeps a pinned row out of locating and painting."""
    top_before = p.screen()
    p.keys("C-Home")
    cur = p.settle(top_before, wait=0.5, calm=4)
    # After a jump Claude Code draws only a little around it (the banner, at
    # the top); one notch makes it draw the whole window. Either way the
    # sweep starts from a full drawing a line or so below the very top.
    # Two lone notches DOWN, never reversing (a notch the other way is
    # swallowed by the momentum of the last). Together they confirm every
    # row of the top screen — the rows above the next page's reach have no
    # other witness — and the lazy screen, when its few rows can be
    # measured, confirms the very first line.
    screens: list[list[str]] = []
    ds: list[int] = []
    one, d1 = _nudge(p, cur, False, tries=2)
    if d1:
        screens, ds = [cur, one], [d1]
    elif d1 is None:
        screens = [one]
    cur = one
    if d1 != 0:
        two, d2 = _nudge(p, one, False, tries=2)
        if d2 and d2 < 0:
            screens.append(two)
            ds.append(d2)
        elif d2 is None:
            screens, ds = [two], []
        cur = two if d2 != 0 else one
    if not screens:
        screens = [cur]
    regs: list[tuple[int, int]] = []
    complete = False
    while time.monotonic() < deadline:
        p.keys("NPage")
        new = p.settle(cur, wait=0.5, calm=3)
        if stood_still(cur, new):
            # The bottom — or a key the app was too busy to take: Ctrl+End
            # changing nothing either settles it.
            p.keys("C-End")
            end = p.settle(new, wait=REDRAW_S, calm=2)
            if stood_still(new, end):
                complete = True
                # The bottom puts Claude Code back into its lazy drawing: the
                # rows above its last few messages go blank, and lines only
                # this screen shows were never drawn. One notch up draws the
                # whole window; read that too, then return to the bottom.
                up, du = _nudge(p, end, True, tries=2)
                if du and du > 0:
                    screens.append(up)
                    ds.append(du)
                elif du is None and screens:
                    d_prev, v_prev = measure(cur, up)
                    if v_prev >= MIN_VOTES and d_prev:
                        screens.append(up)
                        ds.append(d_prev)
                if du != 0:
                    p.keys("C-End")
                    p.settle(up, wait=REDRAW_S, calm=2)
                break
            d, v = measure(new, end)
            if v < MIN_VOTES or d >= 0:
                break
            new = end
        else:
            d, v = measure(cur, new)
            if v < MIN_VOTES or d >= 0:
                break  # nothing measurable: stop rather than stitch a guess
        reg = moved_region(cur, new, d)
        # Only a move seen across most of the screen says where the region
        # is: landing on the bottom puts Claude Code back into its lazy
        # drawing, and that last pair only moves the few rows it draws.
        if reg and reg[1] - reg[0] >= len(cur) // 2:
            regs.append(reg)
        screens.append(new)
        ds.append(d)
        cur = new
    if not regs:
        # Nothing scrolled: one screen holds it all. Top and bottom are the
        # same place then, so the notches down above may have parked Claude
        # Code in its lazily drawn bottom view; a notch up leaves it. Keep
        # whichever drawing shows more.
        up, _d = _nudge(p, cur, True, tries=1)
        best = max((cur, up), key=lambda sc: sum(1 for x in sc if x.strip()))
        return [best], [], 0, -1, complete, 0
    # Read wide (the very top of the scrollback has content on row 0; motion
    # filters a pinned row out of the stitching anyway) but locate and paint
    # by the usual top: a row that's pinned whenever the view is scrolled —
    # Claude Code's prompt — isn't content.
    lo = min(r[0] for r in regs)
    lo_strict = Counter(r[0] for r in regs).most_common(1)[0][0]
    hi = max(r[1] for r in regs)
    return screens, ds, lo, len(cur) - 1 - hi, complete, lo_strict


def _sweep_wheel(
    p: _Pane, cur: list[str], lo: int, hi: int, deadline: float
) -> tuple[list[list[str]], list[int], list[tuple[int, int]], bool]:
    """Read upward to the top by wheel bursts, each move measured. Returns
    (screens, ds, (burst, lines) rates, complete).

    A burst may move at most 60% of a screen — past that some lines are seen
    only once, and a sighting under a floating hint is no sighting. Apps
    accelerate bursts unpredictably, so an overshoot is walked back one lone
    notch at a time (lone notches don't accelerate) until the screens overlap
    again, and the bursts shrink."""
    height = hi - lo + 1
    limit = max(1, int(0.6 * height))
    screens: list[list[str]] = []
    ds: list[int] = []
    rates: list[tuple[int, int]] = []
    ticks = 4
    while time.monotonic() < deadline:
        p.wheel(True, ticks)
        new = p.settle(cur, wait=0.5)
        if stood_still(cur, new):
            new, d = _nudge(p, new, True, tries=2)
            if d == 0:
                return screens, ds, rates, True
            v = MIN_VOTES if d is not None else 0
        else:
            d, v = measure(cur, new)
        if v < MIN_VOTES or d is None or d <= 0 or d > limit:
            ticks = max(1, ticks // 2)
            back = False
            for _ in range(4 * height + 10):
                if time.monotonic() > deadline:
                    break
                p.wheel(False, 1)
                new = p.settle(new, wait=REDRAW_S)
                if new == cur or stood_still(cur, new):
                    back, d = True, 0
                    break
                d, v = measure(cur, new)
                if v >= MIN_VOTES and 0 < d <= limit:
                    back = True
                    break
            if not back:
                return screens, ds, rates, False
            if d == 0:
                continue
        else:
            rates.append((ticks, d))
            ticks = max(1, min(int(0.5 * height / max(0.5, d / ticks)), ticks * 2))
        screens.append(new)
        ds.append(d)
        cur = new
    return screens, ds, rates, False


def _index(p: _Pane, st: Optional[_State], _again: bool = True) -> _Index:
    """Build the index (see the module docstring), leaving the window at its
    normal height and the view back where the reader had it. Built at one
    width or not at all: if the window came back a different width (tmux
    handing sizing to a client that attached a moment ago), the lines are of
    a layout that is gone — build again at the new one."""
    width = p.w
    idx = _index_once(p, st)
    if _again and p.grabbed() and p.w != width:
        return _index(p, st, _again=False)
    return idx


def _index_once(p: _Pane, st: Optional[_State]) -> _Index:
    deadline = time.monotonic() + INDEX_BUDGET_S
    normal_h = p.win_h or p.h
    # Still for a moment first: an app re-wrapping after a width change
    # redraws in bursts, and an index taken through that is of a layout that
    # is about to disappear.
    origin = p.settle(calm=10)
    if _probe_keys(p):
        return _index_by_keys(p, origin, normal_h, deadline)
    bottom = _to_bottom(p, deadline)
    tall_h = _go_tall(p, normal_h, bottom)
    try:
        s0 = p.settle()
        screens = [s0]
        ds: list[int] = []
        rates: list[tuple[int, int]] = []
        lone: list[int] = []
        region: Optional[tuple[int, int]] = None
        # Lone notches: out of the bottom's lazy drawing, a measurement of
        # what one notch does, and the region with its chrome. A row counts
        # only once a neighbouring screen confirms it, so the first screen
        # of the full drawing needs a second one.
        cur = s0
        for _ in range(5):
            new, d = _nudge(p, cur, True)
            if d == 0:
                break
            if d is not None and 0 < d <= 6:
                screens.append(new)
                ds.append(d)
                lone.append(d)
                reg = moved_region(cur, new, d)
                if reg and (
                    region is None or reg[1] - reg[0] >= region[1] - region[0] - 2
                ):
                    region = reg
            else:
                # Redrawn, not scrolled: Claude Code leaving its lazily drawn
                # bottom view (content that fits the window re-anchors to
                # the top). Start over from the full drawing.
                screens, ds, lone, region = [new], [], [], None
            cur = new
            if len(ds) >= 2:
                break
        if region is None:
            # Nothing scrolls: the whole conversation is on this screen.
            lo = 0
            below = bottom_chrome(bottom, cur)
            screens, ds = [cur], []
        else:
            lo, hi = region
            below = len(cur) - 1 - hi
            more, mds, rates, complete = _sweep_wheel(p, cur, lo, hi, deadline)
            if not complete:
                raise RuntimeError("couldn't read the whole scrollback")
            screens += more
            ds += mds
            if more:
                cur = more[-1]
        lines = build_index(screens, ds, lo, len(cur) - 1 - below)
        # Back down while still tall: the reader's view was almost always the
        # bottom, and nothing should look moved.
        _to_bottom(p, deadline)
    finally:
        if tall_h is not None:
            p.restore(normal_h)
    return _finish(
        p,
        lines,
        lo,
        below,
        max(1.0, sum(lone) / len(lone)) if lone else 1.0,
        rates,
        origin,
        deadline,
    )


def _index_by_keys(
    p: _Pane, origin: list[str], normal_h: int, deadline: float
) -> _Index:
    """The keyboard path: tall window, Ctrl+Home, PageDown to the bottom —
    the sweep ends where the reader almost always was."""
    before = p.screen()
    tall_h = _go_tall(p, normal_h, before)
    try:
        for _attempt in range(2):
            screens, ds, lo, below, complete, lo_strict = _sweep_keys(p, deadline)
            if complete:
                break
        else:
            # Paging didn't reliably reach the end here: the wheel instead.
            _keys_ok[p.name] = False
            if tall_h is not None:
                p.restore(normal_h)
                tall_h = None
            return _index(p, None)
        if below < 0:
            # Nothing scrolls: one screen holds it all.
            below = bottom_chrome(origin, screens[0])
            lo = 0
        lines = build_index(screens, ds, lo, len(screens[-1]) - 1 - below)
    finally:
        if tall_h is not None:
            p.restore(normal_h)
    return _finish(p, lines, max(lo, lo_strict), below, 1.0, [], origin, deadline)


def _finish(
    p: _Pane,
    lines: list[str],
    lo: int,
    below: int,
    lpn: float,
    rates: list[tuple[int, int]],
    origin: list[str],
    deadline: float,
) -> _Index:
    """Wrap the lines up as an index, learn where the bottom and the reader's
    view sit in it, and put the view back where the reader had it."""
    idx = _Index(
        lines=lines,
        lookup=_unique(lines),
        lo=lo,
        chrome_below=below,
        width=p.w,
        lpn=lpn,
        rates=rates,
        origin_bottom=len(lines) - 1,
        max_top=max(0, len(lines) - 1),
    )
    now = p.settle()
    blo, bhi = idx.region(len(now))
    btop = locate(idx.lookup, now, blo, bhi)
    if btop is not None:
        idx.max_top = btop
    idx.bottom_screen = now if btop is not None else None
    olo, ohi = idx.region(len(origin))
    top = locate(idx.lookup, origin, olo, ohi)
    if top is not None:
        idx.origin_bottom = min(len(lines) - 1, top + (ohi - olo))
        if top < idx.max_top:
            _navigate(p, idx, top, top, deadline)
    return idx


def _navigate(
    p: _Pane, idx: _Index, want_lo: int, want_hi: int, deadline: float
) -> Optional[tuple[list[str], int]]:
    """Scroll until the index line at the region's top is within
    [want_lo, want_hi]: locate, burst toward it, locate again. Returns
    (screen, top), or None when the view can no longer be placed in the index
    (it changed under us — re-index)."""
    seen: dict[int, int] = {}  # notches in a burst → lines it moved, learned here
    for k, d in idx.rates:
        seen[k] = max(seen.get(k, 0), d)
    last_top: Optional[int] = None
    last_ticks = 0
    last_burst = 0  # notches in the last burst, and which way it went
    last_dir: Optional[bool] = None
    cap = 0  # most notches a burst may send, once one overshot (0 = none)
    stuck = 0
    scr = p.settle()
    page_lines = float(max(1, (idx.region(p.h)[1] - idx.region(p.h)[0] + 1) // 2))
    for _ in range(NAV_MAX_STEPS):
        lo, hi = idx.region(p.h)
        top = locate(idx.lookup, scr, lo, hi)
        if top is None:
            return None
        if last_top is not None and last_ticks:
            moved = abs(top - last_top)
            if moved:
                seen[last_ticks] = moved
                stuck = 0
            else:
                stuck += 1
        if want_lo <= top <= want_hi or stuck >= 3 or time.monotonic() > deadline:
            return scr, top
        delta = (
            top - (want_lo + want_hi) // 2
        )  # > 0: content must move down = scroll up
        n = abs(delta)
        if n <= 4 * idx.lpn:
            ticks = max(1, round(n / idx.lpn))
            for _ in range(ticks):
                p.wheel(delta > 0, 1)
                time.sleep(0.05)  # lone notches: no acceleration
            last_ticks, last_dir = 0, None
            last_top = top
            new = p.settle(scr, wait=0.3)
            scr = new
            continue
        if _keys_ok.get(p.name) and n >= page_lines * 0.75:
            # Page by half-screens, all in one go — no animation.
            pages = max(1, round(n / page_lines))
            p.keys("PPage" if delta > 0 else "NPage", pages)
            new = p.settle(scr, wait=REDRAW_S * 2)
            ntop = locate(idx.lookup, new, lo, hi)
            if ntop is not None and ntop != top:
                page_lines = max(1.0, abs(ntop - top) / pages)
            last_ticks, last_top, scr, last_dir = 0, top, new, None
            continue
        ticks = burst_ticks(seen, n, idx.lpn)
        if last_dir is not None and last_dir != (delta > 0):
            # The last burst carried the view over the target: whatever the
            # rates say, go back with fewer notches than that (acceleration
            # makes rates over-promise), or two bursts bounce forever.
            cap = max(1, cap // 2) if cap else max(1, last_burst // 2)
        if cap:
            ticks = min(ticks, cap)
        p.wheel(delta > 0, ticks)
        last_ticks, last_top = ticks, top
        last_burst, last_dir = ticks, delta > 0
        scr = p.settle(scr, wait=0.4)
    lo, hi = idx.region(p.h)
    top = locate(idx.lookup, scr, lo, hi)
    return (scr, top) if top is not None else None


def _show(p: _Pane, st: _State, deadline: float) -> dict:
    """Bring st.hits[st.cur] into view — untouched when it already is,
    mid-region when it has to move."""
    idx = st.index
    assert idx is not None
    hit = st.hits[st.cur]
    line, col = hit.line, hit.col
    lo, hi = idx.region(p.h)
    span = hi - lo
    clamp = lambda t: max(0, min(idx.max_top, t))  # noqa: E731
    ok_lo, ok_hi = clamp(line - span + MARGIN), clamp(line - MARGIN)
    scr = p.settle()
    top = locate(idx.lookup, scr, lo, hi)
    if top is None or not ok_lo <= top <= ok_hi:
        # Anywhere in the middle half will do: one batch of pages usually
        # lands it there, with no fine-tuning notches.
        mid = clamp(line - span // 2)
        want_lo, want_hi = max(ok_lo, mid - span // 4), min(ok_hi, mid + span // 4)
        if want_lo > want_hi:
            want_lo, want_hi = ok_lo, ok_hi
        got = _navigate(p, idx, want_lo, want_hi, deadline)
        if got is None:
            return {}
        scr, top = got
    row = lo + (line - top)
    if not lo <= row <= hi:
        return {}
    spans = [[row, col, hit.length]]
    if hit.partner is not None:
        prow = lo + (hit.partner[0] - top)
        if lo <= prow <= hi:
            spans.append([prow, hit.partner[1], hit.partner[2]])
    return {
        "row": row,
        "col": col,
        "len": hit.length,
        "spans": spans,
        "region": [lo, hi],
    }


def _unchanged(p: _Pane, idx: _Index) -> bool:
    """The pane shows exactly what it did at the bottom when this index was
    built, at the same size — so the scrollback can't have grown."""
    if idx.bottom_screen is None or idx.width != p.w or idx.stale:
        return False
    return p.settle(calm=1) == idx.bottom_screen


def find(name: str, query, op: str) -> Optional[dict]:
    """One find step in tmux session ``name``'s pane.

    ``op``: ``prepare`` (build the index now — the bar just opened),
    ``search`` (fresh query: count it, land on the newest hit at or above the
    reader's view), ``older`` / ``newer`` (the next hit, wrapping), ``close``
    (back to the bottom = live) or ``cancel`` (stop a running step). Returns
    ``{"mode": "scroll", "status", "total", "index", "row", "col",
    "region"}`` — status found|none|ready|closed|cancelled|error — or None
    when the session is unreachable.
    """
    q = query if isinstance(query, Query) else Query(text=str(query or ""))
    st = _state(name)
    if op == "cancel":
        st.cancel.set()
        return {"mode": "scroll", "status": "cancelled"}
    st.cancel.set()  # a new request supersedes whatever step is running
    with st.lock:
        st.cancel.clear()
        p = _Pane(name, st.cancel)
        if not p.grabbed():
            if p.h == 0:
                return None
            return {
                "mode": "scroll",
                "status": "error",
                "error": "app does not scroll with the mouse",
            }
        try:
            return _step(p, st, q, op)
        except ValueError as err:  # an invalid regular expression
            return {"mode": "scroll", "status": "error", "error": str(err)}
        except _Cancelled:
            return {"mode": "scroll", "status": "cancelled"}
        except RuntimeError as err:
            return {"mode": "scroll", "status": "error", "error": str(err)}


def _step(p: _Pane, st: _State, query: Query, op: str) -> dict:
    deadline = time.monotonic() + INDEX_BUDGET_S
    base = {"mode": "scroll"}
    if op == "close":
        st.query, st.hits, st.cur = None, [], -1
        _to_bottom(p, deadline)
        idx = st.index
        if idx is not None:
            # Esc means "back to live": make sure the view really got there
            # (a key taken mid-animation can fall short).
            for _ in range(3):
                scr = p.settle()
                lo, hi = idx.region(p.h)
                top = locate(idx.lookup, scr, lo, hi)
                if top is None or top >= idx.max_top:
                    break
                _run_to_end(p, False, deadline)
        return {**base, "status": "closed"}
    if op == "prepare" and st.index is not None and _unchanged(p, st.index):
        # Nothing new since the last index (an idle agent): reuse it.
        st.query = None
        return {**base, "status": "ready", "lines": len(st.index.lines), "cached": True}
    if st.index is None or st.index.width != p.w or op == "prepare":
        st.index = _index(p, st)
        st.query = None
    if op == "prepare":
        return {**base, "status": "ready", "lines": len(st.index.lines)}
    if not query.text:
        return {**base, "status": "none", "total": 0, "index": 0}
    if op == "search" or query != st.query:
        st.query = query
        st.hits = find_hits(st.index.lines, query)
        if not st.hits:
            st.cur = -1
            return {**base, "status": "none", "total": 0, "index": 0}
        st.cur = pick_start(st.hits, st.index.origin_bottom)
    elif st.hits:
        st.cur = (st.cur + (1 if op == "newer" else -1)) % len(st.hits)
    if not st.hits:
        return {**base, "status": "none", "total": 0, "index": 0}
    shown = _show(p, st, deadline)
    if not shown:
        # The view drifted off the index (new output): rebuild once, retry.
        st.index = _index(p, st)
        st.hits = find_hits(st.index.lines, query)
        if not st.hits:
            return {**base, "status": "none", "total": 0, "index": 0}
        st.cur = min(st.cur, len(st.hits) - 1)
        shown = _show(p, st, deadline)
        if not shown:
            return {**base, "status": "error", "error": "lost track of the view"}
    return {
        **base,
        "status": "found",
        "total": len(st.hits),
        "index": st.cur + 1,
        **shown,
    }


# --------------------------------------------------------------------------- #
# Ahead of time (driven by the server's find-index loop)
# --------------------------------------------------------------------------- #


def _back_to_live(name: str) -> None:
    """After a cancelled index: the view may be anywhere in the scrollback —
    put it back at the bottom, whatever the cancel flag says."""
    p = _Pane(name, threading.Event())
    if not p.grabbed():
        return
    if _keys_ok.get(name):
        p.keys("C-End")
    else:
        _run_to_end(p, False, time.monotonic() + 5)


def observe(name: str) -> float:
    """One cheap look at an agent pane (a single normal-height capture — no
    keys, no resize, nothing visible): fold new output at the bottom into its
    index, and return how long its screen has been unchanged.

    The bottom view's region rows ARE the last lines of the scrollback, so
    once the view is located in the index its tail is simply replaced by
    them — growth and a streaming reply rewriting its last lines alike. A
    view that can't be located (more than a screen of new output since the
    last look) marks the index stale; a view above the bottom (the reader
    scrolled up) is left alone."""
    st = _state(name)
    if not st.lock.acquire(blocking=False):
        return 0.0
    try:
        p = _Pane(name, threading.Event())
        if not p.grabbed():
            return 0.0
        scr = p.screen()
        now = time.monotonic()
        if scr != st.seen:
            st.seen, st.seen_at = scr, now
        idx = st.index
        if idx is not None and not idx.stale:
            if idx.width != p.w:
                idx.stale = True
            elif scr != idx.bottom_screen:
                lo, hi = idx.region(p.h)
                top = locate(idx.lookup, scr, lo, hi)
                if top is None:
                    idx.stale = True
                elif top >= idx.max_top:
                    lines = idx.lines[:top] + [
                        scr[r] for r in range(lo, min(hi, len(scr) - 1) + 1)
                    ]
                    while lines and not lines[-1].strip():
                        lines.pop()
                    idx.lines = lines
                    idx.lookup = _unique(lines)
                    idx.max_top = top
                    idx.bottom_screen = scr
                    idx.origin_bottom = len(lines) - 1
        return now - st.seen_at
    finally:
        st.lock.release()


def needs_index(name: str) -> bool:
    st = _states.get(name)
    return st is None or st.index is None or st.index.stale


def background_index(name: str, notify) -> bool:
    """Build ``name``'s index now, ahead of any Ctrl+F. ``notify(True)`` goes
    out first — the browser freezes that pane's display on it, so the tall
    window and the sweep are never seen — and ``notify(False)`` after. Any
    human input into the pane cancels it (cancel_background): the view goes
    straight back to the bottom and nothing is kept."""
    st = _state(name)
    if not st.lock.acquire(blocking=False):
        return False
    try:
        st.cancel.clear()
        st.background = True
        p = _Pane(name, st.cancel)
        if not p.grabbed():
            return False
        notify(True)
        time.sleep(0.15)  # let the freeze land before anything moves
        try:
            st.index = _index(p, st)
            return True
        except _Cancelled:
            _back_to_live(name)
            return False
        except RuntimeError:
            return False
    finally:
        st.background = False
        try:
            notify(False)
        finally:
            st.lock.release()


def cancel_background(name: str) -> None:
    """A human touched the pane: stop a background index at once."""
    st = _states.get(name)
    if st is not None and st.background:
        st.cancel.set()
