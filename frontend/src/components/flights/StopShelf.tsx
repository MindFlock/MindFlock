/** The shelf at the Sky's foot: ONE row, **Stops** (docs/flights.md "As built:
 * round 12 shelf" and "As built: round 13 palette"), always there, folding to a thin handle. Three regions,
 * each with a tiny label, split by thin dividers:
 *
 *  1. **Blocks**: the **Session** chip — drag it onto empty sky and the
 *     new-session block opens there; onto a grounded line and a new session
 *     is made for those stops; a click opens the block in the middle of the
 *     sky (`NEW_SESSION_MIME`, lib/skyLayout) — then the default kinds
 *     (`DEFAULT_KINDS`, lib/stopCatalog: Agent, Perch, Ship, Verify, Wait,
 *     Loop; one the server doesn't list is skipped). Each carries its kind under
 *     `STOP_MIME` (and its name as plain text).
 *  2. **Multi-step**: the SAVED STOPS (`useFlightPlans`, built-ins first): the
 *     name and a row of dots, one per step (a perch's gold, "+N" past ten).
 *     Each carries its id under `FLYWAY_MIME` and its name as plain text.
 *     Right-click (or the context-menu key): Rename (a field in the chip),
 *     Takes off… (the TakeoffDialog), Reset to default (a built-in), Delete
 *     (yours; confirmed inline).
 *  3. **All blocks…**: `onCatalog` — the host floats the palette large over
 *     the board (round 14: every kind, every saved stop, each card dragged
 *     like its chip). Without `onCatalog`, `StopCatalog` opens as a dialog.
 *
 * Any chip dropped on empty sky becomes stops with no session yet (a grounded
 * line); dropped on a line it is tacked on there — the sky owns the drop.
 * CLICK or DOUBLE-CLICK a kind or a saved stop — once either way
 * (`useClickOrDouble`: a click waits ~250 ms for a second) — or tap it on a
 * phone, where nothing drags →
 * `onPlace({kind} | {planId})`: the host puts it ON THE BOARD near the shelf
 * (round 13: nothing lands in a session by itself). Without `onPlace` a click
 * is `onTack(kind)` / `onTackPlan(plan)` (onto the focused line, as before);
 * without any of them a chip only drags. The fold handle's ⤢ floats the
 * blocks (`onFloat`: the host swaps the shelf for SkyPalette).
 *
 * The chips, the pick (`useShelfPick`, which governs only this shelf since
 * round 14), `sessionDragData` and a saved stop's menu (`useSavedActions`)
 * are exported. */

import { useEffect, useRef, useState, type DragEvent, type JSX, type KeyboardEvent, type MouseEvent } from "react";
import type { FlightPlan, StepKindDef } from "../../lib/flights";
import { deletePlan, resetPlan } from "../../lib/flightsApi";
import { useClickOrDouble } from "../../lib/clickOrDouble";
import { NEW_SESSION_MIME } from "../../lib/skyLayout";
import { planDots, savedStopOrder } from "../../lib/groundedLines";
import { SHELF_KEY, onShelf, readShelf, savedChainLabel, savedDragData, shelfKinds, shelfPlans, stopDragData, toggleShelf, type CatalogDrop, type ShelfPick } from "../../lib/stopCatalog";
import { errMsg } from "../../lib/format";
import { toast } from "../../lib/toast";
import { useFlightKinds, useFlightPlans } from "../../state/queries";
import { InlineConfirm } from "../InlineConfirm";
import { StopIcon } from "./Bird";
import { kindName } from "./StopParts";
import { PlanNameField, StopCatalog } from "./StopCatalog";
import { TakeoffDialog } from "./TakeoffDialog";
import "./StopMenu.css";
import "./StopShelf.css";

/** What the Session chip's drag carries. */
export function sessionDragData(): Array<[string, string]> {
  return [
    [NEW_SESSION_MIME, "session"],
    ["text/plain", "New session"],
  ];
}

