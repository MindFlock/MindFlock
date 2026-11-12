/** Stability sweep (2026-11-12): a refused optimistic edit puts back only
 * what IT changed. It used to restore the whole flights list as it was before
 * the press, so a poll that landed meanwhile (another route moved on) or a
 * second press (another block deleted at once) was undone with it — the other
 * deleted block came back, a finished route showed running again — until the
 * next refetch. */
import { afterEach, describe, expect, it } from "vitest";
import type { Flight, FlightStep } from "../lib/flights";
import { optimistic, optimisticDelete, withoutSteps } from "../lib/flightOptimism";
import { queryClient } from "../state/queries";

const step = (id: string): FlightStep => ({ id, kind: "agent", title: id, config: {}, on_fail: "halt" });
const flight = (id: string, state: Flight["state"] = "running"): Flight =>
  ({ id, plan_id: "", plan_name: id, steps: ["s1", "s2", "s3"].map(step), state, cursor: 0, step_states: {}, session: id }) as unknown as Flight;

const KEY = ["flights"];
const cache = () => queryClient.getQueryData<Flight[]>(KEY) || [];

/** A request the test answers by hand. */
function pending() {
  let answer: (ok: boolean) => void = () => {};
  const p = new Promise<boolean>((r) => (answer = r));
  return { request: () => p, answer };
}

afterEach(() => queryClient.removeQueries({ queryKey: KEY }));

describe("a refused optimistic edit rolls back only its own change", () => {
  it("a poll that landed meanwhile is kept", async () => {
    queryClient.setQueryData(KEY, [flight("a"), flight("b")]);
    const req = pending();
    const done = optimistic("a", withoutSteps(new Set(["s3"])), req.request);
    expect(cache()[0].steps).toHaveLength(2);
    // The poll: b finished meanwhile; a is still as we guessed it.
    queryClient.setQueryData<Flight[]>(KEY, (fs) => (fs || []).map((f) => (f.id === "b" ? { ...f, state: "done" } : f)));
    req.answer(false);
    expect(await done).toBe(false);
    expect(cache().find((f) => f.id === "a")?.steps).toHaveLength(3);
    expect(cache().find((f) => f.id === "b")?.state).toBe("done");
  });

  it("a server copy that replaced the guess is not overwritten", async () => {
    queryClient.setQueryData(KEY, [flight("a")]);
    const req = pending();
    const done = optimistic("a", withoutSteps(new Set(["s3"])), req.request);
    queryClient.setQueryData(KEY, [{ ...flight("a"), cursor: 2 }]);
    req.answer(false);
    await done;
    expect(cache()[0].cursor).toBe(2);
    expect(cache()[0].steps).toHaveLength(3);
  });

  it("two deletes at once: the refused one comes back, the other stays gone", async () => {
    queryClient.setQueryData(KEY, [flight("a"), flight("b"), flight("c")]);
    const ra = pending();
    const rb = pending();
    const da = optimisticDelete("a", ra.request);
    const db = optimisticDelete("b", rb.request);
    expect(cache().map((f) => f.id)).toEqual(["c"]);
    ra.answer(false);
    await da;
    expect(cache().map((f) => f.id)).toEqual(["a", "c"]);
    rb.answer(true);
    await db;
    expect(cache().map((f) => f.id)).toEqual(["a", "c"]);
  });

  it("a refused delete after another one was refused puts both back in place", async () => {
    queryClient.setQueryData(KEY, [flight("a"), flight("b"), flight("c")]);
    const ra = pending();
    const rb = pending();
    const da = optimisticDelete("a", ra.request);
    const db = optimisticDelete("b", rb.request);
    rb.answer(false);
    await db;
    ra.answer(false);
    await da;
    expect(cache().map((f) => f.id)).toEqual(["a", "b", "c"]);
  });

  it("a throw rolls back the same way and rethrows", async () => {
    queryClient.setQueryData(KEY, [flight("a"), flight("b")]);
    const p = optimisticDelete("a", () => Promise.reject(new Error("net")));
    queryClient.setQueryData<Flight[]>(KEY, (fs) => (fs || []).map((f) => (f.id === "b" ? { ...f, state: "done" } : f)));
    await expect(p).rejects.toThrow("net");
    expect(cache().map((f) => f.id + ":" + f.state)).toEqual(["a:running", "b:done"]);
  });
});
