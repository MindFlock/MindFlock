/** Speech that gets out of the way (lib/voiceDesktop.ts, the ask pop-up and
 * the top-bar mic):
 *  - the speech queue drops ONE session's lines (the one being said too)
 *    and keeps the others'; hold() cuts the line at once, never resumed;
 *  - barge-in: the app stops talking BEFORE the mic opens, the ask's
 *    remaining lines are dropped, the first ~150 ms of audio skipped;
 *  - "stop talking": router kind `hush` and the local whole-phrase match —
 *    never typed, the queue cancelled, listening goes on;
 *  - a hand on the ask card stops its read-out and hands-free listen, and a
 *    step that comes up within 3 s of a hand is not read out;
 *  - Esc while the app talks, the options' "Stop talking";
 *  - "Everything" tells the server (POST /api/voice/say-all) on a switch,
 *    and the pref wins once on load.
 * The vitest environment is node: pure parts driven, the wiring pinned
 * against the source. */

import { afterEach, describe, expect, it, vi } from "vitest";
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
  return { api: vi.fn(), apiRaw: vi.fn(), instApi: vi.fn(), ApiError, peekTerm: vi.fn(), startWavRecording: vi.fn() };
});
vi.mock("../api/client", async (orig) => ({
  ...((await orig()) as object),
  api: mocks.api,
  apiRaw: mocks.apiRaw,
  instApi: mocks.instApi,
  ApiError: mocks.ApiError,
}));
vi.mock("../lib/terminals", () => ({ peekTerm: mocks.peekTerm }));
vi.mock("../lib/wavRecorder", () => ({ startWavRecording: mocks.startWavRecording, resamplerName: () => "x" }));

const V = await import("../lib/voiceDesktop");
const {
  BARGE_SKIP_MS,
  Dictation,
  HUSH_LINE,
  HandsFree,
  LiveDictation,
  SpeechQueue,
  bargeIn,
  holdBack,
  isHushPhrase,
  mayBeCommand,
  routePhrase,
  sayAllFlip,
  sayAllMismatch,
  saveVoicePrefs,
  serverRecognizer,
  sessionTarget,
  syncSayAll,
  SAY_ALL_FORBIDDEN_LINE,
} = V;
type Speaker = import("../lib/voiceDesktop").Speaker;
type LiveHooks = import("../lib/voiceDesktop").LiveHooks;

const tick = async () => {
  for (let i = 0; i < 6; i++) await Promise.resolve();
};

/** An engine that says one line until finish() / cancel(), logging both. */
function engine(log: string[] = []) {
  const said: string[] = [];
  let end: (() => void) | null = null;
  const sp: Speaker = {
    speak: (text) =>
      new Promise<void>((resolve) => {
        said.push(text);
        log.push("speak " + text);
        end = resolve;
      }),
    cancel: () => {
      log.push("cancel");
      // A real engine's promise resolves late (or never): the queue must not wait for it.
    },
  };
  return { sp, said, log, finish: () => end?.() };
}

describe("SpeechQueue: one session's lines", () => {
  it("cancel(title) drops that ask's lines — the one being said and the queued — and keeps another session's", async () => {
    const e = engine();
    const states: boolean[] = [];
    const q = new SpeechQueue(() => e.sp, (on) => states.push(on));
    const done: string[] = [];
    void q.say("Login fix: which store?", { title: "a" }).then(() => done.push("a1"));
    void q.say("B: tests pass.", { title: "b" }).then(() => done.push("b1"));
    void q.say("Login fix: say one or two.", { title: "a" }).then(() => done.push("a2"));
    expect(e.said).toEqual(["Login fix: which store?"]);
    q.cancel("a");
    await tick();
    // Cut at once (no waiting on the engine), both of a's promises resolved,
    // and b's line plays next.
    expect(e.log).toContain("cancel");
    expect(done.sort()).toEqual(["a1", "a2"]);
    expect(e.said).toEqual(["Login fix: which store?", "B: tests pass."]);
    expect(q.busy).toBe(true);
    e.finish();
    await tick();
    expect(done).toContain("b1");
    expect(q.idle).toBe(true);
    expect(states).toEqual([true, false, true, false]);
  });

  it("cancel(title) leaves the line being said alone when it is another session's", async () => {
    const e = engine();
    const q = new SpeechQueue(() => e.sp);
    void q.say("B: one.", { title: "b" });
    void q.say("A: two.", { title: "a" });
    q.cancel("a");
    expect(e.log).not.toContain("cancel");
    expect(q.queued).toBe(0);
    e.finish();
    await tick();
    expect(e.said).toEqual(["B: one."]);
  });

  it("cancel() with no title drops everything, every session", async () => {
    const e = engine();
    const q = new SpeechQueue(() => e.sp);
    void q.say("A", { title: "a" });
    void q.say("B", { title: "b" });
    void q.say("C");
    q.cancel();
    await tick();
    expect(q.idle).toBe(true);
    expect(e.said).toEqual(["A"]);
  });

  it("hold() is over for the line at once: busy false before the engine resolves, and it is never resumed", async () => {
    const e = engine();
    const q = new SpeechQueue(() => e.sp);
    let cut = false;
    void q.say("A: a long read-out.").then(() => (cut = true));
    void q.say("B: next.");
    q.hold();
    expect(q.busy).toBe(false);
    await tick();
    expect(cut).toBe(true);
    // The engine's late resolve changes nothing.
    e.finish();
    await tick();
    expect(e.said).toEqual(["A: a long read-out."]);
    q.release();
    expect(e.said).toEqual(["A: a long read-out.", "B: next."]);
  });
});

