/// <reference types="vite/client" />
// The Code Tree's folder names and zone signs on the readability rig's scenario
// (mindflock-prototypes/readability/rig: the real MindFlock snapshot, the tool
// feed at T0, one keep-out on backend/providers and an only-here on
// backend/web/core): rendered for real (draw.ts against a recording 2D context)
// at the pane sizes the grid gives a Map tab, and checked the way a reader
// reads them — a folder's name belongs to whatever sits nearest to it, so the
// nearest foliage / nest / zone sign / territory must be the folder's own.
import { beforeAll, describe, expect, it } from "vitest";
import type { CodeMapLive, FeedRecord, RedZone } from "../api/types";
import { convexHull, prepGeometry, rectPolyDist, render, terrPad, zoneLobes } from "../lib/codetree/draw";
import { rawFromSnapshot } from "../lib/codetree/input";
import { LiveBirds } from "../lib/codetree/live";
import { S0, buildModel, isUnder, type Model, type TNode } from "../lib/codetree/model";
import { readPalette } from "../lib/codetree/palette";
import type { AgentState, TreeView, TZone } from "../lib/codetree/types";
import { BASE_GAP, TIP_GAP, candidatesForLeaf, insideView, leafScreenGeom } from "../lib/codetree/labels";
import { treeZones } from "../lib/codetree/zones";
import scenarioRaw from "./fixtures/codetree_scenario.json?raw";

interface Scenario {
  label: string;
  title: string;
  activity: string;
  snapshot: { files: Array<[string, number, number]>; edges: Array<[number, number]> };
  feed: FeedRecord[];
  others: Array<{ session: string; path: string; ts: number }>;
  plan: CodeMapLive["plan"];
  zones: Record<string, RedZone[]>;
}
const SC = JSON.parse(scenarioRaw) as Scenario;

// --- a 2D context that draws nothing but measures text like a browser would (≈0.56 em per character) ---
function stubCtx(): CanvasRenderingContext2D {
  const st: Record<string, unknown> = { font: "10px sans-serif", globalAlpha: 1 };
  const grad = { addColorStop() {} };
  return new Proxy(st, {
    get(o, k: string) {
      if (k in o) return o[k];
      if (k === "measureText")
        return (t: string) => {
          const m = /(\d+(?:\.\d+)?)px/.exec(String(o.font));
          return { width: String(t).length * (m ? parseFloat(m[1]) : 10) * 0.56 };
        };
      if (k === "createLinearGradient" || k === "createRadialGradient" || k === "createConicGradient") return () => grad;
      if (k === "getImageData") return () => ({ data: new Uint8ClampedArray(4) });
      return () => undefined;
    },
    set(o, k: string, val) {
      o[k] = val;
      return true;
    },
  }) as unknown as CanvasRenderingContext2D;
}
function installDom() {
  const g = globalThis as Record<string, unknown>;
  if (typeof g.Path2D === "undefined")
    g.Path2D = class {
      moveTo() {}
      lineTo() {}
      bezierCurveTo() {}
      quadraticCurveTo() {}
      arc() {}
      ellipse() {}
      rect() {}
      closePath() {}
      addPath() {}
    };
  if (typeof g.document === "undefined")
    g.document = {
      createElement: () => ({ width: 1, height: 1, getContext: () => stubCtx() }),
      documentElement: { classList: { contains: () => false } },
    };
}

// the Map tab's canvas and HUD (bird index column; the activity strip insets a narrow pane's bottom) as the grid
// lays them out: one pane at 1440x900, a 4-pane grid's pane, a 420 px phone-width window, and a mid size
const SIZES: Array<{ name: string; W: number; H: number; hud: Array<[number, number, number, number]>; left: number; bot: number }> = [
  { name: "1 pane 1440x900", W: 1157, H: 760, hud: [[5, 5, 235, 656], [846, 684, 1154, 756]], left: 239, bot: 8 },
  { name: "4-pane grid", W: 572, H: 335, hud: [[5, 5, 124, 179], [261, 298, 569, 332]], left: 128, bot: 39 },
  { name: "420 px window", W: 398, H: 638, hud: [[5, 6, 101, 184]], left: 105, bot: 8 },
  { name: "mid pane", W: 800, H: 520, hud: [[5, 5, 124, 179]], left: 128, bot: 8 },
];

