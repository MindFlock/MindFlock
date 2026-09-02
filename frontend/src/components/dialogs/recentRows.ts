/** The pure half of the Recently-closed page: row identity, the sort/search
 * keys, and the exact words the unused-worktree sweep asks permission with.
 *
 * Extracted rather than left inline because vitest here runs in a node
 * environment with no DOM — no component is ever rendered — so this module is
 * the only place the copy of a destructive, irreversible action can be pinned by
 * a test. See __tests__/recentRows.test.ts. */

import { humanSize, pathBasename, relTime } from "../../lib/format";
import { previewList } from "../../lib/rowSelection";

/** One row of the merged page. `source` says which of the two things it is:
 * a closed session (reopenable, has a `closed_at`) or a directory on disk that
 * no closed session accounts for. Everything else is shared. */
export interface RecentRow {
  id: string;
  source: "closed" | "disk";
  title?: string | null;
  branch?: string | null;
  /** Disk name — for a worktree, its path under the worktrees root, which reads
   * as the branch it was cut for. */
  name: string;
  path: string;
  folder: string;
  /** base · refresher · pr · tmp · worktree · workspace · in-place ("" unknown). */
  kind: string;
  /** Whether git generated this directory as a linked worktree — the only kind
   * of row the sweep can ever take. */
  worktree: boolean;
  in_place?: boolean;
  provisioned?: boolean;
  exists: boolean;
  closed_at?: string | number | null;
  mtime?: number | null;
  /** Epoch SECONDS of the newest activity signal the server could find. */
  last_used?: number | null;
  size_bytes?: number | null;
  active_session?: string | null;
  /** True when the sweep would take this row right now. */
  stale?: boolean;
}

export interface RecentData {
  rows: RecentRow[];
  stale_days: number;
  hidden: {
    protected: number;
    protected_names: string[];
    protected_bytes: number;
    active: number;
    active_titles: string[];
  };
  roots: string[];
}

export interface PruneCandidate {
  name: string;
  path: string;
  branch?: string;
  size_bytes?: number | null;
  last_used?: number | null;
  dirty?: boolean;
  titles?: string[];
}

export interface PruneResult {
  ok: boolean;
  dry_run: boolean;
  days: number;
  candidates: PruneCandidate[];
  candidate_count: number;
  total_bytes: number;
  dirty_count: number;
  kept: {
    active: string[];
    recent: number;
    not_worktree: number;
    /** Linked worktrees that live outside MindFlock's own worktrees dir — one
     * the user cut inside their own repo, say. Never the sweep's to take. */
    outside_root?: number;
    protected: number;
  };
  removed?: string[];
  removed_count?: number;
  failed?: string[];
  forgot?: number;
  kept_dirty?: string[];
  freed_bytes?: number;
  empty_dirs_removed?: number;
}

/** What the row's headline says — also what a sort by name has to agree with.
 *
 * The saved name (the rename alias, keyed by title) outranks everything:
 * renames deliberately OUTLIVE the session (see clearStaleAlias) precisely so
 * closed work keeps the name the user gave it, and "rapisynth" is how they know
 * the session — "untitled" is only how the server does. The branch comes next
 * because it carries the descriptive tail; then the session title; then the
 * directory's own name, which is all a row that was never a session has. */
export function entryLabel(e: RecentRow, alias = ""): string {
  return alias || e.branch || e.title || e.name || "(untitled)";
}

/** Sort key for one column. Dates are epoch seconds, so "newest first" over
 * `last_used` orders closed sessions and leftover directories on one axis —
 * which is the whole point of merging the two lists. */
export function rowSortValue(
  e: RecentRow,
  key: string,
  alias = ""
): string | number | null {
  if (key === "name") return entryLabel(e, alias);
  if (key === "size") return e.size_bytes ?? null;
  return e.last_used ?? null;
}

/** Everything the row SHOWS, badges included: "gone", "in-place" and "unused"
 * read as searchable words on screen, so they have to be searchable. The path is
 * in here too even though the row only shows it on hover — a workspace is often
 * easiest to find by the repo directory it sits under. */
export function rowSearchFields(e: RecentRow, alias = ""): Array<string | null | undefined> {
  return [
    alias,
    e.branch,
    e.title,
    e.name,
    e.path,
    e.kind,
    e.in_place ? "in-place" : "",
    e.provisioned ? "provisioned" : "",
    e.exists ? "" : "worktree gone",
    e.stale ? "unused" : "",
    e.source === "disk" ? "on disk" : "closed",
  ];
}

/** The directory this row lives in, when the headline does not already say it.
 *
 * Earns its place because of in-place sessions: their branch is whatever the
 * user's repo had checked out, so eight of them in a row all read "main" and the
 * only thing that tells them apart — which repo — was on hover. */
export function dirNote(e: RecentRow, alias = ""): string {
  const base = pathBasename(e.path || "");
  if (!base) return "";
  const shown = entryLabel(e, alias);
  return shown.includes(base) || base === e.title ? "" : base;
}

/** Total bytes over rows, counting each DIRECTORY once.
 *
 * A single worktree can produce two closed rows — the store dedupes on folder
 * AND title, deliberately, so a session and its copy both stay reopenable — and
 * each row is sized by its own `du`. Summing rows would report twice the disk
 * those two rows actually hold. */
export function sumBytes(rows: RecentRow[]): number {
  const seen = new Set<string>();
  let total = 0;
  for (const e of rows) {
    const key = e.path || e.id;
    if (seen.has(key)) continue;
    seen.add(key);
    total += e.size_bytes || 0;
  }
  return total;
}

