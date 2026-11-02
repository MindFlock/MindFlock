/** What a stop touches outside its worktree (docs/flights.md "Round 15", the
 * `looks` task): the kinds registry's `risk` tags, read off a step. A box
 * wears one 3px stripe on its left edge per tag (FlywayCanvas `.fl-risk`),
 * coloured by tone: `remote` the ship tone, `irreversible` the failed tone,
 * `spends` the perch gold. The tooltip names them in words.
 *
 * The kind's tags are the rule; a ship stop refines them by its lane: a
 * `merge` cannot be taken back (`irreversible` is added), a `commit` touches
 * nothing outside the worktree (no tags at all). An older server sends no
 * `risk`: ship and ticket are then `remote`, as the contract lists them. */

import type { FlightStep, StepKindDef } from "./flights";

export type RiskTag = "remote" | "irreversible" | "spends";

/** The tags in the order the stripe stacks them, top to bottom. */
export const RISK_TAGS: readonly RiskTag[] = ["remote", "irreversible", "spends"];

/** Each tag's sentence for the tooltip. */
export const RISK_WORD: Record<RiskTag, string> = {
  remote: "Touches the shared remote",
  irreversible: "Can't be undone",
  spends: "Spends money",
};

/** What the kinds say when the server sent no `risk` field. */
const FALLBACK: Record<string, readonly RiskTag[]> = { ship: ["remote"], ticket: ["remote"] };

const isTag = (v: unknown): v is RiskTag => v === "remote" || v === "irreversible" || v === "spends";

/** The stripe's tags for a step: the kind's, refined by a ship lane, in the
 * canonical order with no repeats. */
export function riskOf(step: Pick<FlightStep, "kind" | "config"> | null | undefined, kinds?: readonly StepKindDef[] | null): RiskTag[] {
  if (!step) return [];
  const def = kinds?.find((k) => k.kind === step.kind);
  const raw: readonly unknown[] = def ? (Array.isArray(def.risk) ? def.risk : []) : FALLBACK[step.kind] || [];
  const tags = new Set<RiskTag>(raw.filter(isTag));
  if (step.kind === "ship") {
    const lane = String((step.config || {}).lane || "");
    if (lane === "commit") tags.clear();
    else if (lane === "merge") tags.add("irreversible");
  }
  return RISK_TAGS.filter((t) => tags.has(t));
}

/** The tooltip: "Touches the shared remote · Can't be undone". */
export function riskTitle(tags: readonly RiskTag[]): string {
  return tags.map((t) => RISK_WORD[t]).join(" · ");
}
