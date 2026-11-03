/** A flight, live: its stops on the canvas (FlywayCanvas) with what each one
 * is doing — done ✓ dimmed, the current one pulsing (gold perched on you,
 * green while the flock works, red halted with the reason), skipped struck
 * through, pending outlined — a perch's checklist and Done / Skip inside its
 * box, and a ⋯ on every box (Jump here · Retry · Halt now / Carry on · Cancel).
 *
 * Two homes here, one set of parts: the Board's card (`FlightCard` — the
 * session's live diagram on one compact row; a click on its head opens the
 * session) and the pane's one-line strip (`PaneFlightStrip`, above the pane
 * body: a button that opens the session's Flight tab). The Flight tab itself
 * is SessionFlight.tsx. Every action posts through lib/flightsApi. */

import { useState, type MouseEvent, type ReactNode } from "react";
import type { Instance } from "../../api/types";
import { useFlightKinds, useFlights, useInstances, useTestPlans } from "../../state/queries";
import { planForSession } from "../../lib/behaviors";
import { openBehaviors } from "../../lib/behaviorsFocus";
import { BehaviorsCard } from "../behaviors/BehaviorsCard";
import { useUi } from "../../state/store";
import { selectSession } from "../../lib/sessionActions";
import { useNow } from "../../lib/useNow";
import { OverflowMenu, type MenuItem } from "../OverflowMenu";
import { InlineConfirm } from "../InlineConfirm";
import {
  currentElapsed,
  flightProgress,
  isLive,
  perchLabel,
  rowWaitsOnYou,
  stepActor,
  stripLabel,
  youItems,
  youText,
  type Flight,
  type FlightRow,
  type StepKindDef,
} from "../../lib/flights";
import { flightDiagram, nodeAtIndex, sessionDiagram, type BoxSize, type Diagram, type DiagramNode } from "../../lib/flightDiagram";
import { windowName } from "../../lib/windowName";
import { controlFlight, jumpFlight, stepDone, stepRetry, stepSkip } from "../../lib/flightsApi";
import { FlywayCanvas } from "./FlywayCanvas";
import { Bird } from "./Bird";
import "./Flights.css";

const stop = (e: MouseEvent) => e.stopPropagation();

/** Open a flight's session: close whatever dialog is up, then select it —
 * `selectSession` is the one way in. */
export function openFlightSession(title: string) {
  if (!title) return;
  useUi.getState().closeDialog();
  selectSession(title);
}

/** Open a session on its Flight tab (the pane strip, the ⋯ "Fly a flyway…",
 * a Board card's +). */
export function openFlightTab(title: string) {
  if (!title) return;
  useUi.getState().closeDialog();
  selectSession(title);
  useUi.getState().setLastTab(title, "flight");
}

/** The headline a card leads with: the subject's title, else its reference,
 * else the session. */
export function flightHeadline(f: Flight): string {
  const s = f.subject;
  if (s?.title) return s.title;
  if (s?.id) return (s.kind === "pr" || s.kind === "issue" ? "#" : "") + s.id;
  return f.session || f.plan_name;
}

/** A Verify stop parked on you (docs/behaviors.md §5): its behaviors, each
 * answered on its own in the Behaviors card — no blanket Done. The stop moves
 * on once the last one is answered (the server settles it), or the card's
 * Skip. `compact` (the pane's one-line strip): a way to the card in the Sky,
 * and Skip. Before the behaviors are written: what it waits for, and Skip. */
