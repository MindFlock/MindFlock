import { describe, it, expect, afterEach } from "vitest";
import type { Caps, Instance } from "../api/types";
import { queryClient } from "../state/queries";
import {
  stageMeta,
  chipState,
  checkChip,
  nextStep,
  effectiveActivity,
  noteActivity,
  forceActivity,
  dropActivity,
  markLoopReset,
  clearLoopReset,
  reconcileLoopReset,
  NO_ORIGIN_CMD,
  NO_ORIGIN_ALT,
} from "../lib/stage";

const inst = (o: Partial<Instance>): Instance => o as unknown as Instance;

/** Publish a caps payload the way the /api/config query would. */
function setCaps(caps: Partial<Caps>) {
  queryClient.setQueryData(["config"], {
    caps: { git: true, tailscale: true, ticketing: true, ...caps },
  });
}
afterEach(() => queryClient.removeQueries({ queryKey: ["config"] }));

describe("chipState (persistent status chip)", () => {
  it("prioritizes lifecycle states over activity/stage", () => {
    expect(chipState(inst({ title: "s1", workspace_missing: true })).label).toBe("missing");
    expect(chipState(inst({ title: "s2", status: "loading" })).cls).toBe("s-provisioning");
    expect(chipState(inst({ title: "s3", status: "paused" })).label).toBe("paused");
    expect(chipState(inst({ title: "s4", setup: { state: "failed" } })).label).toBe("setup ✗");
    expect(chipState(inst({ title: "s5", setup: { state: "running" } })).label).toBe("setting up");
  });

  it("reflects live activity, then falls back to the workflow stage", () => {
    expect(chipState(inst({ title: "a1", activity: "working" })).label).toBe("running");
    expect(chipState(inst({ title: "a2", activity: "clarify" })).label).toBe("clarify");
    const lim = chipState(inst({ title: "a2b", activity: "limit" }));
    expect(lim.label).toBe("limit");
    expect(lim.cls).toBe("s-limit");
    expect(chipState(inst({ title: "a3", stage: "agent", activity: "idle" })).label).toBe("idle");
    expect(chipState(inst({ title: "a4", stage: "agent", activity: "offline" })).label).toBe(
      "offline"
    );
    expect(chipState(inst({ title: "a5", stage: "committed", activity: "idle" })).cls).toBe(
      "s-committed"
    );
    expect(chipState(inst({ title: "a6", stage: "interrupt", activity: "idle", failed_step: "typecheck" })).label).toBe(
      "✗ typecheck"
    );
  });

  it("badges a hook NAME but keeps a line of hook output in the tooltip", () => {
    // The server's generic fallback can return a whole output line (up to 80
    // chars). Pills are never truncated, so a sentence has to stay out of one.
    const line =
      "branch 'feature/sc-20834/token-saving-and-traffic-reduction' set up to track ...";
    const c = chipState(
      inst({ title: "a7", stage: "interrupt", activity: "idle", failed_step: line })
    );
    expect(c.label).toBe("pre-commit ✗");
    expect(c.title).toContain(line);

    // "Run Tests (+3)" is a name, spaces and all — it still gets badged.
    expect(
      chipState(inst({ title: "a8", stage: "interrupt", activity: "idle", failed_step: "Run Tests (+3)" }))
        .label
    ).toBe("✗ Run Tests (+3)");
  });
});

describe("checkChip (verification gate)", () => {
  it("maps the check state to its chip, or null", () => {
    expect(checkChip(inst({ check: null }))).toBeNull();
    expect(checkChip(inst({ check: { state: "running" } }))!.label).toBe("checks…");
    expect(checkChip(inst({ check: { state: "failed" } }))!.label).toBe("✗ checks");
    expect(checkChip(inst({ check: { state: "ok" } }))!.label).toBe("✓ checks");
    expect(checkChip(inst({ check: { state: "ok", stale: true } as never }))!.label).toBe("✓ stale");
  });
});

