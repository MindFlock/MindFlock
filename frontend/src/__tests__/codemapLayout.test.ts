// The Map's picture and layout at every pane size: the whole-tree fit
// (lib/codetree/fit), where the tour's card goes (codemap/tourPlace), who
// gets "/" (CodeTree's slashFinds) and the CSS that docks the Session panel,
// pins a small pane's card actions and keeps toasts off the agents column.
import { describe, expect, it } from "vitest";
import { boxOnScreen, fitWhole, pictureBox, WHOLE_MAX_Z, WHOLE_PAD } from "../lib/codetree/fit";
import { S0 } from "../lib/codetree/model";
import { placeMapCard, type MapScene } from "../components/grid/codemap/tourPlace";
import { slashFinds } from "../components/grid/codemap/CodeTree";
import { css } from "./sourceText";

describe("the whole-tree view", () => {
  const I = { top: 8, bot: 8, left: 240, right: 8 };
  it("fits the leaves themselves, not the model's padded bounds", () => {
    const B = pictureBox({ bounds: { x0: -100 - S0 * 6, y0: -300 - S0 * 6, x1: 200 + S0 * 6, y1: 5 + S0 * 6 }, groundY: 40, leaves: [{}] as never[] });
    expect(B).toEqual({ x0: -100, y0: -300, x1: 200, y1: 40 }); // the ground line under the trunk counts
  });

  it("a wide, short tree: centred across the free canvas, a band of soil — never a third of the pane", () => {
    const B = { x0: -600, y0: -200, x1: 600, y1: 0 };
    const W = 1320,
      H = 860;
    const c = fitWhole(B, W, H, I);
    const s = boxOnScreen(B, c, W, H);
    // as wide as the free canvas allows
    expect(s.x0).toBeCloseTo(I.left + WHOLE_PAD.side, 3);
    expect(s.x1).toBeCloseTo(W - I.right - WHOLE_PAD.side, 3);
    // the spare height lifts the picture off the bottom, but the soil under it stays a band
    const h = H - I.top - I.bot - WHOLE_PAD.top - WHOLE_PAD.bot;
    expect(H - s.y1).toBeCloseTo(I.bot + WHOLE_PAD.bot + h * 0.12, 3);
    expect((H - s.y1) / H).toBeLessThan(0.16);
    expect(s.y0).toBeGreaterThan(H * 0.4);
  });

  it("the soil is as deep as the roots need: the same thin band under the deepest root tip as under a rootless trunk", () => {
    // (no scaffold band any more: docs, CI and config are roots, so the picture's box already holds them)
    const W = 1320,
      H = 860;
    const h = H - I.top - I.bot - WHOLE_PAD.top - WHOLE_PAD.bot;
    const bare = { x0: -600, y0: -200, x1: 600, y1: 0 },
      rooted = { x0: -600, y0: -200, x1: 600, y1: 60 };
    for (const B of [bare, rooted]) expect(H - boxOnScreen(B, fitWhole(B, W, H, I), W, H).y1).toBeCloseTo(I.bot + WHOLE_PAD.bot + h * 0.12, 3);
    expect(Object.keys(WHOLE_PAD).sort()).toEqual(["bot", "side", "top"]);
  });

  it("a tiny tree is drawn large but not past the zoom cap, in the middle of the free canvas", () => {
    const B = { x0: -20, y0: -30, x1: 20, y1: 0 };
    const c = fitWhole(B, 1320, 860, I);
    expect(c.z).toBe(WHOLE_MAX_Z);
    const s = boxOnScreen(B, c, 1320, 860);
    expect((s.x0 + s.x1) / 2).toBeCloseTo((I.left + 1320 - I.right) / 2, 3);
    expect((s.y0 + s.y1) / 2).toBeCloseTo((I.top + WHOLE_PAD.top + 860 - I.bot - WHOLE_PAD.bot) / 2, 3);
  });

  it("a tall tree fills the height, centred across what is left of the bird index and a panel", () => {
    const B = { x0: -100, y0: -900, x1: 100, y1: 300 };
    const W = 1000,
      H = 700;
    const J = { top: 8, bot: 8, left: 240, right: 348 };
    const c = fitWhole(B, W, H, J);
    const s = boxOnScreen(B, c, W, H);
    expect(s.y0).toBeCloseTo(J.top + WHOLE_PAD.top, 3);
    expect(s.y1).toBeCloseTo(H - J.bot - WHOLE_PAD.bot, 3);
    expect((s.x0 + s.x1) / 2).toBeCloseTo((J.left + W - J.right) / 2, 3);
  });
});

