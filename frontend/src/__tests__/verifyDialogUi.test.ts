/** The Verify dialog's wiring: the Cancel run item, the at-a-glance tally and
 * its roll-ups, the sidebar's failure pill, the list's filter-and-select kit,
 * and the second-window rule's toast.
 *
 * Moved from tests/unit/test_test_plans.py's bundle checks ("a stale build is
 * the one failure this feature cannot see from either side" — the CI bundle
 * freshness job owns that now). The cancel ROUTE is a backend contract and
 * stays pinned in Python; the roll-up arithmetic and the coverage rule are
 * driven in testPlans.test.ts (groupPlans, planTargets). */

import { describe, expect, it, vi } from "vitest";
import type { TestPlan } from "../api/types";
import { cssRule, src, tagsWith, tagsWithId } from "./sourceText";

const api = vi.fn();
const toast = vi.fn();
vi.mock("../api/client", async (orig) => ({ ...((await orig()) as object), api: (...a: unknown[]) => api(...a) }));
vi.mock("../lib/toast", () => ({ toast: (...a: unknown[]) => toast(...a) }));

const { cancelChecklistRun, writePlanFor } = await import("../lib/verifyActions");

const PLAN = src("components/dialogs/VerifyPlan.tsx");
const DIALOG = src("components/dialogs/VerifyDialog.tsx");
const CSS = src("components/dialogs/VerifyDialog.css");

describe("Cancel run", () => {
  it("is a run-time menu item, styled as one", () => {
    expect(PLAN).toContain('label: "Cancel run",');
    expect(PLAN).toContain('className: "test-btn vf-cancel",');
    expect(cssRule(CSS, ".vf-menu-list .vf-cancel:hover")).not.toBe("");
  });

  it("POSTs the plan's cancel route", async () => {
    api.mockReset().mockResolvedValue({});
    await cancelChecklistRun({ id: "feat/x" } as TestPlan);
    expect(api).toHaveBeenCalledWith("/api/test-plans/feat%2Fx/cancel", { method: "POST" });
  });
});

describe("the at-a-glance tally", () => {
  it("ships the collapsed row's tally, and the CSS that keeps it from being cut off", () => {
    expect(tagsWith(PLAN, "className", "vf-plan-tally")).toHaveLength(1);
    expect(cssRule(CSS, ".vf-plan-tally")).not.toBe("");
    expect(cssRule(CSS, ".vf-plan-meta-text")).not.toBe("");
  });

  it("rolls the group heading up in words", () => {
    // Driven in testPlans.test.ts ("1 step failed · 1 step needs you").
    const verify = src("components/dialogs/verify.ts");
    expect(verify).toContain('sum.failed + (sum.failed === 1 ? " step failed" : " steps failed")');
    expect(verify).toContain('sum.yours + (sum.yours === 1 ? " step needs" : " steps need") + " you"');
  });

  it("says something shipped and is broken OUTSIDE the dialog", () => {
    expect(tagsWith(src("components/sidebar/VerifyBar.tsx"), "className", "dc-count dc-count-bad")).toHaveLength(1);
    expect(cssRule(src("components/sidebar/toolbars.css"), "#verify-bar .dc-count.dc-count-bad")).not.toBe("");
  });

  it("can bring the log back after the run that produced it ended", () => {
    expect(PLAN).toContain('label: running ? "Show the checking run here" : "Show the last checking run here",');
  });

  it("finds one among many and acts on several", () => {
    // The list dialogs' own filter box and checkbox kit, reused.
    expect(tagsWithId(DIALOG, "vf-plans-filter").map((t) => t.name)).toEqual(["DialogFilter"]);
    expect(DIALOG).toContain('label="Select every checklist shown"');
    expect(DIALOG).toContain('"Delete selected"');
    expect(cssRule(CSS, ".vf-plan-check")).not.toBe("");
    expect(cssRule(CSS, ".vf-plan.picked")).not.toBe("");
  });
});

describe("the second-window rule", () => {
  it("never offers a session whose (repo, branch) another window's plan covers", () => {
    // Driven in testPlans.test.ts ("skips the second window on a branch …").
    expect(src("components/dialogs/verify.ts")).toContain(
      ".map((plan) => coverageKey(repoName(plan.repo_root), plan.branch))"
    );
  });

  it("names the window the checklist is under when a press lands on it", async () => {
    api.mockReset().mockResolvedValue({ plan: "bot7", existing: true });
    toast.mockReset();
    await writePlanFor("bot7-copy");
    expect(toast).toHaveBeenCalledWith(
      "bot7-copy's branch already has a checklist — it's under bot7 — it's in Verify"
    );
  });
});
