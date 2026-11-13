/** "Over" when whisper's accurate pass does not write the phrase (owner
 *  report 2026-11-10: memory critically low, every pass refused; rule
 *  2026-11-12: Moonshine detects, whisper writes). Moonshine's live words
 *  are a preview: a phrase whisper did not write (/end `final_missed`) types
 *  nothing — its preview is taken back — and "over" sends nothing of it;
 *  the caption says to say it again. "Over" waits for whisper's words
 *  however late they come, and a whisper stream's fast words still stand.
 *
 *  End to end: the server recognizer (its stream, its /end) wired into a
 *  LiveDictation the way TopBarMic wires them. */

import { afterEach, describe, expect, it, vi } from "vitest";
import {
  CONTROL_TAIL_MS,
  LiveDictation,
  finalClock,
  finishWaitMs,
  missedLine,
  serverRecognizer,
  CLIP_FINISH_WAIT_MS,
  type DictTarget,
  type LiveHooks,
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

function session(): DictTarget & { keys: string[]; text(): string } {
  const keys: string[] = [];
  // What is in the field: each erase takes its characters back off the end.
  const text = () =>
    keys.reduce((acc, k) => (k.startsWith("⌫") ? acc.slice(0, Math.max(0, acc.length - Array.from(k.slice(1)).length)) : acc + k), "");
  return { kind: "session", title: "t1", name: "Login fix", keys, text, type: (t) => keys.push(t) > 0, erase: (t) => keys.push("⌫" + t) > 0 };
}

/** The recognizer and a dictation on it; `chunk`: the one chunk's answer,
 * `end`: the /end answer — `endDelay` ms late (a busy server); `quietTo`:
 * the quiet the meter has reached when "over" is checked (it grows on). */
type Api = (url: string) => Promise<ReturnType<typeof resp>> | null;
function rig(chunk: unknown, end: unknown, o: { endDelay?: number; quietTo?: number; engine?: string; api?: Api } = {}) {
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
    const own = o.api?.(url);
    if (own) return own;
    if (url === "/api/voice/stream") return resp(200, { id: "s1", engine: o.engine ?? "moonshine" });
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
  const lines: string[] = [];
  const t = session();
  const hooks: LiveHooks = {
    caption: (line) => void lines.push(line),
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
    liveIsText: () => !r.moonshine,
    spokeAt: () => r.spokeAt,
  };
  const d = new LiveDictation(t, hooks, { continuous: true });
  r.start({
    onInterim: () => {},
    onFinal: (x, at) => void d.heard(x, at),
    onStream: (w, p) => void d.stream(w, p),
    onFlush: (x, f, at) => void d.flush(x, f, at),
    onMissed: (why, at) => void d.missed(why, at),
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
  /** Speech into the `i`th recording (a later phrase). */
  const talk = (i: number) => {
    now += 100;
    clips[i].opts.onLevel!(0.001);
    clips[i].feed();
    now += 100;
    clips[i].opts.onLevel!(0.3);
    loudAt = now;
  };
  return { r, d, t, said, lines, calls, clips, speak, talk };
}

/** The /end a Moonshine stream answers when whisper wrote no text (the
 * pass refused, failed, timed out: speech_moonshine.MoonshineStream.finish —
 * no final, no committed words, the reason; pinned server-side in
 * tests/unit/test_voice_held_back_over.py). */
const missedEnd = (heard: string, why = "failed") => ({ committed: "", partial: "", heard, final_missed: why });
/** …and when whisper wrote it (`over` committed at /end). */
const whisperEnd = (final: string) => ({
  committed: "over",
  partial: "",
  final,
  heard: "ship it over",
  final_ms: 900,
  final_wait_ms: 400,
  final_early: true,
  same: false,
  final_by: "whisper",
});
const SEND = ["t1", "/send", { json: { enter: true, say: false } }] as const;

describe("whisper wrote nothing: Moonshine's preview is taken back, nothing is sent", () => {
  it("“over”: the guess goes, no Enter, “nothing sent; say it again”, listening goes on", async () => {
    const x = rig({ committed: "ship it", partial: "over" }, missedEnd("ship it over"));
    await x.speak();
    await vi.waitFor(() => expect(x.calls).toContain("POST /api/voice/stream/s1/end"));
    await vi.waitFor(() => expect(x.lines).toContain(missedLine("failed", true)), { timeout: 3000 });
    expect(x.t.keys.join("")).toContain("ship it"); // the preview was typed…
    expect(x.t.text()).toBe(""); // …and taken back
    expect(mocks.instApi).not.toHaveBeenCalledWith(...SEND);
    expect(x.said).toEqual([]);
    expect(x.d.active).toBe(true);
    x.r.stop();
  });

  it("“over and out”: nothing is sent either", async () => {
    const x = rig({ committed: "ship it over", partial: "and out" }, missedEnd("ship it over and out", "timeout"));
    await x.speak();
    await vi.waitFor(() => expect(x.lines).toContain(missedLine("timeout", true)), { timeout: 3000 });
    expect(x.t.text()).toBe("");
    expect(mocks.instApi).not.toHaveBeenCalledWith(...SEND);
    x.r.stop();
  });

  it("the accurate pass off: the caption says where to turn it on", async () => {
    const x = rig({ committed: "ship it", partial: "over" }, missedEnd("ship it over", "off"));
    await x.speak();
    await vi.waitFor(() => expect(x.lines).toContain(missedLine("off", true)), { timeout: 3000 });
    expect(missedLine("off", true)).toMatch(/Settings → Voice/);
    expect(x.t.text()).toBe("");
    x.r.stop();
  });

  it("a whisper stream (no Moonshine): /end without a final — its fast words still send", async () => {
    const x = rig({ committed: "ship it", partial: "over" }, { committed: "over", partial: "" }, { engine: "whisper" });
    await x.speak();
    await vi.waitFor(() => expect(mocks.instApi).toHaveBeenCalledWith(...SEND), { timeout: 3000 });
    expect(x.t.keys.join("")).toBe("ship it");
    expect(x.said).toEqual(["sent"]);
    x.r.stop();
  });

  const late: Array<[string, { endDelay?: number; quietTo?: number }]> = [
    ["/end later than the old wait (a busy server): “over” waits for whisper's words", { endDelay: 2000 }],
    ["“over” ended on the first 100 ms of quiet: it sends once the quiet is met", { quietTo: 150 }],
    ["both", { quietTo: 150, endDelay: 2000 }],
  ];
  for (const [name, o] of late) {
    it(name, async () => {
      const x = rig({ committed: "ship it", partial: "over" }, whisperEnd("Ship it, over."), o);
      await x.speak();
      await vi.waitFor(() => expect(mocks.instApi).toHaveBeenCalledWith(...SEND), { timeout: 4000 });
      await new Promise((r) => setTimeout(r, 200));
      expect(mocks.instApi).toHaveBeenCalledTimes(1);
      // Whisper's words went, never the preview's: "ship it" was corrected.
      expect(x.t.text()).toBe("Ship it ");
      expect(x.said).toEqual(["sent"]);
      x.r.stop();
    }, 10_000);
  }

  it("a late /end that writes nothing: still nothing sent, however late", async () => {
    const x = rig({ committed: "ship it", partial: "over" }, missedEnd("ship it over"), { endDelay: 2000 });
    await x.speak();
    await vi.waitFor(() => expect(x.lines).toContain(missedLine("failed", true)), { timeout: 4000 });
    expect(x.t.text()).toBe("");
    expect(mocks.instApi).not.toHaveBeenCalledWith(...SEND);
    x.r.stop();
  }, 10_000);

  it("finals that seldom change words do not let the preview go on its own", async () => {
    for (let i = 0; i < 5; i++) finalClock.verdict(false); // fastStands() would say yes
    const x = rig({ committed: "ship it over", partial: "" }, missedEnd("ship it over"));
    await x.speak();
    await vi.waitFor(() => expect(x.lines).toContain(missedLine("failed", true)), { timeout: 3000 });
    expect(mocks.instApi).not.toHaveBeenCalledWith(...SEND);
    x.r.stop();
  });

  it("letting go waits for a Moonshine phrase's /end, not the finals' budget", () => {
    expect(finishWaitMs({ name: "server", streaming: true, moonshine: true, finalMs: 300 })).toBe(CLIP_FINISH_WAIT_MS);
    expect(finishWaitMs({ name: "server", streaming: true, finalMs: 300 })).toBeLessThan(CLIP_FINISH_WAIT_MS);
  });

  it("the next phrase's words wait for a Moonshine phrase's whisper words — never its preview", async () => {
    let opens = 0;
    const api: Api = (url) => {
      if (url === "/api/voice/stream") return Promise.resolve(resp(200, { id: "s" + ++opens, engine: "moonshine" }));
      if (url === "/api/voice/stream/s2/chunk") return Promise.resolve(resp(200, { committed: "and test it", partial: "" }));
      if (url === "/api/voice/stream/s1/end")
        return new Promise((r) =>
          setTimeout(
            () => r(resp(200, { committed: "", partial: "", final: "Fix the bug.", heard: "fix the bug", final_ms: 700, same: false, final_by: "whisper" })),
            800
          )
        );
      return null;
    };
    const x = rig({ committed: "fix the", partial: "bug" }, {}, { api, quietTo: 0 });
    await x.speak();
    x.r.endPhrase?.(); // a pause ends the first phrase; the next records at once
    await vi.waitFor(() => expect(x.clips.length).toBe(2));
    await vi.waitFor(() => expect(x.calls.filter((c) => c === "POST /api/voice/stream").length).toBe(2));
    x.talk(1);
    await vi.waitFor(() => expect(x.calls).toContain("POST /api/voice/stream/s2/chunk"));
    await vi.waitFor(() => expect(x.t.text()).toBe("Fix the bug. and test it"), { timeout: 3000 });
    x.r.stop();
  }, 10_000);
});
