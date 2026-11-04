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
 * their seed POST was a 403 and they saw raw titles for good).
 *
 * Renames are REAL now (lib/renameSession): a rename renames the session, and
 * an alias is only what an old rename left behind. The boot seed migrates
 * them — the server renames every live session whose alias is a free, valid
 * name to it, and answers `renamed: {old: new}` beside the aliases still
 * left — so this browser moves each one's per-title state across
 * (`moveSessionTitle`). A real rename's `session.renamed` carries
 * `data.from`/`data.to` and moves the state the same way; the legacy alias
 * shape (`data.name` only) still re-reads the map. */

import { api } from "../api/client";
import type { EventEnvelope } from "../state/queries";
import { queryClient } from "../state/queries";
import { useUi } from "../state/store";
import type { Instance } from "../api/types";
import { moveSessionTitle } from "./renameSession";

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
    api<SeedAnswer>("/api/aliases", { json: { aliases } })
      .then(adoptSeedAnswer)
      .catch(() => {});
  }
  const ev = window.mindflock?.events;
  if (!ev) return; // core/events.js loads before the app; belt and braces
  ev.subscribe("session.renamed", (env: EventEnvelope) =>
    onSessionRenamedEvent(env, typeof ev.isReplay === "function" && ev.isReplay(env))
  );
}

/** The boot seed's answer: the aliases still left, and the sessions it
 * renamed to their alias (old title → new title). */
export interface SeedAnswer {
  aliases?: Record<string, string>;
  renamed?: Record<string, string>;
}

/** Take the seed's answer: each migrated session's state moves to its new
 * title (its alias goes with the move), then the remaining map is adopted. */
export function adoptSeedAnswer(r: SeedAnswer | null | undefined): void {
  if (!r) return;
  if (r.renamed && typeof r.renamed === "object") {
    for (const [from, to] of Object.entries(r.renamed)) {
      if (typeof to === "string") moveSessionTitle(from, to);
    }
  }
  if (r.aliases) adopt(r.aliases);
}

/** A real rename's old and new title off a `session.renamed` envelope, or
 * null for the legacy alias shape (`data.name` only). */
export function realRename(env: EventEnvelope | null | undefined): { from: string; to: string } | null {
  const d = (env?.data || {}) as Record<string, unknown>;
  const from = typeof d.from === "string" ? d.from : "";
  const to = typeof d.to === "string" ? d.to : "";
  return from && to && from !== to ? { from, to } : null;
}

/** `session.renamed`. A real rename moves this browser's state for the
 * session to its new title — idempotent, so the browser that renamed it
 * (which already moved it on its own answer) is untouched. A replayed one
 * (history after a reconnect) is applied only while the snapshot agrees it
 * happened (the new title listed, the old one gone): the old title may since
 * belong to a new session, whose state must stay its own. The legacy alias
 * shape re-reads the map (live only: a hundred old renames are one map). */
export function onSessionRenamedEvent(env: EventEnvelope, replay = false): void {
  const real = realRename(env);
  if (real) {
    if (replay) {
      const rows = queryClient.getQueryData<Instance[]>(["instances"]);
      if (!rows || !rows.some((r) => r.title === real.to) || rows.some((r) => r.title === real.from)) return;
    }
    moveSessionTitle(real.from, real.to);
    return;
  }
  if (replay) return;
  void pullAliases().catch(() => {});
}

/** Test seam. */
export function _resetAliasSync(): void {
  installed = false;
}
