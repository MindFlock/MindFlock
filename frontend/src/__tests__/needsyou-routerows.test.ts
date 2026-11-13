/** Stability sweep, second pass: the bell's rows for a route (outbox/
 * WaitingRow's FlightYouRow / FlightHaltedRow / FlightPausedRow, built by
 * lib/needsYou `withFlightRows`) must only offer what the server will take.
 * The Sky's own rows decide the same things in components/flights/
 * FlightStrip.tsx (`skipCarriesOn`, `sessionGone`, `YouStepActions gone`). */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Instance, OutboxWaiting } from "../api/types";
import type { Flight, FlightStep } from "../lib/flights";

const calls: string[] = [];
vi.mock("../api/client", () => ({
  api: vi.fn(async (path: string) => {
    calls.push(path);
    return {};
  }),
  instApi: vi.fn(() => new Promise(() => {})),
  ApiError: class extends Error {},
}));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const { QueryClient, QueryClientProvider } = await import("@tanstack/react-query");
const { WaitingRow, skipPausedStop } = await import("../components/outbox/WaitingRow");
const { needsYouRows } = await import("../lib/needsYou");

(globalThis as Record<string, unknown>).document ??= {
  hidden: false,
  addEventListener: () => {},
  removeEventListener: () => {},
  getElementById: () => null,
  querySelector: () => null,
  dispatchEvent: () => true,
};

const NOW = Date.now() / 1000;
const step = (id: string, kind = "agent", extra: Partial<FlightStep> = {}): FlightStep =>
  ({ id, kind, title: "Stop " + id, config: {}, on_fail: "halt", ...extra }) as FlightStep;
const rec = (state: string, detail: Record<string, unknown> = {}) =>
  ({ state, since: 0, note: "", detail, rounds: 0 }) as Flight["step_states"][string];
function flight(extra: Partial<Flight>): Flight {
  return {
    id: "f_1",
    plan_id: "",
    plan_name: "Ticket to merge",
    steps: [step("s1"), step("s2"), step("s3")],
    state: "halted",
    cursor: 1,
    step_states: { s1: rec("done"), s2: rec("failed"), s3: rec("pending") },
    session: "sc-12",
    reason: "Stop s2 failed",
    subject: { kind: "manual", source: "", id: "", title: "", url: "" },
    ...extra,
  } as unknown as Flight;
}
const waiting = (kind: string, f: Flight, title = ""): OutboxWaiting =>
  ({
    key: "flight:" + f.id,
    title,
    kind,
    reason: "",
    text: f.plan_name,
    actions: [],
    flight: { id: f.id, step_id: f.steps[f.cursor].id, step_title: f.steps[f.cursor].title },
  }) as unknown as OutboxWaiting;

function render(w: OutboxWaiting, f: Flight, rows: Array<Partial<Instance>> | undefined, row?: Instance) {
  const c = new QueryClient({ defaultOptions: { queries: { retry: false, enabled: false } } });
  c.setQueryData(["flights"], [f]);
  if (rows) c.setQueryData(["instances"], rows);
  return renderToStaticMarkup(
    createElement(QueryClientProvider, { client: c }, createElement(WaitingRow, { w, row, shown: (t: string) => t, group: "" }))
  );
}

beforeEach(() => {
  calls.length = 0;
});

