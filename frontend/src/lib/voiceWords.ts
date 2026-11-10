/** The voice words: what the mic listens for at the end of a phrase, saved as
 * `voice.words` (Settings → Voice → Voice words; synced across the person's
 * devices) and reported by GET /api/voice as `words` (the effective ones,
 * the defaults overlaid) and `words_default`:
 *  - `send` — send the draft, listening goes on ("over");
 *  - `end` — send what is pending and stop listening ("over and out");
 *  - `clear` — take back what this message typed ("disregard");
 *  - `undo` — take back one phrase ("scratch that"): at the end of a phrase
 *    the words said before it in that phrase, said alone the last phrase
 *    typed and not yet sent;
 *  - `stop` — stop listening, the words left unsent ("stop listening"…);
 *  - `hush` — the app stops talking, listening goes on ("stop talking"…);
 *  - `wake` — the word that opens the mic from standby ("mindflock").
 *
 * This module holds the words the page uses now: the built-in defaults until
 * GET /api/voice answers (at the mic's mount, and after every Settings save),
 * then the server's. The matchers (lib/voiceDesktop.ts) rebuild when
 * {@link voiceWordsVersion} moves, and every hint that names a word reads
 * {@link voiceWords} when it is drawn — so a saved word works, and is shown,
 * at once. Pure but for that one store: no React, no DOM.
 *
 * The recogniser does not always write the word said ("over" → "ova",
 * "disregard" → "this regard"), so a match is tolerant in two ways, both
 * the server's (backend/web/core/voice_words.py), so the phone and the
 * router hear alike: `words.heard` lists what whisper writes for each
 * default phrase ({@link heardOf} folds it in beside {@link withKin}), and
 * {@link soundsLike} — a small phonetic key ({@link soundKey}) with an
 * edit-distance margin — lets a phrase the person added ("roger") be heard
 * a letter off ("rodger"), whole, at the end of an utterance only
 * ({@link soundTail}). tests/unit/data/voice_sounds.json pins all three. */

import { api } from "../api/client";

export type PhraseKind = "send" | "end" | "clear" | "undo" | "stop" | "hush";
export const PHRASE_KINDS: readonly PhraseKind[] = ["send", "end", "clear", "undo", "stop", "hush"];

/** What the recogniser writes for the phrases of each kind (and the wake
 * word): the server's `words.heard`, matching only — never shown, never
 * saved. Absent until GET /api/voice says (and from an older server). */
export type HeardWords = Record<PhraseKind | "wake", string[]>;

export interface VoiceWords {
  send: string[];
  end: string[];
  clear: string[];
  /** Take back one phrase — absent from an older server's payload, which
   * then keeps the defaults ({@link wordsFrom}). */
  undo: string[];
  stop: string[];
  hush: string[];
  wake: string;
  heard?: HeardWords;
}

/** Until the server says: the same defaults as its `words_default`
 * (backend/web/core voice_words), which replace these once it answers. */