function carry(e: DragEvent, data: Array<[string, string]>) {
  for (const [type, value] of data) e.dataTransfer.setData(type, value);
  e.dataTransfer.effectAllowed = "copy";
}

function onStopDrag(e: DragEvent, def: StepKindDef) {
  for (const [type, value] of stopDragData(def)) e.dataTransfer.setData(type, value);
  e.dataTransfer.effectAllowed = "copy";
}

/** What a click on a chip does, in its title: put it on the board (`place`),
 * tack it onto the focused line, or nothing (it only drags). */
const PLACE_HOW = "Click to put it on the board, or drag it onto a chain or the sky";
const clickHow = (click: boolean, place: boolean, tack: string) => (!click ? "Drag onto a chain or the sky" : place ? PLACE_HOW : tack);

/** A kind's chip (the shelf, the floating palette). `onClick` with `place`
 * puts it on the board; without `place` it tacks onto the focused line. */
export function StopChip({ def, onClick, place = false, onMenu }: { def: StepKindDef; onClick?(kind: string): void; place?: boolean; onMenu?(def: StepKindDef, x: number, y: number): void }) {
  const name = kindName(def);
  const perch = def.actor === "you";
  const how = clickHow(!!onClick, place, "Click to tack it onto the focused chain, or drag it onto a chain or the sky");
  const cd = useClickOrDouble(onClick && (() => onClick(def.kind)));
  const props = {
    className: "sky-stop-chip" + (perch ? " perch" : "") + (onClick ? "" : " drag-only"),
    "data-kind": def.kind,
    draggable: true,
    title: (def.summary ? def.summary + "\n" : "") + how + (onMenu ? " — right-click for more" : ""),
    onDragStart: (e: DragEvent) => {
      cd.cancel();
      onStopDrag(e, def);
    },
    onContextMenu: onMenu
      ? (e: MouseEvent) => {
          e.preventDefault();
          onMenu(def, e.clientX, e.clientY);
        }
      : undefined,
  };
  const body = (
    <>
      <span className="sky-stop-icon">
        <StopIcon kind={perch ? "perch" : def.kind} size={14} />
      </span>
      <span className="sky-stop-name">{name}</span>
    </>
  );
  return (
    <span role="listitem" className="sky-stop-item">
      {onClick ? (
        <button type="button" {...props} aria-label={place ? "Put “" + name + "” on the board" : "Tack “" + name + "” onto the focused chain"} onClick={cd.onClick} onDoubleClick={cd.onDoubleClick}>
          {body}
        </button>
      ) : (
        <span {...props}>{body}</span>
      )}
    </span>
  );
}

/** The Session chip: drags the new-session type; a click opens the block. */
export function SessionChip({ onSession }: { onSession?(): void }) {
  const cd = useClickOrDouble(onSession);
  const props = {
    className: "sky-stop-chip sky-session-chip" + (onSession ? "" : " drag-only"),
    "data-tour": "flights-newblock",
    draggable: true,
    title: "A new session: drag it onto the sky (or onto stops with no session yet), or click — Ctrl+N, or double-click the sky",
    onDragStart: (e: DragEvent) => {
      cd.cancel();
      carry(e, sessionDragData());
    },
  };
  const body = (
    <>
      <span className="sky-stop-icon">
        <StopIcon kind="session" size={14} />
      </span>
      <span className="sky-stop-name">Session</span>
    </>
  );
  return (
    <span role="listitem" className="sky-stop-item">
      {onSession ? (
        <button type="button" {...props} aria-label="Start a new session" onClick={cd.onClick} onDoubleClick={cd.onDoubleClick}>
          {body}
        </button>
      ) : (
        <span {...props}>{body}</span>
      )}
    </span>
  );
}

type MenuKey = "rename" | "takeoff" | "reset" | "delete" | "unshelve";

