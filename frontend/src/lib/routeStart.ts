/** Routes on rails: start a saved route at any of its stops, carry on where an
 * earlier run of it stopped, fly an ended route again (docs/flights.md
 * "Resume"; the server's `POST /api/flights/check` `resume`, `start_at`, and
 * `POST /api/flights/{id}/again`).
 *
 * The words and the choices, pure — the menu that shows them is
 * components/flights/RouteStartMenu.tsx, opened through `useRouteStart`:
 *
 *  - a saved route started on a session with no earlier run to carry on from
 *    starts as it always did (one click, nothing asked);
 *  - one WITH such a run opens a small menu at the click: "Carries on at Push
 *    — where it stopped 2h ago (its session was deleted)", Carry on there
 *    (the default, focused), Start from the beginning, Start at another stop…;
 *  - "Start at…" opens the route's own stops, numbered, to pick one;
 *  - an ended route (done, cancelled, halted) offers "Fly again from <stop>"
 *    (where it stopped) and "Fly again from another stop…". */

import { create } from "zustand";
import { relTime } from "./format";
import type { Flight, FlightPlan, FlightStep, StepKindDef } from "./flights";
import { checkStart, startFlightAt, type ResumePoint, type StartCheck } from "./flightsApi";

// --- A start's default --------------------------------------------------------------

/** Where a start goes by default after its check: the resume stop's id when
 * an earlier run of the route stopped part way, else "" (the normal start —
 * no `start_at` is sent). */
export function defaultStartAt(check: Pick<StartCheck, "resume"> | null | undefined): string {
  return check?.resume?.stop || "";
}

/** "Carries on at Push — where it stopped 2h ago (its session was deleted)":
 * the one line that says where a start goes by default. */
export function resumeSentence(r: Pick<ResumePoint, "title" | "at" | "reason">, now?: number): string {
  const when = r.at > 0 ? " " + relTime(r.at, { now }) : "";
  const why = r.reason ? " (" + r.reason + ")" : "";
  return "Carries on at " + r.title + " — where it stopped" + when + why;
}

// --- Stops to start at ----------------------------------------------------------------

/** One stop a route may start at: its number (1-based), title, kind and the
 * kind's label as the stop lists show it. */
export interface StopChoice {
  id: string;
  n: number;
  title: string;
  kind: string;
  label: string;
}

/** The kind's name as the palette shows it (StopParts `kindName`). */
function kindLabel(kind: string, kinds: readonly StepKindDef[] | null | undefined): string {
  if (kind === "you") return "Wait for me";
  if (kind === "someone") return "Someone";
  return kinds?.find((k) => k.kind === kind)?.label || kind;
}

/** A route's stops as choices, in order, numbered from 1. */
export function stopChoices(steps: readonly Pick<FlightStep, "id" | "kind" | "title">[], kinds?: readonly StepKindDef[] | null): StopChoice[] {
  return steps.map((s, i) => ({ id: s.id, n: i + 1, title: s.title || kindLabel(s.kind, kinds), kind: s.kind, label: kindLabel(s.kind, kinds) }));
}

/** A choice as one menu line: "3. Push · Ship". */
export function choiceLine(c: StopChoice): string {
  return c.n + ". " + c.title + (c.label && c.label !== c.title ? " · " + c.label : "");
}

// --- Flying an ended route again ----------------------------------------------------------

/** The index of the first stop the flight had not finished — from its cursor
 * on, the first neither done nor skipped (`steps.length` when it got past
 * every one): the server's `flights.reached`. */
export function reachedIndex(f: Pick<Flight, "steps" | "cursor" | "step_states">): number {
  const steps = f.steps || [];
  const states = f.step_states || {};
  let k = Math.min(Math.max(Math.floor(f.cursor || 0), 0), steps.length);
  while (k < steps.length && ["done", "skipped"].includes(states[steps[k].id]?.state as string)) k++;
  return k;
}

