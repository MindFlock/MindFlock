/** Code tree — tests as ROOTS: strands with tips, never leaves.
 *
 * A test folder's clump hangs under the root limb that mirrors the code it tests. Each test file is the TIP of a
 * root strand: a small rounded nodule where a strand ends. The strands of one clump form a branching root fan
 * grown from the limb just short of its end — every strand leaves it along the limb's heading, forks off an earlier strand where
 * that is the shortest way, and never crosses another strand or runs through a tip that isn't its own.
 *
 * A root Leaf keeps everything a leaf has (id, file.leaf, pick, perch, states): (x, y) is the nodule's centre,
 * `len` the nodule's length, `ang` the direction the strand ends in (so the tip points away from the strand).
 * The strand network is pure numbers, built once per model from the model build (`growRoots`); the Path2D
 * batches are built once per model by draw's prepGeometry (`prepRoots`). */

import { S0 } from "./layout";
import type { Branch, Leaf, Model, TTerm } from "./model";

type G2 = CanvasRenderingContext2D;

/** One node of a clump's root fan. Node 0 is the fan's origin (on the root limb, just short of its end); a tip node is a strand's
 * END, i.e. the back of its nodule (`leaf` set); every other node is a fork. */
export interface RootNode {
  x: number;
  y: number;
  /** parent node index, -1 for the origin */
  p: number;
  /** tips at or below this node */
  n: number;
  leaf?: Leaf;
}
export interface RootNet {
  nodes: RootNode[];
  /** the root limb's heading at the origin (unit): strands leave the origin along it */
  hx: number;
  hy: number;
  /** strand half-width at the origin and at a tip (world) */
  w0: number;
  wt: number;
  /** segments that cross another segment of the same fan (0 unless the search had no clean option) */
  crossings: number;
  /** the narrowest gap (world) any strand leaves outside a tip that isn't its own */
  tightest: number;
}

/** Nodule length as a share of S0, by file size (log): a 20-line test is a small bead, a 2k-line one a fat one. */
const TIP = { len0: 0.26, lenK: 0.2, w: 0.8 };
/** Clearance a strand keeps from any tip that isn't its own (world, on top of that tip's radius). */
const CLEAR = S0 * 0.08;

const tnet = (t: TTerm) => t as TTerm & { rootNet?: RootNet; rootGeom?: RootGeom };
export const rootNetOf = (t: TTerm): RootNet | undefined => tnet(t).rootNet;

/** The tip's nodule: [length, half-width] (world). */
export function tipSize(l: Leaf): [number, number] {
  return [l.len, l.len * TIP.w * 0.5];
}

/** The very end of a root tip (world): the front of its nodule, where the strand points. */
export function rootTip(l: Leaf): [number, number] {
  return [l.x + Math.cos(l.ang) * l.len * 0.5, l.y + Math.sin(l.ang) * l.len * 0.5];
}

/** Where a name attaches to a root tip: `gapPx` screen pixels past the tip's front along the strand's end
 * direction (world x, y; `dir` = that direction in radians — mostly downward or outward, away from the fan). A name
 * placed on this point and extending along `dir` (or centred across it) touches its own tip and no other. A changed
 * tip is drawn at least 5.5 px half-length (draw.ts drawEdits): pass `minHalfPx = 5.5` for one. */
export function rootAnchor(l: Leaf, z: number, gapPx = 3, minHalfPx = 0): { x: number; y: number; dir: number } {
  const zz = Math.max(z, 1e-6);
  const d = Math.max(l.len * 0.5, minHalfPx / zz) + gapPx / zz;
  return { x: l.x + Math.cos(l.ang) * d, y: l.y + Math.sin(l.ang) * d, dir: l.ang };
}

// ---------- growing the fan (model build; pure) ----------

