/** The desktop voice, smoothed (voice loop cycle 1): no deaf mic, no clipped
 * first word, every chime heard, "over" without stacked waits.
 *
 * What is pinned:
 *  1. the mic is muted from a line's first SOUND to its end — not from its
 *     turn (a reply being synthesised is silence the person may talk into) —
 *     and a confirmation's unmute never lifts the next line's mute;
 *  2. the pre-roll: the half second before a recording, kept while the
 *     microphone is open, goes in front of a phrase whose speech began with
 *     it; the noise floor starts from the room's quiet it heard; the hotkey
 *     warms the mic like the button;
 *  3. a chime on a cold AudioContext waits (≤ 400 ms) for its resume instead
 *     of being dropped, and the recorder's chime skip counts from then;
 *  4. "over" ends the phrase as soon as the voice has stopped a moment, so
 *     the final and the 250 ms quiet overlap — and the voice coming back
 *     first makes it a plain word, as before;
 *  5. the router's first phrase shows in the caption at once (muted);
 *  6. "Hold to talk": a tap leaves the mic on; a press while listening is
 *     ending (or just ended) starts nothing;
 *  7. (judge) a microphone that goes away mid-listen is noticed: opened
 *     again once, else listening stops and says so; a phrase taken by "over"
 *     is never typed and sent twice by its late end; the button tells
 *     listening from "muted while a line plays";
 *  8. (cycle 2) "over"'s quiet is only what the meter heard — a phrase's
 *     seam (no blocks) is not quiet, so a breath after "over" never sends
 *     mid-sentence; the pre-roll never repeats audio a phrase already sent
 *     (it starts at the last phrase's end); the microphone asks for echo
 *     cancellation and noise suppression explicitly;
 *  9. (cycle 3, R8-bis) a dialog is answered only by words BEGUN at least
 *     ANSWER_AFTER_MS after this page first had it: each phrase carries when
 *     it began (the server recognizer's first loud block) to the router
 *     (`spoke_ms_ago`, `spoke_at`, `dialog_seen`), and the page refuses an
 *     answer begun sooner — the server's `early` is said, nothing pressed;
 *     each page opens its streams with its own `client` id. */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  CHIME_LATE_MS,
  CONTROL_POLL_MS,
  CONTROL_TAIL_MS,
  MIC_GONE_LINE,
  MIC_LOST_LINE,
  MIC_LOST_RETRY_MS,
  SWALLOW_MAX_MS,
  SWALLOW_STREAM_MAX_MS,
  Confirmer,
  LiveDictation,
  OVER_EARLY_QUIET_MS,
  ANSWER_AFTER_MS,
  DialogSeen,
  HandsFree,
  RecordingClock,
  SILENCE_MS,
  TURN_MAX_MS,
  TURN_MIN_MS,
  dialogSeen,
  dialogStopLine,
  parseTurn,
  turnHoldMs,
  FIRST_PHRASE_HOLD_MS,
  SPOKE_LEAD_MS,
  SUBMIT_SETTLE_MS,
  SpeechQueue,
  VOICE_CLIENT_ID,
  earlyLine,
  routePhrase,
  runVoiceCommand,
  type CommandDeps,
  type RouteReply,
  TALK_DEBOUNCE_MS,
  TAP_HOLD_MS,
  finalClock,
  serverRecognizer,
  talkKeyAction,
  voiceAudioCtx,
  whenRunning,
  type DictTarget,
  type LiveHooks,
  type RecognizerHandlers,
  type Speaker,
} from "../lib/voiceDesktop";
import {
  MicKeeper,
  PRE_ROLL_MS,
  PRE_ROLL_ONSET_S,
  PRE_ROLL_RATE,
  downsample,
  lastLoudAt,
  meterFeed,
  meterQuietMs,
  meterStart,
  meterStop,
  withPreRoll,
} from "../lib/voiceMic";
import { code, src } from "./sourceText";
import { api } from "../api/client";

const mocks = vi.hoisted(() => ({
  apiRaw: vi.fn(),
  instApi: vi.fn(),
  startWavRecording: vi.fn(),
}));
vi.mock("../api/client", () => ({ api: vi.fn(), apiRaw: mocks.apiRaw, instApi: mocks.instApi, ApiError: Error }));
vi.mock("../lib/terminals", () => ({ peekTerm: vi.fn() }));
vi.mock("../lib/wavRecorder", () => ({ startWavRecording: mocks.startWavRecording, resamplerName: () => "browser resampler" }));

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  mocks.apiRaw.mockReset();
  mocks.instApi.mockReset();
  mocks.startWavRecording.mockReset();
  finalClock.reset();
});

// --- 1. muted while a line SOUNDS ------------------------------------------- //

/** An engine that reports its first sound when told (`sound()`), and ends
 * when told (`end()`); `instant`: sounds inside speak() (a cached line). */
function engine(o: { instant?: boolean } = {}) {
  const said: string[] = [];
  let finish: (() => void) | null = null;
  let audible: (() => void) | undefined;
  const sp: Speaker = {
    reportsAudible: true,
    speak: (t, a) => {
      said.push(t);
      audible = a;
      if (o.instant) a?.();
      return new Promise<void>((r) => (finish = r));
    },
    cancel: () => finish?.(),
  };
  return {
    sp,
    said,
    sound: () => audible?.(),
    end: async () => {
      finish?.();
      await Promise.resolve();
      await Promise.resolve();
    },
  };
}

describe("the mic is muted while a line sounds, not while it waits for its audio", () => {
  it("onAudible comes with the first sound (start too); onSpeaking with the turn", async () => {
    const e = engine();
    const ev: string[] = [];
    const q = new SpeechQueue(() => e.sp, (on) => void ev.push("turn " + on), { onAudible: (on) => void ev.push("sound " + on) });
    const started: string[] = [];
    void q.confirmLine("Sent.", { start: () => started.push("Sent.") });
    expect(e.said).toEqual(["Sent."]);
    // Fetching its audio: the pill may say "Speaking…", the mic still hears.
    expect(ev).toEqual(["turn true"]);
    expect(started).toEqual([]);
    expect(q.audibleNow).toBe(false);
    e.sound();
    expect(ev).toEqual(["turn true", "sound true"]);
    expect(started).toEqual(["Sent."]);
    expect(q.audibleNow).toBe(true);
    e.sound(); // once
    expect(ev).toEqual(["turn true", "sound true"]);
    await e.end();
    expect(ev).toEqual(["turn true", "sound true", "turn false", "sound false"]);
    expect(q.audibleNow).toBe(false);
  });

  it("a line cut before it ever sounded never muted anything", async () => {
    const e = engine();
    const ev: string[] = [];
    const q = new SpeechQueue(() => e.sp, () => {}, { onAudible: (on) => void ev.push("sound " + on) });
    void q.say("A reply that is slow to synthesise.");
    q.hold(); // the person keyed the mic
    expect(ev).toEqual([]);
    e.sound(); // a late first sound of the cut line: ignored
    expect(ev).toEqual([]);
  });

  it("an engine that can't tell (no reportsAudible): muted from its turn, as before", () => {
    const ev: string[] = [];
    const sp: Speaker = { speak: () => new Promise<void>(() => {}), cancel: () => {} };
    const q = new SpeechQueue(() => sp, () => {}, { onAudible: (on) => void ev.push("sound " + on) });
    void q.say("Hello.");
    expect(ev).toEqual(["sound true"]);
  });

  it("a chime is not a line: no mute, never audibleNow", () => {
    const ev: string[] = [];
    const q = new SpeechQueue(() => engine().sp, () => {}, { onAudible: (on) => void ev.push("sound " + on) });
    void q.cue(() => {}, 100);
    expect(q.busy).toBe(true);
    expect(q.audibleNow).toBe(false);
    expect(ev).toEqual([]);
  });

  /** As TopBarMic wires it: the Confirmer mutes its line, the gated listener
   * mutes every line from its sound; `mutes` is the recorder's last word. */
  function wired(e: ReturnType<typeof engine>) {
    const mutes: boolean[] = [];
    const mic = { mute: (b: boolean) => void mutes.push(b) };
    const q = new SpeechQueue(() => e.sp, () => {}, { onAudible: (on) => mic.mute(on) });
    const c = new Confirmer({
      queue: q,
      on: () => true,
      mic: () => mic,
      server: () => false,
      local: () => false,
      speaker: { lastError: "", prefetch: async () => {} },
    });
    return { q, c, mutes, muted: () => !!mutes.at(-1) };
  }

  it("a confirmation then a reply: the reply's mute is never lifted by the confirmation's end", async () => {
    for (const instant of [false, true]) {
      const e = engine({ instant });
      const w = wired(e);
      const said = w.c.say("sent");
      void w.q.say("Login fix: the tests pass.");
      e.sound();
      expect(w.muted()).toBe(true);
      await e.end(); // "Sent." over: the reply's turn
      await said;
      await Promise.resolve();
      if (!instant) {
        // Still being synthesised: the mic hears (nothing plays).
        expect(e.said.at(-1)).toBe("Login fix: the tests pass.");
        expect(w.muted()).toBe(false);
        e.sound();
      }
      // The reply sounds: muted — whatever order the two callbacks ran in.
      expect(w.muted()).toBe(true);
      await e.end();
      // Nothing playing: never deaf.
      expect(w.q.idle).toBe(true);
      expect(w.muted()).toBe(false);
    }
  });
});

// --- 2. the pre-roll ---------------------------------------------------------- //

/** An AudioContext with just what the pre-roll and the recorder touch; its
 * ScriptProcessors are kept so a test can feed them blocks. */
class NodeCtx {
  static last: NodeCtx | null = null;
  state: AudioContextState = "running";
  sampleRate = 48000;
  destination = {};
  procs: Array<{ onaudioprocess: ((e: unknown) => void) | null; size: number }> = [];
  resumes = 0;
  onResume: (() => void) | null = null;
  constructor() {
    NodeCtx.last = this;
  }
  resume() {
    this.resumes++;
    this.onResume?.();
    return Promise.resolve();
  }
  createMediaStreamSource() {
    return { connect: () => {}, disconnect: () => {} };
  }
  createScriptProcessor(size: number) {
    const p = { onaudioprocess: null as ((e: unknown) => void) | null, size, connect: () => {}, disconnect: () => {} };
    this.procs.push(p);
    return p;
  }
  createGain() {
    return { gain: { value: 1 }, connect: () => {}, disconnect: () => {} };
  }
  /** One block of `value` into every live ScriptProcessor. */
  feed(value: number) {
    for (const p of this.procs) {
      const data = new Float32Array(p.size).fill(value);
      p.onaudioprocess?.({ inputBuffer: { getChannelData: () => data } });
    }
  }
}

function fakeStream() {
  const track = { stop: vi.fn() };
  return { active: true, getTracks: () => [track], track } as unknown as MediaStream;
}

