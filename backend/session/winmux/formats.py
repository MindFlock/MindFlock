"""tmux's ``#{…}`` format language — the subset MindFlock (and a person
typing tmux commands) uses.

Supported: ``#{var}``; ``#{?cond,then,else}``; ``#{||:a,b}``,
``#{&&:a,b}``; comparisons ``#{==:a,b}`` ``#{!=:a,b}`` ``#{<:a,b}``
``#{>:a,b}`` ``#{<=:a,b}`` ``#{>=:a,b}``; modifiers ``#{=N:var}`` /
``#{=-N:var}`` (truncate), ``#{t:var}`` (time), ``#{b:var}`` / ``#{d:var}``
(basename / dirname), ``#{l:text}`` (literal), ``#{n:var}`` (length),
``#{s/pat/rep/:var}`` (regex substitution); escapes ``##`` ``#,`` ``#}``;
and the short aliases ``#S #D #P #I #W #T #H #h #F``. An unknown variable
expands to ``""``, as in tmux. Truth is tmux's: non-empty and not ``"0"``.
"""

from __future__ import annotations

import os
import re
import time
from typing import Callable, List, Optional

__all__ = ["expand", "truthy", "ctime"]

Lookup = Callable[[str], Optional[str]]

_ALIASES = {
    "S": "session_name",
    "D": "pane_id",
    "P": "pane_index",
    "I": "window_index",
    "W": "window_name",
    "T": "pane_title",
    "H": "host",
    "h": "host_short",
    "F": "window_flags",
}

_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def ctime(epoch: float) -> str:
    """``ctime(3)`` without the newline (tmux's ``#{t:…}``): portable, as
    ``%e`` is not on Windows."""
    t = time.localtime(epoch)
    return "%s %s %2d %02d:%02d:%02d %d" % (
        _DAYS[t.tm_wday],
        _MONTHS[t.tm_mon - 1],
        t.tm_mday,
        t.tm_hour,
        t.tm_min,
        t.tm_sec,
        t.tm_year,
    )


def truthy(v: str) -> bool:
    return v != "" and v != "0"


def _close(s: str, i: int) -> int:
    """Index of the ``}`` closing the ``#{`` whose body starts at ``i``."""
    depth = 1
    n = len(s)
    while i < n:
        c = s[i]
        if c == "#" and i + 1 < n:
            if s[i + 1] == "{":
                depth += 1
                i += 2
                continue
            i += 2  # an escape: ##, #}, #,
            continue
        if c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _split_commas(s: str) -> List[str]:
    """Split on the commas that are not inside a nested ``#{…}``."""
    out, depth, cur, i, n = [], 0, [], 0, len(s)
    while i < n:
        c = s[i]
        if c == "#" and i + 1 < n:
            if s[i + 1] == "{":
                depth += 1
            cur.append(s[i : i + 2])
            i += 2
            continue
        if c == "}" and depth:
            depth -= 1
        elif c == "," and depth == 0:
            out.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(c)
        i += 1
    out.append("".join(cur))
    return out


def _value(expr: str, lookup: Lookup) -> str:
    """A variable name, or a nested format, to its value."""
    if "#" in expr:
        return expand(expr, lookup)
    v = lookup(expr)
    return "" if v is None else str(v)


def _number(v: str) -> Optional[float]:
    try:
        return float(v)
    except ValueError:
        return None


def _body(body: str, lookup: Lookup) -> str:
    if body.startswith("?"):
        parts = _split_commas(body[1:])
        cond = parts[0] if parts else ""
        a = parts[1] if len(parts) > 1 else ""
        b = parts[2] if len(parts) > 2 else ""
        return expand(a if truthy(_value(cond, lookup)) else b, lookup)
    m = re.match(r"^(\|\||&&|==|!=|<=|>=|<|>):", body)
    if m:
        op = m.group(1)
        parts = _split_commas(body[m.end() :])
        a = expand(parts[0], lookup) if parts else ""
        b = expand(parts[1], lookup) if len(parts) > 1 else ""
        if op == "||":
            return "1" if truthy(a) or truthy(b) else "0"
        if op == "&&":
            return "1" if truthy(a) and truthy(b) else "0"
        if op in ("==", "!="):
            return "1" if (a == b) == (op == "==") else "0"
        na, nb = _number(a), _number(b)
        x, y = (na, nb) if na is not None and nb is not None else (a, b)
        res = {"<": x < y, ">": x > y, "<=": x <= y, ">=": x >= y}[op]
        return "1" if res else "0"
    m = re.match(r"^=(-?\d+):", body)
    if m:
        n = int(m.group(1))
        v = _value(body[m.end() :], lookup)
        return v[:n] if n >= 0 else v[n:]
    m = re.match(r"^s/((?:[^/\\]|\\.)*)/((?:[^/\\]|\\.)*)/([i]*):", body)
    if m:
        v = _value(body[m.end() :], lookup)
        flags = re.IGNORECASE if "i" in m.group(3) else 0
        try:
            return re.sub(m.group(1), m.group(2).replace("\\/", "/"), v, flags=flags)
        except re.error:
            return v
    m = re.match(r"^([tbdlnq]):", body)
    if m:
        mod, rest = m.group(1), body[m.end() :]
        if mod == "l":
            return rest
        v = _value(rest, lookup)
        if mod == "t":
            try:
                return ctime(float(v))
            except ValueError:
                return ""
        if mod == "b":
            return os.path.basename(v.rstrip("/\\")) or v
        if mod == "d":
            return os.path.dirname(v)
        if mod == "n":
            return str(len(v))
        if mod == "q":
            return re.sub(r"([^A-Za-z0-9_./-])", r"\\\1", v)
    return _value(body, lookup)


def expand(fmt: str, lookup: Lookup) -> str:
    """``fmt`` with every ``#{…}`` (and short alias) replaced."""
    out: List[str] = []
    i, n = 0, len(fmt)
    while i < n:
        c = fmt[i]
        if c != "#" or i + 1 >= n:
            out.append(c)
            i += 1
            continue
        nxt = fmt[i + 1]
        if nxt == "{":
            end = _close(fmt, i + 2)
            if end < 0:
                out.append(fmt[i:])
                break
            out.append(_body(fmt[i + 2 : end], lookup))
            i = end + 1
            continue
        if nxt in "#,}":
            out.append(nxt)
            i += 2
            continue
        if nxt in _ALIASES:
            v = lookup(_ALIASES[nxt])
            out.append("" if v is None else str(v))
            i += 2
            continue
        if nxt == "(":
            # #(shell command): not run by the host (status-line only in tmux).
            end = fmt.find(")", i + 2)
            i = n if end < 0 else end + 1
            continue
        out.append(c)
        i += 1
    return "".join(out)
