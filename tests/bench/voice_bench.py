"""The voice recognition bench: how well, and how fast, the streaming
recogniser (``backend/web/core/speech_stream.py`` over
``core.speech.LocalSTT``) hears the command vocabulary.

Not a unit test — pytest never collects it (the file name is not
``test_*.py``; ``tests/bench/`` has no ``__init__``). It needs the local
models' packages (faster-whisper, piper, numpy), which the dev venv does not
have; run it with the tool venv::

    PYTHONPATH=$PWD ~/.local/share/uv/tools/mindflock/bin/python \\
        tests/bench/voice_bench.py --configs base.en+small,base.en+small.en

What it does: synthesises ~80 command phrases with piper (the installed
``en_US-lessac-medium`` voice; varied speed, varied timbre, light noise, a
bit of room before and after) — radio words on their own ("over and out",
"disregard"), dictation ending in a radio word ("fix the failing test,
submit"), wake-word questions ("mindflock, how do fences work") and session
switching ("switch to sitecheck bot seven, over") — and feeds each through a
real :class:`speech_stream.Stream` chunked the way the clients chunk (150 ms
first, then 300 ms), one quiet chunk after the speech (the clients keep
sending while the control word settles), then :meth:`Stream.finish`.

Per configuration (``<fast model>+<final model>`` — the fast tier bare and
the accurate pass with the vocabulary hints, as the server runs them;
``+nohints`` for no hints at all, ``+fasthints`` to also lead the fast
tier's prompt with the voice words (what ``core.speech.HINT_CHARS`` warns
against), ``+off`` for no accurate pass) it reports:

* **ctl fast / ctl final** — the control word is heard where it belongs
  (at the end of the fast words, resp. of ``final``; the wake word at the
  start), judged the way the clients judge it (``voice_words.phrase_regex``).
* **WER fast / WER final** — the word error rate of the whole phrase
  against what piper was told to say, punctuation dropped, digits and
  number words folded together (``final`` falls back to the fast text when
  the accurate pass did not run, as it does for the client).
* **pass p50/p95** — one fast pass's milliseconds (``Stream.last_ms`` of
  the passes that ran).
* **finish p50/p95** — ``Stream.finish()`` wall time, which is what the
  client waits for after "over"; **final p50/p95** the accurate pass's own
  time inside it (``final_ms``), and **spec** how many finals were served
  from the pass started early on the quiet tail.

Deterministic (seeded) so two runs compare; ``--save DIR`` writes the WAVs.
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import random
import re
import statistics
import sys
import time
import wave
from array import array
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# A bare settings file: nothing here may read or write the owner's.
if not os.environ.get("MINDFLOCK_SETTINGS_FILE"):
    import tempfile

    _settings_file = Path(tempfile.mkdtemp(prefix="voice-bench-")) / "settings.json"
    _settings_file.write_text(json.dumps({"onboarded": True}))
    os.environ["MINDFLOCK_SETTINGS_FILE"] = str(_settings_file)

import numpy as np  # noqa: E402

from backend.web.core import speech, speech_stream, voice_words  # noqa: E402

RATE = speech_stream.RATE
PIPER = (
    Path(
        os.environ.get("MINDFLOCK_VOICE_DIR")
        or os.path.expanduser("~/.mindflock/voice")
    )
    / "piper"
    / "en_US-lessac-medium.onnx"
)

#: Session titles a fleet might have (the hint builder spells them out).
TITLES = [
    "sitecheck-bot7",
    "voice-stt",
    "map-ux",
    "shortcut-21129",
    "feature/mcp-fences-order",
    "release-notes",
    "app-dev",
]

#: Dictation lines, spoken before a radio word.
DICTATION = [
    "fix the failing test",
    "run the tests again and show me the failures",
    "rename the helper to parse config",
    "add a docstring to the new function",
    "open a pull request for this branch",
    "merge main into this worktree",
    "commit what you have so far",
    "push the branch and watch the checks",
    "why is pytest hanging",
    "what does the autopilot do at the end of a turn",
    "switch to sitecheck bot seven",
    "switch to the voice stt session",
    "go to map ux",
    "show me the ticket for shortcut two one one two nine",
    "check the inbox and reply to the orchestrator",
    "set the fence to the speech module only",
    "the diff looks wrong, revert the last commit",
    "ask claude to explain the stream window",
    "tell codex to rerun the bench",
]

WAKE_QUESTIONS = [
    "how do fences work",
    "what is a worktree",
    "which sessions are waiting",
    "switch to sitecheck bot seven",
    "is the pull request merged",
]

# (what piper says, the dictation reference, the check)
# check = ("end", kind) — a phrase of that kind ends the text;
#         ("start", "wake") — the wake word opens it.
Phrase = Tuple[str, str, Tuple[str, str]]


def phrases(words: dict) -> List[Phrase]:
    """The bench's phrase list from the effective voice words (defaults)."""
    out: List[Phrase] = []
    send, end, clear, stop = (
        words["send"][0],
        words["end"][0],
        words["clear"][0],
        "stop",
    )
    # Radio words on their own, each a few times (they are the complaint).
    for _ in range(3):
        out.append(("%s." % send.capitalize(), "", ("end", "send")))
        out.append(("%s." % end.capitalize(), "", ("end", "end")))
        out.append(("%s." % clear.capitalize(), "", ("end", "clear")))
        out.append(("%s." % stop.capitalize(), "", ("end", "stop")))
    out.append(("Disregard that.", "that", ("end", "clear")))  # the owner's phrasing
    out.append(("Stop listening.", "", ("end", "stop")))
    out.append(("Mic off.", "", ("end", "stop")))
    out.append(("That's all.", "", ("end", "stop")))
    # Dictation ending in a radio word, round-robin over the kinds.
    kinds = [("send", send), ("send", "submit"), ("end", end), ("clear", clear)]
    for i, line in enumerate(DICTATION):
        kind, word = kinds[i % len(kinds)]
        ref = line if word != "submit" else line  # "submit" is a send synonym
        out.append(("%s, %s." % (line.capitalize(), word), ref, ("end", kind)))
    # The wake word, then a question.
    for q in WAKE_QUESTIONS:
        out.append(("%s, %s?" % (words["wake"].capitalize(), q), q, ("start", "wake")))
    # Plain dictation sent with "over" (a second sample of each, faster).
    for line in DICTATION[:8]:
        out.append(("%s, %s." % (line.capitalize(), send), line, ("end", "send")))
    return out