type AgainFields = Pick<Flight, "steps" | "cursor" | "step_states"> & Partial<Pick<Flight, "owed" | "owed_perch" | "owed_added">>;

/** The flight's own stops: without the "Your checks" perches the route added
 * itself for owed checks (`owed_perch`, `owed_added`) — what the person drew,
 * what Fly again flies (the server's `flights.own_steps`). */
export function ownSteps(f: Pick<Flight, "steps"> & Partial<Pick<Flight, "owed_perch" | "owed_added">>): FlightStep[] {
  const added = new Set([...(f.owed_added || []), ...(f.owed_perch ? [f.owed_perch] : [])]);
  return (f.steps || []).filter((s) => !added.has(s.id));
}

/** Where the flight carries on, as an index into `ownSteps`: the first of its
 * own stops it had not finished (standing on a perch the route added = the
 * next own stop after it) — but never past a Verify stop whose check it
 * still owed the person: the earliest of those. `ownSteps(f).length` = it
 * got past them all (the server's `flights.carry_index`). */
export function carryIndex(f: AgainFields): number {
  const own = ownSteps(f);
  const k = reachedIndex(f);
  const ids = new Set(own.map((s) => s.id));
  const next = f.steps.slice(k).find((s) => ids.has(s.id));
  const kOwn = next ? own.indexOf(next) : own.length;
  const owed = new Set((f.owed || []).map((e) => e.step));
  const first = own.slice(0, kOwn).findIndex((s) => owed.has(s.id));
  return first >= 0 ? first : kOwn;
}

/** The ended routes that can fly again: done, cancelled or halted, flown on
 * a session (not a free chain, not a source's default stops), with stops of
 * its own. */
export function canFlyAgain(f: (Pick<Flight, "state" | "steps"> & Partial<Pick<Flight, "free" | "edits_default" | "owed_perch" | "owed_added">>) | null | undefined): boolean {
  if (!f || f.free || f.edits_default || !ownSteps(f).length) return false;
  return f.state === "done" || f.state === "cancelled" || f.state === "halted";
}

/** The stop a Fly again with no `at` starts at: where it stopped, or null for
 * a route that got past every stop (it starts at its first). */
export function againStop(f: AgainFields): FlightStep | null {
  return ownSteps(f)[carryIndex(f)] || null;
}

/** Fly again's label: "Fly again from <stop>" where it stopped, "Fly again"
 * for a route that finished (it starts over). */
export function flyAgainLabel(f: AgainFields): string {
  const s = againStop(f);
  return s ? "Fly again from " + s.title : "Fly again";
}

/** Fly again's tooltip: what will happen. Its session alive: a new run of
 * its stops there. Gone (`gone`): the server flies it on the session that
 * holds its branch, or reopens its closed session. */
export function flyAgainTitle(f: AgainFields & Pick<Flight, "session">, gone: boolean): string {
  const s = againStop(f);
  const from = s ? " from “" + s.title + "”" : " from its first stop";
  if (!gone) return "Fly this route again on " + f.session + from + " — a new run of its stops";
  return f.session + " is gone: it flies again" + from + " on the session that holds its branch, or reopens " + f.session + " from Recently closed";
}

/** Fly again is the route's primary action when it stopped part way —
 * cancelled or halted; on a finished one it is offered, not pressed for you. */
export function flyAgainPrimary(f: Pick<Flight, "state">): boolean {
  return f.state === "cancelled" || f.state === "halted";
}

// --- The menu's state -------------------------------------------------------------------------

/** What the route-start menu shows (RouteStartMenu):
 *  - `start`: a saved route on session `title`; `view` "resume" = the carry-on
 *    line and its three choices, "pick" = the route's stops;
 *  - `again`: an ended flight's stops, to fly it again from one. */
export type RouteStartOpen =
  | { mode: "start"; view: "resume" | "pick"; plan: Pick<FlightPlan, "id" | "name" | "steps">; title: string; check: StartCheck | null; anchor: Rect; back: boolean; go(startAt: string): void }
  | { mode: "again"; flight: Flight; anchor: Rect };

