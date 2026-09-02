/** Recently closed — one page for closed sessions AND what they left on disk.
 *
 * The "Workspaces on disk" dialog used to be a second, near-identical list of
 * the same directories seen from the other end, so the two were folded into this
 * one: "can I have this work back" and "can I have this disk back" are the same
 * question asked about the same directory. Rows carry both identities, and the
 * server (GET /api/recent) leaves out what neither question applies to —
 * protected base clones and cache refreshers, and anything a live session is
 * working in, which is the sidebar's business. It says how many it withheld, and
 * the header repeats that rather than quietly under-reporting the disk.
 *
 * Per row: reopen / delete / forget. In the header: remove every UNUSED
 * worktree in one sweep — see recentRows.ts for the words it asks with, and
 * core/workspaces.py for why it can only ever take a worktree git generated. */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Instance } from "../../api/types";
import { api } from "../../api/client";
import { refreshInstances, refreshRecentlyClosed } from "../../state/queries";
import { useUi } from "../../state/store";
import { selectSession } from "../../lib/sessionActions";
import { humanSize } from "../../lib/format";
import { matchesTokens, searchTokens } from "../../lib/rowSearch";
import { previewList } from "../../lib/rowSelection";
import { sortRows } from "../../lib/rowSort";
import { toast } from "../../lib/toast";
import { DialogFilter } from "./DialogFilter";
import { BulkRowBar, RowCheck, SelectAllCheck, useRowSelection } from "./rowSelect";
import { SortPicker, useSortPref, type SortOption } from "./SortPicker";
import {
  dirNote,
  dirtyMessage,
  entryLabel,
  nothingMessage,
  pruneMessage,
  prunedMessage,
  rowSearchFields,
  rowSortValue,
  staleDays,
  sumBytes,
  whenText,
  whenTitle,
  type PruneResult,
  type RecentData,
  type RecentRow,
} from "./recentRows";

const SORTS: SortOption[] = [
  {
    key: "date",
    label: "Last used",
    defaultDir: "desc",
    asc: "oldest first",
    desc: "newest first",
  },
  { key: "name", label: "Branch / name", defaultDir: "asc", asc: "A → Z", desc: "Z → A" },
  {
    key: "size",
    label: "Size on disk",
    defaultDir: "desc",
    asc: "smallest first",
    desc: "largest first",
  },
];

/** A row whose directory this app may delete. An in-place session ran in the
 * user's OWN repo — that folder is never ours to remove (the server refuses it
 * too); a row whose directory is already gone has nothing to delete; and a
 * closed session can SHARE its worktree with a session that is still running
 * (a copy and its origin), where deleting the directory would pull the rug out
 * from under the live one — the server refuses that too. */
function deletable(e: RecentRow): boolean {
  return e.exists && !e.in_place && !!e.path && !e.active_session;
}

