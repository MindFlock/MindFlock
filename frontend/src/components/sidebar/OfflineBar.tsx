/** "N agents offline · Restart all" — the Offline chip's Restart, for every
 * row that offers it. After the OOM killer (or a crash) takes several agents
 * down at once, clicking each chip in turn is the chore this removes.
 *
 * It runs the server's revive sweep (POST /api/server/revive-all,
 * backend/web/core/revive.py) rather than fanning the per-row endpoint out:
 * the sweep launches two at a time with a breather, waits for memory
 * headroom between launches (relaunching everything straight into the
 * pressure that killed it is how it happens again) and tells an agent that
 * died mid-turn to continue where it left off. Shown from two offline rows
 * up; one row's own chip is the button for one. */

import { useState } from "react";
import { api } from "../../api/client";
import { refreshInstances, useInstances, useMemory } from "../../state/queries";
import { deriveSessionState, forceActivity } from "../../lib/sessionState";
import { toast } from "../../lib/toast";
import { errMsg } from "../../lib/format";

interface SweepResult {
  revived?: string[];
  continued?: string[];
  skipped?: Record<string, string>;
  summary?: string;
}

/** The phrase a finished sweep is toasted with, about the rows the bar
 * counted (`offline`): the sweep also reports every paused or already-running
 * session it passed over, which this button never offered. Exported for tests. */
export function sweepToast(r: SweepResult, offline: string[]): string {
  const revived = r.revived?.length || 0;
  const continued = r.continued?.length || 0;
  const skipped = r.skipped || {};
  const mine = offline.filter((t) => skipped[t] !== undefined);
  const memory = mine.filter((t) => skipped[t] === "low memory").length;
  let line = revived ? "Restarted " + revived + (revived === 1 ? " agent" : " agents") : "No agents restarted";
  if (continued) line += " — " + continued + " told to continue where " + (continued === 1 ? "it" : "they") + " left off";
  if (memory) line += "; " + memory + " held back: memory is low";
  const other = mine.filter((t) => skipped[t] !== "low memory");
  if (other.length) {
    const why = Array.from(new Set(other.map((t) => skipped[t]))).join(", ");
    line += "; " + other.length + " couldn't restart (" + why + ")";
  }
  return line;
}

export function OfflineBar() {
  const { data: instances = [] } = useInstances();
  const memory = useMemory().data;
  const [busy, setBusy] = useState(false);
  const offline = instances.filter((i) => {
    if (i.device || i.pending) return false;
    const ss = deriveSessionState(i);
    return ss.state === "offline" && ss.restartable && i.status !== "paused";
  });
  if (offline.length < 2 && !busy) return null;

  const titles = offline.map((i) => i.title);
  const low = memory?.level === "low" || memory?.level === "critical";
  const run = async () => {
    if (busy) return;
    setBusy(true);
    try {
      const r = await api<SweepResult>("/api/server/revive-all", {
        method: "POST",
        json: { titles },
      });
      for (const t of r.revived || []) forceActivity(t, "idle");
      toast(sweepToast(r, titles));
    } catch (e) {
      toast("Couldn't restart the offline agents — " + errMsg(e));
    } finally {
      setBusy(false);
      void refreshInstances();
    }
  };

  return (
    <div id="offline-bar" role="group" aria-label="Offline agents">
      <span className="ob-count">
        {busy ? "Restarting offline agents…" : offline.length + " agents offline"}
      </span>
      <button
        type="button"
        className="ob-btn"
        disabled={busy}
        title={
          "Relaunch every offline agent, two at a time, resuming its conversation; " +
          "an agent that died mid-task is told to continue where it left off." +
          (low ? " Memory is low: restarts wait for headroom, and the rest are held back if none comes." : "")
        }
        onClick={() => void run()}
      >
        ↻ Restart all
      </button>
    </div>
  );
}