/** A saved stop's menu: what it offers, by whether it is a built-in. */
export function savedMenuItems(plan: Pick<FlightPlan, "builtin">): Array<{ key: MenuKey; label: string; danger?: boolean }> {
  return [
    { key: "rename", label: "Rename" },
    { key: "takeoff", label: "What starts it…" },
    { key: "unshelve", label: "Take off the shelf" },
    plan.builtin ? { key: "reset", label: "Reset to default" } : { key: "delete", label: "Delete", danger: true },
  ];
}

/** The menu at the pointer, in the shape of a box's (StopMenu): ↑ / ↓, Enter,
 * Esc or a press elsewhere closes it. */
function SavedMenu({ label, items, x, y, onPick, onClose }: { label: string; items: Array<{ key: MenuKey; label: string; danger?: boolean }>; x: number; y: number; onPick(k: MenuKey): void; onClose(): void }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    ref.current?.querySelector<HTMLButtonElement>("button")?.focus();
    const onDown = (e: Event) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    };
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      e.preventDefault();
      onClose();
    };
    document.addEventListener("pointerdown", onDown, true);
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("pointerdown", onDown, true);
      document.removeEventListener("keydown", onKey, true);
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const all = [...(ref.current?.querySelectorAll<HTMLButtonElement>("button[role=menuitem]") || [])];
    const i = all.indexOf(document.activeElement as HTMLButtonElement);
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      all[(i + (e.key === "ArrowDown" ? 1 : -1) + all.length) % all.length]?.focus();
    } else if (e.key === "Tab") {
      e.preventDefault();
      onClose();
    }
  };
  return (
    <div
      ref={ref}
      className="fl-stopmenu sky-saved-menu"
      role="menu"
      aria-label={"“" + label + "” — what you can do"}
      style={{ position: "fixed", left: x, top: y }}
      onKeyDown={onKey}
      onContextMenu={(e) => e.preventDefault()}
    >
      {items.map((it) => (
        <button key={it.key} type="button" role="menuitem" className={"fl-stopmenu-item" + (it.danger ? " danger" : "")} onClick={() => onPick(it.key)}>
          {it.label}
        </button>
      ))}
    </div>
  );
}

/** A saved stop's chip: its name and a dot per step. `onClick` with `place`
 * puts it on the board; without `place` it adds to the focused line. */
export function SavedChip({
  plan,
  plans,
  kinds,
  renaming,
  onClick,
  place = false,
  onMenu,
  onRenamed,
}: {
  plan: FlightPlan;
  plans: readonly FlightPlan[];
  kinds: readonly StepKindDef[];
  renaming: boolean;
  onClick?(plan: FlightPlan): void;
  place?: boolean;
  onMenu(plan: FlightPlan, x: number, y: number): void;
  onRenamed(): void;
}) {
  const { dots, more } = planDots(plan.steps, kinds);
  const cd = useClickOrDouble(onClick && (() => onClick(plan)));

  const menuAt = (e: MouseEvent) => {
    e.preventDefault();
    onMenu(plan, e.clientX, e.clientY);
  };
  const menuKey = (e: KeyboardEvent) => {
    if (e.key !== "ContextMenu" && !(e.shiftKey && e.key === "F10")) return;
    e.preventDefault();
    const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
    onMenu(plan, r.left, r.top - 8);
  };

  const body = (
    <>
      {renaming ? <PlanNameField plan={plan} plans={plans} onDone={onRenamed} /> : <span className="sky-stop-name">{plan.name}</span>}
      <span className="sky-saved-dots" aria-hidden="true">
        {dots.map((d, i) => (
          <i key={i} className={d.perch ? "perch" : ""} />
        ))}
        {more > 0 && <span className="sky-saved-more">+{more}</span>}
      </span>
    </>
  );
  const how = clickHow(!!onClick, place, "Click to add it to the focused chain, or drag it onto a chain or the sky");
  const props = {
    className: "sky-stop-chip sky-saved-chip" + (plan.builtin ? " builtin" : "") + (onClick ? "" : " drag-only") + (renaming ? " is-renaming" : ""),
    "data-plan": plan.id,
    draggable: !renaming,
    title: savedChainLabel(plan.steps.length) + (plan.steps.length ? ": " + plan.steps.map((s) => s.title).join(" → ") : "") + "\n" + how + " — right-click for more",
    onDragStart: (e: DragEvent) => {
      cd.cancel();
      carry(e, savedDragData(plan));
    },
    onContextMenu: menuAt,
    onKeyDown: menuKey,
  };
  return (
    <span role="listitem" className="sky-stop-item">
      {onClick && !renaming ? (
        <button type="button" {...props} aria-label={place ? "Put “" + plan.name + "” on the board" : "Add “" + plan.name + "” to the focused chain"} onClick={cd.onClick} onDoubleClick={cd.onDoubleClick}>
          {body}
        </button>
      ) : (
        <span {...props}>{body}</span>
      )}
    </span>
  );
}

