/** The Sky's geometry and its lines — pure, so the Sky (components/flights/
 * Sky.tsx) and the tests read one rule (docs/flights.md "The Sky").
 *
 * The sky is one plane, panned and zoomed as a whole: a point on it maps to
 * the screen as `plane * z + (x, y)` (one `translate(x, y) scale(z)` on one
 * container). Every session is a LINE on it — its row of blocks — at a
 * position the owner dragged it to (remembered per device, keyed by session
 * title) or, until then, one `placeLines` picks: rows of lines left to right,
 * newest first, wrapping at the sky's width, never on top of another line.
 *
 * Which sessions get a line is the view's (`skyLineTitles`): All (every
 * session, workers too, each on its own line), Mine (top-level sessions, their
 * workers folded into their parent's line), Perched on you (lines waiting on
 * you), This session (one line). Hidden helpers (`hidden_by`, a verify run)
 * never get one. */

import type { Instance } from "../api/types";
import { rowWaitsOnYou, type FlightRow } from "./flights";
import { deriveSessionState } from "./sessionState";

// --- Geometry ------------------------------------------------------------------

export interface Pos {
  x: number;
  y: number;
}

/** A line to place: its session title and its drawn size (plane px). */
export interface LineBox {
  id: string;
  w: number;
  h: number;
}

export interface Rect extends Pos {
  w: number;
  h: number;
}

/** Where auto-placed lines go: from (left, top), wrapping at `width`. */
export interface PlaceArea {
  width: number;
  left?: number;
  top?: number;
}

/** The space kept between two lines. */
export const SKY_GAP = 36;
/** The plane's margin, and the intake lane's width at its left edge. */
export const SKY_MARGIN = 40;
export const INTAKE_W = 300;
/** Where the first auto-placed line starts: right of the intake lane. */
export const LINES_LEFT = SKY_MARGIN + INTAKE_W + 64;

export const ZOOM_MIN = 0.4;
export const ZOOM_MAX = 2;

const hits = (a: Rect, b: Rect, gap: number) =>
  a.x < b.x + b.w + gap && b.x < a.x + a.w + gap && a.y < b.y + b.h + gap && b.y < a.y + a.h + gap;

/** Every line's position. A saved one stays where it was put; the rest flow
 * into rows (in the order given — newest first — so the newest sit at the
 * top), each at the first spot on its row that overlaps nothing, wrapping
 * when the row runs past the area's width. A line wider than the area still
 * gets a row of its own. */
export function placeLines(lines: readonly LineBox[], saved: Readonly<Record<string, Pos>>, area: PlaceArea): Record<string, Pos> {
  const out: Record<string, Pos> = {};
  const taken: Rect[] = [];
  for (const l of lines) {
    const s = saved[l.id];
    if (s && Number.isFinite(s.x) && Number.isFinite(s.y)) {
      out[l.id] = { x: s.x, y: s.y };
      taken.push({ x: s.x, y: s.y, w: l.w, h: l.h });
    }
  }
  const x0 = area.left ?? SKY_MARGIN;
  const y0 = area.top ?? SKY_MARGIN;
  const right = Math.max(area.width, x0 + 1);
  let cx = x0;
  let cy = y0;
  let rowH = 0;
  for (const l of lines) {
    if (out[l.id]) continue;
    for (let guard = 0; guard < 10_000; guard++) {
      if (cx > x0 && cx + l.w > right) {
        cx = x0;
        cy += (rowH || 0) + SKY_GAP;
        rowH = 0;
      }
      const at: Rect = { x: cx, y: cy, w: l.w, h: l.h };
      const hit = taken.find((t) => hits(at, t, SKY_GAP - 1));
      if (hit) {
        cx = hit.x + hit.w + SKY_GAP;
        continue;
      }
      out[l.id] = { x: cx, y: cy };
      taken.push(at);
      cx += l.w + SKY_GAP;
      rowH = Math.max(rowH, l.h);
      break;
    }
  }
  return out;
}

/** The smallest rectangle holding them all, or null for none. */
export function boundsOf(rects: readonly Rect[]): Rect | null {
  if (!rects.length) return null;
  const x = Math.min(...rects.map((r) => r.x));
  const y = Math.min(...rects.map((r) => r.y));
  const w = Math.max(...rects.map((r) => r.x + r.w)) - x;
  const h = Math.max(...rects.map((r) => r.y + r.h)) - y;
  return { x, y, w, h };
}

// --- Pan and zoom --------------------------------------------------------------

/** The camera: screen = plane * z + (x, y). */
export interface SkyCamera {
  x: number;
  y: number;
  z: number;
}

export const clampZoom = (z: number) => Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, Number.isFinite(z) ? z : 1));

/** Zoom to `z` keeping the plane point under the screen point (px, py) where
 * it is — the wheel's and the pinch's zoom. */
export function zoomAt(c: SkyCamera, z: number, px: number, py: number): SkyCamera {
  const nz = clampZoom(z);
  const k = nz / c.z;
  return { z: nz, x: px - (px - c.x) * k, y: py - (py - c.y) * k };
}

/** The plane point under a screen point. */
export function toPlane(c: SkyCamera, px: number, py: number): Pos {
  return { x: (px - c.x) / c.z, y: (py - c.y) / c.z };
}

/** The camera that shows `b` whole in a vw × vh window, centred, never closer
 * than `maxZ` (a lone small line doesn't balloon) nor further than the
 * zoom floor. Nothing to show: the plane's origin at 100%. */
