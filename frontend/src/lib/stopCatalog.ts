/** The Stops shelf's regions and its catalog (docs/flights.md "Round 12" →
 * "The shelf"): the default blocks the shelf shows, the order the catalog
 * lists every kind in, the catalog's filter, and what a row's drag carries
 * (the same MIMEs as its chip on the shelf). Pure, so it is tested alone. */

import { STOP_MIME, type FlightPlan, type StepKindDef } from "./flights";
import { FLYWAY_MIME } from "./skyLayout";
import { kindName } from "../components/flights/StopParts";

/** The shelf's default blocks, after the Session chip, in this order. `you`
 * is shown as Perch. */
export const DEFAULT_KINDS = ["agent", "you", "ship", "verify", "wait"] as const;

/** The default blocks the server lists, in DEFAULT_KINDS order (a kind the
 * server does not list is skipped). */
export function defaultKinds<K extends Pick<StepKindDef, "kind">>(kinds: readonly K[]): K[] {
  const out: K[] = [];
  for (const k of DEFAULT_KINDS) {
    const def = kinds.find((d) => d.kind === k);
    if (def) out.push(def);
  }
  return out;
}

/** Every kind, for the catalog: the default blocks first (their order), then
 * the rest as the registry lists them. */
export function catalogKinds<K extends Pick<StepKindDef, "kind">>(kinds: readonly K[]): K[] {
  const first = defaultKinds(kinds);
  return [...first, ...kinds.filter((k) => !first.includes(k))];
}

/** The filter: every word of the query found (case-blind) in one of the
 * texts. An empty query matches everything. */
export function catalogMatch(query: string, ...texts: Array<string | undefined>): boolean {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return true;
  const hay = texts.filter(Boolean).join("\n").toLowerCase();
  return words.every((w) => hay.includes(w));
}

/** The kinds the filter keeps: by label (Perch for `you`), kind and summary. */
export function filterKinds<K extends Pick<StepKindDef, "kind" | "label" | "summary">>(kinds: readonly K[], query: string): K[] {
  return kinds.filter((k) => catalogMatch(query, kindName(k), k.label, k.kind, k.summary));
}

/** The saved stops the filter keeps: by name and description. */
export function filterPlans<P extends Pick<FlightPlan, "name" | "description">>(plans: readonly P[], query: string): P[] {
  return plans.filter((p) => catalogMatch(query, p.name, p.description));
}

/** A saved stop's sub-steps as one line: "a → b → c". */
export function planPath(plan: Pick<FlightPlan, "steps">): string {
  return plan.steps.map((s) => s.title).join(" → ");
}

/** What a catalog row adds (Add, Enter) — the shape the board's drop takes. */
export type CatalogDrop = { kind: string } | { planId: string };

/** The catalog's rows in the order ↑ / ↓ walk them: the kinds, then the
 * saved stops. */
export function catalogRows(kinds: readonly Pick<StepKindDef, "kind">[], plans: readonly Pick<FlightPlan, "id">[]): CatalogDrop[] {
  return [...kinds.map((k) => ({ kind: k.kind })), ...plans.map((p) => ({ planId: p.id }))];
}

/** What a stop chip's drag carries: the kind under STOP_MIME, and its name as
 * plain text for anything else it is dropped on. */
export function stopDragData(def: Pick<StepKindDef, "kind" | "label">): Array<[string, string]> {
  return [
    [STOP_MIME, def.kind],
    ["text/plain", kindName(def)],
  ];
}

/** What a saved stop's drag carries: its id under FLYWAY_MIME (the MIME the
 * canvas reads), its name as plain text. */
export function savedDragData(plan: Pick<FlightPlan, "id" | "name">): Array<[string, string]> {
  return [
    [FLYWAY_MIME, plan.id],
    ["text/plain", plan.name],
  ];
}
