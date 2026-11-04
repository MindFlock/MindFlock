"""The wake word as the standby really sends it: clips CUT by the clients'
lone-utterance detector out of a simulated microphone stream, decoded the
way ``POST /api/voice/wake`` does (and the ways it could), judged by
``core.wake``. docs/voice-wake.md has the numbers and the decision.

``wake_bench.py`` decodes ideal clips (trimmed to the speech by a sample
threshold). Real use differs in the ways that made the owner's "MindFlock"
miss: the clip is what the standby's 85 ms level blocks cut (a soft onset
or a dip inside the word moves its edges), it sits in room noise at the
microphone's own gain, the speakers are many, and ordinary speech and
non-speech (a cough, a key click, a hum) come in too. Not a unit test —
pytest never collects it. Run with the tool venv (faster-whisper, piper,
numpy)::

    PYTHONPATH=$PWD ~/.local/share/uv/tools/mindflock/bin/python \\
        tests/bench/wake_stream_bench.py            # corpus (once) → decode → score
    ... wake_stream_bench.py corpus                 # make the corpus again
    ... wake_stream_bench.py decode [VARIANT …]     # decode (default: every variant)
    ... wake_stream_bench.py score                  # re-score the cached decodes

* **corpus** → ``stream_clips.npz`` in ``$MINDFLOCK_WAKE_BENCH`` (default
  ``~/.cache/mindflock/wake_bench``): each utterance (:data:`POS`,
  :data:`NEG`, :data:`OTHER` from piper's ``en_US-lessac-medium`` at three
  speeds × two timbres, Windows' SAPI voices through ``powershell.exe``
  when there is one (WSL or Windows) at three rates, and the
  OpenAI-compatible host at ``$VOICE_LAB_URL`` when it answers; plus
  synthetic non-speech) is put in room noise (:data:`ROOMS`) at two
  microphone gains (:data:`GAINS`), fed block by block to :class:`Lone`
  (the port of ``frontend/src/lib/wakeWord.ts`` ``LoneUtterance``; the
  phone's ``voiceWakeStep`` is the same rule on 43 ms blocks) and its first
  candidate's clip kept — or none, a miss before the server is asked.
* **decode** → ``stream_<variant>.json``: each clip hinted ("MindFlock")
  and plain, as :data:`VARIANTS` say, each timed, with each segment's
  ``no_speech_prob`` and ``avg_logprob`` (so ``score`` can apply any
  no-speech filter afterwards). ``server`` is what the route ran before
  this bench: the fast model, greedy, the full 30 s window, faster-whisper's
  temperature fallback and its own no-speech filter.
* **score**: per variant, recall (positives that wake), false wakes
  (look-alikes, ordinary speech, non-speech) and the pass times; the rule
  is ``core.wake`` as it is now (re-read on each run).
"""

from __future__ import annotations

import argparse
import collections
import io
import itertools
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE))

from backend.web.core import wake as W  # noqa: E402
from wake_bench import (  # noqa: E402
    CACHE,
    VOICE_DIR,
    _lab,
    _wav_to_f32,
)

SR = 16000
HINT = "MindFlock"

#: What is said for a wake.
POS = [
    "MindFlock.",
    "MindFlock",
    "Mind flock.",
    "MindFlock?",
    "Hey, MindFlock.",
    "MindFlock!",
]
#: Look-alikes: words that share a part, or a sound, with it.
NEG = [
    "Mind.", "Flock.", "Mindful.", "Minefield.", "Mind the gap.", "Wind lock.",
    "Mild block.", "Line clock.", "Mind fog.", "My flag.", "Mindset.", "Hemlock.",
    "Mine.", "Find out.", "Unlock.", "My lock.", "Mine's locked.", "Mind you.",
]  # fmt: skip
#: Ordinary short speech the standby hears all day.
OTHER = [
    "Stop listening.", "Okay.", "Thanks.", "Sure.", "Yes.", "No.", "Hold on.",
    "What's that?", "Right.", "Okay, cool.", "Good morning.", "Hello.", "Over.",
    "Lunch time.", "My phone.", "Sounds good.", "One second.", "Got it.",
]  # fmt: skip

#: Room noise RMS: a quiet room, a fan.
ROOMS = (0.002, 0.008)
#: The speech's loudest 85 ms block at the microphone (its RMS): a soft
#: voice / a distant mic, and a normal one.
GAINS = (0.06, 0.18)

