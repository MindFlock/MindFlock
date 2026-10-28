"""Streaming recognition: the words appear while the person speaks
(``POST /api/voice/stream*``, ``routes/speech.py``; docs/web-api.md).

Whisper is not a streaming model, so this is the rolling window with
committed words — the "LocalAgreement-2" policy of whisper_streaming. A
:class:`Stream` holds the audio since (just before) the last committed word;
every :meth:`Stream.feed` appends a chunk and re-transcribes the whole
window; the words two consecutive passes agree on (the longest common
prefix of their word lists, compared lower-cased without punctuation) become
committed, and the unstable tail waits for the next pass. Clients type only
committed words — a terminal cannot be corrected — so a committed word is
never revisited: the next pass drops what it re-hears of them (by timestamp,
then by matching up to five trailing committed words).

Bounded work, because a pass's cost grows with the audio it decodes: once
the window holds more than :data:`PRUNE_SECONDS`, the audio before the last
committed word's end (less :data:`MARGIN_SECONDS`) is dropped — never audio
under an uncommitted word. A window with no words in it at all keeps only
its last second. And a pass never decodes more than :data:`WINDOW_SECONDS`:
the one forced commit is at that edge — before a pass, the tail words that
start before the last :data:`WINDOW_SECONDS` of audio (a tail that never
settled) are committed as they leave, and the audio before the edge goes.

Silence costs nothing: a chunk whose RMS is under :data:`SILENCE_RMS`,
appended to a window with no uncommitted tail, runs no pass (``last_ms`` is
0) — there is nothing for a pass to settle and nothing new to hear. The
audio is kept, so a soft start of speech is decoded with the next chunk.
Silence also hears nothing: whisper fills a quiet stretch after the speech
with a word ("Never.", "for.", or one of the hint words), so a word a pass
places more than :data:`SILENCE_SLACK` after the last loud 100 ms frame is
dropped — an audible word starts with a loud frame, so the last one is at
or after every real word's start.

Two tiers. The passes above run the FAST model (``base.en``): they are what
the client types as the person speaks. A stream opened with an
``accurate(audio)`` callable as well (``LocalSTT.final_transcriber``: the
one-shot ``small``, ``voice.stt_final``) keeps the whole phrase's audio
beside the window — at most :data:`PHRASE_MAX_SECONDS`, the last of it
beyond that — and :meth:`Stream.finish` decodes all of it once more with the
accurate model: ``final`` is the text the client replaces its typed phrase
with (``same`` when it only differs from the fast words in case, spacing or
punctuation). The accurate pass is skipped for a phrase with less than
:data:`FINAL_MIN_SECONDS` of speech and for one longer than the buffer (the
final would not cover what was typed); one that fails or takes longer than
:data:`FINAL_TIMEOUT_S` leaves the fast text standing.

The accurate pass starts EARLY. Whisper's encoder costs the same for a
second of speech as for thirty, so the pass is ~1 s on ``small`` whatever
the phrase, and a client that acts on a radio word waits for it. A quiet
chunk means the person paused — usually the phrase is over — so
:meth:`Stream.feed` starts the accurate pass on the phrase so far right then
(``_start_early``, one at a time), while the fast tier still settles the
last words and the client decides. :meth:`Stream.finish` takes that result
when no speech arrived after it started (``final_early``; ``final_wait_ms``
is how long it still had to wait, often 0) and runs a fresh pass otherwise.
A pause mid-sentence costs one wasted pass. Likewise ``finish`` skips its
last fast pass when nothing loud arrived since the previous one and no tail
waits: there is nothing new to hear.

The measured wait of recent finishes is kept (:func:`final_stats`) so
``GET /api/voice`` can tell a client how long ``/end`` typically takes.

Pure: a stream is built on a ``transcribe(audio, prompt)`` callable
(``core.speech.LocalSTT.stream_transcriber`` in the server, a scripted fake
in tests) that takes the float32 samples at 16 kHz (an ``array('f')``) and
the last ~200 characters of committed text (Whisper's ``initial_prompt``)
and returns ``[(word, start_s, end_s)]`` relative to the start of the
samples. No numpy here — the server process does not have it unless the
local models are installed.

The registry (:func:`open_stream` …) holds at most :data:`MAX_STREAMS`;
one expires :data:`EXPIRE_SECONDS` after its last chunk, swept on every
registry call and by one daemon timer armed while any stream is open (no
thread per stream). Tests replace :func:`_now` (the clock).
"""

