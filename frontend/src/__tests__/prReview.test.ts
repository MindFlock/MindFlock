/** "Run PR review" (owner request 2026-11-11): run the team's saved PR review
 * route on an open PR, even while the session's agent is there
 * (lib/prReview.ts → POST /api/pr-review). Pinned here:
 *
 *  - the request body and the words for each answer (started / inserted /
 *    created) and each error, the 400 naming where the route is chosen;
 *  - an older server's bare 404 hides the action;
 *  - the ⋯ menus offer it on a session with a PR that isn't paused;
 *  - Intake → GitHub PRs: the row's action calls the route with `pr_url`;
 *  - Settings → Workspace: the PR review route picker over the Sky's plans.
 *
 * vitest is node-only: models and helpers, and renderToStaticMarkup. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Instance, PrReviewResult } from "../api/types";
import type { Flight } from "../lib/flights";
import { code, fn } from "./sourceText";

const api = vi.fn();
const toast = vi.fn();
const errorPop = vi.fn();
const selected: string[] = [];
vi.mock("../api/client", async (orig) => ({ ...((await orig()) as object), api: (...a: unknown[]) => api(...a) }));
vi.mock("../lib/toast", () => ({ toast: (...a: unknown[]) => toast(...a) }));
vi.mock("../lib/errorPop", () => ({ errorPop: (...a: unknown[]) => errorPop(...a) }));
vi.mock("../lib/sessionActions", async (orig) => ({
  ...((await orig()) as object),
  selectSession: (t: string) => selected.push(t),
}));
vi.mock("../state/queries", async (orig) => ({
  ...((await orig()) as object),
  refreshInstances: () => Promise.resolve(),
}));

const { ApiError } = await import("../api/client");
const P = await import("../lib/prReview");
const { canRunPrReview, prReviewMenuItem, withPrReviewItem } = await import("../components/sidebar/prReviewMenu");
const { PrReviewAction, reviewOpenPr } = await import("../components/intake/PrReviewAction");
const { useUi } = await import("../state/store");

const PR = "https://github.com/acme/shop/pull/42";
const row = (o: Partial<Instance> = {}): Partial<Instance> => ({ title: "sc-1", status: "running", branch: "feature/sc-1", pr_url: PR, ...o });
const result = (o: Partial<PrReviewResult> = {}): PrReviewResult => ({
  mode: "started",
  session: "sc-1",
  flight: "f1",
  stops: [{ id: "r1", kind: "coderabbit", title: "CodeRabbit" }],
  ...o,
});
const flight = (steps: Array<{ id: string; title: string }>): Flight => ({ id: "f1", steps } as unknown as Flight);
const routeError = (status: number, error = "") => new ApiError(status, error || "x", error ? { error } : { detail: "Not Found" });

beforeEach(() => {
  api.mockReset();
  toast.mockReset();
  errorPop.mockReset();
  selected.length = 0;
  P.resetPrReviewSupport();
});

describe("the request", () => {
  it("POSTs only the fields given, pr_url for a link", async () => {
    expect(P.prReviewBody({ session: "sc-1" })).toEqual({ session: "sc-1" });
    expect(P.prReviewBody({ prUrl: PR, plan: "" })).toEqual({ pr_url: PR });
    expect(P.prReviewBody({ session: "a", prUrl: PR, plan: "team-review" })).toEqual({ session: "a", pr_url: PR, plan: "team-review" });
    api.mockResolvedValue(result());
    await P.runPrReview({ prUrl: PR });
    expect(api).toHaveBeenCalledWith("/api/pr-review", { method: "POST", json: { pr_url: PR } });
  });
});

describe("the words", () => {
  it("says what each answer did", () => {
    expect(P.prReviewMessage(result())).toBe("PR review started on sc-1");
    expect(P.prReviewMessage(result({ mode: "inserted" }), "Ship")).toBe("Review steps added to sc-1's route after Ship");
    expect(P.prReviewMessage(result({ mode: "inserted" }))).toBe("Review steps added to sc-1's route after the current stop");
    expect(P.prReviewMessage(result({ mode: "created", session: "pr-shop-42" }))).toBe("Opened pr-shop-42 for the PR and started its review");
  });

  it("names the stop the review went in after", () => {
    const f = flight([{ id: "a", title: "Agent" }, { id: "s", title: "Open PR" }, { id: "r1", title: "CodeRabbit" }, { id: "m", title: "Merge" }]);
    expect(P.stopBefore([f], "f1", result().stops)).toBe("Open PR");
    expect(P.stopBefore([f], "other", result().stops)).toBe("");
    expect(P.stopBefore(null, "f1", result().stops)).toBe("");
    expect(P.stopBefore([flight([{ id: "r1", title: "CodeRabbit" }])], "f1", result().stops)).toBe("");
  });

  it("a 400 says where to choose the review route", () => {
    expect(P.prReviewErrorText(routeError(400, "No PR review route is configured"))).toBe(
      "No PR review route is configured. Choose a review route in Settings → Workspace → PR route."
    );
    expect(P.prReviewErrorText(new ApiError(400, "x", null))).toContain("Choose a review route in " + P.PR_REVIEW_SETTING);
  });

  it("a 409 or the action's own 404 says why in plain words", () => {
    expect(P.prReviewErrorText(routeError(409, "sc-1 is paused — resume it first"))).toBe("sc-1 is paused — resume it first");
    expect(P.prReviewErrorText(new ApiError(409, "x", null))).toMatch(/paused or held — resume it/);
    expect(P.prReviewErrorText(routeError(404, "No open PR at that link"))).toBe("No open PR at that link");
    expect(P.prReviewErrorText(routeError(404))).toMatch(/can't run PR reviews yet — update it/);
  });
});

describe("starting it", () => {
  it("toasts the message and opens the session's Agent tab", async () => {
    api.mockResolvedValue(result());
    expect(await P.startPrReview({ session: "sc-1" })).toMatchObject({ mode: "started" });
    expect(toast).toHaveBeenCalledWith("PR review started on sc-1");
    expect(selected).toEqual(["sc-1"]);
    expect(useUi.getState().lastTab?.["sc-1"]).toBe("agent");
  });

  it("an insert reads the route to name the current stop", async () => {
    api.mockImplementation((path: string) =>
      Promise.resolve(
        path === "/api/pr-review"
          ? result({ mode: "inserted" })
          : { flights: [flight([{ id: "a", title: "Agent" }, { id: "r1", title: "CodeRabbit" }])] }
      )
    );
    await P.startPrReview({ session: "sc-1" }, { open: false });
    expect(toast).toHaveBeenCalledWith("Review steps added to sc-1's route after Agent");
    expect(selected).toEqual([]);
  });

  it("no route chosen: an error card with a way to Settings", async () => {
    api.mockRejectedValue(routeError(400, "No PR review route is configured"));
    expect(await P.startPrReview({ session: "sc-1" })).toBeNull();
    const [title, detail, actions] = errorPop.mock.calls[0];
    expect(title).toBe("Couldn't run the PR review on sc-1");
    expect(detail).toContain("Choose a review route in Settings → Workspace → PR route");
    expect(actions[0]).toMatchObject({ label: "Choose a route", primary: true });
    expect(P.prReviewSupported()).toBe(true);
  });

  it("an older server (no such route) hides the action from then on", async () => {
    expect(canRunPrReview(row())).toBe(true);
    api.mockRejectedValue(routeError(404));
    await P.startPrReview({ session: "sc-1" });
    expect(P.prReviewSupported()).toBe(false);
    expect(canRunPrReview(row())).toBe(false);
    expect(renderToStaticMarkup(createElement(PrReviewAction, { prUrl: PR, reference: "#42" }))).toBe("");
  });
});

describe("the ⋯ menus", () => {
  it("offer it on a session with a PR that isn't paused", () => {
    expect(canRunPrReview(row())).toBe(true);
    expect(canRunPrReview(row({ activity: "working" } as Partial<Instance>))).toBe(true);
    expect(canRunPrReview(row({ pr_url: null }))).toBe(false);
    expect(canRunPrReview(row({ status: "paused" }))).toBe(false);
    expect(canRunPrReview(row({ device: "mac-mini" } as Partial<Instance>))).toBe(false);
    expect(canRunPrReview(row({ workspace_missing: true }))).toBe(false);
    expect(prReviewMenuItem(row())).toMatchObject({ key: "pr-review", label: "Run PR review" });
  });

  it("sits after Verify in Ship, and runs the review on the session", async () => {
    const item = (key: string, label: string) => ({ key, label, run() {} });
    const model = { groups: [{ id: "ship" as const, label: "Ship", items: [item("push", "Push"), item("verify", "Verify"), item("checklist", "Behaviors…")] }], more: [] };
    const labels = (inst: Partial<Instance>) => withPrReviewItem(model, inst).groups[0].items.map((i) => i.label);
    expect(labels(row())).toEqual(["Push", "Verify", "Run PR review", "Behaviors…"]);
    expect(labels(row({ status: "paused" }))).toEqual(["Push", "Verify", "Behaviors…"]);
    const noVerify = { ...model, groups: [{ ...model.groups[0], items: [item("push", "Push"), item("checklist", "Behaviors…")] }] };
    expect(withPrReviewItem(noVerify, row()).groups[0].items.map((i) => i.label)).toEqual(["Push", "Run PR review", "Behaviors…"]);

    api.mockResolvedValue(result());
    prReviewMenuItem(row())!.run();
    await vi.waitFor(() => expect(toast).toHaveBeenCalled());
    expect(api).toHaveBeenCalledWith("/api/pr-review", { method: "POST", json: { session: "sc-1" } });
  });

  it("both the rail row's and the pane's menu carry it", () => {
    expect(code("components/sidebar/verifyMenu.ts")).toContain("withPrReviewItem(withVerifyItem(baseSessionMenu(...args), args[0]), args[0])");
    expect(code("components/grid/Pane.tsx")).toMatch(/withPrReviewItem\(\s*withVerifyItem\(/);
  });
});

describe("Intake → GitHub PRs", () => {
  it("each open PR row has Run PR review, which calls the route with pr_url", async () => {
    expect(code("components/intake/PullRequestsTab.tsx")).toContain(
      'actionExtra={<PrReviewAction prUrl={p.url} reference={"#" + p.number} />}'
    );
    const html = renderToStaticMarkup(createElement(PrReviewAction, { prUrl: PR, reference: "#42" }));
    expect(html).toContain("Run PR review");
    expect(html).toContain('data-pr-review="' + PR + '"');
    expect(renderToStaticMarkup(createElement(PrReviewAction, { reference: "#42" }))).toBe("");

    api.mockResolvedValue(result({ mode: "created", session: "pr-shop-42" }));
    expect(await reviewOpenPr(PR)).toBe(true);
    expect(api).toHaveBeenCalledWith("/api/pr-review", { method: "POST", json: { pr_url: PR } });
    expect(toast).toHaveBeenCalledWith("Opened pr-shop-42 for the PR and started its review");
    expect(selected).toEqual(["pr-shop-42"]);
  });
});

describe("Settings → Workspace → PR route", () => {
  it("picks a saved route from the Sky's plans, stored in flights.pr_review_plan", () => {
    const ws = code("components/settings/screens/Workspace.tsx");
    expect(ws).toContain("<PrReviewRoute />");
    expect(ws).toContain("useFlightPlans()");
    expect(ws).toContain('s.saveField("flights", "pr_review_plan", e.target.value)');
    expect(ws).toContain('id="workspace-pr-review-route"');
    // Blank is the built-in default (named), "off" is no route, and the hint
    // says automatically picked-up PRs fly it too (backend core/pr_route.py).
    expect(ws).toContain('label: "Default · " + builtinName + " (built-in)"');
    expect(ws).toContain("value: PR_ROUTE_OFF");
    expect(fn("components/settings/screens/Workspace.tsx", "PrReviewRoute")).toContain("picked up <strong>automatically</strong>");
  });

  it("the copy stays in the app's words", () => {
    const copy = [
      code("lib/prReview.ts"),
      code("components/intake/PrReviewAction.tsx"),
      fn("components/sidebar/prReviewMenu.ts", "prReviewMenuItem"),
      fn("components/settings/screens/Workspace.tsx", "PrReviewRoute"),
    ]
      .join("\n")
      .toLowerCase();
    expect(copy).toContain("run pr review");
    for (const word of ["chain", "flyway", "perch", "fast-track", "checklist", "test plan", "changelog"])
      expect(copy, word).not.toContain(word);
  });
});
