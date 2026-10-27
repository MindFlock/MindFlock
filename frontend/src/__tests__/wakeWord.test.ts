/** The wake word on the desktop (lib/wakeWord.ts) and its wiring into the
 * top-bar mic.
 *
 * What is pinned: the lone-utterance detector as a pure function — a level
 * sequence in, candidate windows out: a word after quiet and before quiet is
 * one, too short (a click) and too long (a sentence) are not, nor speech
 * straight on from speech; syllables a breath apart are one word; the quiet
 * tail is waited for; the clip carries the lead-in. The gate (hidden,
 * recording, the app talking and the 500 ms after, one in flight, one per
 * 1.5 s). The standby's lifecycle with fakes: it holds the mic and records a
 * rolling window; a candidate's clip goes to the check; `wake: true` wakes,
 * false does nothing, 409 stops it with its line (and switching it on again
 * clears that); a refused mic stops it; a dictation and a hidden page pause
 * it; outside a gesture it waits for a click. POST /api/voice/wake's shape,
 * the chime, and the mic's wiring: the wake starts listening as a click does
 * (chime, "Yes?", a quiet miss after WAKE_LISTEN_MS), "mic off" turns it off,
 * the popover's switch and the button's standby look. */

import { afterEach, describe, expect, it, vi } from "vitest";
import { code, cssRule, src } from "./sourceText";

const apiRaw = vi.fn();
vi.mock("../api/client", () => ({ apiRaw: (...a: unknown[]) => apiRaw(...a) }));

const {
  CHIME_MS,
  LoneUtterance,
  WAKE_AFTER_SPEECH_MS,
  WAKE_CHECK_GAP_MS,
  WAKE_DENIED_LINE,
  WAKE_GESTURE_LINE,
  WAKE_KEEP_S,
  WAKE_LEAD_MS,
  WAKE_MAX_MS,
  WAKE_MIN_MS,
  WAKE_NO_RECOGNIZER_LINE,
  WAKE_QUIET_MS,
  WAKE_TAIL_MS,
  WakeStandby,
  chime,
  STOP_CHIME_MS,
  postWake,
  wakeGate,
  wakeLine,
  wakeStatus,
} = await import("../lib/wakeWord");
type WakeVerdict = import("../lib/wakeWord").WakeVerdict;
type WakeCandidate = import("../lib/wakeWord").WakeCandidate;

const QUIET = 0.002;
const LOUD = 0.1;
const BLOCK = 50; // ms: the thresholds fall on whole blocks

const quiet = (ms: number) => Array<number>(Math.round(ms / BLOCK)).fill(QUIET);
const loud = (ms: number) => Array<number>(Math.round(ms / BLOCK)).fill(LOUD);
/** Talking: loud words with a one-block dip between them (a sentence's
 * gaps are shorter than the quiet a lone word needs). */
const talk = (ms: number) => loud(ms).map((v, i) => (i % 5 === 4 ? QUIET : v));

/** Feed `levels`; the candidates with the block index they came at. */
function detect(levels: number[], blockMs = BLOCK): Array<WakeCandidate & { at: number }> {
  const d = new LoneUtterance();
  const out: Array<WakeCandidate & { at: number }> = [];
  levels.forEach((rms, at) => {
    const c = d.feed(rms, blockMs);
    if (c) out.push({ ...c, at });
  });
  return out;
}

describe("the thresholds", () => {
  it("are a word's: 350 ms of quiet before and 200 ms after 250 ms–1.6 s of speech, 100 ms lead-in, 1.5 s between checks, 500 ms after talking", () => {
    expect([WAKE_QUIET_MS, WAKE_TAIL_MS, WAKE_MIN_MS, WAKE_MAX_MS, WAKE_LEAD_MS]).toEqual([350, 200, 250, 1600, 100]);
    expect([WAKE_CHECK_GAP_MS, WAKE_AFTER_SPEECH_MS]).toEqual([1500, 500]);
    // The rolling window holds the longest clip there can be.
    expect(WAKE_KEEP_S * 1000).toBeGreaterThan(WAKE_LEAD_MS + WAKE_MAX_MS + WAKE_TAIL_MS + 2 * 100);
  });
});

