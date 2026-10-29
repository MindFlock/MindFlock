/** Flights on screen (server-rendered, mocked API): a flight's strip on the
 * Board — robot and *you* blocks told apart, the current one marked, Done held
 * until every item is ticked — the pane's one-line strip, the dialog's five
 * tabs, the Board's groups, and the Plans editor's row of blocks with + between
 * them and Describe a step at its end. */

/// <reference types="vite/client" />
import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { css } from "./sourceText";
import type { Flight, FlightPlan, FlightStep, StepKindDef } from "../lib/flights";

vi.mock("../api/client", () => ({ api: vi.fn(() => new Promise(() => {})), ApiError: class extends Error {} }));

vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const { QueryClient, QueryClientProvider } = await import("@tanstack/react-query");
const { useUi } = await import("../state/store");
const { BlockChips, FlightCard, PaneFlightStrip } = await import("../components/flights/FlightStrip");
const { FlightsDialog } = await import("../components/flights/FlightsDialog");
const { Plans } = await import("../components/flights/PlanEditor");

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
  { kind: "you", label: "You", actor: "you", summary: "Waits for you to press Done", fields: [{ name: "text", type: "textarea", label: "What to do" }], default_title: "You" },
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
  useUi.getState().closeDialog();
  Object.assign(useUi.getInitialState(), useUi.getState());
});

