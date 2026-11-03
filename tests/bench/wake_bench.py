"""The wake-word bench: how often the standby's check (``POST
/api/voice/wake``: ``core.speech.LocalSTT.wake_transcribe`` judged by
``core.wake``) wakes on "MindFlock", and how often on something else.

Not a unit test — pytest never collects it (the file name is not
``test_*.py``; ``tests/bench/`` has no ``__init__``). It needs the local
models' packages (faster-whisper, piper, numpy), which the dev venv does not
have; run it with the tool venv::

    PYTHONPATH=$PWD ~/.local/share/uv/tools/mindflock/bin/python \\
        tests/bench/wake_bench.py            # synth (once) → decode (once) → score

    ... wake_bench.py score                  # re-score the cached decodes
    ... wake_bench.py decode                 # decode again (new model / code)
    ... wake_bench.py synth                  # synthesise the clips again

Three steps, each cached in ``$MINDFLOCK_WAKE_BENCH`` (default
``~/.cache/mindflock/wake_bench``; outside the repo — the clips are tens of
MB):

* **synth** → ``clips.npz``: positives (:data:`POS`, "MindFlock.", "Mind
  flock.", "Hey, MindFlock." …) and look-alikes (:data:`NEG`, "Mind.",
  "Mine.", "Mindful.", "Minefield.", "Wind lock." …) from piper's installed
  ``en_US-lessac-medium`` (5 speeds × 2 timbres) and, when it answers, the
  OpenAI-compatible host at ``$VOICE_LAB_URL`` (default
  ``http://127.0.0.1:8770/v1``; voices ``$VOICE_LAB_VOICES``, default
  ``Harvey,Donna``; ``NOLAB=1`` skips it), trimmed to the speech the way the
  clients clip it.
* **decode** → ``probe_<model>.json``: each clip clean and at 15 dB SNR
  (seeded noise), decoded the way the server does — ``$MODEL`` (default
  ``base.en``), greedy, no VAD, no word timestamps (they turn the prompted
  decode into "MindFlock MindFlock MindFlock" loops) — once WITH the hint
  ("MindFlock", ``p``) and once without (``n``), each timed.
* **score**: every rule in :data:`RULES` → recall (positives that wake),
  false wakes (look-alikes that wake), the misses and false wakes by text,
  and the decode times. ``--probe FILE`` scores a decode file from
  elsewhere.

The numbers and the decision are in docs/voice-wake.md.
"""

from __future__ import annotations

import argparse
import collections
import io
import itertools
import json
import os
import re
import statistics
import sys
import time
import urllib.request
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.web.core import wake as W  # noqa: E402

SR = 16000
CACHE = Path(
    os.environ.get("MINDFLOCK_WAKE_BENCH")
    or os.path.expanduser("~/.cache/mindflock/wake_bench")
)
VOICE_DIR = Path(os.path.expanduser("~/.mindflock/voice"))
HINT = "MindFlock"

#: What is said for a wake.
POS = [
    "MindFlock.",
    "MindFlock!",
    "Mind flock.",
    "MindFlock?",
    "Mindflock",
    "Hey, MindFlock.",
]
#: What must not wake: words that share a part, or a sound, with it.
NEG = [
    "Mind.", "Flock.", "My phone.", "Mindful.", "Minefield.", "Blockchain.",
    "Find out.", "Okay.", "Hello.", "Mine.", "Mind the gap.", "Wind lock.",
    "Mild block.", "Line clock.", "Mind fog.", "My flag.", "Mindset.",
    "Bedrock.", "Hemlock.", "Wedlock.",
]  # fmt: skip
SNRS = (None, 15.0)


# --------------------------------------------------------------------------- #
# synth
# --------------------------------------------------------------------------- #
def _resample(x, sr):
    import numpy as np

    if sr == SR:
        return x.astype(np.float32)
    n = int(len(x) * SR / sr)
    return np.interp(
        np.linspace(0, len(x), n, endpoint=False), np.arange(len(x)), x
    ).astype(np.float32)


def _wav_to_f32(data: bytes):
    import numpy as np

    with wave.open(io.BytesIO(data)) as w:
        sr, ch = w.getframerate(), w.getnchannels()
        raw = w.readframes(w.getnframes())
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        a = a.reshape(-1, ch).mean(1)
    return _resample(a, sr)


def _trim(x, thr=0.02):
    """The speech, 100 ms before and 200 ms after (the clients' clip)."""
    import numpy as np

    idx = np.where(np.abs(x) > thr)[0]
    if not len(idx):
        return x
    return x[max(0, idx[0] - int(0.1 * SR)) : idx[-1] + int(0.2 * SR)]


