/** Claude's question prompt (AskUserQuestion) in the ask pop-up: what to
 * post for each answer, in what order, and the flow's state across steps —
 * no DOM (components/dialogs/AskDialog draws it).
 *
 * `GET /dialog` reads it as kind "select": the active tab's `question`, its
 * `options` (a description each; a multi-select's `checked` boxes; "Type
 * something" kind `text`, "Chat about this" kind `chat`), the tabs as
 * `steps` (the last one, Submit, is the review: `review` holds the answers)
 * and the keys that move between them (`nav`). What each answer posts
 * (`selectPosts`, carried out by `runSelect`):
 *  - single-select: the option's digit — it selects AND moves on (to the next
 *    question, the review, or — one question alone — submits);
 *  - multi-select: digits TOGGLE (the dialog id holds across toggles, and
 *    the server does not count them as settling); Next posts the keys of
 *    every box the card ticked differently from the server, in order, in ONE
 *    post, then `nav.next`;
 *  - the review: "Submit answers" is its digit; Cancel its other one;
 *  - "Type something": its keys, a moment for the field to open, then
 *    `{text, keys: ["Enter"]}` — the id holds while it is typed into;
 *  - "Chat about this": its digit, then the words go to the session as a
 *    prompt (/send {text, dialog_safe: true});
 *  - Back: `nav.prev`; "Cancel the question": Escape (the whole tool).
 * Every post carries the option's own `keys` (never a bare digit: with the
 * cursor on the text row they read ["Up", "1"]). Every post of a sequence is
 * pinned to the dialog id the person looked at; a later post the server
 * refuses as changed or just answered (an older server re-ids or holds the
 * prompt after the first key) is sent once more unpinned — the first post
 * already proved the prompt was the one shown. After it the card reads the
 * dialog again and shows the next step without closing; a 409
 * dialog_changed is the normal next step here, not news, unless the new
 * prompt is another kind. */

import { instApi } from "../api/client";
import type { Dialog, DialogOption } from "../api/types";
import { isDialogAnswered, isDialogChanged } from "./flockActions";

/** Claude's question prompt, parsed (anything else draws the old way). */
export function isSelect(d: Dialog | null | undefined): boolean {
  return !!d && d.kind === "select" && d.parsed && d.options.length > 0;
}

export type SelectStage = "single" | "multi" | "review";

export function selectStage(d: Dialog): SelectStage {
  if (d.steps?.[d.step ?? -1]?.submit || (d.review && d.review.length)) return "review";
  return d.multi ? "multi" : "single";
}

/** A box the card can tick (a multi-select option the server says is one). */
export const isBox = (o: DialogOption) => typeof o.checked === "boolean";
/** An option that takes words, not a press: Type something, Chat about this. */
export const takesWords = (o: DialogOption) => o.kind === "text" || o.kind === "chat";

/** The ticks as the server has them. */
export function initialChecked(d: Dialog): Record<string, boolean> {
  const out: Record<string, boolean> = {};
  for (const o of d.options) if (isBox(o)) out[o.key] = !!o.checked;
  return out;
}

/** The card ticked something the server hasn't. */
export function dirty(d: Dialog, checked: Record<string, boolean>): boolean {
  return d.options.some((o) => isBox(o) && !!checked[o.key] !== !!o.checked);
}

export type SelectAct =
  | { type: "pick"; key: string }
  | { type: "next" }
  | { type: "prev" }
  | { type: "text"; key: string; text: string }
  | { type: "chat"; key: string; text: string }
  | { type: "cancel" };

export type SelectPost =
  /** POST /answer {keys[, text]} (pinned to the dialog id when first). */
  | { to: "answer"; keys: string[]; text?: string }
  /** Let the CLI redraw (a field opening) before the next post. */
  | { to: "settle" }
  /** POST /send {text, dialog_safe: true}: words for the session itself. */
  | { to: "send"; text: string };

const press = (o: DialogOption): SelectPost => ({ to: "answer", keys: o.keys?.length ? o.keys : [o.key] });

/** The posts that carry out `act` on `d`, with the card's `checked` ticks
 * (a multi-select's differences go out first, whatever the act — except
 * Cancel, which throws the lot away). [] = nothing to do. */