describe("a flight's strip on the Board", () => {
  const html = renderToStaticMarkup(createElement(FlightCard, { flight: flight(), kinds: KINDS }));

  it("leads with the subject and the plan, and is the session's door", () => {
    expect(html).toContain('class="fl-strip-title">Fix the login<');
    expect(html).toContain("✈ Ticket to merge");
    expect(html).toContain('data-tour="flights-strip"');
    expect(html).toContain('title="Open sc-12-fix-login"');
  });

  it("draws the blocks as chips: robot vs you, done ✓, current ●, pending ○", () => {
    const chips = [...html.matchAll(/<li class="(fl-chip[^"]*)"/g)].map((m) => m[1]);
    expect(chips).toEqual([
      "fl-chip fl-chip-done fl-chip-robot",
      "fl-chip fl-chip-done fl-chip-robot",
      "fl-chip fl-chip-waiting_you fl-chip-you fl-chip-current",
      "fl-chip fl-chip-pending fl-chip-robot",
    ]);
    expect(html).toMatch(/fl-chip-glyph" aria-hidden="true">✓/);
    expect(html).toMatch(/fl-chip-glyph" aria-hidden="true">●/);
    expect(html).toMatch(/fl-chip-glyph" aria-hidden="true">○/);
  });

  it("says the current step and how long, and offers Done (held until every item is ticked) and Skip", () => {
    expect(html).toContain("You: Approve the merge");
    expect(html).toContain('class="fl-now-elapsed">5m<');
    expect(html).toContain("Look at the PR first");
    expect(html).toContain("Read the diff");
    expect(html).toMatch(/<button[^>]*class="btn-primary fl-btn fl-done"[^>]*disabled=""[^>]*>Done<\/button>/);
    expect(html).toMatch(/>Skip<\/button>/);
  });

  it("a halted flight offers Retry, not Done", () => {
    const h = renderToStaticMarkup(createElement(FlightCard, { flight: flight({ state: "halted", reason: "Checks failed" }), kinds: KINDS }));
    expect(h).toContain(">Retry</button>");
    expect(h).not.toContain(">Done</button>");
    expect(h).toContain("Checks failed");
  });

  it("a paused flight says so, offers Resume in its ⋯, and nothing pulses", () => {
    const p = renderToStaticMarkup(createElement(FlightCard, { flight: flight({ state: "paused", cursor: 1 }), kinds: KINDS }));
    expect(p).toContain('class="fl-now-paused">Paused<');
    expect(p).toContain(">Resume</button>");
    expect(p).not.toContain(">Pause</button>");
    expect(p).not.toContain(">Done</button>");
    expect(css("components/flights/FlightsDialog.css")).toMatch(/\.fl-state-paused \.fl-chip-current \.fl-chip-glyph \{\s*animation: none;/);
  });

  it("never says You: twice", () => {
    const steps = STEPS.map((s) => (s.id === "s3" ? { ...s, title: "You: approve the merge" } : s));
    const h = renderToStaticMarkup(createElement(FlightCard, { flight: flight({ steps }), kinds: KINDS }));
    expect(h).toContain(">You: approve the merge<");
    expect(h).not.toContain("You: You:");
  });

  it("a robot step running has no Done / Skip", () => {
    const r = renderToStaticMarkup(createElement(FlightCard, { flight: flight({ state: "running", cursor: 1 }), kinds: KINDS }));
    expect(r).not.toContain(">Done</button>");
    expect(r).not.toContain(">Skip</button>");
  });

  it("in jump mode every chip is a button", () => {
    const j = renderToStaticMarkup(createElement(BlockChips, { flight: flight(), kinds: KINDS, onJump: () => {} }));
    expect(j.match(/aria-label="Jump to /g)?.length).toBe(4);
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

  it("is ✈ plan · dots · step (elapsed), with Done / Skip on a you step", () => {
    const html = withClient(createElement(PaneFlightStrip, { inst: { title: "sc-12-fix-login", flight: row } }), (c) => {
      c.setQueryData(["flights"], [flight()]);
      c.setQueryData(["flight-kinds"], KINDS);
    });
    expect(html).toContain("✈ Ticket to merge · ●●●○ · Approve the merge (5m)");
    expect(html).toContain('class="fl-strip fl-state-waiting_you fl-needs"');
    expect(html).toContain(">Done</button>");
    expect(html).toContain(">Skip</button>");
  });

  it("before the full flight has loaded, reads the session row alone", () => {
    const html = withClient(createElement(PaneFlightStrip, { inst: { title: "x", flight: { ...row, state: "running" } } }));
    expect(html).toContain("✈ Ticket to merge · ●●●○ · Approve the merge");
  });

  it("is gone once the flight has landed", () => {
    expect(withClient(createElement(PaneFlightStrip, { inst: { title: "x", flight: { ...row, state: "done" } } }))).toBe("");
    expect(withClient(createElement(PaneFlightStrip, { inst: { title: "x", flight: null } }))).toBe("");
  });
});

describe("the dialog", () => {
  const render = (target: string | null) => {
    useUi.setState({ openDialog: "flights", dialogTarget: target } as never);
    // A server render reads the store's initial state (getServerSnapshot).
    Object.assign(useUi.getInitialState(), useUi.getState());
    return withClient(createElement(FlightsDialog), (c) => {
      c.setQueryData(["flights"], [flight(), flight({ id: "f_2", state: "running", cursor: 1, subject: { ...flight().subject, title: "Second" } })]);
      c.setQueryData(["flight-plans"], [PLAN]);
      c.setQueryData(["flight-kinds"], KINDS);
      c.setQueryData(["test-plans"], { plans: [] });
      c.setQueryData(["settings"], {});
    });
  };

  it("has the five tabs, in the contract's order", () => {
    const html = render(null);
    const tabs = [...html.matchAll(/data-flights-tab="([a-z]+)"/g)].map((m) => m[1]);
    expect(tabs).toEqual(["board", "plans", "sources", "schedule", "checklists"]);
    expect(html).toContain('id="flights-dialog"');
    expect(html).toContain(">Flights</h2>");
  });

  it("the Board: Needs you, In flight, Arrivals — loudest first", () => {
    const html = render("board");
    const n = html.indexOf("Needs you");
    const f = html.indexOf("In flight");
    const a = html.indexOf("Arrivals");
    expect(n).toBeGreaterThan(0);
    expect(n < f && f < a).toBe(true);
    expect(html).toContain('data-tour="flights-board"');
    // The parked flight is under Needs you, the running one under In flight.
    expect(html.indexOf("Fix the login")).toBeLessThan(f);
    expect(html.indexOf("Second")).toBeGreaterThan(f);
  });

  it("the Plans editor: the row of blocks, + between them, Describe a step at the end", () => {
    const html = render("plans");
    expect(html).toContain('data-tour="flights-plans"');
    expect(html).toContain('data-tour="flights-describe"');
    const blocks = [...html.matchAll(/class="(fl-block [^"]*)"/g)].map((m) => m[1]);
    expect(blocks).toEqual([
      "fl-block fl-block-robot selected",
      "fl-block fl-block-robot",
      "fl-block fl-block-you",
      "fl-block fl-block-robot",
    ]);
    expect(html.match(/class="fl-add"/g)?.length).toBe(5);
    expect(html).toMatch(/draggable="true"/);
    expect(html).toContain(">Describe a step</button>");
    expect(html).toContain(">Save</button>");
    expect(html).toContain(">Take off</button>");
    expect(html).toContain(">Reset to default</button>");
    expect(html).toContain("Built-in");
    // The selected block's form, from its kind's fields.
    expect(html).toContain("Prompt");
    expect(html).toContain("If it fails");
  });
});

describe("the editor on its own", () => {
  it("opens on the plan a deep link named", () => {
    const other = { ...PLAN, id: "p_2", name: "Other", builtin: false, steps: [STEPS[0]] };
    const html = withClient(createElement(Plans, { initialPlan: "p_2", onDirty: () => {} }), (c) => {
      c.setQueryData(["flight-plans"], [PLAN, other]);
      c.setQueryData(["flight-kinds"], KINDS);
    });
    expect(html).toContain('value="Other"');
    expect(html).toContain(">Delete</button>");
    expect(html).not.toContain(">Reset to default</button>");
  });
});

describe("the stylesheet", () => {
  const sheet = css("components/flights/FlightsDialog.css");
  it("outlines you blocks in the needs-you gold and pulses the current one", () => {
    expect(sheet).toMatch(/\.fl-chip-you \{[^}]*var\(--tone-attention\)/);
    expect(sheet).toMatch(/\.fl-block-you \{[^}]*var\(--tone-attention\)/);
    expect(sheet).toContain("animation: fl-pulse");
  });

  it("scrolls the block row inside itself, never the page", () => {
    expect(sheet).toMatch(/\.fl-row-scroll \{[^}]*overflow-x: auto/);
    expect(sheet).toMatch(/\.fl-body \{[^}]*overflow-x: hidden/);
  });
});
