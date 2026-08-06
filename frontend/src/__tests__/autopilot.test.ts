/** The client half of the autopilot ladder — pure, so it is tested directly.
 * Mirrors tests/unit/test_autopilot.py; the two must agree on the rungs, on
 * which stage satisfies which target, and on merge never being satisfied by an
 * observed stage. */

import { beforeEach, describe, expect, it } from "vitest";
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
import {
  clearStep,
  fastTrackStep,
  followAutopilot,
  liveStep,
  markStep,
  nextStep,
  resetFollow,
} from "../lib/stage";
import type { AutopilotRun, Instance } from "../api/types";

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

  it("stays cancellable even while provisioning or committing", () => {
    // An intake-armed session spends its first minutes provisioning, which is
    // precisely when you might change your mind — and the guards above used to
    // hide the toggle there entirely, leaving no way to stop it at all.
    for (const o of [
      { status: "loading" },
      { stage: "provisioning" },
      { stage: "precommit" },
    ]) {
      const s = fastTrackStep({
        title: "t",
        status: "running",
        stage: "agent",
        autopilot: run(),
        ...o,
      });
      expect(s, JSON.stringify(o)).not.toBeNull();
      expect(s!.active).toBe(true);
    }
  });

  it("still surfaces a halted run while provisioning", () => {
    const s = fastTrackStep({
      title: "t",
      status: "loading",
      autopilot: run({ state: "halted", reason: "checks failed" }),
    });
    expect(s?.label).toBe("⏩✗");
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

describe("liveStep (the pane header's live step)", () => {
  const step = (o: Partial<Instance>) => liveStep({ title: "t", status: "running", ...o });

  it("reads as ACTIVE while pre-commit hooks run", () => {
    // Regression: this state used to render as a DISABLED grey pill reading
    // "pre-commit" — the busiest moment in the workflow looked broken.
    const s = step({ stage: "precommit" });
    expect(s?.label).toBe("pre-commit");
    expect(s?.tone).toBe("work");
  });

  it("marks a blocked commit as blocked, naming the hook", () => {
    const s = step({ stage: "interrupt", failed_step: "Run Tests" });
    expect(s?.tone).toBe("blocked");
    expect(s?.title).toContain("Run Tests");
  });

  it("surfaces worktree setup above everything else", () => {
    // Queued prompts are HELD during setup and the driver refuses to act, so it
    // has to be visible.
    expect(step({ stage: "agent", setup: { state: "running" } })?.label).toBe("setting up");
    expect(step({ stage: "agent", setup: { state: "failed" } })?.tone).toBe("blocked");
  });

  it("shows running and failed verification checks", () => {
    expect(step({ stage: "committed", check: { state: "running" } })?.label).toBe("checks");
    expect(step({ stage: "committed", check: { state: "failed" } })?.tone).toBe("blocked");
  });

  it("says what an armed chain is waiting on, in the server's words", () => {
    const s = step({
      stage: "agent",
      autopilot: run({ note: "prompt queue still has work" }),
    });
    expect(s?.label).toBe("prompt queue still has work");
    expect(s?.target).toBe("→ PR");
  });

  it("reports a halted chain as blocked", () => {
    const s = step({ stage: "agent", autopilot: run({ state: "halted", reason: "checks failed" }) });
    expect(s?.tone).toBe("blocked");
    expect(s?.title).toContain("checks failed");
  });

  it("offers an open PR as a link", () => {
    const s = step({ stage: "pr", pr_url: "https://example.test/pr/1" });
    expect(s?.tone).toBe("ok");
    expect(s?.href).toBe("https://example.test/pr/1");
  });

  it("is null when nothing is happening", () => {
    expect(step({ stage: "agent" })).toBeNull();
    expect(step({ stage: "committed" })).toBeNull();
  });

  it("shows in-flight push/PR/merge, which have no stage of their own", () => {
    markStep("t", "push");
    expect(step({ stage: "committed" })?.label).toBe("pushing");
    // …and clears itself once the stage catches up.
    expect(step({ stage: "pushed" })).toBeNull();
    clearStep("t");
  });
});

describe("followAutopilot (go where the run is)", () => {
  const inst = (o: Partial<Instance>) => ({ title: "ft", ...o }) as Partial<Instance>;

  beforeEach(() => resetFollow());

  it("switches to the terminal when the run reaches the commit step", () => {
    // Seed the prior step, then transition — that is what a real run does.
    followAutopilot(inst({ autopilot: run({ step: "" }) }));
    expect(followAutopilot(inst({ autopilot: run({ step: "commit" }) }))).toBe("commit");
  });

  it("fires only ONCE per step", () => {
    followAutopilot(inst({ autopilot: run({ step: "" }) }));
    expect(followAutopilot(inst({ autopilot: run({ step: "commit" }) }))).toBe("commit");
    expect(followAutopilot(inst({ autopilot: run({ step: "commit" }) }))).toBeNull();
    expect(followAutopilot(inst({ autopilot: run({ step: "commit" }) }))).toBeNull();
  });

  it("does not steal focus on a FIRST poll sighting", () => {
    // Loading the page mid-commit must not yank you to that window.
    expect(followAutopilot(inst({ autopilot: run({ step: "commit" }) }))).toBeNull();
  });

  it("does act on a first sighting from a LIVE event", () => {
    // A live event IS the transition, so there is nothing stale about it.
    expect(
      followAutopilot(inst({ autopilot: run({ step: "commit" }) }), { live: true })
    ).toBe("commit");
  });

  it("opens the PR when the run reaches the pr step", () => {
    followAutopilot(inst({ autopilot: run({ step: "commit" }) }), { live: true });
    expect(
      followAutopilot(inst({ autopilot: run({ step: "pr", url: "https://x.test/1" }) }))
    ).toBe("pr");
  });

  it("falls back to the session's own pr_url", () => {
    followAutopilot(inst({ autopilot: run({ step: "push" }) }));
    expect(
      followAutopilot(inst({ autopilot: run({ step: "pr" }), pr_url: "https://y.test/2" }))
    ).toBe("pr");
  });

  it("ignores runs that are not running, and re-arms cleanly after", () => {
    expect(followAutopilot(inst({ autopilot: run({ state: "halted", step: "commit" }) }))).toBeNull();
    expect(followAutopilot(inst({ autopilot: run({ state: "done", step: "commit" }) }))).toBeNull();
    expect(followAutopilot(inst({}))).toBeNull();
    // A halted run cleared the guard, so a fresh run's commit step fires again.
    expect(
      followAutopilot(inst({ autopilot: run({ step: "commit" }) }), { live: true })
    ).toBe("commit");
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