export function selectPosts(d: Dialog, act: SelectAct, checked: Record<string, boolean> = {}): SelectPost[] {
  if (act.type === "cancel") return [{ to: "answer", keys: ["Escape"] }];
  // A multi-select's changed boxes, as one post (their keys in order).
  const changed = d.multi ? d.options.filter((o) => isBox(o) && !!checked[o.key] !== !!o.checked) : [];
  const toggles: SelectPost[] = changed.length
    ? [{ to: "answer", keys: changed.flatMap((o) => (o.keys?.length ? o.keys : [o.key])) }]
    : [];
  const opt = "key" in act ? d.options.find((o) => o.key === act.key) : undefined;
  switch (act.type) {
    case "pick":
      return opt ? [...toggles, press(opt)] : [];
    case "next":
      return d.nav?.next?.length ? [...toggles, { to: "answer", keys: d.nav.next }] : toggles;
    case "prev":
      return d.nav?.prev?.length && (d.step ?? 0) > 0 ? [...toggles, { to: "answer", keys: d.nav.prev }] : [];
    case "text": {
      const text = act.text.trim();
      if (!opt || !text) return [];
      return [...toggles, press(opt), { to: "settle" }, { to: "answer", keys: ["Enter"], text }];
    }
    case "chat": {
      const text = act.text.trim();
      if (!opt || !text) return [];
      return [...toggles, press(opt), { to: "settle" }, { to: "send", text }];
    }
  }
}

/** Between two posts: the keys land one by one (never as one paste). */
export const SELECT_KEY_GAP_MS = 150;
/** After a digit that opens a field (or leaves the prompt), before the words. */
export const SELECT_SETTLE_MS = 400;
/** After the last post, before reading the next step. */
export const SELECT_REDRAW_MS = 350;
/** "Chat about this": how often a /send refused `in_dialog` (the prompt not
 * gone yet) is tried again. */
const SEND_RETRIES = 3;

export interface SelectDeps {
  /** POST /answer {keys, by: "user"[, dialog_id][, text]}. */
  answer(keys: string[], dialogId: string | null, text?: string): Promise<unknown>;
  /** POST /send {text, dialog_safe: true[, say]}. */
  send(text: string): Promise<unknown>;
  fetch(): Promise<Dialog | null>;
  sleep(ms: number): Promise<void>;
}

/** How a sequence went, and the dialog read after it (null: the prompt is
 * gone; undefined: it couldn't be read). */
export type SelectOutcome =
  | { kind: "done"; dialog: Dialog | null | undefined }
  /** The first post found another prompt up: nothing was pressed. */
  | { kind: "changed"; dialog: Dialog | null | undefined }
  /** Someone answered that very prompt first. */
  | { kind: "already"; dialog: Dialog | null | undefined }
  | { kind: "failed"; error: unknown };

const inDialog = (err: unknown) => {
  const e = err as { status?: number; body?: { in_dialog?: boolean } | null };
  return e?.status === 409 && !!e.body?.in_dialog;
};

/** Carry out `posts` on `d` in order, then read the dialog again. */
export async function runSelect(d: Dialog, posts: readonly SelectPost[], deps: SelectDeps): Promise<SelectOutcome> {
  const read = () => deps.fetch().catch(() => undefined);
  let first = true;
  for (const p of posts) {
    if (p.to === "settle") {
      await deps.sleep(SELECT_SETTLE_MS);
      continue;
    }
    if (!first) await deps.sleep(SELECT_KEY_GAP_MS);
    try {
      if (p.to === "answer")
        try {
          await deps.answer(p.keys, d.id, p.text);
        } catch (err) {
          if (first || !(isDialogChanged(err) || isDialogAnswered(err))) throw err;
          await deps.answer(p.keys, null, p.text);
        }
      else
        for (let tries = 0; ; tries++) {
          try {
            await deps.send(p.text);
            break;
          } catch (err) {
            if (!inDialog(err) || tries >= SEND_RETRIES) throw err;
            await deps.sleep(SELECT_SETTLE_MS * 2);
          }
        }
    } catch (err) {
      if (first && isDialogChanged(err)) return { kind: "changed", dialog: await read() };
      if (first && isDialogAnswered(err)) return { kind: "already", dialog: await read() };
      return { kind: "failed", error: err };
    }
    first = false;
  }
  await deps.sleep(SELECT_REDRAW_MS);
  return { kind: "done", dialog: await read() };
}

