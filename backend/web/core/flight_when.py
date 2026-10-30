"""``when``: a condition on every flight stop (docs/flights.md "Round 15").

A step may carry ``when: "<expression>"``; the planner skips a pending step
whose condition is false. This module is the grammar and nothing else — pure,
no I/O, no flights import — so the normaliser, the planner, the route and
the driver all read one parser:

* :func:`parse` → a :class:`Cond` (a nested tuple), raising :class:`WhenError`
  with a message and a hint (what the editor shows under the field),
* :func:`canonical` → the one spelling a condition is stored as,
* :func:`evaluate` → ``(bool, why)`` against the facts the driver gathered,
* :func:`needs` → which fact groups the driver must gather (``diff``,
  ``labels``, ``clock``, ``branch``, ``judge``) so a clock-only condition
  never runs git or asks GitHub.

The words, case-insensitive, ``and`` tighter than ``or``, ``not`` a prefix,
parentheses allowed::

    diff touches <glob>[, <glob>…]     diff lines > N | < N     files > N | < N
    weekday | weekend                  hour between H and H     branch matches <glob>
    label <name>                       judge is <v> | judge <sid> is <v>
    always | never

A condition is a tuple whose first item names it: ``("always",)``,
``("never",)``, ``("not", c)``, ``("and", a, b)``, ``("or", a, b)``,
``("touches", [globs])``, ``("lines", ">", n)``, ``("files", "<", n)``,
``("weekday",)``, ``("weekend",)``, ``("hour", h1, h2)``, ``("branch",
glob)``, ``("label", name)``, ``("judge", sid_or_empty, value)``.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Set, Tuple

__all__ = [
    "Cond",
    "WhenError",
    "MAX_LEN",
    "GROUPS",
    "parse",
    "canonical",
    "evaluate",
    "needs",
    "matches",
]

Cond = tuple

#: The longest expression a step keeps (the normaliser's cap).
MAX_LEN = 300

#: The fact groups :func:`needs` names.
GROUPS = ("diff", "labels", "clock", "branch", "judge")

_HINT = (
    "diff touches <glob>, diff lines > N, files < N, weekday, weekend, "
    "hour between H and H, branch matches <glob>, label <name>, "
    "judge is <value>, always, never — joined with and / or, not in front"
)

_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

_TOKEN_RE = re.compile(r"\(|\)|,|>=?|<=?|=|[^\s(),<>=]+")
_KEYWORDS = frozenset({"and", "or", "not", ")", "(", ","})
_END_OF_WORDS = frozenset({"and", "or", ")"})


class WhenError(ValueError):
    """An expression that is not a condition: ``message`` says what is wrong,
    ``hint`` lists the words that would be right."""

    def __init__(self, message: str, hint: str = _HINT) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class _Parser:
    def __init__(self, text: str) -> None:
        self.toks: List[str] = _TOKEN_RE.findall(text)
        self.i = 0

    # -- token helpers -------------------------------------------------------
    def peek(self) -> Optional[str]:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def peek_l(self) -> Optional[str]:
        t = self.peek()
        return t.lower() if t is not None else None

    def take(self) -> str:
        t = self.peek()
        if t is None:
            raise WhenError("the condition ends too early")
        self.i += 1
        return t

    def expect(self, word: str) -> None:
        got = self.peek_l()
        if got != word:
            raise WhenError(
                "expected '%s' here, found %s"
                % (word, ("'%s'" % self.peek()) if got is not None else "the end")
            )
        self.i += 1

    def words_until_keyword(self, what: str) -> str:
        """Tokens up to the next and / or / ) / end, joined with a space (a
        label or a judge value may hold spaces)."""
        out = []
        while self.peek() is not None and self.peek_l() not in _END_OF_WORDS:
            t = self.take()
            if t in ("(", ","):
                raise WhenError("'%s' cannot be part of %s" % (t, what))
            out.append(t)
        if not out:
            raise WhenError("%s is missing" % what)
        return " ".join(out)

    def number(self, what: str) -> int:
        t = self.take()
        if not re.fullmatch(r"\d{1,9}", t):
            raise WhenError("%s needs a whole number, not '%s'" % (what, t))
        return int(t)

    def compare(self, what: str) -> Tuple[str, int]:
        op = self.take()
        if op not in (">", "<"):
            raise WhenError("%s is compared with > or <, not '%s'" % (what, op))
        return op, self.number(what)

    def globs(self) -> List[str]:
        out = []
        while True:
            t = self.peek()
            if t is None or t.lower() in _END_OF_WORDS or t in ("(", ","):
                break
            out.append(self.take())
            if self.peek() == ",":
                self.i += 1
                continue
            break
        if not out:
            raise WhenError("diff touches needs at least one path or glob")
        return out

    # -- grammar -------------------------------------------------------------
    def parse(self) -> Cond:
        if not self.toks:
            raise WhenError("the condition is empty")
        cond = self.disjunction()
        if self.peek() is not None:
            raise WhenError("unexpected '%s'" % self.peek())
        return cond

    def disjunction(self) -> Cond:
        left = self.conjunction()
        while self.peek_l() == "or":
            self.i += 1
            left = ("or", left, self.conjunction())
        return left

    def conjunction(self) -> Cond:
        left = self.unary()
        while self.peek_l() == "and":
            self.i += 1
            left = ("and", left, self.unary())
        return left

    def unary(self) -> Cond:
        t = self.peek_l()
        if t == "not":
            self.i += 1
            return ("not", self.unary())
        if t == "(":
            self.i += 1
            inner = self.disjunction()
            self.expect(")")
            return inner
        return self.atom()

    def atom(self) -> Cond:
        t = self.take()
        w = t.lower()
        if w == "always":
            return ("always",)
        if w == "never":
            return ("never",)
        if w == "weekday":
            return ("weekday",)
        if w == "weekend":
            return ("weekend",)
        if w == "diff":
            if self.peek() is None:
                raise WhenError("after 'diff' comes 'touches' or 'lines'")
            sub = self.take().lower()
            if sub == "touches":
                return ("touches", self.globs())
            if sub == "lines":
                op, n = self.compare("diff lines")
                return ("lines", op, n)
            raise WhenError("after 'diff' comes 'touches' or 'lines', not '%s'" % sub)
        if w == "files":
            op, n = self.compare("files")
            return ("files", op, n)
        if w == "hour":
            self.expect("between")
            h1 = self.number("hour")
            self.expect("and")
            h2 = self.number("hour")
            if h1 > 24 or h2 > 24:
                raise WhenError("an hour is 0 to 24")
            return ("hour", h1 % 24, h2 if h2 == 24 else h2 % 24)
        if w == "branch":
            self.expect("matches")
            if self.peek() is None or self.peek_l() in _END_OF_WORDS:
                raise WhenError("branch matches needs a name or glob")
            return ("branch", self.take())
        if w == "label":
            return ("label", self.words_until_keyword("the label's name"))
        if w == "judge":
            sid = ""
            if self.peek_l() != "is":
                if self.peek() is None or self.peek_l() in _END_OF_WORDS:
                    raise WhenError("judge is <value>, or judge <stop id> is <value>")
                sid = self.take()
            self.expect("is")
            return (
                "judge",
                sid,
                self.words_until_keyword("the judge's answer").lower(),
            )
        if w in _KEYWORDS:
            raise WhenError("a condition cannot start with '%s'" % t)
        raise WhenError("'%s' is not a condition" % t)


def parse(text: str) -> Cond:
    """``text`` as a :class:`Cond`; raises :class:`WhenError`."""
    s = str(text or "").strip()
    if not s:
        raise WhenError("the condition is empty")
    if len(s) > MAX_LEN:
        raise WhenError("a condition is at most %d characters" % MAX_LEN)
    return _Parser(s).parse()


# --------------------------------------------------------------------------- #
# Canonical spelling
# --------------------------------------------------------------------------- #
def canonical(cond: Cond) -> str:
    """The one spelling of ``cond``: lower-case words, one space, ``and``
    chains flat, parentheses only where ``or`` sits under ``and`` or ``not``."""
    kind = cond[0]
    if kind == "always":
        return "always"
    if kind == "never":
        return "never"
    if kind == "weekday":
        return "weekday"
    if kind == "weekend":
        return "weekend"
    if kind == "touches":
        return "diff touches " + ", ".join(cond[1])
    if kind == "lines":
        return "diff lines %s %d" % (cond[1], cond[2])
    if kind == "files":
        return "files %s %d" % (cond[1], cond[2])
    if kind == "hour":
        return "hour between %d and %d" % (cond[1], cond[2])
    if kind == "branch":
        return "branch matches " + cond[1]
    if kind == "label":
        return "label " + cond[1]
    if kind == "judge":
        return (
            ("judge %s is %s" % (cond[1], cond[2]))
            if cond[1]
            else "judge is " + cond[2]
        )
    if kind == "not":
        inner = cond[1]
        text = canonical(inner)
        return "not (%s)" % text if inner[0] in ("and", "or") else "not " + text
    if kind == "and":
        parts = []
        for side in cond[1:]:
            text = canonical(side)
            parts.append("(%s)" % text if side[0] == "or" else text)
        return " and ".join(parts)
    if kind == "or":
        return " or ".join(canonical(side) for side in cond[1:])
    raise WhenError("unknown condition %r" % (kind,))


# --------------------------------------------------------------------------- #
# What the driver must gather
# --------------------------------------------------------------------------- #
_GROUP_OF = {
    "touches": "diff",
    "lines": "diff",
    "files": "diff",
    "weekday": "clock",
    "weekend": "clock",
    "hour": "clock",
    "branch": "branch",
    "label": "labels",
    "judge": "judge",
}


def needs(cond: Cond) -> Set[str]:
    """The fact groups ``cond`` reads: a subset of :data:`GROUPS`."""
    kind = cond[0]
    if kind in ("not", "and", "or"):
        out: Set[str] = set()
        for side in cond[1:]:
            out |= needs(side)
        return out
    group = _GROUP_OF.get(kind)
    return {group} if group else set()


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #
def _glob_re(pattern: str) -> "re.Pattern":
    out = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            if pattern[i : i + 2] == "**":
                i += 2
                if pattern[i : i + 1] == "/":
                    i += 1
                    out.append("(?:.*/)?")  # ``a/**/b`` also matches ``a/b``
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def matches(path: str, pattern: str) -> bool:
    """Whether changed file ``path`` (repo-relative, ``/`` separated) matches
    one ``diff touches`` glob: ``frontend/**`` and ``frontend/`` mean under
    that folder, ``*.sql`` means any file with that name shape anywhere, a
    pattern with a ``/`` is matched against the whole path (``*`` never
    crosses a ``/``, ``**`` does), a bare name is that file or folder."""
    p = str(pattern or "").strip().lstrip("./")
    path = str(path or "").strip().lstrip("./")
    if not p or not path:
        return False
    if p.endswith("/"):
        p = p.rstrip("/")
        return path == p or path.startswith(p + "/") or ("/" + p + "/") in path
    if "*" not in p and "?" not in p:
        if "/" in p:
            return path == p or path.startswith(p + "/")
        return (
            path == p
            or path.startswith(p + "/")
            or path.endswith("/" + p)
            or ("/" + p + "/") in path
        )
    rx = _glob_re(p)
    if "/" in p:
        return bool(rx.match(path))
    return bool(rx.match(path.rsplit("/", 1)[-1])) or bool(rx.match(path))


def _n(facts: Dict, key: str) -> int:
    try:
        return int(facts.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _cmp(op: str, have: int, want: int) -> bool:
    return have > want if op == ">" else have < want


def _eval_atom(cond: Cond, facts: Dict) -> Tuple[bool, str]:
    kind = cond[0]
    if kind == "always":
        return True, "always"
    if kind == "never":
        return False, "never"
    if kind == "touches":
        files = [str(f) for f in (facts.get("files") or [])]
        for f in files:
            for g in cond[1]:
                if matches(f, g):
                    return True, "diff touches " + f
        if not files:
            return False, "the diff touches no file"
        return False, "no changed file matches " + ", ".join(cond[1])
    if kind == "lines":
        n = _n(facts, "added") + _n(facts, "removed")
        return _cmp(cond[1], n, cond[2]), "%d line%s changed" % (
            n,
            "" if n == 1 else "s",
        )
    if kind == "files":
        n = len(facts.get("files") or [])
        return _cmp(cond[1], n, cond[2]), "%d file%s changed" % (
            n,
            "" if n == 1 else "s",
        )
    if kind in ("weekday", "weekend"):
        day = _n(facts, "weekday")
        name = _DAYS[day % 7]
        weekend = day % 7 >= 5
        return (weekend if kind == "weekend" else not weekend), "it is " + name
    if kind == "hour":
        hour = _n(facts, "hour") % 24
        h1, h2 = cond[1], cond[2]
        if h1 < h2:
            ok = h1 <= hour < h2
        elif h1 == h2:
            ok = True
        else:
            ok = hour >= h1 or hour < h2
        return ok, "it is %02d:00" % hour
    if kind == "branch":
        branch = str(facts.get("branch") or "")
        if not branch:
            return False, "no branch"
        return bool(_glob_re(cond[1]).match(branch)) or branch == cond[1], (
            "branch is " + branch
        )
    if kind == "label":
        labels = [str(l) for l in (facts.get("labels") or [])]
        want = cond[1].lower()
        if any(l.lower() == want for l in labels):
            return True, "label " + cond[1]
        if not labels:
            return False, "no labels"
        return False, "labels: " + ", ".join(labels)
    if kind == "judge":
        judges = facts.get("judges") or {}
        if cond[1]:
            value = judges.get(cond[1])
            who = "judge %s" % cond[1]
        else:
            value = facts.get("judge_last")
            who = "the judge"
        if value is None or value == "":
            return False, who + " has not answered"
        value = str(value).lower()
        return value == cond[2], "%s answered %s" % (who, value)
    raise WhenError("unknown condition %r" % (kind,))


def evaluate(cond: Cond, facts: Optional[Dict]) -> Tuple[bool, str]:
    """``(true?, why)`` for ``cond`` against ``facts`` (``files``, ``added``,
    ``removed``, ``branch``, ``labels``, ``hour``, ``weekday`` 0-6 Monday
    first, ``judges`` ``{sid: value}``, ``judge_last``). A missing fact reads
    as empty / zero. ``why`` is one short fact ("diff touches
    frontend/App.tsx", "it is Saturday")."""
    facts = facts if isinstance(facts, dict) else {}
    kind = cond[0]
    if kind == "not":
        ok, why = evaluate(cond[1], facts)
        return (not ok), why
    if kind == "and":
        whys = []
        for side in cond[1:]:
            ok, why = evaluate(side, facts)
            if not ok:
                return False, why
            whys.append(why)
        return True, "; ".join(whys)[:200]
    if kind == "or":
        whys = []
        for side in cond[1:]:
            ok, why = evaluate(side, facts)
            if ok:
                return True, why
            whys.append(why)
        return False, "; ".join(whys)[:200]
    return _eval_atom(cond, facts)
