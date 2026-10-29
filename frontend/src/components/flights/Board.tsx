/** Flights → Board: the live view, loudest first.
 *
 *  - Needs you — flights parked on a *you* step (or halted), and Verify's due
 *    or failing checklists (a compact row that opens the Checklists tab).
 *  - In flight — one strip per flight; clicking it opens the session.
 *  - Arrivals — Intake's eligible items with no session, grouped by source,
 *    each with a plan picker and Start (which opens the new session).
 *  - Landed — finished flights, folded. */

import { useMemo, useState } from "react";
import type { TestPlan } from "../../api/types";
import { useFlightKinds, useFlightPlans, useFlights, useSettingsDoc, useTestPlans } from "../../state/queries";
import { WorkItemRow, ageText } from "../intake/kit";
import { useUi } from "../../state/store";
import { byGroup } from "../intake/queue";
import { isFailing, isWaitingOnYou, planName } from "../dialogs/verify";
import { FlightCard } from "./FlightStrip";
import { useArrivals, type Arrival } from "./useArrivals";
import { builtinPlanFor, isLive, needsYouFlights, type Flight, type FlightPlan } from "../../lib/flights";
import { startArrival, type ArrivalTarget } from "../../lib/flightsApi";

/** The plan a source names for its flights (`TicketingSource.plan`,
 * `github.repo_settings[repo].plan`, `issue_repo_settings[repo].plan`), "" =
 * none (the server then uses the built-in cut at the source's depth). */
export function sourcePlan(settings: Record<string, unknown> | undefined, t: ArrivalTarget): string {
  const s = (settings || {}) as {
    ticketing?: { sources?: Array<{ id?: string; plan?: string }> };
    github?: {
      repo_settings?: Record<string, { plan?: string }>;
      issue_repo_settings?: Record<string, { plan?: string }>;
    };
  };
  if (t.kind === "tickets") return (s.ticketing?.sources || []).find((x) => x.id === t.source)?.plan || "";
  const per = t.kind === "prs" ? s.github?.repo_settings : s.github?.issue_repo_settings;
  return per?.[t.repo]?.plan || "";
}

function Group({
  id,
  title,
  count,
  hint,
  children,
}: {
  id: string;
  title: string;
  count: number;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <section className={"fl-group fl-group-" + id} id={"fl-group-" + id} aria-labelledby={"fl-group-" + id + "-h"}>
      <h3 id={"fl-group-" + id + "-h"} className="fl-group-head">
        {title}
        {count > 0 && <span className="fl-count">{count}</span>}
        {hint && <span className="fl-group-hint muted">{hint}</span>}
      </h3>
      {children}
    </section>
  );
}

/** A due / failing checklist: one line that opens it on the Checklists tab. */
function ChecklistRow({ plan, onOpen }: { plan: TestPlan; onOpen(branch: string): void }) {
  const failing = isFailing(plan);
  return (
    <button
      type="button"
      className={"fl-check-row" + (failing ? " is-failing" : "")}
      title="Open this checklist on the Checklists tab"
      onClick={() => onOpen(plan.branch || plan.id)}
    >
      <span className="fl-check-glyph" aria-hidden="true">{failing ? "✗" : "☐"}</span>
      <span className="fl-check-name">{planName(plan)}</span>
      <span className="fl-check-state">{failing ? "Failed" : "Waiting on you"}</span>
      {plan.branch && <code className="fl-check-branch">{plan.branch}</code>}
    </button>
  );
}

/** One arrival: Intake's row, with a plan picker beside Start. */
function ArrivalRow({
  a,
  plans,
  settings,
}: {
  a: Arrival;
  plans: FlightPlan[];
  settings: Record<string, unknown> | undefined;
}) {
  const t = a.item.target as ArrivalTarget;
  const own = sourcePlan(settings, t);
  const [plan, setPlan] = useState(own);
  // The kind's own built-in when the server has one (builtin:issue may ship),
  // else the ticket plan.
  const builtin = plans.find((p) => p.id === builtinPlanFor(a.item.kind, plans));
  return (
    <div className="fl-arrival">
      <WorkItemRow
        reference={a.item.reference}
        url={a.item.url}
        title={a.item.title}
        linkTitle={"Open " + a.item.reference}
        tooltip={a.item.reference + (a.item.title ? " — " + a.item.title : "") + "\nfrom " + a.item.group}
        meta={[a.item.state, ageText(a.item.created_at), a.item.assignee].filter(Boolean).join(" · ")}
        route={a.item.route}
        eligible
        eligibleOn={a.auto}
        eligibleLabel={a.auto ? "will auto-start" : "auto-start off"}
        actionLabel="Start"
        failPrefix="Start failed"
        onStart={async ({ agent, depth, effort, repo }) => {
          const title = await startArrival(t, plan, { agent, depth, effort, repo }, a.item.kind === "tickets" ? a.item.reference : "");
          // Starting opens the session (startArrival selects it as soon as its
          // row exists): the dialog steps aside, as a template's Run now does.
          useUi.getState().closeDialog();
          return { message: title ? "Session " + title : "Session for " + a.item.reference, title };
        }}
        actionExtra={
          <label className="fl-plan-pick" title="The plan this flight follows">
            <span className="fl-plan-pick-label">Plan</span>
            <select value={plan} onChange={(e) => setPlan(e.target.value)} aria-label="Plan">
              <option value="">{own ? "Source's plan" : (builtin?.name || "Built-in plan") + " (default)"}</option>
              {plans.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                  {p.builtin ? " (built-in)" : ""}
                </option>
              ))}
            </select>
          </label>
        }
      />
    </div>
  );
}

