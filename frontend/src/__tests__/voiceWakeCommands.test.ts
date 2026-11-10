/** "MindFlock, open settings" said in the MIDDLE of a message (owner report
 * 2026-11-10: "When I say 'MindFlock, open settings' or 'intake' it doesn't
 * do it, and there is also no voice that says the action it is taking"):
 *  - a phrase that starts with the wake word — as the recogniser writes it:
 *    "My flock, …", "Mine flock, …" — is the router's wherever it falls in a
 *    dictation: never typed, the words already typed stay, listening goes on
 *    (the message goes on: "over" sends it whole);
 *  - its streamed guess waits whole (nothing typed only to be taken back);
 *  - not a command after all (the router says prompt): typed as words;
 *  - opening something says so ("Opening Settings.") when spoken replies or
 *    spoken confirmations are on — else the chime, as before. */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  Confirmer,
  LiveDictation,
  announcesActions,
  type DictTarget,
  type LiveHooks,
  type VoicePrefs,
} from "../lib/voiceDesktop";
import { isWakePhrase, mayBeWake, resetVoiceWords, setVoiceWords, wakeAt, wakeRest } from "../lib/voiceWords";
import { AssistantSession, Conversation, ViewStore, openingLine, type AssistantEvent, type UiAction } from "../lib/voiceAssistant";
import { code } from "./sourceText";

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
  return { api: vi.fn(), apiRaw: vi.fn(), instApi: vi.fn(), ApiError, peekTerm: vi.fn() };
});
vi.mock("../api/client", () => ({ api: mocks.api, apiRaw: mocks.apiRaw, instApi: mocks.instApi, ApiError: mocks.ApiError }));
vi.mock("../lib/terminals", () => ({ peekTerm: mocks.peekTerm }));

/** The server's words as GET /api/voice serves them: the wake word's heard
 * forms (core.voice_words HEARD["mindflock"]) among them. */
const SERVER_WORDS = {
  send: ["over"],
  end: ["over and out", "over, out"],
  clear: ["disregard"],
  undo: ["scratch that"],
  stop: ["stop", "stop listening", "mic off", "pause listening", "that's all", "done talking"],
  hush: ["stop talking", "be quiet", "quiet", "hush", "shut up", "enough"],
  wake: "mindflock",
  heard: {
    send: [],
    end: [],
    clear: [],
    undo: [],
    stop: [],
    hush: [],
    wake: ["mind flock", "mine flock", "mind lock", "mindlock", "my flock", "mind block", "min flock", "me flock"],
  },
};

beforeEach(() => {
  setVoiceWords(SERVER_WORDS);
  mocks.instApi.mockResolvedValue({ ok: true });
});
afterEach(() => {
  resetVoiceWords();
  mocks.instApi.mockReset();
});

function session(title = "t1", name = "Login fix"): DictTarget & { keys: string[] } {
  const keys: string[] = [];
  return { kind: "session", title, name, keys, type: (t) => keys.push(t) > 0, erase: (t) => keys.push("⌫" + t) > 0 };
}

/** The page's hooks: the router acts on what starts with the wake word (as
 * TopBarMic's route does through POST /api/voice/route → the assistant),
 * else says prompt. The assistant's "Opening Settings." is asked for. */
function hooks(over: Partial<LiveHooks> = {}) {
  const routed: string[] = [];
  const confirmations: string[] = [];
  const ends: string[] = [];
  const h: LiveHooks = {
    caption: () => {},
    halt: () => {},
    route: async (t) => {
      routed.push(t);
      const rest = wakeRest(t);
      if (!rest) return false;
      if (/^open settings\.?$/i.test(rest)) confirmations.push("Opening Settings.");
      return true;
    },
    activity: () => "idle",
    replies: () => false,
    submitted: () => {},
    done: (how) => void ends.push(how),
    ...over,
  };
  return { h, routed, confirmations, ends };
}

