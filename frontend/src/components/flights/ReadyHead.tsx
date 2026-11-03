/** Round 19: a chain built on a session waits, ready, until ▶ Run or the
 * session's next prompt (docs/flights.md "Round 19"). On the session tile
 * (the Sky's chain and the Flight tab), under the tile's name: one row
 * "[Ready] [▶ Run]", then one small muted line saying how it starts — a press
 * flips it between "on your next prompt" and "only on ▶ Run". The tile's own
 * sub-line does NOT repeat any of this (SkyLine / FlightChain `headSub`).
 *
 * The button's class is `sky-ready-run`, never `sky-run`: `.sky-chain
 * .sky-run` is the free chain's hover-only ▶ trigger circle (SkyChain.css,
 * GroundedLine.css), and this head renders inside `.sky-chain`. */
import { useState, type JSX } from "react";
import type { Flight } from "../../lib/flights";
import { runFlight, setStartOn } from "../../lib/flightsApi";
import { toast } from "../../lib/toast";

/** The head's start-on line: short enough for one line under a 132px tile. */
export function startWord(f: Pick<Flight, "start_on">): string {
  return f.start_on === "run" ? "starts on ▶ Run" : "starts on next prompt";
}

/** The start-on toggle's tooltip: what it does now and what a click does. */
export function startTitle(f: Pick<Flight, "start_on">): string {
  return f.start_on === "run"
    ? "Starts only when you press ▶ Run. Click to start it on this session's next prompt too."
    : "Starts on this session's next prompt (or ▶ Run). Click to start it only on ▶ Run.";
}

export function ReadyHead({ flight }: { flight: Pick<Flight, "id" | "start_on" | "plan_name"> }): JSX.Element {
  const [busy, setBusy] = useState(false);
  const run = async () => {
    setBusy(true);
    try {
      if (await runFlight(flight.id)) toast("Running “" + (flight.plan_name || "the route") + "”");
    } finally {
      setBusy(false);
    }
  };
  const flip = () => void setStartOn(flight.id, flight.start_on === "run" ? "prompt" : "run");
  return (
    <span className="sky-ready" data-no-drag onPointerDown={(e) => e.stopPropagation()}>
      <span className="sky-ready-row">
        <span className="sky-head-chip tone-ready" title="Built, not running yet">
          Ready
        </span>
        <button type="button" className="sky-ready-run" disabled={busy} title="Run this route now" aria-label="Run this route now" onClick={() => void run()}>
          ▶ Run
        </button>
      </span>
      <button type="button" className="sky-start-on" title={startTitle(flight)} onClick={flip}>
        {startWord(flight)}
      </button>
    </span>
  );
}
