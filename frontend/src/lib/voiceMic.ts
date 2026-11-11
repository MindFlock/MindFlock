/** The desktop mic's microphone, kept warm, and what it hears drawn as a level.
 *
 *  - {@link MicKeeper}: the page's one getUserMedia stream. It is asked for
 *    before the press where a gesture allows (the mic's options opening, the
 *    button going down, the ask pop-up's mic appearing) and kept
 *    {@link KEEP_STREAM_MS} after a dictation ends, so the next press records
 *    in the same tick instead of waiting on getUserMedia. The browser's "mic
 *    in use" indicator stays on through that grace; it is released at once
 *    when the page has been hidden {@link HIDDEN_RELEASE_MS}, or the mic is
 *    turned off ("mic off").
 *  - The level meter: the recorder's RMS per audio block ({@link meterFeed};
 *    the browser recognizer has none, so an AnalyserNode is polled instead,
 *    {@link meterPoll}) smoothed against a noise floor — room noise sits near
 *    zero, speech fills it — then averaged (a fast attack, {@link
 *    LEVEL_ATTACK_MS}, a slow release, {@link LEVEL_RELEASE_MS}) and
 *    stepped (quiet / hearing / loud, {@link levelStep}), and only a step
 *    change is written, as the `--mic-step` CSS variable (an opacity) on the
 *    elements that draw it ({@link meterRef}): the top-bar mic's ring, the
 *    options' level bar, the ask pop-up's mic. Never a size, never per block;
 *    no React render. Its "hearing" edge ({@link onHearing}) is the ask
 *    pop-up's "Hearing you…".
 *  - Timing ({@link voiceMark}): the moments from the press to the first
 *    word typed and the first committed one, in `window.__mfVoiceTiming` (ms
 *    after the press), each streaming pass's server time ({@link voicePass},
 *    `window.__mfVoicePasses`), and both on the console (debug) with
 *    localStorage `cs_voice_debug` = "1". */

/// <reference types="vite/client" />
// The capture worklet, bundled on its own by Vite and emitted next to app.js
// (vite.config.ts `worker`); its classes also run here where there's no worklet.
import captureWorkletUrl from "./voiceCapture.worklet.ts?worker&url";
import { CAPTURE_BLOCK_FRAMES, CAPTURE_PROCESSOR, CAPTURE_RATE, type CaptureBlock } from "./voiceCapture.worklet";
import type { WavRecorderOptions, WavRecording } from "./wavRecorder";

/** How long the microphone stays open after a dictation: long enough for a
 * back-and-forth (say a prompt, hear the reply, answer it) without a cold
 * start, short enough that the "mic in use" light doesn't linger. */
export const KEEP_STREAM_MS = 90_000;
/** A page hidden this long gives the microphone back (nobody can press the
 * mic there), grace or not. */
export const HIDDEN_RELEASE_MS = 10_000;

/** The microphone's processing, asked for explicitly: the echo of a line the
 * app says is cancelled (its tail is not transcribed), room noise suppressed. */
export const MIC_CONSTRAINTS: MediaTrackConstraints = { echoCancellation: true, noiseSuppression: true };

const stopTracks = (s: MediaStream) => s.getTracks().forEach((t) => t.stop());

/** Has the page the microphone permission already? (So a warm-up outside a
 * gesture never puts up a permission prompt nobody asked for.) */
async function micGranted(): Promise<boolean> {
  try {
    const st = await navigator.permissions?.query({ name: "microphone" as PermissionName });
    return st?.state === "granted";
  } catch {
    return false;
  }
}

/** The page's keepers: an AudioContext that starts running starts their
 * pre-roll ({@link audioCtxRunning}). */
const keepers = new Set<MicKeeper>();

/** The page's AudioContext runs now (it was resumed, or made, in a
 * gesture): every open microphone stream starts its pre-roll on it at once —
 * not only when a recording asks for the capture — so the syllables said
 * while the recording is still being set up are kept. */
export function audioCtxRunning(ctx: AudioContext): void {
  if (ctx.state === "running") keepers.forEach((k) => k.roll(ctx));
}

/** The capture worklet's module, loaded on `ctx` ahead of the first
 * recording (as the context is made): the first press then never waits up
 * to {@link CAPTURE_WAIT_MS} for it, nor falls back to a ScriptProcessor.
 * A suspended context loads modules too. Never throws. */
export function preloadCapture(ctx: AudioContext | null): void {
  if (ctx) void loadCapture(ctx).catch(() => false);
}

/** The one microphone stream, shared by every recording on the page. A
 * recording {@link hold}s it while it runs and is {@link done} after; with
 * nobody holding it, it is stopped {@link KEEP_STREAM_MS} later. */
export class MicKeeper {
  private stream: MediaStream | null = null;
  private pending: Promise<MediaStream> | null = null;
  private users = 0;
  private gen = 0;
  private timer: ReturnType<typeof setTimeout> | undefined;
  private hiddenTimer: ReturnType<typeof setTimeout> | undefined;
  /** The pre-roll on the open stream ({@link roll}): the capture graph
   * ({@link MicCapture}) where there is an AudioWorklet, else a ScriptProcessor ring. */
  private ring: PreRollRing | null = null;
  /** The capture graph being made (its worklet module loads once per context). */
  private cap: { stream: MediaStream; ctx: AudioContext; p: Promise<MicCapture | null> } | null = null;
  /** {@link onLost}'s listeners. */
  private lostSubs = new Set<() => void>();
  /** {@link markLead}: the wake word's end, and when it was marked. */
  private lead: { from: number; at: number } | null = null;

