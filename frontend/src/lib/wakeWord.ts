/** The wake word on the desktop: say "MindFlock" on its own and the top-bar
 * mic starts listening, as if the Talk hotkey had been pressed.
 *
 * Standby (the `wake` pref in `cs_voice_desktop`, off by default) keeps the
 * page's one microphone stream (lib/voiceMic MicKeeper) open and watches its
 * level, block by block, through the WAV recorder's `onLevel` — a rolling
 * window of a few seconds is kept, nothing is resampled and nothing leaves
 * the page until a candidate comes in:
 *  - {@link LoneUtterance}: speech that starts after quiet, lasts between
 *    {@link WAKE_MIN_MS} and {@link WAKE_MAX_MS} (a word or two — longer is a
 *    sentence, never a wake), and is followed by {@link WAKE_QUIET_MS} of
 *    quiet. Pure: a level sequence in, candidate windows out.
 *  - {@link wakeGate}: a candidate is checked only while the page is visible,
 *    no dictation records, the app is not talking (nor stopped less than
 *    {@link WAKE_AFTER_SPEECH_MS} before the clip began — its own voice must
 *    never wake it), one check in flight, at most one per
 *    {@link WAKE_CHECK_GAP_MS}.
 *  - the check: the clip (with {@link WAKE_LEAD_MS} of lead-in) goes to
 *    POST /api/voice/wake, which recognises it with the word as the hint and
 *    says whether it is the word alone (backend/web/core/wake.py). `wake:
 *    true` → {@link chime} and listening; false → nothing; 409 (no
 *    recognizer ready) → standby stops and says why.
 *
 * {@link WakeStandby} is the lifecycle around that: it runs while wanted, the
 * page is visible and nothing records; it holds the microphone while it runs
 * (so the keeper's grace never releases it); a refused microphone or a 409
 * stops it with a line for the mic's options and Settings → Voice
 * ({@link wakeStatus}). */

import { apiRaw } from "../api/client";
import { startWavRecording, type WavRecorderOptions, type WavRecording } from "./wavRecorder";
import { isLoud, noiseFloor, type MicKeeper } from "./voiceMic";

/** Quiet after the utterance that ends it (and before it, that starts one):
 * the gap around a word said on its own; a breath inside a sentence is
 * shorter. */
export const WAKE_QUIET_MS = 350;
/** Shorter speech is a click, a cough, a "hm" — not two syllables. */
export const WAKE_MIN_MS = 250;
/** Longer speech is a sentence: never a wake, however it starts. */
export const WAKE_MAX_MS = 1600;
/** Audio before the onset sent with the clip (the first consonant's attack). */
export const WAKE_LEAD_MS = 150;
/** At most one check this often… */
export const WAKE_CHECK_GAP_MS = 1500;
/** …and none for a clip that began less than this after the app stopped talking. */
export const WAKE_AFTER_SPEECH_MS = 500;
/** The rolling window standby keeps (s): the longest clip and then some. */
export const WAKE_KEEP_S = 3;
/** A wake that hears nothing this long stops listening (a miss, quietly). */
export const WAKE_LISTEN_MS = 8000;
/** The caption's line while a wake listens, before the first word. */
export const WAKE_HINT = "Yes?";
/** The chime's length; the dictation skips this much of its first audio. */
export const CHIME_MS = 120;
/** A wake check gives up after this (the next utterance tries again). */
const CHECK_TIMEOUT_MS = 10_000;
const CLIP_RATE = 16_000;

/** One candidate: `loudMs` from the onset to the last loud block, `clipMs`
 * the audio to send — the lead-in, the utterance and its quiet tail (the
 * last `clipMs` of what was heard, ending now). */
export interface WakeCandidate {
  loudMs: number;
  clipMs: number;
}

/** A stretch of speech this long lets the noise floor learn again (a fan
 * switched on is a new room, not a five-second word). */
const RELEARN_MS = 5000;

/** The lone-utterance detector, fed each audio block's RMS and length (ms).
 * The noise floor is voiceMic's (it drops to any quieter block, creeps up
 * 5% a block) — except that it does not creep during a stretch of speech
 * (under {@link RELEARN_MS}): a steady voice would otherwise turn "quiet"
 * and a sentence end early as a word. A block is speech by voiceMic's
 * {@link isLoud}. */
export class LoneUtterance {
  private floor = 0;
  /** The end of the last block fed (ms since the detector began). */
  private t = 0;
  /** Quiet before the next onset (ms). */
  private quiet = 0;
  /** The onset of the stretch under way, -1 for none. */
  private start = -1;
  /** The end of its last loud block. */
  private lastLoud = 0;
  /** The stretch outran {@link WAKE_MAX_MS}: a sentence. */
  private long = false;

