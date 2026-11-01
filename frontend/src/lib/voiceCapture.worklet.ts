/** The desktop microphone's capture, on the audio thread: an
 * AudioWorkletProcessor that takes the microphone at the context's rate,
 * resamples it to 16 kHz as it comes (a streaming windowed sinc, the same
 * filter as lib/wavRecorder's `resample`) and posts one block per
 * {@link CAPTURE_BLOCK_FRAMES} input frames over its port: the 16 kHz
 * samples, the block's RMS and the input frames counted so far. The page's
 * main thread only queues what it gets — a busy page (a render, a flood of
 * terminal output) delays the messages, it never loses a block, and never
 * has to resample.
 *
 * This file is two things:
 *  - the worklet module, loaded by `audioWorklet.addModule` from the URL lib/voiceMic
 *    imports it as (`?worker&url`: Vite bundles it on its own and emits it next to
 *    app.js, see vite.config.ts) — it registers {@link CAPTURE_PROCESSOR};
 *  - {@link CaptureCore}, the processor's pure logic, which lib/voiceMic also runs
 *    on the main thread behind a ScriptProcessor where there is no AudioWorklet, and
 *    the tests drive directly.
 * It imports nothing: a worklet module is loaded alone. */

/** The processor's registered name. */
export const CAPTURE_PROCESSOR = "mf-voice-capture";
/** Input frames per posted block: the recorder's old ScriptProcessor block
 * (85 ms at 48 kHz) — the noise floor and the quiet that ends a phrase are
 * counted per block, so their pace stays what it was. */
export const CAPTURE_BLOCK_FRAMES = 4096;
/** The rate the blocks are posted at: whisper's (and the built-in recognizers'). */
export const CAPTURE_RATE = 16_000;

/** One posted block. `pcm`: the 16 kHz samples resampled in it; `rms`: the
 * block's RMS (0..1) at the input rate; `frames`: input frames counted at its
 * end (its end on the capture's clock is `frames / inRate` seconds). */
export interface CaptureBlock {
  pcm: Float32Array;
  rms: number;
  frames: number;
}

/** Output-rate samples on each side of the filter's centre, and its cutoff
 * as a fraction of the lower Nyquist — wavRecorder's SINC_HALF_ZC / SINC_ROLLOFF. */
const SINC_HALF_ZC = 16;
const SINC_ROLLOFF = 0.9;
/** More phases than this (an odd rate pair) compute their taps per sample. */
const MAX_PHASES = 2048;

const gcd = (a: number, b: number): number => (b ? gcd(b, a % b) : a);

/** A streaming resampler (mono): samples go in as they come, and every output
 * sample whose filter span is complete comes out. Output `i` sits at input
 * position `i·from/to`; its taps are a Blackman-windowed sinc low-passing at
 * {@link SINC_ROLLOFF} of the lower Nyquist, normalised by their sum (a
 * constant stays constant). The taps depend only on the position's fraction,
 * so for a rational rate pair (48k→16k: one phase; 44.1k→16k: 160) they are
 * computed once. Before the first sample the input counts as silence. */
export class StreamResampler {
  private readonly L: number; // output steps per M input samples
  private readonly M: number;
  private readonly half: number; // input samples each side of the centre
  private readonly K: number; // taps each side, whole samples
  private readonly fc: number;
  private readonly table: Float32Array[] | null;
  /** Kept input; `buf[0]` is input sample `base` (negative: the silence before). */
  private buf: Float32Array;
  private len = 0;
  private base: number;
  private next = 0; // the next output sample's index

  constructor(
    readonly fromRate: number,
    readonly toRate: number
  ) {
    const from = Math.round(fromRate);
    const to = Math.round(toRate);
    const g = gcd(from, to) || 1;
    this.L = to / g;
    this.M = from / g;
    const ratio = Math.min(1, to / from);
    this.fc = 0.5 * ratio * SINC_ROLLOFF;
    this.half = SINC_HALF_ZC / ratio;
    this.K = Math.ceil(this.half);
    this.base = -this.K;
    this.buf = new Float32Array(Math.max(1024, 4 * this.K + 512));
    this.len = this.K; // the silence before the first sample
    this.table = from === to || this.L > MAX_PHASES ? null : Array.from({ length: this.L }, (_, p) => this.taps(p / this.L));
  }

  /** The normalised taps for a centre `frac` past a whole input sample:
   * offsets −K+1 … K (index 0 is offset −K+1). */
  private taps(frac: number): Float32Array {
    const K = this.K;
    const out = new Float32Array(2 * K);
    let sum = 0;
    for (let k = -K + 1; k <= K; k++) {
      const x = k - frac;
      const u = x / this.half;
      if (u < -1 || u > 1) continue;
      const win = 0.42 + 0.5 * Math.cos(Math.PI * u) + 0.08 * Math.cos(2 * Math.PI * u);
      const arg = 2 * this.fc * x;
      const w = (arg === 0 ? 1 : Math.sin(Math.PI * arg) / (Math.PI * arg)) * win;
      out[k + K - 1] = w;
      sum += w;
    }
    if (sum) for (let i = 0; i < out.length; i++) out[i] /= sum;
    return out;
  }