# --------------------------------------------------------------------------- #
# Audio
# --------------------------------------------------------------------------- #
def _resample(samples: np.ndarray, rate: int) -> np.ndarray:
    if rate == RATE:
        return samples.astype(np.float32)
    n = int(round(len(samples) * RATE / rate))
    x_old = np.linspace(0.0, 1.0, num=len(samples), endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=n, endpoint=False)
    return np.interp(x_new, x_old, samples).astype(np.float32)


def synthesise(voice, text: str, rng: random.Random) -> np.ndarray:
    """``text`` as 16 kHz float32 with varied speed and timbre, light noise
    and 300–500 ms of room on either side."""
    from piper.config import SynthesisConfig

    cfg = SynthesisConfig(
        length_scale=rng.choice([0.8, 0.9, 1.0, 1.1, 1.25]),
        noise_scale=rng.choice([0.4, 0.667, 0.9]),
        noise_w_scale=rng.choice([0.5, 0.8, 1.1]),
    )
    parts, rate = [], RATE
    for chunk in voice.synthesize(text, cfg):
        parts.append(chunk.audio_float_array)
        rate = chunk.sample_rate
    speech_ = _resample(np.concatenate(parts), rate)
    speech_ *= rng.uniform(0.5, 0.9) / max(1e-6, float(np.abs(speech_).max()))
    lead = np.zeros(int(RATE * rng.uniform(0.3, 0.5)), dtype=np.float32)
    tail = np.zeros(int(RATE * rng.uniform(0.3, 0.5)), dtype=np.float32)
    out = np.concatenate([lead, speech_, tail])
    snr_db = rng.uniform(22.0, 35.0)
    noise = np.random.default_rng(rng.getrandbits(32)).standard_normal(len(out))
    noise = noise.astype(np.float32) * float(
        np.sqrt(np.mean(out**2)) / 10 ** (snr_db / 20)
    )
    return np.clip(out + noise, -1.0, 1.0).astype(np.float32)


def _chunks(samples: np.ndarray) -> List[array]:
    """The clients' cadence: 150 ms first, then 300 ms."""
    out, i, size = [], 0, int(0.15 * RATE)
    while i < len(samples):
        out.append(array("f", samples[i : i + size].tolist()))
        i += size
        size = int(0.3 * RATE)
    return out


def _quiet_chunk(seconds: float, rng: random.Random) -> array:
    noise = np.random.default_rng(rng.getrandbits(32)).standard_normal(
        int(seconds * RATE)
    )
    return array("f", (noise * 0.002).astype(np.float32).tolist())


def _save(path: Path, samples: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((samples * 32767).astype("<i2").tobytes())


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
_NUMBERS = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
}


