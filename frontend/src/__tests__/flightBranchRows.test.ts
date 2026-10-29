import { describe, expect, it } from "vitest";
import { normalizeFlightRows } from "../lib/flights";
import { branchOf } from "../lib/flightDiagram";

const real = { id: "f_1", plan_name: "Ticket to merge", cursor: 2, total: 9, state: "running", step: { id: "s3", kind: "ship", title: "Push", state: "running", actor: "robot" } } as any;

describe("normalizeFlightRows", () => {
  it("lifts a branch-only dto out of `flight` so the row reads as having no flight", () => {
    const rows: any[] = normalizeFlightRows<any>([
      { title: "w1", flight: { id: "", plan_name: "Ticket to merge", cursor: 0, total: 0, state: "", step: null, join: null, branch: { parent: "p", step: "s6" } } as any },
      { title: "p", flight: real },
      { title: "plain", flight: null },
    ]);
    expect(rows[0].flight).toBeNull();
    expect(rows[0].flight_branch).toEqual({ parent: "p", step: "s6" });
    expect(rows[1].flight).toBe(real);
    expect(rows[2].flight).toBeNull();
    expect(rows[2].flight_branch).toBeUndefined();
  });
  it("branchOf reads the lifted field first and the raw dto second", () => {
    expect(branchOf({ flight: null, flight_branch: { parent: "p", step: "s6" } } as any)).toEqual({ parent: "p", step: "s6" });
    expect(branchOf({ flight: { ...real, branch: { parent: "p", step: "s2" } } } as any)).toEqual({ parent: "p", step: "s2" });
    expect(branchOf({ flight: real } as any)).toBeNull();
  });
});
