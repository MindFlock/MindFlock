// Code Tree v2 — leaves (lib/codetree/leafgeom.ts): zoomed in, every leaf of a folder is its own shape, with open
// air at its tip for a name; zoomed out a folder is still one clump, not much wider than before.
// Runs on the in-repo fixture, and on the bigger `mf` fixture (the MindFlock repo, 1.1k files) when the prototype
// fixtures are on this machine (~/mindflock-prototypes/map-ux/fixtures/mf_snap.json).
import { describe, expect, it } from "vitest";
import { rawFromSnapshot } from "../lib/codetree/input";
import { S0 } from "../lib/codetree/layout";
import { LEAF, capPx, leafOutline, leafScreen } from "../lib/codetree/leafgeom";
import { buildModel, type Leaf, type Model, type RawRepo, type TTerm } from "../lib/codetree/model";
import fixtureRaw from "./fixtures/codetree_repo.json?raw";

const FX = JSON.parse(fixtureRaw) as RawRepo;
// node built-ins without @types/node (the frontend's tsconfig has none)
const NODE = (globalThis as unknown as { process: { env: Record<string, string | undefined>; stdout: { write(s: string): void } } }).process;
const fs = (await import(/* @vite-ignore */ "node:" + "fs")) as { existsSync(p: string): boolean; readFileSync(p: string, enc: string): string };
const MF_SNAP = (NODE.env.HOME || "/nonexistent") + "/mindflock-prototypes/map-ux/fixtures/mf_snap.json";
const HAVE_MF = fs.existsSync(MF_SNAP);

const models: Array<[string, () => Model]> = [["repo fixture", () => buildModel({ name: FX.name, files: FX.files })]];
if (HAVE_MF)
  models.push(["mf fixture", () => buildModel(rawFromSnapshot(JSON.parse(fs.readFileSync(MF_SNAP, "utf8")), "MindFlock"), { quantise: true })]);
const memo = new Map<string, Model>();
const model = (k: string, f: () => Model) => memo.get(k) || (memo.set(k, f()), memo.get(k)!);

// --- geometry ---
/** the lens outline as a polygon (the two quadratic sides, sampled) */
function lensPoly(q: number[], k = 10): number[][] {
  const pts: number[][] = [];
  const quad = (x0: number, y0: number, cx: number, cy: number, x1: number, y1: number) => {
    for (let i = 0; i < k; i++) {
      const t = i / k,
        m = 1 - t;
      pts.push([m * m * x0 + 2 * m * t * cx + t * t * x1, m * m * y0 + 2 * m * t * cy + t * t * y1]);
    }
  };
  quad(q[0], q[1], q[2], q[3], q[4], q[5]);
  quad(q[4], q[5], q[6], q[7], q[0], q[1]);
  return pts;
}
function segsCross(a: number[], b: number[], c: number[], d: number[]) {
  const o = (p: number[], q: number[], r: number[]) => Math.sign((q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0]));
  return o(a, b, c) !== o(a, b, d) && o(c, d, a) !== o(c, d, b);
}
function inside(p: number[], poly: number[][]) {
  let c = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const a = poly[i],
      b = poly[j];
    if (a[1] > p[1] !== b[1] > p[1] && p[0] < ((b[0] - a[0]) * (p[1] - a[1])) / (b[1] - a[1]) + a[0]) c = !c;
  }
  return c;
}
function polysMeet(A: number[][], B: number[][]) {
  for (let i = 0; i < A.length; i++)
    for (let j = 0; j < B.length; j++) if (segsCross(A[i], A[(i + 1) % A.length], B[j], B[(j + 1) % B.length])) return true;
  return inside(A[0], B) || inside(B[0], A);
}
/** distance along the ray (o, dir) to the polygon's boundary, or Infinity */
function rayHit(ox: number, oy: number, dx: number, dy: number, P: number[][]) {
  let best = Infinity;
  for (let i = 0; i < P.length; i++) {
    const a = P[i],
      b = P[(i + 1) % P.length];
    const ex = b[0] - a[0],
      ey = b[1] - a[1];
    const den = dx * ey - dy * ex;
    if (Math.abs(den) < 1e-12) continue;
    const t = ((a[0] - ox) * ey - (a[1] - oy) * ex) / den,
      u = ((a[0] - ox) * dy - (a[1] - oy) * dx) / den;
    if (t >= 0 && u >= 0 && u <= 1) best = Math.min(best, t);
  }
  return best;
}
const crownTerms = (M: Model) => (M.terms as TTerm[]).filter((t) => t.region === "crown" && t.leaves.length > 1);
const box = (P: number[][]) => {
  let x0 = Infinity,
    y0 = Infinity,
    x1 = -Infinity,
    y1 = -Infinity;
  for (const [x, y] of P) {
    x0 = Math.min(x0, x);
    y0 = Math.min(y0, y);
    x1 = Math.max(x1, x);
    y1 = Math.max(y1, y);
  }
  return [x0, y0, x1, y1];
};

/** pairs of leaves of one clump whose drawn outlines meet at zoom z */
function overlaps(M: Model, z: number): string[] {
  const bad: string[] = [];
  for (const t of crownTerms(M)) {
    const polys = t.leaves.map((l) => lensPoly(leafOutline(l, z)));
    const bx = polys.map(box);
    for (let i = 0; i < polys.length; i++)
      for (let j = i + 1; j < polys.length; j++) {
        const a = bx[i],
          b = bx[j];
        if (a[2] < b[0] || b[2] < a[0] || a[3] < b[1] || b[3] < a[1]) continue;
        if (polysMeet(polys[i], polys[j])) bad.push(t.leaves[i].file.path + " × " + t.leaves[j].file.path);
      }
  }
  return bad;
}
/** per leaf: screen px of open air from its drawn tip along its pointing direction, up to the next outline of its clump */
function tipRoom(M: Model, z: number): Array<[Leaf, number]> {
  const out: Array<[Leaf, number]> = [];
  for (const t of crownTerms(M)) {
    const polys = t.leaves.map((l) => lensPoly(leafOutline(l, z)));
    t.leaves.forEach((l, i) => {
      const s = leafScreen(l, z);
      const dx = Math.cos(s.dir),
        dy = Math.sin(s.dir);
      let d = Infinity;
      for (let j = 0; j < polys.length; j++) if (j !== i) d = Math.min(d, rayHit(s.tx, s.ty, dx, dy, polys[j]));
      out.push([l, d * z]);
    });
  }
  return out;
}

