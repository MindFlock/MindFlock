/** Customize → Pane header (owner, 2026-10-14): "Just like how we can
 * customize the view on the left, there should be a customize tab for the top
 * banner. By default I want to see Agent, Terminal, Diff and Queue along with
 * the fast-forward button and the copy-all button. The 3 dots should be on the
 * far right for those extra options. The Customize menu should be 2 pages …
 * use text instead of icons on the top bar up until the point where they
 * don't fit."
 *
 *  - lib/paneHeader: the default set, Agent locked, order + visibility, an id
 *    added after a save, calm folding only an untouched header;
 *  - hidden items stay reachable in ⋯ (lib/sessionMenu), a hidden tab you are
 *    on still shows, ⋯ is the last thing in the row;
 *  - the choice is the synced pref `pane_header` (store → localStorage
 *    `mf_paneheader` → /api/prefs), and Reset clears it;
 *  - words → icons by measurement (fitDecision), with hysteresis. */

import { describe, expect, it, vi } from "vitest";
import { CSS, code } from "./sourceText";
import type { Instance } from "../api/types";

const storage = new Map<string, string>();
vi.stubGlobal("localStorage", {
  getItem: (k: string) => (storage.has(k) ? storage.get(k)! : null),
  setItem: (k: string, v: string) => void storage.set(k, String(v)),
  removeItem: (k: string) => void storage.delete(k),
  clear: () => storage.clear(),
});

const {
  FIT_HYSTERESIS_PX,
  fitDecision,
  headerItems,
  movePaneItem,
  placePaneItem,
  resolvePaneHeader,
  togglePaneItem,
  asPaneHeaderPref,
} = await import("../lib/paneHeader");
const { useUi } = await import("../state/store");
const { prefFieldFor, PREF_DEFAULTS } = await import("../lib/prefs");
const { sessionMenu } = await import("../lib/sessionMenu");
const { actionCtx } = await import("../lib/sessionActionTable");

const ctx = (o: Partial<Parameters<typeof headerItems>[1]> = {}) => ({
  active: "agent",
  sharedFolder: false,
  git: true,
  thread: false,
  calm: false,
  queueBusy: false,
  ftBusy: false,
  ...o,
});

