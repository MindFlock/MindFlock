/** One vocabulary for why an automatic start waits (lib/waitWhy): the rail's
 * bar, a waiting row, the Memory gauge and a team run's badge all key on the
 * launch gate's `why`, and only memory / warming may say "memory". Also the
 * memory reading's reasons (the notice and the gauge) and its freshness. */

import { describe, expect, it } from "vitest";
import type { Instance } from "../api/types";
import { code } from "./sourceText";
import {
  WAIT_WHYS,
  aboutMemory,
  gateWhy,
  gaugeOkWords,
  holdHead,
  inLine,
  ordinal,
  runWaitBadge,
  runWaitLine,
  stopWaitingToast,
  waitChip,
  waitClears,
  waitedFor,
  waitHint,
  waitKindWord,
  waitLine,
  waitWhyOf,
  whyFromReason,
} from "../lib/waitWhy";
import { levelWords, machineFromDetail, memoryPopModel, memoryReasons, memoryRowText } from "../components/usage/memoryModel";

const { gateRowsKey } = await import("../state/queries");

describe("the words for each why", () => {
  it("only memory and warming say memory", () => {
    for (const why of WAIT_WHYS) {
      const words = [
        waitChip(why, 3),
        waitClears(why),
        holdHead(why),
        runWaitBadge(why).label,
        runWaitBadge(why).title,
        runWaitLine(why),
        gaugeOkWords(why),
        waitHint({ why, kind: "spawn", since: 1, position: 2 }, 200_000),
      ].join(" | ");
      if (aboutMemory(why)) continue;
      expect(words, why).not.toMatch(/memory/i);
    }
    expect(aboutMemory("memory")).toBe(true);
    expect(aboutMemory("warming")).toBe(true);
    expect(aboutMemory("cap")).toBe(false);
  });

  it("the chips", () => {
    expect(waitChip("memory")).toBe("Waiting for memory");
    expect(waitChip("warming")).toBe("Waiting — other agents are still starting");
    expect(waitChip("spacing")).toBe("Starting shortly");
    expect(waitChip("cap")).toBe("Waiting for a free agent slot");
    expect(waitChip("queue", 3)).toBe("Queued — 2 ahead");
    expect(waitChip("queue", 1)).toBe("Queued");
    expect(waitChip("restart")).toBe("Queued");
  });

  it("an older server's reason text reads as its why", () => {
    expect(whyFromReason("memory is low: 2.1 GB free of 24.0 GB (9%)")).toBe("memory");
    expect(whyFromReason("16 agents are running; automatic starts stop at 16 on this machine")).toBe("cap");
    expect(whyFromReason("the last agent started 4s ago; automatic starts are 20s apart")).toBe("spacing");
    expect(whyFromReason("waiting since before the restart")).toBe("restart");
    expect(whyFromReason("2 agents still starting (~3.0 GB to come): only 1.2 GB would be left")).toBe("warming");
    expect(whyFromReason("")).toBe("memory");
    // A known why wins; an unknown one falls back to the reason.
    expect(waitWhyOf({ why: "spacing", reason: "memory is low" })).toBe("spacing");
    expect(waitWhyOf({ why: "bogus", reason: "automatic starts stop at 4" })).toBe("cap");
    expect(waitWhyOf(null)).toBe("memory");
  });

  it("the gate's why: its own, else the queue head's, else its reason; null when it holds nothing", () => {
    const head = { why: "cap", reason: "x" };
    expect(gateWhy({ holding: true, queued: [head], why: "spacing" })).toBe("spacing");
    expect(gateWhy({ holding: true, queued: [head] })).toBe("cap");
    expect(gateWhy({ holding: true, queued: [{ reason: "" }], reason: "automatic starts stop at 3" })).toBe("cap");
    expect(gateWhy({ holding: false, queued: [] })).toBeNull();
    expect(gateWhy({ holding: true, queued: [] })).toBeNull();
    expect(gateWhy(undefined)).toBeNull();
  });
});

