/** "Verify" on a session's ⋯ menu (the rail row's and the pane header's —
 * both draw lib/sessionMenu's model; this adds the item to it): the session's
 * own agent writes the steps to check its work and works them in its
 * terminal (POST /api/instances/{title}/verify). Offered only while no verify
 * is under way or waiting on a person — the pill says those. */

import type { Instance } from "../../api/types";
import { sessionMenu as baseSessionMenu, type MenuItem, type SessionMenuModel } from "../../lib/sessionMenu";
import { verifyOf } from "../../lib/sessionState";
import { startSessionVerify } from "../../lib/verifyActions";
import { withPrReviewItem } from "./prReviewMenu";

/** Whether a session can be asked to verify now. */
export function canStartVerify(inst: Partial<Instance>): boolean {
  if (!inst.title || inst.workspace_missing || inst.peer_share || inst.device || inst.pending) return false;
  if (inst.status === "paused") return false;
  const v = verifyOf(inst);
  return !v || v.state === "passed" || v.state === "failed" || v.state === "blocked";
}

/** The menu item, or null when Verify isn't offered. */
export function verifyMenuItem(inst: Partial<Instance>): MenuItem | null {
  if (!canStartVerify(inst)) return null;
  const again = !!verifyOf(inst);
  const title = inst.title as string;
  return {
    key: "verify",
    label: again ? "Verify again" : "Verify",
    note: "its agent checks its own work",
    title:
      "The agent writes the steps to check its work and works them in its terminal — it tells you anything only a person can do, and you confirm once you have",
    run: () => void startSessionVerify(title),
  };
}

/** The model with Verify in its Ship group, just before "Behaviors…" (or
 * last in the first group when there is no Ship group). */
export function withVerifyItem(model: SessionMenuModel, inst: Partial<Instance>): SessionMenuModel {
  const item = verifyMenuItem(inst);
  if (!item || !model.groups.length) return model;
  const at = Math.max(0, model.groups.findIndex((g) => g.id === "ship"));
  return {
    ...model,
    groups: model.groups.map((g, i) => {
      if (i !== at) return g;
      const before = g.items.findIndex((it) => it.key === "checklist");
      const items = [...g.items];
      items.splice(before >= 0 ? before : items.length, 0, item);
      return { ...g, items };
    }),
  };
}

/** The rail row's menu: lib/sessionMenu's model with Verify and Run PR review
 * in it (the pane header wraps its own call the same way, with withVerifyItem
 * and withPrReviewItem). */
export function sessionMenu(...args: Parameters<typeof baseSessionMenu>): SessionMenuModel {
  return withPrReviewItem(withVerifyItem(baseSessionMenu(...args), args[0]), args[0]);
}
