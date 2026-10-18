import { describe, it, expect, afterEach } from "vitest";
import type { Caps, Instance } from "../api/types";
import { queryClient } from "../state/queries";
import {
  stageMeta,
  chipState,
  activityChip,
  checkChip,
  nextStep,
  effectiveActivity,
  noteActivity,
  forceActivity,
  dropActivity,
  markLoopReset,
  clearLoopReset,
  reconcileLoopReset,
  resetStep,
  NO_ORIGIN_CMD,
  NO_ORIGIN_ALT,
} from "../lib/stage";
import { deriveSessionState } from "../lib/sessionState";

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
    expect(chipState(inst({ title: "s1", workspace_missing: true })).label).toBe("Missing");
    expect(chipState(inst({ title: "s2", status: "loading" })).cls).toContain("s-provisioning");
    expect(chipState(inst({ title: "s3", status: "paused" })).label).toBe("Paused");
    expect(chipState(inst({ title: "s4", setup: { state: "failed" } })).label).toBe("Setup failed");
    expect(chipState(inst({ title: "s5", setup: { state: "running" } })).label).toBe("Starting");
  });

  it("reflects the session state, then falls back to the ship stage", () => {
    // The words are lib/sessionState's — the same ones the bell and the toasts
    // use; the class stays the activity word (CSS, Pane's reduce-motion cover).
    const run = chipState(inst({ title: "a1", activity: "working" }));
    expect(run.label).toBe("Working");
    expect(run.cls).toBe("s-running tone-working");
    const ask = chipState(inst({ title: "a2", activity: "clarify" }));
    expect(ask.label).toBe("Question");
    expect(ask.cls).toBe("s-clarify tone-attention");
    expect(ask.title).toContain("answer it in its pane or from the bell");
    const lim = chipState(inst({ title: "a2b", activity: "limit" }));
    expect(lim.label).toBe("Waiting on limit");
    expect(lim.cls).toBe("s-limit tone-waiting");
    expect(chipState(inst({ title: "a3", stage: "agent", activity: "idle" })).label).toBe("Idle");
    const off = chipState(inst({ title: "a4", stage: "agent", activity: "offline" }));
    expect(off.label).toBe("Offline");
    expect(off.cls).toBe("s-offline tone-broken");
    expect(chipState(inst({ title: "a5", stage: "committed", activity: "idle" })).cls).toBe(
      "s-committed"
    );
    const pc = chipState(inst({ title: "a6", stage: "interrupt", activity: "idle", failed_step: "typecheck" }));
    expect(pc.label).toBe("Pre-commit failed");
    expect(pc.title).toBe("Pre-commit failed at: typecheck");
  });

  it("keeps the failing hook — a name or a whole line of output — in the tooltip", () => {
    const line =
      "branch 'feature/sc-20834/token-saving-and-traffic-reduction' set up to track ...";
    const c = chipState(
      inst({ title: "a7", stage: "interrupt", activity: "idle", failed_step: line })
    );
    expect(c.label).toBe("Pre-commit failed");
    expect(c.title).toContain(line);
  });

  it("paints the dot's object: the same state the caller derived", () => {
    const row = inst({ title: "same", activity: "offline" });
    const ss = deriveSessionState(row);
    expect(chipState(row, ss)).toEqual(chipState(row));
    expect(chipState(row, ss).cls).toContain("tone-" + ss.tone);
  });
});

describe("checkChip (verification gate)", () => {
  it("maps the check state to its chip, or null", () => {
    expect(checkChip(inst({ check: null }))).toBeNull();
    expect(checkChip(inst({ check: { state: "running" } }))!.label).toBe("Checks…");
    expect(checkChip(inst({ check: { state: "failed" } }))!.label).toBe("Checks ✗");
    expect(checkChip(inst({ check: { state: "ok" } }))!.label).toBe("Checks ✓");
    expect(checkChip(inst({ check: { state: "ok", stale: true } as never }))!.label).toBe("Checks stale");
  });
});

