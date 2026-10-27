/** "Over" sends, "over and out" stops, and the mic says so — pinned:
 *  - continuous dictation: a click keys the mic once; "over" sends the
 *    message and listening goes on (the next phrase is a new message, past the
 *    router again), "over and out" sends what is pending and stops, a stop
 *    phrase / Esc / letting go stop without sending; "submit" is a word;
 *  - spoken confirmations ("Sent.", "Sent. Over and out.", "Over and out.",
 *    "Cleared.", "Stopped.", a switch's line): the queue's front, said while
 *    the mic listens (held), stale after 5 s, the recorder muted for them,
 *    their audio cached per voice and made ahead only for a local voice;
 *  - the wiring in TopBarMic and Settings → Voice, and the copy. */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  CONFIRM_LINES,
  CONFIRM_STALE_MS,
  Confirmer,
  DICT_HINT,
  LiveDictation,
  SpeechQueue,
  answerHint,
  localVoice,
  serverSpeaker,
  type ConfirmKind,
  type DictTarget,
  type LiveHooks,
  type Speaker,
  type VoiceCfg,
} from "../lib/voiceDesktop";
import { SRC, code } from "./sourceText";

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

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  mocks.apiRaw.mockReset();
  mocks.instApi.mockReset();
});

/** A session's terminal as a target: what it was sent, in order. */
function session(title = "t1", name = "Login fix"): DictTarget & { keys: string[] } {
  const keys: string[] = [];
  return { kind: "session", title, name, keys, type: (t) => keys.push(t) > 0, erase: (t) => keys.push("⌫" + t) > 0 };
}

function hooks(over: Partial<LiveHooks> = {}) {
  const said: ConfirmKind[] = [];
  const routed: string[] = [];
  const ends: string[] = [];
  const captions: string[] = [];
  let halts = 0;
  const h: LiveHooks = {
    caption: (t) => void captions.push(t),
    halt: () => void halts++,
    route: async (t) => (routed.push(t), false),
    activity: () => "idle",
    replies: () => false,
    submitted: () => {},
    done: (how) => void ends.push(how),
    confirm: (k) => void said.push(k),
    ...over,
  };
  return { h, said, routed, ends, captions, halts: () => halts };
}

