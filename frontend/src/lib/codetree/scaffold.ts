/** Code tree — the SCAFFOLD: the repo's non-code support (docs, CI, root
 * config, lockfiles, a mostly-non-code top-level folder) as a built structure
 * standing beside the trunk, not leaves on the ground.
 *
 * Up to two TOWERS (left / right of the trunk). Each tower is two standards
 * (poles) with a ledger (cross-bar) under every LIFT; a lift is one group
 * (today's "pile" TNode: a top-level folder, or the repo root's files), its
 * name hanging under its top ledger like a heading over a list; each file is a
 * PLANK laid across its lift, one per row, largest first under the name,
 * flush with the OUTER standard (its outer end is where its name goes) and as
 * long as the file is big (log of lines). The biggest group is the bottom
 * lift; a tower is as tall as its files; it stands as close to the trunk as
 * the crown above lets it (never under a clump).
 *
 * Pure geometry (`layoutScaffold`, the anchors, the hit tests) plus the static
 * drawing; model.ts builds the pile nodes and leaves from the geometry, draw.ts
 * calls in from its pile hooks. */

import { S0 } from "./layout";
import type { Leaf, Model, TFile, TNode } from "./model";
import type { Palette } from "./palette";

type G2 = CanvasRenderingContext2D;

/** World sizes (S0 = 10 = the crown's leaf spacing). */
export const SCAF = {
  /** one plank per row, this far apart */
  pitch: S0 * 0.62,
  /** plank thickness */
  plankT: S0 * 0.34,
  /** between the two standards */
  bayW: S0 * 4.2,
  /** a plank's outer end sticks out past the outer standard by this much */
  over: S0 * 0.35,
  /** from a lift's floor ledger up to its bottom plank */
  floor: S0 * 0.35,
  /** above a lift's top plank, under its top ledger: the band its name hangs in (a 15 px name fits from ~1.3 px/unit) */
  ceil: S0 * 1.15,
  /** the first ledger stands this far above the ground (the kickboard) */
  base: S0 * 0.6,
  /** the gap between the trunk and the inner standard (the trunk's flare is ~1 trunkW wide at the ground) */
  gap: S0 * 1.6,
  /** clear air kept between a tower (and its top) and the crown's leaves / wood */
  clear: S0 * 1.2,
  /** how far a lift's top ledger runs out past the outer standard: the arm its name hangs from */
  arm: S0 * 0.9,
};

export interface Plank {
  /** outer end (the name end) and inner end, x; row centre y */
  xo: number;
  xi: number;
  y: number;
  len: number;
  /** -1 = the left tower (outer end points left), +1 = the right one */
  dir: -1 | 1;
  lift: Lift;
}

export interface Lift {
  key: string;
  files: TFile[];
  planks: Plank[];
  tower: Tower;
  /** floor ledger y and the top ledger (the next lift's floor, or the tower top; the lift's name hangs from it) */
  floor: number;
  top: number;
  /** box holding the standards, the planks and their overhang */
  x0: number;
  x1: number;
  pile?: TNode;
}

export interface Tower {
  dir: -1 | 1;
  /** inner standard (trunk side) and outer standard x */
  xi: number;
  xo: number;
  /** ground and top of the standards */
  y1: number;
  y0: number;
  lifts: Lift[];
  /** ties to the trunk (the tower stands right beside it) */
  tied: boolean;
}

export interface Scaffold {
  towers: Tower[];
}

export interface ScaffoldGroup {
  key: string;
  /** largest first */
  files: TFile[];
}

/** What a tower must stay clear of: the crown's leaves (centre + reach) and wood (bbox). */
export interface Obstacles {
  pts: Array<[number, number, number]>;
  boxes: Array<[number, number, number, number]>;
}

const fzOf = (lines: number) => Math.min(1, Math.log10(Math.max(1, lines) + 1) / 3.6);
const liftH = (n: number) => SCAF.floor + n * SCAF.pitch + SCAF.ceil;
/** A plank's length: a quarter of the bay for an empty file, the whole bay plus the overhang for a big one. */
export const plankLen = (lines: number) => SCAF.bayW * (0.28 + 0.72 * fzOf(lines)) + SCAF.over;