// --- The pick, and a saved stop's menu, shared by the shelf and the palette ---

/** Fired on window with the new pick, so the shelf, the floating palette and
 * the catalog they open always agree (another tab hears `storage`). */
const SHELF_EVENT = "mf:sky-shelf";

function storedShelf(): ShelfPick {
  try {
    return readShelf(localStorage.getItem(SHELF_KEY));
  } catch {
    return readShelf(null);
  }
}

/** What sits on the shelf (and in the floating palette): the owner's pick,
 * per device (`mf_sky_shelf`) — the default blocks to begin with, no
 * multi-step block; the catalog's ★ adds, a chip's "Take off the shelf"
 * removes. */
export function useShelfPick(): [ShelfPick, (next: (p: ShelfPick) => ShelfPick) => void] {
  const [shelf, setShelf] = useState<ShelfPick>(storedShelf);
  const now = useRef(shelf);
  now.current = shelf;
  useEffect(() => {
    const heard = (e: Event) => setShelf((e as CustomEvent<ShelfPick>).detail);
    const other = (e: StorageEvent) => {
      if (e.key === SHELF_KEY) setShelf(storedShelf());
    };
    window.addEventListener(SHELF_EVENT, heard);
    window.addEventListener("storage", other);
    return () => {
      window.removeEventListener(SHELF_EVENT, heard);
      window.removeEventListener("storage", other);
    };
  }, []);
  const change = (next: (p: ShelfPick) => ShelfPick) => {
    const n = next(now.current);
    now.current = n;
    try {
      localStorage.setItem(SHELF_KEY, JSON.stringify(n));
    } catch {
      /* a private window: the pick lives for the session */
    }
    setShelf(n);
    window.dispatchEvent(new CustomEvent(SHELF_EVENT, { detail: n }));
  };
  return [shelf, change];
}

/** A saved stop's right-click menu and a kind's (Take off the shelf), Rename
 * in the chip, Takes off…, Reset, and Delete confirmed inline — the state and
 * what they draw (`confirm` goes where the surface wants the question,
 * `overlays` anywhere). */
