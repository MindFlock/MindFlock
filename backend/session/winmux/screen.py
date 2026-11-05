"""A session's screen: what tmux's grid is to ``capture-pane`` and the
``#{…}`` formats, kept with pyte.

The child's output goes through :class:`Term`, which does three jobs tmux
does for a pane:

* **the screen model** — a pyte screen plus the parts pyte lacks and
  MindFlock reads: scrollback history with tmux's ``history-limit``, the
  wrapped-line flag behind ``capture-pane -J``, the alternate screen
  (``?1049``/``?1047``/``?47``; its lines never enter history), tmux's
  ``scroll-on-clear`` (a full clear pushes the screen into history) and
  history-aware resizing (shrinking pushes rows into history, growing pulls
  them back, as tmux does);
* **the terminal's answers** — the child's queries (DA1/DA2, DSR status and
  cursor position, XTVERSION) are answered by the host from the model, as
  tmux answers them, because a session has no attached terminal most of the
  time and an agent CLI waiting on an answer would hang;
* **the bytes clients see** — the same output minus those queries (a client
  answering them too would double the answer) and minus the sequences tmux
  never lets through to its clients (kitty keyboard, modifyOtherKeys,
  window ops, DCS passthrough, colour/mode queries).

pyte's own CSI parser treats ``CSI > 4;1 m`` as SGR and draws a DCS body as
text, so those never reach it: the tokenizer below splits every sequence
itself and hands pyte only what it parses correctly.
"""

from __future__ import annotations

import collections
import re
from typing import Deque, Dict, List, Optional, Tuple

import pyte
from pyte import modes as _mo
from pyte.screens import Char, Cursor, StaticDefaultDict

__all__ = ["Term", "DEFAULT_HISTORY"]

DEFAULT_HISTORY = 2000

#: What the host answers for the child's terminal queries — tmux's answers,
#: so an agent sees the same terminal it sees under tmux.
DA1_REPLY = "\x1b[?1;2c"
DA2_REPLY = "\x1b[>84;0;0c"
XTVERSION_REPLY = "\x1bP>|tmux 3.4\x1b\\"

#: Private modes the host tracks itself (and also hands to pyte).
MOUSE_MODES = (1000, 1002, 1003, 1005, 1006, 1015)
_ALT_MODES = (1049, 1047, 47)

#: Longest unterminated sequence kept across reads before it is dropped as
#: garbage (a stray ESC must not swallow the rest of the session's output).
_MAX_PENDING_CSI = 512
_MAX_PENDING_STRING = 1 << 20

_ANSI = {
    "black": 0,
    "red": 1,
    "green": 2,
    "brown": 3,
    "yellow": 3,
    "blue": 4,
    "magenta": 5,
    "cyan": 6,
    "white": 7,
}

_PALETTE_INDEX: Dict[str, int] = {}
try:  # pyte's 256-colour table: a hex value back to the index tmux emits
    from pyte.graphics import FG_BG_256 as _FG_BG_256

    for _i, _hex in enumerate(_FG_BG_256):
        if _i >= 16:
            _PALETTE_INDEX.setdefault(_hex, _i)
except Exception:  # noqa: BLE001 - an older pyte: truecolor SGR then
    pass

_HEX6 = re.compile(r"^[0-9a-fA-F]{6}$")


def _color_params(color: str, base: int) -> List[str]:
    """SGR parameters for pyte colour ``color`` (``base`` 30 fg / 40 bg)."""
    if color == "default":
        return []
    name = color
    bright = False
    if name.startswith("bright") and name[6:] in _ANSI:
        bright, name = True, name[6:]
    if name in _ANSI:
        return [str((base + 60 if bright else base) + _ANSI[name])]
    if _HEX6.match(color):
        idx = _PALETTE_INDEX.get(color.lower())
        if idx is not None:
            return [str(base + 8), "5", str(idx)]
        r, g, b = int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)
        return [str(base + 8), "2", str(r), str(g), str(b)]
    return []


def sgr_of(ch: Char) -> str:
    """The SGR sequence that sets exactly ``ch``'s attributes from a reset,
    or ``""`` for the default attributes."""
    p: List[str] = []
    if ch.bold:
        p.append("1")
    if ch.italics:
        p.append("3")
    if ch.underscore:
        p.append("4")
    if ch.blink:
        p.append("5")
    if ch.reverse:
        p.append("7")
    if ch.strikethrough:
        p.append("9")
    p += _color_params(ch.fg, 30)
    p += _color_params(ch.bg, 40)
    return ("\x1b[" + ";".join(p) + "m") if p else ""