describe("barge-in: answer before it is done speaking", () => {
  it("the speech is cancelled BEFORE the mic opens, the ask's remaining lines dropped, another session's kept", async () => {
    const log: string[] = [];
    const e = engine(log);
    const q = new SpeechQueue(() => e.sp);
    void q.say("Login fix: Allow git push?", { title: "a" });
    void q.say("Say yes, no, or always.", { title: "a" });
    void q.say("B: deployed.", { title: "b" });
    // What TopBarMic.start does: bargeIn, then the recognizer.
    const skip = bargeIn(q, "a");
    log.push("mic open");
    expect(log).toEqual(["speak Login fix: Allow git push?", "cancel", "mic open"]);
    expect(skip).toBe(BARGE_SKIP_MS);
    expect(BARGE_SKIP_MS).toBeGreaterThanOrEqual(100);
    expect(BARGE_SKIP_MS).toBeLessThanOrEqual(250);
    // Held while listening; the mic done: only b's line comes back.
    await tick();
    expect(e.said).toEqual(["Login fix: Allow git push?"]);
    q.release();
    expect(e.said).toEqual(["Login fix: Allow git push?", "B: deployed."]);
  });

  it("nothing being said: no audio skipped, nothing cancelled", () => {
    const log: string[] = [];
    const q = new SpeechQueue(() => engine(log).sp);
    expect(bargeIn(q, "a")).toBe(0);
    expect(log).toEqual([]);
  });

  it("TopBarMic.start barges in before r.start and hands the recognizer the skip", () => {
    const MIC = code("components/TopBarMic.tsx");
    const start = MIC.slice(MIC.indexOf("const start = ("), MIC.indexOf("// Before the first word"));
    const barge = start.indexOf("const skipMs = bargeIn(queue, ask && opts.title === ask.title ? ask.title : \"\")");
    expect(barge).toBeGreaterThan(-1);
    expect(start.indexOf("r.start(")).toBeGreaterThan(barge);
    expect(start).toContain("{ skipMs }");
    // The old bare hold is gone (bargeIn holds).
    expect(start).not.toContain("queue.hold()");
  });

  it("the server recognizer skips the first audio after a barge-in (startWavRecording skipS)", async () => {
    class FakeCtx {
      state = "running";
      sampleRate = 48000;
      resume() {
        return Promise.resolve();
      }
    }
    vi.stubGlobal("window", { AudioContext: FakeCtx });
    vi.stubGlobal("navigator", { language: "en-US", userAgent: "", mediaDevices: { getUserMedia: async () => ({ active: true, getTracks: () => [] }) } });
    try {
      mocks.startWavRecording.mockImplementation(() => ({ stop: vi.fn(), cancel: vi.fn(), take: vi.fn(), seconds: () => 0 }));
      const r = serverRecognizer();
      const h = { onInterim: () => {}, onFinal: () => {}, onError: () => {}, onEnd: () => {} };
      expect(r.start(h, { skipMs: BARGE_SKIP_MS })).toBe(true);
      await vi.waitFor(() => expect(mocks.startWavRecording).toHaveBeenCalledTimes(1));
      const skipS = mocks.startWavRecording.mock.calls[0][2].skipS;
      expect(skipS).toBeGreaterThan(0.05);
      expect(skipS).toBeLessThanOrEqual(BARGE_SKIP_MS / 1000);
      r.stop();
      mocks.startWavRecording.mockClear();
      expect(r.start(h)).toBe(true);
      await vi.waitFor(() => expect(mocks.startWavRecording).toHaveBeenCalledTimes(1));
      expect(mocks.startWavRecording.mock.calls[0][2].skipS).toBe(0);
      r.stop();
    } finally {
      vi.unstubAllGlobals();
      mocks.startWavRecording.mockReset();
    }
  });
});

