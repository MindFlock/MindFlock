/** The desktop mic's start: seamless, and visibly picking you up.
 *
 * What is pinned: the server stream opens as listening starts (in parallel
 * with the microphone), while chunks still start at the first speech; a
 * phrase that heard nothing DELETEs its stream; a stream left unused too long
 * is reopened at the first word; the microphone is reused within
 * KEEP_STREAM_MS and released after; the partial shows (greyed) before any
 * commit and is never typed; the level smoothing and the "Hearing you…" /
 * "Listening…" rule (pure); and the wiring that keeps the meter off React's
 * render path. */

import { afterEach, describe, expect, it, vi } from "vitest";
import { LiveDictation, serverRecognizer, STREAM_FRESH_MS, type LiveHooks, type RecognizerHandlers } from "../lib/voiceDesktop";
import {
  HEARING_HOLD_MS,
  KEEP_STREAM_MS,
  LEVEL_DECAY_MS,
  LIVE_CAPTION_CHARS,
  MicKeeper,
  isLoud,
  levelTarget,
  liveTail,
  liveUnsure,
  noiseFloor,
  pickupLine,
  smoothLevel,
  voicePass,
} from "../lib/voiceMic";
import { code, cssRule, src } from "./sourceText";

const mocks = vi.hoisted(() => ({
  apiRaw: vi.fn(),
  peekTerm: vi.fn(),
  startWavRecording: vi.fn(),
}));
vi.mock("../api/client", () => ({ api: vi.fn(), apiRaw: mocks.apiRaw, instApi: vi.fn(), ApiError: Error }));
vi.mock("../lib/terminals", () => ({ peekTerm: mocks.peekTerm }));
vi.mock("../lib/wavRecorder", () => ({ startWavRecording: mocks.startWavRecording, resamplerName: () => "browser resampler" }));

class FakeCtx {
  state = "running";
  sampleRate = 48000;
  resume() {
    return Promise.resolve();
  }
}
const resp = (status: number, body: unknown) => ({ ok: status < 300, status, json: async () => body });
const wav = () => new Blob([new Uint8Array(44 + 3200)], { type: "audio/wav" });

function fakeStream() {
  const track = { stop: vi.fn() };
  return { active: true, getTracks: () => [track], track };
}

/** A microphone that answers when told (`grant()`), a recorder whose take()
 * has a chunk once fed, a server that opens s1, s2… */
function setup(opts: { chunk?: () => unknown } = {}) {
  vi.stubGlobal("window", { AudioContext: FakeCtx });
  const streams: Array<ReturnType<typeof fakeStream>> = [];
  const waiting: Array<() => void> = [];
  const getUserMedia = vi.fn(
    () =>
      new Promise((res) => {
        const s = fakeStream();
        streams.push(s);
        waiting.push(() => res(s));
      })
  );
  vi.stubGlobal("navigator", { language: "en-US", userAgent: "", mediaDevices: { getUserMedia } });
  let now = 1_000_000;
  vi.spyOn(Date, "now").mockImplementation(() => now);
  const clips: Array<{ opts: { onLevel?: (rms: number) => void }; take: ReturnType<typeof vi.fn>; feed(): void }> = [];
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
  const calls: Array<[string, string]> = [];
  let n = 0;
  mocks.apiRaw.mockImplementation(async (url: string, init: RequestInit) => {
    calls.push([String(init?.method || "GET"), url]);
    if (url === "/api/voice/stream") return resp(200, { id: "s" + ++n });
    if (url.endsWith("/chunk")) return (opts.chunk || (() => resp(200, { committed: "", partial: "" })))();
    if (url.endsWith("/end")) return resp(200, { committed: "", partial: "" });
    return resp(204, {});
  });
  const ev: string[] = [];
  const h: RecognizerHandlers = {
    onInterim: () => {},
    onFinal: (x) => void ev.push("final:" + x),
    onWords: (x) => void ev.push("words:" + x),
    onPartial: (x) => void ev.push("partial:" + x),
    onFlush: (x) => void ev.push("flush:" + x),
    onReady: () => void ev.push("ready"),
    onLevel: (x) => void ev.push("level:" + x),
    onError: (c, d) => void ev.push("error:" + (d || c)),
    onEnd: () => void ev.push("end"),
  };
  const block = (i: number, loud: boolean, ms = 100) => {
    now += ms;
    clips[i].opts.onLevel!(loud ? 0.3 : 0.001);
  };
  return {
    streams,
    getUserMedia,
    grant: () => waiting.shift()?.(),
    clips,
    calls,
    ev,
    h,
    block,
    advance: (ms: number) => void (now += ms),
    chunks: () => calls.filter(([, u]) => u.endsWith("/chunk")).length,
  };
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  mocks.apiRaw.mockReset();
  mocks.startWavRecording.mockReset();
});