  feed(rms: number, blockMs: number): WakeCandidate | null {
    const at = this.t;
    this.t += Math.max(0, blockMs);
    const learn = this.start < 0 || at - this.start > RELEARN_MS;
    this.floor = learn ? noiseFloor(this.floor, rms) : Math.min(this.floor, rms);
    const loud = isLoud(rms, this.floor);
    if (this.start < 0) {
      if (!loud) {
        this.quiet += blockMs;
        return null;
      }
      // Speech straight on from speech (or from the moment standby began):
      // the middle of something, never a word on its own.
      if (this.quiet < WAKE_QUIET_MS) {
        this.quiet = 0;
        return null;
      }
      this.start = at;
      this.lastLoud = this.t;
      this.long = false;
      return null;
    }
    if (loud) {
      this.lastLoud = this.t;
      if (this.lastLoud - this.start > WAKE_MAX_MS) this.long = true;
      return null;
    }
    const gap = this.t - this.lastLoud;
    if (gap < WAKE_QUIET_MS) return null;
    const loudMs = this.lastLoud - this.start;
    const clipMs = WAKE_LEAD_MS + (this.t - this.start);
    const long = this.long;
    this.start = -1;
    this.quiet = gap;
    return long || loudMs < WAKE_MIN_MS ? null : { loudMs, clipMs };
  }
}

/** Why a candidate is (not) checked. */
export type WakeGateResult = "ok" | "hidden" | "recording" | "speaking" | "busy" | "soon";

/** Whether a candidate ending `now` (`clipMs` long) is checked: the page
 * visible, no recording, the app quiet since {@link WAKE_AFTER_SPEECH_MS}
 * before the clip began (`lastSpoke`: when it was last heard talking), no
 * check in flight, the last one {@link WAKE_CHECK_GAP_MS} ago. Pure. */
export function wakeGate(g: {
  now: number;
  clipMs: number;
  visible: boolean;
  recording: boolean;
  speaking: boolean;
  lastSpoke: number;
  inFlight: boolean;
  lastCheck: number;
}): WakeGateResult {
  if (!g.visible) return "hidden";
  if (g.recording) return "recording";
  if (g.speaking || g.now - g.clipMs - g.lastSpoke < WAKE_AFTER_SPEECH_MS) return "speaking";
  if (g.inFlight) return "busy";
  if (g.now - g.lastCheck < WAKE_CHECK_GAP_MS) return "soon";
  return "ok";
}

/** POST /api/voice/wake's answer: `status` the HTTP status (0: no answer). */
export interface WakeVerdict {
  status: number;
  wake: boolean;
  text: string;
  error: string;
}

const langTag = () =>
  String((typeof navigator !== "undefined" && navigator.language) || "en").split(";")[0];

/** Send one clip to POST /api/voice/wake (`audio/wav`, 16 kHz mono). Never
 * throws: no answer is status 0. */
export async function postWake(clip: Blob, lang = langTag()): Promise<WakeVerdict> {
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), CHECK_TIMEOUT_MS);
  try {
    const r = await apiRaw("/api/voice/wake?lang=" + encodeURIComponent(lang), {
      method: "POST",
      headers: { "Content-Type": "audio/wav" },
      body: clip,
      signal: ac.signal,
    });
    const j = (await r.json().catch(() => ({}))) as { wake?: unknown; text?: unknown; error?: unknown };
    return { status: r.status, wake: r.ok && j.wake === true, text: String(j.text || ""), error: String(j.error || "") };
  } catch (err) {
    return { status: 0, wake: false, text: "", error: String((err as Error)?.message || err) };
  } finally {
    clearTimeout(timer);
  }
}

/** The switch's one hint line (the mic's options and Settings → Voice). */
export const WAKE_SWITCH_HINT =
  "Say “MindFlock” on its own — not inside a sentence — and the mic listens, as if you clicked it. " +
  "The mic stays open while this is on, so the browser's mic light stays on; only a lone word is sent to the server to check.";

/** The lines standby stops with. */
export const WAKE_NO_RECOGNIZER_LINE = "Standby stopped: server recognition isn't ready — Settings → Voice";
export const WAKE_DENIED_LINE = "Standby stopped: the microphone was refused";
export const WAKE_NO_MIC_LINE = "Standby stopped: no microphone";
export const WAKE_GESTURE_LINE = "Click anywhere in the app to open the mic";

/** Standby as the mic button and the options show it: `on` the mic is open
 * and listening for the word; `line` why it is not, when it should be. */
