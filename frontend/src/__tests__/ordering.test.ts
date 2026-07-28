import { describe, it, expect } from "vitest";
import type { Instance } from "../api/types";
import { orderedInstances, matchesFilter, attentionItems } from "../components/sidebar/ordering";

const inst = (o: Partial<Instance>): Instance => o as unknown as Instance;

describe("orderedInstances", () => {
  it("puts saved-order rows first, then unlisted rows in server order", () => {
    const { rows, nextOrder } = orderedInstances(
      [inst({ title: "a" }), inst({ title: "b" }), inst({ title: "c" })],
      ["c", "a"]
    );
    expect(rows.map((i) => i.title)).toEqual(["c", "a", "b"]);
    expect(nextOrder).toEqual(["c", "a", "b"]);
  });

  it("ignores saved titles no longer present", () => {
    const { rows } = orderedInstances([inst({ title: "a" })], ["gone", "a"]);
    expect(rows.map((i) => i.title)).toEqual(["a"]);
  });

  it("does not rewrite the saved order for a transient empty list", () => {
    const { rows, nextOrder } = orderedInstances([], ["x", "y"]);
    expect(rows).toEqual([]);
    expect(nextOrder).toEqual(["x", "y"]);
  });
});

describe("matchesFilter", () => {
  const row = inst({ title: "Scanner", branch: "feature/sc-1/scan-sms" });
  it("matches title, alias, and branch (lowercased filter)", () => {
    expect(matchesFilter(row, "", {})).toBe(true);
    expect(matchesFilter(row, "scan", {})).toBe(true);
    expect(matchesFilter(row, "sms", {})).toBe(true);
    expect(matchesFilter(row, "myalias", { Scanner: "myalias" })).toBe(true);
  });
  it("does not match repo/path or unrelated text", () => {
    expect(matchesFilter(row, "nope", {})).toBe(false);
  });
});

describe("attentionItems", () => {
  it("classifies each state at the right priority and sorts by p then title", () => {
    const items = attentionItems([
      inst({ title: "pushed1", activity: "idle", stage: "pushed" }),
      inst({ title: "clar1", activity: "clarify", last_turn: "help?" }),
      inst({ title: "checkfail1", activity: "idle", check: { state: "failed" } }),
      inst({ title: "broken1", activity: "idle", stage: "interrupt", failed_step: "lint" }),
    ]);
    expect(items.map((i) => [i.p, i.title, i.reason])).toEqual([
      [0, "clar1", "needs your answer"],
      [1, "broken1", "pre-commit failed at lint"],
      [2, "checkfail1", "checks failing"],
      [3, "pushed1", "pushed — ready for PR"],
    ]);
    expect(items[0].snippet).toBe("help?");
  });

  it("skips paused and missing sessions", () => {
    const items = attentionItems([
      inst({ title: "p", status: "paused", activity: "clarify" }),
      inst({ title: "m", workspace_missing: true, activity: "clarify" }),
    ]);
    expect(items).toEqual([]);
  });

  it("flags a calm-but-wedged session sitting on unfinished work", () => {
    const items = attentionItems([
      inst({
        title: "wedged",
        activity: "idle",
        stage: "committed",
        activity_since: Date.now() / 1000 - 2000,
      }),
    ]);
    expect(items).toHaveLength(1);
    expect(items[0].p).toBe(1);
    expect(items[0].reason).toContain("possibly stuck");
  });

  it("does not flag a recently-active idle session", () => {
    const items = attentionItems([
      inst({
        title: "fresh",
        activity: "idle",
        stage: "committed",
        activity_since: Date.now() / 1000 - 10,
      }),
    ]);
    expect(items).toEqual([]);
  });
});
