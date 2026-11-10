# The wake word ("MindFlock")

The desktop app and the phone page can keep the microphone in a **standby**:
it watches the sound level, and when a short lone utterance bounded by
silence comes in it sends just that clip to `POST /api/voice/wake`
(`backend/web/routes/speech.py`, [web-api.md](web-api.md)). A yes starts
listening. This page is how the server decides, the bench that measured it,
and how to tune it to a real voice.

## How a clip is judged

1. **Hinted decode.** `Backend.wake_transcribe` (local faster-whisper: the
   fast streaming model, `base.en` for English) decodes the clip with the
   word as `initial_prompt` (`"MindFlock"`), the way `core/wake.py`
   `decode_options()` says: greedy, **once** (`temperature` 0 — no
   fallback), no VAD, **no no-speech filter**, at most `MAX_TOKENS` (12)
   tokens, on a `PAD_SECONDS` (10 s) encoder input instead of Whisper's
   30 s (`core/speech.py`'s short padding, as the streaming passes have).
   Never with `word_timestamps`: it turns the prompted decode into
   "MindFlock MindFlock MindFlock" loops. Why each one: see "Measured
   (2026-11-04)" below.
2. **The judge** (`core/wake.py` `judge(text, word)` → `(wake, reason)`;
   `is_wake` is its yes/no):
   - a greeting in front is dropped (`LEADS`: hey, hi, ok, okay, yo, hello,
     unless the word itself starts with it, like "hey jarvis"), and so is an
     exact repeat of a word ("MindFlock MindFlock");
   - what is left must be the word alone, in at most one token more than it
     has: a sentence never wakes;
   - **ours** (`mindflock`, the default) takes **anything next to it** —
     the owner's rule (2026-11-05), after the strict rule of 2026-11-03
     turned down his own "MindFlock" decoded as "MindFock", "Mind Fuck",
     "Mindfock." and "Mindful, I'll": the letters (`word`), a known form
     from `core/voice_words.HEARD["mindflock"]` (`form`: "mind flock", "my
     flock", "mind flunk", "mindfock" …), its sound shape (`sound`: a
     mind-like first part — mind, mine, min, minh, mint, mend, men, myn … —
     then an fl/cl/bl/pl/kl/gl/fr/ph onset and a vowel, or a bare f and an
     o/u ("fock", "fuck", "fog", "ful"), and at most five letters, as one
     token or as "mind" + "flap"; or a my-like first part — my, mi, mai —
     with an fl/cl/kl/fr/f onset and an o/u vowel: "My clock.", "My fock",
     never "My flag", "My phone" or "my block"), or two letters off the word
     (`near`: "Windlock", "Mild block", "Bindflock"). The veto below still
     turns down a plain "Mind." or "Mine.";
   - a **custom** word (`voice.words.wake`) keeps the letter slack (two
     edits for 8+ letters, one for 5–7, none below: `near`).
3. **The veto** (ours only, and only on a yes). The hint makes whisper write
   "MindFlock" for things that are not — "Mind.", "Mine.", "Mindful.",
   "Minefield.", "Mind the gap." — so a yes is checked by a second decode of
   the same clip **without** the hint on the same model
   (`Backend.wake_verify_transcribe`; local faster-whisper only, other
   backends return `None` and the yes stands). `core/wake.py` `veto(plain)`
   turns the yes down (`reason: "plain:<what it heard>"`) when the plain text
   is up to three words that start like mind / mine / my with nothing
   flock-like after (no fl, cl, bl, pl, kl, gl, tl, fr or ph onset, nor an
   f and o/u: "mind", "Mindset.", "Minefield.", "mind again", "mind the
   gap"), or is a known other phrase ("Mind the gap."). "Mindful.",
   "Mindfock." and "Mind fog." keep the yes since 2026-11-05: they are how
   the plain model hears a quick "MindFlock". Anything else keeps the yes: an
   empty or odd plain decode ("Men flat.", "Amen.", a bare "My") is the plain
   model missing the word, not hearing another one. A failed plain pass
   keeps the yes too.

A no never pays for the second pass. On the bench the plain pass ran on 41%
of all checks (every prompted yes, look-alikes included); in real use the
standby sends mostly non-wake speech and the prompted pass says no to most
of it.

## The standby (both clients)

`frontend/src/lib/wakeWord.ts` (desktop) and `backend/web/static/mobile.js`
(phone, `voiceWakeStep` / `voiceWakeCheck`) follow the same rules:

- **A lone utterance**: speech after 350 ms of quiet, 250 ms–1.6 s of it,
  then 200 ms of quiet (the tail: the clip goes in that block — on the
  desktop's 85 ms blocks, ~256 ms after the word ends). Longer is a
  sentence, shorter a click; both are never sent.
- **A pause inside it** ("Hey, MindFlock", or "Mind… flock" said slowly)
  is longer than the tail, so its first part goes alone. Speech that
  resumes less than 350 ms after a stretch that started after quiet (and
  was not a sentence) carries that stretch on: the next clip holds both
  parts, under the same 1.6 s. Before, the second part read as "speech
  straight on from speech" and was never sent.
- **One check in flight, one per 0.4 s** — and a candidate that comes
  meanwhile is **kept** (the newest; its clip taken when it came) and goes
  the moment that check says no, if its word ended at most 2.5 s before;
  a yes drops it. Before, it was dropped, with a 1.5 s gap: with the
  server's check taking 1.5–8 s on a loaded CPU, a cough, a key click or a
  first try just before "MindFlock" swallowed the word itself.
- Nothing while the page talks (and 500 ms after), records, or is hidden.

While standby is on, each client holds a listening lease (`POST
/api/voice/listen`), which keeps the local whisper models — the wake
check's `base.en` among them — loaded.

**Held for memory.** The local recognizer is refused only while the memory
guard reads `critical` (not at `low`). Then `/api/voice/wake` answers **503**
`{error, wake: false, held: true}` and both clients **pause** instead of
stopping: the mic stays open, the line reads *Standby paused: memory is low*
(desktop) or *Paused — the server's memory is low* (phone), and the next
answered check clears it. Any other 409 still means no recognizer and stops
standby.

## Measured (2026-11-04): what the owner hit, and the fix

The owner: "MindFlock" is rarely heard while "stop listening" is, and a
wake sometimes takes 5 s. "Stop listening" is said while listening: the
streaming recognizer hears it in context (two common words, the
vocabulary as hints on the accurate pass). "MindFlock" goes through the
standby: one made-up word, cut out by the level detector, judged alone.

The live server's `GET /api/voice/wake/recent` (14 checks, 13:21–13:24,
the owner trying again and again): **no wake**; 10 hinted decodes came back
**empty**, the rest "I'm sorry.", "Both eyes.", "of"; a single pass took
**0.8–8.4 s** (median ~1.5 s), and a yes would have paid a second one.

Where the time went, per check: the 30 s encoder input (a lone word's
pass ~0.8 s on the fast tier's two threads, idle), faster-whisper's
temperature fallback (an uncertain lone word re-decoded at up to five
temperatures, then — with its no-speech filter — often dropped: the
empty texts), twice for a yes (the veto's plain pass), on a CPU shared
with agents; plus the standby dropping every word said while a check was
in flight. The client side is ~0.26 s (the tail) and a round trip.

`tests/bench/wake_stream_bench.py` (below) on clips cut by the clients'
detector out of a simulated microphone stream — piper `lessac` and
Windows SAPI David/Zira, room noise RMS 0.002 and 0.008, mic gains 0.06
and 0.18, every 4th of 2064 streams (72 "MindFlock" streams, 205
look-alikes, 193 ordinary short phrases — "Stop listening.", "Okay.",
"Thanks." … — and 12 non-speech: cough, key clicks, hum, knock):

| Decode | Recall | False wakes (look-alike / ordinary / noise) | Hinted pass median / p90 | Plain pass median / p90 |
|---|---|---|---|---|
| Before: 30 s, fallback, no-speech filter | 67/72 93% | 5 / 0 / 0 | 813 / 1285 ms | 842 / 1977 ms |
| **10 s, once, no filter, 12 tokens** | 65/72 90% | **0 / 0 / 0** | **88 / 112 ms** | **86 / 108 ms** |
| … with `without_timestamps` | 65/72 90% | 1 / 0 / 0 | 90 / 109 ms | 88 / 110 ms |
| … with whisper's no-speech filter (0.6, −1.0) | 65/72 90% | 0 / 0 / 0 | — | — |

(Two threads, `base.en`, int8, a load average ~5.) Shorter encoder inputs
break whisper: at 5 s it wrote "Mind Mind", at 3 s "M M M M" (5/20 and
1/20 on a probe). With no no-speech filter, noise still never woke (the
hinted pass writes nothing for it), so the filter is off: a real but
uncertain "MindFlock" now reaches the judge as text instead of as nothing.

So a yes takes ~0.26 s (the tail) + ~0.2 s (two passes) + a round trip
from the word's end, down from 2–5 s; the four misses the two decodes
share are a "Hey, MindFlock." cut at its comma (the standby's pause rule
above now sends the whole), a fast "MindFock"/"Mind fuck?" (vetoed: "Mind
fog." decodes the same) and a fast SAPI "My clock." (the my-like sound
shape above now takes it).

## Measured (2026-11-03)

`tests/bench/wake_bench.py` on 336 synthetic clips, each clean and at 15 dB
SNR: 192 positive decodes ("MindFlock." "MindFlock!" "Mind flock."
"MindFlock?" "Mindflock" "Hey, MindFlock." from piper lessac at 5 speeds ×
2 timbres and Voice Lab's Harvey and Donna, 3 takes each) and 480
look-alike decodes ("Mind." "Mine." "Mindful." "Minefield." "Mind the
gap." "Wind lock." "Mild block." "Line clock." "Mind fog." "Flock." "My
flag." "Mindset." "Bedrock." "Hemlock." "Wedlock." "Blockchain." "Find
out." "Okay." "Hello." "My phone."). `base.en`, greedy, no VAD.

| Rule | Recall | False wakes |
|---|---|---|
| Before: 2-edit slack on the prompted decode, no lead word | 152/192 79% | 130/480 27% |
| Plain decode only, strict | 148/192 77% | 0/480 0% |
| Strict on the prompted decode (lead + repeat dropped) | 186/192 97% | 90/480 19% |
| … + veto when the plain text is one of a fixed set (mind, mine, mindset, minefield, mindthegap, mindful, mindfully, mined, minds) | 183/192 95% | 23/480 5% |
| … + veto, plain text up to 2 words | 181/192 94% | 13/480 3% |
| … + veto as shipped: mind/mine/my start with no flock-like rest, up to 3 words, a bare "My" kept | **181/192 94%** | **11/480 2%** |
| … the same, but a bare "My" also vetoed | 178/192 93% | 8/480 2% |
| … the same, but "Mindful" not vetoed | 185/192 96% | 34/480 7% |

`small.en` was no better and three times slower. Timing on a loaded box: the
prompted pass's median was ~330 ms (p90 ~560 ms), the plain pass ~350 ms
(p90 ~1.3 s, noisy), so a yes takes about twice a no.

**Decision:** strict judge + the generalised veto. It keeps the recall of
the strict prompted rule within three points and drops false wakes from 27%
to 2%. A bare plain "My" keeps the yes: on the bench it was as often a
cut-off "MindFlock" (3) as a "Mine."/"Mind." (3), and recall is what the
owner asked for.

What is still wrong on the bench:

- **misses**: the plain pass hearing "Mindful." / "Mindful luck!" /
  "Mindfock." for a real "MindFlock" (5: the veto's cost), the hinted pass
  writing nothing or another word ("Much.", "Kneeflock.", "Mayful.", "My").
- **false wakes**: "Line clock." (4) and "Flock." → "The clock." / "long"
  (2), where the plain pass heard a different first word that is not
  mind-like, so the veto does not apply; a few "Mine." → plain "My".

These are synthetic voices. The owner's own voice is the real test; use the
diagnostics below.

## Running the benches

`tests/bench/wake_stream_bench.py` is the realistic one (the standby's own
cuts, room noise, many voices, ordinary speech); `wake_bench.py` decodes
ideal clips trimmed to the speech. Both need the tool venv:

```bash
PYTHONPATH=$PWD ~/.local/share/uv/tools/mindflock/bin/python \
    tests/bench/wake_stream_bench.py corpus      # piper + SAPI (+ Voice Lab) → stream_clips.npz
EVERY=4 ... wake_stream_bench.py decode fast server   # VARIANTS in the script
... wake_stream_bench.py score [--detail fast]   # re-reads core/wake.py: seconds
```

`NOLAB=1` skips Voice Lab (on a CPU-only box its F5 engine takes minutes a
clip); SAPI needs `powershell.exe` (WSL or Windows). `EVERY=N` decodes
every Nth stream (the same ones for every variant). Timings are only
worth anything on an idle box.

`wake_bench.py`:

It needs the local models' packages (faster-whisper, piper, numpy), which
live in the uv tool venv, not the dev venv:

```bash
PYTHONPATH=$PWD ~/.local/share/uv/tools/mindflock/bin/python \
    tests/bench/wake_bench.py            # synth (once) → decode (once) → score
... wake_bench.py score                  # re-score the cached decodes (seconds)
... wake_bench.py decode                 # decode again (~8 min for 336 clips)
... wake_bench.py synth                  # synthesise the clips again
... wake_bench.py score --probe FILE     # score a decode file from elsewhere
```

- `MINDFLOCK_WAKE_BENCH` — the cache folder (default
  `~/.cache/mindflock/wake_bench`, outside the repo): `clips.npz` and
  `probe_<model>.json`.
- `MODEL` — the model to decode with (default `base.en`).
- `VOICE_LAB_URL` (default `http://127.0.0.1:8770/v1`, any OpenAI-compatible
  `/audio/speech`), `VOICE_LAB_VOICES` (default `Harvey,Donna`), `NOLAB=1`
  to synthesise with piper only.

`score` re-reads `core/wake.py`, so a rule change is measured in seconds
against the cached decodes; only a change to the decode itself (model,
prompt, beam) needs `decode`. The rules it compares are `RULES` in the
script; add one there before changing `core/wake.py`.

## Tuning to a real voice

Every check is kept in memory (the last 50) and served at
`GET /api/voice/wake/recent`:

```bash
curl -s -H "Authorization: Bearer $TOKEN" localhost:8765/api/voice/wake/recent | jq '.attempts[] | [.wake, .reason, .text_prompted, .text_plain, .ms]'
```

Each row is `{ts, text_prompted, text_plain, wake, reason, ms}`; each check
also logs one `DEBUG` line on `backend.web.routes.speech` (`voice wake: yes
(word) prompted='MindFlock' plain='Mind flock.' 640 ms`).

- A missed "MindFlock" with `reason: "other"`: the hinted pass wrote
  something the judge does not know. If it is a spelling no one would say
  alone, add it to `HEARD["mindflock"]` in `core/voice_words.py` (and to the
  `wake` list in `tests/unit/data/voice_sounds.json`, which pins it; at most
  24 forms).
- A missed one with `reason: "plain:…"`: the veto took it. Look at what the
  plain pass heard; if it is how the plain model hears your "MindFlock",
  narrow `veto` (or add the spelling as a form, which the veto keeps).
- A false wake: the reason says which path let it in (`form`, `sound`, or a
  plain text the veto did not recognise).