describe('"stop talking"', () => {
  it("the local match: whole phrases only, never a word inside a prompt", () => {
    for (const p of ["stop talking", "Stop talking.", "be quiet", "Quiet!", "shut up", "stop reading", "enough", "okay stop", "OK, stop.", "that's enough", "quiet please", "hush"])
      expect(isHushPhrase(p), p).toBe(true);
    for (const p of ["stop", "please don't stop talking to me", "quiet mode should be on", "enough tests", "stop recording", "over"])
      expect(isHushPhrase(p), p).toBe(false);
    // A streamed first phrase waits whole for its end (the router reads whole phrases).
    expect(mayBeCommand("stop")).toBe(true);
    expect(mayBeCommand("be")).toBe(true);
    expect(mayBeCommand("shut up")).toBe(true);
    // The hold-back keeps "be quiet" whole after other words.
    expect(holdBack("fix it be quiet")).toEqual({ type: "fix it", held: "be quiet" });
  });

  it("the dictation: a whole phrase is a hush — not typed, listening goes on; mid-phrase it is words", () => {
    const d = new Dictation();
    d.begin();
    expect(d.heard("Stop talking.")).toEqual({ kind: "hush" });
    expect(d.listening).toBe(true);
    // Streamed: "stop" then "talking", then the phrase's end.
    expect(d.words("stop")).toEqual({ kind: "continue" });
    expect(d.words("talking")).toEqual({ kind: "continue" });
    expect(d.flush("")).toEqual({ kind: "hush" });
    // After something of the phrase was typed, it is a prompt's words.
    const t = d.words("please fix it and");
    expect(t.kind).toBe("type");
    if (t.kind === "type") d.wrote(t.text);
    const f = d.flush("stop talking");
    expect(f.kind).toBe("type");
    // Push to talk let go on "stop talking": hush, and listening ends.
    const g = new Dictation();
    g.begin();
    g.hearing("stop talking");
    expect(g.finish()).toEqual({ kind: "hush", end: true });
  });

  it("live: the hush hook fires, nothing goes to the terminal, the caption says Quiet., the mic keeps listening", async () => {
    const sent: string[] = [];
    mocks.peekTerm.mockImplementation(() => ({ send: (s: string) => (sent.push(s), true) }));
    const captions: string[] = [];
    let hushed = 0;
    let halted = 0;
    const h: LiveHooks = {
      caption: (t) => void captions.push(t),
      halt: () => void halted++,
      route: async () => false,
      hush: () => void hushed++,
      activity: () => "idle",
      replies: () => false,
      submitted: () => {},
    };
    const d = new LiveDictation(sessionTarget("t1", "Login fix"), h);
    await d.heard("be quiet");
    expect(hushed).toBe(1);
    expect(sent).toEqual([]);
    expect(captions[captions.length - 1]).toBe(HUSH_LINE);
    expect(d.active).toBe(true);
    expect(halted).toBe(0);
    // Into an ask's answer field (no router there) it is never typed either.
    let typed = "";
    const field = { kind: "field" as const, title: "t1", name: "x", type: (s: string) => ((typed += s), true), erase: () => true };
    const f = new LiveDictation(field, h);
    await f.heard("stop talking");
    expect(typed).toBe("");
    expect(hushed).toBe(2);
    mocks.peekTerm.mockReset();
  });

  it("the router's kind hush (words only it knows): the route hook answers 'hush' — not typed, listening goes on", async () => {
    mocks.api.mockResolvedValueOnce({ kind: "hush", say: null });
    expect((await routePhrase("pipe down", "t1")).kind).toBe("hush");
    const sent: string[] = [];
    mocks.peekTerm.mockImplementation(() => ({ send: (s: string) => (sent.push(s), true) }));
    let hushed = 0;
    const d = new LiveDictation(sessionTarget("t1", "Login fix"), {
      caption: () => {},
      halt: () => {},
      route: async () => "hush",
      hush: () => void hushed++,
      activity: () => "idle",
      replies: () => false,
      submitted: () => {},
    });
    await d.heard("pipe down");
    expect(sent).toEqual([]);
    expect(hushed).toBe(1);
    expect(d.active).toBe(true);
    mocks.peekTerm.mockReset();
  });

  it("TopBarMic: kind hush and the hook cancel the WHOLE queue; Esc while talking stops it; the options get Stop talking", () => {
    const MIC = code("components/TopBarMic.tsx");
    expect(MIC).toContain('if (reply.kind === "hush") return "hush";');
    expect(MIC).toContain("hush: () => queue.cancel(),");
    expect(MIC).toMatch(/case "hush":\s*hush\(\);\s*return;/);
    const hush = MIC.slice(MIC.indexOf("const hush = () => {"), MIC.indexOf("// Settings → Voice edits"));
    expect(hush).toContain("queue.cancel();");
    expect(hush).toContain("say(HUSH_LINE, false, \"\", HUSH_CAPTION_MS)");
    // Esc: only while talking and not listening, never a modal's or a popover's.
    const esc = MIC.slice(MIC.indexOf("if (!speaking || listening || menuOpen) return;"), MIC.indexOf("// --- the router's answers"));
    expect(esc).toContain('e.key !== "Escape" || e.defaultPrevented');
    expect(esc).toContain("overlayOpen()");
    expect(esc).toContain("hush();");
    expect(esc).toContain('document.addEventListener("keydown", onKey, true)');
    expect(MIC).toMatch(/\{speaking && \(\s*<button type="button" className="mic-stop-talking" id="mic-stop-talking"[^>]*onClick=\{hush\}>\s*Stop talking/);
  });
});

describe("a hand on the ask card", () => {
  it("quiet(title): that ask's lines dropped, its hands-free listen ended — a dictation the person keyed stays", () => {
    const MIC = code("components/TopBarMic.tsx");
    const quiet = MIC.slice(MIC.indexOf("const quietAsk = (title: string) => {"), MIC.indexOf("// The ask pop-up's side of the mic"));
    expect(quiet).toContain("queue.cancel(title);");
    expect(quiet).toContain("hands.current?.stop(title);");
    expect(quiet).toContain("d === handsLive.current");
    expect(MIC).toContain("quiet: (title) => quietAsk(title),");
    // The next step: the previous read-out is over before the new one is read.
    expect(MIC).toMatch(/if \(asksRead\.has\(title\)\) quietAsk\(title\);[\s\S]*?h\?\.settled\(\)\.then\(\(\) => h\.ask\(title/);
  });

  it("the 3 s rule: a step within HAND_QUIET_MS of a hand is not read; later (or never touched) it is", async () => {
    const { narrate, HAND_QUIET_MS, handKey } = await import("../components/dialogs/AskDialog");
    expect(HAND_QUIET_MS).toBe(3000);
    expect(narrate(undefined, 10_000)).toBe(true);
    expect(narrate(10_000, 10_000 + 500)).toBe(false);
    expect(narrate(10_000, 10_000 + 2_999)).toBe(false);
    expect(narrate(10_000, 10_000 + 3_000)).toBe(true);
    // Which keys are a hand: digits, Enter, letters typed — not Tab, a lone
    // modifier, a shortcut (the Talk hotkey) or Escape (Later).
    const k = (key: string, mod: Partial<KeyboardEvent> = {}) => handKey({ key, ctrlKey: false, metaKey: false, altKey: false, ...mod });
    expect([k("1"), k("Enter"), k("a"), k(" "), k("Backspace")]).toEqual([true, true, true, true, true]);
    expect([k("Tab"), k("Shift"), k("Escape"), k("t", { ctrlKey: true }), k(" ", { altKey: true })]).toEqual([false, false, false, false, false]);
  });

  it("the card wires it: a press (not its mic) or a key on the card, the digit keys, and the read-out effect", () => {
    const ASK = code("components/dialogs/AskDialog.tsx");
    expect(ASK).toMatch(/const byHand = useCallback\(\(\) => \{\s*handAt\.set\(title, Date\.now\(\)\);\s*askMic\(\)\?\.quiet\(title\);/);
    expect(ASK).toMatch(/onPointerDownCapture=\{\(e\) => \{\s*if \(handTarget\(e\.target\)\) byHand\(\);/);
    expect(ASK).toMatch(/onKeyDownCapture=\{\(e\) => \{\s*if \(handKey\(e\) && !\(e\.target as Element \| null\)\?\.closest\?\.\("\.ask-voice"\)\) byHand\(\);/);
    expect(ASK).toContain('t.closest(".ask-voice")) return false;');
    // The window's digit keys (focus anywhere) are a hand too.
    expect(ASK.match(/e\.stopPropagation\(\);\s*byHand\(\);/g)).toHaveLength(2);
    // The read-out: never within 3 s of a hand.
    expect(ASK).toMatch(/if \(readOut\.has\(readKey\)\) return;\s*if \(!narrate\(handAt\.get\(title\), Date\.now\(\)\)\)/);
  });

  it("HandsFree tags every line with its session and settles once the run is over", async () => {
    const spoken: Array<[string, string]> = [];
    let release: () => void = () => {};
    const x = new HandsFree({
      replies: () => true,
      focused: () => "t1",
      name: () => "Login fix",
      busy: () => false,
      fetchDialog: async () => null,
      approveDialogs: async () => true,
      speak: (l, t) => {
        spoken.push([l, t]);
        return new Promise<void>((r) => (release = r));
      },
      caption: () => {},
      listen: async () => "acted",
    });
    const d = { id: "d1", parsed: true, question: "Allow git push?", command: null, options: [{ key: "1", label: "Yes", kind: "yes" }] };
    const run = x.ask("t1", { dialog: d });
    let settled = false;
    void x.settled().then(() => (settled = true));
    await tick();
    expect(spoken).toEqual([["Login fix: Allow git push? Say yes or no.", "t1"]]);
    expect(settled).toBe(false);
    x.stop("t1"); // a hand on the card
    release(); // the queue cut the line
    await run;
    await tick();
    expect(settled).toBe(true);
  });
});

describe('"Everything": a Say line on every prompt (POST /api/voice/say-all)', () => {
  afterEach(() => mocks.api.mockReset());

  it("only a switch across Everything posts; on load the pref wins", () => {
    expect(sayAllFlip("off", "all")).toBe(true);
    expect(sayAllFlip("mine", "all")).toBe(true);
    expect(sayAllFlip("all", "mine")).toBe(false);
    expect(sayAllFlip("all", "off")).toBe(false);
    expect(sayAllFlip("off", "mine")).toBeNull();
    expect(sayAllFlip("all", "all")).toBeNull();
    expect(sayAllMismatch("all", false)).toBe(true);
    expect(sayAllMismatch("off", true)).toBe(false);
    expect(sayAllMismatch("all", true)).toBeNull();
    expect(sayAllMismatch("mine", false)).toBeNull();
    expect(sayAllMismatch("all", undefined)).toBeNull(); // an older server
  });

  it("saveVoicePrefs posts on a switch to / away from Everything, never on a plain save", () => {
    const m = new Map<string, string>();
    const kv = { getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => void m.set(k, v) };
    const posts: boolean[] = [];
    const sync = (on: boolean) => posts.push(on);
    saveVoicePrefs({ ptt: "toggle", replies: "mine" }, kv, sync);
    saveVoicePrefs({ ptt: "hold", replies: "mine" }, kv, sync);
    saveVoicePrefs({ ptt: "hold", replies: "all" }, kv, sync);
    saveVoicePrefs({ ptt: "toggle", replies: "all" }, kv, sync);
    saveVoicePrefs({ ptt: "toggle", replies: "off" }, kv, sync);
    expect(posts).toEqual([true, false]);
  });

  it("POSTs {on} to /api/voice/say-all; a 403 is said once and the pref kept", async () => {
    mocks.api.mockResolvedValueOnce({ say_all: true });
    expect(await V.postSayAll(true)).toBe("ok");
    expect(mocks.api).toHaveBeenCalledWith("/api/voice/say-all", { json: { on: true } });
    mocks.api.mockRejectedValue(new mocks.ApiError(403, "forbidden", null));
    expect(await V.postSayAll(false)).toBe("forbidden");
    const warned: string[] = [];
    await syncSayAll(true, V.postSayAll, (l) => warned.push(l));
    await syncSayAll(false, V.postSayAll, (l) => warned.push(l));
    expect(warned).toEqual([SAY_ALL_FORBIDDEN_LINE]);
    mocks.api.mockRejectedValue(new mocks.ApiError(500, "boom", null));
    expect(await V.postSayAll(true)).toBe("error");
  });

  it("GET /api/voice's say_all is read; TopBarMic reconciles it once on load", async () => {
    mocks.api.mockResolvedValueOnce({ stt: "phone", tts: "phone", say_all: true });
    expect((await V.loadVoiceCfg(null)).say_all).toBe(true);
    mocks.api.mockResolvedValueOnce({ stt: "phone", tts: "phone" });
    expect("say_all" in (await V.loadVoiceCfg(null))).toBe(false);
    const MIC = code("components/TopBarMic.tsx");
    expect(MIC).toMatch(
      /if \(sayAllChecked \|\| typeof c\.say_all !== "boolean"\) return;\s*sayAllChecked = true;\s*const want = sayAllMismatch\(prefsRef\.current\.replies, c\.say_all\);\s*if \(want !== null\) void syncSayAll\(want\);/
    );
  });
});