let M: Model;
const birdsOf = (zones: TZone[]): AgentState[] =>
  new LiveBirds().update(M, {
    feed: SC.feed,
    live: { now: 0, activity: SC.activity, changed: [], others: SC.others, plan: SC.plan } as unknown as CodeMapLive,
    title: SC.title,
    accent: "#a08cff",
    serverNow: 0,
    viewNow: 0,
    zones,
  });

function view(sz: (typeof SIZES)[number], scen: string): TreeView {
  const zones = treeZones(M, SC.zones[scen]);
  const agents = birdsOf(zones);
  const B = M.bounds;
  const w = sz.W - sz.left - 8,
    h = sz.H - 8 - sz.bot;
  const z = Math.min((w * 0.94) / (B.x1 - B.x0), (h * 0.94) / (B.y1 - B.y0));
  const cam = { x: (B.x0 + B.x1) / 2 - (sz.left - 8) / 2 / z, y: (B.y0 + B.y1) / 2 - (8 - sz.bot) / 2 / z, z };
  return {
    g: stubCtx(), M, P: readPalette(null), agents, W: sz.W, H: sz.H, dpr: 1, cam, zones, folded: new Set(), tool: "explore", t: 1000,
    playing: false, reducedMotion: true, hover: null, sel: null, infoNode: null, infoFile: null, pinBlast: null, hoverBlast: null, pulse: null,
    follow: null, markLeaf: null, blastCache: new Map(), hudRects: sz.hud, free: { x0: sz.left, y0: 8, x1: sz.W - 8, y1: sz.H - sz.bot }, fitZv: z,
    noCache: true,
  } as TreeView;
}

/** How a reader takes in a name at rect R (the rule the placers keep, measured independently here): LOUD things —
 * a glowing (changed) clump, a nest, a zone's sign or territory — claim a name that touches them, so they count by
 * the name's edge; the calm canopy reads as one quiet mass, so it counts from the name's centre. Things more than
 * 220 px off are not "near" at all. */
