/** Code tree — label placement. A name is ATTACHED to the thing it names: it touches it (a leaf's tip, a clump's
 * rim where its twig comes in, a branch's wood, a 2 px dot on a fork) or is tethered to it by a short hairline.
 * Never "the nearest free spot": a name that cannot be attached unambiguously is not drawn this frame (the file
 * or folder keeps its name on hover and in its card).
 *
 * Pure functions over screen-space geometry: draw.ts gathers anchors, this module proposes candidate boxes in
 * rule order and picks the first that is whole on screen, free (a uniform-grid occupancy) and touches no other
 * leaf's outline. Anchors come through ONE function per thing (leafScreenGeom; rootAnchor in roots.ts) so the leaves /
 * roots work can re-point them without touching the placer. */

import { leafScreen } from "./leafgeom";
import type { Leaf, TNode } from "./model";

export type R4 = [number, number, number, number];
/** a convex quad, screen space: x0,y0, x1,y1, x2,y2, x3,y3 */
export type Quad = number[];

/** the gap (px) between a folder's anchor (rim, wood, fork dot) and its name */
export const GAP = 3;
/** a file name stands this far past its leaf's tip, so the 1 px hairline that ties it to the tip shows */
export const TIP_GAP = 6;
/** a gap wider than this gets a hairline (every leaf spot: TIP_GAP and BASE_GAP are both wider) */
export const HAIR_MIN = 4;
/** the base-side alternative stands this far off the leaf's base (clear of its stalk), so it always has a hairline */
export const BASE_GAP = 8;
/** a leaf name's box: text height + the halo plate's padding */
export const LEAF_H = 14;
export const LEAF_PAD = 3;
/** a name whose text is steeper than this is not run along its rim / branch (it would read sideways) */
export const RIM_MAX_TILT = 0.45;
export const BRANCH_MAX_TILT = 0.9;
/** safety net only: the most names one view may carry (the rules decide what fits long before this) */
export const LEAF_CAP = 150;
export const FOLDER_CAP = 140;

// ---------- anchors (the one re-pointable call per kind) ----------

export interface LeafGeom {
  /** the drawn tip / base, screen */
  tip: [number, number];
  base: [number, number];
  /** the way the leaf points (radians, screen: y down) */
  dir: number;
  /** half the drawn length, px */
  half: number;
  /** the drawn outline as a diamond (base, side, tip, side), screen */
  quad: Quad;
}

/** A leaf as it is drawn on screen: its tip, base, direction and outline — from `leafScreen` (leafgeom.ts), the one
 * geometry every drawn size goes through (the on-screen cap included). `minHalf` is the drawn half-length floor
 * (a changed leaf is drawn at >= 10.5 px half-length at every zoom; drawEdits scales it about its centre). */
export function leafScreenGeom(l: Leaf, z: number, toS: (x: number, y: number) => [number, number], minHalf = 0): LeafGeom {
  const g = leafScreen(l, z);
  const ca = Math.cos(g.dir),
    sa = Math.sin(g.dir);
  const [cx, cy] = toS(g.cx, g.cy);
  const half = Math.max(g.halfPx, minHalf);
  const tip: [number, number] = [cx + ca * half, cy + sa * half],
    base: [number, number] = [cx - ca * half, cy - sa * half];
  // the outline's widest point: half the quadratic control's offset (leafgeom.ts lensAt: w = len * 0.37 / 0.5,
  // 0.54 of the way from base to tip)
  const wk = l.region === "root" ? 0.5 : 0.37;
  const bw = half * wk;
  const mx = cx + ca * half * 0.08,
    my = cy + sa * half * 0.08;
  const quad = [base[0], base[1], mx - sa * bw, my + ca * bw, tip[0], tip[1], mx + sa * bw, my - ca * bw];
  return { tip, base, dir: g.dir, half, quad };
}

// ---------- candidates ----------

export interface LabelCand {
  /** axis-aligned bounds (occupancy, hit rects) */
  R: R4;
  /** the true box (rotated names), screen */
  q: Quad;
  /** the box centre and its rotation: the text is drawn centred here */
  cx: number;
  cy: number;
  ang: number;
  /** a hairline from the anchor to the box, when the gap is wider than HAIR_MIN */
  hair: [number, number, number, number] | null;
  /** a 2 px dot on the anchor (a name beside a fork) */
  dot: [number, number] | null;
  side: "tip" | "base" | "rim" | "twig" | "along" | "fork";
}

