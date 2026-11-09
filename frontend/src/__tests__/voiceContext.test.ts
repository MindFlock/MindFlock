/** Sentence context for desktop dictation (2026-11, "isn't super great with
 * context of sentences"): each phrase's stream opens with the message's
 * words already said ahead of it — POST /api/voice/stream {context, session}
 * — which the server hands whisper as its prompt.
 *
 * What is pinned: the cap (VOICE_CONTEXT_MAX at a word boundary) and the
 * open's body (pure); what Dictation.context counts — the phrases typed, the
 * phrase still under way (its final not back) as heard so far, never a
 * control word, nothing after a send or a clear, the last phrase gone after
 * "scratch that"; LiveDictation.streamContext's session and resets; a text
 * box's text before the cursor; and a real server recognizer run, where the
 * first phrase of a fresh message opens with none and the second opens with
 * the first's words. */

import { afterEach, describe, expect, it, vi } from "vitest";

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

import {
  Dictation,
  LiveDictation,
  VOICE_CLIENT_ID,
  VOICE_CONTEXT_MAX,
  serverRecognizer,
  sessionTarget,
  streamOpenBody,
  voiceContext,
  type DictTarget,
  type KeyPost,
  type LiveHooks,
  type RecognizerHandlers,
} from "../lib/voiceDesktop";
import MIC_SRC from "../components/TopBarMic.tsx?raw";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  mocks.apiRaw.mockReset();
  mocks.instApi.mockReset();
  mocks.startWavRecording.mockReset();
});

describe("voiceContext: the last 400 characters, cut at a word boundary", () => {
  it("short text stands, whitespace runs become one space", () => {
    expect(VOICE_CONTEXT_MAX).toBe(400);
    expect(voiceContext("")).toBe("");
    expect(voiceContext("  fix the\n  login   bug ")).toBe("fix the login bug");
  });

  it("a cut inside a word leaves that word out whole", () => {
    const words = Array.from({ length: 120 }, (_, i) => "word" + i); // ~700 chars
    const text = words.join(" ");
    const out = voiceContext(text);
    expect(out.length).toBeLessThanOrEqual(400);
    expect(out.length).toBeGreaterThan(390);
    expect(text.endsWith(out)).toBe(true);
    // Starts on a whole word: the character before it in the text is a space.
    expect(text[text.length - out.length - 1]).toBe(" ");
    expect(words).toContain(out.split(" ")[0]);
    expect(out.endsWith("word119")).toBe(true);
  });

  it("a cut right at a space keeps the full 400", () => {
    const tail = "a".repeat(199) + " " + "b".repeat(200); // exactly 400
    const out = voiceContext("x".repeat(50) + " " + tail);
    expect(out).toBe(tail);
    expect(voiceContext("zz " + "y".repeat(10), 5)).toBe("");
    expect(voiceContext("one two three four", 10)).toBe("three four"); // the cut falls on the space
    expect(voiceContext("one two three four", 9)).toBe("four"); // "hree": a broken word, left out
  });

  it("the open's body: context and session only when there are any", () => {
    expect(streamOpenBody("en-US", "c1")).toEqual({ lang: "en-US", client: "c1" });
    expect(streamOpenBody("en-US", "c1", { context: "", session: "" })).toEqual({ lang: "en-US", client: "c1" });
    expect(streamOpenBody("en-US", "c1", { context: " fix the bug ", session: "t1" })).toEqual({
      lang: "en-US",
      client: "c1",
      context: "fix the bug",
      session: "t1",
    });
    const long = streamOpenBody("en", "c", { context: "word ".repeat(200) });
    expect(long.context.length).toBeLessThanOrEqual(400);
  });
});

