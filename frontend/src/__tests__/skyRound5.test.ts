/** Sky and routes clarity, round 5 — the owner's ten decisions, built:
 *  1. a session at a Wait for me stop reads "Needs you: <stop>";
 *  2. a halt point is Needs you (amber "Paused before <stop>"), a person's
 *     pause stays calm ("Paused by you");
 *  3. the route's first Agent stop wears the task face, not the agent's;
 *  4. a shelf click adds to the route being built;
 *  5. a route with no session shows its name and ▶ quietly at rest;
 *  6. the server's `can_fail` on the kinds, with a fallback;
 *  7. a closed session's halted route: Run again leads, Retry only where it
 *     works without the session;
 *  8. Let the agent fix it leads on a test / CI / Verify failure;
 *  9. the Intake lane folds on a narrow Sky, the person's say respected;
 * 10. a stop held by its session (agent offline / limited) never glows
 *     "working": its held tone, words and remedy.
 * The server, the MCP, the phone and voice: tests/unit/test_flight_round5.py. */

import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type { Instance } from "../api/types";
import type { Flight, FlightStep, StepKindDef, StepRunState } from "../lib/flights";
import type { DiagramNode } from "../lib/flightDiagram";
import { code, css } from "./sourceText";

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
const S = await import("../lib/sessionState");
const D = await import("../lib/flightDiagram");
const F = await import("../lib/flights");
const E = await import("../lib/stopEditing");
const G = await import("../lib/groundedLines");
const L = await import("../lib/skyLayout");
const { needsYouRows } = await import("../lib/needsYou");
const { YouStepActions, GONE_LINE } = await import("../components/flights/FlightStrip");
const { hereStop } = await import("../components/flights/useSessionLine");
const { withFixItem } = await import("../components/flights/useChainMenu");
const { frameState } = await import("../components/flights/SkyLine");

const NOW = Math.floor(Date.now() / 1000);
const step = (id: string, kind: string, title: string, config: Record<string, unknown> = {}): FlightStep => ({ id, kind, title, config, on_fail: "halt" }) as FlightStep;
const rec = (state: string, note = "", detail: Record<string, unknown> = {}): StepRunState => ({ state, since: NOW - 60, note, detail, rounds: 0 }) as unknown as StepRunState;
const flight = (over: Partial<Flight> = {}, steps: FlightStep[] = STEPS): Flight =>
  ({
    id: "f_1", plan_id: "p", plan_name: "Ship it", steps, subject: {}, session: "sc-1", repo_root: "/r", branch: "b",
    state: "running", cursor: 0, step_states: {}, reason: "", history: [], started: NOW - 600, updated: NOW - 60, finished: 0, by: "user", ...over,
  }) as unknown as Flight;
const STEPS = [step("s1", "agent", "Agent works the ticket"), step("s2", "checks", "CI checks"), step("s3", "ship", "Push"), step("s4", "agent", "Fix review comments")];
const rowOf = (over: Record<string, unknown> = {}) =>
  ({ title: "sc-1", status: "running", activity: "idle", stage: "agent", task_headline: "Do it", parent: "", branch: "b", flight: null, autopilot: null, ...over }) as unknown as Instance;
const flRow = (over: Record<string, unknown> = {}) => ({ id: "f_1", plan_name: "Ship it", cursor: 0, total: 4, state: "running", step: { id: "s1", kind: "agent", title: "Agent works the ticket", state: "running", actor: "robot" }, ...over });

function withClient(el: ReactElement) {
  const c = new QueryClient({ defaultOptions: { queries: { retry: false, enabled: false } } });
  return renderToStaticMarkup(createElement(QueryClientProvider, { client: c }, el));
}