export function VerifyStopActions({ flight, compact = false }: { flight: Flight; compact?: boolean }) {
  const step = flight.steps[flight.cursor];
  const plans = useTestPlans().data?.plans;
  const rows = useInstances().data;
  const row = rows?.find((r) => r.title === flight.session) || { title: flight.session, branch: "", repo: "" };
  const plan = planForSession(plans || [], row);
  const [busy, setBusy] = useState(false);
  if (!step) return null;
  const skip = (
    <button
      type="button"
      className="test-btn fl-btn fl-skip"
      disabled={busy}
      title={"Skip “" + step.title + "” — the chain goes on without it"}
      onClick={() => {
        setBusy(true);
        void stepSkip(flight.id, step.id).finally(() => setBusy(false));
      }}
    >
      Skip
    </button>
  );
  if (compact || !plan)
    return (
      <div className={"fl-you fl-you-verify" + (compact ? " fl-you-compact" : "")} onClick={stop} data-no-drag>
        {!compact && <p className="fl-you-text">Writing the behaviors… they show here to answer once they are written.</p>}
        <div className="fl-you-acts">
          {plan && (
            <button type="button" className="btn-primary fl-btn fl-behaviors" title="Answer the behaviors that wait on you, on the session's frame in the Sky" onClick={() => openBehaviors({ planId: plan.id })}>
              Behaviors
            </button>
          )}
          {skip}
        </div>
      </div>
    );
  return (
    <div className="fl-you fl-you-verify" onClick={stop} data-no-drag>
      <BehaviorsCard plan={plan} compact flightStep={{ flightId: flight.id, stepId: step.id }} />
    </div>
  );
}

/** Done / Skip on a perch, its checklist as tick boxes (ticking them all is
 * what enables Done); Retry / Skip on a halted stop. A Verify stop parked on
 * you answers its behaviors instead (`VerifyStopActions`). */
export function YouStepActions({ flight, compact = false }: { flight: Flight; compact?: boolean }) {
  const step = flight.steps[flight.cursor];
  const items = youItems(step);
  const [ticked, setTicked] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState(false);
  if (!step || (flight.state !== "waiting_you" && flight.state !== "halted")) return null;
  if (step.kind === "verify" && flight.state === "waiting_you") return <VerifyStopActions flight={flight} compact={compact} />;
  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
    } finally {
      setBusy(false);
    }
  };
  const key = (i: number) => flight.id + ":" + step.id + ":" + i;
  const allTicked = items.every((_, i) => ticked[key(i)]);
  const halted = flight.state === "halted";
  const text = youText(flight);
  return (
    <div className={"fl-you" + (compact ? " fl-you-compact" : "")} onClick={stop} data-no-drag>
      {!compact && text && !halted && <p className="fl-you-text">{text}</p>}
      {!halted && items.length > 0 && (
        <ul className="fl-you-items">
          {items.map((it, i) => (
            <li key={key(i)}>
              <label className="fl-tick">
                <input
                  type="checkbox"
                  checked={!!ticked[key(i)]}
                  onChange={(e) => setTicked((t) => ({ ...t, [key(i)]: e.target.checked }))}
                />
                <span>{it}</span>
              </label>
            </li>
          ))}
        </ul>
      )}
      <div className="fl-you-acts">
        {halted ? (
          <button
            type="button"
            className="btn-primary fl-btn"
            disabled={busy}
            title="Run this stop again"
            onClick={() => void run(() => stepRetry(flight.id, step.id))}
          >
            Retry
          </button>
        ) : (
          <button
            type="button"
            className="btn-primary fl-btn fl-done"
            disabled={busy || !allTicked}
            title={allTicked ? "Mark “" + step.title + "” done — the chain goes on" : "Tick every item first"}
            onClick={() => void run(() => stepDone(flight.id, step.id))}
          >
            Done
          </button>
        )}
        <button
          type="button"
          className="test-btn fl-btn fl-skip"
          disabled={busy}
          title={"Skip “" + step.title + "” — the chain goes on without it"}
          onClick={() => void run(() => stepSkip(flight.id, step.id))}
        >
          Skip
        </button>
      </div>
    </div>
  );
}

/** The flight's own ⋯: Open session · Halt now / Carry on · Cancel. Cancel asks
 * inline first (it ends the flight; the session is kept). */
