/** "Goes as far as": the ONE control on an Intake source for how far its items
 * go by themselves. It is on the ticket source cards (TicketsTab) and the PR /
 * issue repository cards (RepoSources). It replaced their "Fast-track to"
 * select and "Stops" chips: once Stops wasn't Default, the server ignored the
 * depth and nothing on the card said so.
 *
 *   Stop after the agent · Commit · Push · Open PR   (the rungs a source may
 *        default to; Merge stays per item, see lib/autopilot SOURCE_DEPTHS)
 *   ┊ your saved stops, then our built-ins for this kind of item
 *   ┊ Edit in the Sky…   (the source's default stops as the Sky's editor chain)
 *
 * A rung writes the source's depth AND clears its plan; a saved stop writes
 * the plan. The server's precedence (server._intake_plan) is unchanged. Under
 * the chips is one read-only line of the stops it resolves to ("Agent → Commit
 * → Push → Open PR"). It is worked out here (lib/flights sourceReach) and
 * replaced by the server's answer (GET /api/flights/intake-default) when that
 * answer is for the same choice. The Sky lane's chip uses the same words
 * (lib/skyIntake stopsWords). */

import { useQuery } from "@tanstack/react-query";
import { useId } from "react";
import { api } from "../../api/client";
import { useFlightPlans, useFlights } from "../../state/queries";
import { useUi } from "../../state/store";
import { SOURCE_RUNGS, sourceRungLabel, type Lane } from "../../lib/autopilot";
import { sourcePlanOptions, sourceReach, stopsSummaryWords, type IntakeDefault, type SourceReach, type WaitingKind } from "../../lib/flights";
import { toast } from "../../lib/toast";
import { openDefaultEditor } from "./IntakeLane";

/** What a pick writes to the source: a rung sets the depth ("" = stop after
 * the agent) and clears the plan; a saved stop sets the plan. */
export type ReachPatch = { depth: string; plan: "" } | { plan: string };

/** The summary line for a reach. */
export function reachSummary(r: Pick<SourceReach, "steps" | "missing" | "words">, serverSteps?: string[] | null): string {
  const steps = serverSteps && serverSteps.length ? serverSteps : r.steps;
  const line = steps.length === 1 && steps[0] === "Agent" ? "Agent, then nothing more. You take it from there" : steps.length ? steps.join(" → ") : "Your saved route “" + r.words + "”";
  return r.missing ? "Its saved route “" + r.missing + "” is gone, so: " + line : line;
}

export function PlanSelect({
  kind,
  target,
  depth,
  plan,
  onChange,
  className,
  field,
}: {
  kind: WaitingKind;
  /** The source as the server knows it: a ticket source's id, or owner/repo.
   * "" for a card the server doesn't hold yet (no editor in the Sky for it). */
  target: string;
  /** The stored depth and plan. */
  depth: string;
  plan: string;
  onChange(patch: ReachPatch): void;
  className?: string;
  /** The card's field hook (`data-tk-field` / `data-repo-field`). */
  field: { attr: "data-tk-field" | "data-repo-field" };
}) {
  const plans = useFlightPlans().data;
  const flights = useFlights().data;
  const labelId = useId();
  const reach = sourceReach(kind, { plan, depth }, plans);
  const saved = sourcePlanOptions(plans, kind);
  // The server's own word on what a new item flies, kept only when it answers
  // for the choice on screen (a pick's save may not have landed yet).
  const server = useQuery({
    queryKey: ["intake-default", kind, target, reach.choice],
    queryFn: () => api<IntakeDefault>("/api/flights/intake-default?kind=" + encodeURIComponent(kind) + "&target=" + encodeURIComponent(target)).catch(() => null),
    enabled: !!target && plans !== null,
    staleTime: 15_000,
    retry: false,
  }).data;
  const serverSteps = server && (server.plan_id || "") === reach.planId ? stopsSummaryWords(server.steps) : null;

  const pickRung = (l: Lane) => onChange({ depth: l === "leave" ? "" : l, plan: "" });
  const editInSky = async () => {
    if (!target) return;
    const f = await openDefaultEditor({ kind, target }, flights);
    useUi.getState().showSky();
    if (f) toast("“" + (f.plan_name || "Default stops") + "” is on the Sky. Save as default there to change where this source goes.", { duration: 6000 });
  };

  return (
    <div className={"set-row fl-source-reach" + (className ? " " + className : "")} data-reach={reach.choice}>
      <span className="set-label" id={labelId}>
        Goes as far as
      </span>
      <div className="fl-reach-line">
        <div className="fl-chiprow fl-reach-row" role="radiogroup" aria-labelledby={labelId} {...{ [field.attr]: "reach" }}>
          {SOURCE_RUNGS.map((l) => (
            <button
              key={l}
              type="button"
              role="radio"
              aria-checked={reach.choice === l}
              className={"fl-pick" + (reach.choice === l ? " on" : "")}
              data-value={l}
              onClick={() => pickRung(l)}
            >
              {sourceRungLabel(l)}
            </button>
          ))}
          {saved.length > 0 && <span className="fl-reach-sep" aria-hidden="true" />}
          {saved.map((p) => {
            const on = reach.choice === "plan:" + p.value;
            return (
              <button
                key={p.value}
                type="button"
                role="radio"
                aria-checked={on}
                className={"fl-pick fl-reach-plan" + (on ? " on" : "")}
                title={p.builtin ? "Our built-in route for these items" : "Your saved route"}
                data-value={"plan:" + p.value}
                onClick={() => onChange({ plan: p.value })}
              >
                {p.label}
              </button>
            );
          })}
          {/* A saved stop the list doesn't have (yet): still the one on. */}
          {reach.own && !saved.some((p) => "plan:" + p.value === reach.choice) && (
            <button type="button" role="radio" aria-checked className="fl-pick fl-reach-plan on" data-value={reach.choice} onClick={() => onChange({ plan })}>
              {reach.words}
            </button>
          )}
        </div>
        {plans !== null && (
          <button
            type="button"
            className="linklike fl-reach-edit"
            disabled={!target}
            title={target ? "Edit this source's default stops in the Sky" : "Save the source first"}
            onClick={() => void editInSky()}
          >
            Edit in the Sky…
          </button>
        )}
      </div>
      <span className="set-hint fl-reach-summary" data-reach-summary>
        {reachSummary(reach, serverSteps)}
      </span>
    </div>
  );
}