  constructor(
    private keepMs = KEEP_STREAM_MS,
    // Echo cancellation and noise suppression asked for by name (the app's
    // own voice from the speakers is not the person's), not left to defaults.
    private open: () => Promise<MediaStream> = () => navigator.mediaDevices.getUserMedia({ audio: MIC_CONSTRAINTS }),
    /** The page's AudioContext when it runs (null: none yet) — the pre-roll
     * starts on it as soon as the stream opens. */
    private ringCtx: () => AudioContext | null = () => null
  ) {
    keepers.add(this);
    // A device unplugged (or switched off) under an open stream: its track
    // may end without an event of its own — look on every device change.
    if (typeof navigator !== "undefined")
      navigator.mediaDevices?.addEventListener?.("devicechange", () => {
        const st = this.stream;
        if (st && (st.active === false || st.getTracks().some((t) => t.readyState === "ended"))) this.lost(st);
      });
    if (typeof document === "undefined" || !document.addEventListener) return;
    document.addEventListener("visibilitychange", () => {
      clearTimeout(this.hiddenTimer);
      if (document.visibilityState === "hidden") this.hiddenTimer = setTimeout(() => this.releaseIdle(), HIDDEN_RELEASE_MS);
    });
  }

  /** The stream is open now (a press records at once). */
  get isWarm(): boolean {
    return !!this.live();
  }

  private live(): MediaStream | null {
    const s = this.stream;
    return s && s.active !== false ? s : null;
  }

  /** The stream: the open one, the one being opened, or a new one. */
  get(): Promise<MediaStream> {
    const s = this.live();
    if (s) return Promise.resolve(s);
    if (this.pending) return this.pending;
    const g = this.gen;
    const p: Promise<MediaStream> = this.open().then(
      (st) => {
        if (g !== this.gen) {
          // Released while it opened: give it back — or, when a recording
          // started since, open again for it.
          stopTracks(st);
          if (this.users) return this.get();
          throw Object.assign(new Error("the microphone was released"), { name: "AbortError" });
        }
        this.pending = null;
        if (this.stream && this.stream !== st) stopTracks(this.stream);
        this.stream = st;
        // The microphone going away (unplugged, the OS taking it): its track ends.
        for (const t of st.getTracks?.() || []) t.addEventListener?.("ended", () => this.lost(st));
        if (!this.users) this.arm();
        this.roll();
        return st;
      },
      (err) => {
        if (g === this.gen) this.pending = null;
        throw err;
      }
    );
    this.pending = p;
    return p;
  }

  /** A recording runs: keep the stream until {@link done}. */
  hold(): void {
    this.users++;
    clearTimeout(this.timer);
  }

  /** A recording ended: the grace starts when it was the last. */
  done(): void {
    if (this.users > 0) this.users--;
    if (!this.users) this.arm();
  }

  /** Open it ahead of a press (and restart the grace). `ifGranted`: only
   * when the permission is already given (outside a gesture). Never throws. */
  async warm(ifGranted = false): Promise<boolean> {
    if (!this.live() && ifGranted && !(await micGranted())) return false;
    try {
      await this.get();
    } catch {
      return false;
    }
    if (!this.users) this.arm();
    return true;
  }

  /** Stop the stream now. */
  release(): void {
    this.gen++;
    this.pending = null;
    clearTimeout(this.timer);
    this.dropRing();
    const s = this.stream;
    this.stream = null;
    if (s) stopTracks(s);
  }

  private dropRing(): void {
    this.ring?.stop();
    this.ring = null;
    this.cap = null; // one still opening is stopped as it arrives
  }

  /** Keep the {@link PRE_ROLL_MS} pre-roll on the open stream through `ctx`
   * (default: the page's, when it runs) — for as long as the stream stays
   * open, dictation or not (a phrase's next one starts from it). Nothing
   * without an open stream and a running context; never throws. */
  roll(ctx: AudioContext | null = this.ringCtx()): void {
    const s = this.live();
    if (!s || !ctx || ctx.state !== "running") return;
    if (this.ring && this.ring.stream === s && this.ring.ctx === ctx) return;
    const e = this.cap;
    if (e && e.stream === s && e.ctx === ctx) return; // being made
    this.dropRing();
    if (!workletOf(ctx)) {
      this.ring = startPreRoll(s, ctx);
      return;
    }
    // The capture graph on the audio thread: the pre-roll, the recorder and
    // the meter all read its blocks. Its module loads once per context; a
    // context that can't load it gets the ScriptProcessor ring instead.
    const entry = { stream: s, ctx, p: openCapture(s, ctx) };
    this.cap = entry;
    void entry.p.then((c) => {
      if (this.cap !== entry || this.live() !== s) {
        c?.stop();
        return;
      }
      if (c) this.ring = c;
      else {
        this.cap = null;
        this.ring = startPreRoll(s, ctx);
      }
    });
  }

  /** The capture graph on the open stream through `ctx`, for a recording
   * ({@link MicCapture.record}): at once when it runs, a promise while its
   * worklet module loads (null after {@link CAPTURE_WAIT_MS}, or when it
   * can't), null where there is no AudioWorklet — the caller records with
   * its own ScriptProcessor then (lib/wavRecorder). Starts the pre-roll. */
  capture(ctx: AudioContext): MicCapture | Promise<MicCapture | null> | null {
    this.roll(ctx);
    const s = this.live();
    const r = this.ring;
    if (r instanceof MicCapture && r.stream === s && r.ctx === ctx) return r;
    const e = this.cap;
    if (!s || !e || e.stream !== s || e.ctx !== ctx) return null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    return Promise.race([
      e.p.then((c) => (c && this.ring === c ? c : null)),
      new Promise<null>((ok) => (timer = setTimeout(() => ok(null), CAPTURE_WAIT_MS))),
    ]).finally(() => clearTimeout(timer));
  }

