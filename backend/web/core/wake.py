"""The wake word: is a short utterance the word ("MindFlock") on its own?

The desktop app and the phone page can keep the microphone in a standby
that watches the room's sound level and, when a short lone utterance
bounded by silence comes in, send just that clip to ``POST /api/voice/wake``
(routes/speech.py). The server recognises it with the word as the decoder's
hint and asks :func:`is_wake`: the utterance must be the word alone — not a
sentence that happens to contain it ("mindflock is great" is not a wake) —
and it tolerates how whisper tends to hear a made-up word ("Mind flock",
"Mindlock", "mind block", "Mindflock."). The word is the person's
``voice.words`` ``wake`` (``core.voice_words``; "mindflock" by default): one
word or two, the slack shrinking with its length so a short word does not
wake on everything. Pure; no model here.
"""

from __future__ import annotations

import re

__all__ = ["WAKE_WORD", "hint", "is_wake", "normalize"]

#: The word, as the hint and the target. Lower case, no spaces.
WAKE_WORD = "mindflock"
#: How many letters may differ (edits) and still count — for a word of at
#: least 8 letters; 5–7 letters allow one, a shorter word none.
_MAX_EDITS = 2
#: A wake is the word ALONE: at most this many more words in the clip than
#: the word has ("mind flock" for "mindflock").
_EXTRA_WORDS = 1

_NON_LETTERS = re.compile(r"[^a-z]+")


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


def hint(word: str = WAKE_WORD) -> str:
    """The word as the recogniser's hint: ``"MindFlock"`` for ours, else the
    word with each part capitalised (``"hey jarvis"`` → ``"Hey Jarvis"``)."""
    w = " ".join((word or "").split()).lower()
    if not w or normalize(w) == WAKE_WORD:
        return "MindFlock"
    return " ".join(p.capitalize() for p in w.split())


def is_wake(text: str, word: str = WAKE_WORD) -> bool:
    """Whether ``text`` (a recognised short clip) is the wake word on its
    own. The word alone, in at most one token more than it has, within a
    few letters of it (:func:`_slack`); a longer utterance never wakes,
    however it starts."""
    raw = (text or "").strip()
    if not raw:
        return False
    tokens = [t for t in re.split(r"[^A-Za-z]+", raw) if t]
    target = normalize(word) or WAKE_WORD
    parts = len((word or "").split()) or 1
    if not tokens or len(tokens) > parts + _EXTRA_WORDS:
        return False
    heard = normalize(raw)
    slack = _slack(target)
    if not heard or abs(len(heard) - len(target)) > slack:
        return False
    return _edits(heard, target) <= slack
