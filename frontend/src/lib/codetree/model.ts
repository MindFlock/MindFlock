/** Code tree — the model: folder tree → trunk, branches, clumps, leaves,
 * roots: tests (matched to the code they test) and every non-code file (docs,
 * CI, config, test data — the tree's support roots). Pure (no DOM): the worker builds it to run the layout search,
 * the main thread rebuilds it by REPLAYING the recorded placements (fast), and
 * then adds canvas geometry (geom.ts).
 *
 * Ported from the approved prototype (mindflock-prototypes/code-tree/model.js);
 * see layout.ts for the search itself. */

import {
  CROWN,
  CROWN_BIG,
  PK,
  ROOTS,
  S0,
  TAU,
  beginBuild,
  cmpStr,
  hashStr,
  newCtx,
  pkCompact,
  pkEmitTop,
  pkLayout,
  pkUse,
  rng,
  taperHW,
  STEM_SEG,
  type BuildCtx,
  type EmitBranch,
  type LNode,
  type Shape,
  type Term,
  type UnionRec,
  unionRecs,
  finalRecs,
} from "./layout";
import { leafAng, leafLen } from "./leafgeom";
import { growRoots } from "./roots";

export { S0, TAU };

export const dirOf = (p: string) => {
  const i = p.lastIndexOf("/");
  return i < 0 ? "" : p.slice(0, i);
};
export const baseOf = (p: string) => p.slice(p.lastIndexOf("/") + 1);
export function shortName(name: string): string {
  if (name.length <= 24 || !name.includes("/")) return name;
  const s = name.split("/");
  return s[0] + "/…/" + s[s.length - 1];
}

/** One file as the tree needs it (input.ts makes these from the snapshot). */
export interface RawFile {
  p: string;
  /** lines (or a proxy) */
  n: number;
  /** c = code, d = doc/config, a = asset */
  k: "c" | "d" | "a";
  t?: boolean;
  /** indices of the files this one imports */
  i?: number[];
  /** a test's target folder path ("" = repo root) */
  g?: string | null;
}
export interface RawRepo {
  name: string;
  files: RawFile[];
}

export type Place = "crown" | "root";

export interface TFile {
  id: number;
  path: string;
  name: string;
  lines: number;
  kind: "c" | "d" | "a";
  test: boolean;
  imports: number[];
  usedBy: number[];
  target: string | null | undefined;
  ghost: boolean;
  place: Place;
  node: TNode | null;
  leaf: Leaf | null;
  tests: TNode | null;
}

export interface TNode extends LNode {
  name: string;
  path: string;
  parent: TNode | null;
  kids: TNode[];
  files: TFile[];
  depth: number;
  kind: "crown" | "root";
  nFiles: number;
  nGhost: number;
  lines: number;
  id: number;
  term?: Term;
  F?: [number, number];
  branch?: Branch;
  w?: number;
  limb: number;
  // subtree bounds / centroid
  bx0: number;
  by0: number;
  bx1: number;
  by1: number;
  sx: number;
  sy: number;
  cnt: number;
  cx: number;
  cy: number;
  rad: number;
  // display
  disp?: string;
  core?: string | null;
  quiet?: boolean;
  label?: string;
  unmatched?: boolean;
  crownTwin?: TNode | null;
  /** a support root: non-code files (docs, CI, config, test data), not tests */
  support?: boolean;
  /** a support root of test data (fixtures, golden files) */
  data?: boolean;
  /** a root's repo folder ("frontend", "docs", "(repo root)"; "" = the tap root) */
  folder?: string;
  // per-frame flags (draw.ts)
  vHidden?: boolean;
  vFold?: boolean;
  vColl?: boolean;
  fAnc?: TNode | null;
  keep?: boolean;
  lit?: boolean;
}

export interface Leaf {
  x: number;
  y: number;
  file: TFile;
  len: number;
  ang: number;
  region: "crown" | "root";
  shade: number;
  id: number;
  term?: TTerm;
  rot?: number;
  /** leaf outline [bx,by, c1x,c1y, tx,ty, c2x,c2y] (geom.ts) */
  g?: number[];
}

export interface TTerm extends Term {
  node: TNode;
  files: TFile[];
  leaves: Leaf[];
  cx: number;
  cy: number;
  rad: number;
  branch?: Branch;
  tone?: number;
  // geometry (geom.ts)
  [k: string]: unknown;
}

export interface Branch extends EmitBranch {
  node: TNode | null;
  owner: TNode | null;
  term: TTerm | null;
  c: number[];
  len: number;
  pts: Array<[number, number, number, number]>;
  poly: Float32Array;
  bx0: number;
  by0: number;
  bx1: number;
  by1: number;
  subOf: TNode | null;
}

export interface GridItem {
  leaf?: Leaf;
  br?: Branch;
  p?: [number, number, number, number];
}