def _piper(texts):
    from piper.config import SynthesisConfig
    from piper.voice import PiperVoice

    v = PiperVoice.load(str(VOICE_DIR / "piper" / "en_US-lessac-medium.onnx"))
    for t in texts:
        for ls, take in itertools.product((0.75, 0.9, 1.0, 1.15, 1.35), range(2)):
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wf:
                cfg = SynthesisConfig(
                    length_scale=ls, noise_scale=0.667 + 0.15 * take, noise_w_scale=0.8
                )
                v.synthesize_wav(t, wf, syn_config=cfg)
            yield "piper ls=%.2f t%d" % (ls, take), t, _trim(
                _wav_to_f32(buf.getvalue())
            )


def _lab(texts, takes):
    url = os.environ.get("VOICE_LAB_URL", "http://127.0.0.1:8770/v1").rstrip("/")
    voices = os.environ.get("VOICE_LAB_VOICES", "Harvey,Donna").split(",")
    for vname, t, k in itertools.product(voices, texts, range(takes)):
        body = json.dumps(
            # The trailing spaces make each take a fresh render (no host cache).
            {
                "model": "tts-1",
                "voice": vname,
                "input": t + " " * k,
                "response_format": "wav",
            }
        ).encode()
        req = urllib.request.Request(
            url + "/audio/speech",
            body,
            {"Content-Type": "application/json", "Cache-Control": "no-cache"},
        )
        try:
            data = urllib.request.urlopen(req, timeout=120).read()
        except Exception as err:  # noqa: BLE001 — the host is optional
            print("voice lab: %s %r: %s" % (vname, t, err), file=sys.stderr)
            if isinstance(err, OSError) and "refused" in str(err).lower():
                return
            continue
        yield "lab %s t%d" % (vname, k), t, _trim(_wav_to_f32(data))


def synth() -> list:
    import numpy as np

    clips = [("pos",) + c for c in _piper(POS)] + [("neg",) + c for c in _piper(NEG)]
    if not os.environ.get("NOLAB"):
        clips += [("pos",) + c for c in _lab(POS, takes=3)]
        clips += [("neg",) + c for c in _lab(NEG, takes=1)]
    CACHE.mkdir(parents=True, exist_ok=True)
    np.savez(CACHE / "clips.npz", clips=np.array(clips, dtype=object))
    print("synth: %d clips → %s" % (len(clips), CACHE / "clips.npz"), file=sys.stderr)
    return clips


def load_clips() -> list:
    import numpy as np

    f = CACHE / "clips.npz"
    if not f.exists():
        return synth()
    return list(np.load(f, allow_pickle=True)["clips"])


# --------------------------------------------------------------------------- #
# decode
# --------------------------------------------------------------------------- #
def _noise(x, snr, rng):
    import numpy as np

    if snr is None:
        return x
    p = np.mean(x**2) + 1e-9
    n = np.convolve(
        rng.standard_normal(len(x)).astype(np.float32), np.ones(8) / 8, mode="same"
    )
    n *= np.sqrt(p / (10 ** (snr / 10)) / (np.mean(n**2) + 1e-12))
    return (x + n).astype(np.float32)


def decode(model_name: str) -> list:
    """Each clip × :data:`SNRS`, hinted and plain, as the wake route runs
    them (``LocalSTT.wake_transcribe`` / ``wake_verify_transcribe``)."""
    import numpy as np
    from faster_whisper import WhisperModel

    m = WhisperModel(
        model_name,
        device="cpu",
        compute_type="int8",
        download_root=str(VOICE_DIR / "whisper"),
    )
    rng = np.random.default_rng(7)

    def dec(x, prompt):
        kw = {"language": "en", "beam_size": 1, "vad_filter": False}
        if prompt:
            kw["initial_prompt"] = prompt
        t0 = time.monotonic()
        segs, _ = m.transcribe(x, **kw)
        text = " ".join(s.text.strip() for s in segs).strip()
        return {"text": text}, time.monotonic() - t0

    rows = []
    clips = load_clips()
    t0 = time.monotonic()
    for i, (lab, src, said, x) in enumerate(clips):
        for snr in SNRS:
            a = _noise(np.asarray(x, dtype=np.float32), snr, rng)
            p, tp = dec(a, HINT)
            n, tn = dec(a, "")
            rows.append(
                {
                    "label": lab,
                    "src": src,
                    "said": said,
                    "snr": snr,
                    "p": p,
                    "n": n,
                    "tp": tp,
                    "tn": tn,
                }
            )
        if i % 40 == 0:
            print(
                "decode: %d/%d %.0fs" % (i, len(clips), time.monotonic() - t0),
                file=sys.stderr,
            )
    out = CACHE / ("probe_%s.json" % model_name)
    out.write_text(json.dumps(rows))
    print("decode: %d rows → %s" % (len(rows), out), file=sys.stderr)
    return rows


