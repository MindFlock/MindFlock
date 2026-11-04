/** Round 21b "fit": the judge's pass-2 findings 1, 2, 6 and 8 (ux-lab
 * round21/JUDGE-R21-3.md). */
import { describe, expect, it } from "vitest";
import { code } from "./sourceText";

import { boundsOf, fitCamera, firstFit, type Rect } from "../lib/skyLayout";

const sky = code("components/flights/SkyView.tsx");

/** Whether every rect is wholly inside a vw × vh window under camera c. */
const allOnScreen = (rects: Rect[], c: { x: number; y: number; z: number }, vw: number, vh: number) =>
  rects.every((r) => r.x * c.z + c.x >= -0.5 && r.y * c.z + c.y >= -0.5 && (r.x + r.w) * c.z + c.x <= vw + 0.5 && (r.y + r.h) * c.z + c.y <= vh + 0.5);

describe("finding 1: 0, Shift+1, the zoom menu's Fit all and the Fit button are ONE fit", () => {
  // The judge's 12-session sky: routes in a tall stack, a note above them.
  const rects: Rect[] = [
    { x: 40, y: -120, w: 200, h: 160 },
    { x: 300, y: 0, w: 1400, h: 300 },
    { x: 300, y: 340, w: 1400, h: 420 },
    { x: 300, y: 800, w: 1400, h: 400 },
    { x: 300, y: 1240, w: 1400, h: 380 },
  ];
  const VW = 1143;
  const VH = 780;

  it("the whole fit holds everything; the legible first picture would not (that is why Fit all drops its floor)", () => {
    const b = boundsOf(rects);
    expect(allOnScreen(rects, fitCamera(b, VW, VH, 1), VW, VH)).toBe(true);
    expect(allOnScreen(rects, firstFit(b, VW, VH, 1), VW, VH)).toBe(false);
  });

  it("fitAll is fitCamera over every line, the lane, the right column and the notes — no floor, no `whole` flag", () => {
    const fit = sky.match(/const fitAll = useCallback\(\(instant = false\) => \{[\s\S]*?\n {2}\}, \[/);
    expect(fit).not.toBeNull();
    expect(fit![0]).toContain("goTo(fitCamera(boundsOf(everyRect()), vp.w, vp.h, mode === \"one\" ? 1.5 : 1), instant);");
    expect(fit![0]).not.toContain("firstFit");
    expect(sky).not.toMatch(/fitAll\((true|false)/);
    const every = sky.match(/const everyRect = useCallback\(\(\): Rect\[\] => \{[\s\S]*?\n {2}\}, \[/);
    expect(every).not.toBeNull();
    expect(every![0]).toContain('if (o.def.kind === "note") rects.push({ ...posOf(o.key), ...objectBox(o) });');
  });

  it("every way in calls the same fitAll() — the key 0, Shift+1, the zoom menu and the Fit button", () => {
    expect(sky).toMatch(/e\.shiftKey && \(digit\("1"\) \|\| k === "!"\)\) \{\s*handled\(\);\s*fitAll\(\);/);
    expect(sky).toMatch(/k === "0"\) \{\s*handled\(\);\s*fitAll\(\);/);
    expect(sky).toContain("onFitAll={() => fitAll()}");
    expect(sky).toMatch(/className="sky-zoom-btn sky-fit"[\s\S]{0,200}onClick=\{\(\) => fitAll\(\)\}/);
  });

  it("only the automatic first picture keeps the legibility floor", () => {
    expect(sky.match(/firstFit\(/g)).toHaveLength(1);
    expect(sky).toContain('else if (mode !== "one") firstPicture();');
  });
});
