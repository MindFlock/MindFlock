/** How the Map names a zone the REPO shares (scope "shared": from the
 * committed `.mindflock/zones.toml` in this worktree) — and who added it.
 * Such a zone is locked here: it is lifted by editing the file (or from the
 * Zones dialog), never by one worktree's Map. */

import type { RedZone } from "../../../api/types";

export function isShared(z: Pick<RedZone, "scope">): boolean {
  return z.scope === "shared";
}

/** "shared with the repo · added by Ethan" (author/date when the file says). */
export function sharedWhere(z: Pick<RedZone, "author" | "added">): string {
  const who = (z.author || "").trim();
  return "shared with the repo" + (who ? " · added by " + who : z.added ? " · added " + z.added : "");
}
