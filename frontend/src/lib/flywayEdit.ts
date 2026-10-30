/** The rules behind the shelf's flyway rows (FlywayRow, FlywayShelf): which
 * order the rows come in, what a stop dropped on the empty row makes, how a
 * row edits its stops (every change saved at once, so each one is a whole
 * new list), what Undo puts back, and what a rename may be. Pure — the
 * components hold the state and the saving. */

import { blankPlan, newStep, type FlightPlan, type FlightStep, type StepKindDef } from "./flights";

/** A flyway's name, as the server keeps it (`_text(name, 120)`). */
export const NAME_MAX = 120;

/** The shelf's rows: built-ins first, then yours, each as listed. */
export function rowOrder<P extends Pick<FlightPlan, "builtin">>(plans: readonly P[]): P[] {
  return [...plans.filter((p) => p.builtin), ...plans.filter((p) => !p.builtin)];
}

/** "New flyway", or "New flyway 2", 3… — never a name already on the shelf. */
export function freshFlywayName(plans: readonly Pick<FlightPlan, "name">[], base = "New flyway"): string {
  const taken = new Set(plans.map((p) => p.name.trim().toLowerCase()));
  if (!taken.has(base.toLowerCase())) return base;
  let n = 2;
  while (taken.has((base + " " + n).toLowerCase())) n++;
  return base + " " + n;
}

/** A stop dropped on the empty row (or picked from its palette): a new manual
 * flyway with that one stop, named `name` when the owner typed one, else a
 * fresh "New flyway". Null for a kind the server doesn't list. */
export function flywayFromDrop(
  kind: string,
  kinds: readonly StepKindDef[],
  plans: readonly Pick<FlightPlan, "name">[],
  name = ""
): FlightPlan | null {
  const def = kinds.find((k) => k.kind === kind);
  if (!def) return null;
  const typed = name.trim().slice(0, NAME_MAX);
  return { ...blankPlan(), name: typed || freshFlywayName(plans), steps: [newStep(def, [])] };
}

export type RenameCheck = { ok: true; name: string } | { ok: false; error: string };

/** A rename: trimmed, never empty, at most NAME_MAX characters. The same name
 * is fine (nothing to save); one another flyway has is refused, so the shelf
 * never shows two rows you can't tell apart. */
export function checkRename(raw: string, plans: readonly Pick<FlightPlan, "id" | "name">[], selfId: string): RenameCheck {
  const name = raw.trim();
  if (!name) return { ok: false, error: "A flyway needs a name" };
  if (name.length > NAME_MAX) return { ok: false, error: "Keep the name under " + NAME_MAX + " characters" };
  const clash = plans.find((p) => p.id !== selfId && p.name.trim().toLowerCase() === name.toLowerCase());
  if (clash) return { ok: false, error: "Another flyway is already called “" + clash.name.trim() + "”" };
  return { ok: true, name };
}

/** A stop of `def` put in at `at` (null, or past the end: the end). */
export function insertStop(steps: readonly FlightStep[], def: StepKindDef, at: number | null): FlightStep[] {
  const out = steps.slice();
  const i = at === null ? out.length : Math.max(0, Math.min(out.length, at));
  out.splice(i, 0, newStep(def, steps.map((s) => s.id)));
  return out;
}

/** Whether a stop can come off: the server keeps a flyway at one stop at
 * least, so the last one goes with the flyway (its ⋯ → Delete). */
export function canRemoveStop(steps: readonly unknown[]): boolean {
  return steps.length > 1;
}

/** Undo of a removed stop: the same stop back at its old place (or the end,
 * when the line has got shorter), under a fresh id if its own was taken
 * meanwhile. */
export function restoreStep(steps: readonly FlightStep[], step: FlightStep, at: number): FlightStep[] {
  const taken = steps.map((s) => s.id);
  let id = step.id;
  if (taken.includes(id)) {
    let n = steps.length + 1;
    while (taken.includes("s" + n)) n++;
    id = "s" + n;
  }
  const out = steps.slice();
  out.splice(Math.max(0, Math.min(out.length, at)), 0, { ...step, id, config: { ...step.config } });
  return out;
}

/** A flyway copied as one of yours, under a fresh "<name> copy" name. */
export function copyOfFlyway(plan: FlightPlan, plans: readonly Pick<FlightPlan, "name">[]): FlightPlan {
  const copy = JSON.parse(JSON.stringify(plan)) as FlightPlan;
  return { ...copy, id: "", builtin: false, updated: 0, name: freshFlywayName(plans, plan.name.trim() + " copy") };
}

/** A deleted flyway brought back by Undo: the same flyway saved again (the
 * server mints it a new id; a built-in is never deleted, only reset). */
export function undeleted(plan: FlightPlan): FlightPlan {
  return { ...(JSON.parse(JSON.stringify(plan)) as FlightPlan), id: "", builtin: false, updated: 0 };
}
