/** Disk cleanup — the worktree garbage collector's words and shapes for
 * Settings → Disk cleanup (components/settings/screens/Cleanup.tsx) and its
 * one-time toast. The server decides everything (backend
 * core/worktree_gc.py: what is safe, why the rest is kept); this module only
 * says it. Pure, so the vitest suite can pin the wording. */

export type CleanupMode = "auto" | "ask" | "off";

export type CleanupState = "safe" | "recent" | "holds_work" | "in_use" | "not_ours" | "missing" | "unknown";

export interface CleanupItem {
  id: string;
  path: string;
  name: string;
  kind: "worktree" | "orphan" | "clone";
  repo: string;
  branch: string;
  state: CleanupState;
  safe: boolean;
  ours: boolean;
  in_use: string;
  merged: "" | "ancestor" | "content" | "pr";
  merged_into: string;
  kept: string[];
  size_bytes: number | null;
  last_used: number;
  session: string;
  tracked_changes: number;
  untracked_files: number;
  unsaved_commits: number;
}

export interface CleanupSummary {
  count: number;
  bytes: number;
  safe_count: number;
  safe_bytes: number;
}

export interface CleanupReport {
  items: CleanupItem[] | null;
  summary?: CleanupSummary;
  scanning: boolean;
  scanned_at?: number;
  settings: { mode: CleanupMode; after_days: number };
  kept_dir?: string;
  worktrees_root?: string;
  error?: string;
}

export interface RemoveResult {
  ok: boolean;
  error?: string;
  needs_confirm?: boolean;
  branch_deleted?: string;
  saved?: { dir: string; patch: string; bundle: string; kept_branch: string };
}

export const CLEANUP_MODES: readonly { value: CleanupMode; label: string; hint: string }[] = [
  {
    value: "auto",
    label: "Automatic",
    hint: "Removes the safe ones by itself every few hours. Anything that holds work is never touched.",
  },
  {
    value: "ask",
    label: "Ask me",
    hint: "Tells you once when there is a meaningful amount to reclaim. Nothing is removed until you press the button.",
  },
  { value: "off", label: "Off", hint: "No background scan and no notice. This list still works when you open it." },
];

export function cleanupModeOf(v: unknown): CleanupMode {
  return v === "auto" || v === "off" ? v : "ask";
}

export function humanBytes(n: number | null | undefined): string {
  if (n == null) return "…";
  let v = Number(n) || 0;
  for (const unit of ["B", "KB", "MB", "GB", "TB"]) {
    if (v < 1024 || unit === "TB") return unit === "B" ? v + " B" : v.toFixed(v < 10 ? 1 : 0) + " " + unit;
    v /= 1024;
  }
  return v + " B";
}

export function usedAgo(ts: number, now = Date.now() / 1000): string {
  if (!ts) return "";
  const d = Math.floor((now - ts) / 86400);
  if (d < 1) return "used today";
  return d === 1 ? "used yesterday" : "used " + d + " days ago";
}

export const STATE_LABEL: Record<CleanupState, string> = {
  safe: "Will be removed",
  recent: "Used recently",
  holds_work: "Holds work",
  in_use: "In use",
  not_ours: "Not MindFlock's",
  missing: "Already gone",
  unknown: "Couldn't check",
};

/** The one line under an item: why it goes, or every reason it stays. */
export function whyLine(it: CleanupItem): string {
  if (it.safe) {
    if (it.merged === "pr") return "Its PR merged; nothing uncommitted.";
    if (it.merged) return "Merged into " + (it.merged_into || "the base branch") + "; nothing uncommitted.";
    return "Every commit is on a remote; nothing uncommitted. Its branch is kept.";
  }
  return it.kept.length ? cap(it.kept.join("; ")) + "." : "";
}

function cap(s: string): string {
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}

/** Whether removing it needs the person's yes first (anything not safe). */
export function needsConfirm(it: CleanupItem): boolean {
  return !it.safe;
}

/** Whether a per-item Remove is offered at all: never for a folder a session
 * (or route, run, queue) still uses, nor a locked one — the server refuses
 * those too. */
export function canRemove(it: CleanupItem): boolean {
  return !it.in_use && !it.kept.includes("locked in git");
}

/** The confirm card's consequence sentence for an item that holds work. */
export function confirmText(it: CleanupItem, keptDir = "~/.mindflock/kept-work"): string {
  const parts: string[] = [];
  if (it.kind === "orphan") {
    return "Git no longer knows this folder, so its files can't be checked or saved. They will be deleted.";
  }
  if (it.tracked_changes || it.untracked_files) {
    parts.push("Its uncommitted changes are saved first as a patch in " + keptDir + ".");
  }
  if (it.unsaved_commits) {
    parts.push(
      it.kind === "clone"
        ? "Its unpushed commits are saved there as a bundle."
        : it.branch
          ? "Its branch " + it.branch + " is kept, with its commits."
          : "Its commits are kept on a mindflock-kept/… branch.",
    );
  }
  if (!it.ours) parts.push("MindFlock didn't create it.");
  if (!parts.length) parts.push("Nothing in it is lost: its branch stays.");
  return parts.join(" ");
}

/** What the removal kept, for the toast after it. */
export function removedText(it: CleanupItem, r: RemoveResult): string {
  const bits = ["Removed " + it.name + "."];
  if (r.saved?.patch) bits.push("Uncommitted changes saved to " + r.saved.patch + ".");
  if (r.saved?.bundle) bits.push("Unpushed commits saved to " + r.saved.bundle + ".");
  if (r.saved?.kept_branch) bits.push("Its commits are on branch " + r.saved.kept_branch + ".");
  if (r.branch_deleted) bits.push("Deleted merged branch " + r.branch_deleted + ".");
  return bits.join(" ");
}

/** The header line over the list. */
export function summaryLine(s: CleanupSummary | undefined): string {
  if (!s) return "";
  const all = s.count + (s.count === 1 ? " item" : " items") + ", " + humanBytes(s.bytes);
  if (!s.safe_count) return all + ". Nothing is safe to remove right now.";
  return all + ". " + s.safe_count + " can go safely (" + humanBytes(s.safe_bytes) + ").";
}

/** The sections the list is drawn in, in order, with the items in each. */
export function groupItems(items: CleanupItem[]): { key: string; title: string; items: CleanupItem[] }[] {
  const by = (states: CleanupState[]) => items.filter((i) => states.includes(i.state));
  return [
    { key: "safe", title: "Will be removed", items: by(["safe"]) },
    { key: "kept", title: "Kept", items: by(["holds_work", "recent", "missing", "unknown"]) },
    { key: "in_use", title: "In use", items: by(["in_use"]) },
    { key: "not_ours", title: "Not MindFlock's (never removed automatically)", items: by(["not_ours"]) },
  ].filter((g) => g.items.length);
}

/** "<what failed>: <reason>." (the reason the rejected call carried). */
export function failSentence(what: string, err: unknown): string {
  const why = String((err as { message?: unknown } | null)?.message ?? (typeof err === "string" ? err : "")).trim();
  return why ? what + ": " + why.replace(/\.$/, "") + "." : what + ".";
}

/** The one-time toast for `system.cleanup_available`. */
export function cleanupNoticeText(d: Record<string, unknown> | undefined): string {
  const detail = String(d?.detail || "");
  if (detail) return detail;
  const n = Number(d?.count) || 0;
  return n + (n === 1 ? " leftover worktree" : " leftover worktrees") + " can be removed safely.";
}
