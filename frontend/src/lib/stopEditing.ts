/** Editing the stops on a line the way n8n edits its nodes — the rules the
 * canvas (FlywayCanvas) and a session's line (useSessionLine) share, pure so
 * the tests read them (docs/flights-stop-editing.md):
 *
 *  - `insertGaps`   where a + can sit between two boxes of the line (never
 *                   before the flight's current stop, never across a column
 *                   of branches);
 *  - `dropGapFor`   which of those gaps a stop chip dragged from the shelf
 *                   (`STOP_MIME`) lands in — the nearest to the pointer, else
 *                   the end of the line;
 *  - `canShift`     whether ⇤ / ⇥ (and ← / →) can move a stop one place;
 *  - `undoPayload`  what Undo tacks back after a stop was removed;
 *  - `menuItemsFor` a box's right-click menu, by its kind and state;
 *  - `haltToggle` / `haltMark`  a halt point's toggle and the badge a box
 *                   wears while it is one (docs/flights.md "Halt points");
 *  - `removeTool`   what 🗑 says on a stop: pending, flown, or the one the
 *                   flight is at (removing it halts the flight there);
 *  - `stopDropOf`   what a drop on a line carries: a stop kind, or a saved
 *                   stop (round 11: a group of stops). */

import type { DiagramNode, Placed } from "./flightDiagram";
import { STOP_MIME, type Flight, type FlightStep } from "./flights";
import { FLYWAY_MIME } from "./skyLayout";

// --- Gaps on the line -------------------------------------------------------------

/** A + between two boxes: insert before stop `at`; x / y is its centre, in the
 * layout's coordinates. */
export interface InsertGap {
  at: number;
  x: number;
  y: number;
}

/** Every gap of the line a stop may be inserted at: before each stop whose
 * index is ≥ `insertFrom`, never right after the trailing +, and not between a
 * stop with branches and its join point (the line reaches the join THROUGH
 * the branches). */
export function insertGaps(boxes: readonly Placed[], insertFrom: number, vertical: boolean): InsertGap[] {
  const main = boxes.filter((b) => b.node.row === "main");
  const fanned = new Set(boxes.map((b) => b.node.branch?.from));
  const gaps: InsertGap[] = [];
  for (let j = 1; j < main.length; j++) {
    const b = main[j];
    const a = main[j - 1];
    if ((b.node.kind !== "stop" && b.node.kind !== "group") || b.node.index === undefined || b.node.index < insertFrom || a.node.kind === "add" || fanned.has(a.node.id)) continue;
    gaps.push(
      vertical
        ? { at: b.node.index, x: b.x + b.w / 2, y: (a.y + a.h + b.y) / 2 }
        : { at: b.node.index, x: (a.x + a.w + b.x) / 2, y: b.y + Math.min(b.h, a.h) / 2 }
    );
  }
  return gaps;
}

/** Where a stop dropped at `point` (layout coordinates) is tacked on: the
 * index of the gap nearest to it along the line, or null — the end — when the
 * end of the line (the trailing +) is nearer, or there is no gap to take. */
export function dropGapFor(boxes: readonly Placed[], point: { x: number; y: number }, insertFrom: number, vertical: boolean): number | null {
  const along = (p: { x: number; y: number }) => (vertical ? p.y : p.x);
  const gaps = insertGaps(boxes, insertFrom, vertical);
  if (!gaps.length) return null;
  const main = boxes.filter((b) => b.node.row === "main");
  const add = main.find((b) => b.node.kind === "add");
  const last = main.filter((b) => b.node.kind !== "add").pop();
  // The end sits at the trailing +, else just past the last box.
  const end = add
    ? along({ x: add.x + add.w / 2, y: add.y + add.h / 2 })
    : last
      ? along({ x: last.x + last.w, y: last.y + last.h })
      : Infinity;
  const at = along(point);
  let best: InsertGap | null = null;
  for (const g of gaps) if (!best || Math.abs(along(g) - at) < Math.abs(along(best) - at)) best = g;
  return best && Math.abs(along(best) - at) < Math.abs(end - at) ? best.at : null;
}

/** What a drop on a line carries: a stop kind's chip (`STOP_MIME`, its value
 * the kind) or a saved stop (`FLYWAY_MIME`, its value the plan id — tacked
 * on as one group). */
export type StopDrop = { kind: string } | { planId: string };

/** Whether a dragged thing is a stop chip or a saved stop from the shelf. */
export function isStopDrag(types: ArrayLike<string> | null | undefined): boolean {
  if (!types) return false;
  const list = Array.from(types);
  return list.includes(STOP_MIME) || list.includes(FLYWAY_MIME);
}

/** The drop's payload, a kind before a saved stop; null when it has neither. */
export function stopDropOf(dt: Pick<DataTransfer, "getData"> | null | undefined): StopDrop | null {
  if (!dt) return null;
  const kind = dt.getData(STOP_MIME);
  if (kind) return { kind };
  const planId = dt.getData(FLYWAY_MIME);
  return planId ? { planId } : null;
}

// --- Moving ------------------------------------------------------------------------

