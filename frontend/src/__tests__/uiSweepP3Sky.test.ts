/** UI sweep round 1, package P3 (the Sky and routes):
 *  #9  a step or control action refetches the flights alone (no saved routes,
 *      no rows: the `flight.changed` event refetches those);
 *  #13 a Sky line counts its finished stops off the diagram it already drew;
 *  #18 the Sky's toolbar says so while the routes can't be read;
 *  #22 "Stop the route" on a closed session's strip asks first;
 *  #26 a Sky line's ⋯ has the route's Pause / Carry on, Stop the route, Fly again;
 *  #35 the `?` sheet lists Ctrl+K N and Alt+V. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Diagram, DiagramNode } from "../lib/flightDiagram";
import type { Flight, FlightStep } from "../lib/flights";
import { code, css, cssRule } from "./sourceText";

const api = vi.fn((..._args: unknown[]): Promise<unknown> => Promise.resolve({}));
vi.mock("../api/client", () => ({ api: (...a: unknown[]) => api(...a), instApi: vi.fn(() => new Promise(() => {})), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));
vi.mock("../lib/errorPop", () => ({ errorPop: vi.fn() }));

const { QueryClient, QueryClientProvider } = await import("@tanstack/react-query");
const { queryClient } = await import("../state/queries");
const flightsApi = await import("../lib/flightsApi");
const { hasFlownToFold, lineRouteItems } = await import("../components/flights/SkyLine");
const { SKY_FLIGHTS_UNREACHABLE, skyFlightsNote } = await import("../components/flights/SkyView");
const { YouStepActions, routeMenuItems } = await import("../components/flights/FlightStrip");
const { collapseFlown, flownCount } = await import("../lib/flightDiagram");
const { SKY_SHORTCUT_GROUPS } = await import("../lib/skyShortcuts");

(globalThis as Record<string, unknown>).document ??= {
  hidden: false,
  addEventListener: () => {},
  removeEventListener: () => {},
  getElementById: () => null,
  querySelector: () => null,
  dispatchEvent: () => true,
};

const step = (id: string): FlightStep => ({ id, kind: "agent", title: "Stop " + id, config: {}, on_fail: "halt" }) as FlightStep;
const rec = (state: string) => ({ state, since: 0, note: "", detail: {}, rounds: 0 }) as Flight["step_states"][string];
function flight(extra: Partial<Flight> = {}): Flight {
  return {
    id: "f_1",
    plan_id: "",
    plan_name: "Ticket to merge",
    steps: [step("s1"), step("s2"), step("s3")],
    state: "running",
    cursor: 1,
    step_states: { s1: rec("done"), s2: rec("running"), s3: rec("pending") },
    session: "me",
    reason: "",
    subject: { kind: "manual", source: "", id: "", title: "", url: "" },
    ...extra,
  } as unknown as Flight;
}

// --- #9 -------------------------------------------------------------------------------

describe("#9 a step or control action refetches the flights alone", () => {
  let keys: string[];
  beforeEach(() => {
    keys = [];
    api.mockClear();
    vi.spyOn(queryClient, "invalidateQueries").mockImplementation(((f?: { queryKey?: unknown[] }) => {
      keys.push(JSON.stringify(f?.queryKey));
      return Promise.resolve();
    }) as typeof queryClient.invalidateQueries);
  });

  it("Skip, Retry, Jump, Pause, Carry on, Stop the route, Let the agent fix it", async () => {
    await flightsApi.stepSkip("f_1", "s2");
    await flightsApi.stepRetry("f_1", "s2");
    await flightsApi.jumpFlight("f_1", "s3");
    await flightsApi.controlFlight("f_1", "pause");
    await flightsApi.controlFlight("f_1", "resume");
    await flightsApi.controlFlight("f_1", "cancel");
    await flightsApi.stepFix("f_1", "s2");
    expect(new Set(keys)).toEqual(new Set(['["flights"]']));
    expect(keys).toHaveLength(7);
  });

  it("Done too, and a failed action still refetches the flights", async () => {
    await flightsApi.stepDone("f_1", "s2");
    api.mockImplementationOnce(() => Promise.reject(new Error("nope")));
    await flightsApi.stepSkip("f_1", "s2");
    expect(keys).toEqual(['["flights"]', '["flights"]']);
  });

  it("a plan edit still refreshes the saved routes", async () => {
    await flightsApi.refreshFlights();
    expect(keys).toEqual(['["flight-plans"]', '["flights"]']);
  });

  it("the rows are left to the flight.changed event, which still refetches them", () => {
    const src = code("lib/flightsApi.ts");
    const action = src.slice(src.indexOf("export async function flightAction"), src.indexOf("export function isAnswerBehaviors"));
    expect(action).toContain("refreshFlightsOnly()");
    expect(action).not.toContain("refreshInstances");
    expect(action).not.toContain("refreshFlights()");
    const queries = code("state/queries.ts");
    expect(queries).toMatch(/const bump = \(\) => \{[\s\S]{0,200}queryKey: \["instances"\]/);
  });
});

// --- #13 ------------------------------------------------------------------------------

const node = (id: string, kind: DiagramNode["kind"], state: DiagramNode["state"] = "pending", extra: Partial<DiagramNode> = {}): DiagramNode =>
  ({ id, kind, row: "main", title: id, sub: "", state, icon: kind, ...extra }) as DiagramNode;
function diagram(states: DiagramNode["state"][]): Diagram {
  const nodes = [node("session:me", "session"), ...states.map((s, i) => node("stop:s" + i, "stop", s, { index: i })), node("add", "add")];
  const edges = nodes.slice(1).map((n, i) => ({ from: nodes[i].id, to: n.id, kind: n.kind === "add" ? "add" : "line" })) as Diagram["edges"];
  return { nodes, edges, flightId: "f_1", mode: "flight", insertFrom: states.length };
}

describe("#13 a Sky line counts its finished stops without a second diagram", () => {
  it("unfolded: offers the fold when two or more stops have flown", () => {
    expect(hasFlownToFold(diagram(["done", "skipped", "current", "pending"]), false)).toBe(true);
    expect(hasFlownToFold(diagram(["done", "current", "pending"]), false)).toBe(false);
    expect(hasFlownToFold(diagram(["current", "pending"]), false)).toBe(false);
  });

  it("folded: reads the pill off the drawn line (Show finished stops stays)", () => {
    const d = collapseFlown(diagram(["done", "done", "done", "current"]));
    expect(flownCount(d)).toBe(3);
    expect(hasFlownToFold(d, true)).toBe(true);
  });

  it("SkyLine builds no diagram of its own for it", () => {
    const src = code("components/flights/SkyLine.tsx");
    expect(src).not.toContain("sessionDiagram(");
    expect(src).toContain("hasFlownToFold(line.diagram, foldFlown)");
  });
});

// --- #18 ------------------------------------------------------------------------------

describe("#18 the Sky says so when the routes can't be read", () => {
  it("one line while the flights query fails, nothing otherwise", () => {
    expect(skyFlightsNote({ isError: true })).toBe(SKY_FLIGHTS_UNREACHABLE);
    expect(skyFlightsNote({ isError: false })).toBe("");
    expect(skyFlightsNote({})).toBe("");
    // The drawer's words.
    expect(SKY_FLIGHTS_UNREACHABLE).toMatch(/^Can't reach the server — /);
    expect(code("components/flights/SkyDrawer.tsx")).toContain("Can't reach the server — this list may be out of date.");
  });

  it("the toolbar draws it, on one line", () => {
    const src = code("components/flights/SkyView.tsx");
    expect(src).toContain("const flightsNote = skyFlightsNote(flightsQ);");
    expect(src).toMatch(/className="fl-note error sky-flights-note" role="status"/);
    const toolbar = src.slice(src.indexOf("const toolbar = ("));
    expect(toolbar.indexOf("sky-flights-note")).toBeGreaterThan(0);
    const rule = cssRule(css("components/flights/Sky.css"), ".sky-toolbar > .sky-flights-note");
    expect(rule).toContain("white-space: nowrap");
    expect(rule).toContain("text-overflow: ellipsis");
  });
});

// --- #22 ------------------------------------------------------------------------------

function withClient(el: ReturnType<typeof createElement>) {
  const c = new QueryClient({ defaultOptions: { queries: { retry: false, enabled: false } } });
  c.setQueryData(["instances"], []);
  return renderToStaticMarkup(createElement(QueryClientProvider, { client: c }, el));
}

describe("#22 a closed session's Stop the route asks first", () => {
  it("draws the button, and no question until it is pressed", () => {
    const halted = flight({ state: "halted", reason: "Stop s2 failed", step_states: { s1: rec("done"), s2: rec("failed"), s3: rec("pending") } });
    const html = withClient(createElement(YouStepActions, { flight: halted, gone: true }));
    expect(html).toContain(">Stop the route</button>");
    expect(html).not.toContain("Stop this route?");
  });

  it("goes through useCancelAsk, never straight to /cancel", () => {
    const src = code("components/flights/FlightStrip.tsx");
    const you = src.slice(src.indexOf("export function YouStepActions"), src.indexOf("export function FlightMenu"));
    expect(you).toContain("const cancel = useCancelAsk(flight);");
    expect(you).toMatch(/title="Its session is closed — end this route"\s*onClick=\{cancel\.ask\}/);
    expect(you).toContain("{cancel.node}");
    expect(src).not.toContain('"/cancel"');
  });
});

// --- #26 ------------------------------------------------------------------------------

describe("#26 a Sky line's ⋯ has the route's controls", () => {
  const labels = (items: { label: string }[]) => items.map((i) => i.label);

  it("a running route: Pause, then Stop the route (which asks)", () => {
    const ask = vi.fn();
    const items = lineRouteItems(flight(), ask, [{ title: "me" } as never]);
    expect(labels(items)).toEqual(["Pause", "Stop the route"]);
    expect(items[0].className).toContain("skl-mi-group");
    expect(items[1].danger).toBe(true);
    items[1].onSelect();
    expect(ask).toHaveBeenCalledOnce();
  });

  it("a paused route: Carry on", () => {
    expect(labels(lineRouteItems(flight({ state: "paused" }), () => {}, []))[0]).toBe("Carry on");
  });

  it("an ended route: Fly again, and its pick", () => {
    const done = flight({ state: "done", cursor: 3, step_states: { s1: rec("done"), s2: rec("done"), s3: rec("done") } });
    const items = lineRouteItems(done, () => {}, []);
    expect(labels(items)).toContain("Run again from another stop…");
    expect(labels(items)).not.toContain("Pause");
    expect(items[0].className).toContain("skl-mi-group");
  });

  it("no route: nothing", () => {
    expect(lineRouteItems(null, () => {}, [])).toEqual([]);
  });

  it("the same items as the pane strip's ⋯ (one builder)", () => {
    const f = flight();
    expect(labels(lineRouteItems(f, () => {}, []))).toEqual(labels(routeMenuItems(f, () => {})));
    const strip = code("components/flights/FlightStrip.tsx");
    const menu = strip.slice(strip.indexOf("export function FlightMenu"), strip.indexOf("export function routeMenuItems"));
    expect(menu).toContain("routeMenuItems(flight, onCancelAsk, sessionGone(flight, rows))");
    expect(code("components/flights/SkyLine.tsx")).toContain("...lineRouteItems(line.flight || line.landed, line.cancel.ask, p.rows),");
  });
});

// --- #35 ------------------------------------------------------------------------------

describe("#35 the ? sheet lists the needs-you jumps", () => {
  it("Ctrl+K N and Alt+V under Routes and objects, as keymap binds them", () => {
    const g = SKY_SHORTCUT_GROUPS.find((x) => x.title === "Routes and objects")!;
    const keys = g.items.map((i) => i.keys.join(" "));
    expect(keys).toContain("Ctrl K N");
    expect(keys).toContain("Alt V");
    const km = code("lib/keymap.ts");
    expect(km).toMatch(/n: \{\s*desc: "Next session that needs you/);
    expect(km).toMatch(/key: "k", mod: true, label: "the Ctrl\+K chord prefix"/);
    expect(km).toMatch(/key: "v",\s*alt: true,[\s\S]{0,300}id: "verify"/);
  });
});
