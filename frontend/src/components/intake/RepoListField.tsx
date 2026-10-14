/** A ticketing source card's repo field: one repo, or a set of them.
 *
 * With one repo it is the Repo URL box it always was. "+ Add repo" turns it
 * into a list — each row a URL plus an optional hint ("frontend, ui") — and
 * the pipeline then routes every ticket to the repo(s) it needs: a ticket label
 * or title word matching a repo's name or hint first, the agent CLI reading the
 * ticket when that is ambiguous, else the first repo
 * (backend/ticket_ingestion/repo_routing.py). Storage and back-compat live in
 * `repoList.ts`. */

import { useEffect, useRef, useState } from "react";
import { useAutosave } from "./autosave";
import type { TicketingSource } from "../../api/types";
import {
  addRepoRow,
  cleanRepos,
  removeRepoRow,
  reposPatch,
  sameRepos,
  sourceRepos,
  type RepoEntry,
} from "./repoList";
import "./RepoListField.css";

export const REPO_HELP_ONE =
  "Where this source's tickets are worked on. Work spans repos? Add another and " +
  "each ticket starts in the one it needs.";
export const REPO_HELP_MANY =
  "Each ticket starts in the repo whose name or hint matches one of its labels or " +
  "title words; otherwise the agent reads the ticket and picks. Every ticket row " +
  "shows where it will go.";

function rowsFor(source: TicketingSource): RepoEntry[] {
  const stored = sourceRepos(source);
  return stored.length ? stored : [{ url: "", hint: "" }];
}

export function RepoListField({
  source,
  onChange,
  onPending,
  onSettled,
}: {
  source: TicketingSource;
  onChange(patch: Record<string, string | null>): void;
  /** Typed text is waiting to be saved (the card says "Saving…"). */
  onPending?(): void;
  /** A pending edit turned out to change nothing (typed, then typed back). */
  onSettled?(): void;
}) {
  const stored = sourceRepos(source);
  const storedRef = useRef(stored);
  storedRef.current = stored;
  const [rows, setRows] = useState<RepoEntry[]>(() => rowsFor(source));
  // Saved as you type (a short pause), on blur and on Enter — never only on
  // blur, which left a typed URL unsaved under a card reading "Saved ✓".
  const save = useAutosave<RepoEntry[]>((next) => {
    if (!sameRepos(cleanRepos(next), storedRef.current)) onChange(reposPatch(next));
    else onSettled?.();
  }, onPending);
  // A save elsewhere (another tab, settings sync) re-seeds the rows, unless
  // they already say the same thing — then a half-typed blank row survives —
  // or text typed here is still on its way to the server.
  const storedKey = JSON.stringify(stored);
  useEffect(() => {
    if (save.isPending()) return;
    setRows((prev) => (sameRepos(cleanRepos(prev), stored) ? prev : rowsFor(source)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [storedKey]);

  const commit = (next: RepoEntry[]) => {
    setRows(next);
    save.schedule(next);
    save.flush();
  };
  const edit = (index: number, patch: Partial<RepoEntry>) => {
    const next = rows.map((r, i) => (i === index ? { ...r, ...patch } : r));
    setRows(next);
    save.schedule(next);
  };
  const onKey = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") {
      e.preventDefault();
      save.flush();
    }
  };

  const many = rows.length > 1;
  const missing = cleanRepos(rows).length === 0;
  return (
    <div className="set-row tk-repos" role="group" aria-label="Repositories">
      {/* The same required marker the catalog's fields wear (FieldLabel). */}
      <span className="set-label">
        {many ? "Repos" : "Repo URL"}
        <span className="ik-field-marker required" data-marker="required">
          *
        </span>
      </span>
      {rows.map((r, i) => (
        <div className="tk-repo-row" key={i}>
          <input
            type="text"
            autoComplete="off"
            required={i === 0}
            data-tk-field={i === 0 ? "repo_url" : undefined}
            aria-label={many ? `Repo ${i + 1} URL` : "Repo URL"}
            className={"tk-repo-url" + (i === 0 && missing ? " field-missing" : "")}
            placeholder="git@github.com:org/repo.git"
            value={r.url}
            onChange={(e) => edit(i, { url: e.target.value })}
            onBlur={save.flush}
            onKeyDown={onKey}
          />
          {many && (
            <input
              type="text"
              autoComplete="off"
              aria-label={`Repo ${i + 1} hint`}
              className="tk-repo-hint"
              placeholder="hint, e.g. frontend, ui (optional)"
              value={r.hint || ""}
              onChange={(e) => edit(i, { hint: e.target.value })}
              onBlur={save.flush}
              onKeyDown={onKey}
            />
          )}
          {many && (
            <button
              type="button"
              className="test-btn tk-repo-remove"
              aria-label={`Remove repo ${i + 1}`}
              title="Remove this repo"
              onClick={() => commit(removeRepoRow(rows, i))}
            >
              Remove
            </button>
          )}
        </div>
      ))}
      <div>
        <button type="button" className="test-btn tk-repo-add" onClick={() => setRows(addRepoRow(rows))}>
          + Add repo
        </button>
      </div>
      <span className="set-hint">{many ? REPO_HELP_MANY : REPO_HELP_ONE}</span>
    </div>
  );
}
