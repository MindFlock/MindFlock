// Voice cycle 6 — desktop dictation through the server's gate. The judge
// measured the hole (cycle5/judge-rescore.md): live dictation went down the
// terminal websocket as raw keys, and a Codex dialog that came up was seen by
// this page only on the server's next 4 s state tick (2.1–2.4 s later) — a
// "yes …" in that window typed "y" and approved the command. Every key now
// goes through POST /send {voice: true, submit: false, erase, text}, which
// reads the screen at send time and refuses (409 in_dialog, nothing pressed).
import { afterEach, describe, expect, it, vi } from "vitest";

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
  return { instApi: vi.fn(), api: vi.fn(), apiRaw: vi.fn(), ApiError, peekTerm: vi.fn() };
});
vi.mock("../api/client", () => ({ api: mocks.api, apiRaw: mocks.apiRaw, instApi: mocks.instApi, ApiError: mocks.ApiError }));
vi.mock("../lib/terminals", () => ({ peekTerm: mocks.peekTerm }));

import {
  Dictation,
  KEY_TIMEOUT_MS,
  KeyGate,
  LiveDictation,
  dialogSeen,
  dialogStopLine,
  keysLine,
  postKeys,
  runVoiceCommand,
  sessionTarget,
  type CommandDeps,
  type KeyPost,
  type LiveHooks,
  type RouteReply,
} from "../lib/voiceDesktop";

const IN_DIALOG = () => new mocks.ApiError(409, "Answer its prompt first", { in_dialog: true });
const settle = () => new Promise((r) => setTimeout(r, 0));

/** A server whose answers the test releases one by one: every request, and
 * the field as the server's keys left it (BSpace ×erase, then the text). */
function server() {
  const calls: Array<{ title: string; erase: number; text: string; done: (err?: unknown) => void }> = [];
  let field = "";
  const post: KeyPost = (title, erase, text) =>
    new Promise((res, rej) => {
      calls.push({
        title,
        erase,
        text,
        done: (err) => {
          if (err) return rej(err);
          field = Array.from(field).slice(0, Math.max(0, Array.from(field).length - erase)).join("") + text;
          res({ sent: true });
        },
      });
    });
  return {
    post,
    calls,
    field: () => field,
    /** Answer the oldest unanswered request. */
    async answer(err?: unknown) {
      const c = calls.find((x) => !(x as { answered?: boolean }).answered) as (typeof calls)[number] & { answered?: boolean };
      c.answered = true;
      c.done(err);
      await settle();
    },
  };
}

function hooks(over: Partial<LiveHooks> = {}) {
  const captions: string[] = [];
  const calls = { routed: [] as Array<[string, number | undefined]>, halt: 0, checks: [] as string[] };
  const h: LiveHooks = {
    caption: (t) => void captions.push(t),
    halt: () => void calls.halt++,
    route: async (t, spokeAt) => {
      calls.routed.push([t, spokeAt]);
      return false;
    },
    activity: () => "working", // the rows lag: the page never hears of the dialog
    replies: () => false,
    submitted: () => {},
    dialogCheck: (t) => void calls.checks.push(t),
    ...over,
  };
  return { h, captions, calls, last: () => captions[captions.length - 1], routed: () => calls.routed.map((r) => r[0]) };
}

afterEach(() => {
  expect(mocks.peekTerm).not.toHaveBeenCalled(); // never the terminal socket
  mocks.instApi.mockReset();
  mocks.peekTerm.mockReset();
  dialogSeen.note("t1", null);
  vi.useRealTimers();
});

