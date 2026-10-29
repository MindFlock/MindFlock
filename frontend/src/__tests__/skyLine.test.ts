/** A line in the Sky (components/flights/SkyLine, docs/flights-sky-line.md),
 * server-rendered with a mocked API: the box sizes (compact = the title
 * alone, wide = the full text and a detail line), ◧ cycling them, the session
 * box opening into the REAL pane (embedded — the tab strip, the Terminal tab,
 * no grip and no hide −), a flyway dragged over lighting the +, and the
 * host's wiring (onMeasure, onDragStart, onFocus) pinned in the source. */

/// <reference types="vite/client" />
import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { code, css } from "./sourceText";
import type { Instance } from "../api/types";
import type { FlightPlan, FlightStep, StepKindDef } from "../lib/flights";
import type { DiagramNode } from "../lib/flightDiagram";

vi.mock("../api/client", () => ({ api: vi.fn(() => new Promise(() => {})), instApi: vi.fn(() => new Promise(() => {})), ApiError: class extends Error {} }));

vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const { QueryClient, QueryClientProvider } = await import("@tanstack/react-query");
const { useUi } = await import("../state/store");
const { SkyLine, nextSize, clampPane, PANE_MIN_W, PANE_MIN_H } = await import("../components/flights/SkyLine");
const { FlywayCanvas, boxDetail, sizedBox } = await import("../components/flights/FlywayCanvas");
const { Pane } = await import("../components/grid/Pane");
const { flywayDiagram } = await import("../lib/flightDiagram");

// The queries' poll intervals read `document.hidden`; a server render has no
// document, so a quiet one stands in.
(globalThis as Record<string, unknown>).document ??= {
  hidden: false,
  addEventListener: () => {},
  removeEventListener: () => {},
  getElementById: () => null,
  querySelector: () => null,
};

