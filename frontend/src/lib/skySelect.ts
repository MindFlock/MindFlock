/** Selecting on the Sky (docs/flights.md "As built: round 11 selection"): a
 * drag on empty sky draws a marquee, and what it touched becomes the
 * selection — whole lines, or the stop boxes of ONE line. Pure geometry and
 * bookkeeping here; SkyView measures (plane px: the camera's zoom and pan
 * already taken off) and SkySelection acts.
 *
 * The rules:
 * - a line is touched WHOLE when the marquee crosses its title bar (its
 *   `heads`) or covers at least half of it (`WHOLE_LINE`) — a session box or
 *   a grounded line's "No session yet" box is not a head: a sweep over the
 *   first stops clips it too easily;
 * - a stop box (a node id `stop:…` or `group:…`) is touched when the marquee
 *   overlaps it at all;
 * - stops of exactly one line and no whole line → those stops; anything else
 *   → lines (every whole line, and every line a touched stop sits on).
 *
 * A group box stands for its sub-steps (`stepIndexesOf`). */

import type { Flight, FlightStep } from "./flights";
import type { Pos, Rect } from "./skyLayout";

/** A line as the marquee sees it: its rect and its heads — the title bar —
 * (plane px). */
export interface SelectLine {
  id: string;
  rect: Rect;
  heads: readonly Rect[];
}

/** A stop box on a line (plane px). */
export interface SelectStop {
  line: string;
  node: string;
  rect: Rect;
}

/** What is selected: whole lines, or stops of one line (a line id → its
 * selected node ids). Never both. */
export interface SkySelection {
  lines: ReadonlySet<string>;
  stops: ReadonlyMap<string, ReadonlySet<string>>;
}

export const NO_SELECTION: SkySelection = { lines: new Set(), stops: new Map() };

/** The share of a line the marquee must cover to take it whole without
 * crossing a head. */
export const WHOLE_LINE = 0.5;

/** A drag shorter than this (screen px) is a click, not a marquee. */
export const MARQUEE_MIN = 4;

/** The rectangle between two corners, any way round. */
export function rectFrom(a: Pos, b: Pos): Rect {
  return { x: Math.min(a.x, b.x), y: Math.min(a.y, b.y), w: Math.abs(a.x - b.x), h: Math.abs(a.y - b.y) };
}

/** The area two rectangles share (0 when apart or only touching edges). */
export function overlapArea(a: Rect, b: Rect): number {
  const w = Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x);
  const h = Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y);
  return w > 0 && h > 0 ? w * h : 0;
}

export const overlaps = (a: Rect, b: Rect) => overlapArea(a, b) > 0;

/** The line is taken whole: its bar crossed, or half of it covered. */
export function lineTouched(m: Rect, l: SelectLine): boolean {
  if (l.heads.some((h) => overlaps(m, h))) return true;
  const area = l.rect.w * l.rect.h;
  return area > 0 && overlapArea(m, l.rect) >= area * WHOLE_LINE;
}

/** A node id the selection can hold as a stop: a stop, or a group box. */
export const isStopNode = (id: string) => id.startsWith("stop:") || id.startsWith("group:");

/** What a marquee `m` selects among `items`. */
export function select(m: Rect, items: { lines: readonly SelectLine[]; stops: readonly SelectStop[] }): SkySelection {
  const whole = new Set(items.lines.filter((l) => lineTouched(m, l)).map((l) => l.id));
  const stops = new Map<string, Set<string>>();
  for (const s of items.stops) {
    if (!isStopNode(s.node) || !overlaps(m, s.rect)) continue;
    const set = stops.get(s.line) || new Set<string>();
    set.add(s.node);
    stops.set(s.line, set);
  }
  if (!whole.size && stops.size === 1) return { lines: new Set(), stops };
  for (const id of stops.keys()) whole.add(id);
  return { lines: whole, stops: new Map() };
}

/** Shift+drag: `b` added to `a`, under the same one-line rule. */
export function addSelection(a: SkySelection, b: SkySelection): SkySelection {
  if (!a.lines.size && !b.lines.size) {
    const ids = new Set([...a.stops.keys(), ...b.stops.keys()]);
    if (ids.size <= 1) {
      const out = new Map<string, Set<string>>();
      for (const [line, set] of [...a.stops, ...b.stops]) out.set(line, new Set([...(out.get(line) || []), ...set]));
      return { lines: new Set(), stops: out };
    }
  }
  return { lines: new Set([...a.lines, ...a.stops.keys(), ...b.lines, ...b.stops.keys()]), stops: new Map() };
}

/** Every one of these lines (Ctrl/⌘+A). */
export const selectLines = (ids: Iterable<string>): SkySelection => ({ lines: new Set(ids), stops: new Map() });

