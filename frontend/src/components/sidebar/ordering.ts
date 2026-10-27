/** Sidebar ordering + filter + needs-attention model (ports of app.js
 * sections 9's ordered()/_matchesFilter()/attentionItems()). */

import type { Instance } from "../../api/types";
import { attentionPriority, attentionReason, deriveSessionState, effectiveActivity } from "../../lib/sessionState";
import { laneOf } from "../../lib/agentMessages";

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

/** `keys` with every worker pulled up beneath its parent's family.
 *
 * A worker is NEVER separated from its parent on the rail: whatever the saved
 * order says — a duplicated window slotted in under its source, a drag, a row
 * placed before its family existed — a parent's descendants follow it as one
 * block, siblings in their saved order. Everything else (roots, windows, a
 * worker whose parent is absent from `keys`) keeps the saved sequence, with
 * each family riding along at its parent's slot. A parent cycle is treated as
 * roots so nothing is lost. Returns `keys` itself when nothing moves. */
export function keepFamiliesTogether(keys: string[], parentOf: (key: string) => string | undefined): string[] {
  const present = new Set(keys);
  const up = (k: string) => {
    const p = parentOf(k) || "";
    return p && p !== k && present.has(p) ? p : "";
  };
  const kids = new Map<string, string[]>();
  const roots: string[] = [];
  for (const k of keys) {
    const p = up(k);
    // In a cycle iff walking up comes back to k (bounded by the list size).
    let q = p;
    for (let hops = 0; q && q !== k && hops <= keys.length; hops++) q = up(q);
    if (!p || q === k) roots.push(k);
    else kids.set(p, [...(kids.get(p) || []), k]);
  }
  if (!kids.size) return keys;
  const out: string[] = [];
  const done = new Set<string>();
  const emit = (k: string) => {
    if (done.has(k)) return;
    done.add(k);
    out.push(k);
    for (const c of kids.get(k) || []) emit(c);
  };
  roots.forEach(emit);
  for (const k of keys) emit(k); // defensive: nothing is ever dropped
  return out.every((k, i) => k === keys[i]) ? keys : out;
}

/** How deep the rail draws a family. The MCP refuses spawns past depth 3
 * (MINDFLOCK_MAX_SPAWN_DEPTH), and an adopted subtree answers to the same
 * limit; a deeper chain (the knob raised) simply stops indenting. */
export const NEST_MAX = 3;

/** One rail row's family geometry. `depth` 0 = not nested. `more`: a later
 * sibling continues this row's connector downwards (├ rather than └).
 * `guides[k]` (k = 1..depth-1): an ANCESTOR at depth k still has a sibling to
 * come, so its connector passes down through this row. `stem`: the very next
 * row nests under THIS one, so a connector drops from its dot. */
export interface NestInfo {
  depth: number;
  more: boolean;
  guides: boolean[];
  stem: boolean;
}

/** The visual-only family nesting of a rail list, index-aligned with `rows`.
 *
 * A row nests only when it DIRECTLY follows its parent or a row already
 * nested under that parent (a sibling, or a sibling's own subtree). Anything
 * else breaks the chain, and the worker renders flat (its status line then
 * names the parent instead). On the rail that only happens across a section
 * boundary (a run group, a device): `keepFamiliesTogether` already made every
 * family contiguous before this runs.
 *
 * Deliberately a pure read of the order it is given: it never moves, hides or
 * folds a row. `railOrder` (Alt+N, Ctrl+Tab, the notification "[N]"), the
 * number badges and drag-and-drop all keep working off the unmodified list —
 * nesting is paint, not structure. */
