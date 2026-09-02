/** Sorting for the list dialogs — Recently closed, which is also the disk
 * manager since the two were merged. The pure half, so the null and
 * natural-order rules can be tested without a DOM. */

export type SortDir = "asc" | "desc";

export interface SortPref {
  key: string;
  dir: SortDir;
}

/** Natural order, so `shortcut-9` sorts before `shortcut-21018` instead of
 * after it — every name in these lists ends in a ticket number. */
export function compareText(a: string, b: string): number {
  return a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" });
}

/**
 * One comparator for both dialogs' columns. Missing values (a size still being
 * computed, a workspace whose stat failed, an entry with no timestamp) sort to
 * the BOTTOM in both directions: "newest first" that leads with a block of
 * unknown dates is worse than not sorting at all.
 */
export function sortRows<T>(
  rows: T[],
  value: (row: T) => string | number | null | undefined,
  dir: SortDir
): T[] {
  const sign = dir === "asc" ? 1 : -1;
  // Array.sort is stable, so rows that tie keep the server's order.
  return rows.slice().sort((ra, rb) => {
    const a = value(ra);
    const b = value(rb);
    const aEmpty = a == null || a === "";
    const bEmpty = b == null || b === "";
    if (aEmpty || bEmpty) return aEmpty && bEmpty ? 0 : aEmpty ? 1 : -1;
    if (typeof a === "string" || typeof b === "string") {
      return sign * compareText(String(a), String(b));
    }
    return sign * (a - b);
  });
}

/** localStorage-backed sort preference. Sort is a preference, unlike the filter
 * (which is per-visit and clears on close): pick "biggest first" once and the
 * disk manager keeps opening that way. */
export function loadSortPref(storeKey: string, fallback: SortPref): SortPref {
  try {
    const raw = JSON.parse(localStorage.getItem(storeKey) || "null");
    if (raw && typeof raw.key === "string" && (raw.dir === "asc" || raw.dir === "desc")) {
      return { key: raw.key, dir: raw.dir };
    }
  } catch {
    /* unparseable or storage unavailable — fall through */
  }
  return fallback;
}

export function saveSortPref(storeKey: string, pref: SortPref) {
  try {
    localStorage.setItem(storeKey, JSON.stringify(pref));
  } catch {
    /* storage unavailable */
  }
}
