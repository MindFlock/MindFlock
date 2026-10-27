/** Three desktop voice fixes, pinned:
 *  - the accurate final: /end's `final` (the whole phrase re-heard by the
 *    server's accurate model) retypes the WHOLE typed phrase — never past its
 *    start — and the radio words / router decide on it; `same` and an absent
 *    final leave the fast words;
 *  - no box: the bar's only voice words are the pill (Listening… / Speaking…
 *    / Quiet.), no caption element, and the options open only on a click;
 *  - speech by sentence: the server speaker asks for one sentence at a time,
 *    plays the first the moment it is back while the next is synthesised,
 *    gapless, and cancel drops the rest. */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  Dictation,
  HUSH_CAPTION_MS,
  HUSH_LINE,
  LiveDictation,
  SPEECH_SENTENCE_MIN,
  SpeechQueue,
  finalLine,
  micPill,
  serverRecognizer,
  serverSpeaker,
  speechSentences,
  type DictTarget,
  type LiveHooks,
  type RecognizerHandlers,
  type Speaker,
} from "../lib/voiceDesktop";
import { placeUnder } from "../lib/menuPlace";
import { LEVEL_SMOOTH_MS, STEP_OPACITY, levelStep, slowLevel } from "../lib/voiceMic";
import { code, cssRule, src } from "./sourceText";

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
  mocks.startWavRecording.mockReset();
});

/** A context whose clock is the (fake) wall clock, recording each start —
 * the one class every test here stubs: the page's AudioContext is made once
 * (lib/voiceDesktop voiceAudioCtx) and kept. */
let T0 = 0;
const starts: Array<{ text: string; at: number; called: number }> = [];
const stopped: string[] = [];
class ClockCtx {
  state = "running";
  sampleRate = 48000;
  destination = {};
  get currentTime() {
    return (Date.now() - T0) / 1000;
  }
  resume() {
    return Promise.resolve();
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
        starts.push({ text: node.buffer!.text, at, called: ctx.currentTime });
        setTimeout(() => node.onended?.(), (Math.max(at, ctx.currentTime) - ctx.currentTime + node.buffer!.duration) * 1000);
      },
      stop() {
        stopped.push(node.buffer!.text);
      },
    };
    return node;
  }
}

// --- 1. the accurate final ------------------------------------------------- //

/** A text box-like buffer: what is in it after every key. */
function bufferTarget(kind: "session" | "field" = "session"): DictTarget & { buf: string; keys: string[] } {
  const t = {
    kind,
    title: kind === "session" ? "s" : "",
    name: "s",
    buf: "",
    keys: [] as string[],
    type(text: string) {
      t.buf += text;
      t.keys.push("+" + text);
      return true;
    },
    erase(text: string) {
      if (!t.buf.endsWith(text)) return false;
      t.buf = t.buf.slice(0, t.buf.length - text.length);
      t.keys.push("-" + text);
      return true;
    },
  };
  return t;
}

function hooks(over: Partial<LiveHooks> = {}): LiveHooks & { routed: string[] } {
  const routed: string[] = [];
  return {
    routed,
    caption: () => {},
    halt: () => {},
    route: async (text) => {
      routed.push(text);
      return false;
    },
    activity: () => "idle",
    replies: () => false,
    submitted: () => {},
    ...over,
  };
}

