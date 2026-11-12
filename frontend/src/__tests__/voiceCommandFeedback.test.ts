/** A spoken command triggers the first time, and a miss never passes
 * silently (the owner, 2026-11-10: "it's kind of hard to get some commands
 * to trigger sometimes"). Pinned here:
 *  - the router not answering (an error, a 502, past ROUTE_TIMEOUT_MS) no
 *    longer turns a command into a silent prompt: one that needs no server
 *    ("session one", "next session", "stop") is carried out by the page
 *    (localRoute), anything else is typed with a caption saying why;
 *  - a near miss (`near`), an unknown kind: typed, with a caption — never
 *    a spoken line;
 *  - a phrase said to MindFlock: after a wake (its first phrase, or the
 *    standby's `rest`), after a lone wake word mid-message ("My flock." …
 *    "open settings"), or with the wake word misheard ("Line flock, …") —
 *    routed, and nothing of it left typed;
 *  - a wake that hears nothing says so.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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

import {
  ADDRESSED_LINE,
  ADDRESSED_MS,
  LiveDictation,
  NOTE_CAPTION_MS,
  PendingChoice,
  ROUTE_NEW_TIMEOUT_MS,
  ROUTE_TIMEOUT_MS,
  WAKE_MISS_LINE,
  builtinCommands,
  localRoute,
  routeMissLine,
  routePhrase,
  type DictTarget,
  type LiveHooks,
  type RouteVerdict,
} from "../lib/voiceDesktop";
import { applyVoiceView, resetVoiceWords } from "../lib/voiceWords";
import { code } from "./sourceText";

beforeEach(() => {
  mocks.api.mockReset();
  mocks.api.mockResolvedValue({});
  // GET /api/voice's heard wake forms (core/voice_words.HEARD), as the page has them.
  applyVoiceView({ words: { heard: { wake: ["my flock", "mind flock", "mine flock", "mind lock"] } } });
});
afterEach(() => {
  vi.useRealTimers();
  resetVoiceWords();
});

function session(title = "t1", name = "Login fix"): DictTarget & { keys: string[] } {
  const keys: string[] = [];
  return { kind: "session", title, name, keys, type: (t) => keys.push(t) > 0, erase: (t) => keys.push("⌫" + t) > 0 };
}

function hooks(route: (t: string) => RouteVerdict = () => false) {
  const routed: string[] = [];
  const captions: string[] = [];
  const ends: string[] = [];
  const h: LiveHooks = {
    caption: (t) => void captions.push(t),
    halt: () => {},
    route: async (t) => (routed.push(t), route(t)),
    activity: () => "idle",
    replies: () => false,
    submitted: () => {},
    done: (how) => void ends.push(how),
  };
  return { h, routed, captions, ends };
}

/** What a session's input field holds after its keys. */
const field = (keys: string[]) =>
  keys.reduce((s, k) => (k.startsWith("⌫") ? (s.endsWith(k.slice(1)) ? s.slice(0, s.length - k.length + 1) : s) : s + k), "");

describe("the router not answering is never a silent prompt", () => {
  it("“Session one.” with the route failing (a 502, the network) still switches: the page knows that command", async () => {
    mocks.api.mockRejectedValueOnce(new mocks.ApiError(502, "bad gateway", null));
    const r = await routePhrase("Session one.", "t1", new PendingChoice());
    expect(r).toEqual({ kind: "switch_index", index: 1, failed: "error" });
  });

  it("past ROUTE_TIMEOUT_MS the page decides: “next session” moves, a prompt is typed with a caption", async () => {
    vi.useFakeTimers();
    mocks.api.mockReturnValue(new Promise(() => {}));
    const a = routePhrase("next session", "t1", new PendingChoice());
    await vi.advanceTimersByTimeAsync(ROUTE_TIMEOUT_MS - 1);
    let got: unknown = null;
    void a.then((r) => (got = r));
    await vi.advanceTimersByTimeAsync(0);
    expect(got).toBeNull();
    await vi.advanceTimersByTimeAsync(1);
    expect(await a).toEqual({ kind: "switch_rel", delta: 1, failed: "timeout" });
    const b = routePhrase("fix the login bug", "t1", new PendingChoice());
    await vi.advanceTimersByTimeAsync(ROUTE_TIMEOUT_MS);
    const r = await b;
    expect(r).toEqual({ kind: "prompt", failed: "timeout" });
    expect(routeMissLine(r)).toBe("Typed — the voice router didn't answer in time");
  });

  it("a phrase that may start a session waits as long as its creation can take", async () => {
    vi.useFakeTimers();
    let answer: (v: unknown) => void = () => {};
    mocks.api.mockReturnValue(new Promise((res) => (answer = res)));
    const a = routePhrase("new session to fix the build", "t1", new PendingChoice());
    await vi.advanceTimersByTimeAsync(ROUTE_TIMEOUT_MS * 3);
    answer({ kind: "new", title: "fix-the-build", say: "Started fix the build." });
    expect((await a).kind).toBe("new");
    expect(ROUTE_NEW_TIMEOUT_MS).toBeGreaterThan(ROUTE_TIMEOUT_MS * 3);
  });

  it.each([
    ["session one", { kind: "switch_index", index: 1 }],
    ["um, session twenty one", { kind: "switch_index", index: 21 }],
    ["session to", { kind: "switch_index", index: 2 }],
    ["switch to row 3", { kind: "switch_index", index: 3 }],
    ["previous session", { kind: "switch_rel", delta: -1 }],
    ["go back", { kind: "switch_back" }],
    ["where am I", { kind: "where" }],
    ["what can I say", { kind: "commands" }],
    ["never mind", { kind: "cancel" }],
    ["stop listening", { kind: "stop" }],
    ["stop", { kind: "stop" }],
    ["be quiet", { kind: "hush" }],
  ])("the page's own table: %j", (said, want) => {
    expect(localRoute(said)).toEqual(want);
  });

  it.each(["scroll up", "switch to docs", "fix the login bug", "two tests fail", "the number 3 is wrong"])(
    "%j needs the server (or is words): none locally",
    (said) => expect(localRoute(said)).toBeNull()
  );
});

