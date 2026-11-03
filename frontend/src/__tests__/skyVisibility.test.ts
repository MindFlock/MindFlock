/** The Sky's readability sweep (docs/flights-visibility.md): the roomy box
 * tier, the stops a flight has flown folded into one pill (and the canvas's
 * stop indexes translated round it), where the flight is in words on the
 * line's bar, the ⋯ menu's groups, and the type / contrast rules pinned in
 * the stylesheets. */

/// <reference types="vite/client" />
import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { code, css, cssRule } from "./sourceText";
import type { Instance } from "../api/types";
import type { Flight, FlightPlan, FlightStep, StepKindDef } from "../lib/flights";
import type { Diagram, DiagramNode } from "../lib/flightDiagram";
import type { StopDrop } from "../lib/stopEditing";

vi.mock("../api/client", () => ({ api: vi.fn(() => new Promise(() => {})), instApi: vi.fn(() => new Promise(() => {})), ApiError: class extends Error {} }));

vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const { QueryClient, QueryClientProvider } = await import("@tanstack/react-query");
const { useUi } = await import("../state/store");
const { SkyLine, flownCanvas } = await import("../components/flights/SkyLine");
const { FLOWN_MIN, boxMetrics, collapseFlown, drawnIndexes, flownCount, layoutDiagram, lineWhere, sessionDiagram, sizedBoxFor } = await import("../lib/flightDiagram");

(globalThis as Record<string, unknown>).document ??= {
  hidden: false,
  addEventListener: () => {},
  removeEventListener: () => {},
  getElementById: () => null,
  querySelector: () => null,
};

const step = (id: string, kind: string, title: string, config: Record<string, unknown> = {}): FlightStep => ({ id, kind, title, config, on_fail: "halt" });
const STEPS = [
  step("s1", "agent", "Work the ticket"),
  step("s2", "ship", "Commit", { lane: "commit" }),
  step("s3", "ship", "Push", { lane: "push" }),
  step("s4", "reviews", "Reviews settle", { quiet_minutes: 10 }),
  step("s5", "you", "Approve the merge"),
  step("s6", "ship", "Merge", { lane: "merge" }),
];
const KINDS: StepKindDef[] = [
  { kind: "agent", label: "Agent", actor: "robot", summary: "", fields: [], default_title: "Agent" },
  { kind: "ship", label: "Ship", actor: "robot", summary: "", fields: [], default_title: "Ship" },
  { kind: "reviews", label: "Reviews settle", actor: "robot", summary: "", fields: [], default_title: "Reviews settle" },
  { kind: "you", label: "Perch", actor: "you", summary: "", fields: [], default_title: "Perch" },
];
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

/** A flight at `cursor`: the stops before it in `before` (done by default). */
function flight(cursor: number, before: Record<string, string> = {}, state = "running"): Flight {
  const step_states: Flight["step_states"] = {};
  STEPS.forEach((s, i) => {
    const st = i < cursor ? before[s.id] || "done" : i === cursor ? state : "pending";
    step_states[s.id] = { state: st as Flight["step_states"][string]["state"], since: 1, note: "", detail: {}, rounds: 0 };
  });
  return {
    id: "f_1",
    plan_id: PLAN.id,
    plan_name: PLAN.name,
    steps: STEPS,
    subject: { kind: "manual", source: "", id: "", title: "", url: "" },
    session: "me",
    repo_root: "/r",
    branch: "b",
    state,
    cursor,
    step_states,
    reason: state === "halted" ? "Checks went red" : "",
    history: [],
    started: 1,
    updated: 1,
    finished: 0,
    by: "",
  } as Flight;
}

const rowOf = (title: string, over: Record<string, unknown> = {}) =>
  ({ title, status: "running", activity: "working", stage: "agent", task_headline: "Do " + title, parent: "", branch: title, flight: null, autopilot: null, ...over }) as unknown as Instance;

const flightRow = (cursor: number, state = "running") => {
  const s = STEPS[cursor];
  return { id: "f_1", plan_name: PLAN.name, cursor, total: STEPS.length, state, step: { id: s.id, kind: s.kind, title: s.title, state, actor: s.kind === "you" ? "you" : "robot" } };
};
const me = (cursor: number, state = "running", over: Record<string, unknown> = {}) => rowOf("me", { flight: flightRow(cursor, state), ...over });
// Round 11: flown stops stay as boxes unless the line asks to fold them.
const diagramAt = (cursor: number, opts: { before?: Record<string, string>; rows?: Instance[]; fold?: boolean; state?: string } = {}) =>
  sessionDiagram(opts.rows || [me(cursor, opts.state)], "me", { flights: [flight(cursor, opts.before, opts.state)], kinds: KINDS, foldFlown: opts.fold });
