import { describe, expect, it } from "vitest";
import type { RunDTO } from "../api/types";
import {
  cardTone,
  fenceChips,
  modeLine,
  normOrder,
  orderWorthShowing,
  overlapLines,
  runStages,
  stepCaption,
} from "../lib/order";
import { workerLine, workerState } from "../lib/agentMessages";
import { zoneLocked, zoneWhere } from "../lib/codetree/zones";
import threadTabSrc from "../components/grid/ThreadTab.tsx?raw";
import diagramSrc from "../components/grid/OrderDiagram.tsx?raw";

const body = {
  mode: "parallel",
  max_parallel: 2,
  cap: 2,
  steps: [
    {
      n: 1,
      workers: [
        { title: "w1", state: "running", word: "running", detail: "", after: [], why: {}, fence: { only: ["src/api/**"], keep_out: [] } },
        { title: "w2", state: "done", word: "done", detail: "reported done", after: [], why: {} },
      ],
    },
    {
      n: 2,
      workers: [
        {
          title: "w3",
          state: "held",
          word: "waiting",
          detail: "after w1",
          after: ["w1"],
          why: { w1: "overlap: src/api/x.py" },
          fence: { only: [], keep_out: ["db/"], reason: "no schema" },
        },
      ],
    },
    { n: 3, workers: [{ title: "", state: "held" }] },
  ],
};

describe("normOrder", () => {
  it("keeps steps with cards, drops junk", () => {
    const o = normOrder(body)!;
    expect(o.steps.map((s) => s.workers.map((c) => c.title))).toEqual([["w1", "w2"], ["w3"]]);
    expect(o.steps[1].workers[0].fence).toEqual({ only: [], keep_out: ["db/"], reason: "no schema", by: "" });
    expect(o.steps[0].workers[1].fence).toBeNull();
    expect(normOrder(null)).toBeNull();
    expect(normOrder({ steps: [] })).toBeNull();
  });
});

describe("words", () => {
  it("says the rule", () => {
    const o = normOrder(body)!;
    expect(modeLine(o)).toBe("Up to 2 at a time, in 2 steps");
    expect(modeLine({ ...o, mode: "serial", cap: 1 })).toBe("One at a time");
    expect(modeLine({ ...o, cap: 0, steps: [o.steps[0]] })).toBe("All at once");
    expect(stepCaption(0, 2, o)).toBe("Step 1 · together");
    expect(stepCaption(0, 3, o)).toBe("Step 1 · 2 at a time");
    expect(stepCaption(1, 1, o)).toBe("Step 2");
  });

  it("tones, overlap reasons and fence chips", () => {
    expect(["done", "running", "stopped", "held", "planned"].map((state) => cardTone({ state }))).toEqual([
      "ok",
      "work",
      "bad",
      "idle",
      "idle",
    ]);
    const w3 = normOrder(body)!.steps[1].workers[0];
    expect(overlapLines(w3)).toEqual(["same files as w1 (src/api/x.py)"]);
    expect(overlapLines({ why: { w1: "step" } })).toEqual([]);
    expect(fenceChips({ only: ["a/**"], keep_out: ["b/"] })).toEqual([
      { kind: "only", text: "only a/**" },
      { kind: "out", text: "⛔ b/" },
    ]);
    expect(fenceChips(null)).toEqual([]);
  });

  it("shows the diagram only when it says something", () => {
    expect(orderWorthShowing(normOrder(body))).toBe(true);
    const plain = normOrder({ mode: "parallel", cap: 0, steps: [{ n: 1, workers: [{ title: "a", state: "running" }] }] });
    expect(orderWorthShowing(plain)).toBe(false);
    expect(orderWorthShowing(null)).toBe(false);
  });
});

describe("a held worker on the rail", () => {
  it("is waiting its turn, not idle", () => {
    const row = { title: "w3", activity: "idle", order: { state: "held", word: "waiting", detail: "after w1", after: ["w1"], fence: null } };
    expect(workerState(row, "idle")).toBe("waiting");
    const line = workerLine(row, { nested: true, parentName: "", act: "idle" });
    expect(line.text).toBe("waiting · after w1");
    expect(line.title).toContain("MindFlock holds its task");
    // A dialog still wins: it needs you.
    expect(workerState(row, "clarify")).toBe("ask");
  });
});

describe("runStages", () => {
  const run = (over: Partial<RunDTO>): RunDTO =>
    ({ id: "r", name: "g", state: "running", split: true, mode: "worktrees", concurrency: 3, tasks: [], plan: { pieces: [{}, {}] }, check: { state: "none" }, release: { state: "none" }, ...over }) as unknown as RunDTO;

  it("lights the plan while planning", () => {
    const s = runStages(run({ state: "plan_ready" }));
    expect(s.map((x) => [x.key, x.state])).toEqual([
      ["plan", "now"],
      ["pieces", "next"],
      ["merge", "next"],
      ["check", "next"],
      ["release", "next"],
    ]);
    expect(s[0].detail).toBe("2 pieces — waiting for you");
  });

  it("counts pieces and merges, same folder commits", () => {
    const tasks = [
      { id: "1", state: "integrated", title: "a" },
      { id: "2", state: "working", title: "b" },
      { id: "3", state: "queued", title: "c" },
    ] as unknown as RunDTO["tasks"];
    const s = runStages(run({ tasks }));
    expect(s[1]).toMatchObject({ state: "now", detail: "1 of 3 done · 1 working · 1 queued" });
    expect(s[2]).toMatchObject({ label: "Merge back", how: "one at a time", state: "now", detail: "1 of 3 merged" });
    const sf = runStages(run({ tasks, mode: "same_folder" }));
    expect(sf[1].how).toBe("together, in this folder");
    expect(sf[2]).toMatchObject({ label: "Commit each", detail: "1 of 3 committed" });
  });

  it("a non-split group has no plan stage; release waits for you", () => {
    const s = runStages(run({ split: false, state: "release_ready", check: { state: "ok", summary: "12 passed" } }));
    expect(s.map((x) => x.key)).toEqual(["pieces", "merge", "check", "release"]);
    expect(s[2]).toMatchObject({ state: "done", detail: "12 passed" });
    expect(s[3]).toMatchObject({ state: "now", detail: "waiting for you" });
  });
});

describe("fence zones on the Map", () => {
  it("say who set them and are locked", () => {
    expect(zoneWhere({ scope: "session", by: "orch" })).toBe("this session · set by orch");
    expect(zoneWhere({ scope: "worktree" })).toBe("this worktree");
    expect(zoneWhere({ scope: "repo" })).toBe("whole repo");
    expect(zoneWhere({ scope: "repo" }, true)).toBe("allowed in this worktree");
    expect(zoneLocked({ scope: "session" })).toBe(true);
    expect(zoneLocked({ scope: "worktree" })).toBe(false);
  });
});

describe("wiring", () => {
  it("the Thread draws the order for an orchestrator, never for a split lead", () => {
    expect(threadTabSrc).toContain("!leadOf && orderWorthShowing(data?.order)");
    expect(diagramSrc).toContain('json: { start_now: [worker] }');
    expect(diagramSrc).not.toMatch(/window\.(prompt|confirm|alert)|\bconfirm\(/);
  });
});
