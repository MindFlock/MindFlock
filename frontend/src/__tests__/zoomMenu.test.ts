/** The Sky's zoom readout as a menu (docs/flights.md "Round 17": task
 * `zoom-shortcuts`): a button reading "87 %" with aria-haspopup="menu"; the
 * open menu lists Fit all (Shift+1 / 0), Fit selection (Shift+2), Fit the
 * focused line (1), a divider, then 50 % · 100 % (Shift+0) · 150 % · 200 %,
 * the current preset ticked (nearest within 2 points), the fits greyed
 * without a selection / a focused line, presets outside min..max greyed.
 * Each row calls its prop; the keys walk and close the menu. Rendered on the
 * server (no DOM here): the open list through `ZoomMenuList`, the behaviour
 * through the exported pure helpers. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { css as cssOf } from "./sourceText";
import { ZOOM_PRESETS, ZoomMenu, ZoomMenuList, presetAt, zoomLabel, zoomMenuItems, zoomMenuKey, type ZoomMenuProps } from "../components/flights/ZoomMenu";

const props = (o: Partial<ZoomMenuProps> = {}): ZoomMenuProps => ({
  zoom: 0.87,
  min: 0.1,
  max: 3,
  onZoomTo: vi.fn(),
  onFitAll: vi.fn(),
  onFitSelection: vi.fn(),
  onFitFocused: vi.fn(),
  canFitSelection: true,
  canFitFocused: true,
  ...o,
});

const list = (o: Partial<ZoomMenuProps> = {}) => renderToStaticMarkup(createElement(ZoomMenuList, props(o)));
const rows = (html: string) => [...html.matchAll(/<button[^>]*data-item="([^"]+)"[^>]*>/g)].map((m) => ({ id: m[1], tag: m[0] }));

describe("the readout button", () => {
  it("reads the zoom as '87 %' and says it opens a menu, closed", () => {
    const html = renderToStaticMarkup(createElement(ZoomMenu, props()));
    expect(html).toContain('aria-haspopup="menu"');
    expect(html).toContain('aria-expanded="false"');
    expect(html).toContain(">87 %<");
    expect(html).toContain('class="sky-zoom-btn sky-zoom-read sky-zoom-menu-btn"');
    expect(html).not.toContain('role="menu"');
    expect(html).not.toContain("<select");
  });

  it("rounds the label", () => {
    expect(zoomLabel(1)).toBe("100 %");
    expect(zoomLabel(0.4449)).toBe("44 %");
    expect(zoomLabel(2.996)).toBe("300 %");
  });
});

describe("the open menu", () => {
  it("lists the fits, a divider, then the four presets, in order, as menuitems", () => {
    const html = list();
    expect(html).toContain('role="menu"');
    expect(rows(html).map((r) => r.id)).toEqual(["fit-all", "fit-selection", "fit-focused", "zoom-50", "zoom-100", "zoom-150", "zoom-200"]);
    expect(html.match(/role="menuitem"/g)).toHaveLength(7);
    expect(html.match(/role="separator"/g)).toHaveLength(1);
    // The divider sits between the fits and the presets.
    expect(html.indexOf('role="separator"')).toBeGreaterThan(html.indexOf('data-item="fit-focused"'));
    expect(html.indexOf('role="separator"')).toBeLessThan(html.indexOf('data-item="zoom-50"'));
    expect(ZOOM_PRESETS).toEqual([0.5, 1, 1.5, 2]);
    for (const label of ["Fit all", "Fit selection", "Fit the focused line", "50 %", "100 %", "150 %", "200 %"]) expect(html).toContain(">" + label + "<");
  });

  it("shows each row's keys as caps on the right", () => {
    const keys = Object.fromEntries(zoomMenuItems(props()).map((i) => [i.id, i.keys]));
    expect(keys).toEqual({
      "fit-all": [["Shift", "1"], ["0"]],
      "fit-selection": [["Shift", "2"]],
      "fit-focused": [["1"]],
      "zoom-50": [],
      "zoom-100": [["Shift", "0"]],
      "zoom-150": [],
      "zoom-200": [],
    });
    const html = list();
    const fitAll = html.slice(html.indexOf('data-item="fit-all"'), html.indexOf('data-item="fit-selection"'));
    expect(fitAll).toContain("<kbd>Shift</kbd><kbd>1</kbd>");
    expect(fitAll).toContain("<kbd>0</kbd>");
    expect(fitAll).toContain('aria-keyshortcuts="Shift+1 0"');
    const hundred = html.slice(html.indexOf('data-item="zoom-100"'), html.indexOf('data-item="zoom-150"'));
    expect(hundred).toContain("<kbd>Shift</kbd><kbd>0</kbd>");
  });

  it("greys Fit selection without a selection and Fit the focused line without one", () => {
    const by = (o: Partial<ZoomMenuProps>) => Object.fromEntries(rows(list(o)).map((r) => [r.id, / disabled=""/.test(r.tag)]));
    expect(by({})).toMatchObject({ "fit-all": false, "fit-selection": false, "fit-focused": false });
    expect(by({ canFitSelection: false })).toMatchObject({ "fit-all": false, "fit-selection": true, "fit-focused": false });
    expect(by({ canFitFocused: false })).toMatchObject({ "fit-selection": false, "fit-focused": true });
  });

  it("greys a preset outside the zoom's range", () => {
    const d = Object.fromEntries(zoomMenuItems(props({ min: 0.6, max: 1.5 })).map((i) => [i.id, i.disabled]));
    expect(d).toMatchObject({ "zoom-50": true, "zoom-100": false, "zoom-150": false, "zoom-200": true });
  });

  it("ticks the preset the zoom sits on (nearest within 2 points), none between", () => {
    expect(presetAt(1)).toBe(1);
    expect(presetAt(0.985)).toBe(1);
    expect(presetAt(1.02)).toBe(1);
    expect(presetAt(1.03)).toBeNull();
    expect(presetAt(0.87)).toBeNull();
    expect(presetAt(1.49)).toBe(1.5);
    expect(presetAt(2.01)).toBe(2);
    const ticked = (z: number) => zoomMenuItems(props({ zoom: z })).filter((i) => i.checked).map((i) => i.id);
    expect(ticked(0.87)).toEqual([]);
    expect(ticked(0.5)).toEqual(["zoom-50"]);
    expect(ticked(1.51)).toEqual(["zoom-150"]);
    const html = list({ zoom: 1 });
    const hundred = html.slice(html.indexOf('data-item="zoom-100"'), html.indexOf('data-item="zoom-150"'));
    expect(html).toMatch(/aria-current="true"[^>]*data-item="zoom-100"/);
    expect(html.match(/aria-current=/g)).toHaveLength(1);
    expect(hundred).toContain("✓");
    expect(html.match(/✓/g)).toHaveLength(1);
  });

  it("each row calls its own prop", () => {
    const p = props();
    const run = (id: string) => zoomMenuItems(p).find((i) => i.id === id)!.run();
    run("fit-all");
    expect(p.onFitAll).toHaveBeenCalledTimes(1);
    run("fit-selection");
    expect(p.onFitSelection).toHaveBeenCalledTimes(1);
    run("fit-focused");
    expect(p.onFitFocused).toHaveBeenCalledTimes(1);
    run("zoom-50");
    run("zoom-100");
    run("zoom-150");
    run("zoom-200");
    expect((p.onZoomTo as ReturnType<typeof vi.fn>).mock.calls.map((c) => c[0])).toEqual([0.5, 1, 1.5, 2]);
  });
});

describe("the keys inside the menu", () => {
  it("Esc and Tab close it; arrows walk the live rows, wrapping; Home / End jump", () => {
    expect(zoomMenuKey("Escape", 2, 5)).toBe("close");
    expect(zoomMenuKey("Tab", 0, 5)).toBe("close");
    expect(zoomMenuKey("ArrowDown", 0, 5)).toEqual({ to: 1 });
    expect(zoomMenuKey("ArrowDown", 4, 5)).toEqual({ to: 0 });
    expect(zoomMenuKey("ArrowUp", 0, 5)).toEqual({ to: 4 });
    expect(zoomMenuKey("ArrowUp", -1, 5)).toEqual({ to: 4 });
    expect(zoomMenuKey("ArrowDown", -1, 5)).toEqual({ to: 0 });
    expect(zoomMenuKey("Home", 3, 5)).toEqual({ to: 0 });
    expect(zoomMenuKey("End", 0, 5)).toEqual({ to: 4 });
    expect(zoomMenuKey("a", 0, 5)).toBeNull();
    expect(zoomMenuKey("ArrowDown", -1, 0)).toBeNull();
  });
});

describe("the stylesheet", () => {
  it("draws an inline menu, portalled at fixed coordinates, its rows the menu-row tier", () => {
    const c = cssOf("components/flights/ZoomMenu.css");
    expect(c).toMatch(/\.sky-zoom-menu-pop\s*\{[^}]*position:\s*fixed/);
    // The rows are the menu-row tier (StopMenu.css): its hover, ring and greying.
    expect(list()).toMatch(/class="fl-stopmenu-item sky-zoom-menu-item/);
    expect(cssOf("components/flights/StopMenu.css")).toMatch(/\.fl-stopmenu-item:focus-visible\s*\{[^}]*outline:\s*2px solid var\(--accent\)/);
    expect(c).toMatch(/\.sky-zoom-menu-keys kbd\s*\{/);
  });
});
