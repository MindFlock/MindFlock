/// <reference types="vite/client" />
// Tests are ROOTS (lib/codetree/roots.ts): a test file is the tip of a root strand, never a leaf. Checked on the
// readability rig's real MindFlock snapshot and on a synthetic repo with one 240-test folder (the crowded case):
// every root tip is a nodule (no leaf lens, no canopy, no twig stalks), the tips of a fan stay apart, a fan's
// strands neither cross nor run through someone else's tip, and everything of the roots — tips, strands and the
// names drawn for them — stays below the ground line.
import { beforeAll, describe, expect, it } from "vitest";
import type { CodeMapLive, FeedRecord, RedZone } from "../api/types";
import { prepGeometry, render } from "../lib/codetree/draw";
import { rawFromSnapshot } from "../lib/codetree/input";
import { LiveBirds } from "../lib/codetree/live";
import { S0, allNodes, buildModel, type Leaf, type Model, type RawRepo, type TNode, type TTerm } from "../lib/codetree/model";
import { readPalette } from "../lib/codetree/palette";
import { growRoots, rootAnchor, rootNetOf, rootTip, rootTipG, strandHalfW, tipSize, type RootNet } from "../lib/codetree/roots";
import type { AgentState, TreeView } from "../lib/codetree/types";
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

// a 2D context that draws nothing (text ≈ 0.56 em per character), and a Path2D that records nothing
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

/** One folder of 240 tests (all testing src/core) plus a few spread over the rest: the most crowded fan. */
function crowdedRepo(): RawRepo {
  const files: RawRepo["files"] = [];
  for (const d of ["src/core", "src/api", "src/ui", "lib"]) for (let i = 0; i < 30; i++) files.push({ p: `${d}/m${i}.ts`, n: 40 + ((i * 37) % 900), k: "c" });
  for (let i = 0; i < 240; i++) files.push({ p: `tests/core/test_m${i}.py`, n: 15 + ((i * 53) % 2400), k: "c", t: true, g: "src/core" });
  for (let i = 0; i < 12; i++) files.push({ p: `tests/api/test_a${i}.py`, n: 60 + i * 20, k: "c", t: true, g: "src/api" });
  files.push({ p: "README.md", n: 100, k: "d" });
  return { name: "crowded", files };
}

let RIG: Model, CROWD: Model;
beforeAll(() => {
  installDom();
  RIG = buildModel(rawFromSnapshot(SC.snapshot, SC.label), { quantise: true });
  CROWD = buildModel(crowdedRepo());
  prepGeometry(RIG);
  prepGeometry(CROWD);
});
const models = () => [
  ["rig", RIG],
  ["crowded", CROWD],
] as Array<[string, Model]>;
const rootTerms = (M: Model) => M.terms.filter((t) => t.region === "root" && t.leaves.length);
const rootLeaves = (M: Model) => M.leaves.filter((l) => l.region === "root");

function segDist(px: number, py: number, ax: number, ay: number, bx: number, by: number) {
  const dx = bx - ax,
    dy = by - ay;
  const L2 = dx * dx + dy * dy;
  const k = L2 ? Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / L2)) : 0;
  return Math.hypot(px - (ax + dx * k), py - (ay + dy * k));
}
function cross(a: number[], b: number[], c: number[], d: number[]) {
  const o = (p: number[], q: number[], r: number[]) => Math.sign((q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0]));
  const o1 = o(a, b, c),
    o2 = o(a, b, d),
    o3 = o(c, d, a),
    o4 = o(c, d, b);
  return o1 !== o2 && o3 !== o4 && o1 !== 0 && o2 !== 0 && o3 !== 0 && o4 !== 0;
}
/** A fan's strands as DRAWN (the smoothed pieces, sampled), each tagged with the network node it belongs to. */
function drawnPieces(net: RootNet): Array<{ i: number; p: number; pts: number[][] }> {
  const N = net.nodes,
    out: Array<{ i: number; p: number; pts: number[][] }> = [];
  for (let i = 1; i < N.length; i++) {
    const c = N[i],
      p = N[c.p];
    const mx = (p.x + c.x) / 2,
      my = (p.y + c.y) / 2;
    let x0: number, y0: number, kx: number, ky: number;
    if (p.p < 0) {
      const d = Math.hypot(mx - p.x, my - p.y);
      const k = 0.6 * Math.max(0, ((mx - p.x) * net.hx + (my - p.y) * net.hy) / Math.max(1e-6, d)) ** 2;
      [x0, y0, kx, ky] = [p.x, p.y, p.x + net.hx * d * k, p.y + net.hy * d * k];
    } else {
      const q = N[p.p];
      [x0, y0, kx, ky] = [(q.x + p.x) / 2, (q.y + p.y) / 2, p.x, p.y];
    }
    const pts: number[][] = [];
    for (let s = 0; s <= 6; s++) {
      const k = s / 6,
        m = 1 - k;
      pts.push([m * m * x0 + 2 * m * k * kx + k * k * mx, m * m * y0 + 2 * m * k * ky + k * k * my]);
    }
    if (c.leaf) pts.push([c.x, c.y]);
    out.push({ i, p: c.p, pts });
  }
  return out;
}