export function Board({ onTab }: { onTab(tab: "plans" | "sources" | "checklists", rest?: string): void }) {
  const flightsQ = useFlights();
  const plans = useFlightPlans().data || [];
  const kinds = useFlightKinds().data;
  const settings = useSettingsDoc(true).data as Record<string, unknown> | undefined;
  const testPlans = useTestPlans().data?.plans || [];
  const { arrivals, refresh } = useArrivals(true);
  const [landedOpen, setLandedOpen] = useState(false);

  const flights: Flight[] = flightsQ.data || [];
  const needs = needsYouFlights(flights);
  const inFlight = flights.filter((f) => isLive(f) && !needs.includes(f));
  const landed = flights.filter((f) => !isLive(f)).sort((a, b) => (b.finished || b.updated) - (a.finished || a.updated)).slice(0, 20);
  const dueChecklists = useMemo(() => testPlans.filter((p) => isFailing(p) || isWaitingOnYou(p)), [testPlans]);
  const groups = byGroup(arrivals.map((a) => a.item));
  const byKey = new Map(arrivals.map((a) => [a.item.key, a]));
  const noRoute = flightsQ.data === null;

  return (
    <div className="fl-board" data-tour="flights-board">
      {noRoute && (
        <p className="fl-note muted" role="status">
          This server has no flights yet — update MindFlock to fly plans. Arrivals and checklists still work.
        </p>
      )}
      <Group id="needs" title="Needs you" count={needs.length + dueChecklists.length}>
        {needs.length + dueChecklists.length === 0 ? (
          <p className="fl-empty muted">Nothing waits on you.</p>
        ) : (
          <>
            {needs.map((f) => (
              <FlightCard key={f.id} flight={f} kinds={kinds} />
            ))}
            {dueChecklists.map((p) => (
              <ChecklistRow key={p.id} plan={p} onOpen={(b) => onTab("checklists", b)} />
            ))}
          </>
        )}
      </Group>
      <Group id="flying" title="In flight" count={inFlight.length}>
        {inFlight.length === 0 ? (
          <p className="fl-empty muted">
            No flight in the air.{" "}
            <button type="button" className="linklike" onClick={() => onTab("plans")}>
              Plans ›
            </button>
          </p>
        ) : (
          inFlight.map((f) => <FlightCard key={f.id} flight={f} kinds={kinds} />)
        )}
      </Group>
      <Group id="arrivals" title="Arrivals" count={arrivals.length} hint="Ready to start, oldest first">
        {arrivals.length === 0 ? (
          <p className="fl-empty muted">
            Nothing waiting to start.{" "}
            <button type="button" className="linklike" onClick={() => onTab("sources")}>
              Sources ›
            </button>{" "}
            <button type="button" className="linklike" onClick={refresh}>
              Refresh
            </button>
          </p>
        ) : (
          groups.map((g) => (
            <div className="fl-arrival-group" key={g.group}>
              <div className="fl-arrival-source">{g.group}</div>
              {g.items.map((it) => {
                const a = byKey.get(it.key);
                return a ? <ArrivalRow key={it.key} a={a} plans={plans} settings={settings} /> : null;
              })}
            </div>
          ))
        )}
      </Group>
      {landed.length > 0 && (
        <details className="fl-group fl-group-landed" open={landedOpen} onToggle={(e) => setLandedOpen((e.target as HTMLDetailsElement).open)}>
          <summary className="fl-group-head">
            Landed<span className="fl-count muted">{landed.length}</span>
          </summary>
          {landedOpen && landed.map((f) => <FlightCard key={f.id} flight={f} kinds={kinds} />)}
        </details>
      )}
    </div>
  );
}
