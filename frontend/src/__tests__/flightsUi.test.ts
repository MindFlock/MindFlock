/** Flights on screen (server-rendered, mocked API): the flyway canvas (boxes
 * in order, connectors = stops − 1, live states, a perch's Done / Skip inside
 * its box), a flight's card (the drawer's Landed), the pane's one-line strip
 * and its Flight tab (the session diagram), the Sky (every session a line,
 * the bird, the drawer of what is perched on you, Waiting to fly with kind
 * badges and flyway chips — never a <select>), and the flyway card on the
 * Sky's shelf, where flyways are edited now (the Flights dialog is retired).
 * Bird words throughout, never a plane. */

/// <reference types="vite/client" />
import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { code, css } from "./sourceText";
import type { Flight, FlightPlan, FlightStep, StepKindDef } from "../lib/flights";

vi.mock("../api/client", () => ({ api: vi.fn(() => new Promise(() => {})), ApiError: class extends Error {} }));

vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const { QueryClient, QueryClientProvider } = await import("@tanstack/react-query");
const { useUi } = await import("../state/store");
const { FlightCard, PaneFlightStrip } = await import("../components/flights/FlightStrip");
const { FlywayCanvas } = await import("../components/flights/FlywayCanvas");
const { SessionFlight } = await import("../components/flights/SessionFlight");
const { flightDiagram, flywayDiagram } = await import("../lib/flightDiagram");
const { SkyView } = await import("../components/flights/SkyView");
const { FlywayCard } = await import("../components/flights/FlywayCard");
const { PlanSelect } = await import("../components/flights/PlanSelect");
const { planChoices } = await import("../lib/flights");
const { putBody } = await import("../components/intake/sourceForm");
const { fieldPatch } = await import("../components/intake/repoUses");

// The queries' poll intervals read `document.hidden`; a server render has no
// document, so a quiet one stands in (after the imports: some modules probe
// for a real one when they load).
(globalThis as Record<string, unknown>).document ??= {
  hidden: false,
  addEventListener: () => {},
  removeEventListener: () => {},
  getElementById: () => null,
  querySelector: () => null,
};

const NOW_S = Math.floor(Date.now() / 1000);
const step = (id: string, kind: string, title: string, config: Record<string, unknown> = {}): FlightStep => ({
  id,
  kind,
  title,
  config,
  on_fail: "halt",
});
const STEPS = [
  step("s1", "agent", "Work the ticket"),
  step("s2", "ship", "Push", { lane: "push" }),
  step("s3", "you", "Approve the merge", { text: "Look at the PR first", items: ["Read the diff", "Checks green"] }),
  step("s4", "ship", "Merge", { lane: "merge" }),
];
const KINDS: StepKindDef[] = [
  { kind: "agent", label: "Agent", actor: "robot", summary: "The agent works", fields: [{ name: "prompt", type: "textarea", label: "Prompt" }], default_title: "Agent works" },
  { kind: "ship", label: "Ship", actor: "robot", summary: "Commit, push, PR or merge", fields: [{ name: "lane", type: "select", label: "Lane", options: ["commit", "push", "pr", "merge"] }], default_title: "Ship" },
  { kind: "you", label: "Perch", actor: "you", summary: "Waits for you to press Done", fields: [{ name: "text", type: "textarea", label: "What to do" }], default_title: "Check it" },
];

function flight(over: Partial<Flight> = {}): Flight {
  return {
    id: "f_1",
    plan_id: "builtin:ticket",
    plan_name: "Ticket to merge",
    steps: STEPS,
    subject: { kind: "ticket", source: "sc", id: "sc-12", title: "Fix the login", url: "" },
    session: "sc-12-fix-login",
    repo_root: "/r",
    branch: "sc-12",
    state: "waiting_you",
    cursor: 2,
    step_states: {
      s1: { state: "done", since: NOW_S - 900, note: "", detail: {}, rounds: 0 },
      s2: { state: "done", since: NOW_S - 600, note: "", detail: {}, rounds: 0 },
      s3: { state: "waiting_you", since: NOW_S - 300, note: "", detail: {}, rounds: 0 },
    },
    reason: "",
    history: [],
    started: NOW_S - 900,
    updated: NOW_S - 300,
    finished: 0,
    by: "intake",
    ...over,
  };
}