/** Lay out the scaffold: groups (largest first) go to the shorter tower, so each tower's biggest lift is its
 * bottom one; each tower then steps out from the trunk until its whole height is clear of the crown. */
export function layoutScaffold(groups: ScaffoldGroup[], groundY: number, trunkW: number, obs: Obstacles): Scaffold {
  const sides: Array<{ dir: -1 | 1; H: number; gs: ScaffoldGroup[] }> = [
    { dir: -1, H: 0, gs: [] },
    { dir: 1, H: 0, gs: [] },
  ];
  for (const gr of groups) {
    const s = sides[0].H <= sides[1].H ? sides[0] : sides[1];
    s.gs.push(gr);
    s.H += liftH(gr.files.length);
  }
  const towers: Tower[] = [];
  const x0 = trunkW * 1.0 + SCAF.gap;
  for (const s of sides) {
    if (!s.gs.length) continue;
    const H = SCAF.base + s.H;
    const yTop = groundY - H - SCAF.clear;
    // the column the tower needs (standards + overhang + the name arm), clear of every crown leaf and branch
    const w = SCAF.bayW + SCAF.arm;
    const clearAt = (xi: number) => {
      const a = s.dir > 0 ? xi - SCAF.clear : xi - w - SCAF.clear,
        b = s.dir > 0 ? xi + w + SCAF.clear : xi + SCAF.clear;
      for (const [px, py, r] of obs.pts) if (px + r > a && px - r < b && py + r > yTop) return false;
      for (const [bx0, by0, bx1, by1] of obs.boxes) if (bx1 > a && bx0 < b && by1 > yTop && by0 < groundY) return false;
      return true;
    };
    let off = x0;
    for (let i = 0; i < 4000 && !clearAt(s.dir * off); i++) off += S0 * 0.5;
    const xi = s.dir * off,
      xo = xi + s.dir * SCAF.bayW;
    const T: Tower = { dir: s.dir, xi, xo, y1: groundY, y0: groundY - H - S0 * 0.5, lifts: [], tied: off - x0 < S0 * 3 };
    let floor = groundY - SCAF.base;
    for (const gr of s.gs) {
      const top = floor - liftH(gr.files.length);
      const L: Lift = { key: gr.key, files: gr.files, planks: [], tower: T, floor, top, x0: 0, x1: 0 };
      const xoEnd = xo + s.dir * SCAF.over;
      gr.files.forEach((f, r) => {
        const len = plankLen(f.lines);
        L.planks.push({ xo: xoEnd, xi: xoEnd - s.dir * len, y: top + SCAF.ceil + (r + 0.5) * SCAF.pitch, len, dir: s.dir, lift: L });
      });
      L.x0 = Math.min(xi, xoEnd);
      L.x1 = Math.max(xi, xoEnd);
      T.lifts.push(L);
      floor = top;
    }
    towers.push(T);
  }
  return { towers };
}

/** The crown's leaves and wood, as obstacles for the towers (call before the leaf lengths exist: a leaf reaches
 * at most ~0.6 S0 from its centre). */
export function crownObstacles(leaves: Array<{ x: number; y: number }>, branches: Array<{ x0: number; y0: number; x1: number; y1: number; cp: number[]; w: number; region: string }>): Obstacles {
  const pts: Obstacles["pts"] = leaves.map((l) => [l.x, l.y, S0 * 0.6]);
  const boxes: Obstacles["boxes"] = [];
  for (const b of branches) {
    if (b.region === "root") continue;
    let x0 = Math.min(b.x0, b.x1),
      x1 = Math.max(b.x0, b.x1),
      y0 = Math.min(b.y0, b.y1),
      y1 = Math.max(b.y0, b.y1);
    for (let i = 0; i + 1 < b.cp.length; i += 2) {
      x0 = Math.min(x0, b.cp[i]);
      x1 = Math.max(x1, b.cp[i]);
      y0 = Math.min(y0, b.cp[i + 1]);
      y1 = Math.max(y1, b.cp[i + 1]);
    }
    const r = (b.w || 0) * 0.5;
    boxes.push([x0 - r, y0 - r, x1 + r, y1 + r]);
  }
  return { pts, boxes };
}

