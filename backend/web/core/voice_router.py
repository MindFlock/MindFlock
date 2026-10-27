"""The voice router: one finished phrase → a command, or a prompt.

``POST /api/voice/route`` (routes/voice.py) hands every phrase the person
says to :func:`answer`; the phone page (``/m``) and the desktop mic both call
it, so the grammar lives here once. The phone keeps its own table
(mobile.js ``VOICE_CMD``) only as an offline fallback, and the rules below are
a port of it: the same normalisation (:func:`norm`), the same session
matcher (prefix → word overlap → loose containment, :func:`match_session`),
the same attention rule as the rail's bell (:func:`session_state`) and the
same sentences. Answers to a session's dialog ("yes", "docs, always", "tell
session three no") use the phone's dialog words too (mobile.js
``VOICE_YES`` … ``voiceParseAnswer``, :func:`parse_answer`).

Sessions said by number ("session three", "next one") are numbered by the
CLIENT (the desktop's rail order, the phone's list order): the answer
carries ``index`` / ``delta`` and the client maps it, sending the title back
as ``title`` when it wants the rest (a dialog's keys, a ``say``).

Pure: functions take the ``/api/instances`` rows (``title``, ``name``,
``activity``, ``activity_since``, ``last_say``, ``last_turn``, …), the known
repositories, a session's ``/dialog`` body and the text, and return the
answer dict. No I/O — the route gathers the rows, reads the dialog, resolves
the title against the registry and creates the session. Nothing here
presses a key: an answer carries the keys, and the client posts them to
``/answer``.

The default is ``{"kind": "prompt"}``: anything that is not plainly a command
is words for the current session. A false command is worse than a missed one
(the missed one still reaches the agent), so every pattern matches the WHOLE
phrase.
"""

from __future__ import annotations

import re
import time
from functools import lru_cache
from typing import Iterable, List, Optional, Tuple

from backend.web.core import voice_words as _voice_words
from backend.web.core.session_create import title_from_prompt
from backend.web.core.spoken import strip_markdown

__all__ = [
    "LOCAL_KINDS",
    "answer",
    "choice_of",
    "command",
    "dialog_choices",
    "dialog_speech",
    "finish",
    "is_select",
    "match_repo",
    "match_session",
    "norm",
    "parse_answer",
    "plan_new",
    "select_answer",
    "select_choices",
    "select_route",
    "select_speech",
    "session_state",
    "started",
    "title_base",
]

#: Commands the client carries out itself: the answer has no ``say``.
LOCAL_KINDS = ("cancel", "stop", "hush", "off", "repeat", "condense")
#: At most this many names in a "Which one" / "No session called" sentence.
MAX_NAMES = 6
#: At most this many sessions read out by "what needs me".
MAX_NEEDS = 3
#: At most this many sessions read out by "list sessions".
MAX_LIST = 10
#: Idle this long on uncommitted work and the bell lists it as stuck
#: (mobile.js ``STUCK_IDLE_S``).
STUCK_IDLE_S = 20 * 60
#: Longest title made from a "called <name>" (the New dialog's cap).
TITLE_MAX = 32
#: Words of the task a title is made from.
TITLE_WORDS = 4
#: The title when there is neither a name nor a task.
DEFAULT_TITLE = "voice-session"

# Spoken counts: "session three", "number 12", "the second one". Ordinals
# stop at tenth; digits run to 99.
_NUMS = {
    w: i + 1
    for i, w in enumerate(
        "one two three four five six seven eight nine ten eleven twelve thirteen"
        " fourteen fifteen sixteen seventeen eighteen nineteen twenty".split()
    )
}
_ORDS = {
    w: i + 1
    for i, w in enumerate(
        "first second third fourth fifth sixth seventh eighth ninth tenth".split()
    )
}
_NUM_RX = r"(?:[1-9][0-9]?|" + "|".join(_NUMS) + ")"
_ORD_RX = r"(?:[1-9][0-9]?(?:st|nd|rd|th)|" + "|".join(_ORDS) + ")"
# The verbs that move the focus: "(switch|go|…) (over )?to …" ("go back to the
# old approach" stays words for the agent).
_VERB_RX = r"(?:switch|go|change|jump|focus|open|show|select) (?:over )?to"