describe("KeyGate: in order, one request at a time, bursts folded", () => {
  it("goes at once when idle; what comes while a batch flies is ONE next batch", async () => {
    const s = server();
    const g = new KeyGate("t1", "Login fix", s.post);
    g.push("", "fix the bu");
    expect(s.calls.map((c) => [c.erase, c.text])).toEqual([[0, "fix the bu"]]); // no added wait
    g.push("u", "ug"); // the recognizer's next answers, while it flies
    g.push("ug", "ag");
    g.push("", " now");
    expect(s.calls).toHaveLength(1);
    await s.answer();
    // "u"→"ug"→"ag"→" now": one take-back of what the server has, one text.
    expect(s.calls.map((c) => [c.erase, c.text])).toEqual([
      [0, "fix the bu"],
      [1, "ag now"],
    ]);
    await s.answer();
    expect(s.field()).toBe("fix the bag now");
    expect(g.idle).toBe(true);
    expect(await g.settled()).toBeNull();
  });

  it("an erase that reaches only queued text just drops it: nothing sent for a word typed and taken back in flight", async () => {
    const s = server();
    const g = new KeyGate("t1", "x", s.post);
    g.push("", "one ");
    g.push("", "two");
    g.push("two", "");
    await s.answer();
    expect(s.calls).toHaveLength(1);
    expect(s.field()).toBe("one ");
    expect(g.idle).toBe(true);
  });

  it("counts code points: an emoji or an accent is one Backspace", async () => {
    const s = server();
    const g = new KeyGate("t1", "x", s.post);
    g.push("", "a");
    g.push("", "café 🚀");
    g.push("é 🚀", "e");
    await s.answer();
    expect(s.calls[1]).toMatchObject({ erase: 0, text: "cafe" });
    await s.answer();
    g.push("cafe", "");
    expect(s.calls[2]).toMatchObject({ erase: 4, text: "" });
    await s.answer();
    expect(s.field()).toBe("a");
  });

  it("two targets on one session share its lane: the second waits for the first", async () => {
    const s = server();
    const a = new KeyGate("t1", "x", s.post);
    const b = new KeyGate("t1", "x", s.post);
    a.push("", "first ");
    b.push("", "second ");
    expect(s.calls.map((c) => c.text)).toEqual(["first "]);
    await s.answer();
    expect(s.calls.map((c) => c.text)).toEqual(["first ", "second "]);
    await s.answer();
    expect(s.field()).toBe("first second ");
  });

  it("a refusal drops everything queued behind it, says what never went in, and takes nothing more until reset", async () => {
    const s = server();
    const g = new KeyGate("t1", "Login fix", s.post);
    const seen: unknown[] = [];
    g.onRefused((r) => seen.push(r));
    g.push("", "please run ");
    await s.answer();
    g.push("", "yes");
    g.push("s", "t and");
    await s.answer(IN_DIALOG());
    expect(seen).toEqual([{ dialog: true, line: expect.any(String), text: "yet and", erased: "" }]);
    expect(g.push("", "more")).toBe(false);
    expect(s.calls).toHaveLength(2);
    expect(await g.settled()).toMatchObject({ dialog: true });
    g.reset();
    expect(g.push("", "next ")).toBe(true);
    expect(s.calls).toHaveLength(3);
    await s.answer();
    expect(s.field()).toBe("please run next ");
  });

  it("a refused take-back says what is still there", async () => {
    const s = server();
    const g = new KeyGate("t1", "x", s.post);
    let r: unknown = null;
    g.onRefused((x) => (r = x));
    g.push("", "fix the bug ");
    await s.answer();
    g.push("bug ", "bag ");
    await s.answer(IN_DIALOG());
    expect(r).toMatchObject({ dialog: true, text: "bag ", erased: "bug " });
  });

  it("a network error is a failure, not a dialog; a paused session says so", async () => {
    const s = server();
    const g = new KeyGate("t1", "Login fix", s.post);
    g.push("", "x");
    await s.answer(new TypeError("Failed to fetch"));
    expect(g.refused).toMatchObject({ dialog: false, line: "Couldn't type into Login fix — the server didn't answer" });
    const p = server();
    const h = new KeyGate("t1", "Login fix", p.post);
    h.push("", "x");
    await p.answer(new mocks.ApiError(409, "session is paused", { paused: true }));
    expect(h.refused).toMatchObject({ dialog: false, line: "Login fix is paused — resume it first" });
  });

  it("a request that never answers fails after KEY_TIMEOUT_MS and frees the session's lane", async () => {
    vi.useFakeTimers();
    const hung: KeyPost = () => new Promise(() => {});
    const g = new KeyGate("t1", "Login fix", hung);
    g.push("", "hello");
    await vi.advanceTimersByTimeAsync(KEY_TIMEOUT_MS + 10);
    expect(g.refused).toMatchObject({ dialog: false, text: "hello", line: "Couldn't type into Login fix — the server didn't answer" });
    vi.useRealTimers();
    const s = server();
    const next = new KeyGate("t1", "Login fix", s.post);
    next.push("", "again");
    expect(s.calls).toHaveLength(1); // the lane is free
    await s.answer();
  });

  it("measures every batch's round trip (window.__mfVoiceKeys)", async () => {
    vi.stubGlobal("window", {});
    const s = server();
    const g = new KeyGate("t1", "x", s.post);
    g.push("", "a");
    await new Promise((r) => setTimeout(r, 25));
    await s.answer();
    const times = window.__mfVoiceKeys || [];
    expect(times.length).toBeGreaterThan(0);
    expect(times[times.length - 1]).toBeGreaterThanOrEqual(20);
    vi.unstubAllGlobals();
  });

  it("the request is POST /send {voice: true, submit: false, erase, text}", async () => {
    mocks.instApi.mockResolvedValue({ sent: true, submitted: false, erased: 2 });
    await postKeys("t1", 2, "ag");
    expect(mocks.instApi).toHaveBeenCalledWith("t1", "/send", { json: { voice: true, submit: false, erase: 2, text: "ag" } });
  });
});

