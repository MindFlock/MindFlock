"""The voice keywords: the words that send, end, clear, stop and hush, and
the wake word — ours by default, the person's own once they change them.

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
person adds is matched whole. Pure; no I/O.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List

__all__ = [
    "DEFAULTS",
    "KINDS",
    "MAX_PHRASE",
    "MAX_PHRASES",
    "effective",
    "normalize",
    "phrase_regex",
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

#: Longest phrase (characters).
MAX_PHRASE = 40
#: Most phrases per kind.
MAX_PHRASES = 6
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
    default — so ``{}`` means all defaults."""
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


def effective(settings: Any) -> dict:
    """:data:`DEFAULTS` overlaid with the saved words: every kind present.
    ``settings`` is the :class:`~backend.config.settings.Settings`, its
    ``voice`` group, or the ``words`` dict itself."""
    words = settings
    for attr in ("voice", "words"):
        if hasattr(words, attr):
            words = getattr(words, attr)
    saved = normalize(words)
    out = {k: list(DEFAULTS[k]) for k in KINDS}
    out["wake"] = DEFAULTS["wake"]
    out.update(saved)
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