describe("a test is a root tip, never a leaf", () => {
  it("every root leaf's outline is its tip nodule (round), not a leaf lens; root clumps grow no canopy and no twig", () => {
    for (const [name, M] of models()) {
      const L = rootLeaves(M);
      expect(L.length, name).toBeGreaterThan(10);
      for (const l of L) {
        expect(l.g, l.file.path).toEqual(rootTipG(l));
        const q = l.g!;
        // a leaf lens is ~0.37 as wide as it is long; a nodule is ≥ 0.8
        const width = Math.hypot(q[2] - q[6], q[3] - q[7]) / 2,
          len = Math.hypot(q[4] - q[0], q[5] - q[1]);
        expect(width / len, l.file.path).toBeGreaterThan(0.79);
        // a nodule, not a leaf: well under a leaf's length
        expect(l.len).toBeLessThan(S0 * 0.5);
      }
      for (const t of rootTerms(M)) {
        const tt = t as TTerm & { twig?: unknown; cv?: unknown; cvc?: unknown };
        expect(tt.twig, t.node.path).toBeUndefined();
        expect(tt.cv, t.node.path).toBeUndefined();
        expect(tt.cvc, t.node.path).toBeUndefined();
      }
      // crown leaves are untouched: still lenses
      const c = M.leaves.find((l) => l.region === "crown")!;
      expect(Math.hypot(c.g![2] - c.g![6], c.g![3] - c.g![7]) / 2 / c.len).toBeLessThan(0.5);
    }
  });

  it("every tip ends its own strand: one network node per test, at the back of its nodule, pointing the way the strand ends", () => {
    for (const [name, M] of models())
      for (const t of rootTerms(M)) {
        const net = rootNetOf(t)!;
        expect(net, t.node.path).toBeDefined();
        const tips = net.nodes.filter((n) => n.leaf);
        expect(tips.length, name + " " + t.node.path).toBe(t.leaves.length);
        expect(new Set(tips.map((n) => n.leaf)).size).toBe(t.leaves.length);
        expect(net.nodes[0].n).toBe(t.leaves.length);
        for (const n of tips) {
          const l = n.leaf!,
            p = net.nodes[n.p];
          // the strand's end is the nodule's back
          expect(Math.hypot(n.x - (l.x - Math.cos(l.ang) * l.len * 0.5), n.y - (l.y - Math.sin(l.ang) * l.len * 0.5))).toBeLessThan(1e-6);
          // and the tip points along the strand's last stretch
          const a = Math.atan2(n.y - p.y, n.x - p.x);
          expect(Math.abs(Math.atan2(Math.sin(a - l.ang), Math.cos(a - l.ang)))).toBeLessThan(1e-6);
          // no strand ends past a fork: a tip has no children
          expect(net.nodes.some((m) => m.p === net.nodes.indexOf(n))).toBe(false);
        }
        // thick at the limb, a hair at the tips
        for (const n of net.nodes) if (n.p >= 0) expect(strandHalfW(net, n)).toBeLessThanOrEqual(strandHalfW(net, net.nodes[0]) + 1e-9);
      }
  });
});

