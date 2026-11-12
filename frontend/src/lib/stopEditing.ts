/** Editing the stops on a line the way n8n edits its nodes — the rules the
 * canvas (FlywayCanvas) and a session's line (useSessionLine) share, pure so
 * the tests read them (docs/flights-stop-editing.md):
 *
 *  - `insertGaps`   where a + can sit between two boxes of the line (never
 *                   before the flight's current stop, never across a column
 *                   of branches);
 *  - `dropGapFor`   which of those gaps a stop chip dragged from the shelf
 *                   (`STOP_MIME`) lands in — the nearest to the pointer, else
 *                   the end of the line;
 *  - `canShift`     whether ⇤ / ⇥ (and ← / →) can move a stop one place;
 *  - `undoPayload`  what Undo tacks back after a stop was removed;
 *  - `menuItemsFor` a box's right-click menu, by its kind and state;
 *  - `haltToggle` / `haltMark`  a halt point's toggle and the badge a box
 *                   wears while it is one (docs/flights.md "Halt points");
 *  - `removeTool`   what 🗑 says on a stop: pending, flown, or the one the
 *                   flight is at (removing it halts the flight there);
 *  - `stopDropOf`   what a drop on a line carries: a stop kind, or a saved
 *                   stop (round 11: a group of stops);
 *  - `birdAction`   what a click on the bird does (Pause / Carry on /
 *                   Retry), and `canJumpTo` / `jumpTargets` where a dragged
 *                   bird may land (docs/flights.md "As built: round 12 bird");
 *  - `FIELD_HINT` / `FIELD_RANGE` / `onFailHint` / `stopSentence`  what each
 *                   option of a stop's form makes the route do, in plain
 *                   words (the form's hints and its read-back sentence). */

import type { BirdTone, Diagram, DiagramNode, Placed } from "./flightDiagram";
import { STOP_MIME, type Flight, type FlightStep, type OnFail } from "./flights";
import { FLYWAY_MIME } from "./skyLayout";
import { FIX_ROUNDS, VERIFY_ON_FAIL } from "./verifyStop";
import { askOffers } from "./stopAsks";

// --- Gaps on the line -------------------------------------------------------------

/** A + between two boxes: insert before stop `at`; x / y is its centre, in the
 * layout's coordinates. */
export interface InsertGap {
  at: number;
  x: number;
  y: number;
}

/** Every gap of the line a stop may be inserted at: before each stop whose
 * index is ≥ `insertFrom`, never right after the trailing +, and not between a
 * stop with branches and its join point (the line reaches the join THROUGH
 * the branches). A column of children merely hung off a stop (round 12: no
 * join waits on them) keeps its gap, drawn between the stop and the column. */
export function insertGaps(boxes: readonly Placed[], insertFrom: number, vertical: boolean): InsertGap[] {
  const main = boxes.filter((b) => b.node.row === "main");
  const fanned = new Set(boxes.filter((b) => b.node.kind === "branch").map((b) => b.node.branch?.from));
  // Where the column of hung children after a box begins.
  const hung = new Map<string, number>();
  for (const c of boxes)
    if (c.node.kind === "child" && c.node.branch) hung.set(c.node.branch.from, Math.min(hung.get(c.node.branch.from) ?? Infinity, vertical ? c.y : c.x));
  const gaps: InsertGap[] = [];
  for (let j = 1; j < main.length; j++) {
    const b = main[j];
    const a = main[j - 1];
    if ((b.node.kind !== "stop" && b.node.kind !== "group") || b.node.index === undefined || b.node.index < insertFrom || a.node.kind === "add" || fanned.has(a.node.id)) continue;
    const col = hung.get(a.node.id);
    const end = col ?? (vertical ? b.y : b.x);
    gaps.push(
      vertical
        ? { at: b.node.index, x: b.x + b.w / 2, y: (a.y + a.h + end) / 2 }
        : { at: b.node.index, x: (a.x + a.w + end) / 2, y: (col === undefined ? b.y : a.y) + Math.min(b.h, a.h) / 2 }
    );
  }
  return gaps;
}

