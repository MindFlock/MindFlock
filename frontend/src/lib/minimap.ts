/** The minimap's and the off-screen hints' pure maths (docs/flights.md
 * "Round 17": task `minimap`). Plane px are the sky's own coordinates; the
 * camera maps them to the screen (screen = plane × z + (x, y), lib/skyLayout's
 * `SkyCamera`); the map draws the whole plane scaled into a small box
 * (map = plane × scale + offset). Nothing here touches the DOM. */

import { boundsOf, clampZoom, type SkyCamera } from "./skyLayout";

export interface MapRect {
  x: number;
  y: number;
  w: number;
  h: number;
}

/** The map's own size, px (the body under the head row). */
export const MINIMAP_W = 180;
export const MINIMAP_H = 120;
/** The breathing room round the drawn plane inside the map. */
export const MINIMAP_PAD = 8;
/** The sky remembers the fold under this key ("1" open, "0" folded). */
export const MINIMAP_KEY = "mf_sky_minimap";
/** Open unless the owner folded it. */
export const minimapOpen = (pref: string | null | undefined): boolean => pref !== "0";

/** How the plane sits in the map: map = plane × scale + (ox, oy). */
export interface MapFit {
  scale: number;
  ox: number;
  oy: number;
}

/** The fit that draws every rect inside a `map`-sized box with `pad` px
 * round it, centred, one scale for both axes (a plane bigger than the map
 * shrinks; a small one is drawn no larger than 1:1). Nothing to draw: 1:1
 * with the plane's origin at the pad corner. */
export function mapScale(rects: readonly MapRect[], map: { w: number; h: number }, pad = MINIMAP_PAD): MapFit {
  const b = boundsOf(rects);
  if (!b) return { scale: 1, ox: pad, oy: pad };
  const aw = Math.max(1, map.w - 2 * pad);
  const ah = Math.max(1, map.h - 2 * pad);
  const scale = Math.min(1, aw / Math.max(1, b.w), ah / Math.max(1, b.h));
  return { scale, ox: (map.w - b.w * scale) / 2 - b.x * scale, oy: (map.h - b.h * scale) / 2 - b.y * scale };
}

/** A plane point on the map. */
export const toMap = (fit: MapFit, p: { x: number; y: number }) => ({ x: p.x * fit.scale + fit.ox, y: p.y * fit.scale + fit.oy });

/** A map point on the plane. */
export const fromMap = (fit: MapFit, p: { x: number; y: number }) => ({ x: (p.x - fit.ox) / fit.scale, y: (p.y - fit.oy) / fit.scale });

/** A plane rect on the map. */
export function rectToMap(fit: MapFit, r: MapRect): MapRect {
  const a = toMap(fit, r);
  return { x: a.x, y: a.y, w: r.w * fit.scale, h: r.h * fit.scale };
}

/** What the camera shows, in plane px. */
export function viewportRect(cam: SkyCamera, viewport: { w: number; h: number }): MapRect {
  const z = cam.z > 0 ? cam.z : 1;
  return { x: 0 - cam.x / z, y: 0 - cam.y / z, w: viewport.w / z, h: viewport.h / z };
}

/** The camera (same zoom) that centres the plane point `p` in the viewport. */
export function centreAt(p: { x: number; y: number }, viewport: { w: number; h: number }, z: number): SkyCamera {
  const nz = clampZoom(z);
  return { z: nz, x: viewport.w / 2 - p.x * nz, y: viewport.h / 2 - p.y * nz };
}

/** The camera after the viewport rectangle was dragged `dx`, `dy` MAP px
 * from where it was when the drag began at `cam0`. */
export function dragCamera(cam0: SkyCamera, fit: MapFit, dx: number, dy: number): SkyCamera {
  const k = cam0.z / (fit.scale || 1);
  return { z: cam0.z, x: cam0.x - dx * k, y: cam0.y - dy * k };
}

// --- The blocks ------------------------------------------------------------------