/** One line in or out (Shift+click on its bar). */
export function toggleLine(s: SkySelection, id: string): SkySelection {
  const lines = new Set([...s.lines, ...s.stops.keys()]);
  if (s.lines.has(id)) lines.delete(id);
  else lines.add(id);
  return { lines, stops: new Map() };
}

/** One box in or out (Shift+click on a stop or group box): the selection
 * becomes that line's stops. Boxes of another line, or whole lines, give way
 * — a stop selection is always one line's. Emptying it clears everything. */
export function toggleStop(s: SkySelection, line: string, node: string): SkySelection {
  const mine = s.stops.size === 1 && s.stops.has(line) && !s.lines.size ? new Set(s.stops.get(line)) : new Set<string>();
  if (mine.has(node)) mine.delete(node);
  else mine.add(node);
  if (!mine.size) return NO_SELECTION;
  return { lines: new Set(), stops: new Map([[line, mine]]) };
}

/** Only what is still on the board (a closed session's line drops out). */
export function pruneSelection(s: SkySelection, shown: ReadonlySet<string>): SkySelection {
  const gone = [...s.lines, ...s.stops.keys()].some((id) => !shown.has(id));
  if (!gone) return s;
  return { lines: new Set([...s.lines].filter((id) => shown.has(id))), stops: new Map([...s.stops].filter(([id]) => shown.has(id))) };
}

export const selectionEmpty = (s: SkySelection) => !s.lines.size && !s.stops.size;

/** The one line whose stops are selected, and its node ids. */
export function stopSelection(s: SkySelection): { line: string; nodes: string[] } | null {
  const first = [...s.stops][0];
  return first && first[1].size ? { line: first[0], nodes: [...first[1]] } : null;
}

/** The flight's step indexes a stop node stands for: a stop its own; a group
 * box (`group:<gid>`, or `group:<gid>@<index>` for a later run of the same
 * id) the consecutive run of its sub-steps. Nothing for any other node. */
export function stepIndexesOf(node: string, steps: readonly Pick<FlightStep, "id" | "group">[]): number[] {
  if (node.startsWith("stop:")) {
    const id = node.slice(5);
    const i = steps.findIndex((s, k) => (s.id || "i" + k) === id);
    return i < 0 ? [] : [i];
  }
  if (!node.startsWith("group:")) return [];
  const rest = node.slice(6);
  const at = rest.lastIndexOf("@");
  const gid = at >= 0 ? rest.slice(0, at) : rest;
  const from = at >= 0 ? Number(rest.slice(at + 1)) : steps.findIndex((s) => s.group?.id === gid);
  if (!Number.isInteger(from) || from < 0 || steps[from]?.group?.id !== gid) return [];
  const out: number[] = [];
  for (let i = from; i < steps.length && steps[i].group?.id === gid; i++) out.push(i);
  return out;
}

/** Every step index the nodes stand for, sorted, once each. */
export function selectedIndexes(nodes: Iterable<string>, steps: readonly Pick<FlightStep, "id" | "group">[]): number[] {
  const all = new Set<number>();
  for (const n of nodes) for (const i of stepIndexesOf(n, steps)) all.add(i);
  return [...all].sort((a, b) => a - b);
}

/** The indexes sit next to each other (Group needs that). */
export function consecutive(indexes: readonly number[]): boolean {
  const s = [...new Set(indexes)].sort((a, b) => a - b);
  return s.length > 0 && s[s.length - 1] - s[0] === s.length - 1;
}

/** How to take the nodes off their line: a group box by one of its steps
 * with `group` (the whole group goes), a stop by its own id. */
export function removals(nodes: Iterable<string>, steps: readonly Pick<FlightStep, "id" | "group">[]): Array<{ step: string; group: boolean }> {
  const out: Array<{ step: string; group: boolean }> = [];
  for (const n of nodes) {
    const idx = stepIndexesOf(n, steps);
    if (!idx.length) continue;
    out.push({ step: steps[idx[0]].id, group: n.startsWith("group:") });
  }
  return out;
}

/** A rectangle on screen (a box's getBoundingClientRect, relative to the
 * viewport's own corner) on the plane. */
export function screenToPlane(cam: { x: number; y: number; z: number }, r: Rect): Rect {
  const z = cam.z || 1;
  return { x: (r.x - cam.x) / z, y: (r.y - cam.y) / z, w: r.w / z, h: r.h / z };
}

/** And back: a plane rectangle where it is drawn in the viewport. */
export function planeToScreen(cam: { x: number; y: number; z: number }, r: Rect): Rect {
  return { x: cam.x + r.x * cam.z, y: cam.y + r.y * cam.z, w: r.w * cam.z, h: r.h * cam.z };
}

