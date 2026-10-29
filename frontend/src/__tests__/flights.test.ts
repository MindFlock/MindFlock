/** Flights' pure rules (lib/flights.ts): a step's glyph and state, a flight's
 * progress, who waits on whom, the Sky toggle's perch badge, the pane strip's
 * line, the flyway card's block moves, the trigger's schedule round trip, and
 * where the retired Flights dialog's addresses land now. */

import { describe, expect, it } from "vitest";
import {
  blankPlan,
  builtinPlanFor,
  currentElapsed,
  flightDrives,
  duplicateStep,
  fieldOptions,
  flightProgress,
  isLive,
  moveStep,
  needsYouFlights,
  newStep,
  legacyFlightsRoute,
  perchBadge,
  planDirty,
  rowWaitsOnYou,
  scheduleOfWhen,
  stepActor,
  stepGlyph,
  stepStateOf,
  stripDots,
  stripLabel,
  whenFormOf,
  waitingAuto,
  youItems,
  perchLabel,
  perchedOnYou,
  youText,
  type Flight,
  type FlightRow,
  type FlightStep,
  type StepKindDef,
} from "../lib/flights";

const NOW_S = 1_800_000_000;
const NOW_MS = NOW_S * 1000;

const step = (id: string, kind: string, title: string, config: Record<string, unknown> = {}): FlightStep => ({
  id,
  kind,
  title,
  config,
  on_fail: "halt",
});

const STEPS: FlightStep[] = [
  step("s1", "agent", "Work the ticket"),
  step("s2", "ship", "Commit", { lane: "commit" }),
  step("s3", "ship", "Push", { lane: "push" }),
  step("s4", "reviews", "Reviews settle"),
  step("s5", "you", "Approve the merge", { text: "Look at the PR", items: ["Read the diff", "Checks green"] }),
  step("s6", "ship", "Merge", { lane: "merge" }),
];

function flight(over: Partial<Flight> = {}): Flight {
  return {
    id: "f_1",
    plan_id: "builtin:ticket",
    plan_name: "Ticket to merge",
    steps: STEPS,
    subject: { kind: "ticket", source: "sc", id: "sc-12", title: "Fix the login", url: "" },
    session: "sc-12-fix-login",
    repo_root: "/r",
    branch: "sc-12",
    state: "running",
    cursor: 3,
    step_states: {
      s1: { state: "done", since: NOW_S - 3600, note: "", detail: {}, rounds: 0 },
      s2: { state: "done", since: NOW_S - 1800, note: "", detail: {}, rounds: 0 },
      s3: { state: "skipped", since: NOW_S - 1700, note: "", detail: {}, rounds: 0 },
      s4: { state: "running", since: NOW_S - 240, note: "", detail: {}, rounds: 0 },
    },
    reason: "",
    history: [],
    started: NOW_S - 3600,
    updated: NOW_S - 240,
    finished: 0,
    by: "intake",
    ...over,
  };
}

describe("a step's glyph and state", () => {
  it("done ✓ · current ● · pending ○ · failed ✗ · skipped –", () => {
    expect(stepGlyph("done")).toBe("✓");
    expect(stepGlyph("running")).toBe("●");
    expect(stepGlyph("waiting_you")).toBe("●");
    expect(stepGlyph("pending")).toBe("○");
    expect(stepGlyph("failed")).toBe("✗");
    expect(stepGlyph("skipped")).toBe("–");
  });

  it("reads the driver's record first, else the cursor", () => {
    const f = flight({ step_states: {} });
    expect(stepStateOf(f, STEPS[0], 0)).toBe("done");
    expect(stepStateOf(f, STEPS[3], 3)).toBe("running");
    expect(stepStateOf({ ...f, state: "waiting_you" }, STEPS[3], 3)).toBe("waiting_you");
    expect(stepStateOf(f, STEPS[4], 4)).toBe("pending");
    expect(stepStateOf(flight(), STEPS[2], 2)).toBe("skipped");
  });

  it("knows who acts: the registry's word, else only a `you` block is yours", () => {
    const kinds = [{ kind: "verify", actor: "you" }] as unknown as StepKindDef[];
    expect(stepActor({ kind: "you" })).toBe("you");
    expect(stepActor({ kind: "verify" })).toBe("robot");
    expect(stepActor({ kind: "verify" }, kinds)).toBe("you");
  });
});

