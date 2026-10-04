/// <reference types="vite/client" />
// The Code Tree's subagent birds (lib/codetree/subagents.ts + LiveBirds): the
// feed split by agent, naming from the parent's Agent call, the lifecycle
// (work → done → linger → fold into the parent), the colour family, and the
// bird index rows (react-dom/server: the vitest environment has no DOM).
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { CodeMapLive, FeedRecord } from "../api/types";
import { BirdIndex, changeSummary } from "../components/grid/codemap/BirdIndex";
import { LiveBirds } from "../lib/codetree/live";
import { buildModel, type RawRepo } from "../lib/codetree/model";
import { SUB_IDLE_S, SUB_LINGER_S, helperLine, splitFeed, subColour, subName, subShort, subagentsOf } from "../lib/codetree/subagents";
import fixtureRaw from "./fixtures/codetree_repo.json?raw";

const FX = JSON.parse(fixtureRaw) as RawRepo;
const M = buildModel({ name: FX.name, files: FX.files });

const call = (ts: number, id: string, ev: string, desc: string, atype: string): FeedRecord => ({ ts, ev, tool: "Agent", kind: "agent", id, desc, atype });
const inner = (ts: number, agent: string, type: string, rec: Partial<FeedRecord>): FeedRecord =>
  ({ ts, ev: "pre", tool: "Read", kind: "read", id: agent + ts, agent, agent_type: type, ...rec }) as FeedRecord;

