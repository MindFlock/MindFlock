/* Round 21 "r21-focus": This session's pick with nothing focused, the
 * route fit (48 px a side, 60–150 %), the minimap folding while it would
 * cover a route, and the "Needs you →" edge chip for a route a fit clips. */
import { describe, expect, it } from "vitest";
import type { Instance } from "../api/types";
import { ROUTE_FIT_MAX, ROUTE_FIT_MIN, ROUTE_FIT_PAD, fitRoute, pickThisSession, screenBoxCovers } from "../lib/skyLayout";
import { NEEDS_CLIP, OffscreenHints, needsYouHints, needsYouWord, type MinimapRect } from "../components/flights/Minimap";

const row = (title: string, o: Partial<Instance> = {}): Instance =>
  ({
    title,
    status: "running",
    started: true,
    program: "claude",
    provider: "claude",
    hidden_by: "",
    activity: "idle",
    activity_since: 0,
    flight: null,
    ...o,
  }) as unknown as Instance;
const working = (title: string, since = 0) => row(title, { activity: "working", activity_since: since } as Partial<Instance>);
const flightRow = (state: string) => ({ id: "f", plan_name: "p", cursor: 1, total: 3, state, step: { id: "s2", kind: "you", title: "Approve the diff", state: state === "halted" ? "failed" : "waiting_you", actor: "you" } });

describe("pickThisSession: what This session shows with nothing focused", () => {
  it("the first that needs you, in the bell's order, beats everything", () => {
    const rows = [working("a"), row("b"), row("c")];
    expect(pickThisSession(rows, { needsYou: ["c", "b"], mru: ["a"], railOrder: ["a", "b", "c"] })).toBe("c");
  });
  it("a route waiting on you or halted needs you even before the bell lists it", () => {
    const rows = [working("a"), row("b", { flight: flightRow("halted") } as Partial<Instance>), row("c")];
    expect(pickThisSession(rows, { mru: ["a"] })).toBe("b");
    const rows2 = [working("a"), row("w", { flight: flightRow("waiting_you") } as Partial<Instance>)];
    expect(pickThisSession(rows2, { mru: ["a"] })).toBe("w");
  });
  it("else the most recent WORKING session: the MRU's order first, then the latest to start working", () => {
    const rows = [working("old", 100), working("new", 200), row("idle")];
    expect(pickThisSession(rows, { mru: ["idle", "old"] })).toBe("old");
    expect(pickThisSession(rows, { mru: ["idle"] })).toBe("new");
  });
  it("else the MRU head, else the rail's #1, else the newest row", () => {
    const rows = [row("a"), row("b"), row("c")];
    expect(pickThisSession(rows, { mru: ["b"], railOrder: ["c"] })).toBe("b");
    expect(pickThisSession(rows, { mru: [], railOrder: ["\u0000window:x", "c", "a"] })).toBe("c");
    expect(pickThisSession(rows, {})).toBe("c");
  });
  it("only rows the sky draws count; none gives \"\"", () => {
    const rows = [row("hid", { hidden_by: "x" } as Partial<Instance>), row("shown")];
    expect(pickThisSession(rows, { needsYou: ["hid"], mru: ["hid"] })).toBe("shown");
    expect(pickThisSession([], { mru: ["x"] })).toBe("");
    expect(pickThisSession([row("gone")], { needsYou: ["closed"], mru: ["closed"], railOrder: ["closed"] })).toBe("gone");
  });
});

