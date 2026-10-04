/** Code tree — the canvas renderer.
 *
 * Ported from the approved prototype (mindflock-prototypes/code-tree/draw.js,
 * plus pick() and the minimap from ui.js). Two layers: a STATIC layer (sky,
 * soil, canopy, wood, leaves, hatching — everything that only changes with the
 * camera, the rules or the folds) rendered into an offscreen bitmap a little
 * larger than the viewport and blitted (scaled while a gesture is in flight,
 * re-rendered crisply once it settles), and an OVERLAY drawn every frame
 * (rule wraps, reads, buds, edits, gold blast, selection, labels, nests,
 * badges, birds). Every colour comes from the view's palette (palette.ts:
 * app tokens + the tree's --ct-* custom properties); nothing here is themed by
 * hand. */

import { birdSprite, tinted } from "../flock";
import { agentBlast, addBadgeSets, blastOf, collectBadges } from "./blast";
import { dPS, hashStr, rng, taperHW } from "./layout";
import { S0, TAU, baseOf, isUnder, shortName, type Branch, type Leaf, type Model, type TNode, type TTerm } from "./model";
import { glyphShape, type Pal, type Palette } from "./palette";
import type { AgentDef, AgentState, Badge, BirdEv, Blocked, Hit, RiskGroup, TreeView, TZone } from "./types";

/** What an empty view (zoomed into air) says instead of showing nothing. */
export const EMPTY_HINT = "Nothing here — scroll out, or press ⌂ Whole tree";

type G2 = CanvasRenderingContext2D;
type R4 = [number, number, number, number];

interface Layers {
  sh: Path2D;
  body: Path2D;
  hi: Path2D;
}
interface TermGeom {
  blob: Path2D;
  blobHi: Path2D;
  blobFold: Path2D;
  cv?: Layers;
  cvc?: Layers;
  tone: number;
  twig?: { main: Path2D; side: Path2D; stalk: Path2D; w: number };
}
interface CanopyBatch {
  /** limb index into PAL, or -1 = roots */
  limb: number;
  tone: number;
  fine: Layers;
  coarse: Layers;
}
interface Mass {
  path: Path2D;
  n: number;
  x0: number;
  y0: number;
  x1: number;
  y1: number;
  zf: number;
  imgs: Map<string, { cv: HTMLCanvasElement; sc: number }>;
}
interface ModelGeom {
  canopy: Map<string, CanopyBatch>;
  mass: Mass;
  trunkPath: Path2D;
  rootFlare: Path2D;
  trunkCapPath?: Path2D;
  trunkCapW?: number;
  geomReady?: boolean;
}
const tg = (t: TTerm) => t as unknown as TTerm & TermGeom;
const mg = (M: Model) => M as unknown as Model & ModelGeom;

/** Clip a session title for canvas text. */
/** "1 edit blocked" / "3 edits blocked": the one wording for a rule's refusals, everywhere. */
export function blockedTxt(n: number): string {
  return `${n} edit${n === 1 ? "" : "s"} blocked`;
}

/** The short name a rule goes by: a path's last segment, with its parent when that alone says little
 * ("backend/providers" → "providers", "backend/web/core" → "web/core"). */
export function tailPath(p: string): string {
  const segs = p.replace(/^\/+/, "").replace(/\/(\*\*)?$/, "").split("/").filter(Boolean);
  if (!segs.length) return p;
  const last = segs[segs.length - 1];
  return last.length <= 6 && segs.length > 1 ? segs[segs.length - 2] + "/" + last : last;
}

