/** Ship & split (lib/laneActions): the lane vocabulary, reading a row's
 * lane, the pane menu's model, and the calls each item makes — every one a
 * server call (SPEC §5 /lane, /ship-now, /api/runs, skip), never a paste. */
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Caps, Instance } from "../api/types";

const api = vi.fn();
const instApi = vi.fn();
const toast = vi.fn();
vi.mock("../api/client", async (orig) => ({
  ...((await orig()) as object),
  api: (...a: unknown[]) => api(...a),
  instApi: (...a: unknown[]) => instApi(...a),
}));
vi.mock("../lib/toast", () => ({ toast: (...a: unknown[]) => toast(...a) }));
vi.mock("../lib/sessionActions", () => ({ selectSession: () => {}, instances: () => [] }));
vi.mock("../lib/terminals", () => ({ focusTerm: () => {} }));

const L = await import("../lib/laneActions");
const { ApiError } = await import("../api/client");

const inst = (o: Partial<Instance>): Instance =>
  ({ title: "api", provider: "claude", program: "claude", activity: "idle", ...o }) as Instance;
/** Caps as a phase-3 server reports them (splits on), so the MCP reasons are
 * what's under test; `notYet` is today's server. */
const caps = (agent_mcp?: { enabled: boolean; providers: string[] }, split = true): Partial<Caps> =>
  ({ git: true, agent_mcp, team_runs: { split, together: split } }) as Partial<Caps>;

beforeEach(() => {
  api.mockReset();
  instApi.mockReset();
  toast.mockReset();
});

describe("the vocabulary", () => {
  it("autopilot's words map onto lanes", () => {
    expect(L.normalizeLane("agent")).toBe("leave");
    expect(L.normalizeLane("off")).toBe("leave");
    expect(L.normalizeLane(" PR ")).toBe("pr");
    expect(L.normalizeLane("push")).toBe("push");
    expect(L.normalizeLane("nope")).toBe("");
    expect(L.laneDefault("")).toBe("pr");
  });

  it("the control offers four; the menu's keys are L C P M", () => {
    expect(L.LANE_CHOICES).toEqual(["leave", "commit", "pr", "merge"]);
    expect(L.LANE_MENU.map((m) => m.key)).toEqual(["L", "C", "P", "M"]);
    expect(L.LANE_MENU.map((m) => m.label)).toEqual([
      "Leave it",
      "Commit",
      "Open a PR",
      "Merge when checks pass",
    ]);
  });
});

describe("laneChoice: a row's lane as the menu reads it", () => {
  it("row.lane is authoritative", () => {
    expect(L.laneChoice(inst({ lane: { target: "commit", ask_first: true, owner: "web" } }))).toEqual({
      lane: "commit",
      askFirst: true,
      owner: "web",
    });
  });

  it("an older server: the autopilot record's depth is the lane (as the rail reads it)", () => {
    const ap = (depth: string, state = "running") =>
      ({ depth, state, step: "", source: "session" }) as unknown as Instance["autopilot"];
    expect(L.laneChoice(inst({ autopilot: ap("pr") })).lane).toBe("pr");
    expect(L.laneChoice(inst({ autopilot: ap("pr", "done") })).lane).toBe("pr");
    expect(L.laneChoice(inst({ autopilot: ap("agent") })).lane).toBe("leave");
    expect(L.laneChoice(inst({})).lane).toBe("leave");
  });
});

