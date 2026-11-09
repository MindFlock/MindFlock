/** The wait after the person stops talking (the STT latency audit, 2026-11):
 *
 *  - the next phrase's stream opens at once, never behind the last phrase's
 *    /end; its words go out the moment they are back — the phrase before
 *    ends first with its fast words when its /end is still out;
 *  - "over" goes without waiting for the final when the fast words settled
 *    and the finals lately seldom changed a word (FinalClock.fastStands);
 *  - the finals' round trip is a median of finals that CAME (a timeout is
 *    never a sample);
 *  - a settled phrase ends on a shorter quiet; a first phrase that can't be
 *    a command no longer waits the command floor;
 *  - recognition warms on a config read when this device used it lately. */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  CONTROL_TAIL_MS,
  Dictation,
  FAST_TRUST_MIN,
  FIRST_PHRASE_HOLD_MS,
  FIRST_PHRASE_PLAIN_HOLD_MS,
  FinalClock,
  LiveDictation,
  OVER_FINAL_WAIT_MS,
  SETTLED_HOLD_MS,
  SILENCE_MS,
  STT_WARM_EVERY_MS,
  STT_WARM_RECENT_MS,
  finalClock,
  overWaitMs,
  plainWords,
  serverRecognizer,
  sttWarmWanted,
  timedFinal,
  turnHoldMs,
  type DictTarget,
  type LiveHooks,
  type RecognizerHandlers,
  type VoiceCfg,
} from "../lib/voiceDesktop";

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

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  mocks.apiRaw.mockReset();
  mocks.instApi.mockReset();
  mocks.startWavRecording.mockReset();
  finalClock.reset();
});

// --- the clock ------------------------------------------------------------------ //

describe("Moonshine hears the control words, whisper writes the text", () => {
  it("only the accurate pass's final is a sample for the clock", () => {
    expect(timedFinal({ final: "fix the bug", final_ms: 700 })).toBe(true);
    expect(timedFinal({ final: "fix the bug", final_by: "whisper" })).toBe(true);
    // A control-only phrase (or a timed-out pass): Moonshine's own text.
    expect(timedFinal({ final: "Stop.", final_by: "moonshine" })).toBe(false);
    expect(timedFinal({ committed: "bag" })).toBe(false);
    expect(timedFinal(null)).toBe(false);
  });

  it("a garbled “over” still sends, with whisper's words before it", () => {
    // Moonshine heard "send the report over"; whisper wrote "Send the
    // reports, okay." — /end's final carries Moonshine's control phrase on
    // whisper's words (speech_moonshine.with_control).
    const d = new Dictation();
    d.begin();
    expect(d.flush("send the report over", "Send the reports over.")).toEqual({
      kind: "send",
      text: "Send the reports ",
      end: true,
    });
  });
});


describe("FinalClock", () => {
  it("fastStands: only after enough finals, and only while few of them changed a word", () => {
    const c = new FinalClock();
    expect(c.fastStands()).toBe(false);
    for (let i = 0; i < FAST_TRUST_MIN - 1; i++) c.verdict(false);
    expect(c.fastStands()).toBe(false); // too few to say
    c.verdict(false);
    expect(c.fastStands()).toBe(true);
    c.verdict(true);
    expect(c.fastStands()).toBe(true); // 1 of 4
    c.verdict(true);
    expect(c.fastStands()).toBe(false); // 2 of 5: the final is earning its wait
    for (let i = 0; i < 12; i++) c.verdict(false);
    expect(c.fastStands()).toBe(true); // the window moves on
    c.reset();
    expect(c.fastStands()).toBe(false);
  });

  it("plainWords: what `same` ignores — case, punctuation, spacing", () => {
    expect(plainWords("Fix the  bug, over.")).toBe(plainWords("fix the bug over"));
    expect(plainWords("fix the bag")).not.toBe(plainWords("fix the bug"));
    expect(plainWords("don't")).toBe("don't");
  });
});

// --- the endpoint holds --------------------------------------------------------- //

