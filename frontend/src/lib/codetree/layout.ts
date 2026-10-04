/** Code tree — the botanical packing layout (the search).
 *
 * A folder with no sub-folders is a round clump of leaves at the tip of its own
 * twig. A folder with sub-folders is a fork: its items (its own files as one
 * clump, then each sub-folder) are combined pairwise into a balanced binary
 * fan. Every subtree is built bottom-up as a SHAPE in its own frame (origin =
 * where the branch that reaches it ends, growth = up) and placed rigidly: the
 * heavier child continues, the lighter one leans out to its side, and each is
 * pushed out from the fork only until neither its foliage nor the curved stem
 * that reaches it touches anything already placed. Clearances are the overlap
 * harness's own criteria plus a little air, so wood never crosses foreign
 * foliage, sibling foliage never touches and branches never cross — by
 * construction. A final pass slides every subtree toward the crown's heart
 * wherever the whole tree leaves air.
 *
 * Ported from the approved prototype (mindflock-prototypes/code-tree/model.js).
 * The geometry and the cost model are the prototype's; what changed is speed
 * (the prototype's search took ~43 s on a 4.8k-file repo; this one ~2.6 s):
 *  - the collision grid is a dense, growable window of cell lists (no hashing
 *    per lookup), and dead primitives are purged from it during compaction;
 *  - every primitive carries its bounding box, so a pair whose boxes are apart
 *    by more than the clearance is rejected before any distance math;
 *  - a coarse occupancy count per grid skips a probe — and a whole spatial
 *    bucket of a moving subtree — that has nothing it could hit nearby;
 *  - a compaction record that found no move is not re-evaluated until
 *    something changed inside the region its candidates reached;
 *  - probe stems are pooled instead of allocated per step.
 * None of these changes which blocker is found first, so the result is the
 * prototype's result bit for bit (vitest checks it against a recorded run).
 *
 * Re-layouts: a warm build (model.ts) reuses the previous build's FINAL
 * placement of every fork it can identify (a folder's top fork by its path,
 * an inner fork by the range of items it pairs), keeps the previous fan
 * splits and top-level order while still nearly balanced, and only pushes a
 * changed subtree that now overlaps out of the way (pkCompact's resolution
 * mode) — so a small change doesn't reshuffle the tree. A record of the
 * placements also replays exactly (cache hits).
 *
 * Pure: no DOM. Runs in the layout worker and (for replays) on the main
 * thread. Module-level state (PK_*) is per build; a build is synchronous. */

export const S0 = 10; // nominal leaf spacing (world units)
export const TAU = Math.PI * 2;

export function rng(seed: number): () => number {
  let s = seed >>> 0 || 1;
  return () => {
    s ^= s << 13;
    s >>>= 0;
    s ^= s >> 17;
    s ^= s << 5;
    s >>>= 0;
    return s / 4294967296;
  };
}
export function hashStr(str: string): number {
  let h = 2166136261;
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}
/** A short, collision-safe digest (two 32-bit FNV variants). */
export function h2(str: string): string {
  let a = 2166136261,
    b = 0x811c9dc5 ^ 0x5bd1e995;
  for (let i = 0; i < str.length; i++) {
    const c = str.charCodeAt(i);
    a ^= c;
    a = Math.imul(a, 16777619);
    b = Math.imul(b ^ c, 0x01000193) + (b >>> 13);
  }
  return (a >>> 0).toString(36) + (b >>> 0).toString(36);
}
/** Locale-independent ordering: the layout must not depend on the viewer's
 * system locale. */
export const cmpStr = (a: string, b: string) => (a < b ? -1 : a > b ? 1 : 0);

// --- Primitives -------------------------------------------------------------

/** A collision primitive: k 0 = a clump's foliage disc, k 1 = a wood segment.
 * One shape for both keeps V8's property access monomorphic. */
export interface Prim {
  k: 0 | 1;
  // disc
  x: number;
  y: number;
  rho: number;
  t: Term | null;
  // segment
  x0: number;
  y0: number;
  x1: number;
  y1: number;
  hw: number;
  sf: number;
  ef: number;
  term: Term | null;
  // bounding box (padded by nothing; the clearance is added at test time)
  bx0: number;
  by0: number;
  bx1: number;
  by1: number;
  /** distance from the shape's origin (sort key) */
  d: number;
  /** compaction: owning record index; dead = a stale copy */
  sub: number;
  dead: boolean;
  m: number;
  st: number;
}

function mkDisc(x: number, y: number, rho: number, t: Term | null): Prim {
  return {
    k: 0, x, y, rho, t, x0: 0, y0: 0, x1: 0, y1: 0, hw: 0, sf: 0, ef: 0, term: null,
    bx0: x - rho, by0: y - rho, bx1: x + rho, by1: y + rho, d: 0, sub: -1, dead: false, m: 0, st: 0,
  };
}
function mkSeg(x0: number, y0: number, x1: number, y1: number, hw: number, sf: number, ef: number, term: Term | null): Prim {
  return {
    k: 1, x: 0, y: 0, rho: 0, t: null, x0, y0, x1, y1, hw, sf, ef, term,
    bx0: (x0 < x1 ? x0 : x1) - hw, by0: (y0 < y1 ? y0 : y1) - hw, bx1: (x0 > x1 ? x0 : x1) + hw, by1: (y0 > y1 ? y0 : y1) + hw,
    d: 0, sub: -1, dead: false, m: 0, st: 0,
  };
}
/** Overwrite a pooled probe segment in place. */
function setSeg(q: Prim, x0: number, y0: number, x1: number, y1: number, hw: number, sf: number, ef: number, term: Term | null) {
  q.x0 = x0;
  q.y0 = y0;
  q.x1 = x1;
  q.y1 = y1;
  q.hw = hw;
  q.sf = sf;
  q.ef = ef;
  q.term = term;
  q.bx0 = (x0 < x1 ? x0 : x1) - hw;
  q.by0 = (y0 < y1 ? y0 : y1) - hw;
  q.bx1 = (x0 > x1 ? x0 : x1) + hw;
  q.by1 = (y0 > y1 ? y0 : y1) + hw;
}
const mkPool = (n: number) => Array.from({ length: n }, () => mkSeg(0, 0, 0, 0, 0, 0, 0, null));
const SWEEP_POOL = mkPool(5);
const LOC_POOL = mkPool(1)[0];
const COMPACT_POOL = mkPool(5);
function clonePrim(p: Prim, ox: number, oy: number): Prim {
  return p.k === 0
    ? mkDisc(p.x + ox, p.y + oy, p.rho, p.t)
    : mkSeg(p.x0 + ox, p.y0 + oy, p.x1 + ox, p.y1 + oy, p.hw, p.sf, p.ef, p.term);
}

// --- The layout's view of the folder tree -----------------------------------

/** What the layout needs of a folder node (model.ts fills the rest). */
export interface LNode {
  name: string;
  path: string;
  parent: LNode | null;
  kids: LNode[];
  files: LFile[];
  depth: number;
  term?: Term;
  F?: [number, number];
  branch?: unknown;
  w?: number;
}
export interface LFile {
  lines: number;
  ghost?: boolean;
}

/** A clump: its files on a jittered hex grid around the clump centre. */
export interface Term {
  own: boolean;
  node: LNode;
  files: LFile[];
  s: number;
  region: string;
  loc: Array<[number, number]>;
  R: number;
  rho: number;
  cy0: number;
  /** filled by emit */
  leaves?: Array<{ x: number; y: number; file: LFile }>;
  F?: [number, number];
  branch?: unknown;
  [k: string]: unknown;
}

export interface Placement {
  S: Shape;
  Ex: number;
  Ey: number;
  rho: number;
  f: number;
  c: number[];
  mo?: Moments;
  stem?: Prim[];
}

interface Moments {
  sx: number;
  sy: number;
  sxx: number;
  syy: number;
  sxy: number;
}

export interface Shape extends Moments {
  kind: "leaf" | "own" | "node" | "virtual";
  node?: LNode;
  owner?: LNode;
  term: Term | null;
  fork: number | null;
  efId: number;
  prims: Prim[] | null;
  place: Placement[];
  m: number;
  area: number;
  r0: number;
  r1: number;
  lines: number;
  files: number;
  w: number;
  wb: number;
  we: number;
  rad: number;
  grid: Grid | null;
  rc: Map<number, Prim[]> | null;
  base?: Prim[];
  /** subtree CONTENT signature (memo key): equal sig = identical shape */
  sig: string;
  /** subtree STRUCTURE key (which folders, which fork): stable across edits */
  skey: string;
  /** warm build: this fork reused its previous final placement */
  fromWarm?: boolean;
  /** warm build: its content differs from the previous build's */
  changed?: boolean;
}

// --- Knobs -------------------------------------------------------------------

export interface Knobs {
  ax: number;
  ay: number;
  down: number;
  heart: number;
  lim: number;
  stemR: boolean;
  rtop: number;
  cmax: number;
  leader: number;
  leaderOrder: "mid" | "asc" | "desc";
  cwx: number;
  cwy: number;
  flat: number;
  flatA: number;
  trunk?: number;
  cheart?: number;
}

export const PK = {
  gapTT: 0.8,
  gapTW: 1.6,
  gapWW: 1.8, // air between foliage/foliage, foliage/wood, wood/wood
  CS: 16,
  ax: 1,
  ay: 1,
  down: 0.4,
  heart: 1,
  compact: 6, // compaction passes
  cstep: 0.07, // compaction direction step
  stemR: false,
  rtop: 9,
  cmax: 9,
  leader: 0,
  leaderOrder: "mid" as "mid" | "asc" | "desc",
  cwx: 1,
  cwy: 1,
  flat: 0,
  flatA: 0.8,
};

/** The crown's shape knobs; a big crown (thousands of files) compacts harder
 * sideways than up — without it the global slide toward the crown's heart
 * drags every limb down flat and the tree reads as a wide umbrella. */
export const CROWN: Knobs = { ax: 1, ay: 1, down: 0.4, heart: 1, lim: 1.4, stemR: false, rtop: 9, cmax: 9, trunk: 0.42, leader: 0, leaderOrder: "mid", cheart: 1, cwx: 1, cwy: 1, flat: 0, flatA: 0.8 };
export const CROWN_BIG = { min: 2500, cwx: 2.5, cwy: 0.5, cmax: 1.1 };
export const ROOTS: Knobs = { ax: 1, ay: 2.2, down: 0, heart: 0.5, lim: 1.4, stemR: false, rtop: 9, cmax: 9, leader: 0, leaderOrder: "mid", cwx: 1, cwy: 1, flat: 0, flatA: 0.8 };

/** How far a child leans with its direction, and how far any clump may lean
 * from upright overall. */
const LEAN = { k: 0.6, lim: 1.4 };

export function pkUse(P: Knobs) {
  PK.ax = P.ax;
  PK.ay = P.ay;
  PK.down = P.down;
  PK.heart = P.heart;
  LEAN.lim = P.lim;
  PK.stemR = P.stemR;
  PK.rtop = P.rtop;
  PK.cmax = P.cmax;
  PK.leader = P.leader;
  PK.leaderOrder = P.leaderOrder;
  PK.cwx = P.cwx;
  PK.cwy = P.cwy;
  PK.flat = P.flat;
  PK.flatA = P.flatA;
}

