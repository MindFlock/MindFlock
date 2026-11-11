/** "Check another branch…" (the Behaviors board's ⋯): behaviors for a branch
 * no session has been on (POST /api/test-plans/branch; the route is pinned in
 * tests/unit/test_test_plans.py). The picker itself is BehaviorsSettings'
 * (behaviorsSettings.test.ts); the requests are lib/verifyActions'. */

import { describe, expect, it, vi } from "vitest";

const api = vi.fn();
const toast = vi.fn();
vi.mock("../api/client", async (orig) => ({ ...((await orig()) as object), api: (...a: unknown[]) => api(...a) }));
vi.mock("../lib/toast", () => ({ toast: (...a: unknown[]) => toast(...a) }));

const { fetchVerifyRepos } = await import("../lib/verifyActions");

describe("Another branch…", () => {
  it("reads the repo list from the server", async () => {
    api.mockReset().mockResolvedValue({ repos: [{ name: "shop", root: "/r/shop" }] });
    expect(await fetchVerifyRepos()).toEqual([{ name: "shop", root: "/r/shop" }]);
    expect(api).toHaveBeenCalledWith("/api/test-plans/repos");
  });

});
