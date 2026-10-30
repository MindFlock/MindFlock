/** **All blocks** (docs/flights.md "As built: round 13 palette"): the
 * catalog, opened by the shelf's (or the floating palette's) All blocks…. A
 * window-sized card (≈ 92vw × 86vh): a filter with the focus, then two
 * grids of cards — **Blocks** (every kind the server lists: its icon, name,
 * who does it, its one-line summary) and **Multi-step** (every saved stop:
 * its name, a built-in badge, a box per step, "a → b → c").
 *
 * A card is dragged onto the board like its chip on the shelf (the same
 * MIMEs, lib/stopCatalog), so a drop on a line or on empty sky works with the
 * catalog open: the Modal's backdrop would take that drop, so while a card is
 * being dragged the whole Modal lets the pointer through (`.is-dragging`) and
 * fades; a drop that landed closes it. **Add**, Enter on the card the arrows
 * picked, or a double-click → `onAdd({kind} | {planId})` (the host places it
 * on the board). A saved stop's ⋯: Rename (a field on the card), On the
 * shelf, Reset to default (a built-in) or Delete (yours, confirmed on the
 * card). Esc or a press on the backdrop closes. */

import { useEffect, useRef, useState, type DragEvent, type JSX, type KeyboardEvent, type MouseEvent } from "react";
import type { FlightPlan, StepKindDef } from "../../lib/flights";
import { deletePlan, resetPlan, savePlan } from "../../lib/flightsApi";
import { checkRename, planDots } from "../../lib/groundedLines";
import { cardMenu, catalogCards, gridMove, savedDragData, stopDragData, type CatalogCard, type CatalogDrop } from "../../lib/stopCatalog";
import { errMsg } from "../../lib/format";
import { toast } from "../../lib/toast";
import { InlineConfirm } from "../InlineConfirm";
import { Modal } from "../Modal";
import { OverflowMenu } from "../OverflowMenu";
import { StopIcon } from "./Bird";
import "./StopCatalog.css";

/** A saved stop's name as a field (Rename, on a shelf chip or a catalog card):
 * Enter saves (`checkRename`, then `savePlan` with the new name), Esc cancels,
 * leaving it saves when the name is good and keeps the old one when not. */
export function PlanNameField({ plan, plans, onDone }: { plan: FlightPlan; plans: readonly FlightPlan[]; onDone(): void }): JSX.Element {
  const [name, setName] = useState(plan.name);
  const [err, setErr] = useState("");
  const done = useRef(false);

  const commit = async (leaving: boolean) => {
    if (done.current) return;
    const c = checkRename(name, plans, plan.id);
    if (!c.ok) {
      if (!leaving) return setErr(c.error);
      done.current = true;
      toast(c.error + " — kept “" + plan.name + "”");
      return onDone();
    }
    done.current = true;
    onDone();
    if (c.name === plan.name) return;
    try {
      await savePlan({ ...plan, name: c.name });
      toast("Renamed to “" + c.name + "”");
    } catch (e) {
      toast("Renaming failed: " + errMsg(e), { error: true });
    }
  };

  return (
    <>
      <input
        className="sky-saved-rename fl-input"
        aria-label={"Rename “" + plan.name + "”"}
        aria-invalid={!!err}
        title={err || "Enter saves, Esc cancels"}
        value={name}
        autoFocus
        maxLength={140}
        onChange={(e) => {
          setName(e.target.value);
          setErr("");
        }}
        onKeyDown={(e) => {
          e.stopPropagation();
          if (e.key === "Enter") {
            e.preventDefault();
            void commit(false);
          } else if (e.key === "Escape") {
            e.preventDefault();
            done.current = true;
            onDone();
          }
        }}
        onBlur={() => void commit(true)}
        onClick={(e) => e.stopPropagation()}
        onDoubleClick={(e) => e.stopPropagation()}
      />
      {err && (
        <span className="sky-saved-err" role="alert">
          {err}
        </span>
      )}
    </>
  );
}

export interface StopCatalogProps {
  /** The kinds (`useFlightKinds`), as the server lists them. */
  kinds: readonly StepKindDef[];
  /** The saved stops (`useFlightPlans`), built-ins first. */
  plans: readonly FlightPlan[];
  /** Add / Enter / a double-click. Without it the cards only drag. */
  onAdd?(drop: CatalogDrop): void;
  /** Add puts the block ON THE BOARD (the host's `onPlace`) rather than on
   * the focused line: the words say so. */
  place?: boolean;
  /** Is this block on the shelf? With `onShelfToggle`, every card gets a ★ /
   * ☆ that puts it on the shelf or takes it off (the shelf's own pick). */
  shelf?(drop: CatalogDrop): boolean;
  onShelfToggle?(drop: CatalogDrop): void;
  onClose(): void;
  /** Default true (into document.body); false renders in place (the tests). */
  portal?: boolean;
}