from __future__ import annotations

import collections
import io
import logging
import secrets
import sys
import threading
import time
import wave
from array import array
from typing import Callable, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "EXPIRE_SECONDS",
    "FINAL_EXPECT_MS",
    "FINAL_MIN_SECONDS",
    "FINAL_TIMEOUT_S",
    "MAX_STREAMS",
    "PHRASE_MAX_SECONDS",
    "RATE",
    "SILENCE_RMS",
    "SILENCE_SLACK",
    "Stream",
    "WINDOW_SECONDS",
    "clear",
    "close_stream",
    "decode_wav",
    "final_stats",
    "get_stream",
    "open_stream",
    "same_text",
    "sweep",
]

_logger = logging.getLogger(__name__)

#: The sample rate of everything a stream holds (Whisper's).
RATE = 16000
#: Prune the held audio once it is longer than this.
PRUNE_SECONDS = 4.0
#: Audio kept before the end of the last committed word when pruning.
MARGIN_SECONDS = 0.3
#: A window with no words in it keeps only this much audio.
SILENT_KEEP_SECONDS = 1.0
#: The most audio one pass decodes; older uncommitted words are committed
#: as they leave it (the forced commit).
WINDOW_SECONDS = 8.0
#: The old name of the forced-commit limit: it is the window now.
MAX_HELD_SECONDS = WINDOW_SECONDS
#: A chunk quieter than this (RMS, samples in [-1, 1); about -40 dBFS) is
#: silence: with no uncommitted tail it runs no pass.
SILENCE_RMS = 0.01
#: The ``initial_prompt``: this much of the committed text, from its end.
PROMPT_CHARS = 200
#: A re-heard word starting this long before the last committed end is old.
_OVERLAP_SECONDS = 0.1
#: How many trailing committed words a pass's opening words are matched to.
_NGRAM = 5

#: The phrase audio a stream keeps for the accurate pass; a longer phrase
#: keeps its last this-many seconds and skips the pass.
PHRASE_MAX_SECONDS = 60.0
#: A phrase with less speech than this (seconds of 100 ms frames louder than
#: :data:`SILENCE_RMS`) skips the accurate pass: nothing to gain. Two frames:
#: a lone radio word ("stop", ~0.3 s) is exactly what the fast tier mishears
#: and the accurate pass must get right, so only a click is skipped.
FINAL_MIN_SECONDS = 0.2
#: The accurate pass's deadline — how long :meth:`Stream.finish` waits for
#: it, past which the fast text stands. The bench (tests/bench/voice_bench.py)
#: puts a fresh ``small`` pass at ~1.3 s on this class of CPU; the early
#: start absorbs some of it (the wait's median is ~0.7 s, its p95 1.35 s).
FINAL_TIMEOUT_S = 1.5
#: How many recent finishes :func:`final_stats` is computed over.
STATS_KEEP = 20
#: What a client should budget for ``/end``'s accurate pass before this
#: server has measured any: the bench's p95 ``finish`` wait of the default
#: models on an 8-core CPU, 1346 ms (tests/bench/voice_bench.py; the median
#: is ~700 ms); :func:`final_stats` serves the measured p95 once there are
#: :data:`STATS_MIN` finishes to go by.
FINAL_EXPECT_MS = 1400
STATS_MIN = 3
#: The frame the speech in a chunk is counted in (100 ms).
_FRAME = RATE // 10
#: A word a pass starts this long after the last loud frame is invented.
SILENCE_SLACK = 0.4

