# Voice STT latency audit (2026-11-06)

Question: speech recognition "is ok but feels slower than I want" (the owner
compares it to Siri). Where does the delay come from, and what would make
the words appear sooner and feel better?

Scope: code reading, the live server's log, and one light model timing
(load average 2.1 to 2.5 on 8 cores; stopped when it reached 3.4). No product
code was changed.

Labels used below:

- **[M]** measured here.
- **[L]** read from the live log.
- **[B]** bench number quoted in code comments or docstrings.
- **[D]** derived from constants in the code.

The owner's setting is `voice.stt = "server"`, `stt_backend = "local"`
(`~/.mindflock/settings.json`). The phone therefore uses the same
faster-whisper stream as the desktop app, not the OS recognizer. The OS
recognizer (`webkitSpeechRecognition`) is only the phone's *default*.

## TL;DR

The live words are not the main problem. Both clients already type the
unstable tail as a guess as it arrives:

- desktop: `Dictation.guess()` at `frontend/src/lib/voiceDesktop.ts:1105-1117`;
- phone: `onGuess` at `backend/web/static/mobile.js:~4768`;
- both also show it muted in the caption.

So LocalAgreement-2 costs correction churn, not visible lag. The slow part
is everything **after the person stops talking**:

1. **The accurate `small` pass at `/end` costs about 1.1 to 1.5 s, and very
   often runs out its 1.5 s deadline.**
   - It always encodes a 30 s padded input (`FINAL_PAD_SECONDS = 0`,
     `backend/web/core/speech.py:1226`), so 1 s of speech costs the same as
     30 s.
   - **[M]** 1.13 s and 1.18 s on a 3.3 s phrase, 6 threads, light load.
   - **[L]** `~/.mindflock/desktop-server.log` holds **99** lines of "the
     accurate pass took over 1.5s on N s of audio (still running); the fast
     text stands". The phrases were short (p50 1.5 s, p90 3.6 s of audio).
     Each of those `/end`s waited the full 1.5 s and then threw the result
     away.
2. **The early (speculative) accurate pass never helps the "over" path.**
   - The server starts it after 0.5 s of quiet (`EARLY_QUIET_SECONDS`,
     `backend/web/core/speech_stream.py:274`).
   - The desktop ends an "over" phrase after **100 ms** of quiet
     (`OVER_EARLY_QUIET_MS`, `voiceDesktop.ts:821`), or 250 ms
     (`CONTROL_TAIL_MS`, `:811`; phone `VOICE_CONTROL_TAIL_MS`,
     `mobile.js:3518`).
   - The server's own turn verdict holds only 350 ms after a radio word
     (`speech_turn.py:84`).
   - So "…, over" always pays a fresh accurate pass from zero at `/end`.
3. **Endpointing without a radio word costs 0.9 to 1.5 s of silence**
   (`turn.hold_ms`) before `/end` even starts. The ~1.1 s final then follows,
   so a typed phrase is corrected about 2.0 to 2.7 s after the person
   stopped.
4. **The next phrase stalls behind the previous `/end` (desktop).**
   - `openStream` does `await lastEnd` (`voiceDesktop.ts:4900-4904`).
   - When the person resumes after a pause, their first new words wait out
     the rest of the old phrase's 1.1 to 1.5 s final, plus a stream-open
     round trip.
5. **Cold starts.**
   - The STT models are not warmed at boot (`backend/web/server.py:784-788`).
   - They unload after 30 min idle (`speech.py:134`).
   - Under memory pressure, idle models are shed after only 120 s
     (`speech.py:494`). The owner's box is often under memory pressure.
   - **[M]** a reload is about 2.1 s for `base.en` and 1.2 s for `small`
     with a warm disk cache. The code comment says `small` takes about 7 s
     when cold.

The two biggest easy wins:

- **(a) Make the accurate pass cheap or early enough to always land.**
  Start it the moment the server's own fast pass hears a radio word. Pad it
  to 20 s instead of 30 s. Cap its decoding.
- **(b) Stop serialising the next phrase behind the last `/end`.**

A truly streaming transducer model would remove the whole two-tier
re-decoding scheme.

## 1. Timeline of one utterance

Example utterance: "fix the failing test, over". The desktop is described
first; phone differences follow. T = 0 is the button press or hotkey, with
the mic already warm (`KEEP_STREAM_MS = 90 s`, `frontend/src/lib/voiceMic.ts:38`).

