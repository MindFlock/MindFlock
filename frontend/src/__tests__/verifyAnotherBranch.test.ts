/** "Check another branch…" (the Behaviors board's ⋯): behaviors for a branch
 * no session has been on (POST /api/test-plans/branch; the route is pinned in
 * tests/unit/test_test_plans.py). The picker itself is BehaviorsSettings'
 * (behaviorsSettings.test.ts); the requests are lib/verifyActions'. */

import { describe, expect, it, vi } from "vitest";

const api = vi.fn();
const toast = vi.fn();
vi.mock("../api/client", async (orig) => ({ ...((await orig()) as object), api: (...a: unknown[]) => api(...a) }));
vi.mock("../lib/toast", () => ({ toast: (...a: unknown[]) => toast(...a) }));

const { fetchVerifyRepos, writePlanForBranch } = await import("../lib/verifyActions");

describe("Another branch…", () => {
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
    expect(toast.mock.calls[0][0]).toContain("they'll appear in Verify");
  });

  it("says so when the branch already has one", async () => {
    api.mockReset().mockResolvedValue({ plan: "sc-7", existing: true });
    toast.mockReset();
    await writePlanForBranch({ name: "shop", root: "/r/shop" }, "feature/x");
    expect(toast.mock.calls[0][0]).toBe("feature/x already has behaviors — they're in Verify");
  });
});
