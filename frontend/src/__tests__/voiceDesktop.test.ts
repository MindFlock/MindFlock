/** The desktop mic (components/TopBarMic.tsx + lib/voiceDesktop.ts): the
 * radio words, the push-to-talk machine, the prefs, the reply rules and the
 * speech queue — pure, so pinned here — and the wiring around them pinned
 * against the source: where the mic sits, that the old dictation button is
 * gone, and what goes on /send. */

import { describe, expect, it } from "vitest";
import {
  DEFAULT_VOICE_PREFS,
  Dictation,
  ReplyTracker,
  SpeechQueue,
  VOICE_PREFS_KEY,
  loadVoicePrefs,
  parseRadio,
  recognizerErrorLine,
  saveVoicePrefs,
  sentenceChunks,
  spokenLine,
  sttEngine,
  ttsEngine,
  type Speaker,
} from "../lib/voiceDesktop";
import { ALL_CODE, ALL_CSS, code, cssRule, fn, src } from "./sourceText";

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
  it("uses the server only when Settings say so AND it is ready", () => {
    const off = { stt: "phone" as const, tts: "phone" as const, server_ready: { stt: true, tts: true } };
    const on = { stt: "server" as const, tts: "server" as const, server_ready: { stt: true, tts: true } };
    const notReady = { stt: "server" as const, tts: "server" as const, server_ready: {} };
    expect(sttEngine(off, true)).toBe("browser");
    expect(sttEngine(on, true)).toBe("server");
    expect(sttEngine(on, false)).toBe("browser");
    expect(sttEngine(notReady, true)).toBe("browser");
    expect(ttsEngine(on)).toBe("server");
    expect(ttsEngine(notReady)).toBe("browser");
  });

  it("says what a network failure and a denied mic mean", () => {
    expect(recognizerErrorLine("network")).toBe(
      "Speech recognition failed (network): use Chrome, Edge or Safari, or turn on server recognition in Settings → Your phone → Voice"
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
    expect(MIC).toContain('openSettings("mobile")');
    expect(MIC).toContain("Read replies aloud");
    expect(MIC).toContain("Click to talk");
    expect(MIC).toContain("Hold to talk");
  });
});