  /** Feed `input`; the output samples now complete are returned (possibly none). */
  push(input: Float32Array): Float32Array {
    if (this.fromRate === this.toRate) return input.slice();
    this.append(input);
    const { L, M, K } = this;
    const end = this.base + this.len; // input samples available: [base, end)
    // Output i is complete once input ⌊i·M/L⌋ + K is here.
    const last = Math.floor(((end - 1 - K) * L) / M); // the last complete output's index (≤)
    const n = Math.max(0, last - this.next + 1);
    const out = new Float32Array(n);
    for (let o = 0; o < n; o++) {
      const i = this.next + o;
      const pos = i * M;
      const c = Math.floor(pos / L);
      const ph = pos - c * L;
      const taps = this.table ? this.table[ph] : this.taps(ph / L);
      const at = c - K + 1 - this.base;
      let acc = 0;
      for (let k = 0; k < taps.length; k++) acc += this.buf[at + k] * taps[k];
      out[o] = acc;
    }
    this.next += n;
    // Drop what no later output needs: everything before ⌊next·M/L⌋ − K + 1.
    const keepFrom = Math.floor((this.next * M) / L) - K + 1;
    const drop = Math.min(this.len, Math.max(0, keepFrom - this.base));
    if (drop) {
      this.buf.copyWithin(0, drop, this.len);
      this.len -= drop;
      this.base += drop;
    }
    return out;
  }

  private append(input: Float32Array): void {
    if (this.len + input.length > this.buf.length) {
      const grown = new Float32Array(Math.max(this.buf.length * 2, this.len + input.length));
      grown.set(this.buf.subarray(0, this.len));
      this.buf = grown;
    }
    this.buf.set(input, this.len);
    this.len += input.length;
  }
}

/** The processor's logic, thread-free: {@link push} the input as it comes
 * (render quanta of 128 frames, or a ScriptProcessor's whole buffer); every
 * {@link CAPTURE_BLOCK_FRAMES} input frames one {@link CaptureBlock} is handed
 * to `emit`. */
export class CaptureCore {
  private rs: StreamResampler;
  private parts: Float32Array[] = [];
  private outLen = 0;
  private inBlock = 0;
  private sumSq = 0;
  /** Input frames counted. */
  frames = 0;

  constructor(
    readonly inRate: number,
    private emit: (b: CaptureBlock) => void,
    readonly outRate = CAPTURE_RATE,
    readonly blockFrames = CAPTURE_BLOCK_FRAMES
  ) {
    this.rs = new StreamResampler(inRate, outRate);
  }

  push(input: Float32Array): void {
    let from = 0;
    while (from < input.length) {
      const n = Math.min(input.length - from, this.blockFrames - this.inBlock);
      const part = from === 0 && n === input.length ? input : input.subarray(from, from + n);
      for (let i = 0; i < part.length; i++) this.sumSq += part[i] * part[i];
      const out = this.rs.push(part);
      if (out.length) {
        this.parts.push(out);
        this.outLen += out.length;
      }
      this.inBlock += n;
      this.frames += n;
      from += n;
      if (this.inBlock >= this.blockFrames) this.flush();
    }
  }

  private flush(): void {
    const pcm = new Float32Array(this.outLen);
    let at = 0;
    for (const p of this.parts) {
      pcm.set(p, at);
      at += p.length;
    }
    const rms = this.inBlock ? Math.min(1, Math.sqrt(this.sumSq / this.inBlock)) : 0;
    this.parts = [];
    this.outLen = 0;
    this.inBlock = 0;
    this.sumSq = 0;
    this.emit({ pcm, rms, frames: this.frames });
  }
}

// --- the worklet ------------------------------------------------------------- //
// Only inside an AudioWorkletGlobalScope (registerProcessor exists there);
// imported anywhere else, this file is just the classes above.

declare const sampleRate: number;
declare function registerProcessor(name: string, ctor: unknown): void;
declare const AudioWorkletProcessor: {
  prototype: object;
  new (options?: unknown): { readonly port: MessagePort };
};

if (typeof registerProcessor === "function" && typeof AudioWorkletProcessor === "function") {
  registerProcessor(
    CAPTURE_PROCESSOR,
    class extends AudioWorkletProcessor {
      private core: CaptureCore;
      private stopped = false;
      constructor(options?: { processorOptions?: { outRate?: number; blockFrames?: number } }) {
        super(options);
        const o = options?.processorOptions || {};
        this.core = new CaptureCore(
          sampleRate,
          (b) => this.port.postMessage(b, [b.pcm.buffer]),
          o.outRate || CAPTURE_RATE,
          o.blockFrames || CAPTURE_BLOCK_FRAMES
        );
        this.port.onmessage = (e: MessageEvent) => {
          if (e.data === "stop") this.stopped = true;
        };
      }
      process(inputs: Float32Array[][]): boolean {
        if (this.stopped) return false;
        const ch = inputs[0]?.[0];
        if (ch?.length) this.core.push(ch);
        return true;
      }
    }
  );
}
