/** The launch gate's queue (backend core/launch_gate.py): automatic starts
 * (an agent's spawned worker, a queued prompt relaunching its agent, a
 * mailbox wake, what a revive sweep couldn't fit) wait here while memory is
 * short, and start one at a time once it allows. A row that waits says
 * "Waiting for memory"; these are its two actions. */

import { api } from "../api/client";
import type { LaunchGateStatus } from "../api/types";
import { refreshInstances } from "../state/queries";
import { errMsg } from "./format";
import { toast } from "./toast";
import { displayName } from "../state/store";

function gatePath(title: string, act: "start" | "cancel"): string {
  return "/api/launch-gate/" + encodeURIComponent(title) + "/" + act;
}

/** Start anyway: launch it now, past the gate. */
export async function startWaiting(title: string): Promise<boolean> {
  try {
    const r = await api<{ started: boolean; error?: string }>(gatePath(title, "start"), { method: "POST" });
    if (!r.started) {
      toast("Couldn't start " + displayName(title) + " — " + (r.error || "it failed to start"));
      return false;
    }
    toast("Starting " + displayName(title) + " now…");
    return true;
  } catch (e) {
    toast("Couldn't start " + displayName(title) + " — " + errMsg(e));
    return false;
  } finally {
    void refreshInstances();
  }
}

/** Stop waiting: it is never started by the queue. */
export async function cancelWaiting(title: string): Promise<boolean> {
  try {
    await api(gatePath(title, "cancel"), { method: "POST" });
    toast(displayName(title) + " no longer waits for memory");
    return true;
  } catch (e) {
    toast("Couldn't cancel " + displayName(title) + " — " + errMsg(e));
    return false;
  } finally {
    void refreshInstances();
  }
}

/** The calm top-bar line while the gate holds starts back, "" when it holds
 * nothing: "Memory low — new agents wait (3 waiting)". The reason when it is
 * not memory: "Agent limit reached — new agents wait". Pure. */
export function holdingLine(g: LaunchGateStatus | undefined | null): string {
  if (!g || !g.holding || !g.queued.length) return "";
  const n = g.queued.length;
  const capped = /automatic starts stop at/.test(g.reason || "");
  const head = capped ? "Agent limit reached" : "Memory low";
  return head + " — new agents wait (" + n + " waiting)";
}
