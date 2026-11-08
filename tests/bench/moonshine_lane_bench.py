"""The Moonshine lane bench: :class:`core.speech_moonshine.MoonshineStream`
— the real module, not a re-implementation — driven through the STT
candidates bench's clips, per thread setting. Not a unit test (pytest never
collects ``tests/bench``).

The clips are the ones ``tests/bench/stt_candidates_bench.py render`` /
``fetch`` wrote (branch ``emandel2630/stt-investigation``): ``piper`` =
:mod:`voice_bench`'s 48 command phrases (same list, seed and noise), ``libri``
= 73 LibriSpeech utterances; each set is a folder with ``manifest.json``.
Runs in the scratch venv that has ``moonshine-voice`` (never the server's)::

    B="PYTHONPATH=$PWD ~/stt-bench-venv/bin/python tests/bench/moonshine_lane_bench.py"
    $B all                    # every thread setting × set, one process each
    $B run --threads 1 --set piper
    $B run --threads 1 --set libri --polish   # + the whisper pass on long phrases
    $B run --threads 1 --set libri --polish --realtime --hold-ms 900 --min-dur 4.5 --limit 20

Threads. moonshine-voice 0.1.6 exposes no ONNX Runtime thread COUNT: its
only knob is ``MOONSHINE_ORT_SINGLE_THREAD`` (one thread) — otherwise ORT's
default pool (every core, spinning). Process CPU affinity does not cap it
(ORT pins its own threads). So the settings are ``1`` and ``pool``.

Each clip is fed in 160 ms chunks on a simulated clock: a chunk goes in once
it has been spoken, or when the engine is free if it lags (its backlog
counts). Per setting and set:

* **finish p50/p95** — ms from the clip's end (its trailing room included)
  until :meth:`MoonshineStream.finish` returned: what the client waits for
  after "over" (today's whisper pipeline: 1022 / 1500 ms);
* **finish call p50/p95** — the ``finish()`` call alone;
* **first word p50/p90** — ms from the clip's start until the first word
  (committed or tail) showed;
* **pass p50/p95** — one ``feed`` pass;
* **WER final / WER typed** — :func:`voice_bench._wer` over
  :func:`voice_bench._norm` of ``final`` and of the committed (typed) text;
* **ctl** — the control word heard where it belongs
  (:func:`voice_bench._control`, piper only), on the final;
* **same** — finals that equal the typed text (no correction on screen);
* **cores** — process CPU s / wall s while decoding; **RSS** MB peak;
  **load** s.

``--polish`` gives the stream the production whisper accurate pass
(``LocalSTT.final_transcriber`` with the production hints, faster-whisper
from ``$STT_BENCH_MODELS/mfvoice``), which runs only on phrases with
``POLISH_MIN_SECONDS`` of speech or more — **polished** counts them.

Results 2026-11-07 (8-core Core Ultra 9 275HX under WSL2, 0.8–2.9 other
cores busy at each start; moonshine-voice 0.1.6, tiny-streaming, keyterms
from the manifest's production hints):

| threads | set | finish p50/p95 ms | first word p50/p90 | pass p50/p95 | WER final / typed | ctl | same | cores | RSS MB |
|---|---|---|---|---|---|---|---|---|---|
| 1 | piper | **27 / 45** | 1309 / 1323 | 3 / 46 | 8.3 / 8.7% | 45/48 | 46/48 | **0.97** | 195 |
| pool | piper | 73 / 185 | 1365 / 1457 | 6 / 150 | 8.3 / 8.7% | 45/48 | 46/48 | 6.19 | 199 |
| 1 | libri | **9 / 48** | 1641 / 1803 | 6 / 64 | 13.4 / 14.4% | — | 53/73 | **0.97** | 229 |
| pool | libri | 34 / 384 | 1788 / 1941 | 15 / 240 | 13.4 / 14.4% | — | 53/73 | 6.45 | 237 |
| 1 + polish | libri | 1270 / 1525 | 1712 / 1817 | 6 / 71 | 12.7 / 14.4% | — | 50/73 | 2.46 | 1298 |

One thread is faster AND ~6x cheaper than the pool, with identical text.
Today's whisper pipeline on piper: finish 1022 / 1500 ms, WER 8.7%, ctl
44/48. The polish row: only 8 of the long phrases got whisper's text inside
``FINAL_TIMEOUT_S`` (this bench calls ``finish`` the instant the clip's
audio is in, so the early pass never gets the client's phrase-end silence
to run in), and it costs the ~1.2 s wait again — a synchronous polish
undoes the lane's point; deliver it after the Moonshine final instead.

``--realtime`` feeds each chunk at real-time pace on the wall clock and,
after the clip, quiet chunks until the quiet after the last loud frame
reaches ``--hold-ms`` (the client's end-of-turn hold: 900 after a sentence,
350 after "over"), as the phone and the desktop mic do — so the early
accurate pass gets the quiet tail it starts on, and **finish call** is what
``/end`` costs. ``--min-dur`` keeps clips at least that long.

Results 2026-11-07, ``--polish --realtime``, 20 LibriSpeech clips of
5–30 s (``--min-dur 4.5``):

| polish rule | hold ms | finish call p50/p95 ms | WER final / typed | polished |
|---|---|---|---|---|
| start one at the end, wait ≤ 1.5 s (before) | 900 | 349 / 1078 | 8.2 / 12.5% | 13/20 |
| start one at the end, wait ≤ 1.5 s (before) | 350 | 296 / 1344 | 8.2 / 12.5% | 13/20 |
| ride the early pass only, wait ≤ 1 s (``POLISH_WAIT_S``) | 900 | 207 / 905 | 8.2 / 12.5% | 13/20 |
| ride the early pass only, wait ≤ 1 s (``POLISH_WAIT_S``) | 350 | 258 / 1017 | 9.7 / 12.5% | 10/20 |

Per phrase (before, 900 ms hold): the 5–6 s clips stay under 4 s of loud
speech (Moonshine's final, ~10 ms); 6–13 s clips wait 0.1–0.8 s for the
polish; 16–19 s clips 0.8–1.1 s; a 30 s clip sat out the whole 1.5 s and
got Moonshine's text anyway. Hence ``POLISH_WAIT_S``: polish only rides the
early pass and is waited for at most 1 s.

Before every process ``all`` waits until the machine's other processes use
fewer than :data:`MAX_LOAD` cores (``/proc/stat``). Results go to
``$STT_BENCH_RESULTS/moonshine-lane-<threads>-<set>[-polish].json``.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import subprocess
import sys
import time
import wave
from array import array
from pathlib import Path
from typing import Dict, List

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for p in (str(ROOT), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

MODELS = Path(
    os.path.expanduser(os.environ.get("STT_BENCH_MODELS", "~/stt-bench-models"))
)
DATA = Path(os.path.expanduser(os.environ.get("STT_BENCH_DATA", str(MODELS / "data"))))
RESULTS = Path(
    os.path.expanduser(os.environ.get("STT_BENCH_RESULTS", str(MODELS / "results")))
)
# The module finds the model under MINDFLOCK_VOICE_DIR/moonshine: point it at
# a scratch dir whose moonshine/ is the bench's model cache (never the live
# ~/.mindflock/voice).
VOICE = MODELS / "mfvoice"
os.environ.setdefault("MINDFLOCK_VOICE_DIR", str(VOICE))
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

CHUNK_S = 0.16
MAX_LOAD = 3.0
SETTINGS = ("1", "pool")


def _read_wav(path: Path) -> array:
    with wave.open(str(path), "rb") as w:
        raw = w.readframes(w.getnframes())
    out = array("h")
    out.frombytes(raw)
    return array("f", (x / 32768.0 for x in out))


def _pct(values: List[float], q: float) -> int:
    if not values:
        return 0
    s = sorted(values)
    return int(round(s[min(len(s) - 1, int(round(q * (len(s) - 1))))]))


def _pace(t0: float, at: float) -> None:
    """--realtime: wait until ``at`` seconds of audio have been spoken
    since the wall-clock ``t0`` (a chunk is never sent before it exists)."""
    ahead = t0 + at - time.monotonic()
    if ahead > 0:
        time.sleep(ahead)


def _link_models() -> None:
    """``$VOICE/moonshine`` → the candidates bench's model cache."""
    VOICE.mkdir(parents=True, exist_ok=True)
    link = VOICE / "moonshine"
    if not link.exists():
        link.symlink_to(MODELS / "moonshine")