describe("a wake-word phrase in the middle of a message is a command", () => {
  it("text typed, then “My flock, open settings.”: routed, not typed, the confirmation asked for", async () => {
    const t = session();
    const k = hooks();
    const d = new LiveDictation(t, k.h, { continuous: true });
    await d.heard("fix the login bug and");
    expect(t.keys).toEqual(["fix the login bug and "]);
    await d.heard("My flock, open settings.");
    // The router had it — not one key of it went in, nothing was taken back.
    expect(k.routed).toEqual(["fix the login bug and", "My flock, open settings."]);
    expect(t.keys).toEqual(["fix the login bug and "]);
    expect(k.confirmations).toEqual(["Opening Settings."]);
    // Listening goes on, the message with it: the next words follow the old
    // ones (not past the router: they are not a first phrase) and "over"
    // sends the lot.
    expect(d.active).toBe(true);
    expect(d.dict.typed).toBe("fix the login bug and ");
    await d.heard("add a test over");
    expect(k.routed).toHaveLength(2);
    expect(t.keys).toEqual(["fix the login bug and ", "add a test "]);
    expect(mocks.instApi).toHaveBeenCalledTimes(1);
    expect(mocks.instApi).toHaveBeenLastCalledWith("t1", "/send", { json: { enter: true, say: false } });
  });

  it("every heard variant counts: “Mine flock, open intake.”, “mindflock, open schedule.”", async () => {
    const t = session();
    const k = hooks();
    const d = new LiveDictation(t, k.h, { continuous: true });
    await d.heard("refactor the parser");
    await d.heard("Mine flock, open intake.");
    await d.heard("mindflock, open schedule.");
    await d.heard("Hey MindFlock, open settings.");
    expect(t.keys).toEqual(["refactor the parser "]);
    expect(k.routed.slice(1)).toEqual(["Mine flock, open intake.", "mindflock, open schedule.", "Hey MindFlock, open settings."]);
    expect(d.active).toBe(true);
  });

  it("push to talk (not continuous): listening goes on after a mid-message command too", async () => {
    const t = session();
    const k = hooks();
    const d = new LiveDictation(t, k.h, { continuous: false });
    await d.heard("fix the login bug");
    await d.heard("My flock, open settings.");
    expect(d.active).toBe(true);
    expect(t.keys).toEqual(["fix the login bug "]);
  });

  it("a command that ends listening (“MindFlock, stop listening” → end) ends it, the words staying", async () => {
    const t = session();
    const k = hooks({ route: async (x) => (wakeRest(x) ? "end" : false) });
    const d = new LiveDictation(t, k.h, { continuous: true });
    await d.heard("fix the login bug");
    await d.heard("MindFlock, open the ask field.");
    expect(d.active).toBe(false);
    expect(t.keys).toEqual(["fix the login bug "]);
  });

  it("the router says prompt after all: the phrase is typed as words", async () => {
    const t = session();
    const k = hooks({ route: async () => false });
    const d = new LiveDictation(t, k.h, { continuous: true });
    await d.heard("fix the login bug");
    await d.heard("My flock, of sheep.");
    expect(t.keys).toEqual(["fix the login bug ", "My flock, of sheep. "]);
    expect(d.active).toBe(true);
  });

  it("no pause after an ordinary-word form (“my flock open settings”) is dictation, never routed mid-message", async () => {
    const t = session();
    const k = hooks();
    const d = new LiveDictation(t, k.h, { continuous: true });
    await d.heard("the birds are");
    await d.heard("my flock open settings");
    expect(k.routed).toEqual(["the birds are"]);
    expect(t.keys).toEqual(["the birds are ", "my flock open settings "]);
  });

  it("a streamed wake phrase waits whole: its guess is never typed, then the router has it", async () => {
    const t = session();
    const k = hooks();
    const d = new LiveDictation(t, k.h, { continuous: true });
    await d.heard("fix the login bug");
    await d.words("My");
    await d.words("flock open");
    expect(t.keys).toEqual(["fix the login bug "]);
    await d.flush("settings", "My flock, open settings.");
    expect(t.keys).toEqual(["fix the login bug "]);
    expect(k.routed).toEqual(["fix the login bug", "My flock, open settings."]);
    expect(k.confirmations).toEqual(["Opening Settings."]);
  });

  it("an ordinary phrase starting “My …” is typed as soon as it can't be the wake word", async () => {
    const t = session();
    const k = hooks();
    const d = new LiveDictation(t, k.h, { continuous: true });
    await d.heard("fix the login bug");
    await d.words("My");
    expect(t.keys).toEqual(["fix the login bug "]);
    await d.words("tests fail");
    expect(t.keys.join("")).toContain("My tests fail");
  });

  it("the first phrase may still be a command as before", async () => {
    const t = session();
    const k = hooks();
    const d = new LiveDictation(t, k.h, { continuous: true });
    await d.heard("My flock, open settings.");
    expect(t.keys).toEqual([]);
    expect(k.routed).toEqual(["My flock, open settings."]);
    expect(d.active).toBe(true);
  });

  it("a text box is never routed (the field takes every word)", async () => {
    const keys: string[] = [];
    const field: DictTarget = { kind: "field", title: "", name: "box", type: (x) => keys.push(x) > 0, erase: (x) => keys.push("⌫" + x) > 0 };
    const k = hooks();
    const d = new LiveDictation(field, k.h, { continuous: true });
    d.dict.routes = false;
    await d.heard("note that");
    await d.heard("My flock, open settings.");
    expect(k.routed).toEqual([]);
    expect(keys.join("")).toContain("My flock, open settings.");
  });
});

