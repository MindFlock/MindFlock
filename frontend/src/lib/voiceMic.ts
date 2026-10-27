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
 *    zero, speech fills it — and written once per animation frame as the
 *    `--mic-level` CSS variable (0..1) on the elements that draw it
 *    ({@link meterRef}): the top-bar mic's ring, the options' level bar, the
 *    ask pop-up's mic. No React render per block. Its "hearing" edge
 *    ({@link onHearing}) turns the caption's "Listening…" into "Hearing you…".
 *  - Timing ({@link voiceMark}): the moments from the press to the first
 *    committed word, in `window.__mfVoiceTiming` (ms after the press), and on
 *    the console (debug) with localStorage `cs_voice_debug` = "1". */

/** How long the microphone stays open after a dictation: long enough for a
 * back-and-forth (say a prompt, hear the reply, answer it) without a cold
 * start, short enough that the "mic in use" light doesn't linger. */
export const KEEP_STREAM_MS = 90_000;
/** A page hidden this long gives the microphone back (nobody can press the
 * mic there), grace or not. */
export const HIDDEN_RELEASE_MS = 10_000;

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

  constructor(
    private keepMs = KEEP_STREAM_MS,
    private open: () => Promise<MediaStream> = () => navigator.mediaDevices.getUserMedia({ audio: true })
  ) {
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
        if (!this.users) this.arm();
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
    const s = this.stream;
    this.stream = null;
    if (s) stopTracks(s);
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

// --- the level --------------------------------------------------------------- //

/** RMS (0..1) above which a block has speech (and over three noise floors). */
export const RMS_SPEECH = 0.02;
/** The meter falls this fast (an exponential's time constant); it rises at once. */
export const LEVEL_DECAY_MS = 150;
/** RMS over the floor that fills the meter (a raised voice). */
export const LEVEL_FULL_RMS = 0.12;
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

/** The caption's first words before any word is recognised: nothing until
 * the mic records (`ready`), then "Hearing you…" while the level says speech,
 * else "Listening…". */
export function pickupLine(ready: boolean, hearing: boolean): string {
  if (!ready) return "";
  return hearing ? "Hearing you…" : "Listening…";
}

/** The live caption's length (committed words + the unsure tail): about a
 * line and a half beside the mic. */
export const LIVE_CAPTION_CHARS = 90;

/** The committed words of the live caption: only the end of a long
 * dictation (from a word's start, after "…"), leaving room for `tail`. */
export function liveTail(text: string, tail = "", max = LIVE_CAPTION_CHARS): string {
  const room = Math.max(24, max - tail.length);
  if (text.length <= room) return text;
  const cut = text.slice(text.length - room);
  const sp = cut.indexOf(" ");
  return "…" + (sp >= 0 && sp < cut.length - 1 ? cut.slice(sp + 1) : cut);
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

function paint() {
  const v = level.toFixed(3);
  if (v === painted) return;
  painted = v;
  els.forEach((el) => el.style.setProperty("--mic-level", v));
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
  level = smoothLevel(level, target, frameAt ? t - frameAt : 16);
  if (target === 0 && level < 0.01) level = 0;
  frameAt = t;
  paint();
  if (active || level > 0) raf = schedule(frame);
  else frameAt = 0;
}

function kick() {
  if (!raf) raf = schedule(frame);
}

/** A dictation starts listening: the meter runs (a new noise floor). */
export function meterStart(): void {
  active = true;
  floor = 0;
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

/** A ref that draws the level on its element (`--mic-level`), as long as
 * it is mounted (React 19 runs the returned cleanup on detach). */
export function meterRef(el: HTMLElement | null): (() => void) | undefined {
  if (!el) return undefined;
  els.add(el);
  el.style.setProperty("--mic-level", level.toFixed(3));
  return () => {
    els.delete(el);
  };
}

// --- timing ------------------------------------------------------------------ //

/** The moments of a start, in order: the press, the mic recording, the
 * server stream open, the first speech, the first unsure words, the first
 * committed (typed) ones. */
export type VoiceMoment = "start" | "mic" | "stream" | "speech" | "partial" | "commit";

declare global {
  interface Window {
    __mfVoiceTiming?: Partial<Record<VoiceMoment, number>>;
  }
}

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
