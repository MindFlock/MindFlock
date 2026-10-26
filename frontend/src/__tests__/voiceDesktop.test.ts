/** The desktop mic (components/TopBarMic.tsx + lib/voiceDesktop.ts): the
 * radio words, the push-to-talk machine, the prefs, the reply rules, the
 * speech queue and the engine choice — pure, so pinned here — the server
 * recognizer's WAV path against a mocked recorder and `apiRaw`, and the
 * wiring around them pinned against the source: where the mic sits, that the
 * old dictation button is gone, and what goes on /send. */

import { afterEach, describe, expect, it, vi } from "vitest";
import {
  DEFAULT_VOICE_CFG,
  DEFAULT_VOICE_PREFS,
  Dictation,
  ReplyTracker,
  SpeechQueue,
  VOICE_PREFS_KEY,
  engineDiagnostics,
  isElectron,
  loadVoicePrefs,
  noRecognizerLine,
  parseRadio,
  recognizerErrorLine,
  saveVoicePrefs,
  sentenceChunks,
  serverRecognizer,
  spokenLine,
  sttEngine,
  ttsEngine,
  type RecognizerHandlers,
  type Speaker,
  type VoiceCfg,
} from "../lib/voiceDesktop";
import { ALL_CODE, ALL_CSS, blockFrom, code, cssRule, fn, src } from "./sourceText";

const mocks = vi.hoisted(() => ({ api: vi.fn(), apiRaw: vi.fn(), startWavRecording: vi.fn() }));
vi.mock("../api/client", () => ({ api: mocks.api, apiRaw: mocks.apiRaw }));
vi.mock("../lib/wavRecorder", () => ({ startWavRecording: mocks.startWavRecording }));

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
    ["disregard", "disregard", ""],
    ["Disregard that.", "disregard", ""],
    ["scrap it, disregard it", "disregard", "scrap it"],
    ["move it over", "over", "move it"],
    ["hangover", "", "hangover"],
    ["start over the tests then stop", "", "start over the tests then stop"],
    ["", "", ""],
  ])("%j → %s", (text, kind, rest) => {
    expect(parseRadio(text)).toEqual({ kind, text: rest });
  });
});

describe("Dictation: the push-to-talk machine", () => {
  it("toggle: start → phrases → stop sends them, with the phrase count", () => {
    const d = new Dictation();
    d.begin();
    expect(d.heard("fix the login bug")).toEqual({ kind: "continue" });
    expect(d.heard("and add a test")).toEqual({ kind: "continue" });
    expect(d.finish()).toEqual({ kind: "send", text: "fix the login bug and add a test", phrases: 2 });
    expect(d.listening).toBe(false);
  });

  it("toggle: stopping with nothing heard just stops", () => {
    const d = new Dictation();
    d.begin();
    expect(d.finish()).toEqual({ kind: "stopped" });
    expect(d.listening).toBe(false);
  });

  it("hold: down then up sends — and a phrase still in flight on release counts", () => {
    const d = new Dictation();
    d.begin(); // pointerdown
    d.hearing("switch to the login fix");
    expect(d.shown).toBe("switch to the login fix");
    expect(d.finish()).toEqual({ kind: "send", text: "switch to the login fix", phrases: 1 }); // pointerup
  });

  it("a phrase ending in “over” sends at once; “over” alone with nothing to send keeps listening", () => {
    const d = new Dictation();
    d.begin();
    expect(d.heard("Over.")).toEqual({ kind: "continue" });
    expect(d.listening).toBe(true);
    d.heard("run the tests");
    expect(d.heard("over")).toEqual({ kind: "send", text: "run the tests", phrases: 1 });
    expect(d.listening).toBe(false);
  });

  it("“disregard” clears the draft and keeps listening", () => {
    const d = new Dictation();
    d.begin();
    d.heard("delete everything");
    expect(d.heard("Disregard that.")).toEqual({ kind: "cleared" });
    expect(d.listening).toBe(true);
    expect(d.text).toBe("");
    d.heard("keep everything over");
    expect(d.listening).toBe(false);
  });

  it("Escape drops the words; silence keeps them unsent; the next round continues from them", () => {
    const d = new Dictation();
    d.begin();
    d.heard("half a thought");
    expect(d.cancel()).toEqual({ kind: "dropped" });
    expect(d.text).toBe("");
    d.begin();
    d.heard("half a thought");
    expect(d.expire()).toEqual({ kind: "kept", text: "half a thought" });
    d.begin("half a thought");
    expect(d.heard("and the rest over")).toEqual({ kind: "send", text: "half a thought and the rest", phrases: 2 });
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
      "Recognition: server, built in (Windows via WSL) · Voice: server (piper)",
    ],
    [
      "the built-in engine on a Mac, both ways",
      cfg({ server_ready: { stt: true, tts: true, stt_backend: "system", tts_backend: "system" }, system: { platform: "macos" } }),
      "server",
      "server",
      true,
      "Recognition: server, built in (macOS) · Voice: server, built in (macOS)",
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
    now += 1000; // 2 s of quiet > SILENCE_MS
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
    expect(lib).toContain('import { startWavRecording, type WavRecording } from "./wavRecorder";');
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

  it("sends with dialog_safe and the replies pref as `say`", () => {
    expect(MIC).toMatch(/instApi<[^>]*>\(title, "\/send", \{\s*json: \{ text, dialog_safe: true, say: wantSay \}/);
    expect(MIC).toContain("const wantSay = prefsRef.current.replies;");
    expect(MIC).toContain("tracker.sent(title, wantSay)");
  });

  it("asks the router first, for one phrase only, and selects through the one selection authority", () => {
    expect(MIC).toContain("if (phrases === 1)");
    expect(MIC).toContain("routePhrase(text, session)");
    expect(MIC).toContain("selectSession(title, { noKeyboard: true })");
    expect(code("lib/voiceDesktop.ts")).toContain('api<RouteReply>("/api/voice/route", { json: { text, session } })');
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
