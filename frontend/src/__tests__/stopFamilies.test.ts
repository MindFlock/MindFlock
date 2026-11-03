/** The palette's and the catalog's kinds grouped by family (docs/flights.md
 * "Round 15" → `palette-groups`, "As built: round 15 palette-groups"):
 * lib/stopCatalog's `familyOrder` / `familyLabel` / `familyGroups` and the
 * groups `catalogCards` hands the body — Work · Ship · Outside · People in
 * that order, the defaults-first order kept inside each, a filter hiding the
 * groups it empties, one flat group (no labels at all) when the server names
 * no family; then StopCatalogBody's label rows (the Session lead first in
 * Work, the perch gold inside People, Multi-step after them), the floating
 * palette's, and the bottom shelf staying the flat ★ pick. */

import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { css as cssOf } from "./sourceText";
import type { FlightPlan, FlightStep, StepKindDef } from "../lib/flights";

vi.mock("../api/client", () => ({ api: vi.fn(() => new Promise(() => {})), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const { QueryClient, QueryClientProvider } = await import("@tanstack/react-query");
const { catalogCards, familyGroups, familyLabel, familyOrder, gridMove, hasFamilies } = await import("../lib/stopCatalog");
const { StopCatalogBody } = await import("../components/flights/StopCatalog");
const { SkyPalette } = await import("../components/flights/SkyPalette");
const { StopShelf } = await import("../components/flights/StopShelf");

(globalThis as Record<string, unknown>).document ??= {
  hidden: false,
  addEventListener: () => {},
  removeEventListener: () => {},
  getElementById: () => null,
  querySelector: () => null,
};

const kind = (k: string, label: string, family?: string, actor: "robot" | "you" = "robot"): StepKindDef => ({
  kind: k,
  label,
  actor,
  summary: label + " does its thing",
  fields: [],
  default_title: label,
  ...(family ? { family } : {}),
});
// The server's registry order, with the built-ins' families.
const KINDS = [
  kind("agent", "Agent", "work"),
  kind("split", "Split", "work"),
  kind("ship", "Ship", "ship"),
  kind("verify", "Verify", "people"),
  kind("you", "You", "people", "you"),
  kind("wait", "Wait", "outside"),
  kind("notify", "Notify", "outside"),
  kind("join", "Join", "work"),
  kind("checks", "Checks", "ship"),
];
// An older server: the same kinds, no family on any.
const FLAT = KINDS.map(({ family: _f, ...k }) => k as StepKindDef);
const step = (i: number, k = "ship"): FlightStep => ({ id: "s" + i, kind: k, title: "Step " + i, config: {}, on_fail: "halt" });
const plan = (id: string, name: string, steps: FlightStep[], builtin = false): FlightPlan => ({
  id,
  name,
  description: "",
  builtin,
  trigger: { kind: "manual", schedule: null, paused: false },
  session: { repo_path: "", program: "", model: "", in_place: false },
  steps,
  updated: 1,
});
const TICKET = plan("builtin:ticket", "Ticket to merge", [step(1, "agent"), step(2)], true);
const MINE = plan("p_mine", "Mine", [step(1, "agent")]);

const groupsOf = (gs: ReadonlyArray<{ family: string; items: ReadonlyArray<{ kind?: string; key?: string }> }>) =>
  gs.map((g) => g.family + ":" + g.items.map((x) => x.kind ?? (x.key || "").replace(/^kind:/, "")).join(","));

describe("the families (lib/stopCatalog)", () => {
  it("Work · Ship · Outside · People, in that order; a label per family", () => {
    expect([...familyOrder]).toEqual(["work", "ship", "outside", "people"]);
    expect(familyOrder.map(familyLabel)).toEqual(["Work", "Ship", "Outside", "People"]);
    expect(familyLabel("weather")).toBe("Weather");
    expect(familyLabel(undefined)).toBe("Other");
    expect(familyLabel("")).toBe("Other");
  });

  it("groups in the family order, each keeping the order given; only the groups with kinds", () => {
    expect(groupsOf(familyGroups(KINDS))).toEqual(["work:agent,split,join", "ship:ship,checks", "outside:wait,notify", "people:verify,you"]);
    expect(groupsOf(familyGroups(KINDS.filter((k) => k.family !== "ship")))).toEqual(["work:agent,split,join", "outside:wait,notify", "people:verify,you"]);
  });

  it("a family the frontend doesn't know comes after People (as first seen); a kind with none is Other, last", () => {
    const kinds = [kind("odd", "Odd", "weather"), kind("plain", "Plain"), ...KINDS.slice(0, 3)];
    const gs = familyGroups(kinds);
    expect(groupsOf(gs)).toEqual(["work:agent,split", "ship:ship", "weather:odd", "other:plain"]);
    expect(gs.map((g) => g.label)).toEqual(["Work", "Ship", "Weather", "Other"]);
  });

  it("no family on any kind (an older server): one flat group with no label, or none when there is nothing", () => {
    expect(hasFamilies(FLAT)).toBe(false);
    expect(hasFamilies(KINDS)).toBe(true);
    expect(familyGroups(FLAT)).toEqual([{ family: "", label: "", items: FLAT }]);
    expect(familyGroups([])).toEqual([]);
  });
});

describe("the cards by family (catalogCards)", () => {
  it("the defaults-first order inside each family; Blocks and the arrows' order follow the groups", () => {
    const c = catalogCards(KINDS, [TICKET, MINE], "");
    expect(c.grouped).toBe(true);
    // catalogKinds: agent you ship verify wait | split notify join checks.
    expect(groupsOf(c.groups)).toEqual(["work:agent,split,join", "ship:ship,checks", "outside:wait,notify", "people:you,verify"]);
    expect(c.groups.map((g) => g.label)).toEqual(["Work", "Ship", "Outside", "People"]);
    expect(c.blocks.map((b) => b.drop.kind)).toEqual(["agent", "split", "join", "ship", "checks", "wait", "notify", "you", "verify"]);
    expect(c.all.map((x) => ("kind" in x.drop ? x.drop.kind : x.drop.planId))).toEqual([...c.blocks.map((b) => b.drop.kind), "builtin:ticket", "p_mine"]);
  });

  it("a filter hides the groups it empties (and keeps grouping by every kind, not the matches)", () => {
    expect(groupsOf(catalogCards(KINDS, [], "ship").groups)).toEqual(["ship:ship"]);
    expect(groupsOf(catalogCards(KINDS, [], "wait perch").groups)).toEqual([]);
    expect(groupsOf(catalogCards(KINDS, [], "perch").groups)).toEqual(["people:you"]);
    const c = catalogCards(KINDS, [TICKET], "nothing at all");
    expect(c.groups).toEqual([]);
    expect(c.grouped).toBe(true);
  });

  it("an older server: one flat group, no labels, the round-14 order", () => {
    const c = catalogCards(FLAT, [], "");
    expect(c.grouped).toBe(false);
    expect(c.groups).toHaveLength(1);
    expect(c.groups[0].label).toBe("");
    expect(c.blocks.map((b) => b.drop.kind)).toEqual(["agent", "you", "ship", "verify", "wait", "split", "notify", "join", "checks"]);
  });

  it("the arrows walk the family grids as laid out: ↓ from Work's last row lands in Ship", () => {
    // Work 3, Ship 2, Outside 2, People 2, Multi-step 2; 2 a row.
    const sizes = [3, 2, 2, 2, 2];
    expect(gridMove(2, "ArrowDown", sizes, 2)).toBe(3);
    expect(gridMove(1, "ArrowDown", sizes, 2)).toBe(2);
    expect(gridMove(3, "ArrowUp", sizes, 2)).toBe(2);
    // People [7 8] → Multi-step [9 10], keeping the column.
    expect(gridMove(7, "ArrowDown", sizes, 2)).toBe(9);
    expect(gridMove(8, "ArrowDown", sizes, 2)).toBe(10);
  });
});

const head = (filter: unknown) => createElement("header", { className: "h" }, filter as never);
const body = (props: Record<string, unknown> = {}) =>
  renderToStaticMarkup(createElement(StopCatalogBody, { kinds: KINDS, plans: [TICKET, MINE], head, ...props } as never));
const labels = (html: string) => [...html.matchAll(/<h3 class="sky-catalog-label">([^<]*)<\/h3>/g)].map((m) => m[1]);
const families = (html: string) => [...html.matchAll(/data-family="([^"]+)"/g)].map((m) => m[1]);
const kindsIn = (html: string) => [...html.matchAll(/data-kind="([^"]+)"/g)].map((m) => m[1]);

describe("StopCatalogBody by family", () => {
  it("a thin label row per family — Work · Ship · Outside · People — then Saved chains after them", () => {
    const html = body({ onAdd() {} });
    expect(html).toContain('class="sky-catalog-list sky-own-scroll grouped" id="sky-catalog-list" role="listbox"');
    expect(labels(html)).toEqual(["Work", "Ship", "Outside", "People", "Saved chains"]);
    expect(families(html)).toEqual(["work", "ship", "outside", "people"]);
    expect(html).toContain('<div class="sky-catalog-family" data-family="work"><h3 class="sky-catalog-label">Work</h3><div class="sky-catalog-grid"><div id="sky-catalog-row-0"');
    expect(kindsIn(html)).toEqual(["agent", "split", "join", "ship", "checks", "wait", "notify", "you", "verify"]);
    // One grid per family, and the saved stops' own.
    expect(html.match(/<div class="sky-catalog-grid">/g)).toHaveLength(5);
    expect(html.indexOf('data-section="blocks"')).toBeLessThan(html.indexOf('data-section="multi"'));
  });

  it("the perch keeps its gold inside People", () => {
    const html = body();
    const people = html.slice(html.indexOf('data-family="people"'), html.indexOf('data-section="multi"'));
    expect(people).toMatch(/class="sky-catalog-card perch( active)?" data-kind="you"/);
    expect(people).toContain("fl-stop-icon-perch");
  });

  it("the Session lead sits first in Work — even when Work has nothing else", () => {
    const lead = () => createElement("div", { className: "lead" }, "Session");
    expect(body({ lead })).toContain('data-family="work"><h3 class="sky-catalog-label">Work</h3><div class="sky-catalog-grid"><div class="lead">Session</div><div id="sky-catalog-row-0"');
    const html = body({ lead, kinds: KINDS.filter((k) => k.family !== "work") });
    expect(families(html)).toEqual(["work", "ship", "outside", "people"]);
    expect(html).toContain('<div class="sky-catalog-grid"><div class="lead">Session</div></div></div><div class="sky-catalog-family" data-family="ship">');
  });

  it("an older server: no family labels at all — the round-14 Stops grid", () => {
    const html = body({ kinds: FLAT });
    expect(html).toContain('class="sky-catalog-list sky-own-scroll" id="sky-catalog-list"');
    expect(html).not.toContain("data-family");
    expect(labels(html)).toEqual(["Stops", "Saved chains"]);
    expect(html.match(/<div class="sky-catalog-grid">/g)).toHaveLength(2);
  });

  it("the label row: the label, then a hairline to the edge (grouped lists only)", () => {
    const css = cssOf("components/flights/StopCatalog.css");
    expect(css).toMatch(/\.sky-catalog-list\.grouped \.sky-catalog-label \{\s*display: flex;\s*align-items: center;/);
    expect(css).toMatch(/\.sky-catalog-list\.grouped \.sky-catalog-label::after \{\s*content: "";\s*flex: 1;\s*height: 1px;/);
    expect(css).toMatch(/\.sky-catalog-family \{\s*display: flex;\s*flex-direction: column;/);
  });
});

function render(el: ReactElement, kinds: StepKindDef[]) {
  const c = new QueryClient({ defaultOptions: { queries: { retry: false, enabled: false } } });
  c.setQueryData(["flight-kinds"], kinds);
  c.setQueryData(["flight-plans"], [MINE, TICKET]);
  c.setQueryData(["settings"], {});
  vi.stubGlobal("localStorage", { getItem: () => null, setItem: () => {} });
  return renderToStaticMarkup(createElement(QueryClientProvider, { client: c }, el));
}

describe("the palette groups; the bottom shelf stays flat", () => {
  it("the floating palette: Session first in Work, every family, then the saved stops", () => {
    const html = render(createElement(SkyPalette, { onPlace() {}, onSession() {} }), KINDS);
    expect(html).toContain('class="sky-catalog-list sky-own-scroll compact grouped sky-palette-body" id="sky-palette-list"');
    expect(labels(html)).toEqual(["Work", "Ship", "Outside", "People", "Saved chains"]);
    expect(html).toMatch(/data-family="work"><h3 class="sky-catalog-label">Work<\/h3><div class="sky-catalog-grid"><div role="option" aria-selected="false" class="sky-catalog-card sky-catalog-session"/);
    // The count is every block still.
    expect(html).toContain('<span class="sky-palette-count" title="11 stops and saved chains — ★ puts one on the bottom shelf">11</span>');
  });

  it("the shelf: the ★ pick, flat, exactly as an older server's", () => {
    const grouped = render(createElement(StopShelf, {}), KINDS);
    expect(kindsIn(grouped)).toEqual(["agent", "you", "ship", "verify", "wait"]);
    expect(grouped).not.toContain("data-family");
    expect(grouped).not.toContain("sky-catalog-label");
    expect(grouped).toBe(render(createElement(StopShelf, {}), FLAT));
  });
});
