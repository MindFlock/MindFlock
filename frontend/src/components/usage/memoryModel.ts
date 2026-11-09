/** The sidebar's overall memory row and its popover — the Usage row's twin
 * (OverallUsage). The owner, 2026-11-05, after a reboot that turned out to be
 * another app's python: "overall memory as well in the sidebar — just like
 * claude usage".
 *
 * Two sources, newest first:
 *  - GET /api/memory (core/memory_watch.py): the machine, every session's
 *    process tree biggest first, and the top processes outside any session;
 *  - GET /api/system/memory (useMemory(), the memory notice's reading): the
 *    machine only, in bytes — what an older server has, so the row still
 *    reads and the popover shows only the machine line.
 * GET /api/memory/history feeds the popover's last-hour sparkline.
 *
 * Pure (no React, no fetch) so the vitest node env can pin it. */

import type { MemoryReading } from "../../api/types";
import { fmtMb } from "../../lib/sessionMemory";

/** `system` of GET /api/memory (binary MB; any field may be null). */
export interface MemorySystem {
  total_mb?: number | null;
  available_mb?: number | null;
  swap_total_mb?: number | null;
  swap_used_mb?: number | null;
  psi_some_avg10?: number | null;
  psi_full_avg10?: number | null;
  level?: string;
  /** WSL only: Windows' own memory; `level` is the worse of it and the VM's. */
  host_total_mb?: number | null;
  host_available_mb?: number | null;
  host_level?: string | null;
  /** WSL only: the VM's own level (before Windows is folded in). */
  guest_level?: string | null;
}

export interface MemorySessionRow {
  title: string;
  mb: number;
  peak_mb?: number;
  agent_mb?: number;
  mcp_mb?: number;
  other_mb?: number;
  procs?: number;
  at?: number;
}

export interface MemoryOtherRow {
  pid: number;
  name: string;
  cmd: string;
  mb: number;
  /** The MindFlock server itself. */
  server?: boolean;
}

/** GET /api/memory. */
export interface MemoryDetail {
  system: MemorySystem;
  sessions: MemorySessionRow[];
  others: MemoryOtherRow[];
  at?: number | null;
  tick_ms?: number | null;
  tick_cpu_ms?: number | null;
}

/** One line of GET /api/memory/history. */
export interface MemoryHistorySample {
  at: number;
  system?: MemorySystem;
  sessions?: Record<string, number>;
  others?: MemoryOtherRow[];
}

export interface MemoryHistory {
  samples: MemoryHistorySample[];
}

export type MemoryLevel = "ok" | "low" | "critical" | "unknown";
/** Same vocabulary as the pane chip (lib/sessionMemory MemoryTone). */
export type MemoryRowTone = "" | "warn" | "crit";

const MIB = 1024 * 1024;

const num = (v: unknown): number | null => {
  const n = typeof v === "number" ? v : NaN;
  return Number.isFinite(n) ? n : null;
};

function asLevel(v: unknown): MemoryLevel {
  return v === "ok" || v === "low" || v === "critical" ? v : "unknown";
}

/** The machine, in MB, whichever endpoint it came from. */
export interface MachineMemory {
  totalMb: number | null;
  usedMb: number | null;
  swapTotalMb: number | null;
  swapUsedMb: number | null;
  level: MemoryLevel;
  /** WSL only: the Windows host (absent elsewhere / from older servers). */
  hostTotalMb?: number | null;
  hostUsedMb?: number | null;
  hostLevel?: MemoryLevel;
  guestLevel?: MemoryLevel;
}

/** From GET /api/memory's `system`. Used = total − available. */
export function machineFromDetail(sys: MemorySystem | null | undefined): MachineMemory {
  const s = sys || {};
  const total = num(s.total_mb);
  const avail = num(s.available_mb);
  const out: MachineMemory = {
    totalMb: total && total > 0 ? total : null,
    usedMb: total && total > 0 && avail != null ? Math.max(0, total - avail) : null,
    swapTotalMb: num(s.swap_total_mb),
    swapUsedMb: num(s.swap_used_mb),
    level: asLevel(s.level),
  };
  const hTotal = num(s.host_total_mb);
  const hAvail = num(s.host_available_mb);
  if (hTotal && hTotal > 0 && hAvail != null) {
    out.hostTotalMb = hTotal;
    out.hostUsedMb = Math.max(0, hTotal - hAvail);
    out.hostLevel = asLevel(s.host_level);
    if (s.guest_level != null) out.guestLevel = asLevel(s.guest_level);
  }
  return out;
}

