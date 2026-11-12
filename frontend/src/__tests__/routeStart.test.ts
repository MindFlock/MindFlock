/** Routes on rails (lib/routeStart, RouteStartMenu, flightsApi): start a saved
 * route where an earlier run of it stopped, at any of its stops, and fly an
 * ended route again — the defaults, the words, and what each posts. */

import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Flight, FlightStep, StepKindDef } from "../lib/flights";

const calls: Array<[string, unknown]> = [];
const answers = new Map<string, unknown>();
let failWith: { status: number; body: unknown } | null = null;
vi.mock("../api/client", () => {
  class ApiError extends Error {
    status: number;
    body: unknown;
    constructor(status: number, message: string, body: unknown) {
      super(message);
      this.status = status;
      this.body = body;
    }
  }
  return {
    ApiError,
    api: vi.fn(async (path: string, opts?: { json?: unknown }) => {
      calls.push([path, opts?.json]);
      if (failWith && path.includes("/again")) {
        const b = failWith.body as { detail?: string } | null;
        throw new ApiError(failWith.status, b?.detail || "error", failWith.body);
      }
      return answers.get(path) ?? {};
    }),
  };
});
const toast = vi.fn();
const errorPop = vi.fn();
vi.mock("../lib/toast", () => ({ toast: (...a: unknown[]) => toast(...a) }));
vi.mock("../lib/errorPop", () => ({ errorPop: (...a: unknown[]) => errorPop(...a) }));

const RS = await import("../lib/routeStart");
const API = await import("../lib/flightsApi");
const { routeMenuLines } = await import("../components/flights/RouteStartMenu");
const { withAgainItem, AGAIN_HERE_KEY } = await import("../components/flights/useChainMenu");
const { flyAgainItems } = await import("../components/flights/FlightStrip");
const { queryClient } = await import("../state/queries");

const KINDS = [
  { kind: "agent", label: "Agent" },
  { kind: "ship", label: "Ship" },
  { kind: "wait_ci", label: "Wait for CI" },
] as unknown as StepKindDef[];

const step = (id: string, kind: string, title: string): FlightStep => ({ id, kind, title, config: {}, on_fail: "halt" }) as unknown as FlightStep;
const STEPS = [step("s1", "agent", "Build it"), step("s2", "ship", "Commit"), step("s3", "ship", "Push"), step("s4", "wait_ci", "CI green")];

function flight(over: Partial<Flight> = {}): Flight {
  return {
    id: "f_ab12cd",
    plan_id: "p1",
    plan_name: "Ship it",
    steps: STEPS,
    subject: {},
    session: "sc-42",
    repo_root: "/r",
    branch: "b",
    state: "cancelled",
    cursor: 2,
    step_states: { s1: { state: "done" }, s2: { state: "done" }, s3: { state: "running" }, s4: { state: "pending" } },
    reason: "",
    history: [],
    started: 1,
    updated: 2,
    finished: 3,
    by: "user",
    ...over,
  } as unknown as Flight;
}

const RESUME = { stop: "s3", index: 2, title: "Push", from_flight: "f_ab12cd", session: "sc-42", state: "cancelled" as const, reason: "its session was deleted", at: 1000 };
const PLAN = { id: "p1", name: "Ship it", steps: STEPS };

beforeEach(() => {
  calls.length = 0;
  answers.clear();
  failWith = null;
  toast.mockReset();
  errorPop.mockReset();
  RS.useRouteStartMenu.getState().set(null);
  queryClient.setQueryData(["instances"], [{ title: "sc-42" }, { title: "sc-7" }]);
});

