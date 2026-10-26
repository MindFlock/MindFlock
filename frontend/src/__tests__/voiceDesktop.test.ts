/** The desktop mic (components/TopBarMic.tsx + lib/voiceDesktop.ts): the
 * radio words, the dictation machine, the prefs, the reply rules, the speech
 * queue and the engine choice — pure, so pinned here — live typing against a
 * mocked terminal (`peekTerm`) and `instApi`, the server recognizer's WAV path
 * against a mocked recorder and `apiRaw`, and the wiring around them pinned
 * against the source: where the mic sits, that the old dictation button is
 * gone, and what goes on /send. */

import { afterEach, describe, expect, it, vi } from "vitest";
import MIC_SRC from "../components/TopBarMic.tsx?raw";
import {
  DEFAULT_VOICE_CFG,
  DEFAULT_VOICE_PREFS,
  DICT_HINT,
  Dictation,
  LiveDictation,
  OVER_LINE,
  ReplyTracker,
  SpeechQueue,
  VOICE_PREFS_KEY,
  backspaces,
  engineDiagnostics,
  isElectron,
  loadVoicePrefs,
  noRecognizerLine,
  parseRadio,
  recognizerErrorLine,
  saveVoicePrefs,
  sentenceChunks,
  serverRecognizer,
  sessionTarget,
  spokenLine,
  sttEngine,
  ttsEngine,
  type DictTarget,
  type LiveHooks,
  type RecognizerHandlers,
  type Speaker,
  type VoiceCfg,
} from "../lib/voiceDesktop";
import { ALL_CODE, ALL_CSS, blockFrom, code, cssRule, fn, src } from "./sourceText";

const mocks = vi.hoisted(() => {
  class ApiError extends Error {
    constructor(
      public status: number,
      message: string,
      public body: unknown
    ) {
      super(message);
    }
  }
  return {
    api: vi.fn(),
    apiRaw: vi.fn(),
    instApi: vi.fn(),
    ApiError,
    peekTerm: vi.fn(),
    startWavRecording: vi.fn(),
    resamplerName: vi.fn(() => "browser resampler"),
  };
});
vi.mock("../api/client", () => ({ api: mocks.api, apiRaw: mocks.apiRaw, instApi: mocks.instApi, ApiError: mocks.ApiError }));
vi.mock("../lib/terminals", () => ({ peekTerm: mocks.peekTerm }));
vi.mock("../lib/wavRecorder", () => ({
  startWavRecording: mocks.startWavRecording,
  resamplerName: mocks.resamplerName,
}));

function memKV(init: Record<string, string> = {}) {
  const m = new Map(Object.entries(init));
  return {
    getItem: (k: string) => (m.has(k) ? m.get(k)! : null),
    setItem: (k: string, v: string) => void m.set(k, v),
    m,
  };
}

describe("parseRadio: only the last word is a radio word", () => {
  it.each([
    ["fix the login bug over", "over", "fix the login bug"],
    ["Over.", "over", ""],
    ["run the tests submit", "submit", "run the tests"],
    ["Submit.", "submit", ""],
    ["SUBMIT IT!", "submit", ""],
    ["ok, submit that.", "submit", "ok"],
    ["disregard", "disregard", ""],
    ["Disregard that.", "disregard", ""],
    ["scrap it, disregard it", "disregard", "scrap it"],
    ["move it over", "over", "move it"],
    ["hangover", "", "hangover"],
    ["resubmit", "", "resubmit"],
    ["submit the form then wait", "", "submit the form then wait"],
    ["start over the tests then stop", "", "start over the tests then stop"],
    ["", "", ""],
  ])("%j → %s", (text, kind, rest) => {
    expect(parseRadio(text)).toEqual({ kind, text: rest });
  });
});

