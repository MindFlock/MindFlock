/** Sort control for the list dialogs: a key picker plus a direction toggle.
 *
 * Two controls rather than one "Name A→Z / Name Z→A / Date ↑ / Date ↓" list:
 * the key is what you're thinking about, the direction is a flip you make right
 * after, and a combined list doubles in length with every column. Each key
 * carries the direction people actually mean for it — names ascend, dates and
 * sizes descend — so picking a key rarely needs the toggle at all. */

import { useCallback, useState } from "react";
import { loadSortPref, saveSortPref, type SortDir, type SortPref } from "../../lib/rowSort";

export interface SortOption {
  key: string;
  label: string;
  /** Direction applied when this key is picked. Newest / biggest first for
   * dates and sizes; A→Z for names. */
  defaultDir: SortDir;
  /** Arrow labels for this key, e.g. ["oldest first", "newest first"]. */
  asc: string;
  desc: string;
}

/** Sort state for one dialog, remembered across opens. */
export function useSortPref(storeKey: string, options: SortOption[]) {
  const fallback: SortPref = { key: options[0].key, dir: options[0].defaultDir };
  const [pref, setPref] = useState<SortPref>(() => {
    const p = loadSortPref(storeKey, fallback);
    // A key from an older build (or a hand-edited value) must not wedge the
    // dialog into sorting by a column that no longer exists.
    return options.some((o) => o.key === p.key) ? p : fallback;
  });

  const set = useCallback(
    (next: SortPref) => {
      setPref(next);
      saveSortPref(storeKey, next);
    },
    [storeKey]
  );

  const setKey = useCallback(
    (key: string) => {
      const opt = options.find((o) => o.key === key);
      if (opt) set({ key, dir: opt.defaultDir });
    },
    // `options` is a module-level constant at every call site.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [set]
  );

  const flip = useCallback(
    () => set({ key: pref.key, dir: pref.dir === "asc" ? "desc" : "asc" }),
    [pref, set]
  );

  return { pref, setKey, flip };
}

export function SortPicker({
  id,
  options,
  pref,
  onKey,
  onFlip,
}: {
  id: string;
  options: SortOption[];
  pref: SortPref;
  onKey: (key: string) => void;
  onFlip: () => void;
}) {
  const active = options.find((o) => o.key === pref.key) || options[0];
  const dirLabel = pref.dir === "asc" ? active.asc : active.desc;
  return (
    <div className="dlg-sort">
      <label className="muted" htmlFor={id}>
        Sort
      </label>
      <select id={id} value={active.key} onChange={(e) => onKey(e.target.value)}>
        {options.map((o) => (
          <option key={o.key} value={o.key}>
            {o.label}
          </option>
        ))}
      </select>
      <button
        type="button"
        className="dlg-sort-dir"
        title={`${dirLabel} — click to reverse`}
        aria-label={`Sort direction: ${dirLabel}`}
        onClick={onFlip}
      >
        {pref.dir === "asc" ? "↑" : "↓"}
      </button>
    </div>
  );
}