  /** What the microphone heard in the last `ms` before now (16 kHz mono), or
   * null with no pre-roll running. Kept in the page: sent only by the
   * recording that asks for it. */
  preRoll(ms = PRE_ROLL_MS, rate = PRE_ROLL_RATE): Float32Array | null {
    const r = this.ring;
    return r && r.stream === this.live() ? r.last(ms, rate) : null;
  }

  /** A recording ended and went: everything heard up to now is in it, so a
   * later {@link preRoll} starts here — never the last phrase's tail again
   * ("…over" said twice as "ver to the left"). */
  cut(): void {
    this.ring?.cut();
  }

  /** The room's quiet as the pre-roll heard it — its quietest block's RMS
   * (0: no pre-roll): a recording's noise floor starts there, not at its
   * first block (a press that lands mid-word would make the word the floor). */
  quietFloor(): number {
    const r = this.ring;
    return r && r.stream === this.live() ? r.floor() : 0;
  }

  /** The wake word ended at `soundEnd` (ms, Date.now's clock) and listening
   * starts now: the next recording takes what the microphone heard since
   * as its lead-in ({@link takeLead}) — "MindFlock — next session" said
   * without waiting for the chime keeps its command. */
  markLead(soundEnd: number, now = Date.now()): void {
    this.lead = { from: soundEnd, at: now };
  }

  /** What the microphone heard since {@link markLead}'s word ended (16 kHz),
   * once — taken as listening starts, before its chime plays. Null without
   * a mark made in the last {@link LEAD_MARK_FRESH_MS}, without a pre-roll,
   * or when more than the ring holds ({@link RING_KEEP_MS}) went by since
   * the word: a command cut at its start is worse than none. */
  takeLead(now = Date.now()): Float32Array | null {
    const m = this.lead;
    this.lead = null;
    if (!m || now - m.at > LEAD_MARK_FRESH_MS) return null;
    const ms = now - m.from;
    if (!(ms > 0) || ms > RING_KEEP_MS) return null;
    return this.preRoll(ms);
  }

  /** `cb` runs when the open stream's microphone goes away (its track
   * ended: unplugged, taken by the OS) — the stream is dropped first, so
   * {@link get} opens a new one (the system's default microphone now). */
  onLost(cb: () => void): () => void {
    this.lostSubs.add(cb);
    return () => {
      this.lostSubs.delete(cb);
    };
  }

  private lost(st: MediaStream): void {
    if (st !== this.stream) return;
    clearTimeout(this.timer);
    this.dropRing();
    this.stream = null;
    stopTracks(st);
    this.lostSubs.forEach((cb) => cb());
  }

  /** {@link release}, unless a recording holds it. */
  releaseIdle(): void {
    if (!this.users) this.release();
  }

  private arm(): void {
    clearTimeout(this.timer);
    if (!this.stream) return;
    this.timer = setTimeout(() => this.releaseIdle(), this.keepMs);
  }
}

// --- the pre-roll ------------------------------------------------------------ //

/** The pre-roll: while the microphone stays open, the last this-many ms it
 * heard are kept in the page, so a phrase whose first syllable came before
 * its recording started (the next phrase after a pause, a press that lands
 * mid-word) is sent with it. OpenAI's realtime VAD pads 300 ms, Pipecat 500. */
export const PRE_ROLL_MS = 500;
/** A phrase whose speech is heard this early in its recording (s) started
 * before it: the pre-roll goes in front of it. Later, its own lead-in has it. */
export const PRE_ROLL_ONSET_S = 0.3;
/** The pre-roll's rate: the recordings' (whisper's) 16 kHz. */
export const PRE_ROLL_RATE = 16_000;
/** The ring keeps this much (ms) — more than {@link PRE_ROLL_MS}: a wake's
 * lead-in ({@link MicKeeper.markLead}) reaches back to the word's end, over
 * the server's check (typically well under a second; 4 s covers a slow one).
 * The room's quiet ({@link MicKeeper.quietFloor}) is still read from the
 * last PRE_ROLL_MS only. */
export const RING_KEEP_MS = 4000;
/** A {@link MicKeeper.markLead} is for the listening started right then:
 * a recording started later (a click, after a failed start) never takes it. */
export const LEAD_MARK_FRESH_MS = 1000;
/** The pre-roll's block: half the recorder's, so at most ~40 ms (48 kHz)
 * not yet delivered is missing between it and a recording that starts. */
const RING_BLOCK = 2048;

interface PreRollRing {
  stream: MediaStream;
  ctx: AudioContext;
  stop(): void;
  /** Only samples heard after now go into a later {@link last}. */
  cut(): void;
  last(ms: number, rate: number): Float32Array | null;
  floor(): number;
}

/** Average `src` (at `from` Hz) down to `to` Hz — a box filter over each
 * output sample's span: enough for half a second of lead-in (it is speech's
 * first syllable, not music), and synchronous. */
export function downsample(src: Float32Array, from: number, to: number): Float32Array {
  if (from === to || !src.length) return src;
  const n = Math.floor((src.length * to) / from);
  const out = new Float32Array(n);
  const step = from / to;
  for (let i = 0; i < n; i++) {
    const a = Math.floor(i * step);
    const b = Math.max(a + 1, Math.min(src.length, Math.floor((i + 1) * step)));
    let sum = 0;
    for (let j = a; j < b; j++) sum += src[j];
    out[i] = sum / (b - a);
  }
  return out;
}

/** The quietest block's RMS among the newest blocks covering `span`
 * samples (0: none) — the room's quiet over the last {@link PRE_ROLL_MS}. */
