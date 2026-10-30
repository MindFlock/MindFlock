/** **All blocks** (docs/flights.md "As built: round 12 shelf"): the shelf's
 * catalog, opened by its All blocks… chip. Two sections — **Blocks** (every
 * kind the server lists: its icon, name, who does it, its one-line summary)
 * and **Multi-step** (every saved stop: its name, a built-in badge, its
 * sub-steps) — under a filter field that has the focus.
 *
 * A row is dragged onto the board like its chip on the shelf (the same MIMEs,
 * lib/stopCatalog), so a drop on a line or on empty sky works with the
 * catalog open: the Modal's backdrop would take that drop, so while a row is
 * being dragged the whole Modal lets the pointer through (`.is-dragging`) and
 * fades; a drop that landed closes it. **Add** (or Enter on the row ↑ / ↓
 * picked) → `onAdd({kind} | {planId})`. Esc or a press on the backdrop
 * closes. */

import { useEffect, useState, type DragEvent, type JSX, type KeyboardEvent } from "react";
import type { FlightPlan, StepKindDef } from "../../lib/flights";
import { planDots } from "../../lib/groundedLines";
import { catalogKinds, catalogRows, filterKinds, filterPlans, planPath, savedDragData, stopDragData, type CatalogDrop } from "../../lib/stopCatalog";
import { Modal } from "../Modal";
import { StopIcon } from "./Bird";
import { kindName } from "./StopParts";
import "./StopCatalog.css";

export interface StopCatalogProps {
  /** The kinds (`useFlightKinds`), as the server lists them. */
  kinds: readonly StepKindDef[];
  /** The saved stops (`useFlightPlans`), built-ins first. */
  plans: readonly FlightPlan[];
  /** Add / Enter: the host tacks it onto the focused line. Without it the
   * rows only drag. */
  onAdd?(drop: CatalogDrop): void;
  /** Is this block on the shelf? With `onShelfToggle`, every row gets a ★ /
   * ☆ that puts it on the shelf or takes it off (the shelf's own pick). */
  shelf?(drop: CatalogDrop): boolean;
  onShelfToggle?(drop: CatalogDrop): void;
  onClose(): void;
  /** Default true (into document.body); false renders in place (the tests). */
  portal?: boolean;
}

const rowId = (i: number) => "sky-catalog-row-" + i;

