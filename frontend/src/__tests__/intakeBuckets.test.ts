import { describe, it, expect } from "vitest";
import {
  NO_STATE_BUCKET,
  countInBuckets,
  shownTickets,
  visibleBuckets,
} from "../components/intake/buckets";

/** A ticket panel where a source ingests anyone's tickets, so the list is a mix
 * of your own work and the QA queue's. */
const tickets = [
  { bucket: "Ready for test", mine: true },
  { bucket: "Ready for test", mine: false },
  { bucket: "Ready for test", mine: false },
  { bucket: "Completed", mine: false },
  // No `mine` at all: a server that predates the field only ever returned your
  // own tickets, so this one is yours.
  { bucket: "In progress" },
  { mine: false },
];

const BUCKETS = ["In progress", "Ready for test", "Completed", NO_STATE_BUCKET];
const DONE = ["Completed"];

describe("visibleBuckets", () => {
  it("parks done-type buckets until something is chosen", () => {
    expect(visibleBuckets(BUCKETS, DONE, null)).toEqual([
      "In progress",
      "Ready for test",
      NO_STATE_BUCKET,
    ]);
  });

  it("takes an explicit choice literally, done buckets included", () => {
    expect(visibleBuckets(BUCKETS, DONE, ["Completed"])).toEqual(["Completed"]);
  });
});

describe("shownTickets", () => {
  const visible = visibleBuckets(BUCKETS, DONE, null);

  it("shows everything in the visible buckets when not narrowed", () => {
    // The Completed row is out on bucket alone, not on ownership.
    expect(shownTickets(tickets, visible, false)).toHaveLength(5);
  });

  it("drops other people's rows when narrowed to yours", () => {
    const rows = shownTickets(tickets, visible, true);
    expect(rows).toHaveLength(2);
    expect(rows.every((t) => t.mine !== false)).toBe(true);
  });

  it("counts exactly what it shows", () => {
    // The badge and the list must agree — a badge reading 1221 over a list of
    // 52 is what this module was written for.
    for (const mineOnly of [false, true]) {
      expect(countInBuckets(tickets, visible, mineOnly)).toBe(
        shownTickets(tickets, visible, mineOnly).length
      );
    }
  });

  it("defaults to counting everyone's", () => {
    expect(countInBuckets(tickets, visible)).toBe(5);
  });
});
