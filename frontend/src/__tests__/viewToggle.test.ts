/** The Grid · Sky toggle lives on the sidebar's View row, next to 9 (the
 * owner: "I wish the grid/sky icon was at the bottom next to 9 instead of
 * just Sky down there"). The row is always there; the calm first hour hides
 * only its numbers. The top bar wears the toggle only while that row is out of
 * sight — the sidebar hidden, or a phone-narrow window — so there is exactly
 * one visible toggle. */

/// <reference types="vite/client" />
import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { code, css } from "./sourceText";
import type { Flight } from "../lib/flights";

/** A Map-backed localStorage, installed BEFORE the store module loads. */
const storage = new Map<string, string>();
vi.stubGlobal("localStorage", {
  getItem: (k: string) => (storage.has(k) ? storage.get(k)! : null),
  setItem: (k: string, v: string) => void storage.set(k, String(v)),
  removeItem: (k: string) => void storage.delete(k),
  clear: () => storage.clear(),
});
vi.mock("../api/client", () => ({ api: vi.fn(() => new Promise(() => {})), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));
// The bar's bell and mic are their own suites; here they are only neighbours.
vi.mock("../components/NotificationsBell", () => ({ NotificationsBell: () => null }));
vi.mock("../components/TopBarMic", () => ({ TopBarMic: () => null }));
vi.mock("../components/EventToasts", () => ({ redrawFavicon: vi.fn() }));

const { QueryClient, QueryClientProvider } = await import("@tanstack/react-query");
const { useUi } = await import("../state/store");
const { ViewRow } = await import("../components/sidebar/Sidebar");
const { TopBar } = await import("../components/TopBar");

// The queries' poll intervals read `document.hidden`; a server render has no
// document, so a quiet one stands in (after the imports: some modules probe
// for a real one when they load). TopBar reads `window` (the dev shell's
// badge): a bare one is a browser tab.
(globalThis as Record<string, unknown>).document ??= {
  hidden: false,
  title: "MindFlock",
  addEventListener: () => {},
  removeEventListener: () => {},
  getElementById: () => null,
  querySelector: () => null,
};
(globalThis as Record<string, unknown>).window ??= globalThis;

/** A server render reads the store's INITIAL state (zustand's server
 * snapshot), so a patch lands in both. */
function setUi(patch: Record<string, unknown>) {
  useUi.setState(patch as never);
  Object.assign(useUi.getInitialState(), useUi.getState());
}

/** A flight perched on a `you` stop (`halted` = it failed and waits for a
 * retry or a skip): what perchBadge counts. */
const perched = (id: string, state: "waiting_you" | "halted" = "waiting_you") =>
  ({
    id,
    session: "s-" + id,
    state,
    cursor: 0,
    steps: [{ kind: "you", label: "Approve" }],
  }) as unknown as Flight;

function render(el: ReactElement, flights: Flight[] = []) {
  const c = new QueryClient({ defaultOptions: { queries: { retry: false, enabled: false } } });
  c.setQueryData(["flights"], flights);
  return renderToStaticMarkup(createElement(QueryClientProvider, { client: c }, el));
}

const buttons = (html: string) => [...html.matchAll(/<button[^>]*>/g)].map((m) => m[0]);
const viewButtons = (html: string, cls: string) =>
  buttons(html).filter((b) => b.includes(`class="${cls}`) && /data-view="(grid|sky)"/.test(b));

afterEach(() => {
  setUi({ view: "grid", viewMode: "auto", sidebarHidden: false, narrow: false });
});

describe("the sidebar's View row: Auto 1 2 4 9 │ Grid Sky", () => {
  it("holds the numbers, then the toggle's two buttons, the active one filled", () => {
    setUi({ view: "grid", viewMode: "4" });
    const html = render(createElement(ViewRow, { calm: false }));
    expect(html).toMatch(/^<div id="view-modes"><span class="vm-label">View<\/span>/);
    for (const v of ["auto", "1", "2", "4", "9"]) expect(html).toContain(`data-view="${v}"`);
    expect(html).toMatch(/class="vm active" data-view="4">4</);
    // The thin rule sits between the numbers and the pair (Sidebar.css).
    expect(html.indexOf('data-view="9"')).toBeLessThan(html.indexOf('class="vt-pair"'));
    const [grid, sky] = viewButtons(html, "vt");
    expect(grid).toContain('data-view="grid"');
    expect(grid).toContain('class="vt active"');
    expect(grid).toContain('aria-pressed="true"');
    expect(sky).toContain('data-view="sky"');
    expect(sky).toContain('data-tour="view-sky"');
    expect(sky).toContain('class="vt"');
    expect(sky).toContain('aria-pressed="false"');
    // Glyph + word: four panes for the grid, the bird for the sky.
    expect(html).toMatch(/<svg class="vt-icon"[^>]*><rect/);
    expect(html).toMatch(/<svg class="fl-bird vt-icon"/);
    expect(html).toContain('<span class="vt-word">Grid</span>');
    expect(html).toContain('<span class="vt-word">Sky</span>');
    // The tooltips: the view's blurb and its shortcut.
    expect(grid).toMatch(/title="Grid — Every session as a pane[^"]*\(Ctrl\+Y\)"/);
    expect(sky).toMatch(/title="Sky — Every session as a line on one canvas[^"]*\(Ctrl\+Y\)"/);
  });

  it("in the sky, the bird is filled and no number lights (a number is the grid)", () => {
    setUi({ view: "sky", viewMode: "4" });
    const html = render(createElement(ViewRow, { calm: false }));
    expect(html).not.toContain('class="vm active"');
    const [grid, sky] = viewButtons(html, "vt");
    expect(grid).not.toContain("active");
    expect(sky).toContain('class="vt active"');
  });

  it("the Sky half wears the perch count — nothing at zero, red when one halted", () => {
    expect(render(createElement(ViewRow, { calm: false }))).not.toContain("vt-count");
    const two = render(createElement(ViewRow, { calm: false }), [perched("a"), perched("b")]);
    expect(two).toMatch(/<span class="vt-word">Sky<\/span><span class="vt-count">2<\/span><\/button>/);
    expect(two).toMatch(/aria-label="Sky, 2 need you"/);
    expect(two).toMatch(/title="Sky — [^"]*\n2 need you"/);
    const halted = render(createElement(ViewRow, { calm: false }), [perched("a", "halted")]);
    expect(halted).toContain('<span class="vt-count is-failing">1</span>');
  });

  it("the calm first hour hides the numbers, never the toggle", () => {
    const calm = render(createElement(ViewRow, { calm: true }));
    expect(calm).toContain('id="view-modes"');
    expect(calm).toContain('<span class="vm-label">View</span>');
    expect(calm).not.toContain("vm-nums");
    expect(calm).not.toContain('data-view="auto"');
    expect(viewButtons(calm, "vt")).toHaveLength(2);
    expect(calm).toContain('data-tour="view-sky"');
    // In the sky too: the toggle is what brings you back.
    setUi({ view: "sky" });
    expect(render(createElement(ViewRow, { calm: true }))).not.toContain("vm-nums");
    // A count already picked stays visible (it is in effect).
    setUi({ view: "grid", viewMode: "2" });
    expect(render(createElement(ViewRow, { calm: true }))).toContain('class="vm active" data-view="2"');
  });

  it("a number picks the grid at that count; ⊞ the grid at the current one; the bird the sky", () => {
    const side = code("components/sidebar/Sidebar.tsx");
    expect(side).toMatch(/ui\.setViewMode\(v\);\s*setView\("grid"\);/);
    expect(side).toContain("onClick={() => setView(v)}");
    expect(side).toContain('{(!calm || ui.viewMode !== "auto") && (');
    expect(side).toContain("<ViewRow calm={calm} />");
    // No old Sky chip beside the numbers.
    expect(side).not.toContain("vm-sky");
  });

  it("styles: the .vm family, the accent fill when active, words drop on a narrow row", () => {
    const s = css("components/sidebar/Sidebar.css");
    expect(s).toMatch(/#view-modes \.vt \{[^}]*border-radius: 6px;[^}]*font-size: 11px|#view-modes \.vt \{[^}]*font-size: 11px;[^}]*border-radius: 6px;/);
    expect(s).toMatch(/#view-modes \.vt\.active \{[^}]*background: var\(--accent\);/);
    expect(s).toMatch(/#view-modes \.vm-nums \{[^}]*border-right: 1px solid var\(--border\);/);
    expect(s).toMatch(/#view-modes \{\s*container: view-row \/ inline-size;/);
    expect(s).toMatch(/@container view-row \(max-width: \d+px\) \{\s*#view-modes \.vm-nums ~ \.vt-pair \.vt-word \{\s*display: none;/);
  });
});

describe("the top bar wears the toggle only where the row is out of sight", () => {
  const bar = () => render(createElement(TopBar));

  it("sidebar shown: no toggle on the bar — the sidebar's is the one", () => {
    setUi({ sidebarHidden: false, narrow: false });
    const html = bar();
    expect(html).not.toContain("tb-views");
    expect(html).not.toContain('data-tour="view-sky"');
    expect(html).toContain('id="new-btn"');
    expect(html).toContain('id="intake-btn"');
  });

  it("sidebar hidden: the pair is back beside New, Sky the tour's target", () => {
    setUi({ sidebarHidden: true, narrow: false });
    const html = bar();
    expect(html).toContain('data-fold="view"');
    expect(html).toContain('id="view-grid-btn"');
    expect(html).toContain('id="view-sky-btn"');
    expect(html).toContain('data-tour="view-sky"');
    expect(html.indexOf('id="new-btn"')).toBeLessThan(html.indexOf('data-fold="view"'));
    expect(html.indexOf('data-fold="view"')).toBeLessThan(html.indexOf('id="intake-btn"'));
  });

  it("phone-narrow: on the bar (where the ⋯ fold can take it)", () => {
    // The narrow store hides the sidebar by itself; an overlay drawer opened
    // over it doesn't take the bar's toggle away.
    setUi({ sidebarHidden: false, narrow: true });
    expect(bar()).toContain('data-fold="view"');
    const top = code("components/TopBar.tsx");
    expect(top).toContain("const barViews = ui.sidebarHidden || ui.narrow;");
    expect(top).toMatch(/barViews && \{\s*key: "view",/);
  });

  it("GridGlyph is one shared glyph, not two copies", () => {
    expect(code("components/ViewGlyphs.tsx")).toContain("export function GridGlyph(");
    expect(code("components/TopBar.tsx")).toContain('import { GridGlyph } from "./ViewGlyphs";');
    expect(code("components/sidebar/Sidebar.tsx")).toContain('import { GridGlyph } from "../ViewGlyphs";');
    expect(code("components/TopBar.tsx")).not.toContain("function GridGlyph");
  });
});