#: The desktop detector (frontend/src/lib/wakeWord.ts), in ms.
QUIET_MS, TAIL_MS, MIN_MS, MAX_MS, LEAD_MS, RELEARN_MS = 350, 200, 250, 1600, 100, 5000
RMS_SPEECH = 0.02
#: A 4096-frame ScriptProcessor block at 48 kHz, in 16 kHz samples.
BLOCK = 1365


# --------------------------------------------------------------------------- #
# corpus
# --------------------------------------------------------------------------- #
class Lone:
    """``LoneUtterance`` (wakeWord.ts), fed one block's RMS and length."""

    def __init__(self):
        self.floor = 0.0
        self.t = 0.0
        self.quiet = 0.0
        self.start = -1.0
        self.last_loud = 0.0
        self.long = False
        self.prev = -1.0

    def feed(self, rms: float, ms: float):
        at = self.t
        self.t += ms
        learn = self.start < 0 or at - self.start > RELEARN_MS
        if learn:
            self.floor = rms if self.floor == 0 else min(rms, self.floor * 1.05 + 0.001)
        else:
            self.floor = min(self.floor, rms)
        loud = rms > max(RMS_SPEECH, self.floor * 3)
        if self.start < 0:
            if not loud:
                self.quiet += ms
                return None
            if self.quiet < QUIET_MS:
                if self.prev >= 0:  # a pause inside it: carry the stretch on
                    self.start, self.prev = self.prev, -1
                    self.last_loud = self.t
                    self.long = self.last_loud - self.start > MAX_MS
                    return None
                self.quiet = 0
                return None
            self.start = at
            self.prev = -1
            self.last_loud = self.t
            self.long = False
            return None
        if loud:
            self.last_loud = self.t
            if self.last_loud - self.start > MAX_MS:
                self.long = True
            return None
        gap = self.t - self.last_loud
        if gap < TAIL_MS:
            return None
        loud_ms = self.last_loud - self.start
        clip_ms = LEAD_MS + (self.t - self.start)
        long_ = self.long
        self.prev = -1 if long_ else self.start
        self.start = -1
        self.quiet = gap
        if long_ or loud_ms < MIN_MS:
            return None
        return {"loud_ms": loud_ms, "clip_ms": clip_ms, "tail_ms": gap}


def cut(stream):
    """Every candidate clip the detector cuts out of ``stream`` (the clients
    send each: one that comes while a check is in flight is kept for its
    no), with its word-end → send delay (the tail)."""
    import numpy as np

    det = Lone()
    ms = 1000.0 * BLOCK / SR
    out = []
    for i in range(0, len(stream) - BLOCK + 1, BLOCK):
        b = stream[i : i + BLOCK]
        c = det.feed(float(np.sqrt(np.mean(b * b))), ms)
        if c:
            end = i + BLOCK
            n = int(c["clip_ms"] * SR / 1000)
            out.append((stream[max(0, end - n) : end].astype(np.float32), c["tail_ms"]))
    return out


def _trim(x, thr=0.01):
    import numpy as np

    idx = np.where(np.abs(x) > thr * max(1e-6, float(np.abs(x).max())))[0]
    return x[idx[0] : idx[-1] + 1] if len(idx) else x


def _piper(texts):
    from piper.config import SynthesisConfig
    from piper.voice import PiperVoice

    v = PiperVoice.load(str(VOICE_DIR / "piper" / "en_US-lessac-medium.onnx"))
    for t in texts:
        for ls, take in itertools.product((0.8, 1.0, 1.25), range(2)):
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wf:
                cfg = SynthesisConfig(
                    length_scale=ls, noise_scale=0.667 + 0.2 * take, noise_w_scale=0.8
                )
                v.synthesize_wav(t, wf, syn_config=cfg)
            yield "piper ls=%.2f t%d" % (ls, take), t, _wav_to_f32(buf.getvalue())