describe("LoneUtterance: level sequence → candidate windows", () => {
  it("a word between quiets is one candidate, once its quiet tail is over", () => {
    const seq = [...quiet(1000), ...loud(600), ...quiet(1000)];
    const got = detect(seq);
    expect(got).toHaveLength(1);
    // The tail: 200 ms of quiet after the last loud block (the 4th quiet
    // block) — the candidate comes in that very block, no later.
    expect(got[0].at).toBe(20 + 12 + 4 - 1);
    expect(got[0].loudMs).toBe(600);
    // The clip: the lead-in, the word and its quiet tail — ending now.
    expect(got[0].clipMs).toBe(WAKE_LEAD_MS + 600 + 200);
    // The sound ended 200 ms before (the wake latency's start).
    expect(got[0].tailMs).toBe(200);
  });

  it("nothing before the quiet tail is complete", () => {
    expect(detect([...quiet(1000), ...loud(600), ...quiet(150)])).toEqual([]);
    expect(detect([...quiet(1000), ...loud(600), ...quiet(200)])).toHaveLength(1);
  });

  it("too short (a click, a cough) is not a candidate", () => {
    expect(detect([...quiet(1000), ...loud(200), ...quiet(1000)])).toEqual([]);
    expect(detect([...quiet(1000), ...loud(250), ...quiet(1000)])).toHaveLength(1);
  });

  it("too long is a sentence: never a candidate — and a word after it, once quiet, is", () => {
    expect(detect([...quiet(1000), ...talk(3000), ...quiet(1000)])).toEqual([]);
    expect(detect([...quiet(1000), ...loud(1600), ...quiet(1000)])).toHaveLength(1);
    expect(detect([...quiet(1000), ...loud(1700), ...quiet(1000)])).toEqual([]);
    const after = detect([...quiet(1000), ...talk(3000), ...quiet(600), ...loud(500), ...quiet(600)]);
    expect(after).toHaveLength(1);
    expect(after[0].loudMs).toBe(500);
  });

  it("speech straight on from speech is the middle of something, not a lone word", () => {
    // From the moment standby begins: no quiet before it.
    expect(detect([...loud(500), ...quiet(1000)])).toEqual([]);
    // The word a breath after a sentence ends (under the quiet a lone word needs).
    expect(detect([...quiet(1000), ...talk(2000), ...quiet(200), ...loud(500), ...quiet(1000)])).toEqual([]);
  });

  it("a steady sound doesn't fool the floor into ending early; a new steady noise is learnt after 5 s", () => {
    // Without the freeze a constant level reads as quiet after ~0.9 s.
    expect(detect([...quiet(1000), ...loud(3000), ...quiet(1000)])).toEqual([]);
    // A fan switched on: one long stretch, then the floor is the fan, and a
    // word over it (twice as loud, three floors up) is a candidate again.
    const fan = Array<number>(200).fill(0.03);
    const got = detect([...quiet(1000), ...fan, ...Array<number>(12).fill(0.2), ...Array<number>(10).fill(0.03)]);
    expect(got).toHaveLength(1);
    expect(got[0].loudMs).toBe(600);
  });

  it("syllables a breath apart are one word (\"Mind … flock\")", () => {
    const got = detect([...quiet(1000), ...loud(300), ...quiet(150), ...loud(300), ...quiet(1000)]);
    expect(got).toHaveLength(1);
    expect(got[0].loudMs).toBe(750);
  });

  it("works on a real block length (4096 frames at 48 kHz)", () => {
    const ms = (4096 / 48000) * 1000;
    const n = (t: number) => Math.round(t / ms);
    const seq = [...Array(n(1000)).fill(QUIET), ...Array(n(600)).fill(LOUD), ...Array(n(1000)).fill(QUIET)];
    const got = detect(seq, ms);
    expect(got).toHaveLength(1);
    expect(got[0].clipMs).toBeGreaterThan(WAKE_LEAD_MS + 600 + WAKE_TAIL_MS - 1);
    expect(got[0].clipMs).toBeLessThan(WAKE_LEAD_MS + 600 + WAKE_TAIL_MS + 2 * ms);
  });
});