/** POST /answer as the user, pinned to `dialogId` when given. */
export function answerSelect(title: string, keys: string[], dialogId: string | null, text?: string) {
  const json: Record<string, unknown> = { keys, by: "user" };
  if (dialogId) json.dialog_id = dialogId;
  if (text) json.text = text;
  return instApi(title, "/answer", { json });
}

/** What a key does in the card showing select `d`: press an option, tick a
 * box, or open an option's text field. Never with a modifier, never while a
 * text box has the keyboard. */
export function selectKey(
  d: Dialog | null,
  e: Pick<KeyboardEvent, "key" | "ctrlKey" | "metaKey" | "altKey">,
  typing: boolean
): { type: "pick" | "toggle" | "field"; key: string } | null {
  if (!d || !isSelect(d) || typing || e.ctrlKey || e.metaKey || e.altKey || !/^[1-9]$/.test(e.key)) return null;
  const o = d.options.find((x) => x.key === e.key);
  if (!o) return null;
  if (takesWords(o)) return { type: "field", key: o.key };
  return { type: d.multi && isBox(o) ? "toggle" : "pick", key: o.key };
}

/** A spoken "type something" / "chat about this": which kind of field it opens. */
export function fieldPhrase(text: string): "text" | "chat" | null {
  const t = text.toLowerCase().replace(/[^a-z ]+/g, " ").replace(/\s+/g, " ").trim();
  if (/^(type|write) (something|my own|an answer|it)\b/.test(t) || t === "other") return "text";
  if (/^chat about (this|it)\b/.test(t)) return "chat";
  return null;
}

// --- the flow, per session --------------------------------------------------- //

/** A step posted or read this recently keeps the pop-up open on its session
 * even while the rows say otherwise (a poll behind between two steps). */
export const SELECT_GRACE_MS = 10_000;
/** While a step is shown, how often it is read again (a voice answer, the
 * pane, a redraw moved it). */
export const SELECT_POLL_MS = 2000;

export interface SelectFlow {
  /** The step shown. */
  dialog: Dialog;
  /** The card's ticks (a multi-select), by option key. */
  checked: Record<string, boolean>;
  /** The option whose text field is open ("" none), and its words. */
  field: string;
  text: string;
  busy: boolean;
  note: string;
  /** When it last posted or took a read (ms). */
  at: number;
  /** "done": the prompt is gone (answered); "handoff": another kind of
   * prompt is up now — the card draws that one the usual way. */
  ended: "" | "done" | "handoff";
}

function fresh(d: Dialog, now: number): SelectFlow {
  return { dialog: d, checked: initialChecked(d), field: "", text: "", busy: false, note: "", at: now, ended: "" };
}

/** Every session's select flow. Survives the panel being re-mounted (a new
 * step is a new ask key on the row), so ticks, a half-typed answer and a
 * sequence in flight are never lost to a re-render. */
export class SelectFlows {
  private map = new Map<string, SelectFlow>();
  private subs = new Set<() => void>();

  get(title: string): SelectFlow | null {
    return this.map.get(title) || null;
  }

  subscribe = (cb: () => void): (() => void) => {
    this.subs.add(cb);
    return () => {
      this.subs.delete(cb);
    };
  };

  private set(title: string, f: SelectFlow): void {
    this.map.set(title, f);
    this.subs.forEach((s) => s());
  }

  private patch(title: string, p: Partial<SelectFlow>): void {
    const f = this.map.get(title);
    if (f) this.set(title, { ...f, ...p });
  }

  /** A read of `title`'s prompt from elsewhere (the pop-up's first load),
   * taken at `readAt` (ms): starts a flow, or moves one to a newer step —
   * never over a sequence in flight or a newer read, never the prompt a
   * finished flow just answered. */
  offer(title: string, d: Dialog, readAt: number = Date.now()): void {
    if (!isSelect(d)) return;
    const f = this.map.get(title);
    if (f && (f.busy || readAt < f.at || f.dialog.id === d.id)) return;
    this.set(title, fresh(d, readAt));
  }

