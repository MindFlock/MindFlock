/// <reference types="vite/client" />
// The Code Tree's pure parts (lib/codetree/*): the layout (determinism, the
// prototype's result, zero overlaps — a port of the prototype review's
// measure.js), warm starts and replays, test→target matching, blast badges,
// nests, birds from the live feed, and zones on the tree.
import { describe, expect, it } from "vitest";
import type { CodeMapLive, FeedRecord, RedZone } from "../api/types";
import { addBadgeSets, agentBlast, collectBadges } from "../lib/codetree/blast";
import { blockedTxt, buildFoliageField, folderPath, foreignLit, onlyBinds, riskGroups, spreadApart, tailPath } from "../lib/codetree/draw";
import { rawFromSnapshot, testStem, testTargets, kindOf, linesOf } from "../lib/codetree/input";
import { S0 } from "../lib/codetree/layout";
import { LiveBirds, deepestMajority, nestFor, touchesOf } from "../lib/codetree/live";
import { buildModel, dataSig, type Model, type RawRepo } from "../lib/codetree/model";
import { birdColours } from "../lib/codetree/palette";
import { patternFor, treeZones, zoneOfFile } from "../lib/codetree/zones";
import type { AgentState } from "../lib/codetree/types";
import fixtureRaw from "./fixtures/codetree_repo.json?raw";

const FX = JSON.parse(fixtureRaw) as RawRepo & { golden: Record<string, [number, number]> };
const RAW: RawRepo = { name: FX.name, files: FX.files };
const clone = (r: RawRepo): RawRepo => JSON.parse(JSON.stringify(r));

// one cold build shared by most tests (the search is the slow part)
const COLD = buildModel(RAW);
const QUANT = buildModel(RAW, { quantise: true });

// --- the overlap harness (mindflock-prototypes/code-tree-review/round1-fix/measure.js) ---
function segDist(px: number, py: number, ax: number, ay: number, bx: number, by: number) {
  const dx = bx - ax,
    dy = by - ay;
  const L2 = dx * dx + dy * dy;
  let t = L2 ? ((px - ax) * dx + (py - ay) * dy) / L2 : 0;
  t = Math.max(0, Math.min(1, t));
  return Math.hypot(px - (ax + dx * t), py - (ay + dy * t));
}
function segsCross(a: number[], b: number[], c: number[], d: number[]) {
  const o = (p: number[], q: number[], r: number[]) => Math.sign((q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0]));
  const o1 = o(a, b, c),
    o2 = o(a, b, d),
    o3 = o(c, d, a),
    o4 = o(c, d, b);
  return o1 !== o2 && o3 !== o4 && o1 !== 0 && o2 !== 0 && o3 !== 0 && o4 !== 0;
}
function measure(M: Model) {
  const leaves = M.leaves.filter((l) => l.region !== "ground");
  let leafBranch = 0,
    leafTrunk = 0;
  const T = M.trunkW,
    G = M.groundY;
  for (const l of leaves) {
    const rl = l.len * 0.42;
    const seen = new Set<unknown>();
    let hit = false;
    for (const it of M.grid.near(l.x, l.y, rl + S0 * 3)) {
      if (!it.br || seen.has(it.br)) continue;
      seen.add(it.br);
      const b = it.br;
      if (b.term && b.term === l.term) continue; // own twig
      if (l.x < b.bx0 - rl || l.x > b.bx1 + rl || l.y < b.by0 - rl || l.y > b.by1 + rl) continue;
      const P = b.pts,
        n = P.length;
      for (let i = 0; i + 1 < n; i++) {
        const t = (i + 0.5) / (n - 1);
        const hw = (b.wb + (b.we - b.wb) * Math.pow(t, 0.9)) / 2;
        if (segDist(l.x, l.y, P[i][0], P[i][1], P[i + 1][0], P[i + 1][1]) < hw + rl) {
          hit = true;
          break;
        }
      }
      if (hit) break;
    }
    if (hit) leafBranch++;
    if (l.region === "crown" && l.y > -S0 * 0.5 && l.y < G) {
      const k = Math.max(0, Math.min(1, l.y / Math.max(1, G)));
      if (Math.abs(l.x) < T * (0.5 + 0.75 * k) + rl) leafTrunk++;
    }
  }
  let leafLeafX = 0,
    blobX = 0;
  for (const l of leaves)
    for (const it of M.grid.near(l.x, l.y, S0 * 2.5)) {
      const o = it.leaf;
      if (!o || o === l || o.id < l.id || o.region === "ground" || o.term === l.term) continue;
      const d = Math.hypot(o.x - l.x, o.y - l.y);
      if (d < (0.75 * (l.len + o.len)) / 2) leafLeafX++;
      if (d < 0.66 * (l.term!.s + o.term!.s)) blobX++;
    }
  let branchX = 0;
  const B = M.branches;
  const related = (a: (typeof B)[number], b: (typeof B)[number]) =>
    a === b || (a.x0 === b.x0 && a.y0 === b.y0) || (a.x1 === b.x0 && a.y1 === b.y0) || (b.x1 === a.x0 && b.y1 === a.y0);
  for (let i = 0; i < B.length; i++)
    for (let j = i + 1; j < B.length; j++) {
      const a = B[i],
        b = B[j];
      if (related(a, b) || a.bx1 < b.bx0 || b.bx1 < a.bx0 || a.by1 < b.by0 || b.by1 < a.by0) continue;
      let x = false;
      for (let p = 0; p + 1 < a.pts.length && !x; p++)
        for (let q = 0; q + 1 < b.pts.length; q++)
          if (segsCross(a.pts[p], a.pts[p + 1], b.pts[q], b.pts[q + 1])) {
            x = true;
            break;
          }
      if (x) branchX++;
    }
  return { leafBranch, leafTrunk, leafLeafX, blobX, branchX };
}
const ZERO = { leafBranch: 0, leafTrunk: 0, leafLeafX: 0, blobX: 0, branchX: 0 };
const leafPos = (M: Model) => new Map(M.leaves.map((l) => [l.file.path, [l.x, l.y] as [number, number]]));
const maxMove = (A: Model, B: Model) => {
  const a = leafPos(A);
  let mx = 0,
    moved = 0;
  for (const l of B.leaves) {
    const o = a.get(l.file.path);
    if (!o) continue;
    const d = Math.hypot(o[0] - l.x, o[1] - l.y);
    mx = Math.max(mx, d);
    if (d > 1) moved++;
  }
  return { mx, moved };
};

