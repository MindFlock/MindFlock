"""The dictation-context bench: does prompting the accurate pass with the
text dictated before a phrase (``POST /api/voice/stream``'s ``context``)
make whisper hear the phrase better, at what cost, and which decoder
settings should the accurate pass run?

Not a unit test — pytest never collects it. It drives the REAL accurate
pass (``core.speech.LocalSTT.final_transcriber``: the production hints,
greedy/beam, temperature 0, the VAD, the token cap, the 20 s pad), with only
the model (``LocalSTT.final_model_name``) and the beam
(``core.speech.FINAL_BEAM``) swapped per config. faster-whisper and piper
live in the uv TOOL venv::

    T=~/.local/share/uv/tools/mindflock/bin/python
    PYTHONPATH=$PWD $T tests/bench/stt_context_bench.py render
    PYTHONPATH=$PWD $T tests/bench/stt_context_bench.py run            # every config
    PYTHONPATH=$PWD $T tests/bench/stt_context_bench.py run --configs small,small+ctx

Three sets (``$STT_BENCH_DATA``, default ``~/stt-bench-models/data``):

* **libri** — LibriSpeech (``librispeech_asr_dummy``, ``stt_candidates_bench
  fetch``): the clips up to :data:`LIBRI_MAX_S` long, IN ORDER — consecutive
  utterances of one chapter, so a clip's context is the config's own text of
  the clips before it (what a client sends: its earlier finals), cut to
  ``core.speech.CONTEXT_CHARS``. Real read speech, chapter boundaries
  included (a context that does not fit must not hurt).
* **dict** — piper dictation (``render``): coding messages each said as 2–3
  phrases split at pauses (:data:`MESSAGES`), two voices (the installed
  lessac and the multi-speaker libritts_r, a speaker per message), the
  context reset per message: the owner's case.
* **short** — the prompt's failure modes: very short replies ("yes", "run
  the tests") and quiet clips, each with a long context (:data:`LONG_CONTEXT`).
  ``copied``: a hypothesis holding a run of :data:`COPY_RUN` words from the
  context that the reference does not; ``ghost``: a quiet clip that came back
  with words.

Per config: WER per set (``voice_bench._norm``, compounds folded:
:data:`COMPOUNDS`), the pass's wall time p50/p95 over every phrase and p50
over the phrases under :data:`SHORT_S` (the dictation case the +150 ms
budget is about), copied / ghost counts. A ``+ctx`` config chains its OWN
previous outputs as the context; ``+sess`` adds the focused session's
vocabulary (:func:`session_vocabulary` of :data:`SESSION_REPLY`) ahead of it
(:func:`_with_vocabulary`; the server does not: no gain measured).
Timings mean something only on an idle box: the load average is printed;
``timing`` interleaves the configs phrase by phrase for a busy one.

Results (2026-11-09, a busy box: load 8–40, so ``run``'s times are rough
and ``timing``'s paired deltas are the cost figure). WER %, ``--libri 0``
(60 clips, 889 words) for libri / libri-n, the 41-phrase dict set::

    config         libri  libri-n  dict   paired p50 vs small
    small           7.42    9.45   4.70     +0 ms
    small+ctx       6.97    8.32   5.03     -3 ms
    small.en        6.41    8.55   2.68    -41 ms
    small.en+ctx    5.96    7.54   2.35    -27 ms

Beam 5 (30 clips): ~1 point better but 1.9–2.7 s p50, past the 1.5 s cap.
``+sess``: dict 2.35 → 2.35 (no gain). short: 0 copied, 0 ghosts anywhere.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
import wave
import zlib
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "tests" / "bench"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import voice_bench as vb  # noqa: E402  (sets a scratch settings file first)
import numpy as np  # noqa: E402

from backend.web.core import speech, voice_words  # noqa: E402

# The production memory guard reads PSI stalls, which a busy box (other
# sessions' tests) trips; the bench's models are ~250 MB each.
speech._memory_block = lambda: ""  # type: ignore[assignment]

RATE = 16000
MODELS = Path(
    os.path.expanduser(os.environ.get("STT_BENCH_MODELS", "~/stt-bench-models"))
)
DATA = Path(os.path.expanduser(os.environ.get("STT_BENCH_DATA", str(MODELS / "data"))))
OUT = DATA / "context"
LIBRI_MAX_S = 12.0
SHORT_S = 3.5
COPY_RUN = 3
VOICES = [
    os.path.expanduser("~/.mindflock/voice/piper/en_US-lessac-medium.onnx"),
    str(MODELS / "piper" / "en_US-libritts_r-medium.onnx"),
]

#: Coding messages, each said as phrases split at pauses.
MESSAGES: List[List[str]] = [
    [
        "so the accurate pass runs once at the end of every phrase",
        "but it has no idea which sentence it continues",
        "so pass the context through as the initial prompt",
    ],
    ["open the speech stream module", "and find where the early pass gets started"],
    [
        "the pytest run failed on the moonshine stream test",
        "because the fake model never returns a final",
    ],
    [
        "can you refactor the final transcriber",
        "so it takes the dictation context",
        "and caps the tokens to what the prompt leaves",
    ],
    [
        "add a unit test that sends a context string",
        "longer than the limit",
        "and checks it was cut at a word boundary",
    ],
    [
        "the hotwords and the initial prompt share one slot",
        "so keep them both inside the token budget",
    ],
    ["rebase this branch onto main", "then force push it and open a pull request"],
    [
        "check the docker compose file",
        "the postgres container keeps restarting",
        "maybe the volume permissions are wrong",
    ],
    [
        "write a typescript interface for the stream response",
        "with the context count as a number",
    ],
    [
        "the regex in the sanitizer drops control characters",
        "but it should also collapse the whitespace",
    ],
    [
        "look at the route that opens the voice stream",
        "it should return how many characters it used",
    ],
    [
        "run the benchmark again on an idle machine",
        "and report the word error rate for each config",
    ],
    [
        "the tokenizer counts the hotwords exactly",
        "but falls back to an estimate for the fakes",
    ],
    [
        "kill the stale worktree",
        "and delete the merged branches",
        "then prune the remote refs",
    ],
    ["move the helper into its own module", "and update the imports in the router"],
    ["the async handler blocks the event loop", "so wrap the model call in a thread"],
    [
        "why does the schema reject an empty string",
        "the context field should be optional",
    ],
    ["bump the minor version", "and add a line to the release notes"],
]
#: The short replies, each after :data:`LONG_CONTEXT`.
SHORT = [
    "yes",
    "no",
    "run the tests",
    "okay do it",
    "sounds good",
    "commit that",
    "thanks",
    "go ahead",
]
QUIET = 2
LONG_CONTEXT = (
    "Can you refactor the final transcriber so it takes the dictation context "
    "and caps the tokens to what the prompt leaves? Add a unit test that sends a "
    "context string longer than the limit and checks it was cut at a word "
    "boundary. The hotwords and the initial prompt share one slot, so keep them "
    "both inside the token budget."
)
#: A focused session's last reply (what ``+sess`` reads its vocabulary off).
SESSION_REPLY = (
    "I updated `backend/web/core/speech.py`: `final_transcriber` now takes the "
    "dictation context and passes it as `initial_prompt`; `_prompt_room` caps "
    "`max_new_tokens`. `MoonshineStream` and `speech_stream.Stream` reuse the same "
    "accurate callable. Tests: `tests/unit/test_speech_context.py` (12 passed). "
    "The sanitizer `dictation_context` drops control characters and collapses "
    "whitespace. Next: the tokenizer fallback and the hotwords budget."
)
#: Spellings that differ only by a space: folded on both sides of the WER.
COMPOUNDS = {
    "hot words": "hotwords",
    "type script": "typescript",
    "work tree": "worktree",
    "pie test": "pytest",
    "py test": "pytest",
    "white space": "whitespace",
    "bench mark": "benchmark",
    "sanitiser": "sanitizer",
    "tokeniser": "tokenizer",
    "post gres": "postgres",
    "moon shine": "moonshine",
    "ok": "okay",
    "mr": "mister",
}

#: (name, model, beam, context, session vocabulary)
CONFIGS: List[Tuple[str, str, int, bool, bool]] = [
    ("small", "small", 1, False, False),
    ("small+ctx", "small", 1, True, False),
    ("small.en", "small.en", 1, False, False),
    ("small.en+ctx", "small.en", 1, True, False),
    ("small/b5", "small", 5, False, False),
    ("small+ctx/b5", "small", 5, True, False),
    ("small.en+ctx/b5", "small.en", 5, True, False),
    ("small+ctx+sess", "small", 1, True, True),
    ("small.en+ctx+sess", "small.en", 1, True, True),
    ("medium.en", "medium.en", 1, False, False),
    ("medium.en+ctx", "medium.en", 1, True, False),
]
DEFAULT = [c[0] for c in CONFIGS if not c[1].startswith("medium")]


# --------------------------------------------------------------------------- #
# Session vocabulary (the +sess configs)
# --------------------------------------------------------------------------- #
_CODEISH = re.compile(
    r"`([^`\n]{2,60})`|\b([A-Za-z_][\w./-]*[_./][\w./-]*|[a-z]+[A-Z]\w*|[A-Z][a-z]+[A-Z]\w*)\b"
)


def session_vocabulary(text: str, limit: int = 200) -> str:
    """The identifiers and file names of a reply (backticked spans,
    snake_case, dotted / slashed paths, camelCase), the last path part only,
    deduplicated, comma-separated, at most ``limit`` characters."""
    out: List[str] = []
    seen = set()
    size = 0
    for m in _CODEISH.finditer(text or ""):
        word = (m.group(1) or m.group(2) or "").strip().strip(".,;:")
        word = word.rsplit("/", 1)[-1]
        if not word or word.lower() in seen:
            continue
        add = len(word) + (2 if out else 0)
        if size + add > limit:
            continue
        seen.add(word.lower())
        out.append(word)
        size += add
    return ", ".join(out)


# --------------------------------------------------------------------------- #
# Audio
# --------------------------------------------------------------------------- #
def _write_wav(path: Path, samples: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())


def _read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


def _synth(voice, text: str, rng: random.Random, speaker: Optional[int]) -> np.ndarray:
    """``voice_bench.synthesise`` with a speaker for the multi-speaker voice."""
    from piper.config import SynthesisConfig

    cfg = SynthesisConfig(
        speaker_id=speaker,
        length_scale=rng.choice([0.85, 0.95, 1.0, 1.1, 1.2]),
        noise_scale=rng.choice([0.4, 0.667, 0.9]),
        noise_w_scale=rng.choice([0.5, 0.8, 1.1]),
    )
    parts, rate = [], RATE
    for chunk in voice.synthesize(text, cfg):
        parts.append(chunk.audio_float_array)
        rate = chunk.sample_rate
    s = vb._resample(np.concatenate(parts), rate)
    s *= rng.uniform(0.5, 0.9) / max(1e-6, float(np.abs(s).max()))
    lead = np.zeros(int(RATE * rng.uniform(0.3, 0.5)), dtype=np.float32)
    tail = np.zeros(int(RATE * rng.uniform(0.3, 0.5)), dtype=np.float32)
    out = np.concatenate([lead, s, tail])
    snr = rng.uniform(22.0, 35.0)
    noise = np.random.default_rng(rng.getrandbits(32)).standard_normal(len(out))
    noise = noise.astype(np.float32) * float(
        np.sqrt(np.mean(out**2)) / 10 ** (snr / 20)
    )
    return np.clip(out + noise, -1.0, 1.0).astype(np.float32)


def cmd_render(args) -> int:
    from piper.voice import PiperVoice

    rng = random.Random(args.seed)
    voices = [PiperVoice.load(v) for v in VOICES if os.path.exists(v)]
    OUT.mkdir(parents=True, exist_ok=True)
    items: List[dict] = []
    n = 0

    def put(samples, text, group, kind):
        nonlocal n
        name = "%03d.wav" % n
        n += 1
        _write_wav(OUT / name, samples)
        items.append(
            {
                "wav": name,
                "text": text,
                "group": group,
                "kind": kind,
                "dur": round(len(samples) / RATE, 3),
            }
        )

    for g, msg in enumerate(MESSAGES):
        vi = g % len(voices)
        speaker = rng.randrange(0, 900) if vi == 1 else None
        for phrase in msg:
            put(_synth(voices[vi], phrase, rng, speaker), phrase, "m%02d" % g, "dict")
    for i, text in enumerate(SHORT):
        vi = i % len(voices)
        speaker = rng.randrange(0, 900) if vi == 1 else None
        put(_synth(voices[vi], text, rng, speaker), text, "s%02d" % i, "short")
    for i in range(QUIET):
        noise = np.random.default_rng(rng.getrandbits(32)).standard_normal(
            int(1.5 * RATE)
        )
        put((noise * 0.003).astype(np.float32), "", "q%02d" % i, "short")
    (OUT / "manifest.json").write_text(json.dumps({"items": items}, indent=1))
    print("%d clips → %s" % (len(items), OUT))
    return 0


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
def _words(text: str) -> List[str]:
    low = " ".join(vb._norm(text))
    for a, b in COMPOUNDS.items():
        low = re.sub(r"\b%s\b" % re.escape(a), b, low)
    return low.split()


def _copied(hyp: str, ref: str, context: str) -> bool:
    """A run of :data:`COPY_RUN` words of ``context`` in ``hyp`` that
    ``ref`` does not hold."""
    h, r, c = _words(hyp), " ".join(_words(ref)), " ".join(_words(context))
    for i in range(len(h) - COPY_RUN + 1):
        run = " ".join(h[i : i + COPY_RUN])
        if run in c and run not in r:
            return True
    return False


def _pct(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    v = sorted(values)
    return v[min(len(v) - 1, int(round(q * (len(v) - 1))))]


# --------------------------------------------------------------------------- #
# Running
# --------------------------------------------------------------------------- #
def _sets(libri: int, only: str = "") -> Dict[str, List[dict]]:
    out: Dict[str, List[dict]] = {}
    lm = json.loads((DATA / "libri" / "manifest.json").read_text())["items"]
    rows = [
        dict(it, path=str(DATA / "libri" / it["wav"]), group="libri")
        for it in lm
        if it["dur"] <= LIBRI_MAX_S
    ]
    out["libri"] = rows[:libri] if libri else rows
    # The same clips under a laptop mic's noise (NOISY_SNR_DB, a fixed seed):
    # where the audio leaves more open, which the context should settle.
    out["libri-n"] = [dict(it, noisy=True) for it in out["libri"]]
    cm = json.loads((OUT / "manifest.json").read_text())["items"]
    for kind in ("dict", "short"):
        out[kind] = [
            dict(it, path=str(OUT / it["wav"])) for it in cm if it["kind"] == kind
        ]
    wanted = [w for w in only.split(",") if w] or ["libri", "dict", "short"]
    return {k: v for k, v in out.items() if k in wanted}


NOISY_SNR_DB = 12.0


def _audio(it: dict) -> np.ndarray:
    audio = _read_wav(Path(it["path"]))
    if not it.get("noisy"):
        return audio
    rng = np.random.default_rng(zlib.crc32(it["wav"].encode()))
    white = rng.standard_normal(len(audio)).astype(np.float32)
    brown = np.cumsum(rng.standard_normal(len(audio))).astype(np.float32)
    brown -= np.convolve(brown, np.ones(400) / 400, mode="same")  # drop the drift
    noise = white / max(1e-6, float(white.std())) + brown / max(
        1e-6, float(brown.std())
    )
    level = float(np.sqrt(np.mean(audio**2))) / 10 ** (NOISY_SNR_DB / 20)
    return np.clip(audio + noise * level / float(noise.std()), -1, 1).astype(np.float32)


_SANITIZE = speech.dictation_context


def _with_vocabulary(vocab: str) -> None:
    """The ``+sess`` prompt: ``vocab`` ahead of the sanitized context (the
    server does not do this — the bench showed no gain — so the bench puts
    it in by wrapping ``dictation_context``, which the accurate pass calls
    at build time); ``""`` restores the plain sanitizer."""
    if not vocab:
        speech.dictation_context = _SANITIZE  # type: ignore[assignment]
        return

    def wrapped(raw, limit: int = speech.CONTEXT_CHARS) -> str:
        ctx = _SANITIZE(raw, limit)
        return vocab + (". " + ctx if ctx else "")

    speech.dictation_context = wrapped  # type: ignore[assignment]


def run_config(cfg, sets, hints: str) -> dict:
    name, model, beam, ctx, sess = cfg
    stt = speech.LocalSTT()
    stt.final_model_name = lambda: model  # type: ignore[method-assign]
    speech.FINAL_BEAM = beam
    _with_vocabulary(session_vocabulary(SESSION_REPLY) if sess else "")
    t0 = time.monotonic()
    warm = stt.final_transcriber("en", hints)
    warm(np.zeros(RATE, dtype=np.float32).tobytes())  # load + first-call workspaces
    load_s = time.monotonic() - t0
    res: dict = {"config": name, "load_s": round(load_s, 1), "sets": {}, "rows": []}
    all_ms: List[float] = []
    short_ms: List[float] = []
    for set_name, items in sets.items():
        edits = total = copied = ghost = 0
        history: Dict[str, List[str]] = {}
        for it in items:
            group = it["group"]
            if set_name == "short":
                context = LONG_CONTEXT if ctx else ""
            else:
                context = " ".join(history.get(group, [])) if ctx else ""
            run = stt.final_transcriber("en", hints, context=context)
            audio = _audio(it)
            t = time.monotonic()
            hyp = run(audio.astype(np.float32).tobytes())
            ms = (time.monotonic() - t) * 1000
            all_ms.append(ms)
            if it["dur"] <= SHORT_S:
                short_ms.append(ms)
            history.setdefault(group, []).append(hyp)
            e, n = vb._wer(_words(it["text"]), _words(hyp))
            edits += e
            total += n
            if set_name == "short":
                if not it["text"] and _words(hyp):
                    ghost += 1
                if context and _copied(hyp, it["text"], context):
                    copied += 1
            res["rows"].append(
                {
                    "set": set_name,
                    "ref": it["text"],
                    "hyp": hyp,
                    "ms": round(ms),
                    "ctx": len(speech.dictation_context(context)),
                }
            )
        res["sets"][set_name] = {
            "wer": round(100.0 * edits / max(1, total), 2),
            "edits": edits,
            "words": total,
            "copied": copied,
            "ghost": ghost,
        }
    res["p50"] = round(_pct(all_ms, 0.5))
    res["p95"] = round(_pct(all_ms, 0.95))
    res["short_p50"] = round(_pct(short_ms, 0.5))
    res["short_p95"] = round(_pct(short_ms, 0.95))
    return res


def _w(sets: dict, name: str) -> str:
    return "%.2f" % sets[name]["wer"] if name in sets else "—"


def table(results: List[dict]) -> str:
    head = (
        "| config | libri WER | libri-n WER | dict WER | short WER | copied | ghost"
        " | all p50/p95 ms | short p50/p95 ms | load s |"
    )
    lines = [head, "|" + "---|" * 10]
    for r in results:
        s = r["sets"]
        lines.append(
            "| %s | %s | %s | %s | %s | %d | %d | %d / %d | %d / %d | %.1f |"
            % (
                r["config"],
                _w(s, "libri"),
                _w(s, "libri-n"),
                _w(s, "dict"),
                _w(s, "short"),
                s.get("short", {}).get("copied", 0),
                s.get("short", {}).get("ghost", 0),
                r["p50"],
                r["p95"],
                r["short_p50"],
                r["short_p95"],
                r["load_s"],
            )
        )
    return "\n".join(lines)


def cmd_run(args) -> int:
    wanted = args.configs.split(",") if args.configs else DEFAULT
    by = {c[0]: c for c in CONFIGS}
    unknown = [w for w in wanted if w not in by]
    if unknown:
        print("unknown config(s): %s" % ", ".join(unknown))
        return 2
    sets = _sets(args.libri, args.sets)
    hints = vb._hints(voice_words.effective({}))
    print(
        "load average %s; %s; hints %d chars; sets %s"
        % (
            os.getloadavg(),
            "+".join(wanted),
            len(hints),
            {k: len(v) for k, v in sets.items()},
        ),
        flush=True,
    )
    results = []
    out = Path(args.out)
    for name in wanted:
        r = run_config(by[name], sets, hints)
        r["load_avg"] = os.getloadavg()[0]
        results.append(r)
        print(table([r]).splitlines()[-1], "(load %.1f)" % r["load_avg"], flush=True)
        out.write_text(json.dumps(results, indent=1))
    print()
    print(table(results))
    print("\nrows → %s" % out)
    return 0


def rescore(r: dict) -> dict:
    """``r``'s WER, copied and ghost counts again from its saved rows (after
    a change to the scoring: :data:`COMPOUNDS`)."""
    for set_name in list(r["sets"]):
        rows = [x for x in r["rows"] if x["set"] == set_name]
        edits = total = copied = ghost = 0
        for x in rows:
            e, n = vb._wer(_words(x["ref"]), _words(x["hyp"]))
            edits, total = edits + e, total + n
            if set_name == "short":
                ghost += bool(not x["ref"] and _words(x["hyp"]))
                copied += bool(x["ctx"] and _copied(x["hyp"], x["ref"], LONG_CONTEXT))
        r["sets"][set_name].update(
            wer=round(100.0 * edits / max(1, total), 2),
            edits=edits,
            words=total,
            copied=copied,
            ghost=ghost,
        )
    return r


def cmd_rescore(args) -> int:
    results = [rescore(r) for r in json.loads(Path(args.out).read_text())]
    print(table(results))
    return 0


def cmd_timing(args) -> int:
    """Interleaved timing: every config's pass over the SAME phrase back to
    back, the config order shuffled per phrase, ``--reps`` rounds — so a
    busy box slows every config alike and the paired delta against the
    first config (``small``, today's) still means something. Phrases: the
    dictation and short sets (context = the reference text before them,
    :data:`LONG_CONTEXT` for the short ones)."""
    wanted = args.configs.split(",") if args.configs else DEFAULT
    by = {c[0]: c for c in CONFIGS}
    sets = _sets(1)
    hints = vb._hints(voice_words.effective({}))
    runs = {}
    for name in wanted:
        _n, model, beam, ctx, sess = by[name]
        stt = speech.LocalSTT()
        stt.final_model_name = lambda m=model: m  # type: ignore[method-assign]
        stt.final_transcriber("en", hints)(np.zeros(RATE, dtype=np.float32).tobytes())
        runs[name] = (stt, beam, ctx, session_vocabulary(SESSION_REPLY) if sess else "")
    _with_vocabulary("")
    items = sets["dict"] + [it for it in sets["short"] if it["text"]]
    history: Dict[str, List[str]] = {}
    phrases = []
    for it in items:
        before = (
            LONG_CONTEXT
            if it["kind"] == "short"
            else " ".join(history.get(it["group"], []))
        )
        history.setdefault(it["group"], []).append(it["text"])
        phrases.append((it, _read_wav(Path(it["path"])).tobytes(), before))
    ms: Dict[str, List[float]] = {n: [] for n in wanted}
    delta: Dict[str, List[float]] = {n: [] for n in wanted}
    rng = random.Random(5)
    print(
        "load average %s; %d phrases x %d reps"
        % (os.getloadavg(), len(phrases), args.reps),
        flush=True,
    )
    for _rep in range(args.reps):
        for it, audio, before in phrases:
            order = list(wanted)
            rng.shuffle(order)
            got = {}
            for name in order:
                stt, beam, ctx, vocab = runs[name]
                speech.FINAL_BEAM = beam
                _with_vocabulary(vocab)
                run = stt.final_transcriber("en", hints, context=before if ctx else "")
                t = time.monotonic()
                run(audio)
                got[name] = (time.monotonic() - t) * 1000
            for name in wanted:
                ms[name].append(got[name])
                delta[name].append(got[name] - got[wanted[0]])
    print(
        "| config | p50 ms | p95 ms | paired delta p50 vs %s | delta p95 |" % wanted[0]
    )
    print("|---|---|---|---|---|")
    for name in wanted:
        print(
            "| %s | %d | %d | %+d | %+d |"
            % (
                name,
                _pct(ms[name], 0.5),
                _pct(ms[name], 0.95),
                _pct(delta[name], 0.5),
                _pct(delta[name], 0.95),
            )
        )
    print("load average after %s" % (os.getloadavg(),))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render")
    r.add_argument("--seed", type=int, default=11)
    r.set_defaults(fn=cmd_render)
    r = sub.add_parser("run")
    r.add_argument("--configs", default="")
    r.add_argument("--libri", type=int, default=40, help="clips (0: all)")
    r.add_argument(
        "--sets", default="", help="libri,libri-n,dict,short (default: all but libri-n)"
    )
    r.add_argument("--out", default=str(MODELS / "results" / "stt_context.json"))
    r.set_defaults(fn=cmd_run)
    r = sub.add_parser("rescore")
    r.add_argument("--out", default=str(MODELS / "results" / "stt_context.json"))
    r.set_defaults(fn=cmd_rescore)
    r = sub.add_parser("timing")
    r.add_argument("--configs", default="small,small+ctx,small.en,small.en+ctx")
    r.add_argument("--reps", type=int, default=2)
    r.set_defaults(fn=cmd_timing)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
