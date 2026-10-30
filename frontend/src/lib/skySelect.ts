/** Selecting on the Sky (docs/flights.md "As built: round 11 selection"): a
 * drag on empty sky draws a marquee, and what it touched becomes the
 * selection — whole lines, or the stop boxes of ONE line. Pure geometry and
 * bookkeeping here; SkyView measures (plane px: the camera's zoom and pan
 * already taken off) and SkySelection acts.
 *
 * The rules:
 * - a line is touched WHOLE when the marquee crosses its title bar (its
 *   `heads`) or covers at least half of it (`WHOLE_LINE`) — a session box or
 *   a grounded line's "No session yet" box is not a head: a sweep over the
 *   first stops clips it too easily;
 * - a stop box (a node id `stop:…` or `group:…`) is touched when the marquee
 *   overlaps it at all;
 * - stops of exactly one line and no whole line → those stops; anything else
 *   → lines (every whole line, and every line a touched stop sits on).
 *
 * A group box stands for its sub-steps (`stepIndexesOf`). */

import type { FlightStep } from "./flights";
import type { Pos, Rect } from "./skyLayout";

/** A line as the marquee sees it: its rect and its heads — the title bar —
 * (plane px). */
export interface SelectLine {
  id: string;
  rect: Rect;
  heads: readonly Rect[];
}

/** A stop box on a line (plane px). */
export interface SelectStop {
  line: string;
  node: string;
  rect: Rect;
}

/** What is selected: whole lines, or stops of one line (a line id → its
 * selected node ids). Never both. */
export interface SkySelection {
  lines: ReadonlySet<string>;
  stops: ReadonlyMap<string, ReadonlySet<string>>;
}

export const NO_SELECTION: SkySelection = { lines: new Set(), stops: new Map() };

/** The share of a line the marquee must cover to take it whole without
 * crossing a head. */
export const WHOLE_LINE = 0.5;

/** A drag shorter than this (screen px) is a click, not a marquee. */
export const MARQUEE_MIN = 4;

/** The rectangle between two corners, any way round. */
export function rectFrom(a: Pos, b: Pos): Rect {
  return { x: Math.min(a.x, b.x), y: Math.min(a.y, b.y), w: Math.abs(a.x - b.x), h: Math.abs(a.y - b.y) };
}

/** The area two rectangles share (0 when apart or only touching edges). */
export function overlapArea(a: Rect, b: Rect): number {
  const w = Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x);
  const h = Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y);
  return w > 0 && h > 0 ? w * h : 0;
}

export const overlaps = (a: Rect, b: Rect) => overlapArea(a, b) > 0;

/** The line is taken whole: its bar crossed, or half of it covered. */
export function lineTouched(m: Rect, l: SelectLine): boolean {
  if (l.heads.some((h) => overlaps(m, h))) return true;
  const area = l.rect.w * l.rect.h;
  return area > 0 && overlapArea(m, l.rect) >= area * WHOLE_LINE;
}

/** A node id the selection can hold as a stop: a stop, or a group box. */
export const isStopNode = (id: string) => id.startsWith("stop:") || id.startsWith("group:");

/** What a marquee `m` selects among `items`. */
export function select(m: Rect, items: { lines: readonly SelectLine[]; stops: readonly SelectStop[] }): SkySelection {
  const whole = new Set(items.lines.filter((l) => lineTouched(m, l)).map((l) => l.id));
  const stops = new Map<string, Set<string>>();
  for (const s of items.stops) {
    if (!isStopNode(s.node) || !overlaps(m, s.rect)) continue;
    const set = stops.get(s.line) || new Set<string>();
    set.add(s.node);
    stops.set(s.line, set);
  }
  if (!whole.size && stops.size === 1) return { lines: new Set(), stops };
  for (const id of stops.keys()) whole.add(id);
  return { lines: whole, stops: new Map() };
}

/** Shift+drag: `b` added to `a`, under the same one-line rule. */
export function addSelection(a: SkySelection, b: SkySelection): SkySelection {
  if (!a.lines.size && !b.lines.size) {
    const ids = new Set([...a.stops.keys(), ...b.stops.keys()]);
    if (ids.size <= 1) {
      const out = new Map<string, Set<string>>();
      for (const [line, set] of [...a.stops, ...b.stops]) out.set(line, new Set([...(out.get(line) || []), ...set]));
      return { lines: new Set(), stops: out };
    }
  }
  return { lines: new Set([...a.lines, ...a.stops.keys(), ...b.lines, ...b.stops.keys()]), stops: new Map() };
}

