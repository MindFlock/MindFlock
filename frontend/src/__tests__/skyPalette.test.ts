/** The floating blocks palette (SkyPalette, docs/flights.md "As built: round
 * 13 palette"): the shelf's blocks as a sky object — a head to drag it by
 * (Blocks, a count, All blocks…, a ⋯ with Dock at the bottom), the regions as
 * a wrapped grid of the shelf's own chips (the same drag MIMEs), a click =
 * `onPlace`, a corner handle that sizes it by the zoom, and the same pick
 * (`mf_sky_shelf`) as the shelf. Server-rendered with the kinds and plans
 * seeded in a QueryClient; the handlers are pinned in the source. */

import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { code, css as cssOf } from "./sourceText";
import type { FlightPlan, FlightStep, StepKindDef } from "../lib/flights";

vi.mock("../api/client", () => ({ api: vi.fn(() => new Promise(() => {})), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const { QueryClient, QueryClientProvider } = await import("@tanstack/react-query");
const { SkyPalette, PALETTE_KEY, PALETTE_MODE_KEY, PALETTE_W, PALETTE_H, PALETTE_MIN_W, PALETTE_MIN_H, paletteFrame, dragFrame } = await import("../components/flights/SkyPalette");

(globalThis as Record<string, unknown>).document ??= {
  hidden: false,
  addEventListener: () => {},
  removeEventListener: () => {},
  getElementById: () => null,
  querySelector: () => null,
};

const kind = (k: string, label: string, actor: "robot" | "you" = "robot"): StepKindDef => ({ kind: k, label, actor, summary: label + " does its thing", fields: [], default_title: label });
const KINDS = [kind("agent", "Agent"), kind("ship", "Ship"), kind("verify", "Verify"), kind("you", "You", "you"), kind("wait", "Wait"), kind("notify", "Notify")];
const step = (i: number, k = "ship"): FlightStep => ({ id: "s" + i, kind: k, title: "Step " + i, config: {}, on_fail: "halt" });
const plan = (id: string, name: string, steps: FlightStep[], builtin = false): FlightPlan => ({
  id,
  name,
  description: "",
  builtin,
  trigger: { kind: "manual", schedule: null, paused: false },
  session: { repo_path: "", program: "", model: "", in_place: false },
  steps,
  updated: 1,
});
const MINE = plan("p_mine", "Mine", [step(1, "agent"), step(2, "you")]);
const TICKET = plan("builtin:ticket", "Ticket to merge", [step(1, "agent"), step(2)], true);

function render(el: ReactElement, pick: { kinds: string[]; plans: string[] } | null = null) {
  const c = new QueryClient({ defaultOptions: { queries: { retry: false, enabled: false } } });
  c.setQueryData(["flight-kinds"], KINDS);
  c.setQueryData(["flight-plans"], [MINE, TICKET]);
  c.setQueryData(["settings"], {});
  vi.stubGlobal("localStorage", { getItem: (k: string) => (k === "mf_sky_shelf" && pick ? JSON.stringify(pick) : null), setItem: () => {} });
  return renderToStaticMarkup(createElement(QueryClientProvider, { client: c }, el));
}

describe("the palette's seam with the sky", () => {
  it("keeps the agreed names", () => {
    expect(PALETTE_KEY).toBe("palette");
    expect(PALETTE_MODE_KEY).toBe("mf_sky_palette");
  });

  it("its frame: 360 × 420 by default, never under 240 × 200; a corner drag is divided by the zoom", () => {
    expect([PALETTE_W, PALETTE_H, PALETTE_MIN_W, PALETTE_MIN_H]).toEqual([360, 420, 240, 200]);
    expect(paletteFrame()).toEqual({ w: 360, h: 420 });
    expect(paletteFrame(500.4, 300)).toEqual({ w: 500, h: 300 });
    expect(paletteFrame(10, 10)).toEqual({ w: 240, h: 200 });
    expect(paletteFrame(NaN, -5)).toEqual({ w: 360, h: 200 });
    expect(dragFrame({ w: 360, h: 420 }, 100, -50, 0.5)).toEqual({ w: 560, h: 320 });
    expect(dragFrame({ w: 360, h: 420 }, 100, 40, 2)).toEqual({ w: 410, h: 440 });
    expect(dragFrame({ w: 300, h: 300 }, -400, -400, 1)).toEqual({ w: 240, h: 200 });
    expect(dragFrame({ w: 300, h: 300 }, 10, 10, 0)).toEqual({ w: 310, h: 310 });
  });
});

describe("SkyPalette", () => {
  const html = render(createElement(SkyPalette, { onPlace() {}, onDock() {}, onSession() {}, onResize() {}, onHeadDown() {}, width: 400, height: 380 }), {
    kinds: ["agent", "you", "ship", "verify", "wait"],
    plans: ["builtin:ticket"],
  });

  it("a panel at the frame the host keeps", () => {
    expect(html).toMatch(/^<section class="sky-palette" data-tour="sky-palette" aria-label="Blocks palette" style="width:400px;height:380px">/);
    expect(render(createElement(SkyPalette, {}))).toContain('style="width:360px;height:420px"');
  });

  it("the head: Blocks, a count, All blocks… (secondary), a ⋯ with Dock at the bottom; it moves the palette when the host lets it", () => {
    const head = html.slice(html.indexOf('class="sky-palette-head'), html.indexOf("sky-palette-body"));
    expect(head).toContain('class="sky-palette-head movable" data-sky-head="true" title="Drag to move the blocks"');
    expect(head).toContain('<strong class="sky-palette-title">Blocks</strong>');
    // 5 kinds + 1 saved stop on the pick (the Session chip isn't a block).
    expect(head).toMatch(/<span class="sky-palette-count"[^>]*>6<\/span>/);
    expect(head).toContain('<button type="button" class="test-btn sky-palette-all" aria-haspopup="dialog" aria-expanded="false"');
    expect(head).toContain('<summary aria-label="The blocks palette — more"');
    expect(head).toContain(">Dock at the bottom</button>");
    // No onDock: no ⋯; no onHeadDown (or click mode): no move hint.
    const still = render(createElement(SkyPalette, { onHeadDown() {}, draggable: false }));
    expect(still).toContain('class="sky-palette-head" data-sky-head="true"');
    expect(still).not.toContain("vf-menu");
    const src = code("components/flights/SkyPalette.tsx");
    expect(src).toContain("if (draggable && onHeadDown && headPress(e)) onHeadDown(e);");
    expect(src).toContain('closest("button, a, input, summary, [data-no-drag]")');
    expect(src).toContain('items={[{ key: "dock", label: "Dock at the bottom"');
    expect(src).toContain("onSelect: onDock }]");
  });

  it("the regions: Blocks (the Session chip, then the pick's kinds), Multi-step (the pick's saved stops), the shelf's own chips", () => {
    const regions = [...html.matchAll(/class="sky-palette-region" data-region="([^"]+)" role="list" aria-label="([^"]+)"/g)].map((m) => m[1] + ":" + m[2]);
    expect(regions).toEqual(["blocks:Blocks", "multi:Multi-step"]);
    const kinds = [...html.matchAll(/data-kind="([^"]+)"/g)].map((m) => m[1]);
    expect(kinds).toEqual(["agent", "you", "ship", "verify", "wait"]);
    expect(html.indexOf("sky-session-chip")).toBeLessThan(html.indexOf('data-kind="agent"'));
    expect([...html.matchAll(/data-plan="([^"]+)"/g)].map((m) => m[1])).toEqual(["builtin:ticket"]);
    // The same pick as the shelf: nothing picked = the five defaults and no Multi-step.
    const plain = render(createElement(SkyPalette, { onPlace() {} }));
    expect(plain).not.toContain('data-region="multi"');
    expect(code("components/flights/SkyPalette.tsx")).toContain("const [shelf, changeShelf] = useShelfPick();");
  });

  it("every item drags with the shelf's MIMEs (the same chips), and a click places it", () => {
    // Session + 5 kinds + 1 saved stop.
    expect(html.match(/draggable="true"/g)).toHaveLength(7);
    expect(html).toMatch(/<button type="button" class="sky-stop-chip" data-kind="ship" draggable="true" title="Ship does its thing\nClick to put it on the board, or drag it onto a line or the sky — right-click for more" aria-label="Put “Ship” on the board">/);
    expect(html).toContain('aria-label="Put “Ticket to merge” on the board"');
    expect(html).toContain('aria-label="Start a new session"');
    expect(html).not.toContain("focused line");
    const src = code("components/flights/SkyPalette.tsx");
    expect(src).toContain('import { SavedChip, SessionChip, StopChip, useSavedActions, useShelfPick } from "./StopShelf";');
    expect(src).toContain("const clickKind = onPlace ? (kind: string) => onPlace({ kind }) : undefined;");
    expect(src).toContain("const clickPlan = onPlace ? (plan: FlightPlan) => onPlace({ planId: plan.id }) : undefined;");
    // The chips' drags are the shelf's (STOP_MIME / FLYWAY_MIME / NEW_SESSION_MIME).
    const shelf = code("components/flights/StopShelf.tsx");
    expect(shelf).toContain("onDragStart: (e: DragEvent) => onStopDrag(e, def),");
    expect(shelf).toContain("onDragStart: (e: DragEvent) => carry(e, savedDragData(plan)),");
    expect(shelf).toContain("onDragStart: (e: DragEvent) => carry(e, sessionDragData()),");
    // Without onPlace the chips only drag.
    const drag = render(createElement(SkyPalette, {}));
    expect(drag).toMatch(/<span class="sky-stop-chip drag-only" data-kind="agent" draggable="true"/);
  });

  it("All blocks… opens the big catalog, whose Add places", () => {
    const src = code("components/flights/SkyPalette.tsx");
    expect(src).toContain("onClick={() => setCatalog(true)}");
    expect(src).toMatch(/<StopCatalog[\s\S]*?onAdd=\{onPlace \? add : undefined\}[\s\S]*?place[\s\S]*?onShelfToggle=\{\(d\) => changeShelf\(\(p\) => toggleShelf\(p, d\)\)\}/);
    expect(src).toMatch(/const add = \(d: CatalogDrop\) => \{\s*setCatalog\(false\);\s*onPlace\?\.\(d\);/);
  });

  it("the corner handle: the intake lane's — pointer capture, the delta over the zoom, onResize on release; arrows too", () => {
    expect(html).toContain('<span class="sky-palette-resize" role="separator" tabindex="0" data-no-drag="true" aria-label="Resize the blocks palette"');
    expect(render(createElement(SkyPalette, {}))).not.toContain("sky-palette-resize");
    const src = code("components/flights/SkyPalette.tsx");
    expect(src).toContain("e.currentTarget.setPointerCapture(e.pointerId);");
    expect(src).toContain("setLive(dragFrame(g, e.clientX - g.sx, e.clientY - g.sy, zoom));");
    expect(src).toContain("if (live && onResize) onResize(live.w, live.h);");
    expect(src).toContain("onPointerCancel={resizeUp}");
    const css = cssOf("components/flights/SkyPalette.css");
    expect(css).toMatch(/\.sky-palette-resize \{[^}]*cursor: nwse-resize;[^}]*touch-action: none;/);
  });

  it("measures itself for the host (a ResizeObserver), and its body scrolls itself, not the sky", () => {
    const src = code("components/flights/SkyPalette.tsx");
    expect(src).toContain("const ro = new ResizeObserver(read);");
    expect(src).toContain("measureRef.current?.(w, h);");
    expect(html).toContain('class="sky-palette-body sky-own-scroll"');
  });

  it("the chips flow in a wrapped grid; a saved stop takes two columns", () => {
    const css = cssOf("components/flights/SkyPalette.css");
    expect(css).toMatch(/\.sky-palette-region \{\s*display: grid;\s*grid-template-columns: repeat\(auto-fill, minmax\(108px, 1fr\)\);/);
    expect(css).toMatch(/\.sky-palette \.sky-saved-chip \{\s*grid-column: span 2;/);
    expect(css).toMatch(/\.sky-palette \{[^}]*min-width: 240px;[^}]*min-height: 200px;/);
  });

  it("no native dialogs; Esc does nothing special", () => {
    const src = code("components/flights/SkyPalette.tsx");
    expect(src).not.toMatch(/\bwindow\.(prompt|alert|confirm)\(/);
    expect(src).not.toContain('"Escape"');
  });
});

describe("one pick for the shelf, the palette and the catalog", () => {
  it("a change is stored and announced, so every surface mounted hears it", () => {
    const shelf = code("components/flights/StopShelf.tsx");
    expect(shelf).toContain('const SHELF_EVENT = "mf:sky-shelf";');
    expect(shelf).toContain("localStorage.setItem(SHELF_KEY, JSON.stringify(n));");
    expect(shelf).toContain("window.dispatchEvent(new CustomEvent(SHELF_EVENT, { detail: n }));");
    expect(shelf).toContain('window.addEventListener("storage", other);');
  });
});
