"""Shaping text for a voice: what every line MindFlock says out loud goes
through right before it reaches an engine, and NEVER what a caption shows.

The engines (piper's espeak-ng, the OS voices, a neural host like F5-TTS,
the clients' ``speechSynthesis``) take plain text and no SSML, so the one
portable fix for a word they get wrong is to RESPELL it in plain ASCII:
"everything goes live" → "everything goes lyve" (the adjective, /laɪv/),
"I read the file" → "I red the file", "JSON" → "jayson", "SQL" → "sequel".
:func:`shape` does that in one pass of regexes and tables — pure, fast
(well under a millisecond for a Say line), idempotent (shaping shaped text
changes nothing), bounded (:data:`MAX_OUT` characters out). The rule
families, in the order they run:

1. The person's own pronunciations (``voice.pronounce``: ``[{say, as}]``,
   whole word or phrase, case-insensitive, longest first) — applied FIRST
   and protected from every later rule, so they win.
2. Markup: code fences and backticks, emphasis, headings, list markers,
   quotes, HTML tags and emoji are dropped; a Markdown link keeps its text;
   a URL becomes "a link".
3. Paths and files: ``backend/web/core/speech.py`` → "backend, web, core,
   speech dot pie" (segments, the extension through :data:`TABLES`
   ``ext``; ``~`` is "home", a ``:123`` tail is "line 123"); a bare
   ``README.md`` → "README dot M D"; a long hex SHA → its first seven
   characters spelled out.
4. Numbers engineers say differently: ``v0.10.2`` → "version 0 point 10
   point 2" (else an engine says "one zero"), ``PR #275`` → "P R 275",
   ``#275`` → "number 275", ``1m23s`` → "1 minute 23 seconds", ``350ms`` →
   "350 milliseconds", ``12px`` → "12 pixels", ``3 MB`` → "3 megabytes",
   ``1,234`` → "1234".
5. Identifiers: snake_case, kebab-case, camelCase and letter-digit joins
   split into words ("askMicTap" → "ask mic tap", "sitecheck-bot7" →
   "sitecheck bot 7"); each part is looked up in the tech table first
   ("db_url" → "dee bee url"). A date-like all-digit token is left alone.
6. Tech vocabulary (:data:`TABLES` ``tech``): whole words read the way
   engineers say them — "sequel", "jayson", "engine x", "tee mux", "you
   vee", "pie test", "kubernetes", the acronyms as letters ("A P I").
7. Heteronyms by context (:data:`TABLES` ``heteronyms``): ``live`` read as
   the adjective unless a pronoun or "to" precedes it or "in/with/here…"
   follows ("goes live", "live in 3" respell; "we live in", "live with"
   keep); ``read`` as the past tense after I/we/you/they/have/has/had/
   already/just…; the verbs ``record``, ``present``, ``use``, ``separate``,
   ``estimate``, ``wind``, ``tear``, ``alternate``, ``permit`` and the noun
   ``resume`` by their neighbours. The rest of the classic list (lead,
   close, project, object, produce, content, invalid, duplicate, minute,
   wound, bow, refuse, export, import, update, upgrade) is deliberately
   left alone: the engines already read them right, or the noun/verb
   stress is close enough in software talk — the person's own table
   covers the rest. Every respelling was checked on piper's phonemizer and
   on an F5-TTS voice (tests/unit/data/spoken_text.json pins the cases).

:data:`TABLES` is the ONE rule table: the same JSON literal sits in
``frontend/src/lib/spokenText.ts`` (``shapeForSpeech``) and
``backend/web/static/spoken-text.js`` (``window.MindFlockSpokenText.shape``)
between ``@@tables`` / ``@@end`` markers, and ``tests/unit/test_spoken_
text.py`` reads all three and refuses a drift; the fixture runs through
all three implementations.

:func:`normalize_pronounce` is the saved form of ``voice.pronounce``:
at most :data:`PRONOUNCE_MAX` rows of ``{say, as}``, each side ≤
:data:`PRONOUNCE_MAX_CHARS`, ``say`` lower case, duplicates dropped.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional

__all__ = [
    "MAX_IN",
    "MAX_OUT",
    "PRONOUNCE_MAX",
    "PRONOUNCE_MAX_CHARS",
    "TABLES",
    "normalize_pronounce",
    "shape",
]

#: The longest input shaped; the rest is cut first (a Say line is ≤ 320
#: characters, ``/api/voice/speak`` takes 1200).
MAX_IN = 4000
#: The longest output (a respelling adds a few characters per word).
MAX_OUT = 2000
#: ``voice.pronounce`` bounds: rows, and characters per side.
PRONOUNCE_MAX = 40
PRONOUNCE_MAX_CHARS = 40

# The rule tables. A JSON literal (double quotes, no trailing commas, no
# comments inside) — the TypeScript and the static JavaScript carry the same
# text, and the test diffs them. Keys are lower case; a value is spoken
# as written (the engines read lower case fine). Respellings are
# hyphen-free so the identifier splitter leaves them alone on a second pass.
# fmt: off
# @@tables
TABLES = {
  "tech": {
    "sql": "sequel", "json": "jayson", "nginx": "engine x", "tmux": "tee mux",
    "uv": "you vee", "npm": "N P M", "npx": "N P X", "api": "A P I", "apis": "A P I's",
    "cli": "C L I", "url": "U R L", "urls": "U R L's", "ssh": "S S H", "ci": "C I",
    "pr": "P R", "prs": "P R's", "id": "I D", "ids": "I D's", "uuid": "U U I D",
    "eta": "E T A", "fyi": "F Y I", "lgtm": "looks good to me", "pytest": "pie test",
    "regex": "rej ex", "regexes": "rej exes", "github": "git hub", "sudo": "soo doo",
    "chmod": "C H mod", "chown": "C H own", "venv": "vee env", "k8s": "kubernetes",
    "i18n": "i 18 n", "a11y": "a 11 y", "e2e": "end to end", "oauth": "O auth",
    "websocket": "web socket", "websockets": "web sockets", "enum": "ee num",
    "enums": "ee nums", "src": "source", "kwargs": "kay wargs", "tmp": "temp",
    "usr": "user", "msg": "message", "msgs": "messages", "pkg": "package", "pkgs": "packages",
    "ctx": "context", "db": "dee bee", "dbs": "dee bees", "zsh": "zee shell", "vitest": "vee test",
    "tsc": "T S C", "eslint": "E S lint", "stdout": "standard out", "stderr": "standard error",
    "stdin": "standard in", "localhost": "local host", "e.g.": "for example",
    "i.e.": "that is", "etc.": "etcetera", "etc": "etcetera", "vs": "versus", "vs.": "versus",
    "ttl": "T T L", "tls": "T L S", "ssl": "S S L", "http": "H T T P", "https": "H T T P S",
    "html": "H T M L", "css": "C S S", "js": "J S", "ts": "T S", "tsx": "T S X", "jsx": "J S X",
    "yaml": "yammel", "yml": "yammel", "toml": "tommel", "csv": "C S V", "ui": "U I", "ux": "U X",
    "cpu": "C P U", "gpu": "G P U", "ram": "ram", "os": "O S", "io": "I O", "sdk": "S D K",
    "cdn": "C D N", "dns": "D N S", "ip": "I P", "tcp": "T C P", "udp": "U D P", "pty": "P T Y",
    "mcp": "M C P", "llm": "L L M", "llms": "L L M's", "stt": "S T T", "tts": "T T S",
    "wsl": "W S L", "vm": "V M", "vms": "V M's", "qa": "Q A", "ok": "okay", "readme": "read me",
    "dto": "D T O", "orm": "O R M", "jwt": "J W T", "sha": "shah", "utf": "U T F", "ascii": "askey",
    "iso": "I S O", "lib": "lib", "libs": "libs", "cfg": "config", "ptt": "push to talk"
  },
  "ext": {
    "py": "pie", "ts": "T S", "tsx": "T S X", "js": "J S", "jsx": "J S X", "mjs": "M J S",
    "cjs": "C J S", "md": "M D", "json": "jayson", "toml": "tommel", "yml": "yammel",
    "yaml": "yammel", "css": "C S S", "html": "H T M L", "txt": "text", "sh": "S H",
    "zsh": "zee shell", "cfg": "config", "ini": "I N I", "csv": "C S V", "svg": "S V G",
    "png": "P N G", "jpg": "J P G", "jpeg": "J P G", "gif": "gif", "pdf": "P D F", "env": "env",
    "log": "log", "lock": "lock", "sql": "sequel", "xml": "X M L", "rs": "R S", "go": "go",
    "rb": "R B", "java": "java", "kt": "K T", "swift": "swift", "c": "C", "h": "H", "cpp": "C plus plus",
    "wav": "wave", "mp3": "M P 3", "zip": "zip", "tar": "tar", "gz": "G Z", "exe": "E X E",
    "dmg": "D M G", "app": "app", "plist": "P list", "ipynb": "notebook", "pyc": "pie C",
    "egg": "egg", "whl": "wheel", "bak": "backup", "tmp": "temp"
  },
  "units": {
    "ms": ["millisecond", "milliseconds"], "px": ["pixel", "pixels"],
    "kb": ["kilobyte", "kilobytes"], "mb": ["megabyte", "megabytes"],
    "gb": ["gigabyte", "gigabytes"], "tb": ["terabyte", "terabytes"],
    "kib": ["kibibyte", "kibibytes"], "mib": ["mebibyte", "mebibytes"],
    "gib": ["gibibyte", "gibibytes"], "fps": ["frame per second", "frames per second"]
  },
  "durations": {
    "h": ["hour", "hours"], "hr": ["hour", "hours"], "hrs": ["hour", "hours"],
    "m": ["minute", "minutes"], "min": ["minute", "minutes"], "mins": ["minute", "minutes"],
    "s": ["second", "seconds"], "sec": ["second", "seconds"], "secs": ["second", "seconds"]
  },
  "heteronyms": [
    {
      "word": "live", "as": "lyve", "default": "respell",
      "respell_prev": ["go", "goes", "went", "going", "gone", "is", "are", "was", "were", "be",
        "been", "being", "it's", "its", "now", "still", "already", "stay", "stays", "stayed",
        "remain", "remains", "became", "become", "becomes", "got", "get", "gets", "back",
        "fully", "finally", "officially", "currently"],
      "keep_prev": ["i", "we", "you", "they", "people", "who", "to", "can't", "cannot",
        "couldn't", "won't", "wouldn't", "don't", "didn't", "doesn't", "must", "may", "might",
        "will", "would", "could", "should", "shall", "i'll", "we'll", "you'll", "they'll",
        "i'd", "we'd", "you'd", "they'd", "users", "developers", "teams", "long", "let", "lets"],
      "keep_next": ["with", "here", "there", "alone", "together", "near", "among", "off",
        "like", "without", "within", "nearby", "abroad", "downtown", "upstairs", "downstairs",
        "forever", "happily", "comfortably", "longer", "anywhere", "somewhere", "elsewhere"],
      "respell_next_re": "in (?:\\d|one|two|three|four|five|ten|a |an |about |under |less )",
      "keep_next_re": "in (?![\\d]|one|two|three|four|five|ten|a |an |about |under |less )"
    },
    {
      "word": "read", "as": "red", "default": "keep",
      "respell_prev": ["i", "we", "you", "they", "he", "she", "it", "who", "had", "have", "has",
        "having", "already", "just", "once", "been", "was", "were", "got", "haven't", "hasn't",
        "hadn't", "never", "i've", "we've", "you've", "they've", "he's", "she's", "it's",
        "all", "both", "everyone", "everybody", "somebody", "someone", "nobody"]
    },
    {
      "word": "record", "as": "recorde", "default": "keep",
      "respell_prev": ["to", "will", "can", "could", "should", "would", "must", "may", "might",
        "please", "let's", "lets", "don't", "doesn't", "didn't", "won't", "can't", "cannot",
        "and", "or", "then", "we", "i", "you", "they", "also", "now", "just", "always", "never",
        "still", "not", "shall"]
    },
    {
      "word": "present", "as": "prezent", "default": "keep",
      "respell_prev": ["to", "will", "can", "could", "should", "would", "must", "may", "might",
        "please", "let's", "lets", "don't", "doesn't", "didn't", "won't", "can't", "cannot",
        "we", "i", "you", "they", "also", "now", "then", "and", "or", "shall"],
      "respell_next": ["it", "them", "this", "that", "these", "those", "the", "a", "an", "your",
        "our", "my", "their", "its", "results", "data", "options", "findings", "each", "every",
        "both", "all", "some", "two", "three"]
    },
    {
      "word": "use", "as": "yooz", "default": "respell",
      "keep_prev": ["the", "a", "an", "its", "in", "of", "no", "any", "this", "that", "for",
        "their", "our", "your", "my", "each", "every", "general", "heavy", "memory", "cpu",
        "disk", "token", "tokens", "api", "resource", "resources", "intended", "actual",
        "single", "one", "multiple", "make", "makes", "made", "making", "little", "much", "more",
        "less", "some", "into", "under", "without", "daily", "personal", "internal", "external",
        "fair", "common", "wide", "widespread", "first", "second", "last", "future", "later", "later"],
      "keep_next": ["of", "case", "cases", "by"]
    },
    {
      "word": "separate", "as": "seperate", "default": "keep",
      "respell_prev": ["to", "will", "can", "could", "should", "would", "must", "please",
        "let's", "lets", "we", "i", "you", "they", "and", "then", "also", "don't", "doesn't",
        "didn't", "won't", "can't", "cannot", "shall"],
      "respell_next": ["the", "them", "it", "these", "those", "this", "that", "out", "your",
        "our", "my", "each", "both", "all"]
    },
    {
      "word": "estimate", "as": "estimayt", "default": "keep",
      "respell_prev": ["to", "will", "can", "could", "should", "would", "must", "please",
        "let's", "lets", "we", "i", "you", "they", "and", "then", "also", "don't", "doesn't",
        "didn't", "won't", "can't", "cannot", "shall"],
      "respell_next": ["the", "them", "it", "these", "those", "this", "that", "how", "when",
        "what", "your", "our", "my", "each", "both", "all"]
    },
    {
      "word": "wind", "as": "wined", "default": "keep",
      "respell_next": ["down", "up", "back", "through", "around", "forward"]
    },
    {
      "word": "tear", "as": "tare", "default": "respell",
      "keep_prev": ["a", "the", "this", "that", "one", "every", "no", "any", "another", "single"]
    },
    {
      "word": "resume", "as": "rezoomay", "default": "keep",
      "respell_prev": ["my", "your", "his", "her", "their", "our", "attached", "updated"],
      "keep_next": ["the", "a", "an", "it", "them", "this", "that", "where", "from", "at",
        "on", "in", "after", "work", "working", "session", "sessions", "run", "runs", "playback",
        "listening", "recording", "feature", "button", "command", "option", "later", "now",
        "when", "once", "and", "or", "with", "without", "speaking", "reading"]
    },
    {
      "word": "alternate", "as": "alternayt", "default": "keep",
      "respell_prev": ["to", "will", "can", "could", "should", "would", "must", "let's", "lets",
        "we", "i", "you", "they", "and", "then", "also", "don't", "doesn't", "didn't", "shall"],
      "respell_next": ["between", "them", "it", "these", "those"]
    },
    {
      "word": "permit", "as": "permitt", "default": "keep",
      "respell_prev": ["to", "will", "can", "could", "should", "would", "must", "may", "might",
        "please", "let's", "lets", "don't", "doesn't", "didn't", "won't", "can't", "cannot",
        "we", "i", "you", "they", "and", "then", "not", "also", "shall"],
      "respell_next": ["it", "them", "this", "that", "the", "a", "an", "us", "me", "you", "any"]
    }
  ]
}
# @@end
# fmt: on

# --- the private-use placeholders a protected span hides behind -------------- #
# A protected span (an override's replacement, a tech respelling) is swapped
# for U+E000 + its index in U+E010..U+E019 digits + U+E001 — characters no
# rule's ``[A-Za-z0-9]`` class touches — and put back at the end.
_HOLD_OPEN = ""
_HOLD_CLOSE = ""
_HOLD_DIGITS = "".join(chr(0xE010 + i) for i in range(10))
_HOLD_RE = re.compile("([-]+)")


class _Shaper:
    """One call's state: the protected spans."""

    __slots__ = ("held",)

    def __init__(self) -> None:
        self.held: List[str] = []

    def hold(self, text: str) -> str:
        self.held.append(text)
        idx = str(len(self.held) - 1)
        return _HOLD_OPEN + "".join(_HOLD_DIGITS[int(c)] for c in idx) + _HOLD_CLOSE

    def release(self, text: str) -> str:
        def back(m: "re.Match[str]") -> str:
            idx = int("".join(str(_HOLD_DIGITS.index(c)) for c in m.group(1)))
            return self.held[idx] if idx < len(self.held) else ""

        # A held path holds tech words of its own: a few rounds, bounded.
        for _ in range(4):
            if _HOLD_OPEN not in text:
                break
            text = _HOLD_RE.sub(back, text)
        return text