export interface Model {
  name: string;
  files: TFile[];
  crown: TNode;
  roots: TNode;
  nodeOf: Map<string, TNode>;
  rootNodeOf: Map<string, TNode>;
  branches: Branch[];
  leaves: Leaf[];
  terms: TTerm[];
  nodes: TNode[];
  kW: number;
  trunkW: number;
  groundY: number;
  Rtyp: number;
  bounds: { x0: number; y0: number; x1: number; y1: number };
  crownBounds: { x0: number; y0: number; x1: number; y1: number };
  limbCount: number;
  grid: { G: number; near(x: number, y: number, r: number): GridItem[] };
  nameCount: Map<string, number>;
  byPath: Map<string, TFile>;
  /** the placements this build chose (replayable) */
  layoutRec: LayoutRecord;
  [k: string]: unknown;
}

/** What a build records so the next one can replay it (same data) or warm
 * start from it (changed data). */
export interface LayoutRecord {
  v: number;
  files: number;
  /** placements per fork, in build order (post-compaction) */
  u: UnionRec[];
  /** bottom-up memo: union signature -> placements (pre-compaction) */
  memo: Array<[string, UnionRec]>;
  /** structure key -> final placements + content signature (warm start) */
  final: Array<[string, UnionRec, string]>;
  /** every shape signature of the build (what is unchanged next time) */
  sigs: string[];
  /** warm builds in a row since the last cold one */
  warmRun: number;
  /** fan splits and top-level orders (warm hysteresis) */
  splits: Array<[string, string]>;
  orders: Array<[string, string[]]>;
  /** the data signature this record was made for */
  sig: string;
}
export const LAYOUT_VERSION = 3;

export interface BuildOpts {
  /** an exact replay of a record made for this very data */
  replay?: LayoutRecord | null;
  /** a record made for earlier data: memo + compaction warm start */
  warm?: LayoutRecord | null;
  /** quantise widths so small size changes keep shapes (and the memo) intact */
  quantise?: boolean;
  /** compaction passes (default PK.compact; a warm start uses fewer) */
  passes?: number;
  tick?: (k: number) => void;
}

function mkNode(name: string, path: string, parent: TNode | null, kind: TNode["kind"]): TNode {
  return {
    name, path, parent, kids: [], files: [], depth: parent ? parent.depth + 1 : 0, kind, nFiles: 0, nGhost: 0, lines: 0, id: -1, limb: 0,
    bx0: 0, by0: 0, bx1: 0, by1: 0, sx: 0, sy: 0, cnt: 0, cx: 0, cy: 0, rad: 0,
  };
}

export function* allNodesGen(n: TNode): Generator<TNode> {
  yield n;
  for (const k of n.kids) yield* allNodesGen(k);
}
export function allNodes(n: TNode): TNode[] {
  return [...allNodesGen(n)];
}

export function parseFiles(raw: RawRepo): TFile[] {
  const F: TFile[] = raw.files.map((f, i) => ({
    id: i, path: f.p, name: baseOf(f.p), lines: f.n, kind: f.k, test: !!f.t, imports: f.i || [], usedBy: [], target: f.g,
    ghost: false, place: "crown" as Place, node: null, leaf: null, tests: null,
  }));
  for (const f of F) for (const j of f.imports) if (F[j]) F[j].usedBy.push(f.id);
  const top: Record<string, { n: number; c: number }> = {};
  for (const f of F) {
    if (f.test || !f.path.includes("/")) continue;
    const k = f.path.split("/")[0];
    const s = top[k] || (top[k] = { n: 0, c: 0 });
    s.n += f.lines + 1;
    if (f.kind === "c") s.c += f.lines + 1;
  }
  // the crown is code; tests and everything that is not code (docs, CI, config, test data, a mostly-non-code
  // folder) are roots
  for (const f of F) {
    if (f.test || f.kind !== "c") f.place = "root";
    else if (!f.path.includes("/")) f.place = "crown";
    else {
      const s = top[f.path.split("/")[0]];
      f.place = s.c / s.n < 0.3 ? "root" : "crown";
    }
  }
  return F;
}

/** A root file that is not a test: docs, CI, config, test data (a support root). */
export const isSupport = (f: TFile) => f.place === "root" && !(f.test && f.kind === "c");

/** A support limb with more files than this splits into clumps by its second path segment. */
export const SUPPORT_SPLIT = 24;
/** The tap root: the one limb under the trunk that holds every support group with no branch of its own (docs/,
 * .github/, the repo root's files, a tests folder's data) — a clump each. */
export const TAP_ROOT = "~";
export const TAP_LABEL = "docs, CI and config";

/** The folder a name leads with: n's top-level limb — or, on the tap root, its own clump ("docs", never "docs, CI
 * and config › docs"). */
