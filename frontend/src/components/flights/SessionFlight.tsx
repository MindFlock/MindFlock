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
 * and fly it right away. */

import { useEffect, useMemo, useState } from "react";
import type { Instance } from "../../api/types";
import { useFlightKinds, useFlightPlans, useFlights, useInstances } from "../../state/queries";
import { selectSession } from "../../lib/sessionActions";
import { toast } from "../../lib/toast";
import { flightProgress, isLive, newStep, type FlightPlan, type FlightRow, type FlightStep, type StepKindDef } from "../../lib/flights";
import { flywayDiagram, hasSessionDiagram, sessionDiagram, type DiagramNode } from "../../lib/flightDiagram";
import { dropStop, patchStop, savePlan, tackOn, tackOnAll, tackOnNew, takeOff } from "../../lib/flightsApi";
import { FlywayCanvas } from "./FlywayCanvas";
import { FlightMenu, currentNodeId, liveBoxParts, useCancelAsk } from "./FlightStrip";
import { DescribeField, StopInspector, StopPalette } from "./StopParts";
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
  const [selected, setSelected] = useState<string | null>(null);
  const [paletteAt, setPaletteAt] = useState<number | null>(null);
  const [draft, setDraft] = useState<FlightStep | null>(null);
  const [busy, setBusy] = useState(false);
  const [selectIndex, setSelectIndex] = useState<number | null>(null);

  const row = rows.find((r) => r.title === title);
  const flRow = row?.flight as FlightRow | null | undefined;
  const flight = flRow && isLive(flRow) ? flights?.find((f) => f.id === flRow.id) || null : null;
  const diagram = useMemo(() => sessionDiagram(rows, title, { flights, kinds }), [rows, title, flights, kinds]);
  const cancel = useCancelAsk(flight);
  const parts = liveBoxParts(flight, true, cancel.ask);
  const kindOf = useMemo(() => new Map(kinds.map((k) => [k.kind, k])), [kinds]);
  const stops = diagram.nodes.filter((n) => n.kind === "stop");
  const end = flight ? flight.steps.length : 0;
  const sel = selected ? diagram.nodes.find((n) => n.id === selected) || null : null;

  // A stop just tacked on opens in the inspector once the flight has it.
  useEffect(() => {
    if (selectIndex === null) return;
    const n = stops.find((s) => s.index === selectIndex && s.step);
    if (!n) return;
    setSelected(n.id);
    setDraft(n.step!);
    setSelectIndex(null);
  }, [selectIndex, stops]);

  // The inspector follows the box; a box that left the line closes it.
  useEffect(() => {
    if (selected && !sel) {
      setSelected(null);
      setDraft(null);
    }
  }, [selected, sel]);

  if (!row) return <p className="fl-note muted">This session isn't listed right now.</p>;

  const tackOne = async (def: StepKindDef, at: number | null) => {
    const step = newStep(def, flight ? flight.steps.map((s) => s.id) : []);
    setBusy(true);
    setPaletteAt(null);
    try {
      if (flight) {
        const where = at !== null && at < end ? at : undefined;
        if (await tackOn(flight.id, step, where)) setSelectIndex(where ?? end);
      } else {
        const f = await tackOnNew(title, [step]);
        if (f) {
          toast("Took off: " + (f.plan_name || "a new flight"));
          setSelectIndex(f.steps.length - 1);
        }
      }
    } finally {
      setBusy(false);
    }
  };

  const flyFlyway = async (plan: FlightPlan) => {
    setBusy(true);
    setPaletteAt(null);
    try {
      if (flight) {
        if (await tackOnAll(flight.id, plan.steps)) toast("Added “" + plan.name + "”'s " + plan.steps.length + " stops");
      } else await takeOff({ plan_id: plan.id, title });
    } finally {
      setBusy(false);
    }
  };

  const onSelect = (n: DiagramNode) => {
    setPaletteAt(null);
    if (selected === n.id) {
      setSelected(null);
      setDraft(null);
      return;
    }
    setSelected(n.id);
    setDraft(n.step ? { ...n.step, config: { ...n.step.config } } : null);
  };

  const ordered = [...plans.filter((p) => p.builtin), ...plans.filter((p) => !p.builtin)];
  const flywayChips = (
    <div className="fl-palette-flyways">
      <div className="fl-palette-head">Fly a flyway — all of its stops</div>
      <div className="fl-chiprow">
        {ordered.map((p) => (
          <button key={p.id} type="button" className="fl-pick" disabled={busy} title={p.steps.map((s) => s.title).join(" → ")} onClick={() => void flyFlyway(p)}>
            {p.name}
          </button>
        ))}
      </div>
    </div>
  );

  const prog = flight ? flightProgress(flight) : null;
  const editable = !!sel?.editable && !!draft && !!flight;
  const dirtyStop = !!draft && !!sel?.step && JSON.stringify(draft) !== JSON.stringify(sel.step);

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
      </div>
      {cancel.node}
      <FlywayCanvas
        diagram={diagram}
        label={"What " + title + " is doing and will do"}
        selected={paletteAt !== null && paletteAt >= end ? "add" : selected}
        onSelect={onSelect}
        onOpen={(n) => {
          if (n.kind === "add") {
            setSelected(null);
            setDraft(null);
            setPaletteAt(paletteAt !== null && paletteAt >= end ? null : end);
          } else if (n.session && n.session !== title) selectSession(n.session);
        }}
        onInsert={flight ? (at) => setPaletteAt(paletteAt === at ? null : at) : undefined}
        onRemove={flight ? (n) => n.editable && n.step && void dropStop(flight.id, n.step.id) : undefined}
        renderInBox={parts.renderInBox}
        sizeOf={parts.sizeOf}
        reveal={currentNodeId(diagram, flight)}
      />
      {paletteAt !== null && (
        <StopPalette
          kinds={kinds}
          title={paletteAt >= end ? "Tack on a stop" : "Add a stop before “" + (flight?.steps[paletteAt]?.title || "") + "”"}
          onPick={(k) => void tackOne(k, paletteAt)}
          onClose={() => setPaletteAt(null)}
          extra={paletteAt >= end && ordered.length > 0 ? flywayChips : undefined}
        />
      )}
      {sel && draft && (
        <StopInspector
          step={draft}
          def={kindOf.get(draft.kind)}
          readOnly={!editable}
          onChange={(p) => setDraft((d) => (d ? { ...d, ...p } : d))}
          note={
            !editable ? (
              <p className="fl-note muted">{sel.state === "done" || sel.state === "skipped" ? "This stop has flown — it can't change now." : "The flight is at this stop — it can't change while it is here."}</p>
            ) : null
          }
          actions={
            editable ? (
              <>
                <button
                  type="button"
                  className="btn-primary fl-btn"
                  disabled={!dirtyStop || busy}
                  onClick={async () => {
                    setBusy(true);
                    try {
                      await patchStop(flight!.id, draft.id, { title: draft.title, config: draft.config, on_fail: draft.on_fail });
                    } finally {
                      setBusy(false);
                    }
                  }}
                >
                  Save stop
                </button>
                <button
                  type="button"
                  className="test-btn fl-btn danger"
                  disabled={busy}
                  onClick={async () => {
                    setBusy(true);
                    try {
                      if (await dropStop(flight!.id, draft.id)) {
                        setSelected(null);
                        setDraft(null);
                      }
                    } finally {
                      setBusy(false);
                    }
                  }}
                >
                  Remove stop
                </button>
              </>
            ) : undefined
          }
        />
      )}
      {!hasSessionDiagram(rows, title) && (
        <section className="fl-flyway-cards" aria-label="Fly a flyway">
          <h4 className="fl-group-head">Fly a flyway</h4>
          {ordered.length ? (
            <div className="fl-flyway-grid">
              {ordered.map((p) => (
                <FlywayCard key={p.id} plan={p} kinds={kinds} busy={busy} onFly={() => void flyFlyway(p)} />
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