#: Open streams per server (``POST /api/voice/stream`` refuses beyond).
MAX_STREAMS = 4
#: A stream is forgotten this long after its last chunk.
EXPIRE_SECONDS = 60.0

Word = Tuple[str, float, float]
Transcribe = Callable[[array, str], Sequence[Tuple[str, float, float]]]
Accurate = Callable[[array], str]


def _now() -> float:
    return time.monotonic()


def _norm(word: str) -> str:
    """The comparison form of a word: lower-case, punctuation stripped (a
    word that is all punctuation compares as itself)."""
    low = word.strip().lower()
    return "".join(ch for ch in low if ch.isalnum()) or low


def _join(words: Sequence[Word]) -> str:
    return " ".join(w[0] for w in words)


def _quiet(samples: Sequence[float]) -> bool:
    """Whether a chunk is silence: its RMS is under :data:`SILENCE_RMS` (an
    empty chunk is quiet)."""
    n = len(samples)
    if not n:
        return True
    return sum(x * x for x in samples) / n < SILENCE_RMS * SILENCE_RMS


def _voiced(samples: Sequence[float]) -> float:
    """The seconds of ``samples`` in 100 ms frames that are not quiet."""
    return _frames(samples)[0] / RATE


def _frames(samples: Sequence[float]) -> Tuple[int, int]:
    """``(loud samples, the end of the last loud frame)`` of ``samples`` in
    100 ms frames; the end is -1 when none is loud."""
    loud, last = 0, -1
    for i in range(0, len(samples), _FRAME):
        frame = samples[i : i + _FRAME]
        if not _quiet(frame):
            loud += len(frame)
            last = i + len(frame)
    return loud, last


def _plain(text: str) -> List[str]:
    return "".join(
        ch for ch in str(text or "").lower() if ch.isalnum() or ch.isspace()
    ).split()


def same_text(a: str, b: str) -> bool:
    """Whether two transcripts are the same words: compared lower-cased,
    punctuation dropped, whitespace collapsed."""
    return _plain(a) == _plain(b)