const PLAN: FlightPlan = {
  id: "builtin:ticket",
  name: "Ticket to merge",
  description: "",
  builtin: true,
  trigger: { kind: "ticket", schedule: null, paused: false },
  session: { repo_path: "", program: "", model: "", in_place: false },
  steps: STEPS,
  updated: 0,
};

function withClient(el: ReactElement, prime: (c: InstanceType<typeof QueryClient>) => void = () => {}) {
  const c = new QueryClient({ defaultOptions: { queries: { retry: false, enabled: false } } });
  prime(c);
  return renderToStaticMarkup(createElement(QueryClientProvider, { client: c }, el));
}

afterEach(() => {
  vi.unstubAllGlobals();
  useUi.getState().closeDialog();
  useUi.setState({ view: "grid", skyMode: "mine", focused: null } as never);
  Object.assign(useUi.getInitialState(), useUi.getState());
});

const boxes = (html: string) => [...html.matchAll(/class="(fl-box [^"]*)"[^>]*data-node="([^"]+)"/g)].map((m) => [m[2], m[1]]);
const lines = (html: string) => (html.match(/class="fl-wire fl-wire-line"/g) || []).length;

describe("the flyway canvas", () => {
  it("draws the stops in order, joined by connectors — stops − 1 of them — and a + at the end", () => {
    const html = renderToStaticMarkup(createElement(FlywayCanvas, { diagram: flywayDiagram(STEPS, KINDS), label: "Stops", onMove: () => {}, onInsert: () => {} }));
    expect(boxes(html).map(([id]) => id)).toEqual(["stop:s1", "stop:s2", "stop:s3", "stop:s4", "add"]);
    expect(lines(html)).toBe(STEPS.length - 1);
    expect(html).toContain('class="fl-wire fl-wire-add"');
    expect(html).toMatch(/<marker id="[^"]+-arrow"/);
    // + between boxes (on hover), one per gap.
    expect(html.match(/class="fl-gap-add"/g)?.length).toBe(STEPS.length - 1);
    // Draggable stops; the perch drawn as one.
    expect(boxes(html).filter(([, c]) => c.includes("fl-draggable"))).toHaveLength(4);
    expect(boxes(html).find(([id]) => id === "stop:s3")![1]).toContain("fl-perch");
    expect(html).toContain(">Perch: Approve the merge<");
    expect(html).toContain('aria-label="Stops — drag a stop, or ← → on a selected one, to reorder it"');
  });

  it("live: done dimmed ✓, the perch glowing gold, pending outlined", () => {
    const html = renderToStaticMarkup(createElement(FlywayCanvas, { diagram: flightDiagram(flight(), KINDS), label: "Live" }));
    expect(boxes(html).map(([, c]) => c.split(" ").find((x) => x.startsWith("fl-st-")))).toEqual([
      "fl-st-done",
      "fl-st-done",
      "fl-st-perched",
      "fl-st-pending",
      "fl-st-pending",
    ]);
    expect(html).toMatch(/fl-box-glyph fl-glyph-done" aria-hidden="true">✓/);
    expect(html).toContain('class="fl-box-pulse"');
    // No drag without onMove.
    expect(html).not.toContain("fl-draggable");
  });

  it("halted: the box is red and says why", () => {
    const html = renderToStaticMarkup(
      createElement(FlywayCanvas, { diagram: flightDiagram(flight({ state: "halted", cursor: 1, reason: "Checks failed" }), KINDS), label: "Live" })
    );
    expect(boxes(html)[1][1]).toContain("fl-st-halted");
    expect(html).toContain('class="fl-box-reason">Checks failed<');
  });

  it("a phone draws it top to bottom", () => {
    const html = renderToStaticMarkup(createElement(FlywayCanvas, { diagram: flywayDiagram(STEPS, KINDS), label: "Stops", vertical: true }));
    expect(html).toContain("fl-canvas fl-canvas-compact fl-canvas-vertical");
  });
});

describe("a flight's card", () => {
  const card = (f: Flight, expand = false) => withClient(createElement(FlightCard, { flight: f, kinds: KINDS, expand }), (c) => c.setQueryData(["flights"], [f]));
  const html = card(flight(), true);

  it("leads with the subject and the bird's flyway, never a plane, and is the session's door", () => {
    expect(html).toContain('class="fl-strip-title">Fix the login<');
    expect(html).toMatch(/class="fl-strip-plan"><svg class="fl-bird"[^]*?<\/svg>Ticket to merge</);
    expect(html).not.toContain("✈");
    expect(html).toContain('data-tour="flights-strip"');
    expect(html).toContain('title="Open sc-12-fix-login"');
  });

  it("is a compact live canvas: one box per stop, the perch where the flight is", () => {
    expect(html).toContain("fl-canvas fl-canvas-compact");
    expect(boxes(html).filter(([id]) => id.startsWith("stop:"))).toHaveLength(4);
    expect(lines(html)).toBe(3);
  });

  it("perched on you (Needs you): the perch opens wide — its checklist, Done held until every item is ticked, Skip", () => {
    expect(html).toContain("Perch: Approve the merge");
    expect(html).toContain('class="fl-now-elapsed">5m<');
    expect(html).toContain("Look at the PR first");
    expect(html).toContain("Read the diff");
    expect(html).toMatch(/<button[^>]*class="btn-primary fl-btn fl-done"[^>]*disabled=""[^>]*>Done<\/button>/);
    expect(html).toMatch(/>Skip<\/button>/);
    // Inside the perch's own box.
    const perch = html.slice(html.indexOf('data-node="stop:s3"'));
    expect(perch.indexOf(">Done</button>")).toBeLessThan(perch.indexOf('data-node="stop:s4"'));
  });

  it("every live box has a ⋯: Jump here, the flight's Halt now, Cancel", () => {
    const pending = html.slice(html.indexOf('data-node="stop:s4"'));
    expect(pending).toContain('aria-label="Actions for Merge"');
    expect(pending).toContain(">Jump here</button>");
    expect(html).toContain(">Cancel flight</button>");
  });

  it("a halted flight offers Retry, not Done", () => {
    const h = card(flight({ state: "halted", cursor: 1, reason: "Checks failed" }), true);
    expect(h).toContain(">Retry</button>");
    expect(h).not.toContain(">Done</button>");
    expect(h).toContain("Checks failed");
  });

  it("a paused flight says so, offers Carry on in its ⋯, and nothing pulses", () => {
    const p = card(flight({ state: "paused", cursor: 1, step_states: {} }));
    expect(p).toMatch(/class="fl-now-paused" title="Halted by you[^"]*">Paused</);
    expect(p).toContain(">Carry on</button>");
    expect(p).not.toContain(">Halt now</button>");
    expect(p).not.toContain(">Done</button>");
    expect(p).not.toContain("fl-box-pulse");
    // Stopped by a halt point: it says which.
    const h = card(flight({ state: "paused", cursor: 1, reason: "halt point", step_states: {} }));
    expect(h).toContain(">Paused at a halt point<");
  });

  it("never says Perch: twice", () => {
    const steps = STEPS.map((s) => (s.id === "s3" ? { ...s, title: "Perch: approve the merge" } : s));
    const h = card(flight({ steps }));
    expect(h).toContain(">Perch: approve the merge<");
    expect(h).not.toContain("Perch: Perch:");
  });

  it("a robot stop running has no Done / Skip", () => {
    const r = card(flight({ state: "running", cursor: 1, step_states: {} }), true);
    expect(r).not.toContain(">Done</button>");
    expect(r).not.toContain(">Skip</button>");
  });
});

describe("the pane strip", () => {
  const row = {
    id: "f_1",
    plan_name: "Ticket to merge",
    cursor: 2,
    total: 4,
    state: "waiting_you" as const,
    step: { id: "s3", kind: "you", title: "Approve the merge", state: "waiting_you" as const, actor: "you" as const },
  };

  it("is bird · flyway · dots · stop (elapsed) — a button to the Flight tab — with Done / Skip on a perch", () => {
    const html = withClient(createElement(PaneFlightStrip, { inst: { title: "sc-12-fix-login", flight: row } }), (c) => {
      c.setQueryData(["flights"], [flight()]);
      c.setQueryData(["flight-kinds"], KINDS);
    });
    expect(html).toContain('class="fl-strip-text">Ticket to merge · ●●●○ · Approve the merge (5m)<');
    expect(html).toMatch(/<button type="button" class="fl-strip-line" title="Open the Flight tab — [^"]*"><svg class="fl-bird"/);
    expect(html).toContain('class="fl-strip fl-state-waiting_you fl-needs"');
    expect(html).toContain(">Done</button>");
    expect(html).toContain(">Skip</button>");
    expect(html).not.toContain("✈");
  });

  it("before the full flight has loaded, reads the session row alone", () => {
    const html = withClient(createElement(PaneFlightStrip, { inst: { title: "x", flight: { ...row, state: "running" } } }));
    expect(html).toContain("Ticket to merge · ●●●○ · Approve the merge");
  });

  it("is gone once the flight has landed", () => {
    expect(withClient(createElement(PaneFlightStrip, { inst: { title: "x", flight: { ...row, state: "done" } } }))).toBe("");
    expect(withClient(createElement(PaneFlightStrip, { inst: { title: "x", flight: null } }))).toBe("");
  });
});

describe("the pane's Flight tab: the session diagram", () => {
  const rowOf = (title: string, over: Record<string, unknown> = {}) => ({
    title,
    status: "running",
    activity: "idle",
    stage: "agent",
    task_headline: "Do " + title,
    parent: "",
    flight: null,
    autopilot: null,
    ...over,
  });
  const render = (rows: unknown[], title: string) =>
    withClient(createElement(SessionFlight, { inst: { title } }), (c) => {
      c.setQueryData(["instances"], rows);
      c.setQueryData(["flights"], [flight({ session: "me" })]);
      c.setQueryData(["flight-kinds"], KINDS);
      c.setQueryData(["flight-plans"], [PLAN]);
    });

  it("draws the parent, the session, its workers, the flight's stops and the trailing +", () => {
    const fRow = { id: "f_1", plan_name: "Ticket to merge", cursor: 2, total: 4, state: "waiting_you", step: { id: "s3", kind: "you", title: "Approve the merge", state: "waiting_you", actor: "you" } };
    const html = render([rowOf("lead"), rowOf("me", { parent: "lead", flight: fRow }), rowOf("w1", { parent: "me" })], "me");
    expect(boxes(html).map(([id]) => id)).toEqual(["parent:lead", "session:me", "flown", "stop:s3", "stop:s4", "add", "child:w1"]);
    expect(html).toContain('class="fl-wire fl-wire-up"');
    expect(html).toContain('class="fl-wire fl-wire-branch"');
    expect(html).toContain(">Tack on a stop<");
    expect(html).toContain('data-tour="flights-session"');
    // The perch where the flight is, opened wide.
    expect(html).toContain(">Done</button>");
    // Nothing to fly when something already is.
    expect(html).not.toContain("Fly this flyway");
  });

  it("an armed fast-track's rungs are boxes on the line", () => {
    const html = render([rowOf("me", { stage: "committed", autopilot: { depth: "pr", state: "running", step: "", reason: "", source: "user", item: "" } })], "me");
    expect(boxes(html).map(([id]) => id)).toEqual(["session:me", "rung:commit", "rung:push", "rung:pr", "add"]);
    expect(html).toContain(">Fast-track<");
  });

  it("with nothing on the line: the session, the +, and the flyways as cards with Describe a flyway", () => {
    const html = render([rowOf("me")], "me");
    expect(boxes(html).map(([id]) => id)).toEqual(["session:me", "add"]);
    expect(html).toContain(">Fly this flyway</button>");
    expect(html).toContain('class="fl-mini-box fl-perch"');
    expect(html).toContain(">Describe a flyway</button>");
    // Its line in the Sky, one press away; no bird while the session idles.
    expect(html).toMatch(/class="linklike fl-open-sky"[^>]*>Open in the Sky<\/button>/);
    expect(html).not.toContain("fl-bird-marker");
  });
});

describe("the sky", () => {
  /** The sky view — `sky/<title>`: on that session alone. */
  const render = (target: "sky" | `sky/${string}`) => {
    const one = target.startsWith("sky/") ? target.slice(4) : "";
    useUi.setState({ view: "sky", skyMode: one ? "one" : "mine", focused: one || null } as never);
    // A server render reads the store's initial state (getServerSnapshot).
    Object.assign(useUi.getInitialState(), useUi.getState());
    return withClient(createElement(SkyView), (c) => {
      c.setQueryData(["instances"], [
        { title: "sc-12-fix-login", status: "running", activity: "idle", stage: "pushed", task_headline: "Fix the login", parent: "", autopilot: null, flight: { id: "f_1", plan_name: "Ticket to merge", cursor: 2, total: 4, state: "waiting_you", step: { id: "s3", kind: "you", title: "Approve the merge", state: "waiting_you", actor: "you" } } },
        { title: "second", status: "running", activity: "working", stage: "agent", task_headline: "Second", parent: "", autopilot: null, flight: { id: "f_2", plan_name: "Ticket to merge", cursor: 1, total: 4, state: "running", step: { id: "s2", kind: "ship", title: "Push", state: "running", actor: "robot" } } },
        { title: "verify-helper", status: "running", activity: "idle", stage: "agent", parent: "", hidden_by: "verify", flight: null, autopilot: null },
      ]);
      c.setQueryData(["flights"], [flight(), flight({ id: "f_2", session: "second", state: "running", cursor: 1, step_states: {}, subject: { ...flight().subject, title: "Second" } })]);
      c.setQueryData(["flight-plans"], [PLAN]);
      c.setQueryData(["flight-kinds"], KINDS);
      c.setQueryData(["test-plans"], { plans: [] });
      c.setQueryData(["settings"], {});
    });
  };

  it("carries the flyway shelf at its foot — the flyways as rows, + New flyway — and its tour's ?", () => {
    // The shelf starts folded (mf_sky_shelf); opened once, it stays open.
    vi.stubGlobal("localStorage", { getItem: (k: string) => (k === "mf_sky_shelf" ? "1" : null), setItem: () => {} });
    const html = render("sky");
    expect(html).toContain('data-tour="sky-shelf"');
    expect(html).toMatch(/class="sky-flyway-row builtin" data-plan="builtin:ticket"/);
    expect(html).toContain(">+ New flyway</button>");
    expect(html).toContain('data-tour-id="sky"');
    expect(html).toContain('data-tour="sky-line"');
  });

  it("the sky view: the main area, every session a line with the bird on its active box — hidden helpers left out", () => {
    const html = render("sky");
    // The drawer starts folded to its tab, the perch count on it.
    expect(html).toMatch(/class="sky-drawer"[^>]*><button type="button" class="sky-drawer-tab has-perch" aria-expanded="false"/);
    expect(html).toMatch(/^<main id="sky-view" class="sky"/);
    expect(html).toContain('data-tour="flights-sky"');
    // One line per session, newest first; the verify helper has none.
    expect([...html.matchAll(/data-line="([^"]+)"/g)].map((m) => m[1])).toEqual(["second", "sc-12-fix-login"]);
    expect(html).not.toContain('data-line="verify-helper"');
    // Each line: its bar (name, state chip, ⋯), its diagram, the trailing +.
    const line = html.slice(html.indexOf('data-line="sc-12-fix-login"'));
    expect(line).toContain('class="skl-name">sc-12-fix-login<');
    expect(line).toContain('aria-label="Actions for sc-12-fix-login"');
    expect(line).toContain(">Tack on a stop<");
    // The bird: gold on the perch, flying on the running stop.
    expect(line).toMatch(/class="fl-bird-marker tone-you"[^>]*aria-label="The bird: perched on you at Perch: Approve the merge"/);
    expect(html.slice(html.indexOf('data-line="second"'))).toContain('class="fl-bird-marker tone-working"');
    // The toolbar: the views, find, + New session, Tidy, the zoom.
    for (const w of [">All<", ">Mine<", ">Perched on you<", ">This session<", ">+ New session<", ">Tidy<", ">Fit<", ">100%<", 'aria-label="Find a line"'])
      expect(html).toContain(w);
    expect(html).toContain('data-tour="flights-newblock"');
    expect(html).not.toContain("✈");
  });

  it("the drawer keeps what the Board showed: Perched on you with the perch's Done / Skip, then Waiting to fly", () => {
    // The drawer starts folded (mf_sky_drawer); opened once, it stays open.
    vi.stubGlobal("localStorage", { getItem: (k: string) => (k === "mf_sky_drawer" ? "1" : null), setItem: () => {} });
    const html = render("sky");
    expect(html).toContain('data-tour="flights-sky"');
    const drawer = html.slice(html.indexOf('class="sky-drawer open"'));
    expect(drawer).toContain('id="fl-group-needs-h"');
    expect(drawer).toContain(">Fix the login</button>");
    expect(drawer).toContain("Perch: Approve the merge");
    expect(drawer).toMatch(/class="btn-primary fl-btn fl-done"[^>]*>Done<\/button>/);
    expect(drawer.indexOf("Perched on you")).toBeLessThan(drawer.indexOf("Waiting to fly"));
    // The running flight isn't perched: not in the drawer.
    expect(drawer).not.toContain(">Second</button>");
  });

  it("one session's sky (This session — the focused one alone): only its line, its workers folded in", () => {
    const html = render("sky/second");
    expect([...html.matchAll(/data-line="([^"]+)"/g)].map((m) => m[1])).toEqual(["second"]);
    expect(html).toMatch(/aria-checked="true" class="fl-pick on"[^>]*data-value="one">This session</);
  });

});

describe("the flyway card (the shelf's editor)", () => {
  const card = (initial: Partial<FlightPlan>, plans: FlightPlan[] = [PLAN]) =>
    withClient(createElement(FlywayCard, { x: 0, y: 0, plans, initial, onSaved: () => {}, onCancel: () => {} }), (c) => {
      c.setQueryData(["flight-plans"], plans);
      c.setQueryData(["flight-kinds"], KINDS);
    });

  it("a built-in: the canvas, + between stops, Describe a stop, the inspector, Reset to default — and not one native <select>", () => {
    const html = card(PLAN);
    expect(html).toContain('data-tour="sky-flyway-card"');
    expect(boxes(html).map(([id, c]) => id + (c.includes("selected") ? "*" : ""))).toEqual(["stop:s1*", "stop:s2", "stop:s3", "stop:s4", "add"]);
    expect(lines(html)).toBe(3);
    expect(html.match(/class="fl-gap-add"/g)?.length).toBe(3);
    expect(html).toContain(">Describe</button>");
    expect(html).toContain(">Saved</button>");
    expect(html).toContain(">Save &amp; fly on…</button>");
    expect(html).toContain(">Reset to default</button>");
    expect(html).not.toContain(">Delete flyway…</button>");
    expect(html).toContain("Built-in");
    // The selected stop's inspector, from its kind's fields, in the app's controls.
    expect(html).toContain('class="fl-inspector"');
    expect(html).toContain("Prompt");
    expect(html).toContain("If it fails");
    expect(html).toContain(">Duplicate</button>");
    expect(html).toContain('class="rt-seg"');
    expect(html).toContain('aria-label="Flyway name"');
    // Its trigger, folded under one line (a ticket flyway: closed).
    expect(html).toMatch(/<details class="sky-flyway-card-trigger">\s*<summary>Takes off: <strong>Ticket<\/strong>/);
    expect(html).not.toMatch(/<select/);
    expect(html).not.toMatch(/>[^<]*\bplans?\b[^<]*</i);
  });

  it("one of yours: Delete in its ⋯, no Reset", () => {
    const other: FlightPlan = { ...PLAN, id: "p_2", name: "Other", builtin: false, steps: [STEPS[0]] };
    const html = card(other, [PLAN, other]);
    expect(html).toContain('value="Other"');
    expect(html).toContain(">Delete flyway…</button>");
    expect(html).not.toContain(">Reset to default</button>");
  });

  it("a scheduled one opens on its schedule: the days as chips, the time, Paused", () => {
    const nightly: FlightPlan = {
      ...PLAN,
      id: "p_n",
      name: "Nightly",
      builtin: false,
      trigger: { kind: "schedule", schedule: { at: "21:00", days: "weekdays" }, paused: false },
    };
    const html = card(nightly, [nightly]);
    expect(html).toMatch(/<details class="sky-flyway-card-trigger" open="">/);
    expect(html).toContain('class="fl-schedule"');
    expect(html).toContain(">Weekdays<");
    expect(html).toContain('value="21:00"');
    expect(html).toContain("Paused");
    expect(html).toContain('id="fl-plan-repo"');
  });

  it("a new one has no ⋯ (nothing saved to reset or delete)", () => {
    const html = card({});
    expect(html).toContain('value="New flyway"');
    expect(html).not.toContain("vf-menu");
  });
});

describe("the stylesheets", () => {
  const sheet = css("components/flights/Flights.css");
  const canvas = css("components/flights/FlywayCanvas.css");
  it("a perch is gold and rounder, the current box pulses, a halted one is red", () => {
    expect(canvas).toMatch(/\.fl-perch \{[^}]*border-radius: 18px;[^}]*var\(--tone-attention\)/);
    expect(canvas).toMatch(/\.fl-st-perched \{[^}]*var\(--tone-attention\)/);
    expect(canvas).toMatch(/\.fl-st-current \{[^}]*var\(--tone-working\)/);
    expect(canvas).toMatch(/\.fl-st-halted,\s*\.fl-st-failed \{[^}]*var\(--red\)/);
    expect(canvas).toContain("animation: fl-pulse");
    expect(canvas).toMatch(/\.fl-st-skipped \.fl-box-title \{[^}]*line-through/);
  });

  it("scrolls the canvas inside itself, never the page", () => {
    expect(canvas).toMatch(/\.fl-canvas \{[^}]*overflow: auto/);
    expect(canvas).toMatch(/\.fl-canvas-vertical \{[^}]*overflow-x: hidden/);
    expect(css("components/flights/FlywayCard.css")).toMatch(/\.sky-flyway-card \{[^}]*overflow: auto/);
  });

  it("Flights' fields are the New dialog's: its border, radius, background and focus ring", () => {
    expect(canvas).toMatch(/\.fl-input\.fl-input,\s*\.fl-prompt\.fl-prompt \{[^}]*background: var\(--bg\);[^}]*border: 1px solid var\(--border\);[^}]*border-radius: 7px;/);
    expect(canvas).toMatch(/\.fl-input\.fl-input:focus,\s*\.fl-prompt\.fl-prompt:focus \{[^}]*border-color: var\(--accent\)/);
  });

  it("every row waiting to fly wears its kind's badge", () => {
    for (const k of ["tickets", "prs", "issues"]) expect(sheet).toContain(".fl-kind-badge-" + k + " {");
  });
});

