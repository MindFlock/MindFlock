/** The Sky's intake lane and new-session block on screen (server-rendered,
 * mocked API): a block per source with its kind badge, auto-start chip,
 * flyway chip, waiting items with Fly and started-session stubs; the folded
 * rail; and the new-session card's chips, its Fly guard and Escape. */

/// <reference types="vite/client" />
import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { code, css, cssRule } from "./sourceText";
import type { Instance } from "../api/types";
import type { FlightPlan } from "../lib/flights";

const api = vi.fn((..._args: unknown[]): Promise<unknown> => new Promise(() => {}));
vi.mock("../api/client", () => ({ api: (...a: unknown[]) => api(...a), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const { QueryClient, QueryClientProvider } = await import("@tanstack/react-query");
const { IntakeLane, flyLaneItem } = await import("../components/flights/IntakeLane");
const { NewSessionBlock, cardKey, promptKey, flyBlocked, savedStopChoices, repoChoices } = await import("../components/flights/NewSessionBlock");
const { collectQueued } = await import("../components/intake/queue");

(globalThis as Record<string, unknown>).document ??= {
  hidden: false,
  addEventListener: () => {},
  removeEventListener: () => {},
  getElementById: () => null,
  querySelector: () => null,
};

const plan = (id: string, name: string) => ({ id, name, builtin: id.startsWith("builtin:") }) as FlightPlan;
const PLANS = [plan("p_mine", "Mine"), plan("builtin:ticket", "Ticket to merge"), plan("builtin:pr", "Review a PR")];

const SETTINGS = {
  ticketing: { sources: [{ id: "sc", provider: "shortcut", label: "quickpay", plan: "p_mine" }] },
  github: { repos: ["acme/app"], issue_repos: ["acme/web"] },
};
const TICKETS = {
  tickets: [
    { source: "sc", id: "1", slug: "sc-1", name: "Fix the login", eligible: true },
    { source: "sc", id: "3", slug: "sc-3", name: "Started already", has_session: true, session: "sc-3" },
  ],
  source_labels: { sc: "Shortcut" },
};

let store: Record<string, string> = {};
beforeEach(() => {
  store = {};
  (globalThis as Record<string, unknown>).localStorage = {
    getItem: (k: string) => (k in store ? store[k] : null),
    setItem: (k: string, v: string) => (store[k] = v),
    removeItem: (k: string) => delete store[k],
  };
  api.mockReset();
  api.mockImplementation(() => new Promise(() => {}));
});
afterEach(() => {
  delete (globalThis as Record<string, unknown>).localStorage;
});

function withClient(el: ReactElement, prime: (c: InstanceType<typeof QueryClient>) => void = () => {}) {
  const c = new QueryClient({ defaultOptions: { queries: { retry: false, enabled: false } } });
  prime(c);
  return renderToStaticMarkup(createElement(QueryClientProvider, { client: c }, el));
}

const primeLane = (c: InstanceType<typeof QueryClient>) => {
  c.setQueryData(["settings"], SETTINGS);
  c.setQueryData(["mindflock-status"], { available: true, desired: true, pr_enabled: false });
  c.setQueryData(["ticketing-catalog"], [{ id: "shortcut", label: "Shortcut", fields: [] }]);
  c.setQueryData(["tickets"], TICKETS);
  c.setQueryData(["github-prs"], { prs: [] });
  c.setQueryData(["github-issues"], { issues: [] });
};
const lane = (rows: Instance[] = [], prime = primeLane) =>
  withClient(createElement(IntakeLane, { rows, plans: PLANS, onFly: () => {} }), prime);

describe("the intake lane", () => {
  it("a block per source: the kind badge, the label, the auto-start chip and the flyway chip", () => {
    const html = lane();
    expect(html).toContain('data-lane-source="tickets:sc"');
    expect(html).toContain('data-lane-source="prs:acme/app"');
    expect(html).toContain('data-lane-source="issues:acme/web"');
    expect(html).toContain('class="fl-kind-badge fl-kind-badge-tickets">Ticket<');
    expect(html).toContain('class="fl-kind-badge fl-kind-badge-prs">PR<');
    expect(html).toContain('class="fl-kind-badge fl-kind-badge-issues">Issue<');
    // The title drops the kind its badge says; a repo path may break at "/".
    expect(html).toContain('class="sky-lane-block-name" title="Shortcut · quickpay">Shortcut · quickpay<');
    expect(html).toContain('class="sky-lane-block-name" title="PRs · acme/app">acme/<wbr/>app<');
    expect(html).toContain('class="sky-lane-auto on">auto-start on · every 20 s<');
    expect(html).toContain('class="sky-lane-auto">manual<');
    expect(html).toMatch(/class="sky-lane-flyway own"[^>]*>Mine</);
    // A built-in default says where it goes in Intake's words (no depth: the
    // agent alone), not the plan's name (that echoed the badge).
    expect(html).toMatch(/class="sky-lane-flyway"[^>]*>Stop after the agent</);
    expect(html).not.toMatch(/class="sky-lane-flyway"[^>]*>Review a PR</);
    expect(html).not.toContain("<select");
  });

  it("waiting items with Fly; nothing waiting says so; a started session is a stub the Sky's arrow starts from", () => {
    const html = lane([{ title: "sc-3", branch: "" } as Instance]);
    const sc = html.slice(html.indexOf('data-lane-source="tickets:sc"'), html.indexOf('data-lane-source="prs:acme/app"'));
    expect(sc).toContain('data-waiting="tickets:sc:1"');
    expect(sc).toContain(">sc-1<");
    expect(sc).toContain(">Fix the login<");
    expect(sc).toMatch(/class="btn-primary fl-btn sky-lane-fly"[^>]*>Fly</);
    expect(sc).toContain('data-lane-target="sc-3"');
    expect(sc).not.toContain('data-waiting="tickets:sc:3"');
    const prs = html.slice(html.indexOf('data-lane-source="prs:acme/app"'));
    expect(prs).toContain("Nothing waiting");
  });

  it("the ⋯ opens the source in Intake (its own deep link: <tab>:<source>)", () => {
    expect(code("components/flights/IntakeLane.tsx")).toContain('useUi.getState().openDialogFor("intake", block.target)');
  });

  it("folds to a rail: one icon per source with its waiting count", () => {
    store.mf_sky_lane_collapsed = "1";
    const html = lane();
    expect(html).toContain('data-collapsed="true"');
    expect(html).not.toContain("sky-lane-block");
    const icons = [...html.matchAll(/class="sky-lane-rail-icon fl-kind-badge fl-kind-badge-(\w+)" data-lane-source="([^"]+)"/g)].map((m) => [m[1], m[2]]);
    expect(icons).toEqual([
      ["tickets", "tickets:sc"],
      ["prs", "prs:acme/app"],
      ["issues", "issues:acme/web"],
    ]);
    expect(html).toContain('<span class="sky-lane-rail-count">1</span>');
  });

  it("expands (round 12): ▸ / ▾ on its head, every waiting item listed, its headlines in full, the width it is given", () => {
    const many = { tickets: Array.from({ length: 9 }, (_, i) => ({ source: "sc", id: String(i + 1), slug: "sc-" + (i + 1), name: "Ticket " + (i + 1), eligible: true })), source_labels: { sc: "Shortcut" } };
    const prime = (c: InstanceType<typeof QueryClient>) => {
      primeLane(c);
      c.setQueryData(["tickets"], many);
    };
    const draw = (open: boolean) => withClient(createElement(IntakeLane, { rows: [], plans: PLANS, onFly: () => {}, open, onToggleOpen: () => {}, width: open ? 420 : 240 }), prime);
    const folded = draw(false);
    expect(folded).toMatch(/<button type="button" class="sky-lane-toggle sky-lane-expand" aria-expanded="false"[^>]*>▸</);
    expect(folded.match(/data-waiting="tickets:sc:/g)?.length).toBe(6);
    expect(folded).toContain(">3 more<");
    expect(folded).not.toContain("data-open");
    const open = draw(true);
    expect(open).toMatch(/<nav class="sky-lane" aria-label="Intake" data-open="" style="width:420px">/);
    expect(open).toMatch(/aria-expanded="true"[^>]*>▾</);
    expect(open.match(/data-waiting="tickets:sc:/g)?.length).toBe(9);
    expect(open).not.toContain("more<");
    const sheet = css("components/flights/IntakeLane.css");
    expect(cssRule(sheet, ".sky-lane[data-open] .sky-lane-item-headline")).toContain("-webkit-line-clamp: unset");
  });

  it("its head is its handle: a press there is the host's drag (not on its buttons, not in click mode); a double-click expands", () => {
    const src = code("components/flights/IntakeLane.tsx");
    expect(src).toContain('const headPress = (e: ReactPointerEvent) => e.button === 0 && !(e.target as Element).closest("button, a, input, summary, [data-no-drag]");');
    expect(src).toContain("if (draggable && onHeadDown && headPress(e)) onHeadDown(e);");
    expect(src).toMatch(/onDoubleClick=\{\(e\) => \{\s*if \(!onToggleOpen \|\| \(e\.target as Element\)\.closest\("button"\)\) return;/);
    // Measured as a line is: a ResizeObserver to onMeasure.
    expect(src).toContain("measureRef.current?.(w, h);");
    const sky = code("components/flights/SkyView.tsx");
    expect(sky).toContain("onHeadDown={(e) => onDragStart(INTAKE_KEY, e)}");
    expect(sky).toContain("onToggleOpen={() => onLayout(INTAKE_KEY, { open: !intakeOpen })}");
    expect(sky).toContain("onMeasure={(w, h) => onMeasure(INTAKE_KEY, w, h)}");
  });

  it("draws nothing until settings has loaded; no sources links to Sources", () => {
    expect(lane([], () => {})).toBe("");
    const html = lane([], (c) => c.setQueryData(["settings"], {}));
    expect(html).toContain("No sources yet.");
  });
});

describe("Fly on a waiting item", () => {
  const waiting = collectQueued({ tickets: TICKETS }).map((item) => ({ item, auto: true }));

  it("posts the Board's start request with the source's flyway and answers the session title", async () => {
    api.mockImplementation(() => Promise.resolve({ title: "sc-1" }));
    const title = await flyLaneItem(waiting[0], { flyway: { id: "p_mine", name: "Mine", own: true } });
    expect(title).toBe("sc-1");
    expect(api.mock.calls[0]).toEqual(["/api/tickets/start", { json: { source: "sc", id: "1", plan: "p_mine" } }]);
  });

  it("a built-in flyway is left to the server (no plan sent)", async () => {
    api.mockImplementation(() => Promise.resolve({}));
    const title = await flyLaneItem(waiting[0], { flyway: { id: "builtin:ticket", name: "Ticket to merge", own: false } });
    expect(api.mock.calls[0][1]).toEqual({ json: { source: "sc", id: "1" } });
    expect(title).toBe("sc-1");
  });

  it("the chip hands the title to onFly and shows a refusal inline", () => {
    const src = code("components/flights/IntakeLane.tsx");
    expect(src).toContain("if (title) onFly(title);");
    expect(src).toContain("setError(errMsg(err));");
    expect(src).not.toMatch(/\balert\(|\bconfirm\(|\bprompt\(/);
  });
});

describe("the new-session block", () => {
  const card = (prime: (c: InstanceType<typeof QueryClient>) => void = () => {}) =>
    withClient(createElement(NewSessionBlock, { x: 120, y: 80, plans: PLANS, onCreated: () => {}, onCancel: () => {} }), prime);

  it("sits at (x, y): repository field with Browse…, the prompt box, stop chips with No stops first, Fly and Cancel", () => {
    const html = card();
    expect(html).toContain('style="left:120px;top:80px"');
    expect(html).toContain("Browse…");
    expect(html).toContain('class="fl-prompt sky-new-prompt"');
    const flyway = html.slice(html.indexOf('class="fl-chiprow sky-new-stops"'));
    const chips = [...flyway.matchAll(/data-value="([^"]*)">([^<]+)</g)].map((m) => m[2]);
    expect(chips.slice(0, 4)).toEqual(["No stops", "Ticket to merge", "Review a PR", "Mine"]);
    expect(flyway).toMatch(/aria-checked="true" class="fl-pick on fl-pick-default"[^>]*>No stops</);
    expect(html).toContain(">Cancel</button>");
    expect(html).not.toContain("<select");
  });

  it("Fly is disabled without a repository", () => {
    expect(card()).toMatch(/<button[^>]*disabled=""[^>]*>Fly<\/button>/);
    expect(flyBlocked("")).toBe("Pick a repository first");
    expect(flyBlocked("myrepo")).toMatch(/full path/);
    expect(flyBlocked("~/code/app")).toBe("");
  });

  it("agent chips only when more than one CLI is installed, the default picked", () => {
    expect(card((c) => c.setQueryData(["providers"], { providers: [{ name: "claude" }], default: "claude" }))).not.toContain("sky-new-agent");
    const html = card((c) => c.setQueryData(["providers"], { providers: [{ name: "claude" }, { name: "codex" }], default: "codex" }));
    const agents = html.slice(html.indexOf('class="fl-chiprow sky-new-agent"'));
    expect(agents).toMatch(/aria-checked="true" class="fl-pick on"[^>]*data-value="codex">codex</);
  });

  it("Escape cancels (and stops there); Enter in the prompt flies, Shift+Enter doesn't", () => {
    const ev = (key: string, over: Record<string, unknown> = {}) => ({ key, preventDefault: vi.fn(), stopPropagation: vi.fn(), ...over });
    const cancel = vi.fn();
    const esc = ev("Escape");
    cardKey(esc, cancel);
    expect(cancel).toHaveBeenCalledTimes(1);
    expect(esc.stopPropagation).toHaveBeenCalled();
    cardKey(ev("a"), cancel);
    expect(cancel).toHaveBeenCalledTimes(1);
    const fly = vi.fn();
    promptKey(ev("Enter"), fly);
    promptKey(ev("Enter", { shiftKey: true }), fly);
    promptKey(ev("Enter", { nativeEvent: { isComposing: true } }), fly);
    expect(fly).toHaveBeenCalledTimes(1);
    const src = code("components/flights/NewSessionBlock.tsx");
    expect(src).toContain("onKeyDown={(e) => cardKey(e, onCancel)}");
    expect(src).toContain("onKeyDown={(e) => promptKey(e, () => void fly())}");
  });

  it("repository chips: recent first, one per path; saved-stop chips: built-ins first", () => {
    const sug = [
      { path: "/h/near", name: "near", is_git: true, source: "nearby" },
      { path: "/h/app", name: "app", is_git: true, source: "recent" },
      { path: "/h/app", name: "app", is_git: true, source: "cwd" },
    ];
    expect(repoChoices(sug).map((c) => c.value)).toEqual(["/h/app", "/h/near"]);
    expect(savedStopChoices(PLANS).map((c) => c.value)).toEqual(["", "builtin:ticket", "builtin:pr", "p_mine"]);
  });

  it("creates through the New dialog's body builder, then takes off on the picked saved stop; errors stay in the card", () => {
    const src = code("components/flights/NewSessionBlock.tsx");
    expect(src).toContain("newSessionSubmit(ctx).buildBody(");
    expect(src).toContain('api<Instance>("/api/instances", { json: body })');
    expect(src).toContain("await startFlightOn(title, plan)");
    expect(src).toContain("onCreated(title);");
    expect(src).not.toMatch(/\balert\(|\bconfirm\(|window\.prompt/);
  });
});
