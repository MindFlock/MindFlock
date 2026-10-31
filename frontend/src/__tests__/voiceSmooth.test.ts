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
 *     listening from "muted while a line plays". */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  CHIME_LATE_MS,
  CONTROL_TAIL_MS,
  MIC_GONE_LINE,
  MIC_LOST_LINE,
  MIC_LOST_RETRY_MS,
  SWALLOW_MAX_MS,
  SWALLOW_STREAM_MAX_MS,
  Confirmer,
  LiveDictation,
  OVER_EARLY_QUIET_MS,
  SpeechQueue,
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
import { MicKeeper, PRE_ROLL_MS, PRE_ROLL_ONSET_S, PRE_ROLL_RATE, downsample, withPreRoll } from "../lib/voiceMic";
import { code, src } from "./sourceText";

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
