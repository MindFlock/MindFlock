/** The Sky's geometry and its lines — pure, so the sky view (components/
 * flights/SkyView.tsx) and the tests read one rule (docs/flights.md "The Sky").
 *
 * The sky is one plane, panned and zoomed as a whole: a point on it maps to
 * the screen as `plane * z + (x, y)` (one `translate(x, y) scale(z)` on one
 * container). Every session is a LINE on it — its row of blocks — at a
 * position the owner dragged it to (pinned, in the per-device layout store
 * `mf_sky_layout` with the line's size and its open box, keyed by session
 * title) or, until then or after a Tidy, one `assignSpots` gives it ONCE when
 * it first appears: rows of lines left to right, newest first, wrapping at
 * the sky's width, never on top of another line. It keeps that spot while it
 * grows or shrinks; only Tidy places it again.
 *
 * Which sessions get a line is the view's (`skyLineTitles`): All and Mine
 * (every session), Perched on you (lines waiting on you), This session (the
 * focused session's line alone). In every view but This session a worker whose
 * lead has a line is drawn INSIDE that line (a branch, its own bird), never
 * as a line of its own (round 12). Hidden helpers (`hidden_by`, a verify run)
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
export const SKY_GAP = 64;
/** The plane's margin, and the intake lane's width at its left edge. */
export const SKY_MARGIN = 40;
export const INTAKE_W = 240;
/** Where the first auto-placed line starts: right of the intake lane. */
export const LINES_LEFT = SKY_MARGIN + INTAKE_W + 48;

/** The intake lane is a sky object like a line (docs/flights.md "As built:
 * round 12 sky"): its layout entry is this key (x / y / pinned, `open` =
 * expanded), it is placed first, before every line, and drags by its head. */
export const INTAKE_KEY = "intake";
/** The expanded lane's width: every source's items in full. */
export const INTAKE_OPEN_W = 420;

/** The intake lane's size before it has measured itself: its width, folded
 * or expanded, and a guess at its height. Once measured, its measure. */
export const INTAKE_MIN_W = 200;
export const INTAKE_MIN_H = 160;
export const INTAKE_MAX_W = 1200;
export const INTAKE_MAX_H = 1600;

/** A sky object's frame when the owner dragged its corner (`w`/`h` on its
 * layout entry), clamped to `min`–`max`; null when it sizes itself. The
 * intake lane's and the floating palette's (docs/flights.md "As built:
 * round 13 sky"). */
export function objectFrame(
  stored: StoredLine | undefined,
  min: { w: number; h: number },
  max: { w: number; h: number }
): { w: number; h: number } | null {
  if (!stored || !Number.isFinite(stored.w) || !Number.isFinite(stored.h)) return null;
  return {
    w: Math.min(max.w, Math.max(min.w, Math.round(stored.w as number))),
    h: Math.min(max.h, Math.max(min.h, Math.round(stored.h as number))),
  };
}

/** The lane's frame when the owner dragged its corner, clamped; null when it
 * sizes itself. */
export function intakeFrame(stored: StoredLine | undefined): { w: number; h: number } | null {
  return objectFrame(stored, { w: INTAKE_MIN_W, h: INTAKE_MIN_H }, { w: INTAKE_MAX_W, h: INTAKE_MAX_H });
}

/** The floating palette (layout key "palette", SkyPalette): its size before
 * it measured itself, and the range its corner sizes it in. */
export const PALETTE_W = 360;
export const PALETTE_H = 300;
export const PALETTE_MIN = { w: 220, h: 140 };
export const PALETTE_MAX = { w: 1400, h: 1200 };
export const paletteFrame = (stored: StoredLine | undefined) => objectFrame(stored, PALETTE_MIN, PALETTE_MAX);

/** The room kept round a block placed by `spotNear`. */
export const PLACE_GAP = 24;

/** Round 14: "All blocks…" floats the palette LARGE when the owner never
 * sized it — about 60% of the screen's width × 70% of its height (`screen`,
 * px), in plane px at zoom `z`, held to the palette's limits and to the view
 * (`view`: the visible plane), its left edge right of the view's centre unless
 * that would push it out, centred top to bottom. `size`: the frame the owner
 * already gave it (kept; only the spot is chosen). `pad` is screen px. */
