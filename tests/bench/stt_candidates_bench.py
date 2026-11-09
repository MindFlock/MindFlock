"""The STT candidates bench: alternative speech-to-text models measured on
this machine against what ``core.speech.LocalSTT`` runs today
(faster-whisper ``base.en`` streaming + ``small`` final). Results and the
recommendation live in ``notes/stt-2026-11/bench.md``.

Not a unit test — pytest never collects it. Three venvs are involved, never
the server's for anything but piper:

1. **render** (the uv TOOL venv, which has piper — read-only use)::

       PYTHONPATH=$PWD ~/.local/share/uv/tools/mindflock/bin/python \\
           tests/bench/stt_candidates_bench.py render

   synthesises :mod:`voice_bench`'s phrase list (same seed, same
   :func:`voice_bench.synthesise`: varied speed/timbre, 22–35 dB SNR noise,
   300–500 ms of room either side) to ``$STT_BENCH_DATA/piper``.

2. **fetch** / **run** / **all** / **report** (a scratch venv, see the note)::

       uv venv --python 3.12 ~/stt-bench-venv
       uv pip install --python ~/stt-bench-venv/bin/python faster-whisper==1.2.1 \\
           numpy soundfile psutil pyarrow huggingface_hub 'onnx-asr[cpu,hub]' \\
           sherpa-onnx moonshine-voice
       B="PYTHONPATH=$PWD ~/stt-bench-venv/bin/python tests/bench/stt_candidates_bench.py"
       $B fetch                       # librispeech_asr_dummy → $STT_BENCH_DATA/libri
       $B run fw-small+hints --set piper
       $B all --set piper,libri       # every candidate, one subprocess each
       $B report                      # markdown tables from the saved JSON

``fetch`` pulls ``hf-internal-testing/librispeech_asr_dummy`` (73 real
read-speech utterances, clean) and pads each with the same kind of room as
the piper clips (300–500 ms of −54 dBFS noise either side) so the streaming
numbers mean the same thing on both sets.

Every candidate runs in its own process (``all`` spawns one per config,
strictly one at a time, and first waits until other processes use fewer
than :data:`MAX_LOAD` cores, sampled from ``/proc/stat`` — the 1-minute load
average still counts the previous config's threads), so peak RSS is that
model's alone. Per
candidate and data set it reports:

* **WER** — :func:`voice_bench._wer` over :func:`voice_bench._norm`
  (punctuation dropped, digits and number words folded);
* **ctl** — the control word heard where it belongs, judged by
  :func:`voice_bench._control` (piper set only — real speech has none);
* **one-shot p50/p95** — the wall time of transcribing the whole clip at
  once (= what an accurate ``final`` costs after the person stops), after a
  warm-up decode; **RTF** = Σ decode time / Σ audio;
* **cores** — process CPU seconds / wall seconds during those decodes;
* **load s**, **RSS load / peak MB** (``ru_maxrss``);
* for TRUE streaming models (fed 160 ms chunks on a simulated clock: a
  chunk is sent once spoken, and when the engine lags it gets everything
  spoken since its last answer in one go): **first p50/p90** — ms from the
  clip's start until the first word shows; **final p50/p95** — ms from the
  clip's end (its trailing room included) until the final text, backlog
  included. ``prod-stream`` runs the real ``speech_stream.Stream`` through
  :func:`voice_bench.run_config` (150 then 300 ms chunks) for the same
  columns on today's pipeline.

Data and models stay outside the repo: ``STT_BENCH_DATA`` (default
``~/stt-bench-models/data``), ``STT_BENCH_MODELS`` (default
``~/stt-bench-models``; HF downloads go to ``$STT_BENCH_MODELS/hf``,
faster-whisper's to ``$STT_BENCH_MODELS/mfvoice/whisper`` via
``MINDFLOCK_VOICE_DIR`` — never the live ``~/.mindflock/voice``).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import resource
import subprocess
import sys
import time
import wave
from pathlib import Path
from typing import Dict, List, Optional, Sequence

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
os.environ.setdefault("HF_HOME", str(MODELS / "hf"))
os.environ.setdefault("MINDFLOCK_VOICE_DIR", str(MODELS / "mfvoice"))
os.environ.setdefault("MOONSHINE_VOICE_CACHE", str(MODELS / "moonshine"))
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TQDM_DISABLE", "1")

import numpy as np  # noqa: E402

import voice_bench as vb  # noqa: E402  (sets a bare MINDFLOCK_SETTINGS_FILE)

RATE = 16000
CHUNK_S = 0.16
MAX_LOAD = 3.0
SHERPA = MODELS / "sherpa"


# --------------------------------------------------------------------------- #
# Data
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


def cmd_render(args) -> int:
    from piper.voice import PiperVoice

    words = vb.voice_words.effective({})
    rng = random.Random(args.seed)
    # The live piper voice, read only (vb.PIPER follows MINDFLOCK_VOICE_DIR,
    # which this module points at the scratch model dir).
    piper = os.environ.get("STT_BENCH_PIPER") or os.path.expanduser(
        "~/.mindflock/voice/piper/en_US-lessac-medium.onnx"
    )
    voice = PiperVoice.load(piper)
    out = DATA / "piper"
    out.mkdir(parents=True, exist_ok=True)
    items = []
    for i, (text, ref, check) in enumerate(vb.phrases(words)):
        samples = vb.synthesise(voice, text, rng)
        name = "%03d.wav" % i
        _write_wav(out / name, samples)
        items.append(
            {
                "wav": name,
                "text": text,
                "check": list(check),
                "dur": len(samples) / RATE,
            }
        )
    meta = {"words": words, "hints": vb._hints(words), "items": items}
    (out / "manifest.json").write_text(json.dumps(meta, indent=1))
    print("%d clips → %s" % (len(items), out))
    return 0


def cmd_fetch(args) -> int:
    import io

    import pyarrow.parquet as pq
    import soundfile as sf
    from huggingface_hub import hf_hub_download

    path = hf_hub_download(
        "hf-internal-testing/librispeech_asr_dummy",
        "clean/validation-00000-of-00001.parquet",
        repo_type="dataset",
    )
    table = pq.read_table(path).to_pylist()
    out = DATA / "libri"
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(args.seed)
    items = []
    for i, row in enumerate(table):
        audio, rate = sf.read(io.BytesIO(row["audio"]["bytes"]), dtype="float32")
        assert rate == RATE, rate
        lead = rng.standard_normal(int(RATE * rng.uniform(0.3, 0.5))) * 0.002
        tail = rng.standard_normal(int(RATE * rng.uniform(0.3, 0.5))) * 0.002
        samples = np.concatenate([lead, audio, tail]).astype(np.float32)
        name = "%03d.wav" % i
        _write_wav(out / name, samples)
        items.append(
            {
                "wav": name,
                "text": row["text"],
                "check": None,
                "dur": len(samples) / RATE,
            }
        )
    (out / "manifest.json").write_text(json.dumps({"items": items}, indent=1))
    print("%d clips → %s (%.0f s)" % (len(items), out, sum(x["dur"] for x in items)))
    return 0


def load_set(name: str, limit: int = 0):
    meta = json.loads((DATA / name / "manifest.json").read_text())
    items = meta["items"][: limit or None]
    for it in items:
        it["samples"] = _read_wav(DATA / name / it["wav"])
    piper = json.loads((DATA / "piper" / "manifest.json").read_text())
    return items, piper["words"], piper["hints"]


# --------------------------------------------------------------------------- #
# Engines. One-shot: transcribe(samples) → text. Streaming: new_stream() → an
# object with feed(samples) → text so far and finish() → final text.
# --------------------------------------------------------------------------- #
class Engine:
    streaming = False
    biasing = "none"

    def transcribe(self, samples: np.ndarray) -> str:
        s = self.new_stream()
        s.feed(samples)
        return s.finish()

    def new_stream(self):  # pragma: no cover - streaming engines only
        raise NotImplementedError


def hint_terms(hints: str) -> List[str]:
    return [h.strip() for h in hints.split(",") if h.strip()]


class FasterWhisper(Engine):
    """``LocalSTT.final_transcriber`` itself (greedy, VAD, ``hotwords`` from
    the production hint string when ``hints``), with ``name`` as the final
    model: the exact production path, at the final tier's thread budget."""

    biasing = "hotwords (decoder prompt; production uses it on the final)"

    def __init__(self, name: str, hints: str):
        from types import SimpleNamespace

        speech = vb.speech
        speech._voice = lambda: SimpleNamespace(  # type: ignore[assignment]
            stt_backend="local",
            stt_model=name,
            stt_stream_model="base.en" if name != "base.en" else "tiny.en",
            stt_final=name,
        )
        self.run = speech.LocalSTT().final_transcriber("en", hints=hints)
        if not hints:
            self.biasing = "hotwords supported (off in this config)"
        self.run(np.zeros(RATE, dtype=np.float32).tobytes())  # load + prime

    def transcribe(self, samples):
        return self.run(samples.astype(np.float32).tobytes())


