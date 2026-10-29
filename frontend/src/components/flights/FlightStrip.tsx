/** A flight drawn as one strip: its headline, its blocks as a row of chips
 * (done ✓ · current ● pulsing · pending ○ · failed ✗ · skipped –; *you* blocks
 * outlined in the needs-you gold), the current step and how long it has been
 * there, Done / Skip on a parked *you* step, and a ⋯ (Open session · Pause /
 * Resume · Jump to… · Cancel).
 *
 * Two homes, one set of parts: the Board's strip (`FlightCard`, a click on it
 * opens the session and closes the dialog) and the pane's one-line strip
 * (`PaneFlightStrip`, above the terminal, where the guided next-step button
 * was). Every action posts through lib/flightsApi. */

import { useState, type MouseEvent, type ReactNode } from "react";
import type { Instance } from "../../api/types";
import { useFlightKinds, useFlights } from "../../state/queries";
import { useUi } from "../../state/store";
import { selectSession } from "../../lib/sessionActions";
import { useNow } from "../../lib/useNow";
import { OverflowMenu, type MenuItem } from "../OverflowMenu";
import { InlineConfirm } from "../InlineConfirm";
import {
  currentElapsed,
  flightProgress,
  isLive,
  rowWaitsOnYou,
  stepActor,
  stepGlyph,
  stripLabel,
  youItems,
  youText,
  type Flight,
  type FlightRow,
  type StepKindDef,
} from "../../lib/flights";
import { controlFlight, jumpFlight, stepDone, stepRetry, stepSkip } from "../../lib/flightsApi";

const stop = (e: MouseEvent) => e.stopPropagation();

/** Open a flight's session: close whatever dialog is up, then select it —
 * `selectSession` is the one way in. */
export function openFlightSession(title: string) {
  if (!title) return;
  useUi.getState().closeDialog();
  selectSession(title);
}

/** The headline a strip leads with: the subject's title, else its reference,
 * else the session. */
export function flightHeadline(f: Flight): string {
  const s = f.subject;
  if (s?.title) return s.title;
  if (s?.id) return (s.kind === "pr" || s.kind === "issue" ? "#" : "") + s.id;
  return f.session || f.plan_name;
}

/** The row of block chips. In jump mode each chip is a button that moves the
 * cursor there. */
export function BlockChips({
  flight,
  kinds,
  onJump,
}: {
  flight: Flight;
  kinds?: readonly StepKindDef[] | null;
  /** Jump mode: pressing a chip jumps to it. */
  onJump?: (stepId: string, title: string) => void;
}) {
  const { states } = flightProgress(flight);
  return (
    <ol className={"fl-chips" + (onJump ? " fl-chips-jump" : "")} aria-label="Steps">
      {flight.steps.map((s, i) => {
        const st = states[i];
        const you = stepActor(s, kinds) === "you";
        const cls =
          "fl-chip fl-chip-" + st + (you ? " fl-chip-you" : " fl-chip-robot") + (i === flight.cursor && isLive(flight) ? " fl-chip-current" : "");
        const label = s.title + " — " + (you ? "you" : "robot") + ", " + st.replace("_", " ");
        return (
          <li key={s.id} className={cls} title={label}>
            {onJump ? (
              <button
                type="button"
                className="fl-chip-btn"
                aria-label={"Jump to " + s.title}
                onClick={(e) => {
                  e.stopPropagation();
                  onJump(s.id, s.title);
                }}
              >
                <span className="fl-chip-glyph" aria-hidden="true">{stepGlyph(st)}</span>
                <span className="fl-chip-text">{s.title}</span>
              </button>
            ) : (
              <>
                <span className="fl-chip-glyph" aria-hidden="true">{stepGlyph(st)}</span>
                <span className="fl-chip-text">{s.title}</span>
              </>
            )}
          </li>
        );
      })}
    </ol>
  );
}

/** Done / Skip on a parked *you* step, its checklist as tick boxes (ticking
 * them all is what enables Done); Retry / Skip on a halted one. */
