/** One flyway on the shelf, AS its stops: a row with its name (click to
 * rename it in place), its stops drawn on `FlywayCanvas` (compact, the head
 * of each box alone), **Fly** and a ⋯ (Edit details…, Duplicate flyway, Reset
 * to default on a built-in, Delete flyway on one of yours).
 *
 * The row is built by hand on the canvas: a stop chip dropped on it lands in
 * the gap it lights (`onDropStop`), the hover toolbar / right-click menu /
 * Delete take a stop off (Undo in the toast puts it back), ⇤ ⇥ or a drag
 * reorder, ⧉ duplicates, the + between boxes and at the end open the stop
 * palette. Every change SAVES at once (`savePlan`), optimistic: the row
 * shows it before the server answers, a small "Saved" tick fades in after,
 * and a refusal reverts the row with the server's sentence in a toast. A
 * built-in saves as an override (Reset puts it back).
 *
 * The name (and the grip before it) drags the whole flyway onto a line: it
 * carries the flyway's id under `FLYWAY_MIME`, and the Sky owns the drop.
 *
 * `EmptyFlywayRow` is the shelf's last row: "Drop stops here to start a
 * flyway" — the first stop dropped (or picked) makes one. */

import { useEffect, useMemo, useRef, useState, type DragEvent, type JSX } from "react";
import { OverflowMenu, type MenuItem } from "../OverflowMenu";
import { useFlightKinds } from "../../state/queries";
import { errMsg } from "../../lib/format";
import { toast } from "../../lib/toast";
import { duplicateStep, moveStep, STOP_MIME, type FlightPlan, type StepKindDef } from "../../lib/flights";
import { flywayDiagram, type DiagramNode } from "../../lib/flightDiagram";
import { resetPlan, savePlan } from "../../lib/flightsApi";
import { isStopDrag } from "../../lib/stopEditing";
import { canRemoveStop, checkRename, insertStop, restoreStep } from "../../lib/flywayEdit";
import { FlywayCanvas, type FlywayCanvasProps } from "./FlywayCanvas";
import { StopPalette } from "./StopParts";
import "./FlywayRow.css";

/** The drag type a flyway carries (its value: the flyway's id). */
export const FLYWAY_MIME = "application/x-mindflock-flyway";

/** What a flyway's drag carries: its id under FLYWAY_MIME, and its name as
 * plain text for anything else it is dropped on. */
export function flywayDragData(plan: Pick<FlightPlan, "id" | "name">): Array<[string, string]> {
  return [
    [FLYWAY_MIME, plan.id],
    ["text/plain", plan.name],
  ];
}

function onFlywayDrag(e: DragEvent, plan: FlightPlan) {
  for (const [type, value] of flywayDragData(plan)) e.dataTransfer.setData(type, value);
  e.dataTransfer.effectAllowed = "copy";
}

/** A row's boxes: the head alone, so one short line each (the + a dot). */
export const rowBox: NonNullable<FlywayCanvasProps["sizeOf"]> = (n, s) => (n.kind === "stop" ? { w: 128, h: 40 } : n.kind === "add" ? { w: 34, h: 34 } : s);

export interface FlywayRowProps {
  plan: FlightPlan;
  /** Every flyway on the shelf (a rename may not take another's name). */
  plans: readonly FlightPlan[];
  onFly(plan: FlightPlan): void;
  /** Edit details… (the FlywayCard), or ✎ on a stop: that stop selected. */
  onEdit(plan: FlightPlan, stop?: number): void;
  /** Delete flyway (one of yours; the shelf hides it and offers Undo). */
  onDelete(plan: FlightPlan): void;
  onCopy(plan: FlightPlan): void;
  /** The name of the stop chip being dragged, so the end + can say it. */
  dragStop?: string | null;
  /** A new flyway: open with the name field focused. */
  naming?: boolean;
  onNamed?(): void;
}

