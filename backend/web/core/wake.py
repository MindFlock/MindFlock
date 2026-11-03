"""The wake word: is a short utterance the word ("MindFlock") on its own?

The desktop app and the phone page can keep the microphone in a standby
that watches the room's sound level and, when a short lone utterance
bounded by silence comes in, send just that clip to ``POST /api/voice/wake``
(routes/speech.py). The server recognises it with the word as the decoder's
hint and asks :func:`judge` (:func:`is_wake` is its yes/no): the utterance
must be the word alone — not a sentence that happens to contain it
("mindflock is great" is not a wake) — after a greeting in front ("Hey,
MindFlock", :data:`LEADS`) and an exact repeat ("MindFlock MindFlock", how
the prompted decoder loops) are dropped.

How close counts depends on the word (docs/voice-wake.md has the bench):

* OURS ("mindflock", the default) is matched STRICTLY: one of the forms the
  recogniser is known to write for it (``core.voice_words.HEARD``: "mind
  flock", "my flock", "mind flunk") or its sound shape (:data:`_SOUND_ONE`: a
  "mind"-like first part, then an fl/cl/bl/pl/fr onset and a short rest —
  "Mind Flap", "Minh Flock"). No generic letter slack: two edits let
  "Windlock" and "Mild block" in.
* A word of the person's own (``voice.words`` ``wake``) keeps the letter
  slack, shrinking with its length so a short word does not wake on
  everything.

The hint makes whisper write "MindFlock" for things that are not ("Mind.",
"Mine.", "Mindful."), so the route checks a yes for ours with a second,
un-prompted decode of the same clip and :func:`veto` turns it down when
that one plainly heard another word. Pure; no model here.
"""

from __future__ import annotations

import re
from typing import List, Tuple

from backend.web.core import voice_words as _voice_words

__all__ = ["LEADS", "WAKE_WORD", "hint", "is_wake", "judge", "normalize", "veto"]

#: The word, as the hint and the target. Lower case, no spaces.
WAKE_WORD = "mindflock"
#: How many letters may differ (edits) and still count — for a word of at
#: least 8 letters; 5–7 letters allow one, a shorter word none. A custom
#: word's slack only: ours is matched strictly (:func:`_strict`).
_MAX_EDITS = 2
#: A wake is the word ALONE: at most this many more words in the clip than
#: the word has ("mind flock" for "mindflock").
_EXTRA_WORDS = 1
#: A greeting in front of the word, dropped before it is judged ("Hey,
#: MindFlock", "OK MindFlock") — unless the word itself starts with it
#: ("hey jarvis").
LEADS = frozenset({"hey", "hi", "ok", "okay", "yo", "hello"})

_NON_LETTERS = re.compile(r"[^a-z]+")
_TOKEN_SPLIT = re.compile(r"[^A-Za-z]+")

#: Ours as it sounds: a "mind"-like first part, an fl/cl/bl/pl/fr onset, a
#: vowel and at most five letters more ("mindflap", "minhflock",
#: "mendclock"). Matched against one token whole, or two tokens with the
#: first the "mind" part ("mind" + "flunk"), so "mindflock now" never fits.
_FIRST = r"(?:mind|mined|mine|mynd|min|minh|mint|mend|men|myn)"
_SECOND = r"(?:fl|cl|bl|pl|fr)[aeiou][a-z]{0,5}"
_SOUND_ONE = re.compile(r"^%s%s$" % (_FIRST, _SECOND))
_SOUND_FIRST = re.compile(r"^%s$" % _FIRST)
_SOUND_SECOND = re.compile(r"^%s$" % _SECOND)
#: Real words the sound shape would take for ours.
_NOT_OURS = frozenset({"mindful", "mindfully"})

#: What the un-prompted decode writes when the clip was another word that
#: the hint turned into "MindFlock" (:func:`veto`): the word with nothing
#: flock-like after a "mind"/"mine"/"my" start, or one of these whole.
_OTHER = frozenset(
    {"mindful", "mindfully", "mindfulness", "mindthegap", "mindyourstep"}
)
_OTHER_START = re.compile(r"^(?:mind|mine|mined|my)(?P<rest>[a-z]*)$")
#: The flock-like onset a rest must have to keep the yes ("Mind Flock",
#: "Mind clock", "Mine pluck", "mind-tlook").
_FLOCKISH = re.compile(r"^(?:[fcbpkgt]l|fr)")
#: The longest plain decode :func:`veto` reads ("mind the gap").
_VETO_WORDS = 3


def normalize(text: str) -> str:
    """Lower-cased letters only: ``"Mind flock."`` → ``"mindflock"``."""
    return _NON_LETTERS.sub("", (text or "").lower())


