/** The shelf at the Sky's foot: ONE row, **Stops** (docs/flights.md "As built:
 * round 12 shelf"), always there, folding to a thin handle. Three regions,
 * each with a tiny label, split by thin dividers:
 *
 *  1. **Blocks**: the **Session** chip — drag it onto empty sky and the
 *     new-session block opens there; onto a grounded line and a new session
 *     is made for those stops; a click opens the block in the middle of the
 *     sky (`NEW_SESSION_MIME`, lib/skyLayout) — then the default kinds
 *     (`DEFAULT_KINDS`, lib/stopCatalog: Agent, Perch, Ship, Verify, Wait;
 *     one the server doesn't list is skipped). Each carries its kind under
 *     `STOP_MIME` (and its name as plain text).
 *  2. **Multi-step**: the SAVED STOPS (`useFlightPlans`, built-ins first): the
 *     name and a row of dots, one per step (a perch's gold, "+N" past ten).
 *     Each carries its id under `FLYWAY_MIME` and its name as plain text.
 *     Right-click (or the context-menu key): Rename (a field in the chip),
 *     Takes off… (the TakeoffDialog), Reset to default (a built-in), Delete
 *     (yours; confirmed inline).
 *  3. **All blocks…**: opens `StopCatalog` — every kind (the rest of the
 *     registry lives only there) and every saved stop, each row dragged like
 *     its chip, with an Add that goes through the same `onTack` / `onTackPlan`.
 *
 * Any chip dropped on empty sky becomes stops with no session yet (a grounded
 * line); dropped on a line it is tacked on there — the sky owns the drop.
 * CLICK a kind or a saved stop (or tap it on a phone, where nothing drags) →
 * `onTack(kind)` / `onTackPlan(plan)`: the host tacks it onto the focused
 * line. Without them a chip only drags. */

import { useEffect, useRef, useState, type DragEvent, type JSX, type KeyboardEvent, type MouseEvent } from "react";
import type { FlightPlan, StepKindDef } from "../../lib/flights";
import { deletePlan, resetPlan, savePlan } from "../../lib/flightsApi";
import { NEW_SESSION_MIME } from "../../lib/skyLayout";
import { checkRename, planDots, savedStopOrder } from "../../lib/groundedLines";
import { SHELF_KEY, onShelf, readShelf, savedDragData, shelfKinds, shelfPlans, stopDragData, toggleShelf, type CatalogDrop, type ShelfPick } from "../../lib/stopCatalog";
import { errMsg } from "../../lib/format";
import { toast } from "../../lib/toast";
import { useFlightKinds, useFlightPlans } from "../../state/queries";
import { InlineConfirm } from "../InlineConfirm";
import { StopIcon } from "./Bird";
import { kindName } from "./StopParts";
import { StopCatalog } from "./StopCatalog";
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

function StopChip({ def, onTack, onMenu }: { def: StepKindDef; onTack?(kind: string): void; onMenu?(def: StepKindDef, x: number, y: number): void }) {
  const name = kindName(def);
  const perch = def.actor === "you";
  const how = onTack ? "Click to tack it onto the focused line, or drag it onto a line or the sky" : "Drag onto a line or the sky";
  const props = {
    className: "sky-stop-chip" + (perch ? " perch" : "") + (onTack ? "" : " drag-only"),
    "data-kind": def.kind,
    draggable: true,
    title: (def.summary ? def.summary + "\n" : "") + how + (onMenu ? " — right-click for more" : ""),
    onDragStart: (e: DragEvent) => onStopDrag(e, def),
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
      {onTack ? (
        <button type="button" {...props} aria-label={"Tack “" + name + "” onto the focused line"} onClick={() => onTack(def.kind)}>
          {body}
        </button>
      ) : (
        <span {...props}>{body}</span>
      )}
    </span>
  );
}

