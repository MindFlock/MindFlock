/** The integration tidy (docs/vision/tidy.md): a Verify stop parked on you
 * moves on when its behaviors are answered — every Done that can reach it
 * opens its Behaviors card instead, and a Done the server refuses (409
 * `answer_behaviors`) does the same rather than an error card; a halted
 * Verify stop offers "Let the agent fix it"; the Verify tour opens once, the
 * first time a Behaviors stop shows on a route. */
import { describe, expect, it, vi } from "vitest";
import type { DiagramNode } from "../lib/flightDiagram";
import type { FlightStep } from "../lib/flights";
import type { StopMenuCtx } from "../lib/stopEditing";
import type { TestPlan } from "../api/types";

class FakeApiError extends Error {
  status: number;
  body: unknown;
  constructor(status: number, message: string, body: unknown) {
    super(message);
    this.status = status;
    this.body = body;
  }
}
const api = vi.fn();
vi.mock("../api/client", () => ({ api: (...a: unknown[]) => api(...a), ApiError: FakeApiError }));
const toast = vi.fn();
vi.mock("../lib/toast", () => ({ toast: (...a: unknown[]) => toast(...a) }));
const errorPop = vi.fn();
vi.mock("../lib/errorPop", () => ({ errorPop: (...a: unknown[]) => errorPop(...a) }));
const openBehaviors = vi.fn();
vi.mock("../lib/behaviorsFocus", () => ({ openBehaviors: (...a: unknown[]) => openBehaviors(...a) }));

const { menuItemsFor } = await import("../lib/stopEditing");
const { isAnswerBehaviors, stepDone, stepFix } = await import("../lib/flightsApi");
const { queryClient } = await import("../state/queries");
const { firstSightTourDue } = await import("../components/onboarding/useAutoTour");
const { behaviorsOnARoute } = await import("../lib/behaviors");
const { default: TOUR_SRC } = await import("../components/onboarding/replayTour.ts?raw");
const { default: SKY_SRC } = await import("../components/flights/SkyView.tsx?raw");
const { default: WAITING_SRC } = await import("../components/outbox/WaitingRow.tsx?raw");

const step = (kind: string): FlightStep => ({ id: "s1", kind, title: kind === "verify" ? "Verify" : "Approve", config: {}, on_fail: "halt" });
const node = (kind: string, state: DiagramNode["state"]): DiagramNode =>
  ({ id: "stop:s1", kind: "stop", row: "main", title: "x", sub: "", state, icon: kind, step: step(kind), index: 1 }) as DiagramNode;
const ctx: StopMenuCtx = { can: { edit: true }, live: true, here: true };
const keys = (n: DiagramNode) => menuItemsFor(n, ctx).map((i) => i.key);

describe("a stop's menu on a Verify stop", () => {
  it("parked on you: Behaviors in Done's place; any other stop keeps Done", () => {
    expect(keys(node("verify", "perched"))).toContain("behaviors");
    expect(keys(node("verify", "perched"))).not.toContain("done");
    expect(keys(node("you", "perched"))).toContain("done");
    expect(keys(node("you", "perched"))).not.toContain("behaviors");
  });

  it("halted: Retry, Let the agent fix it, Skip", () => {
    const items = menuItemsFor(node("verify", "halted"), ctx);
    expect(items.find((i) => i.key === "fix")?.label).toBe("Let the agent fix it");
    expect(items.map((i) => i.key)).toEqual(expect.arrayContaining(["retry", "fix", "skip"]));
    expect(keys(node("ship", "halted"))).toContain("fix"); // round 21: every halted stop
  });
});

describe("Done on a Verify stop the server refuses", () => {
  it("recognises 409 answer_behaviors, nothing else", () => {
    expect(isAnswerBehaviors(new FakeApiError(409, "answer its behaviors", { error: "answer_behaviors", detail: "…" }))).toBe(true);
    expect(isAnswerBehaviors(new FakeApiError(409, "x", { error: "in_progress" }))).toBe(false);
    expect(isAnswerBehaviors(new FakeApiError(500, "x", { error: "answer_behaviors" }))).toBe(false);
    expect(isAnswerBehaviors(new Error("x"))).toBe(false);
  });

  it("opens the session's behaviors instead of an error card", async () => {
    queryClient.setQueryData(["flights"], [{ id: "f1", session: "fix-login", branch: "fix-login" }]);
    api.mockReset().mockRejectedValue(new FakeApiError(409, "answer its behaviors", { error: "answer_behaviors" }));
    errorPop.mockReset();
    openBehaviors.mockReset();
    expect(await stepDone("f1", "s1")).toBe(false);
    expect(errorPop).not.toHaveBeenCalled();
    expect(openBehaviors).toHaveBeenCalledWith({ title: "fix-login", branch: "fix-login" });
    expect(String(toast.mock.calls.at(-1)?.[0])).toContain("Answer it on its Verify card");
  });

  it("any other refusal is still the error card", async () => {
    api.mockReset().mockRejectedValue(new FakeApiError(409, "not here", { error: "not_current" }));
    errorPop.mockReset();
    openBehaviors.mockReset();
    await stepDone("f1", "s1");
    expect(errorPop).toHaveBeenCalledWith("Done failed", "not here");
    expect(openBehaviors).not.toHaveBeenCalled();
  });

  it("Let the agent fix it posts the built-in fix op", async () => {
    api.mockReset().mockResolvedValue({});
    await stepFix("f 1", "s1");
    expect(api).toHaveBeenCalledWith("/api/flights/f%201/steps/s1/op/fix", { json: {} });
  });

  it("the bell's waiting row offers Behaviors, not Done, on a Verify stop", () => {
    expect(WAITING_SRC).toContain('fl.step.kind === "verify" ? (');
    expect(WAITING_SRC).toContain("onClick={() => openBehaviors({ title: w.title })}");
  });
});

describe("the Verify tour", () => {
  const plan = { id: "fix-login", branch: "fix-login", repo_root: "/r", steps: [], state: "ready" } as unknown as TestPlan;
  const row = { title: "fix-login", branch: "fix-login", repo: "r" };

  it("opens on the first Behaviors stop on a shown route, once", () => {
    expect(behaviorsOnARoute([plan], [row], ["fix-login"])).toBe(true);
    expect(behaviorsOnARoute([plan], [row], [])).toBe(false);
    expect(behaviorsOnARoute([], [row], ["fix-login"])).toBe(false);
    expect(firstSightTourDue({ shown: true, seen: [], id: "verify", busy: false })).toBe(true);
    expect(firstSightTourDue({ shown: true, seen: ["verify"], id: "verify", busy: false })).toBe(false);
    expect(firstSightTourDue({ shown: true, seen: [], id: "verify", busy: true })).toBe(false);
    expect(firstSightTourDue({ shown: false, seen: [], id: "verify", busy: false })).toBe(false);
    expect(SKY_SRC).toContain('useTourOnFirstSight("verify",');
  });

  it("its replay flies the Sky to the behaviors, not the retired dialog", () => {
    expect(TOUR_SRC).not.toContain('verify: "verify"');
    expect(TOUR_SRC).toContain("openBehaviors({ next: true });");
  });
});
