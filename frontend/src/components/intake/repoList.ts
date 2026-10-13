/** A ticketing source's SET of repos — the pure half of `RepoListField`.
 *
 * A source used to name one repo (`repo_url`). It can now name several, stored
 * as `repos: [{url, hint?}]` beside a `repo_url` that always mirrors the first,
 * so anything that only knows the old field keeps working. One repo is stored
 * exactly the old way (no `repos` key at all). The server's side of the shape
 * is `TicketingSource.repo_entries` in backend/config/settings.py; the routing
 * that picks a ticket's repo(s) is backend/ticket_ingestion/repo_routing.py.
 *
 * Like `last_test`, `repos` is a non-string passenger on a string-typed source,
 * so it is read and written only through these helpers. */

import type { TicketingSource } from "../../api/types";

export interface RepoEntry {
  url: string;
  /** Optional words ("frontend, ui") a ticket's labels or title are matched
   * against to pick this repo. Meaningless with one repo, so not shown then. */
  hint?: string;
}

/** The source's repos, first = the default: its `repos` list when that names
 * two or more, else its `repo_url` as a one-element list (`[]` when unset). */
export function sourceRepos(source: TicketingSource | null | undefined): RepoEntry[] {
  const raw = (source as unknown as { repos?: unknown } | null | undefined)?.repos;
  const list = Array.isArray(raw) ? cleanRepos(raw.map(toEntry)) : [];
  if (list.length > 1) return list;
  const url = (source?.repo_url || "").trim();
  if (url) return [{ url }];
  return list;
}

function toEntry(item: unknown): RepoEntry {
  if (typeof item === "string") return { url: item };
  if (item && typeof item === "object") {
    const o = item as Record<string, unknown>;
    return {
      url: typeof o.url === "string" ? o.url : "",
      hint: typeof o.hint === "string" ? o.hint : "",
    };
  }
  return { url: "" };
}

/** What a save stores: trimmed, blank URLs and repeats dropped, blank hints
 * omitted. Rows still being typed (blank) stay on screen but are not saved. */
export function cleanRepos(rows: RepoEntry[]): RepoEntry[] {
  const out: RepoEntry[] = [];
  const seen = new Set<string>();
  for (const r of rows) {
    const url = (r.url || "").trim();
    if (!url || seen.has(url)) continue;
    seen.add(url);
    const hint = (r.hint || "").trim();
    out.push(hint ? { url, hint } : { url });
  }
  return out;
}

/** Whether two cleaned lists store the same thing. */
export function sameRepos(a: RepoEntry[], b: RepoEntry[]): boolean {
  return (
    a.length === b.length &&
    a.every((r, i) => r.url === b[i].url && (r.hint || "") === (b[i].hint || ""))
  );
}

/** The card patch for a new list: `repo_url` = the first repo, and `repos`
 * only when there are two or more (an empty list otherwise, which the server
 * stores as the old one-field shape). The one place the array rides in a
 * string-typed patch. */
export function reposPatch(rows: RepoEntry[]): Record<string, string | null> {
  const list = cleanRepos(rows);
  const patch: Record<string, unknown> = {
    repo_url: list[0]?.url || "",
    repos: list.length > 1 ? list : [],
  };
  return patch as Record<string, string | null>;
}

/** The rows with one blank row appended (the "+ Add repo" button). */
export function addRepoRow(rows: RepoEntry[]): RepoEntry[] {
  return [...rows, { url: "", hint: "" }];
}

/** The rows without row `index`; never fewer than one row on screen. */
export function removeRepoRow(rows: RepoEntry[], index: number): RepoEntry[] {
  const next = rows.filter((_, i) => i !== index);
  return next.length ? next : [{ url: "", hint: "" }];
}
