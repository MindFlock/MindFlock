/** THE way to open Settings. The screen key is typed from the screen
 * registry (SettingsDialog's SCREENS), so a misspelled key fails tsc instead
 * of silently landing on General. Old and feature-named keys
 * (settings/screenKeys.ts) still resolve for links from outside the app;
 * inside it, name the screen. */

import type { SettingsScreen } from "../components/settings/SettingsDialog";
import { useUi } from "../state/store";

export type { SettingsScreen };

/** Where Settings opens when nothing names a screen: Connections — the
 * status of everything MindFlock talks to, and the first-hour screen (General
 * opens on budgets and sleep timers a new user can't use yet). */
export const DEFAULT_SETTINGS_SCREEN: SettingsScreen = "connections";

/** Open Settings on `screen`, or — with none named (the gear button,
 * Ctrl+,) — on the screen you last had open this page life (lib/
 * settingsLastScreen), else General. A named screen is a deep link and
 * always wins. */
export function openSettings(screen?: SettingsScreen): void {
  useUi.getState().openDialogFor("settings", screen ?? null);
}
