/** Resizable sidebar + tighter session rows (moved from
 * tests/unit/test_frontend_sidebar_resize.py, which grepped the built
 * style.css / app.js).
 *
 * Pinned: the sidebar column is a CSS var (`--sidebar-w`) the user sizes with a
 * keyboard-operable handle, the width survives a reload (`mf_sidebar_w`), the
 * handle hides while the sidebar is collapsed or a dialog is up, a drag locks
 * the cursor and selection, and the row's density rules (12px title, hover-only
 * out-of-flow grip, pills sized to content, CSS-only truncation) — so a refactor
 * can't silently give the fixed-260px sidebar back. The rules are read from the
 * sheet that owns them; the handle is server-rendered. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { ALL_CODE, ALL_CSS, src } from "./sourceText";

/** A Map-backed localStorage, installed BEFORE the store module loads (it
 * reads its persisted keys at import time). */
const storage = new Map<string, string>();
vi.stubGlobal("localStorage", {
  getItem: (k: string) => (storage.has(k) ? storage.get(k)! : null),
  setItem: (k: string, v: string) => void storage.set(k, String(v)),
  removeItem: (k: string) => void storage.delete(k),
  clear: () => storage.clear(),
});

/** `selector`'s declarations in `css` (comments stripped), anchored at a line
 * start so a short selector can't match the tail of a longer one (plain
 * "body" would otherwise land in ".pane-body"). */
function rule(css: string, selector: string): string {
  const text = "\n" + css.replace(/\/\*[\s\S]*?\*\//g, "");
  const start = text.indexOf("\n" + selector + " {");
  if (start < 0) throw new Error("no rule for " + selector);
  return text.slice(start, text.indexOf("}", start));
}

const SIDEBAR_CSS = src("components/sidebar/Sidebar.css");
const ROW_CSS = src("components/sidebar/SidebarRow.css");

describe("the sidebar column is user-sized", () => {
  it("reads the width from a CSS var; 260px survives only as the fallback", () => {
    expect(src("styles/base.css")).toContain("grid-template-columns: var(--sidebar-w, 260px) 1fr");
    // The old fixed column is gone from every sheet.
    expect(ALL_CSS).not.toContain("grid-template-columns: 260px 1fr");
  });

  it("has a resize handle positioned against the sidebar", () => {
    // The handle is positioned against the sidebar, so the sidebar anchors it.
    expect(rule(SIDEBAR_CSS, "#sidebar")).toContain("position: relative");
    const handle = rule(SIDEBAR_CSS, ".sidebar-resizer");
    expect(handle).toContain("cursor: col-resize");
    expect(handle).toContain("position: absolute");
    // Overhangs into the grid's padding instead of covering the session list's
    // scrollbar, and paints above #grid (a later sibling).
    expect(handle).toContain("left: 100%");
    expect(handle).toContain("z-index: 40");
  });

  it("renders a keyboard-operable, announced handle that drives --sidebar-w", async () => {
    const { SidebarResizer } = await import("../components/sidebar/SidebarResizer");
    const html = renderToStaticMarkup(createElement(SidebarResizer));
    expect(html).toContain('class="sidebar-resizer"');
    expect(html).toContain('aria-label="Resize sidebar"');
    expect(html).toContain('aria-orientation="vertical"');
    const resizer = src("components/sidebar/SidebarResizer.tsx");
    expect(resizer).toContain("setSidebarWidth");
    expect(resizer).toContain('document.body.style.setProperty("--sidebar-w", px + "px")');
  });

  it("persists the width across a reload (mf_sidebar_w)", async () => {
    const { useUi, SIDEBAR_DEFAULT_W } = await import("../state/store");
    useUi.getState().setSidebarWidth(SIDEBAR_DEFAULT_W + 40);
    expect(storage.get("mf_sidebar_w")).toBe(JSON.stringify(SIDEBAR_DEFAULT_W + 40));
    vi.resetModules();
    const fresh = await import("../state/store");
    expect(fresh.useUi.getState().sidebarWidth).toBe(SIDEBAR_DEFAULT_W + 40);
  });

  it("hides the handle while the sidebar is collapsed (Ctrl+B)", () => {
    // A handle floating over the grid's left edge with no sidebar behind it
    // would resize nothing.
    expect(rule(SIDEBAR_CSS, "body.sidebar-collapsed .sidebar-resizer")).toContain("display: none");
  });

  it("hides the handle while any dialog is open", () => {
    // The .modal backdrop is fixed with no z-index, so the handle's z-index: 40
    // painted its accent line over every open dialog and kept answering :hover.
    expect(rule(SIDEBAR_CSS, "body:has(.modal:not(.hidden)) .sidebar-resizer")).toContain("display: none");
  });

  it("locks the cursor and selection during a drag", () => {
    const dragging = rule(SIDEBAR_CSS, "body.sidebar-resizing,\nbody.sidebar-resizing *");
    expect(dragging).toContain("cursor: col-resize !important");
    expect(dragging).toContain("user-select: none !important");
  });
});

describe("row density: more of the name fits on the line", () => {
  it("sets the row title (and its rename input) smaller than body text", () => {
    expect(rule(ROW_CSS, ".inst .title")).toContain("font-size: 12px");
    // The inline rename input must match, or the row height jumps mid-edit.
    expect(rule(ROW_CSS, ".inst input.title-edit")).toContain("font-size: 12px");
  });

  it("keeps the row grip hover-only and out of flow", () => {
    const grip = rule(ROW_CSS, ".inst .grip");
    expect(grip).toContain("position: absolute");
    expect(grip).toContain("left: 1px");
    expect(grip).toContain("opacity: 0;"); // not 0.35 — fully hidden at rest
    expect(rule(ROW_CSS, ".inst-row:hover .grip")).toContain("opacity: 0.85");
    expect(src("components/sidebar/SidebarRow.tsx")).toContain('className="grip"');
  });

  it("reserves a lane for the grip in the row's left padding", () => {
    const row = rule(ROW_CSS, ".inst-row");
    expect(row).toContain("padding: 9px 8px 9px 12px");
    expect(row).toContain("position: relative");
  });

  it("sizes sidebar pills to their content, not a fixed cap", () => {
    const chip = rule(ROW_CSS, ".inst .stagechip");
    expect(chip).toContain("max-width: none");
    expect(chip).toContain("flex: 0 1 auto");
    expect(chip).toContain("min-width: 0");
    // The shared base (the first `.stagechip {` in the cascade, DiffTab.css)
    // still caps chips OUTSIDE the sidebar, and ellipsizes when it must shrink.
    const base = rule(src("components/grid/DiffTab.css"), ".stagechip");
    expect(base).toContain("max-width: 96px");
    expect(base).toContain("text-overflow: ellipsis");
    expect(base).toContain("white-space: nowrap");
  });

  it("truncates the title in CSS only — no JavaScript character budget", () => {
    expect(ALL_CODE).not.toContain("MAX_NAME");
    const title = rule(ROW_CSS, ".inst .title");
    expect(title).toContain("text-overflow: ellipsis");
    expect(title).toContain("overflow: hidden");
  });

  it("keeps the row grip on the bars' .bar-grip pattern", () => {
    const bar = rule(src("components/sidebar/toolbars.css"), ".bar-grip");
    expect(bar).toContain("position: absolute");
    expect(bar).toContain("opacity: 0;");
  });
});
