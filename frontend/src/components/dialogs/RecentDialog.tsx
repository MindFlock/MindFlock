/** Recently-closed sessions dialog (port of loadRecentlyClosed, section 20):
 * reopen / wipe worktree / forget. */

import { useCallback, useEffect, useMemo, useState } from "react";
import type { Instance } from "../../api/types";
import { api } from "../../api/client";
import { refreshInstances } from "../../state/queries";
import { useUi } from "../../state/store";
import { selectSession } from "../../lib/sessionActions";
import { matchesTokens, searchTokens } from "../../lib/rowSearch";
import { previewList } from "../../lib/rowSelection";
import { sortRows } from "../../lib/rowSort";
import { toast } from "../../lib/toast";
import { DialogFilter } from "./DialogFilter";
import { BulkRowBar, RowCheck, SelectAllCheck, useRowSelection } from "./rowSelect";
import { SortPicker, useSortPref, type SortOption } from "./SortPicker";

interface ClosedEntry {
  id: string;
  title?: string;
  /** The branch the session owned. The server has always sent this; the row used
   * to show only `title`, which for an ingested session is the bare slug
   * ("shortcut-21018") and says nothing about what the work was. */
  branch?: string;
  folder?: string;
  closed_at?: string | number;
  exists: boolean;
  in_place?: boolean;
  provisioned?: boolean;
}

function fmtClosedAt(iso: string | number | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d.getTime()) ? "" : d.toLocaleString();
}

/** Sortable timestamp, or null for an entry the server sent without one (those
 * sink to the bottom either way — see sortRows). */
function closedMs(e: ClosedEntry): number | null {
  if (!e.closed_at) return null;
  const t = new Date(e.closed_at).getTime();
  return isNaN(t) ? null : t;
}

/** What the row's headline says — also what a sort by name has to agree with.
 *
 * The saved name (the rename alias, keyed by title) outranks everything:
 * renames deliberately OUTLIVE the session (see clearStaleAlias) precisely so
 * closed work keeps the name the user gave it, and "rapisynth" is how they
 * know the session — "untitled" is only how the server does. The branch comes
 * next because it carries the descriptive tail; the bare title is last. */
function entryLabel(e: ClosedEntry, alias = ""): string {
  return alias || e.branch || e.title || "(untitled)";
}

const SORTS: SortOption[] = [
  {
    key: "date",
    label: "Date closed",
    defaultDir: "desc",
    asc: "oldest first",
    desc: "newest first",
  },
  { key: "name", label: "Branch / name", defaultDir: "asc", asc: "A → Z", desc: "Z → A" },
];

