/** Fast-track (lib/laneActions): the ONE vocabulary every control speaks,
 * reading a row's target, the ⏩ picker's model, and the calls each item
 * makes — every one a server call (/lane, /api/runs, the run's skip), never a
 * paste. The ⏩ button's own descriptor is pinned in autopilot.test.ts. */
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Caps, Instance } from "../api/types";

const api = vi.fn();
const instApi = vi.fn();
const toast = vi.fn();
const patchInstance = vi.fn();
const threadOpen = vi.fn();
let rows: Instance[] = [];
vi.mock("../api/client", async (orig) => ({
  ...((await orig()) as object),
  api: (...a: unknown[]) => api(...a),
  instApi: (...a: unknown[]) => instApi(...a),
}));
vi.mock("../lib/toast", () => ({ toast: (...a: unknown[]) => toast(...a) }));
vi.mock("../state/queries", () => ({ patchInstance: (...a: unknown[]) => patchInstance(...a) }));
vi.mock("../state/store", () => ({
  useUi: { getState: () => ({ threadOpen: (...a: unknown[]) => threadOpen(...a), setFastTrackMenu: () => {} }) },
}));
vi.mock("../lib/sessionActions", () => ({ selectSession: () => {}, instances: () => rows }));
vi.mock("../lib/terminals", () => ({ focusTerm: () => {} }));

const L = await import("../lib/laneActions");
const { ApiError } = await import("../api/client");

const inst = (o: Partial<Instance>): Instance =>
  ({ title: "api", provider: "claude", program: "claude", activity: "idle", ...o }) as Instance;
/** Caps as a server that takes splits reports them, so the MCP reasons are
 * what's under test; `notYet` is an older server. */
const caps = (agent_mcp?: { enabled: boolean; providers: string[] }, split = true): Partial<Caps> =>
  ({ git: true, agent_mcp, team_runs: { split, together: split } }) as Partial<Caps>;

beforeEach(() => {
  api.mockReset();
  instApi.mockReset();
  toast.mockReset();
  patchInstance.mockReset();
  threadOpen.mockReset();
  rows = [];
});

describe("the vocabulary", () => {
  it("autopilot's words map onto fast-track targets", () => {
    expect(L.normalizeLane("agent")).toBe("leave");
    expect(L.normalizeLane("off")).toBe("leave");
    expect(L.normalizeLane(" PR ")).toBe("pr");
    expect(L.normalizeLane("push")).toBe("push");
    expect(L.normalizeLane("nope")).toBe("");
  });

  it("every control offers all five rungs, Push included, in ladder order", () => {
    expect(L.LANE_CHOICES).toEqual(["leave", "commit", "push", "pr", "merge"]);
    expect(L.LANE_CHOICES.map((l) => L.LANE_LABEL[l])).toEqual([
      "Off",
      "Commit",
      "Push",
      "Open a PR",
      "Merge when green",
    ]);
    expect(L.LANE_CHOICES.map((l) => L.LANE_SHORT[l])).toEqual(["off", "Commit", "Push", "PR", "Merge"]);
    // The picker's letters are unique and leave A for "ask me first".
    const keys = L.LANE_CHOICES.map((l) => L.LANE_KEY[l]);
    expect(keys).toEqual(["O", "C", "U", "P", "M"]);
    expect(new Set([...keys, "A"]).size).toBe(6);
  });

  it("no label a screen shows says 'lane', 'Leave it' or 'Ship'", () => {
    const words = [
      ...Object.values(L.LANE_LABEL),
      ...Object.values(L.LANE_SHORT),
      ...Object.values(L.LANE_DESC),
      L.ASK_FIRST_DESC,
    ].join(" | ");
    expect(words).not.toMatch(/\blanes?\b/i);
    expect(words).not.toMatch(/Leave it/);
    expect(words).not.toMatch(/\bShip\b/);
  });

  it("ask-first sends you to the bell, where approvals wait", () => {
    expect(L.ASK_FIRST_DESC).toBe("Stop one step short and wait for your OK in the bell");
  });

  it("THE default is the Settings one, Off when unset", () => {
    expect(L.laneDefault("")).toBe("leave");
    expect(L.laneDefault(undefined)).toBe("leave");
    expect(L.laneDefault("junk")).toBe("leave");
    expect(L.laneDefault("off")).toBe("leave");
    expect(L.laneDefault("pr")).toBe("pr");
    expect(L.laneDefault("commit")).toBe("commit");
    expect(L.laneDefault("merge")).toBe("merge");
  });
});