describe("the server stream opens at start, chunks start at speech", () => {
  it("opens the stream before the microphone is even ready", async () => {
    const t = setup();
    const r = serverRecognizer({ stream: () => true });
    r.start(t.h);
    // getUserMedia has not answered yet, and the open is already out.
    await vi.waitFor(() => expect(t.calls).toEqual([["POST", "/api/voice/stream"]]));
    expect(t.clips.length).toBe(0);
    t.grant();
    await vi.waitFor(() => expect(t.ev).toContain("ready"));
    // The quiet before the first word is never sent, though the stream is open.
    for (let i = 0; i < 5; i++) {
      t.clips[0].feed();
      t.block(0, false);
    }
    expect(t.chunks()).toBe(0);
    t.clips[0].feed();
    t.block(0, true);
    expect(t.chunks()).toBe(1); // at once: no open round trip in the way
    r.stop();
    expect(t.calls).toContainEqual(["DELETE", "/api/voice/stream/s1"]);
  });

  it("the microphone ready before the open answers: the first loud block waits for the id, then goes", async () => {
    const t = setup();
    let answer: (v: unknown) => void = () => {};
    mocks.apiRaw.mockImplementationOnce(async (url: string, init: RequestInit) => {
      t.calls.push([String(init?.method || "GET"), url]);
      return new Promise((res) => (answer = res));
    });
    const r = serverRecognizer({ stream: () => true });
    r.start(t.h);
    t.grant();
    await vi.waitFor(() => expect(t.clips.length).toBe(1));
    t.block(0, false);
    t.clips[0].feed();
    t.block(0, true);
    expect(t.chunks()).toBe(0); // no id yet
    answer(resp(200, { id: "late" }));
    await vi.waitFor(() => expect(t.chunks()).toBe(1));
    expect(t.calls).toContainEqual(["POST", "/api/voice/stream/late/chunk"]);
    r.stop();
  });

  it("stopped while the open is out: the stream it returns is DELETEd", async () => {
    const t = setup();
    let answer: (v: unknown) => void = () => {};
    mocks.apiRaw.mockImplementationOnce(async () => new Promise((res) => (answer = res)));
    const r = serverRecognizer({ stream: () => true });
    r.start(t.h);
    await vi.waitFor(() => expect(mocks.apiRaw).toHaveBeenCalledTimes(1));
    r.stop();
    answer(resp(200, { id: "orphan" }));
    await vi.waitFor(() => expect(t.calls).toContainEqual(["DELETE", "/api/voice/stream/orphan"]));
  });

  it("let go before a word: the unused stream is DELETEd, never /end-ed", async () => {
    const t = setup();
    const r = serverRecognizer({ stream: () => true });
    r.start(t.h);
    t.grant();
    await vi.waitFor(() => expect(t.clips.length).toBe(1));
    await vi.waitFor(() => expect(t.calls.length).toBe(1));
    t.block(0, false);
    r.finish();
    await vi.waitFor(() => expect(t.ev).toContain("end"));
    expect(t.calls).toContainEqual(["DELETE", "/api/voice/stream/s1"]);
    expect(t.calls.some(([, u]) => u.endsWith("/end"))).toBe(false);
  });

  it("a stream opened long before the first word is opened again (the server forgets one in 60 s)", async () => {
    const t = setup();
    const r = serverRecognizer({ stream: () => true });
    r.start(t.h);
    t.grant();
    await vi.waitFor(() => expect(t.clips.length).toBe(1));
    await vi.waitFor(() => expect(t.calls.length).toBe(1));
    await Promise.resolve();
    t.block(0, false, STREAM_FRESH_MS + 1);
    t.block(0, true);
    expect(t.calls).toContainEqual(["DELETE", "/api/voice/stream/s1"]);
    await vi.waitFor(() => expect(t.calls.filter(([, u]) => u === "/api/voice/stream").length).toBe(2));
    t.clips[0].feed();
    t.block(0, true);
    await vi.waitFor(() => expect(t.calls).toContainEqual(["POST", "/api/voice/stream/s2/chunk"]));
    r.stop();
  });

  it("every block's level goes to onLevel (the meter), recording or not loud", async () => {
    const t = setup();
    const r = serverRecognizer({ stream: () => false });
    r.start(t.h);
    t.grant();
    await vi.waitFor(() => expect(t.clips.length).toBe(1));
    t.block(0, false);
    t.block(0, true);
    expect(t.ev.filter((e) => e.startsWith("level:"))).toEqual(["level:0.001", "level:0.3"]);
    r.stop();
  });
});

