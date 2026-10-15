/** What a background job's chip says once it can carry an expected duration
 * (round-6 contract: JOB-EXPECTATIONS). A long job — a 6-hour training run —
 * is not "stuck" just because it is quiet: the server only calls it stuck
 * once it is far past what it was expected to take, or quiet longer than it
 * has ever been before resuming. These words say which of those it is.
 *
 * Pure: no I/O, the clock is passed in (epoch SECONDS, like the job's own
 * fields). The rail chip (lib/backgroundJobs), the pane's state-chip tooltip
 * (the same helper, through backgroundHint), the bell and the toast all read
 * from here. */

import type { BackgroundJob } from "../api/types";

const MIN = 60;
const HOUR = 3600;
const DAY = 86400;
/** What the server accepts for an expectation: 1 minute … 14 days. */
export const EXPECT_MIN_S = MIN;
export const EXPECT_MAX_S = 14 * DAY;

/** The menu's ready-made answers to "Expected to take…". */
export const EXPECT_PRESETS: ReadonlyArray<{ label: string; secs: number }> = [
  { label: "30 min", secs: 30 * MIN },
  { label: "1 h", secs: HOUR },
  { label: "6 h", secs: 6 * HOUR },
  { label: "24 h", secs: DAY },
];

/** A span for people: "<1m", "45m", "2h 10m", "6h", "2d 3h". */
export function formatSpan(secs: number): string {
  const s = Math.max(0, Number(secs) || 0);
  const m = Math.round(s / MIN);
  if (m < 1) return "<1m";
  if (m < 60) return m + "m";
  if (s < 2 * DAY) {
    const h = Math.floor(m / 60);
    const mm = m % 60;
    return mm ? h + "h " + mm + "m" : h + "h";
  }
  const d = Math.floor(s / DAY);
  const h = Math.round((s - d * DAY) / HOUR);
  return h ? d + "d " + h + "h" : d + "d";
}

const UNIT: Record<string, number> = { s: 1, m: MIN, h: HOUR, d: DAY };

/** "6h", "90m", "1h30m", "1h 30m", "2d", "90s", or a bare number of seconds →
 * seconds; null for anything else or outside 1 minute … 14 days (the same
 * rules the server applies). */
export function parseDuration(input: string | number): number | null {
  let secs: number;
  if (typeof input === "number") secs = input;
  else {
    const t = String(input || "").trim().toLowerCase().replace(/\s+/g, "");
    if (!t) return null;
    if (/^\d+(\.\d+)?$/.test(t)) secs = parseFloat(t);
    else {
      // Each unit at most once, largest first: "1h30m" yes, "30m1h" no.
      const m = /^(?:(\d+(?:\.\d+)?)d)?(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m)?(?:(\d+(?:\.\d+)?)s)?$/.exec(t);
      if (!m || !(m[1] || m[2] || m[3] || m[4])) return null;
      secs = ["d", "h", "m", "s"].reduce((sum, u, i) => sum + (m[i + 1] ? parseFloat(m[i + 1]) * UNIT[u] : 0), 0);
    }
  }
  if (!Number.isFinite(secs)) return null;
  secs = Math.round(secs);
  return secs >= EXPECT_MIN_S && secs <= EXPECT_MAX_S ? secs : null;
}

/** "18:40", or "Thu 18:40" once it isn't today. */
export function etaClock(eta: number, now: number): string {
  const a = new Date(eta * 1000);
  const b = new Date(now * 1000);
  const sameDay = a.toDateString() === b.toDateString();
  // lib/sessionState's fmtClock, inlined: sessionState imports this module (via backgroundJobs).
  const clock = String(a.getHours()).padStart(2, "0") + ":" + String(a.getMinutes()).padStart(2, "0");
  return (sameDay ? "" : a.toLocaleDateString(undefined, { weekday: "short" }) + " ") + clock;
}

/** The job's expected duration, when one is set. */
export function expectOf(job: Pick<BackgroundJob, "expect_s">): number | null {
  const e = Number(job.expect_s);
  return e > 0 ? e : null;
}

const BY: Record<string, string> = { you: "you", agent: "the agent", command: "its command" };