/** Whole days since anything touched this row, or null when nothing is known. */
export function staleDays(e: RecentRow, nowMs = Date.now()): number | null {
  if (!e.last_used) return null;
  return Math.floor(Math.max(0, nowMs / 1000 - e.last_used) / 86400);
}

function closedMs(e: RecentRow): number | null {
  if (!e.closed_at) return null;
  const t = new Date(e.closed_at).getTime();
  return isNaN(t) ? null : t;
}

/** The row's timestamp, said in the terms the row IS: a closed session is dated
 * by when it closed, a leftover directory by when it was last touched. Using
 * one number for both would mean writing "closed 1h ago" for a session closed
 * last month whose directory an editor opened this morning. */
export function whenText(e: RecentRow): string {
  if (e.source === "closed") {
    const ms = closedMs(e);
    return ms ? "closed " + relTime(ms / 1000) : "";
  }
  return e.last_used ? "used " + relTime(e.last_used) : "";
}

export function whenTitle(e: RecentRow): string {
  const ms = closedMs(e);
  return [
    ms ? "Closed " + new Date(ms).toLocaleString() : "",
    e.last_used ? "Last touched " + new Date(e.last_used * 1000).toLocaleString() : "",
  ]
    .filter(Boolean)
    .join("\n");
}

const plural = (n: number, one: string, many = one + "s") => (n === 1 ? one : many);

/** Why a sweep found nothing — a bare "nothing to remove" is indistinguishable
 * from a broken button when there are forty directories on screen. */
export function nothingMessage(r: PruneResult): string {
  const k = r.kept || { active: [], recent: 0, not_worktree: 0, protected: 0 };
  const outside = k.outside_root || 0;
  const days = Math.round(r.days);
  const parts: string[] = [];
  if (k.active.length)
    parts.push(
      `${k.active.length} in use by a running session (${k.active.join(", ")})`
    );
  if (k.recent) parts.push(`${k.recent} used within the last ${days} days`);
  if (k.not_worktree)
    parts.push(
      `${k.not_worktree} that ${plural(k.not_worktree, "is", "are")} not a worktree — ` +
        `a repository or a clone, which this never removes`
    );
  if (outside)
    parts.push(
      `${outside} outside MindFlock's own worktrees folder (one you made yourself — ` +
        `never removed from here)`
    );
  if (k.protected) parts.push(`${k.protected} protected base ${plural(k.protected, "clone")}`);
  return (
    `No unused worktrees to remove.\n\n` +
    (parts.length ? "Kept: " + parts.join("; ") + "." : `Nothing is older than ${days} days.`)
  );
}

/** The first confirmation: what goes, what can never go, and what it costs. */
export function pruneMessage(r: PruneResult): string {
  const n = r.candidates.length;
  const days = Math.round(r.days);
  const size = r.total_bytes ? ` (${humanSize(r.total_bytes)})` : "";
  const dirty = r.dirty_count || 0;
  return (
    `Delete ${n} unused ${plural(n, "worktree")}${size}?\n\n` +
    previewList(r.candidates.map((c) => c.name)) +
    `\n\nOnly worktrees git generated are ever removed — never a repository, a ` +
    `clone, or a folder git did not make. Left alone: anything a session is ` +
    `using, and anything touched in the last ${days} days.\n\n` +
    `Each branch and its commits stay in the repository the worktree came ` +
    `from, so it is the checkout that goes, not the work. Files git ignores ` +
    `(build output, a local database, the .env copied in at setup) go with it.` +
    (dirty
      ? `\n\n${dirty} of ${plural(n, "it", "them")} ${plural(dirty, "has", "have")} ` +
        `uncommitted changes, which would be lost — asked about separately.`
      : "") +
    `\n\nThis cannot be undone.`
  );
}

/** The second confirmation, when some candidates hold uncommitted work. OK takes
 * everything; Cancel takes only the clean ones — so "remove all unused
 * worktrees" can be answered honestly instead of silently doing the destructive
 * half of it. */
export function dirtyMessage(r: PruneResult): string {
  const dirty = r.candidates.filter((c) => c.dirty);
  const clean = r.candidates.length - dirty.length;
  if (!clean) {
    return (
      `All ${dirty.length} unused ${plural(dirty.length, "worktree")} ` +
      `${plural(dirty.length, "has", "have")} uncommitted changes. Delete ` +
      `${plural(dirty.length, "it", "them")} anyway?\n\n` +
      previewList(dirty.map((c) => c.name)) +
      `\n\nCommitted work stays on the branch; these changes do not.\n\n` +
      `This cannot be undone.`
    );
  }
  return (
    `Include the ${dirty.length} ${plural(dirty.length, "worktree")} with ` +
    `uncommitted changes?\n\n` +
    previewList(dirty.map((c) => c.name)) +
    `\n\nOK — delete all ${r.candidates.length}.\n` +
    `Cancel — delete only the ${clean} that ${plural(clean, "is", "are")} clean.`
  );
}

/** What actually happened, for the toast. */
export function prunedMessage(r: PruneResult): string {
  const n = r.removed_count || 0;
  const bits = [
    n ? `Removed ${n} ${plural(n, "worktree")}` : "Removed nothing",
    r.freed_bytes ? humanSize(r.freed_bytes) + " freed" : "",
    r.kept_dirty && r.kept_dirty.length
      ? `kept ${r.kept_dirty.length} with uncommitted work`
      : "",
    r.failed && r.failed.length ? `${r.failed.length} failed` : "",
  ];
  return bits.filter(Boolean).join(" · ");
}
