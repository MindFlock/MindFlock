/** Session renames, kept in step with the server (`/api/aliases`,
 * backend.web.core.aliases) — for every browser, and above all for a GUEST's.
 *
 * The owner's browser owns its renames: it seeds the server's mirror at boot
 * (a merge-only POST, so one browser's seed can't erase another's) and adopts
 * the merged map that comes back. A guest link's browser (docs/people.md) has
 * no renames of its own and may not write any — the owner's names ARE the
 * names of the shared sessions, so it READS the map instead (the gate filters
 * it to the sessions the link reaches).
 *
 * Both then follow `session.renamed` on the event bus: the owner renames a
 * window and every other open page — the owner's other devices, the guest's
 * tab — re-reads the map and shows the new name within a frame, instead of at
 * its next page load, which is how it used to be (and for a guest, never:
 * their seed POST was a 403 and they saw raw titles for good). */

import { api } from "../api/client";
import type { EventEnvelope } from "../state/queries";
import { useUi } from "../state/store";

let installed = false;

function adopt(aliases: Record<string, string>): void {
  try {
    localStorage.setItem("mf_aliases", JSON.stringify(aliases));
  } catch {
    /* storage off: still adopt it for this load */
  }
  useUi.setState({ aliases });
}

/** Re-read the server's map and make it this page's. */
export async function pullAliases(): Promise<void> {
  const r = await api<{ aliases?: Record<string, string> }>("/api/aliases");
  if (r?.aliases) adopt(r.aliases);
}

/** Install once per page load, as soon as `/api/config` has said whether this
 * browser is a guest. */
export function installAliasSync(guest: boolean): void {
  if (installed) return;
  installed = true;
  if (guest) {
    void pullAliases().catch(() => {});
  } else {
    // The answer is the merged map: adopt it, so a rename made in another
    // browser is this one's name for the session too.
    const aliases = useUi.getState().aliases;
    api<{ aliases?: Record<string, string> }>("/api/aliases", { json: { aliases } })
      .then((r) => {
        if (r?.aliases) adopt(r.aliases);
      })
      .catch(() => {});
  }
  const ev = window.mindflock?.events;
  if (!ev) return; // core/events.js loads before the app; belt and braces
  ev.subscribe("session.renamed", (env: EventEnvelope) => {
    // A reconnect replays history; a hundred old renames are one map.
    if (typeof ev.isReplay === "function" && ev.isReplay(env)) return;
    void pullAliases().catch(() => {});
  });
}

/** Test seam. */
export function _resetAliasSync(): void {
  installed = false;
}
