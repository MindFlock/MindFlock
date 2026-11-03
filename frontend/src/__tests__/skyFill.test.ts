/** Tidy fills the screen (docs/flights.md "As built: Tidy fills the screen"):
 * `fillWidth` picks the wrap width whose layout a fit shows largest — wider
 * rows on a landscape window, today's column on a portrait one — and Tidy
 * ends on the whole fit. */

import { describe, expect, it } from "vitest";
import { code } from "./sourceText";
import { FIT_PAD, INTAKE_KEY, LINES_LEFT, SKY_MARGIN, assignSpots, boundsOf, fillWidth, type LineBox, type PlacedLine } from "../lib/skyLayout";

const MIN = LINES_LEFT + 900;
const area = (width = MIN) => ({ width, left: SKY_MARGIN, top: SKY_MARGIN });
const chains = (n: number, w = 900, h = 200): LineBox[] => Array.from({ length: n }, (_, i) => ({ id: `s${i}`, w, h }));
const lane: LineBox = { id: INTAKE_KEY, w: 240, h: 600 };

/** The bounds a fit sees for these items placed at `width` (the lane flat). */
function boundsAt(items: LineBox[], width: number) {
  const spots = assignSpots([], items, area(width));
  return boundsOf(items.map((l) => ({ ...spots[l.id], w: l.w, h: l.id === INTAKE_KEY ? 1 : l.h })))!;
}
const zoomOf = (b: { w: number; h: number }, vw: number, vh: number) => Math.min(1, (vw - 2 * FIT_PAD) / b.w, (vh - 2 * FIT_PAD) / b.h);

describe("fillWidth", () => {
  const twelve = [lane, ...chains(12)];

  it("a landscape window gets wider rows: a fit shows them larger, closer to its shape", () => {
    const w = fillWidth([], twelve, area(), 1920, 1080, { flat: [INTAKE_KEY] });
    expect(w).toBeGreaterThan(MIN);
    const wide = boundsAt(twelve, w);
    const tall = boundsAt(twelve, MIN);
    expect(zoomOf(wide, 1920, 1080)).toBeGreaterThan(zoomOf(tall, 1920, 1080));
    const want = 16 / 9;
    expect(Math.abs(Math.log(wide.w / wide.h / want))).toBeLessThan(Math.abs(Math.log(tall.w / tall.h / want)));
  });

  it("a portrait window keeps today's width (the minimum): a second column gains it less than FILL_GAIN", () => {
    expect(fillWidth([], twelve, area(), 1080, 1920, { flat: [INTAKE_KEY] })).toBe(MIN);
    expect(fillWidth([], twelve, area(1440), 1440, 2560, { flat: [INTAKE_KEY] })).toBe(1440);
  });

  it("a few small lines that already fit at full size: a tie, so the narrowest — the minimum", () => {
    expect(fillWidth([], [lane, ...chains(2, 300, 80)], area(), 1920, 1080, { flat: [INTAKE_KEY] })).toBe(MIN);
  });

  it("never less than the minimum, whatever the window", () => {
    for (const [vw, vh] of [
      [1920, 1080],
      [3840, 1080],
      [800, 2400],
      [400, 300],
    ])
      for (const n of [1, 3, 12, 40]) expect(fillWidth([], [lane, ...chains(n)], area(), vw, vh, { flat: [INTAKE_KEY] })).toBeGreaterThanOrEqual(MIN);
  });

  it("nothing to place, or no window yet: the minimum", () => {
    const placed: PlacedLine[] = chains(4).map((l, i) => ({ ...l, x: LINES_LEFT, y: SKY_MARGIN + i * 300 }));
    expect(fillWidth(placed, chains(4), area(), 1920, 1080)).toBe(MIN);
    expect(fillWidth([], twelve, area(), 0, 0)).toBe(MIN);
  });

  it("the right column's width counts: a wide one keeps the rows narrower", () => {
    const right: LineBox[] = [{ id: "landing", w: 1600, h: 240 }];
    const plain = fillWidth([], twelve, area(), 1920, 1080, { flat: [INTAKE_KEY] });
    const withCol = fillWidth([], twelve, area(), 1920, 1080, { flat: [INTAKE_KEY], right });
    expect(withCol).toBeLessThanOrEqual(plain);
  });
});

describe("the sky view uses it", () => {
  const sky = code("components/flights/SkyView.tsx");

  it("placement wraps at fillWidth's width, and the memo reruns on the window's height", () => {
    expect(sky).toContain("area.width = fillWidth(existing, newcomers, area, vp.w, vp.h, { flat: [INTAKE_KEY, PALETTE_KEY], right: rightItems });");
    expect(sky).toMatch(/return \{ place, fresh \};\s*\}, \[[^\]]*\bvp\.h\b[^\]]*\]\);/);
  });

  it("Tidy arms a whole fit that fires once every shown line has its spot; Tidy these doesn't", () => {
    expect(sky).toMatch(/const tidy = \(\) => \{[\s\S]*?tidyFit\.current = true;[\s\S]*?\n {2}\};/);
    expect(sky).toMatch(/if \(!tidyFit\.current\) return;[\s\S]*?tidyFit\.current = false;\s*fitAll\(true\);/);
    const these = sky.match(/const tidyThese = \(ids: string\[\]\) => \{[\s\S]*?\n {2}\};/);
    expect(these).not.toBeNull();
    expect(these![0]).not.toContain("tidyFit");
  });
});