describe("the menu's model", () => {
  const on = caps({ enabled: true, providers: ["claude"] });

  it("ticks the current lane; ask-first is off on Leave it; Ship it now needs a lane", () => {
    const m = L.shipMenuModel(inst({}), [], on);
    expect(m.lanes.filter((e) => e.kind === "lane" && e.current).length).toBe(1);
    const ask = m.lanes.find((e) => e.kind === "ask")!;
    expect(L.entryWhy(ask)).toMatch(/Leave it/);
    const now = m.tail.find((e) => e.kind === "shipnow")!;
    expect(L.entryWhy(now)).toMatch(/Pick how far/);
    expect(m.group).toBeNull();
    expect(m.tail.map((e) => e.kind)).toEqual(["shipnow", "message"]);
  });

  it("a group member gets the group section with its live size and Move out", () => {
    const run = { id: "r1", name: "Q4 payments", task: "t3", role: "task", grouping: "each" };
    const me = inst({ run, lane: { target: "pr", ask_first: false } });
    const rows = [me, inst({ title: "b", run }), inst({ title: "c", run: { ...run, id: "r2" } })];
    const m = L.shipMenuModel(me, rows, on);
    expect(m.group).toEqual({ name: "Q4 payments", count: 2 });
    expect(m.tail.map((e) => e.kind)).toEqual(["shipnow", "detach", "message"]);
    expect(L.entryWhy(m.tail[0])).toBe("");
    expect(L.shipEntries(m).map(L.entryKey)).toEqual(["L", "C", "P", "M", "A", "S", "N", "O", ""]);
  });

  it("a copy window, a group's lead and a one-for-all member can't set a lane or ship themselves", () => {
    const locked = (o: Partial<Instance>) => {
      const m = L.shipMenuModel(inst(o), [], on);
      return [...m.lanes, ...m.tail.filter((e) => e.kind === "shipnow")].map(L.entryWhy);
    };
    // The copy shows the lane of the window that drives its branch.
    const copy = locked({ title: "api-copy", lane: { target: "commit", ask_first: true, owner: "api" } });
    expect(copy.every((w) => /api drives this branch/.test(w))).toBe(true);
    const lead = locked({ run: { id: "r1", name: "G", task: "", role: "lead", grouping: "together" } });
    expect(lead.every((w) => /release/.test(w))).toBe(true);
    const piece = locked({ run: { id: "r1", name: "G", task: "t1", role: "piece", grouping: "together" } });
    expect(piece.every((w) => /one PR/.test(w))).toBe(true);
    // Its own lane, or a member of an each-their-own group: not locked.
    expect(locked({ lane: { target: "pr", ask_first: false, owner: "api" } })).toEqual(["", "", "", "", "", ""]);
    const each = { id: "r1", name: "G", task: "t1", role: "task", grouping: "each" };
    expect(locked({ run: each, lane: { target: "pr", ask_first: false, owner: "api" } })[0]).toBe("");
  });

  it("a group's lead is never offered 'Move out of the group' (it has no task)", () => {
    const run = { id: "r1", name: "G", task: "", role: "lead", grouping: "together" };
    const m = L.shipMenuModel(inst({ run }), [inst({ run })], on);
    expect(m.tail.map((e) => e.kind)).toEqual(["shipnow", "message"]);
  });

  it("Split says why when the lead couldn't propose a plan", () => {
    const why = (o: Partial<Instance>, c = on) =>
      L.entryWhy(L.shipMenuModel(inst(o), [], c).split[0]);
    expect(why({})).toBe("");
    expect(why({ provider: "aider" })).toMatch(/doesn't get the MindFlock tools/);
    expect(why({ mcp_attached: false })).toMatch(/Restart/);
    expect(why({ activity: "clarify" })).toMatch(/Answer its prompt/);
    expect(why({ activity: "limit" })).toMatch(/usage limit/);
    expect(why({}, caps({ enabled: false, providers: ["claude"] }))).toMatch(/switched off/);
    // No cap reported (an older server): unknown, not off — the server decides.
    expect(why({ provider: "aider" }, caps(undefined))).toBe("");
  });

  it("Split is shown but off on an older server that doesn't say it takes splits", () => {
    const notYet = caps({ enabled: true, providers: ["claude"] }, false);
    const split = L.shipMenuModel(inst({}), [], notYet).split[0];
    expect(L.entryWhy(split)).toMatch(/can't split/);
    expect(L.entryWhy(split)).not.toMatch(/coming next/);
    // An older server that doesn't report team_runs at all: not yet, too.
    expect(L.teamRunCaps({ git: true } as Partial<Caps>)).toEqual({ split: false, together: false });
    expect(L.teamRunCaps(caps(undefined))).toEqual({ split: true, together: true });
  });
});

describe("the calls", () => {
  it("a lane is POST /lane with ask_first only where it means something", async () => {
    instApi.mockResolvedValue({ ok: true });
    await L.setLane("api", "pr", true);
    await L.setLane("api", "leave", true);
    expect(instApi.mock.calls).toEqual([
      ["api", "/lane", { json: { lane: "pr", ask_first: true } }],
      ["api", "/lane", { json: { lane: "leave", ask_first: false } }],
    ]);
  });

  it("ship now, and move out of a group (the run's skip)", async () => {
    instApi.mockResolvedValue({ ok: true });
    api.mockResolvedValue({});
    await L.shipNow("api");
    await L.moveOutOfGroup("r 1", "t3");
    expect(instApi.mock.calls[0]).toEqual(["api", "/ship-now", { json: {} }]);
    expect(api.mock.calls[0]).toEqual(["/api/runs/r%201/tasks/t3/skip", { json: {} }]);
  });

  it("Ship it now sends the lane the row showed — never left to the Settings default", async () => {
    instApi.mockResolvedValue({ ok: true });
    const m = L.shipMenuModel(inst({ lane: { target: "commit", ask_first: true, owner: "api" } }), [], undefined);
    const now = m.tail.find((e) => e.kind === "shipnow")!;
    await L.runShipEntry(now, inst({}), "api", m.current);
    expect(instApi.mock.calls[0]).toEqual(["api", "/ship-now", { json: { lane: "commit" } }]);
  });

  it("a split makes this session the lead of a one-line split run", async () => {
    api.mockResolvedValue({ run: { id: "r9" } });
    await L.startSplitOf(
      inst({ last_prompt_full: "port billing, search and upload", repo: "acme" }),
      "api",
      "pr",
      false
    );
    expect(api.mock.calls[0]).toEqual([
      "/api/runs",
      {
        json: {
          name: "api",
          items: [{ kind: "task", text: "port billing, search and upload" }],
          policy: { lane: "pr", ask_first: false, grouping: "together", release: "ask" },
          concurrency: 3,
          program: "claude",
          split: true,
          lead: "api",
        },
      },
    ]);
  });

  it("the New dialog's lane waits out 'workspace not ready', then arms", async () => {
    instApi
      .mockRejectedValueOnce(new ApiError(409, "workspace not ready", null))
      .mockResolvedValueOnce({ ok: true });
    expect(await L.setLaneWhenReady("api", "commit", false, [0])).toBe(true);
    expect(instApi).toHaveBeenCalledTimes(2);
    expect(toast).not.toHaveBeenCalled();
  });

  it("…says the server's other refusals, and never calls for Leave it", async () => {
    instApi.mockRejectedValueOnce(new ApiError(400, "unknown lane", null));
    expect(await L.setLaneWhenReady("api", "pr", false, [0])).toBe(false);
    expect(String(toast.mock.calls[0][0])).toContain("unknown lane");
    instApi.mockReset();
    expect(await L.setLaneWhenReady("api", "leave", false)).toBe(true);
    expect(instApi).not.toHaveBeenCalled();
  });

  it("runShipEntry: the menu and the row › menu share one action per item", async () => {
    instApi.mockResolvedValue({ ok: true });
    const m = L.shipMenuModel(inst({ lane: { target: "pr", ask_first: false } }), [], undefined);
    const commit = m.lanes.find((e) => e.kind === "lane" && e.lane === "commit")!;
    expect(await L.runShipEntry(commit, inst({}), "api", m.current)).toBe("api → Commit");
    const ask = m.lanes.find((e) => e.kind === "ask")!;
    await L.runShipEntry(ask, inst({}), "api", m.current);
    expect(instApi.mock.calls[1]).toEqual(["api", "/lane", { json: { lane: "pr", ask_first: true } }]);
  });
});
