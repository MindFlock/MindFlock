/** The ↺ toggle's HTTP wiring — the layer that actually broke.
 *
 * The first cut of `resetStage` called `instApi(title, "/reset-stage")` with no
 * options, and `api()` only upgrades a request to POST when a `json` body is
 * passed. So the press sent a GET, which the static fallback answers with a 404
 * — the button toasted "Could not reset this window: Not Found" while every
 * other layer was green: the route test called the coroutine directly and the
 * hand-check used `curl -X POST`. Nothing between them asserted the method, so
 * that is exactly what this file does.
 */

import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { queryClient } from "../state/queries";
import { resetStage } from "../lib/sessionActions";

interface Call {
  url: string;
  method: string;
}

const calls: Call[] = [];
const realFetch = globalThis.fetch;

beforeEach(() => {
  calls.length = 0;
  globalThis.fetch = ((url: string, init?: RequestInit) => {
    calls.push({ url: String(url), method: (init?.method || "GET").toUpperCase() });
    return Promise.resolve({
      ok: true,
      status: 200,
      text: () => Promise.resolve(JSON.stringify({ ok: true, row: null, cleared: [] })),
    } as unknown as Response);
  }) as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = realFetch;
  queryClient.removeQueries({ queryKey: ["instances"] });
});

describe("reset-stage wiring", () => {
  it("POSTs the press (a GET lands on the SPA fallback, not the route)", async () => {
    // quiet: the success toast is DOM-only and these tests run headless — the
    // same option the automatic post-Make-PR reset uses.
    await resetStage("win one", { quiet: true });
    expect(calls).toHaveLength(1);
    expect(calls[0].method).toBe("POST");
    // Title encoding comes from instApi; asserted here so a hand-built URL can
    // never creep back in.
    expect(calls[0].url).toBe("/api/instances/win%20one/reset-stage");
  });

  it("is one request per press — the action carries no undo call", async () => {
    await resetStage("win one", { quiet: true });
    await resetStage("win one", { quiet: true });
    expect(calls.map((c) => c.method)).toEqual(["POST", "POST"]);
  });
});
