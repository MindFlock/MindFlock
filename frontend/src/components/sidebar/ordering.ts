/** Sidebar ordering + filter + needs-attention model (ports of app.js
 * sections 9's ordered()/_matchesFilter()/attentionItems()). */

import type { Instance } from "../../api/types";
import { relTime } from "../../lib/format";
import { effectiveActivity } from "../../lib/stage";

/** Stable user order first (drag order), then unlisted instances in server
 * order. Returns the reconciled order for persistence alongside the rows.
 * A transient empty list must not rewrite the saved order. */
export function orderedInstances(
  instances: Instance[],
  order: string[]
): { rows: Instance[]; nextOrder: string[] } {
  if (!instances.length) return { rows: [], nextOrder: order };
  const byTitle = new Map(instances.map((i) => [i.title, i]));
  const rows: Instance[] = [];
  for (const t of order) {
    const inst = byTitle.get(t);
    if (inst) {
      rows.push(inst);
      byTitle.delete(t);
    }
  }
  for (const i of instances) {
    if (byTitle.has(i.title)) {
      rows.push(i);
      byTitle.delete(i.title);
    }
  }
  return { rows, nextOrder: rows.map((i) => i.title) };
}

/** Slot `title` directly beneath `after` in a materialized order.
 *
 * This is what makes a duplicated window land under the one it was copied
 * from. Without it a copy is simply a session the saved order has never seen,
 * so `orderedInstances` files it after everything else — at the bottom of a
 * rail of twelve, nowhere near the window you were looking at.
 *
 * If `after` isn't in the order (its session closed while the copy was being
 * provisioned) the order is returned untouched, which leaves the newcomer
 * wherever it already was rather than teleporting it somewhere arbitrary. */
export function orderWithAfter(order: string[], title: string, after: string): string[] {
  if (!title || !after || title === after) return order;
  const next = order.filter((t) => t !== title);
  const at = next.indexOf(after);
  if (at < 0) return order;
  next.splice(at + 1, 0, title);
  return next;
}

export const SEARCH_MIN = 6;

/** Match only the session's own identifiers — name, alias, branch. NOT repo
 * or path (every worktree of one repo shares those). */
export function matchesFilter(
  inst: Instance,
  filter: string,
  aliases: Record<string, string>
): boolean {
  if (!filter) return true;
  const hay = [inst.title, aliases[inst.title], inst.branch]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  return hay.indexOf(filter) >= 0;
}

/** Idle-with-unfinished-work threshold before a session counts as wedged. */
const WEDGE_IDLE_S = 20 * 60;

export interface AttentionItem {
  p: number;
  title: string;
  reason: string;
  snippet?: unknown;
}

/** O1: the prioritized "which session needs me" list (bell popover + mobile).
 * 0 waiting on your answer · 1 broken · 2 checks failing · 3 ready to move. */
export function attentionItems(instances: Instance[]): AttentionItem[] {
  const items: AttentionItem[] = [];
  for (const inst of instances || []) {
    if (inst.workspace_missing || inst.status === "paused") continue;
    const act = effectiveActivity(inst);
    if (act === "clarify")
      items.push({ p: 0, title: inst.title, reason: "needs your answer", snippet: inst.last_turn || "" });
    else if (inst.stage === "interrupt")
      items.push({
        p: 1,
        title: inst.title,
        reason: "pre-commit failed" + (inst.failed_step ? " at " + inst.failed_step : ""),
      });
    else if (inst.setup && inst.setup.state === "failed")
      items.push({ p: 1, title: inst.title, reason: "worktree setup failed" });
    else if (inst.check && inst.check.state === "failed" && !(inst.check as { stale?: boolean }).stale)
      items.push({ p: 2, title: inst.title, reason: "checks failing" });
    else if (inst.stage === "pushed")
      items.push({ p: 3, title: inst.title, reason: "pushed — ready for PR" });
    else if (act === "idle" && Number(inst.activity_since) > 0) {
      // Wedged-session watchdog: calm-looking but sitting on unfinished work.
      const idleFor = Date.now() / 1000 - Number(inst.activity_since);
      const un = (inst.diff_stat || ({} as never))?.uncommitted || ({} as { additions?: number; deletions?: number });
      const unfinished =
        (Number(un.additions) || 0) + (Number(un.deletions) || 0) > 0 || inst.stage === "committed";
      if (idleFor > WEDGE_IDLE_S && unfinished)
        items.push({
          p: 1,
          title: inst.title,
          reason:
            "idle " +
            relTime(Number(inst.activity_since)).replace(" ago", "") +
            " with unfinished work — possibly stuck",
          snippet: inst.last_turn || "",
        });
    }
  }
  return items.sort((a, b) => a.p - b.p || a.title.localeCompare(b.title));
}
