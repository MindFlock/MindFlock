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
 * (lib/skyIntake stopsWords).
 *
 * A PR repository card (`prRoute` given: the stored `flights.pr_review_plan`)
 * has one more chip, first: **PR route** — the card names nothing of its own
 * and its PRs fly the flock's PR route (Settings → Workspace; our built-in
 * "Pull request" when none is picked), the route the PRs the pipeline picks
 * up automatically fly too (backend core/pr_route.py). It stores depth "" and
 * no plan; "Agent only" then stores depth "agent" (no route for this repo). */

import { useQuery } from "@tanstack/react-query";
import { useId } from "react";
import { api } from "../../api/client";
import { useFlightPlans, useFlights } from "../../state/queries";
import { useUi } from "../../state/store";
import { SOURCE_RUNGS, sourceRungLabel, type Lane } from "../../lib/autopilot";
import { flockPrRoute, sourcePlanOptions, sourceReach, stopsSummaryWords, type FlockPrRoute, type IntakeDefault, type SourceReach, type WaitingKind } from "../../lib/flights";
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

/** The summary line when a PR repo follows the flock's PR route. */
export function prRouteSummary(flock: FlockPrRoute, serverSteps?: string[] | null): string {
  if (flock.off) return "PR routes are off: the agent gets the review comments, then you take it from there";
  const steps = serverSteps && serverSteps.length ? serverSteps : flock.steps;
  const name = "Your PR route “" + flock.name + "”" + (flock.builtin ? " (the built-in default)" : "");
  return steps.length ? name + ": " + steps.join(" → ") : name;
}

export function PlanSelect({
  kind,
  target,
  depth,
  plan,
  onChange,
  className,
  field,
  prRoute,
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
  /** PR repo cards: the stored `flights.pr_review_plan` ("" = unset), which
   * adds the "PR route" chip. Omitted elsewhere. */
  prRoute?: string;
}) {
  const plans = useFlightPlans().data;
  const flights = useFlights().data;
  const labelId = useId();
  const flock = kind === "prs" && prRoute !== undefined ? flockPrRoute(prRoute, plans) : null;
  const reach = sourceReach(kind, { plan, depth }, plans, flock);
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

  // With a PR route to follow, "Agent only" must say so itself ("agent"):
  // a blank depth is "follow the PR route".
  const pickRung = (l: Lane) => onChange({ depth: l === "leave" ? (flock ? "agent" : "") : l, plan: "" });
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
          {flock && (
            <button
              type="button"
              role="radio"
              aria-checked={reach.choice === "flock"}
              className={"fl-pick fl-reach-flock" + (reach.choice === "flock" ? " on" : "")}
              title={"Follow your PR route (Settings → Workspace): " + flock.name}
              data-value="flock"
              onClick={() => onChange({ depth: "", plan: "" })}
            >
              PR route
            </button>
          )}
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
        {flock && reach.choice === "flock" ? prRouteSummary(flock, serverSteps) : reachSummary(reach, serverSteps)}
      </span>
      {flock && (
        <span className="set-hint" data-pr-route-hint>
          This is also what this repository's PRs fly when they're picked up automatically.
        </span>
      )}
    </div>
  );
}
