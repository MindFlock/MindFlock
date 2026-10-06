/** The team-run routes, as the UI calls them (server: docs/web-api.md "Team
 * runs"). One place for the paths and the "act, refetch, say so" shape, so
 * the rail's group menu, the bell's waiting rows and the row › menu can never
 * drift apart on what a button posts.
 *
 * What each control posts:
 *  - Pause / Resume        POST /api/runs/{id}/pause {reason:"user"} · /resume {}
 *  - Raise budget          POST /api/runs/{id}/resume {budget_usd}  (raise and resume in one)
 *  - Stop / Cancel         POST /api/runs/{id}/cancel               (sessions and branches stay)
 *  - Add lines             POST /api/runs/{id}/tasks {items}
 *  - Retry / Retry fresh   POST /api/runs/{id}/tasks/{task}/retry {fresh}
 *  - Start now             POST /api/runs/{id}/tasks/{task}/start-now
 *  - Skip / Remove / Move out of the group
 *                          POST /api/runs/{id}/tasks/{task}/skip
 * Approving an "asks first" ship is per SESSION, not per run:
 * POST /api/instances/{t}/ship-now {commit_message?}. */

import { api } from "../api/client";
import { refreshInstances } from "../state/queries";
import { refreshRuns } from "../state/runs";
import { errorPop } from "./errorPop";
import { errMsg } from "./format";
import { toast } from "./toast";

export const runPath = (id: string, rest = "") => "/api/runs/" + encodeURIComponent(id) + rest;
export const taskPath = (id: string, task: string, verb: "retry" | "start-now" | "skip") =>
  runPath(id, "/tasks/" + encodeURIComponent(task) + "/" + verb);

/** POST, refetch the groups and the rows, and say so; a failure opens the
 * error card (the server's sentence carries the remedy — too long for a
 * toast). Resolves true when it went through. */
export async function runAction(what: string, path: string, body: unknown = {}, done = ""): Promise<boolean> {
  try {
    await api(path, { json: body });
    if (done) toast(done);
    return true;
  } catch (err) {
    errorPop(what + " failed", errMsg(err));
    return false;
  } finally {
    void refreshRuns();
    void refreshInstances();
  }
}

/** A budget row's "Raise to $N" (the bell): a budget pause is lifted by resuming with the
 * new budget — one call, never a separate PUT. */
export function raiseBudget(runId: string, usd: number, name = ""): Promise<boolean> {
  return runAction("Raise the budget", runPath(runId, "/resume"), { budget_usd: usd }, (name || "The group") + " resumed with a $" + usd + " budget");
}

/** The bell's "Stop" on a budget item, and the group menu's Cancel. */
export function cancelRun(runId: string, name = ""): Promise<boolean> {
  return runAction("Cancel", runPath(runId, "/cancel"), {}, (name || "The group") + " cancelled — its sessions and branches are kept");
}

/** A raise to suggest: the next round number past what was spent (at least
 * the old budget + half of it). */
export function suggestedBudget(budget: number, spent: number): number {
  const floor = Math.max(spent, budget) * 1.5;
  const step = floor < 10 ? 1 : floor < 100 ? 5 : 25;
  return Math.max(step, Math.ceil(floor / step) * step);
}
