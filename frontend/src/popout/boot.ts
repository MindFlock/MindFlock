/** A pop-out window's page-load wiring (main.tsx, instead of the main
 * window's prefs sync, extension slots and join link).
 *
 * Both windows share one localStorage. The main window owns it: it reconciles
 * prefs with the server (lib/prefsSync.ts), writes what it adopts, and is the
 * one that POSTs. A pop-out only READS: it follows every write the main
 * window makes through the `storage` event, which a browser fires in every
 * OTHER same-origin window — a theme flipped in the main window, an accent
 * picked in Settings, or a pref synced in from another device (the main
 * window writes that too) all repaint the pop-out within a frame. And it
 * never pushes a pref itself (see {@link installPopoutWindow}). */

import { useUi } from "../state/store";
import { asPaneHeaderPref } from "../lib/paneHeader";
import { reloadKeymap } from "../lib/keymap";
import { rethemeAll } from "../lib/terminals";
import { applyStoredAppearance } from "../components/settings/screens/Appearance";
import { PRESETS_CHANGED } from "../lib/presets";
import { setPrefBeacon, setPrefSender, THEME_CHANGED } from "../lib/prefs";

function parsed(raw: string | null): unknown {
  if (raw === null) return undefined;
  try {
    return JSON.parse(raw);
  } catch {
    return undefined;
  }
}

/** Light or dark for a `cs_theme` value: unset follows the OS, as
 * index.html's pre-paint script does. */
export function themeIsLight(raw: string | null): boolean {
  if (raw === "light") return true;
  if (raw === "dark") return false;
  try {
    return !!window.matchMedia?.("(prefers-color-scheme: light)").matches;
  } catch {
    return false;
  }
}

/** Take one localStorage change the main window made. Returns whether the
 * key was one this window follows (tests). */
export function adoptStorageChange(key: string | null, raw: string | null): boolean {
  switch (key) {
    case "cs_theme": {
      const light = themeIsLight(raw);
      document.documentElement.classList.toggle("light", light);
      useUi.setState({ themeLight: light });
      rethemeAll();
      document.dispatchEvent(new Event(THEME_CHANGED));
      return true;
    }
    case "cs_accent":
    case "cs_surface":
      applyStoredAppearance();
      rethemeAll();
      return true;
    case "mf_keymap":
      reloadKeymap();
      return true;
    case "mindflock.prompt_presets":
      document.dispatchEvent(new Event(PRESETS_CHANGED));
      return true;
    case "mf_paneheader":
      useUi.setState({ paneHeader: asPaneHeaderPref(parsed(raw)) });
      return true;
    case "mf_reduce_motion":
      useUi.setState({ reduceMotion: parsed(raw) === true });
      return true;
    case "mf_aliases": {
      const v = parsed(raw);
      useUi.setState({ aliases: v && typeof v === "object" ? (v as Record<string, string>) : {} });
      return true;
    }
    default:
      // diff mode / base are read when a diff opens: the cache is enough.
      return false;
  }
}

/** The pref POST a pop-out uses: none. A pref written here (a diff mode
 * picked in this window's Diff tab) stays marked dirty in the shared
 * localStorage, and the main window sends it with its next write, pull or
 * page hide. Settling it here would clear a mark the main window may still
 * need, so the send simply never completes. */
const neverSend = () => new Promise<unknown>(() => {});

let installed = false;

/** Page-load wiring of a pop-out window. Idempotent. */
export function installPopoutWindow(): void {
  if (installed || typeof window === "undefined") return;
  installed = true;
  document.body.classList.add("popout-window");
  setPrefSender(neverSend);
  setPrefBeacon(neverSend);
  window.addEventListener("storage", (e) => {
    if (e.storageArea && e.storageArea !== window.localStorage) return;
    adoptStorageChange(e.key, e.newValue);
  });
}
