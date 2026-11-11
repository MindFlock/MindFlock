/** Flow polish — the stop dialog: a halted stop (or one waiting on you, or a
 * paused route's) leads with why and its remedies, the one that usually works
 * primary, and folds its settings; the current stop can change when it failed
 * or waits at its halt point; Carry on in the dialog; Save validates (client
 * checks first, then the server's sentence under the field it names) and
 * never loses an edit; a Verify stop's Done is "Confirm verified"; the
 * vocabulary (paused / halt point / Add a stop). */

/// <reference types="vite/client" />
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { code } from "./sourceText";
import type { Flight, FlightStep, StepKindDef, StepRunState } from "../lib/flights";
import type { DiagramNode } from "../lib/flightDiagram";

const calls: Array<[string, unknown]> = [];
let failWith: { status: number; body: unknown } | null = null;
vi.mock("../api/client", () => {
  class ApiError extends Error {
    status: number;
    body: unknown;
    constructor(status: number, message: string, body: unknown) {
      super(message);
      this.status = status;
      this.body = body;
    }
  }
  return {
    ApiError,
    api: vi.fn(async (path: string, opts?: unknown) => {
      calls.push([path, opts]);
      if (failWith) {
        const b = failWith.body as { detail?: string; error?: string } | null;
        throw new ApiError(failWith.status, b?.detail || b?.error || "MindFlock couldn't do that (error " + failWith.status + ").", failWith.body);
      }
      return {};
    }),
  };
});
const toast = vi.fn();
const errorPop = vi.fn();
vi.mock("../lib/toast", () => ({ toast: (...a: unknown[]) => toast(...a) }));
vi.mock("../lib/errorPop", () => ({ errorPop: (...a: unknown[]) => errorPop(...a) }));

const SD = await import("../components/flights/StopDialog");
const { hereStop } = await import("../components/flights/useSessionLine");
const API = await import("../lib/flightsApi");

(globalThis as Record<string, unknown>).document ??= {
  hidden: false,
  addEventListener: () => {},
  removeEventListener: () => {},
  getElementById: () => null,
  querySelector: () => null,
};

const COMMAND: StepKindDef = {
  kind: "command",
  label: "Command",
  actor: "robot",
  summary: "Run a command",
  fields: [
    { name: "command", type: "textarea", label: "Command", placeholder: "make test" },
    { name: "timeout_minutes", type: "int", label: "Timeout (minutes)", default: 30 },
  ],
  default_title: "Run",
};
const AGENT: StepKindDef = { kind: "agent", label: "Agent", actor: "robot", summary: "Prompt the agent", fields: [{ name: "prompt", type: "textarea", label: "Prompt" }], default_title: "Agent works" };

const test: FlightStep = { id: "s2", kind: "command", title: "Tests", config: { command: "make test", timeout_minutes: 30 }, on_fail: "halt" };
const verifyStep: FlightStep = { id: "s2", kind: "verify", title: "Verify", config: {}, on_fail: "halt" };
const rec = (state: StepRunState["state"], detail: Record<string, unknown> = {}, note = ""): StepRunState => ({ state, since: 0, note, detail, rounds: 0 });
const flight = (over: Partial<Flight>, step: FlightStep = test, r: StepRunState = rec("pending")): Flight =>
  ({
    id: "f1",
    steps: [{ id: "s1", kind: "agent", title: "Work", config: {}, on_fail: "halt" }, step, { id: "s3", kind: "ship", title: "Push", config: {}, on_fail: "halt" }],
    cursor: 1,
    state: "running",
    reason: "",
    step_states: { s1: rec("done"), [step.id]: r, s3: rec("pending") },
    ...over,
  }) as Flight;
const node = (over: Partial<DiagramNode> = {}, step: FlightStep = test): DiagramNode => ({ id: "stop:s2", kind: "stop", row: "main", title: step.title, sub: "", state: "current", icon: step.kind, step, index: 1, editable: false, ...over });
const render = (props: Record<string, unknown>) => renderToStaticMarkup(createElement(SD.StopDialog, { portal: false, onClose: () => {}, ...props } as never));
const buttons = (html: string) => [...html.matchAll(/<button type="(?:button|submit)"[^>]*>([^<]+)<\/button>/g)].map((m) => m[1]);
const noop = () => {};