/** Every one of these lines (Ctrl/⌘+A). */
export const selectLines = (ids: Iterable<string>): SkySelection => ({ lines: new Set(ids), stops: new Map() });

/** One line in or out (Shift+click on its bar). */
export function toggleLine(s: SkySelection, id: string): SkySelection {
  const lines = new Set([...s.lines, ...s.stops.keys()]);
  if (s.lines.has(id)) lines.delete(id);
  else lines.add(id);
  return { lines, stops: new Map() };
}

/** Only what is still on the board (a closed session's line drops out). */
export function pruneSelection(s: SkySelection, shown: ReadonlySet<string>): SkySelection {
  const gone = [...s.lines, ...s.stops.keys()].some((id) => !shown.has(id));
  if (!gone) return s;
  return { lines: new Set([...s.lines].filter((id) => shown.has(id))), stops: new Map([...s.stops].filter(([id]) => shown.has(id))) };
}

export const selectionEmpty = (s: SkySelection) => !s.lines.size && !s.stops.size;

/** The one line whose stops are selected, and its node ids. */
export function stopSelection(s: SkySelection): { line: string; nodes: string[] } | null {
  const first = [...s.stops][0];
  return first && first[1].size ? { line: first[0], nodes: [...first[1]] } : null;
}

/** The flight's step indexes a stop node stands for: a stop its own; a group
 * box (`group:<gid>`, or `group:<gid>@<index>` for a later run of the same
 * id) the consecutive run of its sub-steps. Nothing for any other node. */
export function stepIndexesOf(node: string, steps: readonly Pick<FlightStep, "id" | "group">[]): number[] {
  if (node.startsWith("stop:")) {
    const id = node.slice(5);
    const i = steps.findIndex((s, k) => (s.id || "i" + k) === id);
    return i < 0 ? [] : [i];
  }
  if (!node.startsWith("group:")) return [];
  const rest = node.slice(6);
  const at = rest.lastIndexOf("@");
  const gid = at >= 0 ? rest.slice(0, at) : rest;
  const from = at >= 0 ? Number(rest.slice(at + 1)) : steps.findIndex((s) => s.group?.id === gid);
  if (!Number.isInteger(from) || from < 0 || steps[from]?.group?.id !== gid) return [];
  const out: number[] = [];
  for (let i = from; i < steps.length && steps[i].group?.id === gid; i++) out.push(i);
  return out;
}

/** Every step index the nodes stand for, sorted, once each. */
export function selectedIndexes(nodes: Iterable<string>, steps: readonly Pick<FlightStep, "id" | "group">[]): number[] {
  const all = new Set<number>();
  for (const n of nodes) for (const i of stepIndexesOf(n, steps)) all.add(i);
  return [...all].sort((a, b) => a - b);
}

/** The indexes sit next to each other (Group needs that). */
export function consecutive(indexes: readonly number[]): boolean {
  const s = [...new Set(indexes)].sort((a, b) => a - b);
  return s.length > 0 && s[s.length - 1] - s[0] === s.length - 1;
}

/** How to take the nodes off their line: a group box by one of its steps
 * with `group` (the whole group goes), a stop by its own id. */
export function removals(nodes: Iterable<string>, steps: readonly Pick<FlightStep, "id" | "group">[]): Array<{ step: string; group: boolean }> {
  const out: Array<{ step: string; group: boolean }> = [];
  for (const n of nodes) {
    const idx = stepIndexesOf(n, steps);
    if (!idx.length) continue;
    out.push({ step: steps[idx[0]].id, group: n.startsWith("group:") });
  }
  return out;
}

/** A rectangle on screen (a box's getBoundingClientRect, relative to the
 * viewport's own corner) on the plane. */
export function screenToPlane(cam: { x: number; y: number; z: number }, r: Rect): Rect {
  const z = cam.z || 1;
  return { x: (r.x - cam.x) / z, y: (r.y - cam.y) / z, w: r.w / z, h: r.h / z };
}

/** And back: a plane rectangle where it is drawn in the viewport. */
export function planeToScreen(cam: { x: number; y: number; z: number }, r: Rect): Rect {
  return { x: cam.x + r.x * cam.z, y: cam.y + r.y * cam.z, w: r.w * cam.z, h: r.h * cam.z };
}
