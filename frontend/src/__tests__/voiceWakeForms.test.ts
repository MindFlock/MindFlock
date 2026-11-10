/** The wake word said at the START of a phrase while listening, as the
 * recogniser mishears it (the owner's trace, 2026-11-10: "Mindful, I'll
 * open settings.", "mindfulout open intake.", "Mindful luck. Tell me what's
 * going on with,", "Line flock." were typed as words). lib/voiceWords
 * wakeAt / wakeRest take the family the server's standby judge takes
 * (wakeLike) before a request (wakeAsks) or alone — never an ordinary
 * sentence ("mindful of the deadline"). tests/unit/data/voice_sounds.json
 * `wake` is the one table and case list the server (test_wake_rest.py), the
 * phone (mobile.js voiceWakeAt) and this file run; `kin` and `words` pin
 * PHRASE_KIN and BUILTIN_VOICE_WORDS to the server's. */

import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../api/client", () => ({ api: vi.fn(), apiRaw: vi.fn(), instApi: vi.fn(), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));
vi.mock("../lib/prefsSync", () => ({ firstPrefsPull: Promise.resolve() }));

const W = await import("../lib/voiceWords");

interface WakeFixture {
  leads: string[];
  plain: string[];
  sound: Record<string, string>;
  tails: string[];
  near: number;
  mine: string[];
  asks: string[];
  ask_fillers: string[];
  ask_you: string[];
  like: Array<[string[], string]>;
  at: Array<[string, string | null]>;
}
interface Fixture {
  words: Record<string, unknown> & { heard: unknown };
  kin: Record<string, Record<string, string[]>>;
  wake: WakeFixture;
}
const fs = (await import(/* @vite-ignore */ "node:" + "fs")) as { readFileSync(p: string, enc: string): string };
const FIXTURE = decodeURIComponent(new URL("../../../tests/unit/data/voice_sounds.json", import.meta.url).pathname);
const F = JSON.parse(fs.readFileSync(FIXTURE, "utf8")) as Fixture;
const K = F.wake;

afterEach(() => W.resetVoiceWords());

describe("the tables are the server's", () => {
  it("wake tables", () => {
    expect(W.WAKE_LEADS).toEqual(K.leads);
    expect(W.WAKE_PLAIN).toEqual(K.plain);
    expect({ ...W.WAKE_SOUND }).toEqual(K.sound);
    expect(W.WAKE_TAILS).toEqual(K.tails);
    expect(W.WAKE_NEAR).toBe(K.near);
    expect(W.WAKE_MINE).toEqual(K.mine);
    expect(W.WAKE_ASKS).toEqual(K.asks);
    expect(W.WAKE_ASK_FILLERS).toEqual(K.ask_fillers);
    expect(W.WAKE_ASK_YOU).toEqual(K.ask_you);
  });

  it("the built-in words and their kin are the server's defaults", () => {
    const { heard: _heard, ...defaults } = F.words;
    expect({ ...W.BUILTIN_VOICE_WORDS }).toEqual(defaults);
    expect(W.PHRASE_KIN).toEqual(F.kin);
  });
});

describe("wakeLike: the default as the recogniser hears it", () => {
  it.each(K.like.map(([ws, r]) => [ws.join(" "), ws, r] as const))("%s", (_name, ws, reason) => {
    W.setVoiceWords(F.words, W.BUILTIN_VOICE_WORDS);
    expect(W.wakeLike(ws)).toBe(reason);
  });
});

describe("wakeAt / wakeRest: the fixture's phrases", () => {
  it.each(K.at.map(([t, r]) => [JSON.stringify(t), t, r] as const))("%s", (_name, text, rest) => {
    W.setVoiceWords(F.words, W.BUILTIN_VOICE_WORDS);
    expect(W.wakeRest(text)).toBe(rest);
    expect(W.wakeAt(text) === null).toBe(rest === null);
    expect(W.isWakePhrase(text)).toBe(!!rest);
  });
});

describe("mayBeWake: a streamed guess still on its way to a request waits", () => {
  it("holds a misheard wake word while a request may follow", () => {
    W.setVoiceWords(F.words, W.BUILTIN_VOICE_WORDS);
    for (const t of ["Mindful", "Mindful,", "Mindful, I'll", "mindfulout", "Mindful luck", "Line flock", "Mindful, um", "mindful, can you", "Mindful luck. Tell", "Mint"])
      expect(W.mayBeWake(t), t).toBe(true);
  });

  it("lets ordinary dictation through at once", () => {
    W.setVoiceWords(F.words, W.BUILTIN_VOICE_WORDS);
    for (const t of ["mindful of", "mindful of the deadline", "my flag is", "Mindful's open house", "mindset matters", "Mindful, this is", "the flock"])
      expect(W.mayBeWake(t), t).toBe(false);
  });
});
