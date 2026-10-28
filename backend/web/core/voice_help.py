"""Voice help: a spoken question about MindFlock → a spoken answer from its docs.

The voice router (:mod:`core.voice_router`, kind ``help``) decides a phrase
is a question about MindFlock itself ("how do I schedule a template?", "what
does autopilot do?"); ``POST /api/voice/help`` (routes/voice.py) answers it
here:

1. **Find the passages.** ``docs/*.md`` split at every heading, each
   section's body cut into passages of a few paragraphs (a table row is its
   own passage; code blocks are dropped — nothing in one is read aloud), ranked
   with BM25 over the passage's words plus its headings (the innermost one
   counts :data:`HEADING_WEIGHT` times). The index is built once per process
   and rebuilt when a doc's mtime or size changes.
2. **Word the answer.** ONE headless model call (:func:`_ask_model`:
   ``claude --model`` :data:`HELP_MODEL` ``-p``, MCP servers stripped, the
   neutral one-shot dir as cwd) with the best passages as its only context
   and strict instructions: at most two plain spoken sentences, "I don't
   know" when the passages don't say. :data:`HELP_TIMEOUT_S`; answers are
   kept per question (:data:`CACHE_MAX`) so asking again costs nothing.
3. **Fall back.** No ``claude``, a failure, a timeout, an empty answer — or a
   pytest run, where the model is never asked — and the best passage's first
   sentences are said instead, markdown stripped (``core.spoken``).

Every answer names the section it came from: "From Fast-track: …".
"""

from __future__ import annotations

import math
import os
import re
import sys
import threading
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from backend.web.core import spoken as _spoken

__all__ = ["answer", "docs_dir", "search"]

#: The model that words an answer: small and quick — the passages carry the facts.
HELP_MODEL = "claude-haiku-5-5"
#: Seconds the model call may take before the passage is read instead.
HELP_TIMEOUT_S = 20.0
#: Passages handed to the model, best first.
TOP_PASSAGES = 4
#: Characters of docs in the model's prompt, all passages together.
CONTEXT_MAX = 4000
#: A passage is cut after the paragraph that takes it past this many characters.
PASSAGE_MAX = 700
#: Longest spoken answer (characters).
SAY_MAX = 320
#: Sentences in a spoken answer.
SAY_SENTENCES = 2
#: Answers kept per process: the same question twice asks the model once.
CACHE_MAX = 64
#: How many times the innermost heading's words count against the body's.
HEADING_WEIGHT = 3
#: Docs that describe the code more than the product: their scores are scaled
#: by this, so a user-facing page wins a tie.
DEV_DOCS = {
    "development.md": 0.4,
    "architecture.md": 0.6,
    "web-api.md": 0.6,
    "configuration.md": 0.6,
    "providers.md": 0.6,
    "ingestion-pipeline.md": 0.7,
    "session-engine.md": 0.7,
    "mcp.md": 0.8,
}
#: A section's opening passage says what the thing IS: its score is scaled by
#: this; a reference heading (a code span: a tool, a flag) by REF_HEADING.
FIRST_BONUS = 1.3
REF_HEADING = 0.7
#: The share of the question's words the passage's own heading (or bold
#: label) carries scales its score by up to 1 + this.
HEADING_BONUS = 0.6
#: Said when nothing in the docs matches.
NOT_FOUND = "I couldn't find that in the MindFlock docs."
#: Said when the docs were not found at all (an install without them).
NO_DOCS = "I can't find the MindFlock docs on this computer."
#: Said when the model read the passages and they don't answer it.
DONT_KNOW = "I don't know — the MindFlock docs don't say."

# BM25's constants.
_K1 = 1.2
_B = 0.75

_WORD_RE = re.compile(r"[a-z0-9]+")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")
_TABLE_RE = re.compile(r"^\s*\|")
_TABLE_RULE_RE = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")
_LEAD_RE = re.compile(r"^\s*(?:[-*+]\s+)?\*\*([^*]{2,80})\*\*")
_SENTENCE_RE = re.compile(r"(?<=[.!?])[\"'”’)]*\s+(?=[A-Z0-9\"“(])")
# Heading decorations not worth saying: code spans, emoji/symbol parentheses
# ("(⚙)", "(✉ per pane)"), a trailing "(`module.py`)".
_HEADING_JUNK_RE = re.compile(r"\s*\((?:[^)]*`[^)]*|[^\w\s)][^)]*)\)\s*")