export function railNesting(rows: Array<{ key: string; parent?: string }>): NestInfo[] {
  const out: NestInfo[] = rows.map(() => ({ depth: 0, more: false, guides: [], stem: false }));
  // The open chain: chain[d] = index of the row at depth d that later rows
  // can still nest under.
  let chain: number[] = [];
  rows.forEach((r, i) => {
    const p = r.parent || "";
    const at = p ? chain.findIndex((j) => rows[j].key === p) : -1;
    if (at >= 0 && at + 1 <= NEST_MAX) {
      chain = chain.slice(0, at + 1);
      out[i].depth = at + 1;
      chain.push(i);
    } else {
      chain = [i];
    }
  });
  for (let i = 0; i < rows.length; i++) {
    const d = out[i].depth;
    if (i + 1 < rows.length && out[i + 1].depth === d + 1) out[i].stem = true;
    if (!d) continue;
    // A later row at the same depth under the same parent, before the chain
    // climbs above this depth, continues the connector.
    for (let j = i + 1; j < rows.length && out[j].depth >= d; j++) {
      if (out[j].depth === d) {
        out[i].more = rows[j].parent === rows[i].parent;
        break;
      }
    }
  }
  // Pass-through guides: each nested row inherits "does my ancestor at depth
  // k continue below me" from the chain above it.
  const open: boolean[] = [];
  for (let i = 0; i < rows.length; i++) {
    const d = out[i].depth;
    open.length = Math.max(d, 0);
    out[i].guides = [];
    for (let k = 1; k < d; k++) out[i].guides[k] = !!open[k];
    if (d) open[d] = out[i].more;
  }
  return out;
}

/** Do two rows draw the same connectors? */
export function sameNest(a: NestInfo, b: NestInfo): boolean {
  if (a.depth !== b.depth || a.more !== b.more || a.stem !== b.stem) return false;
  for (let k = 1; k < a.depth; k++) if (!!a.guides[k] !== !!b.guides[k]) return false;
  return true;
}

/** Rows with a remote row's `parent` in the namespace of its `title`.
 *
 * Another tailnet device's sessions arrive titled "<device>::<title>", and the
 * rail matches a worker to its parent by title. The server namespaces
 * `parent` the same way when it merges a device's rows; an older one passed
 * the bare title through, which names no row here (or a LOCAL session of the
 * same name), so that device's workers never nested. Rows that need nothing
 * come back as the same objects. */
export function deviceLineage<T extends { device?: string; parent?: string }>(rows: T[]): T[] {
  return rows.map((r) =>
    r.device && r.parent && !r.parent.includes("::") ? { ...r, parent: r.device + "::" + r.parent } : r
  );
}

/** The saved order with every never-placed worker slotted beneath its family.
 *
 * An agent spawns its workers server-side, so to this rail they are simply
 * sessions the saved order has never seen — `orderedInstances` would file
 * each one after everything else, nowhere near the orchestrator, and nothing
 * would nest. Each newcomer instead lands after the last row of its parent's
 * contiguous subtree (`orderWithAfter`), ancestors first so a grandchild
 * finds its parent already placed.
 *
 * Only titles the saved order has NEVER held are touched: once a worker is in
 * the order, the user's drags own its position (dragging it away un-nests
 * it, and it stays where it was put). Like placeAfter in sessionActions this
 * MERGES with the live list rather than replacing the order, so the slot of a
 * row missing from this snapshot (a sleeping device) survives. Returns `saved`
 * itself when there is nothing to place. */
export function placeNewWorkers(
  saved: string[],
  live: Array<{ title: string; parent?: string }>
): string[] {
  const parentOf = new Map(live.map((r) => [r.title, r.parent || ""]));
  const seen = new Set(saved);
  const depthOf = (t: string) => {
    let d = 0;
    for (let p = parentOf.get(t); p && d <= live.length; p = parentOf.get(p)) d++;
    return d;
  };
  const fresh = live
    .filter((r) => r.parent && r.parent !== r.title && parentOf.has(r.parent) && !seen.has(r.title))
    .map((r) => r.title)
    .sort((a, b) => depthOf(a) - depthOf(b));
  if (!fresh.length) return saved;
  const isUnder = (t: string, anc: string) => {
    let hops = 0;
    for (let p = parentOf.get(t); p && hops <= live.length; p = parentOf.get(p), hops++)
      if (p === anc) return true;
    return false;
  };
  let order = saved.concat(live.map((r) => r.title).filter((t) => !seen.has(t)));
  // Newcomers still waiting their turn sit wherever the merge appended them;
  // they must not count as the family's tail, or siblings created together
  // would land in reverse.
  const pending = new Set(fresh);
  for (const t of fresh) {
    pending.delete(t);
    const p = parentOf.get(t)!;
    const rest = order.filter((x) => x !== t);
    let at = rest.indexOf(p);
    if (at < 0) continue;
    while (at + 1 < rest.length && !pending.has(rest[at + 1]) && isUnder(rest[at + 1], p)) at++;
    order = orderWithAfter(order, t, rest[at]);
  }
  return order;
}

