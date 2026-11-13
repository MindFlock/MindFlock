/** Moonshine detects, whisper writes (owner rule 2026-11-08; the fallback
 *  closed 2026-11-12). On a Moonshine stream the live words are a preview:
 *  a phrase whisper did not write types nothing — what the preview typed is
 *  taken back, as a final replaces it — and nothing of it is sent; a stop
 *  phrase with words before it waits for whisper's words; letting go with a
 *  phrase in flight leaves nothing of it. A whisper stream (or Web Speech)
 *  keeps its fast words as ever. The end-to-end "over" cases with the
 *  server recognizer are in voiceHeldBack.test.ts. */

import { afterEach, describe, expect, it, vi } from "vitest";
import MIC_SRC from "../components/TopBarMic.tsx?raw";
import {
  ACCURATE_OFF_HINT,
  DEFAULT_VOICE_CFG,
  Dictation,
  LiveDictation,
  accurateOff,
  missedLine,
  type DictTarget,
  type LiveHooks,
} from "../lib/voiceDesktop";

const mocks = vi.hoisted(() => ({ api: vi.fn(), apiRaw: vi.fn(), instApi: vi.fn(), peekTerm: vi.fn() }));
vi.mock("../api/client", () => ({ api: mocks.api, apiRaw: mocks.apiRaw, instApi: mocks.instApi, ApiError: Error }));
vi.mock("../lib/terminals", () => ({ peekTerm: mocks.peekTerm }));

afterEach(() => {
  vi.useRealTimers();
  mocks.instApi.mockReset();
});

/** A Dictation as LiveDictation steps it on a Moonshine stream. */
function moon(): Dictation {
  const d = new Dictation();
  d.begin();
  d.routes = false; // a text box's rules: no router in the way
  d.liveText = false;
  return d;
}

describe("Dictation on Moonshine: the preview is never the text", () => {
  it("missed(): the guess is taken back, nothing typed, earlier text stays", () => {
    const d = moon();
    expect(d.heard("fix the login bug")).toEqual({ kind: "type", text: "fix the login bug " });
    d.wrote("fix the login bug ");
    const g = d.stream("and add", "a test");
    expect(g).toMatchObject({ kind: "type", text: "and add a test", guess: true });
    d.wrote("and add a test");
    expect(d.missed("timeout")).toEqual({ kind: "missed", reason: "timeout", erase: "and add a test" });
    d.unwrote("and add a test");
    expect(d.typed).toBe("fix the login bug ");
    expect(d.listening).toBe(true);
    // The next phrase starts afresh after it.
    expect(d.heard("then push")).toEqual({ kind: "type", text: "then push " });
  });

  it("missed() with nothing typed of the phrase takes nothing back; `sending` when “over” waited", () => {
    const d = moon();
    expect(d.missed("failed", true)).toEqual({ kind: "missed", reason: "failed", sending: true });
  });

  it("a stop phrase with words before it waits for the phrase's end (whisper's words)", () => {
    const d = moon();
    const out = d.stream("leave it there stop recording", "");
    // Not acted on from the preview: still listening, the stop phrase held back.
    expect(d.listening).toBe(true);
    expect(out).toMatchObject({ kind: "type", text: "leave it there", guess: true });
    d.wrote("leave it there");
    // Whisper's words at the end: the stop acts, with THEM before it.
    expect(d.flush("", "Leave it there. Stop recording.")).toMatchObject({ kind: "leave", stop: true });
    expect(d.listening).toBe(false);
  });

  it("a lone stop phrase still acts at once (a control phrase: Moonshine's to hear)", () => {
    const d = moon();
    expect(d.stream("stop recording", "")).toMatchObject({ kind: "leave", stop: true });
    expect(d.listening).toBe(false);
  });

  it("a whisper stream's stop phrase acts on the fast words, as before", () => {
    const d = moon();
    d.liveText = true;
    expect(d.stream("leave it there stop recording", "")).toMatchObject({ kind: "leave", stop: true, text: "leave it there " });
  });

  it("letting go with a phrase in flight: its guess is taken back, listening ends", () => {
    const d = moon();
    d.stream("ship the", "fix");
    d.wrote("ship the fix");
    expect(d.finish()).toEqual({ kind: "leave", text: "", missed: true, erase: "ship the fix" });
    expect(d.listening).toBe(false);
  });

  it("silence with a phrase in flight: the same", () => {
    const d = moon();
    d.stream("ship the", "fix");
    d.wrote("ship the fix");
    expect(d.expire()).toEqual({ kind: "leave", text: "", missed: true, erase: "ship the fix" });
  });

  it("letting go between phrases is unchanged: the words typed stay", () => {
    const d = moon();
    d.heard("ship the fix");
    d.wrote("ship the fix ");
    expect(d.finish()).toEqual({ kind: "leave", text: "" });
  });
});

