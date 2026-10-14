/** Formatting helpers (ports of app.js sections 2/5/7/8). */

import type { Instance } from "../api/types";

/** Human error string from an unknown catch value, with a safe fallback.
 * Consolidates the repeated `(err as Error).message` cast used at catch
 * sites. */
export function errMsg(err: unknown): string {
  return (err as Error)?.message || "error";
}

export function fmtTokens(n: number | null | undefined): string {
  n = n || 0;
  if (n < 1000) return String(n);
  if (n < 1e6) return (n / 1e3).toFixed(n < 1e4 ? 1 : 0) + "k";
  return (n / 1e6).toFixed(1) + "M";
}

export function fmtUsd(u: number | null | undefined): string {
  u = u || 0;
  if (u <= 0) return "$0";
  if (u < 0.01) return "<$0.01";
  if (u < 10) return "$" + u.toFixed(2);
  if (u < 1000) return "$" + u.toFixed(1);
  return "$" + (u / 1e3).toFixed(1) + "k";
}

const PROV_LABELS: Record<string, string> = { claude: "Claude", codex: "Codex", aider: "Aider" };

/** Human label for a coding provider; capitalizes user-defined CLIs. */
export function provLabel(name: string | null | undefined): string {
  if (!name) return "";
  return PROV_LABELS[name] || name.charAt(0).toUpperCase() + name.slice(1);
}

/** "40s ago" / "6m ago" / "2h ago" / "3d ago" since `ts` (epoch seconds) —
 * THE relative-time formatter. `suffix` replaces " ago": "" for the bare "6m"
 * a status line wants ("working · 6m"), " old" for an item's age. `now` pins
 * the clock (epoch seconds; default the real one). A future `ts` reads 0s. */
export function relTime(ts: number, opts: { suffix?: string; now?: number } = {}): string {
  const suffix = opts.suffix ?? " ago";
  const secs = Math.max(0, Math.floor((opts.now ?? Date.now() / 1000) - ts));
  if (secs < 60) return secs + "s" + suffix;
  if (secs < 3600) return Math.floor(secs / 60) + "m" + suffix;
  if (secs < 86400) return Math.floor(secs / 3600) + "h" + suffix;
  return Math.floor(secs / 86400) + "d" + suffix;
}

/** Compact millisecond countdown ("2h 11m" / "~7m"), rounded to the nearest
 * minute (min 1). Port of _queueRelTime from 060-prompt-queue.js — shared by
 * the queue tab and the usage-window reset labels. Distinct from relTime
 * above, which formats an epoch-seconds "X ago". */
export function fmtDurationShort(ms: number): string {
  const mm = Math.max(1, Math.round(ms / 60000));
  return mm >= 60 ? Math.floor(mm / 60) + "h " + (mm % 60) + "m" : "~" + mm + "m";
}

/** For Shortcut/provisioned branches show just the descriptive slug
 * (feature/sc-19827/scan-sms-… -> scan-sms-…). */
export function displayBranch(inst: Pick<Instance, "branch" | "program">): string {
  const full = inst.branch || "";
  let m = full.match(/^feature\/sc-\d+\/(.+)$/);
  if (m) return m[1];
  m = full.match(/^mindflock\/(.+)$/);
  if (m) return m[1];
  return full || inst.program || "";
}

export function humanSize(bytes: number): string {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  if (bytes < 1024 * 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + " MB";
  return (bytes / (1024 * 1024 * 1024)).toFixed(2) + " GB";
}

export function pathBasename(p: string): string {
  const s = (p || "").replace(/[\\/]+$/, "");
  const i = Math.max(s.lastIndexOf("/"), s.lastIndexOf("\\"));
  return i >= 0 ? s.slice(i + 1) : s;
}

/** The first non-empty line of a text, trimmed ("" for none) — how a
 * session's create task reads on one line. */
export function firstLine(text: string | null | undefined): string {
  for (const l of String(text || "").split("\n")) {
    const t = l.trim();
    if (t) return t;
  }
  return "";
}
