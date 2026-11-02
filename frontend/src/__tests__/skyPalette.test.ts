/** The floating blocks palette (SkyPalette, docs/flights.md "As built: round
 * 14 palette"): EVERY block as a sky object — a head to drag it by (Blocks, a
 * count, the filter, a ⋯ with Dock at the bottom, a ×), the catalog's body
 * compact (the Session card, every kind, every saved stop; the same drag
 * MIMEs), a click or a double-click = `onPlace` once, a ★ per card that sets
 * the bottom shelf's pick, a corner handle that sizes it by the zoom.
 * Server-rendered with the kinds and plans seeded in a QueryClient; the
 * handlers are pinned in the source. */

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
  const html = render(createElement(SkyPalette, { onPlace() {}, onDock() {}, onSession() {}, onResize() {}, onHeadDown() {}, onClose() {}, width: 400, height: 380 }), {
    kinds: ["agent", "you", "ship", "verify", "wait"],
    plans: ["builtin:ticket"],
  });

  it("a panel at the frame the host keeps", () => {
    expect(html).toMatch(/^<section class="sky-palette" data-tour="sky-palette" aria-label="Blocks palette" style="width:400px;height:380px">/);
    expect(render(createElement(SkyPalette, {}))).toContain('style="width:360px;height:420px"');
  });

  it("the head: Blocks, a count of every block, the filter, a ⋯ with Dock at the bottom, a × back to the shelf; it moves the palette when the host lets it", () => {
    const head = html.slice(html.indexOf('class="sky-palette-head'), html.indexOf("sky-catalog-list"));
    expect(head).toContain('class="sky-palette-head movable" data-sky-head="true" title="Drag to move the blocks"');
    expect(head).toContain('<strong class="sky-palette-title">Blocks</strong>');
    // Every kind (6) and every saved stop (2) — not just the pick's.
    expect(head).toMatch(/<span class="sky-palette-count"[^>]*>8<\/span>/);
    expect(head).toContain('<input class="fl-input sky-catalog-filter" type="text" placeholder="Filter blocks…" aria-label="Filter blocks" aria-controls="sky-palette-list"');
    expect(head).toContain('<summary aria-label="The blocks palette — more"');
    expect(head).toContain(">Dock at the bottom</button>");
    expect(head).toContain('<button type="button" class="fl-icon-btn sky-palette-close" aria-label="Close the blocks palette" title="Close — back to the shelf">×</button>');
    expect(head.indexOf("vf-menu")).toBeLessThan(head.indexOf("sky-palette-close"));
    // All blocks… is gone from the palette: it IS all the blocks.
    expect(html).not.toContain("sky-palette-all");
    // No onDock: no ⋯; no onClose: no ×; no onHeadDown (or click mode): no move hint.
    const still = render(createElement(SkyPalette, { onHeadDown() {}, draggable: false }));
    expect(still).toContain('class="sky-palette-head" data-sky-head="true"');
    expect(still).not.toContain("The blocks palette — more");
    expect(still).not.toContain("sky-palette-close");
    const src = code("components/flights/SkyPalette.tsx");
    expect(src).toContain("if (draggable && onHeadDown && headPress(e)) onHeadDown(e);");
    expect(src).toContain('closest("button, a, input, summary, [data-no-drag]")');
    expect(src).toContain('items={[{ key: "dock", label: "Dock at the bottom"');
    expect(src).toContain("onSelect: onDock }]");
    expect(src).toContain("onClick={onClose}");
  });

  it("focusFilter (opened by All blocks…) focuses the filter on mount", () => {
    const src = code("components/flights/SkyPalette.tsx");
    expect(src).toContain("autoFocus={focusFilter}");
    expect(code("components/flights/StopCatalog.tsx")).toContain("if (autoFocus) filterRef.current?.focus({ preventScroll: true });");
  });

  it("the body is the catalog's, compact: the Session card first, EVERY kind, EVERY saved stop — whatever the pick", () => {
    expect(html).toContain('<div class="sky-catalog-list sky-own-scroll compact sky-palette-body" id="sky-palette-list" role="listbox" aria-label="All blocks">');
    const sections = [...html.matchAll(/data-section="([^"]+)" role="group" aria-label="([^"]+)"/g)].map((m) => m[1] + ":" + m[2]);
    expect(sections).toEqual(["blocks:Blocks", "multi:Multi-step"]);
    const kinds = [...html.matchAll(/data-kind="([^"]+)"/g)].map((m) => m[1]);
    expect(kinds).toEqual(["agent", "you", "ship", "verify", "wait", "notify"]);
    expect(html.indexOf("sky-catalog-session")).toBeLessThan(html.indexOf('data-kind="agent"'));
    expect([...html.matchAll(/data-plan="([^"]+)"/g)].map((m) => m[1])).toEqual(["builtin:ticket", "p_mine"]);
    // The default pick (no saved stop on the shelf) changes nothing here.
    const plain = render(createElement(SkyPalette, { onPlace() {} }));
    expect(plain).toContain('data-section="multi"');
    expect(plain).toContain('data-kind="notify"');
    const src = code("components/flights/SkyPalette.tsx");
    expect(src).toContain("<StopCatalogBody");
    expect(src).toMatch(/onAdd=\{onPlace\}\s+place\s+compact\s+idPrefix="sky-palette"\s+listClass="sky-palette-body"/);
  });

  it("each card's ★ / ☆ sets the bottom shelf's pick (useShelfPick)", () => {
    expect(html).toContain('aria-label="Take “Ship” off the shelf"');
    expect(html).toContain('aria-label="Put “Notify” on the shelf"');
    expect(html).toContain('aria-label="Take “Ticket to merge” off the shelf"');
    expect(html).toContain('aria-label="Put “Mine” on the shelf"');
    const src = code("components/flights/SkyPalette.tsx");
    expect(src).toContain("const [shelf, changeShelf] = useShelfPick();");
    expect(src).toContain("onShelfToggle={(d: CatalogDrop) => changeShelf((p) => toggleShelf(p, d))}");
  });

  it("a saved stop's ⋯: Rename, Takes off…, On the shelf, Reset / Delete", () => {
    const mine = html.slice(html.indexOf('data-plan="p_mine"'));
    expect(mine).toContain('<summary aria-label="“Mine” — rename, delete, shelf"');
    expect(mine).toMatch(/>Rename<\/button><button type="button" class="vf-menu-item">Takes off…<\/button><button type="button" class="vf-menu-item">☆ On the shelf<\/button><button type="button" class="vf-menu-item vf-menu-danger">Delete<\/button>/);
  });

  it("every card drags with its chip's MIME; a click or a double-click places it, once", () => {
    // Session + 6 kinds + 2 saved stops.
    expect(html.match(/draggable="true"/g)).toHaveLength(9);
    expect(html).toMatch(/class="sky-catalog-card" data-kind="ship" draggable="true" title="Ship does its thing\nDrag onto a line or the sky, or click to put it on the board"/);
    expect(html).not.toContain("focused line");
    const src = code("components/flights/SkyPalette.tsx");
    // The Session card: the new-session type, a click-or-double opens the block.
    expect(src).toContain("const cd = useClickOrDouble(onSession);");
    expect(src).toContain("for (const [type, value] of sessionDragData()) e.dataTransfer.setData(type, value);");
    expect(src).toContain("onDoubleClick={() => cd.onDoubleClick()}");
    expect(html).toMatch(/<div role="option" aria-selected="false" class="sky-catalog-card sky-catalog-session" data-tour="flights-newblock" draggable="true"/);
    // Without onPlace the cards only drag.
    const drag = render(createElement(SkyPalette, {}));
    expect(drag).toMatch(/class="sky-catalog-card active" data-kind="agent" draggable="true" title="Agent does its thing\nDrag onto a line or the sky"/);
    expect(drag).toContain("sky-catalog-session drag-only");
  });

  it("the filter keeps the Session card only when it matches", () => {
    const src = code("components/flights/SkyPalette.tsx");
    expect(src).toContain('lead={(q) => (catalogMatch(q, "Session", "A new agent session") ? <SessionCard key="session" onSession={onSession} /> : null)}');
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
    expect(html).toContain("sky-own-scroll compact sky-palette-body");
    const css = cssOf("components/flights/SkyPalette.css");
    expect(css).toMatch(/\.sky-palette \.sky-palette-body \{\s*flex: 1;[^}]*min-height: 0;[^}]*overflow-y: auto;/);
  });

  it("the cards fill the frame; the filter takes the head's room; the frame holds its least size", () => {
    const css = cssOf("components/flights/SkyPalette.css");
    expect(css).toMatch(/\.sky-palette-head \.sky-catalog-filter\.fl-input\.fl-input \{\s*flex: 1;\s*min-width: 0;\s*max-width: none;/);
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
