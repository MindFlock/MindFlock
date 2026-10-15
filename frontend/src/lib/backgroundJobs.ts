/** Jobs an agent backgrounded and left running after its turn ended
 * (`Instance.background`, classified server-side by
 * backend/providers/claude_background.py). Claude Code calls a session busy
 * while ANY such job lives, so without this an idle session with a dead
 * polling loop read "Working" forever. The row's chip says which kind it is:
 * waiting (the job's completion will wake the agent — "waiting on job · 2h
 * 10m of ~6h" when it has an expected duration, "job past ~6h" once it is over
 * it) or stuck (it never will). Its words come from lib/jobExpectation; a
 * click opens the chip's menu: stop it (stuck, via lib/stopBackgroundJob) and
 * say how long it should take. Pure: no I/O. */

import type { BackgroundJob } from "../api/types";
import { jobChipText } from "./jobExpectation";

export interface BackgroundChip {
  label: string;
  cls: string;
  title: string;
  /** The job the chip's menu acts on — on this device's rows only (a row
   * proxied from another device is handled on that device). */
  job?: BackgroundJob;
  /** The job the menu's Stop stops (stuck only). */
  stopPid?: number;
}

/** The jobs on a row, tolerating an older server (no field) or junk. */
export function backgroundJobs(inst: { background?: BackgroundJob[] | null }): BackgroundJob[] {
  return Array.isArray(inst.background) ? inst.background.filter((j) => j && typeof j.pid === "number") : [];
}

const nowS = () => Date.now() / 1000;

/** The rail chip for a row's background jobs, or null (`now` in epoch
 * seconds). Running jobs get none: the session already reads Working, which
 * is the truth. Stuck outranks a job past its expectation outranks waiting. */
export function backgroundChip(
  inst: { background?: BackgroundJob[] | null; device?: string },
  now: number = nowS()
): BackgroundChip | null {
  const jobs = backgroundJobs(inst);
  const local = !inst.device;
  const stuck = jobs.filter((j) => j.state === "stuck");
  if (stuck.length) {
    const j = stuck[0];
    const t = jobChipText(j, now);
    // A row proxied from another device is stopped on that device.
    if (!local) return { label: stuck.length > 1 ? stuck.length + " jobs stuck" : "job stuck", cls: t.cls, title: t.title };
    return {
      label: stuck.length > 1 ? stuck.length + " jobs stuck ✕" : t.label,
      cls: t.cls,
      title:
        t.title + " — click to stop it or say how long it should take" +
        (stuck.length > 1 ? " (" + (stuck.length - 1) + " more after this one)" : ""),
      stopPid: j.pid,
      job: j,
    };
  }
  const waiting = jobs.filter((j) => j.state === "waiting");
  if (waiting.length) {
    const texts = waiting.map((j) => ({ j, t: jobChipText(j, now) }));
    const { j, t } = texts.find((x) => x.t.kind === "past") || texts[0];
    return {
      label: waiting.length > 1 ? waiting.length + " jobs waiting" : t.label,
      cls: t.cls,
      title: t.title + (local ? " — click to say how long it should take" : ""),
      ...(local ? { job: j } : {}),
    };
  }
  return null;
}

/** One sentence for the state chip's tooltip (the rail's and the pane
 * header's), or "" when no job matters. */
export function backgroundHint(inst: { background?: BackgroundJob[] | null }, now: number = nowS()): string {
  const jobs = backgroundJobs(inst);
  const running = jobs.find((j) => j.state === "running");
  if (running) return jobChipText(running, now).title;
  const chip = backgroundChip(inst, now);
  return chip ? chip.title.replace(/ — click to .*$/, "") : "";
}