export interface WakeStatus {
  on: boolean;
  line: string;
}

let status: WakeStatus = { on: false, line: "" };
const statusSubs = new Set<(s: WakeStatus) => void>();

/** Standby's status now (Settings → Voice shows its line too). */
export function wakeStatus(): WakeStatus {
  return status;
}

export function onWakeStatus(cb: (s: WakeStatus) => void): () => void {
  statusSubs.add(cb);
  return () => {
    statusSubs.delete(cb);
  };
}

function publish(s: WakeStatus) {
  if (s.on === status.on && s.line === status.line) return;
  status = s;
  statusSubs.forEach((cb) => cb(s));
}

export interface StandbyDeps {
  mic: Pick<MicKeeper, "get" | "hold" | "done" | "warm">;
  /** The page's AudioContext (voiceAudioCtx); null: none here. */
  ctx(): AudioContext | null;
  /** The app is talking now (the SpeechQueue). */
  speaking(): boolean;
  /** A dictation is live (it has the microphone). */
  recording(): boolean;
  /** A wake: the chime, the caption, listening. */
  onWake(): void;
  /** Each block's RMS while standby runs (the button's ring). */
  onLevel?(rms: number): void;
  /** Standby turned on/off or stopped with a line (also {@link wakeStatus}). */
  onStatus?(s: WakeStatus): void;
  visible?(): boolean;
  /** Call `go` at the next click or key press (a gesture: the audio engine
   * and the microphone may start there); returns its undo. */
  onGesture?(go: () => void): () => void;
  record?: (stream: MediaStream, ctx: AudioContext, opts: WavRecorderOptions) => WavRecording;
  check?: (clip: Blob) => Promise<WakeVerdict>;
  now?(): number;
}

const pageVisible = () => typeof document === "undefined" || document.visibilityState !== "hidden";

function nextGesture(go: () => void): () => void {
  if (typeof document === "undefined") return () => {};
  const on = () => {
    off();
    go();
  };
  const off = () => {
    document.removeEventListener("pointerdown", on, true);
    document.removeEventListener("keydown", on, true);
  };
  document.addEventListener("pointerdown", on, true);
  document.addEventListener("keydown", on, true);
  return off;
}

/** Standby: listen for the wake word while {@link set} on, the page visible
 * and no dictation recording ({@link sync} re-reads those). */
export class WakeStandby {
  private want = false;
  /** Stopped with this line (a 409, a refused mic) until turned on again. */
  private blocked = "";
  /** The tap under way (its generation: a close or a reopen invalidates). */
  private gen = 0;
  private running = false;
  private holding = false;
  private rec: WavRecording | null = null;
  private det = new LoneUtterance();
  private recorded = 0;
  private lastSpoke = -Infinity;
  private lastCheck = -Infinity;
  private inFlight = false;
  private waiting: (() => void) | null = null;
  private line = "";

  constructor(private deps: StandbyDeps) {}

  private now(): number {
    return this.deps.now ? this.deps.now() : Date.now();
  }

  private visible(): boolean {
    return this.deps.visible ? this.deps.visible() : pageVisible();
  }

  get wanted(): boolean {
    return this.want;
  }

  /** The mic is open and listening for the word. */
  get on(): boolean {
    return !!this.rec;
  }

  /** Turn standby on or off (the pref). `gesture`: inside a click — the
   * microphone may be asked for (a prompt) and the audio engine started.
   * Turning it on again clears a stop's line. */
  set(want: boolean, gesture = false): void {
    if (want === this.want) {
      if (want && gesture && !this.running) this.sync(true);
      return;
    }
    this.want = want;
    this.blocked = "";
    this.line = "";
    this.sync(gesture);
  }

  /** Stop, with `line` shown, until turned on again ({@link set}). */
  stop(line: string): void {
    this.blocked = line;
    this.close();
    this.line = line;
    this.report();
  }

  /** Start or pause for the page's visibility and a dictation. */
  sync(gesture = false): void {
    const run = this.want && !this.blocked && this.visible() && !this.deps.recording();
    if (run && !this.running) void this.open(gesture);
    else if (!run && this.running) this.close();
    if (!this.want) this.line = this.blocked = "";
    this.report();
  }

  private report() {
    const s = { on: !!this.rec, line: this.want || this.blocked ? this.line : "" };
    publish(s);
    this.deps.onStatus?.(s);
  }