describe("missedLine: short, says what happened and what to do", () => {
  it("a miss: say it again; with a send: nothing sent", () => {
    expect(missedLine("timeout")).toBe("Recognition didn't finish — say it again");
    expect(missedLine("failed", true)).toBe("Recognition didn't finish — nothing sent; say it again");
    expect(missedLine("empty")).toBe(missedLine("skipped"));
  });

  it("the accurate pass off: where to turn it on", () => {
    expect(missedLine("off")).toBe("Nothing typed — whisper's accurate pass is off: Settings → Voice");
    expect(missedLine("off", true)).toMatch(/^Nothing sent/);
  });
});

describe("LiveDictation.missed: takes back the preview, captions, listens on", () => {
  function rig(liveIsText: boolean) {
    const keys: string[] = [];
    const lines: string[] = [];
    const t: DictTarget = { kind: "field", title: "", name: "the box", type: (x) => keys.push(x) > 0, erase: (x) => keys.push("⌫" + x) > 0 };
    const hooks: LiveHooks = {
      caption: (line) => void lines.push(line),
      halt: () => {},
      route: async () => false,
      activity: () => "idle",
      replies: () => false,
      submitted: () => {},
      liveIsText: () => liveIsText,
    };
    return { d: new LiveDictation(t, hooks, { continuous: true }), keys, lines };
  }

  it("a Moonshine phrase whisper did not write", async () => {
    const x = rig(false);
    await x.d.stream("fix the", "bug");
    expect(x.keys).toEqual(["fix the bug"]);
    await x.d.missed("timeout");
    expect(x.keys).toEqual(["fix the bug", "⌫fix the bug"]);
    expect(x.lines.at(-1)).toBe(missedLine("timeout"));
    expect(x.d.active).toBe(true);
  });

  it("letting go right after a miss keeps saying so", async () => {
    const x = rig(false);
    await x.d.stream("fix the", "bug");
    await x.d.missed("failed");
    await x.d.finish();
    expect(x.lines.at(-1)).toBe(missedLine("failed"));
    expect(x.d.active).toBe(false);
  });
});

describe("the accurate pass off: the mic says so as it starts", () => {
  const MOON = { ...DEFAULT_VOICE_CFG, server_ready: { stt_stream: true }, stream_engine: "moonshine" };
  it("accurateOff: streaming on Moonshine with no accurate model", () => {
    expect(accurateOff({ ...MOON, final_model: "" })).toBe(true);
    expect(accurateOff({ ...MOON, final_model: "small.en" })).toBe(false);
    expect(accurateOff(MOON)).toBe(false); // an older server: no word
    expect(accurateOff({ ...MOON, stream_engine: "whisper", final_model: "" })).toBe(false);
    expect(accurateOff({ ...MOON, server_ready: {}, final_model: "" })).toBe(false);
    expect(ACCURATE_OFF_HINT).toMatch(/Settings → Voice$/);
  });
  it("TopBarMic hands it to the dictation as the caption before a word", () => {
    expect(MIC_SRC).toMatch(/hint: engine === "server" && !opts\.assistant && accurateOff\(c\) \? ACCURATE_OFF_HINT : opts\.hint/);
  });
});

describe("TopBarMic wires it", () => {
  it("liveIsText follows the server recognizer's engine; onMissed goes to the dictation", () => {
    expect(MIC_SRC).toMatch(/liveIsText: \(\) => !\(r === serverRec && serverRec\.moonshine\)/);
    expect(MIC_SRC).toMatch(/onMissed: \(reason, at\) => \{[\s\S]{0,80}void d\.missed\(reason, at\)/);
  });
});