function ringFloor(blocks: ReadonlyArray<{ pcm: Float32Array; rms: number }>, span: number): number {
  let min = Infinity;
  let n = 0;
  for (let i = blocks.length - 1; i >= 0 && n < span; i--) {
    min = Math.min(min, blocks[i].rms);
    n += blocks[i].pcm.length;
  }
  return Number.isFinite(min) ? min : 0;
}

/** The ring on `stream`: a ScriptProcessor keeping the last
 * {@link RING_KEEP_MS} (and a block) of samples and each block's RMS. Null
 * when the audio engine can't (a context without nodes). */
function startPreRoll(stream: MediaStream, ctx: AudioContext): PreRollRing | null {
  try {
    const src = ctx.createMediaStreamSource(stream);
    const proc = ctx.createScriptProcessor(RING_BLOCK, 1, 1);
    const zero = ctx.createGain();
    zero.gain.value = 0;
    const keep = Math.round((RING_KEEP_MS / 1000) * ctx.sampleRate) + RING_BLOCK;
    const quietSpan = Math.round((PRE_ROLL_MS / 1000) * ctx.sampleRate) + RING_BLOCK;
    let blocks: Array<{ pcm: Float32Array; rms: number }> = [];
    let frames = 0;
    /** Samples heard since the last cut (capped: only its order matters). */
    let fresh = 0;
    proc.onaudioprocess = (e: AudioProcessingEvent) => {
      const pcm = e.inputBuffer.getChannelData(0).slice();
      blocks.push({ pcm, rms: rmsOf(pcm) });
      frames += pcm.length;
      fresh = Math.min(fresh + pcm.length, keep);
      while (blocks.length > 1 && frames - blocks[0].pcm.length >= keep) frames -= blocks.shift()!.pcm.length;
    };
    // Chrome runs a ScriptProcessor only when it reaches the destination:
    // through a zero gain, the microphone stays out of the speakers.
    src.connect(proc);
    proc.connect(zero);
    zero.connect(ctx.destination);
    return {
      stream,
      ctx,
      stop() {
        proc.onaudioprocess = null;
        blocks = [];
        frames = 0;
        fresh = 0;
        for (const node of [src, proc, zero] as AudioNode[]) {
          try {
            node.disconnect();
          } catch {
            /* already gone */
          }
        }
      },
      cut() {
        fresh = 0;
      },
      last(ms, rate) {
        let want = Math.min(frames, fresh, Math.round((ms / 1000) * ctx.sampleRate));
        if (!want) return null;
        const out = new Float32Array(want);
        for (let i = blocks.length - 1; i >= 0 && want > 0; i--) {
          const b = blocks[i].pcm;
          const n = Math.min(want, b.length);
          out.set(b.subarray(b.length - n), want - n);
          want -= n;
        }
        return downsample(out, ctx.sampleRate, rate);
      },
      floor() {
        return ringFloor(blocks, quietSpan);
      },
    };
  } catch {
    return null;
  }
}

// --- the capture graph (AudioWorklet) ---------------------------------------- //

/** A recording waits this long for the capture worklet's module (the first
 * one on a context loads it); slower, it records the old way this once. */
export const CAPTURE_WAIT_MS = 1500;

/** `ctx`'s AudioWorklet, when it has one and the page can make its nodes
 * (none on an insecure page, nor in an old browser). */
function workletOf(ctx: AudioContext): AudioWorklet | null {
  const w = (ctx as { audioWorklet?: AudioWorklet }).audioWorklet;
  return w && typeof w.addModule === "function" && typeof AudioWorkletNode === "function" ? w : null;
}

const loaded = new WeakMap<AudioContext, Promise<boolean>>();
/** The capture module on `ctx` (loaded once; false: it can't be). */
function loadCapture(ctx: AudioContext): Promise<boolean> {
  let p = loaded.get(ctx);
  if (!p) {
    const w = workletOf(ctx);
    p = w
      ? w.addModule(captureWorkletUrl).then(
          () => true,
          () => false
        )
      : Promise.resolve(false);
    loaded.set(ctx, p);
  }
  return p;
}

/** The one capture graph on `stream`: microphone → {@link CAPTURE_PROCESSOR}
 * (an AudioWorkletNode, lib/voiceCapture.worklet) → a zero gain → the
 * destination. The audio thread resamples to 16 kHz and posts a block per
 * {@link CAPTURE_BLOCK_FRAMES} input frames; here each block only goes into
 * the pre-roll ring and to the recordings listening ({@link record}). Null
 * when it can't be made. */
export async function openCapture(stream: MediaStream, ctx: AudioContext): Promise<MicCapture | null> {
  if (!(await loadCapture(ctx))) return null;
  try {
    const src = ctx.createMediaStreamSource(stream);
    const node = new AudioWorkletNode(ctx, CAPTURE_PROCESSOR, {
      numberOfInputs: 1,
      numberOfOutputs: 1,
      outputChannelCount: [1],
      channelCount: 1,
      channelCountMode: "explicit",
      processorOptions: { outRate: CAPTURE_RATE, blockFrames: CAPTURE_BLOCK_FRAMES },
    });
    // A node pulled by nothing may not run: through a zero gain to the
    // destination, the microphone stays out of the speakers.
    const zero = ctx.createGain();
    zero.gain.value = 0;
    src.connect(node);
    node.connect(zero);
    zero.connect(ctx.destination);
    const cap = new MicCapture(stream, ctx, () => {
      node.port.onmessage = null;
      try {
        node.port.postMessage("stop");
      } catch {
        /* closed */
      }
      for (const n of [src, node, zero] as AudioNode[]) {
        try {
          n.disconnect();
        } catch {
          /* already gone */
        }
      }
    });
    node.port.onmessage = (e: MessageEvent<CaptureBlock>) => cap.feed(e.data);
    return cap;
  } catch {
    return null;
  }
}