describe("the pre-roll: the half second before a recording", () => {
  it("is kept while the stream is open (16 kHz, the last 500 ms); its quietest block is the room's floor; release stops it", async () => {
    const ctx = new NodeCtx();
    const mic = new MicKeeper(60_000, async () => fakeStream(), () => ctx as unknown as AudioContext);
    expect(mic.preRoll()).toBeNull();
    expect(mic.quietFloor()).toBe(0);
    await mic.get();
    expect(ctx.procs).toHaveLength(1); // the ring started with the stream
    for (let i = 0; i < 20; i++) ctx.feed(i === 3 ? 0.002 : 0.05);
    const pre = mic.preRoll()!;
    expect(pre.length).toBe((PRE_ROLL_MS / 1000) * PRE_ROLL_RATE);
    expect(pre[0]).toBeCloseTo(0.05);
    // Block 3 has long left the ring: the floor is the quietest block kept.
    expect(mic.quietFloor()).toBeCloseTo(0.05 * 1, 5);
    ctx.feed(0.003);
    expect(mic.quietFloor()).toBeCloseTo(0.003, 5);
    mic.roll(); // same stream, same context: one ring
    expect(ctx.procs).toHaveLength(1);
    mic.release();
    expect(ctx.procs[0].onaudioprocess).toBeNull();
    expect(mic.preRoll()).toBeNull();
  });

  it("never without a running context (no gesture yet), and never throws on an engine without nodes", async () => {
    const ctx = new NodeCtx();
    ctx.state = "suspended";
    const mic = new MicKeeper(60_000, async () => fakeStream(), () => ctx as unknown as AudioContext);
    await mic.get();
    expect(ctx.procs).toHaveLength(0);
    ctx.state = "running";
    mic.roll(ctx as unknown as AudioContext);
    expect(ctx.procs).toHaveLength(1);
    const bare = new MicKeeper(60_000, async () => fakeStream(), () => ({ state: "running" }) as unknown as AudioContext);
    await bare.get();
    expect(bare.preRoll()).toBeNull();
  });

  it("downsample: a box average to the lower rate; withPreRoll: the samples in front, the header fixed", async () => {
    expect(Array.from(downsample(new Float32Array([1, 3, 5, 7, 9, 11]), 48000, 16000))).toEqual([3, 9]);
    const body = new Uint8Array(44 + 4);
    const v = new DataView(body.buffer);
    [..."RIFF"].forEach((c, i) => v.setUint8(i, c.charCodeAt(0)));
    [..."data"].forEach((c, i) => v.setUint8(36 + i, c.charCodeAt(0)));
    v.setUint16(22, 1, true);
    v.setUint32(24, 16000, true);
    v.setUint16(34, 16, true);
    v.setUint32(40, 4, true);
    v.setInt16(44, 1000, true);
    v.setInt16(46, -1000, true);
    const out = new DataView(await (await withPreRoll(new Float32Array([0.5, -0.5]), new Blob([body]))).arrayBuffer());
    expect(out.byteLength).toBe(44 + 8);
    expect(out.getUint32(4, true)).toBe(36 + 8);
    expect(out.getUint32(40, true)).toBe(8);
    expect([0, 1, 2, 3].map((i) => out.getInt16(44 + 2 * i, true))).toEqual([16384, -16384, 1000, -1000]);
    // Anything it can't read goes as it was.
    const junk = new Blob([new Uint8Array(10)]);
    expect(await withPreRoll(new Float32Array([0.5]), junk)).toBe(junk);
    v.setUint32(24, 44100, true);
    const other = new Blob([body]);
    expect(await withPreRoll(new Float32Array([0.5]), other)).toBe(other);
  });

  /** A streaming server recognizer on `ctx` with a pre-roll'd microphone;
   * the recorder's blocks are driven by `block(rms)`. */
  function rig(o: { skipMs?: number } = {}) {
    vi.stubGlobal("window", { AudioContext: NodeCtx });
    // The page's one context: the recorder's and the pre-roll's.
    const ctx = voiceAudioCtx() as unknown as NodeCtx;
    ctx.state = "running";
    vi.stubGlobal("navigator", { language: "en-US", userAgent: "", mediaDevices: { getUserMedia: vi.fn() } });
    const mic = new MicKeeper(60_000, async () => fakeStream(), () => ctx as unknown as AudioContext);
    const clips: Array<{ opts: { onLevel?: (rms: number) => void; skipS?: number }; secs: number; fresh: boolean }> = [];
    mocks.startWavRecording.mockImplementation((_s: unknown, _c: unknown, opts) => {
      const clip = {
        opts,
        secs: 0,
        /** A block came since the last take (one chunk's worth). */
        fresh: false,
        take: vi.fn((min?: number) => {
          if (min === undefined || !clip.fresh) return null;
          clip.fresh = false;
          return new Blob([wavBytes(1600)], { type: "audio/wav" });
        }),
        stop: vi.fn(() => Promise.resolve(new Blob([wavBytes(1600)], { type: "audio/wav" }))),
        cancel: vi.fn(),
        mute: vi.fn(),
        seconds: () => clip.secs,
      };
      clips.push(clip);
      return clip;
    });
    const bodies: number[] = [];
    mocks.apiRaw.mockImplementation(async (url: string, init: RequestInit) => {
      if (url === "/api/voice/stream") return { ok: true, status: 200, json: async () => ({ id: "s1" }) };
      if (url.endsWith("/chunk")) {
        bodies.push((init.body as Blob).size);
        return { ok: true, status: 200, json: async () => ({ committed: "", partial: "" }) };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    });
    const h: RecognizerHandlers = { onInterim: () => {}, onFinal: () => {}, onError: () => {}, onEnd: () => {}, onReady: () => {} };
    const r = serverRecognizer({ stream: () => true, mic });
    return {
      ctx,
      mic,
      r,
      h,
      clips,
      bodies,
      start: async () => {
        await mic.get();
        for (let i = 0; i < 20; i++) ctx.feed(0.004); // the room, before the press
        r.start(h, { skipMs: o.skipMs });
        await vi.waitFor(() => expect(clips).toHaveLength(1));
      },
      /** One recorder block of `rms`, the recording `s` seconds in. */
      block: (rms: number, s: number) => {
        const c = clips[0];
        c.secs = s;
        c.fresh = true;
        c.opts.onLevel!(rms);
      },
    };
  }
  const wavBytes = (samples: number) => {
    const b = new Uint8Array(44 + samples * 2);
    const v = new DataView(b.buffer);
    [..."RIFF"].forEach((c, i) => v.setUint8(i, c.charCodeAt(0)));
    [..."data"].forEach((c, i) => v.setUint8(36 + i, c.charCodeAt(0)));
    v.setUint16(22, 1, true);
    v.setUint32(24, 16000, true);
    v.setUint16(34, 16, true);
    v.setUint32(40, samples * 2, true);
    return b;
  };
  const PRE_BYTES = (PRE_ROLL_MS / 1000) * PRE_ROLL_RATE * 2;

  it("speech from the recording's first block: its first chunk carries the pre-roll in front (once)", async () => {
    const t = rig();
    await t.start();
    t.block(0.3, 0.085); // loud at once: the word began before the recording
    t.block(0.3, 0.17);
    t.block(0.3, 0.26);
    await vi.waitFor(() => expect(t.bodies.length).toBeGreaterThanOrEqual(1));
    expect(t.bodies[0]).toBe(44 + 3200 + PRE_BYTES);
    t.block(0.3, 0.5);
    t.block(0.3, 0.9);
    await vi.waitFor(() => expect(t.bodies.length).toBeGreaterThan(1));
    // Once: every later chunk is the recording's own.
    expect(t.bodies.slice(1).every((b) => b === 44 + 3200)).toBe(true);
    t.r.stop();
  });

  it("speech that starts later has its own lead-in: no pre-roll; nor under a skip (a barge-in, the chime)", async () => {
    const t = rig();
    await t.start();
    t.block(0.004, 0.2);
    t.block(0.004, PRE_ROLL_ONSET_S + 0.05);
    t.block(0.3, 0.5);
    t.block(0.3, 0.6);
    t.block(0.3, 0.7);
    await vi.waitFor(() => expect(t.bodies).toHaveLength(1));
    expect(t.bodies[0]).toBe(44 + 3200);
    t.r.stop();
    mocks.startWavRecording.mockReset();
    const s = rig({ skipMs: 270 });
    await s.start();
    expect(s.clips[0].opts.skipS).toBeGreaterThan(0);
    s.block(0.3, 0.085);
    s.block(0.3, 0.17);
    s.block(0.3, 0.26);
    await vi.waitFor(() => expect(s.bodies).toHaveLength(1));
    expect(s.bodies[0]).toBe(44 + 3200);
    s.r.stop();
  });

  it("the silence detector's floor starts at the room's quiet: a press mid-word still hears the word as speech", async () => {
    const t = rig();
    const speech: number[] = [];
    await t.start();
    t.h.onSpeech = () => void speech.push(1);
    // Before: the first block (the word) set the floor, so it read as quiet.
    t.block(0.05, 0.085);
    expect(speech).toEqual([1]);
    t.r.stop();
  });

  it("the hotkey warms the mic like the button: the engine in the gesture, the microphone kept open", () => {
    const MIC = code("components/TopBarMic.tsx");
    const on = MIC.slice(MIC.indexOf("const onTalk = ("), MIC.indexOf("window.addEventListener(TALK_EVENT"));
    expect(on).toMatch(/if \(phase === "down" && !live\.current\) warmMic\(true\);[\s\S]*talkKeyAction\(/);
    // The meter's floor too starts from the pre-roll's quiet.
    expect(MIC).toContain("meterStart(serverRec.mic.quietFloor());");
  });
});

// --- 3. the chime on a cold AudioContext ------------------------------------------ //

describe("a start chime on a cold AudioContext waits for it instead of being dropped", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.stubGlobal("window", { AudioContext: NodeCtx });
  });

  it("whenRunning: at once when running; within the cap once resumed; false past it", async () => {
    const ctx = new NodeCtx();
    expect(await whenRunning(ctx as unknown as AudioContext)).toBe(true);
    ctx.state = "suspended";
    ctx.onResume = () => setTimeout(() => (ctx.state = "running"), 150);
    const p = whenRunning(ctx as unknown as AudioContext);
    await vi.advanceTimersByTimeAsync(200);
    expect(await p).toBe(true);
    expect(ctx.resumes).toBe(1);
    ctx.state = "suspended";
    ctx.onResume = null;
    const late = whenRunning(ctx as unknown as AudioContext);
    await vi.advanceTimersByTimeAsync(CHIME_LATE_MS + 50);
    expect(await late).toBe(false);
  });

  it("the queue's cue plays the chime once the page's context runs (its length counted from then); too late: dropped, the queue goes on", async () => {
    const ctx = voiceAudioCtx() as unknown as NodeCtx; // the page's one context
    ctx.state = "suspended";
    ctx.onResume = () => setTimeout(() => (ctx.state = "running"), 120);
    const played: number[] = [];
    const q = new SpeechQueue(() => engine().sp);
    const t0 = Date.now();
    let done = false;
    void q.cue(() => void played.push(Date.now() - t0), 200).then(() => (done = true));
    await vi.advanceTimersByTimeAsync(100);
    expect(played).toEqual([]); // not dropped: waiting
    await vi.advanceTimersByTimeAsync(60);
    expect(played).toHaveLength(1);
    expect(played[0]).toBeGreaterThanOrEqual(120);
    await vi.advanceTimersByTimeAsync(150);
    expect(done).toBe(false); // its 200 ms run from when it played
    await vi.advanceTimersByTimeAsync(100);
    expect(done).toBe(true);
    // A context that never comes back: the chime is dropped, nothing waits on it.
    ctx.state = "suspended";
    ctx.onResume = null;
    const late = q.cue(() => void played.push(-1), 200);
    await vi.advanceTimersByTimeAsync(CHIME_LATE_MS + 50);
    await late;
    expect(played).toHaveLength(1);
    expect(q.idle).toBe(true);
    ctx.state = "running";
  });

  it("the recorder's chime skip counts from the resume the chime waited on", async () => {
    vi.useRealTimers();
    const ctx = voiceAudioCtx() as unknown as NodeCtx;
    ctx.state = "suspended";
    let now = 5_000_000;
    vi.spyOn(Date, "now").mockImplementation(() => now);
    // The resume takes 200 ms (the chime waits on it too).
    let resuming: Promise<void> | null = null;
    ctx.resume = () =>
      (resuming ||= new Promise<void>((r) =>
        setTimeout(() => {
          now += 200;
          ctx.state = "running";
          r();
        }, 5)
      ));
    vi.stubGlobal("navigator", { language: "en-US", userAgent: "", mediaDevices: { getUserMedia: vi.fn() } });
    const skips: number[] = [];
    mocks.startWavRecording.mockImplementation((_s: unknown, _c: unknown, o: { skipS: number }) => {
      skips.push(o.skipS);
      return { take: () => null, stop: () => Promise.resolve(new Blob()), cancel: () => {}, seconds: () => 0 };
    });
    const r = serverRecognizer({ mic: new MicKeeper(60_000, async () => fakeStream()) });
    r.start({ onInterim: () => {}, onFinal: () => {}, onError: () => {}, onEnd: () => {} }, { skipMs: 270 });
    await vi.waitFor(() => expect(skips).toHaveLength(1));
    // Before: 270 − 200 = 70 ms skipped, the chime's tail recorded.
    expect(skips[0]).toBeCloseTo(0.27, 3);
    r.stop();
  });

  it("…and when the context ran before a slow microphone opened, from when it ran", async () => {
    vi.useRealTimers();
    const ctx = voiceAudioCtx() as unknown as NodeCtx;
    ctx.state = "suspended";
    ctx.resume = () => Promise.resolve();
    let now = 6_000_000;
    vi.spyOn(Date, "now").mockImplementation(() => now);
    vi.stubGlobal("navigator", { language: "en-US", userAgent: "", mediaDevices: { getUserMedia: vi.fn() } });
    const skips: number[] = [];
    mocks.startWavRecording.mockImplementation((_s: unknown, _c: unknown, o: { skipS: number }) => {
      skips.push(o.skipS);
      return { take: () => null, stop: () => Promise.resolve(new Blob()), cancel: () => {}, seconds: () => 0 };
    });
    let grant: (s: MediaStream) => void = () => {};
    const r = serverRecognizer({ mic: new MicKeeper(60_000, () => new Promise<MediaStream>((g) => (grant = g))) });
    r.start({ onInterim: () => {}, onFinal: () => {}, onError: () => {}, onEnd: () => {} }, { skipMs: 270 });
    // 150 ms in, the context runs (the chime plays from here)…
    now += 150;
    ctx.state = "running";
    await new Promise((res) => setTimeout(res, 60));
    // …and the microphone opens 100 ms after that: 170 ms of the chime left.
    now += 100;
    grant(fakeStream());
    await vi.waitFor(() => expect(skips).toHaveLength(1));
    expect(skips[0]).toBeCloseTo(0.17, 3);
    r.stop();
  });
});