describe("continuous dictation: “over” sends and listening goes on", () => {
  beforeEach(() => mocks.instApi.mockResolvedValue({ ok: true }));

  it("two messages in one listen, each sent on “over”; “over and out” sends the third and stops", async () => {
    const t = session();
    const k = hooks();
    const d = new LiveDictation(t, k.h, { continuous: true });
    await d.heard("fix the login bug over");
    expect(t.keys).toEqual(["fix the login bug "]);
    expect(mocks.instApi).toHaveBeenCalledTimes(1);
    expect(mocks.instApi).toHaveBeenLastCalledWith("t1", "/send", { json: { enter: true, say: false } });
    expect(d.active).toBe(true);
    await d.heard("then add a test over");
    expect(t.keys).toEqual(["fix the login bug ", "then add a test "]);
    expect(mocks.instApi).toHaveBeenCalledTimes(2);
    expect(d.active).toBe(true);
    // Each message is a new one: its first phrase goes past the router.
    expect(k.routed).toEqual(["fix the login bug", "then add a test"]);
    await d.heard("and push it over and out");
    expect(mocks.instApi).toHaveBeenCalledTimes(3);
    expect(d.active).toBe(false);
    expect(k.halts()).toBe(1);
    expect(k.said).toEqual(["sent", "sent", "out"]);
  });

  it("“disregard” takes back only the message under way", async () => {
    const t = session();
    const k = hooks();
    const d = new LiveDictation(t, k.h, { continuous: true });
    await d.heard("first over");
    await d.heard("oops");
    await d.heard("disregard");
    expect(t.keys).toEqual(["first ", "oops ", "⌫oops "]);
    expect(d.active).toBe(true);
    expect(k.said).toEqual(["sent", "cleared"]);
  });

  it("“over and out” with nothing pending sends nothing and stops (“Over and out.”)", async () => {
    const k = hooks();
    const d = new LiveDictation(session(), k.h, { continuous: true });
    await d.heard("ship it over");
    await d.heard("over and out");
    expect(mocks.instApi).toHaveBeenCalledTimes(1);
    expect(d.active).toBe(false);
    expect(k.said).toEqual(["sent", "bye"]);
    expect(k.ends).toEqual(["stopped"]);
  });

  it("“over and out, that's all” sends — the stop phrase does not swallow it", async () => {
    const k = hooks();
    const d = new LiveDictation(session(), k.h, { continuous: true });
    await d.heard("ship it over and out, that's all");
    expect(mocks.instApi).toHaveBeenCalledTimes(1);
    expect(d.active).toBe(false);
    expect(k.said).toEqual(["out"]);
  });

  it("a stop phrase, Esc and letting go stop without sending", async () => {
    for (const how of ["stop", "esc", "finish"] as const) {
      mocks.instApi.mockClear();
      const t = session();
      const k = hooks();
      const d = new LiveDictation(t, k.h, { continuous: true });
      await d.heard("half a thought");
      if (how === "stop") await d.heard("stop recording");
      else if (how === "esc") await d.cancel();
      else await d.finish();
      expect(mocks.instApi).not.toHaveBeenCalled();
      expect(d.active).toBe(false);
      expect(k.said).toEqual(how === "stop" ? ["stopped"] : []);
    }
  });

  it("“submit” is typed as a word; nothing is sent", async () => {
    const t = session();
    const d = new LiveDictation(t, hooks().h, { continuous: true });
    await d.heard("please submit");
    expect(t.keys).toEqual(["please submit "]);
    expect(mocks.instApi).not.toHaveBeenCalled();
    expect(d.active).toBe(true);
  });

  it("a failed send stops listening (the words stay in the field); nothing is confirmed", async () => {
    mocks.instApi.mockRejectedValueOnce(new mocks.ApiError(409, "in a dialog", { in_dialog: true }));
    const k = hooks();
    const d = new LiveDictation(session(), k.h, { continuous: true });
    await d.heard("yes over");
    expect(d.active).toBe(false);
    expect(k.said).toEqual([]);
    expect(k.captions.at(-1)).toBe("Login fix is on a dialog — press Enter yourself when it is answered");
  });

  it("the router acting goes on listening (the next message goes where retarget says); “end” stops", async () => {
    const t1 = session("t1");
    const t2 = session("t2", "Billing");
    const k = hooks({ route: async (t) => t.startsWith("switch"), retarget: () => ({ target: t2 }) });
    const d = new LiveDictation(t1, k.h, { continuous: true });
    await d.heard("switch to billing");
    expect(d.active).toBe(true);
    await d.heard("fix the totals over");
    expect(t2.keys).toEqual(["fix the totals "]);
    expect(t1.keys).toEqual([]);
    expect(mocks.instApi).toHaveBeenLastCalledWith("t2", "/send", { json: { enter: true, say: false } });
    const k2 = hooks({ route: async () => "end" });
    const d2 = new LiveDictation(session(), k2.h, { continuous: true });
    await d2.heard("stop");
    expect(d2.active).toBe(false);
  });

  it("not continuous (a hands-free answer): “over” sends and stops, confirmed “Sent.”", async () => {
    const k = hooks({ replies: () => true });
    const d = new LiveDictation(session(), k.h);
    await d.heard("use sqlite over");
    expect(mocks.instApi).toHaveBeenLastCalledWith("t1", "/send", { json: { enter: true, say: true } });
    expect(d.active).toBe(false);
    expect(k.said).toEqual(["sent"]);
  });
});

