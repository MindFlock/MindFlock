/** The flyway canvas's model — pure, so the Flyways editor, the Sky, the
 * pane's Flight tab and the tests all read one rule (docs/flights.md "The
 * session diagram"). `birdPerch` says where the Sky's bird sits on a line.
 *
 * A canvas is a list of BOXES on a line, read left to right (top to bottom on
 * a phone), joined by drawn connectors — no ports and no manual wiring: the
 * order IS the sequence. Three builders feed it:
 *
 *  - `flywayDiagram`  a flyway being edited: its stops, then the trailing +.
 *  - `flightDiagram`  a flight in the air: its snapshot stops with their live
 *                     state (done ✓ dimmed, current pulsing, perched gold,
 *                     halted red, skipped struck through, pending outlined).
 *  - `sessionDiagram` a SESSION's live diagram, composed from `/api/instances`
 *                     rows alone (plus the full flight when it is cached): its
 *                     parent above, the session box, its children branching
 *                     below, then the fast-track ladder's rungs (armed and no
 *                     flight) or the flight's stops, and always a trailing +
 *                     ("Add a stop").
 *
 * A child spawned DURING one of the flight's stops (its row's `flight.branch`
 * names that stop) is drawn as a BRANCH instead of below the session: the
 * line fans out from that stop to one box per child, stacked in a column,
 * and fans back in at the next box — the join point, which reads "waiting for
 * 2 of 5" while the parent's flight waits on them (docs/flights-branches.md).
 * Round 12: every OTHER child hangs the same way, in parallel rows, off the
 * line's current place (the flight's current stop, the fast-track's current
 * rung, else the session box) and converges at the next box — it stays a
 * `child` box (row "below", last in reading order) with a `branch` link; the
 * flight does not wait on it. Each working or asking branch has a bird of its
 * own (`birdPerches`).
 *
 * Consecutive stops sharing `step.group.id` are ONE `group` box (a stop with
 * sub-steps, `collapseGroups`) unless the line expands it; the canvas counts
 * a group box as one place (`drawnIndexes` / `drawnCanvas` map its places to
 * the flight's indexes and back).
 *
 * Round 20: consecutive stops sharing `step.parallel.id` are ONE parallel
 * group — they fly side by side. Each member stays a stop box (marked
 * `parallel: {id, index, size}`); the line forks into them (`fan` edges from
 * the place before) and joins after them (`join` edges into the place after),
 * the same edge kinds a Split's branches use (`lineEdges`). `layoutDiagram`
 * stacks a group's members in one column.
 *
 * `layoutDiagram` places the boxes and draws the connectors as SVG paths from
 * fixed box sizes, so the markup (and a test of it) needs no DOM measuring. */

import type { AutopilotRun, Instance } from "../api/types";
import { verifyStopFace, type BehaviorTone } from "./behaviors";
import { verifyFace } from "./verifyStop";
import { LANE_LABEL, normalizeLane, type Lane } from "./autopilot";
import { deriveSessionState } from "./sessionState";
import { windowName } from "./windowName";
import { riskOf, type RiskTag } from "./stopRisk";
import { stopSentence } from "./stopEditing";
import { stopAskOf, type StopAsk } from "./stopAsks";
import {
  defaultOnFail,
  isLive,
  isShown,
  newStep,
  parallelSpan,
  perchLabel,
  stepActor,
  stepStateOf,
  stripDots,
  youItems,
  type Flight,
  type FlightReturn,
  type FlightRow,
  type FlightStep,
  type StepFix,
  type StepKindDef,
  type StepRunState,
} from "./flights";

// --- The model ----------------------------------------------------------------

/** `flown` stands for the stops a flight has already flown (two or more,
 * done or skipped, before its current one), folded into one pill. `group`
 * is a stop with sub-steps: consecutive steps sharing `step.group.id`, drawn
 * as ONE box until its line expands it (docs/flights.md "Round 11"). */
export type DiagramNodeKind = "parent" | "session" | "child" | "branch" | "rung" | "stop" | "add" | "flown" | "group" | "behaviors";

/** What a box shows. `edit` is a flyway's stop being drawn (no run yet). */
export type DiagramNodeState =
  | "edit"
  | "done"
  | "current"
  | "perched"
  | "halted"
  | "paused"
  | "pending"
  | "skipped"
  | "failed"
  | "session";

/** Where a box sits: on the line, above the session (its parent), on the
 * branch below it (its children), or in a column of parallel branches between
 * two boxes of the line (children spawned at a stop, joining at the next). */
export type DiagramRow = "main" | "above" | "below" | "branch";

/** How a branch (a child working in parallel) is doing. `gone` children leave
 * the diagram; the word is the join detail's. */
export type BranchState = "working" | "asking" | "done" | "failed" | "gone";

/** The join's count, from the parent's `flight.join`. */
export interface JoinCount {
  waiting: number;
  total: number;
}

/** A group box's sub-steps: the group, its steps in line order, their own
 * stop nodes (with their live states) and how far it has flown. */
export interface GroupInfo {
  id: string;
  name: string;
  plan_id: string;
  steps: FlightStep[];
  nodes: DiagramNode[];
  /** Sub-steps done or skipped, of `total`. */
  done: number;
  total: number;
  /** While it flies: the sub-step the flight is at (its title is the box's
   * second line). */
  current?: DiagramNode;
}

export interface DiagramNode {
  id: string;
  kind: DiagramNodeKind;
  row: DiagramRow;
  title: string;
  /** One line under the title: a stop's config summary, a session's headline. */
  sub: string;
  state: DiagramNodeState;
  /** The icon to draw: a stop's kind, "session", "rung", "add". */
  icon: string;
  /** A stop that waits on you. */
  perch?: boolean;
  /** parent / session / child: its session title (a click selects it). */
  session?: string;
  /** A session box's state chip (lib/sessionState's label and tone). */
  chip?: { label: string; tone: string };
  /** A child's mini ladder: its flight's dots or its fast-track rungs. */
  dots?: string;
  /** stop: the stop itself and its index in its flyway / flight. */
  step?: FlightStep;
  index?: number;
  /** A pending stop of a live flight (or any stop being drawn): the inspector
   * edits it in place. Done / current boxes are read-only. */
  editable?: boolean;
  /** Why it stopped (a halted stop or rung), or why a stop whose failure was
   * handed to you waits (`detail.failed_reason`). */
  reason?: string;
  /** The tooltip's second half, after `sub`: a current stop's config
   * summary when `sub` says what it is doing right now (the server's live
   * note), and a stop's options — halt point, Only when, If it fails — when
   * it has any (`stopOptionsLine`). The tile's face never shows it. */
  summary?: string;
  /** A branch box: the stop it fans out of, the box it joins at, its state
   * and its own line in brief ("2/4 stops · Push"). */
  branch?: { from: string; to: string; state: BranchState; inner: string; live?: "working" | "you" };
  /** A join point while its branches are out: "waiting for 2 of 5". */
  join?: JoinCount;
  /** flown: how many stops it folds, and their titles in line order. */
  count?: number;
  flown?: string[];
  /** group: its sub-steps (`index` is its first sub-step's). */
  group?: GroupInfo;
  /** A sub-step of a group its line has expanded: drawn as a normal stop,
   * inside a thin outline labelled with the group's name. */
  inGroup?: { id: string; name: string; first: boolean; last: boolean };
  /** Round 15 looks: what the stop touches outside its worktree (lib/stopRisk
   * `riskOf`: the box's left-edge stripe, one 3px band per tag). A group
   * wears the union of its sub-steps'. */
  risk?: RiskTag[];
  /** A flown stop's receipt (`detail.receipt`, ≤ 80 chars: "312 passed",
   * "PR #291"), under the title at mid and near. */
  receipt?: string;
  /** A stop parked on you with more than a Done to give (lib/stopAsks): a
   * Review out of fix rounds with findings still open ("unresolved"), the
   * perch asking for the checks owed at the end ("owed"). */
  ask?: StopAsk;
  /** Its sentence (the server's note: "2 findings still open after 3
   * rounds"), the tile's line under its name. */
  askNote?: string;
  /** A stop the flight is at that waits for a moment (a Wait's timer
   * `detail.until`, a paced Loop's `detail.next_at`): epoch seconds, for the
   * tile's live "12:34 left". */
  until?: number;
  /** Round 18: where the Sky draws a node that is not a step (a sub-session,
   * a worker), chain-local — kept per device (lib/skyNodePos). */
  pos?: { x: number; y: number };
  /** The stop's `when` condition, when it has one (the "if" chip). */
  when?: string;
  /** Round 16: a Loop stop's count (its return arc's pill). */
  loop?: LoopMark;
  /** Round 20: a member of a parallel group — its id (`step.parallel.id`),
   * its place in the group (0-based) and the group's size. */
  parallel?: ParallelMark;
  /** The Behaviors tile (docs/behaviors.md §1): the `behaviors` node, or the
   * chain's Verify stop standing in for it — the session's behaviors in brief. */
  behaviors?: BehaviorsMark;
  /** A stop that may send work back to the agent (docs/vision/fixwire.md):
   * where to, why, and the fix under way or just over (its return wire). */
  ret?: ReturnMark;
  /** A Ship stop the agent did itself (`detail.by` "agent": it had already
   * committed, pushed or opened the PR) — its face says "by the agent". */
  by?: "agent";
}

/** A stop's return wire as drawn: the server's `returns` entry (`to`,
 * `case`, `of`), the stop's `detail.fix` when it has one, and what the wire
 * says now — `case` (quiet: why it would go back), `fixing` (the agent is
 * fixing it), `fixed` (just fixed, until the route passes the next stop),
 * `halted` (it ran out of rounds). */
export interface ReturnMark {
  to: string;
  case: string;
  of: number | null;
  fix?: StepFix;
  shown: "case" | "fixing" | "fixed" | "halted";
}

/** A session's behaviors as its tile draws them (lib/behaviors
 * `behaviorSummary`): the plan, the tile's sub line, its tone. */
export interface BehaviorsMark {
  planId: string;
  /** "3 of 5 verified", "Needs you · 2", "Failed · 1", "Writing…", "Not checked". */
  short: string;
  status: string;
  tone: BehaviorTone;
}

/** Round 20: where a stop sits in its parallel group. */
export interface ParallelMark {
  id: string;
  index: number;
  size: number;
}

/** A Loop's count, for its return arc: `runs` completed so far
 * (`detail.runs`), `times` in all (0 = until stopped), and how the loop
 * stands — `pending` (not reached: the arc is dashed), `running` (the flight
 * is inside it, waiting at it, or back at an earlier stop for another run),
 * `done` (it let the flight on). */
export interface LoopMark {
  runs: number;
  times: number;
  state: "pending" | "running" | "done";
}

/** `fan` runs from a stop out to a branch, `join` from a branch into the
 * join point, `loop` from the last stop a Loop repeats back to the first one
 * (a return wire under the line — the Loop itself draws no box). */
export type DiagramEdgeKind = "line" | "up" | "branch" | "add" | "fan" | "join" | "loop" | "fix";

export interface DiagramEdge {
  from: string;
  to: string;
  kind: DiagramEdgeKind;
  /** A fan-in whose branch has arrived (or whose join let go): drawn done. */
  done?: boolean;
  /** A `loop` edge: its Loop's count (the pill on the return wire). */
  loop?: LoopMark;
  /** A `loop` edge: the Loop stop it stands for (in the flight's indexes) —
   * the wire's pill opens, edits and removes it. */
  via?: DiagramNode;
  /** A `fix` edge (a stop's return wire back to the agent): its mark. */
  ret?: ReturnMark;
}

export interface Diagram {
  nodes: DiagramNode[];
  edges: DiagramEdge[];
  /** The live flight the stops belong to ("" = none). */
  flightId: string;
  /** What follows the session box: the flight's stops, the fast-track ladder,
   * or nothing yet. */
  mode: "flight" | "ladder" | "none";
  /** Stops at index ≥ this may have a stop inserted before them (a live
   * flight: only after its current stop; a flyway: anywhere). */
  insertFrom: number;
}

// --- A stop in one line ---------------------------------------------------------

const clip = (s: string, n = 34) => {
  const t = s.replace(/\s+/g, " ").trim();
  return t.length > n ? t.slice(0, n - 1).trimEnd() + "…" : t;
};
const num = (v: unknown, d: number) => {
  const n = Number(v);
  return Number.isFinite(n) && v !== "" && v !== null && v !== undefined ? n : d;
};

/** A bare Wait for me stop's line while it is ahead or waiting. */
export const YOU_WAITS = "waits for your Done";

/** A bare Wait for me stop's line once it has flown (polish pass 5): "waits
 * for your Done" is a promise, wrong under a ✓ — a done one says "done", a
 * skipped one "skipped". "" for every other stop (its summary stays). */
export function youFlownSub(step: Pick<FlightStep, "kind" | "config">, state: string): string {
  if (step.kind !== "you" || (state !== "done" && state !== "skipped")) return "";
  return stopSummary(step) === YOU_WAITS ? state : "";
}

/** A stop's config in one line, the way its box says it: "push", "10 min
 * quiet", "by the agent", "3 to tick". Never an internal id: a kind it has
 * no words for says nothing (round 21: the judges saw "land"). */