function segDist(px: number, py: number, ax: number, ay: number, bx: number, by: number): number {
  const dx = bx - ax,
    dy = by - ay;
  const L2 = dx * dx + dy * dy;
  let k = L2 ? ((px - ax) * dx + (py - ay) * dy) / L2 : 0;
  k = Math.max(0, Math.min(1, k));
  return Math.hypot(px - (ax + dx * k), py - (ay + dy * k));
}
function segsCross(ax: number, ay: number, bx: number, by: number, cx: number, cy: number, dx: number, dy: number): boolean {
  const o = (px: number, py: number, qx: number, qy: number, rx: number, ry: number) => Math.sign((qx - px) * (ry - py) - (qy - py) * (rx - px));
  const o1 = o(ax, ay, bx, by, cx, cy),
    o2 = o(ax, ay, bx, by, dx, dy),
    o3 = o(cx, cy, dx, dy, ax, ay),
    o4 = o(cx, cy, dx, dy, bx, by);
  return o1 !== o2 && o3 !== o4 && o1 !== 0 && o2 !== 0 && o3 !== 0 && o4 !== 0;
}

/** Set every root leaf's nodule (`len`, `ang`) and grow its clump's strand network. Called by buildModel once
 * per root clump, after the clump's leaves are placed: (ex, ey) is the root limb's end, `hb` its heading there. */
