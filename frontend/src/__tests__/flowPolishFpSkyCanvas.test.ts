/** Flow polish (fp-sky-canvas): every route on the Sky can be followed and
 * un-stuck from the Sky, and saving / naming a route is obvious.
 *
 *  1. A route running on its own (no session): "Halted" in red with its
 *     reason, "Paused" only for a real pause; Retry / Skip / Done / Carry on
 *     on its tile; a read-only stop dialog with those remedies; drawn in the
 *     Needs-you view; the drawer flies the camera to it.
 *  2. The drawer says WHY a waiting route waits.
 *  3. The drawer never says "Nothing needs you." when it could not load.
 *  4. A paused route's frame is calm and dashed.
 *  5. Save as a route… (named inline), Edit its stops, the toast.
 *  6. The tools are called Select (V) and Pan (H). */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { code } from "./sourceText";
import type { Flight, FlightPlan, FlightStep, StepKindDef } from "../lib/flights";

vi.mock("../api/client", () => ({ api: vi.fn(() => new Promise(() => {})), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const G = await import("../lib/groundedLines");
const { GroundedLine } = await import("../components/flights/GroundedLine");
const { freeStopRemedies } = await import("../components/flights/useGroundedLine");
const { needsNote, perchLine } = await import("../components/flights/SkyDrawer");
const { frameState } = await import("../components/flights/SkyLine");
const { SaveRouteAsk, savedMenuItems, NO_SAVED_ROUTES } = await import("../components/flights/StopShelf");

// After the imports: the store reads the real (absent) document as it loads.
(globalThis as Record<string, unknown>).document ??= {
  hidden: false,
  addEventListener: () => {},
  removeEventListener: () => {},
  getElementById: () => null,
  querySelector: () => null,
};

const KINDS: StepKindDef[] = [
  { kind: "agent", label: "Agent", actor: "robot", summary: "The agent works", fields: [], default_title: "Agent" },
  { kind: "ship", label: "Ship", actor: "robot", summary: "Push", fields: [], default_title: "Ship" },
  { kind: "you", label: "Wait for me", actor: "you", summary: "You", fields: [], default_title: "Wait for me" },
];

const step = (id: string, k: string, title: string, over: Partial<FlightStep> = {}): FlightStep => ({ id, kind: k, title, config: {}, on_fail: "halt", ...over });
const rec = (state: string, over: Record<string, unknown> = {}) => ({ state, since: 0, note: "", detail: {}, rounds: 0, ...over }) as Flight["step_states"][string];

type FreeFlight = Flight & { free?: boolean };
const flight = (over: Partial<FreeFlight> = {}): FreeFlight => ({
  id: "f_1",
  plan_id: "adhoc",
  plan_name: "Nightly",
  steps: [step("s1", "agent", "Fix it"), step("s2", "ship", "Push")],
  subject: { kind: "manual", source: "", id: "", title: "", url: "" },
  session: "",
  repo_root: "/tmp/repo",
  branch: "",
  state: "running",
  cursor: 0,
  step_states: {},
  reason: "",
  history: [],
  started: 100,
  updated: 100,
  finished: 0,
  by: "",
  free: true,
  ...over,
});

const halted = () => flight({ state: "halted", reason: "Fix it failed: 3 tests red", step_states: { s1: rec("failed") } });
const atHalt = () => flight({ state: "paused", reason: "halt point", steps: [step("s1", "agent", "Fix it", { halt: true }), step("s2", "ship", "Push")] });
const waiting = () =>
  flight({ state: "waiting_you", steps: [step("s1", "you", "Look at the diff"), step("s2", "ship", "Push")], step_states: { s1: rec("waiting_you", { note: "Check the screenshots match" }) }, reason: "needs you" });

describe("1. a route running on its own", () => {
  it("its chip: Halted in red with the reason in its tooltip; Paused only for a real pause; a halt point waits on you", () => {
    const h = G.runChip(halted());
    expect(h).toMatchObject({ label: "Halted", tone: "broken" });
    expect(h.title).toContain("3 tests red");
    expect(h.title).toContain("Retry");
    expect(G.runChip(flight({ state: "paused", reason: "" }))).toMatchObject({ label: "Paused", tone: "muted" });
    expect(G.runChip(atHalt())).toMatchObject({ label: "At your halt point", tone: "attention" });
    expect(G.runChip(waiting())).toMatchObject({ label: "Needs you", tone: "attention" });
    expect(G.runChip(waiting()).title).toContain("Check the screenshots match");
    expect(G.runChip(flight())).toMatchObject({ label: "Running", tone: "working" });
  });

  it("the Needs-you view draws the free runs that wait on you — halted, waiting, at a halt point — and nothing else with no session", () => {
    const all = [
      halted(),
      { ...waiting(), id: "f_2" },
      { ...atHalt(), id: "f_3" },
      flight({ id: "f_4" }),
      flight({ id: "f_5", state: "paused", reason: "" }),
      flight({ id: "f_6", state: "grounded", free: false }),
    ];
    const name = (f: Flight) => f.plan_name;
    expect(G.groundedShown(all, "perched", "", name).map((f) => f.id).sort()).toEqual(["f_1", "f_2", "f_3"]);
    expect(G.groundedShown(all, "all", "", name)).toHaveLength(6);
    expect(G.groundedShown(all, "one", "", name)).toEqual([]);
    expect(G.groundedShown(all, "all", "zzz", name)).toEqual([]);
    expect(G.freeRunNeedsYou({ ...halted(), session: "s" })).toBe(false);
  });

  it("its tiles carry the live remedies: the halted stop has Retry and Skip, the bar says Halted", () => {
    const html = renderToStaticMarkup(
      createElement(GroundedLine, {
        flight: halted(),
        kinds: KINDS,
        plans: [],
        layout: { x: 0, y: 0, size: "normal", open: false },
        focused: false,
        onFocus() {},
        zoom: 1,
        onFlyOn() {},
      })
    );
    expect(html).toMatch(/class="fl-box-chip skl-chip skl-free-chip tone-broken" data-run="halted" title="Halted at “Fix it” — [^"]*3 tests red/);
    expect(html).toContain(">Halted<");
    expect(html).not.toContain(">Paused<");
    expect(html).toContain(">Retry<");
    expect(html).toContain(">Skip<");
    const src = code("components/flights/GroundedLine.tsx");
    expect(src).toContain("renderInBox={running ? parts.renderInBox : undefined}");
    expect(src).toContain("stopTileBoxes(moves.diagram, flight)");
  });

  it("its stop dialog reads only, with the remedies of the stop it is on", () => {
    const h = halted();
    const r = freeStopRemedies(h, { index: 0, state: "halted", step: h.steps[0] });
    expect(r).toMatchObject({ retry: true, skip: true, done: "" });
    expect(r.note).toContain("3 tests red");
    expect(freeStopRemedies(atHalt(), { index: 0, state: "paused", step: atHalt().steps[0] })).toMatchObject({ done: "carry", skip: true, retry: false });
    const w = waiting();
    expect(freeStopRemedies(w, { index: 0, state: "perched", step: w.steps[0] })).toMatchObject({ done: "done", skip: true, retry: false });
    // A stop it has run: nothing to press.
    const later = flight({ cursor: 1, step_states: { s1: rec("done") } });
    expect(freeStopRemedies(later, { index: 0, state: "done", step: later.steps[0] })).toMatchObject({ done: "", retry: false, skip: false, note: "This stop has finished — it can't change now." });
    const src = code("components/flights/useGroundedLine.tsx");
    expect(src).toContain("isFreeRun(flight) && !open.editable");
    expect(src).toContain('doneLabel={r.done === "carry" ? "Carry on" : undefined}');
  });

  it("the drawer flies the camera to a route with no session (never just un-focuses)", () => {
    const drawer = code("components/flights/SkyDrawer.tsx");
    expect(drawer).toContain("const go = () => (flight.session ? onFocus(flight.session) : onFocusFlight?.(flight.id));");
    expect(drawer).not.toContain("onClick={() => onFocus(flight.session)}");
    const sky = code("components/flights/SkyView.tsx");
    expect(sky).toContain("onFocusFlight={focusFlight}");
    expect(sky).toMatch(/const focusFlight = \(id: string\) => \{\s*const key = groundedKey\(id\);\s*if \(rectOf\(key\)\) return jumpTo\(key\);/);
  });
});

describe("2. the drawer says why", () => {
  it("a waiting row shows the stop's note; a halted one its failure; a halt point says so; the bare 'needs you' never", () => {
    expect(perchLine(waiting(), KINDS)).toContain("Check the screenshots match");
    expect(perchLine(halted(), KINDS)).toBe("Fix it — Failed: 3 tests red");
    expect(perchLine(atHalt(), KINDS)).toContain("Paused at your halt point");
    const bare = flight({ state: "waiting_you", steps: [step("s1", "you", "Look")], reason: "needs you" });
    expect(G.perchWhy(bare)).toBe("");
    const failed = flight({ state: "waiting_you", step_states: { s1: rec("waiting_you", { detail: { failed_reason: "lint failed" } }) } });
    expect(G.perchWhy(failed)).toBe("lint failed");
  });
});

describe("3. the drawer never claims 'Nothing needs you.' when it could not load", () => {
  it("loading, error, empty", () => {
    expect(needsNote({ data: undefined, isPending: true }, 0)).toBe("loading");
    expect(needsNote({ data: undefined, isPending: true }, 2)).toBe("");
    expect(needsNote({ data: [], isError: true }, 0)).toBe("error");
    expect(needsNote({ data: [], isError: true }, 3)).toBe("error");
    expect(needsNote({ data: [] }, 0)).toBe("empty");
    expect(needsNote({ data: [] }, 1)).toBe("");
    expect(needsNote({ data: null }, 0)).toBe("empty");
    const src = code("components/flights/SkyDrawer.tsx");
    expect(src).toContain("Can't reach the server — this list may be out of date.");
    expect(src).toContain('note === "empty" && <p className="fl-empty muted">Nothing needs you.</p>');
  });
});

describe("4. a paused route looks paused", () => {
  it("frameState: paused is its own calm state; a halt point is you; the frame box goes dashed", () => {
    expect(frameState({ flight: "paused" })).toBe("paused");
    expect(frameState({ flight: "paused", haltPoint: true })).toBe("you");
    expect(frameState({ flight: "paused", tone: "attention" })).toBe("you");
    expect(frameState({ flight: "paused", tone: "broken" })).toBe("broken");
    expect(frameState({ flight: "halted" })).toBe("halted");
    // The dashed frame lives in SkyLine.css (fp-seams), not inline.
    expect(code("components/flights/SkyLine.css")).toContain('.skl-frame[data-frame="paused"] > .skl-frame-box {\n  border: calc(1.5px / var(--sky-z, 1)) dashed color-mix(in srgb, var(--muted) 70%, transparent);');
    expect(code("components/flights/SkyLine.tsx")).toContain("style={{ left: frameX, top: frameY }}");
  });
});

describe("5. saving and naming a route", () => {
  const plans: FlightPlan[] = [
    { id: "p_mine", name: "Mine", description: "d", builtin: false, trigger: { kind: "manual", schedule: null, paused: false }, session: { repo_path: "/r", program: "", model: "", in_place: false }, steps: [step("s1", "agent", "A")], updated: 5 },
  ];

  it("planToSave: `fresh` is always a new route under the name; `into` writes to the saved route being edited", () => {
    const grouped = [step("s1", "agent", "A", { group: { id: "g", name: "Mine", plan_id: "p_mine" } })];
    expect(G.planToSave("Typed", grouped, plans)).toMatchObject({ id: "p_mine", name: "Mine" });
    expect(G.planToSave("Typed", grouped, plans, { fresh: true })).toMatchObject({ id: "", name: "Typed" });
    expect(G.planToSave("x", [step("s1", "ship", "B")], plans, { into: "p_mine" })).toMatchObject({ id: "p_mine", name: "Mine", description: "d" });
    expect(G.planToSave("x", [step("s1", "ship", "B")], plans, { into: "p_gone" })).toMatchObject({ id: "", name: "x" });
  });

  it("the toast says routes already running keep their own copy", () => {
    expect(G.savedWords("Mine", true)).toBe("Updated “Mine” — routes already running keep their own copy");
    expect(G.savedWords("New", false)).toBe("Saved “New” — routes already running keep their own copy");
  });

  it("Edit its stops: the marks read safely, and name the saved route only while it exists", () => {
    expect(G.readRouteEdits('{"f_1":"p_mine","f_2":3}')).toEqual({ f_1: "p_mine" });
    expect(G.readRouteEdits("not json")).toEqual({});
    expect(G.readRouteEdits("[1]")).toEqual({});
    expect(G.editedPlanOf("f_1", { f_1: "p_mine" }, plans)?.name).toBe("Mine");
    expect(G.editedPlanOf("f_1", { f_1: "p_gone" }, plans)).toBeNull();
    expect(savedMenuItems(plans[0], { edit: true }).map((i) => i.label)).toEqual(["Rename", "Edit its stops", "What starts it…", "Take off the shelf", "Delete"]);
    const sky = code("components/flights/SkyView.tsx");
    expect(sky).toContain("onEditPlan={(plan) => void editPlan(plan)}");
    expect(sky).toContain("markRouteEdit(f.id, plan.id);");
    const g = code("components/flights/GroundedLine.tsx");
    expect(g).toContain("planToSave(asked ?? name, flight.steps, p.plans as FlightPlan[], asked !== undefined ? { fresh: true } : { into: editing?.id })");
  });

  it("the name is asked inline (an InlineConfirm with a field), never window.prompt", () => {
    const html = renderToStaticMarkup(createElement(SaveRouteAsk, { initial: "Commit + 2 more", plans, onSave() {}, onCancel() {} }));
    expect(html).toContain('role="alertdialog" aria-label="Save as a route"');
    expect(html).toContain('aria-label="The saved route&#x27;s name"');
    expect(html).toContain('value="Commit + 2 more"');
    expect(html).toContain("routes already running keep their own copy");
    expect(html).toContain(">Save<");
    for (const f of ["components/flights/StopShelf.tsx", "components/flights/GroundedLine.tsx", "components/flights/SkyLine.tsx"]) expect(code(f)).not.toMatch(/\bwindow\.(prompt|alert|confirm)\(/);
  });

  it("a session's route menu offers Save as a route…; a route with no session asks for the name before it saves", () => {
    const line = code("components/flights/SkyLine.tsx");
    expect(line).toContain('label: "Save as a route…"');
    expect(line).toContain("planToSave(asked, routeSteps, p.plans, { fresh: true })");
    const g = code("components/flights/GroundedLine.tsx");
    expect(g).toContain('{ key: "save", label: "Save as a route…"');
    expect(g).toContain("onSelect: () => setAsking(name) }");
  });

  it("the empty Sky's line says what a route is (the New session button rides it: SkyEmpty newSession)", () => {
    expect(code("components/flights/SkyView.tsx")).toContain("No sessions in the sky yet — a route is the stops a session's work goes through, from the agent to the PR.");
  });

  it("the shelf with no saved route says what a route is, in one sentence, and offers Start a route", () => {
    expect(NO_SAVED_ROUTES.split(/[.!?](\s|$)/).filter((x) => x.trim()).length).toBe(1);
    expect(NO_SAVED_ROUTES).toContain("a route is");
    expect(code("components/flights/StopShelf.tsx")).toContain('<span className="sky-stop-name">Start a route</span>');
  });
});

describe("6. the tools say what a drag does, with their keys", () => {
  it("Select (V) · Pan (H), the keys SkyView binds", () => {
    // (skyTool.test renders them; here, that each names the key bound to it.)
    const sky = code("components/flights/SkyView.tsx");
    expect(sky).toContain('drag: { label: "Select (V)", title: "Select (V): ');
    expect(sky).toContain('click: { label: "Pan (H)", title: "Pan (H): ');
    expect(sky).toMatch(/k === "v" \|\| k === "V"\) \{\s*handled\(\);\s*setTool\("drag"\);/);
    expect(sky).toMatch(/k === "h" \|\| k === "H"\) \{\s*handled\(\);\s*setTool\("click"\);/);
  });
});
