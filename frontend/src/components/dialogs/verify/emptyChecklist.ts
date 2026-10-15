/** "Write steps yourself → [branch] → Start an empty checklist" (round 5, M2).
 *
 * Before this, a person who knew exactly what to check had to ask the agent
 * for a checklist, let it fail, and then add steps to the failed card. The
 * server already makes an EMPTY checklist on request (`empty: true`, origin
 * "manual", no model call): for a session on its own write route, and for a
 * branch no session has been on through the branch route. */

import { api } from "../../../api/client";
import { refreshTestPlans } from "../../../state/queries";
import { errorPop } from "../../../lib/errorPop";
import { errMsg } from "../../../lib/format";
import { toast } from "../../../lib/toast";

interface WriteAnswer {
  plan: string;
  existing: boolean;
}

/** Make an empty checklist for a session's branch (open or closed). Resolves
 * to the checklist's id — a new one, or the one that already covers the
 * branch — or "" when the server refused (the reason is popped). */
export async function startEmptyChecklist(session: string): Promise<string> {
  return post(
    "/api/instances/" + encodeURIComponent(session) + "/test-plan",
    { empty: true },
    session,
  );
}

/** The same for a branch no session of yours has been on. */
export async function startEmptyChecklistForBranch(repoRoot: string, branch: string): Promise<string> {
  return post("/api/test-plans/branch", { repo: repoRoot, branch, empty: true }, branch);
}

async function post(path: string, json: unknown, name: string): Promise<string> {
  try {
    const r = await api<WriteAnswer>(path, { method: "POST", json });
    await refreshTestPlans();
    toast(
      r.existing
        ? name + " already has a checklist — add your steps to it"
        : "Started an empty checklist for " + name + " — add the steps to check",
    );
    return r.plan || "";
  } catch (err) {
    errorPop("Couldn't start the checklist", errMsg(err));
    return "";
  }
}