# --------------------------------------------------------------------------- #
# One setting × set (in its own process)
# --------------------------------------------------------------------------- #
def cmd_run(args) -> int:
    os.environ["MOONSHINE_ORT_SINGLE_THREAD"] = "1" if args.threads == "1" else "0"
    _link_models()
    import voice_bench as vb  # sets a bare MINDFLOCK_SETTINGS_FILE

    from backend.web.core import speech
    from backend.web.core import speech_moonshine as sm

    if not sm.available():
        print("unavailable: " + sm.unavailable_reason(), file=sys.stderr)
        return 1
    folder = DATA / args.set
    manifest = json.loads((folder / "manifest.json").read_text())
    items = manifest["items"]
    if args.min_dur:
        items = [
            it
            for it in items
            if len(_read_wav(folder / it["wav"])) / 16000.0 >= args.min_dur
        ]
    items = items[: args.limit or None]
    words = manifest.get("words") or vb.voice_words.effective({})
    hints = manifest.get("hints") or speech.recognition_hints(words, vb.TITLES)

    t0 = time.monotonic()
    engine = sm.engine()
    load_s = time.monotonic() - t0
    accurate = None
    if args.polish:
        accurate = speech.LocalSTT().final_transcriber("en", hints=hints)
        accurate(array("f", bytes(4 * 16000)))  # load + prime
    # Warm-up: one clip through a stream, not counted.
    warm = sm.MoonshineStream(engine, None, hints)
    warm.feed(_read_wav(folder / items[0]["wav"]))
    warm.finish()

    rows: List[dict] = []
    cpu0, wall0 = time.process_time(), time.monotonic()
    for it in items:
        samples = _read_wav(folder / it["wav"])
        end = len(samples) / vb.RATE
        st = sm.MoonshineStream(engine, accurate, hints)
        st.words = words
        clock = 0.0  # simulated time the engine is free
        t_wall = time.monotonic()  # --realtime: the clip's wall-clock start
        first = None
        passes: List[float] = []
        step = int(CHUNK_S * vb.RATE)
        for i in range(0, len(samples), step):
            chunk = samples[i : i + step]
            spoken = (i + len(chunk)) / vb.RATE
            if args.realtime:
                _pace(t_wall, spoken)
                clock = max(clock, time.monotonic() - t_wall)
            start = max(clock, spoken)
            w = time.monotonic()
            committed, tail = st.feed(chunk)
            took = time.monotonic() - w
            clock = start + took
            if st.last_ms or took > 0.002:
                passes.append(took * 1000)
            if first is None and (committed or tail):
                first = clock
        if args.realtime:
            # The client's turn hold: quiet chunks, at real-time pace, until
            # the quiet after the last loud frame reaches --hold-ms (the
            # early accurate pass starts on the first of them).
            quiet = array("f", bytes(4 * step))
            fed = end
            while (st._fed - st._loud_end) * 1000.0 < args.hold_ms:
                fed += CHUNK_S
                _pace(t_wall, fed)
                st.feed(quiet)
            end = fed
            start = max(clock, end)
        else:
            start = max(clock, end)
        w = time.monotonic()
        out = st.finish()
        call = time.monotonic() - w
        clock = start + call
        final = out.get("final") or st.text
        ref = vb._norm(it["text"])
        row = {
            "wav": it["wav"],
            "ref": it["text"],
            "final": final,
            "typed": st.text,
            "final_by": out.get("final_by", ""),
            "same": bool(out.get("same")),
            "finish_ms": (clock - end) * 1000,
            "call_ms": call * 1000,
            "first_ms": None if first is None else first * 1000,
            "pass_ms": passes,
            "wer": vb._wer(ref, vb._norm(final)),
            "wer_typed": vb._wer(ref, vb._norm(st.text)),
            "dur": end,
        }
        if it.get("check"):
            row["ctl"] = vb._control(final, tuple(it["check"]), words)[0]
        rows.append(row)
    cpu, wall = time.process_time() - cpu0, time.monotonic() - wall0

    def wer(key: str) -> float:
        e = sum(r[key][0] for r in rows)
        n = sum(r[key][1] for r in rows) or 1
        return round(100.0 * e / n, 1)

    passes = [p for r in rows for p in r["pass_ms"]]
    firsts = [r["first_ms"] for r in rows if r["first_ms"] is not None]
    ctl = [r["ctl"] for r in rows if "ctl" in r]
    summary = {
        "threads": args.threads,
        "engine_threads": engine.threads,
        "set": args.set,
        "polish": bool(args.polish),
        "realtime": bool(args.realtime),
        "hold_ms": args.hold_ms if args.realtime else 0,
        "n": len(rows),
        "finish_p50": _pct([r["finish_ms"] for r in rows], 0.5),
        "finish_p95": _pct([r["finish_ms"] for r in rows], 0.95),
        "call_p50": _pct([r["call_ms"] for r in rows], 0.5),
        "call_p95": _pct([r["call_ms"] for r in rows], 0.95),
        "first_p50": _pct(firsts, 0.5),
        "first_p90": _pct(firsts, 0.9),
        "pass_p50": _pct(passes, 0.5),
        "pass_p95": _pct(passes, 0.95),
        "wer": wer("wer"),
        "wer_typed": wer("wer_typed"),
        "ctl": "%d/%d" % (sum(ctl), len(ctl)) if ctl else "",
        "same": "%d/%d" % (sum(r["same"] for r in rows), len(rows)),
        "polished": sum(r["final_by"] == "whisper" for r in rows),
        "cores": round(cpu / wall, 2) if wall else 0.0,
        "rss_mb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024,
        "load_s": round(load_s, 2),
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    name = "moonshine-lane-%s-%s%s%s.json" % (
        args.threads,
        args.set,
        "-polish" if args.polish else "",
        "-rt%d" % args.hold_ms if args.realtime else "",
    )
    (RESULTS / name).write_text(
        json.dumps({"summary": summary, "rows": rows}, indent=1)
    )
    print(json.dumps(summary))
    return 0


# --------------------------------------------------------------------------- #
# Every setting, one process at a time
# --------------------------------------------------------------------------- #
def _busy_cores(seconds: float = 1.0) -> float:
    def sample():
        with open("/proc/stat") as f:
            v = [int(x) for x in f.readline().split()[1:]]
        return sum(v), v[3] + v[4]

    t1, i1 = sample()
    time.sleep(seconds)
    t2, i2 = sample()
    total = (t2 - t1) or 1
    return (os.cpu_count() or 1) * (1 - (i2 - i1) / total)


def _wait_quiet(limit_s: float = 600.0) -> float:
    start = time.monotonic()
    busy = _busy_cores()
    while busy >= MAX_LOAD and time.monotonic() - start < limit_s:
        print("waiting: %.1f cores busy" % busy, file=sys.stderr)
        time.sleep(10)
        busy = _busy_cores()
    return busy


def cmd_all(args) -> int:
    rows: List[Dict] = []
    for data_set in args.sets.split(","):
        for threads in SETTINGS:
            busy = _wait_quiet()
            argv = [
                sys.executable,
                __file__,
                "run",
                "--threads",
                threads,
                "--set",
                data_set,
            ]
            if args.polish:
                argv.append("--polish")
            out = subprocess.run(argv, capture_output=True, text=True)
            if out.returncode != 0:
                print(out.stderr[-2000:], file=sys.stderr)
                continue
            summary = json.loads(out.stdout.strip().splitlines()[-1])
            summary["busy_before"] = round(busy, 1)
            rows.append(summary)
            print(json.dumps(summary), file=sys.stderr)
    print(table(rows))
    return 0


COLUMNS = [
    ("threads", "threads"),
    ("set", "set"),
    ("n", "n"),
    ("finish p50/p95 ms", ("finish_p50", "finish_p95")),
    ("finish call p50/p95", ("call_p50", "call_p95")),
    ("first word p50/p90", ("first_p50", "first_p90")),
    ("pass p50/p95", ("pass_p50", "pass_p95")),
    ("WER final", "wer"),
    ("WER typed", "wer_typed"),
    ("ctl", "ctl"),
    ("same", "same"),
    ("polished", "polished"),
    ("cores", "cores"),
    ("RSS MB", "rss_mb"),
    ("load s", "load_s"),
]


def table(rows: List[dict]) -> str:
    def cell(row, key):
        if isinstance(key, tuple):
            return " / ".join(str(row.get(k, "")) for k in key)
        return str(row.get(key, ""))

    head = "| " + " | ".join(c for c, _ in COLUMNS) + " |"
    rule = "|" + "---|" * len(COLUMNS)
    body = ["| " + " | ".join(cell(r, k) for _, k in COLUMNS) + " |" for r in rows]
    return "\n".join([head, rule, *body])


def cmd_report(args) -> int:
    rows = []
    for path in sorted(RESULTS.glob("moonshine-lane-*.json")):
        rows.append(json.loads(path.read_text())["summary"])
    print(table(rows))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--threads", choices=SETTINGS, default="1")
    run.add_argument("--set", default="piper")
    run.add_argument("--limit", type=int, default=0)
    run.add_argument("--polish", action="store_true")
    run.add_argument("--realtime", action="store_true")
    run.add_argument("--hold-ms", type=int, default=900)
    run.add_argument("--min-dur", type=float, default=0.0)
    run.set_defaults(fn=cmd_run)
    every = sub.add_parser("all")
    every.add_argument("--sets", default="piper,libri")
    every.add_argument("--polish", action="store_true")
    every.set_defaults(fn=cmd_all)
    sub.add_parser("report").set_defaults(fn=cmd_report)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