describe("a start's default and its sentence", () => {
  it("starts at the resume stop when an earlier run stopped part way, else the normal start", () => {
    expect(RS.defaultStartAt(null)).toBe("");
    expect(RS.defaultStartAt({ resume: null })).toBe("");
    expect(RS.defaultStartAt({ resume: RESUME })).toBe("s3");
  });

  it("says where it carries on, when it stopped and why — in one line", () => {
    expect(RS.resumeSentence(RESUME, 1000 + 7200)).toBe("Carries on at Push — where it stopped 2h ago (its session was deleted)");
    expect(RS.resumeSentence({ ...RESUME, reason: "" }, 1000 + 300)).toBe("Carries on at Push — where it stopped 5m ago");
    expect(RS.resumeSentence({ ...RESUME, at: 0, reason: "" })).toBe("Carries on at Push — where it stopped");
  });
});

describe("the stops to start at", () => {
  it("numbers the route's own stops with their kind's label", () => {
    const c = RS.stopChoices(STEPS, KINDS);
    expect(c.map((x) => [x.id, x.n, x.label])).toEqual([
      ["s1", 1, "Agent"],
      ["s2", 2, "Ship"],
      ["s3", 3, "Ship"],
      ["s4", 4, "Wait for CI"],
    ]);
    expect(RS.choiceLine(c[2])).toBe("3. Push · Ship");
    expect(RS.choiceLine(RS.stopChoices([step("y", "you", "")], KINDS)[0])).toBe("1. Wait for me");
  });
});

describe("Fly again on an ended route", () => {
  it("is offered on done, cancelled and halted routes only", () => {
    for (const st of ["done", "cancelled", "halted"] as const) expect(RS.canFlyAgain(flight({ state: st }))).toBe(true);
    for (const st of ["running", "waiting_you", "paused", "ready", "grounded"] as const) expect(RS.canFlyAgain(flight({ state: st }))).toBe(false);
    expect(RS.canFlyAgain(flight({ free: true }))).toBe(false);
    expect(RS.canFlyAgain(flight({ edits_default: { kind: "tickets", target: "x" } as unknown as Flight["edits_default"] }))).toBe(false);
    expect(RS.canFlyAgain(flight({ steps: [] }))).toBe(false);
    expect(RS.canFlyAgain(null)).toBe(false);
  });

  it("says where it flies again from: the first stop it had not finished", () => {
    expect(RS.flyAgainLabel(flight())).toBe("Fly again from Push");
    // A skipped stop at the cursor was passed: the next one.
    const skipped = flight({ step_states: { s1: { state: "done" }, s2: { state: "done" }, s3: { state: "skipped" }, s4: { state: "pending" } } as unknown as Flight["step_states"] });
    expect(RS.reachedIndex(skipped)).toBe(3);
    expect(RS.flyAgainLabel(skipped)).toBe("Fly again from CI green");
    // A route that got past every stop starts over.
    const done = flight({ state: "done", cursor: 4, step_states: Object.fromEntries(STEPS.map((s) => [s.id, { state: "done" }])) as unknown as Flight["step_states"] });
    expect(RS.againStop(done)).toBeNull();
    expect(RS.flyAgainLabel(done)).toBe("Fly again");
  });

  it("is the primary action on a cancelled or halted route, not on a finished one", () => {
    expect(RS.flyAgainPrimary(flight({ state: "cancelled" }))).toBe(true);
    expect(RS.flyAgainPrimary(flight({ state: "halted" }))).toBe(true);
    expect(RS.flyAgainPrimary(flight({ state: "done" }))).toBe(false);
  });

  it("the route's ⋯ offers it twice: from where it stopped, and from another stop", () => {
    expect(flyAgainItems(flight()).map((i) => i.label)).toEqual(["Fly again from Push", "Fly again from another stop…"]);
    expect(flyAgainItems(flight({ state: "running" }))).toEqual([]);
  });

  it("a stop of a route that ended leads its menu with Fly again from here", () => {
    const n = { step: STEPS[1], title: "Commit" };
    expect(withAgainItem([], n, flight())[0]).toMatchObject({ key: AGAIN_HERE_KEY, label: "Fly again from here" });
    expect(withAgainItem([], n, flight({ state: "running" }))).toEqual([]);
    expect(withAgainItem([], { step: step("zz", "ship", "x"), title: "x" }, flight())).toEqual([]);
    expect(withAgainItem([], n, null)).toEqual([]);
  });

  it("toasts one sentence by how the server found its session", () => {
    expect(API.againText("same", "Push", "sc-42")).toBe("Flying again from Push");
    expect(API.againText("branch", "Push", "sc-7")).toBe("Flying again on sc-7, which holds its branch");
    expect(API.againText("reopened", "Push", "sc-42")).toBe("Reopened sc-42 and flying again from Push");
    expect(API.againText("restored", "Push", "sc-42")).toBe("Reopened sc-42: its route carried on as it was");
    expect(API.againText("chosen", "Push", "sc-7")).toBe("Flying again on sc-7 from Push");
  });
});