function nearest(v: TreeView, R: number[], n: TNode) {
  const z = v.cam.z,
    toS = v.toS!;
  const RANGE = 220;
  let own = Infinity,
    loud = Infinity,
    ownC = Infinity,
    calmC = Infinity,
    by = "";
  const cx = (R[0] + R[2]) / 2,
    cy = (R[1] + R[3]) / 2;
  const edge = (x: number, y: number, r: number) => Math.max(0, Math.hypot(Math.max(R[0] - x, 0, x - R[2]), Math.max(R[1] - y, 0, y - R[3])) - r);
  const rectD = (r: number[]) => Math.hypot(Math.max(r[0] - R[2], 0, R[0] - r[2]), Math.max(r[1] - R[3], 0, R[1] - r[3]));
  const loudThing = (d: number, mine: boolean, what: string, r?: number[]) => {
    if (d > RANGE) return;
    if (mine) {
      own = Math.min(own, d);
      if (r) ownC = Math.min(ownC, Math.hypot(Math.max(r[0] - cx, 0, cx - r[2]), Math.max(r[1] - cy, 0, cy - r[3])));
    }
    else if (d < loud) {
      loud = d;
      by = what;
    }
  };
  for (const t of M.terms) {
    if (!t.leaves.length) continue;
    const [x, y] = toS(t.cx, t.cy);
    const r = (t.rad + (t.node.fAnc ? t.s * 0.5 : 0)) * z + 1; // the clump as drawn (a folded one is its canopy)
    const e = edge(x, y, r);
    if (e > RANGE) continue;
    const c = Math.max(0, Math.hypot(x - cx, y - cy) - r);
    const li = v.litInfo && v.litInfo.get(t.node);
    if (isUnder(t.node, n)) {
      own = Math.min(own, e);
      ownC = Math.min(ownC, c);
    } else if (li && li.own > 0) loudThing(e, false, "glowing clump " + t.node.path);
    else if (c < calmC) {
      calmC = c;
      if (!by) by = "calm clump " + t.node.path;
    }
  }
  for (const h of v.nestHits || []) {
    const nn = h.A.nest;
    if (nn && nn.depth >= 1) loudThing(rectD(h.r), isUnder(nn, n), "nest " + nn.path, h.r);
  }
  for (const h of v.tagHits || []) {
    const zn = h.z.node;
    if (zn) loudThing(rectD(h.r), isUnder(zn, n) || isUnder(n, zn), "sign " + zn.path, h.r);
  }
  for (const T of v.terrPolys || [])
    for (const poly of T.polys) {
      const mine = isUnder(T.n, n) || isUnder(n, T.n);
      loudThing(rectPolyDist(R[0], R[1], R[2], R[3], poly), mine, "territory " + T.n.path);
      if (mine) ownC = Math.min(ownC, rectPolyDist(cx, cy, cx, cy, poly));
    }
  // a folder with no clump of its own is its stem
  if (!n.term && n.branch)
    n.branch.pts.forEach((p, k) => {
      const [x, y] = toS(p[0], p[1]);
      const hw = ((n.branch!.wb + (n.branch!.we - n.branch!.wb) * Math.pow(k / (n.branch!.pts.length - 1), 0.9)) / 2) * z;
      own = Math.min(own, edge(x, y, hw));
      ownC = Math.min(ownC, Math.max(0, Math.hypot(x - cx, y - cy) - hw));
    });
  const ok = (loud === Infinity || (loud >= 3 && own <= loud + 0.5)) && (calmC === Infinity || own <= 12 || ownC <= calmC + 0.5);
  return { ok, own, loud, ownC, calmC, by };
}

beforeAll(() => {
  installDom();
  M = buildModel(rawFromSnapshot(SC.snapshot, SC.label), { quantise: true });
  prepGeometry(M);
  (M as Model & { geom?: boolean }).geom = true;
});

describe("folder names stay pinned to their own cluster (the rig's scenario)", () => {
  for (const scen of ["main", "green"])
    for (const sz of SIZES)
      it(`${scen} · ${sz.name}`, () => {
        const v = view(sz, scen);
        render(v, 1000);
        const named = (v.labelHits || []).filter((h) => h.n && !h.crumb && h.n.depth >= 1);
        expect(named.length).toBeGreaterThan(0);
        const bad: string[] = [];
        for (const h of named) {
          const q = nearest(v, h.r, h.n!);
          // the nearest thing a reader sees is the folder's own (and it never touches anyone else's loud things)
          if (!q.ok) bad.push(`${h.n!.path} [${h.r.map(Math.round)}]: own ${q.own.toFixed(1)}/${q.ownC.toFixed(1)} vs ${q.by} loud ${q.loud.toFixed(1)} calm ${q.calmC.toFixed(1)}`);
        }
        expect(bad).toEqual([]);
        // a zone's sign never sits on another folder's nest
        for (const t of v.tagHits || [])
          for (const nh of v.nestHits || []) {
            if (!nh.A.nest || !t.z.node || isUnder(nh.A.nest, t.z.node)) continue;
            const a = t.r,
              b = nh.r;
            expect(a[2] <= b[0] || b[2] <= a[0] || a[3] <= b[1] || b[3] <= a[1]).toBe(true);
          }
        // the keep-out is a territory (the whole branch and its clumps), not a dot: at whole-tree zoom it spans
        // well beyond the clump's own disc on screen
        const keep = (v.terrPolys || []).find((T) => T.keep);
        expect(keep).toBeTruthy();
        const xs = keep!.polys.flatMap((p) => p.filter((_, i) => i % 2 === 0)),
          ys = keep!.polys.flatMap((p) => p.filter((_, i) => i % 2 === 1));
        const span = Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys));
        expect(span).toBeGreaterThan(keep!.n.rad * v.cam.z * 2 + 20);
      });

  it("the folders where the agents nest are named at a full-size pane", () => {
    const v = view(SIZES[0], "main");
    render(v, 1000);
    const names = new Set((v.labelHits || []).filter((h) => h.n).map((h) => h.n!.path));
    for (const A of v.agents) if (A.nest && A.cur && !A.ag.parent && A.nest.depth >= 1) expect(names.has(A.nest.path)).toBe(true);
  });
});