def _edits(a: str, b: str) -> int:
    """Levenshtein distance (the strings are short)."""
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _slack(target: str) -> int:
    """How many edits ``target`` tolerates (see :data:`_MAX_EDITS`)."""
    if len(target) >= 8:
        return _MAX_EDITS
    return 1 if len(target) >= 5 else 0


def _word(word: str) -> str:
    """The word as configured: lower case, single spaces; ours when empty."""
    return " ".join((word or "").split()).lower() or WAKE_WORD


def hint(word: str = WAKE_WORD) -> str:
    """The word as the recogniser's hint: ``"MindFlock"`` for ours, else the
    word with each part capitalised (``"hey jarvis"`` → ``"Hey Jarvis"``)."""
    w = " ".join((word or "").split()).lower()
    if not w or normalize(w) == WAKE_WORD:
        return "MindFlock"
    return " ".join(p.capitalize() for p in w.split())


def _tokens(text: str, word: str = WAKE_WORD) -> List[str]:
    """The words of ``text``, lower case, a greeting in front
    (:data:`LEADS`) dropped unless ``word`` starts with it, and an exact
    repeat of a word collapsed ("mindflock mindflock" → one)."""
    toks = [t.lower() for t in _TOKEN_SPLIT.split(text or "") if t]
    own = _word(word).split()
    if len(toks) > 1 and toks[0] in LEADS and own[0] != toks[0]:
        toks = toks[1:]
    out: List[str] = []
    for t in toks:
        if not out or t != out[-1]:
            out.append(t)
    return out


def _strict(toks: List[str]) -> str:
    """Ours, strictly: the reason it matched ("form", "sound") or ""."""
    if len(toks) > 1 + _EXTRA_WORDS:
        return ""
    heard = "".join(toks)
    if heard == WAKE_WORD:
        return "word"
    if heard in _NOT_OURS:
        return ""
    forms = {normalize(f) for f in _voice_words.heard_of([WAKE_WORD])}
    if heard in forms:
        return "form"
    if len(toks) == 1 and _SOUND_ONE.match(heard):
        return "sound"
    if len(toks) == 2 and _SOUND_FIRST.match(toks[0]) and _SOUND_SECOND.match(toks[1]):
        return "sound"
    return ""


def judge(text: str, word: str = WAKE_WORD) -> Tuple[bool, str]:
    """``(wake, reason)`` for ``text`` (a recognised short clip). A wake's
    reason says how it matched: ``"word"`` (the letters exactly), ``"form"``
    (a known form, ``voice_words.HEARD``), ``"sound"`` (ours' sound shape,
    :data:`_SOUND_ONE`) or ``"near"`` (a custom word within its letter
    slack); a no's: ``"empty"``, ``"long"`` (more words than the word
    alone) or ``"other"``."""
    toks = _tokens(text, word)
    if not toks:
        return False, "empty"
    w = _word(word)
    target = normalize(w) or WAKE_WORD
    parts = len(w.split())
    if len(toks) > parts + _EXTRA_WORDS:
        return False, "long"
    heard = "".join(toks)
    if target == WAKE_WORD:
        how = _strict(toks)
        return (True, how) if how else (False, "other")
    if heard == target:
        return True, "word"
    if heard in (normalize(f) for f in _voice_words.heard_of([w])):
        return True, "form"
    slack = _slack(target)
    if abs(len(heard) - len(target)) <= slack and _edits(heard, target) <= slack:
        return True, "near"
    return False, "other"


def is_wake(text: str, word: str = WAKE_WORD) -> bool:
    """Whether ``text`` is the wake word on its own (:func:`judge`'s yes)."""
    return judge(text, word)[0]


def veto(plain: str) -> str:
    """Why an un-prompted decode of a clip the prompted one called ours
    turns that yes down — ``""`` to keep it. It does when the plain text,
    a greeting and repeats dropped, is up to three words (:data:`_VETO_WORDS`)
    that start like "mind"/"mine"/"my" with nothing flock-like after
    ("mind", "Mine.", "Mindset.", "Minefield.", "mind again", "Mind fog.",
    "mind the gap"), or a known other word ("Mindful."). Anything else keeps
    the yes: an empty or odd plain decode ("Men flat.", "Amen.") — and a
    bare "My", which the bench saw as often for a cut-off "MindFlock" as
    for "Mine." — is the plain model missing the word, not hearing another
    one."""
    toks = _tokens(plain)
    if not toks or len(toks) > _VETO_WORDS:
        return ""
    heard = "".join(toks)
    if heard in _OTHER:
        return "plain:" + heard
    if heard == "my":
        return ""
    m = _OTHER_START.match(heard)
    if not m:
        return ""
    rest = m.group("rest")
    # "mindflock" written plainly, or a flock-like rest: still ours.
    if _strict(toks) or _FLOCKISH.match(rest) or _FLOCKISH.match(rest[1:]):
        return ""
    return "plain:" + heard
