/** The Sky's birds: the MindFlock sprite as the glyph and the marker (its
 * wing band / body band split at the hinge, the tone → class, flapping only
 * while the flock works and never under reduced motion), the ambient flock
 * (its size, its flight easing out of the middle, the loop that never asks
 * for a frame while paused, nothing at all under reduced motion), and the
 * pane's Flight tab drawn as a route, not a bird. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ALL_CODE, css } from "./sourceText";

const { Bird, BirdMarker, HINGE, SPRITE_H, SPRITE_W, birdLook, spriteBox } = await import("../components/flights/Bird");
const { SkyFlock, fitBirds, flockSize, seedBirds, skyLoop, stepBirds, FRAME_MS } = await import("../components/flights/SkyFlock");
const { TabIcon } = await import("../components/grid/TabIcon");

const g = globalThis as Record<string, unknown>;
function reduceMotion(on: boolean) {
  g.window = { matchMedia: (q: string) => ({ matches: on && q.includes("reduce"), addEventListener() {}, removeEventListener() {} }) };
}
afterEach(() => {
  delete g.window;
});

/** A seeded random, so a flight is the same every run. */
function rng(seed = 7) {
  let s = seed;
  return () => {
    s = (s * 16807) % 2147483647;
    return (s - 1) / 2147483646;
  };
}

describe("the sprite", () => {
  it("is the flock's raven: 216×160, wings above the hinge at 56%", () => {
    expect([SPRITE_W, SPRITE_H, HINGE]).toEqual([216, 160, 0.56]);
    expect(spriteBox(216)).toEqual({ w: 216, h: 160, wings: 89.6, body: 70.4 });
    // The marker's 24px bird: the two bands add back up to the whole sprite.
    const b = spriteBox(24);
    expect(b).toEqual({ w: 24, h: 17.8, wings: 10, body: 7.8 });
    expect(b.wings + b.body).toBeCloseTo(b.h, 5);
  });

  it("the glyph is the sprite through a currentColor mask — no drawn gull", () => {
    const html = renderToStaticMarkup(createElement(Bird, { size: 13, className: "tb-bird" }));
    expect(html).toMatch(/^<svg class="fl-bird tb-bird" width="13" height="13"/);
    expect(html).toContain("background-color:currentColor");
    expect(html).toContain("mask-image:url(/bird.png)");
    expect(html).toContain("mask-size:contain");
    expect(html).not.toContain("<path");
    expect(ALL_CODE).not.toContain("M2 9.5c3.4-2.8 6.8-2.3 10 2 3.2-4.3 6.6-4.8 10-2");
  });
});

