/** A grounded flight on the Sky (docs/flights.md "Grounded flights"): stops
 * on the board with no session yet. They do nothing until one is attached.
 * Its own line, laid out under `"flight:" + id`: a title bar (the bird, its
 * name — the flight's `plan_name`, renamed inline: a click on it or F2 →
 * `renameFlight` — and a ⋯), a dashed
 * head box where the session will sit ("No session yet — drop a Session chip
 * here, or ⋯ Fly on a session"), then its stops on the canvas, every one
 * editable (useGroundedLine).
 *
 * The ⋯: **Add a stop…** (the palette), **Fly on a session…** (the host's
 * line picker → `attachGrounded`), **Save to the shelf** (a line that came
 * from a saved stop updates it, else a new saved stop named after the line),
 * **Remove** (inline-confirmed, then `deleteFlight`). Delete or Backspace with
 * the line itself focused asks to remove it too. The host places it, drags
 * it by the bar (`onDragStart`) and gives it room from `onMeasure`, as it
 * does a session's line. `draggable` false (the sky's click mode): the bar
 * never starts a move. `registerDelete` hands the host this line's Delete
 * (docs/flights.md "As built: round 12 sky"): a stop or group box leaves
 * through the canvas's Remove, anything else (the head, the bar, the line)
 * asks to remove the line. `merging`: dragged over a session's line, which
 * takes its stops on release (docs/flights.md "As built: round 13 sky"). */

import { useEffect, useRef, useState, type JSX, type KeyboardEvent, type PointerEvent as ReactPointerEvent } from "react";
import type { Flight, FlightPlan, StepKindDef } from "../../lib/flights";
import type { DiagramNode } from "../../lib/flightDiagram";
import { deleteFlight, renameFlight, savePlan } from "../../lib/flightsApi";
import { isRemoveKey, planToSave, sourcePlanId } from "../../lib/groundedLines";
import { lodFor, nearLines, type SkyLineLayout } from "../../lib/skyLayout";
import { errMsg } from "../../lib/format";
import { toast } from "../../lib/toast";
import { InlineConfirm } from "../InlineConfirm";
import { OverflowMenu, type MenuItem } from "../OverflowMenu";
import { nodeById, removableBox } from "../../lib/skySelect";
import { FlywayCanvas } from "./FlywayCanvas";
import { useGroundedLine } from "./useGroundedLine";
import { Bird } from "./Bird";
import "./SkyLine.css";
import "./GroundedLine.css";

export interface GroundedLineProps {
  flight: Flight;
  kinds: readonly StepKindDef[];
  plans: readonly FlightPlan[];
  layout: SkyLineLayout;
  focused: boolean;
  onFocus(): void;
  zoom: number;
  onDragStart?(e: ReactPointerEvent): void;
  onMeasure?(w: number, h: number): void;
  /** ⋯ Fly on a session…: the host asks which line. */
  onFlyOn(): void;
  /** A Session chip is being dragged over this line. */
  sessionOver?: boolean;
  /** In the Sky's selection, as a whole line. */
  selected?: boolean;
  /** Its stop boxes in the Sky's selection (node ids). */
  highlighted?: ReadonlySet<string>;
  /** Shift+click on a box: the host's multi-selection (SkySelection). */
  onShiftSelect?(n: DiagramNode): void;
  /** False in the sky's click mode: the bar never starts a move. */
  draggable?: boolean;
  /** The host's handle on this line's Delete (SkyLine's `registerDelete`). */
  registerDelete?(fn: ((node: string) => boolean) | null): void;
  /** Dragged by its bar over a session's line: the drop merges it there. */
  merging?: boolean;
}

/** A grounded line's name as typed: trimmed, at most 120 characters (the
 * server's cap); "" = nothing to save. */
export const cleanFlightName = (s: string) => s.trim().slice(0, 120);

/** The name a grounded line shows. */
export const groundedName = (f: Pick<Flight, "plan_name">) => f.plan_name || "Stops";