describe("changed files are named even at the whole-tree zoom", () => {
  for (const sz of [SIZES[0], SIZES[3]])
    it(sz.name, () => {
      const v = view(sz, "main");
      render(v, 1000);
      const edited = new Set<number>();
      for (const A of v.agents) for (const k of A.edits.keys()) edited.add(k);
      const named = (v.labelHits || []).filter((h) => h.leaf);
      // only changed files (and a bird's current file, named in its colour) out here, a handful at most. A perched
      // bird's tag names its file when the bird's own body covers the leaf's tip (one name per leaf, rule 2)
      const namedIds = new Set(named.map((h) => h.leaf!.file.id));
      for (const b of v.birdScreen || []) if (b.A.file) namedIds.add(b.A.file.id);
      expect([...namedIds].filter((id) => edited.has(id)).length).toBeGreaterThan(0);
      expect(named.length).toBeLessThanOrEqual(8 + v.agents.length);
      for (const h of named) if (!edited.has(h.leaf!.file.id)) expect(v.agents.some((A) => A.file === h.leaf!.file)).toBe(true);
    });
});

describe("file names once the view is close enough to show them", () => {
  // zoomed onto each changed file in turn (leaves ~30 px: names show, sparsely), at a full pane and a small one; a
  // changed leaf's name may lie over calm grey neighbours (its own leaf is coloured), never over another change
  for (const sz of [SIZES[0], SIZES[3]])
    it(`every changed file on screen is named, at its own leaf · ${sz.name}`, () => {
      const v0 = view(sz, "main");
      const edited = new Set<number>();
      for (const A of v0.agents) for (const k of A.edits.keys()) edited.add(k);
      expect(edited.size).toBeGreaterThan(0);
      const missing: string[] = [];
      for (const id of [...edited].slice(0, 12)) {
        const l = M.files[id].leaf;
        if (!l || l.term?.node.fAnc) continue;
        const v = view(sz, "main");
        const z = 30 / S0;
        const fx = (sz.left + sz.W - 8) / 2,
          fy = (8 + sz.H - sz.bot) / 2;
        v.cam = { x: l.x - (fx - sz.W / 2) / z, y: l.y - (fy - sz.H / 2) / z, z };
        render(v, 1000);
        const toS = v.toS!;
        const named = new Set((v.labelHits || []).filter((h) => h.leaf).map((h) => h.leaf!.file.id));
        // ... or by the tag of the bird perched on it (its body covers the leaf's tip: one name per leaf, in the tag)
        for (const b of v.birdScreen || []) if (b.A.file) named.add(b.A.file.id);
        // each changed leaf in the free canvas has its name
        for (const e of edited) {
          const f = M.files[e];
          if (!f.leaf || f.leaf.term?.node.fAnc) continue;
          const [sx, sy] = toS(f.leaf.x, f.leaf.y);
          if (sx < sz.left + 40 || sx > sz.W - 120 || sy < 30 || sy > sz.H - sz.bot - 30) continue;
          if (named.has(e)) continue;
          // unnamed only when BOTH its spots (tip, base) are taken: by a name, tag, badge, bird, nest, another
          // change, or the canvas edge — a free spot is never passed over
          const a = leafScreenGeom(f.leaf, z, toS, 10.5);
          const taken: number[][] = [...(v.labelHits || []), ...(v.birdHits || []), ...(v.nestHits || []), ...(v.badgeHits || []), ...(v.tagHits || [])].map((h) => h.r);
          for (const r of v.hudRects || []) taken.push(r); // a HUD panel (the agents column, the activity card) is taken too
          for (const o of edited) {
            const ol = M.files[o].leaf;
            if (!ol || o === e) continue;
            const [ox, oy] = toS(ol.x, ol.y);
            taken.push([ox - 7, oy - 7, ox + 7, oy + 7]);
          }
          for (const b of v.birdScreen || []) taken.push([b.sx - 40, b.sy - 50, b.sx + 40, b.sy + 10]); // a bird's body (generous)
          const free = candidatesForLeaf(a, f.name.length * 10.5 * 0.56).filter(
            (c) => insideView(c.R, { W: v.W, H: v.H }) && !taken.some((r) => c.R[2] + 2 > r[0] && c.R[0] - 2 < r[2] && c.R[3] + 2 > r[1] && c.R[1] - 2 < r[3])
          );
          if (free.length) missing.push(`${f.path} @ ${M.files[id].path}`);
        }
        // and every file name is ATTACHED: its box starts at its own leaf's tip (TIP_GAP) or base (BASE_GAP), or at the
        // selection ring around it — never parked beside a neighbour
        for (const h of v.labelHits || []) {
          if (!h.leaf) continue;
          const l = h.leaf,
            a = leafScreenGeom(l, z, toS, edited.has(l.file.id) ? 10.5 : 0);
          const d = (p: [number, number]) => Math.hypot(Math.max(h.r[0] - p[0], 0, p[0] - h.r[2]), Math.max(h.r[1] - p[1], 0, p[1] - h.r[3]));
          const ring = l === v.sel?.leaf ? Math.max(l.len * 0.8 * z, 8) + 2.5 : 0;
          const ok = d(a.tip) <= TIP_GAP + 0.5 || d(a.base) <= BASE_GAP + 0.5 || (ring > 0 && Math.hypot(...toS(l.x, l.y).map((c, i) => Math.max(h.r[i] - c, 0, c - h.r[i + 2])) as [number, number]) <= ring + TIP_GAP + 0.5);
          if (!ok) missing.push(`unattached ${l.file.path} [${h.r.map(Math.round)}]`);
        }
      }
      expect(missing).toEqual([]);
    });
});

