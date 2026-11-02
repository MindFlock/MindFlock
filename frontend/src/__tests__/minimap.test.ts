/** The minimap and the off-screen hints (docs/flights.md "As built: round 17
 * minimap"): lib/minimap's maths (the map's fit, plane ↔ map, the viewport
 * rectangle, a drag and a click as cameras, the blocks, the edge hints), then
 * the two components — rendered to markup (blocks, the viewport rectangle,
 * the folded glyph) and driven through their handlers (they are hookless,
 * and this suite has no DOM: a click calls onJump, a drag and an empty-map
 * click call onCamera). */

import { describe, expect, it, vi } from "vitest";
import { createElement, isValidElement, type ReactElement, type ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { css } from "./sourceText";
import {
  HINT_INSET,
  HINT_MAX,
  MINIMAP_H,
  MINIMAP_W,
  centreAt,
  dragCamera,
  edgeHints,
  fromMap,
  mapBlocks,
  mapScale,
  minimapOpen,
  pickBlock,
  shortLabel,
  toMap,
  toneVar,
  viewportRect,
} from "../lib/minimap";
import { Minimap, OffscreenHints, type MinimapProps, type MinimapRect } from "../components/flights/Minimap";

const MAP = { w: MINIMAP_W, h: MINIMAP_H };
const VIEW = { w: 1000, h: 600 };

const line = (id: string, x: number, y: number, extra: Partial<MinimapRect> = {}): MinimapRect => ({ id, x, y, w: 400, h: 200, kind: "line", tone: "working", label: id, ...extra });

const RECTS: MinimapRect[] = [
  { id: "intake", x: 0, y: 0, w: 240, h: 1800, kind: "intake" },
  line("a", 400, 0),
  line("b", 1000, 0, { tone: "asking", selected: true }),
  line("c", 3200, 1400, { tone: "idle" }),
  { id: "note1", x: 400, y: 600, w: 200, h: 120, kind: "note", label: "A note" },
];

describe("lib/minimap: the fit", () => {
  it("scales a big plane into the map with 8 px round it, centred", () => {
    const fit = mapScale(RECTS, MAP, 8);
    // bounds 0..3600 × 0..1800: width-bound (164/3600) vs height (104/1800)
    expect(fit.scale).toBeCloseTo(164 / 3600, 6);
    const tl = toMap(fit, { x: 0, y: 0 });
    const br = toMap(fit, { x: 3600, y: 1800 });
    expect(tl.x).toBeCloseTo(8, 6);
    expect(br.x).toBeCloseTo(172, 6);
    // vertically centred
    expect(tl.y).toBeCloseTo(MAP.h - br.y, 6);
  });

  it("never blows a small plane up past 1:1", () => {
    const fit = mapScale([{ x: 10, y: 10, w: 20, h: 20 }], MAP, 8);
    expect(fit.scale).toBe(1);
    expect(toMap(fit, { x: 20, y: 20 })).toEqual({ x: 90, y: 60 });
  });

  it("round-trips plane ↔ map", () => {
    const fit = mapScale(RECTS, MAP);
    const p = { x: 1234, y: 567 };
    const back = fromMap(fit, toMap(fit, p));
    expect(back.x).toBeCloseTo(p.x, 6);
    expect(back.y).toBeCloseTo(p.y, 6);
  });

  it("nothing to draw: a harmless 1:1 fit", () => {
    expect(mapScale([], MAP, 8)).toEqual({ scale: 1, ox: 8, oy: 8 });
  });
});

describe("lib/minimap: the camera", () => {
  it("the viewport rectangle is what the camera shows, in plane px", () => {
    expect(viewportRect({ x: -200, y: -100, z: 2 }, VIEW)).toEqual({ x: 100, y: 50, w: 500, h: 300 });
    expect(viewportRect({ x: 0, y: 0, z: 1 }, VIEW)).toEqual({ x: 0, y: 0, w: 1000, h: 600 });
  });

  it("centreAt puts a plane point mid-viewport at the same zoom", () => {
    const c = centreAt({ x: 800, y: 400 }, VIEW, 0.5);
    expect(c.z).toBe(0.5);
    expect(800 * c.z + c.x).toBe(500);
    expect(400 * c.z + c.y).toBe(300);
  });

  it("dragging the rectangle n map px moves the view n / scale plane px, zoom kept", () => {
    const fit = { scale: 0.05, ox: 0, oy: 0 };
    const cam0 = { x: -100, y: -50, z: 1.5 };
    const c = dragCamera(cam0, fit, 10, -4);
    expect(c.z).toBe(1.5);
    const v0 = viewportRect(cam0, VIEW);
    const v1 = viewportRect(c, VIEW);
    expect(v1.x - v0.x).toBeCloseTo(200, 6);
    expect(v1.y - v0.y).toBeCloseTo(-80, 6);
  });

  it("remembers the fold: open unless folded", () => {
    expect(minimapOpen(null)).toBe(true);
    expect(minimapOpen("1")).toBe(true);
    expect(minimapOpen("0")).toBe(false);
  });
});

describe("lib/minimap: blocks, tones, labels", () => {
  it("lanes and strips are thin bars; lines draw over objects", () => {
    const fit = mapScale(RECTS, MAP);
    const blocks = mapBlocks(RECTS, fit);
    const intake = blocks.find((b) => b.id === "intake")!;
    expect(intake.bar).toBe(true);
    expect(intake.w).toBe(3);
    expect(intake.h).toBeGreaterThan(50);
    expect(blocks[0].id).toBe("intake");
    expect(blocks.findIndex((b) => b.id === "note1")).toBeLessThan(blocks.findIndex((b) => b.id === "a"));
    const strip = mapBlocks([{ id: "landing", x: 0, y: 0, w: 300, h: 100, kind: "landing" }], { scale: 0.1, ox: 0, oy: 0 })[0];
    expect(strip.h).toBe(3);
  });

  it("picks the block under a point, or nothing", () => {
    const fit = mapScale(RECTS, MAP);
    const blocks = mapBlocks(RECTS, fit);
    const c = toMap(fit, { x: 3400, y: 1500 });
    expect(pickBlock(blocks, c.x, c.y)).toBe("c");
    const gap = toMap(fit, { x: 2400, y: 1000 });
    expect(pickBlock(blocks, gap.x, gap.y)).toBe("");
  });

  it("reads the state tones the way the birds do", () => {
    expect(toneVar("working")).toBe("var(--tone-working)");
    expect(toneVar("asking")).toBe("var(--tone-attention)");
    expect(toneVar("waiting_you")).toBe("var(--tone-attention)");
    expect(toneVar("halted")).toBe("var(--tone-broken)");
    expect(toneVar("idle")).toBe("var(--tone-neutral)");
    expect(toneVar("done")).toBe("var(--tone-neutral)");
    expect(toneVar("")).toBe("var(--tone-muted)");
  });

  it("cuts a title at 14 characters with an ellipsis", () => {
    expect(shortLabel("short")).toBe("short");
    expect(shortLabel("exactly-14-chr")).toBe("exactly-14-chr");
    const cut = shortLabel("a much longer session title");
    expect([...cut].length).toBeLessThanOrEqual(14);
    expect(cut.endsWith("…")).toBe(true);
  });
});

describe("lib/minimap: edge hints", () => {
  const cam = { x: 0, y: 0, z: 1 };

  it("only rects wholly off screen get a chip, pinned 8 px in from the nearest edge", () => {
    const rects = [
      { id: "on", x: 100, y: 100, w: 100, h: 100 },
      { id: "half", x: 950, y: 100, w: 100, h: 100 },
      { id: "right", x: 1400, y: 250, w: 100, h: 100 },
      { id: "left", x: -500, y: 200, w: 100, h: 100 },
      { id: "below", x: 400, y: 900, w: 100, h: 50 },
      { id: "above", x: 300, y: -300, w: 100, h: 50 },
    ];
    const hints = edgeHints(rects, cam, VIEW, 8);
    const by = Object.fromEntries(hints.map((h) => [h.id, h]));
    expect(Object.keys(by).sort()).toEqual(["above", "below", "left", "right"]);
    expect(by.right).toMatchObject({ edge: "right", x: VIEW.w - HINT_INSET, y: 300 });
    expect(by.left).toMatchObject({ edge: "left", x: HINT_INSET, y: 250 });
    expect(by.below).toMatchObject({ edge: "bottom", x: 450, y: VIEW.h - HINT_INSET });
    expect(by.above).toMatchObject({ edge: "top", x: 350, y: HINT_INSET });
  });

  it("clamps along the edge so the chip stays whole", () => {
    const [h] = edgeHints([{ id: "far", x: 3000, y: 5000, w: 10, h: 10 }], cam, VIEW, 8);
    // past the bottom-right corner, further down than right → bottom edge, x clamped
    expect(h.edge).toBe("bottom");
    expect(h.x).toBeLessThanOrEqual(VIEW.w - HINT_INSET);
    expect(h.y).toBe(VIEW.h - HINT_INSET);
  });

  it("keeps the nearest 8, nearest first", () => {
    const rects = Array.from({ length: 12 }, (_, i) => ({ id: "r" + i, x: 1100 + i * 300, y: 300, w: 50, h: 50 }));
    const hints = edgeHints(rects, cam, VIEW, HINT_MAX);
    expect(hints).toHaveLength(8);
    expect(hints.map((h) => h.id)).toEqual(["r0", "r1", "r2", "r3", "r4", "r5", "r6", "r7"]);
    for (let i = 1; i < hints.length; i++) expect(hints[i].dist).toBeGreaterThanOrEqual(hints[i - 1].dist);
  });

  it("chips on one edge don't stack", () => {
    const rects = [0, 1, 2].map((i) => ({ id: "s" + i, x: 1200 + i * 10, y: 300, w: 50, h: 50 }));
    const ys = edgeHints(rects, cam, VIEW, 8)
      .map((h) => h.y)
      .sort((a, b) => a - b);
    expect(ys[1] - ys[0]).toBeGreaterThanOrEqual(22);
    expect(ys[2] - ys[1]).toBeGreaterThanOrEqual(22);
  });

  it("follows the camera: zoom and pan", () => {
    // at z 0.5, x 1400 is screen 700 — on screen
    expect(edgeHints([{ id: "a", x: 1400, y: 200, w: 100, h: 100 }], { x: 0, y: 0, z: 0.5 }, VIEW)).toHaveLength(0);
    // panned left by 2000 screen px: now it is off the left
    const [h] = edgeHints([{ id: "a", x: 1400, y: 200, w: 100, h: 100 }], { x: -2000, y: 0, z: 1 }, VIEW);
    expect(h.edge).toBe("left");
  });
});

// --- The components -----------------------------------------------------------------

const props = (over: Partial<MinimapProps> = {}): MinimapProps => ({
  rects: RECTS,
  cam: { x: -300, y: 0, z: 1 },
  viewport: VIEW,
  onCamera: vi.fn(),
  onJump: vi.fn(),
  open: true,
  onToggle: vi.fn(),
  ...over,
});

/** The first element in a tree whose className has `cls`. */
function find(node: ReactNode, cls: string): ReactElement<Record<string, unknown>> | null {
  if (Array.isArray(node)) {
    for (const n of node) {
      const hit = find(n, cls);
      if (hit) return hit;
    }
    return null;
  }
  if (!isValidElement(node)) return null;
  const p = node.props as { className?: string; children?: ReactNode };
  if (typeof p.className === "string" && p.className.split(" ").includes(cls)) return node as ReactElement<Record<string, unknown>>;
  return find(p.children, cls);
}

/** A pointer event on the map element at map point (x, y). */
function ptr(target: object, x: number, y: number) {
  return {
    button: 0,
    pointerId: 1,
    clientX: x,
    clientY: y,
    currentTarget: target,
    stopPropagation() {},
    preventDefault() {},
  };
}
const mapEl = () => ({ getBoundingClientRect: () => ({ left: 0, top: 0 }), setPointerCapture() {} });

describe("Minimap", () => {
  it("draws a block per rect and the viewport rectangle", () => {
    const html = renderToStaticMarkup(createElement(Minimap, props()));
    expect(html).toContain('class="sky-minimap"');
    expect(html.match(/class="sky-minimap-block/g)).toHaveLength(RECTS.length);
    expect(html.match(/sky-minimap-block is-line/g)).toHaveLength(3);
    expect(html).toContain("sky-minimap-block is-bar");
    expect(html).toContain("sky-minimap-block is-object");
    expect(html).toContain("is-selected");
    expect(html).toContain("--mm-tone:var(--tone-attention)");
    expect(html).toContain('title="A note"');
    expect(html).toContain("sky-minimap-view");
    expect(html).toContain(`width:${MINIMAP_W}px;height:${MINIMAP_H}px`);
    expect(html).toContain("◰");
  });

  it("puts the viewport rectangle where the camera looks", () => {
    const p = props();
    const fit = mapScale(RECTS, MAP);
    const v = viewportRect(p.cam, p.viewport);
    const tl = toMap(fit, v);
    const el = find(Minimap(p), "sky-minimap-view")!;
    const st = el.props.style as { left: number; top: number; width: number; height: number };
    expect(st.left).toBeCloseTo(tl.x, 6);
    expect(st.top).toBeCloseTo(tl.y, 6);
    expect(st.width).toBeCloseTo(v.w * fit.scale, 6);
    expect(st.height).toBeCloseTo(v.h * fit.scale, 6);
  });

  it("folded: just the glyph, and it opens", () => {
    const p = props({ open: false });
    const html = renderToStaticMarkup(createElement(Minimap, p));
    expect(html).toContain("is-folded");
    expect(html).toContain("◰");
    expect(html).not.toContain("sky-minimap-block");
    expect(html).not.toContain("sky-minimap-view");
    const glyph = find(Minimap(p), "sky-minimap-glyph")!;
    (glyph.props.onClick as () => void)();
    expect(p.onToggle).toHaveBeenCalledTimes(1);
  });

  it("the head's glyph folds it", () => {
    const p = props();
    (find(Minimap(p), "sky-minimap-glyph")!.props.onClick as () => void)();
    expect(p.onToggle).toHaveBeenCalledTimes(1);
  });

  it("a click on a block jumps to it", () => {
    const p = props();
    const map = find(Minimap(p), "sky-minimap-map")!;
    const fit = mapScale(RECTS, MAP);
    const at = toMap(fit, { x: 3400, y: 1500 });
    const el = mapEl();
    (map.props.onPointerDown as (e: unknown) => void)(ptr(el, at.x, at.y));
    (map.props.onPointerUp as (e: unknown) => void)(ptr(el, at.x, at.y));
    expect(p.onJump).toHaveBeenCalledWith("c");
    expect(p.onCamera).not.toHaveBeenCalled();
  });

  it("a click on empty map centres the camera there, same zoom", () => {
    const p = props({ cam: { x: -300, y: 0, z: 0.8 } });
    const map = find(Minimap(p), "sky-minimap-map")!;
    const fit = mapScale(RECTS, MAP);
    const at = toMap(fit, { x: 2400, y: 1000 });
    const el = mapEl();
    (map.props.onPointerDown as (e: unknown) => void)(ptr(el, at.x, at.y));
    (map.props.onPointerUp as (e: unknown) => void)(ptr(el, at.x, at.y));
    expect(p.onJump).not.toHaveBeenCalled();
    const cam = (p.onCamera as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(cam.z).toBe(0.8);
    expect(2400 * cam.z + cam.x).toBeCloseTo(VIEW.w / 2, 4);
    expect(1000 * cam.z + cam.y).toBeCloseTo(VIEW.h / 2, 4);
  });

  it("dragging the viewport rectangle moves the camera, zoom kept, no jump", () => {
    const p = props();
    const map = find(Minimap(p), "sky-minimap-map")!;
    const v = find(Minimap(p), "sky-minimap-view")!.props.style as { left: number; top: number; width: number; height: number };
    const fit = mapScale(RECTS, MAP);
    const sx = v.left + v.width / 2;
    const sy = v.top + v.height / 2;
    const el = mapEl();
    (map.props.onPointerDown as (e: unknown) => void)(ptr(el, sx, sy));
    (map.props.onPointerMove as (e: unknown) => void)(ptr(el, sx + 20, sy + 5));
    (map.props.onPointerUp as (e: unknown) => void)(ptr(el, sx + 20, sy + 5));
    expect(p.onJump).not.toHaveBeenCalled();
    const calls = (p.onCamera as ReturnType<typeof vi.fn>).mock.calls;
    expect(calls).toHaveLength(1);
    const cam = calls[0][0];
    expect(cam.z).toBe(p.cam.z);
    expect(cam.x).toBeCloseTo(p.cam.x - (20 / fit.scale) * p.cam.z, 4);
    expect(cam.y).toBeCloseTo(p.cam.y - (5 / fit.scale) * p.cam.z, 4);
  });

  it("empty sky: a quiet note, no viewport, presses do nothing", () => {
    const p = props({ rects: [] });
    const html = renderToStaticMarkup(createElement(Minimap, p));
    expect(html).toContain("Nothing on the sky yet");
    expect(html).not.toContain("sky-minimap-view");
  });
});

describe("OffscreenHints", () => {
  const far: MinimapRect[] = [
    line("near-one", 1200, 100, { label: "a very long session name indeed", tone: "asking" }),
    line("on-screen", 100, 100),
    { id: "note-off", x: 5000, y: 100, w: 100, h: 100, kind: "note", label: "note" },
    ...Array.from({ length: 10 }, (_, i) => line("x" + i, 2000 + i * 500, 100)),
  ];

  it("a chip per off-screen line (never objects), at most 8, title cut at 14", () => {
    const html = renderToStaticMarkup(createElement(OffscreenHints, { rects: far, cam: { x: 0, y: 0, z: 1 }, viewport: VIEW, onJump: () => {} }));
    expect(html.match(/class="sky-offscreen-chip/g)).toHaveLength(8);
    expect(html).not.toContain('data-id="note-off"');
    expect(html).not.toContain('data-id="on-screen"');
    expect(html).toContain('data-id="near-one"');
    expect(html).toContain("a very long s…");
    expect(html).toContain('title="Go to a very long session name indeed"');
    expect(html).toContain("--mm-tone:var(--tone-attention)");
    expect(html).toContain("edge-right");
  });

  it("a click jumps to the line", () => {
    const onJump = vi.fn();
    const tree = OffscreenHints({ rects: far, cam: { x: 0, y: 0, z: 1 }, viewport: VIEW, onJump });
    const chip = find(tree, "sky-offscreen-chip")!;
    (chip.props.onClick as (e: unknown) => void)({ stopPropagation() {} });
    expect(onJump).toHaveBeenCalledWith("near-one");
  });

  it("nothing at lod far, nothing when all is on screen", () => {
    expect(OffscreenHints({ rects: far, cam: { x: 0, y: 0, z: 0.3 }, viewport: VIEW, onJump: () => {} })).toBeNull();
    expect(OffscreenHints({ rects: [line("a", 0, 0)], cam: { x: 0, y: 0, z: 1 }, viewport: VIEW, onJump: () => {} })).toBeNull();
  });
});

describe("Minimap.css", () => {
  const text = css("components/flights/Minimap.css");

  it("absolute, hidden under 520 px, no transitions under reduced motion", () => {
    expect(text).toMatch(/\.sky-minimap\s*\{[^}]*position:\s*absolute/);
    expect(text).toMatch(/@media \(max-width: 520px\)\s*\{\s*\.sky-minimap\s*\{\s*display:\s*none/);
    expect(text).toMatch(/@media \(prefers-reduced-motion: reduce\)[^@]*transition:\s*none/);
  });

  it("chips are 22 px and the viewport wears the accent", () => {
    expect(text).toMatch(/\.sky-offscreen-chip\s*\{[^}]*height:\s*22px/);
    // the chip tier (.fl-pick) carries a 28 px min-height: the hint undoes it
    expect(text).toMatch(/\.sky-offscreen-chip\s*\{[^}]*min-height:\s*0/);
    expect(text).toMatch(/\.sky-minimap-view\s*\{[^}]*var\(--accent\)/);
  });
});
