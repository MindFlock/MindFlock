/** Workspaces / disk manager (port of loadWorkspaces, section 20). Rows
 * render first; per-dir sizes (server-side du) fill in when ready. */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../../api/client";
import { refreshInstances } from "../../state/queries";
import { useUi } from "../../state/store";
import { humanSize, relTime } from "../../lib/format";
import { toast } from "../../lib/toast";
import { matchesTokens, searchTokens } from "../../lib/rowSearch";
import { previewList } from "../../lib/rowSelection";
import { sortRows } from "../../lib/rowSort";
import { DialogFilter } from "./DialogFilter";
import { BulkRowBar, RowCheck, SelectAllCheck, useRowSelection } from "./rowSelect";
import { SortPicker, useSortPref, type SortOption } from "./SortPicker";

interface Workspace {
  name: string;
  path: string;
  kind: string;
  active_session?: string | null;
  size_bytes?: number | null;
  /** Directory mtime in epoch SECONDS (Python's os.stat), not milliseconds. */
  mtime?: number | null;
}

const SORTS: SortOption[] = [
  { key: "name", label: "Name", defaultDir: "asc", asc: "A → Z", desc: "Z → A" },
  {
    key: "date",
    label: "Last modified",
    defaultDir: "desc",
    asc: "oldest first",
    desc: "newest first",
  },
  {
    key: "size",
    label: "Size on disk",
    defaultDir: "desc",
    asc: "smallest first",
    desc: "largest first",
  },
];

/** The engine needs these back if they go, so the UI never offers to delete
 * them — no button, and no checkbox. */
function isProtected(w: Workspace): boolean {
  return w.kind === "base" || w.kind === "refresher";
}