describe("turnHoldMs: a settled phrase ends sooner", () => {
  const sentence = { hold: 900, reason: "sentence", at: 2000 };
  it("words typed, every word agreed and the quiet heard: the settled hold", () => {
    expect(SETTLED_HOLD_MS).toBe(600);
    expect(turnHoldMs(sentence, 1000, false, { text: "fix the failing test", settled: true })).toBe(SETTLED_HOLD_MS);
    // Not settled (or nothing known): the verdict's own.
    expect(turnHoldMs(sentence, 1000, false, { text: "fix the failing test", settled: false })).toBe(900);
    expect(turnHoldMs(sentence, 1000, false)).toBe(900);
    // The voice came back after the audio judged: never shorter than the silence stop.
    expect(turnHoldMs(sentence, 3000, false, { text: "fix the failing test", settled: true })).toBe(SILENCE_MS);
    // A comma's long hold is not a settled sentence.
    expect(turnHoldMs({ hold: 1500, reason: "comma", at: 2000 }, 1000, false, { text: "fix it,", settled: true })).toBe(1500);
  });

  it("the first phrase: the command floor only when its words could be a command", () => {
    expect(turnHoldMs(sentence, 1000, true)).toBe(FIRST_PHRASE_HOLD_MS);
    expect(turnHoldMs(sentence, 1000, true, { text: "next session", settled: true })).toBe(FIRST_PHRASE_HOLD_MS);
    expect(turnHoldMs(sentence, 1000, true, { text: "the login page throws a null error", settled: true })).toBe(
      FIRST_PHRASE_PLAIN_HOLD_MS
    );
    // Never the settled hold on a first phrase (it is routed alone).
    expect(turnHoldMs({ hold: 400, reason: "sentence", at: 2000 }, 1000, true, { text: "the login page", settled: true })).toBe(
      FIRST_PHRASE_PLAIN_HOLD_MS
    );
  });
});

// --- "over" without the final ----------------------------------------------------- //

function session(): DictTarget & { keys: string[] } {
  const keys: string[] = [];
  return { kind: "session", title: "t1", name: "Login fix", keys, type: (t) => keys.push(t) > 0, erase: (t) => keys.push("⌫" + t) > 0 };
}

describe("“over” goes at once when the fast words stand", () => {
  let quiet = 0;
  let ends = 0;
  beforeEach(() => {
    vi.useFakeTimers();
    quiet = 0;
    ends = 0;
    mocks.instApi.mockResolvedValue({ ok: true });
  });
  const hooks = (over: Partial<LiveHooks> = {}): LiveHooks => ({
    caption: () => {},
    halt: () => {},
    route: async () => false,
    activity: () => "idle",
    replies: () => false,
    submitted: () => {},
    quietMs: () => quiet,
    endPhrase: () => void ends++,
    endsSurely: () => true,
    ...over,
  });
  const trust = () => {
    for (let i = 0; i < FAST_TRUST_MIN; i++) finalClock.verdict(false);
  };

  it("settled words and trusted finals: Enter without the final's wait", async () => {
    trust();
    const t = session();
    const d = new LiveDictation(t, hooks(), { continuous: true });
    await d.stream("ship it over", "");
    quiet = CONTROL_TAIL_MS;
    await vi.advanceTimersByTimeAsync(60);
    expect(ends).toBe(1); // the phrase still ends at the recognizer (its final is measured)
    expect(mocks.instApi).toHaveBeenCalledTimes(1);
    expect(t.keys).toEqual(["ship it"]);
    // Its final, late, changes nothing and sends nothing.
    await d.flush("", "ship it over");
    expect(mocks.instApi).toHaveBeenCalledTimes(1);
  });

  it("the finals often change words: the wait stands (as before)", async () => {
    for (let i = 0; i < FAST_TRUST_MIN; i++) finalClock.verdict(true);
    const t = session();
    const d = new LiveDictation(t, hooks(), { continuous: true });
    await d.stream("ship it over", "");
    quiet = CONTROL_TAIL_MS;
    await vi.advanceTimersByTimeAsync(OVER_FINAL_WAIT_MS - 100);
    expect(mocks.instApi).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(200);
    expect(mocks.instApi).toHaveBeenCalledTimes(1);
  });

  it("an unsure tail waits; the phrase's last chunk settling it sends at once", async () => {
    trust();
    const t = session();
    const d = new LiveDictation(t, hooks(), { continuous: true });
    await d.stream("ship", "it over");
    quiet = CONTROL_TAIL_MS;
    await vi.advanceTimersByTimeAsync(60);
    expect(ends).toBe(1);
    expect(mocks.instApi).not.toHaveBeenCalled(); // "over" itself is still a guess
    await d.stream("it over", ""); // the tail chunk: two passes agree on every word
    await vi.advanceTimersByTimeAsync(5);
    expect(mocks.instApi).toHaveBeenCalledTimes(1);
    expect(t.keys).toEqual(["ship it"]);
  });

  it("a recognizer without measured finals (no endsSurely): the wait stands", async () => {
    trust();
    const t = session();
    const d = new LiveDictation(t, hooks({ endsSurely: () => false }), { continuous: true });
    await d.stream("ship it over", "");
    quiet = CONTROL_TAIL_MS;
    await vi.advanceTimersByTimeAsync(overWaitMs(null) - 100);
    expect(mocks.instApi).not.toHaveBeenCalled();
  });
});

