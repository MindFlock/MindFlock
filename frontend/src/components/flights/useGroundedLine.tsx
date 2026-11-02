/** A grounded flight's line, editable (docs/flights.md "Grounded flights"):
 * the stops that sit on the board with no session yet. It mirrors
 * useSessionLine — the trailing + and the + between two stops open the stop
 * palette, a stop opens in its dialog (✎, Enter, a double-click), Delete drops it at once (Undo in
 * the toast), a drag (or ⇤ / ⇥) moves it, ⧉ duplicates it, ⏸ makes it a
 * halt point, a shelf chip dropped on the line is tacked on at the gap it
 * lands in (a saved stop as one group) — with one difference: nothing has
 * flown, so EVERY stop can change. All of it goes through the same flight
 * routes a live line uses (`/api/flights/{id}/steps…`); the server takes
 * them on a grounded flight. */

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { toast } from "../../lib/toast";
import { optimistic, withSteps, withoutSteps } from "../../lib/flightOptimism";
import { newStep, type Flight, type FlightPlan, type FlightStep, type StepKindDef } from "../../lib/flights";
import { collapseGroups, drawnCanvas, findNode, firstStepOf, loopSlotAfter, loopStepTo, type DiagramNode } from "../../lib/flightDiagram";
import { errMsg } from "../../lib/format";
import { dropGroup, dropStop, moveStop, patchStop, patchStopGroup, savePlan, setHalt, tackOn, tackOnParallel, tackOnPlan, ungroupStops } from "../../lib/flightsApi";
import { undoPayload } from "../../lib/stopEditing";
import { groundedDiagram } from "../../lib/groundedLines";
import type { FlywayCanvasProps } from "./FlywayCanvas";
import { StopDialog } from "./StopDialog";
import { StopPalette } from "./StopParts";
import { inspectorDef, orderedPlans, shelfPlanFor } from "./useSessionLine";

/** What a chip dropped on a line carries (the canvas's `onDropStop`): a stop
 * kind, or a saved stop's id. A bare string is the older canvas's kind. */
export type StopDrop = { kind: string } | { planId: string };

/** A drop, whichever shape the canvas hands it in. */
export function readDrop(drop: StopDrop | string): StopDrop {
  return typeof drop === "string" ? { kind: drop } : drop;
}

