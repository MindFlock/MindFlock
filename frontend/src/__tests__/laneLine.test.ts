/** Ship lanes on the rail, the parts beyond shipLine's own table (lanes.test):
 * a worker's line leads with its lane, and an "asks first" lane parked at its
 * held rung reads as waiting on you even outside a group. One lane phrase
 * (LANE_HEAD) feeds both. */

import { describe, it, expect } from "vitest";
import { awaitingApproval, laneLead, shipLine, workerLine } from "../lib/agentMessages";
import type { AutopilotRun } from "../api/types";

const NOW = 10_000;
const pr = { target: "pr", ask_first: false, owner: "s" };
const run = (over: Partial<AutopilotRun>): AutopilotRun => ({
  depth: "pr",
  state: "running",
  step: "",
  reason: "",
  source: "run",
  item: "",
  ...over,
});

describe("laneLead", () => {
  it("is the LANE_HEAD phrase, and nothing for no lane or leave", () => {
    expect(laneLead(pr)).toBe("→ PR");
    expect(laneLead(null)).toBe("");
    expect(laneLead({ target: "leave" })).toBe("");
  });
});

describe("asks first, outside a group", () => {
  const row = {
    title: "s",
    lane: { target: "commit", ask_first: true, owner: "s" },
    activity: "idle",
    autopilot: run({ state: "done", depth: "agent" }),
  };
  it("parked at the held rung reads as waiting on your OK", () => {
    expect(awaitingApproval(row)).toBe(true);
    const l = shipLine(row)!;
    expect(l.state).toBe("approve");
    expect(l.lead).toBe("→ commit, asks first");
    expect(l.cls).toBe("rep-ask");
  });
  it("raised to its lane and finished there is not waiting", () => {
    expect(awaitingApproval({ ...row, autopilot: run({ state: "done", depth: "commit" }) })).toBe(false);
    expect(awaitingApproval({ ...row, lane: { ...row.lane, ask_first: false } })).toBe(false);
  });
});

describe("workerLine leads with the lane", () => {
  it("prefixes a working or reported worker, not one waiting on you", () => {
    const base = { title: "w1", parent: "orch", lane: pr };
    expect(
      workerLine({ ...base, activity: "working", activity_since: NOW - 60 }, { nested: true, parentName: "orch", now: NOW }).text
    ).toBe("→ PR · working · 1m");
    expect(workerLine({ ...base, activity: "clarify" }, { nested: true, parentName: "orch" }).text).toBe("? needs your answer");
    expect(workerLine({ title: "w2", activity: "working" }, { nested: true, parentName: "orch" }).text).toBe("working");
  });
});