describe("wakeGate", () => {
  const base = { now: 100_000, clipMs: 1000, visible: true, recording: false, speaking: false, lastSpoke: -Infinity, inFlight: false, lastCheck: -Infinity };
  it("passes a candidate in a quiet, visible, idle page", () => {
    expect(wakeGate(base)).toBe("ok");
  });
  it("hidden, recording, talking, in flight, too soon — in that order", () => {
    expect(wakeGate({ ...base, visible: false, recording: true })).toBe("hidden");
    expect(wakeGate({ ...base, recording: true, speaking: true })).toBe("recording");
    expect(wakeGate({ ...base, speaking: true, inFlight: true })).toBe("speaking");
    expect(wakeGate({ ...base, inFlight: true })).toBe("busy");
    expect(wakeGate({ ...base, lastCheck: base.now - WAKE_CHECK_GAP_MS + 1 })).toBe("soon");
    expect(wakeGate({ ...base, lastCheck: base.now - WAKE_CHECK_GAP_MS })).toBe("ok");
  });
  it("the app stopped talking less than 500 ms before the clip began: its own voice, skipped", () => {
    const began = base.now - base.clipMs;
    expect(wakeGate({ ...base, lastSpoke: began - WAKE_AFTER_SPEECH_MS + 1 })).toBe("speaking");
    expect(wakeGate({ ...base, lastSpoke: began - WAKE_AFTER_SPEECH_MS })).toBe("ok");
  });
});

// --- the standby, with fakes -------------------------------------------------- //

const WAV = new Blob([new Uint8Array(44 + 3200)], { type: "audio/wav" });
const flush = async () => {
  for (let i = 0; i < 8; i++) await Promise.resolve();
};

function rig(o: { verdict?: Partial<WakeVerdict>; deny?: string; warm?: boolean; check?: () => Promise<WakeVerdict> } = {}) {
  let now = 1_000_000;
  let speaking = false;
  let recording = false;
  let visible = true;
  const stream = { id: "mic" } as unknown as MediaStream;
  const mic = {
    get: vi.fn(async () => {
      if (o.deny) throw Object.assign(new Error("no"), { name: o.deny });
      return stream;
    }),
    hold: vi.fn(),
    done: vi.fn(),
    warm: vi.fn(async () => o.warm !== false),
  };
  const ctx = { state: "running", resume: vi.fn(async () => {}) } as unknown as AudioContext;
  const recs: Array<{ opts: { onLevel?: (r: number) => void; keepS?: number; targetRate?: number }; secs: number; cancel: ReturnType<typeof vi.fn>; last: ReturnType<typeof vi.fn> }> = [];
  const record = vi.fn((_s: MediaStream, _c: AudioContext, opts: { onLevel?: (r: number) => void }) => {
    const r = {
      opts,
      secs: 0,
      cancel: vi.fn(),
      last: vi.fn(async () => WAV),
      stop: vi.fn(async () => WAV),
      take: vi.fn(() => null),
      seconds: () => r.secs,
    };
    recs.push(r);
    return r;
  });
  const check = vi.fn(o.check || (async () => ({ status: 200, wake: true, text: "MindFlock", error: "", ...o.verdict })));
  const onWake = vi.fn();
  const statuses: Array<{ on: boolean; line: string }> = [];
  let gesture: (() => void) | null = null;
  const sb = new WakeStandby({
    mic,
    ctx: () => ctx,
    speaking: () => speaking,
    recording: () => recording,
    visible: () => visible,
    onWake,
    onStatus: (s) => statuses.push(s),
    onGesture: (go) => {
      gesture = go;
      return () => {
        gesture = null;
      };
    },
    record: record as never,
    check,
    now: () => now,
  });
  /** Blocks into the live recorder (the clock moves with them). */
  const feed = (levels: number[]) => {
    for (const rms of levels) {
      const r = recs[recs.length - 1];
      now += BLOCK;
      r.secs += BLOCK / 1000;
      r.opts.onLevel?.(rms);
    }
  };
  const word = () => feed([...quiet(500), ...loud(600), ...quiet(400)]);
  return {
    sb,
    mic,
    ctx,
    recs,
    record,
    check,
    onWake,
    statuses,
    feed,
    word,
    gesture: () => gesture,
    set: (k: { speaking?: boolean; recording?: boolean; visible?: boolean }) => {
      if (k.speaking !== undefined) speaking = k.speaking;
      if (k.recording !== undefined) recording = k.recording;
      if (k.visible !== undefined) visible = k.visible;
    },
    tick: (ms: number) => {
      now += ms;
    },
  };
}

