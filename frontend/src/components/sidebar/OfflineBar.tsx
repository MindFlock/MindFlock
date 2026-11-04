/** "N agents offline · Start all" — the Offline chip's Restart / Start, for
 * every row that offers it. After the OOM killer (or a crash) takes several
 * agents down at once, clicking each chip in turn is the chore this removes.
 *
 * It runs the server's revive sweep (POST /api/server/revive-all,
 * backend/web/core/revive.py) rather than fanning the per-row endpoint out:
 * the sweep launches two at a time with a breather, waits for memory
 * headroom between launches (relaunching everything straight into the
 * pressure that killed it is how it happens again) and tells an agent that
 * died mid-turn to continue where it left off. Shown from two offline rows
 * up; one row's own chip is the button for one.
 *
 * Sessions HELD offline after a crash (lib/heldOffline, backend
 * core/held.py) turn the bar into a calm notice from ONE row up: "3 sessions
 * stayed offline after a crash, so they use no memory. Start the ones you
 * need." [Start all] [×]. Its × hides it until a session it was not about is
 * held (a later crash); the plain bar comes back underneath when two or more
 * are still offline. */

import { useState } from "react";
import { api } from "../../api/client";
import { refreshInstances, useInstances, useMemory } from "../../state/queries";
import { deriveSessionState, forceActivity } from "../../lib/sessionState";
import { toast } from "../../lib/toast";
import { errMsg } from "../../lib/format";
import { heldNoticeHidden, heldNoticeKey, heldNoticeText, heldRows } from "../../lib/heldOffline";
import type { Instance } from "../../api/types";

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
  let line = revived ? "Started " + revived + (revived === 1 ? " agent" : " agents") : "No agents started";
  if (continued) line += " — " + continued + " told to continue where " + (continued === 1 ? "it" : "they") + " left off";
  if (memory) line += "; " + memory + " not started: memory is low";
  const other = mine.filter((t) => skipped[t] !== "low memory");
  if (other.length) {
    const why = Array.from(new Set(other.map((t) => skipped[t]))).join(", ");
    line += "; " + other.length + " couldn't start (" + why + ")";
  }
  return line;
}

/** The rows the bar counts and "Start all" starts: this device's sessions
 * whose chip offers Restart or Start (a held one too). A sleeping agent
 * (backend core/sleep.py) reads "offline" but is not: it was stopped on
 * purpose, and wakes when it is opened or sent anything — never counted.
 * Exported for tests. */
export function offlineRows(instances: Instance[]): Instance[] {
  return instances.filter((i) => {
    if (i.device || i.pending || i.sleeping) return false;
    const ss = deriveSessionState(i);
    return ss.state === "offline" && ss.restartable && i.status !== "paused";
  });
}

/** The line under the bar while memory is low: starting everything is how
 * the machine ran out in the first place. */
export const LOW_MEMORY_LINE = "Memory is low — starting them all may fill it again.";

const DISMISS_KEY = "mf.heldNotice.dismissed";

function readDismissed(): string {
  try {
    return localStorage.getItem(DISMISS_KEY) || "";
  } catch {
    return "";
  }
}

function writeDismissed(v: string) {
  try {
    localStorage.setItem(DISMISS_KEY, v);
  } catch {
    /* private window / blocked storage: hidden for this page only */
  }
}

export function OfflineBar() {
  const { data: instances = [] } = useInstances();
  const memory = useMemory().data;
  const [busy, setBusy] = useState(false);
  const [dismissed, setDismissed] = useState(readDismissed);
  const offline = offlineRows(instances);
  const held = heldRows(instances);
  const heldKey = heldNoticeKey(held);
  const notice = held.length > 0 && !heldNoticeHidden(dismissed, heldKey);
  if (!notice && offline.length < 2 && !busy) return null;

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
      toast("Couldn't start the offline agents — " + errMsg(e));
    } finally {
      setBusy(false);
      void refreshInstances();
    }
  };
  const startAll = (
    <button
      type="button"
      className="ob-btn"
      disabled={busy || !titles.length}
      title={
        "Start every offline agent, two at a time, resuming its conversation; " +
        "an agent that died mid-task is told to continue where it left off." +
        (low ? " Memory is low: starts wait for headroom, and the rest stay offline if none comes." : "")
      }
      onClick={() => void run()}
    >
      ▶ Start all
    </button>
  );
  const lowLine = low ? (
    <span className="ob-low" role="note">
      {LOW_MEMORY_LINE}
    </span>
  ) : null;

  if (notice)
    return (
      <div id="offline-bar" className="ob-held" role="status" aria-label="Sessions offline after a crash">
        <span className="ob-text">{busy ? "Starting offline agents…" : heldNoticeText(held.length)}</span>
        {lowLine}
        <span className="ob-acts">
          {startAll}
          <button
            type="button"
            className="ob-x"
            title="Hide this notice — the sessions stay offline until you press Start"
            aria-label="Hide this notice"
            onClick={() => {
              writeDismissed(heldKey);
              setDismissed(heldKey);
            }}
          >
            ×
          </button>
        </span>
      </div>
    );

  return (
    <div id="offline-bar" role="group" aria-label="Offline agents">
      <span className="ob-count">{busy ? "Starting offline agents…" : offline.length + " agents offline"}</span>
      {lowLine}
      {startAll}
    </div>
  );
}
