/** Record a microphone stream as a 16-bit PCM mono WAV — the one format every
 * transcriber behind `POST /api/voice/transcribe` accepts (the built-in
 * Windows / macOS recognizers need plain PCM; faster-whisper and OpenAI take
 * WAV too). MediaRecorder cannot produce it (webm/opus on Chrome, mp4/AAC on
 * Safari), so the samples are pulled straight off the Web Audio graph.
 *
 * Dependency-free and DOM-free: everything it touches is passed in (the
 * caller's MediaStream and running AudioContext), so Node tests drive it with
 * fakes. The stream stays the caller's — stopping its tracks is their job.
 */

const DEFAULT_TARGET_RATE = 16000;
const BLOCK_FRAMES = 4096;
/** {@link WavRecording.take} returns nothing for less audio than this (s). */
export const TAKE_MIN_S = 0.3;

/** 16-bit PCM mono WAV (44-byte RIFF header, little-endian), type "audio/wav". */
export function encodeWav(samples: Float32Array, sampleRate: number): Blob {
  const dataSize = samples.length * 2;
  const buf = new ArrayBuffer(44 + dataSize);
  const v = new DataView(buf);
  const ascii = (at: number, s: string) => {
    for (let i = 0; i < s.length; i++) v.setUint8(at + i, s.charCodeAt(i));
  };
  ascii(0, "RIFF");
  v.setUint32(4, 36 + dataSize, true);
  ascii(8, "WAVE");
  ascii(12, "fmt ");
  v.setUint32(16, 16, true); // fmt chunk size
  v.setUint16(20, 1, true); // PCM
  v.setUint16(22, 1, true); // mono
  v.setUint32(24, sampleRate, true);
  v.setUint32(28, sampleRate * 2, true); // byte rate = rate * block align
  v.setUint16(32, 2, true); // block align = channels * 2 bytes
  v.setUint16(34, 16, true); // bits per sample
  ascii(36, "data");
  v.setUint32(40, dataSize, true);
  for (let i = 0, at = 44; i < samples.length; i++, at += 2) {
    const s = samples[i];
    // NaN would otherwise land as 0 via setInt16 anyway; make it explicit.
    const c = s > 1 ? 1 : s < -1 ? -1 : s === s ? s : 0;
    v.setInt16(at, c < 0 ? Math.round(c * 32768) : Math.round(c * 32767), true);
  }
  return new Blob([buf], { type: "audio/wav" });
}

/** Output-rate samples on each side of the fallback filter's centre: wider
 * is a sharper band edge (and more work — the fallback only runs where the
 * browser has no OfflineAudioContext). */
const SINC_HALF_ZC = 16;
/** The fallback's cutoff as a fraction of the lower Nyquist: just under it,
 * so the band edge has room to fall before the frequencies that would alias. */
const SINC_ROLLOFF = 0.9;

/** Windowed-sinc resample (mono), the fallback for {@link resampleTo} where
 * there is no OfflineAudioContext (Node tests, very old browsers). A Blackman-
 * windowed sinc low-passes at {@link SINC_ROLLOFF} of the lower Nyquist, so a
 * tone above the new Nyquist is filtered out instead of folding back into the
 * speech band (plain linear interpolation let it alias). Each output is
 * normalised by its taps' sum: a constant stays exactly constant, and the
 * edges (where taps run off the clip) are not dimmed. ⌊n·to/from⌋ samples;
 * the input itself when the rates match. */
export function resample(samples: Float32Array, fromRate: number, toRate: number): Float32Array {
  if (fromRate === toRate || samples.length === 0) return samples;
  const n = samples.length;
  const outLen = Math.floor((n * toRate) / fromRate);
  const out = new Float32Array(outLen);
  const step = fromRate / toRate;
  const ratio = Math.min(1, toRate / fromRate);
  const fc = 0.5 * ratio * SINC_ROLLOFF; // cycles per input sample
  const half = SINC_HALF_ZC / ratio; // input samples each side
  for (let i = 0; i < outLen; i++) {
    const t = i * step;
    const lo = Math.max(0, Math.ceil(t - half));
    const hi = Math.min(n - 1, Math.floor(t + half));
    let acc = 0;
    let wsum = 0;
    for (let j = lo; j <= hi; j++) {
      const x = j - t;
      const u = x / half;
      const win = 0.42 + 0.5 * Math.cos(Math.PI * u) + 0.08 * Math.cos(2 * Math.PI * u);
      const arg = 2 * fc * x;
      const w = (arg === 0 ? 1 : Math.sin(Math.PI * arg) / (Math.PI * arg)) * win;
      acc += samples[j] * w;
      wsum += w;
    }
    out[i] = wsum ? acc / wsum : 0;
  }
  return out;
}

type OfflineCtor = new (channels: number, length: number, sampleRate: number) => OfflineAudioContext;

function offlineCtor(): OfflineCtor | null {
  const g = globalThis as unknown as { OfflineAudioContext?: OfflineCtor; webkitOfflineAudioContext?: OfflineCtor };
  return g.OfflineAudioContext || g.webkitOfflineAudioContext || null;
}

/** Which resampler {@link resampleTo} uses here, for the mic's diagnostics. */
export function resamplerName(): string {
  return offlineCtor() ? "browser resampler" : "windowed-sinc resampler";
}

