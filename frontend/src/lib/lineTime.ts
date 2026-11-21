/** Time on a session's line (docs/flights.md "Round 15", the `looks` task):
 *
 *  - the **ruler** under the row of stops (SkyLine `.skl-ruler`): one segment
 *    per flown stop, proportional to how long it took — read from the
 *    flight's `history` as the span from each `started` event to the `done`,
 *    `failed` or `skipped` that ends it (a retried stop adds its rounds up);
 *    the total at the right end. Hidden at the far band.
 *  - **ageing**: a line whose row has been quiet (`activity_since`, epoch
 *    seconds) for more than a day is `old` (its title bar yellows like
 *    paper), for more than three days `stale` (and the bar says "3 days
 *    still"). */

import type { Flight, FlightHistoryEntry, StepState } from "./flights";

export interface LineSegment {
  /** The step's id and title. */
  step: string;
  title: string;
  /** How long it flew, in seconds (0 when the history never saw it start —
   * a stop the planner skipped). */
  seconds: number;
  state: Extract<StepState, "done" | "skipped" | "failed">;
}

const ENDS: ReadonlySet<FlightHistoryEntry["event"]> = new Set(["done", "failed", "skipped"]);

/** Seconds a step flew, from its own history entries in order: every
 * `started` → end pair added up. */
export function stepSeconds(hist: readonly FlightHistoryEntry[], stepId: string): number {
  let total = 0;
  let t0: number | null = null;
  for (const h of hist) {
    if (h.step !== stepId) continue;
    if (h.event === "started") t0 = h.at;
    else if (ENDS.has(h.event) && t0 !== null) {
      total += Math.max(0, h.at - t0);
      t0 = null;
    }
  }
  return Math.round(total);
}

/** The ruler's segments: the flight's stops that have flown, in line order. */
export function segmentsFor(flight: Pick<Flight, "steps" | "step_states" | "history"> | null | undefined): LineSegment[] {
  if (!flight) return [];
  const hist = flight.history || [];
  const out: LineSegment[] = [];
  for (const s of flight.steps || []) {
    const st = flight.step_states?.[s.id]?.state;
    if (st !== "done" && st !== "skipped" && st !== "failed") continue;
    out.push({ step: s.id, title: s.title, seconds: stepSeconds(hist, s.id), state: st });
  }
  return out;
}

/** All the segments together, in seconds. */
export function totalSeconds(segs: readonly LineSegment[]): number {
  return segs.reduce((n, s) => n + s.seconds, 0);
}

/** A duration in a dev's words: "40 s", "12 min", "1 h 05 min", "2 d 3 h". */
export function fmtDuration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return s + " s";
  const m = Math.round(s / 60);
  if (m < 60) return m + " min";
  const h = Math.floor(m / 60);
  if (h < 24) return h + " h" + (m % 60 ? " " + String(m % 60).padStart(2, "0") + " min" : "");
  const d = Math.floor(h / 24);
  return d + " d" + (h % 24 ? " " + (h % 24) + " h" : "");
}

/** A segment's hover title: "Checks · 12 min" (a failed one says so). */
export function segmentTitle(seg: LineSegment): string {
  return seg.title + " · " + fmtDuration(seg.seconds) + (seg.state === "failed" ? " · failed" : seg.state === "skipped" ? " · skipped" : "");
}

// --- Ageing -------------------------------------------------------------------

export type LineAge = "old" | "stale" | null;

export const OLD_AFTER_S = 24 * 3600;
export const STALE_AFTER_S = 72 * 3600;

/** How long a row has been quiet, from `activity_since` (epoch seconds; 0 or
 * missing = unknown, never old). `now` in ms. */
export function ageOf(activitySince: number | null | undefined, now = Date.now()): LineAge {
  const since = Number(activitySince) || 0;
  if (since <= 0) return null;
  const quiet = now / 1000 - since;
  if (quiet > STALE_AFTER_S) return "stale";
  if (quiet > OLD_AFTER_S) return "old";
  return null;
}

