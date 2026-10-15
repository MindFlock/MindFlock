/** The sidebar row's session menu: a ⋮ at the row's right end (SidebarRow),
 * a right-click on the row, and the popover both open (RowMenu, placed by
 * lib/menuPlace). Rendered like sidebarRowState.test.ts. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import type { Instance } from "../api/types";

vi.mock("../api/client", async (orig) => ({
  ...((await orig()) as object),
  api: vi.fn(() => Promise.resolve({})),
  instApi: vi.fn(() => Promise.resolve({})),
}));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));
vi.mock("../lib/sessionActions", () => ({
  PR_FALLBACK_HINT: "",
  cleanupMissing: vi.fn(),
  commitSession: vi.fn(),
  copySession: vi.fn(),
  hasPrSupport: () => true,
  hideSession: vi.fn(),
  ideSession: vi.fn(),
  closeSession: vi.fn(),
  makePrSession: vi.fn(),
  mergeSession: vi.fn(),
  pauseSession: vi.fn(),
  pushSession: vi.fn(),
  resumeSession: vi.fn(),
  selectSession: vi.fn(),
}));
vi.mock("../lib/flockActions", () => ({ openThread: vi.fn(), pasteWrapup: vi.fn() }));
vi.mock("../lib/keymap", () => ({ isEditingTarget: () => false }));
// react-dom/server insists on a server snapshot for useSyncExternalStore; the
// row's terminal-socket and code-map subscriptions pass none (they only ever
// render in a browser). Reading the client snapshot is what a browser does.
vi.mock("react", async (orig) => {
  const r = (await orig()) as typeof import("react");
  return {
    ...r,
    useSyncExternalStore: <T,>(sub: (cb: () => void) => () => void, get: () => T, getServer?: () => T) =>
      r.useSyncExternalStore(sub, get, getServer ?? get),
  };
});

const { queryClient } = await import("../state/queries");
const { SidebarRow } = await import("../components/sidebar/SidebarRow");
const { placeMenu, refitMenu, MENU_MARGIN } = await import("../lib/menuPlace");
const { code, css } = await import("./sourceText");

const row = (o: Partial<Instance>): Instance =>
  ({
    title: "s",
    branch: "feat/x",
    repo: "r",
    folder: "/tmp/r",
    program: "claude",
    status: "running",
    stage: "agent",
    activity: "idle",
    activity_since: 0,
    diff_stat: null,
    queue: null,
    setup: null,
    check: null,
    last_turn: null,
    ...o,
  }) as unknown as Instance;

function render(inst: Instance): string {
  return renderToStaticMarkup(
    createElement(
      QueryClientProvider,
      { client: queryClient },
      createElement(SidebarRow, {
        inst,
        idx: 0,
        onScreen: false,
        dropCue: null,
        onDragState: () => {},
        onDropCue: () => {},
        onDropRow: () => {},
      })
    )
  );
}


/** The row's opening tags, in document order, of the controls on .inst-row. */
const controls = (html: string) =>
  Array.from(html.matchAll(/<(button|span) [^>]*class="([^"]*)"/g)).map((m) => m[2]);