export function RecentDialog() {
  const open = useUi((s) => s.openDialog === "recent");
  const closeDialog = useUi((s) => s.closeDialog);
  // Saved names (rename aliases) are client-side and keyed by title — the
  // server's list has never heard of them, so the join happens here.
  const aliases = useUi((s) => s.aliases);
  const [data, setData] = useState<RecentData | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [query, setQuery] = useState("");
  const sort = useSortPref("mf_sort_recent", SORTS);
  const seq = useRef(0);

  /** `keepError` is for the paths that have something to say and then reload:
   * load() clears the error line first, so a "3 deletes failed: <why>" set
   * before it never reached the screen. */
  const load = useCallback(async (keepError = false) => {
    const mySeq = ++seq.current;
    if (!keepError) setError("");
    setData(null);
    let first: RecentData;
    try {
      first = await api<RecentData>("/api/recent");
    } catch (err) {
      // The guard belongs here too: a slow FAILED load must not blank a list a
      // later one already rendered (a Refresh during a server restart).
      if (mySeq !== seq.current) return;
      setError((err as Error).message);
      setData({
        rows: [],
        stale_days: 7,
        hidden: {
          protected: 0,
          protected_names: [],
          protected_bytes: 0,
          active: 0,
          active_titles: [],
        },
        roots: [],
      });
      return;
    }
    if (mySeq !== seq.current) return;
    setData(first);
    // Sizes are a `du` per row and take seconds on a cold page cache, so the
    // rows render first and the numbers land when they land (the same two-pass
    // load the disk manager used).
    let sized: RecentData;
    try {
      sized = await api<RecentData>("/api/recent?sizes=1");
    } catch {
      return;
    }
    if (mySeq !== seq.current) return;
    setData(sized);
  }, []);

  useEffect(() => {
    if (open) load();
  }, [open, load]);

  // Reopening the dialog must not silently hide rows behind last time's query.
  useEffect(() => {
    if (!open) setQuery("");
  }, [open]);

  const all = data?.rows || [];
  const aliasOf = useCallback(
    (e: RecentRow) => (e.title && aliases[e.title]) || "",
    [aliases]
  );
  // Sorting by size necessarily waits for the ?sizes=1 pass — until it lands
  // every size is null, which sortRows parks at the bottom, so the list holds
  // its previous order and then settles once. No flicker per row.
  const sorted = useMemo(
    () => sortRows(all, (e) => rowSortValue(e, sort.pref.key, aliasOf(e)), sort.pref.dir),
    [all, sort.pref, aliasOf]
  );
  const shown = useMemo(() => {
    const tokens = searchTokens(query);
    return sorted.filter((e) => matchesTokens(rowSearchFields(e, aliasOf(e)), tokens));
  }, [sorted, query, aliasOf]);

  const byId = useMemo(() => new Map(all.map((e) => [e.id, e])), [all]);
  // Both key lists follow the DISPLAY order: shift-ranges have to match what the
  // eye sees, and a bulk confirmation reads like the list it came from.
  const sel = useRowSelection(
    useMemo(() => sorted.map((e) => e.id), [sorted]),
    useMemo(() => shown.map((e) => e.id), [shown])
  );

  const totalBytes = useMemo(() => sumBytes(all), [all]);
  // Distinct directories, not rows: a worktree a session and its copy both
  // closed is two rows and one deletion (the sweep keys on the path), so
  // counting rows would advertise two and then remove one.
  const staleCount = useMemo(
    () => new Set(all.filter((e) => e.stale).map((e) => e.path)).size,
    [all]
  );

  if (!open) return null;

  const picked = sel.keys.map((id) => byId.get(id)).filter(Boolean) as RecentRow[];
  const label = (e: RecentRow) => entryLabel(e, aliasOf(e));
  const hidden = data?.hidden;
  const hiddenNote = [
    hidden?.protected
      ? `${hidden.protected} protected` +
        (hidden.protected_bytes ? ` (${humanSize(hidden.protected_bytes)})` : "")
      : "",
    hidden?.active ? `${hidden.active} in use` : "",
  ]
    .filter(Boolean)
    .join(" + ");

  /** Delete one row's directory. A closed session goes through its own entry, so
   * the row leaves the list in the same call — otherwise it would sit here
   * offering a Reopen that can only fail. */
  const removeOne = (e: RecentRow) =>
    e.source === "closed"
      ? api(`/api/recently-closed/${encodeURIComponent(e.id)}/forget`, {
          json: { wipe: true },
        })
      : api("/api/workspaces/delete", { json: { path: e.path } });

  /** Fan an action out over the selection, then reload once. */
  const runBulk = async (
    targets: RecentRow[],
    verb: string,
    act: (e: RecentRow) => Promise<unknown>
  ) => {
    if (!targets.length) return;
    setBusy(true);
    const results = await Promise.all(
      targets.map((e) =>
        act(e)
          .then(() => "")
          .catch((err) => (err as Error).message)
      )
    );
    setBusy(false);
    const failed = results.filter(Boolean);
    if (failed.length) setError(`${failed.length} ${verb} failed: ${failed[0]}`);
    toast(`${verb} ${results.length - failed.length}/${results.length} item(s)`);
    sel.clear();
    await load(failed.length > 0);
    refreshInstances();
    refreshRecentlyClosed();
  };

  const deleteSelected = async () => {
    const targets = picked.filter(deletable);
    const skipped = picked.length - targets.length;
    if (!targets.length) {
      alert(
        "None of the selected rows has a directory this app may delete — an in-place session runs in your own repo, and the others are already gone or still in use by a running session.\n\nUse Forget to drop a closed session from this list."
      );
      return;
    }
    const sized = targets.filter((e) => e.size_bytes != null);
    const sum = sumBytes(sized);
    const msg =
      `Permanently delete ${targets.length} director${targets.length === 1 ? "y" : "ies"}` +
      (sized.length ? ` (${humanSize(sum)}${sized.length < targets.length ? "+" : ""})` : "") +
      "?\n\n" +
      previewList(targets.map(label)) +
      "\n" +
      (skipped
        ? `\n${skipped} selected row${skipped === 1 ? " has" : "s have"} no directory this app may delete ` +
          `(an in-place session runs in your own repo, and one may be gone already or still in use by a ` +
          `running session) — ${skipped === 1 ? "it" : "they"} will be left alone.\n`
        : "") +
      (targets.every((e) => e.worktree)
        ? "\nA worktree's branch and commits stay in the repository it came from; anything uncommitted, and anything git ignores, does not.\n"
        : "\nSome of these are clones, not worktrees — for those, everything goes with the directory, committed or not.\n") +
      "\nThis cannot be undone.";
    if (!confirm(msg)) return;
    await runBulk(targets, "Deleted", removeOne);
  };

  const forgetSelected = async () => {
    const targets = picked.filter((e) => e.source === "closed");
    if (!targets.length) {
      alert(
        "None of the selected rows is a closed session — there is nothing to forget.\n\nUse Delete to remove a leftover directory from disk."
      );
      return;
    }
    // Non-destructive on disk, but it drops the only Reopen handle these rows
    // have — worth one click to confirm at this scale.
    const msg =
      `Forget ${targets.length} closed session${targets.length === 1 ? "" : "s"}?\n\n` +
      previewList(targets.map(label)) +
      "\n\nThe directories stay on disk (they come back as on-disk rows); the sessions leave this list, so Reopen is no longer offered.";
    if (!confirm(msg)) return;
    await runBulk(targets, "Forgot", (e) =>
      api(`/api/recently-closed/${encodeURIComponent(e.id)}/forget`, {
        json: { wipe: false },
      })
    );
  };

  /** Remove every worktree nothing has used for a week. Two round trips on
   * purpose: the first resolves the candidate list on the SERVER (no path this
   * page holds is ever what gets deleted), and the confirmation shows exactly
   * that list before the second call does anything. */
  const pruneUnused = async () => {
    setError("");
    setBusy(true);
    let pre: PruneResult;
    try {
      pre = await api<PruneResult>("/api/workspaces/prune-worktrees", {
        json: { dry_run: true },
      });
    } catch (err) {
      setBusy(false);
      setError((err as Error).message);
      return;
    }
    setBusy(false);
    if (!pre.candidates?.length) {
      alert(nothingMessage(pre));
      return;
    }
    if (!confirm(pruneMessage(pre))) return;
    let includeDirty = false;
    if (pre.dirty_count) {
      includeDirty = confirm(dirtyMessage(pre));
      // Every candidate holds uncommitted work and the user said no: there is
      // nothing left to delete, so don't fire a sweep that removes nothing.
      if (!includeDirty && pre.dirty_count === pre.candidates.length) return;
    }
    setBusy(true);
    let done: PruneResult;
    try {
      done = await api<PruneResult>("/api/workspaces/prune-worktrees", {
        json: { dry_run: false, include_dirty: includeDirty },
      });
    } catch (err) {
      setBusy(false);
      setError((err as Error).message);
      return;
    }
    setBusy(false);
    toast(prunedMessage(done));
    if (done.failed?.length) setError(`Could not remove: ${done.failed.join(", ")}`);
    await load(!!done.failed?.length);
    refreshInstances();
    refreshRecentlyClosed();
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
          <span
            id="recent-total"
            className="muted"
            title={
              hidden?.protected_names?.length
                ? "Not listed (protected, the engine needs them): " +
                  hidden.protected_names.join(", ")
                : ""
            }
          >
            {query ? `${shown.length} of ${all.length}` : all.length} item
            {(query ? all.length : shown.length) === 1 ? "" : "s"}
            {totalBytes ? ` · ${humanSize(totalBytes)}` : ""}
            {hiddenNote ? ` · ${hiddenNote} hidden` : ""}
          </span>
          <button
            type="button"
            id="recent-prune"
            data-caps="git"
            title={
              "Delete every worktree no session is using and nothing has touched in " +
              `${Math.round(data?.stale_days ?? 7)} days. Repositories and clones are never touched.`
            }
            disabled={busy}
            onClick={pruneUnused}
          >
            Remove unused worktrees{staleCount ? ` (${staleCount})` : ""}
          </button>
          <button type="button" id="recent-refresh" onClick={() => load()}>
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
            label="Select every row shown"
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
            noun="row"
            onClear={sel.clear}
          >
            <button
              type="button"
              title="Drop the selected closed sessions from this list (directories stay on disk)"
              disabled={busy}
              onClick={forgetSelected}
            >
              Forget selected
            </button>
            <button
              type="button"
              className="danger"
              title="Permanently delete the selected directories"
              disabled={busy}
              onClick={deleteSelected}
            >
              Delete from disk
            </button>
          </BulkRowBar>
        )}
        <div id="recent-list">
          {data === null ? (
            <p className="muted">Loading…</p>
          ) : !shown.length ? (
            <p className="muted">
              {query && all.length
                ? `Nothing matches “${query}”.`
                : "No closed sessions, and nothing left on disk."}
            </p>
          ) : (
            shown.map((e) => {
              const gone = !e.exists;
              const a = aliasOf(e);
              const days = e.stale ? staleDays(e) : null;
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
                        the session was for. Then the title, then the directory's own
                        name, which is all a row that was never a session has.
                        Deliberately NOT run through displayBranch() — that strips the
                        prefix, and here the whole name is the point. */}
                    <span
                      className="recent-name"
                      title={[
                        a ? "Saved name: " + a : "",
                        e.branch ? "Branch: " + e.branch : "",
                        e.title ? "Session: " + e.title : "",
                        e.path || "",
                      ]
                        .filter(Boolean)
                        .join("\n")}
                    >
                      {label(e)}
                    </span>
                    <span className="recent-sub">
                      {/* Keep the identities the headline displaced visible: the
                          branch says what the work was, the title is what Reopen
                          acts on and what a delete confirmation names. */}
                      {a && e.branch && (
                        <span className="recent-slug muted">{e.branch}</span>
                      )}
                      {e.title && (a || e.branch) && (
                        <span className="recent-slug muted">{e.title}</span>
                      )}
                      {/* Which directory — an in-place session's branch is
                          whatever its repo had checked out, so a column of them
                          all say "main" without this. */}
                      {dirNote(e, a) && (
                        <span className="recent-slug muted">{dirNote(e, a)}</span>
                      )}
                      {/* The directory's kind, then the flags. `in-place` is a
                          flag, so it is skipped here even if a payload puts it
                          in `kind` — two identical badges read as a bug. */}
                      {e.kind && e.kind !== "in-place" && (
                        <span className="ws-badge kind">{e.kind}</span>
                      )}
                      {e.in_place && <span className="ws-badge kind">in-place</span>}
                      {e.provisioned && <span className="ws-badge kind">provisioned</span>}
                      {gone && <span className="ws-badge gone">worktree gone</span>}
                      {/* Only ever a CLOSED row: the server withholds the disk
                          rows a live session is using. It means a still-running
                          session shares this directory, which is why there is no
                          Delete on this row. */}
                      {e.active_session && (
                        <span
                          className="ws-badge active"
                          title="A running session is still working in this directory"
                        >
                          in use: {e.active_session}
                        </span>
                      )}
                      {/* The rows the sweep would take, marked — so the button in
                          the header is checkable against the list instead of being
                          a number you have to trust. */}
                      {e.stale && (
                        <span
                          className="ws-badge stale"
                          title="No session is using this and nothing has touched it for over a week — Remove unused worktrees would take it"
                        >
                          unused {days}d
                        </span>
                      )}
                      <span className="recent-when muted" title={whenTitle(e)}>
                        {whenText(e)}
                      </span>
                      {e.size_bytes != null && (
                        <span className="recent-size muted">{humanSize(e.size_bytes)}</span>
                      )}
                    </span>
                  </div>
                  <div className="recent-actions">
                    {e.source === "closed" && (
                      <button
                        className="recent-reopen"
                        disabled={gone || busy}
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
                    )}
                    {deletable(e) && (
                      <button
                        className="recent-wipe danger"
                        disabled={busy}
                        title={
                          e.worktree
                            ? "Permanently delete this worktree directory (its branch and commits stay in the repository it came from)"
                            : "Permanently delete this directory"
                        }
                        onClick={async () => {
                          if (
                            !confirm(
                              `Permanently delete '${label(e)}'` +
                                (e.size_bytes != null ? ` (${humanSize(e.size_bytes)})` : "") +
                                "?\nThis cannot be undone."
                            )
                          )
                            return;
                          try {
                            await removeOne(e);
                          } catch (err) {
                            alert("Delete failed: " + (err as Error).message);
                            return;
                          }
                          load();
                          refreshInstances();
                          refreshRecentlyClosed();
                        }}
                      >
                        Delete
                      </button>
                    )}
                    {e.source === "closed" && (
                      <button
                        className="recent-forget"
                        disabled={busy}
                        title="Drop this closed session from the list (its directory stays on disk)"
                        onClick={async () => {
                          try {
                            await api(
                              `/api/recently-closed/${encodeURIComponent(e.id)}/forget`,
                              { json: { wipe: false } }
                            );
                          } catch (err) {
                            alert("Forget failed: " + (err as Error).message);
                            return;
                          }
                          load();
                          refreshRecentlyClosed();
                        }}
                      >
                        Forget
                      </button>
                    )}
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
