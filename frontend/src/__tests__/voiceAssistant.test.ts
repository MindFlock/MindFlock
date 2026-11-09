/** The voice assistant's client (lib/voiceAssistant): the NDJSON stream
 * reader (chunk boundaries, partial lines, bad lines), the events, the
 * conversation id's rollover, the talk policy, yes / no, the mode's end
 * words, the warm-up, and a whole turn — acts, server results, words, ask
 * and confirm — against a faked stream. */

import { describe, expect, it, vi } from "vitest";
import {
  ASSISTANT_FAILED_LINE,
  AssistantSession,
  CONFIRM_TTL_MS,
  CONVERSATION_IDLE_MS,
  Conversation,
  NdjsonSplitter,
  ViewStore,
  WARM_EVERY_MS,
  addressed,
  isAssistantEnd,
  parseEvent,
  readEvents,
  resetWarm,
  speaksKind,
  streamAssistant,
  stripVisible,
  talkFromSettings,
  talkLevel,
  warmAssistant,
  yesNo,
  type AssistantBody,
  type AssistantDeps,
  type AssistantEvent,
  type TalkLevel,
  type UiAction,
} from "../lib/voiceAssistant";

const enc = new TextEncoder();
const streamOf = (chunks: Array<string | Uint8Array>) =>
  new ReadableStream<Uint8Array>({
    start(c) {
      for (const ch of chunks) c.enqueue(typeof ch === "string" ? enc.encode(ch) : ch);
      c.close();
    },
  });

async function collect(chunks: Array<string | Uint8Array>): Promise<AssistantEvent[]> {
  const out: AssistantEvent[] = [];
  for await (const e of readEvents(streamOf(chunks))) out.push(e);
  return out;
}

describe("the NDJSON reader", () => {
  it("holds a line split across chunks until its newline", () => {
    const s = new NdjsonSplitter();
    expect(s.push('{"type":"sa')).toEqual([]);
    expect(s.push('y","text":"hi"}\n{"type":')).toEqual([{ type: "say", text: "hi" }]);
    expect(s.push('"done","ms":3}\n')).toEqual([{ type: "done", ms: 3 }]);
  });

  it("takes several lines in one chunk, skips blank and broken lines", () => {
    const s = new NdjsonSplitter();
    expect(s.push('{"a":1}\n\n  \nnot json\n{"b":2}\n')).toEqual([{ a: 1 }, { b: 2 }]);
  });

  it("reads a last line that has no newline at the stream's end", () => {
    const s = new NdjsonSplitter();
    expect(s.push('{"a":1}\n{"b":')).toEqual([{ a: 1 }]);
    expect(s.push("2}")).toEqual([]);
    expect(s.end()).toEqual([{ b: 2 }]);
    expect(s.end()).toEqual([]);
  });

  it("handles \\r\\n line ends", () => {
    expect(new NdjsonSplitter().push('{"a":1}\r\n{"b":2}\r\n')).toEqual([{ a: 1 }, { b: 2 }]);
  });

  it("yields events from a byte stream as they arrive — a UTF-8 character split across chunks too", async () => {
    const line = enc.encode('{"type":"say","text":"Café ✓"}\n');
    const cut = line.indexOf(0xc3) + 1; // inside "é"
    const events = await collect([line.slice(0, cut), line.slice(cut), '{"type":"done","ms":5,"model":true}']);
    expect(events).toEqual([
      { type: "say", text: "Café ✓" },
      { type: "done", ms: 5, model: true },
    ]);
  });

  it("drops events it doesn't know or that lack what they need", async () => {
    const events = await collect([
      '{"type":"wat"}\n',
      '{"type":"act","action":{"do":"explode"}}\n',
      '{"type":"act","action":{"do":"focus"}}\n',
      '{"type":"act","action":{"do":"open","target":"intake"}}\n',
      '{"type":"confirm","text":"Sure?","action":{"do":"close"}}\n', // no id
    ]);
    expect(events).toEqual([{ type: "act", action: { do: "open", target: "intake" } }]);
  });
});

