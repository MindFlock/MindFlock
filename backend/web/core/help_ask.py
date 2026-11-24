"""Ask how something works: a typed question about MindFlock → a short
written answer from its docs, plus the places in the app to point at.

The sidebar's ? Help → "Ask how something works…" (frontend
``components/helpAsk``) sends ``POST /api/help/ask`` (routes/help.py) a
question and the app's pointable places (``[{id, label}]``, the frontend's
``lib/helpPlaces`` registry). It is voice help (:mod:`core.voice_help`)
shaped for READING instead of hearing:

1. **Find the passages** — :mod:`core.voice_help`'s BM25 index over
   ``docs/*.md``, unchanged (``voice_help.search``).
2. **Word the answer** — the same ONE headless model call voice help makes
   (``voice_help._ask_model``: ``claude --model`` ``HELP_MODEL`` ``-p``, MCP
   stripped, the neutral one-shot cwd, its timeout), with a prompt for text:
   a few short sentences or numbered steps, inline code allowed, "I don't
   know" when the passages don't say. The same prompt may name up to
   :data:`POINTERS_MAX` of the places it was given; any id not in that list
   is dropped — the model's output is only ever text and registry ids.
3. **Fall back** — no ``claude``, a failure, a timeout, a pytest run, or
   more than :data:`RATE_CALLS` model calls in :data:`RATE_WINDOW_S`
   (``limited``): the best passage's first sentences, links and bold off,
   inline code kept. Never a guess: nothing matching is "the docs don't
   cover that".

Every answer names the section it came from (``source``: doc, heading, the
heading path and the passage itself, so the pop-up can show it). Worded
answers are cached per question and place list (:data:`CACHE_MAX`).

The spoken path (``voice_help.answer``, ``POST /api/voice/help``) is not
touched: this module only reads from it.
"""

from __future__ import annotations

import math
import re
import threading
import time
from collections import OrderedDict, deque
from typing import Deque, List, Optional, Sequence, Tuple

from backend.web.core import voice_help as _vh

__all__ = ["answer", "clean_places", "parse_reply", "build_prompt"]

#: Longest written answer (characters).
ANSWER_MAX = 700
#: Sentences in an answer read straight from the docs (no model); the whole
#: section is one click away under it.
FALLBACK_SENTENCES = 2
#: Longest fallback answer (characters).
FALLBACK_MAX = 400
#: A fallback sentence (or parenthesis) with at least this share of its
#: characters in code spans is left out of the answer.
CODE_SHARE_MAX = 0.6
#: Longest passage shown under "From …" (characters).
EXCERPT_MAX = 1200
#: Places the model may name in one answer.
POINTERS_MAX = 3
#: Places accepted from a request, and the longest id / label.
PLACES_MAX = 160
PLACE_ID_MAX = 64
PLACE_LABEL_MAX = 90
#: Model calls allowed per window (the rest are answered from the docs alone).
RATE_CALLS = 8
RATE_WINDOW_S = 60.0
#: Model calls allowed at the same time.
INFLIGHT_MAX = 2
#: Worded answers kept per process.
CACHE_MAX = 64
#: Passages read from voice help's ranking before this module re-ranks them.
SEARCH_DEPTH = 16
#: A passage is only an answer when it scores at least this (after the
#: re-rank) AND holds :data:`MIN_COVERAGE` of the question's words (a word
#: or one of its synonyms): a question that only brushes the docs ("how do I
#: bake bread" meets "baked" once) is "the docs don't cover that", not the
#: nearest paragraph.
MIN_SCORE = 3.0
MIN_COVERAGE = 0.5
#: Sections written for whoever builds a feature, not for whoever uses it
#: (a round's contract, as-built notes, CSS class lists, seams): their
#: scores are scaled by this, so the page a user reads wins.
BUILDER_SECTION = 0.45
_BUILDER_RE = re.compile(
    r"(?i)\bcontract\b|\bas built\b|\bas-built\b|worker notes|\bround \d+|css classes|"
    r"\bseams?\b|threat[- ]model|host changes|known gaps|\((?:frontend|backend)\)|"
    r"`[^`]*\.(?:py|ts|tsx|css|json|toml)`"
)

#: Shown when nothing in the docs matches.
NOT_FOUND = "The MindFlock docs don't cover that."
#: Shown when the docs folder is missing (an install without them).
NO_DOCS = (
    "The MindFlock docs aren't on this computer, so there is nothing to answer from."
)
#: Shown when the model read the passages and they don't answer it.
DONT_KNOW = "The MindFlock docs don't say."