export function stopSummary(step: Pick<FlightStep, "kind" | "config">): string {
  const c = step.config || {};
  switch (step.kind) {
    case "agent": {
      const p = String(c.prompt ?? "").trim();
      return (p ? "“" + clip(p, 28) + "”" : "works its turn") + (c.fresh ? " · new session" : "");
    }
    case "ship": {
      const lane = normalizeLane(String(c.lane ?? "push")) || "push";
      return (lane === "leave" ? "off" : LANE_LABEL[lane].toLowerCase()) + (c.ask_first ? " · asks first" : "");
    }
    case "verify":
      // "Verify on sandbox · agent, then you · 15 min after the stop before"
      // (lib/verifyStop.ts; never the internal `who: both`).
      return verifyFace(c);
    case "land":
      // Finish (backend flight_kinds/land.py): once merged, the tidy-up.
      return "once merged: " + (String(c.close || "delete") === "close" ? "closes" : "deletes") + " the session" + (c.delete_branch === false ? " · keeps the branch" : "");
    case "you": {
      const items = youItems(step as FlightStep);
      if (items.length) return items.length + (items.length === 1 ? " item" : " items") + " to tick";
      const t = String(c.text ?? "").trim();
      return t ? clip(t) : YOU_WAITS;
    }
    case "wait": {
      const until = String(c.until ?? "").trim();
      // Seconds since 2026-11-02 (an older `minutes` still reads).
      const secs = num(c.seconds, 0) > 0 ? num(c.seconds, 0) : num(c.minutes, 10) * 60;
      return until ? "until " + until : durationWord(secs);
    }
    case "reviews":
      return num(c.quiet_minutes, 10) + " min quiet";
    case "checks":
      return "up to " + num(c.timeout_minutes, 60) + " min";
    case "command": {
      const cmd = String(c.command ?? "").trim();
      return cmd ? clip(cmd, 30) : "no command yet";
    }
    case "ticket": {
      const st = String(c.state ?? "").trim();
      return st ? "move to " + st : "comment";
    }
    case "notify": {
      const t = String(c.text ?? "").trim();
      return t ? clip(t) : "a ping";
    }
    case "split": {
      const list = Array.isArray(c.prompts) ? c.prompts.map(String) : String(c.prompts ?? "").split("\n");
      const lines = list.filter((l) => l.trim()).length;
      const n = lines > 1 ? lines : Math.max(1, num(c.count, 1));
      return n + (n === 1 ? " worker" : " workers") + " in parallel";
    }
    case "loop": {
      const times = Math.max(0, num(c.times, 3));
      const until = String(c.until ?? "").trim();
      const every = num(c.every_seconds, 0) > 0 ? num(c.every_seconds, 0) : Math.max(0, num(c.every_minutes, 0)) * 60;
      const parts = [times > 0 ? times + (times === 1 ? " run" : " runs") : until ? "" : "until stopped"];
      if (until) parts.push("until " + clip(until, 22));
      if (every) parts.push("every " + durationWord(every));
      if (c.fresh) parts.push("fresh context");
      return parts.filter(Boolean).join(" · ");
    }
    case "join":
      return (c.of === "spawned" ? "waits for its split's workers" : "waits for every worker") + (c.on_failed === "continue" ? " · goes on past a failure" : "");
    default:
      return "";
  }
}

/** A stop's second line under its title: `sub`, unless it only echoes the
 * title ("Push / push", "Merge when green / merge when green") — then none
 * (round 21). */
export function stopSub(title: string, sub: string): string {
  const norm = (t: string) => t.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
  return sub && norm(sub) !== norm(title) ? sub : "";
}

/** A duration in seconds the way a box says it: "45 s", "10 min", "1 min 30
 * s", "2 h", "1 h 30 min" (the server's `duration_word`). */
export function durationWord(seconds: number): string {
  const n = Math.round(Math.max(0, seconds));
  if (n < 60) return n + " s";
  if (n % 3600 === 0) return n / 3600 + " h";
  if (n < 3600) return n % 60 === 0 ? n / 60 + " min" : Math.floor(n / 60) + " min " + (n % 60) + " s";
  return Math.floor(n / 3600) + " h " + Math.floor((n % 3600) / 60) + " min";
}

/** Minutes the way a box says them: "30 min", "2 h", "1 h 30 min", "1 d". */
export function minutesWord(m: number): string {
  const n = Math.round(m);
  if (n >= 1440 && n % 1440 === 0) return n / 1440 + " d";
  if (n < 60) return n + " min";
  return Math.floor(n / 60) + " h" + (n % 60 ? " " + (n % 60) + " min" : "");
}

// --- The Loop (round 16) ---------------------------------------------------------

/** A Loop step's count from its config and its run record. */
export function loopMark(step: Pick<FlightStep, "config">, rec?: Pick<StepRunState, "state" | "detail"> | null): LoopMark {
  const times = Math.max(0, Math.floor(num(step.config?.times, 3)));
  const runs = Math.max(0, Math.floor(num(rec?.detail?.runs, 0)));
  const st = rec?.state;
  const state: LoopMark["state"] = st === "done" || st === "skipped" ? "done" : st === "running" || st === "waiting_you" || runs > 0 ? "running" : "pending";
  return { runs, times, state };
}

/** The pill on a Loop's arc: the run under way of all of them — "2 / 3",
 * "2 / ∞" — once the loop is reached or has run; "" before. */
export function loopPill(m: LoopMark): string {
  if (m.state === "pending" && m.runs === 0) return "";
  return Math.max(1, m.runs + 1) + " / " + (m.times > 0 ? m.times : "∞");
}

/** The pill's tooltip: "Run 2 of 3", "Run 2 — it loops until stopped". */
export function loopPillTitle(m: LoopMark): string {
  const run = Math.max(1, m.runs + 1);
  return (m.state === "done" ? "Looped: run " : "Run ") + run + (m.times > 0 ? " of " + m.times : " — it loops until stopped");
}

/** A Loop box's second line while it flies (or has sent the flight back):
 * the judge's note when it wrote one — "waiting on the limit", "run 2 of 3 ·
 * next in 12 min" — else the counter, "run 2 of 3" / "run 2 — until <until>". */
export function loopLine(step: Pick<FlightStep, "config">, rec?: Pick<StepRunState, "state" | "detail" | "note"> | null): string {
  const m = loopMark(step, rec);
  const run = Math.max(1, m.runs + 1);
  const until = String(step.config?.until ?? "").trim();
  const counter = m.times > 0 ? "run " + run + " of " + m.times : "run " + run + (until ? " — until " + clip(until, 22) : "");
  const note = String(rec?.note ?? "").replace(/\s+/g, " ").trim();
  if (!note) return counter;
  if (/usage limit|the limit/i.test(note)) return "waiting on the limit";
  const next = /^next run in\s+(.+)$/i.exec(note);
  if (next) return counter + " · next in " + next[1];
  if (/^run \d/i.test(note)) return clip(note, 40);
  return counter + " · " + clip(note, 28);
}

/** The line's return wires: for every Loop stop of `line`, an edge from the
 * last stop before it (the end of what it repeats) back to the box drawn for
 * its `back_to` — `""` is the line's first stop, or its own group's first
 * sub-step when it is in one; `"agent"` (`LOOP_AGENT`) is the session box
 * (`head`: a wire from the end of what it repeats into the session tile).
 * The edge carries the Loop node (`via`). A Loop
 * with nothing before it to repeat (or whose target is gone) draws no wire —
 * it stays a box. Folding (the flown pill, a collapsed group) re-aims the
 * wires at the box that holds their ends (`foldRun`). */
export function loopEdges(line: readonly DiagramNode[], head?: string): DiagramEdge[] {
  const stops = line.filter((n) => n.row === "main" && n.kind === "stop" && n.step);
  const out: DiagramEdge[] = [];
  stops.forEach((n, i) => {
    const s = n.step!;
    if (s.kind !== "loop") return;
    const before = stops.slice(0, i).filter((b) => b.step!.kind !== "loop");
    const back = String(s.config?.back_to ?? "").trim();
    // Back to the AGENT (`back_to: "agent"`): the session's agent is
    // re-prompted, then the chain runs again — a wire from the end of what
    // it repeats into the session tile. Only a session's chain has one.
    if (back === LOOP_AGENT) {
      if (!head) return;
      const last = before[before.length - 1];
      out.push({ from: last ? last.id : head, to: head, kind: "loop", loop: n.loop || loopMark(s), via: n });
      return;
    }
    const gid = s.group?.id;
    const target = back ? before.find((b) => b.step!.id === back) : gid ? before.find((b) => b.step!.group?.id === gid) : before[0];
    const end = before[before.length - 1];
    // First on a session's chain (nothing before it, aimed nowhere): it
    // repeats the session's own prompt — a wire from the session tile back
    // into it (backend flight_kinds/loop `repeats_prompt`).
    if (!before.length && !back && head) {
      out.push({ from: head, to: head, kind: "loop", loop: n.loop || loopMark(s), via: n });
      return;
    }
    if (!target || !end) return;
    out.push({ from: end.id, to: target.id, kind: "loop", loop: n.loop || loopMark(s), via: n });
  });
  return out;
}

/** The line with its Loops lifted off it: each Loop that draws a return
 * wire (`loopEdges`) leaves the line — the line runs straight past it — and
 * comes back as that wire. */
export function liftLoops(line: readonly DiagramNode[], head?: string): { line: DiagramNode[]; loops: DiagramEdge[] } {
  const loops = loopEdges(line, head);
  const lifted = new Set(loops.map((e) => e.via!.id));
  return { line: line.filter((n) => !lifted.has(n.id)), loops };
}

/** The Loop the flight is at (deciding whether to go round, waiting out its
 * pace or a limit, halted on it): its wire, else null. */
export function loopAtWork(d: Pick<Diagram, "edges">): DiagramEdge | null {
  return d.edges.find((e) => e.kind === "loop" && !!e.via && !!STOP_TONE[e.via.state]) || null;
}

// --- Drawing a loop by hand --------------------------------------------------------

/** The step a box starts with: a stop's own, a group box's first sub-step —
 * what a Loop drawn back onto that box repeats from. */
export function firstStepOf(n: Pick<DiagramNode, "step" | "group">): FlightStep | undefined {
  return n.step || n.group?.steps[0];
}

/** Where a Loop drawn out of box `n` goes, in the flight's indexes: right
 * after the box (after a group box's last sub-step). */
export function loopSlotAfter(n: Pick<DiagramNode, "index" | "kind" | "group">): number | undefined {
  if (n.index === undefined) return undefined;
  return n.index + (n.kind === "group" ? n.group?.total || 1 : 1);
}

/** A Loop's `back_to` that means the session's AGENT (the backend's
 * contract): the agent is re-prompted (the Loop's `prompt`, else the prompt
 * that started its turn), its turn runs, then the chain runs again from its
 * first stop. Only on a chain that has a session. */
export const LOOP_AGENT = "agent";

/** A Loop step goes back to the agent. */
export const loopsToAgent = (step: Pick<FlightStep, "kind" | "config"> | null | undefined): boolean =>
  !!step && step.kind === "loop" && String(step.config?.back_to ?? "").trim() === LOOP_AGENT;

/** A new Loop step of `def` that repeats from `target` (its `back_to`): a
 * stop, or `LOOP_AGENT` — the session's agent. */
export function loopStepTo(def: StepKindDef, taken: readonly string[], target: FlightStep | typeof LOOP_AGENT): FlightStep {
  const step = newStep(def, taken);
  return { ...step, config: { ...step.config, back_to: target === LOOP_AGENT ? LOOP_AGENT : target.id } };
}

/** The `back_to` a Loop re-aimed at `to` takes: the agent for a session
 * box, else `to`'s first step's id ("" when it has none). */
export function loopBackTo(to: Pick<DiagramNode, "kind" | "step" | "group">): string {
  if (to.kind === "session") return LOOP_AGENT;
  return firstStepOf(to)?.id || "";
}

/** One choice of a stop's "Loop back to…" (its ⋯ menu): the agent, or a stop
 * at or before it. `to` is what the wire would land on. */
export interface LoopChoice {
  key: string;
  label: string;
  title: string;
  agent: boolean;
  /** What the Loop goes after (`n`, or the last of its parallel group) and
   * what it goes back to — a line's `onLoopFrom(from, to)`. */
  from: DiagramNode;
  to: DiagramNode;
}

/** Where a Loop drawn out of `n` may go back to, for its ⋯ menu's "Loop
 * back to…" — exactly what a wire dropped from `n`'s out port may land on:
 * the session's agent (`session`: the line's session box, when it has
 * one) first, then every stop / group box at or before `n` that can
 * repeat (not a Loop). `nodes` is the line in flight order (its main row);
 * `insertFrom` is the line's (a Loop may only go after an open slot).
 * Empty when `n` can't have a Loop after it. */
export function loopChoices(n: DiagramNode, nodes: readonly DiagramNode[], opts: { session?: DiagramNode | null; insertFrom?: number } = {}): LoopChoice[] {
  const canRepeat = (m: DiagramNode) => (m.kind === "stop" ? !!m.step && m.step.kind !== "loop" : m.kind === "group") && m.index !== undefined && m.row === "main";
  if (!canRepeat(n)) return [];
  const from = loopFromOf(n, nodes);
  const slot = loopSlotAfter(from);
  if (slot === undefined || slot < (opts.insertFrom ?? 0)) return [];
  const out: LoopChoice[] = [];
  if (opts.session)
    out.push({ key: "loop-to-agent", label: "The agent (run it again)", title: "After this stop the agent is prompted again, then the route runs again", agent: true, from, to: opts.session });
  for (const m of nodes) {
    if (!canRepeat(m) || m.index! > n.index!) continue;
    out.push({ key: "loop-to:" + m.id, label: m.id === n.id ? "This stop (“" + m.title + "”)" : "“" + m.title + "”", title: "After this stop the route goes back to “" + m.title + "”", agent: false, from, to: m });
  }
  return out;
}

/** "After `n`" for a Loop drawn out of it: `n`, or the last member of the
 * parallel group it is in (a Loop goes after the whole group). */
export function loopFromOf(n: DiagramNode, nodes: readonly DiagramNode[]): DiagramNode {
  const pid = n.parallel?.id;
  if (!pid) return n;
  let last = n;
  for (const m of nodes) if (m.parallel?.id === pid && m.index !== undefined && last.index !== undefined && m.index > last.index) last = m;
  return last;
}