describe("laneChoice: a row's target as the controls read it", () => {
  it("row.lane is authoritative", () => {
    expect(L.laneChoice(inst({ lane: { target: "commit", ask_first: true, owner: "web" } }))).toEqual({
      lane: "commit",
      askFirst: true,
      owner: "web",
    });
  });

  it("an older server: the autopilot record's depth is the target (as the rail reads it)", () => {
    const ap = (depth: string, state = "running") =>
      ({ depth, state, step: "", source: "session" }) as unknown as Instance["autopilot"];
    expect(L.laneChoice(inst({ autopilot: ap("pr") })).lane).toBe("pr");
    expect(L.laneChoice(inst({ autopilot: ap("pr", "done") })).lane).toBe("pr");
    expect(L.laneChoice(inst({ autopilot: ap("agent") })).lane).toBe("leave");
    expect(L.laneChoice(inst({})).lane).toBe("leave");
  });
});

describe("the ⏩ picker's model", () => {
  it("offers every rung with the current one ticked; ask-first needs a rung", () => {
    const m = L.fastTrackModel(inst({}));
    expect(m.items.map((i) => i.lane)).toEqual(["leave", "commit", "push", "pr", "merge"]);
    expect(m.items.filter((i) => i.current).map((i) => i.lane)).toEqual(["leave"]);
    expect(m.lock).toBe("");
    expect(m.ask.on).toBe(false);
    expect(m.ask.why).toMatch(/Fast-track is off/);
    const pr = L.fastTrackModel(inst({ lane: { target: "pr", ask_first: true, owner: "api" } }));
    expect(pr.items.find((i) => i.current)?.lane).toBe("pr");
    expect(pr.ask).toEqual({ on: true, why: "" });
    expect(pr.current).toEqual({ lane: "pr", askFirst: true });
  });

  it("a copy window, a group's lead and a one-for-all member can't set their own", () => {
    const lockOf = (o: Partial<Instance>) => {
      const m = L.fastTrackModel(inst(o));
      return [m.lock, m.ask.why];
    };
    const copy = lockOf({ title: "api-copy", lane: { target: "commit", ask_first: true, owner: "api" } });
    expect(copy.every((w) => /api drives this branch — set its fast-track/.test(w))).toBe(true);
    const lead = lockOf({ run: { id: "r1", name: "G", task: "", role: "lead", grouping: "together" } });
    expect(lead.every((w) => /release/.test(w))).toBe(true);
    const piece = lockOf({ run: { id: "r1", name: "G", task: "t1", role: "piece", grouping: "together" } });
    expect(piece.every((w) => /one PR — move it out of the group to fast-track it on its own/.test(w))).toBe(
      true
    );
    // Its own target, or a member of an each-their-own group: not locked.
    expect(lockOf({ lane: { target: "pr", ask_first: false, owner: "api" } })).toEqual(["", ""]);
    const each = { id: "r1", name: "G", task: "t1", role: "task", grouping: "each" };
    expect(lockOf({ run: each, lane: { target: "pr", ask_first: false, owner: "api" } })[0]).toBe("");
  });

  it("the sentence a pick is confirmed with names fast-track, never a lane", () => {
    expect(L.fastTrackSaid("api", "pr", true)).toBe("api: fast-track → Open a PR, asks first");
    expect(L.fastTrackSaid("api", "push", false)).toBe("api: fast-track → Push");
    expect(L.fastTrackSaid("api", "leave", true)).toMatch(/^api: fast-track off/);
  });
});

