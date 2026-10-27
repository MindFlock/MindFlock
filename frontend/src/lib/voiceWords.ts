/** The voice words: what the mic listens for at the end of a phrase, saved as
 * `voice.words` (Settings → Voice → Voice words; synced across the person's
 * devices) and reported by GET /api/voice as `words` (the effective ones,
 * the defaults overlaid) and `words_default`:
 *  - `send` — send the draft, listening goes on ("over");
 *  - `end` — send what is pending and stop listening ("over and out");
 *  - `clear` — take back what this message typed ("disregard");
 *  - `stop` — stop listening, the words left unsent ("stop listening"…);
 *  - `hush` — the app stops talking, listening goes on ("stop talking"…);
 *  - `wake` — the word that opens the mic from standby ("mindflock").
 *
 * This module holds the words the page uses now: the built-in defaults until
 * GET /api/voice answers (at the mic's mount, and after every Settings save),
 * then the server's. The matchers (lib/voiceDesktop.ts) rebuild when
 * {@link voiceWordsVersion} moves, and every hint that names a word reads
 * {@link voiceWords} when it is drawn — so a saved word works, and is shown,
 * at once. Pure but for that one store: no React, no DOM. */

import { api } from "../api/client";

export type PhraseKind = "send" | "end" | "clear" | "stop" | "hush";
export const PHRASE_KINDS: readonly PhraseKind[] = ["send", "end", "clear", "stop", "hush"];

export interface VoiceWords {
  send: string[];
  end: string[];
  clear: string[];
  stop: string[];
  hush: string[];
  wake: string;
}

/** Until the server says: the same defaults as its `words_default`
 * (backend/web/core voice_words), which replace these once it answers. */
export const BUILTIN_VOICE_WORDS: VoiceWords = {
  send: ["over"],
  end: ["over and out", "over, out"],
  clear: ["disregard"],
  stop: ["stop", "stop listening", "mic off", "pause listening", "that's all", "done talking"],
  hush: ["stop talking", "be quiet", "quiet", "hush", "shut up", "enough"],
  wake: "mindflock",
};

/** The older wordings a main phrase brings along, as the server's router
 * hears them: while "stop listening" is in the stop words, "stop recording"
 * stops too; drop the main phrase and its kin go with it. Matching only —
 * never shown, never saved. */
export const PHRASE_KIN: Readonly<Partial<Record<PhraseKind, Readonly<Record<string, readonly string[]>>>>> = {
  stop: {
    "stop listening": ["stop recording", "stop dictating", "stop the mic", "stop the microphone"],
    "mic off": ["microphone off", "turn mic off", "turn microphone off", "turn off the mic", "turn off the microphone", "turn the mic off"],
    "that's all": ["that is all"],
    "done talking": ["i'm done talking", "i am done talking"],
  },
  hush: {
    "stop talking": ["stop speaking", "stop reading", "stop saying that", "ok stop", "okay stop"],
    enough: ["that's enough", "that is enough", "enough talking", "enough of that"],
  },
};

/** `w`'s phrases of kind `k` with the kin of each ({@link PHRASE_KIN}). */
export function withKin(w: VoiceWords, k: PhraseKind): string[] {
  return [...new Set(w[k].flatMap((p) => [p, ...(PHRASE_KIN[k]?.[p] || [])]))];
}

/** The server's limits (the editor refuses past them): phrases per kind, and
 * characters per phrase; the wake word is one or two words. */
export const WORDS_MAX = 6;
export const PHRASE_MAX_CHARS = 40;
export const WAKE_MAX_WORDS = 2;
export const WAKE_MIN_CHARS = 3;
export const WAKE_MAX_CHARS = 20;

/** A phrase as saved and shown: lowercase, curly apostrophes straightened,
 * one space between words, trimmed ("Over,  Out" → "over, out"). */