/** The capture graph's blocks, as the page gets them: the {@link
 * RING_KEEP_MS} ring (a {@link PreRollRing}), and the recordings ({@link
 * record}) each block goes to. */
export class MicCapture implements PreRollRing {
  private subs = new Set<(b: CaptureBlock) => void>();
  private blocks: Array<{ pcm: Float32Array; rms: number }> = [];
  private kept = 0;
  private fresh = 0;
  private stopped = false;
  private readonly keep = Math.round((RING_KEEP_MS / 1000) * CAPTURE_RATE) + Math.ceil(CAPTURE_BLOCK_FRAMES / 2);
  private readonly quietSpan = Math.round((PRE_ROLL_MS / 1000) * CAPTURE_RATE) + Math.ceil(CAPTURE_BLOCK_FRAMES / 2);

  constructor(
    readonly stream: MediaStream,
    readonly ctx: AudioContext,
    private teardown: () => void = () => {}
  ) {}

  /** One block from the audio thread. */
  feed(b: CaptureBlock): void {
    if (this.stopped || !b?.pcm) return;
    this.blocks.push({ pcm: b.pcm, rms: b.rms });
    this.kept += b.pcm.length;
    this.fresh = Math.min(this.fresh + b.pcm.length, this.keep);
    while (this.blocks.length > 1 && this.kept - this.blocks[0].pcm.length >= this.keep) this.kept -= this.blocks.shift()!.pcm.length;
    this.subs.forEach((cb) => cb(b));
  }

  /** `cb` gets every block from now on; the returned function stops it. */
  on(cb: (b: CaptureBlock) => void): () => void {
    this.subs.add(cb);
    return () => {
      this.subs.delete(cb);
    };
  }

  stop(): void {
    if (this.stopped) return;
    this.stopped = true;
    this.subs.clear();
    this.blocks = [];
    this.kept = 0;
    this.fresh = 0;
    this.teardown();
  }

  cut(): void {
    this.fresh = 0;
  }

  last(ms: number, rate: number): Float32Array | null {
    let want = Math.min(this.kept, this.fresh, Math.round((ms / 1000) * CAPTURE_RATE));
    if (!want) return null;
    const out = new Float32Array(want);
    for (let i = this.blocks.length - 1; i >= 0 && want > 0; i--) {
      const b = this.blocks[i].pcm;
      const n = Math.min(want, b.length);
      out.set(b.subarray(b.length - n), want - n);
      want -= n;
    }
    return downsample(out, CAPTURE_RATE, rate);
  }

  floor(): number {
    return ringFloor(this.blocks, this.quietSpan);
  }

  /** A recording of the blocks from now on — lib/wavRecorder's
   * {@link WavRecording}, without its own audio node: the samples come
   * resampled already, so take() and stop() only join and encode them. */
  record(opts: WavRecorderOptions = {}): WavRecording {
    return recordBlocks(this, opts);
  }
}

/** 16-bit PCM mono WAV of `samples` at `rate` (lib/wavRecorder's encodeWav;
 * kept here so the recorder needs nothing from that module at run time). */
export function pcmWav(samples: Float32Array, rate: number): Blob {
  const out = new Uint8Array(44 + samples.length * 2);
  const v = new DataView(out.buffer);
  const ascii = (at: number, s: string) => {
    for (let i = 0; i < s.length; i++) v.setUint8(at + i, s.charCodeAt(i));
  };
  ascii(0, "RIFF");
  v.setUint32(4, 36 + samples.length * 2, true);
  ascii(8, "WAVE");
  ascii(12, "fmt ");
  v.setUint32(16, 16, true);
  v.setUint16(20, 1, true);
  v.setUint16(22, 1, true);
  v.setUint32(24, rate, true);
  v.setUint32(28, rate * 2, true);
  v.setUint16(32, 2, true);
  v.setUint16(34, 16, true);
  ascii(36, "data");
  v.setUint32(40, samples.length * 2, true);
  putPcm16(v, 44, samples);
  return new Blob([out], { type: "audio/wav" });
}

/** `samples` as little-endian 16-bit PCM into `v` from byte `at` (clipped; NaN → 0). */
function putPcm16(v: DataView, at: number, samples: Float32Array): void {
  for (let i = 0; i < samples.length; i++, at += 2) {
    const s = samples[i];
    const c = s > 1 ? 1 : s < -1 ? -1 : s === s ? s : 0;
    v.setInt16(at, c < 0 ? Math.round(c * 32768) : Math.round(c * 32767), true);
  }
}

/** {@link MicCapture.record}: lib/wavRecorder's recorder semantics (skip,
 * mute with a rewind, take, last, the rolling window), counted in the
 * capture's 16 kHz samples. */
