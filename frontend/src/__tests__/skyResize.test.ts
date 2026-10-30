/** A line's boxes resized by its corner (docs/flights.md "As built: round 11
 * line"): the layout store's `boxW` / `boxH` (clamped, dropped when they are
 * just a preset's), the ◧ presets as the same two numbers, the tier the
 * canvas draws and the uniform scale on top of it, the corner's drag, the
 * estimate before a line is measured — and the line itself: the scaled canvas
 * in its sizer, `--sky-z` carrying the scale, the handle, names through
 * windowName, flown stops kept and the ⋯'s new words. */

/// <reference types="vite/client" />
import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { code, css } from "./sourceText";
import type { Instance } from "../api/types";
import type { Flight, FlightPlan, FlightStep, StepKindDef } from "../lib/flights";

vi.mock("../api/client", () => ({ api: vi.fn(() => new Promise(() => {})), instApi: vi.fn(() => new Promise(() => {})), ApiError: class extends Error {} }));

vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const { QueryClient, QueryClientProvider } = await import("@tanstack/react-query");
const { useUi } = await import("../state/store");
const sky = await import("../lib/skyLayout");
const { SkyLine, dragBox, presetPatch } = await import("../components/flights/SkyLine");

(globalThis as Record<string, unknown>).document ??= {
  hidden: false,
  addEventListener: () => {},
  removeEventListener: () => {},
  getElementById: () => null,
  querySelector: () => null,
};

const memStore = () => {
  const m = new Map<string, string>();
  return { getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => void m.set(k, v) };
};

describe("the layout store", () => {
  it("keeps boxW / boxH clamped to 140–420 × 60–220", () => {
    const s = memStore();
    let all = sky.patchSkyLine({}, "me", { boxW: 1000, boxH: 10 }, s);
    expect(all.me).toEqual({ boxW: 420, boxH: 60 });
    all = sky.patchSkyLine(all, "me", { boxW: 100, boxH: 999 }, s);
    expect(all.me).toEqual({ boxW: 140, boxH: 220 });
    // Read back the same.
    expect(sky.readSkyLayout(s.getItem(sky.SKY_LAYOUT_KEY))).toEqual(all);
  });

  it("drops the numbers when they are just the preset's, and fills a lone one from it", () => {
    const s = memStore();
    expect(sky.patchSkyLine({}, "me", { size: "compact", boxW: 150, boxH: 64 }, s).me).toEqual({ size: "compact" });
    expect(sky.patchSkyLine({}, "me", { size: "normal", boxW: 236, boxH: 100 }, s).me).toBeUndefined();
    expect(sky.patchSkyLine({}, "me", { boxW: 260 }, s).me).toEqual({ boxW: 260, boxH: 100 });
    // Junk is not a size.
    expect(sky.readSkyLayout(JSON.stringify({ me: { boxW: "big", boxH: null, x: 1, y: 2 } })).me).toEqual({ x: 1, y: 2 });
  });

  it("a line's layout carries them; an old layout's size stands for its numbers", () => {
    expect(sky.lineLayout({ boxW: 300, boxH: 110 }, { x: 0, y: 0 })).toMatchObject({ boxW: 300, boxH: 110 });
    expect(sky.lineBox({ size: "compact" })).toEqual({ w: 150, h: 64 });
    expect(sky.lineBox({ size: "wide" })).toEqual({ w: 330, h: 120 });
    expect(sky.lineBox(undefined)).toEqual({ w: 236, h: 100 });
    expect(sky.lineBox({ size: "compact", boxW: 200, boxH: 90 })).toEqual({ w: 200, h: 90 });
  });

  it("the estimate before measuring follows boxW / boxH; without them, the old size factors", () => {
    const base = { w: 1000, h: 200 };
    expect(sky.estimateLine(base, { boxW: 236, boxH: 100 })).toEqual(base);
    const big = sky.estimateLine(base, { boxW: 354, boxH: 150 });
    expect(big.w).toBe(1500);
    expect(big.h).toBe(42 + 158 * 1.5);
    expect(sky.estimateLine(base, { boxW: 140, boxH: 60 }).w).toBeLessThan(base.w);
    expect(sky.estimateLine(base, { size: "wide" })).toEqual({ w: 1400, h: 200 });
  });
});