// --- the recognizer: phrases overlap --------------------------------------------- //

class FakeCtx {
  state = "running";
  sampleRate = 48000;
  resume() {
    return Promise.resolve();
  }
}
const resp = (status: number, body: unknown) => ({ ok: status < 300, status, json: async () => body });
const wav = () => new Blob([new Uint8Array(44 + 3200)], { type: "audio/wav" });

function setup() {
  vi.stubGlobal("window", { AudioContext: FakeCtx });
  const getUserMedia = vi.fn(async () => {
    const track = { stop: vi.fn() };
    return { active: true, getTracks: () => [track] };
  });
  vi.stubGlobal("navigator", { language: "en-US", userAgent: "", mediaDevices: { getUserMedia } });
  let now = 1_000_000;
  vi.spyOn(Date, "now").mockImplementation(() => now);
  const clips: Array<{ opts: { onLevel?: (rms: number) => void }; feed(): void }> = [];
  mocks.startWavRecording.mockImplementation((_s: unknown, _c: unknown, o) => {
    let ready = false;
    const clip = {
      opts: o,
      take: vi.fn(() => {
        if (!ready) return null;
        ready = false;
        return wav();
      }),
      stop: vi.fn(() => Promise.resolve(wav())),
      cancel: vi.fn(),
      seconds: () => 0.1,
      feed() {
        ready = true;
      },
    };
    clips.push(clip);
    return clip;
  });
  const calls: string[] = [];
  /** Answers per stream id, in order: chunk bodies and the /end. */
  const chunkAnswers = new Map<string, unknown[]>();
  const endAnswers = new Map<string, Promise<unknown>>();
  let n = 0;
  mocks.apiRaw.mockImplementation(async (url: string, init: RequestInit) => {
    calls.push(String(init?.method || "GET") + " " + url);
    if (url === "/api/voice/stream") return resp(200, { id: "s" + ++n });
    const sid = url.split("/")[4];
    if (url.endsWith("/chunk")) return resp(200, chunkAnswers.get(sid)?.shift() ?? { committed: "", partial: "" });
    if (url.endsWith("/end")) return resp(200, await (endAnswers.get(sid) ?? Promise.resolve({ committed: "", partial: "" })));
    return resp(204, {});
  });
  const ev: string[] = [];
  const h: RecognizerHandlers = {
    onInterim: () => {},
    onFinal: (x) => void ev.push("final:" + x),
    onStream: (w, p) => void ev.push("stream:" + w + "|" + p),
    onPartial: (x) => void (x && ev.push("partial:" + x)),
    onFlush: (x, f) => void ev.push("flush:" + x + (f ? "→" + f : "")),
    onReady: () => {},
    onError: (c, d) => void ev.push("error:" + (d || c)),
    onEnd: () => void ev.push("end"),
  };
  const block = (i: number, loud: boolean, ms = 100) => {
    now += ms;
    clips[i].opts.onLevel!(loud ? 0.3 : 0.001);
  };
  const deferred = () => {
    let res: (v: unknown) => void = () => {};
    const p = new Promise<unknown>((r) => (res = r));
    return { p, res };
  };
  return { clips, calls, ev, h, block, chunkAnswers, endAnswers, deferred };
}

