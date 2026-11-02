/** The rules behind the board's grounded lines and the shelf's saved stops
 * (docs/flights.md "Round 11"): which flights draw as a line with no session
 * yet and in what order, the layout key such a line sits under, the diagram
 * its stops draw as (every stop editable: nothing has flown), what Save to
 * the shelf saves, a saved stop's dots on its chip, and what a rename may be.
 * Pure — GroundedLine / useGroundedLine / StopShelf hold the state and the
 * saving. */

import { flightDiagram, type Diagram } from "./flightDiagram";
import { newStep, planFromSteps, stepActor, type Flight, type FlightPlan, type FlightStep, type StepKindDef } from "./flights";
import { chainColor, type ChainHead } from "./skyGraph";

/** A grounded flight's line is laid out under `"flight:" + id` (a session's
 * line under its title — a title never starts with this). */
export const GROUNDED_PREFIX = "flight:";
export const groundedKey = (id: string) => GROUNDED_PREFIX + id;
export const isGroundedKey = (key: string) => key.startsWith(GROUNDED_PREFIX);
export const groundedIdOf = (key: string) => (isGroundedKey(key) ? key.slice(GROUNDED_PREFIX.length) : "");

/** The flights that draw as a grounded line: `state === "grounded"`, newest
 * first (they follow the session lines). */
export function groundedFlights<F extends Pick<Flight, "state" | "started" | "id">>(flights: readonly F[] | null | undefined): F[] {
  return (flights || []).filter((f) => f.state === "grounded").sort((a, b) => (b.started || 0) - (a.started || 0) || (a.id < b.id ? 1 : -1));
}

/** A grounded flight's stops as the canvas draws them: the flight's own
 * diagram, every stop editable (none has flown, none is current), the + at
 * the end and between any two. */
export function groundedDiagram(f: Flight, kinds?: readonly StepKindDef[] | null): Diagram {
  let d = flightDiagram(f, kinds);
  const nodes = d.nodes.map((n) => (n.kind === "stop" ? { ...n, state: "pending" as const, editable: true } : n));
  // A Loop rides its return wire (lib/flightDiagram `liftLoops`): it changes the same way.
  d = { ...d, edges: d.edges.map((e) => (e.via ? { ...e, via: { ...e.via, state: "pending" as const, editable: true } } : e)) };
  const hasAdd = nodes.some((n) => n.kind === "add");
  if (!hasAdd) {
    // flightDiagram adds its + only to a flight in the air; a grounded one
    // takes stops anywhere, so it gets the same trailing +.
    const last = [...nodes].reverse().find((n) => n.row === "main");
    nodes.push({ id: "add", kind: "add", row: "main", title: "Add a stop", sub: "", state: "pending", icon: "add" });
    const edges = last ? [...d.edges, { from: last.id, to: "add", kind: "add" as const }] : d.edges;
    return { ...d, nodes, edges, flightId: f.id, mode: "flight", insertFrom: 0 };
  }
  return { ...d, nodes, flightId: f.id, mode: "flight", insertFrom: 0 };
}

/** One stop of `def`, the way the palette makes it (the kind's defaults). */
export function defaultStepFor(def: StepKindDef): FlightStep {
  return newStep(def, []);
}

/** The saved stop a grounded line came from: the `group.plan_id` every one of
 * its steps shares, when that saved stop still exists — Save to the shelf
 * then updates it. "" otherwise (a new saved stop). */
export function sourcePlanId(steps: readonly Pick<FlightStep, "group">[], plans: readonly Pick<FlightPlan, "id">[]): string {
  if (!steps.length) return "";
  const id = steps[0].group?.plan_id || "";
  if (!id || id === "adhoc") return "";
  if (!steps.every((s) => s.group?.plan_id === id)) return "";
  return plans.some((p) => p.id === id) ? id : "";
}

/** What Save to the shelf sends: the line's steps as a saved stop — under the
 * saved stop it came from (its trigger, session defaults and description
 * kept), else a new one named after the line. */
export function planToSave(name: string, steps: readonly FlightStep[], plans: readonly FlightPlan[]): FlightPlan {
  const id = sourcePlanId(steps, plans);
  const base = id ? plans.find((p) => p.id === id)! : null;
  const fresh = planFromSteps(base ? base.name : name || "Stops", steps, base?.trigger);
  if (!base) return { ...fresh, id: "", updated: 0 };
  return { ...fresh, id: base.id, builtin: base.builtin, description: base.description, session: { ...base.session }, updated: base.updated };
}

/** A saved stop's chip dots: one per step (a perch's gold), at most `max`,
 * the rest as "+N". */
