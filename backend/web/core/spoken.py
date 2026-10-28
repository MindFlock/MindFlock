"""The spoken line: what the phone says when a session's turn ends.

A session in voice mode is asked (:data:`INSTRUCTION`) to end every reply
with one line starting ``Say:`` — up to three short plain sentences a person
can hear: the context first (what was being worked on, so a listener who was
away knows where they are), then what was done, then any question. This module lifts that line out of the reply and, when there is none,
makes a deterministic digest of the reply instead. Pure text helpers plus ONE
cached reader:

* :func:`extract_say` — the LAST ``Say:`` line of a reply (also ``**Say:**``,
  ``SAY:``, ``Say —``; leading list/quote markers allowed), markdown
  stripped, ≤ :data:`SAY_MAX_CHARS`. A ``Say:`` inside a code fence is not a
  say line.
* :func:`strip_markdown` — what speech synthesis should never read aloud.
* :func:`digest` — the fallback: drop code, paths, URLs, tables and headings;
  keep the first sentence plus every question; cap ~45 words.
* :func:`has_question` — whether a line asks the listener something.
* :func:`read_reply` — ``<marker dir>/<session>.reply.json`` (the Stop hook's
  ``{"ts", "text"}``), cached on (mtime, size) so the poll path pays a stat.
* :func:`spoken_for` — the ``spoken`` record the rows, the events and the
  ``/voice`` routes all serve: ``{"say", "source": "hook"|"digest"|"none",
  "has_question", "ts"}``.

Nothing here talks to a model: the on-demand condensation
(``POST /voice/condense``) lives with its route, because it is the one step
that costs money and is never taken on a poll.
"""

from __future__ import annotations

import json
import os
import re
import threading
from typing import Dict, Optional, Tuple

from backend.providers import activity_markers as _am
from backend.providers.activity_markers import VOICE_INSTRUCTION as INSTRUCTION

__all__ = [
    "INSTRUCTION",
    "SAY_MAX_CHARS",
    "digest",
    "extract_say",
    "forget",
    "has_question",
    "read_reply",
    "spoken_for",
    "strip_markdown",
]

#: Ceiling on a spoken line (characters) — the row field, the event data and
#: the ``say`` view all honour it.
SAY_MAX_CHARS = 320
#: The digest's word budget.
DIGEST_MAX_WORDS = 45
#: A digest keeps at most this many sentences.
DIGEST_MAX_SENTENCES = 2

# --- the Say: line ----------------------------------------------------------- #
# Leading list / quote markers a model wraps the line in ("- Say: …", "> Say:
# …", "1. Say: …"), then optional emphasis, then the word, then the marker: a
# colon (optionally itself inside the emphasis, "**Say:**") or a dash. An em
# or en dash may touch the word ("Say—Ready."); the ASCII dash needs
# whitespace before it so "say-so" never matches.
_SAY_LINE_RE = re.compile(
    r"^(?:(?:[-*+•]|>|\d+[.)])\s*)*"
    r"(?:\*\*|__|\*|_)?\s*say\s*(?:\*\*|__|\*|_)?\s*"
    r"(?:[:：]|\s*[—–]|\s-+)\s*(?P<rest>.*)$",
    re.IGNORECASE,
)
_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")

# --- markdown ---------------------------------------------------------------- #
_FENCED_BLOCK_RE = re.compile(
    r"(^|\n)\s*(```|~~~)[^\n]*\n.*?(?:\n\s*\2[^\n]*)(?=\n|$)", re.S
)
_UNCLOSED_FENCE_RE = re.compile(r"(^|\n)\s*(?:```|~~~)[^\n]*\n.*$", re.S)
_INLINE_CODE_RE = re.compile(r"`+([^`]*)`+")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_AUTOLINK_RE = re.compile(r"<(https?://[^>\s]+)>")
_HTML_TAG_RE = re.compile(r"</?[A-Za-z][^>]*>")
_BOLD_RE = re.compile(r"(\*\*|__)(.+?)\1", re.S)
_ITALIC_STAR_RE = re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", re.S)
_ITALIC_UNDER_RE = re.compile(r"(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])", re.S)
_STRIKE_RE = re.compile(r"~~(.+?)~~", re.S)
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_QUOTE_RE = re.compile(r"^\s{0,3}>\s?", re.M)
_LIST_RE = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+", re.M)
_RULE_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$", re.M)
_WS_RE = re.compile(r"\s+")