export function growRoots(t: TTerm, ex: number, ey: number, hb: number): void {
  const L = t.leaves;
  for (const l of L) {
    const fz = Math.min(1, Math.log10(Math.max(1, l.file.lines) + 1) / 3.6);
    l.len = S0 * (TIP.len0 + TIP.lenK * fz);
  }
  const b = t.branch as Branch | undefined;
  let hx = Math.cos(hb),
    hy = Math.sin(hb);
  // the fan branches off the limb a little before its end (inside its wood): the layout may put a tip right on the
  // limb's end, and strands leaving from there would run through it. The first spot back along the limb with room
  // round it, else the roomiest one; strands leave it along the limb's own heading there.
  let bx = ex,
    by = ey;
  const c = b && b.cp && b.cp.length >= 8 ? b.cp : null;
  const at = (u: number): [number, number, number, number] => {
    if (!c) return [ex - hx * S0 * (1 - u) * 2, ey - hy * S0 * (1 - u) * 2, hx, hy];
    const m = 1 - u,
      x = m * m * m * c[0] + 3 * m * m * u * c[2] + 3 * m * u * u * c[4] + u * u * u * c[6],
      y = m * m * m * c[1] + 3 * m * m * u * c[3] + 3 * m * u * u * c[5] + u * u * u * c[7];
    const dx = 3 * m * m * (c[2] - c[0]) + 6 * m * u * (c[4] - c[2]) + 3 * u * u * (c[6] - c[4]),
      dy = 3 * m * m * (c[3] - c[1]) + 6 * m * u * (c[5] - c[3]) + 3 * u * u * (c[7] - c[5]);
    const dl = Math.hypot(dx, dy) || 1;
    return [x, y, dx / dl, dy / dl];
  };
  let room = -1e9;
  const chord = c ? Math.hypot(c[6] - c[0], c[7] - c[1]) : S0 * 2;
  const u0 = 1 - Math.min(0.5, (S0 * 0.6) / Math.max(1e-6, chord));
  for (let k = 0; k <= 10; k++) {
    const u = u0 - ((u0 - 0.4) * k) / 10;
    const [x, y, tx, ty] = at(u);
    let r = 1e9;
    for (const l of L) r = Math.min(r, Math.hypot(l.x - x, l.y - y) - l.len * 0.5);
    if (r > room) {
      room = r;
      bx = x;
      by = y;
      hx = tx;
      hy = ty;
    }
    if (r >= CLEAR * 3) break;
  }
  const w0 = Math.max(0.3, (b ? b.we : 0.6) * 0.5),
    wt = Math.min(w0, S0 * 0.035);
  const nodes: RootNode[] = [{ x: bx, y: by, p: -1, n: 0 }];
  const net: RootNet = { nodes, hx, hy, w0, wt, crossings: 0, tightest: S0 };
  tnet(t).rootNet = net;
  if (!L.length) return;
  // tips in order of distance from the origin: each grows from the network that is already there
  const order = L.map((l, i) => [Math.hypot(l.x - bx, l.y - by), i] as [number, number]).sort((a, c) => a[0] - c[0] || a[1] - c[1]);
  const rad = (l: Leaf) => l.len * 0.5;
  // a segment from (ax, ay) to tip `own`: does it cross a segment, and how far does it pass outside every other tip?
  let blocker: Leaf | null = null;
  const judge = (ax: number, ay: number, ex: number, ey: number, own: Leaf, skip: number): [boolean, number] => {
    const pad = S0 * 1.5;
    const x0 = Math.min(ax, ex) - pad,
      x1 = Math.max(ax, ex) + pad,
      y0 = Math.min(ay, ey) - pad,
      y1 = Math.max(ay, ey) + pad;
    let m = pad;
    blocker = null;
    for (const o of L) {
      if (o === own || o.x < x0 || o.x > x1 || o.y < y0 || o.y > y1) continue;
      const mo = segDist(o.x, o.y, ax, ay, ex, ey) - rad(o);
      if (mo < m) {
        m = mo;
        blocker = o;
      }
    }
    for (let i = 1; i < nodes.length; i++) {
      if (i === skip) continue;
      const c = nodes[i],
        p = nodes[c.p];
      if (Math.max(c.x, p.x) < x0 || Math.min(c.x, p.x) > x1 || Math.max(c.y, p.y) < y0 || Math.min(c.y, p.y) > y1) continue;
      if (segsCross(ax, ay, ex, ey, p.x, p.y, c.x, c.y)) return [true, m];
    }
    return [false, m];
  };
  for (const [, li] of order) {
    const l = L[li];
    // candidates: the origin, and the forks and points along the segments near this tip (a tip's own segment only
    // on its first half, so no strand appears to run on through someone else's tip)
    const cand: Array<[number, number, number, number, number]> = []; // cost, x, y, node index (or -1-segment), along
    const d0 = Math.hypot(l.x - bx, l.y - by);
    // strands prefer to leave the origin forward (along the limb), not backward up toward the ground
    const back = Math.max(0, -((l.x - bx) * hx + (l.y - by) * hy) / Math.max(1e-6, d0));
    cand.push([d0 * (1 + 0.5 * back), bx, by, 0, 0]);
    // only the network near this tip is worth trying (a 240-tip fan has ~500 segments)
    let dmin = d0;
    const sd: number[] = [];
    for (let i = 1; i < nodes.length; i++) {
      const c = nodes[i],
        p = nodes[c.p];
      sd[i] = segDist(l.x, l.y, p.x, p.y, c.x, c.y);
      dmin = Math.min(dmin, sd[i]);
    }
    for (let i = 1; i < nodes.length; i++) {
      if (sd[i] > dmin + S0 * 2.5) continue;
      const c = nodes[i],
        p = nodes[c.p];
      // a fork itself
      if (!c.leaf) cand.push([Math.hypot(l.x - c.x, l.y - c.y) + S0 * 0.05, c.x, c.y, i, 0]);
      const dx = c.x - p.x,
        dy = c.y - p.y;
      const L2 = dx * dx + dy * dy || 1;
      // never a new fork on top of an existing node: a tip's segment on its first half only, a fork's short of both ends
      const kmax = c.leaf ? 0.5 : 0.88;
      const sl = Math.sqrt(L2);
      // the nearest point of the segment, and a few others along it (the nearest one is often blocked by a tip)
      const kn = Math.max(0.12, Math.min(kmax, ((l.x - p.x) * dx + (l.y - p.y) * dy) / L2));
      for (const k of [kn, 0.15, 0.35, 0.5, 0.7, 0.88]) {
        if (k > kmax || (k !== kn && Math.abs(k - kn) < 0.08)) continue;
        const x = p.x + dx * k,
          y = p.y + dy * k;
        const d = Math.hypot(l.x - x, l.y - y);
        // a strand that would leave its parent backward (against the parent's own direction) reads as a hook
        const cosA = d > 1e-6 ? ((l.x - x) * dx + (l.y - y) * dy) / (d * sl) : 1;
        cand.push([d * (1 + 0.35 * Math.max(0, -cosA)) + S0 * 0.05, x, y, -1 - i, k]);
      }
    }
    cand.sort((a, c) => a[0] - c[0]);
    const ux = (ax: number, ay: number) => {
      const d = Math.hypot(l.x - ax, l.y - ay) || 1;
      return [(l.x - ax) / d, (l.y - ay) / d];
    };
    // the cheapest attachment that crosses nothing and passes every other tip with room to spare; failing that, the
    // one that crosses nothing and passes the other tips most widely
    let pick = cand[0],
      best = -1e9,
      crosses = true,
      via: [number, number] | null = null;
    const blocked: Array<[(typeof cand)[number], Leaf]> = [];
    for (let ci = 0; ci < Math.min(cand.length, 96); ci++) {
      const c = cand[ci];
      const [vx, vy] = ux(c[1], c[2]);
      const [x, m] = judge(c[1], c[2], l.x - vx * rad(l), l.y - vy * rad(l), l, c[3] < 0 ? -1 - c[3] : -1);
      if (x) continue;
      if (m >= CLEAR) {
        pick = c;
        crosses = false;
        best = m;
        break;
      }
      if (blocker && blocked.length < 12) blocked.push([c, blocker]);
      if (m > best) {
        best = m;
        pick = c;
        crosses = false;
      }
    }
    // a tip right behind another: bend round it (one waypoint beside the tip in the way)
    if (best < CLEAR)
      for (const [c, o] of blocked) {
        const sx = l.x - c[1],
          sy = l.y - c[2],
          sd = Math.hypot(sx, sy) || 1;
        let done = false;
        for (const sg of [1, -1]) {
          const off = rad(o) + CLEAR * 1.5;
          const wx = o.x - (sy / sd) * off * sg,
            wy = o.y + (sx / sd) * off * sg;
          const [x1, m1] = judge(c[1], c[2], wx, wy, l, c[3] < 0 ? -1 - c[3] : -1);
          if (x1 || m1 < CLEAR) continue;
          const [vx, vy] = ux(wx, wy);
          const [x2, m2] = judge(wx, wy, l.x - vx * rad(l), l.y - vy * rad(l), l, -1);
          if (x2 || Math.min(m1, m2) <= best) continue;
          best = Math.min(m1, m2);
          pick = c;
          via = [wx, wy];
          crosses = false;
          if (best >= CLEAR) {
            done = true;
            break;
          }
        }
        if (done) break;
      }
    if (crosses) net.crossings++;
    net.tightest = Math.min(net.tightest, best);
    // attach: split the chosen segment at the attachment point (a new fork), or start at a node
    let pi = pick[3];
    if (pi < 0) {
      const si = -1 - pi,
        c = nodes[si];
      nodes.push({ x: pick[1], y: pick[2], p: c.p, n: 0 });
      pi = nodes.length - 1;
      c.p = pi;
    }
    if (via) {
      nodes.push({ x: via[0], y: via[1], p: pi, n: 0 });
      pi = nodes.length - 1;
    }
    const [vx, vy] = via ? ux(via[0], via[1]) : ux(pick[1], pick[2]);
    l.ang = Math.atan2(vy, vx);
    nodes.push({ x: l.x - vx * rad(l), y: l.y - vy * rad(l), p: pi, n: 0, leaf: l });
  }
  // tips per node (for the strand taper)
  for (let i = nodes.length - 1; i >= 0; i--) {
    const c = nodes[i];
    if (c.leaf) {
      for (let q = i; q >= 0; q = nodes[q].p) nodes[q].n++;
    }
  }
}

