/** MindFlock MCP, the human's side of a session family: the routes the rail,
 * the bell and the Thread tab call when YOU act on an agent's workers.
 *
 * Two kinds of action, and they never mix (docs: the MCP UX contract):
 *  - DIRECT — `/answer` presses a key in a dialog the agent is stuck on, as
 *    you (`by: "user"` counts as you being present, like /send does);
 *  - PASTE — a named prompt is rendered server-side and typed into the
 *    agent's input box with `/send {submit: false, dialog_safe: true}` (both
 *    re-check the agent live, so nothing lands in a dialog). Nothing runs
 *    until the user presses Enter, so the agent's own MCP tools (with their report-back,
 *    safe-delete and scope rules) do the actual merging and deleting.
 *
 * Normalizers never throw: a half-built response renders as "less", not a
 * crash — the same rule codemapApi.ts follows. */

import { instApi } from "../api/client";
import type { Dialog, DialogOption } from "../api/types";
import { useUi } from "../state/store";
import { pastePlaybook } from "./playbooks";

type Obj = Record<string, unknown>;
const obj = (v: unknown): Obj => (v && typeof v === "object" && !Array.isArray(v) ? (v as Obj) : {});
const str = (v: unknown, d = ""): string => (typeof v === "string" ? v : v == null ? d : String(v));

function normOption(v: unknown): DialogOption | null {
  const o = obj(v);
  const key = str(o.key).trim();
  // A key is what /answer sends; an option without one can't be pressed.
  if (!/^[1-9]$/.test(key)) return null;
  return { key, label: str(o.label).trim() || key, kind: str(o.kind, "other") };
}

/** Normalize a /dialog body. An unparsed dialog keeps its question but never
 * offers options, whatever the server sent: there is nothing safe to click. */
export function normDialog(v: unknown): Dialog {
  const o = obj(v);
  const parsed = o.parsed === true;
  const command = o.command == null ? null : str(o.command).trim() || null;
  const options = parsed && Array.isArray(o.options) ? o.options.map(normOption).filter((x): x is DialogOption => !!x) : [];
  return { id: str(o.id), parsed: parsed && options.length > 0, question: str(o.question).trim(), command, options };
}

/** The dialog `title`'s agent is waiting on, or null when it isn't on one.
 * Asked with `?quiet=1`: "not waiting" (the session left clarify between the
 * poll and this fetch — routine right after an answer) is a 204 with no
 * body, not a 409 every browser logs as a failed request. A 409 from an
 * older server still reads as no dialog. */
export async function fetchDialog(title: string, signal?: AbortSignal): Promise<Dialog | null> {
  try {
    const body = await instApi<unknown>(title, "/dialog?quiet=1", { signal });
    return body == null ? null : normDialog(body);
  } catch (err) {
    if ((err as { status?: number }).status === 409) return null;
    throw err;
  }
}

/** How long a fetched dialog is trusted for a click as it is. Older, the
 * strip reads the dialog again right before it answers: a resize or a
 * redraw since may have given the very same prompt another id. */
export const DIALOG_FRESH_MS = 3000;
/** How much of an option label two reads of one dialog must share — the
 * server's dialog id keeps the same prefix (dialogs.ID_PREFIX_CHARS), since
 * the CLI cuts a long label ("…in ~/.mindflock/work…") to the pane's width. */
const LABEL_PREFIX = 24;

const squash = (s: string | null | undefined) => (s ?? "").replace(/\s+/g, "");
const labelKey = (label: string) => squash(label.split("…")[0]).slice(0, LABEL_PREFIX);

/** Are `a` and `b` two reads of the SAME prompt, drawn differently (another
 * width, a redraw)? Same option keys, each pair of labels agreeing on their
 * width-safe prefix (one a prefix of the other: a redraw can leave stray
 * characters behind a short label), and the same command, whitespace aside
 * (re-wrapping moves only line breaks). A different command with the same
 * buttons is a different prompt. */
export function sameDialogShape(a: Dialog, b: Dialog): boolean {
  if (!a.parsed || !b.parsed || a.options.length !== b.options.length) return false;
  if (squash(a.command) !== squash(b.command)) return false;
  return a.options.every((o, i) => {
    const p = b.options[i];
    if (o.key !== p.key) return false;
    const x = labelKey(o.label);
    const y = labelKey(p.label);
    return !!x && !!y && (x.startsWith(y) || y.startsWith(x));
  });
}