describe("subagents from the feed", () => {
  it("records with `agent` are the helper's, never the main agent's", () => {
    const feed = [call(1, "c1", "pre", "d", "Explore"), inner(2, "a1", "Explore", {}), { ts: 3, ev: "pre", tool: "Read", kind: "read", id: "m" }];
    const { main, byAgent } = splitFeed(feed);
    expect(main.map((r) => r.ts)).toEqual([1, 3]);
    expect([...byAgent.keys()]).toEqual(["a1"]);
  });

  it("each helper is named after the parent's most recent unmatched Agent call before it", () => {
    const feed = [
      call(10, "c1", "pre", "Map the config loader", "Explore"),
      call(11, "c2", "pre", "Fix the flaky test", "general-purpose"),
      // parallel helpers: matched by type, then by recency
      inner(12, "a-gp", "general-purpose", { reads: ["x.py"] }),
      inner(13, "a-ex", "Explore", { reads: ["y.py"] }),
      // a helper with no call in the window and no type
      { ...inner(14, "a-lost", "", {}), agent_type: undefined },
    ];
    const subs = subagentsOf(feed, 20);
    const by = new Map(subs.map((s) => [s.id, s]));
    expect(by.get("a-gp")!.name).toBe("general-purpose · Fix the flaky test");
    expect(by.get("a-ex")!.name).toBe("Explore · Map the config loader");
    expect(by.get("a-ex")!.short).toBe("explore#2");
    expect(by.get("a-lost")!.name).toBe("helper 3");
    expect(subs.map((s) => s.index)).toEqual([1, 2, 3]);
  });

  it("names fall back to the type, then 'helper N'", () => {
    expect(subName("Explore", "", 1)).toBe("Explore");
    expect(subName("", "Look around", 2)).toBe("Look around");
    expect(subName("", "", 4)).toBe("helper 4");
    expect(subShort("Plan Agent", 3)).toBe("plan-agent#3");
    expect(subShort("", 1)).toBe("helper#1");
  });

  it("works while its call is open; done at the call's post; listed a while; then folds away", () => {
    const base = [call(100, "c1", "pre", "Read it all", "Explore"), inner(101, "a1", "Explore", { reads: ["a"] }), inner(105, "a1", "Explore", { reads: ["b"] })];
    let [s] = subagentsOf(base, 110);
    expect([s.done, s.listed, s.callId]).toEqual([false, true, "c1"]);
    const ended = base.concat([call(108, "c1", "post", "Read it all", "Explore")]);
    [s] = subagentsOf(ended, 110);
    expect([s.done, s.doneTs, s.listed]).toEqual([true, 108, true]);
    [s] = subagentsOf(ended, 108 + SUB_LINGER_S + 1);
    expect([s.done, s.listed]).toEqual([true, false]);
    // no post, but silent for SUB_IDLE_S: done
    [s] = subagentsOf(base, 105 + SUB_IDLE_S + 1);
    expect(s.done).toBe(true);
    // the session went idle: every helper is done
    [s] = subagentsOf(base, 110, "idle");
    expect(s.done).toBe(true);
    // a background call's post lands BEFORE the helper's last record: still working
    const bg = base.concat([call(102, "c1", "post", "Read it all", "Explore")]);
    [s] = subagentsOf(bg, 110);
    expect(s.done).toBe(false);
  });

  it("indexes stay put when an older helper scrolls out of the feed window", () => {
    const idx = new Map<string, number>();
    const feed = [call(1, "c1", "pre", "one", "Explore"), inner(2, "a1", "Explore", {}), call(3, "c2", "pre", "two", "Explore"), inner(4, "a2", "Explore", {})];
    expect(subagentsOf(feed, 5, "", idx).map((s) => s.index)).toEqual([1, 2]);
    expect(subagentsOf(feed.slice(2), 5, "", idx).map((s) => [s.id, s.index])).toEqual([["a2", 2]]);
    expect(subagentsOf(feed.slice(2).concat([inner(6, "a3", "Explore", {})]), 7, "", idx).find((s) => s.id === "a3")!.index).toBe(3);
  });

  it("helper colours: the parent's family, apart from the parent and from each other", () => {
    const p = "#b39cff";
    const c1 = subColour(p, 1, false),
      c2 = subColour(p, 2, false);
    expect(c1).toMatch(/^#[0-9a-f]{6}$/);
    expect(new Set([p, c1, c2]).size).toBe(3);
    expect(subColour(p, 1, false)).toBe(c1); // deterministic
    const lum = (c: string) => {
      const x = parseInt(c.slice(1), 16);
      return (x >> 16) + ((x >> 8) & 255) + (x & 255);
    };
    // lifted off a dark sky, pressed into a light one
    expect(lum(c1)).toBeGreaterThan(lum(subColour(p, 1, true)));
    expect(subColour("rgb(240, 123, 60)", 1, false)).toMatch(/^#/);
    expect(subColour("var(--accent)", 1, false)).toBe("var(--accent)");
  });

  it("card lines", () => {
    expect(helperLine("editing", "x.py")).toBe("editing x.py");
    expect(helperLine("reading", "y.ts")).toBe("reading y.ts");
    expect(helperLine("blocked", "z.py")).toBe("blocked at z.py");
    expect(helperLine("done", "x.py")).toBe("done");
    expect(helperLine("thinking", "")).toBe("thinking");
  });
});

describe("helper birds (LiveBirds)", () => {
  const f1 = M.files.find((f) => f.path.startsWith("backend/session/"))!;
  const f2 = M.files.find((f) => f.path.startsWith("frontend/src/lib/"))!;
  const f3 = M.files.find((f) => f.path.startsWith("backend/providers/") && f.id !== f1.id)!;
  const live = (now: number, extra: Partial<CodeMapLive> = {}) =>
    ({ now, activity: "working", changed: [{ path: f3.path }], others: [], plan: null, ...extra }) as unknown as CodeMapLive;
  const main: FeedRecord[] = [
    { ts: 100, ev: "pre", tool: "Read", kind: "read", id: "m1", reads: [f1.path] },
    { ts: 101, ev: "post", tool: "Edit", kind: "edit", id: "m2", writes: [f1.path] },
    call(102, "c1", "pre", "Rewrite the web helper", "general-purpose"),
  ];
  const helper: FeedRecord[] = [
    inner(103, "h1", "general-purpose", { reads: [f2.path] }),
    { ts: 104, ev: "post", tool: "Edit", kind: "edit", id: "h-e", agent: "h1", agent_type: "general-purpose", writes: [f3.path] },
  ];
  const inp = (feed: FeedRecord[], now: number, viewNow: number, extra: Partial<CodeMapLive> = {}) => ({
    feed, live: live(now, extra), title: "me", accent: "#a08cff", serverNow: now, viewNow, zones: [],
  });

  it("a helper is its own bird after its parent: smaller marks of its own, the parent's glyph hollow + index", () => {
    const birds = new LiveBirds();
    const [me, h] = birds.update(M, inp(main.concat(helper), 106, 10));
    expect(me.ag.primary).toBe(true);
    expect(h.ag.parent).toBe("me");
    expect(h.ag.glyph).toBe("○1"); // hollow: a solid "●1" after a folder name is a change count
    expect(h.ag.name).toBe("general-purpose · Rewrite the web helper");
    expect(h.ag.short).toBe("general-purpose#1");
    expect(h.ag.color).not.toBe(me.ag.color);
    // the helper's read and edit are its own, not the parent's
    expect([...h.reads.keys()]).toEqual([f2.id]);
    expect([...h.edits.keys()]).toEqual([f3.id]);
    expect(me.reads.has(f2.id)).toBe(false);
    // f3 is on the branch's change list, but a live helper made it: not ALSO the parent's
    expect([...me.edits.keys()]).toEqual([f1.id]);
    expect(h.status).toBe("editing");
    expect(h.nest).toBe(me.nest); // home is the parent's nest
    expect(h.subInfo!.done).toBe(false);
  });

  it("a helper born mid-view flies out from its parent; when done it flies home to the parent's nest", () => {
    const birds = new LiveBirds();
    birds.update(M, inp(main, 102.5, 10));
    const [me, h] = birds.update(M, inp(main.concat(helper), 106, 20));
    expect(h.cur!.t).toBe(20); // flying now, not settled
    expect(h.evs.length).toBe(2);
    expect(h.evs[0].f).toBe(me.cur!.f); // from where the parent was
    const ended = main.concat(helper, [call(107, "c1", "post", "Rewrite the web helper", "general-purpose")]);
    const [, h2] = birds.update(M, inp(ended, 108, 30));
    expect(h2).toBe(h);
    expect(h2.status).toBe("done");
    expect(h2.cur!.type).toBe("nest");
    expect(h2.cur!.nestNode).toBe(me.nest);
    expect(h2.cur!.t).toBe(30);
  });

  it("the first look is settled, and a finished helper drops off: its work folds into the parent's", () => {
    const ended = main.concat(helper, [call(107, "c1", "post", "Rewrite the web helper", "general-purpose")]);
    const birds = new LiveBirds();
    const first = birds.update(M, inp(ended, 110, 10));
    expect(first.length).toBe(2);
    expect(first[1].cur!.t).toBeLessThan(0);
    const later = birds.update(M, inp(ended, 107 + SUB_LINGER_S + 5, 50));
    expect(later.length).toBe(1);
    const [me] = later;
    expect(me.edits.has(f3.id)).toBe(true);
    expect(me.reads.has(f2.id)).toBe(true);
    // folded work never moves the parent's perch
    expect(me.file === null || me.file.id === f1.id).toBe(true);
  });
});

describe("the bird index", () => {
  const f3 = M.files.find((f) => f.path.startsWith("backend/providers/"))!;
  const feed: FeedRecord[] = [
    call(102, "c1", "pre", "Rewrite the web helper", "general-purpose"),
    { ts: 104, ev: "post", tool: "Edit", kind: "edit", id: "h-e", agent: "h1", agent_type: "general-purpose", writes: [f3.path] },
  ];
  const [me, h] = new LiveBirds().update(M, {
    feed, live: { now: 105, activity: "working", changed: [], others: [], plan: null } as unknown as CodeMapLive, title: "me", accent: "#a08cff", serverNow: 105, viewNow: 1, zones: [],
  });
  const nop = () => {};
  const base = {
    M, agents: [me, h], tzones: [], zones: [], zoneBusy: "", affects: () => 0,
    onFollow: nop, onNest: nop, onFile: nop, onFolder: nop, onPaint: nop, onGoZone: nop, onRemoveZone: nop,
  };

  it("helpers nest under the parent's row, each a button that follows its bird", () => {
    const html = renderToStaticMarkup(createElement(BirdIndex, { ...base, size: "wide", followKey: h.ag.key }));
    expect(html).toMatch(/class="ct-left ct-bix"[^>]*data-hud="left"/);
    expect(html).toContain('class="ct-bix-subs"');
    expect(html).toMatch(/class="ct-bix-sub following"[^>]*aria-pressed="true"/);
    expect(html).toContain("general-purpose · Rewrite the web helper");
    expect(html).toMatch(new RegExp("editing [^<]*" + f3.name.replace(".", "\\.")));
    // the parent row is not the followed one
    expect(html).toMatch(/class="ct-bix-follow"[^>]*aria-pressed="false"/);
  });

  it("small panes get a slim LEFT rail — never a strip along the top", () => {
    for (const size of ["narrow", "tiny"] as const) {
      const html = renderToStaticMarkup(createElement(BirdIndex, { ...base, size, followKey: "me" }));
      expect(html).toMatch(/class="ct-left ct-bix-rail[^"]*"[^>]*data-hud="left"/);
      expect(html).toMatch(new RegExp(`class="ct-rail-row sub[^"]*"[^>]*aria-pressed="false"[^>]*data-agent="${h.ag.key}"`));
      expect(html).toMatch(/class="ct-rail-row following"[^>]*aria-pressed="true"/);
      expect(html).toContain("✎1</b> changed");
      expect(html).not.toContain("ct-chipbird");
    }
  });

  it("CHANGES says where the changes are, and ⛔ / ✓ sit on each changed folder", () => {
    const sum = changeSummary(M, [me, h]);
    // the helper's change is this session's
    expect([sum.total, sum.mine, sum.others]).toEqual([1, 1, 0]);
    expect(sum.tops).toEqual([["backend", 1]]);
    expect(sum.folders[0].label).toMatch(/^backend › /);
    expect(sum.folders[0].cols).toEqual([h.ag.color]);
    const html = renderToStaticMarkup(createElement(BirdIndex, { ...base, size: "wide", followKey: null }));
    expect(html).toContain("this session <b>1</b> + other sessions <b>0</b>");
    expect(html).toContain("<b>1</b> in all");
    expect(html).toContain('aria-label="Keep out on ' + sum.folders[0].label + '"');
    expect(html).toContain('aria-label="Only here on ' + sum.folders[0].label + '"');
  });

  it("a helper's row says WHERE it is, on its own line", () => {
    const html = renderToStaticMarkup(createElement(BirdIndex, { ...base, size: "wide", followKey: null }));
    const top = f3.path.split("/").slice(0, 2).join("/");
    expect(html).toMatch(new RegExp('class="wh">in ' + top.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  });

  it("under this session's ✓ only here, another session's folder is 'other worktree · not affected', never 'exempt'", () => {
    const fo = M.files.find((f) => f.node && f.node.depth >= 1 && !f.path.startsWith("backend/providers/") && f.node !== f3.node)!;
    const other = { ...me, ag: { ...me.ag, key: "them", name: "them", glyph: "▲", color: "#f80", primary: false }, edits: new Map([[fo.id, 1]]) };
    const only = { type: "only", node: f3.node, file: null, waived: false, label: "x", z: { id: "g", pattern: "x", scope: "worktree", kind: "green" } } as never;
    const html = renderToStaticMarkup(createElement(BirdIndex, { ...base, agents: [me, h, other], tzones: [only], size: "wide", followKey: null }));
    expect(html).toContain("other worktree · not affected");
    expect(html).not.toContain("exempt from");
    expect(html).toContain("this session <b>1</b> + other sessions <b>1</b>");
  });

  it("the session's card counts its helpers' changes too, so it agrees with CHANGES' 'this session'", () => {
    expect(me.edits.size).toBe(0);
    const html = renderToStaticMarkup(createElement(BirdIndex, { ...base, size: "wide", followKey: null }));
    expect(html).toMatch(/class="ct-bix-chg"[^>]*>.*?1 changed<\/b><span class="hlp"> \(1 by helpers\)<\/span>/);
  });
});
