/** Routes on rails: a stop parked on you that asks more than a Done
 * (lib/stopAsks) —
 *  - a Review out of fix rounds with findings still open (`detail.ask`
 *    "unresolved"): Give it another round (the step retry) primary, Carry on
 *    (Done) second, and the open findings;
 *  - the one perch asking for the checks Verify stops left for the end
 *    (`detail.ask` "owed"): Done — they all work, and per Verify stop "This
 *    one didn't work" (the plan's confirm, verified false);
 *  - while a route still flies with checks owed: the quiet "N checks owed to
 *    you at the end", never worded as waiting on you.
 * Every surface reads the same derivation: the stop's box, its dialog, its
 * menu, the free run's dialog, the bell's row, the Needs-you list. */

/// <reference types="vite/client" />
import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type { Instance, OutboxWaiting } from "../api/types";
import type { Flight, FlightRow, FlightStep, StepRunState } from "../lib/flights";
import type { DiagramNode } from "../lib/flightDiagram";

const calls: Array<[string, unknown]> = [];
vi.mock("../api/client", () => ({
  api: vi.fn(async (path: string, opts?: unknown) => {
    calls.push([path, opts]);
    return {};
  }),
  ApiError: class extends Error {},
  instApi: vi.fn(),
}));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));
vi.mock("../lib/errorPop", () => ({ errorPop: vi.fn() }));

(globalThis as Record<string, unknown>).document ??= {
  hidden: false,
  addEventListener: () => {},
  removeEventListener: () => {},
  getElementById: () => null,
  querySelector: () => null,
  documentElement: { classList: { contains: () => false, add: () => {}, remove: () => {}, toggle: () => {} } },
};

const { QueryClient, QueryClientProvider } = await import("@tanstack/react-query");
const A = await import("../lib/stopAsks");
const { flightStops } = await import("../lib/flightDiagram");
const { menuItemsFor } = await import("../lib/stopEditing");
const { hereStop } = await import("../components/flights/useSessionLine");
const { freeStopRemedies } = await import("../components/flights/useGroundedLine");
const { YouStepActions, liveBoxParts } = await import("../components/flights/FlightStrip");
const { stopWhy, stopTileBoxes } = await import("../components/flights/SkyChain");
const { OwedNote } = await import("../components/flights/PerchAsks");
const SD = await import("../components/flights/StopDialog");
const { WaitingRow } = await import("../components/outbox/WaitingRow");
const { needsYouRows } = await import("../lib/needsYou");
const { verifySentence } = await import("../lib/verifyStop");

const step = (id: string, kind: string, title: string, config: Record<string, unknown> = {}): FlightStep => ({ id, kind, title, config, on_fail: "agent" }) as FlightStep;
const rec = (state: string, note = "", detail: Record<string, unknown> = {}): StepRunState => ({ state, since: 0, note, detail, rounds: 3 }) as unknown as StepRunState;

const FINDINGS = "1. src/a.ts:12 — the null check is inverted\n2. src/b.ts:40 — the token is logged";
const REVIEW = step("s2", "review", "Second opinion");
const unresolved = (over: Partial<Flight> = {}): Flight =>
  ({
    id: "f_r",
    plan_name: "Ticket to merge",
    session: "lead",
    steps: [step("s1", "agent", "Work"), REVIEW, step("s3", "ship", "Push")],
    cursor: 1,
    state: "waiting_you",
    reason: "",
    history: [],
    step_states: {
      s1: rec("done"),
      s2: rec("waiting_you", "2 findings still open after 3 rounds", { ask: "unresolved", findings: FINDINGS, receipt: "2 findings left open" }),
      s3: rec("pending"),
    },
    ...over,
  }) as unknown as Flight;

const OWED = [
  { step: "v1", title: "Verify on staging", plan: "lead", items: ["Log in as the QA user", "Export a CSV"] },
  { step: "v2", title: "Verify the docs", plan: "lead-docs", items: ["The new page renders"] },
];
const PERCH = step("s4", "you", "Your checks", { text: "Before this route finishes: 3 checks for you", notify: true });
const owedPerch = (over: Partial<Flight> = {}): Flight =>
  ({
    id: "f_o",
    plan_name: "Ticket to merge",
    session: "lead",
    steps: [step("s1", "agent", "Work"), step("v1", "verify", "Verify on staging"), step("v2", "verify", "Verify the docs"), PERCH],
    cursor: 3,
    state: "waiting_you",
    reason: "",
    history: [],
    owed: OWED.map((g) => ({ ...g, since: 1, run_at: 1, perch: "s4" })),
    owed_perch: "s4",
    step_states: {
      s1: rec("done"),
      v1: rec("done", "2 checks for you, asked at the end", { owes: 2 }),
      v2: rec("done", "1 check for you, asked at the end", { owes: 1 }),
      s4: rec("waiting_you", "Before this route finishes: 3 checks for you", { items: [], ask: "owed", owed: OWED }),
    },
    ...over,
  }) as unknown as Flight;