function boxQuad(cx: number, cy: number, w: number, h: number, ang: number): Quad {
  const c = Math.cos(ang),
    s = Math.sin(ang),
    ax = (c * w) / 2,
    ay = (s * w) / 2,
    bx = (-s * h) / 2,
    by = (c * h) / 2;
  return [cx - ax - bx, cy - ay - by, cx + ax - bx, cy + ay - by, cx + ax + bx, cy + ay + by, cx - ax + bx, cy - ay + by];
}
function quadR(q: Quad): R4 {
  return [Math.min(q[0], q[2], q[4], q[6]), Math.min(q[1], q[3], q[5], q[7]), Math.max(q[0], q[2], q[4], q[6]), Math.max(q[1], q[3], q[5], q[7])];
}
function cand(cx: number, cy: number, w: number, h: number, ang: number, side: LabelCand["side"], hair: LabelCand["hair"] = null, dot: LabelCand["dot"] = null): LabelCand {
  const q = boxQuad(cx, cy, w, h, ang);
  return { R: quadR(q), q, cx, cy, ang, hair, dot, side };
}
/** the point of an axis-aligned box nearest to (x, y) */
function nearestOnBox(R: R4, x: number, y: number): [number, number] {
  return [Math.max(R[0], Math.min(R[2], x)), Math.max(R[1], Math.min(R[3], y))];
}

/** An axis-aligned box of w x h that starts `gap` px past point p in direction a: reading outward (a box right of a
 * right-pointing tip, left of a left-pointing one, centred above / below an upright one). */
export function boxOutward(px: number, py: number, a: number, w: number, h: number, gap: number): R4 {
  const c = Math.cos(a),
    s = Math.sin(a);
  const qx = px + c * gap,
    qy = py + s * gap;
  if (Math.abs(c) >= 0.5) {
    // sideways: text starts at the tip, raised / lowered with the leaf's slant
    const yc = qy + (s * h) / 2;
    return c > 0 ? [qx, yc - h / 2, qx + w, yc + h / 2] : [qx - w, yc - h / 2, qx, yc + h / 2];
  }
  // up / down: centred on the tip, leaning the way the leaf leans
  const xc = qx + (c * w) / 2;
  return s < 0 ? [xc - w / 2, qy - h, xc + w / 2, qy] : [xc - w / 2, qy, xc + w / 2, qy + h];
}

/** The same box slid along the tip so that one of its corners (not its middle) is the point nearest the tip: raised
 * / lowered for a sideways tip, shifted right / left for an upright one. Still `gap` from the tip, still reading
 * outward — the name hangs off the tip by its corner, clearing a neighbour whose tip is level with this one. */
export function boxOutwardSlid(px: number, py: number, a: number, w: number, h: number, gap: number): R4[] {
  const c = Math.cos(a),
    s = Math.sin(a);
  const qx = px + c * gap,
    qy = py + s * gap;
  if (Math.abs(c) >= 0.5) {
    const x0 = c > 0 ? qx : qx - w;
    // the side the leaf leans to first (a tip pointing up-right hangs its name above the tip line first)
    const up: R4 = [x0, qy - h, x0 + w, qy],
      dn: R4 = [x0, qy, x0 + w, qy + h];
    return s <= 0 ? [up, dn] : [dn, up];
  }
  const y0 = s < 0 ? qy - h : qy;
  const rt: R4 = [qx, y0, qx + w, y0 + h],
    lf: R4 = [qx - w, y0, qx, y0 + h];
  return c >= 0 ? [rt, lf] : [lf, rt];
}

/** Rule 1: a leaf's name at its TIP, reading outward — centred on the tip, then slid to hang off it by a corner;
 * the one alternative is the BASE side (reading away from the tip), BASE_GAP off the base. Every spot stands
 * clear of its anchor and is tied back to it by a hairline. Box = text width + LEAF_PAD each side. */
