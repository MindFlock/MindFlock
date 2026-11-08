"""Streaming recognition: the words appear while the person speaks
(``POST /api/voice/stream*``, ``routes/speech.py``; docs/web-api.md).

Whisper is not a streaming model, so this is the rolling window with
committed words — the "LocalAgreement-2" policy of whisper_streaming. A
:class:`Stream` holds the audio since (just before) the last committed word;
every :meth:`Stream.feed` appends a chunk and re-transcribes the whole
window; the words two consecutive passes agree on (the longest common
prefix of their word lists, compared lower-cased without punctuation) become
committed, and the unstable tail waits for the next pass (or the last
:data:`AGREE_PASSES` passes). Clients type only committed words — a
terminal cannot be corrected — so a committed word is never revisited: the
next pass drops what it re-hears of them (by matching up to five trailing
committed words near its start, then by timestamp: whisper's word times
move by 0.1–0.3 s from pass to pass). Whisper's ``initial_prompt`` is the
committed text BEFORE the window only: given the words it is about to hear
again, whisper skips them and stretches the next word back over them
(:data:`PROMPT_IN_WINDOW`).

Bounded work, because a pass's cost grows with the audio it decodes: a
pass hears only the uncommitted tail plus about :data:`REHEAR_SECONDS` of
the committed words before it (whole words: the cut is at a committed
word's end), so the committed text is decoded again only as context — once
the window holds more than :data:`PRUNE_SECONDS`, the audio before that is
dropped, and never audio under an uncommitted word (less
:data:`MARGIN_SECONDS`). A window with no words in it at all keeps only its
last second. And a pass never decodes more than :data:`WINDOW_SECONDS`:
the one forced commit is at that edge — before a pass, the tail words that
start before the last :data:`WINDOW_SECONDS` of audio (a tail that never
settled) are committed as they leave, and the audio before the edge goes.

Silence costs nothing: a chunk whose RMS is under the stream's quiet
level (:data:`SILENCE_RMS`, or :data:`FLOOR_FACTOR` times the room's noise
floor when that is more — :meth:`Stream.quiet_rms`; a browser's gain control
lifts the room to well over :data:`SILENCE_RMS` once the person stops, and
whisper wrote "I'm not listening." into that after "Stop listening."),
appended to a window with no uncommitted tail, runs no pass (``last_ms`` is
0) — there is nothing for a pass to settle and nothing new to hear. The
audio is kept, so a soft start of speech is decoded with the next chunk.
Silence also hears nothing: whisper fills a quiet stretch after the speech
with a word ("Never.", "for.", or one of the hint words), so a word a pass
places more than :data:`SILENCE_SLACK` after the last loud 100 ms frame is
dropped — an audible word starts with a loud frame, so the last one is at
or after every real word's start. Nor does the edge of the audio: whisper
may end a pass by repeating the word before as a sliver ("that that.");
a pass's last word that does is dropped (:data:`ECHO_SECONDS`).

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

The accurate pass starts EARLY. The pass is ~0.5–1 s on ``small``, and a
client that acts on a radio word waits for it. Once
:data:`EARLY_QUIET_SECONDS` of quiet follow the last loud frame the person
has most likely finished — or, sooner, once a fast pass hears the words end
in a send, end or stop phrase ("…, over": ``voice_words.control_kind``) with
:data:`RADIO_QUIET_SECONDS` of quiet after it — :meth:`Stream.feed` starts
the accurate pass on the phrase so far right then (``_start_early``, one per
stream at a time), while the client waits out its own phrase-end silence.
:meth:`Stream.finish` takes that result when no speech arrived after it
started (``final_early``; ``final_wait_ms`` is how long it still had to
wait, often 0) and starts a fresh pass otherwise. Speech after it makes it
stale: withdrawn if it has not started yet; a running one is left to finish
(nothing stops a CTranslate2 call part-way) and still serves ``finish``
when the fast tier heard no new word after it (a cough, a click).

The two tiers share the CPU, so they are scheduled (:class:`Lane`, one per
model: one job at a time, the accurate pass first, a stale job dropped
before it runs — never two accurate passes at once: a stale or timed-out
one that is running keeps its lane until it returns, and the next queues
behind it rather than splitting the cores with it), and once a phrase's
accurate pass has started no fast
pass runs for it once one has heard :data:`SETTLE_SECONDS` of the quiet
after the last loud frame (the pass that completes the last word): the
fast words are only the fallback now, and the tail stands as last heard —
so a quiet chunk then runs no pass, and neither does ``finish``. Likewise
``finish`` skips its last fast pass when nothing loud arrived since the
previous one and no tail waits: there is nothing new to hear.

The measured wait of recent finishes is kept (:func:`final_stats`) so
``GET /api/voice`` can tell a client how long ``/end`` typically takes —
the waits of passes that returned; a timed-out one is counted apart, never
as its deadline. Every finish that ran the accurate pass logs one INFO line
of its timings (fast passes, the final, the wait) to mine p50/p95 from.

Pure: a stream is built on a ``transcribe(audio, prompt)`` callable
(``core.speech.LocalSTT.stream_transcriber`` in the server, a scripted fake
in tests) that takes the float32 samples at 16 kHz (an ``array('f')``) and
the last ~200 characters of the committed text before the window (Whisper's
``initial_prompt``) and returns ``[(word, start_s, end_s)]`` relative to the start of the
samples. No numpy here — the server process does not have it unless the
local models are installed.

:meth:`Stream.turn` is the end-of-turn verdict for the words so far
(``core.speech_turn``): how long the quiet after the last loud frame must
last before the phrase is over, given what was said.

The registry (:func:`open_stream` …) holds at most :data:`MAX_STREAMS`,
and at most :data:`CLIENT_STREAMS` of one client (opening past that
replaces that client's oldest); one expires :data:`EXPIRE_SECONDS` after
its last chunk, swept on every
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

from backend.web.core import speech_turn as _turn
from backend.web.core import voice_words as _voice_words

__all__ = [
    "CLIENT_STREAMS",
    "CLIP",
    "EARLY_QUIET_SECONDS",
    "EXPIRE_SECONDS",
    "FAST",
    "FINAL",
    "FINAL_EXPECT_MS",
    "FINAL_MIN_SECONDS",
    "FINAL_TIMEOUT_S",
    "Job",
    "Lane",
    "MAX_STREAMS",
    "PHRASE_MAX_SECONDS",
    "RADIO_QUIET_SECONDS",
    "RATE",
    "SILENCE_RMS",
    "SILENCE_SLACK",
    "Skipped",
    "Stream",
    "WINDOW_SECONDS",
    "clear",
    "close_stream",
    "decode_wav",
    "final_stats",
    "get_stream",
    "lane",
    "open_stream",
    "same_text",
    "sweep",
]

_logger = logging.getLogger(__name__)

#: The sample rate of everything a stream holds (Whisper's).
RATE = 16000
#: Prune the held audio once it is longer than this.
PRUNE_SECONDS = 1.5
#: The committed audio a pass hears again before the uncommitted tail: the
#: window is cut at the end of the last committed word that ends at least
#: this long before the last one does. Context for the words at the seam;
#: the re-heard words are dropped by matching, then by time (``_pass``). It
#: was "everything since 0.3 s before the last committed end, once over
#: 4 s" — up to 4 s of committed audio decoded again every pass.
REHEAR_SECONDS = 1.0
#: Audio kept before an uncommitted word's start when pruning.
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
#: The room's noise floor, learnt per stream: the :data:`FLOOR_PERCENTILE`
#: of its 100 ms frames' RMS over the last :data:`FLOOR_FRAMES` (3 s: a
#: browser's gain control lifts the room within seconds of the person
#: stopping, so the floor follows it), once there
#: are :data:`FLOOR_MIN_FRAMES`. A frame is loud above :data:`FLOOR_FACTOR`
#: times it (6 dB; speech is 15–30 dB over a room), never above
#: :data:`FLOOR_MAX_RMS` (a soft voice stays loud in a loud room).
FLOOR_FRAMES = 30
FLOOR_MIN_FRAMES = 10
FLOOR_PERCENTILE = 0.1
FLOOR_FACTOR = 2.0
FLOOR_MAX_RMS = 0.03
#: After a stop or end phrase, words count only once a loud frame ends this
#: long after its last word (:meth:`Stream._after_control`).
CONTROL_GAP_SECONDS = 0.25
#: …and that frame is speech only within this many dB of the phrase's
#: speech: a browser's gain control lifts the room after the last word to
#: well over the floor it learnt while the person spoke.
CONTROL_SPEECH_DB = 12.0
_SPEECH_PERCENTILE = 0.9
#: The committed words a control phrase is looked for in.
_CONTROL_WORDS = 6
#: A pass's word repeated more than this many times running is a decoder
#: loop ("Stop. Stop. Stop. Stop."): the run keeps its first.
LOOP_RUN = 2
#: The ``initial_prompt``: this much of the committed text, from its end.
PROMPT_CHARS = 200
#: Whether the prompt also carries the committed words still in the window
#: (the old way). It must not: whisper reads a prompt that ends "… bot 7."
#: over audio that starts "bot 7 over and out" as already said, skips the
#: re-heard words and stretches the next one back to the window's start —
#: "over (0.00–1.06)" — where the time filter drops it as re-heard: "bot 7.
#: and out." (the bench: "submit", "to the speech", "this" lost the same
#: way). whisper_streaming prompts with the text before its buffer only.
PROMPT_IN_WINDOW = False
#: Whether a pass's re-heard words are found by their text first (the last
#: committed words in the window, matched anywhere near the pass's start;
#: ``_reheard``) and by time only when the text does not match. The old
#: order filtered by time first, and whisper's word times move 0.1–0.3 s
#: from pass to pass: "Merge (0.18–0.88)" committed, then "Merge (0.18–0.74)
#: mean (0.74–1.04)" lost "mean" as starting before 0.78.
MATCH_TEXT_FIRST = True
#: LocalAgreement-N: a word is committed once this many consecutive passes
#: agree on it (2: whisper_streaming's policy). 3 waits a pass longer
#: before a word is typed, for fewer wrong ones; the cycle-6 bench: fast WER
#: 13.8% against 14.5–17.8%, but the first word at 1628/2256 ms (p50/p90)
#: against 1204/1817 and no more control words heard (36 vs 38–39 of 48).
AGREE_PASSES = 2
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
#: Whisper's edge echo: at the end of the audio it heard, a pass may repeat
#: the word before as a sliver — "that (0.98–1.24) that. (1.24–1.32)",
#: "Work (1.92–2.32) work. (2.32–2.44)", "out. (1.40–1.52) out.
#: (1.52–1.68)" (the bench, 3 of 48 phrases) — and the next pass often
#: agrees. A pass's LAST word that repeats the one before it (in the pass,
#: or the last committed word) and lasts at most this long, starting within
#: :data:`_ECHO_GAP` of its end, is dropped (``_echo``). A real "no no" at
#: the very end loses its second word only in the fast text, if both are
#: that short; the accurate pass has it.
ECHO_SECONDS = 0.2
_ECHO_GAP = 0.1

#: The quiet that must follow the last loud frame before the accurate pass
#: starts early. The clients end a phrase without a radio word after 0.9 to
#: 1.5 s of quiet (``turn.hold_ms``, ``core.speech_turn``), so 0.3 s gives
#: the pass a 0.6 s or longer head start; a pause between words that starts
#: one costs nothing but the cores (the next word makes it stale, and it
#: keeps the accurate lane only until it returns). It was 0.5 s, which the
#: "over" path, ended by the clients after 100 to 250 ms, never reached.
EARLY_QUIET_SECONDS = 0.3
#: The quiet after the last loud frame that starts the accurate pass when
#: a fast pass heard the words end in a send, end or stop phrase
#: (:data:`_RADIO_KINDS`): one 100 ms frame, so the radio word's own last
#: syllable is in the audio. The desktop ends such a phrase after 100 ms of
#: quiet, the phone after 250 ms, the server's turn verdict after 350 ms.
RADIO_QUIET_SECONDS = 0.1
#: The ``voice_words.control_kind`` kinds that start the accurate pass at
#: once: those whose words the client sends (or keeps) and waits on.
_RADIO_KINDS = frozenset({"send", "end", "stop"})
#: How far past the last loud frame a fast pass must have heard before no
#: more are run once the accurate pass started (``Stream._all_heard``): the
#: bench's fast-tier WER went 28% → 36% when the pass over the speech's own
#: last chunk had to stand.
SETTLE_SECONDS = 0.3
#: ``expect_ms`` is the median finish wait plus this margin (or twice the
#: median absolute deviation, when larger), held to
#: [:data:`EXPECT_FLOOR_MS`, the timeout]. Not the p95: the waits that hit
#: the timeout pin a p95 at the timeout, and the client's "over" wait with it.
EXPECT_MARGIN_MS = 250
EXPECT_FLOOR_MS = 300

#: Open streams per server (``POST /api/voice/stream`` refuses beyond).
MAX_STREAMS = 4
#: Open streams per client (:func:`open_stream`'s ``client``): the one it
#: is about to end and the next phrase's, which the phone opens before the
#: ``/end`` of the last ("carry"). A client opening past this replaces its
#: own least recently fed stream instead of being refused.
CLIENT_STREAMS = 2
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


def _echo(before: Word, word: Word) -> bool:
    """Whether ``word`` is whisper's edge echo of ``before`` (see
    :data:`ECHO_SECONDS`): the same word, a sliver, right after it."""
    return (
        _norm(word[0]) == _norm(before[0])
        and word[2] - word[1] <= ECHO_SECONDS
        and word[1] - before[2] <= _ECHO_GAP
    )


def _quiet(samples: Sequence[float], rms: float = SILENCE_RMS) -> bool:
    """Whether a chunk is silence: its RMS is under ``rms`` (an empty chunk
    is quiet)."""
    n = len(samples)
    if not n:
        return True
    return sum(x * x for x in samples) / n < rms * rms


def _rms(samples: Sequence[float]) -> float:
    n = len(samples)
    return (sum(x * x for x in samples) / n) ** 0.5 if n else 0.0


def _unloop(hyp: list) -> list:
    """``hyp`` with any run of the same word longer than :data:`LOOP_RUN`
    cut to its first (a looping decoder), the rest kept in order."""
    out: list = []
    i = 0
    while i < len(hyp):
        j = i + 1
        key = _norm(hyp[i][0])
        while key and j < len(hyp) and _norm(hyp[j][0]) == key:
            j += 1
        out.extend(hyp[i : i + 1] if j - i > LOOP_RUN else hyp[i:j])
        i = j
    return out


def _voiced(samples: Sequence[float]) -> float:
    """The seconds of ``samples`` in 100 ms frames that are not quiet."""
    return _frames(samples)[0] / RATE


def _frames(samples: Sequence[float], rms: float = SILENCE_RMS) -> Tuple[int, int]:
    """``(loud samples, the end of the last loud frame)`` of ``samples`` in
    100 ms frames (loud: RMS ``rms`` or more); the end is -1 when none is
    loud."""
    loud, last = 0, -1
    for i in range(0, len(samples), _FRAME):
        frame = samples[i : i + _FRAME]
        if not _quiet(frame, rms):
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


# --------------------------------------------------------------------------- #
# The per-model job lanes
# --------------------------------------------------------------------------- #
#: Job kinds, in the order a lane serves them: a stream's accurate pass (a
#: client waits on it to send), a one-shot clip or wake check, a fast pass.
FINAL, CLIP, FAST = "final", "clip", "fast"
_RANK = {FINAL: 0, CLIP: 1, FAST: 2}
#: How long a job of each kind waits for its turn before it gives up
#: (:class:`Skipped`, "the model stayed busy"): a feed holds its stream and
#: a server thread while it waits, so no wait is unbounded. A fast pass
#: that gives up leaves its audio unheard for the next one; nobody waits
#: on an accurate pass past :data:`FINAL_TIMEOUT_S` after ``/end``; a clip
#: or wake check is a request the client times out on anyway.
LANE_WAIT_S = {FINAL: 4.0, CLIP: 15.0, FAST: 3.0}
#: A job still running this long after it started is presumed stuck (a
#: model call that never returns): the next waiter abandons it, as
#: :meth:`Lane.cancel` does, so one hung call cannot hold a model forever.
LANE_STUCK_S = 60.0


class Skipped(Exception):
    """A lane job that never ran: a newer one of its owner replaced it, its
    owner's accurate pass pre-empted it, or its waiter withdrew it."""