describe("a typed miss says why — a caption, never a spoken line", () => {
  it.each([
    [{ kind: "prompt", near: { kind: "switch", why: "no_session", arg: "docks" } }, "Typed — no session called docks"],
    [
      { kind: "prompt", near: { kind: "switch", why: "no_session", arg: "docks", suggest: "docs" } },
      "Typed — no session called docks (did you mean docs?)",
    ],
    [{ kind: "prompt", near: { kind: "answer", why: "no_dialog" } }, "Typed — nothing is being asked"],
    [{ kind: "prompt", near: { kind: "status", why: "no_focus" } }, "Typed — no session is focused"],
    // A question about MindFlock said while a session is open: typed, the
    // caption says how to ask MindFlock — the question itself never echoed.
    [
      {
        kind: "prompt",
        near: { kind: "help", why: "unaddressed", suggest: "MindFlock, where do the tickets get picked up" },
      },
      "Typed — say “MindFlock, …” to ask MindFlock instead",
    ],
    [{ kind: "prompt", failed: "error" }, "Typed — couldn't reach the voice router"],
    [{ kind: "prompt", unknown: "teleport" }, "Typed — this page doesn't know “teleport” yet (reload it)"],
    [{ kind: "prompt" }, ""],
    [{ kind: "prompt", near: "garbage" }, ""],
    [{ kind: "prompt", near: { why: "no_session", arg: 7 } }, "Typed — no such session"],
    [{ kind: "switch", failed: "timeout" }, ""],
  ] as const)("%j → %j", (reply, line) => {
    expect(routeMissLine(reply as Parameters<typeof routeMissLine>[0])).toBe(line);
  });

  it("the words are typed, the caption shows the note, then the running words come back", async () => {
    vi.useFakeTimers();
    const t = session();
    const k = hooks(() => ({ note: "Typed — no session called docks" }));
    const d = new LiveDictation(t, k.h, { continuous: true });
    await d.heard("switch to docks");
    expect(k.routed).toEqual(["switch to docks"]);
    expect(t.keys).toEqual(["switch to docks "]);
    expect(k.captions[k.captions.length - 1]).toBe("Typed — no session called docks");
    await vi.advanceTimersByTimeAsync(NOTE_CAPTION_MS + 10);
    expect(k.captions[k.captions.length - 1]).toBe("switch to docks");
    expect(d.active).toBe(true);
  });

  it("TopBarMic turns a prompt with a why into the note; an unknown kind traces", () => {
    const mic = code("components/TopBarMic.tsx");
    expect(mic).toMatch(/routeMissLine\(reply\)/);
    expect(mic).toMatch(/if \(missed\) return \{ note: missed \};/);
    expect(mic).toMatch(/voiceTrace\("route", \{/);
  });
});

describe("“Session one.” as a message's first phrase (the trace, 2026-11-10)", () => {
  it("its final goes to the router and nothing of it is typed", async () => {
    const t = session();
    const k = hooks(() => true);
    const d = new LiveDictation(t, k.h, { continuous: true });
    await d.stream("", "Session");
    await d.stream("", "Session one");
    expect(t.keys).toEqual([]);
    await d.flush("", "Session one.");
    expect(k.routed).toEqual(["Session one."]);
    expect(t.keys).toEqual([]);
    expect(d.active).toBe(true);
  });

  it("“session twelve” and “session number three” are held too (the gate stopped at nine)", async () => {
    for (const said of ["session twelve", "session number three", "take me to the sitecheck session", "what's docs doing"]) {
      const t = session();
      const k = hooks(() => true);
      const d = new LiveDictation(t, k.h, { continuous: true });
      let partial = "";
      for (const w of said.split(" ")) await d.stream("", (partial = (partial + " " + w).trim()));
      expect(t.keys, said).toEqual([]);
      await d.flush("", said);
      expect(k.routed, said).toEqual([said]);
    }
  });
});

describe("said to MindFlock", () => {
  it("a wake-started listen: “open settings” goes to the assistant's route, nothing typed", async () => {
    const t = session();
    const k = hooks((s) => s.startsWith("MindFlock, "));
    const d = new LiveDictation(t, k.h, { continuous: true });
    d.address(8000);
    await d.stream("", "open");
    await d.stream("", "open settings");
    expect(t.keys).toEqual([]);
    await d.flush("", "Open settings.");
    expect(k.routed).toEqual(["MindFlock, Open settings."]);
    expect(t.keys).toEqual([]);
    expect(d.active).toBe(true);
    // Only its FIRST phrase: the next message is plain dictation again.
    await d.heard("fix the login bug");
    expect(k.routed[1]).toBe("fix the login bug");
  });

  it("a wake-started listen: a command of the router's own goes as said (instant), dictation stays dictation", async () => {
    const k = hooks((s) => s === "next session");
    const d = new LiveDictation(session(), k.h, { continuous: true });
    d.address(8000);
    await d.heard("next session");
    expect(k.routed).toEqual(["next session"]);
    const t = session();
    const k2 = hooks();
    const d2 = new LiveDictation(t, k2.h, { continuous: true });
    d2.address(8000);
    await d2.heard("fix the login bug");
    expect(k2.routed).toEqual(["fix the login bug"]);
    expect(t.keys).toEqual(["fix the login bug "]);
  });

  it("the standby's `rest` (“MindFlock, next session” in one breath) is that first phrase", () => {
    const mic = code("components/TopBarMic.tsx");
    expect(mic).toMatch(/const woken = \(soundEnd: number, rest\?: string\) =>/);
    expect(mic).toMatch(/d\.address\(WAKE_LISTEN_MS\);/);
    expect(mic).toMatch(/if \(said\) void d\.heard\(said/);
    expect(mic).toMatch(/onWake: \(soundEnd: number, rest\?: string\) => latestWake\.current\(soundEnd, rest\)/);
  });

  it("an address lapses: a phrase begun after the window is plain dictation", async () => {
    vi.useFakeTimers();
    const t = session();
    const k = hooks();
    const d = new LiveDictation(t, k.h, { continuous: true });
    d.address(1000);
    await vi.advanceTimersByTimeAsync(1500);
    await d.heard("open the settings file and check the port");
    expect(k.routed).toEqual(["open the settings file and check the port"]);
    expect(t.keys).toEqual(["open the settings file and check the port "]);
  });

  it("a lone wake word mid-message types nothing and arms the next phrase", async () => {
    const t = session();
    const k = hooks((s) => s.startsWith("MindFlock, "));
    const d = new LiveDictation(t, k.h, { continuous: true });
    await d.heard("fix the login bug");
    await d.stream("", "My");
    await d.stream("", "My flock");
    await d.flush("", "My flock.");
    expect(field(t.keys)).toBe("fix the login bug ");
    expect(k.captions[k.captions.length - 1]).toBe(ADDRESSED_LINE);
    await d.stream("", "open settings");
    await d.flush("", "Open settings.");
    expect(k.routed).toEqual(["fix the login bug", "MindFlock, Open settings."]);
    // The message so far stays; nothing of the command went in.
    expect(field(t.keys)).toBe("fix the login bug ");
    expect(d.active).toBe(true);
    expect(ADDRESSED_MS).toBeGreaterThanOrEqual(4000);
  });

  it("a wake word misheard mid-message (“Line flock, open settings.”) is held, never typed only to be taken back", async () => {
    const t = session();
    const k = hooks((s) => s.startsWith("Mine flock"));
    const d = new LiveDictation(t, k.h, { continuous: true });
    await d.heard("check the build");
    const before = t.keys.length;
    await d.stream("", "Line");
    await d.stream("", "Line flock,");
    await d.stream("", "Line flock, open settings.");
    expect(t.keys.length).toBe(before);
    // Its final, heard right, is the router's — nothing of it typed.
    await d.flush("", "Mine flock, open settings.");
    expect(k.routed).toEqual(["check the build", "Mine flock, open settings."]);
    expect(field(t.keys)).toBe("check the build ");
  });

  it("a wake that hears nothing says so (a caption, never a line)", () => {
    const k = hooks();
    const d = new LiveDictation(session(), k.h, { continuous: true });
    d.drop(WAKE_MISS_LINE);
    expect(k.captions[k.captions.length - 1]).toBe("Didn't catch anything");
    expect(k.ends).toEqual(["missed"]);
    expect(code("components/TopBarMic.tsx")).toMatch(/d\.drop\(WAKE_MISS_LINE\)/);
  });
});

describe("the offline cheat sheet says what is routed", () => {
  it("names the phrasings the gate now lets through", () => {
    const says = builtinCommands().map((r) => r.say);
    expect(says).toContain("what's <name> doing");
    expect(says).toContain("what needs me");
    expect(says.some((s) => s.includes("take me to <name>"))).toBe(true);
    expect(says.some((s) => s.startsWith("MindFlock, "))).toBe(true);
  });
});
