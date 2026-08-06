/** The client half of the autopilot ladder — pure, so it is tested directly.
 * Mirrors tests/unit/test_autopilot.py; the two must agree on the rungs, on
 * which stage satisfies which target, and on merge never being satisfied by an
 * observed stage. */

import { describe, expect, it } from "vitest";
import {
  DEPTHS,
  DEPTH_ORDER,
  DEPTH_LABELS,
  DEPTH_STEP_LABELS,
  SESSION_DEPTHS,
  SOURCE_DEPTHS,
  atOrPastDepth,
  autopilotChipLabel,
  autopilotChipTitle,
  depthLabel,
  liveRun,
  normalizeDepth,
} from "../lib/autopilot";
import { fastTrackStep, nextStep } from "../lib/stage";
import type { AutopilotRun } from "../api/types";

const run = (over: Partial<AutopilotRun> = {}): AutopilotRun => ({
  depth: "pr",
  state: "running",
  step: "",
  reason: "",
  source: "session",
  item: "",
  ...over,
});

describe("the ladder", () => {
  it("matches the server's rung order", () => {
    expect(DEPTH_ORDER).toEqual(["off", "agent", "commit", "push", "pr", "merge"]);
    expect(DEPTHS).toEqual(["agent", "commit", "push", "pr", "merge"]);
  });

  it("omits the intake-only agent rung from the session button", () => {
    // Arming "agent" on a session that already exists would mean "do nothing".
    expect(SESSION_DEPTHS).not.toContain("agent");
    expect(SESSION_DEPTHS).toContain("merge");
  });

  it("omits merge from what a whole source may default to", () => {
    // A source default applies to every future item with no human in the loop.
    expect(SOURCE_DEPTHS).not.toContain("merge");
    expect(SOURCE_DEPTHS).toContain("pr");
  });

  it("labels every rung in both vocabularies", () => {
    for (const d of DEPTH_ORDER) {
      expect(DEPTH_LABELS[d]).toBeTruthy();
      expect(DEPTH_STEP_LABELS[d]).toBeTruthy();
    }
  });

  it("normalizes junk to empty rather than guessing", () => {
    expect(normalizeDepth("PR")).toBe("pr");
    expect(normalizeDepth("  merge ")).toBe("merge");
    expect(normalizeDepth("off")).toBe("off");
    expect(normalizeDepth("nonsense")).toBe("");
    expect(normalizeDepth(null)).toBe("");
    expect(normalizeDepth(undefined)).toBe("");
  });

  it("falls back to the raw value for an unknown label", () => {
    expect(depthLabel("pr")).toBe("Open PR");
    expect(depthLabel("")).toBe("Off");
  });
});

describe("atOrPastDepth", () => {
  it.each([
    ["committed", "commit", true],
    ["committed", "push", false],
    ["pushed", "push", true],
    ["pushed", "commit", true],
    ["pushed", "pr", false],
    ["pr", "pr", true],
    ["agent", "commit", false],
    ["interrupt", "commit", false],
    ["precommit", "commit", false],
    ["provisioning", "commit", false],
  ])("%s vs %s -> %s", (stage, depth, expected) => {
    expect(atOrPastDepth(stage, depth)).toBe(expected);
  });

  it("is never satisfied for merge", () => {
    // The server has no "merged" stage — a merged PR moves the stage OFF "pr" —
    // so that rung completes when the merge call returns ok, not by observation.
    for (const stage of ["committed", "pushed", "pr", "merged"])
      expect(atOrPastDepth(stage, "merge")).toBe(false);
  });

  it("is false for off and for junk", () => {
    expect(atOrPastDepth("pushed", "off")).toBe(false);
    expect(atOrPastDepth("pushed", "")).toBe(false);
    expect(atOrPastDepth("pushed", "nonsense")).toBe(false);
  });
});

describe("chip text", () => {
  it("names the target while running", () => {
    expect(autopilotChipLabel(run({ depth: "pr" }))).toBe("auto → Open PR");
    expect(autopilotChipTitle(run({ depth: "pr" }))).toContain("Open PR");
  });

  it("always explains a halt", () => {
    // A chain that stops without saying why is the failure mode that destroys
    // trust in the feature, so the reason is surfaced unconditionally.
    const halted = run({ state: "halted", reason: "checks failed" });
    expect(autopilotChipLabel(halted)).toBe("auto ✗");
    expect(autopilotChipTitle(halted)).toContain("checks failed");
  });

  it("names a halt with no reason rather than rendering blank", () => {
    expect(autopilotChipTitle(run({ state: "halted", reason: "" }))).toContain(
      "unknown reason"
    );
  });

  it("reports skipped hooks in the tooltip", () => {
    const r = run({ skipped: ["gitnexus-index"] });
    expect(autopilotChipTitle(r)).toContain("gitnexus-index");
  });

  it("marks a finished run", () => {
    expect(autopilotChipLabel(run({ state: "done" }))).toBe("auto ✓");
  });
});

describe("the ⏩ control is a toggle", () => {
  // Regression: it used to return null once a chain was armed, so the button you
  // just pressed vanished and the only way to turn it off was the primary
  // button — which read as "I can't undo this".
  it("stays visible and turns OFF while a chain is armed", () => {
    const s = fastTrackStep({
      title: "t",
      status: "running",
      stage: "agent",
      autopilot: run(),
    });
    expect(s).not.toBeNull();
    expect(s!.active).toBe(true);
    expect(s!.title).toContain("turn fast-track off");
  });

  it("offers to arm again after a halt, and says why it stopped", () => {
    const s = fastTrackStep({
      title: "t",
      status: "running",
      stage: "interrupt",
      autopilot: run({ state: "halted", reason: "checks failed" }),
    });
    expect(s).not.toBeNull();
    expect(s!.active).toBeFalsy();
    expect(s!.hint).toBe(true);
    expect(s!.title).toContain("checks failed");
  });

  it("is OFF with no run, and mentions the toggle", () => {
    const s = fastTrackStep({ title: "t", status: "running", stage: "agent" });
    expect(s).not.toBeNull();
    expect(s!.active).toBeFalsy();
    expect(s!.title).toContain("turn it off");
  });

  it("is absent only when a press would be meaningless", () => {
    for (const o of [
      { status: "loading" },
      { status: "paused" },
      { workspace_missing: true },
      { stage: "provisioning" },
      { stage: "precommit" },
    ])
      expect(
        fastTrackStep({ title: "t", status: "running", stage: "agent", ...o })
      ).toBeNull();
  });
});

describe("the guided button keeps working while armed", () => {
  it("still offers the manual step, so arming never removes manual control", () => {
    // The autopilot used to take over this slot, which replaced Commit/Push with
    // a status readout and left no way to drive a step by hand.
    const s = nextStep({
      title: "t",
      status: "running",
      stage: "committed",
      has_origin: true,
      autopilot: run(),
    });
    expect(s?.label).toBe("Push");
  });
});

describe("liveRun", () => {
  it("only reports a running chain", () => {
    expect(liveRun({ autopilot: run() })).toBeTruthy();
    expect(liveRun({ autopilot: run({ state: "halted" }) })).toBeNull();
    expect(liveRun({ autopilot: run({ state: "done" }) })).toBeNull();
  });

  it("is null with no record or no depth", () => {
    expect(liveRun({})).toBeNull();
    expect(liveRun({ autopilot: null })).toBeNull();
    expect(liveRun({ autopilot: run({ depth: "" }) })).toBeNull();
  });
});