describe("Dictation.context: the message's words ahead of the next phrase", () => {
  const begun = () => {
    const d = new Dictation();
    d.begin();
    d.routes = false;
    return d;
  };
  /** Type what an outcome says (a target that takes every key). */
  const apply = (d: Dictation, o: { kind: string; text?: string; erase?: string }) => {
    if (o.erase) d.unwrote(o.erase);
    if (o.kind === "erase" && o.text) d.unwrote(o.text);
    if ((o.kind === "type" || o.kind === "leave") && o.text) d.wrote(o.text);
    if (o.kind === "undo" && o.text) d.unwrote(o.text);
  };

  it("a fresh message: none", () => {
    expect(begun().context()).toBe("");
    expect(new Dictation().context()).toBe(""); // not listening
  });

  it("the phrases typed, then the phrase under way as heard so far (its final not back yet)", () => {
    const d = begun();
    apply(d, d.heard("fix the login bug"));
    expect(d.context()).toBe("fix the login bug");
    apply(d, d.stream("and add a", "test"));
    // Phrase two still under way: its committed words and its unsure tail count.
    expect(d.context()).toBe("fix the login bug and add a test");
    apply(d, d.flush("", "and add a test for it"));
    expect(d.context()).toBe("fix the login bug and add a test for it");
  });

  it("only the fast words so far: the guess as typed", () => {
    const d = begun();
    apply(d, d.hearing("refactor the parser"));
    expect(d.guessed).toBe("refactor the parser");
    expect(d.context()).toBe("refactor the parser");
  });

  it("never a control word: a held-back start of one is left out", () => {
    const d = begun();
    apply(d, d.heard("fix the bug"));
    apply(d, d.stream("then run the tests", "stop"));
    expect(d.context()).toBe("fix the bug then run the tests");
  });

  it("a phrase that ends in 'over' or 'disregard': the next one starts afresh", () => {
    const d = begun();
    apply(d, d.heard("fix the bug"));
    apply(d, d.stream("and ship it", "over"));
    expect(d.context()).toBe("");
    const e = begun();
    apply(e, e.heard("fix the bug"));
    apply(e, e.stream("no wait disregard", ""));
    expect(e.context()).toBe("");
  });

  it("after a send (next) or a clear (disregard): what is left — nothing", () => {
    const d = begun();
    apply(d, d.heard("fix the bug"));
    expect(d.context()).toBe("fix the bug");
    d.next();
    expect(d.context()).toBe("");
    apply(d, d.heard("now the parser"));
    expect(d.context()).toBe("now the parser");
    apply(d, d.heard("disregard"));
    expect(d.typed).toBe("");
    expect(d.context()).toBe("");
  });

  it("'scratch that': with words, this phrase goes; alone, the last phrase goes", () => {
    const d = begun();
    apply(d, d.heard("fix the bug"));
    apply(d, d.heard("in the parser"));
    apply(d, d.stream("no that's wrong", "scratch that"));
    expect(d.context()).toBe("fix the bug in the parser");
    const e = begun();
    apply(e, e.heard("fix the bug"));
    apply(e, e.heard("in the parser"));
    apply(e, e.stream("scratch that", ""));
    expect(e.context()).toBe("fix the bug");
    apply(e, e.flush(""));
    expect(e.typed).toBe("fix the bug ");
    expect(e.context()).toBe("fix the bug");
  });

  it("a first phrase the router takes ('next session') is no words of the message", () => {
    const d = new Dictation();
    d.begin();
    apply(d, d.stream("next session", ""));
    expect(d.context()).toBe("");
  });

  it("a text box: its text before the cursor, this phrase's guess in it not counted twice", () => {
    const d = begun();
    apply(d, d.stream("and add", "a test"));
    expect(d.guessed).toBe("and add a test");
    // The box held "Ticket 42: " before the dictation; the guess sits at the cursor.
    expect(d.context("Ticket 42: and add a test")).toBe("Ticket 42: and add a test");
    expect(d.context("Ticket 42: ")).toBe("Ticket 42: and add a test");
  });
});

describe("LiveDictation.streamContext", () => {
  const hooks = (): LiveHooks => ({
    caption: () => {},
    halt: () => {},
    route: async () => false,
    activity: () => "idle",
    replies: () => false,
    submitted: () => {},
  });
  const keys: KeyPost = async () => ({ sent: true });

  it("a session: the words typed into it, and its title; a send resets it", async () => {
    mocks.instApi.mockResolvedValue({});
    const d = new LiveDictation(sessionTarget("t1", "Login fix", keys), hooks(), { continuous: true });
    expect(d.streamContext()).toEqual({ context: "", session: "t1" });
    await d.heard("fix the login bug");
    expect(d.streamContext()).toEqual({ context: "fix the login bug", session: "t1" });
    await d.heard("and add a test over");
    await vi.waitFor(() => expect(mocks.instApi).toHaveBeenCalledWith("t1", "/send", expect.anything()));
    await vi.waitFor(() => expect(d.dict.typed).toBe(""));
    expect(d.streamContext()).toEqual({ context: "", session: "t1" });
    await d.heard("now the parser");
    expect(d.streamContext().context).toBe("now the parser");
    // "disregard": cleared, nothing left.
    await d.heard("disregard");
    expect(d.streamContext().context).toBe("");
  });

  it("while the send is on its way the message's words are gone already: none", async () => {
    let answer: (v: unknown) => void = () => {};
    mocks.instApi.mockImplementation(() => new Promise((res) => (answer = res)));
    const d = new LiveDictation(sessionTarget("t1", "Login fix", keys), hooks(), { continuous: true });
    await d.heard("fix the login bug");
    const going = d.heard("and add a test over");
    await vi.waitFor(() => expect(mocks.instApi).toHaveBeenCalledWith("t1", "/send", expect.anything()));
    expect(d.dict.typed.trim()).toBe("fix the login bug and add a test"); // not through yet
    expect(d.streamContext()).toEqual({ context: "", session: "t1" });
    answer({});
    await going;
    expect(d.dict.typed).toBe("");
    expect(d.streamContext()).toEqual({ context: "", session: "t1" });
    await d.heard("now the parser");
    expect(d.streamContext().context).toBe("now the parser");
  });

  it("a text box: its text up to the cursor (what was there before the dictation too); no session", async () => {
    const el = { value: "Ticket 42: ", cursor: 11 };
    const field: DictTarget = {
      kind: "field",
      title: "",
      name: "the text box",
      before: () => el.value.slice(0, el.cursor),
      type: (s) => {
        el.value = el.value.slice(0, el.cursor) + s + el.value.slice(el.cursor);
        el.cursor += s.length;
        return true;
      },
      erase: () => true,
    };
    const d = new LiveDictation(field, hooks());
    expect(d.streamContext()).toEqual({ context: "Ticket 42:", session: "" });
    await d.heard("fix the login bug");
    expect(d.streamContext()).toEqual({ context: "Ticket 42: fix the login bug", session: "" });
  });

  it("the dictation over (Escape): none", async () => {
    const d = new LiveDictation(sessionTarget("t1", "Login fix", keys), hooks());
    await d.heard("fix the login bug");
    await d.cancel();
    expect(d.active).toBe(false);
    expect(d.streamContext()).toEqual({ context: "", session: "" });
  });

  it("TopBarMic hands each stream open the live dictation's context, and a text box its text before the cursor", () => {
    expect(MIC_SRC).toContain("context: () => (live.current === d ? d.streamContext() : null)");
    expect(MIC_SRC).toMatch(/before: \(\) => el\.value\.slice\(0, el\.selectionStart != null \? el\.selectionStart : el\.value\.length\)/);
  });
});