function recordBlocks(cap: MicCapture, opts: WavRecorderOptions): WavRecording {
  const rate = CAPTURE_RATE;
  const outRate = Math.min(opts.targetRate ?? rate, rate);
  const onLevel = opts.onLevel;
  let blocks: Float32Array[] = [];
  let frames = 0;
  let fresh = 0;
  let done = false;
  let skip = Math.max(0, Math.round((opts.skipS || 0) * rate));
  let muted = false;
  let held: Float32Array[] = [];
  let heldFrames = 0;
  const hold = Math.round(CAPTURE_MUTE_HOLD_S * rate);
  const keep = opts.keepS ? Math.max(1, Math.round(opts.keepS * rate)) : 0;
  const wav = (pcm: Float32Array) => pcmWav(downsample(pcm, rate, outRate), outRate);

  const off = cap.on((b) => {
    if (done) return;
    const block = b.pcm;
    if (muted) {
      held.push(block);
      heldFrames += block.length;
      while (held.length > 1 && heldFrames - held[0].length >= hold) heldFrames -= held.shift()!.length;
      return;
    }
    if (skip > 0) {
      skip -= block.length;
      return;
    }
    blocks.push(block);
    frames += block.length;
    fresh += block.length;
    while (keep && blocks.length > 1 && fresh - blocks[0].length >= keep) fresh -= blocks.shift()!.length;
    if (onLevel && block.length) onLevel(b.rms);
  });

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
  const end = () => {
    done = true;
    off();
  };
  let result: Promise<Blob> | null = null;

  return {
    stop() {
      if (result) return result;
      end();
      result = Promise.resolve(wav(drain()));
      return result;
    },
    take(minS = TAKE_MIN_S) {
      if (done || !fresh || fresh < minS * rate) return null;
      return wav(drain());
    },
    cancel() {
      end();
      blocks = [];
      fresh = 0;
    },
    mute(on: boolean, tailS = 0, rewindS = 0) {
      if (!on && muted) {
        if (rewindS > 0 && !skip) {
          let want = Math.min(heldFrames, Math.round(rewindS * rate));
          const back: Float32Array[] = [];
          for (let i = held.length - 1; i >= 0 && want > 0; i--) {
            const b = held[i];
            const part = b.length > want ? b.subarray(b.length - want) : b;
            back.unshift(part);
            want -= part.length;
          }
          for (const b of back) {
            blocks.push(b);
            frames += b.length;
            fresh += b.length;
          }
        } else skip = Math.max(skip, Math.round(tailS * rate));
      }
      if (on && !muted) {
        held = [];
        heldFrames = 0;
      }
      muted = on;
    },
    seconds() {
      return frames / rate;
    },
    async last(s: number) {
      if (done || !fresh) return null;
      let want = Math.min(fresh, Math.max(1, Math.round(s * rate)));
      const out = new Float32Array(want);
      for (let i = blocks.length - 1; i >= 0 && want > 0; i--) {
        const b = blocks[i];
        const n = Math.min(want, b.length);
        out.set(b.subarray(b.length - n), want - n);
        want -= n;
      }
      return wav(out);
    },
  };
}

/** How much of the muted audio a capture recording keeps for a rewind (s) —
 * more than lib/wavRecorder's MUTE_HOLD_S (0.5): a voice heard over a
 * confirmation ("Sent.") plays on to the line's end before the mute lifts,
 * and the whole command said over it is rewound into the phrase. */
export const CAPTURE_MUTE_HOLD_S = 1.5;
/** lib/wavRecorder's TAKE_MIN_S (the value, not imported: see {@link pcmWav}). */
const TAKE_MIN_S = 0.3;

/** `wav` (16-bit PCM mono at `rate`, as lib/wavRecorder encodes it) with
 * `pre` (at that rate) in front of its samples. Never throws: anything it can't
 * read goes as it was. */
export async function withPreRoll(pre: Float32Array | null, wav: Blob, rate = PRE_ROLL_RATE): Promise<Blob> {
  if (!pre?.length) return wav;
  try {
    const src = new DataView(await wav.arrayBuffer());
    const tag = (at: number) => String.fromCharCode(...[0, 1, 2, 3].map((i) => src.getUint8(at + i)));
    if (src.byteLength < 44 || tag(0) !== "RIFF" || tag(36) !== "data" || src.getUint16(22, true) !== 1 || src.getUint16(34, true) !== 16 || src.getUint32(24, true) !== rate)
      return wav;
    const data = src.byteLength - 44;
    const add = pre.length * 2;
    const out = new Uint8Array(44 + add + data);
    out.set(new Uint8Array(src.buffer, src.byteOffset, 44), 0);
    const v = new DataView(out.buffer);
    v.setUint32(4, 36 + add + data, true);
    v.setUint32(40, add + data, true);
    putPcm16(v, 44, pre);
    out.set(new Uint8Array(src.buffer, src.byteOffset + 44, data), 44 + add);
    return new Blob([out], { type: wav.type || "audio/wav" });
  } catch {
    return wav;
  }
}

// --- the level --------------------------------------------------------------- //

/** RMS (0..1) above which a block has speech (and over three noise floors). */
export const RMS_SPEECH = 0.02;
/** The meter falls this fast (an exponential's time constant); it rises at once. */
export const LEVEL_DECAY_MS = 150;
/** RMS over the floor that fills the meter (a raised voice). */
export const LEVEL_FULL_RMS = 0.12;
/** The ring's level is smoothed before it is stepped — asymmetrically: it
 * rises with this time constant (the ring answers a word's first syllable
 * within about a block)… */
export const LEVEL_ATTACK_MS = 120;
/** …and falls with this one (a word's gaps and syllables never flicker it;
 * the step's hysteresis holds the rest). */
export const LEVEL_RELEASE_MS = 400;
/** The ring's three steps — quiet, hearing, loud — as its opacity: the only
 * thing the meter changes on the mic (never its size; CSS eases each step
 * over ≥300 ms). */
export const STEP_OPACITY = [0.45, 0.75, 1] as const;
export type LevelStep = 0 | 1 | 2;
/** The smoothed level that enters "hearing" / "loud"; a step is left only
 * {@link STEP_HYSTERESIS} below its threshold, so the edge never jitters. */
const STEP_AT = [0.12, 0.55] as const;
const STEP_HYSTERESIS = 0.05;