/** From GET /api/system/memory (bytes) — the older server's fallback. */
export function machineFromReading(r: MemoryReading | null | undefined): MachineMemory {
  if (!r) return { totalMb: null, usedMb: null, swapTotalMb: null, swapUsedMb: null, level: "unknown" };
  const total = num(r.total);
  const avail = num(r.available);
  const swapTotal = num(r.swap_total);
  const swapFree = num(r.swap_free);
  const totalMb = total && total > 0 ? Math.floor(total / MIB) : null;
  return {
    totalMb,
    usedMb: totalMb != null && avail != null ? Math.max(0, totalMb - Math.floor(avail / MIB)) : null,
    swapTotalMb: swapTotal != null ? Math.floor(swapTotal / MIB) : null,
    swapUsedMb: swapTotal != null ? Math.max(0, Math.floor((swapTotal - (swapFree ?? 0)) / MIB)) : null,
    level: asLevel(r.level),
  };
}

/** GB with one decimal, ".0" dropped ("9.8", "16", "19.5") — binary, like
 * fmtMb. The row wants the decimal on BOTH sides ("9.8 / 19.5 GB"), which
 * fmtMb rounds away from 10 GB up. */
function gb1(mb: number): string {
  return (Math.max(0, mb) / 1024).toFixed(1).replace(/\.0$/, "");
}

/** "9.8 / 19.5 GB" — or, while one side is under 1 GB on a tiny box, each
 * side in fmtMb's own words ("812 MB / 1.9 GB"). Null when unknown. */
export function fmtUsedOfTotal(usedMb: number | null, totalMb: number | null): string | null {
  if (usedMb == null || totalMb == null || totalMb <= 0) return null;
  if (usedMb < 1000 || totalMb < 1000) return fmtMb(usedMb) + " / " + fmtMb(totalMb);
  return gb1(usedMb) + " / " + gb1(totalMb) + " GB";
}

/** The row's colour: neutral unless the server's level says otherwise —
 * amber at "low", red at "critical" (the same levels the memory notice and
 * the launch gate act on, stepped with hysteresis server-side). */
export function memoryRowTone(level: string | null | undefined): MemoryRowTone {
  if (level === "critical") return "crit";
  if (level === "low") return "warn";
  return "";
}

/** The pressure level in words, for the popover and the tooltip. */
export function levelWords(level: string | null | undefined): string {
  switch (level) {
    case "ok":
      return "OK — room to spare";
    case "low":
      return "Low — new agents wait";
    case "critical":
      return "Critical — close something";
    default:
      return "Not measured";
  }
}

/** True when Windows alone (not the WSL VM) is what raised the level. */
export function hostIsCause(m: MachineMemory): boolean {
  if (m.hostLevel == null || m.hostLevel !== m.level) return false;
  if (m.level !== "low" && m.level !== "critical") return false;
  return m.guestLevel == null || m.guestLevel !== m.level;
}

/** The collapsed row ("Memory 9.8 / 19.5 GB") and its tooltip. */
export function memoryRowText(m: MachineMemory): { head: string; title: string; share: number | null } {
  const pair = fmtUsedOfTotal(m.usedMb, m.totalMb);
  const share = m.usedMb != null && m.totalMb ? Math.min(1, m.usedMb / m.totalMb) : null;
  const head = "Memory " + (pair ?? "—");
  let title = pair
    ? "This machine's memory: " + pair + " in use (" + Math.round((share || 0) * 100) + "%)"
    : "This machine's memory";
  if (m.level === "low" || m.level === "critical") {
    title += " — " + (hostIsCause(m) ? "Windows is short of memory: " : "") + levelWords(m.level).toLowerCase();
  }
  return { head, title: title + " — click for what holds it", share };
}

/** One bar row in the popover (a session or an outside process). */
export interface MemoryPopRow {
  key: string;
  /** A session's title (click target); "" for an outside process. */
  title: string;
  label: string;
  /** Outside processes: their command line, short — the full one rides the tooltip. */
  sub: string;
  tip: string;
  mb: number;
  value: string;
  /** 0..1 of the biggest row in the popover (the bar's length). */
  frac: number;
}

export interface MemoryPopModel {
  /** [label, value] rows for the machine table. */
  machine: [string, string][];
  tone: MemoryRowTone;
  sessions: MemoryPopRow[];
  others: MemoryPopRow[];
  /** True when only the machine line is known (an older server). */
  machineOnly: boolean;
}

/** "python -m foo.bar --x" → "python -m foo.bar"; ≤ `max` chars, ellipsized. */
export function shortCmd(cmd: string, max = 40): string {
  const s = (cmd || "").replace(/\s+/g, " ").trim();
  // Drop absolute paths' directories: "/usr/bin/python3 /home/me/x.py" reads as "python3 x.py".
  const words = s.split(" ").map((w) => (/^\/\S+\/[^/]+$/.test(w) ? w.slice(w.lastIndexOf("/") + 1) : w));
  const t = words.join(" ");
  return t.length <= max ? t : t.slice(0, max - 1) + "…";
}