| # | Stage | Cost | Source |
|---|---|---|---|
| 0 | Mic start, warm | ~0 ms. The recorder starts at once (`voiceDesktop.ts:5221`) with a 500 ms pre-roll ring (`voiceMic.ts:298`). The stream POST is fired in parallel and audio is buffered until it answers (`voiceDesktop.ts:5116`). | [D] |
| 0′ | Mic start, cold | `getUserMedia` + `ctx.resume()` + the worklet load, which can wait up to `CAPTURE_WAIT_MS = 1500` (`voiceMic.ts:406`) before falling back to ScriptProcessor. **Speech during this wait is lost**: the pre-roll ring only starts once the context runs (`voiceMic.ts:192`; `voiceDesktop.ts:5118-5166`). | [D] |
| 0″ | Model cold | Only after a 30 min idle unload or a memory shed. The first chunk's pass blocks on `_cached` → `_load` under the global `_LOCK` (`speech.py:393-406`): about 2 s for `base.en` [M], more on a cold disk. The client shows "Starting the mic…". | [M][D] |
| 1 | First chunk | It is sent after `FIRST_CHUNK_S = 0.15` s of speech (`voiceDesktop.ts:4501`). The worklet delivers 4096-frame blocks (~85 ms at 48 kHz, `voiceCapture.worklet.ts:25`), so the threshold quantises to about 170 ms. The phone pads its first chunk to 300 ms (`mobile.js:~4709`). | [D] |
| 2 | Fast pass (base.en, 2 threads, greedy, 10 s pad) | About 155 to 250 ms. It grows with the window, which can reach 8 s (`WINDOW_SECONDS`). 155 ms on 1.5 s windows, 250 ms on 4 s windows at load ~5 (`speech.py:1216, 1346-1348`). An unpadded 30 s encode would be 0.6 s [M], so the 10 s pad matters. | [B] |
| 3 | First guess visible | Speech onset + about 170 ms + pass + round trip ≈ **0.35 to 0.5 s**. The guess is typed into the terminal at once; there is no per-word pacing. `KeyGate` batches into one `/send` in flight (`voiceDesktop.ts:1315-1360`). | [D] |
| 4 | Live update cadence | Next chunk = clamp(0.8 × pass, 150 ms, 600 ms) (`voiceDesktop.ts:4510-4513`; `mobile.js:3724-3728`). Only one request is in flight, so the effective cadence is ≈ pass + round trip ≈ 0.2 to 0.3 s. A spoken word shows on screen about **0.4 to 0.7 s** after it ends: half a chunk, plus waiting for the in-flight pass, plus a pass, plus `/send`. Whisper also often leaves a half-heard last word for the next pass. | [D] |
| 5 | LocalAgreement-2 commit | A word commits one pass after it first appears (`AGREE_PASSES = 2`, `speech_stream.py:225`). First *committed* word: p50 1204 ms, p90 1817 ms from clip start (`speech_stream.py:222-224`). This is **not visible as lag**, because the guess is already typed. It shows as retyping when a guess changes. | [B] |
| 6 | "over" heard | After "over" ends, the chunk that contains it closes (≤ ~0.2 s), then a pass and a round trip. The guess tail reads "over" about **0.35 to 0.5 s** after it was said. | [D] |
| 7 | End of speech | Desktop: "over" at the guess tail with ≥ 100 ms of client quiet ends the phrase (`voiceDesktop.ts:1857`). By then that quiet has already passed, so the cost is ~0 to 50 ms (`CONTROL_POLL_MS = 50`). Phone: 250 ms (`mobile.js:3518`). Without a radio word: `turn.hold_ms` = 900 ms after a sentence, 1100 default and for the first phrase, 1500 after a comma (`speech_turn.py:84-94`; `voiceDesktop.ts:4465-4471`). | [D] |
| 8 | Phrase end round trips | `await c.busy`, then the tail chunk (another fast pass, ~0.2 s), then `/end`: up to three serial requests (`voiceDesktop.ts:4998, 5011, 5017`). The phone does the same: one more chunk, then `/end` (`mobile.js:~4687-4689`). | [D] |
| 9 | `/end` fast flush | Skipped only if a fast pass already heard 0.3 s of quiet after the last loud frame (`_all_heard`, `speech_stream.py:859-865`). After a 100 ms over-quiet that has not happened, so it runs (~0.2 s, on the fast lane, alongside the final). | [D] |
| 10 | **Accurate pass (small, 6 threads, beam 1, full 30 s encoder, hotwords)** | **~1.1 to 1.2 s [M] at light load, often >1.5 s under load [L].** The early pass did not start on this path (§TL;DR 2), so this is the full cost. Deadline `FINAL_TIMEOUT_S = 1.5` (`speech_stream.py:243`). | [M][L] |
| 11 | Client wait for the final | Desktop `sendWait` = `overWaitMs` = clamp(1.5 × typical + 100, 300, 1500) ms (`voiceDesktop.ts:838-841`). With the server timing out, the typical value is about 1.6 s, so this pins at **1500 ms**. Phone: `voiceEndBudgetMs`, same rule (`mobile.js:3736-3740`). | [D] |
| 12 | Final replaces the guess | Only the differing tail is retyped (`voiceDesktop.ts:1122-1130`). | [D] |
| 13 | Enter | The final tail's words go in one `/send`, then `await gate.settled()`, then a second `/send` with `enter: true` (`voiceDesktop.ts:2043, 2077, 2085`). That is two serial local round trips, ~20 to 60 ms. | [D] |

