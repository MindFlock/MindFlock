/** Bulk session actions bar (port of renderBulkBar/bulkAction, section 9).
 *
 * Select mode (store `bulkMode`) starts from the rail itself: Ctrl/⌘-click a
 * row, Shift-click a range, or a row menu's **Select**. The picked rows are
 * highlighted where they sit — no checkboxes (tests/unit/test_frontend_bulk.py
 * pins that) and no second copy of the list — and this strip at the top of the
 * list fans the per-session endpoints out over them. Esc leaves select mode.
 * The two end verbs are the row menu's:
 * **Close** (keeps worktrees) and **Delete…** (removes worktrees and branches),
 * each asked on an inline strip — never a native confirm(), which the desktop
 * app does not implement (it silently meant "no" and the bar did nothing). */

import { useEffect, useState } from "react";
import type { Instance } from "../../api/types";
import { instApi } from "../../api/client";
import { queryClient, refreshInstances, useInstances } from "../../state/queries";
import { displayName, useUi } from "../../state/store";
import { isEditingTarget } from "../../lib/keymap";
import { toast } from "../../lib/toast";
import { errorPop } from "../../lib/errorPop";
import { releaseTerms } from "../../lib/terminals";
import {
  folderInUse,
  folderInUseText,
  keptRunningText,
  type EndResult,
} from "../../lib/sessionActions";

type Ask = "close" | "delete" | null;

