/** The flyway shelf at the foot of the Sky. A flyway is a saved route of
 * stops; here each one IS its stops — a row (`FlywayRow`): its name (click
 * to rename), its stops as boxes you build by dragging, **Fly** and a ⋯.
 * Built-ins first, then yours, then the empty row: "Drop stops here to start
 * a flyway". **+ New flyway** names that empty row first; its **Editor…**
 * opens a fresh card (`onNew`). DRAG a row's name onto a line to fly it (it
 * carries the flyway's id as `application/x-mindflock-flyway`; the host owns
 * the drop), or press its Fly (`onFly` — the host asks on which line). Its ⋯
 * → Edit details… opens the card (`onEdit`). Every change on a row saves at
 * once.
 *
 * Under the rows, the STOPS row (`StopShelf`): a chip per stop kind to drag
 * onto a line or into a flyway, or click to tack onto the focused line
 * (`onTack`). The shelf folds to its head; the host remembers that
 * (`collapsed` / `onCollapse`), else the shelf keeps it itself. It is at most
 * ~40% of the window tall, the rows scrolling inside it. */

import { useEffect, useMemo, useState, type DragEvent, type JSX } from "react";
import { STOP_MIME, type FlightPlan } from "../../lib/flights";
import { deletePlan, savePlan } from "../../lib/flightsApi";
import { errMsg } from "../../lib/format";
import { toast } from "../../lib/toast";
import { isStopDrag } from "../../lib/stopEditing";
import { copyOfFlyway, flywayFromDrop, rowOrder, undeleted } from "../../lib/flywayEdit";
import { useFlightKinds } from "../../state/queries";
import { EmptyFlywayRow, FlywayRow } from "./FlywayRow";
import { StopShelf } from "./StopShelf";
import "./FlywayShelf.css";

export { FLYWAY_MIME, flywayDragData } from "./FlywayRow";