_STOP = frozenset(
    """a an the and or but if then so of to in on at by for with from into onto
    about as is are was were be been being do does did doing done have has had
    i me my we our you your it its it's this that these those there here what
    which who whom whose when where why how can could should would will shall
    may might must not no yes any some all each every one just also very more
    most than too only own same such get got make use using used way thing
    things mindflock tell explain describe please there's what's how's""".split()
)
# Words said only in their docs' word: "add my Mac" is about devices, not the
# ⌘ key of the shortcuts table.
_REPLACED = frozenset(("mac", "laptop", "pc"))
# Spoken words → the words the docs use for the same thing.
_SYNONYMS: Dict[str, Tuple[str, ...]] = {
    "phone": ("mobile",),
    "mobile": ("phone",),
    "mac": ("device",),
    "laptop": ("device",),
    "pc": ("device",),
    "computer": ("device",),
    "pull": ("pr",),
    "schedule": ("cron",),
    "cron": ("schedule",),
    "fence": ("zone",),
    "pr": ("pull",),
    "mic": ("voice",),
    "microphone": ("voice", "mic"),
    "talk": ("voice",),
    "speak": ("voice",),
    "plugin": ("extension", "addon"),
    "addon": ("extension",),
    "shortcut": ("keyboard",),
    "hotkey": ("keyboard", "shortcut"),
    "autopilot": ("fast", "track"),
    "reopen": ("recently", "closed"),
    "restore": ("recently", "closed", "reopen"),
}


def _stem(w: str) -> str:
    """A light suffix strip, so "templates" finds "template" and "scheduled"
    finds "schedule"."""
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 5 and w.endswith("ing"):
        return w[:-3]
    if len(w) > 4 and w.endswith("ed"):
        return w[:-1] if w.endswith("ued") or w.endswith("led") else w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def _terms(text: str) -> List[str]:
    return [
        _stem(w) for w in _WORD_RE.findall(str(text or "").lower()) if w not in _STOP
    ]


def _query_terms(question: str) -> List[str]:
    out: List[str] = []
    for w in _WORD_RE.findall(str(question or "").lower()):
        if w in _STOP:
            continue
        if w not in _REPLACED:
            out.append(_stem(w))
        out.extend(_stem(s) for s in _SYNONYMS.get(w, ()))
    return out


def speakable_heading(heading: str) -> str:
    """A heading as said: no code spans or symbol parentheses, no markdown,
    a colon made a comma ("Voice: talking to a session" → "Voice, talking to
    a session")."""
    h = _HEADING_JUNK_RE.sub(" ", str(heading or ""))
    h = _spoken.strip_markdown(h)
    h = re.sub(r"[^\w\s,.'&/+-]", " ", h.replace(":", ","))
    h = re.sub(r"\s+,", ",", re.sub(r"\s+", " ", h)).strip(" ,.-")
    return h


# --- the index --------------------------------------------------------------- #
class Passage:
    """A few paragraphs (or one table row) of one section."""

    __slots__ = ("doc", "headings", "text", "first", "tf", "length", "head")

    def __init__(
        self, doc: str, headings: Tuple[str, ...], text: str, first: bool = False
    ) -> None:
        self.doc = doc
        self.headings = headings
        self.text = text
        self.first = first
        tf = Counter(_terms(text))
        # A paragraph that opens with a bold label ("**Run now and
        # schedules.** …") is a sub-section of its own: the label weighs as
        # the innermost heading does.
        m = _LEAD_RE.match(text)
        head = set(_terms(headings[-1])) if headings else set()
        if m:
            for t in _terms(m.group(1)):
                tf[t] += HEADING_WEIGHT - 1
                head.add(t)
        self.head = head
        for i, h in enumerate(headings):
            weight = HEADING_WEIGHT if i == len(headings) - 1 else 1
            for t in _terms(h):
                tf[t] += weight
        self.tf = tf
        self.length = sum(tf.values())

    @property
    def heading(self) -> str:
        return self.headings[-1] if self.headings else self.doc


