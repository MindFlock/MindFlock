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
 *    and the trailing + ("Tack on a stop");
 *  - a line in the Sky — the same diagram, with the bird (`bird`) standing
 *    on the box the flight is at, its boxes at the line's size (`size`:
 *    compact = the title alone, wide = the full title, the summary and a
 *    second line of detail), and a box opened into a card (`openBox` — the
 *    session's real pane), measured, so its connectors follow it.
 *
 * Empty space pans the line sideways (scroll, or drag it). Positions and
 * connectors come from `layoutDiagram` (fixed box sizes), so the markup needs
 * no measuring and a server render draws the same picture — an open card
 * alone is measured (it starts at the size it asks for). */

import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent, type ReactNode, type RefObject } from "react";
import {
  layoutDiagram,
  reorderDiagram,
  type BoxSize,
  dropIndex,
  type BirdPerch,
  type Diagram,
  type DiagramNode,
  type LayoutOpts,
} from "../../lib/flightDiagram";
import { BirdMarker, StopIcon } from "./Bird";
import "./FlywayCanvas.css";

/** Below this width the line runs top to bottom. */
const VERTICAL_BELOW_PX = 520;

/** How much of each box a line shows (the Sky's ◧): normal is the editor's. */
export type BoxSizeName = "compact" | "normal" | "wide";

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
 * a two-line title, the summary on two lines and the detail line. */