/** The shelf's order: built-ins first, then yours, each as listed. */
export const shelfOrder = (plans: readonly FlightPlan[]): FlightPlan[] => rowOrder(plans);

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
  /** The full editor on a fresh flyway (the empty row's Editor…). */
  onNew(): void;
  onFly(plan: FlightPlan, title?: string): void;
  /** Edit details… — or ✎ on a stop, with that stop's index. */
  onEdit(plan: FlightPlan, stop?: number): void;
  /** Folded to its head (the host remembers it); uncontrolled when absent. */
  collapsed?: boolean;
  onCollapse?(collapsed: boolean): void;
  /** A stop chip clicked: tack that kind onto the focused line. Absent, the
   * stop chips only drag. */
  onTack?(kind: string): void;
}): JSX.Element {
  const kinds = useFlightKinds().data || [];
  const [ownFold, setOwnFold] = useState(false);
  const folded = collapsed ?? ownFold;
  const fold = (v: boolean) => {
    setOwnFold(v);
    onCollapse?.(v);
  };
  // Made here and not on the shelf's list yet (the refetch is on its way);
  // deleted here and maybe still on it.
  const [fresh, setFresh] = useState<FlightPlan[]>([]);
  const [gone, setGone] = useState<ReadonlySet<string>>(new Set());
  // A row whose name field opens focused (a flyway just made or copied).
  const [nameId, setNameId] = useState("");
  // + New flyway: the empty row asks for a name (bumped to focus again).
  const [naming, setNaming] = useState(0);
  const [creating, setCreating] = useState(false);
  // The stop chip being dragged, so a row's end + can name it.
  const [dragStop, setDragStop] = useState<string | null>(null);

  useEffect(() => {
    const ids = new Set(plans.map((p) => p.id));
    setFresh((f) => (f.some((p) => ids.has(p.id)) ? f.filter((p) => !ids.has(p.id)) : f));
  }, [plans]);

  const rows = useMemo(() => {
    const ids = new Set(plans.map((p) => p.id));
    return rowOrder([...plans, ...fresh.filter((p) => !ids.has(p.id))]).filter((p) => !gone.has(p.id));
  }, [plans, fresh, gone]);

  const made = (p: FlightPlan, name: boolean) => {
    setFresh((f) => [...f.filter((x) => x.id !== p.id), p]);
    if (name) setNameId(p.id);
  };

  /** A stop dropped on (or picked in) the empty row: a flyway with it. */
  const create = async (kind: string, typed: string) => {
    const plan = flywayFromDrop(kind, kinds, rows, typed);
    if (!plan || creating) return;
    setCreating(true);
    try {
      const saved = await savePlan(plan);
      setNaming(0);
      made(saved, !typed.trim());
    } catch (err) {
      toast("Couldn't start the flyway: " + errMsg(err), { error: true });
    } finally {
      setCreating(false);
    }
  };

  const copy = async (p: FlightPlan) => {
    try {
      made(await savePlan(copyOfFlyway(p, rows)), true);
    } catch (err) {
      toast("Couldn't copy “" + p.name + "”: " + errMsg(err), { error: true });
    }
  };

  /** One of yours, off the shelf at once; Undo saves it back (a new id). */
  const remove = async (p: FlightPlan) => {
    setGone((g) => new Set([...g, p.id]));
    try {
      await deletePlan(p.id);
      toast("Deleted the flyway “" + p.name + "”", {
        action: {
          label: "Undo",
          onClick: () => {
            savePlan(undeleted(p)).then(
              (back) => made(back, false),
              (err) => toast("Couldn't bring back “" + p.name + "”: " + errMsg(err), { error: true })
            );
          },
        },
      });
    } catch (err) {
      setGone((g) => new Set([...g].filter((id) => id !== p.id)));
      toast("Couldn't delete “" + p.name + "”: " + errMsg(err), { error: true });
    }
  };

  // A stop chip picked up on the shelf: remember its name for the rows.
  const onDragStart = (e: DragEvent) => {
    const types = e.dataTransfer?.types;
    if (!isStopDrag(types)) return;
    let name = "";
    try {
      name = e.dataTransfer.getData("text/plain") || e.dataTransfer.getData(STOP_MIME);
    } catch {
      name = "";
    }
    setDragStop(name || null);
  };

  return (
    <section
      className={"sky-shelf" + (folded ? " folded" : "")}
      aria-label="Flyways"
      data-tour="sky-shelf"
      onDragStart={onDragStart}
      onDragEnd={() => setDragStop(null)}
      onDrop={() => setDragStop(null)}
    >
      <header className="sky-shelf-top">
        <button type="button" className="sky-shelf-head" aria-expanded={!folded} title={folded ? "Show the flyways" : "Fold the flyways away"} onClick={() => fold(!folded)}>
          <span className="sky-shelf-caret" aria-hidden="true">
            {folded ? "▸" : "▾"}
          </span>
          Flyways <span className="sky-shelf-count">{rows.length}</span>
        </button>
        {!folded && (
          <>
            <p className="sky-shelf-explain">
              <strong>Flyways</strong> — saved routes of stops. Drag one onto a line to fly it; drop stops into it to build it.
            </p>
            <button
              type="button"
              className="sky-shelf-new"
              onClick={() => {
                setNameId("");
                setNaming((n) => n + 1);
              }}
            >
              + New flyway
            </button>
          </>
        )}
      </header>
      {!folded && (
        <div className="sky-flyway-rows sky-own-scroll" role="list">
          {rows.map((p) => (
            <FlywayRow
              key={p.id}
              plan={p}
              plans={rows}
              onFly={onFly}
              onEdit={onEdit}
              onDelete={(x) => void remove(x)}
              onCopy={(x) => void copy(x)}
              dragStop={dragStop}
              naming={nameId === p.id}
              onNamed={() => setNameId("")}
            />
          ))}
          <EmptyFlywayRow dragStop={dragStop} naming={naming} busy={creating} onCreate={(k, n) => void create(k, n)} onEditor={onNew} onCancelName={() => setNaming(0)} />
        </div>
      )}
      {!folded && <StopShelf onTack={onTack} />}
    </section>
  );
}