def _norm(text: str) -> List[str]:
    words = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in text.lower())
    out: List[str] = []
    for w in words.split():
        w = _NUMBERS.get(w, w)
        # "21129" vs "2 1 1 2 9": split a run of digits into digits.
        if w.isdigit() and len(w) > 1:
            out.extend(list(w))
        else:
            out.append(w)
    return out


def _wer(ref: Sequence[str], hyp: Sequence[str]) -> Tuple[int, int]:
    """(edits, reference length) — Levenshtein over words."""
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i]
        for j, h in enumerate(hyp, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h)))
        prev = cur
    return prev[-1], len(ref)


def _control(text: str, check: Tuple[str, str], words: dict) -> Tuple[bool, str]:
    """Whether the control word is where it belongs → (hit, the text without
    it). ``submit`` counts as a send word (the clients' table has it)."""
    where, kind = check
    low = " ".join(_norm(text))
    if where == "start":
        wake = words["wake"].lower()
        m = re.match(r"^(%s)\b" % re.escape(wake), low)
        if m:
            return True, low[m.end() :].strip()
        # Two letters of slack, like core.wake.is_wake.
        head = low.split(" ", 1)
        if head and _close(head[0], wake):
            return True, (head[1] if len(head) > 1 else "")
        return False, low
    phrases_ = list(words[kind])
    if kind == "send":
        phrases_.append("submit")
    pat = re.compile(
        voice_words.phrase_regex([" ".join(_norm(p)) for p in phrases_]) + r"$"
    )
    m = pat.search(low)
    if m:
        return True, low[: m.start()].strip()
    return False, low


def _close(a: str, b: str) -> bool:
    e, _ = _wer(list(a), list(b))
    return e <= 2


# --------------------------------------------------------------------------- #
# Configurations
# --------------------------------------------------------------------------- #
class Config:
    def __init__(self, spec: str):
        parts = spec.split("+")
        self.fast = parts[0]
        self.final = parts[1] if len(parts) > 1 else "small"
        self.hints = "nohints" not in parts[2:]
        self.fast_hints = self.hints and "fasthints" in parts[2:]
        self.name = spec


def _transcribers(cfg: Config, hints: str):
    """The two tiers for ``cfg`` from :class:`speech.LocalSTT`, with the
    hints when this build's transcribers take them."""
    speech._voice = lambda: SimpleNamespace(  # type: ignore[assignment]
        stt_backend="local",
        stt_model=cfg.final if cfg.final != "off" else "small",
        stt_stream_model=cfg.fast,
        stt_final=cfg.final,
    )
    stt = speech.LocalSTT()
    kw: dict = {}
    fast_kw: dict = {}
    if cfg.hints and "hints" in inspect.signature(stt.stream_transcriber).parameters:
        kw["hints"] = hints
        if cfg.fast_hints:
            fast_kw["hints"] = speech.recognition_hints(
                voice_words.effective({}), (), 120, vocabulary=False
            )
    fast = stt.stream_transcriber("en", **fast_kw)
    final = stt.final_transcriber("en", **kw) if cfg.final != "off" else None
    return fast, final


def _hints(words: dict) -> str:
    builder = getattr(speech, "recognition_hints", None)
    if builder is None:
        return ""
    return builder(words, TITLES)


def _pct(values: List[float], q: float) -> float:
    if not values:
        return 0.0
    vals = sorted(values)
    k = min(len(vals) - 1, int(round(q * (len(vals) - 1))))
    return vals[k]