function clip(s: string, n = 22): string {
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

/** Call `cb` once the bird sprite has decoded (birds fall back to nothing
 * until then, so the caller repaints). */
export function whenSpriteReady(cb: () => void): void {
  const img = birdSprite();
  if (!img) return;
  if (img.complete && img.naturalWidth) return;
  img.addEventListener("load", () => cb(), { once: true });
}

function trunkPath(M: Model, T: number): Path2D {
  const G = M.groundY;
  const tr = new Path2D();
  tr.moveTo(-T * 1.25, G + S0 * 0.6);
  tr.bezierCurveTo(-T * 0.62, G - T * 0.25, -T * 0.52, G * 0.55, -T * 0.5, 0);
  tr.lineTo(T * 0.5, 0);
  tr.bezierCurveTo(T * 0.52, G * 0.55, T * 0.62, G - T * 0.25, T * 1.25, G + S0 * 0.6);
  tr.closePath();
  return tr;
}

export function prepGeometry(M: Model): void {
  const MG = mg(M);
  if (MG.geomReady) return;
  // leaf outlines + blob paths (world space)
  for (const l of M.leaves) {
    const ca = Math.cos(l.ang),
      sa = Math.sin(l.ang);
    const flat = l.region === "ground",
      root = l.region === "root";
    const len = l.len,
      w = len * (root ? 0.5 : flat ? 0.3 : 0.37);
    const bx = l.x - ca * len * 0.5,
      by = l.y - sa * len * 0.5,
      tx = l.x + ca * len * 0.5,
      ty = l.y + sa * len * 0.5;
    const mx = l.x + ca * len * 0.04,
      my = l.y + sa * len * 0.04;
    l.g = [bx, by, mx - sa * w, my + ca * w, tx, ty, mx + sa * w, my - ca * w];
  }
  for (const t0 of M.terms) {
    const t = tg(t0);
    const p = new Path2D(),
      q = new Path2D();
    const r = t.s * (t.region === "root" ? 0.6 : 0.66),
      r2 = t.s * 0.44;
    for (const l of t.leaves) {
      p.moveTo(l.x + r, l.y);
      p.arc(l.x, l.y, r, 0, TAU);
    }
    for (const l of t.leaves) {
      q.moveTo(l.x - t.s * 0.1 + r2, l.y - t.s * 0.14);
      q.arc(l.x - t.s * 0.1, l.y - t.s * 0.14, r2, 0, TAU);
    }
    const f = new Path2D(),
      rf = t.s * 1.02;
    for (const l of t.leaves) {
      f.moveTo(l.x + rf, l.y);
      f.arc(l.x, l.y, rf, 0, TAU);
    }
    t.blob = p;
    t.blobHi = q;
    t.blobFold = f;
  }
  // canopy: at tree zoom a folder's files read as ONE leafy clump — an overlapping leaf oval per file in one of
  // three greens over a soft shadow, a highlight up-left. Every oval stays inside the clump's foliage disc (the
  // layout's clearance envelope), so a clump still never touches foreign wood. Batched per (limb, tone): a 5k-file
  // tree is a few dozen fills.
  MG.canopy = new Map();
  for (const t0 of M.terms) {
    const t = tg(t0);
    if (!t.leaves.length) continue;
    const s = t.s,
      rnd = rng(hashStr(t.node.path + (t.own ? "#" : "") + "~cv"));
    t.tone = hashStr(t.node.path + (t.own ? "#" : "")) % 3;
    const cv: Layers = { sh: new Path2D(), body: new Path2D(), hi: new Path2D() };
    // the clump's core (a solid mass inside the leaf ring), its shadow and its lit side
    const cr = t.R * 0.8 + s * 0.45,
      ccx = t.cx,
      ccy = t.cy;
    cv.sh.moveTo(ccx + s * 0.07 + cr + s * 0.12, ccy + s * 0.1);
    cv.sh.arc(ccx + s * 0.07, ccy + s * 0.1, cr + s * 0.12, 0, TAU);
    cv.body.moveTo(ccx + cr, ccy);
    cv.body.arc(ccx, ccy, cr, 0, TAU);
    if (t.leaves.length > 2) {
      const hr = cr * 0.55;
      cv.hi.moveTo(ccx - cr * 0.3 + hr, ccy - cr * 0.34);
      cv.hi.ellipse(ccx - cr * 0.3, ccy - cr * 0.34, hr, hr * 0.8, -0.5, 0, TAU);
    }
    for (const l of t.leaves) {
      const a = l.ang + (rnd() - 0.5) * 0.6,
        k = 0.9 + rnd() * 0.2;
      cv.sh.moveTo(l.x + s * 0.06 + s * 0.6, l.y + s * 0.08);
      cv.sh.ellipse(l.x + s * 0.06, l.y + s * 0.08, s * 0.6, s * 0.52, a, 0, TAU);
      cv.body.moveTo(l.x + s * 0.6 * k, l.y);
      cv.body.ellipse(l.x, l.y, s * 0.6 * k, s * 0.44 * k, a, 0, TAU);
      if (rnd() < 0.62) {
        const hx = l.x - s * 0.13,
          hy = l.y - s * 0.16;
        cv.hi.moveTo(hx + s * 0.32, hy);
        cv.hi.ellipse(hx, hy, s * 0.32, s * 0.22, a, 0, TAU);
      }
    }
    t.cv = cv;
    // far out a leaf is a pixel: the coarse clump is one disc per layer (a 5k-file tree in ~2k arcs, not 15k ovals)
    const cc: Layers = { sh: new Path2D(), body: new Path2D(), hi: new Path2D() },
      rr = t.R + s * 0.52;
    // a cumulus, not a ball: a core and a ring of lobes (all inside the clump's disc), shadow below, two lit lobes
    const nl = Math.max(5, Math.min(9, Math.round(4 + rr / s))),
      a0 = rnd() * TAU;
    const lobes = (path: Path2D, dx: number, dy: number, k: number) => {
      path.moveTo(ccx + dx + rr * 0.66 * k, ccy + dy);
      path.arc(ccx + dx, ccy + dy, rr * 0.66 * k, 0, TAU);
      for (let i = 0; i < nl; i++) {
        const a = a0 + (i / nl) * TAU + (rnd() - 0.5) * 0.5,
          lr = rr * (0.32 + rnd() * 0.08) * k,
          d = (rr - lr) * k;
        const x = ccx + dx + Math.cos(a) * d,
          y = ccy + dy + Math.sin(a) * d;
        path.moveTo(x + lr, y);
        path.arc(x, y, lr, 0, TAU);
      }
    };
    cc.sh.moveTo(ccx + s * 0.05 + rr * 0.97, ccy + s * 0.08);
    cc.sh.arc(ccx + s * 0.05, ccy + s * 0.08, rr * 0.97, 0, TAU);
    lobes(cc.body, 0, 0, 0.96);
    for (let i = 0; i < 2; i++) {
      const hr = rr * (0.3 - i * 0.08),
        hx = ccx - rr * (0.28 - i * 0.22),
        hy = ccy - rr * (0.36 - i * 0.12);
      cc.hi.moveTo(hx + hr, hy);
      cc.hi.arc(hx, hy, hr, 0, TAU);
    }
    t.cvc = cc;
    const limb = t.region === "root" ? -1 : t.node.limb;
    const key = limb + ":" + t.tone;
    let B = MG.canopy.get(key);
    if (!B)
      MG.canopy.set(
        key,
        (B = {
          limb,
          tone: t.tone,
          fine: { sh: new Path2D(), body: new Path2D(), hi: new Path2D() },
          coarse: { sh: new Path2D(), body: new Path2D(), hi: new Path2D() },
        })
      );
    for (const L of ["sh", "body", "hi"] as const) {
      B.fine[L].addPath(cv[L]);
      B.coarse[L].addPath(cc[L]);
    }
  }
  MG.mass = buildCrownMass(M);
  for (const t of M.terms) buildClumpTwig(tg(t));
  // trunk outline
  const T = M.trunkW,
    G = M.groundY;
  MG.trunkPath = trunkPath(M, T);
  const rt = new Path2D(); // root crown just below the ground
  rt.moveTo(-T * 1.25, G + S0 * 0.4);
  rt.quadraticCurveTo(0, G + T * 0.7, T * 1.25, G + S0 * 0.4);
  rt.closePath();
  MG.rootFlare = rt;
  MG.geomReady = true;
}

const MASS = { growPx: 13, woodPx: 2.2, cellPx: 3, a: 1 };

// The canopy's body. At tree zoom the narrow air between neighbouring clumps closes up into one darker leafy
// mass, so the crown reads as foliage rather than a scatter of dots — but never over wood: every branch keeps a
// clear channel, so the structure stays readable and no leaf-looking pixel sits on a limb. Grid samples near
// foliage and clear of every branch; one Path2D of small discs (a few thousand arcs on a 5k-file tree).
type MassItem = { t: TTerm } | { p: [number, number, number, number]; q: [number, number, number, number]; hw: number };
function buildCrownMass(M: Model): Mass {
  // sized in screen pixels at the whole-tree zoom, so a 700-file and a 5k-file tree get the same closed canopy
  const B = M.bounds,
    zf = Math.min(1300 / (B.x1 - B.x0), 800 / (B.y1 - B.y0));
  const g = Math.max(S0 * 0.9, MASS.cellPx / zf),
    grow = MASS.growPx / zf,
    wclr = Math.max(S0 * 0.6, MASS.woodPx / zf),
    CS = Math.max(S0 * 6, grow * 1.5);
  const cells = new Map<number, MassItem[]>(),
    key = (i: number, j: number) => i * 65536 + j;
  const put = (x0: number, y0: number, x1: number, y1: number, it: MassItem) => {
    for (let i = Math.floor(x0 / CS); i <= Math.floor(x1 / CS); i++)
      for (let j = Math.floor(y0 / CS); j <= Math.floor(y1 / CS); j++) {
        const k = key(i, j);
        let a = cells.get(k);
        if (!a) cells.set(k, (a = []));
        a.push(it);
      }
  };
  const R = grow + g;
  for (const t of M.terms) if (t.region === "crown" && t.leaves.length) put(t.cx - t.rad - R, t.cy - t.rad - R, t.cx + t.rad + R, t.cy + t.rad + R, { t });
  for (const b of M.branches)
    if (b.region === "crown")
      for (let i = 0; i < b.pts.length - 1; i++) {
        const p = b.pts[i],
          q = b.pts[i + 1],
          hw = taperHW(b.wb, b.we, i / (b.pts.length - 1)),
          m = hw + wclr + g;
        put(Math.min(p[0], q[0]) - m, Math.min(p[1], q[1]) - m, Math.max(p[0], q[0]) + m, Math.max(p[1], q[1]) + m, { p, q, hw });
      }
  const T = M.trunkW; // the trunk is wood too
  const path = new Path2D();
  let n = 0;
  const rnd = rng(hashStr(M.name + "~mass"));
  const cb = M.crownBounds,
    dy = g * 0.866;
  for (let row = 0, y = cb.y0 - grow; y <= Math.min(cb.y1 + grow, M.groundY - S0); row++, y += dy) {
    for (let x0 = cb.x0 - grow + (row & 1 ? g / 2 : 0), x = x0; x <= cb.x1 + grow; x += g) {
      const a = cells.get(key(Math.floor(x / CS), Math.floor(y / CS)));
      if (!a) continue;
      // a leafy lobe of jittered size, smaller toward the mass's edge, so the outline reads as foliage, not a grid
      const jx = x + (rnd() - 0.5) * g * 0.5,
        jy = y + (rnd() - 0.5) * g * 0.5;
      let dT = 1e9,
        inside = false;
      for (const it of a)
        if ("t" in it) {
          const d = Math.hypot(jx - it.t.cx, jy - it.t.cy) - it.t.rad;
          if (d < dT) dT = d;
          if (d < -it.t.s * 0.8) {
            inside = true;
            break;
          }
        }
      if (inside || dT > grow) continue;
      const r = g * (0.62 + 0.3 * rnd()) * (1 - (0.35 * Math.max(0, dT)) / grow);
      let clear = true;
      for (const it of a)
        if (!("t" in it) && dPS(jx, jy, it.p[0], it.p[1], it.q[0], it.q[1]) - it.hw < wclr + r) {
          clear = false;
          break;
        }
      if (!clear || (jy > -S0 && Math.abs(jx) < T * 0.75 + wclr + r)) continue;
      path.moveTo(jx + r, jy);
      path.arc(jx, jy, r, 0, TAU);
      n++;
    }
  }
  const pad = g * 0.95 + S0;
  return { path, n, x0: cb.x0 - grow - pad, y0: cb.y0 - grow - pad, x1: cb.x1 + grow + pad, y1: Math.min(cb.y1 + grow, M.groundY) + pad, zf, imgs: new Map() };
}
// the mass is static per tree: it is rasterised once (per colour) at twice the whole-tree scale and drawn as one
// image — thousands of arcs cost a 5k-file tree ~6 ms per static redraw, the image well under one
function drawMass(g: G2, M: Model, col: string) {
  const ms = mg(M).mass;
  let im = ms.imgs.get(col);
  if (!im) {
    const sc = Math.min(ms.zf * 2.4, 2048 / (ms.x1 - ms.x0), 2048 / (ms.y1 - ms.y0));
    const cv = document.createElement("canvas");
    cv.width = Math.max(1, Math.ceil((ms.x1 - ms.x0) * sc));
    cv.height = Math.max(1, Math.ceil((ms.y1 - ms.y0) * sc));
    const c = cv.getContext("2d");
    if (c) {
      c.setTransform(sc, 0, 0, sc, -ms.x0 * sc, -ms.y0 * sc);
      c.fillStyle = col;
      c.fill(ms.path);
    }
    im = { cv, sc };
    ms.imgs.set(col, im);
  }
  g.drawImage(im.cv, ms.x0, ms.y0, im.cv.width / im.sc, im.cv.height / im.sc);
}
// A clump's own wood. Once single leaves show, the folder's twig runs on from its stem tip into the clump with a
// few side shoots, and every leaf hangs on a hairline stalk from the nearest point of it — so foliage is attached
// to its branch, not confetti around an invisible point. All of it lies inside the clump's own foliage disc, i.e.
// it is the folder's own twig: it never reaches another folder's leaves or wood.
function buildClumpTwig(t: TTerm & TermGeom) {
  const L = t.leaves;
  if (!L.length) return;
  const b = t.branch as Branch | undefined,
    R = Math.max(t.R, t.s * 0.5),
    cx = t.cx,
    cy = t.cy;
  const px = b ? b.x1 : cx,
    py = b ? b.y1 : cy + R * 0.6;
  let hx = 0,
    hy = -1;
  if (b) {
    hx = b.cp[6] - b.cp[4];
    hy = b.cp[7] - b.cp[5];
    const hl = Math.hypot(hx, hy) || 1;
    hx /= hl;
    hy /= hl;
  }
  // the main twig: from the tip, bending from the stem's heading toward and through the clump's centre
  let ux = cx - px,
    uy = cy - py;
  const d0 = Math.hypot(ux, uy);
  if (d0 < t.s * 0.3) {
    ux = hx;
    uy = hy;
  } else {
    ux /= d0;
    uy /= d0;
  }
  const ex = cx + ux * R * 0.5,
    ey = cy + uy * R * 0.5;
  const kx = px + hx * Math.max(d0, R * 0.4) * 0.55,
    ky = py + hy * Math.max(d0, R * 0.4) * 0.55;
  const qpt = (k: number): [number, number] => {
    const m = 1 - k;
    return [m * m * px + 2 * m * k * kx + k * k * ex, m * m * py + 2 * m * k * ky + k * k * ey];
  };
  const main = new Path2D();
  main.moveTo(px, py);
  main.quadraticCurveTo(kx, ky, ex, ey);
  const skel: Array<[number, number]> = [];
  for (let i = 0; i <= 8; i++) skel.push(qpt(i / 8));
  // side shoots, alternating, more for a bigger clump; each ends well inside the clump's disc
  const side = new Path2D(),
    ns = L.length < 4 ? 0 : L.length < 12 ? 2 : L.length < 40 ? 3 : 4;
  const rnd = rng(hashStr(t.node.path + (t.own ? "#" : "") + "~twig"));
  for (let i = 0; i < ns; i++) {
    const k = 0.38 + ((0.5 * i) / Math.max(1, ns - 1)) * (ns > 1 ? 1 : 0),
      [sx, sy] = qpt(k),
      [sx2, sy2] = qpt(Math.min(1, k + 0.05));
    let tx = sx2 - sx,
      ty = sy2 - sy;
    const tl = Math.hypot(tx, ty) || 1;
    tx /= tl;
    ty /= tl;
    const sgn = i % 2 ? 1 : -1,
      a = sgn * (0.75 + rnd() * 0.35),
      ca = Math.cos(a),
      sa = Math.sin(a);
    const dx = tx * ca - ty * sa,
      dy = tx * sa + ty * ca;
    let len = R * (0.45 + rnd() * 0.2);
    // keep the tip inside the disc
    for (let it = 0; it < 6 && Math.hypot(sx + dx * len - cx, sy + dy * len - cy) > R * 0.85; it++) len *= 0.8;
    const ox = sx + dx * len,
      oy = sy + dy * len,
      mx = sx + dx * len * 0.5 + tx * len * 0.18,
      my = sy + dy * len * 0.5 + ty * len * 0.18;
    side.moveTo(sx, sy);
    side.quadraticCurveTo(mx, my, ox, oy);
    for (let j = 1; j <= 4; j++) {
      const kk = j / 4,
        m = 1 - kk;
      skel.push([m * m * sx + 2 * m * kk * mx + kk * kk * ox, m * m * sy + 2 * m * kk * my + kk * kk * oy]);
    }
  }
  // stalks: each leaf's base to the nearest point of that wood
  const stalk = new Path2D();
  for (const l of L) {
    const bx = l.g![0],
      by = l.g![1];
    let best: [number, number] | null = null,
      bd = 1e18;
    for (const q of skel) {
      const dd = (q[0] - bx) * (q[0] - bx) + (q[1] - by) * (q[1] - by);
      if (dd < bd) {
        bd = dd;
        best = q;
      }
    }
    if (best && bd > (t.s * 0.08) ** 2) {
      stalk.moveTo(best[0], best[1]);
      stalk.lineTo(bx, by);
    }
  }
  t.twig = { main, side, stalk, w: b ? Math.max(b.we * 0.5, 0.35) : 0.5 };
}

/** The rules the canopy obeys: waived zones are drawn, never enforced. */
const enforced = (zs: TZone[]) => zs.filter((z) => !z.waived);

/** Does "only here" bind this bird? An only-here scoped to this worktree binds this session and its helpers;
 * another session edits in its own worktree and is not restricted by it — its marks are never dimmed as if it
 * were. (A repo-wide only-here binds everyone.) */
export function onlyBinds(v: Pick<TreeView, "agents" | "zones" | "hasOnly">, A: AgentState): boolean {
  if (!v.hasOnly) return false;
  if (A.ag.primary) return true;
  const prim = v.agents.find((a) => a.ag.primary);
  if (prim && A.ag.parent === prim.ag.key) return true;
  return v.zones.some((z) => !z.waived && z.type === "only" && z.z.scope !== "worktree");
}

// per-frame visibility / zone flags (top-down over pre-ordered nodes)
export function computeFlags(v: TreeView): void {
  const M = v.M,
    z = v.cam.z,
    zones = enforced(v.zones);
  const keepN = new Set<TNode>(),
    onlyN = new Set<TNode>();
  for (const zz of zones) if (zz.node) (zz.type === "keep" ? keepN : onlyN).add(zz.node);
  // an "only here" on a single file lights that file's own clump, so the picture never says "nowhere is allowed"
  for (const zz of zones)
    if (zz.type === "only" && zz.file != null) {
      const l = M.files[zz.file] && M.files[zz.file].leaf;
      if (l && l.term) onlyN.add(l.term.node);
      else if (l && l.pile) onlyN.add(l.pile);
    }
  v.hasOnly = zones.some((zz) => zz.type === "only");
  const litAnc = new Set<TNode>();
  for (const n of onlyN) {
    let q: TNode | null = n;
    while (q) {
      litAnc.add(q);
      q = q.parent;
    }
  }
  v.litAnc = litAnc;
  for (const n of M.nodes) {
    const p = n.parent;
    n.vHidden = p ? !!(p.vHidden || p.vColl) : false;
    n.vFold = v.folded.has(n);
    n.vColl = n.vFold || (n.depth >= 2 && n.rad * z < 26) || (n.depth === 1 && n.kind !== "pile" && n.rad * z < 12);
    n.fAnc = n.vFold ? n : p ? p.fAnc || null : null;
    n.keep = keepN.has(n) || (p ? !!p.keep : false);
    n.lit = !v.hasOnly || onlyN.has(n) || !!(p && p.lit);
  }
  // per-file zones
  v.keepFiles = new Set();
  v.onlyFiles = new Set();
  for (const zz of zones) if (zz.file !== undefined && zz.file !== null) (zz.type === "keep" ? v.keepFiles : v.onlyFiles).add(zz.file);
}

function palFor(P: Palette, node: TNode, region: string): Pal | { leafA: string; leafB: string; doc: string } {
  if (region === "root") return P.ROOTPAL;
  if (region === "ground") return P.PILEPAL;
  return P.PAL[node.limb % P.PAL.length];
}

function staticKey(v: TreeView): string {
  let k =
    v.P.key +
    "|" +
    v.zones.map((z) => z.type + (z.node ? z.node.id : "f" + z.file) + (z.waived ? "~" : "")).join(",") +
    "|" +
    [...v.folded].map((n) => n.id).join(",");
  for (const A of v.agents) k += "|" + [...A.created].join(",");
  return k;
}

/** Force the static layer to re-render on the next frame (palette, data). */
export function invalidateStatic(v: TreeView): void {
  if (v.sc) v.sc.key = "";
}

const SC_M = 0.12; // static-layer margin around the viewport (per side)
const SC_DPR_CAP = 1.5; // the static bitmap never exceeds 1.5x: halves canvas memory on Retina
export function render(v: TreeView, now: number): void {
  const { g } = v;
  const cam = v.cam;
  const W = v.W,
    H = v.H,
    dpr = v.dpr,
    z = cam.z;
  computeFlags(v);
  if (!v.sc) {
    const cv = document.createElement("canvas");
    v.sc = { cv, g: cv.getContext("2d"), cam: null, key: "", n: 0 };
  }
  const sc = v.sc;
  if (!sc.g) return;
  const pc = v.prevCam;
  const moved = !pc || pc.x !== cam.x || pc.y !== cam.y || pc.z !== cam.z;
  if (moved) v.lastMove = now;
  v.prevCam = { x: cam.x, y: cam.y, z: cam.z };
  const sw = Math.round(W * (1 + 2 * SC_M)),
    sh = Math.round(H * (1 + 2 * SC_M));
  const sdpr = Math.min(dpr, SC_DPR_CAP);
  const key = staticKey(v);
  let need = !!v.noCache || !sc.cam || sc.key !== key || sc.w !== sw || sc.h !== sh || sc.dpr !== sdpr;
  if (!need && sc.cam) {
    const r = z / sc.cam.z;
    const hw = W / 2 / z,
      hh = H / 2 / z,
      chw = sw / 2 / sc.cam.z,
      chh = sh / 2 / sc.cam.z;
    // while the wheel is still turning, scale the bitmap over a wide window; re-render crisply once the gesture settles
    if (r < 0.72 || r > 1.42) need = true;
    else if (cam.x - hw < sc.cam.x - chw || cam.x + hw > sc.cam.x + chw || cam.y - hh < sc.cam.y - chh || cam.y + hh > sc.cam.y + chh) need = true;
    else if (now - (v.lastMove || 0) > 140 && (r !== 1 || cam.x !== sc.cam.x || cam.y !== sc.cam.y || sc.fast)) need = true;
  }
  if (need) {
    if (sc.w !== sw || sc.h !== sh || sc.dpr !== sdpr) {
      sc.cv.width = Math.max(1, Math.round(sw * sdpr));
      sc.cv.height = Math.max(1, Math.round(sh * sdpr));
      sc.w = sw;
      sc.h = sh;
      sc.dpr = sdpr;
    }
    const t0 = performance.now();
    // mid-gesture the static layer is drawn lean (one canopy layer, no shading); the settle redraw is full
    const fast = now - (v.lastMove || 0) < 140 && !v.noCache;
    renderStatic(v, sc.g, sw, sh, { x: cam.x, y: cam.y, z: cam.z }, sdpr, fast);
    sc.cam = { x: cam.x, y: cam.y, z: cam.z };
    sc.key = key;
    sc.n++;
    sc.lastMs = performance.now() - t0;
    sc.fast = fast;
  }
  {
    const scam = sc.cam!;
    const r = z / scam.z;
    const cx = W / 2 + (scam.x - cam.x) * z,
      cy = H / 2 + (scam.y - cam.y) * z;
    g.setTransform(1, 0, 0, 1, 0, 0);
    g.globalAlpha = 1;
    g.fillStyle = v.P.bg;
    g.fillRect(0, 0, W * dpr, H * dpr);
    g.drawImage(sc.cv, (cx - (sw / 2) * r) * dpr, (cy - (sh / 2) * r) * dpr, sw * r * dpr, sh * r * dpr);
  }
  renderOverlay(v, now);
}

// a 1x256 vertical gradient strip, cached per colour pair; drawn scaled instead of a fresh gradient per frame
const STRIPS = new Map<string, HTMLCanvasElement>();
function strip(c0: string, c1: string): HTMLCanvasElement {
  let s = STRIPS.get(c0 + c1);
  if (s) return s;
  s = document.createElement("canvas");
  s.width = 1;
  s.height = 256;
  const g = s.getContext("2d");
  if (g) {
    const gr = g.createLinearGradient(0, 0, 0, 256);
    gr.addColorStop(0, c0);
    gr.addColorStop(1, c1);
    g.fillStyle = gr;
    g.fillRect(0, 0, 1, 256);
  }
  STRIPS.set(c0 + c1, s);
  return s;
}

function renderStatic(v: TreeView, g: G2, W: number, H: number, cam: { x: number; y: number; z: number }, dpr: number, fast: boolean) {
  const M = v.M,
    MG = mg(M),
    P = v.P,
    z = cam.z;
  const ox = W / 2 - cam.x * z,
    oy = H / 2 - cam.y * z;
  const vx0 = cam.x - W / 2 / z,
    vx1 = cam.x + W / 2 / z,
    vy0 = cam.y - H / 2 / z,
    vy1 = cam.y + H / 2 / z;
  const inView = (x0: number, y0: number, x1: number, y1: number) => x1 >= vx0 && x0 <= vx1 && y1 >= vy0 && y0 <= vy1;
  const leafPx = S0 * z;
  const fade = (a: number, b: number, x: number) => Math.max(0, Math.min(1, (x - a) / (b - a)));
  const hasOnly = !!v.hasOnly;

  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.globalAlpha = 1;
  g.imageSmoothingEnabled = true;
  g.clearRect(0, 0, W, H);
  // sky + soil (cached strips)
  const gy = M.groundY * z + oy;
  const skyH = Math.max(0, Math.min(H, gy));
  if (skyH > 0) g.drawImage(strip(P.sky[0], P.sky[1]), 0, 0, 1, 256, 0, 0, W, Math.max(1, gy));
  if (gy < H) g.drawImage(strip(P.soil[0], P.soil[1]), 0, 0, 1, 256, 0, Math.max(0, gy), W, H - Math.max(0, gy));
  // soft glow behind crown
  {
    const cx = ox,
      cy = M.crownBounds.y0 * 0.45 * z + oy,
      r = Math.max(1, M.Rtyp * 1.25 * z);
    const rg = g.createRadialGradient(cx, cy, 0, cx, cy, r);
    rg.addColorStop(0, P.glow);
    rg.addColorStop(1, P.glow0);
    g.fillStyle = rg;
    g.fillRect(Math.max(0, cx - r), Math.max(0, cy - r), Math.min(W, 2 * r), Math.min(skyH, 2 * r));
  }
  g.setTransform(dpr * z, 0, 0, dpr * z, dpr * ox, dpr * oy);
  // ground line
  g.strokeStyle = P.groundLine;
  g.lineWidth = 1.5 / z;
  g.beginPath();
  g.moveTo(vx0, M.groundY);
  g.lineTo(vx1, M.groundY);
  g.stroke();

  const createdGhost = new Set<number>();
  for (const A of v.agents) for (const id of A.created) createdGhost.add(id);
  // the calm silhouette everywhere; an only-here territory keeps the tree's full colours (the rest fogs to dusk)
  const limbPal = (t: TTerm): Pal => (t.region === "root" ? P.ROOTPAL : (hasOnly && t.node.lit ? P.PALV : P.PAL)[t.node.limb % P.PAL.length]);
  const termStyle = (t: TTerm): Pal | null => {
    const n = t.node;
    if (hasOnly && !n.lit) return P.DUSK;
    if (n.keep) return P.KEEP;
    if (n.fAnc) return null;
    return limbPal(t);
  };

  // ---------- canopy (tree zoom): leafy clumps; individual leaves take over as you zoom in ----------
  const canopyA = 1 - fade(11, 19, leafPx);
  const drawCanopy = (pal: Pal, layer: "sh" | "body" | "hi", path: Path2D, tone: number) => {
    g.fillStyle = layer === "sh" ? pal.cvSh : layer === "hi" ? pal.cvHi : pal.cv[tone];
    g.globalAlpha = canopyA * (layer === "sh" ? 0.75 : layer === "hi" ? 0.5 : 1);
    g.fill(path);
  };
  if (canopyA > 0.02) {
    // only-here dims everything else to dusk: the batches go down in dusk, lit clumps are repainted on top
    // far out: one batched path per limb and layer; closer in, only the clumps in view, each with its leaf ovals
    const coarse = leafPx < 9 || fast,
      layers: Array<"sh" | "body" | "hi"> = fast ? ["body"] : ["sh", "body", "hi"];
    if (MG.mass && MG.mass.n) {
      g.globalAlpha = canopyA * MASS.a;
      drawMass(g, M, hasOnly ? P.DUSK.blob : P.mass);
      g.globalAlpha = 1;
    }
    if (coarse)
      for (const layer of layers)
        for (const B of MG.canopy.values())
          drawCanopy(hasOnly ? P.DUSK : B.limb < 0 ? P.ROOTPAL : P.PAL[B.limb % P.PAL.length], layer, B.coarse[layer], B.tone);
    const vis: Array<[TTerm & TermGeom, Pal]> = [];
    for (const t0 of M.terms) {
      const t = tg(t0);
      if (!t.cv || t.node.fAnc || !inView(t.cx - t.rad, t.cy - t.rad, t.cx + t.rad, t.cy + t.rad)) continue;
      const base = limbPal(t);
      const st = hasOnly ? (t.node.lit ? (t.node.keep ? P.KEEP : base) : coarse ? null : P.DUSK) : t.node.keep ? P.KEEP : coarse ? null : base;
      if (st) vis.push([t, st]);
    }
    for (const layer of layers) for (const [t, st] of vis) drawCanopy(st, layer, (coarse ? t.cvc! : t.cv!)[layer], t.tone);
    g.globalAlpha = 1;
  }
  // ---------- blobs (foliage mass under the leaves once they show) ----------
  const blobA = (t: TTerm) =>
    (1 - canopyA) * (t.region === "root" ? 1 - 0.6 * fade(8, 30, leafPx) : 1 - 0.6 * fade(6, 16, leafPx) - 0.4 * fade(18, 40, leafPx));
  const keepTerms: Array<TTerm & TermGeom> = [];
  v.termsInView = 0;
  for (const t0 of M.terms) {
    const t = tg(t0);
    if (!t.leaves.length) continue;
    if (!inView(t.cx - t.rad, t.cy - t.rad, t.cx + t.rad, t.cy + t.rad)) continue;
    v.termsInView++;
    let st = termStyle(t);
    const folded = !!t.node.fAnc;
    if (t.node.keep) keepTerms.push(t); // a keep-out stays hatched in the only-here dusk too: both rules are in force
    if (folded) st = hasOnly && !t.node.lit ? P.DUSK : t.node.keep ? P.KEEP : limbPal(t);
    if (folded && st) {
      g.globalAlpha = 1;
      g.fillStyle = st.fold;
      g.fill(t.blobFold);
      g.fillStyle = st.blobHi;
      g.globalAlpha = 0.55;
      g.fill(t.blob);
      continue;
    }
    if (!st) continue;
    const a = blobA(t);
    if (a <= 0.02) continue;
    g.globalAlpha = a;
    g.fillStyle = st.blob;
    g.fill(t.blob);
    if (t.region !== "root" && a > 0.7) {
      g.fillStyle = st.blobHi;
      g.fill(t.blobHi);
    }
  }
  g.globalAlpha = 1;

  // ---------- trunk + branches ----------
  {
    // cap the trunk's screen width so a 4.8k-file trunk never becomes a wall at leaf zoom
    const cap = 110 / z;
    if (M.trunkW > cap) {
      if (!MG.trunkCapPath || MG.trunkCapW !== cap) {
        MG.trunkCapPath = trunkPath(M, cap);
        MG.trunkCapW = cap;
      }
      g.fillStyle = P.BARK.crown;
      g.fill(MG.trunkCapPath);
    } else {
      g.fillStyle = P.BARK.crown;
      g.fill(MG.trunkPath);
    }
  }
  g.fillStyle = P.BARK.root;
  g.fill(MG.rootFlare);
  const thin = new Map<string, Branch[]>(),
    thick = new Map<string, Branch[]>();
  const push = (m: Map<string, Branch[]>, k: string, b: Branch) => {
    let a = m.get(k);
    if (!a) m.set(k, (a = []));
    a.push(b);
  };
  v.visBranches = [];
  const litAnc = v.litAnc || new Set<TNode>();
  for (const b of M.branches) {
    const so = b.subOf;
    if (so && (so.vHidden || so.vColl)) continue;
    if (b.node && b.node.vHidden) continue;
    if (!inView(b.bx0, b.by0, b.bx1, b.by1)) continue;
    let col: string;
    const n = b.node || b.owner;
    const lit = !hasOnly || !!(n && (n.lit || (b.node && litAnc.has(b.node)) || (b.virtual && n.lit)));
    const litPath = hasOnly && !!b.node && litAnc.has(b.node);
    if (b.region === "root") col = lit || litPath ? P.BARK.root : P.BARK.rootDusk;
    else col = hasOnly && n && n.lit ? P.BARK.crownHi : lit || litPath ? P.BARK.crown : P.DUSK.bark;
    v.visBranches.push(b);
    if (b.wb * z < 1.3) push(thin, col, b);
    else push(thick, col, b);
  }
  for (const [col, arr] of thick) {
    g.fillStyle = col;
    // fill in chunks: one huge compound path is far slower to rasterise than ~40 polygons at a time
    for (let i0 = 0; i0 < arr.length; i0 += 40) {
      g.beginPath();
      for (let i = i0; i < Math.min(arr.length, i0 + 40); i++) {
        const p = arr[i].poly,
          n = p.length;
        g.moveTo(p[0], p[1]);
        for (let j = 2; j < n; j += 2) g.lineTo(p[j], p[j + 1]);
        g.closePath();
      }
      g.fill();
    }
    // rounded joints at forks (separate path: mixed winding would punch holes)
    g.beginPath();
    for (const b of arr)
      if (!b.own) {
        g.moveTo(b.x1 + b.we * 0.5, b.y1);
        g.arc(b.x1, b.y1, b.we * 0.5, 0, TAU);
      }
    g.fill();
  }
  for (const [col, arr] of thin) {
    g.strokeStyle = col;
    g.lineWidth = Math.max(0.9 / z, 0);
    g.beginPath();
    for (const b of arr) {
      const c = b.c;
      g.moveTo(c[0], c[1]);
      g.bezierCurveTo(c[2], c[3], c[4], c[5], c[6], c[7]);
    }
    g.stroke();
  }

  // ---------- each clump's own twig + leaf stalks (only once single leaves show) ----------
  const twigA = fade(9, 15, leafPx);
  if (twigA > 0.02) {
    // batched by colour and (screen) width step: a close view of a big tree is a handful of strokes, not hundreds
    g.lineCap = "round";
    const batch = new Map<string, { col: string; lw: number; p: Path2D }>(),
      stalks = new Map<string, Path2D>();
    const put = (col: string, lw: number, p: Path2D) => {
      const k = col + "|" + lw;
      let e = batch.get(k);
      if (!e) batch.set(k, (e = { col, lw, p: new Path2D() }));
      e.p.addPath(p);
    };
    for (const t0 of M.terms) {
      const t = tg(t0);
      if (!t.twig || t.node.fAnc || !inView(t.cx - t.rad, t.cy - t.rad, t.cx + t.rad, t.cy + t.rad)) continue;
      const lit = !hasOnly || t.node.lit;
      const col = t.region === "root" ? (lit ? P.BARK.root : P.BARK.rootDusk) : lit ? P.BARK.crown : P.DUSK.bark;
      put(col, Math.max(1.1, Math.round(t.twig.w * 1.6 * z * 2) / 2), t.twig.main);
      put(col, Math.max(0.8, Math.round(t.twig.w * 0.9 * z * 2) / 2), t.twig.side);
      let e = stalks.get(col);
      if (!e) stalks.set(col, (e = new Path2D()));
      e.addPath(t.twig.stalk);
    }
    g.globalAlpha = twigA;
    for (const e of batch.values()) {
      g.strokeStyle = e.col;
      g.lineWidth = e.lw / z;
      g.stroke(e.p);
    }
    g.lineWidth = 0.75 / z;
    g.globalAlpha = twigA * 0.55;
    for (const [col, p] of stalks) {
      g.strokeStyle = col;
      g.stroke(p);
    }
    g.globalAlpha = 1;
    g.lineCap = "butt";
  }

  // ---------- leaves ----------
  const leafAlpha = fade(10, 16, leafPx);
  v.leavesShown = leafAlpha > 0;
  const groups = new Map<string, Leaf[]>();
  const addL = (col: string, l: Leaf) => {
    let a = groups.get(col);
    if (!a) groups.set(col, (a = []));
    a.push(l);
  };
  const drawGroups = (alpha: number) => {
    g.globalAlpha = alpha;
    for (const [col, arr] of groups) {
      g.fillStyle = col;
      g.beginPath();
      for (const l of arr) {
        const q = l.g!;
        g.moveTo(q[0], q[1]);
        g.quadraticCurveTo(q[2], q[3], q[4], q[5]);
        g.quadraticCurveTo(q[6], q[7], q[0], q[1]);
      }
      g.fill();
    }
    g.globalAlpha = 1;
    groups.clear();
  };
  const keepFiles = v.keepFiles || new Set<number>();
  if (leafAlpha > 0) {
    for (const t of M.terms) {
      if (!t.leaves.length || t.node.fAnc) continue;
      if (!inView(t.cx - t.rad, t.cy - t.rad, t.cx + t.rad, t.cy + t.rad)) continue;
      const st = termStyle(t);
      if (!st) continue;
      for (const l of t.leaves) {
        if (l.file.ghost && !createdGhost.has(l.file.id)) continue;
        if (keepFiles.has(l.file.id)) {
          addL(P.KEEP.leafA, l);
          continue;
        }
        addL(l.file.kind === "c" ? (l.shade ? st.leafB : st.leafA) : st.doc, l);
      }
    }
    drawGroups(leafAlpha);
  }
  // ground piles: always drawn as leaves (they are small)
  for (const p of M.piles) {
    const pw = p.pw || 0,
      ph = p.h || 0;
    if (!inView(p.cx - pw, M.groundY - ph * 2, p.cx + pw, M.groundY + 1)) continue;
    v.termsInView++;
    const lit = !hasOnly || p.lit;
    const keep = p.keep;
    for (const l of p.leaves || [])
      addL(!lit ? P.DUSK.leafA : keep ? P.KEEP.leafA : l.shade ? P.PILEPAL.leafB : l.id % 3 ? P.PILEPAL.leafA : P.PILEPAL.leafB, l);
  }
  drawGroups(1);

  // ---------- keep-out hatching: a shape cue that survives colour blindness and every zoom ----------
  const hatch = (clip: Path2D, x0: number, y0: number, x1: number, y1: number, dim?: boolean) => {
    g.save();
    g.clip(clip);
    g.strokeStyle = dim ? P.hatchDim : P.hatch;
    g.lineWidth = Math.max(1.1 / z, 0.6);
    const gap = Math.max(7 / z, S0 * 0.45);
    g.beginPath();
    for (let d = x0 - y1; d < x1 - y0; d += gap) {
      g.moveTo(x0, x0 - d);
      g.lineTo(x1, x1 - d);
    }
    g.stroke();
    g.restore();
  };
  for (const t of keepTerms) hatch(t.blobFold, t.cx - t.rad - S0, t.cy - t.rad - S0, t.cx + t.rad + S0, t.cy + t.rad + S0, hasOnly && !t.node.lit);
  for (const p of M.piles) {
    const pw = p.pw || 0,
      ph = p.h || 0;
    if (p.keep && inView(p.cx - pw, M.groundY - ph * 2, p.cx + pw, M.groundY + 1)) {
      const clipP = new Path2D();
      clipP.ellipse(p.cx, M.groundY - ph * 0.45, pw * 0.62, ph * 0.9, 0, 0, TAU);
      hatch(clipP, p.cx - pw, M.groundY - ph * 2, p.cx + pw, M.groundY);
    }
  }
  for (const id of keepFiles) {
    const l = M.files[id] && M.files[id].leaf;
    if (!l || !inView(l.x - 10, l.y - 10, l.x + 10, l.y + 10)) continue;
    const clipL = new Path2D();
    const r = Math.max(l.len * 0.8, 6 / z);
    clipL.arc(l.x, l.y, r, 0, TAU);
    hatch(clipL, l.x - r, l.y - r, l.x + r, l.y + r);
  }
}

function leafPath(g: G2, l: Leaf) {
  const q = l.g!;
  g.moveTo(q[0], q[1]);
  g.quadraticCurveTo(q[2], q[3], q[4], q[5]);
  g.quadraticCurveTo(q[6], q[7], q[0], q[1]);
}

/** A ghost bud for a planned NEW file: on the rim of the folder's clump. */
function ghostBudPoint(M: Model, node: TNode, i: number): [number, number] {
  const t = node.term as TTerm | undefined;
  if (t && t.leaves && t.leaves.length) {
    const a = -Math.PI / 2 + ((i % 7) - 3) * 0.42;
    return [t.cx + Math.cos(a) * (t.rad + S0 * 0.4), t.cy + Math.sin(a) * (t.rad + S0 * 0.4)];
  }
  const P = nestPoint(M, node);
  return [P[0] + ((i % 5) - 2) * S0 * 0.8, P[1] - S0 * 0.8];
}

function renderOverlay(v: TreeView, now: number) {
  const { g, M, cam } = v;
  const P = v.P;
  const W = v.W,
    H = v.H,
    dpr = v.dpr,
    z = cam.z;
  const ox = W / 2 - cam.x * z,
    oy = H / 2 - cam.y * z;
  const vx0 = cam.x - W / 2 / z,
    vx1 = cam.x + W / 2 / z,
    vy0 = cam.y - H / 2 / z,
    vy1 = cam.y + H / 2 / z;
  const inView = (x0: number, y0: number, x1: number, y1: number) => x1 >= vx0 && x0 <= vx1 && y1 >= vy0 && y0 <= vy1;
  const leafPx = S0 * z;
  const t = v.t;
  const hasOnly = !!v.hasOnly;
  const createdGhost = new Set<number>();
  for (const A of v.agents) for (const id of A.created) createdGhost.add(id);
  g.setTransform(dpr * z, 0, 0, dpr * z, dpr * ox, dpr * oy);
  g.globalAlpha = 1;
  // hovered branch / folder: brighten the whole branch the click will act on
  const hv = v.hover;
  const hoverNode = hv && (hv.node || (hv.branch && branchNode(hv.branch)) || hv.clump || null);
  if (hv && (hv.branch || hv.node)) {
    const b = hv.branch || (hv.node && hv.node.branch);
    if (b) {
      g.fillStyle = b.region === "root" ? P.BARK.rootHi : P.BARK.crownHi;
      if (b.wb * z < 1.3) {
        g.strokeStyle = g.fillStyle;
        g.lineWidth = 1.6 / z;
        const c = b.c;
        g.beginPath();
        g.moveTo(c[0], c[1]);
        g.bezierCurveTo(c[2], c[3], c[4], c[5], c[6], c[7]);
        g.stroke();
      } else {
        const p = b.poly;
        g.beginPath();
        g.moveTo(p[0], p[1]);
        for (let i = 2; i < p.length; i += 2) g.lineTo(p[i], p[i + 1]);
        g.closePath();
        g.fill();
      }
    }
  }
  if (hoverNode && hoverNode.cnt && v.tool !== "explore") {
    // paint tools: outline the subtree that will be painted
    g.strokeStyle = v.tool === "keep" ? P.red : P.green;
    g.lineWidth = 1.5 / z;
    g.setLineDash([6 / z, 4 / z]);
    g.beginPath();
    g.arc(hoverNode.cx, hoverNode.cy, hoverNode.rad + S0 * 0.6, 0, TAU);
    g.stroke();
    g.setLineDash([]);
  }
  const px = 1 / z; // one screen pixel in world units
  // zones as TERRITORIES: a translucent fill over the whole branch hull with a bold outline (screen space)
  drawTerritories(v, ox, oy);
  g.setTransform(dpr * z, 0, 0, dpr * z, dpr * ox, dpr * oy);
  // zone marks for what has no hull: a ground pile, a single file
  for (const zz of v.zones) {
    const col = zz.type === "keep" ? P.red : P.green;
    g.globalAlpha = zz.waived ? 0.45 : 1; // a waived zone is drawn, not enforced
    if (zz.node && zz.node.branch) {
      g.globalAlpha = 1;
      continue;
    } else if (zz.node && zz.node.kind === "pile") {
      g.strokeStyle = col;
      g.lineWidth = 2 * px;
      g.setLineDash([5 * px, 4 * px]);
      g.beginPath();
      g.ellipse(zz.node.cx, M.groundY - (zz.node.h || 0) * 0.45, (zz.node.pw || 0) * 0.62, (zz.node.h || 0) * 0.9, 0, Math.PI, TAU);
      g.stroke();
      g.setLineDash([]);
    } else if (zz.file !== undefined && zz.file !== null) {
      const l = M.files[zz.file] && M.files[zz.file].leaf;
      if (!l) {
        g.globalAlpha = 1;
        continue;
      }
      g.strokeStyle = col;
      g.lineWidth = 2 * px;
      g.beginPath();
      g.arc(l.x, l.y, Math.max(l.len * 0.75, 6 * px), 0, TAU);
      g.stroke();
    }
    g.globalAlpha = 1;
  }
  // LIT PATHS: the wood from the trunk to every changed file is lit, in the changing bird's colour
  const lit = litMap(v);
  v.litInfo = lit;
  drawVeins(v, px, inView);
  // read dots, edits, buds
  v.budHits = [];
  for (const A of v.agents) {
    const col = A.ag.color;
    // reads
    g.fillStyle = col;
    g.beginPath();
    for (const [id] of A.reads) {
      const l = M.files[id] && M.files[id].leaf;
      if (!l || !inView(l.x - 5, l.y - 5, l.x + 5, l.y + 5)) continue;
      const r = Math.max(3 * px, l.len * 0.17);
      const dx = l.x - Math.cos(l.ang) * l.len * 0.18,
        dy = l.y - Math.sin(l.ang) * l.len * 0.18;
      g.moveTo(dx + r, dy);
      g.arc(dx, dy, r, 0, TAU);
    }
    g.fill();
    // plan buds
    for (const id of A.plan) {
      const l = M.files[id] && M.files[id].leaf;
      if (!l || A.edits.has(id)) continue;
      drawBud(g, P, l.x, l.y, l.len, col, px, !!M.files[id].ghost);
    }
    // ghost buds where a planned new file will grow
    A.planNew.forEach((pn, i) => {
      const [bx, by] = ghostBudPoint(M, pn.node, i);
      if (!inView(bx - 20, by - 20, bx + 20, by + 20)) return;
      drawBud(g, P, bx, by, S0 * 0.6, col, px, true);
      const [sx, sy] = [bx * z + ox, by * z + oy];
      const r = Math.max(6, S0 * 0.6 * 0.45 * z + 4);
      v.budHits!.push({ r: [sx - r, sy - r, sx + r, sy + r], A, path: pn.path });
    });
  }
  // a soft light around every change, wide enough to show past a nest or a perched bird at any zoom; the newest
  // changes shine brightest (freshness), the ones made before the map armed the least
  const fresh = freshness(v);
  for (const A of v.agents) {
    for (const [id] of A.edits) {
      const l = M.files[id] && M.files[id].leaf;
      if (!l) continue;
      const own = l.term ? l.term.node : l.pile;
      if (hasOnly && own && !own.lit && onlyBinds(v, A)) continue;
      const R = Math.max(l.len * 1.6, 26 * px);
      if (!inView(l.x - R, l.y - R, l.x + R, l.y + R)) continue;
      const gr = g.createRadialGradient(l.x, l.y, 0, l.x, l.y, R);
      gr.addColorStop(0, withAlpha(A.ag.color, (P.light ? 0.42 : 0.5) * (0.3 + 0.7 * (fresh.get(id) ?? 1))));
      gr.addColorStop(1, withAlpha(A.ag.color, 0));
      g.fillStyle = gr;
      g.fillRect(l.x - R, l.y - R, 2 * R, 2 * R);
    }
  }
  // blast radius (gold): everything each agent's edits so far could break, plus a pinned / hovered file
  v.badges = [];
  v.badgeSets = new Map();
  const blasts: BlastSpec[] = [];
  // (LIT PATHS: never by default — what could break is asked for: hover or select a changed leaf)
  if (v.pinBlast != null) blasts.push({ fid: v.pinBlast, live: false, hover: false, ag: null, A: null });
  const selF = v.sel && v.sel.leaf ? v.sel.leaf.file.id : null;
  if (selF != null && selF !== v.pinBlast && selF !== v.hoverBlast && v.agents.some((A) => A.edits.has(selF)))
    blasts.push({ fid: selF, live: false, hover: false, ag: null, A: null, quiet: true });
  if (v.hoverBlast != null && v.hoverBlast !== v.pinBlast) blasts.push({ fid: v.hoverBlast, live: false, hover: true, ag: null, A: null, quiet: true });
  for (const B of blasts) drawBlast(v, B, px, inView);
  collectBadges(v);
  // what could break, on demand: quiet gold territories per folder (counted), never scattered gold leaves
  drawRisk(v, px);
  // search / selection pulse
  if (v.pulse && now >= v.pulse.t0 && now - v.pulse.t0 < 2600) {
    const k = (now - v.pulse.t0) / 2600;
    const p = v.pulse;
    g.strokeStyle = P.text;
    g.globalAlpha = 1 - k;
    g.lineWidth = 2 * px;
    g.beginPath();
    g.arc(p.x, p.y, (p.r || 8 * px) + k * 30 * px, 0, TAU);
    g.stroke();
    g.globalAlpha = 1;
  }
  const selLeaf = v.sel && v.sel.leaf ? v.sel.leaf : null;
  if (selLeaf) {
    const l = selLeaf;
    // in only-here dusk the chosen leaf is spotlit so the destination is never grey-on-grey — unless a bird
    // already painted it (the legend promises the bird's colour on an edited leaf, so keep that)
    const own = l.term ? l.term.node : l.pile;
    const painted = v.agents.some((A) => A.edits.has(l.file.id));
    if (hasOnly && own && !own.lit && !painted) {
      const st = palFor(P, own, l.region);
      g.fillStyle = l.file.kind === "c" ? st.leafA : st.doc;
      g.beginPath();
      leafPath(g, l);
      g.fill();
    }
    const r = Math.max(l.len * 0.8, 8 * px);
    g.strokeStyle = P.selHalo;
    g.lineWidth = 5 * px;
    g.beginPath();
    g.arc(l.x, l.y, r, 0, TAU);
    g.stroke();
    g.strokeStyle = P.sel;
    g.lineWidth = 2 * px;
    g.beginPath();
    g.arc(l.x, l.y, r, 0, TAU);
    g.stroke();
  }
  // breadcrumb marker: the last leaf you visited keeps a quiet dotted ring (and its name) after the card closes
  const markLeaf = v.markLeaf && v.markLeaf !== selLeaf ? v.markLeaf : null;
  if (markLeaf) {
    const l = markLeaf;
    const r = Math.max(l.len * 0.8, 8 * px);
    g.strokeStyle = P.mark;
    g.lineWidth = 1.4 * px;
    g.setLineDash([2.5 * px, 2.5 * px]);
    g.beginPath();
    g.arc(l.x, l.y, r, 0, TAU);
    g.stroke();
    g.setLineDash([]);
  }
  // the dashed folder ring lives exactly as long as that folder's card (the trunk's ring would be the whole tree: none)
  if (v.sel && v.sel.node && v.sel.node.cnt && v.sel.node.depth >= 1 && v.infoNode === v.sel.node) {
    const n = v.sel.node;
    g.strokeStyle = P.mark;
    g.lineWidth = 1.6 * px;
    g.setLineDash([5 * px, 4 * px]);
    g.beginPath();
    g.arc(n.cx, n.cy, n.rad + S0 * 0.6, 0, TAU);
    g.stroke();
    g.setLineDash([]);
  }
  // hover: a thin dashed ring in the muted colour, so it never reads as the (solid) selection
  if (hv && hv.leaf && hv.leaf !== selLeaf) {
    const l = hv.leaf;
    g.strokeStyle = P.hover;
    g.lineWidth = 1.1 * px;
    g.setLineDash([2 * px, 2 * px]);
    g.beginPath();
    g.arc(l.x, l.y, Math.max(l.len * 0.72, 6 * px), 0, TAU);
    g.stroke();
    g.setLineDash([]);
  }

  // ---------- screen-space: labels, nests, badges, birds ----------
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  const toS = (x: number, y: number): [number, number] => [x * z + ox, y * z + oy];
  v.toS = toS;
  const occ: R4[] = [];
  const fits = (x0: number, y0: number, x1: number, y1: number) => {
    for (const o of occ) if (x1 > o[0] && x0 < o[2] && y1 > o[1] && y0 < o[3]) return false;
    return true;
  };
  const reserve = (x0: number, y0: number, x1: number, y1: number) => {
    occ.push([x0, y0, x1, y1]);
  };
  for (const r of v.hudRects || []) reserve(r[0], r[1], r[2], r[3]); // HUD panels are occupied space
  const hud = v.hudRects || [];
  const underHud = (x: number, y: number) => {
    for (const r of hud) if (x >= r[0] && x <= r[2] && y >= r[1] && y <= r[3]) return true;
    return false;
  };
  const free = v.free || { x0: 0, y0: 0, x1: W, y1: H };
  v.labelHits = [];
  v.tagHits = [];
  v.birdHits = [];
  v.nestHits = [];
  v.badgeHits = [];
  v.nestLabelled = new Set();
  v.emptyHint = "";
  // foliage on screen: a name may sit on its OWN clump, never on another folder's (it would name the wrong leaves),
  // and an outside name never sits inside a ring that outlines a folder (the selected / hovered one)
  const fol = buildFoliageField(v, toS, hoverNode);
  v.fol = fol;
  // the zones' territories belong to their folders: no other folder's name may sit on (or nearer to) them
  for (const T of v.terrPolys || []) fol.addTerr(T.n, T.polys);
  const nestPx = Math.max(18, Math.min(40, 12 + leafPx * 1.1));
  const birdPx = Math.max(26, Math.min(48, 16 + leafPx * 1.3));
  // birds first (position only): their bodies are reserved before anything else is placed, so no name
  // can hide under a bird; a bird under a HUD panel counts as off-screen and gets an edge pointer
  const raw = v.agents.map((A) => birdPose(v, A, t));
  // where each bird is (world), for the follow camera — a fading helper still counts until it drops off
  v.birdWorld = {};
  v.agents.forEach((A, i) => {
    const p = raw[i];
    if (p) v.birdWorld![A.ag.key] = [p.x, p.y];
  });
  // a finished helper flies home into its parent's nest and fades there
  const alphas = v.agents.map((A) => birdAlpha(v, A, t));
  const poses = raw.map((p, i) => (alphas[i] > 0 ? p : null));
  const pxOf = (A: AgentState) => (A.ag.parent ? Math.round(birdPx * SUB_SCALE) : birdPx);
  interface BirdScr {
    A: AgentState;
    sx: number;
    sy: number;
    pose: Pose;
    body: R4;
    visible: boolean;
    ptr?: { bx: number; by: number; tw: number; th: number; t2: string };
  }
  const birdS: Array<BirdScr | null> = [];
  v.agents.forEach((A, i) => {
    const pose = poses[i];
    if (!pose) {
      birdS.push(null);
      return;
    }
    let [sx, sy] = toS(pose.x, pose.y);
    if (pose.leaf) sy -= Math.max(4, pose.leaf.len * z * 0.45);
    const bp = pxOf(A);
    if (A.ag.parent && !pose.fly) {
      // a helper sitting where its parent sits (the nest, the same leaf) perches beside it, not on it
      const pi = v.agents.findIndex((P) => P.ag.key === A.ag.parent);
      const pp = pi >= 0 ? poses[pi] : null;
      if (pose.nest || (pp && !pp.fly && pp.leaf && pp.leaf === pose.leaf)) sx += ((A.ag.sub || 1) % 2 ? 1 : -1) * birdPx * (0.5 + 0.22 * Math.floor(((A.ag.sub || 1) - 1) / 2));
    }
    const body: R4 = [sx - bp * 0.65, sy - bp * 0.95, sx + bp * 0.65, sy + bp * 0.12];
    const visible = sx > free.x0 + 6 && sx < free.x1 - 6 && sy > free.y0 + 6 && sy < free.y1 - 6 && !underHud(sx, sy - birdPx * 0.4);
    birdS.push({ A, sx, sy, pose, body, visible });
    if (visible) reserve(body[0], body[1], body[2], body[3]);
  });
  // what an edge pointer may never cover: the nests (claimed now, before any tag or name is placed) and the
  // selection ring (a pointer laid over the selected file would read as that bird working there)
  for (const A of v.agents) {
    if (!A.nest || !A.cur || A.ag.parent) continue; // a helper has no nest of its own: it comes home to its parent's
    const Pn = nestPoint(M, A.nest);
    const [sx, sy] = toS(Pn[0], Pn[1]);
    reserve(sx - nestPx * 0.7, sy - nestPx * 0.3, sx + nestPx * 0.7, sy + nestPx * 0.5);
    // a nest says where its folder is: a name beside it reads as that folder's
    if (A.nest.depth >= 1) fol.addNest(sx - nestPx * 0.7, sy - nestPx * 0.3, sx + nestPx * 0.7, sy + nestPx * 0.5, A.nest);
  }
  if (v.sel && v.sel.leaf) {
    const l = v.sel.leaf;
    const [lx, ly] = toS(l.x, l.y);
    const r = Math.max(l.len * 0.8 * z, 8) + 8;
    reserve(lx - r, ly - r, lx + r, ly + r);
  } else if (v.sel && v.sel.node && v.sel.node.cnt && v.sel.node.depth >= 1 && v.sel.node.rad * z < 140) {
    const n = v.sel.node;
    const [nx, ny] = toS(n.cx, n.cy);
    const r = n.rad * z + 6;
    reserve(nx - r, ny - r, nx + r, ny + r);
  }
  // "where am I" claims its corner before any label can take it (after the birds: it never sits on one)
  drawBreadcrumb(v, g, free, fits, reserve);
  // off-screen birds: an edge pointer for EVERY bird you cannot see — a compact chip, its glyph and an arrow (what
  // it is doing is on hover and in the bird index), hugging the edge of the canvas you can see in the bird's
  // direction: never under a panel (the details card on the right counts), never slid into the middle of the
  // tree where it would sit beside another folder. Its spot is claimed now, before any name; it is drawn last
  // so nothing covers it; a click follows the bird
  const edge = { ...free };
  for (const r of hud) {
    if (r[3] - r[1] < (free.y1 - free.y0) * 0.5) continue; // only a full-height panel moves the edge
    if ((r[0] + r[2]) / 2 > (free.x0 + free.x1) / 2) edge.x1 = Math.max(edge.x0 + 80, Math.min(edge.x1, r[0]));
    else edge.x0 = Math.min(edge.x1 - 80, Math.max(edge.x0, r[2]));
  }
  for (const B of birdS) {
    if (!B || B.visible || (B.A.ag.parent && B.A.done)) continue;
    const { A, sx, sy } = B;
    const M2 = 14;
    const ex = Math.max(edge.x0 + M2, Math.min(edge.x1 - M2, sx)),
      ey = Math.max(edge.y0 + M2, Math.min(edge.y1 - M2, sy));
    const dx = sx - ex,
      dy = sy - ey;
    const arrow =
      Math.abs(dx) > Math.abs(dy) * 2.2 ? (dx < 0 ? "←" : "→") : Math.abs(dy) > Math.abs(dx) * 2.2 ? (dy < 0 ? "↑" : "↓") : dy < 0 ? (dx < 0 ? "↖" : "↗") : dx < 0 ? "↙" : "↘";
    const t2 = `${A.ag.glyph} ${arrow}`;
    g.font = `600 10.5px ${P.font}`;
    const tw = g.measureText(t2).width + 14,
      th = 18;
    // the chip sits ON the edge it points through; it slides ALONG that edge to find room, and only a step or
    // two inward
    const horiz = Math.abs(dx) >= Math.abs(dy);
    const clampX = (x: number) => Math.max(edge.x0 + 4, Math.min(edge.x1 - tw - 4, x)),
      clampY = (y: number) => Math.max(edge.y0 + 4, Math.min(edge.y1 - th - 4, y));
    const ax = clampX(ex - tw / 2),
      ay = clampY(ey - th / 2);
    const inX = dx > 0 ? -1 : dx < 0 ? 1 : 0,
      inY = dy > 0 ? -1 : dy < 0 ? 1 : 0;
    let bx: number | null = null,
      by = 0;
    search: for (let inward = 0; inward <= 2; inward++)
      for (let k = 0; k <= 14; k++)
        for (const sgn of k ? [1, -1] : [1]) {
          const x0 = clampX(horiz ? ax + inX * inward * (tw + 6) : ax + sgn * k * (tw * 0.6 + 6)),
            y0 = clampY(horiz ? ay + sgn * k * (th + 4) : ay + inY * inward * (th + 6));
          if (fits(x0, y0, x0 + tw, y0 + th)) {
            bx = x0;
            by = y0;
            break search;
          }
        }
    if (bx === null) {
      bx = ax;
      by = ay;
    }
    reserve(bx, by, bx + tw, by + th);
    B.ptr = { bx, by, tw, th, t2 };
  }
  // nests, each with a pill that names its folder: "lib · Wren's nest" answers "where is the agent" at any zoom
  for (const A of v.agents) {
    if (!A.nest || !A.cur || A.ag.parent) continue;
    const Pn = nestPoint(M, A.nest);
    const [sx, sy] = toS(Pn[0], Pn[1]);
    if (sx < -60 || sx > W + 60 || sy < -60 || sy > H + 60) continue;
    const age = t - A.nestSince;
    drawNest(g, P, sx, sy, nestPx, A.ag, v.playing ? Math.min(1, Math.max(0.05, age / 1.0)) : 1);
    const r: R4 = [sx - nestPx * 0.7, sy - nestPx * 0.3, sx + nestPx * 0.7, sy + nestPx * 0.5];
    reserve(r[0], r[1], r[2], r[3]);
    v.nestHits.push({ r, A });
    if (nestPx < 18 || W < 700) continue; // a small pane: the bird's own tag already says where it works
    const n = A.nest;
    // a nest in a changed folder says it the way the changed-folder pills do: "backend › web/core ●4 · tree-demo's nest"
    const li = n.depth >= 1 && v.litInfo ? v.litInfo.get(n) : undefined;
    const folder = n === M.crown ? "trunk" : li ? folderPath(n) : n.kind === "root" ? n.label || n.name : n.disp || shortName(n.name);
    const segs = li ? li.segs.map((q) => ({ col: q.col, t: " " + q.glyph + q.n })) : [];
    // ONE label per place: the nest's folder pill is the same pill a changed folder gets (the eggs and the bird's
    // own tag say whose nest it is); only an unchanged nest folder says "nest"
    const t1 = folder,
      t2 = li ? "" : " · nest";
    g.font = `600 11px ${P.font}`;
    const w1 = g.measureText(t1).width;
    g.font = `700 11px ${P.font}`;
    const ws = segs.reduce((a, q) => a + g.measureText(q.t).width, 0);
    g.font = `500 10.5px ${P.font}`;
    const w2 = g.measureText(t2).width;
    const tw = w1 + ws + w2 + 14,
      th = 18;
    let bx: number | null = null,
      by = 0;
    for (const [ax, ay] of [
      [-tw / 2, nestPx * 0.55 + 3],
      [-tw / 2, -nestPx * 0.5 - th - 3],
      [nestPx * 0.75 + 4, -th / 2],
      [-nestPx * 0.75 - 4 - tw, -th / 2],
      [-tw / 2, nestPx * 0.55 + 24],
      [-tw / 2, -nestPx * 0.5 - th - 24],
    ]) {
      const x0 = sx + ax,
        y0 = sy + ay;
      if (x0 > -10 && x0 + tw < W + 10 && y0 > -10 && y0 + th < H + 10 && fits(x0, y0, x0 + tw, y0 + th) && fol.belongs(x0, y0, x0 + tw, y0 + th, n)) {
        bx = x0;
        by = y0;
        break;
      }
    }
    if (bx === null) continue;
    const duskN = hasOnly && !n.lit && onlyBinds(v, A);
    if (duskN) g.globalAlpha = 0.5;
    quietPill(g, P, bx, by, tw, th);
    g.textBaseline = "middle";
    g.font = `600 11px ${P.font}`;
    g.fillStyle = duskN ? P.duskLabel : P.pillText;
    g.fillText(t1, bx + 7, by + th / 2 + 0.5);
    g.font = `700 11px ${P.font}`;
    let sx2 = bx + 7 + w1;
    for (const q of segs) {
      g.fillStyle = q.col;
      g.fillText(q.t, sx2, by + th / 2 + 0.5);
      sx2 += g.measureText(q.t).width;
    }
    g.font = `500 10.5px ${P.font}`;
    g.fillStyle = P.muted;
    if (t2) g.fillText(t2, sx2, by + th / 2 + 0.5);
    g.globalAlpha = 1;
    reserve(bx, by, bx + tw, by + th);
    if (n.depth >= 1) {
      v.labelHits.push({ r: [bx, by, bx + tw, by + th], n });
      v.nestLabelled.add(n);
    } else v.nestHits.push({ r: [bx, by, bx + tw, by + th], A });
  }
  // zone signs: named, clickable — placed after the nests and their pills (a sign never covers where an agent
  // lives), before folder names and badges (they make room for it). A sign stays AT its own territory: when the
  // full "⛔ Keep out · backend/providers" has no room there it shortens to the folder's name, then to the bare
  // sign — it never wanders off to sit beside another limb.
  for (const zz of v.zones) {
    let Pt: [number, number] | null = null;
    // a territory's sign stands at its top edge, where it reads as the name of the whole area
    if (zz.node && zz.node.branch && zz.node.cnt) Pt = [zz.node.cx, zz.node.cy - zz.node.rad - S0 * 0.6];
    else if (zz.node && zz.node.branch) Pt = [zz.node.branch.pts[4][0], zz.node.branch.pts[4][1]];
    else if (zz.node && zz.node.kind === "pile") Pt = [zz.node.cx, M.groundY - (zz.node.h || 0) * 1.3];
    else if (zz.node && zz.node.cnt) Pt = [zz.node.cx, zz.node.cy];
    else if (zz.file != null && M.files[zz.file] && M.files[zz.file].leaf) {
      const l = M.files[zz.file].leaf!;
      Pt = [l.x, l.y];
    }
    if (!Pt) continue;
    const [sx, sy] = toS(Pt[0], Pt[1]);
    if (sx < -200 || sx > W + 200 || sy < -100 || sy > H + 100) continue;
    const sign = zz.type === "keep" ? "⛔" : "✓";
    // the territory's own short name: its last two path segments ("web/core"), so it is never just "core"
    const leafName = tailPath(zz.label);
    // the full tag says the same path the Rules list says, so one rule never looks like two
    // a rule that already stopped an edit says so for as long as the map shows it (the ✕ bonk fades in seconds)
    let nb = 0;
    for (const A of v.agents) for (const bk of A.blocked) if (bk.z && bk.z.z.id === zz.z.id) nb++;
    // ONE wording for the count at every pane size (the bird index and the Rules list say it the same way)
    const bt = nb ? ` · ${blockedTxt(nb)}` : "";
    const texts = [
      (zz.type === "keep" ? "⛔ Keep out · " : "✓ Only here · ") + zz.label + (zz.waived ? " · allowed here" : bt),
      `${sign} ${leafName}${zz.waived ? " · allowed" : bt}`,
      ...(nb && !zz.waived ? [`${sign} ${blockedTxt(nb)}`] : []),
      sign,
    ];
    g.font = `700 11px ${P.font}`;
    const th = 19;
    const own = zz.node || (zz.file != null && M.files[zz.file] ? M.files[zz.file].node : null) || null;
    // the territory's own rim: a sign may sit there over its own leaves, never further than a short leader away
    const rr = zz.node && zz.node.cnt ? Math.min(60, zz.node.rad * z * 0.5) : 0;
    const anchor: Array<[number, number, number]> = [[sx, sy, 2]];
    const whole = (x0: number, w: number, y0: number) => x0 >= 2 && x0 + w <= v.W - 2 && y0 >= 2 && y0 + th <= v.H - 2;
    let txt = texts[0],
      tw = 0,
      bx = 0,
      by = 0,
      found = false;
    // a small pane starts at the folder's name (the rail's rule tile and the Rules list say the full path)
    // the TERRITORY is the loud part; its sign is a small tag on the territory's edge ("⛔ providers"). The full
    // "Keep out · backend/providers" waits until you are close (the Rules list and the tag's tooltip say the path)
    // (a small pane says the blocked count as a bare "✕1")
    // the territory itself (screen): its sign may also stand inside it, at its middle or its foot
    const terr = (v.terrPolys || []).find((T) => T.n === zz.node);
    let tb: R4 | null = null;
    if (terr) {
      tb = [Infinity, Infinity, -Infinity, -Infinity];
      for (const poly of terr.polys)
        for (let i = 0; i < poly.length; i += 2) {
          tb[0] = Math.min(tb[0], poly[i]);
          tb[1] = Math.min(tb[1], poly[i + 1]);
          tb[2] = Math.max(tb[2], poly[i]);
          tb[3] = Math.max(tb[3], poly[i + 1]);
        }
    }
    const textsHere = W < 700 || leafPx < 16 ? texts.slice(1) : texts;
    // first well clear of every other folder's nest, glowing leaves and names (a sign hard by another folder's nest
    // makes THAT folder read as the blocked one); only then merely not touching them
    search: for (const clear of [24, 4])
      for (const t0 of textsHere) {
        const w = g.measureText(t0).width + 16;
        const spots: Array<[number, number]> = [
          [sx - w / 2, sy - th - 2],
          [sx - w / 2, sy + 2],
          [sx + 10, sy - th / 2],
          [sx - w - 10, sy - th / 2],
          [sx - w / 2, sy - th - 18],
          [sx - w / 2, sy + rr],
          [sx - w / 2, sy + rr + 18],
        ];
        if (tb) {
          const mx = (tb[0] + tb[2]) / 2,
            my = (tb[1] + tb[3]) / 2;
          spots.push([mx - w / 2, my - th / 2], [mx - w / 2, tb[3] - th - 4], [mx - w / 2, tb[3] + 2], [tb[0] - w - 4, my - th / 2], [tb[2] + 4, my - th / 2]);
        }
        // first on air (or its own leaves) and whole on screen; a shorter text before a farther spot
        for (const clean of [true, false])
          for (const [x0, y0] of spots) {
            // never over (or nearer to) another folder's foliage, nest or territory than to its own territory
            if (fits(x0, y0, x0 + w, y0 + th) && whole(x0, w, y0) && (!clean || !fol.hit(x0, y0, x0 + w, y0 + th, own, true)) && fol.belongs(x0, y0, x0 + w, y0 + th, own, anchor, 220, clear)) {
              bx = x0;
              by = y0;
              found = true;
              txt = t0;
              tw = w;
              break search;
            }
          }
      }
    if (!found) {
      // no free room at all: the bare sign on its own territory (over its own leaves is fine — they are the zone)
      txt = sign;
      tw = g.measureText(sign).width + 16;
      bx = sx - tw / 2;
      by = sy - th / 2;
    }
    if (sx >= 0 && sx <= v.W) bx = Math.max(2, Math.min(v.W - tw - 2, bx)); // never half off the canvas
    g.globalAlpha = zz.waived ? 0.6 : 1;
    // a solid sign in the zone's colour (a waived one stays an outline): unmistakable at any zoom
    const zc = zz.type === "keep" ? P.red : P.green;
    const near = (Math.abs(by + th / 2 - sy) < th + 4 && sx >= bx - 4 && sx <= bx + tw + 4) || (!!terr && terr.polys.some((poly) => rectPolyDist(bx, by, bx + tw, by + th, poly) === 0));
    if (!near) {
      g.strokeStyle = zc;
      g.lineWidth = 1.5;
      g.beginPath();
      g.moveTo(sx, sy);
      g.lineTo(Math.max(bx + 4, Math.min(bx + tw - 4, sx)), by < sy ? by + th : by);
      g.stroke();
    }
    roundRect(g, bx, by, tw, th, 6);
    g.fillStyle = zz.waived ? (zz.type === "keep" ? P.keepBg : P.onlyBg) : zc;
    g.fill();
    g.strokeStyle = zc;
    g.lineWidth = 1;
    if (zz.waived) g.setLineDash([4, 3]);
    g.stroke();
    g.setLineDash([]);
    g.fillStyle = zz.waived ? (zz.type === "keep" ? P.keepText : P.onlyText) : zz.type === "keep" ? P.signKeepText : P.signOnlyText;
    g.textBaseline = "middle";
    g.fillText(txt, bx + 8, by + th / 2 + 0.5);
    g.globalAlpha = 1;
    reserve(bx, by, bx + tw, by + th);
    v.tagHits.push({ r: [bx, by, bx + tw, by + th], z: zz });
    if (own) fol.addTag(bx, by, bx + tw, by + th, own);
  }
  // the changes go over the nests: a nest bowl never hides a changed leaf
  // (each changed leaf's spot is reserved: no name, tag or badge is ever laid over a change)
  drawEdits(v, now, ox, oy, reserve);
  // the biggest folders are named next: nothing may evict a top-level folder's name
  drawLabels(v, g, toS, fits, reserve, leafPx, inView, createdGhost, "limbs");
  // the selected / marked leaf and each bird's current file are always named
  drawPriorityLeafLabels(v, g, toS, fits, reserve, leafPx, birdS);
  // could-break counts (on demand)
  drawRiskChips(v, g, toS, fits, reserve);
  // badges
  for (const bd of v.badges || []) {
    const [sx, sy] = toS(bd.x, bd.y);
    if (sx < -300 || sx > W + 300 || sy < -100 || sy > H + 100) continue;
    g.font = `600 11px ${P.font}`;
    const dotW = bd.ag ? 12 : 0;
    const tw = g.measureText(bd.text).width + 16 + dotW,
      th = 19;
    let ok = false,
      bx = 0,
      by = 0;
    const inScreen = (x0: number, y0: number, x1: number, y1: number) => x0 >= 2 && x1 <= W - 2 && y0 >= 2 && y1 <= H - 2; // a badge is never half off the screen
    for (const [ax, ay] of [
      [8, -th - 4],
      [-tw - 8, -th - 4],
      [8, 6],
      [-tw - 8, 6],
      [-tw / 2, -th - 14],
      [-tw / 2, 12],
      [8, -th - 26],
      [-tw - 8, -th - 26],
      [-tw / 2, -th - 36],
      [-tw / 2, 30],
    ]) {
      bx = sx + ax;
      by = sy + ay;
      if (inScreen(bx, by, bx + tw, by + th) && fits(bx, by, bx + tw, by + th)) {
        ok = true;
        break;
      }
    }
    let txt = bd.text,
      w = tw;
    if (!ok) {
      // never drop a count silently: fall back to a compact chip near the anchor
      txt = bd.short;
      w = g.measureText(txt).width + 14 + dotW;
      let ok2 = false;
      for (const [ax, ay] of [
        [-w / 2, -th / 2],
        [8, -th - 2],
        [-w - 8, -th - 2],
        [8, 4],
        [-w - 8, 4],
        [-w / 2, -th - 12],
        [-w / 2, 10],
      ]) {
        bx = sx + ax;
        by = sy + ay;
        if (inScreen(bx, by, bx + w, by + th) && fits(bx, by, bx + w, by + th)) {
          ok2 = true;
          break;
        }
      }
      if (!ok2) {
        // the bare number, on the nearest free spot around the anchor (never on a name); only as a last resort on top
        txt = String(bd.n);
        w = g.measureText(txt).width + 14 + dotW;
        bx = Math.max(2, Math.min(W - w - 2, sx - w / 2));
        by = Math.max(2, Math.min(H - th - 2, sy - th / 2));
        search: for (let rr = 12; rr <= 96; rr += 12)
          for (let k = 0; k < 12; k++) {
            const a = (k / 12) * TAU,
              x0 = sx + Math.cos(a) * rr - w / 2,
              y0 = sy + Math.sin(a) * rr - th / 2;
            if (inScreen(x0, y0, x0 + w, y0 + th) && fits(x0, y0, x0 + w, y0 + th)) {
              bx = x0;
              by = y0;
              break search;
            }
          }
      }
    }
    g.globalAlpha = bd.alpha;
    roundRect(g, bx, by, w, th, 9);
    g.fillStyle = P.badgeBg;
    g.fill();
    // the border is the editing bird's colour (gold for a pinned / hovered file), so a badge says whose edit it counts
    g.strokeStyle = bd.ag ? bd.ag.color : P.gold;
    g.lineWidth = bd.ag ? 1.6 : 1;
    if (bd.pin) g.setLineDash([3, 2]);
    g.stroke();
    g.setLineDash([]);
    let tx = bx + 8;
    if (bd.ag) {
      g.fillStyle = bd.ag.color;
      g.beginPath();
      g.arc(tx + 3.5, by + th / 2, 3.5, 0, TAU);
      g.fill();
      tx += dotW;
    }
    g.fillStyle = P.gold;
    g.textBaseline = "middle";
    g.fillText(txt, tx, by + th / 2 + 0.5);
    g.globalAlpha = 1;
    reserve(bx, by, bx + w, by + th);
    v.badgeHits.push({ r: [bx, by, bx + w, by + th], bd });
  }
  // birds
  v.birdScreen = [];
  const pointers: BirdScr[] = [];
  // folders already named on screen: a bird working in one says just the file (its folder's pill says where)
  const namedN = new Set<TNode>();
  for (const h of v.labelHits || []) if (h.n) namedN.add(h.n);
  const terrs = v.terrPolys || [];
  for (const [bi, B] of birdS.entries()) {
    if (!B) continue;
    const { A, sx, sy, pose, body } = B;
    const birdPx = pxOf(A),
      alpha = alphas[bi];
    v.birdScreen.push({ A, sx, sy, pose });
    // zoomed out, the file alone could be read as belonging to whatever folder name sits next to the bird: say its folder
    // (a nested folder carries its top-level folder, "backend/config/program.py", so a bird perched beside another
    // limb's name is never read as working in that limb)
    const fname = A.file
      ? leafPx < 14 && A.file.node && A.file.node.depth >= 1 && A.file.node.kind !== "pile" && !namedN.has(A.file.node)
        ? folderPath(A.file.node).replace(" › ", "/") + "/" + A.file.name
        : A.file.name
      : "";
    const doing = pose.fly ? (A.file && !pose.nest ? ` → ${fname}` : " → nest") : pose.blocked ? ` ✕ ${fname}` : A.file && !pose.nest ? ` · ${fname}` : "";
    if (!B.visible) {
      pointers.push(B);
      continue;
    }
    g.globalAlpha = alpha;
    drawBird(g, sx, sy, birdPx, A.ag.color, pose, now, !v.reducedMotion);
    // name tag; a perched bird's tag goes ABOVE it first, so the leaf it sits on (and that leaf's name) stay in view
    // a small pane: the glyph says who (the bird index on the left names it), the tag says where
    const tag0 =
      W < 700 && !(A.ag.parent && A.done) && A.file
        ? `${A.ag.glyph} ${pose.fly ? "→ " : pose.blocked ? "✕ " : ""}${A.file.name}`
        : `${A.ag.glyph} ${clip(A.ag.short || A.ag.name)}${A.ag.parent && A.done ? (pose.fly ? " → home" : " · done") : doing}`;
    g.font = `600 10.5px ${P.font}`;
    const th = 16;
    // a crowded spot shortens the tag (the bird index names it) rather than laying it over another tag
    let tag = tag0,
      tw = 0,
      bx = 0,
      by = 0,
      far = false,
      placedTag = false;
    // whole on screen first (the full tag, then the bare glyph), and only then partly off the edge
    // ... and never across a zone's territory the bird is not working in (a tag over the keep-out reads as the
    // bird being inside it)
    const zd = terrs.filter((T) => !(A.file && isUnder(A.file.node, T.n)));
    const offZones = (x0: number, y0: number, x1: number, y1: number) => zd.every((T) => T.polys.every((poly) => rectPolyDist(x0, y0, x1, y1, poly) > 0));
    // the shortest tag: a top bird's bare glyph; a helper keeps its short name (a bare "○1" beside a folder's
    // change count would read as a count)
    const bare = A.ag.parent ? `${A.ag.glyph} ${clip(A.ag.short || A.ag.name, 14)}` : A.ag.glyph;
    const tries: Array<[string, boolean, boolean]> =
      tag0 === bare
        ? [[tag0, true, true], [tag0, true, false], [tag0, false, false]]
        : [[tag0, true, true], [bare, true, true], [tag0, true, false], [bare, true, false], [tag0, false, false]];
    for (const [t0, strict, clean] of tries) {
      // a helper's tag never lies across a territory it is not working in (it is dropped instead: the bird index
      // names the helper)
      if (A.ag.parent && !clean) continue;
      const w = g.measureText(t0).width + 12;
      const above: [number, number] = [-w / 2, -birdPx * 0.95 - th - 3],
        below: [number, number] = [-w / 2, 6],
        right: [number, number] = [birdPx * 0.65 + 6, -birdPx * 0.45 - th / 2],
        left: [number, number] = [-birdPx * 0.65 - 6 - w, -birdPx * 0.45 - th / 2];
      const order: Array<[number, number]> = pose.fly
        ? [below, above, right, left, [-w / 2, 28], [-w / 2, -birdPx * 0.95 - th - 26]]
        : [above, right, left, below, [-w / 2, -birdPx * 0.95 - th - 26], [-w / 2, 28]];
      const wholeT = (x0: number, y0: number) => x0 >= 2 && x0 + w <= v.W - 2 && y0 >= 2 && y0 + th <= v.H - 2;
      for (const [ax, ay] of order) {
        const x0 = sx + ax,
          y0 = sy + ay;
        if (fits(x0, y0, x0 + w, y0 + th) && (!strict || wholeT(x0, y0)) && (!clean || offZones(x0, y0, x0 + w, y0 + th))) {
          bx = x0;
          by = y0;
          far = Math.abs(ay) > birdPx * 0.95 + th + 12 || Math.abs(ax) > birdPx;
          placedTag = true;
          break;
        }
      }
      if (placedTag) {
        tag = t0;
        tw = w;
        break;
      }
    }
    if (!placedTag && A.ag.parent) {
      // a helper with nowhere free keeps no tag (the bird index names it): never a tag laid over another one
      g.globalAlpha = 1;
      v.birdHits.push({ r: body, A });
      continue;
    }
    if (!placedTag) {
      // nowhere free: the bare glyph just above the bird, kept inside the canvas
      tag = bare;
      tw = g.measureText(tag).width + 12;
      bx = sx - tw / 2;
      by = Math.max(2, Math.min(v.H - th - 2, sy - birdPx * 0.95 - th - 3));
    }
    bx = Math.max(2, Math.min(v.W - tw - 2, bx)); // never half off the canvas
    by = Math.max(2, Math.min(v.H - th - 2, by));
    if (far) {
      g.strokeStyle = A.ag.color;
      g.lineWidth = 1;
      g.beginPath();
      g.moveTo(sx, sy - birdPx * 0.4);
      g.lineTo(Math.max(bx + 4, Math.min(bx + tw - 4, sx)), by + th / 2 < sy ? by + th : by);
      g.stroke();
    }
    // in an only-here dusk a bird working outside the territory keeps its tag, but quieter
    const fileN = A.file ? A.file.node : null;
    if (hasOnly && fileN && !fileN.lit && onlyBinds(v, A)) g.globalAlpha = alpha * 0.55;
    quietPill(g, P, bx, by, tw, th);
    g.fillStyle = A.ag.color;
    g.textBaseline = "middle";
    g.fillText(tag, bx + 6, by + th / 2 + 0.5);
    g.globalAlpha = 1;
    reserve(bx, by, bx + tw, by + th);
    v.birdHits.push({ r: body, A });
    v.birdHits.push({ r: [bx, by, bx + tw, by + th], A });
  }
  // blocked bonks: a red ✕ and a ring where the bird hit the band
  for (const A of v.agents)
    for (const bk of A.blocked) {
      const age = v.playing ? t - bk.t : 2;
      if (age < 0.55 || age > 7) continue;
      const p = bonkPoint(v, bk);
      if (!p) continue;
      const [sx, sy] = toS(p[0], p[1]);
      const a = age < 4 ? 1 : Math.max(0, 1 - (age - 4) / 3);
      if (age < 1.7) {
        g.strokeStyle = P.red;
        g.lineWidth = 2;
        g.globalAlpha = a * (1 - (age - 0.55) / 1.15);
        g.beginPath();
        g.arc(sx, sy, 8 + (age - 0.55) * 26, 0, TAU);
        g.stroke();
      }
      g.globalAlpha = a;
      let cx = sx - 14,
        cy = sy - 16;
      const r = 6;
      for (const [ax, ay] of [
        [-14, -16],
        [14, -16],
        [-14, 16],
        [14, 16],
        [-26, 0],
        [26, 0],
      ]) {
        if (fits(sx + ax - r - 4, sy + ay - r - 4, sx + ax + r + 4, sy + ay + r + 4)) {
          cx = sx + ax;
          cy = sy + ay;
          break;
        }
      }
      g.fillStyle = P.bonkBg;
      g.beginPath();
      g.arc(cx, cy, r + 4, 0, TAU);
      g.fill();
      g.strokeStyle = P.red;
      g.lineWidth = 1.2;
      g.stroke();
      g.strokeStyle = P.bonkX;
      g.lineWidth = 2.4;
      g.lineCap = "round";
      g.beginPath();
      g.moveTo(cx - r * 0.6, cy - r * 0.6);
      g.lineTo(cx + r * 0.6, cy + r * 0.6);
      g.moveTo(cx + r * 0.6, cy - r * 0.6);
      g.lineTo(cx - r * 0.6, cy + r * 0.6);
      g.stroke();
      g.lineCap = "butt";
      g.globalAlpha = 1;
      reserve(cx - r - 4, cy - r - 4, cx + r + 4, cy + r + 4);
    }
  drawLabels(v, g, toS, fits, reserve, leafPx, inView, createdGhost, "rest");
  // off-screen birds: drawn last so nothing covers them; a click follows the bird
  for (const B of pointers) {
    const { A } = B,
      Pp = B.ptr;
    if (!Pp) continue;
    roundRect(g, Pp.bx, Pp.by, Pp.tw, Pp.th, 9);
    g.fillStyle = P.pill;
    g.fill();
    g.strokeStyle = A.ag.color;
    g.lineWidth = 1.2;
    g.stroke();
    g.fillStyle = A.ag.color;
    g.textBaseline = "middle";
    g.font = `600 10.5px ${P.font}`;
    g.fillText(Pp.t2, Pp.bx + 7, Pp.by + Pp.th / 2 + 0.5);
    v.birdHits.push({ r: [Pp.bx, Pp.by, Pp.bx + Pp.tw, Pp.by + Pp.th], A, pointer: true });
  }
  // an empty view (zoomed into air) says so instead of showing a blank screen
  if (!(v.visBranches && v.visBranches.length) && !v.termsInView && !birdS.some((b) => b && b.visible)) {
    const cx = (free.x0 + free.x1) / 2,
      cy = (free.y0 + free.y1) / 2;
    g.font = `500 13px ${P.font}`;
    g.textAlign = "center";
    g.textBaseline = "middle";
    g.fillStyle = P.muted;
    g.fillText(EMPTY_HINT, cx, cy);
    g.textAlign = "left";
    v.emptyHint = EMPTY_HINT;
  }
  v.anim =
    poses.some((p) => p && p.fly) ||
    !!(v.pulse && now - v.pulse.t0 < 2600) ||
    v.agents.some((A) =>
      A.blocked.some((bk) => {
        const a = t - bk.t;
        return a >= 0 && a < 7;
      })
    ) ||
    v.agents.some((A) => !!A.lastEdit && t - A.lastEdit.t < 1.6);
}

// ---------- LIT PATHS: changes, the wood that leads to them, and zones as territories ----------

export interface LitInfo {
  /** changed files under this folder (all birds) */
  n: number;
  /** per bird colour: its glyph and count (a helper counts under its parent's glyph) */
  segs: Array<{ col: string; glyph: string; n: number }>;
  /** the single colour when one bird owns every change below, else null */
  col: string | null;
  /** changed files directly in this folder (not in a sub-folder) */
  own: number;
  /** of n: changes by birds the only-here rules bind (this session and its helpers, unless a rule is repo-wide) */
  bound: number;
}

/** A folder's name as the map says it: a nested folder carries its top-level folder ("backend › config"), so a
 * name drawn beside another limb is never read as that limb's. */
export function folderPath(n: TNode): string {
  const nm = (x: TNode) => (x.kind === "root" || x.kind === "pile" ? x.label || x.name : x.disp || shortName(x.name));
  if (n.depth < 2) return nm(n);
  let top = n;
  while (top.parent && top.depth > 1) top = top.parent;
  const t = nm(top);
  let s = nm(n);
  // a name already disambiguated with its parent ("backend/providers") does not say "backend" twice
  if (s.startsWith(t + "/")) s = s.slice(t.length + 1);
  return `${t} › ${s}`;
}

/** How fresh each change is, 0.35 (made before the map armed) .. 1 (the latest edit): the newest changes glow
 * brightest, so "what just happened" reads without relying on colour. */
function freshness(v: TreeView): Map<number, number> {
  const ts = new Map<number, number>();
  for (const A of v.agents) for (const [id, t] of A.edits) ts.set(id, Math.max(ts.get(id) || 0, t || 0));
  const timed = [...ts.entries()].filter((e) => e[1] > 0).sort((a, b) => a[1] - b[1]);
  const out = new Map<number, number>();
  for (const [id, t] of ts) if (!(t > 0)) out.set(id, 0.35);
  timed.forEach(([id], i) => out.set(id, timed.length < 2 ? 1 : 0.45 + (0.55 * i) / (timed.length - 1)));
  return out;
}

/** Every folder on a path from the trunk to a changed file, with who changed what below it. */
function litMap(v: TreeView): Map<TNode, LitInfo> {
  const out = new Map<TNode, LitInfo>();
  const M = v.M;
  const glyphOf = (A: AgentState) => {
    const par = A.ag.parent ? v.agents.find((P) => P.ag.key === A.ag.parent) : null;
    return glyphShape(par ? par.ag.glyph : A.ag.glyph);
  };
  const seen = new Set<number>();
  // the primary's changes first, so a file two birds touched counts once, for this session
  const order = [...v.agents].sort((a, b) => Number(b.ag.primary) - Number(a.ag.primary));
  for (const A of order)
    for (const [id] of A.edits) {
      if (seen.has(id)) continue;
      const f = M.files[id];
      if (!f || !f.leaf) continue;
      seen.add(id);
      const col = A.ag.color,
        glyph = glyphOf(A),
        bindsA = onlyBinds(v, A);
      let n: TNode | null = f.node;
      while (n) {
        let e = out.get(n);
        if (!e) out.set(n, (e = { n: 0, segs: [], col, own: 0, bound: 0 }));
        e.n++;
        if (bindsA) e.bound++;
        if (n === f.node) e.own++;
        let sg = e.segs.find((q) => q.col === col);
        if (!sg) e.segs.push((sg = { col, glyph, n: 0 }));
        sg.n++;
        e.col = e.segs.length === 1 ? col : null;
        n = n.parent;
      }
    }
  return out;
}

/** The changed leaves: big, bright, in the bird's colour, with a glow; the one being edited now breathes. Drawn
 * after the nests (in world coordinates) so a nest bowl or a perched bird never hides a change. */
function drawEdits(v: TreeView, now: number, ox: number, oy: number, reserve?: Reserve) {
  const { g, M } = v;
  const P = v.P,
    z = v.cam.z,
    dpr = v.dpr,
    t = v.t,
    px = 1 / z,
    hasOnly = !!v.hasOnly;
  const vx0 = -ox / z,
    vy0 = -oy / z,
    vx1 = (v.W - ox) / z,
    vy1 = (v.H - oy) / z;
  const inView = (x0: number, y0: number, x1: number, y1: number) => x1 >= vx0 && x0 <= vx1 && y1 >= vy0 && y0 <= vy1;
  g.setTransform(dpr * z, 0, 0, dpr * z, dpr * ox, dpr * oy);
  const fresh = freshness(v);
  // zoomed out, a folder's changed leaves would pile on one another: nudge them apart (screen space, a few px)
  // so every change stays a separate, countable leaf next to where its file is
  const off = new Map<string, [number, number]>();
  {
    const pts: Array<{ k: string; x: number; y: number; x0: number; y0: number }> = [];
    const seen = new Set<number>();
    for (const A of v.agents)
      for (const [id] of A.edits) {
        const l = M.files[id] && M.files[id].leaf;
        if (!l || seen.has(id)) continue;
        seen.add(id);
        if (!inView(l.x - 40 * px, l.y - 40 * px, l.x + 40 * px, l.y + 40 * px)) continue;
        const x = l.x * z + ox,
          y = l.y * z + oy;
        pts.push({ k: String(id), x, y, x0: x, y0: y });
      }
    spreadApart(pts, 15); // min screen distance between two changed leaves' centres
    for (const q of pts) if (q.x !== q.x0 || q.y !== q.y0) off.set(q.k, [(q.x - q.x0) / z, (q.y - q.y0) / z]);
    if (reserve) for (const q of pts) reserve(q.x - 7, q.y - 7, q.x + 7, q.y + 7);
  }
  for (const A of v.agents) {
    const col = A.ag.color;
    const nowF = !A.done && (A.status === "editing" || A.status === "creating") && A.file ? A.file.id : -1;
    for (const [id] of A.edits) {
      const l0 = M.files[id] && M.files[id].leaf;
      if (!l0) continue;
      if (!inView(l0.x - 40 * px, l0.y - 40 * px, l0.x + 40 * px, l0.y + 40 * px)) continue;
      const o = off.get(String(id));
      g.save();
      if (o) g.translate(o[0], o[1]);
      const l = l0;
      const created = A.created.has(id) && A.lastEdit && A.lastEdit.f.id === id;
      const age = created ? t - A.lastEdit!.t : 99,
        grow = M.files[id].ghost || created ? (v.playing ? Math.min(1, Math.max(0.05, age / 1.2)) : 1) : 1;
      const own = l.term ? l.term.node : l.pile;
      // in an only-here dusk an earlier change outside the territory stays visible, but unlit (exempt, no glow)
      const outside = hasOnly && !!own && !own.lit && onlyBinds(v, A);
      const sc = Math.max(1, (10.5 * px) / (l.len * 0.5)) * grow;
      const rr = l.len * 0.5 * sc;
      if (id === nowF) {
        // being edited right now: a soft ring breathes out of the leaf
        const k = v.playing ? (now / 1600) % 1 : 0.35;
        g.strokeStyle = col;
        g.lineWidth = 2 * px;
        g.globalAlpha = 0.75 * (1 - k);
        g.beginPath();
        g.arc(l.x, l.y, rr + (4 + k * 16) * px, 0, TAU);
        g.stroke();
        g.globalAlpha = 1;
      }
      g.save();
      g.translate(l.x, l.y);
      g.scale(sc, sc);
      g.translate(-l.x, -l.y);
      if (!outside) {
        g.shadowColor = col;
        g.shadowBlur = (4 + 10 * (fresh.get(id) ?? 1)) * dpr;
      }
      g.fillStyle = col;
      g.globalAlpha = outside ? 0.75 : 1;
      g.beginPath();
      leafPath(g, l);
      g.fill();
      g.shadowBlur = 0;
      g.shadowColor = "transparent";
      g.globalAlpha = 1;
      g.strokeStyle = P.editStroke;
      g.lineWidth = (1.1 * px) / sc;
      if (outside) g.setLineDash([2 * px / sc, 2 * px / sc]);
      g.stroke();
      g.setLineDash([]);
      g.restore();
      g.restore();
    }
  }
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
}

/** Nudge points apart (in place) until no two are closer than D, moving each pair symmetrically: a few px for a
 * folder's changed leaves seen from far out, so each change stays a separate, countable mark beside its file. */
export function spreadApart(pts: Array<{ x: number; y: number }>, D: number, iters = 12): void {
  if (pts.length < 2 || pts.length >= 400) return;
  for (let it = 0; it < iters; it++) {
    let moved = false;
    for (let i = 0; i < pts.length; i++)
      for (let j = i + 1; j < pts.length; j++) {
        const a = pts[i],
          b = pts[j];
        let dx = b.x - a.x,
          dy = b.y - a.y;
        const d = Math.hypot(dx, dy);
        if (d >= D) continue;
        if (d < 0.01) {
          dx = Math.cos(i + j);
          dy = Math.sin(i + j);
        } else {
          dx /= d;
          dy /= d;
        }
        const push = (D - d) / 2 + 0.01;
        a.x -= dx * push;
        a.y -= dy * push;
        b.x += dx * push;
        b.y += dy * push;
        moved = true;
      }
    if (!moved) break;
  }
}

/** A colour (#rgb, #rrggbb, rgb(), hsl()) at alpha a, for gradients. */
function withAlpha(c: string, a: number): string {
  const m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(c.trim());
  if (m) {
    let h = m[1];
    if (h.length === 3) h = h.replace(/./g, (x) => x + x);
    const x = parseInt(h, 16);
    return `rgba(${x >> 16},${(x >> 8) & 255},${x & 255},${a})`;
  }
  if (/^(rgb|hsl)\(/i.test(c)) return c.replace(/^(rgb|hsl)\((.*)\)$/i, (_q, f, body) => `${f}a(${body.replace(/\s*\/.*$/, "")},${a})`);
  return c;
}

function branchVisible(b: Branch): boolean {
  const so = b.subOf;
  if (so && (so.vHidden || so.vColl)) return false;
  return !(b.node && b.node.vHidden);
}

/** Each branch's parent branch (the one that ends where it starts): the wood from the trunk to any branch,
 * union connectors included. Matched by end point, computed once per model. */
const BPAR = new WeakMap<Model, Map<Branch, Branch | null>>();
function branchParents(M: Model): Map<Branch, Branch | null> {
  let m = BPAR.get(M);
  if (m) return m;
  m = new Map();
  const ends = new Map<string, Branch[]>();
  const key = (x: number, y: number) => Math.round(x) + "," + Math.round(y);
  for (const b of M.branches) {
    const k = key(b.x1, b.y1);
    let a = ends.get(k);
    if (!a) ends.set(k, (a = []));
    a.push(b);
  }
  for (const b of M.branches) {
    let best: Branch | null = null,
      bd = 0.75;
    const ix = Math.round(b.x0),
      iy = Math.round(b.y0);
    for (let dx = -1; dx <= 1; dx++)
      for (let dy = -1; dy <= 1; dy++)
        for (const q of ends.get(ix + dx + "," + (iy + dy)) || []) {
          if (q === b || q.region !== b.region) continue;
          const d = Math.hypot(q.x1 - b.x0, q.y1 - b.y0);
          if (d < bd) {
            bd = d;
            best = q;
          }
        }
    m.set(b, best);
  }
  BPAR.set(M, m);
  return m;
}

/** The lit wood: every branch from the trunk to a changed file is drawn in a bright bark, with a glowing core in
 * the colour of the bird that changed things below it (neutral where two birds share it). */
function drawVeins(v: TreeView, px: number, inView: (x0: number, y0: number, x1: number, y1: number) => boolean) {
  const { g, M } = v;
  const P = v.P,
    z = v.cam.z,
    dpr = v.dpr;
  const par = branchParents(M);
  const cols = new Map<Branch, Set<string>>();
  const tops = new Map<Branch, Set<string>>();
  const seen = new Set<number>();
  for (const A of v.agents)
    for (const [id] of A.edits) {
      const f = M.files[id];
      const l = f && f.leaf;
      if (!l || seen.has(id * 64 + (A.ag.id % 64))) continue;
      seen.add(id * 64 + (A.ag.id % 64));
      let b: Branch | null = (l.term && (l.term as TTerm & { branch?: Branch }).branch) || (f.node && f.node.branch) || null;
      let guard = 0;
      while (b && guard++ < 200) {
        let c = cols.get(b);
        if (!c) cols.set(b, (c = new Set()));
        if (c.has(A.ag.color) && guard > 1) break;
        c.add(A.ag.color);
        const pb = par.get(b) || null;
        if (!pb) {
          let tc = tops.get(b);
          if (!tc) tops.set(b, (tc = new Set()));
          tc.add(A.ag.color);
        }
        b = pb;
      }
    }
  if (!cols.size) return;
  const colOf = (c: Set<string>) => (c.size === 1 ? [...c][0] : P.text);
  const vis: Array<[Branch, string]> = [];
  for (const [b, c] of cols) if (branchVisible(b) && inView(b.bx0, b.by0, b.bx1, b.by1)) vis.push([b, colOf(c)]);
  // in an only-here dusk the wood to an earlier change outside the territory is drawn, but unlit: only the wood
  // that leads to a change INSIDE the territory stays lit (a shared limb is lit, its side branch out is not)
  const inside = new Set<Branch>();
  if (v.hasOnly)
    for (const A of v.agents)
      for (const [id] of A.edits) {
        const f = M.files[id];
        const l = f && f.leaf;
        const own = l ? (l.term ? l.term.node : l.pile) : null;
        if (!l || !own || !own.lit) continue;
        let b: Branch | null = (l.term && (l.term as TTerm & { branch?: Branch }).branch) || (f.node && f.node.branch) || null;
        let guard = 0;
        while (b && !inside.has(b) && guard++ < 200) {
          inside.add(b);
          b = par.get(b) || null;
        }
      }
  const outside = (b: Branch) => !!v.hasOnly && !inside.has(b);
  g.lineCap = "round";
  g.lineJoin = "round";
  // 1. bright bark over the path
  g.fillStyle = P.litBark;
  g.strokeStyle = P.litBark;
  for (const [b] of vis) {
    g.globalAlpha = outside(b) ? 0.25 : 0.85;
    if (b.wb * z < 1.3) {
      g.lineWidth = 2 * px;
      const c = b.c;
      g.beginPath();
      g.moveTo(c[0], c[1]);
      g.bezierCurveTo(c[2], c[3], c[4], c[5], c[6], c[7]);
      g.stroke();
    } else {
      const p = b.poly;
      g.beginPath();
      g.moveTo(p[0], p[1]);
      for (let i = 2; i < p.length; i += 2) g.lineTo(p[i], p[i + 1]);
      g.closePath();
      g.fill();
    }
  }
  g.globalAlpha = 1;
  // 2. the glowing core; the trunk carries a core from the ground to every lit top branch
  const core = (b: Branch) => Math.max(2, Math.min(4.5, Math.min(b.wb, b.we) * z * 0.34)) * px;
  g.shadowBlur = 7 * dpr;
  for (const [b, c] of tops) {
    const col = colOf(c);
    // (in an only-here dusk the trunk's core to a change outside the territory recedes like the rest of it)
    const out = outside(b);
    g.globalAlpha = out ? 0.3 : 1;
    g.shadowBlur = out ? 0 : 7 * dpr;
    g.strokeStyle = col;
    g.shadowColor = col;
    g.lineWidth = 3 * px;
    g.beginPath();
    g.moveTo(0, M.groundY);
    g.lineTo(b.c[0], b.c[1]);
    g.stroke();
  }
  for (const [b, col] of vis) {
    const out = outside(b);
    g.globalAlpha = out ? 0.35 : 1;
    g.shadowBlur = out ? 0 : 7 * dpr;
    g.strokeStyle = col;
    g.shadowColor = col;
    g.lineWidth = core(b);
    const c = b.c;
    g.beginPath();
    g.moveTo(c[0], c[1]);
    g.bezierCurveTo(c[2], c[3], c[4], c[5], c[6], c[7]);
    g.stroke();
  }
  g.globalAlpha = 1;
  g.shadowBlur = 0;
  g.shadowColor = "transparent";
  g.lineCap = "butt";
}

interface Hull {
  terms: TTerm[];
  /** branches by stroke width (world units, rounded) */
  wood: Map<number, Branch[]>;
}
const HULLS = new WeakMap<Model, Map<TNode, Hull>>();
function hullOf(M: Model, n: TNode): Hull {
  let m = HULLS.get(M);
  if (!m) HULLS.set(M, (m = new Map()));
  let h = m.get(n);
  if (h) return h;
  h = { terms: [], wood: new Map() };
  for (const t of M.terms) if (isUnder(t.node, n)) h.terms.push(t);
  for (const b of M.branches) {
    const o = b.node || b.owner;
    if (!o || !isUnder(o, n)) continue;
    const w = Math.max(1, Math.round(Math.max(b.wb, b.we)));
    let a = h.wood.get(w);
    if (!a) h.wood.set(w, (a = []));
    a.push(b);
  }
  m.set(n, h);
  return h;
}

/** Branches lit by a change that is NOT under any of these zones (the wood from the trunk to that change). */
export function foreignLit(v: Pick<TreeView, "M" | "agents">, list: TZone[]): Branch[] {
  const M = v.M;
  const par = branchParents(M);
  const out = new Set<Branch>();
  for (const A of v.agents)
    for (const [id] of A.edits) {
      const f = M.files[id];
      const l = f && f.leaf;
      if (!l || list.some((zz) => zz.node && isUnder(f.node, zz.node))) continue;
      let b: Branch | null = (l.term && (l.term as TTerm & { branch?: Branch }).branch) || (f.node && f.node.branch) || null;
      let guard = 0;
      while (b && !out.has(b) && guard++ < 200) {
        const o = b.node || b.owner;
        // the wood of the zone's own folders stays inside (a change below it lights it legitimately)
        if (!(o && list.some((zz) => zz.node && isUnder(o, zz.node)))) out.add(b);
        b = par.get(b) || null;
      }
    }
  return [...out];
}

/** The convex hull (monotone chain) of flat [x, y, x, y, …] points, as a flat polygon. */
export function convexHull(p: number[]): number[] {
  const n = p.length >> 1;
  if (n < 3) return p.slice();
  const idx = Array.from({ length: n }, (_, i) => i).sort((a, b) => p[2 * a] - p[2 * b] || p[2 * a + 1] - p[2 * b + 1]);
  const cross = (o: number, a: number, b: number) =>
    (p[2 * a] - p[2 * o]) * (p[2 * b + 1] - p[2 * o + 1]) - (p[2 * a + 1] - p[2 * o + 1]) * (p[2 * b] - p[2 * o]);
  const lo: number[] = [],
    up: number[] = [];
  for (const i of idx) {
    while (lo.length >= 2 && cross(lo[lo.length - 2], lo[lo.length - 1], i) <= 0) lo.pop();
    lo.push(i);
  }
  for (let k = idx.length - 1; k >= 0; k--) {
    const i = idx[k];
    while (up.length >= 2 && cross(up[up.length - 2], up[up.length - 1], i) <= 0) up.pop();
    up.push(i);
  }
  lo.pop();
  up.pop();
  const out: number[] = [];
  for (const i of lo.concat(up)) out.push(p[2 * i], p[2 * i + 1]);
  return out;
}

/** Push `k` points of a circle (circumscribed: the polygon never cuts inside the disc). */
function ringPts(out: number[], x: number, y: number, r: number, k: number) {
  const R = r / Math.cos(Math.PI / k);
  for (let i = 0; i < k; i++) {
    const a = (i / k) * TAU;
    out.push(x + Math.cos(a) * R, y + Math.sin(a) * R);
  }
}

interface Lobes {
  /** convex lobes (world, unpadded): each folder branch with the clump at its tip */
  lobes: number[][];
  padded: Map<number, number[][]>;
}
const LOBES = new WeakMap<Model, Map<TNode, Lobes>>();
/** A zone's territory as world polygons: one convex lobe per branch of the folder (its wood from the fork it leaves
 * to the clump it carries), so the area hugs the whole branch and every clump, never a disc around one clump;
 * grown by `pad` (a Minkowski sum with a disc). */
export function zoneLobes(M: Model, n: TNode, pad: number): number[][] {
  let m = LOBES.get(M);
  if (!m) LOBES.set(M, (m = new Map()));
  let L = m.get(n);
  if (!L) {
    const h = hullOf(M, n);
    const byBranch = new Map<Branch, TTerm[]>();
    const loose: TTerm[] = [];
    const inWood = new Set<Branch>();
    for (const arr of h.wood.values()) for (const b of arr) inWood.add(b);
    for (const t of h.terms) {
      if (!t.leaves.length) continue;
      const b = (t.branch as Branch | undefined) || null;
      if (b && inWood.has(b)) {
        let a = byBranch.get(b);
        if (!a) byBranch.set(b, (a = []));
        a.push(t);
      } else loose.push(t);
    }
    const lobes: number[][] = [];
    for (const b of inWood) {
      const pts: number[] = [];
      const k = b.pts.length;
      for (let i = 0; i < k; i++) ringPts(pts, b.pts[i][0], b.pts[i][1], Math.max(0.5, taperHW(b.wb, b.we, i / Math.max(1, k - 1))), 8);
      for (const t of byBranch.get(b) || []) ringPts(pts, t.cx, t.cy, t.rad, 20);
      if (pts.length >= 6) lobes.push(convexHull(pts));
    }
    for (const t of loose) {
      const pts: number[] = [];
      ringPts(pts, t.cx, t.cy, t.rad, 20);
      lobes.push(convexHull(pts));
    }
    L = { lobes, padded: new Map() };
    m.set(n, L);
  }
  const key = Math.round(pad * 100) / 100;
  let out = L.padded.get(key);
  if (!out) {
    out = L.lobes.map((poly) => {
      const pts: number[] = [];
      for (let i = 0; i < poly.length; i += 2) ringPts(pts, poly[i], poly[i + 1], key, 12);
      return convexHull(pts);
    });
    if (L.padded.size > 6) L.padded.clear();
    L.padded.set(key, out);
  }
  return out;
}

/** Distance from a rect to a convex polygon (0 when they touch or overlap). */
export function rectPolyDist(x0: number, y0: number, x1: number, y1: number, poly: number[]): number {
  const n = poly.length >> 1;
  if (!n) return Infinity;
  // a polygon vertex inside the rect, or the rect's centre inside the polygon: they overlap
  for (let i = 0; i < n; i++) {
    const x = poly[2 * i],
      y = poly[2 * i + 1];
    if (x >= x0 && x <= x1 && y >= y0 && y <= y1) return 0;
  }
  let sgn = 0,
    inside = true;
  const cx = (x0 + x1) / 2,
    cy = (y0 + y1) / 2;
  for (let i = 0; i < n && inside; i++) {
    const ax = poly[2 * i],
      ay = poly[2 * i + 1],
      bx = poly[(2 * i + 2) % (2 * n)],
      by = poly[(2 * i + 3) % (2 * n)];
    const c = Math.sign((bx - ax) * (cy - ay) - (by - ay) * (cx - ax));
    if (c && sgn && c !== sgn) inside = false;
    if (c) sgn = c;
  }
  if (inside) return 0;
  // otherwise the nearest approach of the polygon's edges to the rect (an edge crossing the rect counts as 0)
  const segRect = (ax: number, ay: number, bx: number, by: number) => {
    let best = Infinity;
    const steps = 6;
    for (let s = 0; s <= steps; s++) {
      const x = ax + ((bx - ax) * s) / steps,
        y = ay + ((by - ay) * s) / steps;
      const dx = Math.max(x0 - x, 0, x - x1),
        dy = Math.max(y0 - y, 0, y - y1);
      best = Math.min(best, Math.hypot(dx, dy));
    }
    return best;
  };
  let d = Infinity;
  for (let i = 0; i < n; i++) d = Math.min(d, segRect(poly[2 * i], poly[2 * i + 1], poly[(2 * i + 2) % (2 * n)], poly[(2 * i + 3) % (2 * n)]));
  return d;
}

/** The territory margin around a zone's wood and clumps (world units) at zoom z: a thin world margin plus a fixed
 * on-screen one, so a small folder seen from far out is still an AREA (never a dot). */
export const terrPad = (z: number) => S0 * 0.22 + 16 / z;

const TERR = new WeakMap<object, HTMLCanvasElement>();
/** Zones as territories: a filled region over the folder's whole branch and every clump on it (keep out: red and
 * hatched; only here: green) with a solid border. It never covers what is not the zone's: another folder's leaves
 * are cut out of it (with the border running round the cut), and so are birds and lit wood that only pass by.
 * Drawn into an offscreen layer; the territory's screen polygons are kept for the label placers. */
function drawTerritories(v: TreeView, ox: number, oy: number) {
  v.terrPolys = [];
  const zs = v.zones.filter((zz) => !zz.waived && zz.node && zz.node.kind !== "pile" && zz.node.depth >= 1);
  if (!zs.length) return;
  const { g, M } = v;
  const P = v.P,
    z = v.cam.z,
    dpr = v.dpr;
  let c = TERR.get(v);
  if (!c) {
    c = document.createElement("canvas");
    TERR.set(v, c);
  }
  const cw = Math.max(1, Math.round(v.W * dpr)),
    ch = Math.max(1, Math.round(v.H * dpr));
  if (c.width !== cw || c.height !== ch) {
    c.width = cw;
    c.height = ch;
  }
  const t = c.getContext("2d");
  if (!t) return;
  const vx0 = -ox / z,
    vy0 = -oy / z,
    vx1 = (v.W - ox) / z,
    vy1 = (v.H - oy) / z;
  const pad = terrPad(z);
  for (const type of ["only", "keep"] as const) {
    const list = zs.filter((zz) => zz.type === type);
    if (!list.length) continue;
    const col = type === "keep" ? P.red : P.green;
    const ow = (type === "keep" ? 3 : 2.5) / z; // the border, on screen px
    const polys0: number[][] = [],
      polysB: number[][] = [];
    let bx0 = Infinity,
      by0 = Infinity,
      bx1 = -Infinity,
      by1 = -Infinity;
    for (const zz of list) {
      const own = zoneLobes(M, zz.node!, pad);
      const scr: number[][] = [];
      for (const poly of own) {
        let px0 = Infinity,
          py0 = Infinity,
          px1 = -Infinity,
          py1 = -Infinity;
        for (let i = 0; i < poly.length; i += 2) {
          px0 = Math.min(px0, poly[i]);
          px1 = Math.max(px1, poly[i]);
          py0 = Math.min(py0, poly[i + 1]);
          py1 = Math.max(py1, poly[i + 1]);
        }
        if (px1 < vx0 - ow || px0 > vx1 + ow || py1 < vy0 - ow || py0 > vy1 + ow) continue;
        bx0 = Math.min(bx0, px0);
        by0 = Math.min(by0, py0);
        bx1 = Math.max(bx1, px1);
        by1 = Math.max(by1, py1);
        polys0.push(poly);
        const sp: number[] = [];
        for (let i = 0; i < poly.length; i += 2) sp.push(poly[i] * z + ox, poly[i + 1] * z + oy);
        scr.push(sp);
      }
      for (const poly of zoneLobes(M, zz.node!, pad + ow)) polysB.push(poly);
      if (scr.length) v.terrPolys.push({ n: zz.node!, keep: type === "keep", polys: scr });
    }
    if (!polys0.length) continue;
    const path = (polys: number[][]) => {
      t.beginPath();
      for (const poly of polys) {
        t.moveTo(poly[0], poly[1]);
        for (let i = 2; i < poly.length; i += 2) t.lineTo(poly[i], poly[i + 1]);
        t.closePath();
      }
    };
    // what is NOT the zone's, inside its area: another folder's leaves (with a hair of air round them) — but a
    // neighbour's cut-out never bites into the zone's OWN clumps (seen from far out, clumps overlap: the zone's
    // leaves keep their territory and the area stays one piece instead of shreds)
    const gap = 3 / z;
    const ownT: TTerm[] = [];
    for (const zz of list) for (const tt of hullOf(M, zz.node!).terms) if (tt.leaves.length) ownT.push(tt);
    const holes: Array<[number, number, number]> = [];
    for (const tt of M.terms) {
      if (!tt.leaves.length || tt.node.vHidden) continue;
      if (list.some((zz) => isUnder(tt.node, zz.node!))) continue;
      let r = tt.rad + (tt.node.fAnc ? tt.s * 0.5 : 0) + gap;
      if (tt.cx + r < bx0 || tt.cx - r > bx1 || tt.cy + r < by0 || tt.cy - r > by1) continue;
      for (const o of ownT) r = Math.min(r, Math.max(tt.rad * 0.45, Math.hypot(tt.cx - o.cx, tt.cy - o.cy) - o.rad));
      holes.push([tt.cx, tt.cy, r]);
    }
    // a bird working OUTSIDE the territory is never drawn fenced in
    const bpx = Math.max(26, Math.min(48, 16 + S0 * z * 1.3));
    const birds: Array<[number, number, number]> = [];
    for (const A of v.agents) {
      const at = birdWorldAt(v, A, v.t);
      if (!at) continue;
      if (list.some((zz) => (A.file ? isUnder(A.file.node, zz.node!) : false))) continue;
      birds.push([at[0], at[1] - (bpx * 0.45) / z, (bpx * 0.75) / z]);
    }
    // wood lit by a change OUTSIDE the territory only passes by: its path is cut out too
    const lit = foreignLit(v, list).filter((b) => !(b.bx1 < bx0 || b.bx0 > bx1 || b.by1 < by0 || b.by0 > by1));
    const woodPath = (b: Branch) => {
      const q = b.c;
      t.beginPath();
      t.moveTo(q[0], q[1]);
      t.bezierCurveTo(q[2], q[3], q[4], q[5], q[6], q[7]);
    };
    const litW = (b: Branch) => b.wb + 7 / z; // the lit vein and a hair of air each side
    t.setTransform(1, 0, 0, 1, 0, 0);
    t.globalCompositeOperation = "source-over";
    t.globalAlpha = 1;
    t.clearRect(0, 0, cw, ch);
    t.setTransform(dpr * z, 0, 0, dpr * z, dpr * ox, dpr * oy);
    t.lineCap = "round";
    t.lineJoin = "round";
    t.fillStyle = col;
    t.strokeStyle = col;
    // 1. the border: the area grown by the border width, minus the area
    path(polysB);
    t.fill("nonzero");
    t.globalCompositeOperation = "destination-out";
    path(polys0);
    t.fill("nonzero");
    t.globalCompositeOperation = "source-over";
    // 2. inside the area: the tint, the keep-out hatching, and the border round every cut-out
    t.save();
    path(polys0);
    t.clip("nonzero");
    t.globalAlpha = type === "keep" ? (P.light ? 0.22 : 0.28) : P.light ? 0.16 : 0.14;
    t.fillRect(bx0 - ow, by0 - ow, bx1 - bx0 + 2 * ow, by1 - by0 + 2 * ow);
    if (type === "keep") {
      // hatched across the whole area (a shape cue that survives colour blindness), in screen space
      t.setTransform(1, 0, 0, 1, 0, 0);
      t.globalAlpha = P.light ? 0.42 : 0.5;
      t.lineWidth = 1.4 * dpr;
      const gp = 8 * dpr;
      t.beginPath();
      for (let d = -ch; d < cw; d += gp) {
        t.moveTo(d, ch);
        t.lineTo(d + ch, 0);
      }
      t.stroke();
      t.setTransform(dpr * z, 0, 0, dpr * z, dpr * ox, dpr * oy);
    }
    t.globalAlpha = 1;
    t.lineWidth = ow;
    t.beginPath();
    for (const [x, y, r] of holes) {
      t.moveTo(x + r + ow / 2, y);
      t.arc(x, y, r + ow / 2, 0, TAU);
    }
    t.stroke();
    t.restore();
    // 3. the cut-outs themselves (they also break the outer border where a neighbour's leaves cross it)
    // (destination-out only reads alpha: the zone colour serves)
    t.globalCompositeOperation = "destination-out";
    t.beginPath();
    for (const [x, y, r] of holes) {
      t.moveTo(x + r, y);
      t.arc(x, y, r, 0, TAU);
    }
    for (const [x, y, r] of birds) {
      t.moveTo(x + r, y);
      t.arc(x, y, r, 0, TAU);
    }
    t.fill();
    for (const b of lit) {
      t.lineWidth = litW(b);
      woodPath(b);
      t.stroke();
    }
    t.globalCompositeOperation = "source-over";
    g.setTransform(1, 0, 0, 1, 0, 0);
    g.globalAlpha = 1;
    g.drawImage(c, 0, 0);
  }
}

// Screen-space foliage: every clump's disc (and the dashed ring of a selected / hovered folder) in a coarse grid.
// hit(rect, node, subtree) says whether the rect would sit on foliage that is NOT that node's own: its own clump
// (a leaf folder's leaves, a fork's own files), or with subtree its whole subtree (a collapsed clump's label).
export function buildFoliageField(v: TreeView, toS: (x: number, y: number) => [number, number], hoverNode: TNode | null) {
  const M = v.M,
    z = v.cam.z,
    W = v.W,
    H = v.H,
    CELL = 48;
  interface Disc {
    x: number;
    y: number;
    r: number;
    n: TNode;
    ring?: boolean;
    /** a clump that glows (it holds a change): as loud as a nest or a sign to a reader */
    hot?: boolean;
  }
  const cells = new Map<number, Disc[]>(),
    key = (i: number, j: number) => (i + 64) * 4096 + (j + 64);
  const discs: Disc[] = [];
  const add = (d: Disc) => {
    discs.push(d);
    for (let i = Math.floor((d.x - d.r) / CELL); i <= Math.floor((d.x + d.r) / CELL); i++)
      for (let j = Math.floor((d.y - d.r) / CELL); j <= Math.floor((d.y + d.r) / CELL); j++) {
        const k = key(i, j);
        let a = cells.get(k);
        if (!a) cells.set(k, (a = []));
        a.push(d);
      }
  };
  for (const t of M.terms) {
    if (!t.leaves.length) continue;
    const [x, y] = toS(t.cx, t.cy),
      r = (t.rad + (t.node.fAnc ? t.s * 0.5 : 0)) * z + 1;
    if (r < 1.5 || x + r < -200 || x - r > W + 200 || y + r < -100 || y - r > H + 100) continue; // names may hang just off-screen
    const li = v.litInfo && v.litInfo.get(t.node);
    add({ x, y, r, n: t.node, hot: !!(li && li.own > 0) });
  }
  const rings: TNode[] = [];
  if (hoverNode && hoverNode.cnt && v.tool !== "explore") rings.push(hoverNode);
  if (v.sel && v.sel.node && v.sel.node.cnt && v.sel.node.depth >= 1 && v.infoNode === v.sel.node) rings.push(v.sel.node);
  for (const n of rings) {
    const [x, y] = toS(n.cx, n.cy);
    add({ x, y, r: (n.rad + S0 * 0.6) * z + 2, n, ring: true });
  }
  const seen: Disc[] = [];
  const topOf = (n: TNode): TNode => {
    let q = n;
    while (q.parent && q.depth > 1) q = q.parent;
    return q;
  };
  const tops = new Map<Disc, TNode>();
  const extras: Array<{ n: TNode; poly: number[] | null; x0: number; y0: number; x1: number; y1: number; zone?: boolean }> = [];
  const api = {
    discs,
    /** Whether a name for `n` sitting at rect R reads as being on n's own limb: the foliage nearest to it (within
     * `range` px) belongs to n's top-level folder, never to a neighbour's. A name drawn beside another limb is read
     * as that limb's ("backend › config" under the "frontend" label), so every placer asks this first. */
    sideOk(x0: number, y0: number, x1: number, y1: number, n: TNode | null, range = 150): boolean {
      if (!n || n.depth < 1 || n.kind === "pile") return true;
      const T = topOf(n);
      let dOwn = Infinity,
        dFor = Infinity;
      for (let i = Math.floor((x0 - range) / CELL); i <= Math.floor((x1 + range) / CELL); i++)
        for (let j = Math.floor((y0 - range) / CELL); j <= Math.floor((y1 + range) / CELL); j++) {
          const a = cells.get(key(i, j));
          if (!a) continue;
          for (const d of a) {
            if (d.ring) continue;
            const dx = Math.max(x0 - d.x, 0, d.x - x1),
              dy = Math.max(y0 - d.y, 0, d.y - y1);
            const e = Math.max(0, Math.hypot(dx, dy) - d.r);
            if (e > range) continue;
            let t = tops.get(d);
            if (!t) tops.set(d, (t = topOf(d.n)));
            if (t === T) dOwn = Math.min(dOwn, e);
            else dFor = Math.min(dFor, e);
          }
        }
      return dFor === Infinity || dOwn <= dFor + 3;
    },
    /** A nest on screen (it belongs to its folder: a name beside it reads as that folder's). */
    addNest(x0: number, y0: number, x1: number, y1: number, n: TNode) {
      extras.push({ n, poly: null, x0, y0, x1, y1 });
    },
    /** A zone's sign on screen (it belongs to the zone's folder). */
    addTag(x0: number, y0: number, x1: number, y1: number, n: TNode) {
      extras.push({ n, poly: null, x0, y0, x1, y1, zone: true });
    },
    /** A zone's territory on screen: convex polygons (they belong to the zone's folder). */
    addTerr(n: TNode, polys: number[][]) {
      for (const poly of polys) {
        let x0 = Infinity,
          y0 = Infinity,
          x1 = -Infinity,
          y1 = -Infinity;
        for (let i = 0; i < poly.length; i += 2) {
          x0 = Math.min(x0, poly[i]);
          x1 = Math.max(x1, poly[i]);
          y0 = Math.min(y0, poly[i + 1]);
          y1 = Math.max(y1, poly[i + 1]);
        }
        extras.push({ n, poly, x0, y0, x1, y1, zone: true });
      }
    },
    /** How a name at rect R reads, the way a reader takes it in. LOUD things — a glowing (changed) clump, a nest,
     * a zone's sign or territory — claim a name that merely touches them, so they count by R's EDGE; the calm
     * canopy is one quiet mass, so it counts from the name's CENTRE (a long name may overhang a quiet neighbour).
     * own / loud: n's own things vs anyone else's loud things, by edge; ownC / calmC: n's own vs anyone else's
     * calm foliage, from the centre; by: whose the nearest foreign thing is. */
    near(x0: number, y0: number, x1: number, y1: number, n: TNode | null, anchors?: Array<[number, number, number]>, range = 220) {
      let own = Infinity,
        loud = Infinity,
        ownC = Infinity,
        calmC = Infinity,
        by: TNode | null = null;
      if (!n) return { own, loud, ownC, calmC, by };
      const cx = (x0 + x1) / 2,
        cy = (y0 + y1) / 2;
      const mine = (m: TNode) => isUnder(m, n);
      const edge = (x: number, y: number, r: number) => Math.max(0, Math.hypot(Math.max(x0 - x, 0, x - x1), Math.max(y0 - y, 0, y - y1)) - r);
      const cen = (x: number, y: number, r: number) => Math.max(0, Math.hypot(x - cx, y - cy) - r);
      let byD = Infinity;
      for (let i = Math.floor((x0 - range) / CELL); i <= Math.floor((x1 + range) / CELL); i++)
        for (let j = Math.floor((y0 - range) / CELL); j <= Math.floor((y1 + range) / CELL); j++) {
          const a = cells.get(key(i, j));
          if (!a) continue;
          for (const d of a) {
            if (d.ring) continue;
            const e = edge(d.x, d.y, d.r);
            if (e > range) continue;
            const c = cen(d.x, d.y, d.r);
            if (mine(d.n)) {
              own = Math.min(own, e);
              ownC = Math.min(ownC, c);
            } else if (d.hot) {
              loud = Math.min(loud, e);
              if (e < byD) (byD = e), (by = d.n);
            } else {
              calmC = Math.min(calmC, c);
              if (c < byD) (byD = c), (by = d.n);
            }
          }
        }
      for (const [x, y, r] of anchors || []) {
        own = Math.min(own, edge(x, y, r));
        ownC = Math.min(ownC, cen(x, y, r));
      }
      for (const X of extras) {
        if (X.x0 > x1 + range || X.x1 < x0 - range || X.y0 > y1 + range || X.y1 < y0 - range) continue;
        const e = X.poly ? rectPolyDist(x0, y0, x1, y1, X.poly) : Math.hypot(Math.max(X.x0 - x1, 0, x0 - X.x1), Math.max(X.y0 - y1, 0, y0 - X.y1));
        // a zone is n's own when n is in it or holds it (a sub-folder in its territory, "backend" round its keep-out)
        const isOwn = X.zone ? mine(X.n) || isUnder(n, X.n) : mine(X.n);
        if (isOwn) {
          own = Math.min(own, e);
          ownC = Math.min(ownC, X.poly ? rectPolyDist(cx, cy, cx, cy, X.poly) : Math.hypot(Math.max(X.x0 - cx, 0, cx - X.x1), Math.max(X.y0 - cy, 0, cy - X.y1)));
        } else {
          loud = Math.min(loud, e);
          if (e < byD) (byD = e), (by = X.n);
        }
      }
      return { own, loud, ownC, calmC, by };
    },
    /** A folder's name stays pinned to its own cluster: it touches no one else's loud thing (glowing clump, nest,
     * zone sign, territory) and none is nearer than its own things; nor does it sit nearer to a neighbour's quiet
     * foliage than to its own. A name that cannot sit like that is shortened or dropped, never parked beside a
     * neighbour (where it would name the neighbour). */
    belongs(x0: number, y0: number, x1: number, y1: number, n: TNode | null, anchors?: Array<[number, number, number]>, range = 220, clear = 4): boolean {
      if (!n || n.depth < 1 || n.kind === "pile") return true;
      const q = api.near(x0, y0, x1, y1, n, anchors, range);
      if (q.loud !== Infinity && !(q.loud >= clear && q.own <= q.loud)) return false;
      // a name touching its own cluster (within 10 px of its clump, nest, sign or stem) is read as that; otherwise
      // the quiet canopy nearest its centre must be its own
      return q.calmC === Infinity || q.own <= 10 || q.ownC <= q.calmC;
    },
    hit(x0: number, y0: number, x1: number, y1: number, own: TNode | null, subtree: boolean): boolean {
      seen.length = 0;
      for (let i = Math.floor(x0 / CELL); i <= Math.floor(x1 / CELL); i++)
        for (let j = Math.floor(y0 / CELL); j <= Math.floor(y1 / CELL); j++) {
          const a = cells.get(key(i, j));
          if (!a) continue;
          for (const d of a) {
            if (seen.includes(d)) continue;
            seen.push(d);
            const dx = Math.max(x0 - d.x, 0, d.x - x1),
              dy = Math.max(y0 - d.y, 0, d.y - y1);
            if (dx * dx + dy * dy >= (d.r - 1) * (d.r - 1)) continue;
            if (own && (d.ring ? isUnder(own, d.n) : d.n === own || (subtree && isUnder(d.n, own)))) continue;
            return true;
          }
        }
      return false;
    },
  };
  return api;
}

type Fits = (x0: number, y0: number, x1: number, y1: number) => boolean;
type Reserve = (x0: number, y0: number, x1: number, y1: number) => void;

// "repo › backend › web › core": the folder the view is inside, drawn at the top-left of the free canvas once
// you are zoomed past the whole tree; each segment is clickable
function drawBreadcrumb(v: TreeView, g: G2, free: TreeView["free"], fits: Fits, reserve: Reserve) {
  const M = v.M,
    P = v.P,
    cam = v.cam,
    z = cam.z;
  v.crumbAnc = null;
  v.crumbNode = null;
  if (!v.fitZv || z < v.fitZv * 1.45) return;
  // probe the followed bird when following, else the middle of what the user actually sees (the free canvas,
  // not the camera centre, which the HUD insets push sideways)
  let cx: number, cy: number;
  const fb = v.follow != null && v.birdWorld && v.birdWorld[v.follow];
  if (fb) {
    cx = fb[0];
    cy = fb[1];
  } else {
    const sx = (free.x0 + free.x1) / 2,
      sy = (free.y0 + free.y1) / 2;
    cx = (sx - v.W / 2) / z + cam.x;
    cy = (sy - v.H / 2) / z + cam.y;
  }
  const m = S0 * 2,
    minPx = 60;
  // the deepest folder whose canopy holds the view centre; among equals the tightest one (a wide sibling's box
  // can straddle a small neighbour)
  let best: TNode | null = null,
    bestArea = 1e18;
  for (const n of M.nodes) {
    if (n.kind === "pile" || n.depth < 1 || !n.cnt) continue;
    if (cx < n.bx0 - m || cx > n.bx1 + m || cy < n.by0 - m || cy > n.by1 + m) continue;
    const area = (n.bx1 - n.bx0 + 1) * (n.by1 - n.by0 + 1);
    if (!best || n.depth > best.depth || (n.depth === best.depth && area < bestArea)) {
      best = n;
      bestArea = area;
    }
  }
  if (!best) {
    // between clusters: the folder whose centre is nearest, if it is large on screen
    let bd = 1e18;
    for (const n of M.nodes) {
      if (n.kind === "pile" || n.depth < 1 || !n.cnt || n.quiet) continue;
      const d = Math.hypot(n.cx - cx, n.cy - cy);
      if (d < n.rad + S0 * 6 && d < bd) {
        bd = d;
        best = n;
      }
    }
    if (!best) return;
  }
  while (best.depth > 1 && best.rad * z < minPx && best.parent) best = best.parent; // a folder a few pixels wide is not "where you are"
  // a view that shows a good share of another folder at the same level is not "in" this one: say the level they
  // share (and nothing when that is the whole repo — the crumb would name a folder you are not only looking at)
  const vx0 = (free.x0 - v.W / 2) / z + cam.x,
    vy0 = (free.y0 - v.H / 2) / z + cam.y,
    vx1 = (free.x1 - v.W / 2) / z + cam.x,
    vy1 = (free.y1 - v.H / 2) / z + cam.y;
  const vArea = Math.max(1, (vx1 - vx0) * (vy1 - vy0));
  const share = (n: TNode) => {
    const w = Math.min(vx1, n.bx1) - Math.max(vx0, n.bx0),
      h = Math.min(vy1, n.by1) - Math.max(vy0, n.by0);
    return w > 0 && h > 0 ? (w * h) / vArea : 0;
  };
  const rival = (q: TNode) => M.nodes.some((o) => o !== q && o.depth === q.depth && o.kind === q.kind && o.kind !== "pile" && o.cnt && !isUnder(o, q) && !isUnder(q, o) && share(o) > 0.15);
  let at: TNode | null = best;
  while (at && at.depth >= 1 && rival(at)) at = at.parent;
  if (!at || at.depth < 1) return;
  best = at;
  const chain: TNode[] = [];
  for (let q: TNode | null = best; q && q.depth >= 1; q = q.parent) if (!q.quiet || q === best) chain.unshift(q);
  const repo = M.name.split("/").filter(Boolean).pop() || M.name; // "owner/repo" says the repo
  const segs: Array<{ txt: string; n: TNode }> = [{ txt: best.kind === "root" ? "tests" : repo, n: best.kind === "root" ? M.roots : M.crown }];
  for (const q of chain)
    segs.push({ txt: q.kind === "root" ? (q.label || q.name).replace(/^tests for /, "") : q.depth === 1 ? q.name : q.core || baseOf(q.name), n: q });
  g.font = `600 11.5px ${P.font}`;
  g.textBaseline = "middle";
  const sep = "  ›  ",
    sw = g.measureText(sep).width;
  const widths = segs.map((s) => g.measureText(s.txt).width);
  const tw = widths.reduce((a, b) => a + b, 0) + sw * (segs.length - 1) + 18,
    th = 22;
  const bx = free.x0 + 10,
    by = free.y0 + 8;
  if (bx + tw > free.x1 - 4 || !fits(bx, by, bx + tw, by + th)) return;
  // the folders the crumb names above the one you are in: their names are said up here, so the canopy does not
  // repeat them in the middle of a child's leaves
  v.crumbNode = best;
  v.crumbAnc = new Set();
  for (let q = best.parent; q && q.depth >= 1; q = q.parent) v.crumbAnc.add(q);
  roundRect(g, bx, by, tw, th, 8);
  g.fillStyle = P.pill;
  g.fill();
  g.strokeStyle = P.border;
  g.lineWidth = 1;
  g.stroke();
  let x = bx + 9;
  segs.forEach((s, i) => {
    const last = i === segs.length - 1;
    g.fillStyle = last ? P.text : P.muted;
    g.fillText(s.txt, x, by + th / 2 + 0.5);
    v.labelHits!.push({ r: [x - 3, by, x + widths[i] + 3, by + th], n: s.n, crumb: true });
    x += widths[i];
    if (!last) {
      g.fillStyle = P.sep;
      g.fillText(sep, x, by + th / 2 + 0.5);
      x += sw;
    }
  });
  reserve(bx, by, bx + tw, by + th);
}

/** The map's one label chrome: a soft panel-coloured lozenge with no coloured outline, so labels never compete
 * with the changed leaves (the loudest thing on the map); the counts inside carry the birds' colours. */
function quietPill(g: G2, P: Palette, x: number, y: number, w: number, h: number) {
  roundRect(g, x, y, w, h, h / 2);
  g.fillStyle = P.pill;
  g.fill();
  g.strokeStyle = P.pillEdge;
  g.lineWidth = 1;
  g.stroke();
}

function roundRect(g: G2, x: number, y: number, w: number, h: number, r: number) {
  g.beginPath();
  g.moveTo(x + r, y);
  g.lineTo(x + w - r, y);
  g.arcTo(x + w, y, x + w, y + r, r);
  g.lineTo(x + w, y + h - r);
  g.arcTo(x + w, y + h, x + w - r, y + h, r);
  g.lineTo(x + r, y + h);
  g.arcTo(x, y + h, x, y + h - r, r);
  g.lineTo(x, y + r);
  g.arcTo(x, y, x + r, y, r);
  g.closePath();
}

function drawBud(g: G2, P: Palette, cx: number, cy: number, len: number, col: string, px: number, ghost: boolean) {
  const r = Math.max(4 * px, len * 0.3);
  g.fillStyle = ghost ? P.budGhost : P.bud;
  g.beginPath();
  for (let k = 0; k < 5; k++) {
    const a = (k / 5) * TAU - Math.PI / 2;
    const x = cx + Math.cos(a) * r * 0.62,
      y = cy + Math.sin(a) * r * 0.62;
    g.moveTo(x + r * 0.48, y);
    g.arc(x, y, r * 0.48, 0, TAU);
  }
  g.fill();
  g.fillStyle = col;
  g.beginPath();
  g.arc(cx, cy, r * 0.36, 0, TAU);
  g.fill();
  if (ghost) {
    g.strokeStyle = col;
    g.setLineDash([2.5 * px, 2 * px]);
    g.lineWidth = 1.2 * px;
    g.beginPath();
    g.arc(cx, cy, r * 1.45, 0, TAU);
    g.stroke();
    g.setLineDash([]);
  }
}

/** The could-break layer's groups: the files importing a changed file (or the focused one), counted per folder at
 * a level the view can show (sub-folders of the top-level limbs at the whole-tree view, deeper as you zoom),
 * merged up until there are few enough to count at a glance. Each group's anchor is the top of the foliage that
 * holds its files. */
export function riskGroups(v: TreeView): RiskGroup[] {
  const M = v.M;
  if (!v.riskAll && v.riskFile == null) return [];
  const by = new Map<number, Set<number>>();
  const add = (fid: number) => {
    let b = v.blastCache.get(fid);
    if (!b) {
      b = blastOf(M, fid);
      v.blastCache.set(fid, b);
    }
    for (const id of b.h1) {
      let s = by.get(id);
      if (!s) by.set(id, (s = new Set()));
      s.add(fid);
    }
  };
  if (v.riskFile != null && M.files[v.riskFile]) add(v.riskFile);
  else for (const A of v.agents) for (const fid of A.edits.keys()) if (M.files[fid]) add(fid);
  const lp = v.cam.z * S0;
  const D = lp < 11 ? 2 : lp < 24 ? 3 : 99;
  const groups = new Map<TNode, RiskGroup>();
  for (const [id, files] of by) {
    const f = M.files[id];
    if (!f || !f.node || !f.leaf) continue;
    let n: TNode = f.node;
    if (n.kind !== "pile") while (n.parent && n.depth > D) n = n.parent;
    let e = groups.get(n);
    if (!e) groups.set(n, (e = { n, x: 0, y: 0, r: 0, ids: new Set(), files: new Set() }));
    e.ids.add(id);
    for (const x of files) e.files.add(x);
  }
  let list = [...groups.values()];
  // a folder that holds a counted sub-folder counts only its OTHER files ("backend · other"): a count never
  // contains another count, and a limb-wide total never swallows the folders that matter
  for (const e of list) if (list.some((h) => h !== e && h.n !== e.n && isUnder(h.n, e.n))) e.rest = true;
  // too many to count at a glance: the biggest keep their own count, the small ones become ONE "other" count
  // per top-level limb ("backend · other 6") — never folded into a limb-wide count that swallows the big ones
  const CAP = lp < 11 ? 8 : 12;
  if (list.length > CAP) {
    list.sort((a, b) => b.ids.size - a.ids.size);
    const keep = list.slice(0, CAP - 2),
      rest = list.slice(CAP - 2);
    const other = new Map<TNode, RiskGroup>();
    for (const e of rest) {
      let top: TNode = e.n;
      while (top.parent && top.depth > 1) top = top.parent;
      let o = other.get(top) || keep.find((k) => k.n === top && k.rest);
      if (!o) other.set(top, (o = { n: top, x: 0, y: 0, r: 0, ids: new Set(), files: new Set(), rest: true }));
      for (const id of e.ids) o.ids.add(id);
      for (const f of e.files) o.files.add(f);
    }
    list = [...keep, ...other.values()];
  }
  // anchor: the top middle of the foliage holding the group's files
  for (const e of list) {
    let x0 = 1e18,
      y0 = 1e18,
      x1 = -1e18;
    for (const id of e.ids) {
      const l = M.files[id].leaf!;
      const c = l.term ? [l.term.cx, l.term.cy, l.term.rad] : l.pile ? [l.pile.cx, M.groundY - (l.pile.h || 10) * 0.5, l.pile.pw || 20] : [l.x, l.y, S0];
      x0 = Math.min(x0, c[0] - c[2]);
      x1 = Math.max(x1, c[0] + c[2]);
      y0 = Math.min(y0, c[1] - c[2]);
    }
    e.x = (x0 + x1) / 2;
    e.y = y0;
    e.r = (x1 - x0) / 2;
    // a limb's "other" count sits where the limb forks, by its name (its files are scattered over the limb)
    if (e.rest && e.n.kind === "crown" && e.n.F) {
      e.x = e.n.F[0];
      e.y = e.n.F[1];
      e.r = S0;
    }
  }
  return list.sort((a, b) => b.ids.size - a.ids.size);
}

/** The could-break layer in world space: a soft gold wash over each clump of foliage holding files that import a
 * change — the clump, not a scatter of single leaves, its strength by how many of its files do. The focused file
 * gets a gold ring of its own. The counts are drawn in screen space. */
function drawRisk(v: TreeView, px: number) {
  const { g, M, P } = v;
  const groups = riskGroups(v);
  v.riskGroups = groups;
  if (!groups.length) return;
  const clumps = new Map<object, { x: number; y: number; r: number; n: number; of: number }>();
  for (const e of groups)
    for (const id of e.ids) {
      const l = M.files[id].leaf;
      if (!l) continue;
      const key: object = l.term || l.pile || l;
      let c = clumps.get(key);
      if (!c) {
        c = l.term
          ? { x: l.term.cx, y: l.term.cy, r: l.term.rad, n: 0, of: Math.max(1, l.term.files.length) }
          : l.pile
            ? { x: l.pile.cx, y: M.groundY - (l.pile.h || 10) * 0.5, r: l.pile.pw || 20, n: 0, of: Math.max(1, l.pile.files.length) }
            : { x: l.x, y: l.y, r: S0, n: 0, of: 1 };
        clumps.set(key, c);
      }
      c.n++;
    }
  for (const c of clumps.values()) {
    const k = Math.min(1, 0.35 + (0.65 * c.n) / c.of);
    const R = Math.max(c.r * 1.15, 7 * px);
    const gr = g.createRadialGradient(c.x, c.y, 0, c.x, c.y, R);
    gr.addColorStop(0, withAlpha(P.gold, (P.light ? 0.42 : 0.42) * k));
    gr.addColorStop(0.7, withAlpha(P.gold, (P.light ? 0.26 : 0.26) * k));
    gr.addColorStop(1, withAlpha(P.gold, 0));
    g.fillStyle = gr;
    g.beginPath();
    g.arc(c.x, c.y, R, 0, TAU);
    g.fill();
  }
  const f = v.riskFile != null ? M.files[v.riskFile] : null;
  const l = f && f.leaf;
  if (l) {
    g.strokeStyle = P.gold;
    g.lineWidth = 2.5 * px;
    g.beginPath();
    g.arc(l.x, l.y, Math.max(l.len * 0.9, 9 * px), 0, TAU);
    g.stroke();
  }
}

/** The could-break counts: one gold chip per group ("⚠ web 12") above the foliage it counts, clickable (it lists
 * the files). */
function drawRiskChips(v: TreeView, g: G2, toS: (x: number, y: number) => [number, number], fits: Fits, reserve: Reserve) {
  const { M, P } = v;
  const groups = v.riskGroups || [];
  if (!groups.length) return;
  const z = v.cam.z;
  const f = v.riskFile != null ? M.files[v.riskFile] : null;
  g.font = `650 11px ${P.font}`;
  g.textBaseline = "middle";
  for (const e of groups) {
    const [sx, sy] = toS(e.x, e.y);
    const hw = e.r * z;
    if (sx + hw < 0 || sx - hw > v.W || sy < -40 || sy > v.H + 40) continue;
    const c = e.ids.size;
    const n = e.n;
    const name =
      n.kind === "pile" ? n.label || n.name : n.kind === "root" ? (n.label || n.name).replace(/^tests for /, "tests/") : n.depth >= 1 ? n.disp || shortName(n.name) : "root files";
    const txt = `⚠ ${e.rest ? name + " · other" : name} ${c}`;
    const tw = g.measureText(txt).width + 14,
      th = 18;
    let bx = 0,
      by = 0,
      ok = false;
    for (const [ax, ay] of [
      [-tw / 2, -th - 3],
      [-tw / 2, -th - 20],
      [hw * 0.5 - tw / 2, -th - 3],
      [-hw * 0.5 - tw / 2, -th - 3],
      [-tw / 2, 6],
      [-tw / 2, 24],
    ]) {
      const x0 = sx + ax,
        y0 = sy + ay;
      if (x0 >= 2 && x0 + tw <= v.W - 2 && y0 >= 2 && y0 + th <= v.H - 2 && fits(x0, y0, x0 + tw, y0 + th)) {
        bx = x0;
        by = y0;
        ok = true;
        break;
      }
    }
    if (!ok) {
      // the nearest free spot around the anchor (never on a name); only as a last resort on top
      bx = Math.max(2, Math.min(v.W - tw - 2, sx - tw / 2));
      by = Math.max(2, Math.min(v.H - th - 2, sy - th - 3));
      search: for (let rr = 14; rr <= 120; rr += 14)
        for (let k = 0; k < 16; k++) {
          const a = (k / 16) * TAU - Math.PI / 2,
            x0 = sx + Math.cos(a) * rr - tw / 2,
            y0 = sy + Math.sin(a) * rr - th / 2;
          if (x0 >= 2 && x0 + tw <= v.W - 2 && y0 >= 2 && y0 + th <= v.H - 2 && fits(x0, y0, x0 + tw, y0 + th)) {
            bx = x0;
            by = y0;
            break search;
          }
        }
    }
    roundRect(g, bx, by, tw, th, 9);
    g.fillStyle = P.badgeBg;
    g.fill();
    g.strokeStyle = P.gold;
    g.lineWidth = 1.2;
    g.stroke();
    g.fillStyle = P.gold;
    g.fillText(txt, bx + 7, by + th / 2 + 0.5);
    reserve(bx, by, bx + tw, by + th);
    const where = (n.kind === "pile" || n.kind === "root" ? n.label || n.name : n.depth >= 1 ? folderPath(n) : "root files") + (e.rest ? " (smaller folders)" : "");
    const one = e.files.size === 1 ? M.files[[...e.files][0]] : null;
    const what = f ? f.name : one ? one.name : `${e.files.size} changed files`;
    const bd: Badge = {
      x: e.x,
      y: e.y,
      text: `${where} · ${c} ${c === 1 ? "file imports" : "files import"} ${what}`,
      short: `${where} · ${c}`,
      alpha: 1,
      n: c,
      ag: null,
      pin: true,
      node: n,
      files: e.files,
      ids: e.ids,
      what,
    };
    v.badgeHits!.push({ r: [bx, by, bx + tw, by + th], bd });
  }
}

interface BlastSpec {
  ag: AgentDef | null;
  A: AgentState | null;
  live: boolean;
  fid: number | null;
  hover: boolean;
  /** a selected / hovered file: the gold leaves only — no per-folder pills (the card carries the count; "Show the
   * N that depend on it" pins the full picture) */
  quiet?: boolean;
}

// draws one blast (an agent's cumulative importers, or a pinned/hovered file's importers) and files its ids into badge sets
function drawBlast(v: TreeView, B: BlastSpec, px: number, inView: (x0: number, y0: number, x1: number, y1: number) => boolean) {
  const { g, M } = v;
  const P = v.P;
  let ids: Set<number>,
    by: Map<number, number> | null,
    src: Leaf | null = null,
    latest: number | null = null,
    age = 99;
  if (B.live && B.A) {
    const ab = agentBlast(M, B.A, v.blastCache);
    ids = ab.ids;
    by = ab.by;
    latest = B.A.lastEdit ? B.A.lastEdit.f.id : null;
    if (B.A.lastEdit) {
      src = B.A.lastEdit.f.leaf;
      age = v.t - B.A.lastEdit.t;
    }
  } else {
    const f = M.files[B.fid!];
    if (!f) return;
    src = f.leaf;
    if (!src) return;
    let b = v.blastCache.get(B.fid!);
    if (!b) {
      b = blastOf(M, B.fid!);
      v.blastCache.set(B.fid!, b);
    }
    ids = b.h1;
    by = null;
    latest = B.fid;
  }
  if (!ids.size) return;
  const speed = M.Rtyp * 0.9; // world units per second: the wave takes ~1.5 s to cross the tree
  // the gold wave spreads from a fresh edit; without animation the whole blast shows at once, so a badge
  // never freezes on a half-counted number that disagrees with the card's "affects N"
  const ripple = v.playing ? age * speed : 1e12;
  const alphaAll = B.hover ? 0.85 : 1;
  if (B.live && src && v.playing && age >= 0 && age < 1.2) {
    g.strokeStyle = P.gold;
    g.lineWidth = 1.5 * px;
    g.globalAlpha = Math.max(0, 1 - age / 1.2) * 0.7;
    g.beginPath();
    g.arc(src.x, src.y, ripple * 0.6 + 6 * px, 0, TAU);
    g.stroke();
    g.globalAlpha = 1;
  }
  const outside = (id: number) => {
    if (!(B.live && src && by && by.get(id) === latest)) return false;
    const l = M.files[id].leaf;
    return !!l && Math.hypot(l.x - src.x, l.y - src.y) > ripple;
  };
  const leafShown = v.leavesShown;
  const outline = B.live && B.ag ? B.ag.color : P.mark;
  // gold in the only-here dusk is half-dimmed with everything else out there, so "the rest dims" stays true
  const dimmed = (l: Leaf) => {
    if (!v.hasOnly) return false;
    const own = l.term ? l.term.node : l.pile;
    return !!(own && !own.lit);
  };
  const draw = (pred: (id: number) => boolean, a: number, rDot: number, dimPass: boolean) => {
    g.beginPath();
    let any = false;
    for (const id of ids) {
      if (!pred(id)) continue;
      const l = M.files[id] && M.files[id].leaf;
      if (!l) continue;
      if (dimmed(l) !== dimPass) continue;
      if (l.term && l.term.node.fAnc) continue; // folded: counted on the clump's badge only
      if (outside(id)) continue;
      if (!inView(l.x - 20, l.y - 20, l.x + 20, l.y + 20)) continue;
      any = true;
      if (leafShown) leafPath(g, l);
      else {
        const r = rDot * px;
        g.moveTo(l.x + r, l.y);
        g.arc(l.x, l.y, r, 0, TAU);
      }
    }
    if (!any) return;
    g.globalAlpha = a * 0.22 * alphaAll;
    g.lineWidth = (leafShown ? 5 : 4) * px;
    g.strokeStyle = P.gold;
    g.stroke();
    g.globalAlpha = a * alphaAll;
    g.fillStyle = P.gold;
    g.fill();
    // the owning bird's colour rings every gold leaf: gold is attributable even with two agents editing
    g.globalAlpha = Math.min(1, a * 1.15) * alphaAll;
    g.lineWidth = (leafShown ? 2.2 : 2.4) * px;
    g.strokeStyle = outline;
    g.stroke();
    g.globalAlpha = 1;
    // mid zoom: each gold leaf also carries its bird's glyph (● / ▲), so ownership reads without colour
    const lp = v.cam.z * S0;
    if (B.live && B.ag && lp >= 7 && lp < 40) {
      g.fillStyle = outline;
      g.strokeStyle = P.glyphStroke;
      g.lineWidth = 1 * px;
      const gr = 3.2 * px;
      for (const id of ids) {
        if (!pred(id)) continue;
        const l = M.files[id] && M.files[id].leaf;
        if (!l || (l.term && l.term.node.fAnc) || dimmed(l) !== dimPass) continue;
        if (outside(id)) continue;
        if (!inView(l.x - 20, l.y - 20, l.x + 20, l.y + 20)) continue;
        const gx = l.x + Math.max(l.len * 0.45, 4 * px),
          gy = l.y - Math.max(l.len * 0.45, 4 * px);
        g.beginPath();
        glyphPath(g, B.ag.glyph, gx, gy, gr);
        g.fill();
        g.stroke();
      }
    }
  };
  for (const dimPass of v.hasOnly ? [false, true] : [false]) {
    const k = dimPass ? 0.45 : 1;
    if (B.live && by) {
      draw((id) => by!.get(id) !== latest, 0.6 * k, 2.4, dimPass);
      draw((id) => by!.get(id) === latest, 0.95 * k, 2.8, dimPass);
    } else draw(() => true, 0.95 * k, 2.8, dimPass);
  }
  // vines on hover: faint thin curves to direct dependents
  if (B.hover && src && ids.size <= 24) {
    g.strokeStyle = P.vine;
    g.lineWidth = 1 * px;
    g.beginPath();
    for (const id of ids) {
      const l = M.files[id] && M.files[id].leaf;
      if (!l) continue;
      const mx = (l.x + src.x) / 2,
        my = Math.min(l.y, src.y) - Math.hypot(l.x - src.x, l.y - src.y) * 0.25;
      g.moveTo(src.x, src.y);
      g.quadraticCurveTo(mx, my, l.x, l.y);
    }
    g.stroke();
  }
  // badge sets: per (folder, blast) so a badge always belongs to one agent / one pinned file
  if (B.quiet) return;
  const age2 = B.live && v.playing ? Math.min(1, Math.max(0, (age - 0.4) / 0.6)) : 1;
  addBadgeSets(v, ids, by, { ag: B.live ? B.ag : null, hover: B.hover, fid: B.fid }, age2, (id) => !outside(id));
}

/** A bird's marker glyph as a path (● ▲ ■ ◆, else a dot; a helper's index is dropped). */
function glyphPath(g: G2, glyph: string, gx: number, gy: number, gr: number) {
  glyph = glyphShape(glyph);
  if (glyph === "▲") {
    g.moveTo(gx, gy - gr);
    g.lineTo(gx + gr * 0.95, gy + gr * 0.7);
    g.lineTo(gx - gr * 0.95, gy + gr * 0.7);
    g.closePath();
  } else if (glyph === "■") g.rect(gx - gr * 0.75, gy - gr * 0.75, gr * 1.5, gr * 1.5);
  else if (glyph === "◆") {
    g.moveTo(gx, gy - gr);
    g.lineTo(gx + gr, gy);
    g.lineTo(gx, gy + gr);
    g.lineTo(gx - gr, gy);
    g.closePath();
  } else g.arc(gx, gy, gr * 0.85, 0, TAU);
}

export function nestPoint(M: Model, n: TNode | null | undefined): [number, number] {
  if (!n || n === M.crown) return [0, -S0 * 0.5];
  return n.F || [n.cx, n.cy];
}

function bonkPoint(v: TreeView, bk: Pick<Blocked, "f" | "z" | "kind">): [number, number] | null {
  const M = v.M;
  const zn = bk.z && bk.z.node;
  if (bk.kind === "keep" && zn && zn.branch) {
    const p = zn.branch.pts[4];
    return [p[0], p[1]];
  }
  if (bk.kind === "keep" && zn && zn.kind === "pile") return [zn.cx, M.groundY - (zn.h || 0)];
  const l = bk.f && bk.f.leaf;
  return l ? [l.x, l.y] : null;
}

function targetOf(v: TreeView, r: BirdEv): [number, number] {
  const M = v.M;
  if (r.type === "nest" || r.type === "plan") return nestPoint(M, r.nestNode);
  if (r.blocked) {
    const p = bonkPoint(v, { f: r.f, z: r.blocked, kind: r.blocked.type });
    if (p) return p;
  }
  const l = r.f && r.f.leaf;
  return l ? [l.x, l.y] : nestPoint(M, r.nestNode);
}

interface Pose {
  x: number;
  y: number;
  fly: boolean;
  dx: number;
  dy: number;
  hover?: boolean;
  bonk?: boolean;
  blocked?: boolean;
  editing?: boolean;
  nest?: boolean;
  leaf?: Leaf | null;
}

function birdPose(v: TreeView, A: AgentState, t: number): Pose | null {
  const M = v.M;
  const evs = A.evs;
  if (!evs.length) return null;
  const n = evs.length;
  const cur = evs[n - 1];
  const sky: [number, number] = [A.ag.id % 2 === 0 ? -M.Rtyp * 1.4 : M.Rtyp * 1.4, M.crownBounds.y0 - M.Rtyp * 0.4];
  const restOf = (i: number): [number, number] => {
    const r = evs[i];
    const tgt = targetOf(v, r);
    if (r.blocked) {
      const pv = i > 0 ? restOf(i - 1) : sky;
      return [tgt[0] + (pv[0] - tgt[0]) * 0.28, tgt[1] + (pv[1] - tgt[1]) * 0.28 - S0 * 0.6];
    }
    return tgt;
  };
  const prev = n > 1 ? restOf(n - 2) : sky;
  const tgt = targetOf(v, cur);
  const dist = Math.hypot(tgt[0] - prev[0], tgt[1] - prev[1]);
  const dur = Math.max(0.7, Math.min(2.2, 0.6 + (dist / Math.max(1, M.Rtyp)) * 1.3));
  // without animation a bird is never shown in transit: it sits where its latest event put it
  const u = v.playing ? (t - cur.t) / dur : Math.max((t - cur.t) / dur, 1.3);
  const ease = (x: number) => (x < 0.5 ? 2 * x * x : 1 - Math.pow(-2 * x + 2, 2) / 2);
  if (cur.blocked) {
    const rest = restOf(n - 1);
    if (u < 0.55) {
      const k = ease(Math.max(0, u) / 0.55);
      const x = prev[0] + (tgt[0] - prev[0]) * k,
        y = prev[1] + (tgt[1] - prev[1]) * k - Math.sin(k * Math.PI) * dist * 0.15;
      return { x, y, fly: true, dx: tgt[0] - prev[0], dy: tgt[1] - prev[1] };
    }
    const k = Math.min(1, (u - 0.55) / 0.6);
    const kk = 1 - Math.pow(1 - k, 3);
    return { x: tgt[0] + (rest[0] - tgt[0]) * kk, y: tgt[1] + (rest[1] - tgt[1]) * kk, fly: k < 1, hover: k >= 1, dx: prev[0] - tgt[0], dy: 0, bonk: u < 1.2, blocked: true };
  }
  if (u < 1) {
    const k = ease(Math.max(0, u));
    const cx = (prev[0] + tgt[0]) / 2,
      cy = (prev[1] + tgt[1]) / 2 - dist * 0.28;
    const mt = 1 - k;
    const x = mt * mt * prev[0] + 2 * mt * k * cx + k * k * tgt[0],
      y = mt * mt * prev[1] + 2 * mt * k * cy + k * k * tgt[1];
    const dx = 2 * mt * (cx - prev[0]) + 2 * k * (tgt[0] - cx),
      dy = 2 * mt * (cy - prev[1]) + 2 * k * (tgt[1] - cy);
    return { x, y, fly: true, dx, dy };
  }
  const onLeaf = !!(cur.f && cur.f.leaf) && !(cur.type === "nest" || cur.type === "plan");
  return {
    x: tgt[0],
    y: tgt[1],
    fly: false,
    dx: tgt[0] - prev[0],
    dy: 0,
    editing: cur.type === "edit" || cur.type === "create",
    nest: !onLeaf,
    leaf: onLeaf ? cur.f!.leaf : null,
  };
}

/** A helper (subagent) bird is this much of its parent's size. */
export const SUB_SCALE = 0.68;
/** A finished helper: home in (its flight) for this long, then fade over SUB_FADE_S. */
const SUB_HOME_S = 1.9,
  SUB_FADE_S = 1.1;

/** How opaque a bird is: 1, except a finished helper, which flies back into its
 * parent's nest and fades there (gone at once without animation). */
export function birdAlpha(v: Pick<TreeView, "playing">, A: AgentState, t: number): number {
  if (!A.ag.parent || !A.done) return 1;
  if (!v.playing || !A.cur) return 0;
  const age = t - A.cur.t;
  return age < SUB_HOME_S ? 1 : Math.max(0, 1 - (age - SUB_HOME_S) / SUB_FADE_S);
}

/** A bird's world position at view time t (the follow camera's target). */
export function birdWorldAt(v: TreeView, A: AgentState, t: number): [number, number] | null {
  const p = birdPose(v, A, t);
  return p ? [p.x, p.y] : null;
}

function drawBird(g: G2, sx: number, sy: number, size: number, color: string, pose: Pose, now: number, motion: boolean) {
  const img = tinted(color);
  const bw = size,
    bh = (bw * 160) / 216,
    HINGE = 0.56,
    hy = Math.round(160 * HINGE),
    hp = bh * HINGE;
  g.save();
  if (pose.fly || pose.hover) {
    const a = Math.atan2(pose.dy, pose.dx);
    const left = pose.dx < 0;
    g.translate(sx, sy - bh * 0.25);
    g.rotate(left ? a + Math.PI : a);
    g.rotate(left ? 0.35 : -0.35);
    if (left) g.scale(-1, 1);
    const f = !motion ? 0.8 : pose.hover ? 0.6 + 0.4 * Math.sin(now / 70) : 0.62 + 0.42 * Math.sin(now / 95);
    if (img) {
      g.drawImage(img, 0, hy, 216, 160 - hy, -bw / 2, hp - bh / 2, bw, bh - hp);
      g.translate(0, hp - bh / 2);
      g.scale(1, Math.max(0.2, f));
      g.drawImage(img, 0, 0, 216, hy, -bw / 2, -hp, bw, hp);
    }
  } else {
    // perched: wings folded, a small bob (pecking while editing)
    const left = pose.dx < 0;
    const bob = !motion ? 0 : pose.editing ? Math.max(0, Math.sin(now / 180)) * 2.2 : Math.sin(now / 600) * 0.6;
    g.translate(sx, sy - bh * 0.42 + bob);
    g.rotate(pose.editing ? 0.25 : 0.12);
    if (left) g.scale(-1, 1);
    if (img) {
      g.drawImage(img, 0, hy, 216, 160 - hy, -bw / 2, hp - bh / 2, bw, bh - hp);
      g.translate(0, hp - bh / 2);
      g.scale(1, 0.32);
      g.drawImage(img, 0, 0, 216, hy, -bw / 2, -hp, bw, hp);
    }
  }
  g.restore();
}

function drawNest(g: G2, P: Palette, sx: number, sy: number, s: number, ag: AgentDef, grow: number) {
  g.save();
  g.translate(sx, sy);
  g.scale(grow, grow);
  // bowl
  g.fillStyle = P.nestBowl;
  g.beginPath();
  g.moveTo(-s * 0.62, -s * 0.06);
  g.quadraticCurveTo(0, s * 0.62, s * 0.62, -s * 0.06);
  g.quadraticCurveTo(0, s * 0.12, -s * 0.62, -s * 0.06);
  g.fill();
  // eggs peeking over the rim
  g.fillStyle = ag.color;
  g.beginPath();
  g.ellipse(-s * 0.13, -s * 0.07, s * 0.12, s * 0.15, -0.2, 0, TAU);
  g.fill();
  g.beginPath();
  g.ellipse(s * 0.13, -s * 0.06, s * 0.12, s * 0.15, 0.25, 0, TAU);
  g.fill();
  // woven twigs
  g.strokeStyle = P.nestTwig;
  g.lineWidth = Math.max(1, s * 0.055);
  g.lineCap = "round";
  g.beginPath();
  for (let k = 0; k < 5; k++) {
    const y = -s * 0.02 + k * s * 0.07;
    const w = s * (0.62 - k * 0.09);
    g.moveTo(-w, y + (k % 2 ? s * 0.03 : -s * 0.02));
    g.quadraticCurveTo(0, y + s * 0.16, w, y + (k % 2 ? -s * 0.02 : s * 0.03));
  }
  g.stroke();
  g.strokeStyle = P.nestRim;
  g.lineWidth = Math.max(1, s * 0.05);
  g.beginPath();
  g.moveTo(-s * 0.66, -s * 0.05);
  g.quadraticCurveTo(0, s * 0.1, s * 0.66, -s * 0.07);
  g.stroke();
  g.restore();
}

function labelFont(P: Palette, n: TNode): string {
  // size follows how much code the folder holds, not its depth: big folders are loud, tiny ones quiet
  const nf = Math.max(1, n.nFiles || 1);
  const sz = Math.max(10.5, Math.min(15, 9.5 + 2.1 * Math.log10(nf + 1)));
  const bold = n.depth === 1 || nf >= 40;
  return `${bold ? 600 : 500} ${sz.toFixed(1)}px ${P.font}`;
}

function drawPriorityLeafLabels(
  v: TreeView,
  g: G2,
  toS: (x: number, y: number) => [number, number],
  fits: Fits,
  reserve: Reserve,
  leafPx: number,
  birdS: Array<{ A: AgentState; visible: boolean; pose: Pose } | null>
) {
  if (leafPx < 9) return;
  const P = v.P;
  const z = v.cam.z;
  const selLeaf = v.sel && v.sel.leaf ? v.sel.leaf : null;
  const want: Array<{ l: Leaf; col: string; weight: number }> = [];
  if (selLeaf) want.push({ l: selLeaf, col: P.sel, weight: 600 });
  if (v.markLeaf && v.markLeaf !== selLeaf) want.push({ l: v.markLeaf, col: P.mark, weight: 500 });
  // each bird's current leaf is named in the bird's colour (its tag sits above the bird, away from the leaf)
  for (const B of birdS || []) if (B && B.visible && B.pose.leaf) want.push({ l: B.pose.leaf, col: B.A.ag.color, weight: 600 });
  g.strokeStyle = P.halo;
  g.lineWidth = 3;
  g.textBaseline = "middle";
  g.lineJoin = "round";
  // the selection ring is the answer to "where is it": no other name may sit across it
  if (selLeaf) {
    const [sx, sy] = toS(selLeaf.x, selLeaf.y),
      r = Math.max(selLeaf.len * 0.8 * z, 8) + 3;
    reserve(sx - r, sy - r, sx + r, sy + r);
  }
  const seen = new Set<Leaf>();
  for (const { l, col, weight } of want) {
    if (seen.has(l)) continue;
    seen.add(l);
    if (l.file.ghost && !v.agents.some((A) => A.created.has(l.file.id))) continue;
    const [sx, sy] = toS(l.x, l.y);
    if (sx < 0 || sx > v.W || sy < 0 || sy > v.H) continue;
    g.font = `${weight} 10.5px ${P.font}`;
    const txt = l.file.name,
      tw = g.measureText(txt).width;
    const off = Math.max(l.len * z * 0.5 + 4, l === selLeaf ? Math.max(l.len * 0.8 * z, 8) + 5 : 0);
    let placedAt: [number, number] | null = null,
      far = false;
    const cands: Array<[number, number]> = [
      [off, 0],
      [-off - tw, 0],
      [-tw / 2, -off - 9],
      [-tw / 2, off + 9],
      [off + 10, -16],
      [off + 10, 16],
      [-off - tw - 10, -16],
      [-off - tw - 10, 16],
      [-tw / 2, -off - 28],
      [-tw / 2, off + 28],
    ];
    for (let i = 0; i < cands.length; i++) {
      const [ax, ay] = cands[i];
      const x0 = sx + ax,
        y0 = sy + ay - 7;
      if (fits(x0 - 3, y0, x0 + tw + 3, y0 + 14)) {
        placedAt = [x0, sy + ay];
        far = i >= 4;
        break;
      }
    }
    if (!placedAt) {
      // no free spot: right of the leaf anyway — unless that is under a panel (it would show through gaps)
      const x0 = sx + off,
        y0 = sy - 7;
      let under = false;
      for (const r of v.hudRects || [])
        if (x0 + tw > r[0] && x0 < r[2] && y0 + 14 > r[1] && y0 < r[3]) {
          under = true;
          break;
        }
      if (under) continue;
      placedAt = [sx + off, sy];
    }
    reserve(placedAt[0] - 3, placedAt[1] - 7, placedAt[0] + tw + 3, placedAt[1] + 7);
    if (far) {
      // a leader from the label to its leaf, so an offset name is never read as a neighbour's
      g.save();
      g.strokeStyle = col;
      g.lineWidth = 1;
      g.globalAlpha = 0.8;
      g.beginPath();
      const lx = Math.max(placedAt[0], Math.min(placedAt[0] + tw, sx));
      g.moveTo(sx, sy);
      g.lineTo(lx, placedAt[1] + (placedAt[1] < sy ? 7 : -7));
      g.stroke();
      g.restore();
    }
    g.strokeText(txt, placedAt[0], placedAt[1]);
    g.fillStyle = col;
    g.fillText(txt, placedAt[0], placedAt[1]);
    v.labelHits!.push({ r: [placedAt[0], placedAt[1] - 7, placedAt[0] + tw, placedAt[1] + 7], leaf: l });
  }
}

interface LabelCand {
  type: "pile" | "clump" | "branch";
  n: TNode;
  sx?: number;
  sy?: number;
  pri: number;
}

function drawLabels(
  v: TreeView,
  g: G2,
  toS: (x: number, y: number) => [number, number],
  fits: Fits,
  reserve: Reserve,
  leafPx: number,
  inView: (x0: number, y0: number, x1: number, y1: number) => boolean,
  createdGhost: Set<number>,
  phase: "limbs" | "rest"
) {
  const M = v.M,
    P = v.P;
  const z = v.cam.z;
  const halo = (txt: string, x: number, y: number) => {
    g.strokeText(txt, x, y);
    g.fillText(txt, x, y);
  };
  g.lineJoin = "round";
  g.textBaseline = "middle";
  const W = v.W,
    H = v.H;
  const onScreen = (x: number, y: number, m: number) => x > -m && x < W + m && y > -m && y < H + m;
  const armed = v.tool !== "explore"; // a paint tool asks you to click a folder name: show more of them
  const cand: LabelCand[] = [];
  // the folder each bird nests in, the folder whose card is open and every folder carrying a rule are named
  // with top-level priority
  const nestSet = new Set<TNode>();
  for (const A of v.agents) if (A.nest && A.nest.depth >= 1 && A.cur && !A.ag.parent) nestSet.add(A.nest);
  const ruleSet = new Set<TNode>();
  for (const zz of v.zones) if (zz.node) ruleSet.add(zz.node);
  const selNode = v.infoNode && v.infoNode.depth >= 1 ? v.infoNode : null;
  const fol = v.fol;
  // a rule's folder must be named unless its own tag (which says the path) is already on screen
  const tagged = new Set<TNode>();
  for (const h of v.tagHits || []) if (h.z.node) tagged.add(h.z.node);
  const hovN = hitNode(v.hover);
  const mustOf = (n: TNode) => nestSet.has(n) || (ruleSet.has(n) && !tagged.has(n)) || n === selNode || hovN === n;
  // how many folder names the view may carry: ~25 at whole-tree zoom (top-level + the biggest folders), more as
  // you zoom in; the budget is shared by both passes
  const zr = v.fitZv ? z / v.fitZv : 2;
  // (a small repo can name every folder of 8+ files at once; a big one shows its top level and biggest folders)
  const base0 = M.files.length > 1500 ? 25 : 45;
  // a small pane carries fewer names: the budget scales with its area against a 1440x900 screen
  const areaK = Math.max(0.35, Math.min(1, Math.sqrt((W * H) / (1440 * 900))));
  const budget = Math.round(Math.min(90, base0 * areaK * Math.pow(Math.max(1, zr), 1.25))) + (armed ? 20 : 0);
  if (phase === "limbs") v.labelBudget = budget;
  const minFiles = zr < 1.6 ? 8 : zr < 3 ? 4 : 1;
  const groundS = M.groundY * z + (H / 2 - v.cam.y * z);
  const crumbAnc = v.crumbAnc;
  // LIT PATHS: a folder is named when there is something to see there (a change below it, a nest, a rule, the
  // open card) or it is top-level; the rest of the tree stays unlabelled until you zoom in (or arm a paint tool)
  const litI = v.litInfo || new Map<TNode, LitInfo>();
  const quietZoom = zr < 2.4 && !armed;
  for (const n of M.nodes) {
    if (n.depth < 1 || n.vHidden) continue;
    const nest = nestSet.has(n),
      sel = n === selNode,
      rule = ruleSet.has(n),
      lit = litI.has(n);
    const must = nest || sel || rule;
    // a pass-through folder (one lit sub-folder holding all its changes) is named by that sub-folder instead
    if (lit && !must && n.depth >= 2 && n.kids) {
      const lk = n.kids.filter((k) => litI.has(k));
      if (lk.length === 1 && litI.get(lk[0])!.n === litI.get(n)!.n && hovN !== n) continue;
    }
    const early = n.depth === 1 || must || lit;
    if ((phase === "limbs") !== early) continue;
    if (nest && v.nestLabelled && v.nestLabelled.has(n)) continue; // the nest pill already names it
    if (rule && tagged.has(n) && !nest && !sel && !lit && hovN !== n) continue; // its sign already names it
    if (n.quiet && !must && hovN !== n) continue; // pass-through scaffolding (src/main, java/com/…) is not a place
    if (crumbAnc && crumbAnc.has(n) && !must && hovN !== n) continue; // the breadcrumb already says it
    if (n.kind === "pile") {
      const [sx, sy] = toS(n.cx, M.groundY + S0 * 0.2);
      if (onScreen(sx, sy, 60)) cand.push({ type: "pile", n, sx, sy, pri: 5e5 + n.nFiles });
      continue;
    }
    const big = n.depth === 1;
    const srad = n.rad * z;
    if (quietZoom && !lit && !must && !big && hovN !== n && !n.vFold) continue;
    // zoomed out, a folder that only passes changes through (its pills below already count them) stays quiet
    if (quietZoom && lit && !must && !big && hovN !== n && !n.vColl && !litI.get(n)!.own) continue;
    // a name needs something visible to name: no labels on near-invisible twigs, small folders wait for zoom
    if (!must && !big && !lit && (n.nFiles < minFiles || srad < 7)) continue;
    const pri = (sel ? 4e6 : 0) + (nest ? 3e6 : 0) + (lit ? 2.5e6 : 0) + (rule ? 2e6 : 0) + (big ? 1e6 : 0) + n.nFiles * 10 + srad + (n.vFold ? 1e7 : 0);
    if (n.vColl) {
      if (!big && !must && !lit && !n.vFold && (srad < (armed ? 10 : 15) || n.nFiles < 2)) continue;
      const [sx, sy] = toS(n.cx, n.cy);
      if (onScreen(sx, sy, 80)) cand.push({ type: "clump", n, sx, sy, pri });
    } else if (n.branch) {
      if (!big && !must && !lit && srad < (armed ? 18 : 24) && n.nFiles < 8) continue;
      cand.push({ type: "branch", n, pri });
    }
  }
  cand.sort((a, b) => b.pri - a.pri);
  // one placer for every folder name: the first candidate that is free AND not on another folder's foliage; else
  // a spot a little way off with a leader line back to what it names; a name that must show (a nest, a rule, the
  // open card, the hovered folder) may fall back to any free spot; anything else waits for more zoom
  const LEAD_A = [-Math.PI / 2, -Math.PI / 4, (-3 * Math.PI) / 4, 0, Math.PI, Math.PI / 4, (3 * Math.PI) / 4, Math.PI / 2];
  interface Placed {
    x: number;
    y: number;
    ang: number;
    R: R4;
    lead?: [number, number];
  }
  const place = (
    tries: Array<[number, number, number]>,
    w: number,
    h: number,
    n: TNode,
    subtree: boolean,
    ring: { x: number; y: number; r: number } | null,
    okY: (y: number) => boolean,
    anchors: Array<[number, number, number]>
  ): Placed | null => {
    const rectOf = (x: number, y: number, ang: number): R4 => {
      const ca = Math.abs(Math.cos(ang)),
        sa = Math.abs(Math.sin(ang));
      const hw = (w * ca + h * sa) / 2,
        hh = (w * sa + h * ca) / 2;
      return [x - hw, y - hh, x + hw, y + hh];
    };
    // a name is whole on screen or not drawn at all; free of other names and panels; and (clean) off foreign foliage
    // ... and PINNED to its own cluster: nothing of another folder's (foliage, nest, zone sign, territory) is
    // touched or nearer than its own foliage / stem / nest — a name parked by a neighbour names the neighbour
    const ok = (R: R4, clean: boolean, sub?: boolean) =>
      R[0] >= 2 &&
      R[2] <= W - 2 &&
      R[1] >= 2 &&
      R[3] <= H - 2 &&
      okY(R[1]) &&
      fits(R[0], R[1], R[2], R[3]) &&
      (!clean || !fol || !fol.hit(R[0] + 1, R[1] + 1, R[2] - 1, R[3] - 1, n, !!sub)) &&
      (!fol || fol.belongs(R[0], R[1], R[2], R[3], n, anchors, 220, clr));
    let clr = 14;
    const ringSpot = (sub: boolean, clean: boolean, ds: number[]): Placed | null => {
      if (!ring) return null;
      for (const d of ds)
        for (const a of LEAD_A) {
          const ux = Math.cos(a),
            uy = Math.sin(a),
            ext = (Math.abs(ux) * w) / 2 + (Math.abs(uy) * h) / 2;
          const x = ring.x + ux * (ring.r + d + ext),
            y = ring.y + uy * (ring.r + d + ext),
            R = rectOf(x, y, 0);
          if (ok(R, clean, sub)) return { x, y, ang: 0, R, lead: [ring.x + ux * ring.r, ring.y + uy * ring.r] };
        }
      return null;
    };
    // 1. on air or its own clump; 2. a short way off with a leader (never a long one: a far name is shortened
    // instead); 3. (a folder with sub-folders) over its own sub-folders' leaves, which it does contain; 4. a name
    // that must show: over its own anything, still pinned. All of it first well clear of other folders' loud things
    // (14 px), then merely not touching them
    for (const c of [14, 4]) {
      clr = c;
      for (const sub of subtree ? [true] : [false, true]) {
        for (const [x, y, ang] of tries) {
          const R = rectOf(x, y, ang);
          if (ok(R, true, sub)) return { x, y, ang, R };
        }
        const r = ringSpot(sub, true, [8, 20]);
        if (r) return r;
      }
      if (mustOf(n)) {
        for (const [x, y, ang] of tries) {
          const R = rectOf(x, y, ang);
          if (ok(R, false, false)) return { x, y, ang, R };
        }
        const r = ringSpot(false, false, [8]);
        if (r) return r;
      }
    }
    return null;
  };
  const leader = (Pl: Placed, col: string) => {
    if (!Pl.lead) return;
    const [lx, ly] = Pl.lead,
      R = Pl.R,
      qx = Math.max(R[0] + 2, Math.min(R[2] - 2, lx)),
      qy = Math.max(R[1] + 3, Math.min(R[3] - 3, ly));
    g.save();
    g.strokeStyle = col;
    g.globalAlpha = 0.55;
    g.lineWidth = 1;
    g.beginPath();
    g.moveTo(lx, ly);
    g.lineTo(qx, qy);
    g.stroke();
    g.restore();
  };
  const hasOnly = !!v.hasOnly;
  // a lit folder's name carries its change count in the changing bird's colour: "core ●4", "frontend ●2 ▲2"
  // (a bare count chip — the name dropped for room — has no leading space)
  const segTxt = (q: { glyph: string; n: number }, first = false) => (first ? "" : " ") + q.glyph + q.n;
  const segsW = (info: LitInfo | undefined, bare = false) => {
    if (!info) return 0;
    const f0 = g.font;
    g.font = `700 11px ${P.font}`;
    let w = 0;
    info.segs.forEach((q, i) => (w += g.measureText(segTxt(q, bare && i === 0)).width));
    g.font = f0;
    return w + (bare ? 0 : 2);
  };
  // a folder that holds changes is one clickable pill — "backend › web/core ●4", dots in the changing birds'
  // colours — and the click opens its card (Keep out / Only here are there); everything else stays plain text
  const PILL_FONT = `650 11.5px ${P.font}`;
  const drawNamed = (name: string, info: LitInfo | undefined, cx: number, cy: number, col: string, pill = false) => {
    const bare = !name;
    const w1 = bare ? 0 : g.measureText(name).width,
      tw = w1 + segsW(info, bare);
    let x = cx - tw / 2;
    g.textAlign = "left";
    if (pill) {
      quietPill(g, P, x - 7, cy - 10, tw + 14, 20);
      g.fillStyle = col;
      if (!bare) g.fillText(name, x, cy + 0.5);
      if (!info) return;
      if (!bare) x += w1 + 2;
      const f1 = g.font;
      g.font = `700 11px ${P.font}`;
      info.segs.forEach((q, i) => {
        const t2 = segTxt(q, bare && i === 0);
        g.fillStyle = q.col;
        g.fillText(t2, x, cy + 0.5);
        x += g.measureText(t2).width;
      });
      g.font = f1;
      g.strokeStyle = P.halo;
      return;
    }
    g.fillStyle = col;
    if (!bare) halo(name, x, cy);
    if (!info) return;
    if (!bare) x += w1 + 2;
    const f0 = g.font;
    g.font = `700 11px ${P.font}`;
    info.segs.forEach((q, i) => {
      const t2 = segTxt(q, bare && i === 0);
      g.fillStyle = q.col;
      halo(t2, x, cy);
      x += g.measureText(t2).width;
    });
    g.font = f0;
  };
  for (const c of cand) {
    if ((v.labelBudget || 0) <= 0 && c.pri < 2e6) break;
    const n = c.n;
    const info = litI.get(n);
    // a changed folder below the top level says which limb it is on: "backend › config", never just "config"
    const pill = !!info && (info.own > 0 || !!n.vColl);
    // (a folder on the way to a change too: zoomed in, "src" alone reads as the "frontend" it was a moment ago)
    const plain = n.kind === "root" ? n.label || n.name : n.disp || shortName(n.name);
    const name = (pill || info) && n.depth >= 2 ? folderPath(n) : plain;
    // a name with no room by its own cluster is SHORTENED, never moved off beside a neighbour: the full path, then
    // the folder's own name, then (a changed folder) just its count chip — else it is not drawn this frame
    const names = [name];
    if (plain !== name) names.push(plain);
    if (pill && info && info.segs.length) names.push("");
    // in an only-here dusk everything outside the territory recedes — its names and pills too (not a folder
    // whose changes are all another session's: that session works in its own worktree, unrestricted)
    const dusk = hasOnly && !n.lit && !(info && info.n > 0 && info.bound === 0);
    g.globalAlpha = dusk ? 0.5 : 1;
    const col = pill
      ? P.pillText
      : n.keep
      ? P.keepText
      : dusk
        ? P.duskLabel
        : info || nestSet.has(n) || n === selNode || hovN === n
          ? P.text
          : hasOnly && n.lit
            ? P.onlyText
            : P.calmLabel;
    const underground = n.kind === "root"; // a root's name stays below the ground line
    const okY = (y: number) => !underground || y > groundS + 9;
    if (c.type === "pile") {
      g.font = `600 11px ${P.font}`;
      const t1 = n.label || n.name,
        t2 = `${n.nFiles} file${n.nFiles === 1 ? "" : "s"}`;
      const w = Math.max(g.measureText(t1).width, 40) + 8;
      const x0 = c.sx! - w / 2,
        y0 = c.sy! + 4;
      if (!fits(x0, y0, x0 + w, y0 + 28)) continue;
      reserve(x0, y0, x0 + w, y0 + 28);
      v.labelHits!.push({ r: [x0, y0, x0 + w, y0 + 28], n });
      g.textAlign = "center";
      g.strokeStyle = P.halo;
      g.lineWidth = 3.5;
      g.fillStyle = dusk ? P.duskLabel : P.pileLabel;
      halo(t1, c.sx!, y0 + 7);
      g.font = `10px ${P.font}`;
      g.fillStyle = P.muted;
      halo(t2, c.sx!, y0 + 20);
      g.textAlign = "left";
      v.labelBudget = (v.labelBudget || 0) - 1;
      continue;
    }
    if (c.type === "clump") {
      g.font = pill ? PILL_FONT : labelFont(P, n);
      const t2 = n.vFold ? `folded · ${n.nFiles} files` : "";
      // on the clump it names, else just above / below / beside it — never on a neighbour's leaves
      const rr = n.rad * z;
      const sx = c.sx!,
        sy = c.sy!;
      let Pl: Placed | null = null,
        nm = name;
      for (const cand1 of names) {
        const bare = !cand1;
        const w1 = (bare ? 0 : g.measureText(cand1).width) + segsW(info, bare) + (pill ? 14 : 0);
        const w = Math.max(w1, t2 ? 80 : 0) + 8,
          h = t2 ? 30 : pill ? 22 : 16;
        Pl = place(
          [
            [sx, sy, 0],
            [sx, sy - rr - h / 2 - 3, 0],
            [sx, sy + rr + h / 2 + 3, 0],
            [sx + rr + w / 2 + 3, sy, 0],
            [sx - rr - w / 2 - 3, sy, 0],
          ],
          w,
          h,
          n,
          true,
          { x: sx, y: sy, r: rr },
          okY,
          [[sx, sy, rr]]
        );
        if (Pl) {
          nm = cand1;
          break;
        }
      }
      if (!Pl) continue;
      reserve(Pl.R[0], Pl.R[1], Pl.R[2], Pl.R[3]);
      v.labelHits!.push({ r: Pl.R, n });
      leader(Pl, col);
      g.strokeStyle = P.halo;
      g.lineWidth = 3.5;
      drawNamed(nm, info, Pl.x, t2 ? Pl.y - 6 : Pl.y, col, pill);
      g.textAlign = "center";
      if (t2) {
        g.font = `10.5px ${P.font}`;
        g.fillStyle = dusk ? P.duskLabel : P.text;
        halo(t2, Pl.x, Pl.y + 8);
      }
      g.textAlign = "left";
      v.labelBudget = (v.labelBudget || 0) - 1;
      continue;
    }
    // a name sits with the foliage it names: a leaf folder's on its clump (above, below, on it), a folder with
    // sub-folders beside the fork where its branches part or along its own stem; a top-level limb may also run
    // along its wood
    const b = n.branch;
    if (!b) continue;
    const big = n.depth === 1;
    g.font = pill ? PILL_FONT : labelFont(P, n);
    const [ex, ey] = toS(b.x1, b.y1);
    const side = b.x1 >= b.x0 ? 1 : -1,
      we = (b.we * z) / 2;
    const clumpT = (n.term as TTerm | undefined) || null;
    // a fork's own wood is its own: a name along its stem is pinned to it (a folder with a clump is pinned to the
    // clump — its leaves are what a reader sees, not the thin twig under them)
    const anchors: Array<[number, number, number]> = [];
    for (let k = 0; k < b.pts.length; k++) {
      const [px, py] = toS(b.pts[k][0], b.pts[k][1]);
      anchors.push([px, py, taperHW(b.wb, b.we, k / Math.max(1, b.pts.length - 1)) * z]);
    }
    let Pl: Placed | null = null,
      nm = name;
    for (const cand1 of names) {
      const bare = !cand1;
      const tw = (bare ? 0 : g.measureText(cand1).width) + segsW(info, bare) + (pill ? 14 : 0);
      const tries: Array<[number, number, number]> = [];
      // along its own stem, on the outer side then the inner: the branch IS the folder, and wood is not foliage
      const stemSpots = () => {
        for (const k of [5, 3, 7, 2, 8]) {
          const p = b.pts[k];
          const [sx, sy] = toS(p[0], p[1]);
          const off = taperHW(b.wb, b.we, k / 10) * z + 5;
          let nx = -p[3],
            ny = p[2];
          if (nx * side < 0) {
            nx = -nx;
            ny = -ny;
          }
          const d = off + Math.abs(nx) * (tw / 2 + 4) + Math.abs(ny) * 10;
          tries.push([sx + nx * d, sy + ny * d, 0], [sx - nx * d, sy - ny * d, 0]);
        }
      };
      let ring: { x: number; y: number; r: number };
      let anc = anchors;
      if (clumpT && clumpT.leaves && clumpT.leaves.length) {
        const [cx, cy] = toS(clumpT.cx, clumpT.cy),
          r = clumpT.rad * z;
        tries.push([cx, cy - r - 8, 0], [cx, cy + r + 9, 0], [cx + r + tw / 2 + 6, cy, 0], [cx - r - tw / 2 - 6, cy, 0], [cx, cy, 0]);
        stemSpots();
        ring = { x: cx, y: cy, r };
        anc = [[cx, cy, r]];
      } else {
        if (big && !pill && cand1 === name) {
          const p = b.pts[8];
          const [sx, sy] = toS(p[0], p[1]);
          let ang = Math.atan2(p[3], p[2]);
          if (ang > Math.PI / 2) ang -= Math.PI;
          if (ang < -Math.PI / 2) ang += Math.PI;
          const slen = b.len * z;
          if (slen > tw * 0.8 && Math.abs(ang) < 0.9) {
            const off = (b.w * z) / 2 + 8;
            let nx = -Math.sin(ang),
              ny = Math.cos(ang);
            if (ny > 0) {
              nx = -nx;
              ny = -ny;
            }
            tries.push([sx + nx * off, sy + ny * off, ang]);
          }
        }
        tries.push([ex + side * (we + tw / 2 + 6), ey - 4, 0], [ex - side * (we + tw / 2 + 6), ey - 4, 0], [ex, ey - we - 12, 0], [ex, ey + we + 12, 0]);
        stemSpots();
        ring = { x: ex, y: ey, r: we + 3 };
      }
      Pl = place(tries, tw + 8, pill ? 24 : 20, n, false, ring, okY, anc);
      if (Pl) {
        nm = cand1;
        break;
      }
    }
    if (!Pl) continue;
    reserve(Pl.R[0], Pl.R[1], Pl.R[2], Pl.R[3]);
    v.labelHits!.push({ r: Pl.R, n });
    leader(Pl, col);
    g.save();
    g.translate(Pl.x, Pl.y);
    g.rotate(Pl.ang);
    g.strokeStyle = P.halo;
    g.lineWidth = 3.5;
    drawNamed(nm, info, 0, 0, col, pill);
    g.restore();
    g.textAlign = "left";
    v.labelBudget = (v.labelBudget || 0) - 1;
  }
  g.globalAlpha = 1;
  // leaf labels when zoomed in
  if (phase === "rest" && leafPx >= 20) {
    const L: Leaf[] = [];
    for (const t of M.terms) {
      if (t.node.fAnc) continue;
      if (!inView(t.cx - t.rad, t.cy - t.rad, t.cx + t.rad, t.cy + t.rad)) continue;
      for (const l of t.leaves) {
        if (l.file.ghost && !createdGhost.has(l.file.id)) continue;
        L.push(l);
      }
    }
    for (const p of M.piles) for (const l of p.leaves || []) if (inView(l.x - 5, l.y - 5, l.x + 5, l.y + 5)) L.push(l);
    if (L.length < 1400) {
      const touched = new Set<number>();
      for (const A of v.agents) {
        for (const k of A.reads.keys()) touched.add(k);
        for (const k of A.edits.keys()) touched.add(k);
        for (const k of A.plan) touched.add(k);
      }
      const gold = new Set<number>();
      for (const bd of v.badges || []) for (const id of bd.ids) gold.add(id);
      const pri = (l: Leaf) => (touched.has(l.file.id) ? 2 : 0) + (gold.has(l.file.id) ? 1 : 0);
      // names only where there is something to see — the clump at the middle of the view (or the selected
      // folder), the leaf under the pointer, and files a bird touched or that depend on an edit; every other file
      // stays a leaf until you are close enough that only a handful are on screen (never a carpet of file names)
      const sparse = leafPx < 64;
      const selN = v.sel && v.sel.node && !v.sel.leaf ? v.sel.node : null;
      const hovL = v.hover && v.hover.leaf ? v.hover.leaf : null;
      let focusT: TTerm | null = null;
      if (sparse) {
        const f = v.free || { x0: 0, y0: 0, x1: W, y1: H },
          fx = ((f.x0 + f.x1) / 2 - W / 2) / z + v.cam.x,
          fy = ((f.y0 + f.y1) / 2 - H / 2) / z + v.cam.y;
        let bd = 1e9;
        for (const t of M.terms) {
          if (!t.leaves.length) continue;
          const d = Math.hypot(t.cx - fx, t.cy - fy) / Math.max(t.rad, S0);
          if (d < 1.6 && d < bd) {
            bd = d;
            focusT = t;
          }
        }
        if (v.sel && v.sel.leaf && v.sel.leaf.term) focusT = v.sel.leaf.term;
        if (selN) focusT = null; // a selected folder is where you are looking
      }
      if (sparse)
        for (let i = L.length - 1; i >= 0; i--) {
          const l = L[i];
          if (!(pri(l) || l === hovL || (focusT && l.term === focusT) || (selN && l.term && isUnder(l.term.node, selN)))) L.splice(i, 1);
        }
      L.sort((a, b) => Number(b === hovL) - Number(a === hovL) || pri(b) - pri(a) || b.file.lines - a.file.lines);
      g.font = `10.5px ${P.font}`;
      g.strokeStyle = P.halo;
      g.lineWidth = 3;
      const named = new Set<Leaf>();
      for (const h of v.labelHits || []) if (h.leaf) named.add(h.leaf); // the priority pass already named these
      let cnt = 0;
      for (const l of L) {
        if (cnt > (sparse ? 30 : 70)) break;
        if (named.has(l)) continue;
        const [sx, sy] = toS(l.x, l.y);
        if (!onScreen(sx, sy, 0)) continue;
        const txt = l.file.name,
          tw = g.measureText(txt).width;
        const off = l.len * z * 0.5 + 3;
        let x0 = sx + off,
          y0 = sy - 7,
          ok = false;
        // right of the leaf, else left of it; a 3 px gutter so neighbours never run together
        const own = l.term ? l.term.node : null;
        for (const [ax, ay] of [
          [off, 0],
          [-off - tw, 0],
          [off, -13],
          [off, 13],
        ]) {
          x0 = sx + ax;
          y0 = sy + ay - 7;
          if (fits(x0 - 3, y0, x0 + tw + 3, y0 + 14) && !(own && fol && fol.hit(x0, y0 + 1, x0 + tw, y0 + 13, own, false))) {
            ok = true;
            break;
          }
        }
        if (!ok) continue;
        reserve(x0 - 3, y0, x0 + tw + 3, y0 + 14);
        v.labelHits!.push({ r: [x0, y0, x0 + tw, y0 + 14], leaf: l });
        const dusk = hasOnly && !(l.term ? l.term.node.lit : l.pile && l.pile.lit);
        g.fillStyle = dusk ? P.duskLeafLabel : l.file.kind === "c" ? P.leafLabel : P.muted;
        halo(txt, x0, y0 + 7);
        cnt++;
      }
    }
  }
}

// ---------- picking (from ui.js) ----------

export function branchNode(b: Branch): TNode | null {
  return b.node || (b.owner && b.owner.depth >= 1 ? b.owner : null);
}
export function hitNode(hit: Hit | null | undefined): TNode | null {
  if (!hit) return null;
  return hit.node || (hit.branch && branchNode(hit.branch)) || hit.clump || hit.pile || null;
}

const inRect = (r: number[], x: number, y: number) => x >= r[0] && x <= r[2] && y >= r[1] && y <= r[3];

/** What is under the screen point (sx, sy), in view coordinates. Screen-space
 * things drawn on top come first: what you can read is what you hit. */
export function pick(v: TreeView, sx: number, sy: number): Hit | null {
  const M = v.M,
    z = v.cam.z;
  const wx = (sx - v.W / 2) / z + v.cam.x,
    wy = (sy - v.H / 2) / z + v.cam.y;
  for (const h of v.tagHits || []) if (inRect(h.r, sx, sy)) return { tag: h.z };
  for (const h of v.birdHits || []) if (inRect(h.r, sx, sy)) return { bird: h.A, pointer: !!h.pointer };
  for (const h of v.badgeHits || []) if (inRect(h.r, sx, sy)) return { badge: h.bd };
  for (const h of v.labelHits || []) if (inRect(h.r, sx, sy)) return h.leaf ? { leaf: h.leaf, label: true } : h.n ? { node: h.n, label: true } : null;
  for (const h of v.nestHits || []) if (inRect(h.r, sx, sy)) return { nest: h.A };
  for (const h of v.budHits || []) if (inRect(h.r, sx, sy)) return { bud: { A: h.A, path: h.path } };
  const r = 12 / z;
  const near = M.grid.near(wx, wy, Math.max(r, S0 * 1.2));
  let best: Hit | null = null,
    bd = 1e9;
  const leafPx = S0 * z;
  const created = new Set<number>(),
    planned = new Set<number>(),
    hot = new Set<number>();
  for (const A of v.agents) {
    for (const id of A.created) created.add(id);
    for (const id of A.plan) planned.add(id);
    for (const id of A.edits.keys()) hot.add(id);
    if (A.file) hot.add(A.file.id);
  }
  // zoomed out, a ground pile is one thing: a click on it opens the pile, not whichever doc happens to be under the cursor
  const pileWhole = leafPx < 12 || v.tool !== "explore";
  for (const it of near) {
    if (!it.leaf) continue;
    const l = it.leaf,
      f = l.file;
    if (f.ghost && !created.has(f.id) && !planned.has(f.id)) continue;
    const own = l.term ? l.term.node : l.pile;
    if (own && own.fAnc) continue;
    if (l.region === "ground" && pileWhole && l.pile) return { pile: l.pile };
    if (l.region !== "ground" && leafPx < 4.2 && !planned.has(f.id) && !hot.has(f.id)) continue;
    let d = Math.hypot(l.x - wx, l.y - wy);
    // a changed leaf is drawn big and bright at every zoom: it is pickable at every zoom too
    const lim = hot.has(f.id) ? Math.max(l.len * 0.9, 11 / z) : Math.max(l.len * 0.65, 7 / z);
    if (d >= lim) continue;
    if (hot.has(f.id)) d *= 0.5;
    else if (planned.has(f.id)) d *= 1.3; // an edited / perched leaf wins over a bud sitting on it
    if (d < bd) {
      bd = d;
      best = { leaf: l };
    }
  }
  if (best) {
    if (v.tool !== "explore" && leafPx < 24 && best.leaf) {
      // paint tools act on folders unless the user has zoomed in on single files
      const l = best.leaf;
      const own = l.term ? l.term.node : l.pile;
      if (own && own.depth >= 1) return { node: own, viaLeaf: l };
    }
    return best;
  }
  for (const b of v.visBranches || []) {
    if (wx < b.bx0 - r || wx > b.bx1 + r || wy < b.by0 - r || wy > b.by1 + r) continue;
    for (const p of b.pts) {
      const d = Math.hypot(p[0] - wx, p[1] - wy);
      const lim = Math.max(b.w * 0.6, 7 / z);
      if (d < lim && d < bd) {
        bd = d;
        best = { branch: b };
      }
    }
  }
  if (best) return best;
  // the trunk is the repo: it has a card like everything else
  {
    const tw = Math.min(M.trunkW, 110 / z);
    if (wy > -S0 && wy < M.groundY + S0 && Math.abs(wx) < Math.max(tw * 0.75 + 6 / z, 10 / z) * (1 + (0.5 * Math.max(0, wy)) / Math.max(1, M.groundY)))
      return { trunk: true };
  }
  // a foliage clump (collapsed or folded) under the cursor
  let tbest: TTerm | null = null;
  let td = 1e9;
  for (const t of M.terms) {
    const d = Math.hypot(t.cx - wx, t.cy - wy);
    if (d < t.rad && d < td) {
      td = d;
      tbest = t;
    }
  }
  if (tbest) {
    const n = tbest.node;
    if (n.fAnc) return { clump: n.fAnc };
    // the shallowest collapsed ancestor on screen
    let q: TNode | null = n,
      top: TNode | null = null;
    while (q && q.depth >= 1) {
      if (q.vColl && !q.vHidden) top = q;
      q = q.parent;
    }
    return { clump: top || n, open: !top };
  }
  for (const p of M.piles) if (Math.abs(wx - p.cx) < (p.pw || 0) * 0.6 && wy < M.groundY + S0 && wy > M.groundY - (p.h || 0) * 2) return { pile: p };
  return null;
}

// ---------- minimap (from ui.js) ----------

interface MiniState {
  W: number;
  H: number;
  z: number;
  ox: number;
  oy: number;
  d: number;
  M: Model;
  pkey: string;
  base: HTMLCanvasElement;
}
const MINI = new WeakMap<HTMLCanvasElement, MiniState>();

/** The minimap's static base (one filled silhouette per clump, the wood, the
 * piles) for the canvas's current CSS size. Returns false while hidden. */
export function buildMini(v: TreeView, canvas: HTMLCanvasElement): boolean {
  const M = v.M,
    P = v.P;
  const r = canvas.getBoundingClientRect();
  const W = Math.max(1, r.width),
    H = Math.max(1, r.height),
    d = 2;
  if (W < 4 || H < 4) {
    MINI.delete(canvas);
    return false; // hidden: build it when it becomes visible
  }
  const cur = MINI.get(canvas);
  if (cur && cur.W === W && cur.H === H && cur.M === M && cur.pkey === P.key) return true;
  canvas.width = Math.round(W * d);
  canvas.height = Math.round(H * d);
  const B = M.bounds;
  const z = Math.min(W / (B.x1 - B.x0), H / (B.y1 - B.y0)) * 0.92;
  const st: MiniState = { W, H, z, ox: W / 2 - ((B.x0 + B.x1) / 2) * z, oy: H / 2 - ((B.y0 + B.y1) / 2) * z, d, M, pkey: P.key, base: document.createElement("canvas") };
  const off = st.base;
  off.width = Math.round(W * d);
  off.height = Math.round(H * d);
  const g = off.getContext("2d");
  if (!g) return false;
  g.fillStyle = P.miniBg;
  g.fillRect(0, 0, W * d, H * d);
  g.setTransform(z * d, 0, 0, z * d, st.ox * d, st.oy * d);
  g.fillStyle = P.miniSoil;
  g.fillRect(B.x0 - 999, M.groundY, B.x1 - B.x0 + 2000, 9999);
  // a filled silhouette per clump (the wide blob, not hairline leaves) so a 4.8k-file tree reads as a shape, not dust
  for (const t0 of M.terms) {
    const t = tg(t0);
    if (!t.blobFold) continue;
    g.fillStyle = t.region === "root" ? P.miniRoot : P.PAL[t.node.limb % P.PAL.length].leafB;
    g.globalAlpha = 0.95;
    g.fill(t.blobFold);
  }
  g.globalAlpha = 1;
  const MG = mg(M);
  if (MG.trunkPath) {
    g.fillStyle = P.BARK.crown;
    g.fill(MG.trunkPath);
  }
  g.lineWidth = Math.max(0.6 / z, 0.3);
  g.lineCap = "round";
  for (const b of M.branches) {
    if (b.w * z > 0.25) {
      g.beginPath();
      const p = b.poly;
      g.moveTo(p[0], p[1]);
      for (let i = 2; i < p.length; i += 2) g.lineTo(p[i], p[i + 1]);
      g.closePath();
      g.fillStyle = b.region === "root" ? P.BARK.root : P.BARK.crown;
      g.fill();
    } else if (b.depth <= 2) {
      g.strokeStyle = b.region === "root" ? P.BARK.root : P.BARK.crown;
      g.beginPath();
      g.moveTo(b.x0, b.y0);
      g.lineTo(b.x1, b.y1);
      g.stroke();
    }
  }
  for (const p of M.piles)
    for (const l of p.leaves || []) {
      g.fillStyle = P.PILEPAL.leafA;
      const s = Math.max(4, 2.5 / z);
      g.fillRect(l.x - s, l.y - s * 0.6, s * 2, s * 1.2);
    }
  MINI.set(canvas, st);
  return true;
}

/** Paint the minimap: the base, the only-here dimming, the viewport box, rule
 * rings, the selection and the birds. */
export function drawMini(v: TreeView, canvas: HTMLCanvasElement, screenToWorld: (sx: number, sy: number) => [number, number]): void {
  if (!canvas.offsetParent) return;
  let m = MINI.get(canvas);
  if (!m || m.M !== v.M || m.pkey !== v.P.key) {
    if (!buildMini(v, canvas)) return;
    m = MINI.get(canvas);
  }
  if (!m) return;
  const P = v.P;
  const g = canvas.getContext("2d");
  if (!g) return;
  const d = m.d;
  g.setTransform(1, 0, 0, 1, 0, 0);
  g.drawImage(m.base, 0, 0);
  g.setTransform(d, 0, 0, d, 0, 0);
  const zoneNode = (z: TZone): TNode | null => z.node || (z.file != null && v.M.files[z.file] ? v.M.files[z.file].node : null);
  if (v.hasOnly) {
    // the minimap dims with the main view
    g.fillStyle = P.miniDim;
    g.fillRect(0, 0, m.W, m.H);
    for (const z of v.zones) {
      if (z.type !== "only" || z.waived) continue;
      const n = zoneNode(z);
      if (!n || !n.cnt) continue;
      g.save();
      g.beginPath();
      g.arc(n.cx * m.z + m.ox, n.cy * m.z + m.oy, Math.max(4, n.rad * m.z), 0, TAU);
      g.clip();
      g.setTransform(1, 0, 0, 1, 0, 0);
      g.drawImage(m.base, 0, 0);
      g.restore();
    }
  }
  const [x0, y0] = screenToWorld(0, 0),
    [x1, y1] = screenToWorld(v.W, v.H);
  g.strokeStyle = P.miniView;
  g.lineWidth = 1;
  g.strokeRect(x0 * m.z + m.ox, y0 * m.z + m.oy, (x1 - x0) * m.z, (y1 - y0) * m.z);
  for (const z of v.zones) {
    const n = zoneNode(z);
    if (!n || !n.cnt) continue;
    g.strokeStyle = z.type === "keep" ? P.red : P.green;
    g.globalAlpha = z.waived ? 0.45 : 1;
    g.lineWidth = 1.5;
    g.beginPath();
    g.arc(n.cx * m.z + m.ox, n.cy * m.z + m.oy, Math.max(3, n.rad * m.z), 0, TAU);
    g.stroke();
    g.globalAlpha = 1;
  }
  if (v.sel && v.sel.leaf) {
    const l = v.sel.leaf;
    g.fillStyle = P.sel;
    g.beginPath();
    g.arc(l.x * m.z + m.ox, l.y * m.z + m.oy, 2.2, 0, TAU);
    g.fill();
  }
  if (v.birdWorld)
    for (const A of v.agents) {
      const p = v.birdWorld[A.ag.key];
      if (!p || (A.ag.parent && A.done)) continue;
      g.fillStyle = A.ag.color;
      g.beginPath();
      g.arc(p[0] * m.z + m.ox, p[1] * m.z + m.oy, A.ag.parent ? 2 : 3, 0, TAU);
      g.fill();
    }
}

/** The minimap point (CSS px inside the canvas) → world. */
export function miniToWorld(canvas: HTMLCanvasElement, mx: number, my: number): [number, number] | null {
  const m = MINI.get(canvas);
  if (!m) return null;
  return [(mx - m.ox) / m.z, (my - m.oy) / m.z];
}