describe("the wake word as the recogniser writes it (voiceWords wakeAt, the server's wake_at)", () => {
  it.each([
    ["My flock, open settings.", "open settings."],
    ["Mine flock, open settings.", "open settings."],
    ["mine flock open intake", "open intake"],
    ["Mind flock, open intake", "open intake"],
    ["mindflock, open schedule.", "open schedule."],
    ["Hey MindFlock, open settings", "open settings"],
    ["ask MindFlock what needs me", "what needs me"],
    ["Me flock: open settings", "open settings"],
    ["Mind block, open intake.", "open intake."],
    ["MindFlocks, open intake", "open intake"],
  ])("“%s” is said to MindFlock", (text, rest) => {
    expect(wakeRest(text)).toBe(rest);
    expect(isWakePhrase(text)).toBe(true);
  });

  it.each([
    "my flock open settings", // an ordinary-word form with no pause
    "MindFlock's settings page is slow",
    "tell mindflock to open intake",
    "fix the mindflock login bug",
    "my tests fail",
    "flock mode on",
  ])("“%s” is not", (text) => {
    expect(isWakePhrase(text)).toBe(false);
  });

  it("the wake word alone starts nothing (it has no command)", () => {
    expect(wakeAt("MindFlock.")).toBe(1);
    expect(isWakePhrase("MindFlock.")).toBe(false);
  });

  it("a person's own wake word counts, and the default's still does", () => {
    setVoiceWords({ ...SERVER_WORDS, wake: "jarvis", heard: { ...SERVER_WORDS.heard, wake: [] } });
    expect(wakeRest("Jarvis, open intake")).toBe("open intake");
    expect(wakeRest("jarvis open intake")).toBe("open intake");
    expect(wakeRest("MindFlock, open intake")).toBe("open intake");
  });

  it("mayBeWake: a guess still on its way to the wake word waits", () => {
    for (const t of ["My", "Mind", "Mine flock", "my flock open", "hey my", "MindFlock open"]) expect(mayBeWake(t), t).toBe(true);
    // A lone lead word is typed as it comes: "please" starts many prompts.
    for (const t of ["My tests", "fix the", "Hey", "please", "Hey there", "the flock"]) expect(mayBeWake(t), t).toBe(false);
  });
});

