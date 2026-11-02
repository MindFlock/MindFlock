/** Right-click on empty sky → add a block by search (components/flights/QuickAdd). */
import { describe, expect, it } from "vitest";
import type { FlightPlan, StepKindDef } from "../lib/flights";
import { quickRows } from "../components/flights/QuickAdd";

const K = (kind: string, label: string, summary = "", family = "work") => ({ kind, label, summary, family, actor: "robot", fields: [] }) as unknown as StepKindDef;
const kinds = [K("agent", "Agent", "The agent works"), K("ship", "Ship", "Commit, push, PR", "ship"), K("notify", "Notify me", "A ping", "people")];
const plans = [{ id: "p1", name: "Ticket to merge", steps: [{ id: "s1", kind: "agent", title: "Work it" }] }] as unknown as FlightPlan[];

describe("quickRows", () => {
  it("lists every kind then every saved stop when empty", () => {
    expect(quickRows(kinds, plans, "").map((r) => r.label)).toEqual(["Agent", "Ship", "Notify me", "Ticket to merge"]);
  });
  it("matches every word against label, summary, kind and family", () => {
    expect(quickRows(kinds, plans, "push").map((r) => r.label)).toEqual(["Ship"]);
    expect(quickRows(kinds, plans, "ping me").map((r) => r.label)).toEqual(["Notify me"]);
    expect(quickRows(kinds, plans, "work it").map((r) => r.label)).toEqual(["Ticket to merge"]);
    expect(quickRows(kinds, plans, "zzz")).toEqual([]);
  });
  it("a saved stop picks its plan, a kind its kind", () => {
    expect(quickRows(kinds, plans, "ticket")[0].pick).toEqual({ plan: plans[0] });
    expect(quickRows(kinds, plans, "agent")[0].pick).toEqual({ kind: "agent" });
  });
});