describe("leafScreen", () => {
  const l = {
    x: 10,
    y: 20,
    len: 8,
    ang: -Math.PI / 2,
    region: "crown",
  } as unknown as Leaf;
  it("is the true leaf until the cap, then a shorter leaf on the same base", () => {
    const a = leafScreen(l, 2);
    expect(a.halfPx).toBeCloseTo(8, 6);
    expect([a.bx, a.by, a.tx, a.ty, a.cx, a.cy]).toEqual([10, 24, 10, 16, 10, 20].map((v) => expect.closeTo(v, 6)));
    const b = leafScreen(l, 8); // 64 px at true size
    expect(b.halfPx * 2).toBeLessThanOrEqual(LEAF.capTo);
    expect(b.halfPx * 2).toBeGreaterThan(LEAF.capFrom);
    expect([b.bx, b.by]).toEqual([10, 24].map((v) => expect.closeTo(v, 6))); // the stalk still ends at the base
    expect(b.ty).toBeCloseTo(24 - (b.halfPx * 2) / 8, 6);
    expect(b.wPx).toBeCloseTo(b.halfPx * 2 * LEAF.halfW, 6);
  });
  it("never shrinks a leaf as you zoom in, and never lets it pass the cap", () => {
    let prev = 0;
    for (let px = 1; px < 400; px += 0.5) {
      const d = capPx(px);
      expect(d).toBeGreaterThanOrEqual(prev);
      expect(d).toBeLessThanOrEqual(LEAF.capTo);
      prev = d;
    }
  });
  it("leaves ground (pile) leaves at true size", () => {
    const g = { ...l, region: "ground" } as Leaf;
    expect(leafScreen(g, 8).halfPx).toBeCloseTo(32, 6);
  });
});

describe.each(models)("crown leaves on the %s", (k, f) => {
  it("never overlap within a clump (z = 2.5 and z = 4)", () => {
    const M = model(k, f);
    expect(overlaps(M, 2.5)).toEqual([]);
    expect(overlaps(M, 4)).toEqual([]);
  });
  it("have ≥ 12 px of open air at every tip at z = 4", () => {
    const M = model(k, f);
    const tight = tipRoom(M, 4)
      .filter(([, d]) => d < 12)
      .map(([l, d]) => l.file.path + " " + d.toFixed(1));
    expect(tight).toEqual([]);
  });
  it("point within a few degrees of away-from-the-twig", () => {
    const M = model(k, f);
    for (const t of crownTerms(M)) {
      const b = t.branch!;
      for (const l of t.leaves) {
        const d = Math.hypot(l.x - b.x1, l.y - b.y1);
        if (d < t.s * 0.6) continue;
        const want = Math.atan2(l.y - b.y1, l.x - b.x1);
        const off = Math.abs(Math.atan2(Math.sin(l.ang - want), Math.cos(l.ang - want)));
        expect(off).toBeLessThanOrEqual(LEAF.jitAng / 2 + 1e-9);
      }
    }
  });
  it("keeps a clump about as wide as before zoomed out (≤ 40 % more radius)", () => {
    const M = model(k, f);
    const was = { pitch: LEAF.pitch, jitPos: LEAF.jitPos };
    let H: Model;
    try {
      // HEAD's packing (pitch 0.92·S0, ±0.15·pitch jitter) for the same data
      LEAF.pitch = 0.92;
      LEAF.jitPos = 0.3;
      H = f();
    } finally {
      Object.assign(LEAF, was);
    }
    const key = (t: TTerm) => t.node.path + (t.own ? "#" : "");
    const head = new Map(crownTerms(H).map((t) => [key(t), t.rad]));
    let worst = 0,
      sum = 0,
      n = 0;
    for (const t of crownTerms(M)) {
      const r0 = head.get(key(t));
      if (!r0) continue;
      const g = t.rad / r0;
      worst = Math.max(worst, g);
      sum += g;
      n++;
    }
    expect(n).toBeGreaterThan(0);
    expect(worst).toBeLessThanOrEqual(1.4);
    if (NODE.env.LEAF_STATS) {
      const room = tipRoom(M, 4).map(([, d]) => d);
      room.sort((a, b) => a - b);
      const wb = (m: Model) => [m.crownBounds.x1 - m.crownBounds.x0, m.crownBounds.y1 - m.crownBounds.y0];
      NODE.stdout.write(
        k +
          " " +
          JSON.stringify({
            clumps: n,
            radGrowthMean: +(sum / n).toFixed(3),
            radGrowthMax: +worst.toFixed(3),
            tipRoomPx_min: +room[0].toFixed(1),
            tipRoomPx_p10: +room[Math.floor(room.length * 0.1)].toFixed(1),
            tipRoomPx_median: +room[Math.floor(room.length * 0.5)].toFixed(1), // Infinity (null): open sky
            crownWH_head: wb(H).map((v) => +v.toFixed(0)),
            crownWH_now: wb(M).map((v) => +v.toFixed(0)),
            S0,
          }) +
          "\n",
      );
    }
  });
});