/** The kinds drawn as a thin bar rather than a block. */
export const BAR_KINDS: ReadonlySet<string> = new Set(["intake", "landing"]);
/** The kinds that are lines (state-toned blocks, off-screen hints). */
export const LINE_KINDS: ReadonlySet<string> = new Set(["line", "grounded"]);
/** A bar's thickness and a block's smallest side, map px. */
export const BAR_PX = 3;
export const MIN_BLOCK = 3;

export interface MapBlock extends MapRect {
  id: string;
  kind: string;
  bar: boolean;
}

/** Every rect as the map draws it: lines and objects as blocks no smaller
 * than `MIN_BLOCK`, the intake lane and the landing strip as a thin bar along
 * their long side. Objects first, then lines, so a line wins a shared pixel. */
export function mapBlocks(rects: readonly (MapRect & { id: string; kind: string })[], fit: MapFit): MapBlock[] {
  const out = rects.map((r) => {
    const m = rectToMap(fit, r);
    const bar = BAR_KINDS.has(r.kind);
    if (bar) {
      if (m.h >= m.w) return { id: r.id, kind: r.kind, bar, x: m.x + m.w / 2 - BAR_PX / 2, y: m.y, w: BAR_PX, h: Math.max(MIN_BLOCK, m.h) };
      return { id: r.id, kind: r.kind, bar, x: m.x, y: m.y + m.h / 2 - BAR_PX / 2, w: Math.max(MIN_BLOCK, m.w), h: BAR_PX };
    }
    return { id: r.id, kind: r.kind, bar, x: m.x, y: m.y, w: Math.max(MIN_BLOCK, m.w), h: Math.max(MIN_BLOCK, m.h) };
  });
  const rank = (b: MapBlock) => (LINE_KINDS.has(b.kind) ? 2 : b.bar ? 0 : 1);
  return out.map((b, i) => ({ b, i })).sort((p, q) => rank(p.b) - rank(q.b) || p.i - q.i).map((p) => p.b);
}

/** The block under a map point (the top one, as drawn), with a little slop. */
export function pickBlock(blocks: readonly MapBlock[], mx: number, my: number, slop = 2): string {
  for (let i = blocks.length - 1; i >= 0; i--) {
    const b = blocks[i];
    if (mx >= b.x - slop && mx <= b.x + b.w + slop && my >= b.y - slop && my <= b.y + b.h + slop) return b.id;
  }
  return "";
}

/** A point inside a rect. */
export const inside = (r: MapRect, x: number, y: number) => x >= r.x && x <= r.x + r.w && y >= r.y && y <= r.y + r.h;

// --- Tones ---------------------------------------------------------------------------

/** A state tone's colour token: the six session tones (styles/tokens.css),
 * read the way the birds read them — working green, needs-you gold, a usage
 * wait blue, halted red, idle/done neutral, the rest muted. */
export function toneVar(tone: string | undefined): string {
  switch (tone || "") {
    case "working":
    case "running":
      return "var(--tone-working)";
    case "asking":
    case "waiting_you":
    case "you":
    case "attention":
    case "needs_input":
      return "var(--tone-attention)";
    case "waiting":
    case "limited":
      return "var(--tone-waiting)";
    case "halted":
    case "broken":
    case "failed":
    case "error":
    case "offline":
      return "var(--tone-broken)";
    case "idle":
    case "done":
    case "neutral":
      return "var(--tone-neutral)";
    default:
      return "var(--tone-muted)";
  }
}

/** A title cut to `n` characters, an ellipsis standing in for the rest. */
export function shortLabel(label: string | undefined, n = 14): string {
  const s = (label || "").trim();
  return [...s].length > n ? [...s].slice(0, n - 1).join("").trimEnd() + "…" : s;
}

// --- Off-screen hints ---------------------------------------------------------------

