/** "A session is asking you something" — the one reading of who is asking
 * what, for the pop-up (components/dialogs/AskDialog), the bell, the rail and
 * the toasts.
 *
 * A session asks in one of two ways (the row's `asking`, backend ask-signal):
 *  - `dialog`: its CLI shows a permission / choice prompt (activity
 *    "clarify") — the options come from GET /dialog, answered with /answer;
 *  - `question`: its last reply ENDED with a question in plain words and
 *    nobody has typed to it since — answered by typing (POST /send).
 * An older server sends no `asking`: a clarify row still reads as a dialog.
 *
 * The pop-up opens by itself when an ask appears, except (`askSuppressed`):
 *  - the setting `notifications.ask_popup` is off;
 *  - that very ask was answered from here, put off with "Later", or opened in
 *    its pane — one ask pops up once, never again after you dealt with it;
 *  - it is the focused session and you typed into it in the last
 *    TYPED_RECENTLY_MS — you are already handling it, in its pane;
 *  - another dialog is open (it waits for that one to close, never on top).
 * Several asking at once share ONE pop-up, stepped "1 of N", in the bell's
 * order: every ask is an answer the bell lists first, so by time — the one
 * waiting longest first. */

import { useQuery } from "@tanstack/react-query";
import { api, instApi } from "../api/client";
import type { Dialog, Instance, OutboxWaiting } from "../api/types";
import { queryClient } from "../state/queries";
import { useUi } from "../state/store";
import { effectiveActivity } from "./sessionState";

export type AskKind = "dialog" | "question";

/** The row's `asking` (and a clarify row's stand-in on an older server). */
export interface Asking {
  kind: AskKind;
  /** The question (prose: ≤300 chars; a dialog: often null — read /dialog). */
  text: string | null;
  /** Epoch seconds the ask began. */
  ts: number;
  dialog_id: string | null;
  parsed: boolean;
  source: string | null;
}

/** One session asking, as the pop-up steps through them. */
export interface AskItem {
  title: string;
  kind: AskKind;
  text: string;
  ts: number;
  dialogId: string | null;
  /** This ask's identity: a new ask on the same session has a new key. */
  key: string;
}

/** You typed into the focused pane this recently: you are handling it. */
export const TYPED_RECENTLY_MS = 10_000;
/** An ask announced by an event before the rows poll catches up is trusted
 * this long without a row behind it. */
export const EVENT_ASK_GRACE_MS = 8_000;

const num = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : Number(v) || 0);
const strOrNull = (v: unknown) => (typeof v === "string" && v.trim() ? v.trim() : null);

/** Normalize a row's `asking` (never throws; anything malformed is null). */
export function normAsking(v: unknown): Asking | null {
  if (!v || typeof v !== "object") return null;
  const o = v as Record<string, unknown>;
  const kind = o.kind === "dialog" || o.kind === "question" ? o.kind : null;
  if (!kind) return null;
  return {
    kind,
    text: strOrNull(o.text),
    ts: num(o.ts),
    dialog_id: strOrNull(o.dialog_id),
    parsed: o.parsed === true,
    source: strOrNull(o.source),
  };
}

/** What `inst` is asking right now, or null. A dialog needs the live activity
 * to be clarify (the debounced one every surface reads); a prose question
 * never outlives the agent starting to work again. */
export function askingOf(inst: Partial<Instance>, activity: string = effectiveActivity(inst)): Asking | null {
  if (!inst || inst.workspace_missing || inst.status === "paused" || inst.hidden_by) return null;
  const a = normAsking((inst as { asking?: unknown }).asking);
  if (activity === "clarify") {
    if (a && a.kind === "dialog") return a;
    return {
      kind: "dialog",
      text: null,
      ts: num(inst.activity_since),
      dialog_id: null,
      parsed: false,
      source: null,
    };
  }
  if (a && a.kind === "question" && activity !== "working" && activity !== "offline" && activity !== "limit") return a;
  return null;
}

/** The ask's identity (see AskItem.key). */
export function askKey(kind: AskKind, ts: number, dialogId: string | null): string {
  return kind === "question" ? "q:" + ts : "d:" + (dialogId || "") + "@" + ts;
}

function itemOf(title: string, a: Asking): AskItem {
  return { title, kind: a.kind, text: a.text || "", ts: a.ts, dialogId: a.dialog_id, key: askKey(a.kind, a.ts, a.dialog_id) };
}

/** An ask an event announced (`session.question`, activity → clarify) that
 * the rows may not show yet. */