**From the end of "over" to Enter:**

- ≈ 0.4 (hear "over") + 0.2 (tail chunk) + 1.1 to 1.5 (final) + 0.05 (send)
  ≈ **1.8 to 2.2 s**.
- When the final times out, ≈ 2.2 to 2.4 s, and it still uses the fast text.

**From the end of a sentence without a radio word to the corrected text:**

- ≈ 0.9 to 1.1 s (hold) + 0.2 + 1.1 to 1.5 s
  ≈ **2.2 to 2.8 s**.
- The guess was visible all along, but the person watches it get corrected
  about 2.5 s later.

**Siri, for contrast:**

- An on-device streaming transducer shows partials about 0.2 to 0.3 s behind
  speech.
- It endpoints after roughly 0.5 to 0.8 s of silence, with the final already
  computed.
- It has no second re-decode.

**Phone differences** (`backend/web/static/mobile.js`):

- The mic is the same warm 90 s stream (`VOICE_KEEP_STREAM_MS`, `~3529`).
- The capture worklet module is **not pre-loaded**. `capLoad()` only runs in
  `start()` (`~4472`), so the first press after a page load can wait up to
  1.5 s (`VOICE_CAP_LOAD_MS`) and record that phrase through ScriptProcessor
  + an OfflineAudioContext resample per chunk (`~3582-3603`).
- Audio before `recStart` is not buffered on a cold mic (`~4473-4476`). Only
  300 ms of lead is kept once recording starts (`VOICE_LEAD_MS`).
- The worklet posts 42.5 ms blocks and is tapped 2 at a time, giving 85 ms
  granularity (`~3511-3512`).
- The first chunk is padded to 300 ms (`~4709-4710`), so the phone's first
  guess is about 0.1 s later than the desktop's.
- Release with speech queued costs a chunk round trip and then `/end`
  (`~4687-4689`).
- The `/end` budget is a 50/50 running mean (`~4834`), so one slow `/end`
  swings the next one by half its excess.

## 2. What dominates the perceived slowness, ranked

1. **The accurate pass at `/end` (≈ 1.1 to 1.5 s, often timing out).**
   - The person has stopped talking and is waiting; this is dead time.
   - It is about 60 % of "over"→Enter.
   - When it times out (99 logged times), the wait buys nothing.
2. **Endpoint holds when there is no radio word (0.9 to 1.5 s).**
   - This silence is required before `/end` starts at all.
   - It adds to #1 rather than overlapping it: the early pass only overlaps
     about 0.4 to 0.6 s of it.
3. **The next phrase blocked on the previous `/end`** (desktop
   `voiceDesktop.ts:4904`).
   - Speaking again after a pause shows nothing for up to about 1.5 s plus
     an open round trip.
   - Of all the delays here, this one most likely feels like "it's lagging
     behind me".
4. **Live-word lag of 0.4 to 0.7 s plus guess churn.**
   - Whisper re-decodes the whole window on every chunk, the last word is
     often missing until the next pass, and committed words lag a further
     pass.
   - Rewrites of the typed guess make it feel less sure than Siri, even when
     it is not much slower.
5. **Cold paths.**
   - Model reload of 1 to 7 s after a 30 min idle or a 120 s memory shed.
   - Cold mic or worklet of up to 1.5 s, and the first syllables are lost on
     the desktop.
   - These are rare but memorable.

## 3. Improvements, ranked by impact ÷ effort

All of these are model-independent except E and G.