const mainIds = (d: Diagram) => d.nodes.filter((n) => n.row === "main").map((n) => n.id);
const edge = (d: Diagram, from: string, to: string) => d.edges.find((e) => e.from === from && e.to === to);

// --- The roomy tier ------------------------------------------------------------------

describe("the roomy box tier (the Sky)", () => {
  it("boxMetrics' roomy tier is bigger; the base tier is today's", () => {
    const base = boxMetrics();
    expect(base.box).toEqual({ w: 178, h: 76 });
    expect(base.add).toEqual({ w: 148, h: 40 });
    expect(base.branch).toEqual({ w: 156, h: 52 });
    expect(base.gap).toBe(46);
    const roomy = boxMetrics(false, true);
    expect(roomy.box).toEqual({ w: 236, h: 100 });
    expect(roomy.add).toEqual({ w: 180, h: 48 });
    expect(roomy.branch).toEqual({ w: 200, h: 64 });
    expect(roomy.gap).toBe(56);
    // The line's anchor sits half way down the taller box.
    expect(roomy.anchor).toBe(50);
    expect(boxMetrics(true, true).box.w).toBeGreaterThan(boxMetrics(true).box.w);
  });

  it("layoutDiagram takes `roomy`; without it the geometry is unchanged", () => {
    const d = diagramAt(3);
    const plain = layoutDiagram(d);
    const big = layoutDiagram(d, { roomy: true });
    const stop = (l: typeof plain) => l.boxes.find((b) => b.node.id === "stop:s4")!;
    expect([stop(plain).w, stop(plain).h]).toEqual([178, 76]);
    expect([stop(big).w, stop(big).h]).toEqual([236, 100]);
    expect(big.gap).toBe(56);
    expect(big.width).toBeGreaterThan(plain.width);
  });

  it("sizedBoxFor: the ◧ sizes at the roomy tier; the + and the flown pill keep the layout's", () => {
    const base = { w: 236, h: 100 };
    const stop = { id: "stop:s1", kind: "stop", row: "main", title: "T", sub: "a summary", state: "pending", icon: "agent" } as DiagramNode;
    expect(sizedBoxFor("normal", stop, base, "", true)).toEqual(base);
    expect(sizedBoxFor("compact", stop, base, "", true)).toEqual({ w: 150, h: 64 });
    expect(sizedBoxFor("wide", stop, base, "a detail", true).w).toBe(330);
    // The base tier is FlywayCanvas's old sizedBox, number for number.
    expect(sizedBoxFor("compact", stop, { w: 178, h: 76 })).toEqual({ w: 110, h: 62 });
    expect(sizedBoxFor("wide", stop, { w: 178, h: 76 }, "d")).toEqual({ w: 260, h: 76 + 18 + 15 + 32 });
    const pill = { ...stop, id: "flown", kind: "flown" } as DiagramNode;
    expect(sizedBoxFor("wide", pill, { w: 148, h: 56 }, "", true)).toEqual({ w: 148, h: 56 });
  });
});

// --- What has flown -------------------------------------------------------------------