/** Where a stop dropped at `point` (layout coordinates) is tacked on: the
 * index of the gap nearest to it along the line, or null — the end — when the
 * end of the line (the trailing +) is nearer, or there is no gap to take. */
export function dropGapFor(boxes: readonly Placed[], point: { x: number; y: number }, insertFrom: number, vertical: boolean): number | null {
  const along = (p: { x: number; y: number }) => (vertical ? p.y : p.x);
  const gaps = insertGaps(boxes, insertFrom, vertical);
  if (!gaps.length) return null;
  const main = boxes.filter((b) => b.node.row === "main");
  const add = main.find((b) => b.node.kind === "add");
  const last = main.filter((b) => b.node.kind !== "add").pop();
  // The end sits at the trailing +, else just past the last box.
  const end = add
    ? along({ x: add.x + add.w / 2, y: add.y + add.h / 2 })
    : last
      ? along({ x: last.x + last.w, y: last.y + last.h })
      : Infinity;
  const at = along(point);
  let best: InsertGap | null = null;
  for (const g of gaps) if (!best || Math.abs(along(g) - at) < Math.abs(along(best) - at)) best = g;
  return best && Math.abs(along(best) - at) < Math.abs(end - at) ? best.at : null;
}

/** What a drop on a line carries: a stop kind's chip (`STOP_MIME`, its value
 * the kind) or a saved stop (`FLYWAY_MIME`, its value the plan id — tacked
 * on as one group). */
export type StopDrop = { kind: string } | { planId: string };

/** Whether a dragged thing is a stop chip or a saved stop from the shelf. */
export function isStopDrag(types: ArrayLike<string> | null | undefined): boolean {
  if (!types) return false;
  const list = Array.from(types);
  return list.includes(STOP_MIME) || list.includes(FLYWAY_MIME);
}

/** The drop's payload, a kind before a saved stop; null when it has neither. */
export function stopDropOf(dt: Pick<DataTransfer, "getData"> | null | undefined): StopDrop | null {
  if (!dt) return null;
  const kind = dt.getData(STOP_MIME);
  if (kind) return { kind };
  const planId = dt.getData(FLYWAY_MIME);
  return planId ? { planId } : null;
}

// --- Moving ------------------------------------------------------------------------

/** Whether the stop at `i` (among the line's places) can move one place
 * (`dir` -1 earlier, +1 later): it changes, and so does the place it trades
 * with, which is also not before `insertFrom`. A group box does not move
 * this round (a stop may still trade places with one that can change). */
export function canShift(stops: readonly DiagramNode[], i: number, dir: -1 | 1, insertFrom: number): boolean {
  const to = i + dir;
  return !!stops[i]?.editable && stops[i].kind !== "group" && to >= insertFrom && to >= 0 && to < stops.length && !!stops[to].editable;
}

// --- Undo ---------------------------------------------------------------------------

/** What Undo tacks back on after `step` (at `index`) was removed: the same
 * stop, at its old index while that is still ahead of the flight (after its
 * current stop, and not past the end), else at the end (`at` left out). */
export function undoPayload(step: FlightStep, index: number, flight: Pick<Flight, "cursor" | "steps"> | null): { step: FlightStep; at?: number } {
  const ahead = !!flight && index > flight.cursor && index < flight.steps.length;
  return ahead ? { step, at: index } : { step };
}

// --- The right-click menu --------------------------------------------------------------

export type StopMenuKey = "edit" | "earlier" | "later" | "duplicate" | "remove" | "done" | "behaviors" | "fix" | "skip" | "retry" | "halt" | "pause" | "resume" | "jump" | "tackon" | "open" | "expand" | "note";

/** One line of a box's menu: something to press, or a muted `note`. */
export interface StopMenuItem {
  key: StopMenuKey;
  label: string;
  title: string;
  disabled?: boolean;
  danger?: boolean;
  note?: boolean;
  /** A toggle that is on (the halt point toggle on a halt point): the
   * toolbar draws its button pressed. */
  pressed?: boolean;
}