class Stream:
    """One live recognition: see the module doc for the policy. Thread-safe —
    one pass at a time (a chunk that arrives during a pass waits, then gets
    the state after its own pass)."""

    def __init__(self, transcribe: Transcribe, accurate: Optional[Accurate] = None):
        self._transcribe = transcribe
        self._accurate = accurate
        self._lock = threading.Lock()
        self._audio = array("f")
        self._phrase = array("f")  # the whole phrase, for the accurate pass
        #: The phrase outgrew :data:`PHRASE_MAX_SECONDS` (no accurate pass).
        self.phrase_trimmed = False
        #: Seconds of speech in the phrase (see :data:`FINAL_MIN_SECONDS`).
        self.voiced_s = 0.0
        #: How long the accurate pass took (0 when it did not run).
        self.final_ms = 0
        #: How long :meth:`finish` waited for it (0: it was done already).
        self.final_wait_ms = 0
        #: The last finish's accurate text came from the early pass.
        self.final_early = False
        self._early: Optional[_Early] = None  # the pass started on a quiet tail
        self._unheard = False  # loud audio arrived since the last fast pass
        self._offset = 0.0  # stream seconds before _audio[0]
        self._fed = 0.0  # stream seconds fed so far
        self._loud_end = -1.0  # stream time of the last loud frame's end
        self._committed: List[Word] = []  # stream-absolute times
        self._tail: List[Word] = []  # the last pass's uncommitted words
        self._owed: List[Word] = []  # committed, not yet returned (a pass failed)
        self.last_ms = 0
        #: Seconds of audio the last feed's pass decoded (0 when it ran none).
        self.last_window_s = 0.0
        self.touched = _now()

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    @property
    def held_seconds(self) -> float:
        """The audio the window holds now, in seconds."""
        return len(self._audio) / RATE

    @property
    def phrase_seconds(self) -> float:
        """The phrase audio kept for the accurate pass, in seconds."""
        return len(self._phrase) / RATE

    @property
    def text(self) -> str:
        """Everything committed so far."""
        return _join(self._committed)

    def feed(self, samples: Sequence[float]) -> Tuple[str, str]:
        """Append ``samples`` (float32 at 16 kHz) and run one pass →
        ``(newly committed words, the current unstable tail)``; either may
        be ``""``. No pass for silence with nothing pending (see the module
        doc). A :class:`core.speech.SpeechError` from the transcriber
        propagates, with the chunk kept for the next pass."""
        with self._lock:
            self.touched = _now()
            quiet = _quiet(samples)
            self._audio.extend(samples)
            self._keep(samples)
            self._mark(samples)
            self.last_ms, self.last_window_s = 0, 0.0
            if not self._audio:
                return "", ""
            self._cap()
            if quiet:
                self._start_early()
            else:
                self._unheard = True
                if self._early is not None:
                    self._early.stale = True
            if quiet and not self._tail:
                return self._pay([]), ""
            hyp = self._pass()
            n = 0
            while (
                n < len(hyp)
                and n < len(self._tail)
                and _norm(hyp[n][0]) == _norm(self._tail[n][0])
            ):
                n += 1
            new = hyp[:n]
            self._committed.extend(new)
            self._tail = hyp[n:]
            self._prune()
            self.touched = _now()
            return self._pay(new), _join(self._tail)

    def finish(self) -> dict:
        """End the phrase → ``{"committed": the fast words not committed
        before, "partial": ""}`` after one last fast pass over the window,
        all of it committed; plus ``"final"`` (the accurate pass's text of
        the whole phrase), ``"final_ms"`` (the pass's own time),
        ``"final_wait_ms"`` (how long this call waited for it),
        ``"final_early"`` (it was the pass started on the quiet tail) and
        ``"same"`` (``final`` is the fast text up to :func:`same_text`) when
        the accurate pass ran and heard words (see the module doc for when it
        does not). The last fast pass is skipped when it could hear nothing
        new (no tail, nothing loud since the previous pass). A failed fast
        pass raises, as in :meth:`feed`; a failed accurate one is logged."""
        with self._lock:
            self.touched = _now()
            self._cap()
            fresh = self._audio and (self._tail or self._unheard)
            rest = self._pass() if fresh else []
            self._committed.extend(rest)
            self._tail = []
            self._offset += self.held_seconds
            self._audio = array("f")
            out: dict = {"committed": self._pay(rest), "partial": ""}
            fast, phrase = self.text, self._phrase
            self._phrase = array("f")
            early, self._early = self._early, None
            run = self._accurate is not None and phrase
            run = run and not self.phrase_trimmed
            run = run and self.voiced_s >= FINAL_MIN_SECONDS
        self.final_early = False
        if not run:
            final = ""
        elif early is not None and not early.stale:
            final = self._await(early)
        else:
            final = self._final(phrase)
        if run:
            _record(self.final_wait_ms)
        if final:
            out.update(
                final=final,
                final_ms=self.final_ms,
                final_wait_ms=self.final_wait_ms,
                final_early=self.final_early,
                same=same_text(final, fast),
            )
        return out

    # -- internals (called under the lock) -------------------------------- #
    def _mark(self, samples: Sequence[float]) -> None:
        """Note where the chunk's last loud frame ends, in stream time."""
        _loud, last = _frames(samples)
        if last >= 0:
            self._loud_end = self._fed + last / RATE
        self._fed += len(samples) / RATE

    def _keep(self, samples: Sequence[float]) -> None:
        """Add a chunk to the phrase buffer, at most its last
        :data:`PHRASE_MAX_SECONDS`."""
        if self._accurate is None:
            return
        self._phrase.extend(samples)
        self.voiced_s += _voiced(samples)
        drop = len(self._phrase) - int(PHRASE_MAX_SECONDS * RATE)
        if drop > 0:
            del self._phrase[:drop]
            if not self.phrase_trimmed:
                _logger.info(
                    "voice stream: phrase over %ds, no accurate pass at its end",
                    PHRASE_MAX_SECONDS,
                )
            self.phrase_trimmed = True

    def _start_early(self) -> None:
        """A quiet chunk arrived: start the accurate pass on the phrase so
        far (a copy), unless one that is still good is running or done, the
        phrase is too short or too long for a pass, or there is none. Under
        the lock; the pass itself runs on its own daemon thread."""
        if self._accurate is None or self.phrase_trimmed:
            return
        if self.voiced_s < FINAL_MIN_SECONDS:
            return
        if self._early is not None and not (self._early.stale and self._early.done):
            return
        self._early = _Early(self._accurate, array("f", self._phrase))

    def _await(self, early: "_Early") -> str:
        """The early pass's text, waited for at most :data:`FINAL_TIMEOUT_S`
        more → ``""`` when it failed or timed out (logged). Not under the
        lock. Sets ``final_ms`` (its whole duration), ``final_wait_ms`` and
        ``final_early``."""
        started = time.monotonic()
        early.thread.join(FINAL_TIMEOUT_S)
        self.final_wait_ms = int((time.monotonic() - started) * 1000)
        self.final_ms = int((time.monotonic() - early.started) * 1000)
        self.final_early = True
        return self._outcome(early, early.seconds)

    def _final(self, phrase: array) -> str:
        """The accurate pass over ``phrase`` on its own daemon thread, at
        most :data:`FINAL_TIMEOUT_S` → its text, ``""`` when it failed or
        timed out (logged; a late result is dropped). Not under the lock."""
        early = _Early(self._accurate, phrase)  # type: ignore[arg-type]
        early.thread.join(FINAL_TIMEOUT_S)
        self.final_ms = int((time.monotonic() - early.started) * 1000)
        self.final_wait_ms = self.final_ms
        return self._outcome(early, len(phrase) / RATE)

    @staticmethod
    def _outcome(early: "_Early", seconds: float) -> str:
        if early.thread.is_alive():
            _logger.warning(
                "voice stream: the accurate pass took over %.1fs on %.1fs of"
                " audio; the fast text stands",
                FINAL_TIMEOUT_S,
                seconds,
            )
            return ""
        if "error" in early.box:
            _logger.warning(
                "voice stream: the accurate pass failed (%s); the fast text stands",
                early.box["error"],
            )
            return ""
        return str(early.box.get("text") or "").strip()

    def _prompt(self) -> str:
        text = self.text
        if len(text) <= PROMPT_CHARS:
            return text
        cut = text[-PROMPT_CHARS:]
        space = cut.find(" ")
        return cut[space + 1 :] if 0 <= space < len(cut) - 1 else cut

    def _pass(self) -> List[Word]:
        """Transcribe the window → its words beyond what is committed, in
        stream-absolute seconds."""
        started = time.monotonic()
        self.last_window_s = self.held_seconds
        try:
            raw = self._transcribe(self._audio, self._prompt())
        finally:
            self.last_ms = int((time.monotonic() - started) * 1000)
        self._unheard = False  # a failed pass leaves the audio unheard
        hyp: List[Word] = []
        edge = self._loud_end + SILENCE_SLACK
        for word, start, end in raw or ():
            text = str(word or "").strip()
            if text and float(start) + self._offset <= edge:
                hyp.append(
                    (text, float(start) + self._offset, float(end) + self._offset)
                )
        if not self._committed:
            return hyp
        last_end = self._committed[-1][2]
        hyp = [w for w in hyp if w[1] > last_end - _OVERLAP_SECONDS]
        if hyp and abs(hyp[0][1] - last_end) < 1.0:
            top = min(len(self._committed), len(hyp), _NGRAM)
            for k in range(top, 0, -1):
                said = [_norm(w[0]) for w in self._committed[-k:]]
                if said == [_norm(w[0]) for w in hyp[:k]]:
                    hyp = hyp[k:]
                    break
        return hyp

    def _pay(self, new: List[Word]) -> str:
        """The words to return as newly committed: any still owed, then
        ``new``."""
        out, self._owed = self._owed + new, []
        return _join(out)

    def _cap(self) -> None:
        """Hold at most :data:`WINDOW_SECONDS` before a pass: the tail words
        starting before the edge are committed (forced, see the module doc;
        owed to the caller until a feed returns) and the audio before the
        edge is dropped."""
        drop = len(self._audio) - int(WINDOW_SECONDS * RATE)
        if drop <= 0:
            return
        edge = self._offset + drop / RATE
        while self._tail and self._tail[0][1] < edge:
            word = self._tail.pop(0)
            self._committed.append(word)
            self._owed.append(word)
        del self._audio[:drop]
        self._offset = edge

    def _prune(self) -> None:
        """Drop audio no uncommitted word needs once the window is over
        :data:`PRUNE_SECONDS`."""
        if self.held_seconds > PRUNE_SECONDS:
            self._cut()

    def _cut(self) -> None:
        end = self._offset + self.held_seconds
        if self._committed:
            cut = self._committed[-1][2] - MARGIN_SECONDS
        else:
            cut = self._offset
        if self._tail:
            cut = min(cut, self._tail[0][1] - MARGIN_SECONDS)
        else:
            cut = max(cut, end - SILENT_KEEP_SECONDS)
        drop = int((cut - self._offset) * RATE)
        if drop > 0:
            del self._audio[:drop]
            self._offset += drop / RATE