def _sapi(texts):
    """Windows' System.Speech voices, when ``powershell.exe`` is here."""
    ps = shutil.which("powershell.exe")
    if not ps:
        return
    try:
        temp = subprocess.run(
            [ps, "-NoProfile", "-Command", "$env:TEMP"],
            capture_output=True, text=True, timeout=60,
        ).stdout.strip()  # fmt: skip
        out_win = temp + "\\mf-wake-bench"
        out = Path(
            subprocess.run(["wslpath", "-u", out_win], capture_output=True, text=True).stdout.strip()
            if shutil.which("wslpath") else out_win
        )  # fmt: skip
    except Exception as err:  # noqa: BLE001 — SAPI is optional
        print("sapi: %s" % err, file=sys.stderr)
        return
    jobs = [
        (voice, rate, t, "%s_%d_%d.wav" % (voice.split()[1], rate + 10, i))
        for i, t in enumerate(texts)
        for voice in ("Microsoft David Desktop", "Microsoft Zira Desktop")
        for rate in (-2, 0, 3)
    ]
    lines = [
        "Add-Type -AssemblyName System.Speech",
        "New-Item -ItemType Directory -Force -Path '%s' | Out-Null" % out_win,
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer",
    ]
    for voice, rate, t, name in jobs:
        lines += [
            "$s.SelectVoice('%s'); $s.Rate = %d" % (voice, rate),
            "$s.SetOutputToWaveFile('%s\\%s')" % (out_win, name),
            "$s.Speak('%s')" % t.replace("'", "''"),
        ]
    lines.append("$s.Dispose()")
    # A script file: the jobs are longer than a command line may be.
    out.mkdir(parents=True, exist_ok=True)
    (out / "synth.ps1").write_text("\r\n".join(lines), encoding="utf-8-sig")
    try:
        subprocess.run(
            [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", out_win + "\\synth.ps1"],
            capture_output=True, timeout=900, check=True,
        )  # fmt: skip
    except Exception as err:  # noqa: BLE001
        print("sapi: %s" % err, file=sys.stderr)
        return
    for voice, rate, t, name in jobs:
        f = out / name
        if f.exists():
            yield "sapi %s r%+d" % (voice.split()[1], rate), t, _wav_to_f32(
                f.read_bytes()
            )


def _nonspeech(rng):
    """A cough, key clicks, a hum, a door knock — 3 takes each."""
    import numpy as np

    def env(n, a=0.02, r=0.15):
        e = np.ones(n, np.float32)
        ka, kr = int(a * SR), int(r * SR)
        e[:ka] = np.linspace(0, 1, ka)
        e[-kr:] = np.linspace(1, 0, kr)
        return e

    for k in range(3):
        n = int((0.3 + 0.1 * k) * SR)
        cough = np.convolve(rng.standard_normal(n), np.ones(6) / 6, "same") * env(n)
        yield "synth", "<cough>", cough.astype(np.float32)
        clicks = np.zeros(int(0.9 * SR), np.float32)
        for j in range(4 + k):
            at = int((0.05 + 0.11 * j) * SR)
            clicks[at : at + 160] += rng.standard_normal(160).astype(
                np.float32
            ) * np.linspace(1, 0, 160)
        yield "synth", "<keys>", clicks
        t = np.arange(int(0.5 * SR)) / SR
        f0 = 110 + 40 * k
        hum = sum(np.sin(2 * np.pi * f0 * h * t) / h for h in (1, 2, 3)) * env(len(t))
        yield "synth", "<hum>", hum.astype(np.float32)
        knock = np.zeros(int(0.7 * SR), np.float32)
        for j in range(3):
            at = int((0.05 + 0.2 * j) * SR)
            knock[at : at + 800] += (
                np.sin(2 * np.pi * 180 * np.arange(800) / SR)
                * np.exp(-np.arange(800) / 150)
            ).astype(np.float32)
        yield "synth", "<knock>", knock


def corpus() -> list:
    import numpy as np

    rng = np.random.default_rng(11)
    said = (
        [("pos", t) for t in POS]
        + [("neg", t) for t in NEG]
        + [("other", t) for t in OTHER]
    )
    label = {t: lab for lab, t in said}
    texts = [t for _, t in said]
    src = list(_piper(texts)) + list(_sapi(texts))
    if not os.environ.get("NOLAB"):
        src += list(_lab(POS, takes=2)) + list(_lab(NEG + OTHER, takes=1))
    src += list(_nonspeech(rng))
    clips = []
    for name, text, x in src:
        x = _trim(np.asarray(x, np.float32))
        peak = (
            max(
                float(np.sqrt(np.mean(x[i : i + BLOCK] ** 2)))
                for i in range(0, max(1, len(x) - BLOCK + 1), BLOCK // 2)
            )
            or 1.0
        )
        for room, gain in itertools.product(ROOMS, GAINS):
            noise = lambda n: np.convolve(  # noqa: E731
                rng.standard_normal(n), np.ones(4) / 4, "same"
            ).astype(np.float32)
            lead, tail = np.zeros(int(1.2 * SR), np.float32), np.zeros(SR, np.float32)
            body = x * (gain / peak)
            stream = np.concatenate([lead, body, tail])
            bg = noise(len(stream))
            stream = stream + bg * (room / (float(np.sqrt(np.mean(bg**2))) or 1))
            cands = cut(stream)
            clips.append(
                {
                    "label": label.get(text, "noise"),
                    "src": name,
                    "said": text,
                    "room": room,
                    "gain": gain,
                    "clips": [c for c, _ in cands],
                    "tail_ms": cands[0][1] if cands else None,
                }
            )
    CACHE.mkdir(parents=True, exist_ok=True)
    np.savez(CACHE / "stream_clips.npz", clips=np.array(clips, dtype=object))
    cutn = sum(bool(c["clips"]) for c in clips)
    print(
        "corpus: %d streams, %d cut → %s"
        % (len(clips), cutn, CACHE / "stream_clips.npz"),
        file=sys.stderr,
    )
    return clips


def load_corpus() -> list:
    import numpy as np

    f = CACHE / "stream_clips.npz"
    if not f.exists():
        return corpus()
    return list(np.load(f, allow_pickle=True)["clips"])


# --------------------------------------------------------------------------- #
# decode
# --------------------------------------------------------------------------- #
#: name → how a pass decodes: ``pad`` the encoder input in seconds (0:
#: Whisper's 30), ``fallback`` faster-whisper's temperature fallback,
#: ``filter`` its own no-speech filter (off: every segment kept, ``score``
#: filters), ``tokens`` a cap on the decoded tokens (0: none).
VARIANTS = {
    "server": {"pad": 0, "fallback": True, "filter": True, "tokens": 0},
    "fast": {"pad": 10, "fallback": False, "filter": False, "tokens": 12},
    "fast-nots": {
        "pad": 10,
        "fallback": False,
        "filter": False,
        "tokens": 12,
        "nots": True,
    },
}


def decode(names) -> None:
    import numpy as np
    from faster_whisper import WhisperModel

    from backend.web.core.speech import _padded, whisper_threads

    model = os.environ.get("MODEL", "base.en")
    m = WhisperModel(
        model,
        device="cpu",
        compute_type="int8",
        download_root=str(VOICE_DIR / "whisper"),
        cpu_threads=whisper_threads("fast"),
        num_workers=1,
    )
    every = max(1, int(os.environ.get("EVERY", "1")))
    clips = load_corpus()[::every]

    for name in names:
        v = VARIANTS[name]

        def dec(x, prompt):
            kw = {"language": "en", "beam_size": 1, "vad_filter": False}
            if prompt:
                kw["initial_prompt"] = prompt
            if not v["fallback"]:
                kw["temperature"] = 0.0
            if not v["filter"]:
                kw["no_speech_threshold"] = None
            if v["tokens"]:
                kw["max_new_tokens"] = v["tokens"]
            if v.get("nots"):
                kw["without_timestamps"] = True

            def run():
                segs, _ = m.transcribe(x, **kw)
                return [
                    (s.text.strip(), float(s.no_speech_prob), float(s.avg_logprob))
                    for s in segs
                ]

            t0 = time.monotonic()
            segs = _padded(v["pad"], run) if v["pad"] else run()
            return segs, time.monotonic() - t0

        dec(np.zeros(SR, np.float32), HINT)  # warm
        rows = []
        t0 = time.monotonic()
        for i, c in enumerate(clips):
            base = {k: c[k] for k in ("label", "src", "said", "room", "gain")}
            if not c["clips"]:  # the standby never sent it
                rows.append(base | {"stream": i, "cut": False})
                continue
            # One row per clip sent; ``stream`` ties a stream's clips together.
            for clip in c["clips"]:
                p, tp = dec(clip, HINT)
                n, tn = dec(clip, "")
                rows.append(
                    base
                    | {"stream": i, "cut": True, "p": p, "n": n, "tp": tp, "tn": tn}
                )
            if i % 100 == 0:
                print(
                    "%s: %d/%d %.0fs" % (name, i, len(clips), time.monotonic() - t0),
                    file=sys.stderr,
                )
        out = CACHE / ("stream_%s.json" % name)
        out.write_text(json.dumps(rows))
        print("%s: %d rows → %s" % (name, len(rows), out), file=sys.stderr)


# --------------------------------------------------------------------------- #
# score
# --------------------------------------------------------------------------- #
def text_of(segs, filt=None) -> str:
    """The segments' text; ``filt`` ``(no_speech, logprob)`` drops a segment
    as faster-whisper's filter does (both over / under)."""
    keep = [t for t, nsp, lp in segs if not (filt and nsp > filt[0] and lp < filt[1])]
    return " ".join(keep).strip()


#: name → (no-speech filter on the hinted pass, on the plain one).
FILTERS = {
    "no filter": None,
    "whisper's (0.6, -1.0)": (0.6, -1.0),
}


def score(detail: str = "") -> None:
    corpus_rows = load_corpus()
    print(
        "corpus streams by kind: %s"
        % dict(collections.Counter(c["label"] for c in corpus_rows))
    )
    print(
        "…that the standby sent at least one clip of: %s"
        % dict(collections.Counter(c["label"] for c in corpus_rows if c["clips"]))
    )
    tails = [c["tail_ms"] for c in corpus_rows if c["tail_ms"] is not None]
    if tails:
        print(
            "word end → clip sent (the tail): median %.0f ms\n"
            % statistics.median(tails)
        )
    for name in VARIANTS:
        f = CACHE / ("stream_%s.json" % name)
        if not f.exists():
            continue
        rows = json.loads(f.read_text())
        streams = collections.defaultdict(list)
        for r in rows:
            streams[r.get("stream", id(r))].append(r)
        total = collections.Counter(rs[0]["label"] for rs in streams.values())
        for fname, filt in (
            FILTERS.items() if not VARIANTS[name]["filter"] else [("as run", None)]
        ):
            res = collections.Counter()
            wrong = collections.Counter()
            for rs in streams.values():
                lab, said = rs[0]["label"], rs[0]["said"]
                heard = []
                yes = False
                for r in rs:
                    if not r.get("cut", True):
                        heard.append(("<not sent>", ""))
                        continue
                    p, n = text_of(r["p"], filt), text_of(r["n"], filt)
                    heard.append((p, n))
                    if W.is_wake(p) and not W.veto(n):
                        yes = True
                res[(lab, yes)] += 1
                if yes != (lab == "pos"):
                    wrong[(lab, said, tuple(heard))] += 1
            print(
                "%-10s %-22s recall %3d/%d %3.0f%%   false: look-alike %2d/%d  other %2d/%d  noise %2d/%d"
                % (
                    name, fname,
                    res[("pos", True)], total["pos"], 100 * res[("pos", True)] / max(1, total["pos"]),
                    res[("neg", True)], total["neg"],
                    res[("other", True)], total["other"],
                    res[("noise", True)], total["noise"],
                )
            )  # fmt: skip
            if detail and detail in (name, "all"):
                for (lab, said, heard), k in wrong.most_common(40):
                    print("      %2d× %-6s %-16s %r" % (k, lab, said, heard))
        sent = [r for r in rows if r.get("cut", True)]
        tp = sorted(r["tp"] for r in sent)
        tn = sorted(r["tn"] for r in sent)
        print(
            "%-10s %d clips sent; hinted pass median %.0f ms p90 %.0f ms, plain pass median %.0f ms p90 %.0f ms\n"
            % (
                name, len(sent),
                1000 * statistics.median(tp), 1000 * tp[int(0.9 * (len(tp) - 1))],
                1000 * statistics.median(tn), 1000 * tn[int(0.9 * (len(tn) - 1))],
            )
        )  # fmt: skip


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "step", nargs="?", default="all", choices=("all", "corpus", "decode", "score")
    )
    ap.add_argument("variants", nargs="*")
    ap.add_argument(
        "--detail", default="", help="list the wrong calls of this variant (or 'all')"
    )
    args = ap.parse_args()
    names = args.variants or list(VARIANTS)
    if args.step == "corpus":
        corpus()
        return
    if args.step in ("all", "decode"):
        decode(names)
    if args.step in ("all", "score"):
        score(args.detail)


if __name__ == "__main__":
    main()
