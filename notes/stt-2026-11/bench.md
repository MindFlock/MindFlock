# STT candidates bench — 2026-11-06

**Question:** today's recognition is "ok but feels slower than I want". What would
feel as fast and accurate as Siri, on **this** machine (WSL2, 8 cores of an Intel
Core Ultra 9 275HX, 20 GB cap, CPU only)?

**Answer in one line:** the slowness is the ~1 s *final* after you stop, not the
live words. A true streaming model (**Moonshine tiny-streaming + keyterms**)
delivers final text **~60 ms after the speech ends (p95 ~130 ms) instead of
~1.0 s (p95 1.5 s)**, with the same control-word hit rate as today's final and
an eighth of the memory, but it is weaker on long real speech. For accuracy,
**Parakeet TDT 0.6B v2** makes 3× fewer errors than whisper `small` on real
speech in half the time, but it has no vocabulary biasing unless you run it
through sherpa-onnx's beam search, which gives back most of the speed.

Bench: `tests/bench/stt_candidates_bench.py` (how to run it is at the bottom).
Raw per-utterance JSON: `~/stt-bench-models/results/` (outside the repo).

## Today's pipeline, measured the same day

`prod-stream` is the real `speech_stream.Stream` (base.en fast tier at 2 threads,
`small` final + production hotwords at 6 threads) driven by
`voice_bench.run_config` over the same 48 piper phrases:

| metric | value |
|---|---|
| first word (from clip start, incl. 0.3–0.5 s of lead room) p50 / p90 | 1220 / 1538 ms |
| **finish** (`Stream.finish()`, what the client waits for after "over") p50 / p95 | **1022 / 1500 ms** |
| accurate pass inside it p50 / p95 | 998 / 1083 ms |
| fast pass p50 / p95 | 195 / 342 ms |
| ctl fast / ctl final | 36/48 / 44/48 |
| WER fast / WER final | 18.5% / 8.7% |
| peak RSS (both models) | 1765 MB |

## Results: piper command set (48 phrases, voice_bench's list and seed)

