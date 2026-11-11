/** The launch gate's queue (backend core/launch_gate.py): automatic starts
 * (an agent's spawned worker, a queued prompt relaunching its agent, a
 * mailbox wake, what a revive sweep couldn't fit) wait here — for memory,
 * for agents still warming up, for the live-agent cap, or for the spacing
 * between automatic starts — and start one at a time once it allows. A
 * waiting row says why in lib/waitWhy's words; these are its two actions. */

import { api } from "../api/client";
import type { LaunchGateStatus } from "../api/types";
import { refreshInstances } from "../state/queries";
import { errMsg } from "./format";
import { toast } from "./toast";
import { displayName } from "../state/store";
import { isMemoryCancel, sleptLine, withMemoryConfirm } from "./memoryConfirm";
import { gateWhy, holdHead, stopWaitingToast } from "./waitWhy";

function gatePath(title: string, act: "start" | "cancel"): string {
  return "/api/launch-gate/" + encodeURIComponent(title) + "/" + act;
}

/** Hooks the actions use, swappable in tests. */
export const launchGateDeps = {
  post: <T>(path: string, init: { method?: string; json?: unknown }) => api<T>(path, init),
  say: (line: string) => toast(line),
  refresh: () => void refreshInstances(),
};

/** Start anyway: launch it now, past the gate. A person's start: at critical
 * memory the server asks first (409 memory_confirm, lib/memoryConfirm), and
 * the dialog's Start anyway resends it with `start_anyway`. */
export async function startWaiting(title: string): Promise<boolean> {
  const name = displayName(title);
  try {
    const r = await withMemoryConfirm(
      name,
      (anyway) =>
        launchGateDeps.post<{ started: boolean; error?: string }>(
          gatePath(title, "start"),
          anyway ? { json: { start_anyway: true } } : { method: "POST" }
        ),
      (a) => launchGateDeps.say(sleptLine(a))
    );
    if (!r.started) {
      launchGateDeps.say("Couldn't start " + name + " — " + (r.error || "it failed to start"));
      return false;
    }
    launchGateDeps.say("Starting " + name + " now…");
    return true;
  } catch (e) {
    // Cancel in the memory dialog: it keeps its place in the queue.
    if (isMemoryCancel(e)) launchGateDeps.say(name + " keeps waiting");
    else launchGateDeps.say("Couldn't start " + name + " — " + errMsg(e));
    return false;
  } finally {
    launchGateDeps.refresh();
  }
}

/** Stop waiting: the queue never starts it. `creates`: the start would have
 * made the session (an agent's worker), so nothing is left to start. */
export async function cancelWaiting(title: string, creates?: boolean): Promise<boolean> {
  const name = displayName(title);
  try {
    await launchGateDeps.post(gatePath(title, "cancel"), { method: "POST" });
    launchGateDeps.say(stopWaitingToast(name, creates));
    return true;
  } catch (e) {
    launchGateDeps.say("Couldn't stop " + name + " waiting — " + errMsg(e));
    return false;
  } finally {
    launchGateDeps.refresh();
  }
}

/** The calm rail line while the gate holds starts back, "" when it holds
 * nothing: "Memory low — new agents wait (3 waiting)", "Agent limit reached
 * — new agents wait (1 waiting)", "Starting agents one at a time (2
 * waiting)" — keyed on the gate's why (lib/waitWhy). Pure. */
export function holdingLine(g: LaunchGateStatus | undefined | null): string {
  const why = gateWhy(g);
  if (!g || !why) return "";
  return holdHead(why) + " (" + g.queued.length + " waiting)";
}
