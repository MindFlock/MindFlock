/** Round 15 looks — time on a line (lib/lineTime): a flown stop's duration
 * from its `started` → `done` / `failed` / `skipped` history events (a retry
 * adds its rounds up, a planner-skipped stop that never started is 0), the
 * ruler's segments in line order for flown stops only, the total, the words
 * ("Checks · 12 min"), and ageing: old past a day, stale past three with a
 * "3 days still" word. */

import { describe, expect, it } from "vitest";
import type { Flight, FlightHistoryEntry } from "../lib/flights";
import { OLD_AFTER_S, STALE_AFTER_S, ageOf, ageWord, fmtDuration, segmentTitle, segmentsFor, stepSeconds, totalSeconds } from "../lib/lineTime";

const h = (at: number, step: string, event: FlightHistoryEntry["event"], note = ""): FlightHistoryEntry => ({ at, step, event, note });

const FLIGHT = {
  steps: [
    { id: "s1", kind: "agent", title: "Agent works", config: {}, on_fail: "halt" },
    { id: "s2", kind: "checks", title: "Checks", config: {}, on_fail: "halt" },
    { id: "s3", kind: "test", title: "Tests", config: {}, on_fail: "halt" },
    { id: "s4", kind: "ship", title: "Push", config: {}, on_fail: "halt" },
    { id: "s5", kind: "you", title: "Approve", config: {}, on_fail: "halt" },
  ],
  step_states: {
    s1: { state: "done", since: 100, note: "", detail: {}, rounds: 0 },
    s2: { state: "done", since: 820, note: "", detail: {}, rounds: 0 },
    s3: { state: "skipped", since: 821, note: "skipped: weekend", detail: {}, rounds: 0 },
    s4: { state: "failed", since: 900, note: "", detail: {}, rounds: 1 },
    s5: { state: "running", since: 900, note: "", detail: {}, rounds: 0 },
  },
  history: [
    h(0, "s1", "started"),
    h(100, "s1", "done"),
    h(100, "s2", "started"),
    h(820, "s2", "done", "all green"),
    h(821, "s3", "skipped", "skipped: weekend"),
    h(821, "s4", "started"),
    h(850, "s4", "failed"),
    h(860, "s4", "started"),
    h(900, "s4", "failed"),
    h(900, "s5", "started"),
  ],
} as unknown as Flight;

describe("stepSeconds", () => {
  it("is started → end, every round added up; never started = 0", () => {
    expect(stepSeconds(FLIGHT.history, "s1")).toBe(100);
    expect(stepSeconds(FLIGHT.history, "s2")).toBe(720);
    expect(stepSeconds(FLIGHT.history, "s3")).toBe(0);
    expect(stepSeconds(FLIGHT.history, "s4")).toBe(29 + 40);
    // Still running: its open round does not count yet.
    expect(stepSeconds(FLIGHT.history, "s5")).toBe(0);
    expect(stepSeconds([], "s1")).toBe(0);
  });
});

describe("segmentsFor", () => {
  it("one segment per flown stop, in line order, with its state", () => {
    const segs = segmentsFor(FLIGHT);
    expect(segs).toEqual([
      { step: "s1", title: "Agent works", seconds: 100, state: "done" },
      { step: "s2", title: "Checks", seconds: 720, state: "done" },
      { step: "s3", title: "Tests", seconds: 0, state: "skipped" },
      { step: "s4", title: "Push", seconds: 69, state: "failed" },
    ]);
    expect(totalSeconds(segs)).toBe(889);
    expect(segmentsFor(null)).toEqual([]);
    expect(segmentsFor({ steps: FLIGHT.steps, step_states: {}, history: [] } as unknown as Flight)).toEqual([]);
  });

  it("the hover title names the stop and its time", () => {
    const [agent, checks, tests, push] = segmentsFor(FLIGHT);
    expect(segmentTitle(checks)).toBe("Checks · 12 min");
    expect(segmentTitle(agent)).toBe("Agent works · 2 min");
    expect(segmentTitle(tests)).toBe("Tests · 0 s · skipped");
    expect(segmentTitle(push)).toBe("Push · 1 min · failed");
  });
});

describe("fmtDuration", () => {
  it("speaks in seconds, minutes, hours and days", () => {
    expect(fmtDuration(0)).toBe("0 s");
    expect(fmtDuration(42)).toBe("42 s");
    expect(fmtDuration(720)).toBe("12 min");
    expect(fmtDuration(3600)).toBe("1 h");
    expect(fmtDuration(3900)).toBe("1 h 05 min");
    expect(fmtDuration(26 * 3600)).toBe("1 d 2 h");
    expect(fmtDuration(48 * 3600)).toBe("2 d");
  });
});

describe("ageing", () => {
  const now = 1_800_000_000_000; // ms
  const ago = (s: number) => now / 1000 - s;

  it("old past a day, stale past three; unknown never ages", () => {
    expect(ageOf(ago(3600), now)).toBeNull();
    expect(ageOf(ago(OLD_AFTER_S - 1), now)).toBeNull();
    expect(ageOf(ago(OLD_AFTER_S + 1), now)).toBe("old");
    expect(ageOf(ago(STALE_AFTER_S - 1), now)).toBe("old");
    expect(ageOf(ago(STALE_AFTER_S + 1), now)).toBe("stale");
    expect(ageOf(0, now)).toBeNull();
    expect(ageOf(undefined, now)).toBeNull();
    expect(ageOf(null, now)).toBeNull();
  });

  it("the stale word counts the days: \"3 days still\"", () => {
    expect(ageWord(ago(3 * 86400 + 7200), now)).toBe("3 days still");
    expect(ageWord(ago(9 * 86400), now)).toBe("9 days still");
    // Old but not stale, or fresh: no word.
    expect(ageWord(ago(2 * 86400), now)).toBe("");
    expect(ageWord(ago(60), now)).toBe("");
  });
});
