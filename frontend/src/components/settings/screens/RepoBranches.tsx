/** Settings → Workspace → "Per-repo branches": a repo whose branches differ
 * from the screen's two defaults (most repos fork from staging, a few only
 * have main) gets a row here. Stored as `repository.repo_branches`; inside a
 * row a blank PR base means that row's own base branch, not the default PR
 * base (backend/config/settings.py `repo_branches_for`).
 *
 * Above the rows: each repository MindFlock knows about and the branch it
 * will actually use, with why ("main · the repository's default") — a default
 * the repository lacks is passed over (backend/repo_base.py), so the user can
 * see that without configuring anything, and "Set" starts a row for it. */

import { useEffect, useState } from "react";
import { api } from "../../../api/client";
import { useSettings } from "../useSettings";
import { flashSaved } from "../savedCue";
import { toast } from "../../../lib/toast";
import {
  mapFrom,
  normalizeRepo,
  rowsFrom,
  usedLine,
  usedNote,
  type RepoBaseRow,
  type RepoBranchRow,
} from "../../../lib/repoBranches";

export function RepoBranches() {
  const s = useSettings();
  const stored = s.get("repository", "repo_branches");
  const storedKey = JSON.stringify(stored || {});
  const defaultBase = String(s.get("repository", "base_branch") || "") || "main";
  const [rows, setRows] = useState<RepoBranchRow[]>(() => rowsFrom(stored));
  // Re-seed only when the store says something our rows don't: our own save's
  // echo must not wipe a half-typed row.
  useEffect(() => {
    setRows((prev) => (JSON.stringify(mapFrom(prev)) === storedKey ? prev : rowsFrom(stored)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [storedKey]);

  // What each known repository will use (read-only; re-asked after a save).
  const [used, setUsed] = useState<RepoBaseRow[]>([]);
  useEffect(() => {
    let alive = true;
    api<{ repos?: RepoBaseRow[] }>("/api/repos/bases")
      .then((d) => alive && setUsed(Array.isArray(d?.repos) ? d.repos : []))
      .catch(() => alive && setUsed([]));
    return () => {
      alive = false;
    };
  }, [storedKey, defaultBase]);

  const save = (next: RepoBranchRow[]) => {
    const map = mapFrom(next);
    if (JSON.stringify(map) === storedKey) return;
    // "Saved" by the section's own label, like every other Settings field.
    void s.saveGroups({ repository: { repo_branches: map } }).then((ok) => {
      if (ok && !flashSaved(document.getElementById("workspace-repo-branches"))) toast("Saved per-repo branches");
    });
  };
  const edit = (i: number, patch: Partial<RepoBranchRow>) =>
    setRows((prev) => prev.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const remove = (i: number) => {
    const next = rows.filter((_, j) => j !== i);
    setRows(next);
    save(next);
  };

  return (
    <div className="set-row" id="workspace-repo-branches">
      <span className="set-label">Per-repo branches</span>
      <span className="set-hint">
        For a repo whose branches differ from the defaults above, e.g. one that only has{" "}
        <code>main</code>. Blank PR base = that repo&apos;s base branch.
      </span>
      {used.length > 0 && (
        <ul className="ws-repo-used" id="workspace-repo-used">
          {used.map((u) => {
            const note = usedNote(u);
            const hasRow = rows.some((r) => normalizeRepo(r.repo) === u.repo || r.repo === u.repo);
            return (
              <li key={u.repo}>
                <code>{u.repo}</code> <span className="set-hint">{usedLine(u)}</span>
                {note && <span className="set-hint ws-repo-note"> — {note}</span>}
                {!hasRow && normalizeRepo(u.repo) && (
                  <>
                    {" "}
                    <button
                      type="button"
                      className="linklike"
                      aria-label={"Set branches for " + u.repo}
                      onClick={() => setRows((prev) => [...prev, { repo: u.repo, base: "", pr: "" }])}
                    >
                      Set
                    </button>
                  </>
                )}
              </li>
            );
          })}
        </ul>
      )}
      {rows.length > 0 && (
        <div className="ws-repo-branches">
          <span className="set-hint">Repository</span>
          <span className="set-hint">Base branch</span>
          <span className="set-hint">PR base branch</span>
          <span />
          {rows.map((r, i) => {
            const bad = r.repo.trim() !== "" && !normalizeRepo(r.repo);
            return [
              <input
                key={"repo" + i}
                data-field="repo"
                aria-label="Repository"
                aria-invalid={bad || undefined}
                title={bad ? "owner/name, or the repo's GitHub URL" : undefined}
                placeholder="owner/name"
                autoComplete="off"
                spellCheck={false}
                value={r.repo}
                onChange={(e) => edit(i, { repo: e.target.value })}
                onBlur={() => {
                  const slug = normalizeRepo(r.repo);
                  const next = rows.map((x, j) => (j === i && slug ? { ...x, repo: slug } : x));
                  setRows(next);
                  save(next);
                }}
              />,
              <input
                key={"base" + i}
                data-field="base_branch"
                aria-label="Base branch"
                placeholder={defaultBase}
                autoComplete="off"
                spellCheck={false}
                value={r.base}
                onChange={(e) => edit(i, { base: e.target.value })}
                onBlur={() => save(rows)}
              />,
              <input
                key={"pr" + i}
                data-field="pr_base_branch"
                aria-label="PR base branch"
                placeholder={r.base.trim() || "same as base"}
                autoComplete="off"
                spellCheck={false}
                value={r.pr}
                onChange={(e) => edit(i, { pr: e.target.value })}
                onBlur={() => save(rows)}
              />,
              <button
                key={"rm" + i}
                type="button"
                className="test-btn"
                aria-label={"Remove " + (r.repo || "row")}
                onClick={() => remove(i)}
              >
                Remove
              </button>,
            ];
          })}
        </div>
      )}
      <span>
        <button
          type="button"
          className="test-btn"
          onClick={() => setRows((prev) => [...prev, { repo: "", base: "", pr: "" }])}
        >
          + Add repo
        </button>
      </span>
    </div>
  );
}