def _passages(doc: str, text: str) -> List[Passage]:
    """``doc``'s passages: paragraphs gathered under their heading path up to
    :data:`PASSAGE_MAX` characters, a table row on its own, code dropped."""
    out: List[Passage] = []
    path: List[Tuple[int, str]] = []
    buf: List[str] = []
    para: List[str] = []
    fenced = False
    intable = False
    fresh = [True]  # nothing of the current section said yet

    def heads() -> Tuple[str, ...]:
        return tuple(h for _, h in path)

    def add(text: str) -> None:
        out.append(Passage(doc, heads(), text, fresh[0]))
        fresh[0] = False

    def flush_buf() -> None:
        if buf:
            add("\n\n".join(buf))
            buf.clear()

    def flush_para() -> None:
        if not para:
            return
        p = "\n".join(para).strip()
        para.clear()
        if not p:
            return
        buf.append(p)
        if sum(len(b) for b in buf) >= PASSAGE_MAX:
            flush_buf()

    for line in text.splitlines():
        if _FENCE_RE.match(line):
            fenced = not fenced
            flush_para()
            continue
        if fenced:
            continue
        m = _HEADING_RE.match(line)
        if m:
            flush_para()
            flush_buf()
            level = len(m.group(1))
            while path and path[-1][0] >= level:
                path.pop()
            path.append((level, m.group(2)))
            fresh[0] = True
            continue
        if _TABLE_RE.match(line):
            flush_para()
            flush_buf()  # the words before the table come before its rows
            # A table's first line is its header: column names, not a passage.
            header = not intable
            intable = True
            if not header and not _TABLE_RULE_RE.match(line):
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                row = " — ".join(c for c in cells if c)
                if row:
                    add(row)
            continue
        intable = False
        if not line.strip():
            flush_para()
            continue
        para.append(line)
    flush_para()
    flush_buf()
    return out


class _Index:
    def __init__(self, sig: tuple, passages: List[Passage]) -> None:
        self.sig = sig
        self.passages = passages
        n = len(passages) or 1
        df: Counter = Counter()
        for p in passages:
            df.update(p.tf.keys())
        self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}
        self.avg = (sum(p.length for p in passages) / n) or 1.0

    def score(self, p: Passage, q: List[str]) -> float:
        s = 0.0
        # Floored: a one-line table row must not out-rank the paragraph that
        # explains it just for being short.
        norm = _K1 * (1 - _B + _B * max(p.length, self.avg) / self.avg)
        for t in q:
            f = p.tf.get(t)
            if f:
                s += self.idf.get(t, 0.0) * f * (_K1 + 1) / (f + norm)
        s *= DEV_DOCS.get(p.doc, 1.0)
        wanted = set(q)
        if wanted:
            s *= 1 + HEADING_BONUS * len(wanted & p.head) / len(wanted)
        if p.first:
            s *= FIRST_BONUS
        if p.heading.lstrip().startswith("`"):
            s *= REF_HEADING
        return s


_LOCK = threading.Lock()
_INDEX: Optional[_Index] = None


def docs_dir() -> Optional[Path]:
    """The folder holding MindFlock's docs: ``$MINDFLOCK_DOCS_DIR``, else the
    ``docs/`` next to the package (a checkout or an editable install), else
    ``$MINDFLOCK_REPO_ROOT/docs``, else the cwd's ``docs/`` (an installed
    copy launched from the repo). None when there is none."""
    env = (os.environ.get("MINDFLOCK_DOCS_DIR") or "").strip()
    cands: List[Path] = [Path(env).expanduser()] if env else []
    cands += [p / "docs" for p in Path(__file__).resolve().parents]
    root = (os.environ.get("MINDFLOCK_REPO_ROOT") or "").strip()
    if root:
        cands.append(Path(root).expanduser() / "docs")
    cands.append(Path.cwd() / "docs")
    for c in cands:
        if (c / "web-ui.md").is_file() or (env and c == cands[0] and c.is_dir()):
            return c
    return None


def _signature(folder: Optional[Path]) -> tuple:
    if folder is None:
        return ()
    out = []
    try:
        for f in sorted(folder.glob("*.md")):
            try:
                st = f.stat()
            except OSError:
                continue
            out.append((f.name, st.st_mtime_ns, st.st_size))
    except OSError:
        return ()
    return (str(folder),) + tuple(out)


def _index() -> _Index:
    """The index, rebuilt when the docs folder or any doc's mtime/size changed."""
    global _INDEX
    folder = docs_dir()
    sig = _signature(folder)
    with _LOCK:
        if _INDEX is not None and _INDEX.sig == sig:
            return _INDEX
        passages: List[Passage] = []
        for name, *_ in sig[1:]:
            try:
                text = (folder / name).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            passages.extend(_passages(name, text))
        _INDEX = _Index(sig, passages)
        return _INDEX


def search(question: str, n: int = TOP_PASSAGES) -> List[Tuple[float, Passage]]:
    """The ``n`` best passages for ``question``, best first, as ``(score,
    passage)``; none scoring zero."""
    q = _query_terms(question)
    if not q:
        return []
    idx = _index()
    scored = [(idx.score(p, q), p) for p in idx.passages]
    scored = [x for x in scored if x[0] > 0]
    scored.sort(key=lambda x: -x[0])
    return scored[:n]


