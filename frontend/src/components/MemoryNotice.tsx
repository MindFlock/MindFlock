/** Memory-pressure notice — shown while the machine's memory reads low or
 * critical (GET /api/system/memory, backend/memory_pressure.py). A big job
 * can start agents faster than the machine holds them; once RAM runs out the
 * box swaps and the OOM killer takes agents down mid-turn. This says so
 * BEFORE that happens, and says what MindFlock is already doing about it:
 * agent-spawned sessions are refused, team runs start no new lines and the
 * revive sweep waits — a person's own New and Restart still go through.
 *
 * While the card is up it also says what `mindflock slot` is holding back
 * (GET /api/system/slots): heavy commands — test suites, builds — queued for
 * a machine-wide slot instead of all running at once.
 *
 * "Hide" lasts until the level gets WORSE (low → critical) or clears and
 * comes back: a hidden "low" must not swallow the "critical" that follows. */

import { useState } from "react";
import { useMemory, useSlots } from "../state/queries";
import type { MemoryReading, SlotJob, SlotsReading } from "../api/types";

const GB = 1024 ** 3;
const RANK: Record<string, number> = { ok: 0, unknown: 0, low: 1, critical: 2 };

function gb(n?: number): string {
  return ((n || 0) / GB).toFixed(1) + " GB";
}

/** "2.1 GB free of 23.5 GB (9%) · swap 86% used · stalled 17% of the last
 * minute" — only the parts that say something. Exported for tests. */
export function memoryDetail(m: MemoryReading): string {
  const bits = [gb(m.available) + " free of " + gb(m.total) + " (" + Math.round(m.available_pct || 0) + "%)"];
  const swapTotal = m.swap_total || 0;
  if (swapTotal > 0) {
    const used = 1 - (m.swap_free || 0) / swapTotal;
    if (used >= 0.25) bits.push("swap " + Math.round(used * 100) + "% used");
  }
  const stall = m.psi?.some_avg60 || 0;
  if (stall >= 5) bits.push("stalled on memory " + Math.round(stall) + "% of the last minute");
  return bits.join(" · ");
}

/** Who a slot job is: its session, else its folder, else its pid. */
function jobName(j: SlotJob): string {
  if (j.session) return j.session;
  const base = (j.cwd || "").replace(/\/+$/, "").split("/").pop();
  return base || (j.pid ? "pid " + j.pid : "a job");
}

/** "Heavy jobs: fix-auth running · 2 waiting for a slot" — what
 * `mindflock slot` is holding back right now, "" when nothing uses it.
 * Exported for tests. */
export function slotsLine(s: SlotsReading | undefined): string {
  if (!s || (!s.running && !s.waiting)) return "";
  const holders = s.pools.flatMap((p) => p.holders);
  const bits: string[] = [];
  if (holders.length) {
    const names = holders.slice(0, 2).map(jobName).join(", ");
    const more = holders.length > 2 ? " +" + (holders.length - 2) : "";
    bits.push(names + more + " running");
  }
  if (s.waiting) bits.push(s.waiting + " waiting for a slot");
  return "Heavy jobs: " + bits.join(" · ");
}

export function MemoryNotice() {
  const { data, isError } = useMemory();
  // The level the user hid it at; 0 = not hidden.
  const [hiddenAt, setHiddenAt] = useState(0);
  const rank = RANK[data?.level || "ok"] ?? 0;
  const shown = !!data && !isError && rank > 0 && !(hiddenAt && rank <= hiddenAt);
  // Only while the card is up: the slots are context for low memory.
  const { data: slots } = useSlots(shown);
  // Cleared (or worsened past what was hidden): forget the hide.
  if (hiddenAt && (rank === 0 || rank > hiddenAt)) setHiddenAt(0);
  if (!data || !shown) return null;
  const heavy = slotsLine(slots);

  const critical = data.level === "critical";
  return (
    <div id="memory-notice" role="status" className={critical ? "critical" : ""}>
      <div className="mn-head">⚠ Memory is {critical ? "critically low" : "running low"}</div>
      <div className="mn-detail">{memoryDetail(data)}</div>
      {heavy && <div className="mn-detail mn-slots">{heavy}</div>}
      <div className="mn-detail mn-what">
        MindFlock is holding new agent-spawned sessions, team-run lines and auto-restarts until it
        recovers. Closing idle sessions frees memory fastest
        {critical ? " — the system may start killing agents soon." : "."}
      </div>
      <div className="mn-foot">
        <button type="button" className="mn-btn" onClick={() => setHiddenAt(rank)}>
          Hide
        </button>
      </div>
    </div>
  );
}
