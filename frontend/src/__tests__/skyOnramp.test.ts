/** Round-consolidate's Sky fixes (ux-lab round-consolidate judge + inventory):
 * the landing strip and the other right-column objects make way for a chain
 * that grows into them (lib/skyLayout `clearRightColumn`), a chain just built
 * on a session draws before the instances snapshot catches up
 * (`withLiveFlights`), and the icon families' fallback is the server's. */

import { describe, expect, it } from "vitest";
import { code } from "./sourceText";
import { clearRightColumn, SKY_GAP, withLiveFlights } from "../lib/skyLayout";
import { kindFamily } from "../components/flights/StopIcons";
import type { Flight, FlightRow } from "../lib/flights";

describe("the right column keeps clear of the lines", () => {
  const landing = { id: "landing", x: 1000, y: 40, w: 300, h: 240 };
  const device = { id: "device:mini", x: 1000, y: 312, w: 200, h: 64 };

  it("nothing in the way: nothing moves", () => {
    expect(clearRightColumn([landing, device], [{ x: 400, y: 40, w: 500, h: 200 }])).toEqual({});
    expect(clearRightColumn([], [{ x: 400, y: 40, w: 900, h: 200 }])).toEqual({});
    expect(clearRightColumn([landing], [])).toEqual({});
  });

  it("a chain that grew under the strip pushes the whole column right of everything, each keeping its height", () => {
    // The chain grew from 500 to 700 wide: its right edge (1100) runs under the strip.
    const lines = [
      { x: 400, y: 40, w: 700, h: 200 },
      { x: 40, y: 40, w: 300, h: 320 },
    ];
    expect(clearRightColumn([landing, device], lines)).toEqual({
      landing: { x: 1100 + SKY_GAP, y: 40 },
      "device:mini": { x: 1100 + SKY_GAP, y: 312 },
    });
  });

  it("closer than half a gap counts as in the way", () => {
    expect(clearRightColumn([landing], [{ x: 400, y: 40, w: 600 - SKY_GAP / 2 + 2, h: 200 }])).not.toEqual({});
    expect(clearRightColumn([landing], [{ x: 400, y: 40, w: 600 - SKY_GAP / 2, h: 200 }])).toEqual({});
  });

  it("SkyView moves only the auto-placed objects (never one the owner dragged), and keeps the new spot", () => {
    const v = code("components/flights/SkyView.tsx");
    expect(v).toContain("const column: PlacedLine[] = rightItems.filter((r) => autoPos[r.id] && !layout[r.id]?.pinned && place[r.id])");
    expect(v).toMatch(/for \(const \[id, at\] of Object\.entries\(clearRightColumn\(column, lineRects\)\)\) \{\s*fresh\[id\] = at;\s*place\[id\] = at;/);
  });
});

describe("a chain just built draws at once", () => {
  const flight = (over: Partial<Flight> = {}): Flight =>
    ({
      id: "f_1",
      plan_id: "",
      plan_name: "Route on me",
      steps: [{ id: "s1", kind: "verify", title: "Verify", config: {}, on_fail: "halt" }],
      subject: {},
      session: "me",
      repo_root: "",
      branch: "",
      state: "ready",
      cursor: 0,
      step_states: {},
      reason: "",
      history: [],
      started: 10,
      updated: 10,
      finished: 0,
      by: "user",
      ...over,
    }) as Flight;
  const row = (title: string, f: FlightRow | null = null) => ({ title, flight: f });

  it("a row with no flight yet takes the one the flights list already has for it", () => {
    const rows = [row("me"), row("other")];
    const out = withLiveFlights(rows, [flight()], [{ kind: "verify", actor: "you" }]);
    expect(out).not.toBe(rows);
    expect(out[0].flight).toMatchObject({ id: "f_1", plan_name: "Route on me", cursor: 0, total: 1, state: "ready", step: { id: "s1", kind: "verify", title: "Verify", state: "pending", actor: "you" } });
    expect(out[1]).toBe(rows[1]);
  });

  it("the newest shown flight wins; a finished, grounded or free one never stands in", () => {
    const out = withLiveFlights([row("me")], [flight({ id: "old", started: 1 }), flight({ id: "new", started: 20, state: "running" })]);
    expect(out[0].flight?.id).toBe("new");
    const rows = [row("me")];
    expect(withLiveFlights(rows, [flight({ state: "done" })])).toBe(rows);
    expect(withLiveFlights(rows, [flight({ state: "grounded" })])).toBe(rows);
    expect(withLiveFlights(rows, [flight({ free: true })])).toBe(rows);
    expect(withLiveFlights(rows, null)).toBe(rows);
  });

  it("a row that already names its flight is left as the server says", () => {
    const own = { id: "f_0", plan_name: "x", cursor: 0, total: 2, state: "running", step: { id: "a", kind: "agent", title: "A", state: "running", actor: "robot" } } as FlightRow;
    const rows = [row("me", own)];
    expect(withLiveFlights(rows, [flight()])).toBe(rows);
  });

  it("SkyView draws from the overlaid rows", () => {
    expect(code("components/flights/SkyView.tsx")).toContain("const rows = useMemo(() => withLiveFlights(instQ.data || NO_ROWS, flights, kinds), [instQ.data, flights, kinds]);");
  });
});

describe("the icon families agree with the server", () => {
  it("each kind's fallback family is the server's (backend core/flights.py _FAMILY + flight_kinds specs)", () => {
    const server: Record<string, string> = {
      agent: "work", split: "work", join: "work", ship: "ship", verify: "people", you: "people", wait: "outside", reviews: "ship", checks: "ship",
      command: "work", ticket: "outside", notify: "outside", budget: "outside", call: "outside", checkpoint: "ship", handoff: "flow", judge: "work",
      land: "ship", listen: "outside", loop: "work", preview: "outside", review: "work", screenshot: "outside", someone: "people", sync: "ship", test: "work",
    };
    for (const [k, fam] of Object.entries(server)) expect(kindFamily(k), k).toBe(fam);
  });
});
