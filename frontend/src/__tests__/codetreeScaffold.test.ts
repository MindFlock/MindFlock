/// <reference types="vite/client" />
// The Code Tree's SCAFFOLD (lib/codetree/scaffold.ts): docs, CI and root config
// stand beside the trunk as towers of lifts — one lift per group (a pile node),
// one plank per file — instead of leaf piles on the ground. Pins the geometry
// rules (planks in their lift, no overlaps at any zoom, towers clear of the
// trunk and the crown, the node's box holding its planks so framing / hit
// tests hold), the anchors the names use, the hit tests, and that a ground
// file is never drawn as a leaf lens.
import { beforeAll, describe, expect, it } from "vitest";
import { pick, prepGeometry, render } from "../lib/codetree/draw";
import { S0, buildModel, type Leaf, type Model, type RawFile, type RawRepo } from "../lib/codetree/model";
import { readPalette } from "../lib/codetree/palette";
import {
  SCAF,
  drawScaffold,
  liftAnchor,
  liftAt,
  liftBox,
  plankAnchor,
  plankAt,
  plankEnd,
  type Scaffold,
} from "../lib/codetree/scaffold";
import type { TreeView } from "../lib/codetree/types";
import fixtureRaw from "./fixtures/codetree_repo.json?raw";

const FX = JSON.parse(fixtureRaw) as RawRepo;
const REPO = buildModel({ name: FX.name, files: FX.files });

/** A repo whose non-code outweighs a small crown: a 150-doc folder, CI, root config — the towers must step out
 * from under the crown and stay tall and thin. */
function bigDocs(): RawRepo {
  const files: RawFile[] = [];
  for (const d of ["api", "core", "web", "cli"]) for (let i = 0; i < 18; i++) files.push({ p: `src/${d}/m${i}.py`, n: 40 + ((i * 37) % 400), k: "c" });
  for (let i = 0; i < 150; i++) files.push({ p: `docs/page-${i}.md`, n: 10 + ((i * 53) % 2000), k: "d" });
  for (let i = 0; i < 9; i++) files.push({ p: `.github/workflows/w${i}.yml`, n: 30 + i * 9, k: "d" });
  for (const f of ["README.md", "LICENSE", "pyproject.toml", "uv.lock", ".gitignore"]) files.push({ p: f, n: 120, k: "d" });
  return { name: "bigdocs", files };
}
/** A small repo with a tall trunk and a few docs: the scaffold stands right beside the trunk, tied to it. */
function smallDocs(): RawRepo {
  const files: RawFile[] = [];
  for (let i = 0; i < 6; i++) files.push({ p: `pkg/m${i}.py`, n: 80, k: "c" });
  for (const f of ["README.md", "LICENSE", "setup.cfg"]) files.push({ p: f, n: 60, k: "d" });
  files.push({ p: "docs/intro.md", n: 300, k: "d" }, { p: "docs/usage.md", n: 200, k: "d" });
  return { name: "small", files };
}
const BIG = buildModel(bigDocs());
const SMALL = buildModel(smallDocs());
const MODELS: Array<[string, Model]> = [
  ["repo fixture", REPO],
  ["150 docs", BIG],
  ["small repo", SMALL],
];

const groundLeaves = (M: Model) => M.leaves.filter((l) => l.region === "ground");
const plankBox = (l: Leaf) => {
  const p = l.plank!;
  return { x0: Math.min(p.xo, p.xi), x1: Math.max(p.xo, p.xi), y0: p.y - SCAF.plankT / 2, y1: p.y + SCAF.plankT / 2 };
};