/** What became of one click on a strip's option (see `answerFresh`). */
export type AnswerOutcome =
  /** The key went out, into `dialog` (the one shown, or a fresh read of it). */
  | { kind: "answered"; dialog: Dialog }
  /** Someone else answered that very prompt first (409 dialog_answered). */
  | { kind: "already" }
  /** The prompt on screen is another one (`dialog`), or none: nothing typed. */
  | { kind: "changed"; dialog: Dialog | null }
  | { kind: "failed"; error: unknown };

/** Press `key` in `shown` — the dialog the user clicked on, fetched at
 * `fetchedAt` (ms) — as the user, without ever typing into a prompt nobody
 * has seen:
 *  - a read older than DIALOG_FRESH_MS is refreshed first; a fresh read of
 *    the same prompt under another id (`sameDialogShape`) is answered under
 *    that id, a different prompt is handed back unanswered;
 *  - a 409 dialog_changed whose fresh read is the same prompt is retried
 *    ONCE with the fresh id (the pane was resized or redrawn between the
 *    read and the click) instead of showing "the prompt changed". */
export async function answerFresh(
  title: string,
  key: string,
  shown: Dialog,
  fetchedAt: number,
  now: number = Date.now()
): Promise<AnswerOutcome> {
  let target = shown;
  if (now - fetchedAt > DIALOG_FRESH_MS) {
    let fresh: Dialog | null | undefined;
    try {
      fresh = await fetchDialog(title);
    } catch {
      fresh = undefined; // unreadable: the server's own id check still guards
    }
    if (fresh === null) return { kind: "changed", dialog: null };
    if (fresh && fresh.id !== shown.id) {
      if (!sameDialogShape(shown, fresh)) return { kind: "changed", dialog: fresh };
      target = fresh;
    }
  }
  for (let attempt = 0; ; attempt++) {
    try {
      await answerDialog(title, key, target.id);
      return { kind: "answered", dialog: target };
    } catch (err) {
      if (isDialogAnswered(err)) return { kind: "already" };
      if (!isDialogChanged(err)) return { kind: "failed", error: err };
      const fresh = await fetchDialog(title).catch(() => null);
      if (attempt > 0 || !fresh || !sameDialogShape(shown, fresh)) return { kind: "changed", dialog: fresh };
      target = fresh;
    }
  }
}

/** Press `key` in the dialog `dialogId` names, as the user. The server
 * answers 409 `{dialog_changed: true}` when the prompt on screen is no longer
 * that one — the caller refetches instead of answering a question nobody saw. */
export function answerDialog(title: string, key: string, dialogId: string) {
  return instApi(title, "/answer", { json: { keys: [key], dialog_id: dialogId, by: "user" } });
}

/** Did an /answer fail because that very prompt was just answered (a second
 * strip, the orchestrator's answer_prompt)? The server holds one answer per
 * dialog for a few seconds — the CLI may not have redrawn yet. */
export function isDialogAnswered(err: unknown): boolean {
  const e = err as { status?: number; body?: unknown };
  return e?.status === 409 && obj(e.body).dialog_answered === true;
}

/** Did an /answer fail because the prompt changed under the click? */
export function isDialogChanged(err: unknown): boolean {
  const e = err as { status?: number; body?: unknown };
  return e?.status === 409 && obj(e.body).dialog_changed === true;
}

// --- UI entry points -------------------------------------------------------------

/** Wrap-ups in flight, per orchestrator: the chip is a one-click paste, and a
 * double-click must not type the prompt into the input box twice. */
const wrapping = new Set<string>();

/** The rail's "wrap up" chip: paste the Wrap up prompt into `title`'s input
 * and put the keyboard there, so the one thing left is Enter. */
export async function pasteWrapup(title: string): Promise<boolean> {
  if (!title || wrapping.has(title)) return false;
  wrapping.add(title);
  try {
    // The one paste path (lib/playbooks): render, type with submit:false, put
    // the keyboard in the terminal and say what's left (Enter).
    return await pastePlaybook(title, { id: "wrapup", label: "Wrap up workers" });
  } finally {
    wrapping.delete(title);
  }
}

/** Open `title`'s Thread tab (its family's reports and messages), with the
 * composer addressed to `composeTo` when given (and the caret in it). */
export function openThread(title: string, composeTo?: string) {
  useUi.getState().threadOpen(title, composeTo ? { composeTo } : undefined);
}