/** What the menu opens beside: the control that opened it, or a point. */
export interface Rect {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

export const useRouteStartMenu = create<{ open: RouteStartOpen | null; set(o: RouteStartOpen | null): void }>((set) => ({
  open: null,
  set: (o) => set({ open: o }),
}));

/** Where the person last pressed (the menu opens beside it when the action
 * came from a ⋯ menu): kept by RouteStartMenu's host. */
export const lastPress = { x: 0, y: 0 };

/** What the menu opens beside: the element's box when given, else `at` (a
 * drop's point), else the last press. */
export function anchorOf(el?: Element | null, at?: { x: number; y: number } | null): Rect {
  if (el && typeof el.getBoundingClientRect === "function") {
    const r = el.getBoundingClientRect();
    return { left: r.left, top: r.top, right: r.right, bottom: r.bottom };
  }
  const x = at ? at.x : lastPress.x;
  const y = at ? at.y : lastPress.y;
  return { left: x, top: y, right: x, bottom: y };
}

/** Where a menu of `size` goes for `anchor` in a `view`port: BESIDE it — to
 * its right, else its left — top-aligned and pulled up to stay on screen, so
 * it never covers the control that opened it or what lies under that; with
 * no room on either side, below it (above when there is no room below).
 * Always inside the viewport, 8px from its edges. */
export function placeMenu(anchor: Rect, size: { w: number; h: number }, view: { w: number; h: number }): { x: number; y: number } {
  const M = 8;
  const GAP = 6;
  const clampY = (y: number) => Math.max(M, Math.min(y, view.h - M - size.h));
  const clampX = (x: number) => Math.max(M, Math.min(x, view.w - M - size.w));
  if (anchor.right + GAP + size.w <= view.w - M) return { x: anchor.right + GAP, y: clampY(anchor.top) };
  if (anchor.left - GAP - size.w >= M) return { x: anchor.left - GAP - size.w, y: clampY(anchor.top) };
  const below = anchor.bottom + GAP;
  const y = below + size.h <= view.h - M ? below : anchor.top - GAP - size.h;
  return { x: clampX(anchor.left), y: clampY(y) };
}

// --- Starting a saved route on a session ----------------------------------------------------

/** Start saved route `plan` on session `title` (one with no route in the air):
 * check first; with nothing to carry on from and no `choose`, `normal()` —
 * the start the caller always made (one click). Otherwise the menu opens at
 * `el` (or `at`, or the last press): the carry-on line with its choices, or (`choose`)
 * the route's stops. A pick of stop 1 / "Start from the beginning" is
 * `normal()`; any later stop starts there (`startFlightAt`). */
export async function startSavedRoute(
  plan: Pick<FlightPlan, "id" | "name" | "steps">,
  title: string,
  opts: { normal(): unknown; choose?: boolean; el?: Element | null; at?: { x: number; y: number } | null }
): Promise<void> {
  const check = await checkStart({ plan_id: plan.id, title });
  if (!opts.choose && !check?.resume) {
    await opts.normal();
    return;
  }
  const anchor = anchorOf(opts.el, opts.at);
  // The saved route's own stops (their ids are what `start_at` names); the
  // check's list may lead with a fast-track's rungs the start takes over.
  const steps = plan.steps?.length ? plan.steps : check?.steps || [];
  useRouteStartMenu.getState().set({
    mode: "start",
    view: opts.choose ? "pick" : "resume",
    back: false,
    plan: { ...plan, steps },
    title,
    check,
    anchor,
    go: (startAt) => {
      if (!startAt || startAt === steps[0]?.id) void opts.normal();
      else void startFlightAt(title, plan.id, startAt);
    },
  });
}

/** "Fly again from another stop…": the ended flight's stops at `el` (or the
 * last press). */
export function pickAgainStop(flight: Flight, el?: Element | null): void {
  useRouteStartMenu.getState().set({ mode: "again", flight, anchor: anchorOf(el) });
}