export interface StopMenuCtx {
  /** What the host lets the line do (the canvas's handlers). */
  can: { edit?: boolean; move?: boolean; duplicate?: boolean; remove?: boolean; open?: boolean; halt?: boolean; group?: boolean };
  /** ⇤ / ⇥ possible for this stop (`canShift`). */
  earlier?: boolean;
  later?: boolean;
  /** The stop belongs to a live flight the menu can act on (Done / Skip / Retry). */
  live?: boolean;
  /** The box is the live flight's current stop: Pause / Carry on. */
  here?: boolean;
  /** A group box drawn open (its sub-steps on the line): Fold, not Show. */
  expanded?: boolean;
  /** The bird may be jumped to this box (`canJumpTo`): Jump here. */
  jump?: boolean;
}

// --- Halt points ------------------------------------------------------------------------

const flownState = (s: DiagramNode["state"]) => s === "done" || s === "skipped" || s === "failed";

/** A group's halt point lives on its first sub-step not yet flown (the
 * flight's own, or the next it reaches): that sub-step's node, null when
 * every one has flown. */
export function groupHaltStep(n: DiagramNode): DiagramNode | null {
  if (n.kind !== "group") return null;
  return n.group?.nodes.find((s) => !flownState(s.state) && !!s.step) || null;
}

/** Whether a box can be marked a halt point: a stop not yet flown, that can
 * change or is where the flight is; a group by its first sub-step not flown. */
export function canHalt(n: DiagramNode, here = false): boolean {
  if (n.kind === "group") {
    const s = groupHaltStep(n);
    return !!s && (!!s.editable || FLIGHT_HERE.has(s.state));
  }
  return n.kind === "stop" && !!n.step && (!!n.editable || here) && !flownState(n.state);
}

const FLIGHT_HERE: ReadonlySet<DiagramNode["state"]> = new Set(["current", "perched", "paused", "halted"]);

/** ⏸ Add halt point on a stop, or Remove halt point on one that is. ("Carry
 * on" is only ever the resume of a paused route.) */
export function haltToggle(n: DiagramNode): StopMenuItem {
  if (n.kind === "group") n = groupHaltStep(n) || n;
  return n.step?.halt
    ? { key: "halt", label: "Remove halt point", title: "Take the halt point off this stop — the route goes on through it", pressed: true }
    : { key: "halt", label: "Add halt point", title: "Make this stop a halt point — the route pauses when it gets here, before doing it" };
}

/** ⏸ Pause (the flight under way or waiting on you) or ▶ Carry on (paused),
 * on the flight's current stop; none on a stop halted by a failure (Retry). */
export function flightHold(n: DiagramNode): StopMenuItem | null {
  if (n.state === "paused") return { key: "resume", label: "Carry on", title: "Go on from this stop" };
  if (n.state === "halted") return null;
  return { key: "pause", label: "Pause", title: "Pause the route where it is — Carry on picks it up again" };
}

/** The badge a halt point wears in its corner (null on any other box). */
export function haltMark(n: DiagramNode): { glyph: string; title: string } | null {
  if (n.kind === "group") {
    const s = groupHaltStep(n);
    return s?.step?.halt ? haltMark(s) : null;
  }
  if (n.kind !== "stop" || !n.step?.halt) return null;
  return n.state === "paused"
    ? { glyph: "⏸", title: "Halt point — the route paused here. Carry on to go on" }
    : { glyph: "⏸", title: "Halt point — the route pauses here before doing this stop" };
}

const note = (label: string): StopMenuItem => ({ key: "note", label, title: label, note: true });

/** 🗑 on a stop, by where it is on a live line: the one the flight is at
 * (removing it pauses the flight right before the stop that followed), one
 * that has flown, or one still ahead; a group takes its sub-steps with it. */
export function removeTool(n: DiagramNode, here = false): StopMenuItem {
  if (n.kind === "group") {
    const k = n.group?.total || 0;
    return { key: "remove", label: "Remove", title: "Remove this group and its " + k + (k === 1 ? " stop" : " stops") + " from the route", danger: true };
  }
  if (here) return { key: "remove", label: "Remove — the route pauses here", title: "Remove this stop — the route pauses right before the stop that followed it", danger: true };
  if (flownState(n.state)) return { key: "remove", label: "Remove from the route", title: "Take this finished stop off the route", danger: true };
  return { key: "remove", label: "Remove", title: "Take this stop off the route (Delete)", danger: true };
}