/** A node by id: the line's own, a sub-step of one of its group boxes, or
 * a Loop riding a return wire (`edge.via` — it has no box). */
export function findNode(d: Pick<Diagram, "nodes" | "edges">, id: string | null | undefined): DiagramNode | null {
  if (!id) return null;
  for (const n of d.nodes) {
    if (n.id === id) return n;
    const sub = n.group?.nodes.find((x) => x.id === id);
    if (sub) return sub;
  }
  return d.edges.find((e) => e.via?.id === id)?.via || null;
}

/** The pill on a Loop's wire before it runs: "↻ ×3", "↻ until checks
 * green", "↻ until stopped". */
export function loopIdlePill(m: LoopMark, via?: Pick<DiagramNode, "step"> | null): string {
  const pace = loopPace(via);
  if (m.times > 0) return "↻ repeat ×" + m.times + pace;
  const until = String(via?.step?.config?.until ?? "").trim();
  return (until ? "↻ until " + clip(until, 18) : "↻ until stopped") + pace;
}

/** " · every 30 min" for a paced Loop (`every_seconds`, else the older
 * `every_minutes`), "" otherwise. */
function loopPace(via?: Pick<DiagramNode, "step"> | null): string {
  const c = via?.step?.config || {};
  let secs = Math.floor(num(c.every_seconds, 0));
  if (secs <= 0) secs = Math.floor(num(c.every_minutes, 0)) * 60;
  return secs > 0 ? " · every " + durationWord(secs) : "";
}

/** The case on a running Loop's wire, beside its "2 / 3" pill — what the
 * count does not say: its `until`, its pace ("until checks green · every 30
 * min"); "" when the count says it all. */
export function loopCaption(m: LoopMark, via?: Pick<DiagramNode, "step"> | null): string {
  if (m.state === "pending" && m.runs === 0) return "";
  const until = String(via?.step?.config?.until ?? "").trim();
  const pace = loopPace(via);
  if (until) return "until " + clip(until, 18) + pace;
  return pace ? pace.slice(3) : m.times > 0 ? "" : "until stopped";
}

// --- A stop's return wire back to the agent (docs/vision/fixwire.md) ------------

/** A stop's `detail.fix`, read defensively; undefined when it has none. */
export function fixOf(detail: Record<string, unknown> | null | undefined): StepFix | undefined {
  const f = detail?.fix as Partial<StepFix> | undefined;
  if (!f || typeof f !== "object") return undefined;
  const round = Math.max(1, Math.floor(num(f.round, 1)));
  const of = f.of === null || f.of === undefined ? null : Math.max(0, Math.floor(num(f.of, 0))) || null;
  return {
    active: !!f.active,
    round,
    rounds: Math.max(round, Math.floor(num(f.rounds, round))),
    of,
    reason: String(f.reason ?? ""),
    since: num(f.since, 0),
    to: String(f.to ?? "") || LOOP_AGENT,
    ...(f.kind ? { kind: String(f.kind) } : {}),
    ...(f.outcome === "fixed" || f.outcome === "halted" ? { outcome: f.outcome } : {}),
  };
}

/** Stop `i`'s return mark: the server's `returns` entry and its fix (either
 * makes a wire). `passedNext`: the route is past the stop after it — a fix
 * it just finished goes quiet again. */
export function returnMark(ret: FlightReturn | null | undefined, fix: StepFix | undefined, stopState: DiagramNodeState, passedNext: boolean): ReturnMark | undefined {
  if (!ret && !fix) return undefined;
  const shown: ReturnMark["shown"] = fix?.active
    ? "fixing"
    : fix?.outcome === "fixed" && !passedNext
      ? "fixed"
      : fix?.outcome === "halted" && (stopState === "halted" || stopState === "failed")
        ? "halted"
        : "case";
  return {
    to: ret?.to || fix?.to || LOOP_AGENT,
    case: ret?.case || "if it fails",
    of: ret ? ret.of : (fix?.of ?? null),
    ...(fix ? { fix } : {}),
    shown,
  };
}

/** "fixing 1/2", "fixing round 3" — a fix's round as its label says it. */
export function fixingWord(m: Pick<ReturnMark, "of">, fix: Pick<StepFix, "round">): string {
  return "fixing " + (m.of ? fix.round + "/" + m.of : "round " + fix.round);
}

/** The text on a stop's return wire: quiet, the case ("if pre-commit
 * fails"); while fixing the real reason and the round ("pre-commit failed
 * at ruff · fixing 1/2"); just after, "fixed after 1 round"; out of rounds,
 * "halted after 2 rounds". */
export function returnLabel(m: ReturnMark): string {
  const f = m.fix;
  if (m.shown === "fixing" && f) return (f.reason ? clip(f.reason, 40) + " · " : "") + fixingWord(m, f);
  if (m.shown === "fixed" && f) return "fixed after " + f.rounds + (f.rounds === 1 ? " round" : " rounds");
  if (m.shown === "halted" && f) return "halted after " + f.rounds + (f.rounds === 1 ? " round" : " rounds");
  return m.case;
}

/** The wire's tooltip: the case, and the whole reason while it fixes. */
export function returnTitle(m: ReturnMark, to = "the agent"): string {
  const f = m.fix;
  const cap = m.of ? " (at most " + m.of + (m.of === 1 ? " round)" : " rounds)") : "";
  const base = "If it fails: Let the agent fix it — " + m.case + ", back to " + to + cap;
  if (m.shown === "fixing" && f) return (f.reason || "It failed") + " — the agent is fixing it (round " + f.round + (m.of ? " of " + m.of : "") + ")";
  if (m.shown === "fixed" && f) return "Fixed after " + f.rounds + (f.rounds === 1 ? " round" : " rounds") + (f.reason ? ": " + f.reason : "");
  if (m.shown === "halted" && f) return "Halted after " + f.rounds + (f.rounds === 1 ? " round" : " rounds") + (f.reason ? ": " + f.reason : "");
  return base;
}

/** The face of a stop whose failure is with the agent: "Agent fixing ·
 * round 1". */
export function fixFace(fix: StepFix): string {
  return "Agent fixing · round " + fix.round;
}

/** The line's return wires back to the agent: for every stop of `line` with
 * a return mark (`ret`), an edge from it back to its `to` — an Agent stop
 * drawn before it, else the session tile (`head`). With no head and no such
 * stop it draws none. */
export function returnEdges(line: readonly DiagramNode[], head?: string): DiagramEdge[] {
  const stops = line.filter((n) => n.row === "main" && (n.kind === "stop" || n.kind === "group"));
  const out: DiagramEdge[] = [];
  stops.forEach((n, i) => {
    const m = n.ret;
    if (!m) return;
    const target = m.to === LOOP_AGENT ? undefined : stops.slice(0, i).find((b) => b.step?.id === m.to || b.group?.steps.some((s) => s.id === m.to));
    const to = target ? target.id : head;
    if (!to) return;
    out.push({ from: n.id, to, kind: "fix", ret: m });
  });
  return out;
}

/** A stop's title as its box says it: a perch reads "Perch: <title>". */
export function stopTitle(step: FlightStep, kinds?: readonly StepKindDef[] | null): string {
  return stepActor(step, kinds) === "you" ? perchLabel(step.title) : step.title;
}

// --- A flyway, a flight ---------------------------------------------------------

const stopId = (s: FlightStep, i: number) => "stop:" + (s.id || "i" + i);

/** Round 20: step `i`'s place in its parallel group, or nothing for a step
 * of its own. */
export function parallelMark(steps: readonly FlightStep[], i: number): { parallel?: ParallelMark } {
  const [lo, hi] = parallelSpan(steps, i);
  if (hi - lo < 2) return {};
  return { parallel: { id: steps[i].parallel!.id, index: i - lo, size: hi - lo } };
}

/** The line cut into its places: a parallel group's consecutive members
 * (sharing `parallel.id`) are one place, every other node one of its own. */
export function lineRuns(line: readonly DiagramNode[]): DiagramNode[][] {
  const out: DiagramNode[][] = [];
  for (const n of line) {
    const last = out[out.length - 1];
    const pid = n.parallel?.id;
    if (pid && last && last[0].parallel?.id === pid) last.push(n);
    else out.push([n]);
  }
  return out;
}

const FLOWN_STATES: ReadonlySet<DiagramNodeState> = new Set(["done", "skipped", "failed"]);

/** The line's edges, in line order: one place to the next, a `line` (an
 * `add` into the trailing +). Round 20: into a parallel group the line FORKS
 * — a `fan` edge from the place before to each member — and out of it it
 * JOINS — a `join` edge from each member into the place after (drawn `done`
 * once that member has flown); the + after a group trails its last member
 * only. Two groups in a row join every member of the first into every
 * member of the second. */
export function lineEdges(line: readonly DiagramNode[]): DiagramEdge[] {
  const runs = lineRuns(line);
  const edges: DiagramEdge[] = [];
  for (let k = 1; k < runs.length; k++) {
    const a = runs[k - 1];
    const b = runs[k];
    if (b.length === 1 && b[0].kind === "add") {
      edges.push({ from: a[a.length - 1].id, to: b[0].id, kind: "add" });
      continue;
    }
    for (const x of a)
      for (const y of b) {
        if (a.length > 1) edges.push(FLOWN_STATES.has(x.state) ? { from: x.id, to: y.id, kind: "join", done: true } : { from: x.id, to: y.id, kind: "join" });
        else edges.push({ from: x.id, to: y.id, kind: b.length > 1 ? "fan" : "line" });
      }
  }
  return edges;
}

function chain(nodes: DiagramNode[], edges: DiagramEdge[], line: DiagramNode[]) {
  edges.push(...lineEdges(line));
  nodes.push(...line);
}

/** Where a live flight takes new stops: past its current place (a parallel
 * group at the cursor: past its last member); anywhere on a ready one. */
export function insertFromOf(f: Pick<Flight, "cursor" | "steps"> & { state?: string }): number {
  if (f.state === "ready" || f.state === "grounded") return 0;
  return Math.max(f.cursor + 1, parallelSpan(f.steps, f.cursor)[1]);
}

/** A stop's options read back in lib/stopEditing's sentence — its halt
 * point, "Only when", and "If it fails" — for its tile's tooltip; "" when
 * it has none of them (no halt point, no condition, the kind's own failure
 * handling), so a plain stop's tooltip stays its summary. */
export function stopOptionsLine(s: Pick<FlightStep, "kind" | "halt" | "when" | "on_fail">, kinds?: readonly StepKindDef[] | null): string {
  const fallback = defaultOnFail(kinds?.find((k) => k.kind === s.kind));
  const failSet = !!s.on_fail && s.on_fail !== fallback;
  if (!s.halt && !(s.when || "").trim() && !failSet) return "";
  return stopSentence(s, fallback);
}

function addNode(title: string): DiagramNode {
  return { id: "add", kind: "add", row: "main", title, sub: "", state: "pending", icon: "add" };
}

/** A flyway being drawn: its stops, then the trailing "+". */
export function flywayDiagram(steps: readonly FlightStep[], kinds?: readonly StepKindDef[] | null): Diagram {
  const stops: DiagramNode[] = steps.map((s, i) => ({
    id: stopId(s, i),
    kind: "stop",
    row: "main",
    title: stopTitle(s, kinds),
    sub: stopSub(stopTitle(s, kinds), stopSummary(s)),
    ...(stopOptionsLine(s, kinds) ? { summary: stopOptionsLine(s, kinds) } : {}),
    state: "edit",
    icon: s.kind,
    perch: stepActor(s, kinds) === "you",
    step: s,
    index: i,
    editable: true,
    ...looksOf(s, kinds),
    ...(s.kind === "loop" ? { loop: loopMark(s) } : {}),
    ...parallelMark(steps, i),
  }));
  const { line, loops } = liftLoops(stops);
  line.push(addNode("Add a stop"));
  const nodes: DiagramNode[] = [];
  const edges: DiagramEdge[] = [];
  chain(nodes, edges, line);
  edges.push(...loops);
  return { nodes, edges, flightId: "", mode: "none", insertFrom: 0 };
}

