/** Where "What's new seen up to version X" is kept: the person's synced prefs
 * (`prefs.whats_new_seen`, GET/POST /api/prefs), so the browser tab, the
 * desktop app and the phone share it, and this browser's localStorage as the
 * fallback when the server doesn't have it (an older server that ignores the
 * field, a refused or failed save, settings.json unreadable). The higher of
 * the two wins: seen never goes backwards.
 *
 * Plus the window's open/closed state: a small store the pop-up, the palette,
 * Settings → Help and the top bar's version share. */

import { create } from "zustand";
import { api } from "../../api/client";
import { isReleaseVersion, laterVersion } from "../../lib/whatsNew";
import type { WhatsNewRelease } from "../../whatsNew/releases";

export const WHATS_NEW_LS = "mf_whats_new_seen";
export const WHATS_NEW_PREF = "whats_new_seen";

export function readLocalSeen(): string | null {
  try {
    const v = localStorage.getItem(WHATS_NEW_LS);
    return isReleaseVersion(v) ? v : null;
  } catch {
    return null;
  }
}

function writeLocalSeen(v: string): void {
  try {
    localStorage.setItem(WHATS_NEW_LS, v);
  } catch {
    /* storage unavailable: the server copy is the record */
  }
}

/** The seen version: the server's and this browser's, the higher one. */
export async function loadSeen(): Promise<string | null> {
  let server: string | null = null;
  try {
    const prefs = await api<Record<string, unknown>>("/api/prefs");
    const v = prefs?.[WHATS_NEW_PREF];
    server = typeof v === "string" ? v : null;
  } catch {
    /* offline or unreadable: this browser's copy decides */
  }
  return laterVersion(server, readLocalSeen());
}

/** Record `v` as seen: this browser at once, the person's prefs behind it. */
export function saveSeen(v: string | null): void {
  if (!isReleaseVersion(v)) return;
  writeLocalSeen(v!);
  api("/api/prefs", { json: { [WHATS_NEW_PREF]: v } }).catch(() => {
    /* kept in localStorage; the next close tries again */
  });
}

interface WhatsNewState {
  /** The entries on screen (null = closed). */
  shown: WhatsNewRelease[] | null;
  /** The running engine's release version ("" = not known yet, or a source
   * tree) — the pop-up publishes it for openers outside React. */
  running: string;
  /** The seen version as loaded at page start (null = never). */
  seen: string | null;
  open(entries: WhatsNewRelease[]): void;
  close(): void;
}

export const useWhatsNew = create<WhatsNewState>((set) => ({
  shown: null,
  running: "",
  seen: null,
  open: (entries) => set({ shown: entries }),
  close: () => set({ shown: null }),
}));