// --- Build state ------------------------------------------------------------

/** One fork's two placements: [Ex, Ey, rho, f] × 2. */
export type UnionRec = number[];

export interface BuildCtx {
  kW: number;
  /** placements replayed in build order (an exact replay of a recorded run) */
  cache: UnionRec[] | null;
  ci: number;
  /** the forks of this build, in build order (their placements are read at the
   * end, after compaction moved them) */
  unions: Shape[];
  /** bottom-up memo: union signature -> placements (a deterministic search) */
  memo: Map<string, UnionRec>;
  memoHits: number;
  /** warm build: structure key -> the previous build's FINAL placements and
   * content signature */
  final: Map<string, { u: UnionRec; sig: string }> | null;
  /** warm build: the overlap resolution failed (the caller builds cold) */
  warmFailed: boolean;
  /** warm build: forks the resolution moved */
  resolved: number;
  /** fan splits: (owner, first, last) key -> skey of the first right item */
  splits: Map<string, string>;
  prevSplits: Map<string, string> | null;
  /** top-level item order per region (skeys) */
  orders: Map<string, string[]>;
  prevOrders: Map<string, string[]> | null;
  /** every shape signature built (recorded for the next warm start) */
  sigs: string[];
  /** warm build: every shape signature of the previous build */
  prevSigs: Set<string> | null;
  passes: number;
  /** quantise widths (~3 % log steps): small size edits keep every shape */
  quantise: boolean;
  /** 0 = bottom-up search, 1 = compaction (progress reporting) */
  phase: number;
  /** progress callback (fraction 0..1 of the expected work) */
  tick?: (k: number) => void;
  work: number;
  workTotal: number;
  stats: { hits: number; pens: number; sweeps: number };
  passLog?: Array<[string, number, number, number]>;
}

let PK_FORK = 0;
let PK_STAMP = 0;
let PK_IG0 = -1;
let PK_IG1 = -1;
let PK_ONLY = false;
let CTX: BuildCtx;

export function newCtx(kW: number): BuildCtx {
  return {
    kW, cache: null, ci: 0, unions: [], memo: new Map(), memoHits: 0, final: null, warmFailed: false, resolved: 0, prevSigs: null, sigs: [], splits: new Map(), prevSplits: null, orders: new Map(), prevOrders: null, passes: PK.compact, quantise: false, phase: 0,
    work: 0, workTotal: 1, stats: { hits: 0, pens: 0, sweeps: 0 },
  };
}
/** The placements of every fork, as they stand now (post-compaction). */
export function unionRecs(ctx: BuildCtx): UnionRec[] {
  return ctx.unions.map((U) => U.place.flatMap((p) => [p.Ex, p.Ey, p.rho, p.f]));
}
/** Every shape signature of a build (clumps and forks). */
export function shapeSigs(ctx: BuildCtx): string[] {
  return ctx.sigs;
}
/** Structure key -> final placements + content signature, for a warm start. */
export function finalRecs(ctx: BuildCtx): Array<[string, UnionRec, string]> {
  return ctx.unions.map((U) => [U.skey, U.place.flatMap((p) => [p.Ex, p.Ey, p.rho, p.f]), U.sig]);
}
export function beginBuild(ctx: BuildCtx) {
  CTX = ctx;
  PK_FORK = 0;
}

// --- Spatial hash -----------------------------------------------------------

/** A uniform grid of cells holding primitive lists, stored as a dense window
 * that grows to cover whatever is pushed (a cell outside it is empty). Lists
 * keep insertion order: the blocker found first decides the sweep step, so the
 * order is part of the result. */
export class Grid {
  i0 = 0;
  j0 = 0;
  ni = 0;
  nj = 0;
  lists: Array<Prim[] | undefined> = [];
  occ: Occ;
  constructor(_cap = 256) {
    this.occ = new Occ();
  }
  /** grow the window to hold cells [i0..i1] x [j0..j1] */
  reserve(i0: number, j0: number, i1: number, j1: number) {
    if (this.ni && i0 >= this.i0 && j0 >= this.j0 && i1 < this.i0 + this.ni && j1 < this.j0 + this.nj) return;
    let a0 = i0,
      b0 = j0,
      a1 = i1,
      b1 = j1;
    if (this.ni) {
      a0 = Math.min(a0, this.i0);
      b0 = Math.min(b0, this.j0);
      a1 = Math.max(a1, this.i0 + this.ni - 1);
      b1 = Math.max(b1, this.j0 + this.nj - 1);
      // grow with slack so a slowly widening pattern doesn't realloc every push
      const mi = Math.max(8, (a1 - a0 + 1) >> 1),
        mj = Math.max(8, (b1 - b0 + 1) >> 1);
      if (i0 < this.i0) a0 -= mi;
      if (i1 >= this.i0 + this.ni) a1 += mi;
      if (j0 < this.j0) b0 -= mj;
      if (j1 >= this.j0 + this.nj) b1 += mj;
    }
    const ni = a1 - a0 + 1,
      nj = b1 - b0 + 1;
    const lists: Array<Prim[] | undefined> = new Array(ni * nj);
    for (let i = 0; i < this.ni; i++)
      for (let j = 0; j < this.nj; j++) {
        const v = this.lists[i * this.nj + j];
        if (v) lists[(i + this.i0 - a0) * nj + (j + this.j0 - b0)] = v;
      }
    this.i0 = a0;
    this.j0 = b0;
    this.ni = ni;
    this.nj = nj;
    this.lists = lists;
  }
  /** drop dead prims from every list (they are skipped anyway: exact) */
  purge() {
    const L = this.lists;
    for (let k = 0; k < L.length; k++) {
      const a = L[k];
      if (!a) continue;
      let w = 0;
      for (let r = 0; r < a.length; r++) if (!a[r].dead) a[w++] = a[r];
      if (w === 0) L[k] = undefined;
      else a.length = w;
    }
  }
  push(i: number, j: number, p: Prim) {
    if (!this.ni || i < this.i0 || j < this.j0 || i >= this.i0 + this.ni || j >= this.j0 + this.nj) this.reserve(i, j, i, j);
    const k = (i - this.i0) * this.nj + (j - this.j0);
    const a = this.lists[k];
    if (!a) this.lists[k] = [p];
    else if (a[a.length - 1] !== p) a.push(p);
  }
}

/** Coarse occupancy: how many live primitives' (padded) boxes touch each
 * coarse cell. A probe whose coarse cells hold nothing it could collide with
 * cannot hit — an exact skip, so the first-hit order (and the result) is the
 * same as testing it. Counts, not flags: compaction kills and re-adds prims. */
export class Occ {
  i0 = 0;
  j0 = 0;
  ni = 0;
  nj = 0;
  v: Int32Array = new Int32Array(0);
  constructor(_cap = 0) {}
  get(i: number, j: number): number {
    const a = i - this.i0,
      b = j - this.j0;
    return a < 0 || b < 0 || a >= this.ni || b >= this.nj ? 0 : this.v[a * this.nj + b];
  }
  add(i: number, j: number, d: number) {
    if (!this.ni || i < this.i0 || j < this.j0 || i >= this.i0 + this.ni || j >= this.j0 + this.nj) {
      let a0 = i,
        b0 = j,
        a1 = i,
        b1 = j;
      if (this.ni) {
        a0 = Math.min(a0, this.i0);
        b0 = Math.min(b0, this.j0);
        a1 = Math.max(a1, this.i0 + this.ni - 1);
        b1 = Math.max(b1, this.j0 + this.nj - 1);
        const mi = Math.max(4, (a1 - a0 + 1) >> 1),
          mj = Math.max(4, (b1 - b0 + 1) >> 1);
        if (i < this.i0) a0 -= mi;
        if (i >= this.i0 + this.ni) a1 += mi;
        if (j < this.j0) b0 -= mj;
        if (j >= this.j0 + this.nj) b1 += mj;
      } else {
        a0 -= 4;
        b0 -= 4;
        a1 += 4;
        b1 += 4;
      }
      const ni = a1 - a0 + 1,
        nj = b1 - b0 + 1;
      const v = new Int32Array(ni * nj);
      for (let x = 0; x < this.ni; x++)
        for (let y = 0; y < this.nj; y++) {
          const c = this.v[x * this.nj + y];
          if (c) v[(x + this.i0 - a0) * nj + (y + this.j0 - b0)] = c;
        }
      this.i0 = a0;
      this.j0 = b0;
      this.ni = ni;
      this.nj = nj;
      this.v = v;
    }
    this.v[(i - this.i0) * this.nj + (j - this.j0)] += d;
  }
}
const OCC_CS = 32;
/** coarse box padding: half the largest gap, plus float slack */
const OCC_PAD = 1.5;
function occBox(p: Prim, ox: number, oy: number, out: number[]) {
  const x0 = p.k === 0 ? p.x - p.rho : p.bx0,
    y0 = p.k === 0 ? p.y - p.rho : p.by0,
    x1 = p.k === 0 ? p.x + p.rho : p.bx1,
    y1 = p.k === 0 ? p.y + p.rho : p.by1;
  out[0] = Math.floor((x0 + ox - OCC_PAD) / OCC_CS);
  out[1] = Math.floor((y0 + oy - OCC_PAD) / OCC_CS);
  out[2] = Math.floor((x1 + ox + OCC_PAD) / OCC_CS);
  out[3] = Math.floor((y1 + oy + OCC_PAD) / OCC_CS);
}
const OB = [0, 0, 0, 0];
export function occAdd(o: Occ, p: Prim, d: number) {
  occBox(p, 0, 0, OB);
  for (let i = OB[0]; i <= OB[2]; i++) for (let j = OB[1]; j <= OB[3]; j++) o.add(i, j, d);
}
/** The current compaction record's own subtree (live prims), or null. */
let OWN: Occ | null = null;
/** Could p (offset) collide with anything pkHit would consider in G? */
function occMay(G: Grid, p: Prim, ox: number, oy: number): boolean {
  occBox(p, ox, oy, OB);
  return occMayCells(G.occ);
}
function occMayBox(G: Grid, x0: number, y0: number, x1: number, y1: number): boolean {
  OB[0] = Math.floor((x0 - OCC_PAD) / OCC_CS);
  OB[1] = Math.floor((y0 - OCC_PAD) / OCC_CS);
  OB[2] = Math.floor((x1 + OCC_PAD) / OCC_CS);
  OB[3] = Math.floor((y1 + OCC_PAD) / OCC_CS);
  return occMayCells(G.occ);
}
function occMayCells(o: Occ): boolean {
  const i0 = Math.max(OB[0], o.i0),
    i1 = Math.min(OB[2], o.i0 + o.ni - 1),
    j0 = Math.max(OB[1], o.j0),
    j1 = Math.min(OB[3], o.j0 + o.nj - 1);
  const nj = o.nj,
    v = o.v;
  const own = OWN;
  for (let i = i0; i <= i1; i++) {
    const row = (i - o.i0) * nj - o.j0;
    for (let j = j0; j <= j1; j++) {
      const c = v[row + j];
      if (own) {
        const w = own.get(i, j);
        if (PK_ONLY ? w > 0 : c - w > 0) return true;
      } else if (c > 0) return true;
    }
  }
  // (the own subtree's prims are live prims of G, so its cells lie inside G's window)
  return false;
}