export function GroundedLine(p: GroundedLineProps): JSX.Element {
  const { flight } = p;
  const line = useGroundedLine(flight, { kinds: p.kinds, plans: p.plans });
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  // The name being edited (null: shown), and where the bar's press began (a
  // click on the name renames; a drag that started there moves the line).
  const [naming, setNaming] = useState<string | null>(null);
  const pressAt = useRef<{ x: number; y: number } | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const measureRef = useRef(p.onMeasure);
  measureRef.current = p.onMeasure;
  const name = groundedName(flight);
  const lod = lodFor(p.zoom || 1);

  useEffect(() => {
    const el = rootRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    let last = "";
    const read = () => {
      const w = el.offsetWidth;
      const h = el.offsetHeight;
      const k = w + "x" + h;
      if (!w || !h || k === last) return;
      last = k;
      measureRef.current?.(w, h);
    };
    const ro = new ResizeObserver(read);
    ro.observe(el);
    read();
    return () => ro.disconnect();
  }, []);

  const save = async () => {
    const plan = planToSave(name, flight.steps, p.plans as FlightPlan[]);
    try {
      const saved = await savePlan(plan);
      toast((plan.id ? "Updated “" : "Saved “") + saved.name + "” on the shelf");
    } catch (err) {
      toast("Saving to the shelf failed: " + errMsg(err), { error: true });
    }
  };

  const remove = async () => {
    setBusy(true);
    try {
      if (await deleteFlight(flight.id)) toast("Removed “" + name + "”");
    } finally {
      setBusy(false);
      setConfirming(false);
    }
  };

  const updates = !!sourcePlanId(flight.steps, p.plans);
  const items: MenuItem[] = [
    { key: "add", label: "Add a stop…", title: "One stop, or a saved stop, at the end of the line", onSelect: () => line.togglePalette(true) },
    { key: "fly", label: "Fly on a session…", title: "Pick a session: these stops take off on it", onSelect: p.onFlyOn },
    {
      key: "save",
      label: "Save to the shelf",
      title: updates ? "Update the saved stop these came from" : "Keep these stops on the shelf as a saved stop",
      className: "skl-mi-group",
      onSelect: () => void save(),
    },
    { key: "remove", label: "Remove", danger: true, className: "skl-mi-group", title: "Take these stops off the board", onSelect: () => setConfirming(true) },
  ];

  // The edit ends once: Enter or Esc hands the focus back to the line, and
  // the input's blur that follows must not save it again.
  const named = useRef(false);
  const startRename = () => {
    named.current = false;
    setNaming(name);
  };
  /** Enter or blur: the new name, if it changed; Esc: as it was. */
  const endRename = (save: boolean) => {
    if (named.current) return;
    named.current = true;
    const next = cleanFlightName(naming ?? "");
    setNaming(null);
    rootRef.current?.focus({ preventScroll: true });
    if (save && next && next !== flight.plan_name) void renameFlight(flight.id, next);
  };
  const onNameClick = (e: React.MouseEvent) => {
    const at = pressAt.current;
    pressAt.current = null;
    if (at && Math.hypot(e.clientX - at.x, e.clientY - at.y) >= 4) return;
    startRename();
  };

  const onBarDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    pressAt.current = { x: e.clientX, y: e.clientY };
    if ((e.target as Element).closest("button, summary, a, input, [data-no-drag]")) return;
    rootRef.current?.focus({ preventScroll: true });
    if (p.draggable === false) return;
    p.onDragStart?.(e);
  };
  // Delete from the sky: a stop or group box through the canvas's Remove;
  // anything else on the line asks to remove the line.
  const deleteBox = (node: string): boolean => {
    const n = node ? nodeById(line.diagram.nodes, node) : null;
    if (n && (n.kind === "stop" || n.kind === "group")) {
      if (!removableBox(n) || !line.canvas.onRemove) return false;
      line.canvas.onRemove(n);
      return true;
    }
    setConfirming(true);
    return true;
  };
  const deleteRef = useRef(deleteBox);
  deleteRef.current = deleteBox;
  const register = p.registerDelete;
  useEffect(() => {
    if (!register) return;
    register((node) => deleteRef.current(node));
    return () => register(null);
  }, [register]);
  // Delete / Backspace on the line itself (not a stop on it, not a field).
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.target !== e.currentTarget) return;
    if (e.key === "F2") {
      e.preventDefault();
      e.stopPropagation();
      startRename();
      return;
    }
    if (!isRemoveKey(e.key)) return;
    e.preventDefault();
    e.stopPropagation();
    setConfirming(true);
  };

  return (
    <div
      ref={rootRef}
      className={
        "skl skl-size-" + (p.layout.size || "normal") +
        " skl-grounded" +
        (p.focused ? " is-focused" : "") +
        (p.selected ? " is-selected" : "") +
        (p.sessionOver ? " is-session-over" : "") +
        (p.merging ? " is-merging" : "") +
        (line.editing ? " is-editing" : "")
      }
      data-flight={flight.id}
      data-lod={lod}
      style={{ "--sky-z": p.zoom || 1, "--sky-lines": nearLines(p.zoom || 1) } as React.CSSProperties}
      role="group"
      tabIndex={0}
      data-sky-box
      aria-label={name + " — stops with no session yet"}
      onKeyDown={onKey}
      onPointerDownCapture={() => {
        if (!p.focused) p.onFocus();
      }}
    >
      <div className="skl-bar" title={p.draggable === false ? "Click to focus these stops (drag mode moves them: V)" : "Drag to move these stops — onto a session's line to give them to it"} onPointerDown={onBarDown}>
        <Bird size={16} />
        {naming === null ? (
          <span className="skl-name skl-name-edit" title="Click to rename (F2)" onClick={onNameClick}>
            {name}
          </span>
        ) : (
          <input
            className="fl-input skl-name-input"
            aria-label={"Rename “" + name + "”"}
            value={naming}
            maxLength={120}
            autoFocus
            onChange={(e) => setNaming(e.target.value)}
            onFocus={(e) => e.target.select()}
            onBlur={() => endRename(true)}
            onKeyDown={(e) => {
              e.stopPropagation();
              if (e.key === "Enter") {
                e.preventDefault();
                endRename(true);
              } else if (e.key === "Escape") {
                e.preventDefault();
                endRename(false);
              }
            }}
          />
        )}
        <span className="fl-box-chip skl-chip tone-idle" title="These stops do nothing until a session flies them">
          No session
        </span>
        <span className="skl-menu" data-no-drag>
          <OverflowMenu label={"Actions for " + name} items={items} />
        </span>
      </div>
      {confirming && (
        <div className="skl-grounded-confirm" data-no-drag>
          <InlineConfirm
            title={"Remove “" + name + "”?"}
            body={"Its " + flight.steps.length + (flight.steps.length === 1 ? " stop leaves" : " stops leave") + " the board. A saved stop on the shelf stays."}
            confirmLabel="Remove"
            busy={busy}
            onConfirm={() => void remove()}
            onCancel={() => {
              setConfirming(false);
              rootRef.current?.focus({ preventScroll: true });
            }}
          />
        </div>
      )}
      <div className="skl-grounded-body">
        <div className="skl-grounded-head" data-tour="sky-grounded" data-no-drag title="Drag the Session chip from the shelf here, or ⋯ → Fly on a session">
          <strong>No session yet</strong>
          <span>drop a Session chip here, or ⋯ Fly on a session</span>
        </div>
        <FlywayCanvas
          diagram={line.diagram}
          label={"The stops of " + name}
          vertical={false}
          className="sky-canvas skl-canvas"
          size={p.layout.size || "normal"}
          lod={lod}
          {...line.canvas}
          highlighted={p.highlighted}
          onShiftSelect={p.onShiftSelect}
          roomy
        />
      </div>
      {line.editing && (
        <div className="skl-panels sky-own-scroll" data-no-drag>
          {line.palette}
          {line.inspector}
        </div>
      )}
    </div>
  );
}
