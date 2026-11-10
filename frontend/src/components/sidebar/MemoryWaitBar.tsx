/** The rail's calm line while the launch gate (backend core/launch_gate.py)
 * holds automatic starts back, in the words of what holds them
 * (lib/waitWhy): "Memory low — new agents wait (3 waiting)", "Agent limit
 * reached — new agents wait (1 waiting)", "Starting agents one at a time (2
 * waiting)". Each waiting session also says why on its own row (an agent's
 * queued worker shows as a provisioning row), with Start anyway and Stop
 * waiting; this line says it once, and that it clears by itself. */

import { useMemory } from "../../state/queries";
import { holdingLine, startWaiting } from "../../lib/launchGate";
import { gateWhy, waitClears } from "../../lib/waitWhy";
import { displayName } from "../../state/store";

export function MemoryWaitBar() {
  const { data } = useMemory();
  const g = data?.gate;
  const line = holdingLine(g);
  const why = gateWhy(g);
  if (!g || !line || !why) return null;
  const next = g.queued[0];
  const reason = (g.reason || "").trim();
  const tip =
    (reason ? reason + "\n" : "") +
    (next ? "Next: " + displayName(next.title) + ". " + waitClears(why) + ". " : "") +
    "Waiting agents start one at a time.";
  return (
    <div id="memory-wait-bar" role="status" aria-label="Agents waiting to start">
      <span className="mw-text" title={tip}>
        {line}
      </span>
      {next && (
        <button
          type="button"
          className="ob-btn"
          title={"Start " + displayName(next.title) + " now, without waiting"}
          onClick={() => void startWaiting(next.title)}
        >
          Start next anyway
        </button>
      )}
    </div>
  );
}
