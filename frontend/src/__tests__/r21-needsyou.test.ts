/** Round 21 (JUDGE-R21-2 #1): ONE Needs-you list. The bell, the Sky's Needs
 * you strip/panel and the top bar's Sky badge all read lib/needsYou's
 * `needsYouRows` (through `useNeedsYou`), so a session that asks, a route at a
 * Wait for me stop and a halted route show up in every one of them, in the
 * same order, under one count. */

import { describe, expect, it, vi } from "vitest";
import type { Instance, OutboxWaiting, TestPlan } from "../api/types";
import type { Flight, FlightRow } from "../lib/flights";
import { code, css, fn, has } from "./sourceText";

vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const { needsYouRows, needsYouBadge, needsYouTitles, withFlightRows, NEEDS_YOU_KINDS } = await import("../lib/needsYou");

const NOW = Date.now() / 1000;
const inst = (title: string, extra: Partial<Instance> = {}) =>
  ({ title, status: "running", activity: "idle", stage: "agent", branch: "", activity_since: NOW - 60, ...extra }) as Instance;

const steps = [
  { id: "s1", kind: "agent", title: "Agent works the ticket", config: {} },
  { id: "s2", kind: "checks", title: "CI checks", config: {} },
  { id: "s3", kind: "you", title: "Wait for me: Try it on staging", config: {} },
];
function flight(id: string, session: string, state: Flight["state"], cursor: number, reason = ""): Flight {
  return {
    id,
    plan_id: "adhoc",
    plan_name: "Ticket to PR",
    steps,
    subject: {},
    session,
    repo_root: "/r",
    branch: "b",
    state,
    cursor,
    step_states: {},
    reason,
    history: [],
    started: NOW - 600,
    updated: NOW - 60,
    finished: 0,
    by: "user",
  } as unknown as Flight;
}
function row(f: Flight): FlightRow {
  const s = f.steps[f.cursor];
  return {
    id: f.id,
    plan_name: f.plan_name,
    cursor: f.cursor,
    total: f.steps.length,
    state: f.state,
    step: { id: s.id, kind: s.kind, title: s.title, state: "pending", actor: s.kind === "you" ? "you" : "robot" },
  };
}

// The judges' fixture: one session asking, one route at a Wait for me stop,
// one route halted at CI checks, one session just working.
const fAsk = flight("f_ask", "ask", "running", 0);
const fYou = flight("f_you", "you", "waiting_you", 2);
const fHalt = flight("f_halt", "halt", "halted", 1, "CI checks failed: 2 of 14 checks red");
const fWork = flight("f_work", "work", "running", 0);
const FLIGHTS = [fAsk, fYou, fHalt, fWork];
const INSTANCES = [
  inst("work", { activity: "working", flight: row(fWork) }),
  inst("halt", { stage: "pushed", flight: row(fHalt) }),
  inst("you", { flight: row(fYou) }),
  inst("ask", { asking: { kind: "question", text: "Redis or in-memory?", ts: NOW } as Instance["asking"], flight: row(fAsk) }),
];