describe("hereStop: what the current stop's dialog offers", () => {
  it("a failed stop: its reason in plain words, Retry primary, its settings can change (Save, then Retry)", () => {
    const h = hereStop(flight({ state: "halted", reason: "checks failed" }, test, rec("failed")), node({ state: "halted", reason: "checks failed" }));
    expect(h).toMatchObject({ here: true, why: "It failed: Checks failed", retry: true, resume: false, done: "", editNow: "retry", primary: "retry" });
  });

  it("the route waits at its halt point, nothing run: Carry on primary, no Retry, its settings can change", () => {
    const fl = flight({ state: "paused", reason: "halt point" }, { ...test, halt: true }, rec("pending", { halt_hit: true }));
    const h = hereStop(fl, node({ state: "paused" }, { ...test, halt: true }));
    expect(h).toMatchObject({ retry: false, resume: true, editNow: "resume", primary: "resume" });
    expect(h.why).toBe("Paused at its halt point — nothing has run yet. Carry on runs it.");
  });

  it("a person's pause mid-stop: Carry on primary, the settings stay read-only", () => {
    const h = hereStop(flight({ state: "paused", reason: "" }, test, rec("running")), node({ state: "paused" }));
    expect(h).toMatchObject({ resume: true, retry: false, editNow: "", primary: "resume", why: "Paused — Carry on picks it up again." });
  });

  it("a running stop: nothing to do, and why it can't change", () => {
    const h = hereStop(flight({}, test, rec("running")), node());
    expect(h).toMatchObject({ retry: false, resume: false, done: "", editNow: "", primary: undefined });
    expect(h.why).toBe("It is running — its settings can't change while it runs.");
  });

  it("any stop's dialog on a paused route offers Carry on; a stop ahead is not 'here'", () => {
    const h = hereStop(flight({ state: "paused" }, test, rec("running")), node({ id: "stop:s3", index: 2, editable: true, state: "pending" }, { ...test, id: "s3" }));
    expect(h).toMatchObject({ here: false, resume: true, retry: false, why: "" });
  });

  it("a Verify sign-off: Confirm verified (primary), the steps to check listed, the server's note as why", () => {
    const r = rec("waiting_you", { ask: "confirm", items: ["Open the app", "Log in"] }, "2 steps for you to check, then confirm");
    const h = hereStop(flight({ state: "waiting_you" }, verifyStep, r), node({ state: "perched" }, verifyStep));
    expect(h).toMatchObject({ done: "confirm", retry: false, primary: "done", asks: ["Open the app", "Log in"], why: "2 steps for you to check, then confirm" });
  });

  it("a Verify check that couldn't finish (blocked): Retry primary and Skip, no Done", () => {
    const r = rec("waiting_you", { ask: "blocked", items: ["no browser — press Retry to verify again, or Skip"] }, "the check couldn't finish: no browser");
    const h = hereStop(flight({ state: "waiting_you" }, verifyStep, r), node({ state: "perched" }, verifyStep));
    expect(h).toMatchObject({ done: "", retry: true, primary: "retry", editNow: "", why: "The check couldn't finish: no browser" });
  });

  it("memory refused the check: Retry with the server's reason", () => {
    const r = rec("waiting_you", { ask: "memory" }, "couldn't start the check: memory is low");
    const h = hereStop(flight({ state: "waiting_you" }, verifyStep, r), node({ state: "perched" }, verifyStep));
    expect(h).toMatchObject({ done: "", retry: true, primary: "retry", why: "Couldn't start the check: memory is low" });
  });

  it("a plain perch: Done primary", () => {
    const you: FlightStep = { id: "s2", kind: "you", title: "Approve", config: {}, on_fail: "halt" };
    const h = hereStop(flight({ state: "waiting_you" }, you, rec("waiting_you")), node({ state: "perched" }, you));
    expect(h).toMatchObject({ done: "done", retry: false, primary: "done" });
  });
});

