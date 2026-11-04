/** Held offline (backend core/held.py): a session whose agent DIED — a
 * crash, the server restarting, the OOM killer — stays Offline until a person
 * presses Start. Nothing brings it back by itself (not the terminal socket,
 * not a send, not the queue, not the boot sweep), so a crash under memory
 * pressure can't refill memory and crash again.
 *
 * The words and the small decisions every surface shares live here: the
 * pane's placeholder (components/grid/HeldBody), the rail chip and the Sky
 * tile's chip ("Offline · Start"), the rail notice + Start all
 * (components/sidebar/OfflineBar) and the send paths' "Queued" toast. The
 * Start itself is lib/revive's `startSession` (POST
 * /api/instances/{title}/revive), which releases the hold on the server. */

import { ApiError } from "../api/client";
import type { Instance } from "../api/types";

/** The chip's words for a held session: the state, then the remedy. */
export const HELD_CHIP = "Offline · Start";

/** The second sentence of the pane's placeholder. */
export const HELD_TEXT = "It won't start by itself, so it uses no memory.";

/** A row the server holds offline, and that this device can Start: never a
 * remote device's row (its own server owns its hold), a paused one (Resume
 * is its way back) or one whose folder is gone. */
export function isHeldOffline(inst: Partial<Instance> | null | undefined): boolean {
  if (!inst || !inst.held_offline) return false;
  if (inst.device || inst.status === "paused" || inst.workspace_missing) return false;
  return true;
}

/** "Offline since the server restarted." — `reason` as the server sent it,
 * a plain "Offline since a crash." when it sent none. */
export function heldSince(reason: string | null | undefined): string {
  const r = String(reason || "").trim().replace(/[.\s]+$/, "");
  return "Offline since " + (r || "a crash") + ".";
}

/** The pane placeholder's sentence: why it is down, then why it stays down. */
export function heldLine(reason: string | null | undefined): string {
  return heldSince(reason) + " " + HELD_TEXT;
}

/** The chip's tooltip (rail row, Sky tile). */
export function heldHint(reason: string | null | undefined): string {
  return heldSince(reason) + " " + HELD_TEXT + " Press Start to bring it back.";
}

/** A /send the server refused because the session is held: 409 with
 * `{"error": HELD_REFUSED, "held": true}`. The prompt is QUEUED on the
 * server and sent after Start — so it is not a failure to toast as one. */
export function isHeldRefusal(err: unknown): boolean {
  if (!(err instanceof ApiError) || err.status !== 409) return false;
  const b = err.body as { held?: unknown } | null;
  return !!b && typeof b === "object" && b.held === true;
}

/** The toast for a send to a held session. */
export function heldQueuedText(name: string): string {
  return "Queued — " + name + " is offline; press Start to send it.";
}

/** The rows the rail's notice counts (this device's held sessions). */
export function heldRows(instances: Instance[]): Instance[] {
  return instances.filter((i) => !i.pending && !i.sleeping && isHeldOffline(i));
}

/** The rail notice's sentence for `n` held sessions (n ≥ 1). */
export function heldNoticeText(n: number): string {
  const who = n === 1 ? "1 session stayed" : n + " sessions stayed";
  const them = n === 1 ? "it uses" : "they use";
  return who + " offline after a crash, so " + them + " no memory. Start the ones you need.";
}

/** What a dismissed notice remembers: WHICH sessions it was about. A new
 * session held later (a second crash) brings the notice back; the same set
 * shrinking as you Start them one by one does not. */
export function heldNoticeKey(rows: Pick<Instance, "title">[]): string {
  return rows
    .map((r) => r.title)
    .sort()
    .join("\n");
}

/** Whether a notice dismissed for `dismissed` stays hidden for `current`:
 * every session it is about now was already in the dismissed set. */
export function heldNoticeHidden(dismissed: string | null | undefined, current: string): boolean {
  if (!dismissed || !current) return !current;
  const seen = new Set(dismissed.split("\n"));
  return current.split("\n").every((t) => seen.has(t));
}
