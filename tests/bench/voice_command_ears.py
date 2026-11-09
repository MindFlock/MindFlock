"""The command-ears bench: what the two recognisers WRITE for every voice
command phrase — Moonshine tiny-streaming with the production keyterms
(``core.speech_moonshine``: the live words and ``heard``, which the control
words are read off) and whisper ``small`` with the production hotwords
(``LocalSTT.final_transcriber``: the ``final`` text). Not a unit test
(pytest never collects ``tests/bench``).

It needs the models' packages (piper, moonshine-voice, faster-whisper,
numpy), which the dev venv lacks; run it with the tool venv, one step at a
time, only while ``uptime``'s load is under 3 (each step waits for that)::

    B="env PYTHONPATH=$PWD $HOME/.local/share/uv/tools/mindflock/bin/python tests/bench/voice_command_ears.py"
    $B render                            # synthesise the clips (once)
    $B run --engine moonshine --label after
    $B run --engine whisper --label after
    $B report --label after [--vs before] [--corpus]   # tables (+ the corpus)
    $B guard --label after               # Moonshine false positives

``render``: every phrase of :func:`phrases` — each DEFAULTS voice phrase
(send, end, clear, undo, stop, hush, wake), the router's commands and the
new ones (scrolling, paging, interrupt, help) — said by four piper voices
(``en_US-lessac-medium`` and three speakers of ``en_US-libritts_r-medium``,
:data:`VOICES`), two speeds each from 0.9/1.0/1.1× (``length_scale``),
light noise (22–35 dB SNR) and 300–500 ms of room either side: once ALONE
and once at the end of a dictation sentence ("fix the margin on the login
page, scroll up"; the wake word at the start instead: "mindflock, fix …").
The clips and ``manifest.json`` go to ``$STT_BENCH_DATA/command-ears``
(default ``~/stt-bench-models/data``).

``run``: each clip through one engine in its own process — Moonshine as
production runs it (:class:`speech_moonshine.MoonshineStream`, keyterms from
``speech.recognition_hints(words, TITLES)``, fed in 160 ms chunks, then
``finish()["heard"]``), or whisper as production runs the final
(``LocalSTT.final_transcriber("en", hints=…)`` on ``small``). ``--label``
names the results (``before`` was this branch's base, ``after`` the new
hints); ``--hints-from`` takes the hint string of another results file (to
re-run an engine under a baseline's hints).

Scoring, per phrase and engine (:func:`score`): **words** — the phrase's
words, as written (lower case, punctuation dropped, numbers as digits), are
exactly the words heard in its place (the clip alone: the whole text; in a
sentence: the span aligned to it, :func:`span`); **kind** — the clip ALONE
read as production reads it (``voice_words.control_kind`` for a radio or
hush phrase, ``voice_router.command`` for the router's, ``wake.is_wake``
for the wake word) gives the expected kind (``-``: no reader yet — the new
commands, whose router is another branch's). Every distinct non-exact
hearing becomes a corpus row (``report --corpus`` →
``tests/unit/data/voice_command_corpus.json``: ``said``, ``heard``,
``engine``, ``kind``).

``guard``: dictation that is NOT a command (:data:`GUARD_LINES`, several
ending in "…that", "…more", "…up", "…down") and quiet / noise clips through
Moonshine with the keyterms: a control kind or a router command read off a
dictation line, or any word out of a quiet clip, is a false positive.

Results 2026-11-08 (WSL2, Core Ultra 9 275HX, load < 3; moonshine-voice
tiny-streaming on one thread, faster-whisper 1.2.1 ``small``; 4 voices × 2
speeds × {alone, sentence} = 16 clips a phrase, 752 in all; the hints built
for the seven :data:`TITLES`). ``before`` = the hints without the undo
phrases and :data:`speech.HINT_COMMANDS`; ``after`` = with both (399 of
the 400 characters). Words: the phrase's words heard exactly in its place;
kind (alone): read as production reads it, with the ``after`` HEARD table
(``-``: the new commands, read by the router branch)::

    | phrase | kind | moonshine words (before→after) | moonshine kind (alone) | whisper words (before→after) | whisper kind (alone) |
    |---|---|---|---|---|---|
    | over | send | 15/16 | 7/8 | 16/16 → **15/16** | 8/8 |
    | over and out | end | 16/16 | 8/8 | 16/16 | 8/8 |
    | over, out | end | 15/16 | 8/8 | 16/16 | 8/8 |
    | disregard | clear | 10/16 | 8/8 | 16/16 | 8/8 |
    | scratch that | undo | 11/16 → **12/16** | 8/8 | 12/16 → **15/16** | 8/8 |
    | undo that | undo | 8/16 | 7/8 | 14/16 → **15/16** | 8/8 |
    | stop | stop | 14/16 | 7/8 | 15/16 | 8/8 |
    | stop listening | stop | 15/16 | 8/8 | 16/16 | 8/8 |
    | mic off | stop | 1/16 | 4/8 | 16/16 | 8/8 |
    | pause listening | stop | 11/16 | 6/8 | 16/16 | 8/8 |
    | that's all | stop | 16/16 | 8/8 | 16/16 | 8/8 |
    | done talking | stop | 10/16 | 8/8 | 11/16 | 8/8 |
    | stop talking | hush | 16/16 | 8/8 | 16/16 | 8/8 |
    | be quiet | hush | 16/16 | 8/8 | 16/16 | 8/8 |
    | quiet | hush | 15/16 | 8/8 | 16/16 | 8/8 |
    | hush | hush | 12/16 | 7/8 | 11/16 | 7/8 |
    | shut up | hush | 15/16 | 7/8 | 15/16 | 7/8 |
    | enough | hush | 15/16 | 8/8 | 16/16 | 8/8 |
    | mindflock | wake | 10/16 | 8/8 | 15/16 | 8/8 |
    | next session | switch_rel | 14/16 | 7/8 | 16/16 | 8/8 |
    | previous session | switch_rel | 16/16 | 8/8 | 16/16 | 8/8 |
    | go back | switch_rel | 15/16 | 8/8 | 16/16 | 8/8 |
    | switch to hackathon two | switch | 12/16 | 5/8 | 15/16 | 7/8 |
    | switch to sitecheck bot seven | switch | 0/16 | 5/8 | 14/16 → **13/16** | 6/8 |
    | switch to voice ears | switch | 13/16 | 5/8 | 15/16 | 7/8 |
    | switch to login fix | switch | 9/16 | 5/8 | 14/16 | 8/8 |
    | session three | switch_index | 16/16 | 8/8 | 15/16 | 8/8 |
    | what needs me | needs | 15/16 | 7/8 | 16/16 | 8/8 |
    | what sessions do I have | list | 13/16 | 8/8 | 16/16 | 8/8 |
    | status | status | 13/16 → **14/16** | 6/8 | 12/16 → **15/16** | 7/8 |
    | say that again | repeat | 14/16 | 7/8 | 16/16 | 8/8 |
    | summarize that | condense | 10/16 → **13/16** | 5/8 | 15/16 | 7/8 |
    | cancel | cancel | 15/16 | 7/8 | 16/16 | 8/8 |
    | never mind | cancel | 15/16 | 8/8 | 16/16 | 8/8 |
    | scroll up | scroll_up | 12/16 | - | 12/16 → **13/16** | - |
    | scroll down | scroll_down | 13/16 → **14/16** | - | 15/16 → **16/16** | - |
    | page up | page_up | 11/16 | - | 12/16 → **16/16** | - |
    | page down | page_down | 12/16 | - | 12/16 → **16/16** | - |
    | scroll to the bottom | scroll_bottom | 10/16 | - | 11/16 → **13/16** | - |
    | go to the top | scroll_top | 15/16 | - | 15/16 | - |
    | keep going | more | 15/16 | - | 16/16 | - |
    | more | more | 15/16 | - | 14/16 | - |
    | interrupt | interrupt | 14/16 | - | 16/16 | - |
    | press escape | interrupt | 14/16 | - | 16/16 | - |
    | where am I | where | 15/16 | - | 16/16 | - |
    | what can I say | help | 16/16 | - | 16/16 | - |
    | voice commands | help | 15/16 | - | 15/16 | - |
    | **all** |  | 608/752 → 614/752 | 240/272 | 699/752 → 716/752 | 264/272 |

* Whisper is the better ear for commands (699 → 716 of 752 against
  Moonshine's 608 → 614): the command hotwords take "page up" / "page
  down" 12 → 16 of 16, "status" 12 → 15, "scratch that" 12 → 15, "scroll
  to the bottom" 11 → 13. Moonshine's keyterms barely move it — all of them
  together are worth +14 clips over none (``--hints-from`` an empty list:
  594), mostly the session titles; the command words add ~6 ("summarize
  that" 10 → 13). Whole command phrases as keyterms ("scroll up, scroll
  down, scroll to the bottom, …, switch to") scored the same (616) for 40
  more characters.
* Moonshine's weak spots, every one also a corpus row: "mic off" (1/16:
  "mike off", "my gosh", "my god"…), "done talking" ("then talking" 6×,
  whisper 5×), "undo that" ("and do that", "under that", "I'll do that"),
  "scroll" ("grow up", "goal to the bottom"), "page" ("peach up", "h
  down"), "switch" ("twitch to …", 5 of 64), "sitecheck" ("site check",
  "sidechick"). The switch phrases' kind still reads 5/8 alone: the router
  matches the name by sound.
* The HEARD additions (``voice_words``: "over felt", "it's regard",
  "cretch/cratch/crutch that", "stuff listening", "paul's/how's
  listening", "then talking", "hash", "pash", "mindflog", "mind fluff")
  turn 10 Moonshine and 8 whisper clips into their kind that the old table
  missed, and lose none.
* ``guard``: 0 false positives of 32 (20 dictation lines, 12 quiet / noise
  clips) with either keyterm list, and the same text from both. Before the
  undo sound-alike had to start with the phrase's first letter, Moonshine's
  "And do that." (dictated) read as an undo.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import random
import re
import sys
import time
import wave
from array import array
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for p in (str(ROOT), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

# A bare settings file: nothing here may read or write the owner's.
if not os.environ.get("MINDFLOCK_SETTINGS_FILE"):
    import tempfile

    _settings_file = Path(tempfile.mkdtemp(prefix="ears-bench-")) / "settings.json"
    _settings_file.write_text(json.dumps({"onboarded": True}))
    os.environ["MINDFLOCK_SETTINGS_FILE"] = str(_settings_file)
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

MODELS = Path(
    os.path.expanduser(os.environ.get("STT_BENCH_MODELS", "~/stt-bench-models"))
)
DATA = (
    Path(os.path.expanduser(os.environ.get("STT_BENCH_DATA", str(MODELS / "data"))))
    / "command-ears"
)
RESULTS = Path(
    os.path.expanduser(os.environ.get("STT_BENCH_RESULTS", str(MODELS / "results")))
)
LIVE_VOICE = Path(
    os.environ.get("MINDFLOCK_VOICE_DIR") or os.path.expanduser("~/.mindflock/voice")
)
#: (label, model file, speaker id or None).
VOICES = (
    ("lessac", LIVE_VOICE / "piper" / "en_US-lessac-medium.onnx", None),
    ("libri-f", MODELS / "piper" / "en_US-libritts_r-medium.onnx", 12),
    ("libri-m", MODELS / "piper" / "en_US-libritts_r-medium.onnx", 140),
    ("libri-x", MODELS / "piper" / "en_US-libritts_r-medium.onnx", 520),
)
SPEEDS = (0.9, 1.0, 1.1)
RATE = 16000
CHUNK_S = 0.16
MAX_LOAD = 3.0

#: The session titles of the fleet the hints are built for (the switch
#: phrases name the first four).
TITLES = [
    "hackathon-2",
    "sitecheck-bot7",
    "voice-ears",
    "login-fix",
    "map-ux",
    "release-notes",
    "app-dev",
]
#: Dictation said before a phrase (the sentence context), in turn.
LEADS = [
    "fix the margin on the login page",
    "run the tests again and show me the failures",
    "rename the helper to parse config",
    "open a pull request for this branch",
]

#: Router commands (said, expected ``voice_router.command`` kind).
ROUTER = [
    ("next session", "switch_rel"),
    ("previous session", "switch_rel"),
    ("go back", "switch_rel"),
    ("switch to hackathon two", "switch"),
    ("switch to sitecheck bot seven", "switch"),
    ("switch to voice ears", "switch"),
    ("switch to login fix", "switch"),
    ("session three", "switch_index"),
    ("what needs me", "needs"),
    ("what sessions do I have", "list"),
    ("status", "status"),
    ("say that again", "repeat"),
    ("summarize that", "condense"),
    ("cancel", "cancel"),
    ("never mind", "cancel"),
]
#: The new commands (said, the kind the router branch is expected to give).
NEW = [
    ("scroll up", "scroll_up"),
    ("scroll down", "scroll_down"),
    ("page up", "page_up"),
    ("page down", "page_down"),
    ("scroll to the bottom", "scroll_bottom"),
    ("go to the top", "scroll_top"),
    ("keep going", "more"),
    ("more", "more"),
    ("interrupt", "interrupt"),
    ("press escape", "interrupt"),
    ("where am I", "where"),
    ("what can I say", "help"),
    ("voice commands", "help"),
]

#: Dictation that is not a command, for ``guard`` — several end in words a
#: command ends in.
GUARD_LINES = [
    "we should look into that",
    "can you double check that",
    "I want you to fix that",
    "please patch that",
    "let's stretch that",
    "and do that",
    "go ahead and undo the last migration then rerun it",
    "scratch the old approach and start over from the parser",
    "add a little more padding",
    "the list needs more",
    "make the header line up",
    "move the button down",
    "the page is scrolling too fast on mobile",
    "explain where the config gets loaded",
    "write a test for the escape key handler",
    "this should never interrupt the running build",
    "the session list keeps growing",
    "rename the status column",
    "check what the agent wrote in the summary",
    "keep the old behaviour behind a flag",
]


def phrases(words: dict) -> List[Tuple[str, str, str]]:
    """``(said, kind, group)`` for every phrase the bench says: the voice
    words' defaults (group ``word``), the router's (``router``) and the new
    commands (``new``)."""
    out: List[Tuple[str, str, str]] = []
    for kind in ("send", "end", "clear", "undo", "stop", "hush"):
        for p in words.get(kind) or ():
            out.append((p, kind, "word"))
    out.append((words["wake"], "wake", "word"))
    out += [(s, k, "router") for s, k in ROUTER]
    out += [(s, k, "new") for s, k in NEW]
    return out


# --------------------------------------------------------------------------- #
# Audio
# --------------------------------------------------------------------------- #
def _resample(samples, rate: int):
    import numpy as np

    if rate == RATE:
        return samples.astype(np.float32)
    n = int(round(len(samples) * RATE / rate))
    x_old = np.linspace(0.0, 1.0, num=len(samples), endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=n, endpoint=False)
    return np.interp(x_new, x_old, samples).astype(np.float32)


def synthesise(voice, speaker, text: str, speed: float, rng: random.Random):
    """``text`` as 16 kHz float32: ``speed`` (piper's ``length_scale``),
    light noise and 300–500 ms of room either side."""
    import numpy as np
    from piper.config import SynthesisConfig

    cfg = SynthesisConfig(
        speaker_id=speaker,
        length_scale=speed,
        noise_scale=rng.choice([0.5, 0.667, 0.8]),
        noise_w_scale=rng.choice([0.6, 0.8, 1.0]),
    )
    parts, rate = [], RATE
    for chunk in voice.synthesize(text, cfg):
        parts.append(chunk.audio_float_array)
        rate = chunk.sample_rate
    out = _resample(np.concatenate(parts), rate)
    out *= rng.uniform(0.5, 0.9) / max(1e-6, float(np.abs(out).max()))
    lead = np.zeros(int(RATE * rng.uniform(0.3, 0.5)), dtype=np.float32)
    tail = np.zeros(int(RATE * rng.uniform(0.3, 0.5)), dtype=np.float32)
    out = np.concatenate([lead, out, tail])
    snr_db = rng.uniform(22.0, 35.0)
    noise = np.random.default_rng(rng.getrandbits(32)).standard_normal(len(out))
    noise = noise.astype(np.float32) * float(
        np.sqrt(np.mean(out**2)) / 10 ** (snr_db / 20)
    )
    return np.clip(out + noise, -1.0, 1.0).astype(np.float32)


def _save(path: Path, samples) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((samples * 32767).astype("<i2").tobytes())


def _read(path: Path) -> array:
    with wave.open(str(path), "rb") as w:
        raw = w.readframes(w.getnframes())
    ints = array("h")
    ints.frombytes(raw)
    return array("f", (x / 32768.0 for x in ints))


def _wait_quiet(limit_s: float = 900.0) -> float:
    """Block until the 1-minute load is under :data:`MAX_LOAD` → it."""
    t0 = time.monotonic()
    while True:
        load = os.getloadavg()[0]
        if load < MAX_LOAD or time.monotonic() - t0 > limit_s:
            return load
        print("load %.1f ≥ %.1f: waiting" % (load, MAX_LOAD), file=sys.stderr)
        time.sleep(20)


def cmd_render(args) -> int:
    from piper.voice import PiperVoice

    from backend.web.core import voice_words

    _wait_quiet()
    words = voice_words.effective({})
    rng = random.Random(args.seed)
    DATA.mkdir(parents=True, exist_ok=True)
    loaded: Dict[str, object] = {}
    items: List[dict] = []
    n = 0
    plist = phrases(words)
    for said, kind, group in plist:
        for label, path, speaker in VOICES:
            if not path.exists():
                print("no voice %s (%s): skipped" % (label, path), file=sys.stderr)
                continue
            voice = loaded.get(str(path))
            if voice is None:
                voice = loaded[str(path)] = PiperVoice.load(str(path))
            for speed in rng.sample(SPEEDS, 2):
                for ctx in ("alone", "sentence"):
                    if ctx == "alone":
                        before, after = "", ""
                    elif kind == "wake":
                        before, after = "", LEADS[n % len(LEADS)]
                    else:
                        before, after = LEADS[n % len(LEADS)], ""
                    text = ", ".join(x for x in (before, said, after) if x)
                    wav = "%04d.wav" % n
                    _save(DATA / wav, synthesise(voice, speaker, text, speed, rng))
                    items.append(
                        {
                            "wav": wav,
                            "text": text,
                            "said": said,
                            "before": before,
                            "after": after,
                            "kind": kind,
                            "group": group,
                            "context": ctx,
                            "voice": label,
                            "speed": speed,
                        }
                    )
                    n += 1
    (DATA / "manifest.json").write_text(
        json.dumps({"items": items, "titles": TITLES}, indent=1)
    )
    # The guard set: dictation that is no command, and quiet / noise.
    gdir = DATA / "guard"
    gdir.mkdir(exist_ok=True)
    gitems: List[dict] = []
    for i, line in enumerate(GUARD_LINES):
        label, path, speaker = VOICES[i % len(VOICES)]
        voice = loaded.get(str(path))
        if voice is None:
            voice = loaded[str(path)] = PiperVoice.load(str(path))
        wav = "g%03d.wav" % i
        _save(gdir / wav, synthesise(voice, speaker, line, rng.choice(SPEEDS), rng))
        gitems.append({"wav": wav, "text": line, "voice": label})
    import numpy as np

    for i in range(12):
        secs = rng.uniform(1.0, 4.0)
        level = (0.0005, 0.002, 0.01, 0.03)[i % 4]
        noise = np.random.default_rng(i).standard_normal(int(secs * RATE))
        wav = "q%03d.wav" % i
        _save(gdir / wav, np.clip(noise * level, -1, 1).astype(np.float32))
        gitems.append({"wav": wav, "text": "", "voice": "noise %.4f" % level})
    (gdir / "manifest.json").write_text(json.dumps({"items": gitems}, indent=1))
    print("%d clips + %d guard clips in %s" % (len(items), len(gitems), DATA))
    return 0


# --------------------------------------------------------------------------- #
# Engines
# --------------------------------------------------------------------------- #
def _hints(words: dict, args) -> str:
    from backend.web.core import speech

    if getattr(args, "hints_from", ""):
        return json.loads(Path(args.hints_from).read_text())["hints"]
    return speech.recognition_hints(words, TITLES)


def _moonshine(hints: str, words: dict):
    """``hear(samples) -> text``: a production MoonshineStream per clip."""
    os.environ.setdefault("MOONSHINE_ORT_SINGLE_THREAD", "1")
    from backend.web.core import speech_moonshine as sm

    if not sm.available():
        raise SystemExit("Moonshine unavailable: " + sm.unavailable_reason())
    engine = sm.engine()
    step = int(CHUNK_S * RATE)

    def hear(samples: array) -> str:
        st = sm.MoonshineStream(engine, None, hints)
        st.words = words
        for i in range(0, len(samples), step):
            st.feed(samples[i : i + step])
        out = st.finish()
        return str(out.get("heard") or st.text or "")

    return hear


def _whisper(hints: str):
    """``hear(samples) -> text``: the production accurate pass on small."""
    from backend.web.core import speech

    speech._voice = lambda: SimpleNamespace(  # type: ignore[assignment]
        stt_backend="local",
        stt_model="small",
        stt_stream_model="base.en",
        stt_final="small",
    )
    run = speech.LocalSTT().final_transcriber("en", hints=hints)

    def hear(samples: array) -> str:
        return str(run(samples) or "")

    return hear


def _engine(name: str, hints: str, words: dict):
    return _moonshine(hints, words) if name == "moonshine" else _whisper(hints)


def cmd_run(args) -> int:
    from backend.web.core import voice_words

    words = voice_words.effective({})
    hints = _hints(words, args)
    hear = _engine(args.engine, hints, words)
    manifest = json.loads((DATA / "manifest.json").read_text())
    items = manifest["items"][: args.limit or None]
    hear(_read(DATA / items[0]["wav"]))  # warm-up, not counted
    _wait_quiet()
    rows = []
    t0 = time.monotonic()
    for i, it in enumerate(items):
        if i and i % 100 == 0:
            print("%d/%d %.0f s" % (i, len(items), time.monotonic() - t0))
            _wait_quiet()
        rows.append(dict(it, heard=hear(_read(DATA / it["wav"]))))
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / ("command-ears-%s-%s.json" % (args.engine, args.label))
    out.write_text(
        json.dumps(
            {"engine": args.engine, "label": args.label, "hints": hints, "rows": rows},
            indent=1,
        )
    )
    print("%d clips in %.0f s → %s" % (len(rows), time.monotonic() - t0, out))
    return 0


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


def _toks(text: str) -> List[str]:
    """``voice_router.norm``'s words (lower case, apostrophes kept)."""
    from backend.web.core import voice_router

    return voice_router.norm(text).split()


def _key(word: str) -> str:
    """A word as compared: apostrophes dropped, numbers as digits, a
    hyphenated or run-on digit kept whole."""
    w = word.replace("'", "")
    return _NUMBERS.get(w, w)


def _keys(text: str) -> List[str]:
    out: List[str] = []
    for w in _toks(text):
        k = _key(w)
        # "bot7" / "hackathon2": a letter/digit run split like speech_form.
        out.extend(
            x for x in re.split(r"(?<=[a-z])(?=[0-9])|(?<=[0-9])(?=[a-z])", k) if x
        )
    return out


def span(it: dict, heard: str) -> str:
    """The words of ``heard`` in the phrase's place: the whole text for a
    clip alone, else the hearing aligned to the phrase's words after the
    dictation (before it, for the wake word) — :mod:`difflib` over the
    compared words, a replaced or inserted run that touches the phrase
    counted in."""
    toks = _toks(heard)
    if it["context"] == "alone":
        return " ".join(toks)
    before, said, after = _keys(it["before"]), _keys(it["said"]), _keys(it["after"])
    ref = before + said + after
    lo, hi = len(before), len(before) + len(said)
    # Align on the compared keys, one per heard word (a split word keys as
    # its first part, enough for the alignment).
    hyp = [(_keys(t) or [""])[0] for t in toks]
    sm = difflib.SequenceMatcher(a=ref, b=hyp, autojunk=False)
    keep: List[int] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for d in range(i2 - i1):
                if lo <= i1 + d < hi:
                    keep.append(j1 + d)
        elif tag in ("replace", "insert"):
            touches = (i1 < hi and i2 > lo) if i2 > i1 else (lo <= i1 <= hi)
            # A trailing insert after the phrase belongs to it (it is the
            # sentence's end); a leading one after the wake word likewise.
            if i2 == i1 and not after and i1 == len(ref):
                touches = True
            if touches:
                keep.extend(range(j1, j2))
    return " ".join(toks[j] for j in sorted(set(keep)))


def _exact(said: str, heard: str) -> bool:
    return _keys(said) == _keys(heard)


def _kind(it: dict, heard: str) -> Optional[bool]:
    """The clip alone read the way production reads it → whether it gave
    the expected kind; None when nothing reads that kind yet."""
    from backend.web.core import voice_router, voice_words, wake

    kind = it["kind"]
    if it["group"] == "new":
        # Read by the router branch, not this one ("go to the top" is a
        # switch to a session called "the top" here).
        return None
    if kind == "wake":
        return wake.is_wake(heard)
    if it["group"] == "router":
        got = voice_router.command(heard)
        return got is not None and got.get("kind") == kind
    got = voice_words.control_kind(heard)
    if got is None and kind == "stop":
        cmd = voice_router.command(heard)
        got = cmd.get("kind") if cmd else None
    return got == kind


def score(rows: List[dict]) -> Dict[str, dict]:
    """Per phrase: clips, words hit (alone / sentence), kind hit (alone),
    the distinct mis-hearings (span text → count)."""
    out: Dict[str, dict] = {}
    for r in rows:
        s = out.setdefault(
            r["said"],
            {
                "kind": r["kind"],
                "group": r["group"],
                "n": 0,
                "words": 0,
                "alone": [0, 0],
                "sentence": [0, 0],
                "kind_hit": [0, 0],
                "miss": {},
            },
        )
        got = span(r, r["heard"])
        hit = _exact(r["said"], got)
        s["n"] += 1
        s["words"] += hit
        s[r["context"]][0] += hit
        s[r["context"]][1] += 1
        if r["context"] == "alone":
            k = _kind(r, r["heard"])
            if k is not None:
                s["kind_hit"][0] += k
                s["kind_hit"][1] += 1
        if not hit:
            s["miss"][got] = s["miss"].get(got, 0) + 1
    return out


def _load(engine: str, label: str) -> Optional[dict]:
    path = RESULTS / ("command-ears-%s-%s.json" % (engine, label))
    return json.loads(path.read_text()) if path.exists() else None


def _pct(a: int, b: int) -> str:
    return "%d/%d" % (a, b) if b else "-"


def cmd_report(args) -> int:
    engines = ("moonshine", "whisper")
    now = {e: _load(e, args.label) for e in engines}
    was = {e: _load(e, args.vs) for e in engines} if args.vs else {}
    scored = {e: score(d["rows"]) for e, d in now.items() if d}
    base = {e: score(d["rows"]) for e, d in was.items() if d}
    said = []
    for e in engines:
        for k in scored.get(e, {}):
            if k not in said:
                said.append(k)
    head = ["phrase", "kind"]
    for e in engines:
        if e in scored:
            if e in base:
                head.append("%s words (%s→%s)" % (e, args.vs, args.label))
            else:
                head.append("%s words" % e)
            head.append("%s kind (alone)" % e)
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    totals = {e: [0, 0, 0, 0, 0] for e in engines}
    for p in said:
        row = [p, next(scored[e][p]["kind"] for e in scored if p in scored[e])]
        for e in engines:
            if e not in scored:
                continue
            s = scored[e].get(p)
            if not s:
                row += ["-", "-"]
                continue
            cell = _pct(s["words"], s["n"])
            if e in base and p in base[e]:
                b = base[e][p]
                if b["words"] != s["words"]:
                    cell = "%s → **%s**" % (_pct(b["words"], b["n"]), cell)
                totals[e][4] += b["words"]
            row.append(cell)
            row.append(_pct(*s["kind_hit"]))
            totals[e][0] += s["words"]
            totals[e][1] += s["n"]
            totals[e][2] += s["kind_hit"][0]
            totals[e][3] += s["kind_hit"][1]
        lines.append("| " + " | ".join(row) + " |")
    tot = ["**all**", ""]
    for e in engines:
        if e in scored:
            t = totals[e]
            cell = _pct(t[0], t[1])
            if e in base:
                cell = "%s → %s" % (_pct(t[4], t[1]), cell)
            tot += [cell, _pct(t[2], t[3])]
    lines.append("| " + " | ".join(tot) + " |")
    print("\n".join(lines))
    print()
    for e in engines:
        if e not in scored:
            continue
        print("## %s mis-hearings" % e)
        for p in said:
            miss = scored[e].get(p, {}).get("miss") or {}
            if miss:
                print(
                    "- %s: %s"
                    % (
                        p,
                        "; ".join(
                            "%r×%d" % (k, v)
                            for k, v in sorted(miss.items(), key=lambda x: -x[1])
                        ),
                    )
                )
        print()
    if args.corpus:
        rows = []
        seen = set()
        # Every run's: the baseline's (--vs) mis-hearings too, sorted.
        for runs in (scored, base):
            for e in engines:
                for p, s in (runs.get(e) or {}).items():
                    for heard in s["miss"]:
                        if heard and (p, heard, e) not in seen:
                            seen.add((p, heard, e))
                            rows.append(
                                {
                                    "said": p,
                                    "heard": heard,
                                    "engine": e,
                                    "kind": s["kind"],
                                }
                            )
        order = {p: i for i, p in enumerate(said)}
        rows.sort(key=lambda r: (order.get(r["said"], 99), r["engine"], r["heard"]))
        path = ROOT / "tests" / "unit" / "data" / "voice_command_corpus.json"
        # One row a line: the corpus is read, and diffed, by people.
        body = ",\n".join(" " + json.dumps(r, ensure_ascii=False) for r in rows)
        path.write_text("[\n" + body + "\n]\n")
        print("%d corpus rows → %s" % (len(rows), path))
    return 0


# --------------------------------------------------------------------------- #
# False positives
# --------------------------------------------------------------------------- #
def cmd_guard(args) -> int:
    from backend.web.core import voice_router, voice_words

    words = voice_words.effective({})
    hints = _hints(words, args)
    hear = _engine(args.engine, hints, words)
    gdir = DATA / "guard"
    items = json.loads((gdir / "manifest.json").read_text())["items"]
    _wait_quiet()
    fp = 0
    out = []
    for it in items:
        heard = hear(_read(gdir / it["wav"]))
        ctl = voice_words.control_kind(heard)
        cmd = voice_router.command(heard)
        cmdk = cmd.get("kind") if cmd else None
        bad = (
            bool(ctl)
            or cmdk not in (None, "new")
            or (not it["text"] and bool(_toks(heard)))
        )
        fp += bad
        out.append(dict(it, heard=heard, control=ctl, command=cmdk, false_positive=bad))
        print(
            "%s %-60s → %-55r ctl=%s cmd=%s"
            % ("FP" if bad else "  ", it["text"] or it["voice"], heard, ctl, cmdk)
        )
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / ("command-ears-guard-%s-%s.json" % (args.engine, args.label))
    path.write_text(json.dumps({"hints": hints, "rows": out}, indent=1))
    print("%d false positives of %d → %s" % (fp, len(items), path))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render")
    r.add_argument("--seed", type=int, default=11)
    for name in ("run", "guard"):
        x = sub.add_parser(name)
        x.add_argument(
            "--engine", choices=("moonshine", "whisper"), default="moonshine"
        )
        x.add_argument("--label", default="after")
        x.add_argument("--hints-from", default="")
        x.add_argument("--limit", type=int, default=0)
    rep = sub.add_parser("report")
    rep.add_argument("--label", default="after")
    rep.add_argument("--vs", default="")
    rep.add_argument("--corpus", action="store_true")
    args = ap.parse_args(argv)
    return {
        "render": cmd_render,
        "run": cmd_run,
        "report": cmd_report,
        "guard": cmd_guard,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