/** A flight's stops with their live state. */
export function flightStops(f: Flight, kinds?: readonly StepKindDef[] | null): DiagramNode[] {
  const live = isLive(f);
  // Stops with no session yet, or built and not yet flying (round 19 ready):
  // nothing has flown, every one can change.
  const grounded = f.state === "grounded" || f.state === "ready";
  // Round 20: the cursor's parallel group — every member flies at once.
  const [glo, ghi] = parallelSpan(f.steps, f.cursor);
  const open = Math.max(f.cursor + 1, ghi);
  return f.steps.map((s, i) => {
    const st = stepStateOf(f, s, i);
    const inGroup = ghi - glo > 1 && i >= glo && i < ghi;
    const atCursor = live && (i === f.cursor || inGroup);
    let state: DiagramNodeState =
      st === "done" ? "done" : st === "skipped" ? "skipped" : st === "failed" ? "failed" : st === "waiting_you" ? "perched" : st === "running" ? "current" : "pending";
    if (inGroup && state === "done") {
      // A member through, its group still flying: it stays done.
    } else if (inGroup && f.state === "halted") {
      if (st === "failed") state = "halted";
    } else if (inGroup && f.state === "paused") {
      if (state === "current" || state === "pending") state = "paused";
    } else if (atCursor && f.state === "halted") state = "halted";
    else if (atCursor && f.state === "paused") state = "paused";
    const rec = f.step_states?.[s.id];
    // A Loop that sent the flight back keeps its record `running` while the
    // flight flies the stops it repeats: the box waits like one ahead (the
    // bird is back there); its arc and second line carry the count.
    const loop = s.kind === "loop" ? loopMark(s, rec) : undefined;
    if (loop && state === "current" && live && i > f.cursor) state = "pending";
    const flown = state === "done" || state === "skipped" || state === "failed";
    const looping = !!loop && !flown && (loop.runs > 0 || (atCursor && state !== "pending"));
    const fix = fixOf(rec?.detail);
    const next = f.steps[i + 1];
    const passedNext = !!next && ["done", "skipped"].includes(stepStateOf(f, next, i + 1));
    const ret = returnMark(f.returns?.[s.id], fix, state, passedNext);
    const summary = stopSub(stopTitle(s, kinds), youFlownSub(s, state) || stopSummary(s));
    // A stop at work says what it is doing right now (the server's note: "the
    // agent is working", "3 new review comments for the agent"), not its
    // settings — those stay in its tooltip (`summary`).
    const note = live && state === "current" && !looping && !fix?.active ? liveNote(rec?.note) : "";
    // A failure handed to you (`on_fail: you`) waits amber: why, on the tile.
    const handed = state === "perched" ? String(rec?.detail?.failed_reason ?? "").trim() : "";
    // The tooltip: the settings a live note stands in for, then the stop's
    // options (halt point, Only when, If it fails) — never on the face.
    const tip = [note && summary && note !== summary ? summary : "", stopOptionsLine(s, kinds)].filter(Boolean).join(" · ");
    return {
      id: stopId(s, i),
      kind: "stop" as const,
      row: "main" as const,
      title: stopTitle(s, kinds),
      sub: fix?.active && state === "current" ? fixFace(fix) : looping ? loopLine(s, rec) : note || summary,
      ...(tip ? { summary: tip } : {}),
      state,
      icon: s.kind,
      perch: stepActor(s, kinds) === "you",
      step: s,
      index: i,
      editable: grounded || (live && state === "pending" && i >= open),
      reason: state === "halted" || state === "failed" ? f.reason || rec?.note || "" : handed,
      ...looksOf(s, kinds),
      ...(flown ? { receipt: receiptOf(rec?.detail) } : {}),
      ...(state === "perched" && stopAskOf(s, rec) ? { ask: stopAskOf(s, rec), askNote: String(rec?.note || "").trim() } : {}),
      ...(loop ? { loop } : {}),
      ...(ret ? { ret } : {}),
      ...(state === "done" && rec?.detail?.by === "agent" ? { by: "agent" as const } : {}),
      ...(!flown && untilOf(rec?.detail) ? { until: untilOf(rec?.detail) } : {}),
      ...parallelMark(f.steps, i),
    };
  });
}

/** What a stop at work is doing right now, in one line for its tile: the
 * server's `note` on its record, clipped ("the agent is verifying its work ·
 * 3 of 7 answered · quiet 6 min"); "" when it wrote none. */
export function liveNote(note: unknown): string {
  return clip(String(note ?? ""), 60);
}

/** Round 15 looks on a stop node: its risk stripe's tags and its `when`
 * (read defensively — the field lands in another task), only when set. */
function looksOf(s: FlightStep, kinds?: readonly StepKindDef[] | null): Pick<DiagramNode, "risk" | "when"> {
  const out: Pick<DiagramNode, "risk" | "when"> = {};
  const risk = riskOf(s, kinds);
  if (risk.length) out.risk = risk;
  const when = String((s as { when?: unknown }).when ?? "").trim();
  if (when) out.when = when;
  return out;
}

/** When a waiting stop is due (epoch seconds): a Wait's `detail.until`, a
 * paced Loop's `detail.next_at`; 0 when it has none. */
export function untilOf(detail: Record<string, unknown> | null | undefined): number {
  const v = Number(detail?.until ?? detail?.next_at ?? 0);
  return Number.isFinite(v) && v > 0 ? v : 0;
}

/** "12:34 left", "1:02:03 left", "due" — what a countdown says `secs` before. */
export function leftWord(secs: number): string {
  if (secs <= 0) return "due";
  const s = Math.ceil(secs);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = String(s % 60).padStart(2, "0");
  return (h ? h + ":" + String(m).padStart(2, "0") : String(m)) + ":" + ss + " left";
}

/** A flown stop's receipt: `detail.receipt` on one line, at most 80 chars. */
export function receiptOf(detail: Record<string, unknown> | null | undefined): string {
  const r = detail?.receipt;
  if (typeof r !== "string" && typeof r !== "number") return "";
  return clip(String(r).replace(/\s+/g, " ").trim(), 80);
}

/** A flight on its own (no session box): its stops, then the trailing "+"
 * while it is in the air. */
export function flightDiagram(f: Flight, kinds?: readonly StepKindDef[] | null): Diagram {
  const { line, loops } = liftLoops(flightStops(f, kinds));
  const rets = returnEdges(line);
  if (isShown(f) || f.state === "grounded") line.push(addNode("Add a stop"));
  const nodes: DiagramNode[] = [];
  const edges: DiagramEdge[] = [];
  chain(nodes, edges, line);
  edges.push(...loops, ...rets);
  return { nodes, edges, flightId: f.id, mode: "flight", insertFrom: f.state === "grounded" || f.state === "ready" ? f.cursor + 1 : insertFromOf(f) };
}

// --- The fast-track ladder as boxes --------------------------------------------------

const RUNGS: readonly Exclude<Lane, "leave">[] = ["commit", "push", "pr", "merge"];
const STAGE_RANK: Record<string, number> = { committed: 1, pushed: 2, pr: 3, merged: 4 };

/** The fast-track's rungs, commit → push → pr → merge up to its depth, when it
 * is armed: done rungs read from the session's git stage, the first one not
 * reached is current (red with the reason when the run halted). Empty when
 * nothing is armed. */
export function ladderRungs(row: Pick<Instance, "stage"> & { autopilot?: AutopilotRun | null }): Array<{ lane: Exclude<Lane, "leave">; state: DiagramNodeState }> {
  const run = row.autopilot;
  const depth = normalizeLane(run?.depth);
  if (!run || !depth || depth === "leave") return [];
  if (run.state !== "running" && run.state !== "halted" && run.state !== "done") return [];
  const reached = STAGE_RANK[String(row.stage)] || 0;
  const upTo = RUNGS.indexOf(depth) + 1;
  let current = false;
  return RUNGS.slice(0, upTo).map((lane, i) => {
    if (run.state === "done" || i < reached) return { lane, state: "done" as const };
    if (!current) {
      current = true;
      return { lane, state: run.state === "halted" ? ("halted" as const) : ("current" as const) };
    }
    return { lane, state: "pending" as const };
  });
}

/** A mini ladder for a child box: its flight's dots, else its fast-track's. */
function miniDots(row: Instance): string {
  const fl = row.flight as FlightRow | null | undefined;
  if (fl && isShown(fl)) return stripDots(fl);
  return ladderRungs(row)
    .map((r) => (r.state === "done" || r.state === "current" ? "●" : r.state === "halted" ? "✗" : "○"))
    .join("");
}

function sessionBox(row: Instance, kind: "parent" | "session" | "child", rowAt: DiagramRow): DiagramNode {
  const ss = deriveSessionState(row);
  return {
    id: kind + ":" + row.title,
    kind,
    row: rowAt,
    // The shown name (a rename, the sidebar's label); keys stay the title.
    title: windowName(row.title),
    sub: String(row.task_headline || row.task || "").split("\n")[0].trim(),
    state: "session",
    icon: "session",
    session: row.title,
    chip: { label: ss.label, tone: ss.tone },
    dots: kind === "child" ? miniDots(row) : undefined,
  };
}

/** The session's most recent flight that LANDED (done), from the flights query
 * (it keeps the last finished ones), else null. A cancelled one is gone: you
 * threw it away (the owner saw old cancelled Loops reappear on sessions). */
export function landedFlightOf(title: string, flights?: readonly Flight[] | null): Flight | null {
  let best: Flight | null = null;
  for (const f of flights || []) {
    if (f.session !== title || f.state !== "done") continue;
    if (!best || (f.finished || f.updated || 0) > (best.finished || best.updated || 0)) best = f;
  }
  return best;
}

export interface SessionDiagramOpts {
  /** The full flights (the shared flights query), so the session's stops carry
   * their titles and states; without them the row's own `flight` draws them. */
  flights?: readonly Flight[] | null;
  kinds?: readonly StepKindDef[] | null;
  /** Leave the parent and children out (the Board's one-row card). */
  family?: boolean;
  /** Fold the stops that have flown (two or more before the current stop)
   * into one `flown` pill — a line's own toggle; off by default, flown stops
   * stay as boxes (round 11). */
  foldFlown?: boolean;
  /** The older inverse of `foldFlown`: `false` folds. */
  expandFlown?: boolean;
  /** The groups (`step.group.id`) this line draws open: their sub-steps as
   * stop boxes in a thin outline, instead of one group box. */
  expandedGroups?: ReadonlySet<string> | null;
}

/** The SESSION's live diagram, from `/api/instances` rows: parent above, the
 * session box, its children branching below, then the flight's stops (or the
 * armed fast-track's rungs), and a trailing "+" to tack a stop on. */
export function sessionDiagram(rows: readonly Instance[], title: string, opts: SessionDiagramOpts = {}): Diagram {
  const row = rows.find((r) => r.title === title);
  const empty: Diagram = { nodes: [], edges: [], flightId: "", mode: "none", insertFrom: 0 };
  if (!row) return empty;
  const family = opts.family !== false;
  const nodes: DiagramNode[] = [];
  const edges: DiagramEdge[] = [];
  const me = sessionBox(row, "session", "main");

  if (family && row.parent) {
    const up = rows.find((r) => r.title === row.parent);
    if (up) {
      const p = sessionBox(up, "parent", "above");
      nodes.push(p);
      edges.push({ from: p.id, to: me.id, kind: "up" });
    }
  }

  const kids = rows.filter((r) => r.parent === title && r.title !== title);
  if (!family && kids.length) me.sub = [me.sub, kids.length + (kids.length === 1 ? " worker" : " workers")].filter(Boolean).join(" · ");

  const line: DiagramNode[] = [me];
  let mode: Diagram["mode"] = "none";
  let flightId = "";
  let insertFrom = 0;
  let loops: DiagramEdge[] = [];
  const fl = row.flight as FlightRow | null | undefined;
  if (fl && isShown(fl)) {
    mode = "flight";
    flightId = fl.id;
    // A ready flight (round 19) takes stops anywhere, its first one too.
    insertFrom = fl.state === "ready" ? 0 : fl.cursor + 1;
    const full = opts.flights?.find((f) => f.id === fl.id);
    if (full) {
      // Round 20: a parallel group at the cursor takes no stop inside it.
      if (fl.state !== "ready") insertFrom = Math.max(insertFrom, insertFromOf(full));
      const lifted = liftLoops(flightStops(full, opts.kinds), me.id);
      line.push(...lifted.line);
      loops = [...lifted.loops, ...returnEdges(line, me.id)];
    } else
      for (let i = 0; i < Math.max(0, Number(fl.total) || 0); i++) {
        const cur = i === fl.cursor;
        const perch = cur && fl.step?.actor === "you";
        line.push({
          id: "stop:i" + i,
          kind: "stop",
          row: "main",
          title: cur ? (perch ? perchLabel(fl.step.title) : fl.step.title) : "Stop " + (i + 1),
          sub: "",
          state: i < fl.cursor ? "done" : cur ? (fl.state === "halted" ? "halted" : fl.state === "paused" ? "paused" : perch && fl.state === "waiting_you" ? "perched" : "current") : "pending",
          icon: cur ? fl.step.kind : "stop",
          perch,
          index: i,
          reason: cur && fl.state === "halted" ? (fl as FlightRow & { reason?: string }).reason || "" : "",
        });
      }
  } else if (landedFlightOf(title, opts.flights) && !ladderRungs(row).length) {
    // A flight that landed stays on its session's line, its stops flown (the
    // owner: the stops must not go away from the flow once they finish) —
    // until the session builds or flies its next chain.
    const done = landedFlightOf(title, opts.flights)!;
    mode = "flight";
    flightId = done.id;
    insertFrom = done.steps.length;
    const lifted = liftLoops(flightStops(done, opts.kinds), me.id);
    line.push(...lifted.line);
    loops = [...lifted.loops, ...returnEdges(line, me.id)];
  } else {
    const rungs = ladderRungs(row);
    if (rungs.length) {
      mode = "ladder";
      const reason = row.autopilot?.state === "halted" ? row.autopilot.reason || "" : "";
      for (const r of rungs)
        line.push({
          id: "rung:" + r.lane,
          kind: "rung",
          row: "main",
          title: LANE_LABEL[r.lane],
          sub: "goes as far as" + (r.state === "current" && row.autopilot?.note ? " · " + clip(row.autopilot.note, 24) : ""),
          state: r.state,
          icon: "ship",
          reason: r.state === "halted" ? reason : "",
        });
    }
  }
  line.push(addNode("Add a stop"));

  const fans = family && mode === "flight" && fl ? branchesOf(title, kids, line, fl, opts.flights?.find((f) => f.id === fl.id)) : null;
  if (fans) {
    nodes.push(...line);
    const plain = lineEdges(line);
    for (let i = 1; i < line.length; i++) {
      const a = line[i - 1];
      const b = line[i];
      // A stop with branches reaches the next box THROUGH them.
      if (!fans.out.has(a.id)) edges.push(...plain.filter((e) => e.from === a.id));
      for (const c of fans.out.get(a.id) || []) {
        nodes.push(c);
        edges.push({ from: a.id, to: c.id, kind: "fan" });
        const done = c.branch!.state === "done" || fans.letGo.has(b.id);
        edges.push(done ? { from: c.id, to: b.id, kind: "join", done } : { from: c.id, to: b.id, kind: "join" });
      }
    }
  } else chain(nodes, edges, line);

  if (family) {
    // Every other child: a parallel row off the line's current place,
    // converging at the box after it (round 12) — the line reaches that box
    // through them.
    const hang = hangPoint(line);
    const next = line[line.indexOf(hang) + 1];
    const hung = (fans ? fans.below : kids).map((k) => {
      const state = childState(k);
      const c = sessionBox(k, "child", "below");
      c.branch = { from: hang.id, to: next.id, state, inner: innerSummary(k), ...liveOf(k) };
      return c;
    });
    if (hung.length) {
      const direct = edges.findIndex((e) => e.from === hang.id && e.to === next.id && (e.kind === "line" || e.kind === "add"));
      if (direct >= 0) edges.splice(direct, 1);
    }
    for (const c of hung) {
      nodes.push(c);
      // The family's wire out (`branch`), drawn the way a fan-out is.
      edges.push({ from: hang.id, to: c.id, kind: "branch" });
      edges.push(c.branch!.state === "done" ? { from: c.id, to: next.id, kind: "join", done: true } : { from: c.id, to: next.id, kind: "join" });
    }
  }
  edges.push(...loops);
  const d: Diagram = { nodes, edges, flightId, mode, insertFrom };
  if (mode !== "flight") return d;
  const grouped = collapseGroups(d, opts.expandedGroups);
  const fold = opts.foldFlown === true || opts.expandFlown === false;
  return fold ? collapseFlown(grouped) : grouped;
}