export interface EventAsk {
  title: string;
  kind: AskKind;
  text: string;
  ts: number;
  /** When it arrived (ms), for EVENT_ASK_GRACE_MS. */
  at: number;
}

/** Every session asking, the pop-up's order: by when it began (the longest
 * waiting first), title as the tie-break. The rows first; then the outbox's
 * waiting `question` rows and the events for a session whose row doesn't
 * show its ask yet (a poll behind). */
export function askQueue(
  instances: readonly Instance[],
  waiting?: readonly OutboxWaiting[] | null,
  events?: readonly EventAsk[] | null,
  opts: { now?: number; activity?: (inst: Instance) => string } = {}
): AskItem[] {
  const now = opts.now ?? Date.now();
  const act = opts.activity ?? ((i: Instance) => effectiveActivity(i));
  const out = new Map<string, AskItem>();
  const rows = new Map<string, Instance>();
  for (const inst of instances || []) {
    rows.set(inst.title, inst);
    const a = askingOf(inst, act(inst));
    if (a) out.set(inst.title, itemOf(inst.title, a));
  }
  // A row that says "working" (or is gone) overrules anything older: the
  // agent moved on.
  const movedOn = (title: string) => {
    const r = rows.get(title);
    return !r || act(r) === "working" || !!r.workspace_missing || r.status === "paused" || !!r.hidden_by;
  };
  for (const w of waiting || []) {
    if (w.kind !== "question" || !w.title || out.has(w.title) || movedOn(w.title)) continue;
    const ts = num(w.since);
    out.set(w.title, { title: w.title, kind: "question", text: String(w.text || ""), ts, dialogId: null, key: askKey("question", ts, null) });
  }
  for (const e of events || []) {
    if (out.has(e.title) || now - e.at > EVENT_ASK_GRACE_MS || movedOn(e.title)) continue;
    out.set(e.title, { title: e.title, kind: e.kind, text: e.text, ts: e.ts, dialogId: null, key: askKey(e.kind, e.ts, null) });
  }
  return [...out.values()].sort((a, b) => a.ts - b.ts || a.title.localeCompare(b.title));
}

// --- What you already did about an ask ------------------------------------------ //

/** Asks dealt with here, by session: answered, put off ("Later"), opened in
 * the pane, or left to the pane you were typing in. Keyed by the ask's key;
 * a dialog put off is also remembered by its dialog id ("until that id
 * changes" — a redraw that re-stamps the ask keeps it snoozed). */
export class AskMemory {
  private done = new Map<string, Set<string>>();
  private snoozedDialog = new Map<string, string>();

  /** `how` is informational (tests, debugging); every way counts the same. */
  handle(item: Pick<AskItem, "title" | "key" | "kind" | "dialogId">, _how: "answered" | "later" | "pane" | "typing" = "answered", dialogId?: string | null): void {
    let s = this.done.get(item.title);
    if (!s) this.done.set(item.title, (s = new Set()));
    s.add(item.key);
    const id = dialogId || item.dialogId;
    if (_how === "later" && item.kind === "dialog" && id) this.snoozedDialog.set(item.title, id);
  }

  handled(item: Pick<AskItem, "title" | "key" | "kind" | "dialogId">): boolean {
    if (this.done.get(item.title)?.has(item.key)) return true;
    const snoozed = this.snoozedDialog.get(item.title);
    return item.kind === "dialog" && !!snoozed && !!item.dialogId && snoozed === item.dialogId;
  }

  /** You asked for it (the bell's Reply, a toast, a notification): every
   * memory of that session's asks goes, so it shows. */
  forget(title: string): void {
    this.done.delete(title);
    this.snoozedDialog.delete(title);
  }

  /** Drop what is remembered for sessions no longer asking (keeps it small;
   * a later ask has a new key anyway). */
  prune(asking: ReadonlySet<string>): void {
    for (const t of [...this.done.keys()]) if (!asking.has(t)) this.done.delete(t);
    for (const t of [...this.snoozedDialog.keys()]) if (!asking.has(t)) this.snoozedDialog.delete(t);
  }
}

/** The page's one memory (the pop-up, the bell's Reply and the toasts share it). */
export const askMemory = new AskMemory();

/** Why the pop-up must NOT open by itself for `item` now ("" = it may).
 * `"wait"`-type reasons (another dialog) leave it for later; the others are
 * final for that ask. */