Sorted by one-shot p50. **ctl** = the control word heard where it belongs
(voice_bench's judging); **ctl+join** = the same after joining the first two
words when they spell the wake word ("Mind flock" → "mindflock", a one-line
server-side fix). **CER** = letters only, so "work tree" = "worktree".
Streaming columns are for TRUE streaming models fed 160 ms chunks at real-time
pace; **final after end** = ms from the clip's end to the final text, backlog
included.

| candidate | n | WER | CER | ctl | ctl+join | one-shot p50/p95 ms | RTF | cores | load s | RSS MB | stream WER | stream ctl | first word p50/p90 ms | final after end p50/p95 ms |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| moonshine-tiny | 48 | 23.2% | 7.2% | 37/48 | 38/48 | 118 / 298 | 0.045 | 6.8 | 0.55 | 211 | — | — | — | — |
| parakeet-eou-120m-offline ¹ | 48 | 37.0% | 23.3% | 29/48 | 29/48 | 154 / 221 | 0.054 | 7.0 | 1.1 | 460 | — | — | — | — |
| **moonshine-tiny-streaming+hints** | 48 | 9.8% | 3.0% | 44/48 | 45/48 | 170 / 439 | 0.068 | 7.1 | 0.58 | 221 | **9.1%** | **44/48** | 1342 / 1401 | **56 / 132** |
| moonshine-base | 48 | 18.8% | 5.5% | 37/48 | 41/48 | 190 / 375 | 0.071 | 6.9 | 1.17 | 478 | — | — | — | — |
| moonshine-tiny-streaming | 48 | 12.3% | 3.9% | 44/48 | 45/48 | 232 / 427 | 0.082 | 6.9 | 0.58 | 223 | 11.6% | 44/48 | 1361 / 1401 | 66 / 180 |
| sherpa-kroko-en | 48 | 17.0% | 8.1% | 32/48 | 32/48 | 274 / 541 | 0.101 | 7.1 | 2.58 | 214 | 17.0% | 32/48 | 1557 / 1622 | 45 / 160 |
| **parakeet-tdt-v2** (onnx-asr) | 48 | 11.2% | 1.6% | 40/48 | 44/48 | 347 / 585 | 0.137 | 7.0 | 6.15 | 1240 | — | — | — | — |
| parakeet-tdt-v3 (onnx-asr) | 48 | 11.6% | 2.2% | 41/48 | 42/48 | 367 / 602 | 0.132 | 6.9 | 5.81 | 1282 | — | — | — | — |
| fw-base.en (today's fast model, one-shot) | 48 | 12.0% | 2.0% | 39/48 | 43/48 | 417 / 498 | 0.146 | 3.8 | 1.2 | 407 | — | — | — | — |
| sherpa-zipformer-en (2023-06-26) | 48 | 24.3% | 7.9% | 38/48 | 42/48 | 474 / 912 | 0.188 | 7.0 | 2.38 | 200 | 24.3% | 38/48 | 1163 / 1196 | 93 / 166 |
| sherpa-parakeet-tdt-v2 (greedy) | 48 | 11.2% | 2.4% | 41/48 | 43/48 | 547 / 934 | 0.204 | 7.1 | 2.73 | 913 | — | — | — | — |
| moonshine-small-streaming | 48 | 11.6% | 3.0% | 44/48 | 45/48 | 674 / 1241 | 0.235 | 7.0 | 23.77 | 474 | 12.3% | 43/48 | 1564 / 1660 | 307 / 855 |
| **sherpa-parakeet-tdt-v2+hints1.0** | 48 | 6.5% | 1.5% | 43/48 | 44/48 | 682 / 1015 | 0.251 | 7.1 | 2.91 | 922 | — | — | — | — |
| **fw-small+hints (today's final)** | 48 | **4.7%** | 1.5% | **46/48** | 46/48 | 878 / 1029 | 0.505 | 3.4 | 2.54 | 1375 | — | — | — | — |
| moonshine-medium-streaming | 48 | 12.0% | 2.6% | 41/48 | 43/48 | 1196 / 2011 | 0.407 | 7.0 | 48.93 | 813 | 11.2% | 41/48 | 1774 / 1947 | 807 / 1551 |
| sherpa-fastconformer-80ms | 48 | 19.6% | 6.0% | 38/48 | 42/48 | 1767 / 3174 | 0.661 | 7.1 | 2.63 | 292 | 19.6% | 38/48 | 1198 / 1364 | 420 / 742 |
| sherpa-nemotron-3.5-160ms | 20 | 10.0% | 5.0% | 16/20 | 16/20 | 3433 / 7077 | **1.913** | 7.0 | 7.64 | 1048 | 10.0% | 16/20 | 1877 / 2446 | 2024 / 3360 |
| fw-large-v3-turbo+hints | 20 | 1.7% | 0.4% | 18/20 | 18/20 | 3665 / 4010 | 1.825 | 3.6 | 7.65 | 2169 | — | — | — | — |
| fw-distil-large-v3.5+hints | 20 | 8.3% | 3.1% | 17/20 | 17/20 | 3674 / 4244 | 1.925 | 3.7 | 7.0 | 2040 | — | — | — | — |

On the same first 20 phrases: fw-small+hints 5.0%, parakeet-tdt-v2 10.0%,
sherpa-parakeet+hints1.0 6.7%, moonshine-tiny-streaming(+hints) 11.7%,
large-v3-turbo 1.7%, distil-large-v3.5 8.3%, nemotron 10.0%.

## Results: real speech (73 LibriSpeech utterances, 539 s, mean 7.4 s)

`hf-internal-testing/librispeech_asr_dummy`, padded with 0.3–0.5 s of quiet on
either side like the piper clips. No control words, so no ctl columns.

| candidate | n | WER | CER | one-shot p50/p95 ms | RTF | cores | load s | RSS MB | stream WER | first word p50/p90 ms | final after end p50/p95 ms |
|---|---|---|---|---|---|---|---|---|---|---|---|
| parakeet-eou-120m-offline ¹ | 73 | 35.1% | 23.2% | 242 / 552 | 0.041 | 6.8 | 1.21 | 753 | — | — | — |
| moonshine-tiny | 73 | 12.9% | 6.4% | 250 / 719 | 0.045 | 6.9 | 0.66 | 289 | — | — | — |
| **moonshine-tiny-streaming** | 73 | 12.6% | 6.4% | 394 / 1138 | 0.068 | 6.9 | 0.78 | 315 | **13.0%** | 1326 / 1983 | **24 / 115** |
| moonshine-base | 73 | 9.0% | 4.4% | 411 / 1170 | 0.071 | 7.0 | 1.29 | 534 | — | — | — |
| fw-base.en | 73 | 10.2% | 4.9% | 478 / 659 | 0.069 | 3.5 | 1.21 | 465 | — | — | — |
| sherpa-kroko-en | 73 | 7.8% | 3.5% | 581 / 1547 | 0.093 | 7.1 | 3.02 | 245 | 7.8% | 2820 / 2871 | 21 / 159 |
| **parakeet-tdt-v2** (onnx-asr) | 73 | **2.8%** | 1.2% | **605 / 937** | 0.083 | 7.0 | 6.5 | 1748 | — | — | — |
| parakeet-tdt-v3 (onnx-asr) | 73 | 4.4% | 2.1% | 625 / 912 | 0.085 | 7.0 | 6.18 | 1743 | — | — | — |
| sherpa-parakeet-tdt-v2 (greedy) | 73 | 3.0% | 1.3% | 979 / 1415 | 0.13 | 7.0 | 3.65 | 1283 | — | — | — |
| sherpa-zipformer-en | 73 | 5.0% | 2.0% | 1062 / 2541 | 0.169 | 7.1 | 3.42 | 229 | 5.0% | 1787 / 1830 | 87 / 155 |
| sherpa-parakeet-tdt-v2+hints1.0 | 73 | 2.9% | 1.3% | 1133 / 1488 | 0.154 | 7.1 | 4.33 | 1280 | — | — | — |
| **fw-small+hints (today's final)** | 73 | 8.1% | 3.9% | 1206 / 1623 | 0.17 | 3.4 | 2.61 | 946 | — | — | — |
| moonshine-small-streaming | 73 | 6.3% | 3.1% | 1451 / 4140 | 0.248 | 7.0 | 2.53 | 579 | 6.4% | 1548 / 2262 | 292 / 1044 |
| fw-distil-large-v3.5+hints | 20 | 7.3% | 3.6% | 3534 / 4079 | 0.351 | 3.7 | 7.46 | 2054 | — | — | — |
| fw-large-v3-turbo+hints | 20 | 4.4% | 2.5% | 3978 / 4202 | 0.392 | 3.5 | 7.35 | 2190 | — | — | — |

On the same first 20 utterances: fw-small+hints 7.7%, parakeet-tdt-v2 3.1%,
sherpa-parakeet+hints1.0 2.9%, moonshine-tiny-streaming 12.8%,
large-v3-turbo 4.4%, distil-large-v3.5 7.3%.
moonshine-medium-streaming and sherpa-fastconformer-80ms hit the bench's 10-minute
per-config cap on this set (both are already out on the piper numbers).

¹ See "Parakeet realtime EOU" below: an offline lower bound, not the model's real quality.

## Vocabulary biasing per candidate

MindFlock depends on biasing for session names and the wake word (today:
faster-whisper `hotwords` on the final only).

| candidate | biasing | measured effect here |
|---|---|---|
| faster-whisper (all sizes) | `hotwords` / `initial_prompt` | production setting; keeps "mindflock", "worktree", "sitecheck bot 7" right |
| Moonshine *streaming* (tiny/small/medium) | `Transcriber.set_keyterms([...])`, strength via the `keyterm_boost` load option; can change mid-stream | tiny: WER 12.3% → 9.8% (stream 11.6% → 9.1%), same ctl, no hallucinated keyterms in silence |
| Moonshine tiny/base (non-streaming) | none (keyterms need a streaming arch) | — |
| Parakeet TDT v2/v3 via **onnx-asr** | none | hears "Mind flock", "Mike off", "poll request", "work tree" |
| Parakeet TDT via **sherpa-onnx** | hotwords (Aho-Corasick contextual biasing), needs `modified_beam_search` + a `bpe.vocab` (the bench writes one from `tokens.txt`) | score 1.0: WER 11.2% → 6.5%, ctl 41 → 43/48. **Score 2.0 hallucinates** ("Claude Claude Claude…" into "Stop.", WER 25.7%); 0.5: 8.3%. Beam search costs ~+130 ms on commands, ~+150 ms on 7 s utterances |
| sherpa streaming zipformer / kroko / FastConformer / Nemotron | hotwords with `modified_beam_search` (zipformer ships `bpe.model`; the NeMo ones need a vocab written from `tokens.txt` as above) | not measured: these models lost on speed or accuracy before biasing mattered |
| Parakeet realtime EOU 120M | none in any runtime found | — |

## Per-candidate notes

* **Moonshine streaming** (`moonshine-voice` 0.1.6, models dated 2026-11-02,
  check the English models' licence before shipping: the package warns only that
  non-English models are under the non-commercial Moonshine Community License). The tiny model's encoder state is cached, so a pass costs only the new
  audio and the final after the clip ends is 24–66 ms p50. First word ≈ today's
  (1.34 s vs 1.22 s from clip start; both include 0.3–0.5 s of lead room).
  Its ONNX runtime **spins all cores while decoding** (7 cores busy at RTF
  0.07). In production it needs a thread cap (`session.intra_op.allow_spinning`
  and the thread options are C-API strings; not tested at 2 threads). small and
  medium were slower and no better on commands.
* **Parakeet TDT 0.6B v2** (`onnx-asr` 0.12.0, int8, CC-BY-4.0). Best real-speech
  accuracy measured (2.8%; `small` 8.1%; even large-v3-turbo 4.4% on the same 20),
  at half `small`'s latency. Without biasing it misses compounds and the wake
  word on commands. The same model through sherpa-onnx is ~1.6× slower greedy
  (547 vs 347 ms, 979 vs 605 ms). v3 (multilingual) is a bit worse in English.
* **faster-whisper large-v3-turbo / distil-large-v3.5**: 3.5–4 s per phrase on
  this CPU. turbo is the most accurate model here on commands (1.7%), but not usable.
* **sherpa Nemotron 3.5 ASR streaming 0.6B (160 ms)**: RTF 1.9. It cannot keep up
  with real time on this CPU (final arrives 2.0 s after the end).
* **sherpa FastConformer 80 ms / zipformer 2023 / kroko 2025**: fast streaming
  finals (45–420 ms), but command WER 17–24% (kroko 32/48 ctl).
* **Parakeet realtime EOU 120M** ¹: neither onnx-asr nor sherpa-onnx ships it.
  The only ONNX was a community export (`ysdede/parakeet-realtime-eou-120m-v1-onnx`)
  with no cache inputs, so it can only run **offline** (whole clip, its own
  greedy RNNT driver in the bench, un-normalized 128 log-mels). In that mode,
  WER is 35–37% and it emitted `<EOU>` on ~36/48 piper and ~61/73 LibriSpeech
  clips (the counter includes 2 warm-up decodes). That says nothing about its
  real streaming quality. Replacing "over" with its EOU token needs NVIDIA's
  cache-aware streaming driver (NeMo, or a port), which is out of the ~15 min
  budget. Watch sherpa-onnx's asr-models releases for a packaged version.

## Recommendation

1. **Replace the fast tier with Moonshine tiny-streaming + keyterms, and let it
   finalize.** It is a true streaming decoder, so when the person says "over"
   the text is already done:
   **after you stop, final text in ~60 ms (p95 ~130 ms) instead of ~1.0 s
   (p95 1.5 s)**. On the command set it matches today's *final* (ctl 44/48 vs
   44/48 in the stream, WER 9.1% vs 8.7%) and beats today's *fast* tier (ctl
   36/48, WER 18.5%). Memory drops from 1.77 GB to ~0.22 GB. Keyterms carry the
   session names. It does not change time to the first word (≈1.3 s from clip
   start, as today).
2. **Final tier for dictation: Parakeet TDT 0.6B v2.** On real speech it is
   clearly better than `small` (2.8% vs 8.1% WER) and faster (605 vs 1206 ms
   p50). Run it only where accuracy matters (longer dictation, e.g. > ~6
   words), as a polish pass that replaces Moonshine's text when it lands, not
   as a gate on "over". Without biasing it needs the wake-word join
   (ctl 40 → 44/48) and a session-name fixer. With sherpa hotwords at score
   **1.0** (never 2.0) it reaches 6.5% / 43/48 on commands at 682 ms, about
   200 ms faster than `small` (878 ms), with real-speech accuracy kept at 2.9%.
3. **Not worth it:** large-v3-turbo and distil-large-v3.5 (3.5–4 s), Nemotron
   streaming (RTF 1.9), Moonshine medium, the 2023–2025 sherpa streaming
   models (accuracy), and non-streaming Moonshine on short commands
   ("Disregard." → "This regard.").

Cheap wins regardless of model: join "mind flock" → "mindflock" before the
wake check (+1 to +4 ctl hits for every unbiased model). "Stop." → "top" and
"Disregard that." fail for *every* model including `small`. The first is a
piper rendering of the clipped /s/, the second a voice_bench scoring quirk
(the reference keeps "that" after the clear word).

Next step before any switch: a Moonshine-tiny-streaming lane behind
`speech_stream` (a provider-style backend), benched with the owner's own
recorded phrases (piper is too clean, and it is one voice) and with its thread
count capped at 2–4 alongside the agents.

## Caveats

* **Load.** Every config ran alone in its own process. Before each one, `all`
  sampled `/proc/stat` and waited until other processes used < 3 cores
  (observed 0.2–1.7 busy cores at start). The 1-minute load average reads
  6–15 during runs, but that is our own thread pools: ORT with 6 intra-op
  threads plus spinning. Runs between 22:00 and 22:18, when a sibling's
  pre-commit hook ran the full pytest suite, were discarded and re-run.
* **Threads.** ORT and sherpa models got 6 intra-op threads (observed ~7 cores
  with spinning). faster-whisper ran through `LocalSTT.final_transcriber`
  itself at the production final budget (`whisper_threads` → 6, observed
  3.4–3.8 cores). Streaming candidates were not run at the fast tier's
  2-thread budget.
* **piper is one clean synthetic voice**, so command WERs are optimistic for
  every model. LibriSpeech is read speech by real people, clean. Neither is a
  laptop mic in a room.
* **Small n**: 48 / 73 utterances (20 for the large whispers and Nemotron).
  Differences of 1–2 ctl hits or ~1% WER are noise.
* `first word` counts from the clip's start, including 0.3–0.5 s of lead
  silence. `final after end` counts from the clip's end, after 0.3–0.5 s of
  trailing room, so a model that waits for silence gets that room for free
  (today's pipeline does too: the client sends a quiet chunk before `/end`).
* prod-stream's `finish` comes from voice_bench (150/300 ms chunks plus one
  300 ms quiet chunk). The candidates were fed 160 ms chunks. Both use a
  simulated clock with real decode times.
* Load time includes the warm-up decodes. First-download times are excluded
  (models were already cached).

## Reproduce

```bash
# scratch venv (never the server's uv tool venv)
uv venv --python 3.12 ~/stt-bench-venv
uv pip install --python ~/stt-bench-venv/bin/python 'faster-whisper==1.2.1' numpy \
    soundfile psutil pyarrow huggingface_hub 'onnx-asr[cpu,hub]' sherpa-onnx moonshine-voice
# versions used: faster-whisper 1.2.1 / ctranslate2 4.8.5, onnxruntime 1.31.1,
# onnx-asr 0.12.0, sherpa-onnx 2.0.1, moonshine-voice 0.1.6, numpy 2.5.4

# sherpa models → ~/stt-bench-models/sherpa/<name>/ (GitHub release "asr-models")
for n in sherpa-onnx-streaming-zipformer-en-2023-06-26 \
         sherpa-onnx-streaming-zipformer-en-kroko-2025-08-06 \
         sherpa-onnx-nemo-streaming-fast-conformer-transducer-en-80ms-int8 \
         sherpa-onnx-nemotron-3.5-asr-streaming-0.6b-160ms-int8-2026-06-11 \
         sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8; do
  curl -sSL https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/$n.tar.bz2 \
    | tar xj -C ~/stt-bench-models/sherpa
done
# EOU 120M community export → ~/stt-bench-models/eou/
for f in vocab.txt encoder-model.int8.onnx decoder_joint-model.int8.onnx; do
  curl -sSL -o ~/stt-bench-models/eou/$f \
    https://huggingface.co/ysdede/parakeet-realtime-eou-120m-v1-onnx/resolve/main/$f
done
# HF / faster-whisper / moonshine models download on first run into ~/stt-bench-models/

# 1. piper WAVs, rendered with the tool venv (read-only use of its piper)
PYTHONPATH=$PWD ~/.local/share/uv/tools/mindflock/bin/python tests/bench/stt_candidates_bench.py render
# 2. LibriSpeech dummy
PYTHONPATH=$PWD ~/stt-bench-venv/bin/python tests/bench/stt_candidates_bench.py fetch
# 3. runs: one process per config, gated on other processes' CPU use
B() { PYTHONPATH=$PWD ~/stt-bench-venv/bin/python -u tests/bench/stt_candidates_bench.py "$@"; }
B list                                     # every candidate name
B all --set piper --candidates prod-stream,moonshine-tiny-streaming+hints
B all --set piper,libri --candidates fw-small+hints,fw-base.en,parakeet-tdt-v2,parakeet-tdt-v3,\
parakeet-eou-120m-offline,moonshine-tiny,moonshine-base,moonshine-tiny-streaming,\
moonshine-small-streaming,moonshine-medium-streaming,sherpa-kroko-en,sherpa-zipformer-en,\
sherpa-fastconformer-80ms,sherpa-parakeet-tdt-v2,sherpa-parakeet-tdt-v2+hints1.0,\
fw-large-v3-turbo+hints,fw-distil-large-v3.5+hints
B all --set piper --limit 20 --candidates sherpa-nemotron-3.5-160ms
B report [--misses]                        # the tables above
```
