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

* OURS ("mindflock", the default) takes anything next to it (the owner's
  rule, 2026-11-05: the strict rule turned down his own "MindFlock" heard as
  "MindFock", "Mind Fuck", "Mindful"): one of the forms the recogniser is
  known to write for it (``core.voice_words.HEARD``: "mind flock", "my
  flock", "mindfock"), its sound shape (``voice_words.WAKE_SOUND``: a "mind"-like
  first part, then an fl/cl/bl/pl/fr/ph onset and a short rest, or a bare
  f and o/u — "Mind Flap", "Minh Flock", "Mind fog"; or a "my"-like one with
  a flock-like vowel after the onset: "My clock", "My fock" — never "My
  flag"), or two letters off the word ("Windlock", "Mild block").
* A word of the person's own (``voice.words`` ``wake``) keeps the letter
  slack, shrinking with its length so a short word does not wake on
  everything.

The hint makes whisper write "MindFlock" for things that are not ("Mind.",
"Mine.", "Mindful."), so the route checks a yes for ours with a second,
un-prompted decode of the same clip and :func:`veto` turns it down when
that one plainly heard another word.

How the two passes decode is here too (:func:`decode_options`,
:data:`PAD_SECONDS`), for ``core.speech`` to apply: a lone word on a
short window, greedy, once, no no-speech filter — measured by
``tests/bench/wake_stream_bench.py`` (docs/voice-wake.md). Pure; no model
here.
"""

from __future__ import annotations

import re
from typing import List, Tuple

from backend.web.core import voice_words as _voice_words

__all__ = [
    "LEADS",
    "MAX_TOKENS",
    "PAD_SECONDS",
    "WAKE_WORD",
    "decode_options",
    "hint",
    "REST_MAX",
    "is_wake",
    "judge",
    "judge_rest",
    "normalize",
    "veto",
    "veto_rest",
]

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

#: Ours as it sounds — "mind"/"my"-like first part and a flock-like rest,
#: one token or two ("mind" + "flunk"), so "mindflock now" never fits:
#: ``core.voice_words.wake_like`` (:data:`voice_words.WAKE_SOUND`), the
#: family a request said mid-dictation starts with too.

#: What the un-prompted decode writes when the clip was another word that
#: the hint turned into "MindFlock" (:func:`veto`): the word with nothing
#: flock-like after a "mind"/"mine"/"my" start, or one of these whole.
_OTHER = frozenset({"mindthegap", "mindyourstep"})
_OTHER_START = re.compile(r"^(?:mind|mine|mined|my)(?P<rest>[a-z]*)$")
#: The flock-like onset a rest must have to keep the yes ("Mind Flock",
#: "Mind clock", "Mine pluck", "mind-tlook").
_FLOCKISH = re.compile(r"^(?:[fcbpkgt]l|fr|ph|f[aou])")
#: The longest plain decode :func:`veto` reads ("mind the gap").
_VETO_WORDS = 3


#: The encoder input a wake pass pads its clip to, in seconds
#: (``core.speech``'s short padding, as the streaming passes have):
#: Whisper's own 30 s made a lone word's pass ~0.8 s on the fast tier's two
#: threads (1.5–8 s on the owner's loaded box); 10 s takes it to ~90 ms at
#: the same recall. Under 10 s whisper invents ("Mind Mind", "M M M M").
PAD_SECONDS = 10.0
#: Tokens a wake pass decodes at most: the word, a greeting, a request
#: said in the same breath ("MindFlock, switch to sitecheck bot seven" —
#: the clients send up to 4 s) and their punctuation; a looping decoder
#: ("MindFlock MindFlock …") stops there.
MAX_TOKENS = 32
#: Most words a request said with the word in one breath may have
#: (:func:`judge_rest`): more is a sentence.
REST_MAX = 8


def decode_options(prompt: str = "") -> dict:
    """faster-whisper ``transcribe`` keywords for a wake pass (``prompt``:
    the hint, :func:`hint`; ``""`` for the veto's plain pass), the
    language aside: greedy (``beam_size`` 1); ONE decode
    (``temperature`` 0 — the fallback re-decodes a low-confidence lone word
    at five temperatures, seconds on a busy CPU, for nothing the judge
    takes); no VAD (the clip is the word already; the VAD can eat a short
    one); NO no-speech filter (``no_speech_threshold`` None: a short clip
    padded out reads as "no speech" and an uncertain decode of a real word
    came back as nothing — the judge rejects what is not the word, and on
    the bench noise never woke without the filter); at most
    :data:`MAX_TOKENS`. Never ``word_timestamps``: they send the prompted
    decoder into "MindFlock MindFlock" loops. A key this faster-whisper does
    not take is the caller's to drop."""
    kw = {
        "beam_size": 1,
        "vad_filter": False,
        "temperature": 0.0,
        "no_speech_threshold": None,
        "condition_on_previous_text": False,
        "max_new_tokens": MAX_TOKENS,
    }
    if prompt:
        kw["initial_prompt"] = prompt
    return kw


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
    """Ours, strictly: the reason it matched ("word", "form", "sound",
    "near") or "" — ``core.voice_words.wake_like``, the family the
    mid-dictation wake (``voice_words.wake_at``) takes too: the letters, a
    known form, the sound shape ("Mind Flap", "My clock", "Mindful luck"),
    or two letters off ("Windlock", "Minflog"): the owner's rule is that
    whatever sounds next to the word wakes; the veto still turns down a
    plain "Mind." / "Mine."."""
    if len(toks) > 1 + _EXTRA_WORDS:
        return ""
    return _voice_words.wake_like(toks)


def judge(text: str, word: str = WAKE_WORD) -> Tuple[bool, str]:
    """``(wake, reason)`` for ``text`` (a recognised short clip). A wake's
    reason says how it matched: ``"word"`` (the letters exactly), ``"form"``
    (a known form, ``voice_words.HEARD``), ``"sound"`` (ours' sound shape,
    ``voice_words.WAKE_SOUND``) or ``"near"`` (a custom word within its letter
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


def judge_rest(text: str, word: str = WAKE_WORD) -> Tuple[bool, str, str]:
    """:func:`judge`, and for a clip that is the word with a request after
    it in one breath ("MindFlock, next session"; the clients send up to
    4 s): ``(True, "rest", rest)`` — ``rest`` the words after the word
    (``voice_words.wake_lead`` / ``rest_after``: its heard forms and sound
    shape, a greeting in front, leading punctuation dropped), 1 to
    :data:`REST_MAX` of them, a request (``voice_words.wake_asks``: open,
    switch, next, what … — never an answer or a send). A sentence that
    merely starts like the word ("My clock is broken", "mindful of the
    time") stays ``"long"``. ``rest`` is ``""`` for any other outcome.
    The hint writes "MindFlock" for "Mind …" too: the route checks a rest
    with :func:`veto_rest`."""
    wake, reason = judge(text, word)
    if wake or reason != "long":
        return wake, reason, ""
    words = {"wake": _word(word)}
    at = _voice_words.wake_lead(text, words)
    if at is None:
        return False, reason, ""
    rest = _voice_words.rest_after(text, at)
    n = len(_tokens(rest))
    if n == 0 and normalize(word) == WAKE_WORD:
        # "Mindful, I'll." — the word heard as "Mindful" and a tail, alone.
        return True, "sound", ""
    if not 1 <= n <= REST_MAX or not _voice_words.wake_asks(rest):
        return False, reason, ""
    return True, "rest", rest


def veto_rest(plain: str, rest: str) -> str:
    """Why the un-prompted decode turns down a :func:`judge_rest` yes with a
    ``rest`` — ``""`` to keep it. It keeps it when the plain text also
    starts like the word (``voice_words.wake_lead``: "Mind flock, open
    intake.", "Mindful, open intake.") or ends with the same words as the
    rest after up to three words of its own that :func:`veto` does not
    turn down ("Line flock open intake"); it turns it down when those
    words are plainly another ("Mind, open settings." — "mind"), when
    nothing comes before the rest (the hint put a "MindFlock" in front of
    "open settings"), and when the plain text is something else
    altogether. A failed or empty plain pass is the caller's (it keeps the
    yes, as for a lone word)."""
    if _voice_words.wake_lead(plain) is not None:
        return ""
    said = _tokens(plain, "")
    want = _tokens(rest, "")
    if want and len(said) > len(want) and said[-len(want) :] == want:
        lead = said[: -len(want)]
        if len(lead) <= _VETO_WORDS and not veto(" ".join(lead)):
            return ""
    return "plain:" + normalize(plain)[:40]