describe("1. a session at a Wait for me stop reads Needs you", () => {
  const perched = rowOf({ flight: flRow({ state: "waiting_you", step: { id: "s5", kind: "you", title: "Approve the merge", state: "waiting_you", actor: "you" } }) });

  it("its chip says Needs you: <stop>, its remedy Done", () => {
    const ss = S.deriveSessionState(perched);
    expect(ss).toMatchObject({ state: "needs_you", reason: "flight_you", label: "Needs you: Approve the merge", perch: true, inBell: true });
    expect(S.needsYouAction(ss)?.aria).toBe("Needs you: Approve the merge — press Done in the bell");
    expect(S.REASON_LABEL.flight_you).toBe("Needs you");
  });

  it("the stop keeps its kind's name; the strip and the drawer say Needs you while parked there", () => {
    expect(F.perchLabel("Approve")).toBe("Wait for me: Approve");
    expect(code("components/flights/FlightStrip.tsx")).toContain('you && flight.state === "waiting_you" ? perchedOnYou(step.title)');
    expect(code("components/flights/SkyDrawer.tsx")).toContain('flight.state === "waiting_you" ? perchedOnYou(step.title)');
  });
});

describe("2. a halt point needs you; a pause of yours stays calm", () => {
  const at = (reason: string) => rowOf({ flight: flRow({ state: "paused", reason, cursor: 2, step: { id: "s3", kind: "ship", title: "Push", state: "pending", actor: "robot" } }) });

  it("at a halt point: amber Paused before <stop>, in the bell", () => {
    const ss = S.deriveSessionState(at("halt point"));
    expect(ss).toMatchObject({ state: "needs_you", reason: "flight_paused", label: "Paused before Push", tone: "attention", inBell: true });
    expect(S.notifyRuleFor(ss)).toBe("flight_needs_you");
    expect(S.needsYouAction(ss)?.act).toBe("bell");
    expect(S.haltPointLabel("")).toBe("Paused before its halt point");
  });

  it("a person's pause: Paused by you, calm; the server's: Paused route", () => {
    expect(S.deriveSessionState(at(""))).toMatchObject({ state: "idle", label: "Paused by you", tone: "neutral", inBell: false });
    expect(S.deriveSessionState(at("stop removed"))).toMatchObject({ state: "idle", label: "Paused route", inBell: false });
    expect(S.isPausedRouteChip({ label: "Paused by you" })).toBe(true);
    expect(S.isPausedRouteChip({ label: "Paused before Push" })).toBe(false);
  });

  it("the Needs-you list keeps it under Wait for me, and the bar does not say the stop twice", () => {
    const rows = needsYouRows({ instances: [at("halt point")] });
    expect(rows.map((r) => r.kind)).toEqual(["wait_for_me"]);
    expect(D.whereBesideChip("paused before Push · your halt point", { reason: "flight_paused", label: "Paused before Push" })).toBe("your halt point");
    expect(D.whereBesideChip("paused at Push", { label: "Paused by you" })).toBe("at Push");
  });

  it("a free route reads the same words", () => {
    const steps = [step("s1", "command", "Build", {}), step("s2", "ship", "Push")];
    const halt = flight({ state: "paused", reason: "halt point", cursor: 1, steps: [steps[0], { ...steps[1], halt: true }] as FlightStep[] }, steps);
    expect(G.runChip(halt)).toMatchObject({ label: "Paused before Push", tone: "attention" });
    expect(G.runChip(flight({ state: "paused", reason: "" }))).toMatchObject({ label: "Paused by you", tone: "muted" });
  });
});