describe("the Map tour's card", () => {
  const W = 1320,
    H = 900,
    cardH = 180;
  const area = { x: 0, y: 40, w: W, h: H - 40 };
  const column = { x: 8, y: 48, w: 224, h: 360 };
  const activity = { x: 1012, y: 860, w: 300, h: 32 };
  const scene: MapScene = { area, tree: { x: 500, y: 420, w: 800, h: 460 }, avoid: [column, activity] };
  const rect = (p: { left: number; top: number }) => ({ x: p.left, y: p.top, w: 320, h: cardH });
  const covers = (r: { x: number; y: number; w: number; h: number }, x: number, y: number) => x > r.x && x < r.x + r.w && y > r.y && y < r.y + r.h;
  const ov = (a: { x: number; y: number; w: number; h: number }, b: { x: number; y: number; w: number; h: number }) =>
    Math.max(0, Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x)) * Math.max(0, Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y));

  it("docks in a corner of the canvas, off the tree and the panels, when the step is about the whole tree", () => {
    const r = rect(placeMapCard(null, W, H, cardH, scene));
    expect(ov(r, scene.tree!)).toBe(0);
    expect(ov(r, column)).toBe(0);
    expect(r.x + r.w).toBeLessThanOrEqual(W - 8); // a corner: the right edge
    expect(r.y).toBe(area.y + 8); // the sky, top right
  });

  it("never covers the tree's centre, even when the tree fills the canvas", () => {
    const full: MapScene = { area, tree: { x: 240, y: 48, w: 1072, h: 840 }, avoid: [column] };
    const r = rect(placeMapCard(null, W, H, cardH, full));
    expect(covers(r, 240 + 536, 48 + 420)).toBe(false);
  });

  it("goes beside its target on the side with the most room: right of the agents column, below the search box", () => {
    const r1 = rect(placeMapCard(column, W, H, cardH, scene));
    expect(r1.x).toBeGreaterThanOrEqual(column.x + column.w);
    const search = { x: 1000, y: 8, w: 220, h: 24 };
    const r2 = rect(placeMapCard(search, W, H, cardH, scene));
    expect(r2.y).toBeGreaterThanOrEqual(search.y + search.h);
    expect(ov(r2, search)).toBe(0);
    // a button inside the column: past the column, not over it
    const risk = { x: 130, y: 240, w: 90, h: 20 };
    const r3 = rect(placeMapCard(risk, W, H, cardH, scene));
    expect(ov(r3, risk)).toBe(0);
    expect(covers(r3, risk.x + risk.w / 2, risk.y + risk.h / 2)).toBe(false);
    expect(r3.x).toBeGreaterThanOrEqual(column.x + column.w);
  });

  it("keeps off the picture's parts, even when the zoomed tree fills the canvas", () => {
    // zoomed onto a file: its card top right, the agents column top left, Activity bottom right, a root fan
    // bottom left — the tree's box covers everything
    const docs = { x: 280, y: 790, w: 340, h: 120 };
    const zoomed: MapScene = {
      area,
      tree: { x: 0, y: 40, w: W, h: H - 40 },
      parts: [{ x: 880, y: 410, w: 280, h: 200 }, docs],
      avoid: [column, { x: 1280, y: 48, w: 300, h: 380 }, activity],
    };
    const r = rect(placeMapCard(null, W, H, cardH, zoomed));
    expect(ov(r, docs)).toBe(0);
    expect(ov(r, zoomed.parts![0])).toBe(0);
    expect(ov(r, column)).toBe(0);
  });

  it("stays inside a small pane", () => {
    const small: MapScene = { area: { x: 0, y: 40, w: 620, h: 520 }, tree: { x: 140, y: 200, w: 470, h: 320 }, avoid: [{ x: 8, y: 48, w: 112, h: 180 }] };
    const p = placeMapCard(null, 620, 560, cardH, small);
    expect(p.left).toBeGreaterThanOrEqual(8);
    expect(p.top).toBeGreaterThanOrEqual(8);
    expect(p.left + 320).toBeLessThanOrEqual(620 - 8);
    expect(p.top + cardH).toBeLessThanOrEqual(560 - 8);
  });
});

