import { describe, expect, it } from "vitest";
import { formatDue, fromDue, isOverdue, parseWhen, snoozeChoices, toDue } from "../lib/todoTime";

// Monday 2026-11-09, 14:30 local.
const NOW = new Date(2026, 10, 9, 14, 30);

describe("parseWhen", () => {
  it.each([
    ["Call the bank tue 9am", "Call the bank", "2026-11-10T09:00"],
    ["Call the bank on Tuesday at 9:30 am", "Call the bank", "2026-11-10T09:30"],
    ["Send the report tomorrow", "Send the report", "2026-11-10T09:00"],
    ["Send the report 3pm friday", "Send the report", "2026-11-13T15:00"],
    ["Stretch in 20 min", "Stretch", "2026-11-09T14:50"],
    ["Ship it in 2 hours", "Ship it", "2026-11-09T16:30"],
    ["Pay rent in 3 days", "Pay rent", "2026-11-12T14:30"],
    ["Standup at 4pm", "Standup", "2026-11-09T16:00"],
    ["Standup at 9am", "Standup", "2026-11-10T09:00"],
    ["Movie tonight", "Movie", "2026-11-09T20:00"],
    ["Call mom at 3", "Call mom", "2026-11-09T15:00"],
    ["Plan the week monday 5pm", "Plan the week", "2026-11-09T17:00"],
    ["Plan the week monday 9am", "Plan the week", "2026-11-16T09:00"],
  ])("%s", (input, text, due) => {
    expect(parseWhen(input, NOW)).toEqual({ text, due });
  });

  it.each(["Buy milk", "tuesday", "Read chapter 9", "Fix the 3pm bug in the parser module now", ""])(
    "no time in %j",
    (input) => {
      expect(parseWhen(input, NOW)).toBeNull();
    },
  );
});

describe("formatDue", () => {
  it.each([
    ["2026-11-09T16:00", "Today 4:00 PM"],
    ["2026-11-10T09:00", "Tomorrow 9:00 AM"],
    ["2026-11-12T12:05", "Thu 12:05 PM"],
    ["2026-11-08T09:00", "Yesterday 9:00 AM"],
    ["2026-11-30T09:00", "Nov 30 9:00 AM"],
    ["2027-01-04T09:00", "Jan 4, 2027 9:00 AM"],
  ])("%s → %s", (due, want) => {
    expect(formatDue(due, NOW)).toBe(want);
  });

  it("is empty for no or a bad due", () => {
    expect(formatDue("", NOW)).toBe("");
    expect(formatDue("next tuesday", NOW)).toBe("");
  });
});

describe("due strings", () => {
  it("round-trip", () => {
    expect(toDue(fromDue("2026-11-10T09:05")!)).toBe("2026-11-10T09:05");
  });
  it("overdue at the minute or after", () => {
    expect(isOverdue("2026-11-09T14:30", NOW)).toBe(true);
    expect(isOverdue("2026-11-09T14:31", NOW)).toBe(false);
    expect(isOverdue("", NOW)).toBe(false);
  });
  it("offers three snoozes", () => {
    expect(snoozeChoices(NOW).map((c) => c.until)).toEqual(["2026-11-09T14:40", "2026-11-09T15:30", "2026-11-10T09:00"]);
  });
});
