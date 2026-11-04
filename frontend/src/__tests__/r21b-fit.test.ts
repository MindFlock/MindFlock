/** Round 21b "fit": the judge's pass-2 findings 1, 2, 6 and 8 (ux-lab
 * round21/JUDGE-R21-3.md). */
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";
import { code, css, cssRule } from "./sourceText";

import { boundsOf, fitCamera, firstFit, type Rect } from "../lib/skyLayout";
import { OffscreenHints, edgeChips, type MinimapRect } from "../components/flights/Minimap";
import { CARD_PAD, cardCamera } from "../lib/skyCamera";

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

describe("finding 2: the edge chips stack in a gutter inside the visible sky", () => {
  const VIEW = { w: 1000, h: 700 };
  const cam = { x: 0, y: 0, z: 1 };
  const line = (id: string, x: number, y: number, w = 400, needsYou = false): MinimapRect => ({ id, x, y, w, h: 100, kind: "line", label: id, needsYou });

  it("a route at least half on screen gets no chip — needing you or not; mostly off and needing you gets one", () => {
    // 60 % on screen (600 of 1000 px wide inside a 1000 px window from x=400).
    expect(edgeChips([line("pay", 400, 100, 1000, true)], cam, VIEW)).toEqual([]);
    // 40 % on screen.
    expect(edgeChips([line("pay", 600, 100, 1000, true)], cam, VIEW)).toEqual([{ id: "pay", edge: "right", needs: true }]);
    // Mostly off, but it doesn't need you: no chip (only wholly-off routes get a plain one).
    expect(edgeChips([line("idle", 600, 100, 1000)], cam, VIEW)).toEqual([]);
    expect(edgeChips([line("idle", 1100, 100)], cam, VIEW)).toEqual([{ id: "idle", edge: "right", needs: false }]);
  });

  it("each edge's stack puts the ones that need you first, then the rest along the edge; the cap keeps them", () => {
    const rects = [line("b", 1100, 500), line("a", 1100, 100), line("pay", 1100, 600, 400, true), line("down", 300, 900)];
    expect(edgeChips(rects, cam, VIEW).map((c) => c.edge + ":" + c.id)).toEqual(["right:pay", "right:a", "right:b", "bottom:down"]);
    const many = [...Array.from({ length: 10 }, (_, i) => line("x" + i, 1100 + i * 10, 100)), line("far-pay", 9000, 100, 400, true)];
    const kept = edgeChips(many, cam, VIEW, 8);
    expect(kept).toHaveLength(8);
    expect(kept[0]).toMatchObject({ id: "far-pay", needs: true });
  });

  it("an open drawer over the sky narrows what is visible: a route under it gets its chip, the gutter keeps left of it", () => {
    const rects = [line("under", 750, 100, 200, true)];
    expect(edgeChips(rects, cam, VIEW)).toEqual([]);
    expect(edgeChips(rects, cam, { w: 700, h: 700 })).toEqual([{ id: "under", edge: "right", needs: true }]);
    const html = renderToStaticMarkup(createElement(OffscreenHints, { rects, cam, viewport: VIEW, inset: { right: 300 }, onJump: () => {} }));
    expect(html).toMatch(/class="sky-offscreen"[^>]*style="right:300px;bottom:0"/);
  });

  it("OffscreenHints draws one gutter per edge, the chips inside it in stack order, no per-chip coordinates", () => {
    const rects = [line("b", 1100, 500), line("pay", 1100, 600, 400, true), line("down", 300, 900)];
    const html = renderToStaticMarkup(createElement(OffscreenHints, { rects, cam, viewport: VIEW, onJump: () => {} }));
    const right = html.slice(html.indexOf('class="sky-offscreen-gutter edge-right"'), html.indexOf('class="sky-offscreen-gutter edge-bottom"'));
    expect(right.indexOf('data-id="pay"')).toBeGreaterThan(-1);
    expect(right.indexOf('data-id="pay"')).toBeLessThan(right.indexOf('data-id="b"'));
    expect(html).toContain('class="sky-offscreen-gutter edge-bottom"');
    expect(html).not.toMatch(/sky-offscreen-chip[^>]*style="[^"]*(left|top|right|bottom):/);
  });

  it("Minimap.css: a grid of edge gutters, chips 4 px apart in flow, the ones that need you on top", () => {
    const text = css("components/flights/Minimap.css");
    expect(cssRule(text, ".sky-offscreen")).toMatch(/display:\s*grid/);
    expect(cssRule(text, ".sky-offscreen")).toContain('"left mid right"');
    expect(cssRule(text, ".sky-offscreen .sky-offscreen-gutter")).toMatch(/gap:\s*4px/);
    expect(cssRule(text, ".sky-offscreen .sky-offscreen-gutter.edge-right")).toMatch(/grid-area:\s*right/);
    expect(cssRule(text, ".sky-offscreen .sky-offscreen-gutter.edge-bottom")).toMatch(/grid-area:\s*bottom/);
    expect(cssRule(text, ".sky-offscreen .sky-offscreen-chip")).toMatch(/position:\s*relative/);
    expect(text).not.toMatch(/sky-offscreen-chip\.edge-(left|right|top|bottom)\s*\{[^}]*transform/);
    expect(cssRule(text, ".sky-offscreen .sky-offscreen-chip.is-needs")).toMatch(/z-index:\s*1/);
  });
});