describe("WakeStandby", () => {
  afterEach(() => vi.restoreAllMocks());

  it("on: holds the mic and records a rolling window at 16 kHz; a lone word's clip goes to the check and wakes", async () => {
    const r = rig();
    r.sb.set(true, true);
    await flush();
    expect(r.mic.hold).toHaveBeenCalledTimes(1);
    expect(r.record).toHaveBeenCalledTimes(1);
    expect(r.recs[0].opts).toMatchObject({ targetRate: 16000, keepS: WAKE_KEEP_S });
    expect(r.sb.on).toBe(true);
    expect(r.statuses.at(-1)).toEqual({ on: true, line: "" });
    expect(wakeStatus()).toEqual({ on: true, line: "" });
    r.word();
    await flush();
    expect(r.recs[0].last).toHaveBeenCalledTimes(1);
    expect(r.recs[0].last.mock.calls[0][0]).toBeCloseTo((WAKE_LEAD_MS + 600 + WAKE_TAIL_MS) / 1000, 6);
    expect(r.check).toHaveBeenCalledWith(WAV);
    expect(r.onWake).toHaveBeenCalledTimes(1);
    // Off: the recorder cancelled, the mic given back to the keeper's grace.
    r.sb.set(false);
    expect(r.recs[0].cancel).toHaveBeenCalled();
    expect(r.mic.done).toHaveBeenCalledTimes(1);
    expect(r.statuses.at(-1)).toEqual({ on: false, line: "" });
  });

  it("snappier: the clip goes in the block the 200 ms tail is reached, and the wake carries when the word ended", async () => {
    const r = rig();
    r.sb.set(true, true);
    await flush();
    r.feed([...quiet(500), ...loud(600), ...quiet(150)]);
    await flush();
    expect(r.recs[0].last).not.toHaveBeenCalled(); // the tail isn't over yet
    r.feed(quiet(50)); // 200 ms after the word: this very block
    expect(r.recs[0].last).toHaveBeenCalledTimes(1); // synchronously — no frame wait
    expect(r.recs[0].last.mock.calls[0][0]).toBeCloseTo((100 + 600 + 200) / 1000, 6);
    await flush();
    // The word ended 1100 ms after standby began (500 quiet + 600 loud).
    expect(r.onWake).toHaveBeenCalledWith(1_000_000 + 1100);
    expect(wakeLine(612)).toBe("wake 0.6 s");
    expect(wakeLine(null)).toBe("");
  });

  it("wake: false does nothing; a sentence never even asks", async () => {
    const r = rig({ verdict: { wake: false, text: "mind the block" } });
    r.sb.set(true, true);
    await flush();
    r.word();
    await flush();
    expect(r.check).toHaveBeenCalledTimes(1);
    expect(r.onWake).not.toHaveBeenCalled();
    expect(r.sb.on).toBe(true);
    r.tick(2000);
    r.feed([...quiet(500), ...talk(3000), ...quiet(500)]);
    await flush();
    expect(r.check).toHaveBeenCalledTimes(1);
  });

  it("409 (no recognizer): standby stops with why; switching it on again clears that and retries", async () => {
    const r = rig({ verdict: { status: 409, wake: false } });
    r.sb.set(true, true);
    await flush();
    r.word();
    await flush();
    expect(r.onWake).not.toHaveBeenCalled();
    expect(r.sb.on).toBe(false);
    expect(r.recs[0].cancel).toHaveBeenCalled();
    expect(r.mic.done).toHaveBeenCalledTimes(1);
    expect(r.statuses.at(-1)).toEqual({ on: false, line: WAKE_NO_RECOGNIZER_LINE });
    // A sync (a dictation ending, the page coming back) doesn't restart it.
    r.sb.sync();
    await flush();
    expect(r.record).toHaveBeenCalledTimes(1);
    r.sb.set(false);
    r.sb.set(true, true);
    await flush();
    expect(r.record).toHaveBeenCalledTimes(2);
    expect(r.statuses.at(-1)).toEqual({ on: true, line: "" });
  });

  it("a refused microphone stops it with why", async () => {
    const r = rig({ deny: "NotAllowedError" });
    r.sb.set(true, true);
    await flush();
    expect(r.record).not.toHaveBeenCalled();
    expect(r.statuses.at(-1)).toEqual({ on: false, line: WAKE_DENIED_LINE });
    expect(r.mic.done).toHaveBeenCalledTimes(1); // the hold it took, given back
  });

  it("the app talking — and the 500 ms after — never checks (its own voice)", async () => {
    const r = rig();
    r.sb.set(true, true);
    await flush();
    r.set({ speaking: true });
    r.word();
    await flush();
    expect(r.check).not.toHaveBeenCalled();
    // Stopped talking 300 ms before a word began: still skipped.
    r.feed(quiet(500));
    r.set({ speaking: false });
    r.feed([...quiet(300), ...loud(600), ...quiet(400)]);
    await flush();
    expect(r.check).not.toHaveBeenCalled();
    // Long enough after: checked.
    r.feed(quiet(1000));
    r.word();
    await flush();
    expect(r.check).toHaveBeenCalledTimes(1);
  });

  it("one check in flight, and at most one per 1.5 s", async () => {
    let answer: (v: WakeVerdict) => void = () => {};
    const r = rig({ check: () => new Promise<WakeVerdict>((res) => (answer = res)) });
    r.sb.set(true, true);
    await flush();
    r.word();
    await flush();
    expect(r.check).toHaveBeenCalledTimes(1);
    r.word(); // in flight (and too soon)
    r.tick(5000);
    r.word(); // still in flight
    await flush();
    expect(r.check).toHaveBeenCalledTimes(1);
    answer({ status: 200, wake: false, text: "", error: "" });
    await flush();
    r.tick(WAKE_CHECK_GAP_MS);
    r.word();
    await flush();
    expect(r.check).toHaveBeenCalledTimes(2);
    // Two words inside 1.5 s: the second waits.
    answer({ status: 200, wake: false, text: "", error: "" });
    await flush();
    r.feed([...loud(300), ...quiet(400)]);
    await flush();
    expect(r.check).toHaveBeenCalledTimes(2);
  });

  it("a dictation pauses it (the tap stops, the mic goes back to the dictation's hold) and its end resumes it", async () => {
    const r = rig();
    r.sb.set(true, true);
    await flush();
    r.set({ recording: true });
    r.sb.sync();
    expect(r.recs[0].cancel).toHaveBeenCalled();
    expect(r.mic.done).toHaveBeenCalledTimes(1);
    expect(r.sb.on).toBe(false);
    r.set({ recording: false });
    r.sb.sync();
    await flush();
    expect(r.record).toHaveBeenCalledTimes(2);
    expect(r.sb.on).toBe(true);
  });

  it("a wake while a dictation started meanwhile is dropped", async () => {
    let answer: (v: WakeVerdict) => void = () => {};
    const r = rig({ check: () => new Promise<WakeVerdict>((res) => (answer = res)) });
    r.sb.set(true, true);
    await flush();
    r.word();
    await flush();
    r.set({ recording: true });
    answer({ status: 200, wake: true, text: "MindFlock", error: "" });
    await flush();
    expect(r.onWake).not.toHaveBeenCalled();
  });

  it("a hidden page pauses it and gives the mic back; visible again resumes it", async () => {
    const r = rig();
    r.sb.set(true, true);
    await flush();
    r.set({ visible: false });
    r.sb.sync();
    expect(r.sb.on).toBe(false);
    expect(r.mic.done).toHaveBeenCalledTimes(1);
    r.set({ visible: true });
    r.sb.sync();
    await flush();
    expect(r.sb.on).toBe(true);
    expect(r.mic.hold).toHaveBeenCalledTimes(2);
  });

  it("outside a gesture without the permission (or a running engine): waits for the next click, never prompts", async () => {
    const r = rig({ warm: false });
    r.sb.set(true);
    await flush();
    expect(r.mic.get).not.toHaveBeenCalled();
    expect(r.record).not.toHaveBeenCalled();
    expect(r.statuses.at(-1)).toEqual({ on: false, line: WAKE_GESTURE_LINE });
    r.gesture()?.();
    await flush();
    expect(r.mic.get).toHaveBeenCalledTimes(1);
    expect(r.sb.on).toBe(true);
  });
});