describe("the speech queue's confirmations", () => {
  /** An engine that records what it says and finishes on `end()`. */
  function engine() {
    const said: string[] = [];
    let finish: (() => void) | null = null;
    const sp: Speaker = {
      speak: (t) => {
        said.push(t);
        return new Promise<void>((r) => (finish = r));
      },
      cancel: () => finish?.(),
    };
    return { sp, said, end: async () => (finish?.(), await Promise.resolve(), await Promise.resolve()) };
  }

  it("go to the front — after the confirmations already there — never behind replies", async () => {
    const e = engine();
    const q = new SpeechQueue(() => e.sp);
    q.hold(); // the mic listens
    void q.say("A long reply about the login fix.");
    void q.say("Another reply.");
    void q.confirmLine("Sent.");
    void q.confirmLine("Cleared.");
    expect(e.said).toEqual(["Sent."]); // said while held
    await e.end();
    expect(e.said).toEqual(["Sent.", "Cleared."]);
    await e.end();
    expect(e.said).toEqual(["Sent.", "Cleared."]); // the replies wait for release
    q.release();
    expect(e.said).toEqual(["Sent.", "Cleared.", "A long reply about the login fix."]);
  });

  it("one more than 5 s old by its turn is dropped; `start` runs as it plays", async () => {
    let now = 1000;
    const e = engine();
    const q = new SpeechQueue(() => e.sp, () => {}, { now: () => now, log: () => {} });
    void q.say("A reply being read.");
    const started: string[] = [];
    const late = q.confirmLine("Sent.", { start: () => started.push("late") });
    now += CONFIRM_STALE_MS + 1;
    await e.end();
    await late;
    expect(e.said).toEqual(["A reply being read."]);
    expect(started).toEqual([]);
    void q.confirmLine("Cleared.", { start: () => started.push("fresh") });
    expect(e.said.at(-1)).toBe("Cleared.");
    expect(started).toEqual(["fresh"]);
  });
});

describe("Confirmer: the switch, the mute, the made-ahead audio", () => {
  function rig(o: { on?: boolean; server?: boolean; local?: boolean; lastError?: string } = {}) {
    const mutes: boolean[] = [];
    const lines: string[] = [];
    const prefetched: string[][] = [];
    let start: (() => void) | undefined;
    let done: () => void = () => {};
    const queue = {
      confirmLine: (line: string, opts: { start?: () => void } = {}) => {
        lines.push(line);
        start = opts.start;
        return new Promise<void>((r) => (done = r));
      },
    };
    const speaker = { lastError: o.lastError || "", prefetch: async (ls: readonly string[]) => void prefetched.push([...ls]) };
    const c = new Confirmer({
      queue,
      on: () => o.on !== false,
      mic: () => ({ mute: (b: boolean) => void mutes.push(b) }),
      server: () => o.server !== false,
      local: () => !!o.local,
      speaker,
    });
    return { c, mutes, lines, prefetched, play: () => start?.(), finish: () => done() };
  }

  it("says the kind's line; the mic is muted from its start to its end", async () => {
    const r = rig({ local: true });
    const p = r.c.say("sent");
    expect(r.lines).toEqual([CONFIRM_LINES.sent]);
    expect(r.mutes).toEqual([]);
    r.play();
    expect(r.mutes).toEqual([true]);
    r.finish();
    await p;
    expect(r.mutes).toEqual([true, false]);
    void r.c.say({ line: "Now talking to Billing." });
    expect(r.lines.at(-1)).toBe("Now talking to Billing.");
  });

  it("dropped before it played: the mic was never muted", async () => {
    const r = rig();
    const p = r.c.say("cleared");
    r.finish();
    await p;
    expect(r.mutes).toEqual([]);
  });

  it("switched off: silent", async () => {
    const r = rig({ on: false, local: true });
    await r.c.say("sent");
    r.c.prime();
    expect(r.lines).toEqual([]);
    expect(r.prefetched).toEqual([]);
  });

  it("made ahead once for a local voice; never for another until a line was spoken without an error; never for the browser's", async () => {
    const local = rig({ local: true });
    local.c.prime();
    local.c.prime();
    expect(local.prefetched).toEqual([Object.values(CONFIRM_LINES)]);
    expect(Object.values(CONFIRM_LINES)).toEqual(["Sent.", "Sent. Over and out.", "Over and out.", "Cleared.", "Stopped."]);

    const paid = rig({ local: false });
    paid.c.prime();
    expect(paid.prefetched).toEqual([]);
    const p = paid.c.say("sent");
    paid.play();
    paid.finish();
    await p;
    expect(paid.prefetched).toHaveLength(1);

    const failing = rig({ local: false, lastError: "HTTP 502" });
    const f = failing.c.say("sent");
    failing.play();
    failing.finish();
    await f;
    expect(failing.prefetched).toEqual([]);

    const browser = rig({ server: false, local: true });
    browser.c.prime();
    expect(browser.prefetched).toEqual([]);
  });

  it("a local voice: built in, or an OpenAI-shaped host on a local address; a paid API is not", () => {
    const cfg = (tts_backend: string, tts_detail = ""): VoiceCfg => ({
      stt: "server",
      tts: "server",
      server_ready: { tts_backend, tts_detail },
      system: {},
    });
    expect(localVoice(cfg("local", "piper en_US-amy"))).toBe(true);
    expect(localVoice(cfg("system"))).toBe(true);
    expect(localVoice(cfg("openai", "voicelab at localhost:8020"))).toBe(true);
    expect(localVoice(cfg("openai", "tts-1 at 192.168.1.20:8000"))).toBe(true);
    expect(localVoice(cfg("openai", "kokoro at mini.tail1234.ts.net"))).toBe(true);
    expect(localVoice(cfg("openai", "tts-1 at api.openai.com"))).toBe(false);
    expect(localVoice(cfg(""))).toBe(false);
  });
});