export function candidatesForLeaf(a: LeafGeom, tw: number, h = LEAF_H, gap = TIP_GAP, baseGap = BASE_GAP): LabelCand[] {
  const w = tw + 2 * LEAF_PAD;
  const out: LabelCand[] = [];
  const t = boxOutward(a.tip[0], a.tip[1], a.dir, w, h, gap);
  out.push(cand((t[0] + t[2]) / 2, (t[1] + t[3]) / 2, w, h, 0, "tip", gap > HAIR_MIN ? hairTo(a.tip, t) : null));
  for (const r of boxOutwardSlid(a.tip[0], a.tip[1], a.dir, w, h, gap)) out.push(cand((r[0] + r[2]) / 2, (r[1] + r[3]) / 2, w, h, 0, "tip", gap > HAIR_MIN ? hairTo(a.tip, r) : null));
  const b = boxOutward(a.base[0], a.base[1], a.dir + Math.PI, w, h, baseGap);
  out.push(cand((b[0] + b[2]) / 2, (b[1] + b[3]) / 2, w, h, 0, "base", baseGap > HAIR_MIN ? hairTo(a.base, b) : null));
  return out;
}
function hairTo(p: [number, number], R: R4): [number, number, number, number] {
  const [x, y] = nearestOnBox(R, p[0], p[1]);
  return [p[0], p[1], x, y];
}

/** The rim point a clump hangs from: where its own stem (screen polyline, base -> clump) crosses the clump's rim;
 * a stem wholly inside the rim uses its base; no stem at all hangs it from below. */
export function rimPoint(cx: number, cy: number, r: number, stem: Array<[number, number]>): { u: [number, number]; q: [number, number]; s: [number, number] } {
  let px = cx,
    py = cy + r + 1;
  for (let k = stem.length - 1; k >= 0; k--) {
    const d = Math.hypot(stem[k][0] - cx, stem[k][1] - cy);
    if (d > r) {
      // interpolate to the crossing on the segment k -> k+1
      if (k < stem.length - 1) {
        const [x0, y0] = stem[k],
          [x1, y1] = stem[k + 1],
          d1 = Math.hypot(x1 - cx, y1 - cy),
          f = Math.max(0, Math.min(1, (d - r) / Math.max(1e-6, d - d1)));
        px = x0 + (x1 - x0) * f;
        py = y0 + (y1 - y0) * f;
        if (Math.hypot(px - cx, py - cy) < 1e-3) {
          px = x0;
          py = y0;
        }
      } else {
        px = stem[k][0];
        py = stem[k][1];
      }
      break;
    }
    if (k === 0) {
      px = stem[0][0];
      py = stem[0][1];
    }
  }
  let ux = px - cx,
    uy = py - cy;
  const ul = Math.hypot(ux, uy);
  if (ul < 1e-6) {
    ux = 0;
    uy = 1;
  } else {
    ux /= ul;
    uy /= ul;
  }
  return { u: [ux, uy], q: [cx + ux * r, cy + uy * r], s: [px, py] };
}

/** readable rotation: text never upside down */
export function readable(a: number): number {
  while (a > Math.PI / 2) a -= Math.PI;
  while (a <= -Math.PI / 2) a += Math.PI;
  return a;
}

/** Rule 3, a clump (leaf folder): just outside the rim at the point where its twig comes in, baseline tangent to the
 * rim (flat when the tangent is steep); else ON the twig, straddling the stem just outside the rim. */
export function candidatesForClump(cx: number, cy: number, r: number, stem: Array<[number, number]>, w: number, h: number, gap = GAP): LabelCand[] {
  const { u, q } = rimPoint(cx, cy, r, stem);
  const out: LabelCand[] = [];
  const tan = readable(Math.atan2(u[1], u[0]) + Math.PI / 2);
  if (Math.abs(tan) <= RIM_MAX_TILT) {
    out.push(cand(q[0] + u[0] * (gap + h / 2), q[1] + u[1] * (gap + h / 2), w, h, tan, "rim"));
  } else {
    const ext = (Math.abs(u[0]) * w) / 2 + (Math.abs(u[1]) * h) / 2;
    out.push(cand(q[0] + u[0] * (gap + ext), q[1] + u[1] * (gap + ext), w, h, 0, "rim"));
  }
  // on the twig: the stem runs through the box, which sits just past the rim
  const d = r + h / 2 + 2;
  out.push(cand(cx + u[0] * d, cy + u[1] * d, w, h, 0, "twig"));
  return out;
}

/** Rule 3, a branch (a folder with sub-folders): ALONG its own wood like a river's name (the stem is long enough on
 * screen and the run is straight enough), on the outer side then the inner; else beside its fork, touching a 2 px
 * dot laid on the fork. `pts` = the stem (screen) with its half-width there; `fork` = where it splits. */