function SessionChip({ onSession }: { onSession?(): void }) {
  const props = {
    className: "sky-stop-chip sky-session-chip" + (onSession ? "" : " drag-only"),
    "data-tour": "flights-newblock",
    draggable: true,
    title: "A new session: drag it onto the sky (or onto stops with no session yet), or click — Ctrl+N, or double-click the sky",
    onDragStart: (e: DragEvent) => carry(e, sessionDragData()),
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
        <button type="button" {...props} aria-label="Start a new session" onClick={onSession}>
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
    { key: "takeoff", label: "Takes off…" },
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

function SavedChip({
  plan,
  plans,
  kinds,
  renaming,
  onTackPlan,
  onMenu,
  onRenamed,
}: {
  plan: FlightPlan;
  plans: readonly FlightPlan[];
  kinds: readonly StepKindDef[];
  renaming: boolean;
  onTackPlan?(plan: FlightPlan): void;
  onMenu(plan: FlightPlan, x: number, y: number): void;
  onRenamed(): void;
}) {
  const { dots, more } = planDots(plan.steps, kinds);
  const [name, setName] = useState(plan.name);
  const [err, setErr] = useState("");
  const done = useRef(false);
  useEffect(() => {
    if (!renaming) return;
    setName(plan.name);
    setErr("");
    done.current = false;
  }, [renaming, plan.name]);

  const commit = async (leaving: boolean) => {
    if (done.current) return;
    const c = checkRename(name, plans, plan.id);
    if (!c.ok) {
      if (!leaving) return setErr(c.error);
      done.current = true;
      toast(c.error + " — kept “" + plan.name + "”");
      return onRenamed();
    }
    done.current = true;
    onRenamed();
    if (c.name === plan.name) return;
    try {
      await savePlan({ ...plan, name: c.name });
      toast("Renamed to “" + c.name + "”");
    } catch (e) {
      toast("Renaming failed: " + errMsg(e), { error: true });
    }
  };

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
      {renaming ? (
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
              onRenamed();
            }
          }}
          onBlur={() => void commit(true)}
          onClick={(e) => e.stopPropagation()}
        />
      ) : (
        <span className="sky-stop-name">{plan.name}</span>
      )}
      <span className="sky-saved-dots" aria-hidden="true">
        {dots.map((d, i) => (
          <i key={i} className={d.perch ? "perch" : ""} />
        ))}
        {more > 0 && <span className="sky-saved-more">+{more}</span>}
      </span>
    </>
  );
  const how = onTackPlan ? "Click to add it to the focused line, or drag it onto a line or the sky" : "Drag onto a line or the sky";
  const props = {
    className: "sky-stop-chip sky-saved-chip" + (plan.builtin ? " builtin" : "") + (onTackPlan ? "" : " drag-only") + (renaming ? " is-renaming" : ""),
    "data-plan": plan.id,
    draggable: !renaming,
    title: plan.steps.map((s) => s.title).join(" → ") + "\n" + how + " — right-click for more",
    onDragStart: (e: DragEvent) => carry(e, savedDragData(plan)),
    onContextMenu: menuAt,
    onKeyDown: menuKey,
  };
  return (
    <span role="listitem" className="sky-stop-item">
      {onTackPlan && !renaming ? (
        <button type="button" {...props} aria-label={"Add “" + plan.name + "” to the focused line"} onClick={() => onTackPlan(plan)}>
          {body}
        </button>
      ) : (
        <span {...props}>{body}</span>
      )}
      {renaming && err && (
        <span className="sky-saved-err" role="alert">
          {err}
        </span>
      )}
    </span>
  );
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
}

