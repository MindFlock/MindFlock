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
  ANSWER_LISTEN_MS,
  APPROVE_OFF_LINE,
  controlMaxWords,
  FIRST_CHUNK_S,
  chunkMinS,
  passLine,
  retype,
  DEFAULT_VOICE_CFG,
  DEFAULT_VOICE_PREFS,
  dictHint,
  Dictation,
  HandsFree,
  LiveDictation,
  STOPPED_LINE,
  TALK_EVENT,
  dialogChoices,
  dialogRetryLine,
  dialogSpeech,
  holdBack,
  isAnswerPhrase,
  mayBeCommand,
  pendingDiagnostics,
  railRow,
  railStep,
  routePhrase,
  PENDING_MS,
  PendingChoice,
  runVoiceCommand,
  stopTail,
  talkKeyAction,
  LEFT_LINE,
  ReplyTracker,
  HeardTurns,
  HEARD_DEDUPE_MS,
  repliesMode,
  sayOnSend,
  voiceName,
  SPEECH_STALE_MS,
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
  type CommandDeps,
  type DictEnd,
  type DictTarget,
  type HandsFreeDeps,
  type LiveHooks,
  type RouteReply,
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
    ["Over it.", "over", ""],
    ["ship it over and out", "out", "ship it"],
    ["Over and out.", "out", ""],
    ["ship it, over, out!", "out", "ship it"],
    // "submit" is retired: an ordinary word, typed.
    ["run the tests submit", "", "run the tests submit"],
    ["Submit.", "", "Submit."],
    ["SUBMIT IT!", "", "SUBMIT IT!"],
    ["ok, submit that.", "", "ok, submit that."],
    ["hand it over and out of the way", "", "hand it over and out of the way"],
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
    // Interim words are a guess: typed, and shown greyed after the settled words.
    expect(d.hearing("then push")).toEqual({ kind: "type", text: "then push", route: false, guess: true });
    expect(d.shown).toBe("fix the login bug and add a test");
    expect(d.partial).toBe("then push");
    expect(d.listening).toBe(true);
  });

  it("“over” sends (the phrase before it typed first) and stops — unless continuous; “submit” is a word", () => {
    const d = new Dictation();
    d.begin();
    expect(d.heard("run the tests over")).toEqual({ kind: "send", text: "run the tests ", end: true });
    expect(d.listening).toBe(false);
    d.begin();
    expect(d.heard("Over.")).toEqual({ kind: "send", text: "", end: true });
    d.begin();
    expect(d.heard("run the tests, submit")).toEqual({ kind: "type", text: "run the tests, submit " });
    expect(d.listening).toBe(true);
    d.cancel();
    expect(d.heard("late words")).toEqual({ kind: "stopped" });
  });

  it("continuous: “over” sends and listening goes on; next() starts a new message; “over and out” sends and stops", () => {
    const d = new Dictation();
    d.continuous = true;
    d.begin();
    expect(d.heard("run the tests over")).toEqual({ kind: "send", text: "run the tests " });
    expect(d.listening).toBe(true);
    d.wrote("run the tests ");
    d.next();
    expect(d.typed).toBe("");
    expect(d.heard("then push it over and out")).toEqual({ kind: "send", text: "then push it ", end: true, out: true });
    expect(d.listening).toBe(false);
    // "Over and out, that's all": the stop phrase does not swallow the send.
    d.begin();
    expect(d.heard("ship it over and out, that's all")).toEqual({ kind: "send", text: "ship it ", end: true, out: true });
    // Let go (or a second click) mid-message: stop, even after an "over".
    d.begin();
    d.hearing("ship it over");
    expect(d.finish()).toEqual({ kind: "send", text: "ship it ", end: true });
  });

  it("“disregard” takes back exactly what this dictation typed, and keeps listening", () => {
    const d = new Dictation();
    d.begin();
    d.wrote("delete everything ");
    expect(d.heard("Disregard that.")).toEqual({ kind: "erase", text: "delete everything " });
    expect(d.listening).toBe(true);
    expect(d.typed).toBe("");
    expect(d.heard("keep everything over")).toEqual({ kind: "send", text: "keep everything ", end: true });
  });

  it("push to talk let go stops and leaves the words, and a phrase still in flight counts", () => {
    const d = new Dictation();
    d.begin();
    d.hearing("switch to the login fix");
    expect(d.finish()).toEqual({ kind: "leave", text: "switch to the login fix " });
    expect(d.listening).toBe(false);
    d.begin();
    expect(d.finish()).toEqual({ kind: "leave", text: "" });
    d.begin();
    d.hearing("ship it over");
    expect(d.finish()).toEqual({ kind: "send", text: "ship it ", end: true });
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
    expect(d.expire()).toEqual({ kind: "leave", text: "" });
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
    const tails: string[] = [];
    const calls = { halt: 0, routed: [] as string[], submitted: [] as Array<[string, boolean]> };
    const h: LiveHooks = {
      caption: (t, _sticky, unsure) => {
        captions.push(t);
        tails.push(unsure || "");
      },
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
    return { h, captions, tails, calls, last: () => captions[captions.length - 1] };
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

  it("a stop phrase stops with the words in the field, nothing sent", async () => {
    const sent = terms();
    const k = hooks();
    const d = new LiveDictation(sessionTarget("t1", "Login fix"), k.h);
    await d.heard("run the tests");
    await d.heard("stop recording");
    expect(sent.t1).toEqual(["run the tests "]);
    expect(k.last()).toBe(LEFT_LINE);
    expect(LEFT_LINE).toBe("In the field — press Enter to send");
    expect(k.calls.halt).toBe(1);
    expect(mocks.instApi).not.toHaveBeenCalled();
  });

  it("push to talk let go stops: the phrase in flight is typed, nothing sent", async () => {
    const sent = terms();
    const k = hooks();
    const d = new LiveDictation(sessionTarget("t1", "Login fix"), k.h);
    await d.heard("fix the bug");
    await d.hearing("and the test");
    expect(sent.t1).toEqual(["fix the bug ", "and the test"]); // typed as heard
    await d.finish();
    expect(sent.t1).toEqual(["fix the bug ", "and the test", " "]);
    expect(k.last()).toBe(LEFT_LINE);
    expect(mocks.instApi).not.toHaveBeenCalled();
    expect(d.active).toBe(false);
    // Anything the recognizer delivers afterwards is ignored.
    await d.heard("late");
    expect(sent.t1).toHaveLength(3);
  });

  it("“over” types the phrase, then presses Enter through /send {enter: true, say}", async () => {
    const sent = terms();
    for (const replies of [false, true]) {
      mocks.instApi.mockResolvedValueOnce({ ok: true });
      const k = hooks({ replies: () => replies });
      const d = new LiveDictation(sessionTarget("t1", "Login fix"), k.h);
      await d.heard("run the tests over");
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
    await d.heard("yes over");
    expect(k.last()).toBe("Login fix is on a dialog — press Enter yourself when it is answered");
    expect(k.calls.submitted).toEqual([]); // nothing went: no reply to wait for
  });

  it("a text box: words go in at the cursor; “over” and “over and out” only stop — Enter is never pressed", async () => {
    terms();
    for (const word of ["over", "over and out"]) {
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

  describe("streamed words", () => {
    it("are typed as soon as the first pass hears them — the unsure tail too — and corrected as they change", async () => {
      const sent = terms();
      const k = hooks();
      const d = new LiveDictation(sessionTarget("t1", "Login fix"), k.h);
      await d.stream("", "fix the");
      expect(sent.t1).toEqual(["fix the"]); // the first pass, before anything commits
      expect(k.last()).toBe(""); // settled: nothing yet…
      expect(k.tails[k.tails.length - 1]).toBe("fix the"); // …the guess greyed
      await d.stream("fix the", "log in");
      expect(sent.t1).toEqual(["fix the", " log in"]);
      await d.stream("", "login bug");
      // From where they differ: " log in" → " login bug" takes back " in" and types "in bug".
      expect(sent.t1.slice(2)).toEqual(["\x7f\x7f\x7f", "in bug"]);
      expect(k.last()).toBe("fix the"); // committed words are solid
      expect(k.tails[k.tails.length - 1]).toBe("login bug");
      await d.flush("login bug");
      expect(sent.t1.slice(4)).toEqual([" "]); // the phrase's end: its trailing space
      expect(k.calls.routed).toEqual([]);
      expect(d.active).toBe(true);
    });

    it("each answer is one correction: the backspaces, then the new end", async () => {
      const sent = terms();
      const d = new LiveDictation(sessionTarget("t1", "Login fix"), hooks().h);
      await d.stream("", "run the test");
      await d.stream("run the", "tests now");
      expect(sent.t1).toEqual(["run the test", "s now"]); // a longer guess: no backspace
      await d.stream("", "tests");
      expect(sent.t1.slice(2)).toEqual(["\x7f\x7f\x7f\x7f"]); // a shorter one: backspaces only
      await d.stream("tests", "");
      expect(sent.t1).toHaveLength(3); // the same text: no keys
    });

    it("a trailing “over” in the partial is never typed, and in the flush presses Enter", async () => {
      const sent = terms();
      mocks.instApi.mockResolvedValueOnce({ ok: true });
      const k = hooks();
      const d = new LiveDictation(sessionTarget("t1", "Login fix"), k.h);
      await d.stream("run the", "tests over");
      expect(sent.t1).toEqual(["run the tests"]);
      expect(mocks.instApi).not.toHaveBeenCalled(); // held: more words may follow
      await d.flush("tests over");
      expect(sent.t1).toEqual(["run the tests"]); // no trailing space before Enter
      expect(sent.t1.join("")).not.toContain("over");
      expect(mocks.instApi).toHaveBeenCalledWith("t1", "/send", { json: { enter: true, say: false } });
      expect(k.calls.halt).toBe(1);
    });

    it("“over” arriving in the flush itself works the same; “over and out” is held whole; “submit” is typed", async () => {
      const sent = terms();
      mocks.instApi.mockResolvedValue({ ok: true });
      const k = hooks();
      const d = new LiveDictation(sessionTarget("t1", "Login fix"), k.h);
      await d.words("ship the fix");
      await d.flush("Over.");
      expect(sent.t1).toEqual(["ship the fix"]);
      expect(mocks.instApi).toHaveBeenCalledTimes(1);
      const sent2 = terms();
      const d2 = new LiveDictation(sessionTarget("t2", "Billing"), hooks().h);
      await d2.stream("deploy it", "over and");
      expect(sent2.t2).toEqual(["deploy it"]);
      await d2.stream("over and", "out");
      expect(sent2.t2).toEqual(["deploy it"]);
      await d2.flush("out");
      expect(sent2.t2).toEqual(["deploy it"]);
      expect(mocks.instApi).toHaveBeenCalledTimes(2);
      // "submit" is a word: typed as it comes, nothing sent.
      const sent3 = terms();
      const d3 = new LiveDictation(sessionTarget("t3", "Docs"), hooks().h);
      await d3.stream("", "please submit");
      await d3.flush("please submit");
      expect(sent3.t3.join("")).toBe("please submit ");
      expect(mocks.instApi).toHaveBeenCalledTimes(2);
    });

    it("a held control word goes in once the next word rules it out", async () => {
      const sent = terms();
      const d = new LiveDictation(sessionTarget("t1", "Login fix"), hooks().h);
      await d.stream("", "move it over");
      expect(sent.t1).toEqual(["move it"]);
      await d.stream("", "move it over here");
      expect(sent.t1).toEqual(["move it", " over here"]);
      await d.stream("", "move it over here and stop");
      expect(sent.t1.slice(2)).toEqual([" and"]); // "stop" may be "stop recording"
    });

    it("“stop recording” at the end of a streamed phrase stops with the words in the field", async () => {
      const sent = terms();
      const k = hooks();
      const d = new LiveDictation(sessionTarget("t1", "Login fix"), k.h);
      await d.stream("", "leave it there stop recording");
      await d.flush("leave it there stop recording");
      expect(sent.t1).toEqual(["leave it there", " "]);
      expect(k.last()).toBe(LEFT_LINE);
      expect(mocks.instApi).not.toHaveBeenCalled();
    });

    it("“disregard” backspaces everything this dictation typed, the guess included", async () => {
      const sent = terms();
      const k = hooks();
      const d = new LiveDictation(sessionTarget("t1", "Login fix"), k.h);
      await d.heard("café"); // a phrase from before (clip or stream alike)
      await d.stream("scrap this", "whole thing disregard");
      expect(sent.t1).toEqual(["café ", "scrap this whole thing"]);
      await d.flush("whole thing disregard that");
      expect(sent.t1[2]).toBe("\x7f".repeat("café scrap this whole thing".length));
      expect(k.last()).toBe("Disregarded — go on");
      expect(d.active).toBe(true);
    });

    it("never takes back past the phrase's start: what came before stays", async () => {
      const sent = terms();
      const d = new LiveDictation(sessionTarget("t1", "Login fix"), hooks().h);
      await d.heard("first part");
      await d.stream("", "fix it");
      await d.stream("", "Different words");
      expect(sent.t1).toEqual(["first part ", "fix it", "\x7f\x7f\x7f\x7f\x7f\x7f", "Different words"]);
      expect(d.dict.typed).toBe("first part Different words");
    });

    it("a first phrase that may be a command waits whole for the router — even a guess that turns into one", async () => {
      const sent = terms();
      const k = hooks({
        route: async (t) => {
          k.calls.routed.push(t);
          return true;
        },
      });
      const d = new LiveDictation(sessionTarget("t1", "Login fix"), k.h);
      await d.stream("", "switch two the");
      expect(sent.t1).toEqual(["switch two the"]); // can't be a command: typed
      await d.stream("switch to", "the login fix");
      expect(sent.t1).toEqual(["switch two the", "\x7f".repeat("switch two the".length)]); // now it may: taken back
      expect(k.calls.routed).toEqual([]);
      await d.flush("the login fix");
      expect(k.calls.routed).toEqual(["switch to the login fix"]);
      expect(sent.t1).toHaveLength(2);
    });

    it("…and is typed once the words rule a command out", async () => {
      const sent = terms();
      const k = hooks();
      const d = new LiveDictation(sessionTarget("t1", "Login fix"), k.h);
      await d.words("stop");
      expect(sent.t1).toBeUndefined();
      await d.words("the server first");
      expect(sent.t1).toEqual(["stop the server first"]);
      expect(k.calls.routed).toEqual([]);
    });

    it("a radio word followed by more words is just a word", async () => {
      const sent = terms();
      const d = new LiveDictation(sessionTarget("t1", "Login fix"), hooks().h);
      await d.words("over");
      await d.words("the form then");
      await d.flush("wait");
      expect(sent.t1).toEqual(["over the form then", " wait "]);
      expect(mocks.instApi).not.toHaveBeenCalled();
    });

    it("the dialog guard still comes first: no guess goes in, and the phrase's end says why", async () => {
      const sent = terms();
      const k = hooks({ activity: () => "clarify" });
      const d = new LiveDictation(sessionTarget("t1", "Login fix"), k.h);
      await d.stream("yes do", "it now");
      expect(sent.t1).toBeUndefined();
      expect(d.active).toBe(true);
      await d.flush("it now");
      expect(sent.t1).toBeUndefined();
      expect(k.last()).toBe("Login fix is waiting on a dialog — answer it first");
      expect(d.active).toBe(false);
    });

    it("with no session focused a guess isn't typed, and doesn't end the dictation", async () => {
      const k = hooks();
      const d = new LiveDictation(null, k.h);
      await d.stream("", "Swish to the");
      expect(d.active).toBe(true);
      await d.flush("fix the bug");
      expect(k.last()).toBe("Click a session first — “fix the bug” not typed");
    });

    it("a text box takes streamed words too, never routed, a correction setting its value once", async () => {
      const ops: string[] = [];
      const box: DictTarget = {
        kind: "field",
        title: "",
        name: "the text box",
        type: (t) => ops.push("type:" + t) > 0,
        erase: (t) => ops.push("erase:" + t) > 0,
        replace: (old, t) => ops.push("replace:" + old + "→" + t) > 0,
      };
      const k = hooks();
      const d = new LiveDictation(box, k.h);
      await d.stream("", "switch to the");
      await d.stream("switch to the", "other bran");
      await d.stream("", "other branch");
      await d.flush("other branch");
      expect(ops).toEqual(["replace:→switch to the", "replace:→ other bran", "replace:→ch", "replace:→ "]);
      await d.stream("", "and the tusts");
      await d.stream("", "and the tests");
      expect(ops.slice(4)).toEqual(["replace:→and the tusts", "replace:usts→ests"]);
      expect(k.calls.routed).toEqual([]);
    });
  });

  it("before a word, the caption names the radio words", () => {
    const k = hooks();
    new LiveDictation(sessionTarget("t1", "Login fix"), k.h).show();
    expect(k.last()).toBe("Listening… · " + dictHint()); // "Listening…" from the press, before the mic records
    expect(dictHint()).toBe("Say “over” to send, “over and out” to stop, “disregard” to start over");
  });
});

describe("typing as you speak: the guess and its corrections", () => {
  it.each([
    // [typed so far, the new text, backspaced, typed]
    ["", "fix the", "", "fix the"],
    ["fix the", "fix the login bug", "", " login bug"], // committed beyond what was typed
    ["fix the bu", "fix the bag", "u", "ag"], // a mid-word change
    ["fix the log in", "fix the login", " in", "in"],
    ["run the tests now", "run the tests", " now", ""], // a shorter partial
    ["same", "same", "", ""],
    ["café au", "café olé", "au", "olé"], // by code point
    ["ship 🚀 now", "ship 🚀", " now", ""],
    ["Fix it", "fix it", "Fix it", "fix it"],
  ] as const)("retype(%j → %j): erase %j, type %j", (before, after, erase, type) => {
    expect(retype(before, after)).toEqual({ erase, type });
    expect(backspaces(erase).length).toBe(Array.from(erase).length);
  });

  it("the machine: the phrase is guessed as it comes, settled at its end with its trailing space", () => {
    const d = new Dictation();
    d.begin();
    const keys = (o: ReturnType<Dictation["stream"]>) => {
      if (o.kind === "type" || o.kind === "leave" || o.kind === "send") {
        d.unwrote(o.erase || "");
        d.wrote(o.text);
      }
      return o;
    };
    expect(keys(d.stream("", "fix the"))).toEqual({ kind: "type", text: "fix the", route: false, guess: true });
    expect(keys(d.stream("fix the", "bag"))).toEqual({ kind: "type", text: " bag", route: false, guess: true });
    expect(keys(d.stream("", "bug"))).toEqual({ kind: "type", text: "ug", erase: "ag", route: false, guess: true });
    expect(d.guessed).toBe("fix the bug");
    expect(d.shown).toBe("fix the"); // settled: the committed words
    expect(keys(d.stream("", "bug"))).toEqual({ kind: "continue" }); // no change, no keys
    expect(keys(d.flush("bug."))).toEqual({ kind: "type", text: ". " });
    expect(d.typed).toBe("fix the bug. ");
    expect(d.guessed).toBe("");
    // The next phrase never reaches back into this one.
    expect(keys(d.stream("", "Totally different"))).toEqual({ kind: "type", text: "Totally different", route: false, guess: true });
    expect(keys(d.stream("", "X"))).toEqual({ kind: "type", text: "X", erase: "Totally different", route: false, guess: true });
    expect(d.typed).toBe("fix the bug. X");
  });

  it("the control-phrase hold: a trailing radio word or stop phrase — or its start — is never typed, even as a guess", () => {
    const d = new Dictation();
    d.begin();
    const typed = (o: ReturnType<Dictation["stream"]>) => (o.kind === "type" ? (d.wrote(o.text), o.text) : "");
    expect(typed(d.stream("", "please fix it over"))).toBe("please fix it");
    expect(typed(d.stream("", "please fix it over and"))).toBe(""); // …out?
    expect(typed(d.stream("", "please fix it disregard that"))).toBe("");
    expect(typed(d.stream("", "please fix it stop"))).toBe(""); // …recording?
    expect(typed(d.stream("", "please fix it that is"))).toBe(""); // …all?
    expect(d.typed).toBe("please fix it");
    // Committed, the stop phrase acts: the guess is corrected to the words before it.
    const o = d.stream("please fix it stop recording", "");
    expect(o).toEqual({ kind: "leave", text: " ", stop: true });
    expect(d.listening).toBe(false);
  });

  it("“disregard” at the end takes the whole dictation out — the phrase's guess too", () => {
    const d = new Dictation();
    d.begin();
    d.wrote("earlier words ");
    const g = d.stream("scrap", "that idea");
    if (g.kind === "type") d.wrote(g.text);
    expect(d.typed).toBe("earlier words scrap that idea");
    expect(d.flush("that idea disregard")).toEqual({ kind: "erase", text: "earlier words scrap that idea" });
    expect(d.typed).toBe("");
    expect(d.listening).toBe(true);
  });

  it("“stop talking” as the whole phrase takes its guess back; a hush is never typed", () => {
    const d = new Dictation();
    d.begin();
    d.wrote("one ");
    const g = d.stream("", "please");
    expect(g).toEqual({ kind: "type", text: "please", route: false, guess: true });
    d.wrote("please");
    expect(d.stream("please", "be quiet")).toEqual({ kind: "continue" }); // "be quiet" held whole
    expect(d.flush("be quiet")).toEqual({ kind: "hush", erase: "please" });
  });

  it("the browser recognizer's interim words go the same way: typed, corrected, then settled by the final", async () => {
    const sent: string[] = [];
    mocks.peekTerm.mockImplementation(() => ({ send: (x: string) => (sent.push(x), true) }));
    try {
      const h: LiveHooks = {
        caption: () => {},
        halt: () => {},
        route: async () => false,
        activity: () => "idle",
        replies: () => false,
        submitted: () => {},
      };
      const d = new LiveDictation(sessionTarget("t1", "Login fix"), h);
      await d.hearing("fix the");
      await d.hearing("fix the bug");
      await d.hearing("fix the bag");
      await d.heard("fix the bag.");
      expect(sent).toEqual(["fix the", " bug", "\x7f\x7f", "ag", ". "]);
      // A later phrase: interim words after the first final.
      await d.hearing("and run");
      await d.hearing("and run the tests over");
      await d.heard("and run the tests over");
      expect(sent.slice(5)).toEqual(["and run", " the tests"]);
      expect(d.active).toBe(false);
      expect(MIC_SRC).toContain("void d.hearing(t)");
    } finally {
      mocks.peekTerm.mockReset();
    }
  });
});

describe("streaming helpers", () => {
  it.each([
    ["fix the login bug", "fix the login bug", ""],
    ["run the tests submit", "run the tests submit", ""],
    ["deploy it over it", "deploy it", "over it"],
    ["deploy it over and", "deploy it", "over and"],
    ["deploy it over and out", "deploy it", "over and out"],
    ["deploy it, over, out", "deploy it,", "over, out"],
    ["hand it over and back", "hand it over and back", ""],
    ["scrap that, Disregard that.", "scrap that,", "Disregard that."],
    ["move it over", "move it", "over"],
    ["it", "it", ""],
    ["fine .", "fine .", ""],
    ["please stop", "please", "stop"],
    ["over .", "", "over ."],
    ["", "", ""],
  ])("holdBack(%j) types %j, holds %j", (text, type, held) => {
    expect(holdBack(text)).toEqual({ type, held });
  });

  it.each([
    // The router's commands, from their first word on.
    ["switch", true],
    ["switch to the login fix", true],
    ["go over to billing", true],
    ["ok switch to billing", true],
    ["what needs me", true],
    ["What's it doing?", true],
    ["what sessions do i have", true],
    ["start a new session for the billing page", true],
    ["a", true],
    ["new session fix the bug", true],
    ["stop", true],
    ["stop please", true],
    ["cancel", true],
    ["never mind", true],
    ["repeat that", true],
    ["turn voice off", true],
    ["", true],
    // Prompts: typed as they come.
    ["fix the login bug", false],
    ["go ahead and push", false],
    ["what does this function do", false],
    ["stop the server", false],
    ["a bug in the parser", false],
    ["status of the tests", false],
    ["please fix it", false],
  ] as const)("mayBeCommand(%j) → %s", (text, want) => {
    expect(mayBeCommand(text)).toBe(want);
  });
});

describe("prefs: cs_voice_desktop", () => {
  it("defaults to click-to-talk with replies OFF, no wake-word standby and no spoken confirmations", () => {
    expect(DEFAULT_VOICE_PREFS).toEqual({ ptt: "toggle", replies: "off", wake: false, confirm: false, sounds: true });
    expect(loadVoicePrefs(memKV())).toEqual({ ptt: "toggle", replies: "off", wake: false, confirm: false, sounds: true });
    // Stored as `nods`; the old `confirm` key (true in every saved copy) is
    // ignored — the owner heard "Sent." after "over and out" as an echo.
    expect(loadVoicePrefs(memKV({ [VOICE_PREFS_KEY]: '{"confirm":true}' })).confirm).toBe(false);
    expect(loadVoicePrefs(memKV({ [VOICE_PREFS_KEY]: '{"nods":true}' })).confirm).toBe(true);
    expect(loadVoicePrefs(null)).toEqual(DEFAULT_VOICE_PREFS);
  });

  it("round-trips the three-way choice, and garbage reads as the defaults", () => {
    for (const replies of ["off", "mine", "all"] as const) {
      const kv = memKV();
      saveVoicePrefs({ ptt: "hold", replies }, kv);
      expect(JSON.parse(kv.m.get(VOICE_PREFS_KEY)!)).toEqual({ ptt: "hold", replies, wake: false, nods: false, sounds: true });
      expect(loadVoicePrefs(kv)).toEqual({ ptt: "hold", replies, wake: false, confirm: false, sounds: true });
    }
    expect(loadVoicePrefs(memKV({ [VOICE_PREFS_KEY]: "{nope" }))).toEqual(DEFAULT_VOICE_PREFS);
    expect(loadVoicePrefs(memKV({ [VOICE_PREFS_KEY]: '{"ptt":"sideways","replies":"yes"}' }))).toEqual(
      DEFAULT_VOICE_PREFS
    );
  });

  it("the old switch loads as its meaning: true is 'mine' (replies to what you said), false 'off'", () => {
    expect(loadVoicePrefs(memKV({ [VOICE_PREFS_KEY]: '{"ptt":"hold","replies":true}' }))).toEqual({ ptt: "hold", replies: "mine", wake: false, confirm: false, sounds: true });
    expect(loadVoicePrefs(memKV({ [VOICE_PREFS_KEY]: '{"ptt":"toggle","replies":false}' }))).toEqual({ ptt: "toggle", replies: "off", wake: false, confirm: false, sounds: true });
    expect(repliesMode(true)).toBe("mine");
    expect(repliesMode("all")).toBe("all");
    expect(repliesMode(undefined)).toBe("off");
  });

  it("say:true on a send: voice sends whenever replies are read, typed sends only with Everything", () => {
    const p = (replies: "off" | "mine" | "all") => ({ ptt: "toggle" as const, replies });
    expect([sayOnSend(true, p("off")), sayOnSend(true, p("mine")), sayOnSend(true, p("all"))]).toEqual([false, true, true]);
    expect([sayOnSend(false, p("off")), sayOnSend(false, p("mine")), sayOnSend(false, p("all"))]).toEqual([false, false, true]);
  });

  it("the wake-word standby: on only when saved true, kept through a save, absent reads as off", () => {
    const kv = memKV();
    saveVoicePrefs({ ptt: "toggle", replies: "off", wake: true, confirm: true, sounds: true }, kv);
    expect(JSON.parse(kv.m.get(VOICE_PREFS_KEY)!)).toEqual({ ptt: "toggle", replies: "off", wake: true, nods: true, sounds: true });
    expect(loadVoicePrefs(kv).wake).toBe(true);
    saveVoicePrefs({ ...loadVoicePrefs(kv), replies: "mine" }, kv);
    expect(loadVoicePrefs(kv)).toEqual({ ptt: "toggle", replies: "mine", wake: true, confirm: true, sounds: true });
    for (const raw of ['{"wake":"yes"}', '{"wake":1}', '{"ptt":"hold"}'])
      expect(loadVoicePrefs(memKV({ [VOICE_PREFS_KEY]: raw })).wake).toBe(false);
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
    expect(() => saveVoicePrefs({ ptt: "hold", replies: "all" }, bad)).not.toThrow();
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
  it("name the session first — unless it is the focused one", () => {
    expect(spokenLine("Login fix", "Fixed the login bug. Should I push it?")).toBe(
      "Login fix: Fixed the login bug. Should I push it?"
    );
    expect(voiceName("t1", "t2", "Login fix")).toBe("Login fix");
    expect(voiceName("t1", "t1", "Login fix")).toBe("");
    expect(spokenLine(voiceName("t1", "t1", "Login fix"), "Done.")).toBe("Done.");
    expect(voiceName("t1", null, "Login fix")).toBe("Login fix");
  });

  it("speak only replies to prompts this app sent with say:true", () => {
    const t = new ReplyTracker();
    // A prompt typed at the keyboard, or sent from the phone: never tracked.
    expect(t.take("a", "Done.", null, 1_000)).toBeNull();
    // A voice prompt sent with say:false (replies off): not either.
    t.sent("a", false, 1_000);
    expect(t.take("a", "Done.", null, 2_000)).toBeNull();
    t.sent("b", true, 1_000);
    expect(t.take("b", "Done.", null, 2_000)).toBe("Done.");
  });

  it("once per turn: idle and turn_ended both report it, the second is silent", () => {
    const t = new ReplyTracker();
    t.sent("a", true, 10_000);
    t.working("a");
    expect(t.take("a", "All tests pass.", 11, 12_000)).toBe("All tests pass.");
    expect(t.take("a", "All tests pass.", 11, 12_500)).toBeNull();
    // The next voice prompt's reply is news even when it repeats word for word.
    t.sent("a", true, 20_000);
    expect(t.take("a", "All tests pass.", 21, 22_000)).toBe("All tests pass.");
  });

  it("a Say line older than the prompt is the previous turn's: wait, unless the turn ran", () => {
    const t = new ReplyTracker();
    t.sent("a", true, 100_000);
    expect(t.take("a", "Old news.", 50, 101_000)).toBeNull();
    expect(t.waiting("a", 101_000)).toBe(true);
    t.working("a");
    expect(t.take("a", "Old news.", 50, 102_000)).toBe("Finished.");
    expect(t.waiting("a", 102_000)).toBe(false);
  });

  it("Everything: every session's turn once — idle and turn_ended carry the same words", () => {
    const h = new HeardTurns();
    expect(h.fresh("a", "All tests pass.", null, 1_000)).toBe(true);
    expect(h.fresh("a", "All  tests pass. ", null, 40_000)).toBe(false); // turn_ended, later
    expect(h.fresh("b", "All tests pass.", null, 40_000)).toBe(true); // another session
    // Not the same report any more: news again.
    expect(h.fresh("a", "All tests pass.", null, 1_000 + HEARD_DEDUPE_MS + 1)).toBe(true);
    expect(h.fresh("a", "Pushed.", null, 1_000 + HEARD_DEDUPE_MS + 2)).toBe(true);
    // A stamp, when the event carries one, is the identity.
    expect(h.fresh("c", "Done.", 7, 0)).toBe(true);
    expect(h.fresh("c", "Done.", 7, 10 * HEARD_DEDUPE_MS)).toBe(false);
    expect(h.fresh("c", "Done.", 8, 10 * HEARD_DEDUPE_MS)).toBe(true);
    // No words: nothing to read.
    expect(h.fresh("d", "  ", null, 0)).toBe(false);
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

  const tick = async () => {
    for (let i = 0; i < 4; i++) await Promise.resolve();
  };

  it("a burst from several sessions plays in arrival order, never two at once", async () => {
    const e = fakeEngine();
    let at = 0;
    let max = 0;
    const live = { n: 0 };
    const sp: Speaker = {
      speak: (t) => {
        live.n++;
        max = Math.max(max, live.n);
        return e.sp.speak(t).then(() => {
          live.n--;
        });
      },
      cancel: () => e.sp.cancel(),
    };
    const q = new SpeechQueue(() => sp, () => {}, { now: () => at });
    const done: string[] = [];
    for (const l of ["A: one.", "B: two.", "C: three."]) void q.say(l).then(() => done.push(l));
    expect(e.said).toEqual(["A: one."]);
    for (let i = 0; i < 3; i++) {
      at += 1000;
      e.finish();
      await tick();
    }
    expect(e.said).toEqual(["A: one.", "B: two.", "C: three."]);
    expect(done).toEqual(["A: one.", "B: two.", "C: three."]);
    expect(max).toBe(1);
  });

  it("holds while the mic listens: nothing new starts, then it resumes where it was", async () => {
    const e = fakeEngine();
    const q = new SpeechQueue(() => e.sp);
    q.hold(); // listening before anything is queued
    void q.say("A: one.");
    void q.say("B: two.");
    await tick();
    expect(e.said).toEqual([]);
    expect(q.queued).toBe(2);
    q.release();
    expect(e.said).toEqual(["A: one."]);
    e.finish();
    await tick();
    expect(e.said).toEqual(["A: one.", "B: two."]);
  });

  it("a line older than SPEECH_STALE_MS when its turn comes is dropped (logged, not said)", async () => {
    const e = fakeEngine();
    let at = 0;
    const logs: string[] = [];
    const q = new SpeechQueue(() => e.sp, () => {}, { now: () => at, log: (m) => logs.push(m) });
    void q.say("A: a long one.");
    const stale = q.say("B: old news.");
    void q.say("C: fresh.", { at: SPEECH_STALE_MS + 5_000 });
    at = SPEECH_STALE_MS + 10_000; // A took more than a minute
    e.finish();
    await stale; // resolves although never said
    await tick();
    expect(e.said).toEqual(["A: a long one.", "C: fresh."]);
    expect(logs).toHaveLength(1);
    expect(logs[0]).toMatch(/dropped a line \d+ s old: B: old news\./);
    expect(q.last).toBe("C: fresh.");
  });

  it("drained() resolves once nothing is left — a hands-free listen waits for it", async () => {
    const e = fakeEngine();
    const q = new SpeechQueue(() => e.sp);
    let drained = false;
    await q.drained(); // idle: at once
    void q.say("A: one.");
    void q.say("B: two.");
    void q.drained().then(() => (drained = true));
    e.finish();
    await tick();
    expect(drained).toBe(false);
    e.finish();
    await tick();
    expect(drained).toBe(true);
    // cancel() drains too.
    void q.say("C: three.");
    let again = false;
    void q.drained().then(() => (again = true));
    q.cancel();
    await tick();
    expect(again).toBe(true);
  });

  it("a line can be worded when its turn comes (the name first only if that session isn't focused then)", async () => {
    const e = fakeEngine();
    let focused = "t1";
    const q = new SpeechQueue(() => e.sp);
    void q.say("X: first.");
    void q.say(() => spokenLine(voiceName("t1", focused, "Login fix"), "Pushed."));
    focused = "t2"; // you moved on while the first line played
    e.finish();
    await tick();
    expect(e.said).toEqual(["X: first.", "Login fix: Pushed."]);
    expect(q.last).toBe("Login fix: Pushed.");
  });

  it("cancels the engine that is speaking, even when the choice of engine moved since", async () => {
    const a = fakeEngine();
    const b = fakeEngine();
    let useB = false;
    const cancelled: string[] = [];
    const wrap = (f: ReturnType<typeof fakeEngine>, n: string): Speaker => ({ speak: f.sp.speak, cancel: () => (cancelled.push(n), f.sp.cancel()) });
    const sa = wrap(a, "a");
    const sb = wrap(b, "b");
    const q = new SpeechQueue(() => (useB ? sb : sa));
    void q.say("one");
    useB = true;
    q.hold();
    expect(cancelled).toEqual(["a"]);
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
      "Recognition: server, built in (Windows via WSL) — one clip per phrase: 16 kHz, browser resampler, a phrase ends after 1.1 s of quiet · Voice: server (piper)",
    ],
    [
      "the built-in engine on a Mac, both ways",
      cfg({ server_ready: { stt: true, tts: true, stt_backend: "system", tts_backend: "system" }, system: { platform: "macos" } }),
      "server",
      "server",
      true,
      "Recognition: server, built in (macOS) — one clip per phrase: 16 kHz, browser resampler, a phrase ends after 1.1 s of quiet · Voice: server, built in (macOS)",
    ],
    [
      "local whisper, streaming",
      cfg({ server_ready: { stt: true, stt_backend: "local", stt_stream: true } }),
      "server",
      "browser",
      true,
      "Recognition: server (local) — streaming (words as you speak): 16 kHz, windowed-sinc resampler, a phrase ends after 1.1 s of quiet · Voice: this browser",
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

describe("serverRecognizer: streaming, words as you speak", () => {
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
  const resp = (status: number, body: unknown) => ({ ok: status < 300, status, json: async () => body });
  const wav = (n = 3200) => new Blob([new Uint8Array(44 + n)], { type: "audio/wav" });

  type Route = (url: string, init: RequestInit) => unknown;
  /** A recorder whose take() has a chunk once `feed()` says enough audio came,
   * a server answering through `route`, and handlers that record everything. */
  function setup(route: Route) {
    vi.stubGlobal("window", { AudioContext: FakeCtx });
    vi.stubGlobal("navigator", { language: "en-US", userAgent: "", mediaDevices: { getUserMedia: async () => stream } });
    let now = 1_000_000;
    vi.spyOn(Date, "now").mockImplementation(() => now);
    const clips: Array<{
      opts: { onLevel?: (rms: number) => void };
      take: ReturnType<typeof vi.fn>;
      stop: ReturnType<typeof vi.fn>;
      cancel: ReturnType<typeof vi.fn>;
      feed(): void;
    }> = [];
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
    const calls: Array<[string, string]> = [];
    mocks.apiRaw.mockImplementation(async (url: string, init: RequestInit) => {
      calls.push([String(init?.method || "GET"), url]);
      return route(url, init);
    });
    const ev: string[] = [];
    const h: RecognizerHandlers = {
      onInterim: (x) => void ev.push("interim:" + x),
      onFinal: (x) => void ev.push("final:" + x),
      onWords: (x) => void ev.push("words:" + x),
      onPartial: (x) => void ev.push("partial:" + x),
      onFlush: (x) => void ev.push("flush:" + x),
      onNotice: (x) => void ev.push("notice:" + x),
      onError: (code, detail) => void ev.push("error:" + (detail || code)),
      onEnd: () => void ev.push("end"),
    };
    /** One audio block: loud or quiet, `ms` after the last. */
    const block = (i: number, loud: boolean, ms = 100) => {
      now += ms;
      clips[i].opts.onLevel!(loud ? 0.3 : 0.001);
    };
    return { clips, calls, ev, h, block, chunks: () => calls.filter(([, u]) => u.endsWith("/chunk")).length };
  }

  /** A route that opens stream s1, s2… and answers chunks from `answers`. */
  function server(answers: Array<() => unknown> = [], ends: string[] = []) {
    let n = 0;
    let c = 0;
    let e = 0;
    return (url: string) => {
      if (url === "/api/voice/stream") return resp(200, { id: "s" + ++n, ready: true });
      if (url.endsWith("/chunk")) return (answers[c++] || (() => resp(200, { committed: "", partial: "" })))();
      if (url.endsWith("/end")) return resp(200, { committed: ends[e++] || "", partial: "" });
      if (url.startsWith("/api/voice/transcribe")) return resp(200, { text: "the clip's words" });
      return resp(204, {});
    };
  }

  it("opens a stream as it starts when the server can stream — chunks only from speech, never a clip upload", async () => {
    const t = setup(server());
    const r = serverRecognizer({ stream: () => true });
    r.start(t.h);
    await vi.waitFor(() => expect(t.calls).toEqual([["POST", "/api/voice/stream"]]));
    await vi.waitFor(() => expect(t.clips.length).toBe(1));
    t.clips[0].feed();
    t.block(0, false);
    expect(t.chunks()).toBe(0); // nothing sent before a word is heard
    expect(t.clips[0].take).toHaveBeenCalled(); // the quiet before it is dropped
    t.clips[0].feed();
    t.block(0, true);
    await vi.waitFor(() => expect(t.chunks()).toBe(1));
    const init = mocks.apiRaw.mock.calls[0][1];
    expect(JSON.parse(init.body)).toEqual({ lang: "en-US" });
    expect(init.headers["Content-Type"]).toBe("application/json");
    r.stop();
    // A stream open when stopped is abandoned.
    expect(t.calls).toContainEqual(["DELETE", "/api/voice/stream/s1"]);
  });

  it("without stt_stream it records clips as before", async () => {
    const t = setup(server());
    const r = serverRecognizer({ stream: () => false });
    r.start(t.h);
    await vi.waitFor(() => expect(t.clips.length).toBe(1));
    t.block(0, false);
    t.block(0, true);
    r.finish();
    await vi.waitFor(() => expect(t.ev).toContain("end"));
    expect(t.calls).toEqual([["POST", "/api/voice/transcribe?lang=en-US"]]);
    expect(t.clips[0].take).not.toHaveBeenCalled();
  });

  it("paces chunks: never two in flight, and only once enough new audio exists", async () => {
    let answer: (v: unknown) => void = () => {};
    const slow = () => new Promise((res) => (answer = res));
    const t = setup(server([slow, () => resp(200, { committed: "and add", partial: "a te" })]));
    const r = serverRecognizer({ stream: () => true });
    r.start(t.h);
    await vi.waitFor(() => expect(t.clips.length).toBe(1));
    t.block(0, false); // the noise floor
    t.block(0, true); // speech: open
    await vi.waitFor(() => expect(t.calls.length).toBe(1));
    await Promise.resolve();
    t.block(0, true); // under 300 ms new: take() has nothing
    expect(t.chunks()).toBe(0);
    t.clips[0].feed();
    t.block(0, true);
    expect(t.chunks()).toBe(1);
    const [, init] = mocks.apiRaw.mock.calls[1];
    expect(mocks.apiRaw.mock.calls[1][0]).toBe("/api/voice/stream/s1/chunk");
    expect(init.method).toBe("POST");
    expect(init.headers["Content-Type"]).toBe("audio/wav");
    expect((init.body as Blob).type).toBe("audio/wav");
    // More audio while the first is out: it waits.
    t.clips[0].feed();
    t.block(0, true);
    t.block(0, true);
    expect(t.chunks()).toBe(1);
    answer(resp(200, { committed: "fix the bug", partial: "and" }));
    // Back: the waiting audio goes at once.
    await vi.waitFor(() => expect(t.chunks()).toBe(2));
    await vi.waitFor(() =>
      expect(t.ev).toEqual(["words:fix the bug", "partial:and", "words:and add", "partial:a te"])
    );
    r.stop();
  });

  it("a pause ends the phrase: /end flushes its last words, and the next phrase opens a new stream", async () => {
    const t = setup(server([() => resp(200, { committed: "fix the", partial: "bug" })], ["bug"]));
    const r = serverRecognizer({ stream: () => true });
    r.start(t.h);
    await vi.waitFor(() => expect(t.clips.length).toBe(1));
    t.block(0, false);
    t.block(0, true);
    await vi.waitFor(() => expect(t.calls.length).toBe(1));
    await Promise.resolve();
    t.clips[0].feed();
    t.block(0, true);
    await vi.waitFor(() => expect(t.ev).toContain("words:fix the"));
    for (let i = 0; i < 12; i++) t.block(0, false); // 1.2 s of quiet
    await vi.waitFor(() => expect(t.ev).toContain("flush:bug"));
    expect(t.calls).toContainEqual(["POST", "/api/voice/stream/s1/end"]);
    expect(t.calls.filter(([, u]) => u.includes("transcribe"))).toEqual([]);
    // The next phrase is already recording, and streams on its own id.
    await vi.waitFor(() => expect(t.clips.length).toBe(2));
    t.block(1, false);
    t.block(1, true);
    await vi.waitFor(() => expect(t.calls).toContainEqual(["POST", "/api/voice/stream"]));
    expect(t.calls.filter(([, u]) => u === "/api/voice/stream").length).toBe(2);
    expect(t.ev).not.toContain("end"); // still listening
    r.stop();
    // Opened as it stopped, or before: abandoned either way.
    await vi.waitFor(() => expect(t.calls).toContainEqual(["DELETE", "/api/voice/stream/s2"]));
  });

  it("no 12 s cap while streaming", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      const t = setup(server());
      const r = serverRecognizer({ stream: () => true });
      r.start(t.h);
      await vi.waitFor(() => expect(t.clips.length).toBe(1));
      t.block(0, false);
      t.block(0, true);
      vi.advanceTimersByTime(13_000);
      expect(t.clips[0].stop).not.toHaveBeenCalled();
      r.stop();
    } finally {
      vi.useRealTimers();
    }
  });

  it("let go: the unsent tail goes as a last chunk, /end's words are flushed, then onEnd", async () => {
    const t = setup(server([() => resp(200, { committed: "", partial: "ship" })], ["ship it"]));
    const r = serverRecognizer({ stream: () => true });
    r.start(t.h);
    await vi.waitFor(() => expect(t.clips.length).toBe(1));
    t.block(0, false);
    t.block(0, true);
    await vi.waitFor(() => expect(t.calls.length).toBe(1));
    r.finish();
    await vi.waitFor(() => expect(t.ev).toContain("end"));
    expect(t.calls.map(([, u]) => u)).toEqual([
      "/api/voice/stream",
      "/api/voice/stream/s1/chunk",
      "/api/voice/stream/s1/end",
    ]);
    expect(t.ev.slice(-2)).toEqual(["flush:ship it", "end"]);
    expect(t.clips.length).toBe(1); // no next phrase
  });

  it("a 409 on the open: the phrase — and the rest — go as clips", async () => {
    const t = setup((url) =>
      url === "/api/voice/stream" ? resp(409, { error: "whisper does not stream" }) : server()(url)
    );
    const r = serverRecognizer({ stream: () => true });
    r.start(t.h);
    await vi.waitFor(() => expect(t.clips.length).toBe(1));
    t.block(0, false);
    t.block(0, true);
    await vi.waitFor(() => expect(t.calls.length).toBe(1));
    await Promise.resolve();
    t.clips[0].feed();
    t.block(0, true);
    expect(t.chunks()).toBe(0);
    for (let i = 0; i < 12; i++) t.block(0, false);
    await vi.waitFor(() => expect(t.ev).toContain("final:the clip's words"));
    expect(t.calls[1]).toEqual(["POST", "/api/voice/transcribe?lang=en-US"]);
    expect(t.ev.filter((e) => e.startsWith("notice:"))).toEqual([]); // a 409 is no news
    // The next phrase is a clip from the start: no second open.
    await vi.waitFor(() => expect(t.clips.length).toBe(2));
    t.block(1, false);
    t.block(1, true);
    expect(t.calls.filter(([, u]) => u === "/api/voice/stream").length).toBe(1);
    r.stop();
  });

  it("a failing chunk: the stream is dropped, its held words flushed, one caption, and clips from then on", async () => {
    for (const fail of [
      () => resp(404, { error: "no such voice stream (it ended or expired): open another" }),
      () => resp(502, { error: "inference failed" }),
      () => Promise.reject(new TypeError("Failed to fetch")),
    ]) {
      mocks.apiRaw.mockReset();
      mocks.startWavRecording.mockReset();
      const t = setup(server([() => resp(200, { committed: "fix the", partial: "" }), fail]));
      const r = serverRecognizer({ stream: () => true });
      r.start(t.h);
      await vi.waitFor(() => expect(t.clips.length).toBe(1));
      t.block(0, false);
      t.block(0, true);
      await vi.waitFor(() => expect(t.calls.length).toBe(1));
      await Promise.resolve();
      t.clips[0].feed();
      t.block(0, true);
      await vi.waitFor(() => expect(t.ev).toContain("words:fix the"));
      t.clips[0].feed();
      t.block(0, true);
      await vi.waitFor(() => expect(t.ev.some((e) => e.startsWith("notice:Streaming stopped"))).toBe(true));
      expect(t.calls).toContainEqual(["DELETE", "/api/voice/stream/s1"]);
      expect(t.ev).toContain("flush:");
      // Still listening; the rest of the phrase is a clip.
      t.clips[0].feed();
      t.block(0, true);
      expect(t.chunks()).toBe(2);
      for (let i = 0; i < 12; i++) t.block(0, false);
      await vi.waitFor(() => expect(t.ev).toContain("final:the clip's words"));
      await vi.waitFor(() => expect(t.clips.length).toBe(2));
      t.block(1, false);
      t.block(1, true);
      expect(t.calls.filter(([, u]) => u === "/api/voice/stream").length).toBe(1);
      expect(t.ev.filter((e) => e.startsWith("notice:")).length).toBe(1);
      expect(t.ev).not.toContain("end");
      expect(t.ev.filter((e) => e.startsWith("error:"))).toEqual([]);
      r.stop();
      vi.restoreAllMocks();
    }
  });

  it("paces by the server: the first chunk after 150 ms of speech, later ones after ~0.8 × the last pass (150–600 ms)", async () => {
    let answer: (v: unknown) => void = () => {};
    const slow = () => new Promise((res) => (answer = res));
    const t = setup(server([() => resp(200, { committed: "", partial: "fix", ms: 500 }), slow]));
    const r = serverRecognizer({ stream: () => true });
    r.start(t.h);
    await vi.waitFor(() => expect(t.clips.length).toBe(1));
    t.clips[0].feed();
    t.block(0, false); // the quiet before: trimmed (no lead-in left)
    t.block(0, true); // speech
    await vi.waitFor(() => expect(t.calls.length).toBe(1));
    await Promise.resolve();
    t.clips[0].feed();
    t.block(0, true);
    expect(t.chunks()).toBe(1);
    expect(t.clips[0].take.mock.calls.at(-1)).toEqual([FIRST_CHUNK_S]);
    expect(FIRST_CHUNK_S).toBe(0.15);
    await vi.waitFor(() => expect(r.passMs).toBe(500));
    t.clips[0].feed();
    t.block(0, true);
    expect(t.chunks()).toBe(2);
    expect(t.clips[0].take.mock.calls.at(-1)).toEqual([0.4]); // 500 ms × 0.8
    // A pass still running when the next chunk is due: the audio waits, and
    // goes together when it is back (one request in flight).
    t.clips[0].feed();
    t.block(0, true);
    t.block(0, true);
    expect(t.chunks()).toBe(2);
    answer(resp(200, { committed: "fix", partial: "the bug", ms: 90 }));
    await vi.waitFor(() => expect(t.chunks()).toBe(3));
    expect(t.clips[0].take.mock.calls.at(-1)).toEqual([0.15]); // a fast pass: the floor
    r.stop();
  });

  it.each([
    [null, 0.3],
    [0, 0.3],
    [100, 0.15],
    [187.5, 0.15],
    [500, 0.4],
    [750, 0.6],
    [2000, 0.6],
  ] as const)("chunkMinS(%j) → %j s", (ms, want) => {
    expect(chunkMinS(ms)).toBeCloseTo(want, 6);
  });

  it("with onStream, each chunk's answer arrives whole — once — and the pass time is kept", async () => {
    const t = setup(server([() => resp(200, { committed: ["fix", "the"], partial: "bug", ms: 412 })], ["bug"]));
    const got: Array<[string, string]> = [];
    const r = serverRecognizer({ stream: () => true });
    r.start({ ...t.h, onStream: (w, p) => void got.push([w, p]) });
    await vi.waitFor(() => expect(t.clips.length).toBe(1));
    t.block(0, false);
    t.block(0, true);
    await vi.waitFor(() => expect(t.calls.length).toBe(1));
    await Promise.resolve();
    t.clips[0].feed();
    t.block(0, true);
    await vi.waitFor(() => expect(got).toEqual([["fix the", "bug"]]));
    expect(t.ev.filter((e) => e.startsWith("words:") || e.startsWith("partial:"))).toEqual([]);
    expect(r.passMs).toBe(412);
    expect(passLine("local", r.passMs)).toBe("whisper ~0.4 s/pass");
    expect(passLine("", 1234)).toBe("whisper ~1.2 s/pass");
    expect(passLine("system", 300)).toBe("system ~0.3 s/pass");
    expect(passLine("local", null)).toBe("");
    // The phrase's end still clears the tail and flushes.
    r.finish();
    await vi.waitFor(() => expect(t.ev).toContain("end"));
    expect(t.ev.slice(-3)).toEqual(["partial:", "flush:bug", "end"]);
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
    // Listening is red and still: no pulse anywhere any more.
    expect(src("components/TopBarMic.css")).not.toContain("mic-pulse");
    expect(cssRule(src("components/TopBarMic.css"), "#topbar #mic-btn.listening")).toContain("var(--red)");
  });

  it("types into the session's agent terminal, never a whole-prompt /send; submit is /send {enter: true}", () => {
    const lib = code("lib/voiceDesktop.ts");
    expect(lib).toContain('peekTerm(title, "agent")?.send(data)');
    expect(lib).toMatch(/instApi<[^>]*>\(title, "\/send", \{ json: \{ enter: true, say \} \}\)/);
    // The old one-shot prompt send is gone from the mic.
    expect(MIC).not.toContain("dialog_safe");
    expect(MIC).not.toContain("sendPrompt");
    expect(MIC).toMatch(/submitted: \(title, wantSay\) => \{\s*tracker\.sent\(title, wantSay\);/);
    expect(MIC).toContain("replies: () => prefsRef.current.replies");
  });

  it("the dialog guard reads the rows the app already polls", () => {
    expect(MIC).toContain('queryClient.getQueryData<Instance[]>(["instances"])');
    expect(MIC).toContain("effectiveActivity(");
    expect(MIC).toContain("activity: liveActivity");
  });

  it("asks the router with a dictation's first phrase, and selects through the one selection authority", () => {
    // Hands-free listening asks about the session it read out.
    expect(MIC).toContain('const session = opts.title || useUi.getState().focused || "";');
    expect(MIC).toContain("routePhrase(text, session)");
    expect(MIC).toContain("selectSession(title, { noKeyboard: true })");
    expect(code("lib/voiceDesktop.ts")).toContain('const json: Record<string, unknown> = { text, session };');
    expect(code("lib/voiceDesktop.ts")).toContain('api<RouteReply>("/api/voice/route", { json })');
  });

  it("push to talk ends in finish() (\u201cover\u201d), Escape in cancel() (\u201cdisregard\u201d + stop)", () => {
    const fin = blockFrom(MIC, "const finish = (", "// Escape on the mic");
    expect(fin).toContain("void d.finish()");
    expect(fin).not.toContain("instApi");
    expect(MIC).toContain("void live.current?.cancel()");
  });

  it("opens its options on right-click and Shift+click: controls only, the rest in Settings → Voice", () => {
    expect(MIC).toContain("onContextMenu=");
    expect(MIC).toContain("e.shiftKey");
    expect(MIC).toContain("Read replies aloud");
    expect(MIC).toContain("<span>Click</span>");
    expect(MIC).toContain("<span>Hold</span>");
    // The explainers and the diagnostics moved out of the popover.
    expect(MIC).not.toContain("openSettings(");
    expect(src("components/settings/screens/Voice.tsx")).toContain("{micDiagnostics()}");
  });

  it("streams when GET /api/voice says the server can: each answer typed as a guess, the unsure tail shown muted", () => {
    expect(MIC).toContain("serverRecognizer({ stream: () => !!cfgNow.server_ready.stt_stream })");
    expect(MIC).toContain("void d.stream(w, p)");
    expect(MIC).toContain("void d.flush(t, final)");
    expect(MIC).toContain("d.unsure(t)");
    // The words are in the terminal: no transcript on the page (voicePolish.test).
    expect(MIC).not.toContain("mic-unsure");
    const lib = code("lib/voiceDesktop.ts");
    expect(lib).toContain('call("/api/voice/stream", {');
    expect(lib).toContain('streamPath(c.sid!, "/chunk")');
    expect(lib).toContain('streamPath(sid, "/end")');
  });

  it("asks for the engine before starting: with none (the desktop app, server not ready) it says so and starts nothing", () => {
    const start = blockFrom(MIC, "const start = (", "const finish = (");
    const none = start.indexOf('if (engine === "none")');
    expect(none).toBeGreaterThan(-1);
    expect(start.indexOf("say(noRecognizerLine())")).toBeGreaterThan(none);
    expect(start.indexOf("r.start(")).toBeGreaterThan(none);
    expect(start.slice(none, start.indexOf("r.start("))).toContain("return null;");
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
    expect(MIC_SRC).toContain("if (menuOpen) {\n              setMenuOpen(false);");
    expect(MIC_SRC).toContain('document.addEventListener("pointerdown", onDown, true)');
    expect(MIC_SRC).not.toContain('document.addEventListener("mousedown", onDown)');
  });
});

// --- the wider command set: stop at any point, numbered sessions, answers -- //

describe("control words at any point", () => {
  afterEach(() => {
    mocks.peekTerm.mockReset();
    mocks.instApi.mockReset();
  });

  function terms() {
    const sent: Record<string, string[]> = {};
    mocks.peekTerm.mockImplementation((title: string) => ({
      send: (data: string) => ((sent[title] ||= []).push(data), true),
    }));
    return sent;
  }

  function hooks(over: Partial<LiveHooks> = {}) {
    const captions: string[] = [];
    const calls = { halt: 0, routed: [] as string[], ends: [] as DictEnd[] };
    const h: LiveHooks = {
      caption: (t) => void captions.push(t),
      halt: () => void calls.halt++,
      route: async (t) => {
        calls.routed.push(t);
        return false;
      },
      activity: () => "idle",
      replies: () => false,
      submitted: () => {},
      done: (how) => void calls.ends.push(how),
      ...over,
    };
    return { h, captions, calls, last: () => captions[captions.length - 1] };
  }

  it.each([
    ["fix the bug that is", "fix the bug", "that is"],
    ["fix it, that is all", "fix it,", "that is all"],
    ["fix it stop", "fix it", "stop"],
    ["fix it stop recording", "fix it", "stop recording"],
    ["turn mic", "", "turn mic"],
    ["and then that", "and then", "that"],
    ["deploy it over it", "deploy it", "over it"],
    ["ship it over and out that's", "ship it", "over and out that's"],
  ])("holdBack(%j) holds a control phrase that may still be said: types %j, holds %j", (text, type, held) => {
    expect(holdBack(text)).toEqual({ type, held });
    expect(controlMaxWords()).toBe(7); // "over and out, turn off the microphone"
  });

  it.each([
    ["stop recording", ""],
    ["Fix the bug, stop recording.", "Fix the bug"],
    ["that's all", ""],
    ["That is all.", ""],
    ["ok mic off", "ok"],
    ["stop the recording", null],
    ["stop", null],
    ["record it all", null],
  ] as const)("stopTail(%j) → %j", (text, want) => {
    expect(stopTail(text)).toBe(want);
  });

  it("a stop phrase at the end of a committed group stops at once, never typed; the words before it stay", async () => {
    const sent = terms();
    const k = hooks();
    const d = new LiveDictation(sessionTarget("t1", "Login fix"), k.h);
    await d.words("fix the login bug stop recording");
    expect(sent.t1).toEqual(["fix the login bug "]);
    expect(sent.t1.join("")).not.toMatch(/stop|recording/);
    expect(k.calls.halt).toBe(1);
    expect(k.last()).toBe(LEFT_LINE);
    expect(k.calls.ends).toEqual(["stopped"]);
    expect(d.active).toBe(false);
    expect(mocks.instApi).not.toHaveBeenCalled();
  });

  it("…split across committed groups too: the held words wait for the rest of the phrase", async () => {
    const sent = terms();
    const k = hooks();
    const d = new LiveDictation(sessionTarget("t1", "Login fix"), k.h);
    await d.words("ship it that is");
    expect(sent.t1).toEqual(["ship it"]);
    await d.words("all");
    expect(sent.t1).toEqual(["ship it", " "]);
    expect(k.calls.halt).toBe(1);
  });

  it("said alone, it stops without asking the router; a clip's phrase works the same", async () => {
    terms();
    const k = hooks();
    const d = new LiveDictation(sessionTarget("t1", "Login fix"), k.h);
    await d.words("stop listening");
    expect(k.calls.routed).toEqual([]);
    expect(k.last()).toBe(STOPPED_LINE);
    const sent = terms();
    const k2 = hooks();
    const d2 = new LiveDictation(sessionTarget("t2", "Billing"), k2.h);
    await d2.heard("run the tests");
    await d2.heard("Mic off.");
    expect(sent.t2).toEqual(["run the tests "]);
    expect(k2.calls.halt).toBe(1);
  });

  it("“stop” followed by more words is a prompt", async () => {
    const sent = terms();
    const d = new LiveDictation(sessionTarget("t1", "Login fix"), hooks().h);
    await d.words("stop");
    await d.words("the server and");
    expect(sent.t1).toEqual(["stop the server and"]);
  });

  it("with a question pending, a lone answer goes to the router at once — before the phrase ends", async () => {
    terms();
    const k = hooks({
      pending: () => true,
      route: async (t) => {
        k.calls.routed.push(t);
        return true;
      },
    });
    const d = new LiveDictation(sessionTarget("t1", "Login fix"), k.h);
    await d.words("Yes.");
    expect(k.calls.routed).toEqual(["Yes."]);
    expect(k.calls.ends).toEqual(["acted"]);
    // Nothing pending: "yes" may open a prompt — it waits for more words.
    const k2 = hooks();
    const d2 = new LiveDictation(sessionTarget("t1", "Login fix"), k2.h);
    await d2.words("yes");
    expect(k2.calls.routed).toEqual([]);
    expect(d2.active).toBe(true);
  });

  it("only the WHOLE dictation is an answer: “the answer is no” is a prompt", () => {
    for (const t of ["yes", "No.", "always", "yes always", "don't ask again", "two", "option 3", "the first one", "uh yes please"])
      expect(isAnswerPhrase(t)).toBe(true);
    for (const t of ["the answer is no", "yes and fix the tests", "no idea", "always run the tests"])
      expect(isAnswerPhrase(t)).toBe(false);
  });

  it.each([
    ["focus three", true],
    ["select the login fix", true],
    ["next session", true],
    ["session three yes", true],
    ["row 2", true],
    ["yes", true],
    ["always allow it", true],
    ["what is it asking", true],
    ["stop recording", true],
    ["that's all", true],
    ["yes and also fix the tests", false],
    ["no idea why", false],
    ["go ahead and push", false],
  ] as const)("mayBeCommand(%j) → %s", (text, want) => {
    expect(mayBeCommand(text)).toBe(want);
  });
});

describe("the router's new answers", () => {
  afterEach(() => {
    mocks.api.mockReset();
    mocks.instApi.mockReset();
    mocks.peekTerm.mockReset();
  });

  const RAIL = ["t1", "\u0000win:chat", "t2", "t3"];

  function deps(over: Partial<CommandDeps> = {}) {
    const out = { selected: [] as string[], said: [] as string[], captions: [] as string[], submitted: [] as Array<[string, boolean]> };
    const d: CommandDeps = {
      railOrder: () => RAIL,
      focused: () => "t1",
      select: (t) => void out.selected.push(t),
      name: (t) => ({ t1: "Login fix", t2: "Billing", t3: "Docs" })[t] || t,
      caption: (l) => void out.captions.push(l),
      announce: (l) => void out.said.push(l),
      route: vi.fn(async () => ({ kind: "prompt" }) as RouteReply),
      approveDialogs: async () => true,
      activity: () => "idle",
      replies: () => true,
      submitted: (t, say) => void out.submitted.push([t, say]),
      ...over,
    };
    return { d, out };
  }

  it("routePhrase sends back what the router asked, for 30 s, and drops it on any answer without one", async () => {
    let t = 1_000_000;
    const held = new PendingChoice(() => t);
    const which = [
      { title: "sitecheck-bot7", name: "sitecheck-bot7" },
      { title: "sitecheck-bot7-copy-2", name: "sitecheck-bot7-copy-2" },
    ];
    mocks.api.mockClear();
    mocks.api.mockResolvedValueOnce({
      kind: "ambiguous",
      say: "Which one: sitecheck-bot7 or sitecheck-bot7-copy-2?",
      candidates: which,
      pending: { kind: "pick", candidates: which },
    });
    expect((await routePhrase("switch to the sitecheck session", "t1", held)).kind).toBe("ambiguous");
    expect(mocks.api.mock.calls[0][1]).toEqual({ json: { text: "switch to the sitecheck session", session: "t1" } });
    // The next phrase carries the choice; the switch answer drops it.
    mocks.api.mockResolvedValueOnce({ kind: "switch", title: "sitecheck-bot7-copy-2", say: "sitecheck-bot7-copy-2." });
    await routePhrase("copy two", "t1", held);
    expect(mocks.api.mock.calls[1][1]).toEqual({
      json: { text: "copy two", session: "t1", pending: { kind: "pick", candidates: which } },
    });
    expect(held.take()).toBeNull();
    // A new session's questions ride the same way; past PENDING_MS, gone.
    mocks.api.mockResolvedValueOnce({ kind: "ask", say: "In which project?", pending: { kind: "new", await: "repo" } });
    await routePhrase("start a new session", "t1", held);
    expect(held.take()).toEqual({ kind: "new", await: "repo" });
    t += PENDING_MS + 1;
    mocks.api.mockResolvedValueOnce({ kind: "prompt" });
    await routePhrase("parser", "t1", held);
    expect(mocks.api.mock.calls[3][1]).toEqual({ json: { text: "parser", session: "t1" } });
    // A failed call keeps it (it still expires on its own).
    held.keep({ kind: "ask", pending: { kind: "new", await: "task" } });
    mocks.api.mockRejectedValueOnce(new Error("offline"));
    expect(await routePhrase("fix it", "t1", held)).toEqual({ kind: "prompt" });
    expect(held.take()).toEqual({ kind: "new", await: "task" });
  });

  it("an ask or a Which one is said like a confirmation; read is the router's words", () => {
    const routed = blockFrom(code("components/TopBarMic.tsx"), "const routed = async (", "// --- hands-free");
    const ask = routed.slice(routed.indexOf('case "ask":'), routed.indexOf('case "cancel":'));
    expect(ask).toContain('case "ambiguous":');
    expect(ask).toContain("confirmer.current?.say({ line: r.say })");
    expect(ask).toContain("else announce(r.say");
    // read (and no-session, list …): the router's words, nothing typed.
    expect(routed.slice(routed.indexOf("default:"))).toContain('announce(r.say || "");');
  });

  it("routePhrase passes the new kinds and treats an unknown one as a prompt", async () => {
    mocks.api.mockResolvedValueOnce({ kind: "switch_index", index: 2 });
    expect(await routePhrase("focus two", "t1")).toEqual({ kind: "switch_index", index: 2 });
    mocks.api.mockResolvedValueOnce({ kind: "teleport" });
    expect(await routePhrase("teleport", "t1")).toEqual({ kind: "prompt" });
  });

  it("switch_index maps through railOrder (windows count, as the badges do); out of range says so", async () => {
    const { d, out } = deps();
    expect(await runVoiceCommand({ kind: "switch_index", index: 3 }, "focus three", d)).toBe(true);
    expect(out.selected).toEqual(["t2"]);
    expect(out.said).toEqual(["Billing."]); // the name alone
    await runVoiceCommand({ kind: "switch_index", index: 7 }, "focus seven", d);
    expect(out.said[1]).toBe("There is no session 7");
    await runVoiceCommand({ kind: "switch_index", index: 2 }, "focus two", d);
    expect(out.said[2]).toBe("Row 2 is a window, not a session");
    expect(out.selected).toEqual(["t2"]);
    expect(railRow(RAIL, 0)).toBeNull();
  });

  it("switch_rel moves from the focused session's row, skipping windows and wrapping", async () => {
    const { d, out } = deps();
    await runVoiceCommand({ kind: "switch_rel", delta: 1 }, "next session", d);
    await runVoiceCommand({ kind: "switch_rel", delta: -1 }, "previous session", d);
    expect(out.selected).toEqual(["t2", "t3"]);
    expect(railStep(RAIL, "t3", 1)).toBe("t1");
    expect(railStep(RAIL, "gone", 1)).toBe("t1");
    expect(railStep(RAIL, "gone", -1)).toBe("t3");
    expect(railStep(["t1"], "t1", 1)).toBeNull();
  });

  it("an answer with keys posts /answer as the user — only while approving by voice is on", async () => {
    const reply: RouteReply = { kind: "answer", choice: "yes", title: "t2", dialog_id: "d1", keys: ["1"], say: "Billing: allowed." };
    mocks.instApi.mockResolvedValue({ ok: true });
    const on = deps();
    await runVoiceCommand(reply, "billing yes", on.d);
    expect(mocks.instApi).toHaveBeenCalledWith("t2", "/answer", { json: { keys: ["1"], dialog_id: "d1", by: "user" } });
    expect(on.out.said).toEqual(["Billing: allowed."]);
    expect(on.out.selected).toEqual([]); // a targeted answer never moves focus
    mocks.instApi.mockClear();
    const off = deps({ approveDialogs: async () => false });
    await runVoiceCommand(reply, "billing yes", off.d);
    expect(mocks.instApi).not.toHaveBeenCalled();
    expect(off.out.said).toEqual([APPROVE_OFF_LINE]);
    expect(APPROVE_OFF_LINE).toBe("Approving by voice is off: Settings → Voice");
  });

  it("“always” only when the router says so: the keys sent are the router's, never widened", async () => {
    mocks.instApi.mockResolvedValue({ ok: true });
    const { d } = deps();
    await runVoiceCommand({ kind: "answer", choice: "yes", dialog_id: "d1", keys: ["1"] }, "always", d);
    expect(mocks.instApi).toHaveBeenLastCalledWith("t1", "/answer", { json: { keys: ["1"], dialog_id: "d1", by: "user" } });
    await runVoiceCommand({ kind: "answer", choice: "always", dialog_id: "d1", keys: ["2"] }, "always", d);
    expect(mocks.instApi).toHaveBeenLastCalledWith("t1", "/answer", { json: { keys: ["2"], dialog_id: "d1", by: "user" } });
    // No keys from the router: nothing pressed, whatever was said.
    mocks.instApi.mockReset();
    mocks.instApi.mockResolvedValue({ has_question: false });
    const n = deps();
    await runVoiceCommand({ kind: "answer", choice: "always", no_dialog: true, say: "Login fix has no dialog up." }, "always", n.d);
    expect(mocks.instApi.mock.calls.map((c) => c[1])).toEqual(["/voice"]);
    expect(n.out.said).toEqual(["Login fix has no dialog up."]);
  });

  it("a 409 dialog_changed says so instead of pressing into the next prompt", async () => {
    mocks.instApi.mockRejectedValueOnce(new mocks.ApiError(409, "the prompt changed", { dialog_changed: true }));
    const { d, out } = deps();
    await runVoiceCommand({ kind: "answer", choice: "no", dialog_id: "d1", keys: ["3"] }, "no", d);
    expect(out.said).toEqual(["The prompt on Login fix changed — say it again."]);
  });

  it("an answer to a QUESTION goes as a prompt, typed and submitted with say", async () => {
    const sent: string[] = [];
    mocks.peekTerm.mockImplementation(() => ({ send: (x: string) => sent.push(x) > 0 }));
    mocks.instApi.mockImplementation(async (_t: string, suffix: string) =>
      suffix === "/voice" ? { say: "Which branch, main or dev?", has_question: true } : { ok: true }
    );
    const { d, out } = deps();
    await runVoiceCommand({ kind: "answer", choice: "number", number: 2, no_dialog: true }, "two", d);
    expect(sent).toEqual(["2 "]);
    expect(mocks.instApi).toHaveBeenLastCalledWith("t1", "/send", { json: { enter: true, say: true } });
    expect(out.submitted).toEqual([["t1", true]]);
    expect(mocks.instApi.mock.calls.some((c) => c[1] === "/answer")).toBe(false);
  });

  it("a select dialog's free-text answer from the router rides with its keys (`text`)", async () => {
    mocks.instApi.mockResolvedValue({ ok: true });
    const { d } = deps({ approveDialogs: async () => true });
    await runVoiceCommand(
      { kind: "answer", choice: "number", number: 3, title: "t1", dialog_id: "q1", keys: ["3"], text: "SAML via Okta" },
      "type something SAML via Okta",
      d
    );
    expect(mocks.instApi).toHaveBeenLastCalledWith("t1", "/answer", {
      json: { keys: ["3"], dialog_id: "q1", by: "user", text: "SAML via Okta" },
    });
  });

  it("a working session's old question is not answered", async () => {
    mocks.instApi.mockResolvedValue({ has_question: true });
    const { d, out } = deps({ activity: () => "working" });
    await runVoiceCommand({ kind: "answer", choice: "yes" }, "yes", d);
    expect(out.said).toEqual(["Login fix isn't asking anything."]);
  });

  it("an index-only answer maps the row to its title and asks the router again with it", async () => {
    mocks.instApi.mockResolvedValue({ ok: true });
    const route = vi.fn(async () => ({ kind: "answer", choice: "yes", title: "t3", dialog_id: "d9", keys: ["1"] }) as RouteReply);
    const { d, out } = deps({ route });
    await runVoiceCommand({ kind: "answer", choice: "yes", index: 4 }, "session four yes", d);
    expect(route).toHaveBeenCalledWith("session four yes", "t3");
    expect(mocks.instApi).toHaveBeenCalledWith("t3", "/answer", { json: { keys: ["1"], dialog_id: "d9", by: "user" } });
    expect(out.selected).toEqual([]);
    // Past the rail: said, never routed.
    route.mockClear();
    await runVoiceCommand({ kind: "answer", choice: "yes", index: 9 }, "session nine yes", d);
    expect(route).not.toHaveBeenCalled();
    expect(out.said[out.said.length - 1]).toBe("There is no session 9");
  });

  it("question: the router's sentence is spoken", async () => {
    const { d, out } = deps();
    expect(await runVoiceCommand({ kind: "question", say: "Billing: which port?" }, "what is it asking", d)).toBe(true);
    expect(out.said).toEqual(["Billing: which port?"]);
    expect(await runVoiceCommand({ kind: "switch", title: "t2" }, "switch to billing", d)).toBe(false);
  });
});

describe("hands-free answering (Read replies aloud)", () => {
  const PERMISSION = {
    id: "d1",
    parsed: true,
    question: "Allow **git push**?",
    command: null,
    options: [
      { key: "1", label: "Yes", kind: "yes" },
      { key: "2", label: "Yes, and don't ask again", kind: "always" },
      { key: "3", label: "No", kind: "no" },
    ],
  };

  function deps(ends: DictEnd[], over: Partial<HandsFreeDeps> = {}) {
    const out = { spoken: [] as string[], captions: [] as string[], listens: [] as Array<[string, number]> };
    const d: HandsFreeDeps = {
      replies: () => true,
      focused: () => "t1",
      name: () => "Login fix",
      busy: () => false,
      fetchDialog: async () => PERMISSION,
      approveDialogs: async () => true,
      speak: async (l) => void out.spoken.push(l),
      caption: (l) => void out.captions.push(l),
      listen: async (title, _hint, ms) => {
        out.listens.push([title, ms]);
        return ends.shift() || "missed";
      },
      ...over,
    };
    return { d, out };
  }

  it("ports the phone's wording: name first, the choices, the one retry line", () => {
    expect(dialogSpeech("Login fix", PERMISSION)).toBe("Login fix: Allow git push? Say yes, no, or always.");
    expect(dialogRetryLine(PERMISSION)).toBe("I didn't catch that: say yes, no, or always.");
    const menu = { options: [{ key: "1", label: "Use `main`", kind: "other" }, { key: "2", label: "Use dev", kind: "other" }] };
    expect(dialogChoices(menu)).toBe("one: Use main. two: Use dev. Say one or two.");
    expect(dialogRetryLine(menu)).toBe("I didn't catch that: say one or two.");
  });

  it("a dialog is read aloud and the mic listens 12 s; a miss is retried ONCE, then it goes quiet", async () => {
    const { d, out } = deps(["missed", "missed"]);
    await new HandsFree(d).dialog("t1");
    expect(out.spoken).toEqual([
      "Login fix: Allow git push? Say yes, no, or always.",
      "I didn't catch that: say yes, no, or always.",
    ]);
    expect(out.listens).toEqual([
      ["t1", ANSWER_LISTEN_MS],
      ["t1", ANSWER_LISTEN_MS],
    ]);
    expect(ANSWER_LISTEN_MS).toBe(12_000);
    expect(out.captions).toEqual(["Login fix is waiting on you"]);
  });

  it("an answer ends it; the same dialog is not read twice", async () => {
    const { d, out } = deps(["acted"]);
    const hf = new HandsFree(d);
    await hf.dialog("t1");
    await hf.dialog("t1");
    expect(out.listens).toHaveLength(1);
    expect(out.spoken).toHaveLength(1);
  });

  it("off by default (replies off), never for an unfocused session, never while the mic is busy", async () => {
    for (const over of [{ replies: () => false }, { focused: () => "t2" }, { busy: () => true }]) {
      const { d, out } = deps([], over);
      await new HandsFree(d).dialog("t1");
      expect(out.spoken).toEqual([]);
      expect(out.listens).toEqual([]);
    }
  });

  it("approving by voice off: read out, then “answer it on the screen” — the mic never opens", async () => {
    const { d, out } = deps([], { approveDialogs: async () => false });
    await new HandsFree(d).dialog("t1");
    expect(out.spoken[1]).toBe("Answer it on the screen.");
    expect(out.listens).toEqual([]);
  });

  it("a question in a reply: the mic listens; a miss asks once more, then quiet", async () => {
    const { d, out } = deps(["missed", "missed"]);
    await new HandsFree(d).question("t1", "Which branch?");
    expect(out.listens).toHaveLength(2);
    expect(out.spoken).toEqual(["Still there? Login fix: Which branch?"]);
    expect(out.captions).toEqual(["Login fix is waiting on you"]);
  });

  it("Diagnostics name what waits on the focused session", () => {
    expect(pendingDiagnostics("Login fix", PERMISSION, true, "")).toBe("Pending: Login fix shows a dialog — Allow git push?");
    expect(pendingDiagnostics("Login fix", null, false, "Which branch?")).toBe("Pending: Login fix asked — Which branch?");
    expect(pendingDiagnostics("Login fix", null, false, "")).toBe("Pending: nothing on Login fix");
  });

  it("the mic wires it: a clarify reading reads the dialog, a reply's question opens the mic, and the hint names it", () => {
    const MIC = code("components/TopBarMic.tsx");
    // With the ask pop-up on, the pop-up reads it out instead (askPopup.test).
    expect(MIC).toContain('else if (env.new === "clarify" && !askPopupOn()) void hands.current?.dialog(env.session)');
    expect(MIC).toContain("if (listen) void said.then(() => hands.current?.question(title, ask))");
    expect(MIC).toContain('pending: (title) => liveActivity(title) === "clarify" || questions.has(title)');
    expect(MIC).toContain("runVoiceCommand(r, text, {");
    expect(MIC).toContain("railOrder: () => useUi.getState().railOrder");
    expect(src("components/settings/screens/Voice.tsx")).toContain("With Read replies aloud on");
  });
});

describe("the push-to-talk hotkey", () => {
  it("Hold to talk: down starts, up ends as “over” — a blur's up only ends a press the key began", () => {
    expect(talkKeyAction("down", true, false, false)).toBe("start");
    expect(talkKeyAction("down", true, true, true)).toBe(null); // a repeat never stops it
    expect(talkKeyAction("up", true, true, true)).toBe("finish");
    // The window blurred while the button (not the key) held the mic.
    expect(talkKeyAction("up", true, true, false)).toBe(null);
    expect(talkKeyAction("up", true, false, true)).toBe(null);
  });

  it("Click to talk: each down starts or ends as “over”; up (and a blur's up) is nothing", () => {
    expect(talkKeyAction("down", false, false, false)).toBe("start");
    expect(talkKeyAction("down", false, true, false)).toBe("finish");
    expect(talkKeyAction("up", false, true, false)).toBe(null);
    expect(talkKeyAction("up", false, true, true)).toBe(null);
  });

  it("listens on window (any focus, a terminal's too) and starts inside the key's gesture", () => {
    expect(TALK_EVENT).toBe("mindflock:voice-talk");
    const MIC = code("components/TopBarMic.tsx");
    expect(MIC).toContain("window.addEventListener(TALK_EVENT, onTalk)");
    expect(MIC).toContain("window.removeEventListener(TALK_EVENT, onTalk)");
    const on = blockFrom(MIC, "const onTalk = (", "window.addEventListener(TALK_EVENT");
    expect(on).toContain("talkKeyAction(phase, k.hold, !!live.current, keyHeld.current)");
    // The same start() as a click: it makes/resumes the AudioContext synchronously.
    expect(on).toContain("k.start(textField(document.activeElement))");
    expect(on).toContain("k.finish()");
    expect(blockFrom(MIC, "const start = (", "const finish = (")).toContain("voiceAudioCtx()");
    expect(src("components/TopBarMic.tsx")).toContain('<span className="mic-pop-key"> {talkHotkey()}</span>');
    expect(src("components/settings/screens/Voice.tsx")).toContain("Hotkey:");
  });
});


describe("voice prefs are shared between the popover and Settings → Voice", () => {
  it("saveVoicePrefs announces the new prefs on window and onVoicePrefs hears them", async () => {
    const mod = await import("../lib/voiceDesktop");
    const g = globalThis as Record<string, unknown>;
    const hadWin = "window" in g;
    const prev = g.window;
    g.window = new EventTarget();
    try {
      const seen: unknown[] = [];
      const off = mod.onVoicePrefs((p) => seen.push(p));
      const kv = { store: {} as Record<string, string>, getItem(k: string) { return this.store[k] ?? null; }, setItem(k: string, v: string) { this.store[k] = v; } };
      mod.saveVoicePrefs({ ptt: "hold", replies: "all" }, kv);
      expect(seen).toEqual([{ ptt: "hold", replies: "all", wake: false, confirm: false, sounds: true }]);
      expect(JSON.parse(kv.store[mod.VOICE_PREFS_KEY])).toEqual({ ptt: "hold", replies: "all", wake: false, nods: false, sounds: true });
      off();
      mod.saveVoicePrefs({ ptt: "toggle", replies: "off" }, kv);
      expect(seen).toHaveLength(1);
    } finally {
      if (hadWin) g.window = prev;
      else delete g.window;
    }
  });
});