// small-integer keys: (i, j) in ±32k
const pkKey = (i: number, j: number) => ((i + 32768) << 16) | ((j + 32768) & 0xffff);

/** A primitive's cells, appended to `out` as [i0, j0, i1, j1] quads: discs by
 * bounding box, segments in chunks so a long limb does not claim a huge box. */
function pkSpan(p: Prim, ox: number, oy: number, out: number[]): number[] {
  const CS = PK.CS;
  out.length = 0;
  if (p.k === 0) {
    const r = p.rho + 2,
      x = p.x + ox,
      y = p.y + oy;
    out.push(Math.floor((x - r) / CS), Math.floor((y - r) / CS), Math.floor((x + r) / CS), Math.floor((y + r) / CS));
    return out;
  }
  const r = p.hw + 2,
    ax = p.x0 + ox,
    ay = p.y0 + oy,
    bx = p.x1 + ox,
    by = p.y1 + oy;
  const n = Math.max(1, Math.ceil(Math.hypot(bx - ax, by - ay) / (CS * 3)));
  for (let k = 0; k < n; k++) {
    const x0 = ax + ((bx - ax) * k) / n,
      y0 = ay + ((by - ay) * k) / n,
      x1 = ax + ((bx - ax) * (k + 1)) / n,
      y1 = ay + ((by - ay) * (k + 1)) / n;
    out.push(
      Math.floor((Math.min(x0, x1) - r) / CS),
      Math.floor((Math.min(y0, y1) - r) / CS),
      Math.floor((Math.max(x0, x1) + r) / CS),
      Math.floor((Math.max(y0, y1) + r) / CS)
    );
  }
  return out;
}
const PK_SPAN: number[] = [];
export function pkAdd(G: Grid, p: Prim) {
  occAdd(G.occ, p, 1);
  const sp = pkSpan(p, 0, 0, PK_SPAN);
  for (let s = 0; s < sp.length; s += 4) {
    G.reserve(sp[s], sp[s + 1], sp[s + 2], sp[s + 3]);
    for (let i = sp[s]; i <= sp[s + 2]; i++) for (let j = sp[s + 1]; j <= sp[s + 3]; j++) G.push(i, j, p);
  }
}

export function dPS(px: number, py: number, ax: number, ay: number, bx: number, by: number): number {
  const dx = bx - ax,
    dy = by - ay,
    L2 = dx * dx + dy * dy;
  let t = L2 ? ((px - ax) * dx + (py - ay) * dy) / L2 : 0;
  t = t < 0 ? 0 : t > 1 ? 1 : t;
  const ex = px - ax - dx * t,
    ey = py - ay - dy * t;
  return Math.sqrt(ex * ex + ey * ey);
}
const orient = (px: number, py: number, qx: number, qy: number, rx: number, ry: number) => (qx - px) * (ry - py) - (qy - py) * (rx - px);
function dSS(ax: number, ay: number, bx: number, by: number, cx: number, cy: number, dx: number, dy: number): number {
  const o1 = orient(ax, ay, bx, by, cx, cy),
    o2 = orient(ax, ay, bx, by, dx, dy),
    o3 = orient(cx, cy, dx, dy, ax, ay),
    o4 = orient(cx, cy, dx, dy, bx, by);
  if (((o1 > 0 && o2 < 0) || (o1 < 0 && o2 > 0)) && ((o3 > 0 && o4 < 0) || (o3 < 0 && o4 > 0))) return 0;
  return Math.min(dPS(ax, ay, cx, cy, dx, dy), dPS(bx, by, cx, cy, dx, dy), dPS(cx, cy, ax, ay, bx, by), dPS(dx, dy, ax, ay, bx, by));
}

/** On a hit: the separation vector (p's closest point minus q's) and the
 * clearance needed, so a sweep can jump straight to where that pair parts. */
const PKC = { x: 0, y: 0, need: 0 };
let CPX = 0,
  CPY = 0; // closest point on the segment dPSc last measured
function dPSc(px: number, py: number, ax: number, ay: number, bx: number, by: number): number {
  const dx = bx - ax,
    dy = by - ay,
    L2 = dx * dx + dy * dy;
  let t = L2 ? ((px - ax) * dx + (py - ay) * dy) / L2 : 0;
  t = t < 0 ? 0 : t > 1 ? 1 : t;
  CPX = ax + dx * t;
  CPY = ay + dy * t;
  const ex = px - CPX,
    ey = py - CPY;
  return Math.sqrt(ex * ex + ey * ey);
}

/** Penetration of primitive p (offset by ox, oy) into q; 0 = clear. */
function pkPen(p: Prim, ox: number, oy: number, q: Prim): number {
  if (p.k === 0) {
    const px = p.x + ox,
      py = p.y + oy;
    if (q.k === 0) {
      const need = p.rho + q.rho + PK.gapTT,
        dx = px - q.x,
        dy = py - q.y,
        d2 = dx * dx + dy * dy;
      if (d2 >= need * need) return 0;
      PKC.x = dx;
      PKC.y = dy;
      PKC.need = need;
      return need - Math.sqrt(d2);
    }
    if (q.term === p.t) return 0; // a clump's own twig
    const need = p.rho + q.hw + PK.gapTW;
    // box reject: the segment's box is further than `need` from the centre
    const gx = px < q.bx0 ? q.bx0 - px : px > q.bx1 ? px - q.bx1 : 0,
      gy = py < q.by0 ? q.by0 - py : py > q.by1 ? py - q.by1 : 0;
    if (gx * gx + gy * gy >= (need - q.hw) * (need - q.hw) && (gx > 0 || gy > 0)) return 0;
    const d = dPSc(px, py, q.x0, q.y0, q.x1, q.y1);
    if (d >= need) return 0;
    PKC.x = px - CPX;
    PKC.y = py - CPY;
    PKC.need = need;
    return need - d;
  }
  const ax = p.x0 + ox,
    ay = p.y0 + oy,
    bx = p.x1 + ox,
    by = p.y1 + oy;
  if (q.k === 0) {
    if (p.term === q.t) return 0;
    const need = q.rho + p.hw + PK.gapTW;
    const px0 = p.bx0 + ox + p.hw,
      px1 = p.bx1 + ox - p.hw,
      py0 = p.by0 + oy + p.hw,
      py1 = p.by1 + oy - p.hw;
    const gx = q.x < px0 ? px0 - q.x : q.x > px1 ? q.x - px1 : 0,
      gy = q.y < py0 ? py0 - q.y : q.y > py1 ? q.y - py1 : 0;
    if (gx * gx + gy * gy >= need * need) return 0;
    const d = dPSc(q.x, q.y, ax, ay, bx, by);
    if (d >= need) return 0;
    PKC.x = CPX - q.x;
    PKC.y = CPY - q.y;
    PKC.need = need;
    return need - d;
  }
  if (p.sf === q.sf || p.ef === q.sf || q.ef === p.sf) return 0; // siblings leaving one fork / a branch and its continuation
  const need = p.hw + q.hw + PK.gapWW;
  {
    // box reject on the centrelines' boxes
    const px0 = p.bx0 + ox + p.hw,
      px1 = p.bx1 + ox - p.hw,
      py0 = p.by0 + oy + p.hw,
      py1 = p.by1 + oy - p.hw;
    const qx0 = q.bx0 + q.hw,
      qx1 = q.bx1 - q.hw,
      qy0 = q.by0 + q.hw,
      qy1 = q.by1 - q.hw;
    const gx = px0 > qx1 ? px0 - qx1 : qx0 > px1 ? qx0 - px1 : 0,
      gy = py0 > qy1 ? py0 - qy1 : qy0 > py1 ? qy0 - py1 : 0;
    if (gx * gx + gy * gy >= need * need) return 0;
  }
  const d = dSS(ax, ay, bx, by, q.x0, q.y0, q.x1, q.y1);
  if (d >= need) return 0;
  let best = 1e18,
    vx = 0,
    vy = 0,
    e: number; // closest pair: the best of the four endpoint projections
  e = dPSc(ax, ay, q.x0, q.y0, q.x1, q.y1);
  if (e < best) {
    best = e;
    vx = ax - CPX;
    vy = ay - CPY;
  }
  e = dPSc(bx, by, q.x0, q.y0, q.x1, q.y1);
  if (e < best) {
    best = e;
    vx = bx - CPX;
    vy = by - CPY;
  }
  e = dPSc(q.x0, q.y0, ax, ay, bx, by);
  if (e < best) {
    best = e;
    vx = CPX - q.x0;
    vy = CPY - q.y0;
  }
  e = dPSc(q.x1, q.y1, ax, ay, bx, by);
  if (e < best) {
    best = e;
    vx = CPX - q.x1;
    vy = CPY - q.y1;
  }
  PKC.x = d === 0 ? 0 : vx;
  PKC.y = d === 0 ? 0 : vy;
  PKC.need = need;
  return need - d;
}

/** First penetration of p (offset) into anything in G, in grid order. */
function pkHit(G: Grid, p: Prim, ox: number, oy: number): number {
  if (!occMay(G, p, ox, oy)) return 0;
  const st = ++PK_STAMP,
    CS = PK.CS;
  if (p.k === 0) {
    const r = p.rho + 2,
      x = p.x + ox,
      y = p.y + oy;
    return scanCells(G, p, ox, oy, st, Math.floor((x - r) / CS), Math.floor((y - r) / CS), Math.floor((x + r) / CS), Math.floor((y + r) / CS));
  }
  const r = p.hw + 2,
    ax = p.x0 + ox,
    ay = p.y0 + oy,
    bx = p.x1 + ox,
    by = p.y1 + oy;
  const n = Math.max(1, Math.ceil(Math.hypot(bx - ax, by - ay) / (CS * 3)));
  for (let k = 0; k < n; k++) {
    const x0 = ax + ((bx - ax) * k) / n,
      y0 = ay + ((by - ay) * k) / n,
      x1 = ax + ((bx - ax) * (k + 1)) / n,
      y1 = ay + ((by - ay) * (k + 1)) / n;
    const v = scanCells(
      G, p, ox, oy, st,
      Math.floor((Math.min(x0, x1) - r) / CS),
      Math.floor((Math.min(y0, y1) - r) / CS),
      Math.floor((Math.max(x0, x1) + r) / CS),
      Math.floor((Math.max(y0, y1) + r) / CS)
    );
    if (v) return v;
  }
  return 0;
}
function scanCells(G: Grid, p: Prim, ox: number, oy: number, st: number, i0: number, j0: number, i1: number, j1: number): number {
  if (i0 < G.i0) i0 = G.i0;
  if (j0 < G.j0) j0 = G.j0;
  if (i1 > G.i0 + G.ni - 1) i1 = G.i0 + G.ni - 1;
  if (j1 > G.j0 + G.nj - 1) j1 = G.j0 + G.nj - 1;
  const nj = G.nj,
    lists = G.lists;
  for (let i = i0; i <= i1; i++) {
    const row = (i - G.i0) * nj - G.j0;
    for (let j = j0; j <= j1; j++) {
      const a = lists[row + j];
      if (!a) continue;
      for (let k = 0; k < a.length; k++) {
        const q = a[k];
        if (q.st === st) continue;
        q.st = st;
        if (q.sub !== -1 && (q.dead || (q.sub >= PK_IG0 && q.sub < PK_IG1) !== PK_ONLY)) continue; // compaction: the moving subtree / stale copies
        const v = pkPen(p, ox, oy, q);
        if (v > 0) return v;
      }
    }
  }
  return 0;
}

