"""Streaming recognition on Moonshine tiny-streaming: the live words and
the control words ~60 ms after they are said, where whisper's own fast tier
(``core.speech_stream``) takes ~1 s a pass; whisper's accurate pass still
writes every phrase's final text (bench: ``notes/stt-2026-11/bench.md`` on
branch ``emandel2630/stt-investigation``; this lane's own numbers come from
``tests/bench/moonshine_lane_bench.py``).

:class:`MoonshineStream` is a drop-in :class:`speech_stream.Stream` — a
subclass, so the routes, the registry's sweep and ``Stream.turn`` work on it
unchanged: ``feed(samples) → (newly committed words, unstable tail)``,
``finish() → {"committed", "partial": "", "final", "final_ms",
"final_wait_ms", "final_early", "same"}`` (plus ``"heard"``, Moonshine's own
text, and ``"final_by"``: ``"moonshine"`` or ``"whisper"``), ``turn()``,
``last_ms``, ``last_window_s``, ``held_seconds``, ``text``, ``words``,
``client``, ``touched``, ``busy``.

How the words come. Moonshine is a true streaming model: the audio is fed
into one engine stream that keeps its encoder state, and every pass decodes
the current *line* (Moonshine's own VAD cuts the speech into lines) again —
so a pass costs only the new audio. A line's words change while it is open
("Nor is it." → "Nor is Mr. Crowell." → "Nor is Mr. Quilter's manor," …),
and a client cannot take back a typed word, so a word is committed when two
consecutive *different* hypotheses of the open line agree on it (the
LocalAgreement-2 policy of ``speech_stream``; Moonshine re-decodes on its own
cadence, so two identical answers in a row are one hypothesis, not two
agreeing ones). A completed line is committed whole. A word that may be the
first half of a split wake word ("Mind" of "Mind flock") is held back until
the next word shows it is not (:func:`join_wake`): the client never types
half the wake word.

The final. Two jobs, two models. Moonshine (keyterm-biased, ~60 ms) hears
the CONTROL words — the wake word, "over", the end, clear, stop and hush
phrases, a session's name: the live words, and ``heard`` at the end (its
own text of the whole phrase). Whisper (``small`` with the same hints, ~8%
word errors on real speech and ~5% on commands against Moonshine tiny's
~13% and ~10%) writes the TEXT: every phrase with at least
``speech_stream.FINAL_MIN_SECONDS`` of speech gets the accurate pass when
the stream was opened with an ``accurate(audio)`` callable
(``LocalSTT.final_transcriber``), exactly as ``speech_stream.Stream`` runs
it — started early on the quiet tail (:data:`EARLY_QUIET_SECONDS`, or
``RADIO_QUIET_SECONDS`` once Moonshine's words end in a send, end or stop
phrase), started fresh by :meth:`MoonshineStream.finish` when there is no
early pass or the speech went on past it, waited for up to
``FINAL_TIMEOUT_S`` — and its text is the ``final`` (``final_by:
"whisper"``) with its trailing control phrase made Moonshine's
(:func:`with_control`: a "…, over" whisper heard as "ober" still sends,
with whisper's words before it). Moonshine's text is the ``final``
(``final_by: "moonshine"``) when the pass is off, skipped, failed or timed
out — and, at once with no pass at all, for a phrase that is only a
control phrase (:func:`control_only`: "stop", "over", a switch to a
session): acting on it never waits for whisper.

Keyterms. Moonshine streaming models take a list of terms to bias towards
(``Transcriber.set_keyterms``): :func:`keyterms` splits the same hint string
the whisper final gets (``speech.recognition_hints``: the voice words, the
session titles, the MindFlock vocabulary). The list belongs to the model,
not a stream, so the last stream opened sets it for all; they share one
source, so that is the same list.

Threads. Moonshine's ONNX Runtime takes every core and spins them while it
decodes (~7 cores busy on 8, 43 threads). The library offers no thread
count — only ``MOONSHINE_ORT_SINGLE_THREAD`` (read when a model loads), which
:data:`SINGLE_THREAD` sets: the tiny model is so small that one thread is
FASTER than the pool, not just cheaper (the bench). Set
``MOONSHINE_ORT_SINGLE_THREAD=0`` in the server's environment for the pool.

The model is loaded once through ``speech._cached`` under :data:`CACHE_KEY`,
so the idle sweep and the memory shedding drop it like a whisper model.
:func:`available` (the package importable and the model files on disk) lets
the caller fall back to whisper; ``core.speech_install.start(what=
"moonshine")`` installs both. English only (:func:`supports`).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from array import array
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from backend.web.core import speech as _speech
from backend.web.core import speech_stream as _stream

__all__ = [
    "ARCH",
    "CACHE_KEY",
    "EARLY_QUIET_SECONDS",
    "MODULE",
    "MoonshineEngine",
    "MoonshineStream",
    "PACKAGE",
    "NO_TEXT_KINDS",
    "available",
    "control_only",
    "engine",
    "fetch_model",
    "join_wake",
    "keyterms",
    "model_path",
    "open_stream",
    "supports",
    "unavailable_reason",
    "with_control",
]

_logger = logging.getLogger(__name__)

#: The pip package and the module it installs.
PACKAGE = "moonshine-voice"
MODULE = "moonshine_voice"
#: The model: ``moonshine_voice.ModelArch`` member name, lower-cased with
#: ``-`` (also the model's folder, ``<ARCH>-en``, under the download root).
ARCH = "tiny-streaming"
#: ``speech._cached``'s key for the loaded model.
CACHE_KEY = "moonshine:" + ARCH
#: The files a usable model folder holds (the loader needs every one; the
#: names are moonshine-voice 0.1.6's for a streaming model).
MODEL_FILES = (
    "encoder.ort",
    "decoder_kv.ort",
    "tokenizer.bin",
    "streaming_config.json",
)
#: ``voice_router.command`` kinds that need no text: a phrase Moonshine
#: hears as one of them is acted on at once, without the whisper pass
#: (:func:`control_only`; the router's own ``voice_router.NO_TEXT_KINDS`` —
#: scroll, interrupt, go back, where, the command list — count too). ``new``, ``answer``, ``question`` and ``help``
#: carry the person's words, so they wait for whisper's.
NO_TEXT_KINDS = frozenset(
    {
        "cancel",
        "stop",
        "hush",
        "off",
        "repeat",
        "condense",
        "switch",
        "switch_index",
        "switch_rel",
        "list",
        "needs",
        "status",
        "read",
        "pick",
    }
)
#: The quiet after the last loud frame that starts the whisper pass early
#: when Moonshine's words do not end in a radio word (those start it after
#: ``speech_stream.RADIO_QUIET_SECONDS``). Longer than whisper's own lane
#: (``speech_stream.EARLY_QUIET_SECONDS``, 0.3 s): a pass started on a
#: pause mid-phrase goes stale yet keeps the accurate lane until it
#: returns, and the pass that counts queues behind it. The clients end a
#: phrase without a radio word after 0.9 s of quiet or more, so this still
#: gives that pass a 0.3 s head start. Real-time bench
#: (``tests/bench/moonshine_lane_bench.py``): 0.3 → 0.6 s took /end p95
#: 1514 → 1114 ms and timeouts 4 → 0 on the 48 commands, p95 1553 → 1264
#: ms and timeouts 3 → 1 on 25 LibriSpeech phrases (p50 289 → 569 ms).
EARLY_QUIET_SECONDS = 0.6
#: Load the model with ONNX Runtime on one thread (see the module doc).
SINGLE_THREAD = True
_THREAD_ENV = "MOONSHINE_ORT_SINGLE_THREAD"
#: A stream's engine stream never updates on its own (we call each pass).
_NO_AUTO_UPDATE = 1e9

Word = _stream.Word


# --------------------------------------------------------------------------- #
# Where the model lives, and whether it can run
# --------------------------------------------------------------------------- #
def model_root() -> Path:
    """The download root (``moonshine_voice``'s ``cache_root``):
    ``speech.voice_dir()/moonshine``, beside the whisper and piper models."""
    return _speech.voice_dir() / "moonshine"


def model_path() -> Optional[Path]:
    """The folder of the downloaded model (``…/<ARCH>-en/<variant>/``) with
    every :data:`MODEL_FILES` in it, the newest variant when there are
    several → ``None`` when there is none. No download, no import."""
    root = model_root()
    if not root.is_dir():
        return None
    found = [
        d
        for d in root.glob("**/%s-en/*" % ARCH)
        if d.is_dir() and all((d / f).is_file() for f in MODEL_FILES)
    ]
    return sorted(found, key=lambda d: d.name)[-1] if found else None


def unavailable_reason() -> str:
    """Why Moonshine recognition cannot run here → ``""`` when it can."""
    if not _speech._installed(MODULE):
        return "%s is not installed" % PACKAGE
    if model_path() is None:
        return "the Moonshine %s model is not downloaded (to %s)" % (ARCH, model_root())
    return ""


def available() -> bool:
    """The package is importable and the model files are on disk: the caller
    opens :class:`MoonshineStream`s, else falls back to whisper."""
    return not unavailable_reason()


def supports(lang: str = "") -> bool:
    """Whether a stream in ``lang`` (a BCP-47 tag, ``""``: English) can run
    on Moonshine: the English model only."""
    tag = str(lang or "").strip().lower()
    return not tag or tag == "en" or tag.startswith("en-") or tag.startswith("en_")


def _arch():
    mv = _speech._import(MODULE)
    return getattr(mv.ModelArch, ARCH.upper().replace("-", "_"))


def fetch_model() -> str:
    """Download the model into :func:`model_root` (the installer's helper
    process runs this) → its folder."""
    root = model_root()
    root.mkdir(parents=True, exist_ok=True)
    mv = _speech._import(MODULE)
    path, _arch_used = mv.get_model_for_language("en", _arch(), cache_root=root)
    return str(path)


# --------------------------------------------------------------------------- #
# The engine: one loaded model, shared by every stream
# --------------------------------------------------------------------------- #
class MoonshineEngine:
    """A loaded Moonshine transcriber (``moonshine_voice.Transcriber``, or a
    fake in tests) and the lock every call on it takes: the streams share it
    and its keyterms, and a pass is a few ms on one thread."""

    def __init__(self, transcriber, threads: int = 0):
        self.transcriber = transcriber
        #: ONNX Runtime threads it loaded with (1, or 0: the library's pool).
        self.threads = threads
        # Re-entrant: a stream's __del__ (GC, any thread) closes under it.
        self.lock = threading.RLock()
        self._terms: Optional[Tuple[str, ...]] = None

    def set_keyterms(self, terms: Sequence[str]) -> None:
        """Bias towards ``terms`` (replacing the last list; ``[]``: none).
        Only calls into the model when the list changed."""
        want = tuple(terms or ())
        with self.lock:
            if want == self._terms:
                return
            self.transcriber.set_keyterms(list(want) or None)
            self._terms = want

    @property
    def keyterms(self) -> Tuple[str, ...]:
        return self._terms or ()

    def open(self):
        """A started engine stream."""
        with self.lock:
            stream = self.transcriber.create_stream(update_interval=_NO_AUTO_UPDATE)
            stream.start()
            return stream


def _cap_threads() -> int:
    """Apply :data:`SINGLE_THREAD` before a load → the thread count the
    model will get (0: the library's pool, when the server's environment
    says ``MOONSHINE_ORT_SINGLE_THREAD=0``)."""
    if SINGLE_THREAD:
        os.environ.setdefault(_THREAD_ENV, "1")
    return 1 if os.environ.get(_THREAD_ENV, "") not in ("", "0") else 0


def _load() -> MoonshineEngine:
    path = model_path()
    if path is None:
        raise _speech.SpeechError(unavailable_reason() or "no Moonshine model")
    threads = _cap_threads()
    transcriber_mod = _speech._import(MODULE + ".transcriber")
    try:
        t = transcriber_mod.Transcriber(str(path), _arch())
    except Exception as err:  # noqa: BLE001
        raise _speech.SpeechError("Moonshine failed to load: %s" % err) from err
    return MoonshineEngine(t, threads)


def engine() -> MoonshineEngine:
    """The loaded model (loading it once, through ``speech._cached``, so the
    idle sweep and memory shedding apply). Raises ``SpeechError`` when it
    cannot load (see :func:`available`)."""
    return _speech._cached(CACHE_KEY, _load)  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# Text helpers
# --------------------------------------------------------------------------- #
def keyterms(hints: str) -> List[str]:
    """The comma-separated hint string (``speech.recognition_hints``) as
    Moonshine keyterms: one per phrase, trimmed, no empties or repeats
    (compared lower-cased)."""
    out: List[str] = []
    seen: set = set()
    for part in str(hints or "").split(","):
        term = " ".join(part.split())
        if term and term.lower() not in seen:
            seen.add(term.lower())
            out.append(term)
    return out


_norm = _stream._norm
#: The longest split of the wake word joined back ("mind f lock").
_JOIN_MAX = 3


def _letters(word: str) -> str:
    return "".join(ch for ch in word.lower() if ch.isalnum())


def _wake_of(words: Optional[dict]) -> str:
    wake = words.get("wake") if isinstance(words, dict) else None
    if not isinstance(wake, str) or not wake.strip():
        from backend.web.core import voice_words as _voice_words

        wake = str(_voice_words.DEFAULTS.get("wake") or "")
    return wake.strip()


def _join_list(words: List[str], wake: str) -> List[str]:
    key = _letters(wake)
    if not key or " " in wake.strip():
        return list(words)
    out: List[str] = []
    i = 0
    while i < len(words):
        size = _wake_span(words, i, key)
        if not size:
            out.append(words[i])
            i += 1
            continue
        first, last = words[i], words[i + size - 1]
        tail = last[len(last.rstrip(".,!?;:")) :]
        lead = wake[0].upper() if first[:1].isupper() else wake[0]
        out.append(lead + wake[1:] + tail)
        i += size
    return out


def _wake_span(words: Sequence[str], i: int, key: str) -> int:
    """How many words from ``words[i]`` spell the wake word ``key`` split
    up (2..:data:`_JOIN_MAX`, or one word with a hyphen or the like inside:
    "mind-flock") → 0 when none does (a plain "Mindflock," is left as is)."""
    for size in range(min(_JOIN_MAX, len(words) - i), 0, -1):
        span = words[i : i + size]
        if "".join(_letters(w) for w in span) != key:
            continue
        if (
            size == 1
            and _letters(span[0].rstrip(".,!?;:")) == span[0].rstrip(".,!?;:").lower()
        ):
            return 0
        return size
    return 0


def join_wake(text: str, wake: str = "mindflock") -> str:
    """``text`` with any split of the wake word joined back: "Mind flock,
    stop." → "Mindflock, stop." (also "mind-flock", "mind f lock"); the last
    piece's punctuation stays, a capital first letter stays. A wake word with
    a space in it is left alone."""
    return " ".join(_join_list(str(text or "").split(), wake))


def _wake_prefix(words: Sequence[str], wake: str) -> bool:
    """Whether ``words`` joined could still grow into the wake word: their
    letters are a strict, non-empty prefix of it."""
    key = _letters(wake)
    got = "".join(_letters(w) for w in words)
    return bool(got) and len(got) < len(key) and key.startswith(got)


def _holds(words: Sequence[str], paid: int, n: int, wake: str) -> bool:
    """Whether committing ``words[paid:n]`` could type the first half of a
    split wake word: some run of them ending at ``n`` spells a prefix of it,
    and the hypothesis ends there or its next word still continues it."""
    for k in range(max(paid, n - _JOIN_MAX), n):
        if not _wake_prefix(words[k:n], wake):
            continue
        if n >= len(words) or _wake_prefix(words[k : n + 1], wake):
            return True
    return False


def _control_tail(text: str, words: Optional[dict]) -> Tuple[str, int]:
    """The control phrase ``text`` ends with (``voice_words.control_kind``
    with ``words``) and how many of its last whitespace-separated tokens
    say it — the fewest that are that phrase on their own ("…, over it" →
    ``("send", 2)``) → ``("", 0)`` when there is none."""
    from backend.web.core import voice_words as _voice_words

    kind = _voice_words.control_kind(text, words) or ""
    toks = str(text or "").split()
    if not kind:
        return "", 0
    for n in range(1, len(toks) + 1):
        if _voice_words.control_kind(" ".join(toks[-n:]), words) == kind:
            return kind, n
    return kind, len(toks)


def _unwake(toks: List[str], words: Optional[dict]) -> List[str]:
    """``toks`` without a leading wake word ("Mindflock, stop." → "stop.")."""
    if toks and _letters(toks[0]) == _letters(_wake_of(words)):
        return toks[1:]
    return toks


def control_only(heard: str, words: Optional[dict] = None) -> bool:
    """Whether Moonshine's text of a phrase (``heard``) is a control phrase
    with nothing to type or send: a hush phrase, a clear or undo phrase (the
    words before it are thrown away), a send / end / stop phrase with no words
    before it, or — ending in none of those — a ``voice_router.command`` of
    a kind in :data:`NO_TEXT_KINDS` (a switch, "stop", "cancel", "what needs
    me"…); a leading wake word aside. Words before a send, end or stop
    phrase are text ("Open a pull request for this branch, over" is no
    switch). Such a phrase is acted on at once: no whisper pass is waited
    for. ``words``: the effective voice words (None: the defaults)."""
    toks = _unwake(str(heard or "").split(), words)
    text = " ".join(toks)
    if not _letters(text):
        return False
    try:
        kind, n = _control_tail(text, words)
        if kind in ("hush", "clear", "undo"):
            return True
        if kind:
            return not _letters(" ".join(toks[:-n]))
        from backend.web.core import voice_router as _voice_router

        cmd = _voice_router.command(text, words)
    except Exception:  # noqa: BLE001 — a hint: whisper's text then
        _logger.debug("voice: control_only failed on %r", text, exc_info=True)
        return False
    kind = cmd.get("kind") if cmd is not None else None
    return kind in NO_TEXT_KINDS or kind in _voice_router.NO_TEXT_KINDS


def with_control(final: str, heard: str, words: Optional[dict] = None) -> str:
    """Whisper's text of a phrase (``final``) ending in the control phrase
    Moonshine heard (``heard``) — the control words are Moonshine's, the
    words before them whisper's. When ``heard`` ends in a send, end, clear
    or stop phrase and ``final`` does not end in the same kind, ``final``'s
    own control phrase (one of another kind), or else its last words that
    sound like Moonshine's control words, or else its last words beyond the
    count of Moonshine's words before them that are none of those words,
    make way for Moonshine's: "Send the report, okay." + "Send the report
    over." → "Send the report over."; "Send the report." → "Send the report
    over."; "Mind flock, run it." + "Mindflock, run it, over." keeps "it".
    ``final`` unchanged otherwise (no such phrase in ``heard``, or the same
    kind in both: the clients strip it as ever)."""
    from backend.web.core import voice_words as _voice_words

    try:
        kind, n = _control_tail(heard, words)
        if kind not in ("send", "end", "clear", "undo", "stop"):
            return final
        fkind, fn = _control_tail(final, words)
        if fkind == kind:
            return final
        htoks, ftoks = str(heard).split(), str(final).split()
        tail = htoks[-n:]
        if fkind:
            drop = fn
        else:
            said = " ".join(tail)
            drop = next(
                (
                    d
                    for d in range(min(n + 1, len(ftoks)), 0, -1)
                    if _voice_words.sounds_like(" ".join(ftoks[-d:]), said)
                ),
                0,
            )
            if not drop:
                # Words whisper has beyond Moonshine's body, none of them a
                # word of that body: the garbled control phrase.
                body = {_letters(w) for w in htoks[:-n]}
                most = max(0, min(len(ftoks) - (len(htoks) - n), n))
                while drop < most and _letters(ftoks[-1 - drop]) not in body:
                    drop += 1
        keep = ftoks[: len(ftoks) - drop]
        if keep:
            keep[-1] = keep[-1].rstrip(".,!?;:")
        return " ".join(keep + tail)
    except Exception:  # noqa: BLE001 — whisper's text as it came
        _logger.debug("voice: with_control failed", exc_info=True)
        return final


# --------------------------------------------------------------------------- #
# The stream
# --------------------------------------------------------------------------- #
def _no_transcribe(audio, prompt):  # pragma: no cover — never called
    raise _speech.SpeechError("a Moonshine stream has no whisper fast pass")


class MoonshineStream(_stream.Stream):
    """One live recognition on Moonshine (see the module doc). ``engine``:
    a :class:`MoonshineEngine` (default: :func:`engine`); ``accurate``: the
    whisper pass that writes every phrase's ``final``
    (``LocalSTT.final_transcriber``, or None: Moonshine's text stands);
    ``hints``: the keyterm source (``speech.recognition_hints``)."""

    def __init__(
        self,
        engine_: Optional[MoonshineEngine] = None,
        accurate: Optional[_stream.Accurate] = None,
        hints: str = "",
    ):
        super().__init__(_no_transcribe, accurate)
        self.engine = engine_ if engine_ is not None else engine()
        terms = keyterms(hints)
        if terms:
            self.engine.set_keyterms(terms)
        self._live = self.engine.open()
        self._closed = False
        self._paid: dict = {}  # line id → words of it committed so far
        self._seen: dict = {}  # line id → its last distinct hypothesis
        self._done: set = set()  # completed lines, fully committed
        self._lines: dict = {}  # line id → its latest text (in first-seen order)
        self._held_from = 0.0  # stream time since the open line began
        self._passed_at = 0.0  # stream time of the last pass
        #: Who made the last finish's ``final``: ``"moonshine"`` or ``"whisper"``.
        self.final_by = ""

    # -- the public interface ------------------------------------------- #
    @property
    def held_seconds(self) -> float:
        """Audio since the last line Moonshine completed, in seconds."""
        return max(0.0, self._fed - self._held_from)

    def feed(self, samples: Sequence[float]) -> Tuple[str, str]:
        """Append ``samples`` (float32 at 16 kHz) and run one pass →
        ``(newly committed words, the open line's uncommitted tail)``. No
        pass for silence with nothing pending. A Moonshine failure raises
        ``speech.SpeechError`` (the audio stays in the engine stream)."""
        with self._lock:
            self.touched = _stream._now()
            self._learn(samples)
            quiet = _stream._quiet(samples, self.quiet_rms())
            if self._accurate is None:
                self.voiced_s += _stream._voiced(samples)
            self._keep(samples)
            self._mark(samples)
            self.last_ms, self.last_window_s = 0, 0.0
            if not len(samples):
                return "", self._tail_text()
            self._add(samples)
            if quiet:
                self._start_early(
                    _stream.RADIO_QUIET_SECONDS if self._radio_end() else None
                )
            else:
                self._unheard = True
                self._stale_early()
            if quiet and not self._tail:
                return "", ""
            new = self._update(final=False)
            if self._radio_end():
                # Moonshine heard the words end in "over", an end or a stop
                # phrase: the client ends the phrase within 100-350 ms.
                self._start_early(_stream.RADIO_QUIET_SECONDS)
            self.touched = _stream._now()
            return _stream._join(new), self._tail_text()

    def finish(self) -> dict:
        """End the phrase: the engine stream stops (its open line completes)
        → ``{"committed": the words not committed before, "partial": ""}``
        plus, whenever any words were heard, ``final`` (the whisper accurate
        pass's text of the whole phrase, its control phrase Moonshine's:
        :func:`with_control`; Moonshine's own text when the pass is off,
        skipped, failed or timed out, or the phrase is only a control phrase:
        :func:`control_only`), ``heard`` (Moonshine's own text of the whole
        phrase: what the control words are read off), ``final_ms``,
        ``final_wait_ms``, ``final_early``, ``same`` (``final`` is the
        committed text up to ``same_text``) and ``final_by``."""
        with self._lock:
            self.touched = _stream._now()
            phrase, self._phrase = self._phrase, array("f")
            early, self._early = self._early, None
            started = time.monotonic()
            try:
                rest = self._update(final=True)
            except BaseException:
                if early is not None:
                    early.cancel("the Moonshine flush failed")
                raise
            finally:
                self._close()
            moon_ms = int((time.monotonic() - started) * 1000)
            out: dict = {"committed": _stream._join(rest), "partial": ""}
            fast = self.text
            spoken = self._spoken()
            run = (
                self._accurate is not None
                and bool(phrase)
                and not self.phrase_trimmed
                and self.voiced_s >= _stream.FINAL_MIN_SECONDS
                and not control_only(spoken, self.words)
            )
            job: Optional[_stream._Early] = None
            if not run:
                if early is not None:
                    early.cancel("the Moonshine final stands")
            elif not self._covers(early):
                # No early pass, or one the speech went on past: the whole
                # phrase again (the flush above is Moonshine's ~2-60 ms).
                job = self._fresh(phrase, early)
        self.final_early = False
        self.final_outcome = ""
        final, self.final_by = "", ""
        if run:
            if job is not None:
                final = self._final(job)
            else:
                final = self._await(early)  # type: ignore[arg-type]
            _stream._record(self.final_wait_ms, self.final_outcome)
            self._log_timing(len(phrase) / _stream.RATE)
        self.pass_ms = []
        if final:
            final = with_control(
                join_wake(final, _wake_of(self.words)), spoken, self.words
            )
            self.final_by = "whisper"
        elif spoken:
            final, self.final_by = spoken, "moonshine"
            self.final_ms = self.final_wait_ms = moon_ms
            self.final_early = False
        if final:
            out.update(
                final=final,
                heard=spoken,
                final_ms=self.final_ms,
                final_wait_ms=self.final_wait_ms,
                final_early=self.final_early,
                same=_stream.same_text(final, fast),
                final_by=self.final_by,
            )
        return out

    def close(self) -> None:
        """Free the engine stream (abandoned without ``finish``)."""
        with self._lock:
            self._close()

    def __del__(self):  # an expired stream frees its engine stream
        try:
            self._close()
        except Exception:  # noqa: BLE001
            pass

    # -- internals (under the lock) -------------------------------------- #
    def _start_early(self, quiet: Optional[float] = None) -> None:
        super()._start_early(EARLY_QUIET_SECONDS if quiet is None else quiet)

    def _tail_text(self) -> str:
        return _stream._join(self._tail)

    def _add(self, samples: Sequence[float]) -> None:
        try:
            with self.engine.lock:
                self._live.add_audio(list(samples), _stream.RATE)
        except Exception as err:  # noqa: BLE001
            raise _speech.SpeechError("Moonshine failed: %s" % err) from err

    def _close(self) -> None:
        if getattr(self, "_closed", True):
            return
        self._closed = True
        live, self._live = self._live, None
        close = getattr(live, "close", None)
        if close is not None:
            with self.engine.lock:
                close()

    def _update(self, final: bool) -> List[Word]:
        """One pass (``final``: stop the engine stream, which completes the
        open line) → the words it commits, in order."""
        started = time.monotonic()
        try:
            with self.engine.lock:
                if final:
                    transcript = self._live.stop()
                else:
                    transcript = self._live.update_transcription()
        except Exception as err:  # noqa: BLE001
            raise _speech.SpeechError("Moonshine failed: %s" % err) from err
        self.last_ms = int((time.monotonic() - started) * 1000)
        self.pass_ms.append(self.last_ms)
        self.last_window_s = max(0.0, self._fed - self._passed_at)
        self._passed_at = self._heard_end = self._fed
        self._unheard = False
        wake = _wake_of(self.words)
        new: List[Word] = []
        tail: List[str] = []
        lines = list(getattr(transcript, "lines", None) or ())
        for line in lines:
            lid = getattr(line, "line_id", id(line))
            words = _join_list(str(getattr(line, "text", "") or "").split(), wake)
            self._lines[lid] = " ".join(words)
            if lid in self._done:
                continue
            paid = self._paid.get(lid, 0)
            if final or getattr(line, "is_complete", False):
                start = self._resume(lid, words, paid)
                new.extend(self._word(w) for w in words[start:])
                self._done.add(lid)
                self._paid.pop(lid, None)
                self._seen.pop(lid, None)
                self._held_from = self._fed
                continue
            before = self._seen.get(lid)
            if before is not None and [_norm(w) for w in before] == [
                _norm(w) for w in words
            ]:
                tail = words[paid:]
                continue  # the same hypothesis again: no new agreement
            self._seen[lid] = words
            n = paid
            if before is not None:
                n = max(paid, self._agree(before, words))
            # Never type the first half of a split wake word.
            while n > paid and _holds(words, paid, n, wake):
                n -= 1
            new.extend(self._word(w) for w in words[paid:n])
            self._paid[lid] = n
            tail = words[n:]
        self._committed.extend(new)
        self._tail = [self._word(w) for w in tail]
        return new

    def _resume(self, lid, words: List[str], paid: int) -> int:
        """Where a completed line's words continue after the ``paid`` already
        committed of it: right after them when the line still starts with
        them; else just after the last committed word where it reappears
        near there; else ``paid`` (the ``final`` corrects the rest)."""
        if not paid:
            return 0
        typed = [_norm(w[0]) for w in self._committed[-paid:]]
        if [_norm(w) for w in words[:paid]] == typed:
            return paid
        last = typed[-1]
        for k in sorted(range(len(words)), key=lambda k: abs(k - (paid - 1))):
            if abs(k - (paid - 1)) > 2:
                break
            if _norm(words[k]) == last:
                return k + 1
        return min(paid, len(words))

    @staticmethod
    def _agree(a: Sequence[str], b: Sequence[str]) -> int:
        n = 0
        while n < len(a) and n < len(b) and _norm(a[n]) == _norm(b[n]):
            n += 1
        return n

    @staticmethod
    def _word(text: str) -> Word:
        return (text, 0.0, 0.0)

    def _spoken(self) -> str:
        """Moonshine's own text of the phrase: every line as last decoded
        (a line's words may have changed after some were committed)."""
        return " ".join(t for t in self._lines.values() if t)


# --------------------------------------------------------------------------- #
# The registry: the same one (and the same caps) as speech_stream's
# --------------------------------------------------------------------------- #
def open_stream(
    hints: str = "",
    accurate: Optional[_stream.Accurate] = None,
    client: str = "",
    engine_: Optional[MoonshineEngine] = None,
) -> Optional[str]:
    """``speech_stream.open_stream`` for Moonshine: a new
    :class:`MoonshineStream` registered in ``speech_stream``'s registry (so
    ``get_stream``/``close_stream``/the sweep, :data:`speech_stream.MAX_STREAMS`
    and the per-client cap all apply) → its id, or ``None`` at the cap.
    Raises ``speech.SpeechError`` when the model cannot load (fall back to
    whisper)."""
    stream = MoonshineStream(engine_, accurate, hints)
    with _stream._REG_LOCK:
        sid = _stream.open_stream(_no_transcribe, None, client)
        if sid is None:
            stream.close()
            return None
        stream.client = client
        _stream._STREAMS[sid] = stream
    return sid