class _Early:
    """One accurate pass in flight on its own daemon thread: ``box`` gets
    ``text`` or ``error``; ``stale`` is set when speech arrives after it
    started (its text would not cover the phrase)."""

    def __init__(self, accurate: Accurate, phrase: array):
        self.box: dict = {}
        self.stale = False
        self.seconds = len(phrase) / RATE
        self.started = time.monotonic()

        def run() -> None:
            try:
                self.box["text"] = accurate(phrase)
            except Exception as err:  # noqa: BLE001 — the fast text stands
                self.box["error"] = err

        self.thread = threading.Thread(target=run, name="voice-final", daemon=True)
        self.thread.start()

    @property
    def done(self) -> bool:
        return not self.thread.is_alive()


_WAITS: "collections.deque[int]" = collections.deque(maxlen=STATS_KEEP)


def _record(wait_ms: int) -> None:
    _WAITS.append(int(wait_ms))


def final_stats() -> dict:
    """How long :meth:`Stream.finish` waited for the accurate pass over the
    last :data:`STATS_KEEP` phrases that ran one → ``{"samples", "typical_ms"
    (the median), "p95_ms", "expect_ms", "timeout_ms"}``; the two percentiles
    are 0 before the first. ``expect_ms`` is what a client should budget for
    ``/end``: the measured p95 once :data:`STATS_MIN` phrases are in, the
    bench's :data:`FINAL_EXPECT_MS` before, never over the timeout. ``GET
    /api/voice`` serves it (``server_ready.final_ms`` is ``expect_ms``) so a
    client waits about that long instead of assuming the worst."""
    waits = sorted(_WAITS)
    n = len(waits)

    def pct(q: float) -> int:
        return waits[min(n - 1, int(round(q * (n - 1))))] if n else 0

    timeout = int(FINAL_TIMEOUT_S * 1000)
    expect = pct(0.95) if n >= STATS_MIN else FINAL_EXPECT_MS
    return {
        "samples": n,
        "typical_ms": pct(0.5),
        "p95_ms": pct(0.95),
        "expect_ms": min(expect, timeout),
        "timeout_ms": timeout,
    }


