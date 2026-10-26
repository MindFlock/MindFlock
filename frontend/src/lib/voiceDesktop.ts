/** The desktop mic (components/TopBarMic.tsx): everything that is not DOM.
 *
 * The phone page (backend/web/static/mobile.js, its VOICE section) is the
 * behaviour this mirrors: phrases pile up in a draft, a trailing "over" sends,
 * "disregard" starts over, and the session's reply ends with a `Say:` line
 * that is read aloud. The desktop differs in three ways:
 *  - push to talk: a click (or a held button) starts and stops listening, and
 *    stopping SENDS what was heard — the mic is not on until you key it;
 *  - one phrase is first offered to the voice router (POST /api/voice/route),
 *    so "switch to the login fix" or "start a new session for…" act instead
 *    of being typed into the focused session;
 *  - replies are read aloud only when you turn that on (off by default), only
 *    for prompts THIS app sent by voice, once per turn, and always prefixed
 *    with the session's name — several sessions may be talking.
 *
 * Pieces: {@link parseRadio}, the prefs, {@link Dictation} (the push-to-talk
 * state machine), {@link ReplyTracker}, {@link SpeechQueue}, and the browser
 * adapters (Web Speech / MediaRecorder → server, speechSynthesis / server
 * audio) that the component wires together. */

import { api, apiRaw } from "../api/client";

// --- the radio words -------------------------------------------------------- //

export type RadioKind = "over" | "disregard" | "";

/** "fix the bug over" → {kind: "over", text: "fix the bug"}; "… disregard"
 * (or "disregard that") → {kind: "disregard"}; else {kind: "", text}. Case
 * and punctuation do not matter ("Over." from Safari); only the LAST word
 * counts — an "over" mid-sentence is a word, and "hangover" is not "over".
 * The regex is mobile.js `voiceDictParse`'s, so both surfaces hear alike. */
