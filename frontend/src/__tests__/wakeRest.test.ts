/** The wake word and a command in one breath (contract 1 of the voice-command
 * polish round), and the standby → listening handoff (lib/wakeWord).
 *
 *  - A lone utterance may run to WAKE_MAX_MS (4 s): "MindFlock, next
 *    session" said in one go is a candidate, and POST /api/voice/wake's
 *    `rest` (the words after the word) goes to onWake as its OPTIONAL
 *    second argument; absent or "" — the word alone — it is never passed.
 *  - With `rest` the clip's audio is NOT handed over (heard twice); without
 *    it, what came after the word (a command said over the check) is: the
 *    standby marks the word's end on the microphone (MicKeeper.markLead) —
 *    the recognizer takes it as listening starts (voiceStartCapture.test).
 *  - Longer clips mean more checks from ordinary talk: while a check is in
 *    flight a candidate longer than the word alone (WAKE_WORD_MAX_MS) is not
 *    kept; the 350 ms quiet-before rule stands.
 *  - Standby is deaf while the app is HEARD (and 500 ms after), not while a
 *    reply is still being synthesised (silence). */
import { afterEach, describe, expect, it, vi } from "vitest";
import { code } from "./sourceText";

const apiRaw = vi.fn();
vi.mock("../api/client", () => ({ apiRaw: (...a: unknown[]) => apiRaw(...a) }));

const { LoneUtterance, WAKE_KEEP_S, WAKE_LEAD_MS, WAKE_MAX_MS, WAKE_TAIL_MS, WAKE_WORD_MAX_MS, WakeStandby, postWake } = await import(
  "../lib/wakeWord"
);
type WakeVerdict = import("../lib/wakeWord").WakeVerdict;

const QUIET = 0.002;
const LOUD = 0.1;
const BLOCK = 50;
const quiet = (ms: number) => Array<number>(Math.round(ms / BLOCK)).fill(QUIET);
const loud = (ms: number) => Array<number>(Math.round(ms / BLOCK)).fill(LOUD);
const WAV = new Blob([new Uint8Array(44)], { type: "audio/wav" });
const flush = async () => {
  for (let i = 0; i < 8; i++) await Promise.resolve();
};

function detect(levels: number[]) {
  const d = new LoneUtterance();
  return levels.map((rms) => d.feed(rms, BLOCK)).filter(Boolean);
}

function rig(check: () => Promise<WakeVerdict>) {
  let now = 1_000_000;
  let speaking = false;
  const mic = {
    get: vi.fn(async () => ({ id: "mic" }) as unknown as MediaStream),
    hold: vi.fn(),
    done: vi.fn(),
    warm: vi.fn(async () => true),
    markLead: vi.fn(),
  };
  const ctx = { state: "running", resume: vi.fn(async () => {}) } as unknown as AudioContext;
  const recs: Array<{ opts: { onLevel?: (r: number) => void }; secs: number; last: ReturnType<typeof vi.fn> }> = [];
  const record = vi.fn((_s: MediaStream, _c: AudioContext, opts: { onLevel?: (r: number) => void }) => {
    const r = { opts, secs: 0, cancel: vi.fn(), last: vi.fn(async () => WAV), stop: vi.fn(async () => WAV), take: vi.fn(() => null), seconds: () => r.secs };
    recs.push(r);
    return r;
  });
  const checkFn = vi.fn(check);
  const onWake = vi.fn();
  const sb = new WakeStandby({
    mic,
    ctx: () => ctx,
    speaking: () => speaking,
    recording: () => false,
    visible: () => true,
    onWake,
    record: record as never,
    check: checkFn,
    now: () => now,
  });
  const feed = (levels: number[]) => {
    for (const rms of levels) {
      const r = recs[recs.length - 1];
      now += BLOCK;
      r.secs += BLOCK / 1000;
      r.opts.onLevel?.(rms);
    }
  };
  return { sb, mic, recs, check: checkFn, onWake, feed, now: () => now, speak: (on: boolean) => (speaking = on) };
}
const yes = (rest?: string): WakeVerdict => ({ status: 200, wake: true, text: "MindFlock", error: "", ...(rest === undefined ? {} : { rest }) });

describe("one breath: the word and a command, up to WAKE_MAX_MS", () => {
  it("\"MindFlock, next session\" (2.5 s, no pause) is one candidate — before, over 1.6 s it was a sentence and never sent", () => {
    expect(WAKE_MAX_MS).toBe(4000);
    expect(WAKE_WORD_MAX_MS).toBe(1600);
    const got = detect([...quiet(1000), ...loud(2500), ...quiet(400)]);
    expect(got).toHaveLength(1);
    expect(got[0]!.loudMs).toBe(2500);
    expect(got[0]!.clipMs).toBe(WAKE_LEAD_MS + 2500 + WAKE_TAIL_MS);
    // The rolling window holds the longest clip there can be.
    expect(WAKE_KEEP_S * 1000).toBeGreaterThan(WAKE_LEAD_MS + WAKE_MAX_MS + WAKE_TAIL_MS + 2 * 100);
    // Ordinary talk without the quiet before it is still never a candidate.
    expect(detect([...loud(2500), ...quiet(400)])).toEqual([]);
  });

  it("POST /api/voice/wake reads `rest` (trimmed); an older server's answer has none", async () => {
    const resp = (body: unknown) => ({ ok: true, status: 200, json: async () => body });
    apiRaw.mockResolvedValueOnce(resp({ wake: true, text: "MindFlock, next session.", rest: " next session " }));
    expect(await postWake(WAV, "en")).toMatchObject({ wake: true, rest: "next session" });
    apiRaw.mockResolvedValueOnce(resp({ wake: true, text: "MindFlock." }));
    expect((await postWake(WAV, "en")).rest).toBe("");
    apiRaw.mockReset();
  });
});