export function YouStepActions({ flight, compact = false }: { flight: Flight; compact?: boolean }) {
  const step = flight.steps[flight.cursor];
  const items = youItems(step);
  const [ticked, setTicked] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState(false);
  if (!step || (flight.state !== "waiting_you" && flight.state !== "halted")) return null;
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
    <div className={"fl-you" + (compact ? " fl-you-compact" : "")} onClick={stop}>
      {!compact && text && <p className="fl-you-text">{text}</p>}
      {!halted && items.length > 0 && (
        <ul className="fl-you-items">
          {items.map((it, i) => (
            <li key={key(i)}>
              <label>
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
            title="Run this step again"
            onClick={() => void run(() => stepRetry(flight.id, step.id))}
          >
            Retry
          </button>
        ) : (
          <button
            type="button"
            className="btn-primary fl-btn fl-done"
            disabled={busy || !allTicked}
            title={allTicked ? "Mark “" + step.title + "” done — the flight goes on" : "Tick every item first"}
            onClick={() => void run(() => stepDone(flight.id, step.id))}
          >
            Done
          </button>
        )}
        <button
          type="button"
          className="test-btn fl-btn fl-skip"
          disabled={busy}
          title={"Skip “" + step.title + "” — the flight goes on without it"}
          onClick={() => void run(() => stepSkip(flight.id, step.id))}
        >
          Skip
        </button>
      </div>
    </div>
  );
}

/** The ⋯: Open session · Pause / Resume · Jump to… · Cancel. Cancel asks
 * inline first (it ends the flight; the session is kept). */
export function FlightMenu({
  flight,
  onJumpMode,
  onCancelAsk,
  openSession = true,
}: {
  flight: Flight;
  onJumpMode(): void;
  onCancelAsk(): void;
  /** The pane strip is already in the session: no "Open session". */
  openSession?: boolean;
}) {
  const live = isLive(flight);
  const items: MenuItem[] = [];
  if (openSession && flight.session)
    items.push({ key: "open", label: "Open session", onSelect: () => openFlightSession(flight.session) });
  if (live) {
    items.push(
      flight.state === "halted"
        ? { key: "resume", label: "Resume", title: "Carry on from the current step", onSelect: () => void controlFlight(flight.id, "resume") }
        : { key: "pause", label: "Pause", title: "Hold the flight at its current step", onSelect: () => void controlFlight(flight.id, "pause") }
    );
    items.push({ key: "jump", label: "Jump to…", title: "Move the flight to another block", onSelect: onJumpMode });
    items.push({ key: "cancel", label: "Cancel", danger: true, title: "End this flight — its session is kept", onSelect: onCancelAsk });
  }
  return (
    <span onClick={stop} className="fl-menu">
      <OverflowMenu label="Flight actions" items={items} />
    </span>
  );
}

/** The jump row and the cancel question, under a strip. */
function useStripControls(flight: Flight) {
  const [jumping, setJumping] = useState(false);
  const [asking, setAsking] = useState(false);
  const [busy, setBusy] = useState(false);
  const onJump = jumping
    ? (stepId: string, title: string) => {
        setJumping(false);
        void jumpFlight(flight.id, stepId, title);
      }
    : undefined;
  const extra: ReactNode = (
    <>
      {jumping && (
        <div className="fl-jump-row" onClick={stop}>
          <span>Jump to… pick a block above.</span>
          <button type="button" className="test-btn fl-btn" onClick={() => setJumping(false)}>
            Cancel
          </button>
        </div>
      )}
      {asking && (
        <div onClick={stop}>
          <InlineConfirm
            title="Cancel this flight?"
            body="It stops where it is. Its session, branch and work are kept."
            confirmLabel="Cancel"
            cancelLabel="Keep it"
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
      )}
    </>
  );
  return { onJump, extra, jumpMode: () => setJumping(true), cancelAsk: () => setAsking(true) };
}

/** One flight on the Board. Clicking the strip body opens its session. */
export function FlightCard({ flight, kinds }: { flight: Flight; kinds?: readonly StepKindDef[] | null }) {
  const now = useNow(isLive(flight) ? 30_000 : false);
  const c = useStripControls(flight);
  const prog = flightProgress(flight);
  const step = prog.current;
  const elapsed = currentElapsed(flight, now);
  const waiting = flight.state === "waiting_you" || flight.state === "halted";
  const you = step ? stepActor(step, kinds) === "you" : false;
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
        title={flight.session ? "Open " + flight.session : undefined}
        onClick={() => openFlightSession(flight.session)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            openFlightSession(flight.session);
          }
        }}
      >
        <div className="fl-strip-head">
          <span className="fl-strip-title">{flightHeadline(flight)}</span>
          <span className="fl-strip-plan">✈ {flight.plan_name}</span>
          {flight.session && <span className="fl-strip-session">{flight.session}</span>}
          <FlightMenu flight={flight} onJumpMode={c.jumpMode} onCancelAsk={c.cancelAsk} />
        </div>
        <BlockChips flight={flight} kinds={kinds} onJump={c.onJump} />
        <div className="fl-strip-now">
          {flight.state === "done" ? (
            <span className="fl-landed">Landed</span>
          ) : flight.state === "cancelled" ? (
            <span className="muted">Cancelled{flight.reason ? " — " + flight.reason : ""}</span>
          ) : step ? (
            <>
              <span className={"fl-now-dot" + (you ? " fl-now-you" : "")} aria-hidden="true" />
              <span className="fl-now-title">{(you ? "You: " : "") + step.title}</span>
              {elapsed && <span className="fl-now-elapsed">{elapsed}</span>}
              {flight.state === "halted" && flight.reason && <span className="fl-now-reason">{flight.reason}</span>}
            </>
          ) : null}
          <span className="fl-progress muted">
            {prog.done}/{prog.total}
          </span>
        </div>
      </div>
      <YouStepActions flight={flight} />
      {c.extra}
    </div>
  );
}

