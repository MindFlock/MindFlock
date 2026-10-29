/** A source's flyway choice: which flyway its items fly ("" = the kind's
 * built-in, cut at the source's Fast-track depth), as a row of chips —
 * built-ins first, the default chip reading "Default · <built-in>". On the
 * ticket source cards and the PR / issue repository cards (Flights →
 * Sources); the Board's waiting items default to what it holds. Draws nothing
 * on a server without the flights routes, so those cards look as they always
 * did there. */

import { useFlightPlans } from "../../state/queries";
import { planChoices, type WaitingKind } from "../../lib/flights";
import { ChipRow } from "./controls";

export function PlanSelect({
  kind,
  value,
  onChange,
  className,
  field,
}: {
  kind: WaitingKind;
  value: string;
  onChange(plan: string): void;
  className?: string;
  /** The card's field hook (`data-tk-field` / `data-repo-field`). */
  field: { attr: "data-tk-field" | "data-repo-field" };
}) {
  const plans = useFlightPlans().data;
  if (!plans) return null;
  return (
    <div className="set-row fl-source-plan">
      <span className="set-label">Flyway</span>
      <ChipRow
        className={className}
        label="Flyway"
        field={{ [field.attr]: "plan" }}
        value={value}
        options={planChoices(plans, kind, value)}
        onChange={onChange}
      />
      <span className="set-hint">The flyway its items fly. Fast-track sets how far the default one goes.</span>
    </div>
  );
}