export function StopShelf({ onTack, onTackPlan, onSession, folded = false, onFold }: StopShelfProps): JSX.Element {
  const kinds = useFlightKinds().data;
  const plans = useFlightPlans().data;
  // What sits on the shelf is the owner's pick (per device): the default
  // blocks to begin with, no multi-step block; the catalog's ★ adds, a chip's
  // "Take off the shelf" removes.
  const [shelf, setShelf] = useState<ShelfPick>(() => {
    try {
      return readShelf(localStorage.getItem(SHELF_KEY));
    } catch {
      return readShelf(null);
    }
  });
  const changeShelf = (next: (p: ShelfPick) => ShelfPick) =>
    setShelf((p) => {
      const n = next(p);
      try {
        localStorage.setItem(SHELF_KEY, JSON.stringify(n));
      } catch {
        /* a private window: the pick lives for the session */
      }
      return n;
    });
  const blocks = shelfKinds(shelf, kinds || []);
  const allSaved = savedStopOrder(plans || []);
  const saved = shelfPlans(shelf, allSaved);
  const [menu, setMenu] = useState<{ plan: FlightPlan; x: number; y: number } | null>(null);
  const [kindMenu, setKindMenu] = useState<{ def: StepKindDef; x: number; y: number } | null>(null);
  const [renaming, setRenaming] = useState("");
  const [takeoff, setTakeoff] = useState<FlightPlan | null>(null);
  const [deleting, setDeleting] = useState<FlightPlan | null>(null);
  const [busy, setBusy] = useState(false);
  const [catalog, setCatalog] = useState(false);

  /** The catalog's Add: through the same props as a chip's click. */
  const add = (d: CatalogDrop) => {
    setCatalog(false);
    if ("kind" in d) onTack?.(d.kind);
    else {
      const plan = allSaved.find((p) => p.id === d.planId);
      if (plan) onTackPlan?.(plan);
    }
  };

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

  return (
    <section className={"sky-shelf" + (folded ? " folded" : " open")} data-tour="sky-shelf" aria-label="The shelf of stops">
      <button
        type="button"
        className="sky-shelf-fold"
        aria-expanded={!folded}
        title={folded ? "Show the stops" : "Fold the stops away"}
        onClick={() => onFold?.(!folded)}
      >
        Stops {folded ? "▴" : "▾"}
      </button>
      {!folded && (
        <div className="sky-stops" role="group" aria-label="Stops">
          <span className="sky-stops-head">
            <strong>Stops</strong> <span className="sky-stops-hint">— drag onto the sky or a line</span>
          </span>
          {kinds !== undefined && !kinds?.length ? (
            <p className="sky-stops-empty muted">The stop list didn't load — this server may not know Flights yet.</p>
          ) : (
            <div className="sky-stops-chips">
              <div className="sky-stops-region" data-region="blocks" role="list" aria-label="Blocks">
                <span className="sky-stops-label" aria-hidden="true">
                  Blocks
                </span>
                <SessionChip onSession={onSession} />
                {blocks.map((k) => (
                  <StopChip key={k.kind} def={k} onTack={onTack} onMenu={(def, x, y) => setKindMenu({ def, x, y })} />
                ))}
              </div>
              {saved.length > 0 && (
                <>
                  <span className="sky-stops-divider" aria-hidden="true" />
                  <div className="sky-stops-region" data-region="multi" role="list" aria-label="Multi-step">
                    <span className="sky-stops-label" aria-hidden="true">
                      Multi-step
                    </span>
                    {saved.map((p) => (
                      <SavedChip
                        key={p.id}
                        plan={p}
                        plans={allSaved}
                        kinds={kinds || []}
                        renaming={renaming === p.id}
                        onTackPlan={onTackPlan}
                        onMenu={(plan, x, y) => setMenu({ plan, x, y })}
                        onRenamed={() => setRenaming("")}
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
                aria-haspopup="dialog"
                aria-expanded={catalog}
                title="Every block and every multi-step block, in a list you can filter"
                onClick={() => setCatalog(true)}
              >
                <span className="sky-stop-icon" aria-hidden="true">
                  ⊞
                </span>
                <span className="sky-stop-name">All blocks…</span>
              </button>
            </div>
          )}
        </div>
      )}
      {deleting && (
        <div className="sky-stops-confirm">
          <InlineConfirm
            title={"Delete “" + deleting.name + "”?"}
            body="It leaves the shelf. Stops already on the board stay."
            confirmLabel="Delete"
            busy={busy}
            onConfirm={() => void remove()}
            onCancel={() => setDeleting(null)}
          />
        </div>
      )}
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
      {catalog && (
        <StopCatalog
          kinds={kinds || []}
          plans={allSaved}
          onAdd={onTack || onTackPlan ? add : undefined}
          onClose={() => setCatalog(false)}
          shelf={(d) => onShelf(shelf, d)}
          onShelfToggle={(d) => changeShelf((p) => toggleShelf(p, d))}
        />
      )}
      {takeoff && <TakeoffDialog plan={takeoff} onClose={() => setTakeoff(null)} />}
    </section>
  );
}
