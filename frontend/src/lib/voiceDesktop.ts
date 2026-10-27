/** The desktop mic (components/TopBarMic.tsx): everything that is not DOM.
 *
 * The words are typed into the focused session AS YOU SPEAK: each recognised
 * phrase goes straight into the session's input field (raw keys down its
 * terminal, like typing), and three radio words end a phrase:
 *  - "over" — stop listening; the words stay in the field, unsent;
 *  - "submit" — stop listening and submit the draft (Enter, through
 *    POST /api/instances/{title}/send {enter: true}, after the same dialog
 *    checks as a send);
 *  - "disregard" — take back what this dictation typed (one backspace per
 *    character) and keep listening.
 * The phone page (backend/web/static/mobile.js, its VOICE section) holds its
 * draft and sends it whole instead; the desktop also differs in that
 *  - push to talk: a click (or a held button) starts and stops listening, and
 *    stopping is "over" — the mic is not on until you key it;
 *  - a dictation's first phrase is first offered to the voice router (POST
 *    /api/voice/route), so "switch to the login fix" or "start a new session
 *    for…" act instead of being typed into the focused session;
 *  - replies are read aloud only when you turn that on (off by default), only
 *    for drafts THIS app submitted by voice, once per turn, and always
 *    prefixed with the session's name — several sessions may be talking.
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
import { resamplerName, startWavRecording, type WavRecording } from "./wavRecorder";

// --- the radio words -------------------------------------------------------- //

export type RadioKind = "over" | "submit" | "disregard" | "";

/** "fix the bug over" → {kind: "over", text: "fix the bug"}; "… submit" →
 * {kind: "submit"}; "… disregard" (or "disregard that") → {kind:
 * "disregard"}; else {kind: "", text}. Case and punctuation do not matter
 * ("Over." from Safari); only the LAST word counts — an "over" mid-sentence
 * is a word, and "hangover" is not "over". The shape is mobile.js
 * `voiceDictParse`'s, so both surfaces hear alike. */
