/** An intake source's default stops, edited in the Sky with the blocks. The
 * owner's rule: our built-in stops are every source's default; editing ONE
 * ticket's line never changes it; a SOURCE's default is edited on the board.
 *
 * The lane's stops chip is a button that makes (or finds — one per source) a
 * grounded chain carrying `edits_default`; that chain IS the editor: no ▶, no
 * Run on a session…, and Save as default / Reset to default / Discard on its
 * bar. The pure rules are lib/groundedLines; the rest is pinned on the source
 * and on the server-rendered chain. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { code, css, cssRule } from "./sourceText";
import type { Flight, FlightStep, IntakeDefault, StepKindDef } from "../lib/flights";

vi.mock("../api/client", () => ({ api: vi.fn(() => new Promise(() => {})), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));
vi.mock("../lib/flightsApi", async (orig) => ({
  ...(await orig<typeof import("../lib/flightsApi")>()),
  getIntakeDefault: vi.fn(),
  startGrounded: vi.fn(),
}));

const G = await import("../lib/groundedLines");
const API = await import("../lib/flightsApi");
const { GroundedLine } = await import("../components/flights/GroundedLine");
const { openDefaultEditor } = await import("../components/flights/IntakeLane");

(globalThis as Record<string, unknown>).document ??= {
  hidden: false,
  addEventListener: () => {},
  removeEventListener: () => {},
  getElementById: () => null,
  querySelector: () => null,
};

const kind = (k: string, label: string): StepKindDef => ({ kind: k, label, actor: "robot", summary: label, fields: [], default_title: label });
const KINDS = [kind("agent", "Agent"), kind("ship", "Ship")];
const step = (id: string, k: string, title: string): FlightStep => ({ id, kind: k, title, config: {}, on_fail: "halt" });

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

const SHORTCUT = { kind: "tickets" as const, target: "sc" };
const editor = (over: Partial<Flight> = {}) => flight({ id: "f_ed", plan_name: "Default · Tickets · Shortcut", edits_default: SHORTCUT, ...over });

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

describe("lib/groundedLines: the default-stops editor", () => {
  it("is made as “Default · <label>” and names its source back from that", () => {
    expect(G.defaultEditorName("Tickets · Shortcut")).toBe("Default · Tickets · Shortcut");
    expect(G.editorLabel(editor())).toBe("Tickets · Shortcut");
    // Renamed by an older client, or nameless: what it has, else the target.
    expect(G.editorLabel(editor({ plan_name: "Mine" }))).toBe("Mine");
    expect(G.editorLabel(editor({ plan_name: "Stops" }))).toBe("sc");
    expect(G.editorLabel(editor({ plan_name: "Default · " }))).toBe("Default ·");
  });

  it("an editor is a chain with edits_default; one source is kind + target", () => {
    expect(G.isDefaultEditor(editor())).toBe(true);
    expect(G.isDefaultEditor(flight())).toBe(false);
    expect(G.isDefaultEditor(null)).toBe(false);
    expect(G.sameSource(SHORTCUT, { kind: "tickets", target: "sc" })).toBe(true);
    expect(G.sameSource(SHORTCUT, { kind: "prs", target: "sc" })).toBe(false);
    expect(G.sameSource(SHORTCUT, { kind: "tickets", target: "jira" })).toBe(false);
    expect(G.sameSource(SHORTCUT, undefined)).toBe(false);
  });

  it("editorFor finds the one editor on the board for a source (grounded only)", () => {
    const fs = [flight(), editor({ id: "prs", edits_default: { kind: "prs", target: "o/r" } }), editor({ id: "gone", state: "done" }), editor()];
    expect(G.editorFor(fs, SHORTCUT)?.id).toBe("f_ed");
    expect(G.editorFor(fs, { kind: "prs", target: "o/r" })?.id).toBe("prs");
    expect(G.editorFor(fs, { kind: "issues", target: "o/r" })).toBeNull();
    expect(G.editorFor(null, SHORTCUT)).toBeNull();
  });

  it("chainRunFor: no ▶ on an editor, on a chain another starts, or on no stops", () => {
    const make = () => ({ run: true });
    expect(G.chainRunFor(editor(), "none", make)).toBeNull();
    expect(G.chainRunFor(flight(), "trigger", make)).toBeNull();
    expect(G.chainRunFor(flight({ steps: [] }), "none", make)).toBeNull();
    expect(G.chainRunFor(flight(), "none", make)).toEqual({ run: true });
  });
});

describe("the lane's stops chip opens the editor", () => {
  const dflt: IntakeDefault = { kind: "tickets", target: "sc", label: "Tickets · Shortcut", plan_id: "builtin-ticket", name: "Ticket to merge", own: false, steps: [step("a", "agent", "Fix it")] };
  beforeEach(() => {
    vi.mocked(API.getIntakeDefault).mockReset().mockResolvedValue(dflt);
    vi.mocked(API.startGrounded).mockReset().mockResolvedValue(editor({ id: "f_new" }));
  });

  it("makes a grounded chain of the source's default, named for it, that edits it", async () => {
    const made = vi.fn();
    const f = await openDefaultEditor(SHORTCUT, [flight()], { made });
    expect(API.getIntakeDefault).toHaveBeenCalledWith(SHORTCUT);
    expect(API.startGrounded).toHaveBeenCalledWith({ steps: dflt.steps, name: "Default · Tickets · Shortcut", edits_default: SHORTCUT });
    expect(f?.id).toBe("f_new");
    expect(made).toHaveBeenCalledWith(f, dflt);
  });

  it("never a second: the editor already on the board is handed back instead", async () => {
    const existing = vi.fn();
    const made = vi.fn();
    const f = await openDefaultEditor(SHORTCUT, [editor()], { existing, made });
    expect(f?.id).toBe("f_ed");
    expect(existing).toHaveBeenCalledWith(f);
    expect(made).not.toHaveBeenCalled();
    expect(API.getIntakeDefault).not.toHaveBeenCalled();
    expect(API.startGrounded).not.toHaveBeenCalled();
  });

  it("a default the server can't read makes nothing", async () => {
    vi.mocked(API.getIntakeDefault).mockResolvedValue(null);
    expect(await openDefaultEditor(SHORTCUT, [], {})).toBeNull();
    expect(API.startGrounded).not.toHaveBeenCalled();
  });

  it("the chip is a tiered button naming what it edits; the Sky places + focuses the editor beside the lane", () => {
    const lane = code("components/flights/IntakeLane.tsx");
    expect(lane).toContain('className="fl-icon-btn sky-lane-flyway-btn"');
    expect(lane).toContain('<span className={"sky-lane-flyway" + (block.flyway.own ? " own" : "")}>{block.flyway.name}</span>');
    expect(lane).toContain('aria-label={"Edit the default stops for " + block.label}');
    expect(lane).toContain("onClick={() => onEditDefault({ kind: block.kind, target: block.id }, block.label)}");
    expect(lane).toContain("else void openDefaultEditor(ref, flights);");
    const sky = code("components/flights/SkyView.tsx");
    expect(sky).toContain("onEditDefault={editDefault}");
    expect(sky).toContain("void openDefaultEditor(ref, flights, {");
    expect(sky).toContain("if (rectOf(key)) jumpTo(key);");
    expect(sky).toContain('const at = spotRef.current(f, "lane");');
    expect(sky).toContain('placing.current.set(key, "lane");');
    expect(sky).toContain("setGroundFocus(key);");
    expect(sky).toContain('const pal = near === "palette" ? rectOf(PALETTE_KEY) : near === "lane" ? rectOf(INTAKE_KEY) : null;');
    // The quiet button is bare: the chip inside keeps its own look.
    expect(cssRule(css("components/flights/IntakeLane.css"), ".sky-lane-chips .sky-lane-flyway-btn")).toContain("border: none");
  });
});

describe("the editor chain", () => {
  const html = render(editor());

  it("its bar names the source (the kind on its Default tag), the hint a caption under it, and it is not renamed", () => {
    expect(html).toContain('class="skl-name skl-default-name" title="Default stops for Tickets · Shortcut. New items from this source fly these.">Shortcut<');
    expect(html).toContain('class="fl-box-chip skl-chip skl-default-tag"');
    expect(html).toContain(">Default · Tickets<");
    expect(html).not.toContain("skl-name-edit");
    expect(html).toContain('<div class="skl-default-hint">New items from Shortcut fly these stops</div>');
    expect(html).toContain("is-default-editor");
    expect(html).toContain('data-edits-default="tickets:sc"');
    expect(html).toContain('aria-label="Default stops · Tickets · Shortcut — new items from this source fly these"');
  });

  it("Save as default (primary) and a quiet × Discard on its bar; Reset only while the source has its own", () => {
    // The default isn't read yet on the first render: Save stays pressable.
    expect(html).toMatch(/<button type="button" class="btn-primary fl-btn skl-default-save"[^>]*>Save as default</);
    expect(html).toMatch(/<button type="button" class="fl-icon-btn skl-default-discard" aria-label="Discard this edit"[^>]*>×</);
    // `own` is read after mount (getIntakeDefault): not yet known, not shown.
    expect(html).not.toContain(">Reset to default<");
    const src = code("components/flights/GroundedLine.tsx");
    expect(src).toContain("{ownDefault && (");
    expect(src).toContain("void getIntakeDefault({ kind: edKind, target: edTarget }).then((d) => {");
    expect(src).toContain("setOwnDefault(!!d?.own);");
  });

  it("no ▶ and no Run on a session… — it never runs", () => {
    expect(html).not.toContain("sky-run");
    const src = code("components/flights/GroundedLine.tsx");
    expect(src).toContain("const run = chainRunFor(flight, head.kind, () => ({");
    expect(src).toContain('...(ed ? [] : [{ key: "fly", label: "Run on a session…"');
    // Its ⋯ Remove (and Delete) discards: the default stays as it was.
    expect(src).toContain('{ key: "remove", label: "Discard", danger: true');
    expect(src).toContain("if (ed) return discard();");
    // A plain grounded chain still has its ▶.
    const plain = render(flight());
    expect(plain).not.toContain("is-default-editor");
    expect(plain).toContain("sky-run");
  });

  it("Save writes its steps as the source's default; Reset puts it back on ours; each leaves with an Undo of the previous saved stop", () => {
    const src = code("components/flights/GroundedLine.tsx");
    expect(src).toContain('const res = await setIntakeDefault(reset ? { ...ref, plan_id: "" } : { ...ref, steps: flight.steps });');
    expect(src).toContain("if (!res) return;");
    expect(src).toContain("await optimisticDelete(flight.id, () => deleteFlight(flight.id));");
    expect(src).toContain('const prev = res.previous_plan_id || "";');
    expect(src).toContain("undo: () => setIntakeDefault({ ...ref, plan_id: prev }),");
    expect(src).toContain('toast(reset ? label + " is back on the built-in stops" : "New items from " + label + " fly these stops", {');
    expect(src).toContain("action: undoAction(entry,");
    expect(src).toContain("onClick={() => void writeDefault(false)}");
    expect(src).toContain("onClick={() => void writeDefault(true)}");
    expect(src).toContain("onClick={() => void discard()}");
  });

  it("its stops edit as any grounded chain's — the same hook, not a fork", () => {
    const src = code("components/flights/GroundedLine.tsx");
    expect(src).toContain("const line = useGroundedLine(flight, { kinds: p.kinds, plans: p.plans });");
    expect(html).toContain("Fix it");
    expect(html).toContain("Push");
  });
});