describe("Dictation.untyped: what was counted as typed is put right", () => {
  it("a settled phrase's words come back as words to hand over", () => {
    const d = new Dictation();
    d.begin();
    d.wrote("please fix ");
    d.wrote("yes and also ");
    expect(d.untyped("yes and also ", "")).toBe("yes and also ");
    expect(d.typed).toBe("please fix ");
  });

  it("a guess wholly lost: the phrase's own words wait for its end", () => {
    const d = new Dictation();
    d.begin();
    d.wrote("please fix ");
    const g = d.stream("", "and add");
    expect(g).toMatchObject({ kind: "type", text: "and add" });
    d.wrote("and add");
    expect(d.untyped("and add", "")).toBe("");
    expect(d.typed).toBe("please fix ");
    expect(d.guessed).toBe("");
    expect(d.inPhrase).toBe(true);
  });

  it("a guess partly in: only the part that went in counts", () => {
    const d = new Dictation();
    d.begin();
    d.stream("", "fix the bug");
    d.wrote("fix the bug");
    d.stream("", "fix the bag");
    d.unwrote("ug");
    d.wrote("ag");
    expect(d.untyped("ag", "ug")).toBe("");
    expect(d.typed).toBe("fix the bug");
    expect(d.guessed).toBe("fix the bug");
  });

  it("a refused disregard: the words are all still there", () => {
    const d = new Dictation();
    d.begin();
    d.wrote("one two ");
    expect(d.heard("disregard")).toEqual({ kind: "erase", text: "one two " });
    expect(d.untyped("", "one two ")).toBe("");
    expect(d.typed).toBe("one two ");
  });
});