/** The step for a smoothed level, from the step it is in (hysteresis). */
export function levelStep(prev: LevelStep, smoothed: number): LevelStep {
  const [hear, loud] = STEP_AT;
  if (smoothed >= loud || (prev === 2 && smoothed >= loud - STEP_HYSTERESIS)) return 2;
  if (smoothed >= hear || (prev >= 1 && smoothed >= hear - STEP_HYSTERESIS)) return 1;
  return 0;
}

/** One frame of the slow average the step reads: `prev` moves toward
 * `level` — up with the {@link LEVEL_ATTACK_MS} time constant (fast attack),
 * down with {@link LEVEL_RELEASE_MS} (slow release). */
export function slowLevel(prev: number, level: number, dtMs: number): number {
  const tau = level > prev ? LEVEL_ATTACK_MS : LEVEL_RELEASE_MS;
  return prev + (level - prev) * (1 - Math.exp(-Math.max(0, dtMs) / tau));
}

/** "Hearing you…" stays this long after the last loud block (a word's gaps). */
export const HEARING_HOLD_MS = 700;
/** No block for this long (between phrases): the meter falls to zero. */
const STALE_MS = 250;
/** How often the analyser is read (a recorder block's length). */
const POLL_MS = 85;

/** The noise floor after a block of `rms`: it drops to any quieter block at
 * once, and creeps up (5% a block) so a louder room is learnt. */
export function noiseFloor(floor: number, rms: number): number {
  return floor === 0 ? rms : Math.min(rms, floor * 1.05 + 0.001);
}

/** The block is speech: over {@link RMS_SPEECH} and three noise floors. */
export function isLoud(rms: number, floor: number): boolean {
  return rms > Math.max(RMS_SPEECH, floor * 3);
}

/** Where the meter heads for a block: 0 up to twice the noise floor (the room
 * sits at zero), then the square root of the rest over {@link LEVEL_FULL_RMS}
 * (loudness is heard roughly so), capped at 1. */
export function levelTarget(rms: number, floor: number): number {
  const over = rms - Math.max(floor * 2, 0.003);
  return over <= 0 ? 0 : Math.min(1, Math.sqrt(over / LEVEL_FULL_RMS));
}

/** One frame of the meter: up to `target` at once (attack), else down by
 * {@link LEVEL_DECAY_MS} over `dtMs`, never below the target. */
export function smoothLevel(prev: number, target: number, dtMs: number): number {
  if (target >= prev) return target;
  return Math.max(target, prev * Math.exp(-Math.max(0, dtMs) / LEVEL_DECAY_MS));
}

/** The caption's first words before any word is recognised: "Listening…"
 * from the press (the microphone is warm, or opening — nothing waits on it to
 * say so), and "Hearing you…" while the mic records (`ready`) and the level
 * says speech. */
export function pickupLine(ready: boolean, hearing: boolean): string {
  return ready && hearing ? "Hearing you…" : "Listening…";
}

/** The top bar's live caption length (settled words + the unsure tail): the
 * last ~60 characters, one line beside the mic. */
export const LIVE_CAPTION_CHARS = 60;

/** The end of `text` in about `max` characters, from a word's start after "…". */
function endOf(text: string, max: number): string {
  if (text.length <= max) return text;
  const cut = text.slice(text.length - max);
  const sp = cut.indexOf(" ");
  return "…" + (sp >= 0 && sp < cut.length - 1 ? cut.slice(sp + 1) : cut);
}

/** The settled words of the live caption: only the end of a long
 * dictation, leaving room for `tail`. */
export function liveTail(text: string, tail = "", max = LIVE_CAPTION_CHARS): string {
  return endOf(text, Math.max(16, max - tail.length));
}

/** The unsure tail of the live caption (shown muted after {@link liveTail}):
 * its end, when it alone fills the line. */
export function liveUnsure(tail: string, max = LIVE_CAPTION_CHARS): string {
  return endOf(tail, max);
}

/** The RMS of an analyser's time-domain samples. */
export function rmsOf(buf: Float32Array): number {
  let sum = 0;
  for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
  return buf.length ? Math.min(1, Math.sqrt(sum / buf.length)) : 0;
}

const els = new Set<HTMLElement>();
const hearingSubs = new Set<(on: boolean) => void>();
let level = 0;
let slow = 0;
let step: LevelStep = 0;
let target = 0;
let floor = 0;
let lastFeed = 0;
let lastLoud = 0;
let hearing = false;
let active = false;
let painted = "";
let frameAt = 0;
let raf = 0;
let poll: (() => number) | null = null;
let lastPoll = 0;

const schedule = (f: (t: number) => void): number =>
  typeof requestAnimationFrame === "function"
    ? requestAnimationFrame(f)
    : (setTimeout(() => f(Date.now()), 16) as unknown as number);

function setHearing(on: boolean) {
  if (on === hearing) return;
  hearing = on;
  hearingSubs.forEach((cb) => cb(on));
}

/** Write the step — only when it changes: an opacity, never a size. */
function paint() {
  const v = String(STEP_OPACITY[step]);
  if (v === painted) return;
  painted = v;
  els.forEach((el) => el.style.setProperty("--mic-step", v));
}

function frame(t: number) {
  raf = 0;
  const now = Date.now();
  if (poll && now - lastPoll >= POLL_MS) {
    lastPoll = now;
    meterFeed(poll(), now);
  }
  if (now - lastFeed > STALE_MS) target = 0;
  if (hearing && now - lastLoud > HEARING_HOLD_MS) setHearing(false);
  const dt = frameAt ? t - frameAt : 16;
  level = smoothLevel(level, target, dt);
  if (target === 0 && level < 0.01) level = 0;
  slow = slowLevel(slow, level, dt);
  if (!active && level === 0) slow = 0;
  step = levelStep(step, slow);
  frameAt = t;
  paint();
  if (active || level > 0) raf = schedule(frame);
  else frameAt = 0;
}