// --- The tool: drag mode or click mode (docs/flights.md "As built: round 12 sky")

/** What a press on the sky does. `drag` (the default): a line moves by its
 * bar, empty sky draws the marquee. `click`: nothing moves — a drag anywhere
 * pans, a click focuses a line or selects a box, Shift+drag draws the
 * marquee. Space held is click mode for as long as it is held. */
export type SkyTool = "drag" | "click";
export const SKY_TOOLS: readonly SkyTool[] = ["drag", "click"];
export const SKY_TOOL_KEY = "mf_sky_tool";

/** What a press landed on: empty sky, a line's bar (or the intake lane's
 * head), a box (or the rest of a line), or a control that takes the press
 * itself (a button, a field, an open card, a menu). */
export type SkyTarget = "ground" | "line" | "box" | "control";

/** What the press becomes. `select` is the box's own press (its click selects
 * it, its drag reorders, as before); `none` leaves the press to its target. */
export type SkyGesture = "pan" | "marquee" | "move-line" | "select" | "none";

/** The one decision both tools share. In click mode a press that never moves
 * is still a click on its target (a line is focused, a box selected). */
export function gestureFor(tool: SkyTool, target: SkyTarget, mods: { shift?: boolean; space?: boolean; touch?: boolean } = {}): SkyGesture {
  if (mods.space) return "pan";
  if (target === "control") return "none";
  if (tool === "click") return mods.shift && !mods.touch ? "marquee" : "pan";
  if (target === "ground") return mods.touch ? "pan" : "marquee";
  return target === "line" ? "move-line" : "select";
}

/** The tool this device remembers (`mf_sky_tool`), drag unless it says click. */
export function loadSkyTool(store: Pick<Storage, "getItem"> | null = localStore()): SkyTool {
  try {
    return store?.getItem(SKY_TOOL_KEY) === "click" ? "click" : "drag";
  } catch {
    return "drag";
  }
}

export function saveSkyTool(tool: SkyTool, store: Pick<Storage, "setItem"> | null = localStore()): void {
  try {
    store?.setItem(SKY_TOOL_KEY, tool);
  } catch {
    /* a private window: the tool lasts this visit */
  }
}

function localStore(): Storage | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
}

// --- Delete on the sky -------------------------------------------------------------

/** What Delete / Backspace does on the sky, from where the keyboard is:
 * - `selection`: the marquee's selection holds something — its bar removes;
 * - `box`: a box on line `line` (focused, or the focused line's selected box)
 *   — a stop or a group leaves the line (undoable), a session box asks to
 *   close that session;
 * - `line`: a line itself (its bar, a grounded line's head) — a grounded line
 *   asks to be removed, a session's line asks to close its session;
 * - `swallow`: a box Delete means nothing on (the +, a rung, the flown pill,
 *   the intake lane) — taken, so the keymap does not close the session;
 * - `keymap`: nothing of the sky's — the app's Delete (close the focused
 *   session) as before. */
export type SkyDelete =
  | { kind: "selection" }
  | { kind: "box"; line: string; node: string }
  | { kind: "line"; line: string }
  | { kind: "swallow" }
  | { kind: "keymap" };

/** A node Delete acts on: a stop or group (removed) or a session's box
 * (closed). */
export const deletableNode = (node: string) => isStopNode(node) || /^(session|child|branch):./.test(node);

/** The session a session-ish box stands for (`session:` / `child:` /
 * `branch:` + title), or "". */
export const nodeSession = (node: string) => {
  const m = /^(session|child|branch):(.+)$/.exec(node);
  return m ? m[2] : "";
};

/** Where Delete goes. `focus` is where the keyboard is on the sky: the line
 * it is on (a slot id, or "intake"), the box node it is on ("" for the line
 * itself), or nothing (the sky's empty space, the body). `selectedBox` is the
 * box the focused line has selected (its canvas's own selection), if any. */
export function deleteFor(
  selection: SkySelection,
  focus: { line: string; node: string } | null,
  selectedBox: { line: string; node: string } | null
): SkyDelete {
  if (!selectionEmpty(selection)) return { kind: "selection" };
  const at = focus || selectedBox;
  if (!at) return { kind: "keymap" };
  if (at.line === "intake") return { kind: "swallow" };
  if (!at.node) return { kind: "line", line: at.line };
  return deletableNode(at.node) ? { kind: "box", line: at.line, node: at.node } : { kind: "swallow" };
}

/** A box on a drawn line by its node id: one of the line's own, or a
 * sub-step of an open group's. */
