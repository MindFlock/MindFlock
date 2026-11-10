/** The rules behind the board's grounded lines and the shelf's saved stops
 * (docs/flights.md "Round 11"): which flights draw as a line with no session
 * yet and in what order, the layout key such a line sits under, the diagram
 * its stops draw as (every stop editable: nothing has flown), what Save to
 * the shelf saves, a saved stop's dots on its chip, and what a rename may be.
 * Pure — GroundedLine / useGroundedLine / StopShelf hold the state and the
 * saving. */

import { atHaltPoint, flightDiagram, type Diagram } from "./flightDiagram";
import { isLive, newStep, planFromSteps, stepActor, type Flight, type FlightPlan, type FlightStep, type IntakeSourceRef, type StepKindDef } from "./flights";
import { chainColor, type ChainHead } from "./skyGraph";
import { relTime } from "./format";
import { haltedWhy } from "./sessionState";

/** A grounded flight's line is laid out under `"flight:" + id` (a session's
 * line under its title — a title never starts with this). */
export const GROUNDED_PREFIX = "flight:";
export const groundedKey = (id: string) => GROUNDED_PREFIX + id;
export const isGroundedKey = (key: string) => key.startsWith(GROUNDED_PREFIX);
export const groundedIdOf = (key: string) => (isGroundedKey(key) ? key.slice(GROUNDED_PREFIX.length) : "");

/** A free chain in the air: running with no session, in a folder. */
export const isFreeRun = (f: Pick<Flight, "state"> & { free?: boolean; session?: string }) => !!f.free && !f.session && isLive(f);

/** The flights that draw as a chain on the board (no session): grounded,
 * or a free chain mid-run — it stays put while it runs (n8n: a run happens
 * ON the workflow). Newest first (they follow the session lines). */
export function groundedFlights<F extends Pick<Flight, "state" | "started" | "id"> & { free?: boolean; session?: string }>(flights: readonly F[] | null | undefined): F[] {
  return (flights || []).filter((f) => f.state === "grounded" || isFreeRun(f)).sort((a, b) => (b.started || 0) - (a.started || 0) || (a.id < b.id ? 1 : -1));
}

/** A grounded flight's stops as the canvas draws them: the flight's own
 * diagram, every stop editable (none has flown, none is current), the + at
 * the end and between any two. */
