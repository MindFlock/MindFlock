/** The behaviors' verdict where merges actually happen.
 *
 * A lead pressing Open PR or Merge on a session got no signal that its branch
 * had never been checked, or had a failed behavior. `VerdictChip` puts the
 * verdict on the session's sidebar row and pane head ("✓ Verified",
 * "✗ Failed", "3/5 checked"); clicking it flies the Sky to the session's
 * behaviors on its route (`openBehaviors({ title, branch })`,
 * docs/behaviors.md §4 — Verify is a stop on the route, not a place).
 *
 * It reads `planVerdict` (verify.ts). Styles are `.vchip` in VerifyDialog.css
 * (shared with the peer joint check) — deliberately NOT `.stagechip`, which
 * `grid/DiffTab.css` hides inside pane heads. */

import type { Instance } from "../../api/types";
import { useTestPlans } from "../../state/queries";
import { openBehaviors } from "../../lib/behaviorsFocus";
import { isVerifySession, planForSession, planStatus, planVerdict } from "./verify";

/** The verdict on a session's sidebar row or pane head; nothing when the
 * session has no behaviors. */
export function VerdictChip({
  inst,
  where,
}: {
  inst: Pick<Instance, "title" | "branch" | "repo" | "hidden_by">;
  where: "row" | "pane";
}) {
  // The app-wide list the top bar's badge already keeps polling — no extra
  // request per row.
  const { data } = useTestPlans();
  if (isVerifySession(inst)) return null;
  const plan = planForSession(data?.plans || [], inst);
  if (!plan) return null;
  const v = planVerdict(plan);
  const branch = plan.branch || inst.branch || "";
  const label = "Behaviors: " + v.label + " — " + planStatus(plan).line + " Show them on the route in the Sky.";
  return (
    <button
      type="button"
      className={"vchip vchip-" + where}
      data-verdict={v.key}
      data-tone={v.tone}
      title={label}
      aria-label={label}
      onClick={(e) => {
        e.stopPropagation();
        openBehaviors({ planId: plan.id, title: inst.title, branch });
      }}
      onDoubleClick={(e) => e.stopPropagation()}
      onMouseDown={(e) => e.stopPropagation()}
    >
      <span className="vchip-glyph" aria-hidden="true">
        {v.glyph}
      </span>
      {/* The sidebar row is narrow and its name is the thing it must not lose:
          there the chip is the mark alone (plus "0/3" when partly checked);
          the words are in its tooltip. The pane head has room for them. */}
      {where === "pane" ? v.short : v.key === "partial" ? v.short.replace(" checked", "") : null}
    </button>
  );
}
