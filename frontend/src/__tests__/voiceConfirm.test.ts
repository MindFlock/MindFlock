/** "Over" sends, "over and out" stops, and the mic says so — pinned:
 *  - continuous dictation: a click keys the mic once; "over" sends the
 *    message and listening goes on (the next phrase is a new message, past the
 *    router again), "over and out" sends what is pending and stops, a stop
 *    phrase / Esc / letting go stop without sending; "submit" is a word;
 *  - spoken confirmations ("Sent.", "Sent. Over and out.", "Over and out.",
 *    "Cleared.", "Stopped.", a switch's line): the queue's front, said while
 *    the mic listens (held), stale after 5 s, the recorder muted for them,
 *    their audio cached per voice and made ahead only for a local voice;
 *  - snappy control words: one at the end of the GUESS acts after
 *    CONTROL_TAIL_MS of quiet (the phrase ended at the recognizer at once);
 *    "over" waits for /end's accurate final up to OVER_FINAL_WAIT_MS (less
 *    once the finals' round trip is known — voiceSnappy.test.ts);
 *  - the wiring in TopBarMic and Settings → Voice, and the copy. */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  GATE_RETRY_MS,
  MID_SENTENCE_MS,
  CONFIRM_LINES,
  CONFIRM_STALE_MS,
  CONTROL_TAIL_MS,
  CUE_GAP_MS,
  OVER_FINAL_WAIT_MS,
  finalClock,
  isSwitchCommand,
  sentLine,
  Confirmer,
  dictHint,
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
import { CHIME_MS, STOP_CHIME_MS } from "../lib/wakeWord";
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
    expect(MIC).toMatch(/\{ hint: WAKE_HINT, idle: false \}/);
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
    expect(dictHint()).toBe("Say “over” to send, “over and out” to stop, “disregard” to start over");
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

