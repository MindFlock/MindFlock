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
 *
 * Pieces: {@link parseRadio}, the prefs, {@link Dictation} (the pure
 * state machine), {@link LiveDictation} (one dictation driving a target: the
 * router, the dialog guard, the typing, the submit), {@link ReplyTracker},
 * {@link SpeechQueue}, and the browser adapters (Web Speech / a WAV clip →
 * server, speechSynthesis / server audio) that the component wires together.
 * Which engine runs is {@link sttEngine} / {@link ttsEngine}: in the desktop
 * app (Electron) the server's whenever it is ready, in a browser tab the
 * browser's unless Settings say "server". */

import { ApiError, api, apiRaw, instApi } from "../api/client";
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

// --- the dictation state machine ------------------------------------------- //

/** What a step of {@link Dictation} asks for. `text` to type is a phrase plus
 * its trailing space; `text` to erase is everything this dictation typed. */
export type DictOutcome =
  /** Type `text` now; listening goes on. */
  | { kind: "type"; text: string }
  /** "over": type `text` (if any), stop — the words stay in the field. */
  | { kind: "over"; text: string }
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

/** One dictation: begin → heard… → over / submit / cancel / expire, with a
 * record of what it typed (in order) so "disregard" can take exactly that
 * back. Pure, so the radio words, push to talk and Escape are pinned by
 * tests; {@link LiveDictation} carries the outcomes out. A toggle's second
 * click and a hold's release are both {@link finish}: "over". */
export class Dictation {
  listening = false;
  interim = "";
  /** What this dictation typed so far — only what really went in
   * ({@link wrote}), since a disregard takes it back character by character. */
  typed = "";

  begin(): void {
    this.listening = true;
    this.interim = "";
    this.typed = "";
  }

  /** The caption: what is in the field, then the words in flight. */
  get shown(): string {
    return [this.typed.trim(), this.interim.trim()].filter(Boolean).join(" ");
  }

  hearing(t: string): void {
    if (this.listening) this.interim = t;
  }

  /** `text` went into the field. */
  wrote(text: string): void {
    this.typed += text;
  }

  /** One final phrase. */
  heard(phrase: string): DictOutcome {
    if (!this.listening) return { kind: "stopped" };
    this.interim = "";
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
    const tail = this.interim.trim();
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
    this.interim = "";
    return { kind: "dropped", text: this.erase() };
  }

  /** {@link DICT_IDLE_MS} of silence, or the recognizer gave up: stop, the
   * words stay where they are. */
  expire(): DictOutcome {
    if (!this.listening) return { kind: "stopped" };
    this.listening = false;
    this.interim = "";
    return { kind: "over", text: "" };
  }

  /** Forget what was typed; returns it, for the target to take back. */
  private erase(): string {
    const text = this.typed;
    this.typed = "";
    this.interim = "";
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
  /** The caption under the mic; `sticky` stays until replaced. */
  caption(text: string, sticky?: boolean): void;
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
}

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

  constructor(
    private target: DictTarget | null,
    private hooks: LiveHooks
  ) {
    this.dict.begin();
  }

  /** Still taking words (false once over / submit / Escape / an error). */
  get active(): boolean {
    return !this.done;
  }

  hearing(t: string): void {
    if (this.done) return;
    this.dict.hearing(t);
    this.show();
  }

  heard(phrase: string): Promise<void> {
    return this.step(() => this.dict.heard(phrase));
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
    this.end(kept ? line + " — the words so far stay in the field" : line, true);
  }

  /** The running text, or what to say. */
  show(): void {
    if (this.done) return;
    const field = this.target?.kind === "field";
    this.hooks.caption(this.dict.shown || (field ? "Listening — the words go into the text box" : DICT_HINT), true);
  }

  private step(next: () => DictOutcome): Promise<void> {
    this.chain = this.chain.then(() => (this.done ? undefined : this.apply(next()))).catch(() => {});
    return this.chain;
  }