describe("remedies: the one that usually works goes first", () => {
  it("primary first, then Retry · Carry on · Done · Fix · Skip, only those given", () => {
    const r = SD.remedies({ onRetry: noop, onSkip: noop, onFix: noop, onResume: noop, primary: "resume" });
    expect(r.map((x) => [x.label, x.primary])).toEqual([
      ["Carry on", true],
      ["Retry", false],
      ["Let the agent fix it", false],
      ["Skip", false],
    ]);
    expect(SD.remedies({ onDone: noop, doneLabel: "Confirm verified", primary: "done" })[0]).toMatchObject({ label: "Confirm verified", primary: true });
    // A primary with no handler draws nothing primary.
    expect(SD.remedies({ onSkip: noop, primary: "retry" }).map((x) => x.primary)).toEqual([false]);
  });
});

describe("the parked dialog leads with why and what to do", () => {
  const halted = render({
    node: node({ state: "halted", editable: false }),
    def: COMMAND,
    note: "It failed: Checks failed",
    primary: "retry",
    onRetry: noop,
    onFix: noop,
    onSkip: noop,
    onRemove: noop,
    onToggleHalt: noop,
  });

  it("chip and reason, then the remedies (Retry primary), then the settings folded shut", () => {
    const chip = halted.indexOf('class="stop-dialog-chip fl-st-halted">Halted<');
    const why = halted.indexOf("It failed: Checks failed");
    const acts = halted.indexOf('class="stop-dialog-remedies');
    const fold = halted.indexOf('<details class="stop-dialog-settings"><summary>Its settings</summary>');
    expect(chip).toBeGreaterThan(0);
    expect(why).toBeGreaterThan(chip);
    expect(acts).toBeGreaterThan(why);
    expect(fold).toBeGreaterThan(acts);
    expect(halted).toMatch(/<button type="button" class="btn-primary" data-remedy="retry"[^>]*>Retry<\/button>/);
    expect(buttons(halted.slice(acts, fold))).toEqual(["Retry", "Let the agent fix it", "Skip"]);
    // The old opener is gone.
    expect(halted).not.toContain("it can&#x27;t change while it is here");
  });

  it("the halt point toggle sits in the fold and says the route pauses", () => {
    const fold = halted.slice(halted.indexOf("<details"));
    expect(fold).toContain(SD.HALT_LABEL);
    expect(SD.HALT_LABEL).toBe("Halt point — the route pauses before this stop until you press Carry on");
  });

  it("a stop that can change now: the hint says what runs it; Save is the second tier", () => {
    const html = render({
      node: node({ state: "halted", editable: false }),
      def: COMMAND,
      note: "It failed: Checks failed",
      primary: "retry",
      onRetry: noop,
      onSkip: noop,
      onSave: noop,
      saveHint: "Save, then Retry runs it with these settings",
    });
    expect(html).toContain('<p class="fl-note muted stop-dialog-save-hint">Save, then Retry runs it with these settings</p>');
    expect(html).toContain('aria-label="Stop title"');
    expect(html).toMatch(/<button type="submit" class="test-btn"[^>]*>Save<\/button>/);
    expect(html.match(/class="btn-primary"/g)).toHaveLength(1);
  });

  it("a paused route's stop ahead offers Carry on next to Save (Save stays primary)", () => {
    const html = render({ node: node({ state: "pending", editable: true }), def: COMMAND, onSave: noop, onResume: noop });
    const acts = html.slice(html.indexOf('class="modal-actions'));
    expect(buttons(acts)).toEqual(["Carry on", "Cancel", "Save"]);
    expect(acts).toMatch(/<button type="submit" class="btn-primary"/);
    expect(html).not.toContain("<details");
  });

  it("a Verify sign-off lists what to check under its reason", () => {
    const html = render({ node: node({ state: "perched" }, verifyStep), note: "2 steps for you to check, then confirm", asks: ["Open the app", "Log in"], onDone: noop, doneLabel: "Confirm verified", primary: "done", onSkip: noop });
    expect(html).toContain('<ul class="stop-dialog-asks"><li>Open the app</li><li>Log in</li></ul>');
    expect(html).toMatch(/class="btn-primary" data-remedy="done"[^>]*>Confirm verified</);
  });
});

