"""The end-of-turn bench: when would a client that ends a phrase on the
stream's ``turn.hold_ms`` (``backend/web/core/speech_turn.py``) end each
recorded phrase, against today's fixed 1100 ms of quiet?

Not a unit test (pytest never collects ``tests/bench/``). It needs the local
models' packages, so run it with the tool venv::

    PYTHONPATH=$PWD ~/.local/share/uv/tools/mindflock/bin/python \\
        tests/bench/turn_bench.py WAVDIR [--stretch 0,600,900] [--json OUT]
    # then other holds over the same replay, in seconds (no model runs):
    PYTHONPATH=$PWD ~/.local/share/uv/tools/mindflock/bin/python \\
        tests/bench/turn_bench.py WAVDIR --rescore OUT --hold sentence=900

``WAVDIR`` holds 16 kHz mono 16-bit WAVs (``refs.json`` beside them, ``{clip:
what was said}``, is optional — the judge's 48 replay clips). Each clip, with
2 s of quiet appended, is fed through a real :class:`speech_stream.Stream`
(fast tier only: the accurate pass never changes the words ``turn`` reads)
chunked the way the clients chunk — 150 ms first, then 300 ms — and every
chunk's ``turn`` is noted with the time its response would reach the client
(the chunk's end + the pass's own ms + :data:`RTT_MS`).

Then the client is simulated on a 10 ms clock: it hears a frame as loud at
the stream's own threshold (``SILENCE_RMS`` over 20 ms frames), holds the
latest ``hold_ms`` it has received (:data:`speech_turn.DEFAULT_MS` before the
first), and ends the phrase once the quiet since the last loud frame reaches
it. An end before the clip's last loud frame is PREMATURE (the person was
cut off mid-phrase); otherwise ``saved`` is how much sooner than the 1100 ms
baseline it ended.

``--stretch`` re-runs the clips with every pause inside the phrase (quiet of
:data:`PAUSE_MIN_MS` or more between loud frames) lengthened to N ms (0 = as
recorded): the judge's clips are synthesised, with comma pauses of
150–390 ms, while a person thinking mid-sentence pauses far longer.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import wave
from array import array
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional, Sequence

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.web.core import speech, speech_stream, speech_turn  # noqa: E402

RATE = 16000
#: The client's round trip beyond the server's pass.
RTT_MS = 30
#: The meter's frame.
FRAME_MS = 20
#: Quiet inside a phrase this long or longer is a pause.
PAUSE_MIN_MS = 100
#: Quiet appended after each clip.
TAIL_S = 2.0
#: Today's fixed phrase end.
BASELINE_MS = 1100


def _load(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        assert w.getframerate() == RATE and w.getnchannels() == 1
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0


def _loud(x: np.ndarray) -> np.ndarray:
    """Per :data:`FRAME_MS` frame: loud at the stream's threshold."""
    n = int(RATE * FRAME_MS / 1000)
    k = len(x) // n
    rms = np.sqrt(np.mean(x[: k * n].reshape(k, n) ** 2, axis=1))
    return rms >= speech_stream.SILENCE_RMS


def _pauses(loud: np.ndarray) -> List[tuple]:
    """``[(start_ms, length_ms)]`` of the quiet runs between loud frames."""
    idx = np.where(loud)[0]
    out = []
    for a, b in zip(idx[:-1], idx[1:]):
        gap = (b - a - 1) * FRAME_MS
        if gap >= PAUSE_MIN_MS:
            out.append((int(a + 1) * FRAME_MS, int(gap)))
    return out