/** A group box's menu: Pause / Carry on while the flight is in it, its
 * dialog, open / fold its sub-steps on the line, its halt point, Remove (the
 * whole group). Earlier / Later wait for a later
 * round (a group does not move yet). */
function groupItems(n: DiagramNode, ctx: StopMenuCtx): StopMenuItem[] {
  const { can } = ctx;
  const items: StopMenuItem[] = [];
  // The flight is in it: Pause / Carry on.
  const hold = ctx.here && ctx.live && FLIGHT_HERE.has(n.state) ? flightHold(n) : null;
  if (hold) items.push(hold);
  if (can.edit) items.push({ key: "edit", label: n.editable ? "Edit" : "Details", title: "Open this group and its stops (Enter)" });
  if (can.group)
    items.push(
      ctx.expanded
        ? { key: "expand", label: "Fold its stops", title: "Draw it as one stop again" }
        : { key: "expand", label: "Show its stops", title: "Draw each of its stops on the route" }
    );
  if (can.halt && canHalt(n)) items.push(haltToggle(n));
  if (can.remove) items.push(removeTool(n));
  if (!n.editable && n.state !== "done" && n.state !== "skipped") items.push(note("The route is in this group — only what is still ahead can change"));
  return items;
}
/** A box's right-click menu: an editable stop is edited, moved, duplicated,
 * made a halt point or removed; the flight's current stop offers Pause /
 * Carry on, what its own parts do (Done on a perch, Skip, Retry when halted)
 * and its halt point; a stop that has flown says so; the
 * trailing + adds a stop; a session box opens. */
export function menuItemsFor(n: DiagramNode, ctx: StopMenuCtx): StopMenuItem[] {
  const items = boxItems(n, ctx);
  if (!ctx.jump || (n.kind !== "stop" && n.kind !== "group")) return items;
  // Jump here: the keyboard's and a phone's way to the bird's drag,
  // above Remove and the notes.
  const at = items.findIndex((i) => i.danger || i.note);
  items.splice(at < 0 ? items.length : at, 0, JUMP_ITEM);
  return items;
}

const JUMP_ITEM: StopMenuItem = { key: "jump", label: "Jump here", title: "Move the route to this stop — the halt points after it are armed again" };

