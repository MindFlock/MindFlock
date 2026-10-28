"""The voice keywords: the words that send, end, clear, stop and hush, and
the wake word — ours by default, the person's own once they change them —
and how a recogniser's near miss of one still counts.

Settings → Voice saves ``voice.words`` (a synced personal policy, like
``approve_dialogs``); ``GET /api/voice`` serves :func:`effective` as
``words`` so every client builds its matchers from the server, and the voice
router (``core.voice_router``: ``stop``, ``hush``) and the wake route
(``core.wake``) read the same dict. The clients own ``send`` ("over": type
the pending words and press Enter, keep listening), ``end`` ("over and out":
send and stop) and ``clear`` ("disregard": drop the pending words).

``DEFAULTS`` lists each kind's canonical phrases. The router keeps a few
spoken variants of a default phrase ("stop the microphone", "that is all",
"I'm done talking") as long as that phrase is in the list; a phrase the
person adds is matched whole.

Whisper does not always write the word that was said: "over" comes out as
"ova", "submit" as "some it", "disregard" as "this regard". Two layers make
the matchers tolerant without new false positives, both defined here once
and consumed by the router, the desktop (``lib/voiceWords.ts``,
``lib/voiceDesktop.ts``) and the phone (``mobile.js``):

- :data:`HEARD`, a curated table of what the recogniser writes for each
  default phrase, served as ``words.heard`` (:func:`heard`) so the clients
  need no table of their own;
- :func:`sound_key`, a small metaphone-like key, and :func:`sounds_like`,
  which lets a phrase the person added ("roger") be heard a letter off
  ("rodger") — a whole-phrase comparison at the end of an utterance only,
  guarded by an edit-distance margin that shrinks with the word's length.

:func:`control_kind` is the reference reading of one finished phrase — the
rule the desktop's and the phone's matchers follow — pinned against both by
``tests/unit/data/voice_sounds.json``. Pure; no I/O.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "DEFAULTS",
    "HEARD",
    "HEARD_MAX",
    "KINDS",
    "MAX_PHRASE",
    "MAX_PHRASES",
    "control_kind",
    "effective",
    "heard",
    "heard_of",
    "normalize",
    "phrase_regex",
    "sound_key",
    "sound_tail",
    "sounds_like",
]

#: The phrase kinds, in Settings order (``wake`` is a single word, not a list).
KINDS = ("send", "end", "clear", "stop", "hush")

DEFAULTS: Dict[str, Any] = {
    "send": ["over"],
    "end": ["over and out", "over, out"],
    "clear": ["disregard"],
    "stop": [
        "stop",
        "stop listening",
        "mic off",
        "pause listening",
        "that's all",
        "done talking",
    ],
    "hush": ["stop talking", "be quiet", "quiet", "hush", "shut up", "enough"],
    "wake": "mindflock",
}

#: What the recogniser tends to WRITE for a default phrase, by phrase (not
#: kind: a phrase moved to another kind keeps its forms; one the person adds
#: has none here and gets :func:`sounds_like` instead). Every entry is a
#: mishearing seen in practice AND not a plausible last word of dictation —
#: so "hover" and "ever" are not "over", "discard" is not "disregard". A
#: form is matched like the phrase it belongs to: a radio word's at the end
#: of any phrase, a hush word's as the whole phrase.
HEARD: Dict[str, Tuple[str, ...]] = {
    "over": ("ova", "ovah", "oh ver", "o ver", "overt"),
    "over and out": (
        "over an out",
        "over in out",
        "over and now",
        "over en out",
        "overran out",
        "ova and out",
        "ovah and out",
    ),
    "over, out": ("ova out", "over owt"),
    "disregard": (
        "dis regard",
        "this regard",
        "just regard",
        "the regard",
        "disregarded",
        "disregards",
        "this is guard",
    ),
    "stop": ("stock", "stopped"),
    "stop listening": (
        "stop listing",
        "stop listen",
        "stopped listening",
        "stop lisning",
    ),
    "mic off": ("mike off", "mic of", "mike of"),
    "pause listening": (
        "pause listing",
        "paws listening",
        "paused listening",
        "pause listen",
    ),
    "that's all": ("that's al", "that's hall"),
    "done talking": ("done talkin", "dun talking", "done torking"),
    "stop talking": ("stop talkin", "stop torking", "stopped talking"),
    "be quiet": ("be quite", "b quiet", "be quiets"),
    "quiet": ("quite", "quiets"),
    "hush": ("hushed", "husch"),
    "shut up": ("shadap", "shut app"),
    "enough": ("enuf", "a nuff", "inough"),
    "mindflock": (
        "mind flock",
        "mine flock",
        "mind lock",
        "mindlock",
        "my flock",
        "mind block",
        "mind flog",
        "min flock",
        "mindflocked",
    ),
}

#: Longest phrase (characters).
MAX_PHRASE = 40
#: Most phrases per kind.
MAX_PHRASES = 6
#: Most heard forms served per kind (``words.heard``).
HEARD_MAX = 24
#: The wake word's length bounds (letters, a space between two words counted).
WAKE_MIN = 3
WAKE_MAX = 20

_PHRASE = re.compile(r"^[a-z0-9 ',]{1,%d}$" % MAX_PHRASE)
_WAKE = re.compile(r"^[a-z]+(?: [a-z]+)?$")
# JS's syntax characters: special in a Python or a JS pattern, and the only
# ones JS's ``u`` flag lets a backslash escape. A valid phrase holds none of
# them, but the escape is the contract.
_SPECIAL = re.compile(r"([\\^$.|?*+()\[\]{}/])")


def _clean(text: Any) -> str:
    """Lower case, curly apostrophes straightened, whitespace collapsed, a
    stray space before a comma dropped."""
    s = str(text if text is not None else "").lower().replace("’", "'")
    s = re.sub(r"\s+", " ", s).strip()
    return s.replace(" ,", ",").strip(" ,")


def _phrases(raw: Any) -> List[str]:
    """The valid phrases of one kind, deduplicated in order, at most
    :data:`MAX_PHRASES`."""
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    out: List[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        p = _clean(item)
        if not _PHRASE.match(p) or not re.search(r"[a-z0-9]", p) or p in out:
            continue
        out.append(p)
        if len(out) == MAX_PHRASES:
            break
    return out


def _wake(raw: Any) -> str:
    """The wake word, or ``""`` when it is not one or two words of letters,
    :data:`WAKE_MIN`–:data:`WAKE_MAX` characters."""
    if not isinstance(raw, str):
        return ""
    w = _clean(raw)
    if not _WAKE.match(w) or not WAKE_MIN <= len(w) <= WAKE_MAX:
        return ""
    return w


def normalize(raw: Any) -> dict:
    """The saved form of ``voice.words``: only the kinds that differ from
    :data:`DEFAULTS`, each cleaned (lower case, trimmed, deduplicated, valid
    phrases only, at most :data:`MAX_PHRASES`). A kind that is missing,
    invalid or left with no valid phrase is dropped — it falls back to the
    default — so ``{}`` means all defaults. ``heard`` is never saved: it is
    derived (:func:`heard`)."""
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for kind in KINDS:
        got = _phrases(raw.get(kind))
        if got and got != DEFAULTS[kind]:
            out[kind] = got
    w = _wake(raw.get("wake"))
    if w and w != DEFAULTS["wake"]:
        out["wake"] = w
    return out


def heard_of(phrases: Iterable[str]) -> List[str]:
    """The :data:`HEARD` forms of ``phrases``, in order, each once, none that
    is itself one of the phrases, at most :data:`HEARD_MAX`."""
    own = [p for p in phrases if isinstance(p, str)]
    out: List[str] = []
    for p in own:
        for f in HEARD.get(p, ()):
            if f not in own and f not in out:
                out.append(f)
                if len(out) == HEARD_MAX:
                    return out
    return out


def heard(words: dict) -> Dict[str, List[str]]:
    """``words.heard``: for each kind the forms of the phrases in its list,
    and for ``wake`` the forms of the wake word — always every key, an empty
    list where nothing is known (the person's own phrases)."""
    out = {k: heard_of(words.get(k) or ()) for k in KINDS}
    out["wake"] = heard_of([str(words.get("wake") or "")])
    return out


def effective(settings: Any) -> dict:
    """:data:`DEFAULTS` overlaid with the saved words: every kind present,
    plus ``heard`` (:func:`heard`). ``settings`` is the
    :class:`~backend.config.settings.Settings`, its ``voice`` group, or the
    ``words`` dict itself (an effective one, ``heard`` and all, reads the
    same)."""
    words = settings
    for attr in ("voice", "words"):
        if hasattr(words, attr):
            words = getattr(words, attr)
    saved = normalize(words)
    out = {k: list(DEFAULTS[k]) for k in KINDS}
    out["wake"] = DEFAULTS["wake"]
    out.update(saved)
    out["heard"] = heard(out)
    return out


def phrase_regex(phrases: Iterable[str]) -> str:
    """One non-capturing alternation of ``phrases``, longest first, each
    escaped, a comma optional (``"over, out"`` also matches ``"over out"``):
    ``(?:over and out|over,? out)``. No anchors, flags, lookbehind or named
    groups, so it compiles the same in Python's ``re`` and in JavaScript
    (``new RegExp(src, "i")``). No phrases → ``(?!)``, which never matches."""
    seen: List[str] = []
    for p in phrases or ():
        if isinstance(p, str) and p and p not in seen:
            seen.append(p)
    if not seen:
        return "(?!)"
    seen.sort(key=len, reverse=True)
    alts = [_SPECIAL.sub(r"\\\1", p).replace(",", ",?") for p in seen]
    return "(?:%s)" % "|".join(alts)


# --- the sound of a phrase ---------------------------------------------------------
# The key's rewrites, in order, on the lower-cased letters and digits of a
# phrase run together. Each is a plain pattern → literal, so the same table
# runs through Python's ``re.sub`` and JavaScript's ``replace`` (lib/voiceWords
# ``SOUND_RULES``, mobile.js ``VOICE_SOUND_RULES``): silent starts, "ph" → f,
# the sh/ch class → X, "th" → 0, c/q/x/g resolved, voiced consonants folded
# onto their unvoiced twins (b→p, d→t, v→f, z→s, g→k). Then the first letter
# stays as it is, every other vowel and h/w/y goes, and runs collapse.
_SOUND_RULES: Tuple[Tuple[str, str], ...] = (
    (r"^[kgp]n", "n"),
    (r"^wr", "r"),
    (r"^x", "s"),
    (r"^wh", "w"),
    (r"mb$", "m"),
    (r"x", "ks"),
    (r"ph", "f"),
    (r"sch", "sk"),
    (r"ch", "X"),
    (r"sh", "X"),
    (r"th", "0"),
    (r"gh$", "f"),
    (r"gh", ""),
    (r"ck", "k"),
    (r"q", "k"),
    (r"c(?=[eiy])", "s"),
    (r"c", "k"),
    (r"dg(?=[eiy])", "j"),
    (r"g(?=[eiy])", "j"),
    (r"b", "p"),
    (r"d", "t"),
    (r"v", "f"),
    (r"z", "s"),
    (r"g", "k"),
)
_SOUND_RX = tuple((re.compile(p), r) for p, r in _SOUND_RULES)
_LETTERS = re.compile(r"[^a-z0-9]+")
_SOUND_SOFT = re.compile(r"[aeiouhwy]")
_SOUND_RUNS = re.compile(r"(.)\1+")


def _letters(text: Any) -> str:
    """The lower-cased letters and digits of ``text``, run together
    (``"Over, out!"`` → ``"overout"``; a curly apostrophe is nothing)."""
    return _LETTERS.sub("", str(text or "").lower().replace("’", "'"))


def sound_key(text: Any) -> str:
    """A compact phonetic key of ``text`` (one word or a phrase): ``"over"``
    → ``"ofr"``, ``"rodger"`` and ``"roger"`` → ``"rjr"``, ``"this regard"``
    → ``"0srkrt"``. Letters that sound alike share a key; vowels after the
    first letter do not count. ``""`` for no letters."""
    s = _letters(text)
    if not s:
        return ""
    for rx, rep in _SOUND_RX:
        s = rx.sub(rep, s)
    if not s:
        return ""
    key = s[0] + _SOUND_SOFT.sub("", s[1:])
    return _SOUND_RUNS.sub(r"\1", key)


def _edits(a: str, b: str) -> int:
    """Damerau–Levenshtein distance (adjacent letters swapped count one;
    the strings are short)."""
    la, lb = len(a), len(b)
    d = [[0] * (lb + 1) for _ in range(la + 1)]
    for i in range(la + 1):
        d[i][0] = i
    for j in range(lb + 1):
        d[0][j] = j
    for i in range(1, la + 1):
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[la][lb]


def _slack(n: int) -> int:
    """How many letters may differ for a phrase of ``n`` letters: two from
    eight up, one from five, none below (``core.wake`` keeps the same
    ladder) — so a four-letter word is only ever itself."""
    if n >= 8:
        return 2
    return 1 if n >= 5 else 0


def sounds_like(said: Any, phrase: Any) -> bool:
    """Is ``said`` (the last words of an utterance) ``phrase`` heard a little
    off? Their letters run together must be within :func:`_slack` edits of
    each other AND share a :func:`sound_key` (one key edit allowed from five
    key letters): "rodger" is "roger", "pipe town" is "pipe down", "this
    regard" is "disregard" — "hover" and "ever" are not "over", "hold" is
    not "halt", "sound" is not "send". A phrase of eight letters or more
    also takes a homophone spelt one letter further off — about as long,
    with the same key ("sight check" for sitecheck; never a phrase cut
    short: "over and" is not "over and out")."""
    a, b = _letters(said), _letters(phrase)
    if not a or not b:
        return False
    if a == b:
        return True
    d = _edits(a, b)
    slack = _slack(len(b))
    ka, kb = sound_key(a), sound_key(b)
    if d <= slack:
        return ka == kb or (len(kb) >= 5 and _edits(ka, kb) <= 1)
    return len(b) >= 8 and d == slack + 1 and abs(len(a) - len(b)) <= 1 and ka == kb


def _words(text: Any) -> List[str]:
    """``text`` as matcher words: lower case, apostrophes dropped, anything
    else but letters and digits a break (mobile.js ``voiceNorm`` plus the
    desktop's ``wordKey``)."""
    s = str(text or "").lower().replace("’", "'").replace("'", "")
    return [w for w in re.split(r"[^a-z0-9]+", s) if w]


def sound_tail(
    text: Any, phrases: Sequence[str], tails: Sequence[str] = ()
) -> Optional[Tuple[str, str]]:
    """The last words of ``text`` that sound like one of ``phrases``
    (:func:`sounds_like`, the longest phrase tried first, one word more than
    the phrase has allowed for a split like "rod ger") → ``(before, phrase)``
    — the words said before it, trailing punctuation dropped, and the phrase
    as the person set it — or None. ``tails``: words that may follow a
    phrase and still count ("disregard that")."""
    toks = str(text or "").split()
    idx = [i for i, t in enumerate(toks) if _words(t)]
    if not idx:
        return None
    ordered = sorted(
        (p for p in phrases if isinstance(p, str) and _letters(p)),
        key=lambda p: -len(_letters(p)),
    )
    if not ordered:
        return None
    most = max(len(_words(p)) for p in ordered) + 1
    ends = [len(idx)]
    if len(idx) > 1 and "".join(_words(toks[idx[-1]])) in tails:
        ends.append(len(idx) - 1)
    for end in ends:
        for k in range(min(most, end), 0, -1):
            said = "".join("".join(_words(toks[i])) for i in idx[end - k : end])
            for p in ordered:
                if sounds_like(said, p):
                    before = " ".join(toks[: idx[end - k]])
                    return re.sub(r"[\s,.!?]+$", "", before), p
    return None


# --- one finished phrase, read as the clients read it ------------------------------
#: Words that may follow a send or a clear phrase ("disregard that", "over it").
_RADIO_TAILS = ("that", "it", "all", "everything")
#: Said before or after a hush phrase and still a hush ("okay, be quiet please").
_HUSH_LEADS = ("ok", "okay", "hey", "please")
_HUSH_ENDS = ("please", "now")


def _forms(words: dict, kind: str) -> List[str]:
    """``kind``'s phrases and their heard forms, as served."""
    own = [p for p in words.get(kind) or () if isinstance(p, str)]
    more = (words.get("heard") or {}).get(kind) or ()
    return own + [f for f in more if isinstance(f, str) and f not in own]


def _ends_with(ws: List[str], phrase: str, tails: Sequence[str] = ()) -> bool:
    """``ws`` ends with ``phrase``'s words (then at most one of ``tails``)."""
    pw = _words(phrase)
    if not pw:
        return False
    if ws[-len(pw) :] == pw:
        return True
    return (
        bool(tails)
        and len(ws) > len(pw)
        and ws[-1] in tails
        and ws[-len(pw) - 1 : -1] == pw
    )


def control_kind(text: Any, words: Any = None) -> Optional[str]:
    """The control phrase one finished utterance ends with — the rule the
    desktop (``Dictation.tail``: ``isHushPhrase``, ``stopTail``,
    ``parseRadio``) and the phone (``voiceHeard``: ``voiceIsHush``,
    ``voiceTail``, ``voiceDictParse``) read a phrase by — or None (words):

    - ``"hush"``: the WHOLE utterance is a hush phrase, one of its heard
      forms, or sounds like one; an "ok"/"okay"/"hey"/"please" before and a
      "please"/"now" after are allowed;
    - ``"stop"``: it ends in a stop phrase of two words or more, or a heard
      form of one, word for word (a one-word stop phrase is the router's,
      said on its own: ``voice_router.command``);
    - ``"end"`` / ``"send"`` / ``"clear"``: it ends in one of those phrases
      or heard forms (the longest first, a send or clear phrase followed by
      "that" / "it" / "all" / "everything" too), else its last words sound
      like one (:func:`sound_tail`).

    A control phrase never fires with more words after it: "over the bridge"
    and "this regard is important" are dictation. ``words``: the effective
    words (:func:`effective`; None = the defaults)."""
    w = effective(words if words is not None else {})
    ws = _words(text)
    if not ws:
        return None
    whole = list(ws)
    if len(whole) > 1 and whole[0] in _HUSH_LEADS:
        whole = whole[1:]
    if len(whole) > 1 and whole[-1] in _HUSH_ENDS:
        whole = whole[:-1]
    for p in _forms(w, "hush"):
        if _words(p) in (ws, whole):
            return "hush"
    said = " ".join(whole)
    if any(sounds_like(said, p) for p in w["hush"]):
        return "hush"
    for p in _forms(w, "stop"):
        if len(_words(p)) > 1 and _ends_with(ws, p):
            return "stop"
    radio = [(p, "end", ()) for p in _forms(w, "end")]
    radio += [(p, "send", _RADIO_TAILS) for p in _forms(w, "send")]
    radio += [(p, "clear", _RADIO_TAILS) for p in _forms(w, "clear")]
    radio.sort(key=lambda e: (-len(_words(e[0])), -len(e[0])))
    for p, kind, tails in radio:
        if _ends_with(ws, p, tails):
            return kind
    for kind, tails in (("end", ()), ("send", _RADIO_TAILS), ("clear", _RADIO_TAILS)):
        hit = sound_tail(text, w[kind], tails)
        if hit is not None:
            return kind
    return None