describe("what it posts", () => {
  it("the check posts the start's body; null when the server can't say", async () => {
    answers.set("/api/flights/check", { ok: true, steps: STEPS, resume: RESUME });
    const c = await API.checkStart({ plan_id: "p1", title: "sc-42" });
    expect(calls).toEqual([["/api/flights/check", { plan_id: "p1", title: "sc-42" }]]);
    expect(c?.resume?.stop).toBe("s3");
  });

  it("a start at a stop flies now (no start_on); the normal start is the body as it was", () => {
    expect(API.startBodyAt({ plan_id: "p", title: "t", start_on: "run" as const }, "s3")).toEqual({ plan_id: "p", title: "t", start_at: "s3" });
    expect(API.startBodyAt({ plan_id: "p", title: "t", start_on: "run" as const }, "")).toEqual({ plan_id: "p", title: "t", start_on: "run" });
    expect(API.startBodyAt({ plan_id: "p" } as import("../lib/flightsApi").StartBody, "resume")).toEqual({ plan_id: "p", start_at: "resume" });
  });

  it("Fly again posts {at?, title?} and opens its session with the toast", async () => {
    expect(API.againBody()).toEqual({});
    expect(API.againBody("s2", "sc-7")).toEqual({ at: "s2", title: "sc-7" });
    answers.set("/api/flights/f_ab12cd/again", { flight: flight({ id: "f_new", state: "running", cursor: 2 }), from: "f_ab12cd", session: "sc-42", how: "same" });
    const r = await API.flyAgain("f_ab12cd");
    expect(calls[0]).toEqual(["/api/flights/f_ab12cd/again", {}]);
    expect(r?.flight.id).toBe("f_new");
    expect(toast).toHaveBeenCalledWith("Flying again from Push");
    await API.flyAgain("f_ab12cd", "s2");
    expect(calls.find((c) => c[0].endsWith("/again") && (c[1] as { at?: string }).at)).toEqual(["/api/flights/f_ab12cd/again", { at: "s2" }]);
  });

  it("a refused Fly again shows the server's sentence as it comes", async () => {
    failWith = { status: 409, body: { error: "in_flight", detail: "this route is still going (running) — Carry on or Retry it instead", field: "" } };
    expect(await API.flyAgain("f_ab12cd")).toBeNull();
    expect(errorPop).toHaveBeenCalledWith("Flying it again failed", "this route is still going (running) — Carry on or Retry it instead");
  });

  it("the start toast says where a route started past its first stop", () => {
    expect(API.startedText({ plan_name: "Ship it", cursor: 0, steps: STEPS })).toBe("Started: Ship it");
    expect(API.startedText({ plan_name: "Ship it", cursor: 2, steps: STEPS })).toBe("Started: Ship it at Push");
  });
});

