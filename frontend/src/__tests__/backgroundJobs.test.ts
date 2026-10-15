/** lib/backgroundJobs — what the rail says about jobs an agent backgrounded
 * and left running after its turn (backend/providers/claude_background.py).
 *
 * Pinned: running jobs get no chip (the row already reads Working); a waiting
 * job is a quiet chip; a stuck job's chip opens a menu whose Stop names the
 * pid it stops; stuck outranks waiting; junk/absent fields are no chip; the
 * state chip's tooltip carries the job; and the chip is wired on the rail row
 * through the stop action (inside its menu, JobMenu). The words with an
 * expected duration: jobExpectation.test.ts. */

import { describe, expect, it } from "vitest";
import type { BackgroundJob, Instance } from "../api/types";
import { backgroundChip, backgroundHint } from "../lib/backgroundJobs";
import { deriveSessionState } from "../lib/sessionState";
import { normCode } from "./sourceText";

const job = (state: string, o: Partial<BackgroundJob> = {}): BackgroundJob => ({
  id: "b1",
  pid: 100,
  command: 'until ! pgrep -f "git commit -q -F"; do sleep 5; done',
  state,
  reason: "no CPU or output for 37m",
  started: 0,
  quiet_s: 37 * 60,
  ...o,
});

describe("backgroundChip", () => {
  it("draws nothing for running jobs, no jobs, or junk", () => {
    expect(backgroundChip({ background: [job("running")] })).toBeNull();
    expect(backgroundChip({ background: [] })).toBeNull();
    expect(backgroundChip({ background: null })).toBeNull();
    expect(backgroundChip({})).toBeNull();
    expect(backgroundChip({ background: [{} as BackgroundJob] })).toBeNull();
  });

  it("a waiting job is informational", () => {
    const c = backgroundChip({ background: [job("waiting", { quiet_s: 300 })] })!;
    expect(c.label).toBe("job waiting");
    expect(c.stopPid).toBeUndefined();
    expect(c.title).toContain("quiet 5m");
    expect(c.title).toContain("resumes when it finishes");
  });

  it("a stuck job's chip carries Stop and outranks waiting", () => {
    const c = backgroundChip({ background: [job("waiting", { pid: 1 }), job("stuck", { pid: 2 })] })!;
    expect(c.cls).toContain("bg-stuck");
    expect(c.stopPid).toBe(2);
    expect(c.title).toContain("git commit -q -F");
    expect(c.title).toContain("click to stop it");
    const two = backgroundChip({ background: [job("stuck", { pid: 2 }), job("stuck", { pid: 3 })] })!;
    expect(two.label).toBe("2 jobs stuck ✕");
    // A proxied row is stopped on its own device: no button here.
    const remote = backgroundChip({ device: "rig", background: [job("stuck")] })!;
    expect(remote.stopPid).toBeUndefined();
    expect(remote.title).not.toContain("click");
  });
});

describe("the state chip's tooltip", () => {
  const base: Partial<Instance> = { title: "t", status: "running" };

  it("idle names the job it is waiting on, without the click text", () => {
    const ss = deriveSessionState({ ...base, background: [job("stuck")] }, { activity: "idle" });
    expect(ss.label).toBe("Idle");
    expect(ss.hint).toContain("Background job stuck (no CPU or output for 37m)");
    expect(ss.hint).not.toContain("click");
  });

  it("working after the turn names the running job", () => {
    const ss = deriveSessionState({ ...base, background: [job("running", { command: "pytest -x" })] }, { activity: "working" });
    expect(ss.hint).toBe("Turn ended; a background job is still running: pytest -x");
    expect(backgroundHint({})).toBe("");
  });
});

describe("rail wiring", () => {
  it("the row renders the chip and stops through the server route", () => {
    const row = normCode("components/sidebar/SidebarRow.tsx");
    expect(row).toContain("backgroundChip(inst)");
    expect(row).toContain("<JobMenu title={title} job={bg.job} stopPid={bg.stopPid}");
    expect(row).toContain("stopBackgroundJob(title, stopPid)");
    const stop = normCode("lib/stopBackgroundJob.ts");
    expect(stop).toContain('"/background/" + pid + "/stop"');
  });
});
