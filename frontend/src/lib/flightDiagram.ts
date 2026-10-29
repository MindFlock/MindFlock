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
 *                     ("Tack on a stop").
 *
 * `layoutDiagram` places the boxes and draws the connectors as SVG paths from
 * fixed box sizes, so the markup (and a test of it) needs no DOM measuring. */

import type { AutopilotRun, Instance } from "../api/types";
import { LANE_LABEL, normalizeLane, type Lane } from "./autopilot";
import { deriveSessionState } from "./sessionState";
import {
  isLive,
  perchLabel,
  stepActor,
  stepStateOf,
  stripDots,
  youItems,
  type Flight,
  type FlightRow,
  type FlightStep,
  type StepKindDef,
} from "./flights";

// --- The model ----------------------------------------------------------------

export type DiagramNodeKind = "parent" | "session" | "child" | "rung" | "stop" | "add";

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

/** Where a box sits: on the line, above the session (its parent), or on the
 * branch below it (its children). */
export type DiagramRow = "main" | "above" | "below";

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
  /** Why it stopped (a halted stop or rung). */
  reason?: string;
}

export type DiagramEdgeKind = "line" | "up" | "branch" | "add";

export interface DiagramEdge {
  from: string;
  to: string;
  kind: DiagramEdgeKind;
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

/** A stop's config in one line, the way its box says it: "push", "10 min
 * quiet", "who: agent", "3 to tick". */
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
      return "who: " + String(c.who || "both") + (c.wait_live ? " · once live" : "");
    case "you": {
      const items = youItems(step as FlightStep);
      if (items.length) return items.length + (items.length === 1 ? " item" : " items") + " to tick";
      const t = String(c.text ?? "").trim();
      return t ? clip(t) : "waits for your Done";
    }
    case "wait": {
      const until = String(c.until ?? "").trim();
      return until ? "until " + until : num(c.minutes, 10) + " min";
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
    default:
      return step.kind;
  }
}

/** A stop's title as its box says it: a perch reads "Perch: <title>". */
export function stopTitle(step: FlightStep, kinds?: readonly StepKindDef[] | null): string {
  return stepActor(step, kinds) === "you" ? perchLabel(step.title) : step.title;
}

// --- A flyway, a flight ---------------------------------------------------------

const stopId = (s: FlightStep, i: number) => "stop:" + (s.id || "i" + i);

function chain(nodes: DiagramNode[], edges: DiagramEdge[], line: DiagramNode[]) {
  for (let i = 1; i < line.length; i++)
    edges.push({ from: line[i - 1].id, to: line[i].id, kind: line[i].kind === "add" ? "add" : "line" });
  nodes.push(...line);
}

function addNode(title: string): DiagramNode {
  return { id: "add", kind: "add", row: "main", title, sub: "", state: "pending", icon: "add" };
}

/** A flyway being drawn: its stops, then the trailing "+". */
export function flywayDiagram(steps: readonly FlightStep[], kinds?: readonly StepKindDef[] | null): Diagram {
  const line: DiagramNode[] = steps.map((s, i) => ({
    id: stopId(s, i),
    kind: "stop",
    row: "main",
    title: stopTitle(s, kinds),
    sub: stopSummary(s),
    state: "edit",
    icon: s.kind,
    perch: stepActor(s, kinds) === "you",
    step: s,
    index: i,
    editable: true,
  }));
  line.push(addNode("Add a stop"));
  const nodes: DiagramNode[] = [];
  const edges: DiagramEdge[] = [];
  chain(nodes, edges, line);
  return { nodes, edges, flightId: "", mode: "none", insertFrom: 0 };
}

/** A flight's stops with their live state. */
export function flightStops(f: Flight, kinds?: readonly StepKindDef[] | null): DiagramNode[] {
  const live = isLive(f);
  return f.steps.map((s, i) => {
    const st = stepStateOf(f, s, i);
    const atCursor = i === f.cursor && live;
    let state: DiagramNodeState =
      st === "done" ? "done" : st === "skipped" ? "skipped" : st === "failed" ? "failed" : st === "waiting_you" ? "perched" : st === "running" ? "current" : "pending";
    if (atCursor && f.state === "halted") state = "halted";
    else if (atCursor && f.state === "paused") state = "paused";
    const rec = f.step_states?.[s.id];
    return {
      id: stopId(s, i),
      kind: "stop" as const,
      row: "main" as const,
      title: stopTitle(s, kinds),
      sub: stopSummary(s),
      state,
      icon: s.kind,
      perch: stepActor(s, kinds) === "you",
      step: s,
      index: i,
      editable: live && state === "pending" && i > f.cursor,
      reason: state === "halted" || state === "failed" ? f.reason || rec?.note || "" : "",
    };
  });
}

