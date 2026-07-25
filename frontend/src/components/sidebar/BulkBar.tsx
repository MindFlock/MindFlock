/** Bulk session actions bar (port of renderBulkBar/bulkAction, section 9).
 * Fans the well-tested per-session endpoints out over the checked titles. */

import type { Instance } from "../../api/types";
import { instApi } from "../../api/client";
import { queryClient, refreshInstances, useInstances } from "../../state/queries";
import { useUi } from "../../state/store";
import { toast } from "../../lib/toast";

export function BulkBar() {
  const { data: instances = [] } = useInstances();
  const ui = useUi();
  // Prune selections for sessions that vanished.
  const live = new Set(instances.map((i) => i.title));
  const selected = [...ui.bulkSelected].filter((t) => live.has(t));
  if (!selected.length) return null;
  const anyMissing = instances.some((i) => selected.includes(i.title) && i.workspace_missing);

  const finish = (titles: string[]) => {
    const s = useUi.getState();
    for (const t of titles) {
      if (s.bulkSelected.has(t)) s.toggleBulk(t);
      s.setHidden(t, false);
      if (s.focused === t) s.setFocused(null);
    }
  };

  const run = async (kind: "end" | "delete" | "cleanmissing") => {
    let titles = selected.slice();
    let suffix: string;
    let opts: RequestInit;
    let verb: string;
    if (kind === "end") {
      if (!confirm(`End ${titles.length} session(s)? Their worktrees are kept (reopen from Recent…).`))
        return;
      suffix = "/close";
      opts = { method: "POST" };
      verb = "Ended";
    } else if (kind === "delete") {
      if (!confirm(`Delete + wipe ${titles.length} session(s)? This removes their worktrees and branches.`))
        return;
      suffix = "";
      opts = { method: "DELETE" };
      verb = "Deleted";
    } else {
      titles = titles.filter((t) => instances.some((i) => i.title === t && i.workspace_missing));
      if (!titles.length) return;
      suffix = "";
      opts = { method: "DELETE" };
      verb = "Cleaned up";
      // Optimistic: the rows vanish now, the DELETEs finish behind them.
      queryClient.setQueryData<Instance[]>(["instances"], (prev) =>
        (prev || []).filter((i) => !titles.includes(i.title))
      );
    }
    const results = await Promise.all(
      titles.map((t) => instApi(t, suffix, opts).then(() => true).catch(() => false))
    );
    const ok = results.filter(Boolean).length;
    finish(titles);
    toast(`${verb} ${ok}/${titles.length} session(s)`);
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

  return (
    <div id="bulk-bar">
      <span className="bulk-count">{selected.length} selected</span>
      <div className="bulk-acts">
        <button title="End selected sessions (keeps their worktrees)" onClick={() => run("end")}>
          End
        </button>
        <button title="Hide selected windows" onClick={hide}>
          Hide
        </button>
        {anyMissing && (
          <button
            title="Remove selected sessions whose workspace is gone"
            onClick={() => run("cleanmissing")}
          >
            Clean up missing
          </button>
        )}
        <button
          className="danger"
          title="Delete selected + wipe their worktrees"
          onClick={() => run("delete")}
        >
          Delete + wipe
        </button>
        <button title="Clear selection" onClick={() => useUi.getState().clearBulk()}>
          Clear
        </button>
      </div>
    </div>
  );
}