export function candidatesForBranch(pts: Array<[number, number, number]>, fork: [number, number], w: number, h: number, gap = GAP, along = true): LabelCand[] {
  const out: LabelCand[] = [];
  if (along && pts.length >= 2) {
    const acc = [0];
    for (let i = 1; i < pts.length; i++) acc.push(acc[i - 1] + Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]));
    const L = acc[acc.length - 1];
    const at = (s: number): [number, number, number] => {
      let i = 1;
      while (i < acc.length - 1 && acc[i] < s) i++;
      const f = Math.max(0, Math.min(1, (s - acc[i - 1]) / Math.max(1e-6, acc[i] - acc[i - 1])));
      return [pts[i - 1][0] + (pts[i][0] - pts[i - 1][0]) * f, pts[i - 1][1] + (pts[i][1] - pts[i - 1][1]) * f, pts[i - 1][2] + (pts[i][2] - pts[i - 1][2]) * f];
    };
    if (L >= w + 12)
      for (const t of [0.5, 0.35, 0.65]) {
        const s = L * t;
        if (s - w / 2 < 6 || s + w / 2 > L - 6) continue;
        const a = at(s - w / 2),
          b = at(s + w / 2),
          m = at(s);
        const ang = readable(Math.atan2(b[1] - a[1], b[0] - a[0]));
        if (Math.abs(ang) > BRANCH_MAX_TILT) continue;
        // the run under the name is straight: no stem point strays more than a third of the text height off the chord
        const cl = Math.hypot(b[0] - a[0], b[1] - a[1]) || 1;
        let sag = 0;
        for (let i = 0; i < pts.length; i++) {
          if (acc[i] <= s - w / 2 || acc[i] >= s + w / 2) continue;
          sag = Math.max(sag, Math.abs(((b[0] - a[0]) * (a[1] - pts[i][1]) - (a[0] - pts[i][0]) * (b[1] - a[1])) / cl));
        }
        if (sag > h / 3) continue;
        let nx = -Math.sin(ang),
          ny = Math.cos(ang);
        if (ny > 0) {
          nx = -nx;
          ny = -ny;
        }
        const off = m[2] + 1 + h / 2;
        out.push(cand(m[0] + nx * off, m[1] + ny * off, w, h, ang, "along"), cand(m[0] - nx * off, m[1] - ny * off, w, h, ang, "along"));
      }
  }
  // beside the fork, touching its dot: right, left, above, below, then the four diagonals (a tower or a nest
  // beside the fork takes the straight spots)
  const [fx, fy] = fork,
    d = 2 + gap;
  const dot: [number, number] = [fx, fy];
  out.push(
    cand(fx + d + w / 2, fy, w, h, 0, "fork", null, dot),
    cand(fx - d - w / 2, fy, w, h, 0, "fork", null, dot),
    cand(fx, fy - d - h / 2, w, h, 0, "fork", null, dot),
    cand(fx, fy + d + h / 2, w, h, 0, "fork", null, dot)
  );
  const dd = d * Math.SQRT1_2; // the box corner touches the dot at the same distance as the straight spots
  for (const [sx, sy] of [
    [1, -1],
    [-1, -1],
    [1, 1],
    [-1, 1],
  ])
    out.push(cand(fx + sx * (dd + w / 2), fy + sy * (dd + h / 2), w, h, 0, "fork", null, dot));
  return out;
}

// ---------- occupancy: a uniform screen grid ----------

/** Rects already taken (HUD panels, birds, nests, signs, changes, names): a uniform grid, so a frame of n names is
 * O(n), not O(n^2). Rects outside the canvas clamp into its edge cells. */
export class Occupancy {
  private cells: R4[][];
  private cols: number;
  private rows: number;
  readonly all: R4[] = [];
  constructor(
    readonly W: number,
    readonly H: number,
    readonly cell = 48
  ) {
    this.cols = Math.max(1, Math.ceil(W / cell));
    this.rows = Math.max(1, Math.ceil(H / cell));
    this.cells = Array.from({ length: this.cols * this.rows }, () => []);
  }
  private span(x0: number, y0: number, x1: number, y1: number): [number, number, number, number] {
    const c = this.cell,
      cl = (v: number, n: number) => Math.max(0, Math.min(n - 1, Math.floor(v / c)));
    return [cl(x0, this.cols), cl(y0, this.rows), cl(x1, this.cols), cl(y1, this.rows)];
  }
  /** free of every taken rect — except those wholly inside `own` (a thing's own reserved room, e.g. its ring) */
  fits = (x0: number, y0: number, x1: number, y1: number, own?: R4): boolean => {
    const [i0, j0, i1, j1] = this.span(x0, y0, x1, y1);
    for (let j = j0; j <= j1; j++)
      for (let i = i0; i <= i1; i++)
        for (const o of this.cells[j * this.cols + i]) {
          if (!(x1 > o[0] && x0 < o[2] && y1 > o[1] && y0 < o[3])) continue;
          if (own && o[0] >= own[0] && o[1] >= own[1] && o[2] <= own[2] && o[3] <= own[3]) continue;
          return false;
        }
    return true;
  };
  add = (x0: number, y0: number, x1: number, y1: number): void => {
    const r: R4 = [x0, y0, x1, y1];
    this.all.push(r);
    const [i0, j0, i1, j1] = this.span(x0, y0, x1, y1);
    for (let j = j0; j <= j1; j++) for (let i = i0; i <= i1; i++) this.cells[j * this.cols + i].push(r);
  };
}