describe("“Opening Settings.”: opening something says so", () => {
  it("openingLine names what opened", () => {
    const open = (target: string): UiAction => ({ do: "open", target });
    expect(openingLine(open("settings"), { ok: true, text: "Opened Settings" })).toBe("Opening Settings.");
    expect(openingLine(open("intake"), { ok: true, text: "Opened Intake" })).toBe("Opening Intake.");
    expect(openingLine(open("schedule"), { ok: true, text: "Opened Schedule" })).toBe("Opening Schedule.");
    expect(openingLine(open("settings:voice"), { ok: true, text: "Opened Settings → Voice" })).toBe("Opening Voice settings.");
    expect(openingLine(open("intake"), { ok: true, text: "Opened Intake (tickets, PRs, issues)" })).toBe("Opening Intake.");
    expect(openingLine(open("x"), { ok: false, text: "Can't open x" })).toBe("");
    expect(openingLine({ do: "close" }, { ok: true, text: "Closed Settings" })).toBe("");
  });

  function assistant(confirm?: (line: string) => boolean) {
    let chimes = 0;
    const spoken: string[] = [];
    const s = new AssistantSession({
      stream: async (_body, on) => {
        const evs: AssistantEvent[] = [{ type: "act", action: { do: "open", target: "settings" } }, { type: "done", ms: 1, model: false }];
        evs.forEach(on);
      },
      snapshot: () => ({}),
      exec: () => ({ ok: true, text: "Opened Settings", open: "settings" }),
      speak: (x) => void spoken.push(x),
      chime: () => void chimes++,
      focus: () => {},
      talk: () => "brief",
      confirm,
      view: new ViewStore(),
      conversation: new Conversation(() => 0, () => "c1"),
    });
    return { s, chimes: () => chimes, spoken };
  }

  it("the page speaks confirmations: the line is asked for, no chime", async () => {
    const lines: string[] = [];
    const a = assistant((l) => (lines.push(l), true));
    await a.s.turn("open settings");
    expect(lines).toEqual(["Opening Settings."]);
    expect(a.chimes()).toBe(0);
    // Not the assistant's own voice: the confirmation path says it.
    expect(a.spoken).toEqual([]);
  });

  it("the page keeps quiet (both prefs off): the chime, as before", async () => {
    const a = assistant(() => false);
    await a.s.turn("open settings");
    expect(a.chimes()).toBe(1);
    const b = assistant();
    await b.s.turn("open settings");
    expect(b.chimes()).toBe(1);
  });

  it("only with spoken replies or spoken confirmations on", () => {
    const p = (o: Partial<VoicePrefs>): VoicePrefs => ({ ptt: "toggle", replies: "off", wake: false, confirm: false, sounds: true, ...o });
    expect(announcesActions(p({}))).toBe(false);
    expect(announcesActions(p({ wake: true }))).toBe(false);
    expect(announcesActions(p({ confirm: true }))).toBe(true);
    expect(announcesActions(p({ replies: "mine" }))).toBe(true);
    expect(announcesActions(p({ replies: "all" }))).toBe(true);
  });

  it("Confirmer.say({line}, {force}) speaks with the confirmations switch off", async () => {
    const lines: string[] = [];
    const c = new Confirmer({
      queue: { confirmLine: async (l: string) => void lines.push(l) },
      on: () => false,
      mic: () => null,
      server: () => false,
      local: () => false,
      speaker: { prefetch: async () => {}, lastError: "" },
    });
    await c.say({ line: "Opening Settings." });
    expect(lines).toEqual([]);
    await c.say({ line: "Opening Settings." }, { force: true });
    expect(lines).toEqual(["Opening Settings."]);
  });

  it("TopBarMic wires it: the assistant's confirm goes through announcesActions and the Confirmer", () => {
    const src = code("components/TopBarMic.tsx");
    expect(src).toMatch(/confirm: \(line\) => assistantHooks\.confirm\?\.\(line\) \?\? false/);
    expect(src).toMatch(/assistantHooks\.confirm = \(line\) => \{\s*if \(!announcesActions\(prefsRef\.current\)\) return false;\s*void confirmer\.current\?\.say\(\{ line \}, \{ force: true \}\);/);
  });
});
