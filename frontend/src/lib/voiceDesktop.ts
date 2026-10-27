/** The desktop mic (components/TopBarMic.tsx): everything that is not DOM.
 *
 * The words are typed into the focused session AS YOU SPEAK: each recognised
 * phrase goes straight into the session's input field (raw keys down its
 * terminal, like typing), and the radio words end a phrase:
 *  - "over" — send the draft (Enter, through POST /api/instances/{title}/send
 *    {enter: true}, after the same dialog checks as a send) and keep
 *    listening: the next phrase is the next message;
 *  - "over and out" — send what is pending (nothing, when nothing is) and
 *    stop listening;
 *  - "disregard" — take back what this message typed (one backspace per
 *    character) and keep listening.
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
 *    for…" act instead of being typed into the focused session;
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
import type { Dialog } from "../api/types";
import { peekTerm } from "./terminals";
import { toast } from "./toast";
import { resamplerName, startWavRecording, type WavRecording } from "./wavRecorder";
import { MicKeeper, isLoud, noiseFloor, pickupLine, voiceMark, voicePass, voiceSent } from "./voiceMic";

// --- the radio words -------------------------------------------------------- //

export type RadioKind = "over" | "out" | "disregard" | "";

/** "fix the bug over" → {kind: "over", text: "fix the bug"} (send it, go on
 * listening); "… over and out" (or "over, out") → {kind: "out"} (send it,
 * stop) — matched before "over"; "… disregard" (or "disregard that") →
 * {kind: "disregard"}; else {kind: "", text} — "… submit" too: it is a word
 * like any other. Case and punctuation do not matter ("Over." from Safari); only the LAST word counts — an "over" mid-sentence
 * is a word, and "hangover" is not "over". The shape is mobile.js
 * `voiceDictParse`'s, so both surfaces hear alike. */
export function parseRadio(text: string): { kind: RadioKind; text: string } {
  const t = String(text || "").trim();
  const m =
    /^(.*?)[\s,.!?]*\b(?:(over[\s,.!?]+(?:and[\s,.!?]+)?out)|(over|disregard)(?:\s+(?:that|it|all|everything))?)[.!?\s]*$/i.exec(t);
  if (!m) return { kind: "", text: t };
  return { kind: m[2] ? "out" : (m[3].toLowerCase() as RadioKind), text: m[1].trim() };
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
].map((p) => p.split(" ").map((slot) => slot.split("|")));
const ROUTER_LEADS = new Set(["ok", "okay", "hey", "please"]);

/** Could the phrase that starts with `text` be a router command? A streamed
 * first phrase that could waits whole for its end (the router reads whole
 * phrases); one that can't — almost every prompt — is typed as it comes. The
 * router's lead word and trailing "please" are dropped as it drops them. */