export function catalogFrame(view: Rect, screen: { w: number; h: number }, z: number, size?: { w: number; h: number } | null, pad = 16): Rect {
  const k = z > 0 ? z : 1;
  const m = pad / k;
  const fit = (want: number, room: number, lo: number, hi: number) => Math.round(Math.min(hi, Math.max(lo, Math.min(want, room))));
  // A frame the owner gave it stays; only where it sits is chosen.
  const w = size ? size.w : fit((screen.w * 0.6) / k, view.w - 2 * m, PALETTE_MIN.w, PALETTE_MAX.w);
  const h = size ? size.h : fit((screen.h * 0.7) / k, view.h - 2 * m, PALETTE_MIN.h, PALETTE_MAX.h);
  const x = Math.max(view.x + m, Math.min(view.x + view.w / 2 + PLACE_GAP, view.x + view.w - m - w));
  const y = Math.max(view.y + m, view.y + (view.h - h) / 2);
  return { x: Math.round(x), y: Math.round(y), w, h };
}

/** The landing strip (LandingStrip, layout key "landing", docs/flights.md "As
 * built: round 15 objects"): placed right of every line, dragged by its head,
 * sized by its corner in this range; its size before it measured itself. */
export const LANDING_KEY = "landing";
export const LANDING_W = 300;
export const LANDING_H = 240;
export const LANDING_MIN = { w: 220, h: 120 };
export const LANDING_MAX = { w: 1200, h: 1600 };
export const landingFrame = (stored: StoredLine | undefined) => objectFrame(stored, LANDING_MIN, LANDING_MAX);

/** A sticky note (StickyNote, layout key "note:<id>"): its size when made,
 * and the range its corner sizes it in. */
export const NOTE_W = 200;
export const NOTE_H = 120;
export const NOTE_MIN = { w: 120, h: 70 };
export const NOTE_MAX = { w: 800, h: 800 };
export const noteFrame = (stored: StoredLine | undefined) => objectFrame(stored, NOTE_MIN, NOTE_MAX);

/** A people / device block's size (PeopleBlocks, DeviceBlocks). */
export const BLOCK_W = 200;
export const BLOCK_H = 64;

/** Where the objects placed RIGHT of every line go (the landing strip, then
 * the device and people blocks): one column just right of everything already
 * placed (`placed`: every line and object with a spot, at its size), `gap`
 * clear of it, stacked from `top` with half a gap between. With nothing
 * placed, the column starts where the first line would. The column's x is
 * read once per newcomer — its spot is kept like a line's, so a line that
 * grows or comes later never shoves it. */
export function rightColumn(placed: readonly Rect[], items: readonly LineBox[], top = SKY_MARGIN, gap = SKY_GAP): Record<string, Pos> {
  const right = placed.reduce((m, r) => Math.max(m, r.x + r.w), 0);
  const x = placed.length ? right + gap : LINES_LEFT;
  const out: Record<string, Pos> = {};
  let y = top;
  for (const it of items) {
    out[it.id] = { x, y };
    y += it.h + gap / 2;
  }
  return out;
}

/** Where a block of `size` lands when the palette places it (docs/flights.md
 * "Round 13": place, don't tack): the free spot nearest `want` (its top-left
 * with nothing in the way) that overlaps nothing `taken` (with PLACE_GAP
 * round it) and, when `within` is given (the view, plane px), fits inside it.
 * The search walks rings of PLACE_GAP steps out from `want`; with no free
 * spot in the view, the nearest free one anywhere; with none at all, `want`. */
