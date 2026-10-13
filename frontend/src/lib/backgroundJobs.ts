/** Jobs an agent backgrounded and left running after its turn ended
 * (`Instance.background`, classified server-side by
 * backend/providers/claude_background.py). Claude Code calls a session busy
 * while ANY such job lives, so without this an idle session with a dead
 * polling loop read "Working" forever. The row's chip says which kind it is:
 * waiting (the job's completion will wake the agent) or stuck (it never will —
 * the chip stops it, via lib/stopBackgroundJob). Pure: no I/O. */

import type { BackgroundJob } from "../api/types";

export interface BackgroundChip {
  label: string;
  cls: string;
  title: string;
  /** The job the chip's click stops (stuck only). */
  stopPid?: number;
}

function minutes(s: number): string {
  const m = Math.round(s / 60);
  return m < 1 ? "<1m" : m < 90 ? m + "m" : (m / 60).toFixed(1) + "h";
}

function oneLine(cmd: string): string {
  const s = cmd.replace(/\s+/g, " ").trim();
  return s.length > 140 ? s.slice(0, 139) + "…" : s;
}

/** The jobs on a row, tolerating an older server (no field) or junk. */
export function backgroundJobs(inst: { background?: BackgroundJob[] | null }): BackgroundJob[] {
  return Array.isArray(inst.background) ? inst.background.filter((j) => j && typeof j.pid === "number") : [];
}

/** The rail chip for a row's background jobs, or null. Running jobs get none:
 * the session already reads Working, which is the truth. */
export function backgroundChip(inst: { background?: BackgroundJob[] | null; device?: string }): BackgroundChip | null {
  const jobs = backgroundJobs(inst);
  const stuck = jobs.filter((j) => j.state === "stuck");
  const waiting = jobs.filter((j) => j.state === "waiting");
  if (stuck.length) {
    const j = stuck[0];
    const base = "A background job the agent left running can't finish (" + j.reason + "): " + oneLine(j.command);
    // A row proxied from another device is stopped on that device.
    if (inst.device) return { label: stuck.length > 1 ? stuck.length + " jobs stuck" : "job stuck", cls: "bgchip bg-stuck", title: base };
    return {
      label: stuck.length > 1 ? stuck.length + " jobs stuck ✕" : "job stuck ✕",
      cls: "bgchip bg-stuck",
      title: base + " — click to stop it" + (stuck.length > 1 ? " (" + (stuck.length - 1) + " more after this one)" : ""),
      stopPid: j.pid,
    };
  }
  if (waiting.length) {
    const j = waiting[0];
    return {
      label: waiting.length > 1 ? waiting.length + " jobs waiting" : "job waiting",
      cls: "bgchip bg-waiting",
      title:
        "The turn ended, but a background job is still waiting (quiet " + minutes(j.quiet_s) + "): " +
        oneLine(j.command) + " — the agent resumes when it finishes",
    };
  }
  return null;
}

/** One sentence for the state chip's tooltip, or "" when no job matters. */
export function backgroundHint(inst: { background?: BackgroundJob[] | null }): string {
  const jobs = backgroundJobs(inst);
  const running = jobs.find((j) => j.state === "running");
  if (running) return "Turn ended; a background job is still running: " + oneLine(running.command);
  const chip = backgroundChip(inst);
  return chip ? chip.title.replace(/ — click to stop it.*$/, "") : "";
}