/** Resample through the browser's own resampler — an OfflineAudioContext at
 * the target rate rendering the clip from a buffer source (band-limited, high
 * quality) — or {@link resample} where there is none or it refuses the rate
 * (old Safari: nothing under 22.05 kHz). */
export async function resampleTo(samples: Float32Array, fromRate: number, toRate: number): Promise<Float32Array> {
  const OAC = offlineCtor();
  if (!OAC || fromRate === toRate || samples.length === 0) return resample(samples, fromRate, toRate);
  try {
    const off = new OAC(1, Math.ceil((samples.length * toRate) / fromRate), toRate);
    const buf = off.createBuffer(1, samples.length, fromRate);
    buf.getChannelData(0).set(samples);
    const src = off.createBufferSource();
    src.buffer = buf;
    src.connect(off.destination);
    src.start(0);
    const rendered = await off.startRendering();
    return rendered.getChannelData(0).slice();
  } catch {
    return resample(samples, fromRate, toRate);
  }
}

export interface WavRecording {
  /** Stop and return the WAV of everything heard since start (resampled to
   * the target rate by {@link resampleTo}). */
  stop(): Promise<Blob>;
  /** The WAV of what was heard since the previous take() (or start), and
   * recording goes on — a streaming recognizer's next chunk. Null for less
   * than {@link TAKE_MIN_S} (kept for the next take), or once stopped.
   * Synchronous, so it resamples with {@link resample}; {@link stop} then
   * returns only what came after the last take. */
  take(): Blob | null;
  /** Drop everything; no blob. */
  cancel(): void;
  /** Seconds recorded so far. */
  seconds(): number;
}

export interface WavRecorderOptions {
  /** Output sample rate (default 16000). */
  targetRate?: number;
  /** Called ~every 100 ms with the RMS (0..1) of the latest block — for a level meter and silence detection. */
  onLevel?: (rms: number) => void;
  /** Drop this much audio at the start (seconds), unrecorded and unmetered:
   * the app's own voice, cut just before the mic opened (barge-in). */
  skipS?: number;
}

/** Record a MediaStream through `ctx` (the caller's AudioContext, already running) with a
 * ScriptProcessorNode(4096, 1, 1) — deprecated but present everywhere, including iOS Safari;
 * no AudioWorklet module file to load. Mono (first channel), resampled to targetRate in stop()
 * ({@link resampleTo}) and in take() ({@link resample}). */
export function startWavRecording(
  stream: MediaStream,
  ctx: AudioContext,
  opts: WavRecorderOptions = {},
): WavRecording {
  const targetRate = opts.targetRate ?? DEFAULT_TARGET_RATE;
  const onLevel = opts.onLevel;
  const source = ctx.createMediaStreamSource(stream);
  const processor = ctx.createScriptProcessor(BLOCK_FRAMES, 1, 1);
  // Chrome only runs a ScriptProcessor that reaches the destination; the
  // zero gain keeps the microphone out of the speakers.
  const mute = ctx.createGain();
  mute.gain.value = 0;

  let blocks: Float32Array[] = [];
  let frames = 0;
  let fresh = 0; // frames in `blocks`: since start or the last take()
  let done = false;
  let result: Promise<Blob> | null = null;
  let skip = Math.max(0, Math.round((opts.skipS || 0) * ctx.sampleRate));

  processor.onaudioprocess = (e: AudioProcessingEvent) => {
    if (done) return;
    if (skip > 0) {
      skip -= e.inputBuffer.getChannelData(0).length;
      return;
    }
    // The engine reuses the input buffer between callbacks — copy it.
    const block = e.inputBuffer.getChannelData(0).slice();
    blocks.push(block);
    frames += block.length;
    fresh += block.length;
    if (onLevel && block.length) {
      let sum = 0;
      for (let i = 0; i < block.length; i++) sum += block[i] * block[i];
      onLevel(Math.min(1, Math.sqrt(sum / block.length)));
    }
  };

  source.connect(processor);
  processor.connect(mute);
  mute.connect(ctx.destination);

  const teardown = () => {
    done = true;
    processor.onaudioprocess = null;
    for (const node of [source, processor, mute] as AudioNode[]) {
      try {
        node.disconnect();
      } catch {
        // Already disconnected — stop/cancel must never throw.
      }
    }
  };

  /** Everything in `blocks`, in one array; the list starts over. */
  const drain = () => {
    const all = new Float32Array(fresh);
    let at = 0;
    for (const b of blocks) {
      all.set(b, at);
      at += b.length;
    }
    blocks = [];
    fresh = 0;
    return all;
  };

  return {
    stop() {
      if (result) return result;
      teardown();
      // After cancel() the list is already empty: an empty WAV, not a throw.
      const all = drain();
      result = resampleTo(all, ctx.sampleRate, targetRate).then((pcm) => encodeWav(pcm, targetRate));
      return result;
    },
    take() {
      if (done || fresh < TAKE_MIN_S * ctx.sampleRate) return null;
      return encodeWav(resample(drain(), ctx.sampleRate, targetRate), targetRate);
    },
    cancel() {
      teardown();
      blocks = [];
      fresh = 0;
    },
    seconds() {
      return frames / ctx.sampleRate;
    },
  };
}
