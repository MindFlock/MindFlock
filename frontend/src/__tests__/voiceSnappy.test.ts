/** The desktop mic, snappy — the delay chains cut, each pinned:
 *  1. "over" waits for /end's accurate final only as long as finals have
 *     been taking (overWaitMs from the FinalClock — the server's own
 *     final_ms seeds it, the measured round trips refine it), never longer
 *     than OVER_FINAL_WAIT_MS; a late final never holds the fast words;
 *  2. a send types no lone trailing space before Enter, so nothing settles
 *     (no SUBMIT_SETTLE_MS) when the final matched the fast words;
 *  3. letting go waits for a streamed phrase's final the same budget, not
 *     45 s (finishWaitMs); a clip still waits for its transcript;
 *  4. barge-in by voice: the server recognizer, muted while a line plays,
 *     hears a voice over it (BargeDetector on an analyser) and says so; the
 *     mute lifted for it keeps the recorder's tail; the queue's next line
 *     waits for the person's words (midSentence after bargedIn);
 *  5. replies: the server speaker asks for the first sentence alone, then
 *     packs short sentences into one request (speechRequests) — measured
 *     against Voice Lab: synthesis ≈ max(1.4 s, 0.52 × the audio);
 *  6. the chime's recording skip covers the breath the cue waits
 *     (SpeechQueue.cueLead); a confirmation playing is known (confirming);
 *  7. the wiring in TopBarMic (source pins). */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  BARGE_FRESH_MS,
  BARGE_POLLS,
  BARGE_POLL_MS,
  BARGE_RMS,
  BARGE_SKIP_MS,
  BargeDetector,
  CLIP_FINISH_WAIT_MS,
  CONTROL_TAIL_MS,
  CUE_GAP_MS,
  FINAL_ROUND_TRIP_MS,
  FinalClock,
  HOLD_FINAL_WAIT_MS,
  LiveDictation,
  MID_SENTENCE_MS,
  OVER_FINAL_MIN_MS,
  OVER_FINAL_WAIT_MS,
  SPEAK_REQUEST_MIN_S,
  SPEECH_CHARS_PER_S,
  SUBMIT_SETTLE_MS,
  SpeechQueue,
  finalClock,
  finishWaitMs,
  overWaitMs,
  serverRecognizer,
  serverSpeaker,
  speechRequests,
  speechSentences,
  type DictTarget,
  type LiveHooks,
  type RecognizerHandlers,
  type Speaker,
} from "../lib/voiceDesktop";
import { RMS_SPEECH } from "../lib/voiceMic";
import { code } from "./sourceText";

const mocks = vi.hoisted(() => ({
  api: vi.fn(),
  apiRaw: vi.fn(),
  instApi: vi.fn(),
  peekTerm: vi.fn(),
  startWavRecording: vi.fn(),
}));
vi.mock("../api/client", () => ({ api: mocks.api, apiRaw: mocks.apiRaw, instApi: mocks.instApi, ApiError: Error }));
vi.mock("../lib/terminals", () => ({ peekTerm: mocks.peekTerm }));
vi.mock("../lib/wavRecorder", () => ({ startWavRecording: mocks.startWavRecording, resamplerName: () => "browser resampler" }));

/** The page's one AudioContext (lib/voiceDesktop keeps the first it makes),
 * for the recognizer's analyser (`rms`) and the speaker's playback
 * (`starts`), clocked by the fake timers from `T0`. */
let rms = 0.003;
let T0 = 0;
const starts: Array<{ text: string; at: number }> = [];
class Ctx {
  state = "running";
  sampleRate = 48000;
  destination = {};
  get currentTime() {
    return (Date.now() - T0) / 1000;
  }
  resume() {
    return Promise.resolve();
  }
  createMediaStreamSource() {
    return { connect() {}, disconnect() {} };
  }
  createAnalyser() {
    return {
      fftSize: 2048,
      getFloatTimeDomainData(buf: Float32Array) {
        buf.fill(rms);
      },
    };
  }
  decodeAudioData(b: { text: string; dur: number }) {
    return Promise.resolve({ duration: b.dur, text: b.text });
  }
  createBufferSource() {
    // eslint-disable-next-line @typescript-eslint/no-this-alias
    const ctx = this;
    const node = {
      buffer: null as null | { duration: number; text: string },
      onended: null as null | (() => void),
      connect() {},
      start(at: number) {
        starts.push({ text: node.buffer!.text, at });
        setTimeout(() => node.onended?.(), (Math.max(at, ctx.currentTime) - ctx.currentTime + node.buffer!.duration) * 1000);
      },
      stop() {},
    };
    return node;
  }
}

