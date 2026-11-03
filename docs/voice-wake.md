# The wake word ("MindFlock")

The desktop app and the phone page can keep the microphone in a **standby**:
it watches the sound level, and when a short lone utterance bounded by
silence comes in it sends just that clip to `POST /api/voice/wake`
(`backend/web/routes/speech.py`, [web-api.md](web-api.md)). A yes starts
listening. This page is how the server decides, the bench that measured it,
and how to tune it to a real voice.

## How a clip is judged

1. **Hinted decode.** `Backend.wake_transcribe` (local faster-whisper: the
   fast streaming model, `base.en` for English, greedy, no VAD) decodes the
   clip with the word as `initial_prompt` (`"MindFlock"`). Never with
   `word_timestamps`: it turns the prompted decode into "MindFlock MindFlock
   MindFlock" loops.
2. **The judge** (`core/wake.py` `judge(text, word)` → `(wake, reason)`;
   `is_wake` is its yes/no):
   - a greeting in front is dropped (`LEADS`: hey, hi, ok, okay, yo, hello,
     unless the word itself starts with it, like "hey jarvis"), and so is an
     exact repeat of a word ("MindFlock MindFlock");
   - what is left must be the word alone, in at most one token more than it
     has: a sentence never wakes;
   - **ours** (`mindflock`, the default) is matched **strictly**: the letters
     (`word`), a known form from `core/voice_words.HEARD["mindflock"]`
     (`form`: "mind flock", "my flock", "mind flunk", "minh flock" …) or
     its sound shape (`sound`: a mind-like first part — mind, mine, min,
     minh, mint, mend, men, myn … — then an fl/cl/bl/pl/fr onset, a vowel
     and at most five letters, as one token or as "mind" + "flap"). There
     is no generic letter slack: two edits let "Windlock" and "Mild block"
     in. "mindful" is excluded by name;
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
   flock-like after (no fl, cl, bl, pl, kl, gl, tl or fr onset: "mind",
   "Mindset.", "Minefield.", "mind again", "Mind fog.", "mind the gap"), or
   is a known other word ("Mindful."). Anything else keeps the yes: an
   empty or odd plain decode ("Men flat.", "Amen.", a bare "My") is the plain
   model missing the word, not hearing another one. A failed plain pass
   keeps the yes too.

A no never pays for the second pass. On the bench the plain pass ran on 41%
of all checks (every prompted yes, look-alikes included); in real use the
standby sends mostly non-wake speech and the prompted pass says no to most
of it.

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

## Running the bench

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
