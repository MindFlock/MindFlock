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

/** The saved stops the filter keeps: by name, description and their steps'
 * titles (a search for "push" finds the stops that push). */
export function filterPlans<P extends Pick<FlightPlan, "name" | "description"> & { steps?: FlightPlan["steps"] }>(plans: readonly P[], query: string): P[] {
  return plans.filter((p) => catalogMatch(query, p.name, p.description, ...(p.steps || []).map((s) => s.title)));
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

// --- What sits on the shelf (the owner's pick) ------------------------------

/** The browser key the pick lives under (per device, like the layout). */
export const SHELF_KEY = "mf_sky_shelf";

/** The blocks on the shelf: kinds, and saved stops by id. The default is the
 * five default blocks and NO multi-step block — those wait in the catalog. */
export interface ShelfPick {
  kinds: string[];
  plans: string[];
}

export const defaultShelf = (): ShelfPick => ({ kinds: [...DEFAULT_KINDS], plans: [] });

/** A stored pick, or the default when there is none or it is malformed. */
export function readShelf(raw: string | null | undefined): ShelfPick {
  if (!raw) return defaultShelf();
  try {
    const v = JSON.parse(raw) as Partial<ShelfPick> | null;
    const list = (x: unknown) => (Array.isArray(x) ? x.filter((k): k is string => typeof k === "string") : null);
    const kinds = list(v?.kinds);
    const plans = list(v?.plans);
    if (!kinds || !plans) return defaultShelf();
    return { kinds, plans };
  } catch {
    return defaultShelf();
  }
}

export const onShelf = (pick: ShelfPick, d: CatalogDrop): boolean => ("kind" in d ? pick.kinds.includes(d.kind) : pick.plans.includes(d.planId));

/** The pick with `d` put on or taken off (Take off the shelf / On the shelf). */
export function toggleShelf(pick: ShelfPick, d: CatalogDrop): ShelfPick {
  if ("kind" in d) {
    const on = pick.kinds.includes(d.kind);
    return { ...pick, kinds: on ? pick.kinds.filter((k) => k !== d.kind) : [...pick.kinds, d.kind] };
  }
  const on = pick.plans.includes(d.planId);
  return { ...pick, plans: on ? pick.plans.filter((k) => k !== d.planId) : [...pick.plans, d.planId] };
}

/** The shelf's kind chips: the pick's kinds the server lists, in the pick's
 * order (a kind the server dropped is skipped). */
export function shelfKinds<K extends Pick<StepKindDef, "kind">>(pick: ShelfPick, kinds: readonly K[]): K[] {
  const by = new Map(kinds.map((k) => [k.kind, k]));
  return pick.kinds.map((k) => by.get(k)).filter((k): k is K => !!k);
}

/** The shelf's saved stops: the ones picked, in the shelf's usual order. */
export function shelfPlans<P extends Pick<FlightPlan, "id">>(pick: ShelfPick, plans: readonly P[]): P[] {
  return plans.filter((p) => pick.plans.includes(p.id));
}

// --- The big catalog's cards (round 13) -------------------------------------

/** A kind's card: its icon, name, a robot / you badge, its summary. */
export interface KindCard<K> {
  section: "blocks";
  key: string;
  drop: { kind: string };
  name: string;
  perch: boolean;
  def: K;
}

/** A saved stop's card: its step boxes, name, built-in badge, "a → b → c". */
export interface PlanCard<P> {
  section: "multi";
  key: string;
  drop: { planId: string };
  name: string;
  builtin: boolean;
  path: string;
  plan: P;
}

export type CatalogCard<K, P> = KindCard<K> | PlanCard<P>;

/** The catalog's cards for a filter: Blocks (every kind, the defaults first),
 * Multi-step (the saved stops, in the order given), and `all` in the order
 * the arrows walk them. */
export function catalogCards<K extends Pick<StepKindDef, "kind" | "label" | "summary" | "actor">, P extends Pick<FlightPlan, "id" | "name" | "description" | "builtin" | "steps">>(
  kinds: readonly K[],
  plans: readonly P[],
  query: string
): { blocks: KindCard<K>[]; multi: PlanCard<P>[]; all: Array<CatalogCard<K, P>> } {
  const blocks = filterKinds(catalogKinds(kinds), query).map(
    (def): KindCard<K> => ({ section: "blocks", key: "kind:" + def.kind, drop: { kind: def.kind }, name: kindName(def), perch: def.actor === "you", def })
  );
  const multi = filterPlans(plans, query).map(
    (plan): PlanCard<P> => ({ section: "multi", key: "plan:" + plan.id, drop: { planId: plan.id }, name: plan.name, builtin: !!plan.builtin, path: planPath(plan), plan })
  );
  return { blocks, multi, all: [...blocks, ...multi] };
}

/** The arrows over the cards, laid out as one grid per section (`sizes`, in
 * order) `cols` wide: ← / → step through them all; ↓ / ↑ go a row down / up,
 * from a section's last row into the next section's first (and back), keeping
 * the column where the row is long enough. Nothing past either end. -1 when
 * there are no cards; a key it doesn't know leaves `at` where it is. */
export function gridMove(at: number, key: string, sizes: readonly number[], cols: number): number {
  const n = sizes.reduce((a, b) => a + b, 0);
  if (!n) return -1;
  if (at < 0 || at >= n) return 0;
  const c = Math.max(1, Math.floor(cols) || 1);
  if (key === "ArrowRight") return Math.min(at + 1, n - 1);
  if (key === "ArrowLeft") return Math.max(at - 1, 0);
  if (key !== "ArrowDown" && key !== "ArrowUp") return at;
  const starts: number[] = [];
  sizes.reduce((s, size) => (starts.push(s), s + size), 0);
  let s = 0;
  while (at >= starts[s] + sizes[s]) s++;
  const i = at - starts[s];
  const col = i % c;
  const row = Math.floor(i / c);
  if (key === "ArrowDown") {
    if (row < Math.floor((sizes[s] - 1) / c)) return starts[s] + Math.min(i + c, sizes[s] - 1);
    for (let t = s + 1; t < sizes.length; t++) if (sizes[t]) return starts[t] + Math.min(col, sizes[t] - 1);
    return at;
  }
  if (row > 0) return at - c;
  for (let t = s - 1; t >= 0; t--) {
    if (!sizes[t]) continue;
    const last = Math.floor((sizes[t] - 1) / c) * c;
    return starts[t] + Math.min(last + col, sizes[t] - 1);
  }
  return at;
}

/** Can the server rename this saved stop? Every stored one, built-ins too; a
 * built-in's depth cut (`builtin:ticket@push`) is virtual and can't be edited. */
export const renamable = (plan: Pick<FlightPlan, "id">): boolean => !/^builtin:[a-z]+@/.test(plan.id);

export type CardMenuKey = "rename" | "shelf" | "reset" | "delete";

/** A saved-stop card's ⋯: Rename (when the server allows it), On the shelf
 * (★ when it is, ☆ when not — a toggle), then Reset to default (a built-in) or Delete (yours). */
export function cardMenu(plan: Pick<FlightPlan, "id" | "builtin">, shelved: boolean): Array<{ key: CardMenuKey; label: string; danger?: boolean }> {
  return [
    ...(renamable(plan) ? [{ key: "rename" as const, label: "Rename" }] : []),
    { key: "shelf", label: (shelved ? "★" : "☆") + " On the shelf" },
    plan.builtin ? { key: "reset", label: "Reset to default" } : { key: "delete", label: "Delete", danger: true },
  ];
}