const step = (id: string, kind: string, title: string, config: Record<string, unknown> = {}): FlightStep => ({ id, kind, title, config, on_fail: "halt" });
const STEPS = [
  step("s1", "agent", "Work the ticket all the way through to a green build", { prompt: "Fix the login redirect\nthen run the tests" }),
  step("s2", "ship", "Push", { lane: "push", message: "Fix: login redirect loop" }),
  step("s3", "you", "Approve the merge", { text: "Look at the PR first" }),
];
const KINDS: StepKindDef[] = [
  { kind: "agent", label: "Agent", actor: "robot", summary: "The agent works", fields: [], default_title: "Agent works" },
  { kind: "ship", label: "Ship", actor: "robot", summary: "Ship", fields: [], default_title: "Ship" },
  { kind: "you", label: "Perch", actor: "you", summary: "Waits for you", fields: [], default_title: "Check it" },
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

const rowOf = (title: string, over: Record<string, unknown> = {}) =>
  ({
    title,
    status: "running",
    activity: "idle",
    stage: "agent",
    task_headline: "Do " + title,
    last_prompt: "Make the login page stop looping",
    parent: "",
    branch: title,
    flight: null,
    autopilot: null,
    ...over,
  }) as unknown as Instance;

function withClient(el: ReactElement) {
  const c = new QueryClient({ defaultOptions: { queries: { retry: false, enabled: false } } });
  c.setQueryData(["flights"], []);
  c.setQueryData(["flight-kinds"], KINDS);
  c.setQueryData(["config"], { caps: { git: true, tailscale: false, ticketing: false, github: false } });
  return renderToStaticMarkup(createElement(QueryClientProvider, { client: c }, el));
}

afterEach(() => {
  Object.assign(useUi.getInitialState(), useUi.getState());
});

type Layout = { x: number; y: number; size: "compact" | "normal" | "wide"; open: boolean; paneW?: number; paneH?: number; pinned?: boolean };
const line = (over: Partial<Layout> = {}, extra: Record<string, unknown> = {}, rows = [rowOf("me"), rowOf("w1", { parent: "me" })]) =>
  withClient(
    createElement(SkyLine, {
      title: "me",
      rows,
      plans: [PLAN],
      layout: { x: 0, y: 0, size: "normal", open: false, ...over },
      onLayout: () => {},
      focused: false,
      onFocus: () => {},
      zoom: 1,
      ...extra,
    })
  );

const boxOf = (html: string, id: string) => new RegExp(`<div[^>]*class="(fl-box [^"]*)"[^>]*data-node="${id}"[^>]*style="([^"]*)"`).exec(html);

describe("box sizes", () => {
  const d = flywayDiagram(STEPS, KINDS);
  const render = (size?: "compact" | "normal" | "wide") => renderToStaticMarkup(createElement(FlywayCanvas, { diagram: d, label: "Stops", vertical: false, size }));

  it("normal is the editor's box, untouched by default", () => {
    const html = render();
    expect(html).not.toContain("fl-size-");
    expect(boxOf(html, "stop:s1")?.[2]).toContain("width:178px");
    expect(html).toContain('class="fl-box-sub"');
    expect(html).not.toContain("fl-box-detail");
  });

  it("compact: ~110px boxes, the title alone on one line", () => {
    const html = render("compact");
    expect(html).toContain("fl-size-compact");
    expect(boxOf(html, "stop:s1")?.[2]).toContain("width:110px");
    expect(html).not.toContain('class="fl-box-sub"');
    expect(html).not.toContain("fl-box-detail");
    expect(html).toContain("Work the ticket all the way through");
    expect(css("components/flights/FlywayCanvas.css")).toMatch(/\.fl-size-compact \.fl-box/);
  });

  it("wide: 260px boxes with the full title, the summary and a detail line", () => {
    const html = render("wide");
    expect(html).toContain("fl-size-wide");
    expect(boxOf(html, "stop:s1")?.[2]).toContain("width:260px");
    expect(html).toContain('class="fl-box-sub"');
    // The prompt's first line, the ship stop's commit message, the perch's text.
    expect(html).toContain('<div class="fl-box-detail">Fix the login redirect</div>');
    expect(html).toContain('<div class="fl-box-detail">Fix: login redirect loop</div>');
    expect(html).toContain('<div class="fl-box-detail">Look at the PR first</div>');
    expect(css("components/flights/FlywayCanvas.css")).toMatch(/\.fl-size-wide \.fl-box-title\s*\{[^}]*white-space: normal/);
  });

  it("sizes the layout: wide boxes grow for what they show; the + keeps its size", () => {
    const base = { w: 178, h: 76 };
    const stop = { id: "x", kind: "stop", row: "main", title: "t", sub: "s", state: "pending", icon: "agent" } as DiagramNode;
    expect(sizedBox("normal", stop, base)).toEqual(base);
    expect(sizedBox("compact", stop, base).w).toBe(110);
    expect(sizedBox("wide", stop, base, "more").h).toBeGreaterThan(sizedBox("wide", stop, base).h);
    expect(sizedBox("wide", { ...stop, kind: "add" }, base)).toEqual(base);
    expect(boxDetail({ ...stop, step: STEPS[0] })).toBe("Fix the login redirect");
    expect(boxDetail({ ...stop, step: step("c", "verify", "Verify") })).toBe("");
  });
});

describe("the line", () => {
  it("draws the title bar, the session diagram with its worker, and the +", () => {
    const html = line();
    expect(html).toContain('class="skl skl-size-normal"');
    expect(html).toContain('<span class="skl-name">me</span>');
    expect(html).toContain('data-node="session:me"');
    expect(html).toContain('data-node="child:w1"');
    expect(html).toContain(">Tack on a stop<");
    expect(html).toContain('aria-label="Actions for me"');
    // No pane until a box is opened.
    expect(html).not.toContain("pane-embedded");
  });

  it("the size control cycles compact → normal → wide → compact", () => {
    expect(nextSize("compact")).toBe("normal");
    expect(nextSize("normal")).toBe("wide");
    expect(nextSize("wide")).toBe("compact");
    expect(line({ size: "wide" })).toContain('aria-label="Box size: wide (next: compact)"');
    const html = line({ size: "compact" });
    expect(html).toContain("skl-size-compact");
    expect(html).toContain("fl-size-compact");
    expect(code("components/flights/SkyLine.tsx")).toContain("onClick={() => p.onLayout({ size: nextSize(size) })}");
  });

  it("wide: a session box says what it was last asked", () => {
    expect(line({ size: "wide" })).toContain('<div class="fl-box-detail">Make the login page stop looping</div>');
  });

  it("focus draws the ring; a pin shows when pinned", () => {
    expect(line({}, { focused: true })).toContain("skl skl-size-normal is-focused");
    expect(line({ pinned: true })).toContain('class="skl-pin"');
    expect(line()).not.toContain('class="skl-pin"');
  });

  it("a flyway dragged over lights the + with “Drop to fly <name>”", () => {
    const html = line({}, { dropFlyway: PLAN });
    expect(html).toContain("is-drop");
    expect(boxOf(html, "add")?.[1]).toContain("fl-drop");
    expect(html).toContain('<span class="fl-add-label">Drop to fly Ticket to merge</span>');
    expect(line()).not.toContain("fl-drop");
  });

  it("open: the session box is a card holding the real pane — tab strip, no grid chrome", () => {
    const html = line({ open: true, paneW: 800, paneH: 500 });
    const box = boxOf(html, "session:me");
    expect(box?.[1]).toContain("fl-box-open");
    // A card sizes itself: no layout width on the box; the card says its own.
    expect(box?.[2]).not.toContain("width");
    expect(html).toContain('class="skl-card sky-own-scroll"');
    expect(html).toContain("width:800px;height:530px");
    expect(html).toContain('class="pane pane-embedded"');
    expect(html).toContain('role="tablist"');
    expect(html).toMatch(/>Agent</);
    expect(html).toMatch(/>Terminal</);
    expect(html).not.toContain('class="grip"');
    expect(html).not.toContain('aria-label="Hide window"');
    expect(html).toContain('aria-label="Fold me back into a box"');
    expect(html).toContain('class="skl-resize"');
    // The worker stays a box.
    expect(boxOf(html, "child:w1")?.[1]).not.toContain("fl-box-open");
  });

  it("the card never shrinks under its floor", () => {
    expect(clampPane(100, 50)).toEqual({ w: PANE_MIN_W, h: PANE_MIN_H });
    expect(clampPane(901.4, 610.6)).toEqual({ w: 901, h: 611 });
    expect(line({ open: true, paneW: 10, paneH: 10 })).toContain("width:" + PANE_MIN_W + "px;height:" + (PANE_MIN_H + 30) + "px");
  });

  it("wiring the host relies on: measure, drag, focus, open, resize over the zoom", () => {
    const src = code("components/flights/SkyLine.tsx");
    expect(src).toContain("new ResizeObserver(read)");
    expect(src).toContain("measureRef.current?.(w, h)");
    expect(src).toContain("p.onDragStart?.(e)");
    expect(src).toContain("if (!p.focused) p.onFocus()");
    expect(src).toContain("p.onLayout({ open: !p.layout.open })");
    expect(src).toContain("g.w + (e.clientX - g.sx) / z");
    expect(src).toContain('<Pane key={inst.title} inst={inst} embedded />');
    expect(src).toContain('useUi.getState().openDialogFor("flights", "sky/" + p.title)');
    // A card is measured, so the connectors follow it.
    const canvas = code("components/flights/FlywayCanvas.tsx");
    expect(canvas).toContain("if (o) return measured[n.id] || o.size;");
  });
});

describe("the embedded pane", () => {
  const render = (embedded: boolean) =>
    withClient(createElement(Pane, embedded ? { inst: rowOf("me"), embedded: true } : { inst: rowOf("me"), drag: { dragTitle: null, start() {}, hover() {}, commit() {}, end() {} }, dragging: false }));

  it("keeps the tabs (Terminal too) and drops the grip, the drag and hide −", () => {
    const html = render(true);
    expect(html).toContain("pane-embedded");
    expect(html).toMatch(/>Terminal</);
    expect(html).toContain("agent-term");
    expect(html).not.toContain('class="grip"');
    expect(html).not.toContain('draggable="true"');
    const grid = render(false);
    expect(grid).toContain('class="grip"');
    expect(grid).toContain('draggable="true"');
    expect(grid).not.toContain("pane-embedded");
  });

  it("holds the session's terminals: the grid's pane leaves them while a card is open", () => {
    const src = code("components/grid/Pane.tsx");
    expect(src).toContain("useLayoutEffect(() => (embedded ? holdTerms(title) : undefined), [embedded, title]);");
    expect(src).toContain("if (held) return;");
  });
});