class Job:
    """One queued model call: ``state`` is ``queued`` → ``running`` →
    ``done``, or ``dropped`` (with ``why``) before it ran; ``abandoned``
    when its waiter gave up on it while it ran (:meth:`Lane.cancel`)."""

    __slots__ = ("kind", "owner", "state", "why", "abandoned", "started")

    def __init__(self, kind: str, owner: object):
        self.kind, self.owner = kind, owner
        self.state, self.why = "queued", ""
        self.abandoned = False
        self.started = 0.0  # time.monotonic() when it began to run


_CURRENT = threading.local()


def abandoned() -> bool:
    """Whether the lane job running in this thread was abandoned: a model
    call that can stop part-way (between faster-whisper's segments) checks
    it and stops — its result is no longer wanted."""
    job = getattr(_CURRENT, "job", None)
    return bool(job is not None and job.abandoned)


class Lane:
    """The calls on one model (``whisper:<name>``): at most one runs at a
    time, in its caller's own thread (no worker thread, nothing to stop).

    A CTranslate2 model on the CPU cannot be stopped mid-call, and two calls
    at once only split the cores between them (the early accurate pass
    beside fast passes doubled both, so the final missed its deadline), so
    the only scheduling there is happens before a job starts:

    * the queue is ordered by kind — :data:`FINAL`, then :data:`CLIP`, then
      :data:`FAST` — first come first served within one;
    * a job of an owner (a :class:`Stream`) replaces that owner's queued job
      of the same kind: the latest audio wins, the stale job raises
      :class:`Skipped` without running;
    * a :data:`FINAL` also drops its owner's queued :data:`FAST` jobs — once
      the accurate pass is on its way, the fast words are only a fallback;
    * :meth:`cancel` withdraws a queued job (a finish that stopped waiting,
      an early pass the speech made stale), and ABANDONS a running one: the
      call stops at its next check of :func:`abandoned`, if it makes one.
      With ``release`` the lane is free for the next job at once; without
      it (the accurate passes: ``_Early``) the abandoned call keeps the
      lane until it returns. A CTranslate2 call on a short phrase makes no
      check — it is one segment — so a released lane only ran the next
      pass beside it, both at half speed (the audit's 99 logged "accurate
      pass took over 1.5s"); the accurate pass's decoding is bounded
      instead (``core.speech.FINAL_TOKENS``, temperature 0), so the wait
      behind one is short;
    * no wait is unbounded: a job gives up after :data:`LANE_WAIT_S` of its
      kind (withdrawn, :class:`Skipped`), and a running job older than
      :data:`LANE_STUCK_S` is abandoned by the next waiter."""

    def __init__(self, name: str):
        self.name = name
        self._cond = threading.Condition()
        self._queue: List[Job] = []
        self._running: Optional[Job] = None
        #: Jobs run / dropped before they ran, since the lane was made.
        self.ran = 0
        self.dropped = 0

    @property
    def running(self) -> Optional[Job]:
        return self._running

    def pending(self) -> List[Job]:
        """The queued jobs, in the order they will run."""
        with self._cond:
            return list(self._queue)

    def submit(self, kind: str = FAST, owner: object = None) -> Job:
        """Queue a job (see the class doc for what it replaces) → its
        handle, for :meth:`run_job` from any thread."""
        job = Job(kind, owner)
        with self._cond:
            if owner is not None:
                for old in list(self._queue):
                    if old.owner is not owner:
                        continue
                    if old.kind == kind:
                        self._drop(old, "replaced by a newer %s job" % kind)
                    elif kind == FINAL and old.kind == FAST:
                        self._drop(old, "pre-empted by the accurate pass")
            rank = _RANK.get(kind, _RANK[FAST])
            at = len(self._queue)
            while at and _RANK.get(self._queue[at - 1].kind, 2) > rank:
                at -= 1
            self._queue.insert(at, job)
            self._cond.notify_all()
        return job

    def run_job(
        self, job: Job, fn: Callable[[], object], wait: Optional[float] = None
    ) -> object:
        """Wait for ``job``'s turn — at most ``wait`` seconds (default
        :data:`LANE_WAIT_S` of its kind) — then ``fn()`` in this thread →
        its result. :class:`Skipped` when the job was dropped before it ran
        or its wait ran out (it is withdrawn then)."""
        limit = LANE_WAIT_S.get(job.kind, LANE_WAIT_S[FAST]) if wait is None else wait
        deadline = time.monotonic() + max(0.0, limit)
        with self._cond:
            while job.state == "queued" and not (
                self._running is None and self._queue and self._queue[0] is job
            ):
                now = time.monotonic()
                self._unstick(now)
                if now >= deadline:
                    self._drop(job, "waited %.1fs: the model stayed busy" % limit)
                    self._cond.notify_all()
                    break
                left = deadline - now
                if self._running is not None:
                    left = min(left, self._running.started + LANE_STUCK_S - now)
                self._cond.wait(max(0.01, left))
            if job.state != "queued":
                raise Skipped(job.why)
            self._queue.pop(0)
            job.state = "running"
            job.started = time.monotonic()
            self._running = job
        outer = getattr(_CURRENT, "job", None)
        _CURRENT.job = job
        try:
            return fn()
        finally:
            _CURRENT.job = outer
            with self._cond:
                job.state = "done"
                if self._running is job:
                    self._running = None
                self.ran += 1
                self._cond.notify_all()

    def run(self, fn: Callable[[], object], kind: str = FAST, owner: object = None):
        """:meth:`submit` then :meth:`run_job`."""
        return self.run_job(self.submit(kind, owner), fn)

    def cancel(self, job: Job, why: str = "withdrawn", release: bool = True) -> bool:
        """Drop ``job`` if it has not started → ``True``; abandon it if it
        is running (see the class doc; ``release``: free the lane now, not
        when the call returns) → ``False``."""
        with self._cond:
            if job.state == "running" and not job.abandoned:
                job.abandoned, job.why = True, why
                if release and self._running is job:
                    self._running = None
                self._cond.notify_all()
            if job.state != "queued":
                return False
            self._drop(job, why)
            self._cond.notify_all()
            return True

    def _unstick(self, now: float) -> None:
        """Abandon the running job when it has run :data:`LANE_STUCK_S`
        (under the condition)."""
        job = self._running
        if job is None or now - job.started < LANE_STUCK_S:
            return
        _logger.warning(
            "voice lane %s: a %s job has run %.0fs; abandoned so the model is free",
            self.name,
            job.kind,
            now - job.started,
        )
        job.abandoned, job.why = True, "ran over %.0fs" % LANE_STUCK_S
        self._running = None
        self._cond.notify_all()

    def _drop(self, job: Job, why: str) -> None:
        if job in self._queue:
            self._queue.remove(job)
        job.state, job.why = "dropped", why
        self.dropped += 1