// --- 4. "over" without stacked waits -------------------------------------------------- //

function session(): DictTarget & { keys: string[] } {
  const keys: string[] = [];
  return { kind: "session", title: "t1", name: "Login fix", keys, type: (t) => keys.push(t) > 0, erase: (t) => keys.push("⌫" + t) > 0 };
}

describe("“over”: the final is fetched while the quiet is waited out", () => {
  /** The voice's last loud moment (ms): quietMs follows the clock. */
  let loudAt = 0;
  let ends = 0;
  const sentAt: number[] = [];
  beforeEach(() => {
    vi.useFakeTimers();
    ends = 0;
    sentAt.length = 0;
    mocks.instApi.mockImplementation(async () => {
      sentAt.push(Date.now());
      return { ok: true };
    });
  });
  const hooks = (over: Partial<LiveHooks> = {}): LiveHooks => ({
    caption: () => {},
    halt: () => {},
    route: async () => false,
    activity: () => "idle",
    replies: () => false,
    submitted: () => {},
    quietMs: () => Date.now() - loudAt,
    endPhrase: () => void ends++,
    finalMs: () => 400,
    ...over,
  });

  it(`the voice stopped ${OVER_EARLY_QUIET_MS} ms: the phrase ends at once; the final back after the quiet: Enter at once`, async () => {
    const t = session();
    const d = new LiveDictation(t, hooks(), { continuous: true });
    loudAt = Date.now();
    await vi.advanceTimersByTimeAsync(OVER_EARLY_QUIET_MS);
    const seen = Date.now();
    await d.stream("", "ship it over");
    expect(ends).toBe(1); // before: only at 250 ms of quiet
    await vi.advanceTimersByTimeAsync(300); // the final's round trip
    await d.flush("", "ship it over");
    expect(sentAt).toHaveLength(1);
    expect(sentAt[0] - seen).toBe(300); // before: 150 (rest of the quiet) + 300 = 450
    expect(t.keys).toEqual(["ship it"]);
  });

  it("“do it over” (an answer's words, then “over”): nothing typed only to be taken back — the router reads “do it” first, as for any first phrase that may be a command", async () => {
    const routed: string[] = [];
    for (const [said, words] of [
      ["do it over", "do it"],
      ["go ahead over", "go ahead"],
      ["yes over", "yes"],
    ]) {
      const t = session();
      routed.length = 0;
      const d = new LiveDictation(t, hooks({ route: async (w) => (routed.push(w), false) }), { continuous: true });
      loudAt = Date.now();
      for (let i = 1; i <= said.split(" ").length; i++) await d.stream("", said.split(" ").slice(0, i).join(" "));
      await vi.advanceTimersByTimeAsync(OVER_EARLY_QUIET_MS + 300);
      const sent = d.flush("", said);
      await vi.advanceTimersByTimeAsync(SUBMIT_SETTLE_MS + 50);
      await sent;
      expect(sentAt).toHaveLength(1);
      // Before: the guess typed it ("do it over" fits no command), and the
      // send took it back for the router and typed it again.
      expect([said, t.keys]).toEqual([said, [words + " "]]);
      expect(routed).toEqual([words]);
      sentAt.length = 0;
      mocks.instApi.mockClear();
    }
  });

  it("the final back before the quiet is complete: Enter the moment the quiet is", async () => {
    const t = session();
    const d = new LiveDictation(t, hooks(), { continuous: true });
    loudAt = Date.now();
    await vi.advanceTimersByTimeAsync(OVER_EARLY_QUIET_MS);
    await d.stream("", "ship it over");
    await vi.advanceTimersByTimeAsync(50);
    const flushed = d.flush("", "ship it over");
    await vi.advanceTimersByTimeAsync(40);
    expect(sentAt).toHaveLength(0); // 190 ms of quiet: not yet a radio word
    await vi.advanceTimersByTimeAsync(CONTROL_TAIL_MS);
    await flushed;
    expect(sentAt).toHaveLength(1);
    expect(sentAt[0] - loudAt).toBeLessThanOrEqual(CONTROL_TAIL_MS + 50);
  });

  it("the voice comes back before the quiet: “over” is a plain word — typed, nothing sent, listening on", async () => {
    const t = session();
    const d = new LiveDictation(t, hooks(), { continuous: true });
    loudAt = Date.now();
    await vi.advanceTimersByTimeAsync(OVER_EARLY_QUIET_MS);
    await d.stream("", "do it over");
    expect(ends).toBe(1);
    await vi.advanceTimersByTimeAsync(60);
    loudAt = Date.now(); // "…over the weekend"
    await vi.advanceTimersByTimeAsync(300);
    await d.flush("", "do it over");
    expect(mocks.instApi).not.toHaveBeenCalled();
    expect(t.keys.join("")).toBe("do it over ");
    expect(d.active).toBe(true);
    // The rest of the sentence, and its own "over" (a normal end): sent.
    void d.flush("", "the weekend over");
    await vi.advanceTimersByTimeAsync(300);
    expect(mocks.instApi).toHaveBeenCalledTimes(1);
  });

  it("no final by the deadline: the fast words go once the quiet is known (a late final is ignored)", async () => {
    const t = session();
    const d = new LiveDictation(t, hooks({ finalMs: () => null }), { continuous: true });
    loudAt = Date.now();
    await vi.advanceTimersByTimeAsync(OVER_EARLY_QUIET_MS + 10);
    await d.stream("", "ship it over");
    await vi.advanceTimersByTimeAsync(1500);
    expect(sentAt).toHaveLength(1);
    await d.flush("", "ship it now over");
    expect(sentAt).toHaveLength(1);
  });

  it("the voice still sounding (under 100 ms of quiet): nothing is ended yet", async () => {
    const t = session();
    const d = new LiveDictation(t, hooks(), { continuous: true });
    loudAt = Date.now();
    await d.stream("", "ship it over");
    expect(ends).toBe(0);
    await vi.advanceTimersByTimeAsync(OVER_EARLY_QUIET_MS + CONTROL_TAIL_MS);
    expect(ends).toBe(1);
    d.drop();
  });
});

// --- 5. the router's phrase shows at once -------------------------------------------- //

describe("a first phrase going past the router is in the caption at once", () => {
  it("muted (the unsure tail) while the route is asked; typed only after", async () => {
    let answer: (v: false) => void = () => {};
    const caps: Array<[string, string]> = [];
    const t = session();
    const d = new LiveDictation(t, {
      caption: (line, _s, tail = "") => void caps.push([line, tail]),
      halt: () => {},
      route: () => new Promise<false>((r) => (answer = r)),
      activity: () => "idle",
      replies: () => false,
      submitted: () => {},
    });
    const p = d.heard("open the login fix");
    await vi.waitFor(() => expect(caps.at(-1)).toEqual(["", "open the login fix"]));
    expect(t.keys).toEqual([]);
    answer(false);
    await p;
    expect(t.keys).toEqual(["open the login fix "]);
  });
});

// --- 6. the talk key ------------------------------------------------------------------------ //

