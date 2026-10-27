/** The voice words (lib/voiceWords): the person's own words for send / end /
 * start over / stop / stop talking / the wake word, saved as `voice.words`
 * and read back from GET /api/voice.
 *
 * What is pinned: before the server answers, the built-in defaults; the
 * matchers (parseRadio, the hold-back, the stop tail, the hush match, the
 * router's command list) rebuilt from custom words — longest phrase first,
 * escaped, the clear words' tails kept — the moment the words change, and a
 * GET /api/voice (the mic's loadVoiceCfg, Settings → Voice's re-read after a
 * save) is what changes them; every hint that names a word names the current
 * one. Settings → Voice → Voice words: one row per kind, the defaults faded
 * while a kind has none of its own, Reset only with something to reset, the
 * server's limits refused before a save, `{}` for all defaults. The Voice
 * tour: eight steps on real elements, in the current words, opened by itself
 * on the mic's first use until `tours_seen` has it, and from Settings → Voice
 * and the mic's options. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { code, src } from "./sourceText";

const api = vi.fn();
vi.mock("../api/client", () => ({
  api: (...a: unknown[]) => api(...a),
  apiRaw: vi.fn(),
  instApi: vi.fn(),
  ApiError: class extends Error {},
}));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));
vi.mock("../lib/prefsSync", () => ({ firstPrefsPull: Promise.resolve() }));

const storage = new Map<string, string>();
(globalThis as Record<string, unknown>).localStorage = {
  getItem: (k: string) => (storage.has(k) ? storage.get(k)! : null),
  setItem: (k: string, v: string) => void storage.set(k, String(v)),
  removeItem: (k: string) => void storage.delete(k),
  clear: () => storage.clear(),
};

const W = await import("../lib/voiceWords");
const V = await import("../lib/voiceDesktop");
const { wakeSwitchHint, wakeSwitchLabel } = await import("../lib/wakeWord");
const { TOURS } = await import("../lib/tours");
const { voiceTourSteps } = await import("../lib/tours/voice");
const { VoiceWordsBlock, savedWords, withWord, WORD_ROWS } = await import("../components/settings/screens/VoiceWords");
const { firstUseTourDue, openTourOnFirstUse } = await import("../components/onboarding/useAutoTour");
const { useUi } = await import("../state/store");

const CUSTOM = {
  send: ["send it", "roger"],
  end: ["roger and out", "ten four"],
  clear: ["scratch"],
  stop: ["halt", "mic down now"],
  hush: ["pipe down", "silence"],
  wake: "hey flock",
};

afterEach(() => W.resetVoiceWords());

describe("the words, before and after the server answers", () => {
  it("uses the built-in defaults before any fetch", () => {
    expect(W.voiceWords()).toEqual(W.BUILTIN_VOICE_WORDS);
    expect(W.voiceWords().send).toEqual(["over"]);
    expect(W.voiceWords().end.slice(0, 2)).toEqual(["over and out", "over, out"]);
    expect(W.voiceWords().wake).toBe("mindflock");
    // The server's six per kind.
    expect(W.voiceWords().stop).toEqual(["stop", "stop listening", "mic off", "pause listening", "that's all", "done talking"]);
    expect(W.voiceWords().hush).toEqual(["stop talking", "be quiet", "quiet", "hush", "shut up", "enough"]);
    expect(V.parseRadio("fix it over").kind).toBe("over");
  });

  it("keeps the older wordings while their main phrase is in the list, as the router does", () => {
    expect(V.stopTail("fix it, stop recording")).toBe("fix it");
    expect(V.stopTail("fix it, turn off the mic")).toBe("fix it");
    expect(V.isHushPhrase("stop speaking")).toBe(true);
    expect(V.isHushPhrase("that's enough")).toBe(true);
    W.setVoiceWords({ stop: ["stop", "mic off"], hush: ["hush"] });
    expect(V.stopTail("fix it, stop recording")).toBeNull();
    expect(V.stopTail("fix it, turn off the mic")).toBe("fix it");
    expect(V.isHushPhrase("stop speaking")).toBe(false);
    expect(V.isHushPhrase("hush")).toBe(true);
  });

  it("takes `words` and `words_default` from GET /api/voice; a kind missing or invalid falls back", () => {
    W.applyVoiceView({ words: { send: ["Roger"], end: [], clear: [" "], wake: "a b c" }, words_default: W.BUILTIN_VOICE_WORDS });
    const w = W.voiceWords();
    expect(w.send).toEqual(["roger"]);
    expect(w.end).toEqual(W.BUILTIN_VOICE_WORDS.end);
    expect(w.clear).toEqual(["disregard"]);
    expect(w.wake).toBe("mindflock"); // three words is no wake word
    // An older server (no `words`): nothing changes.
    const v = W.voiceWordsVersion();
    W.applyVoiceView({ stt: "server" });
    expect(W.voiceWordsVersion()).toBe(v);
  });

  it("tells its listeners only when the words change", () => {
    const seen: string[] = [];
    const off = W.onVoiceWords((w) => seen.push(w.send[0]));
    W.setVoiceWords(CUSTOM);
    W.setVoiceWords(CUSTOM);
    off();
    W.setVoiceWords({ send: ["x"] });
    expect(seen).toEqual(["send it"]);
  });

  it("refreshVoiceWords and the mic's loadVoiceCfg read GET /api/voice — an unreachable server keeps them", async () => {
    api.mockResolvedValueOnce({ words: CUSTOM, words_default: W.BUILTIN_VOICE_WORDS });
    await W.refreshVoiceWords();
    expect(W.voiceWords().send).toEqual(["send it", "roger"]);
    api.mockRejectedValueOnce(new Error("down"));
    await W.refreshVoiceWords();
    expect(W.voiceWords().send).toEqual(["send it", "roger"]);
    api.mockResolvedValueOnce({ stt: "server", words: { send: ["copy"] } });
    await V.loadVoiceCfg(null);
    expect(W.voiceWords().send).toEqual(["copy"]);
    expect(api).toHaveBeenLastCalledWith("/api/voice");
  });
});

describe("the matchers, built from custom words", () => {
  beforeEach(() => W.setVoiceWords(CUSTOM, W.BUILTIN_VOICE_WORDS));

  it.each([
    ["fix the bug send it", "over", "fix the bug"],
    ["ship it, Roger.", "over", "ship it"],
    ["ship it roger and out", "out", "ship it"], // longest first: not "roger"
    ["ship it ten four!", "out", "ship it"],
    ["scrap that scratch that", "disregard", "scrap that"], // the clear words keep their tails
    ["fix it over", "", "fix it over"], // the default is gone
    ["fix it disregard", "", "fix it disregard"],
    ["the jolly roger is a flag", "", "the jolly roger is a flag"],
    ["rogers", "", "rogers"],
  ])("parseRadio(%j) → %s", (text, kind, rest) => {
    expect(V.parseRadio(text)).toEqual({ kind, text: rest });
  });

  it("escapes what a phrase holds: a regex character is a character", () => {
    W.setVoiceWords({ send: ["over.*"], end: ["done (now)"] });
    // Only letters, digits and apostrophes count as a phrase's words.
    expect(V.parseRadio("fix it over").kind).toBe("over");
    expect(V.parseRadio("fix it overxx").kind).toBe("");
    expect(V.parseRadio("fix it done now").kind).toBe("out");
    expect(() => V.buildMatchers(W.voiceWords())).not.toThrow();
  });

  it("an apostrophe is optional, in the words and in what is heard", () => {
    W.setVoiceWords({ stop: ["that's it now"], hush: ["that's plenty"] });
    expect(V.stopTail("fix it thats it now")).toBe("fix it");
    expect(V.isHushPhrase("That’s plenty.")).toBe(true);
    expect(V.isHushPhrase("thats plenty")).toBe(true);
  });

  it("the stop words end committed words — a one-word one only said on its own (the router's)", () => {
    expect(V.stopTail("fix the bug, mic down now.")).toBe("fix the bug");
    expect(V.stopTail("fix the bug halt")).toBeNull();
    expect(V.stopTail("fix it stop recording")).toBeNull(); // the default is gone
    expect(V.mayBeCommand("halt")).toBe(true);
    expect(V.mayBeCommand("mic down")).toBe(true);
  });

  it("the hush words are whole phrases, with the same leads and tails", () => {
    for (const p of ["pipe down", "Silence!", "okay, pipe down please", "silence now"]) expect(V.isHushPhrase(p), p).toBe(true);
    for (const p of ["be quiet", "silence the alarm", "stop talking"]) expect(V.isHushPhrase(p), p).toBe(false);
    expect(V.mayBeCommand("pipe")).toBe(true);
  });

  it("the hold-back keeps the custom phrases (and their starts) whole", () => {
    expect(V.holdBack("fix it roger and")).toEqual({ type: "fix it", held: "roger and" });
    expect(V.holdBack("fix it ten")).toEqual({ type: "fix it", held: "ten" });
    expect(V.holdBack("fix it mic down")).toEqual({ type: "fix it", held: "mic down" });
    expect(V.holdBack("fix it over")).toEqual({ type: "fix it over", held: "" });
    // "roger and out, mic down now": the end phrase then a stop phrase.
    expect(V.controlMaxWords()).toBe(6);
  });

  it("the dictation reads them: send keeps listening, end stops, clear erases", () => {
    const d = new V.Dictation();
    d.continuous = true;
    d.routes = false;
    d.begin();
    expect(d.heard("fix the bug send it")).toEqual({ kind: "send", text: "fix the bug " });
    d.next();
    d.wrote("draft ");
    expect(d.heard("scratch")).toEqual({ kind: "erase", text: "draft " });
    expect(d.heard("push it roger and out")).toEqual({ kind: "send", text: "push it ", end: true, out: true });
  });

  it("rebuilds only when the words change", () => {
    const a = V.matchers();
    expect(V.matchers()).toBe(a);
    W.setVoiceWords({ send: ["copy"] });
    expect(V.matchers()).not.toBe(a);
  });
});

describe("the hints name the current words", () => {
  it("dictHint / answerHint / the wake switch", () => {
    expect(V.dictHint()).toBe("Say “over” to send, “over and out” to stop, “disregard” to start over");
    expect(wakeSwitchLabel()).toBe("Listen for “MindFlock”");
    W.setVoiceWords(CUSTOM);
    expect(V.dictHint()).toBe("Say “send it” to send, “roger and out” to stop, “scratch” to start over");
    expect(V.answerHint("Bot")).toBe("Answer Bot — say “send it” to send, “roger and out” to stop, “scratch” to start over");
    expect(wakeSwitchLabel()).toBe("Listen for “Hey Flock”"); // capitalised, as the server hints it
    expect(wakeSwitchHint()).toMatch(/^Say “Hey Flock” on its own/);
  });

  it("no hard-coded word is left where a caption, label or hint is drawn", () => {
    expect(code("components/TopBarMic.tsx")).not.toContain("“MindFlock”");
    expect(code("components/TopBarMic.tsx")).toContain("dictHint()");
    const voice = code("components/settings/screens/Voice.tsx");
    expect(voice).not.toContain("“over and out”");
    expect(voice).not.toContain("“MindFlock”");
    expect(voice).toContain("{dictHint(words)}");
    expect(code("lib/voiceDesktop.ts")).not.toContain("DICT_HINT");
  });
});

describe("Settings → Voice → Voice words", () => {
  type Model = import("../components/settings/useSettings").SettingsModel;
  const model = (words: unknown): Model & { saveField: ReturnType<typeof vi.fn> } => {
    const settings = { voice: { words } } as Record<string, Record<string, unknown>>;
    return {
      settings,
      reload: async () => {},
      saveField: vi.fn(async () => true),
      saveGroup: vi.fn(async () => true),
      saveGroups: vi.fn(async () => true),
      get: (g: string, f: string) => settings[g]?.[f],
    } as Model & { saveField: ReturnType<typeof vi.fn> };
  };
  const render = (words: unknown, view: Record<string, unknown> | null = null) =>
    renderToStaticMarkup(createElement(VoiceWordsBlock, { model: model(words), view }));

  it("one row per kind, then the wake word; the hint says they work everywhere", () => {
    const html = render({});
    expect(WORD_ROWS.map((r) => r.label)).toEqual(["Send", "End (send and stop)", "Start over", "Stop listening", "Stop talking"]);
    expect([...html.matchAll(/data-words-kind="(\w+)"/g)].map((m) => m[1])).toEqual(["send", "end", "clear", "stop", "hush", "wake"]);
    expect(html).toContain('<h3 class="set-section-title">Voice words</h3>');
    expect(html).toMatch(/this computer(&#x27;|')s mic, the phone page and the\s+wake word/);
    expect(html).toContain('id="voice-words"');
  });

  it("shows the defaults faded while a kind has none of its own, the wake word as the placeholder; no Reset", () => {
    const html = render({});
    expect(html).toContain('<span class="voice-chip is-default" role="listitem" title="A default — add your own to replace it">over</span>');
    expect(html).toContain('placeholder="MindFlock"');
    expect(html).not.toContain("voice-words-reset");
    // The server's defaults, when it says them.
    expect(render({}, { words_default: { ...W.BUILTIN_VOICE_WORDS, send: ["copy"], wake: "hey you" } })).toContain('placeholder="Hey You"');
  });

  it("a kind's own phrases are chips with ×, the defaults gone; Reset to defaults appears", () => {
    const html = render({ send: ["Roger"], wake: "hey flock" });
    expect(html).toMatch(/<span class="voice-chip" role="listitem">roger<button[^>]*aria-label="Remove roger"/);
    expect(html).not.toContain('is-default" role="listitem" title="A default — add your own to replace it">over<');
    expect(html).toContain('value="hey flock"');
    expect(html).toContain('id="voice-words-reset"');
  });

  it("a viewer who may not configure sees it locked", () => {
    const html = render({ send: ["roger"] }, { configure_allowed: false });
    expect(html).toContain("Only the owner of this computer can change them.");
    expect(html).not.toContain("voice-words-reset");
    expect(html).toMatch(/id="voice-words-send"[^>]*disabled=""/);
  });

  it("saves only what differs from the defaults ({} = all defaults)", () => {
    const d = W.BUILTIN_VOICE_WORDS;
    expect(withWord({}, "send", ["roger"], d)).toEqual({ send: ["roger"] });
    expect(withWord({ send: ["roger"] }, "send", [], d)).toEqual({});
    expect(withWord({ send: ["roger"] }, "send", ["over"], d)).toEqual({});
    expect(withWord({ send: ["roger"] }, "wake", "", d)).toEqual({ send: ["roger"] });
    expect(withWord({}, "wake", "mindflock", d)).toEqual({});
    expect(withWord({}, "wake", "hey flock", d)).toEqual({ wake: "hey flock" });
    expect(savedWords({ send: ["  Roger "], end: [], wake: "", junk: 1 })).toEqual({ send: ["roger"] });
    expect(savedWords(null)).toEqual({});
  });

  it("refuses what the server would: 6 per kind, 1–40 ASCII characters, no repeats, a wake word of one or two words of letters, 3–20 characters", () => {
    expect(W.phraseError("roger", [])).toBe("");
    expect(W.phraseError("  ", [])).toBe("Type a word or a phrase.");
    expect(W.phraseError("?!", [])).toBe("Type a word or a phrase.");
    expect(W.phraseError("x".repeat(41), [])).toBe("At most 40 characters.");
    expect(W.phraseError("Roger", ["roger"])).toBe("Already in the list.");
    expect(W.phraseError("seven", ["a", "b", "c", "d", "e", "f"])).toMatch(/^At most 6 phrases/);
    expect(W.wakeError("")).toBe("");
    expect(W.wakeError("hey flock")).toBe("");
    expect(W.wakeError("hey there flock")).toMatch(/^One or two words/);
    expect(W.wakeError("x".repeat(21))).toBe("3–20 characters.");
    expect(W.wakeError("hi")).toBe("3–20 characters.");
    expect(W.wakeError("flock 2")).toBe("Letters only — one or two words.");
    expect(W.phraseError("über", [])).toBe("Letters, digits, spaces, apostrophes and commas only.");
    expect(W.phraseError("over, out", [])).toBe("");
  });

  it("is on the Voice screen after This computer's mic; GET /api/voice is re-read after a save and its words applied", () => {
    const voice = src("components/settings/screens/Voice.tsx");
    expect(voice).toMatch(/<MicPrefs \/>\s*<VoiceWordsBlock model=\{model\} view=\{view\} \/>/);
    expect(voice).toMatch(/api<VoiceView>\("\/api\/voice"\)\s*\.then\(\(v\) => \{[\s\S]*?applyVoiceView\(v\);/);
    // The re-read follows the settings document — which every save replaces.
    expect(voice).toContain("}, [active, doc, reread]);");
    expect(src("components/settings/screens/VoiceWords.tsx")).toContain('model.saveField("voice", "words", next as unknown as Json)');
    expect(src("components/settings/screens/VoiceWords.tsx")).toContain('void model.saveField("voice", "words", {});');
  });

  it("Settings search finds it by its words", async () => {
    const { SETTINGS_SEARCH } = await import("../components/palette/settingsSearch");
    const e = SETTINGS_SEARCH.find((x) => x.anchor === "#voice-words")!;
    expect(e.key).toBe("voice");
    for (const t of ["keywords", "voice words", "over", "wake word"]) expect(e.terms).toContain(t);
  });
});

describe("the Voice tour", () => {
  it("eight steps, each on a real element of the mic, the session, the rail, the card or the options", () => {
    const steps = TOURS.voice.steps;
    expect(steps.map((s) => s.target)).toEqual([
      '[data-tour="voice-mic"], #mic-btn',
      ".pane.focused .agent-term, .pane.focused, #grid",
      '#mic-pill, [data-tour="voice-mic"], #mic-btn',
      '[data-tour="voice-mic"], #mic-btn',
      '[data-tour="session-row"], li.inst, #sidebar',
      "#ask-mic, #notif-btn",
      "#mic-wake-row",
      "#mic-replies-row",
    ]);
    expect(steps.map((s) => s.screen ?? "")).toEqual(["", "", "", "", "", "", "mic-pop", "mic-pop"]);
    const mic = src("components/TopBarMic.tsx");
    for (const hook of ['id="mic-btn"\n          data-tour="voice-mic"', 'id="mic-pill"', 'id="mic-wake-row"', 'id="mic-replies-row"'])
      expect(mic).toContain(hook);
    expect(src("components/dialogs/AskDialog.tsx")).toContain('id="ask-mic"');
    expect(src("components/NotificationsBell.tsx")).toContain('id="notif-btn"');
  });

  it("speaks the current words and the Talk hotkey", () => {
    const def = voiceTourSteps(W.BUILTIN_VOICE_WORDS, "Ctrl+T");
    expect(def[0].body).toContain("press Ctrl+T");
    expect(def[2].title).toBe("Say “over” to send");
    expect(def[3].body).toBe("Say “over and out” to send what is left and stop, or “disregard” to take back the message and start it over.");
    expect(def[6].body).toContain("“MindFlock”");
    W.setVoiceWords(CUSTOM);
    // TOURS.voice reads the words each time.
    expect(TOURS.voice.steps[2].title).toBe("Say “send it” to send");
    expect(TOURS.voice.steps[3].body).toContain("“roger and out”");
    expect(TOURS.voice.steps[3].body).toContain("“scratch”");
    expect(TOURS.voice.steps[6].body).toContain("“Hey Flock”");
    expect(TOURS.voice.steps[7].body).toContain("“pipe down”");
  });

  it("opens by itself on the mic's first use — hints on, nothing else touring, not seen yet", () => {
    const base = { seen: [] as string[], id: "voice" as const, pulled: true, hints: true, busy: false };
    expect(firstUseTourDue(base)).toBe(true);
    expect(firstUseTourDue({ ...base, seen: ["voice"] })).toBe(false);
    expect(firstUseTourDue({ ...base, pulled: false })).toBe(false);
    expect(firstUseTourDue({ ...base, hints: false })).toBe(false);
    expect(firstUseTourDue({ ...base, busy: true })).toBe(false);
  });

  it("openTourOnFirstUse opens it once, and closing it marks it seen", async () => {
    vi.useFakeTimers();
    (globalThis as Record<string, unknown>).window = globalThis;
    try {
      await Promise.resolve(); // the first prefs pull (mocked as done)
      useUi.setState({ toursSeen: [], hintsEnabled: true, coachTour: null, tourOpen: false });
      expect(openTourOnFirstUse("voice")).toBe(true);
      vi.runAllTimers();
      expect(useUi.getState().coachTour).toEqual({ id: "voice", step: 0 });
      useUi.getState().closeCoachTour();
      expect(useUi.getState().toursSeen).toContain("voice");
      expect(openTourOnFirstUse("voice")).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });

  it("the mic's starts by a person (click, press, hotkey) go through userStart; the options follow the tour", () => {
    const mic = code("components/TopBarMic.tsx");
    expect(mic).toMatch(/const userStart = \(into[^)]*\) => \{\s*const d = start\(into\);\s*if \(d\) openTourOnFirstUse\("voice"\);/);
    expect(mic.match(/userStart\(pressedField\.current\)/g)).toHaveLength(2);
    expect(mic).toContain("userStart(null);");
    expect(mic).toContain("const keyRef = useRef({ hold, start: userStart, finish });");
    // The wake word and hands-free starts are not a first use.
    expect(mic).toContain('const d = start(textField(document.activeElement), { hint: WAKE_HINT, idle: false });');
    expect(mic).toContain('TOURS.voice.steps[s.coachTour.step]?.screen === MIC_POP_SCREEN');
    expect(mic).toContain('!el?.closest(".coach-card")');
  });

  it("“Show me how”: the ? in the mic's options and the link in Settings → Voice replay it", () => {
    const mic = src("components/TopBarMic.tsx");
    expect(mic).toMatch(/className="tour-btn mic-pop-help"[\s\S]*?setMenuOpen\(false\);\s*replayTour\("voice"\);/);
    expect(src("components/settings/screens/Voice.tsx")).toMatch(/id="voice-tour-link"[^>]*onClick=\{\(\) => replayTour\("voice"\)\}>\s*Show me how/);
    expect(src("components/onboarding/replayTour.ts")).toMatch(/id === "voice"\) \{[\s\S]*?ui\.closeDialog\(\);/);
  });
});
