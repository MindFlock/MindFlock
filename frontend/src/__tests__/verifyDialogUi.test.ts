/** What outlived the Verify dialog's wiring (Verify is a stop on the route
 * now; its card is components/behaviors/BehaviorsCard.tsx): the cancel
 * request and the second-window rule's toast. The cancel ROUTE is a backend
 * contract and stays pinned in Python. (The toast's "in Verify" wording is
 * lib/verifyActions.ts's — not this task's file; see docs/vision/verify.md.) */

import { describe, expect, it, vi } from "vitest";
import type { TestPlan } from "../api/types";
import { src } from "./sourceText";

const api = vi.fn();
const toast = vi.fn();
vi.mock("../api/client", async (orig) => ({ ...((await orig()) as object), api: (...a: unknown[]) => api(...a) }));
vi.mock("../lib/toast", () => ({ toast: (...a: unknown[]) => toast(...a) }));

const { cancelChecklistRun, writePlanFor } = await import("../lib/verifyActions");

describe("Cancel run", () => {
  it("POSTs the plan's cancel route", async () => {
    api.mockReset().mockResolvedValue({});
    await cancelChecklistRun({ id: "feat/x" } as TestPlan);
    expect(api).toHaveBeenCalledWith("/api/test-plans/feat%2Fx/cancel", { method: "POST" });
  });
});

describe("the second-window rule", () => {
  it("never offers a session whose (repo, branch) another window's plan covers", () => {
    // Driven in testPlans.test.ts (checklistTargets).
    expect(src("components/dialogs/verify.ts")).toContain("coverageKey(repoName(plan.repo_root), plan.branch)");
  });

  it("names the window the behaviors are under when a press lands on it", async () => {
    api.mockReset().mockResolvedValue({ plan: "bot7", existing: true });
    toast.mockReset();
    await writePlanFor("bot7-copy");
    expect(toast).toHaveBeenCalledWith(
      "bot7-copy's branch already has behaviors — they're under bot7 — they're on its route in the Sky"
    );
  });
});