export function mayBeCommand(text: string): boolean {
  let w = routerWords(text);
  if (w.length > 1 && ROUTER_LEADS.has(w[0])) w = w.slice(1);
  if (w.length > 1 && w[w.length - 1] === "please") w = w.slice(0, -1);
  if (!w.length) return true;
  return COMMANDS.some((p) => {
    for (let i = 0; i < w.length; i++) {
      if (i >= p.length) return false;
      if (p[i][0] === "*") return true;
      if (!p[i].includes(w[i])) return false;
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
  if (/^(?:switch|go|change|jump)$/.test(w[0] || "")) {
    const at = w[1] === "over" ? 2 : 1;
    return w[at] === "to" && w.length > at + 1;
  }
  if (/^(?:focus|select)$/.test(w[0] || "")) return w.length > 1;
  if (/^(?:next|previous)$/.test(w[0] || "")) return w.length === 2 && /^(?:session|one)$/.test(w[1]);
  return w.length >= 2 && /^(?:session|number|row)$/.test(w[0]) && new RegExp("^(?:" + NUMS + ")$").test(w[1]);
}

const bareWord = (w: string) => w.toLowerCase().replace(/[^\p{L}\p{N}']+/gu, "");

/** "Stop recording" and its kin: said at the end of any committed words they
 * stop listening at once, never typed, and the words before them stay. */
const STOP_PHRASES = [
  "stop recording", "stop listening", "mic off", "microphone off", "turn mic off",
  "turn microphone off", "that's all", "thats all", "that is all",
].map((p) => p.split(" "));
/** "Stop talking" and its kin, as the hold-back keeps them whole. */
const HUSH_PHRASES = [
  "stop talking", "stop speaking", "stop reading", "be quiet", "quiet", "shut up", "hush", "enough",
  "that's enough", "thats enough", "ok stop", "okay stop",
].map((p) => p.split(" "));
/** Every phrase the hold-back keeps whole while it may still be said: the
 * stop family, "stop talking" and its kin, and the radio words with their
 * tails ("disregard that"), "over and out" (so "over" and "over and" wait for
 * the next word too) and "over and out, that's all". */
const CONTROL_PHRASES = [
  ...STOP_PHRASES,
  ...HUSH_PHRASES,
  ...["over", "disregard"].flatMap((w) =>
    [[w], ...["that", "it", "all", "everything"].map((tail) => [w, tail])]
  ),
  ["over", "out"],
  ...["that's", "thats"].map((w) => ["over", "and", "out", w, "all"]),
];
/** The longest control phrase, in words: what the hold-back may keep. */
export const CONTROL_MAX_WORDS = Math.max(...CONTROL_PHRASES.map((p) => p.length));

/** The indexes of `ws`'s real words (a bare "." is not one). */
const wordIndexes = (ws: string[]) => ws.flatMap((w, i) => (bareWord(w) ? [i] : []));

/** A phrase in flight → what may be typed now and what waits: the last words
 * (up to {@link CONTROL_MAX_WORDS}) while they are, or could still become, a
 * control phrase — "over", "disregard that", "stop" (…recording), "that is"
 * (…all) — with a bare punctuation token kept with its word. Any other last
 * word is typed at once; a held one goes in once the next word rules the
 * control phrase out ("stop the server"), or the phrase ends on less than one. */
export function holdBack(text: string): { type: string; held: string } {
  const ws = String(text || "").trim().split(/\s+/).filter(Boolean);
  if (!ws.length) return { type: "", held: "" };
  const idx = wordIndexes(ws);
  let n = ws.length;
  for (let k = 1; k <= Math.min(CONTROL_MAX_WORDS, idx.length); k++) {
    const tail = idx.slice(-k).map((i) => bareWord(ws[i]));
    if (CONTROL_PHRASES.some((p) => p.length >= k && tail.every((w, j) => p[j] === w))) n = idx[idx.length - k];
  }
  return { type: ws.slice(0, n).join(" "), held: ws.slice(n).join(" ") };
}

/** `text` ends with a stop phrase: the words before it (maybe ""), else null.
 * "fix the bug, stop recording." → "fix the bug,". */
export function stopTail(text: string): string | null {
  const ws = String(text || "").trim().split(/\s+/).filter(Boolean);
  const idx = wordIndexes(ws);
  for (const p of STOP_PHRASES) {
    if (idx.length < p.length) continue;
    const tail = idx.slice(-p.length);
    if (tail.every((i, j) => bareWord(ws[i]) === p[j])) return ws.slice(0, tail[0]).join(" ").replace(/[\s,;:]+$/, "");
  }
  return null;
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

const HUSH_RE = new RegExp(
  "^(?:(?:ok|okay|hey|please) )?(?:" + HUSH_PHRASES.map((p) => p.join(" ")).join("|") + ")(?: please| now)?$"
);

/** "Stop talking", "be quiet", "quiet", "shut up", "stop reading",
 * "enough", "okay stop"…: the app's own voice stops — everything said and
 * queued, every session — and listening goes on. Only ever a WHOLE phrase
 * ("don't stop talking to me" is a prompt); never typed. The server's router
 * answers the same words with kind `hush`. */
export function isHushPhrase(text: string): boolean {
  return HUSH_RE.test(routerWords(text).join(" "));
}

/** A whole answer to a dialog or a question: yes / no / always / a number,
 * as the router hears them. Only ever the WHOLE dictation counts — "the
 * answer is no, but…" is a prompt. */
export function isAnswerPhrase(text: string): boolean {
  return ANSWER_RE.test(routerWords(text).join(" "));
}

const joinWords = (a: string, b: string) => [a.trim(), b.trim()].filter(Boolean).join(" ");

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
export const DEFAULT_VOICE_PREFS: VoicePrefs = { ptt: "toggle", replies: "off", wake: false, confirm: true, sounds: true };

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

function normPrefs(v: unknown): VoicePrefs {
  const o = v && typeof v === "object" ? (v as Record<string, unknown>) : {};
  return {
    ptt: o.ptt === "hold" ? "hold" : "toggle",
    replies: repliesMode(o.replies),
    wake: o.wake === true,
    confirm: o.confirm !== false,
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
  return normPrefs(raw);
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
    kv?.setItem(VOICE_PREFS_KEY, JSON.stringify(next));
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

/** What a hotkey phase does, as the button does it: in "Hold to talk", down
 * starts and up (only after a down that the KEY pressed — a blur's stray up
 * never ends a held button) stops; in "Click to talk", each down starts
 * or stops and up is nothing. Stopping leaves the words in the field. */
export function talkKeyAction(
  phase: TalkPhase,
  hold: boolean,
  listening: boolean,
  keyHeld: boolean
): "start" | "finish" | null {
  if (!hold) return phase === "down" ? (listening ? "finish" : "start") : null;
  if (phase === "down") return listening ? null : "start";
  return keyHeld && listening ? "finish" : null;
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
export const DICT_HINT = "Say “over” to send, “over and out” to stop, “disregard” to start over";
/** The caption before a word when the mic listens for `name`'s answer to a
 * question (the ask pop-up's field, a hands-free listen). */
export const answerHint = (name: string) =>
  "Answer " + name + " — say “over” to send, “over and out” to stop, “disregard” to start over";
/** The caption after push to talk let go (or silence) on a session. */
export const LEFT_LINE = "In the field — press Enter to send";
/** The caption after a stop phrase with nothing typed. */
export const STOPPED_LINE = "Stopped listening";
/** The caption after "stop talking" — shown {@link HUSH_CAPTION_MS}. */
export const HUSH_LINE = "Quiet.";
export const HUSH_CAPTION_MS = 1000;

/** The pill beside the top bar's mic — the one thing the bar says about
 * voice, and only ever one of these: {@link HUSH_LINE} for
 * {@link HUSH_CAPTION_MS} after "stop talking", "Listening…" while the mic
 * is open, "Speaking…" while a line plays, else nothing. Never the words
 * heard, typed or said (they are in the terminal; the options show them),
 * and never "Hearing you…" in words — that is the ring's colour. */
export function micPill(s: { listening: boolean; speaking: boolean; quiet: boolean }): string {
  if (s.quiet) return HUSH_LINE;
  if (s.listening) return "Listening…";
  return s.speaking ? "Speaking…" : "";
}

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
export type TailKind = "over" | "out" | "disregard" | "stop" | "hush" | "route";

/** A control phrase at the end of the guess acts once the voice has been
 * quiet this long after it — not the recognizer's {@link SILENCE_MS} and its
 * accurate pass. */
export const CONTROL_TAIL_MS = 250;
/** How often the guess's tail is checked against the quiet. */
export const CONTROL_POLL_MS = 50;
/** "Over" from the guess ends the phrase and waits this long for its /end
 * (the accurate final, retyped first); then the fast words are sent and a
 * later final is ignored. */
export const OVER_FINAL_WAIT_MS = 2500;
/** A phrase taken from its guess: its own late words and end are ignored —
 * for at most this long (a phrase whose end never comes). */
export const SWALLOW_MAX_MS = 5000;

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

  begin(): void {
    this.listening = true;
    this.typed = "";
    this.held = "";
    this.partial = "";
    this.phraseAt = -1;
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
  flush(w: string, final?: string): DictOutcome {
    if (!this.listening) return { kind: "stopped" };
    return this.heard(final?.trim() ? final.trim() : joinWords(this.held, w));
  }

  /** `text` went into the field. */
  wrote(text: string): void {
    this.typed += text;
  }

  /** `text`, the end of what was typed, was taken back. */
  unwrote(text: string): void {
    if (text && this.typed.endsWith(text)) this.typed = this.typed.slice(0, this.typed.length - text.length);
  }

  /** One final phrase: the phrase under way ends as `phrase` (its guess
   * corrected to it, or taken back for a control word). */
  heard(phrase: string): DictOutcome {
    if (!this.listening) return { kind: "stopped" };
    const ctl = this.control(phrase, true);
    if (ctl) return ctl;
    const p = parseRadio(phrase);
    switch (p.kind) {
      case "disregard":
        return { kind: "erase", text: this.erase() };
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
    // The phrase in flight goes in (a radio word among it does nothing).
    const rest = parseRadio(all).text;
    return { kind: "leave", ...this.settle(rest ? rest + " " : "") };
  }

  /** The phrase under way, typed as far as it may be now. */
  private guess(): DictOutcome {
    if (this.phraseAt < 0) this.phraseAt = this.typed.length;
    const all = joinWords(this.held, this.partial);
    // A first phrase that may be a command waits whole for the router.
    const want = this.routes && !this.before && mayBeCommand(all) ? "" : holdBack(all).type;
    const fix = retype(this.guessed, want);
    if (!fix.erase && !fix.type) return { kind: "continue" };
    return withErase({ kind: "type" as const, text: fix.type, route: false, guess: true }, fix.erase);
  }

  /** The phrase under way ends as `text`: the keys from its guess to that.
   * A first phrase the router may take is taken back whole first, so the
   * router reads it before any of it is in the field. */
  private settle(text: string): { text: string; erase?: string } {
    const guessed = this.guessed;
    const whole = this.routes && !this.before && mayBeCommand(text);
    this.phraseAt = -1;
    this.held = "";
    this.partial = "";
    const fix = whole ? { erase: guessed, type: text } : retype(guessed, text);
    return withErase({ text: fix.type }, fix.erase);
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
    return text;
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
  /** A text box that sends itself (the ask pop-up's answer field): "over"
   * calls this instead of leaving the words in the box. `say`: the "Read
   * replies aloud" pref. */
  submit?(say: boolean): Promise<{ ok: boolean; line: string }>;
}

/** The focused session's agent terminal: keys down its socket, as typed. */
export function sessionTarget(title: string, name: string): DictTarget {
  const send = (data: string) => peekTerm(title, "agent")?.send(data) ?? false;
  return {
    kind: "session",
    title,
    name,
    type: (text) => send(text),
    erase: (text) => !text || send(backspaces(text)),
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
  route(text: string): Promise<boolean | "hush" | "end">;
  /** A continuous dictation sent a message (or the router acted) and goes
   * on: where the next message goes — null keeps the target it has. */
  retarget?(): { target: DictTarget | null; hint?: string } | null;
  /** "Stop talking": everything the app is saying or has queued, dropped. */
  hush?(): void;
  /** The session's live activity (the rows the app polls). */
  activity(title: string): string;
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
   * phrase's end. */
  quietMs?(): number;
  /** End the phrase under way at the recognizer now (its /end, its final). */
  endPhrase?(): void;
  /** A send went through `ms` after the word's end ("over → sent"). */
  sentIn?(ms: number): void;
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
  /** A stop phrase ("stop recording"). */
  stopped: "Stopped.",
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
    return { ok: true, line: r?.queued ? "Queued for " + name + "." : "Sent to " + name + "." };
  } catch (err) {
    const body = (err instanceof ApiError ? err.body : null) as
      | { in_dialog?: boolean; waking?: boolean; paused?: boolean }
      | null;
    let line = "Couldn't submit: " + ((err as Error)?.message || "error") + " — the words are still in the field";
    if (err instanceof ApiError && err.status === 409) {
      if (body?.in_dialog) line = name + " is on a dialog — press Enter yourself when it is answered";
      else if (body?.waking) line = name + " is waking up — say over again in a moment";
      else if (body?.paused) line = name + " is paused — resume it first";
    }
    return { ok: false, line };
  }
}

/** One dictation into one target: phrases are carried out in order (the
 * first may wait on the router), each typed as it comes. `target` null: no
 * session is focused — the first phrase can still be a command. */
export class LiveDictation {
  readonly dict = new Dictation();
  private chain: Promise<void> = Promise.resolve();
  private done = false;
  /** The router acted on a phrase, or a draft was submitted. */
  private acted = false;
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
  }

  /** Still taking words (false once over / a stop / Escape / an error). */
  get active(): boolean {
    return !this.done;
  }

  /** Web Speech's interim words: typed as a guess, corrected as they change. */
  hearing(t: string): Promise<void> {
    if (t) this.heardAnything = true;
    if (this.swallowing) return this.chain;
    return this.step(() => this.dict.hearing(t));
  }

  heard(phrase: string): Promise<void> {
    if (phrase.trim()) this.heardAnything = true;
    if (this.phraseEnded()) return this.chain;
    return this.step(() => this.dict.heard(phrase));
  }

  /** Streaming: one answer — committed words `w` and the unstable tail `p`,
   * typed as you speak and corrected as the recognizer changes its mind. */
  stream(w: string, p: string): Promise<void> {
    if (w.trim() || p.trim()) this.heardAnything = true;
    if (this.swallowing) return this.chain;
    return this.step(() => this.dict.stream(w, p));
  }

  /** Streaming: committed words with no unstable tail. */
  words(w: string): Promise<void> {
    return this.stream(w, "");
  }

  /** Streaming: a phrase's end and its last words (`final`: the accurate
   * re-transcription, when it differs — see {@link Dictation.flush}). */
  flush(w: string, final?: string): Promise<void> {
    if (w.trim() || final?.trim()) this.heardAnything = true;
    if (this.phraseEnded()) return this.chain;
    return this.step(() => this.dict.flush(w, final));
  }

  /** A phrase's end arrived: true when it is one taken from its guess
   * already (ignored); a send waiting on it goes with it. */
  private phraseEnded(): boolean {
    clearTimeout(this.sendWait);
    this.sendWait = undefined;
    if (!this.swallowing) return false;
    clearTimeout(this.swallowing);
    this.swallowing = undefined;
    return true;
  }

  /** The phrase is taken from its guess: ignore what the recognizer still
   * says of it. */
  private swallow(): void {
    clearTimeout(this.swallowing);
    this.swallowing = setTimeout(() => (this.swallowing = undefined), SWALLOW_MAX_MS);
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
    if (quiet < CONTROL_TAIL_MS) return;
    clearInterval(this.tailTimer);
    this.tailTimer = undefined;
    this.wordEnd = Date.now() - Math.min(quiet, 60_000);
    this.hooks.endPhrase?.();
    if (t.kind === "over" || t.kind === "out") {
      // The accurate final, if /end brings it in time; else the fast words.
      this.sendWait = setTimeout(() => {
        this.sendWait = undefined;
        this.swallow();
        void this.step(() => this.dict.heard(t.text));
      }, OVER_FINAL_WAIT_MS);
      return;
    }
    // "Disregard", a stop, "stop talking", a switch: no accurate text needed.
    this.swallow();
    void this.step(() => this.dict.heard(t.text));
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
    const base = this.opts.hint || (field ? "the words go into the text box" : DICT_HINT);
    const lead = pickupLine(this.recording, this.hearingNow);
    this.hooks.caption(lead ? lead + " · " + base : field ? "Listening — " + base : base, true);
  }

  private step(next: () => DictOutcome): Promise<void> {
    this.chain = this.chain
      .then(() => {
        if (this.done) return;
        const t = this.target;
        this.dict.answering = t?.kind === "session" && !!this.hooks.pending?.(t.title);
        return this.apply(next());
      })
      .catch(() => {})
      .then(() => this.watchTail());
    return this.chain;
  }

  private end(line: string | null, sticky = false, how?: DictEnd): void {
    if (this.done) return;
    this.done = true;
    clearInterval(this.tailTimer);
    clearTimeout(this.sendWait);
    clearTimeout(this.swallowing);
    this.dict.listening = false;
    this.hooks.halt();
    if (line !== null) this.hooks.caption(line, sticky);
    this.hooks.done?.(how || (this.acted || this.dict.typed ? "acted" : "missed"));
  }

  private async apply(out: DictOutcome): Promise<void> {
    const t = this.target;
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
        t?.erase(out.text);
        this.hooks.caption("Disregarded — go on", true);
        this.hooks.confirm?.("cleared");
        return;
      case "hush":
        if (out.erase && t && !this.keys(t, out.erase, "")) return;
        this.hushed();
        return;
      case "dropped":
        t?.erase(out.text);
        this.end(out.text ? "Disregarded" : "", false, "stopped");
        return;
      case "leave":
        if ((out.text || out.erase) && !(await this.put(out.text, true, out.erase))) return;
        if (out.stop) this.hooks.confirm?.("stopped");
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
        const typedNow = !!(out.text || out.erase);
        if (typedNow && !(await this.put(out.text, true, out.erase))) return;
        if (this.done) return;
        // "Over and out" with nothing said since the last "over": just stop.
        if (out.out && !this.dict.typed) {
          this.hooks.confirm?.("bye");
          this.end(STOPPED_LINE, false, "stopped");
          return;
        }
        // A box that sends itself (the ask pop-up's answer): its own send.
        if (t?.kind === "field" && t.submit) {
          this.acted = true;
          this.hooks.caption("Sending to " + t.name + "…", true);
          const say = this.hooks.replies();
          const r = await t.submit(say);
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
        this.acted = true;
        this.hooks.caption("Sending to " + t.name + "…", true);
        if (typedNow) await new Promise((r) => setTimeout(r, SUBMIT_SETTLE_MS));
        const say = this.hooks.replies();
        const r = await submitDraft(t.title, t.name, say);
        if (r.ok) this.hooks.submitted(t.title, say);
        this.sent(r, out, from);
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
    const n = this.hooks.retarget?.();
    if (n) {
      this.target = n.target;
      this.opts = { ...this.opts, hint: n.hint };
      this.dict.routes = n.target?.kind !== "field";
    }
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
      if (!t || (!this.dict.typed && t.kind !== "field" && dialogUp(this.hooks.activity(t.title)))) return true;
      return this.keys(t, erase, text);
    }
    // What stays typed once the guess is corrected: nothing — the router and
    // the dialog guard come first, with the guess already taken back.
    if (this.dict.typed.length === erase.length && t?.kind !== "field") {
      if (erase && t && !this.keys(t, erase, "")) return false;
      erase = "";
      if (!text) return true;
      const words = text.trim();
      const routed = route ? await this.hooks.route(words) : false;
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
      if (dialogUp(this.hooks.activity(t.title))) {
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

  /** The keys themselves: `erase` taken back, then `text` typed — into a
   * terminal as one DEL per character and then the characters (two sends,
   * like the Backspace key and then typing), into a text box as one value.
   * False — and listening over — when they could not go. */
  private keys(t: DictTarget, erase: string, text: string): boolean {
    if (!erase && !text) return true;
    const ok = t.replace ? t.replace(erase, text) : (!erase || t.erase(erase)) && (!text || t.type(text));
    if (!ok) {
      this.end("Couldn't type into " + t.name + " — its terminal isn't connected", true);
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
    | "question";
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
   * something" row, typed with the keys. */
  text?: string;
  /** answer: the session has no dialog up (`say` says so). */
  no_dialog?: boolean;
}

const ROUTE_KINDS = new Set<string>([
  "prompt", "switch", "new", "ambiguous", "no-session", "new-failed", "list",
  "needs", "status", "cancel", "stop", "hush", "off", "repeat", "condense",
  "switch_index", "switch_rel", "answer", "question",
]);

/** Ask the router what one phrase means. Anything but a well-formed 200 —
 * the route missing (an older server), an error, the network — means "it is
 * a prompt": the words were meant for the session, never lost to the router. */
export async function routePhrase(text: string, session: string): Promise<RouteReply> {
  try {
    const r = await api<RouteReply>("/api/voice/route", { json: { text, session } });
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
  /** POST /api/voice/route for `session`. */
  route(text: string, session: string): Promise<RouteReply>;
  /** GET /api/voice's `approve_dialogs`, read now. */
  approveDialogs(): Promise<boolean>;
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
export async function runVoiceCommand(r: RouteReply, text: string, deps: CommandDeps): Promise<boolean> {
  switch (r.kind) {
    case "switch_index": {
      const n = Math.trunc(Number(r.index));
      const key = railRow(deps.railOrder(), n);
      if (!key) deps.announce("There is no session " + n);
      else if (isWindowRow(key)) deps.announce("Row " + n + " is a window, not a session");
      else {
        deps.select(key);
        deps.announce(r.say || "Now talking to " + deps.name(key) + ".");
      }
      return true;
    }
    case "switch_rel": {
      const key = railStep(deps.railOrder(), deps.focused(), Number(r.delta) || 1);
      if (!key) deps.announce("There is no other session");
      else {
        deps.select(key);
        deps.announce(r.say || "Now talking to " + deps.name(key) + ".");
      }
      return true;
    }
    case "answer":
      await answer(r, text, deps, false);
      return true;
    case "question":
      deps.announce(r.say || "Nothing is being asked.");
      return true;
    default:
      return false;
  }
}

/** An answer: into the dialog the server resolved (only with approving by
 * voice on), else to the question the session asked (as a prompt, through
 * the submit path), else the router's sentence. A row number alone is
 * mapped to its title and asked again, once. Focus never moves. */
async function answer(r: RouteReply, text: string, deps: CommandDeps, again: boolean): Promise<void> {
  if (!r.title && r.index != null && !again) {
    const key = railRow(deps.railOrder(), Number(r.index));
    if (!key || isWindowRow(key)) {
      deps.announce("There is no session " + Math.trunc(Number(r.index)));
      return;
    }
    const next = await deps.route(text, key);
    if (next.kind === "answer") await answer({ ...next, title: next.title || key, index: undefined }, text, deps, true);
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
    if (!(await deps.approveDialogs())) {
      deps.announce(APPROVE_OFF_LINE);
      return;
    }
    try {
      const json: Record<string, unknown> = { keys: r.keys, dialog_id: r.dialog_id, by: "user" };
      if (r.text) json.text = r.text;
      await instApi(title, "/answer", { json });
      deps.announce(r.say || "Answered " + (r.choice === "number" ? String(r.number || "") : r.choice || "") + ".");
    } catch (err) {
      const body = (err instanceof ApiError ? err.body : null) as { dialog_changed?: boolean; dialog_answered?: boolean } | null;
      if (body?.dialog_changed) deps.announce("The prompt on " + name + " changed — say it again.");
      else if (body?.dialog_answered) deps.announce("Already answered.");
      else deps.caption("Couldn't answer " + name + ": " + ((err as Error)?.message || "error"), true);
    }
    return;
  }
  if (await questionPending(title, deps.activity(title))) {
    const words = answerWords(r, text);
    if (!sessionTarget(title, name).type(words + " ")) {
      deps.caption("Couldn't type into " + name + " — its terminal isn't connected", true);
      return;
    }
    await new Promise((res) => setTimeout(res, SUBMIT_SETTLE_MS));
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

  /** A voice draft was submitted to `title`. `say` is what /send was told. */
  sent(title: string, say: boolean, now = Date.now()): void {
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
      const d = await x.fetchDialog(title).catch(() => null);
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
        x.caption(x.name(title) + " is waiting on you");
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

/** One engine: speak() resolves when the text has been said (or given up on). */
export interface Speaker {
  speak(text: string): Promise<void>;
  cancel(): void;
}

/** A queued line older than this when its turn comes is dropped, not said:
 * a backlog never reads stale news. */
export const SPEECH_STALE_MS = 60_000;
/** A confirmation ("Sent.") older than this when its turn comes is dropped:
 * late, it would only confuse. */
export const CONFIRM_STALE_MS = 5_000;
/** The sentences of {@link CONFIRM_LINES}, as the server speaker asks for
 * them ({@link speechSentences}): the ones it keeps in memory (built on
 * first use — the sentence rules are declared further down). */
let confirmSentences: ReadonlySet<string> | null = null;
const isConfirmSentence = (part: string) =>
  (confirmSentences ||= new Set(Object.values(CONFIRM_LINES).flatMap((l) => speechSentences(l)))).has(part);
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
  /** Called as its engine starts on it (a confirmation mutes the mic). */
  start?: () => void;
  /** When it was queued (ms). */
  at: number;
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
 *    old. */
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
  last = "";

  constructor(
    private engine: () => Speaker,
    private onSpeaking: (on: boolean) => void = () => {},
    private opts: { now?: () => number; staleMs?: number; log?: (msg: string) => void } = {}
  ) {}

  private now(): number {
    return this.opts.now ? this.opts.now() : Date.now();
  }

  /** Queue a line; resolves once it has been said (or cut off, or dropped).
   * `at`: when its news happened (ms), if earlier than now; `title`: the
   * session it is about. */
  say(line: SpeechLine, opts: { at?: number; title?: string } = {}): Promise<void> {
    if (typeof line === "string" && !line.trim()) return Promise.resolve();
    return new Promise<void>((done) => {
      this.lines.push({ line, at: opts.at ?? this.now(), title: opts.title || "", done });
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

  get busy(): boolean {
    return !!this.speaking;
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
  }

  release(): void {
    this.held = false;
    this.pump();
  }

  /** Stop and forget everything queued — or, with `title`, only that
   * session's lines (the one being said, if it is one of them): the others
   * play on. */
  cancel(title?: string): void {
    const drop = title === undefined ? this.lines : this.lines.filter((l) => l.title === title);
    this.lines = title === undefined ? [] : this.lines.filter((l) => l.title !== title);
    drop.forEach((l) => l.done());
    if (this.current && (title === undefined || this.current.title === title)) this.cut();
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
    this.onSpeaking(false);
    cur?.done();
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
      const next = this.lines.shift();
      if (!next) return this.settle();
      const stale = next.confirm ? CONFIRM_STALE_MS : this.opts.staleMs ?? SPEECH_STALE_MS;
      const age = this.now() - next.at;
      const text = String((typeof next.line === "function" ? next.line() : next.line) || "").trim();
      if (age > stale) {
        (this.opts.log ?? ((m: string) => console.debug(m)))(
          "[voice] dropped a line " + Math.round(age / 1000) + " s old: " + text.slice(0, 80)
        );
        next.done();
        continue;
      }
      if (!text) {
        next.done();
        continue;
      }
      const sp = this.engine();
      const turn = ++this.turn;
      this.last = text;
      this.speaking = sp;
      this.current = next;
      this.onSpeaking(true);
      next.start?.();
      const done = () => {
        if (this.turn !== turn || !this.speaking) return;
        this.speaking = null;
        this.current = null;
        this.onSpeaking(false);
        next.done();
        this.pump();
      };
      sp.speak(text).then(done, done);
      return;
    }
  }
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
      queue: Pick<SpeechQueue, "confirmLine">;
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
   * (a switch). Resolves once it was said, cut or dropped. */
  say(kind: ConfirmKind | { line: string }): Promise<void> {
    const x = this.deps;
    if (!x.on()) return Promise.resolve();
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
        if (muted) mic?.mute?.(false);
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

const WORD_MS = 400; // onend fallback: per word…
const CHUNK_GRACE_MS = 1500; // …plus this per chunk

/** speechSynthesis, a chunk per sentence. Utterances stay referenced until
 * they end — a garbage-collected one never fires onend (mobile.js PhoneTTS). */
export function browserSpeaker(): Speaker & { available(): boolean } {
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
    speak(text) {
      return new Promise<void>((resolve) => {
        const parts = sentenceChunks(text);
        if (!available() || !parts.length) return resolve();
        const g = gen;
        let i = 0;
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
          const timer = setTimeout(finish, part.split(/\s+/).length * WORD_MS + CHUNK_GRACE_MS);
          keep.push(u);
          try {
            synth!.speak(u);
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
  }
  if (audioCtx.state === "suspended") void audioCtx.resume().catch(() => {});
  return audioCtx;
}

/** POST /api/voice/speak → audio bytes (Content-Type the backend's: mpeg for
 * a host, wav for piper) played through the one AudioContext — one request
 * per sentence ({@link speechSentences}), pipelined: the first sentence is
 * asked for at once and plays the moment it is back, the next is asked for
 * while it plays (one request in flight, never more — a CPU engine would
 * only share itself out), and each is scheduled to start the instant the
 * one before ends, so nothing waits on the whole line and there is no gap
 * when the next is ready in time. speak() resolves when the last sentence
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
  opts: { voiceKey?: () => string; cacheable?: (part: string) => boolean } = {}
): Speaker & { lastError: string; prefetch(lines: readonly string[]): Promise<void>; cached(part: string): boolean } {
  let gen = 0;
  /** Synthesised confirmations: voice + "\n" + sentence → its audio. */
  const cache = new Map<string, Promise<AudioBuffer>>();
  const cacheable = opts.cacheable || isConfirmSentence;
  const keyOf = (part: string) => (opts.voiceKey ? opts.voiceKey() : "") + "\n" + part;
  /** Sources started or scheduled (the one playing and the one queued
   * after it), each with what ends its promise. */
  const playing = new Map<AudioBufferSourceNode, () => void>();
  let inflight: AbortController | null = null;

  const fetchAudio = async (ctx: AudioContext, part: string, ac: AbortController): Promise<AudioBuffer> => {
    const r = await apiRaw("/api/voice/speak", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: part }),
      signal: ac.signal,
    });
    if (!r.ok) {
      const j = (await r.json().catch(() => ({}))) as { error?: string };
      throw new Error(j.error || "HTTP " + r.status);
    }
    return ctx.decodeAudioData(await r.arrayBuffer());
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
        for (const part of speechSentences(line)) {
          if (cache.has(keyOf(part))) continue;
          const p = fetchAudio(ctx, part, new AbortController());
          remember(part, p);
          await p.catch(() => {});
        }
    },
    async speak(text: string): Promise<void> {
      const parts = speechSentences(text);
      if (!parts.length) return;
      const g = gen;
      const ctx = voiceAudioCtx();
      if (!ctx || ctx.state !== "running") {
        self.lastError = "AudioContext " + (ctx ? ctx.state : "missing");
        return fallback.speak(parts.join(" "));
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
          return fallback.speak(parts.slice(i).join(" "));
        }
        if (g !== gen) return;
        self.lastError = "";
        // Sentence i plays (or waits its turn) while i+1 is asked for.
        if (i + 1 < parts.length) next = ask(i + 1);
        const at = Math.max(ctx.currentTime, endAt);
        endAt = at + (audio.duration || 0);
        last = play(ctx, audio, at);
      }
      await last;
    },
    cancel() {
      gen++;
      inflight?.abort();
      inflight = null;
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
  onFinal(text: string): void;
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
  /** Streaming: a phrase ended (a pause, or finish()); its last words.
   * `final`: the whole phrase as the server's accurate model heard it — set
   * only when it differs from the fast words (absent: they stand). */
  onFlush?(text: string, final?: string): void;
  /** A passing line for the caption (streaming fell back to clips). */
  onNotice?(text: string): void;
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
 * soon after it is said. */
export const SILENCE_MS = 1100;
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
}

/** A response's words (the server joins them with spaces). */
const wordsOf = (v: unknown) => String((Array.isArray(v) ? v.join(" ") : v) || "").trim();
const langTag = () => String(navigator.language || "en").split(";")[0];

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
 *    409 on the open, or any failed request, falls back to clips for the rest
 *    of the dictation.
 * While listening is wanted the next phrase starts recording as soon as one
 * ends, so phrases arrive one by one like Chrome's finals. The microphone is
 * `opts.mic`'s (lib/voiceMic): held while listening, kept warm a while after,
 * given back by {@link Recognizer.release}. */
export function serverRecognizer(
  opts: { stream?: () => boolean; mic?: MicKeeper } = {}
): Recognizer & { lastError: string; mic: MicKeeper; passMs: number | null; finalMs: number | null } {
  const mic = opts.mic || new MicKeeper();
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
  /** The previous phrase's /end: the next stream opens after it, so words
   * arrive in order and one stream at a time is open. */
  let lastEnd: Promise<void> = Promise.resolve();
  /** Streams open now, abandoned by stop(). */
  const open = new Set<string>();
  /** Barge-in: audio before this (ms) is the app's own voice dying away. */
  let skipUntil = 0;
  /** A confirmation plays: the clips drop the audio ({@link Recognizer.mute}). */
  let muted = false;

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

  const upload = async (blob: Blob) => {
    const hh = h;
    uploads++;
    const ac = new AbortController();
    aborters.add(ac);
    const timer = setTimeout(() => ac.abort(), UPLOAD_TIMEOUT_MS);
    try {
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
        if (text) hh.onFinal(text);
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

  /** As the phrase starts: open its stream (after the previous phrase's /end). */
  const openStream = (c: Phrase) => {
    const hh = h;
    const before = lastEnd;
    run(c, async () => {
      await before;
      if (h !== hh || !c.stream) return;
      try {
        const r = await call("/api/voice/stream", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ lang: langTag() }),
        });
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
        } else if (r.status === 409) {
          self.lastError = String(r.j.error || "the server can't stream");
          fallBack(c, "");
        } else throw new Error(String(r.j.error || "HTTP " + r.status));
      } catch (err) {
        if (h === hh) streamFailed(c, err);
      }
    });
  };

  const sendChunk = async (c: Phrase, blob: Blob, hh: RecognizerHandlers | null) => {
    try {
      const r = await call(streamPath(c.sid!, "/chunk"), {
        method: "POST",
        headers: { "Content-Type": "audio/wav" },
        body: blob,
      });
      if (h !== hh || !hh || !c.stream) return;
      if (!r.ok) throw new Error(String(r.j.error || "HTTP " + r.status));
      self.lastError = "";
      const ms = Number(r.j.ms);
      if (ms > 0) {
        self.passMs = ms;
        voicePass(ms);
      }
      const w = wordsOf(r.j.committed);
      const p = wordsOf(r.j.partial);
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
    const min = c.sent ? chunkMinS(self.passMs) : Math.max(0, c.speechS - c.trimS) + FIRST_CHUNK_S;
    const blob = c.rec.take(min);
    if (!blob) return;
    c.sent = true;
    c.fresh = false;
    const hh = h;
    run(c, () => sendChunk(c, blob, hh));
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
    let release = () => {};
    lastEnd = new Promise<void>((r) => (release = r));
    try {
      const tail = await c.rec!.stop().catch(() => null);
      if (token !== mine) return; // stop(): dropped
      token = null;
      if (want && !finishing) void clip();
      if (c.busy) await c.busy;
      if (h !== hh || !hh) return;
      if (!c.stream) {
        // Fell back before the stream took anything: the phrase is a clip.
        if (c.heard && tail && tail.size > 44) void upload(tail);
        return;
      }
      if (!c.sid) return; // its open failed (it fell back), or it was dropped
      if (!c.heard) {
        // Nothing said: the stream opened for it goes unused.
        discard(c);
        return;
      }
      if (tail && tail.size > TAIL_MIN_BYTES && c.fresh) await sendChunk(c, tail, hh);
      if (h !== hh || !c.stream || !c.sid) return;
      const sid = c.sid;
      c.sid = null;
      open.delete(sid);
      try {
        const r = await call(streamPath(sid, "/end"), { method: "POST" });
        if (h !== hh) return;
        if (!r.ok) throw new Error(String(r.j.error || "HTTP " + r.status));
        c.stream = false;
        // The accurate pass over the whole phrase: only when it heard it
        // otherwise (`same`: nothing to correct; absent: the fast words stand).
        const fms = Number(r.j.final_ms);
        if (fms > 0) {
          self.finalMs = fms;
          voiceMark("final");
        }
        const final = typeof r.j.final === "string" && r.j.same !== true ? wordsOf(r.j.final) : "";
        hh.onPartial?.("");
        hh.onFlush?.(wordsOf(r.j.committed), final || undefined);
      } catch (err) {
        if (h !== hh) return;
        // Its held words still go in (fallBack flushes a phrase with a stream).
        c.sid = sid;
        streamFailed(c, err);
      }
    } finally {
      release();
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

  /** End the current phrase and send it. */
  const endClip = () => {
    const c = cur;
    cur = null;
    clearTimeout(capTimer);
    if (!c?.rec) return;
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
        if (c.heard && blob.size) void upload(blob);
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
    if (ctx && ctx.state === "suspended") await ctx.resume().catch(() => {});
    if (token !== mine) return discard(c);
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
    // The silence detector: the recorder's RMS per block against a noise
    // floor that follows the quiet moments.
    let floor = 0;
    let lastLoud = 0;
    let blockAt = 0; // the recording's time before this block
    const onLevel = (rms: number) => {
      if (cur !== c) return;
      const at = blockAt;
      blockAt = c.rec?.seconds() ?? 0;
      h?.onLevel?.(rms);
      floor = noiseFloor(floor, rms);
      const now = Date.now();
      if (isLoud(rms, floor)) {
        if (!c.heard) {
          c.speechS = at;
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
        lastLoud = now;
      } else if (!c.heard && c.stream) {
        // Before the first word, keep only a lead-in (under 300 ms): the
        // stream is not sent the quiet before it.
        if (c.rec?.take()) c.trimS = blockAt;
      }
      if (c.heard && now - lastLoud > SILENCE_MS) endClip();
      else pump(c);
    };
    try {
      const skip = Math.max(0, skipUntil - Date.now());
      c.rec = startWavRecording(s, ctx, { targetRate: CLIP_RATE, onLevel, skipS: skip / 1000 });
    } catch {
      discard(c);
      failed("start-failed");
      return;
    }
    if (muted) c.rec.mute?.(true);
    cur = c;
    voiceMark("mic");
    h?.onReady?.();
    if (!c.stream)
      capTimer = setTimeout(() => {
        if (cur === c) endClip();
      }, CLIP_MAX_MS);
  };

  const self = {
    name: "server" as const,
    lastError: "",
    /** The server's last streaming pass, in ms (null before one). */
    passMs: null as number | null,
    /** The last phrase's accurate re-transcription (/end `final_ms`), in ms. */
    finalMs: null as number | null,
    mic,
    available: () => typeof navigator !== "undefined" && !!navigator.mediaDevices?.getUserMedia && !!audioCtxCtor(),
    mute(on: boolean) {
      muted = on;
      cur?.rec?.mute?.(on, BARGE_SKIP_MS / 1000);
    },
    endPhrase() {
      if (cur?.heard && want) endClip();
    },
    start(handlers: RecognizerHandlers, o: { skipMs?: number } = {}) {
      self.stop();
      if (!self.available()) return false;
      skipUntil = Date.now() + Math.max(0, o.skipMs || 0);
      muted = false;
      h = handlers;
      want = true;
      finishing = false;
      holding = true;
      mic.hold();
      streaming = !!opts.stream?.();
      lastEnd = Promise.resolve();
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
      clearTimeout(capTimer);
      const c = cur;
      cur = null;
      c?.rec?.cancel();
      aborters.forEach((a) => a.abort());
      aborters.clear();
      open.forEach(abandon);
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
    tts_backend?: string;
    tts_detail?: string;
  };
  system: VoiceSystem;
}
export const DEFAULT_VOICE_CFG: VoiceCfg = { stt: "phone", tts: "phone", server_ready: {}, system: {} };

/** Where speech runs (Settings → Voice). Unreachable / older server: `prev`
 * (a blink must not swap engines), else the browser's own. */
export async function loadVoiceCfg(prev: VoiceCfg | null): Promise<VoiceCfg> {
  try {
    const j = await api<Partial<VoiceCfg>>("/api/voice");
    if (j && typeof j === "object") {
      return {
        stt: j.stt === "server" ? "server" : "phone",
        tts: j.tts === "server" ? "server" : "phone",
        approve_dialogs: j.approve_dialogs === true,
        ...(typeof j.say_all === "boolean" ? { say_all: j.say_all } : {}),
        server_ready: j.server_ready && typeof j.server_ready === "object" ? j.server_ready : {},
        system: j.system && typeof j.system === "object" ? j.system : {},
      };
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