/** A segment's half-width: thick where many tips hang below it, a hair at a tip. */
export function strandHalfW(net: RootNet, node: RootNode): number {
  const N = Math.max(1, net.nodes[0].n);
  return net.wt + (net.w0 - net.wt) * Math.sqrt(node.n / N);
}

/** Every segment's drawable piece (world): a quadratic from the middle of the parent segment through the parent
 * fork to the middle of this one, then straight on to a tip's nodule. The pieces join into smooth strands that
 * leave the origin along the limb's heading. Calls `f(x0, y0, cx, cy, x1, y1, ex, ey)` (ex/ey = NaN unless the
 * node is a tip). */
function eachPiece(
  net: RootNet,
  i: number,
  f: (x0: number, y0: number, cx: number, cy: number, x1: number, y1: number, ex: number, ey: number) => void,
) {
  const N = net.nodes,
    c = N[i],
    p = N[c.p];
  const mx = (p.x + c.x) / 2,
    my = (p.y + c.y) / 2;
  let x0: number, y0: number, kx: number, ky: number;
  if (p.p < 0) {
    // from the origin: bend out of the limb's own heading (less the further the strand turns from it, so a strand
    // that leaves sideways never bows into its neighbours)
    const d = Math.hypot(mx - p.x, my - p.y);
    const k = 0.6 * Math.max(0, ((mx - p.x) * net.hx + (my - p.y) * net.hy) / Math.max(1e-6, d)) ** 2;
    x0 = p.x;
    y0 = p.y;
    kx = p.x + net.hx * d * k;
    ky = p.y + net.hy * d * k;
  } else {
    const q = N[p.p];
    x0 = (q.x + p.x) / 2;
    y0 = (q.y + p.y) / 2;
    kx = p.x;
    ky = p.y;
  }
  f(x0, y0, kx, ky, mx, my, c.leaf ? c.x : NaN, c.leaf ? c.y : NaN);
}

