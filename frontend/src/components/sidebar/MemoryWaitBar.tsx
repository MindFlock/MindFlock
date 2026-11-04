/** "Memory low — new agents wait (3 waiting)": the rail's calm line while
 * the launch gate (backend core/launch_gate.py) holds automatic starts back.
 * Each waiting session also says "Waiting for memory" on its own row (an
 * agent's queued worker shows as a provisioning row), with Start anyway on
 * hover; this line says it once, why, and that it clears by itself. */

import { useMemory } from "../../state/queries";
import { holdingLine, startWaiting } from "../../lib/launchGate";

export function MemoryWaitBar() {
  const { data } = useMemory();
  const g = data?.gate;
  const line = holdingLine(g);
  if (!g || !line) return null;
  const next = g.queued[0];
  return (
    <div id="memory-wait-bar" role="status" aria-label="Agents waiting for memory">
      <span className="mw-text" title={(g.reason || "") + " — they start one at a time once there is room"}>
        {line}
      </span>
      {next && (
        <button
          type="button"
          className="ob-btn"
          title={"Start " + next.title + " now, past the memory gate"}
          onClick={() => void startWaiting(next.title)}
        >
          Start next anyway
        </button>
      )}
    </div>
  );
}