describe("the next phrase never waits on the last one's /end", () => {
  it("its stream opens at once, and its words end the phrase before with its fast words", async () => {
    const t = setup();
    const end1 = t.deferred();
    t.endAnswers.set("s1", end1.p);
    t.chunkAnswers.set("s1", [{ committed: "fix the", partial: "bug" }]);
    t.chunkAnswers.set("s2", [{ committed: "and run it", partial: "" }]);
    const r = serverRecognizer({ stream: () => true });
    r.start(t.h);
    await vi.waitFor(() => expect(t.clips.length).toBe(1));
    await vi.waitFor(() => expect(t.calls).toContain("POST /api/voice/stream"));
    t.block(0, false); // the room: the noise floor
    t.clips[0].feed();
    t.block(0, true);
    await vi.waitFor(() => expect(t.ev).toContain("stream:fix the|bug"));
    // A pause long enough to end the phrase: its /end goes out and hangs
    // (the accurate pass) — and the next phrase's stream opens meanwhile.
    t.block(0, false, SILENCE_MS + 50);
    await vi.waitFor(() => expect(t.calls).toContain("POST /api/voice/stream/s1/end"));
    await vi.waitFor(() => expect(t.calls.filter((c) => c === "POST /api/voice/stream").length).toBe(2));
    await vi.waitFor(() => expect(t.clips.length).toBe(2));
    // The person talks again: their words come back while /end is still out.
    t.block(1, false); // the room: the noise floor
    t.clips[1].feed();
    t.block(1, true);
    await vi.waitFor(() => expect(t.ev).toContain("stream:and run it|"));
    // In order: the phrase before ends first, as its fast words.
    expect(t.ev.indexOf("flush:bug")).toBeGreaterThan(-1);
    expect(t.ev.indexOf("flush:bug")).toBeLessThan(t.ev.indexOf("stream:and run it|"));
    // Its final, when it comes, is not handed over again (only measured).
    end1.res({ committed: "", partial: "", final: "Fix the bug.", final_ms: 900, same: true });
    await new Promise((res) => setTimeout(res, 0));
    expect(t.ev.filter((e) => e.startsWith("flush:")).length).toBe(1);
    r.stop();
  });

  it("a final back before the next phrase's words: handed over as always", async () => {
    const t = setup();
    t.endAnswers.set("s1", Promise.resolve({ committed: "", partial: "", final: "fix the bag", final_ms: 500 }));
    t.chunkAnswers.set("s1", [{ committed: "fix the", partial: "bug" }]);
    const r = serverRecognizer({ stream: () => true });
    r.start(t.h);
    await vi.waitFor(() => expect(t.clips.length).toBe(1));
    await vi.waitFor(() => expect(t.calls).toContain("POST /api/voice/stream"));
    t.block(0, false); // the room: the noise floor
    t.clips[0].feed();
    t.block(0, true);
    await vi.waitFor(() => expect(t.ev).toContain("stream:fix the|bug"));
    t.block(0, false, SILENCE_MS + 50);
    await vi.waitFor(() => expect(t.ev).toContain("flush:→fix the bag"));
    r.stop();
  });
});

describe("the finals' clock learns from finals that came", () => {
  async function overPhrase(end: unknown) {
    const t = setup();
    t.endAnswers.set("s1", Promise.resolve(end));
    t.chunkAnswers.set("s1", [{ committed: "ship it over", partial: "" }]);
    const r = serverRecognizer({ stream: () => true });
    r.start(t.h);
    await vi.waitFor(() => expect(t.clips.length).toBe(1));
    await vi.waitFor(() => expect(t.calls).toContain("POST /api/voice/stream"));
    t.block(0, false); // the room: the noise floor
    t.clips[0].feed();
    t.block(0, true);
    await vi.waitFor(() => expect(t.ev).toContain("stream:ship it over|"));
    r.endPhrase?.();
    await vi.waitFor(() => expect(t.ev.some((e) => e.startsWith("flush:"))).toBe(true));
    r.stop();
  }

  it("a final: a sample, and whether it changed a word", async () => {
    await overPhrase({ committed: "", partial: "", final: "Ship it, over.", final_ms: 700, same: true });
    expect(finalClock.typical()).not.toBeNull();
    for (let i = 1; i < FAST_TRUST_MIN; i++) finalClock.verdict(false);
    expect(finalClock.fastStands()).toBe(true); // that one did not change a word
  });

  it("a timeout (no final): no sample, no verdict", async () => {
    await overPhrase({ committed: "", partial: "" });
    expect(finalClock.typical()).toBeNull();
    for (let i = 1; i < FAST_TRUST_MIN; i++) finalClock.verdict(false);
    expect(finalClock.fastStands()).toBe(false); // one short: the timeout was not counted
  });
});

// --- warming recognition --------------------------------------------------------- //

describe("sttWarmWanted", () => {
  const now = 10 * STT_WARM_RECENT_MS;
  const cfg = (ready: Record<string, unknown>, stt: "server" | "phone" = "server"): VoiceCfg => ({
    stt,
    tts: "phone",
    server_ready: ready,
    system: {},
  });
  it("only for a streaming server recognizer that says it is not loaded, used here lately, not just warmed", () => {
    const cold = { stt_stream: true, stt_loaded: false };
    expect(sttWarmWanted(cfg(cold), now - 1000, 0, now)).toBe(true);
    expect(sttWarmWanted(cfg({ stt_stream: true, stt_loaded: true }), now - 1000, 0, now)).toBe(false);
    expect(sttWarmWanted(cfg({ stt_stream: true }), now - 1000, 0, now)).toBe(false); // an older server: never
    expect(sttWarmWanted(cfg(cold, "phone"), now - 1000, 0, now)).toBe(false);
    expect(sttWarmWanted(cfg(cold), 0, 0, now)).toBe(false); // never used here
    expect(sttWarmWanted(cfg(cold), now - STT_WARM_RECENT_MS - 1, 0, now)).toBe(false);
    expect(sttWarmWanted(cfg(cold), now - 1000, now - STT_WARM_EVERY_MS + 1, now)).toBe(false);
  });
});
