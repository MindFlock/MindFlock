/** "Over" sends when the accurate pass is held back (owner report
 *  2026-11-10: memory critically low, every /end answered with the fast
 *  text — "the accurate pass failed (local recognition held back: …); the
 *  fast text stands" — and "over" / "over and out" sent nothing).
 *
 *  End to end: the server recognizer (its stream, its /end) wired into a
 *  LiveDictation the way TopBarMic wires them. */

import { afterEach, describe, expect, it, vi } from "vitest";
import { CONTROL_TAIL_MS, LiveDictation, finalClock, serverRecognizer, type DictTarget, type LiveHooks } from "../lib/voiceDesktop";

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
  mocks.api.mockReset();
  mocks.instApi.mockReset();
  mocks.startWavRecording.mockReset();
  finalClock.reset();
});

class FakeCtx {
  state = "running";
  sampleRate = 48000;
  resume() {
    return Promise.resolve();
  }
}
const resp = (status: number, body: unknown) => ({ ok: status < 300, status, json: async () => body });
const wav = () => new Blob([new Uint8Array(44 + 3200)], { type: "audio/wav" });

function session(): DictTarget & { keys: string[] } {
  const keys: string[] = [];
  return { kind: "session", title: "t1", name: "Login fix", keys, type: (t) => keys.push(t) > 0, erase: (t) => keys.push("⌫" + t) > 0 };
}

/** The recognizer and a dictation on it; `chunk`: the one chunk's answer,
 * `end`: the /end answer — `endDelay` ms late (a busy server); `quietTo`:
 * the quiet the meter has reached when "over" is checked (it grows on). */
function rig(chunk: unknown, end: unknown, o: { endDelay?: number; quietTo?: number } = {}) {
  vi.stubGlobal("window", { AudioContext: FakeCtx });
  const getUserMedia = vi.fn(async () => ({ active: true, getTracks: () => [{ stop: vi.fn() }] }));
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
  let chunks = 0;
  mocks.apiRaw.mockImplementation(async (url: string, init: RequestInit) => {
    calls.push(String(init?.method || "GET") + " " + url);
    if (url === "/api/voice/stream") return resp(200, { id: "s1", engine: "moonshine" });
    if (url.endsWith("/chunk")) return resp(200, chunks++ ? { committed: "", partial: "" } : chunk);
    if (url.endsWith("/end")) {
      if (o.endDelay) await new Promise((r) => setTimeout(r, o.endDelay));
      return resp(200, end);
    }
    return resp(204, {});
  });
  mocks.api.mockResolvedValue({ kind: "prompt" });
  mocks.instApi.mockResolvedValue({ ok: true });
  const r = serverRecognizer({ stream: () => true });
  let quiet = 0;
  let quietFrom = 0;
  let loudAt = now;
  const said: string[] = [];
  const t = session();
  const hooks: LiveHooks = {
    caption: () => {},
    halt: () => {},
    route: async () => false,
    activity: () => "idle",
    replies: () => false,
    submitted: () => {},
    done: () => {},
    confirm: (k) => void said.push(k),
    quietMs: () => (quietFrom ? quiet + (performance.now() - quietFrom) : quiet),
    loudAt: () => loudAt,
    endPhrase: () => r.endPhrase?.(),
    finalMs: () => r.finalMs,
    endsSurely: () => r.streaming,
    spokeAt: () => r.spokeAt,
  };
  const d = new LiveDictation(t, hooks, { continuous: true });
  r.start({
    onInterim: () => {},
    onFinal: (x, at) => void d.heard(x, at),
    onStream: (w, p) => void d.stream(w, p),
    onFlush: (x, f, at) => void d.flush(x, f, at),
    onPartial: (x) => d.unsure(x),
    onReady: () => d.ready(),
    onError: () => {},
    onEnd: () => {},
  });
  const speak = async () => {
    await vi.waitFor(() => expect(clips.length).toBe(1));
    await vi.waitFor(() => expect(calls).toContain("POST /api/voice/stream"));
    now += 100;
    clips[0].opts.onLevel!(0.001); // the room
    clips[0].feed();
    now += 100;
    clips[0].opts.onLevel!(0.3);
    loudAt = now;
    await vi.waitFor(() => expect(calls).toContain("POST /api/voice/stream/s1/chunk"));
    // The voice stops: "over" at the end of the guess, the quiet growing.
    quiet = o.quietTo ?? CONTROL_TAIL_MS + 50;
    quietFrom = performance.now();
  };
  return { r, d, t, said, calls, speak };
}

