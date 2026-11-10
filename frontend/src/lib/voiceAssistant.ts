/** The voice assistant's client: talking to MindFlock itself, not to a
 * coding session.
 *
 * A phrase for the assistant ("MindFlock, open intake", anything said in
 * assistant mode, the answer to its question) goes to POST
 * /api/voice/assistant with the conversation's id and a snapshot of what is
 * on screen; the reply is NDJSON — one event per line, acted on as it
 * arrives:
 *  - `act`: a {@link UiAction} the page performs at once (lib/assistantHost
 *    has the executors: dialogs, views, focus, palette commands, session
 *    actions, clicks, prefs, typing);
 *  - `did`: what the server did ("Started session fix-login"); a new
 *    session is focused;
 *  - `say` / `ask` / `confirm` / `error`: words, spoken by the talk policy
 *    ({@link speaksKind}) and captioned; after `ask` / `confirm` the next
 *    phrase goes back to the assistant ({@link AssistantSession.wantsNext}),
 *    a yes / no to a confirm as `confirm: {id, yes}` ({@link yesNo});
 *  - `done`: always last.
 *
 * The owner's rule: no spoken echoes of their own actions — `did` results
 * are a chime and a caption, never words (unless "chatty"). Opening
 * something ("MindFlock, open settings") is the exception (owner report
 * 2026-11-10): "Opening Settings." is said ({@link openingLine}) when the
 * page lets it ({@link AssistantDeps.confirm}: spoken replies or spoken
 * confirmations on), else a chime.
 *
 * Everything here is pure or takes its effects as deps, so vitest covers it
 * in node: the stream reader ({@link NdjsonSplitter}), the conversation
 * rollover ({@link Conversation}), the talk policy, yes/no, the mode's end
 * words, the turn ({@link AssistantSession}) and the view store the strip
 * renders from ({@link assistantView}). */

import { apiRaw } from "../api/client";

// --- the contract's shapes ------------------------------------------------------- //

/** What the client performs (`do` is the discriminator). */
export type UiAction =
  | { do: "open"; target: string }
  | { do: "close" }
  | { do: "view"; mode: "grid" | "sky" }
  | { do: "focus"; session: string }
  | { do: "command"; label: string }
  | { do: "session_action"; session: string; action: string }
  | { do: "click"; label: string }
  | { do: "pref"; key: string; value: unknown }
  | { do: "type"; text: string };

/** What the server performs. */
export type ServerAction =
  | { do: "new_session"; repo: string; prompt: string; program?: string; model?: string }
  | { do: "send"; session: string; text: string }
  | { do: "setting"; path: string; value: unknown }
  | { do: "status"; session?: string }
  | { do: "help"; question: string };

export type AssistantEvent =
  | { type: "act"; action: UiAction }
  | { type: "did"; action: ServerAction; ok: boolean; text: string; detail: Record<string, unknown> }
  | { type: "say"; text: string }
  | { type: "ask"; text: string }
  | { type: "confirm"; id: string; text: string; action: UiAction | ServerAction }
  | { type: "error"; text: string }
  | { type: "done"; ms: number; model: boolean };

export interface SnapshotPref {
  key: string;
  label: string;
  type: "bool" | "select" | "number";
  value: unknown;
  options?: string[];
}

/** What is on screen, sent with every turn (every field optional). */
export interface Snapshot {
  view?: "grid" | "sky";
  dialog?: string | null;
  settings_screen?: string | null;
  focused?: string | null;
  commands?: string[];
  clickables?: string[];
  prefs?: SnapshotPref[];
}

export const SNAPSHOT_COMMANDS_MAX = 300;
export const SNAPSHOT_CLICKABLES_MAX = 150;

export interface AssistantBody {
  text: string;
  conversation: string;
  snapshot: Snapshot;
  confirm?: { id: string; yes: boolean };
}

const UI_DOS = new Set(["open", "close", "view", "focus", "command", "session_action", "click", "pref", "type"]);
const SERVER_DOS = new Set(["new_session", "send", "setting", "status", "help"]);

const str = (v: unknown) => (typeof v === "string" ? v : "");
const obj = (v: unknown): Record<string, unknown> | null =>
  v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : null;

/** A UiAction from the wire, or null when it isn't one we know (or lacks
 * what its kind needs). */
