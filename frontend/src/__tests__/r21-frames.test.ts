/** Round 21 (JUDGE-R21-1 #5, JUDGE-R21-2 #8) — a session's frame says its
 * STATE at a glance: the box is neutral (theme surfaces), the state drives
 * its border (`frameState` → `data-frame`), the session's identity colour is
 * only a small dot by its name, and a ready route's ONE ▶ Run sits on the
 * frame's bar (never on the agent tile). */

/// <reference types="vite/client" />
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { css } from "./sourceText";
import type { Instance } from "../api/types";
import type { StepKindDef } from "../lib/flights";

vi.mock("../api/client", () => ({ api: vi.fn(() => new Promise(() => {})), instApi: vi.fn(() => new Promise(() => {})), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const { QueryClient, QueryClientProvider } = await import("@tanstack/react-query");
const { useUi } = await import("../state/store");
const { SkyLine, frameState } = await import("../components/flights/SkyLine");

(globalThis as Record<string, unknown>).document ??= {
  hidden: false,
  addEventListener: () => {},
  removeEventListener: () => {},
  getElementById: () => null,
  querySelector: () => null,
};

afterEach(() => {
  Object.assign(useUi.getInitialState(), useUi.getState());
});

describe("frameState: the state → frame mapping (pure)", () => {
  it.each([
    // [what the line knows, the frame]
    [{}, "idle"],
    [{ tone: "neutral" as const }, "idle"],
    [{ tone: "muted" as const }, "idle"],
    [{ tone: "waiting" as const }, "idle"],
    [{ tone: "working" as const }, "working"],
    [{ flight: "running" as const }, "working"],
    [{ flight: "running" as const, tone: "neutral" as const }, "working"],
    [{ tone: "attention" as const }, "you"],
    [{ flight: "waiting_you" as const }, "you"],
    [{ behaviorsYou: true }, "you"],
    [{ flight: "running" as const, tone: "attention" as const }, "you"],
    [{ flight: "halted" as const }, "halted"],
    [{ flight: "halted" as const, tone: "attention" as const }, "halted"],
    [{ flight: "halted" as const, tone: "broken" as const }, "halted"],
    [{ tone: "broken" as const }, "broken"],
    [{ tone: "broken" as const, flight: "waiting_you" as const }, "broken"],
    [{ flight: "done" as const }, "done"],
    [{ landed: true }, "done"],
    [{ landed: true, tone: "working" as const }, "working"],
    [{ landed: true, tone: "attention" as const }, "you"],
    [{ flight: "ready" as const }, "ready"],
    [{ flight: "ready" as const, tone: "working" as const }, "working"],
    [{ flight: "ready" as const, tone: "attention" as const }, "you"],
    // Flow polish: a paused route is calm and dashed, never "idle" (nor red / amber)…
    [{ flight: "paused" as const }, "paused"],
    // …unless it paused at a halt point: then it waits on you.
    [{ flight: "paused" as const, haltPoint: true }, "you"],
    [{ flight: "halted" as const, haltPoint: true }, "you"],
    [{ flight: "grounded" as const }, "idle"],
    [{ flight: null }, "idle"],
  ])("%j → %s", (input, want) => {
    expect(frameState(input)).toBe(want);
  });
});

const KINDS: StepKindDef[] = [{ kind: "agent", label: "Agent", actor: "robot", summary: "The agent works", fields: [], default_title: "Agent works" }];

const rowOf = (over: Record<string, unknown> = {}) =>
  ({
    title: "me",
    status: "running",
    activity: "idle",
    stage: "agent",
    provider: "claude",
    program: "claude",
    task_headline: "Do me",
    last_prompt: "",
    parent: "",
    branch: "feature/me",
    flight: null,
    autopilot: null,
    ...over,
  }) as unknown as Instance;

function render(row: Instance, flights: unknown[] = []) {
  const c = new QueryClient({ defaultOptions: { queries: { retry: false, enabled: false } } });
  c.setQueryData(["flights"], flights);
  c.setQueryData(["flight-kinds"], KINDS);
  c.setQueryData(["config"], { caps: { git: true } });
  return renderToStaticMarkup(
    createElement(
      QueryClientProvider,
      { client: c },
      createElement(SkyLine, { title: "me", rows: [row], plans: [], layout: { x: 0, y: 0, size: "normal", open: false }, onLayout: () => {}, focused: false, onFocus: () => {}, zoom: 1 })
    )
  );
}

describe("the rendered frame", () => {
  it("carries its state as data-frame", () => {
    expect(render(rowOf({ activity: "working" }))).toContain('data-frame="working"');
    expect(render(rowOf())).toContain('data-frame="idle"');
    expect(render(rowOf({ status: "error" }))).toMatch(/data-frame="(broken|idle)"/);
  });

  it("the identity colour is a dot before the name, nothing else", () => {
    const html = render(rowOf());
    expect(html).toContain('<span class="skl-id-dot" aria-hidden="true"></span><span class="skl-name">me</span>');
  });

  it("a ready route's ▶ Run is on the bar, exactly once, before the ⋯", () => {
    const fl = { id: "f_r", plan_name: "Later", cursor: 0, total: 1, state: "ready", step: { id: "s1", kind: "agent", title: "Write it", state: "pending", actor: "robot" } };
    const flights = [{ id: "f_r", plan_id: "p", plan_name: "Later", session: "me", state: "ready", cursor: 0, start_on: "prompt", steps: [{ id: "s1", kind: "agent", title: "Write it", config: {}, on_fail: "halt" }], step_states: {}, history: [], started: 1, updated: 1 }];
    const html = render(rowOf({ flight: fl }), flights);
    expect(html).toContain('data-frame="ready"');
    expect(html.match(/class="sky-ready-run"/g)?.length).toBe(1);
    const bar = html.slice(html.indexOf('class="skl-bar skl-label skl-frame-bar"'), html.indexOf('class="skl-menu"'));
    expect(bar).toContain('class="sky-ready-run"');
    // The toggle still says how it starts.
    expect(bar).toContain("runs on your next prompt");
    // The agent tile carries its chip, not the Run.
    const tile = html.slice(html.indexOf("sky-head-session"));
    expect(tile).not.toContain("sky-ready");
  });
});

describe("SkyLine.css: neutral box, state border", () => {
  const sheet = css("components/flights/SkyLine.css");
  const rule = (sel: string) => {
    const i = sheet.indexOf(sel + " {");
    expect(i, sel).toBeGreaterThanOrEqual(0);
    return sheet.slice(i, sheet.indexOf("}", i));
  };

  it("the box and bar take theme surfaces, never the session's colour", () => {
    const box = rule(".skl-frame-box");
    expect(box).toContain("var(--panel)");
    expect(box).toContain("var(--border)");
    expect(box).not.toContain("--sky-chain-color");
    const bar = rule(".skl-chainline.skl-frame > .skl-bar.skl-frame-bar");
    expect(bar).toContain("var(--panel-2)");
    expect(bar).not.toContain("--sky-chain-color");
    expect(rule(".skl-frame-bar .skl-id-dot")).toContain("var(--sky-chain-color");
  });

  it("each state has its border", () => {
    expect(rule('.skl-frame[data-frame="you"] > .skl-frame-box')).toMatch(/var\(--tone-attention\)[^]*box-shadow/);
    expect(rule('.skl-frame:is([data-frame="halted"], [data-frame="broken"]) > .skl-frame-box')).toContain("var(--tone-broken)");
    expect(rule('.skl-frame[data-frame="working"] > .skl-frame-box')).toContain("var(--accent)");
    expect(rule('.skl-frame[data-frame="done"] > :is(.skl-frame-box, .skl-chain-wrap, .skl-ruler, .skl-cards)')).toContain("opacity: 0.55");
    expect(sheet).toMatch(/\.skl-frame\[data-frame="done"\]:is\(:hover, :focus-within[^{]*\{\s*opacity: 1;/);
  });

  it("the working cue stops under reduced motion", () => {
    expect(sheet).toMatch(/@media \(prefers-reduced-motion: reduce\) \{\s*\.skl-frame\[data-frame="working"\] > \.skl-frame-box::after \{\s*animation: none;/);
  });

  it("the name counter-scales with the sky's zoom", () => {
    expect(rule(".skl-chainline.skl-frame > .skl-bar.skl-frame-bar .skl-name")).toMatch(/font-size: clamp\([^;]*var\(--sky-z, 1\)/);
  });
});

describe("idle on uncommitted changes: a quiet note, never the amber frame", () => {
  const dirty = () =>
    rowOf({
      activity_since: Date.now() / 1000 - 25 * 60,
      diff_stat: { files: 3, additions: 120, deletions: 4, uncommitted: { files: 3, additions: 120, deletions: 4 } },
    });

  it("the frame stays idle (not `you`), the bar says what it is and offers Commit… / Carry on", () => {
    const html = render(dirty());
    expect(html).toContain('data-frame="idle"');
    expect(html).not.toContain('data-frame="you"');
    const bar = html.slice(html.indexOf('class="skl-bar skl-label skl-frame-bar"'), html.indexOf('class="skl-menu"'));
    expect(bar).toContain('<span class="skl-uncommitted-text">3 files uncommitted · idle 25m</span>');
    expect(bar).toMatch(/class="linklike skl-uncommitted-act"[^>]*>Commit…<\/button>/);
    expect(bar).toMatch(/class="linklike skl-uncommitted-act"[^>]*>Carry on<\/button>/);
    // The state chip is the calm Idle, not "Idle 25m" in the attention tone.
    expect(bar).toContain("tone-neutral");
    expect(bar).not.toContain("tone-attention");
    expect(html).not.toMatch(/unfinished work|possibly stuck/);
  });

  it("a fresh idle session, or one in place on a person's checkout, has no note", () => {
    expect(render(rowOf({ diff_stat: { files: 3, additions: 1, deletions: 0, uncommitted: { files: 3, additions: 1, deletions: 0 } }, activity_since: Date.now() / 1000 - 60 }))).not.toContain("skl-uncommitted");
    expect(render({ ...dirty(), in_place: true } as Instance)).not.toContain("skl-uncommitted");
  });

  it("another device's row: the words, no actions", () => {
    const html = render({ ...dirty(), device: "rig" } as Instance);
    expect(html).toContain("3 files uncommitted · idle 25m");
    expect(html).not.toContain("skl-uncommitted-act");
  });

  it("its look is the neutral surfaces: no amber, no glow, no breathing", () => {
    const sheet = css("components/flights/SkyLine.css");
    const i = sheet.indexOf(".skl-frame-bar .skl-uncommitted {");
    expect(i).toBeGreaterThanOrEqual(0);
    const note = sheet.slice(i, sheet.indexOf("}", i));
    expect(note).toContain("var(--border)");
    expect(note).toContain("var(--panel-2)");
    expect(note).toContain("var(--muted)");
    const all = sheet.slice(i, sheet.indexOf('.skl[data-lod="far"] .skl-uncommitted-act'));
    expect(all).not.toMatch(/tone-attention|box-shadow|animation/);
  });
});
