/** `repository.repo_branches`: per-repo overrides of the Workspace screen's
 * two default branches, `{"owner/name": {base_branch?, pr_base_branch?}}`.
 * The server resolves them (backend/config/settings.py `repo_branches_for`);
 * this is only the editor's row <-> map translation. */

export interface RepoBranchRow {
  repo: string;
  base: string;
  pr: string;
}

/** `owner/name` from what a person pastes: the slug itself, or any GitHub
 * remote spelling (https, ssh://, scp-style `git@host:owner/name.git`).
 * `""` when it names no repo. */
export function normalizeRepo(raw: string): string {
  let v = raw.trim().replace(/\/+$/, "").replace(/\.git$/, "");
  const scp = /^[^\s/@]+@[^\s/:]+:(.+)$/.exec(v);
  if (scp) v = scp[1];
  else v = v.replace(/^[a-z+]+:\/\/[^/]+\//i, "");
  const parts = v.split("/").filter(Boolean);
  if (parts.length < 2) return "";
  const slug = parts.slice(-2).join("/");
  return /^[^\s/]+\/[^\s/]+$/.test(slug) ? slug : "";
}

export function rowsFrom(stored: unknown): RepoBranchRow[] {
  if (!stored || typeof stored !== "object" || Array.isArray(stored)) return [];
  return Object.entries(stored as Record<string, unknown>).map(([repo, block]) => {
    const b = (block && typeof block === "object" ? block : {}) as Record<string, unknown>;
    return { repo, base: String(b.base_branch || ""), pr: String(b.pr_base_branch || "") };
  });
}

/** The map to save: rows without a repo, or overriding nothing, are left out
 * (a half-typed row stays on screen but never reaches settings). A repo
 * listed twice keeps its last row. */
export function mapFrom(rows: RepoBranchRow[]): Record<string, { base_branch?: string; pr_base_branch?: string }> {
  const out: Record<string, { base_branch?: string; pr_base_branch?: string }> = {};
  for (const r of rows) {
    const slug = normalizeRepo(r.repo);
    const block: { base_branch?: string; pr_base_branch?: string } = {};
    if (r.base.trim()) block.base_branch = r.base.trim();
    if (r.pr.trim()) block.pr_base_branch = r.pr.trim();
    if (slug && Object.keys(block).length) out[slug] = block;
  }
  return out;
}

/** One side of `GET /api/repos/bases` (backend/web/core/repo_bases.py): the
 * branch a repository will use and why. */
export interface RepoBaseSide {
  branch: string;
  source: string;
  why: string;
  note: string;
  error: string;
}

export interface RepoBaseRow {
  repo: string;
  base: RepoBaseSide;
  pr: RepoBaseSide;
}

/** What a known repository will use, as one line: "main · the repository's
 * default", or "PRs: staging · your setting" appended when Open PR targets a
 * different branch than sessions fork from. Nothing resolved reads "no base
 * branch found — set one below". */
export function usedLine(row: RepoBaseRow): string {
  if (!row.base.branch) return "no base branch found — set one below";
  const pr = row.pr.branch && row.pr.branch !== row.base.branch ? " · PRs: " + row.pr.why : "";
  return row.base.why + pr;
}

/** The plain note(s) for a row: a flat setting passed over because this
 * repository lacks it ("Using main: this repository has no staging branch"),
 * said once even when both sides skipped the same branch. */
export function usedNote(row: RepoBaseRow): string {
  const notes = [row.base.note, row.pr.note].filter(Boolean);
  return Array.from(new Set(notes)).join(". ");
}