export function nodeById<N extends { id: string; group?: { nodes: readonly N[] } }>(nodes: readonly N[], id: string): N | null {
  for (const n of nodes) {
    if (n.id === id) return n;
    const sub = n.group?.nodes.find((s) => s.id === id);
    if (sub) return sub;
  }
  return null;
}

/** Whether Delete may take this box off its line (what the canvas's own
 * Delete checks): a stop or a group the flight can still change. */
export const removableBox = (n: { kind: string; editable?: boolean } | null): boolean => !!n && (n.kind === "stop" || n.kind === "group") && n.editable !== false;

// --- Round 13: a grounded line dragged onto a session's line ----------------------

/** The session line a grounded line being dragged by its bar would merge into
 * (docs/flights.md "As built: round 13 sky"): the topmost of `lines` (drawn
 * order: the last drawn is on top) whose rect holds `point` (plane px) and is
 * a SESSION's line — never a grounded line (`flight:`), the intake lane, the
 * floating palette, or the line being dragged (`except`). "" = none: the
 * drop just moves it. */
export function mergeTargetAt(point: Pos, lines: readonly { id: string; rect: Rect }[], except = ""): string {
  for (let i = lines.length - 1; i >= 0; i--) {
    const { id, rect: r } = lines[i];
    if (!id || id === except || id === "intake" || id === "palette" || id.startsWith("flight:")) continue;
    if (point.x >= r.x && point.x <= r.x + r.w && point.y >= r.y && point.y <= r.y + r.h) return id;
  }
  return "";
}

// --- Round 14: deletion asks only when something runs ---------------------------

/** What a removal would take (docs/flights.md "Round 14" → "Deletion asks
 * only when something runs"):
 * - `session`: a session's line (closing it) — its row's `activity` /
 *   `asking`;
 * - `stops`: stops of one flight (a group counts as all its steps) — their
 *   step indexes, and the flight's `state` / `cursor`;
 * - `grounded`: a grounded line — never in the air;
 * - `selection`: several of these at once. */
export type RemovalTarget =
  | { kind: "session"; row: { activity?: string; asking?: unknown } | null | undefined }
  | { kind: "stops"; indexes: readonly number[]; flight: { state: string; cursor: number } | null | undefined }
  | { kind: "grounded" }
  | { kind: "selection"; items: readonly RemovalTarget[] };

/** A session's agent is working, or asking you something. */
export const sessionRuns = (row: { activity?: string; asking?: unknown } | null | undefined): boolean =>
  !!row && (row.activity === "working" || row.activity === "clarify" || !!row.asking);

/** The flight is in the air and its current stop is one of these. */
export const stopsRun = (indexes: readonly number[], flight: { state: string; cursor: number } | null | undefined): boolean =>
  !!flight && (flight.state === "running" || flight.state === "waiting_you") && indexes.includes(flight.cursor);

/** Ask before removing? Only when something is actually running: a session
 * whose agent works or asks, the stop a running flight is at, a selection
 * holding any of those. Everything else goes at once, with Undo. */
export function removalNeedsAsk(t: RemovalTarget): boolean {
  switch (t.kind) {
    case "session":
      return sessionRuns(t.row);
    case "stops":
      return stopsRun(t.indexes, t.flight);
    case "grounded":
      return false;
    case "selection":
      return t.items.some(removalNeedsAsk);
  }
}

/** The first index the server takes a stop put back at (`_first_open` and
 * the Undo rule of `POST /steps`, docs/flights.md "Undo"): any on a route
 * not flying yet (grounded, ready); the cursor itself on a paused route
 * whose current stop has not started (a removed flying stop flies first on
 * Resume); else the one after the current stop (its parallel group). */
export function undoFirstOpen(f: Pick<Flight, "state" | "cursor" | "steps" | "step_states">): number {
  if (f.state === "grounded" || f.state === "ready") return 0;
  const cur = f.steps[f.cursor];
  const ss = cur ? f.step_states?.[cur.id] : undefined;
  if (f.state === "paused" && (!cur || ((ss?.state ?? "pending") === "pending" && !ss?.detail?.performing))) return f.cursor;
  let end = f.cursor + 1;
  const par = cur?.parallel?.id;
  while (par && f.steps[end]?.parallel?.id === par) end++;
  return end;
}

/** Where Undo puts a stop removed from `index` back: there while the server
 * takes that place on the route as it is `now` (`undoFirstOpen`) and it is
 * inside the line, else the end (undefined). */
export function undoAt(index: number, now: Pick<Flight, "state" | "cursor" | "steps" | "step_states"> | null): number | undefined {
  return now && index >= undoFirstOpen(now) && index < now.steps.length ? index : undefined;
}
