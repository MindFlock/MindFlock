/** The Agent tab of a PAUSED session: a calm placeholder in place of the
 * terminal, with the way back.
 *
 * Pause stops the agent and keeps everything else (the worktree, the branch,
 * the conversation). The pane used to mount the agent terminal anyway, and the
 * server relaunched any agent whose worktree existed the moment a socket
 * attached — so pausing a session you were looking at brought its agent back
 * within a second while the row still said Paused. The server now refuses that
 * socket (close code 4423); this is what the pane shows instead, and it opens
 * no socket at all. The Shell tab keeps working: Pause stops only the agent. */

import { useState } from "react";
import { resumeSession } from "../../lib/sessionActions";

/** The sentence under "Paused". */
export const PAUSED_TEXT = "The agent is stopped; your worktree and conversation are kept.";

export function PausedBody({ title, hidden }: { title: string; hidden: boolean }) {
  const [busy, setBusy] = useState(false);
  return (
    <div className={"pane-term agent-term pane-paused" + (hidden ? " hidden" : "")}>
      <div className="paused-body" role="status">
        <p className="paused-head">Paused</p>
        <p className="paused-text">{PAUSED_TEXT}</p>
        <button
          className="paused-resume"
          type="button"
          disabled={busy}
          title="Resume — starts the agent again in the same worktree"
          onClick={(e) => {
            e.stopPropagation();
            setBusy(true);
            // The same action as the header's ▶ Resume and the row menu's.
            // Once the status reads running again the pane mounts the
            // terminal on its own; a failure says why (errorPop) and the
            // button comes back.
            void resumeSession(title).finally(() => setBusy(false));
          }}
        >
          {busy ? "Resuming…" : "▶ Resume"}
        </button>
      </div>
    </div>
  );
}
