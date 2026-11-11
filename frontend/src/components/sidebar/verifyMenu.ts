/** "Verify" on a session's ⋯ menu (the rail row's and the pane header's —
 * both draw lib/sessionMenu's model; this adds the item to it): the session's
 * own agent writes the steps to check its work and works them in its
 * terminal (POST /api/instances/{title}/verify). The item opens the start
 * form in the session's pane (what it should prove, where — VerifyStartForm),
 * not a modal. While a verify is under way the item is "Stop verifying"
 * instead; while one waits on a person there is none — the pill says it. */

import type { Instance } from "../../api/types";
import { sessionMenu as baseSessionMenu, type MenuItem, type SessionMenuModel } from "../../lib/sessionMenu";
import { verifyOf } from "../../lib/sessionState";
import { stopVerifying } from "../../lib/verifyActions";
import { openVerifyForm } from "../behaviors/VerifyStartForm";
import { withPrReviewItem } from "./prReviewMenu";

/** Whether a session can be asked to verify now. */
export function canStartVerify(inst: Partial<Instance>): boolean {
  if (!inst.title || inst.workspace_missing || inst.peer_share || inst.device || inst.pending) return false;
  if (inst.status === "paused") return false;
  const v = verifyOf(inst);
  return !v || v.state === "passed" || v.state === "failed" || v.state === "blocked";
}

/** The menu item, or null when Verify isn't offered: "Verify" / "Verify
 * again" (opens the start form in the pane), or "Stop verifying" while its
 * agent is working the steps. */
export function verifyMenuItem(inst: Partial<Instance>): MenuItem | null {
  const v = verifyOf(inst);
  if (v?.state === "verifying" && inst.title && !inst.peer_share && !inst.device) {
    const title = inst.title;
    return {
      key: "verify-stop",
      label: "Stop verifying",
      note: "the session keeps running",
      title: "Its agent stops working the steps — the session itself keeps running",
      run: () => void stopVerifying(v.plan, title),
    };
  }
  if (!canStartVerify(inst)) return null;
  const title = inst.title as string;
  return {
    key: "verify",
    label: v ? "Verify again" : "Verify",
    note: "its agent checks its own work",
    title:
      "Say what it should prove (optional), then its agent writes the steps to check its work and works them in its terminal — it tells you anything only a person can do",
    run: () => openVerifyForm(title),
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
      // The Ship group already has its one "Verify" entry (lib/stage
      // withVerifyVerdict, once a verify exists): never two.
      if (g.items.some((it) => it.key === "verify")) return g;
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
