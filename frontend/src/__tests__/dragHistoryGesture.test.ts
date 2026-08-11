/** Where the drag-past-the-edge → full-history gesture arms.
 *
 * This geometry is the whole gesture: xterm cannot extend a selection past the
 * top of the terminal (tmux repaints history underneath the highlight), so
 * dragging out of the edge is what hands the drag to the history overlay. If
 * the zone doesn't match the motion a hand actually makes, the feature may as
 * well not exist — which is exactly what happened while the top zone sat
 * OUTSIDE the element.
 *
 * The DOM half (hold timer, one-shot per drag, release cancels) lives in
 * attachDragHistoryGesture; the suite runs in the "node" environment on
 * purpose, so what is pinned here is the decision, not the wiring.
 */

import { describe, it, expect } from "vitest";
import { dragEdgeAt, DRAG_EDGE_ZONE } from "../lib/terminals";

/** A terminal occupying y = 100..400 on screen. */
const term = { top: 100, bottom: 400 };

describe("dragEdgeAt", () => {
  it("arms 'top' while the pointer is still INSIDE the terminal", () => {
    // The regression: a drag up stops where the selection stops — on the top
    // row — and used to need another 8px into the pane header to count. Nobody
    // does that, and nothing tells them to.
    expect(dragEdgeAt(101, term)).toBe("top");
    expect(dragEdgeAt(110, term)).toBe("top");
  });

  it("still arms 'top' once the pointer leaves the element", () => {
    expect(dragEdgeAt(99, term)).toBe("top");
    expect(dragEdgeAt(40, term)).toBe("top");
    expect(dragEdgeAt(-50, term)).toBe("top");
  });

  it("arms 'bottom' on the tmux status row and below it", () => {
    expect(dragEdgeAt(399, term)).toBe("bottom");
    expect(dragEdgeAt(390, term)).toBe("bottom");
    expect(dragEdgeAt(460, term)).toBe("bottom");
  });

  it("stays null through the middle, so ordinary selection is untouched", () => {
    expect(dragEdgeAt(200, term)).toBeNull();
    expect(dragEdgeAt(250, term)).toBeNull();
    // Just inside each zone boundary.
    expect(dragEdgeAt(100 + DRAG_EDGE_ZONE, term)).toBeNull();
    expect(dragEdgeAt(400 - DRAG_EDGE_ZONE, term)).toBeNull();
  });

  it("treats the two edges symmetrically", () => {
    const depth = 5;
    expect(dragEdgeAt(term.top + depth, term)).toBe("top");
    expect(dragEdgeAt(term.bottom - depth, term)).toBe("bottom");
  });

  it("resolves a pane shorter than two zones to one edge, not both", () => {
    // A squeezed pane (nine-up grid) has overlapping zones. Every point still
    // gets exactly one answer — the overlap goes to "top", and the rows below
    // it are "bottom" — so the gesture can't flicker between the two.
    const tiny = { top: 100, bottom: 120 };
    expect(dragEdgeAt(105, tiny)).toBe("top"); // in both zones -> top wins
    expect(dragEdgeAt(117, tiny)).toBe("top");
    expect(dragEdgeAt(119, tiny)).toBe("bottom"); // past the top zone
  });
});
