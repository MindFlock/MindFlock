/** The flyway canvas: stops as boxes on a line, read left to right (top to
 * bottom on a phone), with the connectors drawn between them — curved lines
 * with an arrowhead, no ports and no manual wiring: the order IS the sequence.
 *
 * One component, every home (lib/flightDiagram builds what it draws):
 *  - the Flyways editor — drag a box to reorder (pointer events, mouse and
 *    touch: the picked box lifts, the others slide, the drop snaps into the
 *    line and the connectors redraw), ← / → move the selected box, Delete
 *    removes it, Enter opens it in the inspector, + between boxes on hover;
 *  - a flight in the air — done boxes dimmed with ✓, the current one pulsing
 *    (gold perched on you, green while the flock works, red halted with the
 *    reason), skipped struck through, pending outlined;
 *  - a session's live diagram — its parent above, the session box, its
 *    children branching below, the fast-track's rungs or the flight's stops,
 *    and the trailing + ("Tack on a stop"); children spawned at a stop run as
 *    a column of small branch boxes between it and the next box, fanning
 *    out of the stop and back in at the join point, which wears a "2 of 5"
 *    pill while it waits (an indented group on a phone);
 *  - a line in the Sky — the same diagram, with the bird (`bird`) standing
 *    on the box the flight is at, its boxes at the line's size (`size`:
 *    compact = the title alone, wide = the full title, the summary and a
 *    second line of detail), and a box opened into a card (`openBox` — the
 *    session's real pane), measured, so its connectors follow it; how much
 *    a box draws follows the Sky's zoom (`lod`: far = the head alone, near =
 *    everything, whatever the size), never its geometry.
 *
 * Editing a stop works the way n8n edits a node (docs/flights-stop-editing.md,
 * the rules in lib/stopEditing): a stop that can change wears a toolbar on
 * hover or focus (Edit · Earlier · Later · Duplicate · Halt here · Remove),
 * the flight's current stop one of its own (Halt now / Carry on · Halt
 * here: docs/flights.md "Halt points"), a halt point wears a ⏸ badge, any box
 * opens a menu on right-click (StopMenu), Delete removes the selected stop,
 * the + between boxes inserts one, and a stop chip dragged from the shelf
 * (`STOP_MIME`) — or a saved stop (`FLYWAY_MIME`) — lights the gap it would
 * land in and is tacked on there. On a live line a stop that has flown, and
 * the one the flight is at, offer 🗑 too (removing the current one halts the
 * flight right before the stop that followed).
 *
 * Round 11: a GROUP box (`kind: "group"`, lib/flightDiagram) is one stop with
 * sub-steps, drawn as a stack; its ▸ (`onToggleGroup`) draws its sub-steps on
 * the line inside a thin labelled outline, ▾ folds them back. `onEdit` is the
 * stop's dialog: ✎, Enter / Space, a double-click and the menu's Edit open it,
 * while a click only selects (`onSelect`).
 *
 * Round 12: the bird is in hand (lib/stopEditing `birdAction`, `jumpTargets`).
 * A click on it halts the flight where it is (Carry on while held, Retry on a
 * failed stop); a drag — a long-press first on touch — carries it over the
 * line, every stop it can land on lit, and a drop on one jumps the flight
 * there (`jumpFlight`); dropped anywhere else it flies back. It calls the
 * flight routes itself, as the current stop's toolbar does: no prop. Every
 * branch whose child works or waits on you — a worker, or a child hung off
 * the line — carries a smaller bird of its own on its left edge (a click
 * opens that child, `onOpen`; it never drags).
 *
 * Empty space pans the line sideways (scroll, or drag it). Positions and
 * connectors come from `layoutDiagram` (fixed box sizes), so the markup needs
 * no measuring and a server render draws the same picture — an open card
 * alone is measured (it starts at the size it asks for). */

import { useEffect, useId, useMemo, useRef, useState, type DragEvent, type KeyboardEvent, type MouseEvent, type PointerEvent, type ReactNode, type RefObject } from "react";
import {
  birdFormation,
  birdPerches,
  joinLabel,
  layoutDiagram,
  loopIdlePill,
  loopPill,
  loopPillTitle,
  reorderDiagram,
  sizedBoxFor,
  type BoxSize,
  dropIndex,
  type BirdPerch,
  type Diagram,
  type DiagramNode,
  type LayoutOpts,
} from "../../lib/flightDiagram";
import { controlFlight, flightPath, jumpFlight, refreshFlights, stepDone, stepRetry, stepSkip } from "../../lib/flightsApi";
import { api } from "../../api/client";
import { refreshInstances } from "../../state/queries";
import { errMsg } from "../../lib/format";
import { toast } from "../../lib/toast";
import { riskTitle } from "../../lib/stopRisk";
import { InlineConfirm } from "../InlineConfirm";
import {
  birdAction,
  canHalt,
  canJumpTo,
  canShift,
  dropGapFor,
  flightHold,
  haltMark,
  haltToggle,
  insertGaps,
  isStopDrag,
  jumpFlightOf,
  jumpStepOf,
  jumpTargets,
  menuItemsFor,
  removeTool,
  stopDropOf,
  type StopDrop,
} from "../../lib/stopEditing";
import { BirdMarker, StopIcon } from "./Bird";
import { StopMenu, type SkyMenuItem, type SkyMenuKey } from "./StopMenu";
import "./FlywayCanvas.css";
import "./StopEditing.css";

/** Below this width the line runs top to bottom. */
const VERTICAL_BELOW_PX = 520;

/** How much of each box a line shows (the Sky's ◧): normal is the editor's. */
export type BoxSizeName = "compact" | "normal" | "wide";

/** How much a box draws at the Sky's zoom: far = its head (icon and title),
 * mid = as its size says, near = the title, summary, detail line, reason and
 * its parts, whatever the size. Never its geometry. */
export type BoxLod = "far" | "mid" | "near";

const firstOf = (v: unknown) =>
  String(v ?? "")
    .split("\n")
    .map((l) => l.trim())
    .find(Boolean) || "";

/** A wide box's second line: the agent's prompt, a perch's text (or its
 * items), a ship stop's commit message, the command it runs, the ping. */
export function boxDetail(n: DiagramNode): string {
  const c = n.step?.config || {};
  switch (n.step?.kind) {
    case "agent":
      return firstOf(c.prompt);
    case "you": {
      const items = Array.isArray(c.items) ? (c.items as unknown[]).map(String).filter(Boolean) : [];
      return firstOf(c.text) || items.join(" · ");
    }
    case "ship":
      return firstOf(c.message);
    case "command":
      return firstOf(c.command);
    case "notify":
      return firstOf(c.text);
    default:
      return "";
  }
}

/** A box at a line's size, from the layout's normal one: compact is ~110px
 * and one line (the title; the + a bare circle), wide is 260px with room for
 * a two-line title, the summary on two lines and the detail line. A branch
 * stays smaller than the line's boxes at every size. */
export function sizedBox(size: BoxSizeName, n: DiagramNode, base: BoxSize, detail = "", roomy = false): BoxSize {
  return sizedBoxFor(size, n, base, detail, roomy);
}

