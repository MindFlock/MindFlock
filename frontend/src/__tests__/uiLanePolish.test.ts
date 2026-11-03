/** The intake lane's polish round (IntakeLane.tsx + lib/skyIntake.ts): the
 * stops chip reads as the source's stops and as editable ("Stops  built-in ✎"
 * / "Stops  Payments stops ✎"), lit and saying "editing…" while that source's
 * editor chain is on the board; a block's title drops the kind its badge says,
 * in normal case, wrapping a repo path at its "/"; a source settings doesn't
 * name is marked; and the far zoom keeps the badge so two blocks for one repo
 * (its PRs, its issues) still tell apart. */

/// <reference types="vite/client" />
import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { code, css, cssRule } from "./sourceText";
import type { Flight, FlightPlan } from "../lib/flights";

vi.mock("../api/client", () => ({ api: () => new Promise(() => {}), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const { QueryClient, QueryClientProvider } = await import("@tanstack/react-query");
const { IntakeLane } = await import("../components/flights/IntakeLane");
const { nameParts, stopsTip, stopsWords } = await import("../lib/skyIntake");

(globalThis as Record<string, unknown>).document ??= {
  hidden: false,
  addEventListener: () => {},
  removeEventListener: () => {},
  getElementById: () => null,
  querySelector: () => null,
};

const plan = (id: string, name: string) => ({ id, name, builtin: id.startsWith("builtin:") }) as FlightPlan;
const PLANS = [plan("p_pay", "Payments stops"), plan("builtin:ticket", "Ticket"), plan("builtin:pr", "Review a PR")];

const SETTINGS = {
  ticketing: { sources: [{ id: "jira-1", provider: "jira", label: "Jira — payments", plan: "p_pay" }] },
  github: { repos: ["quickpay/quickpay"], issue_repos: ["quickpay/web"], repo_settings: { "quickpay/quickpay": { depth: "pr" } } },
};

const editor = (kind: string, target: string, state = "grounded") =>
  ({ id: "f_" + target, plan_id: "adhoc", plan_name: "Default · x", session: "", state, cursor: 0, steps: [], step_states: {}, history: [], edits_default: { kind, target } }) as unknown as Flight;

beforeEach(() => {
  (globalThis as Record<string, unknown>).localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
});
afterEach(() => {
  delete (globalThis as Record<string, unknown>).localStorage;
});

function lane(flights: Flight[] = [], issues: unknown[] = []) {
  const c = new QueryClient({ defaultOptions: { queries: { retry: false, enabled: false } } });
  c.setQueryData(["settings"], SETTINGS);
  c.setQueryData(["mindflock-status"], { available: true, desired: true, pr_enabled: false });
  c.setQueryData(["ticketing-catalog"], [{ id: "jira", label: "Jira", fields: [] }]);
  c.setQueryData(["tickets"], { tickets: [] });
  c.setQueryData(["github-prs"], { prs: [] });
  c.setQueryData(["github-issues"], { issues });
  c.setQueryData(["flights"], flights);
  const el: ReactElement = createElement(IntakeLane, { rows: [], plans: PLANS, onFly: () => {} });
  return renderToStaticMarkup(createElement(QueryClientProvider, { client: c }, el));
}

const block = (html: string, key: string) => {
  const at = html.indexOf('data-lane-source="' + key + '"');
  const next = html.indexOf("<section", at + 1);
  return html.slice(at, next < 0 ? undefined : next);
};

describe("the stops chip", () => {
  it("says Stops and the words Intake's \"Goes as far as\" shows — its own by name, else its rung — never the built-in plan's name", () => {
    expect(stopsWords({ id: "p_pay", name: "Payments stops", own: true })).toBe("Payments stops");
    expect(stopsWords({ id: "builtin:ticket", name: "Ticket", own: false, words: "Open PR" })).toBe("Open PR");
    expect(stopsWords({ id: "builtin:ticket", name: "Ticket", own: false })).toBe("Stop after the agent");
    const html = lane();
    const jira = block(html, "tickets:jira-1");
    expect(jira).toContain('<span class="sky-lane-stops own"><span class="sky-lane-stops-key">Chain</span><span class="sky-lane-flyway own">Payments stops</span>');
    const pr = block(html, "prs:quickpay/quickpay");
    expect(pr).toContain('<span class="sky-lane-stops"><span class="sky-lane-stops-key">Chain</span><span class="sky-lane-flyway">Open PR</span>');
    // No depth: the agent alone, said the way the card says it.
    expect(block(html, "issues:quickpay/web")).toContain('<span class="sky-lane-flyway">Stop after the agent</span>');
    expect(pr).not.toContain(">Review a PR<");
  });

  it("reads as editable: a pencil, a pointer, and a tooltip naming what it edits and what it is now", () => {
    const pr = block(lane(), "prs:quickpay/quickpay");
    expect(pr).toContain('<span class="sky-lane-stops-edit" aria-hidden="true">✎</span>');
    expect(pr).toContain('title="Edit the default chain for quickpay/quickpay (goes as far as Open PR)"');
    expect(stopsTip({ name: "Jira — payments", flyway: { id: "p", name: "Payments stops", own: true } })).toBe("Edit the default chain for Jira — payments (its own: Payments stops)");
    const sheet = css("components/flights/IntakeLane.css");
    expect(cssRule(sheet, ".sky-lane-chips .sky-lane-flyway-btn")).toContain("cursor: pointer");
    expect(cssRule(sheet, ".sky-lane-flyway-btn:hover .sky-lane-stops")).toContain("border-color");
    expect(cssRule(sheet, ".sky-lane-flyway-btn:focus-visible .sky-lane-stops-edit")).toContain("opacity: 1");
  });

  it("while the source's editor chain is on the board it is lit and says editing…", () => {
    const html = lane([editor("tickets", "jira-1"), editor("prs", "quickpay/quickpay", "running")]);
    const jira = block(html, "tickets:jira-1");
    expect(jira).toContain('data-editing=""');
    expect(jira).toContain('class="sky-lane-stops own editing"');
    expect(jira).toContain('aria-hidden="true">editing…</span>');
    expect(jira).toContain('title="Editing on the board — go to the editor for Jira — payments (its own: Payments stops)"');
    // Only a grounded chain is an editor (editorFor): a running one is not.
    const pr = block(html, "prs:quickpay/quickpay");
    expect(pr).not.toContain("data-editing");
    expect(pr).not.toContain("editing…");
    expect(cssRule(css("components/flights/IntakeLane.css"), ".sky-lane-stops.editing")).toContain("background");
    expect(code("components/flights/IntakeLane.tsx")).toContain("editing={!!editorFor(flights, { kind: b.kind, target: b.id })}");
  });
});

describe("a block's title", () => {
  it("drops the kind its badge says, keeps the full label in its tooltip, and breaks a repo path at its /", () => {
    const html = lane();
    expect(block(html, "prs:quickpay/quickpay")).toContain('<span class="sky-lane-block-name" title="PRs · quickpay/quickpay">quickpay/<wbr/>quickpay</span>');
    expect(block(html, "issues:quickpay/web")).toContain('title="Issues · quickpay/web">quickpay/<wbr/>web</span>');
    expect(block(html, "tickets:jira-1")).toContain('title="Jira — payments">Jira — payments</span>');
    expect(nameParts("a/b/c")).toEqual(["a/", "b/", "c"]);
    expect(nameParts("Jira — payments")).toEqual(["Jira — payments"]);
  });

  it("normal case, two lines at most, wrapping rather than cutting at the first word", () => {
    const rule = cssRule(css("components/flights/IntakeLane.css"), ".sky-lane-block-name");
    expect(rule).not.toContain("text-transform");
    expect(rule).toContain("-webkit-line-clamp: 2");
    expect(rule).not.toContain("white-space: nowrap");
  });

  it("a source settings doesn't name is marked (dashed, with why) — the mock's second issues block was one", () => {
    const html = lane([], [{ repo: "quickpay/quickpay", number: 218, title: "Login flakes", eligible: true }]);
    expect(html).toContain('class="sky-lane-block sky-lane-block-issues sky-lane-block-stray" data-lane-source="issues:quickpay/quickpay"');
    expect(html).toContain('class="sky-lane-block sky-lane-block-issues" data-lane-source="issues:quickpay/web"');
    const stray = block(html, "issues:quickpay/quickpay");
    expect(stray).toContain('title="Not in your settings: items from quickpay/quickpay are listed all the same"');
    expect(cssRule(css("components/flights/IntakeLane.css"), ".sky-lane-block-stray")).toContain("border-style: dashed");
  });

  it("from far the kind tag stays (unscaled, beside the name) so a repo's PR and issue blocks still tell apart", () => {
    const sheet = css("components/flights/IntakeLane.css");
    expect(sheet).not.toMatch(/\.sky-intake\[data-lod="far"\] :is\([^)]*\.fl-kind-badge/);
    expect(sheet).not.toMatch(/\[data-lod="far"\][^{]*\.fl-kind-badge/);
  });
});

describe("the lane wears the session frame's calm look (one Sky-object style)", () => {
  // The owner: "I dont really like the style of these 3 blocks" — a huge
  // all-caps INTAKE, blue-filled source cards, big bold titles, pill badges.
  const sheet = css("components/flights/IntakeLane.css");

  it("a 14px-radius frame with one muted hairline; a 30px title bar in normal case with a muted count", () => {
    expect(cssRule(sheet, ".sky-lane")).toMatch(/border: 1px solid var\(--lane-line\);[\s\S]*border-radius: 14px;[\s\S]*background: color-mix\(in srgb, var\(--muted\) 4%, var\(--panel\)\)/);
    expect(cssRule(sheet, ".sky-lane")).toContain("--lane-line: color-mix(in srgb, var(--muted) 30%, transparent)");
    const head = cssRule(sheet, ".sky-lane-head");
    expect(head).toMatch(/height: 30px;[\s\S]*border-bottom: 1px solid var\(--lane-hair\);[\s\S]*font-size: 13px;[\s\S]*font-weight: 600/);
    expect(head).not.toContain("text-transform");
    expect(head).not.toContain("letter-spacing");
    const count = cssRule(sheet, ".sky-lane-head .fl-count");
    expect(count).toMatch(/border: none;[\s\S]*background: none;[\s\S]*color: var\(--muted\)/);
  });

  it("a source is a quiet section (no fill, a hairline between two), its kind an outlined muted 11px tag", () => {
    expect(cssRule(sheet, ".sky-lane-block")).toContain("background: transparent");
    expect(cssRule(sheet, ".sky-lane-block + .sky-lane-block")).toContain("border-top-width: 1px");
    const tag = cssRule(sheet, ".sky-lane .fl-kind-badge");
    expect(tag).toMatch(/border: 1px solid color-mix\(in srgb, var\(--lane-kind\) 45%, transparent\);[\s\S]*background: transparent;[\s\S]*color: var\(--muted\);[\s\S]*font-size: 11px/);
    // Waiting items: one inset list of hairline rows, the headline muted 11.5px.
    expect(cssRule(sheet, ".sky-lane-items > li + li")).toContain("border-top: 1px solid");
    expect(cssRule(sheet, ".sky-lane-item-headline")).toMatch(/font-size: 11.5px;[\s\S]*color: var\(--muted\)/);
  });
});
