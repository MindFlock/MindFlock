/** New boxes at once (docs/flights.md "As built: round 11 line"): an
 * optimistic row stands in for a session being made — in the grid, the rail
 * and the Sky — until the instances snapshot lists it (under its own title or
 * one the server numbered), `PENDING_TTL_MS` passes, or a
 * `session.create_failed` names it; `session.created` from anywhere makes one
 * (or gives a waiting guess its real title) and the session-set events
 * refetch, debounced. */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { code } from "./sourceText";
import type { Instance } from "../api/types";

vi.mock("../api/client", () => ({ api: vi.fn(() => new Promise(() => {})), instApi: vi.fn(() => new Promise(() => {})), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const q = await import("../state/queries");
const actions = await import("../lib/sessionActions");

const row = (title: string, over: Record<string, unknown> = {}) => ({ title, status: "running", ...over }) as unknown as Instance;
const pending = (title: string, at: number, base?: string) => row(title, { status: "loading", pending_create: true, pending_at: at, ...(base ? { pending_base: base } : {}) });
const titles = (rows: readonly Instance[]) => rows.map((r) => r.title);
const NOW = 1_000_000;

describe("mergePending: a fetch keeps what it doesn't list yet", () => {
  it("nothing pending: the fetched list itself", () => {
    const fetched = [row("a")];
    expect(q.mergePending(fetched, [row("a"), row("gone")], NOW)).toBe(fetched);
    expect(q.mergePending(fetched, undefined, NOW)).toBe(fetched);
  });

  it("a pending row survives a poll that does not list it, after the fetched rows", () => {
    const out = q.mergePending([row("a")], [row("a"), pending("new", NOW - 3000, "new")], NOW);
    expect(titles(out)).toEqual(["a", "new"]);
  });

  it("goes once the snapshot lists its title — one row, the server's", () => {
    const out = q.mergePending([row("a"), row("new")], [row("a"), pending("new", NOW, "new")], NOW);
    expect(titles(out)).toEqual(["a", "new"]);
    expect((out[1] as unknown as { pending_create?: boolean }).pending_create).toBeUndefined();
  });

  it("goes when the session arrives under a title numbered from its base", () => {
    const out = q.mergePending([row("foo"), row("foo-copy-2")], [row("foo"), pending("foo-copy", NOW, "foo-copy")], NOW);
    expect(titles(out)).toEqual(["foo", "foo-copy-2"]);
  });

  it("a session that was already there doesn't count as its arrival", () => {
    const out = q.mergePending([row("foo-copy-3")], [row("foo-copy-3"), pending("foo-copy", NOW, "foo-copy")], NOW);
    expect(titles(out)).toEqual(["foo-copy-3", "foo-copy"]);
  });

  it("each new title claims one waiting row", () => {
    const cached = [row("x"), pending("foo-copy", NOW, "foo-copy"), pending("foo-copy-2", NOW, "foo-copy")];
    const out = q.mergePending([row("x"), row("foo-copy-5")], cached, NOW);
    expect(titles(out)).toEqual(["x", "foo-copy-5", "foo-copy-2"]);
  });

  it("gives up after PENDING_TTL_MS (and a row with no clock at once)", () => {
    expect(q.PENDING_TTL_MS).toBe(20_000);
    expect(titles(q.mergePending([], [pending("slow", NOW - 19_000)], NOW))).toEqual(["slow"]);
    expect(titles(q.mergePending([], [pending("slow", NOW - 21_000)], NOW))).toEqual([]);
    expect(titles(q.mergePending([], [row("old", { pending_create: true })], NOW))).toEqual([]);
  });

  it("a row the create already landed (its real title, no base) waits for that title only", () => {
    const landed = row("bar-2", { pending_create: true, pending_at: NOW, program: "claude" });
    expect(titles(q.mergePending([row("bar-3")], [landed], NOW))).toEqual(["bar-3", "bar-2"]);
    expect(titles(q.mergePending([row("bar-2")], [landed], NOW))).toEqual(["bar-2"]);
  });

  it("numberedFrom: the base or base-N, nothing looser", () => {
    expect(q.numberedFrom("foo", "foo")).toBe(true);
    expect(q.numberedFrom("foo-2", "foo")).toBe(true);
    expect(q.numberedFrom("foo-bar", "foo")).toBe(false);
    expect(q.numberedFrom("foo-2x", "foo")).toBe(false);
    expect(q.numberedFrom("foobar-2", "foo")).toBe(false);
  });

  it("useInstances merges on every fetch, reading the cache after the request", () => {
    expect(code("state/queries.ts")).toContain('return mergePending(list, queryClient.getQueryData<Instance[]>(["instances"]), Date.now());');
    expect(code("state/queries.ts")).toContain("useEffect(bridgeSessionSetEvents, []);");
  });
});

describe("notePendingCreated: session.created", () => {
  it("a session made anywhere shows at once, with its parent", () => {
    const out = q.notePendingCreated([row("a")], "w1", { parent: "a", program: "claude" }, NOW);
    expect(out[1]).toEqual({ title: "w1", status: "loading", pending_create: true, pending_at: NOW, parent: "a", program: "claude" });
  });

  it("already a row: unchanged", () => {
    const rows = [row("a")];
    expect(q.notePendingCreated(rows, "a", {}, NOW)).toBe(rows);
  });

  it("a guess that was right forgets its base, so a sibling can't claim it", () => {
    const out = q.notePendingCreated([pending("foo-copy", NOW, "foo-copy")], "foo-copy", {}, NOW);
    expect(titles(out)).toEqual(["foo-copy"]);
    expect((out[0] as unknown as { pending_base?: string }).pending_base).toBeUndefined();
    expect(titles(q.mergePending([row("foo-copy-2")], out, NOW))).toEqual(["foo-copy-2", "foo-copy"]);
  });

  it("a waiting guess takes the real (numbered) title: one box, not two", () => {
    const out = q.notePendingCreated([row("foo"), pending("foo-copy", NOW - 500, "foo-copy")], "foo-copy-2", {}, NOW);
    expect(titles(out)).toEqual(["foo", "foo-copy-2"]);
    expect((out[1] as unknown as { pending_base?: string }).pending_base).toBeUndefined();
    // Its base is spent: a second create of the same base gets its own row.
    expect(titles(q.notePendingCreated(out, "foo-copy-3", {}, NOW))).toEqual(["foo", "foo-copy-2", "foo-copy-3"]);
  });
});

describe("landPendingRow: the create answered", () => {
  it("the guess becomes the answer's row, still waiting", () => {
    const out = q.landPendingRow([row("a"), pending("foo", NOW - 100, "foo")], "foo", row("foo-2", { program: "claude" }), NOW);
    expect(titles(out)).toEqual(["a", "foo-2"]);
    expect(out[1]).toMatchObject({ title: "foo-2", program: "claude", pending_create: true, pending_at: NOW });
  });

  it("an event already moved it: that row takes the answer, the guess goes", () => {
    const rows = [pending("foo", NOW, "foo"), pending("foo-2", NOW)];
    expect(titles(q.landPendingRow(rows, "foo", row("foo-2"), NOW))).toEqual(["foo-2"]);
  });

  it("the snapshot already lists it: the guess just goes", () => {
    const out = q.landPendingRow([row("foo-2"), pending("foo", NOW, "foo")], "foo", row("foo-2"), NOW);
    expect(titles(out)).toEqual(["foo-2"]);
    expect((out[0] as unknown as { pending_create?: boolean }).pending_create).toBeUndefined();
  });

  it("no guess left (a poll dropped it): the answer's row is added", () => {
    expect(titles(q.landPendingRow([row("a")], "foo", row("foo"), NOW))).toEqual(["a", "foo"]);
  });
});

describe("dropPendingRow: session.create_failed", () => {
  it("only the pending row of that title", () => {
    const rows = [row("a"), pending("b", NOW)];
    expect(titles(q.dropPendingRow(rows, "b"))).toEqual(["a"]);
    expect(q.dropPendingRow(rows, "a")).toBe(rows);
  });
});

describe("the session actions", () => {
  beforeEach(() => q.queryClient.setQueryData(["instances"], [row("foo")]));
  afterEach(() => q.queryClient.removeQueries({ queryKey: ["instances"] }));

  it("addPendingSession stamps its clock and its base", () => {
    const t = actions.addPendingSession("foo");
    expect(t).toBe("foo-2");
    const rows = q.queryClient.getQueryData<Instance[]>(["instances"])!;
    expect(rows[1]).toMatchObject({ title: "foo-2", pending_create: true, pending_base: "foo" });
    expect(typeof (rows[1] as unknown as { pending_at: number }).pending_at).toBe("number");
  });

  it("landPendingSession swaps in the answer", () => {
    const t = actions.addPendingSession("bar");
    actions.landPendingSession(t, row("bar-7"));
    expect(titles(q.queryClient.getQueryData<Instance[]>(["instances"])!)).toEqual(["foo", "bar-7"]);
  });

  it("a bare answer for another device's guess is not landed: the poll reconciles it", () => {
    const t = actions.addPendingSession("foo-copy", "lap");
    actions.landPendingSession(t, row("foo-copy"));
    expect(titles(q.queryClient.getQueryData<Instance[]>(["instances"])!)).toEqual(["foo", "lap::foo-copy"]);
  });

  it("copy, branch and reopen add a pending row and land the answer", () => {
    const src = code("lib/sessionActions.ts");
    expect(src.match(/landPendingSession\(guess, inst\);/g)?.length).toBe(3);
    expect(src).toContain('const guess = addPendingSession(ent.title || "session", "", true);');
    // A reopen keeps the name it was given.
    expect(src).toContain("if (!keepAlias) clearStaleAlias(title);");
  });
});

describe("the event bridge", () => {
  type Cb = (env: Record<string, unknown>) => void;
  const subs: Record<string, Cb[]> = {};
  const fire = (event: string, session: string, data: Record<string, unknown> = {}) =>
    (subs[event] || []).forEach((cb) => cb({ seq: 1, event, session, old: null, new: null, ts: 0, data }));

  beforeEach(() => {
    vi.useFakeTimers();
    const w = ((globalThis as Record<string, unknown>).window ??= {}) as Record<string, unknown>;
    w.mindflock = {
      events: {
        subscribe: (name: string, cb: Cb) => ((subs[name] ||= []).push(cb), () => {}),
        onStatus: () => () => {},
        isReplay: () => false,
        connected: true,
        lastSeq: 0,
      },
    };
    q.queryClient.setQueryData(["instances"], [row("a")]);
  });
  afterEach(() => {
    vi.useRealTimers();
    q.queryClient.removeQueries({ queryKey: ["instances"] });
  });

  it("subscribes once to created, create_failed, deleted and renamed", () => {
    q.bridgeSessionSetEvents();
    q.bridgeSessionSetEvents();
    for (const e of ["session.created", "session.create_failed", "session.deleted", "session.renamed"]) expect(subs[e]?.length).toBe(1);
  });

  it("session.created shows the box at once; create_failed takes it back; a burst refetches once", () => {
    q.bridgeSessionSetEvents();
    const inv = vi.spyOn(q.queryClient, "invalidateQueries");
    fire("session.created", "w1", { parent: "a" });
    expect(titles(q.queryClient.getQueryData<Instance[]>(["instances"])!)).toEqual(["a", "w1"]);
    // A verify run is a hidden helper: no box for it.
    fire("session.created", "verify-plan-1");
    expect(titles(q.queryClient.getQueryData<Instance[]>(["instances"])!)).toEqual(["a", "w1"]);
    fire("session.create_failed", "w1");
    expect(titles(q.queryClient.getQueryData<Instance[]>(["instances"])!)).toEqual(["a"]);
    fire("session.deleted", "a");
    fire("session.renamed", "a");
    expect(inv).not.toHaveBeenCalled();
    vi.advanceTimersByTime(150);
    expect(inv).toHaveBeenCalledTimes(1);
    expect(inv).toHaveBeenCalledWith({ queryKey: ["instances"] });
    inv.mockRestore();
  });

  it("flight.changed already refetches the flights", () => {
    expect(code("state/queries.ts")).toContain('for (const name of ["flight.changed", "flight.needs_you", "flight.finished"]) ev.subscribe(name, bump);');
  });
});
