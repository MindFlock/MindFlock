/** Duplicating a window files the copy directly beneath its source.
 *
 * The pure splice is covered in ordering.test.ts; this pins the wiring, which
 * is where the behaviour actually lived before: `copySession` created a
 * session the saved order had never seen, so `orderedInstances` filed it after
 * everything else and the copy appeared at the bottom of the rail.
 *
 * Both placements matter. The optimistic provisioning row has to land in place
 * too, or the row shows up at the bottom and then jumps a second later when
 * the server answers.
 */

import { describe, it, expect, beforeEach, vi } from "vitest";
import type { Instance } from "../api/types";

const instApi = vi.fn();

vi.mock("../api/client", () => ({
  api: vi.fn(async () => ({})),
  instApi: (...args: unknown[]) => instApi(...args),
}));

// The real one refetches over the network; the cache is seeded by hand here.
vi.mock("../state/queries", async (orig) => {
  const actual = (await orig()) as Record<string, unknown>;
  return { ...actual, refreshInstances: vi.fn(async () => {}) };
});

const { copySession } = await import("../lib/sessionActions");
const { queryClient } = await import("../state/queries");
const { useUi } = await import("../state/store");

const rows = (titles: string[]) =>
  queryClient.setQueryData<Instance[]>(
    ["instances"],
    titles.map((t) => ({ title: t }) as unknown as Instance)
  );

const order = () => useUi.getState().order;

describe("copySession placement", () => {
  beforeEach(() => {
    instApi.mockReset();
    useUi.getState().setOrder([]);
    rows([]);
  });

  it("puts the copy directly under its source, not at the end of the rail", async () => {
    rows(["alpha", "beta", "gamma"]);
    useUi.getState().setOrder(["alpha", "beta", "gamma"]);
    instApi.mockImplementation(async () => {
      // The server answers after its own row exists.
      rows(["alpha", "beta", "gamma", "beta-copy"]);
      return { title: "beta-copy" };
    });

    await copySession("beta");

    expect(order()).toEqual(["alpha", "beta", "beta-copy", "gamma"]);
  });

  it("places the optimistic provisioning row immediately, before the server answers", async () => {
    rows(["alpha", "beta", "gamma"]);
    useUi.getState().setOrder(["alpha", "beta", "gamma"]);
    let midFlight: string[] = [];
    instApi.mockImplementation(async () => {
      midFlight = [...order()];
      rows(["alpha", "beta", "gamma", "beta-copy"]);
      return { title: "beta-copy" };
    });

    await copySession("beta");

    // No bottom-of-the-list stop-over on the way.
    expect(midFlight).toEqual(["alpha", "beta", "beta-copy", "gamma"]);
  });

  it("places the title the SERVER chose when it differs from the guess", async () => {
    rows(["alpha", "beta", "gamma"]);
    useUi.getState().setOrder(["alpha", "beta", "gamma"]);
    instApi.mockImplementation(async () => {
      rows(["alpha", "beta", "gamma", "beta-copy-2"]);
      return { title: "beta-copy-2" };
    });

    await copySession("beta");

    // The optimistic guess is gone (it never became a session); the real one
    // sits under its source rather than at the bottom.
    expect(order()).toEqual(["alpha", "beta", "beta-copy-2", "gamma"]);
  });

  it("materializes a sparse saved order rather than splicing into a stale one", async () => {
    // Only one row was ever dragged, so `cs_order` holds one title. The copy
    // still has to land under `beta`, which the saved order has never heard of.
    rows(["alpha", "beta", "gamma"]);
    useUi.getState().setOrder(["gamma"]);
    instApi.mockImplementation(async () => {
      rows(["alpha", "beta", "gamma", "beta-copy"]);
      return { title: "beta-copy" };
    });

    await copySession("beta");

    expect(order()).toEqual(["gamma", "alpha", "beta", "beta-copy"]);
  });

  it("keeps saved slots for sessions missing from the current snapshot", async () => {
    // A paired laptop is asleep, so its rows are absent from /api/instances.
    // Duplicating something local must not forget where they sat — they would
    // come back at the bottom in server order, with nothing the user did to
    // explain it.
    rows(["alpha", "beta"]);
    useUi.getState().setOrder(["lap::one", "alpha", "lap::two", "beta"]);
    instApi.mockImplementation(async () => {
      rows(["alpha", "beta", "beta-copy"]);
      return { title: "beta-copy" };
    });

    await copySession("beta");

    expect(order()).toEqual(["lap::one", "alpha", "lap::two", "beta", "beta-copy"]);
  });

  it("does not place a title this browser has never seen", async () => {
    // A remote row's copy is answered by the device that owns it, under its own
    // BARE title — not the `device::title` the rail shows.
    rows(["alpha", "lap::foo"]);
    useUi.getState().setOrder(["alpha", "lap::foo"]);
    instApi.mockImplementation(async () => ({ title: "foo-copy" }));

    await copySession("lap::foo");

    expect(order()).not.toContain("foo-copy");
    // …and the optimistic guess it replaced is cleaned up too.
    expect(order()).not.toContain("lap::foo-copy");
  });

  it("leaves the order alone when the copy fails", async () => {
    rows(["alpha", "beta"]);
    useUi.getState().setOrder(["alpha", "beta"]);
    instApi.mockImplementation(async () => {
      throw new Error("nope");
    });
    // copySession alerts on failure; jsdom-free env has no alert.
    (globalThis as Record<string, unknown>).alert = () => {};

    await copySession("beta");

    // The pending row is dropped, and no ghost title is left in the order.
    expect(order()).not.toContain("beta-copy");
  });
});