export function FlywayRow({ plan, plans, onFly, onEdit, onDelete, onCopy, dragStop, naming, onNamed }: FlywayRowProps): JSX.Element {
  const kinds = useFlightKinds().data || [];
  const kindOf = useMemo(() => new Map(kinds.map((k) => [k.kind, k])), [kinds]);
  // What the row shows ahead of the server (null: the shelf's own copy).
  const [local, setLocal] = useState<FlightPlan | null>(null);
  const shown = local || plan;
  const cur = useRef(shown);
  cur.current = shown;
  const confirmed = useRef(plan);
  const queue = useRef<Promise<void>>(Promise.resolve());
  const pending = useRef(0);
  const gen = useRef(0);
  const [tick, setTick] = useState(0);
  const [palette, setPalette] = useState<number | null>(null);
  const [renaming, setRenaming] = useState(!!naming);
  const [name, setName] = useState(plan.name);
  const [nameError, setNameError] = useState("");
  const nameRef = useRef<HTMLInputElement>(null);
  // Enter / Esc close the field, and its blur on the way out must not
  // save a second time.
  const nameOpen = useRef(!!naming);

  // The shelf caught up (a refetch after our saves, or someone else's):
  // drop the optimistic copy once nothing of ours is still on its way.
  useEffect(() => {
    if (pending.current === 0) {
      confirmed.current = plan;
      setLocal(null);
    }
  }, [plan]);

  useEffect(() => {
    if (!naming) return;
    nameOpen.current = true;
    setRenaming(true);
    setName(cur.current.name);
    onNamed?.();
  }, [naming]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!renaming) return;
    nameRef.current?.focus();
    nameRef.current?.select();
  }, [renaming]);

  /** Show `next` now and save it; saves go one after another, and a refused
   * one reverts the row and drops the changes queued behind it. */
  const commit = (next: FlightPlan) => {
    setLocal(next);
    cur.current = next;
    pending.current++;
    const mine = gen.current;
    queue.current = queue.current.then(async () => {
      try {
        if (mine !== gen.current) return;
        const saved = await savePlan(next);
        confirmed.current = saved;
        if (cur.current === next) {
          cur.current = saved;
          setLocal(saved);
        }
        setTick((t) => t + 1);
      } catch (err) {
        gen.current++;
        cur.current = confirmed.current;
        setLocal(confirmed.current);
        toast("Couldn't save “" + next.name + "”: " + errMsg(err), { error: true });
      } finally {
        pending.current--;
      }
    });
  };
  const setSteps = (steps: FlightPlan["steps"]) => commit({ ...cur.current, steps });

  const idx = (n: DiagramNode) => (n.index === undefined ? -1 : n.index);
  const insert = (def: StepKindDef | undefined, at: number | null) => {
    if (!def) return;
    setSteps(insertStop(cur.current.steps, def, at));
    setPalette(null);
  };
  const remove = (i: number) => {
    const steps = cur.current.steps;
    const step = steps[i];
    if (!step) return;
    if (!canRemoveStop(steps)) {
      toast("A flyway keeps one stop at least — " + (cur.current.builtin ? "Reset it from its ⋯" : "delete the flyway from its ⋯ instead"));
      return;
    }
    setSteps(steps.filter((_, j) => j !== i));
    toast("Removed “" + step.title + "” from “" + cur.current.name + "”", {
      action: { label: "Undo", onClick: () => setSteps(restoreStep(cur.current.steps, step, i)) },
    });
  };

  const startRename = () => {
    nameOpen.current = true;
    setName(shown.name);
    setNameError("");
    setRenaming(true);
  };
  /** Enter / blur save, Esc cancels. A refused name keeps the field open
   * on Enter; leaving it (blur) puts the old name back and says why. */
  const finishRename = (save: boolean, left = false) => {
    if (!nameOpen.current) return;
    if (!save) {
      nameOpen.current = false;
      setRenaming(false);
      setNameError("");
      return;
    }
    const r = checkRename(name, plans, shown.id);
    if (!r.ok) {
      if (left) {
        finishRename(false);
        toast(r.error + " — kept “" + shown.name + "”");
      } else setNameError(r.error);
      return;
    }
    nameOpen.current = false;
    setRenaming(false);
    setNameError("");
    if (r.name !== shown.name) commit({ ...cur.current, name: r.name });
  };

  const reset = async () => {
    try {
      const p = await resetPlan(shown.id);
      gen.current++;
      confirmed.current = p;
      cur.current = p;
      setLocal(p);
      toast("“" + p.name + "” is back to its default");
    } catch (err) {
      toast("Couldn't reset “" + shown.name + "”: " + errMsg(err), { error: true });
    }
  };

  const menu: MenuItem[] = [
    { key: "edit", label: "Edit details…", title: "Its trigger, schedule, session and description", onSelect: () => onEdit(shown) },
    { key: "copy", label: "Duplicate flyway", onSelect: () => onCopy(shown) },
    shown.builtin
      ? { key: "reset", label: "Reset to default", title: "Put this built-in flyway back the way MindFlock ships it", onSelect: () => void reset() }
      : { key: "delete", label: "Delete flyway", danger: true, onSelect: () => onDelete(shown) },
  ];

  const diagram = useMemo(() => flywayDiagram(shown.steps, kinds), [shown.steps, kinds]);
  const quoted = "“" + shown.name + "”";
  return (
    <div role="listitem" className={"sky-flyway-row" + (shown.builtin ? " builtin" : "")} data-plan={shown.id}>
      <div
        className="sky-flyway-row-head"
        draggable={!renaming}
        title={"Drag onto a line to fly " + quoted + " on it"}
        onDragStart={(e) => onFlywayDrag(e, shown)}
      >
        <span className="sky-flyway-grip" aria-hidden="true">
          ⠿
        </span>
        {renaming ? (
          <input
            ref={nameRef}
            type="text"
            className="fl-input sky-flyway-rename"
            aria-label="Flyway name"
            aria-invalid={nameError ? true : undefined}
            value={name}
            maxLength={140}
            onChange={(e) => {
              setName(e.target.value);
              setNameError("");
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                finishRename(true);
              } else if (e.key === "Escape") {
                e.preventDefault();
                e.stopPropagation();
                finishRename(false);
              }
            }}
            onBlur={() => finishRename(true, true)}
          />
        ) : (
          <button type="button" className="sky-flyway-name" aria-label={"Rename the flyway " + quoted} title={"Click to rename · drag onto a line to fly it"} onClick={startRename}>
            {shown.name}
          </button>
        )}
        {shown.builtin && <span className="sky-flyway-tag">built-in</span>}
        {tick > 0 && (
          <span key={tick} className="sky-flyway-saved" role="status">
            ✓ Saved
          </span>
        )}
        {nameError && (
          <span className="sky-flyway-name-error" role="alert">
            {nameError}
          </span>
        )}
      </div>
      <FlywayCanvas
        compact
        vertical={false}
        lod="far"
        sizeOf={rowBox}
        className="sky-flyway-canvas"
        diagram={diagram}
        label={quoted + "'s stops"}
        dragStop={dragStop}
        onSelect={(n) => onEdit(shown, idx(n))}
        onOpen={(n) => {
          if (n.kind === "add") setPalette(palette === shown.steps.length ? null : shown.steps.length);
        }}
        onMove={(from, to) => {
          if (to >= 0 && to < cur.current.steps.length) setSteps(moveStep(cur.current.steps, from, to));
        }}
        onRemove={(n) => remove(idx(n))}
        onDuplicate={(n) => setSteps(duplicateStep(cur.current.steps, idx(n)))}
        onInsert={(at) => setPalette(palette === at ? null : at)}
        onDropStop={(kind, at) => insert(kindOf.get(kind), at)}
      />
      <div className="sky-flyway-row-acts">
        <button type="button" className="sky-flyway-fly" aria-label={"Fly " + quoted + " on a line"} title="Fly it on a line" onClick={() => onFly(shown)}>
          Fly
        </button>
        <OverflowMenu label={"More for " + quoted} items={menu} />
      </div>
      {palette !== null && (
        <div className="sky-flyway-palette">
          <StopPalette
            kinds={kinds}
            title={palette >= shown.steps.length ? "Add a stop at the end of " + quoted : "Add a stop before “" + (shown.steps[palette]?.title || "") + "”"}
            onPick={(k) => insert(k, palette)}
            onClose={() => setPalette(null)}
          />
        </div>
      )}
    </div>
  );
}