const nodeAt = (f: Flight): DiagramNode => flightStops(f).find((n) => n.index === f.cursor)!;

function withClient(el: ReactElement, prime: (c: InstanceType<typeof QueryClient>) => void = () => {}) {
  const c = new QueryClient({ defaultOptions: { queries: { retry: false, enabled: false } } });
  prime(c);
  return renderToStaticMarkup(createElement(QueryClientProvider, { client: c }, el));
}
const buttons = (html: string) => [...html.matchAll(/<button[^>]*>([^<]+)<\/button>/g)].map((m) => m[1]);

describe("which ask a parked stop has", () => {
  it("unresolved only on a Review, owed only on a Wait for me, nothing on a failure handed to you", () => {
    expect(A.flightAsk(unresolved())).toBe("unresolved");
    expect(A.flightAsk(owedPerch())).toBe("owed");
    expect(A.stopAskOf(step("x", "verify", "V"), rec("waiting_you", "", { ask: "unresolved" }))).toBe("");
    expect(A.stopAskOf(step("x", "agent", "A"), rec("waiting_you", "", { ask: "owed" }))).toBe("");
    expect(A.stopAskOf(REVIEW, rec("waiting_you", "", { ask: "unresolved", failed_reason: "x" }))).toBe("");
    expect(A.stopAskOf(REVIEW, rec("running", "", { ask: "unresolved" }))).toBe("");
    expect(A.flightAsk(unresolved({ state: "running" }))).toBe("");
    expect(A.flightAsk(null)).toBe("");
  });

  it("the words: Give it another round first, then Carry on; the owed perch's one Done", () => {
    expect(A.askOffers("unresolved")!.map((o) => [o.key, o.label, o.primary])).toEqual([
      ["retry", "Give it another round", true],
      ["done", "Carry on", false],
    ]);
    expect(A.askOffers("owed")!.map((o) => [o.key, o.label, o.primary])).toEqual([["done", "Done — they all work", true]]);
    expect(A.askOffers("")).toBeNull();
    expect(A.OWED_FAILED).toBe("This one didn't work");
  });

  it("the diagram node carries the ask and the server's sentence; the tile says it and makes room", () => {
    const n = nodeAt(unresolved());
    expect(n).toMatchObject({ state: "perched", ask: "unresolved", askNote: "2 findings still open after 3 rounds" });
    expect(stopWhy(n)).toBe("2 findings still open after 3 rounds");
    const plain = nodeAt(owedPerch({ step_states: { ...owedPerch().step_states, s4: rec("waiting_you", "Approve") } }));
    expect(plain.ask).toBeUndefined();
    const box = stopTileBoxes({ nodes: [n] }, unresolved())[n.id];
    expect(box.w).toBe(A.ASK_TILE_W);
    expect(A.askRoom("unresolved", { findings: FINDINGS })).toBeGreaterThan(30);
    expect(A.askRoom("unresolved", { findings: Array(40).fill("x").join("\n") })).toBe(166);
    expect(A.askRoom("owed", { owed: OWED })).toBe(46 + 2 * 22 + 46 + 22);
  });
});