/** The /end a Moonshine stream answers when the whisper pass was held back
 * (speech_moonshine.MoonshineStream.finish: Moonshine's own text as the
 * final; `committed`, the words the chunks had not — pinned server-side in
 * tests/unit/test_voice_held_back_over.py). */
const heldBack = (text: string, committed: string) => ({
  committed,
  partial: "",
  final: text,
  heard: text,
  final_ms: 3,
  final_wait_ms: 0,
  final_early: false,
  same: true,
  final_by: "moonshine",
});

describe("the accurate pass held back by memory: the fast text sends", () => {
  it("“over”: the fast words go, the control word stripped, and “Sent.”", async () => {
    const x = rig({ committed: "ship it", partial: "over" }, heldBack("ship it over", "over"));
    await x.speak();
    await vi.waitFor(() => expect(x.calls).toContain("POST /api/voice/stream/s1/end"));
    await vi.waitFor(() => expect(mocks.instApi).toHaveBeenCalledWith("t1", "/send", { json: { enter: true, say: false } }), { timeout: 3000 });
    expect(x.t.keys.join("")).toBe("ship it");
    expect(x.said).toEqual(["sent"]);
    expect(x.d.active).toBe(true);
    x.r.stop();
  });

  it("“over and out”: sends and stops", async () => {
    const x = rig({ committed: "ship it over", partial: "and out" }, heldBack("ship it over and out", "and out"));
    await x.speak();
    await vi.waitFor(() => expect(mocks.instApi).toHaveBeenCalledWith("t1", "/send", { json: { enter: true, say: false } }), { timeout: 3000 });
    expect(x.t.keys.join("")).toBe("ship it");
    expect(x.said).toEqual(["out"]);
    expect(x.d.active).toBe(false);
    x.r.stop();
  });

  it("a whisper stream (no Moonshine): /end without a final still sends", async () => {
    const x = rig({ committed: "ship it", partial: "over" }, { committed: "over", partial: "" });
    await x.speak();
    await vi.waitFor(() => expect(mocks.instApi).toHaveBeenCalledWith("t1", "/send", { json: { enter: true, say: false } }), { timeout: 3000 });
    expect(x.t.keys.join("")).toBe("ship it");
    expect(x.said).toEqual(["sent"]);
    x.r.stop();
  });

  const late: Array<[string, { endDelay?: number; quietTo?: number }]> = [
    ["/end later than the wait (a busy server): the fast words go at its end", { endDelay: 2000 }],
    ["“over” ended on the first 100 ms of quiet: it sends once the quiet is met", { quietTo: 150 }],
    ["both", { quietTo: 150, endDelay: 2000 }],
  ];
  for (const [name, o] of late) {
    it(name, async () => {
      const x = rig({ committed: "ship it", partial: "over" }, heldBack("ship it over", "over"), o);
      await x.speak();
      await vi.waitFor(() => expect(mocks.instApi).toHaveBeenCalledWith("t1", "/send", { json: { enter: true, say: false } }), { timeout: 3000 });
      await new Promise((r) => setTimeout(r, o.endDelay ? o.endDelay + 200 : 200)); // the late /end changes nothing
      expect(mocks.instApi).toHaveBeenCalledTimes(1);
      expect(x.t.keys.join("")).toBe("ship it");
      expect(x.said).toEqual(["sent"]);
      x.r.stop();
    }, 10_000);
  }
});