def _attrs(ch: Char) -> tuple:
    return ch[1:]


_DEFAULT_ATTRS = Char(" ")[1:]


def _is_blank(ch: Char) -> bool:
    return ch.data in (" ", "") and _attrs(ch) == _DEFAULT_ATTRS


def _normalize_sgr(params: str) -> str:
    """``CSI … m`` parameters pyte can parse: colon sub-parameters
    (``38:2::r:g:b``, ``4:3``) rewritten to their ``;`` forms or dropped
    (underline styles become plain underline, ``58`` underline colour is
    dropped — pyte has neither)."""
    if ":" not in params:
        return params
    out: List[str] = []
    for group in params.split(";"):
        if ":" not in group:
            out.append(group)
            continue
        sub = group.split(":")
        head = sub[0]
        if head in ("38", "48") and len(sub) >= 3:
            if sub[1] == "5":
                out += [head, "5", sub[2] or "0"]
            elif sub[1] == "2":
                rgb = [s or "0" for s in sub[2:]]
                rgb = rgb[-3:] if len(rgb) >= 3 else (rgb + ["0", "0", "0"])[:3]
                out += [head, "2"] + rgb
        elif head == "4":
            out.append("24" if (len(sub) > 1 and sub[1] == "0") else "4")
        # 58 (underline colour) and anything else with sub-parameters: dropped
    return ";".join(out)


class _Screen(pyte.Screen):
    """pyte's screen plus history, wrap flags and the alternate screen.

    A line is pyte's ``StaticDefaultDict``; the host hangs two attributes on
    it: ``wrapped`` (the line ended by an auto-wrap, tmux's
    ``GRID_LINE_WRAPPED``) and ``width`` (the screen width when it scrolled
    into history)."""

    def __init__(self, columns: int, lines: int, history: int) -> None:
        self.history: Deque = collections.deque(maxlen=max(0, history))
        self._drawing = False
        self.alt: Optional[tuple] = None
        super().__init__(columns, lines)

    # -- wraps -----------------------------------------------------------------
    def draw(self, data: str) -> None:
        self._drawing = True
        try:
            super().draw(data)
        finally:
            self._drawing = False

    def linefeed(self) -> None:
        # pyte wraps by carriage_return()+linefeed() inside draw(): a line feed
        # during draw is a wrap, any other is an explicit newline — exactly
        # how tmux sets / clears GRID_LINE_WRAPPED in screen_write_linefeed.
        self.buffer[self.cursor.y].wrapped = self._drawing
        super().linefeed()

    # -- history ---------------------------------------------------------------
    def _push_history(self, line) -> None:
        if self.history.maxlen == 0:
            return
        line.width = self.columns
        self.history.append(line)

    def index(self) -> None:
        top, bottom = self.margins or (0, self.lines - 1)
        if self.cursor.y == bottom and top == 0 and self.alt is None:
            # Only a full-width scroll from the top feeds history (tmux keeps
            # no history for a scroll region that starts lower down).
            self._push_history(self.buffer[0])
        super().index()

    def erase_in_display(self, how: int = 0, *args, **kwargs) -> None:
        if how == 3:
            self.history.clear()  # E3: clear the scrollback, as tmux does
            return
        if how == 2 and self.alt is None:
            # tmux's scroll-on-clear (default on): a full-screen clear first
            # scrolls the used rows into history.
            last = -1
            for y in range(self.lines):
                if y in self.buffer and any(
                    not _is_blank(c) for c in self.buffer[y].values()
                ):
                    last = y
            for y in range(last + 1):
                self._push_history(self.buffer[y])
                self.buffer[y] = StaticDefaultDict(self.default_char)
        super().erase_in_display(how, *args, **kwargs)
        if how == 2:
            for line in self.buffer.values():
                line.wrapped = False

    def reset(self) -> None:
        super().reset()
        if getattr(self, "alt", None) is not None:
            self.alt = None

    # -- alternate screen ------------------------------------------------------
    def enter_alt(self, mode: int) -> None:
        if self.alt is not None:
            return
        cur = Cursor(self.cursor.x, self.cursor.y, self.cursor.attrs)
        cur.hidden = self.cursor.hidden
        self.alt = (self.buffer, cur, mode)
        self.buffer = collections.defaultdict(
            lambda: StaticDefaultDict(self.default_char)
        )
        self.dirty.update(range(self.lines))

    def leave_alt(self, mode: int) -> None:
        if self.alt is None:
            return
        buf, cur, _ = self.alt
        self.alt = None
        self.buffer = buf
        if mode == 1049:
            self.cursor.x, self.cursor.y = cur.x, cur.y
            self.cursor.attrs = cur.attrs
        # The main screen may have been saved at another size.
        for y in [y for y in self.buffer if y >= self.lines]:
            self.buffer.pop(y, None)
        self.ensure_hbounds()
        self.ensure_vbounds()
        self.dirty.update(range(self.lines))

    # -- resize ----------------------------------------------------------------
    def _row_blank(self, y: int) -> bool:
        if y not in self.buffer:
            return True
        return all(_is_blank(c) for c in self.buffer[y].values())

    def resize(self, lines: Optional[int] = None, columns: Optional[int] = None):
        lines = lines or self.lines
        columns = columns or self.columns
        if lines == self.lines and columns == self.columns:
            return
        old = self.lines
        if lines < old:
            n = old - lines
            # Like tmux: blank rows below the cursor go first, then rows off
            # the top (into history on the main screen).
            drop_bottom = 0
            for y in range(old - 1, self.cursor.y, -1):
                if drop_bottom == n or not self._row_blank(y):
                    break
                drop_bottom += 1
            for y in range(old - drop_bottom, old):
                self.buffer.pop(y, None)
            top = n - drop_bottom
            if top:
                rows = {}
                for y in range(old - drop_bottom):
                    if y < top:
                        if self.alt is None:
                            self._push_history(self.buffer[y])
                    elif y in self.buffer:
                        rows[y - top] = self.buffer[y]
                self.buffer.clear()
                self.buffer.update(rows)
                self.cursor.y = max(0, self.cursor.y - top)
        elif lines > old and self.alt is None and self.history:
            k = min(lines - old, len(self.history))
            rows = {y + k: line for y, line in self.buffer.items() if y < old}
            for i in range(k):
                rows[k - 1 - i] = self.history.pop()
            self.buffer.clear()
            self.buffer.update(rows)
            self.cursor.y += k
        if columns < self.columns:
            for line in self.buffer.values():
                for x in [x for x in line if x >= columns]:
                    line.pop(x, None)
        self.lines, self.columns = lines, columns
        self.tabstops = set(range(8, columns, 8))
        self.set_margins()
        self.ensure_hbounds()
        self.ensure_vbounds()
        self.dirty.update(range(lines))

    def ensure_hbounds(self) -> None:
        self.cursor.x = min(max(0, self.cursor.x), self.columns)

    def ensure_vbounds(self, use_margins=None) -> None:
        self.cursor.y = min(max(0, self.cursor.y), self.lines - 1)