describe("starting a saved route on a session", () => {
  it("with nothing to carry on from, starts as it always did — one click, no menu", async () => {
    answers.set("/api/flights/check", { ok: true, steps: STEPS, resume: null });
    const normal = vi.fn();
    await RS.startSavedRoute(PLAN, "sc-42", { normal });
    expect(normal).toHaveBeenCalledTimes(1);
    expect(RS.useRouteStartMenu.getState().open).toBeNull();
  });

  it("an older server (no check) starts as always too", async () => {
    answers.set("/api/flights/check", null);
    const normal = vi.fn();
    await RS.startSavedRoute(PLAN, "sc-42", { normal });
    expect(normal).toHaveBeenCalledTimes(1);
  });

  it("with a resume point, the menu says where it carries on; its default starts there", async () => {
    answers.set("/api/flights/check", { ok: true, steps: STEPS, resume: RESUME });
    const normal = vi.fn();
    await RS.startSavedRoute(PLAN, "sc-42", { normal, at: { x: 40, y: 50 } });
    expect(normal).not.toHaveBeenCalled();
    const open = RS.useRouteStartMenu.getState().open!;
    expect(open).toMatchObject({ mode: "start", view: "resume", x: 40, y: 50 });
    const lines = routeMenuLines(open, KINDS, 1000 + 3600);
    expect(lines.map((l) => l.label)).toEqual([
      "Carries on at Push — where it stopped 1h ago (its session was deleted)",
      "Carry on at Push",
      "Start from the beginning",
      "Start at another stop…",
    ]);
    expect(lines.find((l) => l.primary)?.key).toBe("resume");
    lines.find((l) => l.key === "resume")!.run!();
    await Promise.resolve();
    expect(calls.at(-1)).toEqual(["/api/flights/start", { plan_id: "p1", title: "sc-42", start_at: "s3" }]);
    // Start from the beginning: the normal start.
    lines.find((l) => l.key === "fresh")!.run!();
    expect(normal).toHaveBeenCalledTimes(1);
  });

  it("Start at… lists the route's stops, the resume one marked and focused; stop 1 is the normal start", async () => {
    answers.set("/api/flights/check", { ok: true, steps: STEPS, resume: RESUME });
    const normal = vi.fn();
    await RS.startSavedRoute(PLAN, "sc-42", { normal, choose: true });
    const open = RS.useRouteStartMenu.getState().open!;
    expect(open).toMatchObject({ mode: "start", view: "pick" });
    const lines = routeMenuLines(open, KINDS);
    expect(lines.map((l) => l.label)).toEqual(["Start “Ship it” at…", "1. Build it · Agent", "2. Commit · Ship", "3. Push · Ship — where it stopped", "4. CI green · Wait for CI"]);
    expect(lines.find((l) => l.primary)?.key).toBe("stop:s3");
    lines[1].run!();
    expect(normal).toHaveBeenCalledTimes(1);
    lines[2].run!();
    await Promise.resolve();
    expect(calls.at(-1)).toEqual(["/api/flights/start", { plan_id: "p1", title: "sc-42", start_at: "s2" }]);
  });

  it("Start at… with no resume point defaults to the first stop and has no Back", async () => {
    answers.set("/api/flights/check", { ok: true, steps: STEPS, resume: null });
    await RS.startSavedRoute(PLAN, "sc-42", { normal: vi.fn(), choose: true });
    const lines = routeMenuLines(RS.useRouteStartMenu.getState().open!, KINDS);
    expect(lines.find((l) => l.primary)?.key).toBe("stop:s1");
    expect(lines.some((l) => l.key === "back")).toBe(false);
  });

  it("Fly again from another stop… lists the ended route's stops, where it stopped focused", () => {
    RS.pickAgainStop(flight());
    const lines = routeMenuLines(RS.useRouteStartMenu.getState().open!, KINDS);
    expect(lines.map((l) => l.label)).toEqual([
      "Fly “Ship it” again from…",
      "1. Build it · Agent — the beginning",
      "2. Commit · Ship",
      "3. Push · Ship — where it stopped",
      "4. CI green · Wait for CI",
    ]);
    expect(lines.find((l) => l.primary)?.key).toBe("stop:s3");
    lines[1].run!();
    expect(calls.at(-1)).toEqual(["/api/flights/f_ab12cd/again", { at: "s1" }]);
    lines[3].run!();
    expect(calls.at(-1)).toEqual(["/api/flights/f_ab12cd/again", {}]);
  });
});