  private async open(gesture: boolean): Promise<void> {
    this.running = true;
    const g = ++this.gen;
    this.waiting?.();
    this.waiting = null;
    // Outside a gesture: only a microphone already granted, and an engine
    // already running — else wait for the next click (never a prompt nobody
    // asked for; a context resumed outside a gesture stays suspended).
    let ctx = this.deps.ctx();
    if (!gesture) {
      if (ctx && ctx.state === "suspended") await ctx.resume().catch(() => {});
      const ready = !!ctx && ctx.state === "running" && (await this.deps.mic.warm(true));
      if (g !== this.gen) return;
      if (!ready) {
        this.running = false;
        this.line = WAKE_GESTURE_LINE;
        this.waiting = (this.deps.onGesture || nextGesture)(() => {
          this.waiting = null;
          if (g === this.gen && !this.running) this.sync(true);
        });
        this.report();
        return;
      }
    }
    if (g !== this.gen) return;
    this.deps.mic.hold();
    this.holding = true;
    let stream: MediaStream;
    try {
      stream = await this.deps.mic.get();
    } catch (err) {
      if (g !== this.gen) return;
      const n = (err as { name?: string })?.name || "";
      if (n === "AbortError") return this.close();
      return this.stop(n === "NotFoundError" || n === "OverconstrainedError" ? WAKE_NO_MIC_LINE : WAKE_DENIED_LINE);
    }
    ctx = this.deps.ctx();
    if (ctx && ctx.state === "suspended") await ctx.resume().catch(() => {});
    if (g !== this.gen) return;
    if (!ctx || ctx.state !== "running") {
      this.close();
      this.line = WAKE_GESTURE_LINE;
      this.report();
      return;
    }
    this.det = new LoneUtterance();
    this.recorded = 0;
    const record = this.deps.record || startWavRecording;
    try {
      this.rec = record(stream, ctx, {
        targetRate: CLIP_RATE,
        keepS: WAKE_KEEP_S,
        onLevel: (rms) => this.block(g, rms),
      });
    } catch {
      this.close();
      return;
    }
    this.line = "";
    this.report();
  }

  /** One audio block: the detector, then the gate, then the check. */
  private block(g: number, rms: number): void {
    const rec = this.rec;
    if (g !== this.gen || !rec) return;
    this.deps.onLevel?.(rms);
    const total = rec.seconds() * 1000;
    const ms = total - this.recorded;
    this.recorded = total;
    const now = this.now();
    const speaking = this.deps.speaking();
    if (speaking) this.lastSpoke = now;
    const cand = this.det.feed(rms, ms);
    if (!cand) return;
    const gate = wakeGate({
      now,
      clipMs: cand.clipMs,
      visible: this.visible(),
      recording: this.deps.recording(),
      speaking,
      lastSpoke: this.lastSpoke,
      inFlight: this.inFlight,
      lastCheck: this.lastCheck,
    });
    if (gate !== "ok") return;
    this.lastCheck = now;
    void this.check(g, rec, cand);
  }

  private async check(g: number, rec: WavRecording, cand: WakeCandidate): Promise<void> {
    this.inFlight = true;
    let v: WakeVerdict | null = null;
    try {
      const clip = await rec.last(cand.clipMs / 1000);
      if (clip && g === this.gen) v = await (this.deps.check || postWake)(clip);
    } finally {
      this.inFlight = false;
    }
    if (!v || g !== this.gen) return;
    if (v.status === 409) return this.stop(WAKE_NO_RECOGNIZER_LINE);
    // A wake while something else started recording meanwhile: that one wins.
    if (v.wake && !this.deps.recording()) {
      this.deps.onWake();
      this.sync();
    }
  }

  private close(): void {
    this.gen++;
    this.running = false;
    this.waiting?.();
    this.waiting = null;
    const rec = this.rec;
    this.rec = null;
    rec?.cancel();
    if (this.holding) {
      this.holding = false;
      this.deps.mic.done();
    }
  }
}

/** The wake's chime: two short rising tones (~{@link CHIME_MS}) through the
 * page's AudioContext, quiet — no asset. Never throws. */
export function chime(ctx: AudioContext | null): void {
  if (!ctx || ctx.state !== "running") return;
  try {
    const t = ctx.currentTime;
    const end = t + CHIME_MS / 1000;
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = "sine";
    osc.frequency.setValueAtTime(660, t);
    osc.frequency.setValueAtTime(990, t + CHIME_MS / 2000);
    gain.gain.setValueAtTime(0.0001, t);
    gain.gain.exponentialRampToValueAtTime(0.12, t + 0.01);
    gain.gain.setValueAtTime(0.12, end - 0.03);
    gain.gain.exponentialRampToValueAtTime(0.0001, end);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start(t);
    osc.stop(end + 0.01);
  } catch {
    /* no audio engine: the caption still says "Yes?" */
  }
}
