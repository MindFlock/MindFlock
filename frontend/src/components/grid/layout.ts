/** Pure grid-layout logic (ports of app.js sections 3 + 15's viewCap /
 * computeVisible / balancedRows / insertIntoGrid / reconcileGridRows /
 * placeInGrid / previewRowsFor). No DOM — the components render the rows. */

import type { Instance } from "../../api/types";
import type { ViewMode } from "../../state/store";
import { orderedInstances } from "../sidebar/ordering";

/** Hard ceiling on simultaneously-rendered panes ("Auto" grows only to this). */
export const MAX_VISIBLE = 9;

/** Sentinel slot marking where a dragged window will land. */
export const DROP_PH = "\u0000drop";

export function viewCap(viewMode: ViewMode | string): number {
  return viewMode === "auto" ? Infinity : parseInt(viewMode, 10);
}

export interface VisibleOpts {
  hidden: Set<string>;
  viewMode: ViewMode | string;
  mru: string[];
  order: string[];
}

/** Which instances get a pane: hidden never; fixed views cap to the N
 * most-recently-selected (MRU, filled from stable order), displayed in
 * stable sidebar order. */
export function computeVisible(instances: Instance[], opts: VisibleOpts): Instance[] {
  const { rows } = orderedInstances(instances, opts.order);
  const shown = rows.filter((i) => !opts.hidden.has(i.title));
  const cap = Math.min(viewCap(opts.viewMode), MAX_VISIBLE);
  if (shown.length <= cap) return shown;
  const shownTitles = shown.map((i) => i.title);
  const byMru = opts.mru.filter((t) => shownTitles.includes(t));
  const rest = shownTitles.filter((t) => !byMru.includes(t));
  const chosen = new Set(byMru.concat(rest).slice(0, cap));
  return shown.filter((i) => chosen.has(i.title));
}

/** Balanced default: 2 windows -> one row of two; 4 -> 2x2; etc. */
export function balancedRows(titles: string[]): string[][] {
  const n = titles.length;
  if (!n) return [];
  const rowCount = Math.max(1, Math.round(Math.sqrt(n)));
  const perRow = Math.ceil(n / rowCount);
  const rows: string[][] = [];
  for (let i = 0; i < n; i += perRow) rows.push(titles.slice(i, i + perRow));
  return rows;
}

/** Append a new window into the next available slot (existing windows never
 * move because of an addition). Mutates and returns `rows`. */
function insertIntoGrid(rows: string[][], title: string): string[][] {
  const n = rows.flat().length + 1;
  const rowCount = Math.max(1, Math.round(Math.sqrt(n)));
  const perRow = Math.ceil(n / rowCount);
  const last = rows[rows.length - 1];
  if (last && last.length < perRow) last.push(title);
  else rows.push([title]);
  return rows;
}

/** Keep rows covering exactly the visible set with minimum disturbance: a
 * window swapped into view takes the EXACT spot of the one it replaced. */
export function reconcileGridRows(prev: string[][], visibleTitles: string[]): string[][] {
  const vis = new Set(visibleTitles);
  const kept = new Set(prev.flat().filter((t) => vis.has(t)));
  const incoming = visibleTitles.filter((t) => !kept.has(t));
  let rows = prev
    .map((r) => r.map((t) => (vis.has(t) ? t : incoming.shift() || null)).filter(Boolean) as string[])
    .filter((r) => r.length);
  if (!rows.length) return balancedRows(visibleTitles);
  for (const t of incoming) rows = insertIntoGrid(rows, t);
  return rows;
}

export type DropSide = "left" | "right" | "top" | "bottom";

/** Shared filter/locate/splice for placeInGrid and previewRowsFor: drop
 * `removeTitle` from every row, locate targetTitle's cell, then splice `token`
 * in beside it (left/right = same row, top/bottom = a new stacked row; a bare
 * new row when the target is gone). */
function insertBeside(
  prev: string[][],
  removeTitle: string,
  token: string,
  targetTitle: string,
  side: DropSide
): string[][] {
  const rows = prev.map((r) => r.filter((t) => t !== removeTitle)).filter((r) => r.length);
  let ri = -1,
    ci = -1;
  rows.forEach((r, i) => {
    const j = r.indexOf(targetTitle);
    if (j >= 0) {
      ri = i;
      ci = j;
    }
  });
  if (ri < 0) rows.push([token]);
  else if (side === "left") rows[ri].splice(ci, 0, token);
  else if (side === "right") rows[ri].splice(ci + 1, 0, token);
  else if (side === "top") rows.splice(ri, 0, [token]);
  else rows.splice(ri + 1, 0, [token]);
  return rows;
}

/** Move dragTitle next to targetTitle on the given side. */
export function placeInGrid(
  prev: string[][],
  dragTitle: string,
  targetTitle: string,
  side: DropSide
): string[][] {
  if (!dragTitle || dragTitle === targetTitle) return prev;
  return insertBeside(prev, dragTitle, dragTitle, targetTitle, side);
}

/** Rows as they WOULD be if dropped now, the dragged window replaced by the
 * DROP_PH placeholder (the dragged pane is "picked up"). */
export function previewRowsFor(
  prev: string[][],
  dragTitle: string,
  targetTitle: string,
  side: DropSide
): string[][] {
  return insertBeside(prev, dragTitle, DROP_PH, targetTitle, side);
}

/** Nearest of the 4 sides of a pane's rect (left/right = same row,
 * top/bottom = new stacked row). */
export function dropSideFor(rect: DOMRect, clientX: number, clientY: number): DropSide {
  const dx = (clientX - rect.left) / rect.width - 0.5;
  const dy = (clientY - rect.top) / rect.height - 0.5;
  if (Math.abs(dx) >= Math.abs(dy)) return dx < 0 ? "left" : "right";
  return dy < 0 ? "top" : "bottom";
}
