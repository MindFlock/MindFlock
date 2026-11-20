/** Error polish: the connection banner decides "answering with errors" vs
 * "not answering" from the failure's HTTP status (client.ts words every
 * failure as a sentence, so its text can't tell them apart), and the bell's
 * Clear survives a reload (the event bus replays its backlog on page load). */

import { afterEach, describe, expect, it } from "vitest";
import { ApiError, UNREACHABLE } from "../api/client";
import { pollFailText } from "../components/ConnBanner";
import { NOTIF_CLEARED_KEY, loadClearedTs, saveClearedTs } from "../components/NotificationsBell";

describe("ConnBanner pollFailText", () => {
  it("a 5xx (already a sentence from client.ts) reads as answering with errors", () => {
    const t = pollFailText(new ApiError(500, "MindFlock hit an error (details in the log).", null));
    expect(t).toContain("reachable but answering with errors (HTTP 500)");
    expect(t).not.toContain("isn’t answering");
  });

  it("no answer at all (status 0) reads as not answering", () => {
    expect(pollFailText(new ApiError(0, UNREACHABLE, null))).toContain("The server isn’t answering");
  });

  it("a non-ApiError or nothing reads as not answering", () => {
    expect(pollFailText(new TypeError("Failed to fetch"))).toContain("isn’t answering");
    expect(pollFailText(null)).toContain("isn’t answering");
  });
});

describe("bell Clear sticks across a reload", () => {
  const g = globalThis as unknown as { localStorage?: Storage };
  const real = g.localStorage;
  afterEach(() => {
    g.localStorage = real;
  });
  function fakeStorage(): Map<string, string> {
    const m = new Map<string, string>();
    g.localStorage = {
      getItem: (k: string) => m.get(k) ?? null,
      setItem: (k: string, v: string) => void m.set(k, v),
    } as unknown as Storage;
    return m;
  }

  it("saves the newest cleared row's ts and reads it back", () => {
    const m = fakeStorage();
    expect(loadClearedTs()).toBe(0);
    const mark = saveClearedTs([{ ts: 100 }, { ts: 250.5 }, { ts: 200 }], 0);
    expect(mark).toBe(250.5);
    expect(m.get(NOTIF_CLEARED_KEY)).toBe("250.5");
    expect(loadClearedTs()).toBe(250.5);
  });

  it("an empty Clear never moves the mark back", () => {
    fakeStorage();
    expect(saveClearedTs([], 300)).toBe(300);
    expect(saveClearedTs([{ ts: 10 }], 300)).toBe(300);
  });

  it("no storage: Clear still returns its mark, and loading reads 0", () => {
    g.localStorage = {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("blocked");
      },
    } as unknown as Storage;
    expect(loadClearedTs()).toBe(0);
    expect(saveClearedTs([{ ts: 5 }], 0)).toBe(5);
  });
});

describe("bell wiring", () => {
  it("the replayed backlog skips rows at or before the mark, and Clear saves it", async () => {
    const src = (await import("../components/NotificationsBell.tsx?raw")).default as string;
    expect(src).toContain("const clearedTs = useRef(loadClearedTs());");
    expect(src).toMatch(/if \(ts <= clearedTs\.current\) return;/);
    expect(src).toContain("clearedTs.current = saveClearedTs(notifs, clearedTs.current);");
  });
});