export interface FlywayCanvasProps {
  diagram: Diagram;
  /** Its accessible name. */
  label: string;
  compact?: boolean;
  /** Force the orientation (else: by the canvas's own width). */
  vertical?: boolean;
  /** The selected box (a node id). */
  selected?: string | null;
  /** Boxes to highlight (stops a description just added, until saved). */
  highlight?: ReadonlySet<string>;
  /** Boxes in the Sky's selection (a marquee over this line's stops): each
   * wears `.fl-box-selected`, beside the one `selected` box. */
  highlighted?: ReadonlySet<string>;
  /** A stop box picked (a click; and Enter, ✎, the menu's Edit when the host
   * passes no `onEdit`): open it in the inspector. */
  onSelect?(n: DiagramNode): void;
  /** Shift+click on a stop or group box: the host adds it to (or takes it
   * out of) its multi-selection instead of selecting it alone. */
  onShiftSelect?(n: DiagramNode): void;
  /** The stop's dialog (✎, Enter / Space, a double-click, the menu's Edit):
   * with it, a click only selects. */
  onEdit?(n: DiagramNode): void;
  /** A group box's ▸ / ▾ (and the menu's Show / Fold its sub-steps): draw its
   * sub-steps on the line, or one box again. */
  onToggleGroup?(groupId: string): void;
  /** A session / parent / child box, or the trailing +, pressed. */
  onOpen?(n: DiagramNode): void;
  /** Reordering: drag, and ← / → on the selected stop (indexes among stops). */
  onMove?(from: number, to: number): void;
  /** Delete on a selected stop (and the toolbar's / the menu's Remove). */
  onRemove?(n: DiagramNode): void;
  /** Duplicate (the toolbar, the menu): the same stop again, right after it. */
  onDuplicate?(n: DiagramNode): void;
  /** ⏸ Halt here / Carry on past here (the toolbar, the menu): make a stop not
   * yet flown a halt point, or no longer one. */
  onToggleHalt?(n: DiagramNode): void;
  /** A stop chip (`STOP_MIME`, its value the stop kind: `{kind}`) or a saved
   * stop (`FLYWAY_MIME`, its value the plan id: `{planId}`) dropped on the
   * line: tack it on before stop `at` — null = at the end. */
  onDropStop?(drop: StopDrop, at: number | null): void;
  /** The name of a stop kind being dragged from the shelf (the host's, like
   * `dropLabel`): the end + reads "Tack on <it>". */
  dragStop?: string | null;
  /** The + between two boxes: insert a stop before stop `at`. */
  onInsert?(at: number): void;
  /** Extra content inside a box (a perch's checklist and Done / Skip, ⋯). */
  renderInBox?(n: DiagramNode): ReactNode;
  /** A box drawn bigger than the rest. */
  sizeOf?: LayoutOpts["sizeOf"];
  /** Scroll the line so this box (the flight's current stop) is in view. */
  reveal?: string | null;
  /** The bird: standing on this box (lib/flightDiagram `birdPerch`). Passed
   * at all (null too), the line also draws a bird on each branch whose child
   * works or waits on you (`birdPerches`; a click opens that child). */
  bird?: BirdPerch | null;
  /** How much each box shows (default normal — the editor, the Flight tab). */
  size?: BoxSizeName;
  /** A box's wide second line, where the stop's own (boxDetail) has none
   * (the Sky: a session's last prompt). */
  detailOf?(n: DiagramNode): string;
  /** A flyway dragged over the line: the + lights up, saying this. */
  dropLabel?: string | null;
  /** A box drawn open as a card — `body` replaces its insides, `size` is its
   * size until the card is measured (then the layout follows the card). */
  openBox?(n: DiagramNode): { body: ReactNode; size: BoxSize } | null;
  /** How much each box draws (the Sky's semantic zoom; default mid). */
  lod?: BoxLod;
  /** The Sky's bigger boxes (lib/flightDiagram boxMetrics' roomy tier). */
  roomy?: boolean;
  dataTour?: string;
  className?: string;
}

/** ⏸ on a stop's toolbar: Halt here, pressed (Carry on past here) while the
 * stop is a halt point. */
function HaltTool({ n, onToggle }: { n: DiagramNode; onToggle(n: DiagramNode): void }) {
  const t = haltToggle(n);
  const on = t.label !== "Halt here";
  return (
    <button type="button" className={"fl-icon-btn fl-tool fl-tool-halt" + (on ? " fl-tool-on" : "")} aria-pressed={on} aria-label={t.label + " — “" + n.title + "”"} title={t.title} onClick={() => onToggle(n)}>
      ⏸
    </button>
  );
}

/** 🗑 on a stop's toolbar — what it says follows where the stop is
 * (lib/stopEditing `removeTool`): still ahead, flown, or where the flight is
 * (removing it halts the flight there). */
function RemoveTool({ n, here = false, onRemove }: { n: DiagramNode; here?: boolean; onRemove(n: DiagramNode): void }) {
  const t = removeTool(n, here);
  const said = n.kind === "stop" && t.label !== "Remove" ? "Remove “" + n.title + "”" + t.label.slice("Remove".length) : "Remove “" + n.title + "”";
  return (
    <button type="button" className="fl-icon-btn fl-tool fl-tool-remove" aria-label={said} title={n.kind === "stop" && t.label === "Remove" ? "Remove this stop from the line (Delete)" : t.title} onClick={() => onRemove(n)}>
      🗑
    </button>
  );
}

/** Round 15: a flown checkpoint stop's menu line. Danger, since it resets
 * the worktree; the press opens an inline confirm on the stage, never
 * `window.confirm` (the desktop app has none). */
export const REWIND_ITEM: SkyMenuItem = { key: "rewind", label: "Rewind to here", title: "Reset the worktree to this checkpoint and fly on from the stop after it", danger: true };

/** A stop whose menu offers Rewind: a checkpoint that has flown (done), on a
 * live flight. */
export function canRewind(n: DiagramNode, flightId: string): boolean {
  return !!flightId && n.kind === "stop" && n.step?.kind === "checkpoint" && n.state === "done";
}

/** The stop's menu with Rewind slotted in before the notes (round 15). */
export function withRewind(items: readonly SkyMenuItem[], n: DiagramNode, flightId: string): SkyMenuItem[] {
  if (!canRewind(n, flightId)) return items.slice();
  const out = items.slice();
  const at = out.findIndex((i) => i.note);
  out.splice(at < 0 ? out.length : at, 0, REWIND_ITEM);
  return out;
}

/** Where a worker's bird flies in formation: the leader's spot plus its slot
 * (lib/flightDiagram `formationSlot`, in bird-lengths) — behind along the
 * line's direction, out to the side. */
export function formationAt(leader: { x: number; y: number }, slot: { dx: number; dy: number }, size: number, vertical: boolean): { x: number; y: number } {
  const step = size * 0.9;
  return vertical ? { x: leader.x + slot.dy * step, y: leader.y + slot.dx * step } : { x: leader.x + slot.dx * step, y: leader.y + slot.dy * step };
}

const STATE_GLYPH: Partial<Record<DiagramNode["state"], string>> = {
  done: "✓",
  skipped: "–",
  failed: "✗",
  halted: "!",
};

const STATE_WORD: Partial<Record<DiagramNode["state"], string>> = {
  done: "done",
  skipped: "skipped",
  failed: "failed",
  halted: "halted",
  current: "under way",
  perched: "perched on you",
  paused: "paused here",
  pending: "still to come",
};

/** The outline of each group drawn open: the box around its sub-steps. */
export function groupFrames(boxes: readonly { node: DiagramNode; x: number; y: number; w: number; h: number }[]): Array<{ id: string; name: string; x: number; y: number; w: number; h: number }> {
  const out = new Map<string, { id: string; name: string; x: number; y: number; r: number; b: number }>();
  for (const p of boxes) {
    const g = p.node.inGroup;
    if (!g) continue;
    const f = out.get(g.id);
    if (!f) out.set(g.id, { id: g.id, name: g.name, x: p.x, y: p.y, r: p.x + p.w, b: p.y + p.h });
    else Object.assign(f, { x: Math.min(f.x, p.x), y: Math.min(f.y, p.y), r: Math.max(f.r, p.x + p.w), b: Math.max(f.b, p.y + p.h) });
  }
  return [...out.values()].map((f) => ({ id: f.id, name: f.name, x: f.x, y: f.y, w: f.r - f.x, h: f.b - f.y }));
}

/** The canvas's own width, for the phone layout (none on a server render). */
function useNarrow(ref: RefObject<HTMLElement | null>, forced?: boolean): boolean {
  const [narrow, setNarrow] = useState(false);
  useEffect(() => {
    if (forced !== undefined) return;
    const el = ref.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => setNarrow(el.clientWidth > 0 && el.clientWidth < VERTICAL_BELOW_PX));
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref, forced]);
  return forced ?? narrow;
}

/** A bird in hand: the pointer, where it went down, where it is now (stage
 * px), and the stop under it. `live` once it moved (a touch: once held). */
interface BirdDrag {
  pointer: number;
  touch: boolean;
  sx: number;
  sy: number;
  x: number;
  y: number;
  live: boolean;
  over: string | null;
}

/** How long a touch holds the bird before it lifts (a tap still clicks). */
const BIRD_HOLD_MS = 380;

interface Drag {
  id: string;
  from: number;
  to: number;
  /** How far the pointer has moved along the line. */
  delta: number;
  start: number;
  pointer: number;
  moved: boolean;
}