function boxItems(n: DiagramNode, ctx: StopMenuCtx): StopMenuItem[] {
  const { can } = ctx;
  if (n.kind === "add") return can.open ? [{ key: "tackon", label: "Add a stop", title: "Pick a stop to add at the end of the route" }] : [];
  if (n.kind === "rung") return [note("Goes as far as — this stop moves as the session ships")];
  if (n.kind === "group") return groupItems(n, ctx);
  if (n.kind !== "stop") return can.open ? [{ key: "open", label: "Open", title: n.session ? "Go to " + n.session : "Open it" }] : [];
  if (n.editable) {
    const items: StopMenuItem[] = [];
    if (can.edit) items.push({ key: "edit", label: "Edit", title: "Open this stop in the inspector (Enter)" });
    if (can.move) {
      items.push({ key: "earlier", label: "Earlier", title: "Move this stop one place earlier on the route (←)", disabled: !ctx.earlier });
      items.push({ key: "later", label: "Later", title: "Move this stop one place later on the route (→)", disabled: !ctx.later });
    }
    if (can.duplicate) items.push({ key: "duplicate", label: "Duplicate", title: "The same stop again, right after this one" });
    if (can.halt && canHalt(n)) items.push(haltToggle(n));
    if (can.remove) items.push(removeTool(n));
    return items;
  }
  if (flownState(n.state)) {
    // A flown stop: read it, take it off a live line.
    const items: StopMenuItem[] = [];
    if (can.edit) items.push({ key: "edit", label: "Details", title: "Open this stop (it has run: read-only)" });
    if (can.remove && ctx.live) items.push(removeTool(n));
    items.push(note("This stop has run"));
    return items;
  }
  if (!ctx.here && (n.state === "pending" || n.state === "edit")) return [note("Still to come — it can't change here")];
  // The flight is here (under way, perched on you, halted or paused).
  const items: StopMenuItem[] = [];
  const hold = ctx.here && ctx.live ? flightHold(n) : null;
  if (hold) items.push(hold);
  if (ctx.live) {
    // A Verify stop moves on when its check is confirmed, not on Done —
    // its Verify card opens instead (the key the hosts already handle).
    const verify = n.step?.kind === "verify";
    // A Review out of fix rounds, the perch asking for the checks owed at
    // the end: their own words, the one that usually works first.
    const offers = n.state === "perched" ? askOffers(n.ask || "") : null;
    if (offers) for (const o of offers) items.push({ key: o.key, label: o.label, title: o.title });
    else if (n.state === "perched")
      items.push(
        verify
          ? { key: "behaviors", label: "Open Verify", title: "Check the steps only you can do, then Confirm verified or Not verified… — the route goes on once you answer" }
          : { key: "done", label: "Done", title: "Mark this stop done — the bird flies on" }
      );
    if (n.state === "halted") items.push({ key: "retry", label: "Retry", title: "Run this stop again" });
    // Any halted stop (round 21; the server's op_fix takes every kind).
    if (n.state === "halted")
      items.push({
        key: "fix",
        label: "Let the agent fix it",
        title: verify ? "Hand the failed steps and their evidence to the agent — it verifies again after its fix" : "Hand the failure to the agent — this stop runs again after its fix",
      });
    items.push({ key: "skip", label: "Skip", title: "Skip this stop — the bird flies on to the next" });
  }
  if (can.halt && canHalt(n, !!ctx.here)) items.push(haltToggle(n));
  if (can.edit) items.push({ key: "edit", label: "Details", title: "Open this stop (the route is here: read-only)" });
  if (can.remove && ctx.live && ctx.here) items.push(removeTool(n, true));
  items.push(note("The route is here — it can't change now"));
  return items;
}

// --- The bird in hand (round 12) ----------------------------------------------------------

/** A click on the bird (Enter / Space on it): ⏸ Pause the flight here while
 * it flies or waits on you, ▶ Carry on while paused, Retry when a stop
 * failed. Null for a bird that is not on a live flight's stop (the session
 * box, a fast-track rung): there is nothing to pause. */
export function birdAction(tone: BirdTone, onFlight: boolean): { key: "pause" | "resume" | "retry"; label: string; title: string } | null {
  if (!onFlight) return null;
  if (tone === "paused") return { key: "resume", label: "Carry on", title: "Carry on — the route goes on from this stop" };
  if (tone === "halted") return { key: "retry", label: "Retry", title: "Retry — run this stop again" };
  return { key: "pause", label: "Pause", title: "Pause the route here — Carry on picks it up again" };
}

/** The flight a bird is dragged on: its id and the step it is at (null when
 * no stop says so). */
export interface JumpFlight {
  id: string;
  at: string | null;
}

/** The step a box jumps the flight to: a stop's own, a group's first
 * sub-step; none for any other box (the session, a branch, the +). */
export function jumpStepOf(n: DiagramNode): FlightStep | null {
  if (n.kind === "group") return n.group?.nodes[0]?.step || n.group?.steps[0] || null;
  return n.kind === "stop" ? n.step || null : null;
}

/** The live flight a line draws, and the step it is at — a group's by the
 * sub-step it is in; null on a line with no live flight. */
export function jumpFlightOf(d: Diagram): JumpFlight | null {
  if (!d.flightId) return null;
  for (const n of d.nodes) {
    if (n.kind === "group" && n.group?.current?.step) return { id: d.flightId, at: n.group.current.step.id };
    if (n.kind === "stop" && n.step && FLIGHT_HERE.has(n.state)) return { id: d.flightId, at: n.step.id };
  }
  return { id: d.flightId, at: null };
}

/** Whether the bird may be dropped on a box: any stop of the flight,
 * earlier or later — a group by its first sub-step — but the one it is at
 * (a no-op), and never the session, a branch or the +. */
