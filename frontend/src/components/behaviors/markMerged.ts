/** "It's merged into main" — a person telling Verify a checklist's work has
 * reached the branch it ships from (`POST /api/test-plans/{id}/shipped`).
 *
 * For the merge the server cannot see: a squash through another branch's
 * pull request leaves no ancestry and no merged PR on this branch. The server
 * takes it the way it takes a merge it saw itself — the deploy wait, then
 * "check it now" and the notification — so the reply is either a checklist
 * that is due or one waiting out its deploy. */

import { api } from "../../api/client";
import type { TestPlan } from "../../api/types";
import { errorPop } from "../../lib/errorPop";
import { errMsg } from "../../lib/format";
import { toast } from "../../lib/toast";
import { patchPlan, planPath } from "../../lib/verifyActions";
import { refreshTestPlans } from "../../state/queries";
import { planName } from "../dialogs/verify";

/** The toast once the server took it: due now, or still waiting on the
 * deploy. */
export function mergedToast(plan: TestPlan, after: TestPlan | undefined): string {
  if (after && after.state === "due") return planName(plan) + " is ready to check";
  return planName(plan) + " is merged — it's ready to check once it deploys";
}

export async function markPlanMerged(plan: TestPlan): Promise<void> {
  try {
    const r = await api<{ plan?: TestPlan }>(planPath(plan.id) + "/shipped", { method: "POST" });
    if (r?.plan) patchPlan(plan.id, () => r.plan as TestPlan);
    await refreshTestPlans();
    toast(mergedToast(plan, r?.plan));
  } catch (err) {
    errorPop("Couldn't mark " + planName(plan) + " merged", errMsg(err));
    void refreshTestPlans();
  }
}
