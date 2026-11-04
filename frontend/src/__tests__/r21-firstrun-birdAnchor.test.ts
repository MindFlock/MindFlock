/** Round 21 (JUDGE-R21-1 #3): in All/Mine the bird slid 19–58px off its stop
 * after zooming out or Tidy. The far level of detail grew the marker with CSS
 * `zoom`, and zoom scales an element's OWN left/top too: a bird placed at
 * plane x stood at x × --sky-far-ui (1.2 at 50 %, 1.4 at the 40 % floor).
 * SkyFlock.css now grows it with `scale` about its layout origin, so the
 * point its transform pins to left/top stays on the stop at any zoom and
 * after any layout change. */

import { describe, expect, it } from "vitest";
import { css } from "./sourceText";

/** Where the marker's foot (the point its transform pins: 50 % / 78 % of its
 * box, translate(-50%, -78%)) lands in its route's plane, for a marker at
 * `left`/`top` of box w×h grown by `zoom` (CSS zoom: left/top and the box
 * both × zoom) or by `scale` about transform-origin 0 0 (applied after the
 * translate, so the foot — at the origin by then — doesn't move). */
function footAt(o: { left: number; top: number; w: number; h: number; zoom?: number; scale?: number }): { x: number; y: number } {
  const z = o.zoom ?? 1;
  const s = o.scale ?? 1;
  // Layout: CSS zoom multiplies the element's own offsets and size.
  const L = o.left * z;
  const T = o.top * z;
  const W = o.w * z;
  const H = o.h * z;
  // Local foot point, translated by (-50 %, -78 %) of the (zoomed) box…
  const fx = 0.5 * W - 0.5 * W;
  const fy = 0.78 * H - 0.78 * H;
  // …then `scale` about the origin (0 0): (0, 0) stays (0, 0).
  return { x: L + fx * s, y: T + fy * s };
}

/** The far level of detail's growth factor (SkyLine.css --sky-far-ui). */
const farUi = (zoom: number) => Math.min(1.4, Math.max(1, 0.6 / zoom));

describe("the bird stays on its stop when the Sky zooms out", () => {
  // A route laid out once, then again by Tidy (T): the stop moved.
  const layouts = [
    { name: "initial", stopX: 330, stopTop: 40 },
    { name: "after Tidy", stopX: 512, stopTop: 64 },
  ];

  for (const zoom of [1, 0.62, 0.5, 0.4]) {
    for (const l of layouts) {
      it(`at ${Math.round(zoom * 100)} %, ${l.name}: scale keeps it on the stop`, () => {
        const f = farUi(zoom);
        expect(footAt({ left: l.stopX, top: l.stopTop, w: 30, h: 28, scale: f })).toEqual({ x: l.stopX, y: l.stopTop });
      });
    }
  }

  it("zoom (the old rule) drifts by x × (factor − 1) — about one stop at the floor", () => {
    const f = farUi(0.4);
    const at = footAt({ left: 330, top: 40, w: 30, h: 28, zoom: f });
    expect(Math.round(at.x - 330)).toBe(132); // plane px; ≈ 53px on screen at 40 %
  });
});

describe("the CSS that does it", () => {
  const flock = css("components/flights/SkyFlock.css");
  const line = css("components/flights/SkyLine.css");

  it("far out, the marker is never zoomed — it is scaled about its origin", () => {
    expect(flock).toMatch(/\.skl\[data-lod="far"\] span\.fl-bird-marker \{\s*zoom: 1;\s*\}/);
    expect(flock).toMatch(
      /\.skl\[data-lod="far"\] span\.fl-bird-marker:not\(\.fl-bird-dragging\) \{\s*transform-origin: 0 0;\s*scale: var\(--sky-far-ui, 1\);\s*\}/
    );
  });

  it("its selector outranks the far zoom rule it undoes (one element more)", () => {
    // SkyLine.css's rule is `.skl[data-lod="far"] .fl-bird-marker` (0,3,0);
    // `span.fl-bird-marker` makes ours (0,3,1), whatever the bundle order.
    expect(line).toMatch(/\.skl\[data-lod="far"\] \.fl-bird-marker \{\s*zoom: var\(--sky-far-ui\);/);
  });
});