export function spotNear(want: Pos, taken: readonly Rect[], size: { w: number; h: number }, within?: Rect | null): Pos {
  const step = PLACE_GAP;
  const free = (x: number, y: number) => !taken.some((t) => hits({ x, y, w: size.w, h: size.h }, t, PLACE_GAP - 1));
  const inside = (x: number, y: number) =>
    !within || (x >= within.x && y >= within.y && x + size.w <= within.x + within.w && y + size.h <= within.y + within.h);
  const search = (bounded: boolean): Pos | null => {
    for (let r = 0; r <= 80; r++) {
      let best: Pos | null = null;
      let bestD = Infinity;
      for (let i = -r; i <= r; i++)
        for (let j = -r; j <= r; j++) {
          if (Math.max(Math.abs(i), Math.abs(j)) !== r) continue;
          const x = Math.round(want.x + i * step);
          const y = Math.round(want.y + j * step);
          if (bounded && !inside(x, y)) continue;
          const d = Math.hypot(i, j);
          if (d < bestD && free(x, y)) {
            best = { x, y };
            bestD = d;
          }
        }
      if (best) return best;
    }
    return null;
  };
  return (within && search(true)) || search(false) || { x: Math.round(want.x), y: Math.round(want.y) };
}

export function intakeSize(stored: StoredLine | undefined, measured?: { w: number; h: number }): { w: number; h: number } {
  const frame = intakeFrame(stored);
  if (frame) return frame;
  if (measured && measured.w > 0 && measured.h > 0) return measured;
  return stored?.open ? { w: INTAKE_OPEN_W, h: 520 } : { w: INTAKE_W, h: 320 };
}

/** The zoom's range: near has more to show, so it goes past 200%. */
export const ZOOM_MIN = 0.1;
export const ZOOM_MAX = 3;
/** The room a fit keeps round what it shows (screen px). */
export const FIT_PAD = 56;

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

/** A line that already has its spot: where it sits, at its size now. */
export interface PlacedLine extends Rect {
  id: string;
}

/** Spots for the lines that have none yet — and only for them. A line keeps
 * the spot it was given (its auto spot, or where it was dropped) for as long
 * as it is in the sky, whatever its size does later; a newcomer gets the
 * first free place by `placeLines`' rule, clear of every placed line at its
 * size now. Two placed lines may later grow into each other: the owner drags
 * one, or Tidies, rather than the sky shoving them about. */