/** Whether the stop at `i` (among the line's places) can move one place
 * (`dir` -1 earlier, +1 later): it changes, and so does the place it trades
 * with, which is also not before `insertFrom`. A group box does not move
 * this round (a stop may still trade places with one that can change). */
export function canShift(stops: readonly DiagramNode[], i: number, dir: -1 | 1, insertFrom: number): boolean {
  const to = i + dir;
  return !!stops[i]?.editable && stops[i].kind !== "group" && to >= insertFrom && to >= 0 && to < stops.length && !!stops[to].editable;
}

// --- Undo ---------------------------------------------------------------------------

/** What Undo tacks back on after `step` (at `index`) was removed: the same
 * stop, at its old index while that is still ahead of the flight (after its
 * current stop, and not past the end), else at the end (`at` left out). */
export function undoPayload(step: FlightStep, index: number, flight: Pick<Flight, "cursor" | "steps"> | null): { step: FlightStep; at?: number } {
  const ahead = !!flight && index > flight.cursor && index < flight.steps.length;
  return ahead ? { step, at: index } : { step };
}

// --- The right-click menu --------------------------------------------------------------

export type StopMenuKey = "edit" | "earlier" | "later" | "duplicate" | "remove" | "done" | "skip" | "retry" | "halt" | "pause" | "resume" | "tackon" | "open" | "expand" | "note";

/** One line of a box's menu: something to press, or a muted `note`. */
export interface StopMenuItem {
  key: StopMenuKey;
  label: string;
  title: string;
  disabled?: boolean;
  danger?: boolean;
  note?: boolean;
}

export interface StopMenuCtx {
  /** What the host lets the line do (the canvas's handlers). */
  can: { edit?: boolean; move?: boolean; duplicate?: boolean; remove?: boolean; open?: boolean; halt?: boolean; group?: boolean };
  /** ⇤ / ⇥ possible for this stop (`canShift`). */
  earlier?: boolean;
  later?: boolean;
  /** The stop belongs to a live flight the menu can act on (Done / Skip / Retry). */
  live?: boolean;
  /** The box is the live flight's current stop: Halt now / Carry on. */
  here?: boolean;
  /** A group box drawn open (its sub-steps on the line): Fold, not Show. */
  expanded?: boolean;
}

// --- Halt points ------------------------------------------------------------------------

const flownState = (s: DiagramNode["state"]) => s === "done" || s === "skipped" || s === "failed";

/** A group's halt point lives on its first sub-step not yet flown (the
 * flight's own, or the next it reaches): that sub-step's node, null when
 * every one has flown. */
export function groupHaltStep(n: DiagramNode): DiagramNode | null {
  if (n.kind !== "group") return null;
  return n.group?.nodes.find((s) => !flownState(s.state) && !!s.step) || null;
}

/** Whether a box can be marked a halt point: a stop not yet flown, that can
 * change or is where the flight is; a group by its first sub-step not flown. */
export function canHalt(n: DiagramNode, here = false): boolean {
  if (n.kind === "group") {
    const s = groupHaltStep(n);
    return !!s && (!!s.editable || FLIGHT_HERE.has(s.state));
  }
  return n.kind === "stop" && !!n.step && (!!n.editable || here) && !flownState(n.state);
}

const FLIGHT_HERE: ReadonlySet<DiagramNode["state"]> = new Set(["current", "perched", "paused", "halted"]);

/** ⏸ Halt here on a stop, or Carry on past here on a halt point. */
export function haltToggle(n: DiagramNode): StopMenuItem {
  if (n.kind === "group") n = groupHaltStep(n) || n;
  return n.step?.halt
    ? { key: "halt", label: "Carry on past here", title: "Take the halt point off this stop — the flight flies on through it" }
    : { key: "halt", label: "Halt here", title: "Make this stop a halt point — the flight stops when it gets here, before doing it" };
}

/** ⏹ Halt now (the flight under way or waiting on you) or ▶ Carry on (held),
 * on the flight's current stop; none on a stop halted by a failure (Retry). */
export function flightHold(n: DiagramNode): StopMenuItem | null {
  if (n.state === "paused") return { key: "resume", label: "Carry on", title: "Fly on from this stop" };
  if (n.state === "halted") return null;
  return { key: "pause", label: "Halt now", title: "Stop the flight where it is — Carry on picks it up again" };
}

/** The badge a halt point wears in its corner (null on any other box). */
export function haltMark(n: DiagramNode): { glyph: string; title: string } | null {
  if (n.kind === "group") {
    const s = groupHaltStep(n);
    return s?.step?.halt ? haltMark(s) : null;
  }
  if (n.kind !== "stop" || !n.step?.halt) return null;
  return n.state === "paused"
    ? { glyph: "⏸", title: "Halt point — the flight stopped here. Carry on to fly on" }
    : { glyph: "⏸", title: "Halt point — the flight stops here before doing this stop" };
}

const note = (label: string): StopMenuItem => ({ key: "note", label, title: label, note: true });

/** 🗑 on a stop, by where it is on a live line: the one the flight is at
 * (removing it halts the flight right before the stop that followed), one
 * that has flown, or one still ahead; a group takes its sub-steps with it. */
