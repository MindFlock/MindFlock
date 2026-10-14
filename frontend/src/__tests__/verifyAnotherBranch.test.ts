/** Verify → New checklist → "Another branch…": a checklist for a branch no
 * session has been on (POST /api/test-plans/branch; the route is pinned in
 * tests/unit/test_test_plans.py). */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type { ChecklistTarget } from "../components/dialogs/verify";

const api = vi.fn();
const toast = vi.fn();
vi.mock("../api/client", async (orig) => ({ ...((await orig()) as object), api: (...a: unknown[]) => api(...a) }));
vi.mock("../lib/toast", () => ({ toast: (...a: unknown[]) => toast(...a) }));

const { ANOTHER_BRANCH, VerifyNewChecklist } = await import("../components/dialogs/VerifyChrome");
const { fetchVerifyRepos, writePlanForBranch } = await import("../lib/verifyActions");

const render = (targets: ChecklistTarget[]) =>
  renderToStaticMarkup(
    createElement(VerifyNewChecklist, {
      targets,
      reason: "",
      plans: [],
      open: true,
      onToggle() {},
      onReveal() {},
    })
  );

const SESSION: ChecklistTarget = { title: "sc-1", branch: "feature/a", closed: false, reason: "", planId: "" };

describe("Another branch…", () => {
  it("is the last choice in Ask the agent's picker", () => {
    const html = render([SESSION]);
    expect(html).toContain('<option value="' + ANOTHER_BRANCH + '">Another branch…</option>');
    // A session is picked by default, so the branch form stays out of the way.
    expect(html).not.toContain('id="vf-newplan-branch"');
  });

  it("is the default when there is no session to pick, with a repo and a branch box", () => {
    const html = render([]);
    expect(html).toContain('<option value="' + ANOTHER_BRANCH + '" selected="">Another branch…</option>');
    expect(html).toContain('id="vf-newplan-repo"');
    expect(html).toContain('id="vf-newplan-branch"');
    expect(html).toContain('placeholder="branch, e.g. feature/login"');
    // Nothing typed yet: the button waits.
    expect(html).toMatch(/<button[^>]*id="vf-newplan-go"[^>]*disabled=""/);
  });

  it("reads the repo list from the server", async () => {
    api.mockReset().mockResolvedValue({ repos: [{ name: "shop", root: "/r/shop" }] });
    expect(await fetchVerifyRepos()).toEqual([{ name: "shop", root: "/r/shop" }]);
    expect(api).toHaveBeenCalledWith("/api/test-plans/repos");
  });

  it("posts the repo's root and the branch, and says where it will appear", async () => {
    api.mockReset().mockResolvedValue({ plan: "shop@feature/x", existing: false });
    toast.mockReset();
    expect(await writePlanForBranch({ name: "shop", root: "/r/shop" }, "feature/x")).toBe(true);
    expect(api).toHaveBeenCalledWith("/api/test-plans/branch", {
      json: { repo: "/r/shop", branch: "feature/x" },
    });
    expect(toast.mock.calls[0][0]).toContain("it'll appear in Verify");
  });

  it("says so when the branch already has one", async () => {
    api.mockReset().mockResolvedValue({ plan: "sc-7", existing: true });
    toast.mockReset();
    await writePlanForBranch({ name: "shop", root: "/r/shop" }, "feature/x");
    expect(toast.mock.calls[0][0]).toBe("feature/x already has a checklist — it's in Verify");
  });
});
