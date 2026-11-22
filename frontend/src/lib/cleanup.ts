/** Disk cleanup — the worktree garbage collector's words and shapes for
 * Settings → Disk cleanup (components/settings/screens/Cleanup.tsx) and its
 * one-time toast. The server decides everything (backend
 * core/worktree_gc.py: what is safe, why the rest is kept); this module only
 * says it. Pure, so the vitest suite can pin the wording. */

export type CleanupMode = "auto" | "ask" | "off";

export type CleanupState =
  | "safe"
  | "recent"
  | "holds_work"
  | "unchecked"
  | "in_use"
  | "not_ours"
  | "missing"
  | "unknown";

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
  /** On disk (``du``), shared hard-linked files included. */
  size_bytes: number | null;
  /** What removing it alone gives back: files nothing else hard-links. */
  free_bytes?: number | null;
  last_used: number;
  session: string;
  tracked_changes: number;
  untracked_files: number;
  unsaved_commits: number;
  /** Why its files can't be checked (state "unchecked"): unregistered ·
   * repo_gone · gitdir_gone · no_git · own_repo · clone_tmp. */
  unchecked_why?: string;
}

export interface CleanupSummary {
  /** What MindFlock made (the headline); `other_*` = worktrees of the same
   * repos it did not make (listed, never removed). Bytes are measured once
   * across items, so shared hard links and nested items count once; null
   * while an item is still being measured (never a per-item sum). */
  count: number;
  bytes: number | null;
  other_count?: number;
  other_bytes?: number | null;
  safe_count: number;
  safe_bytes: number | null;
  /** Folders whose files can't be checked: never collected, each a
   * person's call after a look inside. */
  unchecked_count?: number;
  unchecked_bytes?: number | null;
}

export interface CleanupReport {
  items: CleanupItem[] | null;
  summary?: CleanupSummary;
  scanning: boolean;
  /** The list is out; sizes the cache didn't know are being measured. */
  sizing?: boolean;
  progress?: { phase: "fetch" | "check" | "size"; done: number; total: number };
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
  /** A folder that can't be checked was deleted for good. */
  permanent?: boolean;
  deleted?: { files: number; work_files: number; bytes: number };
}

/** What a look inside a folder that can't be checked found
 * (GET /api/cleanup/inspect; backend worktree_gc.inspect_unchecked). */
export interface InspectFacts {
  path: string;
  name: string;
  files: number;
  dirs: number;
  bytes: number;
  size_bytes?: number | null;
  newest: number;
  newest_file: string;
  work_files: number;
  work_bytes: number;
  dependency_files: number;
  symlinks: number;
  mounts: string[];
  complete: boolean;
  top: { name: string; dir: boolean; kind: "work" | "dependency" | "git"; files: number; bytes: number }[];
  top_more: number;
  work_sample: { path: string; bytes: number; mtime: number }[];
  git: {
    dot: "file" | "dir" | "link" | "none";
    gitdir: string;
    gitdir_exists: boolean;
    repo: string;
    repo_exists: boolean;
    branch: string;
    compare: null | {
      changed_count: number;
      missing_count: number;
      extra_count: number;
      changed: string[];
      missing: string[];
      extra: string[];
    };
    notes: string[];
  };
  verdict: "matches" | "within" | "no_work" | "differs" | "own_repo" | "unknown";
  /** Nothing in it could be lost: Remove needs no "anyway". */
  safe: boolean;
  /** The same, in sentences (the server's words). */
  summary: string[];
}

export interface InspectResult {
  ok: boolean;
  error?: string;
  facts?: InspectFacts;
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
  unchecked: "Can't be checked",
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
  return !it.in_use && !it.kept.includes("locked in git") && !isUnchecked(it);
}

/** A folder whose files can't be checked: it gets "Look inside", and is
 * removed only from there (never by "Remove N safe"). */
export function isUnchecked(it: CleanupItem): boolean {
  return it.state === "unchecked";
}

/** "N files (X), M of them look like work" — the facts every unchecked
 * confirm names. */
export function factsLine(f: InspectFacts): string {
  return (
    f.files +
    (f.files === 1 ? " file" : " files") +
    " (" +
    humanBytes(f.bytes) +
    "), " +
    f.work_files +
    (f.work_files === 1 ? " of them looks" : " of them look") +
    " like work (not dependencies, caches or build output)"
  );
}