export function useGroundedLine(flight: Flight, src: { kinds: readonly StepKindDef[]; plans: readonly FlightPlan[] }) {
  const { kinds, plans } = src;
  const [selected, setSelected] = useState<string | null>(null);
  const [paletteAt, setPaletteAt] = useState<number | null>(null);
  // Round 18: the palette from a wire dropped on empty sky — the node it
  // hangs off and the spot the new stop takes (chain-local).
  const [paletteHang, setPaletteHang] = useState<{ node: string | null; pos: { x: number; y: number } | null; withStep?: string } | null>(null);
  // The open dialog (a node id), and the group's to go back to from a sub-step.
  const [dialog, setDialog] = useState<{ id: string; back?: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [selectIndex, setSelectIndex] = useState<number | null>(null);
  // Groups shown open (▸ / ▾), by group id.
  const [expandedGroups, setExpandedGroups] = useState<ReadonlySet<string>>(() => new Set());

  const diagram = useMemo(() => collapseGroups(groundedDiagram(flight, kinds), expandedGroups), [flight, kinds, expandedGroups]);
  const kindOf = useMemo(() => new Map(kinds.map((k) => [k.kind, k])), [kinds]);
  const stops = diagram.nodes.filter((n) => n.kind === "stop");
  const end = flight.steps.length;
  // A Loop has no box: it is found on its return wire.
  const find = (id: string | null | undefined): DiagramNode | null => (id ? diagram.nodes.find((n) => n.id === id) || findNode(diagram, id) : null);
  const sel = find(selected);
  const open = dialog ? find(dialog.id) : null;
  const latest = useRef(flight);
  latest.current = flight;

  // A stop just tacked on opens in its dialog once the flight has it.
  useEffect(() => {
    if (selectIndex === null) return;
    const n = stops.find((s) => s.index === selectIndex && s.step);
    if (!n) return;
    setSelected(n.id);
    setDialog({ id: n.id });
    setSelectIndex(null);
  }, [selectIndex, stops]);

  // A box that left the line takes its dialog (and its selection) with it.
  useEffect(() => {
    if (selected && !sel) setSelected(null);
    if (dialog && !open) setDialog(null);
  }, [selected, sel, dialog, open]);

  const where = (at: number | null) => (at !== null && at < end ? at : undefined);

  const tackOne = async (def: StepKindDef, at: number | null) => {
    const pos = paletteHang?.pos;
    const withStep = paletteHang?.withStep;
    setBusy(true);
    setPaletteAt(null);
    setPaletteHang(null);
    try {
      const fresh = newStep(def, flight.steps.map((s) => s.id));
      const step = pos ? { ...fresh, pos } : fresh;
      // Shown at once; the server's flight replaces the guess (lib/flightOptimism).
      // In parallel with a stop (round 20): into its group, else after `at`.
      const go = withStep ? () => tackOnParallel(flight.id, step, withStep) : () => tackOn(flight.id, step, where(at));
      if (await optimistic(flight.id, withSteps([step], where(at)), go)) setSelectIndex(where(at) ?? end);
    } finally {
      setBusy(false);
    }
  };

  const tackPlan = async (plan: Pick<FlightPlan, "id" | "name">, at: number | null) => {
    setBusy(true);
    setPaletteAt(null);
    try {
      const steps = plans.find((p) => p.id === plan.id)?.steps;
      const go = () => tackOnPlan(flight.id, plan.id, where(at));
      if (await (steps?.length ? optimistic(flight.id, withSteps(steps, where(at)), go) : go())) toast("Added “" + plan.name + "”");
    } finally {
      setBusy(false);
    }
  };

  const removeStop = async (step: FlightStep, index: number): Promise<boolean> => {
    const id = flight.id;
    // Gone at once; a refusal puts it back (lib/flightOptimism).
    if (!(await optimistic(id, withoutSteps(new Set([step.id])), () => dropStop(id, step.id)))) return false;
    toast("Removed “" + step.title + "”", {
      action: {
        label: "Undo",
        ariaLabel: "Undo: put “" + step.title + "” back",
        onClick: () => {
          const now = latest.current?.id === id ? latest.current : null;
          const back = undoPayload(step, index, now);
          void tackOn(id, back.step, back.at);
        },
      },
    });
    return true;
  };

  /** A whole group off the line (every sub-step, one toast). */
  const removeGroup = async (n: DiagramNode): Promise<boolean> => {
    const first = n.group?.steps[0];
    if (!first) return false;
    const ids = new Set((n.group?.steps || []).map((s) => s.id));
    if (!(await optimistic(flight.id, withoutSteps(ids), () => dropGroup(flight.id, first.id)))) return false;
    toast("Removed “" + (n.group?.name || n.title) + "” — its " + (n.group?.steps.length || 0) + " stops");
    return true;
  };

  const removeNode = (n: DiagramNode): Promise<boolean> => {
    if (n.kind === "group") return removeGroup(n);
    return n.step && n.index !== undefined ? removeStop(n.step, n.index) : Promise.resolve(false);
  };

  const onSelect = (n: DiagramNode) => {
    setPaletteAt(null);
    setSelected(selected === n.id ? null : n.id);
  };

  /** ✎, Enter, a double-click, the menu's Edit: the box's dialog. */
  const onEdit = (n: DiagramNode) => {
    setPaletteAt(null);
    setSelected(n.id);
    setDialog({ id: n.id });
  };

  const toggleGroup = (gid: string) =>
    setExpandedGroups((g) => {
      const next = new Set(g);
      if (next.has(gid)) next.delete(gid);
      else next.add(gid);
      return next;
    });

  /** The trailing +'s palette, open or shut (the ⋯'s "Add a stop…"). */
  const togglePalette = (open?: boolean) => {
    setSelected(null);
    setDialog(null);
    setPaletteHang(null);
    const shown = paletteAt !== null && paletteAt >= end;
    setPaletteAt(open ?? !shown ? end : null);
  };

  /** Round 18: the palette for a stop right after `after` (null: the start),
   * drawn at `pos` (chain-local) when given — a wire dropped on empty sky. */
  const paletteAfter = (after: DiagramNode | null, pos?: { x: number; y: number } | null) => {
    setSelected(null);
    setDialog(null);
    const slot = after ? loopSlotAfter(after) : 0;
    setPaletteAt(slot === undefined ? end : Math.min(slot, end));
    setPaletteHang({ node: after ? after.id : null, pos: pos ? { x: Math.round(pos.x), y: Math.round(pos.y) } : null });
  };


  /** Round 20: the palette for a stop that runs side by side with `withNode`
   * (its parallel group, made when it has none); the pick goes through
   * `tackOnParallel`. */
  const paletteParallel = (withNode: DiagramNode, pos?: { x: number; y: number } | null) => {
    const first = firstStepOf(withNode);
    if (!first) return paletteAfter(withNode, pos);
    paletteAfter(withNode, pos);
    setPaletteHang({ node: withNode.id, pos: pos ? { x: Math.round(pos.x), y: Math.round(pos.y) } : null, withStep: first.id });
  };

  const ordered = orderedPlans(plans);
  const savedChips = (
    <div className="fl-palette-flyways">
      <div className="fl-palette-head">A saved stop — all of its steps</div>
      <div className="fl-chiprow">
        {ordered.map((p) => (
          <button key={p.id} type="button" className="fl-pick" disabled={busy} title={p.steps.map((s) => s.title).join(" → ")} onClick={() => void tackPlan(p, paletteAt)}>
            {p.name}
          </button>
        ))}
      </div>
    </div>
  );

  const raw: Pick<
    FlywayCanvasProps,
    "selected" | "onSelect" | "onEdit" | "onOpen" | "onInsert" | "onRemove" | "onMove" | "onDuplicate" | "onToggleHalt" | "onToggleGroup" | "onDropStop" | "onLoopFrom" | "onLoopTo"
  > = {
    selected: paletteAt !== null && paletteAt >= end ? "add" : dialog?.id || selected,
    onSelect,
    onEdit,
    onOpen: (n) => {
      if (n.kind === "add") togglePalette();
      else if (n.kind === "group" && n.group) toggleGroup(n.group.id);
    },
    onInsert: (at) => setPaletteAt(paletteAt === at ? null : at),
    onRemove: (n) => void removeNode(n),
    onToggleGroup: toggleGroup,
    onMove: (from, to) => {
      const step = flight.steps[from];
      if (step && to >= 0 && to < flight.steps.length && to !== from) void moveStop(flight.id, step.id, to);
    },
    onDuplicate: (n) => {
      if (!n.step || n.index === undefined) return;
      void tackOn(flight.id, n.step, where(n.index + 1));
    },
    onToggleHalt: (n) => {
      const step = n.kind === "group" ? n.group?.steps[0] : n.index !== undefined ? flight.steps[n.index] : undefined;
      if (step) void setHalt(flight.id, step.id, !step.halt);
    },
    // A loop drawn by hand: a Loop right after `from`, back to `to`.
    onLoopFrom: (from, to) => {
      const def = kindOf.get("loop");
      const target = firstStepOf(to);
      const at = loopSlotAfter(from);
      if (!def || !target || at === undefined) return void toast("This server has no Loop stop");
      void tackOn(flight.id, loopStepTo(def, flight.steps.map((s) => s.id), target), where(at)).then((ok) => ok && toast("Loops back to “" + target.title + "”"));
    },
    // A Loop's pill dropped on another stop: it repeats from there.
    onLoopTo: (loop, to) => {
      const target = firstStepOf(to);
      if (!loop.step || !target || loop.step.config?.back_to === target.id) return;
      void patchStop(flight.id, loop.step.id, { config: { ...loop.step.config, back_to: target.id } }).then((ok) => ok && toast("Loops back to “" + target.title + "” now"));
    },
    // A stop kind, or a saved stop (as one group) — the canvas's new drop
    // shape; a bare kind from the older one reads the same.
    onDropStop: (drop: StopDrop | string, at: number | null) => {
      const d = readDrop(drop);
      if ("planId" in d) {
        const plan = plans.find((p) => p.id === d.planId);
        void tackPlan(plan || { id: d.planId, name: "the saved stop" }, at);
        return;
      }
      const def = kindOf.get(d.kind);
      if (def) void tackOne(def, at);
      else toast("There is no “" + d.kind + "” stop to add");
    },
  };
  // Collapsed groups make the drawn places differ from the flight's indexes:
  // the canvas speaks drawn places, the handlers above the flight's own.
  const shown = drawnCanvas(diagram, raw);

  const palette: ReactNode =
    paletteAt !== null ? (
      <StopPalette
        kinds={kinds}
        title={paletteAt >= end ? "Add a stop" : "Add a stop before “" + (flight.steps[paletteAt]?.title || "") + "”"}
        onPick={(k) => void tackOne(k, paletteAt)}
        onClose={() => {
          setPaletteAt(null);
          setPaletteHang(null);
        }}
        extra={ordered.length > 0 ? savedChips : undefined}
      />
    ) : null;

  const close = () => setDialog(dialog?.back && find(dialog.back) ? { id: dialog.back } : null);
  const act = async (f: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await f();
    } finally {
      setBusy(false);
    }
  };

  let inspector: ReactNode = null;
  if (open && open.kind === "group" && open.group) {
    const g = open.group;
    inspector = (
      <StopDialog
        key={open.id}
        node={open}
        kindOf={kindOf}
        busy={busy}
        onClose={close}
        onRename={(name) =>
          act(async () => {
            if (g.steps[0] && (await patchStopGroup(flight.id, g.steps[0].id, { name }))) setDialog(null);
          })
        }
        onOpenSub={(sub) => setDialog({ id: sub.id, back: open.id })}
        onRemoveSub={(sub) => void act(() => removeNode(sub))}
        onUngroup={() =>
          void act(async () => {
            if (await ungroupStops(flight.id, g.id)) setDialog(null);
          })
        }
        onSaveShelf={(name) =>
          act(async () => {
            const plan = shelfPlanFor(name, g.steps, g.plan_id, plans);
            try {
              const saved = await savePlan(plan);
              toast((plan.id ? "Updated “" : "Saved “") + (saved?.name || name) + "” on the shelf");
            } catch (err) {
              toast("Saving to the shelf failed: " + errMsg(err), { error: true });
            }
          })
        }
        onRemove={() =>
          void act(async () => {
            if (await removeGroup(open)) setDialog(null);
          })
        }
      />
    );
  } else if (open && open.kind === "stop" && open.step) {
    const step = open.step;
    inspector = (
      <StopDialog
        key={open.id}
        node={open}
        def={inspectorDef(kindOf.get(step.kind), step)}
        lineSteps={flight.steps}
        busy={busy}
        onClose={close}
        onSave={(patch) =>
          act(async () => {
            if (await patchStop(flight.id, step.id, patch)) close();
          })
        }
        onDuplicate={() => {
          const at = open.index! + 1;
          void tackOn(flight.id, step, at < end ? at : undefined);
          close();
        }}
        onRemove={() =>
          void act(async () => {
            if (await removeNode(open)) close();
          })
        }
      />
    );
  }

  return {
    diagram: shown.diagram,
    busy,
    togglePalette,
    expandedGroups,
    toggleGroup,
    editing: palette !== null || inspector !== null,
    canvas: shown.canvas,
    /** Round 18: the line in the flight's indexes and its handlers (the
     * Sky's chain speaks them). */
    graphDiagram: diagram,
    graphCanvas: raw,
    paletteAfter,
    paletteParallel,
    /** Where the open palette / dialog hangs (a node id, "" = the end, null =
     * nothing open) and a spot asked for (chain-local). */
    anchor: inspector !== null && dialog ? dialog.id : palette !== null ? (paletteHang?.node ?? "") : null,
    anchorPos: palette !== null ? paletteHang?.pos ?? null : null,
    palette,
    /** The open stop's dialog (StopDialog), or null. */
    inspector,
  };
}
