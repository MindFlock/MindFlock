/** The plan loop and "what could break" away from the Map: the rail chip's
 * plan state, the bell's "Plan ready" row, and the blast digest Make PR shows. */
import { describe, expect, it } from "vitest";
import { redZoneChip } from "../lib/codemapSeen";
import { needsAttention, withPlanReady } from "../components/outbox/outbox";
import { digestLine, digestOf, EMPTY_DIGEST } from "../lib/mapDigest";
import { src } from "./sourceText";

describe("rail chip: plan · n", () => {
  const base = { zones: 0, breaches: 0, last_block_ts: null, guard: "none", mode: null };

  it("shows a waiting plan, with the Plan section as its click target", () => {
    const c = redZoneChip({ ...base, plan_pending: 3 }, 0);
    expect(c?.label).toBe("plan · 3");
    expect(c?.title).toBe("Plan ready — review & Go");
    expect(c?.cls).toContain("rz-plan");
    expect(c?.section).toBe("plan");
  });

  it("yields to anything louder, and outranks the quiet shield", () => {
    const guarded = { ...base, zones: 2, guard: "guarded", plan_pending: 2 };
    expect(redZoneChip({ ...guarded, breaches: 1 }, 0)?.section).toBe("breaches");
    expect(redZoneChip({ ...guarded, last_block_ts: 10 }, 5)?.label).toBe("⛔");
    expect(redZoneChip({ ...guarded, guard: "off" }, 0)?.label).toBe("!");
    expect(redZoneChip(guarded, 0)?.label).toBe("plan · 2");
    expect(redZoneChip({ ...guarded, plan_pending: 0 }, 0)?.label).toBe("🛡");
    expect(redZoneChip({ ...base, plan_pending: 0 }, 0)).toBeNull();
  });
});

describe("bell: Plan ready — review & Go", () => {
  const row = (title: string, plan_pending?: number, extra: object = {}) => ({
    title,
    redzone: plan_pending === undefined ? null : { plan_pending },
    ...extra,
  });

  it("adds a row ranked like an approval for a session waiting on Go", () => {
    const rows = withPlanReady(
      needsAttention(
        [
          { p: 0, title: "asking", reason: "needs your answer" },
          { p: 3, title: "ready", reason: "pushed — ready for PR" },
        ],
        []
      ),
      [row("asking"), row("ready"), row("planner", 4)]
    );
    expect(rows.map((r) => [r.key, r.rank, r.plan ?? 0])).toEqual([
      ["asking", 0, 0],
      ["planner", 1, 4],
      ["ready", 4, 0],
    ]);
  });

  it("is one row per session: replaces a lesser reason, never a question", () => {
    const rows = withPlanReady(
      needsAttention(
        [
          { p: 0, title: "asking", reason: "needs your answer" },
          { p: 2, title: "red", reason: "checks failing" },
        ],
        []
      ),
      [row("asking", 2), row("red", 1)]
    );
    expect(rows.map((r) => [r.key, r.rank, r.plan ?? 0])).toEqual([
      ["asking", 0, 0],
      ["red", 1, 1],
    ]);
  });

  it("leaves out paused and missing sessions", () => {
    const rows = withPlanReady([], [row("p", 1, { status: "paused" }), row("m", 1, { workspace_missing: true })]);
    expect(rows).toEqual([]);
  });

  it("is wired into the bell, opening the Map's Plan section", () => {
    const bell = src("components/NotificationsBell.tsx");
    expect(bell).toContain("withPlanReady(needsAttention(attentionItems(instances), outbox?.groups?.waiting), instances)");
    expect(bell).toContain('openMap(it.title, { section: "plan" })');
  });
});

describe("what could break: digestOf", () => {
  // a.py ← b.py ← c.py; d/e.py and tests/test_a.py import a.py.
  const snap = {
    files: [
      ["a.py", 1, 0],
      ["b.py", 1, 0],
      ["c.py", 1, 0],
      ["d/e.py", 1, 0],
      ["tests/test_a.py", 1, 2],
    ] as Array<[string, number, number]>,
    edges: [
      [1, 0],
      [2, 1],
      [3, 0],
      [4, 0],
    ] as Array<[number, number]>,
  };
  const live = {
    now: 1000,
    activity: "idle",
    changed: [{ path: "a.py", status: "M", added: 1, removed: 0 }],
    feed: [],
    plan: null,
  };

  it("counts the direct importers of the changes, tests apart", () => {
    const d = digestOf(snap, live);
    expect([d.changed, d.dependents, d.folders, d.tests, d.ready]).toEqual([1, 2, 2, 1, true]);
    expect(digestLine(d)).toBe("2 files in 2 folders (1 test)");
  });

  it("grows from the plan while nothing was edited since it", () => {
    const plan = { source: "declared", ts: 900, items: [{ path: "b.py", intent: "", new: false }], thread: "" };
    const d = digestOf(snap, { ...live, changed: [], plan } as never);
    expect([d.changed, d.dependents, d.tests]).toEqual([1, 1, 0]);
    expect(digestLine(d)).toBe("1 file in 1 folder");
  });

  it("is nothing to say with no importers, and not ready without both reads", () => {
    expect(digestLine(digestOf(snap, { ...live, changed: [{ path: "c.py", status: "M", added: 1, removed: 0 }] }))).toBe("");
    expect(digestOf(null, live)).toBe(EMPTY_DIGEST);
    expect(digestOf(snap, null).ready).toBe(false);
  });

  it("Make PR shows it under the base field; Verify's menu opens the risk layer", () => {
    const pr = src("components/dialogs/MakePrDialog.tsx");
    expect(pr).toContain("useMapDigest(target");
    expect(pr).toContain('openMap(target, { section: "risk" })');
    const verify = src("components/dialogs/VerifyPlan.tsx");
    expect(verify).toContain('label: "What could break"');
    expect(verify).toContain('openMap(session.title, { section: "risk" })');
  });
});

describe("Plan first lands on the Map", () => {
  it("opens the Plan section after a Plan-first create", () => {
    const s = src("lib/newSessionSubmit.ts");
    expect(s).toContain('if (body.plan_first) useUi.getState().openMap(inst.title, { section: "plan" });');
  });
});