describe("no native <select> in Flights", () => {
  it("not one in its components", () => {
    for (const f of ["SkyView", "skyContracts", "SkyDrawer", "useSessionLine", "FlywayCard", "FlywayInspector", "FlywayShelf", "PlanSelect", "FlightStrip", "SessionFlight", "StopParts", "FlywayCanvas", "controls"])
      expect(code("components/flights/" + f + ".tsx"), f).not.toMatch(/<select/);
  });
});

describe("a source's flyway chips (on Intake's source cards)", () => {
  const PLANS: FlightPlan[] = [
    { ...PLAN, id: "builtin:ticket", name: "Ticket to merge" },
    { ...PLAN, id: "p_mine", name: "Mine", builtin: false },
    { ...PLAN, id: "builtin:pr", name: "Pull request" },
  ];

  it("offers 'Default · <the kind's built-in>' first, then every flyway, built-ins first", () => {
    expect(planChoices(PLANS, "tickets").map((o) => o.label)).toEqual(["Default · Ticket to merge", "Ticket to merge", "Pull request", "Mine"]);
    expect(planChoices(PLANS, "tickets").map((o) => o.value)).toEqual(["", "builtin:ticket", "builtin:pr", "p_mine"]);
    expect(planChoices(PLANS, "prs")[0].label).toBe("Default · Pull request");
    // An issue repo without builtin:issue on this server: the ticket flyway.
    expect(planChoices(PLANS, "issues")[0].label).toBe("Default · Ticket to merge");
    // A stored flyway the list lost is kept, never silently dropped.
    expect(planChoices(PLANS, "tickets", "p_gone").at(-1)).toEqual({ value: "p_gone", label: "p_gone (not found)" });
  });

  it("renders on the card as chips with the stored flyway on, and nothing on a server without flights", () => {
    const html = withClient(
      createElement(PlanSelect, { kind: "tickets", value: "p_mine", onChange: () => {}, className: "tk-plan", field: { attr: "data-tk-field" } }),
      (c) => c.setQueryData(["flight-plans"], PLANS)
    );
    expect(html).toContain('data-tk-field="plan"');
    expect(html).toMatch(/<button type="button" role="radio" aria-checked="true" class="fl-pick on"[^>]*data-value="p_mine">Mine<\/button>/);
    expect(html).toContain(">Default · Ticket to merge<");
    expect(html).not.toContain("<select");
    const none = withClient(
      createElement(PlanSelect, { kind: "prs", value: "", onChange: () => {}, field: { attr: "data-repo-field" } }),
      (c) => c.setQueryData(["flight-plans"], null)
    );
    expect(none).toBe("");
  });

  it("round-trips: a ticket source saves `plan` on the source, a repo card under its override map", () => {
    expect(putBody([{ id: "sc", provider: "shortcut", plan: "p_mine" }]).sources[0]).toMatchObject({ plan: "p_mine" });
    const pr = fieldPatch({}, "pr", "acme/api", "plan", "p_mine");
    expect(pr.patch).toEqual({ github: { repo_settings: { "acme/api": { plan: "p_mine" } } } });
    const issue = fieldPatch({ "acme/api": { depth: "push" } }, "issue", "acme/api", "plan", "p_mine");
    expect(issue.patch).toEqual({ github: { issue_repo_settings: { "acme/api": { depth: "push", plan: "p_mine" } } } });
    // Back to the default: the key goes, the depth stays.
    expect(fieldPatch(issue.blocks, "issue", "acme/api", "plan", "").blocks).toEqual({ "acme/api": { depth: "push" } });
    // The cards wire it: the ticket card beside Fast-track, the repo card too.
    expect(code("components/intake/TicketsTab.tsx")).toContain("onChange={(plan) => onChange({ plan })}");
    expect(code("components/intake/RepoSources.tsx")).toContain("onChange={(id) => save(PLAN_FIELD, id)}");
    expect(code("components/intake/RepoSources.tsx")).toContain('kind={use === "pr" ? "prs" : "issues"}');
  });

  it("the drawer's waiting rows: a kind heading, a badge on every row, Fly, and the flyway chips on the source's flyway", () => {
    // The drawer starts folded (mf_sky_drawer); opened once, it stays open.
    vi.stubGlobal("localStorage", { getItem: (k: string) => (k === "mf_sky_drawer" ? "1" : null), setItem: () => {} });
    useUi.setState({ view: "sky", skyMode: "mine" } as never);
    Object.assign(useUi.getInitialState(), useUi.getState());
    const html = withClient(createElement(SkyView), (c) => {
      c.setQueryData(["flights"], []);
      c.setQueryData(["flight-plans"], PLANS);
      c.setQueryData(["flight-kinds"], KINDS);
      c.setQueryData(["test-plans"], { plans: [] });
      c.setQueryData(["settings"], { ticketing: { sources: [{ id: "sc", plan: "p_mine" }] } });
      c.setQueryData(["mindflock-status"], { available: true, desired: true });
      c.setQueryData(["tickets"], {
        tickets: [{ source: "sc", id: "1", slug: "sc-1", name: "Fix it", eligible: true }],
        source_labels: { sc: "Shortcut" },
      });
    });
    expect(html).toMatch(/class="fl-waiting-head">Tickets</);
    expect(html).toContain('class="fl-kind-badge fl-kind-badge-tickets">Ticket<');
    expect(html).toContain(">Fly</button>");
    expect(html).toContain("flies by itself");
    const pick = html.slice(html.indexOf('class="fl-chiprow fl-plan-pick"'));
    expect(pick).toMatch(/aria-checked="true" class="fl-pick on"[^>]*data-value="p_mine">Mine</);
    expect(html).not.toContain("<select");
  });
});

describe("the wiring outside Flights", () => {
  it("the pane has a Flight tab that draws the session diagram", () => {
    const pane = code("components/grid/Pane.tsx");
    expect(pane).toContain('data-tab="flight"');
    expect(pane).toContain("<SessionFlight inst={inst} />");
    expect(pane).toContain("const flightShown = hasSessionDiagram(allRows ?? [], title);");
    expect(code("lib/paneHeader.ts")).toContain('!(id === "flight" && !ctx.flight && ctx.active !== "flight")');
  });

  it("the session ⋯ offers Fly a flyway…, which opens that tab, and Show in the Sky", () => {
    const menu = code("lib/sessionMenu.ts");
    expect(menu).toContain('label: "Fly a flyway…"');
    expect(menu).toContain('run: () => openTab(title, "flight")');
    expect(menu).toContain('label: "Show in the Sky"');
    expect(menu).toContain("run: () => showInSky(title),");
  });
});