describe("Dictation: the machine (what to type, what to take back)", () => {
  it("each final is typed at once, with a trailing space", () => {
    const d = new Dictation();
    d.begin();
    expect(d.heard("fix the login bug")).toEqual({ kind: "type", text: "fix the login bug " });
    d.wrote("fix the login bug ");
    expect(d.heard("and add a test")).toEqual({ kind: "type", text: "and add a test " });
    d.wrote("and add a test ");
    d.hearing("then push");
    expect(d.shown).toBe("fix the login bug and add a test then push");
    expect(d.listening).toBe(true);
  });

  it("“over” stops with the words in the field; “submit” stops and submits; the phrase before either is typed first", () => {
    const d = new Dictation();
    d.begin();
    expect(d.heard("run the tests over")).toEqual({ kind: "over", text: "run the tests " });
    expect(d.listening).toBe(false);
    d.begin();
    expect(d.heard("Over.")).toEqual({ kind: "over", text: "" });
    d.begin();
    expect(d.heard("run the tests, submit")).toEqual({ kind: "submit", text: "run the tests " });
    expect(d.listening).toBe(false);
    expect(d.heard("late words")).toEqual({ kind: "stopped" });
  });

  it("“disregard” takes back exactly what this dictation typed, and keeps listening", () => {
    const d = new Dictation();
    d.begin();
    d.wrote("delete everything ");
    expect(d.heard("Disregard that.")).toEqual({ kind: "erase", text: "delete everything " });
    expect(d.listening).toBe(true);
    expect(d.typed).toBe("");
    expect(d.heard("keep everything over")).toEqual({ kind: "over", text: "keep everything " });
  });

  it("push to talk let go is “over”, and a phrase still in flight counts", () => {
    const d = new Dictation();
    d.begin();
    d.hearing("switch to the login fix");
    expect(d.finish()).toEqual({ kind: "over", text: "switch to the login fix " });
    expect(d.listening).toBe(false);
    d.begin();
    expect(d.finish()).toEqual({ kind: "over", text: "" });
    d.begin();
    d.hearing("ship it submit");
    expect(d.finish()).toEqual({ kind: "submit", text: "ship it " });
    d.begin();
    d.wrote("oops ");
    d.hearing("disregard");
    expect(d.finish()).toEqual({ kind: "dropped", text: "oops " });
  });

  it("Escape is “disregard” and stop; silence stops and leaves the words", () => {
    const d = new Dictation();
    d.begin();
    d.wrote("half a thought ");
    expect(d.cancel()).toEqual({ kind: "dropped", text: "half a thought " });
    expect(d.listening).toBe(false);
    expect(d.cancel()).toEqual({ kind: "stopped" });
    d.begin();
    d.wrote("half a thought ");
    expect(d.expire()).toEqual({ kind: "over", text: "" });
    expect(d.typed).toBe("half a thought ");
  });

  it("one backspace per character typed — code points, not UTF-16 units or bytes", () => {
    expect(backspaces("abc ")).toBe("\x7f".repeat(4));
    expect(backspaces("café ")).toBe("\x7f".repeat(5));
    expect(backspaces("ship it 🚀 ")).toBe("\x7f".repeat(10));
    expect(backspaces("日本語 ")).toBe("\x7f".repeat(4));
    expect(backspaces("")).toBe("");
  });
});