/** A flight on its own (no session box): its stops, then the trailing "+"
 * while it is in the air. */
export function flightDiagram(f: Flight, kinds?: readonly StepKindDef[] | null): Diagram {
  const line = flightStops(f, kinds);
  if (isLive(f)) line.push(addNode("Tack on a stop"));
  const nodes: DiagramNode[] = [];
  const edges: DiagramEdge[] = [];
  chain(nodes, edges, line);
  return { nodes, edges, flightId: f.id, mode: "flight", insertFrom: f.cursor + 1 };
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
  if (fl && isLive(fl)) return stripDots(fl);
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
    title: row.title,
    sub: String(row.task_headline || row.task || "").split("\n")[0].trim(),
    state: "session",
    icon: "session",
    session: row.title,
    chip: { label: ss.label, tone: ss.tone },
    dots: kind === "child" ? miniDots(row) : undefined,
  };
}

export interface SessionDiagramOpts {
  /** The full flights (the shared flights query), so the session's stops carry
   * their titles and states; without them the row's own `flight` draws them. */
  flights?: readonly Flight[] | null;
  kinds?: readonly StepKindDef[] | null;
  /** Leave the parent and children out (the Board's one-row card). */
  family?: boolean;
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
  const fl = row.flight as FlightRow | null | undefined;
  if (fl && isLive(fl)) {
    mode = "flight";
    flightId = fl.id;
    insertFrom = fl.cursor + 1;
    const full = opts.flights?.find((f) => f.id === fl.id);
    if (full) line.push(...flightStops(full, opts.kinds));
    else
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
          sub: "fast-track" + (r.state === "current" && row.autopilot?.note ? " · " + clip(row.autopilot.note, 24) : ""),
          state: r.state,
          icon: "ship",
          reason: r.state === "halted" ? reason : "",
        });
    }
  }
  line.push(addNode("Tack on a stop"));
  chain(nodes, edges, line);

  if (family)
    for (const k of kids) {
      const c = sessionBox(k, "child", "below");
      nodes.push(c);
      edges.push({ from: me.id, to: c.id, kind: "branch" });
    }
  return { nodes, edges, flightId, mode, insertFrom };
}

/** Whether a session has anything for its Flight tab to draw beyond itself: a
 * flight in the air, workers, a parent or an armed fast-track. */
export function hasSessionDiagram(rows: readonly Instance[], title: string): boolean {
  const row = rows.find((r) => r.title === title);
  if (!row) return false;
  const fl = row.flight as FlightRow | null | undefined;
  return !!(fl && isLive(fl)) || ladderRungs(row).length > 0 || !!row.parent || rows.some((r) => r.parent === title);
}

// --- The bird -------------------------------------------------------------------

/** How the bird sits: bobbing while the flock works, gold and still perched on
 * you, red when halted, still when paused. */
export type BirdTone = "working" | "you" | "halted" | "paused";

export interface BirdPerch {
  nodeId: string;
  tone: BirdTone;
}

const STOP_TONE: Partial<Record<DiagramNodeState, BirdTone>> = {
  current: "working",
  perched: "you",
  halted: "halted",
  paused: "paused",
};

/** Where the bird sits on a line: the flight's current stop, else the
 * fast-track's current rung, else the session box while its agent works (or
 * waits on you); nowhere when the session is idle with nothing armed. */
export function birdPerch(d: Diagram): BirdPerch | null {
  const main = d.nodes.filter((n) => n.row === "main");
  const stop = main.find((n) => n.kind === "stop" && STOP_TONE[n.state]);
  if (stop) return { nodeId: stop.id, tone: STOP_TONE[stop.state]! };
  const rung = main.find((n) => n.kind === "rung" && (n.state === "current" || n.state === "halted"));
  if (rung) return { nodeId: rung.id, tone: rung.state === "halted" ? "halted" : "working" };
  const me = main.find((n) => n.kind === "session");
  if (me?.chip?.tone === "working") return { nodeId: me.id, tone: "working" };
  if (me?.chip?.tone === "attention") return { nodeId: me.id, tone: "you" };
  return null;
}

// --- Layout -------------------------------------------------------------------

export interface BoxSize {
  w: number;
  h: number;
}

export interface LayoutOpts {
  compact?: boolean;
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
  /** The SVG path, arrowhead at its end. */
  d: string;
}

export interface Layout {
  boxes: Placed[];
  wires: Wire[];
  width: number;
  height: number;
  /** The gap between two boxes on the line (where a "+" can sit). */
  gap: number;
}