/** Where a session's other children hang: the flight's current stop, the
 * fast-track's current (or halted) rung, else the session box itself. Never
 * the trailing + (there is no box after it). */
function hangPoint(line: readonly DiagramNode[]): DiagramNode {
  const here = line.find((n, i) => i < line.length - 1 && (n.kind === "stop" || n.kind === "rung") && HERE_STATES.has(n.state));
  return here || line[0];
}

const HERE_STATES: ReadonlySet<DiagramNodeState> = new Set(["current", "perched", "paused", "halted"]);

/** A child's own bird: working, or waiting on you; none while it idles. */
function liveOf(k: Instance): { live?: "working" | "you" } {
  const ss = deriveSessionState(k);
  return ss.state === "working" ? { live: "working" } : ss.state === "needs_you" ? { live: "you" } : {};
}

/** A hung child's state: working or asking while it is, failed by its report
 * or a broken session, else done — its turn ended (the flight never waits
 * on it). */
function childState(k: Instance): BranchState {
  const ss = deriveSessionState(k);
  if (ss.state === "working") return "working";
  if (ss.state === "needs_you") return "asking";
  if (String(k.last_report?.status || "") === "failed" || ss.tone === "broken") return "failed";
  return "done";
}

// --- Folding a run of the line into one box ----------------------------------------

/** The line with the consecutive main-row boxes `run` drawn as ONE box,
 * `into`: the line's edges chain through it; a column of branches that fanned
 * out of a folded box fans out of `into` and, when it joined at a folded box
 * too, joins at the box after the run; a branch from outside that joined at a
 * folded box joins at `into`. The flown pill and a collapsed group both fold
 * this way. */
function foldRun(d: Diagram, run: readonly DiagramNode[], into: DiagramNode): Diagram {
  const main = d.nodes.filter((n) => n.row === "main");
  const folded = new Set(run.map((n) => n.id));
  const first = run[0];
  const after = main[main.indexOf(run[run.length - 1]) + 1];
  const to = (id: string) => (folded.has(id) ? into.id : id);
  // Branches that fan out of a folded box: they join past the run.
  const inner = new Set<string>();
  const nodes: DiagramNode[] = [];
  for (const n of d.nodes) {
    if (n.id === first.id) nodes.push(into);
    if (folded.has(n.id)) continue;
    if (n.branch && folded.has(n.branch.from)) {
      inner.add(n.id);
      nodes.push({ ...n, branch: { ...n.branch, from: into.id, to: after && folded.has(n.branch.to) ? after.id : n.branch.to } });
    } else if (n.branch && folded.has(n.branch.to)) nodes.push({ ...n, branch: { ...n.branch, to: into.id } });
    else nodes.push(n);
  }
  const fanned = inner.size > 0;
  // Round 20: a parallel group's fork / join folded into the box: the
  // box's own wires are plain line again.
  const members = new Set(run.filter((n) => n.parallel).map((n) => n.id));
  const edges: DiagramEdge[] = [];
  const seen = new Set<string>();
  for (const e0 of d.edges) {
    const e: DiagramEdge =
      (e0.kind === "fan" && members.has(e0.to)) || (e0.kind === "join" && members.has(e0.from))
        ? { from: e0.from, to: e0.to, kind: d.nodes.find((n) => n.id === e0.to)?.kind === "add" ? "add" : "line" }
        : e0;
    const from = to(e.from);
    let dest = to(e.to);
    // A fan-in into a folded box from a branch of the run joins past it.
    if (e.kind === "join" && dest === into.id && inner.has(e.from) && after) dest = after.id;
    // A return wire folded to one box (a Loop inside a collapsed group, a
    // finished loop in the flown pill) stays: it goes round that box.
    if (from === dest && e.kind !== "loop") continue;
    // The line reaches the box after the run THROUGH its branches.
    if (fanned && from === into.id && (e.kind === "line" || e.kind === "add")) continue;
    const key = from + ">" + dest + ">" + e.kind;
    if (seen.has(key)) continue;
    seen.add(key);
    edges.push({ ...e, from, to: dest });
  }
  return { ...d, nodes, edges };
}

// --- What has flown: folded into one pill ----------------------------------------

/** Fewer than this many flown stops stay drawn one by one. */
export const FLOWN_MIN = 2;
export const FLOWN_ID = "flown";

/** The line with the stops it has already flown folded into ONE `flown`
 * node: the leading run of done / skipped stops before the first stop that
 * isn't (the flight's current one), when there are `FLOWN_MIN` or more. A
 * failed stop breaks the run (it stays in sight), and so does a group (or a
 * sub-step of an expanded one). The line's edges chain through the pill; a
 * column of branches that fanned out of a folded stop fans out of the pill
 * instead and joins at the box after it. `insertFrom` and every stop's
 * `index` stay the flight's own. */
export function collapseFlown(d: Diagram): Diagram {
  const main = d.nodes.filter((n) => n.row === "main");
  const run: DiagramNode[] = [];
  for (const n of main) {
    if (n.kind === "session") continue;
    if (n.kind !== "stop" || n.inGroup || (n.state !== "done" && n.state !== "skipped")) break;
    run.push(n);
  }
  // Round 20: the pill never ends inside a parallel group (one member still
  // flying): it stops before the group's first member.
  if (run.length < FLOWN_MIN) return d;
  const after = main[main.indexOf(run[run.length - 1]) + 1];
  const pid = run[run.length - 1].parallel?.id;
  if (pid && after?.parallel?.id === pid) while (run.length && run[run.length - 1].parallel?.id === pid) run.pop();
  if (run.length < FLOWN_MIN) return d;
  const first = run[0];
  const last = run[run.length - 1];
  const pill: DiagramNode = {
    id: FLOWN_ID,
    kind: "flown",
    row: "main",
    title: run.length + " done",
    sub: first.title + " … " + last.title,
    state: "done",
    icon: "flown",
    count: run.length,
    flown: run.map((n) => n.title),
  };
  return foldRun(d, run, pill);
}

// --- Groups: a stop with sub-steps ----------------------------------------------------

const FLYING: ReadonlySet<DiagramNodeState> = new Set(["current", "perched", "halted", "paused"]);

/** One group box from its run of sub-step nodes. Its state: done once every
 * sub-step is done or skipped; the flight's state while it is at one of them
 * (current, perched, halted, paused); failed when one failed; else pending
 * (or `edit`, being drawn). It reads "k of n" while it flies, the current
 * sub-step's title as its second line. */
export function groupNode(id: string, g: { id: string; name: string; plan_id?: string }, run: readonly DiagramNode[]): DiagramNode {
  const total = run.length;
  const done = run.filter((n) => n.state === "done" || n.state === "skipped").length;
  const flying = run.find((n) => FLYING.has(n.state));
  const failed = run.find((n) => n.state === "failed");
  const state: DiagramNodeState =
    done === total ? "done" : flying ? flying.state : failed ? "failed" : run.every((n) => n.state === "edit") ? "edit" : "pending";
  const titles = run.map((n) => n.title).join(" → ");
  const node: DiagramNode = {
    id,
    kind: "group",
    row: "main",
    title: g.name,
    sub: flying ? flying.title : total + (total === 1 ? " stop: " : " stops: ") + clip(titles, 40),
    state,
    icon: "group",
    perch: flying?.perch,
    index: run[0].index,
    editable: run.every((n) => !!n.editable),
    reason: (flying || failed)?.reason || "",
    group: { id: g.id, name: g.name, plan_id: g.plan_id || "", steps: run.map((n) => n.step!), nodes: run.slice(), done, total, ...(flying ? { current: flying } : {}) },
  };
  const join = run.find((n) => n.join)?.join;
  if (join) node.join = join;
  // The stripe: every tag any sub-step wears, in the canonical order.
  const tags = new Set(run.flatMap((n) => n.risk || []));
  if (tags.size) node.risk = (["remote", "irreversible", "spends"] as const).filter((t) => tags.has(t));
  return node;
}

/** The line with each run of consecutive stops that share `step.group.id`
 * drawn as ONE `group` box (`groupNode`), except the groups in `expanded`:
 * their sub-steps stay stop boxes, marked `inGroup` for the canvas's outline.
 * A group id that comes back later on the line (not consecutive) is a box of
 * its own (`group:<id>@<index>`). Branches out of a sub-step hang off the
 * group box. Indexes stay the flight's own. */
export function collapseGroups(d: Diagram, expanded?: ReadonlySet<string> | null): Diagram {
  const runs: DiagramNode[][] = [];
  let cur: DiagramNode[] = [];
  let curId = "";
  for (const n of d.nodes) {
    if (n.row !== "main") continue;
    const gid = n.kind === "stop" ? n.step?.group?.id || "" : "";
    if (gid && gid === curId) cur.push(n);
    else {
      if (cur.length) runs.push(cur);
      cur = gid ? [n] : [];
      curId = gid;
    }
  }
  if (cur.length) runs.push(cur);
  if (!runs.length) return d;
  let out = d;
  const seen = new Set<string>();
  for (const run of runs) {
    // A group of ONE stop is that stop (the owner saw a lone Commit "boxed
    // out" as a "Stops · 0 of 1" group after a chain was joined on).
    if (run.length < 2) continue;
    const g = run[0].step!.group!;
    const id = "group:" + g.id + (seen.has(g.id) ? "@" + run[0].index : "");
    seen.add(g.id);
    if (expanded?.has(g.id)) {
      const marks = new Map(run.map((n, i) => [n.id, { id: g.id, name: g.name, first: i === 0, last: i === run.length - 1 }]));
      out = { ...out, nodes: out.nodes.map((n) => (marks.has(n.id) ? { ...n, inGroup: marks.get(n.id)! } : n)) };
      continue;
    }
    out = foldRun(out, run, groupNode(id, g, run));
  }
  return out;
}

/** The `reason` the server gives a flight paused at a halt point
 * (backend/web/core/flights.py `HALT_POINT`). */
export const HALT_POINT_REASON = "halt point";

/** Round 21b (JUDGE-R21-3 #5): whether the flight stopped at its current
 * stop because the owner set a halt point there ("Halt here"), NOT because a
 * stop failed. The server parks it `paused` with reason `halt point`
 * (`_hit_halt_point`); a halted flight whose current stop is a halt point
 * and never failed is read the same way. Nothing failed, so there is
 * nothing to retry: its buttons are Carry on (`POST …/resume`,
 * `op_resume`, which lets the halt point through) and Skip. */
export function atHaltPoint(flight: Pick<Flight, "steps" | "cursor" | "state" | "reason" | "step_states"> | null | undefined): boolean {
  if (!flight) return false;
  const step = flight.steps[flight.cursor];
  if (!step) return false;
  if (flight.state === "paused") return flight.reason === HALT_POINT_REASON;
  if (flight.state !== "halted" || !step.halt) return false;
  const ss = flight.step_states?.[step.id];
  return ss?.state !== "failed" && !ss?.detail?.failed_reason;
}

/** The group box (or stop) the flight's step `index` is drawn in. */
export function nodeAtIndex(d: Pick<Diagram, "nodes">, index: number): DiagramNode | undefined {
  return d.nodes.find(
    (n) => n.row === "main" && n.index !== undefined && (n.kind === "stop" ? n.index === index : n.kind === "group" && index >= n.index && index < n.index + (n.group?.total || 1))
  );
}

// --- Drawn places and the flight's indexes ---------------------------------------------

/** One place on the line as the canvas counts it: a stop, or a collapsed
 * group (`size` sub-steps, the first at the flight's index `first`). */
export interface LineUnit {
  node: DiagramNode;
  first: number;
  size: number;
}

/** The line's places in order: its stops and its collapsed groups. */
export function lineUnits(d: Pick<Diagram, "nodes">): LineUnit[] {
  return d.nodes
    .filter((n) => n.row === "main" && (n.kind === "stop" || n.kind === "group") && n.index !== undefined)
    .map((n) => ({ node: n, first: n.index!, size: n.kind === "group" ? n.group?.total || 1 : 1 }));
}

/** The flight's index for the canvas's place `p` (an insert's gap, a drop):
 * the first step of the place's unit; past the last one, the end. */
export function flightIndexAt(d: Pick<Diagram, "nodes">, p: number): number {
  const u = lineUnits(d);
  if (!u.length) return p;
  if (p < u.length) return u[Math.max(0, p)].first;
  const last = u[u.length - 1];
  return last.first + last.size + (p - u.length);
}

/** A move between two drawn places in the flight's indexes (`to` is the
 * index in the final list, as the move route takes it): moving later lands
 * right after the target's last step, earlier right before its first. */
