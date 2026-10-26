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

/** Linear-interpolation resample (mono). Returns the input when rates match. */
export function resample(samples: Float32Array, fromRate: number, toRate: number): Float32Array {
  if (fromRate === toRate || samples.length === 0) return samples;
  const n = samples.length;
  const outLen = Math.floor((n * toRate) / fromRate);
  const out = new Float32Array(outLen);
  const step = fromRate / toRate;
  for (let i = 0; i < outLen; i++) {
    const pos = i * step;
    const i0 = Math.floor(pos);
    const i1 = i0 + 1 < n ? i0 + 1 : n - 1;
    const frac = pos - i0;
    out[i] = samples[i0] + (samples[i1] - samples[i0]) * frac;
  }
  return out;
}

export interface WavRecording {
  /** Stop and return the WAV of everything heard since start. */
  stop(): Promise<Blob>;
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
}

/** Record a MediaStream through `ctx` (the caller's AudioContext, already running) with a
 * ScriptProcessorNode(4096, 1, 1) — deprecated but present everywhere, including iOS Safari;
 * no AudioWorklet module file to load. Mono (first channel), resampled to targetRate in stop(). */
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
  let done = false;
  let result: Promise<Blob> | null = null;

  processor.onaudioprocess = (e: AudioProcessingEvent) => {
    if (done) return;
    // The engine reuses the input buffer between callbacks — copy it.
    const block = e.inputBuffer.getChannelData(0).slice();
    blocks.push(block);
    frames += block.length;
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

  return {
    stop() {
      if (result) return result;
      teardown();
      // After cancel() the list is already empty: an empty WAV, not a throw.
      const all = new Float32Array(blocks.reduce((n, b) => n + b.length, 0));
      let at = 0;
      for (const b of blocks) {
        all.set(b, at);
        at += b.length;
      }
      blocks = [];
      result = Promise.resolve(encodeWav(resample(all, ctx.sampleRate, targetRate), targetRate));
      return result;
    },
    cancel() {
      teardown();
      blocks = [];
    },
    seconds() {
      return frames / ctx.sampleRate;
    },
  };
}