describe("a flight's progress", () => {
  it("counts done and skipped, names the current step and its record", () => {
    const p = flightProgress(flight());
    expect(p.done).toBe(3);
    expect(p.total).toBe(6);
    expect(p.current?.title).toBe("Reviews settle");
    expect(p.currentState?.since).toBe(NOW_S - 240);
    expect(p.states).toEqual(["done", "done", "skipped", "running", "pending", "pending"]);
  });

  it("says how long the current step has been current, with the app's one formatter", () => {
    expect(currentElapsed(flight(), NOW_MS)).toBe("4m");
    expect(currentElapsed(flight({ step_states: {} }), NOW_MS)).toBe("");
  });

  it("is live while running, parked on you or halted", () => {
    expect(isLive({ state: "running" })).toBe(true);
    expect(isLive({ state: "waiting_you" })).toBe(true);
    expect(isLive({ state: "halted" })).toBe(true);
    expect(isLive({ state: "done" })).toBe(false);
    expect(isLive({ state: "cancelled" })).toBe(false);
  });
});

describe("what waits on you", () => {
  const parked = flight({ id: "f_2", state: "waiting_you", cursor: 4 });
  const halted = flight({ id: "f_3", state: "halted", reason: "Checks failed" });
  const onVerify = flight({
    id: "f_4",
    state: "waiting_you",
    cursor: 0,
    steps: [step("v1", "verify", "Verify")],
  });

  it("is the flights parked on a you step or halted", () => {
    expect(needsYouFlights([flight(), parked, halted]).map((f) => f.id)).toEqual(["f_2", "f_3"]);
  });

  it("the Sky toggle's badge counts the flights perched on you (or halted)", () => {
    expect(perchBadge([flight(), parked, halted])).toEqual({ count: 2, failing: true });
    expect(perchBadge([flight(), parked])).toEqual({ count: 1, failing: false });
  });

  it("never counts a flight parked on a verify step (Verify's own badge does)", () => {
    expect(perchBadge([onVerify]).count).toBe(0);
  });

  it("says nothing at all on zero", () => {
    expect(perchBadge([])).toEqual({ count: 0, failing: false });
    expect(perchBadge([flight()])).toEqual({ count: 0, failing: false });
  });

  it("a you step's items and text", () => {
    expect(youItems(STEPS[4])).toEqual(["Read the diff", "Checks green"]);
    expect(youItems(STEPS[0])).toEqual([]);
    expect(youText(parked)).toBe("Look at the PR");
    expect(youText(halted)).toBe("Checks failed");
  });
});

describe("a paused flight", () => {
  const paused = flight({ id: "f_9", state: "paused" });

  it("is in the air but waits on nobody: In flight, not Needs you, not the badge", () => {
    expect(isLive(paused)).toBe(true);
    expect(needsYouFlights([paused])).toEqual([]);
    expect(perchBadge([paused])).toEqual({ count: 0, failing: false });
  });

  it("still owns the ship ladder, as running and waiting_you do; halted, done and cancelled give it back", () => {
    for (const st of ["running", "waiting_you", "paused"] as const) expect(flightDrives({ state: st }), st).toBe(true);
    for (const st of ["halted", "done", "cancelled"] as const) expect(flightDrives({ state: st }), st).toBe(false);
    expect(flightDrives(null)).toBe(false);
  });

  it("says Paused on the strip", () => {
    expect(stripLabel(paused, NOW_MS)).toBe("Ticket to merge · ●●–●○○ · Paused · Reviews settle (4m)");
  });
});