describe("tiers and the scale", () => {
  it("each preset draws its own tier at scale 1", () => {
    for (const s of sky.SKY_SIZES) expect(sky.boxScale(sky.BOX_PRESETS[s].w)).toEqual({ size: s, k: 1 });
  });

  it("between presets: the nearest tier, scaled to the width asked for", () => {
    expect(sky.boxTier(192)).toBe("compact");
    expect(sky.boxTier(194)).toBe("normal");
    expect(sky.boxTier(282)).toBe("normal");
    expect(sky.boxTier(284)).toBe("wide");
    const s = sky.boxScale(420);
    expect(s.size).toBe("wide");
    expect(s.k).toBeCloseTo(420 / 330);
    expect(sky.boxScale(140).k).toBeCloseTo(140 / 150);
  });

  it("◧ and the ⋯'s sizes are presets of the two numbers", () => {
    expect(presetPatch("normal")).toEqual({ size: "normal", boxW: 236, boxH: 100 });
    const src = code("components/flights/SkyLine.tsx");
    expect(src).toContain("onSelect: () => p.onLayout(presetPatch(s)),");
  });
});

describe("the corner's drag", () => {
  const start = { cw: 1000, ch: 200, bw: 236 };

  it("the canvas follows the pointer along the axis it moved most", () => {
    // +10% of the canvas's width → boxes 10% wider.
    expect(dragBox(start, 100, 5).w).toBe(260);
    // Mostly down: +25% of its height.
    expect(dragBox(start, 5, 50).w).toBe(295);
    // Nothing moved: the same box.
    expect(dragBox(start, 0, 0)).toEqual({ w: 236, h: 100 });
  });

  it("clamps, and the height is the tier's at the same scale", () => {
    expect(dragBox(start, 5000, 0)).toEqual({ w: 420, h: Math.round(120 * (420 / 330)) });
    expect(dragBox(start, -5000, 0)).toEqual({ w: 140, h: Math.max(60, Math.round(64 * (140 / 150))) });
  });

  it("is persisted once, on release, with the tier's name", () => {
    const src = code("components/flights/SkyLine.tsx");
    expect(src).toContain("if (liveBox) p.onLayout({ size: boxTier(liveBox.w), boxW: liveBox.w, boxH: liveBox.h });");
    expect(src).toContain("setLiveBox(dragBox(g, (e.clientX - g.sx) / z, (e.clientY - g.sy) / z));");
    expect(src).toMatch(/setPointerCapture\(e\.pointerId\)/);
  });
});

// --- The line ----------------------------------------------------------------

const step = (id: string, kind: string, title: string, state = "pending"): FlightStep & { state: string } => ({ id, kind, title, config: {}, on_fail: "halt", state });
const KINDS: StepKindDef[] = [
  { kind: "agent", label: "Agent", actor: "robot", summary: "The agent works", fields: [], default_title: "Agent works" },
  { kind: "ship", label: "Ship", actor: "robot", summary: "Ship", fields: [], default_title: "Ship" },
];
const FLIGHT = {
  id: "f1",
  plan_id: "builtin:ticket",
  plan_name: "Ticket to merge",
  session: "me",
  state: "running",
  cursor: 2,
  steps: [step("s1", "agent", "Work it", "done"), step("s2", "ship", "Commit", "done"), step("s3", "ship", "Push", "running"), step("s4", "ship", "Merge")],
  history: [],
} as unknown as Flight;
const PLANS: FlightPlan[] = [];
const me = () =>
  ({
    title: "me",
    status: "running",
    activity: "running",
    stage: "agent",
    task_headline: "Do me",
    parent: "",
    branch: "me",
    flight: { id: "f1", state: "running", cursor: 2, plan_name: "Ticket to merge", steps: 4, step: { id: "s3", kind: "ship", title: "Push", state: "running" } },
    autopilot: null,
  }) as unknown as Instance;

function withClient(el: ReactElement) {
  const c = new QueryClient({ defaultOptions: { queries: { retry: false, enabled: false } } });
  c.setQueryData(["flights"], [FLIGHT]);
  c.setQueryData(["flight-kinds"], KINDS);
  c.setQueryData(["config"], { caps: { git: true, tailscale: false, ticketing: false, github: false } });
  return renderToStaticMarkup(createElement(QueryClientProvider, { client: c }, el));
}

afterEach(() => {
  useUi.setState({ aliases: {} });
});

const line = (layout: Record<string, unknown> = {}, zoom = 1) =>
  withClient(
    createElement(SkyLine, {
      title: "me",
      rows: [me()],
      plans: PLANS,
      layout: { x: 0, y: 0, size: "normal", open: false, ...layout },
      onLayout: () => {},
      focused: false,
      onFocus: () => {},
      zoom,
    })
  );