describe("POST /api/voice/wake", () => {
  afterEach(() => apiRaw.mockReset());
  const resp = (status: number, body: unknown) => ({ ok: status < 300, status, json: async () => body });

  it("sends the clip as audio/wav with the language, and reads the verdict", async () => {
    apiRaw.mockResolvedValueOnce(resp(200, { wake: true, text: "MindFlock.", word: "mindflock" }));
    const v = await postWake(WAV, "en-US");
    const [path, init] = apiRaw.mock.calls[0];
    expect(path).toBe("/api/voice/wake?lang=en-US");
    expect(init).toMatchObject({ method: "POST", headers: { "Content-Type": "audio/wav" }, body: WAV });
    expect(v).toEqual({ status: 200, wake: true, text: "MindFlock.", error: "" });
  });

  it("409 is a status, a failure no answer — never a throw", async () => {
    apiRaw.mockResolvedValueOnce(resp(409, { error: "no recognizer" }));
    expect(await postWake(WAV, "en")).toEqual({ status: 409, wake: false, text: "", error: "no recognizer" });
    apiRaw.mockRejectedValueOnce(new Error("offline"));
    expect(await postWake(WAV, "en")).toMatchObject({ status: 0, wake: false });
  });
});

describe("the chime", () => {
  it("two rising tones through the page's context, ~CHIME_MS, quiet — and a falling, softer stop", () => {
    const freq: number[] = [];
    const ramps: number[] = [];
    let stopAt = 0;
    const param = () => ({
      setValueAtTime: (v: number) => freq.push(v),
      exponentialRampToValueAtTime: (v: number) => ramps.push(v),
    });
    const node = () => ({ connect: vi.fn() });
    const ctx = {
      state: "running",
      currentTime: 10,
      destination: {},
      createOscillator: () => ({ ...node(), type: "", frequency: param(), start: vi.fn(), stop: (t: number) => (stopAt = t) }),
      createGain: () => ({ ...node(), gain: param() }),
    } as unknown as AudioContext;
    chime(ctx);
    expect(freq.slice(0, 2)).toEqual([660, 990]);
    expect(Math.max(...ramps)).toBeLessThanOrEqual(0.15);
    expect(stopAt - 10).toBeGreaterThan(CHIME_MS / 1000 - 0.001);
    expect(stopAt - 10).toBeLessThan(CHIME_MS / 1000 + 0.05);
    freq.length = 0;
    ramps.length = 0;
    chime(ctx, "stop");
    expect(freq.slice(0, 2)).toEqual([880, 660]);
    expect(Math.max(...ramps)).toBeLessThan(0.12);
    expect(stopAt - 10).toBeGreaterThan(STOP_CHIME_MS / 1000 - 0.001);
    expect(stopAt - 10).toBeLessThan(STOP_CHIME_MS / 1000 + 0.05);
  });
  it("no engine, a suspended one or a throwing one: silent, never a throw", () => {
    expect(() => chime(null)).not.toThrow();
    expect(() => chime({ state: "suspended" } as unknown as AudioContext)).not.toThrow();
    expect(() => chime({ state: "running", createOscillator: () => { throw new Error("x"); } } as unknown as AudioContext)).not.toThrow();
  });
});

