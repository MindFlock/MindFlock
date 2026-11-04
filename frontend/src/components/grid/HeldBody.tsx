/** The Agent tab of a session HELD offline after a crash: a calm placeholder
 * in place of the terminal, with the one way back — Start.
 *
 * After a crash (the server restarting, the OOM killer) every pane used to
 * relaunch the agent it attached to, and on a machine that had just run out
 * of memory that refilled it and crashed again. The server now holds such a
 * session (backend core/held.py) and refuses its terminal socket with close
 * code 4424; this is what the pane shows instead, and it opens no socket.
 * Start (POST /api/instances/{title}/revive) releases the hold and launches
 * the agent; on success the pane mounts a fresh terminal (`onStarted`). The
 * Shell tab is untouched. Mirrors PausedBody. */

import { useState } from "react";
import { startSession } from "../../lib/revive";
import { heldLine } from "../../lib/heldOffline";

export function HeldBody({
  title,
  reason,
  hidden,
  onStarted,
}: {
  title: string;
  reason?: string | null;
  hidden: boolean;
  /** The server took the Start: mount the terminal again. */
  onStarted?: () => void;
}) {
  const [busy, setBusy] = useState(false);
  return (
    <div className={"pane-term agent-term pane-paused pane-held" + (hidden ? " hidden" : "")}>
      <div className="paused-body held-body" role="status">
        <p className="paused-head">Offline</p>
        <p className="paused-text">{heldLine(reason)}</p>
        <button
          className="paused-resume held-start"
          type="button"
          disabled={busy}
          title="Start the agent again in the same worktree, resuming its conversation"
          onClick={(e) => {
            e.stopPropagation();
            setBusy(true);
            void startSession(title).then((r) => {
              setBusy(false);
              // Refused: the toast says why, and the button comes back. Taken:
              // the toast carries the answer's memory warning, if any.
              if (r) onStarted?.();
            });
          }}
        >
          {busy ? "Starting…" : "▶ Start"}
        </button>
      </div>
    </div>
  );
}
