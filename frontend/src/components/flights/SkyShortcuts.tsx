/** The keyboard shortcuts sheet (docs/flights.md "Round 17": task
 * `zoom-shortcuts`), opened with `?` like Linear, Figma and Gmail. SEAM: the
 * sky passes the groups it knows; the task draws the sheet. */

import type { ReactElement } from "react";

export interface ShortcutItem {
  /** The keys as pressed, each a cap: ["Ctrl", "Z"], ["Space", "drag"], ["0"]. */
  keys: string[];
  what: string;
}

export interface ShortcutGroup {
  title: string;
  items: ShortcutItem[];
}

export interface SkyShortcutsProps {
  open: boolean;
  onClose(): void;
  groups: readonly ShortcutGroup[];
}

export function SkyShortcuts(_p: SkyShortcutsProps): ReactElement | null {
  return null;
}