describe("the marker", () => {
  it("tone → class: the canvas colours it; only a working flock flaps", () => {
    expect(birdLook("working", false)).toEqual({ className: "fl-bird-marker tone-working", sprite: "fl-bird-big fl-sprite-bird fl-flapping", flaps: true });
    for (const tone of ["you", "halted", "paused"]) {
      const look = birdLook(tone, false);
      expect(look.className).toBe("fl-bird-marker tone-" + tone);
      expect(look.flaps).toBe(false);
      expect(look.sprite).not.toContain("fl-flapping");
    }
    // The tints live with the canvas: gold on you, red halted, grey paused.
    const canvas = css("components/flights/FlywayCanvas.css");
    expect(canvas).toMatch(/\.fl-bird-marker\.tone-you \{[^}]*var\(--tone-attention\)/);
    expect(canvas).toMatch(/\.fl-bird-marker\.tone-halted \{[^}]*var\(--red\)/);
    expect(canvas).toMatch(/\.fl-bird-marker\.tone-paused \{[^}]*var\(--muted\)/);
  });

  it("reduced motion: a working bird holds still", () => {
    expect(birdLook("working", true).flaps).toBe(false);
    reduceMotion(true);
    const html = renderToStaticMarkup(createElement(BirdMarker, { tone: "working", x: 10, y: 20, label: "The bird" }));
    expect(html).not.toContain("fl-flapping");
    // …and the stylesheet says so too, whatever the class.
    const sky = css("components/flights/SkyFlock.css");
    expect(sky).toMatch(/@media \(prefers-reduced-motion: reduce\) \{\s*\.fl-sprite-bird\.fl-flapping \.fl-sprite-wings \{\s*animation: none;/);
  });

  it("draws the wings and the body as two bands of one mask, placed for the hop", () => {
    const html = renderToStaticMarkup(createElement(BirdMarker, { tone: "working", x: 543, y: 26, label: "The bird: flying Push" }));
    expect(html).toMatch(/^<span class="fl-bird-marker tone-working" style="left:543px;top:26px" role="img" aria-label="The bird: flying Push"/);
    expect(html).toContain('class="fl-bird-big fl-sprite-bird fl-flapping" style="width:24px;height:17.8px"');
    expect(html).toMatch(/class="fl-sprite-wings" style="top:0;height:10px;[^"]*mask-size:24px 17.8px;[^"]*mask-position:0 0"/);
    expect(html).toMatch(/class="fl-sprite-body" style="top:10px;height:7.8px;[^"]*mask-position:0 -10px"/);
    // The wings beat about the hinge (their band's bottom edge).
    const sky = css("components/flights/SkyFlock.css");
    expect(sky).toMatch(/\.fl-sprite-wings \{[^}]*transform-origin: 50% 100%/);
    expect(sky).toMatch(/\.fl-sprite-bird\.fl-flapping \.fl-sprite-wings \{[^}]*animation: fl-sprite-flap/);
    // The hop the host relies on is still the canvas's transition.
    expect(css("components/flights/FlywayCanvas.css")).toMatch(/\.fl-bird-marker \{[^}]*transition: left [^;]*top /);
  });

  it("a size prop scales it", () => {
    const html = renderToStaticMarkup(createElement(BirdMarker, { tone: "you", x: 0, y: 0, label: "b", size: 32 }));
    expect(html).toContain('class="fl-bird-big fl-sprite-bird" style="width:32px;height:23.7px"');
  });
});

describe("the ambient flock", () => {
  it("flies ~8 birds, scaled by density, none in a sky with no size", () => {
    expect(flockSize(1200, 800)).toBe(8);
    expect(flockSize(1200, 800, 2)).toBe(16);
    expect(flockSize(1200, 800, 0.5)).toBe(4);
    expect(flockSize(1200, 800, 100)).toBe(40);
    expect(flockSize(0, 800)).toBe(0);
    expect(flockSize(1200, 800, 0)).toBe(0);
  });

  it("eases out of the middle of the sky, at a cruising speed", () => {
    const [b] = seedBirds(1, 1000, 600, rng());
    Object.assign(b, { x: 520, y: 300, vx: 0, vy: 0.45, tx: 520, ty: 300, tt: 1e9 });
    const before = Math.hypot(b.x - 500, b.y - 300);
    for (let i = 0; i < 60; i++) stepBirds([b], 1000, 600, rng());
    expect(Math.hypot(b.x - 500, b.y - 300)).toBeGreaterThan(before + 20);
    const sp = Math.hypot(b.vx, b.vy);
    expect(sp).toBeGreaterThanOrEqual(0.45 - 1e-9);
    expect(sp).toBeLessThanOrEqual(1.3 + 1e-9);
  });

  it("crosses the sky: a bird past an edge comes back in at the other", () => {
    const [b] = seedBirds(1, 1000, 600, rng());
    Object.assign(b, { x: 1029.5, y: 100, vx: 1.2, vy: 0, tx: 1029, ty: 100, tt: 1e9 });
    stepBirds([b], 1000, 600, rng());
    expect(b.x).toBe(-30);
  });

  it("a resize keeps the birds it can and folds them inside", () => {
    const birds = seedBirds(8, 1000, 600, rng());
    birds[0].x = 1500;
    const fit = fitBirds(birds, 4, 800, 500, rng());
    expect(fit).toHaveLength(4);
    expect(fit[0]).toBe(birds[0]);
    expect(fit[0].x).toBe(700);
    expect(fitBirds(fit, 6, 800, 500, rng())).toHaveLength(6);
  });

  it("paused: never asks for a frame", () => {
    const raf = vi.fn(() => 1);
    const tick = vi.fn();
    const stop = skyLoop(tick, { paused: true, raf, cancel: vi.fn() });
    expect(raf).not.toHaveBeenCalled();
    stop();
    expect(tick).not.toHaveBeenCalled();
  });

  it("flying: one rAF, at most 60 fps, no work while the tab is hidden, stop cancels", () => {
    const frames: Array<(now: number) => void> = [];
    const raf = vi.fn((cb: (now: number) => void) => frames.push(cb));
    const cancel = vi.fn();
    const tick = vi.fn();
    let hidden = false;
    const stop = skyLoop(tick, { raf, cancel, hidden: () => hidden });
    expect(raf).toHaveBeenCalledTimes(1);
    frames.at(-1)!(1000);
    frames.at(-1)!(1000 + FRAME_MS / 2); // a 120 Hz frame: skipped
    frames.at(-1)!(1000 + FRAME_MS);
    expect(tick).toHaveBeenCalledTimes(2);
    hidden = true;
    frames.at(-1)!(2000);
    expect(tick).toHaveBeenCalledTimes(2);
    stop();
    expect(cancel).toHaveBeenCalledWith(frames.length);
    const n = frames.length;
    frames.at(-1)!(3000);
    expect(frames.length).toBe(n);
  });

  it("is a decoration canvas, and nothing at all under reduced motion", () => {
    reduceMotion(false);
    const html = renderToStaticMarkup(createElement(SkyFlock, { width: 800, height: 600, zoom: 1 }));
    expect(html).toBe('<canvas class="sky-flock" style="width:800px;height:600px" aria-hidden="true"></canvas>');
    expect(css("components/flights/SkyFlock.css")).toMatch(/\.sky-flock \{[^}]*pointer-events: none/);
    expect(css("components/flights/SkyFlock.css")).toMatch(/:root\.light \.sky-flock \{[^}]*opacity: 0\.3/);
    reduceMotion(true);
    expect(renderToStaticMarkup(createElement(SkyFlock, { width: 800, height: 600, zoom: 1 }))).toBe("");
  });
});

describe("the pane's Flight tab", () => {
  it("is a route — three stops joined by arrows — not a bird", () => {
    const html = renderToStaticMarkup(createElement(TabIcon, { name: "flight" }));
    expect(html.match(/<circle /g)).toHaveLength(3);
    expect(html.match(/<polyline /g)).toHaveLength(2);
    expect(html).not.toContain("M2 9.5c");
    expect(html).not.toContain("bird");
  });
});