describe.each(MODELS)("scaffold geometry (%s)", (_name, M) => {
  const SC = M.scaffold as Scaffold;

  it("has a pile node per ground group and a plank per ground file", () => {
    expect(M.piles.length).toBeGreaterThan(0);
    expect(SC.towers.length).toBeGreaterThan(0);
    expect(SC.towers.length).toBeLessThanOrEqual(2);
    let n = 0;
    for (const p of M.piles) {
      expect(p.kind).toBe("pile");
      expect(p.lift && p.lift.pile).toBe(p);
      expect(p.leaves!.length).toBe(p.nFiles);
      n += p.nFiles;
    }
    expect(groundLeaves(M).length).toBe(n);
    for (const l of groundLeaves(M)) {
      expect(l.plank, l.file.path).toBeDefined();
      expect(l.pile && l.pile.leaves!.includes(l)).toBe(true);
      expect(l.file.leaf).toBe(l);
    }
  });

  it("puts every plank inside its lift's box, and the node's cx / pw / h box holds them all", () => {
    for (const p of M.piles) {
      const B = liftBox(p);
      // the node's own fields describe the same box (framing, hit tests, the minimap use them)
      expect(p.cx - p.pw! / 2).toBeCloseTo(B.x0, 6);
      expect(p.cx + p.pw! / 2).toBeCloseTo(B.x1, 6);
      expect(p.cy - p.h! / 2).toBeCloseTo(B.y0, 6);
      expect(p.cy + p.h! / 2).toBeCloseTo(B.y1, 6);
      expect(p.F).toEqual([p.cx, p.cy]);
      for (const l of p.leaves!) {
        const b = plankBox(l);
        expect(b.x0, l.file.path).toBeGreaterThanOrEqual(B.x0 - 1e-6);
        expect(b.x1, l.file.path).toBeLessThanOrEqual(B.x1 + 1e-6);
        expect(b.y0, l.file.path).toBeGreaterThan(B.y0);
        expect(b.y1, l.file.path).toBeLessThan(B.y1);
        // the leaf's centre / length / angle describe the plank: its tip is the plank's outer (name) end
        const [ex, ey] = plankEnd(l);
        expect(l.x + Math.cos(l.ang) * l.len * 0.5).toBeCloseTo(ex, 6);
        expect(l.y + Math.sin(l.ang) * l.len * 0.5).toBeCloseTo(ey, 6);
      }
    }
  });

  it("never overlaps two planks of a lift, at any zoom (rows are a pitch apart, the boards thinner)", () => {
    expect(SCAF.plankT).toBeLessThan(SCAF.pitch);
    for (const p of M.piles) {
      const ys = p.leaves!.map((l) => l.plank!.y).sort((a, b) => a - b);
      for (let i = 1; i < ys.length; i++) expect(ys[i] - ys[i - 1]).toBeGreaterThanOrEqual(SCAF.pitch - 1e-9);
    }
    // world geometry scales with the camera: the screen gap between two boards is (pitch - plankT)·z > 0 at every z
  });

  it("lists a lift's files largest first under its name (top row = biggest)", () => {
    for (const p of M.piles) {
      const ls = p.leaves!.slice().sort((a, b) => a.plank!.y - b.plank!.y);
      for (let i = 1; i < ls.length; i++) expect(ls[i].file.lines).toBeLessThanOrEqual(ls[i - 1].file.lines);
      // a bigger file is a longer plank
      for (let i = 1; i < ls.length; i++) expect(ls[i].plank!.len).toBeLessThanOrEqual(ls[i - 1].plank!.len + 1e-9);
    }
  });

  it("stacks a tower's lifts without overlap, the biggest at the bottom, standing on the ground", () => {
    for (const T of SC.towers) {
      expect(T.y1).toBe(M.groundY);
      for (let i = 0; i < T.lifts.length; i++) {
        const L = T.lifts[i];
        expect(L.floor).toBeGreaterThan(L.top);
        expect(L.floor).toBeLessThan(M.groundY);
        if (i) {
          expect(L.floor).toBeLessThanOrEqual(T.lifts[i - 1].top + 1e-9); // above the lift below (a shared ledger)
          expect(L.files.length).toBeLessThanOrEqual(T.lifts[i - 1].files.length);
        }
      }
      expect(T.y0).toBeLessThan(T.lifts[T.lifts.length - 1].top);
    }
    // two towers: one on each side
    if (SC.towers.length === 2) expect(SC.towers[0].dir).toBe(-SC.towers[1].dir);
  });

  it("stands beside the trunk: clear of the trunk's width and of every crown leaf and branch", () => {
    for (const T of SC.towers) {
      const x0 = Math.min(T.xi, T.xo) - (T.dir < 0 ? SCAF.arm + SCAF.over : 0),
        x1 = Math.max(T.xi, T.xo) + (T.dir > 0 ? SCAF.arm + SCAF.over : 0);
      // on its own side, past the trunk's flare
      expect(Math.sign(T.xi)).toBe(T.dir);
      expect(Math.abs(T.xi)).toBeGreaterThanOrEqual(M.trunkW + SCAF.gap - 1e-9);
      for (const l of M.leaves) {
        if (l.region !== "crown") continue;
        const r = l.len / 2;
        const inside = l.x + r > x0 && l.x - r < x1 && l.y + r > T.y0 && l.y - r < T.y1;
        expect(inside, `${l.file.path} inside the ${T.dir < 0 ? "left" : "right"} tower`).toBe(false);
      }
      for (const b of M.branches) {
        if (b.region === "root") continue;
        for (const q of b.pts) {
          const inside = q[0] > x0 && q[0] < x1 && q[1] > T.y0 && q[1] < T.y1;
          expect(inside, `a branch of ${b.node ? b.node.path : "?"} through the tower`).toBe(false);
        }
      }
    }
  });

  it("anchors names by attachment: a plank's at its outer end (outward), a lift's on its top ledger's arm", () => {
    for (const p of M.piles) {
      const L = p.lift!,
        T = L.tower;
      const la = liftAnchor(p);
      expect(la.dir).toBe(T.dir);
      expect(la.y).toBe(L.top);
      // past the outer standard, away from the trunk
      expect((la.x - T.xo) * T.dir).toBeGreaterThan(0);
      for (const l of p.leaves!) {
        for (const z of [0.5, 2, 8]) {
          const a = plankAnchor(l, z);
          expect(a.dir).toBe(T.dir);
          expect(a.y).toBe(l.plank!.y);
          // 3 px past the outer end at every zoom
          expect((a.x - l.plank!.xo) * a.dir * z).toBeCloseTo(3, 6);
        }
        // the outer end is the end away from the trunk
        expect(Math.abs(l.plank!.xo)).toBeGreaterThan(Math.abs(l.plank!.xi));
      }
    }
  });

  it("hits a plank on its board and a lift anywhere in its box", () => {
    for (const p of M.piles) {
      for (const l of p.leaves!) {
        const pk = l.plank!;
        // the outer end, the middle and the inner end of the board all hit this plank
        for (const x of [pk.xo - pk.dir * 0.5, (pk.xo + pk.xi) / 2, pk.xi + pk.dir * 0.5]) expect(plankAt(M, x, pk.y, SCAF.pitch * 0.3)).toBe(l);
      }
      const B = liftBox(p);
      expect(liftAt(M, (B.x0 + B.x1) / 2, (B.y0 + B.y1) / 2)).toBe(p);
    }
    // the trunk and the open sky are no lift
    expect(liftAt(M, 0, M.groundY - S0)).toBeNull();
    expect(plankAt(M, 0, M.groundY - S0, S0)).toBeNull();
  });
});

