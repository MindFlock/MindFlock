/** Verify walks IN THE SESSION that wrote the work (owner request
 * 2026-11-10): when the run route hands the steps to the plan's own
 * session's agent (`in_session`), "Check now" / "Let the agent walk every
 * behavior" say so and open that session's Agent tab; a separate checker
 * (the plan's session is gone) keeps its "Starting an agent on …" line. The
 * flight's pane strip offers "Walking here — talk to it" while its Verify
 * stop is walked in place. vitest is node-only: the decisions are pinned
 * through the pure helpers. */

import { beforeEach, describe, expect, it, vi } from "vitest";
import type { TestPlan } from "../api/types";

const api = vi.fn();
const toast = vi.fn();
const selected: string[] = [];
vi.mock("../api/client", async (orig) => ({ ...((await orig()) as object), api: (...a: unknown[]) => api(...a) }));
vi.mock("../lib/toast", () => ({ toast: (...a: unknown[]) => toast(...a) }));
vi.mock("../lib/sessionActions", async (orig) => ({
  ...((await orig()) as object),
  selectSession: (t: string) => selected.push(t),
}));
vi.mock("../state/queries", async (orig) => ({
  ...((await orig()) as object),
  refreshInstances: () => Promise.resolve(),
  refreshTestPlans: () => Promise.resolve(),
}));

const { runStartedMessage, startChecklistRun } = await import("../lib/verifyActions");
const { verifyWalkSession } = await import("../components/flights/FlightStrip");
const { useUi } = await import("../state/store");

const plan = (over: Partial<TestPlan> = {}): TestPlan =>
  ({
    id: "sc-1",
    title: "sc-1",
    repo_root: "/r/shop",
    branch: "feature/sc-1",
    sha: "a1b2c3d4",
    live_branch: "main",
    effective_live_branch: "main",
    state: "generated",
    error: "",
    steps: [{ id: "s1", text: "do s1", actor: "agent" }],
    runs: [],
    run_session: "",
    ...over,
  }) as TestPlan;

describe("runStartedMessage", () => {
  it("names the session whose agent walks the steps, and says to talk to it", () => {
    const m = runStartedMessage(plan(), { session: "sc-1", in_session: true });
    expect(m).toBe("Handed the steps to sc-1's agent. Talk to it there");
    expect(m).not.toMatch(/Starting an agent/);
  });

  it("says when the walk waits for the agent's current turn", () => {
    const m = runStartedMessage(plan(), { session: "sc-1", in_session: true, queued: true });
    expect(m).toContain("once its current turn ends");
    expect(m).not.toContain("behavior");
  });

  it("keeps the checker sentence when the plan's session is gone", () => {
    const m = runStartedMessage(plan(), { session: "verify-sc-1-a1b2c3d" });
    expect(m).toMatch(/^Starting an agent on .* in verify-sc-1-a1b2c3d$/);
  });
});

describe("startChecklistRun", () => {
  beforeEach(() => {
    api.mockReset();
    toast.mockReset();
    selected.length = 0;
  });

  it("opens the walking session's Agent tab after handing it the steps", async () => {
    api.mockResolvedValue({ ok: true, session: "sc-1", in_session: true, queued: false });
    await startChecklistRun(plan());
    expect(toast).toHaveBeenCalledWith("Handed the steps to sc-1's agent. Talk to it there");
    expect(selected).toEqual(["sc-1"]);
    expect(useUi.getState().lastTab?.["sc-1"]).toBe("agent");
  });

  it("does not teleport into a separate checker", async () => {
    api.mockResolvedValue({ ok: true, session: "verify-sc-1-a1b2c3d" });
    await startChecklistRun(plan());
    expect(selected).toEqual([]);
  });
});

describe("verifyWalkSession", () => {
  const flight = (detail: Record<string, unknown>, state = "running") => ({
    session: "sc-1",
    state,
    cursor: 0,
    steps: [{ id: "v", kind: "verify" }],
    step_states: { v: { state: "running", since: 0, note: "", detail, rounds: 0 } },
  });

  it("is the walking session while the stop is walked in place", () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    expect(verifyWalkSession(flight({ in_session: true, walk_session: "sc-1" }) as any)).toBe("sc-1");
  });

  it("is empty for a checker, a stop parked on you, or no flight", () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    expect(verifyWalkSession(flight({ in_session: false }) as any)).toBe("");
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    expect(verifyWalkSession(flight({ in_session: true }, "waiting_you") as any)).toBe("");
    expect(verifyWalkSession(null)).toBe("");
  });
});
