/** The session pane's Flight tab: the SESSION DIAGRAM (docs/flights.md "The
 * session diagram") — a systems drawing of what this session is and what it
 * is about to do, built from `/api/instances` rows alone:
 *
 *   [parent]                       (above, when it has one — click = go up)
 *      │
 *   [session] → [rung / stop] → … → [+ Tack on a stop]
 *      ├─ [child]                  (its workers, each with its own state and
 *      └─ [child]                   mini ladder — click = open it)
 *
 * The line after the session box is the flight's stops when it flies one,
 * else the armed fast-track's rungs (commit → push → pr → merge up to its
 * depth), else nothing yet. The trailing + tacks a stop on: onto the live
 * flight (`POST /api/flights/{id}/steps`), or — no flight yet — as a new
 * ad-hoc flight that takes over the fast-track's remaining rungs
 * (`POST /api/flights/start {title, steps, absorb_fasttrack}`), so the
 * ladder's boxes turn into the flight's seamlessly. "Fly a flyway" on the +
 * appends all of a flyway's stops. A pending stop opens in the inspector and
 * is edited in place; done and current ones are read-only.
 *
 * With nothing on the line yet the tab also offers the flyways as cards
 * ("Fly this flyway") and "Describe a flyway…" to make one from a sentence
 * and fly it right away. The line's editing (the +, the palette, the
 * inspector) is useSessionLine's, shared with the Sky's lines; "Open in the
 * Sky" shows this session's line there (`#flights/sky/<title>`). */

import { useState } from "react";
import type { Instance } from "../../api/types";
import { useFlightKinds, useFlightPlans, useFlights, useInstances } from "../../state/queries";
import { useUi } from "../../state/store";
import { selectSession } from "../../lib/sessionActions";
import { flightProgress, type FlightPlan, type StepKindDef } from "../../lib/flights";
import { birdPerch, flywayDiagram, hasSessionDiagram } from "../../lib/flightDiagram";
import { skyTarget } from "../../lib/skyLayout";
import { savePlan, takeOff } from "../../lib/flightsApi";
import { FlywayCanvas } from "./FlywayCanvas";
import { FlightMenu } from "./FlightStrip";
import { DescribeField } from "./StopParts";
import { useSessionLine } from "./useSessionLine";
import { Bird } from "./Bird";
import "./FlightsDialog.css";

/** One flyway as a card: its name, its stops as tiny boxes, "Fly this flyway". */
function FlywayCard({ plan, kinds, onFly, busy }: { plan: FlightPlan; kinds: readonly StepKindDef[]; onFly(): void; busy: boolean }) {
  const d = flywayDiagram(plan.steps, kinds);
  return (
    <div className="fl-flyway-card">
      <div className="fl-flyway-card-head">
        <Bird size={14} />
        <strong>{plan.name}</strong>
        {plan.builtin && <span className="fl-builtin-tag">Built-in</span>}
      </div>
      <ol className="fl-mini-line" aria-label={plan.name + "'s stops"}>
        {d.nodes
          .filter((n) => n.kind === "stop")
          .map((n) => (
            <li key={n.id} className={"fl-mini-box" + (n.perch ? " fl-perch" : "")} title={n.title + " — " + n.sub}>
              {n.title}
            </li>
          ))}
      </ol>
      <button type="button" className="btn-primary fl-btn" disabled={busy} onClick={onFly}>
        Fly this flyway
      </button>
    </div>
  );
}

export function SessionFlight({ inst, active = true }: { inst: Pick<Instance, "title">; active?: boolean }) {
  const title = inst.title;
  const rows = useInstances().data || [];
  const flights = useFlights(active).data;
  const kinds = useFlightKinds(active).data || [];
  const plans = useFlightPlans(active).data || [];
  const line = useSessionLine(title, { rows, flights, kinds, plans }, { onSession: (t) => selectSession(t) });
  const [busy, setBusy] = useState(false);
  const { row, flight, diagram, ordered, cancel } = line;

  if (!row) return <p className="fl-note muted">This session isn't listed right now.</p>;

  const prog = flight ? flightProgress(flight) : null;
  const working = busy || line.busy;

  return (
    <div className="fl-session" data-tour="flights-session">
      <div className="fl-session-head">
        <Bird size={15} />
        {flight ? (
          <>
            <strong>{flight.plan_name}</strong>
            <span className="muted">
              {prog!.done}/{prog!.total} stops
              {flight.state === "paused" ? " · paused" : flight.state === "halted" ? " · halted" : flight.state === "waiting_you" ? " · perched on you" : ""}
            </span>
            <FlightMenu flight={flight} onCancelAsk={cancel.ask} openSession={false} />
          </>
        ) : diagram.mode === "ladder" ? (
          <>
            <strong>Fast-track</strong>
            <span className="muted">tack a stop on and the flight takes over its rungs</span>
          </>
        ) : (
          <>
            <strong>No flight yet</strong>
            <span className="muted">tack a stop onto the line, or fly a flyway below</span>
          </>
        )}
        <button
          type="button"
          className="linklike fl-open-sky"
          title="Show this session's line in the Sky, with the bird where it is"
          onClick={() => useUi.getState().openDialogFor("flights", skyTarget(title))}
        >
          Open in the Sky
        </button>
      </div>
      {cancel.node}
      <FlywayCanvas diagram={diagram} label={"What " + title + " is doing and will do"} bird={birdPerch(diagram)} {...line.canvas} />
      {line.palette}
      {line.inspector}
      {!hasSessionDiagram(rows, title) && (
        <section className="fl-flyway-cards" aria-label="Fly a flyway">
          <h4 className="fl-group-head">Fly a flyway</h4>
          {ordered.length ? (
            <div className="fl-flyway-grid">
              {ordered.map((p) => (
                <FlywayCard key={p.id} plan={p} kinds={kinds} busy={working} onFly={() => void line.flyFlyway(p)} />
              ))}
            </div>
          ) : (
            <p className="fl-note muted">No flyways on this server yet.</p>
          )}
          <DescribeField
            want="plan"
            taken={[]}
            button="Describe a flyway"
            placeholder="Describe a flyway… e.g. push, open a PR, wait for reviews, then ask me before merging"
            onStops={async (steps) => {
              setBusy(true);
              try {
                const words = steps.map((s) => s.title).join(" → ");
                const plan = await savePlan({
                  id: "",
                  name: words.length > 60 ? words.slice(0, 59) + "…" : words || "Described flyway",
                  description: "",
                  builtin: false,
                  trigger: { kind: "manual", schedule: null, paused: false },
                  session: { repo_path: "", program: "", model: "", in_place: false },
                  steps,
                  updated: 0,
                });
                await takeOff({ plan_id: plan.id, title });
              } finally {
                setBusy(false);
              }
            }}
          />
        </section>
      )}
    </div>
  );
}