export function groundedDiagram(f: Flight, kinds?: readonly StepKindDef[] | null): Diagram {
  if (isFreeRun(f)) {
    // Mid-run: the stops as they are flying (the bird, the ✓s, the countdown).
    return { ...flightDiagram(f, kinds), flightId: f.id, mode: "flight" };
  }
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
 * kept), else a new one named after the line. `into`: write to that saved
 * route whatever the steps say (a saved route's "Edit its stops" copy);
 * `fresh`: always a new saved route called `name` (Save as a route…). */
export function planToSave(name: string, steps: readonly FlightStep[], plans: readonly FlightPlan[], opts: { into?: string; fresh?: boolean } = {}): FlightPlan {
  const into = opts.into && plans.some((p) => p.id === opts.into) ? opts.into : "";
  const id = opts.fresh ? "" : into || sourcePlanId(steps, plans);
  const base = id ? plans.find((p) => p.id === id)! : null;
  const fresh = planFromSteps(base ? base.name : name || "New route", steps, base?.trigger);
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
  if (!name) return { ok: false, error: "A saved route needs a name" };
  if (name.length > NAME_MAX) return { ok: false, error: "Keep the name under " + NAME_MAX + " characters" };
  const clash = plans.find((p) => p.id !== selfId && p.name.trim().toLowerCase() === name.toLowerCase());
  if (clash) return { ok: false, error: "Another saved route is already called “" + clash.name.trim() + "”" };
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
      out.push({ fromFlight: f.id, fromStep: st.id, stepTitle: st.title || "Start a route", fromSession: f.session || "", fromName: f.plan_name || "", target, started: !!child, session });
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
  const chain = link.fromSession ? nameOf(link.fromSession) : link.fromName || "a route";
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

// --- An intake source's default stops, edited on the board -------------------

/** The name a default-stops editor chain is made with: "Default · <label>". */
export const DEFAULT_EDITOR_PREFIX = "Default · ";
export const defaultEditorName = (label: string) => DEFAULT_EDITOR_PREFIX + label;

/** The two refs name the same intake source. */
export const sameSource = (a: IntakeSourceRef | null | undefined, b: IntakeSourceRef | null | undefined) => !!a && !!b && a.kind === b.kind && a.target === b.target;

/** A grounded chain that edits an intake source's default stops (never run). */
export const isDefaultEditor = (f: Pick<Flight, "edits_default"> | null | undefined): boolean => !!f?.edits_default?.kind && !!f.edits_default.target;

/** The editor chain already on the board for that source (grounded, editing
 * it), or null — the lane's stops chip selects it instead of making another. */
export function editorFor<F extends Pick<Flight, "state" | "edits_default">>(flights: readonly F[] | null | undefined, ref: IntakeSourceRef): F | null {
  return (flights || []).find((f) => f.state === "grounded" && sameSource(f.edits_default, ref)) || null;
}

/** What an editor chain's source is called: its name without "Default · "
 * (the label it was made with), else the target. */
export function editorLabel(f: Pick<Flight, "plan_name" | "edits_default">): string {
  const n = (f.plan_name || "").trim();
  if (n.startsWith(DEFAULT_EDITOR_PREFIX) && n.length > DEFAULT_EDITOR_PREFIX.length) return n.slice(DEFAULT_EDITOR_PREFIX.length);
  return n && n !== "Stops" ? n : f.edits_default?.target || "";
}

/** A chain's ▶: none for an editor of a source's default (it never runs —
 * Save as default is what it does), none for one another chain starts, none
 * with no stops; else `make()`'s. */
export function chainRunFor<R>(f: Pick<Flight, "edits_default"> & { steps: readonly unknown[] }, headKind: ChainHead["kind"], make: () => R): R | null {
  if (isDefaultEditor(f) || !f.steps.length || headKind === "trigger") return null;
  return make();
}

/** An editor's bar, split from its label ("Tickets · Jira — payments", "PRs ·
 * owner/repo"): the source's own name as the title ("Jira — payments") and
 * the kind ("Tickets") for the small tag beside it. A label with no " · "
 * is all name. */
export function editorTitle(label: string): { kind: string; name: string } {
  const at = label.indexOf(" · ");
  if (at <= 0 || at + 3 >= label.length) return { kind: "", name: label };
  return { kind: label.slice(0, at), name: label.slice(at + 3) };
}

/** The same value, keys in any order (a step's config as JSON). */
function sameValue(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (Array.isArray(a) || Array.isArray(b)) {
    if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
    return a.every((v, i) => sameValue(v, b[i]));
  }
  if (!a || !b || typeof a !== "object" || typeof b !== "object") return false;
  const ka = Object.keys(a as object).filter((k) => (a as Record<string, unknown>)[k] !== undefined);
  const kb = Object.keys(b as object).filter((k) => (b as Record<string, unknown>)[k] !== undefined);
  return ka.length === kb.length && ka.every((k) => sameValue((a as Record<string, unknown>)[k], (b as Record<string, unknown>)[k]));
}

type DefaultStepShape = Pick<FlightStep, "kind" | "title" | "config"> & Partial<Pick<FlightStep, "on_fail" | "halt" | "when">>;

/** An editor chain's steps differ from the source's current default: another
 * count, or a step whose kind, title, config, on-fail, halt or condition
 * changed (ids and where a tile sits are not a change). Save as default is
 * only worth pressing then. */
export function defaultChanged(steps: readonly DefaultStepShape[], base: readonly DefaultStepShape[]): boolean {
  if (steps.length !== base.length) return true;
  return steps.some((s, i) => {
    const b = base[i];
    return (
      s.kind !== b.kind ||
      s.title !== b.title ||
      (s.on_fail || "") !== (b.on_fail || "") ||
      !!s.halt !== !!b.halt ||
      (s.when || "") !== (b.when || "") ||
      !sameValue(s.config || {}, b.config || {})
    );
  });
}

/** A free chain's "Last run …" chip and its tooltip: how it went and when
 * ("Last run done · 3m ago"), and why it stopped when it says. */
export function lastRunWords(last: NonNullable<Flight["last_run"]>, now?: number): { label: string; title: string } {
  const label = last.state === "done" ? "Last run done" : "Last run stopped";
  const when = last.at ? " · " + relTime(last.at, now === undefined ? {} : { now }) : "";
  return { label, title: label + when + (last.reason ? " — " + last.reason : "") };
}

// --- A route running on its own (no session): followed and un-stuck --------

/** What a route's state is called on its chip, and why, in the round's one
 * vocabulary: "Halted" (red) = a stop failed, the reason in the tooltip;
 * "At your halt point" (amber) = it paused at a halt point a person set and
 * waits on them; "Paused" (calm) = a person paused it; "Needs you" = a stop
 * waits on the person; "Running". */
export function runChip(f: Pick<Flight, "state" | "reason" | "steps" | "cursor" | "step_states">): { label: string; tone: string; title: string } {
  const step = f.steps[f.cursor];
  if (atHaltPoint(f)) return { label: "At your halt point", tone: "attention", title: "Paused at your halt point before “" + (step?.title || "the next stop") + "” — Carry on to go on" };
  if (f.state === "halted") {
    const why = haltedWhy(step?.title, f.reason);
    return { label: "Halted", tone: "broken", title: "Halted" + (step ? " at “" + step.title + "”" : "") + (why ? " — " + why : "") + ". Retry or Skip it on the stop." };
  }
  if (f.state === "waiting_you") {
    const why = perchWhy(f);
    return { label: "Needs you", tone: "attention", title: (why || "Waiting on you") + (step ? " — at “" + step.title + "”" : "") };
  }
  if (f.state === "paused") return { label: "Paused", tone: "muted", title: "A person paused it: Carry on (in ⋯, or on its stop) to go on" };
  if (f.state === "running") return { label: "Running", tone: "working", title: "Running" + (step ? " “" + step.title + "”" : "") };
  return { label: f.state === "ready" ? "Ready" : f.state === "done" ? "Done" : f.state === "cancelled" ? "Stopped" : "Not started", tone: "muted", title: "" };
}

/** A free run (no session) that waits on the person: halted, at a Wait for
 * me stop, or at a halt point — the Needs-you view draws it. */
export function freeRunNeedsYou(f: Pick<Flight, "state" | "reason" | "steps" | "cursor" | "step_states" | "session"> & { free?: boolean }): boolean {
  if (!isFreeRun(f)) return false;
  return f.state === "halted" || f.state === "waiting_you" || atHaltPoint(f);
}

/** The routes with no session a view of the Sky draws: none in This session;
 * in Needs you only the free runs that wait on the person (halted, waiting,
 * at a halt point); else all of them. A search narrows them by name. */
export function groundedShown<F extends Flight & { free?: boolean }>(flights: readonly F[] | null | undefined, mode: string, query: string, nameOf: (f: F) => string): F[] {
  if (mode === "one") return [];
  const q = query.trim().toLowerCase();
  const all = groundedFlights(flights);
  return (mode === "perched" ? all.filter(freeRunNeedsYou) : all).filter((f) => !q || nameOf(f).toLowerCase().includes(q));
}

/** Why a route waits on the person, in plain words: a failed stop's reason
 * (halted), "Paused at your halt point" (a halt point reached), or the
 * waiting stop's own note / failure reason / the route's reason — never the
 * bare "needs you" the server writes when it has nothing better. "" = none
 * to tell. */
export function perchWhy(f: Pick<Flight, "state" | "reason" | "steps" | "cursor" | "step_states">): string {
  const step = f.steps[f.cursor];
  if (atHaltPoint(f)) return "Paused at your halt point — Carry on when you're ready";
  const rec = step ? f.step_states?.[step.id] : undefined;
  const failed = String((rec?.detail as Record<string, unknown> | undefined)?.failed_reason ?? "").trim();
  if (f.state === "halted") return haltedWhy(step?.title, f.reason || failed);
  if (f.state !== "waiting_you" && f.state !== "paused") return "";
  const words = [String(rec?.note || "").trim(), failed, String(f.reason || "").trim()].find((w) => w && !/^needs you\.?$/i.test(w)) || "";
  return words ? haltedWhy(step?.title, words) : "";
}

// --- "Edit its stops": an editable copy of a saved route on the Sky ----------

/** Which grounded chains are editing copies of a saved route, per device:
 * flight id → the saved route's id. The copy's Save writes back to it. */
export const ROUTE_EDITS_KEY = "mf_sky_route_edits";

/** The stored map, read safely (anything odd reads as none). */
export function readRouteEdits(raw: string | null | undefined): Record<string, string> {
  if (!raw) return {};
  try {
    const v = JSON.parse(raw) as unknown;
    if (!v || typeof v !== "object" || Array.isArray(v)) return {};
    return Object.fromEntries(Object.entries(v as Record<string, unknown>).filter((e): e is [string, string] => typeof e[1] === "string" && !!e[1]));
  } catch {
    return {};
  }
}

/** The saved route a grounded chain edits ("" = none, or it is gone). */
export function editedPlanOf<P extends Pick<FlightPlan, "id">>(flightId: string, edits: Readonly<Record<string, string>>, plans: readonly P[]): P | null {
  const id = edits[flightId];
  return (id && plans.find((p) => p.id === id)) || null;
}

/** The toast every route save ends with: routes already flying never change. */
export const SAVED_COPY_NOTE = "routes already running keep their own copy";
export const savedWords = (name: string, updated: boolean) => (updated ? "Updated “" : "Saved “") + name + "” — " + SAVED_COPY_NOTE;