export function nameTop(n: TNode): TNode {
  let top = n;
  while (top.parent && top.depth > 1 && !(top.depth === 2 && top.parent.folder === "")) top = top.parent;
  return top;
}

/** A generic folder tree; single-child chains are compacted (never the root). */
function buildTree(rootName: string, entries: Array<{ segs: string[]; file: TFile }>, kind: TNode["kind"]) {
  const root = mkNode(rootName, "", null, kind);
  const byPath = new Map<string, TNode>([["", root]]);
  for (const { segs, file } of entries) {
    let n = root,
      p = "";
    for (const s of segs) {
      p = p ? p + "/" + s : s;
      let c = byPath.get(p);
      if (!c) {
        c = mkNode(s, p, n, kind);
        n.kids.push(c);
        byPath.set(p, c);
      }
      n = c;
    }
    n.files.push(file);
  }
  const alias = new Map<string, string>();
  (function compact(n: TNode) {
    for (const k of n.kids) {
      while (k.files.length === 0 && k.kids.length === 1) {
        const c = k.kids[0];
        alias.set(k.path, c.path);
        k.name += "/" + c.name;
        k.path = c.path;
        k.kids = c.kids;
        k.files = c.files;
        for (const g of k.kids) g.parent = k;
      }
      compact(k);
    }
  })(root);
  const nodeOf = new Map<string, TNode>();
  (function fin(n: TNode, d: number) {
    n.depth = d;
    nodeOf.set(n.path, n);
    n.kids.sort((a, b) => cmpStr(a.name, b.name));
    n.files.sort((a, b) => b.lines - a.lines || cmpStr(a.name, b.name));
    n.nFiles = 0;
    n.nGhost = 0;
    n.lines = 0;
    for (const f of n.files) {
      if (f.ghost) n.nGhost++;
      else {
        n.nFiles++;
        n.lines += f.lines;
      }
    }
    for (const k of n.kids) {
      fin(k, d + 1);
      n.nFiles += k.nFiles;
      n.nGhost += k.nGhost;
      n.lines += k.lines;
    }
  })(root, 0);
  for (const [a] of alias) {
    // map intermediate paths onto the compacted node
    let p = a;
    while (alias.has(p)) p = alias.get(p)!;
    if (nodeOf.has(p)) nodeOf.set(a, nodeOf.get(p)!);
  }
  return { root, nodeOf };
}

/** Quantise a positive number to ~3 % log steps (stable under small edits). */
const qlog = (v: number) => (v > 0 ? Math.exp(Math.round(Math.log(v) / 0.03) * 0.03) : v);

/** The signature of the data a layout depends on (file set, sizes as the
 * layout sees them, tests' targets). */
