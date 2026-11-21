/** Sky and routes clarity, round 3 — when things stop or go wrong: an offline
 * agent's chip on the Sky (and its Needs-you row) carries Restart; a route
 * whose session was closed says so, not only in a tooltip; a failed stop's
 * dialog says the failure once ("It failed: test_export_empty"), a pause says
 * whose it is; an ended route offers "Run again" (the vocabulary's word),
 * never "Fly again". */

import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type { Flight, FlightStep, StepRunState } from "../lib/flights";
import type { DiagramNode } from "../lib/flightDiagram";
import type { SessionState } from "../lib/sessionState";
import { code } from "./sourceText";

vi.mock("../api/client", () => ({ api: vi.fn(() => new Promise(() => {})), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));
vi.mock("../lib/errorPop", () => ({ errorPop: vi.fn() }));

(globalThis as Record<string, unknown>).document ??= {
  hidden: false,
  addEventListener: () => {},
  removeEventListener: () => {},
  getElementById: () => null,
  querySelector: () => null,
  documentElement: { classList: { contains: () => false, add: () => {}, remove: () => {}, toggle: () => {} } },
};

const { QueryClient, QueryClientProvider } = await import("@tanstack/react-query");
const { SkyStateChip, OFFLINE_RESTART_CHIP } = await import("../components/flights/SkyLine");
const { YouStepActions, GONE_LINE } = await import("../components/flights/FlightStrip");
const { hereStop } = await import("../components/flights/useSessionLine");
const RS = await import("../lib/routeStart");
const API = await import("../lib/flightsApi");

const NOW = Math.floor(Date.now() / 1000);
const step = (id: string, kind: string, title: string): FlightStep => ({ id, kind, title, config: {}, on_fail: "halt" }) as FlightStep;
const rec = (state: string, note = ""): StepRunState => ({ state, since: NOW - 60, note, detail: {}, rounds: 0 }) as unknown as StepRunState;
const STEPS = [step("s1", "agent", "Work"), step("s2", "checks", "CI checks"), step("s3", "ship", "Push")];
const flight = (over: Partial<Flight> = {}): Flight =>
  ({
    id: "f_1", plan_id: "p", plan_name: "Ship it", steps: STEPS, subject: {}, session: "sc-1", repo_root: "/r", branch: "b",
    state: "halted", cursor: 1, step_states: { s1: rec("done"), s2: rec("failed"), s3: rec("pending") },
    reason: "CI checks failed: test_export_empty", history: [], started: NOW - 600, updated: NOW - 60, finished: 0, by: "user", ...over,
  }) as unknown as Flight;
const node = (over: Partial<DiagramNode> = {}): DiagramNode =>
  ({ id: "stop:s2", kind: "stop", row: "main", title: "CI checks", sub: "", state: "halted", icon: "checks", step: STEPS[1], index: 1, editable: false, ...over }) as DiagramNode;

function withClient(el: ReactElement) {
  const c = new QueryClient({ defaultOptions: { queries: { retry: false, enabled: false } } });
  return renderToStaticMarkup(createElement(QueryClientProvider, { client: c }, el));
}

const offline: SessionState = { state: "offline", label: "Offline", tone: "broken", inBell: true, restartable: true, hint: "The agent's terminal is gone — Restart relaunches it (resuming the conversation)" };

describe("an offline agent carries its remedy on the Sky", () => {
  it("the chip is a Restart button", () => {
    const html = renderToStaticMarkup(createElement(SkyStateChip, { ss: offline, title: "sc-1", className: "skl-chip tone-broken" }));
    expect(html).toContain('<button type="button" class="skl-chip tone-broken sky-held-start"');
    expect(html).toContain('aria-label="Offline — restart the agent"');
    expect(html).toContain(">" + OFFLINE_RESTART_CHIP + "<");
    expect(OFFLINE_RESTART_CHIP).toBe("Offline · Restart");
  });

  it("one on another device (not restartable here) stays a label", () => {
    const html = renderToStaticMarkup(createElement(SkyStateChip, { ss: { ...offline, restartable: false }, title: "sc-1", className: "c" }));
    expect(html).toBe('<span class="c" title="' + (offline.hint || "").replace(/'/g, "&#x27;") + '">Offline</span>');
  });

  it("the Needs-you drawer's offline row says why and offers Restart", () => {
    const src = code("components/flights/SkyDrawer.tsx");
    expect(src).toContain('const revive = row.kind === "broken" && ss?.state === "offline"');
    expect(src).toContain("<ReviveButton title={row.title} start={revive === \"start\"} />");
  });
});

describe("a route whose session was closed says so", () => {
  it("in the words under its stop, not only a tooltip", () => {
    const html = withClient(createElement(YouStepActions, { flight: flight(), gone: true }));
    expect(html).toContain(GONE_LINE);
    expect(html).toContain("Stop the route");
    expect(withClient(createElement(YouStepActions, { flight: flight() }))).not.toContain(GONE_LINE);
  });
});

describe("a stopped stop's dialog says it once, and plainly", () => {
  it("the failure without the stop's name in front", () => {
    expect(hereStop(flight(), node({ reason: "CI checks failed: test_export_empty" })).why).toBe("It failed: test_export_empty");
    expect(hereStop(flight({ reason: "Checks went red" }), node({ reason: "Checks went red" })).why).toBe("It failed: Checks went red");
  });

  it("a pause says whose it is: yours, or the reason the server gave", () => {
    const paused = flight({ state: "paused", reason: "", step_states: { s1: rec("done"), s2: rec("running"), s3: rec("pending") } });
    expect(hereStop(paused, node({ state: "paused", reason: "" })).why).toBe("Paused by you — Carry on picks it up again.");
    const removed = flight({ state: "paused", reason: "stop removed", step_states: { s1: rec("done"), s2: rec("running"), s3: rec("pending") } });
    expect(hereStop(removed, node({ state: "paused", reason: "" })).why).toBe("Paused: stop removed — Carry on picks it up again.");
  });
});

describe("an ended route offers Run again", () => {
  it("in the vocabulary's word everywhere it is said", () => {
    expect(RS.flyAgainLabel(flight({ state: "done", cursor: 3 }))).toBe("Run again");
    expect(API.againText("same", "Push", "sc-1")).toBe("Running again from Push");
    for (const f of ["lib/routeStart.ts", "components/flights/FlightStrip.tsx", "components/flights/useChainMenu.tsx", "components/flights/RouteStartMenu.tsx", "lib/flightsApi.ts"]) {
      const strings = code(f).match(/"[^"\n]*"/g) || [];
      expect(strings.filter((s) => /\bFly(ing)? (it )?again\b|\bflies again\b|"Fly “/.test(s)), f).toEqual([]);
    }
  });
});