describe("scaffold placement", () => {
  it("a small repo's tower stands right beside the trunk and is tied to it", () => {
    const T = (SMALL.scaffold as Scaffold).towers;
    expect(T.length).toBeGreaterThan(0);
    for (const t of T) {
      expect(t.tied).toBe(true);
      expect(Math.abs(t.xi) - SMALL.trunkW).toBeLessThan(SCAF.gap + S0 * 3);
    }
  });

  it("a tower is as tall as its files, not a pile as wide as them", () => {
    const docs = BIG.piles.find((p) => p.name === "docs")!;
    expect(docs.nFiles).toBe(150);
    expect(docs.h!).toBeGreaterThan(docs.pw! * 10);
    expect(docs.h!).toBeCloseTo(SCAF.floor + 150 * SCAF.pitch + SCAF.ceil, 6);
  });

  it("is deterministic (a replayed model has the same scaffold)", () => {
    const M2 = buildModel(bigDocs(), { replay: BIG.layoutRec });
    const a = (BIG.scaffold as Scaffold).towers.map((t) => [t.xi, t.y0, t.lifts.map((L) => [L.key, L.top])]);
    const b = (M2.scaffold as Scaffold).towers.map((t) => [t.xi, t.y0, t.lifts.map((L) => [L.key, L.top])]);
    expect(b).toEqual(a);
  });
});