describe("the accurate final retypes the whole phrase", () => {
  it("the fast guess, then /end's final: the whole phrase corrected, the phrase before it untouched", async () => {
    const t = bufferTarget();
    const d = new LiveDictation(t, hooks());
    await d.stream("hello there", "");
    await d.flush("", undefined); // phrase 1: the fast words stand
    expect(t.buf).toBe("hello there ");
    await d.stream("fix the", "bag in");
    expect(t.buf).toBe("hello there fix the bag in");
    // The accurate model heard it otherwise, from its first word.
    await d.flush("log in", "Fix the bug in the login.");
    expect(t.buf).toBe("hello there Fix the bug in the login. ");
    // Bounded at the phrase's start: phrase 1 was never erased.
    expect(t.keys.filter((k) => k.startsWith("-")).every((k) => !k.includes("hello"))).toBe(true);
  });

  it("only the tail that differs is backspaced (a correction, not a wipe)", async () => {
    const t = bufferTarget();
    const d = new LiveDictation(t, hooks());
    await d.stream("hello", "");
    await d.flush("");
    await d.stream("fix the login", "bag");
    await d.flush("", "fix the login bug");
    expect(t.buf).toBe("hello fix the login bug ");
    expect(t.keys.slice(-2)).toEqual(["-ag", "+ug "]); // from where they differ
  });

  it("no final (timeout / off): the fast words stand", async () => {
    const t = bufferTarget();
    const d = new LiveDictation(t, hooks());
    await d.stream("hello", "");
    await d.flush("");
    await d.stream("fix the", "bag");
    await d.flush("bag");
    expect(t.buf).toBe("hello fix the bag ");
  });

  it("a text box gets its value set once per correction", async () => {
    const el = { value: "", selectionStart: 0, selectionEnd: 0 };
    const sets: string[] = [];
    const field: DictTarget = {
      kind: "field",
      title: "",
      name: "the text box",
      type: () => false,
      erase: () => false,
      replace: (old, text) => {
        const at = el.value.length;
        const from = old && el.value.endsWith(old) ? at - old.length : at;
        el.value = el.value.slice(0, from) + text;
        sets.push(el.value);
        return true;
      },
    };
    const d = new LiveDictation(field, hooks());
    await d.stream("fix the", "bag");
    await d.flush("", "fix the bug");
    expect(el.value).toBe("fix the bug ");
    expect(sets.at(-1)).toBe("fix the bug ");
  });

  it("the control phrases are decided on the final, not the fast guess", () => {
    // Fast "…overalls" is no radio word; the accurate "…over." is.
    const a = new Dictation();
    a.begin();
    a.routes = false;
    a.stream("ship it", "overalls");
    expect(a.flush("", "Ship it, over.").kind).toBe("send");
    // And the other way: fast "over" held back, the final says "overall".
    const b = new Dictation();
    b.begin();
    b.routes = false;
    const g = b.stream("fix the login over", "");
    expect(g.kind === "type" && g.text).toBe("fix the login");
    b.wrote("fix the login");
    const out = b.flush("", "fix the login overall");
    expect(out).toEqual({ kind: "type", text: " overall " });
    expect(b.listening).toBe(true);
  });

  it("a first phrase goes to the router as the final's words", async () => {
    const h = hooks({
      route: async (text) => {
        h.routed.push(text);
        return true;
      },
    });
    const t = bufferTarget();
    const d = new LiveDictation(t, h);
    await d.stream("which to the", "log in fix");
    await d.flush("", "switch to the login fix");
    expect(h.routed).toEqual(["switch to the login fix"]);
    expect(t.buf).toBe(""); // the router acted: nothing stays typed
  });

  describe("the recognizer's /end", () => {
    const resp = (status: number, body: unknown) => ({ ok: status < 300, status, json: async () => body });
    const wav = () => new Blob([new Uint8Array(44 + 3200)], { type: "audio/wav" });

    /** One streamed phrase whose /end answers `end`; what onFlush got. */
    async function phrase(end: Record<string, unknown>) {
      vi.stubGlobal("window", { AudioContext: ClockCtx });
      vi.stubGlobal("navigator", { language: "en-US", userAgent: "", mediaDevices: { getUserMedia: async () => ({ active: true, getTracks: () => [] }) } });
      let now = 1_000_000;
      vi.spyOn(Date, "now").mockImplementation(() => now);
      const clips: Array<{ opts: { onLevel?: (rms: number) => void }; feed(): void }> = [];
      mocks.startWavRecording.mockImplementation((_s: unknown, _c: unknown, opts) => {
        let ready = false;
        const clip = {
          opts,
          take: vi.fn(() => (ready ? ((ready = false), wav()) : null)),
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
      mocks.apiRaw.mockImplementation(async (url: string) => {
        if (url === "/api/voice/stream") return resp(200, { id: "s1" });
        if (url.endsWith("/chunk")) return resp(200, { committed: "fix the", partial: "bag" });
        if (url.endsWith("/end")) return resp(200, end);
        return resp(204, {});
      });
      const flushed: Array<[string, string | undefined]> = [];
      const h: RecognizerHandlers = {
        onInterim: () => {},
        onFinal: () => {},
        onStream: () => {},
        onFlush: (t, f) => void flushed.push([t, f]),
        onError: () => {},
        onEnd: () => {},
      };
      const r = serverRecognizer({ stream: () => true });
      r.start(h);
      await vi.waitFor(() => expect(clips.length).toBe(1));
      const block = (loud: boolean) => {
        now += 100;
        clips[0].opts.onLevel!(loud ? 0.3 : 0.001);
      };
      block(false);
      block(true);
      await vi.waitFor(() => expect(mocks.apiRaw).toHaveBeenCalled());
      await Promise.resolve();
      clips[0].feed();
      block(true);
      for (let i = 0; i < 12; i++) block(false); // the pause ends the phrase
      await vi.waitFor(() => expect(flushed.length).toBe(1));
      r.stop();
      return { flushed, r };
    }

    it("final that differs → onFlush(fast, final); its time kept for the diagnostics", async () => {
      const { flushed, r } = await phrase({ committed: "bag", partial: "", final: "fix the bug", final_ms: 1200, same: false });
      expect(flushed).toEqual([["bag", "fix the bug"]]);
      expect(r.finalMs).toBe(1200);
      expect(finalLine(r.finalMs)).toBe("final 1.2 s");
      expect(finalLine(null)).toBe("");
    });

    it("`same`: nothing to retype", async () => {
      const { flushed } = await phrase({ committed: "bag", partial: "", final: "fix the bag", final_ms: 900, same: true });
      expect(flushed).toEqual([["bag", undefined]]);
    });

    it("no final: the fast words stand", async () => {
      const { flushed } = await phrase({ committed: "bag", partial: "" });
      expect(flushed).toEqual([["bag", undefined]]);
    });
  });

  it("the mic hands the final to the dictation, and the diagnostics show its time", () => {
    const MIC = code("components/TopBarMic.tsx");
    expect(MIC).toContain("onFlush: (t, final) => {");
    expect(MIC).toContain("void d.flush(t, final)");
    expect(MIC).toContain("finalLine(serverRec.finalMs)");
  });
});

// --- 2. no box: the pill ------------------------------------------------------ //

describe("the bar shows a state pill, never a transcript", () => {
  it("the pill's states", () => {
    expect(micPill({ listening: false, speaking: false, quiet: false })).toBe("");
    expect(micPill({ listening: true, speaking: false, quiet: false })).toBe("Listening…");
    expect(micPill({ listening: false, speaking: true, quiet: false })).toBe("Speaking…");
    // Listening wins over a line held while the mic is open.
    expect(micPill({ listening: true, speaking: true, quiet: false })).toBe("Listening…");
    expect(micPill({ listening: false, speaking: false, quiet: true })).toBe(HUSH_LINE);
    expect(micPill({ listening: true, speaking: false, quiet: true })).toBe("Quiet.");
    expect(HUSH_CAPTION_MS).toBe(1000);
  });

  const MIC = code("components/TopBarMic.tsx");

  it("no caption element: the only thing beside the button is the pill, with the state alone", () => {
    expect(MIC).not.toContain("mic-caption");
    expect(MIC).not.toContain("capStyle");
    const bar = MIC.slice(MIC.indexOf('<span className="mic-wrap">'), MIC.indexOf("{menuOpen &&"));
    expect(bar).toContain('<span id="mic-pill" className={pill ? "on" : ""} role="status" aria-live="polite">');
    expect(bar).toMatch(/aria-live="polite">\s*\{pill\}\s*<\/span>/);
    // Neither the words heard nor the line said ever reach the bar.
    expect(bar).not.toMatch(/\{caption\}|\{unsure\}|liveTail/);
    expect(MIC).toContain("const pill = micPill({ listening, speaking, quiet });");
    // One portal: the options. Nothing else floats over the page.
    expect(MIC.split("createPortal(").length - 1).toBe(1);
    expect(src("components/TopBarMic.css")).not.toContain("#mic-caption");
  });

  it("“Hearing you” is the still ring's opacity step, not words", () => {
    expect(MIC).not.toContain("dataset.hearing");
    expect(cssRule(src("components/TopBarMic.css"), ".mic-ring.listening::after")).toBe("opacity: var(--mic-step, 0.45);");
  });

  it("“stop talking” shows Quiet. in the pill for a second", () => {
    expect(MIC).toMatch(/const hush = \(\) => \{\s*queue\.cancel\(\);\s*quietPill\(\);/);
    expect(MIC).toContain("quietTimer.current = setTimeout(() => setQuiet(false), HUSH_CAPTION_MS);");
  });

  it("the options open only on an explicit right-click or Shift+click — never by themselves (the Voice tour aside)", () => {
    expect(MIC.split("setMenuOpen(true)").length - 1).toBe(2);
    const open = MIC.indexOf("setMenuOpen(true)", MIC.indexOf("const openMenu = () => {"));
    expect(MIC.lastIndexOf("const openMenu = () => {", open)).toBeGreaterThan(MIC.lastIndexOf("};", open));
    // The other: the Voice tour on a step in the options ("mic-pop"), which closes them after.
    expect(MIC).toMatch(/if \(tourPop && !menuOpen\) \{\s*tourOpened\.current = true;\s*setMenuOpen\(true\);/);
    const calls = [...MIC.matchAll(/openMenu\(\)/g)].map((m) => m.index!);
    expect(calls.length).toBe(2);
    for (const at of calls) {
      const handler = [...MIC.slice(0, at).matchAll(/\bon([A-Z]\w*)=\{/g)].pop()?.[1];
      expect(["Click", "ContextMenu"]).toContain(handler);
    }
  });

  it("the options are five rows at most: title, replies, push to talk, the wake switch, Stop talking", () => {
    const pop = MIC.slice(MIC.indexOf('id="mic-pop"'), MIC.indexOf("document.body", MIC.indexOf('id="mic-pop"')));
    const rows = [...pop.matchAll(/className="(mic-pop-head|mic-pop-row[^"]*|mic-stop-talking)"/g)].map((m) => m[1]);
    expect(rows).toEqual(["mic-pop-head", "mic-pop-row", "mic-pop-row", "mic-pop-row mic-switch-row", "mic-stop-talking"]);
    expect(pop).toMatch(/\{speaking && \(\s*<button type="button" className="mic-stop-talking"/);
    // No hints, diagnostics, live words, level bar or fold.
    for (const gone of ["mic-hint", "mic-diag", "mic-live", "mic-level", "<details", "mic-sub", "openSettings("])
      expect(pop).not.toContain(gone);
    expect(pop.split('className="mic-seg-opt"').length - 1).toBe(3); // Off / Mine / Everything
    expect(MIC).toContain('const REPLIES_SHORT: Record<VoicePrefs["replies"], string> = { off: "Off", mine: "Mine", all: "Everything" };');
    // Placed by placeUnder from the button's rect, its height capped, scrolling inside.
    expect(MIC).toContain("placeUnder(rect, popW, window.innerWidth, window.innerHeight)");
    expect(MIC).toContain("const popW = Math.min(POP_W, window.innerWidth - 32);");
    const css = cssRule(src("components/TopBarMic.css"), ".mic-pop");
    expect(css).toContain("overflow-y: auto;");
    expect(css).toContain("max-width: min(360px, calc(100vw - 32px));");
  });
});

describe("the options never leave the window (placeUnder)", () => {
  const btn = (left: number) => ({ left, right: left + 26, bottom: 32 });
  it("hangs under the mic, left edges together, its height what is left of the window", () => {
    expect(placeUnder(btn(300), 280, 1280, 720)).toEqual({ left: 300, top: 38, maxHeight: 720 - 38 - 16 });
  });
  it("near the right edge it ends at the mic's right edge instead; never closer than 8 px to a side", () => {
    expect(placeUnder(btn(1200), 280, 1280, 720).left).toBe(1226 - 280);
    expect(placeUnder(btn(1250), 280, 1280, 720).left).toBe(1280 - 8 - 280);
    expect(placeUnder(btn(2), 280, 900, 600).left).toBe(8);
    // A narrow window: the width the mic gives it (vw - 32) still fits.
    const p = placeUnder(btn(100), 320 - 32, 320, 400);
    expect(p.left).toBeGreaterThanOrEqual(8);
    expect(p.left + 288).toBeLessThanOrEqual(320 - 8);
  });
  it("a short window: the height shrinks (it scrolls inside), never negative", () => {
    expect(placeUnder(btn(300), 280, 1280, 200).maxHeight).toBe(200 - 38 - 16);
    expect(placeUnder(btn(300), 280, 1280, 40).maxHeight).toBe(0);
  });
});

describe("the mic is still: the level is an opacity in three steps", () => {
  it("smoothed over ~400 ms, so one loud block barely moves it", () => {
    expect(LEVEL_SMOOTH_MS).toBe(400);
    expect(slowLevel(0, 1, 85)).toBeLessThan(0.2);
    let v = 0;
    for (let t = 0; t < 1200; t += 16) v = slowLevel(v, 1, 16);
    expect(v).toBeGreaterThan(0.9);
  });
  it("quiet / hearing / loud, with hysteresis at each edge (no jitter)", () => {
    expect(STEP_OPACITY).toEqual([0.45, 0.75, 1]);
    expect(levelStep(0, 0.05)).toBe(0);
    expect(levelStep(0, 0.2)).toBe(1);
    expect(levelStep(0, 0.7)).toBe(2);
    // Just under an edge: stays where it was.
    expect(levelStep(1, 0.1)).toBe(1);
    expect(levelStep(2, 0.52)).toBe(2);
    expect(levelStep(1, 0.52)).toBe(1);
    // Well under: steps down.
    expect(levelStep(1, 0.05)).toBe(0);
    expect(levelStep(2, 0.3)).toBe(1);
  });
  it("the meter writes only the step, and only when it changes", () => {
    const lib = code("lib/voiceMic.ts");
    const paint = lib.slice(lib.indexOf("function paint()"), lib.indexOf("function frame("));
    expect(paint).toContain("const v = String(STEP_OPACITY[step]);");
    expect(paint).toContain("if (v === painted) return;");
    expect(paint.match(/setProperty\(/g)).toHaveLength(1);
    // The ask pop-up's mic is still too.
    const ask = src("components/dialogs/AskDialog.css");
    expect(ask).not.toContain("mic-pulse");
    expect(cssRule(ask, ".ask-mic.listening")).not.toContain("animation");
  });
});

// --- 3. speech by sentence -------------------------------------------------- //

describe("the sentence splitter", () => {
  it("ends at . ? ! ; before a space or the end", () => {
    expect(speechSentences("The tests pass now. Shall I push it? Yes, pushing it now!")).toEqual([
      "The tests pass now.",
      "Shall I push it?",
      "Yes, pushing it now!",
    ]);
    expect(speechSentences("First I fixed the login; then I ran the tests.")).toEqual([
      "First I fixed the login;",
      "then I ran the tests.",
    ]);
    expect(speechSentences("  ")).toEqual([]);
  });

  it("keeps numbers, e.g./i.e./U.S. and titles together", () => {
    expect(speechSentences("It went from 3.5 to 4.2 seconds after the fix. Then it settled.")).toEqual([
      "It went from 3.5 to 4.2 seconds after the fix.",
      "Then it settled.",
    ]);
    expect(speechSentences("Use a faster model, e.g. the small one. It loads quicker.")).toEqual([
      "Use a faster model, e.g. the small one.",
      "It loads quicker.",
    ]);
    expect(speechSentences("Ask Dr. Smith about the U.S. servers today. She knows them.")).toEqual([
      "Ask Dr. Smith about the U.S. servers today.",
      "She knows them.",
    ]);
  });

  it("a sentence under ~12 characters joins the next", () => {
    expect(SPEECH_SENTENCE_MIN).toBe(12);
    expect(speechSentences("Done. The tests all pass. OK.")).toEqual(["Done. The tests all pass.", "OK."]);
    expect(speechSentences("Yes. No. Maybe so, we will see.")).toEqual(["Yes. No. Maybe so, we will see."]);
  });

  it("a closing quote or bracket stays with its sentence; a long one is cut at a comma", () => {
    expect(speechSentences("He said “ship it.” Then he left the room.")).toEqual(["He said “ship it.”", "Then he left the room."]);
    const long = Array.from({ length: 12 }, (_, i) => "clause number " + i + " of many").join(", ") + ".";
    const parts = speechSentences(long);
    expect(parts.length).toBeGreaterThan(1);
    expect(parts.every((p) => p.length <= 180)).toBe(true);
    expect(parts.join(" ")).toBe(long);
  });
});

/** The mocked /speak: SYNTH_MS per sentence in the request (a CPU engine),
 * each sentence 2 s of audio. */
const SYNTH_MS = 1500;
const AUDIO_S = 2;
const LINE = "The first sentence is here. The second one follows it. And the third ends it.";
const sentencesIn = (t: string) => (t.match(/[.!?](\s|$)/g) || []).length;

describe("the server speaker plays sentence by sentence", () => {
  let requests: Array<{ text: string; at: number; signal?: AbortSignal }>;
  let inFlight = 0;
  let maxInFlight = 0;

  beforeEach(() => {
    vi.useFakeTimers();
    T0 = Date.now();
    starts.length = 0;
    stopped.length = 0;
    requests = [];
    inFlight = 0;
    maxInFlight = 0;
    vi.stubGlobal("window", { AudioContext: ClockCtx });
    mocks.apiRaw.mockImplementation((_url: string, init: RequestInit) => {
      const text = JSON.parse(String(init.body)).text as string;
      requests.push({ text, at: Date.now() - T0, signal: init.signal || undefined });
      inFlight++;
      maxInFlight = Math.max(maxInFlight, inFlight);
      return new Promise((resolve, reject) => {
        init.signal?.addEventListener("abort", () => {
          inFlight--;
          reject(new Error("aborted"));
        });
        setTimeout(() => {
          if (init.signal?.aborted) return;
          inFlight--;
          resolve({
            ok: true,
            status: 200,
            arrayBuffer: async () => ({ text, dur: AUDIO_S * sentencesIn(text) }),
          });
        }, SYNTH_MS * sentencesIn(text));
      });
    });
  });

  const fallback = (): Speaker & { said: string[] } => {
    const said: string[] = [];
    return { said, speak: async (t) => void said.push(t), cancel: () => {} };
  };

  it("first sound after the FIRST sentence's synthesis (1.5 s), not the whole line's (4.5 s); the next asked for while it plays; gapless", async () => {
    const sp = serverSpeaker(fallback());
    let done = -1;
    void sp.speak(LINE).then(() => (done = Date.now() - T0));
    await vi.advanceTimersByTimeAsync(1499);
    expect(starts).toEqual([]);
    await vi.advanceTimersByTimeAsync(1);
    // Time to first sound: one sentence's synthesis.
    expect(starts.map((s) => s.at)).toEqual([1.5]);
    expect(requests.map((r) => [r.text, r.at])).toEqual([
      ["The first sentence is here.", 0],
      ["The second one follows it.", 1500], // asked for as sentence 1 starts playing
    ]);
    await vi.advanceTimersByTimeAsync(10_000);
    // Back to back: each starts exactly where the one before ends.
    expect(starts.map((s) => s.at)).toEqual([1.5, 3.5, 5.5]);
    expect(starts.map((s) => s.text)).toEqual(speechSentences(LINE));
    expect(requests.map((r) => r.at)).toEqual([0, 1500, 3000]);
    expect(maxInFlight).toBe(1); // one prefetch ahead, never two requests at once
    expect(done).toBe(7500); // resolves when the last sentence has played
  });

  it("the queue is speaking from the first request to the last sentence's end", async () => {
    const on: Array<[boolean, number]> = [];
    const sp = serverSpeaker(fallback());
    const q = new SpeechQueue(() => sp, (b) => void on.push([b, Date.now() - T0]));
    void q.say(LINE);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(on).toEqual([
      [true, 0],
      [false, 7500],
    ]);
  });

  it("cancel (and hold) stops the sentence playing, aborts the one being made and drops the rest", async () => {
    const sp = serverSpeaker(fallback());
    const q = new SpeechQueue(() => sp);
    const said = q.say(LINE);
    await vi.advanceTimersByTimeAsync(2000); // sentence 1 playing, 2 being made
    expect(starts.length).toBe(1);
    q.cancel();
    await said;
    expect(stopped).toEqual(["The first sentence is here."]);
    expect(requests[1].signal?.aborted).toBe(true);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(starts.length).toBe(1);
    expect(requests.length).toBe(2);
    // hold() cuts the same way.
    const q2 = new SpeechQueue(() => sp);
    void q2.say(LINE);
    await vi.advanceTimersByTimeAsync(1600);
    expect(starts.length).toBe(2);
    q2.hold();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(starts.length).toBe(2);
    expect(stopped.length).toBe(2);
  });

  it("a failed sentence: what is scheduled plays out, the rest goes to the fallback as one utterance", async () => {
    let n = 0;
    const real = mocks.apiRaw.getMockImplementation()!;
    mocks.apiRaw.mockImplementation((url: string, init: RequestInit) =>
      ++n === 2 ? Promise.resolve({ ok: false, status: 502, json: async () => ({ error: "voice host down" }) }) : real(url, init)
    );
    const fb = fallback();
    const sp = serverSpeaker(fb);
    await Promise.all([sp.speak(LINE), vi.advanceTimersByTimeAsync(10_000)]);
    expect(starts.map((s) => s.text)).toEqual(["The first sentence is here."]);
    expect(fb.said).toEqual(["The second one follows it. And the third ends it."]);
    expect(sp.lastError).toBe("voice host down");
  });
});
