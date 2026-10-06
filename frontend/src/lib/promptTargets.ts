/** Where Customize → Prompts pastes a saved prompt.
 *
 * Saved prompts is the ONE place that puts text into sessions that are already
 * running (a template only ever starts a new one). So its target is a choice,
 * not just "the focused session": the focused one (the default, as before),
 * any other running session by the name its rail row shows, or — once there
 * are two or more — every running session at once.
 *
 * Pure: the panel hands in the instances snapshot it already polls and the
 * naming function, so this is testable without a DOM.
 */

import { isVerifySession } from "../components/dialogs/verify";

/** The select value that means "every running session". */
export const ALL_RUNNING = "*";

/** As much of an `/api/instances` row as targeting needs. */
export interface TargetRow {
  title?: string;
  status?: string;
  started?: boolean;
}

export interface PromptTarget {
  value: string;
  label: string;
}

/** "Running" = the filter the templates' old send row used: status running,
 * or a session that has started — minus a PAUSED one. Pausing keeps
 * `started` true but removes the tmux session (and usually the worktree), so
 * the old filter counted paused sessions into "All running (n)" and pasted
 * into them: /send either failed ("workspace no longer exists") or rebooted
 * an agent nobody asked for. Verify sessions are left out too — the rail
 * hides them, so a paste there could never be followed by your Enter. */
export function isRunningTarget(row: TargetRow | null | undefined): boolean {
  if (!row || !row.title) return false;
  if (isVerifySession(String(row.title))) return false;
  if (row.status === "paused") return false;
  return row.status === "running" || !!row.started;
}

/** The running sessions' titles, in rail order when the rail has published
 * one (rows it doesn't list keep their snapshot order, after it). */
export function runningTitles(rows: TargetRow[] | null | undefined, railOrder: string[] = []): string[] {
  const titles = (rows || []).filter(isRunningTarget).map((r) => String(r.title));
  const pos = (t: string) => {
    const i = railOrder.indexOf(t);
    return i < 0 ? Number.MAX_SAFE_INTEGER : i;
  };
  return titles
    .map((t, i) => ({ t, i }))
    .sort((a, b) => pos(a.t) - pos(b.t) || a.i - b.i)
    .map((x) => x.t);
}

/** The picker's options: the focused session first (labelled as today, even
 * if it is not running), then every other running session, then
 * "All running sessions (n)" when n ≥ 2. */
export function promptTargets(
  focused: string | null,
  running: string[],
  nameOf: (title: string) => string
): PromptTarget[] {
  const out: PromptTarget[] = [];
  if (focused) out.push({ value: focused, label: nameOf(focused) });
  for (const t of running) {
    if (t !== focused) out.push({ value: t, label: nameOf(t) });
  }
  if (running.length >= 2) {
    out.push({ value: ALL_RUNNING, label: `All running sessions (${running.length})` });
  }
  return out;
}

/** The value the picker shows: the user's pick while it is still offered,
 * else the focused session, else nothing ("" — no target). */
export function resolveTarget(
  picked: string | null,
  focused: string | null,
  options: PromptTarget[]
): string {
  if (picked && options.some((o) => o.value === picked)) return picked;
  return focused || "";
}

export interface PasteOutcome {
  ok: string[];
  failed: { title: string; error: string }[];
}

/** Paste into each title in parallel; never throws — every failure is
 * collected so the caller can report the partial result. */
export async function pasteIntoAll(
  titles: string[],
  send: (title: string) => Promise<unknown>
): Promise<PasteOutcome> {
  const settled = await Promise.allSettled(titles.map((t) => send(t)));
  const out: PasteOutcome = { ok: [], failed: [] };
  settled.forEach((r, i) => {
    if (r.status === "fulfilled") out.ok.push(titles[i]);
    else
      out.failed.push({
        title: titles[i],
        error: String((r.reason as Error)?.message || r.reason || "failed"),
      });
  });
  return out;
}

/** The toast after a fan-out paste: nothing has run yet, and says so. */
export function pastedAllToast(n: number): string {
  return n === 1
    ? "Pasted into 1 session — press Enter there to send"
    : `Pasted into ${n} sessions — press Enter in each to send`;
}