/** The pile node's box (= its lift: standards, planks, overhang). */
export function liftBox(n: TNode): { x0: number; y0: number; x1: number; y1: number } {
  const L = n.lift;
  if (L) return { x0: L.x0, y0: L.top, x1: L.x1, y1: L.floor };
  const pw = n.pw || 0,
    h = n.h || 0;
  return { x0: n.cx - pw / 2, y0: n.cy - h / 2, x1: n.cx + pw / 2, y1: n.cy + h / 2 };
}

/** A plank's outer end (world): where its name attaches. */
export function plankEnd(l: Leaf): [number, number] {
  const p = l.plank;
  return p ? [p.xo, p.y] : [l.x + Math.cos(l.ang) * l.len * 0.5, l.y + Math.sin(l.ang) * l.len * 0.5];
}

/** Where a plank's name starts (world) at zoom z: 3 px past the outer end, text running outward (`dir`). */
export function plankAnchor(l: Leaf, z: number): { x: number; y: number; dir: -1 | 1 } {
  const p = l.plank;
  if (!p) return { x: l.x, y: l.y, dir: 1 };
  return { x: p.xo + (p.dir * 3) / z, y: p.y, dir: p.dir };
}

/** What a lift holds, for its caption: the NON-CODE files of a code folder that is also on the tree ("frontend · 3
 * non-code": its README / package.json / index.html — the code is the frontend branch), the data of a folder whose
 * code is underground ("test data · 42": fixtures, golden files — the tests are the roots), or a folder that is all
 * non-code ("docs · 21 files"). */
export type LiftKind = "code" | "tests" | "plain";
const LIFT_KIND = new WeakMap<TNode, LiftKind>();
export function liftKind(M: Pick<Model, "nodeOf" | "files">, n: TNode): LiftKind {
  const hit = LIFT_KIND.get(n);
  if (hit) return hit;
  const k = n.name;
  let kind: LiftKind = "plain";
  if (k !== "(repo root)") {
    const crown = M.nodeOf.get(k);
    if (crown && crown.kind === "crown" && crown.depth === 1) kind = "code";
    else if (M.files.some((f) => f.place === "root" && f.path.startsWith(k + "/"))) kind = "tests";
  }
  LIFT_KIND.set(n, kind);
  return kind;
}
/** A lift's caption as [name, count]: "frontend" + " · 3 non-code", "test data" + " · 42" (a folder named like
 * tests; any other says whose: "testsv2 test data"), "docs" + " · 21 files". */
export function liftCaption(M: Pick<Model, "nodeOf" | "files">, n: TNode): [string, string] {
  const kind = liftKind(M, n),
    N = n.nFiles;
  if (kind === "code") return [n.label || n.name, ` · ${N} non-code`];
  if (kind === "tests") return [/^(tests?|__tests__|specs?)$/.test(n.name) ? "test data" : `${n.label || n.name} test data`, ` · ${N}`];
  return [n.label || n.name, ` · ${N} file${N === 1 ? "" : "s"}`];
}

/** Where a lift's name hangs (world): at the outer end of its top ledger's arm, on the ledger line — the text
 * hangs UNDER it (top-aligned, a px or two below), over the lift's planks, and runs outward (`dir`). */
export function liftAnchor(n: TNode): { x: number; y: number; dir: -1 | 1 } {
  const L = n.lift;
  if (!L) return { x: n.cx, y: n.cy, dir: 1 };
  const T = L.tower;
  return { x: T.xo + T.dir * SCAF.arm, y: L.top, dir: T.dir };
}

/** Distance from (x, y) to a plank's board (0 inside). */
export function plankDist(l: Leaf, x: number, y: number): number {
  const p = l.plank;
  if (!p) return Math.hypot(l.x - x, l.y - y);
  const a = Math.min(p.xo, p.xi),
    b = Math.max(p.xo, p.xi);
  const dx = x < a ? a - x : x > b ? x - b : 0,
    dy = Math.max(0, Math.abs(y - p.y) - SCAF.plankT / 2);
  return Math.hypot(dx, dy);
}