export function parseUiAction(v: unknown): UiAction | null {
  const a = obj(v);
  if (!a || !UI_DOS.has(str(a.do))) return null;
  switch (a.do) {
    case "open":
      return str(a.target) ? { do: "open", target: str(a.target) } : null;
    case "close":
      return { do: "close" };
    case "view":
      return a.mode === "grid" || a.mode === "sky" ? { do: "view", mode: a.mode } : null;
    case "focus":
      return str(a.session) ? { do: "focus", session: str(a.session) } : null;
    case "command":
      return str(a.label) ? { do: "command", label: str(a.label) } : null;
    case "session_action":
      return str(a.session) && str(a.action) ? { do: "session_action", session: str(a.session), action: str(a.action) } : null;
    case "click":
      return str(a.label) ? { do: "click", label: str(a.label) } : null;
    case "pref":
      return str(a.key) ? { do: "pref", key: str(a.key), value: a.value } : null;
    case "type":
      return typeof a.text === "string" ? { do: "type", text: a.text } : null;
  }
  return null;
}

/** One wire line's event, or null (unknown type, bad shape: skipped). */
export function parseEvent(v: unknown): AssistantEvent | null {
  const e = obj(v);
  if (!e) return null;
  switch (e.type) {
    case "act": {
      const action = parseUiAction(e.action);
      return action ? { type: "act", action } : null;
    }
    case "did": {
      const a = obj(e.action);
      if (!a || !SERVER_DOS.has(str(a.do))) return null;
      return { type: "did", action: a as unknown as ServerAction, ok: e.ok !== false, text: str(e.text), detail: obj(e.detail) || {} };
    }
    case "say":
    case "ask":
    case "error":
      return { type: e.type, text: str(e.text) };
    case "confirm": {
      const a = obj(e.action);
      if (!str(e.id) || !a) return null;
      const action = UI_DOS.has(str(a.do)) ? parseUiAction(a) : SERVER_DOS.has(str(a.do)) ? (a as unknown as ServerAction) : null;
      return action ? { type: "confirm", id: str(e.id), text: str(e.text), action } : null;
    }
    case "done":
      return { type: "done", ms: Number(e.ms) || 0, model: !!e.model };
  }
  return null;
}

// --- the stream ------------------------------------------------------------------ //

/** NDJSON, fed in chunks as they arrive: a line split across chunks is
 * held until its newline; blank lines and lines that aren't JSON are
 * skipped (one bad line never ends the turn). */
export class NdjsonSplitter {
  private buf = "";
  /** The complete lines in `chunk` (with what was held), parsed. */
  push(chunk: string): unknown[] {
    this.buf += chunk;
    const out: unknown[] = [];
    let nl: number;
    while ((nl = this.buf.indexOf("\n")) >= 0) {
      const line = this.buf.slice(0, nl);
      this.buf = this.buf.slice(nl + 1);
      const v = parseLine(line);
      if (v !== undefined) out.push(v);
    }
    return out;
  }
  /** The stream ended: a last line without its newline. */
  end(): unknown[] {
    const line = this.buf;
    this.buf = "";
    const v = parseLine(line);
    return v === undefined ? [] : [v];
  }
}

function parseLine(line: string): unknown {
  const t = line.trim();
  if (!t) return undefined;
  try {
    return JSON.parse(t);
  } catch {
    return undefined;
  }
}

/** Every event of an NDJSON body, as its bytes arrive (UTF-8 split across
 * chunks is decoded whole). */
export async function* readEvents(body: ReadableStream<Uint8Array>): AsyncGenerator<AssistantEvent> {
  const reader = body.getReader();
  const dec = new TextDecoder();
  const split = new NdjsonSplitter();
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      for (const v of split.push(dec.decode(value, { stream: true }))) {
        const e = parseEvent(v);
        if (e) yield e;
      }
    }
    for (const v of split.push(dec.decode())) {
      const e = parseEvent(v);
      if (e) yield e;
    }
    for (const v of split.end()) {
      const e = parseEvent(v);
      if (e) yield e;
    }
  } finally {
    try {
      reader.releaseLock();
    } catch {
      /* already released */
    }
  }
}

export const ASSISTANT_FAILED_LINE = "MindFlock couldn't answer that.";

/** POST one turn, calling `on` for each event as it arrives. A failure (the
 * route missing on an older server, a 400, the network) is an `error`
 * event; a stream that ends without `done` gets one. Never throws (an abort
 * ends quietly). */
