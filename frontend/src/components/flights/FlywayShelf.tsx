/** The flyway shelf: the saved flyways as a strip of chips along the bottom of
 * the Sky (built-ins first), each with its name and a tiny row of stop dots
 * (a perch's dot gold). Click a chip to edit it (`onEdit`), its Fly button to
 * fly it (`onFly` — the host asks on which line), or DRAG it onto a line: the
 * chip carries the flyway's id as `application/x-mindflock-flyway` and the host
 * owns the drop targets. **+ New flyway** opens a fresh card (`onNew`). The
 * shelf folds to its head; the host remembers that (`collapsed` /
 * `onCollapse`), else the shelf keeps it itself. Under the flyways, the
 * STOPS row (`StopShelf`): a chip per stop kind to drag onto a line, or click
 * to tack onto the focused one (`onTack`). */

import { useState, type DragEvent, type JSX } from "react";
import type { FlightPlan } from "../../lib/flights";
import { StopShelf } from "./StopShelf";
import "./FlywayShelf.css";

/** The drag type a flyway chip carries (its value: the flyway's id). */
export const FLYWAY_MIME = "application/x-mindflock-flyway";

/** Dots past this many fold into "+N". */
const MAX_DOTS = 10;

/** The shelf's order: built-ins first, then yours, each as listed. */
export function shelfOrder(plans: readonly FlightPlan[]): FlightPlan[] {
  return [...plans.filter((p) => p.builtin), ...plans.filter((p) => !p.builtin)];
}

/** What a chip's drag carries: the flyway's id under FLYWAY_MIME, and its name
 * as plain text for anything else it is dropped on. */
export function flywayDragData(plan: Pick<FlightPlan, "id" | "name">): Array<[string, string]> {
  return [
    [FLYWAY_MIME, plan.id],
    ["text/plain", plan.name],
  ];
}

function onChipDrag(e: DragEvent, plan: FlightPlan) {
  for (const [type, value] of flywayDragData(plan)) e.dataTransfer.setData(type, value);
  e.dataTransfer.effectAllowed = "copy";
}

function StopDots({ plan }: { plan: FlightPlan }) {
  const shown = plan.steps.slice(0, MAX_DOTS);
  const more = plan.steps.length - shown.length;
  return (
    <span className="sky-shelf-dots" aria-hidden="true">
      {shown.map((s) => (
        <i key={s.id} className={"sky-shelf-dot" + (s.kind === "you" ? " perch" : "")} />
      ))}
      {more > 0 && <span className="sky-shelf-more">+{more}</span>}
    </span>
  );
}

export function FlywayShelf({
  plans,
  onNew,
  onFly,
  onEdit,
  collapsed,
  onCollapse,
  onTack,
}: {
  plans: FlightPlan[];
  onNew(): void;
  onFly(plan: FlightPlan, title?: string): void;
  onEdit(plan: FlightPlan): void;
  /** Folded to its head (the host remembers it); uncontrolled when absent. */
  collapsed?: boolean;
  onCollapse?(collapsed: boolean): void;
  /** A stop chip clicked: tack that kind onto the focused line. Absent, the
   * stop chips only drag. */
  onTack?(kind: string): void;
}): JSX.Element {
  const [ownFold, setOwnFold] = useState(false);
  const folded = collapsed ?? ownFold;
  const fold = (v: boolean) => {
    setOwnFold(v);
    onCollapse?.(v);
  };
  const ordered = shelfOrder(plans);
  return (
    <section className={"sky-shelf" + (folded ? " folded" : "")} aria-label="Flyways" data-tour="sky-shelf">
      <button type="button" className="sky-shelf-head" aria-expanded={!folded} title={folded ? "Show the flyways" : "Fold the flyways away"} onClick={() => fold(!folded)}>
        <span className="sky-shelf-caret" aria-hidden="true">
          {folded ? "▸" : "▾"}
        </span>
        Flyways <span className="sky-shelf-count">{ordered.length}</span>
      </button>
      {!folded && (
        <div className="sky-shelf-chips" role="list">
          {ordered.map((p) => (
            <div
              key={p.id}
              role="listitem"
              className={"sky-shelf-chip" + (p.builtin ? " builtin" : "")}
              data-plan={p.id}
              draggable={true}
              title={"Drag onto a line to fly “" + p.name + "” on it"}
              onDragStart={(e) => onChipDrag(e, p)}
            >
              <button type="button" className="sky-shelf-open" aria-label={"Edit the flyway “" + p.name + "”"} onClick={() => onEdit(p)}>
                <span className="sky-shelf-name">{p.name}</span>
                <StopDots plan={p} />
              </button>
              <button type="button" className="sky-shelf-fly" aria-label={"Fly “" + p.name + "” on a line"} title="Fly it on a line" onClick={() => onFly(p)}>
                Fly
              </button>
            </div>
          ))}
          <button type="button" className="sky-shelf-chip sky-shelf-new" onClick={onNew}>
            + New flyway
          </button>
        </div>
      )}
      {!folded && <StopShelf onTack={onTack} />}
    </section>
  );
}