def run_config(
    cfg: Config, clips, words: dict, hints: str, tail_quiet: int, rng, verbose
):
    fast, final = _transcribers(cfg, hints if cfg.hints else "")
    # Load (and prime) ahead, so the first phrase's numbers are not the load.
    speech.LocalSTT().warm()
    pass_ms: List[float] = []
    finish_ms: List[float] = []
    final_ms: List[float] = []
    ctl_fast = ctl_final = 0
    final_ran = spec = 0
    wer_fast = [0, 0]
    wer_final = [0, 0]
    misses: List[str] = []
    for text, _ref, check, samples in clips:
        stream = speech_stream.Stream(fast, final)
        for chunk in _chunks(samples):
            stream.feed(chunk)
            if stream.last_ms:
                pass_ms.append(stream.last_ms)
        for _ in range(tail_quiet):
            stream.feed(_quiet_chunk(0.3, rng))
            if stream.last_ms:
                pass_ms.append(stream.last_ms)
        t0 = time.monotonic()
        out = stream.finish()
        finish_ms.append((time.monotonic() - t0) * 1000)
        fast_text = stream.text
        hit, _body = _control(fast_text, check, words)
        ctl_fast += hit
        e, n = _wer(_norm(text), _norm(fast_text))
        wer_fast[0] += e
        wer_fast[1] += n
        if out.get("final"):
            final_ran += 1
            final_ms.append(out.get("final_ms", 0))
            spec += bool(out.get("final_early"))
            hit_f, _body = _control(out["final"], check, words)
            ctl_final += hit_f
            e, n = _wer(_norm(text), _norm(out["final"]))
        else:
            hit_f = hit
            ctl_final += hit
        wer_final[0] += e
        wer_final[1] += n
        if not (hit and hit_f):
            misses.append(
                "%-55s fast=%r final=%r" % (text, fast_text, out.get("final", ""))
            )
        if verbose:
            print(
                "  %-55s fast=%r final=%r %dms"
                % (text, fast_text, out.get("final", ""), out.get("final_ms", 0))
            )
    n = len(clips)
    row = {
        "config": cfg.name,
        "ctl_fast": "%d/%d" % (ctl_fast, n),
        "ctl_final": "%d/%d" % (ctl_final, n),
        "wer_fast": "%.1f%%" % (100.0 * wer_fast[0] / max(1, wer_fast[1])),
        "wer_final": "%.1f%%" % (100.0 * wer_final[0] / max(1, wer_final[1])),
        "pass_p50": "%d" % _pct(pass_ms, 0.5),
        "pass_p95": "%d" % _pct(pass_ms, 0.95),
        "finish_p50": "%d" % _pct(finish_ms, 0.5),
        "finish_p95": "%d" % _pct(finish_ms, 0.95),
        "final_p50": "%d" % _pct(final_ms, 0.5),
        "final_p95": "%d" % _pct(final_ms, 0.95),
        "spec": "%d/%d" % (spec, final_ran),
    }
    return row, misses


COLUMNS = [
    ("config", "config"),
    ("ctl_fast", "ctl fast"),
    ("ctl_final", "ctl final"),
    ("wer_fast", "WER fast"),
    ("wer_final", "WER final"),
    ("pass_p50", "pass p50"),
    ("pass_p95", "pass p95"),
    ("finish_p50", "finish p50"),
    ("finish_p95", "finish p95"),
    ("final_p50", "final p50"),
    ("final_p95", "final p95"),
    ("spec", "early"),
]


def table(rows: List[dict]) -> str:
    head = "| " + " | ".join(h for _, h in COLUMNS) + " |"
    sep = "|" + "|".join("---" for _ in COLUMNS) + "|"
    body = ["| " + " | ".join(str(r[k]) for k, _ in COLUMNS) + " |" for r in rows]
    return "\n".join([head, sep] + body)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--configs",
        default="base.en+small",
        help="comma-separated <fast>+<final>[+nohints|+fasthints]; final 'off' = no accurate pass",
    )
    ap.add_argument(
        "--phrases", type=int, default=0, help="use only the first N phrases"
    )
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument(
        "--tail-quiet", type=int, default=1, help="quiet 300 ms chunks after the speech"
    )
    ap.add_argument("--save", default="", help="write the synthesised WAVs here")
    ap.add_argument(
        "--misses", action="store_true", help="print the phrases that missed"
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    words = voice_words.effective({})
    rng = random.Random(args.seed)
    from piper.voice import PiperVoice

    voice = PiperVoice.load(str(PIPER))
    plist = phrases(words)
    if args.phrases:
        plist = plist[: args.phrases]
    clips = []
    for text, ref, check in plist:
        clips.append((text, ref, check, synthesise(voice, text, rng)))
    if args.save:
        folder = Path(args.save)
        folder.mkdir(parents=True, exist_ok=True)
        for i, (text, _ref, _check, samples) in enumerate(clips):
            _save(folder / ("%03d.wav" % i), samples)
    total_s = sum(len(c[3]) for c in clips) / RATE
    print(
        "%d phrases, %.1f s of audio (%.1f s mean); hints: %s"
        % (
            len(clips),
            total_s,
            total_s / len(clips),
            "built" if _hints(words) else "none (old build)",
        )
    )
    hints = _hints(words)
    if hints and args.verbose:
        print("hints (%d chars): %s" % (len(hints), hints))
    rows = []
    for spec in args.configs.split(","):
        cfg = Config(spec.strip())
        row, misses = run_config(
            cfg,
            clips,
            words,
            hints,
            args.tail_quiet,
            random.Random(args.seed),
            args.verbose,
        )
        rows.append(row)
        print(table([row]).split("\n")[-1])
        if args.misses:
            for line in misses:
                print("  miss: " + line)
        speech.unload_all()
    print()
    print(table(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