# --- helpers ------------------------------------------------------------------ #
_WORDISH = "[A-Za-z0-9]"


def _bounded(key: str) -> str:
    """``key`` (escaped) as a whole word: not glued to a letter or digit on
    either side. Group 1 is the character before (kept by the caller), so no
    lookbehind is needed — the static twin runs on older Safaris too."""
    return "(^|[^A-Za-z0-9])(%s)(?![A-Za-z0-9])" % re.escape(key)


def _match_case(original: str, spoken: str) -> str:
    """``spoken`` capitalised when ``original`` was ("Live" → "Lyve"); a
    respelling of an all-caps word stays as written."""
    if original[:1].isupper() and (len(original) == 1 or not original[1:].isupper()):
        return spoken[:1].upper() + spoken[1:]
    return spoken


_PREV_RE = re.compile(r"([A-Za-z0-9']+)[ \t-]*$")
_NEXT_RE = re.compile(r"^[ \t-]*([A-Za-z0-9']+)")


def _prev_word(text: str, start: int) -> str:
    m = _PREV_RE.search(text[max(0, start - 48) : start])
    return m.group(1).lower() if m else ""


def _next_word(text: str, end: int) -> str:
    m = _NEXT_RE.search(text[end : end + 48])
    return m.group(1).lower() if m else ""