export const BUILTIN_VOICE_WORDS: VoiceWords = {
  send: ["over"],
  end: ["over and out", "over, out"],
  clear: ["disregard"],
  undo: ["scratch that"],
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

/** The stop forms that are also ordinary English endings ("…so make sure
 * that that's all"), so they stop only said on their own — the default
 * phrases "that's all" and "done talking" with their kin and heard forms
 * (core.voice_words STOP_ALONE; mobile.js VOICE_STOP_ALONE; the same list in
 * all three, tests/unit/test_voice_stop_alone.py). Any one-word stop phrase
 * is whole-only too ({@link stopAlone}). Matching only. */
export const STOP_ALONE: readonly string[] = [
  "stop",
  "that's all", "that is all", "that's al", "that's hall",
  "done talking", "i'm done talking", "i am done talking", "done talkin", "dun talking", "done torking", "then talking",
];

/** Said before a lone stop phrase and still only that phrase ("Okay,
 * that's all."): core.voice_words STOP_FILLERS. */
export const STOP_FILLERS: readonly string[] = [
  "ok", "okay", "um", "umm", "uh", "uhm", "er", "erm", "ah", "hmm", "oh", "so", "well", "alright",
];

/** `phrase` (a stop phrase or form) stops only as the whole phrase: one
 * word, or one of {@link STOP_ALONE}. The person's own phrases of two words
 * or more ("halt listening") end any phrase, like "stop listening". */
export function stopAlone(phrase: string): boolean {
  const ws = phraseWords(phrase).map(wordKey);
  return ws.length === 1 || STOP_ALONE.some((p) => phraseWords(p).map(wordKey).join(" ") === ws.join(" "));
}

/** `w`'s phrases of kind `k` with the kin of each ({@link PHRASE_KIN}). */
export function withKin(w: VoiceWords, k: PhraseKind): string[] {
  return [...new Set(w[k].flatMap((p) => [p, ...(PHRASE_KIN[k]?.[p] || [])]))];
}

/** What the recogniser writes for `w`'s phrases of kind `k` (the server's
 * `words.heard`, {@link HeardWords}), none that is a phrase itself. */
export function heardOf(w: VoiceWords, k: PhraseKind | "wake"): string[] {
  const own: readonly string[] = k === "wake" ? [w.wake] : w[k];
  return (w.heard?.[k] || []).filter((f) => !own.includes(f));
}

/** The server's limits (the editor refuses past them): phrases per kind, and
 * characters per phrase; the wake word is one or two words; heard forms
 * per kind. */
export const WORDS_MAX = 6;
export const HEARD_MAX = 24;
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
  // The heard forms are the server's for THESE words: never a fallback's.
  delete out.heard;
  const h = r.heard && typeof r.heard === "object" ? (r.heard as Record<string, unknown>) : null;
  if (h) {
    const heard = {} as HeardWords;
    for (const k of [...PHRASE_KINDS, "wake"] as const) {
      const v = h[k];
      const list = Array.isArray(v) ? v.filter((p) => typeof p === "string").map(normPhrase).filter((p) => phraseWords(p).length) : [];
      heard[k] = [...new Set(list)].slice(0, HEARD_MAX);
    }
    out.heard = heard;
  }
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
    applyVoiceView(await api<unknown>("/api/voice?warm=0")); // words only: never wakes the voice host
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

// --- the sound of a phrase (backend/web/core/voice_words.py, kept identical) -- //

/** The key's rewrites, in order, on the lower-cased letters and digits of a
 * phrase run together — the server's `_SOUND_RULES`, pattern for pattern:
 * silent starts, "ph" → f, the sh/ch class → X, "th" → 0, c/q/x/g resolved,
 * voiced consonants folded onto their unvoiced twins. */
const SOUND_RULES: ReadonlyArray<[RegExp, string]> = [
  [/^[kgp]n/g, "n"],
  [/^wr/g, "r"],
  [/^x/g, "s"],
  [/^wh/g, "w"],
  [/mb$/g, "m"],
  [/x/g, "ks"],
  [/ph/g, "f"],
  [/sch/g, "sk"],
  [/ch/g, "X"],
  [/sh/g, "X"],
  [/th/g, "0"],
  [/gh$/g, "f"],
  [/gh/g, ""],
  [/ck/g, "k"],
  [/q/g, "k"],
  [/c(?=[eiy])/g, "s"],
  [/c/g, "k"],
  [/dg(?=[eiy])/g, "j"],
  [/g(?=[eiy])/g, "j"],
  [/b/g, "p"],
  [/d/g, "t"],
  [/v/g, "f"],
  [/z/g, "s"],
  [/g/g, "k"],
];

/** The lower-cased letters and digits of `text`, run together ("Over,
 * out!" → "overout"). */
const letters = (text: string) => String(text || "").toLowerCase().replace(/[^a-z0-9]+/g, "");

/** A compact phonetic key of one word or a phrase (the server's
 * `sound_key`): "over" → "ofr", "roger" and "rodger" → "rjr", "this regard"
 * → "0srkrt". Letters that sound alike share a key; vowels after the first
 * letter do not count. "" for no letters. */
export function soundKey(text: string): string {
  let s = letters(text);
  if (!s) return "";
  for (const [rx, to] of SOUND_RULES) s = s.replace(rx, to);
  if (!s) return "";
  return (s[0] + s.slice(1).replace(/[aeiouhwy]/g, "")).replace(/(.)\1+/g, "$1");
}

/** Damerau–Levenshtein distance (adjacent letters swapped count one). */
function edits(a: string, b: string): number {
  const d: number[][] = [];
  for (let i = 0; i <= a.length; i++) d.push([i, ...new Array<number>(b.length).fill(0)]);
  for (let j = 0; j <= b.length; j++) d[0][j] = j;
  for (let i = 1; i <= a.length; i++)
    for (let j = 1; j <= b.length; j++) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      d[i][j] = Math.min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost);
      if (i > 1 && j > 1 && a[i - 1] === b[j - 2] && a[i - 2] === b[j - 1]) d[i][j] = Math.min(d[i][j], d[i - 2][j - 2] + 1);
    }
  return d[a.length][b.length];
}

