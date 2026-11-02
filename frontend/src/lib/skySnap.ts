/** Snapping and alignment guides while a box is dragged (docs/flights.md
 * "Round 17": task `sky-core`). The pure function the sky calls each pointer
 * move: the dragged box's left / centre / right and top / middle / bottom
 * snap to the others' within `threshold` plane px (the sky passes 8 screen
 * px / zoom), and the guides come back for the sky to draw as 1 px accent
 * lines (`.sky-guide`). `Alt` held = the sky never calls this. */

export interface SnapRect {
  id: string;
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface SnapGuide {
  /** A vertical guide at plane x, or a horizontal one at plane y. */
  axis: "x" | "y";
  at: number;
  /** The span the guide is drawn over (plane px), from the moving box to the box it aligned with. */
  from: number;
  to: number;
}

export interface SnapResult {
  x: number;
  y: number;
  guides: SnapGuide[];
}

/** The three stations of a box along one axis: its start, middle and end. */
const stations = (start: number, size: number): readonly number[] => [start, start + size / 2, start + size];

interface Match {
  /** How far the moving box moves along this axis to align. */
  shift: number;
  /** Where the guide is drawn (the other box's station). */
  at: number;
  other: SnapRect;
}

/** The nearest alignment on one axis within `threshold`: the moving box's
 * stations against every other box's. Ties go to the first found (the
 * others' order: the sky passes them in drawing order). */
function nearest(mine: readonly number[], others: readonly SnapRect[], axis: "x" | "y", threshold: number): Match | null {
  let best: Match | null = null;
  for (const o of others) {
    const theirs = axis === "x" ? stations(o.x, o.w) : stations(o.y, o.h);
    for (const m of mine)
      for (const t of theirs) {
        const d = t - m;
        if (Math.abs(d) <= threshold && (!best || Math.abs(d) < Math.abs(best.shift))) best = { shift: d, at: t, other: o };
      }
  }
  return best;
}

/** Where `moving` (at its proposed `x`,`y`) lands once snapped to `others`'
 * edges and centres within `threshold` plane px, and the guides to draw. The
 * moving box itself is never in `others` (same id is skipped). Each axis
 * snaps on its own; a guide spans from the moving box to the box it aligned
 * with, along the guide's own direction. */
export function snapBox(moving: SnapRect, others: readonly SnapRect[], threshold: number): SnapResult {
  const out: SnapResult = { x: moving.x, y: moving.y, guides: [] };
  if (!(threshold > 0) || !others.length) return out;
  const rest = others.filter((o) => o.id !== moving.id && o.w > 0 && o.h > 0);
  if (!rest.length) return out;
  const mx = nearest(stations(moving.x, moving.w), rest, "x", threshold);
  const my = nearest(stations(moving.y, moving.h), rest, "y", threshold);
  if (mx) out.x = moving.x + mx.shift;
  if (my) out.y = moving.y + my.shift;
  // The guides: a vertical line at the x it aligned to, spanning both boxes'
  // heights (at the snapped position); a horizontal one likewise.
  if (mx) {
    const from = Math.min(out.y, mx.other.y);
    const to = Math.max(out.y + moving.h, mx.other.y + mx.other.h);
    out.guides.push({ axis: "x", at: mx.at, from, to });
  }
  if (my) {
    const from = Math.min(out.x, my.other.x);
    const to = Math.max(out.x + moving.w, my.other.x + my.other.w);
    out.guides.push({ axis: "y", at: my.at, from, to });
  }
  return out;
}

/** The snap threshold on the plane for 8 screen px at zoom `z`. */
export const SNAP_SCREEN_PX = 8;
export const snapThreshold = (z: number): number => SNAP_SCREEN_PX / (z > 0 ? z : 1);