describe("LiveDictation through the gate (the judge's 2.1–2.4 s window)", () => {
  it("a dialog up on screen before the page knows: the next phrase is refused, nothing typed, the words go to the router", async () => {
    const s = server();
    const k = hooks();
    const d = new LiveDictation(sessionTarget("t1", "Codex fix", s.post), k.h, { continuous: true });
    await d.heard("please fix the login");
    await s.answer();
    expect(s.field()).toBe("please fix the login ");
    // Codex's approval came up; the rows still say "working" (the 4 s tick).
    const spoke = Date.now();
    await d.heard("yes and also", spoke);
    await s.answer(IN_DIALOG());
    await settle();
    expect(s.field()).toBe("please fix the login "); // no "y" reached it
    expect(d.dict.typed).toBe("please fix the login "); // its count put right
    expect(k.routed()).toEqual(["please fix the login", "yes and also"]);
    expect(k.calls.routed[1][1]).toBe(spoke); // the 300 ms answer rule gets when it began
    expect(k.last()).toBe(dialogStopLine("Codex fix", "yes and also"));
    expect(d.active).toBe(false);
    expect(dialogSeen.blocks("t1")).toBe(true); // noted: the next dictation types nothing
    expect(k.calls.checks).toContain("t1"); // and /dialog read for its id
  });

  it("refused mid-guess: typing stops at once for the phrase; its end hands the whole untyped rest over", async () => {
    const s = server();
    const k = hooks();
    const d = new LiveDictation(sessionTarget("t1", "Login fix", s.post), k.h, { continuous: true });
    await d.heard("please fix the login");
    await s.answer();
    await d.stream("", "and add");
    await s.answer(IN_DIALOG());
    expect(d.dict.typed).toBe("please fix the login "); // the guess never went in
    expect(d.dict.guessed).toBe("");
    await d.stream("and add", "a test");
    await d.stream("a", "test now"); // w: words committed since the last answer
    expect(s.calls).toHaveLength(2); // nothing after the refusal
    await d.flush("test now");
    await settle();
    expect(k.routed()).toEqual(["please fix the login", "and add a test now"]);
    expect(k.last()).toBe(dialogStopLine("Login fix", "and add a test now"));
    expect(s.field()).toBe("please fix the login ");
    expect(s.calls).toHaveLength(2);
  });

  it("the router answering the dialog keeps a continuous dictation going, and the next message types again", async () => {
    const s = server();
    let answered = false;
    const k = hooks({
      route: async (t) => {
        k.calls.routed.push([t, undefined]);
        if (t === "yes") {
          answered = true;
          dialogSeen.note("t1", null); // answered: the sighting goes
          return true;
        }
        return false;
      },
    });
    const d = new LiveDictation(sessionTarget("t1", "Login fix", s.post), k.h, { continuous: true });
    await d.heard("please fix the login");
    await s.answer();
    await d.heard("yes");
    await s.answer(IN_DIALOG());
    await settle();
    expect(answered).toBe(true);
    expect(d.active).toBe(true);
    await d.heard("and add a test");
    await s.answer();
    expect(s.field()).toBe("please fix the login and add a test ");
  });

  it("“over” presses Enter only after its words went in — and never when they were refused", async () => {
    const s = server();
    mocks.instApi.mockResolvedValue({ sent: true, submitted: true, entered: true });
    const k = hooks();
    const d = new LiveDictation(sessionTarget("t1", "Login fix", s.post), k.h, { continuous: true });
    const p = d.heard("run the tests over");
    await settle();
    expect(s.calls.map((c) => c.text)).toEqual(["run the tests "]);
    expect(mocks.instApi).not.toHaveBeenCalled(); // waits on the words
    await s.answer();
    await p;
    expect(mocks.instApi).toHaveBeenCalledWith("t1", "/send", { json: { enter: true, say: false } });
    expect(s.field()).toBe("run the tests ");

    mocks.instApi.mockClear();
    const s2 = server();
    const k2 = hooks();
    const d2 = new LiveDictation(sessionTarget("t1", "Login fix", s2.post), k2.h, { continuous: true });
    const p2 = d2.heard("yes over");
    await settle();
    await s2.answer(IN_DIALOG());
    await p2;
    await settle();
    expect(mocks.instApi).not.toHaveBeenCalled(); // no Enter into the dialog
    expect(k2.last()).toBe(dialogStopLine("Login fix", "yes"));
    expect(d2.active).toBe(false);
  });

  it("a refused “disregard” says so — never “Disregarded” — and stops", async () => {
    const s = server();
    const k = hooks();
    const d = new LiveDictation(sessionTarget("t1", "Login fix", s.post), k.h, { continuous: true });
    await d.heard("one two");
    await s.answer();
    const p = d.heard("disregard");
    await settle();
    await s.answer(IN_DIALOG());
    await p;
    await settle();
    expect(s.field()).toBe("one two ");
    expect(d.dict.typed).toBe("one two "); // still there, as counted
    expect(k.captions).not.toContain("Disregarded — go on");
    expect(k.last()).toBe(dialogStopLine("Login fix", ""));
    expect(d.active).toBe(false);
  });

  it("a network error stops and says so; nothing falls back to the terminal socket", async () => {
    const s = server();
    const k = hooks();
    const d = new LiveDictation(sessionTarget("t1", "Login fix", s.post), k.h);
    await d.heard("hello there");
    await s.answer(new TypeError("Failed to fetch"));
    expect(k.last()).toBe("Couldn't type into Login fix — the server didn't answer");
    expect(d.active).toBe(false);
    await d.heard("more");
    expect(s.calls).toHaveLength(1);
  });

  it("bursts keep their order: whatever the timing of the answers, the field ends as the words", async () => {
    // A seeded walk of streamed guesses (corrections, longer and shorter),
    // the server answering after every 0–3 pushes.
    let seed = 7;
    const rnd = (n: number) => ((seed = (seed * 1103515245 + 12345) % 2 ** 31), seed % n);
    const words = "fix the login bug and then add a test for the token refresh path".split(" ");
    for (let run = 0; run < 25; run++) {
      const s = server();
      const d = new LiveDictation(sessionTarget("t1", "x", s.post), hooks().h, { continuous: true });
      await d.heard("start here");
      await s.answer();
      let said: string[] = [];
      for (let i = 0; i < 14; i++) {
        said = words.slice(0, 1 + rnd(words.length));
        if (rnd(3) === 0) said = [...said.slice(0, -1), said[said.length - 1] + "s"]; // a changed last word
        await d.stream("", said.join(" "));
        for (let n = rnd(3); n > 0; n--) if (s.calls.some((c) => !(c as { answered?: boolean }).answered)) await s.answer();
      }
      await d.flush(said.join(" "));
      while (s.calls.some((c) => !(c as { answered?: boolean }).answered)) await s.answer();
      expect(s.field()).toBe("start here " + said.join(" ") + " ");
      // Folded: fewer requests than recognizer answers.
      expect(s.calls.length).toBeLessThanOrEqual(16);
    }
  });
});

