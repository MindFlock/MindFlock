/** Light / dark — one action for every control that changes it (the top
 * bar's moon button, Settings → Appearance → Mode).
 *
 * The <html> `light` class is what paints; `cs_theme` in localStorage is the
 * pre-paint cache index.html reads, and the synced `theme` pref (lib/prefs.ts)
 * carries it to your other devices. The store's `themeLight` is what the
 * controls render from, so the two can never show different answers.
 *
 * Until anyone chooses, `cs_theme` is absent and index.html follows the OS
 * colour scheme; the first choice made here is what sticks. */

import { useUi } from "../state/store";
import { notePrefWrite, THEME_CHANGED } from "./prefs";

export function isLightNow(): boolean {
  try {
    return document.documentElement.classList.contains("light");
  } catch {
    return false;
  }
}

export function setTheme(light: boolean): void {
  try {
    document.documentElement.classList.toggle("light", light);
  } catch {
    /* no document (tests) */
  }
  try {
    localStorage.setItem("cs_theme", light ? "light" : "dark");
  } catch {
    /* storage unavailable */
  }
  notePrefWrite("cs_theme"); // follows you to your other devices
  useUi.setState({ themeLight: light });
  // The top bar repaints terminals and the favicon on this (as it does for a
  // theme adopted from another device).
  try {
    document.dispatchEvent(new Event(THEME_CHANGED));
  } catch {
    /* no document */
  }
}
