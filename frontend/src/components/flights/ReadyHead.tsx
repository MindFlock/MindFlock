/** Round 19: a chain built on a session waits, ready, until ▶ Run or the
 * session's next prompt (docs/flights.md "Round 19"). On the session tile
 * (the Sky's chain and the Flight tab): a "Ready" chip, ▶ Run, and how it
 * starts — a press flips it between "on your next prompt" and "only on Run". */
import { useState, type JSX } from "react";
import type { Flight } from "../../lib/flights";
import { runFlight, setStartOn } from "../../lib/flightsApi";
import { toast } from "../../lib/toast";

export function startWord(f: Pick<Flight, "start_on">): string {
  return f.start_on === "run" ? "runs when you press ▶ Run" : "runs on your next prompt";
}

export function ReadyHead({ flight }: { flight: Pick<Flight, "id" | "start_on" | "plan_name"> }): JSX.Element {
  const [busy, setBusy] = useState(false);
  const run = async () => {
    setBusy(true);
    try {
      if (await runFlight(flight.id)) toast("Running “" + (flight.plan_name || "the chain") + "”");
    } finally {
      setBusy(false);
    }
  };
  const flip = () => void setStartOn(flight.id, flight.start_on === "run" ? "prompt" : "run");
  return (
    <span className="sky-ready" data-no-drag onPointerDown={(e) => e.stopPropagation()}>
      <span className="sky-head-chip tone-ready" title="Built, not running yet">
        Ready
      </span>
      <button type="button" className="fl-icon-btn sky-run" disabled={busy} title="Run this chain now" aria-label="Run this chain now" onClick={() => void run()}>
        ▶ Run
      </button>
      <button type="button" className="linklike sky-start-on" title="Change how this chain starts" onClick={flip}>
        {startWord(flight)}
      </button>
    </span>
  );
}
