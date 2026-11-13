/** The UI model's half of fixtures/verify_shared.json — the server evaluates
 * the same plans in tests/unit/test_verify_shared_fixture.py. verify.ts
 * repeats the store's rules (`latestRun` ~ `_current_run`, `openHumanSteps` ~
 * `open_human_steps`); a plan the two answer differently about is a defect. */

import { describe, expect, it } from "vitest";
import fixtureRaw from "./fixtures/verify_shared.json?raw";
import { latestRun, openHumanSteps } from "../components/dialogs/verify";
import type { TestPlan } from "../api/types";

interface Case {
  name: string;
  plan: TestPlan;
  current: number | null;
  open_human: string[];
}

const cases = (JSON.parse(fixtureRaw) as { cases: Case[] }).cases;

describe("verify.ts agrees with the server about the shared fixture", () => {
  for (const c of cases) {
    it(c.name, () => {
      const run = latestRun(c.plan);
      expect(run ? c.plan.runs.indexOf(run) : null).toBe(c.current);
      expect(openHumanSteps(c.plan).map((s) => s.id)).toEqual(c.open_human);
    });
  }
});