describe("the default set and the rules", () => {
  it("is the owner's list: Agent · Terminal · Diff · Queue · Map, then ⏩ and Copy entire history", () => {
    expect(headerItems(null, ctx())).toEqual({
      tabs: ["agent", "shell", "diff", "queue", "map"],
      buttons: ["fasttrack", "copy"],
    });
    const r = resolvePaneHeader(null);
    for (const off of ["thread", "next", "history", "cost", "diffstat", "hide"] as const)
      expect(r.shown.has(off), off).toBe(false);
  });

  it("Agent can't be switched off", () => {
    const p = togglePaneItem(null, "agent");
    expect(resolvePaneHeader(p).shown.has("agent")).toBe(true);
    expect(headerItems({ order: [], hidden: ["agent"] }, ctx({ active: "diff" })).tabs).toContain("agent");
  });

  it("switches and reorders within each kind", () => {
    let p = togglePaneItem(null, "map"); // Map is on by default: this hides it
    expect(headerItems(p, ctx()).tabs).not.toContain("map");
    p = togglePaneItem(p, "map");
    p = togglePaneItem(p, "history");
    p = togglePaneItem(p, "queue");
    expect(headerItems(p, ctx())).toEqual({
      tabs: ["agent", "shell", "diff", "map"],
      buttons: ["fasttrack", "copy", "history"],
    });
    p = movePaneItem(p, "copy", -1);
    expect(headerItems(p, ctx()).buttons).toEqual(["copy", "fasttrack", "history"]);
    // A tab never moves among the buttons, and the ends don't wrap.
    expect(movePaneItem(p, "agent", -1)).toEqual(p);
    p = placePaneItem(p, "map", "agent");
    expect(headerItems(p, ctx()).tabs).toEqual(["map", "agent", "shell", "diff"]);
    expect(placePaneItem(p, "map", "copy")).toEqual(p);
  });

  it("an item a saved choice never mentioned takes its default (a newer build's item)", () => {
    // Saved before "hide" existed: hide keeps its default (off), at its default place.
    const saved = { order: ["agent", "shell", "diff", "queue", "map", "thread", "fasttrack", "copy", "next", "history", "cost", "diffstat"], hidden: [] };
    const r = resolvePaneHeader(saved);
    expect(r.buttons.at(-1)).toBe("hide");
    expect(r.shown.has("hide")).toBe(false);
    expect(r.shown.has("map")).toBe(true); // mentioned, not hidden
    // Unknown ids from a newer build are ignored, not drawn.
    expect(resolvePaneHeader({ order: ["agent", "zap"], hidden: [] }).tabs).not.toContain("zap");
  });

  it("leaves out what the session can't have, whatever the setting", () => {
    let p = togglePaneItem(null, "map");
    p = togglePaneItem(p, "thread");
    expect(headerItems(p, ctx({ sharedFolder: true })).tabs).not.toContain("shell");
    expect(headerItems(p, ctx({ git: false })).tabs).toEqual(["agent", "shell", "queue"]);
    expect(headerItems(p, ctx({ thread: false })).tabs).not.toContain("thread");
    expect(headerItems(p, ctx({ thread: true })).tabs).toContain("thread");
  });

  it("a hidden tab you are ON still shows while you are on it", () => {
    expect(headerItems(null, ctx({ active: "map" })).tabs).toEqual(["agent", "shell", "diff", "queue", "map"]);
    const noQueue = togglePaneItem(null, "queue");
    expect(headerItems(noQueue, ctx({ active: "queue" })).tabs).toContain("queue");
    expect(headerItems(noQueue, ctx({ active: "agent" })).tabs).not.toContain("queue");
    // …but never a tab the session can't have.
    expect(headerItems(null, ctx({ active: "map", git: false })).tabs).not.toContain("map");
  });

  it("calm folds Queue and ⏩ only from an untouched header, and never their state", () => {
    expect(headerItems(null, ctx({ calm: true }))).toEqual({ tabs: ["agent", "shell", "diff"], buttons: ["copy"] });
    expect(headerItems(null, ctx({ calm: true, queueBusy: true, ftBusy: true }))).toEqual({
      tabs: ["agent", "shell", "diff", "queue"],
      buttons: ["fasttrack", "copy"],
    });
    expect(headerItems(null, ctx({ calm: true, active: "queue" })).tabs).toContain("queue");
    // Customized (even to the same set): calm leaves it alone.
    const custom = togglePaneItem(togglePaneItem(null, "map"), "map");
    expect(headerItems(custom, ctx({ calm: true }))).toEqual(headerItems(null, ctx()));
  });

  it("reads a stored value strictly", () => {
    expect(asPaneHeaderPref(null)).toBeNull();
    expect(asPaneHeaderPref({ order: "x" })).toBeNull();
    expect(asPaneHeaderPref([])).toBeNull();
    expect(asPaneHeaderPref({ order: ["a", 1], hidden: [] })).toEqual({ order: ["a"], hidden: [] });
  });
});