def norm(text) -> str:
    """mobile.js ``voiceNorm``: lowercase, curly quotes straightened, anything
    but letters, digits, ``'`` and spaces made a space, spaces collapsed."""
    s = str(text or "").lower().replace("’", "'").replace("`", "'")
    s = re.sub(r"[^a-z0-9' ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# A default "stop" / "hush" phrase (core.voice_words.DEFAULTS) → the spoken
# variants heard while that phrase is in the person's list; any other phrase
# (a default without variants, or one the person added) is matched whole.
_VARIANTS = {
    "stop listening": r"stop (?:listening|recording|dictating|the mic|the microphone)",
    "mic off": (
        r"(?:mic|microphone) off"
        r"|turn (?:off the (?:mic|microphone)|the (?:mic|microphone) off)"
    ),
    "that's all": r"that'?s all|that is all",
    "done talking": r"(?:i'?m |i am )?done talking",
    "stop talking": r"stop (?:talking|speaking|reading|saying that)",
    "enough": r"(?:that'?s |that is )?enough|enough (?:talking|of that)",
}
#: The kinds whose words the person may change (``voice.words``).
_WORD_KINDS = ("hush", "stop")


@lru_cache(maxsize=32)
def _words_rx(phrases: Tuple[str, ...]) -> "re.Pattern":
    """The whole-phrase pattern for one kind's ``phrases``: each default's
    :data:`_VARIANTS`, the rest as said (normalised like the text, an
    apostrophe optional)."""
    parts = [_VARIANTS[p] for p in phrases if p in _VARIANTS]
    literal = [norm(p) for p in phrases if p not in _VARIANTS]
    alt = _voice_words.phrase_regex([p for p in literal if p])
    parts.append(alt.replace("'", "'?"))
    return re.compile(r"^(?:%s)$" % "|".join(parts))


def _kind_rx(kind: str, words: Optional[dict]) -> "re.Pattern":
    """``kind``'s pattern for the effective ``words`` (None = the defaults)."""
    got = (words or {}).get(kind)
    if not isinstance(got, (list, tuple)) or not got:
        got = _voice_words.DEFAULTS[kind]
    return _words_rx(tuple(str(p) for p in got))


# The phone's table, in its order (mobile.js VOICE_CMD), plus "list" and the
# wider "stop" (the client stops listening; typed text stays). "hush" and
# "stop" are the person's words (``voice.words``): :func:`command` swaps in
# their patterns; these are the defaults'.
_CMDS = [
    ("cancel", re.compile(r"^(?:cancel|never ?mind|scratch that|forget it)$")),
    ("switch", re.compile(r"^%s (.+)$" % _VERB_RX)),
    (
        "list",
        re.compile(
            r"^(?:(?:what|which) sessions (?:do i have|are (?:up|open|running))"
            r"|list (?:the |my )?sessions"
            r"|what(?:'?s| is) (?:up|open|running))$"
        ),
    ),
    ("needs", re.compile(r"^(?:what|who) needs (?:me|attention|my attention)$")),
    (
        "status",
        re.compile(
            r"^(?:what'?s it doing|what is it doing|status|what'?s happening"
            r"|what'?s going on)$"
        ),
    ),
    (
        "condense",
        re.compile(r"^(?:summari[sz]e(?: that| it)?|what did it say|summary)$"),
    ),
    ("repeat", re.compile(r"^(?:say that again|repeat(?: that)?)$")),
    ("off", re.compile(r"^(?:voice off|turn voice off|turn off voice)$")),
    # "Stop talking": the client cuts what is being said and drops the queue,
    # and keeps listening if it was — unlike "stop", which stops the mic.
    ("hush", _kind_rx("hush", None)),
    ("stop", _kind_rx("stop", None)),
]

# "switch to session three", "go to 2", "session three", "number 4", "the
# third session" — the client's own numbering (its rail / list order).
_INDEX_RES = [
    re.compile(r"^%s (?:(?:session|window|number) )?(%s)$" % (_VERB_RX, _NUM_RX)),
    re.compile(r"^%s the (%s) (?:one|session|window)$" % (_VERB_RX, _ORD_RX)),
    re.compile(r"^(?:session|window|number) (%s)$" % _NUM_RX),
    re.compile(r"^the (%s) (?:session|window)$" % _ORD_RX),
]
# "the second one" alone: an option when the session is asking, else a session.
_PICK_RE = re.compile(r"^(?:the )?(%s) one$" % _ORD_RX)
# "next session", "go to the previous one", "go back".
_REL_RE = re.compile(
    r"^(?:(?:%s )?(?:the )?(next|previous|prev|last) (?:session|one|window)"
    r"|go (back))$" % _VERB_RX
)
# "what is it asking", "what does docs want", "read the question".
_QUESTION_RE = re.compile(
    r"^(?:what(?:'?s| is) (?P<a>.+?) asking(?: me)?(?: for| about)?"
    r"|what does (?P<b>.+?) want(?: me to do)?"
    r"|what(?:'?s| is) the question"
    r"|read(?: me)? (?:the|its|it's) question(?: again)?"
    r"|read (?:that|it) again)$"
)

# --- answers: the phone's dialog words (mobile.js VOICE_YES … VOICE_NUM) -------
_YES_RE = re.compile(
    r"\b(?:yes|yeah|yep|yup|allow|approve|okay|ok|go ahead|do it|sure)\b"
)
_NO_RE = re.compile(r"\b(?:no|nope|deny|reject|don'?t|cancel|skip|stop)\b")
# "always" must be SAID, and said as the whole answer: "always", "yes,
# always", "always allow that", "yes and don't ask again".
_ALWAYS_RE = re.compile(
    r"^(?:(?:yes|yeah|yep|yup|ok|okay) ?)?(?:and )?"
    r"(?:always(?: allow(?: (?:this|it|that|all))?)?"
    r"|don'?t ask(?: me)? again|do not ask(?: me)? again|never ask(?: me)? again)$"
)
_PNUM_RE = re.compile(
    r"\b(one|first|1|two|second|2|three|third|3|four|fourth|4|five|fifth|5|six|sixth"
    r"|6|seven|seventh|7|eight|eighth|8|nine|ninth|9)\b"
)
_PNUM = {
    w: (i // 3) + 1
    for i, w in enumerate(
        "one first 1 two second 2 three third 3 four fourth 4 five fifth 5 six sixth 6"
        " seven seventh 7 eight eighth 8 nine ninth 9".split()
    )
}
#: mobile.js ``VOICE_ORD``: options read out by number.
_ORD_WORDS = ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine")
# Every word an answer may be made of — a phrase with any other word in it is
# not an answer ("yes, but run the tests first" is words for the session).
# Numbers count only after option/choice ("option two") or as an ordinal
# before one/option/choice ("the second one").
_BODY_WORDS = frozenset(
    "yes yeah yep yup sure ok okay approve approved allow allowed go ahead do it"
    " that this all no nope deny denied reject don't dont not skip cancel stop"
    " always and ask me again never thanks thank you".split()
)
_PICK_NOUNS = ("one", "option", "choice")
# Who "it" is: the session being talked to.
_SELF = frozenset(
    ("it", "him", "her", "them", "the agent", "the session", "this session", "this one")
)
_NOUNS = ("session", "window", "number")

# "(start|open|create|make|spin up)? (a|another)? new session (called|named
# <name>)? (in|for|on <repo>)? (to|and|that <task>)?" — on the normalised
# text, where ":", "—" and "," are already spaces (so "new session: fix it"
# is the bare form below).
_NEW_RE = re.compile(
    r"^(?:(?:start|open|create|make|spin up) )?(?:(?:a|another) )?new session"
    r"(?: (?:called|named) (?P<name>.+?))?"
    r"(?: (?P<prep>in|for|on) (?P<repo>.+?))?"
    r"(?: (?:to|and|that) (?P<task>.+))?$"
)
# The bare "new session <task>".
_NEW_BARE_RE = re.compile(r"^new session (?P<task>.+)$")
_LEAD = ("ok", "okay", "hey", "please")
# Words around a spoken repository name that are not part of it.
_REPO_LEAD = ("the", "my", "our")
_REPO_TAIL = ("repo", "repository", "project", "folder", "codebase", "checkout")


def _end(text) -> str:
    """mobile.js ``endSentence``: a full stop unless it ends in one."""
    s = str(text or "").strip()
    return s if not s or s[-1] in ".!?" else s + "."


# --- words ↔ the original text ---------------------------------------------
class _Words:
    """The phrase as normalised words, each remembering the whitespace token
    of the ORIGINAL text it came from, so a task is handed on as it was said
    ("fix auth.py", not "fix auth py"). The phone's lead word (ok/okay/hey/
    please) and trailing "please" are dropped, as in ``voiceCommand``."""

    def __init__(self, text) -> None:
        self.toks = str(text or "").split()
        words: List[str] = []
        owner: List[int] = []
        for i, tok in enumerate(self.toks):
            for w in norm(tok).split():
                words.append(w)
                owner.append(i)
        if len(words) > 1 and words[0] in _LEAD:
            words, owner = words[1:], owner[1:]
        if len(words) > 1 and words[-1] == "please":
            words, owner = words[:-1], owner[:-1]
        self.words = words
        self.owner = owner
        self.text = " ".join(words)

    def at(self, offset: int) -> int:
        """The word index of character ``offset`` in :attr:`text`."""
        return self.text[:offset].count(" ")

    def original(self, start: int, end: Optional[int] = None) -> str:
        """Words ``start``..``end`` as the person said them (whole tokens),
        stray leading punctuation (":", "—") and a trailing comma dropped."""
        end = len(self.words) if end is None else end
        if start >= end:
            return ""
        out = " ".join(self.toks[self.owner[start] : self.owner[end - 1] + 1])
        return out.strip().lstrip(":;,—–-").strip().rstrip(",;:").strip()


def _parse_new(w: _Words) -> Optional[dict]:
    """The "new session" phrase → word spans, or None.

    ``{"name": (a, b) | None, "prep": str, "repo": (a, b) | None, "task": a |
    None}`` — word indexes into ``w.words``; the task runs to the end."""
    m = _NEW_RE.match(w.text) or _NEW_BARE_RE.match(w.text)
    if m is None:
        return None

    def span(group: str):
        if group not in m.re.groupindex or m.group(group) is None:
            return None
        return (w.at(m.start(group)), w.at(m.end(group)) + 1)

    task = span("task")
    return {
        "name": span("name"),
        "prep": m.groupdict().get("prep") or "",
        "repo": span("repo"),
        "task": task[0] if task else None,
    }


def _count(word: str) -> Optional[int]:
    """``"three"`` / ``"3"`` / ``"third"`` / ``"3rd"`` → 3, else None."""
    m = re.fullmatch(r"([1-9][0-9]?)(?:st|nd|rd|th)?", word or "")
    if m:
        return int(m.group(1))
    return _NUMS.get(word) or _ORDS.get(word)


def _ordinal(word: str) -> Optional[int]:
    if re.fullmatch(_ORD_RX, word or ""):
        return _count(word)
    return None


def choice_of(text) -> Optional[dict]:
    """What an answer says, the phone's order (mobile.js ``voiceParseAnswer``):
    the whole-answer always forms, then a NO anywhere, then a yes, then a
    picked number → ``{"choice": "always"|"no"|"yes"}`` or ``{"choice":
    "number", "number": N}``; None when ``text`` is not made only of answer
    words (``_BODY_WORDS``, "option two", "the second one")."""
    words = norm(text).split()
    if not words:
        return None
    number = None
    i = 0
    while i < len(words):
        w = words[i]
        if w in ("option", "choice"):
            j = i + 1
            if j < len(words) and words[j] == "number":
                j += 1
            n = _count(words[j]) if j < len(words) and not _ordinal(words[j]) else None
            if n is None:
                return None
            number = number or n
            i = j + 1
            continue
        k = i + 1 if w == "the" else i
        if k + 1 < len(words) and _ordinal(words[k]) and words[k + 1] in _PICK_NOUNS:
            number = number or _ordinal(words[k])
            i = k + 2
            continue
        if w not in _BODY_WORDS:
            return None
        i += 1
    t = " ".join(words)
    if _ALWAYS_RE.match(t):
        return {"choice": "always"}
    if _NO_RE.search(t):
        return {"choice": "no"}
    if _YES_RE.search(t):
        return {"choice": "yes"}
    if number is not None:
        return {"choice": "number", "number": number}
    return None


def _target(words: List[str], loose: bool) -> Optional[dict]:
    """Who an answer or a question is for: ``{"self": True}`` ("it"),
    ``{"index": N}`` ("session three", "number 2", "the third session") or
    ``{"name": words, "strict": loose}``; None for no target at all (a bare
    "session", or "it" where only a name or number may stand)."""
    t = " ".join(words)
    if not t or t in _NOUNS:
        return None
    if t in _SELF:
        return None if loose else {"self": True}
    m = re.fullmatch(r"(?:session|window|number) (%s)" % _NUM_RX, t) or re.fullmatch(
        r"the (%s) (?:session|window)" % _ORD_RX, t
    )
    if m:
        return {"index": _count(m.group(1))}
    if choice_of(t) is not None or t in ("all", "everything", "that", "this"):
        return None
    return {"name": t, "strict": loose}


def _answer_command(words: List[str]) -> Optional[dict]:
    """An answer phrase → ``{"kind": "answer", "choice", "number"?, "words",
    "target"}``, or None. The forms, first match wins:

    * bare — ``yes``, ``no, don't``, ``always allow``, ``option two``, and
      ``answer <answer>``: target None (the session being talked to);
    * ``(answer|say) <answer> (to|on|for) <target>``, ``tell <target> (to)?
      <answer>`` — explicit: an unknown name is ``no-session``;
    * ``<answer> to <target>``, ``<target>, <answer>`` — loose: the target
      must be a number or a session's whole name (or the start of it, whole
      words); anything else leaves the phrase a prompt."""

    def hit(body, tgt):
        c = choice_of(" ".join(body))
        if c is None or tgt is None:
            return None
        return dict(c, kind="answer", words=" ".join(body), target=tgt)

    if not words:
        return None
    c = choice_of(" ".join(words))
    if c is not None:
        return dict(c, kind="answer", words=" ".join(words), target=None)
    n = len(words)
    if words[0] == "answer":
        c = choice_of(" ".join(words[1:]))
        if c is not None:
            return dict(c, kind="answer", words=" ".join(words[1:]), target=None)
    if words[0] in ("answer", "say"):
        for p in range(2, n - 1):
            if words[p] in ("to", "on", "for"):
                got = hit(words[1:p], _target(words[p + 1 :], False))
                if got:
                    return got
    if words[0] == "tell":
        for k in range(2, n):
            body = words[k + 1 :] if words[k] == "to" else words[k:]
            got = hit(body, _target(words[1:k], False))
            if got:
                return got
    for p in range(1, n - 1):
        if words[p] == "to":
            got = hit(words[:p], _target(words[p + 1 :], True))
            if got:
                return got
    for k in range(1, n):
        got = hit(words[k:], _target(words[:k], True))
        if got:
            return got
    return None


def _question_command(text: str) -> Optional[dict]:
    m = _QUESTION_RE.match(text)
    if m is None:
        return None
    said = m.group("a") or m.group("b")
    if not said:
        return {"kind": "question", "target": None}
    tgt = _target(said.split(), False)
    if tgt is None:
        return None
    if tgt.get("self"):
        tgt = None
    elif "name" in tgt:
        tgt["strict"] = True  # "what does the user want" is words for the agent
    return {"kind": "question", "target": tgt}


def command(text, words: Optional[dict] = None) -> Optional[dict]:
    """The command ``text`` is, or None (a prompt): ``{"kind", "arg"}``, and
    for ``new`` ``{"kind": "new", "name", "repo", "task"}`` as said (the repo
    words unresolved — :func:`plan_new` resolves them); ``{"kind":
    "switch_index", "index"}``, ``{"kind": "switch_rel", "delta"}``, ``{"kind":
    "pick", "index"}`` ("the second one": :func:`answer` decides), ``{"kind":
    "answer", "choice", "number"?, "words", "target"}`` and ``{"kind":
    "question", "target"}`` (targets unresolved — :func:`answer` resolves
    them against the rows). ``words``: the person's effective voice words
    (``core.voice_words.effective``) — ``hush`` and ``stop`` are theirs;
    None = the defaults."""
    w = _Words(text)
    if not w.text:
        return None
    for kind, rx in _CMDS[:1]:
        if rx.match(w.text):
            return {"kind": kind, "arg": ""}
    p = _parse_new(w)
    if p is not None:
        return {
            "kind": "new",
            "name": " ".join(w.words[p["name"][0] : p["name"][1]]) if p["name"] else "",
            "repo": " ".join(w.words[p["repo"][0] : p["repo"][1]]) if p["repo"] else "",
            "task": w.original(p["task"]) if p["task"] is not None else "",
        }
    m = _REL_RE.match(w.text)
    if m:
        return {"kind": "switch_rel", "delta": 1 if m.group(1) == "next" else -1}
    for rx in _INDEX_RES:
        m = rx.match(w.text)
        if m:
            return {"kind": "switch_index", "index": _count(m.group(1))}
    for kind, rx in _CMDS[1:]:
        if kind in _WORD_KINDS and words is not None:
            rx = _kind_rx(kind, words)
        m = rx.match(w.text)
        if m:
            return {
                "kind": kind,
                "arg": (m.group(1) or "").strip() if m.groups() else "",
            }
    q = _question_command(w.text)
    if q is not None:
        return q
    m = _PICK_RE.match(w.text)
    if m:
        return {"kind": "pick", "index": _count(m.group(1)), "words": w.text}
    return _answer_command(w.words)


# --- sessions ----------------------------------------------------------------
def _name(row: dict) -> str:
    """What a session is called (mobile.js ``nameOf``): its ``name``, else
    its title."""
    return str(row.get("name") or row.get("title") or "")


def _listed(rows: Iterable) -> List[dict]:
    """The rows a person can talk about: titled, and not hidden off the rail
    by the feature that owns them (a Verify checklist's session)."""
    return [
        r
        for r in rows or ()
        if isinstance(r, dict) and r.get("title") and not r.get("hidden_by")
    ]


def match_session(query, rows) -> Tuple[Optional[dict], List[dict]]:
    """mobile.js ``voiceMatchSession`` → ``(hit, candidates)``.

    Names and titles, normalised: a unique prefix match, else the unique best
    word overlap, else a unique loose containment; a tie lists the tied
    rows, no match an empty list."""
    q = norm(query)
    cands = [
        {"row": r, "n": norm(_name(r)), "t": norm(r.get("title"))}
        for r in _listed(rows)
    ]
    if not q:
        return None, [c["row"] for c in cands]
    pre = [c for c in cands if c["n"].startswith(q) or c["t"].startswith(q)]
    if len(pre) == 1:
        return pre[0]["row"], []
    if pre:
        return None, [c["row"] for c in pre]
    qw = q.split(" ")
    scored = []
    for c in cands:
        words = (c["n"] + " " + c["t"]).split(" ")
        n = sum(1 for x in qw if x in words)
        if n > 0:
            scored.append((n, c))
    scored.sort(key=lambda x: -x[0])  # stable, like Array.prototype.sort
    if len(scored) == 1 or (len(scored) > 1 and scored[0][0] > scored[1][0]):
        return scored[0][1]["row"], []
    if scored:
        return None, [c["row"] for _, c in scored]
    loose = [c for c in cands if q in c["n"] or q in c["t"] or (c["n"] and c["n"] in q)]
    if len(loose) == 1:
        return loose[0]["row"], []
    return None, [c["row"] for c in loose]


def _row(rows, title) -> Optional[dict]:
    if not title:
        return None
    return next((r for r in _listed(rows) if r.get("title") == title), None)


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def session_state(row: dict, now: Optional[float] = None) -> dict:
    """mobile.js ``sessionState`` → ``{"state", "label", "reason", "in_bell"}``.

    ``in_bell`` is the rail bell's attention set — a question, a failed
    setup or pre-commit, failing checks, a gone terminal, or idle with
    unfinished work for :data:`STUCK_IDLE_S` — the one rule the phone's "what
    needs me" reads too."""
    now = time.time() if now is None else now

    def st(state, label, reason="", in_bell=False):
        return {"state": state, "label": label, "reason": reason, "in_bell": in_bell}

    status = str(row.get("status") or "")
    setup = row.get("setup") if isinstance(row.get("setup"), dict) else {}
    if row.get("workspace_missing"):
        return st("offline", "Missing")
    if status == "paused":
        return st("idle", "Paused")
    if row.get("pending") or status == "loading":
        return st("starting", "Starting")
    if row.get("sleeping"):
        return st("idle", "Sleeping")
    if setup.get("state") == "running":
        return st("starting", "Starting")
    if setup.get("state") == "failed":
        return st("needs_you", "Setup failed", "setup_failed", True)
    act = row.get("activity") or "idle"
    if act == "clarify" or (act == "idle" and _prose_question(row)):
        return st("needs_you", "Question", "question", True)
    if act == "working":
        return st("working", "Working")
    if act == "limit":
        q = row.get("queue") if isinstance(row.get("queue"), dict) else {}
        until = _num(q.get("limited_until"))
        label = "Waiting on limit"
        if until > now:
            label += ", resumes " + time.strftime("%H:%M", time.localtime(until))
        return st("limit", label)
    if act == "offline":
        return st("offline", "Offline", "", True)
    if row.get("stage") == "interrupt":
        return st("needs_you", "Pre-commit failed", "precommit_failed", True)
    check = row.get("check") if isinstance(row.get("check"), dict) else {}
    if check.get("state") == "failed" and not check.get("stale"):
        return st("needs_you", "Checks failing", "checks_failing", True)
    since = _num(row.get("activity_since"))
    idle_for = now - since if since > 0 else 0
    ds = row.get("diff_stat") if isinstance(row.get("diff_stat"), dict) else {}
    un = ds.get("uncommitted") if isinstance(ds.get("uncommitted"), dict) else {}
    dirty = _num(un.get("additions")) + _num(un.get("deletions")) > 0
    if idle_for > STUCK_IDLE_S and dirty:
        return st("idle", "Idle", "stuck", True)
    return st("idle", "Idle")


def _prose_question(row: dict) -> Optional[str]:
    """The question ``row``'s session asked in prose (its ``asking`` of kind
    ``question``, core.asking), or None."""
    asking = row.get("asking") if isinstance(row.get("asking"), dict) else {}
    if asking.get("kind") != "question":
        return None
    return str(asking.get("text") or "") or None


def asking_text(row: dict) -> Optional[str]:
    """What ``row``'s session is asking, as a row says it: its ``asking``
    text (a dialog's question line or a prose question), else None."""
    asking = row.get("asking") if isinstance(row.get("asking"), dict) else {}
    return str(asking.get("text") or "") or None


def _attn_rank(row: dict, now: float) -> int:
    """mobile.js ``attnRank`` for a bell row: a question first, then whatever
    is broken, then failing checks."""
    reason = session_state(row, now)["reason"]
    return 0 if reason == "question" else 2 if reason == "checks_failing" else 1


def _state_word(ss: dict) -> str:
    """mobile.js ``voiceStateWord``: the state as a voice says it."""
    return {
        "question": "waiting on a question",
        "precommit_failed": "stuck on a failed pre-commit",
        "checks_failing": "waiting on failing checks",
        "setup_failed": "stuck on a failed setup",
    }.get(ss["reason"], ss["label"].lower())


def _names(rows: List[dict], n: int = MAX_NAMES) -> str:
    return ", ".join(_name(r) for r in rows[:n])


def _unmatched(arg: str, rows, cands) -> dict:
    """The ``ambiguous`` / ``no-session`` answer for a name that picked no
    single session."""
    if cands:
        return {"kind": "ambiguous", "say": "Which one: %s?" % _names(cands)}
    everyone = _listed(rows)
    if not everyone:
        return {"kind": "no-session", "say": "There are no sessions."}
    return {
        "kind": "no-session",
        "say": "No session called %s. Sessions: %s." % (arg, _names(everyone)),
    }


def _switched(hit: dict) -> dict:
    name = _name(hit)
    say = "Now on %s." % name
    if hit.get("last_say"):
        say += " " + _end(hit["last_say"])
    return {"kind": "switch", "title": hit["title"], "name": name, "say": say}


def _switch(arg: str, rows) -> dict:
    hit, cands = match_session(arg, rows)
    if hit is None:
        return _unmatched(arg, rows, cands)
    return _switched(hit)


def _strict_match(query: str, rows) -> Tuple[Optional[dict], List[dict]]:
    """A name said without a verb ("docs, yes"): only a session's whole name
    or title, or its start in whole words ("invoice" for invoice-parser), a
    leading "the"/"my" ignored — never :func:`match_session`'s word overlap,
    so "update the docs, yes" is no answer to ``docs``."""
    q = norm(query)
    for lead in ("the ", "my "):
        if q.startswith(lead):
            q = q[len(lead) :]
    if not q:
        return None, []
    cands = []
    for r in _listed(rows):
        for said in (norm(_name(r)), norm(r.get("title"))):
            if said == q or said.startswith(q + " "):
                cands.append(r)
                break
    exact = [r for r in cands if q in (norm(_name(r)), norm(r.get("title")))]
    if len(exact) == 1:
        return exact[0], []
    if len(cands) == 1:
        return cands[0], []
    return None, cands


def _resolve(tgt, session: str, rows, title: str):
    """An answer's or a question's target → ``(row, None)``, ``(None,
    {"index": N})`` (the client maps it), or ``(None, final answer)``.
    ``tgt`` None is the session being talked to; ``title`` is the client's
    own mapping of an index (or of next/previous) to a title."""
    if tgt is None or tgt.get("self"):
        row = _row(rows, session)
        return (row, None) if row is not None else (None, {"kind": "prompt"})
    if "index" in tgt:
        if not title:
            return None, {"index": tgt["index"]}
        row = _row(rows, title)
        if row is None:
            return None, {"kind": "no-session", "say": "%s is not listed." % title}
        return row, None
    said = tgt["name"]
    if tgt.get("strict"):
        hit, cands = _strict_match(said, rows)
        if hit is None and not cands:
            return None, {"kind": "prompt"}
    else:
        hit, cands = match_session(said, rows)
    if hit is None:
        return None, _unmatched(said, rows, cands)
    return hit, None


def _list(rows, now: float) -> dict:
    listed = _listed(rows)
    if not listed:
        return {"kind": "list", "say": "There are no sessions."}
    parts = []
    for r in listed[:MAX_LIST]:
        ss = session_state(r, now)
        word = "waiting on you" if ss["reason"] == "question" else _state_word(ss)
        parts.append("%s (%s)" % (_name(r), word))
    more = len(listed) - MAX_LIST
    tail = ", and %d more" % more if more > 0 else ""
    return {"kind": "list", "say": "Sessions: %s%s." % (", ".join(parts), tail)}


def _needs(rows, now: float) -> dict:
    bell = [r for r in _listed(rows) if session_state(r, now)["in_bell"]]
    bell.sort(key=lambda r: _attn_rank(r, now))
    lines = []
    for r in bell[:MAX_NEEDS]:
        ss = session_state(r, now)
        if ss["reason"] == "question":
            said = asking_text(r) or r.get("last_say") or "a question"
            lines.append(_end("%s is asking: %s" % (_name(r), strip_markdown(said))))
        else:
            lines.append(_end("%s is %s" % (_name(r), _state_word(ss))))
    return {"kind": "needs", "say": " ".join(lines) if lines else "Nothing needs you."}


def _status(session: str, rows, now: float) -> dict:
    if not session:
        return {"kind": "status", "say": "No session is open."}
    row = _row(rows, session)
    if row is None:
        return {"kind": "status", "say": "%s is not listed." % session}
    ss = session_state(row, now)
    since = _num(row.get("activity_since"))
    mins = round((now - since) / 60) if since else 0
    dur = ""
    if row.get("activity") == "working" and mins >= 1:
        dur = " for %d minute%s" % (mins, "" if mins == 1 else "s")
    say = "%s is %s%s." % (_name(row), _state_word(ss), dur)
    if row.get("last_turn"):
        say += " " + _end(strip_markdown(row["last_turn"]))
    return {"kind": "status", "say": say}


def _answer_or_question(cmd: dict, session: str, rows, title: str) -> dict:
    row, out = _resolve(cmd.get("target"), session, rows, title)
    if row is None:
        if cmd["kind"] == "question" and cmd.get("target") is None:
            return {"kind": "question", "say": "No session is open."}
        if "kind" in out:
            return out
        if cmd["kind"] == "question":
            return {"kind": "question", "index": out["index"]}
        got = {"kind": "answer", "choice": cmd["choice"]}
        if cmd["choice"] == "number":
            got["number"] = cmd["number"]
        got.update(index=out["index"], words=cmd["words"])
        return got
    got = {"kind": cmd["kind"], "title": row["title"], "name": _name(row)}
    if cmd["kind"] == "answer":
        got["choice"] = cmd["choice"]
        if cmd["choice"] == "number":
            got["number"] = cmd["number"]
        got["words"] = cmd["words"]
        tgt = cmd.get("target")
        if tgt is None or tgt.get("self"):
            got["bare"] = True
    return got


def answer(
    text,
    session: str,
    rows,
    now: Optional[float] = None,
    title: str = "",
    words: Optional[dict] = None,
) -> dict:
    """The answer to ``text`` said to ``session`` (a title, ``""`` for none).

    ``title``: the client's mapping of the phrase's numbered target ("session
    three, yes") or next/previous to one of its titles, sent back — the
    numbering is the client's (its rail or list order), so without it those
    answers carry ``index`` / ``delta`` and no ``say``. ``words``: as
    :func:`command`'s.

    ``new`` comes back as :func:`command` parsed it — the route resolves it
    with :func:`plan_new` and creates the session; ``answer`` and
    ``question`` that name a session come back for :func:`finish` (the route
    reads that session's dialog). Every other kind is final."""
    cmd = command(text, words)
    if cmd is None:
        return {"kind": "prompt"}
    now = time.time() if now is None else now
    kind = cmd["kind"]
    if kind in LOCAL_KINDS:
        return {"kind": kind}
    if kind == "new":
        return cmd
    if kind == "switch":
        return _switch(cmd["arg"], rows)
    if kind == "pick":
        cur = _row(rows, session)
        if cur is not None and cur.get("activity") == "clarify":
            cmd = {
                "kind": "answer",
                "choice": "number",
                "number": cmd["index"],
                "words": cmd["words"],
                "target": None,
            }
        else:
            cmd = {"kind": "switch_index", "index": cmd["index"]}
        kind = cmd["kind"]
    if kind in ("switch_index", "switch_rel"):
        if not title:
            return dict(cmd)
        row = _row(rows, title)
        if row is None:
            return {"kind": "no-session", "say": "%s is not listed." % title}
        return _switched(row)
    if kind in ("answer", "question"):
        return _answer_or_question(cmd, session, rows, title)
    if kind == "list":
        return _list(rows, now)
    if kind == "needs":
        return _needs(rows, now)
    return _status(session, rows, now)


# --- answering dialogs -----------------------------------------------------------
def parse_answer(text, options) -> Optional[dict]:
    """mobile.js ``voiceParseAnswer``: the option ``text`` picks, or None.

    Permission dialogs (any yes/no/always option) take WORDS only: the
    whole-answer always forms first (an always without an always option is
    a yes), then a no anywhere — whatever else was said — then a yes.
    Numbers never pick a permission (recognisers turn "too"/"to" into "2",
    and "2" is the standing-permission option of Claude's dialog). Dialogs
    made only of ``other`` options take a number, matched to the option's
    own key."""
    t = norm(text)
    opts = [o for o in options or () if isinstance(o, dict)]

    def by_kind(k):
        return next((o for o in opts if o.get("kind") == k), None)

    if any(o.get("kind") in ("yes", "no", "always") for o in opts):
        if _ALWAYS_RE.match(t):
            return by_kind("always") or by_kind("yes")
        if _NO_RE.search(t):
            return by_kind("no")
        if _YES_RE.search(t):
            return by_kind("yes")
        return None
    m = _PNUM_RE.search(t)
    if m:
        key = str(_PNUM[m.group(1)])
        return next((o for o in opts if str(o.get("key")) == key), None)
    return None


# --- Claude's question tool (``/dialog``'s ``kind: "select"``) -------------------
#: The keys that switch tabs / press the cursor's row, when a body has none.
_SELECT_NAV = {"next": ["Right"], "prev": ["Left"], "submit": ["Enter"]}
_SEL_NEXT_RE = re.compile(r"^(?:next|next question|next one|go on|skip|skip it)$")
_SEL_BACK_RE = re.compile(
    r"^(?:back|go back|previous|previous question|prev|last question)$"
)
_SEL_SUBMIT_RE = re.compile(
    r"^(?:submit|submit (?:it|them|that|answers|my answers|the answers)"
    r"|send (?:it|them)|done|finish|finished)$"
)
_SEL_TEXT_RE = re.compile(
    r"^(?:type something|other|something else|write in|write my own|my own answer"
    r"|type my own|type an answer)$"
)
#: What leads a pick ("pick two", "choose red and blue") — dropped.
_SEL_LEAD = frozenset(
    "pick choose select take tick check option options number numbers the".split()
)
#: What leads an UN-pick in a multi-select ("untick cheese").
_SEL_UNDO = frozenset("untick uncheck unselect remove drop deselect".split())
#: Words that only join picks ("one and three", "red then blue").
_SEL_JOIN = frozenset("and plus also then".split())


def is_select(dialog) -> bool:
    """Whether ``dialog`` (a ``/dialog`` body) is Claude's question tool."""
    return _parsed(dialog) and dialog.get("kind") == "select"


def _select_opts(dialog: dict) -> List[dict]:
    return [o for o in dialog.get("options") or () if isinstance(o, dict)]


def _opt_keys(opt: dict) -> List[str]:
    return [str(k) for k in (opt.get("keys") or [opt.get("key")])]


def _said_label(opt: dict) -> str:
    return strip_markdown(opt.get("label") or "").rstrip(".") or "that"


def _select_picks(words: List[str], opts: List[dict]) -> Optional[List[dict]]:
    """The choices ``words`` pick, in order, or None when any word is
    neither a pick nor a joiner: a label (the longest run of words that is
    one — "salt and pepper" stays whole — else the unique label those words
    begin, "plain" for "Plain HTML"), a number ("two", "3"), an ordinal
    ("the second one"); "pick", "option", "and", "the"… only join them."""
    labels = [(norm(o.get("label")), o) for o in opts]

    def by_key(n) -> Optional[dict]:
        return next((o for o in opts if str(o.get("key")) == str(n)), None)

    picks: List[dict] = []
    i, n = 0, len(words)
    while i < n:
        w = words[i]
        hit = None
        for j in range(n, i, -1):
            said = " ".join(words[i:j])
            exact = [o for lab, o in labels if lab == said]
            if exact:
                hit = (exact[0], j)
                break
        if hit is None:
            for j in range(n, i, -1):
                said = " ".join(words[i:j])
                starts = [o for lab, o in labels if lab.startswith(said + " ")]
                if len(said) >= 3 and len(starts) == 1:
                    hit = (starts[0], j)
                    break
        if hit is None and _ordinal(w) and i + 1 < n and words[i + 1] in _PICK_NOUNS:
            hit = (by_key(_ordinal(w)), i + 2)
        if hit is None and _count(w) is not None:
            hit = (by_key(_count(w)), i + 1)
        if hit is None:
            if w in _SEL_LEAD or w in _SEL_JOIN:
                i += 1
                continue
            return None
        if hit[0] is None:
            return None  # a number past the last choice
        picks.append(hit[0])
        i = hit[1]
    return picks


def select_answer(text, dialog) -> Optional[dict]:
    """A phrase said to Claude's question tool → ``{"keys", "said"}`` (and
    ``"text"`` — typed before the keys — for a free-text answer), or None.

    * The review screen (``review`` in the body): "submit" / a yes → Submit
      answers, "cancel" / a no → Cancel, "back" → the previous tab.
    * A question: "next" / "back" switch tabs (``nav``); "submit" / "done"
      goes on to the review tab (Right up to it) — never Enter, which would
      pick whatever row the cursor is on. "type something" / "other" →
      the free-text row. Numbers and labels pick: one for a single-select
      ("two", "red", "option two"); any number for a multi-select ("one and
      three", "cheese and mushrooms") — boxes already ticked are left alone,
      "untick cheese" clears one. A bare "yes" is no pick. With the cursor
      on the free-text row, any other phrase is the answer typed into it,
      then Enter."""
    if not isinstance(dialog, dict):
        return None
    t = norm(text)
    if len(t.split()) > 1 and t.split()[-1] == "please":
        t = t.rsplit(" ", 1)[0]
    if not t:
        return None
    opts = _select_opts(dialog)
    nav = dialog.get("nav") or _SELECT_NAV
    steps = [s for s in dialog.get("steps") or () if isinstance(s, dict)]
    step = dialog.get("step") if isinstance(dialog.get("step"), int) else 0
    if "review" in dialog:
        if _SEL_BACK_RE.match(t) and step > 0:
            return {"keys": list(nav["prev"]), "said": "back"}
        if _NO_RE.search(t):
            opt = next((o for o in opts if o.get("kind") == "no"), None)
        elif _SEL_SUBMIT_RE.match(t) or _YES_RE.search(t):
            opt = next((o for o in opts if o.get("kind") == "yes"), None)
        else:
            picks = _select_picks(t.split(), opts) or []
            opt = picks[0] if len(picks) == 1 else None
        if opt is None:
            return None
        return {"keys": _opt_keys(opt), "said": _said_label(opt)}
    if _SEL_NEXT_RE.match(t):
        if step + 1 < len(steps):
            return {"keys": list(nav["next"]), "said": "next"}
        return None
    if _SEL_BACK_RE.match(t):
        return {"keys": list(nav["prev"]), "said": "back"} if step > 0 else None
    if _SEL_SUBMIT_RE.match(t):
        review = next((k for k, s in enumerate(steps) if s.get("submit")), None)
        if review is None or review <= step:
            return None
        return {"keys": list(nav["next"]) * (review - step), "said": "submit"}
    text_opt = next((o for o in opts if o.get("kind") == "text"), None)
    if text_opt is not None and _SEL_TEXT_RE.match(t):
        return {"keys": _opt_keys(text_opt), "said": "type something"}
    words = t.split()
    undo = bool(dialog.get("multi")) and bool(words) and words[0] in _SEL_UNDO
    picks = _select_picks(words[1:] if undo else words, opts)
    if picks:
        if not dialog.get("multi"):
            if len(picks) != 1:
                return None
            return {"keys": _opt_keys(picks[0]), "said": _said_label(picks[0])}
        keys: List[str] = []
        said: List[str] = []
        seen = set()
        for opt in picks:
            if str(opt.get("key")) in seen:
                continue
            seen.add(str(opt.get("key")))
            if opt.get("checked") is (not undo) and opt.get("kind") == "other":
                continue  # already as asked
            for k in _opt_keys(opt):
                if k != "Up" or not keys:
                    keys.append(k)
            said.append(_said_label(opt))
        if not keys:
            return {"keys": [], "said": "already " + ("cleared" if undo else "ticked")}
        return {"keys": keys, "said": " and ".join(said)}
    if (
        text_opt is not None
        and str(dialog.get("cursor")) == str(text_opt.get("key"))
        and not _ANSWERED_WORDS_RE.match(t)
    ):
        return {
            "keys": ["Enter"],
            "text": str(text or "").strip(),
            "said": str(text or "").strip(),
        }
    return None


#: Phrases that are never a typed free-text answer.
_ANSWERED_WORDS_RE = re.compile(
    r"^(?:yes|yeah|yep|no|nope|cancel|stop|never ?mind|ok|okay)$"
)


def select_choices(dialog: dict) -> str:
    """How to answer Claude's question tool, as said."""
    opts = _select_opts(dialog)
    if "review" in dialog:
        answered = [
            "%s %s"
            % (
                _end(strip_markdown(r.get("question") or "")),
                _end(strip_markdown(r.get("answer") or "")),
            )
            for r in dialog.get("review") or ()
            if isinstance(r, dict)
        ]
        return " ".join(answered + ["Say submit or cancel."])
    names = ", ".join(_said_label(o) for o in opts if o.get("kind") == "other")
    if dialog.get("multi"):
        ticked = [_said_label(o) for o in opts if o.get("checked") is True]
        out = "Options: %s — pick as many as you like, then say next." % names
        if ticked:
            out += " Ticked: %s." % ", ".join(ticked)
        return out
    return "Options: %s — say a number or the option." % names


def select_speech(name: str, dialog: dict) -> str:
    """``<name> asks: <question> <choices>`` for Claude's question tool."""
    question = strip_markdown(dialog.get("question") or "") or "a question"
    return "%s asks: %s %s" % (name, _end(question), select_choices(dialog))


def _kind_word(opt: dict) -> str:
    """mobile.js ``voiceKindWord``."""
    k = opt.get("kind")
    if k in ("yes", "no", "always"):
        return k
    return strip_markdown(opt.get("label") or "") or "that"


def dialog_choices(dialog: dict) -> str:
    """mobile.js ``voiceDialogChoices``: how to answer, as said."""
    opts = [o for o in dialog.get("options") or () if isinstance(o, dict)]
    kinds = {o.get("kind") for o in opts}
    if "yes" in kinds or "no" in kinds:
        return "Say yes, no, or always." if "always" in kinds else "Say yes or no."
    n = min(len(opts), len(_ORD_WORDS))
    names = [
        "%s: %s" % (_ORD_WORDS[i], strip_markdown(opts[i].get("label") or ""))
        for i in range(n)
    ]
    nums = list(_ORD_WORDS[:n])
    if len(nums) == 1:
        said = nums[0]
    elif len(nums) == 2:
        said = "%s or %s" % (nums[0], nums[1])
    else:
        said = ", ".join(nums[:-1]) + ", or " + nums[-1]
    return ". ".join(names) + ". Say " + said + "."


def dialog_speech(name: str, dialog: dict) -> str:
    """mobile.js ``voiceDialogSpeech``: ``<name>: <question> [It wants to
    run: <command>.] <choices>`` — Claude's question tool:
    :func:`select_speech`."""
    if dialog.get("kind") == "select":
        return select_speech(name, dialog)
    parts = [
        "%s: %s"
        % (name, strip_markdown(dialog.get("question") or "") or "a permission prompt")
    ]
    if dialog.get("command"):
        parts.append("It wants to run: %s." % str(dialog["command"])[:200])
    parts.append(dialog_choices(dialog))
    return " ".join(parts)


def _parsed(dialog) -> bool:
    return (
        isinstance(dialog, dict)
        and bool(dialog.get("parsed"))
        and bool(dialog.get("options"))
    )


def finish(ans: dict, dialog, spoken=None, remote: bool = False) -> dict:
    """An ``answer`` / ``question`` from :func:`answer` that names a session,
    finished with that session's live dialog (the ``/dialog`` body, None when
    it is not asking) and, for a question, its spoken record (``{"say",
    "has_question"}``). ``remote``: another device's session, whose dialog
    this server cannot read.

    An answer to a parsed dialog → ``dialog_id`` + ``keys`` (the option
    :func:`parse_answer` picks; ``[]`` and a "didn't catch that" ``say`` when
    none) — never pressed here, the client POSTs ``/answer``. No parsed
    dialog → ``no_dialog`` and why; but a BARE answer ("yes" said to the
    session being talked to) with no parsed dialog is ``{"kind": "prompt"}``:
    the agent may have asked in words, and the words reach it as typed. A
    question asked in prose (``/dialog``'s ``kind: "question"``) is read out
    like a dialog's; an answer to it that names the session is
    ``no_dialog`` with a ``say`` that it was asked in words — switch to it
    and say the answer, which goes in as a prompt."""
    out = dict(ans)
    bare = out.pop("bare", False)
    name = out.get("name") or out.get("title") or ""
    if out.get("kind") == "question":
        if _parsed(dialog):
            say = dialog_speech(name, dialog)
        elif isinstance(dialog, dict) and dialog.get("question"):
            say = _end("%s: %s" % (name, strip_markdown(dialog["question"])))
        elif (
            isinstance(spoken, dict)
            and spoken.get("has_question")
            and spoken.get("say")
        ):
            say = _end("%s: %s" % (name, strip_markdown(spoken["say"])))
        else:
            say = "%s: Nothing is being asked." % name
        out["say"] = say
        return out
    if not _parsed(dialog):
        if bare:
            return {"kind": "prompt"}
        out["no_dialog"] = True
        if remote:
            out["say"] = "%s is on another device: answer it there." % name
        elif isinstance(dialog, dict) and dialog.get("kind") == "question":
            out["say"] = (
                "%s asked in words, not a dialog: switch to it and say your answer."
                % name
            )
        elif isinstance(dialog, dict):
            out["say"] = "%s is waiting on you: open the screen." % name
        else:
            out["say"] = "%s is not asking anything." % name
        return out
    out["dialog_id"] = dialog.get("id")
    if is_select(dialog):
        return _finish_select(out, name, dialog, out.get("words") or "")
    opt = parse_answer(out.get("words") or "", dialog.get("options"))
    if opt is None:
        out["keys"] = []
        choices = dialog_choices(dialog)
        out["say"] = "I didn't catch that for %s: %s" % (
            name,
            "say " + choices.split("Say ", 1)[-1] if "Say " in choices else choices,
        )
        return out
    out["keys"] = [str(opt.get("key"))]
    out["say"] = "Answered %s on %s." % (_kind_word(opt), name)
    return out


def _finish_select(out: dict, name: str, dialog: dict, words: str) -> dict:
    """``out`` (an ``answer``) finished against Claude's question tool:
    :func:`select_answer`'s ``keys`` (and ``text``), or ``[]`` and how to
    answer it."""
    sel = select_answer(words, dialog)
    if sel is None or not sel["keys"]:
        out["keys"] = []
        out["say"] = (
            "%s: %s." % (name, _end(sel["said"]).rstrip("."))
            if sel is not None
            else "I didn't catch that for %s. %s" % (name, select_choices(dialog))
        )
        return out
    out["keys"] = sel["keys"]
    if sel.get("text"):
        out["text"] = sel["text"]
    out["say"] = "Answered %s on %s." % (sel["said"], name)
    return out


def select_route(text, session: str, dialog, rows=()) -> Optional[dict]:
    """A phrase said to ``session`` while its agent shows Claude's question
    tool → the ``answer`` (``dialog_id``, ``keys``, maybe ``text``), or None
    to route it as usual. :func:`command` reads "red", "one and three",
    "next" or "submit" as a prompt (or "go back" as a session switch);
    against a question they are its answer."""
    if not session or not is_select(dialog):
        return None
    if select_answer(text, dialog) is None:
        return None
    row = _row(rows, session) or {"title": session}
    out = {"kind": "answer", "title": session, "name": _name(row), "words": text}
    out["dialog_id"] = dialog.get("id")
    return _finish_select(out, out["name"], dialog, text)


# --- new sessions --------------------------------------------------------------
def _compact(text) -> str:
    return re.sub(r"[^a-z0-9]", "", norm(text))


def match_repo(words: List[str], repos) -> Optional[dict]:
    """The repository the spoken ``words`` name, or None.

    ``repos``: ``[{"name", "path", …}]``, most recent first. Spacing, case
    and punctuation don't count ("site check bot 5" is ``sitecheck-bot5``)
    and "the … repo" is not part of a name. An exact name wins, else a name
    the words begin (three letters at least) — the most recent on a tie."""
    ws = list(words)
    while ws and ws[0] in _REPO_LEAD:
        ws = ws[1:]
    while ws and ws[-1] in _REPO_TAIL:
        ws = ws[:-1]
    q = "".join(ws)
    if not q:
        return None
    known = [r for r in repos or () if isinstance(r, dict) and r.get("path")]
    for r in known:
        if _compact(r.get("name")) == q:
            return r
    if len(q) >= 3:
        for r in known:
            if _compact(r.get("name")).startswith(q):
                return r
    return None


def title_base(name: str, task: str) -> str:
    """The title before it is made unique: the ``called`` name hyphenated,
    else the task's first :data:`TITLE_WORDS` words the way the New dialog
    names a session from its task (``title_from_prompt``), else
    :data:`DEFAULT_TITLE`."""
    if name:
        out = "-".join(re.findall(r"[a-z0-9]+", name.lower()))[:TITLE_MAX].strip("-")
        if out:
            return out
    if task:
        out = "-".join(title_from_prompt(task).split("-")[:TITLE_WORDS])
        if out:
            return out
    return DEFAULT_TITLE


def _repo_names(repos, n: int = MAX_NAMES) -> str:
    seen: List[str] = []
    for r in repos or ():
        nm = str(r.get("name") or "")
        if nm and nm not in seen:
            seen.append(nm)
    return ", ".join(seen[:n])


def _which_repo(repos) -> dict:
    say = "I don't know which repository: say new session in <repo>."
    names = _repo_names(repos)
    if names:
        say = "I don't know which repository: say new session in %s." % names
    return {"kind": "new-failed", "say": say}


def plan_new(text, session: str, rows, repos) -> dict:
    """``text`` (a ``new`` phrase) → what to create: ``{"kind": "new",
    "title_base", "name", "repo_name", "repo_path", "task"}``, or
    ``{"kind": "new-failed", "say"}``; ``{"kind": "prompt"}`` when ``text`` is
    no ``new`` phrase after all.

    The repository: the one named — the longest run of the words after
    "in/for/on" that names a known repository, the rest of them starting the
    task ("in sitecheck add tests"); else the current session's (a local
    row); else the only one known, else the most recent. Words after "for"
    or "on" that name no repository were the task ("new session for the
    login bug"); after "in" they were meant as one, and nothing is guessed.
    """
    w = _Words(text)
    p = _parse_new(w)
    if p is None:
        return {"kind": "prompt"}
    name = " ".join(w.words[p["name"][0] : p["name"][1]]) if p["name"] else ""
    task_at = p["task"]
    repo = None
    if p["repo"] is not None:
        a, b = p["repo"]
        for k in range(b - a, 0, -1):
            repo = match_repo(w.words[a : a + k], repos)
            if repo is not None:
                if a + k < b:
                    task_at = a + k  # "in sitecheck add tests …": the rest is the task
                break
        if repo is None:
            if p["prep"] == "in":
                said = w.original(a, b)
                names = _repo_names(repos)
                return {
                    "kind": "new-failed",
                    "say": "I don't know a repository called %s.%s"
                    % (said, " Repositories: %s." % names if names else ""),
                }
            task_at = a - 1  # "for the login bug" / "on the parser" is the task
    if repo is None:
        cur = _row(rows, session)
        if cur is not None and not cur.get("device") and cur.get("path"):
            repo = {"name": cur.get("repo") or "", "path": cur["path"]}
    if repo is None:
        known = [r for r in repos or () if isinstance(r, dict) and r.get("path")]
        recent = [r for r in known if r.get("source") in ("recent", "session")]
        if len(known) == 1:
            repo = known[0]
        elif recent:
            repo = recent[0]
    if repo is None:
        return _which_repo(repos)
    task = w.original(task_at) if task_at is not None else ""
    path = str(repo["path"])
    repo_name = str(repo.get("name") or "") or path.rstrip("/").rsplit("/", 1)[-1]
    return {
        "kind": "new",
        "title_base": title_base(name, task),
        "name": name,
        "repo_name": repo_name,
        "repo_path": path,
        "task": task,
    }


def started(title: str, repo_name: str) -> dict:
    """The answer once ``title`` was created in ``repo_name``."""
    return {
        "kind": "new",
        "title": title,
        "name": title,
        "say": "Started %s in %s." % (title, repo_name),
    }