export function RecentDialog() {
  const open = useUi((s) => s.openDialog === "recent");
  const closeDialog = useUi((s) => s.closeDialog);
  // Saved names (rename aliases) are client-side and keyed by title — the
  // server's closed-list has never heard of them, so the join happens here.
  const aliases = useUi((s) => s.aliases);
  const [list, setList] = useState<ClosedEntry[] | null>(null);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const sort = useSortPref("mf_sort_recent", SORTS);

  const load = useCallback(async () => {
    try {
      const data = await api<ClosedEntry[]>("/api/recently-closed");
      setError("");
      setList(Array.isArray(data) ? data : []);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    if (open) load();
  }, [open, load]);

  // Reopening the dialog must not silently hide rows behind last time's query.
  useEffect(() => {
    if (!open) setQuery("");
  }, [open]);

  const all = list || [];
  const aliasOf = useCallback(
    (e: ClosedEntry) => (e.title && aliases[e.title]) || "",
    [aliases]
  );
  const sorted = useMemo(
    () =>
      sortRows(
        all,
        (e) => (sort.pref.key === "name" ? entryLabel(e, aliasOf(e)) : closedMs(e)),
        sort.pref.dir
      ),
    [all, sort.pref, aliasOf]
  );
  // Search everything the row SHOWS, badges included — "gone" and "in-place"
  // read as searchable words on screen, so they have to be searchable. The
  // saved name is now the headline, so it has to match too.
  const data = useMemo(() => {
    const tokens = searchTokens(query);
    return sorted.filter((e) =>
      matchesTokens(
        [
          aliasOf(e),
          e.branch,
          e.title,
          e.folder,
          e.in_place ? "in-place" : "",
          e.provisioned ? "provisioned" : "",
          e.exists ? "" : "worktree gone",
        ],
        tokens
      )
    );
  }, [sorted, query, aliasOf]);

  const byId = useMemo(() => new Map(all.map((e) => [e.id, e])), [all]);
  // Both key lists follow the DISPLAY order: shift-ranges have to match what the
  // eye sees, and a bulk confirmation reads like the list it came from.
  const sel = useRowSelection(
    useMemo(() => sorted.map((e) => e.id), [sorted]),
    useMemo(() => data.map((e) => e.id), [data])
  );

  if (!open) return null;

  const picked = sel.keys.map((id) => byId.get(id)).filter(Boolean) as ClosedEntry[];
  const label = (e: ClosedEntry) => entryLabel(e, aliasOf(e));

  /** Fan the per-entry endpoint out over the selection, then reload once. */
  const runBulk = async (wipe: boolean, verb: string) => {
    const targets = wipe ? picked.filter((e) => e.exists && !e.in_place) : picked;
    if (!targets.length) return;
    const results = await Promise.all(
      targets.map((e) =>
        api(`/api/recently-closed/${encodeURIComponent(e.id)}/forget`, { json: { wipe } })
          .then(() => "")
          .catch((err) => (err as Error).message)
      )
    );
    const failed = results.filter(Boolean);
    if (failed.length) setError(`${failed.length} ${verb} failed: ${failed[0]}`);
    toast(`${verb} ${results.length - failed.length}/${results.length} session(s)`);
    sel.clear();
    load();
  };

  const wipeSelected = async () => {
    // in-place sessions have no worktree of ours to remove, and a gone one is
    // already gone: both are dropped here rather than failing one call each.
    const targets = picked.filter((e) => e.exists && !e.in_place);
    const skipped = picked.length - targets.length;
    if (!targets.length) {
      alert(
        "None of the selected sessions has a worktree to wipe — they are in-place or already gone.\n\nUse Forget to drop them from this list."
      );
      return;
    }
    const msg =
      `Permanently delete ${targets.length} worktree${targets.length === 1 ? "" : "s"}?\n\n` +
      previewList(targets.map(label)) +
      "\n" +
      (skipped
        ? `\n${skipped} selected row${skipped === 1 ? " is" : "s are"} in-place or already gone and will be left alone.\n`
        : "") +
      "\nThis cannot be undone.";
    if (!confirm(msg)) return;
    await runBulk(true, "Wiped");
  };

  const forgetSelected = async () => {
    // Non-destructive on disk, but it drops the only Reopen handle these rows
    // have — worth one click to confirm at this scale.
    const msg =
      `Forget ${picked.length} closed session${picked.length === 1 ? "" : "s"}?\n\n` +
      previewList(picked.map(label)) +
      "\n\nWorktrees stay on disk; the rows leave this list, so Reopen is no longer offered.";
    if (!confirm(msg)) return;
    await runBulk(false, "Forgot");
  };

  return (
    <div
      id="recent-dialog"
      className="modal"
      onClick={(e) => {
        if (e.target === e.currentTarget) closeDialog();
      }}
    >
      <div id="recent-panel">
        <div className="ws-head">
          <h2>Recently closed</h2>
          <span id="recent-total" className="muted">
            {query ? `${data.length} of ${all.length}` : data.length} session
            {(query ? all.length : data.length) === 1 ? "" : "s"}
          </span>
          <button type="button" id="recent-refresh" onClick={load}>
            Refresh
          </button>
          <button type="button" id="recent-close" onClick={closeDialog}>
            Close
          </button>
        </div>
        <div className="dlg-filter-row">
          <SelectAllCheck
            state={sel.allState}
            onChange={sel.setAllVisible}
            label="Select every session shown"
          />
          <DialogFilter
            id="recent-filter"
            value={query}
            onChange={setQuery}
            placeholder="Filter by name, branch, session, or path…  ( Ctrl+F )"
            onEscape={closeDialog}
          />
          <SortPicker
            id="recent-sort"
            options={SORTS}
            pref={sort.pref}
            onKey={sort.setKey}
            onFlip={sort.flip}
          />
        </div>
        {!!picked.length && (
          <BulkRowBar
            count={picked.length}
            hiddenCount={sel.hiddenCount}
            noun="session"
            onClear={sel.clear}
          >
            <button
              type="button"
              title="Drop the selected rows from this list (worktrees stay on disk)"
              onClick={forgetSelected}
            >
              Forget selected
            </button>
            <button
              type="button"
              className="danger"
              title="Permanently delete the selected sessions' worktrees"
              onClick={wipeSelected}
            >
              Wipe worktrees
            </button>
          </BulkRowBar>
        )}
        <div id="recent-list">
          {list === null ? (
            <p className="muted">Loading…</p>
          ) : !data.length ? (
            <p className="muted">
              {query && all.length
                ? `No closed session matches “${query}”.`
                : "No recently closed sessions."}
            </p>
          ) : (
            data.map((e) => {
              const gone = !e.exists;
              const a = aliasOf(e);
              return (
                <div
                  className={"recent-row" + (sel.has(e.id) ? " picked" : "")}
                  key={e.id}
                >
                  <RowCheck
                    checked={sel.has(e.id)}
                    title="Select (Shift-click to extend the range)"
                    onToggle={(shift) => sel.toggle(e.id, shift)}
                  />
                  <div className="recent-info">
                    {/* The saved name (rename alias) is the headline when there is
                        one — it is the name the user gave this work, and it survives
                        the session on purpose (see clearStaleAlias). Otherwise the
                        BRANCH: it carries the descriptive tail
                        ("…/path-expansion-never-routes-cloudflare") that says what
                        the session was for. The bare title is the slug, which for an
                        ingested ticket is just its number. Deliberately NOT run
                        through displayBranch() — that strips the prefix, and here the
                        whole name is the point. */}
                    <span
                      className="recent-name"
                      title={[
                        a ? "Saved name: " + a : "",
                        e.branch ? "Branch: " + e.branch : "",
                        e.title ? "Session: " + e.title : "",
                        e.folder || "",
                      ]
                        .filter(Boolean)
                        .join("\n")}
                    >
                      {label(e)}
                    </span>
                    <span className="recent-sub">
                      {/* Keep the identities the headline displaced visible: the
                          branch says what the work was, the title is what Reopen
                          acts on and what the wipe confirmation names. */}
                      {a && e.branch && (
                        <span className="recent-slug muted">{e.branch}</span>
                      )}
                      {e.title && (a || e.branch) && (
                        <span className="recent-slug muted">{e.title}</span>
                      )}
                      {e.in_place && <span className="ws-badge kind">in-place</span>}
                      {e.provisioned && <span className="ws-badge kind">provisioned</span>}
                      {gone && <span className="ws-badge gone">worktree gone</span>}
                      <span className="recent-when muted">{fmtClosedAt(e.closed_at)}</span>
                    </span>
                  </div>
                  <div className="recent-actions">
                    <button
                      className="recent-reopen"
                      disabled={gone}
                      onClick={async () => {
                        try {
                          const inst = await api<Instance>(
                            `/api/recently-closed/${encodeURIComponent(e.id)}/reopen`,
                            { method: "POST" }
                          );
                          closeDialog();
                          await refreshInstances();
                          if (inst?.title) selectSession(inst.title);
                        } catch (err) {
                          alert("Reopen failed: " + (err as Error).message);
                          load();
                        }
                      }}
                    >
                      Reopen
                    </button>
                    {!e.in_place && !gone && (
                      <button
                        className="recent-wipe danger"
                        onClick={async () => {
                          if (
                            !confirm(
                              `Permanently delete the worktree for '${label(e)}'?\nThis cannot be undone.`
                            )
                          )
                            return;
                          try {
                            await api(`/api/recently-closed/${encodeURIComponent(e.id)}/forget`, {
                              json: { wipe: true },
                            });
                          } catch (err) {
                            alert("Wipe failed: " + (err as Error).message);
                            return;
                          }
                          load();
                        }}
                      >
                        Wipe worktree
                      </button>
                    )}
                    <button
                      className="recent-forget"
                      onClick={async () => {
                        try {
                          await api(`/api/recently-closed/${encodeURIComponent(e.id)}/forget`, {
                            json: { wipe: false },
                          });
                        } catch (err) {
                          alert("Forget failed: " + (err as Error).message);
                          return;
                        }
                        load();
                      }}
                    >
                      Forget
                    </button>
                  </div>
                </div>
              );
            })
          )}
        </div>
        <p id="recent-error" className="error">{error}</p>
      </div>
    </div>
  );
}
