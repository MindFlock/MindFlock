# Faster speech-to-text for MindFlock: research (2026-11-06)

Scope: web research plus reading the voice code. No product code changed.
Model speed on *this* laptop is measured by the sibling benchmark worker; every
speed below is a **published** number from the cited source, not ours. Claims
marked **UNVERIFIED** rest on one anecdote, a vendor's marketing, or my
inference.

## Executive summary

**Where the time goes today.** The owner's voice settings are `voice.stt = "server"` and
`voice.stt_backend = "local"`. Both the phone and the desktop stream 16 kHz WAV chunks to
faster-whisper on the WSL CPU. Reading `speech_stream.py`, `speech_turn.py` and the clients'
chunk constants:

- **First word on screen.** The first chunk is 150 ms of speech, then a `base.en` pass of
  about 0.47 s, so a grey partial shows about 0.7 s after the owner starts. A committed word
  needs two passes that agree on it (LocalAgreement-2), which takes about another 0.5–0.8 s.
- **After the owner stops.** The accurate `small` pass starts after 0.5 s of quiet and takes
  about 1.2 s, so the final cannot land before **about 1.7 s after the last word**. That holds
  even after "over", whose turn hold is only 350 ms. For plain speech the 1.1–1.5 s turn hold
  covers part of that wait.
- **What this means.** The accurate final pass is the biggest single chunk of waiting, and the
  rolling-window design of Whisper is the second.

**Why Siri feels fast.** Three things, and only the last is a UX trick:
- **A true streaming model.** It processes each bit of audio once, keeps its state, and emits
  text every few tens of ms. Whisper instead re-decodes an 8 s window on every pass.
- **Dedicated hardware.** It runs on the Neural Engine, which is idle otherwise, so it never
  fights other work for the CPU.
- **Showing rough text immediately.** Apple's SpeechAnalyzer emits "volatile" text at once,
  then replaces it in place with a final result.

MindFlock already does the UX tricks:
- it shows grey partials as they arrive;
- it starts the accurate pass early, before the turn hold ends;
- it adapts the turn hold to the words.

What it lacks is the architecture. Ranked by user-visible impact divided by effort:

1. **Run the final pass on NVIDIA Parakeet instead of faster-whisper `small`.** Both client
   surfaces benefit, desktop and phone, because the server does the work.
   - **Where it plugs in.** It is a drop-in `LocalSTT.final_transcriber` callable (it takes
     `audio` and returns text). Use Parakeet TDT 0.6B v2 int8 via `sherpa-onnx` (or `onnx-asr`).
   - **Speed.** Published CPU throughput is **30–36× real time** on desktop x86, against
     about 7.6× for faster-whisper `small`. A 5 s phrase should take about 0.15–0.25 s
     instead of about 1.2 s, which moves the final after "over" from about 1.7 s to about
     0.7 s. That figure is estimated from published numbers; the benchmark worker should
     confirm it.
   - **Accuracy.** Better: Open ASR Leaderboard average 6.05% vs about 8.6% for `small.en`.
   - **Risk: hotwords.**
     - Whisper's `hotwords=` biasing does not carry over.
     - `sherpa-onnx` hotwords need `modified_beam_search` and are unproven on NeMo models.
     - Mitigation: keep the wake check (`wake_transcribe`) on `base.en`, and snap session
       names after the fact, the way the voice router already matches titles.
   - **Effort:** small.
2. **Shorter turn holds driven by audio, not just words.** Both surfaces benefit, phone and
   desktop.
   - **What.** Add Pipecat **Smart Turn v3**: 8 MB int8, reads the audio itself, **12.6 ms per
     check** on a c7a.2xlarge CPU, 94% English accuracy.
   - **How it runs.** It runs after 200 ms of quiet and its verdict feeds `speech_turn.Turn`
     (`p` / `hold_ms`). Pipecat itself uses it to cut the 0.8 s silence rule to 0.2 s plus
     the check, so the owner would not need to say "over".
   - **Fit.** It slots into the existing `turn` field of each streaming chunk; the clients
     already honour `hold_ms`.
   - **Effort:** small–medium. Combined with #1, a finished sentence would land its final in
     about 0.5 s instead of about 2 s.
3. **A true streaming model for the live words.** Both surfaces benefit.
   - **What.** NVIDIA **Nemotron Speech Streaming EN 0.6B** (March 2026): cache-aware
     streaming with 80/160/560/1120 ms chunks. Average WER is 7.67% at 160 ms chunks and
     7.07% at 560 ms; it has punctuation and is packaged as int8 by `sherpa-onnx`.
   - **Speed.** Words arrive once per chunk and are never re-decoded, so a word appears
     about 160–560 ms after it is said with no two-pass agreement. Published CPU speed:
     **RTFx 7.25** at 0.56 s chunks (int8, EPYC); `sherpa-onnx` 1-thread RTF 0.16.
   - **Fit.** It needs a second `Stream` implementation (state plus feed-a-chunk), because
     `speech_stream.Stream` is built around re-transcribing a window. The HTTP chunk protocol
     (`committed` / `partial` / `turn`) and both clients stay as they are.
   - **Effort:** medium.