export function canJumpTo(flight: JumpFlight | null, n: DiagramNode): boolean {
  if (!flight?.id || n.row !== "main") return false;
  const step = jumpStepOf(n);
  return !!step && step.id !== flight.at;
}

/** Every box of a line the bird can be dropped on, in line order. */
export function jumpTargets(d: Diagram, flight: JumpFlight | null = jumpFlightOf(d)): DiagramNode[] {
  return flight ? d.nodes.filter((n) => canJumpTo(flight, n)) : [];
}

// --- What each option does (the stop form's hints) ------------------------------------

/** Every option of every stop kind in plain words — what it makes the route
 * do, its default and its allowed range — keyed `kind.field`. The server's
 * kinds (backend flights.py STEP_KINDS and flight_kinds/) are the truth for
 * the defaults and ranges; it refuses a number out of range or an unknown
 * value, so the number fields clamp to `FIELD_RANGE`. Verify's own fields
 * are explained by its editor (VerifyStopFields). */
export const FIELD_HINT: Readonly<Record<string, string>> = {
  "agent.prompt": "Typed to the session's agent when the route gets here; the stop is done once its turn has settled. {ticket} {pr} {branch} {subject} are filled in.",
  "agent.fresh": "Off: this session's agent does it. On: a new session in this repo takes the prompt, with the CLI and model below.",
  "agent.join": "On: the stop also waits until every worker the agent started has finished. Default on.",
  "agent.program": "The new session's CLI (claude, codex…). Empty: the same as this session.",
  "agent.model": "The new session's model. Empty: the same as this session.",
  "split.prompts": "One worker per line, all started at once. {index} and {n} are filled in.",
  "split.count": "With a single prompt: how many workers get it. 1 to 20, default 1.",
  "split.model": "The workers' model. Empty: their CLI's default.",
  "split.fence_only": "Each worker may change only these paths (comma-separated). Empty: anything.",
  "split.report_back": "On: each worker reports its result back to this session when it is done. Default on.",
  "join.of": "All workers = every worker this session has; Its Split's workers = only the ones a Split before it started.",
  "join.on_failed": "Fail this stop = a failed worker fails it (then “If it fails” decides); Go on = it carries on with the others.",
  "ship.lane": "How far the work goes: commit only, push, open the PR, or merge it once CI is green.",
  "ship.ask_first": "On: the route waits on you before anything leaves this machine. Default off.",
  "ship.message": "A hint for the commit message. Empty: it is written from the changes.",
  "you.text": "What the route shows you. It waits (under Needs you) until you press Done.",
  "you.items": "One item per line — each becomes a tick box.",
  "you.notify": "On: your phone and the bell are told when the route gets here. Default on.",
  "wait.seconds": "1 second to 14 days, default 10 min.",
  "wait.until": "A time of day, 24-hour (18:00): today, or tomorrow once it has passed. A time wins over How long.",
  "reviews.quiet_minutes": "Done once no new review comment has come for this long. 1 to 1440 min, default 10.",
  "reviews.max_rounds": "Each round sends the new comments to the agent and pushes; more comments after this many rounds fail the stop. 1 to 50, default 5.",
  "reviews.include_bots": "Off: comments from bot reviewers are left out. Default on.",
  "reviews.reply_declined": "On: a comment the agent decides not to act on also gets a reply on the PR giving the reason, not only a line in its summary. Default off.",
  "reviews.each_round": "Added to every round's prompt, after the comments — e.g. re-run your code review before the push, reply on the PR to comments you decline. Empty: nothing added.",
  "checks.timeout_minutes": "The stop fails if CI hasn't finished by then; a failing check fails it at once. 1 to 1440 min, default 60.",
  "command.command": "Runs in the session's folder; a non-zero exit fails the stop.",
  "command.timeout_minutes": "Still running then: it is stopped and the stop fails. 1 to 1440 min, default 30.",
  "ticket.state": "Its exact name or id in your tracker. Empty: comment only. Skipped when the route has no ticket.",
  "ticket.comment": "Posted on the route's ticket. Empty: no comment.",
  "notify.text": "Goes to your phone and the bell; the route never waits for it.",
  "loop.back_to": "The route goes back to this stop for each new run.",
  "loop.times": "Runs in all, counting the first. 0 = until stopped (or until “Stop looping when”). 0 to 1000, default 3.",
  "loop.every_seconds": "Runs start at least this far apart; 0 = straight away. Up to 7 days.",
  "loop.wait_limits": "On: while the agent is at a usage limit the loop waits instead of going round. Default on.",
  "loop.fresh": "On: the agent's context is cleared before each run (when its CLI can).",
  "loop.until": "Checked before each run; once true the loop ends. Empty: only the count stops it. ⓘ lists the conditions.",
  "loop.prompt": "Typed to the agent at each run. Empty: the session's last prompt again.",
  "budget.max_usd": "The session's estimated spend. Empty: no dollar limit.",
  "budget.max_minutes": "How long the route has been running. 0 = no limit; up to 10080 min (7 days).",
  "budget.max_tokens": "Fresh tokens (in, out, cache writes). 0 = no limit.",
  "budget.scope": "Whole route = it keeps watching every later stop and fails the one running when a limit is passed; Here only = it checks once, at this stop.",
  "call.method": "GET sends no body.",
  "call.url": "http(s)://…; {branch} {pr} {ticket} {subject} are filled in.",
  "call.headers": "One “Name: value” per line.",
  "call.body": "Sent unless the method is GET; {receipt} = the last finished stop's result.",
  "call.expect": "The stop is done when the HTTP status starts with this (2 = any 2xx); any other status fails it.",
  "call.timeout_minutes": "No answer by then fails the stop. 1 to 60 min, default 5.",
  "checkpoint.note": "Only names it. The stop records the commit and any uncommitted work, so you can rewind to it later.",
  "handoff.wait": "Off: this stop is done as soon as the other route takes off. On: it waits for that route to finish, and fails if it halts or is cancelled.",
  "judge.question": "Asked about the work; the answer is recorded on the stop.",
  "judge.options": "Comma-separated, up to 12. An answer outside them fails the stop. Default: yes, no.",
  "judge.by": "model = a one-off model reads the diff and answers; agent = the session's agent answers after its turn.",
  "land.delete_branch": "Deletes the local branch once merged (never the default branch). Default on.",
  "land.remote_branch": "Deletes it on origin too. Default on.",
  "land.close": "delete = removes the session and its folder (it closes instead when there is uncommitted work); close = keeps the folder.",
  "listen.match": "Empty: anything that arrives counts.",
  "listen.timeout_minutes": "Nothing by then fails the stop. 0 = wait for ever; up to 10080 min (7 days).",
  "preview.command": "Runs in the session's folder; the URL it prints last becomes {preview}.",
  "preview.timeout_minutes": "1 to 1440 min, default 20.",
  "preview.stop_command": "Runs when the route ends or is cancelled. Empty: the preview is left up.",
  "review.program": "The reviewer's CLI. Empty: the same as this session.",
  "review.model": "The reviewer's model. Empty: its CLI's default.",
  "review.focus": "Added to what the reviewer is asked to look at.",
  "review.block": "On: any finding fails the stop (then “If it fails” decides). Off: findings are only recorded. Default on.",
  "review.keep": "Off: the reviewer session is closed when it has answered.",
  "review.rounds": "How many times the findings go back to the agent to fix or rebut (with “If it fails” on the agent). 1 to 10, default 3.",
  "review.unresolved": "Ask me: the route waits on you with the open findings — Give it another round, or Carry on. Carry on: the stop finishes with them on record.",
  "verify.ask": "Carry on, ask me at the end: the route keeps going and asks you for every check once, before it finishes.",
  "screenshot.urls": "{preview} = the Preview stop's URL; lines that aren't http(s) are skipped.",
  "screenshot.widths": "Comma-separated, each 200 to 4000 px. Default 1280, 390.",
  "screenshot.timeout_minutes": "1 to 1440 min, default 10.",
  "screenshot.comment_pr": "Needs a PR and a GitHub token; without them the stop says why it didn't post.",
  "someone.who": "A peer link's name. Empty: the webhook below is used.",
  "someone.text": "Empty: the stop's title is asked. The route waits until they press Done.",
  "someone.items": "One per line, shown as a list in the ask.",
  "someone.webhook": "A Slack incoming webhook or any URL that takes a JSON POST.",
  "sync.with": "Empty: the default branch (main).",
  "sync.how": "rebase replays this branch on top of it; merge adds a merge commit. A conflict fails the stop, listing the files.",
  "sync.fetch": "On: it is fetched from origin first. Default on.",
  "test.command": "Empty: the project's tests are found (pytest, npm test, go test, cargo test).",
  "test.expect": "pass = done when every test passes; fail = done when something fails (to show a bug is real).",
  "test.rerun_failures": "1 to 3 failures get one more run, and that run decides. Default on.",
  "test.timeout_minutes": "1 to 1440 min, default 30.",
};

