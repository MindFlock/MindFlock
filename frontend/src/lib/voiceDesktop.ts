/** The desktop mic (components/TopBarMic.tsx): everything that is not DOM.
 *
 * The words are typed into the focused session AS YOU SPEAK: each recognised
 * phrase goes straight into the session's input field — through the
 * server's voice gate (POST /api/instances/{title}/send {voice: true,
 * submit: false, erase, text}, {@link KeyGate}), never raw keys down the
 * terminal socket: the server reads the screen at send time and refuses
 * (409 in_dialog, nothing pressed) while a dialog is up, which this page
 * hears of only 2–4 s later — and the radio words end a phrase:
 *  - "over" — send the draft (Enter, through POST /api/instances/{title}/send
 *    {enter: true}, after the same dialog checks as a send) and keep
 *    listening: the next phrase is the next message;
 *  - "over and out" — send what is pending (nothing, when nothing is) and
 *    stop listening;
 *  - "disregard" — take back what this message typed (one backspace per
 *    character) and keep listening;
 *  - "scratch that" (the `undo` words) — take back one phrase: at the end of
 *    a phrase, the words said before it in that phrase ("fix the login bug
 *    scratch that" types nothing); said alone, the last phrase typed and not
 *    yet sent. Silent: a caption, never a spoken line.
 * "Submit" is an ordinary word (the router still takes it on a select
 * dialog's review screen); stopping with the words left unsent is a stop
 * phrase, Escape, the button / hotkey again, or letting go in "Hold to talk".
 * The phone page (backend/web/static/mobile.js, its VOICE section) holds its
 * draft and sends it whole instead; the desktop also differs in that
 *  - push to talk: a click (or a held button) starts and stops listening, and
 *    stopping leaves the words in the field, unsent — the mic is not on
 *    until you key it; once keyed by a click (or the wake word) it stays on,
 *    no idle timeout, until "over and out" or a stop;
 *  - a dictation's first phrase is first offered to the voice router (POST
 *    /api/voice/route), so "switch to the login fix" or "start a new session
 *    for…" act instead of being typed into the focused session — and so is
 *    any phrase that starts with the wake word ("My flock, open settings."),
 *    mid-message too (lib/voiceWords wakeAt);
 *  - replies are read aloud only when you turn that on (off by default):
 *    "mine" for drafts THIS app submitted by voice, "all" for every
 *    session's finished turn — once per turn, one line at a time through
 *    the one {@link SpeechQueue}, and prefixed with the session's name
 *    unless it is the one you are looking at.
 * And at any point "stop recording" (the stop family, {@link stopTail})
 * stops; the router's numbered sessions, answers and questions are carried
 * out by {@link runVoiceCommand}; with replies on, {@link HandsFree} reads a
 * dialog or a question out and listens for the answer; and the keymap's Talk
 * hotkey ({@link TALK_EVENT}) is push to talk from anywhere.
 *
 * Pieces: {@link parseRadio}, the prefs, {@link Dictation} (the pure
 * state machine), {@link LiveDictation} (one dictation driving a target: the
 * router, the dialog guard, the typing, the submit), {@link ReplyTracker},
 * {@link SpeechQueue}, and the browser adapters (Web Speech / a WAV clip or
 * a stream of chunks → server, speechSynthesis / server audio) that the
 * component wires together.
 * Which engine runs is {@link sttEngine} / {@link ttsEngine}: in the desktop
 * app (Electron) the server's whenever it is ready, in a browser tab the
 * browser's unless Settings say "server". */

import { ApiError, api, apiRaw, instApi } from "../api/client";
import type { Dialog, VoiceHelp } from "../api/types";
import { toast } from "./toast";
import { resamplerName, startWavRecording, type WavRecording } from "./wavRecorder";
import {
  KEEP_STREAM_MS,
  MicKeeper,
  PRE_ROLL_MS,
  audioCtxRunning,
  preloadCapture,
  PRE_ROLL_ONSET_S,
  isLoud,
  noiseFloor,
  pickupLine,
  rmsOf,
  voiceMark,
  voicePass,
  voiceSent,
  withPreRoll,
} from "./voiceMic";
import { applyVoiceView, normPhrase, phraseWords, quoted, voiceWords, voiceWordsVersion, withKin, wordKey, type VoiceWords } from "./voiceWords";
import { pronounce, setPronounce, shapeForSpeech } from "./spokenText";

/** A voice trace line in the server's log (POST /api/voice/trace): what a
 * dictation heard, what it decided, and how a send came back — so "over did
 * not send" can be read off the log instead of guessed. Fire and forget. */
export function voiceTrace(event: string, data: Record<string, unknown> = {}): void {
  try {
    void api("/api/voice/trace", { json: { event, ...data } }).catch(() => {});
  } catch {
    /* no fetch (tests) */
  }
}

// --- the radio words -------------------------------------------------------- //

export type RadioKind = "over" | "out" | "disregard" | "undo" | "";

/** "fix the bug over" → {kind: "over", text: "fix the bug"} (send it, go on
 * listening); "… over and out" (or "over, out") → {kind: "out"} (send it,
 * stop); "… disregard" (or "disregard that") → {kind: "disregard"}; "…
 * scratch that" → {kind: "undo"} (take back this phrase, or alone the last
 * one); else {kind: "", text} — "… submit" too: it is a word like any other.
 * The words are the CURRENT ones (lib/voiceWords: `send`, `end`, `clear`,
 * `undo`), the longest
 * phrase tried first, so "over and out" wins over "over". Case and
 * punctuation do not matter ("Over." from Safari); only the LAST words count
 * — an "over" mid-sentence is a word, and "hangover" is not "over". The shape
 * is mobile.js `voiceDictParse`'s, so both surfaces hear alike. */
export function parseRadio(text: string): { kind: RadioKind; text: string } {
  const t = String(text || "").trim();
  const M = matchers();
  const m = M.radio.exec(t);
  if (!m) return { kind: "", text: t };
  const kind = M.radioKinds.find(([rx]) => rx.test(m[2]))?.[1] || "";
  return { kind, text: m[1].trim() };
}

/** The words of a phrase as the router reads them (voice_router.py `norm`):
 * lowercase, curly quotes straightened, anything but a–z, digits and `'` a
 * space. */