export function normPhrase(p: string): string {
  return String(p || "")
    .toLowerCase()
    .replace(/[’`]/g, "'")
    .replace(/\s+/g, " ")
    .trim();
}

/** A phrase's words as the matchers compare them: letters, digits and
 * apostrophes only ("over, out" → ["over", "out"]). */
export function phraseWords(p: string): string[] {
  return normPhrase(p)
    .replace(/[^\p{L}\p{N}' ]+/gu, " ")
    .split(" ")
    .filter((w) => w.replace(/'/g, ""));
}

/** A word compared without its apostrophes ("that's" = "thats"). */
export const wordKey = (w: string) => w.replace(/'/g, "");

/** `raw` (a `words` / `words_default` object, a saved `voice.words`) made
 * whole: each kind a list of normalised, distinct phrases with at least one
 * word, else `fallback`'s; the wake word one or two words, else
 * `fallback`'s. Never throws. */
export function wordsFrom(raw: unknown, fallback: VoiceWords): VoiceWords {
  const r = raw && typeof raw === "object" ? (raw as Record<string, unknown>) : {};
  const out = { ...fallback };
  for (const k of PHRASE_KINDS) {
    const v = r[k];
    if (!Array.isArray(v)) continue;
    const list = [...new Set(v.map((p) => normPhrase(String(p))).filter((p) => phraseWords(p).length))];
    if (list.length) out[k] = list;
  }
  const wake = typeof r.wake === "string" ? normPhrase(r.wake) : "";
  const n = phraseWords(wake).length;
  if (n >= 1 && n <= WAKE_MAX_WORDS) out.wake = wake;
  return out;
}

let defaults: VoiceWords = BUILTIN_VOICE_WORDS;
let current: VoiceWords = BUILTIN_VOICE_WORDS;
let version = 0;
const listeners = new Set<(w: VoiceWords) => void>();

/** The words the mic hears now. */
export function voiceWords(): VoiceWords {
  return current;
}

/** The defaults (the server's once it answered). */
export function voiceWordsDefault(): VoiceWords {
  return defaults;
}

/** Moves whenever the words change: the matchers rebuild on it. */
export function voiceWordsVersion(): number {
  return version;
}

/** Take the words from the server (`words`, `words_default` as GET
 * /api/voice says them). Nothing changes, nothing is told, when they are the
 * same. */
export function setVoiceWords(words: unknown, wordsDefault?: unknown): void {
  const d = wordsDefault === undefined ? defaults : wordsFrom(wordsDefault, BUILTIN_VOICE_WORDS);
  const w = wordsFrom(words, d);
  if (JSON.stringify(w) === JSON.stringify(current) && JSON.stringify(d) === JSON.stringify(defaults)) return;
  defaults = d;
  current = w;
  version++;
  for (const fn of [...listeners]) fn(w);
}

/** Back to the built-in defaults (the tests). */
export function resetVoiceWords(): void {
  setVoiceWords(BUILTIN_VOICE_WORDS, BUILTIN_VOICE_WORDS);
}

/** Called with the new words each time they change; returns the unsubscribe. */
export function onVoiceWords(fn: (w: VoiceWords) => void): () => void {
  listeners.add(fn);
  return () => void listeners.delete(fn);
}

/** GET /api/voice's answer (any part of it): take its words when it has them
 * (an older server has none: the words stay as they are). */
export function applyVoiceView(j: unknown): void {
  const v = j && typeof j === "object" ? (j as { words?: unknown; words_default?: unknown }) : {};
  if (v.words && typeof v.words === "object") setVoiceWords(v.words, v.words_default);
}

/** Re-read the words from GET /api/voice (unreachable: they stay). */
export async function refreshVoiceWords(): Promise<VoiceWords> {
  try {
    applyVoiceView(await api<unknown>("/api/voice"));
  } catch {
    /* the words stay */
  }
  return current;
}

// --- the words as the UI says them --------------------------------------- //

/** “over”. */
export const quoted = (p: string) => "“" + p + "”";

/** The wake word as a name: "mindflock" is “MindFlock”; any other
 * capitalised as the server hints it to the recogniser ("Hey Jarvis"). */
export function wakeName(w: VoiceWords = current): string {
  if (w.wake === "mindflock") return "MindFlock";
  return w.wake.replace(/(^|\s)(\p{L})/gu, (_, a: string, b: string) => a + b.toUpperCase());
}

// --- the editor's checks (the server's limits) ---------------------------- //

/** Why `phrase` can't join `list` ("" when it can). The server takes
 * English letters, digits, spaces, apostrophes and commas. */
export function phraseError(phrase: string, list: readonly string[]): string {
  const p = normPhrase(phrase);
  if (!phraseWords(p).length) return "Type a word or a phrase.";
  if (/[^a-z0-9 ',]/.test(p)) return "Letters, digits, spaces, apostrophes and commas only.";
  if (p.length > PHRASE_MAX_CHARS) return "At most " + PHRASE_MAX_CHARS + " characters.";
  if (list.includes(p)) return "Already in the list.";
  if (list.length >= WORDS_MAX) return "At most " + WORDS_MAX + " phrases — remove one first.";
  return "";
}

/** Why `wake` can't be the wake word ("" when it can; "" = the default). */
export function wakeError(wake: string): string {
  const p = normPhrase(wake);
  if (!p) return "";
  if (/[^a-z ]/.test(p)) return "Letters only — one or two words.";
  const n = phraseWords(p).length;
  if (n > WAKE_MAX_WORDS) return "One or two words — a sentence never wakes it.";
  if (p.length < WAKE_MIN_CHARS || p.length > WAKE_MAX_CHARS)
    return WAKE_MIN_CHARS + "–" + WAKE_MAX_CHARS + " characters.";
  return "";
}