describe("the microphone stays warm KEEP_STREAM_MS", () => {
  it("a second dictation within the grace reuses the stream; after it, the tracks stop", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const t = setup();
    const r = serverRecognizer({ stream: () => false });
    r.start(t.h);
    t.grant();
    await vi.waitFor(() => expect(t.clips.length).toBe(1));
    r.stop();
    vi.advanceTimersByTime(KEEP_STREAM_MS - 1000);
    expect(t.streams[0].track.stop).not.toHaveBeenCalled();
    expect(r.mic.isWarm).toBe(true);
    // The next press records without asking for the microphone again.
    r.start(t.h);
    await vi.waitFor(() => expect(t.clips.length).toBe(2));
    expect(t.getUserMedia).toHaveBeenCalledTimes(1);
    // Held while it records: no release under it however long.
    vi.advanceTimersByTime(KEEP_STREAM_MS * 2);
    expect(t.streams[0].track.stop).not.toHaveBeenCalled();
    r.stop();
    vi.advanceTimersByTime(KEEP_STREAM_MS + 1);
    expect(t.streams[0].track.stop).toHaveBeenCalled();
    expect(r.mic.isWarm).toBe(false);
  });

  it("release() (“mic off”) gives it back at once", async () => {
    const t = setup();
    const r = serverRecognizer({ stream: () => false });
    r.start(t.h);
    t.grant();
    await vi.waitFor(() => expect(t.clips.length).toBe(1));
    r.release();
    expect(t.streams[0].track.stop).toHaveBeenCalled();
  });

  it("MicKeeper: warm() opens once (concurrent asks share it) and arms the grace; released while opening it is given back", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const s = fakeStream();
    let grant: () => void = () => {};
    const open = vi.fn(() => new Promise<MediaStream>((res) => (grant = () => res(s as unknown as MediaStream))));
    const k = new MicKeeper(1000, open);
    const a = k.warm();
    const b = k.get();
    grant();
    expect(await a).toBe(true);
    expect(await b).toBe(s);
    expect(open).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(1001);
    expect(s.track.stop).toHaveBeenCalled();

    const s2 = fakeStream();
    open.mockImplementationOnce(() => new Promise<MediaStream>((res) => (grant = () => res(s2 as unknown as MediaStream))));
    const c = k.warm();
    k.release();
    grant();
    expect(await c).toBe(false);
    expect(s2.track.stop).toHaveBeenCalled();
    expect(k.isWarm).toBe(false);
  });

  it("warm(ifGranted) outside a gesture never asks for a permission not given", async () => {
    vi.stubGlobal("navigator", { permissions: { query: async () => ({ state: "prompt" }) } });
    const open = vi.fn(async () => fakeStream() as unknown as MediaStream);
    const k = new MicKeeper(1000, open);
    expect(await k.warm(true)).toBe(false);
    expect(open).not.toHaveBeenCalled();
    vi.stubGlobal("navigator", { permissions: { query: async () => ({ state: "granted" }) } });
    expect(await k.warm(true)).toBe(true);
    expect(open).toHaveBeenCalledTimes(1);
    k.release();
  });
});