export function fitCamera(b: Rect | null, vw: number, vh: number, maxZ = 1.25, pad = 40): SkyCamera {
  if (!b || b.w <= 0 || b.h <= 0 || vw <= 0 || vh <= 0) return { x: 0, y: 0, z: 1 };
  const z = clampZoom(Math.min(maxZ, (vw - 2 * pad) / b.w, (vh - 2 * pad) / b.h));
  // Wider than the window even at the zoom floor: start at its left edge (the
  // intake lane), not its middle — centring would cut both ends off.
  const x = b.w * z + 2 * pad <= vw ? (vw - b.w * z) / 2 - b.x * z : pad - b.x * z;
  // Taller than the window even at the floor: start at its top, not its middle.
  const y = b.h * z + 2 * pad <= vh ? (vh - b.h * z) / 2 - b.y * z : pad - b.y * z;
  return { x, y, z };
}

/** The camera that brings one rectangle to the middle of the window, at `z`. */
export function centerOn(r: Rect, vw: number, vh: number, z: number): SkyCamera {
  const nz = clampZoom(z);
  return { z: nz, x: vw / 2 - (r.x + r.w / 2) * nz, y: Math.max(24, vh / 2 - (r.h * nz) / 2) - r.y * nz };
}

// --- Remembered positions (per device) ------------------------------------------

export const SKY_POS_KEY = "mf_sky_pos";
/** The most lines remembered; the oldest drop off past it. */
const POS_CAP = 400;

type Store = Pick<Storage, "getItem" | "setItem">;
const local = (): Store | null => {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
};

/** `{ [session title]: { x, y } }`, whatever was stored cleaned of anything
 * that isn't a pair of finite numbers. */
export function readSkyPos(raw: string | null | undefined): Record<string, Pos> {
  const out: Record<string, Pos> = {};
  if (!raw) return out;
  let doc: unknown;
  try {
    doc = JSON.parse(raw);
  } catch {
    return out;
  }
  if (!doc || typeof doc !== "object" || Array.isArray(doc)) return out;
  for (const [k, v] of Object.entries(doc as Record<string, unknown>)) {
    const p = v as Partial<Pos> | null;
    if (k && p && typeof p === "object" && Number.isFinite(p.x) && Number.isFinite(p.y)) out[k] = { x: Math.round(p.x!), y: Math.round(p.y!) };
  }
  return out;
}

export function loadSkyPos(store: Store | null = local()): Record<string, Pos> {
  try {
    return readSkyPos(store?.getItem(SKY_POS_KEY));
  } catch {
    return {};
  }
}

/** Remember one line's spot (a drop, a new session made where its card was);
 * returns the new map. Never throws: a private window just doesn't remember. */
export function rememberSkyPos(all: Readonly<Record<string, Pos>>, title: string, p: Pos, store: Store | null = local()): Record<string, Pos> {
  const next: Record<string, Pos> = { ...all };
  delete next[title];
  next[title] = { x: Math.round(p.x), y: Math.round(p.y) };
  const keys = Object.keys(next);
  for (const k of keys.slice(0, Math.max(0, keys.length - POS_CAP))) delete next[k];
  try {
    store?.setItem(SKY_POS_KEY, JSON.stringify(next));
  } catch {
    /* storage full or blocked: the spot lasts this visit */
  }
  return next;
}

// --- Which sessions get a line ------------------------------------------------------

export type SkyMode = "all" | "mine" | "perched" | "one";

export const SKY_MODES: ReadonlyArray<{ id: SkyMode; label: string; title: string }> = [
  { id: "all", label: "All", title: "Every session on its own line, workers too" },
  { id: "mine", label: "Mine", title: "Your top-level sessions, their workers folded into their line" },
  { id: "perched", label: "Perched on you", title: "Only the lines waiting on you" },
  { id: "one", label: "This session", title: "One line, filling the sky" },
];

/** Drawn in the sky at all: not a hidden helper (a verify run). */
export const inSky = (r: Pick<Instance, "hidden_by">) => !r.hidden_by;

/** The line waits on you: its flight is perched on you or halted, or the
 * session itself needs you (a question, a failed hook). */
export function linePerched(r: Instance): boolean {
  const fl = r.flight as FlightRow | null | undefined;
  if (rowWaitsOnYou(fl) || fl?.state === "halted") return true;
  return deriveSessionState(r).state === "needs_you";
}

/** A line draws its family (parent above, workers below) in the views where a
 * worker has no line of its own. */
export const lineFamily = (mode: SkyMode) => mode === "mine" || mode === "one";

/** The sessions that get a line, newest first (the rows' own order is oldest
 * first). `focus` is This session's title; `query` filters by name or task. */
export function skyLineTitles(rows: readonly Instance[], mode: SkyMode, opts: { query?: string; focus?: string } = {}): string[] {
  const shown = rows.filter(inSky);
  if (mode === "one") return shown.some((r) => r.title === opts.focus) ? [opts.focus!] : [];
  const titles = new Set(shown.map((r) => r.title));
  // A worker whose parent isn't drawn is top-level here (orphaned, or its
  // parent is a hidden helper).
  const top = (r: Instance) => !r.parent || !titles.has(r.parent);
  let pick = mode === "all" ? shown : mode === "mine" ? shown.filter(top) : shown.filter(linePerched);
  const q = String(opts.query || "").trim().toLowerCase();
  if (q) pick = pick.filter((r) => r.title.toLowerCase().includes(q) || String(r.task_headline || r.task || "").toLowerCase().includes(q));
  return pick.map((r) => r.title).reverse();
}

// --- The address ------------------------------------------------------------------

/** The Flights target that opens the sky on one session (`#flights/sky/<title>`). */
export const skyTarget = (title = "") => (title ? "sky/" + title : "sky");