function routerWords(text: string): string[] {
  return String(text || "")
    .toLowerCase()
    .replace(/[’`]/g, "'")
    .replace(/[^a-z0-9' ]+/g, " ")
    .split(" ")
    .filter(Boolean);
}

/** A number as it is spoken or recognised (a slot: "one|first|1|…"). */
const NUMS = [
  "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
  "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth",
  "1", "2", "3", "4", "5", "6", "7", "8", "9",
].join("|");

/** The router's commands as word patterns (backend/web/core/voice_router.py
 * `_CMDS` and `_NEW_RE`): each slot lists the words it takes, and a final "*"
 * takes the rest of the phrase. Every command matches the WHOLE phrase. */
const COMMANDS: string[][][] = [
  "cancel", "never mind", "nevermind", "scratch that", "forget it",
  "switch|go|change|jump to *", "switch|go|change|jump over to *",
  "what|which sessions do i have", "what|which sessions are up|open|running",
  "list sessions *", "list the|my sessions",
  "what's|whats up|open|running", "what is up|open|running",
  "what|who needs me|attention", "what|who needs my attention",
  "what's|whats it doing", "what is it doing", "status",
  "what's|whats happening", "what's|whats going on",
  "summarize|summarise", "summarize|summarise that|it", "what did it say", "summary",
  "say that again", "repeat", "repeat that",
  "voice off", "turn voice off", "turn off voice",
  "stop listening|recording", "stop", "mic|microphone off", "turn mic|microphone off",
  "stop talking|speaking|reading", "be quiet", "quiet", "shut up", "hush", "enough", "that's|thats enough",
  "that's|thats all", "that is all",
  // Focus by name or by the rail's number, or the next / previous row.
  "focus|open|show|select *",
  "next|previous session|one", `session|number|row ${NUMS} *`,
  // Answers to a dialog or a question ("session three yes" targets one).
  "yes|yeah|yep|yup|sure|ok|okay|allow|approve|no|nope|deny|reject|always",
  "yes|yeah always", "always allow *", "don't|dont ask again", "do not ask again",
  "go ahead", "do it", `option|choice ${NUMS}`, NUMS, `the ${NUMS} one`,
  "answer|tell|reply *",
  "what is|what's|whats it asking", "what's|whats|what is the question", "what does it want",
  "new session *", "a|another new session *",
  "start|open|create|make new session *", "start|open|create|make a|another new session *",
  "spin up new session *", "spin up a|another new session *",
  // Scroll the focused session's pane (the server scrolls it: kind `scroll`),
  // and "more" / "keep going" after one.
  "scroll *", "page up|down *", "go|jump|back to the top|bottom *", "top|bottom", "up|down",
  "to the top|bottom", "more", "keep going", "some more", "further",
  // Interrupt the agent (the server presses Escape: kind `interrupt`).
  "interrupt *", "escape", "press|hit escape *", "stop it", "stop that",
  // The session focused before (kind `switch_back`).
  "go|switch|jump|flip|change back *", "back", "last session|one", "the other one", "previous",
  // Which session is this (kind `where`).
  "where am i *", "which|what session is this|that", "which|what session am i in|on", "what am i looking at",
  // The cheat sheet (kind `commands`).
  "what can i say *", "commands", "voice commands", "show|list|read the|my commands *", "what are the commands *",
  "what commands *", "help",
].map((p) => p.split(" ").map((slot) => slot.split("|")));
const ROUTER_LEADS = new Set(["ok", "okay", "hey", "please"]);

/** Could the phrase that starts with `text` be a router command? A streamed
 * first phrase that could waits whole for its end (the router reads whole
 * phrases); one that can't — almost every prompt — is typed as it comes. The
 * router's lead word and trailing "please" are dropped as it drops them. */
export function mayBeCommand(text: string): boolean {
  // A doubled word ("stop stop", "Stop. Stop.") is said once, as the router
  // (voice_router.command) reads it.
  let w = routerWords(text).filter((x, i, a) => i === 0 || x !== a[i - 1]);
  if (w.length > 1 && ROUTER_LEADS.has(w[0])) w = w.slice(1);
  if (w.length > 1 && w[w.length - 1] === "please") w = w.slice(0, -1);
  if (!w.length) return true;
  const keys = w.map(wordKey);
  return COMMANDS.concat(matchers().commands).some((p) => {
    for (let i = 0; i < w.length; i++) {
      if (i >= p.length) return false;
      if (p[i][0] === "*") return true;
      if (!p[i].includes(w[i]) && !p[i].includes(keys[i])) return false;
    }
    return true;
  });
}

/** A whole session-switch command ("switch to the login fix", "focus 3",
 * "next session"): the router can act on it from the guess, before the
 * phrase's end. */
export function isSwitchCommand(text: string): boolean {
  let w = routerWords(text);
  if (w.length > 1 && ROUTER_LEADS.has(w[0])) w = w.slice(1);
  if (w.length > 1 && w[w.length - 1] === "please") w = w.slice(0, -1);
  // A whole scroll ("scroll up", "page down", "scroll to the bottom") or
  // "switch back": acted on as soon as the voice is quiet, like a switch.
  const all = w.join(" ");
  if (/^(?:scroll (?:up|down)(?: a bit| more)?|page (?:up|down)|scroll to the (?:top|bottom)|switch back)$/.test(all)) return true;
  if (/^(?:switch|go|change|jump)$/.test(w[0] || "")) {
    const at = w[1] === "over" ? 2 : 1;
    return w[at] === "to" && w.length > at + 1;
  }
  if (/^(?:focus|select)$/.test(w[0] || "")) return w.length > 1;
  if (/^(?:next|previous)$/.test(w[0] || "")) return w.length === 2 && /^(?:session|one)$/.test(w[1]);
  return w.length >= 2 && /^(?:session|number|row)$/.test(w[0]) && new RegExp("^(?:" + NUMS + ")$").test(w[1]);
}

const bareWord = (w: string) => w.toLowerCase().replace(/[’`]/g, "'").replace(/[^\p{L}\p{N}']+/gu, "");

// --- the matchers, built from the current words (lib/voiceWords) ----------- //

// The sound-alike tolerance (lib/voiceWords: `heard`, soundTail, soundsLikeAny),
// imported here beside the matchers that use it.
import { heardOf, isWakePhrase, mayBeWake, soundTail, soundsLikeAny } from "./voiceWords";

/** The radio words' optional tails: "disregard that", "over it". */
const RADIO_TAILS = ["that", "it", "all", "everything"];
/** Spoken before or after "stop talking" and still a hush: "okay, be quiet
 * please". */
const HUSH_LEADS = ["ok", "okay", "hey", "please"];
const HUSH_ENDS = ["please", "now"];

const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
/** A phrase as a pattern: its words in order, any run of spaces and , . ! ?
 * between them, an apostrophe optional ("that's" = "thats"). */
const phraseRx = (p: string) =>
  phraseWords(p)
    .map((w) => esc(w).replace(/'/g, "['’`]?"))
    .join("[\\s,.!?]+");
/** Longest first (in words, then characters): "over and out" before "over". */
const longestFirst = (a: string, b: string) => phraseWords(b).length - phraseWords(a).length || b.length - a.length;
const keysOf = (p: string) => phraseWords(p).map(wordKey);

/** A RegExp with a second hearing: when the pattern (the words as written
 * and what the recogniser is known to write for them) misses, `also` may
 * still answer — the last words SOUNDING like a phrase (lib/voiceWords
 * soundTail / soundsLikeAny), which no pattern can say. It is a RegExp, so
 * parseRadio and isHushPhrase read it as the one they always had; `test`
 * goes through `exec`. */
class SoundRegExp extends RegExp {
  private readonly also: (text: string) => RegExpExecArray | null;
  constructor(src: string, flags: string, also: (text: string) => RegExpExecArray | null) {
    super(src, flags);
    this.also = also;
  }
  exec(text: string): RegExpExecArray | null {
    return super.exec(text) ?? this.also(text);
  }
}
/** `exec`'s answer for a hearing: the whole text, the words before, the phrase. */
const heardAs = (text: string, before: string, phrase: string): RegExpExecArray =>
  Object.assign([text, before, phrase], { index: 0, input: text, groups: undefined }) as RegExpExecArray;

export interface VoiceMatchers {
  /** The words they were built from ({@link voiceWordsVersion}). */
  v: number;
  /** "<the words before> <a send / end / clear phrase>" at the end — the
   * phrase as written, as the recogniser writes it (`heard`), or as it
   * sounds ({@link SoundRegExp}). */
  radio: RegExp;
  /** Which kind the matched phrase is: the first whole match, longest first. */
  radioKinds: Array<[RegExp, RadioKind]>;
  /** The stop phrases (their older wordings and heard forms too) of two
   * words or more, longest first, as word keys: they end any committed
   * words, word for word ({@link stopSaid}: a letter off). A one-word one ("stop") is the router's, said
   * as a whole phrase — at the end of words it is a word ("make it stop"). */
  stop: string[][];
  /** The stop phrases of two words or more as said (kin and heard forms
   * too), for the sound-alike stop tail ({@link stopTail}). */
  stopSaid: string[];
  hush: RegExp;
  /** Every phrase the hold-back keeps whole while it may still be said. */
  control: string[][];
  /** The longest of {@link control}, in words. */
  maxWords: number;
  /** The stop and hush phrases as router commands (one slot per word). */
  commands: string[][][];
}

/** The matchers for `w` (pure: the tests build their own). The phrases are
 * `w`'s with their kin (lib/voiceWords PHRASE_KIN) and what is heard for
 * them (`w.heard`); the send / end / clear and hush matchers also take the
 * person's phrases a letter off (soundsLike), the stop tail does not. */
export function buildMatchers(w: VoiceWords, v = -1): VoiceMatchers {
  const tails = "(?:[\\s,]+(?:" + RADIO_TAILS.join("|") + "))?";
  const ends = [...w.end, ...heardOf(w, "end")];
  const sends = [...w.send, ...heardOf(w, "send")];
  const clears = [...w.clear, ...heardOf(w, "clear")];
  const others: Array<{ p: string; kind: RadioKind; rx: string }> = [
    ...ends.map((p) => ({ p, kind: "out" as const, rx: phraseRx(p) })),
    ...sends.map((p) => ({ p, kind: "over" as const, rx: phraseRx(p) + tails })),
    ...clears.map((p) => ({ p, kind: "disregard" as const, rx: phraseRx(p) + tails })),
  ].filter((e) => e.rx);
  // The undo words (absent from words built before the kind existed: a
  // test's literal) — less any another kind already says: a person whose
  // clear word is "scratch" keeps "scratch that" (its tail) as a clear.
  const taken = others.map((e) => new RegExp("^(?:" + e.rx + ")$", "iu"));
  const undos = [...(w.undo || []), ...heardOf(w, "undo")].filter((p) => !taken.some((rx) => rx.test(normPhrase(p))));
  const entries: Array<{ p: string; kind: RadioKind; rx: string }> = [
    ...others,
    // "scratch that" carries its own "that": no tails.
    ...undos.map((p) => ({ p, kind: "undo" as const, rx: phraseRx(p) })),
  ]
    .filter((e) => e.rx)
    .sort((a, b) => longestFirst(a.p, b.p));
  const sounded = (text: string): RegExpExecArray | null => {
    // A clear phrase heard a little off must start with its letter unless
    // said alone: a false clear throws the draft away ("… in this regard").
    // An undo stricter still (core.voice_words.control_kind): only as the
    // WHOLE utterance, from its first letter — "let's stretch that" is
    // dictation, and a false undo takes a phrase back.
    const undoHit = soundTail(text, undos.filter((p) => (w.undo || []).includes(p)), [], true);
    const hit =
      soundTail(text, w.end) ||
      soundTail(text, w.send, RADIO_TAILS) ||
      soundTail(text, w.clear, RADIO_TAILS, true) ||
      (undoHit && !/[\p{L}\p{N}]/u.test(undoHit.before) ? undoHit : null);
    return hit ? heardAs(text, hit.before, hit.phrase) : null;
  };
  const radio = entries.length
    ? new SoundRegExp(
        "^(.*?)[\\s,.!?]*(?<![\\p{L}\\p{N}'])(" + entries.map((e) => "(?:" + e.rx + ")").join("|") + ")[.!?\\s]*$",
        "iu",
        sounded
      )
    : /(?!)/;
  const radioKinds = entries.map((e) => [new RegExp("^(?:" + e.rx + ")$", "iu"), e.kind] as [RegExp, RadioKind]);
  // The stop and stop-talking words with their older wordings (PHRASE_KIN)
  // and what is heard for them.
  const stops = [...new Set([...withKin(w, "stop"), ...heardOf(w, "stop")])];
  const hushes = [...new Set([...withKin(w, "hush"), ...heardOf(w, "hush")])];
  const stopAll = [...stops].sort(longestFirst).map(keysOf);
  const stop = stopAll.filter((p) => p.length > 1);
  const stopSaid = stops.filter((p) => phraseWords(p).length > 1);
  const hushKeys = [...hushes].sort(longestFirst).map((p) => keysOf(p).join(" "));
  // A hush a letter off: the whole phrase, its lead and tail word dropped.
  const hushSounded = (keys: string): RegExpExecArray | null => {
    let ws = keys.split(" ").filter(Boolean);
    if (ws.length > 1 && HUSH_LEADS.includes(ws[0])) ws = ws.slice(1);
    if (ws.length > 1 && HUSH_ENDS.includes(ws[ws.length - 1])) ws = ws.slice(0, -1);
    return soundsLikeAny(ws.join(" "), w.hush) ? heardAs(keys, "", w.hush[0]) : null;
  };
  const hush = new SoundRegExp(
    "^(?:(?:" + HUSH_LEADS.join("|") + ") )?(?:" + (hushKeys.map(esc).join("|") || "(?!)") + ")(?: (?:" + HUSH_ENDS.join("|") + "))?$",
    "u",
    hushSounded
  );
  const radioPhrases = (ps: string[]) =>
    ps.map(keysOf).flatMap((k) => [k, ...RADIO_TAILS.map((t) => [...k, t])]);
  const control = [
    ...stopAll,
    ...hushes.map(keysOf),
    ...radioPhrases(sends),
    ...radioPhrases(clears),
    // "scratch" is held while "that" may follow.
    ...undos.map(keysOf),
    ...ends.map(keysOf),
    // "Over and out, that's all": the end phrase, then a stop phrase.
    ...ends.map(keysOf).flatMap((e) => stop.map((s) => [...e, ...s])),
  ].filter((p) => p.length);
  const commands = [...stops, ...hushes].map((p) => phraseWords(p).map((x) => [x, wordKey(x)]));
  return { v, radio, radioKinds, stop, stopSaid, hush, control, maxWords: Math.max(1, ...control.map((p) => p.length)), commands };
}

let built: VoiceMatchers | null = null;
/** The matchers for the words the mic hears now (rebuilt when they change). */
export function matchers(): VoiceMatchers {
  const v = voiceWordsVersion();
  if (!built || built.v !== v) built = buildMatchers(voiceWords(), v);
  return built;
}

/** The longest control phrase, in words: what the hold-back may keep. */
export function controlMaxWords(): number {
  return matchers().maxWords;
}

/** The indexes of `ws`'s real words (a bare "." is not one). */
const wordIndexes = (ws: string[]) => ws.flatMap((w, i) => (bareWord(w) ? [i] : []));

/** A phrase in flight → what may be typed now and what waits: the last words
 * (up to {@link controlMaxWords}) while they are, or could still become, a
 * control phrase — "over", "disregard that", "stop" (…recording), "that is"
 * (…all) — with a bare punctuation token kept with its word. Any other last
 * word is typed at once; a held one goes in once the next word rules the
 * control phrase out ("stop the server"), or the phrase ends on less than one. */
export function holdBack(text: string): { type: string; held: string } {
  const ws = String(text || "").trim().split(/\s+/).filter(Boolean);
  if (!ws.length) return { type: "", held: "" };
  const M = matchers();
  const idx = wordIndexes(ws);
  let n = ws.length;
  for (let k = 1; k <= Math.min(M.maxWords, idx.length); k++) {
    const tail = idx.slice(-k).map((i) => wordKey(bareWord(ws[i])));
    if (M.control.some((p) => p.length >= k && tail.every((w, j) => p[j] === w))) n = idx[idx.length - k];
  }
  return { type: ws.slice(0, n).join(" "), held: ws.slice(n).join(" ") };
}

/** `text` ends with a stop phrase (of two words or more — {@link
 * VoiceMatchers.stop}) word for word, else its last words sound like one
 * ("Dop recording", "stop lisening": lib/voiceWords soundTail; the server's
 * core.voice_words.control_kind reads it the same): the words before it
 * (maybe ""), else null. "fix the bug, stop recording." → "fix the bug,". */
export function stopTail(text: string): string | null {
  const ws = String(text || "").trim().split(/\s+/).filter(Boolean);
  const idx = wordIndexes(ws);
  const M = matchers();
  for (const p of M.stop) {
    if (idx.length < p.length) continue;
    const tail = idx.slice(-p.length);
    if (tail.every((i, j) => wordKey(bareWord(ws[i])) === p[j])) return ws.slice(0, tail[0]).join(" ").replace(/[\s,;:]+$/, "");
  }
  // Each phrase against its own number of words first ("fix it, top
  // listening" keeps "it"), then soundTail's split forms ("stop lis ening").
  for (const p of M.stopSaid) {
    const n = phraseWords(p).length;
    if (idx.length < n) continue;
    const tail = idx.slice(-n);
    if (soundsLikeAny(tail.map((i) => ws[i]).join(" "), [p])) return ws.slice(0, tail[0]).join(" ").replace(/[\s,;:.!?]+$/, "");
  }
  const hit = soundTail(text, M.stopSaid);
  return hit ? hit.before.replace(/[\s,;:]+$/, "") : null;
}

const ANSWER_RE = new RegExp(
  "^(?:(?:um|uh|er|so) )?(?:" +
    [
      "yes|yeah|yep|yup|sure|ok|okay|allow|allow it|approve|go ahead|do it",
      "no|nope|deny|reject|don'?t",
      "always|yes always|yeah always|always allow(?: it| that| this| all)?|(?:don'?t|do not) ask(?: me)? again",
      "(?:option |choice |number |the )?(?:" + NUMS + ")(?: one)?",
    ].join("|") +
    ")(?: please)?$"
);

/** "Stop talking", "be quiet", "quiet", "shut up", "stop reading",
 * "enough", "okay stop"… (the current `hush` words): the app's own voice
 * stops — everything said and queued, every session — and listening goes on.
 * Only ever a WHOLE phrase ("don't stop talking to me" is a prompt); never
 * typed. The server's router answers the same words with kind `hush`. */
export function isHushPhrase(text: string): boolean {
  return matchers().hush.test(phraseWords(text).map(wordKey).join(" "));
}

/** A whole answer to a dialog or a question: yes / no / always / a number,
 * as the router hears them. Only ever the WHOLE dictation counts — "the
 * answer is no, but…" is a prompt. */
export function isAnswerPhrase(text: string): boolean {
  return ANSWER_RE.test(routerWords(text).join(" "));
}

const joinWords = (a: string, b: string) => [a.trim(), b.trim()].filter(Boolean).join(" ");

/** The most context a phrase's stream opens with (chars): the server keeps
 * about 300 of it for the accurate model's prompt. */
export const VOICE_CONTEXT_MAX = 400;

/** The message's words ahead of a phrase, as its stream's `context`: one
 * space for any run of whitespace, and only the last `max` characters, cut
 * at a word boundary (a word the cut falls in is left out whole). */
export function voiceContext(text: string, max = VOICE_CONTEXT_MAX): string {
  const t = String(text || "")
    .replace(/\s+/g, " ")
    .trim();
  if (t.length <= max) return t;
  const cut = t.slice(t.length - max);
  return (t[t.length - max - 1] === " " ? cut : cut.replace(/^\S*/, "")).trim();
}

/** POST /api/voice/stream's body: `context` (the message's words before the
 * phrase, {@link voiceContext}) and `session` (the title its words go to)
 * only when there are any. */
export function streamOpenBody(
  lang: string,
  client: string,
  ctx?: { context?: string; session?: string } | null
): Record<string, string> {
  const body: Record<string, string> = { lang, client };
  const context = voiceContext(ctx?.context || "");
  if (context) body.context = context;
  if (ctx?.session) body.session = ctx.session;
  return body;
}

// --- prefs (per browser: a hand's habit, not a synced setting) -------------- //

export type PttMode = "toggle" | "hold";
/** What is read aloud: nothing; the replies to prompts you sent by voice
 * from this app ("mine"); or every session's finished turn ("all"). */
export type RepliesMode = "off" | "mine" | "all";
export interface VoicePrefs {
  ptt: PttMode;
  /** Off by default on the desktop: the phone is the hands-free one. */
  replies: RepliesMode;
  /** Standby for the wake word (lib/wakeWord): the mic stays open and a lone
   * "MindFlock" starts listening. Off by default; absent reads as off. */
  wake?: boolean;
  /** Spoken confirmations of what YOUR words did ("Sent.", "Cleared.") —
   * {@link CONFIRM_LINES}. On by default, whatever `replies` says; absent
   * reads as on. */
  confirm?: boolean;
  /** The mic's chimes (lib/wakeWord chime): rising as listening starts,
   * falling as it stops. On by default; absent reads as on. */
  sounds?: boolean;
}
export const VOICE_PREFS_KEY = "cs_voice_desktop";
export const DEFAULT_VOICE_PREFS: VoicePrefs = { ptt: "toggle", replies: "off", wake: false, confirm: false, sounds: true };

/** Whether voice is in use on this desktop without the mic being pressed:
 * replies are read out, or standby listens for the wake word. Only then does
 * a load or focus ready the server's voice (`loadVoiceCfg(…, warm)`). */
export function voiceInUse(p: VoicePrefs): boolean {
  return p.replies !== "off" || !!p.wake;
}

/** Whether a voice command that opens something says so out loud
 * ("Opening Settings."): spoken replies or spoken confirmations are on. */
export function announcesActions(p: VoicePrefs): boolean {
  // On by default: the owner asked to hear what a voice command does ("no
  // voice that says the action", 2026-11-10). Silent only with the voice's
  // sounds off and neither spoken replies nor spoken confirmations on.
  return p.sounds !== false || p.replies !== "off" || p.confirm === true;
}

/** Whether something said on this desktop goes to the SERVER's voice: replies
 * read out (or standby, whose switch says the session's name), spoken
 * confirmations, or "Read everything" on the server — with the server's voice
 * the one speaking. Only then does pressing the mic ready that voice:
 * dictation alone never wakes a local voice host (Voice Lab), recognition
 * runs inside the server itself. */
export function speaksOnServer(p: VoicePrefs, cfg: VoiceCfg, electron = isElectron()): boolean {
  if (ttsEngine(cfg, electron) !== "server") return false;
  return voiceInUse(p) || p.confirm !== false || cfg.say_all === true;
}

/** A line the server's voice has not answered within this shows
 * {@link STARTING_VOICE_LINE} — when the voice may be cold
 * ({@link voiceMayBeCold}). A warm neural host takes ~1.4 s per line anyway,
 * so a warm voice never says it is starting. */
export const VOICE_STARTING_MS = 1000;
/** A local voice host stops after this long without a request (Voice Lab's
 * socket proxy: `--exit-idle-time=10min`). */
export const VOICE_COLD_MS = 10 * 60_000;

/** Whether the server's voice may have to start for the next line: nothing
 * said with it on this page yet (`lastOk` 0), or the last line it said was
 * {@link VOICE_COLD_MS} ago or more. */
export function voiceMayBeCold(lastOk: number, now: number): boolean {
  return !lastOk || now - lastOk >= VOICE_COLD_MS;
}

/** The three choices as the mic's popover and Settings → Voice show them. */
export const REPLIES_CHOICES: ReadonlyArray<{ value: RepliesMode; label: string }> = [
  { value: "off", label: "Off" },
  { value: "mine", label: "Replies to what I said by voice" },
  { value: "all", label: "Everything, every session" },
];

/** A stored `replies`: one of the three words, or the old switch (true was
 * today's "mine"). */
export function repliesMode(v: unknown): RepliesMode {
  if (v === "mine" || v === "all") return v;
  return v === true ? "mine" : "off";
}

/** `stored`: `v` is the saved JSON, where the spoken confirmations live
 * under `nods` (they were `confirm`, on by default, so every saved copy
 * carried true: the owner heard "Sent." after "over and out" as an echo,
 * 2026-10-28 — the old key is ignored, so they are off unless chosen
 * since). In memory the field is still `confirm`. */
function normPrefs(v: unknown, stored = false): VoicePrefs {
  const o = v && typeof v === "object" ? (v as Record<string, unknown>) : {};
  return {
    ptt: o.ptt === "hold" ? "hold" : "toggle",
    replies: repliesMode(o.replies),
    wake: o.wake === true,
    confirm: (stored ? o.nods : o.confirm) === true,
    sounds: o.sounds !== false,
  };
}

/** `say: true` on a send from this app (the agent writes a Say line for
 * it): a voice send whenever replies are read, a typed one only in "all". */
export function sayOnSend(byVoice: boolean, p: VoicePrefs = loadVoicePrefs()): boolean {
  return p.replies === "all" || (byVoice && p.replies === "mine");
}

type KV = Pick<Storage, "getItem" | "setItem">;
function storage(): KV | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
}

export function loadVoicePrefs(kv: KV | null = storage()): VoicePrefs {
  let raw: unknown = null;
  try {
    const s = kv?.getItem(VOICE_PREFS_KEY);
    raw = s ? JSON.parse(s) : null;
  } catch {
    raw = null;
  }
  return normPrefs(raw, true);
}

/** Fired on `window` whenever the prefs are saved (detail: the new prefs):
 * the top-bar mic and Settings → Voice both edit them, and each follows the
 * other's change without a reload. */
export const VOICE_PREFS_EVENT = "mindflock:voice-prefs";

/** Save the prefs; replies switched TO "Everything" (or away from it)
 * tells the server too ({@link syncSayAll}) — only on a switch, never on a
 * plain save. */
export function saveVoicePrefs(
  p: VoicePrefs,
  kv: KV | null = storage(),
  sync: (on: boolean) => unknown = syncSayAll
): void {
  const next = normPrefs(p);
  const flip = sayAllFlip(loadVoicePrefs(kv).replies, next.replies);
  try {
    const { confirm, ...rest } = next;
    kv?.setItem(VOICE_PREFS_KEY, JSON.stringify({ ...rest, nods: confirm }));
  } catch {
    /* private window / quota: the choice lasts this page */
  }
  if (flip !== null) void sync(flip);
  try {
    if (typeof window !== "undefined" && typeof CustomEvent !== "undefined")
      window.dispatchEvent(new CustomEvent(VOICE_PREFS_EVENT, { detail: next }));
  } catch {
    /* no window (tests) */
  }
}

// --- "Everything": a Say line on every prompt ------------------------------- //
//
// A Say line is written for a prompt sent through /send with say:true; one
// typed straight into a terminal never asks for it. The server's global
// marker (POST /api/voice/say-all, GET /api/voice `say_all`) makes the prompt
// hook ask EVERY prompt in every session for one — "Everything" turns it on.

/** The caption when this device may not change the marker (a 403: only
 * the owner can) — said once; the pref stays. */
export const SAY_ALL_FORBIDDEN_LINE =
  "Read replies aloud: only the server's owner can ask every prompt for a spoken line — replies sent from here still get one";

/** The marker to post when replies move from `prev` to `next`: on when
 * switched TO "all", off when switched away from it, else null. */
export function sayAllFlip(prev: RepliesMode, next: RepliesMode): boolean | null {
  return (prev === "all") === (next === "all") ? null : next === "all";
}

/** On load: a device on "all" turns the server's marker ON when it is off
 * (true); it never turns it off — another device may be the one listening
 * to everything, and off is only ever an explicit switch away
 * (`sayAllFlip`). Null when nothing is to be posted (an older server sends
 * no `say_all`). */
export function sayAllMismatch(pref: RepliesMode, server: boolean | undefined): boolean | null {
  if (typeof server !== "boolean") return null;
  return pref === "all" && !server ? true : null;
}

/** POST /api/voice/say-all {on}: "forbidden" on a 403 (not the owner). */
export async function postSayAll(on: boolean): Promise<"ok" | "forbidden" | "error"> {
  try {
    await api("/api/voice/say-all", { json: { on } });
    return "ok";
  } catch (err) {
    return err instanceof ApiError && err.status === 403 ? "forbidden" : "error";
  }
}

let sayAllWarned = false;

/** Tell the server the marker; a 403 is "this device can't change it" — a
 * toast once per page, the pref kept. */
export async function syncSayAll(
  on: boolean,
  post: (on: boolean) => Promise<"ok" | "forbidden" | "error"> = postSayAll,
  warn: (line: string) => void = (l) => toast(l)
): Promise<"ok" | "forbidden" | "error"> {
  const r = await post(on);
  if (r === "forbidden" && !sayAllWarned) {
    sayAllWarned = true;
    warn(SAY_ALL_FORBIDDEN_LINE);
  }
  return r;
}

/** Follow the prefs as any surface saves them; returns the unsubscribe. */
export function onVoicePrefs(fn: (p: VoicePrefs) => void): () => void {
  if (typeof window === "undefined") return () => {};
  const h = (e: Event) => {
    const d = (e as CustomEvent).detail as VoicePrefs | undefined;
    fn(d && typeof d === "object" ? normPrefs(d) : loadVoicePrefs());
  };
  window.addEventListener(VOICE_PREFS_EVENT, h);
  return () => window.removeEventListener(VOICE_PREFS_EVENT, h);
}

// --- the push-to-talk hotkey ------------------------------------------------- //

/** The keymap's Talk action (default Ctrl+T, Alt+Space in a browser tab,
 * rebindable) dispatches this
 * on window: `down` once per physical press, `up` on release — and an `up`
 * when the window loses focus. */
export const TALK_EVENT = "mindflock:voice-talk";
export type TalkPhase = "down" | "up";

/** In "Hold to talk", a key let go sooner than this after its press was a
 * tap: the mic stays on (as "Click to talk" would) and the next press
 * stops it. Held longer, letting go stops — push to talk as before. */
export const TAP_HOLD_MS = 350;
/** A press this soon after listening ended starts nothing: the bounce of
 * the press that stopped it, or a double tap — not a new dictation. */
export const TALK_DEBOUNCE_MS = 300;

/** What a hotkey phase does, as the button does it: in "Hold to talk", down
 * starts and up (only after a down that the KEY pressed — a blur's stray up
 * never ends a held button) stops; in "Click to talk", each down starts
 * or stops and up is nothing. Stopping leaves the words in the field.
 * `o.heldMs` (an up: how long the key was down): a hold's tap — under
 * {@link TAP_HOLD_MS} — "latch"es instead (the mic stays on), and with
 * `o.latched` (the key's own tap left it on) the next down finishes. `o.busy`
 * (a dictation still ending, or one just over — {@link TALK_DEBOUNCE_MS}):
 * a down that would start does nothing. */
export function talkKeyAction(
  phase: TalkPhase,
  hold: boolean,
  listening: boolean,
  keyHeld: boolean,
  o: { heldMs?: number; latched?: boolean; busy?: boolean } = {}
): "start" | "finish" | "latch" | null {
  if (!hold) return phase === "down" ? (listening ? "finish" : o.busy ? null : "start") : null;
  if (phase === "down") return listening ? (o.latched ? "finish" : null) : o.busy ? null : "start";
  if (!keyHeld || !listening) return null;
  return o.heldMs !== undefined && o.heldMs < TAP_HOLD_MS ? "latch" : "finish";
}

// --- the dictation state machine ------------------------------------------- //

/** What a step of {@link Dictation} asks for. `text` to type ends a phrase
 * with its trailing space (a phrase still being said has none: its last word
 * may change); `erase`, when set, goes first — the end of the phrase under
 * way as typed so far, which the recognizer has since heard otherwise (never
 * more than that phrase). `text` to erase or drop is everything this
 * dictation typed. */
export type DictOutcome =
  /** Type `text` (after `erase`); listening goes on. `route: false`: it
   * can't be a router command, so it goes straight in. `guess`: the phrase
   * is not over — typed only where it can go now, never past the router, a
   * dialog on screen or a missing session (the phrase's end decides then). */
  | { kind: "type"; text: string; erase?: string; route?: boolean; guess?: boolean }
  /** Push to talk let go, silence: type `text` (if any), stop — the words
   * stay in the field. `stop`: it was a stop phrase ("stop recording"). */
  | { kind: "leave"; text: string; erase?: string; stop?: boolean }
  /** "over": type `text` (if any) and send the draft; `end`: then stop
   * ("over and out", or a dictation that is not {@link
   * Dictation.continuous}), else the next phrase starts the next message.
   * `out`: it was "over and out" (nothing typed: nothing is sent). */
  | { kind: "send"; text: string; erase?: string; end?: boolean; out?: boolean }
  /** "disregard": take back `text`; listening goes on. */
  | { kind: "erase"; text: string }
  /** "scratch that": take back `text` — the phrase it ended (`alone`
   * false), or said alone the last phrase typed and not yet sent (`alone`;
   * "" when there is none). Silent; listening goes on unless `end` (said as
   * push to talk let go). */
  | { kind: "undo"; text: string; alone: boolean; end?: boolean }
  /** Escape: take back `text` and stop. */
  | { kind: "dropped"; text: string }
  /** "Stop talking" (a whole phrase): the app's voice stops, nothing is
   * typed (`erase` takes the phrase's guess back); listening goes on unless
   * `end` (said as push to talk let go). */
  | { kind: "hush"; end?: boolean; erase?: string }
  /** Nothing to do (an empty phrase). */
  | { kind: "continue" }
  /** Not listening: nothing to do. */
  | { kind: "stopped" };

/** Two minutes without a word stops listening (the words stay in the field). */
export const DICT_IDLE_MS = 120_000;
/** A hold released mid-phrase waits this long for the phrase's final. */
export const HOLD_FINAL_WAIT_MS = 1500;
/** "say “over” to send, “over and out” to stop, “disregard” to start over" —
 * the first phrase of each kind, from the current words. */
const radioLine = (w: VoiceWords) =>
  "say " + quoted(w.send[0]) + " to send, " + quoted(w.end[0]) + " to stop, " + quoted(w.clear[0]) + " to start over";
/** The caption before a word: "Say “over” to send, …" in the current words. */
export const dictHint = (w: VoiceWords = voiceWords()) => {
  const line = radioLine(w);
  return line[0].toUpperCase() + line.slice(1);
};
/** The caption before a word when the mic listens for `name`'s answer to a
 * question (the ask pop-up's field, a hands-free listen). */
export const answerHint = (name: string, w: VoiceWords = voiceWords()) => "Answer " + name + " — " + radioLine(w);
/** The caption after push to talk let go (or silence) on a session. */
export const LEFT_LINE = "In the field — press Enter to send";
/** The caption after "scratch that": what was taken back. */
export function undoLine(o: { text: string; alone: boolean }): string {
  const t = o.text.trim();
  if (!t) return o.alone ? "Nothing to take back — go on" : "Scratched — go on";
  const short = Array.from(t).length > 40 ? Array.from(t).slice(0, 39).join("") + "…" : t;
  return "Took back “" + short + "” — go on";
}
/** The caption after a stop phrase with nothing typed. */
export const STOPPED_LINE = "Stopped listening";
/** The caption after "stop talking" — shown {@link HUSH_CAPTION_MS}. */
export const HUSH_LINE = "Quiet.";
export const HUSH_CAPTION_MS = 1000;
/** The mic's tooltip while listening and a line plays (the recorder muted
 * for it): what it hears now is not typed. */
export const MIC_PAUSED_LINE = "Speaking — your mic is paused until it ends";

/** The pill beside the top bar's mic — the one thing the bar says about
 * voice, and only ever one of these: {@link HUSH_LINE} for
 * {@link HUSH_CAPTION_MS} after "stop talking", "Listening…" while the mic
 * is open, "Speaking…" while a line plays, else nothing. Never the words
 * heard, typed or said (they are in the terminal; the options show them),
 * and never "Hearing you…" in words — that is the ring's colour. */
export function micPill(s: {
  listening: boolean;
  speaking: boolean;
  quiet: boolean;
  starting?: boolean;
  warming?: boolean;
}): string {
  if (s.quiet) return HUSH_LINE;
  if (s.listening) return s.warming ? STARTING_MIC_LINE : "Listening…";
  if (s.speaking && s.starting) return STARTING_VOICE_LINE;
  return s.speaking ? "Speaking…" : "";
}

/** The pill while the server's voice is starting ({@link serverSpeaker}'s
 * `onStarting`): a local voice host (Voice Lab) that went idle starts on the
 * first line asked of it and loads its model, which takes a while. */
export const STARTING_VOICE_LINE = "Starting voice…";

/** The pill while the server's recognition loads for the first phrase
 * (`serverRecognizer`'s `onLoading`): local recognition is kept in memory
 * only while someone listens (the standby), so a press after a while idle
 * waits a few seconds for it. */
export const STARTING_MIC_LINE = "Starting the mic…";

/** How often a page renews its listening lease (POST /api/voice/listen) while
 * the standby is on: well inside the server's 180 s lease. */
export const LISTEN_RENEW_MS = 60_000;

/** The page's one listening lease: on while the wake-word standby is on —
 * the server keeps local recognition in memory only while some page holds
 * one — renewed every {@link LISTEN_RENEW_MS}, ended at once when it goes
 * off. Idempotent (the mic renders twice on a Mac). `post` is the request
 * (a seam); it never throws. */
export function listenLeaser(post: (on: boolean) => Promise<unknown> = postListen): {
  set(on: boolean): void;
  readonly on: boolean;
} {
  let on = false;
  let timer: ReturnType<typeof setInterval> | undefined;
  const send = (want: boolean) => void post(want).catch(() => {});
  return {
    set(want: boolean) {
      if (want === on) return;
      on = want;
      clearInterval(timer);
      timer = undefined;
      send(want);
      if (want) timer = setInterval(() => send(true), LISTEN_RENEW_MS);
    },
    get on() {
      return on;
    },
  };
}

function postListen(on: boolean): Promise<unknown> {
  return apiRaw("/api/voice/listen", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ on, client: VOICE_CLIENT_ID }),
  });
}

/** This page's lease ({@link listenLeaser}), driven by the standby. */
export const LISTEN_LEASE = listenLeaser();

/** The keys that turn `before` (typed) into `after`: `erase` — the tail of
 * `before` past the start they share, taken back one backspace per
 * character — then `type`, the tail of `after`. By code point, so "café" or
 * an emoji is one key. "fix the bu" → "fix the bag": erase "u", type "ag". */
export function retype(before: string, after: string): { erase: string; type: string } {
  const a = Array.from(before);
  const b = Array.from(after);
  let i = 0;
  while (i < a.length && i < b.length && a[i] === b[i]) i++;
  return { erase: a.slice(i).join(""), type: b.slice(i).join("") };
}

/** What a phrase's guess can end in ({@link Dictation.tail}). */
export type TailKind = "over" | "out" | "disregard" | "undo" | "stop" | "hush" | "route";

/** A control phrase at the end of the guess acts once the voice has been
 * quiet this long after it — not the recognizer's {@link SILENCE_MS} and its
 * accurate pass. */
export const CONTROL_TAIL_MS = 250;
/** How often the guess's tail is checked against the quiet. */
export const CONTROL_POLL_MS = 50;
/** "Over" at the end of the guess with the voice quiet this long already
 * (not mid-syllable: a meter block at least): the phrase is ended at the
 * recognizer NOW, so its accurate final is on its way while the
 * {@link CONTROL_TAIL_MS} of quiet that makes "over" a radio word is still
 * being waited out — the two overlap instead of stacking. The voice coming
 * back before that quiet makes "over" a plain word again (typed, nothing
 * sent), exactly as if the phrase had not been ended. */
export const OVER_EARLY_QUIET_MS = 100;
/** "Over" from the guess ends the phrase and waits for its /end (the
 * accurate final, retyped first) — at most this long, and less once the
 * finals' round trip is known ({@link overWaitMs}); then the fast words are
 * sent and a later final is ignored. */
export const OVER_FINAL_WAIT_MS = 1500;
/** The least "over" waits for the final (no round trip is under this). */
export const OVER_FINAL_MIN_MS = 300;
/** What a final's round trip costs beyond the server's own pass (/end's
 * `final_ms`): the last chunk, the request, the typing. */
export const FINAL_ROUND_TRIP_MS = 150;

/** The wait for the final, from the finals' typical round trip (ms; null:
 * none known yet — the most): half again as long plus a breath, so a final
 * that takes its usual time is waited for and one that is late is not —
 * within {@link OVER_FINAL_MIN_MS}..{@link OVER_FINAL_WAIT_MS}. The fast
 * words are never held longer for a slow final: a late one is ignored. */
export function overWaitMs(typical: number | null): number {
  if (typical == null || !(typical > 0)) return OVER_FINAL_WAIT_MS;
  return Math.min(OVER_FINAL_WAIT_MS, Math.max(OVER_FINAL_MIN_MS, Math.round(typical * 1.5 + 100)));
}

/** The finals' round trips the typical one is the median of: the last this
 * many (one slow moment moves it not at all; a slow spell in a few phrases). */
export const FINAL_SAMPLES = 7;
/** The fast words stand without waiting for the final ({@link
 * FinalClock.fastStands}) once at least this many finals were heard… */
export const FAST_TRUST_MIN = 3;
/** …of the last this many… */
export const FAST_TRUST_WINDOW = 12;
/** …and at most this share of them changed a word. */
export const FAST_TRUST_MAX_CHANGED = 1 / 3;

/** The words a final is compared on: case, punctuation and spacing aside
 * (the server's `same`). */
export const plainWords = (t: string) =>
  String(t || "")
    .toLowerCase()
    .replace(/[^\p{L}\p{N}' ]+/gu, " ")
    .replace(/\s+/g, " ")
    .trim();

/** What the finals' round trip has been: from a phrase ended at the
 * recognizer for a control word ({@link LiveHooks.endPhrase}) to its /end
 * back with a final in it — the median of the last {@link FINAL_SAMPLES}
 * (the recognizer notes them: it alone knows a final from a timeout, which
 * says nothing of how long a final takes and is never a sample). Until one
 * was measured, the server's own measure of its accurate pass ({@link
 * LiveHooks.finalMs}) with {@link FINAL_ROUND_TRIP_MS} on top stands in.
 * It also keeps whether the last finals changed the fast words ({@link
 * verdict}): when they seldom do, a phrase whose fast words all settled
 * goes without waiting for its final ({@link fastStands}). One per page: a
 * dictation learns from the ones before it. */
export class FinalClock {
  private times: number[] = [];
  private changes: boolean[] = [];
  /** One final came back `ms` after its phrase was ended. */
  note(ms: number): void {
    if (!(ms >= 0) || ms > 60_000) return;
    this.times.push(ms);
    if (this.times.length > FINAL_SAMPLES) this.times.shift();
  }
  /** One final came back: `changed`, it heard other words than the fast tier. */
  verdict(changed: boolean): void {
    this.changes.push(changed);
    if (this.changes.length > FAST_TRUST_WINDOW) this.changes.shift();
  }
  /** The finals lately seldom changed a word ({@link FAST_TRUST_MAX_CHANGED}
   * of at least {@link FAST_TRUST_MIN}): waiting for one buys little. */
  fastStands(): boolean {
    const n = this.changes.length;
    if (n < FAST_TRUST_MIN) return false;
    return this.changes.filter(Boolean).length / n <= FAST_TRUST_MAX_CHANGED;
  }
  /** The typical round trip (ms); `seed`: the server's pass time, used while
   * nothing was measured; null: unknown. */
  typical(seed?: number | null): number | null {
    const n = this.times.length;
    if (n) {
      const s = [...this.times].sort((a, b) => a - b);
      return Math.round(n % 2 ? s[(n - 1) / 2] : (s[n / 2 - 1] + s[n / 2]) / 2);
    }
    return seed != null && seed > 0 ? seed + FINAL_ROUND_TRIP_MS : null;
  }
  reset(): void {
    this.times = [];
    this.changes = [];
  }
}
export const finalClock = new FinalClock();
/** Whether an /end answer's final is the accurate pass's — a sample for
 * {@link finalClock}: it has one, and not Moonshine's own text (`final_by:
 * "moonshine"`: a control-only phrase answered at once, or a pass that
 * timed out — neither says how long the accurate pass takes, nor whether
 * it changes words). */
export const timedFinal = (j: Record<string, unknown> | null | undefined): boolean =>
  !!j && typeof j.final === "string" && j.final_by !== "moonshine";
/** A phrase taken from its guess: its own late words and end are ignored —
 * for at most this long (a phrase whose end never comes). */
export const SWALLOW_MAX_MS = 5000;
/** …but a recognizer that always reports a phrase's end (the server's
 * stream: its /end, or its failure, within the request's 45 s timeout) is
 * waited for that long: a late end after a backlog is the same phrase, and
 * would otherwise be typed and SENT a second time. */
export const SWALLOW_STREAM_MAX_MS = 50_000;

/** `o` with `erase` only when there is one (outcomes stay minimal). */
const withErase = <T extends object>(o: T, erase: string): T & { erase?: string } => (erase ? { ...o, erase } : o);

/** One dictation: begin → heard… → over / cancel / finish / expire, with a
 * record of what it typed (in order) so "disregard" can take exactly that
 * back. Pure, so the radio words, push to talk and Escape are pinned by
 * tests; {@link LiveDictation} carries the outcomes out. A toggle's second
 * click and a hold's release are both {@link finish}: stop, words left in.
 *
 * Words are typed as they are heard, like a phone's keyboard: the phrase
 * under way — a streaming recognizer's committed words and its unstable
 * tail ({@link stream}), or Web Speech's interim words ({@link hearing}) —
 * is typed as a guess, and every new answer corrects it from where it
 * differs ({@link retype}). Its end — {@link flush}, or a final
 * ({@link heard}) — settles it: the radio words, read off the whole phrase,
 * work alike for every engine. What is never typed, not even as a guess: a
 * trailing control phrase or its start ({@link holdBack}: "over",
 * "disregard that", "stop", "that is"…, until the next word says it isn't one), and a
 * first phrase that may be a router command (it waits whole for the router). */
export class Dictation {
  listening = false;
  /** What this dictation typed so far — only what really went in
   * ({@link wrote}, {@link unwrote}): the phrases it settled, then the guess
   * at the phrase under way. A disregard takes it back character by character. */
  typed = "";
  /** Streaming: the phrase's committed words so far (final; the caption
   * shows them settled). */
  held = "";
  /** The words the recognizer may still change: the stream's unstable tail,
   * or Web Speech's interim phrase — muted in the caption, typed as a guess. */
  partial = "";
  /** A first phrase goes past the router (false in a text box). */
  routes = true;
  /** "over" sends and listening goes on (the next phrase starts the next
   * message, {@link next}); false: "over" sends and stops. */
  continuous = false;
  /** A dialog or a question waits on the session: an answer said as the
   * whole dictation goes to the router at once (set before each step). */
  answering = false;
  /** {@link typed}'s length when the phrase under way began (-1: between
   * phrases): what came before it is never corrected. */
  private phraseAt = -1;
  /** Where each phrase this message settled began in {@link typed}, in
   * order: "scratch that" said alone takes back from the last one. A mark
   * past the end of {@link typed} (its phrase never went in) is skipped. */
  private marks: number[] = [];

  begin(): void {
    this.listening = true;
    this.typed = "";
    this.held = "";
    this.partial = "";
    this.phraseAt = -1;
    this.marks = [];
  }

  /** The phrase under way — its committed words and its guess — ends in a
   * control phrase: which, and the whole phrase, else null. "over and out,
   * that's all" is an "out"; a switch command counts only as a message's
   * first phrase (it goes to the router). */
  tail(): { kind: TailKind; text: string } | null {
    if (!this.listening) return null;
    const all = joinWords(this.held, this.partial);
    if (!all) return null;
    if (isHushPhrase(all)) return { kind: "hush", text: all };
    const before = stopTail(all);
    if (before !== null) return { kind: parseRadio(before).kind === "out" ? "out" : "stop", text: all };
    const r = parseRadio(all).kind;
    if (r) return { kind: r, text: all };
    if (this.routes && !this.before && isSwitchCommand(all)) return { kind: "route", text: all };
    return null;
  }

  /** A message was sent and listening goes on: the next words start a new
   * one — nothing typed yet, and its first phrase goes past the router. */
  next(): void {
    this.typed = "";
    this.held = "";
    this.partial = "";
    this.phraseAt = -1;
    this.marks = [];
  }

  /** The caption's settled words: the phrases typed, then the phrase's
   * committed words ({@link partial} is shown apart, muted). */
  get shown(): string {
    return [this.before.trim(), this.held.trim()].filter(Boolean).join(" ");
  }

  /** What was typed before the phrase under way. */
  private get before(): string {
    return this.phraseAt < 0 ? this.typed : this.typed.slice(0, this.phraseAt);
  }

  /** The phrase under way as typed so far (its guess). */
  get guessed(): string {
    return this.phraseAt < 0 ? "" : this.typed.slice(this.phraseAt);
  }

  /** The message's words ahead of the NEXT phrase — the context its stream
   * opens with (POST /api/voice/stream `context`, the accurate model's
   * prompt; {@link voiceContext} caps it). The next phrase opens while this
   * one may still be under way (its final not back): its words count as
   * best known — the committed words and the unsure tail, else its typed
   * guess — with its trailing control phrase never part of it ("over",
   * "disregard": the next words start afresh, ""; "scratch that": this
   * phrase, or alone the last one, taken back). A first phrase the router
   * takes ("next session") is no words of the message. `ahead`: the text
   * before the cursor in the target, this dictation's words in it (a text
   * box's value up to the cursor); absent: what this dictation typed. */
  context(ahead?: string): string {
    if (!this.listening) return "";
    const guessed = this.guessed;
    let base = ahead ?? this.typed;
    if (guessed && base.endsWith(guessed)) base = base.slice(0, base.length - guessed.length);
    const said = joinWords(this.held, this.partial) || guessed.trim();
    if (!said) return base.trim();
    if (stopTail(said) !== null || isHushPhrase(said)) return base.trim();
    const r = parseRadio(said);
    if (r.kind === "over" || r.kind === "out" || r.kind === "disregard") return "";
    if (r.kind === "undo") {
      if (r.text.trim() || guessed) return base.trim();
      const at = [...this.marks].reverse().find((m) => m < this.typed.length);
      const last = at === undefined ? "" : this.typed.slice(at);
      return (last && base.endsWith(last) ? base.slice(0, base.length - last.length) : base).trim();
    }
    const words = holdBack(r.text).type;
    if (this.routes && !this.before.trim() && isSwitchCommand(words)) return base.trim();
    // Said to MindFlock: no words of the message either.
    if (this.routes && isWakePhrase(words)) return base.trim();
    return joinWords(base, words);
  }

  /** Web Speech's interim words: the phrase so far, typed as a guess. */
  hearing(t: string): DictOutcome {
    if (!this.listening) return { kind: "stopped" };
    this.partial = String(t || "").trim();
    return this.guess();
  }

  /** Streaming: the unstable tail, for the caption only (a stream's end or
   * fall-back clears it; {@link stream} types it). */
  unsure(t: string): void {
    if (this.listening) this.partial = t;
  }

  /** Streaming: one answer from the recognizer — words `w` committed since
   * the last one, and the unstable tail `p` after them. A stop phrase (or,
   * while {@link answering}, a lone answer) in the committed words acts now;
   * else the phrase so far is typed as a guess: one correction per answer. */
  stream(w: string, p: string): DictOutcome {
    if (!this.listening) return { kind: "stopped" };
    this.held = joinWords(this.held, w);
    this.partial = String(p || "").trim();
    // "Stop talking" waits for the phrase's end ("quiet mode…" is a prompt).
    const ctl = this.control(this.held, false);
    if (ctl) return ctl;
    return this.guess();
  }

  /** Streaming: committed words and no unstable tail. */
  words(w: string): DictOutcome {
    return this.stream(w, "");
  }

  /** Streaming: the phrase ended with its last committed words — with the
   * held ones, the phrase's final ({@link heard}: radio words and all).
   * `final`: the whole phrase re-heard by the server's accurate model, when
   * it differs from the fast words — the phrase ends as THAT (its whole
   * guess corrected, never past the phrase's start) and the radio words, a
   * stop phrase and the router read it, not the fast guess. */
  flush(w: string, final?: string, radio = true): DictOutcome {
    if (!this.listening) return { kind: "stopped" };
    return this.heard(final?.trim() ? final.trim() : joinWords(this.held, w), radio);
  }

  /** `text` went into the field. */
  wrote(text: string): void {
    this.typed += text;
  }

  /** `text`, the end of what was typed, was taken back. */
  unwrote(text: string): void {
    if (text && this.typed.endsWith(text)) this.typed = this.typed.slice(0, this.typed.length - text.length);
  }

  /** A phrase is under way (its guess may be typed). */
  get inPhrase(): boolean {
    return this.phraseAt >= 0;
  }

  /** Keys counted as typed never went in (the server's voice gate refused
   * them, {@link KeyGate}): `text`, the end of {@link typed}, is not in the
   * field, and `erased`, which they would have taken back, still is.
   * Returns the part of `text` from phrases already over — the phrase under
   * way corrects itself at its end against what really went in (its guess
   * is now only the part that did). */
  untyped(text: string, erased: string): string {
    const base = text && this.typed.endsWith(text) ? this.typed.slice(0, this.typed.length - text.length) : this.typed;
    const guess = this.phraseAt < 0 ? 0 : this.typed.length - this.phraseAt;
    const over = this.phraseAt < 0 ? text : guess >= text.length ? "" : text.slice(0, text.length - guess);
    this.typed = base + erased;
    if (this.phraseAt >= 0 && guess <= text.length) this.phraseAt = this.typed.length;
    return over;
  }

  /** One final phrase: the phrase under way ends as `phrase` (its guess
   * corrected to it, or taken back for a control word). `radio` false: its
   * radio words are plain words (an "over" the voice went on after). */
  heard(phrase: string, radio = true): DictOutcome {
    voiceTrace("heard", { phrase, radio, listening: this.listening, continuous: this.continuous, typed: this.typed.length });
    if (!this.listening) return { kind: "stopped" };
    const ctl = this.control(phrase, true);
    if (ctl) return ctl;
    const p = radio ? parseRadio(phrase) : { kind: "" as RadioKind, text: String(phrase || "").trim() };
    switch (p.kind) {
      case "disregard":
        return { kind: "erase", text: this.erase() };
      case "undo":
        return this.undo(p.text);
      case "over":
      case "out": {
        const end = p.kind === "out" || !this.continuous;
        if (end) this.listening = false;
        const s = this.settle(p.text ? p.text + " " : "");
        if (p.kind === "out") return { kind: "send", ...s, end, out: true };
        return end ? { kind: "send", ...s, end } : { kind: "send", ...s };
      }
      default: {
        const s = this.settle(p.text ? p.text + " " : "");
        return s.text || s.erase ? { kind: "type", ...s } : { kind: "continue" };
      }
    }
  }

  /** The toggle's second click, the hold's release: stop, the words left in
   * the field ("… over" as its last words still sends). A phrase still in
   * flight (no final yet) counts — it was said, and its guess is in. */
  finish(): DictOutcome {
    if (!this.listening) return { kind: "stopped" };
    const tail = joinWords(this.held, this.partial);
    const out = tail || this.phraseAt >= 0 ? this.heard(tail) : ({ kind: "continue" } as const);
    this.listening = false;
    if (out.kind === "type") return withErase({ kind: "leave" as const, text: out.text }, out.erase || "");
    if (out.kind === "erase") return { kind: "dropped", text: out.text };
    if (out.kind === "undo") return { ...out, end: true };
    if (out.kind === "continue") return { kind: "leave", text: "" };
    if (out.kind === "hush") return withErase({ kind: "hush" as const, end: true }, out.erase || "");
    if (out.kind === "send") return { ...out, end: true };
    return out;
  }

  /** Escape: "disregard", and stop. */
  cancel(): DictOutcome {
    if (!this.listening) return { kind: "stopped" };
    this.listening = false;
    return { kind: "dropped", text: this.erase() };
  }

  /** {@link DICT_IDLE_MS} of silence, or the recognizer gave up: stop, the
   * words stay where they are. */
  expire(): DictOutcome {
    if (!this.listening) return { kind: "stopped" };
    this.listening = false;
    const all = joinWords(this.held, this.partial);
    if (isHushPhrase(all)) return withErase({ kind: "hush" as const, end: true }, this.settle("").erase || "");
    // The phrase in flight goes in (a radio word among it does nothing) —
    // but not one that ended in "scratch that".
    const r = parseRadio(all);
    const rest = r.kind === "undo" ? "" : r.text;
    return { kind: "leave", ...this.settle(rest ? rest + " " : "") };
  }

  /** The phrase under way, typed as far as it may be now. */
  private guess(): DictOutcome {
    if (this.phraseAt < 0) this.phraseAt = this.typed.length;
    const all = joinWords(this.held, this.partial);
    // A first phrase that may be a command waits whole for the router —
    // asked of the words that would be typed, the radio word held back, as
    // its end asks it ({@link settle}): "do it over" is "do it", an answer,
    // so nothing is typed only to be taken back and typed again.
    // So does a phrase said to MindFlock ("MindFlock, open settings"),
    // wherever it falls in the message.
    const hb = holdBack(all);
    const want = this.mayRoute(hb.type) ? "" : hb.type;
    const fix = retype(this.guessed, want);
    if (!fix.erase && !fix.type) return { kind: "continue" };
    return withErase({ kind: "type" as const, text: fix.type, route: false, guess: true }, fix.erase);
  }

  /** The phrase under way ends as `text`: the keys from its guess to that.
   * A first phrase the router may take is taken back whole first, so the
   * router reads it before any of it is in the field. */
  private settle(text: string): { text: string; erase?: string } {
    const guessed = this.guessed;
    const whole = this.mayRoute(text);
    if (text.trim()) this.marks.push(this.phraseAt < 0 ? this.typed.length : this.phraseAt);
    this.phraseAt = -1;
    this.held = "";
    this.partial = "";
    const fix = whole ? { erase: guessed, type: text } : retype(guessed, text);
    return withErase({ text: fix.type }, fix.erase);
  }

  /** The phrase `text` may be the router's, so its guess waits whole: a
   * message's first phrase that may be a command ({@link mayBeCommand}), or
   * a phrase that starts with the wake word ({@link mayBeWake}) anywhere in
   * the message — "MindFlock, open settings" after words already typed is
   * the router's too ({@link LiveDictation}'s put). Never in a text box. */
  private mayRoute(text: string): boolean {
    return this.routes && ((!this.before && mayBeCommand(text)) || mayBeWake(text));
  }

  /** The control phrases: "stop talking" said as a whole phrase (`whole`:
   * the phrase is over), the stop family any committed words can end with
   * (stop now; the words before it go in) and, while {@link answering}, an
   * answer said as the whole dictation (to the router at once, without
   * waiting for the phrase's end). Else null. */
  private control(all: string, whole: boolean): DictOutcome | null {
    if (whole && isHushPhrase(all)) return withErase({ kind: "hush" as const }, this.settle("").erase || "");
    const before = stopTail(all);
    if (before !== null) {
      this.listening = false;
      // "Over and out, that's all": the "over and out" sends.
      const out = parseRadio(before);
      if (out.kind === "out") return { kind: "send", ...this.settle(out.text ? out.text + " " : ""), end: true, out: true };
      return { kind: "leave", ...this.settle(before ? before + " " : ""), stop: true };
    }
    if (this.answering && this.routes && !this.before && isAnswerPhrase(all))
      return { kind: "type", ...this.settle(all.trim() + " ") };
    return null;
  }

  /** Forget what was typed; returns it, for the target to take back. */
  private erase(): string {
    const text = this.typed;
    this.typed = "";
    this.held = "";
    this.partial = "";
    this.phraseAt = -1;
    this.marks = [];
    return text;
  }

  /** "scratch that": `said` — the words before it in this phrase. Any
   * words: the phrase's guess is taken back (nothing of it goes in). None:
   * the last phrase this message typed is taken back, its characters
   * exactly; earlier phrases stay. */
  private undo(said: string): DictOutcome {
    const guessed = this.guessed;
    this.phraseAt = -1;
    this.held = "";
    this.partial = "";
    if (said.trim() || guessed) {
      this.typed = this.typed.slice(0, this.typed.length - guessed.length);
      return { kind: "undo", text: guessed, alone: false };
    }
    while (this.marks.length && this.marks[this.marks.length - 1] >= this.typed.length) this.marks.pop();
    const at = this.marks.pop();
    if (at === undefined) return { kind: "undo", text: "", alone: true };
    const text = this.typed.slice(at);
    this.typed = this.typed.slice(0, at);
    return { kind: "undo", text, alone: true };
  }
}

/** The keys that take back `text` in a terminal: one DEL (what Backspace
 * sends) per character — per code point, so "café" or an emoji is one key,
 * not its UTF-16 or UTF-8 length. Never Escape: in Claude Code that also
 * interrupts a running turn. */
export function backspaces(text: string): string {
  return "\x7f".repeat(Array.from(text).length);
}

/** A session's activity that means a dialog is on screen (a question, a
 * permission prompt, a usage-limit menu): typed words would answer it. */
export function dialogUp(activity: string): boolean {
  return activity === "clarify" || activity === "limit";
}

/** A dictation held by a sighting alone asks for /dialog again at most this
 * often ({@link LiveHooks.dialogCheck}). */
export const DIALOG_CHECK_EVERY_MS = 2000;

// --- the server's voice gate: every dictation key goes through /send -------- //

/** A batch of dictation keys the server's voice gate did not take ({@link
 * KeyGate}): nothing of it — nor of the keys queued behind it — went in. */
export interface GateRefusal {
  /** 409 `in_dialog`: a dialog is on the session's screen (the server read
   * it at send time — the rows this page sees lag its 4 s state tick). */
  dialog: boolean;
  /** Why, as a caption (a failure's; a dialog's is {@link dialogStopLine}). */
  line: string;
  /** What was counted as typed but never went in: the end of the dictation's
   * typing. */
  text: string;
  /** What those keys would have taken back: it is still in the field. */
  erased: string;
}

/** One batch's request: take back `erase` characters, then type `text`. */
export type KeyPost = (title: string, erase: number, text: string) => Promise<unknown>;

/** POST /api/instances/{title}/send {voice: true, submit: false, erase, text}:
 * BSpace ×erase then the text, in one tmux call, refused (409 in_dialog,
 * nothing pressed) while a dialog is on the session's screen. */
export const postKeys: KeyPost = async (title, erase, text) =>
  instApi(title, "/send", { json: { voice: true, submit: false, erase, text } });

/** A batch the server has not answered in this long has failed (the gate
 * stops typing and says so): a hung request never holds the session's lane,
 * nor the words behind it, for good. */
export const KEY_TIMEOUT_MS = 4000;

/** `p`, or a rejection once `ms` passed without an answer. */
function answerWithin<T>(p: Promise<T>, ms: number): Promise<T> {
  return new Promise<T>((res, rej) => {
    const timer = setTimeout(() => rej(new Error("the server didn't answer")), ms);
    p.then(
      (v) => (clearTimeout(timer), res(v)),
      (e) => (clearTimeout(timer), rej(e))
    );
  });
}

/** One session's batches go one at a time, in order, whichever target sent
 * them (a dictation's and a router answer's): the next waits for the last. */
const keyLanes = new Map<string, Promise<unknown>>();
function inLane<T>(title: string, fn: () => Promise<T>): Promise<T> {
  const call = (): Promise<T> => {
    try {
      return Promise.resolve(fn());
    } catch (err) {
      return Promise.reject(err);
    }
  };
  const prev = keyLanes.get(title);
  const p = prev ? prev.then(call, call) : call();
  keyLanes.set(title, p);
  const clear = () => {
    if (keyLanes.get(title) === p) keyLanes.delete(title);
  };
  p.then(clear, clear);
  return p;
}

/** The last {@link KEY_TIMES_KEPT} batches' round trips (ms): the latency the
 * gate adds to live typing (window.__mfVoiceKeys; the debug trace). */
const KEY_TIMES_KEPT = 20;
let keyTimes: number[] = [];
declare global {
  interface Window {
    __mfVoiceKeys?: number[];
  }
}
function keyTime(ms: number, erase: number, chars: number): void {
  keyTimes = [...keyTimes, Math.round(ms)].slice(-KEY_TIMES_KEPT);
  if (typeof window !== "undefined") window.__mfVoiceKeys = keyTimes;
  try {
    if (typeof localStorage !== "undefined" && localStorage.getItem("cs_voice_debug") === "1")
      console.debug("[voice] keys ⌫" + erase + " +" + chars + " in " + Math.round(ms) + " ms");
  } catch {
    /* no storage: no trace */
  }
}

/** A pending batch: `erase` characters back, then `text` (code points). */
interface KeyBatch {
  erase: number;
  text: string[];
}

/** Fold "take back `erase` characters, then type `text`" into `b`: an erase
 * that reaches only what `b` would type just drops it ("fix the bu" then
 * ⌫1 "ag" is "fix the bag"), so a burst is one request. */
function foldKeys(b: KeyBatch, erase: number, text: string[]): KeyBatch {
  const keep = Math.max(0, b.text.length - erase);
  return { erase: b.erase + Math.max(0, erase - b.text.length), text: [...b.text.slice(0, keep), ...text] };
}

/** A dictation target's keys, through the server's voice gate instead of the
 * terminal socket (MF2: the socket types into whatever is on screen; the
 * gate reads the screen at send time and refuses in ~20 ms a dialog this
 * page will not hear of for 2–4 s). Keys go at once when nothing is in
 * flight; what comes while a batch is in flight is folded into ONE next
 * batch (a local round trip is ~10–30 ms: a burst of recognizer answers
 * goes as one correction). In order, one request at a time per session.
 * The first refusal (a dialog) or failure (the network, the session gone)
 * drops everything queued behind it, never falls back to the socket, and is
 * told to {@link onRefused}; the gate then takes nothing until {@link reset}. */
export class KeyGate {
  /** What the server took from this gate (code points): what a refusal's
   * erase would have taken back is read off its end. */
  private field: string[] = [];
  private queued: KeyBatch | null = null;
  private flying: KeyBatch | null = null;
  private failed: GateRefusal | null = null;
  private waiters: Array<(r: GateRefusal | null) => void> = [];
  private listeners = new Set<(r: GateRefusal) => void>();

  constructor(
    readonly title: string,
    readonly name: string,
    private post: KeyPost = postKeys
  ) {}

  /** The refusal that stopped this gate, if any. */
  get refused(): GateRefusal | null {
    return this.failed;
  }

  /** Nothing queued or in flight. */
  get idle(): boolean {
    return !this.flying && !this.queued;
  }

  /** Queue "take back `erase`, then type `text`". False once refused. */
  push(erase: string, text: string): boolean {
    if (this.failed) return false;
    if (!erase && !text) return true;
    this.queued = foldKeys(this.queued || { erase: 0, text: [] }, Array.from(erase).length, Array.from(text));
    if (!this.flying) this.fly();
    return true;
  }

  /** Resolves once every key pushed so far went in (null) or was refused
   * (the refusal) — before an Enter, which must follow the words. */
  settled(): Promise<GateRefusal | null> {
    if (this.failed || this.idle) return Promise.resolve(this.failed);
    return new Promise((res) => this.waiters.push(res));
  }

  /** Be told of the refusal (once). Returns the unsubscribe. */
  onRefused(fn: (r: GateRefusal) => void): () => void {
    this.listeners.add(fn);
    return () => void this.listeners.delete(fn);
  }

  /** A new message: the gate takes keys again (the server gates each). */
  reset(): void {
    if (!this.idle) return;
    this.failed = null;
    this.field = [];
  }

  private fly(): void {
    const b = this.queued;
    this.queued = null;
    if (!b || (!b.erase && !b.text.length)) return this.wake();
    this.flying = b;
    const t0 = Date.now();
    const text = b.text.join("");
    inLane(this.title, () => answerWithin(this.post(this.title, b.erase, text), KEY_TIMEOUT_MS)).then(
      () => {
        keyTime(Date.now() - t0, b.erase, b.text.length);
        this.field = [...this.field.slice(0, Math.max(0, this.field.length - b.erase)), ...b.text];
        this.flying = null;
        if (this.queued) this.fly();
        else this.wake();
      },
      (err) => this.fail(b, err)
    );
  }

  private fail(b: KeyBatch, err: unknown): void {
    // Everything from this batch on: one take-back and one text, against
    // what the server has.
    const all = this.queued ? foldKeys(b, this.queued.erase, this.queued.text) : b;
    const erased = this.field.slice(Math.max(0, this.field.length - all.erase)).join("");
    this.flying = null;
    this.queued = null;
    const body = (err instanceof ApiError ? err.body : null) as { in_dialog?: boolean; paused?: boolean } | null;
    const dialog = err instanceof ApiError && err.status === 409 && !!body?.in_dialog;
    let line = "Couldn't type into " + this.name + " — " + ((err as Error)?.message || "the server didn't answer");
    if (body?.paused) line = this.name + " is paused — resume it first";
    else if (!(err instanceof ApiError)) line = "Couldn't type into " + this.name + " — the server didn't answer";
    this.failed = { dialog, line, text: all.text.join(""), erased };
    for (const fn of [...this.listeners]) {
      try {
        fn(this.failed);
      } catch {
        /* one listener never stops the rest */
      }
    }
    this.wake();
  }

  private wake(): void {
    const w = this.waiters;
    this.waiters = [];
    for (const res of w) res(this.failed);
  }
}

/** Where a dictation's words go: a session's terminal, or a text box. */
export interface DictTarget {
  kind: "session" | "field";
  /** The session's title ("" for a text box). */
  title: string;
  /** What captions call it. */
  name: string;
  /** Put characters in: false when they could not go (socket closed). */
  type(text: string): boolean;
  /** Take back `text`, the last characters put in. */
  erase(text: string): boolean;
  /** A correction in one go: `old`, the last characters put in, becomes
   * `text` (a text box sets its value once; without it, erase then type). */
  replace?(old: string, text: string): boolean;
  /** A text box: its text before the cursor, where the words go — the
   * context a phrase is heard in ({@link LiveDictation.streamContext}).
   * Absent (a session's terminal): what the dictation typed. */
  before?(): string;
  /** A text box that sends itself (the ask pop-up's answer field): "over"
   * calls this instead of leaving the words in the box. `say`: the "Read
   * replies aloud" pref. */
  submit?(say: boolean): Promise<{ ok: boolean; line: string }>;
  /** The keys go through the server's voice gate ({@link KeyGate}): `type`,
   * `erase` and `replace` only queue them (false once refused); whether they
   * went in is told later — {@link KeyGate.onRefused}, {@link
   * KeyGate.settled}. Absent: the keys are in when the call returns. */
  gate?: KeyGate;
}

/** The focused session's agent: every key through POST /send {voice: true}
 * — the server's gate, never raw keys down the terminal socket, which would
 * type into a dialog the page has not heard of yet. */
export function sessionTarget(title: string, name: string, post: KeyPost = postKeys): DictTarget {
  const gate = new KeyGate(title, name, post);
  return {
    kind: "session",
    title,
    name,
    gate,
    type: (text) => gate.push("", text),
    erase: (text) => gate.push(text, ""),
    replace: (old, text) => gate.push(old, text),
  };
}

/** What {@link LiveDictation} needs from the page. */
export interface LiveHooks {
  /** The caption under the mic; `sticky` stays until replaced. `unsure`:
   * a streamed tail the recognizer may still change, shown muted after it. */
  caption(text: string, sticky?: boolean, unsure?: string): void;
  /** Listening is over: drop the recognizer (it reports nothing more). */
  halt(): void;
  /** A dictation's first phrase, for a session: true when the router acted
   * on it (and said so) — then it is not typed; "hush" when it was "stop
   * talking" in words only the router knows (listening goes on); "end" when
   * what it did ends listening even in a continuous dictation ("stop",
   * "cancel", or the ask pop-up opened a field the mic starts again into). */
  route(text: string, spokeAt?: number): Promise<boolean | "hush" | "end">;
  /** When the phrase being recorded now began (ms; 0: no speech yet, or the
   * recognizer can't say) — the server recognizer's first loud block. */
  spokeAt?(): number;
  /** A continuous dictation sent a message (or the router acted) and goes
   * on: where the next message goes — null keeps the target it has. */
  retarget?(): { target: DictTarget | null; hint?: string } | null;
  /** "Stop talking": everything the app is saying or has queued, dropped. */
  hush?(): void;
  /** The session's live activity (the rows the app polls). */
  activity(title: string): string;
  /** Typing into `title` is held by a dialog this page saw while its row says
   * none: read /dialog again ({@link recheckDialog}), at most every
   * {@link DIALOG_CHECK_EVERY_MS}. */
  dialogCheck?(title: string): void;
  /** `say` for a submit: the "Read replies aloud" pref. */
  replies(): boolean;
  /** A submit went through (`say` as sent): the reply tracker waits on it. */
  submitted(title: string, say: boolean): void;
  /** A dialog or a question waits on the session: a lone answer phrase
   * ("yes", "always", "two") goes to the router at once. */
  pending?(title: string): boolean;
  /** The dictation is over, and how (hands-free listening retries a miss). */
  done?(how: DictEnd): void;
  /** Say what your words did, out loud ({@link CONFIRM_LINES}). */
  confirm?(kind: ConfirmKind): void;
  /** How long (ms) the voice has been quiet: a control phrase at the end of
   * the guess acts after {@link CONTROL_TAIL_MS}. Absent: only at the
   * phrase's end. Only quiet the level meter HEARD (lib/voiceMic
   * meterQuietMs): between two recordings no block comes, and that is not
   * quiet — "over" then a breath across the seam is not the radio word. */
  quietMs?(): number;
  /** When the meter last heard the voice (ms; lib/voiceMic lastLoudAt): a
   * loud block after an early end means the voice came back. Absent: from
   * {@link quietMs} and the clock. */
  loudAt?(): number;
  /** End the phrase under way at the recognizer now (its /end, its final). */
  endPhrase?(): void;
  /** A send went through `ms` after the word's end ("over → sent"). */
  sentIn?(ms: number): void;
  /** The server's measure of the last phrase's accurate pass (ms; /end's
   * `final_ms`), if it has one: the first "over" of a page waits about that
   * long for its final ({@link FinalClock}). */
  finalMs?(): number | null;
  /** The recognizer reports every phrase's end (the server's stream): a
   * phrase taken from its guess is ignored until its end, not just
   * {@link SWALLOW_MAX_MS}. */
  endsSurely?(): boolean;
}

/** The spoken confirmations of the dictation's actions — about what YOU did,
 * so they speak with replies off too (Settings → Voice "Spoken
 * confirmations"). A switch's line is the router's own `say`. */
export const CONFIRM_LINES = {
  /** "over": sent, listening goes on. */
  sent: "Sent.",
  /** "over and out" with words to send. */
  out: "Sent. Over and out.",
  /** "over and out" with nothing to send. */
  bye: "Over and out.",
  /** "disregard". */
  cleared: "Cleared.",
  // A stop phrase ("stop recording") has no line: the falling stop chime is
  // its answer — "Stopped." was heard as the mic saying stop back, every
  // time (2026-10-28).
} as const;
export type ConfirmKind = keyof typeof CONFIRM_LINES;

/** How a dictation ended: `acted` — words went in, or the router acted on
 * them; `missed` — nothing usable was said; `stopped` — the person stopped
 * it (Escape, a stop phrase) or the recognizer failed. */
export type DictEnd = "acted" | "missed" | "stopped";

/** Typed keys travel the terminal's socket, the Enter a separate request:
 * give the keys this long to land first (and to stop counting as one paste
 * with the Enter, which a TUI would turn into a newline). */
export const SUBMIT_SETTLE_MS = 250;

/** POST /send {enter: true}: press Enter on the session's typed draft.
 * Resolves to the caption; `ok` when it went. */
export async function submitDraft(title: string, name: string, say: boolean): Promise<{ ok: boolean; line: string }> {
  try {
    const r = await instApi<{ queued?: boolean }>(title, "/send", { json: { enter: true, say } });
    voiceTrace("submit", { title, ok: true, queued: !!r?.queued });
    return { ok: true, line: r?.queued ? "Queued for " + name + "." : "Sent to " + name + "." };
  } catch (err) {
    const body = (err instanceof ApiError ? err.body : null) as
      | { in_dialog?: boolean; waking?: boolean; paused?: boolean; held?: boolean }
      | null;
    let line = "Couldn't submit: " + ((err as Error)?.message || "error") + " — the words are still in the field";
    voiceTrace("submit", { title, ok: false, status: err instanceof ApiError ? err.status : 0, body });
    if (err instanceof ApiError && err.status === 409) {
      if (body?.in_dialog) line = name + " is on a dialog — press Enter yourself when it is answered";
      else if (body?.waking) line = name + " is waking up — say over again in a moment";
      else if (body?.paused) line = name + " is paused — resume it first";
      else if (body?.held) line = name + " is offline since a crash — press Start first";
    }
    return { ok: false, line };
  }
}

/** A phrase from a recognizer that can't say when it began (Web Speech) is
 * taken to have begun no later than this long before the previous phrase
 * ended ({@link LiveDictation}): an early estimate is the safe one for
 * {@link ANSWER_AFTER_MS}. */
export const SPOKE_LEAD_MS = 2000;

/** The caption when a dialog came up on `name` while its message was being
 * typed (MF2): `words`, the rest of the phrase, were not typed (nor
 * answered: the router took them for a prompt). */
export const dialogStopLine = (name: string, words: string) =>
  name + " asked something — " + (words ? "“" + words + "” not typed" : "nothing more typed") + "; the words before it stay";

/** One dictation into one target: phrases are carried out in order (the
 * first may wait on the router), each typed as it comes. `target` null: no
 * session is focused — the first phrase can still be a command. */
export class LiveDictation {
  readonly dict = new Dictation();
  private chain: Promise<void> = Promise.resolve();
  private done = false;
  /** The router acted on a phrase, or a draft was submitted. */
  private acted = false;
  /** A message was sent in this listen ("over"): an "over and out" with
   * nothing typed after it only stops ({@link apply}'s send). */
  private sentOne = false;
  /** Any words came in (interim, committed, final, unsure). */
  heardAnything = false;
  /** The mic records (the caption says "Listening…"). */
  private recording = false;
  /** The level says speech (the caption says "Hearing you…" until a word). */
  private hearingNow = false;
  /** Checking the guess's control tail against the quiet. */
  private tailTimer: ReturnType<typeof setInterval> | undefined;
  /** "Over" from the guess: waiting for the phrase's /end (then the fast
   * words go, {@link OVER_FINAL_WAIT_MS}). */
  private sendWait: ReturnType<typeof setTimeout> | undefined;
  /** The phrase was taken from its guess: its late words and end are not
   * heard (until its end, or {@link SWALLOW_MAX_MS}). */
  private swallowing: ReturnType<typeof setTimeout> | undefined;
  /** When the control word ended (ms), for "over → sent". */
  private wordEnd = 0;
  /** When words last came in (ms) — or the mic was keyed. */
  private wordsAt = Date.now();
  /** "Over" waits for its final ({@link sendWait}): what it does when the
   * wait is over — or at once, when the fast words settle meanwhile and the
   * finals lately seldom change them ({@link fastStands}). */
  private sendNow: (() => void) | null = null;
  /** "Over" ended its phrase before {@link CONTROL_TAIL_MS} of quiet
   * ({@link OVER_EARLY_QUIET_MS}): when, and what the voice has done since —
   * "met" (the quiet came: "over" is the radio word), "resumed" (the voice
   * came back first: a plain word), "" (not yet). */
  private early: { at: number; seen: "" | "met" | "resumed"; timer?: ReturnType<typeof setInterval> } | null = null;
  /** When the person last talked over a line the app said (ms). */
  private bargeAt = 0;
  /** When this dictation began, and when its last phrase ended (ms): the
   * early bound on a phrase's start when the recognizer can't say
   * ({@link spokeFrom}). */
  private readonly startedAt = Date.now();
  private lastEnd = 0;
  /** When the phrase whose words are coming in began (0: none yet). */
  private phraseSpoke = 0;
  /** When the phrase the step under way acts on began: sent with the router
   * call, so a dialog is answered only by words begun after it was there. */
  private stepSpoke = 0;
  /** A dialog came up on the session while this message's words were going
   * in (MF2): the phrase under way types nothing more — its end hands the
   * rest to the router ({@link heldBack}). */
  private dialogHit = false;
  /** When {@link dialogBlocks} last asked for a held sighting to be read again. */
  private checkedAt = 0;
  /** Words of phrases already over that the server's gate refused (a dialog
   * came up on screen): they go to the router with the rest ({@link heldBack}). */
  private unsent = "";
  /** Stop hearing of the current target's gate refusals. */
  private unwatch: (() => void) | null = null;
  /** A send is on its way ("over"): the message's words are still counted
   * as typed until it is through, but they are gone — a phrase opening
   * meanwhile starts the next message ({@link streamContext}: none). */
  private sending = false;

  constructor(
    private target: DictTarget | null,
    private hooks: LiveHooks,
    /** `hint`: the caption before a word (hands-free: what to answer).
     * `continuous`: "over" sends and listening goes on, until "over and
     * out" or a stop ({@link Dictation.continuous}). */
    private opts: { hint?: string; continuous?: boolean } = {}
  ) {
    this.dict.begin();
    this.dict.routes = target?.kind !== "field";
    this.dict.continuous = !!opts.continuous;
    this.watch(target);
  }

  /** For the recognizer's next phrase (its stream's open): the message's
   * words already said ahead of it ({@link Dictation.context}; a text box:
   * its text up to the cursor, {@link DictTarget.before}) and the session
   * they go to (none for a plain text box). After a send or a clear it is
   * what is left: nothing. */
  streamContext(): { context: string; session: string } {
    if (this.done) return { context: "", session: "" };
    const t = this.target;
    if (this.sending) return { context: "", session: t?.title || "" };
    let ahead: string | undefined;
    try {
      ahead = t?.before?.();
    } catch {
      ahead = undefined;
    }
    return { context: voiceContext(this.dict.context(ahead)), session: t?.title || "" };
  }

  /** Hear of `t`'s gate refusals (a session's keys go through the server). */
  private watch(t: DictTarget | null): void {
    this.unwatch?.();
    this.unwatch = t?.gate ? t.gate.onRefused((r) => this.refused(t, r)) : null;
  }

  /** The server's gate refused a batch of keys (or it failed): nothing of
   * it, nor anything queued behind it, went in. What this dictation counted
   * as typed is put right; on a dialog (MF2: it came up on screen before
   * this page heard of it) nothing more is typed for this phrase, the
   * dialog is noted (and read, for its id), and the words not typed go to
   * the router — at the phrase's end, or now when it is over; any other
   * failure ends listening with its line — never a fall-back to the
   * terminal socket. */
  private refused(t: DictTarget, r: GateRefusal): void {
    if (t !== this.target) return;
    if (this.done) {
      if (r.text.trim() || r.erased) this.hooks.caption(r.dialog ? dialogStopLine(t.name, r.text.trim()) : r.line, true);
      return;
    }
    const over = this.dict.untyped(r.text, r.erased);
    if (!r.dialog) {
      this.end(r.line, true, "stopped");
      return;
    }
    this.dialogHit = true;
    dialogSeen.activity(t.title, "clarify");
    this.checkedAt = Date.now();
    this.hooks.dialogCheck?.(t.title);
    this.unsent = joinWords(this.unsent, over);
    // A phrase still being said hands its words over at its end; between
    // phrases they go now, in turn (nothing to hand over — a refused
    // take-back, a space — still stops, saying so). Unless a step under way
    // handed them over first ("over" waits for its words: dialogHit gone).
    this.chain = this.chain
      .then(async () => {
        if (this.dialogHit && !this.done && !this.dict.inPhrase && this.target === t) await this.heldBack(t, "");
      })
      .catch(() => {});
  }

  /** Still taking words (false once over / a stop / Escape / an error). */
  get active(): boolean {
    return !this.done;
  }

  /** Web Speech's interim words: typed as a guess, corrected as they change. */
  /** The person is mid-sentence: the phrase under way has words that came in
   * the last {@link MID_SENTENCE_MS} and the voice has not paused
   * {@link CONTROL_TAIL_MS} since — or the mic was keyed just now. A reply
   * waits (the queue's gate) until this is false. */
  midSentence(): boolean {
    if (this.done) return false;
    const now = Date.now();
    // Barged in: the person talks over a line (their words still on their
    // way back) — the next line waits as for a sentence.
    if (now - this.bargeAt < MID_SENTENCE_MS) return true;
    const recent = now - this.wordsAt < MID_SENTENCE_MS;
    if (!this.heardAnything) return recent;
    const quiet = this.hooks.quietMs?.() ?? 0;
    return recent && !!(this.dict.held || this.dict.partial) && quiet < CONTROL_TAIL_MS;
  }

  /** The person started talking over a line the app was saying (the
   * recognizer's barge-in; the caller cuts the line): the next line waits
   * ({@link midSentence}) until their words are back and settled. */
  bargedIn(): void {
    if (this.done) return;
    this.bargeAt = Date.now();
    this.wordsAt = this.bargeAt;
  }

  hearing(t: string): Promise<void> {
    if (t) this.heardAnything = true;
    if (t) this.wordsAt = Date.now();
    if (t) this.notePhrase();
    if (this.swallowing) return this.chain;
    return this.step(() => this.dict.hearing(t));
  }

  /** A phrase's end: `spokeAt` — when it began (ms), when the recognizer
   * knows ({@link RecognizerHandlers.onFinal}). */
  heard(phrase: string, spokeAt?: number): Promise<void> {
    if (phrase.trim()) this.heardAnything = true;
    const spoke = this.endPhraseAt(spokeAt);
    const early = this.early;
    if (this.phraseEnded()) return this.chain;
    if (early) return this.afterQuiet(early, (radio) => this.dict.heard(phrase, radio), spoke);
    return this.step(() => this.dict.heard(phrase), spoke);
  }

  /** Streaming: one answer — committed words `w` and the unstable tail `p`,
   * typed as you speak and corrected as the recognizer changes its mind.
   * "Over" waiting for its final ({@link sendNow}) goes now once this
   * settled the last of its words and finals seldom change them. */
  stream(w: string, p: string): Promise<void> {
    if (w.trim() || p.trim()) this.heardAnything = true;
    if (w.trim() || p.trim()) this.wordsAt = Date.now();
    if (w.trim() || p.trim()) this.notePhrase();
    if (this.swallowing) return this.chain;
    return this.step(() => this.dict.stream(w, p)).then(() => {
      if (this.sendNow && this.fastStands()) this.sendNow();
    });
  }

  /** The phrase's fast words all settled (no unstable tail: two passes
   * agreed on every word) from a recognizer whose finals are measured
   * (the server's stream), and its finals lately seldom changed a word
   * ({@link FinalClock.fastStands}): the final would buy little — the
   * words go without waiting for it. */
  private fastStands(): boolean {
    return !!this.hooks.endsSurely?.() && !this.dict.partial && !!this.dict.held && finalClock.fastStands();
  }

  /** Streaming: committed words with no unstable tail. */
  words(w: string): Promise<void> {
    return this.stream(w, "");
  }

  /** Streaming: a phrase's end and its last words (`final`: the accurate
   * re-transcription, when it differs — see {@link Dictation.flush}). */
  flush(w: string, final?: string, spokeAt?: number): Promise<void> {
    if (w.trim() || final?.trim()) this.heardAnything = true;
    const spoke = this.endPhraseAt(spokeAt);
    const early = this.early;
    if (this.phraseEnded()) return this.chain;
    if (early) return this.afterQuiet(early, (radio) => this.dict.flush(w, final, radio), spoke);
    return this.step(() => this.dict.flush(w, final), spoke);
  }

  /** The early bound on when a phrase began, for a recognizer that can't
   * say: not before this dictation, nor more than {@link SPOKE_LEAD_MS}
   * before the previous phrase ended. Early is the safe side. */
  private spokeFrom(): number {
    return Math.max(this.startedAt, this.lastEnd - SPOKE_LEAD_MS);
  }

  /** Words of a phrase came in: remember when it began (once per phrase). */
  private notePhrase(): void {
    if (!this.phraseSpoke) this.phraseSpoke = this.hooks.spokeAt?.() || this.spokeFrom();
  }

  /** A phrase ended: when it began — the recognizer's word, else the start
   * noted with its first words, else {@link spokeFrom}, the earliest of
   * those that are known — and the next phrase starts afresh. */
  private endPhraseAt(spokeAt?: number): number {
    const known = [spokeAt || 0, this.phraseSpoke].filter((v) => v > 0);
    const spoke = known.length ? Math.min(...known) : this.spokeFrom();
    this.phraseSpoke = 0;
    this.lastEnd = Date.now();
    return spoke;
  }

  /** A phrase's end arrived: true when it is one taken from its guess
   * already (ignored); a send waiting on it goes with it. */
  private phraseEnded(): boolean {
    clearTimeout(this.sendWait);
    this.sendWait = undefined;
    this.sendNow = null;
    this.early = null;
    if (!this.swallowing) return false;
    clearTimeout(this.swallowing);
    this.swallowing = undefined;
    return true;
  }

  /** The phrase is taken from its guess: ignore what the recognizer still
   * says of it. */
  private swallow(): void {
    clearTimeout(this.swallowing);
    const max = this.hooks.endsSurely?.() ? SWALLOW_STREAM_MAX_MS : SWALLOW_MAX_MS;
    this.swallowing = setTimeout(() => (this.swallowing = undefined), max);
  }

  /** The phrase was just ended early for "over": watch the voice (every
   * {@link CONTROL_POLL_MS}) until it has been quiet {@link CONTROL_TAIL_MS}
   * ("met") or comes back first ("resumed"). */
  private watchQuiet(): NonNullable<LiveDictation["early"]> {
    const e: NonNullable<LiveDictation["early"]> = { at: Date.now(), seen: "" };
    this.early = e;
    e.timer = setInterval(() => {
      if (this.judge(e) || this.done) clearInterval(e.timer);
    }, CONTROL_POLL_MS);
    return e;
  }

  /** What the voice did since an early end (see {@link early}). */
  private judge(e: NonNullable<LiveDictation["early"]>): "" | "met" | "resumed" {
    if (e.seen) return e.seen;
    const quiet = this.hooks.quietMs?.() ?? Infinity;
    const loud = this.hooks.loudAt?.() ?? Date.now() - quiet;
    // A loud block after the phrase was ended: the voice went on.
    if (loud > e.at) e.seen = "resumed";
    else if (quiet >= CONTROL_TAIL_MS) e.seen = "met";
    return e.seen;
  }

  /** The early-ended phrase's end is here (its final, or the fast words at
   * the deadline): it acts the moment the quiet is known — at once when it
   * already is (the usual case: a final takes longer than the quiet) — as
   * the radio word when the quiet came, as plain words when the voice came
   * back first. */
  private afterQuiet(e: NonNullable<LiveDictation["early"]>, act: (radio: boolean) => DictOutcome, spoke?: number): Promise<void> {
    return new Promise<void>((resolve) => {
      const go = () => {
        const seen = this.judge(e);
        if (!seen && !this.done) return void setTimeout(go, CONTROL_POLL_MS);
        clearInterval(e.timer);
        resolve(this.step(() => act(seen !== "resumed"), spoke));
      };
      go();
    });
  }

  /** After each step: a control phrase at the end of the guess acts once
   * the voice has been quiet {@link CONTROL_TAIL_MS} — checked every
   * {@link CONTROL_POLL_MS} while the guess ends in one. */
  private watchTail(): void {
    if (!this.hooks.quietMs || this.done || this.sendWait || this.swallowing || !this.dict.tail()) {
      clearInterval(this.tailTimer);
      this.tailTimer = undefined;
      return;
    }
    if (!this.tailTimer) this.tailTimer = setInterval(() => this.checkTail(), CONTROL_POLL_MS);
    this.checkTail();
  }

  private checkTail(): void {
    const t = this.done || this.sendWait || this.swallowing ? null : this.dict.tail();
    if (!t) {
      clearInterval(this.tailTimer);
      this.tailTimer = undefined;
      return;
    }
    const quiet = this.hooks.quietMs?.() ?? 0;
    const radio = t.kind === "over" || t.kind === "out";
    // "Over" and the voice already stopped a moment: the phrase ends now and
    // its final is fetched while the rest of the quiet is waited out.
    const early = radio && quiet < CONTROL_TAIL_MS && quiet >= OVER_EARLY_QUIET_MS;
    if (quiet < CONTROL_TAIL_MS && !early) return;
    clearInterval(this.tailTimer);
    this.tailTimer = undefined;
    // The word's end: the meter's last loud block (the quiet it heard can
    // lag the clock by a block).
    this.wordEnd = this.hooks.loudAt?.() || Date.now() - Math.min(quiet, 60_000);
    // The phrase is taken from its guess: when it began (its end, swallowed,
    // brings nothing more).
    const spoke = this.endPhraseAt(this.hooks.spokeAt?.());
    this.hooks.endPhrase?.();
    if (radio) {
      const e = early ? this.watchQuiet() : null;
      // The accurate final, if /end brings it within the finals' usual round
      // trip (overWaitMs); else the fast words — at once when they settled
      // already and the finals lately seldom change them (fastStands).
      const fire = () => {
        clearTimeout(this.sendWait);
        this.sendWait = undefined;
        this.sendNow = null;
        this.early = null;
        this.swallow();
        // The words as they stand now (the phrase's last chunk may have
        // settled them since), while they still end in the radio word.
        const now = this.dict.tail();
        const text = now && (now.kind === "over" || now.kind === "out") ? now.text : t.text;
        if (e) void this.afterQuiet(e, (r) => this.dict.heard(text, r), spoke);
        else void this.step(() => this.dict.heard(text), spoke);
      };
      this.sendNow = fire;
      this.sendWait = setTimeout(fire, this.fastStands() ? 0 : overWaitMs(finalClock.typical(this.hooks.finalMs?.())));
      return;
    }
    // "Disregard", "scratch that", a stop, "stop talking", a switch: no
    // accurate text needed.
    this.swallow();
    void this.step(() => this.dict.heard(t.text), spoke);
  }

  /** Streaming: the unstable tail, for the caption only ({@link stream}
   * types it). */
  unsure(t: string): void {
    if (this.done) return;
    if (t) this.heardAnything = true;
    this.dict.unsure(t);
    this.show();
  }

  /** The recognizer records now. */
  ready(): void {
    if (this.done || this.recording) return;
    this.recording = true;
    this.show();
  }

  /** The level meter heard speech start (`on`) or stop: before the first
   * word, the caption says so. */
  loud(on: boolean): void {
    if (this.done || this.hearingNow === on) return;
    this.hearingNow = on;
    if (!this.dict.shown && !this.dict.partial) this.show();
  }

  /** A passing line (streaming fell back to clips): the next words replace it. */
  notice(line: string): void {
    if (!this.done) this.hooks.caption(line, true);
  }

  /** Push to talk let go: stop, after the phrases already heard. */
  finish(): Promise<void> {
    return this.step(() => this.dict.finish());
  }

  /** Escape: "disregard", and stop. */
  cancel(): Promise<void> {
    return this.step(() => this.dict.cancel());
  }

  /** Silence, or the recognizer went down: stop, the words stay. */
  expire(): Promise<void> {
    return this.step(() => this.dict.expire());
  }

  /** The recognizer failed: stop with `line`; the words stay. */
  fail(line: string): void {
    if (this.done) return;
    this.dict.listening = false;
    const kept = this.dict.typed.trim() && this.target?.kind === "session";
    this.end(kept ? line + " — the words so far stay in the field" : line, true, "stopped");
  }

  /** Hands-free listening ran out with nothing said: stop quietly, a miss. */
  drop(): void {
    this.end("", false, "missed");
  }

  /** The running text — the words in, then the unsure tail (greyed) — or,
   * before a word, whether the mic hears you and what to say. */
  show(): void {
    if (this.done) return;
    if (this.dict.shown || this.dict.partial) {
      this.hooks.caption(this.dict.shown, true, this.dict.partial);
      return;
    }
    const field = this.target?.kind === "field";
    const base = this.opts.hint || (field ? "the words go into the text box" : dictHint());
    const lead = pickupLine(this.recording, this.hearingNow);
    this.hooks.caption(field && lead === "Listening…" ? "Listening — " + base : lead + " · " + base, true);
  }

  private step(next: () => DictOutcome, spoke?: number): Promise<void> {
    this.chain = this.chain
      .then(() => {
        if (this.done) return;
        this.stepSpoke = spoke || this.phraseSpoke || this.spokeFrom();
        const t = this.target;
        this.dict.answering = t?.kind === "session" && !!this.hooks.pending?.(t.title);
        return this.apply(next());
      })
      .catch(() => {})
      .then(() => this.watchTail());
    return this.chain;
  }

  private end(line: string | null, sticky = false, how?: DictEnd): void {
    voiceTrace("end", { line, how: how || "", done: this.done });
    if (this.done) return;
    this.done = true;
    this.unsent = "";
    clearInterval(this.tailTimer);
    clearTimeout(this.sendWait);
    this.sendNow = null;
    clearTimeout(this.swallowing);
    clearInterval(this.early?.timer);
    this.early = null;
    this.dict.listening = false;
    this.hooks.halt();
    if (line !== null) this.hooks.caption(line, sticky);
    this.hooks.done?.(how || (this.acted || this.dict.typed ? "acted" : "missed"));
  }

  private async apply(out: DictOutcome): Promise<void> {
    const t = this.target;
    voiceTrace("apply", {
      out,
      target: t ? { kind: t.kind, name: t.name, submit: !!(t as { submit?: unknown }).submit } : null,
      typed: this.dict.typed.length,
      sentOne: this.sentOne,
    });
    switch (out.kind) {
      case "continue":
        this.show();
        return;
      case "stopped":
        this.end("");
        return;
      case "type":
        if (await this.put(out.text, out.route !== false, out.erase, out.guess)) this.show();
        return;
      case "erase":
        if (out.text && t && this.dialogBlocks(t)) {
          this.end(dialogStopLine(t.name, ""), true, "stopped");
          return;
        }
        t?.erase(out.text);
        // Through the gate: said only once it was taken back (a refusal
        // stops, saying so: {@link refused}).
        if (t?.gate && (await t.gate.settled())) return;
        this.hooks.caption("Disregarded — go on", true);
        this.hooks.confirm?.("cleared");
        return;
      case "undo": {
        if (out.text && t && this.dialogBlocks(t)) {
          this.end(dialogStopLine(t.name, ""), true, "stopped");
          return;
        }
        if (out.text) {
          t?.erase(out.text);
          if (t?.gate && (await t.gate.settled())) return;
        }
        // Silent: your own action, never said back (no confirmation line).
        const line = undoLine(out);
        if (out.end) this.end(t?.kind === "session" && this.dict.typed ? LEFT_LINE : line, false);
        else this.hooks.caption(line, true);
        return;
      }
      case "hush":
        if (out.erase && t && !this.dialogBlocks(t) && !this.keys(t, out.erase, "")) return;
        this.hushed();
        return;
      case "dropped":
        if (out.text && t && this.dialogBlocks(t)) {
          this.end(dialogStopLine(t.name, ""), true, "stopped");
          return;
        }
        t?.erase(out.text);
        if (t?.gate && (await t.gate.settled())) return;
        this.end(out.text ? "Disregarded" : "", false, "stopped");
        return;
      case "leave":
        if ((out.text || out.erase) && !(await this.put(out.text, true, out.erase))) return;
        this.end(
          t?.kind === "session" && this.dict.typed ? LEFT_LINE : out.stop ? STOPPED_LINE : "",
          false,
          out.stop ? "stopped" : undefined
        );
        return;
      case "send": {
        // The word's end: from the guess's tail, else the quiet before now.
        const quiet = this.hooks.quietMs?.();
        const from = this.wordEnd || (quiet != null && quiet < 60_000 ? Date.now() - quiet : 0);
        this.wordEnd = 0;
        // A lone trailing space is not typed before Enter: nothing follows it
        // in this message, and the keys would only have to settle first.
        const typedNow = !!(out.erase || out.text.trim());
        if (typedNow && !(await this.put(out.text, true, out.erase))) return;
        if (this.done) return;
        // "Over and out" with nothing said since the last "over" (a message
        // went this listen, and nothing was typed after it): just stop — a
        // second Enter would only send an empty prompt. With no send yet it
        // sends what is in the box: words an earlier listen left there, or
        // that the person typed or pasted (owner report 2026-11-10: "over and
        // out does not submit what is in the box"). A box that can't be sent
        // from here (a plain text box, no session) just stops too.
        const unsendable = !t || (t.kind === "field" && !t.submit);
        if (out.out && !this.dict.typed && (this.sentOne || unsendable)) {
          this.hooks.confirm?.("bye");
          this.end(STOPPED_LINE, false, "stopped");
          return;
        }
        // A box that sends itself (the ask pop-up's answer): its own send.
        if (t?.kind === "field" && t.submit) {
          this.acted = true;
          this.hooks.caption("Sending to " + t.name + "…", true);
          const say = this.hooks.replies();
          this.sending = true;
          const r = await t.submit(say).finally(() => (this.sending = false));
          if (r.ok && t.title) this.hooks.submitted(t.title, say);
          this.sent(r, out, from);
          return;
        }
        // A text box: Enter is never pressed for you there.
        if (!t || t.kind === "field") {
          if (out.end || !t) this.end(t ? "" : "Click a session first");
          else this.next();
          return;
        }
        // A dialog is up: Enter would press it.
        if (this.dialogBlocks(t)) {
          await this.heldBack(t, "");
          return;
        }
        this.acted = true;
        this.hooks.caption("Sending to " + t.name + "…", true);
        this.sending = true;
        try {
          // Through the gate: Enter only once every word went in (a refusal
          // was told to {@link refused}: the check below hands it over). Raw
          // keys: give them a moment to land first.
          if (t.gate) await t.gate.settled();
          else if (typedNow) await new Promise((r) => setTimeout(r, SUBMIT_SETTLE_MS));
          if (this.done) return;
          if (this.dialogBlocks(t)) {
            await this.heldBack(t, "");
            return;
          }
          const say = this.hooks.replies();
          const r = await submitDraft(t.title, t.name, say);
          if (r.ok) this.hooks.submitted(t.title, say);
          this.sending = false;
          this.sent(r, out, from);
        } finally {
          this.sending = false;
        }
        return;
      }
    }
  }

  /** A send came back: it is confirmed out loud, and its line is the
   * caption. Listening stops when `end` (the confirmation is queued first,
   * so nothing released by the stop speaks before it); a continuous
   * dictation goes on with the next message — unless the send failed (the
   * words are still in the field: listening ends, so nothing is typed after
   * them). The next words wait their turn while a send is under way. */
  private sent(r: { ok: boolean; line: string }, how: { end?: boolean; out?: boolean }, from = 0): void {
    if (r.ok) this.sentOne = true;
    if (r.ok) this.hooks.confirm?.(how.out ? "out" : "sent");
    if (r.ok && from) {
      const ms = Date.now() - from;
      voiceSent(ms);
      this.hooks.sentIn?.(ms);
    }
    if (how.end) this.end(r.line, !r.ok);
    else if (!r.ok) this.end(r.line, true, "stopped");
    else this.next(r.line);
  }

  /** A continuous dictation goes on: the next words are a new message, into
   * where the next message goes now (the focused session, once the ask
   * pop-up closed or moved on). `line`: the caption until a word comes
   * (null: the router's, as it left it; none: the hint). */
  private next(line?: string | null): void {
    if (this.done) return;
    this.dict.next();
    this.dialogHit = false;
    this.unsent = "";
    const n = this.hooks.retarget?.();
    if (n) {
      this.target = n.target;
      this.opts = { ...this.opts, hint: n.hint };
      this.dict.routes = n.target?.kind !== "field";
      this.watch(n.target);
    }
    // A new message: its keys go to the gate again (the server gates each).
    this.target?.gate?.reset();
    if (line) this.hooks.caption(line, true);
    else if (line === undefined) this.show();
  }

  /** "Stop talking": the app's voice stops; the caption says so for a
   * second, then listening goes on (or ends, when the phrase came as push
   * to talk let go). Nothing is typed. */
  private hushed(): void {
    this.acted = true;
    this.hooks.hush?.();
    if (!this.dict.listening) {
      this.end(HUSH_LINE);
      return;
    }
    this.hooks.caption(HUSH_LINE, true);
    setTimeout(() => this.show(), HUSH_CAPTION_MS);
  }

  /** Take back `erase` (the end of the phrase's guess), then type `text`; a
   * session's first phrase goes past the router (unless it can't be a
   * command: `route` false) and the dialog guard first. A `guess` (the phrase
   * isn't over) goes in only where it can now: with no session or a dialog
   * up it waits for the phrase's end, which decides. False when it did not
   * go in (and listening ended). */
  private async put(text: string, route = true, erase = "", guess = false): Promise<boolean> {
    const t = this.target;
    if (guess) {
      if (!t) return true;
      if (this.dialogBlocks(t)) {
        // Words are in already: the rest of this phrase is never typed.
        if (this.dict.typed) this.dialogHit = true;
        return true;
      }
      return this.keys(t, erase, text);
    }
    // Said to MindFlock mid-message ("My flock, open settings."): the
    // router's, never typed — the words before it stay in the box, and
    // listening goes on (unless what it did ends it).
    if (route && t?.kind !== "field" && this.dict.routes && this.dict.typed.length > erase.length && isWakePhrase(text)) {
      const wake = await this.wakeCommand(t, text, erase);
      if (wake !== null) return wake;
      erase = "";
    }
    // A dialog came up mid-message: not a key more — nor one taken back.
    if (t && (this.dict.typed || this.unsent) && this.dialogBlocks(t)) return this.heldBack(t, text);
    // What stays typed once the guess is corrected: nothing — the router and
    // the dialog guard come first, with the guess already taken back.
    if (this.dict.typed.length === erase.length && t?.kind !== "field") {
      if (erase && t && !this.keys(t, erase, "")) return false;
      erase = "";
      if (!text) return true;
      const words = text.trim();
      // The words show at once, muted — not typed yet: the router (a round
      // trip) decides whether they are a command or the prompt.
      if (route) this.hooks.caption(this.dict.shown, true, words);
      const routed = route ? await this.hooks.route(words, this.stepSpoke) : false;
      if (routed === "hush") {
        this.hushed();
        return false;
      }
      if (routed) {
        this.acted = true;
        if (this.dict.continuous && routed !== "end") this.next(null);
        else this.end(null);
        return false;
      }
      if (this.done) return false;
      if (!t) {
        this.end("Click a session first — “" + words + "” not typed", true);
        return false;
      }
      if (this.dialogBlocks(t)) {
        this.end(t.name + " is waiting on a dialog — answer it first", true);
        return false;
      }
    }
    if (!t) {
      this.end("Couldn't type into the session — its terminal isn't connected", true);
      return false;
    }
    return this.keys(t, erase, text);
  }

  /** A phrase said to MindFlock in the middle of a message: its guess taken
   * back, then the router asked. Acted on: false (nothing typed; the
   * message so far stays, listening goes on unless the command ends it).
   * Not a command after all: null, the guess gone — the caller types it. */
  private async wakeCommand(t: DictTarget | null, text: string, erase: string): Promise<boolean | null> {
    if (erase && t && !this.keys(t, erase, "")) return false;
    const words = text.trim();
    this.hooks.caption(this.dict.shown, true, words);
    const routed = await this.hooks.route(words, this.stepSpoke);
    voiceTrace("wake", { phrase: words, routed, typed: this.dict.typed.length });
    if (routed === "hush") {
      this.hushed();
      return false;
    }
    if (routed) {
      this.acted = true;
      if (routed === "end") this.end(null);
      else this.show();
      return false;
    }
    return this.done ? false : null;
  }

  /** A dialog is up on the session words go to (MF2: typed words could
   * answer it — Codex's "y", a digit in Claude's menus): its row says so, a
   * dialog came up during this phrase, or this page knows of one there now
   * — whenever it was seen, before the mic opened too (MF2-residual: a
   * sub-agent's prompt under a "working" row, a dialog the pop-up showed
   * before the next rows poll). Asked right before every batch of keys. A
   * held sighting the row doesn't confirm is read again ({@link
   * LiveHooks.dialogCheck}), so one that has gone stops holding. A text box
   * is never a dialog. */
  private dialogBlocks(t: DictTarget): boolean {
    if (t.kind !== "session") return false;
    if (this.dialogHit || dialogUp(this.hooks.activity(t.title))) return true;
    if (!dialogSeen.blocks(t.title)) return false;
    const now = Date.now();
    if (this.hooks.dialogCheck && now - this.checkedAt >= DIALOG_CHECK_EVERY_MS) {
      this.checkedAt = now;
      this.hooks.dialogCheck(t.title);
    }
    return true;
  }

  /** MF2: a dialog came up on the session while this message was being
   * typed. Nothing more goes into the terminal, and nothing typed is taken
   * back (a Backspace could act on the dialog too): the words not typed yet
   * — `rest` — go to the router, where an answer counts only if it was
   * begun after the dialog was there; else they are dropped, and the
   * caption says so. Listening ends — unless the router acted in a
   * continuous dictation. Always false: nothing went in. */
  private async heldBack(t: DictTarget, rest: string): Promise<boolean> {
    this.dialogHit = false;
    const words = joinWords(this.unsent, rest).trim();
    this.unsent = "";
    if (words && this.dict.routes) {
      this.hooks.caption(this.dict.shown, true, words);
      const routed = await this.hooks.route(words, this.stepSpoke);
      if (routed === "hush") {
        this.hushed();
        return false;
      }
      if (routed) {
        this.acted = true;
        if (this.dict.continuous && routed !== "end") this.next(null);
        else this.end(null);
        return false;
      }
    }
    this.end(dialogStopLine(t.name, words), true, "stopped");
    return false;
  }

  /** The keys themselves: `erase` taken back, then `text` typed — into a
   * terminal as one DEL per character and then the characters (two sends,
   * like the Backspace key and then typing), into a text box as one value.
   * False — and listening over — when they could not go. */
  private keys(t: DictTarget, erase: string, text: string): boolean {
    if (!erase && !text) return true;
    // MF2: asked again right before the keys go (every caller asked already;
    // this is the last word) — a dialog up gets none, nor a Backspace.
    if (this.dialogBlocks(t)) {
      this.end(dialogStopLine(t.name, text.trim()), true, "stopped");
      return false;
    }
    // Through the server's gate: queued in order, counted as typed now
    // (live typing never waits on a round trip); a refusal puts the count
    // right ({@link refused}).
    const ok = t.gate
      ? t.gate.push(erase, text)
      : t.replace
        ? t.replace(erase, text)
        : (!erase || t.erase(erase)) && (!text || t.type(text));
    if (!ok) {
      this.end(t.gate?.refused?.line || "Couldn't type into " + t.name + " — its terminal isn't connected", true);
      return false;
    }
    this.dict.unwrote(erase);
    if (text) {
      this.dict.wrote(text);
      voiceMark("typed");
    }
    return true;
  }
}

// --- the router --------------------------------------------------------------- //

/** POST /api/voice/route's answer. `prompt`: the words are a prompt for the
 * focused session; the rest act (see TopBarMic's `routed`). */
export interface RouteReply {
  kind:
    | "prompt"
    | "switch"
    | "new"
    | "ambiguous"
    | "no-session"
    | "new-failed"
    | "list"
    | "needs"
    | "status"
    | "cancel"
    | "stop"
    | "hush"
    | "off"
    | "repeat"
    | "condense"
    | "switch_index"
    | "switch_rel"
    | "answer"
    | "question"
    | "ask"
    | "read"
    | "help"
    | "early"
    | "scroll"
    | "interrupt"
    | "switch_back"
    | "where"
    | "commands"
    // The voice assistant: "MindFlock, …" — `text` is the rest of the phrase
    // (lib/voiceAssistant; TopBarMic hands it over).
    | "assistant";
  title?: string;
  name?: string;
  say?: string;
  /** switch_index / answer: the rail's row number, 1-based. */
  index?: number;
  /** switch_rel: +1 the next row, -1 the previous. */
  delta?: number;
  /** answer: what was said. "always" comes only from the router. */
  choice?: "yes" | "no" | "always" | "number";
  number?: number;
  /** answer: the server resolved the dialog — `keys` are ready for /answer. */
  dialog_id?: string;
  keys?: string[];
  /** answer, on Claude's question prompt: free words for its "Type
   * something" row, typed with the keys; help: the question asked;
   * assistant: the words after "MindFlock," for the assistant. */
  text?: string;
  /** answer: the session has no dialog up (`say` says so). */
  no_dialog?: boolean;
  /** early: how much sooner than allowed the phrase began (ms; null when
   * it said nothing about when it began). */
  early_ms?: number | null;
  /** scroll: which way, and how far — the server already scrolled
   * `title`'s pane (`ok` false: it couldn't). */
  dir?: "up" | "down";
  amount?: "page" | "half" | "line" | "top" | "bottom";
  count?: number;
  /** scroll / interrupt: whether the server did it. */
  ok?: boolean;
  /** commands: the cheat sheet's rows ({@link VoiceCommandItem}). */
  items?: VoiceCommandItem[];
  /** ambiguous: the sessions "Which one: a or b?" reads out. */
  candidates?: { title: string; name: string }[];
  /** ambiguous / ask: what the router asked back — sent with the next
   * phrase ({@link PendingChoice}), which it reads as the pick, the project
   * or the task. */
  pending?: Record<string, unknown>;
}

const ROUTE_KINDS = new Set<string>([
  "prompt", "switch", "new", "ambiguous", "no-session", "new-failed", "list",
  "needs", "status", "cancel", "stop", "hush", "off", "repeat", "condense",
  "switch_index", "switch_rel", "answer", "question", "ask", "read", "help", "early",
  "scroll", "interrupt", "switch_back", "where", "commands", "assistant",
]);

/** One row of the voice cheat sheet (kind `commands`, components/voice/
 * VoiceCommands): what to say, what it does, and an optional group. */
export interface VoiceCommandItem {
  say: string;
  does: string;
  group?: string;
}

/** How long what the router asked back waits for its answer. */
export const PENDING_MS = 30_000;

/** What the router last asked back ("Which one: a or b?", "In which
 * project?", "What should it do?"): its answer's `pending`, sent with the
 * next phrase for {@link PENDING_MS}; any answer without one drops it. */
export class PendingChoice {
  private held: { data: Record<string, unknown>; at: number } | null = null;
  constructor(private now: () => number = Date.now) {}
  take(): Record<string, unknown> | null {
    if (this.held && this.now() - this.held.at > PENDING_MS) this.held = null;
    return this.held ? this.held.data : null;
  }
  keep(r: RouteReply | null | undefined): void {
    const p = r?.pending;
    this.held = p && typeof p === "object" ? { data: p, at: this.now() } : null;
  }
}

/** The desktop's one: every phrase the mic routes goes through it. */
export const routePending = new PendingChoice();

// --- an answer said before its dialog was there (R8-bis) ------------------------- //

/** A phrase answers a dialog only when the person BEGAN saying it at least
 * this long after this page first had that dialog — under the fastest human
 * reaction to something appearing, so a "yes" said into a dictation 300 ms
 * before a permission prompt came up never answers it. The server applies
 * the same rule with its own stamp too (voice_router.answer_in_time,
 * ANSWER_AFTER_S); this is the page's half. */
export const ANSWER_AFTER_MS = 300;

/** When this page first had each session's dialog: {@link note}d from the
 * rows (`asking.dialog_id`), the dialogs read out or shown, and the
 * router's answers. The first sighting of an id is kept until another id —
 * or none — is seen: an answer (by voice, by hand, on the pop-up), a
 * /dialog that came back empty (204), or the row leaving its dialog
 * ({@link rows}) — so the same command asked again (the same id) is a new
 * sighting, never answered by a "yes" begun before it. */
export class DialogSeen {
  private seen = new Map<string, { id: string; at: number; rows?: boolean }>();
  /** Each row's last activity ({@link rows}: a dialog → none edge). */
  private was = new Map<string, string>();
  /** Sessions whose `session.activity_changed` event said a dialog came up
   * (MF2-residual): typing is held at once, before the rows poll (4 s) or
   * /dialog's id is back. No id, so it never times an answer ({@link at}). */
  private raised = new Set<string>();
  constructor(private now: () => number = Date.now) {}
  /** `title` has dialog `id` now ("" / null: it has none). */
  note(title: string, id: string | null | undefined): void {
    if (!title) return;
    if (!id) {
      this.seen.delete(title);
      this.raised.delete(title);
      return;
    }
    const cur = this.seen.get(title);
    if (!cur || cur.id !== String(id)) this.seen.set(title, { id: String(id), at: this.now() });
  }
  /** The rows: a dialog's id when the row carries it. A row asking nothing
   * clears a sighting the rows themselves confirmed, and any sighting the
   * moment the row LEAVES a dialog (clarify → working: answered, by hand or
   * otherwise) — but not a fresh sighting under a row that never showed the
   * dialog (a sub-agent's prompt reads "working" while /dialog serves it:
   * clearing that every poll would refuse every answer to it). */
  rows(list: Iterable<{ title: string; activity?: string; asking?: { kind?: string; dialog_id?: string | null } | null }>): void {
    for (const r of list) {
      const act = String(r.activity || "");
      const was = this.was.get(r.title);
      this.was.set(r.title, act);
      const a = r.asking;
      if (a && a.kind === "dialog") {
        if (!a.dialog_id) continue;
        this.note(r.title, a.dialog_id);
        const cur = this.seen.get(r.title);
        if (cur) cur.rows = true;
      } else if (r.activity !== "clarify" && (this.seen.get(r.title)?.rows || (was !== undefined && dialogUp(was) && !dialogUp(act))))
        this.note(r.title, null);
    }
  }
  /** `session.activity_changed` (the event, not the 4 s poll): `act` going
   * to a dialog holds typing into `title` at once ({@link blocks}); leaving
   * one (`old` a dialog: answered, by hand or otherwise) clears it and any
   * sighting, as the row's edge does. */
  activity(title: string, act: string | null | undefined, old?: string | null): void {
    if (!title) return;
    if (dialogUp(String(act || ""))) this.raised.add(title);
    else if (this.raised.has(title) || dialogUp(String(old || ""))) this.note(title, null);
  }
  /** When this page first had `title`'s current dialog, whatever its id
   * (ms; 0: none). */
  since(title: string): number {
    return this.seen.get(title)?.at || 0;
  }
  /** This page knows a dialog is up on `title` now — seen (rows, /dialog,
   * the pop-up, the router) or raised by the event — however long ago:
   * nothing is typed into it ({@link LiveDictation}, MF2-residual). Cleared
   * the moment it goes: an answer, a /dialog 204, the row or event leaving
   * the dialog ({@link recheckDialog} for one nothing else would clear). */
  blocks(title: string): boolean {
    return this.seen.has(title) || this.raised.has(title);
  }
  /** When `title`'s dialog `id` was first had (ms), or 0. */
  at(title: string, id: string): number {
    const cur = this.seen.get(title);
    return cur && cur.id === String(id) ? cur.at : 0;
  }
  /** May a phrase begun at `spokeAt` (ms) answer `title`'s dialog `id`? */
  inTime(title: string, id: string, spokeAt: number | undefined): boolean {
    const at = this.at(title, id);
    return !!spokeAt && spokeAt > 0 && at > 0 && spokeAt >= at + ANSWER_AFTER_MS;
  }
  /** POST /api/voice/route's `dialog_seen`: `{title: {id, ms_ago}}`. */
  body(): Record<string, { id: string; ms_ago: number }> {
    const now = this.now();
    const out: Record<string, { id: string; ms_ago: number }> = {};
    for (const [t, v] of this.seen) out[t] = { id: v.id, ms_ago: Math.max(0, now - v.at) };
    return out;
  }
}

/** The desktop's one. */
export const dialogSeen = new DialogSeen();

/** A second /dialog read this long after a 204 confirms the dialog is gone
 * (one 204 can be a frame between two redraws). */
export const DIALOG_RECHECK_MS = 400;
const rechecking = new Set<string>();

/** Typing into `title` is held by a sighting alone (its row says no dialog:
 * a sub-agent's prompt reads "working", and nothing clears a sighting of one
 * answered by hand): read /dialog again. Gone (two 204s
 * {@link DIALOG_RECHECK_MS} apart, the sighting unchanged meanwhile): the
 * sighting is cleared, so the next words type; another id: noted. One read
 * at a time per session; errors change nothing. */
export async function recheckDialog(
  title: string,
  fetch: (title: string) => Promise<Dialog | null>,
  seen: DialogSeen = dialogSeen,
  waitMs = DIALOG_RECHECK_MS
): Promise<void> {
  if (!title || rechecking.has(title) || !seen.blocks(title)) return;
  rechecking.add(title);
  try {
    const at = seen.since(title);
    const read = () => fetch(title).catch(() => undefined);
    let d = await read();
    if (d === null) {
      await new Promise((r) => setTimeout(r, waitMs));
      d = await read();
    }
    if (d === null && seen.since(title) === at) seen.note(title, null);
    else if (d?.id) seen.note(title, d.id);
  } finally {
    rechecking.delete(title);
  }
}

/** The line for an answer begun before its dialog was there. */
export const earlyLine = (name: string) => name + " asked just now: say your answer again";

/** Ask the router what one phrase means, with what it last asked back —
 * and when the phrase began (`spokeAt`, ms: sent as an age, `spoke_ms_ago`,
 * so the server reads it on its own clock; `spoke_at` is this page's clock,
 * for the record) and which dialogs this page has had since when
 * (`dialog_seen`): the server presses an answer only when the phrase began
 * after its dialog was there; `heard`, the fast recognizer's text of the
 * same phrase when the stream gave one. Anything but a well-formed 200 — the route
 * missing (an older server), an error, the network — means "it is a
 * prompt": the words were meant for the session, never lost to the
 * router. */
export async function routePhrase(
  text: string,
  session: string,
  pending: PendingChoice = routePending,
  spokeAt?: number,
  seen: DialogSeen = dialogSeen,
  heard?: string,
): Promise<RouteReply> {
  try {
    // `session`: the focused session (the one a scroll or an interrupt acts
    // on); `heard`: Moonshine's text of the phrase (/end's `heard`), which
    // the router reads beside whisper's for the command words.
    const json: Record<string, unknown> = { text, session };
    if (heard && heard.trim()) json.heard = heard.trim();
    const held = pending.take();
    if (held) json.pending = held;
    if (spokeAt && spokeAt > 0) {
      json.spoke_ms_ago = Math.max(0, Date.now() - spokeAt);
      json.spoke_at = spokeAt;
    }
    const had = seen.body();
    if (Object.keys(had).length) json.dialog_seen = had;
    const r = await api<RouteReply>("/api/voice/route", { json });
    if (r && typeof r === "object") pending.keep(r);
    if (r && typeof r === "object" && ROUTE_KINDS.has(String(r.kind))) return r;
  } catch {
    /* fall through */
  }
  return { kind: "prompt" };
}

// --- numbered sessions, answers, questions --------------------------------------- //

/** Window rows ride in the rail's order as sentinels. */
const isWindowRow = (key: string) => key.startsWith("\u0000");

/** Row `n` (1-based) of the rail exactly as its badges number it — railOrder,
 * the one numbering authority, windows included — or null past the end. */
export function railRow(order: string[], n: number): string | null {
  const i = Math.trunc(Number(n)) - 1;
  return i >= 0 && i < order.length ? order[i] : null;
}

/** The session `delta` rows from `from` on the rail (windows skipped,
 * wrapping like Ctrl+Tab); from no row, the first / the last. Null: no
 * other session. */
export function railStep(order: string[], from: string, delta: number): string | null {
  const sessions = order.filter((k) => !isWindowRow(k));
  if (!sessions.length) return null;
  const i = sessions.indexOf(from);
  const step = delta < 0 ? -1 : 1;
  if (i < 0) return step > 0 ? sessions[0] : sessions[sessions.length - 1];
  const next = sessions[(i + step + sessions.length) % sessions.length];
  return next === from ? null : next;
}

export const APPROVE_OFF_LINE = "Approving by voice is off: Settings → Voice";
export const NO_PREVIOUS_LINE = "No previous session";

/** "Go back": the session focused before this one — the one the store
 * remembers (`prev`), else the MRU's next — never a window, the focused one,
 * a session that is gone (`live`) or hidden. Said again, it bounces back. */
export function previousSession(o: {
  focused: string | null | undefined;
  prev?: string | null;
  mru: readonly string[];
  live: readonly string[];
  hidden?: ReadonlySet<string>;
}): string | null {
  const live = new Set(o.live);
  for (const t of [o.prev || "", ...o.mru])
    if (t && !isWindowRow(t) && t !== o.focused && live.has(t) && !o.hidden?.has(t)) return t;
  return null;
}

/** The caption after the server scrolled a pane (kind `scroll`) — never
 * said: you see it scroll. */
export function scrollLine(r: Pick<RouteReply, "dir" | "amount" | "count" | "ok">, name = ""): string {
  if (r.ok === false) return "Couldn't scroll" + (name ? " " + name : "");
  if (r.amount === "top") return "At the top";
  if (r.amount === "bottom") return "At the bottom";
  const n = Math.trunc(Number(r.count) || 1);
  return "Scrolled " + (r.dir === "up" ? "up" : "down") + (n > 1 ? " ×" + n : "");
}

/** The cheat sheet when the server sends no rows of its own (an older
 * server, the right-click menu before it answers): what the desktop's mic
 * takes, in the default words. */
export function builtinCommands(w: VoiceWords = voiceWords()): VoiceCommandItem[] {
  const q = (k: "send" | "end" | "clear" | "undo" | "stop" | "hush") => (w[k] || [])[0] || "";
  const dict = "While dictating";
  const rows: VoiceCommandItem[] = [
    { say: "… " + q("send"), does: "Send the message; keep listening", group: dict },
    { say: "… " + q("end"), does: "Send and stop listening", group: dict },
    { say: q("clear"), does: "Take back the whole message", group: dict },
    { say: q("undo"), does: "Take back the last phrase", group: dict },
    { say: q("stop"), does: "Stop listening; words stay unsent", group: dict },
    { say: q("hush"), does: "Stop reading aloud", group: dict },
    { say: "switch to <name>", does: "Focus a session by name", group: "Sessions" },
    { say: "session 3", does: "Focus the rail's row 3", group: "Sessions" },
    { say: "next session / previous session", does: "Step along the rail", group: "Sessions" },
    { say: "go back", does: "The session you were on before", group: "Sessions" },
    { say: "where am I", does: "Says the focused session's name", group: "Sessions" },
    { say: "new session for <task>", does: "Start a session", group: "Sessions" },
    { say: "scroll up / scroll down", does: "Scroll the focused pane a page", group: "Reading" },
    { say: "scroll to the top / bottom", does: "Jump to either end", group: "Reading" },
    { say: "more", does: "Scroll again the same way", group: "Reading" },
    { say: "what did it say", does: "Read the last reply", group: "Reading" },
    { say: "interrupt", does: "Press Escape on the agent", group: "Agent" },
    { say: "yes / no / option 2", does: "Answer the dialog on screen", group: "Agent" },
    { say: "what can I say", does: "This list", group: "Agent" },
  ];
  return rows.filter((r) => r.say.replace(/[…\s]/g, ""));
}

/** What a router answer needs from the page (TopBarMic wires the store). */
export interface CommandDeps {
  /** useUi railOrder: the rail's rows as numbered. */
  railOrder(): string[];
  focused(): string;
  /** Focus a session (selectSession). */
  select(title: string): void;
  name(title: string): string;
  /** The caption; `announce` also speaks it when replies are on. */
  caption(line: string, sticky?: boolean): void;
  announce(line: string): void;
  /** Say `line` whatever the replies choice — the answer to a question the
   * person asked out loud — and caption it; absent: `announce`. */
  speak?(line: string): void;
  /** POST /api/voice/route for `session` (`spokeAt`: when the phrase began). */
  route(text: string, session: string, spokeAt?: number): Promise<RouteReply>;
  /** GET /api/voice's `approve_dialogs`, read now. */
  approveDialogs(): Promise<boolean>;
  /** "Go back": the session focused before ({@link previousSession}). */
  previous?(): string | null;
  /** Show the voice cheat sheet with `items`. */
  showCommands?(items: VoiceCommandItem[]): void;
  /** The session's live activity (the rows the app polls). */
  activity(title: string): string;
  replies(): boolean;
  submitted(title: string, say: boolean): void;
}

/** The session's last Say line asks a question (GET /instances/{t}/voice
 * `has_question`) and it is not already working on something else. */
export async function questionPending(title: string, activity: string): Promise<boolean> {
  if (activity === "working") return false;
  try {
    const j = await instApi<{ has_question?: boolean }>(title, "/voice");
    return !!j?.has_question;
  } catch {
    return false;
  }
}

/** The words that answer a question as a prompt. */
function answerWords(r: RouteReply, text: string): string {
  if (r.choice === "number" && r.number) return String(r.number);
  return r.choice || text.trim();
}

/** Carry out the router's numbered-session, answer and question replies.
 * True when `r` was one of them; anything else is the caller's. */
export async function runVoiceCommand(
  r: RouteReply,
  text: string,
  deps: CommandDeps,
  spokeAt?: number,
  seen: DialogSeen = dialogSeen,
): Promise<boolean> {
  switch (r.kind) {
    case "switch_index": {
      const n = Math.trunc(Number(r.index));
      const key = railRow(deps.railOrder(), n);
      if (!key) deps.announce("There is no session " + n);
      else if (isWindowRow(key)) deps.announce("Row " + n + " is a window, not a session");
      else {
        deps.select(key);
        deps.announce(r.say || deps.name(key) + ".");
      }
      return true;
    }
    case "switch_rel": {
      const key = railStep(deps.railOrder(), deps.focused(), Number(r.delta) || 1);
      if (!key) deps.announce("There is no other session");
      else {
        deps.select(key);
        deps.announce(r.say || deps.name(key) + ".");
      }
      return true;
    }
    case "answer":
      await answer(r, text, deps, false, spokeAt, seen);
      return true;
    case "early": {
      // Begun before its dialog was there: nothing pressed. The dialog is
      // known from now on — the next words answer it.
      const title = r.title || deps.focused();
      if (r.dialog_id) seen.note(title, r.dialog_id);
      deps.announce(r.say || earlyLine(deps.name(title)));
      return true;
    }
    case "question":
      deps.announce(r.say || "Nothing is being asked.");
      return true;
    case "switch_back": {
      const key = deps.previous?.() || null;
      if (!key) deps.caption(NO_PREVIOUS_LINE);
      else {
        deps.select(key);
        deps.announce(r.say || deps.name(key) + ".");
      }
      return true;
    }
    case "scroll": {
      // The server scrolled the pane: a caption only, never said back.
      const title = r.title || deps.focused();
      deps.caption(scrollLine(r, title ? deps.name(title) : ""));
      return true;
    }
    case "interrupt": {
      const title = r.title || deps.focused();
      const name = title ? deps.name(title) : "";
      deps.caption(r.ok === false ? "Couldn't interrupt" + (name ? " " + name : "") : "Interrupted" + (name ? " " + name : ""));
      return true;
    }
    case "where": {
      // A question asked out loud: answered out loud, replies on or off.
      const title = deps.focused();
      const line = title ? deps.name(title) + "." : "No session is focused.";
      (deps.speak || deps.announce)(line);
      return true;
    }
    case "commands": {
      const items = Array.isArray(r.items) ? r.items.filter((i) => i && typeof i.say === "string" && i.say.trim()) : [];
      deps.showCommands?.(items.length ? items : builtinCommands());
      if (r.say) (deps.speak || deps.announce)(r.say);
      else deps.caption("Voice commands");
      return true;
    }
    case "help": {
      deps.caption(HELP_LOOKING_LINE);
      const said = (await askHelp(r.text || text)).say;
      // Asked out loud, answered out loud — replies on or off — and kept
      // for "say that again".
      (deps.speak || deps.announce)(said);
      return true;
    }
    default:
      return false;
  }
}

// --- help: questions about MindFlock --------------------------------------------- //

export const HELP_LOOKING_LINE = "Looking that up…";
export const HELP_FAILED_LINE = "Couldn't look that up.";

/** POST /api/voice/help: the answer to a question about MindFlock, from its
 * docs. Never throws — a failure is a line to say. */
export async function askHelp(text: string): Promise<VoiceHelp> {
  try {
    const j = await api<VoiceHelp>("/api/voice/help", { json: { text } });
    if (j && typeof j.say === "string" && j.say.trim()) return j;
  } catch {
    /* fall through */
  }
  return { say: HELP_FAILED_LINE, source: null, model: false };
}

/** An answer: into the dialog the server resolved (only with approving by
 * voice on), else to the question the session asked (as a prompt, through
 * the submit path), else the router's sentence. A row number alone is
 * mapped to its title and asked again, once. Focus never moves. */
async function answer(
  r: RouteReply,
  text: string,
  deps: CommandDeps,
  again: boolean,
  spokeAt: number | undefined,
  seen: DialogSeen,
): Promise<void> {
  if (!r.title && r.index != null && !again) {
    const key = railRow(deps.railOrder(), Number(r.index));
    if (!key || isWindowRow(key)) {
      deps.announce("There is no session " + Math.trunc(Number(r.index)));
      return;
    }
    const next = await deps.route(text, key, spokeAt);
    if (next.kind === "answer") await answer({ ...next, title: next.title || key, index: undefined }, text, deps, true, spokeAt, seen);
    else if (next.kind === "early") await runVoiceCommand({ ...next, title: next.title || key }, text, deps, spokeAt, seen);
    else deps.announce(next.say || deps.name(key) + " isn't asking anything.");
    return;
  }
  const title = r.title || deps.focused();
  if (!title) {
    deps.caption("Click a session first");
    return;
  }
  const name = deps.name(title);
  if (r.dialog_id && r.keys?.length) {
    // The page's half of the rule: never pressed for words begun before
    // this page had the dialog (the server checked its half).
    if (!seen.inTime(title, r.dialog_id, spokeAt)) {
      seen.note(title, r.dialog_id);
      deps.announce(earlyLine(name));
      return;
    }
    if (!(await deps.approveDialogs())) {
      deps.announce(APPROVE_OFF_LINE);
      return;
    }
    try {
      const json: Record<string, unknown> = { keys: r.keys, dialog_id: r.dialog_id, by: "user" };
      if (r.text) json.text = r.text;
      await instApi(title, "/answer", { json });
      // Answered: the next prompt — even an identical one — is new.
      seen.note(title, null);
      deps.announce(r.say || "Answered " + (r.choice === "number" ? String(r.number || "") : r.choice || "") + ".");
    } catch (err) {
      const body = (err instanceof ApiError ? err.body : null) as { dialog_changed?: boolean; dialog_answered?: boolean } | null;
      if (body?.dialog_changed) deps.announce("The prompt on " + name + " changed — say it again.");
      else if (body?.dialog_answered) {
        seen.note(title, null);
        deps.announce("Already answered.");
      }
      else deps.caption("Couldn't answer " + name + ": " + ((err as Error)?.message || "error"), true);
    }
    return;
  }
  // Typed as a prompt, the answer ("1", "yes") would press a dialog that is
  // up: never — that one is answered only through /answer above.
  if (dialogUp(deps.activity(title)) || seen.blocks(title)) {
    deps.announce(name + " is on a dialog — answer it on the screen.");
    return;
  }
  if (await questionPending(title, deps.activity(title))) {
    const words = answerWords(r, text);
    // Through the server's gate, then Enter once the words are in: a dialog
    // that came up on screen meanwhile gets neither.
    const into = sessionTarget(title, name);
    into.type(words + " ");
    const refused = await into.gate?.settled();
    if (refused) {
      if (refused.dialog) {
        seen.activity(title, "clarify");
        deps.announce(name + " is on a dialog — answer it on the screen.");
      } else deps.caption(refused.line, true);
      return;
    }
    const say = deps.replies();
    const sent = await submitDraft(title, name, say);
    if (sent.ok) deps.submitted(title, say);
    deps.caption(sent.line, !sent.ok);
    return;
  }
  deps.announce(r.say || name + " isn't asking anything.");
}

// --- spoken replies ------------------------------------------------------------ //

/** A line named after its session: "Login fix: Fixed it." ("" = bare). */
export function spokenLine(name: string, say: string): string {
  const s = String(say || "").trim();
  const n = String(name || "").trim();
  return n ? n + ": " + s : s;
}

/** The name a spoken line leads with: none for the session you are looking
 * at (you know who is talking), its name for any other — several may be. */
export function voiceName(title: string, focused: string | null | undefined, name: string): string {
  return title && title === focused ? "" : name;
}

// --- news only, and where a reply is from ------------------------------------ //
// The twin of mobile.js's voiceIsNews / voiceContextLead: a reply is read by
// itself only when it is NEWS — it ended after the listener could have heard
// it (the page opened, replies turned on, they last looked at, sent to or
// heard that session) and no more than NEWS_MS ago. Asking for it ("what did
// it say") reads it whatever its age.

/** A reply older than this is never read by itself (mobile.js VOICE_NEWS_MS). */
export const NEWS_MS = 10 * 60_000;
/** Not heard from this long, a reply opens with where it is from
 * (mobile.js VOICE_CONTEXT_GAP_MS). */
export const CONTEXT_GAP_MS = 10 * 60_000;

/** A reply that ended at `endedAt` (ms; 0 = unknown) is news to a listener
 * who could last have heard its session at `heardAt` (ms): it ended after
 * that, and within {@link NEWS_MS} of `now`. An unknown end never is. */
export function isNews(endedAt: number, heardAt: number, now = Date.now()): boolean {
  const e = Number(endedAt) || 0;
  return e > 0 && e > (Number(heardAt) || 0) && now - e <= NEWS_MS;
}

/** When a turn ended (ms) from an "it finished" event: its stamp (unix
 * seconds) less the dwell `session.turn_ended` carries (`idle_for`); 0 when
 * the event has no stamp. */
export function turnEndedAt(ts: number | null | undefined, idleFor?: unknown): number {
  const t = Number(ts) || 0;
  if (!t) return 0;
  return (t - Math.max(0, Number(idleFor) || 0)) * 1000;
}

/** What {@link contextLead} reads. */
export interface ContextLeadInput {
  title: string;
  /** The session on screen (focused). */
  focused: string | null | undefined;
  name: string;
  /** The row's task headline ("Fix pin bar below terminal"), when it has one. */
  headline?: string | null;
  /** When the listener last heard from / looked at `title` (ms; 0 = never). */
  lastAt: number;
  now: number;
  /** The words about to be said (a line already naming the session gets no lead). */
  text: string;
}

const leadNorm = (t: string) => String(t || "").toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();

/** What a reply opens with so the listener knows where it is from — "In
 * Login fix, on fix the pin bar: " — when it is about another session than
 * the focused one, or one not heard from in {@link CONTEXT_GAP_MS} (or
 * ever); "" for the session being talked to right along, a line already
 * naming it, or no name. */
export function contextLead(o: ContextLeadInput): string {
  const name = String(o.name || "").trim();
  if (!name || !o.title) return "";
  const away = !o.lastAt || o.now - o.lastAt > CONTEXT_GAP_MS;
  if (o.title === o.focused && !away) return "";
  const t = leadNorm(o.text);
  if (t.startsWith(leadNorm(name)) || t.startsWith(leadNorm("In " + name))) return "";
  let task = String(o.headline || "").replace(/\s+/g, " ").trim().replace(/[\s.,;:!?]+$/, "");
  // "Fix pin bar" → "fix pin bar" (said mid-sentence); "API docs" stays.
  if (task && !/^.[A-Z]/.test(task)) task = task.charAt(0).toLowerCase() + task.slice(1);
  return "In " + name + (task ? ", on " + task : "") + ": ";
}

/** A reply as said: {@link contextLead} then the words. */
export function replyText(o: ContextLeadInput): string {
  const text = String(o.text || "").trim();
  return contextLead({ ...o, text }) + text;
}

/** When the listener last heard from / looked at each session, and since
 * when anything is news at all — {@link isNews}'s `heardAt`, and
 * {@link contextLead}'s `lastAt`. */
export class HeardClock {
  private marks = new Map<string, number>();
  private from: number;

  constructor(now = Date.now()) {
    this.from = now;
  }

  /** Replies were turned on (or the page came back): nothing that ended
   * before `now` is news. */
  restart(now = Date.now()): void {
    this.from = now;
  }

  /** The listener heard from, sent to or looked at `title` at `now`. */
  mark(title: string | null | undefined, now = Date.now()): void {
    if (title) this.marks.set(title, now);
  }

  /** When `title` was last heard from (0 = never). */
  last(title: string): number {
    return this.marks.get(title) ?? 0;
  }

  /** The news boundary for `title`: the later of {@link restart} and its mark. */
  heardAt(title: string): number {
    return Math.max(this.from, this.last(title));
  }

  clear(): void {
    this.marks.clear();
  }
}

/** With "all", the same reply rides both "it finished" events (idle and
 * turn_ended, seconds to a minute apart): this long, the same words from the
 * same session are that one reply. */
export const HEARD_DEDUPE_MS = 3 * 60_000;

/** Which finished turns were already read (or queued): once per (session,
 * stamp) — the Say line's own stamp when the event carries one, else the
 * words themselves inside {@link HEARD_DEDUPE_MS}. */
export class HeardTurns {
  private seen = new Map<string, { key: string; at: number }>();

  /** True the first time `title`'s turn with `say` (stamped `ts` seconds,
   * when known) is seen; false for the second report of it. */
  fresh(title: string, say: string, ts: number | null, now = Date.now()): boolean {
    const words = String(say || "").replace(/\s+/g, " ").trim();
    if (!title || !words) return false;
    const key = ts ? "ts:" + ts : "say:" + words;
    const last = this.seen.get(title);
    if (last && last.key === key && (ts || now - last.at < HEARD_DEDUPE_MS)) return false;
    this.seen.set(title, { key, at: now });
    return true;
  }

  clear(): void {
    this.seen.clear();
  }
}

/** Server stamps and the browser clock may disagree a little (the server can
 * be another machine): a say this much older than the send still counts. */
const STAMP_SLACK_S = 5;
/** A prompt whose turn never reported back stops being waited on. */
const PENDING_MAX_MS = 30 * 60_000;

interface Pending {
  sentAt: number; // ms, browser clock
  working: boolean;
}

/** Which replies may be read aloud: the turns of drafts THIS app submitted
 * by voice with `say: true` — never a prompt typed at the keyboard or sent
 * from the phone — and each turn once (idle and turn_ended both report it). */
export class ReplyTracker {
  private pending = new Map<string, Pending>();
  private lastSent = new Map<string, number>();

  /** A voice draft was submitted to `title`. `say` is what /send was told. */
  sent(title: string, say: boolean, now = Date.now()): void {
    this.lastSent.set(title, now);
    if (!say) {
      this.pending.delete(title);
      return;
    }
    this.pending.set(title, { sentAt: now, working: false });
  }

  waiting(title: string, now = Date.now()): boolean {
    const p = this.pending.get(title);
    if (p && now - p.sentAt > PENDING_MAX_MS) this.pending.delete(title);
    return this.pending.has(title);
  }

  /** When a draft last went to `title` from here (ms; 0 = never) — the
   * listener was talking to it then ({@link contextLead}'s `lastAt`). */
  sentAt(title: string): number {
    return this.lastSent.get(title) ?? 0;
  }

  /** The session started on it: the next idle is this prompt's turn ending. */
  working(title: string): void {
    const p = this.pending.get(title);
    if (p) p.working = true;
  }

  /** A turn ended for `title` with `say` (stamped `ts` seconds when known —
   * the Say line's own stamp, or the event's). The words to speak (the
   * caller names the session, {@link voiceName}), or null: not ours,
   * already spoken, or an idle that predates the prompt. */
  take(title: string, say: string, ts: number | null, now = Date.now()): string | null {
    if (!this.waiting(title, now)) return null;
    const p = this.pending.get(title)!;
    const fresh = !ts || ts * 1000 >= p.sentAt - STAMP_SLACK_S * 1000;
    const s = String(say || "").trim();
    if (s && fresh) {
      this.pending.delete(title);
      return s;
    }
    // No Say line this turn (or only the previous turn's): one word, so the
    // owner who asked to hear back is not left waiting — but only once the
    // session is known to have worked on it, or a stale idle would end it.
    if (p.working) {
      this.pending.delete(title);
      return "Finished.";
    }
    return null;
  }

  clear(): void {
    this.pending.clear();
    this.lastSent.clear();
  }
}

// --- hands-free: a dialog or a question read out, the mic opened ---------------- //

/** How long the mic listens for an answer after reading one out (mobile.js
 * VOICE_DIALOG_MS / VOICE_ANSWER_MS). */
export const ANSWER_LISTEN_MS = 12_000;

const ORD = ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine"];

/** Markdown read aloud as plain words (links keep their text). */
function plain(text: string): string {
  return String(text || "")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/[*_`#>]+/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

/** What to say to answer `d` (mobile.js voiceDialogChoices): a permission
 * prompt takes words, any other dialog the numbers of its options — Claude's
 * question prompt (kind "select") its numbers, ticked then "next" when it
 * takes several, and "submit" on its review step. */
export function dialogChoices(d: Pick<Dialog, "options"> & Partial<Pick<Dialog, "kind" | "multi" | "steps" | "step">>): string {
  const select = d.kind === "select";
  if (select && d.steps?.[d.step ?? -1]?.submit) return "Say submit, or cancel.";
  const kinds = new Set(d.options.map((o) => o.kind));
  if (!select && (kinds.has("yes") || kinds.has("no")))
    return kinds.has("always") ? "Say yes, no, or always." : "Say yes or no.";
  const opts = d.options.slice(0, ORD.length);
  const nums = opts.map((_, i) => ORD[i]);
  const list =
    nums.length === 1
      ? nums[0]
      : nums.length === 2
        ? nums[0] + " or " + nums[1]
        : nums.slice(0, -1).join(", ") + ", or " + nums[nums.length - 1];
  const say = select && d.multi ? ". Say the numbers to tick, then next." : ". Say " + list + ".";
  return opts.map((o, i) => ORD[i] + ": " + plain(o.label)).join(". ") + say;
}

/** A dialog read aloud, name first (mobile.js voiceDialogSpeech):
 * "Login fix: Allow git push? Say yes, no, or always." */
export function dialogSpeech(name: string, d: Dialog): string {
  const parts = [spokenLine(name, plain(d.question) || "a permission prompt")];
  if (d.command) parts.push("It wants to run: " + String(d.command).slice(0, 200) + ".");
  parts.push(dialogChoices(d));
  return parts.join(" ");
}

/** The one retry after a miss: "I didn't catch that: say yes, no, or always." */
export function dialogRetryLine(d: Parameters<typeof dialogChoices>[0]): string {
  return "I didn't catch that: " + dialogChoices(d).replace(/^.*\bSay /, "say ");
}

/** What {@link HandsFree} needs from the page. */
export interface HandsFreeDeps {
  /** "Read replies aloud": hands-free is that pref, nothing else. */
  replies(): boolean;
  focused(): string;
  name(title: string): string;
  /** The mic is listening already (a dictation the person started). */
  busy(): boolean;
  fetchDialog(title: string): Promise<Dialog | null>;
  approveDialogs(): Promise<boolean>;
  /** The name a spoken line leads with ({@link voiceName}: none for the
   * session you are looking at); `name` when absent. */
  voiceName?(title: string): string;
  /** Caption the line and say it (`title`: the session it is about — the
   * queue can drop one ask's lines); resolves once it has been said, cut
   * off, or dropped. */
  speak(line: string, title: string): Promise<void>;
  /** Resolves once the speech queue has nothing left to say: the mic never
   * opens while another session's line is still being read. */
  drained?(): Promise<void>;
  caption(line: string, sticky?: boolean): void;
  /** Open the mic on `title` for `ms` (the caption `hint` until a word):
   * resolves with how that dictation ended. */
  listen(title: string, hint: string, ms: number): Promise<DictEnd>;
}

/** With "Read replies aloud" on: a dialog that comes up on the focused
 * session, or a question in a reply to a prompt this app sent, is read out
 * name first and the mic opens {@link ANSWER_LISTEN_MS} for the answer; a
 * miss gets ONE retry ("I didn't catch that: …"), then it goes quiet.
 *
 * With the ask pop-up on (lib/ask.ts) the pop-up is the one hands-free
 * entry for a dialog: it calls {@link ask} for the session it shows —
 * focused or not — and TopBarMic leaves `dialog` alone, so nothing is read
 * out twice. Every line goes through the page's one speech queue, and the
 * mic opens only once that queue is quiet ({@link HandsFreeDeps.drained}). */
export class HandsFree {
  private seen = "";
  private running = false;
  /** Resolves when the run under way is over ({@link settled}). */
  private finished: Promise<void> = Promise.resolve();
  private endRun: () => void = () => {};
  /** {@link stop}ped: listening for this title ends, no retry. */
  private stopped = "";

  constructor(private deps: HandsFreeDeps) {}

  /** Resolves once nothing is being read out or listened for here — the
   * next step of an ask waits for the previous one's read-out to be over. */
  settled(): Promise<void> {
    return this.running ? this.finished : Promise.resolve();
  }

  private begin(): void {
    this.running = true;
    this.finished = new Promise<void>((r) => (this.endRun = r));
  }

  private over(): void {
    this.running = false;
    this.endRun();
  }

  /** A dialog came up on `title` (its activity went to clarify). */
  async dialog(title: string): Promise<void> {
    const x = this.deps;
    if (!this.ready(title) || title !== x.focused()) return;
    this.begin();
    try {
      const d = await x.fetchDialog(title).catch(() => undefined);
      // None up (/dialog 204): the next one — even the same command — is new.
      if (d === null) dialogSeen.note(title, null);
      else if (d?.id) dialogSeen.note(title, d.id);
      if (!d || !d.parsed || !d.options.length || d.id === this.seen) return;
      if (!x.replies() || x.busy() || title !== x.focused()) return;
      this.seen = d.id;
      const name = x.name(title);
      await x.speak(dialogSpeech(this.spoken(title), d), title);
      if (!(await x.approveDialogs())) {
        await x.speak("Answer it on the screen.", title);
        return;
      }
      await this.listen(title, dialogRetryLine(d), name + ": " + dialogChoices(d));
    } finally {
      this.over();
    }
  }

  /** `title`'s reply (already read out) asks `say`, a question. */
  async question(title: string, say: string): Promise<void> {
    if (!this.ready(title)) return;
    this.begin();
    try {
      const name = this.deps.name(title);
      await this.listen(title, "Still there? " + spokenLine(this.spoken(title), say), answerHint(name));
    } finally {
      this.over();
    }
  }

  /** The ask pop-up shows `title` asking: a parsed `dialog` is read out name
   * first ("Login fix: Allow git push? Say yes, no, or always.") and, with
   * approving by voice on, the mic listens for the answer; a `question` is
   * read out (unless `said` — the reply that asked it is being read already:
   * wait for that instead) and the mic listens for the reply. Nothing at all
   * unless "Read replies aloud" is on. */
  async ask(title: string, a: { dialog?: Dialog | null; question?: string; said?: Promise<void> | null }): Promise<void> {
    const x = this.deps;
    if (a.dialog?.id) dialogSeen.note(title, a.dialog.id); // on the pop-up: seen
    if (!this.ready(title)) return;
    this.begin();
    this.stopped = "";
    try {
      const name = x.name(title);
      const d = a.dialog;
      if (d) {
        if (!d.parsed || !d.options.length || d.id === this.seen) return;
        this.seen = d.id;
        await x.speak(dialogSpeech(this.spoken(title), d), title);
        if (this.stopped === title) return;
        if (!(await x.approveDialogs())) {
          await x.speak("Answer it on the screen.", title);
          return;
        }
        await this.listen(title, dialogRetryLine(d), name + ": " + dialogChoices(d));
        return;
      }
      const q = String(a.question || "").trim();
      if (!q) return;
      if (a.said) await a.said;
      else await x.speak(spokenLine(this.spoken(title), plain(q)), title);
      if (this.stopped === title) return;
      await this.listen(title, "Still there? " + spokenLine(this.spoken(title), plain(q)), answerHint(name));
    } finally {
      this.over();
    }
  }

  /** The pop-up moved on from `title`: no more listening (or retry) for it. */
  stop(title: string): void {
    this.stopped = title;
  }

  private spoken(title: string): string {
    return this.deps.voiceName ? this.deps.voiceName(title) : this.deps.name(title);
  }

  private ready(title: string): boolean {
    return !!title && !this.running && this.deps.replies() && !this.deps.busy();
  }

  private async listen(title: string, retry: string, hint: string): Promise<void> {
    const x = this.deps;
    for (let tries = 0; ; tries++) {
      if (x.drained) await x.drained();
      if (!x.replies() || x.busy() || this.stopped === title) return;
      const how = await x.listen(title, hint, ANSWER_LISTEN_MS);
      if (how !== "missed" || this.stopped === title) return;
      if (tries >= 1) {
        x.caption(x.name(title) + " needs you");
        return;
      }
      await x.speak(retry, title);
    }
  }
}

/** The options' Diagnostics line for what waits on the focused session. */
export function pendingDiagnostics(name: string, dialog: Dialog | null, dialogUp: boolean, question: string): string {
  if (!name) return "Pending: no session focused";
  if (dialog) return "Pending: " + name + " shows a dialog — " + (plain(dialog.question) || "unparsed");
  if (dialogUp) return "Pending: " + name + " shows a dialog";
  if (question) return "Pending: " + name + " asked — " + plain(question);
  return "Pending: nothing on " + name;
}

// --- speaking ------------------------------------------------------------------- //

/** One engine: speak() resolves when the text has been said (or given up on).
 * `audible`: called as its first sound actually plays — an engine that
 * does so says {@link reportsAudible}; for any other the queue takes the
 * line's turn for its start. */
export interface Speaker {
  speak(text: string, audible?: () => void): Promise<void>;
  cancel(): void;
  readonly reportsAudible?: boolean;
}

/** A queued line older than this when its turn comes is dropped, not said:
 * a backlog never reads stale news. */
export const SPEECH_STALE_MS = 60_000;
/** A confirmation ("Sent.") older than this when its turn comes is dropped:
 * late, it would only confuse. */
export const CONFIRM_STALE_MS = 5_000;
/** The quiet between a line's end and a sound queued behind it
 * ({@link SpeechQueue.cue}): an engine reports its words over a moment
 * before the speakers are done with them. */
export const CUE_GAP_MS = 120;
/** A line held back by the queue's gate is tried again this often. */
export const GATE_RETRY_MS = 50;
/** Words this recent in the phrase under way: the person is mid-sentence
 * (a reply waits for their pause). Also the grace after the mic is keyed. */
export const MID_SENTENCE_MS = 1500;
/** The sentences of {@link CONFIRM_LINES}, as the server speaker asks for
 * them ({@link speechSentences}): the ones it keeps in memory (built on
 * first use — the sentence rules are declared further down). */
let confirmSentences: ReadonlySet<string> | null = null;
const isConfirmSentence = (part: string) =>
  (confirmSentences ||= new Set(Object.values(CONFIRM_LINES).flatMap((l) => speechRequests(l)))).has(part);
/** At most this many lines' audio kept (the five, plus switches). */
export const SPEAK_CACHE_MAX = 32;

/** A line's words, or a function that words it when its turn comes (whether
 * to lead with the session's name depends on what is focused THEN). */
export type SpeechLine = string | (() => string);

interface Queued {
  line: SpeechLine;
  /** A confirmation ({@link SpeechQueue.confirmLine}): at the front, said even
   * while held, stale after {@link CONFIRM_STALE_MS}. */
  confirm?: boolean;
  /** Called as its first sound plays (a confirmation mutes the mic) — not
   * when its turn comes: a line being fetched is silent, the mic hears on. */
  start?: () => void;
  /** A sound ({@link SpeechQueue.cue}), not words: this plays it. */
  sound?: Speaker;
  /** When it was queued (ms). */
  at: number;
  /** Said with this engine instead of the queue's (the assistant's voice). */
  voice?: Speaker;
  /** The session it is about ("" for the app's own words): {@link
   * SpeechQueue.cancel} can drop one session's lines and keep the rest. */
  title: string;
  done: () => void;
}

/** Everything the desktop says, one line at a time — replies, ask
 * read-outs, the router's answers: never two at once, never the browser's
 * voice over the server's.
 *  - in arrival order;
 *  - {@link hold} (the mic is listening) cuts the line being said — it is
 *    over, never resumed — and starts no other until {@link release};
 *  - {@link cancel} drops everything, or one session's lines (the one being
 *    said too) and keeps the others';
 *  - a line queued more than {@link SPEECH_STALE_MS} before its turn is
 *    dropped (a debug log, not aloud);
 *  - {@link drained} resolves once nothing is left to say — a hands-free
 *    listen waits for it;
 *  - `last` is what was said last ("repeat");
 *  - a {@link confirm}ation ("Sent.") goes to the front, after the other
 *    confirmations waiting — never behind a reply — is said even while held
 *    (the mic is muted for it), and is dropped once {@link CONFIRM_STALE_MS}
 *    old; a per-session {@link cancel} never drops one;
 *  - a {@link cue} (the mic's chime) takes a confirmation's place: never over
 *    words — a stop's chime follows a pending "Over and out." whole. */
export class SpeechQueue {
  private lines: Queued[] = [];
  /** The engine saying the current line (cancel THAT one, even if the
   * engine choice moved since). */
  private speaking: Speaker | null = null;
  /** The line being said. */
  private current: Queued | null = null;
  private held = false;
  private turn = 0;
  private waiters: Array<() => void> = [];
  /** {@link setGate}: may a line (not a confirmation) start now? */
  private gate: (() => boolean) | null = null;
  /** The line being said is heard now (its first sound played). */
  private audible = false;
  private gateRetry: ReturnType<typeof setTimeout> | undefined;
  last = "";

  /** `onSpeaking`: a line's turn starts / ends (the pill, Escape) — from
   * when it is asked of its engine. `opts.onAudible`: its sound starts / it
   * is over — the mic is muted for THAT span only (a reply still being
   * synthesised is silence the person may talk into). */
  constructor(
    private engine: () => Speaker,
    private onSpeaking: (on: boolean) => void = () => {},
    private opts: { now?: () => number; staleMs?: number; log?: (msg: string) => void; onAudible?: (on: boolean) => void } = {}
  ) {}

  private now(): number {
    return this.opts.now ? this.opts.now() : Date.now();
  }

  /** Queue a line; resolves once it has been said (or cut off, or dropped).
   * `at`: when its news happened (ms), if earlier than now; `title`: the
   * session it is about; `voice`: the engine that says it (the assistant's),
   * else the queue's. */
  say(line: SpeechLine, opts: { at?: number; title?: string; voice?: Speaker } = {}): Promise<void> {
    if (typeof line === "string" && !line.trim()) return Promise.resolve();
    return new Promise<void>((done) => {
      this.lines.push({ line, at: opts.at ?? this.now(), title: opts.title || "", done, voice: opts.voice });
      this.pump();
    });
  }

  /** A short confirmation of the person's own action: queued at the front
   * (after the confirmations already there), said even while {@link hold}
   * keeps the rest waiting — the mic is listening for the next message — and
   * dropped when its turn comes more than {@link CONFIRM_STALE_MS} after it
   * was queued. `start`: called as it begins playing. */
  confirmLine(line: string, opts: { start?: () => void } = {}): Promise<void> {
    if (!line.trim()) return Promise.resolve();
    return new Promise<void>((done) => {
      let at = 0;
      while (at < this.lines.length && this.lines[at].confirm) at++;
      this.lines.splice(at, 0, { line, at: this.now(), title: "", done, confirm: true, start: opts.start });
      this.pump();
    });
  }

  /** A short sound (the mic's chime) in the queue's order: never over words —
   * after the line being said and the confirmations waiting (a stop phrase's
   * "Over and out." plays whole, then the chime), before any reply; played even
   * while held or gated, {@link CUE_GAP_MS} of quiet first when it follows a
   * line. `play` starts it, `ms` is its length. Not words: no onSpeaking,
   * never {@link last}; a {@link hold} (the mic keyed again) drops it
   * unplayed. Resolves once it has played (or was dropped). */
  cue(play: () => void, ms: number): Promise<void> {
    const lead = this.cueLead;
    return new Promise<void>((done) => {
      let at = 0;
      while (at < this.lines.length && this.lines[at].confirm) at++;
      this.lines.splice(at, 0, { line: "", at: this.now(), title: "", done, confirm: true, sound: soundSpeaker(play, ms, lead) });
      this.pump();
    });
  }

  /** Continuous listening: instead of a hold, a line (not a confirmation)
   * starts only when `ok` says so — not while the person is mid-sentence —
   * checked again every {@link GATE_RETRY_MS} while one waits. Null: no gate. */
  setGate(ok: (() => boolean) | null): void {
    this.gate = ok;
    clearTimeout(this.gateRetry);
    this.gateRetry = undefined;
    if (!ok) this.pump();
  }

  /** Cut the line being said (barge-in) and go on with the next. */
  skip(): void {
    this.cut();
    this.pump();
  }

  get busy(): boolean {
    return !!this.speaking;
  }

  /** The line being said has started sounding (not just its turn). */
  get audibleNow(): boolean {
    return !!this.speaking && this.audible;
  }

  /** The line being said is a confirmation ("Sent.") — the person's own
   * action, played whole (a voice over it is not a barge-in). */
  get confirming(): boolean {
    return !!this.current?.confirm && !this.current.sound;
  }

  /** The quiet a {@link cue} queued now starts with: {@link CUE_GAP_MS} when
   * something is being said or waits (the chime's recording skip covers it). */
  get cueLead(): number {
    return this.speaking || this.lines.length ? CUE_GAP_MS : 0;
  }

  get queued(): number {
    return this.lines.length;
  }

  /** Nothing being said and nothing waiting. */
  get idle(): boolean {
    return !this.speaking && !this.lines.length;
  }

  /** Resolves once the queue is {@link idle} (at once when it is). */
  drained(): Promise<void> {
    if (this.idle) return Promise.resolve();
    return new Promise<void>((r) => this.waiters.push(r));
  }

  /** Listening: stop talking now (the line is over — its engine is
   * cancelled before this returns); what is queued waits for
   * {@link release}. */
  hold(): void {
    this.held = true;
    this.cut();
    // A sound still waiting belongs to the moment just over (a stop's chime).
    const sounds = this.lines.filter((l) => l.sound);
    this.lines = this.lines.filter((l) => !l.sound);
    sounds.forEach((l) => l.done());
  }

  release(): void {
    this.held = false;
    this.pump();
  }

  /** Stop and forget everything queued — or, with `title`, only that
   * session's lines (the one being said, if it is one of them): the others
   * play on, and so do the confirmations and sounds (about the person's own
   * action, never a session's). */
  cancel(title?: string): void {
    const mine = (l: Queued) => title === undefined || (l.title === title && !l.confirm);
    const drop = this.lines.filter(mine);
    this.lines = this.lines.filter((l) => !mine(l));
    drop.forEach((l) => l.done());
    if (this.current && mine(this.current)) this.cut();
    if (this.held) this.settle();
    else this.pump();
  }

  /** The line being said is over now: its engine cancelled (synchronously —
   * both engines stop their audio inside cancel()), its promise resolved,
   * whatever the engine's own late resolve does after. */
  private cut(): void {
    const sp = this.speaking;
    const cur = this.current;
    if (!sp) return;
    this.speaking = null;
    this.current = null;
    this.turn++;
    sp.cancel();
    if (!cur?.sound) this.onSpeaking(false);
    this.silent(cur);
    cur?.done();
  }

  /** The line `q` is over: its mute is lifted (when it had sounded). */
  private silent(q: Queued | null): void {
    const was = this.audible;
    this.audible = false;
    if (was && q) this.opts.onAudible?.(false);
  }

  private settle(): void {
    if (!this.idle) return;
    const w = this.waiters;
    this.waiters = [];
    w.forEach((r) => r());
  }

  private pump(): void {
    if (this.speaking) return;
    for (;;) {
      // Held: only a confirmation (always at the front) may go.
      if (this.held && !this.lines[0]?.confirm) return;
      // Gated (continuous listening): a line waits for the person's pause.
      if (this.gate && this.lines[0] && !this.lines[0].confirm && !this.gate()) {
        if (!this.gateRetry)
          this.gateRetry = setTimeout(() => {
            this.gateRetry = undefined;
            this.pump();
          }, GATE_RETRY_MS);
        return;
      }
      const next = this.lines.shift();
      if (!next) return this.settle();
      const stale = next.confirm ? CONFIRM_STALE_MS : this.opts.staleMs ?? SPEECH_STALE_MS;
      const age = this.now() - next.at;
      const text = next.sound ? "" : String((typeof next.line === "function" ? next.line() : next.line) || "").trim();
      if (age > stale) {
        if (!next.sound)
          (this.opts.log ?? ((m: string) => console.debug(m)))(
            "[voice] dropped a line " + Math.round(age / 1000) + " s old: " + text.slice(0, 80)
          );
        next.done();
        continue;
      }
      if (!text && !next.sound) {
        next.done();
        continue;
      }
      const sp = next.sound || next.voice || this.engine();
      const turn = ++this.turn;
      if (!next.sound) this.last = text;
      this.speaking = sp;
      this.current = next;
      this.audible = false;
      if (!next.sound) this.onSpeaking(true);
      // Its first sound: the mic is muted from here (not from its turn — a
      // reply being synthesised is seconds of silence the mic must hear).
      // A sound (the chime) is not a line: no mute, never audibleNow.
      const audible = () => {
        if (this.turn !== turn || !this.speaking || this.audible) return;
        this.audible = true;
        this.opts.onAudible?.(true);
        next.start?.();
      };
      const reports = !next.sound && !!sp.reportsAudible;
      if (!reports && !next.sound) audible();
      const done = () => {
        if (this.turn !== turn || !this.speaking) return;
        this.speaking = null;
        this.current = null;
        if (!next.sound) this.onSpeaking(false);
        this.silent(next);
        next.done();
        this.pump();
      };
      (reports ? sp.speak(text, audible) : sp.speak(text)).then(done, done);
      return;
    }
  }
}

/** A chime asked for while the page's AudioContext is not running (the
 * first press after a reload or a long idle: its resume is still in
 * flight) waits this long for it, then is dropped — a late chime would only
 * confuse. mobile.js VOICE_CHIME_LATE_MS. */
export const CHIME_LATE_MS = 400;
/** How often a waiting chime looks at the context again. */
const RESUME_POLL_MS = 20;

/** `ctx` runs — now, or within `maxMs` (its resume asked for): true; false
 * when it didn't by then (or closed). Never throws. */
export function whenRunning(ctx: AudioContext, maxMs = CHIME_LATE_MS): Promise<boolean> {
  if (ctx.state === "running") return Promise.resolve(true);
  try {
    void ctx.resume().catch(() => {});
  } catch {
    /* no resume: the wait decides */
  }
  const until = Date.now() + maxMs;
  return new Promise<boolean>((done) => {
    const look = () => {
      if (ctx.state === "running") return done(true);
      if (ctx.state === "closed" || Date.now() >= until) return done(false);
      setTimeout(look, RESUME_POLL_MS);
    };
    setTimeout(look, 0);
  });
}

/** A {@link SpeechQueue.cue}'s player: `lead` ms of quiet, then `play`, done
 * `ms` later; cancel() ends it (a sound already started rings out — it is
 * short). The page's AudioContext not running yet (a cold first press):
 * `play` waits for it, up to {@link CHIME_LATE_MS} — and its `ms` count from
 * when it really plays — else the sound is dropped. */
function soundSpeaker(play: () => void, ms: number, lead: number): Speaker {
  let timer: ReturnType<typeof setTimeout> | undefined;
  let end = () => {};
  let over = false;
  return {
    speak: () =>
      new Promise<void>((resolve) => {
        end = resolve;
        const sound = () => {
          if (over) return;
          play();
          timer = setTimeout(resolve, ms);
        };
        const go = () => {
          const ctx = audioCtx;
          if (!ctx || ctx.state === "running") return sound();
          void whenRunning(ctx).then((ok) => (ok ? sound() : resolve()));
        };
        if (lead > 0) timer = setTimeout(go, lead);
        else go();
      }),
    cancel: () => {
      over = true;
      clearTimeout(timer);
      end();
    },
  };
}

/** The first audio after the app's own voice was cut is not the person (the
 * speaker's tail, the room): the server's recognizer drops this much. */
export const BARGE_SKIP_MS = 150;

/** The mic is keyed while the app may be talking (barge-in): stop talking
 * BEFORE the mic opens — the line being said is cut (never resumed) and,
 * when the person keyed it to answer an ask (`title`), every line still
 * queued for that ask is dropped; other sessions' lines wait for the mic to
 * be done. Returns how much of the first audio to skip (0 when nothing was
 * being said). */
export function bargeIn(q: SpeechQueue, title: string): number {
  const talking = q.busy;
  // Held first: dropping the ask's lines must not start another's.
  q.hold();
  if (title) q.cancel(title);
  return talking ? BARGE_SKIP_MS : 0;
}

/** The server's voice runs here or on a private network (GET /api/voice
 * `server_ready`: the built-in `local` / `system` engines, or an
 * OpenAI-shaped host whose detail names a local address — Voice Lab,
 * Kokoro): synthesising the confirmations ahead costs nothing. A paid API
 * (or anything unknown) is not prefetched. */
export function localVoice(cfg: VoiceCfg): boolean {
  const r = cfg.server_ready || {};
  const id = String(r.tts_backend || "").toLowerCase();
  if (id === "local" || id === "system") return true;
  return /(?:^|[\s/@])(?:localhost|0\.0\.0\.0|(?:127|10)\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+|[\w.-]+\.(?:local|localhost|ts\.net))(?:[:/\s]|$)/i.test(
    String(r.tts_detail || "")
  );
}

/** Which voice the server speaks in (its engine and detail): the speaker's
 * cache keeps a line's audio per voice. */
export function ttsVoiceKey(cfg: VoiceCfg): string {
  const r = cfg.server_ready || {};
  return String(r.tts_backend || "") + "|" + String(r.tts_detail || "");
}

/** The spoken confirmations, wired: a line goes through the queue's
 * front ({@link SpeechQueue.confirmLine}) with the live recorder muted while it
 * plays ({@link Recognizer.mute}); nothing when the switch is off. The
 * server voice's five fixed lines are synthesised ahead ({@link prime}) —
 * once, on the first gesture, when the voice is {@link localVoice}; for any
 * other server voice only after a line was first spoken without an error. */
export class Confirmer {
  private primed = false;

  constructor(
    private deps: {
      /** `audibleNow`: the queue's next line already sounds (it holds the
       * mute now — lifting it here would let the reply's audio in). */
      queue: Pick<SpeechQueue, "confirmLine"> & { readonly audibleNow?: boolean };
      /** Settings → Voice "Spoken confirmations". */
      on(): boolean;
      /** The recognizer listening now, if any. */
      mic(): Pick<Recognizer, "mute"> | null;
      /** The server's voice speaks (else the browser's: said directly). */
      server(): boolean;
      /** That voice is local ({@link localVoice}). */
      local(): boolean;
      speaker: { prefetch(lines: readonly string[]): Promise<void>; lastError: string };
    }
  ) {}

  /** Say `kind`'s line ({@link CONFIRM_LINES}), or a line of the router's
   * (a switch). Resolves once it was said, cut or dropped. The mic is muted
   * from its first sound to its end — unless the line after it already
   * sounds by then (its own mute stands). `force`: said with the switch
   * off too (the caller decided: {@link announcesActions}). */
  say(kind: ConfirmKind | { line: string }, opts: { force?: boolean } = {}): Promise<void> {
    const x = this.deps;
    if (!opts.force && !x.on()) return Promise.resolve();
    const line = typeof kind === "string" ? CONFIRM_LINES[kind] : kind.line;
    const mic = x.mic();
    let muted = false;
    return x.queue
      .confirmLine(line, {
        start: () => {
          muted = true;
          mic?.mute?.(true);
        },
      })
      .then(() => {
        if (muted && !x.queue.audibleNow) mic?.mute?.(false);
        if (muted && x.server() && !x.speaker.lastError) this.prime(true);
      });
  }

  /** Synthesise the five lines ahead (fire and forget), once: on a gesture
   * for a local voice; `spoke`: a line was just said by the server's voice
   * without an error — then for any voice. */
  prime(spoke = false): void {
    const x = this.deps;
    if (this.primed || !x.on() || !x.server() || (!spoke && !x.local())) return;
    this.primed = true;
    void x.speaker.prefetch(Object.values(CONFIRM_LINES)).catch(() => {});
  }
}

/** One chunk per sentence; long sentences cut at a comma past ~180 chars
 * (some engines stop mid-way on long utterances). mobile.js sentenceChunks. */
export function sentenceChunks(text: string): string[] {
  const s = String(text || "").replace(/\s+/g, " ").trim();
  if (!s) return [];
  const pieces: string[] = [];
  let start = 0;
  for (let i = 0; i < s.length; i++) {
    if (".!?".indexOf(s.charAt(i)) < 0) continue;
    let j = i + 1;
    while (j < s.length && ".!?\"')]".indexOf(s.charAt(j)) >= 0) j++;
    if (j >= s.length || s.charAt(j) === " ") {
      pieces.push(s.slice(start, j));
      start = j;
      i = j - 1;
    }
  }
  if (start < s.length) pieces.push(s.slice(start));
  const out: string[] = [];
  for (let sent of pieces) {
    sent = sent.trim();
    while (sent.length > 180) {
      let cut = Math.max(sent.lastIndexOf(", ", 180), sent.lastIndexOf("; ", 180));
      if (cut < 40) cut = sent.lastIndexOf(" ", 180);
      if (cut < 40) cut = 180;
      out.push(sent.slice(0, cut + 1).trim());
      sent = sent.slice(cut + 1).trim();
    }
    if (sent) out.push(sent);
  }
  return out;
}

/** Words whose period never ends a sentence ("e.g." and "U.S." are caught
 * as letter-dot runs). */
const ABBREVIATIONS = new Set([
  "mr", "mrs", "ms", "dr", "st", "jr", "sr", "vs", "etc", "approx", "cf", "no", "fig", "inc", "ltd", "dept", "est",
]);
/** A sentence shorter than this joins the next: a request per "OK." costs
 * more than it saves. */
export const SPEECH_SENTENCE_MIN = 12;
/** A longer sentence is cut at a comma (sentenceChunks' rule) — well
 * under POST /api/voice/speak's 1200-character cap. */
const SPEECH_SENTENCE_MAX = 180;

/** The server speaker's pieces: one per sentence, so the first can play
 * while the next is synthesised. Ends at `.` `?` `!` `;` (with any closing
 * quotes or brackets) before a space or the end — never inside "3.5", after
 * "e.g." / "U.S." or a title like "Dr.". A sentence under
 * {@link SPEECH_SENTENCE_MIN} characters joins the next; one over
 * {@link SPEECH_SENTENCE_MAX} is cut at a comma. */
export function speechSentences(text: string): string[] {
  const s = String(text || "").replace(/\s+/g, " ").trim();
  if (!s) return [];
  const pieces: string[] = [];
  let start = 0;
  for (let i = 0; i < s.length; i++) {
    const c = s.charAt(i);
    if (".!?;".indexOf(c) < 0) continue;
    let j = i + 1;
    while (j < s.length && ".!?\"')]”’".indexOf(s.charAt(j)) >= 0) j++;
    if (j < s.length && s.charAt(j) !== " ") continue; // "3.5", "e.g.x"
    if (c === ".") {
      const word = s.slice(s.lastIndexOf(" ", i) + 1, i + 1).replace(/^[("'“‘[]+/, "");
      if (/^(?:\p{L}\.){2,}$/u.test(word) || ABBREVIATIONS.has(word.slice(0, -1).toLowerCase())) continue;
    }
    pieces.push(s.slice(start, j).trim());
    start = j;
    i = j - 1;
  }
  if (start < s.length) pieces.push(s.slice(start).trim());
  const joined: string[] = [];
  let buf = "";
  for (const p of pieces) {
    if (!p) continue;
    buf = buf ? buf + " " + p : p;
    if (buf.length >= SPEECH_SENTENCE_MIN) {
      joined.push(buf);
      buf = "";
    }
  }
  if (buf) joined.push(buf);
  const out: string[] = [];
  for (let sent of joined) {
    while (sent.length > SPEECH_SENTENCE_MAX) {
      let cut = sent.lastIndexOf(", ", SPEECH_SENTENCE_MAX);
      if (cut < 40) cut = sent.lastIndexOf(" ", SPEECH_SENTENCE_MAX);
      if (cut < 40) cut = SPEECH_SENTENCE_MAX;
      out.push(sent.slice(0, cut + 1).trim());
      sent = sent.slice(cut + 1).trim();
    }
    if (sent) out.push(sent);
  }
  return out;
}

/** About how many characters the voice says a second (a request's audio is
 * guessed from its text). */
export const SPEECH_CHARS_PER_S = 15;
/** The server voice's one request costs at least this much speech's worth
 * of time however short the text (Voice Lab: 1.4 s for "Sent.", as for
 * 2.7 s of speech — synthesis ≈ max(1.4 s, 0.52 × the audio's length)): a
 * request after the first is packed to at least this much speech. */
export const SPEAK_REQUEST_MIN_S = 2.7;

/** The server speaker's requests ({@link serverSpeaker}): the first
 * sentence alone — the first sound is one short synthesis away — then the
 * rest packed a few sentences at a time: each request at least
 * {@link SPEAK_REQUEST_MIN_S} of speech (a short sentence alone costs the
 * engine's floor for a fraction of the sound, and a gap before each) and at
 * most about 1.5× the request before it, so it is back before that one has
 * finished playing (the engine synthesises about twice as fast as it
 * speaks). A long sentence stays a request of its own. */
export function speechRequests(text: string): string[] {
  const parts = speechSentences(text);
  if (parts.length <= 2) return parts;
  const out = [parts[0]];
  const minChars = SPEAK_REQUEST_MIN_S * SPEECH_CHARS_PER_S;
  let i = 1;
  while (i < parts.length) {
    const cap = Math.max(minChars, out[out.length - 1].length * 1.5);
    let req = parts[i++];
    while (i < parts.length && req.length < minChars && req.length + 1 + parts[i].length <= cap) req += " " + parts[i++];
    out.push(req);
  }
  return out;
}

const WORD_MS = 400; // onend fallback: per word…
const CHUNK_GRACE_MS = 1500; // …plus this per chunk
/** speechSynthesis gave no onstart this long after speak(): it is taken as
 * sounding (some voices never fire it). */
export const BROWSER_START_GRACE_MS = 300;

/** speechSynthesis, a chunk per sentence. Utterances stay referenced until
 * they end — a garbage-collected one never fires onend (mobile.js PhoneTTS). */
export function browserSpeaker(): Speaker & { available(): boolean; readonly reportsAudible: true } {
  const synth = typeof window !== "undefined" ? window.speechSynthesis : undefined;
  const keep: SpeechSynthesisUtterance[] = [];
  let gen = 0;
  const available = () => !!(synth && typeof SpeechSynthesisUtterance !== "undefined");
  const unkeep = (u: SpeechSynthesisUtterance) => {
    const i = keep.indexOf(u);
    if (i >= 0) keep.splice(i, 1);
  };
  return {
    available,
    reportsAudible: true as const,
    speak(text, audible) {
      return new Promise<void>((resolve) => {
        // Shaped for the voice only here: a caption shows the text as written.
        const parts = sentenceChunks(shapeForSpeech(text, pronounce()));
        if (!available() || !parts.length) return resolve();
        const g = gen;
        let i = 0;
        let heard = false;
        const sounding = () => {
          if (heard || g !== gen) return;
          heard = true;
          audible?.();
        };
        const next = () => {
          if (g !== gen || i >= parts.length) return resolve();
          const part = parts[i++];
          const u = new SpeechSynthesisUtterance(part);
          u.lang = navigator.language || "en-US";
          let done = false;
          const finish = () => {
            if (done) return;
            done = true;
            clearTimeout(timer);
            unkeep(u);
            next();
          };
          u.onend = finish;
          u.onerror = finish;
          u.onstart = sounding;
          const timer = setTimeout(finish, part.split(/\s+/).length * WORD_MS + CHUNK_GRACE_MS);
          keep.push(u);
          try {
            synth!.speak(u);
            if (!heard) setTimeout(sounding, BROWSER_START_GRACE_MS);
          } catch {
            finish();
          }
        };
        // Chrome swallows a speak() issued in the same tick as a cancel().
        setTimeout(next, 60);
      });
    },
    cancel() {
      gen++;
      try {
        synth?.cancel();
      } catch {
        /* nothing to cancel */
      }
    },
  };
}

function audioCtxCtor(): typeof AudioContext | undefined {
  return typeof window === "undefined"
    ? undefined
    : window.AudioContext || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
}

/** The page's one AudioContext (server playback and the server recognizer's
 * recording). Made on first use, which is inside the mic's click — a context
 * made outside a gesture stays suspended. */
let audioCtx: AudioContext | null = null;
export function voiceAudioCtx(): AudioContext | null {
  const AC = audioCtxCtor();
  if (!AC) return null;
  if (!audioCtx) {
    try {
      audioCtx = new AC();
    } catch {
      return null;
    }
    const ctx = audioCtx;
    // The capture worklet loads now, alongside the microphone's start (the
    // first recording never waits on it), and the open microphone's
    // pre-roll starts the moment the context runs.
    preloadCapture(ctx);
    ctx.addEventListener?.("statechange", () => audioCtxRunning(ctx));
  }
  if (audioCtx.state === "suspended") void audioCtx.resume().catch(() => {});
  return audioCtx;
}

/** The page's AudioContext when it runs — never makes or resumes one (the
 * microphone's pre-roll starts on it, outside any gesture). */
export function runningAudioCtx(): AudioContext | null {
  return audioCtx && audioCtx.state === "running" ? audioCtx : null;
}

/** POST /api/voice/speak → audio bytes (Content-Type the backend's: mpeg for
 * a host, wav for piper) played through the one AudioContext — a request
 * for the first sentence, then a few sentences at a time
 * ({@link speechRequests}), pipelined: the first is asked for at once and
 * plays the moment it is back, the next is asked for while it plays (one
 * request in flight, never more — a CPU engine would only share itself
 * out), and each is scheduled to start the instant the one before ends, so
 * nothing waits on the whole line and there is no gap when the next is
 * ready in time. speak() resolves when the last request
 * has played; cancel() stops what plays, aborts the request and drops the
 * rest. Any failure — 409 (turned off), 502, the network, a clip that won't
 * decode, a context that isn't running — says the rest of the line with
 * `fallback`, as one call.
 * A {@link CONFIRM_LINES} sentence (or another line `cacheable` allows, a
 * switch's) is kept in memory per voice (`voiceKey`: the engine and the
 * voice) once synthesised — a slow local voice takes seconds per line, a
 * confirmation must not — and {@link prefetch} fills that ahead. */
export function serverSpeaker(
  fallback: Speaker,
  opts: {
    voiceKey?: () => string;
    cacheable?: (part: string) => boolean;
    /** The first {@link serverSpeaker} `onStarting` (it can be set later). */
    onStarting?: (on: boolean) => void;
    now?: () => number;
    /** More of /api/voice/speak's body (`{as: "assistant"}`: the assistant's voice). */
    body?: Record<string, unknown>;
  } = {}
): Speaker & {
  lastError: string;
  /** The voice may be starting (true: a line's first sentence has waited
   * {@link VOICE_STARTING_MS} on a voice that may be cold), then has
   * answered, failed or been cut (false). */
  onStarting?: (on: boolean) => void;
  readonly reportsAudible: true;
  prefetch(lines: readonly string[]): Promise<void>;
  cached(part: string): boolean;
} {
  let gen = 0;
  /** Synthesised confirmations: voice + "\n" + sentence → its audio. */
  const cache = new Map<string, Promise<AudioBuffer>>();
  const cacheable = opts.cacheable || isConfirmSentence;
  const keyOf = (part: string) => (opts.voiceKey ? opts.voiceKey() : "") + "\n" + part;
  /** Sources started or scheduled (the one playing and the one queued
   * after it), each with what ends its promise. */
  const playing = new Map<AudioBufferSourceNode, () => void>();
  let inflight: AbortController | null = null;
  const now = opts.now || (() => Date.now());
  /** When the server's voice last answered (0: not on this page yet). */
  let lastOk = 0;
  /** The pending "starting" note: its timer, and whether it showed. */
  let startTimer: ReturnType<typeof setTimeout> | undefined;
  let startShown = false;
  const endStarting = () => {
    clearTimeout(startTimer);
    startTimer = undefined;
    if (startShown) {
      startShown = false;
      self.onStarting?.(false);
    }
  };
  /** Watch `p` (a line's first request): past {@link VOICE_STARTING_MS} on a
   * voice that may be cold, say it is starting until `p` settles. */
  const watchStart = (p: Promise<unknown>) => {
    if (!self.onStarting || !voiceMayBeCold(lastOk, now())) return;
    endStarting();
    const t = setTimeout(() => {
      if (startTimer !== t) return;
      startShown = true;
      self.onStarting?.(true);
    }, VOICE_STARTING_MS);
    startTimer = t;
    const done = () => {
      if (startTimer === t || startShown) endStarting();
    };
    p.then(done, done);
  };

  const fetchAudio = async (ctx: AudioContext, part: string, ac: AbortController): Promise<AudioBuffer> => {
    const r = await apiRaw("/api/voice/speak", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...opts.body, text: part }),
      signal: ac.signal,
    });
    if (!r.ok) {
      const j = (await r.json().catch(() => ({}))) as { error?: string };
      throw new Error(j.error || "HTTP " + r.status);
    }
    const buf = await r.arrayBuffer();
    lastOk = now();
    return ctx.decodeAudioData(buf);
  };

  /** Keep `p` as `part`'s audio once it decodes (a failure is forgotten). */
  const remember = (part: string, p: Promise<AudioBuffer>) => {
    const key = keyOf(part);
    if (cache.size >= SPEAK_CACHE_MAX) cache.delete(cache.keys().next().value as string);
    cache.set(key, p);
    p.catch(() => {
      if (cache.get(key) === p) cache.delete(key);
    });
  };

  const self = {
    lastError: "",
    onStarting: opts.onStarting as ((on: boolean) => void) | undefined,
    /** speak() calls its `audible` as the first sentence starts playing. */
    reportsAudible: true as const,
    /** The audio of `part` is in memory for the current voice. */
    cached(part: string): boolean {
      return cache.has(keyOf(part));
    },
    /** Synthesise `lines`' sentences into the cache, one request at a time,
     * skipping what is there; never throws. */
    async prefetch(lines: readonly string[]): Promise<void> {
      const ctx = voiceAudioCtx();
      if (!ctx) return;
      for (const line of lines)
        for (const part of speechRequests(line)) {
          if (cache.has(keyOf(part))) continue;
          const p = fetchAudio(ctx, part, new AbortController());
          remember(part, p);
          await p.catch(() => {});
        }
    },
    async speak(text: string, audible?: () => void): Promise<void> {
      const parts = speechRequests(text);
      if (!parts.length) return;
      const g = gen;
      const ctx = voiceAudioCtx();
      // A context still resuming (or iOS's "interrupted") gets a moment
      // before the line goes to the other voice.
      if (ctx && ctx.state !== "running") await whenRunning(ctx);
      if (g !== gen) return;
      if (!ctx || ctx.state !== "running") {
        self.lastError = "AudioContext " + (ctx ? ctx.state : "missing");
        return fallback.speak(parts.join(" "), audible);
      }
      /** Ask for sentence `i` (the one request in flight) — or take it from
       * the cache. */
      const ask = (i: number): Promise<AudioBuffer> => {
        const hit = cacheable(parts[i]) ? cache.get(keyOf(parts[i])) : undefined;
        if (hit) return hit;
        const ac = new AbortController();
        inflight = ac;
        const p = fetchAudio(ctx, parts[i], ac);
        const settled = () => {
          if (inflight === ac) inflight = null;
        };
        p.then(settled, settled);
        if (i === 0) watchStart(p);
        if (cacheable(parts[i])) remember(parts[i], p);
        return p;
      };
      /** Where the last scheduled sentence ends (the context's clock). */
      let endAt = 0;
      let last: Promise<void> = Promise.resolve();
      let next = ask(0);
      for (let i = 0; i < parts.length; i++) {
        let audio: AudioBuffer;
        try {
          audio = await next;
        } catch (err) {
          if (g !== gen) return;
          self.lastError = String((err as Error)?.message || err);
          // What is scheduled plays out first; the rest goes in one call.
          await last;
          if (g !== gen) return;
          return fallback.speak(parts.slice(i).join(" "), audible);
        }
        if (g !== gen) return;
        self.lastError = "";
        // Sentence i plays (or waits its turn) while i+1 is asked for.
        if (i + 1 < parts.length) next = ask(i + 1);
        const at = Math.max(ctx.currentTime, endAt);
        endAt = at + (audio.duration || 0);
        last = play(ctx, audio, at);
        // The first sentence starts now: the line is heard from here.
        if (i === 0) audible?.();
      }
      await last;
    },
    cancel() {
      gen++;
      inflight?.abort();
      inflight = null;
      endStarting();
      const cut = [...playing];
      playing.clear();
      for (const [s, finish] of cut) {
        try {
          s.onended = null;
          s.stop(0);
        } catch {
          /* already stopped */
        }
        finish();
      }
      fallback.cancel();
    },
  };

  /** `audio` started at `at` (the context's clock); resolves when it ends. */
  const play = (ctx: AudioContext, audio: AudioBuffer, at: number) =>
    new Promise<void>((resolve) => {
      const src = ctx.createBufferSource();
      src.buffer = audio;
      src.connect(ctx.destination);
      let done = false;
      const finish = () => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        playing.delete(src);
        resolve();
      };
      src.onended = finish;
      const wait = Math.max(0, at - ctx.currentTime) + (audio.duration || 1);
      const timer = setTimeout(finish, wait * 1000 + CHUNK_GRACE_MS);
      playing.set(src, finish);
      try {
        src.start(at);
      } catch {
        finish();
      }
    });
  return self;
}

// --- recognition ------------------------------------------------------------------ //

/** What a recognizer reports. `onEnd` fires once the recognizer is down for
 * good (never after {@link Recognizer.stop}, which reports nothing). */
export interface RecognizerHandlers {
  onInterim(text: string): void;
  /** One phrase. `spokeAt`: when it began (ms) — the server recognizer's
   * first loud block; absent: the recognizer can't say. */
  onFinal(text: string, spokeAt?: number): void;
  onStatus?(text: string): void;
  /** The server recognizer's detector heard speech (it has no interim words). */
  onSpeech?(): void;
  /** The microphone records (each phrase's recording started). */
  onReady?(): void;
  /** Each audio block's RMS (0..1) — the level meter's. */
  onLevel?(rms: number): void;
  /** Streaming (the server's, when it can): words committed — final, typed
   * as they come. */
  onWords?(text: string): void;
  /** Streaming: the unstable tail after them — for the caption (a stream's
   * end and a fall-back clear it with ""). */
  onPartial?(text: string): void;
  /** Streaming: one chunk's answer whole — the words committed by it and the
   * unstable tail after them — so the typing corrects once per answer. When
   * set, a chunk's answer comes here instead of onWords + onPartial. */
  onStream?(committed: string, partial: string): void;
  /** Streaming: nothing is typed in the message yet (its box is empty) —
   * a lone command's short end-of-turn hold applies only then
   * ({@link turnHoldMs}). Absent: taken as empty. */
  blank?(): boolean;
  /** Streaming: asked as each phrase's stream opens — the message's words
   * already said ahead of it ({@link LiveDictation.streamContext}) and the
   * session they go to, so the accurate model hears the phrase in its
   * sentence. Absent or null: none. */
  context?(): { context?: string; session?: string } | null;
  /** Streaming: a phrase ended (a pause, or finish()); its last words.
   * `final`: the whole phrase as the server's accurate model heard it — set
   * only when it differs from the fast words (absent: they stand). */
  onFlush?(text: string, final?: string, spokeAt?: number): void;
  /** A passing line for the caption (streaming fell back to clips). */
  onNotice?(text: string): void;
  /** The person talks over a line the app is saying (the server's
   * recognizer, muted for it, heard a voice: {@link BargeDetector}). The
   * line is the caller's to cut; the recorder's mute is lifted by that (its
   * tail then the person's, not skipped). */
  onBarge?(): void;
  /** `code`: the Web Speech error names, plus "server" / "nothing-recorded"
   * (`detail` is then the line to show). */
  onError(code: string, detail?: string): void;
  onEnd(): void;
}

export interface Recognizer {
  readonly name: "browser" | "server";
  available(): boolean;
  /** `skipMs`: the first audio to drop — the app's own voice was cut just
   * before (barge-in, {@link BARGE_SKIP_MS}); the server's recognizer
   * honours it, Web Speech can't. */
  start(h: RecognizerHandlers, opts?: { skipMs?: number }): boolean;
  /** End listening but deliver what was said (a final, or the clip's words)
   * before onEnd. */
  finish(): void;
  /** Drop everything; report nothing. */
  stop(): void;
  /** Give the microphone back. */
  release(): void;
  /** `on`: the app is saying a confirmation — its audio is not the person:
   * dropped (the server's recognizer) or its words ignored (Web Speech) until
   * off, and {@link BARGE_SKIP_MS} after. The phrase and the listen go on. */
  mute?(on: boolean): void;
  /** End the phrase under way now, as its silence would (its words — and
   * the server's accurate final — follow); listening goes on. Web Speech
   * can't: its final comes when it decides. */
  endPhrase?(): void;
}

type SpeechRecCtor = new () => {
  continuous: boolean;
  interimResults: boolean;
  maxAlternatives: number;
  lang: string;
  onresult: ((ev: { resultIndex: number; results: ArrayLike<{ isFinal: boolean; 0?: { transcript: string } }> }) => void) | null;
  onerror: ((ev: { error?: string }) => void) | null;
  onend: (() => void) | null;
  start(): void;
  stop(): void;
  abort?(): void;
};

export function speechRecCtor(): SpeechRecCtor | null {
  if (typeof window === "undefined") return null;
  const w = window as unknown as Record<string, SpeechRecCtor | undefined>;
  return w.SpeechRecognition || w.webkitSpeechRecognition || null;
}

/** Web Speech, continuous: Chrome keeps one session up across pauses and
 * delivers a final per phrase, but still ends after a long pause — restarted
 * here while listening is wanted (the restart is the adapter's own business,
 * so the component sees one long listen). A `network` error is a Chromium
 * without Google's speech key (Arc, Electron…): it fails every start, so the
 * first one is reported rather than retried forever. */
export function browserRecognizer(): Recognizer {
  let rec: InstanceType<SpeechRecCtor> | null = null;
  let want = false;
  let finishing = false;
  let h: RecognizerHandlers | null = null;
  let interim = "";
  let rapid = 0;
  let startedAt = 0;
  /** Muted (a confirmation plays), or its tail: results until then are the
   * app's own voice. */
  let muted = false;
  let deafUntil = 0;

  const open = (): boolean => {
    const Ctor = speechRecCtor();
    if (!Ctor || !h) return false;
    let r: InstanceType<SpeechRecCtor>;
    try {
      r = new Ctor();
    } catch {
      return false;
    }
    r.continuous = true;
    r.interimResults = true;
    r.maxAlternatives = 1;
    r.lang = navigator.language || "en-US";
    interim = "";
    r.onresult = (ev) => {
      if (muted || Date.now() < deafUntil) return;
      let finals = "";
      interim = "";
      for (let i = ev.resultIndex; i < ev.results.length; i++) {
        const res = ev.results[i];
        const txt = res[0]?.transcript || "";
        if (res.isFinal) finals += txt;
        else interim += txt;
      }
      if (finals.trim()) h?.onFinal(finals.trim());
      else h?.onInterim(interim.trim());
    };
    r.onerror = (ev) => {
      const code = ev?.error || "error";
      if (code === "no-speech" || code === "aborted") return; // onend decides
      want = false;
      h?.onError(code);
    };
    r.onend = () => {
      if (rec === r) rec = null;
      // A final can stall and never arrive: the last interim reading is what
      // was said, and better than losing the words.
      const tail = interim.trim();
      interim = "";
      if (tail) h?.onFinal(tail);
      if (want && !finishing) {
        rapid = Date.now() - startedAt < 1000 ? rapid + 1 : 0;
        if (rapid < 8) {
          setTimeout(() => {
            if (want && !rec && !open()) {
              want = false;
              h?.onError("start-failed");
              h?.onEnd();
            }
          }, 250);
          return;
        }
        h?.onError("start-failed", "Speech recognition keeps stopping — try again, or another browser.");
      }
      want = false;
      finishing = false;
      h?.onEnd();
    };
    rec = r;
    startedAt = Date.now();
    try {
      r.start();
    } catch {
      rec = null;
      return false;
    }
    return true;
  };

  const self: Recognizer = {
    name: "browser",
    available: () => !!speechRecCtor(),
    mute(on) {
      if (!on && muted) deafUntil = Date.now() + BARGE_SKIP_MS;
      muted = on;
    },
    start(handlers) {
      self.stop();
      h = handlers;
      want = true;
      finishing = false;
      rapid = 0;
      muted = false;
      deafUntil = 0;
      if (!open()) {
        want = false;
        return false;
      }
      return true;
    },
    finish() {
      if (!rec) {
        if (want) {
          want = false;
          h?.onEnd();
        }
        return;
      }
      finishing = true;
      want = false;
      try {
        rec.stop(); // delivers the pending final, then onend
      } catch {
        rec = null;
        h?.onEnd();
      }
    },
    stop() {
      want = false;
      finishing = false;
      const r = rec;
      rec = null;
      h = null;
      if (!r) return;
      r.onresult = r.onerror = r.onend = null;
      try {
        if (r.abort) r.abort();
        else r.stop();
      } catch {
        /* not started */
      }
    },
    release() {
      self.stop();
    },
  };
  return self;
}

/** Quiet after speech that ends a clip: short, so each phrase is typed
 * soon after it is said. A streamed phrase ends on the server's verdict
 * instead, when it gives one ({@link turnHoldMs}). */
export const SILENCE_MS = 1100;
/** The least quiet that ends a phrase, whatever the server's verdict says:
 * under it a breath between two words would cut the phrase. */
export const TURN_MIN_MS = 300;
/** The most: a verdict that asks for more is read as this. */
export const TURN_MAX_MS = 3000;
/** The least quiet that ends a `sentence` / `question` verdict's phrase
 * into an empty box: that phrase goes to the router alone, so whisper's
 * full stop at a breath ("…so far. [pause] over and out") must not cut a
 * command in half. Words typed already: the verdict's own (900). */
export const FIRST_PHRASE_HOLD_MS = 1100;
/** …but this, when the words so far can't be the start of a command (the
 * router would type them anyway: a cut there costs nothing). */
export const FIRST_PHRASE_PLAIN_HOLD_MS = 900;
/** A `sentence` / `question` verdict's quiet once the phrase has settled —
 * every word agreed by two passes and the last pass heard the quiet after
 * them — with words already typed: nothing is left for more quiet to tell. */
export const SETTLED_HOLD_MS = 600;

/** What the recognizer has of the phrase so far, for {@link turnHoldMs}:
 * its words (committed, then the unstable tail), and whether they settled
 * (no unstable tail, the last pass after the last loud block). */
export interface PhraseHeard {
  text: string;
  settled: boolean;
}

/** The server's end-of-turn verdict on a streamed phrase (each chunk's
 * `turn`: core/speech_turn.py) — how much quiet ends it now (`hold`, ms),
 * why (`reason`: control, command, sentence, comma, connector, wake…), and
 * when the audio it judged was taken (`at`, ms). */
export interface TurnVerdict {
  hold: number;
  reason: string;
  at: number;
}

/** A chunk response's `turn` ({p, hold_ms, reason}), or null (an older
 * server, a failed verdict, anything malformed). */
export function parseTurn(v: unknown, at: number): TurnVerdict | null {
  if (!v || typeof v !== "object") return null;
  const o = v as { hold_ms?: unknown; reason?: unknown };
  const hold = Number(o.hold_ms);
  if (!Number.isFinite(hold) || hold <= 0) return null;
  return { hold, reason: String(o.reason || ""), at };
}

/** The quiet (ms, as the meter heard it) that ends a streamed phrase: the
 * latest chunk's verdict — 350 after a radio word or a lone command, 900
 * after a sentence, 1500 after a comma or a trailing "and…" — within
 * {@link TURN_MIN_MS}..{@link TURN_MAX_MS}; {@link SILENCE_MS} without one.
 * Never shorter than SILENCE_MS when the voice was heard after the audio
 * the verdict judged (`loudAt` later than its `at`: it is about older
 * words), nor for a lone command once words are typed (`blank` false: the
 * server can't see the box — "next session" mid-message is words); a
 * sentence or question into an empty box (`blank`: the first phrase, which
 * is routed alone) waits at least {@link FIRST_PHRASE_HOLD_MS} —
 * {@link FIRST_PHRASE_PLAIN_HOLD_MS} when its words (`heard`) can't begin a
 * command. A sentence or question after words were typed whose words
 * settled (`heard.settled`) ends after {@link SETTLED_HOLD_MS}. */
export function turnHoldMs(turn: TurnVerdict | null, loudAt: number, blank: boolean, heard?: PhraseHeard | null): number {
  if (!turn) return SILENCE_MS;
  let hold = turn.reason === "command" && !blank ? SILENCE_MS : turn.hold;
  const sentence = turn.reason === "sentence" || turn.reason === "question";
  if (blank && sentence) {
    const plain = !!heard?.text && !mayBeCommand(heard.text) && !mayBeCommand(holdBack(heard.text).type);
    hold = Math.max(hold, plain ? FIRST_PHRASE_PLAIN_HOLD_MS : FIRST_PHRASE_HOLD_MS);
  } else if (sentence && heard?.settled && heard.text) hold = Math.min(hold, SETTLED_HOLD_MS);
  if (loudAt > turn.at) hold = Math.max(hold, SILENCE_MS);
  return Math.min(TURN_MAX_MS, Math.max(TURN_MIN_MS, hold));
}

/** A recording's own clock on the page's (ms): a block's callback runs no
 * sooner than its audio was recorded — later when the page is busy (a
 * render, a flood of terminal output) — so the earliest `now − seconds
 * recorded` seen is when the recording's second 0 was, and a late callback
 * never makes speech look later than it began (the answer timing rule,
 * {@link ANSWER_AFTER_MS}, reads it). {@link reset} when the count stops
 * following time (muted: its blocks aren't counted). */
export class RecordingClock {
  private zero = Infinity;
  /** A block ending `endS` seconds into the recording was handed over `now`. */
  block(endS: number, now = Date.now()): void {
    this.zero = Math.min(this.zero, now - endS * 1000);
  }
  reset(): void {
    this.zero = Infinity;
  }
  /** When second `s` of the recording was, on the page's clock (ms; 0 before a block). */
  at(s: number): number {
    return Number.isFinite(this.zero) ? Math.round(this.zero + s * 1000) : 0;
  }
}
import { CAPTURE_MUTE_HOLD_S, PRE_ROLL_RATE } from "./voiceMic";
const CLIP_MAX_MS = 12_000; // the hard cap on one clip
const UPLOAD_TIMEOUT_MS = 45_000;
const CLIP_RATE = 16_000; // the built-in recognizers' rate; whisper's too
/** A let-go phrase's last bit of audio under this many bytes (~50 ms) isn't sent. */
const TAIL_MIN_BYTES = 44 + 1600;
/** A phrase's first chunk goes once this much speech is recorded (the
 * lead-in before it rides along): the first words show up after one pass. */
export const FIRST_CHUNK_S = 0.15;
/** Later chunks, before the server has said how long a pass takes. */
const LATER_CHUNK_S = 0.3;

/** The least new audio for a phrase's next chunk: about as long as the last
 * pass took (`passMs`, the server's `ms`) × 0.8, held to 150–600 ms — a fast
 * server is fed small chunks often, a slow one no slivers ({@link
 * LATER_CHUNK_S} while unknown). One chunk is in flight at a time; audio that
 * comes meanwhile is sent together when it is back. */
export function chunkMinS(passMs: number | null): number {
  if (passMs == null || !(passMs > 0)) return LATER_CHUNK_S;
  return Math.min(600, Math.max(150, passMs * 0.8)) / 1000;
}

/** A stream opened at a phrase's start and still unused this long is opened
 * again at the first word: the server forgets one 60 s after its last chunk
 * (speech_stream.EXPIRE_SECONDS). */
export const STREAM_FRESH_MS = 40_000;

/** How long a clip's transcript may take after letting go (a whole phrase
 * uploaded at once, transcribed whole). */
export const CLIP_FINISH_WAIT_MS = 45_000;

/** The toggle's second click / the hold's release waits for the phrase in
 * flight this long before stopping with what it has: a streamed phrase's
 * words are typed already — only its accurate final is awaited, the finals'
 * usual round trip ({@link overWaitMs}); a clip's transcript is all of it
 * ({@link CLIP_FINISH_WAIT_MS}); Web Speech's final {@link HOLD_FINAL_WAIT_MS}. */
export function finishWaitMs(rec: { name: string; streaming?: boolean; finalMs?: number | null }): number {
  if (rec.name !== "server") return HOLD_FINAL_WAIT_MS;
  return rec.streaming ? overWaitMs(finalClock.typical(rec.finalMs)) : CLIP_FINISH_WAIT_MS;
}

/** Barge-in by voice, while the app says a line: the microphone's level is
 * read this often (an analyser; the recorder drops muted blocks unmetered)… */
export const BARGE_POLL_MS = 40;
/** …and this many loud reads in a row (≈ {@link BARGE_POLL_MS} × this after
 * the voice starts) are the person talking over the line. */
export const BARGE_POLLS = 3;
/** A read is loud over the app's voice from this RMS (twice {@link
 * RMS_SPEECH}) — and three times what the microphone hears of the app's own
 * voice ({@link BargeDetector.bleed}). */
export const BARGE_RMS = 0.04;
/** The mute lifted this soon after a barge-in is for it: the recorder's tail
 * is the person's, not the voice dying away (no {@link BARGE_SKIP_MS}). */
export const BARGE_FRESH_MS = 1000;
/** A barge-in lifts the mute with this much of the muted audio kept: the
 * detector's {@link BARGE_POLLS} reads plus the word's soft start. */
export const BARGE_REWIND_MS = BARGE_POLL_MS * BARGE_POLLS + 150;
/** The microphone went away mid-listen: the caption's line while it is
 * opened again (the system's default microphone now). */
export const MIC_LOST_LINE = "The microphone went away — listening on the default one";
/** …and when it goes away again this soon after, listening stops with this. */
export const MIC_GONE_LINE = "The microphone went away — check it, then click the mic";
export const MIC_LOST_RETRY_MS = 3000;
/** A stream open answered 409 for the server's stream cap ({@link
 * streamFull}: a stream still ending, another window) is asked again this
 * often… */
export const STREAM_OPEN_RETRY_MS = 300;
/** …this many times (≈1.5 s, the phone's VOICE_STREAM_FULL_MAX_MS); still
 * full, that phrase goes as a clip and the next one streams again — never
 * a downgrade that sticks for the rest of the dictation. */
export const STREAM_OPEN_RETRIES = 5;
/** A 409 on POST /api/voice/stream that is the server's stream CAP ("…
 * already open: end one first") — passing — not "this backend does not
 * stream" (clips for the rest of the dictation). mobile.js voiceStreamFull. */
export const streamFull = (error: unknown) => /already open|end one first/i.test(String(error || ""));
/** Before its first word a streamed phrase is sent this much of what came
 * before its first loud block (the word's soft start: "stop", not "top") —
 * a rolling window, the phone's VOICE_LEAD_MS. */
export const LEAD_IN_MS = 300;
/** Loud blocks in a row that are a voice, not a blip: a click, a knock, a
 * key is one ~85 ms block; a syllable is two or more. A streamed phrase
 * that never had this many, and of which the server heard no word, is
 * dropped as noise instead of ending as an (empty) phrase. */
export const ONSET_RUN = 2;
/** A phrase's speech this long (ms) lets its noise floor learn again (a fan
 * switched on is a new room) — lib/wakeWord's RELEARN_MS. */
export const FLOOR_RELEARN_MS = 5000;

/** The dictation's level detector for one phrase, fed each block's RMS: the
 * noise floor (from the room's quiet, lib/voiceMic {@link noiseFloor}) and
 * whether a block is loud ({@link isLoud}). As the standby's detector
 * (lib/wakeWord LoneUtterance): the floor never creeps up while the phrase
 * is in speech (for {@link FLOOR_RELEARN_MS}) — a steady voice would turn
 * "quiet" and a sentence end early — and never under the start chime
 * (`hush`: no word starts in it; the floor only goes down). `run` / `best`:
 * loud blocks in a row now / at most ({@link ONSET_RUN}). Pure. */
export class PhraseLevel {
  run = 0;
  best = 0;
  private speechMs = 0;
  constructor(public floor = 0) {}

  /** One block (`blockMs` long): loud or not. `speech`: the phrase is in
   * speech (its first word was heard). */
  feed(rms: number, blockMs: number, o: { hush?: boolean; speech?: boolean } = {}): boolean {
    if (o.speech) this.speechMs += Math.max(0, blockMs);
    const learn = !o.hush && (!o.speech || this.speechMs > FLOOR_RELEARN_MS);
    this.floor = learn || this.floor === 0 ? noiseFloor(this.floor, rms) : Math.min(this.floor, rms);
    const loud = !o.hush && isLoud(rms, this.floor);
    this.run = loud ? this.run + 1 : 0;
    this.best = Math.max(this.best, this.run);
    return loud;
  }
}

/** Speech in `pcm` (at `rate`): cut in ~85 ms blocks through a
 * {@link PhraseLevel} from `floor`, the first run of {@link ONSET_RUN} loud
 * blocks — its start and the last loud block's end (s into `pcm`) — or null
 * (quiet, or blips only). A wake's lead-in is read so ({@link MicKeeper.takeLead}). */
export function leadSpeech(pcm: Float32Array, rate: number, floor = 0): { startS: number; endS: number } | null {
  const n = Math.max(1, Math.round(rate * 0.085));
  const lv = new PhraseLevel(floor);
  let start = -1;
  let end = -1;
  let runAt = 0;
  for (let at = 0; at + n / 2 <= pcm.length; at += n) {
    const b = pcm.subarray(at, Math.min(pcm.length, at + n));
    const loud = lv.feed(rmsOf(b), (b.length / rate) * 1000, { speech: start >= 0 });
    if (!loud) continue;
    if (lv.run === 1) runAt = at;
    if (start < 0 && lv.run >= ONSET_RUN) start = runAt;
    if (start >= 0) end = at + b.length;
  }
  return start < 0 ? null : { startS: start / rate, endS: end / rate };
}

/** Hears for the person while the app talks. What reaches the microphone
 * of the app's own voice — what the browser's echo cancellation leaves of
 * the speakers — is learnt as a floor ({@link bleed}: the first read seeds
 * it, every read since moves it a little, kept from line to line, so a
 * machine without cancellation never barges on its own voice); a read over
 * {@link BARGE_RMS} and three times that floor is loud, and
 * {@link BARGE_POLLS} loud reads in a row are a barge-in (then the run
 * starts over). Pure, so it is pinned by tests. */
export class BargeDetector {
  /** The app's voice as the microphone hears it (a read's RMS, averaged). */
  bleed = 0;
  /** The last read was loud. */
  loud = false;
  private run = 0;
  /** A barge-in was signalled this line: what follows is the person, not
   * the app's voice — the floor learns nothing more until the next line. */
  private barged = false;

  /** A line starts (the recorder is muted for it). */
  begin(): void {
    this.run = 0;
    this.loud = false;
    this.barged = false;
  }

  /** One read's RMS while muted: true when it completes a barge-in. */
  block(rms: number): boolean {
    this.loud = this.bleed > 0 && rms > Math.max(BARGE_RMS, this.bleed * 3);
    if (!this.barged) this.bleed = this.bleed > 0 ? this.bleed * 0.95 + rms * 0.05 : rms;
    this.run = this.loud ? this.run + 1 : 0;
    if (this.run < BARGE_POLLS) return false;
    this.run = 0;
    this.barged = true;
    return true;
  }
}

/** One phrase being recorded. `stream`: it streams (else it is a clip — clip
 * mode, or streaming fell back); `sid` the stream once open (`opened` when);
 * `busy` the one request in flight (the open, or a chunk); `fresh`: speech
 * not sent yet; `sent`: a chunk went; `trimS` / `speechS`: the recording's
 * time at the last lead-in trim and where speech began. */
interface Phrase {
  rec: WavRecording | null;
  heard: boolean;
  stream: boolean;
  sid: string | null;
  opened: number;
  busy: Promise<void> | null;
  fresh: boolean;
  ending: boolean;
  sent: boolean;
  trimS: number;
  speechS: number;
  /** When its speech began (ms, the clock; 0: not yet) — handed over with
   * its words ({@link RecognizerHandlers.onFlush}): its first loud block on
   * the recording's own {@link clock}. */
  spokeAt: number;
  /** The recording's seconds on the page's clock. */
  clock: RecordingClock;
  /** When the meter last heard its voice (ms; 0: not yet). */
  loudAt: number;
  /** The latest chunk's end-of-turn verdict (null: none — clips, an older
   * server): the quiet that ends it ({@link turnHoldMs}). */
  turn: TurnVerdict | null;
  /** The pre-roll (16 kHz) its speech began in — sent in front of its first
   * audio ({@link PRE_ROLL_ONSET_S}), or its lead-in ({@link LEAD_IN_MS}, a
   * wake's: what was said after the word); null once sent, or none. */
  pre: Float32Array | null;
  /** The most loud blocks in a row it had ({@link ONSET_RUN}). */
  run: number;
  /** Its committed words so far, and the latest unstable tail (streaming). */
  said: string;
  partial: string;
  /** When the last chunk with an answer was taken (ms; 0: none yet). */
  passAt: number;
  /** Ended for a control word ({@link Recognizer.endPhrase}): its /end's
   * round trip is a sample for {@link finalClock}. */
  byWord: boolean;
  /** When it was ended (ms; 0: not yet). */
  endAt: number;
  /** Its end went to the handlers (onFlush) — by its /end, or ahead of it
   * when the next phrase's words came first ({@link settleBefore}). */
  flushed: boolean;
  /** Resolves once its words are all in (its last chunk back) or it has no
   * more to give; null before it ends. */
  heardAll: Promise<void> | null;
}

/** A response's words (the server joins them with spaces). */
/** A phrase's `heard` (/end) goes with its route call at most this long
 * after the /end came back. */
export const HEARD_FRESH_MS = 3000;
const wordsOf = (v: unknown) => String((Array.isArray(v) ? v.join(" ") : v) || "").trim();
const langTag = () => String(navigator.language || "en").split(";")[0];

/** This page's own id for POST /api/voice/stream `{client}`: random once per
 * page load, so the server's per-client stream cap (CLIENT_STREAMS) counts
 * THIS window — two windows or tabs of one browser (one address, one user
 * agent) never evict each other's streams. */
export const VOICE_CLIENT_ID = (() => {
  try {
    if (globalThis.crypto?.randomUUID) return "d-" + globalThis.crypto.randomUUID();
  } catch {
    /* fall through */
  }
  return "d-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 12);
})();

/** The server's recognizer (mobile.js ServerSTT), fed by lib/wavRecorder as
 * 16 kHz PCM through the page's one AudioContext (the built-in Windows /
 * macOS recognizers take nothing but PCM), in one of two ways:
 *  - one WAV clip per phrase: ended by {@link SILENCE_MS} of quiet after
 *    speech or the cap, uploaded whole as `audio/wav` to POST
 *    /api/voice/transcribe; the response's `{text}` is the phrase;
 *  - streaming, when `opts.stream()` says the server can (GET /api/voice
 *    `server_ready.stt_stream`): POST /api/voice/stream opens a phrase's
 *    stream as the phrase starts — the first one in parallel with the
 *    microphone, so its round trip is over before the first word — and from
 *    the phrase's first speech on (the quiet before it is never sent),
 *    whenever the previous request is back and enough new audio exists
 *    ({@link FIRST_CHUNK_S} of speech for the first, then {@link chunkMinS}
 *    from the server's pass time), the recorder's take() goes to
 *    /stream/{id}/chunk — its `committed` words (final) and its `partial`
 *    (unstable) go to onStream together (else onWords, onPartial). The same quiet ends the
 *    phrase: /end flushes its last words to onFlush, and the next phrase
 *    opens a new stream (a phrase that heard nothing DELETEs its). No cap. A
 *    409 that says the server does not stream, or any failed request, falls
 *    back to clips for the rest of the dictation; the stream cap's 409 is
 *    asked again ({@link STREAM_OPEN_RETRIES}), then only that phrase is a clip.
 * While listening is wanted the next phrase starts recording as soon as one
 * ends, so phrases arrive one by one like Chrome's finals. The microphone is
 * `opts.mic`'s (lib/voiceMic): held while listening, kept warm a while after,
 * given back by {@link Recognizer.release}. */
export function serverRecognizer(
  opts: { stream?: () => boolean; mic?: MicKeeper } = {}
): Recognizer & {
  /** `chimeMs`: the start chime's span — recorded, but no word starts in it
   * (`skipMs` drops audio: the app's own voice cut just before). */
  start(h: RecognizerHandlers, opts?: { skipMs?: number; chimeMs?: number }): boolean;
  lastError: string;
  mic: MicKeeper;
  passMs: number | null;
  finalMs: number | null;
  readonly streaming: boolean;
  readonly spokeAt: number;
  /** The phrase just ended's Moonshine text (/end `heard`), once. */
  takeHeard(): string;
  /** Local recognition is loading for this phrase (true: the stream opened
   * with `loading`), then it answered, failed or stopped (false). */
  onLoading?: (on: boolean) => void;
} {
  const mic = opts.mic || new MicKeeper(KEEP_STREAM_MS, undefined, runningAudioCtx);
  /** The server said the stream's model is loading, and nothing came back yet. */
  let loading = false;
  const setLoading = (on: boolean) => {
    if (loading === on) return;
    loading = on;
    self.onLoading?.(on);
  };
  /** This dictation holds the microphone (one {@link MicKeeper.hold}). */
  let holding = false;
  let h: RecognizerHandlers | null = null;
  let want = false;
  let finishing = false;
  let token: object | null = null; // one per clip; null = no clip
  let cur: Phrase | null = null;
  let uploads = 0;
  let capTimer: ReturnType<typeof setTimeout> | undefined;
  const aborters = new Set<AbortController>();
  /** This dictation streams (until a failure falls back to clips). */
  let streaming = false;
  /** The last ended phrase's Moonshine text (/end `heard`), for the router
   * ({@link takeHeard}); cleared when the next phrase's speech starts. */
  let lastHeard: { text: string; at: number } | null = null;
  /** Phrases ended whose end has not gone to the handlers yet, oldest
   * first: the next phrase's stream opens at once (never behind their /end
   * and its accurate pass), and its words wait only for theirs to be in
   * ({@link settleBefore}) — so phrases still reach the handlers in order. */
  const ending: Phrase[] = [];
  /** Streams open now, abandoned by stop(). */
  const open = new Set<string>();
  /** Barge-in: audio before this (ms) is the app's own voice dying away. */
  let skipUntil = 0;
  /** start()'s skip, until the dictation's first recording takes it: on a
   * cold AudioContext the line's tail it covers dies away only once the
   * context runs, so the skip is counted again from then. */
  let firstSkipMs = 0;
  /** The start chime: until then (ms) no word starts — its audio is
   * recorded (a word said over it keeps its start, the pre-roll stays), the
   * detector sits it out. Counted again from when a cold context runs. */
  let hushUntil = 0;
  let firstHushMs = 0;
  /** `ctx` runs now: the skip and the chime start() asked for run from now. */
  const skipFromNow = () => {
    if (firstSkipMs) skipUntil = Math.max(skipUntil, Date.now() + firstSkipMs);
    if (firstHushMs) hushUntil = Math.max(hushUntil, Date.now() + firstHushMs);
  };
  /** A wake's lead-in (lib/voiceMic MicKeeper.takeLead: what was said after
   * the word, before the chime), for the dictation's first recording. */
  let wakeLead: Float32Array | null = null;
  /** The app says a line: its audio is not the person's — the recorder
   * drops it ({@link Recognizer.mute}) while an analyser on the microphone
   * listens for a voice over it ({@link BargeDetector} → onBarge). */
  let muted = false;
  const barge = new BargeDetector();
  let bargeTimer: ReturnType<typeof setInterval> | undefined;
  let bargeTap: { src: MediaStreamAudioSourceNode; an: AnalyserNode; buf: Float32Array<ArrayBuffer>; stream: MediaStream } | null = null;
  let lastBargeAt = 0;
  /** This mute's first barge-in (ms; 0: none) and the last loud read after
   * it: a voice over a line that plays on (a confirmation) is rewound from
   * its start when the mute lifts ({@link CAPTURE_MUTE_HOLD_S} at most). */
  let bargeFrom = 0;
  let loudOver = 0;
  /** When the microphone last went away mid-listen (ms). */
  let lostAt = 0;
  /** The microphone and the context the phrase records with. */
  let micStream: MediaStream | null = null;
  let micCtx: AudioContext | null = null;
  /** Muted: read the microphone's level every {@link BARGE_POLL_MS}. */
  const listenOver = () => {
    if (bargeTimer || !micStream || !micCtx) return;
    try {
      if (bargeTap?.stream !== micStream) {
        bargeTap?.src.disconnect();
        const src = micCtx.createMediaStreamSource(micStream);
        const an = micCtx.createAnalyser();
        an.fftSize = 2048;
        src.connect(an);
        bargeTap = { src, an, buf: new Float32Array(an.fftSize), stream: micStream };
      }
    } catch {
      bargeTap = null;
      return;
    }
    barge.begin();
    bargeTimer = setInterval(() => {
      const t = bargeTap;
      if (!t || !muted || !h) return;
      t.an.getFloatTimeDomainData(t.buf);
      const hit = barge.block(rmsOf(t.buf));
      if (bargeFrom && barge.loud) loudOver = Date.now();
      if (!hit) return;
      lastBargeAt = Date.now();
      if (!bargeFrom) bargeFrom = loudOver = lastBargeAt;
      h.onBarge?.();
    }, BARGE_POLL_MS);
  };
  const unlistenOver = () => {
    clearInterval(bargeTimer);
    bargeTimer = undefined;
  };

  const unhold = () => {
    if (!holding) return;
    holding = false;
    mic.done();
  };
  const errCode = (err: unknown) => {
    const n = (err as { name?: string })?.name || "";
    if (n === "NotAllowedError" || n === "SecurityError" || n === "PermissionDeniedError") return "not-allowed";
    if (n === "NotFoundError" || n === "DevicesNotFoundError" || n === "OverconstrainedError") return "audio-capture";
    return "start-failed";
  };
  const why = (err: unknown) =>
    (err as Error)?.name === "AbortError" ? "the server took too long" : String((err as Error)?.message || err);
  /** The recognizer is done when nothing is wanted, recording or uploading. */
  const maybeEnd = () => {
    if (!want && !token && uploads === 0 && h) {
      finishing = false;
      unhold();
      const hh = h;
      hh.onEnd();
    }
  };
  /** A clip that could not start: report it, and end. */
  const failed = (code: string, detail?: string) => {
    token = null;
    want = false;
    h?.onError(code, detail);
    maybeEnd();
  };

  const upload = async (clip: Blob | Promise<Blob>, spokeAt?: number) => {
    const hh = h;
    uploads++;
    const ac = new AbortController();
    aborters.add(ac);
    const timer = setTimeout(() => ac.abort(), UPLOAD_TIMEOUT_MS);
    try {
      const blob = await clip;
      const r = await apiRaw("/api/voice/transcribe?lang=" + encodeURIComponent(langTag()), {
        method: "POST",
        headers: { "Content-Type": "audio/wav" },
        body: blob,
        signal: ac.signal,
      });
      const j = (await r.json().catch(() => ({}))) as { text?: string; error?: string };
      if (h !== hh || !hh) return;
      if (r.ok) {
        self.lastError = "";
        const text = String(j.text || "").trim();
        if (text) hh.onFinal(text, spokeAt || undefined);
      } else {
        self.lastError = String(j.error || "HTTP " + r.status);
        want = false;
        hh.onError(
          "server",
          r.status === 409
            ? "Server recognition isn't ready: Settings → Voice"
            : "Couldn't transcribe: " + self.lastError
        );
      }
    } catch (err) {
      if (h !== hh || !hh) return;
      self.lastError = why(err);
      want = false;
      hh.onError("server", "Couldn't reach the server: " + self.lastError);
    } finally {
      clearTimeout(timer);
      aborters.delete(ac);
      uploads--;
    }
    if (h === hh) maybeEnd();
  };

  // --- streaming ------------------------------------------------------------ //

  /** One stream request (JSON back, whatever the status); throws on the
   * network, an abort (stop()) or the timeout. */
  const call = async (
    path: string,
    init: RequestInit
  ): Promise<{ ok: boolean; status: number; j: Record<string, unknown> }> => {
    const ac = new AbortController();
    aborters.add(ac);
    const timer = setTimeout(() => ac.abort(), UPLOAD_TIMEOUT_MS);
    try {
      const r = await apiRaw(path, { ...init, signal: ac.signal });
      const j = (await r.json().catch(() => ({}))) as Record<string, unknown>;
      return { ok: r.ok, status: r.status, j };
    } finally {
      clearTimeout(timer);
      aborters.delete(ac);
    }
  };
  const streamPath = (sid: string, op: string) => "/api/voice/stream/" + encodeURIComponent(sid) + op;
  const abandon = (sid: string) => {
    open.delete(sid);
    void apiRaw(streamPath(sid, ""), { method: "DELETE" }).catch(() => {});
  };

  /** Streaming is over for this dictation: close the phrase's stream, type
   * what it held back (its words so far are its flush), and go on with it —
   * and with a next phrase already recording — as a clip. `line`: said once,
   * in the caption ("" for a 409: the server just can't stream now). */
  const fallBack = (c: Phrase, line: string) => {
    if (!c.stream) return;
    setLoading(false);
    streaming = false;
    for (const p of new Set([c, cur])) {
      if (!p?.stream) continue;
      p.stream = false;
      const sid = p.sid;
      p.sid = null;
      if (sid) {
        abandon(sid);
        h?.onPartial?.("");
        h?.onFlush?.("");
      }
      if (cur === p) {
        clearTimeout(capTimer);
        capTimer = setTimeout(() => {
          if (cur === p) endClip();
        }, CLIP_MAX_MS);
      }
    }
    if (line) h?.onNotice?.(line);
  };
  const streamFailed = (c: Phrase, err: unknown) => {
    self.lastError = why(err);
    fallBack(c, "Streaming stopped (" + self.lastError + ") — one clip per phrase from here");
  };

  /** `c`'s one request at a time; when it is back, the next chunk may go. */
  const run = (c: Phrase, req: () => Promise<void>) => {
    c.busy = req().finally(() => {
      c.busy = null;
      pump(c);
    });
  };

  /** As the phrase starts: open its stream — at once, alongside the
   * previous phrase's /end (the server keeps two per page,
   * speech_stream.CLIENT_STREAMS; the one ending is no longer counted). */
  const openStream = (c: Phrase) => {
    const hh = h;
    run(c, async () => {
      if (h !== hh || !c.stream) return;
      try {
        // The words already in the message, as of now: the phrase before
        // this one counts as far as it is heard (its final may still come).
        let ctx: { context?: string; session?: string } | null = null;
        try {
          ctx = hh?.context?.() || null;
        } catch {
          ctx = null;
        }
        const ask = () =>
          call("/api/voice/stream", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(streamOpenBody(langTag(), VOICE_CLIENT_ID, ctx)),
          });
        let r = await ask();
        // A 409 for the stream cap: passing — asked again (the phone's
        // 300 ms × 1.5 s) before this phrase goes as a clip.
        for (let n = 0; r.status === 409 && streamFull(r.j.error) && n < STREAM_OPEN_RETRIES; n++) {
          await new Promise((res) => setTimeout(res, STREAM_OPEN_RETRY_MS));
          if (h !== hh || !c.stream) return;
          r = await ask();
        }
        const id = String(r.j.id || "");
        if (h !== hh || !c.stream) {
          if (r.ok && id) void apiRaw(streamPath(id, ""), { method: "DELETE" }).catch(() => {});
          return;
        }
        if (r.ok && id) {
          c.sid = id;
          c.opened = Date.now();
          open.add(id);
          voiceMark("stream");
          // Nobody was listening: the model loads now, the first words wait.
          if (r.j.loading === true) setLoading(true);
        } else if (r.status === 409 && streamFull(r.j.error)) {
          // Still full: this phrase is a clip; the next one streams again.
          self.lastError = String(r.j.error);
          c.stream = false;
          if (cur === c) {
            clearTimeout(capTimer);
            capTimer = setTimeout(() => {
              if (cur === c) endClip();
            }, CLIP_MAX_MS);
          }
        } else if (r.status === 409) {
          self.lastError = String(r.j.error || "the server can't stream");
          fallBack(c, "");
        } else throw new Error(String(r.j.error || "HTTP " + r.status));
      } catch (err) {
        if (h === hh) streamFailed(c, err);
      }
    });
  };

  const sendChunk = async (c: Phrase, blob: Blob, hh: RecognizerHandlers | null, takenAt = Date.now()) => {
    try {
      // With its pre-roll in front (the first chunk only; none: no wait).
      const pre = joined(c, blob);
      if (pre !== blob) blob = await pre;
      const r = await call(streamPath(c.sid!, "/chunk"), {
        method: "POST",
        headers: { "Content-Type": "audio/wav" },
        body: blob,
      });
      if (h !== hh || !hh || !c.stream) return;
      if (!r.ok) throw new Error(String(r.j.error || "HTTP " + r.status));
      self.lastError = "";
      setLoading(false); // a pass came back: the model is up
      const ms = Number(r.j.ms);
      if (ms > 0) {
        self.passMs = ms;
        voicePass(ms);
      }
      // The server's end-of-turn verdict on the words so far (absent: the
      // fixed quiet ends the phrase, as before).
      c.turn = parseTurn(r.j.turn, takenAt);
      const w = wordsOf(r.j.committed);
      const p = wordsOf(r.j.partial);
      c.said = joinWords(c.said, w);
      c.partial = p;
      c.passAt = takenAt;
      // The phrases before this one go to the handlers first.
      await settleBefore(c, hh);
      if (h !== hh || !c.stream) return;
      if (hh.onStream) hh.onStream(w, p);
      else {
        if (w) hh.onWords?.(w);
        hh.onPartial?.(p);
      }
    } catch (err) {
      if (h === hh) streamFailed(c, err);
    }
  };

  /** The next chunk, when speech was heard, nothing is in flight and enough
   * is new: the first {@link FIRST_CHUNK_S} of speech, then {@link chunkMinS}. */
  const pump = (c: Phrase) => {
    if (!c.stream || !c.sid || !c.heard || c.busy || c.ending || !c.rec || h === null) return;
    // (A lead-in taken from the ring at the first word holds that block:
    // speechS before trimS, the first chunk goes that much sooner.)
    const min = c.sent ? chunkMinS(self.passMs) : Math.max(0, c.speechS - c.trimS + FIRST_CHUNK_S);
    const takenAt = Date.now();
    const blob = c.rec.take(min);
    if (!blob) return;
    c.sent = true;
    c.fresh = false;
    const hh = h;
    run(c, () => sendChunk(c, blob, hh, takenAt));
  };

  /** The phrases ended before `c` whose end has not reached the handlers:
   * each, once its words are all in, ends now with its fast words (its
   * committed words and last unstable tail) — the person talks again, and
   * their new words never wait on an earlier phrase's accurate pass; that
   * final, when it comes, is only measured. */
  const settleBefore = async (c: Phrase, hh: RecognizerHandlers) => {
    for (const p of [...ending]) {
      if (p === c) return;
      await p.heardAll;
      if (h !== hh) return;
      if (p.flushed) continue;
      p.flushed = true;
      const i = ending.indexOf(p);
      if (i >= 0) ending.splice(i, 1);
      if (!p.stream) continue; // fell back: its words went with the fall-back
      hh.onPartial?.("");
      hh.onFlush?.(p.partial, undefined, p.spokeAt || undefined);
    }
  };

  /** A streamed phrase is over (a pause, or finish()): the next starts
   * recording at once; this one's last chunk (when it holds speech not sent
   * yet — at a pause it is usually just the quiet) and its /end follow the
   * request in flight, and the flushed words go to onFlush. */
  const endStream = async (c: Phrase) => {
    const hh = h;
    const mine = token;
    c.ending = true;
    uploads++;
    let heard = () => {};
    c.heardAll = new Promise<void>((r) => (heard = r));
    ending.push(c);
    const unlist = () => {
      const i = ending.indexOf(c);
      if (i >= 0) ending.splice(i, 1);
    };
    try {
      const tail = await c.rec!.stop().catch(() => null);
      if (token !== mine) return; // stop(): dropped
      token = null;
      if (want && !finishing) void clip();
      if (c.busy) await c.busy;
      if (h !== hh || !hh) return;
      if (!c.stream) {
        // Fell back before the stream took anything: the phrase is a clip.
        if (c.heard && tail && tail.size > 44) void upload(joined(c, tail), c.spokeAt);
        return;
      }
      if (!c.sid) return; // its open failed (it fell back), or it was dropped
      // A blip (a click, a knock, a key): never two loud blocks in a row,
      // and the server, answered, heard no word in it — noise, not a phrase
      // (never an empty one typed, nor its /end's pass).
      if (c.heard && c.run < ONSET_RUN && c.passAt && !c.said && !c.partial) c.heard = false;
      if (!c.heard) {
        // Nothing said: the stream opened for it goes unused.
        discard(c);
        return;
      }
      if (tail && c.fresh && (tail.size > TAIL_MIN_BYTES || (c.pre && tail.size > 44))) await sendChunk(c, tail, hh);
      heard(); // its words are all in: a next phrase's may go once it is flushed
      if (h !== hh || !c.stream || !c.sid) return;
      const sid = c.sid;
      c.sid = null;
      open.delete(sid);
      try {
        const r = await call(streamPath(sid, "/end"), { method: "POST" });
        setLoading(false);
        if (h !== hh) return;
        if (!r.ok) throw new Error(String(r.j.error || "HTTP " + r.status));
        // The accurate pass over the whole phrase: only when it heard it
        // otherwise (`same`: nothing to correct; absent — off, skipped,
        // failed or timed out — the fast words stand).
        const ran = typeof r.j.final === "string";
        const timed = timedFinal(r.j);
        const fms = Number(r.j.final_ms);
        if (timed && fms > 0) {
          self.finalMs = fms;
          voiceMark("final");
        }
        const committed = wordsOf(r.j.committed);
        lastHeard = typeof r.j.heard === "string" && r.j.heard.trim() ? { text: r.j.heard.trim(), at: Date.now() } : null;
        if (timed) {
          // Measured only when an accurate final came (a timeout, or
          // Moonshine's own text, says nothing of how long one takes), late
          // or not; and whether it changed a word.
          if (c.byWord && c.endAt) finalClock.note(Date.now() - c.endAt);
          const fast = joinWords(c.said, committed);
          finalClock.verdict(r.j.same !== true && plainWords(wordsOf(r.j.final)) !== plainWords(fast));
        }
        if (c.flushed) return; // the next phrase's words came first: it ended with its fast words
        await settleBefore(c, hh);
        if (h !== hh || c.flushed) return;
        c.flushed = true;
        unlist();
        c.stream = false;
        const final = ran && r.j.same !== true ? wordsOf(r.j.final) : "";
        hh.onPartial?.("");
        hh.onFlush?.(committed, final || undefined, c.spokeAt || undefined);
      } catch (err) {
        if (h !== hh || c.flushed) return;
        // Its held words still go in (fallBack flushes a phrase with a stream).
        c.sid = sid;
        c.flushed = true;
        unlist();
        streamFailed(c, err);
      }
    } finally {
      heard();
      unlist();
      uploads--;
      if (h === hh) maybeEnd();
    }
  };

  // --- the phrase ------------------------------------------------------------ //

  /** A phrase that won't stream (it never recorded, or heard nothing): its
   * stream — open, or still opening (the open DELETEs it then) — goes. */
  const discard = (c: Phrase) => {
    c.stream = false;
    const sid = c.sid;
    c.sid = null;
    if (sid) abandon(sid);
  };

  /** `blob`, the phrase's first audio to go, with its pre-roll in front
   * (once; anything else as it is). */
  const joined = (c: Phrase, blob: Blob): Promise<Blob> | Blob => {
    const pre = c.pre;
    c.pre = null;
    return pre ? withPreRoll(pre, blob) : blob;
  };

  /** End the current phrase and send it. */
  const endClip = () => {
    const c = cur;
    cur = null;
    clearTimeout(capTimer);
    if (!c?.rec) return;
    c.endAt = Date.now();
    // What the microphone heard so far goes with this phrase: the next
    // one's pre-roll starts after it (never its tail twice).
    mic.cut();
    if (c.stream) {
      void endStream(c);
      return;
    }
    const mine = token;
    c.rec.stop().then(
      (blob) => {
        if (token !== mine) return; // stop(): the clip is discarded
        token = null;
        // A clip the detector heard nothing in has no words to send.
        if (c.heard && blob.size) void upload(joined(c, blob), c.spokeAt);
        if (want && !finishing) void clip();
        else maybeEnd();
      },
      () => {
        if (token !== mine) return;
        token = null;
        maybeEnd();
      }
    );
  };

  const clip = async () => {
    const mine = {};
    token = mine;
    const c: Phrase = {
      rec: null,
      heard: false,
      stream: streaming,
      sid: null,
      opened: 0,
      busy: null,
      fresh: false,
      ending: false,
      sent: false,
      trimS: 0,
      speechS: 0,
      spokeAt: 0,
      clock: new RecordingClock(),
      loudAt: 0,
      turn: null,
      pre: null,
      run: 0,
      said: "",
      partial: "",
      passAt: 0,
      byWord: false,
      endAt: 0,
      flushed: false,
      heardAll: null,
    };
    // Its stream opens while the microphone and the audio engine get ready.
    if (c.stream) openStream(c);
    let s: MediaStream;
    try {
      s = await mic.get();
    } catch (err) {
      discard(c);
      if (token === mine) failed(errCode(err));
      return;
    }
    if (token !== mine) return discard(c);
    // The context the mic's click made and resumed (voiceAudioCtx): the
    // resume may still be in flight.
    const ctx = voiceAudioCtx();
    if (ctx && ctx.state === "suspended") {
      await ctx.resume().catch(() => {});
      // The start chime waited on this same resume (SpeechQueue.cue): it
      // plays now, so the skip it is owed counts from now.
      skipFromNow();
    }
    if (token !== mine) return discard(c);
    micStream = s;
    micCtx = ctx;
    if (!want) {
      // finish() came between two phrases: nothing more to record.
      discard(c);
      token = null;
      maybeEnd();
      return;
    }
    if (!ctx || ctx.state !== "running") {
      discard(c);
      failed("start-failed", "The audio engine isn't running — click the mic again");
      return;
    }
    // The capture graph on the audio thread (lib/voiceMic: an AudioWorklet
    // resampling to 16 kHz): the recording, the pre-roll and the meter read
    // its blocks. Null where there is none: the recorder's own ScriptProcessor.
    // The pre-roll runs on this stream from here: what it holds now is the
    // half second before this recording.
    let cap = mic.capture(ctx);
    if (cap instanceof Promise) {
      cap = await cap;
      if (token !== mine) return discard(c);
      if (!want) {
        discard(c);
        token = null;
        maybeEnd();
        return;
      }
    }
    const skip = Math.max(0, skipUntil - Date.now());
    firstSkipMs = 0;
    firstHushMs = 0;
    // Not after a barge-in (that audio is the app's voice dying away), nor
    // while muted. Under the start chime it is kept: no word starts there,
    // but one said over it keeps its start.
    const pre = skip || muted ? null : mic.preRoll();
    // The silence detector: the recorder's RMS per block against a noise
    // floor that follows the quiet moments — from the room's quiet as the
    // pre-roll heard it, when there is one ({@link PhraseLevel}).
    const level = new PhraseLevel(mic.quietFloor());
    // A wake's lead-in (the first recording only): a command said after the
    // word while the server checked it — the phrase has begun already.
    const lead = wakeLead;
    wakeLead = null;
    const said = lead && !skip && !muted ? leadSpeech(lead, PRE_ROLL_RATE, level.floor) : null;
    let blockAt = 0; // the recording's time before this block
    const onLevel = (rms: number) => {
      if (cur !== c) return;
      const at = blockAt;
      blockAt = c.rec?.seconds() ?? 0;
      h?.onLevel?.(rms);
      const now = Date.now();
      c.clock.block(blockAt, now);
      // The start chime: no word starts under it.
      const hush = !c.heard && hushUntil > 0 && c.clock.at(at) < hushUntil;
      const loud = level.feed(rms, (blockAt - at) * 1000, { hush, speech: c.heard });
      c.run = Math.max(c.run, level.best);
      if (loud) {
        if (!c.heard) {
          lastHeard = null; // a new phrase: the last one's `heard` is not its
          c.speechS = at;
          // When it began: this block's start on the recording's own clock
          // (a late callback doesn't move it) — before the recording when
          // its first moments were loud (the pre-roll holds the start: the
          // earliest it can have begun).
          c.spokeAt = c.clock.at(at);
          // Speech from its first moments: it began before the recording.
          if (at < PRE_ROLL_ONSET_S && !c.trimS) {
            c.pre = pre;
            if (pre) c.spokeAt -= PRE_ROLL_MS;
          } else if (c.stream && cap) {
            // Later: its lead-in is the LEAD_IN_MS before this block and the
            // block, from the capture's ring (the recording's own blocks,
            // in step) — never the 0–255 ms the trims below happen to leave.
            const blockS = blockAt - at;
            const ring = mic.preRoll(LEAD_IN_MS + blockS * 1000);
            if (ring && ring.length >= Math.floor(blockS * PRE_ROLL_RATE)) {
              c.rec?.take(0);
              c.pre = ring;
              c.trimS = blockAt;
            }
          }
          h?.onSpeech?.();
          // Opened so long ago the server may have forgotten it: a new one.
          if (c.stream && c.sid && now - c.opened > STREAM_FRESH_MS) {
            discard(c);
            c.stream = true;
            openStream(c);
          }
        }
        c.heard = true;
        c.fresh = true;
        c.loudAt = now;
      } else if (!c.heard && c.stream) {
        // Before the first word, keep only a lead-in (under 300 ms): the
        // stream is not sent the quiet before it.
        if (c.rec?.take()) c.trimS = blockAt;
      }
      // The quiet heard that ends it: the server's verdict on the words so
      // far while it streams (fallen back to a clip: the fixed quiet).
      const heardSoFar = c.stream
        ? { text: joinWords(c.said, c.partial), settled: !c.partial && c.passAt > c.loudAt }
        : null;
      if (c.heard && now - c.loudAt > turnHoldMs(c.stream ? c.turn : null, c.loudAt, h?.blank?.() ?? true, heardSoFar)) endClip();
      else pump(c);
    };
    try {
      const ro = { targetRate: CLIP_RATE, onLevel, skipS: skip / 1000 };
      c.rec = cap ? cap.record(ro) : startWavRecording(s, ctx, ro);
    } catch {
      discard(c);
      failed("start-failed");
      return;
    }
    if (muted) c.rec.mute?.(true);
    cur = c;
    if (said && lead) {
      // The wake's command, said before the recording: it goes in front of
      // the first chunk, and the phrase ends on the quiet after it.
      const t0 = Date.now() - (lead.length / PRE_ROLL_RATE) * 1000;
      lastHeard = null;
      c.pre = lead;
      c.heard = true;
      c.fresh = true;
      c.run = ONSET_RUN;
      c.spokeAt = Math.round(t0 + said.startS * 1000);
      c.loudAt = Math.round(t0 + said.endS * 1000);
    }
    voiceMark("mic");
    h?.onReady?.();
    if (said && lead && cur === c) h?.onSpeech?.();
    if (!c.stream)
      capTimer = setTimeout(() => {
        if (cur === c) endClip();
      }, CLIP_MAX_MS);
  };

  // The microphone went away (unplugged, taken by the OS) mid-listen: the
  // phrase it was recording ends (its words go), and the next one opens the
  // microphone again — the default one now. Gone again within
  // MIC_LOST_RETRY_MS: listening stops and says so (never a "Listening…"
  // over a dead microphone).
  mic.onLost(() => {
    if (!want || !h) return;
    const now = Date.now();
    const again = now - lostAt < MIC_LOST_RETRY_MS;
    lostAt = now;
    if (again) {
      if (cur) endClip();
      want = false;
      self.lastError = "the microphone went away";
      h.onError("mic-lost", MIC_GONE_LINE);
      maybeEnd();
      return;
    }
    h.onNotice?.(MIC_LOST_LINE);
    if (cur) endClip();
    else if (!token) void clip();
  });

  const self = {
    name: "server" as const,
    lastError: "",
    onLoading: undefined as ((on: boolean) => void) | undefined,
    /** The server's last streaming pass, in ms (null before one). */
    passMs: null as number | null,
    /** The last phrase's accurate re-transcription (/end `final_ms`), in ms. */
    finalMs: null as number | null,
    mic,
    available: () => typeof navigator !== "undefined" && !!navigator.mediaDevices?.getUserMedia && !!audioCtxCtor(),
    /** The dictation streams (its words are typed as they come). */
    get streaming(): boolean {
      return streaming;
    },
    /** The phrase just ended's Moonshine text (/end `heard`), once, while
     * fresh (a few seconds; never the phrase before it): POST
     * /api/voice/route sends it as `heard`. */
    takeHeard(): string {
      const h = lastHeard;
      lastHeard = null;
      return h && Date.now() - h.at < HEARD_FRESH_MS ? h.text : "";
    },
    /** When the phrase being recorded now began (ms; 0: no speech yet). */
    get spokeAt(): number {
      return cur?.heard ? cur.spokeAt : 0;
    },
    mute(on: boolean) {
      const was = muted;
      muted = on;
      if (on) listenOver();
      else unlistenOver();
      if (on && !was) bargeFrom = loudOver = 0;
      const now = Date.now();
      // A voice was heard over this line: it was cut for it (a barge-in), or
      // it played on (a confirmation, "Sent."). Either way the tail is the
      // person's, kept — and so is everything they said since their voice
      // began, heard while the detector counted and the line went on
      // (lib/voiceMic CAPTURE_MUTE_HOLD_S at most): a command said over
      // "Sent." is not lost.
      const over = !on && was && bargeFrom > 0;
      const barged = over || (!on && now - lastBargeAt < BARGE_FRESH_MS);
      const rewindMs = over ? Math.min(CAPTURE_MUTE_HOLD_S * 1000, now - bargeFrom + BARGE_REWIND_MS) : BARGE_REWIND_MS;
      cur?.rec?.mute?.(on, barged ? 0 : BARGE_SKIP_MS / 1000, barged ? rewindMs / 1000 : 0);
      // Muted, its blocks aren't counted: the count no longer follows time.
      cur?.clock.reset();
      const c = cur;
      if (over && c?.rec) {
        // What was rewound is speech: the phrase has begun (it may be over
        // by now — the quiet after it ends it, as any phrase).
        if (!c.heard) {
          lastHeard = null;
          c.heard = true;
          c.speechS = c.trimS;
          c.spokeAt = Math.max(0, bargeFrom - BARGE_REWIND_MS);
          h?.onSpeech?.();
        }
        c.fresh = true;
        c.run = Math.max(c.run, ONSET_RUN);
        c.loudAt = Math.max(c.loudAt, loudOver);
      }
      if (!on) bargeFrom = loudOver = 0;
    },
    endPhrase() {
      if (cur?.heard && want) {
        cur.byWord = true;
        endClip();
      }
    },
    start(handlers: RecognizerHandlers, o: { skipMs?: number; chimeMs?: number } = {}) {
      self.stop();
      if (!self.available()) return false;
      // A wake's lead-in: taken now, before the start chime plays.
      wakeLead = mic.takeLead();
      skipUntil = Date.now() + Math.max(0, o.skipMs || 0);
      firstSkipMs = Math.max(0, o.skipMs || 0);
      firstHushMs = Math.max(0, o.chimeMs || 0);
      hushUntil = firstHushMs ? Date.now() + firstHushMs : 0;
      // A cold context (resuming since the press): the chime waits for it,
      // and so does the skip — even when the microphone opens later still.
      const cold = audioCtx && audioCtx.state !== "running" ? audioCtx : null;
      if (cold && (firstSkipMs || firstHushMs)) {
        const mine = handlers;
        void whenRunning(cold).then((ok) => {
          if (ok && h === mine) skipFromNow();
        });
      }
      muted = false;
      h = handlers;
      want = true;
      finishing = false;
      holding = true;
      mic.hold();
      streaming = !!opts.stream?.();
      ending.length = 0;
      // Voice is used here: a later load or focus warms recognition (warmStt).
      noteVoiceUsed();
      void clip();
      return true;
    },
    finish() {
      want = false;
      finishing = true;
      if (cur) {
        h?.onStatus?.("Transcribing…");
        endClip();
      } else maybeEnd();
    },
    stop() {
      want = false;
      finishing = false;
      h = null;
      token = null;
      setLoading(false);
      clearTimeout(capTimer);
      unlistenOver();
      try {
        bargeTap?.src.disconnect();
      } catch {
        /* already gone */
      }
      bargeTap = null;
      const c = cur;
      cur = null;
      c?.rec?.cancel();
      aborters.forEach((a) => a.abort());
      aborters.clear();
      open.forEach(abandon);
      ending.length = 0;
      unhold();
    },
    release() {
      self.stop();
      mic.release();
    },
  };
  return self;
}

// --- GET /api/voice -------------------------------------------------------------- //

/** GET /api/voice's `system`: the operating system's own speech on the
 * server's machine (Windows System.Speech — from WSL too — or the macOS
 * Speech framework). */
export interface VoiceSystem {
  available?: boolean;
  platform?: string;
  detail?: string;
  voices?: unknown[];
  recognizers?: unknown[];
}

export interface VoiceCfg {
  /** The phone page's preference; the desktop app goes by `server_ready`. */
  stt: "phone" | "server";
  tts: "phone" | "server";
  /** Settings → Voice "Approve dialogs by voice": unknown reads as off. */
  approve_dialogs?: boolean;
  /** The server asks every prompt for a Say line ("Everything" on some
   * device); absent from an older server. */
  say_all?: boolean;
  server_ready: {
    stt?: boolean;
    tts?: boolean;
    detail?: string;
    stt_backend?: string;
    stt_detail?: string;
    /** The ready recognizer streams (POST /api/voice/stream): words as you speak. */
    stt_stream?: boolean;
    /** Local recognition is in memory now (absent: an older server). */
    stt_loaded?: boolean;
    tts_backend?: string;
    tts_detail?: string;
  };
  system: VoiceSystem;
}
export const DEFAULT_VOICE_CFG: VoiceCfg = { stt: "phone", tts: "phone", server_ready: {}, system: {} };

/** localStorage: when this device last listened with the server's
 * recognizer (ms). */
export const VOICE_USED_KEY = "cs_voice_used_at";
/** Voice used here within this long: reading the config (a load, a focus)
 * warms the server's recognition when it is not in memory ({@link
 * sttWarmWanted}) — it unloads after idle minutes, and reloading costs the
 * first words of the next press 1–7 s. Not used lately: nothing is loaded
 * for nobody (local whisper is ~1 GB). */
export const STT_WARM_RECENT_MS = 3 * 24 * 3600_000;
/** At most one warm-up this often. */
export const STT_WARM_EVERY_MS = 60_000;

/** Note that this device listened with the server's recognizer now. */
export function noteVoiceUsed(now = Date.now()): void {
  try {
    localStorage.setItem(VOICE_USED_KEY, String(now));
  } catch {
    /* storage off: no warm-ups */
  }
}

function voiceUsedAt(): number {
  try {
    return Number(localStorage.getItem(VOICE_USED_KEY)) || 0;
  } catch {
    return 0;
  }
}

/** Whether to warm the server's recognition now: speech runs there, it
 * streams, it says it is NOT in memory (an older server says nothing: never),
 * this device used it within {@link STT_WARM_RECENT_MS}, and the last
 * warm-up was over {@link STT_WARM_EVERY_MS} ago. Pure. */
export function sttWarmWanted(cfg: VoiceCfg, usedAt: number, lastWarm: number, now = Date.now()): boolean {
  const r = cfg.server_ready || {};
  return (
    cfg.stt === "server" &&
    r.stt_stream === true &&
    r.stt_loaded === false &&
    usedAt > 0 &&
    now - usedAt < STT_WARM_RECENT_MS &&
    now - lastWarm >= STT_WARM_EVERY_MS
  );
}

let sttWarmedAt = 0;
/** Warm the server's recognition ahead of the press ({@link sttWarmWanted}):
 * a stream opened and dropped at once — opening one is what loads it
 * (POST /api/voice/stream, `loading`), in the background on the server.
 * True when it asked. Never throws. */
export function warmStt(cfg: VoiceCfg, now = Date.now()): boolean {
  if (!sttWarmWanted(cfg, voiceUsedAt(), sttWarmedAt, now)) return false;
  sttWarmedAt = now;
  void (async () => {
    try {
      const r = await apiRaw("/api/voice/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lang: String(navigator.language || "en").split(";")[0], client: VOICE_CLIENT_ID }),
      });
      const j = (await r.json().catch(() => ({}))) as { id?: unknown };
      const id = String(j.id || "");
      if (r.ok && id) await apiRaw("/api/voice/stream/" + encodeURIComponent(id), { method: "DELETE" });
    } catch {
      /* the press loads it then, as before */
    }
  })();
  return true;
}

/** Where speech runs (Settings → Voice), and the voice words (lib/voiceWords
 * takes them). Unreachable / older server: `prev`
 * (a blink must not swap engines), else the browser's own. `warm` false
 * (`?warm=0`): read it WITHOUT readying the server's voice — a local voice
 * host (Voice Lab) is gigabytes and only stays up while it is asked. */
export async function loadVoiceCfg(prev: VoiceCfg | null, warm = true): Promise<VoiceCfg> {
  try {
    const j = await api<Partial<VoiceCfg>>(warm ? "/api/voice" : "/api/voice?warm=0");
    // The voice words ride along: the matchers and hints use them from now;
    // so do the person's pronunciations (the browser's own voice shapes with them).
    applyVoiceView(j);
    setPronounce((j as { pronounce?: unknown } | null)?.pronounce);
    if (j && typeof j === "object") {
      const cfg: VoiceCfg = {
        stt: j.stt === "server" ? "server" : "phone",
        tts: j.tts === "server" ? "server" : "phone",
        approve_dialogs: j.approve_dialogs === true,
        ...(typeof j.say_all === "boolean" ? { say_all: j.say_all } : {}),
        server_ready: j.server_ready && typeof j.server_ready === "object" ? j.server_ready : {},
        system: j.system && typeof j.system === "object" ? j.system : {},
      };
      // The voice UI is up (a load, a focus): recognition warms while the
      // person reaches for the mic, when they use it here.
      warmStt(cfg);
      return cfg;
    }
  } catch {
    /* below */
  }
  return prev || DEFAULT_VOICE_CFG;
}

/** The desktop app: the preload's `mfapp` bridge, or Electron's user agent.
 * Its Chromium has no Google speech key, so Web Speech recognition fails with
 * `network` on every start there. */
export function isElectron(): boolean {
  if (typeof window !== "undefined" && (window as unknown as { mfapp?: unknown }).mfapp) return true;
  return typeof navigator !== "undefined" && String(navigator.userAgent || "").includes("Electron");
}

export type SttEngine = "server" | "browser" | "none";

/** The recognizer to use (`serverOk`: this page can record for the server).
 * In the desktop app, the server's whenever it is ready — `voice.stt` is the
 * phone page's preference — else none: never Web Speech, which fails there.
 * In a browser tab, the server's when Settings say "server" AND it is ready,
 * else the browser's (the phone page's rule). */
export function sttEngine(cfg: VoiceCfg, serverOk: boolean, electron = isElectron()): SttEngine {
  const ready = !!cfg.server_ready.stt && serverOk;
  if (electron) return ready ? "server" : "none";
  return cfg.stt === "server" && ready ? "server" : "browser";
}

/** The voice: the server's when ready (in the desktop app) or when Settings
 * also say "server" (in a tab), else speechSynthesis, which works in both. */
export function ttsEngine(cfg: VoiceCfg, electron = isElectron()): "server" | "browser" {
  if (!cfg.server_ready.tts) return "browser";
  return electron || cfg.tts === "server" ? "server" : "browser";
}

/** The caption when this page has no recognizer. */
export function noRecognizerLine(electron = isElectron()): string {
  return electron
    ? "Speech needs the server's engine here: Settings → Voice"
    : "Voice input needs Chrome, Edge or Safari — or server recognition (Settings → Voice)";
}

const PLATFORM_WORDS: Record<string, string> = {
  wsl: "Windows via WSL",
  windows: "Windows",
  win32: "Windows",
  macos: "macOS",
  darwin: "macOS",
};

/** "server (piper)", "server, built in (Windows via WSL)", or "server". */
function serverEngineName(backend: string | undefined, system: VoiceSystem): string {
  const id = String(backend || "");
  if (id === "system") {
    const p = String(system.platform || "");
    const where = PLATFORM_WORDS[p.toLowerCase()] || p;
    return "server, built in" + (where ? " (" + where + ")" : "");
  }
  return id ? "server (" + id + ")" : "server";
}

/** How the server's recognizer is fed: streamed (the recorder's take() uses
 * the windowed sinc) or a clip per phrase, the rate, the resampler, and the
 * quiet that ends a phrase. */
function clipsLine(stream: boolean): string {
  const how = stream
    ? "streaming (words as you speak): 16 kHz, windowed-sinc resampler"
    : "one clip per phrase: 16 kHz, " + resamplerName();
  return " — " + how + ", a phrase ends after " + SILENCE_MS / 1000 + " s of quiet";
}

/** The options' Diagnostics line: the engines actually in use (`stt` /
 * `tts` as the page resolved them, "none" when it has none), and why the
 * server's recognizer isn't, when this page would use it. */
export function engineDiagnostics(
  cfg: VoiceCfg,
  stt: SttEngine,
  tts: "server" | "browser" | "none",
  electron = isElectron()
): string {
  const ready = cfg.server_ready;
  let rec =
    stt === "server"
      ? serverEngineName(ready.stt_backend, cfg.system) + clipsLine(!!ready.stt_stream)
      : stt === "browser"
        ? "this browser"
        : electron
          ? "none"
          : "none in this browser";
  if (stt !== "server" && !ready.stt && (electron || cfg.stt === "server"))
    rec += " (server recognition isn't ready" + (ready.stt_detail ? ": " + ready.stt_detail : "") + ")";
  const voice =
    tts === "server" ? serverEngineName(ready.tts_backend, cfg.system) : tts === "browser" ? "this browser" : "none";
  return "Recognition: " + rec + " · Voice: " + voice;
}

/** The diagnostics' pass time: how long the server's last streaming pass
 * took — "whisper ~0.4 s/pass" ("" before one). */
export function passLine(backend: string | undefined, passMs: number | null): string {
  if (passMs == null || !(passMs > 0)) return "";
  const id = String(backend || "");
  return (!id || id === "local" ? "whisper" : id) + " ~" + (passMs / 1000).toFixed(1) + " s/pass";
}

/** The diagnostics' accurate pass: how long the last phrase's re-hearing
 * (/end `final_ms`) took — "final 1.2 s" ("" before one). */
export function finalLine(finalMs: number | null): string {
  return finalMs != null && finalMs > 0 ? "final " + (finalMs / 1000).toFixed(1) + " s" : "";
}

/** The diagnostics' "over" latency: the word's end to Enter for the last
 * send — "over → sent 0.9 s" ("" before one). */
export function sentLine(ms: number | null): string {
  return ms != null && ms >= 0 ? "over → sent " + (ms / 1000).toFixed(1) + " s" : "";
}

/** The diagnostics' typing latency: the median round trip of the last
 * batches of dictation keys through the server's gate ({@link KeyGate}) —
 * "typing ~18 ms/batch" ("" before one). */
export function keysLine(times: readonly number[] = keyTimes): string {
  if (!times.length) return "";
  const sorted = [...times].sort((a, b) => a - b);
  return "typing ~" + sorted[Math.floor(sorted.length / 2)] + " ms/batch";
}

/** The caption for a recognizer error, or "" for one that needs no words. */
export function recognizerErrorLine(code: string, detail?: string): string {
  if (code === "network")
    return "Speech recognition failed (network): use Chrome, Edge or Safari, or turn on server recognition in Settings → Voice";
  if (code === "not-allowed" || code === "service-not-allowed") return "Microphone permission denied";
  if (code === "audio-capture") return "No microphone found";
  if (detail) return detail;
  if (code === "unavailable" || code === "start-failed") return "Speech recognition did not start (" + code + ")";
  return "Voice error: " + code;
}
