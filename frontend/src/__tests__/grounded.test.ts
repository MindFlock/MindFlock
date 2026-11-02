/** Grounded lines (docs/flights.md "Grounded flights"): stops on the board
 * with no session yet. The pure rules (lib/groundedLines: which flights, in
 * what order, under which layout key, the diagram with every stop editable,
 * what Save to the shelf saves), the line itself server-rendered (its bar,
 * the dashed "No session yet" head, its ⋯), and how the Sky hosts them (after
 * the session lines, drops on empty sky, the Session chip on one, the line
 * picker → attach) and makes a new session's box appear at once. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { code } from "./sourceText";
import type { Flight, FlightPlan, FlightStep, StepKindDef } from "../lib/flights";

vi.mock("../api/client", () => ({ api: vi.fn(() => new Promise(() => {})), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const G = await import("../lib/groundedLines");
const { GroundedLine, groundedName } = await import("../components/flights/GroundedLine");
const { readDrop } = await import("../components/flights/useGroundedLine");

(globalThis as Record<string, unknown>).document ??= {
  hidden: false,
  addEventListener: () => {},
  removeEventListener: () => {},
  getElementById: () => null,
  querySelector: () => null,
};

const kind = (k: string, label: string, actor: "robot" | "you" = "robot"): StepKindDef => ({
  kind: k,
  label,
  actor,
  summary: label + " does its thing",
  fields: [{ name: "lane", label: "Lane", type: "text", default: "push" } as StepKindDef["fields"][number]],
  default_title: label,
});
const KINDS = [kind("agent", "Agent"), kind("ship", "Ship"), kind("you", "You", "you")];

const step = (id: string, k: string, title: string, group?: FlightStep["group"]): FlightStep => ({ id, kind: k, title, config: {}, on_fail: "halt", ...(group ? { group } : {}) });

const flight = (over: Partial<Flight> = {}): Flight => ({
  id: "f_1",
  plan_id: "adhoc",
  plan_name: "Stops",
  steps: [step("s1", "agent", "Fix it"), step("s2", "ship", "Push")],
  subject: { kind: "manual", source: "", id: "", title: "", url: "" },
  session: "",
  repo_root: "",
  branch: "",
  state: "grounded",
  cursor: 0,
  step_states: {},
  reason: "",
  history: [],
  started: 100,
  updated: 100,
  finished: 0,
  by: "",
  ...over,
});

const plan = (id: string, name: string, over: Partial<FlightPlan> = {}): FlightPlan => ({
  id,
  name,
  description: "",
  builtin: false,
  trigger: { kind: "manual", schedule: null, paused: false },
  session: { repo_path: "", program: "", model: "", in_place: false },
  steps: [step("s1", "agent", "Fix it")],
  updated: 1,
  ...over,
});

describe("lib/groundedLines", () => {
  it("a grounded line's layout key is flight:<id>, never a session title", () => {
    expect(G.groundedKey("f_9")).toBe("flight:f_9");
    expect(G.isGroundedKey("flight:f_9")).toBe(true);
    expect(G.isGroundedKey("sc-12-fix")).toBe(false);
    expect(G.groundedIdOf("flight:f_9")).toBe("f_9");
    expect(G.groundedIdOf("sc-12")).toBe("");
  });

  it("only grounded flights, newest first", () => {
    const fs = [flight({ id: "a", started: 1 }), flight({ id: "b", state: "running", session: "x", started: 5 }), flight({ id: "c", started: 9 }), flight({ id: "d", state: "done", started: 10 })];
    expect(G.groundedFlights(fs).map((f) => f.id)).toEqual(["c", "a"]);
    expect(G.groundedFlights(null)).toEqual([]);
  });

  it("its diagram: every stop editable and pending, a trailing +, inserts anywhere", () => {
    const d = G.groundedDiagram(flight(), KINDS);
    const stops = d.nodes.filter((n) => n.kind === "stop");
    expect(stops.map((n) => n.title)).toEqual(["Fix it", "Push"]);
    expect(stops.every((n) => n.editable && n.state === "pending")).toBe(true);
    expect(d.nodes[d.nodes.length - 1].kind).toBe("add");
    expect(d.edges.some((e) => e.to === "add" && e.kind === "add")).toBe(true);
    expect(d.flightId).toBe("f_1");
    expect(d.mode).toBe("flight");
    expect(d.insertFrom).toBe(0);
  });

  it("a kind's default stop is the palette's: its default title and field defaults", () => {
    const s = G.defaultStepFor(KINDS[1]);
    expect(s).toMatchObject({ kind: "ship", title: "Ship", config: { lane: "push" }, on_fail: "halt" });
  });

  it("Save to the shelf updates the saved stop every step came from, else makes a new one named after the line", () => {
    const plans = [plan("p_mine", "Mine", { description: "kept", trigger: { kind: "schedule", schedule: { at: "09:00", days: "daily" }, paused: false } })];
    const g = { id: "g1", name: "Mine", plan_id: "p_mine" };
    const from = [step("s1", "agent", "A", g), step("s2", "ship", "B", g)];
    expect(G.sourcePlanId(from, plans)).toBe("p_mine");
    const up = G.planToSave("Mine", from, plans);
    expect(up.id).toBe("p_mine");
    expect(up.description).toBe("kept");
    expect(up.trigger.kind).toBe("schedule");
    expect(up.steps.map((s) => s.title)).toEqual(["A", "B"]);
    // The group is the board's, not the saved stop's.
    expect(up.steps.every((s) => !("group" in s))).toBe(true);
    // A mixed line, an ad-hoc one, or a saved stop that's gone: a new one.
    expect(G.sourcePlanId([...from, step("s3", "ship", "C")], plans)).toBe("");
    expect(G.sourcePlanId([step("s1", "agent", "A", { id: "g", name: "x", plan_id: "adhoc" })], plans)).toBe("");
    expect(G.sourcePlanId([step("s1", "agent", "A", { id: "g", name: "x", plan_id: "p_gone" })], plans)).toBe("");
    const fresh = G.planToSave("My stops", [step("s1", "agent", "A")], plans);
    expect(fresh).toMatchObject({ id: "", name: "My stops", builtin: false });
  });

  it("Delete and Backspace ask to remove the line", () => {
    expect(G.isRemoveKey("Delete")).toBe(true);
    expect(G.isRemoveKey("Backspace")).toBe(true);
    expect(G.isRemoveKey("x")).toBe(false);
  });
});

describe("the grounded line", () => {
  const html = renderToStaticMarkup(
    createElement(GroundedLine, {
      flight: flight({ plan_name: "Ticket to merge" }),
      kinds: KINDS,
      plans: [],
      layout: { x: 0, y: 0, size: "normal", open: false },
      focused: false,
      onFocus() {},
      zoom: 1,
      onFlyOn() {},
    })
  );

  it("its bar names it (the flight's plan_name) and says it has no session", () => {
    expect(html).toContain('data-flight="f_1"');
    expect(html).toContain('class="skl-name skl-name-edit" title="Click to rename (F2)">Ticket to merge<');
    expect(html).toContain(">No session<");
    expect(html).toContain('aria-label="Ticket to merge — stops with no session yet"');
    expect(html).toContain('tabindex="0"');
    expect(groundedName({ plan_name: "" })).toBe("Stops");
  });

  it("round 18: no head box — a headless chain of its stops under a small label", () => {
    expect(html).not.toContain("skl-grounded-head");
    expect(html).not.toContain("No session yet");
    expect(html).toContain('class="skl skl-chainline skl-grounded"');
    expect(html).toContain('data-tour="sky-grounded"');
    expect(html).toContain('data-chain="flight:f_1"');
    expect(html.indexOf("skl-label")).toBeLessThan(html.indexOf("Fix it"));
    expect(html).toContain("Push");
    const src = code("components/flights/GroundedLine.tsx");
    expect(src).toContain('chainGraph(moves.diagram, { head, key: "flight:" + flight.id, flightId: flight.id })');
    // The empty socket on its first tile is a tour hook too.
    expect(src).toContain('useTourHooks(wrapRef, { ".sky-socket": "sky-grounded" });');
  });

  it("round 18: a chain another hands off to wears its trigger head and says so", () => {
    const head = { kind: "trigger" as const, title: "When me reaches Start a chain", sub: "runs on a new session", session: "new", color: "hsl(1 70% 60%)", from: { flight: "f_0", step: "s2", title: "Start a chain" } };
    const t = renderToStaticMarkup(
      createElement(GroundedLine, {
        flight: flight({ plan_name: "Ticket to merge" }),
        kinds: KINDS,
        plans: [],
        layout: { x: 0, y: 0, size: "normal", open: false },
        focused: false,
        onFocus() {},
        zoom: 1,
        onFlyOn() {},
        head,
      })
    );
    expect(t).toContain(">Started by a chain<");
    expect(t).toContain("is-triggered");
    expect(t).toContain('aria-label="Ticket to merge — When me reaches Start a chain"');
  });

  it("its ⋯: Add a stop…, Fly on a session…, Save to the shelf, Remove — Remove at once, undoable (round 14: a grounded line never runs)", () => {
    const src = code("components/flights/GroundedLine.tsx");
    for (const l of ['label: "Add a stop…"', 'label: "Fly on a session…"', 'label: "Save to the shelf"', 'label: "Remove", danger: true'])
      expect(src).toContain(l);
    expect(src).toContain("onSelect: () => void remove() },");
    expect(src).not.toContain("<InlineConfirm");
    expect(src).not.toContain("setConfirming");
    // The host removes it (and pins the Undo where it sat); alone, it deletes and pushes its own entry.
    expect(src).toContain("if (p.onRemove) await p.onRemove();");
    expect(src).toContain("else if (await deleteFlight(flight.id)) {");
    expect(src).toMatch(/const entry = pushUndo\(\{ label: "Remove “" \+ name \+ "”", undo: \(\) => startGrounded\(/);
    expect(src).toContain('toast("Removed “" + name + "”", { action: undoAction(entry,');
    expect(src).toContain("const saved = await savePlan(plan);");
    expect(src).toMatch(/if \(e\.target !== e\.currentTarget\) return;[\s\S]*?if \(!isRemoveKey\(e\.key\)\) return;/);
    expect(src).not.toMatch(/\bwindow\.(prompt|alert|confirm)\(/);
  });

  it("edits through the flight routes, as a session's line does — every stop can change", () => {
    const src = code("components/flights/useGroundedLine.tsx");
    for (const f of ["dropStop(id, step.id)", "moveStop(flight.id, step.id, to)", "tackOn(flight.id, step, where(at))", "tackOnPlan(flight.id, plan.id, where(at))", "setHalt(flight.id, step.id, !step.halt)", "patchStop(flight.id, step.id, patch)", "<StopDialog"])
      expect(src).toContain(f);
    expect(readDrop("ship")).toEqual({ kind: "ship" });
    expect(readDrop({ planId: "p_1" })).toEqual({ planId: "p_1" });
  });
});

describe("the Sky hosts them", () => {
  const sky = code("components/flights/SkyView.tsx");

  it("after the session lines, keyed flight:<id>, not in This session or Perched on you", () => {
    expect(sky).toContain("if (mode === \"one\" || mode === \"perched\") return [];");
    expect(sky.indexOf("...titles.map((t) => {")).toBeLessThan(sky.indexOf("...grounded.map((f) => {"));
    expect(sky).toContain("const id = groundedKey(f.id);");
    expect(sky).toContain("<GroundedLine");
  });

  it("a chip on empty sky: stops with no session yet, pinned where it fell", () => {
    expect(sky).toContain('if ("plan" in d) return startGrounded({ plan_id: d.plan.id });');
    expect(sky).toContain("return def ? startGrounded({ steps: [defaultStepFor(def)] }) : null;");
    expect(sky).toContain("setLayout((l) => patchSkyLine(l, groundedKey(f.id), { x: at.x, y: at.y, pinned: true }));");
  });

  it("a Session chip on one opens a block that makes its session; ⋯ Fly on a session… picks a line → attach", () => {
    expect(sky).toContain("setNewAt({ x: p.x, y: Math.max(SKY_MARGIN, p.y - 40), attach: { id: f.id, name: groundedName(f) } });");
    expect(sky).toContain("void attachGrounded(f.id, { title });");
    const block = code("components/flights/NewSessionBlock.tsx");
    expect(block).toContain("attach ? await attachGrounded(attach.id, { title })");
  });

  it("the line picker shows names, never raw titles", () => {
    expect(sky).toMatch(/<button key=\{t\} type="button" className="fl-chip" title=\{t\} onClick=\{\(\) => asking\.on\(t\)\}>\s*\{windowName\(t\)\}/);
  });
});

describe("a new session's box appears at once", () => {
  const sky = code("components/flights/SkyView.tsx");
  const block = code("components/flights/NewSessionBlock.tsx");

  it("the block writes the optimistic row and tells the sky before the server answers", () => {
    expect(block.indexOf("onPending?.(guess);")).toBeGreaterThan(block.indexOf("const guess = addPendingSession("));
    expect(block.indexOf("onPending?.(guess);")).toBeLessThan(block.indexOf('await api<Instance>("/api/instances"'));
    expect(block).toContain("failPendingSession(guess);");
    expect(block).toContain("onFailed?.();");
  });

  it("the sky pins the pending row's line where the block sat, hides the block, and carries the spot to the real title", () => {
    expect(sky).toContain("setLayout((l) => patchSkyLine(l, guess, { x: newAt.x, y: newAt.y, pinned: true }));");
    expect(sky).toContain("hidden={!!newAt.flying}");
    expect(sky).toContain("if (guess && guess !== title) setLayout((l) => tidySkyLayout(l, guess));");
    // A pending row is a line like any other (skyLineTitles keeps it).
    expect(code("lib/skyLayout.ts")).toContain("const inSky = (r: Pick<Instance, \"hidden_by\">) => !r.hidden_by;");
  });
});

describe("round 13: renaming a grounded line", () => {
  const src = code("components/flights/GroundedLine.tsx");

  it("a click on its name (not the end of a drag) or F2 edits it inline; never prompt()", () => {
    expect(src).toContain('className="skl-name skl-name-edit" title="Click to rename (F2)" onClick={onNameClick}');
    expect(src).toContain("if (at && Math.hypot(e.clientX - at.x, e.clientY - at.y) >= 4) return;");
    expect(src).toMatch(/if \(e\.key === "F2"\) \{\s*e\.preventDefault\(\);\s*e\.stopPropagation\(\);\s*startRename\(\);/);
    expect(src).toContain('className="fl-input skl-name-input"');
    expect(src).not.toMatch(/\bprompt\(/);
  });

  it("Enter and blur save through renameFlight, Esc cancels; the edit ends once", () => {
    expect(src).toContain("onBlur={() => endRename(true)}");
    expect(src).toMatch(/if \(e\.key === "Enter"\) \{\s*e\.preventDefault\(\);\s*endRename\(true\);/);
    expect(src).toMatch(/else if \(e\.key === "Escape"\) \{\s*e\.preventDefault\(\);\s*endRename\(false\);/);
    expect(src).toContain("if (save && next && next !== flight.plan_name) void renameFlight(flight.id, next);");
    expect(src).toMatch(/if \(named\.current\) return;\s*named\.current = true;/);
  });

  it("the name is trimmed and capped at the server's 120", async () => {
    const { cleanFlightName } = await import("../components/flights/GroundedLine");
    expect(cleanFlightName("  Ship it  ")).toBe("Ship it");
    expect(cleanFlightName("   ")).toBe("");
    expect(cleanFlightName("x".repeat(200))).toHaveLength(120);
  });
});
