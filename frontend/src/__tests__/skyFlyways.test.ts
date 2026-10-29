/** Flyways made from the Sky: a line's stops → a flyway (`planFromSteps`, for
 * "Save as flyway"), the shelf of saved flyways (chips in order, stop dots,
 * Fly, drag data, + New flyway, folding), and the floating card (a fresh
 * name, the editor canvas, Save's body, Save & fly handing the saved flyway
 * on, a refusal pointing at its stop). Server-rendered with a mocked API,
 * like flightsUi.test.ts. */

import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { code } from "./sourceText";
import type { FlightPlan, FlightStep, StepKindDef } from "../lib/flights";

vi.mock("../api/client", () => ({
  api: vi.fn(() => new Promise(() => {})),
  ApiError: class extends Error {
    status: number;
    body: unknown;
    constructor(status: number, message: string, body: unknown) {
      super(message);
      this.status = status;
      this.body = body;
    }
  },
}));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const { api, ApiError } = await import("../api/client");
const { QueryClient, QueryClientProvider } = await import("@tanstack/react-query");
const { planFromSteps } = await import("../lib/flights");
const { startFlightOn } = await import("../lib/flightsApi");
const { FlywayShelf, FLYWAY_MIME, flywayDragData, shelfOrder } = await import("../components/flights/FlywayShelf");
const { FlywayCard, cardDraft, freshFlywayName, saveCard } = await import("../components/flights/FlywayCard");

(globalThis as Record<string, unknown>).document ??= {
  hidden: false,
  addEventListener: () => {},
  removeEventListener: () => {},
  getElementById: () => null,
  querySelector: () => null,
};

const apiMock = vi.mocked(api);
beforeEach(() => {
  apiMock.mockReset();
  apiMock.mockImplementation(() => new Promise(() => {}));
});

const step = (id: string, kind: string, title: string, config: Record<string, unknown> = {}): FlightStep => ({ id, kind, title, config, on_fail: "halt" });
const STEPS = [step("s1", "agent", "Work it"), step("s2", "you", "Approve", { items: ["Read the diff"] }), step("s3", "ship", "Merge", { lane: "merge" })];
const KINDS: StepKindDef[] = [
  { kind: "agent", label: "Agent", actor: "robot", summary: "The agent works", fields: [{ name: "prompt", type: "textarea", label: "Prompt" }], default_title: "Agent works" },
  { kind: "ship", label: "Ship", actor: "robot", summary: "Commit, push, PR or merge", fields: [{ name: "lane", type: "select", label: "Lane", options: ["commit", "push", "pr", "merge"] }], default_title: "Ship" },
  { kind: "you", label: "Perch", actor: "you", summary: "Waits for you to press Done", fields: [{ name: "text", type: "textarea", label: "What to do" }], default_title: "Check it" },
];
const plan = (id: string, name: string, builtin = false, steps: FlightStep[] = STEPS): FlightPlan => ({
  ...planFromSteps(name, steps),
  id,
  builtin,
  updated: 0,
});
const PLANS = [plan("p_mine", "My review"), plan("builtin:ticket", "Ticket to merge", true), plan("p_long", "Long", false, Array.from({ length: 13 }, (_, i) => step("s" + (i + 1), "agent", "Stop " + (i + 1))))];

function withClient(el: ReactElement) {
  const c = new QueryClient({ defaultOptions: { queries: { retry: false, enabled: false } } });
  c.setQueryData(["flight-kinds"], KINDS);
  return renderToStaticMarkup(createElement(QueryClientProvider, { client: c }, el));
}

describe("planFromSteps (Save as flyway)", () => {
  it("keeps what a stop is, re-numbered s1..sN, and drops what a run did", () => {
    const live = [
      { ...step("s4", "agent", "Work it", { prompt: "go", _absorbed: true, gone: undefined }), state: "done", detail: { performing: { at: 1 } } },
      step("x9", "you", "Approve", { items: ["a", "b"] }),
    ] as unknown as FlightStep[];
    const p = planFromSteps("  From my line ", live);
    expect(p).toEqual({
      name: "From my line",
      description: "",
      builtin: false,
      trigger: { kind: "manual", schedule: null, paused: false },
      session: { repo_path: "", program: "", model: "", in_place: false },
      steps: [
        { id: "s1", kind: "agent", title: "Work it", config: { prompt: "go" }, on_fail: "halt" },
        { id: "s2", kind: "you", title: "Approve", config: { items: ["a", "b"] }, on_fail: "halt" },
      ],
    });
    expect(p).not.toHaveProperty("id");
    expect(p).not.toHaveProperty("updated");
    // A copy: editing the flyway never edits the line.
    (p.steps[1].config.items as string[]).push("c");
    expect((live[1].config.items as string[]).length).toBe(2);
  });

  it("names a nameless one, and keeps a trigger it is given", () => {
    const trig = { kind: "schedule" as const, schedule: { at: "21:00", days: "daily" as const }, paused: false };
    const p = planFromSteps("", STEPS, trig);
    expect(p.name).toBe("New flyway");
    expect(p.trigger).toEqual(trig);
    expect(p.trigger).not.toBe(trig);
  });
});

