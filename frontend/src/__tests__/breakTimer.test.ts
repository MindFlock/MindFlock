import { describe, it, expect } from "vitest";
import {
  armBreak,
  BREAK_DEFAULT_MINUTES,
  BREAK_MAX_MINUTES,
  BREAK_MIN_MINUTES,
  clampBreakMinutes,
  clampIdleMinutes,
  fmtElapsed,
  IDLE_DEFAULT_MINUTES,
  IDLE_MAX_MINUTES,
  IDLE_MIN_MINUTES,
  nextArm,
  SNOOZE_MS,
  snoozeArm,
} from "../lib/breakTimer";

const T = 1_700_000_000_000; // a fixed "now"
const MIN = 60_000;

describe("clampIdleMinutes", () => {
  it("defaults to ten minutes, not to its floor", () => {
    // The idle flock's floor is one minute, so "unreadable" and "as small as
    // allowed" are further apart here than they are for break reminders —
    // reading a cleared field as 1 would put birds on screen every minute.
    expect(clampIdleMinutes("")).toBe(IDLE_DEFAULT_MINUTES);
    expect(clampIdleMinutes("abc")).toBe(IDLE_DEFAULT_MINUTES);
    expect(clampIdleMinutes("   ")).toBe(IDLE_DEFAULT_MINUTES);
    expect(clampIdleMinutes(null)).toBe(IDLE_DEFAULT_MINUTES);
    expect(clampIdleMinutes(undefined)).toBe(IDLE_DEFAULT_MINUTES);
    expect(IDLE_DEFAULT_MINUTES).toBe(10);
  });

  it("keeps a sane whole number and clamps the rest", () => {
    expect(clampIdleMinutes(3)).toBe(3);
    expect(clampIdleMinutes("25")).toBe(25);
    expect(clampIdleMinutes(9.6)).toBe(10);
    expect(clampIdleMinutes(0)).toBe(IDLE_MIN_MINUTES);
    expect(clampIdleMinutes(-5)).toBe(IDLE_MIN_MINUTES);
    expect(clampIdleMinutes(99999)).toBe(IDLE_MAX_MINUTES);
  });
});

describe("clampBreakMinutes", () => {
  it("keeps a sane whole number of minutes", () => {
    expect(clampBreakMinutes(45)).toBe(45);
    expect(clampBreakMinutes("90")).toBe(90);
    expect(clampBreakMinutes(45.4)).toBe(45);
  });

  it("clamps to the supported range instead of arming a useless timer", () => {
    expect(clampBreakMinutes(1)).toBe(BREAK_MIN_MINUTES);
    expect(clampBreakMinutes(0)).toBe(BREAK_MIN_MINUTES);
    expect(clampBreakMinutes(-30)).toBe(BREAK_MIN_MINUTES);
    expect(clampBreakMinutes(99999)).toBe(BREAK_MAX_MINUTES);
  });

  it("falls back to the default for anything unreadable", () => {
    expect(clampBreakMinutes("")).toBe(BREAK_DEFAULT_MINUTES);
    expect(clampBreakMinutes("abc")).toBe(BREAK_DEFAULT_MINUTES);
    // Number() reads all three of these as 0; a cleared field must not become
    // a five-minute reminder.
    expect(clampBreakMinutes("   ")).toBe(BREAK_DEFAULT_MINUTES);
    expect(clampBreakMinutes(null)).toBe(BREAK_DEFAULT_MINUTES);
    expect(clampBreakMinutes(undefined)).toBe(BREAK_DEFAULT_MINUTES);
  });
});

describe("armBreak / snoozeArm", () => {
  it("arms one whole interval out and records the interval it used", () => {
    expect(armBreak(T, 60)).toEqual({ at: T + 60 * MIN, every: 60 });
  });

  it("arms off the clamped interval, never the raw one", () => {
    expect(armBreak(T, 1)).toEqual({ at: T + BREAK_MIN_MINUTES * MIN, every: BREAK_MIN_MINUTES });
  });

  it("snoozes five minutes without changing the armed interval", () => {
    expect(snoozeArm(T, 60)).toEqual({ at: T + SNOOZE_MS, every: 60 });
  });
});

describe("nextArm", () => {
  it("arms fresh when nothing was saved", () => {
    expect(nextArm(T, 60, null)).toEqual({ at: T + 60 * MIN, every: 60 });
  });

  it("keeps a saved deadline across a reload — a refresh must not dodge a break", () => {
    const saved = { at: T + 4 * MIN, every: 60 };
    expect(nextArm(T, 60, saved)).toEqual(saved);
  });

  it("keeps an OVERDUE deadline: the break you were owed is still owed", () => {
    const saved = { at: T - 10 * MIN, every: 60 };
    expect(nextArm(T, 60, saved)).toEqual(saved);
  });

  it("keeps a snooze across a reload", () => {
    const saved = snoozeArm(T, 60);
    expect(nextArm(T + 1000, 60, saved)).toEqual(saved);
  });

  it("re-arms when the interval changed — a deadline for 90 means nothing at 30", () => {
    const saved = { at: T + 80 * MIN, every: 90 };
    expect(nextArm(T, 30, saved)).toEqual({ at: T + 30 * MIN, every: 30 });
  });

  it("re-arms a deadline further out than one whole interval (clock skew)", () => {
    const saved = { at: T + 200 * MIN, every: 60 };
    expect(nextArm(T, 60, saved)).toEqual({ at: T + 60 * MIN, every: 60 });
  });

  it("re-arms on a corrupt saved record instead of never firing again", () => {
    expect(nextArm(T, 60, { at: NaN, every: 60 })).toEqual({ at: T + 60 * MIN, every: 60 });
    expect(nextArm(T, 60, { at: Infinity, every: 60 })).toEqual({ at: T + 60 * MIN, every: 60 });
  });

  it("compares against the clamped interval, so an out-of-range setting is stable", () => {
    // Saved under the floor the store clamps to; asking with the raw 1 must
    // not re-arm on every single mount.
    const saved = { at: T + 2 * MIN, every: BREAK_MIN_MINUTES };
    expect(nextArm(T, 1, saved)).toEqual(saved);
  });
});

describe("fmtElapsed", () => {
  it("reads as m:ss", () => {
    expect(fmtElapsed(0)).toBe("0:00");
    expect(fmtElapsed(9_000)).toBe("0:09");
    expect(fmtElapsed(65_000)).toBe("1:05");
    expect(fmtElapsed(90 * MIN)).toBe("90:00");
  });

  it("never shows negative time", () => {
    expect(fmtElapsed(-5000)).toBe("0:00");
  });
});
