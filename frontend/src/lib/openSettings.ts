/** THE way to open Settings. The screen key is typed from the screen
 * registry (SettingsDialog's SCREENS), so a misspelled key fails tsc instead
 * of silently landing on General. Old and feature-named keys
 * (settings/screenKeys.ts) still resolve for links from outside the app;
 * inside it, name the screen. */

import type { SettingsScreen } from "../components/settings/SettingsDialog";
import { useUi } from "../state/store";

export type { SettingsScreen };

export function openSettings(screen?: SettingsScreen): void {
  useUi.getState().openDialogFor("settings", screen ?? null);
}