beforeEach(() => {
  vi.stubGlobal("window", { AudioContext: Ctx });
  rms = 0.003;
  starts.length = 0;
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  mocks.apiRaw.mockReset();
  mocks.instApi.mockReset();
  mocks.startWavRecording.mockReset();
  finalClock.reset();
});

function session(title = "t1", name = "Login fix"): DictTarget & { keys: string[] } {
  const keys: string[] = [];
  return { kind: "session", title, name, keys, type: (t) => keys.push(t) > 0, erase: (t) => keys.push("⌫" + t) > 0 };
}

function hooks(over: Partial<LiveHooks> = {}) {
  const said: string[] = [];
  let ends = 0;
  const h: LiveHooks = {
    caption: () => {},
    halt: () => {},
    route: async () => false,
    activity: () => "idle",
    replies: () => false,
    submitted: () => {},
    confirm: (k) => void said.push(k),
    endPhrase: () => void ends++,
    ...over,
  };
  return { h, said, ends: () => ends };
}

// --- 1. the wait for the final adapts ---------------------------------------- //

describe("“over” waits for the final only as long as finals take", () => {
  it("overWaitMs: half again the typical round trip plus a breath, within 300 ms..1.5 s; the most while unknown", () => {
    expect(OVER_FINAL_WAIT_MS).toBe(1500);
    expect(OVER_FINAL_MIN_MS).toBe(300);
    expect(overWaitMs(null)).toBe(OVER_FINAL_WAIT_MS);
    expect(overWaitMs(0)).toBe(OVER_FINAL_WAIT_MS);
    expect(overWaitMs(400)).toBe(700);
    expect(overWaitMs(50)).toBe(OVER_FINAL_MIN_MS);
    expect(overWaitMs(2000)).toBe(OVER_FINAL_WAIT_MS); // a slow final is not waited for
  });

  it("FinalClock: the server's pass seeds it (plus the round trip), the measured round trips take over and follow the last few", () => {
    const c = new FinalClock();
    expect(c.typical()).toBeNull();
    expect(c.typical(null)).toBeNull();
    expect(c.typical(500)).toBe(500 + FINAL_ROUND_TRIP_MS);
    c.note(300);
    expect(c.typical(500)).toBe(300); // measured beats the seed
    c.note(700);
    expect(c.typical()).toBe(460); // 0.6 × 300 + 0.4 × 700
    for (let i = 0; i < 10; i++) c.note(200);
    expect(c.typical()).toBeLessThan(210); // a slow moment is forgotten
    c.note(-1);
    c.note(120_000);
    expect(c.typical()).toBeLessThan(210); // nonsense is ignored
    c.reset();
    expect(c.typical()).toBeNull();
  });

  describe("in a dictation", () => {
    let quiet = 0;
    beforeEach(() => {
      vi.useFakeTimers();
      quiet = 0;
      mocks.instApi.mockResolvedValue({ ok: true });
    });
    const fast = (over: Partial<LiveHooks> = {}) => hooks({ quietMs: () => quiet, ...over });

    it("the server's final_ms sets the first wait: a 400 ms pass → the fast words go at 700 ms, not 1.5 s", async () => {
      const t = session();
      const d = new LiveDictation(t, fast({ finalMs: () => 400 }).h, { continuous: true });
      await d.stream("", "ship it over");
      quiet = CONTROL_TAIL_MS;
      await vi.advanceTimersByTimeAsync(50);
      const wait = overWaitMs(400 + FINAL_ROUND_TRIP_MS);
      expect(wait).toBe(925);
      await vi.advanceTimersByTimeAsync(wait - 60);
      expect(mocks.instApi).not.toHaveBeenCalled();
      await vi.advanceTimersByTimeAsync(60);
      expect(mocks.instApi).toHaveBeenCalledTimes(1);
      expect(t.keys).toEqual(["ship it"]);
    });

    it("a final that comes back is measured: the next “over” waits its round trip and a half, and a final within it sends at once", async () => {
      const t = session();
      const k = fast();
      const d = new LiveDictation(t, k.h, { continuous: true });
      await d.stream("", "run the test over");
      quiet = CONTROL_TAIL_MS;
      await vi.advanceTimersByTimeAsync(50);
      expect(k.ends()).toBe(1);
      // /end answers 300 ms after the phrase was ended: measured.
      await vi.advanceTimersByTimeAsync(300);
      const flushed = d.flush("", "run the tests over");
      await vi.advanceTimersByTimeAsync(SUBMIT_SETTLE_MS + 10); // the correction settles before Enter
      await flushed;
      expect(mocks.instApi).toHaveBeenCalledTimes(1);
      expect(t.keys).toEqual(["run the test", "s "]);
      expect(finalClock.typical()).toBe(300);
      // The next message: "over" waits 550 ms (300 × 1.5 + 100), then sends the fast words.
      expect(overWaitMs(300)).toBe(550);
      await d.stream("", "and push over"); // the quiet is already there: the phrase ends in this step
      expect(k.ends()).toBe(2);
      await vi.advanceTimersByTimeAsync(500);
      expect(mocks.instApi).toHaveBeenCalledTimes(1);
      await vi.advanceTimersByTimeAsync(60);
      expect(mocks.instApi).toHaveBeenCalledTimes(2);
      // Its late final is still measured (the clock follows a slow moment)…
      await vi.advanceTimersByTimeAsync(1000);
      await d.flush("", "and push it over");
      expect(mocks.instApi).toHaveBeenCalledTimes(2); // …but never sent twice
      expect(finalClock.typical()).toBeGreaterThan(300);
    });

    it("a final matching the fast words types nothing: Enter goes without the settle", async () => {
      const t = session();
      const d = new LiveDictation(t, fast().h, { continuous: true });
      await d.stream("", "ship it over");
      quiet = CONTROL_TAIL_MS;
      await vi.advanceTimersByTimeAsync(50);
      const sent = d.flush("", "ship it over");
      await vi.advanceTimersByTimeAsync(1); // not SUBMIT_SETTLE_MS
      await sent;
      expect(mocks.instApi).toHaveBeenCalledTimes(1);
      expect(t.keys).toEqual(["ship it"]);
    });
  });
});