_PLACE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]*$")
_PLACES_LINE_RE = re.compile(r"(?im)^\s*\**\s*places\s*\**\s*:\s*(.*?)\s*$")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_BOLD_RE = re.compile(r"(\*\*|__)(.+?)\1")
_ITALIC_RE = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)\*(?![\w*])")
_HEADING_LINE_RE = re.compile(r"(?m)^\s*#{1,6}\s+")
_BULLET_RE = re.compile(r"(?m)^\s*[-*+]\s+")
_CODE_RE = re.compile(r"`([^`]*)`")
_PAREN_RE = re.compile(r"\s*\(([^()]*)\)")
_DONT_KNOW_RE = re.compile(r"(?i)^\W*i\s+don'?t\s+know\b")


# --- places ---------------------------------------------------------------------- #
def clean_places(raw) -> List[Tuple[str, str]]:
    """``[(id, label)]`` from a request's ``places`` — well-formed ids only
    (lower-case, ``[a-z0-9._:-]``), labels one line, at most
    :data:`PLACES_MAX`, first of each id kept. Anything else is dropped."""
    out: List[Tuple[str, str]] = []
    seen = set()
    if not isinstance(raw, list):
        return out
    for item in raw:
        if len(out) >= PLACES_MAX:
            break
        if not isinstance(item, dict):
            continue
        pid = item.get("id")
        label = item.get("label")
        if not isinstance(pid, str) or not isinstance(label, str):
            continue
        pid = pid.strip()
        label = " ".join(label.split())[:PLACE_LABEL_MAX]
        if not label or len(pid) > PLACE_ID_MAX or not _PLACE_ID_RE.match(pid):
            continue
        if pid in seen:
            continue
        seen.add(pid)
        out.append((pid, label))
    return out


# --- text shaping ------------------------------------------------------------------ #
def _readable(text: str) -> str:
    """Markdown made plain for reading: links become their words, bold and
    italics and heading marks go, inline code stays; blank-line paragraphs
    kept, everything else on one line each."""
    s = str(text or "")
    s = _LINK_RE.sub(r"\1", s)
    s = _BOLD_RE.sub(r"\2", s)
    s = _ITALIC_RE.sub(r"\1", s)
    s = _HEADING_LINE_RE.sub("", s)
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in s.splitlines()]
    out: List[str] = []
    for ln in lines:
        if ln or (out and out[-1]):
            out.append(ln)
    return "\n".join(out).strip()


