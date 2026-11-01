/** Round 17 (docs/flights.md "Round 17", task `sky-core`): snapping while a
 * box is dragged (lib/skySnap `snapBox`). The dragged box's left / centre /
 * right and top / middle / bottom snap to the others' within the threshold
 * (8 screen px = 8 / zoom plane px), each axis on its own, the nearest
 * alignment winning; the guides come back as spans from the moving box to
 * the one it aligned with. Pure geometry — no DOM. */

import { describe, expect, it } from "vitest";
import { SNAP_SCREEN_PX, snapBox, snapThreshold, type SnapRect } from "../lib/skySnap";

const box = (id: string, x: number, y: number, w = 100, h = 50): SnapRect => ({ id, x, y, w, h });

describe("snapBox", () => {
  it("snaps a left edge to a left edge within the threshold, and draws one vertical guide over both boxes", () => {
    const other = box("b", 300, 0);
    const r = snapBox(box("a", 305, 200), [other], 8);
    expect(r.x).toBe(300);
    expect(r.y).toBe(200);
    expect(r.guides).toEqual([{ axis: "x", at: 300, from: 0, to: 250 }]);
  });

  it("snaps centres and right edges too, the nearest alignment winning", () => {
    // Centre of a (x+50) to the centre of b (350): 3 px away; left edge 300 vs 303: also 3 — the first found of equal ties stays.
    const r1 = snapBox(box("a", 303, 0), [box("b", 300, 100)], 8);
    expect(r1.x).toBe(300);
    // Right edge of a at 398 → b's right edge 400 (2 px) beats a's left 298 → b's left 300 (2 px, a tie: first found, the left).
    const r2 = snapBox(box("a", 298, 0), [box("b", 300, 100)], 8);
    expect(r2.x).toBe(300);
    // Only the right edges are close: a's right 406 → b's right 400 (6 px).
    const r3 = snapBox(box("a", 306, 0), [box("b", 300, 100)], 8);
    expect(r3.x).toBe(300);
    // a's centre 357 vs b's centre 350: 7 px, inside; a's left 307 vs b's left 300: 7 px too (tie → left, same shift).
    const r4 = snapBox(box("a", 307, 0), [box("b", 300, 100)], 8);
    expect(r4.x).toBe(300);
    // a's left 290 vs b's left 300: 10 px — out of reach; a's right 390 vs b's right 400: 10 — out; a's centre 340 vs 350: 10 — out. No snap.
    const r5 = snapBox(box("a", 290, 0), [box("b", 300, 100)], 8);
    expect(r5.x).toBe(290);
    expect(r5.guides).toEqual([]);
  });

  it("the y axis snaps on its own: top, middle, bottom", () => {
    const other = box("b", 0, 100, 100, 50);
    expect(snapBox(box("a", 500, 104), [other], 8).y).toBe(100);
    // a's middle (y + 25) to b's middle 125.
    expect(snapBox(box("a", 500, 97, 100, 50), [other], 8).y).toBe(100);
    // a's bottom (y + 50) to b's bottom 150.
    expect(snapBox(box("a", 500, 106, 100, 50), [other], 8).y).toBe(100);
    const r = snapBox(box("a", 500, 104), [other], 8);
    expect(r.guides).toEqual([{ axis: "y", at: 100, from: 0, to: 600 }]);
  });

  it("both axes can snap at once, each to its own neighbour, two guides", () => {
    const r = snapBox(box("a", 203, 497), [box("b", 200, 0), box("c", 600, 500)], 8);
    expect(r.x).toBe(200);
    expect(r.y).toBe(500);
    expect(r.guides.map((g) => g.axis).sort()).toEqual(["x", "y"]);
    const gx = r.guides.find((g) => g.axis === "x")!;
    const gy = r.guides.find((g) => g.axis === "y")!;
    // The vertical guide spans b's top to a's bottom (at the snapped y).
    expect(gx).toEqual({ axis: "x", at: 200, from: 0, to: 550 });
    // The horizontal one from a's left to c's right.
    expect(gy).toEqual({ axis: "y", at: 500, from: 200, to: 700 });
  });

  it("the nearest alignment across several boxes wins", () => {
    const r = snapBox(box("a", 305, 0), [box("far", 311, 100), box("near", 302, 200)], 8);
    expect(r.x).toBe(302);
    expect(r.guides[0].at).toBe(302);
  });

  it("outside the threshold nothing moves and there are no guides; a zero threshold never snaps", () => {
    const r = snapBox(box("a", 320, 0), [box("b", 300, 100)], 8);
    expect(r).toEqual({ x: 320, y: 0, guides: [] });
    expect(snapBox(box("a", 301, 0), [box("b", 300, 100)], 0)).toEqual({ x: 301, y: 0, guides: [] });
  });

  it("skips itself and empty boxes among the others", () => {
    const self = box("a", 305, 0);
    const r = snapBox(self, [box("a", 300, 0), box("empty", 300, 0, 0, 0)], 8);
    expect(r).toEqual({ x: 305, y: 0, guides: [] });
  });

  it("the threshold is 8 screen px: 8 / zoom plane px", () => {
    expect(SNAP_SCREEN_PX).toBe(8);
    expect(snapThreshold(1)).toBe(8);
    expect(snapThreshold(2)).toBe(4);
    expect(snapThreshold(0.5)).toBe(16);
    expect(snapThreshold(0)).toBe(8);
  });
});

describe("the sky wires it", () => {
  it("snaps the pressed box each move (Alt = raw), draws .sky-guide lines in the plane while the drag lasts, and drops at the snapped spot", async () => {
    const { code, css } = await import("./sourceText");
    const v = code("components/flights/SkyView.tsx");
    expect(v).toContain('import { snapBox, snapThreshold, type SnapGuide } from "../../lib/skySnap";');
    expect(v).toContain("if (!pressedRect || ev.altKey || !still.length) return { ...raw, guides: [] as SnapGuide[] };");
    expect(v).toContain("const r = snapBox({ id, x: from[id].x + raw.dx, y: from[id].y + raw.dy, w: pressedRect.w, h: pressedRect.h }, still, snapThreshold(camRef.current.z));");
    expect(v).toContain("setGuides(d.guides);");
    expect(v).toContain("setGuides([]);");
    // The drop uses the last (snapped) delta, not a fresh raw one.
    expect(v).toMatch(/if \(g\.moved\) \{\s*const \{ dx, dy \} = g;/);
    expect(v).toContain('className="sky-guide"');
    expect(v).toContain('data-snapping={guides.length ? "" : undefined}');
    const sheet = css("components/flights/Sky.css");
    expect(sheet).toMatch(/\.sky-guide \{[^}]*position: absolute;[^}]*pointer-events: none;[^}]*background: var\(--accent\);/);
    // One SCREEN px however the plane is zoomed.
    expect(sheet).toMatch(/\.sky-guide\[data-axis="x"\] \{\s*width: calc\(1px \/ var\(--sky-z, 1\)\);/);
    expect(sheet).toMatch(/\.sky-guide\[data-axis="y"\] \{\s*height: calc\(1px \/ var\(--sky-z, 1\)\);/);
  });
});