describe("LiveDictation: typing into the session as you speak", () => {
  afterEach(() => {
    mocks.peekTerm.mockReset();
    mocks.instApi.mockReset();
  });

  /** A terminal per session that records the raw keys sent down it. */
  function terms(open = true) {
    const sent: Record<string, string[]> = {};
    mocks.peekTerm.mockImplementation((title: string, kind: string) => {
      expect(kind).toBe("agent");
      return {
        send: (data: string) => {
          if (!open) return false;
          (sent[title] ||= []).push(data);
          return true;
        },
      };
    });
    return sent;
  }

  function hooks(over: Partial<LiveHooks> = {}) {
    const captions: string[] = [];
    const calls = { halt: 0, routed: [] as string[], submitted: [] as Array<[string, boolean]> };
    const h: LiveHooks = {
      caption: (t) => void captions.push(t),
      halt: () => void calls.halt++,
      route: async (t) => {
        calls.routed.push(t);
        return false;
      },
      activity: () => "idle",
      replies: () => false,
      submitted: (title, say) => void calls.submitted.push([title, say]),
      ...over,
    };
    return { h, captions, calls, last: () => captions[captions.length - 1] };
  }

  it("types each final at once with a trailing space, and shows the running text", async () => {
    const sent = terms();
    const k = hooks();
    const d = new LiveDictation(sessionTarget("t1", "Login fix"), k.h);
    await d.heard("fix the login bug");
    expect(sent.t1).toEqual(["fix the login bug "]);
    await d.heard("and add a test");
    expect(sent.t1).toEqual(["fix the login bug ", "and add a test "]);
    expect(k.last()).toBe("fix the login bug and add a test");
    // Only the first phrase was offered to the router.
    expect(k.calls.routed).toEqual(["fix the login bug"]);
    expect(k.calls.halt).toBe(0);
    expect(d.active).toBe(true);
  });

  it("a session on a dialog gets nothing typed: the words are dropped and listening ends", async () => {
    for (const activity of ["clarify", "limit"]) {
      const sent = terms();
      const k = hooks({ activity: () => activity });
      const d = new LiveDictation(sessionTarget("t1", "Login fix"), k.h);
      await d.heard("yes do it");
      expect(sent.t1).toBeUndefined();
      expect(k.last()).toBe("Login fix is waiting on a dialog — answer it first");
      expect(k.calls.halt).toBe(1);
      expect(d.active).toBe(false);
    }
  });

  it("the dialog is checked before the FIRST phrase only", async () => {
    const sent = terms();
    let act = "working";
    const k = hooks({ activity: () => act });
    const d = new LiveDictation(sessionTarget("t1", "Login fix"), k.h);
    await d.heard("one");
    act = "clarify"; // the first phrase is already in the field: keep going
    await d.heard("two");
    expect(sent.t1).toEqual(["one ", "two "]);
  });

  it("“disregard” sends one backspace per character this dictation typed — multi-byte ones included — and goes on", async () => {
    const sent = terms();
    const k = hooks();
    const d = new LiveDictation(sessionTarget("t1", "Login fix"), k.h);
    await d.heard("café");
    await d.heard("ship it 🚀");
    await d.heard("disregard that");
    expect(sent.t1.slice(0, 2)).toEqual(["café ", "ship it 🚀 "]);
    expect(sent.t1[2]).toBe("\x7f".repeat(5 + 10));
    expect(sent.t1.join("")).not.toContain("\x1b"); // never Escape
    expect(k.last()).toBe("Disregarded — go on");
    expect(d.active).toBe(true);
    // A fresh start: the next phrase is a first phrase again (router, dialog check).
    await d.heard("keep everything");
    expect(sent.t1[3]).toBe("keep everything ");
    expect(k.calls.routed).toEqual(["café", "keep everything"]);
  });

  it("Escape takes it back and stops", async () => {
    const sent = terms();
    const k = hooks();
    const d = new LiveDictation(sessionTarget("t1", "Login fix"), k.h);
    await d.heard("rm -rf");
    await d.cancel();
    expect(sent.t1).toEqual(["rm -rf ", "\x7f".repeat(7)]);
    expect(k.calls.halt).toBe(1);
    expect(d.active).toBe(false);
  });

  it("“over” stops with the words in the field, nothing submitted", async () => {
    const sent = terms();
    const k = hooks();
    const d = new LiveDictation(sessionTarget("t1", "Login fix"), k.h);
    await d.heard("run the tests over");
    expect(sent.t1).toEqual(["run the tests "]);
    expect(k.last()).toBe(OVER_LINE);
    expect(OVER_LINE).toBe("In the field — say submit or press Enter");
    expect(k.calls.halt).toBe(1);
    expect(mocks.instApi).not.toHaveBeenCalled();
  });

  it("push to talk let go is “over”: the phrase in flight is typed, nothing submitted", async () => {
    const sent = terms();
    const k = hooks();
    const d = new LiveDictation(sessionTarget("t1", "Login fix"), k.h);
    await d.heard("fix the bug");
    d.hearing("and the test");
    await d.finish();
    expect(sent.t1).toEqual(["fix the bug ", "and the test "]);
    expect(k.last()).toBe(OVER_LINE);
    expect(mocks.instApi).not.toHaveBeenCalled();
    expect(d.active).toBe(false);
    // Anything the recognizer delivers afterwards is ignored.
    await d.heard("late");
    expect(sent.t1).toHaveLength(2);
  });

  it("“submit” types the phrase, then presses Enter through /send {enter: true, say}", async () => {
    const sent = terms();
    for (const replies of [false, true]) {
      mocks.instApi.mockResolvedValueOnce({ ok: true });
      const k = hooks({ replies: () => replies });
      const d = new LiveDictation(sessionTarget("t1", "Login fix"), k.h);
      await d.heard("run the tests submit");
      expect(mocks.instApi).toHaveBeenLastCalledWith("t1", "/send", { json: { enter: true, say: replies } });
      expect(k.calls.submitted).toEqual([["t1", replies]]);
      expect(k.last()).toBe("Sent to Login fix.");
      expect(k.calls.halt).toBe(1);
    }
    expect(sent.t1).toEqual(["run the tests ", "run the tests "]);
  });

  it("a 409 in_dialog leaves the draft and says to press Enter yourself", async () => {
    terms();
    mocks.instApi.mockRejectedValueOnce(new mocks.ApiError(409, "in a dialog", { in_dialog: true }));
    const k = hooks({ replies: () => true });
    const d = new LiveDictation(sessionTarget("t1", "Login fix"), k.h);
    await d.heard("yes submit");
    expect(k.last()).toBe("Login fix is on a dialog — press Enter yourself when it is answered");
    expect(k.calls.submitted).toEqual([]); // nothing went: no reply to wait for
  });

  it("a text box: words go in at the cursor; “over” and “submit” only stop — Enter is never pressed", async () => {
    terms();
    for (const word of ["over", "submit"]) {
      const typed: string[] = [];
      const erased: string[] = [];
      const box: DictTarget = {
        kind: "field",
        title: "",
        name: "the text box",
        type: (t) => typed.push(t) > 0,
        erase: (t) => erased.push(t) > 0,
      };
      const k = hooks();
      const d = new LiveDictation(box, k.h);
      await d.heard("hello");
      await d.heard("oops disregard");
      await d.heard("world " + word);
      expect(typed).toEqual(["hello ", "world "]);
      expect(erased).toEqual(["hello "]);
      expect(k.calls.routed).toEqual([]); // a text box is never routed
      expect(k.calls.halt).toBe(1);
      expect(mocks.instApi).not.toHaveBeenCalled();
      expect(mocks.peekTerm).not.toHaveBeenCalled();
    }
  });

  it("a phrase the router acted on is not typed", async () => {
    const sent = terms();
    const k = hooks({ route: async () => true });
    const d = new LiveDictation(sessionTarget("t1", "Login fix"), k.h);
    await d.heard("switch to the billing page");
    expect(sent.t1).toBeUndefined();
    expect(k.calls.halt).toBe(1);
  });

  it("no session focused: a non-command first phrase is not typed anywhere", async () => {
    terms();
    const k = hooks();
    const d = new LiveDictation(null, k.h);
    await d.heard("fix the bug");
    expect(k.calls.routed).toEqual(["fix the bug"]);
    expect(k.last()).toBe("Click a session first — “fix the bug” not typed");
    expect(mocks.peekTerm).not.toHaveBeenCalled();
  });

  it("a terminal that isn't connected: says so and stops", async () => {
    terms(false);
    const k = hooks();
    const d = new LiveDictation(sessionTarget("t1", "Login fix"), k.h);
    await d.heard("hello");
    expect(k.last()).toBe("Couldn't type into Login fix — its terminal isn't connected");
    expect(d.active).toBe(false);
  });

  it("phrases are carried out in order even while the first waits on the router", async () => {
    const sent = terms();
    let release: (v: boolean) => void = () => {};
    const k = hooks({ route: () => new Promise<boolean>((r) => (release = r)) });
    const d = new LiveDictation(sessionTarget("t1", "Login fix"), k.h);
    const a = d.heard("one");
    const b = d.heard("two");
    await Promise.resolve();
    expect(sent.t1).toBeUndefined();
    release(false);
    await Promise.all([a, b]);
    expect(sent.t1).toEqual(["one ", "two "]);
  });

  it("before a word, the caption names the radio words", () => {
    const k = hooks();
    new LiveDictation(sessionTarget("t1", "Login fix"), k.h).show();
    expect(k.last()).toBe(DICT_HINT);
    expect(DICT_HINT).toBe("Say over to leave it, submit to send, disregard to start over");
  });
});