describe("validation as you type", () => {
  it("a field path's last name, and its label from the kind's fields (else the form's own)", () => {
    expect(SD.fieldNameOf("steps[2].config.timeout_minutes")).toBe("timeout_minutes");
    expect(SD.fieldNameOf("step.config.command")).toBe("command");
    expect(SD.fieldNameOf("step.title")).toBe("title");
    expect(SD.fieldNameOf("steps[1]")).toBe("");
    expect(SD.fieldNameOf("")).toBe("");
    expect(SD.fieldLabelOf("steps[2].config.timeout_minutes", COMMAND)).toBe("Timeout (minutes)");
    expect(SD.fieldLabelOf("step.on_fail", COMMAND)).toBe("If it fails");
    expect(SD.fieldLabelOf("step.config.nope", COMMAND)).toBe("");
  });

  it("a Command stop with no command needs one — shown at once, and Save stays open", () => {
    expect(SD.clientCheck({ kind: "command", config: { command: "  " } })).toEqual({ field: "command", detail: "Needs a command" });
    expect(SD.clientCheck({ kind: "command", config: { command: "make" } })).toBeNull();
    const html = render({ node: node({ state: "pending", editable: true }, { ...test, config: { command: "" } }), def: COMMAND, onSave: noop });
    // Under the Command field itself (StopForm's errors), not above the form.
    expect(html).toContain('<span class="fl-fld-error" role="alert">Needs a command</span>');
    expect(html).not.toContain("stop-dialog-error");
    expect(code("components/flights/StopDialog.tsx")).toContain("if (check) setRefused(check);\n          else if (dirty) void save();\n          else p.onClose();");
  });

  it("an empty Agent prompt is a hint, not an error", () => {
    expect(SD.clientCheck({ kind: "agent", config: { prompt: "" } })).toBeNull();
    const agent: FlightStep = { id: "s2", kind: "agent", title: "Work", config: { prompt: "" }, on_fail: "halt" };
    const html = render({ node: node({ state: "pending", editable: true }, agent), def: AGENT, onSave: noop });
    expect(html).toContain("Prompt: " + SD.AGENT_EMPTY_HINT);
    expect(SD.AGENT_EMPTY_HINT).toBe("Empty = the agent carries on with its task");
    expect(html).not.toContain('role="alert"');
  });

  it("a refusal stays open under its field; ok clears the edits", () => {
    const src = code("components/flights/StopDialog.tsx");
    expect(src).toContain("if (isSaveResult(r)) {");
    expect(src).toContain("setRefused({ field: r.field, detail: r.detail || \"The server didn't take it\" });");
  });
});