describe("the row's actions button (⋮)", () => {
  it("sits at the right end, just before the ✕ — not between the number and the name", () => {
    const html = render(row({ title: "kebab" }));
    const order = controls(html);
    const kebab = order.findIndex((c) => c.startsWith("row-kebab"));
    const kill = order.findIndex((c) => c.startsWith("kill"));
    const title = order.findIndex((c) => c === "title");
    const chip = order.findIndex((c) => c.startsWith("stagechip"));
    expect(kebab).toBeGreaterThan(title);
    expect(kebab).toBeGreaterThan(chip);
    expect(kill).toBe(kebab + 1);
    // The old text-glyph opener is gone.
    expect(html).not.toContain('class="chevron');
    expect(html).not.toContain("⋯");
  });

  it("is an SVG kebab menu button with the same accessible name and popup state", () => {
    const html = render(row({ title: "aria" }));
    const btn = /<button[^>]*class="row-kebab"[^>]*>([\s\S]*?)<\/button>/.exec(html);
    expect(btn, html).toBeTruthy();
    // Round 5 (finding 20): its own accessible name, not the pane ⋯'s.
    expect(btn![0]).toContain('aria-label="Session menu for aria"');
    expect(btn![0]).toContain('aria-haspopup="menu"');
    expect(btn![0]).toContain('aria-expanded="false"');
    expect(btn![0]).toContain('type="button"');
    expect(btn![1]).toMatch(/^<svg[^>]*aria-hidden="true"/);
    expect((btn![1].match(/<circle /g) || []).length).toBe(3);
  });

  it("a pending (force-started) row has no menu to open", () => {
    const html = render(row({ title: "pend", pending: true } as Partial<Instance>));
    expect(html).not.toContain("row-kebab");
  });

  it("a remote row keeps it (its menu leaves out what the server refuses)", () => {
    const html = render(row({ title: "rig::x", device: "rig" } as Partial<Instance>));
    expect(html).toContain('class="row-kebab"');
  });

  it("a right-click on the row opens the same menu at the pointer; Shift+F10 / Menu under the ⋮", () => {
    const src = code("components/sidebar/SidebarRow.tsx");
    expect(src).toContain("onContextMenu={onRowContextMenu}");
    expect(src).toMatch(/setMenuAt\(\{ x: e\.clientX, y: e\.clientY, flipY: e\.clientY, align: "start"/);
    // The rename input (and links) keep the browser's own menu.
    expect(src).toMatch(/if \(pending \|\| editing\) return;[\s\S]{0,120}isEditingTarget\(t\)/);
    expect(src).toMatch(/e\.key === "ContextMenu" \|\| \(e\.key === "F10" && e\.shiftKey/);
    // Both open the ONE session menu model, in the popover.
    expect(src).toMatch(/<RowMenu[\s\S]{0,300}<SessionMenu[\s\S]{0,40}model=\{sessionMenu\(inst, ctx, \{/);
  });

  it("never shifts the layout: hidden by opacity, always holding its slot", () => {
    const sheet = css("components/sidebar/SidebarRow.css");
    const rule = /\.inst \.row-kebab \{([^}]*)\}/.exec(sheet)?.[1] || "";
    expect(rule).toContain("opacity: 0");
    expect(rule).toContain("flex: none");
    expect(rule).toMatch(/width: 16px/);
    // Nothing ever display:none's or visibility:hidden's it.
    expect(sheet).not.toMatch(/\.row-kebab[^{]*\{[^}]*(display: none|visibility: hidden)/);
    // Shown on hover, keyboard focus in the row, the selected row, while open,
    // and always where there is no hover.
    for (const sel of [
      ".inst-row:hover .row-kebab",
      ".inst-row:has(:focus-visible) .row-kebab",
      ".inst.active .row-kebab",
      ".inst .row-kebab.open",
    ])
      expect(sheet).toContain(sel);
    expect(sheet).toMatch(/@media \(hover: none\) \{\s*\.inst \.row-kebab \{\s*opacity: 1;/);
    // A window row holds the same slot, so its chip lines up.
    expect(code("components/sidebar/WindowList.tsx")).toContain('className="row-kebab-slot"');
    expect(sheet).toMatch(/\.inst \.row-kebab-slot \{[^}]*width: 16px/);
  });

  it("the menu is a popover over the whole window, not an accordion in the row", () => {
    const menu = code("components/sidebar/RowMenu.tsx");
    expect(menu).toContain("createPortal(");
    expect(menu).toContain("document.body");
    expect(/\.row-menu \{([^}]*)\}/.exec(css("components/sidebar/SidebarRow.css"))?.[1]).toContain("position: fixed");
    expect(code("components/sidebar/SidebarRow.tsx")).not.toContain("inst-actions");
  });
});

describe("placeMenu — the whole menu on screen", () => {
  const VW = 1044;
  const VH = 904;
  const m = MENU_MARGIN;

  it("hangs under a button, right edges aligned", () => {
    const p = placeMenu({ x: 240, y: 300, flipY: 270, align: "end" }, 200, 300, VW, VH);
    expect(p).toEqual({ left: 40, top: 300, maxHeight: VH - 2 * m });
  });

  it("a button near the left edge: shifted right, never off screen", () => {
    expect(placeMenu({ x: 220, y: 300, flipY: 270, align: "end" }, 280, 300, VW, VH).left).toBe(m);
  });

  it("opens at the pointer, and flips above it with no room below", () => {
    expect(placeMenu({ x: 100, y: 200, flipY: 200, align: "start" }, 280, 400, VW, VH)).toMatchObject({ left: 100, top: 200 });
    expect(placeMenu({ x: 100, y: 800, flipY: 800, align: "start" }, 280, 400, VW, VH).top).toBe(400);
  });

  it("flips to the pointer's left with no room on its right", () => {
    expect(placeMenu({ x: 1000, y: 100, flipY: 100, align: "start" }, 280, 300, VW, VH).left).toBe(720);
  });

  it("no room either way: clamped into the window, and taller than it scrolls", () => {
    const p = placeMenu({ x: 100, y: 500, flipY: 500, align: "start" }, 280, 700, VW, VH);
    expect(p.top).toBe(VH - m - 700);
    const tall = placeMenu({ x: 100, y: 500, flipY: 500, align: "start" }, 280, 2000, VW, VH);
    expect(tall).toMatchObject({ top: m, maxHeight: VH - 2 * m });
  });

  it("re-fits a grown menu by nudging it, never flipping it across the anchor", () => {
    const first = placeMenu({ x: 100, y: 400, flipY: 400, align: "start" }, 280, 300, VW, VH);
    expect(first.top).toBe(400);
    expect(refitMenu(first, 280, 350, VW, VH).top).toBe(400); // still fits: stays
    expect(refitMenu(first, 280, 700, VW, VH).top).toBe(VH - m - 700); // nudged up just enough
  });
});