describe("a perch's words", () => {
  it("Perch: is added once, never twice (nor after the old You:)", () => {
    expect(perchLabel("Approve the merge")).toBe("Perch: Approve the merge");
    expect(perchLabel("Perch: approve the merge")).toBe("Perch: approve the merge");
    expect(perchLabel("You: approve the merge")).toBe("Perch: approve the merge");
    expect(perchLabel("you:check it")).toBe("Perch: check it");
    expect(perchLabel("")).toBe("Perch");
  });

  it("the bell says Perched on you", () => {
    expect(perchedOnYou("Approve the merge")).toBe("Perched on you: Approve the merge");
    expect(perchedOnYou("Perch: approve")).toBe("Perched on you: approve");
    expect(perchedOnYou("")).toBe("Perched on you");
  });

  it("a perch's checklist reads a list or the editor's lines", () => {
    expect(youItems({ config: { items: "Read the diff\n\n  Checks green " } })).toEqual(["Read the diff", "Checks green"]);
  });
});

describe("the pane strip's line", () => {
  it("flyway · dots · stop (elapsed) from a full flight — no plane glyph, the strip draws the bird", () => {
    expect(stripLabel(flight(), NOW_MS)).toBe("Ticket to merge · ●●–●○○ · Reviews settle (4m)");
  });

  it("from a session row: the cursor fills the dots, no elapsed", () => {
    const row: FlightRow = {
      id: "f_1",
      plan_name: "Ticket to merge",
      cursor: 2,
      total: 6,
      state: "running",
      step: { id: "s3", kind: "ship", title: "Push", state: "running", actor: "robot" },
    };
    expect(stripDots(row)).toBe("●●●○○○");
    expect(stripLabel(row)).toBe("Ticket to merge · ●●●○○○ · Push");
    expect(rowWaitsOnYou(row)).toBe(false);
    expect(rowWaitsOnYou({ ...row, state: "waiting_you", step: { ...row.step, actor: "you" } })).toBe(true);
    expect(rowWaitsOnYou(null)).toBe(false);
  });

  it("says Landed once it is done", () => {
    expect(stripLabel(flight({ state: "done", cursor: 6 }), NOW_MS)).toMatch(/· Landed$/);
  });
});

describe("waiting to fly", () => {
  it("start by themselves on the engine (tickets) or their own switch (PRs, issues)", () => {
    expect(waitingAuto("tickets", { available: true, desired: true })).toBe(true);
    expect(waitingAuto("tickets", { available: true, desired: false, running: true })).toBe(false);
    expect(waitingAuto("prs", { pr_enabled: true })).toBe(true);
    expect(waitingAuto("issues", { issues_enabled: false })).toBe(false);
    expect(waitingAuto("prs", null)).toBe(false);
  });

  it("default to the built-in plan of their kind (an issue is worked like a ticket)", () => {
    expect(builtinPlanFor("tickets")).toBe("builtin:ticket");
    expect(builtinPlanFor("prs")).toBe("builtin:pr");
    expect(builtinPlanFor("issues")).toBe("builtin:ticket");
    expect(builtinPlanFor("schedule")).toBe("builtin:schedule");
  });

  it("use builtin:<kind> when the server ships it, else the ticket plan", () => {
    const shipped = [{ id: "builtin:ticket" }, { id: "builtin:pr" }, { id: "builtin:issue" }];
    expect(builtinPlanFor("issues", shipped)).toBe("builtin:issue");
    expect(builtinPlanFor("issues", [{ id: "builtin:ticket" }])).toBe("builtin:ticket");
    expect(builtinPlanFor("prs", [{ id: "builtin:ticket" }])).toBe("builtin:ticket");
    expect(builtinPlanFor("prs", shipped)).toBe("builtin:pr");
  });
});