describe("snappy control words: acted on from the guess after a moment's quiet", () => {
  let quiet = 0;
  let ends = 0;
  const sentIn: number[] = [];
  beforeEach(() => {
    vi.useFakeTimers();
    quiet = 0;
    ends = 0;
    sentIn.length = 0;
    finalClock.reset(); // no round trip known: "over" waits the most
    mocks.instApi.mockResolvedValue({ ok: true });
  });
  const fast = (over: Partial<LiveHooks> = {}) =>
    hooks({ quietMs: () => quiet, endPhrase: () => void ends++, sentIn: (ms) => void sentIn.push(ms), ...over });

  it("“disregard” at the end of the guess: nothing until 250 ms of quiet, then at once — its late end ignored", async () => {
    expect(CONTROL_TAIL_MS).toBe(250);
    const t = session();
    const k = fast();
    const d = new LiveDictation(t, k.h, { continuous: true });
    await d.heard("keep this");
    await d.stream("", "oops disregard");
    expect(t.keys).toEqual(["keep this ", "oops"]); // the radio word is held back
    quiet = 100;
    await vi.advanceTimersByTimeAsync(200);
    expect(t.keys).toHaveLength(2);
    expect(ends).toBe(0);
    quiet = CONTROL_TAIL_MS;
    await vi.advanceTimersByTimeAsync(50);
    expect(ends).toBe(1); // the recognizer's phrase ended at once
    expect(t.keys.at(-1)).toBe("⌫keep this oops");
    expect(k.said).toEqual(["cleared"]);
    // The recognizer's own end of that phrase changes nothing.
    await d.flush("oops disregard");
    expect(t.keys).toHaveLength(3);
    expect(d.active).toBe(true);
    // The next phrase is heard as usual.
    await d.flush("fresh start");
    expect(t.keys.at(-1)).toBe("fresh start ");
  });

  it("a stop phrase in the guess stops at once; nothing is sent", async () => {
    const t = session();
    const k = fast();
    const d = new LiveDictation(t, k.h, { continuous: true });
    await d.stream("", "half a thought stop recording");
    quiet = 300;
    await vi.advanceTimersByTimeAsync(50);
    expect(d.active).toBe(false);
    expect(ends).toBe(1);
    expect(mocks.instApi).not.toHaveBeenCalled();
    expect(k.said).toEqual(["stopped"]);
  });

  it("a held word the next word rules out goes in, and nothing fires", async () => {
    const t = session();
    const d = new LiveDictation(t, fast().h, { continuous: true });
    await d.stream("", "move it over");
    expect(t.keys).toEqual(["move it"]);
    await d.stream("", "move it over here");
    expect(t.keys).toEqual(["move it", " over here"]);
    quiet = 1000;
    await vi.advanceTimersByTimeAsync(500);
    expect(ends).toBe(0);
    expect(mocks.instApi).not.toHaveBeenCalled();
  });

  it("“over”: the phrase ends at once and Enter goes as /end answers — its accurate final retyped first", async () => {
    const t = session();
    const k = fast();
    const d = new LiveDictation(t, k.h, { continuous: true });
    await d.stream("", "run the test over");
    quiet = 300;
    await vi.advanceTimersByTimeAsync(50);
    expect(ends).toBe(1);
    expect(mocks.instApi).not.toHaveBeenCalled(); // waiting for /end
    await vi.advanceTimersByTimeAsync(600);
    const flushed = d.flush("", "run the tests over");
    await vi.advanceTimersByTimeAsync(300); // the keys settle before Enter
    await flushed;
    expect(t.keys).toEqual(["run the test", "s "]); // the final's "tests": the guess corrected
    expect(mocks.instApi).toHaveBeenCalledTimes(1);
    expect(k.said).toEqual(["sent"]);
    expect(sentIn).toHaveLength(1);
    expect(sentIn[0]).toBeGreaterThanOrEqual(900);
    expect(d.active).toBe(true);
    // No second send when the timer would have run out.
    await vi.advanceTimersByTimeAsync(OVER_FINAL_WAIT_MS);
    expect(mocks.instApi).toHaveBeenCalledTimes(1);
  });

  it("“over”: no /end within 1.5 s — the fast words go, and the late final is ignored", async () => {
    expect(OVER_FINAL_WAIT_MS).toBe(1500);
    const t = session();
    const k = fast();
    const d = new LiveDictation(t, k.h, { continuous: true });
    await d.stream("", "ship it over");
    quiet = 300;
    await vi.advanceTimersByTimeAsync(50);
    await vi.advanceTimersByTimeAsync(OVER_FINAL_WAIT_MS - 100);
    expect(mocks.instApi).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(400);
    expect(mocks.instApi).toHaveBeenCalledTimes(1);
    expect(t.keys).toEqual(["ship it"]); // no trailing space before Enter (nothing settles)
    await d.flush("", "ship it over and over");
    expect(t.keys).toHaveLength(1);
    expect(mocks.instApi).toHaveBeenCalledTimes(1);
  });

  it("a switch command as a message's first phrase goes to the router from the guess", async () => {
    const routed: string[] = [];
    const k = fast({ route: async (t) => (routed.push(t), true) });
    const d = new LiveDictation(session(), k.h, { continuous: true });
    await d.stream("", "switch to billing");
    expect(routed).toEqual([]);
    quiet = 300;
    await vi.advanceTimersByTimeAsync(50);
    expect(routed).toEqual(["switch to billing"]);
    expect(d.active).toBe(true);
    expect(isSwitchCommand("switch to the login fix")).toBe(true);
    expect(isSwitchCommand("focus 3")).toBe(true);
    expect(isSwitchCommand("next session")).toBe(true);
    expect(isSwitchCommand("session three")).toBe(true);
    expect(isSwitchCommand("switch to")).toBe(false);
    expect(isSwitchCommand("switch the flag")).toBe(false);
    expect(isSwitchCommand("go ahead")).toBe(false);
  });

  it("without a quiet clock (hands-free tests, Web Speech without a level) nothing acts from the guess", async () => {
    const t = session();
    const d = new LiveDictation(t, hooks().h, { continuous: true });
    await d.stream("", "oops disregard");
    await vi.advanceTimersByTimeAsync(5000);
    expect(t.keys).toEqual(["oops"]);
  });

  it("diagnostics: “over → sent 0.9 s”", () => {
    expect(sentLine(900)).toBe("over → sent 0.9 s");
    expect(sentLine(null)).toBe("");
    const mic = code("components/TopBarMic.tsx");
    expect(mic).toMatch(/wakeLine\(lastWakeMs\), sentLine\(lastSentMs\)/);
    expect(mic).toMatch(/endPhrase: \(\) => \{\s*if \(live\.current === d\) rec\.current\?\.endPhrase\?\.\(\);/);
    expect(mic).toMatch(/quietMs: \(\) => \{\s*const at = lastLoudAt\(\);/);
  });
});

describe("the mic's chimes: rising at every start, falling at every stop", () => {
  const MIC = code("components/TopBarMic.tsx");
  const fnBody = (name: string, until: string) => MIC.slice(MIC.indexOf(name), MIC.indexOf(until, MIC.indexOf(name)));

  it("every start chimes: start() itself, after the recognizer started (a click, the hotkey, the wake, the ask card's mic all go through it)", () => {
    const start = fnBody("const start = (", "const listenFor");
    expect(start).toMatch(/if \(!ok\) \{\s*halt\(\);[^}]*\}\s*sound\("start"\);\s*chimed\.current = true;/);
    // The only chime() call is the helper's: no path plays its own.
    expect(MIC.match(/\bchime\(/g)).toHaveLength(1);
    expect(fnBody("const woken = ", "const latestWake")).not.toMatch(/chime|sound\(/);
    // Its sound is skipped from the recording.
    expect(start).toMatch(/\{ skipMs: Math\.max\(skipMs, chimeSkip\) \}/);
  });

  it("every stop chimes: halt() — where over and out, a stop phrase, Esc, the button, letting go, the idle stop and a refused send all end — only after a start chimed", () => {
    expect(MIC).toMatch(/const halt = \(\) => \{[^}]*if \(chimed\.current\) \{\s*chimed\.current = false;[^}]*sound\("stop"\);\s*\}/);
    // Before the queue releases the replies.
    const halt = fnBody("const halt = () => {", "const target = (");
    expect(halt.indexOf('sound("stop")')).toBeLessThan(halt.indexOf("queue.release()"));
  });

  it("“over” (send, keep listening) never ends the dictation, so no chime", async () => {
    mocks.instApi.mockResolvedValue({ ok: true });
    const k = hooks();
    const d = new LiveDictation(session(), k.h, { continuous: true });
    await d.heard("ship it over");
    expect(k.halts()).toBe(0);
    expect(d.active).toBe(true);
  });

  it("never over a line being said (in the speech queue's order, its own length); the Sounds switch off: none", () => {
    const sound = fnBody("const sound = (", "const halt = ");
    expect(sound).toMatch(/if \(prefsRef\.current\.sounds === false\) return;/);
    expect(sound).toMatch(
      /void queue\.cue\(\(\) => chime\(voiceAudioCtx\(\), kind\), kind === "stop" \? STOP_CHIME_MS : CHIME_MS\);/
    );
    expect(sound).not.toMatch(/speakingListeners/);
    // …with the breath the cue waits when something is queued (SpeechQueue.cueLead).
    expect(MIC).toMatch(/const chimeSkip = prefsRef\.current\.sounds !== false \? queue\.cueLead \+ CHIME_MS \+ 30 : 0;/);
    const voice = code("components/settings/screens/Voice.tsx");
    expect(voice).toContain('id="voice-sounds"');
    expect(voice).toMatch(/checked=\{prefs\.sounds !== false\}/);
    expect(voice).toMatch(/save\(\{ \.\.\.prefs, sounds: e\.target\.checked \}\)/);
  });
});

describe("continuous listening: replies play in the person's pauses, the recorder muted", () => {
  let quiet = 0;
  beforeEach(() => {
    vi.useFakeTimers();
    quiet = 0;
  });
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
    return { sp, said, end: () => finish?.() };
  }

  it("a reply queued while listening plays in the quiet gap — not after “over and out” — and waits while the person is mid-sentence", async () => {
    mocks.instApi.mockResolvedValue({ ok: true });
    const e = engine();
    const speaking: boolean[] = [];
    const q = new SpeechQueue(() => e.sp, (on) => void speaking.push(on));
    const k = hooks({ quietMs: () => quiet });
    const d = new LiveDictation(session(), k.h, { continuous: true });
    // As TopBarMic.start: the barge-in's hold, then the gate instead of it.
    q.hold();
    q.setGate(() => !d.midSentence());
    q.release();
    // Just keyed: a moment's grace before anything is said.
    void q.say("Login fix: the tests pass.");
    expect(e.said).toEqual([]);
    await vi.advanceTimersByTimeAsync(MID_SENTENCE_MS + GATE_RETRY_MS);
    expect(e.said).toEqual(["Login fix: the tests pass."]); // the mic still listening
    expect(d.active).toBe(true);
    e.end();
    await vi.advanceTimersByTimeAsync(0);
    // Mid-sentence: the next line waits for the 250 ms pause.
    await d.stream("", "now please also");
    void q.say("Billing: done.");
    await vi.advanceTimersByTimeAsync(200);
    expect(e.said).toHaveLength(1);
    quiet = 260;
    await vi.advanceTimersByTimeAsync(GATE_RETRY_MS);
    expect(e.said).toEqual(["Login fix: the tests pass.", "Billing: done."]);
    expect(speaking).toEqual([true, false, true]);
    // A confirmation never waits on the gate.
    e.end();
    await vi.advanceTimersByTimeAsync(0);
    quiet = 0;
    await d.stream("", "and the docs");
    void q.confirmLine("Cleared.");
    expect(e.said.at(-1)).toBe("Cleared.");
  });

  it("midSentence: the grace after keying, then only words in the last 1.5 s without a 250 ms pause", async () => {
    const k = hooks({ quietMs: () => quiet });
    const d = new LiveDictation(session(), k.h, { continuous: true });
    expect(d.midSentence()).toBe(true);
    await vi.advanceTimersByTimeAsync(MID_SENTENCE_MS);
    expect(d.midSentence()).toBe(false);
    await d.stream("", "fix the");
    expect(d.midSentence()).toBe(true);
    quiet = 300;
    expect(d.midSentence()).toBe(false);
    quiet = 0;
    await vi.advanceTimersByTimeAsync(MID_SENTENCE_MS);
    expect(d.midSentence()).toBe(false); // stale words
    await d.flush("fix the bug");
    expect(d.midSentence()).toBe(false); // the phrase is over
  });

  it("the wiring: the gate instead of the hold while continuous, the recorder muted during a line, a press cuts the line", () => {
    const MIC = code("components/TopBarMic.tsx");
    expect(MIC).toMatch(/if \(continuous\) \{\s*gated\.current = true;\s*queue\.setGate\(\(\) => !d\.midSentence\(\)\);\s*queue\.release\(\);\s*\}/);
    expect(MIC).toMatch(/if \(gated\.current\) rec\.current\?\.mute\?\.\(on\);/);
    expect(MIC).toMatch(/const halt = \(\) => \{\s*gated\.current = false;\s*queue\.setGate\(null\);/);
    expect(MIC).toMatch(/if \(gated\.current && queue\.busy && prefsRef\.current\.ptt !== "hold"\) \{\s*queue\.skip\(\);\s*return;\s*\}/);
    // A hands-free answer is not continuous: the queue stays held for it.
    expect(MIC).toMatch(/title,\s*hint,\s*continuous: false,/);
  });
});

describe("a stop's own confirmation plays whole: nothing cuts it, the chime comes after it", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    mocks.instApi.mockResolvedValue({ ok: true });
  });

  /** The desktop's wiring end to end: a real SpeechQueue, Confirmer and
   * LiveDictation, start() and halt() as TopBarMic's (the barge-in's hold,
   * the start chime, the gate; halt: the gate off, the stop chime queued by
   * sound(), the release). The engine logs each line's start and whether it
   * finished on its own (`end`) or was cancelled mid-line (`cut`). */
  function rig() {
    const events: string[] = [];
    let current = "";
    let finish: () => void = () => {};
    const sp: Speaker = {
      speak: (t) => {
        current = t;
        events.push("say " + t);
        return new Promise<void>(
          (r) =>
            (finish = () => {
              events.push("end " + current);
              current = "";
              r();
            })
        );
      },
      cancel: () => {
        if (current) events.push("cut " + current);
        current = "";
      },
    };
    const q = new SpeechQueue(() => sp);
    const mutes: boolean[] = [];
    const confirmer = new Confirmer({
      queue: q,
      on: () => true,
      mic: () => ({ mute: (b: boolean) => void mutes.push(b) }),
      server: () => false,
      local: () => false,
      speaker: { lastError: "", prefetch: async () => {} },
    });
    const sound = (kind: "start" | "stop") =>
      void q.cue(() => void events.push("chime " + kind), kind === "stop" ? STOP_CHIME_MS : CHIME_MS);
    let halts = 0;
    const k = hooks({
      confirm: (kind) => void confirmer.say(kind),
      halt: () => {
        halts++;
        q.setGate(null);
        sound("stop");
        q.release();
      },
      quietMs: () => 0,
    });
    const t = session();
    q.hold();
    const d = new LiveDictation(t, k.h, { continuous: true });
    sound("start");
    q.setGate(() => !d.midSentence());
    q.release();
    // From here on, nothing may cancel, skip or hold the queue.
    const spies = (["cancel", "skip", "hold"] as const).map((m) => vi.spyOn(q, m));
    return {
      q,
      d,
      t,
      events,
      mutes,
      halts: () => halts,
      /** The engine finishes the line it is saying. */
      end: async () => {
        finish();
        await vi.advanceTimersByTimeAsync(0);
      },
      queueCalls: () => spies.flatMap((s) => s.mock.calls.map(() => s.getMockName())),
    };
  }

  /** `p`, with the clock running (a send waits SUBMIT_SETTLE_MS). */
  const settle = async (p: Promise<void>) => {
    let done = false;
    void p.then(() => (done = true));
    while (!done) await vi.advanceTimersByTimeAsync(10);
  };

  it("a stop phrase: “Stopped.” finishes on its own, then the falling chime — no cancel, skip or hold", async () => {
    const r = rig();
    expect(r.events).toEqual(["chime start"]); // the queue was idle: at once
    await vi.advanceTimersByTimeAsync(CHIME_MS);
    await r.d.heard("half a thought stop recording");
    expect(r.d.active).toBe(false);
    expect(r.halts()).toBe(1);
    expect(r.events).toEqual(["chime start", "say Stopped."]);
    expect(r.mutes).toEqual([true]);
    // The stop's teardown and the release are done: the line plays on.
    await vi.advanceTimersByTimeAsync(2000);
    expect(r.events).toEqual(["chime start", "say Stopped."]);
    await r.end();
    // A breath after the words, never over them.
    await vi.advanceTimersByTimeAsync(CUE_GAP_MS - 1);
    expect(r.events.at(-1)).toBe("end Stopped.");
    await vi.advanceTimersByTimeAsync(1);
    expect(r.events).toEqual(["chime start", "say Stopped.", "end Stopped.", "chime stop"]);
    expect(r.events.some((e) => e.startsWith("cut"))).toBe(false);
    expect(r.queueCalls()).toEqual([]);
    expect(r.mutes).toEqual([true, false]);
    await vi.advanceTimersByTimeAsync(STOP_CHIME_MS);
    expect(r.q.idle).toBe(true);
  });

  it("“over and out”: “Over and out.” / “Sent. Over and out.” play whole, the chime after", async () => {
    for (const [words, line] of [
      ["over and out", CONFIRM_LINES.bye],
      ["ship it over and out", CONFIRM_LINES.out],
    ] as const) {
      const r = rig();
      await vi.advanceTimersByTimeAsync(CHIME_MS);
      await settle(r.d.heard(words));
      expect(r.d.active).toBe(false);
      expect(r.events).toEqual(["chime start", "say " + line]);
      await r.end();
      await vi.advanceTimersByTimeAsync(CUE_GAP_MS);
      expect(r.events).toEqual(["chime start", "say " + line, "end " + line, "chime stop"]);
      expect(r.queueCalls()).toEqual([]);
    }
  });

  it("the cut-off: a line still being said when the stop acts — the chime used to start with “Stopped.”; now it waits for its end", async () => {
    const r = rig();
    await vi.advanceTimersByTimeAsync(CHIME_MS);
    await settle(r.d.heard("ship it over"));
    expect(r.events.at(-1)).toBe("say Sent."); // still being said…
    await r.d.heard("over and out"); // …when the stop phrase acts
    expect(r.d.active).toBe(false);
    await r.end();
    // "Over and out." starts the moment "Sent." ends — alone.
    expect(r.events.slice(-2)).toEqual(["end Sent.", "say Over and out."]);
    await vi.advanceTimersByTimeAsync(500);
    expect(r.events.at(-1)).toBe("say Over and out.");
    await r.end();
    await vi.advanceTimersByTimeAsync(CUE_GAP_MS);
    expect(r.events.slice(-3)).toEqual(["say Over and out.", "end Over and out.", "chime stop"]);
    expect(r.events.some((e) => e.startsWith("cut"))).toBe(false);
    expect(r.queueCalls()).toEqual([]);
  });

  it("“disregard”: “Cleared.” plays whole while listening goes on — the next phrase does not clip it, and no chime", async () => {
    const r = rig();
    await vi.advanceTimersByTimeAsync(CHIME_MS);
    await r.d.heard("oops disregard");
    expect(r.d.active).toBe(true);
    expect(r.events).toEqual(["chime start", "say Cleared."]);
    // The next phrase arrives while it is still being said.
    await r.d.heard("fresh start");
    expect(r.t.keys.at(-1)).toBe("fresh start ");
    await vi.advanceTimersByTimeAsync(500);
    expect(r.events).toEqual(["chime start", "say Cleared."]);
    await r.end();
    await vi.advanceTimersByTimeAsync(CUE_GAP_MS + STOP_CHIME_MS);
    expect(r.events).toEqual(["chime start", "say Cleared.", "end Cleared."]);
    expect(r.queueCalls()).toEqual([]);
  });

  it("the queue: a session's cancel spares the confirmations and the chime; a hold drops a chime still waiting; a chime is not words", async () => {
    const speaking: boolean[] = [];
    const q = new SpeechQueue(
      () => ({ speak: () => new Promise<void>(() => {}), cancel: () => {} }),
      (on) => void speaking.push(on)
    );
    void q.say("Billing: done.", { title: "billing" });
    void q.confirmLine("Stopped.");
    let chimed = 0;
    const cue = q.cue(() => void chimed++, STOP_CHIME_MS);
    q.cancel("billing"); // cuts the reply being said
    q.cancel(""); // never the person's own lines
    expect(q.last).toBe("Stopped.");
    expect(q.queued).toBe(1); // the chime, behind "Stopped."
    q.hold(); // the mic keyed again: "Stopped." is cut (barge-in), the chime dropped
    await cue;
    expect(chimed).toBe(0);
    expect(q.idle).toBe(true);
    // An idle queue plays a chime at once (the start chime is skipped from
    // the recording from this moment); no onSpeaking, never "repeat".
    q.release();
    speaking.length = 0;
    void q.cue(() => void chimed++, CHIME_MS);
    expect(chimed).toBe(1);
    expect(q.busy).toBe(true);
    await vi.advanceTimersByTimeAsync(CHIME_MS);
    expect(q.idle).toBe(true);
    expect(speaking).toEqual([]);
    expect(q.last).toBe("Stopped.");
  });

  it("the wiring: halt() never cancels, skips or holds the queue; the stop chime is queued before the release", () => {
    const MIC = code("components/TopBarMic.tsx");
    const halt = MIC.slice(MIC.indexOf("const halt = () => {"), MIC.indexOf("const target = ("));
    expect(halt).not.toMatch(/queue\.(cancel|skip|hold)\(|bargeIn\(|speechSynthesis/);
    expect(halt.indexOf('sound("stop")')).toBeLessThan(halt.indexOf("queue.release()"));
  });
});