/** How many letters may differ for a phrase of `n` letters: two from eight
 * up, one from five, none below — a four-letter word is only ever itself. */
const slack = (n: number) => (n >= 8 ? 2 : n >= 5 ? 1 : 0);

/** Is `said` (the last words of an utterance) `phrase` heard a little off
 * (the server's `sounds_like`)? Their letters run together within
 * {@link slack} edits AND one {@link soundKey} (one key edit allowed from
 * five key letters): "rodger" is "roger", "pipe town" is "pipe down" —
 * "hover" and "ever" are not "over", "hold" is not "halt". A phrase of
 * eight letters or more also takes a homophone one letter further off —
 * about as long, with the same key ("sight check" for sitecheck; never a
 * phrase cut short: "over and" is not "over and out"). */
export function soundsLike(said: string, phrase: string): boolean {
  const a = letters(said);
  const b = letters(phrase);
  if (!a || !b) return false;
  if (a === b) return true;
  const d = edits(a, b);
  const n = slack(b.length);
  const ka = soundKey(a);
  const kb = soundKey(b);
  if (d <= n) return ka === kb || (kb.length >= 5 && edits(ka, kb) <= 1);
  return b.length >= 8 && d === n + 1 && Math.abs(a.length - b.length) <= 1 && ka === kb;
}

/** The whole of `text` (its words) sounds like one of `phrases`. */
export function soundsLikeAny(text: string, phrases: readonly string[]): boolean {
  return phrases.some((p) => soundsLike(text, p));
}

/** The last words of `text` that sound like one of `phrases` (the longest
 * phrase tried first, one word more than it has allowed for a split like
 * "rod ger") → the phrase as the person set it and the words said before
 * it (trailing punctuation dropped), else null. `tails`: words that may
 * follow a phrase and still count ("disregard that"); `lead`: a tail that
 * is not the whole utterance must also start with the phrase's first letter
 * (the clear words — a false hit throws a draft away: "in this regard" is
 * dictation, "dis regards" clears). The server's `sound_tail`. */
export function soundTail(
  text: string,
  phrases: readonly string[],
  tails: readonly string[] = [],
  lead = false,
): { before: string; phrase: string } | null {
  const toks = String(text || "").trim().split(/\s+/).filter(Boolean);
  const idx = toks.flatMap((t, i) => (letters(t) ? [i] : []));
  if (!idx.length) return null;
  const ordered = phrases.filter((p) => letters(p)).sort((a, b) => letters(b).length - letters(a).length);
  if (!ordered.length) return null;
  const most = Math.max(...ordered.map((p) => phraseWords(p).length)) + 1;
  const ends = [idx.length];
  if (idx.length > 1 && tails.includes(letters(toks[idx[idx.length - 1]]))) ends.push(idx.length - 1);
  for (const end of ends)
    for (let k = Math.min(most, end); k >= 1; k--) {
      const said = idx.slice(end - k, end).map((i) => letters(toks[i])).join("");
      for (const p of ordered) {
        if (lead && k < end && said[0] !== letters(p)[0]) continue;
        if (soundsLike(said, p)) {
          const before = toks.slice(0, idx[end - k]).join(" ").replace(/[\s,.!?]+$/, "");
          return { before, phrase: p };
        }
      }
    }
  return null;
}

// --- a phrase said TO MindFlock: it starts with the wake word (the server's
// voice_words.wake_at, kept identical) --------------------------------------- //

/** Said before the wake word and still addressing it ("hey MindFlock, …"). */
export const WAKE_LEADS: readonly string[] = ["hey", "ok", "okay", "please", "ask"];
/** The default wake word's forms that address MindFlock with no pause after
 * them (none is an ordinary phrase); every other form the recogniser writes
 * ("my flock", "mind block") only with a pause mark right after it ("My
 * flock, open settings."). The server's `WAKE_PLAIN`. */
export const WAKE_PLAIN: readonly string[] = [
  "mindflock", "mind flock", "mine flock", "minflock", "min flock",
  "mindlock", "mind lock", "mindflog", "mind flog", "minh flock",
];
const PAUSE_AFTER = /[,.:;!?—–-]$/;
/** A token's matcher words: lower case, apostrophes dropped, anything else
 * but letters and digits a break (the server's `_words`). */
