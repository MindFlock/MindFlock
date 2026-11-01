/** Snapping and alignment guides while a box is dragged (docs/flights.md
 * "Round 17": task `sky-core`). SEAM: the pure function the sky calls each
 * pointer move; the task fills it in. Until then nothing snaps. */

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

/** Where `moving` (at its proposed `x`,`y`) lands once snapped to `others`'
 * edges and centres within `threshold` plane px, and the guides to draw. */
export function snapBox(moving: SnapRect, others: readonly SnapRect[], threshold: number): SnapResult {
  void others;
  void threshold;
  return { x: moving.x, y: moving.y, guides: [] };
}