describe('"/" finds a file from anywhere in the Map\'s pane', () => {
  function fakeDoc(hovered: string | null) {
    const doc = {
      body: { tagName: "BODY" },
      documentElement: { tagName: "HTML" },
      querySelector: (s: string) => (s === ".pane:hover" && hovered ? {} : null),
    };
    return doc;
  }
  function fakePane(doc: ReturnType<typeof fakeDoc>, opts: { hover?: boolean; focused?: boolean; kids?: unknown[] }) {
    return {
      ownerDocument: doc,
      contains: (el: unknown) => (opts.kids || []).includes(el),
      matches: (s: string) => s === ":hover" && !!opts.hover,
      classList: { contains: (c: string) => c === "focused" && !!opts.focused },
    } as unknown as Element;
  }
  it("from a button or the canvas in the pane, never from a field you type in or another pane", () => {
    const doc = fakeDoc(null);
    const canvas = { tagName: "CANVAS" },
      input = { tagName: "INPUT" },
      other = { tagName: "BUTTON" };
    const pane = fakePane(doc, { kids: [canvas, input] });
    expect(slashFinds(canvas as unknown as Element, pane)).toBe(true);
    expect(slashFinds(input as unknown as Element, pane)).toBe(false);
    expect(slashFinds({ tagName: "DIV", isContentEditable: true } as unknown as Element, pane)).toBe(false);
    expect(slashFinds(other as unknown as Element, pane)).toBe(false);
  });
  it("with nothing focused: the pane under the pointer, else the focused pane", () => {
    const none = fakeDoc(null);
    expect(slashFinds(none.body as unknown as Element, fakePane(none, { hover: true }))).toBe(true);
    expect(slashFinds(null, fakePane(none, { focused: true }))).toBe(true);
    expect(slashFinds(null, fakePane(none, {}))).toBe(false);
    // the pointer is over another pane: that one's map gets it
    const elsewhere = fakeDoc(".pane");
    expect(slashFinds(null, fakePane(elsewhere, { focused: true }))).toBe(false);
  });
});

describe("the Map's layout CSS", () => {
  const map = css("components/grid/CodeMapTab.css");
  it("docks the Session panel beside the canvas in a wide pane; over it, the card stacks above the panel", () => {
    expect(map).toMatch(/\.ct-body\s*\{[^}]*container: ct-body \/ inline-size/);
    const dock = map.slice(map.indexOf("@container ct-body (min-width: 1100px)"));
    expect(dock).toMatch(/\.ct-drawer\s*\{\s*position: relative;/);
    expect(map).toMatch(/\.ct-body:has\(> \.ct-drawer\) \.ct-info\s*\{\s*z-index: 11;/);
  });
  it("a small pane's card sheet scrolls on its own with its actions pinned at its top", () => {
    expect(map).toMatch(/\.ct-stage\.narrow \.ct-info > \.ct-actions\s*\{\s*order: -1;\s*position: sticky;/);
  });
  it("toasts keep right of the agents column and its expanded form", () => {
    expect(map).toMatch(/\.ct-toast\s*\{[^}]*left: 244px;/);
    expect(map).toMatch(/\.ct-stage\.narrow:has\(\.ct-bix-pop\) \.ct-toast\s*\{\s*left: 256px;/);
  });
});