export function useSavedActions(changeShelf: (next: (p: ShelfPick) => ShelfPick) => void) {
  const [menu, setMenu] = useState<{ plan: FlightPlan; x: number; y: number } | null>(null);
  const [kindMenu, setKindMenu] = useState<{ def: StepKindDef; x: number; y: number } | null>(null);
  const [renaming, setRenaming] = useState("");
  const [takeoff, setTakeoff] = useState<FlightPlan | null>(null);
  const [deleting, setDeleting] = useState<FlightPlan | null>(null);
  const [busy, setBusy] = useState(false);

  const pick = async (key: MenuKey) => {
    const plan = menu?.plan;
    setMenu(null);
    if (!plan) return;
    if (key === "rename") setRenaming(plan.id);
    else if (key === "unshelve") changeShelf((p) => toggleShelf(p, { planId: plan.id }));
    else if (key === "takeoff") setTakeoff(plan);
    else if (key === "delete") setDeleting(plan);
    else if (key === "reset") {
      try {
        await resetPlan(plan.id);
        toast("“" + plan.name + "” is back to its default");
      } catch (e) {
        toast("Reset failed: " + errMsg(e), { error: true });
      }
    }
  };

  const remove = async () => {
    if (!deleting) return;
    setBusy(true);
    try {
      await deletePlan(deleting.id);
      toast("Deleted “" + deleting.name + "”");
      setDeleting(null);
    } catch (e) {
      toast("Deleting failed: " + errMsg(e), { error: true });
    } finally {
      setBusy(false);
    }
  };

  const confirm = deleting ? (
    <InlineConfirm
      title={"Delete “" + deleting.name + "”?"}
      body="It leaves the shelf. Stops already on the board stay."
      confirmLabel="Delete"
      busy={busy}
      onConfirm={() => void remove()}
      onCancel={() => setDeleting(null)}
    />
  ) : null;

  const overlays = (
    <>
      {menu && <SavedMenu label={menu.plan.name} items={savedMenuItems(menu.plan)} x={menu.x} y={menu.y} onPick={(k) => void pick(k)} onClose={() => setMenu(null)} />}
      {kindMenu && (
        <SavedMenu
          label={kindName(kindMenu.def)}
          items={[{ key: "unshelve", label: "Take off the shelf" }]}
          x={kindMenu.x}
          y={kindMenu.y}
          onPick={() => {
            changeShelf((p) => toggleShelf(p, { kind: kindMenu.def.kind }));
            setKindMenu(null);
          }}
          onClose={() => setKindMenu(null)}
        />
      )}
      {takeoff && <TakeoffDialog plan={takeoff} onClose={() => setTakeoff(null)} />}
    </>
  );

  return {
    renaming,
    doneRenaming: () => setRenaming(""),
    openMenu: (plan: FlightPlan, x: number, y: number) => setMenu({ plan, x, y }),
    openKindMenu: (def: StepKindDef, x: number, y: number) => setKindMenu({ def, x, y }),
    confirm,
    overlays,
  };
}

export interface StopShelfProps {
  onTack?(kind: string): void;
  onTackPlan?(plan: FlightPlan): void;
  /** The Session chip pressed: the new-session block in the sky's middle. */
  onSession?(): void;
  /** Folded to its handle (the host remembers it). */
  folded?: boolean;
  onFold?(folded: boolean): void;
  /** Round 13 — a diagram tool's manners: a chip click / the catalog's Add
   * PLACES the block on the board (a grounded line near the palette) instead
   * of tacking it onto the focused line. When given, it replaces the
   * tack-on-click paths; `onTack` / `onTackPlan` stay for the drops on a line. */
  onPlace?(drop: CatalogDrop): void;
  /** The shelf's "Float the blocks" (the host switches `mf_sky_palette`). */
  onFloat?(): void;
  /** "All blocks…" (round 14): the host floats the palette large over the
   * board — every block, the board around it. Without it, the catalog opens
   * as a dialog (`StopCatalog`). */
  onCatalog?(): void;
}