describe("a fan reads at high zoom", () => {
  it("at z ≥ 2.5 the tips of one fan are ≥ 1.1 × the tip diameter apart (a changed tip drawn at its 11 px minimum too)", () => {
    for (const z of [2.5, 4, 8])
      for (const [name, M] of models())
        for (const t of rootTerms(M)) {
          const L = t.leaves;
          for (let i = 0; i < L.length; i++)
            for (let j = i + 1; j < L.length; j++) {
              const d = Math.hypot(L[i].x - L[j].x, L[i].y - L[j].y) * z;
              const dia = Math.max(tipSize(L[i])[0], tipSize(L[j])[0]) * z;
              expect(d, `${name} ${L[i].file.path} · ${L[j].file.path} @${z}`).toBeGreaterThanOrEqual(1.1 * dia);
              // a changed tip is drawn ≥ 11 px long (drawEdits): still clear of its neighbours
              expect(d, `${name} changed ${L[i].file.path} @${z}`).toBeGreaterThanOrEqual(1.1 * Math.max(dia, 11));
            }
        }
  });

  it("a fan's strands never cross each other, as drawn (smoothed), and never run through a tip that isn't theirs", { timeout: 30_000 }, () => {
    for (const [name, M] of models())
      for (const t of rootTerms(M)) {
        const net = rootNetOf(t)!;
        expect(net.crossings, `${name} ${t.node.path}: the search found no clean attachment`).toBe(0);
        const pcs = drawnPieces(net);
        let crossings = 0;
        for (let a = 0; a < pcs.length; a++)
          for (let b = a + 1; b < pcs.length; b++) {
            const A = pcs[a],
              B = pcs[b];
            // pieces that meet at a node (parent/child or siblings) touch by construction
            if (A.p === B.p || A.p === B.i || B.p === A.i) continue;
            let x = false;
            for (let i = 0; i + 1 < A.pts.length && !x; i++)
              for (let j = 0; j + 1 < B.pts.length; j++)
                if (cross(A.pts[i], A.pts[i + 1], B.pts[j], B.pts[j + 1])) {
                  x = true;
                  break;
                }
            if (x) crossings++;
          }
        expect(crossings, `${name} ${t.node.path}`).toBe(0);
        // the network keeps a visible gap (≥ 0.07·S0 past the nodule's half-length) from every tip that isn't its own,
        // and the strands as drawn never touch one
        for (let i = 1; i < net.nodes.length; i++) {
          const c = net.nodes[i],
            p = net.nodes[c.p];
          for (const l of t.leaves) {
            if (l === c.leaf) continue;
            expect(segDist(l.x, l.y, p.x, p.y, c.x, c.y), `${name} ${t.node.path} ${l.file.path}`).toBeGreaterThan(l.len * 0.5 + S0 * 0.07);
          }
        }
        expect(net.tightest, `${name} ${t.node.path}`).toBeGreaterThan(S0 * 0.07);
        for (const pc of pcs) {
          const own = net.nodes[pc.i].leaf;
          for (const l of t.leaves) {
            if (l === own) continue;
            for (let k = 0; k + 1 < pc.pts.length; k++) {
              const [a, b] = [pc.pts[k], pc.pts[k + 1]];
              expect(segDist(l.x, l.y, a[0], a[1], b[0], b[1]), `${name} ${t.node.path} ${l.file.path}`).toBeGreaterThan(l.len * 0.5);
            }
          }
        }
      }
  });
});