describe("nextStep (guided next action)", () => {
  const step = (o: Partial<Instance>) => nextStep(inst({ status: "running", ...o }));
  it("walks the commit -> push -> PR -> merge cycle", () => {
    expect(step({ title: "n1", stage: "agent" })?.label).toBe("Commit…");
    expect(step({ title: "n2", stage: "interrupt" })?.label).toBe("Re-commit");
    expect(step({ title: "n3", stage: "committed" })?.label).toBe("Push");
    expect(step({ title: "n4", stage: "pushed" })?.label).toBe("Make PR");
    expect(step({ title: "n5", stage: "pr" })?.label).toBe("Merge");
  });

  it("offers a non-destructive hint when there is no origin remote", () => {
    const ns = step({ title: "n6", stage: "committed", has_origin: false });
    expect(ns?.label).toBe("No remote — add origin…");
    expect(ns?.hint).toBe(true);
    // The copied line is runnable, not a <url> placeholder, and SSH is the
    // example — MindFlock never rewrites your remote, so SSH is first-class.
    expect(NO_ORIGIN_CMD).toContain("git remote add origin");
    expect(NO_ORIGIN_CMD).toContain("git@github.com:");
    expect(NO_ORIGIN_CMD).not.toContain("<url>");
    // ...and HTTPS is offered alongside it, so neither reads as "the real one".
    expect(ns?.title).toContain(NO_ORIGIN_ALT);
  });

  it("keeps PR + Merge actionable when gh/token are absent (github: false)", () => {
    setCaps({ github: false });
    const pr = step({ title: "g1", stage: "pushed" });
    // Still runnable — it degrades to GitHub's prefilled compare page — but
    // rendered as a hint that says where the click lands.
    expect(pr?.label).toBe("Make PR ↗");
    expect(pr?.hint).toBe(true);
    expect(typeof pr?.run).toBe("function");
    expect(pr?.title).toContain(
      "add a GitHub token in Intake → Pull requests, or install the GitHub CLI"
    );
    const merge = step({ title: "g2", stage: "pr", pr_url: "https://github.com/o/r/pull/7" });
    expect(merge?.label).toBe("Merge on GitHub ↗");
    expect(merge?.hint).toBe(true);
    // No PR URL known yet: still offered, still a hint, never a dead end.
    expect(step({ title: "g3", stage: "pr" })?.label).toBe("Merge ↗");
  });

  it("uses the plain PR labels when the server can open PRs itself", () => {
    setCaps({ github: true });
    expect(step({ title: "g4", stage: "pushed" })?.label).toBe("Make PR");
    expect(step({ title: "g5", stage: "pushed" })?.hint).toBeUndefined();
    expect(step({ title: "g6", stage: "pr" })?.label).toBe("Merge");
  });

  it("assumes PR support when the server never reports the capability", () => {
    // Feature-detected against an explicit false: an older server that omits
    // `github` must not lose its Make PR button.
    setCaps({});
    expect(step({ title: "g7", stage: "pushed" })?.label).toBe("Make PR");
    expect(step({ title: "g8", stage: "pr" })?.label).toBe("Merge");
  });

  it("has no next step while loading, paused, or workspace-gone", () => {
    expect(nextStep(inst({ title: "n7", status: "loading", stage: "agent" }))).toBeNull();
    expect(nextStep(inst({ title: "n8", status: "paused", stage: "agent" }))).toBeNull();
    expect(nextStep(inst({ title: "n9", status: "running", workspace_missing: true, stage: "agent" }))).toBeNull();
  });
});

describe("loop-reset pin", () => {
  it("pins the guided stage back to agent after a PR, then clears once git moves off pr", () => {
    const row = inst({ title: "loopy", status: "running", stage: "pr", activity: "idle" });
    markLoopReset("loopy");
    // Pinned: real stage is "pr" but the guided cycle restarts at "Commit…".
    expect(nextStep(row)?.label).toBe("Commit…");
    expect(chipState(row).label).toBe("idle");
    // Still on pr -> pin holds.
    reconcileLoopReset(inst({ title: "loopy", stage: "pr" }));
    expect(nextStep(row)?.label).toBe("Commit…");
    // Git-derived stage genuinely left pr -> pin drops, real stage takes over.
    reconcileLoopReset(inst({ title: "loopy", stage: "agent" }));
    expect(nextStep(row)?.label).toBe("Merge");
    clearLoopReset("loopy");
  });
});

describe("activity debounce", () => {
  it("requires two consecutive polls to change the shown activity", () => {
    noteActivity(inst({ title: "dbg", activity: "idle" }));
    expect(effectiveActivity(inst({ title: "dbg" }))).toBe("idle");
    noteActivity(inst({ title: "dbg", activity: "working" }));
    expect(effectiveActivity(inst({ title: "dbg" }))).toBe("idle"); // one poll: not yet
    noteActivity(inst({ title: "dbg", activity: "working" }));
    expect(effectiveActivity(inst({ title: "dbg" }))).toBe("working"); // two polls: flips
    dropActivity("dbg");
  });

  it("forceActivity applies immediately (event stream, no debounce)", () => {
    forceActivity("evt", "clarify");
    expect(effectiveActivity(inst({ title: "evt" }))).toBe("clarify");
    dropActivity("evt");
  });
});

describe("stageMeta", () => {
  it("falls back to the agent descriptor for unknown stages", () => {
    expect(stageMeta("committed").label).toBe("committed");
    expect(stageMeta("nonsense").label).toBe(stageMeta("agent").label);
  });
});