class OnnxAsr(Engine):
    biasing = "none (onnx-asr has no context biasing; sherpa-onnx's TDT path does)"

    def __init__(self, model: str, threads: int, path: Optional[Path] = None):
        import onnx_asr
        import onnxruntime as ort

        so = ort.SessionOptions()
        so.intra_op_num_threads = threads
        so.inter_op_num_threads = 1
        self.m = onnx_asr.load_model(model, path, quantization="int8", sess_options=so)
        self.eou = 0  # utterances whose text carried an <EOU> token

    def transcribe(self, samples):
        text = str(self.m.recognize(samples, sample_rate=RATE))
        if "<EOU>" in text:
            self.eou += 1
        return text.replace("<EOU>", " ").replace("<EOB>", " ").strip()


class SherpaOffline(Engine):
    """A sherpa-onnx offline NeMo transducer (Parakeet TDT), optionally
    with ``hints`` as hotwords: contextual biasing needs
    ``modified_beam_search`` and a ``bpe.vocab`` the package does not ship,
    so one is written from ``tokens.txt`` (every piece scored −1: the
    encoder then picks the fewest pieces)."""

    def __init__(self, folder: str, threads: int, hints: str, score: float = 2.0):
        import sherpa_onnx

        d = SHERPA / folder
        kw = {}
        self.biasing = (
            "hotwords supported via modified_beam_search (off in this config)"
        )
        if hints:
            vocab = MODELS / "results" / ("bpe-%s.vocab" % folder)
            vocab.parent.mkdir(parents=True, exist_ok=True)
            pieces = [
                ln.rsplit(" ", 1)[0]
                for ln in (d / "tokens.txt").read_text().splitlines()
                if ln.strip()
            ]
            vocab.write_text(
                "".join("%s\t-1\n" % p for p in pieces if not p.startswith("<"))
            )
            hw = MODELS / "results" / ("hotwords-%s.txt" % folder)
            hw.write_text("\n".join(hint_terms(hints)))
            kw = dict(
                decoding_method="modified_beam_search",
                hotwords_file=str(hw),
                hotwords_score=score,
                modeling_unit="bpe",
                bpe_vocab=str(vocab),
            )
            self.biasing = "hotwords (on, score %.1f)" % score
        self.rec = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=str(d / "encoder.int8.onnx"),
            decoder=str(d / "decoder.int8.onnx"),
            joiner=str(d / "joiner.int8.onnx"),
            tokens=str(d / "tokens.txt"),
            num_threads=threads,
            model_type="nemo_transducer",
            **kw,
        )

    def transcribe(self, samples):
        s = self.rec.create_stream()
        s.accept_waveform(RATE, samples)
        self.rec.decode_stream(s)
        return s.result.text.strip()