| Rank | Change | Saves | Effort | Files |
|---|---|---|---|---|
| **A** | **Start the accurate pass the moment the server's fast pass hears a trailing radio or stop phrase.** `_after_control` and `voice_words.control_kind` already detect it, and the server sees "over" a full round trip before the client acts on it. Today the pass waits for 0.5 s of quiet that the "over" path never reaches. Also lower `EARLY_QUIET_SECONDS` to about 0.3 s for plain sentence ends (the client hold is ≥ 900 ms there). Withdraw on more speech, as today. | 0.3 to 0.6 s off every "over"; nearly the whole remaining wait on sentence ends | S | `backend/web/core/speech_stream.py` (`feed` / `_start_early`, about lines 715-722 and 835-850) |
| **B** | **Make the final pass bounded and cheaper.** Pass `temperature=0.0` and a `max_new_tokens` cap, as the fast tier already does (`speech.py:1540-1546`), so no temperature-fallback runaway. Try `FINAL_PAD_SECONDS = 20`: the code's own bench gives 644 ms vs 1150 ms p50 for WER 4.8 % → 5.9 % (`speech.py:1221-1225`). Re-bench `small.en` and `distil-small.en` (both downloaded) on idle CPU. | About 0.5 s per final, and far fewer 1.5 s timeouts | S (constants) + a bench run | `backend/web/core/speech.py:1226, 1597-1605`; `tests/bench/voice_bench.py` |
| **C** | **Don't serialise the next phrase's stream open behind the previous `/end`.** Open at once; the server allows 2 streams per client (`CLIENT_STREAMS`). If the CPU must favour the final, let the `Lane` priority handle it rather than the client. | Up to ~1.5 s stall when resuming after a pause | S | `frontend/src/lib/voiceDesktop.ts:4900-4904, 4992-5039` |
| **D** | **Fold round trips at phrase end.** Let `/end` accept the tail audio as its body, to drop a chunk round trip plus a fast pass. Let the "over" send be one `/send` with the text and `enter: true` (`voiceDesktop.ts:2077→2085`). On the phone, send the queued tail with `/end` (`mobile.js:~4687`). | 0.2 to 0.3 s per phrase | S to M | `backend/web/routes/speech.py:1072-1091`, `speech_stream.Stream.finish`, `voiceDesktop.ts:4998-5017, 2035-2089`, `mobile.js:~4516-4538, 4687` |
| **E** | **Decide whether the final should gate Enter at all.** When the final is `same` (only case or punctuation differ), waiting is pure loss. A cheaper gate: send Enter on the fast text when the fast words are stable (≥ 2 agreeing passes over the whole phrase) and the phrase is short, and use the final only when the fast tier was unsure. This needs the fast pass's average log-probability, which faster-whisper returns per segment. Measure first: `voice_bench.py` already reports the `same` rate and WER for fast vs final. | Up to the whole 1.1 s on most phrases | M | `speech_stream.py` (expose a confidence), `voiceDesktop.ts` `checkTail` / `sendWait`, `mobile.js` `controlFire` |
| **F** | **Keep models resident while the owner uses voice at all.** Warm STT when the desktop or phone voice UI mounts (`GET /api/voice` warms only TTS today, `routes/speech.py:~742`). Raise the shed idle time for whisper from 120 s, or exempt `base.en` (~150 MB): reloading it costs the person 2 s, and keeping it costs little memory. Pre-load the phone's worklet module on page load or in `voicePrime` (`mobile.js:~8883`). On the desktop, start the pre-roll ring as soon as the stream exists, not at `capture()`. | Removes the 1 to 7 s cold hits and lost first syllables | S | `backend/web/core/speech.py:134, 494-529`, `backend/web/routes/speech.py:~742`, `mobile.js:~3931-3949, 8883`, `frontend/src/lib/voiceMic.ts:192` |
| **G** | **Earlier endpointing (VAD / end-of-utterance).** The turn model already exists (350/900/1100/1500 ms). Two cheap gains: drop `FIRST_PHRASE_HOLD_MS`'s 1100 ms floor to 900 when the first phrase is not `mayBeCommand`; and hold for only about 600 ms when the early accurate pass is already done and agrees with the fast text (the phrase is "settled"). A learned end-of-utterance model (e.g. the turn-detector class of small transformers) could go lower. Whisper's punctuation is too noisy, as `speech_turn.py` already notes. | 0.2 to 0.5 s on non-radio phrase ends | S to M | `voiceDesktop.ts:4423-4471`, `mobile.js:~3548-3562`, `backend/web/core/speech_turn.py` |
| **H** | **Smaller first chunk / faster cadence.** The worklet block (4096 frames) quantises all thresholds to about 85 ms. Post 1024-frame blocks. Send the first chunk at about 100 ms. The phone should not pad its first chunk to 300 ms (`mobile.js:~4709`). | About 0.05 to 0.15 s on the first guess only | S | `frontend/src/lib/voiceCapture.worklet.ts:25`, `voiceDesktop.ts:4501`, `mobile.js:~3522, 4709` |
| **I** | Show the unstable tail in grey **only**, and type committed words. | Already done | — | Both clients already type the guess and mute the tail in the caption. Typing only committed words would make the text *later*, not sooner. |

