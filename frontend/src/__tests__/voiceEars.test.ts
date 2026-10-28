/** The ears: a voice word heard a little off still acts, and dictation never
 * does. tests/unit/data/voice_sounds.json is the one fixture the server
 * (core.voice_words.control_kind, test_voice_words.py), the phone
 * (mobile.js, test_mobile_voice_words.py) and this file run, so the three
 * cannot drift: its `words` / `custom` are GET /api/voice's `words` with
 * `heard` (what whisper writes for each default phrase), `keys` pins
 * soundKey, `alike` soundsLike, and each case is one finished utterance and
 * the control kind it ends with — read here the way Dictation.tail reads it:
 * isHushPhrase (the whole phrase), stopTail (a stop phrase of two words or
 * more at the end), parseRadio (send / end / clear at the end). */

import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../api/client", () => ({ api: vi.fn(), apiRaw: vi.fn(), instApi: vi.fn(), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));
vi.mock("../lib/prefsSync", () => ({ firstPrefsPull: Promise.resolve() }));

const W = await import("../lib/voiceWords");
const V = await import("../lib/voiceDesktop");

interface Fixture {
  words: unknown;
  custom: unknown;
  keys: Record<string, string>;
  alike: Array<[string, string, boolean]>;
  cases: Array<{ text: string; kind: string | null; words: "default" | "custom" }>;
}
// The fixture lives with the Python tests (the repo's tests/unit/data).
const fs = (await import(/* @vite-ignore */ "node:" + "fs")) as { readFileSync(p: string, enc: string): string };
const FIXTURE = decodeURIComponent(new URL("../../../tests/unit/data/voice_sounds.json", import.meta.url).pathname);
const F = JSON.parse(fs.readFileSync(FIXTURE, "utf8")) as Fixture;

/** One finished utterance → the control kind it ends with (Dictation.tail's order). */
function kindOf(text: string): string | null {
  if (V.isHushPhrase(text)) return "hush";
  if (V.stopTail(text) !== null) return "stop";
  const r = V.parseRadio(text).kind;
  return r === "over" ? "send" : r === "out" ? "end" : r === "disregard" ? "clear" : null;
}

afterEach(() => W.resetVoiceWords());

describe("the shared fixture", () => {
  it("is what the server serves: every kind, the wake word and `heard`", () => {
    const w = W.wordsFrom(F.words, W.BUILTIN_VOICE_WORDS);
    expect(w.send).toEqual(["over"]);
    expect(w.heard?.send).toEqual(["ova", "ovah", "oh ver", "o ver", "overt"]);
    expect(w.heard?.clear).toContain("this regard");
    expect(w.heard?.wake).toContain("mind lock");
    const c = W.wordsFrom(F.custom, W.BUILTIN_VOICE_WORDS);
    expect(c.send).toEqual(["roger"]);
    expect(c.heard).toEqual({ send: [], end: [], clear: [], stop: [], hush: [], wake: [] });
    expect(F.cases.length).toBeGreaterThanOrEqual(80);
  });

  it("soundKey and soundsLike are the server's, pin for pin", () => {
    for (const [text, key] of Object.entries(F.keys)) expect(W.soundKey(text), text).toBe(key);
    for (const [a, b, want] of F.alike) expect(W.soundsLike(a, b), a + " ~ " + b).toBe(want);
  });

  it.each(F.cases.filter((c) => c.words === "default").map((c) => [c.text, c.kind] as const))(
    "the defaults: %j → %j",
    (text, kind) => {
      W.setVoiceWords(F.words, W.BUILTIN_VOICE_WORDS);
      expect(kindOf(text)).toBe(kind);
    }
  );

  it.each(F.cases.filter((c) => c.words === "custom").map((c) => [c.text, c.kind] as const))(
    "the person's words: %j → %j",
    (text, kind) => {
      W.setVoiceWords(F.custom, W.BUILTIN_VOICE_WORDS);
      expect(kindOf(text)).toBe(kind);
    }
  );
});