describe("prefs: cs_voice_desktop", () => {
  it("defaults to click-to-talk with replies OFF", () => {
    expect(DEFAULT_VOICE_PREFS).toEqual({ ptt: "toggle", replies: false });
    expect(loadVoicePrefs(memKV())).toEqual({ ptt: "toggle", replies: false });
    expect(loadVoicePrefs(null)).toEqual(DEFAULT_VOICE_PREFS);
  });

  it("round-trips, and garbage reads as the defaults", () => {
    const kv = memKV();
    saveVoicePrefs({ ptt: "hold", replies: true }, kv);
    expect(JSON.parse(kv.m.get(VOICE_PREFS_KEY)!)).toEqual({ ptt: "hold", replies: true });
    expect(loadVoicePrefs(kv)).toEqual({ ptt: "hold", replies: true });
    expect(loadVoicePrefs(memKV({ [VOICE_PREFS_KEY]: "{nope" }))).toEqual(DEFAULT_VOICE_PREFS);
    expect(loadVoicePrefs(memKV({ [VOICE_PREFS_KEY]: '{"ptt":"sideways","replies":"yes"}' }))).toEqual(
      DEFAULT_VOICE_PREFS
    );
  });

  it("a storage that throws never breaks the mic", () => {
    const bad = {
      getItem: () => {
        throw new Error("denied");
      },
      setItem: () => {
        throw new Error("denied");
      },
    };
    expect(loadVoicePrefs(bad)).toEqual(DEFAULT_VOICE_PREFS);
    expect(() => saveVoicePrefs({ ptt: "hold", replies: true }, bad)).not.toThrow();
  });

  it("every localStorage call in the lib is inside a try", () => {
    for (const name of ["storage", "loadVoicePrefs", "saveVoicePrefs"]) {
      const body = fn("lib/voiceDesktop.ts", name);
      const at = body.search(/localStorage|kv\?\./);
      expect(at).toBeGreaterThan(-1);
      expect(body.indexOf("try {")).toBeGreaterThan(-1);
      expect(body.indexOf("try {")).toBeLessThan(at);
    }
  });
});