describe("the shelf", () => {
  it("built-ins first; each chip its name, its stop dots (a perch gold), Fly, and draggable", () => {
    expect(shelfOrder(PLANS).map((p) => p.id)).toEqual(["builtin:ticket", "p_mine", "p_long"]);
    const html = renderToStaticMarkup(createElement(FlywayShelf, { plans: PLANS, onNew() {}, onFly() {}, onEdit() {} }));
    expect([...html.matchAll(/data-plan="([^"]+)"/g)].map((m) => m[1])).toEqual(["builtin:ticket", "p_mine", "p_long"]);
    expect(html.match(/draggable="true"/g)).toHaveLength(3);
    const mine = html.slice(html.indexOf('data-plan="p_mine"'), html.indexOf('data-plan="p_long"'));
    expect(mine).toContain('<span class="sky-shelf-name">My review</span>');
    expect(mine.match(/class="sky-shelf-dot( perch)?"/g)).toEqual(['class="sky-shelf-dot"', 'class="sky-shelf-dot perch"', 'class="sky-shelf-dot"']);
    expect(mine).toContain('aria-label="Fly “My review” on a line"');
    expect(mine).toContain('aria-label="Edit the flyway “My review”"');
    // Thirteen stops: ten dots and "+3".
    const long = html.slice(html.indexOf('data-plan="p_long"'));
    expect(long.match(/class="sky-shelf-dot"/g)).toHaveLength(10);
    expect(long).toContain(">+3<");
    expect(html).toContain('class="sky-shelf-chip sky-shelf-new">+ New flyway</button>');
    expect(html).toContain('aria-expanded="true"');
    expect(html).not.toContain("✈");
  });

  it("a chip's drag carries the flyway's id under its own type", () => {
    expect(FLYWAY_MIME).toBe("application/x-mindflock-flyway");
    expect(flywayDragData(PLANS[0])).toEqual([
      ["application/x-mindflock-flyway", "p_mine"],
      ["text/plain", "My review"],
    ]);
    const src = code("components/flights/FlywayShelf.tsx");
    expect(src).toMatch(/onDragStart=\{\(e\) => onChipDrag\(e, p\)\}/);
    expect(src).toMatch(/for \(const \[type, value\] of flywayDragData\(plan\)\) e\.dataTransfer\.setData\(type, value\)/);
    expect(src).toMatch(/onClick=\{\(\) => onEdit\(p\)\}/);
    expect(src).toMatch(/onClick=\{\(\) => onFly\(p\)\}/);
    expect(src).toMatch(/onClick=\{onNew\}/);
  });

  it("folds to its head (the host's `collapsed`)", () => {
    const html = renderToStaticMarkup(createElement(FlywayShelf, { plans: PLANS, onNew() {}, onFly() {}, onEdit() {}, collapsed: true }));
    expect(html).toContain('class="sky-shelf folded"');
    expect(html).toContain('aria-expanded="false"');
    expect(html).not.toContain("sky-shelf-chip");
    expect(html).toContain('<span class="sky-shelf-count">3</span>');
  });
});

describe("the card", () => {
  it("a new flyway gets a name the shelf doesn't have", () => {
    expect(freshFlywayName([])).toBe("New flyway");
    expect(freshFlywayName([{ name: "New flyway" }, { name: "new flyway 2" }])).toBe("New flyway 3");
    expect(cardDraft([{ name: "New flyway" }]).name).toBe("New flyway 2");
    expect(cardDraft([], planFromSteps("From my line", STEPS)).name).toBe("From my line");
    expect(cardDraft([], { steps: STEPS }).steps).toEqual(STEPS);
  });

  it("floats at (x, y): the name, the editor canvas, Describe, and Save & fly / Save / Cancel", () => {
    const html = withClient(createElement(FlywayCard, { x: 120, y: 80, plans: PLANS, onSaved() {}, onCancel() {} }));
    expect(html).toMatch(/^<div class="sky-flyway-card" style="left:120px;top:80px" role="dialog" aria-label="New flyway"/);
    expect(html).toMatch(/<input type="text" class="fl-input" aria-label="Flyway name"[^>]*value="New flyway"/);
    expect(html).toContain('aria-label="The new flyway&#x27;s stops — drag a stop');
    expect(html).toContain('data-node="add"');
    expect(html).toContain('class="fl-describe"');
    expect(html).toContain("Press + to add the first stop");
    // Nothing to save yet.
    expect(html).toMatch(/<button type="button" class="btn-primary fl-btn" disabled="">Save &amp; fly on…<\/button>/);
    expect(html).toMatch(/<button type="button" class="test-btn fl-btn" disabled="">Save<\/button>/);
    expect(html).toContain(">Cancel</button>");
    expect(html).not.toContain("<select");
  });

  it("seeded from a line: its stops on the canvas, the first in the inspector", () => {
    const html = withClient(createElement(FlywayCard, { x: 0, y: 0, plans: PLANS, initial: planFromSteps("From second", STEPS), onSaved() {}, onCancel() {} }));
    expect([...html.matchAll(/data-node="(stop:[^"]+)"/g)].map((m) => m[1])).toEqual(["stop:s1", "stop:s2", "stop:s3"]);
    expect(html).toContain('aria-label="Stop: Work it"');
    expect(html).toMatch(/<button type="button" class="btn-primary fl-btn">Save &amp; fly on…<\/button>/);
    // Editing a saved one says so.
    const edit = withClient(createElement(FlywayCard, { x: 0, y: 0, plans: PLANS, initial: PLANS[0], onSaved() {}, onCancel() {} }));
    expect(edit).toContain('aria-label="Edit the flyway “My review”"');
  });

  it("Save posts the flyway (no id = create) and keeps the card; Save & fly hands the saved one on", async () => {
    const draft = cardDraft(PLANS, planFromSteps("  From second ", STEPS));
    const saved = plan("p_new", "From second");
    apiMock.mockResolvedValueOnce({ plan: saved });
    const onSaved = vi.fn();
    const r = await saveCard(draft, false, onSaved);
    expect(apiMock).toHaveBeenCalledWith("/api/flights/plans", { json: expect.not.objectContaining({ id: expect.anything() }) });
    const body = (apiMock.mock.calls[0][1] as { json: FlightPlan }).json;
    expect(body.name).toBe("From second");
    expect(body.steps.map((s) => s.id)).toEqual(["s1", "s2", "s3"]);
    expect(r).toEqual({ plan: saved, error: "", stop: -1 });
    expect(onSaved).not.toHaveBeenCalled();

    // Save & fly on…: the second save is an update (it has an id now).
    apiMock.mockResolvedValueOnce({ plan: saved });
    const r2 = await saveCard(saved, true, onSaved);
    expect((apiMock.mock.calls.at(-1)![1] as { json: FlightPlan }).json.id).toBe("p_new");
    expect(onSaved).toHaveBeenCalledWith(saved);
    expect(r2.plan).toBe(saved);
  });

  it("a refusal: the server's sentence, the stop it names, and no fly", async () => {
    apiMock.mockRejectedValueOnce(new ApiError(400, "Merge needs a lane", { error: "bad_step", field: "steps[2].config.lane" }));
    const onSaved = vi.fn();
    const r = await saveCard(cardDraft([], { steps: STEPS }), true, onSaved);
    expect(r).toEqual({ plan: null, error: "Merge needs a lane", stop: 2 });
    expect(onSaved).not.toHaveBeenCalled();
  });

  it("the host flies a saved flyway on a line with startFlightOn", async () => {
    apiMock.mockResolvedValueOnce({ flight: { id: "f_9", plan_name: "From second", session: "" } });
    const f = await startFlightOn("second", "p_new");
    expect(apiMock).toHaveBeenCalledWith("/api/flights/start", { json: { plan_id: "p_new", title: "second" } });
    expect(f?.id).toBe("f_9");
  });
});