/** The plank under (x, y) within `tol` (world), the nearest one. */
export function plankAt(M: Model, x: number, y: number, tol: number): Leaf | null {
  let best: Leaf | null = null,
    bd = tol;
  for (const p of M.piles) {
    const B = liftBox(p);
    if (x < B.x0 - tol || x > B.x1 + tol || y < B.y0 - tol || y > B.y1 + tol) continue;
    for (const l of p.leaves || []) {
      const d = plankDist(l, x, y);
      if (d <= bd) {
        bd = d;
        best = l;
      }
    }
  }
  return best;
}

/** The lift whose box (plus the name arm) holds (x, y). */
export function liftAt(M: Model, x: number, y: number, tol = 0): TNode | null {
  for (const p of M.piles) {
    const B = liftBox(p),
      L = p.lift,
      arm = L ? SCAF.arm : 0;
    const x0 = B.x0 - (L && L.tower.dir < 0 ? arm : 0),
      x1 = B.x1 + (L && L.tower.dir > 0 ? arm : 0);
    if (x >= x0 - tol && x <= x1 + tol && y >= B.y0 - tol && y <= B.y1 + tol) return p;
  }
  return null;
}

/** A plank's board as a path (world): what leafPath draws for a ground leaf (fills, edits, gold). */
export function plankPath(g: G2 | Path2D, l: Leaf): void {
  const p = l.plank;
  if (!p) return;
  const a = Math.min(p.xo, p.xi),
    t = SCAF.plankT;
  g.rect(a, p.y - t / 2, p.len, t);
}

/** A ring around a plank (selection, hover, marks): a rectangle `pad` (world) clear of the board. */
export function plankRing(g: G2, l: Leaf, pad: number): void {
  const p = l.plank;
  if (!p) return;
  const a = Math.min(p.xo, p.xi) - pad,
    t = SCAF.plankT + pad * 2;
  g.rect(a, p.y - t / 2, p.len + pad * 2, t);
}

/** The size a mark (read dot, bud) on a plank is drawn for: a plank is long and thin, a mark is not. */
export const PLANK_MARK = S0 * 0.62;

const fade = (a: number, b: number, x: number) => Math.max(0, Math.min(1, (x - a) / (b - a)));

export interface ScafDraw {
  z: number;
  leafPx: number;
  hasOnly: boolean;
  keepFiles: Set<number>;
  createdGhost: Set<number>;
  inView: (x0: number, y0: number, x1: number, y1: number) => boolean;
}

/** The static layer: the frame (standards, ledgers, braces, ties) as a light lattice at every zoom, the planks
 * fading in with the leaves (10..16 px a leaf), a faint fill per lift until they do. Unlit (only-here dusk)
 * lifts go dusk, keep-out ones red (the hatch is drawn by draw.ts over `liftBox`). */