describe("never lose an edit", () => {
  const src = code("components/flights/StopDialog.tsx");

  it("Esc, the backdrop and Cancel ask before dropping a changed draft (the repo's InlineConfirm)", () => {
    expect(src).toContain("const close = () => (dirty ? setAsking(true) : p.onClose());");
    expect(src).toContain("<Shell p={p} title={title} onClose={close}");
    expect(src).toContain('title="Discard your changes?"');
    expect(src).toContain("<InlineConfirm");
    expect(src).not.toMatch(/\b(window\.)?(confirm|prompt|alert)\(/);
  });

  it("the route reaching the stop mid-edit keeps the draft on screen and says why Save went", () => {
    expect(SD.SAVE_LOST).toBe("The route reached this stop — your changes can't be saved now");
    expect(src).toContain("const lost = hadSave && !p.onSave && dirty;");
    // The form draws the draft (`shown`), read-only once Save is gone.
    expect(src).toContain("const shown = draft || n.step || null;");
  });

  it("Retry / Carry on / the agent's fix save unsaved settings first", () => {
    expect(src).toContain('if (editable && dirty && (k === "retry" || k === "resume" || k === "fix")) void save().then((ok) => ok && go());');
  });
});

describe("the session line's wiring", () => {
  const src = code("components/flights/useSessionLine.tsx");

  it("Carry on in the dialog; every remedy closes it once it went through", () => {
    expect(src).toContain('onResume={h.resume ? () => actClose(() => controlFlight(flight!.id, "resume")) : undefined}');
    expect(src).toContain("const actClose = (f: () => Promise<boolean>) => void act(async () => (await f()) && close());");
    for (const f of ["stepRetry", "stepSkip", "stepDone", "stepFix"]) expect(src).toContain("actClose(() => " + f + "(flight!.id, open.step!.id))");
  });

  it("a Verify stop's Done is Confirm verified; no Behaviors detour", () => {
    expect(src).toContain('doneLabel={h.done === "confirm" ? "Confirm verified" : undefined}');
    expect(src).not.toContain("answerBehaviors");
    expect(src).not.toContain('"Behaviors"');
  });

  it("the current stop saves through saveStop and stays open for Retry / Carry on", () => {
    expect(src).toContain("const r = await saveStop(flight!.id, open.step!.id, patch);");
    expect(src).toContain("if (r.ok && !h.editNow) close();");
    expect(src).toContain('"Save, then " + (h.editNow === "retry" ? "Retry" : "Carry on") + " runs it with these settings"');
  });

  it("Add a stop, never Tack on", () => {
    expect(src).toContain('paletteAt >= end ? "Add a stop"');
    expect(src).not.toMatch(/"[^"]*Tack on[^"]*"/);
  });
});

describe("flightsApi: saves resolve to {ok, field, detail}", () => {
  beforeEach(() => {
    calls.length = 0;
    failWith = null;
    toast.mockClear();
    errorPop.mockClear();
  });

  it("saveStop: the server's refusal verbatim, its field, no card", async () => {
    failWith = { status: 400, body: { error: "bad_config", detail: "timeout_minutes must be a whole number from 1 to 1440", field: "step.config.timeout_minutes" } };
    const r = await API.saveStop("f1", "s2", { config: { timeout_minutes: 0 } });
    expect(r).toEqual({ ok: false, error: "bad_config", detail: "timeout_minutes must be a whole number from 1 to 1440", field: "step.config.timeout_minutes" });
    expect(errorPop).not.toHaveBeenCalled();
    expect(calls[0][0]).toBe("/api/flights/f1/steps/s2");
  });

  it("saveStop: ok; an older server's bare refusal still has a sentence", async () => {
    expect(await API.saveStop("f1", "s2", { title: "x" })).toEqual(API.SAVED);
    failWith = { status: 409, body: { error: "in_progress" } };
    expect(await API.saveStop("f1", "s2", { title: "x" })).toEqual({ ok: false, field: "", detail: "in_progress", error: "in_progress" });
    expect(API.isSaveResult(API.SAVED)).toBe(true);
    expect(API.isSaveResult(undefined)).toBe(false);
  });

  it("patchStop keeps its boolean and its card (callers with no form)", async () => {
    failWith = { status: 400, body: { detail: "nope", field: "step.title" } };
    expect(await API.patchStop("f1", "s2", { title: "" })).toBe(false);
    expect(errorPop).toHaveBeenCalledWith("Saving the stop failed", "nope");
  });

  it("savePlanResult / takeOffResult name the field", async () => {
    failWith = { status: 400, body: { error: "bad_plan", detail: "Runs in all must be 1–50", field: "steps[2].config.times" } };
    const plan = await API.savePlanResult({ id: "", name: "x", steps: [] } as never);
    expect(plan).toMatchObject({ ok: false, field: "steps[2].config.times", detail: "Runs in all must be 1–50", plan: null });
    const start = await API.takeOffResult({ plan_id: "p1" });
    expect(start).toMatchObject({ ok: false, field: "steps[2].config.times", flight: null });
    expect(errorPop).not.toHaveBeenCalled();
    expect(await API.takeOff({ plan_id: "p1" })).toBeNull();
    expect(errorPop).toHaveBeenCalledWith("Starting the route failed", "Runs in all must be 1–50");
  });

  it("a pause is 'Route paused'", async () => {
    await API.controlFlight("f1", "pause");
    expect(toast).toHaveBeenCalledWith("Route paused — Carry on picks it up again");
  });
});