/** The shelf's last row: drop a stop here (or pick one) and a flyway starts
 * with it. `naming` (+ New flyway) puts a name field in it first. */
export function EmptyFlywayRow({
  dragStop,
  naming,
  busy,
  onCreate,
  onEditor,
  onCancelName,
}: {
  dragStop?: string | null;
  /** + New flyway was pressed: name it first (bumped to focus again). */
  naming: number;
  busy: boolean;
  onCreate(kind: string, name: string): void;
  /** The full editor (a fresh FlywayCard, with Describe). */
  onEditor(): void;
  onCancelName(): void;
}): JSX.Element {
  const kinds = useFlightKinds().data || [];
  const [over, setOver] = useState(false);
  const [palette, setPalette] = useState(false);
  const [name, setName] = useState("");
  const nameRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (naming) nameRef.current?.focus();
  }, [naming]);

  const onDragOver = (e: DragEvent<HTMLDivElement>) => {
    if (!isStopDrag(e.dataTransfer?.types)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
    if (!over) setOver(true);
  };
  const onDrop = (e: DragEvent<HTMLDivElement>) => {
    if (!isStopDrag(e.dataTransfer?.types)) return;
    e.preventDefault();
    e.stopPropagation();
    setOver(false);
    const kind = e.dataTransfer.getData(STOP_MIME);
    if (kind) onCreate(kind, name);
  };
  const said = busy ? "Starting the flyway…" : over ? "Drop to start a flyway with " + (dragStop || "this stop") : "Drop stops here to start a flyway";
  return (
    <div
      role="listitem"
      className={"sky-flyway-row sky-flyway-empty" + (over ? " over" : "")}
      onDragOver={onDragOver}
      onDragLeave={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setOver(false);
      }}
      onDrop={onDrop}
    >
      <div className="sky-flyway-row-head">
        <span className="sky-flyway-grip" aria-hidden="true">
          +
        </span>
        {naming > 0 ? (
          <input
            ref={nameRef}
            type="text"
            className="fl-input sky-flyway-rename"
            aria-label="New flyway's name"
            placeholder="Name the new flyway"
            value={name}
            maxLength={140}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Escape") {
                e.preventDefault();
                e.stopPropagation();
                setName("");
                onCancelName();
              } else if (e.key === "Enter") {
                e.preventDefault();
                setPalette(true);
              }
            }}
          />
        ) : (
          <span className="sky-flyway-empty-name">New flyway</span>
        )}
      </div>
      <p className="sky-flyway-empty-text" aria-live="polite">
        {said}
      </p>
      <div className="sky-flyway-row-acts">
        <button type="button" className="sky-flyway-pick" disabled={busy} onClick={() => setPalette(!palette)}>
          Pick a stop
        </button>
        <button type="button" className="sky-flyway-pick" title="The full editor: its trigger, session, and Describe" onClick={onEditor}>
          Editor…
        </button>
      </div>
      {palette && (
        <div className="sky-flyway-palette">
          <StopPalette
            kinds={kinds}
            title="The new flyway's first stop"
            onPick={(k) => {
              setPalette(false);
              onCreate(k.kind, name);
            }}
            onClose={() => setPalette(false)}
          />
        </div>
      )}
    </div>
  );
}