describe("what the row › menu and the palette offer besides the picker", () => {
  const on = caps({ enabled: true, providers: ["claude"] });

  it("Move out of a group: a member line only, never a lead", () => {
    const run = { id: "r1", name: "Q4 payments", task: "t3", role: "task", grouping: "each" };
    expect(L.detachableGroup(inst({ run }))).toEqual({ id: "r1", task: "t3", name: "Q4 payments" });
    expect(L.detachableGroup(inst({ run: { ...run, role: "lead", task: "" } }))).toBeNull();
    expect(L.detachableGroup(inst({}))).toBeNull();
    expect(L.groupSize(inst({ run }), [inst({ run }), inst({ title: "b", run }), inst({ title: "c" })])).toBe(2);
  });

  it("Split says why when the lead couldn't propose a plan", () => {
    const why = (o: Partial<Instance>, c = on) => L.splitBlockReason(c, inst(o));
    expect(why({})).toBe("");
    expect(why({ provider: "aider" })).toMatch(/doesn't get the MindFlock tools/);
    expect(why({ mcp_attached: false })).toMatch(/Restart/);
    expect(why({ activity: "clarify" })).toMatch(/Answer its prompt/);
    expect(why({ activity: "limit" })).toMatch(/usage limit/);
    expect(why({}, caps({ enabled: false, providers: ["claude"] }))).toMatch(/switched off/);
    // No cap reported (an older server): unknown, not off — the server decides.
    expect(why({ provider: "aider" }, caps(undefined))).toBe("");
  });

  it("Split is off on an older server that doesn't say it takes splits", () => {
    const notYet = caps({ enabled: true, providers: ["claude"] }, false);
    expect(L.splitBlockReason(notYet, inst({}))).toMatch(/can't split/);
    expect(L.teamRunCaps({ git: true } as Partial<Caps>)).toEqual({ split: false, together: false });
    expect(L.teamRunCaps(caps(undefined))).toEqual({ split: true, together: true });
  });
});

describe("the calls", () => {
  it("a target is POST /lane with ask_first only where it means something", async () => {
    instApi.mockResolvedValue({ ok: true });
    await L.setLane("api", "pr", true);
    await L.setLane("api", "leave", true);
    await L.setLane("api", "push", false, { message: "Add search" });
    expect(instApi.mock.calls).toEqual([
      ["api", "/lane", { json: { lane: "pr", ask_first: true } }],
      ["api", "/lane", { json: { lane: "leave", ask_first: false } }],
      ["api", "/lane", { json: { lane: "push", ask_first: false, message: "Add search" } }],
    ]);
  });

  it("pickFastTrack flips the row at once, settles it from the answer and says so", async () => {
    rows = [inst({ lane: null, autopilot: null })];
    const lane = { target: "merge", ask_first: false, owner: "api", by: "user" };
    instApi.mockResolvedValue({ ok: true, lane, autopilot: { depth: "merge", state: "running" } });
    expect(await L.pickFastTrack("api", "api", "merge", false)).toBe(true);
    expect(patchInstance.mock.calls[0]).toEqual(["api", { lane }]);
    expect(patchInstance.mock.calls[1][1].lane).toEqual(lane);
    expect(patchInstance.mock.calls[1][1].autopilot.depth).toBe("merge");
    expect(toast.mock.calls[0][0]).toBe("api: fast-track → Merge when green");
  });

  it("…and rolls back with the server's sentence on a refusal", async () => {
    const before = { target: "pr", ask_first: false, owner: "api" };
    rows = [inst({ lane: before, autopilot: null })];
    instApi.mockRejectedValueOnce(new ApiError(409, "its group is paused", null));
    expect(await L.pickFastTrack("api", "api", "leave", false)).toBe(false);
    expect(patchInstance.mock.calls[0]).toEqual(["api", { lane: null, autopilot: null }]);
    expect(patchInstance.mock.calls[1]).toEqual(["api", { lane: before, autopilot: null }]);
    expect(String(toast.mock.calls[0][0])).toContain("its group is paused");
  });

  it("move out of a group is the run's skip", async () => {
    api.mockResolvedValue({});
    await L.moveOutOfGroup("r 1", "t3");
    expect(api.mock.calls[0]).toEqual(["/api/runs/r%201/tasks/t3/skip", { json: {} }]);
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

  it("splitSession carries the session's own target (a PR when it had none) and opens the Thread", async () => {
    api.mockResolvedValue({ run: { id: "r9" } });
    const said = await L.splitSession(inst({ last_prompt: "x" }), "api");
    expect(api.mock.calls[0][1].json.policy.lane).toBe("pr");
    expect(threadOpen).toHaveBeenCalledWith("api");
    expect(said).toMatch(/approve the plan in its Thread tab/);
    await L.splitSession(inst({ last_prompt: "x", lane: { target: "merge", ask_first: true } }), "api");
    expect(api.mock.calls[1][1].json.policy).toMatchObject({ lane: "merge", ask_first: true });
  });

  it("the New dialog's fast-track waits out 'workspace not ready', then arms", async () => {
    instApi
      .mockRejectedValueOnce(new ApiError(409, "workspace not ready", null))
      .mockResolvedValueOnce({ ok: true });
    expect(await L.setLaneWhenReady("api", "commit", false, [0])).toBe(true);
    expect(instApi).toHaveBeenCalledTimes(2);
    expect(toast).not.toHaveBeenCalled();
  });

  it("…says the server's other refusals, and never calls for Off", async () => {
    instApi.mockRejectedValueOnce(new ApiError(400, "unknown lane", null));
    expect(await L.setLaneWhenReady("api", "pr", false, [0])).toBe(false);
    expect(String(toast.mock.calls[0][0])).toMatch(/^Couldn't fast-track api to “Open a PR”/);
    instApi.mockReset();
    expect(await L.setLaneWhenReady("api", "leave", false)).toBe(true);
    expect(instApi).not.toHaveBeenCalled();
  });

  it("the Ship & split menu's calls are gone: no ship-now, no menu model", () => {
    const mod = L as unknown as Record<string, unknown>;
    for (const gone of ["shipNow", "shipMenuModel", "shipEntries", "runShipEntry", "openShipMenu", "LANE_MENU"])
      expect(mod[gone], gone).toBeUndefined();
  });
});