// ---------- the leaves on screen: rule 5's "touches another leaf's outline" ----------

function sepAxis(a: Quad, b: Quad): boolean {
  // true when some edge normal of a separates a and b
  const n = a.length / 2;
  for (let i = 0; i < n; i++) {
    const x0 = a[2 * i],
      y0 = a[2 * i + 1],
      x1 = a[(2 * i + 2) % a.length],
      y1 = a[(2 * i + 3) % a.length];
    const nx = y0 - y1,
      ny = x1 - x0;
    let amin = Infinity,
      amax = -Infinity,
      bmin = Infinity,
      bmax = -Infinity;
    for (let k = 0; k < a.length; k += 2) {
      const p = a[k] * nx + a[k + 1] * ny;
      amin = Math.min(amin, p);
      amax = Math.max(amax, p);
    }
    for (let k = 0; k < b.length; k += 2) {
      const p = b[k] * nx + b[k + 1] * ny;
      bmin = Math.min(bmin, p);
      bmax = Math.max(bmax, p);
    }
    if (amax <= bmin || bmax <= amin) return true;
  }
  return false;
}
/** two convex polygons overlap (separating-axis test) */
export function convexOverlap(a: Quad, b: Quad): boolean {
  return !sepAxis(a, b) && !sepAxis(b, a);
}

export interface LeafShape<T = Leaf> {
  leaf: T;
  quad: Quad;
  R: R4;
}

/** The leaves drawn on screen this frame, in a uniform grid: does a name's box touch any of them (besides its own)? */
export class LeafField<T = Leaf> {
  private items: Array<LeafShape<T>> = [];
  private cells: Array<Array<LeafShape<T>>>;
  private cols: number;
  private rows: number;
  constructor(
    W: number,
    H: number,
    private cell = 48
  ) {
    this.cols = Math.max(1, Math.ceil(W / cell));
    this.rows = Math.max(1, Math.ceil(H / cell));
    this.cells = Array.from({ length: this.cols * this.rows }, () => []);
  }
  get size(): number {
    return this.items.length;
  }
  add(leaf: T, quad: Quad): void {
    const R = quadR(quad);
    if (R[2] < 0 || R[3] < 0 || R[0] > this.cols * this.cell || R[1] > this.rows * this.cell) return;
    const it = { leaf, quad, R };
    this.items.push(it);
    const c = this.cell,
      cl = (v: number, n: number) => Math.max(0, Math.min(n - 1, Math.floor(v / c)));
    for (let j = cl(R[1], this.rows); j <= cl(R[3], this.rows); j++) for (let i = cl(R[0], this.cols); i <= cl(R[2], this.cols); i++) this.cells[j * this.cols + i].push(it);
  }
  /** the first leaf (not excluded) whose outline the box q touches, or null */
  touches(q: Quad, skip: (l: T) => boolean, pad = 1): T | null {
    const R = quadR(q),
      x0 = R[0] - pad,
      y0 = R[1] - pad,
      x1 = R[2] + pad,
      y1 = R[3] + pad;
    const c = this.cell,
      cl = (v: number, n: number) => Math.max(0, Math.min(n - 1, Math.floor(v / c)));
    // grow the box by `pad` (around its centre) so a name merely grazing a leaf counts as touching it
    const mx = (q[0] + q[2] + q[4] + q[6]) / 4,
      my = (q[1] + q[3] + q[5] + q[7]) / 4;
    const g: Quad = q.map((v, i) => {
      const m = i % 2 ? my : mx,
        d = v - m;
      return m + d + Math.sign(d) * pad;
    });
    const seen = new Set<LeafShape<T>>();
    for (let j = cl(y0, this.rows); j <= cl(y1, this.rows); j++)
      for (let i = cl(x0, this.cols); i <= cl(x1, this.cols); i++)
        for (const it of this.cells[j * this.cols + i]) {
          if (seen.has(it)) continue;
          seen.add(it);
          if (it.R[2] < x0 || it.R[0] > x1 || it.R[3] < y0 || it.R[1] > y1) continue;
          if (skip(it.leaf)) continue;
          if (convexOverlap(g, it.quad)) return it.leaf;
        }
    return null;
  }
}