export function assignSpots(existing: readonly PlacedLine[], newcomers: readonly LineBox[], area: PlaceArea): Record<string, Pos> {
  const saved: Record<string, Pos> = {};
  for (const e of existing) saved[e.id] = { x: e.x, y: e.y };
  const fresh = newcomers.filter((n) => !saved[n.id]);
  if (!fresh.length) return {};
  const all = placeLines([...existing, ...fresh], saved, area);
  return Object.fromEntries(fresh.map((n) => [n.id, all[n.id]]));
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
export function fitCamera(b: Rect | null, vw: number, vh: number, maxZ = 1.25, pad = FIT_PAD): SkyCamera {
  if (!b || b.w <= 0 || b.h <= 0 || vw <= 0 || vh <= 0) return { x: 0, y: 0, z: 1 };
  const z = clampZoom(Math.min(maxZ, (vw - 2 * pad) / b.w, (vh - 2 * pad) / b.h));
  // Wider than the window even at the zoom floor: start at its left edge (the
  // intake lane), not its middle — centring would cut both ends off.
  const x = b.w * z + 2 * pad <= vw ? (vw - b.w * z) / 2 - b.x * z : pad - b.x * z;
  // Taller than the window even at the floor: start at its top, not its middle.
  const y = b.h * z + 2 * pad <= vh ? (vh - b.h * z) / 2 - b.y * z : pad - b.y * z;
  return { x, y, z };
}

/** The first picture (a first visit, a new view, `0`): everything when it
 * fits at a legible zoom (no further than far), else the drawn content's
 * WIDTH at no less than `LOD_MID` — anchored top-left, so the intake lane and
 * the newest lines are on screen and the owner pans down to the rest. The
 * toolbar's Fit is the true fit (`fitCamera`): everything, at any zoom. */
export function firstFit(b: Rect | null, vw: number, vh: number, maxZ = 1, pad = FIT_PAD): SkyCamera {
  const all = fitCamera(b, vw, vh, maxZ, pad);
  if (!b || b.w <= 0 || b.h <= 0 || vw <= 0 || vh <= 0 || all.z >= LOD_MID) return all;
  const z = clampZoom(Math.min(maxZ, Math.max(LOD_MID, (vw - 2 * pad) / b.w)));
  return { z, x: pad - b.x * z, y: pad - b.y * z };
}

/** The camera that brings one rectangle to the middle of the window, at `z`. */
export function centerOn(r: Rect, vw: number, vh: number, z: number): SkyCamera {
  const nz = clampZoom(z);
  return { z: nz, x: vw / 2 - (r.x + r.w / 2) * nz, y: Math.max(24, vh / 2 - (r.h * nz) / 2) - r.y * nz };
}

// --- Semantic zoom ----------------------------------------------------------------

/** How much a box shows at the camera's zoom (its level of detail). The
 * geometry never depends on it — a box and a line keep their plane size at
 * every zoom (the fit reads measured sizes) — only what is drawn inside, and
 * the type's size on screen: far = the icon and the title, large enough to
 * read; mid = today's box; near = the title, the summary, the detail, the
 * reason and the in-box parts, in small plane type so a zoomed-in box holds
 * more words in the same room. */
export type Lod = "far" | "mid" | "near";
/** Below this, far; from it, mid. */
export const LOD_MID = 0.62;
/** From this, near. */
export const LOD_NEAR = 1.15;

export const lodFor = (z: number): Lod => (!(z >= LOD_MID) ? "far" : z < LOD_NEAR ? "mid" : "near");

/** The lines of a near box's longer texts (its summary, its detail): more the
 * closer the camera — the plane type shrinks as the zoom grows, so the box's
 * room holds more of them. 0 outside near. */
export const nearLines = (z: number): number => (lodFor(z) === "near" ? Math.max(2, Math.min(8, Math.floor(z * 2))) : 0);

// --- The drawer -------------------------------------------------------------------

/** The drawer at the sky's right edge folds to its tab (the perch count on
 * it) until opened, then remembers (`mf_sky_drawer`: "1" open, "0" folded). */
export const SKY_DRAWER_KEY = "mf_sky_drawer";
export const skyDrawerOpen = (pref: string | null | undefined): boolean => pref === "1";

// --- The layout store (per device) ----------------------------------------------

/** One line's layout: where it sits on the plane, how big its boxes are drawn
 * (`boxW` × `boxH`, plane px of one stop box, set by the line's corner handle
 * or a ◧ preset; `size` names the preset — compact → normal → wide,
 * `cycleSize` — and alone, in an older layout, stands for its numbers),
 * whether its session
 * box is opened into the session's pane (and that pane's size), and whether
 * the owner put it there by hand (`pinned`: Tidy forgets it). The shape
 * SkyLine (components/flights/SkyLine.tsx) takes as its `layout`. */
export interface SkyLineLayout {
  x: number;
  y: number;
  size: "compact" | "normal" | "wide";
  open: boolean;
  paneW?: number;
  paneH?: number;
  pinned?: boolean;
  boxW?: number;
  /** A sky object's own frame, dragged by its corner (the intake lane; a
   * line's boxes use boxW/boxH instead). Screen-independent plane px. */
  w?: number;
  h?: number;
  boxH?: number;
}

/** What a flyway chip on the sky's shelf carries while dragged (its
 * `dataTransfer`, the flyway's id): dropped on a line, that flyway flies on
 * that line's session. */
export const FLYWAY_MIME = "application/x-mindflock-flyway";

/** What the toolbar's + New session carries while dragged onto the sky:
 * dropped on empty sky, the new-session block opens there. */
export const NEW_SESSION_MIME = "application/x-mindflock-new-session";

export type SkyLineSize = SkyLineLayout["size"];
export const SKY_SIZES: readonly SkyLineSize[] = ["compact", "normal", "wide"];
export const cycleSize = (s: SkyLineSize | undefined): SkyLineSize => SKY_SIZES[(SKY_SIZES.indexOf(s || "normal") + 1) % SKY_SIZES.length];

// --- Box sizes (the corner handle and the ◧ presets) -------------------------------

/** One stop box's size on a line, in plane px: the range the corner handle
 * keeps to. */
export const BOX_W_MIN = 140;
export const BOX_W_MAX = 420;
export const BOX_H_MIN = 60;
export const BOX_H_MAX = 220;

/** What each ◧ preset sets: the sky's roomy tier of the canvas's sizes. */
export const BOX_PRESETS: Readonly<Record<SkyLineSize, { w: number; h: number }>> = {
  compact: { w: 150, h: 64 },
  normal: { w: 236, h: 100 },
  wide: { w: 330, h: 120 },
};

const clampTo = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, Math.round(v)));