  private end(line: string | null, sticky = false): void {
    if (this.done) return;
    this.done = true;
    this.dict.listening = false;
    this.hooks.halt();
    if (line !== null) this.hooks.caption(line, sticky);
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
        if (await this.put(out.text)) this.show();
        return;
      case "erase":
        t?.erase(out.text);
        this.hooks.caption("Disregarded — go on", true);
        return;
      case "dropped":
        t?.erase(out.text);
        this.end(out.text ? "Disregarded" : "");
        return;
      case "over":
        if (out.text && !(await this.put(out.text))) return;
        this.end(t?.kind === "session" && this.dict.typed ? OVER_LINE : "");
        return;
      case "submit": {
        const typedNow = !!out.text;
        if (out.text && !(await this.put(out.text))) return;
        // A text box: Enter is never pressed for you there.
        if (!t || t.kind === "field") {
          this.end(t ? "" : "Click a session first");
          return;
        }
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

  /** Type `text`; a session's first phrase goes past the router and the
   * dialog guard first. False when it did not go in (and listening ended). */
  private async put(text: string): Promise<boolean> {
    const t = this.target;
    if (!this.dict.typed && t?.kind !== "field") {
      const words = text.trim();
      if (await this.hooks.route(words)) {
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
    | "condense";
  title?: string;
  name?: string;
  say?: string;
}

const ROUTE_KINDS = new Set<string>([
  "prompt", "switch", "new", "ambiguous", "no-session", "new-failed", "list",
  "needs", "status", "cancel", "stop", "off", "repeat", "condense",
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
  private lines: string[] = [];
  private speaking = false;
  private held = false;
  last = "";

  constructor(
    private engine: () => Speaker,
    private onSpeaking: (on: boolean) => void = () => {}
  ) {}

  say(line: string): void {
    const t = String(line || "").trim();
    if (!t) return;
    this.lines.push(t);
    this.pump();
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
    this.lines = [];
    if (this.speaking) this.engine().cancel();
  }

  private pump(): void {
    if (this.speaking || this.held || !this.lines.length) return;
    const line = this.lines.shift()!;
    this.last = line;
    this.speaking = true;
    this.onSpeaking(true);
    const done = () => {
      this.speaking = false;
      this.onSpeaking(false);
      this.pump();
    };
    this.engine().speak(line).then(done, done);
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

/** A WAV clip → POST /api/voice/transcribe (mobile.js ServerSTT): one clip
 * per phrase, recorded as 16 kHz PCM by lib/wavRecorder through the page's
 * one AudioContext (the built-in Windows / macOS recognizers take nothing
 * but PCM), ended by {@link SILENCE_MS} of quiet after speech or the cap,
 * and uploaded whole as `audio/wav`; the response's `{text}` is the phrase.
 * While listening is wanted the next clip starts as soon as one is sent, so
 * phrases arrive one by one like Chrome's finals. The mic stream is kept
 * until {@link Recognizer.release}. */
export function serverRecognizer(): Recognizer & { lastError: string } {
  let stream: MediaStream | null = null;
  let h: RecognizerHandlers | null = null;
  let want = false;
  let finishing = false;
  let token: object | null = null; // one per clip; null = no clip
  let cur: { rec: WavRecording | null; heard: boolean } | null = null;
  let uploads = 0;
  let capTimer: ReturnType<typeof setTimeout> | undefined;
  const aborters = new Set<AbortController>();

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
    const lang = String(navigator.language || "en").split(";")[0];
    try {
      const r = await apiRaw("/api/voice/transcribe?lang=" + encodeURIComponent(lang), {
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
      const why = (err as Error)?.name === "AbortError" ? "the server took too long" : String((err as Error)?.message || err);
      self.lastError = why;
      want = false;
      hh.onError("server", "Couldn't reach the server: " + why);
    } finally {
      clearTimeout(timer);
      aborters.delete(ac);
      uploads--;
    }
    if (h === hh) maybeEnd();
  };

  /** End the current clip and send it. */
  const endClip = () => {
    const c = cur;
    cur = null;
    clearTimeout(capTimer);
    if (!c?.rec) return;
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
    if (!ctx || ctx.state !== "running") {
      failed("start-failed", "The audio engine isn't running — click the mic again");
      return;
    }
    const c: { rec: WavRecording | null; heard: boolean } = { rec: null, heard: false };
    // The silence detector: the recorder's RMS per block against a noise
    // floor that follows the quiet moments.
    let floor = 0;
    let lastLoud = 0;
    const onLevel = (rms: number) => {
      if (cur !== c) return;
      floor = floor === 0 ? rms : Math.min(rms, floor * 1.05 + 0.001);
      const now = Date.now();
      if (rms > Math.max(RMS_SPEECH, floor * 3)) {
        if (!c.heard) h?.onSpeech?.();
        c.heard = true;
        lastLoud = now;
      }
      if (c.heard && now - lastLoud > SILENCE_MS) endClip();
    };
    try {
      c.rec = startWavRecording(s, ctx, { targetRate: CLIP_RATE, onLevel });
    } catch {
      failed("start-failed");
      return;
    }
    cur = c;
    h?.onStatus?.("Listening…");
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
  server_ready: {
    stt?: boolean;
    tts?: boolean;
    detail?: string;
    stt_backend?: string;
    stt_detail?: string;
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

/** How the server's recognizer is fed: the clip's rate, the resampler that
 * makes it, and the quiet that ends a phrase. */
function clipsLine(): string {
  return " — 16 kHz clips, " + resamplerName() + ", a phrase ends after " + SILENCE_MS / 1000 + " s of quiet";
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
      ? serverEngineName(ready.stt_backend, cfg.system) + clipsLine()
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
