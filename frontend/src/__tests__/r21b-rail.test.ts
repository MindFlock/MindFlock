/** Round 21b (JUDGE-R21-3 #4 and a pass-1 leftover): the rail says Halted.
 *
 *  - A session whose ROUTE is halted showed "Idle" or "Pushed" on its rail
 *    row, while a Wait for me session showed its amber chip. Now its chip
 *    says "Halted" in red (the broken tone), with the full reason in the
 *    tooltip, at the same rank as Wait for me (ahead of Working).
 *  - The Sky's Needs you card (and the bell's halted row) said the stop's
 *    name twice: "CI checks — CI checks failed: …". Now "CI checks —
 *    Failed: …", the words the Sky's halted stop uses. */

import { describe, expect, it, vi } from "vitest";
import type { Instance } from "../api/types";
import type { Flight, FlightRow } from "../lib/flights";
import { code, has } from "./sourceText";

vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const { deriveSessionState, haltedWhy, haltedHint, needsYouAction, notifyRuleFor, REASON_LABEL } = await import("../lib/sessionState");
const { chipState } = await import("../lib/stage");
const { needsYouRows, needsYouBadge } = await import("../lib/needsYou");
const { stopWhy } = await import("../components/flights/SkyChain");

const NOW = Date.now() / 1000;
const inst = (title: string, extra: Partial<Instance> = {}) =>
  ({ title, status: "running", activity: "idle", stage: "agent", branch: "", activity_since: NOW - 60, ...extra }) as Instance;

type Row = FlightRow & { reason?: string };
const haltedRow = (extra: Partial<Row> = {}): Row => ({
  id: "f_halt",
  plan_name: "Ticket to merge",
  cursor: 2,
  total: 4,
  state: "halted",
  reason: "CI checks failed: 2 of 14 checks red (test-unit, lint)",
  step: { id: "s3", kind: "checks", title: "CI checks", state: "failed", actor: "robot" },
  ...extra,
});

describe("a halted route's rail chip", () => {
  it("says Halted in red on an idle, pushed session — not Idle or Pushed", () => {
    for (const stage of ["agent", "pushed", "committed"]) {
      const i = inst("h", { stage, flight: haltedRow() as FlightRow });
      const ss = deriveSessionState(i);
      expect(ss).toMatchObject({ state: "needs_you", reason: "flight_halted", label: "Halted", tone: "broken", inBell: true });
      const chip = chipState(i, ss);
      expect(chip.label).toBe("Halted");
      expect(chip.cls).toContain("tone-broken");
    }
    expect(REASON_LABEL.flight_halted).toBe("Halted");
  });

  it("keeps the full reason in the tooltip, the stop's name said once", () => {
    const ss = deriveSessionState(inst("h", { flight: haltedRow() as FlightRow }));
    expect(ss.hint).toContain("Halted at CI checks — Failed: 2 of 14 checks red (test-unit, lint)");
    expect(ss.hint).toContain("Ticket to merge");
    expect(ss.hint).not.toMatch(/CI checks.*CI checks/);
    expect(chipState(inst("h", { flight: haltedRow() as FlightRow })).title).toBe(ss.hint);
    // A reason that doesn't start with the stop's name is kept whole.
    expect(haltedHint({ plan_name: "P", reason: "3 tests failed", step: { title: "Run the tests" } })).toMatch(/^Halted at Run the tests — 3 tests failed\n/);
    // No reason at all: just where.
    expect(haltedHint({ plan_name: "P", step: { title: "Push" } })).toMatch(/^Halted at Push\n/);
  });

  it("has the same rank as Wait for me: ahead of Working, behind a Question", () => {
    expect(deriveSessionState(inst("h", { activity: "working", flight: haltedRow() as FlightRow })).label).toBe("Halted");
    expect(
      deriveSessionState(inst("h", { activity: "clarify", flight: haltedRow() as FlightRow })).reason
    ).toBe("question");
    expect(
      deriveSessionState(inst("h", { asking: { kind: "question", text: "?", ts: NOW } as Instance["asking"], flight: haltedRow() as FlightRow }))
        .reason
    ).toBe("question");
  });

  it("only for the session's own route (a worker's branch-only dto has no id), and only while halted", () => {
    expect(deriveSessionState(inst("h", { flight: haltedRow({ id: "" }) as FlightRow })).label).toBe("Idle");
    expect(deriveSessionState(inst("h", { flight: haltedRow({ state: "running" }) as FlightRow })).label).toBe("Idle");
    expect(deriveSessionState(inst("h", { flight: haltedRow({ state: "done" }) as FlightRow })).label).toBe("Idle");
  });

  it("is a door to the bell (Retry / Skip there), rings as a route's finish", () => {
    const ss = deriveSessionState(inst("h", { flight: haltedRow() as FlightRow }));
    expect(needsYouAction(ss)).toEqual({ act: "bell", aria: "Halted — Retry or Skip in the bell" });
    expect(notifyRuleFor(ss)).toBe("flight_finished");
  });

  it("does not add a second Needs-you row: the halted route's row stays the one", () => {
    const f = {
      id: "f_halt", plan_id: "adhoc", plan_name: "Ticket to merge", session: "h", state: "halted", cursor: 2,
      steps: [
        { id: "s1", kind: "agent", title: "Agent works the ticket", config: {} },
        { id: "s2", kind: "ship", title: "Open PR", config: {} },
        { id: "s3", kind: "checks", title: "CI checks", config: {} },
      ],
      step_states: {}, reason: "CI checks failed: 2 of 14 checks red", history: [],
    } as unknown as Flight;
    const rows = needsYouRows({ instances: [inst("h", { stage: "pushed", flight: haltedRow() as FlightRow })], waiting: [], flights: [f] });
    expect(rows.map((r) => [r.title, r.kind])).toEqual([["h", "halted"]]);
    expect(needsYouBadge(rows)).toEqual({ count: 1, failing: true });
  });
});

describe("the halted card says the stop's name once", () => {
  it("haltedWhy drops the stop's name the way the Sky's halted stop does", () => {
    const reason = "CI checks failed: 2 of 14 checks red";
    expect(haltedWhy("CI checks", reason)).toBe("Failed: 2 of 14 checks red");
    expect(haltedWhy("CI checks", reason)).toBe(stopWhy({ state: "halted", title: "CI checks", reason, step: undefined } as never));
    expect(haltedWhy("ci checks", reason)).toBe("Failed: 2 of 14 checks red");
    expect(haltedWhy("Run the tests", "3 tests failed")).toBe("3 tests failed");
    expect(haltedWhy("CI checks", "CI checks")).toBe("CI checks");
    expect(haltedWhy("", reason)).toBe(reason);
    expect(haltedWhy("CI checks", "")).toBe("");
  });

  it("the Sky's Needs you card and the bell's halted row read it", () => {
    const sky = code("components/flights/SkyDrawer.tsx");
    expect(has(sky, 'haltedWhy(step?.title, flight.reason)')).toBe(true);
    expect(sky).not.toContain('" — " + flight.reason');
    const bell = code("components/outbox/WaitingRow.tsx");
    expect(has(bell, "haltedWhy(at, w.reason)")).toBe(true);
    expect(has(bell, "why !== chip")).toBe(true);
  });
});