/** A box size inside the range. */
export const clampBox = (w: number, h: number): { w: number; h: number } => ({
  w: clampTo(w, BOX_W_MIN, BOX_W_MAX),
  h: clampTo(h, BOX_H_MIN, BOX_H_MAX),
});

/** A line's box size: its own `boxW` × `boxH`, else its preset's numbers. */
export function lineBox(l: { size?: SkyLineSize; boxW?: number; boxH?: number } | undefined): { w: number; h: number } {
  const p = BOX_PRESETS[l?.size || "normal"] || BOX_PRESETS.normal;
  return clampBox(Number.isFinite(l?.boxW) ? l!.boxW! : p.w, Number.isFinite(l?.boxH) ? l!.boxH! : p.h);
}

/** The preset a box width is nearest to: the tier the canvas draws (what a box
 * shows — compact the title alone, wide the detail line), before the line
 * scales it to the width asked for. */
export function boxTier(w: number): SkyLineSize {
  const { compact, normal, wide } = BOX_PRESETS;
  return w < (compact.w + normal.w) / 2 ? "compact" : w < (normal.w + wide.w) / 2 ? "normal" : "wide";
}

/** How a line draws boxes `w` wide: the canvas's `size` tier, scaled
 * uniformly by `k` (w over that tier's width) — 1 at every preset. */
export function boxScale(w: number): { size: SkyLineSize; k: number } {
  const size = boxTier(w);
  return { size, k: w / BOX_PRESETS[size].w };
}

/** What a device remembers per line: only what differs from "auto-placed,
 * normal, closed" — so a line nobody touched has no entry. */
export type StoredLine = Partial<SkyLineLayout>;
export type SkyLayoutMap = Record<string, StoredLine>;

export const SKY_LAYOUT_KEY = "mf_sky_layout";
/** The positions-only record it replaced (`{title: {x, y}}`): read once, as
 * pinned lines, when there is no layout yet. */
export const SKY_POS_KEY = "mf_sky_pos";
/** The most lines remembered; the oldest drop off past it. */
const LAYOUT_CAP = 400;
/** An opened box's pane, unless the owner resized it. */
export const PANE_W = 720;
export const PANE_H = 420;

type Store = Pick<Storage, "getItem" | "setItem">;
const local = (): Store | null => {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
};

const parseObj = (raw: string | null | undefined): Record<string, unknown> | null => {
  if (!raw) return null;
  try {
    const doc: unknown = JSON.parse(raw);
    return doc && typeof doc === "object" && !Array.isArray(doc) ? (doc as Record<string, unknown>) : null;
  } catch {
    return null;
  }
};

/** `{ [session title]: { x, y } }` (the old `mf_sky_pos`), cleaned of
 * anything that isn't a pair of finite numbers. */
export function readSkyPos(raw: string | null | undefined): Record<string, Pos> {
  const out: Record<string, Pos> = {};
  for (const [k, v] of Object.entries(parseObj(raw) || {})) {
    const p = v as Partial<Pos> | null;
    if (k && p && typeof p === "object" && Number.isFinite(p.x) && Number.isFinite(p.y)) out[k] = { x: Math.round(p.x!), y: Math.round(p.y!) };
  }
  return out;
}

/** One stored line, cleaned: a position only as a finite pair, a size only
 * as one of the three, pane sizes only positive; null when nothing is left. */