/** The saved order with every never-placed GROUP member (ship lanes: a session
 * the server started for a run) slotted after the last member of its group.
 *
 * The rail draws a group's members under its header whatever the saved order
 * says, so this only decides their order WITHIN the group — and keeps a member
 * where it belongs once the group's header is gone. The server starts a group's
 * lines over minutes as slots free up; each would otherwise file at the very
 * bottom of the saved order, under whatever was created in between. Same rules
 * as `placeNewWorkers`: only titles the order has never held (a drag owns the
 * rest), merged with the live list rather than replacing the order, and
 * `saved` itself back when there is nothing to place. Members created together
 * keep the server's order; the first member of a group stays where it lands. */
export function placeNewRunMembers(
  saved: string[],
  live: Array<{ title: string; run?: { id: string } | null }>
): string[] {
  const seen = new Set(saved);
  const fresh = live.filter((r) => r.run?.id && !seen.has(r.title));
  if (!fresh.length) return saved;
  const runOf = new Map(live.map((r) => [r.title, r.run?.id || ""]));
  let order = saved.concat(live.map((r) => r.title).filter((t) => !seen.has(t)));
  const placed = new Set<string>();
  for (const r of fresh) {
    const rest = order.filter((t) => t !== r.title);
    let at = -1;
    rest.forEach((t, i) => {
      if (runOf.get(t) === r.run!.id && (seen.has(t) || placed.has(t))) at = i;
    });
    placed.add(r.title);
    if (at >= 0) order = orderWithAfter(order, r.title, rest[at]);
  }
  return order;
}

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

export interface AttentionItem {
  p: number;
  title: string;
  reason: string;
  snippet?: unknown;
  /** Set (possibly "") when the session asks in plain words rather than on a
   * dialog (lib/ask.ts): the bell's strip shows it with Reply, no buttons. */
  question?: string;
}

/** The question `inst`'s agent asked in plain words, or undefined when it is
 * not asking one (a dialog on screen wins: that is what waits). */
export function askedQuestion(inst: Partial<Instance>): string | undefined {
  const a = inst.asking;
  if (!a || a.kind !== "question" || effectiveActivity(inst) === "clarify") return undefined;
  return String(a.text || "");
}

/** O1: the prioritized "which session needs me" list (bell popover + mobile).
 * 0 waiting on your answer · 1 broken (pre-commit / setup failed, offline,
 * stuck) · 2 checks failing · 3 ready to move.
 *
 * Every row but "ready to move" is the session's ONE state (lib/sessionState):
 * the bell lists exactly the states whose `inBell` is true, worded with the
 * same label the row's chip and the toast use. "Pushed — ready for PR" is a
 * ship-stage fact, not an agent state, so it stays here. */
export function attentionItems(instances: Instance[]): AttentionItem[] {
  const items: AttentionItem[] = [];
  for (const inst of instances || []) {
    if (inst.workspace_missing || inst.status === "paused") continue;
    const ss = deriveSessionState(inst);
    if (ss.inBell) {
      const step = ss.reason === "precommit_failed" ? (inst.failed_step || "").trim() : "";
      items.push({
        p: attentionPriority(ss),
        title: inst.title,
        reason: attentionReason(ss) + (step ? " at " + step : ""),
        snippet:
          ss.reason === "question" || ss.reason === "stuck"
            ? (askedQuestion(inst) ?? (inst.last_turn || ""))
            : undefined,
        question: ss.reason === "question" ? askedQuestion(inst) : undefined,
      });
    } else if (ss.state === "idle" && inst.stage === "pushed") {
      // A session whose lane STOPS at push is finished there, by your own
      // choice — "ready for PR" would nag about a PR you said you didn't want.
      if (laneOf(inst)?.target !== "push")
        items.push({ p: 3, title: inst.title, reason: "pushed — ready for PR" });
    }
  }
  return items.sort((a, b) => a.p - b.p || a.title.localeCompare(b.title));
}