export async function streamAssistant(
  body: AssistantBody,
  on: (e: AssistantEvent) => void,
  opts: { signal?: AbortSignal; fetch?: (path: string, init: RequestInit) => Promise<Response> } = {}
): Promise<void> {
  const post = opts.fetch || apiRaw;
  const started = Date.now();
  let sawDone = false;
  const emit = (e: AssistantEvent) => {
    if (sawDone) return;
    if (e.type === "done") sawDone = true;
    on(e);
  };
  try {
    const r = await post("/api/voice/assistant", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: opts.signal,
    });
    if (!r.ok || !r.body) {
      const j = (await r.json().catch(() => null)) as { error?: string } | null;
      emit({ type: "error", text: (j && typeof j.error === "string" && j.error) || ASSISTANT_FAILED_LINE });
    } else for await (const e of readEvents(r.body)) emit(e);
  } catch (err) {
    if (opts.signal?.aborted || (err as Error)?.name === "AbortError") return;
    emit({ type: "error", text: ASSISTANT_FAILED_LINE });
  }
  emit({ type: "done", ms: Date.now() - started, model: false });
}

/** The warm-up is asked for at most this often (assistant mode on). */
export const WARM_EVERY_MS = 60_000;
let warmedAt = 0;

/** POST /api/voice/assistant/warm: the server starts its model process
 * before the first phrase. Fire and forget; throttled. */
export function warmAssistant(now = Date.now(), post: (path: string, init: RequestInit) => Promise<unknown> = apiRaw): boolean {
  if (now - warmedAt < WARM_EVERY_MS) return false;
  warmedAt = now;
  void Promise.resolve()
    .then(() => post("/api/voice/assistant/warm", { method: "POST" }))
    .catch(() => {});
  return true;
}
/** Tests: forget the last warm-up. */
export function resetWarm(): void {
  warmedAt = 0;
}

// --- the conversation ------------------------------------------------------------ //

/** A new conversation after this long without a turn. */
export const CONVERSATION_IDLE_MS = 2 * 60_000;

const randomId = () => {
  try {
    if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return crypto.randomUUID();
  } catch {
    /* fall through */
  }
  return "c" + Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
};

/** The client-made conversation id: the same one while turns keep coming,
 * a new one after {@link CONVERSATION_IDLE_MS} without a turn. */
export class Conversation {
  private current = "";
  private lastAt = 0;
  constructor(
    private now: () => number = Date.now,
    private makeId: () => string = randomId
  ) {}
  /** The id for a turn starting now (and the turn counts from now). */
  turn(): string {
    const t = this.now();
    if (!this.current || t - this.lastAt > CONVERSATION_IDLE_MS) this.current = this.makeId();
    this.lastAt = t;
    return this.current;
  }
  /** A turn's answer came: the idle clock runs from its end. */
  touch(): void {
    if (this.current) this.lastAt = this.now();
  }
  reset(): void {
    this.current = "";
    this.lastAt = 0;
  }
}

// --- the words ------------------------------------------------------------------- //