function pkDist(p: Prim): number {
  return p.k === 0 ? Math.max(0, Math.hypot(p.x, p.y) - p.rho) : Math.min(Math.hypot(p.x0, p.y0), Math.hypot(p.x1, p.y1)) - p.hw;
}

// --- Stems ------------------------------------------------------------------

/** Stem geometry: a cubic from the fork (origin) to E. It leaves the fork
 * leaning between "up" and the child's direction and arrives along the child's
 * own (leaning) up, so forks read as a Y and limbs as arcs that turn toward the
 * light; a long limb runs straighter, so it never sags into a bowl. */
const STEM = { a0: 0.9, a1: 1.3, k0: 0.42, k1: 0.22, Lref: 180 };
export function stemCP(Ex: number, Ey: number, rho: number): number[] {
  const L = Math.hypot(Ex, Ey) || 1,
    ux = Ex / L,
    uy = Ey / L,
    kL = Math.min(1, STEM.Lref / L);
  let t0x = ux,
    t0y = uy - STEM.a0 * kL;
  const l0 = Math.hypot(t0x, t0y) || 1;
  t0x /= l0;
  t0y /= l0;
  let t1x = ux + STEM.a1 * kL * Math.sin(rho),
    t1y = uy - STEM.a1 * kL * Math.cos(rho);
  const l1 = Math.hypot(t1x, t1y) || 1;
  t1x /= l1;
  t1y /= l1;
  const k0 = L * STEM.k0,
    k1 = L * STEM.k1; // a long lean out of the fork, a short turn into the child
  return [0, 0, t0x * k0, t0y * k0, Ex - t1x * k1, Ey - t1y * k1, Ex, Ey];
}
export function bez(c: number[], t: number): [number, number] {
  const mt = 1 - t;
  return [
    mt * mt * mt * c[0] + 3 * mt * mt * t * c[2] + 3 * mt * t * t * c[4] + t * t * t * c[6],
    mt * mt * mt * c[1] + 3 * mt * mt * t * c[3] + 3 * mt * t * t * c[5] + t * t * t * c[7],
  ];
}
export const STEM_SEG = 10;
/** Branch half-width along t ∈ [0,1]: the drawn outline, the collision stems
 * and the harness all use this taper. */
export const taperHW = (wb: number, we: number, t: number) => (wb + (we - wb) * Math.pow(t, 0.9)) / 2;
const TAP5 = [0, 1, 2, 3, 4, 5].map((i) => Math.pow(i / 5, 0.9));
function pkStemSegs(S: Shape, c: number[], fid: number, pool?: Prim[]): Prim[] {
  const out: Prim[] = pool || [],
    n = 5;
  let px = c[0],
    py = c[1];
  const ef = S.fork != null ? S.fork : S.efId;
  for (let i = 1; i <= n; i++) {
    const q = bez(c, i / n);
    const hw = Math.max(S.wb + (S.we - S.wb) * TAP5[i - 1], S.wb + (S.we - S.wb) * TAP5[i]) / 2 + 0.35; // + chord error of the 5-piece polyline
    if (pool) setSeg(pool[i - 1], px, py, q[0], q[1], hw, fid, ef, S.term || null);
    else out.push(mkSeg(px, py, q[0], q[1], hw, fid, ef, S.term || null));
    px = q[0];
    py = q[1];
  }
  return out;
}
/** A child is placed rotated by rho after an optional mirror (f = -1):
 * x' = c·f·x − s·y, y' = s·f·x + c·y */
function pkRotPrim(p: Prim, c: number, s: number, f: number): Prim {
  const q =
    p.k === 0
      ? mkDisc(c * f * p.x - s * p.y, s * f * p.x + c * p.y, p.rho, p.t)
      : mkSeg(c * f * p.x0 - s * p.y0, s * f * p.x0 + c * p.y0, c * f * p.x1 - s * p.y1, s * f * p.x1 + c * p.y1, p.hw, p.sf, p.ef, p.term);
  q.d = p.d;
  return q;
}
function pkRotated(S: Shape, rho: number, f: number): Prim[] {
  if (!rho && f === 1) return S.prims!;
  const key = Math.round(rho * 1e4) * 2 + (f < 0 ? 1 : 0);
  if (!S.rc) S.rc = new Map();
  let P = S.rc.get(key);
  if (P) return P;
  const c = Math.cos(rho),
    s = Math.sin(rho);
  P = S.prims!.map((p) => pkRotPrim(p, c, s, f));
  S.rc.set(key, P);
  return P;
}
/** Mass moments of S mirrored by f and rotated by rho (about S's origin). */
function pkMom(S: Shape, rho: number, f: number): Moments {
  const c = Math.cos(rho),
    s = Math.sin(rho),
    sx = f * S.sx,
    sxy = f * S.sxy;
  return {
    sx: c * sx - s * S.sy,
    sy: s * sx + c * S.sy,
    sxx: c * c * S.sxx - 2 * c * s * sxy + s * s * S.syy,
    syy: s * s * S.sxx + 2 * c * s * sxy + c * c * S.syy,
    sxy: c * s * (S.sxx - S.syy) + (c * c - s * s) * sxy,
  };
}
/** Cost of a child placement: its mass moment about the union's heart
 * (0, −h), a point above the fork, plus a penalty for mass below the fork. */
function pkCost(S: Shape, mo: Moments, Ex: number, Ey: number, h: number): number {
  const cy = Ey + mo.sy / S.m,
    Ey2 = Ey + h;
  let c =
    (mo.sxx + 2 * Ex * mo.sx + S.m * Ex * Ex) * PK.ax +
    (mo.syy + 2 * Ey2 * mo.sy + S.m * Ey2 * Ey2) * PK.ay +
    PK.down * S.m * Math.max(0, cy) * Math.abs(cy);
  if (PK.flat) {
    const e = Math.max(0, Math.abs(Math.atan2(Ex, -Ey)) - PK.flatA);
    c += PK.flat * S.m * h * h * e * e;
  }
  return c;
}

// --- Rigid groups: skip whole bundles of a moving shape's prims ------------

/** Spatial buckets of a rigid prim set (by the coarse cell of each prim's box
 * centre), each with the union box of its members. Moved as one, a bucket whose
 * box touches no relevant occupancy cannot hit anything: all its prims are
 * skipped. The prims are still visited in their own order, so the first hit —
 * which decides the sweep step — is the one the plain loop would find. */
interface Buckets {
  of: Int32Array;
  box: Float64Array;
  flag: Int32Array;
  gen: number;
}
const BUCKET_CS = 64;
function bucketsOf(P: Prim[]): Buckets {
  const ids = new Map<number, number>();
  const of = new Int32Array(P.length);
  const boxes: number[] = [];
  for (let i = 0; i < P.length; i++) {
    const p = P[i];
    const x0 = p.k === 0 ? p.x - p.rho : p.bx0,
      y0 = p.k === 0 ? p.y - p.rho : p.by0,
      x1 = p.k === 0 ? p.x + p.rho : p.bx1,
      y1 = p.k === 0 ? p.y + p.rho : p.by1;
    const key = pkKey(Math.floor((x0 + x1) / 2 / BUCKET_CS), Math.floor((y0 + y1) / 2 / BUCKET_CS));
    let b = ids.get(key);
    if (b === undefined) {
      b = ids.size;
      ids.set(key, b);
      boxes.push(x0, y0, x1, y1);
    } else {
      const o = b * 4;
      if (x0 < boxes[o]) boxes[o] = x0;
      if (y0 < boxes[o + 1]) boxes[o + 1] = y0;
      if (x1 > boxes[o + 2]) boxes[o + 2] = x1;
      if (y1 > boxes[o + 3]) boxes[o + 3] = y1;
    }
    of[i] = b;
  }
  return { of, box: Float64Array.from(boxes), flag: new Int32Array(ids.size), gen: 0 };
}

// --- The sweep ----------------------------------------------------------------

interface SweepHit {
  l: number;
  Ex: number;
  Ey: number;
  c: number[];
  stem: Prim[];
  rho: number;
  f: number;
  mo: Moments;
}

/** Push S out from the fork along direction psi until it and its stem are
 * clear; null if it cannot beat costCap. */
function pkSweep(
  S: Shape,
  psi: number,
  rho: number,
  f: number,
  Phi: Grid,
  l0: number,
  fid: number,
  maxL: number,
  costCap: number | undefined,
  costBase: number,
  h: number
): SweepHit | null {
  CTX.stats.sweeps++;
  const ux = Math.sin(psi),
    uy = -Math.cos(psi);
  const P = pkRotated(S, rho, f),
    mo = pkMom(S, rho, f),
    cr = Math.cos(-rho),
    sr = Math.sin(-rho);
  let l = l0,
    selfN = 0;
  for (let it = 0; it < 120 && l <= maxL; it++) {
    const Ex = ux * l,
      Ey = uy * l;
    if (costCap !== undefined && costBase + pkCost(S, mo, Ex, Ey, h) >= costCap) return null; // only gets worse further out
    const c = stemCP(Ex, Ey, rho);
    const stem = pkStemSegs(S, c, fid, SWEEP_POOL);
    let pen = 0,
      self = false,
      frac = 1;
    for (let i = 0; i < stem.length && !pen; i++) {
      pen = pkHit(Phi, stem[i], 0, 0);
      if (pen) frac = Math.max(0.35, (i + 0.5) / stem.length);
    }
    if (!pen && S.grid)
      for (let i = 0; i < stem.length && !pen; i++) {
        const g = stem[i]; // the stem in the child's own frame
        const loc = LOC_POOL;
        setSeg(
          loc,
          f * (cr * (g.x0 - Ex) - sr * (g.y0 - Ey)),
          sr * (g.x0 - Ex) + cr * (g.y0 - Ey),
          f * (cr * (g.x1 - Ex) - sr * (g.y1 - Ey)),
          sr * (g.x1 - Ex) + cr * (g.y1 - Ey),
          g.hw,
          g.sf,
          g.ef,
          g.term
        );
        pen = pkHit(S.grid, loc, 0, 0);
        if (pen && i >= 3) self = true;
      }
    if (!pen) for (let i = 0; i < P.length && !pen; i++) pen = pkHit(Phi, P[i], Ex, Ey);
    if (!pen) return { l, Ex, Ey, c, stem: stem.map((q) => clonePrim(q, 0, 0)), rho, f, mo };
    if (self && ++selfN > 4) return null; // the stem's arrival runs into the child's own foliage: no length fixes that
    // advance until the blocking pair parts along u (exact for discs), at least a sliver
    const dot = PKC.x * ux + PKC.y * uy,
      dd = PKC.x * PKC.x + PKC.y * PKC.y;
    let step = -dot + Math.sqrt(Math.max(0, dot * dot - dd + PKC.need * PKC.need));
    if (self) step = pen;
    else step /= frac;
    l += Math.max(0.6, Math.min(step + 0.15, 400), l * 0.015);
  }
  return null;
}