describe("finding 6 (brief 3): Enter zooms to at least 100 % centred on the open card; Esc goes back", () => {
  it("from the route's 75 % fit: 100 %, the card's centre at the window's centre", () => {
    const c = { x: 60, y: 300, z: 0.75 };
    // The judge's card: 541 × 368 at 75 % (viewport px).
    const r = { x: 60, y: 331, w: 541, h: 368 };
    const to = cardCamera(c, r, 1143, 780);
    expect(to.z).toBe(1);
    // The card's plane centre lands on the window's centre.
    const px = (r.x + r.w / 2 - c.x) / c.z;
    const py = (r.y + r.h / 2 - c.y) / c.z;
    expect(px * to.z + to.x).toBeCloseTo(1143 / 2, 6);
    expect(py * to.z + to.y).toBeCloseTo(780 / 2, 6);
  });

  it("already closer than 100 %: the zoom stays; a card bigger than the window starts top-left, CARD_PAD in", () => {
    expect(cardCamera({ x: 0, y: 0, z: 1.4 }, { x: 100, y: 100, w: 400, h: 300 }, 1143, 780).z).toBe(1.4);
    const big = cardCamera({ x: 0, y: 0, z: 0.5 }, { x: 200, y: 100, w: 700, h: 500 }, 1143, 780);
    expect(big.z).toBe(1);
    expect((200 / 0.5) * big.z + big.x).toBeCloseTo(CARD_PAD, 6);
    expect((100 / 0.5) * big.z + big.y).toBeCloseTo(CARD_PAD, 6);
  });

  it("the sky wires it: Enter keeps the camera it leaves and glides by cardCamera; Esc (focused or This session) closes and restores", () => {
    expect(sky).toMatch(/beforeEnter\.current = \{ id, cam: camRef\.current \};/);
    expect(sky).toContain("goToRef.current(cardCamera(camRef.current, { x: c.left - v.left, y: c.top - v.top, w: c.width, h: c.height }, v.width, v.height));");
    expect(sky).toMatch(/k === "Escape" && \(focusLine \|\| oneLine\) && layout\[focusLine \|\| oneLine\]\?\.open\) \{\s*handled\(\);\s*const id = focusLine \|\| oneLine;\s*onLayout\(id, \{ open: false \}\);\s*restoreAfterEnter\(id\);/);
    expect(sky).toMatch(/if \(mode === "one"\) autoFit\.current = true;\s*else goTo\(was\.cam\);/);
  });
});
