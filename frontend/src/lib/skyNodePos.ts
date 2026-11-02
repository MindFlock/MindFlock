/** Where the Sky draws a chain's nodes that are not steps (sub-sessions hung
 * under a stop or the session, a Split's workers): they have no `step.pos` on
 * the server, so their spot is kept per device, per chain (`mf_sky_node_pos`
 * = {<chain key>: {<node id>: {x, y}}}, chain-local plane px). Storage that
 * throws (private window) just means nothing is remembered. */
import type { Pos } from "./skyLayout";

const KEY = "mf_sky_node_pos";

type Store = Record<string, Record<string, Pos>>;

function read(): Store {
  try {
    const raw = localStorage.getItem(KEY);
    const v = raw ? JSON.parse(raw) : {};
    return v && typeof v === "object" ? (v as Store) : {};
  } catch {
    return {};
  }
}

export function loadNodePos(chain: string): Record<string, Pos> {
  return { ...(read()[chain] || {}) };
}

export function saveNodePos(chain: string, spots: Record<string, Pos>): void {
  try {
    const all = read();
    if (Object.keys(spots).length) all[chain] = spots;
    else delete all[chain];
    localStorage.setItem(KEY, JSON.stringify(all));
  } catch {
    /* not remembered */
  }
}