# --- the digest -------------------------------------------------------------- #
_HEADING_LINE_RE = re.compile(r"^\s{0,3}#{1,6}\s")
_TABLE_LINE_RE = re.compile(r"^\s*\|.*\|\s*$|^\s*[|:\- ]+\|[|:\- ]*$")
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.I)
# A path: a token with a slash (an absolute/relative path, a URL fragment, a
# "foo/bar" module), or one ending in a short file extension ("server.py",
# "settings.json:12"). Sentence punctuation after it is not part of the token.
_PATH_TOKEN_RE = re.compile(
    r"(?<!\S)(?:~?[\w.@-]*/[\w./@%+-]*|[\w-]+(?:\.[\w-]+)*\.[A-Za-z][A-Za-z0-9]{0,4}(?::\d+)?)"
    r"(?=[\s,;:.!?)\]]|$)"
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])[\"'”’)]*\s+")
_QUESTION_START_RE = re.compile(
    r"^(?:should|shall|do|does|did|which|what|when|where|who|whom|whose|why|how|"
    r"can|could|would|will|is|are|am|may|might|"
    r"wanna|shouldn't|can't|couldn't|wouldn't|won't|isn't|aren't|don't|doesn't|"
    r"didn't)\b",
    re.I,
)


def strip_markdown(text: str) -> str:
    """``text`` with its markdown taken off for a listener: fenced blocks and
    HTML tags removed; inline code, links, images, emphasis and strike-through
    unwrapped to their text; headings, quotes, list markers and rules
    dropped; whitespace collapsed to single spaces."""
    if not text:
        return ""
    s = str(text).replace("\r\n", "\n").replace("\r", "\n")
    s = _FENCED_BLOCK_RE.sub(r"\1 ", s)
    s = _UNCLOSED_FENCE_RE.sub(r"\1 ", s)
    s = _IMAGE_RE.sub(r"\1", s)
    s = _LINK_RE.sub(r"\1", s)
    s = _AUTOLINK_RE.sub(r"\1", s)
    s = _HTML_TAG_RE.sub(" ", s)
    s = _INLINE_CODE_RE.sub(r"\1", s)
    s = _RULE_RE.sub(" ", s)
    s = _HEADING_RE.sub("", s)
    s = _QUOTE_RE.sub("", s)
    s = _LIST_RE.sub("", s)
    for _ in range(2):  # nested emphasis (***x***, **_x_**)
        s = _BOLD_RE.sub(r"\2", s)
        s = _STRIKE_RE.sub(r"\1", s)
        s = _ITALIC_STAR_RE.sub(r"\1", s)
        s = _ITALIC_UNDER_RE.sub(r"\1", s)
    return _WS_RE.sub(" ", s).strip()