# --------------------------------------------------------------------------- #
# The clip format
# --------------------------------------------------------------------------- #
def decode_wav(data: bytes) -> array:
    """A 16 kHz mono 16-bit PCM WAV → its samples as float32 in [-1, 1).
    ``ValueError`` with a sentence for the page on anything else."""
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            rate, channels, width = (
                wav.getframerate(),
                wav.getnchannels(),
                wav.getsampwidth(),
            )
            frames = wav.readframes(wav.getnframes())
    except (wave.Error, EOFError) as err:
        raise ValueError("send the chunk as a WAV file (%s)" % err) from err
    if (rate, channels, width) != (RATE, 1, 2):
        raise ValueError(
            "send 16 kHz mono 16-bit WAV, not %d Hz, %d channel(s), %d-bit"
            % (rate, channels, width * 8)
        )
    pcm = array("h")
    pcm.frombytes(frames[: len(frames) - len(frames) % 2])
    if sys.byteorder == "big":
        pcm.byteswap()
    return array("f", (s / 32768.0 for s in pcm))


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #
_STREAMS: Dict[str, Stream] = {}
_REG_LOCK = threading.RLock()
_TIMER: Optional[threading.Timer] = None


def sweep(now: Optional[float] = None) -> List[str]:
    """Forget every stream idle for :data:`EXPIRE_SECONDS` (one mid-pass is
    never idle); returns the ids dropped."""
    stamp = _now() if now is None else now
    dropped: List[str] = []
    with _REG_LOCK:
        for sid, stream in list(_STREAMS.items()):
            if not stream.busy and stamp - stream.touched >= EXPIRE_SECONDS:
                _STREAMS.pop(sid, None)
                dropped.append(sid)
    return dropped


