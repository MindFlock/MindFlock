/** The Behaviors board's ⋯ settings (components/behaviors/BehaviorsSettings.tsx,
 * docs/behaviors.md §3): the ONE "Write behaviors on every push" switch
 * (`repository.verify_enabled`), Watched repositories, "Check another
 * branch…" (POST /api/test-plans/branch) and "Start empty behaviors for a
 * session…" (POST /api/instances/{title}/test-plan {empty}). */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Instance, Json, TestPlan } from "../api/types";
import type { ChecklistTarget } from "../components/dialogs/verify";

const api = vi.fn();
const toast = vi.fn();
vi.mock("../api/client", async (orig) => ({ ...((await orig()) as object), api: (...a: unknown[]) => api(...a) }));
vi.mock("../lib/toast", () => ({ toast: (...a: unknown[]) => toast(...a) }));
const errorPop = vi.fn();
vi.mock("../lib/errorPop", () => ({ errorPop: (...a: unknown[]) => errorPop(...a) }));
let settings: Json = {};
let instances: Instance[] = [];
let plans: TestPlan[] = [];
// The real hooks poll (and read `document`); node has none.
vi.mock("../state/queries", async (orig) => ({
  ...((await orig()) as object),
  useSettingsDoc: () => ({ data: settings }),
  useInstances: () => ({ data: instances }),
  useTestPlans: () => ({ data: { plans, live_branch: "main" } }),
  useGithubConnected: () => ({ data: true }),
}));

const { BehaviorsSettings, emptyTargets, targetLabel, writesOnPush } = await import("../components/behaviors/BehaviorsSettings");
const { askBehaviorsForBranch, startEmptyBehaviors } = await import("../lib/verifyActions");

const render = () => renderToStaticMarkup(createElement(QueryClientProvider, { client: new QueryClient() }, createElement(BehaviorsSettings, {})));
const visible = (html: string) => html.replace(/<[^>]*>/g, " ").replace(/&[a-z#0-9]+;/g, " ");

beforeEach(() => {
  settings = {};
  instances = [];
  plans = [];
  api.mockReset();
  toast.mockReset();
});

describe("BehaviorsSettings renders", () => {
  it("the one switch, on by default", () => {
    const html = render();
    expect(html).toContain("Write behaviors on every push");
    expect(html).toMatch(/id="bh-push" aria-checked="true"[^>]*checked=""/);
    expect(html.match(/role="switch"/g)).toHaveLength(1);
  });

  it("the switch reads verify_enabled: false as off", () => {
    settings = { repository: { verify_enabled: false } };
    expect(render()).toMatch(/id="bh-push" aria-checked="false"/);
    expect(writesOnPush(settings)).toBe(false);
    expect(writesOnPush({})).toBe(true);
    expect(writesOnPush(null)).toBe(true);
  });

  it("Watched repositories: the list when there is one, and the way to add one", () => {
    expect(render()).toContain("None yet — behaviors are written for your sessions&#x27; branches.");
    settings = { repository: { verify_repos: ["acme/shop"], verify_repo_settings: { "acme/shop": { live_branch: "staging" } } } };
    const html = render();
    expect(html).toContain("Watched repositories");
    expect(html).toContain("<b>acme/shop</b>");
    expect(html).toContain("Live branch: staging");
    expect(html).toContain(">Watch a GitHub repository for every push</button>");
  });

  it("Check another branch… and Start empty behaviors for a session…", () => {
    const html = render();
    expect(html).toContain("Check another branch…</summary>");
    expect(html).toContain("Start empty behaviors for a session…</summary>");
    expect(html).toContain('placeholder="branch, e.g. feature/login"');
    // Nothing typed yet: both branch buttons wait.
    expect(html).toMatch(/<button[^>]*id="bh-branch-ask"[^>]*disabled=""/);
    expect(html).toMatch(/<button[^>]*id="bh-branch-empty"[^>]*disabled=""/);
  });

  it("never says checklist, test plan or step", () => {
    settings = { repository: { verify_repos: ["acme/shop"] } };
    expect(visible(render())).not.toMatch(/checklist|test plan|\bsteps?\b/i);
  });
});

describe("the session picker", () => {
  const t = (title: string, over: Partial<ChecklistTarget> = {}): ChecklistTarget => ({ title, branch: "feature/" + title, closed: false, reason: "", planId: "", ...over });

  it("offers sessions with no behaviors yet (a branch with no commits too)", () => {
    const list = [t("a"), t("b", { planId: "b", reason: "has behaviors" }), t("c", { reason: "no commits yet", noCommits: true }), t("d", { reason: "on main — nothing to compare" })];
    expect(emptyTargets(list).map((x) => x.title)).toEqual(["a", "c"]);
  });

  it("labels a closed one", () => {
    expect(targetLabel(t("a", { closed: true }))).toBe("feature/a — a (closed)");
    expect(targetLabel(t("main", { branch: "main" }))).toBe("main");
  });
});

describe("the requests", () => {
  it("Check another branch → the agent writes them", async () => {
    api.mockResolvedValue({ plan: "shop@feature/x", existing: false });
    expect(await askBehaviorsForBranch({ name: "shop", root: "/r/shop" }, "feature/x")).toBe("shop@feature/x");
    expect(api).toHaveBeenCalledWith("/api/test-plans/branch", { json: { repo: "/r/shop", branch: "feature/x" } });
    expect(toast).toHaveBeenCalledWith("Asked the agent to write behaviors for feature/x — up to three minutes");
  });

  it("Check another branch → Start empty", async () => {
    api.mockResolvedValue({ plan: "shop@feature/x", existing: false });
    await askBehaviorsForBranch({ name: "shop", root: "/r/shop" }, "feature/x", { empty: true });
    expect(api).toHaveBeenCalledWith("/api/test-plans/branch", { json: { repo: "/r/shop", branch: "feature/x", empty: true } });
    expect(toast).toHaveBeenCalledWith("Started empty behaviors for feature/x — add what it should do");
  });

  it("says so when the branch already has behaviors; \"\" when refused", async () => {
    api.mockResolvedValue({ plan: "sc-7", existing: true });
    expect(await askBehaviorsForBranch({ name: "shop", root: "/r/shop" }, "feature/x")).toBe("sc-7");
    expect(toast).toHaveBeenCalledWith("feature/x already has behaviors — they're on the Behaviors board");
    api.mockReset().mockRejectedValue(new Error("no such branch"));
    expect(await askBehaviorsForBranch({ name: "shop", root: "/r/shop" }, "nope")).toBe("");
    expect(errorPop).toHaveBeenCalledWith("Couldn't start behaviors for nope", "no such branch");
  });

  it("Start empty behaviors for a session", async () => {
    api.mockResolvedValue({ plan: "sc-1", existing: false });
    expect(await startEmptyBehaviors("sc-1")).toBe("sc-1");
    expect(api).toHaveBeenCalledWith("/api/instances/sc-1/test-plan", { method: "POST", json: { empty: true } });
    expect(toast).toHaveBeenCalledWith("Started empty behaviors for sc-1 — add what it should do");
  });
});