export function flightMove(d: Pick<Diagram, "nodes">, from: number, to: number): { from: number; to: number } {
  const u = lineUnits(d);
  if (!u[from] || !u[to]) return { from, to };
  const f = u[from].first;
  if (to === from) return { from: f, to: f };
  return { from: f, to: to > from ? u[to].first + u[to].size - u[from].size : u[to].first };
}

/** The line as a canvas counts it: a canvas speaks of a stop by its place
 * among the DRAWN places (a move's from / to, an insert's gap) and compares it
 * with `insertFrom`, so with a `flown` pill folding N stops, or a group box
 * holding several, every place's `index` and `insertFrom` are renumbered to
 * drawn places (`lineUnits`). `drawnCanvas` maps the canvas's numbers back.
 * A line already counted that way (nothing folded, no group box): the
 * diagram itself. */
export function drawnIndexes(d: Diagram): Diagram {
  const u = lineUnits(d);
  let from = u.findIndex((x) => x.first >= d.insertFrom);
  if (from < 0) from = u.length;
  if (from === d.insertFrom && u.every((x, p) => x.first === p)) return d;
  const at = new Map(u.map((x, p) => [x.node.id, p]));
  return {
    ...d,
    insertFrom: from,
    nodes: d.nodes.map((n) => (at.has(n.id) ? { ...n, index: at.get(n.id)! } : n)),
  };
}

/** The canvas handlers `drawnCanvas` maps (FlywayCanvas's, structurally). */
export interface IndexedCanvas {
  onSelect?(n: DiagramNode): void;
  onEdit?(n: DiagramNode): void;
  onOpen?(n: DiagramNode): void;
  onInsert?(at: number): void;
  onRemove?(n: DiagramNode): void;
  onMove?(from: number, to: number): void;
  onDuplicate?(n: DiagramNode): void;
  onToggleHalt?(n: DiagramNode): void;
  onDropStop?(drop: never, at: number | null): void;
  onLoopFrom?(from: DiagramNode, to: DiagramNode): void;
  onLoopTo?(loop: DiagramNode, to: DiagramNode): void;
  renderInBox?(n: DiagramNode): unknown;
  sizeOf?(n: DiagramNode, base: BoxSize): BoxSize;
}

/** A line in the flight's indexes (`d`) and its canvas handlers (which speak
 * them), made ready for FlywayCanvas: the diagram renumbered to drawn places
 * (`drawnIndexes`) and handlers that map the canvas's places back — moves
 * (`flightMove`), inserts and drops (`flightIndexAt`) — and hand back the
 * line's own nodes. A line already counted that way passes through untouched,
 * so it is safe to apply twice. */
export function drawnCanvas<C extends IndexedCanvas>(d: Diagram, canvas: C): { diagram: Diagram; canvas: C } {
  const drawn = drawnIndexes(d);
  if (drawn === d) return { diagram: d, canvas };
  const byId = new Map(d.nodes.map((n) => [n.id, n]));
  const orig = (n: DiagramNode) => byId.get(n.id) || n;
  const c = canvas;
  const node = <F extends ((n: DiagramNode, ...rest: never[]) => unknown) | undefined>(f: F): F =>
    (f && ((n: DiagramNode, ...rest: never[]) => f(orig(n), ...rest))) as F;
  return {
    diagram: drawn,
    canvas: {
      ...c,
      onSelect: node(c.onSelect),
      onEdit: node(c.onEdit),
      onOpen: node(c.onOpen),
      onRemove: node(c.onRemove),
      onDuplicate: node(c.onDuplicate),
      onToggleHalt: node(c.onToggleHalt),
      renderInBox: node(c.renderInBox),
      sizeOf: c.sizeOf && ((n: DiagramNode, b: BoxSize) => c.sizeOf!(orig(n), b)),
      onInsert: c.onInsert && ((at: number) => c.onInsert!(flightIndexAt(d, at))),
      onMove: c.onMove && ((from: number, to: number) => {
        const m = flightMove(d, from, to);
        c.onMove!(m.from, m.to);
      }),
      onDropStop: c.onDropStop && ((drop: never, at: number | null) => c.onDropStop!(drop, at === null ? null : flightIndexAt(d, at))),
      onLoopFrom: c.onLoopFrom && ((from: DiagramNode, to: DiagramNode) => c.onLoopFrom!(orig(from), orig(to))),
      onLoopTo: c.onLoopTo && ((loop: DiagramNode, to: DiagramNode) => c.onLoopTo!(orig(loop), orig(to))),
    },
  };
}

/** Where the flight is, in words, for a line's bar: "at Reviews settle · 10
 * min quiet", "waiting on you at Perch: approve", "halted at Push · <why>",
 * "at Push · waiting for 2 of 3", "fast-track at Push". Empty while the bird
 * sits on the session box itself (its state chip says it) or nowhere. */
export function lineWhere(d: Diagram): string {
  let b = birdPerch(d);
  const lp = loopAtWork(d);
  let n = b ? (lp && lp.from === b.nodeId && !d.nodes.some((x) => x.id === b!.nodeId && STOP_TONE[x.state]) ? lp.via : d.nodes.find((x) => x.id === b!.nodeId)) : undefined;
  if (!n || n.kind === "session") {
    // The bird may sit on a busy session while the flight is parked at a
    // stop (birdPerch): the bar still says where the flight is.
    const isParked = (x: DiagramNode) => x.state === "halted" || x.state === "paused";
    const parked = d.nodes.find((x) => x.row === "main" && (x.kind === "stop" || x.kind === "group") && isParked(x)) || (lp?.via && isParked(lp.via) ? lp.via : undefined);
    if (!parked) return "";
    n = parked;
    b = { nodeId: parked.id, tone: STOP_TONE[parked.state]! };
  }
  if (!b) return "";
  const head = n.kind === "rung" ? "goes as far as " : b.tone === "you" ? "needs you at " : b.tone === "halted" ? "halted at " : b.tone === "paused" ? "paused at " : "at ";
  // A stop with an ask (lib/stopAsks) says it on its own tile, its line
  // under its name and its buttons: the bar says only where it is.
  const tail = n.ask ? "" : n.join && n.join.waiting > 0 ? joinLabel(n.join) : b.tone === "halted" && n.reason ? n.reason : n.kind === "stop" || n.kind === "group" ? n.sub : "";
  // "needs you at" already says whose stop it is: no "Wait for me:" after it.
  const title = b.tone === "you" ? n.title.replace(/^wait for me:\s*/i, "") : n.title;
  return head + title + (tail ? " · " + tail : "");
}

/** How many stops a line's `flown` pill folds (0: none). The canvas speaks
 * of a stop by its place among the line's DRAWN stops; the flight by its
 * index — the pill's count is the difference. */
export function flownCount(d: Pick<Diagram, "nodes">): number {
  return d.nodes.find((n) => n.kind === "flown")?.count || 0;
}

// --- Branches: children working in parallel between two stops ----------------------

/** What a child row's `flight` dto says about where it was spawned (the
 * parent's title and the stop it was spawned during). */
export interface BranchRef {
  parent: string;
  step: string;
}

/** The parent's `flight` dto's join and a child's `flight.branch` — optional:
 * an older server sends neither. */
type BranchedRow = FlightRow & { join?: JoinCount | null; branch?: BranchRef | null };

/** One child, as a join step's `detail.children[]` lists it. */
interface JoinChild {
  title: string;
  state: BranchState;
  report?: string;
}

export function branchOf(row: Pick<Instance, "flight" | "flight_branch">): BranchRef | null {
  // Lifted to `flight_branch` by normalizeFlightRows; raw rows still carry it
  // inside the dto (tests, an older bundle).
  const b = row.flight_branch ?? (row.flight as BranchedRow | null | undefined)?.branch;
  return b && typeof b.step === "string" && b.step ? b : null;
}

/** The parent flight's join count, when it waits on its workers. */
export function joinOf(row: Pick<Instance, "flight">): JoinCount | null {
  const j = (row.flight as BranchedRow | null | undefined)?.join;
  return j && Number.isFinite(Number(j.total)) ? { waiting: Math.max(0, Number(j.waiting) || 0), total: Number(j.total) || 0 } : null;
}

/** "waiting for 2 of 5" — a join point's words. */
export function joinLabel(j: JoinCount): string {
  return "waiting for " + j.waiting + " of " + j.total;
}

const REPORT_STATE: Record<string, BranchState> = { done: "done", failed: "failed", blocked: "asking" };

/** A branch's state: the child's live activity first (working, or asking —
 * a question, a perch), then its last report (done / failed / blocked), then
 * what the join's detail says, else still working (the join waits on it). */
export function branchState(child: Instance, listed?: JoinChild | null): BranchState {
  const ss = deriveSessionState(child);
  if (ss.state === "working") return "working";
  if (ss.state === "needs_you") return "asking";
  const rep = REPORT_STATE[String(child.last_report?.status || "")];
  if (rep) return rep;
  if (listed?.state) return listed.state;
  if (ss.tone === "broken") return "failed";
  return "working";
}

const BRANCH_CHIP: Record<BranchState, { label: string; tone: string }> = {
  working: { label: "Working", tone: "working" },
  asking: { label: "Asking", tone: "attention" },
  done: { label: "Done", tone: "neutral" },
  failed: { label: "Failed", tone: "broken" },
  gone: { label: "Gone", tone: "muted" },
};

const BRANCH_NODE_STATE: Record<BranchState, DiagramNodeState> = {
  working: "current",
  asking: "perched",
  done: "done",
  failed: "failed",
  gone: "skipped",
};

/** A child's own line in brief: "2/4 stops · Push" (its flight, else its
 * fast-track's rungs), "" when it has neither. */
export function innerSummary(row: Instance): string {
  const fl = row.flight as FlightRow | null | undefined;
  if (fl && isShown(fl) && Number(fl.total) > 0) return Math.min(fl.cursor, fl.total) + "/" + fl.total + " stops" + (fl.step?.title ? " · " + fl.step.title : "");
  const rungs = ladderRungs(row);
  if (!rungs.length) return "";
  const done = rungs.filter((r) => r.state === "done").length;
  const cur = rungs.find((r) => r.state !== "done");
  return done + "/" + rungs.length + " stops" + (cur ? " · " + LANE_LABEL[cur.lane] : "");
}

/** The join detail's children, by title (a join step's, or an agent step's
 * that waits for its workers). */
function joinChildren(full: Flight | undefined): Map<string, JoinChild> {
  const out = new Map<string, JoinChild>();
  if (!full) return out;
  for (const rec of Object.values(full.step_states || {})) {
    const list = (rec?.detail as { children?: unknown } | undefined)?.children;
    if (!Array.isArray(list)) continue;
    for (const c of list as JoinChild[]) if (c && typeof c.title === "string") out.set(c.title, c);
  }
  return out;
}

/** Split the children into branches (spawned during a stop of THIS flight)
 * and the rest (drawn below, as before), and find the join point: the box
 * after the spawning stop, or the current stop itself when it is a `join`
 * step. `out` maps a stop's id to its branch boxes; `letGo` holds the join
 * points whose fan-in draws done (the join let go, or the flight is past). */
function branchesOf(title: string, kids: readonly Instance[], line: DiagramNode[], fl: FlightRow, full: Flight | undefined) {
  const out = new Map<string, DiagramNode[]>();
  const below: Instance[] = [];
  const letGo = new Set<string>();
  const listed = joinChildren(full);
  // A stop's place on the line, by its step id (the full flight's), or —
  // drawn from the row alone — the current stop by the row's step id.
  const at = (step: string) => line.findIndex((n) => n.kind === "stop" && (n.step ? n.step.id === step : n.index === fl.cursor && fl.step?.id === step));
  for (const k of kids) {
    const b = branchOf(k);
    // Spawned under another parent (re-parented since): not this line's.
    const i = b && (!b.parent || b.parent === title) ? at(b.step) : -1;
    if (i < 0 || i + 1 >= line.length) {
      below.push(k);
      continue;
    }
    const entry = listed.get(k.title);
    if (entry?.state === "gone") continue;
    const state = branchState(k, entry);
    const from = line[i];
    const to = line[i + 1];
    const inner = innerSummary(k);
    const c: DiagramNode = {
      id: "branch:" + k.title,
      kind: "branch",
      row: "branch",
      title: windowName(k.title),
      sub: inner || (entry?.report ? clip(String(entry.report)) : k.last_report?.summary ? clip(k.last_report.summary) : ""),
      state: BRANCH_NODE_STATE[state],
      icon: "session",
      session: k.title,
      chip: BRANCH_CHIP[state],
      branch: { from: from.id, to: to.id, state, inner, ...liveOf(k) },
    };
    out.set(from.id, [...(out.get(from.id) || []), c]);
  }
  // The join point: the parent's current step waits on its workers — a join
  // step at itself, an agent step at the box after it.
  const join = joinOf({ flight: fl });
  const ci = line.findIndex((n) => n.kind === "stop" && n.index === fl.cursor);
  if (join && ci >= 0) {
    const point = fl.step?.kind === "join" ? line[ci] : line[ci + 1];
    if (point) {
      point.join = join;
      if (join.waiting === 0) letGo.add(point.id);
    }
  }
  for (const n of line) if (n.state === "done" || n.state === "skipped") letGo.add(n.id);
  return { out, below, letGo };
}

/** Whether a session has anything for its Flight tab to draw beyond itself: a
 * flight in the air, workers, a parent or an armed fast-track. */
export function hasSessionDiagram(rows: readonly Instance[], title: string): boolean {
  const row = rows.find((r) => r.title === title);
  if (!row) return false;
  const fl = row.flight as FlightRow | null | undefined;
  return !!(fl && isShown(fl)) || ladderRungs(row).length > 0 || !!row.parent || rows.some((r) => r.parent === title);
}

// --- The bird -------------------------------------------------------------------

/** How the bird sits: bobbing while the flock works, gold and still perched on
 * you, red when halted, still when paused. */
export type BirdTone = "working" | "you" | "halted" | "paused";

