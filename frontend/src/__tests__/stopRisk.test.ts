/** Round 15 looks — the risk stripe (lib/stopRisk): a stop's tags are its
 * kind's `risk`, a ship stop's lane refines them (merge adds irreversible,
 * commit has none), an older server without `risk` still marks ship and
 * ticket remote, the tooltip names the tags, and the diagram carries them
 * onto the boxes (a group wears the union of its sub-steps'). */

import { describe, expect, it } from "vitest";
import type { Flight, FlightStep, StepKindDef } from "../lib/flights";
import { RISK_TAGS, RISK_WORD, riskOf, riskTitle } from "../lib/stopRisk";
import { flightStops, flywayDiagram, groupNode } from "../lib/flightDiagram";

const def = (kind: string, risk?: string[]): StepKindDef => ({ kind, label: kind, actor: "robot", summary: "", fields: [], default_title: kind, ...(risk ? { risk } : {}) });
const KINDS: StepKindDef[] = [def("agent"), def("ship", ["remote"]), def("ticket", ["remote"]), def("land", ["irreversible"]), def("call", ["remote"]), def("budget", []), def("odd", ["remote", "weird", "remote", "spends"])];
const step = (id: string, kind: string, config: Record<string, unknown> = {}): FlightStep => ({ id, kind, title: kind, config, on_fail: "halt" });

describe("riskOf", () => {
  it("is the kind's risk, in the canonical order, with no repeats or unknown words", () => {
    expect(riskOf(step("a", "agent"), KINDS)).toEqual([]);
    expect(riskOf(step("c", "call"), KINDS)).toEqual(["remote"]);
    expect(riskOf(step("l", "land"), KINDS)).toEqual(["irreversible"]);
    expect(riskOf(step("o", "odd"), KINDS)).toEqual(["remote", "spends"]);
    expect(riskOf(step("b", "budget"), KINDS)).toEqual([]);
    expect(RISK_TAGS).toEqual(["remote", "irreversible", "spends"]);
  });

  it("a ship stop by its lane: merge can't be undone, commit touches nothing outside", () => {
    expect(riskOf(step("s", "ship", { lane: "push" }), KINDS)).toEqual(["remote"]);
    expect(riskOf(step("s", "ship", { lane: "pr" }), KINDS)).toEqual(["remote"]);
    expect(riskOf(step("s", "ship", { lane: "merge" }), KINDS)).toEqual(["remote", "irreversible"]);
    expect(riskOf(step("s", "ship", { lane: "commit" }), KINDS)).toEqual([]);
  });

  it("an older server without `risk`: ship and ticket are remote, the rest nothing", () => {
    expect(riskOf(step("s", "ship", { lane: "push" }), null)).toEqual(["remote"]);
    expect(riskOf(step("s", "ship", { lane: "merge" }), [def("ship")])).toEqual(["irreversible"]);
    expect(riskOf(step("t", "ticket"), undefined)).toEqual(["remote"]);
    expect(riskOf(step("a", "agent"), [])).toEqual([]);
    expect(riskOf(null, KINDS)).toEqual([]);
  });

  it("the tooltip names the tags in words", () => {
    expect(riskTitle(["remote", "irreversible"])).toBe(RISK_WORD.remote + " · " + RISK_WORD.irreversible);
    expect(riskTitle(["spends"])).toBe("Spends money");
    expect(riskTitle([])).toBe("");
  });
});

describe("on the diagram", () => {
  const steps = [step("s1", "agent"), step("s2", "ship", { lane: "merge" }), step("s3", "ship", { lane: "commit" })];

  it("a flyway's and a flight's stop boxes carry `risk` only when they have tags", () => {
    const d = flywayDiagram(steps, KINDS);
    const by = (id: string) => d.nodes.find((n) => n.id === "stop:" + id)!;
    expect(by("s1").risk).toBeUndefined();
    expect(by("s2").risk).toEqual(["remote", "irreversible"]);
    expect(by("s3").risk).toBeUndefined();
    const f = { id: "f", steps, cursor: 0, state: "running", step_states: {}, reason: "", history: [] } as unknown as Flight;
    const live = flightStops(f, KINDS);
    expect(live.map((n) => n.risk)).toEqual([undefined, ["remote", "irreversible"], undefined]);
  });

  it("a group box wears the union of its sub-steps' tags", () => {
    const d = flywayDiagram([step("a", "agent"), step("b", "ship", { lane: "push" }), step("c", "land")], KINDS);
    const run = d.nodes.filter((n) => n.kind === "stop");
    const g = groupNode("group:g", { id: "g", name: "Ship it" }, run);
    expect(g.risk).toEqual(["remote", "irreversible"]);
    expect(groupNode("group:h", { id: "h", name: "Think" }, run.slice(0, 1)).risk).toBeUndefined();
  });
});