describe("spoken replies", () => {
  it("name the session first", () => {
    expect(spokenLine("Login fix", "Fixed the login bug. Should I push it?")).toBe(
      "Login fix: Fixed the login bug. Should I push it?"
    );
  });

  it("speak only replies to prompts this app sent with say:true", () => {
    const t = new ReplyTracker();
    // A prompt typed at the keyboard, or sent from the phone: never tracked.
    expect(t.take("a", "A", "Done.", null, 1_000)).toBeNull();
    // A voice prompt sent with say:false (replies off): not either.
    t.sent("a", false, 1_000);
    expect(t.take("a", "A", "Done.", null, 2_000)).toBeNull();
    t.sent("b", true, 1_000);
    expect(t.take("b", "Login fix", "Done.", null, 2_000)).toBe("Login fix: Done.");
  });

  it("once per turn: idle and turn_ended both report it, the second is silent", () => {
    const t = new ReplyTracker();
    t.sent("a", true, 10_000);
    t.working("a");
    expect(t.take("a", "A", "All tests pass.", 11, 12_000)).toBe("A: All tests pass.");
    expect(t.take("a", "A", "All tests pass.", 11, 12_500)).toBeNull();
    // The next voice prompt's reply is news even when it repeats word for word.
    t.sent("a", true, 20_000);
    expect(t.take("a", "A", "All tests pass.", 21, 22_000)).toBe("A: All tests pass.");
  });

  it("a Say line older than the prompt is the previous turn's: wait, unless the turn ran", () => {
    const t = new ReplyTracker();
    t.sent("a", true, 100_000);
    expect(t.take("a", "A", "Old news.", 50, 101_000)).toBeNull();
    expect(t.waiting("a", 101_000)).toBe(true);
    t.working("a");
    expect(t.take("a", "A", "Old news.", 50, 102_000)).toBe("A: Finished.");
    expect(t.waiting("a", 102_000)).toBe(false);
  });
});

describe("SpeechQueue", () => {
  function fakeEngine() {
    const said: string[] = [];
    let end: (() => void) | null = null;
    const sp: Speaker = {
      speak: (text) =>
        new Promise<void>((resolve) => {
          said.push(text);
          end = resolve;
        }),
      cancel: () => end?.(),
    };
    return { sp, said, finish: () => end?.() };
  }

  it("says lines in order, one at a time, and waits while the mic listens", async () => {
    const e = fakeEngine();
    const states: boolean[] = [];
    const q = new SpeechQueue(() => e.sp, (on) => states.push(on));
    q.say("A: one.");
    q.say("B: two.");
    expect(e.said).toEqual(["A: one."]);
    q.hold(); // the mic keyed: stop talking
    await Promise.resolve();
    await Promise.resolve();
    expect(e.said).toEqual(["A: one."]);
    expect(q.queued).toBe(1);
    q.release();
    expect(e.said).toEqual(["A: one.", "B: two."]);
    expect(q.last).toBe("B: two.");
    expect(states[0]).toBe(true);
  });
});