// --- Clumps -----------------------------------------------------------------

/** A clump: its files on a jittered hex grid, nearest the centre first
 * (biggest files in the middle). */
export function pkTerm(node: LNode, own: boolean, files: LFile[], region: string): Term {
  const n = files.length;
  const s = S0 * (region === "root" ? 0.84 : 0.92);
  const rnd = rng(hashStr(node.path + (own ? "#" : "") + region));
  const K = Math.ceil(Math.sqrt(Math.max(1, n))) + 2;
  const pts: Array<[number, number, number]> = [];
  for (let j = -K; j <= K; j++)
    for (let i = -K; i <= K; i++) {
      const x = (i + (j & 1) * 0.5) * s,
        y = j * s * 0.866;
      pts.push([x, y, Math.hypot(x, y * 1.08) + (rnd() - 0.5) * 0.01]);
    }
  pts.sort((a, b) => a[2] - b[2]);
  const P = pts.slice(0, n);
  let cx = 0,
    cy = 0;
  for (const p of P) {
    cx += p[0];
    cy += p[1];
  }
  cx /= Math.max(1, n);
  cy /= Math.max(1, n);
  const loc = P.map((p) => [p[0] - cx + (rnd() - 0.5) * s * 0.3, p[1] - cy + (rnd() - 0.5) * s * 0.3] as [number, number]);
  let R = 0;
  for (const p of loc) R = Math.max(R, Math.hypot(p[0], p[1]));
  // the foliage disc: leaf centres plus the blob radius every renderer draws around them
  return { own, node, files, s, region, loc, R, rho: R + 0.66 * s, cy0: 0 };
}
export function pkWidth(kW: number, lines: number, files: number): number {
  const w = Math.max(0.55, kW * Math.sqrt(lines + 40 * files));
  return CTX && CTX.quantise ? Math.exp(Math.round(Math.log(w) / 0.03) * 0.03) : w;
}
function pkFinishShape(S: Shape): Shape {
  if (S.prims) {
    S.prims.sort((a, b) => a.d - b.d); // nearest the origin first: collisions near the fork are found early
    let r = 0;
    for (const p of S.prims)
      r = Math.max(r, p.k === 0 ? Math.hypot(p.x, p.y) + p.rho : Math.max(Math.hypot(p.x0, p.y0), Math.hypot(p.x1, p.y1)) + p.hw);
    S.rad = r;
  }
  return S;
}
function emptyShape(kind: Shape["kind"]): Shape {
  return {
    kind, term: null, fork: null, efId: 0, prims: null, place: [], m: 0, area: 0, sx: 0, sy: 0, sxx: 0, syy: 0, sxy: 0, r0: 0, r1: 0,
    lines: 0, files: 0, w: 0, wb: 0, we: 0, rad: 0, grid: null, rc: null, sig: "", skey: "",
  };
}
function pkTermShape(t: Term, kind: "leaf" | "own", node: LNode): Shape {
  const n = t.loc.length;
  const cy = -(t.R * 0.42 + t.s * 0.6); // the clump sits on its twig's tip
  let sxx = 0,
    syy = 0,
    sy = 0,
    sxy = 0;
  for (const p of t.loc) {
    sxx += p[0] * p[0];
    syy += (cy + p[1]) * (cy + p[1]);
    sy += cy + p[1];
    sxy += p[0] * (cy + p[1]);
  }
  t.cy0 = cy;
  let lines = 0,
    files = 0;
  for (const f of t.files)
    if (!f.ghost) {
      lines += f.lines;
      files++;
    }
  const w = pkWidth(CTX.kW, lines, files);
  const S = emptyShape(kind);
  S.node = node;
  S.term = t;
  S.efId = -(1e6 + ++PK_FORK);
  const d0 = mkDisc(0, cy, t.rho, t);
  S.prims = [d0];
  S.m = Math.max(1, n);
  S.area = Math.PI * t.rho * t.rho;
  S.sy = sy;
  S.sxx = sxx;
  S.syy = syy;
  S.sxy = sxy;
  S.lines = lines;
  S.files = files;
  S.w = w;
  S.wb = w;
  S.we = w * 0.35;
  d0.d = pkDist(d0);
  // what makes this clump's shape: its files' count (the hex grid), its
  // width (quantised in model.ts) and its place in the tree
  S.skey = h2((kind === "own" ? "o" : "l") + ":" + node.path + ":" + t.region);
  S.sig = h2(S.skey + ":" + n + ":" + w.toFixed(4));
  CTX.sigs.push(S.sig);
  return pkFinishShape(S);
}
function pkLmin(S: Shape): number {
  return S.kind === "virtual" ? S0 * 0.5 + S.w * 0.3 : S.term ? S0 * 0.6 + S.w * 0.5 : S0 * 0.8 + S.w * 0.6;
}
const PSI_H = [0, 0.25, 0.5, 0.75, 1.0],
  EXT_H = [0, 0.8, 1.6];
const PSI_L = [0.12, 0.3, 0.48, 0.66, 0.84, 1.02, 1.2, 1.38, 1.56];

/** The direction range a placed child adds to its union: its clumps' (and
 * stems') leans, plus its own stem. */
function pkRange(S: Shape, pp: { rho: number; f: number; Ex: number; Ey: number }): [number, number] {
  let a = pp.rho + (pp.f > 0 ? S.r0 : -S.r1),
    b = pp.rho + (pp.f > 0 ? S.r1 : -S.r0);
  if (PK.stemR) {
    const psi = Math.atan2(pp.Ex, -pp.Ey);
    a = Math.min(a, psi);
    b = Math.max(b, psi);
  }
  return [a, b];
}
const leanOf = (S: Shape, psi: number, f: number) => {
  const r0 = f > 0 ? S.r0 : -S.r1,
    r1 = f > 0 ? S.r1 : -S.r0;
  return Math.max(-LEAN.lim - r0, Math.min(LEAN.lim - r1, LEAN.k * psi));
};
/** Children grow outward: a child on the right keeps (or is mirrored to) its
 * bulk on the right. */
const flipFor = (S: Shape, side: number) => (S.place.length && S.sx * side < 0 ? -1 : 1);

function pkUnion(owner: LNode, X: Shape, Y: Shape, fid: number, w: number): Shape {
  const U = emptyShape("virtual");
  U.owner = owner;
  U.fork = fid;
  U.lines = X.lines + Y.lines;
  U.files = X.files + Y.files;
  U.w = w;
  U.wb = w;
  U.we = Math.min(w, Math.max(X.wb, Y.wb));
  U.m = X.m + Y.m;
  U.area = X.area + Y.area;
  U.r0 = 1e9;
  U.r1 = -1e9;
  return U;
}

/** Apply a recorded fork (replay or memo): the two placements, no search. */
function unionFromRec(owner: LNode, X: Shape, Y: Shape, fid: number, w: number, r: UnionRec, withPrims: boolean): Shape {
  const U = pkUnion(owner, X, Y, fid, w);
  const pls: Array<[Shape, number]> = [
    [X, 0],
    [Y, 4],
  ];
  if (withPrims) U.prims = [];
  for (const [S, o] of pls) {
    const pl: Placement = { S, Ex: r[o], Ey: r[o + 1], rho: r[o + 2], f: r[o + 3], c: stemCP(r[o], r[o + 1], r[o + 2]) };
    U.place.push(pl);
    if (!withPrims) continue;
    const mo = pkMom(S, pl.rho, pl.f);
    for (const sg of pkStemSegs(S, pl.c, fid)) {
      sg.d = pkDist(sg);
      U.prims!.push(sg);
    }
    for (const p of pkRotated(S, pl.rho, pl.f)) {
      const q = clonePrim(p, pl.Ex, pl.Ey);
      q.d = pkDist(q);
      U.prims!.push(q);
    }
    const ex = pl.Ex,
      ey = pl.Ey;
    U.sx += mo.sx + S.m * ex;
    U.sy += mo.sy + S.m * ey;
    U.sxx += mo.sxx + 2 * ex * mo.sx + S.m * ex * ex;
    U.syy += mo.syy + 2 * ey * mo.sy + S.m * ey * ey;
    U.sxy += mo.sxy + ex * mo.sy + ey * mo.sx + S.m * ex * ey;
    const rr = pkRange(S, pl);
    U.r0 = Math.min(U.r0, rr[0]);
    U.r1 = Math.max(U.r1, rr[1]);
    S.grid = null;
    S.rc = null;
  }
  if (withPrims) pkFinishShape(U);
  return U;
}

/** `key`: the fork's identity across builds — a folder's top fork is the
 * folder; an inner fork is the range of the folder's items it pairs. */