/** The bar's word for a stale line: "3 days still". */
export function ageWord(activitySince: number | null | undefined, now = Date.now()): string {
  if (ageOf(activitySince, now) !== "stale") return "";
  const days = Math.floor((now / 1000 - (Number(activitySince) || 0)) / 86400);
  return days + (days === 1 ? " day" : " days") + " still";
}

// --- The route's clock on the frame bar -----------------------------------

type ClockFlight = Pick<Flight, "state" | "steps" | "cursor" | "step_states" | "history" | "finished">;

/** When the route came to the state it is in now (epoch seconds; 0 when it
 * can't be told): a stop at work or waiting on you since its record's
 * `since`; halted since the failure that halted it; paused since the pause
 * (by you, or at a halt point); a finished route at `finished`. Ready and
 * stops-only routes have no clock. */
export function stateSince(f: ClockFlight | null | undefined): number {
  if (!f) return 0;
  const cur = f.steps?.[f.cursor];
  const rec = cur ? f.step_states?.[cur.id] : undefined;
  const hist = f.history || [];
  const lastOf = (events: readonly string[]): number => {
    for (let i = hist.length - 1; i >= 0; i--) {
      const h = hist[i];
      if (events.includes(h.event) && (!cur || !h.step || h.step === cur.id)) return Number(h.at) || 0;
    }
    return 0;
  };
  switch (f.state) {
    case "running":
    case "waiting_you":
      return Number(rec?.since) || 0;
    case "halted":
      return lastOf(["failed", "halted"]) || Number(rec?.since) || 0;
    case "paused":
      return lastOf(["paused", "halted_at"]);
    case "done":
    case "cancelled":
      return Number(f.finished) || 0;
    default:
      return 0;
  }
}

/** 2020-01-01: a `since` before it is a placeholder, not a time. */
const CLOCK_FLOOR_S = 1_577_836_800;

const CLOCK_TITLE: Partial<Record<Flight["state"], string>> = {
  running: "At this stop since ",
  waiting_you: "Waiting on you since ",
  halted: "Halted since ",
  paused: "Paused since ",
  done: "Finished at ",
  cancelled: "Stopped at ",
};

function clockAt(epochS: number, now: number): string {
  const d = new Date(epochS * 1000);
  const hm = String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
  // Not today: the day too.
  return new Date(now).toDateString() === d.toDateString() ? hm : d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + " " + hm;
}

/** How long the route has been where it is, for the frame bar after the
 * where-line: "for 12 min" (at a stop, waiting on you, halted, paused — the
 * minute it got there says nothing yet), "route finished 15 min ago" for a
 * route that landed; its `title` the clock time ("Waiting on you since
 * 14:02"). Null when there is nothing to say. `now` in ms. */
export function routeClock(f: ClockFlight | null | undefined, now = Date.now()): { text: string; title: string } | null {
  const since = stateSince(f);
  // No time, or one that can't be a real clock (an old record's placeholder):
  // nothing rather than "for 20000 d".
  if (!f || since < CLOCK_FLOOR_S || since * 1000 > now + 60_000) return null;
  const secs = Math.max(0, now / 1000 - since);
  const title = (CLOCK_TITLE[f.state] || "Since ") + clockAt(since, now);
  if (f.state === "done" || f.state === "cancelled") {
    const word = f.state === "done" ? "route finished " : "route stopped ";
    return { text: word + (secs < 60 ? "just now" : fmtDuration(secs) + " ago"), title };
  }
  if (secs < 60) return null;
  return { text: "for " + fmtDuration(secs), title };
}

/** The bar's one line: where the route is, then its clock ("at Verify · 2 of
 * 4 steps done · for 4 min"); either may be empty. */
export function withClock(where: string, clock: { text: string } | null | undefined): string {
  return [where, clock?.text || ""].filter(Boolean).join(" · ");
}