def _cap(text: str, limit: int = SAY_MAX_CHARS) -> str:
    """``text`` cut to ``limit`` characters at a word boundary, with an
    ellipsis when anything was cut."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    sp = cut.rfind(" ")
    if sp > limit // 2:
        cut = cut[:sp]
    return cut.rstrip(" ,;:—–-") + "…"


def extract_say(text: str) -> Optional[str]:
    """The LAST ``Say:`` line of ``text`` (see the module doc for the accepted
    spellings), markdown stripped, ≤ :data:`SAY_MAX_CHARS`; None when the
    reply has none, or the line is empty once stripped. A later ``Say:``
    always wins over an earlier one; lines inside a code fence never count
    (a reply that quotes the instruction is not following it)."""
    if not text:
        return None
    lines = str(text).replace("\r\n", "\n").split("\n")
    # The instruction asks for the reply's LAST line, so that line counts
    # whatever a fence earlier in the reply did — models leave fences open,
    # and an unclosed fence must not swallow the one line the phone needs.
    for raw in reversed(lines):
        if not raw.strip():
            continue
        m = _SAY_LINE_RE.match(raw.strip())
        if m:
            rest = strip_markdown(m.group("rest")).strip(" *_").strip()
            if rest:
                return _cap(rest)
        break
    found: Optional[str] = None
    in_fence = False
    for raw in lines:
        if _FENCE_RE.match(raw):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _SAY_LINE_RE.match(raw.strip())
        if not m:
            continue
        rest = strip_markdown(m.group("rest"))
        # A line that was only emphasis closers ("**Say:**" with nothing after).
        rest = rest.strip(" *_").strip()
        if rest:
            found = rest
    if found is None:
        return None
    return _cap(found)


def has_question(text: str) -> bool:
    """Whether ``text`` asks the listener something: it contains a ``?`` or
    starts with a question word / auxiliary ("should", "do you", "which",
    "can I", …)."""
    if not text:
        return False
    s = str(text).strip()
    if "?" in s:
        return True
    return bool(_QUESTION_START_RE.match(s))


def _is_path_token(token: str) -> bool:
    core = token.rstrip(".,;:!?)]}\"'”’").lstrip("([{\"'“‘")
    if not core:
        return False
    return bool(_PATH_TOKEN_RE.fullmatch(core))


def _drop_paths(text: str) -> str:
    kept = []
    for tok in text.split(" "):
        if not tok:
            continue
        if _is_path_token(tok):
            # Keep the sentence punctuation the path carried, so "edited
            # server.py." still ends its sentence.
            tail = tok[len(tok.rstrip(".,;:!?")) :]
            if tail and kept:
                kept[-1] = kept[-1] + tail
            continue
        kept.append(tok)
    return " ".join(kept)


def _sentences(text: str) -> list:
    out = []
    for piece in _SENTENCE_SPLIT_RE.split(text):
        piece = piece.strip()
        if not piece or not re.search(r"[A-Za-z]{2,}", piece):
            continue
        out.append(piece)
    return out


def digest(text: str, max_words: int = DIGEST_MAX_WORDS) -> str:
    """A spoken digest of a reply that has no ``Say:`` line.

    Drops fenced code, inline code, URLs, paths (tokens with a ``/`` or a
    file extension), markdown markers, tables and headings; collapses
    whitespace; keeps the first sentence plus every sentence ending in ``?``
    (at most :data:`DIGEST_MAX_SENTENCES`, about ``max_words`` words); ends
    with a period. Empty in → ``""``."""
    if not text:
        return ""
    s = str(text).replace("\r\n", "\n").replace("\r", "\n")
    s = _FENCED_BLOCK_RE.sub(r"\1 ", s)
    s = _UNCLOSED_FENCE_RE.sub(r"\1 ", s)
    lines = [
        ln
        for ln in s.split("\n")
        if not _HEADING_LINE_RE.match(ln) and not _TABLE_LINE_RE.match(ln)
    ]
    s = "\n".join(lines)
    s = _INLINE_CODE_RE.sub(" ", s)
    s = _URL_RE.sub(" ", s)
    s = strip_markdown(s)
    s = _drop_paths(s)
    s = _WS_RE.sub(" ", s).strip()
    # Drop a Say: marker left in a digest's path (the line itself is handled
    # by extract_say; here it would be read aloud as the word "say").
    s = re.sub(r"(?i)(?:^|(?<=\s))say\s*[:：]\s*", "", s)
    sentences = _sentences(s)
    if not sentences:
        return ""
    picked = [sentences[0]]
    for sent in sentences[1:]:
        if len(picked) >= DIGEST_MAX_SENTENCES:
            break
        if sent.endswith("?"):
            picked.append(sent)
    words = " ".join(picked).split(" ")
    cut = len(words) > max_words
    out = " ".join(words[:max_words]).strip()
    if cut:
        out = out.rstrip(" ,;:—–-") + "…"
    if out and out[-1] not in ".!?…":
        out += "."
    return _cap(out)


# --- the reply file (one cached reader) ---------------------------------------- #
_REPLY_CACHE: Dict[str, Tuple[Tuple[int, int], Optional[Tuple[float, str]]]] = {}
_REPLY_CACHE_LOCK = threading.Lock()
_REPLY_CACHE_MAX = 512


def read_reply(session_name: str) -> Optional[Tuple[float, str]]:
    """``(ts, text)`` from ``<marker dir>/<session>.reply.json`` — the
    ``last_assistant_message`` the Stop hook filed — or None when there is
    none (or it does not parse). Cached per session on the file's (mtime,
    size), so a poll costs one stat. Never raises."""
    if not session_name:
        return None
    try:
        path = _am.reply_path(session_name)
        st = os.stat(path)
    except Exception:  # noqa: BLE001 — absent (or guarded) = no reply
        return None
    # os.replace gives every reply a new inode: two equal-length replies in
    # one mtime tick (a coarse-mtime mount) still miss the cache.
    key = (st.st_mtime_ns, st.st_size, st.st_ino)
    with _REPLY_CACHE_LOCK:
        hit = _REPLY_CACHE.get(session_name)
        if hit is not None and hit[0] == key:
            return hit[1]
    value: Optional[Tuple[float, str]] = None
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(obj, dict):
            value = (float(obj.get("ts") or 0.0), str(obj.get("text") or ""))
    except Exception:  # noqa: BLE001 — torn/garbled = no reply
        value = None
    with _REPLY_CACHE_LOCK:
        if len(_REPLY_CACHE) >= _REPLY_CACHE_MAX:
            _REPLY_CACHE.clear()
        _REPLY_CACHE[session_name] = (key, value)
    return value


def forget(session_name: str) -> None:
    """Drop ``session_name``'s reply file and cache entry (session removal):
    a re-opened or namesake session must not speak a dead one's last words.
    Never raises."""
    with _REPLY_CACHE_LOCK:
        _REPLY_CACHE.pop(session_name, None)
    try:
        _am.reply_path(session_name).unlink()
    except Exception:  # noqa: BLE001 — absent, or guarded: nothing to drop
        pass


def _none(ts: Optional[float] = None) -> dict:
    return {"say": None, "source": "none", "has_question": False, "ts": ts}


def spoken_for(title: str, inst) -> dict:
    """The ``spoken`` record for session ``title``: ``{"say", "source",
    "has_question", "ts"}``.

    ``say`` is the reply file's ``Say:`` line (``source: "hook"``); failing
    that, a :func:`digest` of the reply text, else of the provider's newest
    assistant text (``source: "digest"``; the reply text first because the
    hook filed exactly what was said and it costs no transcript read); else
    ``source: "none"``. ``ts`` is the reply file's stamp when the words came
    from it, else None. Cheap on the poll path (a stat; the provider's reader
    is mtime-cached) and never raises."""
    try:
        from backend.session import tmux

        name = tmux.to_mindflock_tmux_name(title or "")
    except Exception:  # noqa: BLE001
        return _none()
    reply = read_reply(name)
    ts = reply[0] if reply else None
    if reply and reply[1]:
        say = extract_say(reply[1])
        if say:
            return {
                "say": say,
                "source": "hook",
                "has_question": has_question(say),
                "ts": ts,
            }
    text = reply[1] if reply and reply[1].strip() else ""
    from_reply = bool(text)
    if not text:
        text = _last_assistant_text(inst, name) or ""
        say = extract_say(text) if text else None
        if say:
            # The model wrote its line; only the stamp is unknown.
            return {
                "say": say,
                "source": "transcript",
                "has_question": has_question(say),
                "ts": None,
            }
    if not text.strip():
        return _none(ts)
    said = digest(text)
    if not said:
        return _none(ts)
    return {
        "say": said,
        "source": "digest",
        "has_question": has_question(said),
        "ts": ts if from_reply else None,
    }


def _last_assistant_text(inst, session_name: str) -> Optional[str]:
    """The provider's newest assistant text for this window, or None."""
    if inst is None:
        return None
    try:
        from backend import providers

        wt = ""
        try:
            wt = inst.GetWorktreePath() or ""
        except Exception:  # noqa: BLE001
            wt = ""
        prov = providers.resolve(getattr(inst, "Program", "") or "")
        text = prov.last_assistant_text(session_name, wt)
        return text if isinstance(text, str) else None
    except Exception:  # noqa: BLE001 — no transcript reads as no reply
        return None