describe("the server speaker keeps a confirmation's audio per voice", () => {
  const requests: string[] = [];
  class Ctx {
    state = "running";
    currentTime = 0;
    destination = {};
    resume() {
      return Promise.resolve();
    }
    decodeAudioData(b: { text: string }) {
      return Promise.resolve({ duration: 0, text: b.text });
    }
    createBufferSource() {
      const node = {
        buffer: null,
        onended: null as null | (() => void),
        connect() {},
        start() {
          setTimeout(() => node.onended?.(), 0);
        },
        stop() {},
      };
      return node;
    }
  }

  beforeEach(() => {
    requests.length = 0;
    vi.stubGlobal("window", { AudioContext: Ctx });
    mocks.apiRaw.mockImplementation(async (_url: string, init: RequestInit) => {
      const text = JSON.parse(String(init.body)).text as string;
      requests.push(text);
      return { ok: true, status: 200, arrayBuffer: async () => ({ text }) };
    });
  });

  const fallback: Speaker = { speak: async () => {}, cancel: () => {} };

  it("the second “Sent.” makes no request; another voice, or a reply, does", async () => {
    let voice = "local|amy";
    const sp = serverSpeaker(fallback, { voiceKey: () => voice });
    await sp.speak("Sent.");
    await sp.speak("Sent.");
    expect(requests).toEqual(["Sent."]);
    expect(sp.cached("Sent.")).toBe(true);
    await sp.speak("The tests all pass now.");
    await sp.speak("The tests all pass now.");
    expect(requests).toEqual(["Sent.", "The tests all pass now.", "The tests all pass now."]);
    voice = "openai|harvey at localhost:8020";
    await sp.speak("Sent.");
    expect(requests.at(-1)).toBe("Sent.");
    expect(requests).toHaveLength(4);
  });

  it("prefetch fills the cache: the five lines then speak without a request", async () => {
    const sp = serverSpeaker(fallback, { voiceKey: () => "local|amy" });
    await sp.prefetch(Object.values(CONFIRM_LINES));
    const made = requests.length;
    expect(made).toBeGreaterThan(0);
    for (const line of Object.values(CONFIRM_LINES)) await sp.speak(line);
    expect(requests).toHaveLength(made);
    await sp.prefetch(Object.values(CONFIRM_LINES));
    expect(requests).toHaveLength(made); // already there: nothing asked again
  });

  it("a failed synthesis is not kept", async () => {
    mocks.apiRaw.mockImplementationOnce(async () => ({ ok: false, status: 502, json: async () => ({ error: "down" }) }));
    const sp = serverSpeaker(fallback);
    await sp.speak("Cleared.");
    expect(sp.cached("Cleared.")).toBe(false);
    await sp.speak("Cleared.");
    expect(sp.cached("Cleared.")).toBe(true);
  });
});