describe("hidden items stay reachable from ⋯", () => {
  const caps = { git: true, tailscale: true, ticketing: true, github: true };
  const inst = {
    title: "s",
    status: "ready",
    branch: "me/b",
    folder: "/w/s",
    diff_stat: { files: 1, additions: 2, deletions: 0, uncommitted: { files: 1, additions: 2, deletions: 0 } },
  } as unknown as Instance;
  const keys = (opts = {}) => {
    const m = sessionMenu(inst, actionCtx("s", { inst, caps }), opts);
    return [...m.groups.flatMap((g) => g.items.map((i) => i.key)), ...m.more.map((i) => i.key)];
  };

  it("tabs the header doesn't draw are in More; the ones it draws are not", () => {
    const shown = keys({ visibleTabs: ["agent", "queue"] });
    for (const k of ["terminal", "diff", "map", "thread"]) expect(shown, k).toContain(k);
    const all = keys({ visibleTabs: ["agent", "shell", "diff", "queue", "map"] });
    for (const k of ["terminal", "diff", "map"]) expect(all, k).not.toContain(k);
    // Queue is Work's "Queue prompt…" (it opens the Queue tab) — always.
    expect(shown).toContain("queue");
  });

  it("History, Copy, Hide and Fast-track… are in the menu", () => {
    const k = keys({ onHistory: () => {}, onCopyHistory: () => {} });
    for (const x of ["history", "copy-history", "hide", "fast-track"]) expect(k, x).toContain(x);
  });

  it("a hidden next-step button is first under Ship, unless Ship already has it by name", () => {
    const run = vi.fn();
    const m = sessionMenu(inst, actionCtx("s", { inst, caps }), { onNextStep: { label: "Re-commit", run } });
    const ship = m.groups.find((g) => g.id === "ship")!;
    expect(ship.items[0].key).toBe("next-step");
    expect(ship.items[0].label).toBe("Re-commit");
    ship.items[0].run();
    expect(run).toHaveBeenCalledOnce();
    // "Commit…" is already Ship's own item: not twice.
    const m2 = sessionMenu(inst, actionCtx("s", { inst, caps }), { onNextStep: { label: "Commit…", run } });
    expect(m2.groups.find((g) => g.id === "ship")!.items.filter((i) => i.label === "Commit…")).toHaveLength(1);
  });

  it("the pane wires every hidden item to its ⋯ (source)", () => {
    const pane = code("components/grid/Pane.tsx");
    expect(pane).toContain('onHistory: shows("history") ? undefined : openHistory,');
    expect(pane).toContain('onNextStep: shows("next") ? undefined : shownStep ?? undefined,');
    expect(pane).toContain('!shows("cost") && usageHeadline(inst),');
    expect(pane).toContain('!shows("diffstat") && hasDiffStat && diffStatText(inst),');
    expect(pane).toContain('<div className="pane-more-summary" role="note">');
  });

  it("⋯ is the last thing in the header, after the person's buttons", () => {
    const pane = code("components/grid/Pane.tsx");
    const head = pane.slice(pane.indexOf('className={"pane-head pane-head-fit"'), pane.indexOf("<AgentBlockedBanner"));
    const buttons = head.indexOf("{items.buttons.map((id) => buttonEls[id])}");
    const tail = head.indexOf('<div className="head-tail">');
    const more = head.indexOf("<PaneMoreMenu");
    expect(buttons).toBeGreaterThan(0);
    expect(buttons).toBeLessThan(tail);
    expect(tail).toBeLessThan(more);
    // Nothing is drawn after ⋯ inside the tail.
    // (Its wrapper is the New tour's ring target, data-tour="pane-menu".)
    expect(head.slice(head.indexOf("</PaneMoreMenu>"))).toMatch(/^<\/PaneMoreMenu>\s*<\/span>\s*<\/div>\s*<\/div>\s*$/);
    expect(head).toContain('<span className="pane-menu-hook" data-tour="pane-menu">');
  });
});

describe("the choice is a synced pref, and Reset clears it", () => {
  it("maps pane_header ↔ mf_paneheader, unset by default", () => {
    expect(prefFieldFor("mf_paneheader")).toBe("pane_header");
    expect(PREF_DEFAULTS.pane_header).toBeNull();
    expect(prefFieldFor("mf_customize_page")).toBeNull(); // the open page is this browser's own
  });

  it("setPaneHeader saves it; null removes it (the server gets null)", () => {
    const p = togglePaneItem(null, "map");
    useUi.getState().setPaneHeader(p);
    expect(JSON.parse(storage.get("mf_paneheader")!)).toEqual(p);
    expect(useUi.getState().paneHeader).toEqual(p);
    useUi.getState().setPaneHeader(null);
    expect(storage.has("mf_paneheader")).toBe(false);
    expect(useUi.getState().paneHeader).toBeNull();
  });

  it("an adopted value from another device lands in the store", () => {
    const sync = code("lib/prefsSync.ts");
    expect(sync).toContain('case "pane_header":');
    expect(sync).toContain("st.paneHeader = asPaneHeaderPref(v);");
  });

  it("Reset to default on the Sidebar page restores the starter bars and order", () => {
    const ui = useUi.getState();
    ui.toggleBarHidden("verify");
    ui.setBarOrder(["prompts", "usage"]);
    ui.setShowFilter(false);
    useUi.getState().resetSidebarBars();
    const s = useUi.getState();
    expect(s.barsExplicit).toBe(false);
    expect(s.barOrder).toEqual([]);
    expect(s.showFilter).toBe(true);
    expect(storage.has("mf_hiddenbars")).toBe(false);
    expect(storage.has("mf_barorder")).toBe(false);
  });

  it("Customize remembers its page", () => {
    useUi.getState().setCustomizePage("pane");
    expect(useUi.getState().customizePage).toBe("pane");
    expect(storage.get("mf_customize_page")).toBe('"pane"');
  });
});