describe("a Review out of fix rounds with findings still open", () => {
  it("its box: the findings, Give it another round (retry) primary and Carry on — no lone Done", () => {
    const html = withClient(createElement(YouStepActions, { flight: unresolved() }));
    expect(html).toContain('class="fl-findings"');
    expect(html).toContain("the null check is inverted");
    expect(buttons(html)).toEqual(["Give it another round", "Carry on"]);
    expect(html).toMatch(/class="btn-primary fl-btn fl-retry"[^>]*>Give it another round/);
    expect(html).toContain("2 findings still open after 3 rounds");
    // The pane's one-line strip: the buttons only (the findings in the tooltip).
    const compact = withClient(createElement(YouStepActions, { flight: unresolved(), compact: true }));
    expect(compact).not.toContain('class="fl-findings"');
    expect(buttons(compact)).toEqual(["Give it another round", "Carry on"]);
  });

  it("its box is sized for the findings", () => {
    const f = unresolved();
    const n = nodeAt(f);
    const parts = liveBoxParts(f, true, () => {});
    const plain = liveBoxParts(f, true, () => {}).sizeOf({ ...n, ask: undefined }, { w: 156, h: 60 });
    expect(parts.sizeOf(n, { w: 156, h: 60 }).h).toBeGreaterThan(plain.h);
  });

  it("its dialog: Retry worded Give it another round and primary, Done worded Carry on, the findings listed", () => {
    const f = unresolved();
    const h = hereStop(f, nodeAt(f));
    expect(h).toMatchObject({ ask: "unresolved", retry: true, done: "done", primary: "retry", findings: FINDINGS, why: "2 findings still open after 3 rounds" });
    expect(h.words.retry?.[0]).toBe("Give it another round");
    expect(h.words.done?.[0]).toBe("Carry on");
    const r = SD.remedies({ onRetry: () => {}, onDone: () => {}, onSkip: () => {}, primary: h.primary, retryLabel: h.words.retry?.[0], doneLabel: h.words.done?.[0] });
    expect(r.map((x) => [x.label, x.primary])).toEqual([
      ["Give it another round", true],
      ["Carry on", false],
      ["Skip", false],
    ]);
  });

  it("its menu: Give it another round, then Carry on (retry / done keys the hosts already handle)", () => {
    const items = menuItemsFor(nodeAt(unresolved()), { can: {}, live: true, here: true });
    const keys = items.filter((i) => i.key === "retry" || i.key === "done").map((i) => [i.key, i.label]);
    expect(keys).toEqual([
      ["retry", "Give it another round"],
      ["done", "Carry on"],
    ]);
  });

  it("a route running on its own: the same two in its dialog", () => {
    const f = unresolved();
    const r = freeStopRemedies(f, nodeAt(f));
    expect(r).toMatchObject({ retry: true, done: "done", ask: "unresolved" });
  });

  it("the bell's row: Give it another round and Carry on, the findings in its drawer", () => {
    const f = unresolved();
    const w: OutboxWaiting = { key: "flight:f_r", title: "lead", kind: "flight_you", reason: "Needs you: Second opinion", text: "Ticket to merge", actions: ["done", "skip", "open"] } as OutboxWaiting;
    const row = { title: "lead", flight: { id: "f_r", plan_name: "Ticket to merge", cursor: 1, total: 3, state: "waiting_you", step: { id: "s2", kind: "review", title: "Second opinion", state: "waiting_you", actor: "robot" } } } as unknown as Instance;
    const html = withClient(createElement(WaitingRow, { w, row, shown: (t: string) => t, group: "" }), (c) => c.setQueryData(["flights"], [f]));
    // The session's name, then the two answers (no Skip, no lone Done), then Open ↗.
    expect(buttons(html)).toEqual(["lead", "Give it another round", "Carry on", "Open ↗"]);
    expect(html).toContain("the token is logged");
  });

  it("the Needs-you list keeps it (one wait_for_me row for the route)", () => {
    const f = unresolved();
    const inst = { title: "lead", status: "running", activity: "idle", stage: "agent", branch: "", flight: { id: f.id, plan_name: f.plan_name, cursor: 1, total: 3, state: "waiting_you", step: { id: "s2", kind: "review", title: "Second opinion", state: "waiting_you", actor: "robot" } } } as unknown as Instance;
    const rows = needsYouRows({ instances: [inst], waiting: [], flights: [f] });
    expect(rows.map((r) => [r.kind, r.flight?.id])).toEqual([["wait_for_me", "f_r"]]);
  });
});