// ---------- placement ----------

export interface Viewport {
  W: number;
  H: number;
  /** keep this far inside the canvas edge */
  margin?: number;
}

/** Rule 5's edge: whole inside the canvas (a margin), else not drawn. */
export function insideView(R: R4, vp: Viewport): boolean {
  const m = vp.margin ?? 2;
  return R[0] >= m && R[1] >= m && R[2] <= vp.W - m && R[3] <= vp.H - m;
}

/** the gap (px) kept between a name and anything already taken: two names never abut and read as one */
export const GUTTER = 2;

/** The first candidate (in rule order) that is whole on screen, free (with a GUTTER), touching no other leaf, and
 * accepted by the caller's extra checks; null = the name is not drawn. */
export function placeLabel<T>(
  cands: LabelCand[],
  occ: Pick<Occupancy, "fits">,
  vp: Viewport,
  leaves?: { field: LeafField<T>; skip: (l: T) => boolean } | null,
  accept?: (c: LabelCand) => boolean,
  gutter = GUTTER
): LabelCand | null {
  for (const c of cands) {
    if (!insideView(c.R, vp)) continue;
    if (!occ.fits(c.R[0] - gutter, c.R[1] - gutter, c.R[2] + gutter, c.R[3] + gutter)) continue;
    if (leaves && leaves.field.touches(c.q, leaves.skip)) continue;
    if (accept && !accept(c)) continue;
    return c;
  }
  return null;
}

export interface LabelItem<K> {
  key: K;
  cands: LabelCand[];
  skip?: (l: Leaf) => boolean;
}

/** Many names, already in priority order: each takes its first fitting candidate and reserves it; at most `cap`. */
export function placeLabels<K>(items: Array<LabelItem<K>>, occ: Occupancy, vp: Viewport, field?: LeafField | null, cap = Infinity): Array<{ key: K; c: LabelCand }> {
  const out: Array<{ key: K; c: LabelCand }> = [];
  for (const it of items) {
    if (out.length >= cap) break;
    const c = placeLabel(it.cands, occ, vp, field ? { field, skip: it.skip || (() => false) } : null);
    if (!c) continue;
    occ.add(c.R[0], c.R[1], c.R[2], c.R[3]);
    out.push({ key: it.key, c });
  }
  return out;
}

// ---------- the name column: a hovered clump's names that found no room at their tips (rule 6) ----------

/** a column row's height and the gap between the clump's leaves and the column */
export const COL_ROW = 16;
export const COL_GAP = 14;

/** Where a hovered clump's name column stands: a strip `w` wide beside the clump (`box` = its leaves on screen),
 * right of it when the free canvas has room there, else left, else — a clump wider than the view — at the edge of
 * the canvas on the roomier side, over the clump's own leaves (`force` picks the side: a clump wider than the view
 * gets a strip at each edge, see `columnStrips`). Vertically the tallest run of free canvas the strip
 * has between HUD panels (and below `minY`: a root's names stay underground). Null when it has no height. */
export interface ColumnStrip {
  side: 1 | -1;
  x0: number;
  x1: number;
  y0: number;
  y1: number;
  /** the strip stands over the clump (it was too wide for the view): the column gets a plate */
  over: boolean;
}
export function columnStrip(box: R4, w: number, free: R4, hud: R4[] = [], minY = -Infinity, gap = COL_GAP, force?: 1 | -1): ColumnStrip | null {
  const roomR = free[2] - 4 - (box[2] + gap),
    roomL = box[0] - gap - (free[0] + 4);
  const side: 1 | -1 = force || (roomR >= w ? 1 : roomL >= w ? -1 : roomR >= roomL ? 1 : -1);
  const over = roomR < w && roomL < w;
  const x0 = side > 0 ? Math.min(box[2] + gap, free[2] - 4 - w) : Math.max(box[0] - gap - w, free[0] + 4);
  const x1 = x0 + w;
  const lo = Math.max(free[1] + 4, minY),
    hi = free[3] - 4;
  const cuts = hud.filter((r) => r[2] > x0 && r[0] < x1 && r[3] > lo && r[1] < hi).sort((a, b) => a[1] - b[1]);
  let y0 = lo,
    y1 = lo,
    at = lo;
  for (const r of [...cuts, [x0, hi, x1, hi] as R4]) {
    if (r[1] - at > y1 - y0) {
      y0 = at;
      y1 = r[1];
    }
    at = Math.max(at, r[3]);
  }
  return y1 - y0 >= COL_ROW ? { side, x0, x1, y0, y1, over } : null;
}