export function dataSig(raw: RawRepo): string {
  let h = 2166136261;
  const mix = (s: string) => {
    for (let i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
  };
  mix(raw.name);
  for (const f of raw.files) mix("\u0000" + f.p + "\u0001" + f.n + "\u0001" + f.k + (f.t ? "t" : "") + "\u0001" + (f.g ?? ""));
  return raw.files.length + ":" + (h >>> 0).toString(36);
}

export function mixAng(a: number, b: number, t: number): number {
  let d = b - a;
  while (d > Math.PI) d -= TAU;
  while (d < -Math.PI) d += TAU;
  return a + d * t;
}

export function buildModel(raw: RawRepo, opts: BuildOpts = {}): Model {
  let replay = opts.replay && opts.replay.v === LAYOUT_VERSION ? opts.replay : null;
  if (replay && replay.files !== raw.files.length) replay = null;
  const warm = !replay && opts.warm && opts.warm.v === LAYOUT_VERSION ? opts.warm : null;
  const F = parseFiles(raw);
  const crownEntries: Array<{ segs: string[]; file: TFile }> = [];
  for (const f of F) if (f.place === "crown") crownEntries.push({ segs: dirOf(f.path) ? dirOf(f.path).split("/") : [], file: f });
  const crown = buildTree(raw.name, crownEntries, "crown");
  const M = {
    name: raw.name,
    files: F,
    crown: crown.root,
    nodeOf: crown.nodeOf,
    branches: [] as Branch[],
    leaves: [] as Leaf[],
    terms: [] as TTerm[],
  } as unknown as Model;
  for (const n of allNodes(M.crown)) for (const f of n.files) f.node = n;

  // branch widths follow sqrt(lines beneath); the scale is fixed from the crown's expected size before layout
  const nCrown = crownEntries.length;
  const Rest = Math.sqrt((Math.max(1, nCrown) * S0 * S0) / 1.6) * 1.15;
  const allLines = M.crown.lines + 40 * M.crown.nFiles;
  M.kW = (Rest * 0.05) / Math.sqrt(Math.max(1, allLines));
  if (opts.quantise) M.kW = qlog(M.kW);
  const widthOf = (lines: number, files: number) => Math.max(0.55, M.kW * Math.sqrt(lines + 40 * files));
  M.trunkW = widthOf(M.crown.lines, M.crown.nFiles);

  const ctx: BuildCtx = newCtx(M.kW);
  ctx.quantise = !!opts.quantise;
  if (replay) {
    ctx.cache = replay.u;
    // the fan splits / top order the recorded build chose (it may have kept a
    // warm start's): replaying them re-forms the same forks in the same order
    ctx.prevSplits = new Map(replay.splits || []);
    ctx.prevOrders = new Map(replay.orders || []);
  }
  if (warm) {
    ctx.memo = new Map(warm.memo);
    ctx.final = new Map(warm.final.map(([k, u, sg]) => [k, { u, sig: sg }]));
    ctx.prevSigs = new Set(warm.sigs);
    ctx.prevSplits = new Map(warm.splits);
    ctx.prevOrders = new Map(warm.orders);
  }
  // a warm build keeps every fork where it stood: no compaction, only the
  // overlap resolution (layout.ts pkCompact)
  ctx.passes = opts.passes ?? (warm ? 0 : PK.compact);
  ctx.tick = opts.tick;
  // progress: one tick per fork; a fan over k clumps has k - 1 forks
  let clumps = 0;
  for (const n of allNodes(M.crown)) if (n.files.length) clumps++;
  ctx.workTotal = Math.max(1, clumps + Math.ceil(F.filter((f) => f.place === "root").length / 6));
  beginBuild(ctx);

  const CR = nCrown >= CROWN_BIG.min ? Object.assign({}, CROWN, CROWN_BIG) : CROWN;
  pkUse(CR);
  // ---- crown: top-level folders alternate by weight around the trunk (biggest in the middle) ----
  const LC = pkLayout(
    M.crown,
    "crown",
    (items) => {
      const byW = items.slice().sort((a, b) => b.m - a.m || cmpStr(a.node!.name, b.node!.name));
      const L: Shape[] = [],
        R: Shape[] = [];
      byW.forEach((it, i) => (i % 2 ? R : L).push(it));
      return L.reverse().concat(R);
    },
    M.trunkW * 0.62
  );
  ctx.phase = 1;
  const tCompact0 = typeof performance !== "undefined" ? performance.now() : 0;
  if (LC.top && LC.top.base && !ctx.cache)
    pkCompact(LC.top, LC.top.base, [0, (-PK.heart * (CR.cheart || 1) * Math.sqrt(LC.top.area / Math.PI)) / 0.72], ctx.passes, "c");
  const tCompact = (typeof performance !== "undefined" ? performance.now() : 0) - tCompact0;
  const emitted: EmitBranch[] = M.branches as unknown as EmitBranch[];
  if (LC.top) {
    M.crown.F = [0, 0];
    M.crown.w = M.trunkW;
    pkEmitTop(emitted, LC.top, M.crown, (x, y) => [x, y], "crown");
  }

  // ---- roots: tests grouped by WHAT they test (mirrored under that branch); support roots (non-code) beside them ----
  const limbOfPath = (p: string | number | null | undefined): TNode | null => {
    // crown node at depth<=2 containing path p
    if (p == null) return null;
    let q = typeof p === "number" ? dirOf(F[p].path) : p;
    while (q && !M.nodeOf.has(q)) q = dirOf(q);
    if (!q) return null;
    let n: TNode | null = M.nodeOf.get(q)!;
    while (n && n.depth > 2) n = n.parent;
    return n && n.depth >= 1 ? n : null;
  };
  const rootEntriesArr: Array<{ segs: string[]; file: TFile }> = [];
  // a support file hangs on its folder's support limb: "S:frontend" (frontend's non-code files) beside "tests for
  // frontend", "D:frontend" (frontend's test data), or — with no branch of its own — as a clump of the tap root
  // under the trunk ("~/S:docs", "~/S:(repo root)", "~/D:tests"); a big group splits by its second path segment
  const supKey = (f: TFile) => (f.test ? "D:" : "S:") + (f.path.includes("/") ? f.path.split("/")[0] : "(repo root)");
  const supCount = new Map<string, number>();
  for (const f of F) if (isSupport(f)) supCount.set(supKey(f), (supCount.get(supKey(f)) || 0) + 1);
  for (const f of F) {
    if (f.place !== "root") continue;
    if (isSupport(f)) {
      const d = dirOf(f.path) ? dirOf(f.path).split("/") : [];
      const k = supKey(f);
      const twin = d.length ? M.nodeOf.get(d[0]) : undefined;
      const segs = [k].concat(supCount.get(k)! > SUPPORT_SPLIT && d.length > 1 ? [d[1]] : []);
      rootEntriesArr.push({ segs: twin && twin.depth === 1 ? segs : [TAP_ROOT].concat(segs), file: f });
      continue;
    }
    const n = limbOfPath(f.target);
    f.tests = n;
    let segs: string[];
    if (n) {
      const chain: string[] = [];
      let q: TNode | null = n;
      while (q && q.depth >= 1) {
        chain.unshift(q.name);
        q = q.parent;
      }
      segs = chain.map((nm, i) => (i === 0 ? "T:" : "") + nm);
    } else {
      const d = dirOf(f.path).split("/");
      const lm = M.nodeOf.get(d[0]);
      if (lm && lm.depth === 1) {
        f.tests = lm;
        segs = ["T:" + lm.name];
      } else segs = ["U:" + d[0]].concat(d.length > 1 ? [d[1]] : []);
    }
    rootEntriesArr.push({ segs, file: f });
  }
  const roots = buildTree("tests", rootEntriesArr, "root");
  M.roots = roots.root;
  M.rootNodeOf = roots.nodeOf;
  for (const n of allNodes(M.roots)) {
    if (n.depth === 0) continue;
    for (const f of n.files) f.node = n;
    const parts = n.path.split("/");
    const own = parts[0] === TAP_ROOT ? parts.slice(1) : parts;
    if (n.name.startsWith(TAP_ROOT + "/")) n.name = n.name.slice(TAP_ROOT.length + 1); // a tap root of one clump
    if (!own.length) {
      // the tap root itself
      n.support = true;
      n.crownTwin = null;
      n.folder = "";
      n.name = n.label = TAP_LABEL;
      continue;
    }
    const tag = own[0].slice(0, 2);
    n.unmatched = tag === "U:";
    n.support = tag === "S:" || tag === "D:";
    n.data = tag === "D:";
    const crownPath = own.map((s) => s.replace(/^[TUSD]:/, "")).join("/");
    n.folder = crownPath;
    const twin = n.unmatched ? null : M.nodeOf.get(crownPath) || null;
    // a support clump with no crown folder of its own (frontend/public) keeps its limb's twin
    n.crownTwin = n.support && !twin && n.depth > 1 ? n.parent!.crownTwin || null : twin;
    n.name = n.name.replace(/^[TUSD]:/, "");
    const at = shortName(crownPath);
    n.label = !n.support
      ? n.unmatched
        ? n.depth === 1
          ? "other tests · " + at + "/"
          : "tests · " + at
        : "tests for " + shortName(n.crownTwin ? n.crownTwin.path : n.name)
      : n.data
        ? n.crownTwin && n.depth === 1
          ? "test data"
          : "test data · " + at + "/"
        : n.crownTwin
          ? at + " · non-code"
          : crownPath === "(repo root)"
            ? "repo root files"
            : at;
  }
  const crownX = new Map<TNode, [number, number]>();
  for (const t of LC.terms)
    for (const l of t.leaves || []) {
      let n: TNode | null = t.node as TNode;
      while (n && n.depth > 1) n = n.parent;
      if (!n || n.depth < 1) continue;
      const e = crownX.get(n) || [0, 0];
      e[0] += l.x;
      e[1]++;
      crownX.set(n, e);
    }
  // roots spread wide and shallow: depth costs more than width
  pkUse(ROOTS);
  const LR = pkLayout(
    M.roots,
    "root",
    (items) =>
      items.slice().sort((A2, B2) => {
        const x = (it: Shape) => {
          const nd = it.node as TNode;
          if (it.kind === "own" || nd.unmatched) return 0;
          let q = nd.crownTwin || null;
          while (q && q.depth > 1) q = q.parent;
          const e = q ? crownX.get(q) : undefined;
          return e ? e[0] / e[1] : 0;
        };
        return x(A2) - x(B2) || cmpStr(A2.node!.name, B2.node!.name) || cmpStr(A2.node!.path, B2.node!.path);
      }),
    M.trunkW * 0.62
  );
  if (LR.top && LR.top.base && !ctx.cache) pkCompact(LR.top, LR.top.base, [0, (-PK.heart * Math.sqrt(LR.top.area / Math.PI)) / 0.72], warm ? 0 : 1, "r");
  if (ctx.warmFailed) {
    // a changed subtree could not be cleared without moving the rest: lay out cold
    const M2 = buildModel(raw, { ...opts, warm: null, replay: null });
    (M2.buildStats as Record<string, unknown>).warmFallback = true;
    return M2;
  }

  // ---- trunk height / ground ----
  let maxY = 0,
    minX = 0,
    maxX = 0,
    minY = 0;
  for (const t of LC.terms)
    for (const l of t.leaves || []) {
      maxY = Math.max(maxY, l.y);
      minX = Math.min(minX, l.x);
      maxX = Math.max(maxX, l.x);
      minY = Math.min(minY, l.y);
    }
  M.Rtyp = Math.max((maxX - minX) / 2, -minY) * 0.8;
  const groundY = Math.max(M.Rtyp * (CR.trunk || 0.42), maxY + S0 * 4, M.trunkW * 2.5);
  M.groundY = groundY;
  if (LR.top) {
    const ry = groundY + S0 * 1.6;
    M.roots.F = [0, ry];
    M.roots.w = widthOf(M.roots.lines, M.roots.nFiles) * 0.8;
    const n0 = M.branches.length;
    pkEmitTop(emitted, LR.top, M.roots, (x, y) => [x, ry - y], "root");
    for (let i = n0; i < M.branches.length; i++) {
      const b = M.branches[i];
      b.w *= 0.9;
      b.wb *= 0.9;
      b.we *= 0.9;
    }
  }

  const finishTerms = (terms: Term[], region: "crown" | "root") => {
    for (const t0 of terms) {
      const t = t0 as TTerm;
      t.region = region;
      let cx = 0,
        cy = 0;
      for (const l of t.leaves) {
        cx += l.x;
        cy += l.y;
      }
      t.cx = cx / Math.max(1, t.leaves.length);
      t.cy = cy / Math.max(1, t.leaves.length);
      t.rad = 0;
      for (const l of t.leaves) t.rad = Math.max(t.rad, Math.hypot(l.x - t.cx, l.y - t.cy));
      t.rad += t.s * 0.5;
      M.terms.push(t);
    }
  };
  finishTerms(LC.terms, "crown");
  finishTerms(LR.terms, "root");

  // ---- leaves (flat array) ----
  for (const t of M.terms) {
    const tb = t.branch as Branch | undefined;
    const bx = tb ? tb.x1 : t.cx,
      by = tb ? tb.y1 : t.cy;
    const r2 = rng(hashStr(t.node.path + "@"));
    // leaves fan out from the twig tip, leaning with the twig
    const hb = tb ? Math.atan2(tb.cp[7] - tb.cp[5], tb.cp[6] - tb.cp[4]) : -Math.PI / 2;
    for (const l of t.leaves) {
      const f = l.file;
      const fz = Math.min(1, Math.log10(Math.max(1, f.lines) + 1) / 3.6);
      if (t.region === "crown") {
        // crown leaves: sized to their clump's pitch and fanned out from the twig tip (leafgeom.ts)
        l.len = leafLen(f.lines, t.s);
        l.ang = leafAng(l.x, l.y, bx, by, hb, t.s, r2());
      } else {
        l.len = Math.min(t.s * 1.12, S0 * (0.5 + 0.62 * fz)) * 0.55;
        const radial = Math.atan2(l.y - by, l.x - bx);
        const ang = Math.hypot(l.y - by, l.x - bx) < t.s * 0.6 ? hb : mixAng(radial, hb, 0.4);
        l.ang = ang + (r2() - 0.5) * 0.7;
      }
      l.term = t;
      l.region = t.region as Leaf["region"];
      l.shade = r2() < 0.5 ? 0 : 1;
      f.leaf = l;
      l.id = M.leaves.length;
      M.leaves.push(l);
    }
    // a test (or a doc, a config file) is a root tip, not a leaf: roots.ts sets its nodule (len) and strand end direction (ang)
    if (t.region === "root") growRoots(t, bx, by, hb);
  }

  // ---- node ids, limb index, bounds ----
  M.nodes = [];
  for (const R of [M.crown, M.roots])
    for (const n of allNodes(R)) {
      n.id = M.nodes.length;
      M.nodes.push(n);
    }
  let limbI = 0;
  for (const n of M.crown.kids.slice().sort((a, b) => (a.F ? a.F[0] : 0) - (b.F ? b.F[0] : 0))) n.limb = limbI++;
  M.limbCount = limbI;
  let rl = 0;
  for (const n of M.roots.kids) n.limb = rl++;
  for (const n of M.nodes) {
    if (n.depth > 1) {
      let q: TNode | null = n;
      while (q && q.depth > 1) q = q.parent;
      n.limb = q && q.limb !== undefined ? q.limb : 0;
    }
  }
  // subtree bounds/centroid per node
  for (const n of M.nodes) {
    n.bx0 = 1e9;
    n.by0 = 1e9;
    n.bx1 = -1e9;
    n.by1 = -1e9;
    n.sx = 0;
    n.sy = 0;
    n.cnt = 0;
  }
  for (const l of M.leaves) {
    let n: TNode | null = l.file.node;
    while (n) {
      if (l.x < n.bx0) n.bx0 = l.x;
      if (l.x > n.bx1) n.bx1 = l.x;
      if (l.y < n.by0) n.by0 = l.y;
      if (l.y > n.by1) n.by1 = l.y;
      n.sx += l.x;
      n.sy += l.y;
      n.cnt++;
      n = n.parent;
    }
  }
  for (const n of M.nodes) {
    n.cx = n.sx / Math.max(1, n.cnt);
    n.cy = n.sy / Math.max(1, n.cnt);
    n.rad = Math.max(S0, Math.hypot(n.bx1 - n.bx0, n.by1 - n.by0) / 2);
  }
  M.crown.F = [0, 0];
  // world bounds
  let wx0 = 1e9,
    wy0 = 1e9,
    wx1 = -1e9,
    wy1 = -1e9;
  for (const l of M.leaves) {
    wx0 = Math.min(wx0, l.x);
    wx1 = Math.max(wx1, l.x);
    wy0 = Math.min(wy0, l.y);
    wy1 = Math.max(wy1, l.y);
  }
  if (!M.leaves.length) {
    wx0 = -S0 * 4;
    wx1 = S0 * 4;
    wy0 = -S0 * 4;
    wy1 = groundY;
  }
  M.bounds = { x0: wx0 - S0 * 6, y0: wy0 - S0 * 6, x1: wx1 + S0 * 6, y1: wy1 + S0 * 6 };
  M.crownBounds = { x0: minX, x1: maxX, y0: minY, y1: maxY };

  // ---- geometry: tapered branch outlines (world space) ----
  for (const b of M.branches) buildBranchGeom(b);
  for (const b of M.branches) b.subOf = b.node ? b.node.parent : b.owner;
  M.grid = buildGrid(M);
  assignDisplayNames(M);
  M.nameCount = new Map();
  for (const f of F) M.nameCount.set(f.name, (M.nameCount.get(f.name) || 0) + 1);
  M.byPath = new Map(F.map((f) => [f.path, f]));
  M.layoutRec = {
    v: LAYOUT_VERSION,
    files: raw.files.length,
    u: unionRecs(ctx),
    memo: [...ctx.memo],
    final: finalRecs(ctx),
    sigs: replay ? replay.sigs : ctx.sigs,
    splits: replay ? replay.splits : [...ctx.splits],
    orders: replay ? replay.orders : [...ctx.orders],
    warmRun: replay ? replay.warmRun : warm ? warm.warmRun + 1 : 0,
    sig: dataSig(raw),
  };
  M.buildStats = { memoHits: ctx.memoHits, unions: ctx.unions.length, replay: !!replay, warm: !!warm, resolved: ctx.resolved, compactMs: Math.round(tCompact), passLog: ctx.passLog, ...ctx.stats };
  if (ctx.cache && ctx.ci !== ctx.cache.length) M.replayMismatch = true;
  return M;
}

const BOILER = new Set(["src", "main", "java", "kotlin", "scala", "com", "org", "net", "io", "pkg", "internal"]);
const normSeg = (s: string) => s.toLowerCase().replace(/[-_.\s]/g, "");
/** Display names. A compacted chain such as "src/main" or
 * "java/com/acme/vnext/common" is scaffolding every Maven module shares: its
 * label is built from the segments that are NOT boilerplate and NOT already
 * said by an ancestor. A chain with nothing left to say is "quiet" — a
 * pass-through folder, not labelled at tree zoom. Names shared by several
 * folders get the nearest telling ancestor. */
export function assignDisplayNames(M: Model) {
  const crown = M.nodes.filter((n) => n.kind === "crown" && n.depth >= 1);
  const nonFinal = new Map<string, number>();
  for (const n of crown) {
    const s = n.name.split("/");
    for (let i = 0; i < s.length - 1; i++) nonFinal.set(normSeg(s[i]), (nonFinal.get(normSeg(s[i])) || 0) + 1);
  }
  const boiler = (s: string) => BOILER.has(s.toLowerCase()) || (nonFinal.get(normSeg(s)) || 0) >= 3;
  const said = (n: TNode) => {
    const out = new Set([normSeg(M.name)]);
    for (let q = n.parent; q && q.depth >= 1; q = q.parent) for (const s of q.name.split("/")) out.add(normSeg(s));
    return out;
  };
  for (const n of crown) {
    const segs = n.name.split("/");
    const anc = said(n);
    if (n.depth === 1 || (segs.length === 1 && !(n.depth > 2 && anc.has(normSeg(n.name))))) {
      n.quiet = false;
      n.disp = shortName(n.name);
      n.core = segs.length === 1 ? n.name : baseOf(n.name);
      continue;
    }
    const keep = segs.filter((s) => !boiler(s) && !anc.has(normSeg(s)));
    if (!keep.length) {
      n.quiet = true;
      n.core = null;
      n.disp = (n.parent && n.parent.depth >= 1 ? baseOf(n.parent.name) + "/" : "") + shortName(n.name);
      continue;
    }
    n.quiet = false;
    n.core = keep.join("/");
    n.disp = n.core;
  }
  const telling = (n: TNode) => {
    let q = n.parent;
    while (q && q.depth >= 1 && q.quiet) q = q.parent;
    return q && q.depth >= 1 ? q : null;
  };
  for (let round = 0; round < 2; round++) {
    const cnt = new Map<string, number>();
    for (const n of crown) if (!n.quiet) cnt.set(n.disp!, (cnt.get(n.disp!) || 0) + 1);
    let changed = false;
    for (const n of crown) {
      if (n.quiet || (cnt.get(n.disp!) || 0) < 2) continue;
      let q = telling(n);
      for (let k = 0; k < round && q; k++) q = telling(q);
      if (!q) continue;
      const pre = baseOf(q.core || q.name);
      if (!n.disp!.startsWith(pre + "/")) {
        n.disp = pre + "/" + n.disp;
        changed = true;
      }
    }
    if (!changed) break;
  }
  for (const n of crown) n.disp = shortName(n.disp!);
}

function buildBranchGeom(b: Branch) {
  const c = b.cp,
    SEG = STEM_SEG;
  const left: number[] = [],
    right: number[] = [],
    pts: Array<[number, number, number, number]> = [];
  const L = Math.hypot(b.x1 - b.x0, b.y1 - b.y0) || 1;
  for (let i = 0; i <= SEG; i++) {
    const t = i / SEG,
      mt = 1 - t;
    const x = mt * mt * mt * c[0] + 3 * mt * mt * t * c[2] + 3 * mt * t * t * c[4] + t * t * t * c[6];
    const y = mt * mt * mt * c[1] + 3 * mt * mt * t * c[3] + 3 * mt * t * t * c[5] + t * t * t * c[7];
    let tx = 3 * mt * mt * (c[2] - c[0]) + 6 * mt * t * (c[4] - c[2]) + 3 * t * t * (c[6] - c[4]);
    let ty = 3 * mt * mt * (c[3] - c[1]) + 6 * mt * t * (c[5] - c[3]) + 3 * t * t * (c[7] - c[5]);
    const tl = Math.hypot(tx, ty) || 1;
    tx /= tl;
    ty /= tl;
    const w = taperHW(b.wb, b.we, t);
    pts.push([x, y, tx, ty]);
    left.push(x - ty * w, y + tx * w);
    right.push(x + ty * w, y - tx * w);
  }
  b.c = c.slice();
  b.len = L;
  b.pts = pts;
  const poly = new Float32Array((SEG + 1) * 4);
  for (let i = 0; i <= SEG; i++) {
    poly[i * 2] = left[i * 2];
    poly[i * 2 + 1] = left[i * 2 + 1];
  }
  for (let i = 0; i <= SEG; i++) {
    const j = SEG - i;
    poly[(SEG + 1) * 2 + i * 2] = right[j * 2];
    poly[(SEG + 1) * 2 + i * 2 + 1] = right[j * 2 + 1];
  }
  b.poly = poly;
  let bx0 = 1e9,
    by0 = 1e9,
    bx1 = -1e9,
    by1 = -1e9;
  for (const q of pts) {
    bx0 = Math.min(bx0, q[0]);
    bx1 = Math.max(bx1, q[0]);
    by0 = Math.min(by0, q[1]);
    by1 = Math.max(by1, q[1]);
  }
  b.bx0 = bx0 - b.wb;
  b.bx1 = bx1 + b.wb;
  b.by0 = by0 - b.wb;
  b.by1 = by1 + b.wb;
}

function buildGrid(M: Model): Model["grid"] {
  const G = S0 * 4,
    cells = new Map<number, GridItem[]>();
  const key = (i: number, j: number) => i * 100003 + j;
  const add = (x: number, y: number, item: GridItem) => {
    const k = key(Math.floor(x / G), Math.floor(y / G));
    let a = cells.get(k);
    if (!a) cells.set(k, (a = []));
    a.push(item);
  };
  for (const l of M.leaves) add(l.x, l.y, { leaf: l });
  for (const b of M.branches) for (const p of b.pts) add(p[0], p[1], { br: b, p });
  return {
    G,
    near(x: number, y: number, r: number) {
      const out: GridItem[] = [];
      const i0 = Math.floor((x - r) / G),
        i1 = Math.floor((x + r) / G),
        j0 = Math.floor((y - r) / G),
        j1 = Math.floor((y + r) / G);
      for (let i = i0; i <= i1; i++)
        for (let j = j0; j <= j1; j++) {
          const a = cells.get(key(i, j));
          if (a) for (const it of a) out.push(it);
        }
      return out;
    },
  };
}

export function isUnder(n: TNode | null | undefined, anc: TNode): boolean {
  while (n) {
    if (n === anc) return true;
    n = n.parent;
  }
  return false;
}