describe("the level and the pickup line (pure)", () => {
  it("room noise sits at zero, speech fills it, capped at 1", () => {
    const floor = 0.004;
    expect(levelTarget(0.004, floor)).toBe(0);
    expect(levelTarget(0.007, floor)).toBe(0); // under twice the floor
    expect(levelTarget(0.05, floor)).toBeGreaterThan(0.5);
    expect(levelTarget(0.5, floor)).toBe(1);
    // A louder room: the same voice reads lower.
    expect(levelTarget(0.05, 0.015)).toBeLessThan(levelTarget(0.05, floor));
  });

  it("attack at once, decay with a ~150 ms time constant, never below the target", () => {
    expect(smoothLevel(0.1, 0.8, 16)).toBe(0.8);
    const after = smoothLevel(0.8, 0, LEVEL_DECAY_MS);
    expect(after).toBeCloseTo(0.8 / Math.E, 5);
    expect(smoothLevel(0.8, 0.5, 1000)).toBe(0.5);
    // Frame by frame comes to the same as one step.
    let v = 0.8;
    for (let i = 0; i < 10; i++) v = smoothLevel(v, 0, LEVEL_DECAY_MS / 10);
    expect(v).toBeCloseTo(after, 5);
  });

  it("the noise floor follows the quiet; loud is over RMS_SPEECH and three floors", () => {
    expect(noiseFloor(0, 0.01)).toBe(0.01);
    expect(noiseFloor(0.01, 0.002)).toBe(0.002);
    expect(noiseFloor(0.01, 0.5)).toBeCloseTo(0.0115, 6);
    expect(isLoud(0.019, 0.001)).toBe(false);
    expect(isLoud(0.03, 0.001)).toBe(true);
    expect(isLoud(0.03, 0.012)).toBe(false);
  });

  it("“Hearing you…” while the level says speech, “Listening…” when quiet, nothing before the mic records", () => {
    expect(pickupLine(false, true)).toBe("Listening…"); // from the press, before the mic records
    expect(pickupLine(false, false)).toBe("Listening…");
    expect(pickupLine(true, false)).toBe("Listening…");
    expect(pickupLine(true, true)).toBe("Hearing you…");
    expect(HEARING_HOLD_MS).toBeGreaterThanOrEqual(300);
  });

  it("the live caption keeps only the last ~60 characters of a long dictation, from a word's start", () => {
    expect(LIVE_CAPTION_CHARS).toBe(60);
    expect(liveTail("fix the bug")).toBe("fix the bug");
    const long = Array.from({ length: 40 }, (_, i) => "word" + i).join(" ");
    const tail = liveTail(long, "and");
    expect(tail.startsWith("…word")).toBe(true);
    expect(long.endsWith(tail.slice(1))).toBe(true);
    expect(tail.length + " and".length).toBeLessThanOrEqual(62);
    // A long unsure tail alone: its end, and the settled words keep a sliver.
    expect(liveUnsure("short")).toBe("short");
    const unsure = liveUnsure(long);
    expect(unsure.startsWith("…word")).toBe(true);
    expect(unsure.length).toBeLessThanOrEqual(61);
  });

  it("each streaming pass's server time is kept (the last 20) for the debug console", () => {
    vi.stubGlobal("window", {});
    try {
      for (let i = 1; i <= 25; i++) voicePass(i * 10.4);
      const w = (globalThis as unknown as { window: Window }).window;
      expect(w.__mfVoicePasses).toHaveLength(20);
      expect(w.__mfVoicePasses?.[19]).toBe(260);
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

describe("the caption: what it hears before what it types", () => {
  function hooks() {
    const captions: Array<[string, string]> = [];
    const h: LiveHooks = {
      caption: (t, _s, unsure) => void captions.push([t, unsure || ""]),
      halt: () => {},
      route: async () => false,
      activity: () => "idle",
      replies: () => false,
      submitted: () => {},
    };
    return { h, captions, last: () => captions[captions.length - 1] };
  }

  it("“Listening…” from the press, “Hearing you…” at speech once the mic records, back when quiet — until a word", () => {
    mocks.peekTerm.mockReturnValue({ send: () => true });
    const k = hooks();
    const d = new LiveDictation({ kind: "session", title: "t1", name: "Login fix", type: () => true, erase: () => true }, k.h);
    d.show();
    // From the press — nothing waits on the microphone to say so.
    expect(k.last()[0]).toBe("Listening… · Say “over” to send, “over and out” to stop, “disregard” to start over");
    d.ready();
    expect(k.last()[0]).toBe("Listening… · Say “over” to send, “over and out” to stop, “disregard” to start over");
    d.loud(true);
    expect(k.last()[0]).toMatch(/^Hearing you… · /);
    d.loud(false);
    expect(k.last()[0]).toMatch(/^Listening… · /);
    // Words: the level no longer rewrites the caption.
    d.unsure("fix the");
    const n = k.captions.length;
    d.loud(true);
    expect(k.captions.length).toBe(n);
  });

  it("the partial shows greyed before any commit, typed at once as a guess, and solid once committed", async () => {
    const typed: string[] = [];
    const k = hooks();
    const d = new LiveDictation(
      { kind: "session", title: "t1", name: "Login fix", type: (x) => typed.push(x) > 0, erase: (x) => typed.push("⌫" + x) > 0 },
      k.h
    );
    d.ready();
    d.unsure("fix the log");
    // The caption is the unsure tail alone (shown muted), not the hint.
    expect(k.last()).toEqual(["", "fix the log"]);
    expect(typed).toEqual([]); // unsure() alone is the caption's
    await d.stream("", "fix the log");
    expect(typed).toEqual(["fix the log"]); // the first pass's words, typed
    await d.stream("fix the login", "bug");
    expect(typed).toEqual(["fix the log", "in bug"]);
    expect(k.last()).toEqual(["fix the login", "bug"]); // committed solid, the rest greyed
  });

  it("a text box says where the words go", () => {
    const k = hooks();
    const d = new LiveDictation({ kind: "field", title: "", name: "the text box", type: () => true, erase: () => true }, k.h);
    d.show();
    expect(k.last()[0]).toBe("Listening — the words go into the text box");
    d.ready();
    d.loud(true);
    expect(k.last()[0]).toBe("Hearing you… · the words go into the text box");
  });
});

describe("wiring: warm, measured, and off React's render path", () => {
  const MIC = code("components/TopBarMic.tsx");
  const ASK = code("components/dialogs/AskDialog.tsx");

  it("the mic warms on the options, on the press and for the ask pop-up (permission only)", () => {
    expect(MIC).toMatch(/const openMenu = \(\) => \{\s*setMenuOpen\(true\);\s*warmMic\(true\);/);
    expect(MIC).toMatch(/onPointerDown=\{\(e\) => \{[\s\S]{0,600}if \(!live\.current\) warmMic\(true\);/);
    expect(MIC).toContain("warm: () => warmMic(false)");
    expect(MIC).toContain("void serverRec.mic.warm(!gesture)");
    expect(ASK).toContain("useEffect(() => askMic()?.warm?.(), [title]);");
  });

  it("each block feeds the meter, never React state; the ring is one shared class", () => {
    const onLevel = MIC.slice(MIC.indexOf("onLevel: (rms) =>"), MIC.indexOf("onSpeech: () =>"));
    expect(onLevel).toContain("meterFeed(rms)");
    expect(onLevel).not.toMatch(/\bset[A-Z]|\bsay\(/);
    expect(MIC).toContain("useEffect(() => meterRef(btnRef.current), []);");
    expect(MIC).toContain('"mic-ring"');
    expect(ASK).toContain('"ask-mic tb-item mic-ring"');
    expect(ASK).toContain("ref={meterRef}");
    const css = src("components/TopBarMic.css");
    // Still: the ring's opacity is the level's step, eased; never a size.
    expect(cssRule(css, ".mic-ring.listening::after")).toContain("opacity: var(--mic-step, 0.45)");
    expect(cssRule(css, ".mic-ring::after")).toContain("transition: opacity 150ms ease-out");
    expect(css).not.toMatch(/scale\(/);
    expect(css).not.toMatch(/animation:/);
    const lib = code("lib/voiceMic.ts");
    expect(lib).toContain('el.style.setProperty("--mic-step", v)');
    expect(lib).not.toContain('"--mic-level"');
    expect(lib).toContain("requestAnimationFrame(f)");
  });

  it("no live words or level bar anywhere on the page: the words are in the terminal", () => {
    expect(MIC).not.toContain("mic-level");
    expect(MIC).not.toContain('id="mic-live"');
    expect(MIC).not.toContain("mic-caption");
    expect(MIC).not.toContain("showCaption");
  });

  it("the timing marks, from the press to the first commit", () => {
    for (const m of ["start", "partial", "commit"]) expect(MIC).toContain('voiceMark("' + m + '")');
    expect(MIC).toContain('if (on) voiceMark("speech")');
    const lib = code("lib/voiceDesktop.ts");
    // The first characters typed (a guess, usually), and each pass's server time.
    expect(lib).toContain('voiceMark("typed")');
    expect(lib).toContain("voicePass(ms)");
    expect(lib).toContain('voiceMark("stream")');
    expect(lib).toContain('voiceMark("mic")');
    expect(code("lib/voiceMic.ts")).toContain('localStorage.getItem("cs_voice_debug") === "1"');
  });
});