// --- 3. letting go -------------------------------------------------------------- //

describe("letting go waits for the phrase in flight only as long as it should", () => {
  it("finishWaitMs: a streamed phrase its final's budget; a clip its transcript; Web Speech its final", () => {
    expect(CLIP_FINISH_WAIT_MS).toBe(45_000);
    expect(finishWaitMs({ name: "browser" })).toBe(HOLD_FINAL_WAIT_MS);
    expect(finishWaitMs({ name: "server", streaming: false })).toBe(CLIP_FINISH_WAIT_MS);
    expect(finishWaitMs({ name: "server", streaming: true })).toBe(OVER_FINAL_WAIT_MS);
    expect(finishWaitMs({ name: "server", streaming: true, finalMs: 400 })).toBe(overWaitMs(400 + FINAL_ROUND_TRIP_MS));
    finalClock.note(200);
    expect(finishWaitMs({ name: "server", streaming: true, finalMs: 400 })).toBe(400);
  });
});

// --- 4. barge-in by voice ------------------------------------------------------- //

describe("barge-in: the person talks over a line the app says", () => {
  it("BargeDetector: three loud reads over a quiet floor; a click is not; the run starts over after", () => {
    expect(BARGE_POLL_MS).toBe(40);
    expect(BARGE_POLLS).toBe(3);
    expect(BARGE_RMS).toBe(RMS_SPEECH * 2);
    const b = new BargeDetector();
    b.begin();
    for (let i = 0; i < 20; i++) expect(b.block(0.003)).toBe(false); // the room
    expect(b.bleed).toBeCloseTo(0.003, 3);
    expect(b.block(0.1)).toBe(false); // a click
    expect(b.block(0.002)).toBe(false);
    expect(b.block(0.08)).toBe(false);
    expect(b.block(0.08)).toBe(false);
    expect(b.block(0.08)).toBe(true); // the third in a row: a barge-in
    const bleed = b.bleed;
    expect(b.block(0.08)).toBe(false); // the run starts over
    expect(b.block(0.08)).toBe(false);
    expect(b.block(0.08)).toBe(true);
    // What follows a barge-in is the person: the floor learns nothing more
    // this line, so the next line's barge-in needs no louder a voice.
    for (let i = 0; i < 30; i++) b.block(0.3);
    expect(b.bleed).toBe(bleed);
    b.begin();
    expect([0.08, 0.08, 0.08].map((r) => b.block(r))).toEqual([false, false, true]);
  });

  it("a soft voice over a cancelled echo barges; the floor is three times what the microphone hears of the app", () => {
    const b = new BargeDetector();
    for (let i = 0; i < 20; i++) b.block(0.003);
    expect([0.05, 0.05, 0.05].map((r) => b.block(r))).toEqual([false, false, true]);
    // A louder bleed (half-cancelled): a voice must stand out three to one.
    const c = new BargeDetector();
    for (let i = 0; i < 20; i++) c.block(0.02);
    expect([0.05, 0.05, 0.05].map((r) => c.block(r))).toEqual([false, false, false]);
    expect([0.1, 0.1, 0.1].map((r) => c.block(r))).toEqual([false, false, true]);
  });

  it("no echo cancellation at all: the app's own voice seeds the floor from the first read and never barges on itself", () => {
    const b = new BargeDetector();
    b.begin();
    for (let i = 0; i < 40; i++) expect(b.block(0.06)).toBe(false);
    expect(b.bleed).toBeCloseTo(0.06, 3);
    // The next line, with the floor learnt: the same.
    b.begin();
    for (let i = 0; i < 40; i++) expect(b.block(0.06)).toBe(false);
  });

  it("midSentence: true for MID_SENTENCE_MS after a barge-in (the next line waits for the person's words)", () => {
    vi.useFakeTimers();
    const d = new LiveDictation(session(), hooks({ quietMs: () => 5000 }).h, { continuous: true });
    vi.advanceTimersByTime(MID_SENTENCE_MS + 1);
    expect(d.midSentence()).toBe(false);
    d.bargedIn();
    expect(d.midSentence()).toBe(true);
    vi.advanceTimersByTime(MID_SENTENCE_MS - 1);
    expect(d.midSentence()).toBe(true);
    vi.advanceTimersByTime(2);
    expect(d.midSentence()).toBe(false);
  });

  /** The server recognizer with a microphone, a recorder and an analyser
   * whose level is `rms()`. */
  function rig() {
    const stream = { active: true, getTracks: () => [{ stop() {} }] };
    vi.stubGlobal("navigator", { language: "en-US", userAgent: "", mediaDevices: { getUserMedia: () => Promise.resolve(stream) } });
    const mutes: Array<[boolean, number | undefined]> = [];
    mocks.startWavRecording.mockImplementation(() => ({
      take: () => null,
      stop: () => Promise.resolve(new Blob([new Uint8Array(44)])),
      cancel: () => {},
      seconds: () => 0,
      mute: (on: boolean, tailS?: number) => void mutes.push([on, tailS]),
      last: () => Promise.resolve(null),
    }));
    mocks.apiRaw.mockResolvedValue({ ok: true, status: 200, json: async () => ({}) });
    const ev: string[] = [];
    const h: RecognizerHandlers = {
      onInterim: () => {},
      onFinal: () => {},
      onReady: () => void ev.push("ready"),
      onLevel: () => {},
      onBarge: () => void ev.push("barge"),
      onError: (c, d) => void ev.push("error:" + (d || c)),
      onEnd: () => void ev.push("end"),
    };
    return { h, ev, mutes, level: (v: number) => void (rms = v) };
  }

  it("muted for a line: the analyser is read every 40 ms; a voice over it → onBarge once; the mute lifted then keeps the tail", async () => {
    vi.useFakeTimers();
    const t = rig();
    const r = serverRecognizer({ stream: () => false });
    expect(r.start(t.h)).toBe(true);
    await vi.waitFor(() => expect(t.ev).toContain("ready"));
    r.mute!(true);
    expect(t.mutes).toEqual([[true, BARGE_SKIP_MS / 1000]]);
    // The room, for a second: nothing.
    await vi.advanceTimersByTimeAsync(1000);
    expect(t.ev.filter((e) => e === "barge")).toEqual([]);
    // The person: three reads.
    t.level(0.08);
    await vi.advanceTimersByTimeAsync(BARGE_POLL_MS * 2);
    expect(t.ev.filter((e) => e === "barge")).toEqual([]);
    await vi.advanceTimersByTimeAsync(BARGE_POLL_MS);
    expect(t.ev.filter((e) => e === "barge")).toEqual(["barge"]);
    // The line is cut by the caller → the mute lifted: no tail skipped (the voice is the person's).
    r.mute!(false);
    expect(t.mutes.at(-1)).toEqual([false, 0]);
    // A later line, ended on its own: the voice's tail is skipped as before.
    await vi.advanceTimersByTimeAsync(BARGE_FRESH_MS + 1);
    t.level(0.003);
    r.mute!(true);
    await vi.advanceTimersByTimeAsync(200);
    r.mute!(false);
    expect(t.mutes.at(-1)).toEqual([false, BARGE_SKIP_MS / 1000]);
    expect(t.ev.filter((e) => e === "barge")).toEqual(["barge"]);
    r.stop();
  });

  it("stop() ends the polling", async () => {
    vi.useFakeTimers();
    const t = rig();
    const r = serverRecognizer({ stream: () => false });
    r.start(t.h);
    await vi.waitFor(() => expect(t.ev).toContain("ready"));
    r.mute!(true);
    r.stop();
    t.level(0.3);
    await vi.advanceTimersByTimeAsync(1000);
    expect(t.ev).not.toContain("barge");
  });
});