describe("territory geometry", () => {
  it("a zone's lobes hug its branch and every clump; padding grows them", () => {
    const prov = M.nodeOf.get("backend/providers")!;
    const lobes = zoneLobes(M, prov, 0);
    expect(lobes.length).toBeGreaterThan(0);
    // every clump of the folder is inside the union, and so is its branch's base (the fork it leaves)
    const inside = (x: number, y: number) => lobes.some((poly) => rectPolyDist(x, y, x, y, poly) === 0);
    for (const t of M.terms) if (isUnder(t.node, prov) && t.leaves.length) expect(inside(t.cx, t.cy)).toBe(true);
    expect(inside(prov.branch!.x0, prov.branch!.y0)).toBe(true);
    const grown = zoneLobes(M, prov, terrPad(0.3));
    const area = (p: number[]) => {
      let a = 0;
      for (let i = 0; i < p.length; i += 2) a += p[i] * p[(i + 3) % p.length] - p[(i + 2) % p.length] * p[i + 1];
      return Math.abs(a) / 2;
    };
    expect(area(grown[0])).toBeGreaterThan(area(lobes[0]));
  });
  it("convex hull: the outer points only", () => {
    expect(convexHull([0, 0, 2, 0, 1, 1, 2, 2, 0, 2]).length).toBe(8);
  });
});