describe("the stops a flight has flown fold into one pill — when the line asks (foldFlown)", () => {
  it("by default every flown stop stays a box (round 11)", () => {
    const d = diagramAt(3);
    expect(mainIds(d)).toEqual(["session:me", "stop:s1", "stop:s2", "stop:s3", "stop:s4", "stop:s5", "stop:s6", "add"]);
    expect(flownCount(d)).toBe(0);
    // `expandFlown: false` is the older spelling of foldFlown.
    const old = sessionDiagram([me(3)], "me", { flights: [flight(3)], kinds: KINDS, expandFlown: false });
    expect(flownCount(old)).toBe(3);
    expect(flownCount(sessionDiagram([me(3)], "me", { flights: [flight(3)], kinds: KINDS, expandFlown: true }))).toBe(0);
  });

  it(FLOWN_MIN + " or more done stops before the current one: one `flown` node", () => {
    const d = diagramAt(3, { fold: true });
    expect(mainIds(d)).toEqual(["session:me", "flown", "stop:s4", "stop:s5", "stop:s6", "add"]);
    const pill = d.nodes.find((n) => n.kind === "flown")!;
    expect(pill).toMatchObject({ id: "flown", kind: "flown", row: "main", title: "3 done", sub: "Work the ticket … Push", state: "done", count: 3 });
    expect(pill.flown).toEqual(["Work the ticket", "Commit", "Push"]);
    // The line chains through it.
    expect(edge(d, "session:me", "flown")?.kind).toBe("line");
    expect(edge(d, "flown", "stop:s4")?.kind).toBe("line");
    expect(d.edges.some((e) => e.from.startsWith("stop:s1") || e.to === "stop:s2")).toBe(false);
    // The flight's own numbers are untouched.
    expect(d.insertFrom).toBe(4);
    expect(d.nodes.find((n) => n.id === "stop:s5")?.index).toBe(4);
    expect(flownCount(d)).toBe(3);
  });

  it("a single flown stop stays as it is", () => {
    const d = diagramAt(1, { fold: true });
    expect(mainIds(d)).toEqual(["session:me", "stop:s1", "stop:s2", "stop:s3", "stop:s4", "stop:s5", "stop:s6", "add"]);
    expect(flownCount(d)).toBe(0);
  });

  it("unfolded, every stop is drawn", () => {
    const d = diagramAt(3);
    expect(mainIds(d)).toContain("stop:s1");
    expect(d.nodes.some((n) => n.kind === "flown")).toBe(false);
  });

  it("skipped stops fold too; a failed one breaks the run and stays in sight", () => {
    expect(diagramAt(3, { fold: true, before: { s2: "skipped" } }).nodes.find((n) => n.kind === "flown")?.count).toBe(3);
    const d = diagramAt(3, { fold: true, before: { s2: "failed" } });
    expect(d.nodes.some((n) => n.kind === "flown")).toBe(false);
    expect(mainIds(d)).toContain("stop:s2");
  });

  it("from the row alone (no full flight cached): the numbered stops fold the same way", () => {
    const d = sessionDiagram([me(3)], "me", { foldFlown: true });
    const pill = d.nodes.find((n) => n.kind === "flown")!;
    expect(pill.title).toBe("3 done");
    expect(pill.sub).toBe("Stop 1 … Stop 3");
    expect(mainIds(d)).toEqual(["session:me", "flown", "stop:i3", "stop:i4", "stop:i5", "add"]);
  });

  it("a branch that fanned out of a folded stop fans out of the pill and joins past it", () => {
    const kid = rowOf("w1", { parent: "me", activity: "idle", flight: { branch: { parent: "me", step: "s2" } }, last_report: { id: "m", status: "done", summary: "ok", ts: 1 } });
    const d = diagramAt(3, { fold: true, rows: [me(3), kid] });
    const b = d.nodes.find((n) => n.id === "branch:w1")!;
    expect(b.branch?.from).toBe("flown");
    expect(b.branch?.to).toBe("stop:s4");
    expect(edge(d, "flown", "branch:w1")?.kind).toBe("fan");
    expect(edge(d, "branch:w1", "stop:s4")).toMatchObject({ kind: "join", done: true });
    // The line reaches the next box THROUGH the branch.
    expect(edge(d, "flown", "stop:s4")).toBeUndefined();
    expect(d.edges.some((e) => e.from === e.to)).toBe(false);
  });

  it("collapseFlown on a line with nothing flown is the line itself", () => {
    const d = diagramAt(0, { fold: true });
    expect(collapseFlown(d)).toBe(d);
  });

  it("the pill is the layout's own size, centred on the line's anchor", () => {
    const d = diagramAt(3, { fold: true });
    for (const roomy of [false, true]) {
      const m = boxMetrics(false, roomy);
      const l = layoutDiagram(d, { roomy, sizeOf: (_n, b) => ({ w: b.w + 99, h: b.h }) });
      const p = l.boxes.find((b) => b.node.kind === "flown")!;
      expect([p.w, p.h]).toEqual([m.flown.w, m.flown.h]);
      const s = l.boxes.find((b) => b.node.id === "session:me")!;
      expect(p.y + p.h / 2).toBe(s.y + m.anchor);
    }
  });

  it("a session's line (useSessionLine) folds only on request: its own foldFlown, off by default", () => {
    expect(code("components/flights/useSessionLine.tsx")).toMatch(/sessionDiagram\(rows, title, \{ flights, kinds, family, foldFlown, expandedGroups \}\)/);
    expect(code("components/flights/useSessionLine.tsx")).toContain("const [foldFlown, setFoldFlown] = useState(false);");
    expect(code("components/flights/SessionFlight.tsx")).toMatch(/useSessionLine\(title/);
  });
});

describe("the canvas counts drawn stops; the flight its indexes (flownCanvas)", () => {
  const d = diagramAt(3, { fold: true });
  const calls: unknown[][] = [];
  const canvas = {
    onMove: (a: number, b: number) => calls.push(["move", a, b]),
    onInsert: (at: number) => calls.push(["insert", at]),
    onDropStop: (k: StopDrop, at: number | null) => calls.push(["drop", k, at]),
    onRemove: (n: DiagramNode) => calls.push(["remove", n.index]),
    onSelect: (n: DiagramNode) => calls.push(["select", n.index]),
  };
  const shown = flownCanvas(d, canvas);

  it("drawnIndexes: every stop and insertFrom come down by the pill's count", () => {
    expect(shown.diagram.insertFrom).toBe(1);
    expect(shown.diagram.nodes.find((n) => n.id === "stop:s5")?.index).toBe(1);
    expect(drawnIndexes(diagramAt(1))).toEqual(diagramAt(1));
    // Nothing folded, no group box: the line itself.
    const plain = diagramAt(3);
    expect(drawnIndexes(plain)).toBe(plain);
  });

  it("moves, inserts and drops go back to the flight's indexes; nodes to the flight's own", () => {
    calls.length = 0;
    const s5 = shown.diagram.nodes.find((n) => n.id === "stop:s5")!;
    shown.canvas.onMove!(1, 2);
    shown.canvas.onInsert!(2);
    shown.canvas.onDropStop!({ kind: "ship" }, 1);
    shown.canvas.onDropStop!({ kind: "ship" }, null);
    shown.canvas.onRemove!(s5);
    shown.canvas.onSelect!(s5);
    expect(calls).toEqual([
      ["move", 4, 5],
      ["insert", 5],
      ["drop", { kind: "ship" }, 4],
      ["drop", { kind: "ship" }, null],
      ["remove", 4],
      ["select", 4],
    ]);
  });

  it("without a pill the canvas props pass through untouched", () => {
    const plain = flownCanvas(diagramAt(1), canvas);
    expect(plain.canvas).toBe(canvas);
  });
});

// --- Where the flight is, in words -------------------------------------------------------

describe("lineWhere: the bar's 'at <stop>'", () => {
  it("at the current stop, with its summary", () => {
    expect(lineWhere(diagramAt(3))).toBe("at Reviews settle · 10 min quiet");
  });

  it("perched on you, halted with the reason", () => {
    expect(lineWhere(diagramAt(4, { state: "waiting_you" }))).toBe("needs you at Approve the merge · waits for your Done");
    expect(lineWhere(diagramAt(3, { state: "halted" }))).toBe("halted at Reviews settle · Checks went red");
  });

  it("nothing while the bird sits on the session box (its chip says it)", () => {
    expect(lineWhere(sessionDiagram([rowOf("me")], "me"))).toBe("");
  });

  it("a fast-track rung", () => {
    const d = sessionDiagram([rowOf("me", { stage: "committed", autopilot: { depth: "pr", state: "running", step: "", reason: "", note: "" } })], "me");
    expect(lineWhere(d)).toBe("fast-track at Push");
  });
});

// --- The line: its bar and its ⋯ ------------------------------------------------------------

function withClient(el: ReactElement) {
  const c = new QueryClient({ defaultOptions: { queries: { retry: false, enabled: false } } });
  c.setQueryData(["flights"], [flight(3)]);
  c.setQueryData(["flight-kinds"], KINDS);
  c.setQueryData(["config"], { caps: { git: true, tailscale: false, ticketing: false, github: false } });
  return renderToStaticMarkup(createElement(QueryClientProvider, { client: c }, el));
}

afterEach(() => {
  Object.assign(useUi.getInitialState(), useUi.getState());
});

const line = (layout: Record<string, unknown> = {}) =>
  withClient(
    createElement(SkyLine, {
      title: "me",
      rows: [me(3)],
      plans: [PLAN],
      layout: { x: 10, y: 20, size: "normal", open: false, ...layout },
      onLayout: () => {},
      focused: false,
      onFocus: () => {},
      zoom: 1,
    })
  );

describe("a line in the sky", () => {
  it("its bar says where the flight is, in words", () => {
    const html = line();
    expect(html).toMatch(/class="skl-where"[^>]*>at Reviews settle · 10 min quiet</);
  });

  it("draws the flown stops as tiles (round 11: no pill by default)", () => {
    const html = line();
    expect(html).not.toContain('data-node="flown"');
    expect(html).toContain('data-node="stop:s1"');
    // A press on the pill unfolds the line.
    expect(code("components/flights/SkyLine.tsx")).toMatch(/if \(n\.kind === "flown"\) setFoldFlown\(\(x\) => !x\)/);
  });

  it("the ⋯ in groups: the session │ the boxes and the line │ what flies │ closing", () => {
    const html = line();
    const bar = html.slice(html.indexOf('class="skl-menu"'), html.indexOf("</details>", html.indexOf('class="skl-menu"')));
    const labels = [...bar.matchAll(/<button type="button" class="vf-menu-item([^"]*)"[^>]*>([^<]+)<\/button>/g)].map((m) => [m[2], m[1].trim()]);
    expect(labels.map(([l]) => l)).toEqual([
      "Open session",
      "Open in the grid",
      "Show only this chain",
      // Nothing folds by default; the line offers to fold what has flown.
      // (Round 18: a chain's tiles have one size — no box sizes.)
      "Fold finished stops",
      "Pin here",
      "Add a stop…",
      "Close session",
    ]);
    const cls = Object.fromEntries(labels);
    expect(cls["Fold finished stops"]).toContain("skl-mi-group");
    expect(cls["Add a stop…"]).toContain("skl-mi-group");
    expect(cls["Close session"]).toContain("skl-mi-group");
    expect(cls["Close session"]).toContain("vf-menu-danger");
  });

  it("a pinned line offers Tidy instead of Pin; an open one folds back", () => {
    const html = line({ pinned: true, open: false });
    expect(html).toContain(">Tidy this chain<");
    expect(html).not.toContain(">Pin here<");
  });

  it("round 18: no ◧ on a chain's label (its tiles have one size)", () => {
    const html = line({ size: "wide" });
    expect(html).not.toContain('class="skl-size"');
  });
});

// --- The stylesheets ---------------------------------------------------------------------------

describe("type and contrast", () => {
  const sky = css("components/flights/SkyLine.css");
  const canvas = css("components/flights/FlywayCanvas.css");
  const vis = css("components/flights/SkyVisibility.css");
  const lane = css("components/flights/IntakeLane.css");

  it("the line's bar: 42px, a 15px name, a 12.5px chip, the where line 13px", () => {
    expect(cssRule(sky, ".skl-bar")).toMatch(/height: 42px;[\s\S]*font-size: 15px/);
    expect(cssRule(sky, ".skl-bar .skl-chip")).toContain("font-size: 12.5px");
    expect(cssRule(sky, ".skl-where")).toContain("font-size: 13px");
  });

  it("the roomy box type: titles 15px/600, summaries 13px, chips 12px, a 26px icon tile", () => {
    expect(cssRule(sky, ".skl .fl-box:not(.fl-box-open) .fl-box-title")).toMatch(/font-size: 15px;[\s\S]*font-weight: 600/);
    expect(sky).toMatch(/\n\.skl \.fl-box:not\(\.fl-box-open\) :is\(\.fl-box-sub, \.fl-box-detail, \.fl-box-reason\) \{\n  font-size: 13px;/);
    expect(cssRule(sky, ".skl .fl-box:not(.fl-box-open) .fl-box-chip")).toContain("font-size: 12px");
    expect(cssRule(sky, ".skl .fl-box:not(.fl-box-open) .fl-box-icon")).toMatch(/width: 26px;[\s\S]*height: 26px/);
  });

  it("far keeps a 15px title ≥ 11px on screen from 62% down to the 40% floor", () => {
    expect(cssRule(sky, '.skl[data-lod="far"]')).toContain("--sky-far: clamp(1, calc(0.76 / var(--sky-z, 1)), 1.9)");
    for (const z of [0.61, 0.5, 0.4]) expect(15 * Math.min(1.9, Math.max(1, 0.76 / z)) * z).toBeGreaterThanOrEqual(11);
    expect(cssRule(sky, '.skl[data-lod="far"] .fl-box:not(.fl-box-open) .fl-box-title')).toContain("calc(15px * var(--sky-far))");
    // Near wraps more: three lines of title.
    expect(cssRule(sky, '.skl[data-lod="near"] .fl-box:not(.fl-box-open) .fl-box-title')).toContain("-webkit-line-clamp: 3");
  });

  it("states: done is dimmed by its ink (no opacity), current/perched/halted wear 2px outlines, the selected ring is 3px", () => {
    expect(cssRule(canvas, ".fl-st-done")).not.toContain("opacity");
    expect(cssRule(canvas, ".fl-st-done .fl-box-title")).toContain("color-mix(in srgb, var(--text) 78%, var(--panel))");
    expect(cssRule(canvas, ".fl-st-current")).toContain("0 0 0 2px var(--tone-working)");
    expect(cssRule(canvas, ".fl-st-perched")).toMatch(/background: color-mix\(in srgb, var\(--tone-attention\) 14%, var\(--panel-2\)\)[\s\S]*0 0 0 2px var\(--tone-attention\)/);
    expect(cssRule(canvas, ".fl-st-halted")).toContain("0 0 0 2px var(--red)");
    expect(cssRule(canvas, ".fl-box.selected")).toContain("outline-color: var(--accent)");
    expect(cssRule(canvas, ".fl-wire")).toContain("stroke-width: 2");
  });

  it("text inks hold 4.5:1 in light mode: green, red, muted and the drawer's gold", () => {
    expect(cssRule(canvas, ".light")).toMatch(/--fl-ink-working: #1a6f38;[\s\S]*--fl-ink-red: #b23a17;[\s\S]*--fl-ink-muted: #4f5769/);
    expect(cssRule(canvas, ".fl-box-reason")).toContain("var(--fl-ink-red)");
    expect(cssRule(vis, ".light")).toContain("--sky-ink-gold: #7a5a10");
  });

  it("the ⋯ menu: 13.5px items in 32px rows, screen-sized at any zoom, a rule above each group", () => {
    expect(cssRule(sky, ".skl-menu .vf-menu-list .vf-menu-item")).toMatch(/min-height: 32px;[\s\S]*font-size: 13.5px/);
    expect(cssRule(sky, ".skl-menu .vf-menu-list")).toContain("transform: scale(calc(1 / var(--sky-z, 1)))");
    expect(cssRule(sky, ".skl-menu .vf-menu-list .vf-menu-item.skl-mi-group::before")).toContain("border-top: 1px solid var(--border)");
    expect(cssRule(vis, ".sky-line-slot:has(.skl-menu .vf-menu[open])")).toContain("z-index: 40");
  });

  it("the intake lane: 13px cards, a bold id, a 28px Fly; the drawer: 14px bodies, 36px Done / Skip", () => {
    expect(cssRule(lane, ".sky-lane-item-ref")).toMatch(/font-size: 13px;[\s\S]*font-weight: 700/);
    expect(cssRule(lane, ".sky-lane-fly.fl-btn")).toContain("height: 28px");
    expect(cssRule(lane, ".sky-lane-block-name")).toMatch(/font-size: 13px;[\s\S]*-webkit-line-clamp: 2/);
    expect(cssRule(vis, ".sky-drawer-body")).toContain("font-size: 14px");
    expect(cssRule(vis, ".sky-drawer .fl-you-acts .fl-btn")).toContain("min-height: 36px");
    expect(cssRule(vis, ".sky-drawer .fl-fly")).toContain("min-height: 36px");
  });

  it("the controls (ChipRow, Seg, Toggle): 13px, 28–30px tall; the toolbar's chips too", () => {
    expect(cssRule(canvas, ".fl-pick")).toMatch(/min-height: 28px;[\s\S]*font-size: 13px/);
    expect(cssRule(canvas, ".fl-toggle")).toMatch(/min-height: 30px;[\s\S]*font-size: 13px/);
    expect(cssRule(canvas, ".fl-seg-host .rt-seg button")).toMatch(/min-height: 28px;[\s\S]*font-size: 13px/);
    // The toolbar's mode keys: a 28px segmented group (Sky.css, round 11 style).
    const sky = css("components/flights/Sky.css");
    expect(canvas).toMatch(/--fl-ctl-h: 28px;/);
    expect(cssRule(sky, ".sky-modes.fl-chiprow")).toMatch(/height: var\(--fl-ctl-h\);/);
    expect(cssRule(sky, ".sky-toolbar .sky-modes .fl-pick")).toMatch(/font-size: 13px/);
    expect(vis).not.toContain(".sky-toolbar .sky-modes .fl-pick");
  });

  it("SkyLine brings the overrides in", () => {
    expect(code("components/flights/SkyLine.tsx")).toContain('import "./SkyVisibility.css";');
  });
});
