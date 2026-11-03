/** The sky's room and its semantic zoom (docs/flights-zoom.md): the level of
 * detail a box shows at the camera's zoom (lib/skyLayout lodFor — far / mid /
 * near — while the geometry never changes with it), the first picture
 * (firstFit: legible over whole), the spread-out constants, the drawer folded
 * by default, and `data-lod` on a rendered line at three zooms. */

/// <reference types="vite/client" />
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { code, css, cssRule } from "./sourceText";
import type { Instance } from "../api/types";
import {
  FIT_PAD,
  INTAKE_W,
  LINES_LEFT,
  LOD_MID,
  LOD_NEAR,
  SKY_DRAWER_KEY,
  SKY_GAP,
  SKY_MARGIN,
  ZOOM_MAX,
  ZOOM_MIN,
  firstFit,
  fitCamera,
  lodFor,
  nearLines,
  placeLines,
  skyDrawerOpen,
} from "../lib/skyLayout";

vi.mock("../api/client", () => ({ api: vi.fn(() => new Promise(() => {})), instApi: vi.fn(() => new Promise(() => {})), ApiError: class extends Error {} }));

vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const { QueryClient, QueryClientProvider } = await import("@tanstack/react-query");
const { useUi } = await import("../state/store");
const { SkyLine } = await import("../components/flights/SkyLine");

// The queries' poll intervals read `document.hidden`; a server render has no
// document, so a quiet one stands in.
(globalThis as Record<string, unknown>).document ??= {
  hidden: false,
  addEventListener: () => {},
  removeEventListener: () => {},
  getElementById: () => null,
  querySelector: () => null,
};

afterEach(() => {
  Object.assign(useUi.getInitialState(), useUi.getState());
});

describe("the level of detail", () => {
  it("has three bands: far below 62%, mid to 115%, near from 115%", () => {
    expect([LOD_MID, LOD_NEAR]).toEqual([0.62, 1.15]);
    expect(lodFor(ZOOM_MIN)).toBe("far");
    expect(lodFor(0.619)).toBe("far");
    expect(lodFor(LOD_MID)).toBe("mid");
    expect(lodFor(1)).toBe("mid");
    expect(lodFor(1.149)).toBe("mid");
    expect(lodFor(LOD_NEAR)).toBe("near");
    expect(lodFor(2.5)).toBe("near");
    expect(lodFor(ZOOM_MAX)).toBe("near");
    // A broken zoom reads as far (nothing is drawn too small to read).
    expect(lodFor(Number.NaN)).toBe("far");
  });

  it("near shows more lines the closer the camera; none outside near", () => {
    expect(nearLines(0.4)).toBe(0);
    expect(nearLines(1)).toBe(0);
    expect(nearLines(LOD_NEAR)).toBe(2);
    expect(nearLines(2)).toBe(4);
    expect(nearLines(3)).toBe(6);
    for (let z = LOD_NEAR; z < ZOOM_MAX; z += 0.1) expect(nearLines(z + 0.1)).toBeGreaterThanOrEqual(nearLines(z));
  });

  it("zooms 40%–300%: near has more to show", () => {
    expect([ZOOM_MIN, ZOOM_MAX]).toEqual([0.1, 3]);
  });
});