### What a truly streaming model would remove

Candidates: sherpa-onnx streaming Zipformer transducer, or NeMo
FastConformer cache-aware streaming.

- **Removed:**
  - Re-decoding the whole window on every chunk. The per-chunk cost becomes
    constant at roughly tens of ms on CPU and stays flat as the phrase grows.
  - LocalAgreement-2's extra pass, and most guess churn: transducer partials
    are monotonic, and only the last token or two change.
  - The hallucination filters for quiet (`SILENCE_SLACK`, the echo and loop
    filters, `PROMPT_IN_WINDOW` tricks), because transducers don't invent
    "Thank you." in silence.
  - The 30 s-padded accurate pass. The final is the stream's own result at
    the endpoint, so nothing is left to wait for. Rows #8 to #11 of the
    timeline collapse to about one round trip.
  - Probably the `small` model's ~1 GB resident memory, which also removes
    the memory-shed cold starts.
- **Gained:** built-in endpoint rules (trailing silence after speech,
  typically 0.5 to 1.2 s and configurable) that can replace or seed
  `turn.hold_ms`. The radio word "over" plus a short hold would then send in
  about 0.3 to 0.5 s.
- **Risks / unknowns:**
  - Accuracy on the command vocabulary. sherpa-onnx transducers support
    hotword / contextual biasing with modified beam search; that needs a
    bench run against `voice_bench.py`'s phrases.
  - Session names and code words.
  - Punctuation and casing, which many streaming models don't output.
  - A new runtime dependency: onnxruntime is already in the tool venv for
    piper.
- **Path of least risk:** keep the client protocol (`/stream`,
  `chunk → {committed, partial, turn}`, `/end → {final}`) and add a
  `StreamingSTT` backend behind `stream_transcriber`. `Stream` would then
  skip the window, the agreement and the accurate tier when the backend
  says it streams natively.

## 4. Bugs and inefficiencies found on the way

1. **Zombie accurate passes steal the CPU.**
   - `_stale_early` → `Lane.cancel` *abandons* a running final
     (`speech_stream.py:852-857, 548-561`), but CTranslate2 only checks
     `abandoned()` between segments (`speech.py:~1607-1610`). A short phrase
     is one segment, so the abandoned `small` keeps 6 threads busy for its
     full ~1.1 s.
   - Example: a pause of ≥ 0.5 s before "over" (common: `turn_bench.py`
     stretches exactly this) starts an early pass. "over" makes it stale.
     The fresh final at `/end` then runs beside the zombie, and both slow
     down.
   - This is a likely contributor to the 99 logged timeouts.
2. **Final pass decoding is unbounded.**
   - No `temperature=0.0` and no `max_new_tokens` (`speech.py:1597-1605`;
     compare the fast tier at `1540-1546`).
   - A low-confidence pass can run faster-whisper's temperature fallback.
     The code itself records 28 s at a 10 s pad (`speech.py:1225`).
   - Combined with #1, a runaway final can hog the cores for many seconds.
3. **The early-pass timing does not match the clients' radio-word timing.**
   - `EARLY_QUIET_SECONDS = 0.5` (`speech_stream.py:274`) against client
     radio quiet of 100 / 250 ms (`voiceDesktop.ts:821, 811`;
     `mobile.js:3518`) and the server's own 350 ms hold (`speech_turn.py:84`).
   - The docstring at `speech_stream.py:269-273` still reasons from the old
     fixed 1.1 s `SILENCE_MS`.
4. **A memory-pressure check refuses passes on an already loaded model.**
   - `LocalSTT._model` checks `_memory_block()` *before* the cache lookup
     (`speech.py:1435-1437`).
   - Under pressure every fast pass raises `SpeechError`. The client drops
     to clip mode ("Streaming stopped…") and the clips are refused the same
     way, although no load was needed.
   - Fix: check only when the key is not cached.