/** Build the popover from /api/memory (or, absent, the fallback machine). */
export function memoryPopModel(
  detail: MemoryDetail | null | undefined,
  fallback: MachineMemory,
  aliases: Record<string, string> = {},
): MemoryPopModel {
  const m = detail ? machineFromDetail(detail.system) : fallback;
  const machine: [string, string][] = [];
  const pair = fmtUsedOfTotal(m.usedMb, m.totalMb);
  if (pair) {
    const pct = Math.round(((m.usedMb || 0) / (m.totalMb || 1)) * 100);
    machine.push(["In use", pair + " (" + pct + "%)"]);
  }
  if ((m.swapUsedMb || 0) > 0) {
    machine.push(["Swap", fmtMb(m.swapUsedMb || 0) + (m.swapTotalMb ? " of " + fmtMb(m.swapTotalMb) : "")]);
  }
  const host = fmtUsedOfTotal(m.hostUsedMb ?? null, m.hostTotalMb ?? null);
  if (host) {
    const pct = Math.round(((m.hostUsedMb || 0) / (m.hostTotalMb || 1)) * 100);
    machine.push(["Windows", host + " (" + pct + "%)"]);
  }
  machine.push(["Pressure", (hostIsCause(m) ? "Windows: " : "") + levelWords(m.level)]);

  const sess = detail && Array.isArray(detail.sessions) ? detail.sessions.filter((r) => r && r.mb > 0) : [];
  const oth = detail && Array.isArray(detail.others) ? detail.others.filter((r) => r && r.mb > 0) : [];
  sess.sort((a, b) => b.mb - a.mb);
  oth.sort((a, b) => b.mb - a.mb);
  // One scale for both lists, so a 5 GB outsider is visibly bigger than
  // every session — the point of the "Outside MindFlock" list.
  const top = Math.max(1, ...sess.map((r) => r.mb), ...oth.map((r) => r.mb));

  const sessions: MemoryPopRow[] = sess.map((r) => {
    const parts: string[] = [];
    if ((r.agent_mb || 0) > 0) parts.push("agent " + fmtMb(r.agent_mb || 0));
    if ((r.mcp_mb || 0) > 0) parts.push("MCP servers " + fmtMb(r.mcp_mb || 0));
    if ((r.other_mb || 0) > 0) parts.push("other " + fmtMb(r.other_mb || 0));
    const label = aliases[r.title] || r.title;
    return {
      key: "s:" + r.title,
      title: r.title,
      label,
      sub: "",
      tip: label + ": " + fmtMb(r.mb) + (parts.length ? " — " + parts.join(" · ") : "") + " — click to open it",
      mb: r.mb,
      value: fmtMb(r.mb),
      frac: r.mb / top,
    };
  });
  const others: MemoryPopRow[] = oth.map((r) => {
    const label = r.server ? "MindFlock server" : r.name || "process " + r.pid;
    return {
      key: "p:" + r.pid,
      title: "",
      label,
      sub: shortCmd(r.cmd),
      tip: label + " (pid " + r.pid + "): " + fmtMb(r.mb) + (r.cmd ? "\n" + r.cmd : ""),
      mb: r.mb,
      value: fmtMb(r.mb),
      frac: r.mb / top,
    };
  });
  return { machine, tone: memoryRowTone(m.level), sessions, others, machineOnly: !detail };
}

/** The sparkline: the machine's used MB over the samples, as an SVG polyline
 * `points` string in a `w`×`h` box (y = 0 at the top = the machine's total).
 * Null under two usable samples. At most `maxPoints` points (evenly thinned). */
export function sparkPoints(
  samples: MemoryHistorySample[] | null | undefined,
  w: number,
  h: number,
  maxPoints = 120,
): { points: string; lastUsedMb: number; totalMb: number } | null {
  const pts: { at: number; used: number; total: number }[] = [];
  for (const s of samples || []) {
    const m = machineFromDetail(s && s.system);
    if (m.usedMb == null || m.totalMb == null || !Number.isFinite(s.at)) continue;
    pts.push({ at: s.at, used: m.usedMb, total: m.totalMb });
  }
  if (pts.length < 2) return null;
  const step = Math.max(1, Math.ceil(pts.length / maxPoints));
  const kept = pts.filter((_, i) => i % step === 0 || i === pts.length - 1);
  const t0 = kept[0].at;
  const span = Math.max(1, kept[kept.length - 1].at - t0);
  const totalMb = Math.max(...kept.map((p) => p.total));
  const r1 = (n: number) => Math.round(n * 10) / 10;
  const points = kept
    .map((p) => r1(((p.at - t0) / span) * w) + "," + r1(h - (Math.min(p.used, totalMb) / totalMb) * h))
    .join(" ");
  return { points, lastUsedMb: kept[kept.length - 1].used, totalMb };
}