/** The strips for a hovered clump: one beside it — or, when it is wider than the view, one at EACH edge of the
 * canvas (each tip goes to the nearer one: twice the rows, half the hairline). */
export function columnStrips(box: R4, w: number, free: R4, hud: R4[] = [], minY = -Infinity): ColumnStrip[] {
  const one = columnStrip(box, w, free, hud, minY);
  if (!one || !one.over) return one ? [one] : [];
  const other = columnStrip(box, w, free, hud, minY, COL_GAP, one.side > 0 ? -1 : 1);
  return other && (other.x1 <= one.x0 - 40 || other.x0 >= one.x1 + 40) ? [one, other] : [one];
}

export interface NameColumn {
  side: 1 | -1;
  /** one row per shown item: its index in the input, its box and its text's middle */
  rows: Array<{ i: number; R: R4; y: number }>;
  /** items left out for lack of height (the column's last row says "+N more") */
  more: number;
  /** the "+N more" row's box, when there is one */
  moreR: R4 | null;
}

/** Rule 6's column rows, in the strip: one per item (in priority order, the first ones keep their rows when the
 * strip is too short), stacked in TIP order — so the hairlines from tips to rows never cross — each as near its
 * tip's height as the rows above it allow and sliding down past anything `fits` says is taken. */
export function nameColumn(items: Array<{ tx: number; ty: number; w: number }>, S: ColumnStrip, fits: (R: R4) => boolean = () => true, row = COL_ROW): NameColumn | null {
  if (!items.length) return null;
  const rowAt = (w: number, y: number): R4 => (S.side > 0 ? [S.x0, y - row / 2 + 1, S.x0 + w, y + row / 2 - 1] : [S.x1 - w, y - row / 2 + 1, S.x1, y + row / 2 - 1]);
  // how many rows the strip holds (a "+N more" row takes the last one when they can't all have one)
  const cap = Math.floor((S.y1 - S.y0) / row);
  const keep = Math.min(items.length, items.length > cap ? cap - 1 : cap);
  if (keep < 1) return null;
  const order = items
    .slice(0, keep)
    .map((it, i) => ({ i, ty: it.ty }))
    .sort((a, b) => a.ty - b.ty);
  const top = S.y0 + row / 2,
    bot = S.y1 - row / 2 - (items.length > keep ? row : 0);
  // pack upward from the bottom first (no row lower than its tip, the run inside the strip), then lay the rows
  // downward from there, each sliding past anything taken; a row with no room left is counted in "+N more"
  const ideal: number[] = order.map((o) => Math.max(top, Math.min(bot, o.ty)));
  for (let k = ideal.length - 2; k >= 0; k--) ideal[k] = Math.min(ideal[k], ideal[k + 1] - row);
  const lay = (want: number[]) => {
    const out: NameColumn["rows"] = [];
    let yy0 = -Infinity;
    for (let k = 0; k < order.length; k++) {
      const w = Math.min(items[order[k].i].w, S.x1 - S.x0);
      let yy = Math.max(want[k], top, yy0 + row);
      while (yy <= bot && !fits(rowAt(w, yy))) yy += 2;
      if (yy > bot) break;
      out.push({ i: order[k].i, R: rowAt(w, yy), y: yy });
      yy0 = yy;
    }
    return out;
  };
  let rows = lay(ideal);
  // taken spots pushed the last rows out: pack them from the top instead (the rows still in tip order)
  if (rows.length < order.length) {
    const packed = lay(ideal.map(() => top));
    if (packed.length > rows.length) rows = packed;
  }
  const y = rows.length ? rows[rows.length - 1].y : -Infinity;
  if (!rows.length) return null;
  const more = items.length - rows.length;
  let moreR: R4 | null = null;
  if (more) {
    if (y + row > S.y1 - row / 2) rows.pop(); // (no room under the last row: it gives its place to the count)
    moreR = rowAt(70, rows.length ? rows[rows.length - 1].y + row : top);
  }
  return { side: S.side, rows, more: items.length - rows.length, moreR };
}

// ---------- which folders get a name, and how short it may get (F4, F5) ----------

