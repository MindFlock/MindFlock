// The Map's first-open experience: the tour (codemap/MapTour.tsx), the
// calmer bird index (a capped folder list) and the preload guard.
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { MAP_TOUR, MapTour, mapTourSteps, placeCard } from "../components/grid/codemap/MapTour";
import { FOLDERS_SHOWN, birdWords, shownFolders } from "../components/grid/codemap/BirdIndex";
import { activityRows } from "../components/grid/codemap/TreeCards";
import { focusDue, prefetchNeeded } from "../components/grid/CodeMapTab";
import type { Model } from "../lib/codetree/model";
import type { AgentState } from "../lib/codetree/types";
import { src } from "./sourceText";

describe("the Map tour", () => {
  it("walks the tree, the agents, what could break, the rules, plan first and finding things — in that order", () => {
    expect(MAP_TOUR.map((s) => s.title)).toEqual([
      "Your code, drawn as a tree",
      "Bright leaves are what changed",
      "Each agent is a bird",
      "What could break",
      "Rules: keep agents in or out",
      "Plan first",
      "Getting around",
    ]);
    // each pointer names a real part of the tab
    expect(MAP_TOUR.map((s) => s.target)).toEqual(["", "", '[data-hud="left"]', ".ct-bix-risk-tog", ".ct-rules", ".ct-plan-btn", ".cm-search"]);
    const tab = src("components/grid/CodeMapTab.tsx");
    for (const cls of ["ct-plan-btn", "ct-rules", "ct-next"]) expect(tab, cls).toContain(cls);
  });

  it("adapts to what is on screen: nothing changed, hidden targets, and ends on the next step", () => {
    const titles = (s: ReturnType<typeof mapTourSteps>) => s.map((x) => x.title);
    // changes on the tree: every step, then the concrete next step
    const busy = mapTourSteps({ changed: 5, next: { kind: "review-changes", changed: 5, risky: 9 } });
    expect(titles(busy)).toEqual([...MAP_TOUR.map((s) => s.title), "Your next step"]);
    const last = busy[busy.length - 1];
    expect(last.target).toBe(".ct-next");
    expect(renderToStaticMarkup(createElement("p", null, last.body))).toContain("5 changed — start with ⚠ Could break");
    // nothing changed: the changes step reworded, ⚠ Could break left out (its button only exists with changes)
    const calm = mapTourSteps({ changed: 0, next: { kind: "ask-plan" } });
    expect(titles(calm)).toEqual([
      "Your code, drawn as a tree",
      "Nothing has changed yet",
      "Each agent is a bird",
      "Rules: keep agents in or out",
      "Plan first",
      "Getting around",
      "Your next step",
    ]);
    // a target not on screen (the agents column folded to a rail, a narrow toolbar) is skipped, never pointed at
    const narrow = mapTourSteps({ changed: 5, next: { kind: "none" }, visible: (sel) => sel !== ".ct-bix-risk-tog" && sel !== ".cm-search" });
    expect(titles(narrow)).toEqual(["Your code, drawn as a tree", "Bright leaves are what changed", "Each agent is a bird", "Rules: keep agents in or out", "Plan first"]);
    // a CLI that can't declare a plan: no "Plan first"; a hidden next-step line still gets its card, centred
    const noPlan = mapTourSteps({ changed: 1, next: { kind: "breaches", n: 2 }, planSupported: false, visible: (sel) => sel !== ".ct-next" });
    expect(titles(noPlan)).not.toContain("Plan first");
    expect(noPlan[noPlan.length - 1]).toMatchObject({ title: "Your next step", target: "" });
  });

  it("renders as a non-modal card with its step count, Skip and Next", () => {
    const html = renderToStaticMarkup(createElement(MapTour, { rootRef: { current: null }, onClose: () => {} }));
    expect(html).toContain('role="dialog"');
    expect(html).toContain('aria-modal="false"');
    expect(html).toContain("1 of 6");
    expect(html).toContain("Skip tour");
    expect(html).toContain(">Next<");
    expect(html).toContain("Your code, drawn as a tree");
    // no browser dialogs (Electron implements none)
    expect(html).not.toMatch(/prompt\(|confirm\(|alert\(/);
  });

  it("places the card beside a tall left column, below a toolbar control, centred with no target, always inside", () => {
    const W = 1000,
      H = 700,
      cardH = 180;
    // the agents column: to its right
    expect(placeCard({ x: 8, y: 50, w: 240, h: 500 }, W, H, cardH)).toEqual({ left: 260, top: 50 });
    // a toolbar control near the top: below it
    const below = placeCard({ x: 300, y: 8, w: 200, h: 24 }, W, H, cardH);
    expect(below).toEqual({ left: 300, top: 44 });
    // a control at the bottom: above it
    expect(placeCard({ x: 300, y: 650, w: 100, h: 30 }, W, H, cardH).top).toBe(650 - 12 - cardH);
    // a control at the right edge: clamped inside
    expect(placeCard({ x: 950, y: 8, w: 40, h: 24 }, W, H, cardH).left).toBe(W - 320 - 8);
    // nothing to point at: centred
    expect(placeCard(null, W, H, cardH)).toEqual({ left: 340, top: 260 });
    // a tiny pane: still on screen
    const tiny = placeCard(null, 300, 200, 180);
    expect(tiny.left).toBeGreaterThanOrEqual(8);
    expect(tiny.top).toBeGreaterThanOrEqual(8);
  });
});

describe("the bird index lists a few changed folders, not all of them", () => {
  const folders = Array.from({ length: 12 }, (_, i) => ({ id: i, ruled: i === 9 }));
  it("shows the first few plus any folder a rule covers; all on request", () => {
    expect(FOLDERS_SHOWN).toBe(5);
    expect(shownFolders(folders, false, (f) => f.ruled).map((f) => f.id)).toEqual([0, 1, 2, 3, 4, 9]);
    expect(shownFolders(folders, true, (f) => f.ruled)).toHaveLength(12);
    expect(shownFolders(folders.slice(0, 4), false, () => false)).toHaveLength(4);
  });
});

describe("preloading the Map", () => {
  it("does nothing once the snapshot and the laid-out tree are both cached, or while one is in flight", () => {
    expect(prefetchNeeded(false, false, false)).toBe(true);
    expect(prefetchNeeded(true, false, false)).toBe(true);
    expect(prefetchNeeded(true, true, false)).toBe(false);
    expect(prefetchNeeded(false, false, true)).toBe(false);
  });
});

describe("the bird index says what is going on in the sidebar's words", () => {
  it("this session: Idle / Question, never 'done' or 'finished — back in the nest'", () => {
    expect(birdWords("done", true, "idle", "Idle", 0)).toEqual({ chip: "idle", now: "idle — waiting for your next message" });
    expect(birdWords("waiting", true, "clarify", "Question", 0)).toEqual({ chip: "question", now: "needs you · question — answer it in the terminal" });
    expect(birdWords("thinking", true, "working", "Working", 2)).toEqual({ chip: "thinking", now: "in the nest — 2 helpers out working" });
    expect(birdWords("done", true, "offline", "Offline", 0).now).toBe("offline");
  });

  it("another session: idle or waiting by its status, not 'editing on its own branch'", () => {
    expect(birdWords("thinking", false, "working", "Idle", 0)).toEqual({ chip: "idle", now: "idle · on its own branch" });
    expect(birdWords("thinking", false, "working", "Question", 0)).toEqual({ chip: "question", now: "question · on its own branch" });
    expect(birdWords("thinking", false, "working", "Working", 0)).toEqual({ chip: "thinking", now: "in the nest, thinking" });
    // not listed (another machine's session): the bird's own reading
    expect(birdWords("thinking", false, "working", "", 0).chip).toBe("thinking");
    const bix = src("components/grid/codemap/BirdIndex.tsx");
    expect(bix).not.toContain("finished — back in the nest");
    expect(bix).not.toContain("editing on its own branch");
  });
});

describe("the activity box is honest about changes made before the map watched", () => {
  const M = { byPath: new Map(), files: [] } as unknown as Model;
  const prim = (edits: Array<[number, number]>) =>
    ({ ag: { id: 0, key: "s1", name: "s1", color: "#a08cff", glyph: "●", primary: true }, edits: new Map(edits), subInfo: undefined }) as unknown as AgentState;
  it("says 'N files changed before the map started watching' instead of 'Nothing yet'", () => {
    const rows = activityRows(M, [prim([[1, 0], [2, 0], [3, 0]])], [], [], "s1");
    expect(rows.map((r) => r.text)).toEqual(["3 files changed before the map started watching"]);
    expect(rows[0].path).toBe("");
    // edits the feed saw carry their time: no such row
    expect(activityRows(M, [prim([[1, 1700000000]])], [], [], "s1")).toEqual([]);
  });
});

describe("Open map lands where it pointed", () => {
  const f = { title: "s1", path: "a/b.py", seq: 7 };
  it("applies a focus once, for its own tab, and waits for the tree when it flies somewhere", () => {
    expect(focusDue(f, "s1", true, true, 6)).toBe(true);
    expect(focusDue(f, "s1", true, true, 7)).toBe(false); // consumed
    expect(focusDue(f, "s2", true, true, 0)).toBe(false); // another session's Map
    expect(focusDue(f, "s1", false, true, 0)).toBe(false); // tab not showing
    expect(focusDue(f, "s1", true, false, 0)).toBe(false); // tree not laid out yet: wait, don't drop
    expect(focusDue({ title: "s1", section: "risk", seq: 8 }, "s1", true, false, 0)).toBe(false);
    // a Plan & lists section needs no tree
    expect(focusDue({ title: "s1", section: "breaches", seq: 9 }, "s1", true, false, 0)).toBe(true);
    expect(focusDue(null, "s1", true, true, 0)).toBe(false);
  });

  it("every Plan & lists section a focus can name is tagged in the panel", () => {
    const panel = src("components/grid/codemap/SessionPanel.tsx");
    for (const s of ["breaches", "scope", "plan", "blast", "zones", "changed", "others"]) expect(panel, s).toContain(`data-sec="${s}"`);
  });
});

describe("one vocabulary, and no noise", () => {
  it("rules, not zones; ⛔ Keep out / ✓ Only here; Plan & lists; no hop count or layout timing", () => {
    const tab = src("components/grid/CodeMapTab.tsx");
    const panel = src("components/grid/codemap/SessionPanel.tsx");
    const cards = src("components/grid/codemap/TreeCards.tsx");
    expect(tab).toContain('"+ Rule…"');
    expect(tab).not.toContain('"+ Zone"');
    expect(tab).toContain("Plan &amp; lists");
    expect(tab).not.toContain("layout · ");
    expect(tab).not.toContain("quick={[]}");
    expect(panel).not.toContain("hop");
    expect(panel).not.toContain("red zone");
    expect(panel).toContain("Rules <span");
    // the file card shows the rule it carries, like the folder card
    expect(cards).toContain("Remove ⛔ keep out");
    expect(cards).toContain("Remove ✓ only here");
    expect(cards).not.toContain("⛔ Keep agents out");
  });
});