describe("a resized line", () => {
  it("at a preset: no scale, no style on the sizer", () => {
    const html = line();
    expect(html).toContain('<div class="skl-scale"><div class="skl-scale-in">');
    expect(html).toContain('aria-label="Resize me&#x27;s boxes"');
  });

  it("between presets: the tier's canvas, scaled, with --sky-z times the scale", () => {
    const html = line({ boxW: 300, boxH: 109 }, 0.5);
    const k = 300 / 330;
    expect(html).toContain("fl-size-wide");
    expect(html).toContain(`transform:scale(${k})`);
    expect(html).toContain(`--sky-z:${0.5 * k}`);
    // The bar keeps the sky's own zoom.
    expect(html).toMatch(/class="skl skl-size-wide[^"]*"[^>]*style="--sky-z:0.5;/);
  });

  it("an old layout's size alone is its preset", () => {
    const html = line({ size: "compact" });
    expect(html).toContain("fl-size-compact");
    expect(html).not.toContain("transform:scale(");
  });

  it("the CSS: the sizer, the type factors read against the canvas's --sky-z, the handle screen-sized", () => {
    const c = css("components/flights/SkyLine.css");
    expect(c).toMatch(/\.skl-scale-in \{[^}]*transform-origin: 0 0;/);
    expect(c).toMatch(/\.skl\[data-lod="far"\] \.skl-scale-in \{[^}]*--sky-far: clamp\(1, calc\(0\.76 \/ var\(--sky-z, 1\)\), 1\.9\);/);
    expect(c).toMatch(/\.skl\[data-lod="near"\] \.skl-scale-in \{[^}]*--sky-near: clamp\(1, var\(--sky-z, 1\), 3\);/);
    expect(c).toMatch(/\.skl-box-resize \{[^}]*transform: scale\(calc\(1 \/ var\(--sky-z, 1\)\)\);/);
  });

  it("an open card keeps its own size on a scaled line", () => {
    const src = code("components/flights/SkyLine.tsx");
    expect(src).toContain("const cw = s.w / k;");
    expect(src).toContain('transform: `scale(${1 / k})`, transformOrigin: "0 0", "--sky-z": z');
  });
});

describe("flown stops and the ⋯'s words", () => {
  const menu = (html: string) => [...html.matchAll(/<button type="button" class="vf-menu-item[^"]*"[^>]*>([^<]+)<\/button>/g)].map((m) => m[1]);

  it("flown stops stay boxes; the ⋯ offers to fold them", () => {
    const html = line();
    expect(html).toContain('data-node="stop:s1"');
    expect(html).not.toContain('data-node="flown"');
    expect(menu(html)).toContain("Fold flown stops");
  });

  it("draws the hook's diagram (the fold and the open groups are its), and asks it what a fold would hide", () => {
    const src = code("components/flights/SkyLine.tsx");
    expect(src).toContain("const { foldFlown, setFoldFlown } = line;");
    expect(src).toContain("{ flights, kinds, family: true, foldFlown: true }");
    expect(src).toContain("const shown = { diagram: line.diagram, canvas: line.canvas };");
    expect(src).toContain('if (n.kind === "flown") setFoldFlown((x) => !x);');
  });

  it("the word flyway leaves the ⋯", () => {
    const items = menu(line());
    expect(items).toContain("Add a stop…");
    expect(items.some((l) => /flyway/i.test(l))).toBe(false);
    expect(line()).toContain('title="A saved stop, tacked on here"');
  });
});

describe("names", () => {
  it("the bar, the line's label and the ⋯ say the session's name, not its title", () => {
    useUi.setState({ aliases: { me: "Login fix" } });
    const html = line();
    expect(html).toContain('<span class="skl-name">Login fix</span>');
    expect(html).toContain('aria-label="Login fix&#x27;s line"');
    expect(html).toContain('aria-label="Actions for Login fix"');
    expect(html).toContain('aria-label="Resize Login fix&#x27;s boxes"');
    // Still keyed by the raw title.
    expect(html).toContain('data-line="me"');
  });

  it("the open card's name and labels too", () => {
    const src = code("components/flights/SkyLine.tsx");
    expect(src).toContain('<span className="skl-card-name">{windowName(inst.title)}</span>');
    expect(src).toContain('aria-label={"Fold " + windowName(inst.title) + " back into a box"}');
    expect(src).toContain(`aria-label={"Resize " + windowName(inst.title) + "'s card"}`);
    // Shown text and labels: never the raw title (keys and data-line may).
    expect(src).not.toMatch(/>\{(p|inst)\.title\}<|(label|title)=\{[^}]*[^(\w.](p|inst)\.title\b/);
  });
});