# --- the answer -------------------------------------------------------------- #
class HelpModelError(RuntimeError):
    """The model could not word an answer (missing, failed, timed out)."""


def build_prompt(question: str, passages: List[Passage]) -> str:
    """The one-shot prompt: the rules, the question, the passages."""
    parts = []
    left = CONTEXT_MAX
    for i, p in enumerate(passages, 1):
        body = p.text[:left]
        left -= len(body)
        parts.append("[%d] %s — %s\n%s" % (i, p.doc, " › ".join(p.headings), body))
        if left <= 0:
            break
    return (
        "You answer a spoken question about MindFlock, an app that runs fleets of "
        "coding-agent sessions. Use ONLY the documentation excerpts below. Reply "
        "with at most two short, plain sentences that will be read aloud: no code, "
        "file paths, URLs, markdown, lists or symbols. Say where to click or what to "
        "say when the excerpts tell you. If the excerpts do not answer the "
        "question, reply exactly: I don't know.\n\n"
        "Question: %s\n\nExcerpts:\n\n%s" % (str(question).strip(), "\n\n".join(parts))
    )


def _under_pytest() -> bool:
    return "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules


def _ask_model(prompt: str) -> str:
    """Ask :data:`HELP_MODEL` ``prompt`` headlessly; its stdout. Raises
    :class:`HelpModelError`. Never runs under pytest (tests replace it)."""
    if _under_pytest():
        raise HelpModelError("no model calls under pytest")
    from backend import providers
    from backend.web.core import commit_message as _commit_message

    argv = providers.resolve("claude").oneshot_argv(prompt)
    if not argv:
        raise HelpModelError("claude has no headless mode here")
    argv[1:1] = ["--model", HELP_MODEL]
    try:
        return _commit_message._run(argv, _commit_message.neutral_cwd(), HELP_TIMEOUT_S)
    except _commit_message.CommitMessageError as err:
        raise HelpModelError(str(err)) from err


def _sentences(text: str, n: int = SAY_SENTENCES, limit: int = SAY_MAX) -> str:
    """The first ``n`` sentences of ``text``, cut at a word to ``limit``."""
    s = " ".join(_SENTENCE_RE.split(str(text or "").strip())[:n]).strip()
    if len(s) > limit:
        s = s[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:—-") + "…"
    return s


def _plain(text: str) -> str:
    """Markdown off; arrows and bullets read as words."""
    s = _spoken.strip_markdown(text)
    s = s.replace("→", " then ").replace("·", ",").replace(" — ", ", ")
    return re.sub(r"\s+", " ", s).strip()


def fallback_text(p: Passage) -> str:
    """What is said without a model: the passage's first sentences."""
    return _sentences(_plain(p.text))


def _source(p: Passage) -> dict:
    return {"doc": p.doc, "heading": speakable_heading(p.heading)}


def _from(p: Passage, said: str) -> str:
    h = speakable_heading(p.heading)
    return ("From %s: %s" % (h, said)) if h else said


_CACHE: "OrderedDict[tuple, dict]" = OrderedDict()
_CACHE_LOCK = threading.Lock()


def answer(question: str) -> dict:
    """``{say, source: {doc, heading} | None, model: bool}`` for ``question``.
    Blocking (the model call): run it in a thread."""
    q = " ".join(str(question or "").split())
    if docs_dir() is None:
        return {"say": NO_DOCS, "source": None, "model": False}
    hits = search(q)
    if not hits:
        return {"say": NOT_FOUND, "source": None, "model": False}
    best = hits[0][1]
    key = (q.lower(), _index().sig)
    with _CACHE_LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return dict(_CACHE[key])
    got: Optional[dict] = None
    try:
        out = _plain(_ask_model(build_prompt(q, [p for _, p in hits])))
    except HelpModelError:
        out = ""
    if out:
        said = _sentences(out)
        if re.match(r"(?i)i don'?t know\b", said):
            got = {"say": DONT_KNOW, "source": None, "model": True}
        else:
            got = {"say": _from(best, said), "source": _source(best), "model": True}
    if got is None:
        return {
            "say": _from(best, fallback_text(best)),
            "source": _source(best),
            "model": False,
        }
    with _CACHE_LOCK:
        _CACHE[key] = got
        while len(_CACHE) > CACHE_MAX:
            _CACHE.popitem(last=False)
    return dict(got)