export function FlightMenu({
  flight,
  onCancelAsk,
  openSession = true,
}: {
  flight: Flight;
  onCancelAsk(): void;
  /** The pane is already in the session: no "Open session". */
  openSession?: boolean;
}) {
  const live = isLive(flight);
  const items: MenuItem[] = [];
  if (openSession && flight.session)
    items.push({ key: "open", label: "Open session", onSelect: () => openFlightSession(flight.session) });
  if (live) {
    items.push(
      flight.state === "halted" || flight.state === "paused"
        ? { key: "resume", label: "Carry on", title: "Go on from the current stop", onSelect: () => void controlFlight(flight.id, "resume") }
        : { key: "pause", label: "Halt now", title: "Halt the chain at its current stop — Carry on picks it up again", onSelect: () => void controlFlight(flight.id, "pause") }
    );
    items.push({ key: "cancel", label: "Stop the chain", danger: true, title: "End this chain — its session is kept", onSelect: onCancelAsk });
  }
  return (
    <span onClick={stop} className="fl-menu" data-no-drag>
      <OverflowMenu label="Chain actions" items={items} />
    </span>
  );
}

/** A box's ⋯ on a live flight: Jump here (any stop but the current one),
 * Retry (the halted or failed one), and the flight's Halt now / Carry on / Cancel. */
export function stopMenuItems(flight: Flight, n: DiagramNode, onCancelAsk: () => void): MenuItem[] {
  if (!isLive(flight) || n.kind !== "stop" || n.index === undefined) return [];
  const step = flight.steps[n.index];
  if (!step) return [];
  const items: MenuItem[] = [];
  const atCursor = n.index === flight.cursor;
  if (!atCursor) items.push({ key: "jump", label: "Jump here", title: "Move the chain to this stop", onSelect: () => void jumpFlight(flight.id, step.id, step.title) });
  if (n.state === "halted" || n.state === "failed")
    items.push({ key: "retry", label: "Retry", title: "Run this stop again", onSelect: () => void stepRetry(flight.id, step.id) });
  if (atCursor)
    items.push(
      flight.state === "halted" || flight.state === "paused"
        ? { key: "resume", label: "Carry on", title: "Go on from here", onSelect: () => void controlFlight(flight.id, "resume") }
        : { key: "pause", label: "Halt now", title: "Halt the chain at this stop — Carry on picks it up again", onSelect: () => void controlFlight(flight.id, "pause") }
    );
  items.push({ key: "cancel", label: "Stop the chain", danger: true, title: "End this chain — its session is kept", onSelect: onCancelAsk });
  return items;
}

/** The cancel question, under a card or the pane's diagram. */
export function useCancelAsk(flight: Flight | null) {
  const [asking, setAsking] = useState(false);
  const [busy, setBusy] = useState(false);
  const node: ReactNode =
    asking && flight ? (
      <div onClick={stop}>
        <InlineConfirm
          title="Stop this chain?"
          body="It stops where it is. Its session, branch and work are kept."
          confirmLabel="Stop the chain"
          cancelLabel="Keep it running"
          busy={busy}
          onCancel={() => setAsking(false)}
          onConfirm={async () => {
            setBusy(true);
            try {
              await controlFlight(flight.id, "cancel");
            } finally {
              setBusy(false);
              setAsking(false);
            }
          }}
        />
      </div>
    ) : null;
  return { node, ask: () => setAsking(true) };
}

/** What goes inside a live flight's boxes: the ⋯, and — when `expand` — a
 * perch's checklist with Done / Skip (or a halted stop's Retry / Skip) in the
 * current box, which is drawn bigger to hold them. `verifyInPlace` (a chain
 * drawn by SkyChain): a Verify stop parked on you stays a tile — its
 * Behaviors card opens in the tile's place (docs/behaviors.md §1). */