const wordsOf = (t: string) =>
  String(t || "")
    .toLowerCase()
    .replace(/['’]/g, "")
    .split(/[^a-z0-9]+/)
    .filter(Boolean);
const tokensOf = (text: string) => String(text || "").trim().split(/\s+/).filter(Boolean);

/** The wake word's names and forms: the person's and, always, the default's. */
function wakeForms(w: VoiceWords): { names: string[]; plain: Set<string>; heard: Set<string> } {
  const own = normPhrase(w.wake || "") || BUILTIN_VOICE_WORDS.wake;
  const names = own === BUILTIN_VOICE_WORDS.wake ? [own] : [own, BUILTIN_VOICE_WORDS.wake];
  const plain = new Set([...WAKE_PLAIN, own].map(letters));
  const heard = new Set(heardOf(w, "wake").map(letters));
  return { names, plain, heard };
}

/** The index of the whitespace token of `text` where the words after a
 * LEADING wake word start, else null: one lead word ({@link WAKE_LEADS})
 * may come first; the wake word as written, as the recogniser writes it
 * (`words.heard.wake`) or one or two words that {@link soundsLike} it — a
 * form other than the wake word itself or {@link WAKE_PLAIN} only with a
 * pause mark after it. A possessive ("MindFlock's …") or the name
 * mid-sentence never counts. The server's `voice_words.wake_at`. */
export function wakeAt(text: string, w: VoiceWords = current): number | null {
  const toks = tokensOf(text);
  const f = wakeForms(w);
  let start = 0;
  if (toks.length > 1 && WAKE_LEADS.includes(wordsOf(toks[0]).join(" "))) start = 1;
  for (const k of [1, 2]) {
    const span = toks.slice(start, start + k);
    if (span.length < k || /['’]/.test(span.join(""))) continue;
    const said = span.map((t) => wordsOf(t).join("")).join("");
    if (!said) continue;
    if (f.plain.has(said)) return start + k;
    const paused = PAUSE_AFTER.test(span[span.length - 1]);
    if (paused && (f.heard.has(said) || f.names.some((n) => soundsLike(said, n)))) return start + k;
  }
  return null;
}

/** The words of `text` after its leading wake word ({@link wakeAt}), stray
 * leading punctuation dropped ("" when nothing follows), else null. */
export function wakeRest(text: string, w: VoiceWords = current): string | null {
  const at = wakeAt(text, w);
  if (at === null) return null;
  return tokensOf(text).slice(at).join(" ").replace(/^[\s:;,.!?—–-]+/, "").trim();
}

/** A phrase said to MindFlock: it starts with the wake word and says more. */
export function isWakePhrase(text: string, w: VoiceWords = current): boolean {
  return !!wakeRest(text, w);
}

/** Could the phrase that starts with `text` (a streamed guess, its pause
 * marks not written yet) be said to MindFlock? It starts with a wake form,
 * pause or not, or is still the start of one ("my", "hey mind"): its
 * guess waits whole, so a command is never typed only to be taken back. */
export function mayBeWake(text: string, w: VoiceWords = current): boolean {
  if (wakeAt(text, w) !== null) return true;
  const toks = tokensOf(text).filter((t) => wordsOf(t).length);
  if (!toks.length) return false;
  const f = wakeForms(w);
  const forms = [...WAKE_PLAIN, ...f.names, ...heardOf(w, "wake")].map(wordsOf);
  let s = 0;
  if (WAKE_LEADS.includes(wordsOf(toks[0]).join(" "))) s = 1;
  const rest = toks.slice(s).map((t) => wordsOf(t).join(""));
  // Only "hey" / "please" so far: typed (a prompt starts so as often).
  if (!rest.length) return false;
  if (/['’]/.test(toks.slice(s, s + 2).join(""))) return false;
  const one = rest[0];
  // A whole form in one word ("mindflock"), or one that sounds like it.
  if (forms.some((p) => p.join("") === one) || f.names.some((n) => soundsLike(one, n))) return true;
  // Alone so far, the first word of a form ("my", "mind"): the next decides.
  if (rest.length < 2) return forms.some((p) => p[0] === one);
  const two = one + rest[1];
  return forms.some((p) => p.join("") === two) || f.names.some((n) => soundsLike(two, n));
}
