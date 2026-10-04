/// <reference types="vite/client" />
// The Map tab's decisions (CodeMapTab.tsx + codemap/*): the pure glue behind
// its zone / plan loop (the tree itself is codetree.test.ts), and server-rendered markup of the panels. The vitest
// environment is node (no DOM), so the React parts are checked with
// react-dom/server and the effect logic through the helpers they call.
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { RedZone } from "../api/types";
import {
  classifyPath,
  feedState,
  guardPill,
  liveAllowState,
  scopeRequests,
  sessionsOnWorktree,
  zoneAddTell,
  zoneDoc,
} from "../lib/codemap";
import { searchController } from "../lib/codemapSearch";
import { SessionPanel, type PanelActions, type PanelModel } from "../components/grid/codemap/SessionPanel";
import { ActivityCard } from "../components/grid/codemap/TreeCards";

describe("zone add: tell_agent agrees with the checkbox", () => {
  it("never tells while the agent waits on a prompt (the checkbox shows unchecked + disabled)", () => {
    // newAdd("green", …) starts with tell: true.
    expect(zoneAddTell(true, undefined, true)).toBe(false);
    expect(zoneAddTell(true, undefined, false)).toBe(true);
    expect(zoneAddTell(false, undefined, false)).toBe(false);
    // "ask the agent to revert?" (the override) is itself disabled during clarify.
    expect(zoneAddTell(false, true, false)).toBe(true);
    expect(zoneAddTell(false, true, true)).toBe(false);
  });
});

describe("Allow this file: 'Allowed ✓' is not forever", () => {
  it("drops 'done' once the path is outside the green zones again", () => {
    const zonesWith: RedZone[] = [{ id: "g1", pattern: "/src/a.py", kind: "green" } as RedZone, { id: "g2", pattern: "/lib/**", kind: "green" } as RedZone];
    const zonesWithout = zonesWith.slice(1);
    const outside = (zs: RedZone[]) => {
      const d = zoneDoc(zs, [], [], false);
      return (p: string) => classifyPath(p, d) === "outside";
    };
    const busy = { "src/a.py": "done", "src/b.py": "busy" };
    // Still allowed: unchanged (same object — no re-render churn).
    expect(liveAllowState(busy, outside(zonesWith))).toBe(busy);
    // Its zone removed: the repeat request is allowable again.
    const feed = [{ ts: 1, ev: "pre", deny: { path: "src/a.py", kind: "green" } }] as never;
    expect(scopeRequests(feed, outside(zonesWithout)).map((r) => r.path)).toEqual(["src/a.py"]);
    expect(liveAllowState(busy, outside(zonesWithout))).toEqual({ "src/b.py": "busy" });
  });
});



describe("sessions sharing the worktree", () => {
  it("counts the worktree (folder), not the repo (path)", () => {
    const list = [
      { title: "a", path: "/r", folder: "/wt/a" },
      { title: "a-copy", path: "/r", folder: "/wt/a/" },
      { title: "b", path: "/r", folder: "/wt/b" },
      { title: "c", path: "/r", folder: "/wt/c" },
    ];
    expect(sessionsOnWorktree(list, "a")).toBe(2);
    expect(sessionsOnWorktree(list, "b")).toBe(1);
    expect(sessionsOnWorktree(list, "missing")).toBe(1);
  });
});