describe("the talk key: a hold's tap leaves the mic on; a press while it ends does nothing", () => {
  it("Hold to talk: held ≥ 350 ms then let go stops; a tap latches, and the next press stops", () => {
    expect(TAP_HOLD_MS).toBe(350);
    expect(talkKeyAction("up", true, true, true, { heldMs: TAP_HOLD_MS })).toBe("finish");
    expect(talkKeyAction("up", true, true, true, { heldMs: 120 })).toBe("latch");
    expect(talkKeyAction("down", true, true, false, { latched: true })).toBe("finish");
    // Listening the key didn't start (the button, the wake word): a press is still nothing.
    expect(talkKeyAction("down", true, true, false, { latched: false })).toBe(null);
    // No timing given: as before.
    expect(talkKeyAction("up", true, true, true)).toBe("finish");
  });

  it("busy (ending, or just ended): a press that would start does nothing — in both modes; a stop still stops", () => {
    expect(TALK_DEBOUNCE_MS).toBe(300);
    expect(talkKeyAction("down", true, false, false, { busy: true })).toBe(null);
    expect(talkKeyAction("down", false, false, false, { busy: true })).toBe(null);
    expect(talkKeyAction("down", false, true, false, { busy: true })).toBe("finish");
  });

  it("wired: the press's time, the latch, and the busy state from a finish under way or a halt just now", () => {
    const MIC = code("components/TopBarMic.tsx");
    const on = MIC.slice(MIC.indexOf("const onTalk = ("), MIC.indexOf("window.addEventListener(TALK_EVENT"));
    expect(on).toContain('heldMs: phase === "up" && keyHeld.current ? now - keyDownAt.current : undefined,');
    expect(on).toContain("busy: !!deciding.current || now - haltedAt.current < TALK_DEBOUNCE_MS,");
    expect(on).toMatch(/else if \(act === "latch"\) keyLatched\.current = true;/);
    const halt = MIC.slice(MIC.indexOf("const halt = () => {"), MIC.indexOf("const target = ("));
    expect(halt).toContain("haltedAt.current = Date.now();");
    expect(halt).toContain("keyLatched.current = false;");
    // The gated mute follows the line's sound, not its turn.
    expect(MIC).toMatch(/const a = \(on: boolean\) => \{[\s\S]*?if \(gated\.current\) rec\.current\?\.mute\?\.\(on\);/);
    expect(MIC).toContain("{ onAudible: (on) => audibleListeners.forEach((l) => l(on)) }");
  });
});

// --- 7. the judge's baseline: mic loss, double sends, the true state ---------- //

/** A stream whose one track can end (`end()`), as an unplugged mic's does. */
function endingStream() {
  const handlers: Array<() => void> = [];
  const track = {
    readyState: "live",
    stop: vi.fn(),
    addEventListener: (ev: string, cb: () => void) => void (ev === "ended" && handlers.push(cb)),
  };
  const stream = { active: true, getTracks: () => [track] } as unknown as MediaStream;
  return {
    stream,
    track,
    end: () => {
      track.readyState = "ended";
      handlers.forEach((h) => h());
    },
  };
}

describe("a microphone that goes away is noticed", () => {
  it("MicKeeper: its track ending drops the stream and tells onLost — the next get() opens a new one", async () => {
    const opened: Array<ReturnType<typeof endingStream>> = [];
    const mic = new MicKeeper(60_000, async () => {
      const e = endingStream();
      opened.push(e);
      return e.stream;
    });
    let lost = 0;
    mic.onLost(() => void lost++);
    const first = await mic.get();
    opened[0].end();
    expect(lost).toBe(1);
    expect(mic.isWarm).toBe(false);
    expect(opened[0].track.stop).toHaveBeenCalled();
    const second = await mic.get();
    expect(second).not.toBe(first);
    opened[0].end(); // the old stream again: not this one
    expect(lost).toBe(1);
  });

  it("devicechange with the open stream's track ended (no event of its own): lost too", async () => {
    let onChange: () => void = () => {};
    vi.stubGlobal("navigator", { mediaDevices: { addEventListener: (_: string, cb: () => void) => void (onChange = cb) } });
    const e = endingStream();
    const mic = new MicKeeper(60_000, async () => e.stream);
    let lost = 0;
    mic.onLost(() => void lost++);
    await mic.get();
    onChange(); // a device came or went, ours still live
    expect(lost).toBe(0);
    e.track.readyState = "ended";
    onChange();
    expect(lost).toBe(1);
  });

  function rig() {
    vi.stubGlobal("window", { AudioContext: NodeCtx });
    vi.stubGlobal("navigator", { language: "en-US", userAgent: "", mediaDevices: { getUserMedia: vi.fn() } });
    const ctx = voiceAudioCtx() as unknown as NodeCtx;
    ctx.state = "running";
    const opened: Array<ReturnType<typeof endingStream>> = [];
    const mic = new MicKeeper(60_000, async () => {
      const e = endingStream();
      opened.push(e);
      return e.stream;
    });
    const recs: Array<{ stream: unknown; stop: ReturnType<typeof vi.fn>; opts: { onLevel?: (r: number) => void } }> = [];
    mocks.startWavRecording.mockImplementation((stream: unknown, _c: unknown, opts) => {
      const rec = { stream, opts, stop: vi.fn(() => Promise.resolve(new Blob([new Uint8Array(3244)]))), cancel: vi.fn(), take: () => null, seconds: () => 1 };
      recs.push(rec);
      return rec;
    });
    mocks.apiRaw.mockResolvedValue({ ok: true, status: 200, json: async () => ({ text: "half a thought" }) });
    const ev: string[] = [];
    const h: RecognizerHandlers = {
      onInterim: () => {},
      onFinal: (t) => void ev.push("final:" + t),
      onNotice: (t) => void ev.push("notice:" + t),
      onError: (c, d) => void ev.push("error:" + c + ":" + d),
      onEnd: () => void ev.push("end"),
    };
    const r = serverRecognizer({ stream: () => false, mic });
    return { r, h, ev, opened, recs };
  }

  it("mid-listen: the phrase it was recording goes (its words typed), the next records on a new microphone", async () => {
    const t = rig();
    t.r.start(t.h);
    await vi.waitFor(() => expect(t.recs).toHaveLength(1));
    t.recs[0].opts.onLevel!(0.004);
    t.recs[0].opts.onLevel!(0.3); // speech: the clip has words
    t.opened[0].end();
    expect(t.ev).toContain("notice:" + MIC_LOST_LINE);
    await vi.waitFor(() => expect(t.recs).toHaveLength(2));
    expect(t.recs[1].stream).toBe(t.opened[1].stream);
    await vi.waitFor(() => expect(t.ev).toContain("final:half a thought"));
    expect(t.ev).not.toContain("end");
    t.r.stop();
  });

  it(`gone again within ${MIC_LOST_RETRY_MS / 1000} s: listening stops and says why — never “Listening…” over a dead mic`, async () => {
    const t = rig();
    t.r.start(t.h);
    await vi.waitFor(() => expect(t.recs).toHaveLength(1));
    t.opened[0].end();
    await vi.waitFor(() => expect(t.recs).toHaveLength(2));
    t.opened[1].end();
    expect(t.ev).toContain("error:mic-lost:" + MIC_GONE_LINE);
    await vi.waitFor(() => expect(t.ev).toContain("end"));
    expect(t.recs).toHaveLength(2);
  });
});

describe("a phrase taken by “over” is never typed and sent twice by its late end", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    mocks.instApi.mockResolvedValue({ ok: true });
  });
  const hooks = (over: Partial<LiveHooks> = {}): LiveHooks => ({
    caption: () => {},
    halt: () => {},
    route: async () => false,
    activity: () => "idle",
    replies: () => false,
    submitted: () => {},
    quietMs: () => CONTROL_TAIL_MS,
    endPhrase: () => {},
    ...over,
  });

  it("a streamed phrase's /end 8 s late (a chunk backlog): ignored — before, typed and Entered again after 5 s", async () => {
    expect(SWALLOW_MAX_MS).toBe(5000);
    expect(SWALLOW_STREAM_MAX_MS).toBeGreaterThan(45_000);
    const t = session();
    const d = new LiveDictation(t, hooks({ endsSurely: () => true }), { continuous: true });
    await d.stream("", "ship it over");
    await vi.advanceTimersByTimeAsync(1600); // no final: the fast words go
    expect(mocks.instApi).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(6400);
    await d.stream("ship it over", ""); // the backlog's late words…
    void d.flush("", "ship it over"); // …and its end
    await vi.advanceTimersByTimeAsync(500);
    expect(mocks.instApi).toHaveBeenCalledTimes(1);
    expect(t.keys).toEqual(["ship it"]);
    // The next phrase is heard as ever.
    void d.flush("", "and push it over");
    await vi.advanceTimersByTimeAsync(500);
    expect(mocks.instApi).toHaveBeenCalledTimes(2);
  });

  it("wired: the server's stream reports every end", () => {
    const MIC = code("components/TopBarMic.tsx");
    expect(MIC).toContain("endsSurely: () => r === serverRec && serverRec.streaming,");
  });
});