export function parseRadio(text: string): { kind: RadioKind; text: string } {
  const t = String(text || "").trim();
  const m = /^(.*?)[\s,.!?]*\b(over|submit|disregard)(?:\s+(?:that|it|all|everything))?[.!?\s]*$/i.exec(t);
  if (!m) return { kind: "", text: t };
  return { kind: m[2].toLowerCase() as RadioKind, text: m[1].trim() };
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

const bareWord = (w: string) => w.toLowerCase().replace(/[^\p{L}\p{N}']+/gu, "");

/** "Stop recording" and its kin: said at the end of any committed words they
 * stop listening at once, never typed, and the words before them stay. */
const STOP_PHRASES = [
  "stop recording", "stop listening", "mic off", "microphone off", "turn mic off",
  "turn microphone off", "that's all", "thats all", "that is all",
].map((p) => p.split(" "));
/** Every phrase the hold-back keeps whole while it may still be said: the
 * stop family and the radio words with their tails ("submit it"). */
const CONTROL_PHRASES = [
  ...STOP_PHRASES,
  ...["over", "submit", "disregard"].flatMap((w) =>
    [[w], ...["that", "it", "all", "everything"].map((tail) => [w, tail])]
  ),
];
/** The longest control phrase, in words: what the hold-back may keep. */
export const CONTROL_MAX_WORDS = Math.max(...CONTROL_PHRASES.map((p) => p.length));

/** The indexes of `ws`'s real words (a bare "." is not one). */
const wordIndexes = (ws: string[]) => ws.flatMap((w, i) => (bareWord(w) ? [i] : []));

/** Streamed words → what may be typed now and what waits: the last word
 * (it may be a radio word — never typed), and before it as many words (up to
 * {@link CONTROL_MAX_WORDS} in all) as could still be the start of a control
 * phrase — "submit it", "stop recording", "that is all" — with a bare
 * punctuation token kept with its word. */
export function holdBack(text: string): { type: string; held: string } {
  const ws = String(text || "").trim().split(/\s+/).filter(Boolean);
  if (!ws.length) return { type: "", held: "" };
  const idx = wordIndexes(ws);
  let n = idx.length ? idx[idx.length - 1] : 0;
  for (let k = 2; k <= Math.min(CONTROL_MAX_WORDS, idx.length); k++) {
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

/** A whole answer to a dialog or a question: yes / no / always / a number,
 * as the router hears them. Only ever the WHOLE dictation counts — "the
 * answer is no, but…" is a prompt. */
export function isAnswerPhrase(text: string): boolean {
  return ANSWER_RE.test(routerWords(text).join(" "));
}

const joinWords = (a: string, b: string) => [a.trim(), b.trim()].filter(Boolean).join(" ");

// --- prefs (per browser: a hand's habit, not a synced setting) -------------- //

export type PttMode = "toggle" | "hold";
export interface VoicePrefs {
  ptt: PttMode;
  /** Read replies aloud. Off on the desktop: the phone is the hands-free one. */
  replies: boolean;
}
export const VOICE_PREFS_KEY = "cs_voice_desktop";
export const DEFAULT_VOICE_PREFS: VoicePrefs = { ptt: "toggle", replies: false };

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
  const o = raw && typeof raw === "object" ? (raw as Record<string, unknown>) : {};
  return {
    ptt: o.ptt === "hold" ? "hold" : "toggle",
    replies: o.replies === true,
  };
}

export function saveVoicePrefs(p: VoicePrefs, kv: KV | null = storage()): void {
  try {
    kv?.setItem(VOICE_PREFS_KEY, JSON.stringify({ ptt: p.ptt, replies: !!p.replies }));
  } catch {
    /* private window / quota: the choice lasts this page */
  }
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
 * never ends a held button) is "over"; in "Click to talk", each down starts
 * or ends ("over") and up is nothing. */
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

/** What a step of {@link Dictation} asks for. `text` to type is a phrase plus
 * its trailing space; `text` to erase is everything this dictation typed. */
export type DictOutcome =
  /** Type `text` now; listening goes on. `route: false`: it can't be a
   * router command (a streamed phrase's first words), so it goes straight in. */
  | { kind: "type"; text: string; route?: boolean }
  /** "over": type `text` (if any), stop — the words stay in the field.
   * `stop`: it was a stop phrase ("stop recording"), not the radio word. */
  | { kind: "over"; text: string; stop?: boolean }
  /** "submit": type `text` (if any), stop, and submit the draft. */
  | { kind: "submit"; text: string }
  /** "disregard": take back `text`; listening goes on. */
  | { kind: "erase"; text: string }
  /** Escape: take back `text` and stop. */
  | { kind: "dropped"; text: string }
  /** Nothing to do (an empty phrase). */
  | { kind: "continue" }
  /** Not listening: nothing to do. */
  | { kind: "stopped" };

/** Two minutes without a word stops listening (the words stay in the field). */
export const DICT_IDLE_MS = 120_000;
/** A hold released mid-phrase waits this long for the phrase's final. */
export const HOLD_FINAL_WAIT_MS = 1500;
export const DICT_HINT = "Say over to leave it, submit to send, disregard to start over";
/** The caption after "over" (and push to talk let go) on a session. */
export const OVER_LINE = "In the field — say submit or press Enter";
/** The caption after a stop phrase with nothing typed. */
export const STOPPED_LINE = "Stopped listening";

/** One dictation: begin → heard… → over / submit / cancel / expire, with a
 * record of what it typed (in order) so "disregard" can take exactly that
 * back. Pure, so the radio words, push to talk and Escape are pinned by
 * tests; {@link LiveDictation} carries the outcomes out. A toggle's second
 * click and a hold's release are both {@link finish}: "over".
 *
 * A streaming recognizer delivers a phrase as it is said: {@link words} as
 * they are committed, then {@link flush} at its end, which is that phrase's
 * {@link heard} — so the radio words, read off the whole phrase, work alike. */
export class Dictation {
  listening = false;
  interim = "";
  /** What this dictation typed so far — only what really went in
   * ({@link wrote}), since a disregard takes it back character by character. */
  typed = "";
  /** Streaming: committed words not typed yet — the last one (it may be a
   * radio word), or a first phrase whole while it may be a router command. */
  held = "";
  /** Streaming: the recognizer's unstable tail — shown, never typed. */
  partial = "";
  /** A first phrase goes past the router (false in a text box). */
  routes = true;
  /** A dialog or a question waits on the session: an answer said as the
   * whole dictation goes to the router at once (set before each step). */
  answering = false;

  begin(): void {
    this.listening = true;
    this.interim = "";
    this.typed = "";
    this.held = "";
    this.partial = "";
  }

  /** The caption: what is in the field, then the words in flight (the
   * streamed {@link partial} is shown apart, muted). */
  get shown(): string {
    return [this.typed.trim(), this.held.trim(), this.interim.trim()].filter(Boolean).join(" ");
  }

  hearing(t: string): void {
    if (this.listening) this.interim = t;
  }

  /** Streaming: the tail the recognizer may still change. */
  unsure(t: string): void {
    if (this.listening) this.partial = t;
  }

  /** Streaming: words committed in the phrase under way. All but the last
   * are typed now; a first phrase that may be a command waits whole. */
  words(w: string): DictOutcome {
    if (!this.listening) return { kind: "stopped" };
    const all = joinWords(this.held, w);
    const ctl = this.control(all);
    if (ctl) return ctl;
    if (this.routes && !this.typed && mayBeCommand(all)) {
      this.held = all;
      return { kind: "continue" };
    }
    const cut = holdBack(all);
    this.held = cut.held;
    return cut.type ? { kind: "type", text: cut.type + " ", route: false } : { kind: "continue" };
  }

  /** Streaming: the phrase ended with its last committed words — with the
   * held ones, the phrase's final ({@link heard}: radio words and all). */
  flush(w: string): DictOutcome {
    if (!this.listening) return { kind: "stopped" };
    const all = joinWords(this.held, w);
    this.held = "";
    this.partial = "";
    return this.heard(all);
  }

  /** `text` went into the field. */
  wrote(text: string): void {
    this.typed += text;
  }

  /** One final phrase. */
  heard(phrase: string): DictOutcome {
    if (!this.listening) return { kind: "stopped" };
    this.interim = "";
    const ctl = this.control(phrase);
    if (ctl) return ctl;
    const p = parseRadio(phrase);
    const text = p.text ? p.text + " " : "";
    switch (p.kind) {
      case "disregard":
        return { kind: "erase", text: this.erase() };
      case "over":
      case "submit":
        this.listening = false;
        return { kind: p.kind, text };
      default:
        return text ? { kind: "type", text } : { kind: "continue" };
    }
  }

  /** The toggle's second click, the hold's release: "over". A phrase still
   * in flight (no final yet) counts — it was said. */
  finish(): DictOutcome {
    if (!this.listening) return { kind: "stopped" };
    const tail = joinWords(this.held, this.interim);
    this.held = "";
    this.partial = "";
    const out = tail ? this.heard(tail) : { kind: "continue" as const };
    this.listening = false;
    if (out.kind === "type") return { kind: "over", text: out.text };
    if (out.kind === "erase") return { kind: "dropped", text: out.text };
    if (out.kind === "continue") return { kind: "over", text: "" };
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
    this.interim = "";
    // Streamed words held back go in (a radio word among them does nothing).
    const rest = parseRadio(this.held).text;
    this.held = "";
    this.partial = "";
    return { kind: "over", text: rest ? rest + " " : "" };
  }

  /** The control phrases any committed words can end with: the stop family
   * (stop now; the words before it go in) and, while {@link answering}, an
   * answer said as the whole dictation (to the router at once, without
   * waiting for the phrase's end). Else null. */
  private control(all: string): DictOutcome | null {
    const before = stopTail(all);
    if (before !== null) {
      this.listening = false;
      this.interim = "";
      this.held = "";
      this.partial = "";
      return { kind: "over", text: before ? before + " " : "", stop: true };
    }
    if (this.answering && this.routes && !this.typed && isAnswerPhrase(all)) {
      this.held = "";
      this.partial = "";
      return { kind: "type", text: all.trim() + " " };
    }
    return null;
  }

  /** Forget what was typed; returns it, for the target to take back. */
  private erase(): string {
    const text = this.typed;
    this.typed = "";
    this.interim = "";
    this.held = "";
    this.partial = "";
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
   * on it (and said so) — then it is not typed. */
  route(text: string): Promise<boolean>;
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
}

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
      else if (body?.waking) line = name + " is waking up — say submit again in a moment";
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

  constructor(
    private target: DictTarget | null,
    private hooks: LiveHooks,
    /** `hint`: the caption before a word (hands-free: what to answer). */
    private opts: { hint?: string } = {}
  ) {
    this.dict.begin();
    this.dict.routes = target?.kind !== "field";
  }

  /** Still taking words (false once over / submit / Escape / an error). */
  get active(): boolean {
    return !this.done;
  }

  hearing(t: string): void {
    if (this.done) return;
    if (t) this.heardAnything = true;
    this.dict.hearing(t);
    this.show();
  }

  heard(phrase: string): Promise<void> {
    if (phrase.trim()) this.heardAnything = true;
    return this.step(() => this.dict.heard(phrase));
  }

  /** Streaming: committed words, typed as they come (the last held back). */
  words(w: string): Promise<void> {
    if (w.trim()) this.heardAnything = true;
    return this.step(() => this.dict.words(w));
  }

  /** Streaming: a phrase's end and its last words. */
  flush(w: string): Promise<void> {
    if (w.trim()) this.heardAnything = true;
    return this.step(() => this.dict.flush(w));
  }

  /** Streaming: the unstable tail, for the caption only. */
  unsure(t: string): void {
    if (this.done) return;
    if (t) this.heardAnything = true;
    this.dict.unsure(t);
    this.show();
  }

  /** A passing line (streaming fell back to clips): the next words replace it. */
  notice(line: string): void {
    if (!this.done) this.hooks.caption(line, true);
  }

  /** Push to talk let go: "over", after the phrases already heard. */
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

  /** The running text, or what to say. */
  show(): void {
    if (this.done) return;
    const field = this.target?.kind === "field";
    const shown =
      this.dict.shown || this.opts.hint || (field ? "Listening — the words go into the text box" : DICT_HINT);
    this.hooks.caption(shown, true, this.dict.partial);
  }

  private step(next: () => DictOutcome): Promise<void> {
    this.chain = this.chain
      .then(() => {
        if (this.done) return;
        const t = this.target;
        this.dict.answering = t?.kind === "session" && !!this.hooks.pending?.(t.title);
        return this.apply(next());
      })
      .catch(() => {});
    return this.chain;
  }

  private end(line: string | null, sticky = false, how?: DictEnd): void {
    if (this.done) return;
    this.done = true;
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
        if (await this.put(out.text, out.route !== false)) this.show();
        return;
      case "erase":
        t?.erase(out.text);
        this.hooks.caption("Disregarded — go on", true);
        return;
      case "dropped":
        t?.erase(out.text);
        this.end(out.text ? "Disregarded" : "", false, "stopped");
        return;
      case "over":
        if (out.text && !(await this.put(out.text))) return;
        this.end(
          t?.kind === "session" && this.dict.typed ? OVER_LINE : out.stop ? STOPPED_LINE : "",
          false,
          out.stop ? "stopped" : undefined
        );
        return;
      case "submit": {
        const typedNow = !!out.text;
        if (out.text && !(await this.put(out.text))) return;
        // A text box: Enter is never pressed for you there.
        if (!t || t.kind === "field") {
          this.end(t ? "" : "Click a session first");
          return;
        }
        this.acted = true;
        this.end("Submitting to " + t.name + "…", true);
        if (typedNow) await new Promise((r) => setTimeout(r, SUBMIT_SETTLE_MS));
        const say = this.hooks.replies();
        const r = await submitDraft(t.title, t.name, say);
        if (r.ok) this.hooks.submitted(t.title, say);
        this.hooks.caption(r.line, !r.ok);
        return;
      }
    }
  }

  /** Type `text`; a session's first phrase goes past the router (unless it
   * can't be a command: `route` false) and the dialog guard first. False when
   * it did not go in (and listening ended). */
  private async put(text: string, route = true): Promise<boolean> {
    const t = this.target;
    if (!this.dict.typed && t?.kind !== "field") {
      const words = text.trim();
      if (route && (await this.hooks.route(words))) {
        this.acted = true;
        this.end(null);
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
    if (!t || !t.type(text)) {
      this.end("Couldn't type into " + (t?.name || "the session") + " — its terminal isn't connected", true);
      return false;
    }
    this.dict.wrote(text);
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
  /** answer: the session has no dialog up (`say` says so). */
  no_dialog?: boolean;
}

const ROUTE_KINDS = new Set<string>([
  "prompt", "switch", "new", "ambiguous", "no-session", "new-failed", "list",
  "needs", "status", "cancel", "stop", "off", "repeat", "condense",
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
      await instApi(title, "/answer", { json: { keys: r.keys, dialog_id: r.dialog_id, by: "user" } });
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

/** Every spoken reply names its session first: several may be talking. */
export function spokenLine(name: string, say: string): string {
  const s = String(say || "").trim();
  const n = String(name || "").trim();
  return n ? n + ": " + s : s;
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
   * the Say line's own stamp, or the event's). The line to speak, or null:
   * not ours, already spoken, or an idle that predates the prompt. */
  take(title: string, name: string, say: string, ts: number | null, now = Date.now()): string | null {
    if (!this.waiting(title, now)) return null;
    const p = this.pending.get(title)!;
    const fresh = !ts || ts * 1000 >= p.sentAt - STAMP_SLACK_S * 1000;
    const s = String(say || "").trim();
    if (s && fresh) {
      this.pending.delete(title);
      return spokenLine(name, s);
    }
    // No Say line this turn (or only the previous turn's): one word, so the
    // owner who asked to hear back is not left waiting — but only once the
    // session is known to have worked on it, or a stale idle would end it.
    if (p.working) {
      this.pending.delete(title);
      return spokenLine(name, "Finished.");
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
 * prompt takes words, any other dialog the numbers of its options. */
export function dialogChoices(d: Pick<Dialog, "options">): string {
  const kinds = new Set(d.options.map((o) => o.kind));
  if (kinds.has("yes") || kinds.has("no")) return kinds.has("always") ? "Say yes, no, or always." : "Say yes or no.";
  const opts = d.options.slice(0, ORD.length);
  const nums = opts.map((_, i) => ORD[i]);
  const list =
    nums.length === 1
      ? nums[0]
      : nums.length === 2
        ? nums[0] + " or " + nums[1]
        : nums.slice(0, -1).join(", ") + ", or " + nums[nums.length - 1];
  return opts.map((o, i) => ORD[i] + ": " + plain(o.label)).join(". ") + ". Say " + list + ".";
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
export function dialogRetryLine(d: Pick<Dialog, "options">): string {
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
  /** Caption the line and say it; resolves once it has been said. */
  speak(line: string): Promise<void>;
  caption(line: string, sticky?: boolean): void;
  /** Open the mic on `title` for `ms` (the caption `hint` until a word):
   * resolves with how that dictation ended. */
  listen(title: string, hint: string, ms: number): Promise<DictEnd>;
}

/** With "Read replies aloud" on: a dialog that comes up on the focused
 * session, or a question in a reply to a prompt this app sent, is read out
 * name first and the mic opens {@link ANSWER_LISTEN_MS} for the answer; a
 * miss gets ONE retry ("I didn't catch that: …"), then it goes quiet. */
export class HandsFree {
  private seen = "";
  private running = false;

  constructor(private deps: HandsFreeDeps) {}

  /** A dialog came up on `title` (its activity went to clarify). */
  async dialog(title: string): Promise<void> {
    const x = this.deps;
    if (!this.ready(title) || title !== x.focused()) return;
    this.running = true;
    try {
      const d = await x.fetchDialog(title).catch(() => null);
      if (!d || !d.parsed || !d.options.length || d.id === this.seen) return;
      if (!x.replies() || x.busy() || title !== x.focused()) return;
      this.seen = d.id;
      const name = x.name(title);
      await x.speak(dialogSpeech(name, d));
      if (!(await x.approveDialogs())) {
        await x.speak("Answer it on the screen.");
        return;
      }
      await this.listen(title, dialogRetryLine(d), name + ": " + dialogChoices(d));
    } finally {
      this.running = false;
    }
  }

  /** `title`'s reply (already read out) asks `say`, a question. */
  async question(title: string, say: string): Promise<void> {
    if (!this.ready(title)) return;
    this.running = true;
    try {
      const name = this.deps.name(title);
      await this.listen(title, "Still there? " + spokenLine(name, say), "Answer " + name + " — say submit to send");
    } finally {
      this.running = false;
    }
  }

  private ready(title: string): boolean {
    return !!title && !this.running && this.deps.replies() && !this.deps.busy();
  }

  private async listen(title: string, retry: string, hint: string): Promise<void> {
    const x = this.deps;
    for (let tries = 0; ; tries++) {
      if (!x.replies() || x.busy()) return;
      const how = await x.listen(title, hint, ANSWER_LISTEN_MS);
      if (how !== "missed") return;
      if (tries >= 1) {
        x.caption(x.name(title) + " is waiting on you");
        return;
      }
      await x.speak(retry);
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

/** Lines in order, one at a time; {@link hold} keeps them waiting while the
 * mic listens (the app must not talk into its own recognizer, and a line
 * is not cut off by the next one). */
export class SpeechQueue {
  private lines: Array<{ text: string; done: () => void }> = [];
  private speaking = false;
  private held = false;
  last = "";

  constructor(
    private engine: () => Speaker,
    private onSpeaking: (on: boolean) => void = () => {}
  ) {}

  /** Queue a line; resolves once it has been said (or cut off, or dropped). */
  say(line: string): Promise<void> {
    const t = String(line || "").trim();
    if (!t) return Promise.resolve();
    return new Promise<void>((done) => {
      this.lines.push({ text: t, done });
      this.pump();
    });
  }

  get busy(): boolean {
    return this.speaking;
  }

  get queued(): number {
    return this.lines.length;
  }

  /** Listening: stop talking now; what is queued waits for {@link release}. */
  hold(): void {
    this.held = true;
    if (this.speaking) this.engine().cancel();
  }

  release(): void {
    this.held = false;
    this.pump();
  }

  /** Stop and forget everything queued. */
  cancel(): void {
    const dropped = this.lines;
    this.lines = [];
    dropped.forEach((l) => l.done());
    if (this.speaking) this.engine().cancel();
  }

  private pump(): void {
    if (this.speaking || this.held || !this.lines.length) return;
    const line = this.lines.shift()!;
    this.last = line.text;
    this.speaking = true;
    this.onSpeaking(true);
    const done = () => {
      this.speaking = false;
      this.onSpeaking(false);
      line.done();
      this.pump();
    };
    this.engine().speak(line.text).then(done, done);
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

const SPEAK_MAX_CHARS = 1200; // POST /api/voice/speak's cap

/** POST /api/voice/speak → audio bytes (Content-Type the backend's: mpeg for
 * a host, wav for piper) played through the one AudioContext. Any failure —
 * 409 (turned off), 502, the network, a clip that won't decode, a context
 * that isn't running — says that sentence group with `fallback` instead. */
export function serverSpeaker(fallback: Speaker): Speaker & { lastError: string } {
  let gen = 0;
  let playing: AudioBufferSourceNode | null = null;
  const self = {
    lastError: "",
    speak(text: string): Promise<void> {
      const groups: string[] = [];
      let cur = "";
      for (const s of sentenceChunks(text)) {
        if (cur && (cur + " " + s).length > SPEAK_MAX_CHARS) {
          groups.push(cur);
          cur = s;
        } else cur = cur ? cur + " " + s : s;
      }
      if (cur) groups.push(cur.slice(0, SPEAK_MAX_CHARS));
      const g = gen;
      const one = async (part: string): Promise<void> => {
        const ctx = voiceAudioCtx();
        if (!ctx || ctx.state !== "running") {
          self.lastError = "AudioContext " + (ctx ? ctx.state : "missing");
          return fallback.speak(part);
        }
        try {
          const r = await apiRaw("/api/voice/speak", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ text: part }),
          });
          if (!r.ok) {
            const j = (await r.json().catch(() => ({}))) as { error?: string };
            throw new Error(j.error || "HTTP " + r.status);
          }
          const audio = await ctx.decodeAudioData(await r.arrayBuffer());
          self.lastError = "";
          if (g !== gen) return;
          await new Promise<void>((resolve) => {
            const src = ctx.createBufferSource();
            src.buffer = audio;
            src.connect(ctx.destination);
            let done = false;
            const finish = () => {
              if (done) return;
              done = true;
              clearTimeout(timer);
              if (playing === src) playing = null;
              resolve();
            };
            src.onended = finish;
            const timer = setTimeout(finish, (audio.duration || 1) * 1000 + CHUNK_GRACE_MS);
            playing = src;
            try {
              src.start(0);
            } catch {
              finish();
            }
          });
        } catch (err) {
          self.lastError = String((err as Error)?.message || err);
          if (g !== gen) return;
          return fallback.speak(part);
        }
      };
      return groups.reduce<Promise<void>>((p, part) => p.then(() => (g === gen ? one(part) : undefined)), Promise.resolve());
    },
    cancel() {
      gen++;
      const s = playing;
      playing = null;
      if (s) {
        try {
          s.onended = null;
          s.stop(0);
        } catch {
          /* already stopped */
        }
      }
      fallback.cancel();
    },
  };
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
  /** Streaming (the server's, when it can): words committed — final, typed
   * as they come. */
  onWords?(text: string): void;
  /** Streaming: the unstable tail after them — shown, never typed. */
  onPartial?(text: string): void;
  /** Streaming: a phrase ended (a pause, or finish()); its last words. */
  onFlush?(text: string): void;
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
  start(h: RecognizerHandlers): boolean;
  /** End listening but deliver what was said (a final, or the clip's words)
   * before onEnd. */
  finish(): void;
  /** Drop everything; report nothing. */
  stop(): void;
  /** Give the microphone back. */
  release(): void;
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
    start(handlers) {
      self.stop();
      h = handlers;
      want = true;
      finishing = false;
      rapid = 0;
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
const RMS_SPEECH = 0.02; // RMS (0..1) above which a clip has speech
const UPLOAD_TIMEOUT_MS = 45_000;
const CLIP_RATE = 16_000; // the built-in recognizers' rate; whisper's too
/** A let-go phrase's last bit of audio under this many bytes (~50 ms) isn't sent. */
const TAIL_MIN_BYTES = 44 + 1600;

/** One phrase being recorded. `stream`: it streams (else it is a clip — clip
 * mode, or streaming fell back); `sid` the stream once open; `busy` the one
 * request in flight (the open, or a chunk); `fresh`: speech not sent yet. */
interface Phrase {
  rec: WavRecording | null;
  heard: boolean;
  stream: boolean;
  sid: string | null;
  busy: Promise<void> | null;
  fresh: boolean;
  ending: boolean;
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
 *    `server_ready.stt_stream`): at the first speech of a phrase POST
 *    /api/voice/stream opens a stream, and whenever the previous request is
 *    back and 300 ms (wavRecorder TAKE_MIN_S) of new audio exists, the recorder's take()
 *    goes to /stream/{id}/chunk — its `committed` words (final) go to
 *    onWords as they come, its `partial` (unstable) to onPartial. The same
 *    quiet ends the phrase: /end flushes its last words to onFlush, and the
 *    next phrase opens a new stream. No cap. A 409 on the open, or any
 *    failed request, falls back to clips for the rest of the dictation.
 * While listening is wanted the next phrase starts recording as soon as one
 * ends, so phrases arrive one by one like Chrome's finals. The mic stream is
 * kept until {@link Recognizer.release}. */
export function serverRecognizer(opts: { stream?: () => boolean } = {}): Recognizer & { lastError: string } {
  let stream: MediaStream | null = null;
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

  const getStream = async () => {
    if (stream && stream.active !== false) return stream;
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    return stream;
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

  /** At the phrase's first speech: open its stream (after the previous
   * phrase's /end). */
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
          open.add(id);
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
      const w = wordsOf(r.j.committed);
      if (w) hh.onWords?.(w);
      hh.onPartial?.(wordsOf(r.j.partial));
    } catch (err) {
      if (h === hh) streamFailed(c, err);
    }
  };

  /** The next chunk, when nothing is in flight and enough is new. */
  const pump = (c: Phrase) => {
    if (!c.stream || !c.sid || c.busy || c.ending || !c.rec || h === null) return;
    const blob = c.rec.take();
    if (!blob) return;
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
      if (!c.sid) return; // no speech: no stream was opened
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
        hh.onPartial?.("");
        hh.onFlush?.(wordsOf(r.j.committed));
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
    let s: MediaStream;
    try {
      s = await getStream();
    } catch (err) {
      if (token === mine) failed(errCode(err));
      return;
    }
    if (token !== mine) return;
    // The context the mic's click made and resumed (voiceAudioCtx): the
    // resume may still be in flight.
    const ctx = voiceAudioCtx();
    if (ctx && ctx.state === "suspended") await ctx.resume().catch(() => {});
    if (token !== mine) return;
    if (!want) {
      // finish() came between two phrases: nothing more to record.
      token = null;
      maybeEnd();
      return;
    }
    if (!ctx || ctx.state !== "running") {
      failed("start-failed", "The audio engine isn't running — click the mic again");
      return;
    }
    const c: Phrase = { rec: null, heard: false, stream: streaming, sid: null, busy: null, fresh: false, ending: false };
    // The silence detector: the recorder's RMS per block against a noise
    // floor that follows the quiet moments.
    let floor = 0;
    let lastLoud = 0;
    const onLevel = (rms: number) => {
      if (cur !== c) return;
      floor = floor === 0 ? rms : Math.min(rms, floor * 1.05 + 0.001);
      const now = Date.now();
      if (rms > Math.max(RMS_SPEECH, floor * 3)) {
        if (!c.heard) {
          h?.onSpeech?.();
          if (c.stream) openStream(c);
        }
        c.heard = true;
        c.fresh = true;
        lastLoud = now;
      } else if (!c.heard && c.stream) {
        // Before the first word, keep only a lead-in (under TAKE_MIN_S):
        // the stream is not sent the quiet before it.
        c.rec?.take();
      }
      if (c.heard && now - lastLoud > SILENCE_MS) endClip();
      else pump(c);
    };
    try {
      c.rec = startWavRecording(s, ctx, { targetRate: CLIP_RATE, onLevel });
    } catch {
      failed("start-failed");
      return;
    }
    cur = c;
    h?.onStatus?.("Listening…");
    if (!c.stream)
      capTimer = setTimeout(() => {
        if (cur === c) endClip();
      }, CLIP_MAX_MS);
  };

  const self = {
    name: "server" as const,
    lastError: "",
    available: () => typeof navigator !== "undefined" && !!navigator.mediaDevices?.getUserMedia && !!audioCtxCtor(),
    start(handlers: RecognizerHandlers) {
      self.stop();
      if (!self.available()) return false;
      h = handlers;
      want = true;
      finishing = false;
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
    },
    release() {
      self.stop();
      const s = stream;
      stream = null;
      s?.getTracks().forEach((t) => t.stop());
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