// --- 5. the reply's requests ---------------------------------------------------- //

describe("the server speaker's requests: the first sentence alone, then short ones packed", () => {
  it("speechRequests: long sentences one each (as before); short ones packed to ~2.7 s of speech, never past 1.5× the request before", () => {
    expect(SPEAK_REQUEST_MIN_S).toBe(2.7);
    expect(SPEECH_CHARS_PER_S).toBe(15);
    const long = "The first sentence is here. The second one follows it. And the third ends it.";
    expect(speechRequests(long)).toEqual(speechSentences(long));
    expect(speechRequests("Sent. Over and out.")).toEqual(["Sent. Over and out."]);
    expect(speechRequests("")).toEqual([]);
    expect(speechRequests("Done. Tests pass. Pushing now. Over to you. Merged it.")).toEqual([
      "Done. Tests pass.",
      "Pushing now. Over to you. Merged it.",
    ]);
    // A long first sentence lets the next request be longer (it plays long enough).
    const first = "This first sentence runs on for a good long while before it ends.";
    const reqs = speechRequests(first + " Then this. And this one. And a third short one. And the last.");
    expect(reqs[0]).toBe(first);
    expect(reqs.slice(1).join(" ")).toBe("Then this. And this one. And a third short one. And the last.");
    for (let i = 1; i < reqs.length; i++) expect(reqs[i].length).toBeLessThanOrEqual(Math.max(40.5, reqs[i - 1].length * 1.5));
    expect(reqs.length).toBeLessThan(5);
  });

  /** Voice Lab as measured (2026-10-27, "Donna"): synthesis ≈ max(1.4 s,
   * 0.52 × the audio), about 15 characters of text a second of audio. */
  function lab() {
    const requests: string[] = [];
    mocks.apiRaw.mockImplementation((_url: string, init: RequestInit) => {
      const text = JSON.parse(String(init.body)).text as string;
      requests.push(text);
      const dur = text.length / SPEECH_CHARS_PER_S;
      return new Promise((resolve) =>
        setTimeout(() => resolve({ ok: true, status: 200, arrayBuffer: async () => ({ text, dur }) }), Math.max(1400, 520 * dur))
      );
    });
    const fallback: Speaker = { speak: async () => {}, cancel: () => {} };
    T0 = Date.now();
    return { starts, requests, fallback, T0 };
  }

  it("four short sentences: two requests, one gap, done in ~5 s — not four requests with three gaps in ~6.3 s", async () => {
    vi.useFakeTimers();
    const l = lab();
    const sp = serverSpeaker(l.fallback);
    let done = -1;
    void sp.speak("Done. Tests pass. Pushing now. Over to you. Merged it.").then(() => (done = Date.now() - l.T0));
    await vi.advanceTimersByTimeAsync(10_000);
    expect(l.requests).toEqual(["Done. Tests pass.", "Pushing now. Over to you. Merged it."]);
    expect(l.starts.map((s) => Math.round(s.at * 100) / 100)).toEqual([1.4, 2.8]); // the first sound at the engine's floor
    expect(done).toBe(5200);
  });

  it("long sentences: still one request each, back to back (each is back before the one before ends)", async () => {
    vi.useFakeTimers();
    const l = lab();
    const sp = serverSpeaker(l.fallback);
    const line = "The tests pass now and the branch is ready. I pushed it a moment ago to the remote. Anything else before I open the pull request?";
    void sp.speak(line);
    await vi.advanceTimersByTimeAsync(15_000);
    expect(l.requests).toEqual(speechSentences(line));
    const at = l.starts.map((s) => s.at);
    expect(at[0]).toBeCloseTo(1.49, 1);
    for (let i = 1; i < at.length; i++) expect(at[i]).toBeCloseTo(at[i - 1] + l.requests[i - 1].length / SPEECH_CHARS_PER_S, 2); // gapless
  });
});