describe("the button tells listening from muted-while-a-line-plays", () => {
  it("a line audible while listening: the ring goes faint and dashed, the tooltip says the mic is paused", () => {
    const MIC = code("components/TopBarMic.tsx");
    expect(MIC).toMatch(/const a = \(on: boolean\) => \{\s*setSounding\(on\);/);
    expect(MIC).toContain("const paused = listening && sounding;");
    expect(MIC).toContain('paused && "muted",');
    expect(MIC).toContain("aria-label={paused ? MIC_PAUSED_LINE :");
    const css = src("components/TopBarMic.css");
    expect(css).toMatch(/\.mic-ring\.listening\.muted::after \{\s*opacity: 0\.25;\s*border-style: dashed;/);
  });
});

// --- cycle 2: what the meter did not hear is not quiet; no audio twice ---------- //

describe("“over” counts only the quiet the meter heard (cycle 2, R4)", () => {
  /** A recorder block's length at 48 kHz (4096 samples). */
  const BLOCK = 85;
  beforeEach(() => {
    vi.useFakeTimers();
    mocks.instApi.mockResolvedValue({ ok: true });
    // A fresh meter for every case: no loud block left from the last one.
    meterStart(0.002);
  });
  afterEach(() => meterStop());

  it("the timing these cases rest on: the tail is polled more often than a block comes", () => {
    expect(CONTROL_POLL_MS).toBe(50);
    expect(CONTROL_POLL_MS).toBeLessThan(BLOCK);
    expect(OVER_EARLY_QUIET_MS).toBe(100);
    expect(CONTROL_TAIL_MS).toBe(250);
  });

  /** A dictation wired to the real meter as TopBarMic wires it; `endPhrase`
   * opens a seam: no block reaches the meter until `resume()`. */
  function wired() {
    let seam = false;
    let ends = 0;
    const t = session();
    const d = new LiveDictation(
      t,
      {
        caption: () => {},
        halt: () => {},
        route: async () => false,
        activity: () => "idle",
        replies: () => false,
        submitted: () => {},
        quietMs: meterQuietMs,
        loudAt: lastLoudAt,
        endPhrase: () => {
          ends++;
          seam = true;
        },
        finalMs: () => 400,
      },
      { continuous: true }
    );
    return {
      t,
      d,
      ends: () => ends,
      resume: () => void (seam = false),
      /** `n` recorder blocks of `rms`, one every {@link BLOCK} ms (none in a seam). */
      blocks: async (rms: number, n: number) => {
        for (let i = 0; i < n; i++) {
          await vi.advanceTimersByTimeAsync(BLOCK);
          if (!seam) meterFeed(rms);
        }
      },
    };
  }

  it("a breath after “over” spanning the phrase seam: the voice is seen back, “over” is typed, nothing sent", async () => {
    const w = wired();
    await w.blocks(0.1, 3); // "move it over"
    await w.d.stream("", "move it over");
    await w.blocks(0.002, 2); // a breath: two quiet blocks heard (170 ms)…
    await vi.advanceTimersByTimeAsync(CONTROL_POLL_MS); // …the phrase ends early
    expect(w.ends()).toBe(1);
    // The seam: the next recording's first block is 200 ms away — the meter
    // hears nothing (before: that counted as quiet, 250 ms "met", sent).
    await vi.advanceTimersByTimeAsync(200);
    expect(mocks.instApi).not.toHaveBeenCalled();
    expect(meterQuietMs()).toBe(2 * BLOCK);
    w.resume();
    await w.blocks(0.1, 3); // "…to the left"
    await w.d.flush("", "move it over");
    await vi.advanceTimersByTimeAsync(1600);
    expect(mocks.instApi).not.toHaveBeenCalled();
    expect(w.t.keys.join("")).toBe("move it over ");
    expect(w.d.active).toBe(true);
    w.d.drop();
  });

  it("the voice really stopped: the first quiet block after the seam makes it the radio word — sent", async () => {
    const w = wired();
    await w.blocks(0.1, 3);
    await w.d.stream("", "ship it over");
    await w.blocks(0.002, 2);
    await vi.advanceTimersByTimeAsync(CONTROL_POLL_MS);
    expect(w.ends()).toBe(1);
    await vi.advanceTimersByTimeAsync(100);
    w.resume();
    await w.blocks(0.002, 1);
    await w.d.flush("", "ship it over");
    await vi.advanceTimersByTimeAsync(100);
    expect(mocks.instApi).toHaveBeenCalledTimes(1);
    expect(w.t.keys).toEqual(["ship it"]);
  });

  it("no early end inside a seam: quiet the meter did not hear never reaches the 100 ms", async () => {
    const w = wired();
    await w.blocks(0.1, 3);
    // The recorder is between clips (the 12 s cap, a stream's restart): no
    // block for 300 ms right after the voice — then the voice goes on.
    await w.d.stream("", "move it over");
    await vi.advanceTimersByTimeAsync(300);
    expect(w.ends()).toBe(0);
    w.d.drop();
  });

  it("wired: TopBarMic's quiet is the meter's observed quiet, and the meter's last loud block", () => {
    const MIC = code("components/TopBarMic.tsx");
    expect(MIC).toContain("quietMs: meterQuietMs,");
    expect(MIC).toContain("loudAt: lastLoudAt,");
  });
});

describe("the pre-roll never repeats audio a phrase already sent (cycle 2, R5)", () => {
  /** One ring block (2048 samples at 48 kHz) at 16 kHz. */
  const RING_BLOCK_16K = Math.floor((2048 * PRE_ROLL_RATE) / 48000);

  it("after a cut (a phrase's end): only what the ring heard since — none right at it", async () => {
    const ctx = new NodeCtx();
    const mic = new MicKeeper(60_000, async () => fakeStream(), () => ctx as unknown as AudioContext);
    await mic.get();
    for (let i = 0; i < 20; i++) ctx.feed(0.3); // the phrase, sent with its recording
    mic.cut();
    expect(mic.preRoll()).toBeNull();
    ctx.feed(0.01);
    const pre = mic.preRoll()!;
    expect(pre.length).toBe(RING_BLOCK_16K);
    expect(pre.every((v) => Math.abs(v - 0.01) < 1e-6)).toBe(true);
    // Later than the pre-roll's half second: all of it again.
    for (let i = 0; i < 20; i++) ctx.feed(0.02);
    expect(mic.preRoll()!.length).toBe((PRE_ROLL_MS / 1000) * PRE_ROLL_RATE);
  });

  it("a phrase ended early (“over”), the next one's speech from its first block: its pre-roll is the gap, not the last phrase's tail", async () => {
    vi.stubGlobal("window", { AudioContext: NodeCtx });
    const ctx = voiceAudioCtx() as unknown as NodeCtx;
    ctx.state = "running";
    vi.stubGlobal("navigator", { language: "en-US", userAgent: "", mediaDevices: { getUserMedia: vi.fn() } });
    const mic = new MicKeeper(60_000, async () => fakeStream(), () => ctx as unknown as AudioContext);
    const clips: Array<{ opts: { onLevel?: (rms: number) => void }; secs: number; fresh: boolean }> = [];
    const wav = (n: number) => {
      const b = new Uint8Array(44 + n * 2);
      const v = new DataView(b.buffer);
      [..."RIFF"].forEach((c, i) => v.setUint8(i, c.charCodeAt(0)));
      [..."data"].forEach((c, i) => v.setUint8(36 + i, c.charCodeAt(0)));
      v.setUint16(22, 1, true);
      v.setUint32(24, 16000, true);
      v.setUint16(34, 16, true);
      v.setUint32(40, n * 2, true);
      return new Blob([b], { type: "audio/wav" });
    };
    mocks.startWavRecording.mockImplementation((_s: unknown, _c: unknown, opts) => {
      const clip = {
        opts,
        secs: 0,
        fresh: false,
        take: vi.fn((min?: number) => {
          if (min === undefined || !clip.fresh) return null;
          clip.fresh = false;
          return wav(1600);
        }),
        stop: vi.fn(() => Promise.resolve(wav(0))),
        cancel: vi.fn(),
        mute: vi.fn(),
        seconds: () => clip.secs,
      };
      clips.push(clip);
      return clip;
    });
    /** Each stream's chunk sizes, by the order the streams opened. */
    const bodies: number[][] = [];
    mocks.apiRaw.mockImplementation(async (url: string, init: RequestInit) => {
      if (url === "/api/voice/stream") {
        bodies.push([]);
        return { ok: true, status: 200, json: async () => ({ id: "s" + bodies.length }) };
      }
      const m = /\/s(\d+)\/chunk$/.exec(url);
      if (m) {
        bodies[Number(m[1]) - 1].push((init.body as Blob).size);
        return { ok: true, status: 200, json: async () => ({ committed: "", partial: "" }) };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    });
    const r = serverRecognizer({ stream: () => true, mic });
    await mic.get();
    for (let i = 0; i < 20; i++) ctx.feed(0.004);
    r.start({ onInterim: () => {}, onFinal: () => {}, onError: () => {}, onEnd: () => {} });
    await vi.waitFor(() => expect(clips).toHaveLength(1));
    const block = (i: number, rms: number, s: number) => {
      clips[i].secs = s;
      clips[i].fresh = true;
      clips[i].opts.onLevel!(rms);
    };
    // Phrase 1: "…over", the ring hearing it as the recorder does.
    for (let k = 1; k <= 8; k++) {
      ctx.feed(0.3);
      block(0, 0.3, k * 0.085);
    }
    await vi.waitFor(() => expect(bodies[0]?.length).toBeGreaterThanOrEqual(1));
    r.endPhrase!();
    ctx.feed(0.004); // the breath between the two phrases
    await vi.waitFor(() => expect(clips).toHaveLength(2));
    block(1, 0.3, 0.085); // "…to the left": speech from the first block
    block(1, 0.3, 0.17);
    block(1, 0.3, 0.26);
    await vi.waitFor(() => expect(bodies[1]?.length).toBeGreaterThanOrEqual(1));
    // Before: 44 + 3200 + the whole 500 ms — phrase 1's "…ver" sent again.
    expect(bodies[1][0]).toBe(44 + 3200 + RING_BLOCK_16K * 2);
    r.stop();
  });
});

describe("the desktop asks for echo cancellation and noise suppression (cycle 2, R12)", () => {
  it("the microphone opens with both on, explicitly", async () => {
    const gum = vi.fn(async () => fakeStream());
    vi.stubGlobal("navigator", { mediaDevices: { getUserMedia: gum } });
    const mic = new MicKeeper(60_000);
    await mic.get();
    expect(gum).toHaveBeenCalledWith({ audio: { echoCancellation: true, noiseSuppression: true } });
    mic.release();
  });
});

// --- 9. R8-bis: an answer said before its dialog was there ------------------------- //

describe("a dialog is answered only by words begun after it was there (R8-bis)", () => {
  function deps(over: Partial<CommandDeps> = {}) {
    const said: string[] = [];
    const d: CommandDeps = {
      railOrder: () => ["t1", "t2"],
      focused: () => "t1",
      select: () => {},
      name: (t) => ({ t1: "Login fix", t2: "Billing" })[t] || t,
      caption: () => {},
      announce: (l) => void said.push(l),
      route: vi.fn(async () => ({ kind: "prompt" }) as RouteReply),
      approveDialogs: async () => true,
      activity: () => "clarify",
      replies: () => true,
      submitted: () => {},
      ...over,
    };
    return { d, said };
  }
  const YES: RouteReply = { kind: "answer", choice: "yes", title: "t1", dialog_id: "d1", keys: ["1"], say: "Answered yes on Login fix." };

  it("DialogSeen: the first sighting of an id holds until another (or none) is seen", () => {
    let t = 1000;
    const seen = new DialogSeen(() => t);
    seen.note("t1", "d1");
    t = 2000;
    seen.note("t1", "d1");
    expect(seen.at("t1", "d1")).toBe(1000);
    expect(seen.inTime("t1", "d1", 1000 + ANSWER_AFTER_MS)).toBe(true);
    expect(seen.inTime("t1", "d1", 1000 + ANSWER_AFTER_MS - 1)).toBe(false);
    expect(seen.inTime("t1", "d1", undefined)).toBe(false);
    expect(seen.inTime("t1", "d0", 5000)).toBe(false);
    expect(seen.body()).toEqual({ t1: { id: "d1", ms_ago: 1000 } });
    // The rows: an id when they carry it. A row asking nothing clears only a
    // sighting the rows confirmed (a row lags a sub-agent's prompt).
    seen.rows([
      { title: "t1", activity: "working", asking: null },
      { title: "t2", activity: "clarify", asking: { kind: "dialog", dialog_id: "d7" } },
      { title: "t3", activity: "clarify", asking: { kind: "dialog", dialog_id: null } },
    ]);
    expect(seen.at("t1", "d1")).toBe(1000);
    expect(seen.at("t2", "d7")).toBe(2000);
    seen.rows([{ title: "t2", activity: "idle", asking: null }]);
    expect(seen.at("t2", "d7")).toBe(0);
    seen.note("t1", null);
    seen.note("t2", "d7");
    expect(seen.body()).toEqual({ t2: { id: "d7", ms_ago: 0 } });
    // A new id is a new dialog: its own first sighting.
    t = 3000;
    seen.note("t2", "d8");
    expect(seen.at("t2", "d8")).toBe(3000);
  });

  it("the judge's case: a 'yes' begun before the page had the dialog presses nothing — the early line instead", async () => {
    const seen = new DialogSeen();
    seen.note("t1", "d1");
    const at = seen.at("t1", "d1");
    mocks.instApi.mockResolvedValue({ ok: true });
    const { d, said } = deps();
    for (const spoke of [at - 750, at, at + ANSWER_AFTER_MS - 1, undefined]) {
      await runVoiceCommand(YES, "yes", d, spoke, seen);
    }
    expect(mocks.instApi).not.toHaveBeenCalled();
    expect(said).toEqual(Array(4).fill(earlyLine("Login fix")));
    // Begun after it was there: answered.
    await runVoiceCommand(YES, "yes", d, at + ANSWER_AFTER_MS, seen);
    expect(mocks.instApi).toHaveBeenCalledWith("t1", "/answer", { json: { keys: ["1"], dialog_id: "d1", by: "user" } });
    // Answered: the next prompt — even an identical one — is a new sighting.
    expect(seen.at("t1", "d1")).toBe(0);
  });

  it("a dialog the page never had (first known from the router's own answer) is not answered — and is known from then", async () => {
    const seen = new DialogSeen();
    const { d, said } = deps();
    await runVoiceCommand({ ...YES, title: "t2", dialog_id: "d5" }, "billing yes", d, Date.now() - 100, seen);
    expect(mocks.instApi).not.toHaveBeenCalled();
    expect(said).toEqual([earlyLine("Billing")]);
    expect(seen.at("t2", "d5")).toBeGreaterThan(0);
  });

  it("the server's early: said, nothing pressed, the dialog noted", async () => {
    const seen = new DialogSeen();
    const { d, said } = deps();
    const r: RouteReply = { kind: "early", title: "t2", name: "Billing", dialog_id: "d3", say: "Billing asked just now: say your answer again.", early_ms: 420 };
    expect(await runVoiceCommand(r, "billing yes", d, Date.now(), seen)).toBe(true);
    expect(said).toEqual(["Billing asked just now: say your answer again."]);
    expect(mocks.instApi).not.toHaveBeenCalled();
    expect(seen.at("t2", "d3")).toBeGreaterThan(0);
  });

  it("an index-only answer asks again WITH the phrase's start", async () => {
    const seen = new DialogSeen();
    const route = vi.fn(async () => ({ kind: "early", title: "t2", dialog_id: "d9", say: "Billing asked just now: say your answer again." }) as RouteReply);
    const { d, said } = deps({ route });
    await runVoiceCommand({ kind: "answer", choice: "yes", index: 2 }, "session two yes", d, 12345, seen);
    expect(route).toHaveBeenCalledWith("session two yes", "t2", 12345);
    expect(said).toEqual(["Billing asked just now: say your answer again."]);
    expect(mocks.instApi).not.toHaveBeenCalled();
  });

  it("routePhrase sends when the phrase began (an age and this page's clock) and the page's sightings", async () => {
    const call = api as unknown as ReturnType<typeof vi.fn>;
    call.mockReset();
    call.mockResolvedValue({ kind: "prompt" });
    const seen = new DialogSeen();
    seen.note("t1", "d1");
    const spoke = Date.now() - 1500;
    await routePhrase("yes", "t1", undefined, spoke, seen);
    const json = call.mock.calls[0][1].json;
    expect(json.spoke_at).toBe(spoke);
    expect(json.spoke_ms_ago).toBeGreaterThanOrEqual(1500);
    expect(json.spoke_ms_ago).toBeLessThan(2500);
    expect(json.dialog_seen.t1.id).toBe("d1");
    // Without either: the body is as before.
    await routePhrase("fix it", "t1", undefined, undefined, new DialogSeen());
    expect(call.mock.calls[1][1]).toEqual({ json: { text: "fix it", session: "t1" } });
    call.mockReset();
  });

  it("LiveDictation hands the router when the phrase began: the recognizer's word, else its first words' start, else the early bound", async () => {
    const got: Array<number | undefined> = [];
    let rec = 0;
    const d = new LiveDictation(session(), {
      caption: () => {},
      halt: () => {},
      route: async (_t, at) => {
        got.push(at);
        return true;
      },
      activity: () => "idle",
      replies: () => false,
      submitted: () => {},
      spokeAt: () => rec,
    }, { continuous: true });
    const t0 = Date.now();
    // 1. the recognizer says (the phrase's end carries it).
    await d.flush("yes", undefined, t0 - 4000);
    expect(got[0]).toBe(t0 - 4000);
    // 2. streamed words first: the start noted with them (the recognizer's
    // phrase under way) — the earlier of that and the end's own word.
    rec = t0 - 900;
    await d.stream("yes", "");
    rec = t0 + 5000; // the NEXT phrase began meanwhile: not this one's
    await d.flush("", undefined);
    expect(got[1]).toBe(t0 - 900);
    // 3. nothing known: the early bound — no later than SPOKE_LEAD_MS before
    // the previous phrase's end, nor before the dictation itself began.
    rec = 0;
    const before = Date.now();
    await d.heard("no");
    expect(got[2]).toBe(Math.max(t0, got[2] as number));
    expect(got[2]).toBeLessThanOrEqual(Math.max(t0 + 50, before - SPOKE_LEAD_MS + 50));
  });

  it("wired: TopBarMic routes with the phrase's start and feeds the sightings from the rows", () => {
    const MIC = code("components/TopBarMic.tsx");
    expect(MIC).toContain("routePhrase(text, session, undefined, spokeAt, undefined, r === serverRec ? serverRec.takeHeard() : \"\")");
    expect(MIC).toContain("await routed(reply, text, session, spokeAt)");
    expect(MIC).toContain("spokeAt: () => (r === serverRec ? serverRec.spokeAt : 0)");
    expect(MIC).toContain("void d.flush(t, final, at)");
    expect(MIC).toContain("void d.heard(t, at)");
    expect(MIC).toContain("route: (t, s, at) => routePhrase(t, s, undefined, at)");
    expect(MIC).toContain('dialogSeen.rows(queryClient.getQueryData<Instance[]>(["instances"]) || [])');
    const VD = code("lib/voiceDesktop.ts");
    expect(VD).toContain("hh.onFlush?.(committed, final || undefined, c.spokeAt || undefined)");
    expect(VD).toContain("if (text) hh.onFinal(text, spokeAt || undefined)");
    expect(VD).toContain("if (d?.id) dialogSeen.note(title, d.id)");
  });
});

describe("each page opens its streams with its own client id", () => {
  it("random once per page load, sent with every POST /api/voice/stream", () => {
    expect(VOICE_CLIENT_ID).toMatch(/^d-[0-9a-z-]{8,}$/);
    expect(VOICE_CLIENT_ID.length).toBeLessThanOrEqual(64);
    // With the phrase's sentence context (voiceContext.test.ts): streamOpenBody's lang + client always.
    expect(code("lib/voiceDesktop.ts")).toContain("body: JSON.stringify(streamOpenBody(langTag(), VOICE_CLIENT_ID, ctx))");
  });
});

// --- 10. cycle 4: no typing into a dialog, fresh sightings, the recording's clock, the server's turn --- //

describe("a dialog that comes up mid-message stops the typing at once (cycle 4, MF2)", () => {
  afterEach(() => {
    dialogSeen.note("t1", null);
  });
  /** A continuous dictation into session t1 ("Login fix"); `act` its row. */
  function dictation(
    o: {
      route?: (w: string, at?: number) => Promise<boolean | "hush" | "end">;
      continuous?: boolean;
      target?: DictTarget & { keys: string[] };
      hooks?: Partial<LiveHooks>;
    } = {}
  ) {
    const t = o.target ?? session();
    const st = { act: "working", spoke: 0 };
    const routed: Array<[string, number | undefined]> = [];
    const captions: string[] = [];
    const d = new LiveDictation(
      t,
      {
        caption: (l) => void captions.push(l),
        halt: () => {},
        route: async (w, at) => {
          routed.push([w, at]);
          return o.route ? o.route(w, at) : false;
        },
        activity: () => st.act,
        replies: () => false,
        submitted: () => {},
        spokeAt: () => st.spoke,
        ...o.hooks,
      },
      { continuous: o.continuous ?? true }
    );
    return { t, st, d, routed, captions, last: () => captions[captions.length - 1] };
  }
  const backspaced = (keys: string[]) => keys.some((k) => k.startsWith("⌫"));

  it("the row goes clarify mid-phrase: not a word more is typed, the rest goes to the router, nothing is taken back", async () => {
    const x = dictation();
    await x.d.heard("please fix the login");
    x.st.spoke = Date.now() - 400; // phrase 2 began before the dialog
    await x.d.stream("and add", "");
    const before = x.t.keys.join("");
    expect(before).toBe("please fix the login and add");
    x.st.act = "clarify"; // Codex's approval came up: "y" or a digit would answer it
    await x.d.stream("a test", "");
    await x.d.stream("", "yes");
    expect(x.t.keys.join("")).toBe(before);
    await x.d.flush("", undefined);
    expect(x.t.keys.join("")).toBe(before);
    expect(backspaced(x.t.keys)).toBe(false);
    // The rest went to the router — with the phrase's own start (before the
    // dialog: the answer timing rule refuses it there).
    expect(x.routed[1]).toEqual(["a test", x.st.spoke]);
    expect(x.last()).toBe(dialogStopLine("Login fix", "a test"));
    expect(x.d.active).toBe(false);
  });

  it("a dialog only this page saw (the pop-up, /dialog: the row still says working) stops it too", async () => {
    const x = dictation();
    await x.d.heard("please fix the login");
    await x.d.stream("and add", "");
    const before = x.t.keys.join("");
    dialogSeen.note("t1", "d-subagent");
    await x.d.stream("a test", "");
    await x.d.flush("", undefined);
    expect(x.t.keys.join("")).toBe(before);
    expect(x.last()).toBe(dialogStopLine("Login fix", "a test"));
  });

  it("the router acts on the rest (an answer, the early line): nothing typed, a continuous dictation goes on", async () => {
    const x = dictation({ route: async (w) => w === "yes" });
    await x.d.heard("please fix the login");
    x.st.act = "clarify";
    await x.d.heard("yes");
    expect(x.t.keys).toEqual(["please fix the login "]);
    expect(x.routed.map((r) => r[0])).toEqual(["please fix the login", "yes"]);
    expect(x.d.active).toBe(true);
  });

  it("a correction the dialog would read as keys: no Backspace, no retype", async () => {
    const x = dictation();
    await x.d.heard("please fix the login");
    await x.d.stream("", "and ad");
    const before = x.t.keys.join("");
    x.st.act = "clarify";
    await x.d.flush("", "and add a test");
    expect(x.t.keys.join("")).toBe(before);
    expect(backspaced(x.t.keys)).toBe(false);
  });

  it("“over” with a dialog up presses no Enter; “disregard” and Escape send no Backspace", async () => {
    mocks.instApi.mockResolvedValue({ ok: true });
    const a = dictation();
    await a.d.heard("please fix the login");
    a.st.act = "clarify";
    await a.d.heard("over");
    expect(mocks.instApi).not.toHaveBeenCalled();
    expect(a.last()).toBe(dialogStopLine("Login fix", ""));
    for (const how of ["disregard", "escape"]) {
      const b = dictation();
      await b.d.heard("please fix the login");
      b.st.act = "clarify";
      if (how === "disregard") await b.d.heard("disregard");
      else await b.d.cancel();
      expect(backspaced(b.t.keys)).toBe(false);
      expect(b.last()).toBe(dialogStopLine("Login fix", ""));
      expect(b.d.active).toBe(false);
    }
  });

  it("a sighting from before the message began holds it too (cycle 5, MF2-residual: it is a live dialog until cleared); once cleared, it types", async () => {
    dialogSeen.note("t1", "d-old");
    await new Promise((r) => setTimeout(r, 5));
    const x = dictation();
    await x.d.heard("please fix the login");
    expect(x.t.keys).toEqual([]);
    expect(x.routed.map((r) => r[0])).toEqual(["please fix the login"]);
    dialogSeen.note("t1", null); // answered / a 204 / the row's edge
    const y = dictation();
    await y.d.heard("please fix the login");
    await y.d.heard("and add a test");
    expect(y.t.keys).toEqual(["please fix the login ", "and add a test "]);
  });

  it("a usage-limit menu (limit) mid-message stops it the same way", async () => {
    const x = dictation();
    await x.d.heard("please fix the login");
    x.st.act = "limit"; // "1. Wait / 2. Upgrade": a digit would pick one
    await x.d.heard("and add 2 tests");
    expect(x.t.keys).toEqual(["please fix the login "]);
    expect(x.last()).toBe(dialogStopLine("Login fix", "and add 2 tests"));
    expect(x.d.active).toBe(false);
  });

  it("a text box is never a dialog: a clarify row and a fresh sighting type on", async () => {
    const keys: string[] = [];
    const box: DictTarget & { keys: string[] } = {
      kind: "field",
      title: "t1",
      name: "Ask box",
      keys,
      type: (t) => keys.push(t) > 0,
      erase: (t) => keys.push("⌫" + t) > 0,
    };
    const x = dictation({ target: box });
    x.st.act = "clarify";
    await x.d.heard("use the staging");
    dialogSeen.note("t1", "d-fresh");
    await x.d.heard("database please");
    expect(keys).toEqual(["use the staging ", "database please "]);
    expect(x.routed).toEqual([]); // a box's words never go to the router
    expect(x.d.active).toBe(true);
  });

  it("moved to another session by “over”: a dialog seen there before the switch holds the words (cycle 5); none there, they type", async () => {
    vi.useFakeTimers();
    mocks.instApi.mockResolvedValue({ ok: true });
    const t2 = session();
    t2.title = "t2";
    t2.name = "Docs";
    const x = dictation({ hooks: { retarget: () => ({ target: t2, hint: "" }) } });
    await x.d.heard("please fix the login");
    dialogSeen.note("t2", "d-earlier"); // seen on t2 while t1's message was typed
    await vi.advanceTimersByTimeAsync(1000);
    await x.d.heard("over");
    expect(mocks.instApi).toHaveBeenCalledTimes(1); // t1's message went
    await vi.advanceTimersByTimeAsync(1000);
    await x.d.heard("now update the readme");
    expect(t2.keys).toEqual([]);
    expect(x.routed.map((r) => r[0])).toContain("now update the readme");
    dialogSeen.note("t2", null);
    const y = dictation({ hooks: { retarget: () => ({ target: t2, hint: "" }) } });
    await y.d.heard("please fix the login");
    await vi.advanceTimersByTimeAsync(1000);
    await y.d.heard("over");
    await vi.advanceTimersByTimeAsync(1000);
    await y.d.heard("now update the readme");
    expect(t2.keys).toEqual(["now update the readme "]);
    expect(y.d.active).toBe(true);
  });

  it("a guess with nothing typed yet while a dialog is up types nothing; the phrase's end takes the first-phrase path", async () => {
    const x = dictation();
    x.st.act = "clarify";
    await x.d.stream("", "yes please");
    expect(x.t.keys).toEqual([]);
    await x.d.flush("yes please", undefined);
    expect(x.t.keys).toEqual([]);
    expect(x.routed.map((r) => r[0])).toEqual(["yes please"]);
    // Nothing was typed: the dialog guard's line, not the mid-message one.
    expect(x.last()).toBe("Login fix is waiting on a dialog — answer it first");
  });

  it("the router hushes the held-back rest: the voice stops, nothing typed or taken back, no stop line", async () => {
    let hushes = 0;
    const x = dictation({ route: async (w) => (w === "stop talking" ? "hush" : false), hooks: { hush: () => void hushes++ } });
    await x.d.heard("please fix the login");
    x.st.act = "clarify";
    await x.d.heard("stop talking");
    expect(hushes).toBe(1);
    expect(x.t.keys).toEqual(["please fix the login "]);
    expect(x.captions.some((c) => c.includes("asked something"))).toBe(false);
  });

  it("the router ends listening on the held-back rest (“end”): a continuous dictation stops instead of going on", async () => {
    const x = dictation({ route: async (w) => (w === "voice off" ? "end" : false) });
    await x.d.heard("please fix the login");
    x.st.act = "clarify";
    await x.d.heard("voice off");
    expect(x.d.active).toBe(false);
    expect(x.t.keys).toEqual(["please fix the login "]);
    expect(x.captions.some((c) => c.includes("asked something"))).toBe(false);
  });

  it("“over”: a dialog that comes up during the settle before Enter presses nothing; a dictation ended there sends nothing", async () => {
    vi.useFakeTimers();
    mocks.instApi.mockResolvedValue({ ok: true });
    const a = dictation();
    await a.d.heard("please fix the login");
    const going = a.d.heard("and add a test over");
    await vi.advanceTimersByTimeAsync(SUBMIT_SETTLE_MS / 2);
    expect(a.last()).toBe("Sending to Login fix…");
    a.st.act = "clarify"; // came up while the keys settled
    await vi.advanceTimersByTimeAsync(SUBMIT_SETTLE_MS);
    await going;
    expect(mocks.instApi).not.toHaveBeenCalled();
    // The words typed before stay — none taken back.
    expect(a.t.keys.join("").trim()).toBe("please fix the login and add a test");
    expect(backspaced(a.t.keys)).toBe(false);
    expect(a.routed.map((r) => r[0])).toEqual(["please fix the login"]); // no rest: the router isn't asked
    expect(a.last()).toBe(dialogStopLine("Login fix", ""));

    const b = dictation();
    await b.d.heard("please fix the login");
    const sending = b.d.heard("and add a test over");
    await vi.advanceTimersByTimeAsync(SUBMIT_SETTLE_MS / 2);
    b.d.fail("The microphone went away");
    await vi.advanceTimersByTimeAsync(SUBMIT_SETTLE_MS);
    await sending;
    expect(mocks.instApi).not.toHaveBeenCalled();
  });

  it("a hush correcting a typed guess while a dialog is up: the voice stops, but no Backspace goes", async () => {
    let hushes = 0;
    const x = dictation({ hooks: { hush: () => void hushes++ } });
    await x.d.heard("please fix the login");
    await x.d.stream("", "please"); // a guess, typed
    await x.d.stream("please", "be quiet"); // held whole: may be a hush
    const before = x.t.keys.slice();
    expect(before[before.length - 1]).toBe("please");
    x.st.act = "clarify";
    await x.d.flush("be quiet", undefined); // a hush that would take "please" back
    expect(hushes).toBe(1);
    expect(x.t.keys).toEqual(before);
    expect(backspaced(x.t.keys)).toBe(false);
  });

  it("once the dialog is gone (row working, sighting cleared), a NEW dictation types normally", async () => {
    vi.useFakeTimers();
    const x = dictation();
    await x.d.heard("please fix the login");
    x.st.act = "clarify";
    dialogSeen.rows([{ title: "t1", activity: "clarify", asking: { kind: "dialog", dialog_id: "d1" } }]);
    await x.d.heard("and add a test");
    expect(x.last()).toBe(dialogStopLine("Login fix", "and add a test"));
    // Answered on the screen: the row leaves its dialog.
    dialogSeen.rows([{ title: "t1", activity: "working", asking: null }]);
    expect(dialogSeen.since("t1")).toBe(0);
    await vi.advanceTimersByTimeAsync(1000);
    const y = dictation();
    await y.d.heard("also add a test");
    await y.d.heard("for the logout");
    expect(y.t.keys).toEqual(["also add a test ", "for the logout "]);
    expect(y.d.active).toBe(true);
  });
});

describe("a sighting never outlives its dialog (cycle 4, S1 — the client half)", () => {
  afterEach(() => {
    dialogSeen.note("t1", null);
  });

  it("the row leaving its dialog (answered by hand) clears any sighting; a row that never showed it (a sub-agent's prompt) keeps it", () => {
    let t = 1000;
    const seen = new DialogSeen(() => t);
    seen.rows([{ title: "t1", activity: "clarify", asking: { kind: "dialog", dialog_id: null } }]);
    seen.note("t1", "d1"); // from the pop-up / GET /dialog
    seen.rows([{ title: "t1", activity: "clarify", asking: null }]);
    expect(seen.at("t1", "d1")).toBe(1000);
    seen.rows([{ title: "t1", activity: "working", asking: null }]); // clicked Yes
    expect(seen.since("t1")).toBe(0);
    // The same command asked again (the same id): a new sighting.
    t = 5000;
    seen.note("t1", "d1");
    expect(seen.inTime("t1", "d1", 4900)).toBe(false);
    expect(seen.at("t1", "d1")).toBe(5000);
    seen.rows([{ title: "t2", activity: "working" }]);
    seen.note("t2", "s1");
    seen.rows([{ title: "t2", activity: "working" }]);
    expect(seen.at("t2", "s1")).toBe(5000);
  });

  it("a /dialog that comes back empty (204) clears it; one that fails keeps it", async () => {
    const hf = (fetchDialog: () => Promise<null>) =>
      new HandsFree({
        replies: () => true,
        focused: () => "t1",
        name: () => "Login fix",
        busy: () => false,
        fetchDialog,
        approveDialogs: async () => true,
        speak: async () => {},
        caption: () => {},
        listen: async () => "missed",
      });
    dialogSeen.note("t1", "d1");
    await hf(async () => Promise.reject(new Error("down"))).dialog("t1");
    expect(dialogSeen.since("t1")).toBeGreaterThan(0);
    await hf(async () => null).dialog("t1");
    expect(dialogSeen.since("t1")).toBe(0);
  });

  it("“Already answered” (someone else got there first) clears it", async () => {
    const seen = new DialogSeen();
    seen.note("t1", "d1");
    const said: string[] = [];
    mocks.instApi.mockRejectedValue(Object.assign(new Error("409"), { status: 409, body: { dialog_answered: true } }));
    const deps: CommandDeps = {
      railOrder: () => ["t1"],
      focused: () => "t1",
      select: () => {},
      name: () => "Login fix",
      caption: () => {},
      announce: (l) => void said.push(l),
      route: vi.fn(async () => ({ kind: "prompt" }) as RouteReply),
      approveDialogs: async () => true,
      activity: () => "clarify",
      replies: () => true,
      submitted: () => {},
    };
    const YES: RouteReply = { kind: "answer", choice: "yes", title: "t1", dialog_id: "d1", keys: ["1"] };
    await runVoiceCommand(YES, "yes", deps, seen.at("t1", "d1") + ANSWER_AFTER_MS, seen);
    expect(said).toEqual(["Already answered."]);
    expect(seen.since("t1")).toBe(0);
  });

  it("an answer the server could not pin to a dialog is never typed into one (S3)", async () => {
    const said: string[] = [];
    const deps: CommandDeps = {
      railOrder: () => ["t1"],
      focused: () => "t1",
      select: () => {},
      name: () => "Login fix",
      caption: () => {},
      announce: (l) => void said.push(l),
      route: vi.fn(async () => ({ kind: "prompt" }) as RouteReply),
      approveDialogs: async () => true,
      activity: () => "clarify",
      replies: () => true,
      submitted: () => {},
    };
    mocks.instApi.mockResolvedValue({ has_question: true });
    await runVoiceCommand({ kind: "answer", choice: "number", number: 1, title: "t1" }, "one", deps, Date.now(), new DialogSeen());
    expect(said).toEqual(["Login fix is on a dialog — answer it on the screen."]);
    expect(mocks.instApi).not.toHaveBeenCalled();
  });

  it("…and with the row saying working, a sighting this page still holds (a sub-agent's prompt) refuses it too — by design", async () => {
    const said: string[] = [];
    const seen = new DialogSeen();
    seen.note("t1", "d-subagent");
    const deps: CommandDeps = {
      railOrder: () => ["t1"],
      focused: () => "t1",
      select: () => {},
      name: () => "Login fix",
      caption: () => {},
      announce: (l) => void said.push(l),
      route: vi.fn(async () => ({ kind: "prompt" }) as RouteReply),
      approveDialogs: async () => true,
      activity: () => "working",
      replies: () => true,
      submitted: () => {},
    };
    mocks.instApi.mockResolvedValue({ has_question: true });
    await runVoiceCommand({ kind: "answer", choice: "yes", title: "t1" }, "yes", deps, Date.now(), seen);
    expect(said).toEqual(["Login fix is on a dialog — answer it on the screen."]);
    expect(mocks.instApi).not.toHaveBeenCalled();
  });

  it("DialogSeen.rows edges: limit → working clears; a title's first row never clears; the same id keeps its first time; a dialog row without an id still counts as on a dialog", () => {
    let t = 1000;
    const seen = new DialogSeen(() => t);
    // A title's first row: no edge to read — the sighting stays.
    seen.note("a", "d1");
    seen.rows([{ title: "a", activity: "working" }]);
    expect(seen.since("a")).toBe(1000);
    // limit → working: the usage-limit menu is a dialog too.
    seen.rows([{ title: "b", activity: "limit" }]);
    seen.note("b", "m1");
    seen.rows([{ title: "b", activity: "working" }]);
    expect(seen.since("b")).toBe(0);
    // clarify → limit: still on a dialog — kept.
    seen.rows([{ title: "c", activity: "clarify" }]);
    seen.note("c", "x1");
    seen.rows([{ title: "c", activity: "limit" }]);
    expect(seen.since("c")).toBe(1000);
    // clarify → clarify under the same id: the first time stands.
    seen.rows([{ title: "e", activity: "clarify", asking: { kind: "dialog", dialog_id: "e1" } }]);
    t = 2000;
    seen.rows([{ title: "e", activity: "clarify", asking: { kind: "dialog", dialog_id: "e1" } }]);
    expect(seen.at("e", "e1")).toBe(1000);
    // A dialog row the server could not id: the sighting from /dialog stays,
    // and leaving that row is still the edge that clears it.
    seen.rows([{ title: "f", activity: "working" }]);
    seen.note("f", "f1");
    seen.rows([{ title: "f", activity: "clarify", asking: { kind: "dialog", dialog_id: null } }]);
    expect(seen.since("f")).toBe(2000);
    seen.rows([{ title: "f", activity: "working", asking: null }]);
    expect(seen.since("f")).toBe(0);
  });

  it("DialogSeen.since: the first time of the current dialog whatever its id; 0 once none", () => {
    let t = 100;
    const seen = new DialogSeen(() => t);
    expect(seen.since("t1")).toBe(0);
    seen.note("t1", "d1");
    t = 200;
    seen.note("t1", "d1");
    expect(seen.since("t1")).toBe(100);
    t = 300;
    seen.note("t1", "d2");
    expect(seen.since("t1")).toBe(300);
    expect(seen.at("t1", "d1")).toBe(0);
    seen.note("t1", null);
    expect(seen.since("t1")).toBe(0);
  });

  it("an empty /dialog (204) clears the sighting quietly: nothing said, nothing read out, nothing listened for", async () => {
    const speak = vi.fn(async () => {});
    const caption = vi.fn();
    const listen = vi.fn(async () => "missed" as const);
    const hf = new HandsFree({
      replies: () => true,
      focused: () => "t1",
      name: () => "Login fix",
      busy: () => false,
      fetchDialog: async () => null,
      approveDialogs: async () => true,
      speak,
      caption,
      listen,
    });
    dialogSeen.note("t1", "d1");
    await hf.dialog("t1");
    expect(dialogSeen.since("t1")).toBe(0);
    expect(speak).not.toHaveBeenCalled();
    expect(caption).not.toHaveBeenCalled();
    expect(listen).not.toHaveBeenCalled();
  });
});

describe("the server recognizer: a phrase's start on the recording's clock, its end on the server's verdict (cycle 4)", () => {
  it("RecordingClock: the earliest `now − seconds` — a late callback moves nothing; reset when the count stops following time", () => {
    const c = new RecordingClock();
    expect(c.at(1)).toBe(0);
    c.block(0.1, 10_100);
    c.block(0.2, 10_200);
    c.block(0.3, 10_900); // handed over 600 ms late (a render, an xterm flood)
    expect(c.at(0.2)).toBe(10_200);
    expect(c.at(0.3)).toBe(10_300);
    c.reset();
    expect(c.at(0)).toBe(0);
  });

  it("turnHoldMs: the verdict within 300..3000 ms; the fixed quiet without one, for stale words, and for a command once words are typed", () => {
    const v = (hold: number, reason: string, at = 100) => ({ hold, reason, at });
    expect(turnHoldMs(null, 0, true)).toBe(SILENCE_MS);
    expect(turnHoldMs(v(350, "control"), 50, false)).toBe(350);
    expect(turnHoldMs(v(350, "command"), 50, true)).toBe(350);
    expect(turnHoldMs(v(350, "command"), 50, false)).toBe(SILENCE_MS);
    expect(turnHoldMs(v(900, "sentence"), 50, false)).toBe(900);
    expect(turnHoldMs(v(1500, "connector"), 50, true)).toBe(1500);
    expect(turnHoldMs(v(100, "control"), 50, true)).toBe(TURN_MIN_MS);
    expect(turnHoldMs(v(99_000, "comma"), 50, true)).toBe(TURN_MAX_MS);
    // The voice was heard after the audio the verdict judged.
    expect(turnHoldMs(v(350, "control", 100), 101, true)).toBe(SILENCE_MS);
    expect(turnHoldMs(v(1500, "connector", 100), 101, true)).toBe(1500);
    expect(parseTurn({ p: 0.9, hold_ms: 350, reason: "control" }, 7)).toEqual({ hold: 350, reason: "control", at: 7 });
    for (const bad of [undefined, null, "x", {}, { hold_ms: "soon" }, { hold_ms: -1 }, { hold_ms: Infinity }]) expect(parseTurn(bad, 7)).toBeNull();
  });

  it("the first phrase into an empty box (routed alone): a sentence or question waits ≥ 1100 ms; a command, a radio word and a typed-into box keep theirs (cycle 5, U1)", () => {
    const v = (hold: number, reason: string, at = 100) => ({ hold, reason, at });
    expect(FIRST_PHRASE_HOLD_MS).toBe(1100);
    // "…give me what you have so far. [0.9 s] over and out": not cut at the full stop.
    expect(turnHoldMs(v(900, "sentence"), 50, true)).toBe(FIRST_PHRASE_HOLD_MS);
    expect(turnHoldMs(v(900, "question"), 50, true)).toBe(FIRST_PHRASE_HOLD_MS);
    expect(turnHoldMs(v(900, "sentence"), 50, false)).toBe(900);
    expect(turnHoldMs(v(900, "question"), 50, false)).toBe(900);
    expect(turnHoldMs(v(350, "command"), 50, true)).toBe(350);
    expect(turnHoldMs(v(350, "control"), 50, true)).toBe(350);
    expect(turnHoldMs(v(1500, "comma"), 50, true)).toBe(1500);
    expect(turnHoldMs(v(2000, "sentence"), 50, true)).toBe(2000);
    // TopBarMic's box is empty until a word of this dictation is typed.
    expect(code("components/TopBarMic.tsx")).toContain("blank: () => live.current !== d || !d.dict.typed");
  });

  /** A streaming recognizer whose blocks come every 85 ms on a hand clock;
   * each chunk answers `committed` + `turn`. */
  function rig(turn: unknown, o: { blank?: boolean; turnAt?: (n: number) => unknown } = {}) {
    vi.stubGlobal("window", { AudioContext: NodeCtx });
    const ctx = voiceAudioCtx() as unknown as NodeCtx;
    ctx.state = "running";
    vi.stubGlobal("navigator", { language: "en-US", userAgent: "", mediaDevices: { getUserMedia: vi.fn() } });
    const mic = new MicKeeper(60_000, async () => fakeStream(), () => ctx as unknown as AudioContext);
    const clock = { now: Date.now() };
    vi.spyOn(Date, "now").mockImplementation(() => clock.now);
    const clips: Array<{ opts: { onLevel?: (rms: number) => void }; secs: number; fresh: boolean; stop: ReturnType<typeof vi.fn> }> = [];
    mocks.startWavRecording.mockImplementation((_s: unknown, _c: unknown, opts) => {
      const clip = {
        opts,
        secs: 0,
        fresh: false,
        take: vi.fn(() => {
          if (!clip.fresh) return null;
          clip.fresh = false;
          return new Blob([new Uint8Array(44 + 3200)], { type: "audio/wav" });
        }),
        stop: vi.fn(() => Promise.resolve(new Blob([new Uint8Array(44)], { type: "audio/wav" }))),
        cancel: vi.fn(),
        mute: vi.fn(),
        seconds: () => clip.secs,
      };
      clips.push(clip);
      return clip;
    });
    let chunks = 0;
    mocks.apiRaw.mockImplementation(async (url: string) => {
      if (url === "/api/voice/stream") return { ok: true, status: 200, json: async () => ({ id: "s1" }) };
      if (url.endsWith("/chunk")) {
        chunks++;
        const v = o.turnAt ? o.turnAt(chunks) : turn;
        return { ok: true, status: 200, json: async () => ({ committed: "next session", partial: "", ms: 120, turn: v }) };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    });
    const r = serverRecognizer({ stream: () => true, mic });
    const h: RecognizerHandlers = { onInterim: () => {}, onFinal: () => {}, onError: () => {}, onEnd: () => {}, blank: () => o.blank ?? true };
    const settle = async () => {
      for (let i = 0; i < 5; i++) await new Promise((res) => setTimeout(res, 0));
    };
    return {
      r,
      clips,
      clock,
      chunks: () => chunks,
      start: async () => {
        await mic.get();
        for (let i = 0; i < 20; i++) ctx.feed(0.004);
        r.start(h);
        await vi.waitFor(() => expect(clips).toHaveLength(1));
        await settle();
      },
      /** One block of `rms`, handed over `late` ms after its audio ended. */
      block: async (rms: number, late = 0) => {
        const c = clips[0];
        c.secs = Math.round((c.secs + 0.085) * 1000) / 1000;
        clock.now += 85 + late;
        c.fresh = true;
        c.opts.onLevel!(rms);
        clock.now -= late;
        await settle();
      },
      ended: () => clips[0].stop.mock.calls.length > 0,
    };
  }

  it("spokeAt: the first loud block's start on the recording's clock — a stalled main thread doesn't make it later (S2)", async () => {
    const t = rig(undefined);
    await t.start();
    for (let i = 0; i < 6; i++) await t.block(0.004); // the room, 0.51 s
    const zero = t.clock.now - 510;
    await t.block(0.3, 700); // the voice, its callback 700 ms late
    // Its block began at 0.51 s on the recording: not 700 ms later.
    expect(t.r.spokeAt).toBe(zero + 510);
    t.r.stop();
  });

  it("a lone command's 350 ms verdict ends the phrase after 350 ms of quiet heard, not 1100", async () => {
    const t = rig({ p: 0.95, hold_ms: 350, reason: "command" });
    await t.start();
    for (let i = 0; i < 6; i++) await t.block(0.3);
    expect(t.chunks()).toBeGreaterThan(0);
    for (let i = 0; i < 4; i++) await t.block(0.004); // 340 ms
    expect(t.ended()).toBe(false);
    await t.block(0.004); // 425 ms
    expect(t.ended()).toBe(true);
    t.r.stop();
  });

  it("…but once words are typed it is words: the fixed quiet; no verdict: the fixed quiet; never under 300 ms", async () => {
    for (const [turn, blank, endsAt] of [
      [{ p: 0.95, hold_ms: 350, reason: "command" }, false, 13],
      [undefined, true, 13],
      [{ p: 1, hold_ms: 50, reason: "control" }, true, 4],
    ] as const) {
      const t = rig(turn, { blank });
      await t.start();
      for (let i = 0; i < 6; i++) await t.block(0.3);
      let n = 0;
      while (!t.ended() && n < 20) {
        await t.block(0.004);
        n++;
      }
      expect([turn, n]).toEqual([turn, endsAt]); // 13 × 85 = 1105 > 1100; 4 × 85 = 340 > 300
      t.r.stop();
      vi.restoreAllMocks();
      mocks.startWavRecording.mockReset();
      mocks.apiRaw.mockReset();
    }
  });

  it("parseTurn / turnHoldMs edges: a numeric string is a hold, no reason is ''; zero, NaN and junk are no verdict; the voice heard AT the judged audio is not stale", () => {
    expect(parseTurn({ hold_ms: "900" }, 5)).toEqual({ hold: 900, reason: "", at: 5 });
    for (const bad of [{ hold_ms: 0 }, { hold_ms: NaN }, { hold_ms: "9 s" }, 900, "900"]) expect(parseTurn(bad, 5)).toBeNull();
    // Strictly later: the same instant is the verdict's own audio.
    expect(turnHoldMs({ hold: 350, reason: "control", at: 100 }, 100, true)).toBe(350);
    // Only a command is overridden by typed words; a radio word still ends short.
    expect(turnHoldMs({ hold: 350, reason: "control", at: 100 }, 0, false)).toBe(350);
    expect(turnHoldMs({ hold: 350, reason: "wake", at: 100 }, 0, false)).toBe(350);
  });

  it("RecordingClock: after reset the next block re-anchors, even later than before", () => {
    const c = new RecordingClock();
    c.block(0.5, 10_500);
    expect(c.at(0)).toBe(10_000);
    c.reset();
    c.block(0.5, 13_500); // 3 s muted: the count stood still, time didn't
    expect(c.at(0.5)).toBe(13_500);
  });

  it("a later chunk without a verdict drops the last one: the phrase ends on the fixed quiet", async () => {
    // Every chunk while the voice is heard says "command, 350"; the first
    // one over the quiet comes back without a verdict.
    let quiet = false;
    const t = rig(undefined, { turnAt: () => (quiet ? undefined : { p: 0.95, hold_ms: 350, reason: "command" }) });
    await t.start();
    for (let i = 0; i < 6; i++) await t.block(0.3);
    const loudChunks = t.chunks();
    expect(loudChunks).toBeGreaterThan(0);
    quiet = true;
    let n = 0;
    while (!t.ended() && n < 20) {
      await t.block(0.004);
      n++;
    }
    expect(t.chunks()).toBeGreaterThan(loudChunks);
    expect(n).toBe(13); // 13 × 85 = 1105 > SILENCE_MS — not the stale 350
    t.r.stop();
  });

  it("a mute resets the recording's clock: speech after it is placed by the blocks after it, not the count from before", async () => {
    const t = rig(undefined);
    await t.start();
    for (let i = 0; i < 6; i++) await t.block(0.004);
    t.r.mute!(true);
    t.clock.now += 2000; // muted: no block counted, time goes on
    t.r.mute!(false);
    await t.block(0.004);
    await t.block(0.3);
    // The loud block began 85 ms before it was handed over — not 2 s earlier.
    expect(t.r.spokeAt).toBe(t.clock.now - 85);
    t.r.stop();
  });

  it("wired: TopBarMic tells the recognizer whether the message's box is still empty", () => {
    expect(code("components/TopBarMic.tsx")).toContain("blank: () => live.current !== d || !d.dict.typed");
  });
});