// ---------- drawing (static layer) ----------

interface RootGeom {
  /** strand pieces batched by width band: [half-width (world), path] */
  bands: Array<[number, Path2D]>;
  /** every tip's nodule except uncreated ghosts' */
  tips: Path2D;
  /** tips (and their last piece) drawn only once created */
  ghosts: Leaf[];
}
const BANDS = 5,
  FINE_BANDS = 3;

function tipEllipse(g: G2 | Path2D, l: Leaf) {
  const [len, hw] = tipSize(l);
  g.moveTo(l.x + Math.cos(l.ang) * len * 0.5, l.y + Math.sin(l.ang) * len * 0.5);
  g.ellipse(l.x, l.y, len * 0.5, hw, l.ang, 0, Math.PI * 2);
}

/** A root leaf's outline for the shared overlays (edits, gold, the dusk spotlight): its tip nodule. */
export function rootTipPath(g: G2, l: Leaf): void {
  tipEllipse(g, l);
}

/** The 8-number outline every leaf carries (`l.g`), for a root tip: its nodule as a fat lens — base at the
 * strand's end, tip at the front — so code that reads `g` (bounds, stalk bases) sees the nodule, not a leaf. */
export function rootTipG(l: Leaf): number[] {
  const ca = Math.cos(l.ang),
    sa = Math.sin(l.ang);
  const len = l.len,
    w = len * TIP.w;
  const bx = l.x - ca * len * 0.5,
    by = l.y - sa * len * 0.5,
    tx = l.x + ca * len * 0.5,
    ty = l.y + sa * len * 0.5;
  return [bx, by, l.x - sa * w, l.y + ca * w, tx, ty, l.x + sa * w, l.y - ca * w];
}