// --- 6. the queue's cue lead and confirmation ------------------------------------- //

describe("the queue knows its cue's lead and when a confirmation plays", () => {
  const engine = () => {
    const sp: Speaker & { cancel: () => void } = { speak: () => new Promise(() => {}), cancel: () => {} };
    return sp;
  };
  it("cueLead: nothing when idle, CUE_GAP_MS when something is said or waits", () => {
    const q = new SpeechQueue(engine);
    expect(q.cueLead).toBe(0);
    void q.say("A reply.");
    expect(q.cueLead).toBe(CUE_GAP_MS);
    q.hold();
    void q.say("Another.");
    expect(q.cueLead).toBe(CUE_GAP_MS); // waiting behind the hold
    q.cancel();
    expect(q.cueLead).toBe(0);
  });
  it("confirming: while a confirmation is said, not a reply, not a cue", () => {
    vi.useFakeTimers();
    const q = new SpeechQueue(engine);
    void q.say("A reply.");
    expect(q.busy).toBe(true);
    expect(q.confirming).toBe(false);
    q.cancel();
    void q.confirmLine("Sent.");
    expect(q.confirming).toBe(true);
    q.cancel();
    void q.cue(() => {}, 100);
    expect(q.busy).toBe(true);
    expect(q.confirming).toBe(false);
  });
});

// --- 7. the wiring ------------------------------------------------------------------ //

describe("TopBarMic wires it", () => {
  const MIC = code("components/TopBarMic.tsx");
  it("a barge-in cuts a reply (not a confirmation) and the dictation holds the next line", () => {
    const h = MIC.slice(MIC.indexOf("onBarge: () =>"), MIC.indexOf("onError: (code, detail) =>"));
    expect(h).toContain("if (live.current !== d || !queue.busy || queue.confirming) return;");
    expect(h).toContain("d.bargedIn();");
    expect(h).toContain("queue.skip();");
  });
  it("the first “over” waits by the server's final_ms; letting go by finishWaitMs", () => {
    expect(MIC).toContain("finalMs: () => (r === serverRec ? serverRec.finalMs : null),");
    expect(MIC).toContain("setTimeout(decide, finishWaitMs(r === serverRec ? serverRec : { name: r.name }))");
    expect(MIC).not.toContain("45_000");
  });
});