export function FlywayCanvas(p: FlywayCanvasProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const vertical = useNarrow(rootRef, p.vertical);
  const uid = useId().replace(/[^a-zA-Z0-9_-]/g, "");
  const [drag, setDrag] = useState<Drag | null>(null);
  const justDragged = useRef(false);
  const pan = useRef<{ x: number; y: number; left: number; top: number; id: number } | null>(null);
  const boxRefs = useRef<Record<string, HTMLDivElement | null>>({});
  const stageRef = useRef<HTMLDivElement>(null);
  const size: BoxSizeName = p.size || "normal";
  const lod: BoxLod = p.lod || "mid";
  const small = !!p.compact || size === "compact";
  // The wide box's second line sizes it; the level of detail only shows it.
  const detail = (n: DiagramNode) => (size === "wide" ? boxDetail(n) || p.detailOf?.(n) || "" : "");
  const shownDetail = (n: DiagramNode) => (lod === "far" ? "" : lod === "near" ? boxDetail(n) || p.detailOf?.(n) || "" : detail(n));
  // A stop chip dragged over the line: the gap it would land in (null = the
  // end) and its kind's name. The host's `dragStop` lights the end before.
  const [stopOver, setStopOver] = useState<{ at: number | null; label: string } | null>(null);
  const stopLabel = stopOver?.label || p.dragStop || "";
  const stopAt = stopOver ? stopOver.at : null;
  const addLabel = p.dropLabel || (stopLabel && stopAt === null ? "Tack on " + stopLabel : null);
  // The right-click menu: on which box, where in the stage.
  const [menu, setMenu] = useState<{ id: string; x: number; y: number } | null>(null);
  // Round 15: "Rewind to here" asked on a flown checkpoint — the inline
  // confirm row, at the menu's spot; `error` is the server's sentence when
  // it refused (the session is working).
  const [rewind, setRewind] = useState<{ id: string; x: number; y: number; busy: boolean; error: string } | null>(null);

  // Cards (an open box): what each draws, and its size as last measured.
  const opened = new Map<string, { body: ReactNode; size: BoxSize }>();
  if (p.openBox)
    for (const n of p.diagram.nodes) {
      const o = p.openBox(n);
      if (o) opened.set(n.id, o);
    }
  const openKey = [...opened.keys()].join("|");
  const [measured, setMeasured] = useState<Record<string, BoxSize>>({});
  useEffect(() => {
    const ids = openKey ? openKey.split("|") : [];
    if (!ids.length || typeof ResizeObserver === "undefined") return;
    const read = () =>
      setMeasured((cur) => {
        let next: Record<string, BoxSize> | null = null;
        for (const id of ids) {
          const el = boxRefs.current[id];
          if (!el || !el.offsetWidth || !el.offsetHeight) continue;
          // Layout px: the Sky's zoom (a transform) doesn't change them.
          const m = { w: el.offsetWidth, h: el.offsetHeight };
          if (cur[id]?.w !== m.w || cur[id]?.h !== m.h) (next ??= { ...cur })[id] = m;
        }
        return next ?? cur;
      });
    const ro = new ResizeObserver(read);
    for (const id of ids) {
      const el = boxRefs.current[id];
      if (el) ro.observe(el);
    }
    read();
    return () => ro.disconnect();
  }, [openKey]);

  const sizeOf = (n: DiagramNode, b: BoxSize): BoxSize => {
    const o = opened.get(n.id);
    if (o) return measured[n.id] || o.size;
    if (n.kind === "add" && addLabel) return { w: Math.max(b.w, 60 + addLabel.length * 7), h: Math.max(b.h, 40) };
    const s = sizedBox(size, n, b, detail(n), !!p.roomy);
    return p.sizeOf ? p.sizeOf(n, s) : s;
  };
  const opts = { compact: small, vertical, sizeOf, roomy: !!p.roomy };
  const base = useMemo(() => layoutDiagram(p.diagram, opts), [p.diagram, small, vertical, p.sizeOf, size, addLabel, measured, p.openBox, p.detailOf, p.roomy]); // eslint-disable-line react-hooks/exhaustive-deps
  const shown = useMemo(
    () => (drag && drag.moved && drag.to !== drag.from ? layoutDiagram(reorderDiagram(p.diagram, drag.from, drag.to), opts) : base),
    [drag, base, p.diagram] // eslint-disable-line react-hooks/exhaustive-deps
  );
  // The line's places, as the canvas counts them: its stops and group boxes.
  const stops = p.diagram.nodes.filter((n) => n.row === "main" && (n.kind === "stop" || n.kind === "group"));
  const axis = (b: { x: number; y: number; w: number; h: number }) => (vertical ? b.y + b.h / 2 : b.x + b.w / 2);

  // Keep the selected box in view (a ← / → move, a pick off the edge),
  // inside the canvas only — the dialog around it stays where it is.
  const bring = (id: string | null | undefined, center: boolean) => {
    const root = rootRef.current;
    const box = id ? base.boxes.find((b) => b.node.id === id) : null;
    if (!root || !box || root.clientWidth === 0) return;
    if (vertical) return;
    const left = box.x;
    const right = box.x + box.w + 12;
    if (center) root.scrollLeft = Math.max(0, left - (root.clientWidth - box.w) / 2);
    else if (left < root.scrollLeft) root.scrollLeft = left;
    else if (right > root.scrollLeft + root.clientWidth) root.scrollLeft = right - root.clientWidth;
  };
  useEffect(() => bring(p.selected, false), [p.selected, p.diagram.nodes.length]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => bring(p.reveal, true), [p.reveal, vertical]); // eslint-disable-line react-hooks/exhaustive-deps

  const canDrag = (n: DiagramNode) => !!p.onMove && n.kind === "stop" && n.editable !== false && n.index !== undefined;

  const onBoxDown = (e: PointerEvent<HTMLDivElement>, n: DiagramNode) => {
    if (!canDrag(n) || e.button !== 0) return;
    if ((e.target as HTMLElement).closest("button, input, textarea, a, summary, [data-no-drag]")) return;
    const at = vertical ? e.clientY : e.clientX;
    setDrag({ id: n.id, from: stops.indexOf(n), to: stops.indexOf(n), delta: 0, start: at, pointer: e.pointerId, moved: false });
    try {
      e.currentTarget.setPointerCapture(e.pointerId);
    } catch {
      /* a synthetic event has no capture */
    }
  };

  const onBoxMove = (e: PointerEvent<HTMLDivElement>) => {
    if (!drag || e.pointerId !== drag.pointer) return;
    const delta = (vertical ? e.clientY : e.clientX) - drag.start;
    const moved = drag.moved || Math.abs(delta) > 5;
    if (!moved) return;
    const self = base.boxes.find((b) => b.node.id === drag.id);
    if (!self) return;
    const others = base.boxes.filter((b) => (b.node.kind === "stop" || b.node.kind === "group") && b.node.row === "main" && b.node.id !== drag.id).map(axis);
    // Never before the flight's current stop.
    setDrag({ ...drag, delta, moved, to: Math.max(p.diagram.insertFrom, dropIndex(others, axis(self) + delta)) });
  };

  const onBoxUp = (e: PointerEvent<HTMLDivElement>) => {
    if (!drag || e.pointerId !== drag.pointer) return;
    const d = drag;
    setDrag(null);
    if (d.moved) {
      justDragged.current = true;
      setTimeout(() => (justDragged.current = false), 0);
      if (d.to !== d.from) p.onMove?.(d.from, d.to);
    }
  };

  /** ⇤ / ⇥, ← / →: one place along the line, when both stops can change. */
  const shift = (n: DiagramNode, dir: -1 | 1) => {
    const i = stops.indexOf(n);
    if (p.onMove && canShift(stops, i, dir, p.diagram.insertFrom)) p.onMove(i, i + dir);
  };

  const activate = (n: DiagramNode) => {
    if (justDragged.current) return;
    if (n.kind === "stop" || n.kind === "group") {
      // A stop that is flying or has flown opens on a click — its dialog
      // shows what it is doing / did (the owner: "for push or commit I want
      // to see what is going on when I click, like the agent"). A pending
      // one selects; ✎, Enter or a double-click edit it.
      if (!n.editable && p.onEdit && n.state !== "pending") p.onEdit(n);
      else p.onSelect?.(n);
    } else p.onOpen?.(n);
  };
  /** The stop's dialog — the inspector, on a host without one. */
  const edit = (n: DiagramNode) => (p.onEdit ? p.onEdit(n) : p.onSelect?.(n));

  const onBoxKey = (e: KeyboardEvent<HTMLDivElement>, n: DiagramNode) => {
    if (e.target !== e.currentTarget) return;
    const back = vertical ? "ArrowUp" : "ArrowLeft";
    const fwd = vertical ? "ArrowDown" : "ArrowRight";
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      if (p.onEdit && (n.kind === "stop" || n.kind === "group")) edit(n);
      else activate(n);
      return;
    }
    if (canDrag(n) && (e.key === back || e.key === fwd)) {
      e.preventDefault();
      shift(n, e.key === back ? -1 : 1);
      return;
    }
    if ((e.key === "Delete" || e.key === "Backspace") && p.onRemove && (n.kind === "stop" || n.kind === "group") && n.editable !== false) {
      e.preventDefault();
      p.onRemove(n);
    }
  };

  // Empty space pans the line (a mouse drag; touch scrolls natively).
  const onStageDown = (e: PointerEvent<HTMLDivElement>) => {
    if (e.pointerType !== "mouse" || e.button !== 0 || e.target !== e.currentTarget) return;
    const el = rootRef.current;
    if (!el) return;
    pan.current = { x: e.clientX, y: e.clientY, left: el.scrollLeft, top: el.scrollTop, id: e.pointerId };
    try {
      e.currentTarget.setPointerCapture(e.pointerId);
    } catch {
      /* ignore */
    }
  };
  const onStageMove = (e: PointerEvent<HTMLDivElement>) => {
    const pn = pan.current;
    const el = rootRef.current;
    if (!pn || !el || e.pointerId !== pn.id) return;
    el.scrollLeft = pn.left - (e.clientX - pn.x);
    el.scrollTop = pn.top - (e.clientY - pn.y);
  };
  const onStageUp = () => {
    pan.current = null;
  };

  // The + between two stops (hover), before every stop the line still lets
  // you insert at — not across a column of branches (lib/stopEditing). A
  // stop chip dragged over the line lights the one it would land in.
  const gaps = (p.onInsert || stopAt !== null) && !drag ? insertGaps(shown.boxes, p.diagram.insertFrom, vertical) : [];

  // The live flight's current stop (under way, perched, held or halted): it
  // wears a toolbar of its own — Halt now / Carry on, Halt here — on a line
  // whose host marks halt points (its menu offers Halt now on any line).
  const flying = (n: DiagramNode) => n.state === "current" || n.state === "perched" || n.state === "paused" || n.state === "halted";
  const isHere = (n: DiagramNode) =>
    !!p.diagram.flightId &&
    ((n.kind === "stop" && !!n.step && n.index === p.diagram.insertFrom - 1) || n.kind === "group") &&
    flying(n);
  const flown = (n: DiagramNode) => n.state === "done" || n.state === "skipped" || n.state === "failed";
  // On a live line any stop comes off: one still ahead, the one the flight
  // is at, one that has flown.
  const removable = (n: DiagramNode) => !!p.onRemove && (n.kind === "group" || (n.kind === "stop" && (!!n.editable || (!!p.diagram.flightId && !!n.step))));

  // The hover toolbar: on a stop that can change, when there is something
  // to change it with; on the current stop, to halt the flight (or remove
  // it); on a flown stop, to remove it; on a group box, its own.
  const tooled = (n: DiagramNode) => {
    if (n.kind === "group") return !!(p.onEdit || p.onRemove || p.onToggleHalt);
    if (n.kind !== "stop") return false;
    if (n.editable) return !!(p.onMove || p.onRemove || p.onDuplicate || p.onToggleHalt);
    if (isHere(n)) return !!p.onToggleHalt || removable(n);
    return flown(n) && removable(n);
  };
  const pad = 6;
  // Headroom over the top row for the bird to stand in, a toolbar, or an
  // open group's label.
  // Round 16: a Loop's return arc (and its pill) rides over the line — the
  // stage keeps room for its rise.
  const top = Math.max(p.bird || p.diagram.nodes.some((n) => tooled(n) || !!n.inGroup) ? 26 : pad, shown.rise ? Math.ceil(shown.rise) + pad : 0);

  // A point on the screen in the stage's pixels (the Sky zooms the line with
  // a transform; its width against its layout width undoes that).
  const stagePoint = (clientX: number, clientY: number) => {
    const el = stageRef.current;
    if (!el) return null;
    const r = el.getBoundingClientRect();
    const z = el.offsetWidth ? r.width / el.offsetWidth || 1 : 1;
    return { x: (clientX - r.left) / z, y: (clientY - r.top) / z };
  };

  // A stop chip (or a saved stop) from the shelf, over the line and dropped on it.
  const overStop = (e: DragEvent<HTMLDivElement>) => !!p.onDropStop && isStopDrag(e.dataTransfer?.types);
  const gapAt = (e: DragEvent<HTMLDivElement>) => {
    const pt = stagePoint(e.clientX, e.clientY);
    return pt ? dropGapFor(base.boxes, { x: pt.x - pad, y: pt.y - top }, p.diagram.insertFrom, vertical) : null;
  };
  const onDragOver = (e: DragEvent<HTMLDivElement>) => {
    if (!overStop(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
    const at = gapAt(e);
    const label = p.dragStop || "a stop";
    if (stopOver?.at !== at || stopOver?.label !== label) setStopOver({ at, label });
  };
  const onDragLeave = (e: DragEvent<HTMLDivElement>) => {
    if (stopOver && !rootRef.current?.contains(e.relatedTarget as Node | null)) setStopOver(null);
  };
  const onDrop = (e: DragEvent<HTMLDivElement>) => {
    if (!overStop(e)) return;
    e.preventDefault();
    // The sky's own drop (a flyway on a line) stays out of it.
    e.stopPropagation();
    const drop = stopDropOf(e.dataTransfer);
    const at = gapAt(e);
    setStopOver(null);
    if (drop) p.onDropStop!(drop, at);
  };

  // Right-click: the box's menu, at the pointer. Inside an open card (the
  // session's pane, its terminal) the card keeps its own.
  const onBoxMenu = (e: MouseEvent<HTMLElement>, n: DiagramNode) => {
    if (opened.has(n.id) && e.target !== e.currentTarget) return;
    e.preventDefault();
    e.stopPropagation();
    const pt = stagePoint(e.clientX, e.clientY);
    if (pt) setMenu({ id: n.id, x: pt.x, y: pt.y });
  };
  // A Loop has no box: its menu opens on its wire's pill (the edge's `via`).
  const menuNode = menu ? p.diagram.nodes.find((n) => n.id === menu.id) || p.diagram.edges.find((e) => e.via?.id === menu.id)?.via || null : null;
  const menuItems: SkyMenuItem[] = menuNode
    ? withRewind(
        menuItemsFor(menuNode, {
          can: { edit: !!(p.onEdit || p.onSelect), move: !!p.onMove, duplicate: !!p.onDuplicate, remove: !!p.onRemove, open: !!p.onOpen, halt: !!p.onToggleHalt, group: !!p.onToggleGroup },
          earlier: canShift(stops, stops.indexOf(menuNode), -1, p.diagram.insertFrom),
          later: canShift(stops, stops.indexOf(menuNode), 1, p.diagram.insertFrom),
          live: !!p.diagram.flightId && (!!menuNode.step || menuNode.kind === "group"),
          here: isHere(menuNode),
          jump: canJumpTo(jumpFlightOf(p.diagram), menuNode),
        }),
        menuNode,
        p.diagram.flightId
      )
    : [];
  const rewindNode = rewind ? p.diagram.nodes.find((n) => n.id === rewind.id) || null : null;
  const doRewind = async () => {
    if (!rewind || !rewindNode?.step) return;
    const flight = p.diagram.flightId;
    const sid = rewindNode.step.id;
    setRewind({ ...rewind, busy: true, error: "" });
    try {
      await api(flightPath(flight, "/steps/" + encodeURIComponent(sid) + "/op/rewind"), { json: {} });
      setRewind(null);
      toast("Rewound to “" + rewindNode.title + "” — the flight goes on from the stop after it");
      void refreshFlights();
      void refreshInstances();
    } catch (err) {
      // Refused (409 while the session works, …): the server's sentence,
      // inline under the question. The row stays so a second try is one press.
      setRewind((r) => (r ? { ...r, busy: false, error: errMsg(err) } : r));
    }
  };
  const pick = (n: DiagramNode, key: SkyMenuKey) => {
    const flight = p.diagram.flightId;
    const step = n.step?.id || "";
    if (key === "rewind") {
      if (menu) setRewind({ id: n.id, x: menu.x, y: menu.y, busy: false, error: "" });
    }
    else if (key === "edit") edit(n);
    else if (key === "expand") {
      const g = n.group?.id || n.inGroup?.id;
      if (g) p.onToggleGroup?.(g);
    }
    else if (key === "earlier" || key === "later") shift(n, key === "earlier" ? -1 : 1);
    else if (key === "duplicate") p.onDuplicate?.(n);
    else if (key === "remove") p.onRemove?.(n);
    else if (key === "done") void stepDone(flight, step);
    else if (key === "skip") void stepSkip(flight, step);
    else if (key === "retry") void stepRetry(flight, step);
    else if (key === "halt") p.onToggleHalt?.(n);
    else if (key === "pause" || key === "resume") void controlFlight(flight, key);
    else if (key === "jump") {
      const to = jumpStepOf(n);
      if (to) void jumpFlight(flight, to.id, n.title);
    }
    else if (key === "tackon" || key === "open") p.onOpen?.(n);
  };
  const closeMenu = (refocus: boolean) => {
    const id = menu?.id;
    setMenu(null);
    if (refocus && id) boxRefs.current[id]?.focus();
  };

  // --- The bird in hand -------------------------------------------------------
  const [birdDrag, setBirdDrag] = useState<BirdDrag | null>(null);
  const birdRef = useRef<BirdDrag | null>(null);
  birdRef.current = birdDrag;
  const birdHold = useRef<ReturnType<typeof setTimeout> | null>(null);
  const birdDragged = useRef(false);
  // Dropped on a stop: it stands there until the flight says where it is.
  const [landed, setLanded] = useState<{ on: string; from: string } | null>(null);
  // Let go elsewhere: a short flight home.
  const [snapping, setSnapping] = useState(false);
  useEffect(() => () => void (birdHold.current && clearTimeout(birdHold.current)), []);
  useEffect(() => {
    if (landed && p.bird?.nodeId !== landed.from) setLanded(null);
  }, [p.bird?.nodeId]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!landed) return;
    const t = setTimeout(() => setLanded(null), 6000);
    return () => clearTimeout(t);
  }, [landed]);
  useEffect(() => {
    if (!snapping) return;
    const t = setTimeout(() => setSnapping(false), 260);
    return () => clearTimeout(t);
  }, [snapping]);

  const perchNode = p.bird ? p.diagram.nodes.find((n) => n.id === p.bird!.nodeId) : undefined;
  const flightAt = jumpFlightOf(p.diagram);
  // The bird is in hand on a live flight's stop (not the session box, a rung).
  const onFlight = !!flightAt && !!perchNode && (perchNode.kind === "stop" || perchNode.kind === "group") && !!jumpStepOf(perchNode);
  const birdAct = p.bird ? birdAction(p.bird.tone, onFlight) : null;
  const targets = useMemo(() => new Set(onFlight ? jumpTargets(p.diagram, flightAt).map((n) => n.id) : []), [onFlight, p.diagram, flightAt?.at]); // eslint-disable-line react-hooks/exhaustive-deps
  const lit = birdDrag?.live ? targets : null;

  const pressBird = () => {
    if (!birdAct || !flightAt) return;
    if (birdAct.key === "retry") {
      if (flightAt.at) void stepRetry(flightAt.id, flightAt.at);
    } else void controlFlight(flightAt.id, birdAct.key);
  };
  // The stop under a point of the stage, among those the bird can land on.
  const targetAt = (x: number, y: number): string | null => {
    const lx = x - pad;
    const ly = y - top;
    const slack = 6;
    const hit = shown.boxes.find((b) => targets.has(b.node.id) && lx >= b.x - slack && lx <= b.x + b.w + slack && ly >= b.y - slack && ly <= b.y + b.h + slack);
    return hit ? hit.node.id : null;
  };
  const endBird = (d: BirdDrag | null, drop: boolean) => {
    if (birdHold.current) clearTimeout(birdHold.current);
    birdHold.current = null;
    setBirdDrag(null);
    if (!d?.live) return;
    birdDragged.current = true;
    setTimeout(() => (birdDragged.current = false), 0);
    const to = drop && d.over ? p.diagram.nodes.find((n) => n.id === d.over) : undefined;
    const step = to ? jumpStepOf(to) : null;
    if (!to || !step || !flightAt || !p.bird) {
      setSnapping(true);
      return;
    }
    setLanded({ on: to.id, from: p.bird.nodeId });
    void jumpFlight(flightAt.id, step.id, to.title).then((ok) => {
      if (!ok) {
        setLanded(null);
        setSnapping(true);
      }
    });
  };
  const onBirdDown = (e: PointerEvent<HTMLSpanElement>) => {
    // Never the line's bar drag, a box's, the stage's pan or the sky's marquee.
    e.stopPropagation();
    if (e.button !== 0 || !targets.size) return;
    const pt = stagePoint(e.clientX, e.clientY);
    if (!pt) return;
    const touch = e.pointerType === "touch";
    const d: BirdDrag = { pointer: e.pointerId, touch, sx: e.clientX, sy: e.clientY, x: pt.x, y: pt.y, live: false, over: null };
    setBirdDrag(d);
    try {
      e.currentTarget.setPointerCapture(e.pointerId);
    } catch {
      /* a synthetic event has no capture */
    }
    if (touch)
      birdHold.current = setTimeout(() => {
        birdHold.current = null;
        const cur = birdRef.current;
        if (cur && cur.pointer === d.pointer) setBirdDrag({ ...cur, live: true });
      }, BIRD_HOLD_MS);
  };
  const onBirdMove = (e: PointerEvent<HTMLSpanElement>) => {
    const d = birdRef.current;
    if (!d || e.pointerId !== d.pointer) return;
    e.stopPropagation();
    const far = Math.hypot(e.clientX - d.sx, e.clientY - d.sy) > (d.touch ? 10 : 4);
    if (!d.live) {
      // A touch that moves before the hold is a scroll or a pan: not the bird's.
      if (d.touch) {
        if (far) endBird(d, false);
        return;
      }
      if (!far) return;
    }
    const pt = stagePoint(e.clientX, e.clientY);
    if (pt) setBirdDrag({ ...d, live: true, x: pt.x, y: pt.y, over: targetAt(pt.x, pt.y) });
  };
  const onBirdUp = (e: PointerEvent<HTMLSpanElement>) => {
    const d = birdRef.current;
    if (!d || e.pointerId !== d.pointer) return;
    e.stopPropagation();
    endBird(d, e.type === "pointerup");
  };
  const birdHand = birdAct
    ? {
        title: birdAct.title + (targets.size ? " · Drag the bird to another stop to jump there" : ""),
        className: birdDrag?.live ? "fl-bird-dragging" : snapping ? "fl-bird-snap" : undefined,
        onClick: (e: MouseEvent<HTMLSpanElement>) => {
          e.stopPropagation();
          if (!birdDragged.current) pressBird();
        },
        onKeyDown: (e: KeyboardEvent<HTMLSpanElement>) => {
          if (e.key === "Escape" && birdRef.current) {
            e.preventDefault();
            endBird(birdRef.current, false);
            return;
          }
          if (e.key !== "Enter" && e.key !== " ") return;
          e.preventDefault();
          e.stopPropagation();
          pressBird();
        },
        onPointerDown: onBirdDown,
        onPointerMove: onBirdMove,
        onPointerUp: onBirdUp,
        onPointerCancel: onBirdUp,
        // A long-press is the drag, not the system's menu.
        onContextMenu: (e: MouseEvent<HTMLSpanElement>) => {
          if (birdRef.current?.touch) e.preventDefault();
        },
      }
    : undefined;

  const perch = p.bird ? shown.boxes.find((b) => b.node.id === (landed?.on || p.bird!.nodeId)) : undefined;
  // The branches' own birds, on a line that draws a bird at all.
  const branchBirds =
    p.bird !== undefined
      ? birdFormation(birdPerches(p.diagram))
          .filter((b) => b.child)
          .map((b) => ({ perch: b, box: shown.boxes.find((x) => x.node.id === b.nodeId) }))
          .filter((b): b is { perch: typeof b.perch; box: NonNullable<typeof b.box> } => !!b.box)
      : [];
  // Round 15 formation: with the leader's bird on the line, the workers'
  // birds fly in a V behind it (lib/flightDiagram `formationSlot`) instead of
  // sitting on their branch boxes — while the leader is still (dragged, or
  // landed and waiting for the flight), they stay on their boxes.
  const leaderAt = perch && !birdDrag?.live ? { x: perch.x + pad + perch.w / 2, y: perch.y + top } : null;
  return (
    <div
      ref={rootRef}
      className={
        "fl-canvas" +
        (p.compact || vertical ? " fl-canvas-compact" : "") +
        (size !== "normal" ? " fl-size-" + size : "") +
        (vertical ? " fl-canvas-vertical" : "") +
        " fl-lod-" + lod +
        (drag?.moved ? " fl-dragging" : "") +
        (lit ? " fl-bird-carried" : "") +
        (stopOver ? " fl-stop-over" : "") +
        (p.className ? " " + p.className : "")
      }
      role="list"
      aria-label={p.label + (p.onMove ? " — drag a stop, or ← → on a selected one, to reorder it" : "")}
      data-tour={p.dataTour}
      onDragOver={p.onDropStop ? onDragOver : undefined}
      onDragLeave={p.onDropStop ? onDragLeave : undefined}
      onDrop={p.onDropStop ? onDrop : undefined}
    >
      <div
        ref={stageRef}
        className="fl-stage"
        style={{ width: shown.width + pad * 2, height: shown.height + top + pad }}
        onPointerDown={onStageDown}
        onPointerMove={onStageMove}
        onPointerUp={onStageUp}
        onPointerCancel={onStageUp}
      >
        <svg className="fl-wires" width={shown.width + pad * 2} height={shown.height + top + pad} aria-hidden="true" focusable="false">
          <defs>
            <marker id={uid + "-arrow"} viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M 0 0 L 10 5 L 0 10 z" className="fl-arrowhead" />
            </marker>
          </defs>
          <g transform={`translate(${pad} ${top})`}>
            {shown.wires.map((w) => (
              <path
                key={w.from + ">" + w.to + (w.kind === "loop" ? ">loop" : "")}
                className={"fl-wire fl-wire-" + w.kind + (w.done ? " fl-wire-done" : "") + (w.kind === "loop" ? " fl-edge-loop fl-loop-" + (w.loop?.state || "pending") : "")}
                d={w.d}
                // Arrowheads only where the line arrives: a fan-out has none.
                markerEnd={w.kind === "add" || w.kind === "fan" ? undefined : `url(#${uid}-arrow)`}
              />
            ))}
          </g>
        </svg>
        {shown.wires.map((w) => {
          // A Loop is its return wire: the pill in the middle of its run under
          // the line says how many times ("↻ ×3", then "2 / 3" while it goes
          // round) and stands for the Loop itself — a click opens it, the
          // right-click menu and Delete work on it as on a box. Screen-sized
          // at any zoom of the Sky, like the join pill.
          if (w.kind !== "loop" || !w.apex || !w.loop) return null;
          const m = w.loop;
          const n = w.via;
          const text = loopPill(m) || loopIdlePill(m, n);
          const title = (m.state === "pending" && m.runs === 0 ? "Loop: " + (n?.sub || text) : loopPillTitle(m)) + (n ? " — click to change it" : "");
          return (
            <span
              key={"loop:" + (n?.id || w.from)}
              className={"fl-loop-pill fl-loop-" + m.state + (n ? " fl-st-" + n.state : "") + (n && p.selected === n.id ? " selected" : "")}
              style={{ transform: `translate(${w.apex.x + pad}px, ${w.apex.y + top}px) translate(-50%, -50%) scale(calc(1 / var(--sky-z, 1)))` }}
              data-node={n?.id}
              title={title}
              aria-label={title}
              role={n ? "button" : undefined}
              tabIndex={n ? 0 : undefined}
              onClick={n ? () => edit(n) : undefined}
              onContextMenu={n ? (e) => onBoxMenu(e, n) : undefined}
              onKeyDown={
                n
                  ? (e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        edit(n);
                      } else if ((e.key === "Delete" || e.key === "Backspace") && removable(n)) {
                        e.preventDefault();
                        p.onRemove!(n);
                      }
                    }
                  : undefined
              }
            >
              {text}
            </span>
          );
        })}
        {groupFrames(shown.boxes).map((f) => (
          // An open group: its sub-steps inside a thin outline, its name on
          // the outline with ▾ to fold it back into one box.
          <div
            key={"frame:" + f.id}
            className="fl-group-frame"
            style={{ transform: `translate(${f.x + pad - 8}px, ${f.y + top - 10}px)`, width: f.w + 16, height: f.h + 18 }}
            aria-hidden={p.onToggleGroup ? undefined : true}
          >
            <span className="fl-group-label">
              {p.onToggleGroup && (
                <button
                  type="button"
                  className="fl-icon-btn fl-group-chev"
                  aria-label={"Fold “" + f.name + "” into one box"}
                  aria-expanded={true}
                  title="Fold its sub-steps into one box"
                  onClick={() => p.onToggleGroup!(f.id)}
                >
                  ▾
                </button>
              )}
              {f.name}
            </span>
          </div>
        ))}
        {shown.boxes.map((b) => {
          const n = b.node;
          const lifted = drag?.moved && drag.id === n.id;
          const own = lifted ? base.boxes.find((x) => x.node.id === n.id) : null;
          const x = (own ? own.x + (vertical ? 0 : drag!.delta) : b.x) + pad;
          const y = (own ? own.y + (vertical ? drag!.delta : 0) : b.y) + top;
          // Stops already flown, folded into one pill (lib/flightDiagram's
          // "flown" node): a click opens them; no toolbar, menu or drag.
          if ((n.kind as string) === "flown")
            return (
              <div
                key={n.id}
                ref={(el) => {
                  boxRefs.current[n.id] = el;
                }}
                role="listitem"
                tabIndex={0}
                className="fl-box fl-box-flown"
                data-node={n.id}
                aria-label={n.title + " — show them"}
                title={n.title + " — show them"}
                style={{ transform: `translate(${x}px, ${y}px)`, width: b.w, minHeight: b.h }}
                onClick={() => p.onOpen?.(n)}
                onKeyDown={(e) => {
                  if (e.key !== "Enter" && e.key !== " ") return;
                  e.preventDefault();
                  p.onOpen?.(n);
                }}
              >
                <span className="fl-box-glyph fl-glyph-done" aria-hidden="true">
                  ✓
                </span>
                <span className="fl-box-title">{n.title}</span>
              </div>
            );
          const glyph = STATE_GLYPH[n.state];
          const open = opened.get(n.id);
          const drop = n.kind === "add" && !!addLabel;
          const more = shownDetail(n);
          const far = lod === "far";
          const near = lod === "near";
          const tools = !open && tooled(n);
          const here = isHere(n);
          const i = tools ? stops.indexOf(n) : -1;
          const mark = haltMark(n);
          const hold = here && (!n.editable || n.kind === "group") ? flightHold(n) : null;
          const haltable = !!p.onToggleHalt && canHalt(n, here);
          const waiting = n.join && n.join.waiting > 0 ? n.join : null;
          const cls =
            "fl-box fl-box-" + n.kind +
            (open ? " fl-box-open" : "") +
            (drop ? " fl-drop" : "") +
            " fl-st-" + n.state +
            (n.perch ? " fl-perch" : "") +
            (p.selected === n.id ? " selected" : "") +
            (p.highlight?.has(n.id) ? " fl-described" : "") +
            (p.highlighted?.has(n.id) ? " fl-box-selected" : "") +
            (lifted ? " fl-lifted" : "") +
            (tools ? " fl-has-tools" : "") +
            (mark ? " fl-halt-point" : "") +
            (n.inGroup ? " fl-in-group" : "") +
            (canDrag(n) ? " fl-draggable" : "") +
            (lit?.has(n.id) ? " fl-jump-target" : "") +
            (lit && birdDrag?.over === n.id ? " fl-jump-over" : "");
          const said =
            n.kind === "add"
              ? drop
                ? addLabel!
                : n.title
              : n.title +
                (n.group ? ", a stop of " + n.group.total + (n.group.total === 1 ? " sub-step" : " sub-steps") + (n.group.current ? ", " + n.group.done + " of " + n.group.total + " done" : "") : "") +
                (n.kind === "stop" || n.kind === "rung" || n.kind === "group" ? ", " + (STATE_WORD[n.state] || "") : "") +
                (n.inGroup ? ", in “" + n.inGroup.name + "”" : "") +
                (mark ? ", halt point" : "") +
                (n.chip ? ", " + n.chip.label : "") +
                (waiting ? ", " + joinLabel(waiting) : "");
          return (
            <div
              key={n.id}
              ref={(el) => {
                boxRefs.current[n.id] = el;
              }}
              role="listitem"
              tabIndex={0}
              className={cls}
              data-node={n.id}
              // The Sky tour's hooks: the session box, and the first stop that
              // wears the hover toolbar (lib/tours/sky.ts).
              data-tour={n.kind === "session" ? "sky-session-box" : tools && i === stops.findIndex((s) => tooled(s)) ? "sky-stop" : undefined}
              aria-label={said}
              aria-current={p.selected === n.id ? "true" : undefined}
              title={open ? undefined : n.kind === "add" ? (drop ? addLabel! : n.title) : n.reason ? n.title + " — " + n.reason : n.sub ? n.title + " — " + n.sub : n.title}
              // A card sizes itself (and is measured); a box takes the layout's.
              style={open ? { transform: `translate(${x}px, ${y}px)` } : { transform: `translate(${x}px, ${y}px)`, width: b.w, minHeight: b.h }}
              onPointerDown={(e) => onBoxDown(e, n)}
              onPointerMove={onBoxMove}
              onPointerUp={onBoxUp}
              onPointerCancel={onBoxUp}
              // Inside a card, a click is the card's (the pane's tabs, its
              // terminal): only the box itself, or Enter on it, acts.
              onClick={(e) => {
                if (open && e.target !== e.currentTarget) return;
                if (e.shiftKey && p.onShiftSelect && (n.kind === "stop" || n.kind === "group") && !justDragged.current) {
                  p.onShiftSelect(n);
                  return;
                }
                activate(n);
              }}
              onDoubleClick={(e) => {
                if (!p.onEdit || open || (n.kind !== "stop" && n.kind !== "group")) return;
                if ((e.target as HTMLElement).closest("button, input, textarea, a, summary")) return;
                e.preventDefault();
                edit(n);
              }}
              onKeyDown={(e) => onBoxKey(e, n)}
              onContextMenu={(e) => onBoxMenu(e, n)}
            >
              {n.risk && n.risk.length > 0 && !open && (
                // Round 15: the risk stripe — one 3px band per tag down the
                // left edge; the tooltip names them (lib/stopRisk).
                <span className="fl-risk" title={riskTitle(n.risk)} role="img" aria-label={"Touches: " + riskTitle(n.risk)} data-risk={n.risk.join(" ")}>
                  {n.risk.map((t) => (
                    <i key={t} className={"fl-risk-" + t} />
                  ))}
                </span>
              )}
              {mark && !open && (
                <span className="fl-halt-mark" title={mark.title} aria-hidden="true">
                  {mark.glyph}
                </span>
              )}
              {tools && n.kind === "group" && (
                // A group's own: its dialog, ⇤ / ⇥ (not this round), its halt
                // point, Remove (the whole group).
                <div className="fl-tools" role="toolbar" aria-label={"Change “" + n.title + "”"} data-no-drag onClick={(e) => e.stopPropagation()}>
                  {hold && here && (
                    <button
                      type="button"
                      className={"fl-icon-btn fl-tool fl-tool-" + hold.key}
                      aria-label={hold.label + " — “" + n.title + "”"}
                      title={hold.title}
                      onClick={() => void controlFlight(p.diagram.flightId, hold.key as "pause" | "resume")}
                    >
                      {hold.key === "pause" ? "⏹" : "▶"}
                    </button>
                  )}
                  {(p.onEdit || p.onSelect) && (
                    <button type="button" className="fl-icon-btn fl-tool" aria-label={"Edit “" + n.title + "”"} title="Open this stop and its sub-steps (Enter)" onClick={() => edit(n)}>
                      ✎
                    </button>
                  )}
                  {p.onMove && (
                    <>
                      <button type="button" className="fl-icon-btn fl-tool" aria-label={"Move “" + n.title + "” earlier"} title="A stop with sub-steps doesn't move yet" disabled>
                        ⇤
                      </button>
                      <button type="button" className="fl-icon-btn fl-tool" aria-label={"Move “" + n.title + "” later"} title="A stop with sub-steps doesn't move yet" disabled>
                        ⇥
                      </button>
                    </>
                  )}
                  {haltable && <HaltTool n={n} onToggle={p.onToggleHalt!} />}
                  {p.onRemove && <RemoveTool n={n} onRemove={p.onRemove} />}
                </div>
              )}
              {tools && n.kind === "stop" && here && !n.editable && (
                <div className="fl-tools" role="toolbar" aria-label={"Halt or carry on at “" + n.title + "”"} data-no-drag onClick={(e) => e.stopPropagation()}>
                  {/* Halt now on the line of a host that marks halt points (its menu has it on any line). */}
                  {hold && p.onToggleHalt && (
                    <button
                      type="button"
                      className={"fl-icon-btn fl-tool fl-tool-" + hold.key}
                      aria-label={hold.label + " — “" + n.title + "”"}
                      title={hold.title}
                      onClick={() => void controlFlight(p.diagram.flightId, hold.key as "pause" | "resume")}
                    >
                      {hold.key === "pause" ? "⏹" : "▶"}
                    </button>
                  )}
                  {haltable && <HaltTool n={n} onToggle={p.onToggleHalt!} />}
                  {removable(n) && <RemoveTool n={n} here onRemove={p.onRemove!} />}
                </div>
              )}
              {tools && n.kind === "stop" && !here && !n.editable && (
                <div className="fl-tools" role="toolbar" aria-label={"Remove “" + n.title + "”"} data-no-drag onClick={(e) => e.stopPropagation()}>
                  <RemoveTool n={n} onRemove={p.onRemove!} />
                </div>
              )}
              {tools && n.kind === "stop" && n.editable && (
                // Above the box on hover or focus; a press here never starts
                // the box's drag (onBoxDown skips buttons) nor opens it.
                <div className="fl-tools" role="toolbar" aria-label={"Change “" + n.title + "”"} data-no-drag onClick={(e) => e.stopPropagation()}>
                  {(p.onEdit || p.onSelect) && (
                    <button type="button" className="fl-icon-btn fl-tool" aria-label={"Edit “" + n.title + "”"} title="Edit this stop (Enter)" onClick={() => edit(n)}>
                      ✎
                    </button>
                  )}
                  {p.onMove && (
                    <>
                      <button
                        type="button"
                        className="fl-icon-btn fl-tool"
                        aria-label={"Move “" + n.title + "” earlier"}
                        title={"Move this stop one place earlier (" + (vertical ? "↑" : "←") + ")"}
                        disabled={!canShift(stops, i, -1, p.diagram.insertFrom)}
                        onClick={() => shift(n, -1)}
                      >
                        ⇤
                      </button>
                      <button
                        type="button"
                        className="fl-icon-btn fl-tool"
                        aria-label={"Move “" + n.title + "” later"}
                        title={"Move this stop one place later (" + (vertical ? "↓" : "→") + ")"}
                        disabled={!canShift(stops, i, 1, p.diagram.insertFrom)}
                        onClick={() => shift(n, 1)}
                      >
                        ⇥
                      </button>
                    </>
                  )}
                  {p.onDuplicate && (
                    <button type="button" className="fl-icon-btn fl-tool" aria-label={"Duplicate “" + n.title + "”"} title="Duplicate: the same stop again, right after this one" onClick={() => p.onDuplicate!(n)}>
                      ⧉
                    </button>
                  )}
                  {haltable && <HaltTool n={n} onToggle={p.onToggleHalt!} />}
                  {p.onRemove && <RemoveTool n={n} onRemove={p.onRemove} />}
                </div>
              )}
              {waiting && !open && (
                <span className="fl-join-pill" title={"The line goes on once its workers are done: " + joinLabel(waiting)} aria-hidden="true">
                  {waiting.waiting + " of " + waiting.total}
                </span>
              )}
              {open ? (
                open.body
              ) : n.kind === "add" ? (
                <>
                  <span className="fl-add-plus" aria-hidden="true">
                    <StopIcon kind="add" size={small ? 16 : 17} />
                  </span>
                  {drop ? <span className="fl-add-label">{addLabel}</span> : !small && !far && <span className="fl-add-label">{n.title}</span>}
                </>
              ) : n.kind === "group" ? (
                // A stop with sub-steps: a stack (FlywayCanvas.css), its name,
                // "k of n" while it flies, the sub-step it is at (else what it
                // holds) and one dot per sub-step.
                <>
                  <div className="fl-box-head">
                    {p.onToggleGroup && n.group && (
                      <button
                        type="button"
                        className="fl-icon-btn fl-group-chev"
                        aria-label={"Show the sub-steps of “" + n.title + "”"}
                        aria-expanded={false}
                        title="Show its sub-steps on the line"
                        data-no-drag
                        onClick={(e) => {
                          e.stopPropagation();
                          p.onToggleGroup!(n.group!.id);
                        }}
                      >
                        ▸
                      </button>
                    )}
                    <span className="fl-box-icon">
                      <StopIcon kind={n.perch ? "perch" : n.icon} size={small ? 13 : 15} />
                    </span>
                    <span className="fl-box-title">{n.title}</span>
                    {n.group?.current && <span className="fl-group-count">{n.group.done + " of " + n.group.total}</span>}
                    {glyph && (
                      <span className={"fl-box-glyph fl-glyph-" + n.state} aria-hidden="true">
                        {glyph}
                      </span>
                    )}
                    {flying(n) && <span className="fl-box-pulse" aria-hidden="true" />}
                  </div>
                  {!far && (size !== "compact" || near) && n.sub && <div className="fl-box-sub">{n.sub}</div>}
                  {!far && n.reason && (size !== "compact" || near) && <div className="fl-box-reason">{n.reason}</div>}
                  {!far && n.group && (
                    <div className="fl-group-dots" aria-hidden="true">
                      {n.group.nodes.map((s) => (
                        <span key={s.id} className={"fl-gdot fl-st-" + s.state} title={s.title} />
                      ))}
                    </div>
                  )}
                </>
              ) : n.kind === "branch" && (near || (size !== "compact" && !far)) ? (
                <>
                  <div className="fl-box-head">
                    <span className={"fl-box-dot tone-" + (n.chip?.tone || "neutral")} aria-hidden="true" />
                    <span className="fl-box-title">{n.title}</span>
                    {glyph && (
                      <span className={"fl-box-glyph fl-glyph-" + n.state} aria-hidden="true">
                        {glyph}
                      </span>
                    )}
                  </div>
                  {n.chip && <span className={"fl-box-chip tone-" + n.chip.tone}>{n.chip.label}</span>}
                  {n.sub && <div className="fl-box-sub">{n.sub}</div>}
                </>
              ) : far || (size === "compact" && !near) ? (
                // A compact box, and every box from far away: the head alone
                // (a compact one keeps its parts).
                <>
                <div className="fl-box-head">
                  {n.chip ? (
                    <span className={"fl-box-dot tone-" + n.chip.tone} aria-hidden="true" />
                  ) : (
                    <span className="fl-box-icon">
                      <StopIcon kind={n.perch ? "perch" : n.icon} size={13} />
                    </span>
                  )}
                  <span className="fl-box-title">{n.title}</span>
                  {glyph && (
                    <span className={"fl-box-glyph fl-glyph-" + n.state} aria-hidden="true">
                      {glyph}
                    </span>
                  )}
                  {(n.state === "current" || n.state === "perched") && <span className="fl-box-pulse" aria-hidden="true" />}
                </div>
                {!far && p.renderInBox?.(n)}
                </>
              ) : (
                <>
                  <div className="fl-box-head">
                    <span className="fl-box-icon">
                      <StopIcon kind={n.perch ? "perch" : n.icon} size={small ? 13 : 15} />
                    </span>
                    <span className="fl-box-title">{n.title}</span>
                    {n.when && (
                      // Round 15: the stop runs only when its condition holds.
                      <span className="fl-box-if" title={"Only when " + n.when} aria-label={"Only when " + n.when}>
                        if
                      </span>
                    )}
                    {glyph && (
                      <span className={"fl-box-glyph fl-glyph-" + n.state} aria-hidden="true">
                        {glyph}
                      </span>
                    )}
                    {(n.state === "current" || n.state === "perched") && <span className="fl-box-pulse" aria-hidden="true" />}
                  </div>
                  {/* Round 15: what a flown stop did, in a line (mid and near). */}
                  {!far && n.receipt && <div className="fl-box-receipt">{n.receipt}</div>}
                  {n.chip && <span className={"fl-box-chip tone-" + n.chip.tone}>{n.chip.label}</span>}
                  {n.sub && <div className="fl-box-sub">{n.sub}</div>}
                  {more && <div className="fl-box-detail">{more}</div>}
                  {n.reason && <div className="fl-box-reason">{n.reason}</div>}
                  {n.dots && (
                    <div className="fl-box-dots" aria-label="Its line">
                      {n.dots}
                    </div>
                  )}
                  {p.renderInBox?.(n)}
                </>
              )}
            </div>
          );
        })}
        {gaps.map((g) => {
          // Screen-sized at any zoom of the Sky.
          const style = { transform: `translate(${g.x + pad}px, ${g.y + top}px) translate(-50%, -50%) scale(calc(1 / var(--sky-z, 1)))` };
          if (g.at === stopAt)
            return (
              <span key={"gap" + g.at} className="fl-gap-add fl-gap-lit" style={style} aria-hidden="true">
                + <span className="fl-gap-label">Tack on here</span>
              </span>
            );
          return p.onInsert ? (
            <button
              key={"gap" + g.at}
              type="button"
              className="fl-gap-add"
              style={style}
              aria-label={"Add a stop here (before stop " + (g.at + 1) + ")"}
              title="Add a stop here"
              onClick={() => p.onInsert?.(g.at)}
            >
              +
            </button>
          ) : null;
        })}
        {perch && (
          <BirdMarker
            tone={p.bird!.tone}
            x={birdDrag?.live ? birdDrag.x : perch.x + pad + perch.w / 2}
            y={birdDrag?.live ? birdDrag.y : perch.y + top}
            hand={birdHand}
            label={"The bird: " + (p.bird!.tone === "you" ? "perched on you at " : p.bird!.tone === "halted" ? "halted at " : p.bird!.tone === "paused" ? "paused at " : "flying ") + perch.node.title}
          />
        )}
        {branchBirds.map(({ perch: bp, box }) => {
          const n = box.node;
          const doing = bp.tone === "you" ? "waiting on you" : "working";
          const open = () => p.onOpen?.(n);
          const inV = leaderAt && bp.formation ? formationAt(leaderAt, bp.formation, 18, vertical) : null;
          return (
            <BirdMarker
              key={"bird:" + n.id}
              tone={bp.tone}
              size={18}
              x={inV ? inV.x : box.x + pad}
              y={inV ? inV.y : box.y + top + Math.min(box.h, 52) / 2}
              label={n.title + "’s bird: " + doing}
              className="fl-bird-branch"
              formation={!!inV}
              hand={
                p.onOpen
                  ? {
                      title: "Open " + n.title + " — " + doing,
                      onClick: (e) => {
                        e.stopPropagation();
                        open();
                      },
                      onKeyDown: (e) => {
                        if (e.key !== "Enter" && e.key !== " ") return;
                        e.preventDefault();
                        e.stopPropagation();
                        open();
                      },
                      // A branch's bird never drags, nor starts the line's drags.
                      onPointerDown: (e) => e.stopPropagation(),
                    }
                  : undefined
              }
            />
          );
        })}
        {menuNode && menuItems.length > 0 && (
          <StopMenu
            x={menu!.x}
            y={menu!.y}
            label={"“" + menuNode.title + "” — what you can do"}
            items={menuItems}
            onPick={(key) => {
              // Rewind reads the menu's spot for its confirm row: close after.
              pick(menuNode, key);
              closeMenu(false);
            }}
            onClose={closeMenu}
          />
        )}
        {rewind && rewindNode && (
          <div
            className="fl-rewind"
            style={{ left: rewind.x, top: rewind.y }}
            data-no-drag
            onPointerDown={(e) => e.stopPropagation()}
            onClick={(e) => e.stopPropagation()}
            onKeyDown={(e) => {
              if (e.key !== "Escape") return;
              e.stopPropagation();
              setRewind(null);
              boxRefs.current[rewind.id]?.focus();
            }}
          >
            <InlineConfirm
              title={"Rewind to “" + rewindNode.title + "”?"}
              body={
                "The worktree goes back to " +
                (rewindNode.receipt ? "where it was " + rewindNode.receipt : "this checkpoint") +
                " — work since then is lost unless it was committed somewhere else — and the flight flies on from the stop after it."
              }
              confirmLabel="Rewind"
              busy={rewind.busy}
              onConfirm={() => void doRewind()}
              onCancel={() => {
                setRewind(null);
                boxRefs.current[rewind.id]?.focus();
              }}
            />
            {rewind.error && (
              <p className="fl-rewind-error" role="alert">
                {rewind.error}
              </p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
