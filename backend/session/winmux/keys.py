"""``send-keys`` key names → the bytes a terminal sends for them.

tmux turns a key name (``Enter``, ``C-c``, ``M-Up``, ``F5``) into the byte
sequence an xterm-compatible terminal would have produced for that key and
writes it into the pane; the host does the same, so a caller that drives an
agent with ``send-keys -t s C-u`` gets the same effect on Windows. The
tables follow tmux's ``input-keys.c`` (standard keys, no extended-keys
mode): cursor keys honour the pane's application-cursor mode (DECCKM) the
way tmux does, and modifiers on named keys use xterm's ``CSI 1;<m>X`` /
``CSI <n>;<m>~`` form.

A name that is not a key is sent as its literal characters, as tmux does
(``send-keys -t s hello`` types ``hello``).
"""

from __future__ import annotations

from typing import Optional

__all__ = ["key_bytes"]

#: Keys whose xterm form is ``CSI <n> ~`` (modifiers: ``CSI <n>;<m> ~``).
_TILDE = {
    "ic": 2,
    "insert": 2,
    "dc": 3,
    "delete": 3,
    "home": 1,
    "end": 4,
    "ppage": 5,
    "pageup": 5,
    "pgup": 5,
    "npage": 6,
    "pagedown": 6,
    "pgdn": 6,
    "f5": 15,
    "f6": 17,
    "f7": 18,
    "f8": 19,
    "f9": 20,
    "f10": 21,
    "f11": 23,
    "f12": 24,
}

#: Cursor keys: the final byte of ``CSI X`` / ``SS3 X``.
_CURSOR = {"up": "A", "down": "B", "right": "C", "left": "D"}

#: F1-F4: ``SS3 P..S`` (modifiers: ``CSI 1;<m> P..S``).
_SS3_F = {"f1": "P", "f2": "Q", "f3": "R", "f4": "S"}

#: Keys that are a fixed byte string and take Meta only (as an ESC prefix).
_PLAIN = {
    "enter": b"\r",
    "escape": b"\x1b",
    "esc": b"\x1b",
    "tab": b"\t",
    "btab": b"\x1b[Z",
    "bspace": b"\x7f",
    "space": b" ",
}

#: ``C-<char>`` for the punctuation that has a control code.
_CTRL_PUNCT = {
    "@": 0x00,
    " ": 0x00,
    "[": 0x1B,
    "\\": 0x1C,
    "]": 0x1D,
    "^": 0x1E,
    "_": 0x1F,
    "?": 0x7F,
    "/": 0x1F,
    "2": 0x00,
    "3": 0x1B,
    "4": 0x1C,
    "5": 0x1D,
    "6": 0x1E,
    "7": 0x1F,
    "8": 0x7F,
}


def _split_modifiers(name: str):
    """``("C-M-x")`` → ``(ctrl, meta, shift, "x")``. tmux accepts ``C-``,
    ``M-``, ``S-`` in any order and case, and ``^x`` for Ctrl."""
    ctrl = meta = shift = False
    rest = name
    while len(rest) > 2 and rest[1] == "-" and rest[0] in "CcMmSs":
        c = rest[0].upper()
        if c == "C":
            ctrl = True
        elif c == "M":
            meta = True
        else:
            shift = True
        rest = rest[2:]
    if len(rest) == 2 and rest[0] == "^":
        ctrl, rest = True, rest[1]
    return ctrl, meta, shift, rest


def key_bytes(
    name: str, app_cursor: bool = False, app_keypad: bool = False
) -> Optional[bytes]:
    """The bytes for key ``name``, or ``None`` when ``name`` is not a key
    (the caller then sends it literally, as tmux does). ``app_cursor``: the
    pane's application cursor mode, under which unmodified arrows (and
    Home/End) are ``SS3`` sequences."""
    if not name:
        return None
    ctrl, meta, shift, base = _split_modifiers(name)
    low = base.lower()
    mods = 1 + (1 if shift else 0) + (2 if meta else 0) + (4 if ctrl else 0)
    out: Optional[bytes] = None
    if low in _CURSOR:
        fin = _CURSOR[low]
        if mods > 1:
            out = ("\x1b[1;%d%s" % (mods, fin)).encode()
        elif app_cursor:
            out = ("\x1bO" + fin).encode()
        else:
            out = ("\x1b[" + fin).encode()
        return out
    if low in ("home", "end") and mods == 1 and app_cursor:
        return b"\x1bOH" if low == "home" else b"\x1bOF"
    if low in _TILDE:
        n = _TILDE[low]
        if mods > 1:
            return ("\x1b[%d;%d~" % (n, mods)).encode()
        return ("\x1b[%d~" % n).encode()
    if low in _SS3_F:
        fin = _SS3_F[low]
        if mods > 1:
            return ("\x1b[1;%d%s" % (mods, fin)).encode()
        return ("\x1bO" + fin).encode()
    if low in _PLAIN:
        out = _PLAIN[low]
        if ctrl and low == "space":
            out = b"\x00"
        elif shift and low == "tab":
            out = b"\x1b[Z"
        return (b"\x1b" + out) if meta else out
    if len(base) == 1:
        ch = base
        if ctrl:
            if ch.isalpha() and ch.isascii():
                code = bytes([ord(ch.lower()) & 0x1F])
            elif ch in _CTRL_PUNCT:
                code = bytes([_CTRL_PUNCT[ch]])
            else:
                code = ch.encode("utf-8")
        else:
            code = (ch.upper() if shift else ch).encode("utf-8")
        if not (ctrl or meta or shift):
            # A bare one-character name IS that character — tmux sends it.
            return code
        return (b"\x1b" + code) if meta else code
    return None