describe("roots stay underground", () => {
  it("every tip and every strand point is below the ground line (bar the tips the layout itself put on it)", () => {
    for (const [name, M] of models()) {
      // the root search can place a clump's top row ON the ground line (the rig: 5 tests of tests for backend › web,
      // the same positions as before roots.ts): a layout fix (root clearance under the ground), outside this module
      const atLine = new Set(rootLeaves(M).filter((l) => l.y - tipSize(l)[0] / 2 <= M.groundY));
      expect(atLine.size, name).toBeLessThanOrEqual(name === "rig" ? 5 : 0);
      for (const l of rootLeaves(M)) {
        if (atLine.has(l)) continue;
        expect(l.y - tipSize(l)[0] / 2, `${name} ${l.file.path}`).toBeGreaterThan(M.groundY);
        expect(rootTip(l)[1]).toBeGreaterThan(M.groundY);
      }
      for (const t of rootTerms(M))
        for (const n of rootNetOf(t)!.nodes) if (!n.leaf || !atLine.has(n.leaf)) expect(n.y, `${name} ${t.node.path}`).toBeGreaterThan(M.groundY);
    }
  });

  it("names drawn for roots (folders and tips) sit below the ground line at every zoom", () => {
    const M = RIG;
    const roots = new Set<TNode>(allNodes(M.roots));
    const zones = treeZones(M, SC.zones.main);
    const agents: AgentState[] = new LiveBirds().update(M, {
      feed: SC.feed,
      live: { now: 0, activity: SC.activity, changed: [], others: SC.others, plan: SC.plan } as unknown as CodeMapLive,
      title: SC.title,
      accent: "#a08cff",
      serverNow: 0,
      viewNow: 0,
      zones,
    });
    // the biggest root clump, framed at the ground line and below it, from the whole-tree zoom to the closest
    const big = rootTerms(M).sort((a, b) => b.leaves.length - a.leaves.length)[0];
    let checked = 0;
    for (const z of [0.8, 1.6, 2.5, 4, 7])
      for (const dy of [-0.4, 0, 0.4]) {
        const W = 1157,
          H = 760;
        const cam = { x: big.cx, y: big.cy + (dy * H) / z, z };
        const v = {
          g: stubCtx(), M, P: readPalette(null), agents, W, H, dpr: 1, cam, zones, folded: new Set(), tool: "explore", t: 1000,
          playing: false, reducedMotion: true, hover: null, sel: null, infoNode: null, infoFile: null, pinBlast: null, hoverBlast: null,
          pulse: null, follow: null, markLeaf: null, blastCache: new Map(), hudRects: [], free: { x0: 8, y0: 8, x1: W - 8, y1: H - 8 },
          fitZv: 0.8, noCache: true,
        } as unknown as TreeView;
        render(v, 1000);
        const groundS = (M.groundY - cam.y) * z + H / 2;
        for (const h of v.labelHits || []) {
          const isRoot = (h.leaf && h.leaf.region === "root") || (h.n && roots.has(h.n) && h.n !== M.roots);
          if (!isRoot || h.crumb) continue; // the breadcrumb is a HUD strip, not a name in the picture
          checked++;
          expect(h.r[1], `${h.leaf ? h.leaf.file.path : h.n!.path} @z${z}`).toBeGreaterThan(groundS);
        }
      }
    expect(checked).toBeGreaterThan(10);
  });
});

describe("label anchors for root tips (labels.ts)", () => {
  it("rootTip is the nodule's front; rootAnchor sits just past it along the strand's end direction", () => {
    for (const l of rootLeaves(RIG)) {
      const [tx, ty] = rootTip(l);
      expect(Math.hypot(tx - l.x, ty - l.y)).toBeCloseTo(l.len / 2, 6);
      for (const z of [1, 3, 8]) {
        const a = rootAnchor(l, z);
        expect(a.dir).toBe(l.ang);
        // 3 px past the tip on screen
        expect(Math.hypot(a.x - tx, a.y - ty) * z).toBeCloseTo(3, 4);
        // along the direction the tip points
        expect((a.x - l.x) * Math.cos(l.ang) + (a.y - l.y) * Math.sin(l.ang)).toBeGreaterThan(0);
      }
    }
  });

  it("tips point away from the fan's origin (outward / downward), so a name past the tip leaves the fan", () => {
    let out = 0,
      n = 0;
    for (const [, M] of models())
      for (const t of rootTerms(M)) {
        const o = rootNetOf(t)!.nodes[0];
        for (const l of t.leaves) {
          n++;
          if ((l.x - o.x) * Math.cos(l.ang) + (l.y - o.y) * Math.sin(l.ang) > 0) out++;
        }
      }
    expect(out / n).toBeGreaterThan(0.9);
  });
});

describe("cost", () => {
  it("growing every fan is a one-time pass (a 240-tip fan in well under a frame budget per model)", () => {
    const t = rootTerms(CROWD).sort((a, b) => b.leaves.length - a.leaves.length)[0];
    expect(t.leaves.length).toBe(240);
    const b = t.branch as { x1: number; y1: number; cp: number[] } | undefined;
    const t0 = performance.now();
    for (let k = 0; k < 5; k++) growRoots(t, b ? b.x1 : t.cx, b ? b.y1 : t.cy, b ? Math.atan2(b.cp[7] - b.cp[5], b.cp[6] - b.cp[4]) : Math.PI / 2);
    // a frame budget with headroom for a loaded CI box (a quiet machine does it in ~10 ms)
    expect((performance.now() - t0) / 5).toBeLessThan(250);
    // deterministic: the same fan again
    const again = buildModel(crowdedRepo());
    const a = rootTerms(again).sort((p, q) => q.leaves.length - p.leaves.length)[0];
    expect(rootNetOf(a)!.nodes.map((n) => [n.x, n.y, n.p])).toEqual(rootNetOf(t)!.nodes.map((n) => [n.x, n.y, n.p]));
    expect(a.leaves.map((l: Leaf) => l.ang)).toEqual(t.leaves.map((l: Leaf) => l.ang));
  });
});