describe("engines and errors", () => {
  const cfg = (over: Partial<VoiceCfg> = {}): VoiceCfg => ({ ...DEFAULT_VOICE_CFG, ...over });
  const ready = { stt: true, tts: true, stt_backend: "system", tts_backend: "piper" };

  it.each([
    // [where, voice.stt, server ready, page can record, → engine]
    ["desktop app", "phone", true, true, "server"],
    ["desktop app", "server", true, true, "server"],
    ["desktop app", "phone", false, true, "none"],
    ["desktop app", "server", false, true, "none"],
    ["desktop app", "server", true, false, "none"],
    ["browser tab", "phone", true, true, "browser"],
    ["browser tab", "phone", false, true, "browser"],
    ["browser tab", "server", true, true, "server"],
    ["browser tab", "server", false, true, "browser"],
    ["browser tab", "server", true, false, "browser"],
  ] as const)("recognition: %s, voice.stt=%s, ready=%s, can record=%s → %s", (where, stt, ok, rec, want) => {
    const c = cfg({ stt, server_ready: ok ? ready : {} });
    expect(sttEngine(c, rec, where === "desktop app")).toBe(want);
  });

  it.each([
    ["desktop app", "phone", true, "server"],
    ["desktop app", "phone", false, "browser"],
    ["browser tab", "phone", true, "browser"],
    ["browser tab", "server", true, "server"],
    ["browser tab", "server", false, "browser"],
  ] as const)("voice: %s, voice.tts=%s, ready=%s → %s", (where, tts, ok, want) => {
    expect(ttsEngine(cfg({ tts, server_ready: ok ? ready : {} }), where === "desktop app")).toBe(want);
  });

  it("with no recognizer, the desktop app points at the server's engine, never at another browser", () => {
    expect(noRecognizerLine(true)).toBe("Speech needs the server's engine here: Settings → Voice");
    expect(noRecognizerLine(false)).toBe(
      "Voice input needs Chrome, Edge or Safari — or server recognition (Settings → Voice)"
    );
  });

  it("the desktop app is the preload's mfapp bridge or Electron's user agent", () => {
    vi.stubGlobal("window", {});
    vi.stubGlobal("navigator", { userAgent: "Mozilla/5.0 Chrome/140.0" });
    expect(isElectron()).toBe(false);
    vi.stubGlobal("navigator", { userAgent: "Mozilla/5.0 Chrome/140.0 Electron/38.1.0" });
    expect(isElectron()).toBe(true);
    vi.stubGlobal("navigator", { userAgent: "" });
    vi.stubGlobal("window", { mfapp: {} });
    expect(isElectron()).toBe(true);
    vi.unstubAllGlobals();
  });

  it.each([
    [
      "the built-in engine on WSL + piper",
      cfg({ server_ready: ready, system: { available: true, platform: "wsl" } }),
      "server",
      "server",
      true,
      "Recognition: server, built in (Windows via WSL) — 16 kHz clips, browser resampler, a phrase ends after 1.1 s of quiet · Voice: server (piper)",
    ],
    [
      "the built-in engine on a Mac, both ways",
      cfg({ server_ready: { stt: true, tts: true, stt_backend: "system", tts_backend: "system" }, system: { platform: "macos" } }),
      "server",
      "server",
      true,
      "Recognition: server, built in (macOS) — 16 kHz clips, browser resampler, a phrase ends after 1.1 s of quiet · Voice: server, built in (macOS)",
    ],
    ["a browser tab on its own engines", cfg(), "browser", "browser", false, "Recognition: this browser · Voice: this browser"],
    [
      "the desktop app with no server engine",
      cfg({ server_ready: { stt: false, stt_detail: "no engine installed" } }),
      "none",
      "browser",
      true,
      "Recognition: none (server recognition isn't ready: no engine installed) · Voice: this browser",
    ],
    [
      "a tab asking for the server before it is ready",
      cfg({ stt: "server", server_ready: { stt: false, stt_detail: "whisper is not installed" } }),
      "browser",
      "browser",
      false,
      "Recognition: this browser (server recognition isn't ready: whisper is not installed) · Voice: this browser",
    ],
    ["a tab with no Web Speech", cfg(), "none", "none", false, "Recognition: none in this browser · Voice: none"],
  ] as const)("diagnostics: %s", (_name, c, stt, tts, electron, want) => {
    expect(engineDiagnostics(c, stt, tts, electron)).toBe(want);
  });

  it("says what a network failure and a denied mic mean", () => {
    expect(recognizerErrorLine("network")).toBe(
      "Speech recognition failed (network): use Chrome, Edge or Safari, or turn on server recognition in Settings → Voice"
    );
    expect(recognizerErrorLine("not-allowed")).toBe("Microphone permission denied");
  });

  it("chunks a reply by sentence without splitting file names", () => {
    expect(sentenceChunks("Login fix: Fixed note.txt today. Should I push it?")).toEqual([
      "Login fix: Fixed note.txt today.",
      "Should I push it?",
    ]);
  });
});

