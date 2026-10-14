/** One GitHub repository, three uses — and the storage rules that go with each.
 *
 * A repository can be watched for review comments on your PRs
 * (`github.repos`), for new issues (`github.issue_repos`) and by Verify
 * (`repository.verify_repos`). Those are three separate lists in settings.json
 * and they STAY three lists — settings sync, the pipeline's restart signature
 * and every older device read them by those keys — but on screen a repository
 * is added once and its uses are ticks ("Use it for: PR comments · New issues ·
 * Verify"). This module turns a set of ticks into the one patch that writes
 * them, so the rules each list carries live in one place instead of in three
 * card lists that each had to remember them:
 *
 * - **PR comments, first repository.** A setup that never chose a PR switch
 *   (`github.enabled` absent) and has no PR repository yet saves its first one
 *   with `enabled: false` — adding a repository never starts acting on review
 *   comments by surprise. (Unset with repos already watched keeps meaning "on":
 *   settings from before the off-by-default change.)
 * - **Removing an Intake use drops its block.** A pr/issue block is a couple of
 *   menu picks (agent, depth, age); a map that only grew would hand an old
 *   agent choice back to a repo re-added months later.
 * - **Removing Verify KEEPS its block** (`keepsRemovedBlocks`): a verify block
 *   holds text the user wrote — the live branch, where it runs, up to 2,000
 *   characters of notes — and the backend reads an orphan block as nothing, so
 *   keeping it cannot re-track anything. A re-add comes back with what was typed.
 *
 * The patch is `{group: {field: value}}` for `POST /api/settings`, which takes
 * several groups at once — so ticking PR comments AND Verify is ONE request,
 * one settings echo, one pipeline restart (settings_hooks debounces a burst,
 * but one request is one burst by construction). */

import type { Json } from "../../api/types";

export type RepoUse = "pr" | "issue" | "verify";

export const REPO_USES: readonly RepoUse[] = ["pr", "issue", "verify"];

/** Where each use is stored. */
export const USE_STORE: Record<RepoUse, { group: string; list: string; map: string }> = {
  pr: { group: "github", list: "repos", map: "repo_settings" },
  issue: { group: "github", list: "issue_repos", map: "issue_repo_settings" },
  verify: { group: "repository", list: "verify_repos", map: "verify_repo_settings" },
};

/** The words a tick carries, everywhere it appears. */
export const USE_LABEL: Record<RepoUse, string> = {
  pr: "PR comments",
  issue: "New issues",
  verify: "Verify",
};

/** The longer sentence form, for the add flow. */
export const USE_SENTENCE: Record<RepoUse, string> = {
  pr: "Address review comments on my PRs",
  issue: "Start on new issues",
  verify: "Verify what ships",
};

/** Whether a removed repo's block is kept (Verify — the user's own words). */
export const keepsRemovedBlocks = (use: RepoUse): boolean => use === "verify";

const lower = (s: string) => s.trim().toLowerCase();

function groupOf(settings: Json, group: string): Json {
  const g = settings[group];
  return g && typeof g === "object" && !Array.isArray(g) ? (g as Json) : {};
}

/** The repos stored for one use, as saved. */
export function reposFor(settings: Json, use: RepoUse): string[] {
  const { group, list } = USE_STORE[use];
  const v = groupOf(settings, group)[list];
  return Array.isArray(v) ? v.map(String) : [];
}

/** One use's per-repo blocks, as saved. */
export function blocksFor(settings: Json, use: RepoUse): Record<string, Json> {
  const { group, map } = USE_STORE[use];
  const v = groupOf(settings, group)[map];
  return v && typeof v === "object" && !Array.isArray(v) ? { ...(v as Record<string, Json>) } : {};
}

/** The uses a repo has today (owner/name compared case-insensitively — GitHub
 * does, and `MindFlock/App` vs `mindflock/app` is one repository). */
export function usesOf(settings: Json, repo: string): RepoUse[] {
  const r = lower(repo);
  return REPO_USES.filter((u) => reposFor(settings, u).some((x) => lower(x) === r));
}

/** Every repository any use knows about, in first-seen order — the add flow's
 * suggestions, together with the ticket sources' GitHub repos. */
export function knownRepos(settings: Json, extra: string[] = []): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  for (const r of [...REPO_USES.flatMap((u) => reposFor(settings, u)), ...extra]) {
    const k = lower(r);
    if (!k || seen.has(k)) continue;
    seen.add(k);
    out.push(r.trim());
  }
  return out;
}

/** `git@github.com:acme/api.git` / `https://github.com/acme/api` → `acme/api`;
 * "" for anything that isn't a GitHub repository URL. */
export function githubSlugOf(url: string): string {
  const m = /github\.com[/:]([^/\s]+)\/([^/\s]+?)(?:\.git)?\/?$/i.exec((url || "").trim());
  return m ? m[1] + "/" + m[2] : "";
}

/** The `POST /api/settings` patch that makes `repo`'s uses exactly `next`.
 *
 * Only the lists that change are written, plus — for PR comments — the
 * `enabled: false` a never-chosen setup's first repository gets. Returns `{}`
 * when nothing would change. */
export function usesPatch(settings: Json, repo: string, next: RepoUse[]): Record<string, Json> {
  const slug = repo.trim();
  const want = new Set(next);
  const have = new Set(usesOf(settings, slug));
  const patch: Record<string, Json> = {};
  const put = (group: string, field: string, value: unknown) => {
    patch[group] = { ...(patch[group] || {}), [field]: value };
  };
  for (const use of REPO_USES) {
    const on = want.has(use);
    if (on === have.has(use)) continue;
    const { group, list, map } = USE_STORE[use];
    const repos = reposFor(settings, use);
    if (on) {
      put(group, list, [...repos, slug]);
      if (use === "pr") {
        const gh = groupOf(settings, "github");
        // The first repository on a setup that never chose: saved OFF.
        if (gh.enabled == null && !repos.length) put(group, "enabled", false);
      }
    } else {
      put(group, list, repos.filter((r) => lower(r) !== lower(slug)));
      if (!keepsRemovedBlocks(use)) {
        const blocks = blocksFor(settings, use);
        const key = Object.keys(blocks).find((k) => lower(k) === lower(slug));
        if (key) {
          delete blocks[key];
          put(group, map, blocks);
        }
      }
    }
  }
  return patch;
}

/** The patch for one field of one repo's block (`agent`, `live_branch`, …),
 * built on `blocks` — the editor's own copy, so two edits in quick succession
 * COMPOSE: the map is written wholesale, and a second patch built from a
 * settings echo that hasn't landed yet would silently drop the first.
 * Blank removes the field — blank means "inherit" on every surface — and an
 * emptied block is dropped. `skip_authors` is stored as a list. */
export function fieldPatch(
  blocks: Record<string, Json>,
  use: RepoUse,
  repo: string,
  field: string,
  value: string
): { patch: Record<string, Json>; blocks: Record<string, Json> } {
  const { group, map } = USE_STORE[use];
  const next = { ...blocks };
  const block: Json = { ...((next[repo] as Json) || {}) };
  const v = value.trim();
  if (!v) delete block[field];
  else if (field === "skip_authors") block[field] = v.split(",").map((x) => x.trim()).filter(Boolean);
  else block[field] = v;
  if (Object.keys(block).length) next[repo] = block;
  else delete next[repo];
  return { patch: { [group]: { [map]: next } }, blocks: next };
}