/** Build every root clump's Path2D batches (draw's prepGeometry, once per model). */
export function prepRoots(M: Model): void {
  for (const t0 of M.terms) {
    if (t0.region !== "root") continue;
    const t = tnet(t0),
      net = t.rootNet;
    if (!net) continue;
    const N = net.nodes;
    const ghostTip = (i: number) => !!(N[i].leaf && N[i].leaf!.file.ghost);
    const bands: Array<[number, Path2D]> = [];
    const wOf = (b: number) => net.wt + ((net.w0 - net.wt) * (b + 0.5)) / BANDS;
    for (let b = 0; b < BANDS; b++) bands.push([wOf(b), new Path2D()]);
    for (let i = 1; i < N.length; i++) {
      if (ghostTip(i)) continue;
      const w = strandHalfW(net, N[i]);
      const bi = Math.max(0, Math.min(BANDS - 1, Math.floor(((w - net.wt) / Math.max(1e-6, net.w0 - net.wt)) * BANDS)));
      const P = bands[bi][1];
      eachPiece(net, i, (x0, y0, kx, ky, x1, y1, ex, ey) => {
        P.moveTo(x0, y0);
        P.quadraticCurveTo(kx, ky, x1, y1);
        if (ex === ex) P.lineTo(ex, ey);
      });
    }
    const tips = new Path2D(),
      ghosts: Leaf[] = [];
    for (const l of t.leaves) {
      if (l.file.ghost) ghosts.push(l);
      else tipEllipse(tips, l);
    }
    t.rootGeom = { bands, tips, ghosts };
  }
}

/** The strand from the fan's origin to one tip (world), for lighting a changed test the way lit wood leads to a
 * changed leaf. */
const STRANDS = new WeakMap<Leaf, Path2D>();
export function rootStrandPath(l: Leaf): Path2D | null {
  const hit = STRANDS.get(l);
  if (hit) return hit;
  const t = l.term && tnet(l.term),
    net = t && t.rootNet;
  if (!net) return null;
  const N = net.nodes;
  let i = N.findIndex((n) => n.leaf === l);
  if (i < 0) return null;
  const p = new Path2D();
  for (; i > 0; i = N[i].p)
    eachPiece(net, i, (x0, y0, kx, ky, x1, y1, ex, ey) => {
      if (ex === ex) {
        p.moveTo(x1, y1);
        p.lineTo(ex, ey);
      }
      p.moveTo(x0, y0);
      p.quadraticCurveTo(kx, ky, x1, y1);
    });
  STRANDS.set(l, p);
  return p;
}

export interface RootStyle {
  /** the thick strands near the limb (the limb's own wood colour) */
  strand: string;
  /** the fine strands out at the tips: paler, like real rootlets, so they read on the soil */
  fine: string;
  tip: string;
}

/** Draw the root fans in view (static layer, world transform set). `style` gives a clump's colours (null = not
 * drawn, e.g. folded); `tipCol` may override one tip (keep-out file); `created` = ghost files that exist now. */
export function drawRoots(
  g: G2,
  terms: TTerm[],
  z: number,
  alpha: number,
  tipAlpha: number,
  style: (t: TTerm) => RootStyle | null,
  tipCol: (l: Leaf) => string | null,
  created: Set<number>,
): void {
  if (alpha <= 0.02) return;
  g.lineCap = "round";
  g.lineJoin = "round";
  g.globalAlpha = alpha;
  const minPx = 0.7 / z;
  for (const t0 of terms) {
    const t = tnet(t0),
      G = t.rootGeom;
    const st = G && style(t0);
    if (!G || !st) continue;
    G.bands.forEach(([hw, p], b) => {
      g.strokeStyle = b < FINE_BANDS ? st.fine : st.strand;
      g.lineWidth = Math.max(minPx, hw * 2);
      g.stroke(p);
    });
    g.globalAlpha = alpha * tipAlpha;
    g.fillStyle = st.tip;
    g.fill(G.tips);
    for (const l of t.leaves) {
      const own = tipCol(l);
      if (own) {
        g.fillStyle = own;
        g.beginPath();
        tipEllipse(g, l);
        g.fill();
      }
    }
    for (const l of G.ghosts) {
      if (!created.has(l.file.id)) continue;
      const sp = rootStrandPath(l);
      if (sp) {
        g.strokeStyle = st.fine;
        g.lineWidth = Math.max(minPx, (t.rootNet!.wt + t.rootNet!.w0) * 0.5);
        g.stroke(sp);
      }
      g.fillStyle = tipCol(l) || st.tip;
      g.beginPath();
      tipEllipse(g, l);
      g.fill();
    }
    g.globalAlpha = alpha;
  }
  g.globalAlpha = 1;
  g.lineCap = "butt";
  g.lineJoin = "miter";
}