describe("Skip on a route paused at a halt point carries on only where the Sky does", () => {
  const paused = (extra: Partial<Flight>) => flight({ state: "paused", reason: "halt point", step_states: { s1: rec("done"), s2: rec("pending"), s3: rec("pending") }, ...extra });

  it("carries on when there is somewhere to go", async () => {
    const f = paused({});
    await skipPausedStop({ id: f.id, step: { id: "s2" } }, f);
    expect(calls.some((p) => p.endsWith("/skip"))).toBe(true);
    expect(calls.some((p) => p.endsWith("/resume"))).toBe(true);
  });

  it("not into a second halt point", async () => {
    const f = paused({ steps: [step("s1"), step("s2"), step("s3", "agent", { halt: true } as Partial<FlightStep>)] });
    await skipPausedStop({ id: f.id, step: { id: "s2" } }, f);
    expect(calls.some((p) => p.endsWith("/skip"))).toBe(true);
    expect(calls.some((p) => p.endsWith("/resume"))).toBe(false);
  });

  it("not past the last stop (Carry on failed — already finished)", async () => {
    const f = paused({ cursor: 2, step_states: { s1: rec("done"), s2: rec("done"), s3: rec("pending") } });
    await skipPausedStop({ id: f.id, step: { id: "s3" } }, f);
    expect(calls.some((p) => p.endsWith("/resume"))).toBe(false);
  });
});

describe("a route with no session row offers what its stop's real kind allows", () => {
  it("a Verify stop: Verify, not a Done the server refuses", () => {
    const f = flight({ state: "waiting_you", steps: [step("s1"), step("s2", "verify"), step("s3")], step_states: { s1: rec("done"), s2: rec("running"), s3: rec("pending") }, session: "" });
    const html = render(waiting("flight_you", f), f, []);
    expect(html).toContain(">Verify<");
    expect(html).not.toContain(">Done<");
  });
});

describe("a route whose session is closed can be ended from the bell", () => {
  const f = flight({});
  it("session open: no Stop the route", () => {
    const row = { title: "sc-12" } as Instance;
    const html = render(waiting("flight_halted", f, "sc-12"), f, [row], row);
    expect(html).toContain(">Retry<");
    expect(html).not.toContain("Stop the route");
  });

  it("session closed: says so and offers Stop the route", () => {
    const html = render(waiting("flight_halted", f), f, [{ title: "other" }]);
    expect(html).toContain(">Stop the route<");
    expect(html).toContain("Its session is closed — end this route");
    expect(html).toContain("sc-12 is closed");
  });

  it("a waiting route too", () => {
    const g = flight({ state: "waiting_you", steps: [step("s1"), step("s2", "you"), step("s3")], step_states: { s1: rec("done"), s2: rec("running"), s3: rec("pending") } });
    const html = render(waiting("flight_you", g), g, [{ title: "other" }]);
    expect(html).toContain(">Stop the route<");
  });

  it("rows still loading: not taken for closed", () => {
    expect(render(waiting("flight_halted", f), f, undefined)).not.toContain("Stop the route");
  });
});

describe("a route on another device is no row with buttons that post here", () => {
  const remote = (state: string, kind = "you") =>
    ({
      title: "mac::sc-1",
      device: "mac",
      status: "running",
      activity: "idle",
      stage: "agent",
      branch: "",
      activity_since: NOW - 60,
      flight: { id: "f_remote", plan_name: "Ticket to PR", state, cursor: 1, total: 3, step: { id: "s2", kind, title: "Wait for me: Try it", state: "running", actor: kind === "you" ? "you" : "robot" } },
    }) as unknown as Instance;
  const rows = (inst: Instance) => needsYouRows({ instances: [inst], waiting: [], flights: [], testPlans: [] });

  it("waiting on you: still listed (as the session's own row), with no route buttons", () => {
    const r = rows(remote("waiting_you"));
    expect(r.map((x) => [x.title, x.kind])).toEqual([["mac::sc-1", "wait_for_me"]]);
    expect(r[0].waiting).toBeUndefined();
  });

  it("halted: still listed as halted, with no route buttons", () => {
    const r = rows(remote("halted", "agent"));
    expect(r.map((x) => [x.title, x.kind])).toEqual([["mac::sc-1", "halted"]]);
    expect(r[0].waiting).toBeUndefined();
  });

  it("a shared peer row the same", () => {
    const peer = { ...remote("waiting_you"), device: undefined, title: "ana::sc-1" } as unknown as Instance;
    expect(rows(peer)[0].waiting).toBeUndefined();
  });
});