export function WorkspacesDialog() {
  const open = useUi((s) => s.openDialog === "workspaces");
  const closeDialog = useUi((s) => s.closeDialog);
  const [ws, setWs] = useState<Workspace[] | null>(null);
  const [total, setTotal] = useState("");
  const [error, setError] = useState("");
  const [clearBusy, setClearBusy] = useState(false);
  const [query, setQuery] = useState("");
  const sort = useSortPref("mf_sort_workspaces", SORTS);
  const seq = useRef(0);

  const load = useCallback(async () => {
    const mySeq = ++seq.current;
    setError("");
    setWs(null);
    let data: { workspaces?: Workspace[] };
    try {
      data = await api("/api/workspaces");
    } catch (e) {
      setWs([]);
      setError((e as Error).message);
      return;
    }
    if (mySeq !== seq.current) return;
    const list = data.workspaces || [];
    setWs(list);
    setTotal(list.length + " dir" + (list.length === 1 ? "" : "s"));
    // Sizes can take seconds on a cold tree — fill in when ready.
    let sized: { workspaces?: Workspace[] };
    try {
      sized = await api("/api/workspaces?sizes=1");
    } catch {
      return;
    }
    if (mySeq !== seq.current) return;
    const byPath = new Map((sized.workspaces || []).map((s) => [s.path, s.size_bytes || 0]));
    let sum = 0;
    for (const v of byPath.values()) sum += v;
    setWs(list.map((w) => ({ ...w, size_bytes: byPath.get(w.path) ?? w.size_bytes })));
    setTotal(list.length + " dir" + (list.length === 1 ? "" : "s") + " · " + humanSize(sum));
  }, []);

  useEffect(() => {
    if (open) load();
  }, [open, load]);

  // Reopening the dialog must not silently hide rows behind last time's query.
  useEffect(() => {
    if (!open) setQuery("");
  }, [open]);

  // Sorting by size necessarily waits for the ?sizes=1 pass — until it lands
  // every size is null, which sortRows parks at the bottom, so the list simply
  // holds its name order and then settles once. No flicker per row.
  const sorted = useMemo(
    () =>
      sortRows(
        ws || [],
        (w) =>
          sort.pref.key === "name" ? w.name : sort.pref.key === "size" ? w.size_bytes : w.mtime,
        sort.pref.dir
      ),
    [ws, sort.pref]
  );

  // The path is searchable even though the row only shows it on hover: a
  // workspace is often easiest to find by the repo directory it sits under.
  const shown = useMemo(() => {
    const tokens = searchTokens(query);
    return sorted.filter((w) => matchesTokens([w.name, w.path, w.kind, w.active_session], tokens));
  }, [sorted, query]);

  // Protected dirs have no Delete button, so they get no checkbox either —
  // otherwise "select all" would tick rows the bulk action must then silently
  // skip. Keyed by path: two worktrees under different repos share a name.
  const byPath = useMemo(() => new Map((ws || []).map((w) => [w.path, w])), [ws]);
  // Display order, so a shift-range matches the eye and a delete confirmation
  // lists rows the way the dialog does.
  const selectable = useMemo(
    () => sorted.filter((w) => !isProtected(w)).map((w) => w.path),
    [sorted]
  );
  const selectableShown = useMemo(
    () => shown.filter((w) => !isProtected(w)).map((w) => w.path),
    [shown]
  );
  const sel = useRowSelection(selectable, selectableShown);

  if (!open) return null;

  const picked = sel.keys.map((p) => byPath.get(p)).filter(Boolean) as Workspace[];

  const delSelected = async () => {
    if (!picked.length) return;
    const active = picked.filter((w) => w.active_session);
    const sized = picked.filter((w) => w.size_bytes != null);
    const sum = sized.reduce((n, w) => n + (w.size_bytes || 0), 0);
    // A bulk delete is the one action here with no undo, so the confirmation
    // names the rows: the count alone can't be checked against what was ticked.
    const msg =
      `Permanently delete ${picked.length} workspace${picked.length === 1 ? "" : "s"}` +
      (sized.length ? ` (${humanSize(sum)}${sized.length < picked.length ? "+" : ""})` : "") +
      "?\n\n" +
      previewList(picked.map((w) => w.name)) +
      "\n" +
      (active.length
        ? `\n${active.length} of these ${active.length === 1 ? "is an ACTIVE session" : "are ACTIVE sessions"} ` +
          `(${active.map((w) => w.active_session).join(", ")}) — ${active.length === 1 ? "it" : "they"} will be killed first.\n`
        : "") +
      "\nThis cannot be undone.";
    if (!confirm(msg)) return;
    setError("");
    setClearBusy(true);
    const results = await Promise.all(
      picked.map((w) =>
        api("/api/workspaces/delete", { json: { path: w.path } })
          .then(() => "")
          .catch((e) => (e as Error).message)
      )
    );
    setClearBusy(false);
    const failed = results.filter(Boolean);
    if (failed.length) setError(`${failed.length} delete(s) failed: ${failed[0]}`);
    toast(`Deleted ${results.length - failed.length}/${results.length} workspace(s)`);
    sel.clear();
    await load();
    refreshInstances();
  };

  const del = async (w: Workspace) => {
    const szNote = w.size_bytes == null ? "" : " (" + humanSize(w.size_bytes) + ")";
    let msg = `Permanently delete '${w.name}'${szNote}?`;
    if (w.active_session)
      msg = `'${w.name}' is the ACTIVE session '${w.active_session}'. Kill it and delete the directory?`;
    else if (w.kind === "base")
      msg =
        "This is the shared base clone — deleting it forces a fresh clone on the next worktree session. Continue?";
    else if (w.kind === "refresher")
      msg =
        "This is the cache refresher workspace — deleting it re-seeds the test cache on the next refresh. Continue?";
    if (!confirm(msg + "\nThis cannot be undone.")) return;
    try {
      await api("/api/workspaces/delete", { json: { path: w.path } });
    } catch (e) {
      alert("Delete failed: " + (e as Error).message);
      return;
    }
    await load();
    refreshInstances();
  };

  const clearAll = async () => {
    if (
      !confirm(
        "Delete ALL unprotected, idle workspaces?\n\n" +
          "Kept: protected base clones / cache refreshers, and any workspace with a " +
          "running session.\n" +
          // The filter narrows the LIST, not the sweep — a user who filtered to
          // three rows and then hit this button would otherwise lose everything.
          (query ? "\nThe filter does not limit this — every unprotected, idle workspace goes.\n" : "") +
          "\nThis cannot be undone."
      )
    )
      return;
    setError("");
    setClearBusy(true);
    let r: { removed_count?: number; kept_active?: string[] };
    try {
      r = await api("/api/workspaces/clear", { json: {} });
    } catch (e) {
      setError((e as Error).message);
      setClearBusy(false);
      return;
    }
    setClearBusy(false);
    const n = r?.removed_count || 0;
    const kept = r?.kept_active || [];
    let msg = n ? `Removed ${n} workspace${n === 1 ? "" : "s"}` : "Nothing to remove";
    if (kept.length) msg += ` · kept ${kept.length} active session${kept.length === 1 ? "" : "s"}`;
    toast(msg);
    await load();
    refreshInstances();
  };

  return (
    <div
      id="workspaces-dialog"
      className="modal"
      onClick={(e) => {
        if (e.target === e.currentTarget) closeDialog();
      }}
    >
      <div id="workspaces-panel">
        <div className="ws-head">
          <h2>Workspaces on disk</h2>
          <span id="ws-total" className="muted">
            {/* `total` is empty until the first load lands (and after a failed
                one) — "3 of " would be worse than just the count. */}
            {query && total ? `${shown.length} of ${total}` : total}
          </span>
          <button
            type="button"
            id="ws-clear"
            title="Delete all unprotected, idle workspaces (keeps protected dirs and any running session)"
            disabled={clearBusy}
            onClick={clearAll}
          >
            Clear unprotected
          </button>
          <button type="button" id="ws-refresh" onClick={load}>
            Refresh
          </button>
          <button type="button" id="ws-close" onClick={closeDialog}>
            Close
          </button>
        </div>
        <div className="dlg-filter-row">
          <SelectAllCheck
            state={sel.allState}
            onChange={sel.setAllVisible}
            label="Select every deletable workspace shown"
          />
          <DialogFilter
            id="ws-filter"
            value={query}
            onChange={setQuery}
            placeholder="Filter by name, path, or session…  ( Ctrl+F )"
            onEscape={closeDialog}
          />
          <SortPicker
            id="ws-sort"
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
            noun="workspace"
            onClear={sel.clear}
          >
            <button
              type="button"
              className="danger"
              title="Permanently delete the selected workspaces"
              disabled={clearBusy}
              onClick={delSelected}
            >
              Delete selected
            </button>
          </BulkRowBar>
        )}
        <div id="ws-list">
          {ws === null ? (
            <p className="muted">Loading…</p>
          ) : !shown.length && !error ? (
            <p className="muted">
              {query && ws.length
                ? `No workspace matches “${query}”.`
                : "No workspace directories on disk."}
            </p>
          ) : (
            shown.map((w) => {
              const prot = isProtected(w);
              return (
                <div className={"ws-row" + (sel.has(w.path) ? " picked" : "")} key={w.path}>
                  <RowCheck
                    checked={sel.has(w.path)}
                    disabled={prot}
                    title={
                      prot
                        ? "Protected — cannot be deleted"
                        : "Select (Shift-click to extend the range)"
                    }
                    onToggle={(shift) => sel.toggle(w.path, shift)}
                  />
                  <div className="ws-info">
                    <span className="ws-name" title={w.path}>{w.name}</span>
                    <span className="ws-badge kind">{w.kind}</span>
                    {w.active_session && (
                      <span className="ws-badge active">active: {w.active_session}</span>
                    )}
                  </div>
                  {/* Sorting by a column you can't see is unverifiable, so the
                      date the sort uses is on the row. Relative, with the exact
                      stamp on hover. */}
                  <span
                    className="ws-when muted"
                    title={w.mtime ? "Modified " + new Date(w.mtime * 1000).toLocaleString() : ""}
                  >
                    {w.mtime ? relTime(w.mtime) : ""}
                  </span>
                  <span className="ws-size">
                    {w.size_bytes == null ? "…" : humanSize(w.size_bytes)}
                  </span>
                  {prot ? (
                    <span className="ws-protected" title="Protected — needed by the engine">
                      protected
                    </span>
                  ) : (
                    <button className="ws-del" onClick={() => del(w)}>
                      Delete
                    </button>
                  )}
                </div>
              );
            })
          )}
        </div>
        <p id="ws-error" className="error">{error}</p>
      </div>
    </div>
  );
}