function pkCombine(X: Shape, Y: Shape, owner: LNode, base: Prim[] | undefined, key?: string): Shape {
  const fid = ++PK_FORK;
  const w = pkWidth(CTX.kW, X.lines + Y.lines, X.files + Y.files);
  const sig = h2("(" + X.sig + "|" + Y.sig + "|" + w.toFixed(4) + (base ? "|B" + base[0].hw.toFixed(4) : "") + ")");
  const skey = key || h2("U(" + X.skey + "," + Y.skey + ")");
  if (CTX.cache) {
    // replay: placements a previous build recorded, in build order
    const r = CTX.cache[CTX.ci++] || [0, -S0, 0, 1, 0, -S0, 0, 1];
    const U = unionFromRec(owner, X, Y, fid, w, r, false);
    U.sig = sig;
    U.skey = skey;
    CTX.unions.push(U);
    return U;
  }
  if (CTX.final) {
    // warm: this fork's previous FINAL placement (post-compaction); overlaps
    // a changed subtree causes are resolved after the bottom-up pass
    const fv = CTX.final.get(skey);
    if (fv) {
      CTX.memoHits++;
      const U = unionFromRec(owner, X, Y, fid, w, fv.u, true);
      U.sig = sig;
      U.skey = skey;
      U.fromWarm = true;
      U.changed = fv.sig !== sig;
      CTX.unions.push(U);
      progress();
      return U;
    }
  }
  const memo = CTX.memo.get(sig);
  if (memo) {
    CTX.memoHits++;
    const U = unionFromRec(owner, X, Y, fid, w, memo, true);
    U.sig = sig;
    U.skey = skey;
    U.changed = !!CTX.final;
    CTX.unions.push(U);
    progress();
    return U;
  }
  // the branch that will reach this fork arrives from below, within ~0.5 rad of vertical: keep that fan clear
  const Lc = Math.max(S0 * 2.2, w * 1.5);
  const basePrims = base
    ? base.map((p) => (p.ef === -3 ? mkSeg(p.x0, p.y0, p.x1, p.y1, p.hw, p.sf, fid, p.term) : p))
    : [-0.5, 0, 0.5].map((a) => mkSeg(Math.sin(a) * Lc, Math.cos(a) * Lc, 0, 0, w * 0.5 + 0.8, -1, fid, null));
  const mkPhi = () => {
    const G = new Grid(64);
    for (const p of basePrims) pkAdd(G, p);
    return G;
  };
  const Phi0 = mkPhi();
  const H = X.m >= Y.m ? X : Y,
    L = H === X ? Y : X;
  const sH = H === X ? -1 : 1,
    sL = -sH;
  const fr = L.m / (H.m + L.m);
  for (const S of [H, L])
    if (!S.grid && S.place.length) {
      S.grid = new Grid(S.prims!.length * 4);
      for (const p of S.prims!) pkAdd(S.grid, p);
    }
  const maxL = 3 * (H.rad + L.rad) + S0 * 30;
  const h = (PK.heart * Math.sqrt((X.area + Y.area) / Math.PI)) / 0.72; // the union's heart: about one clump-radius up
  let best: { c: number; pH: SweepHit; pL: SweepHit } | null = null;
  const topLim = !!base && PK.rtop < 9 && !PK.stemR; // the trunk fork's limbs rise at most rtop from vertical
  let strict = true;
  const tryH = (aH: number, ext: number, lim: number) => {
    if (strict && topLim && aH > PK.rtop) return;
    const psiH = sH * aH,
      fH = flipFor(H, sH);
    const pH = pkSweep(H, psiH, leanOf(H, psiH, fH), fH, Phi0, pkLmin(H) + ext * L.rad, fid, lim, undefined, 0, h);
    if (!pH) return;
    const cH = pkCost(H, pH.mo, pH.Ex, pH.Ey, h);
    if (best && cH >= best.c) return;
    const Phi1 = mkPhi();
    for (const sg of pH.stem) pkAdd(Phi1, sg);
    for (const p of pkRotated(H, pH.rho, pH.f)) pkAdd(Phi1, clonePrim(p, pH.Ex, pH.Ey));
    const fL = flipFor(L, sL);
    for (const aL of PSI_L) {
      if (strict && topLim && aL > PK.rtop) continue;
      const psiL = sL * aL;
      const pL = pkSweep(L, psiL, leanOf(L, psiL, fL), fL, Phi1, pkLmin(L), fid, lim, best ? best.c : undefined, cH, h);
      if (!pL) continue;
      const c = cH + pkCost(L, pL.mo, pL.Ex, pL.Ey, h);
      if (strict && PK.stemR) {
        // keep every limb rising: the union's direction range stays inside the lean budget
        const rH = pkRange(H, pH),
          rL = pkRange(L, pL),
          lo = Math.min(rH[0], rL[0]),
          hi = Math.max(rH[1], rL[1]);
        if (hi - lo > 2 * LEAN.lim || (base && (lo < -PK.rtop || hi > PK.rtop))) continue;
      }
      if (!best || c < best.c) best = { c, pH, pL };
    }
  };
  // the heavier child leans little when its sibling is small, more when they
  // are a matched pair, and may climb past its sibling (a leader with a side
  // shoot) instead of sitting beside it (a rake)
  for (const ext of EXT_H) for (const aH of PSI_H) tryH(aH * (0.35 + 1.3 * fr), ext, maxL);
  if (!best && PK.stemR) for (const ext of [2.4, 3.4]) for (const aH of PSI_H) tryH(aH * (0.35 + 1.3 * fr), ext, maxL);
  strict = false;
  if (!best) for (const aH of [0.3, 0.6, 0.9]) tryH(aH, 0.6, maxL);
  if (!best) for (const aH of [0.5, 0.9]) tryH(aH, 1.2, 1e5);
  if (!best) {
    // nothing fits anywhere (cannot happen with finite shapes): stack them far apart
    const far = 4 * (H.rad + L.rad) + S0 * 40;
    const r: UnionRec = [sH * far * 0.3, -far, 0, 1, sL * far * 0.3, -far, 0, 1];
    if (H !== X) r.splice(0, 8, r[4], r[5], r[6], r[7], r[0], r[1], r[2], r[3]);
    const U = unionFromRec(owner, X, Y, fid, w, r, true);
    U.sig = sig;
    U.skey = skey;
    U.changed = true;
    CTX.unions.push(U);
    return U;
  }
  const B = best as { c: number; pH: SweepHit; pL: SweepHit };
  const pX = H === X ? B.pH : B.pL,
    pY = H === X ? B.pL : B.pH;
  const U = pkUnion(owner, X, Y, fid, w);
  U.prims = [];
  U.sig = sig;
  U.skey = skey;
  U.changed = !!CTX.final;
  for (const [S, pp] of [
    [X, pX],
    [Y, pY],
  ] as Array<[Shape, SweepHit]>) {
    U.place.push({ S, Ex: pp.Ex, Ey: pp.Ey, rho: pp.rho, f: pp.f, c: pp.c });
    for (const sg of pp.stem) {
      sg.d = pkDist(sg);
      U.prims.push(sg);
    }
    for (const p of pkRotated(S, pp.rho, pp.f)) {
      const q = clonePrim(p, pp.Ex, pp.Ey);
      q.d = pkDist(q);
      U.prims.push(q);
    }
    const mo = pp.mo,
      ex = pp.Ex,
      ey = pp.Ey;
    U.sx += mo.sx + S.m * ex;
    U.sy += mo.sy + S.m * ey;
    U.sxx += mo.sxx + 2 * ex * mo.sx + S.m * ex * ex;
    U.syy += mo.syy + 2 * ey * mo.sy + S.m * ey * ey;
    U.sxy += mo.sxy + ex * mo.sy + ey * mo.sx + S.m * ex * ey;
    const rr = pkRange(S, pp);
    U.r0 = Math.min(U.r0, rr[0]);
    U.r1 = Math.max(U.r1, rr[1]);
    S.grid = null;
    S.rc = null; // children are never placed again: free their collision caches
  }
  const rec: UnionRec = U.place.flatMap((p) => [p.Ex, p.Ey, p.rho, p.f]);
  CTX.unions.push(U);
  CTX.memo.set(sig, rec);
  progress();
  return pkFinishShape(U);
}

/** The share of a cold build the bottom-up search takes (compaction the rest). */
const PHASE_SEARCH = 0.45;
function progress() {
  CTX.work += 1;
  if (CTX.tick) CTX.tick(PHASE_SEARCH * Math.min(1, CTX.work / CTX.workTotal));
}

/** Balanced binary fan over an ordered item list (weights = leaves). */
/** Where a fan splits an ordered item list: the most balanced point. A warm
 * build keeps the previous build's split while it is still nearly as balanced
 * (a file more or less must not re-pair a whole crown). */
function splitAt(list: Shape[], owner: LNode): number {
  const tw = list.reduce((s, x) => s + x.m, 0);
  let acc = 0,
    k2 = 1,
    best = 1e18;
  const cum: number[] = [];
  for (let i = 0; i < list.length - 1; i++) {
    acc += list[i].m;
    cum.push(acc);
    const dd = Math.abs(acc - tw / 2);
    if (dd < best) {
      best = dd;
      k2 = i + 1;
    }
  }
  const key = h2(owner.path + "|" + list[0].skey + "|" + list[list.length - 1].skey);
  const was = CTX.prevSplits ? CTX.prevSplits.get(key) : undefined;
  if (was !== undefined) {
    const j = list.findIndex((x) => x.skey === was);
    if (j >= 1 && Math.abs(cum[j - 1] - tw / 2) <= best + 0.12 * tw) k2 = j;
  }
  CTX.splits.set(key, list[k2].skey);
  return k2;
}
function pkFan(list: Shape[], owner: LNode, base?: Prim[], key?: string): Shape {
  if (list.length === 1) return list[0];
  const k2 = splitAt(list, owner);
  const rk = (l: Shape[]) => h2("r:" + owner.path + "|" + l[0].skey + "|" + l[l.length - 1].skey);
  return pkCombine(pkFan(list.slice(0, k2), owner, undefined, rk(list.slice(0, k2))), pkFan(list.slice(k2), owner, undefined, rk(list.slice(k2))), owner, base, key || rk(list));
}
function pkNode(n: LNode, region: string, terms: Term[]): Shape {
  if (!n.kids.length) {
    const t = pkTerm(n, false, n.files, region);
    n.term = t;
    terms.push(t);
    return pkTermShape(t, "leaf", n);
  }
  const items: Shape[] = [];
  if (n.files.length) {
    const t = pkTerm(n, true, n.files, region);
    terms.push(t);
    items.push(pkTermShape(t, "own", n));
  }
  for (const k of n.kids) items.push(pkNode(k, region, terms));
  const S = items.length === 1 ? items[0] : pkFan(items, n, undefined, h2("n:" + n.path + ":" + region));
  if (S.kind === "virtual") {
    S.kind = "node";
    S.node = n;
    S.wb = S.w;
  }
  return S;
}

/** Bottom→top order of the limbs along a leader: the biggest in the middle of
 * the crown, the smallest at the very top and bottom. */
function pkLeaderSeq(items: Shape[]): Shape[] {
  const byW = items.slice().sort((a, b) => b.m - a.m || cmpStr(a.node!.name, b.node!.name));
  if (PK.leaderOrder === "asc") return byW.slice().reverse();
  if (PK.leaderOrder === "desc") return byW;
  const lo: Shape[] = [],
    hi: Shape[] = [];
  byW.forEach((it, i) => (i % 2 ? hi : lo).push(it));
  return lo.reverse().concat(hi);
}

/** Top level: the root's items fan out from the trunk top; the trunk itself is
 * the obstacle below that fork. */
export function pkLayout(root: LNode, region: string, order: (items: Shape[]) => Shape[], trunkHW: number): { terms: Term[]; top: Shape | null } {
  const terms: Term[] = [];
  const items: Shape[] = [];
  if (root.files.length) {
    const t = pkTerm(root, true, root.files, region);
    terms.push(t);
    items.push(pkTermShape(t, "own", root));
  }
  for (const k of root.kids) items.push(pkNode(k, region, terms));
  if (!items.length) return { terms, top: null };
  let ord = order(items);
  const was = CTX.prevOrders ? CTX.prevOrders.get(region) : undefined;
  if (was) {
    // warm: the previous build's order of the top-level items; new ones go where the cold order puts them
    const at = new Map(ord.map((x, i) => [x.skey, i]));
    const kept = was.map((k) => ord.find((x) => x.skey === k)).filter((x): x is Shape => !!x);
    const keptSet = new Set(kept);
    const out = kept.slice();
    for (const x of ord) if (!keptSet.has(x)) out.splice(Math.min(out.length, at.get(x.skey)!), 0, x);
    ord = out;
  }
  CTX.orders.set(region, ord.map((x) => x.skey));
  // the trunk below the fork, widening toward the ground (sf -2 never matches a stem; ef -3 = "ends at this fork")
  const base: Prim[] = [];
  for (let i = 0; i < 6; i++) base.push(mkSeg(0, i * S0 * 6, 0, (i + 1) * S0 * 6, trunkHW * (1 + 0.25 * i), -2, -3, null));
  let top: Shape | null = ord.length === 1 ? ord[0] : null;
  if (!top && PK.leader && ord.length > PK.leader) {
    const seq = pkLeaderSeq(ord);
    let U = seq[seq.length - 1];
    for (let i = seq.length - 2; i >= 0; i--) {
      const left = (seq.length - 2 - i) % 2 === 0,
        bse = i === 0 ? base : undefined;
      const k = h2("lead:" + region + ":" + i);
      U = left ? pkCombine(seq[i], U, root, bse, k) : pkCombine(U, seq[i], root, bse, k);
    }
    top = U;
    top.base = base;
  }
  if (!top) {
    const k2 = splitAt(ord, root);
    const rk = (l: Shape[]) => h2("r:" + region + "|" + l[0].skey + "|" + l[l.length - 1].skey);
    top = pkCombine(pkFan(ord.slice(0, k2), root, undefined, rk(ord.slice(0, k2))), pkFan(ord.slice(k2), root, undefined, rk(ord.slice(k2))), root, base, h2("top:" + region));
    top.base = base;
  }
  return { terms, top };
}

