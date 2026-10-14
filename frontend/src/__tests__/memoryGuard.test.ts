/** The memory notice's detail line and the rail's "Restart all" toast
 * (components/MemoryNotice.tsx, components/sidebar/OfflineBar.tsx). */

import { describe, expect, it } from "vitest";
import { memoryDetail } from "../components/MemoryNotice";
import { sweepToast } from "../components/sidebar/OfflineBar";

const GB = 1024 ** 3;

describe("memoryDetail", () => {
  it("names free memory, swap in use and the stall share", () => {
    expect(
      memoryDetail({
        level: "low",
        available: 2.1 * GB,
        total: 23.5 * GB,
        available_pct: 8.9,
        swap_total: 16 * GB,
        swap_free: 2 * GB,
        psi: { some_avg60: 17.4 },
      })
    ).toBe("2.1 GB free of 23.5 GB (9%) · swap 88% used · stalled on memory 17% of the last minute");
  });

  it("leaves out a quiet swap and a calm PSI", () => {
    expect(
      memoryDetail({ level: "low", available: 3 * GB, total: 24 * GB, available_pct: 12.5, swap_total: 8 * GB, swap_free: 8 * GB, psi: { some_avg60: 1 } })
    ).toBe("3.0 GB free of 24.0 GB (13%)");
  });
});

describe("sweepToast", () => {
  it("counts restarts and the agents told to continue", () => {
    expect(sweepToast({ revived: ["a", "b", "c"], continued: ["b"], skipped: {} }, ["a", "b", "c"])).toBe(
      "Restarted 3 agents — 1 told to continue where it left off"
    );
  });

  it("says when memory held the rest back", () => {
    expect(
      sweepToast({ revived: ["a"], continued: [], skipped: { b: "low memory", c: "low memory" } }, ["a", "b", "c"])
    ).toBe("Restarted 1 agent; 2 held back: memory is low");
  });

  it("reports only the rows the bar offered, not every session the sweep passed over", () => {
    expect(
      sweepToast(
        { revived: [], skipped: { a: "workspace missing", paused1: "paused", other: "usage limit" } },
        ["a", "b"]
      )
    ).toBe("No agents restarted; 1 couldn't restart (workspace missing)");
  });
});
