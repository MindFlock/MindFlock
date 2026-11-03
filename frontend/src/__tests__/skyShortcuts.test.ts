/** The Sky's `?` sheet (docs/flights.md "Round 17": task `zoom-shortcuts`):
 * lib/skyShortcuts `SKY_SHORTCUT_GROUPS` is the sky's real key list in six
 * groups, caps one per key; `withPlatform` shows ⌘ / ⌥ on a Mac; the sheet
 * is a card (role="dialog", aria-label "Keyboard shortcuts", never a native
 * <dialog>) listing the groups with <kbd> caps under a filter that hides the
 * rows not matching by key or words; `?` (with an empty filter) or Esc
 * closes. Server-rendered (no DOM here): the card through
 * `SkyShortcutsCard`, the keys through `sheetKey`. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { code, css as cssOf } from "./sourceText";
import { SKY_SHORTCUT_GROUPS, filterShortcuts, isMacLike, withPlatform } from "../lib/skyShortcuts";
import { SkyShortcuts, SkyShortcutsCard, sheetKey, type ShortcutGroup } from "../components/flights/SkyShortcuts";

const card = (filter = "", groups: readonly ShortcutGroup[] = SKY_SHORTCUT_GROUPS) =>
  renderToStaticMarkup(createElement(SkyShortcutsCard, { groups, filter, onFilter: () => {}, onClose: () => {} }));
const titles = (gs: readonly ShortcutGroup[]) => gs.map((g) => g.title);
const find = (keys: string[]) => SKY_SHORTCUT_GROUPS.flatMap((g) => g.items).find((i) => i.keys.join("+") === keys.join("+"));

describe("the sky's shortcut list", () => {
  it("has the six groups, in order, none empty", () => {
    expect(titles(SKY_SHORTCUT_GROUPS)).toEqual(["Move around", "Select", "Edit stops", "Chains and objects", "The palette", "Help"]);
    for (const g of SKY_SHORTCUT_GROUPS) expect(g.items.length).toBeGreaterThan(0);
  });

  it("lists the keys SkyView answers to, and the round's new ones, one cap per key", () => {
    for (const k of [
      ["0"],
      ["1"],
      ["+"],
      ["−"],
      ["T"],
      ["V"],
      ["H"],
      ["Esc"],
      ["Enter"],
      ["Delete"],
      ["Space", "drag"],
      ["Shift", "drag"],
      ["Ctrl", "A"],
      ["Ctrl", "Z"],
      ["Ctrl", "Shift", "Z"],
      ["Shift", "1"],
      ["Shift", "2"],
      ["Shift", "0"],
      ["Tab"],
      ["double-click"],
      ["F"],
      ["?"],
      ["Alt", "drag"],
    ])
      expect(find(k), k.join("+")).toBeTruthy();
    for (const it of SKY_SHORTCUT_GROUPS.flatMap((g) => g.items)) {
      expect(it.what.length).toBeGreaterThan(0);
      for (const cap of it.keys) if (cap !== "+") expect(cap, "one cap per key").not.toContain("+");
    }
  });

  it("the keys SkyView's handler reads are really there", () => {
    const sky = code("components/flights/SkyView.tsx");
    for (const k of ['k === "0"', 'k === "1"', 'k === "t"', 'k === "v"', 'k === "h"', 'k === "+"', 'k === "-"']) expect(sky).toContain(k);
  });
});

describe("withPlatform", () => {
  it("turns Ctrl into ⌘ and Alt into ⌥ on a Mac, and nothing else", () => {
    const mac = withPlatform(SKY_SHORTCUT_GROUPS, true);
    const caps = mac.flatMap((g) => g.items.flatMap((i) => i.keys));
    expect(caps).not.toContain("Ctrl");
    expect(caps).not.toContain("Alt");
    const all = (gs: readonly ShortcutGroup[]) => gs.flatMap((g) => g.items.map((i) => i.keys.join(" ")));
    expect(all(mac)).toContain("⌘ Z");
    expect(all(mac)).toContain("⌘ Shift Z");
    expect(all(mac)).toContain("⌥ drag");
    expect(all(mac)).toContain("Shift 1");
    expect(titles(mac)).toEqual(titles(SKY_SHORTCUT_GROUPS));
  });

  it("leaves the groups as they are elsewhere, and never mutates the list", () => {
    const before = JSON.stringify(SKY_SHORTCUT_GROUPS);
    expect(withPlatform(SKY_SHORTCUT_GROUPS, false)).toEqual(SKY_SHORTCUT_GROUPS);
    withPlatform(SKY_SHORTCUT_GROUPS, true);
    expect(JSON.stringify(SKY_SHORTCUT_GROUPS)).toBe(before);
  });

  it("is idempotent, so a Mac list passed twice stays ⌘", () => {
    const once = withPlatform(SKY_SHORTCUT_GROUPS, true);
    expect(withPlatform(once, true)).toEqual(once);
  });

  it("isMacLike reads the navigator (none in node)", () => {
    const nav = (globalThis as { navigator?: unknown }).navigator;
    if (!nav) expect(isMacLike()).toBe(false);
    vi.stubGlobal("navigator", { platform: "MacIntel", userAgent: "" });
    expect(isMacLike()).toBe(true);
    vi.stubGlobal("navigator", { platform: "Win32", userAgent: "Windows NT" });
    expect(isMacLike()).toBe(false);
    vi.unstubAllGlobals();
  });
});

describe("the filter", () => {
  const whats = (q: string) => filterShortcuts(SKY_SHORTCUT_GROUPS, q).flatMap((g) => g.items.map((i) => i.what));

  it("keeps everything with nothing typed", () => {
    expect(filterShortcuts(SKY_SHORTCUT_GROUPS, "  ")).toEqual(SKY_SHORTCUT_GROUPS);
  });

  it("matches by words, case-folded, every word", () => {
    expect(whats("UNDO")).toEqual(["Undo the last removal"]);
    expect(whats("fit selection")).toEqual(["Fit the selection"]);
    expect(whats("zebra")).toEqual([]);
  });

  it("matches by keys: 'ctrl z', 'ctrl+z', 'shift 1', '?', '+', and ⌘ answers to ctrl", () => {
    expect(whats("ctrl z")).toEqual(["Undo the last removal", "Redo"]);
    expect(whats("ctrl+shift+z")).toEqual(["Redo"]);
    expect(whats("shift 1")).toContain("Fit all the chains");
    expect(whats("?")).toEqual(["These shortcuts"]);
    expect(whats("+")).toContain("Zoom in");
    expect(filterShortcuts(withPlatform(SKY_SHORTCUT_GROUPS, true), "ctrl z").flatMap((g) => g.items.map((i) => i.what))).toEqual(["Undo the last removal", "Redo"]);
  });

  it("a group's name finds its rows; a group with nothing left drops out", () => {
    const pal = filterShortcuts(SKY_SHORTCUT_GROUPS, "palette");
    expect(titles(pal)).toContain("The palette");
    expect(pal.find((g) => g.title === "The palette")!.items).toHaveLength(SKY_SHORTCUT_GROUPS.find((g) => g.title === "The palette")!.items.length);
    expect(titles(filterShortcuts(SKY_SHORTCUT_GROUPS, "undo"))).toEqual(["Edit stops"]);
  });
});

describe("the sheet", () => {
  it("is a card named Keyboard shortcuts, not a native dialog, with a filter at the top", () => {
    const html = card();
    expect(html).toContain('role="dialog"');
    expect(html).toContain('aria-label="Keyboard shortcuts"');
    expect(html).not.toContain("<dialog");
    expect(html).toContain('class="sky-keys-filter"');
    expect(html.indexOf("sky-keys-filter")).toBeLessThan(html.indexOf("sky-keys-group"));
    expect(html).toContain('aria-label="Close"');
  });

  it("renders every group with its head and each key as a <kbd> cap", () => {
    const html = card();
    for (const g of SKY_SHORTCUT_GROUPS) expect(html).toContain("<h4>" + g.title + "</h4>");
    expect(html).toContain("<kbd>Ctrl</kbd><kbd>Shift</kbd><kbd>Z</kbd>");
    expect(html).toContain('<kbd>Space</kbd><kbd class="is-gesture">drag</kbd>');
    expect(html).toContain("<kbd>?</kbd>");
    const rowCount = SKY_SHORTCUT_GROUPS.reduce((n, g) => n + g.items.length, 0);
    expect(html.match(/class="sky-keys-row"/g)).toHaveLength(rowCount);
  });

  it("hides the rows the filter doesn't match, and says so when none do", () => {
    const html = card("undo");
    expect(html.match(/class="sky-keys-row"/g)).toHaveLength(1);
    expect(html).toContain("Undo the last removal");
    expect(html).not.toContain("<h4>Move around</h4>");
    expect(html).toContain('value="undo"');
    const none = card("zebra");
    expect(none).not.toContain("sky-keys-row");
    expect(none).toContain("No shortcut matches “zebra”.");
  });

  it("draws nothing while closed", () => {
    expect(renderToStaticMarkup(createElement(SkyShortcuts, { open: false, onClose() {}, groups: SKY_SHORTCUT_GROUPS }))).toBe("");
  });

  it("Esc closes; ? closes with an empty filter (else it is a character); Tab is kept in the card", () => {
    expect(sheetKey("Escape", "")).toBe("close");
    expect(sheetKey("Escape", "zoom")).toBe("close");
    expect(sheetKey("?", "")).toBe("close");
    expect(sheetKey("?", "ctrl")).toBeNull();
    expect(sheetKey("Tab", "")).toBe("trap");
    expect(sheetKey("a", "")).toBeNull();
    expect(sheetKey("0", "")).toBeNull();
  });

  it("listens in the capture phase while open, focuses the filter and gives the focus back", () => {
    const src = code("components/flights/SkyShortcuts.tsx");
    expect(src).toContain('window.addEventListener("keydown", onKey, true)');
    expect(src).toContain("filterRef.current?.focus()");
    expect(src).toContain("back.focus()");
    expect(src).toContain("e.target === e.currentTarget");
  });

  it("lays the groups out in two columns over a fixed backdrop", () => {
    const c = cssOf("components/flights/SkyShortcuts.css");
    expect(c).toMatch(/\.sky-keys-backdrop\s*\{[^}]*position:\s*fixed/);
    expect(c).toMatch(/\.sky-keys-cols\s*\{[^}]*columns:\s*2/);
    expect(c).toMatch(/\.sky-keys-card kbd\s*\{/);
  });
});

describe("the sheet is wired (round-consolidate defect 1)", () => {
  it("SkyView hands the sheet the real list, never an empty one", () => {
    const sky = code("components/flights/SkyView.tsx");
    expect(sky).toContain('import { SKY_SHORTCUT_GROUPS } from "../../lib/skyShortcuts";');
    expect(sky).toContain("groups={SKY_SHORTCUT_GROUPS}");
    expect(sky).not.toContain("groups={[]}");
  });

  it("double-click on empty sky is listed as the new-session block, as the guide and the shelf say", () => {
    const rows = SKY_SHORTCUT_GROUPS.flatMap((g) => g.items).filter((i) => i.keys.join("+") === "double-click");
    expect(rows.map((r) => r.what)).toContain("On empty sky: a new session there");
    expect(rows.map((r) => r.what).join(" ")).not.toMatch(/palette/);
  });
});
