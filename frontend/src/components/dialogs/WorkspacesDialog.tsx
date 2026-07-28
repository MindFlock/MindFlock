/** Workspaces / disk manager (port of loadWorkspaces, section 20). Rows
 * render first; per-dir sizes (server-side du) fill in when ready. */

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../api/client";
import { refreshInstances } from "../../state/queries";
import { useUi } from "../../state/store";
import { humanSize } from "../../lib/format";
import { toast } from "../../lib/toast";

interface Workspace {
  name: string;
  path: string;
  kind: string;
  active_session?: string | null;
  size_bytes?: number | null;
}

export function WorkspacesDialog() {
  const open = useUi((s) => s.openDialog === "workspaces");
  const closeDialog = useUi((s) => s.closeDialog);
  const [ws, setWs] = useState<Workspace[] | null>(null);
  const [total, setTotal] = useState("");
  const [error, setError] = useState("");
  const [clearBusy, setClearBusy] = useState(false);
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

  if (!open) return null;

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
          "running session.\n\nThis cannot be undone."
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
          <span id="ws-total" className="muted">{total}</span>
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
        <div id="ws-list">
          {ws === null ? (
            <p className="muted">Loading…</p>
          ) : !ws.length && !error ? (
            <p className="muted">No workspace directories on disk.</p>
          ) : (
            ws.map((w) => {
              const isProtected = w.kind === "base" || w.kind === "refresher";
              return (
                <div className="ws-row" key={w.path}>
                  <div className="ws-info">
                    <span className="ws-name" title={w.path}>{w.name}</span>
                    <span className="ws-badge kind">{w.kind}</span>
                    {w.active_session && (
                      <span className="ws-badge active">active: {w.active_session}</span>
                    )}
                  </div>
                  <span className="ws-size">
                    {w.size_bytes == null ? "…" : humanSize(w.size_bytes)}
                  </span>
                  {isProtected ? (
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