/** A number option's allowed range, `kind.field` → [lowest, highest] — the
 * server's own clamps (seconds for a duration). */
export const FIELD_RANGE: Readonly<Record<string, readonly [number, number]>> = {
  "split.count": [1, 20],
  "wait.seconds": [1, 60 * 60 * 24 * 14],
  "reviews.quiet_minutes": [1, 1440],
  "reviews.max_rounds": [1, 50],
  "review.rounds": [1, 10],
  "checks.timeout_minutes": [1, 1440],
  "command.timeout_minutes": [1, 1440],
  "loop.times": [0, 1000],
  "loop.every_seconds": [0, 60 * 60 * 24 * 7],
  "budget.max_minutes": [0, 10080],
  "budget.max_tokens": [0, 1e12],
  "call.timeout_minutes": [1, 60],
  "listen.timeout_minutes": [0, 10080],
  "preview.timeout_minutes": [1, 1440],
  "screenshot.timeout_minutes": [1, 1440],
  "test.timeout_minutes": [1, 1440],
};

/** `kind.field`'s hint, "" when it has none. */
export const fieldHint = (kind: string, field: string): string => FIELD_HINT[kind + "." + field] || "";

/** A number typed into `kind.field`, kept inside its range ("" stays ""). */
export function clampField(kind: string, field: string, v: number | ""): number | "" {
  const r = FIELD_RANGE[kind + "." + field];
  if (v === "" || !r) return v;
  return Math.min(r[1], Math.max(r[0], v));
}