class EouRnnt(Engine):
    """nvidia/parakeet_realtime_eou_120m-v1 from the community ONNX export
    ``ysdede/parakeet-realtime-eou-120m-v1-onnx`` (encoder + decoder_joint,
    no cache inputs — so OFFLINE only: the cache-aware streaming encoder
    needs a driver neither onnx-asr nor sherpa-onnx ships). 128 log-mels
    (onnx-asr's NeMo filterbank), greedy RNNT; records whether ``<EOU>``
    came out at the end."""

    biasing = "none"

    def __init__(self, threads: int, normalize: bool = False):
        import onnxruntime as ort
        from onnx_asr.preprocessors.numpy_preprocessor import NemoPreprocessorNumpy

        d = MODELS / "eou"
        so = ort.SessionOptions()
        so.intra_op_num_threads = threads
        self.enc = ort.InferenceSession(str(d / "encoder-model.int8.onnx"), so)
        self.dec = ort.InferenceSession(str(d / "decoder_joint-model.int8.onnx"), so)
        self.vocab = [t for t in (d / "vocab.txt").read_text().split("\n") if t]
        self.blank = len(self.vocab)
        self.pre = NemoPreprocessorNumpy("nemo128")
        self.normalize = normalize
        self.eou = 0

    def _features(self, samples):
        if self.normalize:
            f, _ = self.pre(
                samples[None, :].astype(np.float32), np.array([len(samples)])
            )
            return f.astype(np.float32)
        p = self.pre
        w = samples[None, :].astype(np.float32)
        w = w - p._preemph * np.pad(w, ((0, 0), (1, 0)))[:, :-1]
        w = np.pad(w, ((0, 0), (p._n_fft // 2, p._n_fft // 2)))
        win = np.lib.stride_tricks.sliding_window_view(w, p._n_fft, axis=1)[
            :, :: p._hop_length
        ]
        pad = (p._n_fft - p._win_length) // 2
        win = win * np.pad(np.hanning(p._win_length), (pad, pad))
        spec = np.abs(np.fft.rfft(win, p._n_fft)).astype(np.float32) ** 2
        mel = np.log(np.matmul(spec, p._melscale_fbanks) + p._log_zero_guard_value)
        return mel.transpose(0, 2, 1).astype(np.float32)

    def transcribe(self, samples):
        feats = self._features(samples)
        (enc,) = self.enc.run(
            None,
            {
                "audio_signal": feats,
                "length": np.array([feats.shape[2]], dtype=np.int64),
            },
        )
        s1 = np.zeros((1, 1, 640), dtype=np.float32)
        s2 = np.zeros((1, 1, 640), dtype=np.float32)
        last, out = self.blank, []
        for t in range(enc.shape[2]):
            frame = enc[:, :, t : t + 1]
            for _ in range(10):
                logits, n1, n2 = self.dec.run(
                    None,
                    {
                        "encoder_outputs": frame,
                        "targets": np.array([[last]], dtype=np.int32),
                        "input_states_1": s1,
                        "input_states_2": s2,
                    },
                )
                k = int(logits.reshape(-1)[-(self.blank + 1) :].argmax())
                if k == self.blank:
                    break
                out.append(k)
                last, s1, s2 = k, n1, n2
        toks = [self.vocab[k] for k in out]
        if "<EOU>" in toks:
            self.eou += 1
        text = "".join(t for t in toks if not t.startswith("<"))
        return text.replace("\u2581", " ").strip()


class Moonshine(Engine):
    def __init__(self, arch: str, hints: str):
        from moonshine_voice import ModelArch, get_model_for_language
        from moonshine_voice.transcriber import Transcriber

        a = ModelArch[arch.upper().replace("-", "_")]
        path, a = get_model_for_language("en", a, cache_root=MODELS / "moonshine")
        self.streaming = "STREAMING" in a.name
        self.t = Transcriber(path, a)
        if hints and self.streaming:
            self.t.set_keyterms(hint_terms(hints))
            self.biasing = "keyterms (on)"
        else:
            self.biasing = (
                "keyterms (streaming archs only)"
                if self.streaming
                else "none (non-streaming arch)"
            )

    @staticmethod
    def _text(tr) -> str:
        return " ".join(
            line.text.strip() for line in (tr.lines if tr else []) if line.text
        )

    def transcribe(self, samples):
        if not self.streaming:
            return self._text(
                self.t.transcribe_without_streaming(samples.tolist(), RATE)
            )
        return Engine.transcribe(self, samples)

    def new_stream(self):
        t = self.t
        text = self._text

        class S:
            def __init__(self):
                self.s = t.create_stream(update_interval=1e9)
                self.s.start()

            def feed(self, samples):
                self.s.add_audio(samples.tolist(), RATE)
                return text(self.s.update_transcription())

            def finish(self):
                out = text(self.s.stop())
                self.s.close() if hasattr(self.s, "close") else None
                return out

        return S()


class SherpaOnline(Engine):
    streaming = True

    def __init__(self, folder: str, threads: int, hints: str, tail_s: float = 0.6):
        import sherpa_onnx

        d = SHERPA / folder

        def pick(stem: str) -> str:
            got = sorted(d.glob(stem + "*.int8.onnx")) or sorted(
                d.glob(stem + "*.onnx")
            )
            return str(got[0])

        kw = {}
        self.biasing = (
            "off here; sherpa hotwords need modified_beam_search + a bpe.vocab"
        )
        if hints:
            hw = MODELS / "results" / ("hotwords-%s.txt" % folder)
            hw.parent.mkdir(parents=True, exist_ok=True)
            hw.write_text(
                "\n".join(h.upper() if self._upper(d) else h for h in hint_terms(hints))
            )
            self.biasing = "hotwords (on)"
            kw = dict(
                decoding_method="modified_beam_search",
                hotwords_file=str(hw),
                hotwords_score=1.5,
                modeling_unit="bpe",
                bpe_vocab=str(d / "bpe.vocab"),
            )
        self.rec = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=str(d / "tokens.txt"),
            encoder=pick("encoder"),
            decoder=pick("decoder"),
            joiner=pick("joiner"),
            num_threads=threads,
            **kw,
        )
        self.tail = np.zeros(int(tail_s * RATE), dtype=np.float32)

    @staticmethod
    def _upper(d: Path) -> bool:
        toks = (d / "tokens.txt").read_text().split()
        letters = "".join(t for t in toks if t.isalpha())
        return sum(c.isupper() for c in letters) > sum(c.islower() for c in letters)

    def new_stream(self):
        rec, tail = self.rec, self.tail

        class S:
            def __init__(self):
                self.s = rec.create_stream()

            def _run(self):
                while rec.is_ready(self.s):
                    rec.decode_stream(self.s)
                return rec.get_result(self.s).strip()

            def feed(self, samples):
                self.s.accept_waveform(RATE, samples)
                return self._run()

            def finish(self):
                self.s.accept_waveform(RATE, tail)
                self.s.input_finished()
                return self._run()

        return S()


#: name → (factory(threads, hints) → Engine, uses hints, default limit)
def _candidates() -> Dict[str, tuple]:
    c: Dict[str, tuple] = {}
    for m in ("base.en", "small", "small.en", "large-v3-turbo", "distil-large-v3.5"):
        big = m.startswith(("large", "distil-large"))
        c["fw-" + m] = (lambda t, h, m=m: FasterWhisper(m, ""), 20 if big else 0)
        c["fw-%s+hints" % m] = (lambda t, h, m=m: FasterWhisper(m, h), 20 if big else 0)
    c["parakeet-tdt-v2"] = (lambda t, h: OnnxAsr("nemo-parakeet-tdt-0.6b-v2", t), 0)
    c["parakeet-tdt-v3"] = (lambda t, h: OnnxAsr("nemo-parakeet-tdt-0.6b-v3", t), 0)
    tdt = "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8"
    c["sherpa-parakeet-tdt-v2"] = (lambda t, h: SherpaOffline(tdt, t, ""), 0)
    c["sherpa-parakeet-tdt-v2+hints"] = (lambda t, h: SherpaOffline(tdt, t, h), 0)
    for sc in ("0.5", "1.0"):
        c["sherpa-parakeet-tdt-v2+hints" + sc] = (
            lambda t, h, sc=sc: SherpaOffline(tdt, t, h, float(sc)),
            0,
        )
    c["parakeet-eou-120m-offline"] = (lambda t, h: EouRnnt(t), 0)
    for a in ("tiny", "base", "tiny-streaming", "small-streaming", "medium-streaming"):
        c["moonshine-" + a] = (lambda t, h, a=a: Moonshine(a, ""), 0)
        if "streaming" in a:
            c["moonshine-%s+hints" % a] = (lambda t, h, a=a: Moonshine(a, h), 0)
    sherpa = {
        "zipformer-en": "sherpa-onnx-streaming-zipformer-en-2023-06-26",
        "kroko-en": "sherpa-onnx-streaming-zipformer-en-kroko-2025-08-06",
        "fastconformer-80ms": "sherpa-onnx-nemo-streaming-fast-conformer-transducer-en-80ms-int8",
        "nemotron-3.5-160ms": "sherpa-onnx-nemotron-3.5-asr-streaming-0.6b-160ms-int8-2026-06-11",
    }
    for k, folder in sherpa.items():
        c["sherpa-" + k] = (lambda t, h, f=folder: SherpaOnline(f, t, ""), 0)
    # Only this one ships the bpe model hotwords are encoded with.
    c["sherpa-zipformer-en+hints"] = (
        lambda t, h: SherpaOnline(sherpa["zipformer-en"], t, h),
        0,
    )
    return c


# --------------------------------------------------------------------------- #
# Measuring
# --------------------------------------------------------------------------- #
def _pct(values: Sequence[float], q: float) -> float:
    return vb._pct(list(values), q)


def _cpu() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


def _peak_mb() -> int:
    """Peak RSS in MB (Linux ``ru_maxrss`` is KiB)."""
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024 / 1e6)


def _rss_mb() -> float:
    try:
        import psutil

        return psutil.Process().memory_info().rss / 1e6
    except Exception:  # noqa: BLE001
        return 0.0


def _stream_one(engine: Engine, samples: np.ndarray):
    """Feed ``samples`` at real-time pace in 160 ms chunks on a simulated
    clock → (first-word s from clip start or None, final latency s after
    clip end, final text, passes)."""
    n, pos, clock = len(samples), 0, 0.0
    step = int(CHUNK_S * RATE)
    first = None
    s = engine.new_stream()
    passes = 0
    while pos < n:
        upto = min(n, max(pos + step, int(clock * RATE)))
        clock = max(clock, upto / RATE)
        t0 = time.monotonic()
        text = s.feed(samples[pos:upto])
        clock += time.monotonic() - t0
        passes += 1
        pos = upto
        if first is None and text.strip():
            first = clock
    t0 = time.monotonic()
    final = s.finish()
    clock += time.monotonic() - t0
    return first, clock - n / RATE, final, passes


def run_candidate(name: str, set_name: str, threads: int, limit: int) -> dict:
    cands = _candidates()
    factory, default_limit = cands[name]
    clips, words, hints = load_set(set_name, limit or default_limit)
    load_start = os.getloadavg()[0]
    busy_start = _busy_cores(2.0)
    rss0 = _rss_mb()
    t0 = time.monotonic()
    engine = factory(threads, hints)
    engine.transcribe(np.zeros(RATE, dtype=np.float32))  # warm-up
    engine.transcribe(clips[0]["samples"])
    load_s = time.monotonic() - t0
    rss_load = _rss_mb()
    rows, lat, audio = [], [], 0.0
    cpu0, wall0 = _cpu(), 0.0
    for c in clips:
        t = time.monotonic()
        text = engine.transcribe(c["samples"])
        dt = time.monotonic() - t
        wall0 += dt
        lat.append(dt * 1000)
        audio += c["dur"]
        rows.append(
            {"wav": c["wav"], "ref": c["text"], "oneshot": text, "ms": round(dt * 1000)}
        )
    cores = (_cpu() - cpu0) / max(wall0, 1e-9)
    firsts, finals = [], []
    if engine.streaming:
        for c, r in zip(clips, rows):
            first, fin, text, passes = _stream_one(engine, c["samples"])
            if first is not None:
                firsts.append(first * 1000)
            finals.append(fin * 1000)
            r.update(
                stream=text,
                first_ms=first and round(first * 1000),
                final_ms=round(fin * 1000),
                passes=passes,
            )
    out = {
        "candidate": name,
        "set": set_name,
        "n": len(clips),
        "threads": threads,
        "biasing": engine.biasing,
        "streaming": engine.streaming,
        "load_s": round(load_s, 2),
        "rss_start_mb": round(rss0),
        "rss_load_mb": round(rss_load),
        "rss_peak_mb": _peak_mb(),
        "oneshot_p50": round(_pct(lat, 0.5)),
        "oneshot_p95": round(_pct(lat, 0.95)),
        "rtf": round(sum(lat) / 1000 / audio, 3),
        "cores": round(cores, 1),
        "first_p50": round(_pct(firsts, 0.5)) if firsts else None,
        "first_p90": round(_pct(firsts, 0.9)) if firsts else None,
        "final_p50": round(_pct(finals, 0.5)) if finals else None,
        "final_p95": round(_pct(finals, 0.95)) if finals else None,
        "eou_tokens": getattr(engine, "eou", None),
        "load_start": round(load_start, 2),
        "busy_start": round(busy_start, 1),
        "load_end": round(os.getloadavg()[0], 2),
        "rows": rows,
    }
    _score(out, clips, words)
    return out


def _joined(text: str, words: dict) -> str:
    """``text`` with its first two words joined when that makes the wake
    word ("Mind flock" → "mindflock"): what a model without vocabulary
    biasing writes, and a one-line fix on the server's side."""
    w = vb._norm(text)
    if len(w) > 1 and vb._close(w[0] + w[1], words["wake"].lower()):
        return " ".join([w[0] + w[1]] + w[2:])
    return text


def _score(out: dict, clips, words) -> None:
    """WER, CER (letters only: "work tree" = "worktree"), and the control
    word judged strictly (``ctl``) and with the wake word's halves joined
    (``ctlj``)."""
    for key in ("oneshot", "stream"):
        if key not in out["rows"][0]:
            continue
        edits = refn = cedits = crefn = hits = jhits = checked = 0
        for c, r in zip(clips, out["rows"]):
            ref, hyp = vb._norm(c["text"]), vb._norm(r[key])
            e, n = vb._wer(ref, hyp)
            edits += e
            refn += n
            e, n = vb._wer(list("".join(ref)), list("".join(hyp)))
            cedits += e
            crefn += n
            if c["check"]:
                checked += 1
                hit, _ = vb._control(r[key], tuple(c["check"]), words)
                jhit, _ = vb._control(_joined(r[key], words), tuple(c["check"]), words)
                hits += hit
                jhits += jhit
                r[key + "_ctl"] = hit
        out["wer_" + key] = round(100.0 * edits / max(1, refn), 1)
        out["cer_" + key] = round(100.0 * cedits / max(1, crefn), 1)
        out["ctl_" + key] = "%d/%d" % (hits, checked) if checked else None
        out["ctlj_" + key] = "%d/%d" % (jhits, checked) if checked else None


def run_prod_stream(set_name: str) -> dict:
    """Today's pipeline: voice_bench's real ``speech_stream.Stream`` with
    base.en (fast) + small (final, production hints)."""
    clips, words, hints = load_set(set_name)
    vclips = [
        (
            c["text"],
            c["text"],
            tuple(c["check"]) if c["check"] else ("end", "send"),
            c["samples"],
        )
        for c in clips
    ]
    cfg = vb.Config("base.en+small")
    rss0 = _rss_mb()
    t0 = time.monotonic()
    vb.speech.LocalSTT  # noqa: B018 — loaded inside run_config (its warm())
    row, _misses = vb.run_config(cfg, vclips, words, hints, 1, random.Random(7), False)
    total = time.monotonic() - t0
    return {
        "candidate": "prod-stream",
        "set": set_name,
        "n": len(clips),
        "threads": "2 fast + 6 final",
        "biasing": "hotwords on the small final",
        "streaming": True,
        "voice_bench": row,
        "wall_s": round(total, 1),
        "rss_peak_mb": _peak_mb(),
        "rss_start_mb": round(rss0),
    }


def cmd_run(args) -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    for set_name in args.set.split(","):
        if args.candidate == "prod-stream":
            out = run_prod_stream(set_name)
        else:
            out = run_candidate(args.candidate, set_name, args.threads, args.limit)
        path = RESULTS / ("%s__%s.json" % (args.candidate, set_name))
        path.write_text(json.dumps(out, indent=1))
        brief = {k: v for k, v in out.items() if k != "rows"}
        print(json.dumps(brief))
    return 0


def _load1() -> float:
    return os.getloadavg()[0]


def _busy_cores(seconds: float = 3.0) -> float:
    """Cores busy right now (``/proc/stat`` over ``seconds``): unlike the
    1-minute load average it does not still count the previous config."""

    def snap():
        with open("/proc/stat") as f:
            v = [int(x) for x in f.readline().split()[1:]]
        return sum(v), v[3] + v[4]  # total, idle + iowait

    t0, i0 = snap()
    time.sleep(seconds)
    t1, i1 = snap()
    return (os.cpu_count() or 1) * (1 - (i1 - i0) / max(1, t1 - t0))


def cmd_all(args) -> int:
    names = args.candidates.split(",") if args.candidates else list(_candidates())
    for name in names:
        for set_name in args.set.split(","):
            if (
                RESULTS / ("%s__%s.json" % (name, set_name))
            ).exists() and not args.force:
                print("skip %s %s (done)" % (name, set_name))
                continue
            waited = 0
            busy = _busy_cores()
            while busy > MAX_LOAD and waited < 600:
                print("busy %.1f cores > %.1f — waiting" % (busy, MAX_LOAD), flush=True)
                time.sleep(15)
                waited += 18
                busy = _busy_cores()
            print(
                "== %s on %s (busy %.1f cores, load %.2f)"
                % (name, set_name, busy, _load1()),
                flush=True,
            )
            cmd = [
                sys.executable,
                __file__,
                "run",
                name,
                "--set",
                set_name,
                "--threads",
                str(args.threads),
                "--limit",
                str(args.limit),
            ]
            try:
                subprocess.run(cmd, timeout=args.timeout, check=False)
            except subprocess.TimeoutExpired:
                print("TIMEOUT %s %s" % (name, set_name), flush=True)
    return 0


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def _fmt(v) -> str:
    return "—" if v is None else str(v)


def cmd_report(args) -> int:
    results = [json.loads(p.read_text()) for p in sorted(RESULTS.glob("*__*.json"))]
    manifests = {}
    for r in results:
        if r["candidate"] == "prod-stream":
            continue
        if r["set"] not in manifests:
            meta = json.loads((DATA / r["set"] / "manifest.json").read_text())
            manifests[r["set"]] = {it["wav"]: it for it in meta["items"]}
        words = json.loads((DATA / "piper" / "manifest.json").read_text())["words"]
        _score(r, [manifests[r["set"]][row["wav"]] for row in r["rows"]], words)
    for set_name in ("piper", "libri"):
        rs = [
            r
            for r in results
            if r["set"] == set_name and r["candidate"] != "prod-stream"
        ]
        if not rs:
            continue
        print("\n### %s\n" % set_name)
        print(
            "| candidate | n | WER | CER | ctl | ctl+join | one-shot p50/p95 ms | RTF | cores "
            "| load s | RSS MB | stream WER | stream ctl | first word p50/p90 ms "
            "| final after end p50/p95 ms |"
        )
        print("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for r in sorted(rs, key=lambda r: r["oneshot_p50"]):
            pct = lambda v: "—" if v is None else "%s%%" % v  # noqa: E731
            print(
                "| %s | %d | %s | %s | %s | %s | %s / %s | %s | %s | %s | %s | %s | %s | %s | %s |"
                % (
                    r["candidate"],
                    r["n"],
                    pct(r["wer_oneshot"]),
                    pct(r.get("cer_oneshot")),
                    _fmt(r.get("ctl_oneshot")),
                    _fmt(r.get("ctlj_oneshot")),
                    r["oneshot_p50"],
                    r["oneshot_p95"],
                    r["rtf"],
                    r["cores"],
                    r["load_s"],
                    r["rss_peak_mb"],
                    pct(r.get("wer_stream")),
                    _fmt(r.get("ctl_stream")),
                    "%s / %s" % (_fmt(r["first_p50"]), _fmt(r["first_p90"])),
                    "%s / %s" % (_fmt(r["final_p50"]), _fmt(r["final_p95"])),
                )
            )
    for r in results:
        if r["candidate"] == "prod-stream":
            print(
                "\nprod-stream (%s): %s peak %s MB"
                % (r["set"], json.dumps(r["voice_bench"]), r["rss_peak_mb"])
            )
    if args.misses:
        for r in results:
            if r["set"] != "piper":
                continue
            for row in r["rows"]:
                bad = [k for k in ("oneshot", "stream") if row.get(k + "_ctl") is False]
                if bad:
                    print(
                        "%s %s: %r one=%r stream=%r"
                        % (
                            r["candidate"],
                            row["wav"],
                            row["ref"],
                            row["oneshot"],
                            row.get("stream"),
                        )
                    )
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("render")
    p.add_argument("--seed", type=int, default=7)
    p = sub.add_parser("fetch")
    p.add_argument("--seed", type=int, default=7)
    p = sub.add_parser("run")
    p.add_argument("candidate")
    p.add_argument("--set", default="piper")
    p.add_argument("--threads", type=int, default=6)
    p.add_argument("--limit", type=int, default=0)
    p = sub.add_parser("all")
    p.add_argument("--candidates", default="")
    p.add_argument("--set", default="piper,libri")
    p.add_argument("--threads", type=int, default=6)
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("report")
    p.add_argument("--misses", action="store_true")
    p = sub.add_parser("list")
    args = ap.parse_args(argv)
    if args.cmd == "list":
        print("\n".join(["prod-stream"] + list(_candidates())))
        return 0
    return {
        "render": cmd_render,
        "fetch": cmd_fetch,
        "run": cmd_run,
        "all": cmd_all,
        "report": cmd_report,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