const norm = (t: string) =>
  String(t || "")
    .toLowerCase()
    .replace(/[’`]/g, "'")
    .replace(/[^\p{L}\p{N}' ]+/gu, " ")
    .replace(/\s+/g, " ")
    .trim();

const YES = ["yes", "yeah", "yep", "yup", "sure", "do it", "go ahead", "ok", "okay", "yes please", "please do", "confirm"];
const NO = ["no", "nope", "cancel", "never mind", "nevermind", "don't", "do not", "no thanks"];

/** A confirm's answer: true (yes), false (no), null (anything else — sent
 * as ordinary text, and the pending confirm is dropped). A leading
 * "MindFlock," and a trailing "please"/"thanks" are ignored. */
export function yesNo(text: string): boolean | null {
  const t = stripAddress(norm(text)).replace(/\s+(please|thanks|thank you)$/, "").trim();
  if (YES.includes(t)) return true;
  if (NO.includes(t)) return false;
  return null;
}

/** "MindFlock, …", "Hey MindFlock …", "Flock, …": the words after the
 * address (null: not addressed). The server's router does this for real
 * (kind `assistant`); in assistant mode the page strips a habit-born one. */
export function addressed(text: string): string | null {
  const m = /^\s*(?:(?:hey|ok|okay)[\s,]+)?(?:mind\s?flock|flock)\b[\s,.:!?-]*/i.exec(String(text || ""));
  if (!m) return null;
  return String(text).slice(m[0].length).trim();
}

const stripAddress = (t: string) => {
  const r = addressed(t);
  return r === null ? t : norm(r);
};

const ENDS = [
  "that's all", "thats all", "that is all", "that's it", "thats it", "that'll be all",
  "thanks", "thank you", "thanks mindflock", "thank you mindflock", "thanks that's all", "ok thanks", "okay thanks",
  "goodbye", "bye", "done", "i'm done", "we're done", "never mind", "nevermind", "stop listening",
];

/** Assistant mode ends on these (the router's stop words end it too: they
 * stop the mic). */
export function isAssistantEnd(text: string): boolean {
  const t = stripAddress(norm(text)).replace(/\s+(mind\s?flock|flock)$/, "").trim();
  return ENDS.includes(t);
}

/** Assistant mode ends after this long with no speech. */
export const ASSISTANT_IDLE_MS = 60_000;

// --- the talk policy ------------------------------------------------------------- //

export type TalkLevel = "quiet" | "brief" | "chatty";

/** `voice.assistant_talk` ("" = brief). */
export function talkLevel(v: unknown): TalkLevel {
  return v === "quiet" || v === "chatty" ? v : "brief";
}

/** `voice.assistant_talk` from a settings document (GET /api/settings). */
export function talkFromSettings(doc: unknown): TalkLevel {
  const v = obj(doc);
  return talkLevel(obj(v?.voice)?.assistant_talk);
}

/** Whether an event of `kind` is spoken at `level`:
 *  - quiet: only `ask` / `confirm` / `error`;
 *  - brief (default): also `say` answers;
 *  - chatty: also `did` results.
 * `act` (navigation, your own action) is never spoken: a chime + a caption. */
export function speaksKind(kind: AssistantEvent["type"], level: TalkLevel): boolean {
  if (kind === "ask" || kind === "confirm" || kind === "error") return true;
  if (kind === "say") return level !== "quiet";
  if (kind === "did") return level === "chatty";
  return false;
}

// --- the view (what the strip shows) ------------------------------------------- //

export type AssistantPhase = "off" | "listening" | "thinking" | "replying";

export interface AssistantChip {
  text: string;
  ok: boolean;
  /** A session to jump to (a new one, the one focused). */
  session?: string;
  /** A dialog to reopen. */
  open?: string;
}

export interface AssistantView {
  /** Assistant mode is on (every phrase goes to MindFlock). */
  mode: boolean;
  phase: AssistantPhase;
  /** The words heard so far (live), or the phrase being answered. */
  heard: string;
  /** The reply's caption (its words, or the error). */
  caption: string;
  error: boolean;
  chips: AssistantChip[];
  /** A question waits for the next phrase (Yes / No buttons for a confirm). */
  question: { kind: "ask" | "confirm"; text: string } | null;
  /** Bumped on every change worth showing: the strip's fade restarts. */
  seq: number;
}

export const EMPTY_VIEW: AssistantView = {
  mode: false,
  phase: "off",
  heard: "",
  caption: "",
  error: false,
  chips: [],
  question: null,
  seq: 0,
};

/** How long the strip stays after its last change (no question pending). */
export const STRIP_FADE_MS = 6000;

/** The one store the strip renders from (useSyncExternalStore). */
export class ViewStore {
  private v: AssistantView = EMPTY_VIEW;
  private subs = new Set<() => void>();
  get = (): AssistantView => this.v;
  subscribe = (fn: () => void): (() => void) => {
    this.subs.add(fn);
    return () => this.subs.delete(fn);
  };
  set(patch: Partial<AssistantView>): void {
    this.v = { ...this.v, ...patch, seq: this.v.seq + 1 };
    this.subs.forEach((s) => s());
  }
  /** Hidden (the fade), the mode kept. */
  clear(): void {
    this.set({ phase: this.v.mode ? "listening" : "off", heard: "", caption: "", error: false, chips: [], question: null });
  }
}

export const assistantView = new ViewStore();

/** The strip is on screen. */
export function stripVisible(v: AssistantView): boolean {
  return v.mode || v.phase !== "off" || !!v.caption || !!v.chips.length || !!v.question || !!v.heard;
}

/** What a phrase's listening state says — never ambiguous about where the
 * words go. */
export const LISTENING_LINE = "Talking to MindFlock";
export const THINKING_LINE = "Thinking…";

// --- a turn ---------------------------------------------------------------------- //

/** What an executor did: a chip ("Opened Intake"), or a failure to say. */
export interface ExecResult {
  ok: boolean;
  text: string;
  session?: string;
  open?: string;
}

export interface AssistantDeps {
  /** The turn's POST, as {@link streamAssistant}. */
  stream(body: AssistantBody, on: (e: AssistantEvent) => void, signal: AbortSignal): Promise<void>;
  snapshot(): Snapshot;
  /** Perform a UiAction (lib/assistantHost). */
  exec(a: UiAction): ExecResult | Promise<ExecResult>;
  /** Speak with the assistant's voice; resolves when said. */
  speak(text: string): void;
  /** The chime for a silent action. */
  chime(): void;
  /** Say `line` (an {@link openingLine}) as a spoken confirmation: true
   * when it will be said (then no chime), false when the page keeps quiet
   * (Settings → Voice: spoken replies and confirmations both off). */
  confirm?(line: string): boolean;
  /** Focus a session the server started. */
  focus(title: string): void;
  talk(): TalkLevel;
  /** It asked something (`ask` / `confirm`): the mic opens for the answer
   * once the question is said, if it isn't listening already. */
  asked?(kind: "ask" | "confirm"): void;
  now?(): number;
  view?: ViewStore;
  conversation?: Conversation;
}

/** What opening something says out loud — "Opening Settings.", "Opening
 * Voice settings.", "Opening Intake." — from the executor's chip ("Opened
 * Settings → Voice"); "" for any other action (said as a chime). */
export function openingLine(a: UiAction, r: ExecResult): string {
  if (a.do !== "open" || !r.ok) return "";
  const m = /^Opened\s+(.+)$/.exec(String(r.text || "").trim());
  if (!m) return "";
  const screen = /^Settings\s*→\s*(.+)$/.exec(m[1]);
  const what = (screen ? screen[1] + " settings" : m[1]).replace(/\s*\(.*\)$/, "").trim();
  return what ? "Opening " + what + "." : "";
}

/** How long a pending confirm is good for (the server holds it this long). */
export const CONFIRM_TTL_MS = 60_000;

/** The assistant's side of the conversation: one turn at a time (a new
 * phrase cuts the one in flight), the question pending, the view. */
export class AssistantSession {
  readonly conv: Conversation;
  readonly view: ViewStore;
  private pending: { kind: "ask" } | { kind: "confirm"; id: string } | null = null;
  private pendingAt = 0;
  private inflight: AbortController | null = null;
  private turnNo = 0;

  constructor(private deps: AssistantDeps) {
    this.conv = deps.conversation || new Conversation(deps.now || Date.now);
    this.view = deps.view || assistantView;
  }

  private now(): number {
    return this.deps.now ? this.deps.now() : Date.now();
  }

  /** A question is waiting: the next phrase goes to the assistant even
   * outside assistant mode. */
  wantsNext(): boolean {
    if (this.pending && this.now() - this.pendingAt > CONFIRM_TTL_MS) this.dropQuestion();
    return !!this.pending;
  }

  /** The pending confirm's id (null: none, or an ask). */
  confirmId(): string | null {
    return this.wantsNext() && this.pending?.kind === "confirm" ? this.pending.id : null;
  }

  dropQuestion(): void {
    this.pending = null;
    if (this.view.get().question) this.view.set({ question: null });
  }

  /** A turn is in flight. */
  get busy(): boolean {
    return !!this.inflight;
  }

  /** Cut the turn in flight (a new phrase, the mode ending). */
  cancel(): void {
    this.inflight?.abort();
    this.inflight = null;
  }

  /** Live words while the phrase is spoken (the strip's transcript). */
  hearing(text: string): void {
    this.view.set({ heard: text, phase: "listening" });
  }

  /** Assistant mode on / off (the strip's listening state). */
  setMode(on: boolean): void {
    if (!on) this.cancel();
    const v = this.view.get();
    this.view.set({ mode: on, phase: on ? "listening" : v.phase === "listening" ? "off" : v.phase });
  }

  /** A Yes / No button (or the words): answer the pending confirm. */
  answer(yes: boolean): Promise<void> {
    return this.turn(yes ? "yes" : "no");
  }

  /** One phrase to the assistant: resolves when its `done` came. */
  async turn(text: string): Promise<void> {
    const words = String(text || "").trim();
    if (!words) return;
    this.cancel();
    const my = ++this.turnNo;
    const ac = new AbortController();
    this.inflight = ac;
    const body: AssistantBody = { text: words, conversation: this.conv.turn(), snapshot: this.safeSnapshot() };
    const cid = this.confirmId();
    if (cid) {
      const yn = yesNo(words);
      if (yn !== null) body.confirm = { id: cid, yes: yn };
    }
    // Any phrase answers what was asked: a confirm's yes/no, an ask's answer
    // or something else entirely (the confirm dropped).
    this.pending = null;
    this.view.set({ phase: "thinking", heard: words, caption: "", error: false, chips: [], question: null });
    const level = this.deps.talk();
    let chimed = false;
    const chimeOnce = () => {
      if (chimed) return;
      chimed = true;
      this.deps.chime();
    };
    const mine = () => this.turnNo === my && !ac.signal.aborted;
    // Events act in arrival order; an executor that awaits keeps that order.
    let chain: Promise<void> = Promise.resolve();
    const on = (e: AssistantEvent) => {
      chain = chain.then(() => (mine() ? this.event(e, level, chimeOnce) : undefined)).catch(() => {});
    };
    try {
      await this.deps.stream(body, on, ac.signal);
      await chain;
    } finally {
      if (this.inflight === ac) this.inflight = null;
    }
    if (!mine()) return;
    this.conv.touch();
    const v = this.view.get();
    this.view.set({ phase: v.mode ? "listening" : "off", heard: v.question || v.caption || v.chips.length ? v.heard : "" });
  }

  private safeSnapshot(): Snapshot {
    try {
      return this.deps.snapshot();
    } catch {
      return {};
    }
  }

  private chip(c: AssistantChip): void {
    this.view.set({ chips: [...this.view.get().chips, c].slice(-4) });
  }

  private say(kind: AssistantEvent["type"], text: string, level: TalkLevel, chime: () => void): void {
    if (!text) return;
    if (speaksKind(kind, level)) this.deps.speak(text);
    else chime();
  }

  private async event(e: AssistantEvent, level: TalkLevel, chime: () => void): Promise<void> {
    switch (e.type) {
      case "act": {
        let r: ExecResult;
        try {
          r = await this.deps.exec(e.action);
        } catch (err) {
          r = { ok: false, text: "Couldn't do that: " + ((err as Error)?.message || "error") };
        }
        // Your own action: a chime and the chip — opening something says
        // so ("Opening Settings.") when the page speaks confirmations. A
        // failure is said (the screen doesn't show why), once, as the
        // caption (no chip repeating it).
        if (r.ok) {
          this.chip({ text: r.text, ok: r.ok, session: r.session, open: r.open });
          const line = openingLine(e.action, r);
          if (!(line && this.deps.confirm?.(line))) chime();
        } else {
          this.view.set({ caption: r.text, error: true, phase: "replying" });
          this.deps.speak(r.text);
        }
        return;
      }
      case "did": {
        const title = typeof e.detail.title === "string" ? e.detail.title : "";
        if (e.action.do === "new_session" && e.ok && title) this.deps.focus(title);
        const session = title || (e.action.do === "send" ? e.action.session : undefined);
        if (!e.ok) {
          // The server follows a failed action with an `error` event carrying
          // the sentence to say: caption it here, speak it there — once.
          this.view.set({ caption: e.text, error: true, phase: "replying" });
        } else {
          if (e.text) this.chip({ text: e.text, ok: e.ok, session });
          this.say("did", e.text, level, chime);
        }
        return;
      }
      case "say":
        this.view.set({ caption: e.text, error: false, phase: "replying" });
        this.say("say", e.text, level, chime);
        return;
      case "ask":
        this.pending = { kind: "ask" };
        this.pendingAt = this.now();
        this.view.set({ question: { kind: "ask", text: e.text }, caption: e.text, error: false, phase: "replying" });
        this.say("ask", e.text, level, chime);
        this.deps.asked?.("ask");
        return;
      case "confirm":
        this.pending = { kind: "confirm", id: e.id };
        this.pendingAt = this.now();
        this.view.set({ question: { kind: "confirm", text: e.text }, caption: e.text, error: false, phase: "replying" });
        this.say("confirm", e.text, level, chime);
        this.deps.asked?.("confirm");
        return;
      case "error":
        this.view.set({ caption: e.text, error: true, phase: "replying" });
        this.say("error", e.text, level, chime);
        return;
      case "done":
        return;
    }
  }
}