/** The two in-page confirm steps for deleting a folder that can't be
 * checked (the desktop app has no window.confirm). Step 1 names the folder,
 * its size and what it holds; step 2 is the last word before it goes. */
export function uncheckedConfirm(
  it: CleanupItem,
  f: InspectFacts,
  step: 1 | 2,
): { title: string; body: string; confirmLabel: string } {
  const size = humanBytes(it.size_bytes ?? f.bytes);
  if (step === 1) {
    return {
      title: "Delete " + it.name + " (" + size + ") for good?",
      body:
        "It holds " +
        factsLine(f) +
        ". Its files can't be checked, so nothing is saved first, and MindFlock has no trash: this is permanent.",
      confirmLabel: "Continue…",
    };
  }
  return {
    title: "Last check: delete " + it.name + " permanently?",
    body:
      f.work_files +
      (f.work_files === 1 ? " file that looks" : " files that look") +
      " like work and " +
      size +
      " go with it. There is no undo.",
    confirmLabel: "Delete permanently",
  };
}

/** The confirm card's consequence sentence for an item that holds work. */
export function confirmText(it: CleanupItem, keptDir = "~/.mindflock/kept-work"): string {
  const parts: string[] = [];
  if (it.kind === "orphan") {
    return "Its files can't be checked or saved: look inside first, then remove it for good if nothing in it matters.";
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
  if (r.permanent) {
    const n = r.deleted?.files ?? 0;
    return "Deleted " + it.name + " permanently (" + n + (n === 1 ? " file" : " files") + ", " + humanBytes(r.deleted?.bytes ?? 0) + ").";
  }
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
  const mine = "MindFlock made " + s.count + " of these: " + humanBytes(s.bytes) + ".";
  const reclaim = s.safe_count
    ? " " + s.safe_count + " can go safely, freeing " + humanBytes(s.safe_bytes) + "."
    : " Nothing is safe to remove right now.";
  const unchecked = s.unchecked_count
    ? " " + s.unchecked_count + " can't be checked (" + humanBytes(s.unchecked_bytes ?? null) + "): look inside each to decide."
    : "";
  const other = s.other_count
    ? " " + s.other_count + " other worktree" + (s.other_count === 1 ? "" : "s") + " of the same repos (" + humanBytes(s.other_bytes ?? 0) + ") are listed, never removed."
    : "";
  return mine + reclaim + unchecked + other;
}

/** What removing it frees (falls back to its size on an older server). */
export function freeBytes(it: CleanupItem): number {
  return Number(it.free_bytes ?? it.size_bytes ?? 0) || 0;
}

/** A section's "frees …" — "…" while one of its items is still unmeasured
 * (a removed folder has no size and doesn't count), never a sum that
 * quietly takes the unknown ones as zero. */
export function groupFreesText(items: CleanupItem[]): string {
  const pending = items.some((i) => i.state !== "missing" && i.free_bytes == null && i.size_bytes == null);
  return "frees " + humanBytes(pending ? null : items.reduce((a, i) => a + freeBytes(i), 0));
}

/** What the header adds while a scan runs. */
export function scanNote(rep: CleanupReport | null): string {
  if (!rep?.scanning || rep.items == null) return "";
  const p = rep.progress;
  if (rep.sizing || p?.phase === "size") {
    return " Measuring sizes…" + (p?.phase === "size" && p.total ? " " + p.done + "/" + p.total : "");
  }
  return " Checking again…";
}

/** "10.1 GB on disk · frees 1.7 GB" when shared files make the two differ. */
export function sizeText(it: CleanupItem): string {
  if (it.size_bytes == null) return humanBytes(null);
  if (it.free_bytes == null || it.free_bytes >= it.size_bytes * 0.95) return humanBytes(it.size_bytes);
  return humanBytes(it.size_bytes) + " on disk · frees " + humanBytes(it.free_bytes);
}

/** The sections the list is drawn in, in order, with the items in each. */
export function groupItems(items: CleanupItem[]): { key: string; title: string; items: CleanupItem[] }[] {
  const by = (states: CleanupState[]) => items.filter((i) => states.includes(i.state));
  return [
    { key: "safe", title: "Will be removed", items: by(["safe"]) },
    { key: "kept", title: "Kept", items: by(["holds_work", "recent", "missing", "unknown"]) },
    {
      key: "unchecked",
      title: "Can't be checked (never removed automatically)",
      items: by(["unchecked"]),
    },
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
