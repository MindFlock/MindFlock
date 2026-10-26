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

Bounded work: once the window holds more than :data:`PRUNE_SECONDS`, the
audio before the last committed word's end (less :data:`MARGIN_SECONDS`) is
dropped — never audio under an uncommitted word. A window with no words in
it at all keeps only its last second. The one forced commit: a window still
over :data:`MAX_HELD_SECONDS` after that (a tail that never settles) commits
the current pass's words that ended :data:`PRUNE_SECONDS` before the end of
the audio, so inference time cannot grow without limit.

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

import io
import secrets
import sys
import threading
import time
import wave
from array import array
from typing import Callable, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "EXPIRE_SECONDS",
    "MAX_STREAMS",
    "RATE",
    "Stream",
    "clear",
    "close_stream",
    "decode_wav",
    "get_stream",
    "open_stream",
    "sweep",
]

#: The sample rate of everything a stream holds (Whisper's).
RATE = 16000
#: Prune the held audio once it is longer than this.
PRUNE_SECONDS = 10.0
#: Audio kept before the end of the last committed word when pruning.
MARGIN_SECONDS = 0.3
#: A window with no words in it keeps only this much audio.
SILENT_KEEP_SECONDS = 1.0
#: Over this after a prune, the settled part of the tail is committed anyway.
MAX_HELD_SECONDS = 30.0
#: The ``initial_prompt``: this much of the committed text, from its end.
PROMPT_CHARS = 200
#: A re-heard word starting this long before the last committed end is old.
_OVERLAP_SECONDS = 0.1
#: How many trailing committed words a pass's opening words are matched to.
_NGRAM = 5

#: Open streams per server (``POST /api/voice/stream`` refuses beyond).
MAX_STREAMS = 4
#: A stream is forgotten this long after its last chunk.
EXPIRE_SECONDS = 60.0

Word = Tuple[str, float, float]
Transcribe = Callable[[array, str], Sequence[Tuple[str, float, float]]]


def _now() -> float:
    return time.monotonic()


def _norm(word: str) -> str:
    """The comparison form of a word: lower-case, punctuation stripped (a
    word that is all punctuation compares as itself)."""
    low = word.strip().lower()
    return "".join(ch for ch in low if ch.isalnum()) or low


def _join(words: Sequence[Word]) -> str:
    return " ".join(w[0] for w in words)


class Stream:
    """One live recognition: see the module doc for the policy. Thread-safe —
    one pass at a time (a chunk that arrives during a pass waits, then gets
    the state after its own pass)."""

    def __init__(self, transcribe: Transcribe):
        self._transcribe = transcribe
        self._lock = threading.Lock()
        self._audio = array("f")
        self._offset = 0.0  # stream seconds before _audio[0]
        self._committed: List[Word] = []  # stream-absolute times
        self._tail: List[Word] = []  # the last pass's uncommitted words
        self.last_ms = 0
        self.touched = _now()

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    @property
    def held_seconds(self) -> float:
        """The audio the window holds now, in seconds."""
        return len(self._audio) / RATE

    @property
    def text(self) -> str:
        """Everything committed so far."""
        return _join(self._committed)

    def feed(self, samples: Sequence[float]) -> Tuple[str, str]:
        """Append ``samples`` (float32 at 16 kHz) and run one pass →
        ``(newly committed words, the current unstable tail)``; either may
        be ``""``. A :class:`core.speech.SpeechError` from the transcriber
        propagates, with the chunk kept for the next pass."""
        with self._lock:
            self.touched = _now()
            self._audio.extend(samples)
            if not self._audio:
                return "", ""
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
            new = new + self._prune()
            self.touched = _now()
            return _join(new), _join(self._tail)

    def finish(self) -> str:
        """One last pass over what the window holds, all of it committed →
        the words not committed before (``""`` when there is no audio)."""
        with self._lock:
            self.touched = _now()
            rest = self._pass() if self._audio else []
            self._committed.extend(rest)
            self._tail = []
            self._offset += self.held_seconds
            self._audio = array("f")
            return _join(rest)

    # -- internals (called under the lock) -------------------------------- #
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
        try:
            raw = self._transcribe(self._audio, self._prompt())
        finally:
            self.last_ms = int((time.monotonic() - started) * 1000)
        hyp: List[Word] = []
        for word, start, end in raw or ():
            text = str(word or "").strip()
            if text:
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

    def _prune(self) -> List[Word]:
        """Drop audio no uncommitted word needs once the window is over
        :data:`PRUNE_SECONDS`; returns the words force-committed (see the
        module doc), usually none."""
        if self.held_seconds <= PRUNE_SECONDS:
            return []
        self._cut()
        forced: List[Word] = []
        if self.held_seconds > MAX_HELD_SECONDS:
            settled = self._offset + self.held_seconds - PRUNE_SECONDS
            while self._tail and self._tail[0][2] <= settled:
                forced.append(self._tail.pop(0))
            self._committed.extend(forced)
            if forced:
                self._cut()
        return forced

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


def open_stream(transcribe: Transcribe) -> Optional[str]:
    """A new stream's opaque id, or ``None`` at :data:`MAX_STREAMS`."""
    with _REG_LOCK:
        sweep()
        if len(_STREAMS) >= MAX_STREAMS:
            return None
        sid = secrets.token_urlsafe(18)
        _STREAMS[sid] = Stream(transcribe)
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
    """Forget every stream and the sweep timer (tests, shutdown)."""
    global _TIMER
    with _REG_LOCK:
        _STREAMS.clear()
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