_LANES: Dict[str, Lane] = {}
_LANES_LOCK = threading.Lock()


def lane(name: str) -> Lane:
    """The process-wide lane of model ``name`` (made on first use). A
    transcriber names its model's lane in its ``lane`` attribute
    (``core.speech.LocalSTT``); one without uses ``fast`` / ``final``."""
    with _LANES_LOCK:
        found = _LANES.get(name)
        if found is None:
            found = _LANES[name] = Lane(name)
        return found


def _lane_of(fn: Optional[Callable], default: str) -> Lane:
    return lane(str(getattr(fn, "lane", "") or default))


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
        self._levels: collections.deque = collections.deque(maxlen=FLOOR_FRAMES)
        # (stream time of a 100 ms frame's end, its RMS), the last 10 s.
        self._log: collections.deque = collections.deque(maxlen=100)
        self._heard_end = 0.0  # stream time the last fast pass heard up to
        self._committed: List[Word] = []  # stream-absolute times
        self._tail: List[Word] = []  # the last pass's uncommitted words
        # The passes before it, oldest first (AGREE_PASSES > 2).
        self._older: List[List[Word]] = []
        self._owed: List[Word] = []  # committed, not yet returned (a pass failed)
        self.last_ms = 0
        #: Seconds of audio the last feed's pass decoded (0 when it ran none).
        self.last_window_s = 0.0
        #: Every fast pass's ms this phrase (the finish's timing log).
        self.pass_ms: List[int] = []
        #: How the last finish's accurate pass ended: ``ok``, ``timeout``,
        #: ``skipped``, ``error`` or ``""`` (none ran).
        self.final_outcome = ""
        self.touched = _now()
        #: Who opened it (:func:`open_stream`), ``""`` when unknown.
        self.client = ""
        #: The person's voice words (``voice_words.effective``; None: the
        #: defaults), for :meth:`_after_control`; the route sets them.
        self.words: Optional[dict] = None

    def quiet_rms(self) -> float:
        """The RMS under which a frame is quiet in this stream: the room's
        noise floor times :data:`FLOOR_FACTOR` (at most
        :data:`FLOOR_MAX_RMS`) or :data:`SILENCE_RMS`, whichever is more."""
        if len(self._levels) < FLOOR_MIN_FRAMES:
            return SILENCE_RMS
        levels = sorted(self._levels)
        floor = levels[int(FLOOR_PERCENTILE * (len(levels) - 1))]
        return max(SILENCE_RMS, min(FLOOR_MAX_RMS, FLOOR_FACTOR * floor))

    def _learn(self, samples: Sequence[float]) -> None:
        """Note the RMS of each whole 100 ms frame of ``samples``."""
        for i in range(0, len(samples) - _FRAME + 1, _FRAME):
            level = _rms(samples[i : i + _FRAME])
            self._levels.append(level)
            self._log.append((self._fed + (i + _FRAME) / RATE, level))

    def _spoke_after(self, t: float) -> bool:
        """Whether a frame ending after stream time ``t`` was speech: louder
        than :meth:`quiet_rms` and within :data:`CONTROL_SPEECH_DB` of the
        phrase's speech (the :data:`_SPEECH_PERCENTILE` of the frames kept)."""
        if self._loud_end <= t:
            return False
        levels = sorted(level for _end, level in self._log)
        if not levels:
            return True
        speech = levels[int(_SPEECH_PERCENTILE * (len(levels) - 1))]
        need = max(self.quiet_rms(), speech * 10 ** (-CONTROL_SPEECH_DB / 20))
        return any(end > t and level >= need for end, level in self._log)

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
            self._learn(samples)
            quiet = _quiet(samples, self.quiet_rms())
            self._audio.extend(samples)
            self._keep(samples)
            self._mark(samples)
            self.last_ms, self.last_window_s = 0, 0.0
            if not self._audio:
                return "", ""
            self._cap()
            if quiet:
                self._start_early(RADIO_QUIET_SECONDS if self._radio_end() else None)
            else:
                self._unheard = True
                self._stale_early()
            if quiet and (not self._tail or self._settled()):
                return self._pay([]), _join(self._tail)
            hyp = self._pass()
            if hyp is None:  # dropped from the lane: the audio waits
                return self._pay([]), _join(self._tail)
            n = self._agreed(hyp)
            new = hyp[:n]
            self._committed.extend(new)
            self._remember(n)
            self._tail = hyp[n:]
            self._prune()
            if self._radio_end():
                self._start_early(RADIO_QUIET_SECONDS)
            self.touched = _now()
            return self._pay(new), _join(self._tail)

    def turn(self, words: object = None) -> dict:
        """The end-of-turn verdict for the phrase so far → ``{"p",
        "hold_ms", "reason"}`` (``core.speech_turn.score``): the committed
        words and the unstable tail, the quiet since the last loud frame,
        and whether a fast pass has heard every loud frame. ``words``: the
        effective voice words (None = the defaults)."""
        with self._lock:
            text = _join(self._committed + self._tail)
            quiet = self._fed - self._loud_end if self._loud_end >= 0 else 0.0
            heard = not self._unheard
        return _turn.score(text, max(0.0, quiet) * 1000, heard, words).json()

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
            phrase, self._phrase = self._phrase, array("f")
            early, self._early = self._early, None
            run = self._accurate is not None and phrase
            run = run and not self.phrase_trimmed
            run = run and self.voiced_s >= FINAL_MIN_SECONDS
            job: Optional[_Early] = None
            if run and not self._covers(early):
                # The accurate pass first: the fast flush is only its fallback.
                job = self._fresh(phrase, early)
            if run and self._all_heard():
                # Every loud frame was heard by a fast pass and the accurate
                # pass is on its way: the tail stands as heard, no flush.
                rest: Optional[List[Word]] = list(self._tail)
            else:
                fresh = self._audio and (self._tail or self._unheard)
                try:
                    rest = self._pass() if fresh else []
                except BaseException:
                    if job is not None:
                        job.cancel("the fast flush failed")
                    raise
                if rest is None:
                    rest = list(self._tail)
            self._committed.extend(rest)
            self._tail, self._older = [], []
            self._offset += self.held_seconds
            self._audio = array("f")
            out: dict = {"committed": self._pay(rest), "partial": ""}
            fast = self.text
            if run and job is None and not self._covers(early):
                # The flush heard a word the stale pass's audio has not.
                job = self._fresh(phrase, early)
        self.final_early = False
        self.final_outcome = ""
        if not run:
            final = ""
        elif job is None:
            final = self._await(early)  # type: ignore[arg-type]
        else:
            final = self._final(job)
        if run:
            _record(self.final_wait_ms, self.final_outcome)
            self._log_timing(len(phrase) / RATE)
        self.pass_ms = []
        if final:
            out.update(
                final=final,
                final_ms=self.final_ms,
                final_wait_ms=self.final_wait_ms,
                final_early=self.final_early,
                same=same_text(final, fast),
            )
        return out

    def _fresh(self, phrase: array, early: Optional["_Early"]) -> "_Early":
        """A new accurate pass over ``phrase`` for :meth:`finish`; the early
        one it replaces is withdrawn if it has not started (a running one
        keeps the lane: the new one queues behind it)."""
        if early is not None:
            early.withdraw("a fresh pass replaced it")
        return _Early(self._accurate, phrase, owner=self)  # type: ignore[arg-type]

    def _covers(self, early: Optional["_Early"]) -> bool:
        """Whether the early pass ``early`` serves :meth:`finish`: it is not
        stale, or it went stale yet the fast tier has heard no word since
        (the words now are the words then: what made it stale was a cough,
        a click, the room) and it was not withdrawn before it ran."""
        if early is None:
            return False
        if not early.stale:
            return True
        if early.heard is None or early.job.state == "dropped":
            return False
        return self._heard_words() == early.heard

    def _heard_words(self) -> List[str]:
        return [_norm(w[0]) for w in self._committed + self._tail]

    def _log_timing(self, seconds: float) -> None:
        """One INFO line per finish that ran the accurate pass: the
        phrase's fast passes and the final, to mine p50/p95 from the log."""
        ms = sorted(self.pass_ms)
        _logger.info(
            "voice stream timing: phrase %.1fs, %d fast passes (p50 %d, max %d ms),"
            " final %s %d ms, waited %d ms%s",
            seconds,
            len(ms),
            ms[len(ms) // 2] if ms else 0,
            ms[-1] if ms else 0,
            self.final_outcome or "none",
            self.final_ms,
            self.final_wait_ms,
            ", early" if self.final_early else "",
        )

    # -- internals (called under the lock) -------------------------------- #
    def _mark(self, samples: Sequence[float]) -> None:
        """Note where the chunk's last loud frame ends, in stream time."""
        _loud, last = _frames(samples, self.quiet_rms())
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

    def _start_early(self, quiet: Optional[float] = None) -> None:
        """Start the accurate pass on the phrase so far (a copy) once
        ``quiet`` seconds (default :data:`EARLY_QUIET_SECONDS`; a quiet
        chunk, or :data:`RADIO_QUIET_SECONDS` after a pass heard a radio
        word: :meth:`_radio_end`) follow the last loud frame — unless one
        that is still good is queued, running or done, the phrase is too
        short or too long for a pass, or there is none. A stale one still
        running keeps the lane; the new one queues behind it. Under the
        lock; the pass runs on its own daemon thread."""
        if self._accurate is None or self.phrase_trimmed:
            return
        if self.voiced_s < FINAL_MIN_SECONDS or self._loud_end < 0:
            return
        need = EARLY_QUIET_SECONDS if quiet is None else quiet
        if self._fed - self._loud_end < need - 1e-6:  # float stream times
            return
        if self._early is not None and not self._early.stale:
            return
        if self._early is not None:
            self._early.withdraw("a newer early pass replaced it")
        self._early = _Early(self._accurate, array("f", self._phrase), owner=self)

    def _stale_early(self) -> None:
        """Speech arrived after the early pass started: its text may not
        cover the phrase. Withdrawn if it has not started yet; a running
        one is left to finish (it keeps its lane: see :class:`Lane`) with
        the words heard so far noted — :meth:`finish` still takes it when
        no new word follows (:meth:`_covers`)."""
        if self._early is not None and not self._early.stale:
            self._early.stale = True
            self._early.heard = self._heard_words()
            self._early.withdraw("the speech went on")

    def _radio_end(self) -> bool:
        """Whether the words so far end in a send, end or stop phrase
        (:data:`_RADIO_KINDS`, ``voice_words.control_kind`` with
        :attr:`words`): the person is done, and the client will act on it
        within 100–350 ms — the accurate pass should start now."""
        if self._accurate is None or not (self._tail or self._committed):
            return False
        said = (self._committed + self._tail)[-_CONTROL_WORDS:]
        try:
            kind = _voice_words.control_kind(_join(said), self.words)
        except Exception:  # noqa: BLE001 — a hint, never a failure
            return False
        return kind in _RADIO_KINDS

    def _all_heard(self) -> bool:
        """A fast pass heard every loud frame so far and
        :data:`SETTLE_SECONDS` of the quiet after the last: whisper drops or
        clips a word that ends right at the window's edge, so the pass that
        hears it with some quiet after is the one worth having."""
        settled = self._loud_end + SETTLE_SECONDS <= self._heard_end
        return settled and not self._unheard

    def _settled(self) -> bool:
        """The accurate pass over the phrase as it is now has started, and
        the fast tier heard all of it: no fast pass is issued after that
        (it would only re-agree on the tail, on the cores the final needs)."""
        return self._early is not None and not self._early.stale and self._all_heard()

    def _await(self, early: "_Early", wait: Optional[float] = None) -> str:
        """The early pass's text, waited for at most ``wait`` seconds more
        (default :data:`FINAL_TIMEOUT_S`) → ``""`` when it failed or timed
        out (logged). Not under the lock. Sets ``final_ms`` (its own run
        time once it ran, else its whole age), ``final_wait_ms`` and
        ``final_early``."""
        started = time.monotonic()
        self._wait_cap = FINAL_TIMEOUT_S if wait is None else wait
        early.thread.join(self._wait_cap)
        self.final_wait_ms = int((time.monotonic() - started) * 1000)
        self.final_ms = early.ms()
        self.final_early = True
        return self._outcome(early)

    def _final(self, job: "_Early") -> str:
        """The accurate pass ``finish`` started (``job``), waited for at most
        :data:`FINAL_TIMEOUT_S` from its start → its text, ``""`` when it
        failed or timed out (logged; a late result is dropped). Not under
        the lock."""
        self._wait_cap = FINAL_TIMEOUT_S
        job.thread.join(max(0.0, FINAL_TIMEOUT_S - (time.monotonic() - job.started)))
        self.final_wait_ms = int((time.monotonic() - job.started) * 1000)
        self.final_ms = job.ms()
        return self._outcome(job)

    def _outcome(self, early: "_Early") -> str:
        """``early``'s text, or ``""`` with :attr:`final_outcome` saying why
        (logged): ``timeout`` (still queued or running: withdrawn, or
        abandoned keeping its lane), ``skipped``, ``error``."""
        if early.thread.is_alive():
            queued = early.cancel("its finish stopped waiting")
            self.final_outcome = "timeout"
            _logger.warning(
                "voice stream: no accurate pass within %.1fs of the end on %.1fs"
                " of audio (%s, %s; %d ms since it was asked for); the fast text"
                " stands",
                getattr(self, "_wait_cap", FINAL_TIMEOUT_S),
                early.seconds,
                "the early one" if self.final_early else "a fresh one",
                "never started: the model was busy" if queued else "still running",
                int((time.monotonic() - early.started) * 1000),
            )
            return ""
        if "skipped" in early.box:
            self.final_outcome = "skipped"
            _logger.info(
                "voice stream: the accurate pass was dropped (%s); the fast text stands",
                early.box["skipped"],
            )
            return ""
        if "error" in early.box:
            self.final_outcome = "error"
            _logger.warning(
                "voice stream: the accurate pass failed (%s); the fast text stands",
                early.box["error"],
            )
            return ""
        self.final_outcome = "ok"
        return str(early.box.get("text") or "").strip()

    def _agreed(self, hyp: List[Word]) -> int:
        """How many of ``hyp``'s opening words the previous
        :data:`AGREE_PASSES` − 1 passes agree on (the words to commit)."""
        passes = ([self._tail] + self._older[::-1])[: max(1, AGREE_PASSES - 1)]
        if len(passes) < AGREE_PASSES - 1:
            return 0
        n = 0
        while all(
            n < len(hyp) and n < len(p) and _norm(hyp[n][0]) == _norm(p[n][0])
            for p in passes
        ):
            n += 1
        return n

    def _remember(self, n: int) -> None:
        """Keep the last pass for the next agreement (:data:`AGREE_PASSES`),
        less the ``n`` words just committed."""
        keep = max(0, AGREE_PASSES - 2)
        if not keep:
            self._older = []
            return
        older = [p[n:] for p in self._older] + [self._tail[n:]]
        self._older = older[-keep:]

    def _prompt(self) -> str:
        words = self._committed
        if not PROMPT_IN_WINDOW:
            # Only the words before the window: whisper skips what the
            # prompt says it heard (see PROMPT_IN_WINDOW).
            edge = self._offset + 0.01
            k = len(words)
            while k and words[k - 1][2] > edge:
                k -= 1
            words = words[:k]
        text = _join(words)
        if len(text) <= PROMPT_CHARS:
            return text
        cut = text[-PROMPT_CHARS:]
        space = cut.find(" ")
        return cut[space + 1 :] if 0 <= space < len(cut) - 1 else cut

    def _pass(self) -> Optional[List[Word]]:
        """Transcribe the window, on the fast model's lane (:class:`Lane`)
        → its words beyond what is committed, in stream-absolute seconds;
        ``None`` when the lane dropped the pass before it ran (``last_ms``
        0, the audio stays unheard)."""
        started = time.monotonic()
        self.last_window_s = self.held_seconds
        audio, prompt = self._audio, self._prompt()
        try:
            raw = _lane_of(self._transcribe, "fast").run(
                lambda: self._transcribe(audio, prompt), FAST, self
            )
        except Skipped:
            self.last_ms, self.last_window_s = 0, 0.0
            return None
        finally:
            if self.last_window_s:
                self.last_ms = int((time.monotonic() - started) * 1000)
                self.pass_ms.append(self.last_ms)
        self._unheard = False  # a failed pass leaves the audio unheard
        self._heard_end = self._fed
        hyp: List[Word] = []
        edge = self._loud_end + SILENCE_SLACK
        for word, start, end in raw or ():
            text = str(word or "").strip()
            if text and float(start) + self._offset <= edge:
                hyp.append(
                    (text, float(start) + self._offset, float(end) + self._offset)
                )
        hyp = _unloop(hyp)
        if len(hyp) >= 2 and _echo(hyp[-2], hyp[-1]):
            hyp.pop()
        return self._after_control(self._beyond(hyp))

    def _beyond(self, hyp: List[Word]) -> List[Word]:
        """``hyp`` less the committed words it hears again."""
        if not self._committed:
            return hyp
        last_end = self._committed[-1][2]
        k = self._reheard(hyp, last_end) if MATCH_TEXT_FIRST else 0
        if k:
            hyp = hyp[k:]
            if len(hyp) == 1 and _echo(self._committed[-1], hyp[0]):
                hyp = []
            return hyp
        hyp = [w for w in hyp if w[1] > last_end - _OVERLAP_SECONDS]
        if hyp and abs(hyp[0][1] - last_end) < 1.0:
            top = min(len(self._committed), len(hyp), _NGRAM)
            for k in range(top, 0, -1):
                said = [_norm(w[0]) for w in self._committed[-k:]]
                if said == [_norm(w[0]) for w in hyp[:k]]:
                    hyp = hyp[k:]
                    break
        if len(hyp) == 1 and _echo(self._committed[-1], hyp[0]):
            hyp = []
        return hyp

    def _after_control(self, hyp: List[Word]) -> List[Word]:
        """Nothing after a stop or end phrase ("stop listening", "over and
        out") unless the person spoke again: once the committed words end in
        one (``voice_words.control_kind``, :attr:`words`) — or the pass's
        own words up to some word do — the words after it stand only when
        speech (:meth:`_spoke_after`) ended more than
        :data:`CONTROL_GAP_SECONDS` after it. Whisper fills the room after
        "Stop listening." with "I'm not listening." / "in the", and a
        client reads a control phrase only at the very end."""
        if not hyp:
            return hyp
        said = list(self._committed[-_CONTROL_WORDS:])
        for i in range(-1, len(hyp) - 1):
            if i >= 0:
                said = (said + [hyp[i]])[-_CONTROL_WORDS:]
            if not said or self._spoke_after(said[-1][2] + CONTROL_GAP_SECONDS):
                continue
            try:
                kind = _voice_words.control_kind(_join(said), self.words)
            except Exception:  # noqa: BLE001 — a hint, never a failure
                kind = None
            if kind in ("stop", "end"):
                return hyp[: i + 1]
        return hyp

    def _reheard(self, hyp: List[Word], last_end: float) -> int:
        """How many of ``hyp``'s opening words are committed words heard
        again, found by their text: the longest run of the last
        :data:`_NGRAM` committed words that ``hyp`` says within its first
        few words, its last word centred before ``last_end`` (a new word
        that happens to repeat the last one starts after it) → the index
        after the run, 0 when none matches (the caller falls back to
        time)."""
        said = [_norm(w[0]) for w in self._committed[-_NGRAM:]]
        heard = [_norm(w[0]) for w in hyp]
        for k in range(min(len(said), len(heard)), 0, -1):
            tail = said[-k:]
            for j in range(0, min(len(heard) - k, _NGRAM) + 1):
                if heard[j : j + k] != tail:
                    continue
                last = hyp[j + k - 1]
                if (last[1] + last[2]) / 2 < last_end:
                    return j + k
        return 0

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
            self._older = []
        del self._audio[:drop]
        self._offset = edge

    def _prune(self) -> None:
        """Drop audio no uncommitted word needs once the window is over
        :data:`PRUNE_SECONDS`."""
        if self.held_seconds > PRUNE_SECONDS:
            self._cut()

    def _cut(self) -> None:
        end = self._offset + self.held_seconds
        cut = self._offset
        if self._committed:
            keep_from = self._committed[-1][2] - REHEAR_SECONDS
            for word in reversed(self._committed):
                if word[2] <= keep_from:
                    cut = word[2]
                    break
        if self._tail:
            cut = min(cut, self._tail[0][1] - MARGIN_SECONDS)
        else:
            cut = max(cut, end - SILENT_KEEP_SECONDS)
        drop = int((cut - self._offset) * RATE)
        if drop > 0:
            del self._audio[:drop]
            self._offset += drop / RATE


class _Early:
    """One accurate pass, queued on its model's lane (:class:`Lane`, as a
    :data:`FINAL` of ``owner``) and run on its own daemon thread: ``box``
    gets ``text``, ``error`` or ``skipped`` (the lane dropped it before it
    ran); ``stale`` is set when speech arrives after it started (its text
    may not cover the phrase), ``heard`` to the fast words then."""

    def __init__(self, accurate: Accurate, phrase: array, owner: object = None):
        self.box: dict = {}
        self.stale = False
        self.heard: Optional[List[str]] = None
        self.seconds = len(phrase) / RATE
        self.started = time.monotonic()
        self.ended = 0.0
        self.lane = _lane_of(accurate, "final")
        self.job = self.lane.submit(FINAL, owner)

        def run() -> None:
            try:
                self.box["text"] = self.lane.run_job(self.job, lambda: accurate(phrase))
            except Skipped as err:
                self.box["skipped"] = str(err) or "dropped"
            except Exception as err:  # noqa: BLE001 — the fast text stands
                self.box["error"] = err
            finally:
                self.ended = time.monotonic()

        self.thread = threading.Thread(target=run, name="voice-final", daemon=True)
        self.thread.start()

    @property
    def done(self) -> bool:
        return not self.thread.is_alive()

    def ms(self) -> int:
        """The pass's own run time (to now while it runs); its age when it
        never started."""
        began = self.job.started or self.started
        end = self.ended or time.monotonic()
        return int(max(0.0, end - began) * 1000)

    def withdraw(self, why: str) -> bool:
        """Drop the pass if it has not started → whether it was; a running
        one is left alone (its result may still serve)."""
        if self.job.state != "queued":
            return False
        return self.lane.cancel(self.job, why)

    def cancel(self, why: str) -> bool:
        """Withdraw the pass if it has not started → whether it was; else
        abandon it (it stops at its next segment), KEEPING its lane until it
        returns: two accurate passes never run at once (:class:`Lane`)."""
        return self.lane.cancel(self.job, why, release=False)


#: The waits of the last :data:`STATS_KEEP` accurate passes that returned.
_WAITS: "collections.deque[int]" = collections.deque(maxlen=STATS_KEEP)
#: Whether each of the last :data:`STATS_KEEP` finishes that asked for an
#: accurate pass timed out (``True``) or not.
_TIMEOUTS: "collections.deque[bool]" = collections.deque(maxlen=STATS_KEEP)


def _record(wait_ms: int, outcome: str = "ok") -> None:
    """Note one finish's accurate pass: its wait when it returned (``ok``,
    ``error``: a failure is quick), a timeout only as a count — recording
    the deadline as a wait pinned :func:`final_stats`'s median, and the
    clients' "over" wait with it, at the timeout. A ``skipped`` pass (the
    lane dropped it) records nothing but the miss."""
    _TIMEOUTS.append(outcome == "timeout")
    if outcome in ("ok", "error"):
        _WAITS.append(int(wait_ms))


def final_stats() -> dict:
    """How long :meth:`Stream.finish` waited for the accurate pass over the
    last :data:`STATS_KEEP` phrases whose pass returned → ``{"samples",
    "typical_ms" (the median), "p95_ms", "expect_ms", "timeout_ms",
    "timeouts" (how many of the last :data:`STATS_KEEP` finishes gave up on
    it: counted, never as a wait)}``; the two percentiles are 0 before the
    first. ``expect_ms`` is what a client should budget for
    ``/end``: once :data:`STATS_MIN` phrases are in, the median plus
    :data:`EXPECT_MARGIN_MS` or twice the median absolute deviation, whichever
    is more — robust, where the p95 sits at the timeout as soon as one wait
    in twenty hits it; the bench's :data:`FINAL_EXPECT_MS` before; held to
    [:data:`EXPECT_FLOOR_MS`, the timeout] either way. ``GET
    /api/voice`` serves it (``server_ready.final_ms`` is ``expect_ms``) so a
    client waits about that long instead of assuming the worst."""
    waits = sorted(_WAITS)
    n = len(waits)

    def pct(q: float) -> int:
        return waits[min(n - 1, int(round(q * (n - 1))))] if n else 0

    timeout = int(FINAL_TIMEOUT_S * 1000)
    if n >= STATS_MIN:
        med = pct(0.5)
        dev = sorted(abs(w - med) for w in waits)[n // 2]
        expect = med + max(EXPECT_MARGIN_MS, 2 * dev)
    else:
        expect = FINAL_EXPECT_MS
    return {
        "samples": n,
        "typical_ms": pct(0.5),
        "p95_ms": pct(0.95),
        "expect_ms": max(min(EXPECT_FLOOR_MS, timeout), min(expect, timeout)),
        "timeout_ms": timeout,
        "timeouts": sum(_TIMEOUTS),
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
    transcribe: Transcribe, accurate: Optional[Accurate] = None, client: str = ""
) -> Optional[str]:
    """A new stream's opaque id, or ``None`` at :data:`MAX_STREAMS`;
    ``accurate`` adds the accurate pass at its end (see the module doc).

    ``client`` names who opens it (one page: ``routes/speech.py`` derives
    it). A client never needs more than :data:`CLIENT_STREAMS` at once, so
    one opening past that has left one behind (a carried stream its page
    dropped, a reload): its least recently fed streams are closed to make
    room — never another client's, and never one mid-pass while an idle
    one is there to go first."""
    with _REG_LOCK:
        sweep()
        if client:
            mine = [(sid, st) for sid, st in _STREAMS.items() if st.client == client]
            mine.sort(key=lambda item: (item[1].busy, item[1].touched))
            while len(mine) >= CLIENT_STREAMS:
                old, _st = mine.pop(0)
                _STREAMS.pop(old, None)
                _logger.info("voice stream: a newer stream of its client replaced one")
        if len(_STREAMS) >= MAX_STREAMS:
            return None
        sid = secrets.token_urlsafe(18)
        stream = _STREAMS[sid] = Stream(transcribe, accurate)
        stream.client = client
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
    """Forget every stream, the model lanes, the sweep timer and the
    finish-wait record (tests, shutdown)."""
    global _TIMER
    with _LANES_LOCK:
        _LANES.clear()
    with _REG_LOCK:
        _STREAMS.clear()
        _WAITS.clear()
        _TIMEOUTS.clear()
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