describe("the wake callback and the handoff", () => {
  afterEach(() => vi.restoreAllMocks());

  it("with `rest`: onWake(soundEnd, rest) — and the audio is NOT handed over as a lead-in (heard twice)", async () => {
    const r = rig(async () => yes("next session"));
    r.sb.set(true, true);
    await flush();
    r.feed([...quiet(500), ...loud(2000), ...quiet(250)]);
    await flush();
    expect(r.onWake).toHaveBeenCalledTimes(1);
    expect(r.onWake.mock.calls[0]).toEqual([1_000_000 + 2500, "next session"]);
    expect(r.mic.markLead).not.toHaveBeenCalled();
  });

  it("the word alone: onWake(soundEnd) — no second argument — and the word's end is marked for the new recording's lead-in", async () => {
    for (const verdict of [yes(), yes(""), yes("  ")]) {
      const r = rig(async () => verdict);
      r.sb.set(true, true);
      await flush();
      r.feed([...quiet(500), ...loud(600), ...quiet(250)]);
      await flush();
      expect(r.onWake.mock.calls).toEqual([[1_000_000 + 1100]]);
      expect(r.mic.markLead.mock.calls).toEqual([[1_000_000 + 1100]]);
      // Marked before listening starts (the recognizer takes it in start()).
      expect(r.mic.markLead.mock.invocationCallOrder[0]).toBeLessThan(r.onWake.mock.invocationCallOrder[0]);
    }
  });

  it("a no: nothing marked, nothing woken", async () => {
    const r = rig(async () => ({ status: 200, wake: false, text: "mind the block", error: "" }));
    r.sb.set(true, true);
    await flush();
    r.feed([...quiet(500), ...loud(600), ...quiet(250)]);
    await flush();
    expect(r.onWake).not.toHaveBeenCalled();
    expect(r.mic.markLead).not.toHaveBeenCalled();
  });
});

describe("more checks from ordinary talk: a long clip waits for nothing", () => {
  it("while a check is in flight a candidate longer than the word alone is not kept; a word-sized one is (and goes on the no)", async () => {
    let answer: (v: WakeVerdict) => void = () => {};
    const r = rig(() => new Promise<WakeVerdict>((ok) => (answer = ok)));
    r.sb.set(true, true);
    await flush();
    r.feed([...quiet(500), ...loud(600), ...quiet(250)]);
    await flush();
    expect(r.check).toHaveBeenCalledTimes(1);
    // Ordinary talk (2 s) while it is out: not kept.
    r.feed([...quiet(400), ...loud(2000), ...quiet(250)]);
    answer({ status: 200, wake: false, text: "", error: "" });
    await flush();
    r.feed(quiet(100));
    await flush();
    expect(r.check).toHaveBeenCalledTimes(1);
    // A word-sized one meanwhile is kept and goes on the no.
    r.feed([...quiet(500), ...loud(600), ...quiet(250)]);
    await flush();
    expect(r.check).toHaveBeenCalledTimes(2);
    r.feed([...quiet(400), ...loud(700), ...quiet(250)]);
    answer({ status: 200, wake: false, text: "", error: "" });
    await flush();
    expect(r.check).toHaveBeenCalledTimes(3);
  });

  it("not in flight: a long candidate is checked (it may be the word and a command)", async () => {
    const r = rig(async () => ({ status: 200, wake: false, text: "", error: "" }));
    r.sb.set(true, true);
    await flush();
    r.feed([...quiet(500), ...loud(3000), ...quiet(250)]);
    await flush();
    expect(r.check).toHaveBeenCalledTimes(1);
  });
});

describe("standby hears while a reply is synthesised (silence), not while the app is heard", () => {
  it("the mic wires the standby to the queue's AUDIBLE line, and passes `rest` through to the wake handler", () => {
    const MIC = code("components/TopBarMic.tsx");
    expect(MIC).toMatch(/new WakeStandby\(\{[\s\S]*?speaking: \(\) => queue\.audibleNow,/);
    expect(MIC).not.toMatch(/speaking: \(\) => queue\.busy/);
    expect(MIC).toContain("onWake: (soundEnd: number, rest?: string) => latestWake.current(soundEnd, rest),");
    expect(MIC).toContain("useRef<(soundEnd: number, rest?: string) => void>(woken)");
  });

  it("a word said while nothing is heard is checked; while heard (and 500 ms after) it is not", async () => {
    const r = rig(async () => ({ status: 200, wake: false, text: "", error: "" }));
    r.sb.set(true, true);
    await flush();
    r.speak(true);
    r.feed([...quiet(500), ...loud(600), ...quiet(250)]);
    await flush();
    expect(r.check).not.toHaveBeenCalled();
    r.speak(false);
    r.feed(quiet(600));
    r.feed([...quiet(500), ...loud(600), ...quiet(250)]);
    await flush();
    expect(r.check).toHaveBeenCalledTimes(1);
  });
});