export function StopCatalog({ kinds, plans, onAdd, onClose, portal = true, shelf, onShelfToggle }: StopCatalogProps): JSX.Element {
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const [dragging, setDragging] = useState(false);
  const shownKinds = filterKinds(catalogKinds(kinds), query);
  const shownPlans = filterPlans(plans, query);
  const rows = catalogRows(shownKinds, shownPlans);
  const at = Math.min(active, rows.length - 1);

  useEffect(() => {
    if (at >= 0) document.getElementById(rowId(at))?.scrollIntoView?.({ block: "nearest" });
  }, [at]);

  const add = (d: CatalogDrop) => onAdd?.(d);

  const onFilterKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!rows.length) return;
      setActive((Math.max(at, 0) + (e.key === "ArrowDown" ? 1 : -1) + rows.length) % rows.length);
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (at >= 0) add(rows[at]);
    }
  };

  // The drag's data is set at once; the Modal turns see-through a tick later
  // (changing it inside dragstart can cancel the drag).
  const onDragStart = (e: DragEvent, data: Array<[string, string]>) => {
    for (const [type, value] of data) e.dataTransfer.setData(type, value);
    e.dataTransfer.effectAllowed = "copy";
    setTimeout(() => setDragging(true), 0);
  };
  const onDragEnd = (e: DragEvent) => {
    setDragging(false);
    if (e.dataTransfer && e.dataTransfer.dropEffect !== "none") onClose();
  };

  const addButton = (d: CatalogDrop, name: string) => {
    const on = shelf ? shelf(d) : false;
    return (
      <>
        {onShelfToggle && (
          <button
            type="button"
            className={"fl-icon-btn sky-catalog-shelf" + (on ? " on" : "")}
            tabIndex={-1}
            aria-pressed={on}
            aria-label={(on ? "Take “" : "Put “") + name + (on ? "” off the shelf" : "” on the shelf")}
            title={on ? "On the shelf — click to take it off" : "Put it on the shelf at the foot of the sky"}
            onClick={() => onShelfToggle(d)}
          >
            {on ? "★" : "☆"}
          </button>
        )}
        {onAdd && (
          <button type="button" className="test-btn sky-catalog-add" tabIndex={-1} aria-label={"Add “" + name + "”"} onClick={() => add(d)}>
            Add
          </button>
        )}
      </>
    );
  };

  const how = onAdd ? "Drag onto a line or the sky, or Add it to the focused line" : "Drag onto a line or the sky";
  let i = -1;
  return (
    <Modal
      id="sky-catalog"
      className={"sky-catalog-modal" + (dragging ? " is-dragging" : "")}
      onClose={onClose}
      portal={portal}
      role="dialog"
      aria-modal="true"
      aria-labelledby="sky-catalog-title"
    >
      <div className="sky-catalog" onKeyDown={(e) => e.key === "Escape" || e.stopPropagation()}>
        <div className="sky-catalog-head">
          <h2 id="sky-catalog-title">All blocks</h2>
          <button type="button" className="fl-icon-btn sky-catalog-close" aria-label="Close" title="Close (Esc)" onClick={onClose}>
            ×
          </button>
        </div>
        <input
          className="fl-input sky-catalog-filter"
          type="text"
          placeholder="Filter blocks…"
          aria-label="Filter blocks"
          aria-controls="sky-catalog-list"
          aria-activedescendant={at >= 0 ? rowId(at) : undefined}
          autoComplete="off"
          spellCheck={false}
          autoFocus
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setActive(0);
          }}
          onKeyDown={onFilterKey}
        />
        <div className="sky-catalog-list" id="sky-catalog-list" role="listbox" aria-label="All blocks">
          {shownKinds.length > 0 && (
            <section className="sky-catalog-section" data-section="blocks" role="group" aria-label="Blocks">
              <h3 className="sky-catalog-label">Blocks</h3>
              {shownKinds.map((k) => {
                const n = ++i;
                const perch = k.actor === "you";
                const name = kindName(k);
                return (
                  <div
                    key={k.kind}
                    id={rowId(n)}
                    role="option"
                    aria-selected={n === at}
                    className={"sky-catalog-row" + (n === at ? " active" : "") + (perch ? " perch" : "")}
                    data-kind={k.kind}
                    draggable
                    title={how}
                    onDragStart={(e) => onDragStart(e, stopDragData(k))}
                    onDragEnd={onDragEnd}
                    onMouseEnter={() => setActive(n)}
                  >
                    <span className="sky-catalog-icon">
                      <StopIcon kind={perch ? "perch" : k.kind} size={15} />
                    </span>
                    <span className="sky-catalog-words">
                      <span className="sky-catalog-name">
                        {name}
                        <span className={"sky-catalog-badge actor-" + (perch ? "you" : "robot")}>{perch ? "you" : "robot"}</span>
                      </span>
                      {k.summary && <span className="sky-catalog-summary">{k.summary}</span>}
                    </span>
                    {addButton({ kind: k.kind }, name)}
                  </div>
                );
              })}
            </section>
          )}
          {shownPlans.length > 0 && (
            <section className="sky-catalog-section" data-section="multi" role="group" aria-label="Multi-step">
              <h3 className="sky-catalog-label">Multi-step</h3>
              {shownPlans.map((p) => {
                const n = ++i;
                const { dots, more } = planDots(p.steps, kinds);
                return (
                  <div
                    key={p.id}
                    id={rowId(n)}
                    role="option"
                    aria-selected={n === at}
                    className={"sky-catalog-row sky-catalog-plan" + (n === at ? " active" : "")}
                    data-plan={p.id}
                    draggable
                    title={how}
                    onDragStart={(e) => onDragStart(e, savedDragData(p))}
                    onDragEnd={onDragEnd}
                    onMouseEnter={() => setActive(n)}
                  >
                    <span className="sky-catalog-boxes" aria-hidden="true">
                      {dots.map((d, j) => (
                        <i key={j} className={d.perch ? "perch" : ""} />
                      ))}
                      {more > 0 && <span className="sky-catalog-more">+{more}</span>}
                    </span>
                    <span className="sky-catalog-words">
                      <span className="sky-catalog-name">
                        {p.name}
                        {p.builtin && <span className="sky-catalog-badge builtin">built-in</span>}
                      </span>
                      <span className="sky-catalog-summary">{planPath(p) || "No steps yet"}</span>
                    </span>
                    {addButton({ planId: p.id }, p.name)}
                  </div>
                );
              })}
            </section>
          )}
          {rows.length === 0 && <p className="sky-catalog-empty muted">{query.trim() ? "Nothing matches “" + query.trim() + "”." : "No blocks yet — this server may not know Flights."}</p>}
        </div>
        <p className="sky-catalog-hint muted">{onAdd ? "Drag a block onto a line or the sky · ↑ ↓ pick · Enter adds it to the focused line · Esc closes" : "Drag a block onto a line or the sky · Esc closes"}</p>
      </div>
    </Modal>
  );
}
