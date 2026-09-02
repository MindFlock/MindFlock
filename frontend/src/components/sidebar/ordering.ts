/** Sidebar ordering + filter + needs-attention model (ports of app.js
 * sections 9's ordered()/_matchesFilter()/attentionItems()). */

import type { Instance } from "../../api/types";
import { relTime } from "../../lib/format";
import { effectiveActivity } from "../../lib/stage";

/** Arrange `keys` by the saved drag order: known keys in saved order first,
 * then keys the order has never seen, in the order given. The one ordering
 * rule for the whole rail — sessions and windows share it, because a window's
 * order key (its NUL-prefixed sentinel) lives in the same namespace as a session
 * title, exactly as it already does in the MRU and the grid rows. */
export function orderedKeys(keys: string[], order: string[]): string[] {
  const present = new Set(keys);
  const out: string[] = [];
  const seen = new Set<string>();
  for (const k of order) {
    if (present.has(k) && !seen.has(k)) {
      out.push(k);
      seen.add(k);
    }
  }
  for (const k of keys) {
    if (!seen.has(k)) {
      out.push(k);
      seen.add(k);
    }
  }
  return out;
}

/** Stable user order first (drag order), then unlisted instances in server
 * order. Returns the reconciled order for persistence alongside the rows.
 * A transient empty list must not rewrite the saved order. */
export function orderedInstances(
  instances: Instance[],
  order: string[]
): { rows: Instance[]; nextOrder: string[] } {
  if (!instances.length) return { rows: [], nextOrder: order };
  const byTitle = new Map(instances.map((i) => [i.title, i]));
  const rows = orderedKeys([...byTitle.keys()], order).map((t) => byTitle.get(t)!);
  return { rows, nextOrder: rows.map((i) => i.title) };
}

/** The saved order after dragging one rail row (a session or a window)
 * above/below another.
 *
 * A MERGE of the saved order with the live rail, never a replacement: the
 * saved order is sparse and can hold slots for rows that aren't in this
 * snapshot — a sleeping remote device's sessions, a closed assistant window —
 * and materializing only what's on screen would silently erase them (the same
 * `nextOrder` trap placeAfter in sessionActions documents). Live keys the
 * order has never seen are appended in rail order, so the splice lands exactly
 * where the drop cue showed.
 *
 * `stale` prunes order keys that should NOT keep a slot once the merge has
 * them in hand: verify/ext window sentinels whose window is closed. Those
 * panes don't survive a reload anyway, so a remembered position is a slow
 * leak, not a feature — unlike the three fixed windows (assistant, logs),
 * whose sentinels are constants and whose position SHOULD survive a
 * close/reopen. Session titles always keep their slots. */
export function movedRailOrder(opts: {
  saved: string[];
  live: string[];
  drag: string;
  target: string;
  before: boolean;
  stale?: (key: string) => boolean;
}): string[] {
  const { saved, live, drag, target, before, stale } = opts;
  if (!drag || drag === target) return saved;
  const seen = new Set(saved);
  const order = saved
    .concat(live.filter((k) => !seen.has(k)))
    .filter((k) => k !== drag && !(stale && stale(k)));
  let to = order.indexOf(target);
  if (to < 0) to = order.length;
  else if (!before) to += 1;
  order.splice(to, 0, drag);
  // A never-dragged window still sitting at the rail's tail must NOT be baked
  // into the saved order by someone ELSE's drag: persisted, its sentinel
  // would file every later-created session below it (new keys append after
  // everything saved). A trailing sentinel the saved order has never seen
  // re-appears in the same place dynamically, so drop it — unless it IS the
  // dragged key, which is the user placing it there on purpose. A sentinel
  // that ended up ABOVE anything is load-bearing for that row and stays.
  const savedSet = new Set(saved);
  while (order.length) {
    const last = order[order.length - 1];
    if (last !== drag && last.startsWith("\u0000") && !savedSet.has(last)) order.pop();
    else break;
  }
  return order;
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
      //
      // This branch had never once rendered: `activity_since` read a key nothing
      // wrote, so it was 0 for every session and the condition was dead. Now that
      // it is populated, "unfinished" has to mean what the row says. A COMMITTED
      // branch is not unfinished work — git considers it done and the header is
      // simply asking you to push — and counting it flagged every session anyone
      // had committed and walked away from as "possibly stuck", on the bell's
      // attention badge. Uncommitted output with nobody typing is the real
      // signal: an agent stopped in the middle of something.
      const idleFor = Date.now() / 1000 - Number(inst.activity_since);
      const un = (inst.diff_stat || ({} as never))?.uncommitted || ({} as { additions?: number; deletions?: number });
      const unfinished = (Number(un.additions) || 0) + (Number(un.deletions) || 0) > 0;
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