/** The base box and the line's spacing. */
export function boxMetrics(compact = false): { box: BoxSize; add: BoxSize; gap: number; vgap: number; indent: number; anchor: number } {
  return compact
    ? { box: { w: 140, h: 62 }, add: { w: 44, h: 44 }, gap: 34, vgap: 22, indent: 22, anchor: 31 }
    : { box: { w: 178, h: 76 }, add: { w: 148, h: 40 }, gap: 46, vgap: 30, indent: 26, anchor: 38 };
}

/** Place every box and draw every connector. Horizontal: the line left to
 * right, the parent above the session, the children stacked below it on a
 * branch from its left edge. Vertical: the line top to bottom, the children in
 * a column to the session's right. */
export function layoutDiagram(d: Diagram, opts: LayoutOpts = {}): Layout {
  // A phone (or a narrow pane) draws two columns at most: compact boxes,
  // and the + as wide as a box.
  const m = boxMetrics(!!opts.compact || !!opts.vertical);
  if (opts.vertical) m.add = { w: m.box.w, h: 36 };
  const size = (n: DiagramNode): BoxSize => {
    // A session box also carries its state chip, a child its mini ladder.
    const base = n.kind === "add" ? m.add : { w: m.box.w, h: m.box.h + (n.chip ? 18 : 0) + (n.dots ? 14 : 0) };
    return opts.sizeOf ? opts.sizeOf(n, base) : base;
  };
  const above = d.nodes.filter((n) => n.row === "above");
  const main = d.nodes.filter((n) => n.row === "main");
  const below = d.nodes.filter((n) => n.row === "below");
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
    for (const n of main) {
      const s = size(n);
      // A smaller box (the +) sits centred on the line's anchor.
      put(n, x, top + (n.kind === "add" ? m.anchor - s.h / 2 : 0));
      x += s.w + m.gap;
    }
    const sp = session ? at.get(session.id) : undefined;
    for (const n of above) put(n, sp ? sp.x : 0, 0);
    const lineH = Math.max(m.box.h, ...main.map((n) => size(n).h));
    let y = top + lineH + m.vgap;
    for (const n of below) {
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
      put(n, n.kind === "add" ? (m.box.w - s.w) / 2 : 0, y);
      y += s.h + m.vgap;
    }
    const sp = session ? at.get(session.id) : undefined;
    let cy = sp ? sp.y : 0;
    for (const n of below) {
      put(n, m.box.w + m.indent, cy);
      cy += size(n).h + 12;
    }
  }

  // Reading (and tab) order: the parent, the line, then the branch.
  const rank: Record<DiagramRow, number> = { above: 0, main: 1, below: 2 };
  boxes.sort((p, q) => rank[p.node.row] - rank[q.node.row]);

  const wires: Wire[] = [];
  for (const e of d.edges) {
    const a = at.get(e.from);
    const b = at.get(e.to);
    if (!a || !b) continue;
    let path = "";
    if (e.kind === "up") {
      const x = a.x + Math.min(a.w, b.w) / 2;
      path = `M ${x} ${a.y + a.h} V ${b.y - 2}`;
    } else if (e.kind === "branch") {
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
      const y1 = a.y + (a.node.kind === "add" ? a.h / 2 : m.anchor);
      const y2 = b.y + (b.node.kind === "add" ? b.h / 2 : m.anchor);
      const x1 = a.x + a.w;
      const x2 = b.x - 2;
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
    wires.push({ from: e.from, to: e.to, kind: e.kind, d: path });
  }

  const width = Math.max(0, ...boxes.map((p) => p.x + p.w));
  const height = Math.max(0, ...boxes.map((p) => p.y + p.h));
  return { boxes, wires, width, height, gap: m.gap };
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
  const stops = main.filter((n) => n.kind === "stop");
  if (from === to || !stops[from] || to < 0 || to >= stops.length) return d;
  const moved = stops.slice();
  const [it] = moved.splice(from, 1);
  moved.splice(to, 0, it);
  let k = 0;
  const line = main.map((n) => (n.kind === "stop" ? moved[k++] : n));
  const lineIds = new Set(line.map((n) => n.id));
  const edges = d.edges.filter((e) => !(lineIds.has(e.from) && lineIds.has(e.to) && (e.kind === "line" || e.kind === "add")));
  for (let i = 1; i < line.length; i++)
    edges.push({ from: line[i - 1].id, to: line[i].id, kind: line[i].kind === "add" ? "add" : "line" });
  return { ...d, nodes: [...d.nodes.filter((n) => n.row !== "main"), ...line], edges };
}