  /** The prompt as just read (a poll, or after a sequence). */
  took(title: string, d: Dialog | null | undefined, now: number = Date.now(), note = ""): void {
    const f = this.map.get(title);
    if (!f || d === undefined) {
      if (f) this.patch(title, { busy: false });
      return;
    }
    if (!d) return this.set(title, { ...f, busy: false, ended: "done", at: now });
    if (!isSelect(d)) return this.set(title, { ...f, busy: false, ended: "handoff", note, at: now });
    if (d.id === f.dialog.id) {
      // After a sequence: what it sent is the server's now. A poll: the
      // server's ticks may have moved (a voice answer); the card's own
      // unsent ticks stay.
      if (f.busy) return this.set(title, { ...fresh(d, now), note: f.note });
      const checked = dirty(f.dialog, f.checked) ? f.checked : initialChecked(d);
      return this.set(title, { ...f, dialog: d, checked, ended: "", at: now });
    }
    // Another step of the same prompt: shown as is, no "changed" (`note` is
    // only for a hand-off to another kind). The same step redrawn (another
    // id, a resize) keeps a field being typed in.
    const next = fresh(d, now);
    const was = f.field ? f.dialog.options.find((o) => o.key === f.field) : undefined;
    const same = was && d.step === f.dialog.step && d.options.some((o) => o.key === was.key && o.kind === was.kind);
    this.set(title, same && !f.busy ? { ...next, field: f.field, text: f.text } : next);
  }

  /** A poll's read, begun at `startedAt` (ms): dropped when a sequence ran
   * (or is running) since — its own read is newer. */
  polled(title: string, d: Dialog | null | undefined, startedAt: number, now: number = Date.now()): void {
    const f = this.map.get(title);
    if (!f || f.busy || f.ended || f.at > startedAt) return;
    this.took(title, d, now);
  }

  toggle(title: string, key: string): void {
    const f = this.map.get(title);
    if (!f || f.busy || f.ended) return;
    this.patch(title, { checked: { ...f.checked, [key]: !f.checked[key] }, note: "" });
  }

  openField(title: string, key: string): void {
    const f = this.map.get(title);
    if (f && !f.busy && f.field !== key) this.patch(title, { field: key, text: "" });
  }

  closeField(title: string): void {
    this.patch(title, { field: "", text: "" });
  }

  setText(title: string, text: string): void {
    this.patch(title, { text });
  }

  /** Carry out `act` on the shown step; resolves once the next one is read. */
  async act(title: string, act: SelectAct, deps: SelectDeps, now: () => number = Date.now): Promise<void> {
    const f = this.map.get(title);
    if (!f || f.busy || f.ended) return;
    const posts = selectPosts(f.dialog, act, f.checked);
    if (!posts.length) return;
    this.patch(title, { busy: true, note: "", at: now() });
    const out = await runSelect(f.dialog, posts, deps);
    if (out.kind === "failed") {
      const msg = (out.error as Error)?.message || String(out.error || "error");
      this.patch(title, { busy: false, note: "Couldn't answer: " + msg, at: now() });
      return;
    }
    // A changed prompt of the same kind is just the next step; only another
    // kind of prompt is news.
    this.took(title, out.dialog, now(), out.kind === "changed" ? "The prompt changed — this is the new one" : "");
  }

  /** Still walking `title` through its steps (see {@link SELECT_GRACE_MS}). */
  active(title: string, now: number = Date.now()): boolean {
    const f = this.map.get(title);
    return !!f && !f.ended && (f.busy || now - f.at < SELECT_GRACE_MS);
  }

  /** Forget the sessions no longer asking. */
  prune(asking: ReadonlySet<string>): void {
    let gone = false;
    for (const t of [...this.map.keys()])
      if (!asking.has(t) && !this.active(t)) {
        this.map.delete(t);
        gone = true;
      }
    if (gone) this.subs.forEach((s) => s());
  }
}

/** The page's one set of flows (the pop-up's panels share it). */
export const selectFlows = new SelectFlows();
