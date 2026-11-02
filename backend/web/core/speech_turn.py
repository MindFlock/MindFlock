"""End of turn: how long the quiet after a phrase must last before the
phrase is over, from what was said so far (the stream chunk's ``turn``,
``routes/speech.py``; docs/web-api.md).

Both clients end a phrase after a fixed ~1.1 s of quiet. That is too long
for a lone command and too short for someone who stopped on "and…". This
module reads the words a stream has so far (committed plus the unstable
tail, whisper's punctuation and all) and returns a :class:`Turn`:
``hold_ms`` is the quiet after the last loud frame that ends the phrase,
``p`` is how likely the person has finished, and ``reason`` names the rule
that decided. The rules, first match wins:

=============  ===========================================  ==========
reason         the words so far                             hold
=============  ===========================================  ==========
``empty``      nothing heard yet                            default
``unheard``    loud audio no fast pass has heard yet        default
``control``    end in a radio / stop / hush phrase          short
               (``voice_words.control_kind``: "over",
               "fix it, disregard", "stop listening"), or
               in one and a stray connector after its full
               stop ("over. The": whisper's filler for the
               quiet)
``command``    ARE a voice-router command that takes no     short
               argument ("repeat", "next session",
               "what needs me"; ``voice_router.command``)
``wake``       are only the wake word ("Mindflock,", or a     long
               form or sound-alike of it: "Mind clock")
``comma``      end in a comma, dash, colon or ellipsis      long
``question``   end in "?" or "!" (two words or more)        :data:`QUESTION_MS`
``connector``  end in a connector, article, preposition,    long
               auxiliary, pronoun or filler ("and", "the",
               "to", "is", "I", "um")
``sentence``   end in "." with :data:`MIN_SENTENCE_WORDS`   :data:`SENTENCE_MS`
               or more
``default``    anything else                                default
=============  ===========================================  ==========

The numbers follow what other voice products ship (Alexa ~500 / 750 ms for
commands vs open requests, Talon 300 ms for commands, Vapi 1.5 s with no
punctuation): :data:`SHORT_MS` 350, :data:`DEFAULT_MS` 1100 (today's fixed
quiet), :data:`LONG_MS` 1500. Punctuation gets less than those products give
it — :data:`QUESTION_MS` and :data:`SENTENCE_MS` are 900, not 600 / 700 —
because whisper writes a full stop (sometimes a "?") at EVERY pause: the
owner dictates "fix the failing test, [pause] over", and at 700 ms a pause of
900 ms before the radio word ended 17 of the 27 such bench phrases early
(``tests/bench/turn_bench.py``, pauses stretched to 900 ms); at 900 none,
for 34 ms less saved on average as recorded.

A short hold applies to the WHOLE phrase being a command, or to its last
words being a radio phrase — what the clients already act on after 250 ms
of quiet, so the short hold never ends a phrase a client would not. A "."
counts only on a phrase of a few words (:data:`MIN_SENTENCE_WORDS`).

``p`` starts at the rule's prior (how often a phrase like that is finished)
and rises linearly with the quiet to 1 at ``hold_ms``: a client that would
rather act on a probability than a deadline reads it the same way.

Pure; no I/O. ``voice_router`` is imported when first needed (it pulls in
session code the stream does not otherwise need).
"""

from __future__ import annotations

import re
from typing import Any, NamedTuple, Optional

from backend.web.core import voice_words as _voice_words

__all__ = [
    "COMMAND_KINDS",
    "CONNECTORS",
    "DEFAULT_MS",
    "LONG_MS",
    "MIN_SENTENCE_WORDS",
    "QUESTION_MS",
    "SENTENCE_MS",
    "SHORT_MS",
    "Turn",
    "score",
]

#: A lone command or a radio phrase at the end.
SHORT_MS = 350
#: Ends in "?" or "!".
QUESTION_MS = 900
#: Ends in "." and is a sentence (:data:`MIN_SENTENCE_WORDS`).
SENTENCE_MS = 900
#: Anything else: the clients' fixed quiet before this module.
DEFAULT_MS = 1100
#: Trails off: a connector, a comma, the wake word alone.
LONG_MS = 1500
#: Words a phrase ending in "." needs before the full stop counts.
MIN_SENTENCE_WORDS = 3

#: ``voice_router.command`` kinds that take no argument: the whole phrase is
#: the command, so nothing more is coming. ``switch`` (a session name may go
#: on), ``new``, ``question``, ``answer`` and ``pick`` are left out.
COMMAND_KINDS = frozenset(
    (
        "cancel",
        "list",
        "needs",
        "status",
        "condense",
        "repeat",
        "off",
        "hush",
        "stop",
        "switch_rel",
        "switch_index",
    )
)