/** The pane's strip: `✈ Ticket to merge · ●●●○○○ · Reviews settle (4m)`, with
 * Done / Skip for a *you* step and a ⋯ for Jump / Cancel. Reads the session
 * row's `flight` field, and the full flight from the shared flights query
 * when it is there (for the elapsed time, the checklist and Jump's blocks). */
export function PaneFlightStrip({ inst }: { inst: Pick<Instance, "title" | "flight"> }) {
  const row = inst.flight as FlightRow | null | undefined;
  const live = !!row && isLive(row);
  const flights = useFlights(live).data;
  const kinds = useFlightKinds(live).data;
  const now = useNow(live ? 30_000 : false);
  const full = flights?.find((f) => f.id === row?.id) || null;
  if (!row || !live) return null;
  return full ? <PaneStripFull flight={full} kinds={kinds} now={now} /> : <PaneStripRow row={row} />;
}

function PaneStripRow({ row }: { row: FlightRow }) {
  return (
    <div className={"fl-strip fl-state-" + row.state + (rowWaitsOnYou(row) ? " fl-needs" : "")} data-tour="flights-strip">
      <span className="fl-strip-line">{stripLabel(row)}</span>
    </div>
  );
}

function PaneStripFull({ flight, kinds, now }: { flight: Flight; kinds?: readonly StepKindDef[] | null; now: number }) {
  const c = useStripControls(flight);
  const step = flight.steps[flight.cursor];
  const waiting = flight.state === "waiting_you" || flight.state === "halted";
  return (
    <div className={"fl-strip fl-state-" + flight.state + (waiting ? " fl-needs" : "")} data-tour="flights-strip" onMouseDown={stop}>
      <div className="fl-strip-row">
        <span className="fl-strip-line" title={flight.steps.map((s) => s.title).join(" → ")}>
          {stripLabel(flight, now)}
        </span>
        {waiting && step && <YouStepActions flight={flight} compact />}
        <FlightMenu flight={flight} onJumpMode={c.jumpMode} onCancelAsk={c.cancelAsk} openSession={false} />
      </div>
      {c.onJump && <BlockChips flight={flight} kinds={kinds} onJump={c.onJump} />}
      {c.extra}
    </div>
  );
}