describe("the plan editor's blocks", () => {
  const KIND: StepKindDef = {
    kind: "wait",
    label: "Wait",
    actor: "robot",
    summary: "Waits a while",
    default_title: "Wait",
    fields: [
      { name: "minutes", type: "int", label: "Minutes", default: 5 },
      { name: "until", type: "text", label: "Until" },
    ],
  };

  it("a new block takes its kind's default title and field defaults, under a free id", () => {
    expect(newStep(KIND, ["s1", "s2"])).toEqual({ id: "s3", kind: "wait", title: "Wait", config: { minutes: 5 }, on_fail: "halt" });
    expect(newStep(KIND, ["s1", "s3"]).id).toBe("s4");
  });

  it("moves, duplicates and compares", () => {
    const ids = (xs: FlightStep[]) => xs.map((s) => s.id);
    expect(ids(moveStep(STEPS, 0, 2))).toEqual(["s2", "s3", "s1", "s4", "s5", "s6"]);
    expect(ids(moveStep(STEPS, 5, 0))).toEqual(["s6", "s1", "s2", "s3", "s4", "s5"]);
    expect(moveStep(STEPS, 9, 0)).toEqual(STEPS);
    const dup = duplicateStep(STEPS, 1);
    expect(ids(dup)).toEqual(["s1", "s2", "s7", "s3", "s4", "s5", "s6"]);
    expect(dup[2].config).not.toBe(STEPS[1].config);
    const p = blankPlan();
    expect(planDirty(p, { ...p, updated: 99 })).toBe(false);
    expect(planDirty(p, { ...p, name: "x" })).toBe(true);
  });

  it("reads a select's options as plain values or value/label pairs", () => {
    expect(
      fieldOptions({ name: "lane", type: "select", label: "Lane", options: ["commit", { value: "pr", label: "Open PR" }] })
    ).toEqual([
      { value: "commit", label: "commit" },
      { value: "pr", label: "Open PR" },
    ]);
  });
});

describe("the schedule trigger, through Schedule's own editor", () => {
  it("round-trips every shape normalize_schedule stores", () => {
    for (const s of [{ at: "07:30", days: "weekdays" as const }, { cron: "*/30 * * * *" }, { once: "2026-11-02T21:00" }])
      expect(scheduleOfWhen(whenFormOf(s))).toEqual(s);
    expect(whenFormOf(null).days).toBe("daily");
    expect(scheduleOfWhen({ ...whenFormOf(null), days: "cron", cron: " " })).toBeNull();
  });
});

describe("the retired Flights dialog's addresses", () => {
  it("land on the sky, the sky's shelf, or the flat dialog they named", () => {
    expect(legacyFlightsRoute(null)).toEqual({ to: "sky", focus: "" });
    expect(legacyFlightsRoute("")).toEqual({ to: "sky", focus: "" });
    expect(legacyFlightsRoute("board")).toEqual({ to: "sky", focus: "" });
    expect(legacyFlightsRoute("sky")).toEqual({ to: "sky", focus: "" });
    expect(legacyFlightsRoute("sky/fix-login/x")).toEqual({ to: "sky", focus: "fix-login/x" });
    expect(legacyFlightsRoute("flyways")).toEqual({ to: "flyways", plan: "" });
    expect(legacyFlightsRoute("flyways/p_1")).toEqual({ to: "flyways", plan: "p_1" });
    expect(legacyFlightsRoute("plans/builtin:ticket")).toEqual({ to: "flyways", plan: "builtin:ticket" });
    expect(legacyFlightsRoute("sources")).toEqual({ to: "intake", target: null });
    expect(legacyFlightsRoute("sources/tickets:jira")).toEqual({ to: "intake", target: "tickets:jira" });
    expect(legacyFlightsRoute("checklists")).toEqual({ to: "verify", target: null });
    expect(legacyFlightsRoute("checklists/feature/x")).toEqual({ to: "verify", target: "feature/x" });
    expect(legacyFlightsRoute("schedule")).toEqual({ to: "schedule", target: null });
    expect(legacyFlightsRoute("schedule/new")).toEqual({ to: "schedule", target: "new" });
    expect(legacyFlightsRoute("nope/x")).toEqual({ to: "sky", focus: "" });
  });
});