function cleanLine(v: unknown): StoredLine | null {
  if (!v || typeof v !== "object") return null;
  const p = v as Record<string, unknown>;
  const out: StoredLine = {};
  if (Number.isFinite(p.x) && Number.isFinite(p.y)) {
    out.x = Math.round(p.x as number);
    out.y = Math.round(p.y as number);
    if (p.pinned === true) out.pinned = true;
  }
  if (SKY_SIZES.includes(p.size as SkyLineSize) && p.size !== "normal") out.size = p.size as SkyLineSize;
  if (p.open === true) out.open = true;
  for (const k of ["paneW", "paneH", "w", "h"] as const) if (Number.isFinite(p[k]) && (p[k] as number) > 0) out[k] = Math.round(p[k] as number);
  // The boxes' own size, clamped — both numbers or neither, and neither when
  // they are just the preset's.
  if (Number.isFinite(p.boxW) || Number.isFinite(p.boxH)) {
    const b = lineBox({ size: out.size, boxW: p.boxW as number | undefined, boxH: p.boxH as number | undefined });
    const preset = BOX_PRESETS[out.size || "normal"];
    if (b.w !== preset.w || b.h !== preset.h) {
      out.boxW = b.w;
      out.boxH = b.h;
    }
  }
  return Object.keys(out).length ? out : null;
}

export function readSkyLayout(raw: string | null | undefined): SkyLayoutMap {
  const out: SkyLayoutMap = {};
  for (const [k, v] of Object.entries(parseObj(raw) || {})) {
    const line = k ? cleanLine(v) : null;
    if (line) out[k] = line;
  }
  return out;
}

/** The device's layout — or, on the first visit after the upgrade, the old
 * positions as pinned lines (written straight back as the new record). */
export function loadSkyLayout(store: Store | null = local()): SkyLayoutMap {
  try {
    const raw = store?.getItem(SKY_LAYOUT_KEY);
    if (raw != null) return readSkyLayout(raw);
    const old = readSkyPos(store?.getItem(SKY_POS_KEY));
    const out: SkyLayoutMap = {};
    for (const [k, p] of Object.entries(old)) out[k] = { x: p.x, y: p.y, pinned: true };
    if (Object.keys(out).length) saveSkyLayout(out, store);
    return out;
  } catch {
    return {};
  }
}

/** Write the whole map (oldest entries past the cap dropped). Never throws: a
 * private window just doesn't remember. */
export function saveSkyLayout(all: Readonly<SkyLayoutMap>, store: Store | null = local()): SkyLayoutMap {
  const next: SkyLayoutMap = { ...all };
  const keys = Object.keys(next);
  for (const k of keys.slice(0, Math.max(0, keys.length - LAYOUT_CAP))) delete next[k];
  try {
    store?.setItem(SKY_LAYOUT_KEY, JSON.stringify(next));
  } catch {
    /* storage full or blocked: the layout lasts this visit */
  }
  return next;
}

/** Change one line (a drop pins it, the size control, the box opening, a new
 * session made where its card was); returns the new map, remembered. The
 * line moves to the end, so the cap drops the least recently touched. */
export function patchSkyLine(all: Readonly<SkyLayoutMap>, title: string, patch: Partial<SkyLineLayout>, store: Store | null = local()): SkyLayoutMap {
  const next: SkyLayoutMap = { ...all };
  const merged = cleanLine({ ...next[title], ...patch });
  delete next[title];
  if (merged) next[title] = merged;
  return saveSkyLayout(next, store);
}

/** Tidy: forget where lines were put by hand — every line, or just `title`
 * — so the auto-layout places them again. Sizes and open boxes stay. */
export function tidySkyLayout(all: Readonly<SkyLayoutMap>, title?: string, store: Store | null = local()): SkyLayoutMap {
  const next: SkyLayoutMap = {};
  for (const [k, v] of Object.entries(all)) {
    if (title !== undefined && k !== title) {
      next[k] = v;
      continue;
    }
    const { x: _x, y: _y, pinned: _p, ...rest } = v;
    if (Object.keys(rest).length) next[k] = rest;
  }
  return saveSkyLayout(next, store);
}