def _cut(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` characters at a line, else a sentence, else
    a word."""
    s = text.strip()
    if len(s) <= limit:
        return s
    head = s[:limit]
    for sep in ("\n", ". "):
        at = head.rfind(sep)
        if at > limit * 0.5:
            return head[: at + (1 if sep == ". " else 0)].rstrip()
    return head.rsplit(" ", 1)[0].rstrip(" ,;:—-") + "…"


def _heading_text(heading: str) -> str:
    """A heading as shown: no markdown, backticks off."""
    h = _readable(heading).replace("`", "")
    return " ".join(h.split())


def _code_share(text: str) -> float:
    """The share of ``text``'s characters inside `code spans`."""
    t = text.strip()
    if not t:
        return 0.0
    return sum(len(m) + 2 for m in _CODE_RE.findall(t)) / len(t)


def fallback_text(text: str) -> str:
    """An answer without a model: the passage's first sentences, read as
    one paragraph (a list's items joined), inline code kept — but a
    sentence or a parenthesis that is mostly code (the files a feature
    lives in, an API path) is left out: it answers a builder, not a
    reader."""
    s = _BULLET_RE.sub("", _readable(text))
    s = " ".join(s.split())
    s = _PAREN_RE.sub(
        lambda m: "" if _code_share(m.group(1)) >= CODE_SHARE_MAX else m.group(0), s
    )
    sentences = [
        x
        for x in _vh._SENTENCE_RE.split(s)
        if x.strip() and _code_share(x) < CODE_SHARE_MAX
    ]
    said = " ".join(sentences[:FALLBACK_SENTENCES]).strip()
    return _cut(said, FALLBACK_MAX)


def _source(p) -> dict:
    return {
        "doc": p.doc,
        "heading": _heading_text(p.heading),
        "path": " › ".join(_heading_text(h) for h in p.headings),
        "excerpt": _cut(_readable(p.text), EXCERPT_MAX),
    }


#: Docs mostly written for whoever runs or builds MindFlock rather than whoever
#: clicks through it: their scores are scaled by this for a written answer
#: (voice help's own DEV_DOCS scaling is applied already).
READER_DOCS = {"cli.md": 0.6, "peer-link.md": 0.6, "devices-anywhere.md": 0.7}
_BUILDER_LEAD_RE = re.compile(r"(?i)^\W*(?:round \d+\b|owner's ask|contract\b)")


def _builder(p) -> bool:
    """A section for whoever builds the feature: its heading says so (a
    round, a contract, as-built notes, CSS classes, a file), or its opening
    words do ("Round 7 of the Sky …")."""
    if any(_BUILDER_RE.search(h) for h in p.headings):
        return True
    return bool(_BUILDER_LEAD_RE.match(p.text))


def _word_groups(question: str) -> List[set]:
    """The question's words as voice help reads them, one set per word: its
    stem and its synonyms' (``voice_help._query_terms``, grouped)."""
    out: List[set] = []
    for w in _vh._WORD_RE.findall(str(question or "").lower()):
        # A bare number ("tab 2", a version) says little about the topic,
        # and one the docs never use would weigh as the rarest word.
        if w in _vh._STOP or w.isdigit():
            continue
        g = set() if w in _vh._REPLACED else {_vh._stem(w)}
        g.update(_vh._stem(x) for x in _vh._SYNONYMS.get(w, ()))
        if g and g not in out:
            out.append(g)
    return out


def _covers(p, groups: List[set], idf: dict, rare: float) -> bool:
    """Whether ``p`` holds :data:`MIN_COVERAGE` of the question's weight:
    each word counts by its rarity in the docs (its idf; a word the docs
    never use counts as the rarest), so "change the theme" is covered by a
    passage on themes and "bake bread" is not by one that says "baked"."""
    if not groups:
        return False
    weights = [max(idf.get(t, rare) for t in g) for g in groups]
    held = sum(w for g, w in zip(groups, weights) if any(t in p.tf for t in g))
    return held >= MIN_COVERAGE * sum(weights)


def search(question: str, n: int = _vh.TOP_PASSAGES) -> list:
    """Voice help's ranking (``voice_help.search``) read :data:`SEARCH_DEPTH`
    deep and re-ranked for a reader: builder sections scaled by
    :data:`BUILDER_SECTION`; a passage under :data:`MIN_SCORE` or short of
    :data:`MIN_COVERAGE` of the question's words dropped. The
    ``n`` best as ``(score, passage)``."""
    hits = _vh.search(question, SEARCH_DEPTH)
    groups = _word_groups(question)
    idx = _vh._index()
    rare = math.log(1 + (len(idx.passages) + 0.5) / 0.5)
    scored = [
        (s * (BUILDER_SECTION if _builder(p) else 1.0) * READER_DOCS.get(p.doc, 1.0), p)
        for s, p in hits
    ]
    scored = [
        x for x in scored if x[0] >= MIN_SCORE and _covers(x[1], groups, idx.idf, rare)
    ]
    scored.sort(key=lambda x: -x[0])
    return scored[:n]


# --- the model --------------------------------------------------------------------- #
def build_prompt(
    question: str, passages: Sequence, places: Sequence[Tuple[str, str]]
) -> str:
    """The one-shot prompt: the rules, the question, the places, the passages."""
    excerpts = _vh.format_excerpts(
        [_vh._item(p, body) for p, body in _vh._budget(list(passages))]
    )
    rules = (
        "You answer a question someone typed into the help box of MindFlock, an app "
        "that runs fleets of coding-agent sessions. Use ONLY the documentation "
        "excerpts below. Reply with at most four short sentences, or at most four "
        "numbered steps when the answer is a procedure. Plain text: no headings, "
        "links, tables, bold or file paths; put `backticks` only around the exact "
        "name of a button, setting or key. Say where to click when the excerpts say. "
        "If the excerpts do not answer the question, reply exactly: I don't know."
    )
    if places:
        rules += (
            "\n\nThen, on its own last line, write PLACES: and up to three ids from "
            "the list of places in the app below that show what the question is "
            "about, comma-separated, best first; write PLACES: none if none fit. "
            "Use only ids from the list."
        )
        listing = "\n".join("%s — %s" % (pid, label) for pid, label in places)
        return (
            "%s\n\nQuestion: %s\n\nPlaces (id — what it is):\n%s\n\nExcerpts:\n\n%s"
            % (
                rules,
                str(question).strip(),
                listing,
                excerpts,
            )
        )
    return "%s\n\nQuestion: %s\n\nExcerpts:\n\n%s" % (
        rules,
        str(question).strip(),
        excerpts,
    )


def parse_reply(raw: str, allowed: Sequence[str]) -> Tuple[str, List[str], bool]:
    """``(text, pointer ids, dont_know)`` from the model's reply. The LAST
    ``PLACES:`` line is the pointer choice (every such line is taken out of
    the text); an id not in ``allowed`` is dropped, at most
    :data:`POINTERS_MAX` kept. The text is made readable and cut to
    :data:`ANSWER_MAX`."""
    s = str(raw or "")
    picked: List[str] = []
    found = _PLACES_LINE_RE.findall(s)
    if found:
        s = _PLACES_LINE_RE.sub("", s)
        ok = set(allowed)
        for tok in re.split(r"[,\s]+", found[-1]):
            tok = tok.strip("`'\"()[].;").lower()
            if tok in ok and tok not in picked:
                picked.append(tok)
            if len(picked) >= POINTERS_MAX:
                break
    text = _readable(s)
    text = re.sub(r"(?i)^answer\s*:\s*", "", text).strip()
    if _DONT_KNOW_RE.match(text):
        return "", [], True
    return _cut(text, ANSWER_MAX), picked, False


def _ask_model(prompt: str) -> str:
    """Voice help's model call (``voice_help._ask_model``): never runs under
    pytest. Module-level so tests replace it."""
    return _vh._ask_model(prompt)


class _Gate:
    """At most :data:`RATE_CALLS` model calls per :data:`RATE_WINDOW_S` and
    :data:`INFLIGHT_MAX` at once; past either, the answer comes from the
    docs alone."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.stamps: Deque[float] = deque()
        self.inflight = 0

    def enter(self) -> bool:
        now = time.monotonic()
        with self.lock:
            while self.stamps and now - self.stamps[0] > RATE_WINDOW_S:
                self.stamps.popleft()
            if len(self.stamps) >= RATE_CALLS or self.inflight >= INFLIGHT_MAX:
                return False
            self.stamps.append(now)
            self.inflight += 1
            return True

    def leave(self) -> None:
        with self.lock:
            self.inflight = max(0, self.inflight - 1)

    def reset(self) -> None:
        with self.lock:
            self.stamps.clear()
            self.inflight = 0


_GATE = _Gate()
_CACHE: "OrderedDict[tuple, dict]" = OrderedDict()
_CACHE_LOCK = threading.Lock()


def answer(question: str, places=None) -> dict:
    """``{text, found, source, model, pointers, limited}`` for ``question``.

    ``found`` is False when the docs don't answer it (``text`` then says
    so); ``source`` is ``{doc, heading, path, excerpt}`` or None;
    ``pointers`` are ids from ``places`` (``[{id, label}]``) the model
    named — [] without one; ``limited`` is True when the model was skipped
    because too many questions came at once. Blocking: run it in a thread."""
    q = " ".join(str(question or "").split())
    allowed = clean_places(places)
    ids = [pid for pid, _ in allowed]
    base = {"model": False, "pointers": [], "limited": False}
    if _vh.docs_dir() is None:
        return dict(base, text=NO_DOCS, found=False, source=None)
    hits = search(q)
    if not hits:
        return dict(base, text=NOT_FOUND, found=False, source=None)
    best = hits[0][1]
    key = (q.lower(), _vh._index().sig, tuple(ids))
    with _CACHE_LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return dict(_CACHE[key])
    got: Optional[dict] = None
    limited = False
    if _GATE.enter():
        try:
            raw = _ask_model(build_prompt(q, [p for _, p in hits], allowed))
        except _vh.HelpModelError:
            raw = ""
        finally:
            _GATE.leave()
        if raw.strip():
            text, picked, dont_know = parse_reply(raw, ids)
            if dont_know:
                got = dict(base, model=True, text=DONT_KNOW, found=False, source=None)
            elif text:
                got = dict(
                    base,
                    model=True,
                    text=text,
                    found=True,
                    source=_source(best),
                    pointers=picked,
                )
    else:
        limited = True
    if got is None:
        return dict(
            base,
            text=fallback_text(best.text) or NOT_FOUND,
            found=True,
            source=_source(best),
            limited=limited,
        )
    with _CACHE_LOCK:
        _CACHE[key] = got
        while len(_CACHE) > CACHE_MAX:
            _CACHE.popitem(last=False)
    return dict(got)


def _reset_for_tests() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()
    _GATE.reset()
