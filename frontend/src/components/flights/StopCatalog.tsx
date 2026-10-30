/** **All blocks** (docs/flights.md "As built: round 14 palette"): the
 * catalog's content, `StopCatalogBody` — a filter, then the grids of cards —
 * **Blocks** (every kind the server lists: its icon, name, who does it, its
 * one-line summary; by family since round 15, "As built: round 15
 * palette-groups") and **Multi-step** (every saved stop: its name, a
 * built-in badge, a box per step, "a → b → c"). The floating palette
 * (SkyPalette) IS this body, compact; `StopCatalog` wraps it in a Modal for a
 * shelf whose host floats no palette.
 *
 * A card is dragged onto the board like its chip on the shelf (the same
 * MIMEs, lib/stopCatalog). A click or a double-click on a card places it once
 * (`clickOrDouble`), as do its **Add** and Enter on the card the arrows
 * picked → `onAdd({kind} | {planId})` (the host places it on the board). ★ /
 * ☆ puts it on the bottom shelf or takes it off. A saved stop's ⋯: Rename (a
 * field on the card), Takes off…, On the shelf, Reset to default (a
 * built-in) or Delete (yours, confirmed on the card). */

import { useEffect, useRef, useState, type DragEvent, type JSX, type KeyboardEvent, type MouseEvent, type MutableRefObject, type ReactNode } from "react";
import type { FlightPlan, StepKindDef } from "../../lib/flights";
import { deletePlan, resetPlan, savePlan } from "../../lib/flightsApi";
import { checkRename, planDots } from "../../lib/groundedLines";
import { clickOrDouble, type ClickOrDouble } from "../../lib/clickOrDouble";
import { cardMenu, catalogCards, familyLabel, gridMove, savedDragData, stopDragData, type CatalogCard, type CatalogDrop } from "../../lib/stopCatalog";
import { errMsg } from "../../lib/format";
import { toast } from "../../lib/toast";
import { InlineConfirm } from "../InlineConfirm";
import { Modal } from "../Modal";
import { OverflowMenu } from "../OverflowMenu";
import { StopIcon } from "./Bird";
import { TakeoffDialog } from "./TakeoffDialog";
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

export interface StopCatalogBodyProps {
  /** The kinds (`useFlightKinds`), as the server lists them. */
  kinds: readonly StepKindDef[];
  /** The saved stops (`useFlightPlans`), built-ins first. */
  plans: readonly FlightPlan[];
  /** A card's click or double-click (once — `clickOrDouble`), its Add, Enter
   * in the filter. Without it the cards only drag. */
  onAdd?(drop: CatalogDrop): void;
  /** Add puts the block ON THE BOARD (the host's `onPlace`) rather than on
   * the focused line: the words say so. */
  place?: boolean;
  /** Is this block on the bottom shelf? With `onShelfToggle`, every card gets
   * a ★ / ☆ that puts it on the shelf or takes it off (the shelf's own pick). */
  shelf?(drop: CatalogDrop): boolean;
  onShelfToggle?(drop: CatalogDrop): void;
  /** The palette's cards: an icon, the name, one line (a kind's summary, a
   * saved stop's boxes); no badges, no Add (a click places). */
  compact?: boolean;
  /** The list's and the cards' ids: "sky-catalog" (the Modal), "sky-palette". */
  idPrefix?: string;
  /** Extra classes on the list (the palette's `sky-palette-body`). */
  listClass?: string;
  /** Where the filter sits: the surface draws its head around it. */
  head(filter: JSX.Element): ReactNode;
  /** A card before the kinds (the palette's Session) for this query, or null. */
  lead?(query: string): ReactNode;
  /** Focus the filter on mount (without scrolling anything to it). */
  autoFocus?: boolean;
  /** A card's drag began (true) or ended (false, `landed` when it dropped). */
  onDrag?(on: boolean, landed: boolean): void;
  /** Set to "Esc answers the open question" (true when it did): the Modal's. */
  escRef?: MutableRefObject<(() => boolean) | null>;
}

/** How many columns a grid of cards is laid out in right now (1 when it
 * can't tell — the tests, or nothing shown). */
function gridCols(list: HTMLElement | null): number {
  const g = list?.querySelector<HTMLElement>(".sky-catalog-grid");
  if (!g || typeof getComputedStyle === "undefined") return 1;
  return getComputedStyle(g).gridTemplateColumns.split(" ").filter(Boolean).length || 1;
}

