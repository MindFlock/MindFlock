/** The Settings screen you last had open, for this page's life: reopening
 * Settings lands there, as people usually come back to the setting they were
 * on. A deep link (a target) still wins, a screen that no longer exists
 * (another mode, a retired key) falls back to General, and a reload forgets
 * it — on purpose, so a fresh start is a fresh start. */

import { DEFAULT_SETTINGS_SCREEN } from "./openSettings";
import { resolveScreenKey } from "../components/settings/screenKeys";

let lastScreen: string | null = null;

export function rememberSettingsScreen(key: string): void {
  lastScreen = key || null;
}

export function lastSettingsScreen(): string | null {
  return lastScreen;
}

/** The screen Settings opens on: the deep link's, else the one you last
 * had open (if it still exists), else General. */
export function openingScreen(target: string | null | undefined, keys: readonly string[]): string {
  const linked = target ? resolveScreenKey(target, keys) : null;
  if (linked) return linked;
  if (lastScreen && keys.includes(lastScreen)) return lastScreen;
  return DEFAULT_SETTINGS_SCREEN;
}