// --- Global compaction -----------------------------------------------------

interface Rec {
  U: Shape;
  pl: Placement;
  O: [number, number];
  A: number[];
  Ac: number[];
  E: [number, number];
  lo: number;
  hi: number;
  depth: number;
  own: Prim[];
}

const mul = (A: number[], B: number[]) => [A[0] * B[0] + A[1] * B[2], A[0] * B[1] + A[1] * B[3], A[2] * B[0] + A[3] * B[2], A[2] * B[1] + A[3] * B[3]];
const app = (A: number[], x: number, y: number): [number, number] => [A[0] * x + A[1] * y, A[2] * x + A[3] * y];

/** The bottom-up build put every subtree as close to its fork as its SIBLING
 * allowed; it never saw its cousins. This pass re-sweeps each subtree against
 * the whole tree (largest first, every direction), sliding it rigidly toward
 * the crown's heart wherever there is air, and keeps a move only when it is
 * overlap-free. In a warm build (CTX.prevSigs set) it first resolves overlaps
 * instead: every subtree whose content changed is checked where it stands and
 * pushed out along its own direction until clear (deepest first, escalating
 * to the fork above), then the whole tree is verified overlap-free — else the
 * caller lays out cold (CTX.warmFailed). */
export function pkCompact(top: Shape, base: Prim[], C: [number, number], passes: number, region: string) {
  const recs: Rec[] = [];
  const G = new Grid(4096);
  for (const p of base) {
    const q = mkSeg(p.x0, p.y0, p.x1, p.y1, p.hw, p.sf, top.fork!, p.term);
    q.sub = -5;
    pkAdd(G, q);
  }
  function mkStemWorld(rec: Rec, Ex: number, Ey: number, pool?: Prim[]) {
    const pl = rec.pl,
      c = stemCP(Ex, Ey, pl.rho);
    const w: number[] = [];
    for (let i = 0; i < 8; i += 2) {
      const q = app(rec.A, c[i], c[i + 1]);
      w.push(rec.O[0] + q[0], rec.O[1] + q[1]);
    }
    return { c, segs: pkStemSegs(pl.S, w, rec.U.fork!, pool) };
  }
  const discOf = (rec: Rec, S: Shape) => {
    const q = app(rec.Ac, 0, S.term!.cy0);
    const d = mkDisc(rec.E[0] + q[0], rec.E[1] + q[1], S.term!.rho, S.term);
    d.sub = rec.lo;
    d.m = S.m;
    return d;
  };
  (function walk(U: Shape, O: [number, number], A: number[], depth: number) {
    for (const pl of U.place) {
      const rec: Rec = { U, pl, O, A, Ac: A, E: [0, 0], lo: recs.length, hi: 0, depth, own: [] };
      recs.push(rec);
      for (const sg of mkStemWorld(rec, pl.Ex, pl.Ey).segs) {
        sg.sub = rec.lo;
        rec.own.push(sg);
        pkAdd(G, sg);
      }
      const e = app(A, pl.Ex, pl.Ey);
      rec.E = [O[0] + e[0], O[1] + e[1]];
      const cr = Math.cos(pl.rho),
        sr = Math.sin(pl.rho),
        f = pl.f || 1;
      rec.Ac = mul(A, [cr * f, -sr, sr * f, cr]);
      if (pl.S.term) {
        const d = discOf(rec, pl.S);
        rec.own.push(d);
        pkAdd(G, d);
      } else walk(pl.S, rec.E, rec.Ac, depth + 1);
      rec.hi = recs.length;
    }
  })(top, [0, 0], [1, 0, 0, 1], 0);
  const order = recs
    .map((_r, i) => i)
    .sort((a, b) => recs[a].depth - recs[b].depth || recs[b].hi - recs[b].lo - (recs[a].hi - recs[a].lo));
  const prev = CTX.prevSigs;
  const totalPasses = passes;
  // Dirty tracking (exact): a record whose last evaluation found no move is
  // skipped while nothing changed inside the region its candidates reached —
  // its evaluation would see the same prims and find no move again.
  const changes: number[] = []; // boxes of applied moves, in order
  const evalAt = new Int32Array(recs.length).fill(-1); // changes.length / 4 at a no-move evaluation
  const reach = new Float64Array(recs.length * 4);
  const REACH_PAD = 3 * PK.CS + 8;
  const logChange = (b: number[]) => changes.push(b[0], b[1], b[2], b[3]);
  let deadN = 0;
  let moved = 0;
  /** One record. mode 0 = compaction (slide toward the heart), 1 = warm
   * resolution (push a changed subtree out of an overlap), 2 = verify (is it
   * clear where it stands?). Returns false only for a blocked verify/resolve. */
  const evalRec = (ri: number, mode: 0 | 1 | 2): boolean => {
      const rec = recs[ri],
        pl = rec.pl,
        S = pl.S;
      if (mode === 0 && evalAt[ri] >= 0) {
        let dirty = false;
        const r0 = ri * 4;
        for (let c = evalAt[ri] * 4; c < changes.length && !dirty; c += 4)
          dirty = changes[c] <= reach[r0 + 2] && changes[c + 2] >= reach[r0] && changes[c + 1] <= reach[r0 + 3] && changes[c + 3] >= reach[r0 + 1];
        if (!dirty) return true;
      }
      // the subtree's prims (minus this placement's own stem) and its mass about the heart
      const sub: Prim[] = [];
      let m = 0,
        sx = 0,
        sy = 0;
      let bx0 = 1e18,
        by0 = 1e18,
        bx1 = -1e18,
        by1 = -1e18;
      for (let k = rec.lo; k < rec.hi; k++)
        for (const p of recs[k].own) {
          if (k === rec.lo && p.k === 1) continue;
          sub.push(p);
          if (p.k === 0) {
            m += p.m;
            sx += p.m * (p.x - C[0]);
            sy += p.m * (p.y - C[1]);
          }
          if (p.bx0 < bx0) bx0 = p.bx0;
          if (p.by0 < by0) by0 = p.by0;
          if (p.bx1 > bx1) bx1 = p.bx1;
          if (p.by1 > by1) by1 = p.by1;
        }
      if (!m) return true;
      // the subtree's own live prims (incl. this placement's stem): what pkHit ignores / self-tests
      const own = new Occ(sub.length * 2 + 8);
      for (let k = rec.lo; k < rec.hi; k++) for (const p of recs[k].own) occAdd(own, p, 1);
      const ox = rec.O[0],
        oy = rec.O[1];
      const dO = (p: Prim) =>
        p.k === 0 ? Math.hypot(p.x - ox, p.y - oy) - p.rho : Math.min(Math.hypot(p.x0 - ox, p.y0 - oy), Math.hypot(p.x1 - ox, p.y1 - oy));
      const dk = new Float64Array(sub.length);
      for (let i = 0; i < sub.length; i++) dk[i] = dO(sub[i]);
      const idx = Array.from(sub.keys()).sort((a, b) => dk[a] - dk[b]);
      const subS = idx.map((i) => sub[i]);
      const groups = bucketsOf(subS);
      const cost = (dx: number, dy: number) => PK.cwx * (2 * dx * sx + m * dx * dx) + PK.cwy * (2 * dy * sy + m * dy * dy);
      const psi0 = Math.atan2(pl.Ex, -pl.Ey),
        lmin = pkLmin(S),
        l0 = Math.hypot(pl.Ex, pl.Ey);
      const lmax = Math.max(l0 * 1.3, l0 + S0 * 3); // slide and swing, but never grow a long bare limb
      let best: { cc: number; Ex: number; Ey: number; dx: number; dy: number; st: { c: number[]; segs: Prim[] } } | null = null;
      // what this evaluation looked at: the subtree moved by every tested offset, and every tested stem
      let rdx0 = 0,
        rdx1 = 0,
        rdy0 = 0,
        rdy1 = 0,
        sbx0 = 1e18,
        sby0 = 1e18,
        sbx1 = -1e18,
        sby1 = -1e18;
      /** Test one candidate end point: null = blocked (PKC/self/frac set). */
      const test = (Ex: number, Ey: number, dx: number, dy: number) => {
        const st = mkStemWorld(rec, Ex, Ey, COMPACT_POOL);
        if (dx < rdx0) rdx0 = dx;
        if (dx > rdx1) rdx1 = dx;
        if (dy < rdy0) rdy0 = dy;
        if (dy > rdy1) rdy1 = dy;
        for (const g of st.segs) {
          if (g.bx0 < sbx0) sbx0 = g.bx0;
          if (g.by0 < sby0) sby0 = g.by0;
          if (g.bx1 > sbx1) sbx1 = g.bx1;
          if (g.by1 > sby1) sby1 = g.by1;
          // the self-test probes the stem moved by -d against the subtree
          if (g.bx0 - dx < sbx0) sbx0 = g.bx0 - dx;
          if (g.by0 - dy < sby0) sby0 = g.by0 - dy;
          if (g.bx1 - dx > sbx1) sbx1 = g.bx1 - dx;
          if (g.by1 - dy > sby1) sby1 = g.by1 - dy;
        }
        let pen = 0,
          frac = 1,
          self = false;
        PK_IG0 = rec.lo;
        PK_IG1 = rec.hi;
        PK_ONLY = false;
        OWN = own;
        for (let i = 0; i < st.segs.length && !pen; i++) {
          pen = pkHit(G, st.segs[i], 0, 0);
          if (pen) frac = Math.max(0.35, (i + 0.5) / st.segs.length);
        }
        if (!pen) {
          PK_ONLY = true;
          for (let i = 0; i < st.segs.length && !pen; i++) {
            pen = pkHit(G, st.segs[i], -dx, -dy);
            if (pen) self = true;
          }
          PK_ONLY = false;
        }
        if (!pen) pen = hitGroups(G, subS, groups, dx, dy);
        PK_IG0 = PK_IG1 = -1;
        OWN = null;
        return { pen, frac, self, st };
      };
      if (mode !== 0) {
        const r0 = test(pl.Ex, pl.Ey, 0, 0);
        if (!r0.pen) return true;
        if (mode === 2) return false;
        // push out along its own direction, then a little to either side; the first clear spot wins
        for (const da of [0, 0.08, -0.08, 0.16, -0.16, 0.3, -0.3, 0.5, -0.5]) {
          const ux = Math.sin(psi0 + da),
            uy = -Math.cos(psi0 + da);
          const uw = app(rec.A, ux, uy);
          let l = l0;
          for (let it = 0; it < 80 && l <= l0 * 2.5 + S0 * 30; it++) {
            const Ex = ux * l,
              Ey = uy * l;
            const ew = app(rec.A, Ex, Ey);
            const dx = rec.O[0] + ew[0] - rec.E[0],
              dy = rec.O[1] + ew[1] - rec.E[1];
            const r = test(Ex, Ey, dx, dy);
            if (!r.pen) {
              best = { cc: 0, Ex, Ey, dx, dy, st: { c: r.st.c, segs: r.st.segs.map((q) => clonePrim(q, 0, 0)) } };
              break;
            }
            const dot = PKC.x * uw[0] + PKC.y * uw[1],
              dd = PKC.x * PKC.x + PKC.y * PKC.y;
            const step = (-dot + Math.sqrt(Math.max(0, dot * dot - dd + PKC.need * PKC.need))) / r.frac;
            l += Math.max(0.6, Math.min(step + 0.15, 400), l * 0.015);
          }
          if (best) break;
        }
        if (!best) return false;
        CTX.resolved++;
      } else {
        const cands = [psi0];
        for (let a = -1.56; a <= 1.57; a += PK.cstep) if (Math.abs(a - psi0) > 0.03) cands.push(a);
        const wAng = (v: [number, number]) => Math.abs(Math.atan2(v[0], -v[1]));
        const wa0 = wAng(app(rec.A, Math.sin(psi0), -Math.cos(psi0)));
        for (const psi of cands) {
          const ux = Math.sin(psi),
            uy = -Math.cos(psi);
          const uw = app(rec.A, ux, uy);
          if (wAng(uw) > Math.max(PK.cmax, wa0) + 1e-9) continue; // never swing a limb out flatter than the budget
          let l = lmin;
          for (let it = 0; it < 120 && l <= lmax; it++) {
            const Ex = ux * l,
              Ey = uy * l;
            const ew = app(rec.A, Ex, Ey);
            const dx = rec.O[0] + ew[0] - rec.E[0],
              dy = rec.O[1] + ew[1] - rec.E[1];
            const cc = cost(dx, dy);
            if (cc >= (best ? best.cc : -1e-6)) break; // no better than staying put / the best so far
            const r = test(Ex, Ey, dx, dy);
            if (!r.pen) {
              best = { cc, Ex, Ey, dx, dy, st: { c: r.st.c, segs: r.st.segs.map((q) => clonePrim(q, 0, 0)) } };
              break;
            }
            if (r.self) break;
            const dot = PKC.x * uw[0] + PKC.y * uw[1],
              dd = PKC.x * PKC.x + PKC.y * PKC.y;
            const step = (-dot + Math.sqrt(Math.max(0, dot * dot - dd + PKC.need * PKC.need))) / r.frac;
            l += Math.max(0.6, Math.min(step + 0.15, 400), l * 0.015);
          }
        }
      }
      if (!best) {
        {
          evalAt[ri] = changes.length / 4;
          const r0 = ri * 4;
          reach[r0] = Math.min(bx0 + rdx0, sbx0) - REACH_PAD;
          reach[r0 + 1] = Math.min(by0 + rdy0, sby0) - REACH_PAD;
          reach[r0 + 2] = Math.max(bx1 + rdx1, sbx1) + REACH_PAD;
          reach[r0 + 3] = Math.max(by1 + rdy1, sby1) + REACH_PAD;
        }
        return true;
      }
      evalAt[ri] = -1;
      moved++;
      // the region this move touches: everything it kills and everything it adds
      const chg = [1e18, 1e18, -1e18, -1e18];
      const grow = (p: Prim) => {
        if (p.bx0 < chg[0]) chg[0] = p.bx0;
        if (p.by0 < chg[1]) chg[1] = p.by0;
        if (p.bx1 > chg[2]) chg[2] = p.bx1;
        if (p.by1 > chg[3]) chg[3] = p.by1;
      };
      for (let k = rec.lo; k < rec.hi; k++) for (const p of recs[k].own) grow(p);
      // apply: the subtree translates rigidly; this placement gets its new stem
      pl.Ex = best.Ex;
      pl.Ey = best.Ey;
      pl.c = best.st.c;
      for (const p of rec.own) {
        p.dead = true;
        occAdd(G.occ, p, -1);
        deadN++;
      }
      rec.own = [];
      for (const sg of best.st.segs) {
        sg.sub = rec.lo;
        rec.own.push(sg);
        pkAdd(G, sg);
      }
      for (let k = rec.lo; k < rec.hi; k++) {
        const r = recs[k];
        if (k > rec.lo) {
          r.O = [r.O[0] + best.dx, r.O[1] + best.dy];
          const nw: Prim[] = [];
          for (const p of r.own) {
            p.dead = true;
            occAdd(G.occ, p, -1);
            deadN++;
            const q = clonePrim(p, best.dx, best.dy);
            q.sub = k;
            q.m = p.m;
            nw.push(q);
            pkAdd(G, q);
          }
          r.own = nw;
        }
        r.E = [r.E[0] + best.dx, r.E[1] + best.dy];
      }
      if (S.term) {
        const d = discOf(rec, S);
        rec.own.push(d);
        pkAdd(G, d);
      }
      for (let k = rec.lo; k < rec.hi; k++) for (const p of recs[k].own) grow(p);
      logChange(chg);
      if (deadN > 2048) {
        G.purge();
        deadN = 0;
      }
      return true;
  };
  if (prev) {
    // Warm: the forks stand where the previous build left them. A subtree whose
    // content changed may now overlap a neighbour: push it out (deepest first),
    // escalating to the fork above when it cannot clear on its own.
    const parentOf = new Int32Array(recs.length).fill(-1);
    for (let i = 0; i < recs.length; i++) for (let k = recs[i].lo + 1; k < recs[i].hi; k++) if (recs[k].depth === recs[i].depth + 1) parentOf[k] = i;
    const deep = order.slice().reverse();
    let must = new Set<number>();
    for (let i = 0; i < recs.length; i++) if (!prev.has(recs[i].pl.S.sig)) must.add(i);
    for (let round = 0; round < 8 && must.size; round++) {
      const next = new Set<number>();
      for (const ri of deep) {
        if (!must.has(ri)) continue;
        if (!evalRec(ri, 1) && parentOf[ri] >= 0) next.add(parentOf[ri]);
      }
      must = next;
    }
    // nothing may overlap anything: otherwise the caller lays out cold
    for (let i = 0; i < recs.length && !CTX.warmFailed; i++) if (!evalRec(i, 2)) CTX.warmFailed = true;
  }
  for (let pass = 0; pass < totalPasses; pass++) {
    moved = 0;
    for (const ri of order) evalRec(ri, 0);
    (CTX.passLog || (CTX.passLog = [])).push([region, pass, moved, recs.length]);
    if (CTX.tick) CTX.tick(PHASE_SEARCH + (1 - PHASE_SEARCH) * ((pass + 1) / totalPasses) * (region === "c" ? 0.92 : 1));
  }
}

