/** Verify's verdict where merges actually happen.
 *
 * A lead pressing Open PR or Merge on a session got no signal that its branch
 * had never been verified, or that a step failed. `VerdictChip` puts the
 * verdict on the session's sidebar row and pane head in Verify's one set of
 * words: the session row's `verify` when it has one (mapped through the
 * pill's own labels — "Verifying", "Verify failed", …), else the plan's
 * verdict (`planVerdict`). Clicking it goes where the pill, the Ship menu's
 * Verify and a bell event go: the session's Verify steps on its route in the
 * Sky (`openBehaviors({ planId, title, branch })`, docs/behaviors.md §4).
 *
 * Styles are `.vchip` in VerifyDialog.css (shared with the peer joint check)
 * — deliberately NOT `.stagechip`, which `grid/DiffTab.css` hides inside pane
 * heads. */

import type { Instance, SessionVerify, TestPlan } from "../../api/types";
import { useTestPlans } from "../../state/queries";
import { openBehaviors } from "../../lib/behaviorsFocus";
import { behaviorSummary, verifyWords } from "../../lib/behaviors";
import { verifyOf } from "../../lib/sessionState";
import { isVerifySession, planForSession, planVerdict } from "./verify";

/** What the chip says: its key (styling), tone, mark, the pane's words, the
 * row's few characters, and its tooltip. Pure, so the tests pin it. */
export interface ChipWords {
  key: string;
  tone: string;
  glyph: string;
  pane: string;
  row: string;
  title: string;
}

export function chipWords(verify: SessionVerify | null, plan: TestPlan | null): ChipWords | null {
  if (verify) {
    const w = verifyWords(verify);
    return {
      key: verify.state,
      tone: w.tone,
      glyph: w.glyph,
      pane: w.label,
      row: "",
      title: "Verify: " + w.line + ". Click to see its steps.",
    };
  }
  if (!plan) return null;
  const v = planVerdict(plan);
  const sentence = behaviorSummary(plan).sentence;
  return {
    key: v.key,
    tone: v.tone,
    glyph: v.glyph,
    pane: v.short,
    // The sidebar row is narrow and its name is the thing it must not lose:
    // the mark alone, plus "2/5" when partly done.
    row: v.key === "partial" ? v.short : "",
    title: "Verify: " + v.label + (sentence ? " — " + sentence : "") + " Click to see its steps.",
  };
}

/** The verdict on a session's sidebar row or pane head; nothing when the
 * session has never verified. */
export function VerdictChip({
  inst,
  where,
}: {
  inst: Pick<Instance, "title" | "branch" | "repo" | "hidden_by"> & { verify?: SessionVerify | null };
  where: "row" | "pane";
}) {
  // The app-wide list the top bar's badge already keeps polling — no extra
  // request per row.
  const { data } = useTestPlans();
  if (isVerifySession(inst)) return null;
  const plan = planForSession(data?.plans || [], inst);
  const verify = verifyOf(inst);
  const w = chipWords(verify, plan);
  if (!w) return null;
  const branch = plan?.branch || inst.branch || "";
  return (
    <button
      type="button"
      className={"vchip vchip-" + where}
      data-verdict={w.key}
      data-tone={w.tone}
      title={w.title}
      aria-label={w.title}
      onClick={(e) => {
        e.stopPropagation();
        openBehaviors({ planId: verify?.plan || plan?.id, title: inst.title, branch });
      }}
      onDoubleClick={(e) => e.stopPropagation()}
      onMouseDown={(e) => e.stopPropagation()}
    >
      <span className="vchip-glyph" aria-hidden="true">
        {w.glyph}
      </span>
      {where === "pane" ? w.pane : w.row || null}
    </button>
  );
}