describe("the heard forms (words.heard)", () => {
  it("fold into the matchers beside the words, and go when the server sends none", () => {
    W.setVoiceWords(F.words, W.BUILTIN_VOICE_WORDS);
    expect(V.parseRadio("fix the bug ova")).toEqual({ kind: "over", text: "fix the bug" });
    expect(V.parseRadio("ship it over an out")).toEqual({ kind: "out", text: "ship it" });
    expect(V.parseRadio("scrap it this regard")).toEqual({ kind: "disregard", text: "scrap it" });
    expect(V.stopTail("fix it stop listing")).toBe("fix it");
    expect(V.isHushPhrase("a nuff")).toBe(true);
    // Before the server answers (or from an older one): the words alone
    // (and what sounds like them: "be quite" is "be quiet" either way).
    W.resetVoiceWords();
    expect(W.voiceWords().heard).toBeUndefined();
    expect(V.parseRadio("fix the bug ova").kind).toBe("");
    expect(V.stopTail("fix it stop listing")).toBeNull();
    expect(V.isHushPhrase("a nuff")).toBe(false);
    expect(V.isHushPhrase("be quite")).toBe(true);
  });

  it("are held back while they may still be said, like the words", () => {
    W.setVoiceWords(F.words, W.BUILTIN_VOICE_WORDS);
    expect(V.holdBack("fix the bug this")).toEqual({ type: "fix the bug", held: "this" });
    expect(V.holdBack("fix the bug this regard")).toEqual({ type: "fix the bug", held: "this regard" });
    expect(V.holdBack("fix the bug this time")).toEqual({ type: "fix the bug this time", held: "" });
    expect(V.holdBack("ship it over an")).toEqual({ type: "ship it", held: "over an" });
    expect(V.controlMaxWords()).toBe(7);
  });

  it("wait whole for the router as the stop and hush words do", () => {
    W.setVoiceWords(F.words, W.BUILTIN_VOICE_WORDS);
    expect(V.mayBeCommand("stop listing")).toBe(true);
    expect(V.mayBeCommand("be quite")).toBe(true);
  });

  it("follow their phrase: dropped with it, kept where it moved", () => {
    W.setVoiceWords({ ...(F.words as object), stop: ["halt"], hush: ["quiet"], heard: { ...(F.words as { heard: object }).heard, stop: [], hush: ["quite", "quiets"] } });
    expect(V.stopTail("fix it stop listing")).toBeNull();
    expect(V.isHushPhrase("quite")).toBe(true);
    // The server's shape is read whole; a kind it leaves out is just empty.
    W.setVoiceWords({ send: ["roger"], heard: { send: ["rodjah"] } });
    expect(W.voiceWords().heard).toEqual({ send: ["rodjah"], end: [], clear: [], stop: [], hush: [], wake: [] });
    expect(V.parseRadio("fix it rodjah").kind).toBe("over");
  });
});

describe("a letter off (soundsLike)", () => {
  it("the person's phrases, whole, at the end only", () => {
    W.setVoiceWords(F.custom, W.BUILTIN_VOICE_WORDS);
    expect(V.parseRadio("fix the bug rodger")).toEqual({ kind: "over", text: "fix the bug" });
    expect(V.parseRadio("fix it, Rojer.")).toEqual({ kind: "over", text: "fix it" });
    expect(V.parseRadio("Rodger")).toEqual({ kind: "over", text: "" });
    expect(V.parseRadio("ship it roger an out")).toEqual({ kind: "out", text: "ship it" });
    expect(V.parseRadio("toss it scrap dat")).toEqual({ kind: "disregard", text: "toss it" });
    expect(V.parseRadio("toss it scrap dat that")).toEqual({ kind: "disregard", text: "toss it" });
    expect(V.isHushPhrase("pipe town")).toBe(true);
    expect(V.isHushPhrase("okay pipe town now")).toBe(true);
    // Mid-sentence, two letters off, another sound: words.
    expect(V.parseRadio("rodger that is a flag").kind).toBe("");
    expect(V.parseRadio("fix it rodjer").kind).toBe("");
    expect(V.parseRadio("fix it rogers").kind).toBe("");
    expect(V.isHushPhrase("pipe gown")).toBe(false);
    expect(V.isHushPhrase("silents")).toBe(false);
    // The stop tail is word for word: a one-word stop word a letter off
    // is the router's business, said on its own.
    expect(V.stopTail("fix it hault listening")).toBeNull();
  });

  it("never reaches the defaults a real word is a letter from", () => {
    W.setVoiceWords(F.words, W.BUILTIN_VOICE_WORDS);
    for (const t of ["it was ever thus", "preheat the oven", "take cover", "make it hover", "we offer it", "the summit meeting", "discard"])
      expect(kindOf(t), t).toBeNull();
  });

  it("a RegExp still: parseRadio's and isHushPhrase's one path, test through exec", () => {
    W.setVoiceWords(F.custom, W.BUILTIN_VOICE_WORDS);
    const M = V.matchers();
    expect(M.radio).toBeInstanceOf(RegExp);
    expect(M.radio.test("fix it rodger")).toBe(true);
    expect(M.radio.test("fix it rodger now")).toBe(false);
    const m = M.radio.exec("fix it, rodger.");
    expect(m && [m[1], m[2]]).toEqual(["fix it", "roger"]);
    expect(M.hush.test("pipe town")).toBe(true);
    expect(M.hush.test("pipe gown")).toBe(false);
  });
});

describe("the dictation corpus still types through untouched", () => {
  it.each([
    "fix the login bug",
    "run the tests submit",
    "hand it over and out of the way",
    "start over the tests then stop",
    "submit the form then wait",
    "the summit meeting is at noon",
    "this regard is important",
    "hover over it and click",
    "go over the bridge",
    "I have concerns in this regard, but go on",
    "it's quite a lot of work",
    "stop listing the files",
    "record it all",
  ])("%j", (text) => {
    W.setVoiceWords(F.words, W.BUILTIN_VOICE_WORDS);
    expect(kindOf(text)).toBeNull();
    // Typed whole: nothing of it is a control phrase or one's start that
    // the hold-back would keep ("stop" alone it keeps, as it always did).
    if (!/\bstop$/.test(text)) expect(V.holdBack(text).held).toBe("");
  });
});