# --- 2. markup ----------------------------------------------------------------- #
_FENCE = re.compile(r"^[ \t]*(```|~~~)[^\n]*$", re.M)
_INLINE_CODE = re.compile(r"`([^`\n]*)`")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_URL = re.compile(
    r"(^|[^A-Za-z0-9])((?:https?|ftp|wss?)://[^\s)\]>\"']+|www\.[^\s)\]>\"']+)"
)
_HTML = re.compile(r"</?[A-Za-z][^<>]*>")
_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+", re.M)
_LIST = re.compile(r"^[ \t]*(?:[-*+]|\d{1,3}[.)])[ \t]+", re.M)
_QUOTE = re.compile(r"^[ \t]*>[ \t]?", re.M)
_STRONG = re.compile(r"(^|[\s(])(\*\*|__)(\S(?:.*?\S)?)\2(?=[\s).,!?;:]|$)")
_EM = re.compile(r"(^|[\s(])([*_])([^\s*_](?:.*?[^\s*_])?)\2(?=[\s).,!?;:]|$)")
_STRIKE = re.compile(r"~~(\S(?:.*?\S)?)~~")
_EMOJI = re.compile(
    "[\U0001f000-\U0001faff\U0001f900-\U0001f9ff☀-➿⬀-⯿️‍"
    "←-⇿⌀-⏿■-◿\U0001f1e6-\U0001f1ff]"
)