describe("Customize has two pages", () => {
  const dlg = code("components/customize/CustomizeDialog.tsx");
  it("Sidebar and Pane header, each with an intro and a Reset", () => {
    expect(dlg).toContain('className="customize-tabs" role="tablist"');
    expect(dlg).toContain('label: "Sidebar"');
    expect(dlg).toContain('label: "Pane header"');
    expect(dlg).toContain('{cur.id === "pane" ? <PaneHeaderPicker /> : <SidebarBarsPicker />}');
    expect(dlg).toContain('onClick={() => (cur.id === "pane" ? setPaneHeader(null) : resetSidebarBars())}');
    expect(dlg).toContain("Reset to default");
  });

  it("the Pane header page previews the header with the real classes, words and icons", () => {
    const picker = code("components/customize/PaneHeaderPicker.tsx");
    expect(picker).toContain('"pane-head pane-head-fit cz-preview-head" + (icons ? " head-icons" : "")');
    expect(picker).toContain("<PreviewHead tabs={shownTabs} buttons={shownButtons} icons={false} />");
    expect(picker).toContain("<PreviewHead tabs={shownTabs} buttons={shownButtons} icons={true} />");
    expect(picker).toContain("<InfoTip");
    expect(CSS["components/customize/CustomizeDialog.css"]).toMatch(/\.cz-preview \{\s*position: sticky;/);
  });

  it("both pages draw the same row (switch · name · line · ↑ ↓ · drag)", () => {
    for (const f of ["components/customize/SidebarBarsPicker.tsx", "components/customize/PaneHeaderPicker.tsx"])
      expect(code(f), f).toContain("<CustomizeRow");
    const row = code("components/customize/CustomizeRow.tsx");
    expect(row).toContain('role="switch"');
    expect(row).toContain("onClick={() => onMove(-1)}");
    expect(row).toContain("onClick={() => onMove(1)}");
    expect(row).toContain("draggable");
  });
});

describe("words until they don't fit, then icons", () => {
  const words = { icons: false, textNeed: null };

  it("words stay while they fit and go the moment they don't", () => {
    expect(fitDecision(words, 600, 520)).toEqual({ icons: false, textNeed: 520 });
    expect(fitDecision(words, 600, 600)).toEqual({ icons: false, textNeed: 600 });
    expect(fitDecision(words, 600, 601)).toEqual({ icons: true, textNeed: 601 });
  });

  it("words come back only past the hysteresis — no flicker at the edge", () => {
    let s = fitDecision(words, 500, 520); // icons, words needed 520
    expect(s.icons).toBe(true);
    for (const w of [500, 520, 521, 520 + FIT_HYSTERESIS_PX - 1]) {
      s = fitDecision(s, w, 300); // need while drawn as icons doesn't matter
      expect(s.icons, String(w)).toBe(true);
    }
    s = fitDecision(s, 520 + FIT_HYSTERESIS_PX, 300);
    expect(s).toEqual({ icons: false, textNeed: 520 });
  });

  it("an unknown need (the header's contents changed) tries the words once", () => {
    expect(fitDecision({ icons: true, textNeed: null }, 400, 300)).toEqual({ icons: false, textNeed: null });
    // …and the next measure (with words) decides, remembering what it saw.
    expect(fitDecision({ icons: false, textNeed: null }, 400, 480)).toEqual({ icons: true, textNeed: 480 });
  });

  it("a header with no width (hidden) changes nothing", () => {
    const s = { icons: true, textNeed: 500 };
    expect(fitDecision(s, 0, 100)).toBe(s);
  });

  it("the pane measures before paint, on resize, and when its contents change", () => {
    const pane = code("components/grid/Pane.tsx");
    expect(pane).toContain("useLayoutEffect(measureFit);");
    expect(pane).toContain("const ro = new ResizeObserver(() => measureFit());");
    expect(pane).toContain("data-fit-key={fitKeyNow}");
    expect(pane).toContain('className={"pane-head pane-head-fit" + (icons ? " head-icons" : "")}');
    expect(pane).toContain('{icons ? "▶" : "▶ Resume"}');
  });
});