export function StopShelf({ onTack, onTackPlan, onSession, folded = false, onFold, onPlace, onFloat, onCatalog }: StopShelfProps): JSX.Element {
  const kinds = useFlightKinds().data;
  const plans = useFlightPlans().data;
  const [shelf, changeShelf] = useShelfPick();
  const blocks = shelfKinds(shelf, kinds || []);
  const allSaved = savedStopOrder(plans || []);
  const saved = shelfPlans(shelf, allSaved);
  const acts = useSavedActions(changeShelf);
  const [catalog, setCatalog] = useState(false);
  // A click: onto the board when the host places (round 13), else tacked
  // onto the focused line (the pane's Flight tab, and round 12's sky).
  const place = !!onPlace;
  const clickKind = onPlace ? (kind: string) => onPlace({ kind }) : onTack;
  const clickPlan = onPlace ? (plan: FlightPlan) => onPlace({ planId: plan.id }) : onTackPlan;

  /** The catalog's Add: through the same props as a chip's click. */
  const add = (d: CatalogDrop) => {
    setCatalog(false);
    if (onPlace) return onPlace(d);
    if ("kind" in d) onTack?.(d.kind);
    else {
      const plan = allSaved.find((p) => p.id === d.planId);
      if (plan) onTackPlan?.(plan);
    }
  };

  return (
    <section className={"sky-shelf" + (folded ? " folded" : " open")} data-tour="sky-shelf" data-toast-clear aria-label="The shelf of stops">
      <span className="sky-shelf-tabs">
        <button
          type="button"
          className="sky-shelf-fold"
          aria-expanded={!folded}
          title={folded ? "Show the stops" : "Fold the stops away"}
          onClick={() => onFold?.(!folded)}
        >
          Stops {folded ? "▴" : "▾"}
        </button>
        {onFloat && (
          <button type="button" className="sky-shelf-fold sky-shelf-float" aria-label="Float the stops" title="Float the stops — a palette on the board you can move and size" onClick={onFloat}>
            ⤢
          </button>
        )}
      </span>
      {!folded && (
        <div className="sky-stops" role="group" aria-label="Stops">
          <span className="sky-stops-head">
            <strong>Stops</strong> <span className="sky-stops-hint">— drag onto the sky or a chain</span>
          </span>
          {kinds !== undefined && !kinds?.length ? (
            <p className="sky-stops-empty muted">The stop list didn't load — this server may be too old for the Sky.</p>
          ) : (
            <div className="sky-stops-chips">
              <div className="sky-stops-region" data-region="blocks" role="list" aria-label="Stops">
                <span className="sky-stops-label" aria-hidden="true">
                  Stops
                </span>
                <SessionChip onSession={onSession} />
                {blocks.map((k) => (
                  <StopChip key={k.kind} def={k} onClick={clickKind} place={place} onMenu={acts.openKindMenu} />
                ))}
              </div>
              {saved.length > 0 && (
                <>
                  <span className="sky-stops-divider" aria-hidden="true" />
                  <div className="sky-stops-region" data-region="multi" role="list" aria-label="Saved chains">
                    <span className="sky-stops-label" aria-hidden="true">
                      Saved chains
                    </span>
                    {saved.map((p) => (
                      <SavedChip
                        key={p.id}
                        plan={p}
                        plans={allSaved}
                        kinds={kinds || []}
                        renaming={acts.renaming === p.id}
                        onClick={clickPlan}
                        place={place}
                        onMenu={acts.openMenu}
                        onRenamed={acts.doneRenaming}
                      />
                    ))}
                  </div>
                </>
              )}
              <span className="sky-stops-divider" aria-hidden="true" />
              <button
                type="button"
                className="sky-stop-chip sky-catalog-chip"
                data-tour="sky-catalog"
                aria-haspopup={onCatalog ? undefined : "dialog"}
                aria-expanded={onCatalog ? undefined : catalog}
                title={onCatalog ? "Every stop and saved chain, in a palette on the board you can filter and drag from" : "Every stop and every saved chain, in a list you can filter"}
                onClick={() => (onCatalog ? onCatalog() : setCatalog(true))}
              >
                <span className="sky-stop-icon" aria-hidden="true">
                  ⊞
                </span>
                <span className="sky-stop-name">All stops…</span>
              </button>
            </div>
          )}
        </div>
      )}
      {acts.confirm && <div className="sky-stops-confirm">{acts.confirm}</div>}
      {acts.overlays}
      {catalog && (
        <StopCatalog
          kinds={kinds || []}
          plans={allSaved}
          onAdd={onPlace || onTack || onTackPlan ? add : undefined}
          place={place}
          onClose={() => setCatalog(false)}
          shelf={(d) => onShelf(shelf, d)}
          onShelfToggle={(d) => changeShelf((p) => toggleShelf(p, d))}
        />
      )}
    </section>
  );
}