/** Scroll the list (only the list: scrollIntoView would also scroll the sky
 * the palette floats in) until the card shows. The rects are screen px; the
 * list's own scale turns them into its px under the sky's zoom. */
function reveal(list: HTMLElement | null, el: HTMLElement | null) {
  if (!list || !el) return;
  const l = list.getBoundingClientRect();
  const r = el.getBoundingClientRect();
  const scale = list.offsetHeight ? l.height / list.offsetHeight : 1;
  if (r.top < l.top) list.scrollTop -= (l.top - r.top) / scale;
  else if (r.bottom > l.bottom) list.scrollTop += (r.bottom - l.bottom) / scale;
}

/** A click on a card's own controls (★, ⋯, the rename field, Add, the
 * question) is theirs, not the card's. */
const inner = (t: EventTarget) => !!(t as Element).closest?.("button, input, summary, .vf-menu, .set-confirm");

/** The catalog's content — the filter (handed to `head`), then **Blocks** (a
 * grid per family under its label row — Work · Ship · Outside · People — or
 * one flat grid when the server names no family) and **Multi-step**, grids
 * of cards — shared by the floating palette (its whole body) and the All
 * blocks… Modal. */
export function StopCatalogBody({
  kinds,
  plans,
  onAdd,
  place = false,
  shelf,
  onShelfToggle,
  compact = false,
  idPrefix = "sky-catalog",
  listClass = "",
  head,
  lead,
  autoFocus = false,
  onDrag,
  escRef,
}: StopCatalogBodyProps): JSX.Element {
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const [renaming, setRenaming] = useState("");
  const [deleting, setDeleting] = useState("");
  const [takeoff, setTakeoff] = useState<FlightPlan | null>(null);
  const [busy, setBusy] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);
  const filterRef = useRef<HTMLInputElement>(null);
  const keyed = useRef(false);
  const cards = catalogCards(kinds, plans, query);
  const rows = cards.all;
  const at = Math.min(active, rows.length - 1);
  const listId = idPrefix + "-list";
  const rowId = (i: number) => idPrefix + "-row-" + i;
  const leadEl = lead?.(query) ?? null;
  const leads = leadEl ? 1 : 0;
  // The kinds by family (round 15), or one flat grid from an older server.
  // The lead sits first in Work (flat: the one grid), even when the filter
  // left that group nothing else; Work leads the order, so it is still first.
  const leadFamily = cards.grouped ? "work" : "";
  const groups =
    leadEl && !cards.groups.some((g) => g.family === leadFamily) ? [{ family: leadFamily, label: cards.grouped ? familyLabel(leadFamily) : "", items: [] }, ...cards.groups] : cards.groups;
  const leadIn = (family: string) => (leadEl && family === leadFamily ? 1 : 0);

  useEffect(() => {
    if (autoFocus) filterRef.current?.focus({ preventScroll: true });
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!keyed.current || at < 0) return;
    keyed.current = false;
    reveal(listRef.current, document.getElementById(rowId(at)));
  }, [at]); // eslint-disable-line react-hooks/exhaustive-deps

  if (escRef)
    escRef.current = () => {
      // An open delete question takes Esc first (a rename field spends its own).
      if (!deleting) return false;
      setDeleting("");
      return true;
    };

  // One click-or-double per card, kept across renders (a waiting click must
  // meet its second click), calling the newest onAdd.
  const addRef = useRef(onAdd);
  addRef.current = onAdd;
  const clicks = useRef(new Map<string, ClickOrDouble>());
  useEffect(() => () => clicks.current.forEach((c) => c.cancel()), []);
  const clicker = (key: string, drop: CatalogDrop) => {
    let c = clicks.current.get(key);
    if (!c) clicks.current.set(key, (c = clickOrDouble(() => addRef.current?.(drop))));
    return c;
  };

  const add = (d: CatalogDrop) => onAdd?.(d);

  const onFilterKey = (e: KeyboardEvent<HTMLInputElement>) => {
    const el = e.currentTarget;
    // ← / → move the pick only from the filter's edge (else they move the caret).
    const edge = e.key === "ArrowLeft" ? el.selectionStart === 0 && el.selectionEnd === 0 : e.key === "ArrowRight" ? el.selectionStart === el.value.length : true;
    if ((e.key === "ArrowDown" || e.key === "ArrowUp" || e.key === "ArrowLeft" || e.key === "ArrowRight") && edge) {
      e.preventDefault();
      if (!rows.length) return;
      keyed.current = true;
      // The lead card sits first in the first grid but is no row: the arrows
      // walk the grids (one per family) as laid out and never land on it.
      setActive(Math.max(0, gridMove(at + leads, e.key, [...groups.map((g) => g.items.length + leadIn(g.family)), cards.multi.length], gridCols(listRef.current)) - leads));
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (at >= 0) add(rows[at].drop);
    }
  };

  const onDragStart = (e: DragEvent, key: string, data: Array<[string, string]>) => {
    clicks.current.get(key)?.cancel();
    for (const [type, value] of data) e.dataTransfer.setData(type, value);
    e.dataTransfer.effectAllowed = "copy";
    // A tick later: changing the page inside dragstart can cancel the drag.
    if (onDrag) setTimeout(() => onDrag(true, false), 0);
  };
  const onDragEnd = (e: DragEvent) => onDrag?.(false, !!e.dataTransfer && e.dataTransfer.dropEffect !== "none");

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
        title={on ? "On the bottom shelf — click to take it off" : "Put it on the bottom shelf"}
        onClick={() => onShelfToggle(d)}
      >
        {on ? "★" : "☆"}
      </button>
    );
  };

  const how = onAdd ? (place ? "Drag onto a line or the sky, or click to put it on the board" : "Drag onto a line or the sky, or click to add it to the focused line") : "Drag onto a line or the sky";

  const card = (c: CatalogCard<StepKindDef, FlightPlan>, n: number) => {
    const plan = c.section === "multi" ? c.plan : null;
    const def = c.section === "blocks" ? c.def : null;
    const naming = !!plan && renaming === plan.id;
    const asking = !!plan && deleting === plan.id;
    const on = n === at;
    const cd = clicker(c.key, c.drop);
    const mine = (e: MouseEvent) => !!onAdd && !naming && !asking && !inner(e.target);
    const about = plan ? (c.section === "multi" && c.path ? c.path : "No steps yet") : def?.summary;
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
        title={compact && about ? about + "\n" + how : how}
        onDragStart={(e) => onDragStart(e, c.key, plan ? savedDragData(plan) : stopDragData(def as StepKindDef))}
        onDragEnd={onDragEnd}
        onMouseEnter={() => setActive(n)}
        onClick={(e) => mine(e) && cd.onClick(e)}
        onDoubleClick={(e) => mine(e) && cd.onDoubleClick()}
      >
        <div className="sky-catalog-card-head">
          {def && (
            <span className="sky-catalog-icon">
              <StopIcon kind={def.actor === "you" ? "perch" : def.kind} size={compact ? 14 : 18} />
            </span>
          )}
          <span className="sky-catalog-name">
            {plan && naming ? <PlanNameField plan={plan} plans={plans} onDone={() => setRenaming("")} /> : <span className="sky-catalog-title">{c.name}</span>}
            {compact ? null : c.section === "blocks" ? (
              <span className={"sky-catalog-badge actor-" + (c.perch ? "you" : "robot")}>{c.perch ? "you" : "robot"}</span>
            ) : (
              c.builtin && <span className="sky-catalog-badge builtin">built-in</span>
            )}
          </span>
          {star(c.drop, c.name)}
          {plan && (
            <OverflowMenu
              label={"“" + plan.name + "” — rename, delete, shelf"}
              boundaryId={listId}
              items={cardMenu(plan, shelf ? shelf(c.drop) : false)
                .filter((it) => it.key !== "shelf" || onShelfToggle)
                .map((it) => ({
                  key: it.key,
                  label: it.label,
                  danger: it.danger,
                  onSelect: () => {
                    if (it.key === "rename") setRenaming(plan.id);
                    else if (it.key === "takeoff") setTakeoff(plan);
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
            {!compact && <span className="sky-catalog-summary">{about}</span>}
          </>
        ) : (
          about && <span className="sky-catalog-summary">{about}</span>
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
          onAdd &&
          !compact && (
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

  const filter = (
    <input
      ref={filterRef}
      className="fl-input sky-catalog-filter"
      type="text"
      placeholder="Filter blocks…"
      aria-label="Filter blocks"
      aria-controls={listId}
      aria-activedescendant={at >= 0 ? rowId(at) : undefined}
      autoComplete="off"
      spellCheck={false}
      value={query}
      onChange={(e) => {
        setQuery(e.target.value);
        setActive(0);
      }}
      onKeyDown={onFilterKey}
    />
  );

  const what = place ? "puts it on the board" : "adds it to the focused line";
  let i = -1;
  return (
    <>
      {head(filter)}
      <div
        className={"sky-catalog-list sky-own-scroll" + (compact ? " compact" : "") + (cards.grouped ? " grouped" : "") + (listClass ? " " + listClass : "")}
        id={listId}
        role="listbox"
        aria-label="All blocks"
        ref={listRef}
      >
        {groups.length > 0 && (
          <section className="sky-catalog-section" data-section="blocks" role="group" aria-label="Blocks">
            {cards.grouped ? (
              groups.map((g) => (
                <div key={g.family} className="sky-catalog-family" data-family={g.family}>
                  <h3 className="sky-catalog-label">{g.label}</h3>
                  <div className="sky-catalog-grid">
                    {leadIn(g.family) ? leadEl : null}
                    {g.items.map((c) => card(c, ++i))}
                  </div>
                </div>
              ))
            ) : (
              <>
                <h3 className="sky-catalog-label">Blocks</h3>
                <div className="sky-catalog-grid">
                  {leadEl}
                  {groups[0].items.map((c) => card(c, ++i))}
                </div>
              </>
            )}
          </section>
        )}
        {cards.multi.length > 0 && (
          <section className="sky-catalog-section" data-section="multi" role="group" aria-label="Multi-step">
            <h3 className="sky-catalog-label">Multi-step</h3>
            <div className="sky-catalog-grid">{cards.multi.map((c) => card(c, ++i))}</div>
          </section>
        )}
        {rows.length + leads === 0 && <p className="sky-catalog-empty muted">{query.trim() ? "Nothing matches “" + query.trim() + "”." : "No blocks yet — this server may not know Flights."}</p>}
        <p className="sky-catalog-hint muted">
          {onAdd ? "Drag a block onto a line or the sky · a click or a double-click " + what + " · the arrows pick, Enter " + what : "Drag a block onto a line or the sky"}
        </p>
      </div>
      {takeoff && <TakeoffDialog plan={takeoff} onClose={() => setTakeoff(null)} />}
    </>
  );
}

export interface StopCatalogProps extends Omit<StopCatalogBodyProps, "head" | "compact" | "idPrefix" | "listClass" | "autoFocus" | "onDrag" | "escRef"> {
  onClose(): void;
  /** Default true (into document.body); false renders in place (the tests). */
  portal?: boolean;
}

/** **All blocks** as a dialog — the shelf's fallback when its host floats no
 * palette (`onCatalog` not given). A card the size of a good dialog, not of
 * the window (round 14: the board must show around it, so a drag out of it
 * reads as the move). A card being dragged lets the pointer through to the
 * sky (`.is-dragging`, the backdrop would take the drop) and fades it; a drop
 * that landed closes it. */
export function StopCatalog({ onClose, portal = true, ...body }: StopCatalogProps): JSX.Element {
  const [dragging, setDragging] = useState(false);
  const esc = useRef<(() => boolean) | null>(null);
  return (
    <Modal
      id="sky-catalog"
      className={"sky-catalog-modal" + (dragging ? " is-dragging" : "")}
      onClose={onClose}
      onEscape={() => esc.current?.() ?? false}
      portal={portal}
      role="dialog"
      aria-modal="true"
      aria-labelledby="sky-catalog-title"
    >
      <div className="sky-catalog" onKeyDown={(e) => e.key === "Escape" || e.stopPropagation()}>
        <StopCatalogBody
          {...body}
          autoFocus
          escRef={esc}
          onDrag={(on, landed) => {
            setDragging(on);
            if (!on && landed) onClose();
          }}
          head={(filter) => (
            <div className="sky-catalog-head">
              <h2 id="sky-catalog-title">All blocks</h2>
              {filter}
              <button type="button" className="fl-icon-btn sky-catalog-close" aria-label="Close" title="Close (Esc)" onClick={onClose}>
                ×
              </button>
            </div>
          )}
        />
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