describe("Go — only the planned files: the exemption prompt", () => {
  const model = (goExempt: PanelModel["goExempt"]): PanelModel =>
    ({
      live: null,
      mode: "plan",
      green: true,
      greenZones: [],
      zones: [],
      zoneCounts: new Map(),
      breaches: [],
      requests: [],
      recent: [],
      fs: feedState([], 100, ""),
      guard: guardPill(null, 0, "claude", []),
      provider: "claude",
      skew: 0,
      plan: { items: [{ path: "src/a.py", intent: "edit", new: false }], ts: 1, source: "declared" },
      planSupported: true,
      progress: new Map(),
      offPlan: [],
      goZones: [],
      midFlight: true,
      clarify: false,
      busy: "",
      actionMsg: { text: "Sent to the agent.", bad: false },
      goExempt,
      blast: [],
      blastTests: 0,
      depth: 1,
      seeds: 0,
      graphPartial: false,
      changed: [],
      exempt: new Set(),
      others: [],
      zoneBusy: "",
      reqBusy: {},
      levelName: (p: string) => p,
      classify: () => "ok",
    }) as unknown as PanelModel;
  const actions: PanelActions = {
    selectPath: () => {},
    openDiff: () => {},
    askPlan: () => {},
    go: () => {},
    removeZone: () => {},
    waiveZone: () => {},
    previewZone: () => {},
    openAdd: () => {},
    allow: () => {},
    keepGoExempt: () => {},
    goExemptAsBreaches: () => {},
  };
  const render = (g: PanelModel["goExempt"]) => renderToStaticMarkup(createElement(SessionPanel, { m: model(g), a: actions }));

  it("says how many files were exempted and offers Treat as breaches", () => {
    const html = render({ paths: ["docs/x.md", "README.md"], state: "" });
    expect(html).toContain("2 files already changed outside this scope");
    expect(html).toContain("Treat as breaches");
    expect(html).toContain("Keep exempt");
  });

  it("nothing exempted, or kept: no prompt", () => {
    expect(render(null)).not.toContain("Treat as breaches");
    expect(render({ paths: ["a"], state: "kept" })).not.toContain("Treat as breaches");
    expect(render({ paths: ["a"], state: "breaches" })).toContain("treated as breaches");
  });
});

describe("find box: an abandoned query's answer never lands", () => {
  afterEach(() => vi.useRealTimers());

  it("clearing the box while a search is in flight drops its late answer", async () => {
    vi.useFakeTimers();
    let resolve: (v: string[]) => void = () => {};
    const fetch = vi.fn((q: string) => (q === "inst" ? new Promise<string[]>((r) => (resolve = r)) : Promise.resolve([q + "!"])));
    const seen: Array<{ q: string; items: string[] }> = [];
    const ctl = searchController<string>(fetch, (r) => seen.push({ q: r.q, items: r.items }));
    ctl.set("inst");
    await vi.advanceTimersByTimeAsync(200); // debounce fired; request in flight
    expect(fetch).toHaveBeenCalledWith("inst");
    ctl.set(""); // Escape / a pick clears the box
    expect(seen.at(-1)).toEqual({ q: "", items: [] });
    resolve(["instance.py"]);
    await vi.advanceTimersByTimeAsync(0);
    // The stale answer did not refill the list.
    expect(seen.some((s) => s.items.includes("instance.py"))).toBe(false);
    ctl.set("y");
    await vi.advanceTimersByTimeAsync(50);
    expect(seen.at(-1)).toEqual({ q: "", items: [] });
    await vi.advanceTimersByTimeAsync(200);
    expect(seen.at(-1)).toEqual({ q: "y", items: ["y!"] });
  });

  it("dispose drops anything in flight", async () => {
    vi.useFakeTimers();
    const seen: string[][] = [];
    const ctl = searchController<string>(async (q) => [q], (r) => seen.push(r.items));
    ctl.set("abc");
    ctl.dispose();
    await vi.advanceTimersByTimeAsync(500);
    expect(seen).toEqual([]);
  });
});

describe("the activity card in a small pane", () => {
  const rows = [{ key: "a", ts: 0, path: "backend/x.py", text: "edited x.py — 21 depend on it", A: null, bad: false }] as never;
  const props = { rows, skew: 0, onToggle: () => {}, onRow: () => {}, zoneOf: () => null, onZone: () => {} };
  it("folds to a one-line ticker (no header block over the roots); open, it is the full card again", () => {
    const tick = renderToStaticMarkup(createElement(ActivityCard, { ...props, open: false, ticker: true }));
    expect(tick).toContain('class="ct-card ct-activity collapsed ticker"');
    expect(tick).not.toContain(">Activity<");
    expect(tick).toContain("edited x.py");
    const open = renderToStaticMarkup(createElement(ActivityCard, { ...props, open: true, ticker: true }));
    expect(open).toContain('class="ct-card ct-activity"');
    expect(open).toContain("Activity");
    const wide = renderToStaticMarkup(createElement(ActivityCard, { ...props, open: false }));
    expect(wide).toContain('class="ct-card ct-activity collapsed"');
  });
});