describe("the mic's wiring (components/TopBarMic.tsx)", () => {
  const MIC = code("components/TopBarMic.tsx");

  it("a wake starts listening at once, as a click does, the chime in the same moment (skipped), a quiet miss after WAKE_LISTEN_MS", () => {
    // start() first — no delay before it — then the chime, synchronously.
    expect(MIC).toMatch(
      /const woken = \(soundEnd: number\) => \{\s*if \(live\.current\) return;\s*const d = start\(textField\(document\.activeElement\), \{ hint: WAKE_HINT, idle: false \}\);\s*if \(!d\) return;\s*wakeFrom = \{ d, at: soundEnd \};/
    );
    // start() chimes (every start does), its sound skipped from the recording.
    expect(MIC).toMatch(/sound\("start"\);\s*chimed\.current = true;/);
    expect(MIC).toMatch(/const chimeSkip = prefsRef\.current\.sounds !== false \? CHIME_MS \+ 30 : 0;/);
    expect(MIC).toContain("onWake: (soundEnd) => latestWake.current(soundEnd),");
    expect(MIC).toMatch(/if \(live\.current === d && !d\.heardAnything\) d\.drop\(\);\s*\}, WAKE_LISTEN_MS\);/);
    // start() honours the extra skip next to the barge-in's.
    expect(MIC).toMatch(/const skipMs = Math\.max\(bargeIn\([^;]*\), opts\.skipMs \|\| 0\);/);
  });

  it("standby reads the queue and the dictation, and follows the pref, a dictation and the page's visibility", () => {
    expect(MIC).toMatch(/new WakeStandby\(\{\s*mic: serverRec\.mic,\s*ctx: voiceAudioCtx,\s*speaking: \(\) => queue\.busy,\s*recording: \(\) => !!live\.current,/);
    expect(MIC).toMatch(/standby\.current\?\.set\(!!prefs\.wake\);\s*\}, \[prefs\.wake\]\);/);
    expect(MIC).toMatch(/standby\.current\?\.sync\(\);\s*\}, \[listening\]\);/);
    expect(MIC).toMatch(/document\.addEventListener\("visibilitychange", onVis\)/);
  });

  it("\"mic off\" turns standby off (and the switch); the switch flips it in the click", () => {
    expect(MIC).toMatch(/case "off":[\s\S]*?setPrefs\(\{ \.\.\.prefsRef\.current, wake: false \}\);\s*standby\.current\?\.set\(false\);/);
    expect(MIC).toMatch(/const setWakePref = \(on: boolean\) => \{\s*if \(on\) voiceAudioCtx\(\);\s*standby\.current\?\.set\(on, true\);/);
  });

  it("the popover has the switch and its hint; the button shows standby, quietly", () => {
    const tsx = src("components/TopBarMic.tsx");
    expect(tsx).toMatch(/id="mic-wake"\s*checked=\{!!prefs\.wake\}\s*onChange=\{\(e\) => setWakePref\(e\.target\.checked\)\}/);
    expect(tsx).toContain('<span className="mic-pop-label">Listen for “MindFlock”</span>');
    // The hint lives in Settings → Voice; why standby stopped is the row's tooltip.
    expect(src("components/settings/screens/Voice.tsx")).toContain("{WAKE_SWITCH_HINT}");
    expect(tsx).toContain('title={(prefs.wake && wake.line) || ""}');
    expect(MIC).toContain('wake.on && !listening && "standby"');
    // Standby: a small still grey dot — no ring, no animation.
    const css = src("components/TopBarMic.css");
    expect(css).not.toContain(".mic-ring.standby");
    expect(cssRule(css, "#mic-btn.speaking::before")).toContain("var(--accent)");
    expect(css).toMatch(/#mic-btn\.standby::before,\s*#mic-btn\.speaking::before \{[^}]*border-radius: 50%;[^}]*background: var\(--muted\);/);
  });

  it("Settings search finds Voice for the wake word", async () => {
    const { searchSettings } = await import("../components/palette/settingsSearch");
    for (const q of ["wake word", "hey mindflock", "standby"]) expect(searchSettings(q)[0]?.key).toBe("voice");
  });
});