/** Every prim of the rigid subtree, moved by (dx, dy), against G (outside the
 * subtree): the first hit in the prims' own order. */
function hitGroups(G: Grid, P: Prim[], B: Buckets, dx: number, dy: number): number {
  CTX.stats.hits++;
  const g = ++B.gen * 2,
    of = B.of,
    flag = B.flag,
    box = B.box;
  for (let i = 0; i < P.length; i++) {
    const b = of[i];
    let f = flag[b];
    if (f < g) {
      const o = b * 4;
      f = flag[b] = g + (occMayBox(G, box[o] + dx, box[o + 1] + dy, box[o + 2] + dx, box[o + 3] + dy) ? 1 : 0);
    }
    if (f === g) continue; // the whole bucket is clear
    const v = pkHit(G, P[i], dx, dy);
    if (v) return v;
  }
  return 0;
}

// --- World placement ------------------------------------------------------

export interface EmitBranch {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
  cp: number[];
  w: number;
  wb: number;
  we: number;
  node: LNode | null;
  owner: LNode | null;
  virtual: boolean;
  own: boolean;
  term: Term | null;
  region: string;
  depth: number;
  id: number;
}

interface Xf {
  a: number;
  b: number;
  c: number;
  d: number;
  ox: number;
  oy: number;
}

/** World placement: shapes → leaf positions and branch records. T = local→
 * layout transform; `map` takes layout coordinates to the world (the root
 * system is mirrored below the ground). */
export function pkEmit(out: EmitBranch[], S: Shape, T: Xf, map: (x: number, y: number) => [number, number], region: string) {
  const tp = (x: number, y: number) => map(T.ox + T.a * x + T.b * y, T.oy + T.c * x + T.d * y);
  if (S.term) {
    const t = S.term;
    t.leaves = t.loc.map((p, i) => {
      const q = tp(p[0], t.cy0 + p[1]);
      return { x: q[0], y: q[1], file: t.files[i] };
    });
    return;
  }
  const own = S.kind === "node" ? S.node : S.owner;
  for (const pl of S.place) {
    const C = pl.S,
      c = pl.c;
    const p0 = tp(c[0], c[1]),
      p1 = tp(c[2], c[3]),
      p2 = tp(c[4], c[5]),
      p3 = tp(c[6], c[7]);
    const cp = [p0[0], p0[1], p1[0], p1[1], p2[0], p2[1], p3[0], p3[1]];
    const ex = cp[6],
      ey = cp[7];
    const node = C.kind === "node" || C.kind === "leaf" ? C.node! : null;
    const b: EmitBranch = {
      x0: cp[0],
      y0: cp[1],
      x1: ex,
      y1: ey,
      cp,
      w: C.w,
      wb: C.wb,
      we: C.we,
      node,
      owner: node ? node.parent : C.kind === "own" ? C.node! : C.owner || null,
      virtual: C.kind === "virtual",
      own: C.kind === "own",
      term: C.term || null,
      region,
      depth: (own ? own.depth : 0) + 1,
      id: out.length,
    };
    out.push(b);
    if (node) {
      node.F = [ex, ey];
      node.branch = b;
      node.w = C.w;
    }
    if (C.term) {
      C.term.F = [ex, ey];
      C.term.branch = b;
    }
    // child matrix B = R(rho)·diag(f, 1); composed A·B
    const cr = Math.cos(pl.rho),
      sr = Math.sin(pl.rho),
      f = pl.f || 1;
    const b00 = cr * f,
      b01 = -sr,
      b10 = sr * f,
      b11 = cr;
    const ox = T.ox + T.a * pl.Ex + T.b * pl.Ey,
      oy = T.oy + T.c * pl.Ex + T.d * pl.Ey;
    pkEmit(out, C, { a: T.a * b00 + T.b * b10, b: T.a * b01 + T.b * b11, c: T.c * b00 + T.d * b10, d: T.c * b01 + T.d * b11, ox, oy }, map, region);
  }
}
/** The top shape hangs on the trunk top (a lone item gets one short straight stem). */
export function pkEmitTop(out: EmitBranch[], S: Shape, owner: LNode, map: (x: number, y: number) => [number, number], region: string) {
  const T: Xf = { a: 1, b: 0, c: 0, d: 1, ox: 0, oy: 0 };
  if (S.kind === "virtual") return pkEmit(out, S, T, map, region);
  const wrap = emptyShape("virtual");
  wrap.owner = owner;
  wrap.place = [{ S, Ex: 0, Ey: -S0 * 2, rho: 0, f: 1, c: stemCP(0, -S0 * 2, 0) }];
  pkEmit(out, wrap, T, map, region);
}