describe("the server recognizer opens each phrase's stream with its context", () => {
  class FakeCtx {
    state = "running";
    sampleRate = 48000;
    resume() {
      return Promise.resolve();
    }
  }
  const stream = { active: true, getTracks: () => [] };
  const resp = (status: number, body: unknown) => ({ ok: status < 300, status, json: async () => body });
  const wav = () => new Blob([new Uint8Array(44 + 3200)], { type: "audio/wav" });

  function setup(chunks: unknown[], ends: unknown[]) {
    vi.stubGlobal("window", { AudioContext: FakeCtx });
    vi.stubGlobal("navigator", { language: "en-US", userAgent: "", mediaDevices: { getUserMedia: async () => stream } });
    let now = 1_000_000;
    vi.spyOn(Date, "now").mockImplementation(() => now);
    const clips: Array<{ opts: { onLevel?: (rms: number) => void }; feed(): void }> = [];
    mocks.startWavRecording.mockImplementation((_s: unknown, _c: unknown, opts) => {
      let ready = false;
      const clip = {
        opts,
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
    const opens: Array<Record<string, string>> = [];
    let n = 0;
    let c = 0;
    let e = 0;
    mocks.apiRaw.mockImplementation(async (url: string, init: RequestInit) => {
      if (url === "/api/voice/stream") {
        opens.push(JSON.parse(String(init.body)));
        return resp(200, { id: "s" + ++n, ready: true });
      }
      if (url.endsWith("/chunk")) return resp(200, chunks[c++] || { committed: "", partial: "" });
      if (url.endsWith("/end")) return resp(200, ends[e++] || { committed: "", partial: "" });
      return resp(204, {});
    });
    const block = (i: number, loud: boolean, ms = 100) => {
      now += ms;
      clips[i].opts.onLevel!(loud ? 0.3 : 0.001);
    };
    return { clips, opens, block };
  }

  it("the first phrase of a fresh message: none; the second: the first phrase's words", async () => {
    const t = setup([{ committed: "fix the login", partial: "bug" }], [{ committed: "bug" }]);
    mocks.instApi.mockResolvedValue({});
    const d = new LiveDictation(sessionTarget("t1", "Login fix", async () => ({ sent: true })), {
      caption: () => {},
      halt: () => {},
      route: async () => false,
      activity: () => "idle",
      replies: () => false,
      submitted: () => {},
    });
    const h: RecognizerHandlers = {
      onInterim: () => {},
      onFinal: (x) => void d.heard(x),
      onStream: (w, p) => void d.stream(w, p),
      onFlush: (x, final) => void d.flush(x, final),
      context: () => d.streamContext(),
      onError: () => {},
      onEnd: () => {},
    };
    const r = serverRecognizer({ stream: () => true });
    r.start(h);
    await vi.waitFor(() => expect(t.opens.length).toBe(1));
    expect(t.opens[0]).toEqual({ lang: "en-US", client: VOICE_CLIENT_ID, session: "t1" });
    await vi.waitFor(() => expect(t.clips.length).toBe(1));
    t.block(0, false);
    t.block(0, true);
    await Promise.resolve();
    t.clips[0].feed();
    t.block(0, true);
    await vi.waitFor(() => expect(d.dict.held).toBe("fix the login"));
    for (let i = 0; i < 12; i++) t.block(0, false); // a pause: the phrase ends
    await vi.waitFor(() => expect(t.clips.length).toBe(2));
    t.block(1, false);
    t.block(1, true);
    await vi.waitFor(() => expect(t.opens.length).toBe(2));
    expect(t.opens[1]).toEqual({ lang: "en-US", client: VOICE_CLIENT_ID, context: "fix the login bug", session: "t1" });
    r.stop();
  });
});