export interface BirdPerch {
  nodeId: string;
  tone: BirdTone;
  /** A branch's bird: the child session it flies for (a click opens it). */
  child?: string;
  /** Round 15 formation (`birdFormation`): a worker's bird flies in a V
   * behind the parent's bird — its place in the V, in bird-lengths (dx back
   * along the line, dy to the side; the canvas scales and orients them).
   * Only while the line has a main bird to follow; purely visual. */
  formation?: { dx: number; dy: number };
}

const STOP_TONE: Partial<Record<DiagramNodeState, BirdTone>> = {
  current: "working",
  perched: "you",
  halted: "halted",
  paused: "paused",
};

/** Where the bird sits on a line: the flight's current stop — but while a
 * join waits on its branches, at the join point (gold when a branch asks),
 * unless the flight is halted or paused — else the fast-track's current rung,
 * else the session box while its agent works (or waits on you); nowhere when
 * the session is idle with nothing armed. */
export function birdPerch(d: Diagram): BirdPerch | null {
  const main = d.nodes.filter((n) => n.row === "main");
  // A Loop draws no box: while the flight is at one, the bird stands on the
  // stop its wire leaves from (the end of what it repeats).
  const lp = loopAtWork(d);
  const stop: Pick<DiagramNode, "id" | "state"> | undefined =
    main.find((n) => (n.kind === "stop" || n.kind === "group") && STOP_TONE[n.state]) || (lp ? { id: lp.from, state: lp.via!.state } : undefined);
  const me = main.find((n) => n.kind === "session");
  // A stop's failure is with the agent (its return wire is lit): the bird
  // sits on the agent it went back to while it fixes.
  const fixing = d.edges.find((e) => e.kind === "fix" && e.ret?.shown === "fixing" && main.some((n) => n.id === e.from && n.state === "current"));
  if (fixing) return { nodeId: fixing.to, tone: "working" };
  // The bird follows the work: a flight halted or paused at a stop while
  // the agent itself is busy puts the bird on the session box, not on the
  // stop it is parked at (the owner: "why is the bird on Commit when the
  // agent is running").
  if (stop && (stop.state === "halted" || stop.state === "paused")) {
    if (me?.chip?.tone === "working") return { nodeId: me.id, tone: "working" };
    return { nodeId: stop.id, tone: STOP_TONE[stop.state]! };
  }
  const point = main.find((n) => n.join && n.join.waiting > 0);
  if (point) {
    const asking = d.nodes.some((n) => n.branch?.to === point.id && n.branch.state === "asking");
    return { nodeId: point.id, tone: asking ? "you" : "working" };
  }
  if (stop) return { nodeId: stop.id, tone: STOP_TONE[stop.state]! };
  const rung = main.find((n) => n.kind === "rung" && (n.state === "current" || n.state === "halted"));
  if (rung) return { nodeId: rung.id, tone: rung.state === "halted" ? "halted" : "working" };
  if (me?.chip?.tone === "working") return { nodeId: me.id, tone: "working" };
  if (me?.chip?.tone === "attention") return { nodeId: me.id, tone: "you" };
  return null;
}

/** Every bird on a line: the main one (`birdPerch`) first, then one per
 * branch — a worker spawned at a stop or a child hung off the line — whose
 * session works (bobbing) or waits on you (gold); an idle one has none. A
 * branch's bird names its session (`child`). */
export function birdPerches(d: Diagram): BirdPerch[] {
  const main = birdPerch(d);
  const out: BirdPerch[] = main ? [main] : [];
  for (const n of d.nodes) if (n.branch?.live && n.session) out.push({ nodeId: n.id, tone: n.branch.live, child: n.session });
  return out;
}

/** Round 15: the same birds, the workers' given their place in the V behind
 * the leader (`formationSlot`, in line order) — only when the line has a
 * main bird to follow. Purely visual: `birdPerches` itself is unchanged. */
export function birdFormation(perches: readonly BirdPerch[]): BirdPerch[] {
  const lead = perches[0] && !perches[0].child ? perches[0] : null;
  if (!lead) return perches.slice();
  let k = 0;
  return perches.map((b) => (b.child ? { ...b, formation: formationSlot(k++) } : b));
}

/** The k-th worker's place in the V behind the leader: the first pair one
 * length back and one to each side, the next pair two back and two out —
 * the wedge a flock flies. */
export function formationSlot(k: number): { dx: number; dy: number } {
  const row = Math.floor(k / 2) + 1;
  return { dx: -row, dy: (k % 2 === 0 ? -1 : 1) * row };
}

// --- Layout -------------------------------------------------------------------

export interface BoxSize {
  w: number;
  h: number;
}

export interface LayoutOpts {
  compact?: boolean;
  /** The Sky's bigger boxes (bigger type, read from afar): `boxMetrics`'
   * roomy tier. The pane's Flight tab and the flyway editor keep the base. */
  roomy?: boolean;
  /** Phone width: the line runs top to bottom, connectors too. */
  vertical?: boolean;
  /** A box drawn bigger than the rest (an expanded perch, with its checklist
   * and Done / Skip inside). */
  sizeOf?: (n: DiagramNode, base: BoxSize) => BoxSize;
}