/** What "If it fails" makes the route do, for the pick — under the choice. */
export function onFailHint(k: OnFail, kind = ""): string {
  if (kind === "verify") return capital(VERIFY_ON_FAIL[k]) + ".";
  switch (k) {
    case "agent":
      return "The failure goes to the agent; the stop runs again when its turn ends (up to " + FIX_ROUNDS + " rounds, then the route halts).";
    case "skip":
      return "The route goes on as if this stop was not there.";
    case "you":
      return "The route waits on you, amber, showing the failure.";
    default:
      return "The route stops here, red; you Retry or Skip.";
  }
}

const capital = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

/** The parts of a stop that change how the route treats it, read back as one
 * sentence: its halt point, its "Only when" and what a failure does — "The
 * route pauses here first (a halt point). It runs only when weekday —
 * otherwise it is skipped. If it fails, the route halts, red." `body`
 * replaces the failure part with a sentence of the stop's own that already
 * says it (a Verify stop's `verifySentence`). */
export function stopSentence(step: Pick<FlightStep, "kind" | "halt" | "when" | "on_fail">, fallback: OnFail = "halt", body = ""): string {
  const out: string[] = [];
  if (step.halt) out.push("The route pauses here first (a halt point) — Carry on to run it.");
  const when = (step.when || "").trim();
  if (when) out.push("It runs only when " + when + " — otherwise it is skipped.");
  if (body) {
    out.push(body);
    return out.join(" ");
  }
  const k = (step.on_fail || fallback) as OnFail;
  out.push(
    "If it fails, " +
      (k === "agent" ? "the agent gets the failure and the stop runs again, up to " + FIX_ROUNDS + " rounds" : k === "skip" ? "the route goes on without it" : k === "you" ? "the route waits on you" : "the route halts, red") +
      "."
  );
  return out.join(" ");
}
