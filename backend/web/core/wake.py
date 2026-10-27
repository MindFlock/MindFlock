"""The wake word: is a short utterance the word "MindFlock" on its own?

The desktop app and the phone page can keep the microphone in a standby
that watches the room's sound level and, when a short lone utterance
bounded by silence comes in, send just that clip to ``POST /api/voice/wake``
(routes/speech.py). The server recognises it with the word as the decoder's
hint and asks :func:`is_wake`: the utterance must be the word alone — not a
sentence that happens to contain it ("mindflock is great" is not a wake) —
and it tolerates how whisper tends to hear a made-up word ("Mind flock",
"Mindlock", "mind block", "Mindflock."). Pure; no model here.
"""

from __future__ import annotations

import re

__all__ = ["WAKE_WORD", "is_wake", "normalize"]

#: The word, as the hint and the target. Lower case, no spaces.
WAKE_WORD = "mindflock"
#: How many letters may differ (edits) and still count.
_MAX_EDITS = 2
#: A wake is the word ALONE: at most this many words in the clip.
_MAX_WORDS = 2

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


def is_wake(text: str, word: str = WAKE_WORD) -> bool:
    """Whether ``text`` (a recognised short clip) is the wake word on its
    own. The word alone, in one or two tokens, within :data:`_MAX_EDITS`
    letters of it; a longer utterance never wakes, however it starts."""
    raw = (text or "").strip()
    if not raw:
        return False
    tokens = [t for t in re.split(r"[^A-Za-z]+", raw) if t]
    if not tokens or len(tokens) > _MAX_WORDS:
        return False
    heard = normalize(raw)
    target = normalize(word)
    if not heard or abs(len(heard) - len(target)) > _MAX_EDITS:
        return False
    return _edits(heard, target) <= _MAX_EDITS