export interface Placed {
  node: DiagramNode;
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface Wire {
  from: string;
  to: string;
  kind: DiagramEdgeKind;
  /** The SVG path, arrowhead at its end (none on a fan-out). */
  d: string;
  /** A fan-in drawn done (its branch arrived, or the join let go). */
  done?: boolean;
  /** A `loop` wire: its Loop's count, the Loop stop itself, and the middle
   * of the wire's run under the line (where its pill sits). */
  loop?: LoopMark;
  via?: DiagramNode;
  apex?: { x: number; y: number };
}

export interface Layout {
  boxes: Placed[];
  wires: Wire[];
  width: number;
  height: number;
  /** How far the return arcs (and their pills) reach ABOVE y = 0: the
   * canvas keeps that much headroom over the line. 0 without a loop. */
  rise: number;
  /** The gap between two boxes on the line (where a "+" can sit). */
  gap: number;
}

export interface BoxMetrics {
  box: BoxSize;
  add: BoxSize;
  branch: BoxSize;
  /** The pill folding what a flight has flown. */
  flown: BoxSize;
  gap: number;
  vgap: number;
  bgap: number;
  indent: number;
  anchor: number;
  /** What a session box's state chip, a branch's sub-line and a child's mini
   * ladder add to a box's height. */
  chipH: number;
  subH: number;
  dotsH: number;
}

/** The base box and the line's spacing; `branch` is a branch box (smaller:
 * the child's title, its chip, its line in brief) and `bgap` the space
 * between two branches of a column. `roomy` is the Sky's tier: bigger boxes
 * for 15px titles and 13px summaries (docs/flights-visibility.md). */
export function boxMetrics(compact = false, roomy = false): BoxMetrics {
  if (roomy)
    return compact
      ? { box: { w: 160, h: 64 }, add: { w: 52, h: 52 }, branch: { w: 150, h: 54 }, flown: { w: 104, h: 44 }, gap: 40, vgap: 26, bgap: 10, indent: 26, anchor: 32, chipH: 22, subH: 20, dotsH: 16 }
      : { box: { w: 236, h: 100 }, add: { w: 180, h: 48 }, branch: { w: 200, h: 64 }, flown: { w: 148, h: 56 }, gap: 56, vgap: 36, bgap: 12, indent: 30, anchor: 50, chipH: 22, subH: 20, dotsH: 16 };
  return compact
    ? { box: { w: 140, h: 62 }, add: { w: 44, h: 44 }, branch: { w: 124, h: 46 }, flown: { w: 96, h: 40 }, gap: 34, vgap: 22, bgap: 8, indent: 22, anchor: 31, chipH: 18, subH: 16, dotsH: 14 }
    : { box: { w: 178, h: 76 }, add: { w: 148, h: 40 }, branch: { w: 156, h: 52 }, flown: { w: 120, h: 44 }, gap: 46, vgap: 30, bgap: 10, indent: 26, anchor: 38, chipH: 18, subH: 16, dotsH: 14 };
}

/** A box at a line's ◧ size, from the layout's normal one (FlywayCanvas's
 * `sizedBox`): compact is the title alone, one line; wide has room for a
 * two-line title, the summary on two lines and the detail line. A branch
 * stays smaller than the line's boxes at every size; the + and the flown
 * pill keep the layout's. `roomy`: the Sky's bigger tier. */
export function sizedBoxFor(size: "compact" | "normal" | "wide", n: DiagramNode, base: BoxSize, detail = "", roomy = false): BoxSize {
  if (n.kind === "add" || n.kind === "flown" || size === "normal") return base;
  if (roomy) {
    if (n.kind === "branch") return size === "compact" ? { w: 140, h: 48 } : { w: 270, h: base.h + 20 };
    if (size === "compact") return { w: 150, h: 64 };
    return { w: 330, h: base.h + 22 + (n.sub ? 18 : 0) + (detail ? 40 : 0) };
  }
  if (n.kind === "branch") return size === "compact" ? { w: 110, h: 40 } : { w: 210, h: base.h + 16 };
  if (size === "compact") return { w: 110, h: 62 };
  return { w: 260, h: base.h + 18 + (n.sub ? 15 : 0) + (detail ? 32 : 0) };
}

/** Place every box and draw every connector. Horizontal: the line left to
 * right, the parent above the session, the children stacked below it on a
 * branch from its left edge, and a stop's branches in a column between it and
 * the next box (centred on the line, hanging down when there are many).
 * Vertical: the line top to bottom, the children in a column to the
 * session's right, a stop's branches an indented group under it. */
export function layoutDiagram(d: Diagram, opts: LayoutOpts = {}): Layout {
  // A phone (or a narrow pane) draws two columns at most: compact boxes,
  // and the + as wide as a box.
  const m = boxMetrics(!!opts.compact || !!opts.vertical, !!opts.roomy);
  if (opts.vertical) m.add = { w: m.box.w, h: opts.roomy ? 44 : 36 };
  // On a phone a branch group fits the line's column, indented.
  if (opts.vertical) m.branch = { w: m.box.w - m.indent, h: m.branch.h };
  const size = (n: DiagramNode): BoxSize => {
    // The flown pill is the layout's own (never a line's ◧ size).
    if (n.kind === "flown") return m.flown;
    // A session box also carries its state chip, a child its mini ladder, a
    // branch its line in brief.
    const base =
      n.kind === "add"
        ? m.add
        : n.kind === "branch"
          ? { w: m.branch.w, h: m.branch.h + (n.sub ? m.subH : 0) }
          : { w: m.box.w, h: m.box.h + (n.chip ? m.chipH : 0) + (n.dots ? m.dotsH : 0) };
    return opts.sizeOf ? opts.sizeOf(n, base) : base;
  };
  const above = d.nodes.filter((n) => n.row === "above");
  const main = d.nodes.filter((n) => n.row === "main");
  const below = d.nodes.filter((n) => n.row === "below");
  // A stop's branches, by the stop they fan out of.
  const fans = new Map<string, DiagramNode[]>();
  // Round 12: a child hung off the line (row "below" with a `branch`) is a
  // row of the same column.
  for (const n of d.nodes) if ((n.row === "branch" || n.row === "below") && n.branch) fans.set(n.branch.from, [...(fans.get(n.branch.from) || []), n]);
  const at = new Map<string, Placed>();
  const boxes: Placed[] = [];
  const put = (node: DiagramNode, x: number, y: number) => {
    const s = size(node);
    const p = { node, x, y, w: s.w, h: s.h };
    at.set(node.id, p);
    boxes.push(p);
    return p;
  };
  const session = main.find((n) => n.kind === "session");

  if (!opts.vertical) {
    const top = above.length ? m.box.h + m.vgap : 0;
    let x = 0;
    let fanBottom = 0;
    // Round 20: a parallel group's members share one column (the first on
    // the line, the rest below it); the line goes on past the widest.
    let colX = 0;
    let colY = 0;
    let colW = 0;
    for (let k = 0; k < main.length; k++) {
      const n = main[k];
      const s = size(n);
      const pid = n.parallel?.id;
      if (pid) {
        const first = main[k - 1]?.parallel?.id !== pid;
        if (first) {
          colX = x;
          colY = top;
          colW = 0;
        }
        put(n, colX, colY);
        colY += s.h + m.vgap;
        colW = Math.max(colW, s.w);
        const lastOne = main[k + 1]?.parallel?.id !== pid;
        if (!lastOne) continue;
        x = colX + colW + m.gap;
        fanBottom = Math.max(fanBottom, colY - m.vgap);
      } else {
        // A smaller box (the +, the flown pill) sits centred on the line's anchor.
        put(n, x, top + (n.kind === "add" || n.kind === "flown" ? m.anchor - s.h / 2 : 0));
        x += s.w + m.gap;
      }
      const col = pid ? main.filter((g) => g.parallel?.id === pid).flatMap((g) => fans.get(g.id) || []) : fans.get(n.id);
      if (!col?.length) continue;
      const sizes = col.map(size);
      const colH = sizes.reduce((h, c) => h + c.h, 0) + m.bgap * (col.length - 1);
      let y = top + Math.max(0, m.anchor - colH / 2);
      col.forEach((c, i) => {
        put(c, x, y);
        y += sizes[i].h + m.bgap;
      });
      fanBottom = Math.max(fanBottom, y - m.bgap);
      x += Math.max(...sizes.map((c) => c.w)) + m.gap;
    }
    const sp = session ? at.get(session.id) : undefined;
    for (const n of above) put(n, sp ? sp.x : 0, 0);
    const lineH = Math.max(m.box.h, ...main.map((n) => size(n).h));
    let y = Math.max(top + lineH, fanBottom) + m.vgap;
    for (const n of below) {
      if (at.has(n.id)) continue;
      put(n, (sp ? sp.x : 0) + m.indent, y);
      y += size(n).h + 12;
    }
  } else {
    let y = 0;
    for (const n of above) {
      put(n, 0, y);
      y += size(n).h + m.vgap;
    }
    for (const n of main) {
      const s = size(n);
      // Round 20: a parallel group's members indented under the place
      // before, as a Split's branches are (its fork runs down their side).
      put(n, n.parallel ? m.indent : n.kind === "add" || n.kind === "flown" ? (m.box.w - s.w) / 2 : 0, y);
      y += s.h + (n.parallel && n.parallel.index < n.parallel.size - 1 ? m.bgap : m.vgap);
      const col = fans.get(n.id);
      if (!col?.length) continue;
      for (const c of col) {
        put(c, m.indent, y);
        y += size(c).h + m.bgap;
      }
      y += m.vgap - m.bgap;
    }
    const sp = session ? at.get(session.id) : undefined;
    let cy = sp ? sp.y : 0;
    for (const n of below) {
      if (at.has(n.id)) continue;
      put(n, m.box.w + m.indent, cy);
      cy += size(n).h + 12;
    }
  }

  // Reading (and tab) order: the parent, the line (a stop's branches right
  // after it — the sort keeps the order they were placed in), then the
  // children below.
  const rank: Record<DiagramRow, number> = { above: 0, main: 1, branch: 1, below: 2 };
  boxes.sort((p, q) => rank[p.node.row] - rank[q.node.row]);

  // Where a connector meets a box's side: the line's anchor, a small box's
  // middle (the +, a branch).
  const anchorOf = (p: Placed) => (p.node.kind === "add" || p.node.kind === "flown" ? p.h / 2 : p.node.kind === "branch" ? Math.min(p.h, m.box.h) / 2 : m.anchor);
  const wires: Wire[] = [];
  for (const e of d.edges) {
    const a = at.get(e.from);
    const b = at.get(e.to);
    if (!a || !b) continue;
    // A child hung off the line: its `branch` wire runs the way a fan-out does.
    const kind: DiagramEdgeKind = e.kind === "branch" && b.node.branch ? "fan" : e.kind;
    let path = "";
    if (kind === "loop" || kind === "fix") {
      // A stop's return wire back to the agent (`fix`) arcs back the way a
      // Loop's does (the Sky draws its words; this canvas only the arc).
      const arc = loopArc(a, b, anchorOf, !!opts.vertical, boxes);
      wires.push({ from: e.from, to: e.to, kind, d: arc.d, apex: arc.apex, ...(e.loop ? { loop: e.loop } : {}), ...(e.via ? { via: e.via } : {}) });
      continue;
    }
    if (opts.vertical && kind === "fan") {
      // Down the group's left side, into each branch.
      const x = a.x + m.indent / 2;
      path = `M ${x} ${a.y + a.h} V ${b.y + anchorOf(b)} H ${b.x}`;
    } else if (opts.vertical && kind === "join") {
      // Out of each branch's right edge, down its right side, into the join.
      const rx = Math.max(a.x + a.w, b.x + b.w) + 8;
      const y2 = b.y + anchorOf(b);
      path = `M ${a.x + a.w} ${a.y + anchorOf(a)} H ${rx} V ${y2} H ${b.x + b.w + 2}`;
    } else if (kind === "up") {
      const x = a.x + Math.min(a.w, b.w) / 2;
      path = `M ${x} ${a.y + a.h} V ${b.y - 2}`;
    } else if (kind === "branch") {
      if (!opts.vertical) {
        const x = a.x + m.indent / 2;
        const y = b.y + Math.min(b.h, m.box.h) / 2;
        path = `M ${x} ${a.y + a.h} V ${y} H ${b.x - 2}`;
      } else {
        const y1 = a.y + m.anchor;
        const y2 = b.y + Math.min(b.h, m.box.h) / 2;
        const mx = a.x + a.w + m.indent / 2;
        path = `M ${a.x + a.w} ${y1} H ${mx} V ${y2} H ${b.x - 2}`;
      }
    } else if (!opts.vertical) {
      const y1 = a.y + anchorOf(a);
      const y2 = b.y + anchorOf(b);
      const x1 = a.x + a.w;
      // A fan-out has no arrowhead to leave room for.
      const x2 = b.x - (kind === "fan" ? 0 : 2);
      const mx = (x1 + x2) / 2;
      path = y1 === y2 ? `M ${x1} ${y1} H ${x2}` : `M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`;
    } else {
      const x1 = a.x + a.w / 2;
      const x2 = b.x + b.w / 2;
      const y1 = a.y + a.h;
      const y2 = b.y - 2;
      const my = (y1 + y2) / 2;
      path = x1 === x2 ? `M ${x1} ${y1} V ${y2}` : `M ${x1} ${y1} C ${x1} ${my}, ${x2} ${my}, ${x2} ${y2}`;
    }
    wires.push(e.done ? { from: e.from, to: e.to, kind: e.kind, d: path, done: true } : { from: e.from, to: e.to, kind: e.kind, d: path });
  }

  const arcs = wires.filter((w) => w.apex);
  const width = Math.max(0, ...boxes.map((p) => p.x + p.w), ...arcs.map((w) => w.apex!.x + LOOP_PILL_ROOM));
  const height = Math.max(0, ...boxes.map((p) => p.y + p.h), ...arcs.map((w) => w.apex!.y + LOOP_PILL_ROOM));
  const rise = Math.max(0, ...arcs.map((w) => LOOP_PILL_ROOM - w.apex!.y));
  return { boxes, wires, width, height, gap: m.gap, rise: arcs.length ? rise : 0 };
}

/** Room kept past a return wire's pill (half its height and a margin). */
export const LOOP_PILL_ROOM = 14;

/** Where the horizontal return wire leaves the last repeated box and comes
 * back up into the first one, as a share of each box's width: it spans as
 * much of the loop as it can, so a one-stop loop is still a U. */
export const LOOP_FROM = 0.72;
export const LOOP_TO = 0.28;

/** How far under the lowest box it passes the return wire runs. */
export const LOOP_DROP = 22;

/** A return wire's reach past the column on a phone, from its span: taller
 * for a longer reach, never less than 30 px nor more than 84. */
export function loopRise(span: number): number {
  return Math.round(Math.min(84, Math.max(30, 18 + Math.abs(span) * 0.16)));
}

/** The return wire from box `a` back to box `b` — the path a Loop would
 * draw, for the canvas's preview while one is being drawn or re-aimed. */
export function loopWirePath(a: Placed, b: Placed, boxes: readonly Placed[], vertical = false): string {
  return loopArc(a, b, (p) => p.h / 2, vertical, boxes).d;
}

/** A Loop's return wire — a flowchart's loop-back. Horizontal: down out of
 * the bottom of the last box it repeats, back along under the line (under
 * every box in its span: branches, children), and up into the bottom of the
 * first, arrowhead on it, rounded corners. Vertical (a phone): out of the last
 * box's right edge, round the right of the column, into the first one's right
 * edge. `apex` is the middle of its far run, where its pill sits. */
function loopArc(
  a: Placed,
  b: Placed,
  anchorOf: (p: Placed) => number,
  vertical: boolean,
  boxes: readonly Placed[]
): { d: string; apex: { x: number; y: number } } {
  if (!vertical) {
    const x1 = a.x + a.w * LOOP_FROM;
    const x2 = b.x + b.w * LOOP_TO;
    const lo = Math.min(a.x, b.x);
    const hi = Math.max(a.x + a.w, b.x + b.w);
    const floor = Math.max(a.y + a.h, b.y + b.h, ...boxes.filter((p) => p.x < hi && p.x + p.w > lo).map((p) => p.y + p.h));
    const yb = floor + LOOP_DROP;
    const r = Math.max(0, Math.min(10, Math.abs(x1 - x2) / 2, LOOP_DROP / 2));
    const y1 = a.y + a.h;
    const y2 = b.y + b.h + 2;
    const dir = x2 <= x1 ? -1 : 1;
    return {
      d: `M ${x1} ${y1} V ${yb - r} Q ${x1} ${yb} ${x1 + dir * r} ${yb} H ${x2 - dir * r} Q ${x2} ${yb} ${x2} ${yb - r} V ${y2}`,
      apex: { x: (x1 + x2) / 2, y: yb },
    };
  }
  // The cubic's control points stand 4/3 of the reach out: its midpoint
  // (the apex) then sits the full reach out.
  const x1 = a.x + a.w;
  const x2 = b.x + b.w + 2;
  const y1 = a.y + anchorOf(a);
  const y2 = b.y + anchorOf(b);
  const h = loopRise(y2 - y1);
  const cx = Math.max(x1, x2) + (h * 4) / 3;
  return {
    d: `M ${x1} ${y1} C ${cx} ${y1}, ${cx} ${y2}, ${x2} ${y2}`,
    apex: { x: 0.125 * x1 + 0.75 * cx + 0.125 * x2, y: (y1 + y2) / 2 },
  };
}

/** Where a box dragged along the line lands: the index it takes among the
 * stops, counted from the centres of the OTHER stops (line order, the dragged
 * one left out) that its own centre has passed. */
export function dropIndex(otherCenters: readonly number[], at: number): number {
  return otherCenters.filter((c) => c < at).length;
}

/** The diagram with stop `from` moved to `to` (indexes among the stops): the
 * line's connectors follow the new order — a drag's preview, and what a drop
 * commits through `moveStep`. */
export function reorderDiagram(d: Diagram, from: number, to: number): Diagram {
  const main = d.nodes.filter((n) => n.row === "main");
  // A group box is one place, as the canvas counts them.
  const isPlace = (n: DiagramNode) => n.kind === "stop" || n.kind === "group";
  const stops = main.filter(isPlace);
  if (from === to || !stops[from] || to < 0 || to >= stops.length) return d;
  const moved = stops.slice();
  const [it] = moved.splice(from, 1);
  moved.splice(to, 0, it);
  let k = 0;
  const line = main.map((n) => (isPlace(n) ? moved[k++] : n));
  const lineIds = new Set(line.map((n) => n.id));
  // Branches follow the stop they fan out of and join at its new next box.
  const next = new Map(line.slice(0, -1).map((n, i) => [n.id, line[i + 1].id]));
  const joinDone = new Map(d.edges.filter((e) => e.kind === "join").map((e) => [e.from, !!e.done]));
  const rejoin = (n: DiagramNode) => (n.branch && next.has(n.branch.from) ? { ...n, branch: { ...n.branch, to: next.get(n.branch.from)! } } : n);
  const branches = d.nodes.filter((n) => n.row === "branch").map(rejoin);
  // The children hung off the line, likewise, in their own place.
  const rest = d.nodes.filter((n) => n.row !== "main" && n.row !== "branch").map(rejoin);
  const hung = rest.filter((n) => !!n.branch);
  const fanned = new Set([...branches, ...hung].map((n) => n.branch?.from));
  const edges = d.edges.filter((e) => e.kind !== "join" && !(lineIds.has(e.from) && lineIds.has(e.to) && (e.kind === "line" || e.kind === "add")));
  for (let i = 1; i < line.length; i++)
    if (!fanned.has(line[i - 1].id)) edges.push({ from: line[i - 1].id, to: line[i].id, kind: line[i].kind === "add" ? "add" : "line" });
  for (const b of [...branches, ...hung])
    if (b.branch) edges.push({ from: b.id, to: b.branch.to, kind: "join", ...(joinDone.get(b.id) ? { done: true } : {}) });
  return { ...d, nodes: [...rest, ...line, ...branches], edges };
}

// --- The Behaviors tile (docs/behaviors.md §1) --------------------------------------

/** The Behaviors tile's node id. */
export const BEHAVIORS_ID = "behaviors";

/** The chain's Verify stop that stands in for the Behaviors tile: the LAST
 * plain stop of kind `verify` on the line, else null. */
export function verifyStopOf(d: Pick<Diagram, "nodes">): DiagramNode | null {
  const main = d.nodes.filter((n) => n.row === "main" && n.kind === "stop" && n.step?.kind === "verify");
  return main.length ? main[main.length - 1] : null;
}

/** The line with its session's behaviors on it, as the LAST tile of the
 * chain: the Verify stop wears `mark` when the chain has one (that stop IS
 * the tile — no second one), else a derived `behaviors` node goes after the
 * last stop (right of the session's agent when there are none), wired in
 * with a plain line (a join out of every member of a trailing parallel
 * group). It is not a flight step: no index, no step, never editable. A
 * rendering view — `mark` null leaves the diagram as it is. */
export function withBehaviors(d: Diagram, mark: BehaviorsMark | null | undefined): Diagram {
  if (!mark) return d;
  const verify = verifyStopOf(d);
  // A halted Verify stop says so on its face ("Failed · 2 — halted").
  if (verify) return { ...d, nodes: d.nodes.map((n) => (n.id === verify.id ? { ...n, behaviors: { ...mark, short: verifyStopFace(mark.short, n.state) } } : n)) };
  const main = d.nodes.filter((n) => n.row === "main" && n.kind !== "add" && n.kind !== "parent");
  const session = main.find((n) => n.kind === "session");
  const line = main.filter((n) => n.kind !== "session");
  const last = line[line.length - 1] || session;
  if (!last) return d;
  const node: DiagramNode = { id: BEHAVIORS_ID, kind: "behaviors", row: "main", title: "Verify", sub: mark.short, state: "pending", icon: "behaviors", behaviors: mark };
  // A trailing parallel group joins into the tile out of each member.
  const pid = last.parallel?.id;
  const from = pid ? line.filter((n) => n.parallel?.id === pid) : [last];
  const edges: DiagramEdge[] = [...d.edges, ...from.map((n): DiagramEdge => ({ from: n.id, to: BEHAVIORS_ID, kind: from.length > 1 ? "join" : "line" }))];
  return { ...d, nodes: [...d.nodes, node], edges };
}