describe("the first picture", () => {
  it("fits everything, centred, when it fits legibly (no further than mid)", () => {
    const b = { x: 40, y: 40, w: 900, h: 400 };
    const cam = firstFit(b, 1400, 900);
    expect(cam).toEqual(fitCamera(b, 1400, 900, 1));
    expect(cam.z).toBe(1);
    // Centred horizontally.
    expect(cam.x + (b.x + b.w / 2) * cam.z).toBeCloseTo(700);
  });

  it("wide content: its width at no less than 62%, anchored at the top-left", () => {
    // The intake lane and a 9-stop line: ~2300 plane px in a 1400px window.
    const b = { x: SKY_MARGIN, y: SKY_MARGIN, w: 2300, h: 1400 };
    expect(fitCamera(b, 1400, 900).z).toBeLessThan(LOD_MID);
    const cam = firstFit(b, 1400, 900);
    expect(cam.z).toBe(LOD_MID);
    expect(lodFor(cam.z)).toBe("mid");
    expect(cam.x).toBeCloseTo(FIT_PAD - b.x * cam.z);
    expect(cam.y).toBeCloseTo(FIT_PAD - b.y * cam.z);
  });

  it("tall content that fits across: the width, not the height, sets the zoom", () => {
    const b = { x: 0, y: 0, w: 1000, h: 4000 };
    const cam = firstFit(b, 1400, 900);
    expect(cam.z).toBeCloseTo(Math.min(1, (1400 - 2 * FIT_PAD) / 1000));
    expect(cam.y).toBe(FIT_PAD);
  });

  it("Fit is the true fit: everything, however small", () => {
    const b = { x: SKY_MARGIN, y: SKY_MARGIN, w: 60000, h: 30000 };
    expect(fitCamera(b, 1400, 900).z).toBe(ZOOM_MIN);
    expect(firstFit(b, 1400, 900).z).toBe(LOD_MID);
    expect(firstFit(null, 1400, 900)).toEqual({ x: 0, y: 0, z: 1 });
  });

  it("the sky wires it: an automatic fit and 0 are legible, the Fit button whole", () => {
    const sky = code("components/flights/SkyView.tsx");
    // Round 17: through the eased camera (`goTo`), instant only for the first picture.
    expect(sky).toContain("goTo((whole ? fitCamera : firstFit)(boundsOf(rects), vp.w, vp.h, mode === \"one\" ? 1.5 : 1), instant);");
    expect(sky).toContain("else fitAll(false, true);");
    expect(sky).toContain("onClick={() => fitAll(true)}");
    expect(sky).toMatch(/k === "0"\) \{\s*handled\(\);\s*fitAll\(\);/);
    expect(sky).toMatch(/title="See everything: every chain in the window, however small/);
  });
});

describe("room on the screen", () => {
  it("spreads the lines out and narrows the intake lane", () => {
    expect(SKY_GAP).toBe(64);
    expect(INTAKE_W).toBe(240);
    expect(LINES_LEFT).toBe(SKY_MARGIN + INTAKE_W + 48);
    expect(FIT_PAD).toBe(56);
    const pos = placeLines([{ id: "a", w: 400, h: 100 }, { id: "b", w: 400, h: 100 }], {}, { width: 2000, left: LINES_LEFT, top: SKY_MARGIN });
    expect(pos.b.x - (pos.a.x + 400)).toBe(64);
  });

  it("the drawer starts folded to its tab and remembers being opened", () => {
    expect(SKY_DRAWER_KEY).toBe("mf_sky_drawer");
    expect(skyDrawerOpen(null)).toBe(false);
    expect(skyDrawerOpen(undefined)).toBe(false);
    expect(skyDrawerOpen("0")).toBe(false);
    expect(skyDrawerOpen("1")).toBe(true);
    const sky = code("components/flights/SkyView.tsx");
    expect(sky).toContain("const drawerOpen = skyDrawerOpen(drawerPref);");
    expect(sky).toContain("writePref(SKY_DRAWER_KEY, v);");
    // Folded with something perched, the tab says so.
    expect(code("components/flights/SkyDrawer.tsx")).toContain('className={"sky-drawer-tab" + (count > 0 ? " has-perch" : "")}');
    expect(css("components/flights/Sky.css")).toMatch(/\.sky-drawer:not\(\.open\) \.sky-drawer-tab\.has-perch \{[^}]*--tone-attention/);
  });

  it("the toolbar is one slim row that doesn't wrap, shedding its longest words when the main area is narrow", () => {
    const sheet = css("components/flights/Sky.css");
    expect(sheet).toMatch(/\.sky-toolbar \{[^}]*flex-wrap: nowrap;[^}]*max-height: 36px;/);
    expect(sheet).toMatch(/\.sky \{[^}]*container: sky \/ inline-size;/);
    expect(sheet).toMatch(/@container sky \(max-width: 860px\) \{\s*\.sky-zoom-100,\s*\.sky-toolbar > \.tour-btn \{\s*display: none;/);
    expect(code("components/flights/SkyView.tsx")).toContain('className="sky-zoom-btn sky-zoom-100" title="Back to 100% (Shift+0)"');
  });

  it("every line sits at its placed spot (its slot is translated there), so the gap and Tidy show", () => {
    const sky = code("components/flights/SkyView.tsx");
    expect(sky).toContain("style={{ transform: `translate(${at.x}px, ${at.y}px)` }}");
    expect(sky).toContain("{lineFor(l.id, at)}");
    const sheet = css("components/flights/Sky.css");
    expect(sheet).toMatch(/\.sky-line-slot \{[^}]*position: absolute;/);
    // Tidy is the only glide (docs/flights-stable.md).
    expect(sheet).toMatch(/\.sky-plane\[data-tidying\] \.sky-line-slot,\s*\.sky-plane\[data-tidying\] \.sky-intake,\s*\.sky-plane\[data-tidying\] \.sky-palette-slot \{[^}]*transition: transform/);
    expect(sheet).toMatch(/\.sky-line-slot\.is-dragging \{[^}]*transition: none;/);
    expect(sheet).toContain(".sky-line-slot.is-flash .skl,");
  });

  it("the intake lane keeps its width; from far it is the frames' silhouette (no item rows) at the frames' type scale", () => {
    expect(code("components/flights/SkyView.tsx")).toContain("data-lod={lodFor(cam.z)}");
    const lane = css("components/flights/IntakeLane.css");
    expect(lane).toMatch(/\.sky-intake\[data-lod="far"\] :is\([^)]*\.sky-lane-items[^)]*\) \{\s*display: none;/);
    // The owner: the lane's words ballooned when zoomed out. A session frame's
    // bar type is fixed from far, so the lane's is too; at mid only the names
    // grow, as a frame's name does (at most 1.3×).
    expect(lane).not.toContain("--sky-far");
    expect(cssRule(lane, '.sky-intake[data-lod="mid"]')).toContain("--sky-mid: clamp(1, calc(0.8 / var(--sky-z, 1)), 1.3)");
    expect(lane).toMatch(/\.sky-intake\[data-lod="mid"\] :is\(\.sky-lane-title, \.sky-lane-block-name\) \{\s*font-size: calc\(13px \* var\(--sky-mid\)\);\s*\}/);
    expect(lane).not.toMatch(/\[data-lod="mid"\][^{]*(sky-lane-item|sky-lane-auto|sky-lane-stops|sky-lane-fly)/);
  });
});

const rowOf = (title: string, over: Record<string, unknown> = {}) =>
  ({
    title,
    status: "running",
    activity: "idle",
    stage: "agent",
    task_headline: "Do " + title,
    last_prompt: "Make the login page stop looping",
    parent: "",
    branch: title,
    flight: null,
    autopilot: null,
    ...over,
  }) as unknown as Instance;

const lineAt = (zoom: number) => {
  const c = new QueryClient({ defaultOptions: { queries: { retry: false, enabled: false } } });
  c.setQueryData(["flights"], []);
  c.setQueryData(["flight-kinds"], []);
  c.setQueryData(["config"], { caps: { git: true, tailscale: false, ticketing: false, github: false } });
  return renderToStaticMarkup(
    createElement(
      QueryClientProvider,
      { client: c },
      createElement(SkyLine, {
        title: "me",
        rows: [rowOf("me")],
        plans: [],
        layout: { x: 0, y: 0, size: "normal", open: false },
        onLayout: () => {},
        focused: false,
        onFocus: () => {},
        zoom,
      })
    )
  );
};

const chainStyle = (html: string) => /<div[^>]*class="sky-chain[^"]*"[^>]*style="([^"]*)"/.exec(html)?.[1];

describe("a line at three zooms", () => {
  it("wears data-lod and --sky-z on its root", () => {
    const far = lineAt(0.4);
    const mid = lineAt(1);
    const near = lineAt(2.5);
    expect(far).toMatch(/<div[^>]*class="skl [^"]*"[^>]*data-lod="far"/);
    expect(mid).toMatch(/<div[^>]*class="skl [^"]*"[^>]*data-lod="mid"/);
    expect(near).toMatch(/<div[^>]*class="skl [^"]*"[^>]*data-lod="near"/);
    expect(far).toContain("--sky-z:0.4");
    expect(near).toContain("--sky-z:2.5;--sky-lines:5");
  });

  it("never changes a chain's geometry with the zoom (round 18: the graph's, plane px)", () => {
    const at = [0.4, 1, 2.5].map((z) => chainStyle(lineAt(z)));
    expect(at[0]).toBeTruthy();
    expect(at[0]).toMatch(/width:\d+px/);
    expect(at[1]).toBe(at[0]);
    expect(at[2]).toBe(at[0]);
  });

  it("hands the level to the canvas and carries what near reveals", () => {
    const line = code("components/flights/SkyLine.tsx");
    expect(line).toContain("const lod = lodFor(p.zoom || 1);");
    expect(line).toContain("data-lod={lod}");
    // The session tile says what the session is on (its sub), at every level.
    expect(line).toContain('firstLine(row.task_headline || row.task || "")');
  });

  it("counter-scales the type: legible far, informative near", () => {
    const sheet = css("components/flights/SkyLine.css");
    expect(sheet).toMatch(/\.skl\[data-lod="far"\] \{[^}]*--sky-far: clamp\(1, calc\(0\.76 \/ var\(--sky-z, 1\)\), 1\.9\);/);
    expect(sheet).toMatch(/\.skl\[data-lod="near"\] \{[^}]*--sky-near: clamp\(1, var\(--sky-z, 1\), 3\);/);
    expect(sheet).toMatch(/\.skl\[data-lod="far"\] \.fl-box:not\(\.fl-box-open\) \.fl-box-title \{[^}]*font-size: calc\(15px \* var\(--sky-far\)\);[^}]*-webkit-line-clamp: 2;/);
    expect(sheet).toMatch(/\.skl\[data-lod="near"\] \.fl-box:not\(\.fl-box-open\) \.fl-box-title \{[^}]*font-size: calc\(15px \/ var\(--sky-near\)\);[^}]*white-space: normal;/);
    expect(sheet).toMatch(/-webkit-line-clamp: var\(--sky-lines, 2\);/);
    // Mid's low end still grows its titles a little (the first picture, ≥ 62%).
    expect(sheet).toMatch(/\.skl\[data-lod="mid"\] \{[^}]*--sky-mid: clamp\(1, calc\(0\.8 \/ var\(--sky-z, 1\)\), 1\.3\);/);
    // The bar's height is never the zoom's.
    expect(sheet).toMatch(/\.skl-bar \{[^}]*height: 42px;/);
  });
});