describe("the one Needs-you list", () => {
  it("asking + Wait for me + halted, in that order; the working session is not in it", () => {
    const rows = needsYouRows({ instances: INSTANCES, waiting: [], flights: FLIGHTS });
    expect(rows.map((r) => [r.title, r.kind])).toEqual([
      ["ask", "asking"],
      ["you", "wait_for_me"],
      ["halt", "halted"],
    ]);
    // The asking row carries its question (the bell's and the panel's strip).
    expect(rows[0].attn?.question).toBe("Redis or in-memory?");
    // The routes carry their full flight (the panel's stop block) …
    expect(rows[1].flight?.id).toBe("f_you");
    expect(rows[2].flight?.id).toBe("f_halt");
    // … and the halted one says why, with Retry / Skip on offer.
    expect(rows[2].waiting).toMatchObject({ kind: "flight_halted", reason: "CI checks failed: 2 of 14 checks red", actions: ["retry", "skip", "open"] });
    // A halted route outranks its session's "ready for PR".
    expect(rows.filter((r) => r.title === "halt")).toHaveLength(1);
  });

  it("is the same list (and count) whatever surface asks", () => {
    const a = needsYouRows({ instances: INSTANCES, waiting: [], flights: FLIGHTS });
    const b = needsYouRows({ instances: INSTANCES, waiting: [], flights: FLIGHTS });
    expect(a.map((r) => r.key)).toEqual(b.map((r) => r.key));
    expect(needsYouBadge(a)).toEqual({ count: 3, failing: true });
    expect(needsYouTitles(INSTANCES, [], FLIGHTS)).toEqual(["ask", "you", "halt"]);
  });

  it("nothing needs you → an empty list and no badge", () => {
    const rows = needsYouRows({ instances: [inst("work", { activity: "working", flight: row(fWork) })], waiting: [], flights: [fWork] });
    expect(rows).toEqual([]);
    expect(needsYouBadge(rows)).toEqual({ count: 0, failing: false });
  });

  it("the session rows alone are enough (an older server, flights not loaded yet)", () => {
    const rows = needsYouRows({ instances: INSTANCES, waiting: undefined, flights: null });
    expect(rows.map((r) => r.kind)).toEqual(["asking", "wait_for_me", "halted"]);
    expect(rows[2].waiting).toMatchObject({ kind: "flight_halted", flight: { id: "f_halt", step_id: "s2", step_title: "CI checks" } });
  });

  it("a route the session rows don't show yet, or one with no session, still counts", () => {
    const loose = flight("f_loose", "", "halted", 1, "lint failed");
    const rows = needsYouRows({ instances: [inst("you")], waiting: [], flights: [fYou, loose] });
    expect(rows.map((r) => [r.key, r.title, r.kind])).toEqual([
      ["you", "you", "wait_for_me"],
      ["flight:f_loose", "", "halted"],
    ]);
    // No session to jump to: Ctrl+K N skips it.
    expect(needsYouTitles([inst("you")], [], [fYou, loose])).toEqual(["you"]);
  });

  it("the server's own Wait-for-me row and the session row are one row", () => {
    const listed: OutboxWaiting[] = [{ key: "k", title: "you", kind: "flight_you", reason: "Needs you: Try it on staging" }];
    const merged = withFlightRows(listed, INSTANCES, FLIGHTS);
    expect(merged.filter((w) => w.title === "you")).toHaveLength(1);
    const rows = needsYouRows({ instances: INSTANCES, waiting: listed, flights: FLIGHTS });
    expect(rows.find((r) => r.title === "you")?.flight?.id).toBe("f_you");
  });

  it("approvals come after a question, behaviors after checks", () => {
    const approve: OutboxWaiting = { key: "a", title: "work", kind: "approve", step: "commit" };
    const plan = { id: "p1", title: "Login", state: "ready", steps: [{ id: "1", result: "fail" }], verdict: "fail" } as unknown as TestPlan;
    const rows = needsYouRows({ instances: INSTANCES, waiting: [approve], flights: FLIGHTS, testPlans: [plan] });
    const kinds = rows.map((r) => r.kind);
    expect(kinds.slice(0, 4)).toEqual(["asking", "approval", "wait_for_me", "halted"]);
    // Every row's kind is one the phone page names too.
    for (const k of kinds) expect(NEEDS_YOU_KINDS).toContain(k);
  });
});

describe("a session's own verify is one bell row", () => {
  it("drops the Behaviors row for a plan its session row already shows", () => {
    const plan = { id: "p1", title: "Login", state: "due", steps: [{ id: "1", text: "Log in" }] } as unknown as TestPlan;
    const base = needsYouRows({ instances: INSTANCES, waiting: [], flights: FLIGHTS, testPlans: [plan] });
    expect(base.some((r) => r.kind === "behaviors")).toBe(true);
    const withVerify = [...INSTANCES, { ...inst("v1"), verify: { state: "failed", plan: "p1" } } as unknown as Instance];
    const rows = needsYouRows({ instances: withVerify, waiting: [], flights: FLIGHTS, testPlans: [plan] });
    expect(rows.some((r) => r.kind === "behaviors")).toBe(false);
  });
});

describe("every surface reads it", () => {
  it("the bell's list and badge", () => {
    const bell = fn("components/NotificationsBell.tsx", "NotificationsBell");
    expect(has(bell, "const attn = useNeedsYou();")).toBe(true);
    expect(has(bell, '{attn.length > 99 ? "99+" : String(attn.length)}')).toBe(true);
  });

  it("the Sky's Needs you strip and panel", () => {
    const drawer = fn("components/flights/SkyDrawer.tsx", "SkyDrawer");
    expect(has(drawer, "const needs = useNeedsYou();")).toBe(true);
    expect(has(drawer, "const count = needs.length;")).toBe(true);
    expect(code("components/flights/SkyDrawer.tsx")).not.toContain("needsYouFlights");
  });

  it("the top bar's Sky badge", () => {
    expect(code("components/TopBar.tsx")).toContain("const perch = needsYouBadge(useNeedsYou());");
  });

  it("a halted route has Retry / Skip in the bell", () => {
    const w = code("components/outbox/WaitingRow.tsx");
    expect(w).toContain('if (w.kind === "flight_halted") return <FlightHaltedRow w={w} row={row} shown={shown} />;');
    expect(has(w, "stepRetry(fl.id, fl.step.id)")).toBe(true);
  });

  it("an answer strip with nothing to say draws no empty box in the bell", () => {
    expect(css("components/NotificationsBell.css")).toContain(
      ".notif-attn .flock-answer.is-unparsed:not(:has(.fa-q, .fa-opt, .fa-note, .fa-hint, .fa-state))"
    );
  });
});