describe("serverRecognizer: a WAV clip per phrase", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    mocks.apiRaw.mockReset();
    mocks.startWavRecording.mockReset();
  });

  class FakeCtx {
    state = "running";
    sampleRate = 48000;
    resume() {
      return Promise.resolve();
    }
  }
  const stream = { active: true, getTracks: () => [] };

  function setup(text = "fix the login bug") {
    vi.stubGlobal("window", { AudioContext: FakeCtx });
    vi.stubGlobal("navigator", { language: "en-US", userAgent: "", mediaDevices: { getUserMedia: async () => stream } });
    const clips: Array<{ opts: { targetRate?: number; onLevel?: (rms: number) => void }; stop: ReturnType<typeof vi.fn>; cancel: ReturnType<typeof vi.fn> }> = [];
    mocks.startWavRecording.mockImplementation((_s: unknown, _c: unknown, opts) => {
      const blob = new Blob([new Uint8Array(44 + 3200)], { type: "audio/wav" });
      const clip = { opts, stop: vi.fn(() => Promise.resolve(blob)), cancel: vi.fn(), seconds: () => 0.1 };
      clips.push(clip);
      return clip;
    });
    mocks.apiRaw.mockImplementation(async () => ({ ok: true, status: 200, json: async () => ({ text }) }));
    const finals: string[] = [];
    const errors: string[] = [];
    let ended = 0;
    const h: RecognizerHandlers = {
      onInterim: () => {},
      onFinal: (t) => void finals.push(t),
      onError: (code, detail) => void errors.push(detail || code),
      onEnd: () => void ended++,
    };
    return { clips, finals, errors, h, ended: () => ended };
  }

  it("records through startWavRecording (16 kHz, the page's AudioContext) and posts audio/wav", async () => {
    const t = setup();
    const r = serverRecognizer();
    expect(r.available()).toBe(true);
    expect(r.start(t.h)).toBe(true);
    await vi.waitFor(() => expect(t.clips.length).toBe(1));
    const [s, ctx, opts] = mocks.startWavRecording.mock.calls[0];
    expect(s).toBe(stream);
    expect(ctx).toBeInstanceOf(FakeCtx);
    expect(opts.targetRate).toBe(16000);
    t.clips[0].opts.onLevel!(0.001); // the first block sets the noise floor
    t.clips[0].opts.onLevel!(0.3); // speech
    r.finish();
    await vi.waitFor(() => expect(t.ended()).toBe(1));
    expect(t.clips[0].stop).toHaveBeenCalled();
    expect(mocks.apiRaw).toHaveBeenCalledTimes(1);
    const [url, init] = mocks.apiRaw.mock.calls[0];
    expect(url).toBe("/api/voice/transcribe?lang=en-US");
    expect(init.method).toBe("POST");
    expect(init.headers["Content-Type"]).toBe("audio/wav");
    expect((init.body as Blob).type).toBe("audio/wav");
    expect(t.finals).toEqual(["fix the login bug"]);
    expect(t.errors).toEqual([]);
  });

  it("silence after speech ends the clip and starts the next; stop() cancels the one recording", async () => {
    const t = setup();
    let now = 1_000_000;
    vi.spyOn(Date, "now").mockImplementation(() => now);
    const r = serverRecognizer();
    r.start(t.h);
    await vi.waitFor(() => expect(t.clips.length).toBe(1));
    const level = t.clips[0].opts.onLevel!;
    level(0.001); // the noise floor
    level(0.3); // speech
    now += 1000;
    level(0.001);
    expect(t.clips[0].stop).not.toHaveBeenCalled(); // not quiet for long enough yet
    now += 200; // 1.2 s of quiet > SILENCE_MS (1.1 s)
    level(0.001);
    expect(t.clips[0].stop).toHaveBeenCalled();
    await vi.waitFor(() => expect(t.clips.length).toBe(2));
    await vi.waitFor(() => expect(t.finals).toEqual(["fix the login bug"]));
    r.stop();
    expect(t.clips[1].cancel).toHaveBeenCalled();
    expect(t.ended()).toBe(0); // stop() reports nothing
  });

  it("a clip the detector heard no speech in is not uploaded", async () => {
    const t = setup();
    const r = serverRecognizer();
    r.start(t.h);
    await vi.waitFor(() => expect(t.clips.length).toBe(1));
    t.clips[0].opts.onLevel!(0.001);
    r.finish();
    await vi.waitFor(() => expect(t.ended()).toBe(1));
    expect(mocks.apiRaw).not.toHaveBeenCalled();
  });

  it("a 409 (no engine ready) says where to set one up", async () => {
    const t = setup();
    mocks.apiRaw.mockImplementation(async () => ({ ok: false, status: 409, json: async () => ({ error: "no backend" }) }));
    const r = serverRecognizer();
    r.start(t.h);
    await vi.waitFor(() => expect(t.clips.length).toBe(1));
    t.clips[0].opts.onLevel!(0.001);
    t.clips[0].opts.onLevel!(0.3);
    r.finish();
    await vi.waitFor(() => expect(t.errors).toEqual(["Server recognition isn't ready: Settings → Voice"]));
  });

  it("no MediaRecorder left: the WAV recorder is the only way a clip is made", () => {
    const lib = code("lib/voiceDesktop.ts");
    expect(lib).not.toContain("MediaRecorder");
    expect(lib).not.toContain("SERVER_MIMES");
    expect(lib).toContain('import { resamplerName, startWavRecording, type WavRecording } from "./wavRecorder";');
    expect(lib).toContain('headers: { "Content-Type": "audio/wav" }');
  });
});

