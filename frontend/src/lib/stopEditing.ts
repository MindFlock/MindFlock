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
 *  - `menuItemsFor` a box's right-click menu, by its kind and state. */

import type { DiagramNode, Placed } from "./flightDiagram";
import { STOP_MIME, type Flight, type FlightStep } from "./flights";

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
    if (b.node.kind !== "stop" || b.node.index === undefined || b.node.index < insertFrom || a.node.kind === "add" || fanned.has(a.node.id)) continue;
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

/** Whether a dragged thing is a stop chip from the shelf. */
export function isStopDrag(types: ArrayLike<string> | null | undefined): boolean {
  return !!types && Array.from(types).includes(STOP_MIME);
}

// --- Moving ------------------------------------------------------------------------

/** Whether the stop at `i` (among the line's stops) can move one place
 * (`dir` -1 earlier, +1 later): it changes, and so does the stop it trades
 * places with, which is also not before `insertFrom`. */
export function canShift(stops: readonly DiagramNode[], i: number, dir: -1 | 1, insertFrom: number): boolean {
  const to = i + dir;
  return !!stops[i]?.editable && to >= insertFrom && to >= 0 && to < stops.length && !!stops[to].editable;
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

export type StopMenuKey = "edit" | "earlier" | "later" | "duplicate" | "remove" | "done" | "skip" | "retry" | "tackon" | "open" | "note";

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
  can: { edit?: boolean; move?: boolean; duplicate?: boolean; remove?: boolean; open?: boolean };
  /** ⇤ / ⇥ possible for this stop (`canShift`). */
  earlier?: boolean;
  later?: boolean;
  /** The stop belongs to a live flight the menu can act on (Done / Skip / Retry). */
  live?: boolean;
}

const note = (label: string): StopMenuItem => ({ key: "note", label, title: label, note: true });

/** A box's right-click menu: an editable stop is edited, moved, duplicated or
 * removed; the flight's current stop offers what its own parts do (Done on a
 * perch, Skip, Retry when halted); a stop that has flown says so; the
 * trailing + tacks a stop on; a session box opens. */
export function menuItemsFor(n: DiagramNode, ctx: StopMenuCtx): StopMenuItem[] {
  const { can } = ctx;
  if (n.kind === "add") return can.open ? [{ key: "tackon", label: "Tack on a stop", title: "Pick a stop to tack onto the end of the line" }] : [];
  if (n.kind === "rung") return [note("A fast-track rung — it moves as the session ships")];
  if (n.kind !== "stop") return can.open ? [{ key: "open", label: "Open", title: n.session ? "Go to " + n.session : "Open it" }] : [];
  if (n.editable) {
    const items: StopMenuItem[] = [];
    if (can.edit) items.push({ key: "edit", label: "Edit", title: "Open this stop in the inspector (Enter)" });
    if (can.move) {
      items.push({ key: "earlier", label: "Earlier", title: "Move this stop one place earlier on the line (←)", disabled: !ctx.earlier });
      items.push({ key: "later", label: "Later", title: "Move this stop one place later on the line (→)", disabled: !ctx.later });
    }
    if (can.duplicate) items.push({ key: "duplicate", label: "Duplicate", title: "The same stop again, right after this one" });
    if (can.remove) items.push({ key: "remove", label: "Remove", title: "Take this stop off the line (Delete)", danger: true });
    return items;
  }
  if (n.state === "done" || n.state === "skipped" || n.state === "failed") return [note("This stop has flown")];
  if (n.state === "pending" || n.state === "edit") return [note("Still to come — it can't change here")];
  // The flight is here (under way, perched on you, halted or paused).
  const items: StopMenuItem[] = [];
  if (ctx.live) {
    if (n.state === "perched") items.push({ key: "done", label: "Done", title: "Mark this perch done — the bird flies on" });
    if (n.state === "halted") items.push({ key: "retry", label: "Retry", title: "Run this stop again" });
    items.push({ key: "skip", label: "Skip", title: "Skip this stop — the bird flies on to the next" });
  }
  items.push(note("The flight is here — it can't change now"));
  return items;
}