describe("the wiring and the copy", () => {
  const MIC = code("components/TopBarMic.tsx");

  it("a click (or the wake word) listens with no idle timeout; Hold to talk and a hands-free answer keep it", () => {
    expect(MIC).toMatch(/const continuous = opts\.continuous !== false;/);
    expect(MIC).toMatch(/idles\.current = opts\.idle \?\? \(!continuous \|\| prefsRef\.current\.ptt === "hold"\);/);
    expect(MIC).toMatch(/if \(idles\.current\) idleTimer\.current = setTimeout/);
    expect(MIC).toMatch(/title,\s*hint,\s*continuous: false,/); // listenFor
    expect(MIC).toMatch(/\{ hint: WAKE_HINT, skipMs: CHIME_MS \+ 30, idle: false \}/);
    expect(MIC).toMatch(/\{ hint: opts\.hint, continuous \}/);
  });

  it("the confirmations are wired: the dictation's, a switch's line, made ahead on a gesture", () => {
    expect(MIC).toMatch(/confirm: \(kind\) => void confirmer\.current\?\.say\(kind\)/);
    expect(MIC).toMatch(/void confirmer\.current\?\.say\(\{ line \}\)/);
    expect(MIC).toMatch(/confirmer\.current\?\.prime\(\);/);
    expect(MIC).toMatch(/mic: \(\) => rec\.current,/);
    expect(MIC).toMatch(/serverSpeaker\(browserSp, \{ voiceKey: \(\) => ttsVoiceKey\(cfgNow\) \}\)/);
  });

  it("Settings → Voice has the switch (on unless saved off); the mic's popover does not", () => {
    const voice = code("components/settings/screens/Voice.tsx");
    expect(voice).toContain('id="voice-confirm"');
    expect(voice).toMatch(/checked=\{prefs\.confirm !== false\}/);
    expect(voice).toMatch(/save\(\{ \.\.\.prefs, confirm: e\.target\.checked \}\)/);
    expect(MIC).not.toMatch(/confirm: e\.target\.checked|id="mic-confirm/);
  });

  it("the hints name “over”, “over and out” and “disregard”", () => {
    expect(DICT_HINT).toBe("Say “over” to send, “over and out” to stop, “disregard” to start over");
    expect(answerHint("Bot")).toBe("Answer Bot — say “over” to send, “over and out” to stop, “disregard” to start over");
  });

  it("no user-facing string says “submit” as a radio word (a select dialog's review keeps its own)", () => {
    const hits: string[] = [];
    for (const path of Object.keys(SRC)) {
      const body = code(path);
      for (const m of body.matchAll(/["'`][^"'`\n]*\bsay(?:s)?\b[^"'`\n]*\bsubmit\b[^"'`\n]*["'`]/gi)) hits.push(path + ": " + m[0]);
      for (const m of body.matchAll(/“submit”|submit to send/gi)) hits.push(path + ": " + m[0]);
    }
    // The router's own word on Claude's question prompt's review step.
    expect(hits.filter((h) => !/Say submit, or cancel\./.test(h))).toEqual([]);
  });
});