describe("nextStep (guided next action)", () => {
  const step = (o: Partial<Instance>) => nextStep(inst({ status: "running", ...o }));
  it("walks the commit -> push -> PR -> merge cycle", () => {
    expect(step({ title: "n1", stage: "agent" })?.label).toBe("Commit…");
    expect(step({ title: "n2", stage: "interrupt" })?.label).toBe("Re-commit");
    expect(step({ title: "n3", stage: "committed" })?.label).toBe("Push");
    expect(step({ title: "n4", stage: "pushed" })?.label).toBe("Open PR");
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
    expect(pr?.label).toBe("Open PR ↗");
    expect(pr?.hint).toBe(true);
    expect(typeof pr?.run).toBe("function");
    expect(pr?.title).toContain(
      "add a GitHub token in Settings → Connections → GitHub → Connect, or install the GitHub CLI"
    );
    const merge = step({ title: "g2", stage: "pr", pr_url: "https://github.com/o/r/pull/7" });
    expect(merge?.label).toBe("Merge on GitHub ↗");
    expect(merge?.hint).toBe(true);
    // No PR URL known yet: still offered, still a hint, never a dead end.
    expect(step({ title: "g3", stage: "pr" })?.label).toBe("Merge ↗");
  });

  it("uses the plain PR labels when the server can open PRs itself", () => {
    setCaps({ github: true });
    expect(step({ title: "g4", stage: "pushed" })?.label).toBe("Open PR");
    expect(step({ title: "g5", stage: "pushed" })?.hint).toBeUndefined();
    expect(step({ title: "g6", stage: "pr" })?.label).toBe("Merge");
  });

  it("assumes PR support when the server never reports the capability", () => {
    // Feature-detected against an explicit false: an older server that omits
    // `github` must not lose its Make PR button.
    setCaps({});
    expect(step({ title: "g7", stage: "pushed" })?.label).toBe("Open PR");
    expect(step({ title: "g8", stage: "pr" })?.label).toBe("Merge");
  });

  it("has no next step while loading, paused, or workspace-gone", () => {
    expect(nextStep(inst({ title: "n7", status: "loading", stage: "agent" }))).toBeNull();
    expect(nextStep(inst({ title: "n8", status: "paused", stage: "agent" }))).toBeNull();
    expect(nextStep(inst({ title: "n9", status: "running", workspace_missing: true, stage: "agent" }))).toBeNull();
  });
});