function kick() {
  if (!raf) raf = schedule(frame);
}

/** A dictation starts listening: the meter runs (a new noise floor — `seed`,
 * the room's quiet as the pre-roll heard it, else learnt from the first block). */
export function meterStart(seed = 0): void {
  active = true;
  floor = seed > 0 ? seed : 0;
  target = 0;
  lastLoud = 0;
  setHearing(false);
  kick();
}

/** Listening is over: the meter falls to zero and stops. */
export function meterStop(): void {
  active = false;
  poll = null;
  target = 0;
  setHearing(false);
  kick();
}

/** One audio block's RMS (the server recognizer's recorder). */
export function meterFeed(rms: number, now = Date.now()): void {
  if (!active) return;
  floor = noiseFloor(floor, rms);
  target = levelTarget(rms, floor);
  lastFeed = now;
  if (isLoud(rms, floor)) {
    lastLoud = now;
    setHearing(true);
  }
}

/** When the meter last heard the voice (ms; 0 before it has): a control
 * word at the end of the guess acts once it has been quiet a moment. */
export function lastLoudAt(): number {
  return lastLoud;
}

/** How long the voice has been quiet (ms) as the meter HEARD it: from its
 * last loud block to its latest block — never the time since with no block
 * at all (a phrase's seam, the next recording starting: the voice may be
 * back unseen). Infinity before it has heard the voice. "Over" is a radio
 * word only after quiet heard ({@link lastLoudAt} after the phrase ended:
 * the voice came back). */
export function meterQuietMs(): number {
  return lastLoud ? Math.max(0, lastFeed - lastLoud) : Infinity;
}

/** Read the level from `read` instead (an AnalyserNode, for the browser
 * recognizer), every {@link POLL_MS}; null stops. */
export function meterPoll(read: (() => number) | null): void {
  poll = read;
  lastPoll = 0;
  if (read) kick();
}

/** Speech starts or stops being heard (the caption's "Hearing you…"). */
export function onHearing(cb: (on: boolean) => void): () => void {
  hearingSubs.add(cb);
  return () => {
    hearingSubs.delete(cb);
  };
}

/** A ref that draws the level's step on its element (`--mic-step`, an
 * opacity), as long as it is mounted (React 19 runs the returned cleanup on
 * detach). */
export function meterRef(el: HTMLElement | null): (() => void) | undefined {
  if (!el) return undefined;
  els.add(el);
  el.style.setProperty("--mic-step", String(STEP_OPACITY[step]));
  return () => {
    els.delete(el);
  };
}

// --- timing ------------------------------------------------------------------ //

/** The moments of a start, in order: the press, the mic recording, the
 * server stream open, the first speech, the first unsure words, the first
 * characters typed (a guess, usually — "first partial typed"), the first
 * committed words, the first phrase's accurate final back (/end). */
export type VoiceMoment = "start" | "mic" | "stream" | "speech" | "partial" | "typed" | "commit" | "final";

declare global {
  interface Window {
    __mfVoiceTiming?: Partial<Record<VoiceMoment, number>>;
    /** The last {@link PASSES_KEPT} streaming passes' server times (ms). */
    __mfVoicePasses?: number[];
    /** The last wake's latency (ms): the word's end → listening. */
    __mfVoiceWake?: number;
    /** The last "over" → Enter (ms): the word's end → the send. */
    __mfVoiceSent?: number;
  }
}

const PASSES_KEPT = 20;
let passes: number[] = [];

let t0 = 0;
let marks: Partial<Record<VoiceMoment, number>> = {};
const clock = () => (typeof performance !== "undefined" ? performance.now() : Date.now());

function debugOn(): boolean {
  try {
    return typeof localStorage !== "undefined" && localStorage.getItem("cs_voice_debug") === "1";
  } catch {
    return false;
  }
}

/** Mark `m` (the first time since the press; "start" begins a new run). */
export function voiceMark(m: VoiceMoment): void {
  if (m === "start") {
    t0 = clock();
    marks = {};
  } else if (!t0 || m in marks) return;
  marks[m] = Math.round(clock() - t0);
  if (typeof window !== "undefined") window.__mfVoiceTiming = { ...marks };
  if (debugOn()) console.debug("[voice] " + m + " +" + marks[m] + " ms");
}

/** A wake took `ms` from the word's end to listening (the debug trace's
 * "wake N ms"; the mic's diagnostics show the last). */
export function voiceWake(ms: number): void {
  if (typeof window !== "undefined") window.__mfVoiceWake = Math.round(ms);
  if (debugOn()) console.debug("[voice] wake " + Math.round(ms) + " ms (sound end → listening)");
}

/** "Over" took `ms` from the word's end to Enter (the debug trace's "over →
 * sent N s"; the mic's diagnostics show the last). */
export function voiceSent(ms: number): void {
  if (typeof window !== "undefined") window.__mfVoiceSent = Math.round(ms);
  if (debugOn()) console.debug("[voice] over → sent " + (ms / 1000).toFixed(1) + " s");
}

/** A streaming pass took `ms` on the server (its chunk answer's `ms`). */
export function voicePass(ms: number): void {
  passes = [...passes, Math.round(ms)].slice(-PASSES_KEPT);
  if (typeof window !== "undefined") window.__mfVoicePasses = passes;
  if (debugOn()) console.debug("[voice] pass " + Math.round(ms) + " ms");
}
