import { describe, expect, it } from "vitest";
import type { Instance } from "../api/types";
import { offlineRows } from "../components/sidebar/OfflineBar";
import { HELD_WORKER_LABEL, deriveSessionState } from "../lib/sessionState";

/** A held worker's agent starts only on its turn (core.worker_order_driver):
 * until then it reads offline to the server, but the rail says Waiting and
 * "Start all" leaves it alone. */
const held = {
  title: "pr3153-refs",
  status: "running",
  activity: "offline",
  order: { state: "held", word: "waiting", detail: "after pr3150-counts-refs", after: ["pr3150-counts-refs"], fence: null },
} as unknown as Instance;

describe("a held worker waits, not offline", () => {
  it("reads Waiting, out of the bell, not restartable", () => {
    const ss = deriveSessionState(held, { activity: "offline" });
    expect(ss.label).toBe(HELD_WORKER_LABEL);
    expect(ss.state).not.toBe("offline");
    expect(ss.inBell).toBe(false);
    expect(ss.restartable).toBe(false);
    expect(ss.hint).toContain("after pr3150-counts-refs");
  });
  it("is not counted by Start all", () => {
    expect(offlineRows([held])).toEqual([]);
  });
  it("a released worker that went down still reads offline", () => {
    const down = { ...held, order: { ...(held.order as object), state: "running" } } as Instance;
    expect(deriveSessionState(down, { activity: "offline" }).state).toBe("offline");
  });
});
