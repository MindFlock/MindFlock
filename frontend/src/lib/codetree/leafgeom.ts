/** Code tree — one leaf's geometry, in the world and on screen.
 *
 * The rules (pinned by __tests__/codetreeLeaves.test.ts):
 *  - a crown clump packs its leaves at pitch `LEAF.pitch·S0`, and no leaf is longer than `pitch / LEAF.room`:
 *    every leaf fits in a disc around its centre that no neighbour's disc reaches, so outlines never cross;
 *  - a leaf points away from its twig's tip (a fan opening along the twig), with only a few degrees of
 *    jitter: neighbours run near-parallel and every tip faces open air, where a name can start;
 *  - on screen a leaf grows with the zoom only up to `LEAF.capFrom` px, then eases toward `LEAF.capTo` px
 *    and stops: zooming in further opens the gaps instead of inflating the leaves. Its BASE stays put (the
 *    stalk ends there); the drawn centre and tip slide toward it.
 *
 * Every consumer of a leaf's drawn size (outline, rings, pick radius, the perch, edit leaves, names at the tip)
 * goes through `leafScreen` / `leafOutline`, so they agree at every zoom. */

import type { Leaf } from "./model";

export const LEAF = {
  /** crown clump hex pitch, × S0 (was 0.92) */
  pitch: 1.15,
  /** pitch / longest leaf: the free ring around every leaf's disc */
  room: 1.22,
  /** leaf length range as a fraction of the longest (small file … big file) */
  minFrac: 0.55,
  /** clump lattice jitter, × pitch (each axis, ±half of it) */
  jitPos: 0.08,
  /** leaf angle jitter (radians, ±half of it) */
  jitAng: 0.12,
  /** on-screen leaf length (px): linear up to capFrom, then easing toward capTo */
  capFrom: 24,
  capTo: 44,
  /** visible half-width / length (a lens whose control points sit 0.37·len off-axis) */
  halfW: 0.185,
};

/** A crown leaf's world length from its file size (log lines), at clump pitch `s`. */
export function leafLen(lines: number, s: number): number {
  const fz = Math.min(1, Math.log10(Math.max(1, lines) + 1) / 3.6);
  const max = s / LEAF.room;
  return max * (LEAF.minFrac + (1 - LEAF.minFrac) * fz);
}

/** A crown leaf's pointing direction: away from the twig tip (bx, by), the twig heading `hb` for the leaves
 * that sit on the tip itself; `jit` in [0, 1) is the leaf's own seeded jitter. */
export function leafAng(x: number, y: number, bx: number, by: number, hb: number, s: number, jit: number): number {
  const d = Math.hypot(x - bx, y - by);
  const ang = d < s * 0.6 ? hb : Math.atan2(y - by, x - bx);
  return ang + (jit - 0.5) * LEAF.jitAng;
}

/** Drawn length in screen px of a leaf `lenPx` px long at true scale (monotone, C1, ≤ capTo). */
export function capPx(lenPx: number): number {
  const a = LEAF.capFrom,
    b = LEAF.capTo;
  if (lenPx <= a) return lenPx;
  return a + (b - a) * (1 - Math.exp(-(lenPx - a) / (b - a)));
}

export interface LeafScreen {
  bx: number;
  by: number;
  tx: number;
  ty: number;
  cx: number;
  cy: number;
  dir: number;
  halfPx: number;
  wPx: number;
}

/** base, tip, centre and pointing direction (world units; dir = radians), plus the drawn half-length
 *  and half-width in SCREEN px at zoom z (after any on-screen cap). Ground (pile) leaves are never capped. */
export function leafScreen(l: Leaf, z: number): LeafScreen {
  const ca = Math.cos(l.ang),
    sa = Math.sin(l.ang);
  const full = l.len * z;
  const px = l.region === "ground" ? full : capPx(full);
  const len = z > 0 ? px / z : l.len;
  const bx = l.x - ca * l.len * 0.5,
    by = l.y - sa * l.len * 0.5;
  return {
    bx,
    by,
    tx: bx + ca * len,
    ty: by + sa * len,
    cx: bx + ca * len * 0.5,
    cy: by + sa * len * 0.5,
    dir: l.ang,
    halfPx: px * 0.5,
    wPx: px * LEAF.halfW,
  };
}

/** The lens outline [bx,by, c1x,c1y, tx,ty, c2x,c2y] (world units) of a leaf `len` long from its base. */
export function lensAt(l: Leaf, len: number): number[] {
  const ca = Math.cos(l.ang),
    sa = Math.sin(l.ang);
  const w = len * (l.region === "root" ? 0.5 : l.region === "ground" ? 0.3 : 0.37);
  const bx = l.x - ca * l.len * 0.5,
    by = l.y - sa * l.len * 0.5,
    tx = bx + ca * len,
    ty = by + sa * len;
  const mx = bx + ca * len * 0.54,
    my = by + sa * len * 0.54;
  return [bx, by, mx - sa * w, my + ca * w, tx, ty, mx + sa * w, my - ca * w];
}

/** The outline as drawn at zoom z: `l.g` (true size) until the on-screen cap bites, then a shorter lens on the
 * same base. */
export function leafOutline(l: Leaf, z: number): number[] {
  if (l.region === "ground" || l.len * z <= LEAF.capFrom) return l.g || lensAt(l, l.len);
  return lensAt(l, capPx(l.len * z) / z);
}