describe("events", () => {
  it("parses every UiAction of the contract", () => {
    const acts: UiAction[] = [
      { do: "open", target: "settings:voice" },
      { do: "close" },
      { do: "view", mode: "sky" },
      { do: "focus", session: "fix-login" },
      { do: "command", label: "Toggle sidebar" },
      { do: "session_action", session: "fix-login", action: "push" },
      { do: "click", label: "Save" },
      { do: "pref", key: "sky_birds", value: false },
      { do: "type", text: "hello" },
    ];
    for (const a of acts) expect(parseEvent({ type: "act", action: a })).toEqual({ type: "act", action: a });
    expect(parseEvent({ type: "act", action: { do: "view", mode: "list" } })).toBeNull();
  });

  it("parses did (ok defaults true, detail an object), confirm with either kind of action, done", () => {
    expect(parseEvent({ type: "did", action: { do: "new_session", repo: "web", prompt: "x" }, text: "Started", detail: { title: "t" } })).toEqual({
      type: "did",
      action: { do: "new_session", repo: "web", prompt: "x" },
      ok: true,
      text: "Started",
      detail: { title: "t" },
    });
    expect(parseEvent({ type: "did", action: { do: "send", session: "a", text: "b" }, ok: false, text: "No" })).toMatchObject({ ok: false, detail: {} });
    expect(parseEvent({ type: "confirm", id: "c1", text: "Delete it?", action: { do: "session_action", session: "a", action: "delete" } })).toMatchObject({ id: "c1" });
    expect(parseEvent({ type: "confirm", id: "c2", text: "Send?", action: { do: "send", session: "a", text: "b" } })).toMatchObject({ id: "c2" });
    expect(parseEvent({ type: "done" })).toEqual({ type: "done", ms: 0, model: false });
  });
});

