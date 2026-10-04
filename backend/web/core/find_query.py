"""The Ctrl+F query: what counts as a hit, for every pane find (tmux history
and the scroll-mode index alike — see pane_find / pane_scroll_find).

Options, as in an editor's find bar:

* ``case`` — match case (off: case-insensitive);
* ``word`` — whole words only (not inside a longer word);
* ``regex`` — the text is a regular expression (off: literal);
* ``near`` + ``within`` — proximity: a hit is an occurrence of the text with
  an occurrence of ``near`` on the same line (``within`` 0) or at most
  ``within`` lines away, either direction. The same options apply to both
  terms; the nearest partner is reported so both can be shown.

The frontend builds the same pattern for painting (lib/screenFind), so the
two must stay in step: whole word is a lookaround on word characters, not
``\\b`` (which misbehaves at a term's own punctuation), and literal text is
fully escaped.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass
from typing import Optional

MAX_HITS = 5000


@dataclass(frozen=True)
class Query:
    text: str
    case: bool = False
    word: bool = False
    regex: bool = False
    near: str = ""
    within: int = 0

    @classmethod
    def from_payload(cls, payload: dict) -> "Query":
        try:
            within = max(0, min(1000, int(payload.get("within") or 0)))
        except (TypeError, ValueError):
            within = 0
        return cls(
            text=str(payload.get("query") or ""),
            case=bool(payload.get("case")),
            word=bool(payload.get("word")),
            regex=bool(payload.get("regex")),
            near=str(payload.get("near") or ""),
            within=within,
        )


@dataclass(frozen=True)
class Hit:
    line: int
    col: int
    length: int
    # The nearest occurrence of the ``near`` term, for a proximity hit.
    partner: Optional[tuple[int, int, int]] = None


def compile_term(text: str, q: Query) -> re.Pattern:
    """One term as a pattern. Raises ValueError (a readable message) for an
    invalid regular expression."""
    src = text if q.regex else re.escape(text)
    if q.word:
        src = rf"(?<!\w)(?:{src})(?!\w)"
    try:
        return re.compile(src, 0 if q.case else re.IGNORECASE)
    except re.error as err:
        raise ValueError(f"invalid regular expression: {err.msg}") from None


def _occurrences(
    lines: list[str], pat: re.Pattern, limit: int
) -> list[tuple[int, int, int]]:
    out: list[tuple[int, int, int]] = []
    for i, line in enumerate(lines):
        for m in pat.finditer(line):
            if m.end() == m.start():
                continue  # an empty match isn't a hit
            out.append((i, m.start(), m.end() - m.start()))
            if len(out) >= limit:
                return out
    return out


def find_hits(lines: list[str], q: Query) -> list[Hit]:
    """Every hit of ``q`` in ``lines``, top to bottom. Raises ValueError for
    an invalid pattern; returns [] for an empty query."""
    if not q.text:
        return []
    main = _occurrences(lines, compile_term(q.text, q), MAX_HITS)
    if not q.near:
        return [Hit(ln, col, n) for ln, col, n in main]
    other = _occurrences(lines, compile_term(q.near, q), MAX_HITS * 4)
    keys = [o[0] for o in other]
    out: list[Hit] = []
    for ln, col, n in main:
        lo = bisect.bisect_left(keys, ln - q.within)
        hi = bisect.bisect_right(keys, ln + q.within)
        best = None
        for o in other[lo:hi]:
            if o[0] == ln and o[1] < col + n and col < o[1] + o[2]:
                continue  # the same text can't be its own partner
            key = (abs(o[0] - ln), abs(o[1] - col))
            if best is None or key < best[0]:
                best = (key, o)
        if best is not None:
            out.append(Hit(ln, col, n, best[1]))
    return out