export function planDots(
  steps: readonly Pick<FlightStep, "kind">[],
  kinds?: readonly StepKindDef[] | null,
  max = 10
): { dots: Array<{ perch: boolean }>; more: number } {
  const shown = steps.slice(0, max).map((s) => ({ perch: stepActor(s, kinds) === "you" }));
  return { dots: shown, more: Math.max(0, steps.length - max) };
}

/** The shelf's saved stops: built-ins first, then yours, each as listed. */
export function savedStopOrder<P extends Pick<FlightPlan, "builtin">>(plans: readonly P[]): P[] {
  return [...plans.filter((p) => p.builtin), ...plans.filter((p) => !p.builtin)];
}

/** A saved stop's name, as the server keeps it (`_text(name, 120)`). */
export const NAME_MAX = 120;

export type RenameCheck = { ok: true; name: string } | { ok: false; error: string };

/** A rename: trimmed, never empty, at most NAME_MAX characters. The same name
 * is fine (nothing to save); one another saved stop has is refused, so the
 * shelf never shows two chips you can't tell apart. */
export function checkRename(raw: string, plans: readonly Pick<FlightPlan, "id" | "name">[], selfId: string): RenameCheck {
  const name = raw.trim();
  if (!name) return { ok: false, error: "A saved stop needs a name" };
  if (name.length > NAME_MAX) return { ok: false, error: "Keep the name under " + NAME_MAX + " characters" };
  const clash = plans.find((p) => p.id !== selfId && p.name.trim().toLowerCase() === name.toLowerCase());
  if (clash) return { ok: false, error: "Another saved stop is already called “" + clash.name.trim() + "”" };
  return { ok: true, name };
}

/** Delete or Backspace on a focused grounded line asks to remove it. */
export const isRemoveKey = (key: string) => key === "Delete" || key === "Backspace";

// --- Round 18: one chain starts another (the Handoff kind) ---------------------

/** The plug-in kind that starts another chain (docs/flights.md "Round 18"). */
export const HANDOFF_KIND = "handoff";

/** One Handoff stop and the chain it starts: `target` is the stops-only
 * flight it names (`config.target` "flight:<id>") until it has started, then
 * the started flight (`detail.child`, `started`). `session` is where the
 * target runs ("new" = a new session); `fromSession` the source flight's. */
export interface HandoffLink {
  fromFlight: string;
  fromStep: string;
  stepTitle: string;
  fromSession: string;
  fromName: string;
  target: string;
  started: boolean;
  session: string;
}

/** Every Handoff stop on the board that names a chain (a saved stop target
 * — "plan:<id>" — names none until it has started one). */
export function handoffLinks(flights: readonly Flight[] | null | undefined): HandoffLink[] {
  const out: HandoffLink[] = [];
  for (const f of flights || [])
    for (const st of f.steps || []) {
      if (st.kind !== HANDOFF_KIND) continue;
      const detail = (f.step_states?.[st.id]?.detail || {}) as Record<string, unknown>;
      const child = typeof detail.child === "string" ? detail.child : "";
      const raw = String(st.config?.target ?? "");
      const target = child || (isGroundedKey(raw) ? groundedIdOf(raw) : "");
      if (!target) continue;
      const session = String((child && typeof detail.session === "string" ? detail.session : st.config?.session) || "new");
      out.push({ fromFlight: f.id, fromStep: st.id, stepTitle: st.title || "Start a chain", fromSession: f.session || "", fromName: f.plan_name || "", target, started: !!child, session });
    }
  return out;
}

/** "a new session" for "new", else the title. */
export const sessionWord = (s: string, nameOf: (t: string) => string = (t) => t) => (!s || s === "new" ? "a new session" : nameOf(s));

/** The head of a stops-only chain another chain hands off to: a trigger tile
 * "When <chain> reaches <stop>", naming the session it will run in, in the
 * SOURCE session's colour. Null when nothing hands off to it. */
export function triggerHeadFor(f: Pick<Flight, "id">, flights: readonly Flight[] | null | undefined, nameOf: (t: string) => string = (t) => t): ChainHead | null {
  const link = handoffLinks(flights).find((l) => l.target === f.id && !l.started);
  if (!link) return null;
  const chain = link.fromSession ? nameOf(link.fromSession) : link.fromName || "a chain";
  return {
    kind: "trigger",
    title: "When " + chain + " reaches " + link.stepTitle,
    sub: "runs on " + sessionWord(link.session, nameOf),
    session: link.session,
    color: chainColor(link.fromSession),
    from: { flight: link.fromFlight, step: link.fromStep, title: link.stepTitle },
  };
}

/** A Handoff stop of `def` that starts the stops-only flight `target` ("flight:<id>")
 * on a new session, done once it has taken off. */
export function handoffStepFor(def: StepKindDef, taken: readonly string[], target: string): FlightStep {
  const st = newStep(def, taken);
  return { ...st, config: { ...st.config, target, session: "new", wait: false } };
}
