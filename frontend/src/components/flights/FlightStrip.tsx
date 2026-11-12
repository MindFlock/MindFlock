/** A flight, live: its stops on the canvas (FlywayCanvas) with what each one
 * is doing — done ✓ dimmed, the current one pulsing (gold perched on you,
 * green while the flock works, red halted with the reason), skipped struck
 * through, pending outlined — a perch's checklist and Done / Skip inside its
 * box, and a ⋯ on every box (Jump here · Retry · Pause / Carry on · Cancel).
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
import { VerifyConfirm } from "../behaviors/VerifyConfirm";
import { verifyLine, verifyOf } from "../../lib/sessionState";
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
  stripParts,
  youItems,
  youText,
  type Flight,
  type FlightRow,
  type StepKindDef,
} from "../../lib/flights";
import { HALT_POINT_REASON, atHaltPoint, flightDiagram, nodeAtIndex, sessionDiagram, type BoxSize, type Diagram, type DiagramNode } from "../../lib/flightDiagram";
import { windowName } from "../../lib/windowName";
import { controlFlight, flightAction, flightPath, flyAgain, jumpFlight, stepDone, stepFix, stepRetry, stepSkip } from "../../lib/flightsApi";
import { canFlyAgain, flyAgainLabel, flyAgainPrimary, flyAgainTitle, pickAgainStop } from "../../lib/routeStart";
import "./RouteStartMenu.css";
import { ASK_TILE_W, OWED_OPEN, OWED_OPEN_TITLE, askOffers, askRoom, askSentence, findingsOf, flightAsk, owedGroups, owedItemsText, owedNoteOf } from "../../lib/stopAsks";
import { OwedChecks, OwedNote, ReviewFindings } from "./PerchAsks";
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

/** A Verify stop parked on you (docs/behaviors.md §5), by the server's
 * `detail.ask`: `confirm` — when the session's own agent did its verify
 * steps and left a person's (`verify.state` "needs_you"), those steps and
 * Confirm verified / Not verified… (the one answer a person gives; the stop
 * moves on once it is given, or on Skip), else its card; `blocked` — the
 * check couldn't finish: why, Retry (verify again) and Skip; `memory` —
 * memory refused the check: why, Retry and Skip. `compact` (the pane's
 * one-line strip): the buttons, and a way to the card — the pane's own
 * verify row carries the confirm. Before its steps are written: what it
 * waits for, and Skip. */
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
      title={"Skip “" + step.title + "” — the route goes on without it"}
      onClick={() => {
        setBusy(true);
        void stepSkip(flight.id, step.id).finally(() => setBusy(false));
      }}
    >
      Skip
    </button>
  );
  // The check couldn't finish (`blocked`: no report, a deadline, the window
  // gone) or memory refused it (`memory`): say why, and Retry verifies again
  // (the server's `op_retry` takes both).
  const ss = flight.step_states?.[step.id];
  const verify = verifyOf(row);
  const confirming = verify?.state === "needs_you";
  const ask = confirming ? "" : String(ss?.detail?.ask || "");
  if (ask === "blocked" || ask === "memory") {
    const items = Array.isArray(ss?.detail?.items) ? (ss?.detail?.items as unknown[]).map(String) : [];
    const why = verifyStopWhy(ss?.note, ask);
    return (
      <div className={"fl-you fl-you-verify fl-you-needs" + (compact ? " fl-you-compact" : "")} onClick={stop} data-no-drag>
        <p className="fl-you-text">{why}</p>
        {!compact && items.length > 0 && (
          <ul className="fl-you-needs-list">
            {items.map((t, i) => (
              <li key={i}>{t}</li>
            ))}
          </ul>
        )}
        <div className="fl-you-acts">
          <button
            type="button"
            className="btn-primary fl-btn fl-retry"
            disabled={busy}
            title={ask === "memory" ? "Try to start the check again" : "Its agent verifies its work again"}
            onClick={() => {
              setBusy(true);
              void stepRetry(flight.id, step.id).finally(() => setBusy(false));
            }}
          >
            Retry
          </button>
          {skip}
        </div>
      </div>
    );
  }
  // A plain sign-off (no step left for a person): VerifyConfirm's heading
  // is the agent's own word (`verify.detail`) — no line above it saying the
  // same, and never "do yours" when there is nothing of yours.
  const signOff = confirming && !(verify.human_steps || []).length && !(Number(verify.steps?.human) || 0);
  if (confirming && !compact && !plan)
    return (
      <div className="fl-you fl-you-verify fl-you-needs" onClick={stop} data-no-drag>
        {!signOff && <p className="fl-you-text">{verifyLine(verify)}</p>}
        <VerifyConfirm verify={verify} session={row?.title} />
        <div className="fl-you-acts">{skip}</div>
      </div>
    );
  if (compact || !plan)
    return (
      <div className={"fl-you fl-you-verify" + (compact ? " fl-you-compact" : "")} onClick={stop} data-no-drag>
        {!compact && <p className="fl-you-text">{VERIFY_WRITING}</p>}
        <div className="fl-you-acts">
          {plan && (
            <button type="button" className="btn-primary fl-btn fl-behaviors" title="Its verify steps and their results, on the session's frame in the Sky" onClick={() => openBehaviors({ planId: plan.id })}>
              Verify
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

/** The pane strip's button while this session's own agent verifies a Verify stop. */
export const VERIFY_WALK_HERE = "Its agent is verifying its work — open the session to talk to it";

/** A Verify stop parked before its steps are written. */
export const VERIFY_WRITING = "Its agent is writing its verify steps… they show here once they are written.";

/** Why a Verify stop parked on you couldn't go on, as its tile says it:
 * the server's note with a capital ("The check couldn't finish: …"), else
 * the plain words of its `ask`. */
export function verifyStopWhy(note: unknown, ask: string): string {
  const n = String(note ?? "").trim();
  if (n) return n[0].toUpperCase() + n.slice(1);
  return ask === "memory" ? "Couldn't start the check: memory is low" : "The check couldn't finish";
}

/** The session walking the flight's current Verify stop IN PLACE — the
 * server handed the steps to the flight's own session's agent
 * (`detail.in_session`, `detail.walk_session`) — or "" (another stop, a
 * separate checker, or the stop is parked on you / halted). */
export function verifyWalkSession(flight: Pick<Flight, "steps" | "cursor" | "state" | "step_states" | "session"> | null | undefined): string {
  if (!flight || flight.state === "waiting_you" || flight.state === "halted") return "";
  const step = flight.steps[flight.cursor];
  if (!step || step.kind !== "verify") return "";
  const d = flight.step_states?.[step.id]?.detail;
  if (!d || d.in_session !== true) return "";
  return String(d.walk_session || flight.session || "");
}

/** "Its agent is verifying its work — open the session to talk to it": a
 * Verify stop being worked by this session's own agent. One press opens its
 * Agent tab (`onOpen`), where the person can talk to it mid-verify. */
export function VerifyWalkHint({ session, here, onOpen }: { session: string; here: boolean; onOpen(): void }) {
  if (!session) return null;
  return (
    <button
      type="button"
      className="test-btn fl-btn fl-walk-hint"
      title={here ? "Opens its Agent tab, where it verifies its work and you can talk to it" : "Opens " + windowName(session) + ", where its agent verifies the work and you can talk to it"}
      onClick={(e) => {
        e.stopPropagation();
        onOpen();
      }}
    >
      {here ? VERIFY_WALK_HERE : windowName(session) + "'s agent is verifying the work — open it to talk to it"}
    </button>
  );
}

/** Whether "Let the agent fix it" applies to the flight's current stop — the
 * server's `op_fix` rule (backend/web/core/flights.py): the stop FAILED (and
 * the route did not already move on past it), or its failure was handed to
 * you (`on_fail: you` — waiting on you with `detail.failed_reason`). Any
 * kind: the agent gets the failure, then the stop runs again. A halt point
 * (paused, nothing failed) has nothing to fix. */
export function canFixStep(flight: Pick<Flight, "steps" | "cursor" | "state" | "step_states" | "session"> | null | undefined): boolean {
  // No session (a free run): no agent to hand it to — the server refuses
  // it (409 `no_session`).
  if (!flight || !flight.session || (flight.state !== "halted" && flight.state !== "waiting_you")) return false;
  const step = flight.steps[flight.cursor];
  const ss = step ? flight.step_states?.[step.id] : undefined;
  if (!ss) return false;
  if (ss.state === "failed") return !ss.detail?.moved_on;
  return ss.state === "waiting_you" && !!ss.detail?.failed_reason;
}

/** Whether the stop the route moves to after its current one (passing the
 * ones already done or skipped) is a halt point not yet let through. */
export function nextIsHaltPoint(flight: Pick<Flight, "steps" | "cursor" | "step_states">): boolean {
  for (let k = flight.cursor + 1; k < flight.steps.length; k++) {
    const s = flight.steps[k];
    const ss = flight.step_states?.[s.id];
    if (ss && (ss.state === "done" || ss.state === "skipped")) continue;
    return !!s.halt && !ss?.detail?.halt_hit;
  }
  return false;
}

/** Whether Skip on this route should also Carry on: the server keeps a
 * paused route paused after a skip (`_advance_keeping_pause`), so Skip
 * carries on too, as it says — unless the next stop is a halt point itself
 * (it halts there) or there is no stop left (the skip finished the route;
 * Carry on would be refused). */
export function skipCarriesOn(flight: Pick<Flight, "state" | "steps" | "cursor" | "step_states">): boolean {
  if (flight.state !== "paused") return false;
  const ahead = flight.steps.slice(flight.cursor + 1).some((s) => {
    const st = flight.step_states?.[s.id]?.state;
    return st !== "done" && st !== "skipped";
  });
  return ahead && !nextIsHaltPoint(flight);
}

/** Done / Skip on a perch, its checklist as tick boxes (ticking them all is
 * what enables Done); Retry / Skip on a halted stop — and on one whose
 * failure was handed to you (`on_fail: you`: Done would mark the failure
 * done; Retry runs it again) — and Let the agent fix it wherever the server
 * takes it (`canFixStep`); Carry on / Skip on a paused route, at a reached
 * halt point (`atHaltPoint`: nothing failed, so nothing to retry) or where a
 * person paused it. A Verify stop parked on you answers its verify instead
 * (`VerifyStopActions`). `inTile` (a stop on the Sky): its text is the
 * tile's own line under its name, not a paragraph here. */
export function YouStepActions({
  flight,
  compact = false,
  inTile = false,
  gone = false,
}: {
  flight: Flight;
  compact?: boolean;
  inTile?: boolean;
  /** Its session is closed (not among the rows): no agent to hand a fix
   * to, and Stop the route is the way to end it (it is drawn nowhere else
   * with a ⋯). */
  gone?: boolean;
}) {
  const step = flight.steps[flight.cursor];
  const items = youItems(step);
  const [ticked, setTicked] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState(false);
  const haltPoint = atHaltPoint(flight);
  const paused = haltPoint || flight.state === "paused";
  const failedReason = step ? String(flight.step_states?.[step.id]?.detail?.failed_reason ?? "").trim() : "";
  const handed = flight.state === "waiting_you" && !!failedReason;
  if (!step || (flight.state !== "waiting_you" && flight.state !== "halted" && !paused)) return null;
  if (step.kind === "verify" && flight.state === "waiting_you" && !handed) return <VerifyStopActions flight={flight} compact={compact} />;
  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
    } finally {
      setBusy(false);
    }
  };
  // A Review out of fix rounds with findings still open, the perch asking
  // for the checks owed at the end: their own words (lib/stopAsks).
  const ask = flightAsk(flight);
  const rec = flight.step_states?.[step.id];
  const offers = askOffers(ask) || [];
  const owed = ask === "owed" ? owedGroups(rec?.detail?.owed) : [];
  if (ask === "unresolved") {
    const findings = findingsOf(rec?.detail);
    return (
      <div className={"fl-you fl-you-unresolved" + (compact ? " fl-you-compact" : "") + (inTile ? " fl-you-tile" : "")} onClick={stop} data-no-drag>
        {!compact && !inTile && <p className="fl-you-text">{askSentence(rec, "Findings are still open after the last round")}</p>}
        {!compact && <ReviewFindings findings={findings} />}
        <div className="fl-you-acts">
          {offers.map((o) => (
            <button
              key={o.key}
              type="button"
              className={(o.primary ? "btn-primary" : "test-btn") + " fl-btn fl-" + (o.key === "retry" ? "retry" : "carry")}
              disabled={busy}
              title={o.title + (compact && findings ? "\n\n" + findings : "")}
              onClick={() => void run(() => (o.key === "retry" ? stepRetry(flight.id, step.id) : stepDone(flight.id, step.id)))}
            >
              {o.label}
            </button>
          ))}
        </div>
      </div>
    );
  }
  const doneOffer = offers.find((o) => o.key === "done");
  const key = (i: number) => flight.id + ":" + step.id + ":" + i;
  const allTicked = items.every((_, i) => ticked[key(i)]);
  const halted = flight.state === "halted" || paused || handed;
  const fix = !paused && !gone && canFixStep(flight);
  // The owed perch's sentence is the server's note ("Before this route
  // finishes: 3 checks for you"), which its own text alone would leave out.
  const text = handed ? (/^failed\b/i.test(failedReason) ? failedReason : "Failed: " + failedReason) : ask === "owed" ? askSentence(rec, youText(flight)) : youText(flight);
  return (
    <div className={"fl-you" + (compact ? " fl-you-compact" : "") + (inTile ? " fl-you-tile" : "")} onClick={stop} data-no-drag>
      {!compact && !inTile && text && (!halted || handed) && <p className="fl-you-text">{text}</p>}
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
      {!halted && !compact && <OwedChecks groups={owed} session={flight.session} />}
      <div className="fl-you-acts">
        {paused ? (
          <button
            type="button"
            className="btn-primary fl-btn fl-carry"
            disabled={busy}
            title={haltPoint ? "Carry on past the halt point — “" + step.title + "” runs, and the route goes on" : "Carry on — the route goes on from “" + step.title + "”"}
            onClick={() => void run(() => controlFlight(flight.id, "resume"))}
          >
            Carry on
          </button>
        ) : halted ? (
          <button
            type="button"
            className="btn-primary fl-btn"
            disabled={busy}
            title={handed ? "Run “" + step.title + "” again" : "Run this stop again"}
            onClick={() => void run(() => stepRetry(flight.id, step.id))}
          >
            Retry
          </button>
        ) : (
          <button
            type="button"
            className="btn-primary fl-btn fl-done"
            disabled={busy || !allTicked}
            title={allTicked ? (doneOffer ? doneOffer.title + (compact && owed.length ? "\n\n" + owedItemsText(owed) : "") : "Mark “" + step.title + "” done — the route goes on") : "Tick every item first"}
            onClick={() => void run(() => stepDone(flight.id, step.id))}
          >
            {doneOffer ? doneOffer.label : "Done"}
          </button>
        )}
        {fix && (
          <button
            type="button"
            className="test-btn fl-btn fl-fix"
            disabled={busy}
            title={step.kind === "verify" ? "Hand the failure to the agent — Verify runs again after its fix" : "Hand the failure to the agent — “" + step.title + "” runs again after its fix"}
            onClick={() => void run(() => stepFix(flight.id, step.id))}
          >
            Let the agent fix it
          </button>
        )}
        {compact && owed.length > 0 && flight.session && (
          // The one-line strip has no room for each check's "This one didn't
          // work": this opens the Route tab, where the stop lists them.
          <button type="button" className="test-btn fl-btn fl-owed-open" title={OWED_OPEN_TITLE} onClick={() => openFlightTab(flight.session)}>
            {OWED_OPEN}
          </button>
        )}
        <button
          type="button"
          className="test-btn fl-btn fl-skip"
          disabled={busy}
          title={"Skip “" + step.title + "” — the route goes on without it"}
          // Skip carries on a paused route too, where there is somewhere to go (`skipCarriesOn`).
          onClick={() => void run(async () => (await stepSkip(flight.id, step.id)) && skipCarriesOn(flight) && controlFlight(flight.id, "resume"))}
        >
          Skip
        </button>
        {gone && (
          <button
            type="button"
            className="test-btn fl-btn"
            disabled={busy}
            title="Its session is closed — end this route"
            onClick={() => void run(() => flightAction("Stop the route", flightPath(flight.id, "/cancel"), {}, "Route stopped"))}
          >
            Stop the route
          </button>
        )}
      </div>
    </div>
  );
}

/** The flight's own ⋯: Open session · Pause / Carry on · Cancel. Cancel asks
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
  const rows = useInstances().data;
  const items: MenuItem[] = [];
  if (openSession && flight.session)
    items.push({ key: "open", label: "Open session", onSelect: () => openFlightSession(flight.session) });
  if (live) {
    items.push(
      flight.state === "halted" || flight.state === "paused"
        ? { key: "resume", label: "Carry on", title: "Go on from the current stop", onSelect: () => void controlFlight(flight.id, "resume") }
        : { key: "pause", label: "Pause", title: "Pause the route at its current stop — Carry on picks it up again", onSelect: () => void controlFlight(flight.id, "pause") }
    );
    items.push({ key: "cancel", label: "Stop the route", danger: true, title: "End this route — its session is kept", onSelect: onCancelAsk });
  }
  items.push(...flyAgainItems(flight, sessionGone(flight, rows)));
  return (
    <span onClick={stop} className="fl-menu" data-no-drag>
      <OverflowMenu label="Route actions" items={items} />
    </span>
  );
}

/** An ended route's (done, cancelled, halted) Fly again in a ⋯: from where
 * it stopped, and from another stop (the route's stops, at the press). */
export function flyAgainItems(flight: Flight, gone = false): MenuItem[] {
  if (!canFlyAgain(flight)) return [];
  return [
    { key: "again", label: flyAgainLabel(flight), title: flyAgainTitle(flight, gone), onSelect: () => void flyAgain(flight.id) },
    { key: "again-pick", label: "Fly again from another stop…", title: "Pick the stop it flies again from", onSelect: () => pickAgainStop(flight) },
  ];
}

/** Whether the flight's session is gone (not among the rows; unknown while
 * they load = not gone). */
export function sessionGone(flight: Pick<Flight, "session">, rows: readonly Pick<Instance, "title">[] | null | undefined): boolean {
  return !!rows && !!flight.session && !rows.some((r) => r.title === flight.session);
}

/** Fly again on an ended route, in a row: "Fly again from <stop>" (where it
 * stopped — the primary button on a cancelled or halted route) and "From
 * another stop…" (the beginning, or any stop). */
export function FlyAgain({ flight }: { flight: Flight }) {
  const [busy, setBusy] = useState(false);
  const gone = sessionGone(flight, useInstances().data);
  if (!canFlyAgain(flight)) return null;
  return (
    <span className="fl-again-row" onClick={stop} data-no-drag>
      <button
        type="button"
        className={(flyAgainPrimary(flight) ? "btn-primary" : "test-btn") + " fl-btn fl-fly-again"}
        disabled={busy}
        title={flyAgainTitle(flight, gone)}
        onClick={async () => {
          setBusy(true);
          try {
            await flyAgain(flight.id);
          } finally {
            setBusy(false);
          }
        }}
      >
        {flyAgainLabel(flight)}
      </button>
      <button type="button" className="linklike fl-start-at" disabled={busy} title="The beginning, or any of its stops" onClick={(e) => pickAgainStop(flight, e.currentTarget)}>
        From another stop…
      </button>
    </span>
  );
}

/** A box's ⋯ on a live flight: Jump here (any stop but the current one),
 * Retry (the halted or failed one), and the flight's Pause / Carry on / Cancel. */
export function stopMenuItems(flight: Flight, n: DiagramNode, onCancelAsk: () => void): MenuItem[] {
  if (!isLive(flight) || n.kind !== "stop" || n.index === undefined) return [];
  const step = flight.steps[n.index];
  if (!step) return [];
  const items: MenuItem[] = [];
  const atCursor = n.index === flight.cursor;
  if (!atCursor) items.push({ key: "jump", label: "Jump here", title: "Move the route to this stop", onSelect: () => void jumpFlight(flight.id, step.id, step.title) });
  // A reached halt point failed nothing: Carry on (below), no Retry.
  if ((n.state === "halted" || n.state === "failed") && !(atCursor && atHaltPoint(flight)))
    items.push({ key: "retry", label: "Retry", title: "Run this stop again", onSelect: () => void stepRetry(flight.id, step.id) });
  if (atCursor)
    items.push(
      flight.state === "halted" || flight.state === "paused"
        ? { key: "resume", label: "Carry on", title: "Go on from here", onSelect: () => void controlFlight(flight.id, "resume") }
        : { key: "pause", label: "Pause", title: "Pause the route at this stop — Carry on picks it up again", onSelect: () => void controlFlight(flight.id, "pause") }
    );
  items.push({ key: "cancel", label: "Stop the route", danger: true, title: "End this route — its session is kept", onSelect: onCancelAsk });
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
          title="Stop this route?"
          body="It stops where it is. Its session, branch and work are kept."
          confirmLabel="Stop the route"
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
export function liveBoxParts(flight: Flight | null, expand: boolean, onCancelAsk: () => void, opts: { verifyInPlace?: boolean; inTile?: boolean } = {}) {
  const cur = flight ? flight.steps[flight.cursor] : null;
  // A Verify stop parked on its verify (not on a failure handed to you).
  const verifyHere = !!flight && cur?.kind === "verify" && flight.state === "waiting_you" && !flight.step_states?.[cur.id]?.detail?.failed_reason;
  const expanded = (n: DiagramNode) =>
    !!flight &&
    expand &&
    n.kind === "stop" &&
    n.index === flight.cursor &&
    (n.state === "perched" || n.state === "halted" || n.state === "paused") &&
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
        {expanded(n) && <YouStepActions flight={flight} inTile={opts.inTile} />}
      </>
    );
  };
  const sizeOf = (n: DiagramNode, base: BoxSize): BoxSize => {
    if (!expanded(n)) return base;
    // A Verify stop's behaviors: room for the card (it scrolls past it).
    if (verifyHere) return { w: Math.max(base.w, 320), h: base.h + 220 };
    const items = n.state === "perched" ? youItems(cur).length : 0;
    const text = n.state === "perched" && (youText(flight!) || n.reason) ? 24 : 0;
    const rec = n.step ? flight!.step_states?.[n.step.id] : undefined;
    return { w: Math.max(base.w, n.ask ? ASK_TILE_W : 250), h: base.h + text + items * 23 + 38 + askRoom(n.ask, rec?.detail) };
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
                <span className="fl-now-paused" title={flight.reason === HALT_POINT_REASON ? "The route reached a halt point and stopped before this stop — Carry on to go on" : "Paused by you — Carry on to go on"}>
                  {flight.reason === HALT_POINT_REASON ? "Paused at a halt point" : "Paused"}
                </span>
              )}
              {flight.state === "halted" && flight.reason && <span className="fl-now-reason">{flight.reason}</span>}
              <OwedNote note={owedNoteOf(flight)} groups={owedGroups(flight.owed)} />
            </>
          ) : null}
          <span className="fl-progress muted">
            {prog.done}/{prog.total}
          </span>
        </div>
        {(flight.state === "done" || flight.state === "cancelled") && canFlyAgain(flight) && (
          <div className="fl-strip-again">
            <FlyAgain flight={flight} />
          </div>
        )}
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
  // The stop's name and "Needs you" always show; the plan name, then the
  // sentence, take the cut; the dots go whole. The tooltip has it all.
  const parts = stripParts(full || row, now);
  const whole = parts.whole;
  const waiting = row.state === "waiting_you" || row.state === "halted";
  // Parked (on you, halted) or paused: its one-click way on, right here.
  const parked = waiting || row.state === "paused";
  const walking = verifyWalkSession(full);
  return (
    <div className={"fl-strip fl-state-" + row.state + (waiting || rowWaitsOnYou(row) ? " fl-needs" : "")} data-tour="flights-strip" onMouseDown={stop}>
      <div className="fl-strip-row">
        <button
          type="button"
          className="fl-strip-line"
          title={"Open the Route tab — " + whole + (full ? "\n" + full.steps.map((s) => s.title).join(" → ") : "")}
          onClick={() => openFlightTab(inst.title)}
        >
          <Bird size={13} />
          {parts.state ? (
            <span className="fl-strip-text fl-strip-parts" aria-label={whole}>
              {parts.plan && <span className="fl-strip-plan">{parts.plan}</span>}
              {parts.dots && (
                // Its own line-high box that wraps: dots that don't fit drop
                // to the hidden second line, whole — never "●●●●…".
                <span className="fl-strip-dots">
                  <span className="fl-strip-dots-in">{parts.dots}</span>
                </span>
              )}
              <span className="fl-strip-state">{parts.state}</span>
              {parts.what && <span className="fl-strip-what">{parts.what}</span>}
              <span className="fl-strip-stop">{parts.stop}</span>
            </span>
          ) : (
            <span className="fl-strip-text">{line}</span>
          )}
        </button>
        <OwedNote note={owedNoteOf(full || row)} groups={owedGroups((full || row).owed)} align="end" />
        {full && parked && <YouStepActions flight={full} compact />}
        {walking && (
          <VerifyWalkHint
            session={walking}
            here={walking === inst.title}
            onOpen={() => (walking === inst.title ? useUi.getState().setLastTab(inst.title, "agent") : selectSession(walking))}
          />
        )}
        {full && <FlightMenu flight={full} onCancelAsk={cancel.ask} openSession={false} />}
      </div>
      {cancel.node}
    </div>
  );
}