5. **WAV decode runs on the event loop with a per-sample Python generator.**
   - `routes/speech.py:1053` → `speech_stream.decode_wav`
     (`speech_stream.py:1212`).
   - Small per 300 ms chunk (~5 k samples), but it is pure-Python work on
     the loop. Moving it into the `_stt` call costs nothing.
   - The same pure-Python per-sample loops run 3 to 4 times per chunk under
     the stream lock (`_learn`, `_quiet`, `_mark`, `_keep` / `_voiced`,
     `speech_stream.py:327-371, 660-665`). That is fine at this size, and
     `array` / `audioop` would be faster if it ever matters.
6. **Settings are re-read on every chunk.** `_feed` → `_words()` →
   `load_settings()` (`routes/speech.py:~1016`). I did not check whether it is
   cached; if not, that is file I/O on every chunk.
7. **The next stream open is serialised behind the previous `/end`.**
   `voiceDesktop.ts:4900-4904` and `4992`. See improvement C.
8. **Serial round trips at phrase end and on "over".**
   - Phrase end: `voiceDesktop.ts:4998 → 5011 → 5017`; `mobile.js:~4687-4689`.
   - Enter after "over": `voiceDesktop.ts:2077 → 2085`.
9. **The phone's `/end` budget is a 50/50 running mean** (`mobile.js:4834`):
   one slow `/end` moves the next budget by half its excess. The desktop
   uses an EMA of 0.6 / 0.4 (`voiceDesktop.ts:853-855`). Both should be a
   median of recent values, as the server does in `final_stats`.
10. **`final_stats` gets pinned at the timeout.**
    - With many timeouts, the recorded waits (1500 ms each) make the median
      1500. `expect_ms` and the clients' "over" wait then sit at the 1.5 s
      cap (`speech_stream.py:1146-1184`).
    - Recording a timed-out wait as "timeout", not as its value, would let
      the client wait only as long as a *successful* final takes, then go
      on with the fast text.
11. **Cold desktop press loses speech.** The pre-roll ring starts only once
    the AudioContext runs (`voiceMic.ts:192`; `voiceDesktop.ts:5118-5166`).
    The phone keeps nothing before `recStart` (`mobile.js:~4473-4476`).
12. **The phone worklet is not pre-loaded.** `mobile.js:~3931-3949` runs
    only from `start()`, so the first press can wait 1.5 s and then use
    ScriptProcessor + OfflineAudioContext resampling.
13. **The timeout log line is misleading.** `speech_stream.py:899-905` always
    says "took over 1.5s". For an early pass that started long before
    `/end`, the real running time is `final_ms` (often larger). Logging
    `final_ms` and `final_early` would make the 99 lines diagnosable:
    early-and-stale or fresh?
14. **Docs drift.**
    - `speech.py:553` says idle models go "within 10–20 minutes";
      `IDLE_SECONDS = 1800`.
    - `speech.py:2831` says the server warms both kinds at boot;
      `server.py:784-788` warms TTS only, on purpose.
15. **The timings are not visible.** Per-utterance `final_ms`,
    `final_wait_ms`, `final_early` and pass ms reach only the client. The
    server logs nothing on success, so a real p50/p95 of the owner's use
    can't be mined; only the 99 timeouts could. A one-line INFO per
    `/end` (or a small JSONL like `logs/memory.jsonl`) would make the next
    audit data-driven.

## Measurements taken

Taken at 2026-11-06 21:51, load 2.2 → 3.4. Tool venv: faster-whisper 1.2.1,
int8, 8 cores. Clip: a piper `en_US-lessac-medium` rendering of "fix the
failing test in the speech stream, over" with 0.2 s / 0.3 s of room, 3.3 s
in total.

| Model / config | Load | Pass times |
|---|---|---|
| `base.en`, 2 threads, greedy, word timestamps, **unpadded (30 s)** | 2.09 s | 0.65 / 0.61 / 0.60 s |
| `small`, 6 threads, beam 1, VAD, **30 s as shipped** | 1.21 s | 1.13 / 0.32 / 1.18 s |

- The server pads `base.en` to 10 s, so its real passes are the bench's
  ~155 to 250 ms; this row only shows why the pad matters.
- The 0.32 s outlier was not investigated.
- Both texts were correct; `base.en` heard "and the speech stream", `small`
  heard "in".
- No bench was run, as requested.
