/** Round 15 looks (docs/flights.md "As built: round 15 looks") — the boxes
 * and lines carry information: the risk stripe on a box's left edge, a flown
 * box's receipt under its title (mid and near, never far), the "if" chip on
 * a stop with a `when`, the time ruler under a line's stops (hidden far),
 * ageing (`data-age`, the "3 days still" word), the workers' birds in a V
 * behind the leader, one distinct icon per new stop kind, and a flown
 * checkpoint's "Rewind to here" asking inline — never `window.confirm`. */

/// <reference types="vite/client" />
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { code, css } from "./sourceText";
import type { Flight, FlightStep, StepKindDef, StepRunState } from "../lib/flights";
import type { Diagram, DiagramNode } from "../lib/flightDiagram";

vi.mock("../api/client", () => ({ api: vi.fn(async () => ({})), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));
vi.mock("../lib/errorPop", () => ({ errorPop: vi.fn() }));

const { FlywayCanvas, REWIND_ITEM, canRewind, formationAt, withRewind } = await import("../components/flights/FlywayCanvas");
const { LineRuler } = await import("../components/flights/SkyLine");
const { StopIcon } = await import("../components/flights/Bird");
const { stopLog, shotsOf } = await import("../components/flights/useSessionLine");
const { birdFormation, birdPerches, flightStops, formationSlot, flywayDiagram, receiptOf } = await import("../lib/flightDiagram");
const { segmentsFor } = await import("../lib/lineTime");

(globalThis as Record<string, unknown>).document ??= {
  hidden: false,
  addEventListener: () => {},
  removeEventListener: () => {},
  getElementById: () => null,
  querySelector: () => null,
};

const def = (kind: string, risk?: string[]): StepKindDef => ({ kind, label: kind, actor: "robot", summary: "", fields: [], default_title: kind, ...(risk ? { risk } : {}) });
const KINDS: StepKindDef[] = [def("agent"), def("ship", ["remote"]), def("checkpoint"), def("test"), def("budget", ["spends"])];
const step = (id: string, kind: string, title: string, config: Record<string, unknown> = {}, extra: Record<string, unknown> = {}): FlightStep =>
  ({ id, kind, title, config, on_fail: "halt", ...extra }) as FlightStep;
const rec = (state: StepRunState["state"], detail: Record<string, unknown> = {}): StepRunState => ({ state, since: 1, note: "", detail, rounds: 0 });

const STEPS = [
  step("s1", "test", "Tests"),
  step("s2", "checkpoint", "Checkpoint"),
  step("s3", "ship", "Merge when green", { lane: "merge" }, { when: "diff touches frontend/**" }),
  step("s4", "budget", "Budget", { max_usd: 5 }),
];
const flight = (over: Partial<Flight> = {}): Flight =>
  ({
    id: "f_1",
    plan_id: "p",
    plan_name: "Plan",
    steps: STEPS,
    subject: { kind: "manual", source: "", id: "", title: "", url: "" },
    session: "sess",
    repo_root: "/r",
    branch: "b",
    state: "running",
    cursor: 2,
    step_states: { s1: rec("done", { receipt: "312 passed", passed: 312 }), s2: rec("done", { receipt: "at a1b2c3d", sha: "a1b2c3d" }), s3: rec("running") },
    reason: "",
    history: [
      { at: 0, step: "s1", event: "started", note: "" },
      { at: 720, step: "s1", event: "done", note: "" },
      { at: 720, step: "s2", event: "started", note: "" },
      { at: 725, step: "s2", event: "done", note: "" },
      { at: 725, step: "s3", event: "started", note: "" },
    ],
    started: 0,
    updated: 0,
    finished: 0,
    by: "user",
    ...over,
  }) as Flight;

const diagramOf = (f: Flight): Diagram => {
  const nodes = flightStops(f, KINDS);
  const edges = nodes.slice(1).map((n, i) => ({ from: nodes[i].id, to: n.id, kind: "line" as const }));
  return { nodes, edges, flightId: f.id, mode: "flight", insertFrom: f.cursor + 1 };
};
const render = (d: Diagram, lod?: "far" | "mid" | "near", size?: "compact" | "normal" | "wide") =>
  renderToStaticMarkup(createElement(FlywayCanvas, { diagram: d, label: "Stops", vertical: false, lod, size, bird: null }));
const box = (html: string, id: string) => {
  const i = html.indexOf('data-node="' + id + '"');
  const start = html.lastIndexOf("<div", i);
  const next = html.indexOf('data-node="', i + 1);
  return html.slice(start, next < 0 ? undefined : html.lastIndexOf("<div", next));
};

describe("the risk stripe", () => {
  const d = diagramOf(flight());
  const html = render(d);

  it("one 3px band per tag down the left edge, the tooltip naming them", () => {
    const merge = box(html, "stop:s3");
    expect(merge).toContain('class="fl-risk"');
    expect(merge).toContain('title="Touches the shared remote · Can&#x27;t be undone"');
    expect(merge).toContain('data-risk="remote irreversible"');
    expect(merge).toContain('<i class="fl-risk-remote"></i><i class="fl-risk-irreversible"></i>');
    const budget = box(html, "stop:s4");
    expect(budget).toContain('data-risk="spends"');
    expect(budget).toContain("Spends money");
    // A test stop touches nothing outside: no stripe.
    expect(box(html, "stop:s1")).not.toContain("fl-risk");
  });

  it("is drawn by token: remote the ship tone, irreversible the failed tone, spends the perch gold", () => {
    const sheet = css("components/flights/FlywayCanvas.css");
    expect(sheet).toMatch(/\.fl-risk\s*\{[^}]*width:\s*3px/);
    expect(sheet).toMatch(/--fl-risk-remote:\s*var\(--tone-waiting\)/);
    expect(sheet).toMatch(/--fl-risk-irreversible:\s*var\(--red\)/);
    expect(sheet).toMatch(/--fl-risk-spends:\s*var\(--gold/);
  });
});

describe("receipts", () => {
  const d = diagramOf(flight());

  it("a flown box shows its receipt under the title at mid and near, not far", () => {
    for (const lod of ["mid", "near"] as const) {
      const html = render(d, lod);
      expect(box(html, "stop:s1")).toContain('<div class="fl-box-receipt">312 passed</div>');
      expect(box(html, "stop:s2")).toContain("at a1b2c3d");
    }
    expect(render(d, "far")).not.toContain("fl-box-receipt");
    // The running stop has none yet.
    expect(box(render(d), "stop:s3")).not.toContain("fl-box-receipt");
    // The receipt follows the head, before the summary.
    const b = box(render(d), "stop:s1");
    expect(b.indexOf("fl-box-receipt")).toBeGreaterThan(b.indexOf("fl-box-title"));
  });

  it("receiptOf keeps one line of at most 80 chars, only strings and numbers", () => {
    expect(receiptOf({ receipt: "  PR\n#291  " })).toBe("PR #291");
    expect(receiptOf({ receipt: 200 })).toBe("200");
    expect(receiptOf({ receipt: "x".repeat(100) }).length).toBe(80);
    expect(receiptOf({ receipt: { no: 1 } })).toBe("");
    expect(receiptOf(undefined)).toBe("");
  });

  it("\"What it did\" leads with the receipt and shows the shots as thumbnails that open", () => {
    const f = flight();
    const node = diagramOf(f).nodes.find((n) => n.id === "stop:s1")!;
    const shots = [
      { name: "home-1280", url: "/api/flights/f_1/shots/s1/1.png", width: 1280 },
      { name: "home-390", url: "/api/flights/f_1/shots/s1/2.png", width: 390 },
      { bad: true },
    ];
    const html = renderToStaticMarkup(createElement("div", null, stopLog(node, f, rec("done", { receipt: "312 passed", shots }), undefined)));
    expect(html).toContain("<h3");
    const lead = html.indexOf("What it did");
    const receipt = html.indexOf('class="stop-dialog-receipt"');
    expect(receipt).toBeGreaterThan(lead);
    expect(html).toMatch(/stop-dialog-receipt[^>]*>312 passed</);
    expect(receipt).toBeLessThan(html.indexOf("stop-dialog-hist"));
    expect(html.match(/<img /g)?.length).toBe(2);
    expect(html).toContain('href="/api/flights/f_1/shots/s1/1.png" target="_blank" rel="noreferrer"');
    expect(html).toContain('src="/api/flights/f_1/shots/s1/2.png"');
    expect(html).toContain("home-390 · 390");
    expect(shotsOf({ shots })).toHaveLength(2);
    expect(shotsOf({})).toEqual([]);
    // A receipt alone is enough to show the section.
    expect(renderToStaticMarkup(createElement("div", null, stopLog(node, { ...f, history: [] }, rec("done", { receipt: "exit 0" }), undefined)))).toContain("exit 0");
    expect(stopLog(node, { ...f, history: [] }, rec("done"), undefined)).toBeNull();
  });
});

describe("the “if” chip", () => {
  it("marks a stop with a `when`; the tooltip is the expression; hidden far", () => {
    const d = diagramOf(flight());
    const html = render(d);
    expect(box(html, "stop:s3")).toContain('class="fl-box-if" title="Only when diff touches frontend/**"');
    expect(box(html, "stop:s3")).toMatch(/fl-box-if[^>]*>if</);
    expect(box(html, "stop:s1")).not.toContain("fl-box-if");
    expect(d.nodes.find((n) => n.id === "stop:s3")!.when).toBe("diff touches frontend/**");
    // The editor's boxes carry it too.
    expect(flywayDiagram(STEPS, KINDS).nodes[2].when).toBe("diff touches frontend/**");
    expect(css("components/flights/FlywayCanvas.css")).toMatch(/\.fl-lod-far \.fl-box-if\s*\{\s*display:\s*none/);
  });
});

describe("time under the line", () => {
  it("the ruler: one segment per flown stop, proportional, titled, the total at the right", () => {
    const segs = segmentsFor(flight());
    const html = renderToStaticMarkup(createElement(LineRuler, { segments: segs }));
    expect(html).toContain('class="skl-ruler"');
    expect(html).toContain('aria-label="Time so far: 12 min over 2 stops"');
    expect(html).toContain('title="Tests · 12 min"');
    expect(html).toContain('title="Checkpoint · 5 s"');
    expect(html).toMatch(/skl-ruler-seg skl-ruler-done" style="flex-grow:720"/);
    expect(html).toMatch(/skl-ruler-total[^>]*>12 min</);
    // Nothing flown, nothing drawn.
    expect(renderToStaticMarkup(createElement(LineRuler, { segments: [] }))).toBe("");
  });

  it("is 4px tall and hidden at the far band; the line leaves it out there too", () => {
    const sheet = css("components/flights/SkyLine.css");
    expect(sheet).toMatch(/\.skl-ruler-bar\s*\{[^}]*height:\s*4px/);
    expect(sheet).toMatch(/\.skl\[data-lod="far"\] \.skl-ruler\s*\{\s*display:\s*none/);
    const line = code("components/flights/SkyLine.tsx");
    expect(line).toContain('{row && lod !== "far" && <LineRuler segments={segments} />}');
    expect(line).toContain("segmentsFor(line.flight)");
  });
});

describe("ageing", () => {
  it("the line wears data-age from the row's activity_since and says how long it has been still", () => {
    const line = code("components/flights/SkyLine.tsx");
    expect(line).toContain("data-age={age || undefined}");
    expect(line).toContain("ageOf(row?.activity_since)");
    expect(line).toContain("ageWord(row?.activity_since)");
    expect(line).toMatch(/<span className="skl-age"[^>]*>\s*\{still\}/);
    const sheet = css("components/flights/SkyLine.css");
    expect(sheet).toMatch(/\.skl\[data-age="old"\] \.skl-bar/);
    expect(sheet).toMatch(/\.skl\[data-age="stale"\] \.skl-bar/);
    expect(sheet).toMatch(/\.skl\[data-age="old"\] \.skl-bar,\s*\.skl\[data-age="stale"\] \.skl-bar\s*\{[^}]*--gold/);
  });
});

describe("formation", () => {
  const stop = (id: string, state: DiagramNode["state"]): DiagramNode => ({ id, kind: "stop", row: "main", title: id, sub: "", state, icon: "agent", step: step(id, "agent", id), index: 0 });
  const worker = (title: string, live: "working" | "you"): DiagramNode => ({
    id: "branch:" + title,
    kind: "branch",
    row: "branch",
    title,
    sub: "",
    state: "session",
    icon: "session",
    session: title,
    branch: { from: "stop:a", to: "stop:b", state: "working", inner: "", live },
  });

  it("birdFormation hands each worker's bird its slot in the V behind the leader; birdPerches itself is unchanged", () => {
    const d: Diagram = { nodes: [stop("stop:a", "current"), stop("stop:b", "pending"), worker("w1", "working"), worker("w2", "you"), worker("w3", "working")], edges: [], flightId: "f", mode: "flight", insertFrom: 1 };
    expect(birdPerches(d).every((b) => !b.formation)).toBe(true);
    const birds = birdFormation(birdPerches(d));
    expect(birds[0]).toEqual({ nodeId: "stop:a", tone: "working" });
    expect(birds.slice(1).map((b) => b.formation)).toEqual([
      { dx: -1, dy: -1 },
      { dx: -1, dy: 1 },
      { dx: -2, dy: -2 },
    ]);
    expect(birds[2]).toMatchObject({ child: "w2", tone: "you" });
    expect([0, 1, 2, 3, 4, 5].map(formationSlot)).toEqual([
      { dx: -1, dy: -1 },
      { dx: -1, dy: 1 },
      { dx: -2, dy: -2 },
      { dx: -2, dy: 2 },
      { dx: -3, dy: -3 },
      { dx: -3, dy: 3 },
    ]);
  });

  it("with no leader the workers' birds stay on their boxes (no slot)", () => {
    const d: Diagram = { nodes: [stop("stop:a", "done"), stop("stop:b", "pending"), worker("w1", "working")], edges: [], flightId: "f", mode: "flight", insertFrom: 1 };
    const birds = birdFormation(birdPerches(d));
    expect(birds).toHaveLength(1);
    expect(birds[0].formation).toBeUndefined();
  });

  it("the canvas puts them behind along the line's direction, out to the side", () => {
    expect(formationAt({ x: 100, y: 40 }, { dx: -1, dy: 1 }, 20, false)).toEqual({ x: 82, y: 58 });
    expect(formationAt({ x: 100, y: 40 }, { dx: -2, dy: -2 }, 20, true)).toEqual({ x: 64, y: 4 });
    const canvas = code("components/flights/FlywayCanvas.tsx");
    expect(canvas).toContain("formationAt(leaderAt, bp.formation, 18, vertical)");
    // The class and the name stay what round 12 pinned: formation is a data
    // attribute, purely visual.
    expect(canvas).toContain("formation={!!inV}");
    expect(css("components/flights/FlywayCanvas.css")).toContain(".fl-bird-marker.fl-bird-branch[data-formation]");
    const d: Diagram = { nodes: [stop("stop:a", "current"), stop("stop:b", "pending"), worker("w1", "working")], edges: [{ from: "stop:a", to: "stop:b", kind: "line" }], flightId: "f", mode: "flight", insertFrom: 1 };
    const html = renderToStaticMarkup(createElement(FlywayCanvas, { diagram: d, label: "L", vertical: false, bird: { nodeId: "stop:a", tone: "working" } }));
    expect(html).toMatch(/class="fl-bird-marker tone-working fl-bird-branch"[^>]*data-formation="v"/);
    // Without a leader, the worker's bird sits on its box.
    const idle = renderToStaticMarkup(createElement(FlywayCanvas, { diagram: d, label: "L", vertical: false, bird: null }));
    expect(idle).toContain("fl-bird-branch");
    expect(idle).not.toContain("data-formation");
  });
});

describe("icons", () => {
  const NEW = ["test", "sync", "checkpoint", "land", "budget", "review", "judge", "call", "listen", "someone", "preview", "screenshot"];
  const draw = (kind: string) => renderToStaticMarkup(createElement(StopIcon, { kind }));

  it("every new kind has its own 24×24 stroke glyph in the existing style", () => {
    const fallback = draw("no-such-kind");
    const seen = new Set<string>();
    for (const k of NEW) {
      const html = draw(k);
      expect(html, k).toMatch(/^<svg class="fl-stop-icon" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"/);
      expect(html, k).not.toBe(fallback);
      expect(html, k).not.toContain('<circle cx="12" cy="12" r="6">');
      seen.add(html);
    }
    expect(seen.size).toBe(NEW.length);
  });

  it("the old kinds keep theirs", () => {
    for (const k of ["agent", "ship", "verify", "wait", "reviews", "checks", "command", "ticket", "notify", "split", "join"]) expect(draw(k)).not.toBe(draw("no-such-kind"));
  });
});

describe("rewind to here", () => {
  const checkpoint = (state: DiagramNode["state"]): DiagramNode => ({ id: "stop:s2", kind: "stop", row: "main", title: "Checkpoint", sub: "", state, icon: "checkpoint", step: STEPS[1], index: 1, receipt: "at a1b2c3d" });
  const note = { key: "note" as const, label: "This stop has flown", title: "", note: true };
  const details = { key: "edit" as const, label: "Details", title: "" };

  it("is offered on a flown checkpoint of a live flight only", () => {
    expect(canRewind(checkpoint("done"), "f_1")).toBe(true);
    expect(canRewind(checkpoint("pending"), "f_1")).toBe(false);
    expect(canRewind(checkpoint("current"), "f_1")).toBe(false);
    expect(canRewind(checkpoint("done"), "")).toBe(false);
    expect(canRewind({ ...checkpoint("done"), step: STEPS[0] }, "f_1")).toBe(false);
  });

  it("slots in before the menu's notes, as a danger item", () => {
    expect(withRewind([details, note], checkpoint("done"), "f_1")).toEqual([details, REWIND_ITEM, note]);
    expect(withRewind([details], checkpoint("done"), "f_1")).toEqual([details, REWIND_ITEM]);
    expect(withRewind([details, note], checkpoint("pending"), "f_1")).toEqual([details, note]);
    expect(REWIND_ITEM).toMatchObject({ key: "rewind", label: "Rewind to here", danger: true });
  });

  it("asks inline on the stage and posts the op; the server's refusal shows under the question — never window.confirm", () => {
    const canvas = code("components/flights/FlywayCanvas.tsx");
    expect(canvas).not.toMatch(/window\.confirm|confirm\(/);
    expect(canvas).not.toMatch(/window\.prompt|alert\(/);
    expect(canvas).toContain('"/steps/" + encodeURIComponent(sid) + "/op/rewind"');
    expect(canvas).toContain('className="fl-rewind"');
    expect(canvas).toContain("<InlineConfirm");
    expect(canvas).toContain('confirmLabel="Rewind"');
    expect(canvas).toContain("error: errMsg(err)");
    expect(canvas).toMatch(/<p className="fl-rewind-error" role="alert">/);
    // The menu stays the stage's: counter-scaled like the menu.
    expect(css("components/flights/FlywayCanvas.css")).toMatch(/\.fl-rewind\s*\{[^}]*scale\(calc\(1 \/ var\(--sky-z, 1\)\)\)/);
  });
});