// --- drawing: a recording 2D context (what was drawn, not pixels) ---
type Call = [string, unknown[]];
function recCtx(calls: Call[]): CanvasRenderingContext2D {
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
      return (...a: unknown[]) => {
        calls.push([k, a]);
      };
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
      createElement: () => ({ width: 1, height: 1, getContext: () => recCtx([]) }),
      documentElement: { classList: { contains: () => false } },
    };
}

describe("scaffold drawing", () => {
  beforeAll(installDom);
  const P = readPalette(null);
  const all = () => true;

  it("draws planks as boards (rects), never as leaf lenses, once rows are 2 px apart", () => {
    const M = BIG;
    const n = groundLeaves(M).length;
    for (const z of [0.5, 1, 2, 6]) {
      const calls: Call[] = [];
      drawScaffold(recCtx(calls), M, P, { z, leafPx: S0 * z, hasOnly: false, keepFiles: new Set(), createdGhost: new Set(), inView: all });
      expect(calls.some((c) => c[0] === "quadraticCurveTo" || c[0] === "bezierCurveTo" || c[0] === "ellipse"), `z=${z}`).toBe(false);
      const rects = calls.filter((c) => c[0] === "rect").length;
      if (SCAF.pitch * z >= 2.2) expect(rects, `z=${z}`).toBe(n);
      else expect(rects, `z=${z}: a lift is one block when its rows would merge`).toBe(0);
    }
  });

  function view(M: Model, cx: number, cy: number, z: number): TreeView {
    return {
      g: recCtx([]), M, P, agents: [], W: 1000, H: 700, dpr: 1, cam: { x: cx, y: cy, z }, zones: [], folded: new Set(), tool: "explore", t: 1000,
      playing: false, reducedMotion: true, hover: null, sel: null, infoNode: null, infoFile: null, pinBlast: null, hoverBlast: null, pulse: null,
      follow: null, markLeaf: null, blastCache: new Map(), hudRects: [], free: { x0: 0, y0: 0, x1: 1000, y1: 700 }, fitZv: 0.5,
      noCache: true,
    } as unknown as TreeView;
  }

  it("names a lift beside its top ledger, hanging under it, on the side away from the trunk", () => {
    const M = REPO;
    prepGeometry(M);
    for (const p of M.piles) {
      const B = liftBox(p);
      const z = 3;
      const v = view(M, (B.x0 + B.x1) / 2, (B.y0 + B.y1) / 2, z);
      render(v, 1000);
      const hit = (v.labelHits || []).find((h) => h.n === p);
      expect(hit, p.label).toBeDefined();
      const toS = v.toS!;
      const a = liftAnchor(p);
      const [ax, ay] = toS(a.x, a.y);
      const r = hit!.r;
      // hangs under the ledger line (within a couple of px), starts at the arm's end and runs outward
      expect(r[1]).toBeGreaterThanOrEqual(ay);
      expect(r[1] - ay).toBeLessThan(4);
      if (a.dir > 0) expect(Math.abs(r[0] - ax)).toBeLessThan(4);
      else expect(Math.abs(r[2] - ax)).toBeLessThan(4);
    }
  });

  it("picks the plank under the pointer zoomed in, the whole lift zoomed out", () => {
    const M = REPO;
    prepGeometry(M);
    const l = groundLeaves(M)[0];
    const pk = l.plank!;
    const mid = (pk.xo + pk.xi) / 2;
    // zoomed in (a leaf is 30 px): the plank itself
    let v = view(M, mid, pk.y, 3);
    render(v, 1000);
    expect(pick(v, 500, 350)).toEqual({ leaf: l });
    // zoomed out (a leaf is 5 px): the lift it is on
    v = view(M, mid, pk.y, 0.5);
    render(v, 1000);
    expect(pick(v, 500, 350)).toEqual({ pile: l.pile });
  });
});