describe("back-to-idle pin", () => {
  it("honours the SERVER's pin: the ladder restarts while stage stays the truth", () => {
    // stage_reset rides alongside stage (which is still git truth), so a pinned
    // window reads "idle"/"Commit…" without anyone lying about the branch.
    const pinned = inst({
      title: "sr1",
      status: "running",
      stage: "pr",
      stage_reset: true,
      activity: "idle",
    });
    expect(nextStep(pinned)?.label).toBe("Commit…");
    expect(chipState(pinned).label).toBe("Idle");
    // Released (a new commit landed): the real stage takes over again with no
    // client-side bookkeeping involved.
    expect(nextStep(inst({ title: "sr1", status: "running", stage: "pr" }))?.label).toBe("Merge");
  });

  it("echoes the press locally until the server's row confirms it", () => {
    const row = inst({ title: "loopy", status: "running", stage: "pr", activity: "idle" });
    markLoopReset("loopy");
    // Echo: the header flips on the click, before the round trip lands.
    expect(nextStep(row)?.label).toBe("Commit…");
    expect(chipState(row).label).toBe("Idle");
    // A row that does NOT yet carry the pin must not cancel the echo — that is
    // exactly the in-flight window the echo exists for.
    reconcileLoopReset(row);
    expect(nextStep(row)?.label).toBe("Commit…");
    // Confirmed: the echo is dropped and the server's flag carries it alone.
    reconcileLoopReset(inst({ title: "loopy", stage: "pr", stage_reset: true }));
    expect(nextStep(row)?.label).toBe("Merge"); // the un-flagged row is no longer pinned
    clearLoopReset("loopy");
  });

  it("offers ↺ only where the ladder has somewhere to be put back from", () => {
    setCaps({ git: true });
    const at = (o: Partial<Instance>) => resetStep(inst({ status: "running", ...o }));
    // Finished-branch stages: offered, and the tooltip names the ask it silences.
    expect(at({ title: "r1", stage: "committed" })?.label).toBe("↺");
    expect(at({ title: "r2", stage: "committed" })?.title).toContain("push");
    expect(at({ title: "r3", stage: "pushed" })?.title).toContain("open a PR");
    expect(at({ title: "r4", stage: "pr" })?.title).toContain("merge");
    // Nothing to reset: the ladder is already at (or before) its start.
    expect(at({ title: "r5", stage: "agent" })).toBeNull();
    expect(at({ title: "r6", stage: "precommit" })).toBeNull();
    expect(at({ title: "r7", stage: "interrupt" })).toBeNull();
    // Already pinned — server-side or by the local echo. ↺ is a one-shot action,
    // so once it has done its job it leaves rather than sitting there ON.
    expect(at({ title: "r8", stage: "pr", stage_reset: true })).toBeNull();
    markLoopReset("r9");
    expect(at({ title: "r9", stage: "pr" })).toBeNull();
    clearLoopReset("r9");
    // Sessions that cannot act at all, and repos with no git.
    expect(at({ title: "r10", stage: "pr", status: "paused" })).toBeNull();
    expect(at({ title: "r11", stage: "pr", workspace_missing: true })).toBeNull();
    setCaps({ git: false });
    expect(at({ title: "r12", stage: "pr" })).toBeNull();
  });
});

describe("activityChip (the Assistant window's pill)", () => {
  it("speaks the same five words a session's pill does", () => {
    // The Assistant has an agent but no session, so it is painted from the
    // activity alone. If these two ever disagree, one window says "running" in
    // a colour that means something else — the exact drift the split avoids.
    for (const act of ["working", "clarify", "limit"]) {
      const own = activityChip(act);
      const viaSession = chipState(inst({ title: "ac-" + act, activity: act }));
      expect(own).toEqual(viaSession);
    }
  });

  it("distinguishes a parked agent from one that isn't running", () => {
    expect(activityChip("idle").label).toBe("Idle");
    expect(activityChip("offline").label).toBe("Offline");
    // Anything the server hasn't taught it is parked, never "running": a chip
    // that invents work is worse than one that under-reports it.
    expect(activityChip("").label).toBe("Idle");
    expect(activityChip("something-new").label).toBe("Idle");
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
    expect(stageMeta("committed").label).toBe("Committed");
    expect(stageMeta("nonsense").label).toBe(stageMeta("agent").label);
  });

  it("speaks like the state chips: sentence case, the verdict mark after the word", () => {
    // The stage chip sits in the state chip's slot ("Working", "Question"):
    // one capitalisation for both, and ✗ / ✓ always trail ("Checks ✗").
    for (const s of ["provisioning", "agent", "precommit", "interrupt", "committed", "pushed", "pr", "merged"]) {
      const l = stageMeta(s).label;
      expect(l[0], s).toBe(l[0].toUpperCase());
      expect(l, s).not.toMatch(/^[✓✗]/);
    }
    expect(stageMeta("interrupt").label).toBe("Pre-commit ✗");
    expect(stageMeta("pushed").label).toBe("Pushed");
    for (const c of [{ state: "running" }, { state: "failed" }, { state: "ok" }, { state: "ok", stale: true }]) {
      const l = checkChip(inst({ check: c as never }))!.label;
      expect(l).toMatch(/^Checks/);
      expect(l).not.toMatch(/^[✓✗]/);
    }
  });
});