describe("the waiting row's line", () => {
  const now = 1_000_000_000;
  it("says what it is, how long it has waited and where in line", () => {
    expect(waitLine({ kind: "spawn", since: now / 1000 - 180, position: 2 }, now)).toBe("New worker · waiting 3m · 2nd in line");
    expect(waitLine({ kind: "wake", since: now / 1000 - 20, position: 1 }, now)).toBe("Wake · waiting 20s · next in line");
    expect(waitLine({ kind: "queued prompt", since: 0, position: 0 }, now)).toBe("Restart for a queued prompt");
  });
  it("names each kind", () => {
    expect(waitKindWord("spawn")).toBe("New worker");
    expect(waitKindWord("revive")).toBe("Restart");
    expect(waitKindWord("wake")).toBe("Wake");
    expect(waitKindWord("ticket")).toBe("New ticket session");
    expect(waitKindWord("other", true)).toBe("New session");
    expect(waitKindWord(undefined)).toBe("Start");
  });
  it("ordinals and spans", () => {
    expect([1, 2, 3, 4, 11, 12, 13, 21, 22, 23, 101].map(ordinal)).toEqual([
      "1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd", "23rd", "101st",
    ]);
    expect(inLine(1)).toBe("next in line");
    expect(inLine(0)).toBe("");
    expect(waitedFor(now / 1000 - 7200, now)).toBe("waiting 2h");
    expect(waitedFor(null, now)).toBe("");
  });
  it("Stop waiting's toast says what is left", () => {
    expect(stopWaitingToast("w1", true)).toBe("Cancelled w1 — it won't be started");
    expect(stopWaitingToast("w1")).toBe("w1 stopped waiting — start it yourself when you need it");
  });
});

describe("the team run's badge", () => {
  it("says the agent limit, not memory, when the cap holds it", () => {
    expect(runWaitBadge("cap").label).toBe("agent limit");
    expect(runWaitBadge("memory").label).toBe("memory");
    const src = code("components/sidebar/RunGroupHeader.tsx");
    expect(src).toContain("runWaitBadge(why)");
    expect(src).toContain("groupTitle(group, why)");
    expect(src).not.toContain(">memory<");
  });
});

describe("the gauge", () => {
  it("never says room to spare while the gate holds starts back", () => {
    expect(levelWords("ok")).toBe("OK — room to spare");
    expect(levelWords("ok", "cap")).toBe("OK — agent limit reached, new agents wait");
    expect(levelWords("ok", "memory")).toBe("OK, but no room for one more agent — new agents wait");
    expect(levelWords("low", "cap")).toBe("Low — new agents wait");
    const m = machineFromDetail({ total_mb: 20000, available_mb: 10000, level: "ok" });
    expect(memoryRowText(m, { gate: "spacing" }).title).toContain("starting agents one at a time");
  });

  it("shows the server's reasons beside a low level", () => {
    const m = machineFromDetail({ total_mb: 20000, available_mb: 13000, level: "low" });
    const reasons = "Processes stalled on memory 34% of the last minute";
    const model = memoryPopModel(null, m, {}, { reasons });
    expect(model.machine).toContainEqual(["Why", reasons]);
    expect(memoryRowText(m, { reasons }).title).toContain("(processes stalled on memory 34% of the last minute)");
    // Not at OK.
    const ok = memoryPopModel(null, { ...m, level: "ok" }, {}, { reasons });
    expect(ok.machine.map((r) => r[0])).not.toContain("Why");
  });
});

describe("the memory reading's reasons", () => {
  it("reads the why list, else a message that says more than the free figures", () => {
    expect(memoryReasons({ why: ["processes stalled on memory 34% of the last minute", "2 OOM kills in the last 10 minutes"] })).toBe(
      "Processes stalled on memory 34% of the last minute; 2 OOM kills in the last 10 minutes"
    );
    expect(memoryReasons({ why: [], message: "memory is critical: Windows commit charge at 97%" })).toBe("Windows commit charge at 97%");
    expect(memoryReasons({ message: "memory is low: 2.1 GB free of 24.0 GB (9%)" })).toBe("");
    expect(memoryReasons({ why: ["", 3] })).toBe("");
    expect(memoryReasons(null)).toBe("");
  });

  it("the notice renders it", () => {
    const src = code("components/MemoryNotice.tsx");
    expect(src).toContain("memoryReasons(data)");
    expect(src).toContain("mn-why");
  });
});

describe("the reading stays fresh", () => {
  it("refetches on the server's memory events and on a queue change", () => {
    const src = code("state/queries.ts");
    expect(src).toContain('"system.memory_changed", "system.sessions_slept"');
    expect(src).toContain("useEffect(bridgeMemoryEvents, [])");
    expect(src).toContain("noteGateRows(fetched)");
  });

  it("the gate's rows key changes with who waits, why, where, and who failed", () => {
    const w = (title: string, why: string, position: number) =>
      ({ title, waiting_for_memory: { id: "1", title, kind: "spawn", since: 1, reason: "", position, why } }) as Partial<Instance>;
    const a = gateRowsKey([w("a", "memory", 1), { title: "b" }]);
    expect(a).toBe(gateRowsKey([{ title: "b" }, w("a", "memory", 1)]));
    expect(a).not.toBe(gateRowsKey([w("a", "cap", 1)]));
    expect(a).not.toBe(gateRowsKey([w("a", "memory", 2)]));
    expect(gateRowsKey([{ title: "c", start_failed: { error: "x", at: 5 } }])).toBe("f:c:5");
    expect(gateRowsKey([])).toBe("");
  });
});