class Term:
    """One pane's terminal: feed it the child's output, read the screen.

    :meth:`feed` returns ``(client_text, reply)`` — what attached clients
    should be sent and what to write back to the child."""

    def __init__(self, cols: int, rows: int, history: int = DEFAULT_HISTORY):
        self.screen = _Screen(cols, rows, history)
        self.stream = pyte.Stream(self.screen)
        self._pending = ""
        self.private: set = set()  # tracked private modes (mouse, 2004, 1004)
        self.keypad = False
        self.title = ""
        self.cwd = ""  # from OSC 7, when the shell reports it

    # -- geometry --------------------------------------------------------------
    @property
    def rows(self) -> int:
        return self.screen.lines

    @property
    def cols(self) -> int:
        return self.screen.columns

    def resize(self, rows: int, cols: int) -> None:
        self.screen.resize(lines=max(1, rows), columns=max(1, cols))

    def set_history_limit(self, n: int) -> None:
        n = max(0, int(n))
        if self.screen.history.maxlen != n:
            self.screen.history = collections.deque(self.screen.history, maxlen=n)

    # -- state for the formats -------------------------------------------------
    @property
    def history_size(self) -> int:
        return len(self.screen.history)

    @property
    def history_limit(self) -> int:
        return self.screen.history.maxlen or 0

    @property
    def alternate_on(self) -> bool:
        return self.screen.alt is not None

    def mode(self, n: int) -> bool:
        return n in self.private

    @property
    def app_cursor(self) -> bool:
        return (1 << 5) in self.screen.mode  # DECCKM (pyte names no constant)

    @property
    def mouse_any(self) -> bool:
        return any(m in self.private for m in (1000, 1002, 1003))

    @property
    def bracketed_paste(self) -> bool:
        return 2004 in self.private

    def reset(self) -> None:
        self.screen.reset()
        self.private.clear()
        self.keypad = False

    # -- feeding -----------------------------------------------------------------
    def feed(self, data: str) -> Tuple[str, str]:
        s = self._pending + data
        self._pending = ""
        n = len(s)
        client: List[str] = []
        scr: List[str] = []
        reply: List[str] = []

        def flush():
            if scr:
                self.stream.feed("".join(scr))
                scr.clear()

        i = 0
        while i < n:
            j = s.find("\x1b", i)
            if j < 0:
                client.append(s[i:])
                scr.append(s[i:])
                break
            if j > i:
                client.append(s[i:j])
                scr.append(s[i:j])
            if j + 1 >= n:
                self._pending = s[j:]
                break
            c = s[j + 1]
            if c == "[":
                k = j + 2
                while k < n and "\x30" <= s[k] <= "\x3f":
                    k += 1
                p_end = k
                while k < n and "\x20" <= s[k] <= "\x2f":
                    k += 1
                if k >= n:
                    if n - j > _MAX_PENDING_CSI:
                        i = n
                        break
                    self._pending = s[j:]
                    break
                fin = s[k]
                if not ("\x40" <= fin <= "\x7e"):
                    i = k  # malformed: drop the introducer, keep what follows
                    continue
                params, inter = s[j + 2 : p_end], s[p_end:k]
                seq = s[j : k + 1]
                i = k + 1
                self._csi(seq, params, inter, fin, client, scr, reply, flush)
                continue
            if c == "]":
                end, tlen = self._find_st(s, j + 2, bel=True)
                if end < 0:
                    if n - j > _MAX_PENDING_STRING:
                        i = n
                        break
                    self._pending = s[j:]
                    break
                body = s[j + 2 : end]
                seq = s[j : end + tlen]
                i = end + tlen
                self._osc(seq, body, client, scr)
                continue
            if c in "P_^X":
                end, tlen = self._find_st(s, j + 2, bel=False)
                if end < 0:
                    if n - j > _MAX_PENDING_STRING:
                        i = n
                        break
                    self._pending = s[j:]
                    break
                body = s[j + 2 : end]
                seq = s[j : end + tlen]
                i = end + tlen
                if c == "P" and (body[:2] in ("+q", "$q") or body.startswith("tmux;")):
                    continue  # queries / tmux passthrough: tmux keeps them
                client.append(seq)
                continue
            if "\x20" <= c <= "\x2f":
                if j + 2 >= n:
                    self._pending = s[j:]
                    break
                seq = s[j : j + 3]
                i = j + 3
                client.append(seq)
                if c in "()#%":
                    scr.append(seq)
                continue
            # Two-byte escapes.
            seq = s[j : j + 2]
            i = j + 2
            if c == "c":  # RIS
                flush()
                self.reset()
            elif c == "=":
                self.keypad = True
            elif c == ">":
                self.keypad = False
            else:
                scr.append(seq)
            client.append(seq)
        flush()
        return "".join(client), "".join(reply)

    @staticmethod
    def _find_st(s: str, start: int, bel: bool) -> Tuple[int, int]:
        """Index and length of the string terminator (``ESC \\``, or BEL for
        OSC) at or after ``start``; ``(-1, 0)`` when it has not arrived."""
        k = start
        n = len(s)
        while k < n:
            ch = s[k]
            if ch == "\x07" and bel:
                return k, 1
            if ch == "\x9c":
                return k, 1
            if ch == "\x1b":
                if k + 1 >= n:
                    return -1, 0
                if s[k + 1] == "\\":
                    return k, 2
            k += 1
        return -1, 0

    def _csi(self, seq, params, inter, fin, client, scr, reply, flush) -> None:
        prefix = params[:1] if params[:1] in ("<", "=", ">", "?") else ""
        body = params[1:] if prefix else params
        if prefix in ("<", "=", ">"):
            # Never shown to clients (tmux strips kitty keyboard, modifyOtherKeys
            # and the DA2/DA3/XTVERSION queries); two of them are answered.
            if prefix == ">" and not inter and fin == "c" and body in ("", "0"):
                reply.append(DA2_REPLY)
            elif prefix == ">" and not inter and fin == "q" and body in ("", "0"):
                reply.append(XTVERSION_REPLY)
            return
        if inter:
            if inter == "$" and fin == "p":
                return  # DECRQM: a query tmux does not pass on
            client.append(seq)  # cursor style (SP q) and friends: client only
            return
        if fin == "c" and not prefix:
            if body in ("", "0"):
                reply.append(DA1_REPLY)
            return
        if fin == "n":
            if not prefix and body == "5":
                reply.append("\x1b[0n")
            elif body == "6":
                flush()
                y = self.screen.cursor.y + 1
                x = min(self.screen.cursor.x, self.screen.columns - 1) + 1
                if _mo.DECOM in self.screen.mode and self.screen.margins:
                    y -= self.screen.margins.top
                reply.append("\x1b[%s%d;%dR" % ("?" if prefix else "", y, x))
            return
        if fin == "t":
            return  # window ops (and their size queries): tmux swallows them
        if fin == "u" and prefix == "?":
            return  # kitty keyboard query
        if prefix == "?" and fin in "hl":
            nums = [int(x) for x in body.split(";") if x.isdigit()]
            keep = []
            for m in nums:
                if m in _ALT_MODES:
                    flush()
                    if fin == "h":
                        self.screen.enter_alt(m)
                        if m in (1049, 1047):
                            self.screen.erase_in_display(2)
                    else:
                        if m == 1047 and self.screen.alt is not None:
                            self.screen.erase_in_display(2)
                        self.screen.leave_alt(m)
                    continue
                if m in MOUSE_MODES or m in (1004, 2004):
                    if fin == "h":
                        self.private.add(m)
                    else:
                        self.private.discard(m)
                keep.append(str(m))
            client.append(seq)
            if keep:
                scr.append("\x1b[?" + ";".join(keep) + fin)
            return
        if fin == "m" and not prefix:
            norm = _normalize_sgr(body)
            client.append(seq)
            scr.append("\x1b[" + norm + "m")
            return
        client.append(seq)
        scr.append(seq)

    def _osc(self, seq: str, body: str, client, scr) -> None:
        code, _, rest = body.partition(";")
        if code in ("0", "1", "2"):
            self.title = rest if code != "1" else self.title
            client.append(seq)
            scr.append("\x1b]" + body + "\x07")
            return
        if code in ("4", "10", "11", "12", "17", "19", "104") and "?" in rest:
            return  # colour queries: tmux does not hand them to clients
        if code == "7":
            # file://host/path — the shell's cwd, the best pane_current_path
            # there is on Windows.
            m = re.match(r"file://[^/]*(/.*)", rest)
            if m:
                from urllib.parse import unquote

                path = unquote(m.group(1))
                if re.match(r"^/[A-Za-z]:", path):
                    path = path[1:]
                self.cwd = path
        client.append(seq)

    # -- reading -----------------------------------------------------------------
    def total_lines(self) -> int:
        return len(self.screen.history) + self.screen.lines

    def line(self, i: int):
        """Line ``i`` of history+screen (0 = the oldest history line)."""
        h = self.screen.history
        if i < len(h):
            return h[i]
        return self.screen.buffer[i - len(h)]

    def _width(self, line, is_history: bool) -> int:
        if is_history:
            return getattr(line, "width", self.screen.columns)
        return self.screen.columns

    def render(
        self,
        i: int,
        codes: bool = False,
        trim: bool = True,
        line=None,
    ) -> str:
        """Line ``i`` as text (``codes``: with SGR). ``trim`` drops trailing
        blanks (capture-pane's default); without it the line keeps them up to
        the last used cell, and a wrapped line keeps its full width — what
        ``-J``/``-N`` print."""
        is_hist = line is None and i < len(self.screen.history)
        if line is None:
            line = self.line(i)
        width = self._width(line, is_hist)
        wrapped = getattr(line, "wrapped", False)
        cells = [line[x] for x in range(width)]
        end = width
        if not (wrapped and not trim):
            while end > 0 and _is_blank(cells[end - 1]):
                end -= 1
        out: List[str] = []
        cur = _DEFAULT_ATTRS
        for x in range(end):
            ch = cells[x]
            if ch.data == "":
                continue  # the stub after a wide character
            if codes:
                a = _attrs(ch)
                if a != cur:
                    out.append(
                        "\x1b[0m" + sgr_of(ch) if cur != _DEFAULT_ATTRS else sgr_of(ch)
                    )
                    cur = a
            out.append(ch.data)
        if codes and cur != _DEFAULT_ATTRS:
            out.append("\x1b[0m")
        return "".join(out)

    def wrapped(self, i: int) -> bool:
        return bool(getattr(self.line(i), "wrapped", False))

    def capture(
        self,
        start: Optional[str] = None,
        end: Optional[str] = None,
        codes: bool = False,
        join: bool = False,
        keep_spaces: bool = False,
        alternate: bool = False,
    ) -> str:
        """``capture-pane -p`` output (``-S start -E end -e -J -N -a``)."""
        if alternate:
            if self.screen.alt is None:
                raise ValueError("no alternate screen")
            buf = self.screen.alt[0]
            lines = [buf[y] for y in range(self.screen.lines)]
            return "".join(
                self.render(0, codes, not (join or keep_spaces), line=ln) + "\n"
                for ln in lines
            )
        hsize = len(self.screen.history)
        sy = self.screen.lines
        last = hsize + sy - 1

        def pos(v: Optional[str], default: int, is_end: bool) -> int:
            if v is None:
                return default
            if v == "-":
                return last if is_end else 0
            n = int(v)
            if n < 0 and -n > hsize:
                return 0
            return max(0, min(hsize + n, last))

        top = pos(start, hsize, False)
        bottom = pos(end, last, True)
        if bottom < top:
            top, bottom = bottom, top
        trim = not (join or keep_spaces)
        out: List[str] = []
        for i in range(top, bottom + 1):
            out.append(self.render(i, codes, trim))
            if not (join and self.wrapped(i)):
                out.append("\n")
        return "".join(out)

    # -- painting a client -------------------------------------------------------
    def modes_sequence(self) -> str:
        """The modes the child set, as the sequences that set them on a fresh
        client terminal (after a reset of the same modes)."""
        out = ["\x1b[?1l\x1b>\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?1005l"]
        out.append("\x1b[?1015l\x1b[?2004l\x1b[?1004l")
        if self.app_cursor:
            out.append("\x1b[?1h")
        if self.keypad:
            out.append("\x1b=")
        for m in sorted(self.private):
            out.append("\x1b[?%dh" % m)
        return "".join(out)

    def repaint(self, history_lines: int = 0) -> str:
        """Everything a freshly attached client needs to show this screen
        (tmux redraws a client on attach): its main/alternate state, the
        screen's lines with their colours, the scroll region, the cursor and
        the child's modes. On the main screen, up to ``history_lines`` of
        history are printed first so the client's own scrollback has them."""
        scr = self.screen
        out = ["\x1b[0m\x1b[r\x1b[?1049l"]
        if scr.alt is not None:
            out.append("\x1b[?1049h")
        out.append("\x1b[H\x1b[2J")
        if scr.alt is None and history_lines > 0 and scr.history:
            hsize = len(scr.history)
            first = max(0, hsize - history_lines)
            for i in range(first, hsize):
                out.append(self.render(i, codes=True) + "\x1b[0m\x1b[K\r\n")
            out.append("\x1b[H")
        for y in range(scr.lines):
            out.append("\x1b[%d;1H" % (y + 1))
            out.append(self.render(len(scr.history) + y, codes=True))
            out.append("\x1b[0m\x1b[K")
        if scr.margins and (scr.margins.top, scr.margins.bottom) != (0, scr.lines - 1):
            out.append("\x1b[%d;%dr" % (scr.margins.top + 1, scr.margins.bottom + 1))
        out.append(self.modes_sequence())
        if self.title:
            out.append("\x1b]2;%s\x07" % self.title)
        attrs = sgr_of(scr.cursor.attrs)
        out.append(attrs)
        out.append(
            "\x1b[%d;%dH" % (scr.cursor.y + 1, min(scr.cursor.x, scr.columns - 1) + 1)
        )
        out.append("\x1b[?25l" if scr.cursor.hidden else "\x1b[?25h")
        return "".join(out)

    def view(self, offset: int) -> str:
        """A copy-mode view ``offset`` lines up from the bottom, with tmux's
        ``[offset/history]`` position indicator at the top right."""
        scr = self.screen
        hsize = len(scr.history)
        top = hsize - offset
        out = ["\x1b[0m\x1b[?25l\x1b[H\x1b[2J"]
        for y in range(scr.lines):
            out.append("\x1b[%d;1H" % (y + 1))
            out.append(self.render(top + y, codes=True))
            out.append("\x1b[0m\x1b[K")
        tag = "[%d/%d]" % (offset, hsize)
        col = max(1, scr.columns - len(tag) + 1)
        out.append("\x1b[1;%dH\x1b[30;43m%s\x1b[0m" % (col, tag))
        return "".join(out)