describe("streamAssistant", () => {
  const body: AssistantBody = { text: "open intake", conversation: "c", snapshot: {} };

  it("POSTs the body and hands each event over as it arrives", async () => {
    const fetch = vi.fn(async () => new Response(streamOf(['{"type":"act","action":{"do":"close"}}\n', '{"type":"done","ms":9,"model":false}\n']), { status: 200 }));
    const got: AssistantEvent[] = [];
    await streamAssistant(body, (e) => got.push(e), { fetch });
    expect(fetch).toHaveBeenCalledOnce();
    const [path, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(path).toBe("/api/voice/assistant");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual(body);
    expect(got).toEqual([
      { type: "act", action: { do: "close" } },
      { type: "done", ms: 9, model: false },
    ]);
  });

  it("a stream that ends without done gets one; nothing after done is passed on", async () => {
    const got: AssistantEvent[] = [];
    await streamAssistant(body, (e) => got.push(e), { fetch: async () => new Response(streamOf(['{"type":"say","text":"Hi"}\n']), { status: 200 }) });
    expect(got.map((e) => e.type)).toEqual(["say", "done"]);
    const got2: AssistantEvent[] = [];
    await streamAssistant(body, (e) => got2.push(e), {
      fetch: async () => new Response(streamOf(['{"type":"done","ms":1}\n{"type":"say","text":"late"}\n']), { status: 200 }),
    });
    expect(got2.map((e) => e.type)).toEqual(["done"]);
  });

  it("a refusal (an older server's 404, a 400) is an error event with the server's words, then done", async () => {
    const got: AssistantEvent[] = [];
    await streamAssistant(body, (e) => got.push(e), { fetch: async () => new Response(JSON.stringify({ error: "bad body" }), { status: 400 }) });
    expect(got[0]).toEqual({ type: "error", text: "bad body" });
    expect(got[1].type).toBe("done");
    const got2: AssistantEvent[] = [];
    await streamAssistant(body, (e) => got2.push(e), { fetch: async () => new Response("nope", { status: 404 }) });
    expect(got2[0]).toEqual({ type: "error", text: ASSISTANT_FAILED_LINE });
  });

  it("a network failure is an error; an abort ends quietly", async () => {
    const got: AssistantEvent[] = [];
    await streamAssistant(body, (e) => got.push(e), { fetch: async () => Promise.reject(new TypeError("offline")) });
    expect(got.map((e) => e.type)).toEqual(["error", "done"]);
    const ac = new AbortController();
    ac.abort();
    const got2: AssistantEvent[] = [];
    await streamAssistant(body, (e) => got2.push(e), {
      signal: ac.signal,
      fetch: async () => Promise.reject(Object.assign(new Error("aborted"), { name: "AbortError" })),
    });
    expect(got2).toEqual([]);
  });
});

describe("the warm-up", () => {
  it("POSTs /api/voice/assistant/warm at most once a minute", async () => {
    resetWarm();
    const post = vi.fn(async () => ({}));
    expect(warmAssistant(1_000_000, post)).toBe(true);
    expect(warmAssistant(1_000_000 + WARM_EVERY_MS - 1, post)).toBe(false);
    expect(warmAssistant(1_000_000 + WARM_EVERY_MS + 1, post)).toBe(true);
    await Promise.resolve();
    await Promise.resolve();
    expect(post).toHaveBeenCalledTimes(2);
    expect(post.mock.calls[0]).toEqual(["/api/voice/assistant/warm", { method: "POST" }]);
    resetWarm();
  });
});

describe("the conversation", () => {
  it("keeps one id while turns keep coming, a new one after 2 minutes without a turn", () => {
    let t = 0;
    let n = 0;
    const c = new Conversation(
      () => t,
      () => "id" + ++n
    );
    expect(c.turn()).toBe("id1");
    t += CONVERSATION_IDLE_MS - 1;
    expect(c.turn()).toBe("id1");
    t += CONVERSATION_IDLE_MS; // exactly the limit: still the same
    expect(c.turn()).toBe("id1");
    t += CONVERSATION_IDLE_MS + 1;
    expect(c.turn()).toBe("id2");
  });

  it("counts the idle time from the answer's end (touch), and reset starts afresh", () => {
    let t = 0;
    let n = 0;
    const c = new Conversation(
      () => t,
      () => "id" + ++n
    );
    c.turn();
    t += CONVERSATION_IDLE_MS; // a long answer
    c.touch();
    t += CONVERSATION_IDLE_MS - 5;
    expect(c.turn()).toBe("id1");
    c.reset();
    expect(c.turn()).toBe("id2");
  });
});

describe("the words", () => {
  it("yes / no answer a confirm; anything else is ordinary text", () => {
    for (const y of ["yes", "Yeah.", "yep", "sure", "Do it", "go ahead", "MindFlock, yes", "yes please", "OK"]) expect(yesNo(y)).toBe(true);
    for (const n of ["no", "Nope", "cancel", "never mind", "no thanks", "no, thanks"]) expect(yesNo(n)).toBe(false);
    for (const x of ["yes delete the other one", "maybe", "open intake", ""]) expect(yesNo(x)).toBeNull();
  });

  it("addressed: the words after MindFlock / Hey MindFlock / Flock", () => {
    expect(addressed("MindFlock, open intake")).toBe("open intake");
    expect(addressed("hey mindflock open intake")).toBe("open intake");
    expect(addressed("Mind flock: what's running?")).toBe("what's running?");
    expect(addressed("Flock, new session")).toBe("new session");
    expect(addressed("flocking birds")).toBeNull();
    expect(addressed("open the flock")).toBeNull();
  });

  it("assistant mode ends on that's all / thanks / goodbye — never on an ordinary request", () => {
    for (const e of ["That's all.", "thats all", "thanks", "Thank you", "thanks MindFlock", "MindFlock, that's all", "that's it", "goodbye"]) expect(isAssistantEnd(e)).toBe(true);
    for (const x of ["thanks for the help, now open intake", "that's all the sessions?", "open settings", "stop the build"]) expect(isAssistantEnd(x)).toBe(false);
  });
});

describe("the talk policy", () => {
  it("reads voice.assistant_talk ('' = brief)", () => {
    expect(talkLevel("")).toBe("brief");
    expect(talkLevel("quiet")).toBe("quiet");
    expect(talkLevel("chatty")).toBe("chatty");
    expect(talkLevel("loud")).toBe("brief");
    expect(talkFromSettings({ voice: { assistant_talk: "quiet" } })).toBe("quiet");
    expect(talkFromSettings(undefined)).toBe("brief");
  });

  it("quiet: questions, confirms, errors only; brief: + answers; chatty: + results; acts never", () => {
    const table: Record<TalkLevel, string[]> = {
      quiet: ["ask", "confirm", "error"],
      brief: ["say", "ask", "confirm", "error"],
      chatty: ["did", "say", "ask", "confirm", "error"],
    };
    for (const level of ["quiet", "brief", "chatty"] as TalkLevel[])
      for (const kind of ["act", "did", "say", "ask", "confirm", "error", "done"] as AssistantEvent["type"][])
        expect([level, kind, speaksKind(kind, level)]).toEqual([level, kind, table[level].includes(kind)]);
  });
});

// --- a whole turn ---------------------------------------------------------------- //

function rig(scripts: AssistantEvent[][], opts: { talk?: TalkLevel; exec?: AssistantDeps["exec"] } = {}) {
  let t = 1_000_000;
  const bodies: AssistantBody[] = [];
  const spoken: string[] = [];
  const asked: string[] = [];
  const focused: string[] = [];
  const execd: UiAction[] = [];
  let chimes = 0;
  const view = new ViewStore();
  const s = new AssistantSession({
    stream: async (body, on) => {
      bodies.push(body);
      for (const e of scripts.shift() || []) on(e);
    },
    snapshot: () => ({ view: "grid", dialog: null }),
    exec: opts.exec || ((a) => (execd.push(a), { ok: true, text: "Did " + a.do })),
    speak: (x) => void spoken.push(x),
    chime: () => void chimes++,
    focus: (title) => void focused.push(title),
    talk: () => opts.talk || "brief",
    asked: (k) => void asked.push(k),
    now: () => t,
    view,
    conversation: new Conversation(
      () => t,
      () => "conv"
    ),
  });
  return {
    s,
    view,
    bodies,
    spoken,
    asked,
    focused,
    execd,
    chimes: () => chimes,
    advance: (ms: number) => void (t += ms),
  };
}

const done: AssistantEvent = { type: "done", ms: 10, model: true };

describe("a turn", () => {
  it("sends the text, the conversation and the snapshot; an act runs at once — a chip and a chime, no words", async () => {
    const r = rig([[{ type: "act", action: { do: "open", target: "intake" } }, done]]);
    await r.s.turn("open intake");
    expect(r.bodies[0]).toEqual({ text: "open intake", conversation: "conv", snapshot: { view: "grid", dialog: null } });
    expect(r.execd).toEqual([{ do: "open", target: "intake" }]);
    expect(r.spoken).toEqual([]);
    expect(r.chimes()).toBe(1);
    expect(r.view.get().chips).toEqual([{ text: "Did open", ok: true, session: undefined, open: undefined }]);
    expect(r.view.get().phase).toBe("off");
  });

  it("chimes once per turn however many things it did", async () => {
    const r = rig([[{ type: "act", action: { do: "close" } }, { type: "act", action: { do: "view", mode: "sky" } }, done]]);
    await r.s.turn("close it and show the sky");
    expect(r.chimes()).toBe(1);
    expect(r.view.get().chips).toHaveLength(2);
  });

  it("a failed act is said (the screen doesn't show why) and captioned as an error", async () => {
    const r = rig([[{ type: "act", action: { do: "click", label: "Nope" } }, done]], { exec: () => ({ ok: false, text: "Nothing on screen called “Nope”" }) });
    await r.s.turn("press nope");
    expect(r.spoken).toEqual(["Nothing on screen called “Nope”"]);
    expect(r.view.get()).toMatchObject({ error: true, caption: "Nothing on screen called “Nope”" });
  });

  it("did new_session focuses the new session; brief keeps it silent, chatty says it", async () => {
    const ev: AssistantEvent = { type: "did", action: { do: "new_session", repo: "web", prompt: "fix" }, ok: true, text: "Started session fix-login", detail: { title: "fix-login" } };
    const r = rig([[ev, done]]);
    await r.s.turn("start a session");
    expect(r.focused).toEqual(["fix-login"]);
    expect(r.spoken).toEqual([]);
    expect(r.chimes()).toBe(1);
    expect(r.view.get().chips[0]).toMatchObject({ text: "Started session fix-login", session: "fix-login" });
    const c = rig([[ev, done]], { talk: "chatty" });
    await c.s.turn("start a session");
    expect(c.spoken).toEqual(["Started session fix-login"]);
  });

  it("a failed did is said once, by the error event that follows it, whatever the talk level", async () => {
    const r = rig(
      [[{ type: "did", action: { do: "setting", path: "auth.token", value: "x" }, ok: false, text: "That setting is off limits", detail: {} }, { type: "error", text: "That setting is off limits" }, done]],
      { talk: "quiet" },
    );
    await r.s.turn("set the token");
    expect(r.spoken).toEqual(["That setting is off limits"]);
    expect(r.view.get().chips).toEqual([]);
    expect(r.view.get().error).toBe(true);
  });

  it("say is spoken at brief, only captioned (and chimed) at quiet", async () => {
    const r = rig([[{ type: "say", text: "Two sessions need you." }, done]]);
    await r.s.turn("what needs me");
    expect(r.spoken).toEqual(["Two sessions need you."]);
    expect(r.view.get().caption).toBe("Two sessions need you.");
    const q = rig([[{ type: "say", text: "Two sessions need you." }, done]], { talk: "quiet" });
    await q.s.turn("what needs me");
    expect(q.spoken).toEqual([]);
    expect(q.view.get().caption).toBe("Two sessions need you.");
  });

  it("an error is spoken even when quiet", async () => {
    const r = rig([[{ type: "error", text: "MindFlock couldn't answer that." }, done]], { talk: "quiet" });
    await r.s.turn("hm");
    expect(r.spoken).toEqual(["MindFlock couldn't answer that."]);
    expect(r.view.get().error).toBe(true);
  });

  it("ask: spoken, the mic is asked to open, and the next phrase is the assistant's in the same conversation", async () => {
    const r = rig([[{ type: "ask", text: "Which repo?" }, done], [{ type: "say", text: "OK." }, done]]);
    await r.s.turn("start a session");
    expect(r.spoken).toEqual(["Which repo?"]);
    expect(r.asked).toEqual(["ask"]);
    expect(r.s.wantsNext()).toBe(true);
    expect(r.view.get().question).toEqual({ kind: "ask", text: "Which repo?" });
    r.advance(5000);
    await r.s.turn("the web app");
    expect(r.bodies[1]).toMatchObject({ text: "the web app", conversation: "conv" });
    expect(r.bodies[1].confirm).toBeUndefined();
    expect(r.s.wantsNext()).toBe(false);
  });

  it("confirm: a yes goes back as confirm {id, yes: true}; the act it allows runs", async () => {
    const r = rig([
      [{ type: "confirm", id: "k1", text: "Delete fix-login?", action: { do: "session_action", session: "fix-login", action: "delete" } }, done],
      [{ type: "act", action: { do: "session_action", session: "fix-login", action: "delete" } }, done],
    ]);
    await r.s.turn("delete fix-login");
    expect(r.spoken).toEqual(["Delete fix-login?"]);
    expect(r.s.confirmId()).toBe("k1");
    expect(r.view.get().question?.kind).toBe("confirm");
    await r.s.turn("yes");
    expect(r.bodies[1]).toMatchObject({ text: "yes", confirm: { id: "k1", yes: true } });
    expect(r.execd).toEqual([{ do: "session_action", session: "fix-login", action: "delete" }]);
    expect(r.s.wantsNext()).toBe(false);
  });

  it("confirm: no → yes:false; the Yes / No buttons answer the same way", async () => {
    const c: AssistantEvent = { type: "confirm", id: "k2", text: "Merge it?", action: { do: "session_action", session: "a", action: "merge" } };
    const r = rig([[c, done], [{ type: "say", text: "Left it." }, done], [c, done], [done]]);
    await r.s.turn("merge a");
    await r.s.turn("no");
    expect(r.bodies[1].confirm).toEqual({ id: "k2", yes: false });
    await r.s.turn("merge a");
    await r.s.answer(true);
    expect(r.bodies[3]).toMatchObject({ text: "yes", confirm: { id: "k2", yes: true } });
  });

  it("confirm: anything that isn't yes/no is ordinary text, and the confirm is dropped", async () => {
    const r = rig([[{ type: "confirm", id: "k3", text: "Delete it?", action: { do: "close" } }, done], [done], [done]]);
    await r.s.turn("delete it");
    await r.s.turn("actually open settings");
    expect(r.bodies[1].confirm).toBeUndefined();
    expect(r.s.confirmId()).toBeNull();
    await r.s.turn("yes");
    expect(r.bodies[2].confirm).toBeUndefined();
  });

  it("a pending question lapses after the server's 60 s", async () => {
    const r = rig([[{ type: "confirm", id: "k4", text: "Sure?", action: { do: "close" } }, done], [done]]);
    await r.s.turn("x");
    r.advance(CONFIRM_TTL_MS + 1);
    expect(r.s.wantsNext()).toBe(false);
    expect(r.view.get().question).toBeNull();
    await r.s.turn("yes");
    expect(r.bodies[1].confirm).toBeUndefined();
  });

  it("a new phrase cuts the turn in flight: the old one's late events are not acted on", async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((res) => (release = res));
    const execd: UiAction[] = [];
    const s = new AssistantSession({
      stream: async (body, on, signal) => {
        if (body.text === "first") {
          await gate;
          if (!signal.aborted) on({ type: "act", action: { do: "close" } });
          on({ type: "act", action: { do: "view", mode: "sky" } });
          return;
        }
        on({ type: "act", action: { do: "view", mode: "grid" } });
      },
      snapshot: () => ({}),
      exec: (a) => (execd.push(a), { ok: true, text: "" }),
      speak: () => {},
      chime: () => {},
      focus: () => {},
      talk: () => "brief",
      view: new ViewStore(),
    });
    const first = s.turn("first");
    expect(s.busy).toBe(true);
    await s.turn("second");
    release();
    await first;
    expect(execd).toEqual([{ do: "view", mode: "grid" }]);
  });

  it("events act in arrival order even when an executor awaits", async () => {
    const order: string[] = [];
    const r = rig(
      [
        [
          { type: "act", action: { do: "open", target: "new-session" } },
          { type: "act", action: { do: "type", text: "fix the login" } },
          done,
        ],
      ],
      {
        exec: async (a) => {
          if (a.do === "open") await new Promise((res) => setTimeout(res, 5));
          order.push(a.do);
          return { ok: true, text: a.do };
        },
      }
    );
    await r.s.turn("new session to fix the login");
    expect(order).toEqual(["open", "type"]);
  });

  it("the view: mode on shows the strip listening; thinking during a turn; mode off hides it", async () => {
    const r = rig([[done]]);
    expect(stripVisible(r.view.get())).toBe(false);
    r.s.setMode(true);
    expect(r.view.get()).toMatchObject({ mode: true, phase: "listening" });
    expect(stripVisible(r.view.get())).toBe(true);
    r.s.hearing("open in");
    expect(r.view.get().heard).toBe("open in");
    const p = r.s.turn("open intake");
    expect(r.view.get().phase).toBe("thinking");
    await p;
    expect(r.view.get().phase).toBe("listening");
    r.s.setMode(false);
    r.view.clear();
    expect(stripVisible(r.view.get())).toBe(false);
  });

  it("an empty phrase sends nothing", async () => {
    const r = rig([]);
    await r.s.turn("   ");
    expect(r.bodies).toEqual([]);
  });
});