/** A hint chip's height and the room it keeps from the edge, screen px. */
export const HINT_H = 22;
export const HINT_INSET = 8;
/** A hint chip's widest (CSS max-width), for keeping a top/bottom chip on screen. */
export const HINT_MAX_W = 140;
/** At most this many hints at once. */
export const HINT_MAX = 8;

export type HintEdge = "left" | "right" | "top" | "bottom";

export interface EdgeHint {
  id: string;
  edge: HintEdge;
  /** The chip's anchor: on its edge's side, `HINT_INSET` in from the screen's
   * edge (x for left/right, y for top/bottom), its middle along the edge. */
  x: number;
  y: number;
  /** How far outside the viewport the rect lies, screen px. */
  dist: number;
}

/** For each rect wholly outside the viewport, where its chip goes: pinned to
 * the edge it lies beyond (the further axis when it is past a corner), at
 * the rect's middle along that edge, clamped `HINT_INSET` px in so the chip
 * stays whole; the nearest `max` first; chips on one edge pushed apart so
 * none covers another. */
export function edgeHints(
  rects: readonly (MapRect & { id: string })[],
  cam: SkyCamera,
  viewport: { w: number; h: number },
  max = HINT_MAX,
): EdgeHint[] {
  const { w: vw, h: vh } = viewport;
  if (vw <= 0 || vh <= 0 || max <= 0) return [];
  const clamp = (v: number, lo: number, hi: number) => (lo > hi ? (lo + hi) / 2 : Math.min(hi, Math.max(lo, v)));
  const yLo = HINT_INSET + HINT_H / 2;
  const yHi = vh - HINT_INSET - HINT_H / 2;
  const xLo = HINT_INSET + HINT_MAX_W / 2;
  const xHi = vw - HINT_INSET - HINT_MAX_W / 2;
  const out: EdgeHint[] = [];
  for (const r of rects) {
    const sx = r.x * cam.z + cam.x;
    const sy = r.y * cam.z + cam.y;
    const sw = r.w * cam.z;
    const sh = r.h * cam.z;
    const gapL = -(sx + sw);
    const gapR = sx - vw;
    const gapT = -(sy + sh);
    const gapB = sy - vh;
    const gx = Math.max(0, gapL, gapR);
    const gy = Math.max(0, gapT, gapB);
    if (gx <= 0 && gy <= 0) continue; // some of it is on screen
    const cx = sx + sw / 2;
    const cy = sy + sh / 2;
    let h: EdgeHint;
    if (gx >= gy) {
      const edge: HintEdge = gapL > 0 ? "left" : "right";
      h = { id: r.id, edge, x: edge === "left" ? HINT_INSET : vw - HINT_INSET, y: clamp(cy, yLo, yHi), dist: Math.hypot(gx, gy) };
    } else {
      const edge: HintEdge = gapT > 0 ? "top" : "bottom";
      h = { id: r.id, edge, x: clamp(cx, xLo, xHi), y: edge === "top" ? HINT_INSET : vh - HINT_INSET, dist: Math.hypot(gx, gy) };
    }
    out.push(h);
  }
  out.sort((a, b) => a.dist - b.dist || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  const kept = out.slice(0, max);
  // Keep the chips on one edge from stacking: walk them along the edge and
  // push each clear of the one before, then back inside if the run overflowed.
  for (const edge of ["left", "right", "top", "bottom"] as const) {
    const side = kept.filter((k) => k.edge === edge);
    if (side.length < 2) continue;
    const vertical = edge === "left" || edge === "right";
    const key = vertical ? "y" : "x";
    const gap = vertical ? HINT_H + 4 : HINT_MAX_W + 6;
    const hi = vertical ? yHi : xHi;
    const lo = vertical ? yLo : xLo;
    side.sort((a, b) => a[key] - b[key]);
    for (let i = 1; i < side.length; i++) side[i][key] = Math.max(side[i][key], side[i - 1][key] + gap);
    const over = side[side.length - 1][key] - hi;
    if (over > 0) for (const s of side) s[key] = Math.max(lo, s[key] - over);
  }
  return kept;
}
