/** Favorite sessions (backend core/favorites.py): the ones you keep coming
 * back to. Star one from its ⋯ menu; the sidebar's Favorites link (beside
 * Recently closed) lists them all, open or closed: an open one is a click
 * away, a closed one reopens with the same worktree, branch and
 * conversation. A favorite never closes by itself (auto-close). */

import type { Instance } from "../api/types";
import { api } from "../api/client";
import { queryClient, refreshInstances } from "../state/queries";
import { displayName } from "../state/store";
import { errorPop } from "./errorPop";
import { errMsg } from "./format";
import { toast } from "./toast";

/** The React Query key of GET /api/favorites. */
export const FAVORITES_KEY = ["favorites"] as const;

/** One row of GET /api/favorites. */
export interface FavoriteRow {
  title: string;
  at?: number;
  /** open: a live session; closed: reopenable from Recently closed
   * (`entry_id`); gone: neither (forgotten, or its folder deleted). */
  state: "open" | "closed" | "gone";
  entry_id?: string;
  branch?: string;
  folder?: string;
  closed_at?: string;
  wiped?: boolean;
}

export function refreshFavorites() {
  return queryClient.invalidateQueries({ queryKey: FAVORITES_KEY });
}

/** The ⋯ menu's switch, and the dialog's Unstar. */
export async function setFavorite(title: string, on: boolean): Promise<boolean> {
  try {
    const r = await api<{ favorite?: boolean }>("/api/favorites", { method: "POST", json: { title, on } });
    toast((r.favorite ? "Added “" : "Removed “") + displayName(title) + (r.favorite ? "” to Favorites" : "” from Favorites"));
    return !!r.favorite;
  } catch (err) {
    errorPop(on ? "Couldn't add to Favorites" : "Couldn't remove from Favorites", errMsg(err));
    return !on;
  } finally {
    void refreshFavorites();
    void refreshInstances();
  }
}

/** The menu item for a session: "☆ Add to favorites" / "★ In favorites". */
export function favoriteMenuLabel(inst: Partial<Instance> | undefined): string {
  return inst?.favorite ? "★ In favorites" : "☆ Add to favorites";
}

/** What a favorite's row says beside its name. */
export function favoriteStateText(r: FavoriteRow): string {
  if (r.state === "open") return "open";
  if (r.state === "closed") return "closed";
  return r.wiped ? "deleted" : "not found";
}

/** Favorites sorted for the list: open first, then closed, then gone; each
 * group by name. */
export function sortFavorites(rows: FavoriteRow[], nameOf: (t: string) => string = displayName): FavoriteRow[] {
  const rank = { open: 0, closed: 1, gone: 2 } as const;
  return [...rows].sort(
    (a, b) => rank[a.state] - rank[b.state] || nameOf(a.title).localeCompare(nameOf(b.title))
  );
}