export function liveBoxParts(flight: Flight | null, expand: boolean, onCancelAsk: () => void, opts: { verifyInPlace?: boolean } = {}) {
  const cur = flight ? flight.steps[flight.cursor] : null;
  const verifyHere = !!flight && cur?.kind === "verify" && flight.state === "waiting_you";
  const expanded = (n: DiagramNode) =>
    !!flight &&
    expand &&
    n.kind === "stop" &&
    n.index === flight.cursor &&
    (n.state === "perched" || n.state === "halted") &&
    !(opts.verifyInPlace && verifyHere);
  const renderInBox = (n: DiagramNode): ReactNode => {
    if (!flight || n.kind !== "stop") return null;
    const items = stopMenuItems(flight, n, onCancelAsk);
    return (
      <>
        {items.length > 0 && (
          <span className="fl-box-menu" onClick={stop} data-no-drag>
            <OverflowMenu label={"Actions for " + n.title} items={items} />
          </span>
        )}
        {expanded(n) && <YouStepActions flight={flight} />}
      </>
    );
  };
  const sizeOf = (n: DiagramNode, base: BoxSize): BoxSize => {
    if (!expanded(n)) return base;
    // A Verify stop's behaviors: room for the card (it scrolls past it).
    if (verifyHere) return { w: Math.max(base.w, 320), h: base.h + 220 };
    const items = n.state === "perched" ? youItems(cur).length : 0;
    const text = n.state === "perched" && youText(flight!) ? 24 : 0;
    return { w: Math.max(base.w, 250), h: base.h + text + items * 23 + 38 };
  };
  return { renderInBox, sizeOf };
}

/** The box of the stop a flight is at — the group box holding it, when it is
 * a sub-step of one; at a Loop (no box), the stop its wire leaves from (the
 * canvas scrolls it into view). */
export function currentNodeId(d: Diagram, flight: Flight | null): string | null {
  if (!flight) return null;
  return nodeAtIndex(d, flight.cursor)?.id || d.edges.find((e) => e.kind === "loop" && e.via?.step?.id === flight.steps[flight.cursor]?.id)?.from || null;
}

/** One flight on the Board: its headline, the bird and its flyway, the
 * session; then the session's live diagram on one compact row. A click on
 * the head opens its session; on a box, it opens that box's ⋯ or acts. */