/** A folder name with no room by its own cluster is SHORTENED, never moved: the full path, then the folder's own
 * name. Only a folder that MUST show (a nest, the open card, a rule) may shrink further, to its bare count chip ("")
 * — a "●86" alone names nothing (the bird index lists the folder anyway). */
export function folderNameVariants(name: string, plain: string, hasCount: boolean, must: boolean): string[] {
  const out = [name];
  if (plain !== name) out.push(plain);
  if (hasCount && must) out.push("");
  return out;
}

/** n carries exactly its parent's changes, as the parent's one lit sub-folder (`lit`: changes under each folder) */
export function sameAsParent(n: TNode, lit: Map<TNode, { n: number }>): boolean {
  const p = n.parent,
    ln = lit.get(n),
    lp = p && lit.get(p);
  return !!(p && p.depth >= 1 && ln && lp && ln.n === lp.n && p.kids && p.kids.filter((k) => lit.has(k)).length === 1);
}

/** One pill per chain: the top-level folder a chain of equal counts runs down from ("frontend" for frontend › src
 * ●121), which names the whole chain — null when the chain starts deeper (that one is named by its lit sub-folder). */
export function chainTop1(n: TNode, lit: Map<TNode, { n: number }>): TNode | null {
  let m = n;
  while (sameAsParent(m, lit)) m = m.parent!;
  return m !== n && m.depth === 1 ? m : null;
}

/** A boilerplate source root: a "src" that is its parent folder's only sub-folder is not a place, never named alone. */
export function loneSrc(n: TNode): boolean {
  return n.name === "src" && !!n.parent && n.parent.depth >= 1 && !!n.parent.kids && n.parent.kids.length === 1;
}

// ---------- which leaves get names, in which order (rule 2, rule 6) ----------

export interface LeafRankCtx {
  hovered?: Leaf | null;
  selected?: Leaf | null;
  /** each visible bird's current file */
  bird?: Set<number>;
  changed?: Set<number>;
  planned?: Set<number>;
  read?: Set<number>;
  /** files that import a changed one (the could-break set) */
  gold?: Set<number>;
}

/** Rule 2's order: hovered > selected > a bird's current file > changed > planned > read > could-break > the rest by
 * size. Higher first. */
export function leafRank(l: Leaf, c: LeafRankCtx): number {
  const id = l.file.id;
  const tier =
    l === c.hovered ? 8 : l === c.selected ? 7 : c.bird?.has(id) ? 6 : c.changed?.has(id) ? 5 : c.planned?.has(id) ? 4 : c.read?.has(id) ? 3 : c.gold?.has(id) ? 2 : 0;
  return tier * 1e9 + Math.min(1e8, Math.max(0, l.file.lines || 0));
}

/** a name of this rank or above is LOUD: its leaf is coloured or ringed (hovered, selected, a bird's, changed) */
export const LOUD_RANK = 5e9;

/** Rule 5's ambiguity test, which leaves a name may lie over: its own, always; a LOUD name (its own leaf coloured or
 * ringed, so it is never read as a neighbour's) may also cover calm grey leaves — never another loud one. */
export function leafSkip(self: Leaf, loudName: boolean, loudLeaf: (o: Leaf) => boolean): (o: Leaf) => boolean {
  return (o) => o === self || (loudName && !loudLeaf(o));
}

/** Rule 6: zoomed in but not all the way (sparse names), which leaves are named — anything ranked (touched / gold /
 * hovered / selected), every leaf of the HOVERED clump, the focused clump's and the selected folder's. */
export function leafShown(l: Leaf, rank: number, hoverTerm: unknown, focusTerm: unknown, inSelected: boolean): boolean {
  return rank >= 1e9 || (!!hoverTerm && l.term === hoverTerm) || (!!focusTerm && l.term === focusTerm) || inSelected;
}

/** Rule 2's one name per leaf, on the bird's side: a bird tag says what the bird does to its file — unless that
 * file's leaf already carries its own name, then the tag says only who (and a refusal's ✕). */
export function tagDoing(fname: string, pose: { fly?: boolean; blocked?: boolean; nest?: boolean }, hasFile: boolean, leafNamed: boolean): string {
  if (leafNamed && hasFile && !pose.nest) return pose.blocked && !pose.fly ? " ✕" : "";
  return pose.fly ? (hasFile && !pose.nest ? ` → ${fname}` : " → nest") : pose.blocked ? ` ✕ ${fname}` : hasFile && !pose.nest ? ` · ${fname}` : "";
}