export function sizedBox(size: BoxSizeName, n: DiagramNode, base: BoxSize, detail = ""): BoxSize {
  if (n.kind === "add" || size === "normal") return base;
  if (size === "compact") return { w: 110, h: 62 };
  return { w: 260, h: base.h + 18 + (n.sub ? 15 : 0) + (detail ? 32 : 0) };
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
  /** A stop box picked (click, Enter): open it in the inspector. */
  onSelect?(n: DiagramNode): void;
  /** A session / parent / child box, or the trailing +, pressed. */
  onOpen?(n: DiagramNode): void;
  /** Reordering: drag, and ← / → on the selected stop (indexes among stops). */
  onMove?(from: number, to: number): void;
  /** Delete on a selected stop. */
  onRemove?(n: DiagramNode): void;
  /** The + between two boxes: insert a stop before stop `at`. */
  onInsert?(at: number): void;
  /** Extra content inside a box (a perch's checklist and Done / Skip, ⋯). */
  renderInBox?(n: DiagramNode): ReactNode;
  /** A box drawn bigger than the rest. */
  sizeOf?: LayoutOpts["sizeOf"];
  /** Scroll the line so this box (the flight's current stop) is in view. */
  reveal?: string | null;
  /** The bird: standing on this box (lib/flightDiagram `birdPerch`). */
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
  dataTour?: string;
  className?: string;
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
  const size: BoxSizeName = p.size || "normal";
  const small = !!p.compact || size === "compact";
  const detail = (n: DiagramNode) => (size === "wide" ? boxDetail(n) || p.detailOf?.(n) || "" : "");

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
    if (n.kind === "add" && p.dropLabel) return { w: Math.max(b.w, 60 + p.dropLabel.length * 7), h: Math.max(b.h, 40) };
    const s = sizedBox(size, n, b, detail(n));
    return p.sizeOf ? p.sizeOf(n, s) : s;
  };
  const opts = { compact: small, vertical, sizeOf };
  const base = useMemo(() => layoutDiagram(p.diagram, opts), [p.diagram, small, vertical, p.sizeOf, size, p.dropLabel, measured, p.openBox, p.detailOf]); // eslint-disable-line react-hooks/exhaustive-deps
  const shown = useMemo(
    () => (drag && drag.moved && drag.to !== drag.from ? layoutDiagram(reorderDiagram(p.diagram, drag.from, drag.to), opts) : base),
    [drag, base, p.diagram] // eslint-disable-line react-hooks/exhaustive-deps
  );
  const stops = p.diagram.nodes.filter((n) => n.row === "main" && n.kind === "stop");
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
    const others = base.boxes.filter((b) => b.node.kind === "stop" && b.node.row === "main" && b.node.id !== drag.id).map(axis);
    setDrag({ ...drag, delta, moved, to: dropIndex(others, axis(self) + delta) });
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

  const activate = (n: DiagramNode) => {
    if (justDragged.current) return;
    if (n.kind === "stop") p.onSelect?.(n);
    else p.onOpen?.(n);
  };

  const onBoxKey = (e: KeyboardEvent<HTMLDivElement>, n: DiagramNode) => {
    if (e.target !== e.currentTarget) return;
    const back = vertical ? "ArrowUp" : "ArrowLeft";
    const fwd = vertical ? "ArrowDown" : "ArrowRight";
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      activate(n);
      return;
    }
    if (canDrag(n) && (e.key === back || e.key === fwd)) {
      e.preventDefault();
      const i = stops.indexOf(n);
      const to = i + (e.key === back ? -1 : 1);
      if (to >= 0 && to < stops.length && stops[to].editable !== false) p.onMove?.(i, to);
      return;
    }
    if ((e.key === "Delete" || e.key === "Backspace") && p.onRemove && n.kind === "stop" && n.editable !== false) {
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
  // you insert at.
  const gaps: Array<{ at: number; x: number; y: number }> = [];
  if (p.onInsert && !drag) {
    const mainBoxes = shown.boxes.filter((b) => b.node.row === "main");
    for (let j = 1; j < mainBoxes.length; j++) {
      const b = mainBoxes[j];
      const a = mainBoxes[j - 1];
      if (b.node.kind !== "stop" || b.node.index === undefined || b.node.index < p.diagram.insertFrom || a.node.kind === "add") continue;
      gaps.push(
        vertical
          ? { at: b.node.index, x: b.x + b.w / 2, y: (a.y + a.h + b.y) / 2 }
          : { at: b.node.index, x: (a.x + a.w + b.x) / 2, y: b.y + Math.min(b.h, a.h) / 2 }
      );
    }
  }

  const pad = 6;
  // Headroom over the top row for the bird to stand in.
  const top = p.bird ? 26 : pad;
  const perch = p.bird ? shown.boxes.find((b) => b.node.id === p.bird!.nodeId) : undefined;
  return (
    <div
      ref={rootRef}
      className={
        "fl-canvas" +
        (p.compact || vertical ? " fl-canvas-compact" : "") +
        (size !== "normal" ? " fl-size-" + size : "") +
        (vertical ? " fl-canvas-vertical" : "") +
        (drag?.moved ? " fl-dragging" : "") +
        (p.className ? " " + p.className : "")
      }
      role="list"
      aria-label={p.label + (p.onMove ? " — drag a stop, or ← → on a selected one, to reorder it" : "")}
      data-tour={p.dataTour}
    >
      <div
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
                key={w.from + ">" + w.to}
                className={"fl-wire fl-wire-" + w.kind}
                d={w.d}
                markerEnd={w.kind === "add" ? undefined : `url(#${uid}-arrow)`}
              />
            ))}
          </g>
        </svg>
        {shown.boxes.map((b) => {
          const n = b.node;
          const lifted = drag?.moved && drag.id === n.id;
          const own = lifted ? base.boxes.find((x) => x.node.id === n.id) : null;
          const x = (own ? own.x + (vertical ? 0 : drag!.delta) : b.x) + pad;
          const y = (own ? own.y + (vertical ? drag!.delta : 0) : b.y) + top;
          const glyph = STATE_GLYPH[n.state];
          const open = opened.get(n.id);
          const drop = n.kind === "add" && !!p.dropLabel;
          const more = detail(n);
          const cls =
            "fl-box fl-box-" + n.kind +
            (open ? " fl-box-open" : "") +
            (drop ? " fl-drop" : "") +
            " fl-st-" + n.state +
            (n.perch ? " fl-perch" : "") +
            (p.selected === n.id ? " selected" : "") +
            (p.highlight?.has(n.id) ? " fl-described" : "") +
            (lifted ? " fl-lifted" : "") +
            (canDrag(n) ? " fl-draggable" : "");
          const said =
            n.kind === "add"
              ? drop
                ? p.dropLabel!
                : n.title
              : n.title + (n.kind === "stop" || n.kind === "rung" ? ", " + (STATE_WORD[n.state] || "") : "") + (n.chip ? ", " + n.chip.label : "");
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
              aria-label={said}
              aria-current={p.selected === n.id ? "true" : undefined}
              title={open ? undefined : n.kind === "add" ? (drop ? p.dropLabel! : n.title) : n.reason ? n.title + " — " + n.reason : n.sub ? n.title + " — " + n.sub : n.title}
              // A card sizes itself (and is measured); a box takes the layout's.
              style={open ? { transform: `translate(${x}px, ${y}px)` } : { transform: `translate(${x}px, ${y}px)`, width: b.w, minHeight: b.h }}
              onPointerDown={(e) => onBoxDown(e, n)}
              onPointerMove={onBoxMove}
              onPointerUp={onBoxUp}
              onPointerCancel={onBoxUp}
              // Inside a card, a click is the card's (the pane's tabs, its
              // terminal): only the box itself, or Enter on it, acts.
              onClick={(e) => (open && e.target !== e.currentTarget ? undefined : activate(n))}
              onKeyDown={(e) => onBoxKey(e, n)}
            >
              {open ? (
                open.body
              ) : n.kind === "add" ? (
                <>
                  <span className="fl-add-plus" aria-hidden="true">
                    <StopIcon kind="add" size={small ? 16 : 17} />
                  </span>
                  {drop ? <span className="fl-add-label">{p.dropLabel}</span> : !small && <span className="fl-add-label">{n.title}</span>}
                </>
              ) : size === "compact" ? (
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
                {p.renderInBox?.(n)}
                </>
              ) : (
                <>
                  <div className="fl-box-head">
                    <span className="fl-box-icon">
                      <StopIcon kind={n.perch ? "perch" : n.icon} size={small ? 13 : 15} />
                    </span>
                    <span className="fl-box-title">{n.title}</span>
                    {glyph && (
                      <span className={"fl-box-glyph fl-glyph-" + n.state} aria-hidden="true">
                        {glyph}
                      </span>
                    )}
                    {(n.state === "current" || n.state === "perched") && <span className="fl-box-pulse" aria-hidden="true" />}
                  </div>
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
        {gaps.map((g) => (
          <button
            key={"gap" + g.at}
            type="button"
            className="fl-gap-add"
            style={{ transform: `translate(${g.x + pad}px, ${g.y + top}px) translate(-50%, -50%)` }}
            aria-label={"Add a stop here (before stop " + (g.at + 1) + ")"}
            title="Add a stop here"
            onClick={() => p.onInsert?.(g.at)}
          >
            +
          </button>
        ))}
        {perch && (
          <BirdMarker
            tone={p.bird!.tone}
            x={perch.x + pad + perch.w / 2}
            y={perch.y + top}
            label={"The bird: " + (p.bird!.tone === "you" ? "perched on you at " : p.bird!.tone === "halted" ? "halted at " : p.bird!.tone === "paused" ? "paused at " : "flying ") + perch.node.title}
          />
        )}
      </div>
    </div>
  );
}