/** The positions placeLines keeps: the pinned lines'. */
export function pinnedPositions(all: Readonly<SkyLayoutMap>): Record<string, Pos> {
  const out: Record<string, Pos> = {};
  for (const [k, v] of Object.entries(all)) if (v.pinned && Number.isFinite(v.x) && Number.isFinite(v.y)) out[k] = { x: v.x!, y: v.y! };
  return out;
}

/** A line's whole layout: what is stored, at the spot it was placed. */
export function lineLayout(stored: StoredLine | undefined, at: Pos): SkyLineLayout {
  return {
    x: at.x,
    y: at.y,
    size: stored?.size || "normal",
    open: !!stored?.open,
    paneW: stored?.paneW,
    paneH: stored?.paneH,
    pinned: !!stored?.pinned,
    boxW: stored?.boxW,
    boxH: stored?.boxH,
  };
}

/** The line's title bar (plane px): the part of a line its boxes' size leaves
 * alone. */
const LINE_BAR_H = 42;

/** The room a line needs before it has been measured: its drawn size (at the
 * normal boxes) scaled by its boxes' size — `boxW` / `boxH` when set, else the
 * old factors of its `size` — and an opened box's pane below it. */
export function estimateLine(base: { w: number; h: number }, stored: StoredLine | undefined): { w: number; h: number } {
  let w: number;
  let h: number;
  if (Number.isFinite(stored?.boxW) || Number.isFinite(stored?.boxH)) {
    const b = lineBox(stored);
    const n = BOX_PRESETS.normal;
    w = Math.round(base.w * (b.w / n.w));
    h = Math.round(Math.min(base.h, LINE_BAR_H) + Math.max(0, base.h - LINE_BAR_H) * (b.h / n.h));
  } else {
    const k = stored?.size === "compact" ? 0.75 : stored?.size === "wide" ? 1.4 : 1;
    w = Math.round(base.w * k);
    h = Math.round(base.h * (stored?.size === "compact" ? 0.85 : 1));
  }
  if (stored?.open) {
    w = Math.max(w, (stored.paneW || PANE_W) + 24);
    h += (stored.paneH || PANE_H) + 12;
  }
  return { w, h };
}

// --- Which sessions get a line ------------------------------------------------------

export type SkyMode = "all" | "mine" | "perched" | "one";

export const SKY_MODES: ReadonlyArray<{ id: SkyMode; label: string; title: string }> = [
  { id: "all", label: "All", title: "Every session — a worker inside its lead's line" },
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

/** The sessions that get a line, newest first (the rows' own order is oldest
 * first). `focus` is This session's title; `query` filters by name or task.
 *
 * A worker whose parent is itself a line is not one: it is drawn inside its
 * parent's line, beside the others (a line always draws its family). A worker
 * whose parent is gone, hidden, filtered out, or itself folded into a line
 * (a worker's worker) keeps a line of its own, its parent's box above it. This
 * session shows its title's line alone, a worker's too. */
export function skyLineTitles(rows: readonly Instance[], mode: SkyMode, opts: { query?: string; focus?: string } = {}): string[] {
  const shown = rows.filter(inSky);
  if (mode === "one") return shown.some((r) => r.title === opts.focus) ? [opts.focus!] : [];
  let pick = mode === "perched" ? shown.filter(linePerched) : shown;
  const q = String(opts.query || "").trim().toLowerCase();
  if (q) pick = pick.filter((r) => r.title.toLowerCase().includes(q) || String(r.task_headline || r.task || "").toLowerCase().includes(q));
  const byTitle = new Map(pick.map((r) => [r.title, r]));
  const isLine = new Map<string, boolean>();
  const lineOf = (r: Instance, seen: Set<string>): boolean => {
    const known = isLine.get(r.title);
    if (known !== undefined) return known;
    const up = r.parent && r.parent !== r.title ? byTitle.get(r.parent) : undefined;
    // A cycle of parents (never meant to happen) draws them all.
    const out = !up || seen.has(up.title) || !lineOf(up, new Set(seen).add(r.title));
    isLine.set(r.title, out);
    return out;
  };
  return pick.filter((r) => lineOf(r, new Set())).map((r) => r.title).reverse();
}