export function drawScaffold(g: G2, M: Model, P: Palette, o: ScafDraw): number {
  const SC = M.scaffold as Scaffold | undefined;
  if (!SC) return 0;
  const z = o.z;
  const C = P.SCAF;
  let shown = 0;
  const lw = Math.max(1 / z, Math.min(S0 * 0.24, 2.6 / z));
  // planks are slats at every zoom where a row is still 2 px apart (fainter until the leaves show, 10..16 px a
  // leaf); further out a lift is one faint block
  const slats = SCAF.pitch * z >= 2.2;
  const plankA = slats ? 0.7 + 0.3 * fade(10, 16, o.leafPx) : 0;
  for (const T of SC.towers) {
    const bx0 = Math.min(T.xi, T.xo) - SCAF.arm - SCAF.over,
      bx1 = Math.max(T.xi, T.xo) + SCAF.arm + SCAF.over;
    if (!o.inView(bx0, T.y0, bx1, T.y1)) continue;
    shown++;
    const dim = (L: Lift) => o.hasOnly && !(L.pile && L.pile.lit);
    // lift fills: the size of each lift reads before its planks do
    if (!slats)
      for (const L of T.lifts) {
        const keep = L.pile && L.pile.keep;
        g.fillStyle = dim(L) ? P.DUSK.leafA : keep ? P.KEEP.leafA : C.fill;
        g.globalAlpha = keep ? 0.5 : 1;
        g.fillRect(Math.min(T.xi, T.xo), L.top + SCAF.ceil * 0.6, SCAF.bayW, L.floor - L.top - SCAF.ceil * 0.6 - SCAF.floor * 0.5);
      }
    g.globalAlpha = 1;
    // braces: one diagonal per lift, zig-zagging up the tower (behind the planks)
    g.strokeStyle = C.brace;
    g.lineWidth = lw * 0.7;
    g.beginPath();
    T.lifts.forEach((L, i) => {
      const [xa, xb] = i % 2 ? [T.xo, T.xi] : [T.xi, T.xo];
      g.moveTo(xa, L.floor);
      g.lineTo(xb, L.top);
    });
    g.stroke();
    // ties: the tower holds on to the trunk (only when it stands right beside it)
    if (T.tied) {
      g.strokeStyle = C.brace;
      g.lineWidth = lw * 0.8;
      g.beginPath();
      for (const L of T.lifts) {
        g.moveTo(T.xi, L.top);
        g.lineTo(T.dir * M.trunkW * 0.5, L.top);
      }
      g.stroke();
    }
    // planks
    if (plankA > 0) {
      const groups = new Map<string, Leaf[]>();
      for (const L of T.lifts) {
        if (!L.pile) continue;
        const keep = L.pile.keep,
          d = dim(L);
        for (const l of L.pile.leaves || []) {
          if (l.file.ghost && !o.createdGhost.has(l.file.id)) continue;
          const col = d ? P.DUSK.leafA : keep || o.keepFiles.has(l.file.id) ? P.KEEP.leafA : l.file.id % 3 ? C.plankA : C.plankB; // a board in three a shade darker
          let a = groups.get(col);
          if (!a) groups.set(col, (a = []));
          a.push(l);
        }
      }
      g.globalAlpha = plankA;
      for (const [col, arr] of groups) {
        g.fillStyle = col;
        g.beginPath();
        for (const l of arr) plankPath(g, l);
        g.fill();
      }
      g.globalAlpha = 1;
    }
    // standards + ledgers on top: the frame holds the planks
    g.strokeStyle = C.pole;
    g.lineWidth = lw;
    g.lineCap = "round";
    g.beginPath();
    g.moveTo(T.xi, T.y1);
    g.lineTo(T.xi, T.y0);
    g.moveTo(T.xo, T.y1);
    g.lineTo(T.xo, T.y0);
    // a ledger under every lift, and each lift's top ledger runs out into the arm its name hangs from
    for (const L of T.lifts) {
      g.moveTo(T.xi, L.floor);
      g.lineTo(T.xo, L.floor);
      g.moveTo(T.xi, L.top);
      g.lineTo(T.xo + T.dir * SCAF.arm, L.top);
    }
    g.stroke();
    // base plates
    g.fillStyle = C.pole;
    const bp = SCAF.bayW * 0.12;
    g.fillRect(T.xi - bp, T.y1 - lw, bp * 2, lw * 1.6);
    g.fillRect(T.xo - bp, T.y1 - lw, bp * 2, lw * 1.6);
    g.lineCap = "butt";
  }
  return shown;
}

/** The minimap: each tower's frame and its lifts as small filled blocks. */
export function drawScaffoldMini(g: G2, M: Model, P: Palette, z: number): void {
  const SC = M.scaffold as Scaffold | undefined;
  if (!SC) return;
  for (const T of SC.towers) {
    g.fillStyle = P.SCAF.fill;
    for (const L of T.lifts) g.fillRect(Math.min(T.xi, T.xo), L.top + Math.max(1 / z, SCAF.ceil * 0.5), SCAF.bayW, L.floor - L.top - Math.max(1 / z, SCAF.ceil * 0.5));
    g.strokeStyle = P.SCAF.pole;
    g.lineWidth = Math.max(1 / z, 1);
    g.beginPath();
    g.moveTo(T.xi, T.y1);
    g.lineTo(T.xi, T.y0);
    g.moveTo(T.xo, T.y1);
    g.lineTo(T.xo, T.y0);
    g.stroke();
  }
}


/** A ring around a whole lift (a selected / hovered / zoned pile): its box, `pad` (world) clear of it. */
export function liftRing(g: G2, n: TNode, pad: number): void {
  const B = liftBox(n);
  g.rect(B.x0 - pad, B.y0 - pad, B.x1 - B.x0 + pad * 2, B.y1 - B.y0 + pad * 2);
}
