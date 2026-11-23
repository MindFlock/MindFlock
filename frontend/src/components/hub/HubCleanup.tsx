/** Settings → Disk cleanup's hub line (components/settings/screens/
 * Cleanup.tsx): how much the hub holds, and one button that removes every
 * unpinned item now (POST /api/hub/clear) — asked first in-page. */

import { useState } from "react";
import { api } from "../../api/client";
import { errMsg } from "../../lib/format";
import { humanSize, refreshHub, useHub, useHubUi } from "../../lib/hub";
import { toast } from "../../lib/toast";
import { InlineConfirm } from "../InlineConfirm";

export function HubCleanup() {
  const q = useHub(true);
  const [asking, setAsking] = useState(false);
  const [busy, setBusy] = useState(false);
  const l = q.data;
  if (!l) return null;
  const pinned = l.items.filter((i) => i.pinned).length;
  const unpinned = l.items.length - pinned;
  const clear = async () => {
    setBusy(true);
    try {
      const r = await api<{ removed: number; freed: number }>("/api/hub/clear", { method: "POST" });
      toast("Removed " + r.removed + (r.removed === 1 ? " item" : " items") + " from the hub — " + humanSize(r.freed) + " freed");
      refreshHub();
    } catch (err) {
      toast("Couldn't clear the hub: " + errMsg(err));
    } finally {
      setBusy(false);
      setAsking(false);
    }
  };
  return (
    <div className="set-row" id="cleanup-hub">
      <span className="set-label">Hub</span>
      <span className="cleanup-summary">
        {l.count ? (
          <>
            {l.count} {l.count === 1 ? "item" : "items"}, {humanSize(l.used)}
            {pinned ? " (" + pinned + " pinned)" : ""} — unpinned items go by themselves {l.keep_days} days after their last use.
          </>
        ) : (
          "The hub is empty."
        )}
      </span>
      <span className="test-row">
        <button type="button" className="test-btn" onClick={() => useHubUi.getState().setOpen(true)}>
          Open the hub
        </button>
        {unpinned > 0 && (
          <button type="button" className="test-btn" disabled={busy} onClick={() => setAsking(true)}>
            Remove unpinned items
          </button>
        )}
      </span>
      {asking && (
        <InlineConfirm
          title={"Remove " + unpinned + " unpinned " + (unpinned === 1 ? "item" : "items") + "?"}
          body="Their references stop working in every session. Pinned items stay."
          confirmLabel="Remove them"
          busy={busy}
          onConfirm={() => void clear()}
          onCancel={() => setAsking(false)}
        />
      )}
    </div>
  );
}