def stretch(x: np.ndarray, to_ms: int) -> np.ndarray:
    """``x`` with every pause inside the phrase made ``to_ms`` long (only
    lengthened: a longer one stays)."""
    if not to_ms:
        return x
    parts, at = [], 0
    for start, gap in _pauses(_loud(x)):
        if gap >= to_ms:
            continue
        cut = int((start + gap // 2) * RATE / 1000)
        parts.append(x[at:cut])
        parts.append(np.zeros(int((to_ms - gap) * RATE / 1000), np.float32))
        at = cut
    parts.append(x[at:])
    return np.concatenate(parts)


def _chunks(x: np.ndarray):
    pos, first = 0, True
    while pos < len(x):
        n = int(RATE * (0.15 if first else 0.3))
        first = False
        yield pos + n, x[pos : pos + n]
        pos += n


def replay(fast, x: np.ndarray, words: dict) -> List[dict]:
    """Feed ``x`` (+ :data:`TAIL_S` of quiet) → one entry per chunk:
    ``{"at_ms": when the response reaches the client, "text", "turn"}``."""
    x = np.concatenate([x, np.zeros(int(TAIL_S * RATE), np.float32)])
    stream = speech_stream.Stream(fast)
    out = []
    for end, piece in _chunks(x):
        stream.feed(array("f", piece.tolist()))
        turn = stream.turn(words)
        out.append(
            {
                "end_ms": round(end * 1000 / RATE),
                "at_ms": round(end * 1000 / RATE) + stream.last_ms + RTT_MS,
                "text": speech_stream._join(stream._committed + stream._tail),
                "heard": not stream._unheard,
                "turn": turn,
            }
        )
    return out


def simulate(x: np.ndarray, log: List[dict], hold_of=None) -> dict:
    """The client's 10 ms clock over the clip (see the module doc) →
    ``{"end_ms", "speech_end_ms", "premature", "pauses": [...]}``.
    ``hold_of(entry)``: the hold a log entry gives (default: its
    ``turn.hold_ms``; the baseline passes ``lambda e: 1100``)."""
    hold_of = hold_of or (lambda e: e["turn"]["hold_ms"])
    loud = _loud(x)
    idx = np.where(loud)[0]
    speech_end = (idx[-1] + 1) * FRAME_MS
    total = len(x) * 1000 // RATE + int(TAIL_S * 1000)
    last_loud, heard, end = None, False, None
    li = 0
    hold = speech_turn.DEFAULT_MS
    for t in range(0, total, 10):
        while li < len(log) and log[li]["at_ms"] <= t:
            hold = hold_of(log[li])
            li += 1
        f = t // FRAME_MS
        if f < len(loud) and loud[f]:
            last_loud, heard = int(f + 1) * FRAME_MS, True
            continue
        if heard and last_loud is not None and t - last_loud >= hold:
            end = t
            break
    pauses = []
    for start, gap in _pauses(loud):
        # The verdict the client held during this pause: the latest entry
        # that reached it before the pause ended.
        got = [e for e in log if e["at_ms"] <= start + gap]
        e = got[-1] if got else None
        pauses.append(
            {
                "start_ms": start,
                "ms": gap,
                "text": e["text"] if e else "",
                "hold_ms": hold_of(e) if e else speech_turn.DEFAULT_MS,
                "reason": e["turn"]["reason"] if e else "none",
            }
        )
    return {
        "end_ms": end,
        "speech_end_ms": int(speech_end),
        "premature": bool(end is not None and end < speech_end),
        "pauses": pauses,
    }


def _kind(ref: str) -> str:
    low = ref.lower().rstrip(".?!")
    if low.startswith("mindflock"):
        return "wake question"
    if "," not in low:
        return "lone control"
    return "dictation + radio"


def replay_all(wavdir: Path, stretches: Sequence[int]) -> List[dict]:
    """Every clip at every stretch through the real fast tier → one row per
    clip: ``{"stretch", "clip", "ref", "kind", "log"}`` (``log``: what
    :func:`replay` noted, the verdicts dropped — :func:`score_rows` redoes
    them, so a saved replay can be re-scored with other holds)."""
    speech._voice = lambda: SimpleNamespace(  # type: ignore[assignment]
        stt_backend="local", stt_model="small", stt_stream_model="base.en", stt_final=""
    )
    stt = speech.LocalSTT()
    fast = stt.stream_transcriber("en")
    fast(array("f", [0.0] * RATE), "")  # load + prime
    words = speech_turn._voice_words.effective({})
    refs = {}
    if (wavdir / "refs.json").exists():
        refs = json.loads((wavdir / "refs.json").read_text())
    rows = []
    for st in stretches:
        for path in sorted(wavdir.glob("*.wav")):
            t0 = time.perf_counter()
            log = replay(fast, stretch(_load(path), st), words)
            ref = refs.get(path.name, "")
            for e in log:
                e.pop("turn", None)
            rows.append(
                {
                    "stretch": st,
                    "clip": path.name,
                    "ref": ref,
                    "kind": _kind(ref),
                    "log": log,
                    "replay_s": round(time.perf_counter() - t0, 1),
                }
            )
            print(path.name, st, log[-1]["text"], file=sys.stderr, flush=True)
    return rows


def score_rows(wavdir: Path, rows: List[dict]) -> List[dict]:
    """Each replayed row's verdicts (:func:`speech_turn.score` with today's
    holds) and the simulated client's end (:func:`simulate`) against the
    1100 ms baseline."""
    words = speech_turn._voice_words.effective({})
    out = []
    for r in rows:
        x = stretch(_load(wavdir / r["clip"]), r["stretch"])
        log = r["log"]
        for e in log:
            e["turn"] = speech_turn.score(e["text"], 0, e["heard"], words).json()
        sim = simulate(x, log)
        base = simulate(x, log, lambda e: BASELINE_MS)
        saved = None
        if not sim["premature"] and None not in (sim["end_ms"], base["end_ms"]):
            saved = base["end_ms"] - sim["end_ms"]
        got = [e for e in log if e["at_ms"] <= (sim["end_ms"] or 10**9)]
        out.append(
            {
                "stretch": r["stretch"],
                "clip": r["clip"],
                "ref": r["ref"],
                "kind": r["kind"],
                "text": log[-1]["text"],
                "final_reason": got[-1]["turn"]["reason"] if got else None,
                "final_hold": got[-1]["turn"]["hold_ms"] if got else None,
                "end_ms": sim["end_ms"],
                "base_end_ms": base["end_ms"],
                "premature": sim["premature"],
                "base_premature": base["premature"],
                "saved_ms": saved,
                "pauses": sim["pauses"],
            }
        )
    return out


def run(
    wavdir: Path,
    stretches: Sequence[int],
    out: Optional[Path],
    rescore: Optional[Path] = None,
    verbose: bool = False,
) -> int:
    if rescore:
        rows = [json.loads(line) for line in rescore.read_text().splitlines() if line]
    else:
        rows = replay_all(wavdir, stretches)
        if out:
            out.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    scored = score_rows(wavdir, rows)
    if verbose:
        for r in scored:
            print(json.dumps(r))
    print(table(scored))
    return 0


def table(rows: List[dict]) -> str:
    lines = [
        "| pauses | kind | clips | premature | baseline premature | saved p50 ms | saved mean ms |",
        "|---|---|---|---|---|---|---|",
    ]
    keys = []
    for r in rows:
        for k in ((r["stretch"], r["kind"]), (r["stretch"], "all")):
            if k not in keys:
                keys.append(k)
    for st, kind in keys:
        sel = [
            r
            for r in rows
            if r["stretch"] == st and (kind == "all" or r["kind"] == kind)
        ]
        saved = [r["saved_ms"] for r in sel if r["saved_ms"] is not None]
        lines.append(
            "| %s | %s | %d | %d | %d | %s | %s |"
            % (
                "as recorded" if not st else "%d ms" % st,
                kind,
                len(sel),
                sum(r["premature"] for r in sel),
                sum(r["base_premature"] for r in sel),
                int(statistics.median(saved)) if saved else "-",
                int(statistics.mean(saved)) if saved else "-",
            )
        )
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("wavdir", type=Path)
    ap.add_argument(
        "--stretch", default="0", help="pause lengths, ms (0 = as recorded)"
    )
    ap.add_argument("--json", type=Path, default=None, help="save the replay here")
    ap.add_argument(
        "--rescore", type=Path, default=None, help="score a saved replay (no models)"
    )
    ap.add_argument(
        "--hold", default="", help="override holds: reason=ms,... (e.g. sentence=900)"
    )
    ap.add_argument("-v", "--verbose", action="store_true", help="print every row")
    a = ap.parse_args(argv)
    for item in filter(None, a.hold.split(",")):
        reason, ms = item.split("=")
        assert reason in speech_turn._HOLD, reason
        speech_turn._HOLD[reason] = int(ms)
    os.environ.setdefault("OMP_NUM_THREADS", "2")
    stretches = [int(s) for s in a.stretch.split(",")]
    return run(a.wavdir, stretches, a.json, a.rescore, a.verbose)


if __name__ == "__main__":
    sys.exit(main())