4. **An opt-in cloud streaming backend** (new backend id, `streams = True`). Both surfaces
   benefit; it is the best answer when the laptop is busy or on battery.
   - **Speed.** Independent end-of-speech-to-final medians (Pipecat stt-benchmark, 1,000 turns):
     - **Deepgram Nova-3: 247 ms**, 1.37% WER, $0.46/h, keyterms;
     - **Soniox stt-rt-v6: 266 ms**, 0.71% WER, $0.12/h, context terms;
     - **AssemblyAI Universal-Streaming: 256 ms**, 2.42% WER, $0.15/h, keyterms, semantic
       end of turn.
   - **Fit.** The server proxies a WebSocket per MindFlock stream, so the key never leaves
     the server and the client protocol is unchanged.
   - **Effort:** medium. Audio leaves the machine, so it must stay opt-in, like the
     `openai` backend.
5. **Platform engines.**
   - **Mac mini:** replace the `system` backend's `SFSpeechRecognizer` with a SpeechAnalyzer
     helper on macOS 26 (Swift-only; wrap something like the `yap` CLI, or a 100-line Swift
     tool).
     - Accuracy: LibriSpeech-clean WER **2.12% vs 9.02%** for the old engine.
     - Effort: small, Mac only.
   - **Windows / Intel NPU:** a warm Windows-native OpenVINO helper, following the
     `WarmPowerShell` pattern. The NPU is invisible inside WSL2.
     - One published Arrow Lake run (an Arrow Lake desktop chip, not this laptop's 275HX)
       put whisper-large-v3-turbo at **1.64 s NPU vs 3.04 s CPU** for a 13.7 s clip.
     - Worth a spike for battery reasons; it is not the first speed lever.
     - Effort: high, gain uncertain.

**The phone question, answered.** `webkitSpeechRecognition` in iPhone Safari does **not** give
us Siri's new engine.
- **The engine.** WebKit's source (`WebSpeechRecognizerTask.mm`, main as of 2026-11) drives
  the **old `SFSpeechRecognizer`**: on-device when the phone supports it, dictation hint, no
  punctuation flag, no contextual strings, a one-hour session cap. It is not SpeechAnalyzer.
- **Accuracy.** That engine measured **9.02% WER on LibriSpeech clean, against 5.42% for
  whisper base**.
- **Where it works.** Only in a Safari tab, never as a Home Screen web app (WebKit 225298,
  still closed LATER). iOS 26 opens Home Screen sites as web apps by default.
- **Biasing.** It takes no vocabulary of its own.
- **Already built.** MindFlock ships this path as `PhoneSTT`. Settings → Voice → recognition
  "on the phone" sets `voice.stt = "phone"`. Trying it costs the owner one toggle and is the
  quickest way to feel on-device streaming latency, but expect worse accuracy on session
  names and radio words.
- **Not recommended as the default.**

**Not worth it:**
- **WinRT `Windows.Media.SpeechRecognition` dictation.** It is the online engine, it listens
  only to the microphone (it cannot be fed the WAV the app already has), and it needs package
  identity.
- **Win+H / Fluid Dictation.** There is no API.
- **The new Windows AI speech API.** It is experimental, needs MSIX, and this laptop is not
  Copilot+ (the NPU is 13 TOPS against the 40 TOPS bar). Revisit when it leaves experimental.
- **Azure.** About 1 s to the final, $1/h.
- **Groq Whisper.** It does not stream.
- **Voxtral Realtime and Canary-Qwen.** They need a GPU.

## Comparison table

How to read it:
- **"Final after stop"** is the time from the end of speech to the final text: the Pipecat
  stt-benchmark median for cloud services, or a published CPU speed for local ones.
- **WER** is the Open ASR Leaderboard average for local models, or Pipecat's pooled WER on
  conversational turns for cloud services. The two are not comparable across those groups.

| Option | Where | Live words | Final after stop | English WER | Vocabulary biasing | Cost / license | Fit with `Backend` | Effort |
|---|---|---|---|---|---|---|---|---|
| **Today:** faster-whisper `base.en` stream + `small` final | server CPU | rolling window, ~0.47 s/pass, commit after 2 agreeing passes | ≥ ~1.7 s (0.5 s quiet + ~1.2 s pass) | small.en ~8.6 | `hotwords` on the final pass | MIT | is `local` | — |
| Parakeet TDT 0.6B v2 (final pass) | server CPU | — | est. 0.15–0.25 s pass for 5 s audio (30–36× real time) | **6.05** | sherpa hotwords UNVERIFIED on NeMo; snap names after recognition | CC-BY-4.0 | `final_transcriber` | **S** |
| Parakeet Unified EN 0.6B (final pass) | server CPU | (streaming only in NeMo) | RTF 0.032 @ 2 threads (sherpa) | 5.91 | as above | NVIDIA Open Model | `final_transcriber` | S |
| Nemotron Speech Streaming EN 0.6B | server CPU | true streaming, 160/560 ms chunks | RTFx 7.25 @ 0.56 s (int8, EPYC) | 7.67 @160 ms / 7.07 @560 ms | sherpa `hotwords` field, untested | NVIDIA Open Model | new `Stream` kind | M |
| Moonshine v2 Streaming small | server CPU | streaming, 148 ms latency (Apple M3) | — | 7.84 | none | MIT | new `Stream` kind | M |
| parakeet_realtime_eou_120m | server CPU | streaming 80–160 ms | EOU token p50 160 ms | 9.30, no punctuation | none | NVIDIA Open Model | turn detector | M |
| Smart Turn v3 (turn detector) | server CPU | — | cuts hold from ~1.1 s to ~0.2 s + 12–95 ms check | n/a | n/a | BSD-2 (UNVERIFIED) | feeds `speech_turn` | S–M |
| Deepgram Nova-3 | cloud | sub-300 ms partials | **247 ms** (p95 298) | 1.37 | keyterms ≤ 500 tokens, updatable mid-stream | $0.46/h | new streaming backend | M |
| Soniox stt-rt-v6 | cloud | ~50 ms first token (AA, older model) | **266 ms** (p95 324) | **0.71** | `context` text + terms | **$0.12/h** | new streaming backend | M |
| AssemblyAI Universal-Streaming EN | cloud | word P50 317 ms (vendor) | **256 ms** (p95 362) | 2.42 | keyterms (100) | $0.15/h; idle socket billed | new streaming backend | M |
| ElevenLabs Scribe v2 Realtime | cloud | ~150 ms (vendor) | 281 ms | 3.13 | keyterms (+$0.08/h) | $0.39/h | new streaming backend | M |
| Cartesia Ink-2 | cloud | 70 ms (AA) | 299 ms | 1.02 | yes | ~$0.24–0.39/h | new streaming backend | M |
| OpenAI gpt-4o-transcribe (realtime) | cloud | — | 637 ms | 2.95 | prompt | ~$1/h | new streaming backend | M |
| Azure Speech real-time | cloud | — | 1016 ms | 1.01 | phrase list ≤ 500 | $1.00/h | new streaming backend | M |
| iOS Safari `webkitSpeechRecognition` | phone on-device | live interims, flaky | fast (no number published) | LS-clean 9.02 (old engine) | none | free | **exists** (`voice.stt = "phone"`) | 0 |
| macOS 26 SpeechAnalyzer | Mac mini | volatile then final | 34 min of audio in 45 s (`yap`) | LS-clean **2.12** | `contextualStrings` UNVERIFIED | free | `system` backend on mac | S |
| OpenVINO Whisper on the Intel NPU | Windows-native helper | — | turbo: 1.64 s NPU vs 3.04 s CPU for 13.7 s audio (Arrow Lake desktop) | as Whisper | Whisper prompt | free | warm helper like `WarmPowerShell` | H |
| WinRT dictation / Windows AI speech | Windows | hypotheses | not published | not published | constraints | needs MSIX; online / experimental | — | ✗ |

## Details

### 1. Why Siri and iOS dictation feel fast

The parts that come from the design:
- **On-device streaming on the Neural Engine.** Siri's speech recognition moved on-device in
  iOS 15 (A12+). (Source: MacRumors iOS 15 guide, 2021; I found no Apple primary for this.)
- **SpeechAnalyzer / SpeechTranscriber** (iOS / macOS 26, WWDC25 session 277, June 2025:
  <https://developer.apple.com/videos/play/wwdc2025/277/>).
  - Apple calls the new model faster than `SFSpeechRecognizer`, good for long-form and
    distant audio, with "low latency without sacrificing accuracy". **Apple publishes no
    latency number.**
  - Models are system assets (`AssetInventory`): they live outside the app's memory and are
    updated by the OS.
  - The API is **Swift only**: <https://developer.apple.com/documentation/speech/speechanalyzer>.
- **Accuracy.** Lyonesse/Inscribe benchmark, 2026-07-13, M2 Pro, forced on-device:
  <https://lyonesse.app/blog/apple-speech-api-benchmark.html>.

  | Engine | LibriSpeech clean WER | LibriSpeech other WER |
  |---|---|---|
  | SpeechAnalyzer | 2.12% | 4.56% |
  | Whisper small | 3.74% | 7.95% |
  | Whisper base | 5.42% | 12.51% |
  | Old `SFSpeechRecognizer` | 9.02% | 16.25% |

  SpeechAnalyzer used about a third of Whisper small's compute per second of audio.
- **Speed.** MacStories, about 2025-06-18:
  <https://www.macstories.net/stories/hands-on-how-apples-new-speech-apis-outpace-whisper-for-lightning-fast-transcription/>.
  The `yap` CLI transcribed 34 min of audio in 0:45, against 1:41 for Whisper large-v3-turbo
  (MacWhisper).

The parts that are UX:
- **Volatile then final.** Volatile results stream immediately and are shown dimmed, then are
  replaced in place by a finalized result for the same audio range. Apple's sample warns of
  duplicates if the volatile text is not cleared.
- **First word measured.** One developer measured about **0.3–0.5 s** to the first word on an
  iPhone 16e running iOS 26.5, warm
  (<https://dev.to/simple_memo/ios-26s-speechanalyzer-on-a-live-mic-the-5-things-the-docs-dont-tell-you-2ng5>,
  about 2026-09; **UNVERIFIED**, a single measurement).
- **Endpointing.** I found no Apple source on how Siri decides you have stopped.

What MindFlock can copy:
- **The architecture.** A streaming model (recommendation 3) and an idle accelerator (the NPU
  helper, recommendation 5) are the parts MindFlock lacks.
- **The UX.** Volatile-then-final display, an early final pass and an adaptive end of turn
  are already built: the desktop draws `partial` muted, and the phone types `onGuess`
  guesses and replaces them with `final`.
- **What is left to gain:**
  - A word should appear in a fraction of a second, without waiting for two passes to agree.
  - The final should land quickly after the owner stops.

  Both are model speed, not presentation.

### 2. The phone: iPhone Safari and Android Chrome

**iPhone Safari's `webkitSpeechRecognition`**
- **The engine.** It is the old engine, mostly on-device. WebKit main
  (`Source/WebCore/Modules/speech/cocoa/WebSpeechRecognizerTask.mm`, read 2026-11-06) does the
  following:
  - creates `SFSpeechRecognizer` with an `SFSpeechAudioBufferRecognitionRequest`;
  - calls `setRequiresOnDeviceRecognition:YES` **when** `supportsOnDeviceRecognition` is true;
  - sets the dictation task hint and `setDetectMultipleUtterances:YES`;
  - sets `maximumRecognitionDuration = 60*60` (one hour);
  - does **not** set `addsPunctuation` or `contextualStrings`;
  - has no SpeechAnalyzer reference.

  I verified this directly against the source.
- **Accuracy.** That engine is the 9.02% / 16.25% row above, worse than our `base.en`.
- **`continuous`.** Supported since Safari 17, per MDN browser-compat-data (2026-11-02).
  MindFlock's `PhoneSTT` sets it anyway and restarts per phrase.
- **Interim results.** These are flaky. One Apple forums report (Mar 2025) says recognition
  slows, finalizes early and appears to move to the server: <https://developer.apple.com/forums/thread/775699>
  (**UNVERIFIED**, no replies).
- **Home Screen web apps.** Still unsupported: WebKit bug 225298 is closed LATER, last
  activity 2025-03-01: <https://bugs.webkit.org/show_bug.cgi?id=225298>. Neither the Safari 26
  nor the Safari 27 notes change this.
  - iOS 26 opens every Home Screen site as a web app by default (<https://webkit.org/?p=16993>,
    June 2025).
  - So the phone page must be used in a Safari tab. MindFlock's Safari banner already tells
    the owner this.
- **Other limits:**
  - There is no `processLocally`, `phrases` or grammar support in WebKit's API, so nothing
    can carry session names.
  - Siri must be on (Safari 14.1 notes, via a secondary source).
  - Speech recognition dies when an `<audio>` element finishes. This was verified in our
    2026-10-22 notes; it is why the phone speaks with `speechSynthesis`.

**Android Chrome**
- **Google's servers only.** The on-device options (`processLocally`, `available()` /
  `install()`, Chrome 139+) and the vocabulary option (`phrases`, Chrome 142+, which works
  only on-device) are desktop-only. A Chrome engineer stated on blink-dev (2026-05-06):
  "Android &amp; ChromeOS currently do not support on-device Web Speech":
  <https://groups.google.com/a/chromium.org/g/blink-dev/c/P8P-x7AnC6I>.
- **Behaviour.** Per browser-compat-data, `continuous` has no effect there. It works in
  installed web apps, per our 2026-10-22 notes. I found no latency number.
- **Desktop Chrome note.** On-device Web Speech with `phrases` (Chrome 142+) is real, but
  Electron's Chromium has no Google speech key, and the owner's default Windows browser is
  Arc, which also lacks it. So it is not a path for the desktop app.

**Verdict.** The browser recognizer is a free, low-latency fallback that MindFlock already has.
It is not the engine that makes Siri impressive. The way to get Apple's new engine on the
phone is a native app or an App Intent / Shortcut wrapper around SpeechAnalyzer. That is a
large step (App Store or TestFlight); park it unless the phone becomes the main surface.
- **Possible hybrid (UNVERIFIED):** run `webkitSpeechRecognition` purely for instant grey text
  while the server stream produces the committed and final words. Two consumers of the iOS
  microphone at once (`getUserMedia` plus `SFSpeechRecognizer`) is not documented to work.
  Prototype only if #1–#3 still leave the phone feeling slow.

### 3. Desktop: Windows, the Intel NPU and the Mac mini

**WinRT `Windows.Media.SpeechRecognition`: no.**
- The dictation and web-search grammars run on a remote service and need the **Online speech
  recognition** privacy setting:
  <https://learn.microsoft.com/en-us/windows/apps/develop/input/speech-recognition> (updated 2026-09-21).
- **"Unpackaged apps cannot use these APIs"** (same page), which rules out PowerShell and loose
  exes.
- `SpeechRecognizer` has no audio-source input; it is microphone-only
  (<https://learn.microsoft.com/en-us/uwp/api/windows.media.speechrecognition.speechrecognizer>,
  2025-11-21). It would fight the Electron renderer for the mic.

**Windows AI APIs speech recognition: watch it.**
- **What it is.** On-device, Windows 11 24H2+, Windows App SDK 2.2.2-experimental9+ (June
  2026). It is **experimental and "not supported for production"**, and needs an MSIX
  package declaring `systemAIModels`:
  <https://learn.microsoft.com/en-us/windows/ai/apis/speech-recognition> (2026-10-08).
- **Inputs and outputs.**
  - It accepts `AudioConfiguration.FromStream(IInputStream)`, so the app could feed it the
    audio it already has.
  - Its `StreamingRecognition` raises `Recognizing` (partial) and `Recognized` (final) events.
  - Preview is English-only, with no performance numbers (heise, 2026-06-04).
- **On this laptop.** It runs on the NPU only on Copilot+ PCs (40+ TOPS). The 275HX's NPU 3 is
  13 TOPS (TechPowerUp spec page), so here it would run on the CPU.

**Win+H voice typing / Fluid Dictation.** There is no developer API. Azure "embedded speech"
(the same technology as Voice Typing and Live Captions) is limited-access, for customers
managed directly by Microsoft.

**Intel NPU via OpenVINO (Windows-native only)**
- **No NPU in WSL2.** <https://github.com/microsoft/WSL/issues/40842> is open from 2026-06-18,
  and there is no Linux user-mode NPU driver. Inside WSL, OpenVINO 2026.4.1 sees only CPU and
  GPU (the iGPU, via `/dev/dxg`).
- **Driver.** Intel's Windows NPU driver lists the 275HX.
- **Published numbers** (single community runs, **treat as indicative**):

  | Model and hardware | Clip | NPU | CPU | iGPU | Source |
  |---|---|---|---|---|---|
  | whisper-large-v3-turbo int8, Core Ultra 7 265K (Arrow Lake), OpenVINO GenAI 2026.4.1 | 13.7 s | 1.64 s | 3.04 s | 7.8–10 s | <https://github.com/cjpais/Handy/pull/2361> (2026-11-01, closed unmerged) |
  | whisper-small int8, Lunar Lake (48 TOPS NPU) | not stated | 0.29 s | 1.74 s OpenVINO; 3.40 s faster-whisper | — | <https://github.com/zhuxixi/voice-input/pull/22> (2026-09-19) |
  | Parakeet TDT 0.6B v2, OpenVINO, Core Ultra 7 155H NPU | — | ~37.8× real time, 2.87% WER | 5–8× real time | — | <https://huggingface.co/FluidInference/parakeet-tdt-0.6b-v2-ov> (undated) |

  - **Compile cost.** The first NPU compile is slow (about 49 s for small, about 2 min for
    turbo) and then cached.
  - **Parakeet caveat.** INT8 Parakeet failed on one user's NPU; FP32 worked
    (<https://dev.to/cibernox/running-asr-for-smart-homes-in-the-npu-of-intel-processors-2iec>).
- **Shape.**
  - **What.** A warm Windows-side helper process (Windows Python with `openvino-genai`, or a
    C# exe) talked to over stdin, like `WarmPowerShell`.
  - **Input.** It is fed the WAV the renderer already captured.
  - **Install.** Installing a Windows-side Python stack is new territory for `speech_install`.
- **Why it ranks last.** The NPU's real value here is **battery and a free CPU** (the owner
  keeps the GPU off for battery), not raw speed: Parakeet on the CPU is already about 0.2 s
  per phrase. Arrow Lake's 13 TOPS NPU is roughly a third of Lunar Lake's (**UNVERIFIED** for
  speech workloads). Do this only after #1 and #3, and only if CPU contention under worker
  load (the server stopped answering under 5 workers + a full pytest on 2026-11-04) is felt.

**Mac mini: SpeechAnalyzer.**
- **What it fixes.** The `system` backend on macOS uses `SFSpeechRecognizer` through PyObjC,
  which is the 9% WER engine. On macOS 26, SpeechAnalyzer gets 2.1%.
- **How.** The API is Swift only, so PyObjC cannot call it. Options:
  - ship a tiny Swift CLI, or
  - depend on `yap` (finnvoor/yap, v1.2.1 2026-07-20, `brew install yap`; `transcribe` for
    files, `dictate` for the live mic; MIT, **UNVERIFIED**).

  Either one is spawned like the current `_mac_main` helper.
- **Vocabulary.** iOS / macOS 27 adds custom vocabulary (`DictationTranscriber` with a custom
  language model). Whether 26's `AnalysisContext.contextualStrings` reaches the transcriber is
  **UNVERIFIED**.

### 4. Local state of the art on a CPU

Full table with all sources is in the comparison table above. These notes cover what matters
for MindFlock.

**Streaming beats rolling-window re-decoding.** Microsoft (arXiv 2604.14493, 2026-04-19,
<https://arxiv.org/html/2604.14493v2>) measured:

| Approach | Delay | WER |
|---|---|---|
| Offline Parakeet v3, chunked | 2.4 s | 12.83% |
| Nemotron native cache-aware streaming | 0.56 s | 7.28% |
| Nemotron, ONNX int8 on an EPYC 7V12 | 0.56 s | 8.01% (RTFx 7.25) |

Our LocalAgreement-2 layer exists only because Whisper cannot stream.

**Nemotron Speech Streaming EN 0.6B** (<https://huggingface.co/nvidia/nemotron-speech-streaming-en-0.6b>,
released 2026-03-13; card read 2026-11-06):

| Chunk size | Average WER (Open ASR sets, no punctuation) |
|---|---|
| 1.12 s | 6.93% |
| 0.56 s | 7.07% |
| 0.16 s | 7.67% |
| 0.08 s | 8.43% |

- Punctuation and capitalization are built in.
- License: NVIDIA Open Model License (commercial use allowed).
- The card mentions no endpointing and no biasing.
- Runtimes: NeMo, Transformers ≥ 5.13, NeMo-Speech.cpp (GGUF), and a `sherpa-onnx` int8 build
  (<https://k2-fsa.github.io/sherpa/onnx/nemo/nemotron-streaming.html>, RTF 0.16 at 1 thread,
  560 ms chunks).

**Parakeet TDT 0.6B v2** (<https://huggingface.co/nvidia/parakeet-tdt-0.6b-v2>, 2025-05-01):
- Open ASR Leaderboard average 6.05%; LibriSpeech 1.69 / 3.19. CC-BY-4.0. Offline only.
- **CPU speed:**
  - RTFx 36 with `onnx-asr` on a Ryzen 9800X3D (<https://github.com/istupakov/onnx-asr>);
  - RTFx 32 f32 with parakeet.cpp at 8 threads on x86, and q8 1.55× faster
    (<https://localai.io/blog/parakeet-cpp-asr-on-cpu/>, 2026-06-05);
  - faster-whisper `small` is about 7.6× real time on an i7-12700K
    (<https://github.com/SYSTRAN/faster-whisper>).
- **Other variants:** v3 (25 languages) scores 6.32. Parakeet Unified EN 0.6B (2026-04-07)
  scores 5.91 offline under the NVIDIA license; `sherpa-onnx` runs it offline only.
- **Who uses it.** Dictation apps (Handy, Spokenly, VoiceInk, OpenWhispr) pick Parakeet for
  speed on the CPU (<https://handy.computer/docs/models>,
  <https://openwhispr.com/blog/parakeet-vs-whisper-vs-nemotron> 2026-07-18). OpenWhispr's
  pairing is Nemotron for streaming plus Parakeet for the final, which matches
  recommendations 1 and 3.

**Hotwords are the open risk.**
- **`sherpa-onnx`.** Its hotwords (contextual biasing, a file of phrases with optional scores)
  work only with transducer models under `modified_beam_search`; greedy search ignores them
  (<https://k2-fsa.github.io/sherpa/onnx/hotwords/index.html>).
  - Its NeMo model pages never claim hotwords work.
  - Its 1.13.1 changelog fixes "NeMo transducer modified beam search" and adds per-stream
    hotwords to the JS API.
  - **UNVERIFIED** for Parakeet and Nemotron. The benchmark worker should test it with our
    `recognition_hints`.
- **NeMo GPU-PB / TurboBias** supports per-utterance phrase boosting for TDT/RNN-T
  (<https://docs.nvidia.com/nemo/speech/nightly/asr/asr_customization/word_boosting.html>),
  but needs the full NeMo + PyTorch stack. That is too heavy for the server.
- **Fallback that needs no model support.** Fuzzy-snap recognized words to the known vocabulary
  after decoding: session titles via `speech_form`, the wake forms in
  `voice_words.HEARD`, the radio words.
- **Why the risk is smaller than it looks:**
  - "over" and "disregard" are ordinary English words, which Parakeet transcribes well.
  - The wake word goes through `wake_transcribe`, which can stay on `base.en` with its hint.
    It is a separate route.

**Other models.**
- **Moonshine v2 Streaming** (arXiv 2602.12241, 2026-02-12; MIT; `pip install
  moonshine-voice`):
  - Open ASR averages: tiny 12.01, small 7.84, medium 6.65.
  - Latency on an Apple M3 CPU: 50 / 148 / 258 ms, against **553 ms for faster-whisper base
    on the same machine**.
  - No biasing. It is the light streaming option if Nemotron is too heavy.
- **Kyutai STT** (stt-1b-en_fr, 0.5 s delay, with a semantic VAD in its Rust server; stt-2.6b-en,
  2.5 s delay): no CPU numbers published, so it is GPU-oriented.
- **Not for this CPU:**
  - **Whisper large-v3-turbo / distil-large-v3.5:** slower and less accurate than Parakeet on
    a CPU.
  - **Voxtral Realtime (4B):** needs a 16 GB GPU.
  - **Canary-Qwen 2.5B:** GPU.
  - **Granite 4.0 1B Speech:** scores 5.52 but no CPU numbers are published.
- **sherpa-onnx's English streaming Zipformers** are 2023 models trained on LibriSpeech only.
  Nemotron supersedes them.
- **Memory.** int8 Parakeet 0.6B and Nemotron 0.6B are each roughly 0.6–0.7 GB on disk
  (**UNVERIFIED**; read off sherpa-onnx release assets as I remember them). Run both resident
  and the memory guard (`backend.memory_pressure`) must budget for about 1.5 GB, against about
  0.5 GB today. That matters given the 2026-10-14 OOM.

### 5. Cloud streaming, as an opt-in key

**The best independent comparison** is the Pipecat stt-benchmark
(<https://github.com/pipecat-ai/stt-benchmark>, read 2026-11-06; 1,000 real conversational
turns played in real time through Silero VAD).
- **What it times.** "TTFS" is the time from the estimated end of speech (the VAD stop
  minus its `stop_secs`) to the final segment.
- **Its own caveat.** WER differences under about 0.25 points among the leaders are inside the
  confidence interval.

| Vendor / model | TTFS median | TTFS p95 | Pooled WER |
|---|---|---|---|
| Deepgram nova-3 | 247 ms | 298 ms | 1.37% |
| AssemblyAI universal-streaming-english | 256 ms | 362 ms | 2.42% |
| Soniox stt-rt-v6 | 266 ms | 324 ms | 0.71% |
| ElevenLabs scribe_v2_realtime | 281 ms | 348 ms | 3.13% |
| Cartesia ink-2 | 299 ms | 328 ms | 1.02% |
| Speechmatics linden-1 | 369 ms | 438 ms | 0.96% |
| Google gemini-3.5-transcribe-live | 458 ms | 532 ms | 1.97% |
| OpenAI gpt-4o-transcribe | 637 ms | 965 ms | 2.95% |
| OpenAI gpt-realtime-whisper | 740 ms | 878 ms | 2.30% |
| Azure | 1016 ms | 1345 ms | 1.01% |

**The other benchmarks disagree.**
- Artificial Analysis AA-WER Streaming (2026-06-01,
  <https://artificialanalysis.ai/articles/new-streaming-speech-to-text-benchmark-aa-wer-streaming>)
  times from the moment Silero decides speech ended. It puts Soniox, on an older model, at
  **11.9%** WER (worst there).
- Gradium's write-up of a Coval run (2026-09-10) puts Nova-3 at 25.3% WER and Scribe's final
  at 2,080 ms. Gradium is itself a vendor.
- **Treat every cloud number as approximate until measured on the owner's voice.**

**Prices (read 2026-11-06), per hour of streaming:**
- Soniox **$0.12** (<https://soniox.com/pricing>);
- AssemblyAI Universal-Streaming **$0.15**, with keyterms +$0.04, **billed on open-socket
  time including idle** (<https://www.assemblyai.com/pricing.md>);
- Deepgram Nova-3 / Flux $0.46 pay-as-you-go, with keyterms +$0.078
  (<https://deepgram.com/pricing>);
- ElevenLabs $0.39;
- Speechmatics Linden-1 $0.30;
- Azure $1.00 (Azure retail prices API);
- OpenAI gpt-realtime-whisper / gpt-live-transcribe $1.02.

**Vocabulary biasing:**
- **Deepgram:** keyterms ≤ 500 tokens (20–50 terms advised), updatable mid-stream.
- **AssemblyAI:** 100 keyterms.
- **Soniox:** `context` with free text plus `terms`.
- **ElevenLabs:** keyterms, paid.
- **Azure:** phrase list ≤ 500.
- **OpenAI:** `prompt` / `keywords`.

All of these could carry `recognition_hints`.

**End of turn built in:**
- **Deepgram Flux** (`eot_threshold`, an eager end-of-turn, about 260 ms; vendor claim).
- **AssemblyAI** (semantic; minimum 400 / maximum 1280 ms, aggressive preset 160 / 400 ms; a
  `ForceEndpoint` message).
- **Soniox** (`max_endpoint_delay_ms` 500–3000).
- **Speechmatics** (Linden-1 only).
- **OpenAI.** The gpt-4o models have `semantic_vad`. The newest `gpt-live-transcribe` has no
  server VAD at all: the client commits each turn.

**Forcing the final.** A radio word maps onto an explicit finalize message:
- verified: AssemblyAI `ForceEndpoint`, ElevenLabs `commit()`, OpenAI `commit`;
- **UNVERIFIED:** Deepgram `Finalize`, Soniox `finalize`.

**Browser-direct.** Every streaming vendor above issues short-lived tokens (Deepgram
`/v1/auth/grant`, AssemblyAI `/v3/token`, Soniox temporary keys, ElevenLabs single-use, OpenAI
ephemeral secrets). So a later phase could let the phone or desktop stream straight to the
vendor, saving the hop through the WSL server. Phase 1 should proxy through the server instead:
- the `/api/voice/stream*` protocol and both clients stay unchanged;
- the key stays in `config.toml`;
- the extra hop on a tailnet costs tens of ms.

**Fit and recommendation.**
- **Fit.** A `CloudStreamSTT(Backend)` with `streams = True`.
  - `stream_transcriber` opens the vendor WebSocket at stream open.
  - Each chunk forwards PCM and returns the vendor's newly-final words as `committed` and its
    interim words as `partial`.
  - `/end` sends the vendor's finalize message.
  - `final_transcriber` returns `None` (the vendor's final is the final).
- **Recommendation:**
  - **Soniox** for price and accuracy;
  - **Deepgram Nova-3** as the safe, well-documented choice;
  - **AssemblyAI** for its semantic end of turn, if the owner wants to drop "over" entirely.

### 6. Endpointing: ending the turn without saying "over"

- **Today.** `speech_turn.py` reads only the words. The hold is 350 ms after a radio word or
  command, 900 ms after a sentence or question, 1100 ms by default and 1500 ms when trailing.
  Clients act on `hold_ms` (clamped to 0.3–3 s).
- **Pipecat Smart Turn v3** (2025-09-11,
  <https://www.daily.co/blog/announcing-smart-turn-v3-with-cpu-inference-in-just-12ms/>;
  current v3.2, <https://github.com/pipecat-ai/smart-turn>):
  - 8M params, 8 MB int8 ONNX, reads raw audio (up to 8 s), 23 languages.
  - **12.6 ms** CPU on a c7a.2xlarge, 94.8 ms on a t3.medium; 94.31% English accuracy.
  - Pipecat runs it after Silero VAD with `stop_secs = 0.2`, against 0.8 s without it, and
    falls back to ending the turn after 3 s of quiet
    (<https://docs.pipecat.ai/server/utilities/smart-turn>). That is about **600 ms saved** on
    complete sentences.
  - **Fit.** Run it in `Stream.feed` once 200 ms of quiet follow the last loud frame, over the
    phrase's last ≤ 8 s (the stream already keeps the phrase audio for the final pass). Blend
    its probability into `Turn.p` / `hold_ms`: a confident "done" plus a sentence ending gives
    a short hold; "not done" keeps the long one. The word rules stay as the tie-breaker. This
    attacks the 1.1 s default directly.
- **LiveKit turn detector v1** (2026-06-17, <https://livekit.com/blog/solving-end-of-turn-detection>;
  LiveKit's own benchmark):
  - false cutoffs 9.9% at a 300 ms budget (Deepgram Flux 12.9%) and 4.5% at 600 ms;
  - v1-mini is open-weight and meant for the CPU;
  - the older text-only model is Qwen2.5-0.5B (396 MB, 50–160 ms).

  It is heavier than Smart Turn, and our word rules already do the text side.
- **Silero VAD v6:** under 1 ms per 30 ms chunk (project claim); 220–240 µs measured by Cekura.
  **TEN VAD** claims a lower RTF and that Silero lags at speech end (vendor claim). Our RMS
  gate does the same job today. A real VAD would mainly help in noisy rooms.
- **parakeet_realtime_eou_120m:** an end-of-utterance token at p50 160 ms, p90 280 ms (model
  card). It pairs naturally with the Nemotron or Parakeet family if #3 lands.
- **Kyutai** semantic VAD and **Deepgram Flux** eager end-of-turn do the same model-native
  thing. Telnyx measured Flux's eager end-of-turn at about 150 ms saved at p50 and about
  350 ms at p95 (Jan 2026), at the cost of more speculative work downstream.
- **The cheap win.** Smart Turn (or the EOU model) plus the existing word rules lets an
  ordinary finished sentence end in about 0.2–0.4 s of quiet instead of 0.9–1.1 s. Radio words
  remain the explicit "send now".

## Suggested order and what to measure

1. **Parakeet final pass.** Ask the benchmark worker for:
   - final-pass ms on 1–10 s phrases on this laptop (Parakeet v2 int8 vs `small`);
   - WER on our voice bench phrases, with and without sherpa hotwords;
   - resident RSS.
2. **Smart Turn in `speech_turn`.** Re-run `tests/bench/turn_bench.py` (the 27 "pause before
   over" phrases) for false cutoffs at 200 / 400 ms holds.
3. **Nemotron streaming `Stream`.** Measure first-word latency from speech onset and the
   CPU share at 160 vs 560 ms chunks while sessions are running.
4. **Cloud backend.** Soniox first ($0.12/h); same metrics, from both the phone and the
   desktop.
5. **Mac SpeechAnalyzer `system` engine.** Then the NPU spike, only if CPU contention shows up.

Phone: no product change is needed to try on-device. A one-toggle A/B (recognition "on the
phone", in a Safari tab) tells the owner how much of the "slow" feel is the server path. Expect
worse names.
