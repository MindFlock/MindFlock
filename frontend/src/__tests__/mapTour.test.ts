// The Map's first-open experience: the tour (codemap/MapTour.tsx), the
// calmer bird index (a capped folder list) and the preload guard.
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { MAP_TOUR, MapTour, placeCard } from "../components/grid/codemap/MapTour";
import { FOLDERS_SHOWN, shownFolders } from "../components/grid/codemap/BirdIndex";
import { prefetchNeeded } from "../components/grid/CodeMapTab";

describe("the Map tour", () => {
  it("walks the tree, the agents, what could break, the rules and finding things — in that order", () => {
    expect(MAP_TOUR.map((s) => s.title)).toEqual([
      "Your code, drawn as a tree",
      "Bright leaves are what changed",
      "Each agent is a bird",
      "What could break",
      "Fence agents in or out",
      "Getting around",
    ]);
    // each pointer names a real part of the tab
    expect(MAP_TOUR.map((s) => s.target)).toEqual(["", "", '[data-hud="left"]', ".ct-bix-risk-tog", ".ct-rules", ".cm-search"]);
  });

  it("renders as a non-modal card with its step count, Skip and Next", () => {
    const html = renderToStaticMarkup(createElement(MapTour, { rootRef: { current: null }, onClose: () => {} }));
    expect(html).toContain('role="dialog"');
    expect(html).toContain('aria-modal="false"');
    expect(html).toContain("1 of 6");
    expect(html).toContain("Skip tour");
    expect(html).toContain(">Next<");
    expect(html).toContain("Your code, drawn as a tree");
    // no browser dialogs (Electron implements none)
    expect(html).not.toMatch(/prompt\(|confirm\(|alert\(/);
  });

  it("places the card beside a tall left column, below a toolbar control, centred with no target, always inside", () => {
    const W = 1000,
      H = 700,
      cardH = 180;
    // the agents column: to its right
    expect(placeCard({ x: 8, y: 50, w: 240, h: 500 }, W, H, cardH)).toEqual({ left: 260, top: 50 });
    // a toolbar control near the top: below it
    const below = placeCard({ x: 300, y: 8, w: 200, h: 24 }, W, H, cardH);
    expect(below).toEqual({ left: 300, top: 44 });
    // a control at the bottom: above it
    expect(placeCard({ x: 300, y: 650, w: 100, h: 30 }, W, H, cardH).top).toBe(650 - 12 - cardH);
    // a control at the right edge: clamped inside
    expect(placeCard({ x: 950, y: 8, w: 40, h: 24 }, W, H, cardH).left).toBe(W - 320 - 8);
    // nothing to point at: centred
    expect(placeCard(null, W, H, cardH)).toEqual({ left: 340, top: 260 });
    // a tiny pane: still on screen
    const tiny = placeCard(null, 300, 200, 180);
    expect(tiny.left).toBeGreaterThanOrEqual(8);
    expect(tiny.top).toBeGreaterThanOrEqual(8);
  });
});

describe("the bird index lists a few changed folders, not all of them", () => {
  const folders = Array.from({ length: 12 }, (_, i) => ({ id: i, ruled: i === 9 }));
  it("shows the first few plus any folder a rule covers; all on request", () => {
    expect(FOLDERS_SHOWN).toBe(5);
    expect(shownFolders(folders, false, (f) => f.ruled).map((f) => f.id)).toEqual([0, 1, 2, 3, 4, 9]);
    expect(shownFolders(folders, true, (f) => f.ruled)).toHaveLength(12);
    expect(shownFolders(folders.slice(0, 4), false, () => false)).toHaveLength(4);
  });
});

describe("preloading the Map", () => {
  it("does nothing once the snapshot and the laid-out tree are both cached, or while one is in flight", () => {
    expect(prefetchNeeded(false, false, false)).toBe(true);
    expect(prefetchNeeded(true, false, false)).toBe(true);
    expect(prefetchNeeded(true, true, false)).toBe(false);
    expect(prefetchNeeded(false, false, true)).toBe(false);
  });
});