# --------------------------------------------------------------------------- #
# score
# --------------------------------------------------------------------------- #
#: ``voice_words.HEARD["mindflock"]`` before this bench.
_LEGACY_FORMS = (
    "mind flock", "mine flock", "mind lock", "mindlock", "my flock",
    "mind block", "mind flog", "min flock", "mindflocked",
)  # fmt: skip


def _legacy_is_wake(text: str) -> bool:
    """The rule before this bench (2-edit slack, at most one token more, no
    greeting dropped), kept here to compare against."""
    raw = (text or "").strip()
    tokens = [t for t in re.split(r"[^A-Za-z]+", raw) if t]
    if not tokens or len(tokens) > 2:
        return False
    heard = W.normalize(raw)
    forms = {W.normalize(f) for f in _LEGACY_FORMS}
    if heard in forms:
        return True
    return abs(len(heard) - 9) <= 2 and W._edits(heard, W.WAKE_WORD) <= 2


#: The brief's veto (the plain decode is one of these words), to compare.
_SET_VETO = {
    "mind",
    "mine",
    "mindset",
    "minefield",
    "mindthegap",
    "mindful",
    "mindfully",
    "mined",
    "minds",
}


def _p(r):
    return r["p"]["text"]


def _n(r):
    return r["n"]["text"]


#: name → (row → wake?). The last is what the server runs.
RULES = {
    "before: 2-edit slack, prompted": lambda r: _legacy_is_wake(_p(r)),
    "plain only, strict": lambda r: W.is_wake(_n(r)),
    "strict, prompted only": lambda r: W.is_wake(_p(r)),
    "strict + set veto": lambda r: W.is_wake(_p(r))
    and W.normalize(" ".join(W._tokens(_n(r)))) not in _SET_VETO,
    "SHIPPED: strict + veto": lambda r: W.is_wake(_p(r)) and not W.veto(_n(r)),
}


def score(rows: list, detail: str = "SHIPPED: strict + veto") -> None:
    pos = [r for r in rows if r["label"] == "pos"]
    neg = [r for r in rows if r["label"] == "neg"]
    print(
        "%d positives, %d look-alikes (clips × %d noise levels)\n"
        % (len(pos), len(neg), len(SNRS))
    )
    print("%-34s %16s %16s" % ("rule", "recall", "false wakes"))
    for name, f in RULES.items():
        tp = sum(bool(f(r)) for r in pos)
        fp = sum(bool(f(r)) for r in neg)
        print(
            "%-34s %4d/%d %4.0f%% %4d/%d %4.0f%%"
            % (
                name,
                tp,
                len(pos),
                100 * tp / max(1, len(pos)),
                fp,
                len(neg),
                100 * fp / max(1, len(neg)),
            )
        )
    f = RULES[detail]
    print("\n%s — misses (prompted | plain):" % detail)
    for (p, n), c in collections.Counter(
        (_p(r), _n(r)) for r in pos if not f(r)
    ).most_common(15):
        print("  %2d× %r | %r" % (c, p, n))
    print("%s — false wakes (said: prompted | plain):" % detail)
    for (s, p, n), c in collections.Counter(
        (r["said"], _p(r), _n(r)) for r in neg if f(r)
    ).most_common(15):
        print("  %2d× %s: %r | %r" % (c, s, p, n))
    for key, label in (("tp", "prompted pass"), ("tn", "plain pass")):
        ts = sorted(r[key] for r in rows if r.get(key))
        if ts:
            print(
                "%s: median %.0f ms, p90 %.0f ms"
                % (
                    label,
                    1000 * statistics.median(ts),
                    1000 * ts[int(0.9 * (len(ts) - 1))],
                )
            )
    # The second pass runs only on a prompted yes.
    shipped = sum(W.is_wake(_p(r)) for r in rows)
    print("plain pass runs on %d/%d checks (prompted yes only)" % (shipped, len(rows)))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "step", nargs="?", default="all", choices=("all", "synth", "decode", "score")
    )
    ap.add_argument("--probe", help="score this decode file instead")
    args = ap.parse_args()
    model = os.environ.get("MODEL", "base.en")
    probe = Path(args.probe) if args.probe else CACHE / ("probe_%s.json" % model)
    if args.step == "synth":
        synth()
        return
    if args.step == "decode" or (args.step == "all" and not probe.exists()):
        rows = decode(model)
    else:
        rows = json.loads(probe.read_text())
    if args.step != "decode":
        score(rows)


if __name__ == "__main__":
    main()