export function askSuppressed(
  item: AskItem,
  ctx: {
    popupOn: boolean;
    memory: Pick<AskMemory, "handled">;
    focused: string | null;
    /** When the person last typed into `item.title`'s terminal (ms; 0 never). */
    typedAt: number;
    /** A dialog other than the pop-up is open. */
    otherDialog: boolean;
    now?: number;
  }
): "" | "off" | "handled" | "typing" | "other-dialog" {
  if (!ctx.popupOn) return "off";
  if (ctx.memory.handled(item)) return "handled";
  const now = ctx.now ?? Date.now();
  if (ctx.focused === item.title && ctx.typedAt && now - ctx.typedAt < TYPED_RECENTLY_MS) return "typing";
  if (ctx.otherDialog) return "other-dialog";
  return "";
}

/** Step `delta` through the queue from `current` (wrapping); the first when
 * `current` isn't in it; null when the queue is empty. */
export function stepAsk(queue: readonly AskItem[], current: string | null, delta: number): string | null {
  if (!queue.length) return null;
  const at = current ? queue.findIndex((q) => q.title === current) : -1;
  if (at < 0) return queue[0].title;
  return queue[(at + (delta < 0 ? -1 : 1) + queue.length) % queue.length].title;
}

/** After answering (or putting off) `current`: the next one still asking,
 * else null (close). `queue` is the queue without what was just dealt with. */
export function nextAfter(queue: readonly AskItem[], current: string, before: readonly string[]): string | null {
  if (!queue.length) return null;
  // The one that followed it in the order shown, if it still asks.
  const at = before.indexOf(current);
  for (let i = 1; at >= 0 && i < before.length; i++) {
    const t = before[(at + i) % before.length];
    if (t !== current && queue.some((q) => q.title === t)) return t;
  }
  const other = queue.find((q) => q.title !== current);
  return other ? other.title : null;
}

// --- The kind, in words ------------------------------------------------------------ //

/** The pop-up's kind label, from the dialog's own heading when the server
 * sends one, else from what it asks: Plan approval · Permission · Question. */
export function askKindLabel(kind: AskKind, d?: Pick<Dialog, "question" | "command" | "options"> & { heading?: string | null } | null): string {
  if (kind === "question") return "Question";
  if (!d) return "Permission";
  const heading = String(d.heading || "");
  const words = [heading, d.question, ...d.options.map((o) => o.label)].join(" ");
  if (/\bplan\b/i.test(words)) return "Plan approval";
  if (/permission/i.test(heading)) return "Permission";
  if (d.command || d.options.some((o) => o.kind === "yes" || o.kind === "always")) return "Permission";
  return "Question";
}

// --- The setting ------------------------------------------------------------------- //

/** The notification settings view (GET /api/notify/ntfy) — only the field
 * the pop-up reads. Shared with Settings → Notifications' switch. */
export interface AskSettingsView {
  ask_popup?: boolean;
}

export const NOTIFY_VIEW_KEY = ["notify-view"] as const;

export function useAskPopup(): boolean {
  const q = useQuery({
    queryKey: NOTIFY_VIEW_KEY,
    queryFn: () => api<AskSettingsView>("/api/notify/ntfy"),
    staleTime: 60_000,
    refetchInterval: 300_000,
    retry: false,
  });
  return q.data?.ask_popup !== false;
}

/** The setting as last read (default on: an older server doesn't send it). */
export function askPopupOn(): boolean {
  return queryClient.getQueryData<AskSettingsView>(NOTIFY_VIEW_KEY)?.ask_popup !== false;
}

/** Save the setting the way the ntfy fields are written (POST the patch to
 * the same view); the echo goes into the shared cache. */
export async function saveAskPopup(on: boolean): Promise<boolean> {
  const v = await api<AskSettingsView>("/api/notify/ntfy", { json: { ask_popup: on } });
  queryClient.setQueryData(NOTIFY_VIEW_KEY, v);
  return v?.ask_popup !== false;
}

// --- The server calls ------------------------------------------------------------- //

/** "Later" on a prose question: the server stops reporting it. */
export function dismissQuestion(title: string) {
  return instApi(title, "/asking/dismiss", { json: {} });
}

/** Answer a prose question by typing it: POST /send {text, enter: true},
 * dialog-safe on the server (409 `in_dialog` when a dialog came up). */
export function sendAnswer(title: string, text: string, say = false) {
  return instApi<{ queued?: boolean }>(title, "/send", { json: say ? { text, enter: true, say: true } : { text, enter: true } });
}

// --- The pop-up's door -------------------------------------------------------------- //

/** Open the pop-up on `title` (the bell's Reply, a toast or a notification):
 * what you put off on that session shows again. */
export function openAsk(title: string): void {
  if (!title) return;
  askMemory.forget(title);
  useUi.getState().openDialogFor("ask", title);
}
