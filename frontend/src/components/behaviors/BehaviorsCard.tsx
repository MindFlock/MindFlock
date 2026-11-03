/** The Behaviors card (docs/behaviors.md §2): the ONE place a session's
 * behaviors are read, answered and edited — opened in place on the Sky
 * (the Behaviors tile, a Verify stop, a Behaviors-board row).
 *
 * SEAM (pre-committed by the integrator). `BehaviorsCardProps` is frozen; the
 * body below is a placeholder that vs-card replaces. */

import type { JSX } from "react";
import type { TestPlan } from "../../api/types";
import { behaviorSummary } from "../../lib/behaviors";

export interface BehaviorsCardProps {
  plan: TestPlan;
  /** × / Esc: fold the card back into its tile or row. */
  onClose?(): void;
  /** Narrow layout (a board row, a small tile slot). */
  compact?: boolean;
  /** Set when the card is a Verify stop's: the chain it parks. Lets the card
   * offer "Skip this stop" next to the behaviors. */
  flightStep?: { flightId: string; stepId: string } | null;
}

export function BehaviorsCard({ plan, onClose }: BehaviorsCardProps): JSX.Element {
  const s = behaviorSummary(plan);
  return (
    <div className={"bh-card is-" + s.tone} data-plan={plan.id}>
      <div className="bh-head">
        <span className="bh-status">{s.status}</span>
        {onClose ? <button type="button" className="fl-icon-btn" aria-label="Close" onClick={onClose}>×</button> : null}
      </div>
      <ul className="bh-rows">
        {(plan.steps || []).map((st) => <li key={st.id}>{st.text}</li>)}
      </ul>
    </div>
  );
}