describe("fitRoute: This session's fit", () => {
  const VW = 1148;
  const VH = 780;
  it("fits the route to the window less 48 px a side, centred", () => {
    const c = fitRoute({ x: 56, y: 56, w: 1400, h: 200 }, VW, VH);
    expect(c.z).toBeCloseTo((VW - 2 * ROUTE_FIT_PAD) / 1400, 6);
    const left = c.x + 56 * c.z;
    expect(left).toBeCloseTo(ROUTE_FIT_PAD, 6);
    expect(left + 1400 * c.z).toBeCloseTo(VW - ROUTE_FIT_PAD, 6);
  });
  it("never closer than 150 % (a short route doesn't balloon)", () => {
    expect(fitRoute({ x: 0, y: 0, w: 200, h: 100 }, VW, VH).z).toBe(ROUTE_FIT_MAX);
    expect(ROUTE_FIT_MAX).toBe(1.5);
  });
  it("never further than 60 %; a route still wider then starts at its head (left edge)", () => {
    const c = fitRoute({ x: 56, y: 56, w: 4000, h: 200 }, VW, VH);
    expect(c.z).toBe(ROUTE_FIT_MIN);
    expect(ROUTE_FIT_MIN).toBe(0.6);
    expect(c.x + 56 * c.z).toBeCloseTo(ROUTE_FIT_PAD, 6);
  });
  it("its height counts too (an open box makes a tall route)", () => {
    const c = fitRoute({ x: 0, y: 0, w: 600, h: 900 }, VW, VH);
    expect(c.z).toBeCloseTo((VH - 2 * ROUTE_FIT_PAD) / 900, 6);
  });
  it("nothing to fit, or no window yet: 100 % at the origin", () => {
    expect(fitRoute(null, VW, VH)).toEqual({ x: 0, y: 0, z: 1 });
    expect(fitRoute({ x: 0, y: 0, w: 100, h: 100 }, 0, 0)).toEqual({ x: 0, y: 0, z: 1 });
  });
});

describe("the minimap folds while it would cover a route", () => {
  const map = { x: 940, y: 600, w: 182, h: 150 };
  it("covers a route whose screen box overlaps it, under the camera", () => {
    const route = { x: 800, y: 500, w: 400, h: 200 };
    expect(screenBoxCovers(map, [route], { x: 0, y: 0, z: 1 })).toBe(true);
    // Zoomed out to half, the same route sits clear of the corner.
    expect(screenBoxCovers(map, [route], { x: 0, y: 0, z: 0.5 })).toBe(false);
    expect(screenBoxCovers(map, [], { x: 0, y: 0, z: 1 })).toBe(false);
  });
});

describe("the Needs you edge chip", () => {
  const VIEW = { w: 1000, h: 700 };
  const line = (id: string, x: number, y: number, w: number, needsYou = false): MinimapRect => ({ id, x, y, w, h: 120, kind: "line", label: id, needsYou });
  it("a route that needs you and runs past the right edge gets a chip there; one that doesn't, none", () => {
    const hints = needsYouHints([line("pay", 700, 200, 600, true), line("other", 700, 400, 600)], { x: 0, y: 0, z: 1 }, VIEW);
    expect(hints).toHaveLength(1);
    expect(hints[0]).toMatchObject({ id: "pay", edge: "right" });
    expect(needsYouWord("right")).toBe("Needs you →");
    expect(needsYouWord("left")).toBe("← Needs you");
  });
  it("a little clipping (≤ NEEDS_CLIP px) is not clipped; wholly off screen is edgeHints' own", () => {
    expect(needsYouHints([line("pay", 1000 - 500 + NEEDS_CLIP - 1, 0, 500, true)], { x: 0, y: 0, z: 1 }, VIEW)).toHaveLength(0);
    expect(needsYouHints([line("pay", 1200, 0, 500, true)], { x: 0, y: 0, z: 1 }, VIEW)).toHaveLength(0);
  });
  it("OffscreenHints draws it with the words, even at the far zoom where other chips hide", () => {
    const tree = OffscreenHints({ rects: [line("pay", 3000, 100, 500, true), line("idle", 3000, 300, 500)], cam: { x: 0, y: 0, z: 0.4 }, viewport: VIEW, onJump: () => {} });
    const s = JSON.stringify(tree, (k, v) => (k === "_owner" || k === "_store" ? undefined : v));
    expect(s).toContain("Needs you →");
    expect(s).toContain('"data-id":"pay"');
    expect(s).not.toContain('"data-id":"idle"');
  });
});