describe("the one perch asking for the checks owed at the end", () => {
  it("its box: the server's sentence, each Verify stop's items, This one didn't work per group, Done — they all work", () => {
    const html = withClient(createElement(YouStepActions, { flight: owedPerch() }));
    expect(html).toContain("Before this route finishes: 3 checks for you");
    expect(html).toContain("Verify on staging");
    expect(html).toContain("Log in as the QA user");
    expect(html).toContain("The new page renders");
    // The Verify confirm's own step list.
    expect(html).toContain('class="vc-steps"');
    const b = buttons(html);
    expect(b.filter((x) => x === "This one didn&#x27;t work")).toHaveLength(2);
    expect(b).toContain("Done — they all work");
    expect(b).toContain("Skip");
    expect(b).not.toContain("Done");
  });

  it("a person's own Wait for me carrying them: its own text stays, the owed sentence leads", () => {
    const own = step("s4", "you", "Approve the merge", { text: "Approve the merge", items: ["Read the diff"] });
    const f = owedPerch({
      steps: [...owedPerch().steps.slice(0, 3), own],
      owed_perch: "",
      step_states: { ...owedPerch().step_states, s4: rec("waiting_you", "Approve the merge · before this route finishes: 3 checks for you", { items: ["Read the diff"], ask: "owed", owed: OWED }) },
    });
    const html = withClient(createElement(YouStepActions, { flight: f }));
    expect(html).toContain("Approve the merge · before this route finishes: 3 checks for you");
    expect(html).toContain("Read the diff");
  });

  it("its dialog: Done — they all work primary, the groups under the stop's own items", () => {
    const f = owedPerch();
    const h = hereStop(f, nodeAt(f));
    expect(h).toMatchObject({ ask: "owed", done: "done", primary: "done", retry: false });
    expect(h.owed.map((g) => g.title)).toEqual(["Verify on staging", "Verify the docs"]);
    expect(h.words.done?.[0]).toBe("Done — they all work");
  });

  it("its menu: Done — they all work", () => {
    const items = menuItemsFor(nodeAt(owedPerch()), { can: {}, live: true, here: true });
    expect(items.find((i) => i.key === "done")?.label).toBe("Done — they all work");
  });

  it("the Needs-you list keeps it under Wait for me", () => {
    const f = owedPerch();
    const inst = { title: "lead", status: "running", activity: "idle", stage: "agent", branch: "", flight: { id: f.id, plan_name: f.plan_name, cursor: 3, total: 4, state: "waiting_you", step: { id: "s4", kind: "you", title: "Your checks", state: "waiting_you", actor: "you" } } } as unknown as Instance;
    const rows = needsYouRows({ instances: [inst], waiting: [], flights: [f] });
    expect(rows.map((r) => [r.kind, r.waiting?.kind])).toEqual([["wait_for_me", "flight_you"]]);
  });
});

describe("checks owed while the route still flies", () => {
  it("owedGroups reads defensively; owedCount counts items (a group with none counts one)", () => {
    expect(A.owedGroups([{ step: "v1", items: ["a", " ", 3] }, { title: "no id" }, null, "x"])).toEqual([{ step: "v1", title: "Verify", plan: "", items: ["a", "3"] }]);
    expect(A.owedGroups(undefined)).toEqual([]);
    expect(A.owedCount(A.owedGroups(OWED))).toBe(3);
    expect(A.owedCount([{ step: "v", title: "V", plan: "", items: [] }])).toBe(1);
  });

  it("the quiet line: the server's owed_note, else worked out; never once the perch is up or the route is over", () => {
    const flying = { state: "running" as const, owed: OWED, owed_note: "3 checks owed to you at the end" };
    expect(A.owedNoteOf(flying)).toBe("3 checks owed to you at the end");
    expect(A.owedNoteOf({ state: "running", owed: OWED.slice(1) })).toBe("1 check owed to you at the end");
    expect(A.owedNoteOf({ ...flying, state: "waiting_you" })).toBe("");
    expect(A.owedNoteOf({ ...flying, state: "done" })).toBe("");
    expect(A.owedNoteOf({ state: "running", owed: [] })).toBe("");
    // The session row's flight carries the same two fields.
    const row: Pick<FlightRow, "state" | "owed" | "owed_note"> = { state: "running", owed: OWED, owed_note: "3 checks owed to you at the end" };
    expect(A.owedNoteOf(row)).toBe("3 checks owed to you at the end");
  });

  it("its tooltip groups the items by Verify stop title and never says the route waits on you", () => {
    const tip = A.owedTip(A.owedGroups(OWED));
    expect(tip.split("\n")).toEqual([
      "Verify on staging:",
      "  • Log in as the QA user",
      "  • Export a CSV",
      "Verify the docs:",
      "  • The new page renders",
      "The route carries on; it asks you once, before it finishes.",
    ]);
    expect(tip.toLowerCase()).not.toContain("needs you");
  });

  it("OwedNote draws a muted link (not the amber needs-you look); nothing when there is no note", () => {
    const html = renderToStaticMarkup(createElement(OwedNote, { note: "3 checks owed to you at the end", groups: A.owedGroups(OWED) }));
    expect(html).toContain('class="linklike fl-owed-note"');
    expect(html).toContain("3 checks owed to you at the end");
    expect(html).toContain("Verify on staging:");
    expect(html).not.toContain("fl-needs");
    expect(renderToStaticMarkup(createElement(OwedNote, { note: "", groups: [] }))).toBe("");
  });

  it("a Verify stop that asks at the end says so in its sentence", () => {
    expect(verifySentence({ ask: "end" }, "halt")).toContain("the route carries on and asks you for the steps only a person can do at the end");
    expect(verifySentence({ ask: "end", who: "you" }, "halt")).toContain("asks for your sign-off at the end");
    expect(verifySentence({ ask: "here" }, "halt")).toContain("you confirm the steps only a person can do");
  });
});