describe("a router answer typed as a prompt goes through the gate too", () => {
  function deps(over: Partial<CommandDeps> = {}) {
    const out = { said: [] as string[], captions: [] as string[], submitted: [] as Array<[string, boolean]> };
    const d: CommandDeps = {
      railOrder: () => ["t1"],
      focused: () => "t1",
      select: () => {},
      name: () => "Login fix",
      caption: (l) => void out.captions.push(l),
      announce: (l) => void out.said.push(l),
      route: vi.fn(async () => ({ kind: "prompt" }) as RouteReply),
      approveDialogs: async () => true,
      activity: () => "idle",
      replies: () => true,
      submitted: (t, say) => void out.submitted.push([t, say]),
      ...over,
    } as CommandDeps;
    return { d, out };
  }

  it("refused (a dialog came up on screen): no Enter, said on a dialog", async () => {
    mocks.instApi.mockImplementation(async (_t: string, suffix: string, o?: { json?: Record<string, unknown> }) => {
      if (suffix === "/voice") return { say: "Which branch?", has_question: true };
      if (o?.json?.voice) throw IN_DIALOG();
      return { ok: true };
    });
    const { d, out } = deps();
    await runVoiceCommand({ kind: "answer", choice: "number", number: 2, no_dialog: true }, "two", d);
    expect(mocks.instApi.mock.calls.some((c) => (c[2] as { json?: { enter?: boolean } })?.json?.enter)).toBe(false);
    expect(out.said).toEqual(["Login fix is on a dialog — answer it on the screen."]);
    expect(dialogSeen.blocks("t1")).toBe(true);
  });
});

describe("diagnostics", () => {
  it("keysLine: the median batch round trip", () => {
    expect(keysLine([])).toBe("");
    expect(keysLine([30, 12, 18])).toBe("typing ~18 ms/batch");
  });
});
