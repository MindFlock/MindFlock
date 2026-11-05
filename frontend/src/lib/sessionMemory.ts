/** The pane header's memory chip ("812 MB", "1.4 GB"): how much the session's
 * whole process tree holds — the agent CLI, its MCP servers and whatever it
 * runs (tests, dev servers) — from `Instance.memory` (PSS, sampled ~15 s by
 * the server). The owner (2026-11-05, after a reboot nothing explained):
 * "a memory tracker for each session … an optional number we can see at the
 * pane header like diff number". It is the Customize → Pane header item
 * "memory" (lib/paneHeader), on by default, drawn only when the server sent a
 * reading. Pure. */

import type { SessionMemory } from "../api/types";

/** The reading worth drawing, or null (absent, null, or nonsense). */
export function memoryReading(m: SessionMemory | null | undefined): SessionMemory | null {
  if (!m || typeof m !== "object") return null;
  const mb = Number(m.mb);
  if (!Number.isFinite(mb) || mb <= 0) return null;
  return m;
}

/** Compact MB: "0 MB", "812 MB", "1.4 GB", "12 GB". Under 1 GB in whole MB;
 * from 1 GB one decimal below 10 GB, whole GB above (binary: 1024 MB = 1 GB,
 * as the server's MB are). */
export function fmtMb(mb: number): string {
  const n = Math.max(0, Number(mb) || 0);
  if (n < 1000) return Math.round(n) + " MB";
  const gb = n / 1024;
  if (gb < 10) return gb.toFixed(1).replace(/\.0$/, "") + " GB";
  return Math.round(gb) + " GB";
}

export type MemoryTone = "" | "warn" | "crit";

/** Absolute floors, for when the machine's size is unknown (and so a big box
 * still flags a session that is big on its own). */
export const WARN_MB = 2048;
export const CRIT_MB = 4096;
/** Shares of the machine's RAM. */
export const WARN_SHARE = 0.1;
export const CRIT_SHARE = 0.2;
/** Under memory pressure, how many of the biggest sessions turn red — and
 * the least they must hold to count (a 90 MB session is never the culprit). */
export const PRESSURE_TOP = 3;
export const PRESSURE_MIN_MB = 512;

/** The chip's colour — neutral unless it matters:
 *  - "crit" (red) at ≥ 20% of the machine's RAM or ≥ 4 GB; also while the
 *    machine reads low/critical (GET /api/system/memory `level`) for one of
 *    the 3 biggest sessions that holds ≥ 512 MB — the ones to close first;
 *  - "warn" (amber) at ≥ 10% of RAM or ≥ 2 GB;
 *  - "" otherwise.
 * `totalMb` 0/unknown → the absolute floors only. `rank` is the session's
 * place among this machine's sessions by MB (0 = biggest), -1 unknown. */
export function memoryTone(mb: number, totalMb: number, level?: string, rank = -1): MemoryTone {
  const share = totalMb > 0 ? mb / totalMb : 0;
  if (mb >= CRIT_MB || share >= CRIT_SHARE) return "crit";
  if ((level === "low" || level === "critical") && rank >= 0 && rank < PRESSURE_TOP && mb >= PRESSURE_MIN_MB) return "crit";
  if (mb >= WARN_MB || share >= WARN_SHARE) return "warn";
  return "";
}

/** Where `title` ranks by memory among `rows` (0 = biggest), -1 when it has
 * no reading. Ties keep the list's order. */
export function memoryRank(rows: { title: string; memory?: SessionMemory | null }[] | undefined, title: string): number {
  const sized = (rows || [])
    .map((r) => ({ title: r.title, mb: memoryReading(r.memory)?.mb ?? 0 }))
    .filter((r) => r.mb > 0)
    .sort((a, b) => b.mb - a.mb);
  return sized.findIndex((r) => r.title === title);
}

/** The chip's tooltip: "Memory: 1.4 GB (peak 2.1 GB) — agent 520 MB · MCP
 * servers 410 MB · other 480 MB (tests, servers…) · 23 processes". Parts the
 * server left at zero are dropped; the peak only when above now. */
export function memoryTip(m: SessionMemory): string {
  let s = "Memory: " + fmtMb(m.mb);
  if ((m.peak_mb || 0) > m.mb) s += " (peak " + fmtMb(m.peak_mb) + ")";
  const parts: string[] = [];
  if (m.agent_mb > 0) parts.push("agent " + fmtMb(m.agent_mb));
  if (m.mcp_mb > 0) parts.push("MCP servers " + fmtMb(m.mcp_mb));
  if (m.other_mb > 0) parts.push("other " + fmtMb(m.other_mb) + " (tests, servers…)");
  if (m.procs > 0) parts.push(m.procs + (m.procs === 1 ? " process" : " processes"));
  if (parts.length) s += " — " + parts.join(" · ");
  return s;
}