/** A deterministic synthetic repo: nested packages, docs, tests. */
function synthRepo(n: number, seed = 7): RawRepo {
  let s = seed;
  const r = () => ((s = (s * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff);
  const files: RawRepo["files"] = [];
  const dirs = ["src/core", "src/core/io", "src/api", "src/api/routes", "src/ui", "src/ui/widgets", "src/ui/widgets/forms", "lib", "lib/util", "pkg/a", "pkg/b/c"];
  for (let i = 0; i < n; i++) {
    const d = dirs[Math.floor(r() * dirs.length)];
    files.push({ p: `${d}/m${i}.ts`, n: 20 + Math.floor(r() * 900), k: "c" });
  }
  for (let i = 0; i < n / 10; i++) files.push({ p: `docs/page${i}.md`, n: 50, k: "d" });
  for (let i = 0; i < n / 6; i++) {
    const tgt = files[Math.floor(r() * n)];
    files.push({ p: `tests/test_${tgt.p.split("/").pop()!.replace(".ts", "")}.py`, n: 80, k: "c", t: true, g: tgt.p.slice(0, tgt.p.lastIndexOf("/")) });
  }
  files.push({ p: "README.md", n: 100, k: "d" }, { p: "setup.py", n: 30, k: "c" });
  for (let i = 0; i < n; i++) if (r() < 0.5) files[i].i = [Math.floor(r() * n)].filter((j) => j !== i);
  return { name: "synth", files };
}

describe("layout", () => {
  it("is the approved prototype's layout (same data → the same leaf positions)", () => {
    let mx = 0,
      n = 0;
    for (const l of COLD.leaves) {
      const g = FX.golden[l.file.path];
      expect(g, l.file.path).toBeDefined();
      mx = Math.max(mx, Math.hypot(g[0] - l.x, g[1] - l.y));
      n++;
    }
    expect(n).toBe(FX.files.length);
    expect(mx).toBeLessThan(1e-4);
  });

  it("is deterministic", () => {
    const again = buildModel(clone(RAW));
    expect(maxMove(COLD, again).mx).toBe(0);
    expect(again.branches.length).toBe(COLD.branches.length);
  });

  it("has zero overlaps (leaf–branch, leaf–trunk, leaf–leaf, foliage, branch crossings)", () => {
    expect(measure(COLD)).toEqual(ZERO);
    expect(measure(QUANT)).toEqual(ZERO);
    expect(measure(buildModel(synthRepo(420), { quantise: true }))).toEqual(ZERO);
  });

  it("puts tests underground under the code they test, docs on the ground", () => {
    const t = COLD.files.find((f) => f.test && f.kind === "c")!;
    expect(t.place).toBe("root");
    expect(t.leaf!.y).toBeGreaterThan(COLD.groundY);
    const doc = COLD.files.find((f) => f.path.startsWith("docs/"))!;
    expect(doc.place).toBe("ground");
    const code = COLD.files.find((f) => f.path.startsWith("backend/session/"))!;
    expect(code.leaf!.y).toBeLessThan(COLD.groundY);
    // roots are labelled by what they test
    expect(COLD.roots.kids.some((k) => /^tests for /.test(k.label || ""))).toBe(true);
  });

  it("replays a record exactly, and fast", () => {
    const rec = JSON.parse(JSON.stringify(QUANT.layoutRec));
    const t0 = performance.now();
    const R = buildModel(RAW, { quantise: true, replay: rec });
    expect(performance.now() - t0).toBeLessThan(500);
    expect(R.replayMismatch).toBeFalsy();
    expect(maxMove(QUANT, R).mx).toBe(0);
  });

  it("a record for other data is not replayed", () => {
    const other = clone(RAW);
    other.files.push({ p: "backend/session/zz_new.py", n: 40, k: "c" });
    const R = buildModel(other, { quantise: true, replay: QUANT.layoutRec });
    expect(R.buildStats).toMatchObject({ replay: false });
    expect(measure(R)).toEqual(ZERO);
  });

  it("warm starts keep the tree where it stood after small changes", () => {
    const rec = QUANT.layoutRec;
    // a size change: only leaves of that file's own clump may trade places
    // (a clump puts its biggest files in the middle); no other leaf moves
    const a = clone(RAW);
    a.files[5].n = Math.round(a.files[5].n * 3) + 200;
    const Wa = buildModel(a, { quantise: true, warm: rec });
    const own = Wa.byPath.get(a.files[5].p)!.node!.path;
    const pos = leafPos(QUANT);
    for (const l of Wa.leaves) {
      const o = pos.get(l.file.path)!;
      if (Math.hypot(o[0] - l.x, o[1] - l.y) > 1e-9) expect(l.file.node!.path).toBe(own);
    }
    // a new file in an existing folder: only that folder's clump re-forms
    const b = clone(RAW);
    b.files.push({ p: "backend/session/zz_new.py", n: 120, k: "c" });
    const Wb = buildModel(b, { quantise: true, warm: rec });
    const mb = maxMove(QUANT, Wb);
    expect(mb.moved).toBeLessThan(RAW.files.length * 0.2);
    expect(measure(Wb)).toEqual(ZERO);
    // a deleted file (imports dropped so indices stay meaningful)
    const c = clone(RAW);
    c.files = c.files.filter((f) => f.p !== "backend/session/instance.py").map((f) => ({ ...f, i: [] }));
    const Wc = buildModel(c, { quantise: true, warm: rec });
    expect(maxMove(QUANT, Wc).moved).toBeLessThan(RAW.files.length * 0.3);
    expect(measure(Wc)).toEqual(ZERO);
    // and what a warm build chose replays exactly
    const R = buildModel(b, { quantise: true, replay: JSON.parse(JSON.stringify(Wb.layoutRec)) });
    expect(maxMove(Wb, R).mx).toBe(0);
  });

  it("the data signature moves with the data", () => {
    const a = clone(RAW);
    expect(dataSig(a)).toBe(dataSig(RAW));
    a.files[0].n += 1;
    expect(dataSig(a)).not.toBe(dataSig(RAW));
  });

  it("an empty or one-file repo still builds", () => {
    expect(() => buildModel({ name: "x", files: [] })).not.toThrow();
    const one = buildModel({ name: "x", files: [{ p: "main.py", n: 10, k: "c" }] });
    expect(one.leaves.length).toBe(1);
  });
});

describe("snapshot → tree input", () => {
  it("kinds, a line proxy from bytes, imports from edges, tests from flags", () => {
    expect([kindOf("a/b.py"), kindOf("README.md"), kindOf("logo.png"), kindOf("Makefile")]).toEqual(["c", "d", "a", "d"]);
    expect(linesOf(3200, "c")).toBe(100);
    expect(linesOf(10, "c")).toBe(1);
    expect(linesOf(5000, "a")).toBe(0);
    const raw = rawFromSnapshot(
      {
        files: [
          ["src/app.py", 3200, 0],
          ["src/util.py", 640, 0],
          ["tests/test_app.py", 960, 2],
          ["docs/x.md", 100, 0],
        ],
        edges: [
          [0, 1],
          [2, 0],
          [2, 2],
        ],
      },
      "repo"
    );
    expect(raw.files.map((f) => [f.p, f.n, f.k, !!f.t, f.i || [], f.g ?? null])).toEqual([
      ["src/app.py", 100, "c", false, [1], null],
      ["src/util.py", 20, "c", false, [], null],
      ["tests/test_app.py", 30, "c", true, [0], "src"],
      ["docs/x.md", 3, "d", false, [], null],
    ]);
  });

  it("test → target: a name match (preferring an imported one), else the deepest folder of most imports", () => {
    expect([testStem("test_foo.py"), testStem("foo_test.go"), testStem("Foo.test.ts"), testStem("FooTest.java"), testStem("BarIT.java")]).toEqual([
      "foo",
      "foo",
      "foo",
      "foo",
      "bar",
    ]);
    const paths = ["a/x/foo.py", "b/foo.py", "a/y/z/m.py", "a/y/z/n.py", "a/y/q.py", "tests/test_foo.py", "tests/test_other.py", "tests/test_none.py"];
    const kinds = paths.map(() => "c" as const);
    const tests = new Set([5, 6, 7]);
    const imports = [[], [], [], [], [], [1], [2, 3, 4], []];
    const t = testTargets(paths, kinds, tests, imports);
    expect(t.get(5)).toBe("b"); // both stems match: the one it imports wins
    expect(t.get(6)).toBe("a/y/z"); // 2 of its 3 imports sit in a/y/z: the deepest folder with at least half
    expect(t.get(7)).toBeNull();
  });
});

function agent(M: Model, over: Partial<AgentState> = {}): AgentState {
  return {
    ag: { id: 0, key: "s1", name: "s1", color: "#a08cff", glyph: "●", primary: true },
    reads: new Map(), edits: new Map(), plan: new Set(), planNew: [], created: new Set(), blocked: [], nest: null, nestSince: 0, evs: [],
    cur: null, lastEdit: null, status: "editing", file: null, done: false, activity: "working", task: "", ...over,
  } as AgentState;
}

describe("could break, on the map", () => {
  const hubs = COLD.files.filter((f) => f.usedBy.length).sort((a, b) => b.usedBy.length - a.usedBy.length);
  const view = (over: Record<string, unknown>) =>
    ({ M: COLD, cam: { x: 0, y: 0, z: 0.5 }, blastCache: new Map(), agents: [], riskAll: false, riskFile: null, ...over }) as never as Parameters<typeof riskGroups>[0];
  it("off by default: nothing drawn until asked", () => {
    const A = agent(COLD, { edits: new Map([[hubs[0].id, 1]]) });
    expect(riskGroups(view({ agents: [A] }))).toEqual([]);
  });
  it("counts every dependent once, per folder, never a count inside a count — at every zoom", () => {
    const A = agent(COLD, { edits: new Map(hubs.slice(0, 3).map((f, i) => [f.id, i + 1])) });
    const all = new Set<number>();
    for (const f of hubs.slice(0, 3)) for (const id of f.usedBy) if (COLD.files[id].leaf) all.add(id);
    for (const z of [0.3, 0.6, 1.3, 3]) {
      const gs = riskGroups(view({ agents: [A], riskAll: true, cam: { x: 0, y: 0, z } }));
      expect(gs.length).toBeGreaterThan(0);
      expect(gs.reduce((n, e) => n + e.ids.size, 0)).toBe(all.size);
      for (const e of gs) for (const o of gs) if (e !== o && !e.rest) expect(e.n !== o.n && o.n.parent === e.n && !o.rest).toBe(false);
    }
  });
  it("a focused file shows ITS dependents only", () => {
    const A = agent(COLD, { edits: new Map(hubs.slice(0, 3).map((f, i) => [f.id, i + 1])) });
    const f = hubs[1];
    const gs = riskGroups(view({ agents: [A], riskFile: f.id }));
    const want = new Set(f.usedBy.filter((id) => id !== f.id && COLD.files[id].leaf));
    expect(gs.reduce((n, e) => n + e.ids.size, 0)).toBe(want.size);
    for (const e of gs) expect([...e.files]).toEqual([f.id]);
  });
});

describe("rule wording", () => {
  it("one count wording everywhere, and a rule's short name is never a bare 'core'", () => {
    expect(blockedTxt(1)).toBe("1 edit blocked");
    expect(blockedTxt(3)).toBe("3 edits blocked");
    expect(tailPath("/backend/providers/**")).toBe("providers");
    expect(tailPath("backend/web/core/")).toBe("web/core");
    expect(tailPath("README.md")).toBe("README.md");
  });
  it("only-here binds this session and its helpers, not another session's worktree", () => {
    const me = agent(COLD);
    const helper = agent(COLD, { ag: { id: 1, key: "s1::h", name: "h", color: "#fff", glyph: "○1", primary: false, parent: "s1", sub: 1 } });
    const other = agent(COLD, { ag: { id: 2, key: "s2", name: "s2", color: "#f80", glyph: "▲", primary: false } });
    const zone = (scope: string) => ({ type: "only", node: null, file: 0, waived: false, label: "x", z: { id: "z", pattern: "x", scope, kind: "green" } }) as never;
    const v = { agents: [me, helper, other], zones: [zone("worktree")], hasOnly: true };
    expect([onlyBinds(v, me), onlyBinds(v, helper), onlyBinds(v, other)]).toEqual([true, true, false]);
    expect(onlyBinds({ ...v, zones: [zone("repo")] }, other)).toBe(true);
    expect(onlyBinds({ ...v, hasOnly: false }, me)).toBe(false);
  });
});

describe("blast radius badges", () => {
  // the most imported files: real gold
  const hubs = COLD.files.filter((f) => f.usedBy.length).sort((a, b) => b.usedBy.length - a.usedBy.length);
  it("badges always sum to the bird's 'affects N', at every zoom", () => {
    const A = agent(COLD, { edits: new Map(hubs.slice(0, 3).map((f, i) => [f.id, i + 1])) });
    const cache = new Map<number, { h1: Set<number> }>();
    const { ids, by } = agentBlast(COLD, A, cache);
    expect(ids.size).toBeGreaterThan(0);
    for (const z of [0.3, 0.6, 1.3, 3, 6]) {
      const v = { M: COLD, cam: { x: 0, y: 0, z }, badgeSets: new Map(), badges: [] } as never as Parameters<typeof collectBadges>[0];
      addBadgeSets(v, ids, by, { ag: A.ag, hover: false, fid: null }, 1);
      const badges = collectBadges(v);
      expect(badges.reduce((s, b) => s + b.n, 0)).toBe(ids.size);
      expect(badges.length).toBeLessThanOrEqual(12);
      for (const b of badges) expect(b.text).toMatch(/depends? on|tests? depends? on/);
    }
  });
  it("a pinned file's blast is its direct importers, deduped against a bird's identical badge", () => {
    const f = hubs[0];
    const A = agent(COLD, { edits: new Map([[f.id, 1]]) });
    const { ids, by } = agentBlast(COLD, A, new Map());
    expect([...ids].sort()).toEqual([...new Set(f.usedBy)].filter((x) => x !== f.id).sort());
    const v = { M: COLD, cam: { x: 0, y: 0, z: 3 }, badgeSets: new Map(), badges: [] } as never as Parameters<typeof collectBadges>[0];
    addBadgeSets(v, ids, by, { ag: A.ag, hover: false, fid: null }, 1);
    addBadgeSets(v, ids, null, { ag: null, hover: false, fid: f.id }, 1);
    const badges = collectBadges(v);
    expect(badges.filter((b) => !b.ag).length).toBe(0);
  });
});

describe("nests", () => {
  it("the deepest folder holding most of the recent edits", () => {
    const sess = COLD.files.filter((f) => f.path.startsWith("backend/session/") && !f.path.slice(16).includes("/"));
    const other = COLD.files.find((f) => f.path.startsWith("frontend/src/lib/"))!;
    const n = deepestMajority(COLD, [sess[0].id, sess[1].id, other.id]);
    expect(n.path).toBe(sess[0].node!.path);
    expect(deepestMajority(COLD, []).depth).toBe(0);
    // recent edits first, then the plan, then reads
    const A = agent(COLD, { plan: new Set([other.id]), reads: new Map([[sess[0].id, 1]]) });
    expect(nestFor(COLD, A).path).toBe(other.node!.path);
    A.edits = new Map([[sess[2].id, 5]]);
    expect(nestFor(COLD, A)).toBe(sess[2].node);
  });
  it("a test's nest is the code it tests", () => {
    const t = COLD.files.find((f) => f.test && f.tests && f.tests.depth >= 1)!;
    expect(deepestMajority(COLD, [t.id]).path.startsWith(t.tests!.path)).toBe(true);
  });
});

describe("birds from the live poll", () => {
  const f1 = COLD.files.find((f) => f.path.startsWith("backend/session/"))!;
  const f2 = COLD.files.find((f) => f.path.startsWith("frontend/src/lib/"))!;
  const feed: FeedRecord[] = [
    { ts: 100, ev: "pre", tool: "Read", kind: "read", id: "1", reads: [f2.path] },
    { ts: 101, ev: "post", tool: "Read", kind: "read", id: "1", reads: [f2.path] },
    { ts: 110, ev: "pre", tool: "Edit", kind: "edit", id: "2", writes: [f1.path] },
    { ts: 111, ev: "post", tool: "Edit", kind: "edit", id: "2", writes: [f1.path] },
    { ts: 120, ev: "pre", tool: "Edit", kind: "edit", id: "3", writes: [f2.path], deny: { path: f2.path, pattern: "/frontend/**", zone_id: "z1" } },
  ];
  const live = {
    now: 125,
    activity: "working",
    changed: [],
    others: [{ session: "other-one", path: f2.path, ts: 118 }],
    plan: { source: "declared", ts: 90, items: [{ path: f2.path, intent: "x", new: false }, { path: "backend/session/brand_new.py", intent: "y", new: true }] },
  } as unknown as CodeMapLive;

  it("touches: reads at pre, edits at post, refusals terminal", () => {
    expect(touchesOf(feed).map((t) => [t.type, t.ts])).toEqual([
      ["read", 100],
      ["edit", 111],
      ["blocked", 120],
    ]);
  });

  it("this session + others on the repo, each with reads / edits / plan / blocks / nest", () => {
    const zones = treeZones(COLD, [{ id: "z1", pattern: "/frontend/**", name: "", re: "^frontend/", kind: "red" } as RedZone]);
    const birds = new LiveBirds();
    const [me, other] = birds.update(COLD, { feed, live, title: "me", accent: "#a08cff", serverNow: 125, viewNow: 10, zones });
    expect(me.ag.primary && !other.ag.primary).toBe(true);
    expect(other.ag.name).toBe("other-one");
    expect(me.ag.color).not.toBe(other.ag.color);
    expect([...me.reads.keys()]).toEqual([f2.id]);
    expect([...me.edits.keys()]).toEqual([f1.id]);
    expect(me.blocked.map((b) => [b.kind, b.f && b.f.path, !!b.z])).toEqual([["keep", f2.path, true]]);
    expect(me.status).toBe("blocked");
    expect(me.plan.has(f2.id)).toBe(true);
    expect(me.planNew.map((p) => [p.path, p.node.path])).toEqual([["backend/session/brand_new.py", f1.node!.path]]);
    expect(me.nest).toBe(f1.node);
    expect([...other.edits.keys()]).toEqual([f2.id]);
    // the first look is settled (no flight); a new touch is a flight from now
    expect(me.cur!.t).toBeLessThan(0);
    const later = feed.concat([{ ts: 124, ev: "pre", tool: "Read", kind: "read", id: "4", reads: [f1.path] }]);
    const [me2] = birds.update(COLD, { feed: later, live, title: "me", accent: "#a08cff", serverNow: 125, viewNow: 20, zones });
    expect(me2).toBe(me); // the same bird object across polls
    expect(me2.cur!.t).toBe(20);
    expect(me2.evs.length).toBe(2);
    expect(me2.status).toBe("reading");
  });

  it("an idle session's bird is back in its nest", () => {
    const [me] = new LiveBirds().update(COLD, { feed, live: { ...live, activity: "idle" }, title: "me", accent: "#a08cff", serverNow: 125, viewNow: 1, zones: [] });
    expect(me.status).toBe("done");
    expect(me.file).toBeNull();
  });

  it("bird colours: the accent first, then secondaries that keep apart from it", () => {
    const c = birdColours("#f07b3c", 3);
    expect(c[0]).toBe("#f07b3c");
    expect(c.slice(1)).not.toContain("#ffa24d"); // orange next to an orange accent: skipped
  });
});

describe("zones on the tree", () => {
  const z = (id: string, re: string, kind = "red", extra: Partial<RedZone> = {}): RedZone => ({ id, pattern: id, name: "", re, kind, ...extra });
  it("a zone covering a whole folder is a band on that folder; stray matches are single files", () => {
    const tz = treeZones(COLD, [z("a", "^backend/providers/"), z("b", "^backend/session/instance\\.py$")]);
    const prov = COLD.nodeOf.get("backend/providers")!;
    expect(tz.filter((t) => t.z.id === "a").map((t) => t.node)).toEqual([prov]);
    const inst = COLD.byPath.get("backend/session/instance.py")!;
    expect(tz.filter((t) => t.z.id === "b").map((t) => t.file)).toEqual([inst.id]);
    expect(zoneOfFile(COLD.files.find((f) => f.node && f.node.path.startsWith("backend/providers"))!, tz)?.type).toBe("keep");
    expect(zoneOfFile(inst, tz)?.type).toBe("keep");
  });
  it("only here: everything outside is blocked; a waived zone blocks nothing", () => {
    const tz = treeZones(COLD, [z("g", "^backend/session/", "green")]);
    const inside = COLD.files.find((f) => f.path.startsWith("backend/session/"))!;
    const outside = COLD.files.find((f) => f.path.startsWith("frontend/"))!;
    expect(zoneOfFile(inside, tz)).toBeNull();
    expect(zoneOfFile(outside, tz)?.type).toBe("only");
    const waived = treeZones(COLD, [z("r", "^frontend/", "red", { waived: true })]);
    expect(waived.every((t) => t.waived)).toBe(true);
    expect(zoneOfFile(outside, waived)).toBeNull();
  });
  it("painting: a folder is its path, a file is its literal path; the trunk and mixed roots refuse", () => {
    const prov = COLD.nodeOf.get("backend/providers")!;
    expect(patternFor(COLD, { node: prov })).toEqual({ pattern: "backend/providers", label: "backend/providers" });
    const readme = COLD.byPath.get("README.md")!;
    expect(patternFor(COLD, { file: readme })).toMatchObject({ pattern: "/README.md" });
    expect("error" in patternFor(COLD, { node: COLD.crown })).toBe(true);
    for (const r of COLD.roots.kids) {
      const p = patternFor(COLD, { node: r });
      expect("pattern" in p || "error" in p).toBe(true);
    }
  });
});

describe("folder names on the map", () => {
  it("a nested folder carries its top-level folder, so it never reads as a neighbouring limb's", () => {
    const prov = COLD.nodeOf.get("backend/providers")!;
    expect(folderPath(prov)).toBe("backend › providers");
    const top = COLD.nodeOf.get("backend")!;
    expect(folderPath(top)).toBe("backend");
    // deeper folders still name their top-level folder first
    const deep = [...COLD.nodeOf.values()].find((n) => n.depth >= 3 && n.kind !== "root" && n.kind !== "pile");
    if (deep) expect(folderPath(deep).startsWith(deep.path.split("/")[0] + " › ")).toBe(true);
  });
});

describe("readability: changes, sides and territories", () => {
  it("changed leaves seen from far out are nudged apart into separate, countable marks", () => {
    const pts = [
      { x: 100, y: 100 },
      { x: 100, y: 100 },
      { x: 103, y: 101 },
      { x: 98, y: 104 },
      { x: 400, y: 400 },
    ];
    spreadApart(pts, 15);
    for (let i = 0; i < 4; i++) for (let j = i + 1; j < 4; j++) expect(Math.hypot(pts[i].x - pts[j].x, pts[i].y - pts[j].y)).toBeGreaterThanOrEqual(14.9);
    // a change far from the others never moves; the cluster stays where its files are (a few px, not a reshuffle)
    expect(pts[4]).toEqual({ x: 400, y: 400 });
    for (const q of pts.slice(0, 4)) expect(Math.hypot(q.x - 100, q.y - 101)).toBeLessThan(20);
  });

  it("a folder's name may only sit where the nearest foliage is its own limb's", () => {
    const W = 6000,
      H = 6000;
    const v = { M: COLD, cam: { x: 0, y: 0, z: 1 }, W, H, tool: "explore", sel: null, infoNode: null } as never as Parameters<typeof buildFoliageField>[0];
    const toS = (x: number, y: number): [number, number] => [x + W / 2, y + H / 2];
    const fol = buildFoliageField(v, toS, null);
    const topOf = (n: (typeof COLD.nodes)[number]) => {
      let q = n;
      while (q.parent && q.depth > 1) q = q.parent;
      return q;
    };
    const crownTops = COLD.crown.kids.filter((k) => k.kind !== "pile" && k.nFiles > 0);
    expect(crownTops.length).toBeGreaterThan(1);
    // a clump of each of two different limbs
    const tA = COLD.terms.find((t) => t.leaves.length && t.region === "crown" && topOf(t.node) === crownTops[0])!;
    const tB = COLD.terms.find((t) => t.leaves.length && t.region === "crown" && topOf(t.node) !== crownTops[0] && t.node.depth >= 1)!;
    expect(tA && tB).toBeTruthy();
    const box = (t: typeof tA): [number, number, number, number] => {
      const [x, y] = toS(t.cx, t.cy);
      return [x - 3, y - 3, x + 3, y + 3];
    };
    // on its own clump: fine; on the other limb's clump: never
    expect(fol.sideOk(...box(tA), tA.node)).toBe(true);
    expect(fol.sideOk(...box(tB), tA.node)).toBe(false);
    expect(fol.sideOk(...box(tB), tB.node)).toBe(true);
    // the trunk and the ground piles carry no side
    expect(fol.sideOk(...box(tB), COLD.crown)).toBe(true);
  });

  it("wood lit by a change outside a zone is cut out of that zone's territory; a change inside stays in", () => {
    const prov = COLD.nodeOf.get("backend/providers")!;
    const z: RedZone = { id: "p", pattern: "backend/providers", name: "", re: "^backend/providers/", kind: "red" };
    const tz = treeZones(COLD, [z]);
    const outsideF = COLD.files.find((f) => f.leaf && f.node && f.path.startsWith("frontend/"))!;
    const insideF = COLD.files.find((f) => f.leaf && f.node && f.path.startsWith("backend/providers/"))!;
    const out = foreignLit({ M: COLD, agents: [agent(COLD, { edits: new Map([[outsideF.id, 1]]) })] }, tz);
    expect(out.length).toBeGreaterThan(0);
    // none of the cut wood is the zone's own
    for (const b of out) {
      const o = b.node || b.owner;
      let q = o,
        under = false;
      while (q) {
        if (q === prov) under = true;
        q = q.parent;
      }
      expect(under).toBe(false);
    }
    expect(foreignLit({ M: COLD, agents: [agent(COLD, { edits: new Map([[insideF.id, 1]]) })] }, tz)).toEqual([]);
  });
});
