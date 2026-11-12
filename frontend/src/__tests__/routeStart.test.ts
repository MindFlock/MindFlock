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
const { flyAgainItems, sessionGone } = await import("../components/flights/FlightStrip");
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

describe("a route's own stops (not the checks perches it added itself)", () => {
  const perch = step("p9", "you", "Your checks");
  const mid = step("p5", "you", "Your checks");

  it("leaves out owed_perch and owed_added from the stops and the labels", () => {
    const f = flight({ steps: [...STEPS.slice(0, 3), mid, STEPS[3], perch], owed_perch: "p9", owed_added: ["p5"] });
    expect(RS.ownSteps(f).map((s) => s.id)).toEqual(["s1", "s2", "s3", "s4"]);
    RS.pickAgainStop(f);
    const lines = routeMenuLines(RS.useRouteStartMenu.getState().open!, KINDS);
    expect(lines.some((l) => l.label.includes("Your checks"))).toBe(false);
  });

  it("standing on its end perch = past every own stop: Fly again starts over", () => {
    const states = Object.fromEntries(STEPS.map((s) => [s.id, { state: "done" }]));
    const f = flight({ state: "cancelled", steps: [...STEPS, perch], cursor: 4, owed_perch: "p9", step_states: { ...states, p9: { state: "perched" } } as unknown as Flight["step_states"] });
    expect(RS.carryIndex(f)).toBe(4);
    expect(RS.flyAgainLabel(f)).toBe("Fly again");
  });

  it("standing on a perch it added mid-route = the next own stop", () => {
    const f = flight({
      steps: [...STEPS.slice(0, 3), mid, STEPS[3]],
      cursor: 3,
      owed_added: ["p5"],
      step_states: { s1: { state: "done" }, s2: { state: "done" }, s3: { state: "done" }, p5: { state: "perched" }, s4: { state: "pending" } } as unknown as Flight["step_states"],
    });
    expect(RS.flyAgainLabel(f)).toBe("Fly again from CI green");
  });

  it("never past a Verify stop whose check it still owed: the earliest of those", () => {
    const f = flight({ cursor: 3, step_states: { s1: { state: "done" }, s2: { state: "done" }, s3: { state: "done" }, s4: { state: "pending" } } as unknown as Flight["step_states"], owed: [{ step: "s2", title: "Commit", plan: "", items: ["a"] }] });
    expect(RS.carryIndex(f)).toBe(1);
    expect(RS.flyAgainLabel(f)).toBe("Fly again from Commit");
  });

  it("a route whose only stops are its own perches can't fly again", () => {
    expect(RS.canFlyAgain(flight({ steps: [perch], owed_perch: "p9" }))).toBe(false);
  });
});

describe("what Fly again says it will do", () => {
  it("its session alive: a new run there; gone: the branch's session or a reopen", () => {
    expect(RS.flyAgainTitle(flight(), false)).toBe("Fly this route again on sc-42 from “Push” — a new run of its stops");
    expect(RS.flyAgainTitle(flight(), true)).toBe("sc-42 is gone: it flies again from “Push” on the session that holds its branch, or reopens sc-42 from Recently closed");
    expect(sessionGone(flight(), [{ title: "sc-42" }])).toBe(false);
    expect(sessionGone(flight(), [{ title: "other" }])).toBe(true);
    expect(sessionGone(flight(), undefined)).toBe(false);
    expect(flyAgainItems(flight(), true)[0].title).toContain("is gone");
  });
});

describe("where the menu opens", () => {
  const view = { w: 1000, h: 800 };
  const size = { w: 340, h: 200 };
  it("beside what opened it, to its right, top-aligned — never over it", () => {
    expect(RS.placeMenu({ left: 100, top: 300, right: 220, bottom: 330 }, size, view)).toEqual({ x: 226, y: 300 });
  });
  it("to its left when the right has no room (a drawer at the edge)", () => {
    expect(RS.placeMenu({ left: 700, top: 300, right: 900, bottom: 330 }, size, view)).toEqual({ x: 354, y: 300 });
  });
  it("pulled up to stay on screen near the bottom", () => {
    expect(RS.placeMenu({ left: 100, top: 700, right: 220, bottom: 730 }, size, view)).toEqual({ x: 226, y: 592 });
  });
  it("below it when neither side has room, above when below has none", () => {
    const narrow = { w: 400, h: 800 };
    expect(RS.placeMenu({ left: 20, top: 100, right: 380, bottom: 130 }, size, narrow)).toEqual({ x: 20, y: 136 });
    expect(RS.placeMenu({ left: 20, top: 700, right: 380, bottom: 730 }, size, narrow)).toEqual({ x: 20, y: 494 });
  });
  it("a point (a drop, a ⋯ item) opens just beside it", () => {
    expect(RS.anchorOf(null, { x: 50, y: 60 })).toEqual({ left: 50, top: 60, right: 50, bottom: 60 });
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
    expect(open).toMatchObject({ mode: "start", view: "resume", anchor: { left: 40, top: 50, right: 40, bottom: 50 } });
    const lines = routeMenuLines(open, KINDS, 1000 + 3600);
    expect(lines.map((l) => l.label)).toEqual([
      "Carries on at Push — where it stopped 1h ago (its session was deleted)",
      "Carry on at Push",
      "Start from the beginning",
      "Start at another stop…",
    ]);
    expect(lines.find((l) => l.primary)?.key).toBe("resume");
    expect(lines[1].title).not.toContain("nothing is sent again");
    const started = open as Extract<typeof open, { mode: "start" }>;
    RS.useRouteStartMenu.getState().set({ ...started, check: { ...started.check!, resume: { ...RESUME, in_progress: true } } });
    expect(routeMenuLines(RS.useRouteStartMenu.getState().open!, KINDS)[1].title).toContain("nothing is sent again");
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
    // Each stop is drawn as parts on one row: number, title, kind, mark.
    expect(lines[3].stop).toMatchObject({ choice: { n: 3, title: "Push", label: "Ship" }, mark: "where it stopped" });
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