const rowId = (i: number) => "sky-catalog-row-" + i;

/** How many columns a grid of cards is laid out in right now (1 when it
 * can't tell — the tests, or nothing shown). */
function gridCols(list: HTMLElement | null): number {
  const g = list?.querySelector<HTMLElement>(".sky-catalog-grid");
  if (!g || typeof getComputedStyle === "undefined") return 1;
  return getComputedStyle(g).gridTemplateColumns.split(" ").filter(Boolean).length || 1;
}

export function StopCatalog({ kinds, plans, onAdd, place = false, onClose, portal = true, shelf, onShelfToggle }: StopCatalogProps): JSX.Element {
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const [dragging, setDragging] = useState(false);
  const [renaming, setRenaming] = useState("");
  const [deleting, setDeleting] = useState("");
  const [busy, setBusy] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);
  const cards = catalogCards(kinds, plans, query);
  const rows = cards.all;
  const at = Math.min(active, rows.length - 1);

  useEffect(() => {
    if (at >= 0) document.getElementById(rowId(at))?.scrollIntoView?.({ block: "nearest" });
  }, [at]);

  const add = (d: CatalogDrop) => onAdd?.(d);

  const onFilterKey = (e: KeyboardEvent<HTMLInputElement>) => {
    const el = e.currentTarget;
    // ← / → move the pick only from the filter's edge (else they move the caret).
    const edge = e.key === "ArrowLeft" ? el.selectionStart === 0 && el.selectionEnd === 0 : e.key === "ArrowRight" ? el.selectionStart === el.value.length : true;
    if ((e.key === "ArrowDown" || e.key === "ArrowUp" || e.key === "ArrowLeft" || e.key === "ArrowRight") && edge) {
      e.preventDefault();
      if (!rows.length) return;
      setActive(gridMove(at, e.key, [cards.blocks.length, cards.multi.length], gridCols(listRef.current)));
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (at >= 0) add(rows[at].drop);
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

  const reset = async (plan: FlightPlan) => {
    try {
      await resetPlan(plan.id);
      toast("“" + plan.name + "” is back to its default");
    } catch (e) {
      toast("Reset failed: " + errMsg(e), { error: true });
    }
  };
  const remove = async (plan: FlightPlan) => {
    setBusy(true);
    try {
      await deletePlan(plan.id);
      toast("Deleted “" + plan.name + "”");
      setDeleting("");
    } catch (e) {
      toast("Deleting failed: " + errMsg(e), { error: true });
    } finally {
      setBusy(false);
    }
  };

  const star = (d: CatalogDrop, name: string) => {
    if (!onShelfToggle) return null;
    const on = shelf ? shelf(d) : false;
    return (
      <button
        type="button"
        className={"fl-icon-btn sky-catalog-shelf" + (on ? " on" : "")}
        tabIndex={-1}
        aria-pressed={on}
        aria-label={(on ? "Take “" : "Put “") + name + (on ? "” off the shelf" : "” on the shelf")}
        title={on ? "On the shelf — click to take it off" : "Put it on the shelf (and in the floating palette)"}
        onClick={() => onShelfToggle(d)}
      >
        {on ? "★" : "☆"}
      </button>
    );
  };

  const how = onAdd ? (place ? "Drag onto a line or the sky, or Add it to the board" : "Drag onto a line or the sky, or Add it to the focused line") : "Drag onto a line or the sky";

  const card = (c: CatalogCard<StepKindDef, FlightPlan>, n: number) => {
    const plan = c.section === "multi" ? c.plan : null;
    const def = c.section === "blocks" ? c.def : null;
    const naming = !!plan && renaming === plan.id;
    const asking = !!plan && deleting === plan.id;
    const on = n === at;
    const dblAdd = (e: MouseEvent) => {
      if (!onAdd || naming || asking || (e.target as Element).closest("button, input, summary, .vf-menu")) return;
      add(c.drop);
    };
    return (
      <div
        key={c.key}
        id={rowId(n)}
        role="option"
        aria-selected={on}
        className={"sky-catalog-card" + (plan ? " sky-catalog-plan" : "") + (c.section === "blocks" && c.perch ? " perch" : "") + (on ? " active" : "")}
        data-kind={def?.kind}
        data-plan={plan?.id}
        draggable={!naming && !asking}
        title={how}
        onDragStart={(e) => onDragStart(e, plan ? savedDragData(plan) : stopDragData(def as StepKindDef))}
        onDragEnd={onDragEnd}
        onMouseEnter={() => setActive(n)}
        onDoubleClick={dblAdd}
      >
        <div className="sky-catalog-card-head">
          {def && (
            <span className="sky-catalog-icon">
              <StopIcon kind={def.actor === "you" ? "perch" : def.kind} size={18} />
            </span>
          )}
          <span className="sky-catalog-name">
            {plan && naming ? <PlanNameField plan={plan} plans={plans} onDone={() => setRenaming("")} /> : <span className="sky-catalog-title">{c.name}</span>}
            {c.section === "blocks" ? (
              <span className={"sky-catalog-badge actor-" + (c.perch ? "you" : "robot")}>{c.perch ? "you" : "robot"}</span>
            ) : (
              c.builtin && <span className="sky-catalog-badge builtin">built-in</span>
            )}
          </span>
          {star(c.drop, c.name)}
          {plan && (
            <OverflowMenu
              label={"“" + plan.name + "” — rename, delete, shelf"}
              boundaryId="sky-catalog-list"
              items={cardMenu(plan, shelf ? shelf(c.drop) : false)
                .filter((it) => it.key !== "shelf" || onShelfToggle)
                .map((it) => ({
                  key: it.key,
                  label: it.label,
                  danger: it.danger,
                  onSelect: () => {
                    if (it.key === "rename") setRenaming(plan.id);
                    else if (it.key === "shelf") onShelfToggle?.(c.drop);
                    else if (it.key === "reset") void reset(plan);
                    else setDeleting(plan.id);
                  },
                }))}
            />
          )}
        </div>
        {plan ? (
          <>
            <StepBoxes plan={plan} kinds={kinds} />
            <span className="sky-catalog-summary">{c.section === "multi" && c.path ? c.path : "No steps yet"}</span>
          </>
        ) : (
          def?.summary && <span className="sky-catalog-summary">{def.summary}</span>
        )}
        {asking && plan ? (
          <InlineConfirm
            title={"Delete “" + plan.name + "”?"}
            body="It leaves the catalog and the shelf. Stops already on the board stay."
            confirmLabel="Delete"
            busy={busy}
            onConfirm={() => void remove(plan)}
            onCancel={() => setDeleting("")}
          />
        ) : (
          onAdd && (
            <span className="sky-catalog-card-foot">
              <button type="button" className="test-btn sky-catalog-add" tabIndex={-1} aria-label={"Add “" + c.name + "”"} onClick={() => add(c.drop)}>
                Add
              </button>
            </span>
          )
        )}
      </div>
    );
  };

  let i = -1;
  return (
    <Modal
      id="sky-catalog"
      className={"sky-catalog-modal" + (dragging ? " is-dragging" : "")}
      onClose={onClose}
      onEscape={() => {
        // An open delete question takes Esc first (a rename field spends its
        // own Esc: it prevents the default, which the Modal leaves alone).
        if (!deleting) return false;
        setDeleting("");
        return true;
      }}
      portal={portal}
      role="dialog"
      aria-modal="true"
      aria-labelledby="sky-catalog-title"
    >
      <div className="sky-catalog" onKeyDown={(e) => e.key === "Escape" || e.stopPropagation()}>
        <div className="sky-catalog-head">
          <h2 id="sky-catalog-title">All blocks</h2>
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
          <button type="button" className="fl-icon-btn sky-catalog-close" aria-label="Close" title="Close (Esc)" onClick={onClose}>
            ×
          </button>
        </div>
        <div className="sky-catalog-list sky-own-scroll" id="sky-catalog-list" role="listbox" aria-label="All blocks" ref={listRef}>
          {cards.blocks.length > 0 && (
            <section className="sky-catalog-section" data-section="blocks" role="group" aria-label="Blocks">
              <h3 className="sky-catalog-label">Blocks</h3>
              <div className="sky-catalog-grid">{cards.blocks.map((c) => card(c, ++i))}</div>
            </section>
          )}
          {cards.multi.length > 0 && (
            <section className="sky-catalog-section" data-section="multi" role="group" aria-label="Multi-step">
              <h3 className="sky-catalog-label">Multi-step</h3>
              <div className="sky-catalog-grid">{cards.multi.map((c) => card(c, ++i))}</div>
            </section>
          )}
          {rows.length === 0 && <p className="sky-catalog-empty muted">{query.trim() ? "Nothing matches “" + query.trim() + "”." : "No blocks yet — this server may not know Flights."}</p>}
        </div>
        <p className="sky-catalog-hint muted">
          {onAdd
            ? "Drag a block onto a line or the sky · the arrows pick · Enter or a double-click " + (place ? "puts it on the board" : "adds it to the focused line") + " · Esc closes"
            : "Drag a block onto a line or the sky · Esc closes"}
        </p>
      </div>
    </Modal>
  );
}

/** A saved stop's sub-steps: a small box per step (a perch's gold, "+N" past
 * ten), joined like a line. */
function StepBoxes({ plan, kinds }: { plan: FlightPlan; kinds: readonly StepKindDef[] }) {
  const { dots, more } = planDots(plan.steps, kinds);
  return (
    <span className="sky-catalog-boxes" aria-hidden="true">
      {dots.map((d, j) => (
        <i key={j} className={d.perch ? "perch" : ""} />
      ))}
      {more > 0 && <span className="sky-catalog-more">+{more}</span>}
    </span>
  );
}