def open_stream(
    transcribe: Transcribe, accurate: Optional[Accurate] = None
) -> Optional[str]:
    """A new stream's opaque id, or ``None`` at :data:`MAX_STREAMS`;
    ``accurate`` adds the accurate pass at its end (see the module doc)."""
    with _REG_LOCK:
        sweep()
        if len(_STREAMS) >= MAX_STREAMS:
            return None
        sid = secrets.token_urlsafe(18)
        _STREAMS[sid] = Stream(transcribe, accurate)
        _arm_timer()
        return sid


def get_stream(sid: str) -> Optional[Stream]:
    """The live stream ``sid`` (``None`` when unknown or expired)."""
    with _REG_LOCK:
        sweep()
        return _STREAMS.get(sid)


def close_stream(sid: str) -> Optional[Stream]:
    """Forget ``sid`` → the stream it was (``None`` when unknown or expired)."""
    with _REG_LOCK:
        sweep()
        return _STREAMS.pop(sid, None)


def clear() -> None:
    """Forget every stream, the sweep timer and the finish-wait record
    (tests, shutdown)."""
    global _TIMER
    with _REG_LOCK:
        _STREAMS.clear()
        _WAITS.clear()
        if _TIMER is not None:
            _TIMER.cancel()
            _TIMER = None


def _arm_timer() -> None:
    """One daemon sweep timer while any stream is open, re-armed by its own
    firing, so an abandoned stream's audio is freed without a request."""
    global _TIMER
    with _REG_LOCK:
        if _TIMER is not None and _TIMER.is_alive():
            return
        timer = threading.Timer(EXPIRE_SECONDS / 2, _timer_fire)
        timer.daemon = True
        timer.start()
        _TIMER = timer


def _timer_fire() -> None:
    global _TIMER
    with _REG_LOCK:
        _TIMER = None
    try:
        sweep()
    finally:
        with _REG_LOCK:
            if _STREAMS:
                _arm_timer()
