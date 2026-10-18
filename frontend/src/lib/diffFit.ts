/** Split or Unified for the Diff tab, by pane width. Split is a four-column
 * grid (numbers · old · numbers · new) that crushes the code in a narrow
 * pane, so with no explicit pick a pane under SPLIT_MIN_PX reads Unified.
 * The person's pick (`cs_diffmode`, lib/diff setDiffMode — written only when
 * they press Split or Unified) always wins. Pure but for the storage read. */

import type { DiffMode } from "./diff";

export const SPLIT_MIN_PX = 700;

/** The pick saved by a press of Split / Unified, or null when there was none. */
export function pickedDiffMode(): DiffMode | null {
  let m: string | null = null;
  try {
    m = localStorage.getItem("cs_diffmode");
  } catch {
    /* storage unavailable */
  }
  return m === "split" || m === "unified" ? m : null;
}

/** The mode to draw: the pick, else by width (0 = not measured yet: Split,
 * the old default). */
export function diffModeFor(picked: DiffMode | null, width: number): DiffMode {
  if (picked) return picked;
  return width > 0 && width < SPLIT_MIN_PX ? "unified" : "split";
}
