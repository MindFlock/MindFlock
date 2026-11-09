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
 * Shell tab is untouched. Mirrors PausedBody.
 *
 * A session whose agent CLI isn't installed (it exited 127 at launch) is held
 * too, but Start would only fail the same way (the server refuses it): the
 * placeholder says what is wrong and offers Install instead, until Setup reads
 * the CLI as installed — then Start comes back. */

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import type { Instance, SetupStatus } from "../../api/types";
import { agentName } from "../../lib/agentNames";
import { startSession } from "../../lib/revive";
import { heldLine } from "../../lib/heldOffline";
import { agentMissing } from "../../lib/sessionState";
import { queryClient } from "../../state/queries";
import { useUi } from "../../state/store";
import { openAgentSettings } from "../settings/screens/CodingCli";
import { installTarget } from "../setup/AgentBlocked";
import { SETUP_KEY } from "../setup/useSetup";

/** The placeholder's sentence when the held session's agent isn't installed. */
export function heldMissingLine(name: string): string {
  return (
    agentName(name) +
    " isn't installed on this computer, so this session's agent couldn't start. Install it, then Start it here."
  );
}

/** The agent `title`'s row runs whose CLI Setup reads as not installed, else
 * null. The row comes from the instances cache (the pane re-renders with
 * it); the Setup read is subscribed to, so Start comes back once installed.
 * The explicit client: this body also renders outside a provider (tests). */
function useMissingAgent(title: string): { name: string; isDefault: boolean } | null {
  const { data: setup } = useQuery(
    {
      queryKey: SETUP_KEY,
      queryFn: () => api<SetupStatus>("/api/setup"),
      staleTime: 30_000,
    },
    queryClient,
  );
  const rows = queryClient.getQueryData<Instance[]>(["instances"]) || [];
  const inst = rows.find((r) => r.title === title);
  if (!inst) return null;
  const name = agentMissing(inst, setup?.logins, "offline");
  if (!name) return null;
  return {
    name,
    isDefault: installTarget(name, setup?.default_provider || "") === "setup",
  };
}

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
  const missing = useMissingAgent(title);
  return (
    <div className={"pane-term agent-term pane-paused pane-held" + (hidden ? " hidden" : "")}>
      <div className="paused-body held-body" role="status">
        <p className="paused-head">Offline</p>
        {missing ? (
          <>
            <p className="paused-text">{heldMissingLine(missing.name)}</p>
            <button
              className="paused-resume held-install"
              type="button"
              title={
                "Open " + (missing.isDefault ? "Setup" : "Settings → Agents") + " to install " + agentName(missing.name)
              }
              onClick={(e) => {
                e.stopPropagation();
                if (missing.isDefault) useUi.getState().openDialogFor("setup");
                else openAgentSettings(missing.name);
              }}
            >
              Install {agentName(missing.name)}…
            </button>
          </>
        ) : (
          <>
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
          </>
        )}
      </div>
    </div>
  );
}
