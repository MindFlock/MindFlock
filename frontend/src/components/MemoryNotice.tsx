/** Memory-pressure notice — shown while the machine's memory reads low or
 * critical (GET /api/system/memory, backend/memory_pressure.py). A big job
 * can start agents faster than the machine holds them; once RAM runs out the
 * box swaps and the OOM killer takes agents down mid-turn. This says so
 * BEFORE that happens, and says what MindFlock is already doing about it:
 * automatic starts wait in the launch gate's queue (backend
 * core/launch_gate.py), team runs start no new lines, and a person's own
 * New / Start asks first (lib/memoryConfirm).
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
import { memoryReasons } from "./usage/memoryModel";

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

/** Whether WINDOWS (the WSL host), not the Linux side, is what reads low:
 * the host's level is set and at least as bad as the guest's. Then the
 * notice must show Windows' numbers — "13.4 GB free of 19.5 GB" under
 * "critically low" read as a bug when the 13.4 GB was inside WSL. */
export function windowsIsShort(m: MemoryReading): boolean {
  const host = RANK[m.host?.level || "ok"] ?? 0;
  const guest = RANK[m.guest_level || "ok"] ?? 0;
  return host > 0 && host >= guest;
}

/** "Windows: 0.5 GB free of 31.4 GB (2%) · WSL can reuse 0.3 GB it already
 * holds · 5.1 GB of WSL's 19.5 GB is unused inside WSL, but Windows can't take
 * it back while WSL holds it" — exported for tests. The reuse part is what
 * the server counts as Windows headroom (memory_pressure.host_headroom): WSL's
 * page cache is already Windows' memory spent, so a new agent reusing it costs
 * Windows nothing; it is named so the reader sees why the level is what it is. */
export function windowsDetail(m: MemoryReading): string {
  const h = m.host || {};
  const bits = ["Windows: " + gb(h.available) + " free of " + gb(h.total) + " (" + Math.round(h.available_pct || 0) + "%)"];
  if ((h.guest_reusable || 0) >= 0.1 * GB) bits.push("WSL can reuse " + gb(h.guest_reusable) + " it already holds");
  if (m.total) bits.push(gb(m.available) + " of WSL's " + gb(m.total) + " is unused inside WSL, but Windows can't take it back while WSL holds it");
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
  const windows = windowsIsShort(data);
  // What the server saw (stalls, swap-in, OOM kills, Windows' commit
  // charge): "running low · 13 GB free" alone read as a bug.
  const why = memoryReasons(data);
  return (
    <div id="memory-notice" role="status" className={critical ? "critical" : ""}>
      <div className="mn-head">
        ⚠ {windows ? "Windows is " + (critical ? "critically low" : "running low") + " on memory" : "Memory is " + (critical ? "critically low" : "running low")}
      </div>
      {why && <div className="mn-detail mn-why">Why: {why}</div>}
      <div className="mn-detail">{windows ? windowsDetail(data) : memoryDetail(data)}</div>
      {windows && (
        <div className="mn-detail">
          Closing Windows apps frees memory now. Restarting WSL (<code>wsl --shutdown</code>, then reopen) makes it hand back what it
          isn&apos;t using and applies any <code>.wslconfig</code> change.
        </div>
      )}
      {heavy && <div className="mn-detail mn-slots">{heavy}</div>}
      <div className="mn-detail mn-what">
        Agents that start by themselves (a worker an agent spawns, a queued prompt, a restart) wait
        until it recovers, one at a time; a start of yours asks first. Putting idle agents to sleep or
        closing sessions frees memory fastest
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