def _strip_markup(text: str) -> str:
    text = _FENCE.sub("", text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _IMAGE.sub(r"\1", text)
    text = _LINK.sub(r"\1", text)
    text = _URL.sub(r"\1a link", text)
    text = _HTML.sub("", text)
    text = _HEADING.sub("", text)
    text = _LIST.sub("", text)
    text = _QUOTE.sub("", text)
    text = _STRONG.sub(r"\1\3", text)
    text = _EM.sub(r"\1\3", text)
    text = _STRIKE.sub(r"\1", text)
    text = _EMOJI.sub("", text)
    return text


# --- 3. paths, files, SHAs ----------------------------------------------------- #
_SEG = r"[A-Za-z0-9_.~@+-]+"
_PATH = re.compile(
    r"(^|[^A-Za-z0-9/.~])((?:~|\.{1,2})?/%s(?:/%s)*/?|%s(?:/%s)+/?)(?::(\d+))?(?![A-Za-z0-9/])"
    % (_SEG, _SEG, _SEG, _SEG)
)
_FILE = re.compile(
    r"(^|[^A-Za-z0-9/.])([A-Za-z0-9_][A-Za-z0-9_-]*\.(%s))(?::(\d+))?(?![A-Za-z0-9.])"
    % "|".join(sorted(TABLES["ext"], key=len, reverse=True))
)
_SHA = re.compile(r"(^|[^A-Za-z0-9])([0-9a-f]{7,40})(?![A-Za-z0-9])")
_HAS_DIGIT = re.compile(r"\d")
_HAS_ALPHA = re.compile(r"[a-f]")


def _spoken_segment(seg: str, last: bool) -> str:
    if seg == "~":
        return "home"
    if seg == "..":
        return "dot dot"
    if last and "." in seg.lstrip("."):
        stem, _, ext = seg.rpartition(".")
        spoken_ext = TABLES["ext"].get(ext.lower())
        if spoken_ext is not None:
            return "%s dot %s" % (_spoken_name(stem), spoken_ext)
    return _spoken_name(seg)


def _spoken_name(name: str) -> str:
    """A path segment's words: a hidden name's leading dot said, joiners
    spaced."""
    hidden = name.startswith(".")
    words = name.lstrip(".").replace("_", " ").replace("-", " ").strip()
    return ("dot " + words).strip() if hidden else words


def _path_words(path: str, line: Optional[str], s: "_Shaper") -> str:
    segs = [seg for seg in path.split("/") if seg and seg != "."]
    words = ", ".join(
        _spoken_segment(seg, i == len(segs) - 1) for i, seg in enumerate(segs)
    )
    if line:
        words += " line " + line
    # Said the engineers' way inside too ("web-api.md" → "web A P I dot M
    # D"), so a second pass over the released text changes nothing.
    return _tech(words, s)


def _is_path(token: str) -> bool:
    """A real path, not "and/or" or "n/a": starts like one, has two slashes,
    or ends in a known extension."""
    if token.startswith(("/", "~/", "./", "../")):
        return True
    if token.count("/") >= 2:
        return True
    tail = token.rstrip("/").rsplit("/", 1)[-1]
    ext = tail.rpartition(".")[2].lower() if "." in tail else ""
    return bool(ext) and ext in TABLES["ext"]


def _paths(text: str, s: _Shaper) -> str:
    def path(m: "re.Match[str]") -> str:
        token, tail = m.group(2), ""
        while token.endswith((".", ",")):  # the sentence's own punctuation
            token, tail = token[:-1], token[-1] + tail
        if not _is_path(token) or not re.search("[A-Za-z]", token):
            return m.group(0)
        return m.group(1) + s.hold(_path_words(token, m.group(3), s)) + tail

    text = _PATH.sub(path, text)

    def file(m: "re.Match[str]") -> str:
        stem, _, ext = m.group(2).rpartition(".")
        spoken = "%s dot %s" % (_spoken_name(stem), TABLES["ext"][ext.lower()])
        if m.group(4):
            spoken += " line " + m.group(4)
        return m.group(1) + s.hold(_tech(spoken, s))

    text = _FILE.sub(file, text)

    def sha(m: "re.Match[str]") -> str:
        h = m.group(2)
        if not (_HAS_DIGIT.search(h) and _HAS_ALPHA.search(h)):
            return m.group(0)
        return m.group(1) + s.hold(" ".join(h[:7]))

    return _SHA.sub(sha, text)


# --- 4. numbers ---------------------------------------------------------------- #
_VERSION = re.compile(r"(^|[^A-Za-z0-9])[vV](\d+(?:\.\d+)+)(?![A-Za-z0-9]|\.\d)")
_DOTTED = re.compile(r"(^|[^A-Za-z0-9.])(\d+\.\d+(?:\.\d+)+)(?![A-Za-z0-9]|\.\d)")
_PR = re.compile(r"(^|[^A-Za-z0-9])(?:PR|pr|Pr)[ \t]?#?(\d+)(?![A-Za-z0-9])")
_ISSUE = re.compile(
    r"(^|[^A-Za-z0-9])(issue|Issue|ticket|Ticket)[ \t]?#(\d+)(?![A-Za-z0-9])"
)
_HASH = re.compile(r"(^|[^A-Za-z0-9&#])#(\d+)(?![A-Za-z0-9])")
_DURATION = re.compile(
    r"(^|[^A-Za-z0-9.])(\d+)(h|hr|hrs)(?:[ \t]?(\d+)(m|min|mins))?(?:[ \t]?(\d+)(s|sec|secs))?"
    r"(?![A-Za-z0-9])|(^|[^A-Za-z0-9.])(\d+)(m|min|mins)[ \t]?(\d+)(s|sec|secs)(?![A-Za-z0-9])"
)
_DURATION_ONE = re.compile(
    r"(^|[^A-Za-z0-9.])(\d+(?:\.\d+)?)(ms|h|hr|hrs|m|min|mins|s|sec|secs)(?![A-Za-z0-9])"
)
_UNIT = re.compile(
    r"(^|[^A-Za-z0-9.])(\d+(?:\.\d+)?)[ \t]?(%s)(?![A-Za-z0-9])"
    % "|".join(sorted(TABLES["units"], key=len, reverse=True)),
    re.I,
)
_THOUSANDS = re.compile(r"(^|[^A-Za-z0-9.,])(\d{1,3}(?:,\d{3})+)(?![A-Za-z0-9,])")


def _plural(n: str, pair: List[str]) -> str:
    return pair[0] if n in ("1", "1.0") else pair[1]


def _numbers(text: str) -> str:
    text = _VERSION.sub(
        lambda m: m.group(1) + "version " + " point ".join(m.group(2).split(".")), text
    )
    text = _DOTTED.sub(
        lambda m: m.group(1) + " point ".join(m.group(2).split(".")), text
    )
    text = _PR.sub(lambda m: m.group(1) + "P R " + m.group(2), text)
    text = _ISSUE.sub(lambda m: m.group(1) + m.group(2) + " " + m.group(3), text)
    text = _HASH.sub(lambda m: m.group(1) + "number " + m.group(2), text)

    def duration(m: "re.Match[str]") -> str:
        d = TABLES["durations"]
        if m.group(2) is not None:
            out = [m.group(1), m.group(2), " ", _plural(m.group(2), d[m.group(3)])]
            if m.group(4):
                out += [" ", m.group(4), " ", _plural(m.group(4), d[m.group(5)])]
            if m.group(6):
                out += [" ", m.group(6), " ", _plural(m.group(6), d[m.group(7)])]
            return "".join(out)
        return "%s%s %s %s %s" % (
            m.group(8),
            m.group(9),
            _plural(m.group(9), d[m.group(10)]),
            m.group(11),
            _plural(m.group(11), d[m.group(12)]),
        )

    text = _DURATION.sub(duration, text)

    def one(m: "re.Match[str]") -> str:
        unit = m.group(3)
        if unit == "ms":
            pair = TABLES["units"]["ms"]
        else:
            pair = TABLES["durations"][unit]
        return "%s%s %s" % (m.group(1), m.group(2), _plural(m.group(2), pair))

    text = _DURATION_ONE.sub(one, text)
    text = _UNIT.sub(
        lambda m: "%s%s %s"
        % (
            m.group(1),
            m.group(2),
            _plural(m.group(2), TABLES["units"][m.group(3).lower()]),
        ),
        text,
    )
    return text


# --- 5. identifiers ------------------------------------------------------------ #
_IDENT = re.compile(
    r"(^|[^A-Za-z0-9_'-])([A-Za-z0-9]+(?:[_-][A-Za-z0-9]+)*)(?![A-Za-z0-9_])"
)
_CAMEL_LOWER_UPPER = re.compile(r"([a-z0-9])([A-Z])")
_CAMEL_ACRONYM = re.compile(r"([A-Z]+)([A-Z][a-z])")
_LETTER_DIGIT = re.compile(r"([A-Za-z])(\d)")
_ALL_DIGITS = re.compile(r"^[\d_-]+$")


def _split_part(part: str) -> str:
    """ "askMicTap" → "ask mic tap", "HTMLElement" → "HTML element", "bot7"
    → "bot 7"; a part the camel split touched is lowered (an engine reads
    "Mic" and "mic" alike, a caption never sees it), an all-caps run stays
    for the tech table."""
    split = _CAMEL_ACRONYM.sub(r"\1 \2", part)
    split = _CAMEL_LOWER_UPPER.sub(r"\1 \2", split)
    if split != part:
        split = " ".join(w if w.isupper() else w.lower() for w in split.split(" "))
    return _LETTER_DIGIT.sub(r"\1 \2", split)


def _identifiers(text: str, s: _Shaper) -> str:
    tech = TABLES["tech"]

    def ident(m: "re.Match[str]") -> str:
        token = m.group(2)
        low = token.lower()
        if low in tech:
            return m.group(1) + s.hold(_match_case(token, tech[low]))
        if _ALL_DIGITS.match(token):
            return m.group(0)
        parts = re.split(r"[_-]", token)
        if len(parts) == 1 and _split_part(token) == token:
            return m.group(0)
        spoken: List[str] = []
        for p in parts:
            if not p:
                continue
            if p.lower() in tech:
                spoken.append(s.hold(_match_case(p, tech[p.lower()])))
            else:
                spoken.append(_split_part(p))
        return m.group(1) + " ".join(spoken)

    return _IDENT.sub(ident, text)


# --- 6. tech words and 1. overrides ------------------------------------------- #
def _table_regex(keys: List[str]) -> "re.Pattern[str]":
    alts = "|".join(re.escape(k) for k in sorted(keys, key=len, reverse=True))
    return re.compile("(^|[^A-Za-z0-9])(%s)(?![A-Za-z0-9])" % alts, re.I)


_TECH_RE = _table_regex(list(TABLES["tech"]))


def _tech(text: str, s: _Shaper) -> str:
    tech = TABLES["tech"]

    def word(m: "re.Match[str]") -> str:
        token = m.group(2)
        spoken = tech.get(token.lower())
        if spoken is None:
            return m.group(0)
        return m.group(1) + s.hold(_match_case(token, spoken))

    return _TECH_RE.sub(word, text)


def _overrides(text: str, rules: List[Dict[str, str]], s: _Shaper) -> str:
    if not rules:
        return text
    table = {r["say"]: r["as"] for r in rules}
    alts = "|".join(
        re.escape(k).replace("\\ ", "[ \\t]+")
        for k in sorted(table, key=len, reverse=True)
    )
    pattern = re.compile("(^|[^A-Za-z0-9])(%s)(?![A-Za-z0-9])" % alts, re.I)

    def word(m: "re.Match[str]") -> str:
        key = re.sub(r"[ \t]+", " ", m.group(2).lower())
        spoken = table.get(key)
        if spoken is None:
            return m.group(0)
        return m.group(1) + s.hold(_match_case(m.group(2), spoken))

    return pattern.sub(word, text)


# --- 7. heteronyms ------------------------------------------------------------- #
def _compile_heteronym(rule: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "re": re.compile(_bounded(rule["word"]), re.I),
        "as": rule["as"],
        "respell": rule["default"] == "respell",
        "keep_prev": frozenset(rule.get("keep_prev", ())),
        "keep_next": frozenset(rule.get("keep_next", ())),
        "respell_prev": frozenset(rule.get("respell_prev", ())),
        "respell_next": frozenset(rule.get("respell_next", ())),
        "respell_next_re": None,
        "keep_next_re": None,
    }
    if rule.get("respell_next_re"):
        out["respell_next_re"] = re.compile("^[ \t]*" + rule["respell_next_re"], re.I)
    if rule.get("keep_next_re"):
        out["keep_next_re"] = re.compile("^[ \t]*" + rule["keep_next_re"], re.I)
    return out


_HETERONYMS = [_compile_heteronym(r) for r in TABLES["heteronyms"]]


def _decide(rule: Dict[str, Any], prev: str, nxt: str, after: str) -> bool:
    """Whether to respell: the explicit exceptions first (a keep-context on
    a respell-by-default word, a respell-context on a keep-by-default word),
    then the default."""
    if rule["respell"]:
        if prev in rule["keep_prev"]:
            return False
        # "goes live in production": a respell-context before the word beats
        # a keep-context after it.
        if prev in rule["respell_prev"]:
            return True
        if rule["respell_next_re"] is not None and rule["respell_next_re"].search(
            after
        ):
            return True
        if nxt in rule["keep_next"]:
            return False
        if rule["keep_next_re"] is not None and rule["keep_next_re"].search(after):
            return False
        return True
    if prev in rule["keep_prev"] or nxt in rule["keep_next"]:
        return False
    if prev in rule["respell_prev"] or nxt in rule["respell_next"]:
        return True
    if rule["respell_next_re"] is not None and rule["respell_next_re"].search(after):
        return True
    return False


def _heteronyms(text: str) -> str:
    for rule in _HETERONYMS:

        def word(m: "re.Match[str]", rule: Dict[str, Any] = rule) -> str:
            start, end = m.start(2), m.end(2)
            prev = _prev_word(text_now, start)
            nxt = _next_word(text_now, end)
            if _decide(rule, prev, nxt, text_now[end : end + 48]):
                return m.group(1) + _match_case(m.group(2), rule["as"])
            return m.group(0)

        text_now = text
        text = rule["re"].sub(word, text)
    return text


# --- the entry points ----------------------------------------------------------- #
_WS = re.compile(r"[ \t]+")
_NL = re.compile(r"[ \t]*\n[ \t\n]*")


def shape(text: Any, overrides: Any = None) -> str:
    """``text`` as a voice should read it (the module doc lists the rule
    families). ``overrides`` is ``voice.pronounce`` in any state (raw list,
    normalised, or nothing): :func:`normalize_pronounce` makes it safe.
    Pure; never raises on odd input (a non-string is ``""``)."""
    if not isinstance(text, str) or not text:
        return ""
    s = _Shaper()
    text = text[:MAX_IN]
    text = _overrides(text, normalize_pronounce(overrides), s)
    text = _strip_markup(text)
    text = _paths(text, s)
    text = _numbers(text)
    text = _identifiers(text, s)
    text = _tech(text, s)
    text = _heteronyms(text)
    text = _THOUSANDS.sub(lambda m: m.group(1) + m.group(2).replace(",", ""), text)
    text = _NL.sub("\n", _WS.sub(" ", text)).strip()
    text = s.release(text)
    if len(text) > MAX_OUT:
        cut = text.rfind(" ", 0, MAX_OUT)
        text = text[: cut if cut > MAX_OUT // 2 else MAX_OUT].rstrip()
    return text


_SAY_OK = re.compile(r"^[a-z0-9][a-z0-9 '.+#_-]*$")
_AS_BAD = re.compile(r"[\x00-\x1f\x7f<>`]")


def normalize_pronounce(raw: Any) -> List[Dict[str, str]]:
    """The saved form of ``voice.pronounce``: ``[{"say", "as"}]`` — ``say``
    lower case (the match is case-insensitive), whitespace collapsed, letters
    / digits / spaces / ``'.+#_-`` only; ``as`` trimmed, printable, no
    markup characters; both 1–:data:`PRONOUNCE_MAX_CHARS` characters; a row
    whose ``say`` repeats an earlier one is dropped; at most
    :data:`PRONOUNCE_MAX` rows. Anything else (not a list, a row that is not
    a dict) is skipped, so ``[]`` is "nothing of your own"."""
    if not isinstance(raw, (list, tuple)):
        return []
    out: List[Dict[str, str]] = []
    seen = set()
    for row in raw:
        if not isinstance(row, dict):
            continue
        say = row.get("say")
        spoken = row.get("as")
        if not isinstance(say, str) or not isinstance(spoken, str):
            continue
        say = re.sub(r"\s+", " ", say.replace("’", "'")).strip().lower()
        spoken = re.sub(r"\s+", " ", spoken.replace("’", "'")).strip()
        if (
            not say
            or not spoken
            or len(say) > PRONOUNCE_MAX_CHARS
            or len(spoken) > PRONOUNCE_MAX_CHARS
        ):
            continue
        if not _SAY_OK.match(say) or _AS_BAD.search(spoken) or say in seen:
            continue
        seen.add(say)
        out.append({"say": say, "as": spoken})
        if len(out) == PRONOUNCE_MAX:
            break
    return out
