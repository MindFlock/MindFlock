/** Verify's verdict, OUTSIDE Verify — where merges actually happen.
 *
 * A lead pressing Open PR or Merge on a session got no signal that its branch
 * had never been checked, or had failed its checklist: the verdict lived only
 * inside the Verify dialog. `VerdictChip` puts it on the session's sidebar row
 * and pane head ("✓ Verified", "✗ Failed", "3/5 checked"); clicking it opens
 * Verify on that branch (`openDialogFor("verify", branch)`).
 *
 * `VerdictBadge` is the same verdict on a checklist row inside the dialog.
 *
 * Both read `planVerdict` (verify.ts), the rule the dialog's groups use, so
 * the chip on a session and the heading its checklist sits under cannot
 * disagree. Styles are `.vchip` in VerifyDialog.css — deliberately NOT
 * `.stagechip`, which `grid/DiffTab.css` hides inside pane heads. */

import type { Instance, TestPlan } from "../../api/types";
import { useTestPlans } from "../../state/queries";
import { useUi } from "../../state/store";
import { isVerifySession, planForSession, planStatus, planVerdict } from "./verify";

/** The verdict as a small pill — a checklist row's, inside the dialog. */
export function VerdictBadge({ plan }: { plan: TestPlan }) {
  const v = planVerdict(plan);
  return (
    <span className="vchip" data-verdict={v.key} data-tone={v.tone}>
      <span className="vchip-glyph" aria-hidden="true">
        {v.glyph}
      </span>
      {v.label}
    </span>
  );
}

/** The verdict on a session's sidebar row or pane head; nothing when no
 * checklist covers the session. */
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
  const label = "Checklist: " + v.label + " — " + planStatus(plan).line + " Open it in Verify.";
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
        if (branch) useUi.getState().openDialogFor("verify", branch);
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