export function BulkBar() {
  const { data: instances = [] } = useInstances();
  const bulkMode = useUi((s) => s.bulkMode);
  const bulkSelected = useUi((s) => s.bulkSelected);
  const [ask, setAsk] = useState<Ask>(null);
  const [busy, setBusy] = useState(false);
  // Sessions holding a folder the last Delete could not remove (409).
  const [held, setHeld] = useState<string[]>([]);
  useEffect(() => {
    if (!bulkMode) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || e.defaultPrevented || isEditingTarget(e.target as Element)) return;
      if (useUi.getState().openDialog) return;
      e.preventDefault();
      setAsk(null);
      useUi.getState().setBulkMode(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [bulkMode]);
  if (!bulkMode) return null;

  // A provisioning row has nothing to act on yet; everything else is listed.
  const rows = instances.filter((i) => !i.pending);
  const live = new Set(rows.map((i) => i.title));
  // Prune selections for sessions that vanished.
  const selected = rows.filter((i) => bulkSelected.has(i.title)).map((i) => i.title);
  const n = selected.length;
  const anyMissing = rows.some((i) => selected.includes(i.title) && i.workspace_missing);

  const finish = (titles: string[]) => {
    const s = useUi.getState();
    s.setBulk(titles, false);
    for (const t of titles) {
      s.setHidden(t, false);
      if (s.focused === t) s.setFocused(null);
      releaseTerms(t);
    }
  };

  /** Fan one endpoint out over `titles`; tolerate individual failures and say
   * how many landed, how many workers stayed up, and which folders are held. */
  const run = async (kind: "close" | "delete" | "cleanmissing") => {
    let titles = selected.slice();
    let suffix = "";
    let opts: RequestInit = { method: "POST" };
    let verb = "Closed";
    if (kind === "close") {
      suffix = "/close";
    } else if (kind === "delete") {
      suffix = "/cleanup";
      verb = "Deleted";
    } else {
      titles = titles.filter((t) => rows.some((i) => i.title === t && i.workspace_missing));
      if (!titles.length) return;
      opts = { method: "DELETE" };
      verb = "Deleted";
      // Optimistic: the rows vanish now, the DELETEs finish behind them.
      queryClient.setQueryData<Instance[]>(["instances"], (prev) =>
        (prev || []).filter((i) => !titles.includes(i.title))
      );
    }
    setAsk(null);
    setBusy(true);
    const inUse: string[] = [];
    const failed: string[] = [];
    let kept = 0;
    const done: string[] = [];
    await Promise.all(
      titles.map(async (t) => {
        try {
          const r = await instApi<EndResult>(t, suffix, opts);
          done.push(t);
          kept += r?.children?.length || 0;
        } catch (err) {
          const h = folderInUse(err);
          if (h) inUse.push(...h.filter((x) => !inUse.includes(x)));
          else failed.push(t);
        }
      })
    );
    setBusy(false);
    finish(done);
    setHeld(inUse);
    const keptText = keptRunningText({ children: new Array(kept).fill("") });
    toast(`${verb} ${done.length}/${titles.length} session(s)` + (keptText ? " · " + keptText : ""), {
      duration: keptText ? 5000 : undefined,
    });
    if (failed.length)
      errorPop(
        `${verb === "Closed" ? "Close" : "Delete"} failed for ${failed.length} session(s)`,
        failed.map((t) => displayName(t)).join(", ")
      );
    await refreshInstances();
  };

  const hide = () => {
    const hid = selected.slice();
    const s = useUi.getState();
    for (const t of hid) {
      s.setHidden(t, true);
      if (s.focused === t) s.setFocused(null);
    }
    s.clearBulk();
    toast(`Hid ${hid.length} session(s) — click to undo`, {
      onClick: () => {
        const st = useUi.getState();
        for (const t of hid) st.setHidden(t, false);
      },
      duration: 5000,
    });
  };

  const setAll = (on: boolean) => useUi.getState().setBulk([...live], on);

  return (
    <div id="bulk-bar" className="bulk-open" role="group" aria-label="Bulk session actions">
      <div className="bulk-head">
        <span className="bulk-count">
          {n ? `${n} selected` : "Click sessions to select them"}
        </span>
        <div className="bulk-acts">
          <button type="button" onClick={() => setAll(n !== rows.length)} disabled={!rows.length}>
            {n === rows.length && n ? "None" : "All"}
          </button>
          <button
            type="button"
            className="bulk-done"
            title="Leave select mode (Esc)"
            aria-label="Leave select mode"
            onClick={() => useUi.getState().setBulkMode(false)}
          >
            ✕
          </button>
        </div>
      </div>
      {ask ? (
        <div className="bulk-ask" role="group" aria-label={ask === "close" ? "Close selected?" : "Delete selected?"}>
          <span className="bulk-q">
            {ask === "close"
              ? `Close ${n} session(s)? Keeps their worktrees — reopen from Recently closed.`
              : `Delete ${n} session(s)? Removes their worktrees and branches.`}
          </span>
          <span className="bulk-acts">
            <button
              type="button"
              className={ask === "delete" ? "danger" : undefined}
              disabled={busy}
              onClick={() => void run(ask)}
            >
              {ask === "close" ? "Close" : "Delete"}
            </button>
            {/* Focus lands on the harmless answer: Enter never destroys. */}
            <button type="button" autoFocus onClick={() => setAsk(null)}>
              Cancel
            </button>
          </span>
        </div>
      ) : (
        <div className="bulk-acts bulk-verbs">
          <button
            type="button"
            title="Close selected sessions (keeps their worktrees)"
            disabled={!n || busy}
            onClick={() => setAsk("close")}
          >
            Close
          </button>
          <button type="button" title="Hide selected windows" disabled={!n || busy} onClick={hide}>
            Hide
          </button>
          {anyMissing && (
            <button
              type="button"
              title="Remove selected sessions whose workspace is gone"
              disabled={busy}
              onClick={() => void run("cleanmissing")}
            >
              Delete missing
            </button>
          )}
          <button
            type="button"
            className="danger"
            title="Delete selected sessions — removes their worktrees and branches (asks first)"
            disabled={!n || busy}
            onClick={() => setAsk("delete")}
          >
            Delete…
          </button>
        </div>
      )}
      {held.length > 0 && (
        <div className="bulk-held" role="status">
          <span>{folderInUseText(held)}</span>
          <button type="button" className="bulk-held-x" aria-label="Dismiss" onClick={() => setHeld([])}>
            ✕
          </button>
        </div>
      )}
    </div>
  );
}
