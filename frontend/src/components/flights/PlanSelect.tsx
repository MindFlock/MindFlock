/** A source's Plan picker: which flight plan its items fly ("" = the kind's
 * built-in, cut at the source's Fast-track depth). On the ticket source cards
 * and the PR / issue repository cards (Flights → Sources); the Board's
 * arrivals default to what it holds. Draws nothing on a server without the
 * flights routes, so those cards look as they always did there. */

import { useFlightPlans } from "../../state/queries";
import { planChoices, type ArrivalKind } from "../../lib/flights";

export function PlanSelect({
  kind,
  value,
  onChange,
  className,
  field,
}: {
  kind: ArrivalKind;
  value: string;
  onChange(plan: string): void;
  className?: string;
  /** The card's field hook (`data-tk-field` / `data-repo-field`). */
  field: { attr: "data-tk-field" | "data-repo-field" };
}) {
  const plans = useFlightPlans().data;
  if (!plans) return null;
  return (
    <label className="set-row fl-source-plan">
      <span className="set-label">Plan</span>
      <select className={className} {...{ [field.attr]: "plan" }} value={value} onChange={(e) => onChange(e.target.value)}>
        {planChoices(plans, kind, value).map((o) => (
          <option key={o.value || "default"} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
      <span className="set-hint">The flight its items follow. Fast-track sets how far the default plan goes.</span>
    </label>
  );
}