export function FlightCard({ flight, kinds, expand = false }: { flight: Flight; kinds?: readonly StepKindDef[] | null; expand?: boolean }) {
  const now = useNow(isLive(flight) ? 30_000 : false);
  const rows = useInstances().data || [];
  const flights = useFlights().data;
  const cancel = useCancelAsk(flight);
  const prog = flightProgress(flight);
  const step = prog.current;
  const elapsed = currentElapsed(flight, now);
  const waiting = flight.state === "waiting_you" || flight.state === "halted";
  const you = step ? stepActor(step, kinds) === "you" : false;
  const live = isLive(flight);
  // The session's live diagram when its row says it is flying this flight;
  // otherwise (landed, or the session is gone) the flight's own stops.
  const row = rows.find((r) => r.title === flight.session);
  const fromRow = live && row && (row.flight as FlightRow | null | undefined)?.id === flight.id;
  const diagram: Diagram = fromRow
    ? sessionDiagram(rows, flight.session, { flights: flights || [flight], kinds, family: false })
    : flightDiagram(flight, kinds);
  const parts = liveBoxParts(flight, expand, cancel.ask);
  return (
    <div
      className={"fl-strip-card fl-state-" + flight.state + (waiting ? " fl-needs" : "")}
      data-flight={flight.id}
      data-tour="flights-strip"
      role="group"
      aria-label={flightHeadline(flight)}
    >
      <div
        className="fl-strip-body"
        role="button"
        tabIndex={0}
        title={flight.session ? "Open " + windowName(flight.session) : undefined}
        onClick={() => openFlightSession(flight.session)}
        onKeyDown={(e) => {
          if (e.target !== e.currentTarget) return;
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            openFlightSession(flight.session);
          }
        }}
      >
        <div className="fl-strip-head">
          <span className="fl-strip-title">{flightHeadline(flight)}</span>
          <span className="fl-strip-plan">
            <Bird size={13} />
            {flight.plan_name}
          </span>
          {flight.session && <span className="fl-strip-session">{windowName(flight.session)}</span>}
          <FlightMenu flight={flight} onCancelAsk={cancel.ask} />
        </div>
        <div className="fl-strip-now">
          {flight.state === "done" ? (
            <span className="fl-landed">Finished</span>
          ) : flight.state === "cancelled" ? (
            <span className="muted">Cancelled{flight.reason ? " — " + flight.reason : ""}</span>
          ) : step ? (
            <>
              <span className={"fl-now-dot" + (you ? " fl-now-you" : "") + (flight.state === "halted" ? " fl-now-halted" : "")} aria-hidden="true" />
              <span className="fl-now-title">{you ? perchLabel(step.title) : step.title}</span>
              {elapsed && <span className="fl-now-elapsed">{elapsed}</span>}
              {flight.state === "paused" && (
                <span className="fl-now-paused" title={flight.reason === "halt point" ? "The chain reached a halt point and stopped before this stop — Carry on to go on" : "Halted by you — Carry on to go on"}>
                  {flight.reason === "halt point" ? "Paused at a halt point" : "Paused"}
                </span>
              )}
              {flight.state === "halted" && flight.reason && <span className="fl-now-reason">{flight.reason}</span>}
            </>
          ) : null}
          <span className="fl-progress muted">
            {prog.done}/{prog.total}
          </span>
        </div>
      </div>
      <FlywayCanvas
        diagram={diagram}
        compact
        vertical={false}
        label={"Where “" + flightHeadline(flight) + "” is"}
        onSelect={() => openFlightTab(flight.session)}
        onOpen={(n) => (n.kind === "add" ? openFlightTab(flight.session) : n.session ? openFlightSession(n.session) : undefined)}
        renderInBox={parts.renderInBox}
        sizeOf={parts.sizeOf}
        reveal={currentNodeId(diagram, flight)}
      />
      {cancel.node}
    </div>
  );
}

/** The pane's strip: `<bird> Ticket to merge · ●●●○○○ · Reviews settle (4m)` —
 * a button that opens the session's Flight tab — with Done / Skip for a perch
 * and a ⋯. Reads the session row's `flight` field, and the full flight from
 * the shared flights query when it is there (for the elapsed time and the
 * checklist). */
export function PaneFlightStrip({ inst }: { inst: Pick<Instance, "title" | "flight"> }) {
  const row = inst.flight as FlightRow | null | undefined;
  const live = !!row && isLive(row);
  const flights = useFlights(live).data;
  useFlightKinds(live);
  const now = useNow(live ? 30_000 : false);
  const full = flights?.find((f) => f.id === row?.id) || null;
  const cancel = useCancelAsk(full);
  if (!row || !live) return null;
  const line = full ? stripLabel(full, now) : stripLabel(row);
  const waiting = row.state === "waiting_you" || row.state === "halted";
  return (
    <div className={"fl-strip fl-state-" + row.state + (waiting || rowWaitsOnYou(row) ? " fl-needs" : "")} data-tour="flights-strip" onMouseDown={stop}>
      <div className="fl-strip-row">
        <button
          type="button"
          className="fl-strip-line"
          title={"Open the Chain tab — " + line + (full ? "\n" + full.steps.map((s) => s.title).join(" → ") : "")}
          onClick={() => openFlightTab(inst.title)}
        >
          <Bird size={13} />
          <span className="fl-strip-text">{line}</span>
        </button>
        {full && waiting && <YouStepActions flight={full} compact />}
        {full && <FlightMenu flight={full} onCancelAsk={cancel.ask} openSession={false} />}
      </div>
      {cancel.node}
    </div>
  );
}