describe("wiring", () => {
  const TOPBAR = src("components/TopBar.tsx");
  const MIC = code("components/TopBarMic.tsx");

  it("sits right after the bell in .tb-left and right before it in the mac .tb-end", () => {
    const left = TOPBAR.slice(TOPBAR.indexOf('<div className="tb-left">'), TOPBAR.indexOf('<nav className="tb-menu"'));
    expect(left.indexOf("{!mac && <TopBarMic />}")).toBeGreaterThan(left.indexOf("{!mac && <NotificationsBell />}"));
    expect(left.indexOf("{!mac && <NotificationsBell />}")).toBeGreaterThan(-1);
    const end = TOPBAR.slice(TOPBAR.indexOf('<div className="tb-end">'));
    expect(end.indexOf("<TopBarMic />")).toBeGreaterThan(-1);
    expect(end.indexOf("<TopBarMic />")).toBeLessThan(end.indexOf("<NotificationsBell />"));
    // One button for the whole app, styled as a bar item, never an emoji.
    expect(ALL_CODE.split('id="mic-btn"').length - 1).toBe(1);
    expect(MIC).toContain('"tb-item"');
    expect(MIC).not.toContain("🎤");
  });

  it("the old hidden dictation button is gone, with its display:none rule", () => {
    expect(ALL_CODE).not.toContain("VoiceInput");
    expect(src("components/sidebar/toolbars.css")).not.toContain("#mic-btn");
    expect(ALL_CSS).not.toMatch(/#mic-btn[^{]*\{[^}]*display:\s*none\s*!important/);
    // The pulse moved with the button.
    expect(src("components/TopBarMic.css")).toContain("@keyframes mic-pulse");
    expect(cssRule(src("components/TopBarMic.css"), "#topbar #mic-btn.listening")).toContain("mic-pulse");
  });

  it("types into the session's agent terminal, never a whole-prompt /send; submit is /send {enter: true}", () => {
    const lib = code("lib/voiceDesktop.ts");
    expect(lib).toContain('peekTerm(title, "agent")?.send(data)');
    expect(lib).toMatch(/instApi<[^>]*>\(title, "\/send", \{ json: \{ enter: true, say \} \}\)/);
    // The old one-shot prompt send is gone from the mic.
    expect(MIC).not.toContain("dialog_safe");
    expect(MIC).not.toContain("sendPrompt");
    expect(MIC).toContain("submitted: (title, wantSay) => tracker.sent(title, wantSay)");
    expect(MIC).toContain("replies: () => prefsRef.current.replies");
  });

  it("the dialog guard reads the rows the app already polls", () => {
    expect(MIC).toContain('queryClient.getQueryData<Instance[]>(["instances"])');
    expect(MIC).toContain("effectiveActivity(");
    expect(MIC).toContain("activity: liveActivity");
  });

  it("asks the router with a dictation's first phrase, and selects through the one selection authority", () => {
    expect(MIC).toContain('routePhrase(text, useUi.getState().focused || "")');
    expect(MIC).toContain("selectSession(title, { noKeyboard: true })");
    expect(code("lib/voiceDesktop.ts")).toContain('api<RouteReply>("/api/voice/route", { json: { text, session } })');
  });

  it("push to talk ends in finish() (\u201cover\u201d), Escape in cancel() (\u201cdisregard\u201d + stop)", () => {
    const fin = blockFrom(MIC, "const finish = (", "// Escape on the mic");
    expect(fin).toContain("void d.finish()");
    expect(fin).not.toContain("instApi");
    expect(MIC).toContain("void live.current?.cancel()");
  });

  it("opens its options on right-click and Shift+click, and deep-links the speech engines", () => {
    expect(MIC).toContain("onContextMenu=");
    expect(MIC).toContain("e.shiftKey");
    expect(MIC).toContain("openSettings(VOICE_SCREEN)");
    expect(MIC).toContain('const VOICE_SCREEN = "voice" as SettingsScreen;');
    expect(MIC).toContain("Speech engines: Settings → Voice");
    expect(MIC).toContain("Read replies aloud");
    expect(MIC).toContain("Click to talk");
    expect(MIC).toContain("Hold to talk");
  });

  it("asks for the engine before starting: with none (the desktop app, server not ready) it says so and starts nothing", () => {
    const start = blockFrom(MIC, "const start = (", "const finish = (");
    const none = start.indexOf('if (engine === "none")');
    expect(none).toBeGreaterThan(-1);
    expect(start.indexOf("say(noRecognizerLine())")).toBeGreaterThan(none);
    expect(start.indexOf("r.start(")).toBeGreaterThan(none);
    expect(start.slice(none, start.indexOf("r.start("))).toContain("return;");
  });

  it("re-reads GET /api/voice on window focus and after a settings save", () => {
    expect(MIC).toContain('window.addEventListener("focus", onFocus)');
    expect(MIC).toContain('window.removeEventListener("focus", onFocus)');
    expect(MIC).toMatch(/getQueryCache\(\)\.subscribe[\s\S]{0,200}queryKey\[0\] !== "settings"/);
  });

  it("no old Settings path is left in the mic", () => {
    for (const f of ["components/TopBarMic.tsx", "lib/voiceDesktop.ts"]) expect(src(f)).not.toContain("Your phone → Voice");
  });
});


describe("the voice options popover closes easily", () => {
  it("has a close button, closes on a mic click, and watches pointerdown in the capture phase", () => {
    expect(MIC_SRC).toContain('id="mic-pop-close"');
    expect(MIC_SRC).toContain("if (menuOpen) {\n            setMenuOpen(false);");
    expect(MIC_SRC).toContain('document.addEventListener("pointerdown", onDown, true)');
    expect(MIC_SRC).not.toContain('document.addEventListener("mousedown", onDown)');
  });
});
