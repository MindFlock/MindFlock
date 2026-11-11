/** "Run PR review" on a session's ⋯ menu (the rail row's and the pane
 * header's — both draw lib/sessionMenu's model; this adds the item to it, the
 * way verifyMenu.ts adds Verify): runs the team's saved PR review route on the
 * session's open PR (lib/prReview.ts) — a fresh route, or the review stops
 * slotted in after its current stop when it is already on one. The agent may
 * still be working; the review doesn't wait for it to finish. */

import type { Instance } from "../../api/types";
import { prReviewSupported, startPrReview } from "../../lib/prReview";
import type { MenuItem, SessionMenuModel } from "../../lib/sessionMenu";

/** Whether the menu offers it: the session has a PR, isn't paused, and is a
 * row of this device's (a remote or shared row can't be driven from here). */
export function canRunPrReview(inst: Partial<Instance>): boolean {
  if (!inst.title || !inst.pr_url) return false;
  if (inst.status === "paused") return false;
  if (inst.workspace_missing || inst.peer_share || inst.device || inst.pending) return false;
  return prReviewSupported();
}

/** The menu item, or null when it isn't offered. */
export function prReviewMenuItem(inst: Partial<Instance>): MenuItem | null {
  if (!canRunPrReview(inst)) return null;
  const title = inst.title as string;
  return {
    key: "pr-review",
    label: "Run PR review",
    note: "your team's review route on its PR",
    title:
      "Runs the PR review route chosen in Settings → Workspace on this session's PR — right after its current stop when it is already on a route",
    run: () => void startPrReview({ session: title }),
  };
}

/** The model with Run PR review in its Ship group, after Verify (else before
 * "Behaviors…", else last in the first group when there is no Ship group). */
export function withPrReviewItem(model: SessionMenuModel, inst: Partial<Instance>): SessionMenuModel {
  const item = prReviewMenuItem(inst);
  if (!item || !model.groups.length) return model;
  const at = Math.max(0, model.groups.findIndex((g) => g.id === "ship"));
  return {
    ...model,
    groups: model.groups.map((g, i) => {
      if (i !== at) return g;
      const items = [...g.items];
      const verify = items.findIndex((it) => it.key === "verify");
      const behaviors = items.findIndex((it) => it.key === "checklist");
      items.splice(verify >= 0 ? verify + 1 : behaviors >= 0 ? behaviors : items.length, 0, item);
      return { ...g, items };
    }),
  };
}