describe("3. the route's first Agent stop wears the task face", () => {
  it("a clipboard and what it hands the agent, not the agent's sparkle", () => {
    const f = flight({ subject: { kind: "ticket", source: "jira", id: "PAY-1", title: "t", url: "" } });
    const [first, , , later] = D.flightStops(f);
    expect(first.icon).toBe(D.TASK_ICON);
    expect(first.sub).toBe("gives the agent the ticket");
    // Only the first: a later Agent stop keeps its own face.
    expect(later.icon).toBe("agent");
    expect(D.taskSummary(step("x", "agent", "A"), null)).toBe("gives the agent its task");
    expect(D.taskSummary(step("x", "agent", "A", { prompt: "Write the tests" }), null)).toBe("“Write the tests”");
  });

  it("the icon is drawn, in the work family", () => {
    expect(code("components/flights/Bird.tsx")).toMatch(/\btask: \(/);
    expect(code("components/flights/StopIcons.tsx")).toContain('task: "work"');
  });
});

describe("4. a shelf click adds to the route being built", () => {
  const built = flight({ id: "f_9", state: "grounded", session: "" });

  it("the focused route with no session, not started — else a new route", () => {
    expect(G.buildingRoute(G.groundedKey("f_9"), [built])?.id).toBe("f_9");
    expect(G.buildingRoute("", [built])).toBeNull();
    expect(G.buildingRoute("sc-1", [built])).toBeNull();
    expect(G.buildingRoute(G.groundedKey("f_9"), [{ ...built, state: "running" }])).toBeNull();
    expect(G.addedToRouteText("Verify", "Agent works")).toBe("Added “Verify” to the end of “Agent works” — click empty sky first to start another route");
  });

  it("the click tacks it on; a click-placed route is focused; a drop still places", () => {
    const v = code("components/flights/SkyView.tsx");
    const place = v.slice(v.indexOf("const placeGrounded = async"), v.indexOf("const editPlan = async"));
    expect(place).toContain("buildingRoute(groundFocus, flights)");
    expect(place).toContain("tackOn(into.id, newStep(def, into.steps.map((st) => st.id)))");
    expect(place).toContain("if (!plan) setGroundFocus(groundedKey(f.id));");
    expect(v).toContain("const dropGrounded = async");
  });
});

describe("5. a route with no session shows its name and ▶ at rest", () => {
  it("quietly — not hidden — full on hover", () => {
    const bar = css("components/flights/SkyLine.css");
    expect(bar).toMatch(/\.skl-chainline\.skl-grounded > \.skl-bar\.skl-label \{\s*opacity: 0\.72;/);
    expect(bar).not.toMatch(/\.skl-chainline\.skl-grounded > \.skl-bar\.skl-label \{\s*opacity: 0;/);
    const run = css("components/flights/SkyChain.css");
    expect(run).toMatch(/\.sky-chain \.sky-run \{[^}]*opacity: 0\.55;/);
  });
});

describe("6. can_fail comes from the server", () => {
  it("the kind's flag wins; a kind with none falls back to the list", () => {
    expect(E.canFail("wait", [{ kind: "wait", can_fail: true }])).toBe(true);
    expect(E.canFail("checks", [{ kind: "checks", can_fail: false }])).toBe(false);
    expect(E.canFail("wait", [{ kind: "wait" }])).toBe(false);
    expect(E.canFail("checks", null)).toBe(true);
    expect(E.stopSentence(step("x", "checks", "CI"), "halt", "", [{ kind: "checks", can_fail: false }])).not.toContain("If it fails");
    expect(E.stopSentence(step("x", "checks", "CI"), "halt")).toContain("If it fails");
  });

  it("the stop form asks the kind's def", () => {
    expect(code("components/flights/StopParts.tsx")).toContain("canFail(step.kind, [def])");
  });
});

describe("7. a closed session's route: Run again leads", () => {
  it("Run again, Retry only where it works without the session, never an action that fails", () => {
    expect(F.haltedRemedies({ kind: "checks", state: "halted", canFix: false, gone: true, retryWorks: false })).toEqual(["again", "skip", "stop"]);
    expect(F.haltedRemedies({ kind: "test", state: "halted", canFix: false, gone: true, retryWorks: true })).toEqual(["again", "retry", "skip", "stop"]);
    // Not ended yet (a failure handed to you): no Run again.
    expect(F.haltedRemedies({ kind: "checks", state: "waiting_you", canFix: false, gone: true, retryWorks: false })).toEqual(["skip", "stop"]);
    expect(F.retryWorksGone("test", [{ kind: "test", needs_session: false }])).toBe(true);
    expect(F.retryWorksGone("checks", [{ kind: "checks", needs_session: true }])).toBe(false);
    expect(F.retryWorksGone("test", null)).toBe(false);
  });

  it("the drawer's row: Run again primary, no Retry on a stop that needs the session", () => {
    const f = flight({ state: "halted", cursor: 1, step_states: { s1: rec("done"), s2: rec("failed") }, reason: "CI checks failed: 2 red" });
    const html = withClient(createElement(YouStepActions, { flight: f, gone: true }));
    expect(html).toMatch(/class="btn-primary fl-btn fl-fly-again"[^>]*>Run again</);
    expect(html).not.toContain(">Retry<");
    expect(html).toContain(">Stop the route<");
    expect(html).toContain(GONE_LINE);
    expect(GONE_LINE).toContain("Run again reopens it");
  });
});

describe("8. which remedy leads on a failure", () => {
  it("Let the agent fix it on a test, CI or Verify failure; Retry on any other", () => {
    for (const kind of ["test", "checks", "verify"]) expect(F.haltedRemedies({ kind, state: "halted", canFix: true, gone: false, retryWorks: false })[0]).toBe("fix");
    expect(F.haltedRemedies({ kind: "command", state: "halted", canFix: true, gone: false, retryWorks: false })).toEqual(["retry", "fix", "skip"]);
    // No agent to take it: Retry.
    expect(F.haltedRemedies({ kind: "checks", state: "halted", canFix: false, gone: false, retryWorks: false })).toEqual(["retry", "skip"]);
  });

  it("the stop's buttons, its dialog and its menu agree", () => {
    const f = flight({ state: "halted", cursor: 1, step_states: { s1: rec("done"), s2: rec("failed") }, reason: "CI checks failed: 2 red" });
    const html = withClient(createElement(YouStepActions, { flight: f }));
    expect(html).toMatch(/class="btn-primary fl-btn fl-fix"[^>]*>Let the agent fix it</);
    expect(html.indexOf(">Let the agent fix it<")).toBeLessThan(html.indexOf(">Retry<"));
    const n = { id: "stop:s2", kind: "stop", row: "main", title: "CI checks", sub: "", state: "halted", icon: "checks", step: STEPS[1], index: 1, editable: false } as DiagramNode;
    expect(hereStop(f, n).primary).toBe("fix");
    const cmd = flight({ ...f, steps: [STEPS[0], step("s2", "command", "Build")] } as Partial<Flight>, [STEPS[0], step("s2", "command", "Build")]);
    expect(hereStop(cmd, { ...n, step: cmd.steps[1], icon: "command", title: "Build" }).primary).toBe("retry");
    const menu = withFixItem([{ key: "retry", label: "Retry" }, { key: "skip", label: "Skip" }] as never, { state: "halted", title: "CI checks", step: { kind: "checks" } });
    expect(menu.map((i) => i.key)).toEqual(["fix", "retry", "skip"]);
    const other = withFixItem([{ key: "retry", label: "Retry" }, { key: "skip", label: "Skip" }] as never, { state: "halted", title: "Build", step: { kind: "command" } });
    expect(other.map((i) => i.key)).toEqual(["retry", "fix", "skip"]);
  });

  it("the bell's halted row renders the same order", () => {
    const w = code("components/outbox/WaitingRow.tsx");
    expect(w).toContain("haltedRemedies({");
    expect(w).toContain("Let the agent fix it");
  });
});

describe("9. the Intake lane folds on a narrow Sky", () => {
  it("folds by itself, never under a person who opened it, and opens again with room", () => {
    expect(L.skyIsNarrow(820)).toBe(true);
    expect(L.skyIsNarrow(1200)).toBe(false);
    expect(L.skyIsNarrow(0)).toBe(false);
    expect(L.laneFolded(null, true)).toBe(true);
    expect(L.laneFolded(null, false)).toBe(false);
    // Opened by hand on a narrow Sky: stays open.
    const opened = L.laneHandAfter(false, true);
    expect(opened).toBe("open");
    expect(L.laneFolded(opened, true)).toBe(false);
    // Folded by hand: stays folded with room.
    expect(L.laneFolded(L.laneHandAfter(true, false), false)).toBe(true);
    // Opened by hand on a wide Sky: follows the width again.
    expect(L.laneHandAfter(false, false)).toBeNull();
  });

  it("the Sky passes its width; the rail says why it folded", () => {
    expect(code("components/flights/SkyView.tsx")).toContain("narrow={skyIsNarrow(vp.w)}");
    expect(code("components/flights/IntakeLane.tsx")).toContain("laneFolded(hand, narrow)");
  });
});

describe("10. a stop held by its session never glows working", () => {
  const offline = { state: "offline", restartable: true } as const;

  it("held words and remedy", () => {
    const cur = { state: "current" as const, step: { kind: "agent" } };
    expect(D.heldMark(offline, cur)).toMatchObject({ why: "offline", label: "Held · agent offline", remedy: "restart" });
    expect(D.heldMark({ state: "offline", restartable: false, held: true }, cur)).toMatchObject({ remedy: "start" });
    const lim = D.heldMark({ state: "limit", restartable: false, resumeAt: NOW + 3600 }, cur)!;
    expect(lim.label).toMatch(/^Held · usage limit, resumes \d\d:\d\d$/);
    expect(lim.remedy).toBeUndefined();
    // CI checks go on without the agent — unless the server says it is held.
    expect(D.heldMark(offline, { state: "current", step: { kind: "checks" } })).toBeNull();
    expect(D.heldMark(null, { state: "current", step: { kind: "checks" } }, "the agent is offline")?.why).toBe("offline");
    expect(D.heldMark(offline, { state: "pending", step: { kind: "agent" } })).toBeNull();
  });

  it("on the line: the held stop, a still bird, the bar and the frame", () => {
    const f = flight({ step_states: { s1: rec("running", "the agent is offline") } });
    const row = rowOf({ activity: "offline", flight: flRow() });
    const d = D.sessionDiagram([row], "sc-1", { flights: [f] });
    const n = d.nodes.find((x) => x.id === "stop:s1")!;
    expect(n.held?.label).toBe("Held · agent offline");
    expect(n.sub).toBe("Held · agent offline");
    expect(D.birdPerch(d)?.tone).toBe("held");
    expect(D.lineWhere(d)).toBe("held at Agent works the ticket · agent offline");
    expect(frameState({ flight: "running", tone: "waiting", held: true })).toBe("held");
    // A working agent: no hold.
    const busy = D.sessionDiagram([rowOf({ activity: "working", flight: flRow() })], "sc-1", { flights: [f] });
    expect(busy.nodes.find((x) => x.id === "stop:s1")!.held).toBeUndefined();
  });

  it("its look: dashed in the waiting tone, no glow, no bob", () => {
    expect(css("components/flights/SkyChain.css")).toMatch(/\.sky-node\.st-current\.is-held \.sky-tile \{[^}]*dashed var\(--tone-waiting\)[^}]*box-shadow: none/);
    expect(css("components/flights/FlywayCanvas.css")).toMatch(/\.fl-bird-marker\.tone-held \{[^}]*--tone-waiting/);
    expect(css("components/flights/FlywayCanvas.css")).toMatch(/\.fl-st-current\.fl-held \{[^}]*animation: none/);
    expect(css("components/flights/SkyLine.css")).toContain('.skl-frame[data-frame="held"] > .skl-frame-box');
    expect(code("components/flights/FlightStrip.tsx")).toContain("<HeldRemedy held={n.held} session={flight.session} />");
  });
});

// Used by the type-only import above (kinds in 6. and 7.).
export type _Kinds = StepKindDef;