export function parseRadio(text: string): { kind: RadioKind; text: string } {
  const t = String(text || "").trim();
  const m = /^(.*?)[\s,.!?]*\b(over|disregard)(?:\s+(?:that|it|all|everything))?[.!?\s]*$/i.exec(t);
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

// --- the push-to-talk state machine ----------------------------------------- //

/** What a step of {@link Dictation} asks the component to do. */
export type DictOutcome =
  /** Send `text`; `phrases` is how many phrases made it (one → try the router). */
  | { kind: "send"; text: string; phrases: number }
  /** "disregard": the draft is empty again, listening goes on. */
  | { kind: "cleared" }
  /** Keep listening (a phrase was added, or "over" with nothing to send). */
  | { kind: "continue" }
  /** Listening ended with nothing heard. */
  | { kind: "stopped" }
  /** Escape / "cancel": the words are dropped. */
  | { kind: "dropped" }
  /** Listening ended on silence: the words stay, unsent. */
  | { kind: "kept"; text: string };

/** Two minutes without a word stops listening (the words are kept). */
export const DICT_IDLE_MS = 120_000;
/** A hold released mid-phrase waits this long for the phrase's final. */
export const HOLD_FINAL_WAIT_MS = 1500;
export const DICT_HINT = "Say “over” to send, “disregard” to start over";

/** The draft and its life: begin → heard… → finish / cancel / expire. Pure,
 * so toggle, hold, "over", "disregard" and Escape are pinned by tests. The
 * gestures map onto it the same way in both modes — a toggle's second click
 * and a hold's release are both {@link finish}. */
export class Dictation {
  listening = false;
  phrases: string[] = [];
  interim = "";

  /** Start listening. `keep`: the words a previous round left unsent. */
  begin(keep = ""): void {
    this.listening = true;
    this.phrases = keep.trim() ? [keep.trim()] : [];
    this.interim = "";
  }

  get text(): string {
    return this.phrases.join(" ").trim();
  }

  /** The draft as the caption shows it: the phrases, then the words in flight. */
  get shown(): string {
    return [this.text, this.interim.trim()].filter(Boolean).join(" ");
  }

  hearing(t: string): void {
    if (this.listening) this.interim = t;
  }

  /** One final phrase. */
  heard(phrase: string): DictOutcome {
    if (!this.listening) return { kind: "stopped" };
    this.interim = "";
    const p = parseRadio(phrase);
    if (p.kind === "disregard") {
      this.phrases = [];
      return { kind: "cleared" };
    }
    if (p.text) this.phrases.push(p.text);
    if (p.kind === "over" && this.text) return this.sendNow();
    return { kind: "continue" };
  }

  /** The toggle's second click, the hold's release: send what was heard. A
   * phrase still in flight (no final yet) counts — it was said. */
  finish(): DictOutcome {
    if (!this.listening) return { kind: "stopped" };
    const tail = this.interim.trim();
    this.interim = "";
    if (tail) {
      const p = parseRadio(tail);
      if (p.kind === "disregard") this.phrases = [];
      else if (p.text) this.phrases.push(p.text);
    }
    if (!this.text) {
      this.listening = false;
      return { kind: "stopped" };
    }
    return this.sendNow();
  }

  /** Escape, "cancel": drop it all. */
  cancel(): DictOutcome {
    const had = this.listening || !!this.text;
    this.listening = false;
    this.phrases = [];
    this.interim = "";
    return had ? { kind: "dropped" } : { kind: "stopped" };
  }

  /** {@link DICT_IDLE_MS} of silence: stop, keep the words, send nothing. */
  expire(): DictOutcome {
    this.listening = false;
    this.interim = "";
    return this.text ? { kind: "kept", text: this.text } : { kind: "stopped" };
  }

  private sendNow(): DictOutcome {
    const out: DictOutcome = { kind: "send", text: this.text, phrases: this.phrases.length };
    this.listening = false;
    this.phrases = [];
    return out;
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

/** Which replies may be read aloud: the turns of prompts THIS app sent by
 * voice with `say: true` — never a prompt typed at the keyboard or sent from
 * the phone — and each turn once (idle and turn_ended both report it). */
export class ReplyTracker {
  private pending = new Map<string, Pending>();

  /** A voice prompt went to `title`. `say` is what /send was told. */
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

/** The page's one AudioContext (server playback). Made on first use, which is
 * inside the mic's click — a context made outside a gesture stays suspended. */
let audioCtx: AudioContext | null = null;
export function voiceAudioCtx(): AudioContext | null {
  const AC =
    typeof window === "undefined"
      ? undefined
      : window.AudioContext || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
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

const SERVER_MIMES = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"];
const SILENCE_MS = 1800; // quiet after speech that ends a clip
const CLIP_MAX_MS = 12_000; // the hard cap on one clip
const LEVEL_MS = 100; // silence sampling period
const RMS_SPEECH = 0.02; // RMS (0..1) above which a clip has speech
const UPLOAD_TIMEOUT_MS = 45_000;

/** MediaRecorder → POST /api/voice/transcribe (mobile.js ServerSTT): one
 * clip per phrase, ended by {@link SILENCE_MS} of quiet after speech or the
 * cap, uploaded whole (no timeslice: short slices come back empty on Safari)
 * with its mime as the Content-Type; the response's `{text}` is the phrase.
 * While listening is wanted the next clip starts as soon as one is sent, so
 * phrases arrive one by one like Chrome's finals. The mic stream is kept
 * until {@link Recognizer.release}. */
export function serverRecognizer(): Recognizer & { lastError: string } {
  let stream: MediaStream | null = null;
  let h: RecognizerHandlers | null = null;
  let want = false;
  let finishing = false;
  let token: object | null = null; // one per clip; null = no clip
  let rec: MediaRecorder | null = null;
  let uploads = 0;
  let source: MediaStreamAudioSourceNode | null = null;
  let meterTimer: ReturnType<typeof setInterval> | undefined;
  let capTimer: ReturnType<typeof setTimeout> | undefined;
  const aborters = new Set<AbortController>();

  const pickMime = () => {
    if (typeof MediaRecorder === "undefined" || !MediaRecorder.isTypeSupported) return "";
    for (const m of SERVER_MIMES) {
      try {
        if (MediaRecorder.isTypeSupported(m)) return m;
      } catch {
        /* next */
      }
    }
    return "";
  };
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
  const meterStop = () => {
    clearInterval(meterTimer);
    meterTimer = undefined;
    try {
      source?.disconnect();
    } catch {
      /* gone */
    }
    source = null;
  };
  /** The recognizer is done when nothing is wanted, recording or uploading. */
  const maybeEnd = () => {
    if (!want && !token && uploads === 0 && h) {
      finishing = false;
      const hh = h;
      hh.onEnd();
    }
  };

  const upload = async (blob: Blob, mime: string) => {
    const hh = h;
    uploads++;
    const ac = new AbortController();
    aborters.add(ac);
    const timer = setTimeout(() => ac.abort(), UPLOAD_TIMEOUT_MS);
    const lang = String(navigator.language || "en").split(";")[0];
    try {
      const r = await apiRaw("/api/voice/transcribe?lang=" + encodeURIComponent(lang), {
        method: "POST",
        headers: { "Content-Type": mime },
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
            ? "Server recognition is off: Settings → Your phone → Voice"
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
    const r = rec;
    rec = null;
    clearTimeout(capTimer);
    meterStop();
    if (!r) return;
    try {
      r.stop();
    } catch {
      token = null;
    }
  };

  const clip = async () => {
    const mine = {};
    token = mine;
    let s: MediaStream;
    try {
      s = await getStream();
    } catch (err) {
      if (token !== mine) return;
      token = null;
      want = false;
      h?.onError(errCode(err));
      maybeEnd();
      return;
    }
    if (token !== mine) return;
    const mt = pickMime();
    let r: MediaRecorder;
    try {
      r = mt ? new MediaRecorder(s, { mimeType: mt }) : new MediaRecorder(s);
    } catch {
      token = null;
      want = false;
      h?.onError("start-failed");
      maybeEnd();
      return;
    }
    const mime = r.mimeType || mt || "audio/webm";
    const chunks: Blob[] = [];
    let heard = false;
    let live = false;
    r.ondataavailable = (ev) => {
      if (ev.data && ev.data.size) chunks.push(ev.data);
    };
    r.onstop = () => {
      if (token !== mine) return; // stop(): the clip is discarded
      token = null;
      const blob = new Blob(chunks, { type: mime });
      // A clip the detector heard nothing in has no words to send.
      if (blob.size && (heard || !live)) void upload(blob, mime);
      if (want && !finishing) void clip();
      else maybeEnd();
    };
    rec = r;
    try {
      r.start();
    } catch {
      token = null;
      rec = null;
      want = false;
      h?.onError("start-failed");
      maybeEnd();
      return;
    }
    h?.onStatus?.("Listening…");
    // The silence detector: RMS of the time-domain signal every LEVEL_MS
    // against a noise floor that follows the quiet moments.
    const ctx = voiceAudioCtx();
    let analyser: AnalyserNode | null = null;
    if (ctx && ctx.state === "running") {
      try {
        source = ctx.createMediaStreamSource(s);
        analyser = ctx.createAnalyser();
        analyser.fftSize = 1024;
        source.connect(analyser);
        live = true;
      } catch {
        source = null;
        analyser = null;
      }
    }
    if (analyser) {
      const buf = new Uint8Array(analyser.fftSize);
      let floor = 0;
      let lastLoud = 0;
      meterTimer = setInterval(() => {
        if (token !== mine || !analyser) return;
        analyser.getByteTimeDomainData(buf);
        let sum = 0;
        for (let i = 0; i < buf.length; i++) {
          const d = (buf[i] - 128) / 128;
          sum += d * d;
        }
        const rms = Math.sqrt(sum / buf.length);
        floor = floor === 0 ? rms : Math.min(rms, floor * 1.05 + 0.001);
        const now = Date.now();
        if (rms > Math.max(RMS_SPEECH, floor * 3)) {
          if (!heard) h?.onSpeech?.();
          heard = true;
          lastLoud = now;
        }
        if (heard && now - lastLoud > SILENCE_MS) endClip();
      }, LEVEL_MS);
    }
    capTimer = setTimeout(() => {
      if (token === mine) endClip();
    }, CLIP_MAX_MS);
  };

  const self = {
    name: "server" as const,
    lastError: "",
    available: () =>
      typeof navigator !== "undefined" &&
      !!navigator.mediaDevices?.getUserMedia &&
      typeof MediaRecorder !== "undefined",
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
      if (rec) {
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
      meterStop();
      const r = rec;
      rec = null;
      if (r) {
        try {
          r.ondataavailable = null;
          r.onstop = null;
          if (r.state !== "inactive") r.stop();
        } catch {
          /* gone */
        }
      }
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

export interface VoiceCfg {
  stt: "phone" | "server";
  tts: "phone" | "server";
  server_ready: { stt?: boolean; tts?: boolean; detail?: string; stt_detail?: string };
}
export const DEFAULT_VOICE_CFG: VoiceCfg = { stt: "phone", tts: "phone", server_ready: {} };

/** Where speech runs (Settings → Your phone → Voice). Unreachable / older
 * server: `prev` (a blink must not swap engines), else the browser's own. */
export async function loadVoiceCfg(prev: VoiceCfg | null): Promise<VoiceCfg> {
  try {
    const j = await api<Partial<VoiceCfg>>("/api/voice");
    if (j && typeof j === "object") {
      return {
        stt: j.stt === "server" ? "server" : "phone",
        tts: j.tts === "server" ? "server" : "phone",
        server_ready: j.server_ready && typeof j.server_ready === "object" ? j.server_ready : {},
      };
    }
  } catch {
    /* below */
  }
  return prev || DEFAULT_VOICE_CFG;
}

/** The recognizer to use: the server's when Settings say so AND it is ready
 * (and this browser can record), else the browser's. */
export function sttEngine(cfg: VoiceCfg, serverOk: boolean): "server" | "browser" {
  return cfg.stt === "server" && !!cfg.server_ready.stt && serverOk ? "server" : "browser";
}
export function ttsEngine(cfg: VoiceCfg): "server" | "browser" {
  return cfg.tts === "server" && !!cfg.server_ready.tts ? "server" : "browser";
}

/** The caption for a recognizer error, or "" for one that needs no words. */
export function recognizerErrorLine(code: string, detail?: string): string {
  if (code === "network")
    return "Speech recognition failed (network): use Chrome, Edge or Safari, or turn on server recognition in Settings → Your phone → Voice";
  if (code === "not-allowed" || code === "service-not-allowed") return "Microphone permission denied";
  if (code === "audio-capture") return "No microphone found";
  if (detail) return detail;
  if (code === "unavailable" || code === "start-failed") return "Speech recognition did not start (" + code + ")";
  return "Voice error: " + code;
}
