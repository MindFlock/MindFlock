/** Stability sweep: rows in the one Needs-you list (lib/needsYou
 * `needsYouRows`) about something the person cannot see or act on. Each test
 * builds the stale situation and asserts the list no longer has the row. */

import { describe, expect, it, vi } from "vitest";
import type { Instance } from "../api/types";

vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const { needsYouRows } = await import("../lib/needsYou");

const NOW = Date.now() / 1000;
const inst = (title: string, extra: Record<string, unknown> = {}) =>
  ({ title, status: "running", activity: "idle", stage: "agent", branch: "", activity_since: NOW - 60, ...extra }) as unknown as Instance;
const titles = (instances: Instance[]) => needsYouRows({ instances, waiting: [], flights: [], testPlans: [] }).map((r) => r.title);

// A verify checker (`hidden_by: "verify"`) is kept off the rail, the Sky and
// the calm screen's session count, and its agent never pops the ask pop-up:
// with only checkers left the app reads as "no sessions". Its agent's own
// states (gone offline, idle on files the walk left behind) then made "1 needs
// you" about a window that is nowhere to be seen.
describe("a verify checker's own agent is not something waiting on you", () => {
  const checker = (extra: Record<string, unknown> = {}) => inst("verify-sc-1-abc1234", { hidden_by: "verify", ...extra });

  it("not when its agent went offline", () => {
    expect(titles([checker({ activity: "offline" })])).toEqual([]);
  });

  it("not when it sits idle on uncommitted files", () => {
    const idle = checker({ activity_since: NOW - 7200, diff_stat: { uncommitted: { additions: 3, deletions: 0 } } });
    expect(titles([idle])).toEqual([]);
  });

  it("not for a question its agent asked", () => {
    expect(titles([checker({ activity: "clarify" })])).toEqual([]);
  });

  it("still when its verify waits on you", () => {
    const v = { plan: "sc-1", state: "blocked", detail: "needs you: run aws sso login", steps: [] };
    expect(titles([checker({ verify: v })])).toEqual(["verify-sc-1-abc1234"]);
  });

  it("an ordinary session in the same state still is", () => {
    expect(titles([inst("sc-1", { activity: "offline" })])).toEqual(["sc-1"]);
    expect(titles([inst("verify-foo", { hidden_by: "", activity: "offline" })])).toEqual(["verify-foo"]);
  });
});

// A route waiting on you whose session is no longer listed (closed by a door
// that did not cancel it yet — flights.prune gives it 30 minutes): the
// server's own flight_you row still names the session, and the full flights
// made a second row for the same route, keyed by the flight. One route, one
// row — the one with no dead session's name on it.
describe("a route whose session is gone is one row", () => {
  const f = {
    id: "f_gone",
    plan_id: "adhoc",
    plan_name: "Ticket to PR",
    steps: [{ id: "s1", kind: "you", title: "Wait for me: Try it", config: {} }],
    subject: {},
    session: "gone",
    repo_root: "/r",
    branch: "b",
    state: "waiting_you",
    cursor: 0,
    step_states: {},
    reason: "",
    history: [],
    started: NOW - 600,
    updated: NOW - 60,
    finished: 0,
    by: "user",
  };
  const server = {
    key: "flight::f_gone::s1",
    title: "gone",
    run: null,
    kind: "flight_you",
    reason: "Try it",
    since: NOW - 60,
    preview: null,
    actions: ["done", "skip", "open"],
    flight: { id: "f_gone", plan_name: "Ticket to PR", step: "s1", step_title: "Wait for me: Try it", kind: "you" },
  };

  it("not two", () => {
    const rows = needsYouRows({
      instances: [inst("other")],
      waiting: [server] as never,
      flights: [f] as never,
      testPlans: [],
    }).filter((r) => r.kind === "wait_for_me");
    expect(rows.length).toBe(1);
    expect(rows[0].title).toBe("");
  });

  it("a live session keeps its own row", () => {
    const rows = needsYouRows({
      instances: [inst("gone")],
      waiting: [server] as never,
      flights: [f] as never,
      testPlans: [],
    }).filter((r) => r.kind === "wait_for_me");
    expect(rows.map((r) => r.title)).toEqual(["gone"]);
  });
});