#: Last words that say more is coming. Not "that", "it", "this", "one",
#: "you", "her", "well", "over", "out", "up", "on", "off": each ends plenty of
#: finished phrases ("revert that", "thank you", "turn it off"). A "?" wins
#: over a connector ("what is it for?").
CONNECTORS = frozenset("""
    and or but nor so because cause cuz then plus also either neither
    if when while whereas although though unless until than as whether
    the a an
    to of in at by for with from into onto about like via under after
    before between through without within against toward towards per
    my your our their his its whose which who whom what where
    is are was were be been being am do does did have has had
    will would should could can may might must shall
    i we they he she
    not let's lets
    um uh er erm uhm hmm mm
    """.split())

# Prior that the phrase is finished, by reason.
_PRIOR = {
    "empty": 0.0,
    "unheard": 0.3,
    "control": 0.95,
    "command": 0.9,
    "wake": 0.1,
    "comma": 0.2,
    "connector": 0.15,
    "question": 0.85,
    "sentence": 0.75,
    "default": 0.5,
}
_HOLD = {
    "empty": DEFAULT_MS,
    "unheard": DEFAULT_MS,
    "control": SHORT_MS,
    "command": SHORT_MS,
    "wake": LONG_MS,
    "comma": LONG_MS,
    "connector": LONG_MS,
    "question": QUESTION_MS,
    "sentence": SENTENCE_MS,
    "default": DEFAULT_MS,
}

_TRAIL_OPEN = re.compile(r"(?:,|;|:|-|–|—|\.\.\.|…)$")
_WAKE_LEADS = ("hey", "ok", "okay")


class Turn(NamedTuple):
    """One verdict (:func:`score`)."""

    p: float
    hold_ms: int
    reason: str

    def json(self) -> dict:
        """The chunk response's ``turn``."""
        return {"p": self.p, "hold_ms": self.hold_ms, "reason": self.reason}


def _command(text: str, words: dict) -> bool:
    from backend.web.core import voice_router as _router

    try:
        got = _router.command(text, words)
    except Exception:  # noqa: BLE001 — a verdict, never a failure
        return False
    return bool(got) and got.get("kind") in COMMAND_KINDS


def _stray(tail: str, said: list, words: dict) -> bool:
    """``tail`` is a finished radio phrase plus one connector after its
    full stop ("Fix it, over. The"): whisper's filler for the quiet after
    the speech (``speech_stream.SILENCE_SLACK``), not more words — unless the
    two make the start of a longer control phrase ("Over. And" → "over and
    out")."""
    head = tail.rsplit(None, 1)
    if len(head) != 2 or head[0][-1:] not in (".", "?", "!"):
        return False
    for kind in _voice_words.KINDS:
        for p in words.get(kind) or ():
            pw = _voice_words._words(p)
            if any(said[-k:] == pw[:k] for k in range(1, len(pw))):
                return False
    return bool(_voice_words.control_kind(head[0], words))


def _reason(text: str, words: dict) -> str:
    said = _voice_words._words(text)
    if not said:
        return "empty"
    if _voice_words.control_kind(text, words):
        return "control"
    if _command(text, words):
        return "command"
    wake = _voice_words._words(words.get("wake") or "")
    rest = said[1:] if len(said) > 1 and said[0] in _WAKE_LEADS else said
    forms = [wake] + [_voice_words._words(f) for f in words["heard"].get("wake", ())]
    if wake and (
        rest in forms or _voice_words.sounds_like("".join(rest), "".join(wake))
    ):
        return "wake"
    tail = text.rstrip()
    if _TRAIL_OPEN.search(tail):
        return "comma"
    if len(said) >= 2 and tail[-1:] in ("?", "!"):
        return "question"
    if said[-1] in CONNECTORS:
        return "control" if _stray(tail, said, words) else "connector"
    if len(said) >= MIN_SENTENCE_WORDS and tail.endswith("."):
        return "sentence"
    return "default"


def score(
    text: Any,
    quiet_ms: float = 0,
    heard: bool = True,
    words: Any = None,
) -> Turn:
    """The end-of-turn verdict for the words so far (``text``: committed and
    tentative, as the recogniser wrote them) after ``quiet_ms`` of quiet
    since the last loud frame. ``heard``: a fast pass has heard every loud
    frame (False: the words are behind the voice — ``unheard``, the default
    hold). ``words``: the effective voice words (``voice_words.effective``
    or the settings; None = the defaults). See the module doc."""
    w = _voice_words.effective(words if words is not None else {})
    text = str(text or "")
    reason = _reason(text, w)
    if reason not in ("empty",) and not heard:
        reason = "unheard"
    hold = _HOLD[reason]
    prior = _PRIOR[reason]
    if reason == "empty":
        p = 0.0
    else:
        ramp = min(1.0, max(0.0, float(quiet_ms)) / hold)
        p = prior + (1.0 - prior) * ramp
    return Turn(round(p, 2), hold, reason)


def hold_for(text: Any, words: Optional[dict] = None) -> int:
    """Just the hold for ``text`` (heard, no quiet yet) — for benches."""
    return score(text, 0, True, words).hold_ms