/** "~6h, set by you" — the expectation and who said so. */
function expectWords(expect: number, by?: string | null): string {
  return "~" + formatSpan(expect) + (by && BY[by] ? ", set by " + BY[by] : "");
}

function oneLine(cmd: string): string {
  const s = String(cmd || "").replace(/\s+/g, " ").trim();
  return s.length > 140 ? s.slice(0, 139) + "…" : s;
}

export interface JobChipText {
  label: string;
  cls: string;
  title: string;
  /** The tone the label stands for: running / waiting / past / stuck. */
  kind: "running" | "waiting" | "past" | "stuck";
}

/** One job, said as the rail chip says it (`now` in epoch seconds):
 *  - stuck → "job stuck ✕", the tooltip its reason;
 *  - past its expectation, not yet stuck → "job past ~6h" (amber dot);
 *  - waiting inside its expectation → "waiting on job · 2h 10m of ~6h"
 *    (the agent's turn is over: it is the job that runs, not the session),
 *    the tooltip the ETA as a clock time;
 *  - waiting otherwise → "job waiting", the tooltip the reason when it is
 *    judged by its own rhythm (it has come back from long quiets before);
 *  - running → "job running" (the rail draws no chip for it: the row reads
 *    Working; the pane's tooltip uses the title).
 * Titles carry the command. */
export function jobChipText(job: BackgroundJob, now: number): JobChipText {
  const cmd = oneLine(job.command);
  const started = Number(job.started) || 0;
  const elapsed = started > 0 ? Math.max(0, now - started) : 0;
  const expect = expectOf(job);
  const eta = Number(job.eta) || (expect && started > 0 ? started + expect : 0);
  const past = !!expect && (job.past_expected ?? (started > 0 && elapsed > expect));
  const quiet = Number(job.quiet_s) || 0;
  const progress = expect && started > 0 ? formatSpan(elapsed) + " of ~" + formatSpan(expect) : "";

  if (job.state === "stuck")
    return {
      kind: "stuck",
      label: "job stuck ✕",
      cls: "bgchip bg-stuck",
      title: "Background job stuck (" + (job.reason || "no CPU or output for " + formatSpan(quiet)) + "): " + cmd,
    };
  if (job.state === "running")
    return {
      kind: "running",
      label: "job running" + (progress && !past ? " · " + progress : ""),
      cls: "bgchip bg-running",
      title:
        "Turn ended; a background job is still running" +
        (expect ? " (" + (past ? "past its expected " + expectWords(expect, job.expected_by) : progress + (eta ? ", done about " + etaClock(eta, now) : "")) + ")" : "") +
        ": " + cmd,
    };
  if (past && expect)
    return {
      kind: "past",
      label: "job past ~" + formatSpan(expect),
      cls: "bgchip bg-waiting bg-past",
      title:
        "Past its expected " + expectWords(expect, job.expected_by) + " — " + formatSpan(elapsed) + " in, quiet " +
        formatSpan(quiet) +
        (Number(job.threshold_s) > quiet ? "; called stuck after " + formatSpan(Number(job.threshold_s)) + " quiet" : "") +
        ": " + cmd,
    };
  if (expect && progress)
    return {
      kind: "waiting",
      label: "waiting on job · " + progress,
      cls: "bgchip bg-waiting",
      title:
        "Expected to take " + expectWords(expect, job.expected_by) + (eta ? " — done about " + etaClock(eta, now) : "") +
        "; quiet " + formatSpan(quiet) + ": " + cmd,
    };
  const rhythm = Number(job.longest_gap_s) > 0 && job.reason ? job.reason : "quiet " + formatSpan(quiet);
  return {
    kind: "waiting",
    label: "job waiting",
    cls: "bgchip bg-waiting",
    title: "The turn ended, but a background job is still waiting (" + rhythm + "): " + cmd + " — the agent resumes when it finishes",
  };
}

/** The toast for `session.job_past_expected`; `name` is the session's name. */
export function jobPastExpectedNote(name: string, expectS: number): string {
  return "Job on " + name + " is past its expected ~" + formatSpan(expectS) + " — still quiet";
}

/** The bell row for the same event (the row is already named by its session). */
export function jobPastExpectedRow(expectS: number): string {
  return "job is past its expected ~" + formatSpan(expectS) + " — still quiet";
}
