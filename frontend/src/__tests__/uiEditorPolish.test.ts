/** The grounded chain's polish round: a default-stops editor reads as a DRAFT
 * at rest (its bar always shown, a dashed accent outline and a faint tint
 * round the chain, a "Default · <kind>" tag), its bar names just the source,
 * the hint is a caption under it, Discard is a quiet ×, Reset a quiet
 * secondary, and Save as default is pressable only once the stops differ from
 * the source's current default. A free chain's ▶ takes the empty socket's
 * place — one circle, the socket back only while something could land on it —
 * and its "Last run" chip says when. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { code, css, cssRule } from "./sourceText";
import type { Flight, FlightStep, StepKindDef } from "../lib/flights";

vi.mock("../api/client", () => ({ api: vi.fn(() => new Promise(() => {})), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const G = await import("../lib/groundedLines");
const { GroundedLine } = await import("../components/flights/GroundedLine");

(globalThis as Record<string, unknown>).document ??= {
  hidden: false,
  addEventListener: () => {},
  removeEventListener: () => {},
  getElementById: () => null,
  querySelector: () => null,
};

const kind = (k: string, label: string): StepKindDef => ({ kind: k, label, actor: "robot", summary: label, fields: [], default_title: label });
const KINDS = [kind("agent", "Agent"), kind("ship", "Ship"), kind("wait", "Wait")];
const step = (id: string, k: string, title: string, config: Record<string, unknown> = {}, more: Partial<FlightStep> = {}): FlightStep => ({ id, kind: k, title, config, on_fail: "halt", ...more });

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

const render = (f: Flight) =>
  renderToStaticMarkup(
    createElement(GroundedLine, {
      flight: f,
      kinds: KINDS,
      plans: [],
      layout: { x: 0, y: 0, size: "normal", open: false },
      focused: false,
      onFocus() {},
      zoom: 1,
      onFlyOn() {},
    })
  );

describe("lib/groundedLines: an editor's title", () => {
  it("splits the label into the kind (the tag) and the source's name (the title)", () => {
    expect(G.editorTitle("Tickets · Jira — payments")).toEqual({ kind: "Tickets", name: "Jira — payments" });
    expect(G.editorTitle("PRs · owner/repo")).toEqual({ kind: "PRs", name: "owner/repo" });
    // Only the first " · " splits; a name may hold one.
    expect(G.editorTitle("Issues · a · b")).toEqual({ kind: "Issues", name: "a · b" });
  });
  it("a label with no kind is all name", () => {
    expect(G.editorTitle("jira-1")).toEqual({ kind: "", name: "jira-1" });
    expect(G.editorTitle("")).toEqual({ kind: "", name: "" });
    expect(G.editorTitle(" · x")).toEqual({ kind: "", name: " · x" });
    expect(G.editorTitle("Tickets · ")).toEqual({ kind: "", name: "Tickets · " });
  });
});

describe("lib/groundedLines: defaultChanged", () => {
  const base = [step("a", "agent", "Work", { prompt: "go", n: { x: 1, y: [1, 2] } }), step("b", "ship", "Push")];
  it("the same stops (other ids, keys in another order, a tile moved) are no change", () => {
    const same = [step("x1", "agent", "Work", { n: { y: [1, 2], x: 1 }, prompt: "go" }, { pos: { x: 40, y: 0 } }), step("x2", "ship", "Push", {}, { halt: false })];
    expect(G.defaultChanged(same, base)).toBe(false);
    expect(G.defaultChanged([], [])).toBe(false);
    // A key set to undefined is the key left out.
    expect(G.defaultChanged([step("a", "ship", "Push", { extra: undefined })], [step("b", "ship", "Push")])).toBe(false);
  });
  it("a stop added, removed or moved in the order is a change", () => {
    expect(G.defaultChanged(base.slice(0, 1), base)).toBe(true);
    expect(G.defaultChanged([...base, step("c", "wait", "Wait")], base)).toBe(true);
    expect(G.defaultChanged([base[1], base[0]], base)).toBe(true);
  });
  it("a kind, title, config, on-fail, halt or condition changed is a change", () => {
    const one = (more: Partial<FlightStep>) => [{ ...base[0], ...more }, base[1]];
    expect(G.defaultChanged(one({ kind: "wait" }), base)).toBe(true);
    expect(G.defaultChanged(one({ title: "Work hard" }), base)).toBe(true);
    expect(G.defaultChanged(one({ config: { prompt: "go!", n: { x: 1, y: [1, 2] } } }), base)).toBe(true);
    expect(G.defaultChanged(one({ config: { prompt: "go", n: { x: 1, y: [2, 1] } } }), base)).toBe(true);
    expect(G.defaultChanged(one({ config: { prompt: "go" } }), base)).toBe(true);
    expect(G.defaultChanged(one({ on_fail: "skip" }), base)).toBe(true);
    expect(G.defaultChanged(one({ halt: true }), base)).toBe(true);
    expect(G.defaultChanged(one({ when: "weekday" }), base)).toBe(true);
  });
});

describe("lib/groundedLines: lastRunWords", () => {
  it("says how the last run went and when, and why it stopped", () => {
    expect(G.lastRunWords({ state: "done", reason: "", at: 1000 }, 1180)).toEqual({ label: "Last run done", title: "Last run done · 3m ago" });
    expect(G.lastRunWords({ state: "cancelled", reason: "you stopped it", at: 1000 }, 8200)).toEqual({ label: "Last run stopped", title: "Last run stopped · 2h ago — you stopped it" });
    expect(G.lastRunWords({ state: "done", reason: "", at: 0 }, 50).title).toBe("Last run done");
  });
  it("the chip wears it", () => {
    const html = render(flight({ free: true, last_run: { state: "done", reason: "", at: Math.floor(Date.now() / 1000) - 120 } }));
    expect(html).toMatch(/class="fl-box-chip skl-chip tone-ready" title="Last run done · 2m ago">Last run done</);
  });
});

describe("the default-stops editor", () => {
  const ed = flight({ id: "f_ed", plan_name: "Default · Tickets · Jira — payments", edits_default: { kind: "tickets", target: "jira-1" } });
  const html = render(ed);

  it("a short title naming the source, the kind on a small Default tag", () => {
    expect(html).toContain(">Jira — payments</span>");
    expect(html).not.toContain(">Default stops · ");
    expect(html).toMatch(/class="fl-box-chip skl-chip skl-default-tag"[^>]*>Default · Tickets</);
  });
  it("the hint is a caption under the bar, not in it", () => {
    expect(html).toContain('<div class="skl-default-hint">New items from Jira — payments fly these stops</div>');
    const bar = html.slice(html.indexOf('class="skl-bar skl-label"'), html.indexOf('class="skl-default-hint"'));
    // (Tooltips may say it; no text in the bar does.)
    expect(bar).not.toContain(">New items from");
    expect(bar).not.toContain("skl-default-hint");
  });
  it("Save as default the one primary; Reset a quiet secondary; Discard a quiet × with a tooltip", () => {
    expect(html.match(/btn-primary/g)?.length).toBe(1);
    expect(html).toMatch(/<button type="button" class="fl-icon-btn skl-default-discard" aria-label="Discard this edit" title="Discard — [^"]*"[^>]*>×</);
    const src = code("components/flights/GroundedLine.tsx");
    expect(src).toContain('className="test-btn fl-btn skl-default-reset"');
    const g = css("components/flights/GroundedLine.css");
    expect(cssRule(g, ".skl-grounded .skl-default-acts > .skl-default-reset")).toContain("background: transparent");
  });
  it("Save is pressable only once the stops differ from the source's default", () => {
    const src = code("components/flights/GroundedLine.tsx");
    expect(src).toContain("setBaseSteps(d ? d.steps : null);");
    expect(src).toContain("const changed = !baseSteps || defaultChanged(flight.steps, baseSteps);");
    expect(src).toContain("disabled={busy || !flight.steps.length || !changed}");
    expect(src).toContain('{changed ? "Save as default" : "No changes"}');
    // Unchanged reads greyed, not as an accent button you can't press.
    expect(cssRule(css("components/flights/GroundedLine.css"), ".skl-grounded .skl-default-acts > .skl-default-save:disabled")).toContain("background: transparent");
  });
  it("a draft at rest: its bar always shown, a dashed accent outline and a faint tint round the chain", () => {
    expect(html).toContain("is-default-editor");
    const g = css("components/flights/GroundedLine.css");
    expect(cssRule(g, ".skl-chainline.skl-grounded.is-default-editor > .skl-bar.skl-label")).toContain("opacity: 1");
    const frame = cssRule(g, ".skl.skl-chainline.is-default-editor::before");
    expect(frame).toContain("dashed");
    expect(frame).toContain("var(--accent)");
    expect(frame).toContain("rgba(var(--accent-rgb), 0.045)");
    // Screen-sized at any zoom, so it still reads at Fit.
    expect(frame).toContain("calc(1.5px / var(--sky-z, 1))");
    expect(frame).toContain("pointer-events: none");
  });
});

describe("a free chain's ▶", () => {
  it("sits where the empty socket does: one circle, not two", () => {
    const src = code("components/flights/SkyChain.tsx");
    expect(src).toContain('(p.run && socket ? " has-run" : "")');
    expect(src).toContain("style={{ left: at.x - 24, top: at.y }}");
    expect(src).not.toContain("socket ? 46 : 22");
    expect(render(flight())).toContain('class="sky-chain has-run"');
  });
  it("the socket hides under it, and comes back (the ▶ stepping aside) while a wire or a Session chip could land there", () => {
    const c = css("components/flights/SkyChain.css");
    expect(cssRule(c, ".sky-chain.has-run .sky-socket:not(.is-target)")).toContain("opacity: 0");
    const aside = cssRule(c, ".sky-chain.has-run:has(.sky-socket.is-target) .sky-run:not(.is-running)");
    expect(aside).toContain("opacity: 0");
    expect(aside).toContain("pointer-events: none");
    const g = css("components/flights/GroundedLine.css");
    expect(cssRule(g, ".skl-grounded.is-session-over .sky-chain.has-run .sky-socket")).toContain("opacity: 1");
    expect(cssRule(g, ".skl-grounded.is-session-over .sky-chain.has-run .sky-run:not(.is-running)")).toContain("pointer-events: none");
  });
  it("an editor has neither ▶ nor has-run; its socket stays as it was", () => {
    const html = render(flight({ edits_default: { kind: "tickets", target: "t" }, plan_name: "Default · Tickets · T" }));
    expect(html).not.toContain("has-run");
    expect(html).toContain('class="sky-socket"');
  });
});