export function removeTool(n: DiagramNode, here = false): StopMenuItem {
  if (n.kind === "group") {
    const k = n.group?.total || 0;
    return { key: "remove", label: "Remove", title: "Remove this stop and its " + k + (k === 1 ? " sub-step" : " sub-steps") + " from the line", danger: true };
  }
  if (here) return { key: "remove", label: "Remove — the flight halts here", title: "Remove this stop — the flight halts right before the stop that followed it", danger: true };
  if (flownState(n.state)) return { key: "remove", label: "Remove from the line", title: "Take this flown stop off the line", danger: true };
  return { key: "remove", label: "Remove", title: "Take this stop off the line (Delete)", danger: true };
}

/** A group box's menu: Halt now / Carry on while the flight is in it, its
 * dialog, open / fold its sub-steps on the line, its halt point, Remove (the
 * whole group). Earlier / Later wait for a later
 * round (a group does not move yet). */
function groupItems(n: DiagramNode, ctx: StopMenuCtx): StopMenuItem[] {
  const { can } = ctx;
  const items: StopMenuItem[] = [];
  // The flight is in it: Halt now / Carry on.
  const hold = ctx.here && ctx.live && FLIGHT_HERE.has(n.state) ? flightHold(n) : null;
  if (hold) items.push(hold);
  if (can.edit) items.push({ key: "edit", label: n.editable ? "Edit" : "Details", title: "Open this stop and its sub-steps (Enter)" });
  if (can.group)
    items.push(
      ctx.expanded
        ? { key: "expand", label: "Fold its sub-steps", title: "Draw it as one box again" }
        : { key: "expand", label: "Show its sub-steps", title: "Draw each of its sub-steps on the line" }
    );
  if (can.halt && canHalt(n)) items.push(haltToggle(n));
  if (can.remove) items.push(removeTool(n));
  if (!n.editable && n.state !== "done" && n.state !== "skipped") items.push(note("The flight is in this stop — only what is still ahead can change"));
  return items;
}
/** A box's right-click menu: an editable stop is edited, moved, duplicated,
 * made a halt point or removed; the flight's current stop offers Halt now /
 * Carry on, what its own parts do (Done on a perch, Skip, Retry when halted)
 * and its halt point; a stop that has flown says so; the
 * trailing + tacks a stop on; a session box opens. */
export function menuItemsFor(n: DiagramNode, ctx: StopMenuCtx): StopMenuItem[] {
  const { can } = ctx;
  if (n.kind === "add") return can.open ? [{ key: "tackon", label: "Tack on a stop", title: "Pick a stop to tack onto the end of the line" }] : [];
  if (n.kind === "rung") return [note("A fast-track rung — it moves as the session ships")];
  if (n.kind === "group") return groupItems(n, ctx);
  if (n.kind !== "stop") return can.open ? [{ key: "open", label: "Open", title: n.session ? "Go to " + n.session : "Open it" }] : [];
  if (n.editable) {
    const items: StopMenuItem[] = [];
    if (can.edit) items.push({ key: "edit", label: "Edit", title: "Open this stop in the inspector (Enter)" });
    if (can.move) {
      items.push({ key: "earlier", label: "Earlier", title: "Move this stop one place earlier on the line (←)", disabled: !ctx.earlier });
      items.push({ key: "later", label: "Later", title: "Move this stop one place later on the line (→)", disabled: !ctx.later });
    }
    if (can.duplicate) items.push({ key: "duplicate", label: "Duplicate", title: "The same stop again, right after this one" });
    if (can.halt && canHalt(n)) items.push(haltToggle(n));
    if (can.remove) items.push(removeTool(n));
    return items;
  }
  if (flownState(n.state)) {
    // A flown stop: read it, take it off a live line.
    const items: StopMenuItem[] = [];
    if (can.edit) items.push({ key: "edit", label: "Details", title: "Open this stop (it has flown: read-only)" });
    if (can.remove && ctx.live) items.push(removeTool(n));
    items.push(note("This stop has flown"));
    return items;
  }
  if (!ctx.here && (n.state === "pending" || n.state === "edit")) return [note("Still to come — it can't change here")];
  // The flight is here (under way, perched on you, halted or paused).
  const items: StopMenuItem[] = [];
  const hold = ctx.here && ctx.live ? flightHold(n) : null;
  if (hold) items.push(hold);
  if (ctx.live) {
    if (n.state === "perched") items.push({ key: "done", label: "Done", title: "Mark this perch done — the bird flies on" });
    if (n.state === "halted") items.push({ key: "retry", label: "Retry", title: "Run this stop again" });
    items.push({ key: "skip", label: "Skip", title: "Skip this stop — the bird flies on to the next" });
  }
  if (can.halt && canHalt(n, !!ctx.here)) items.push(haltToggle(n));
  if (can.edit) items.push({ key: "edit", label: "Details", title: "Open this stop (the flight is here: read-only)" });
  if (can.remove && ctx.live && ctx.here) items.push(removeTool(n, true));
  items.push(note("The flight is here — it can't change now"));
  return items;
}
