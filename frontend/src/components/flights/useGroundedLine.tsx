/** A grounded flight's line, editable (docs/flights.md "Grounded flights"):
 * the stops that sit on the board with no session yet. It mirrors
 * useSessionLine — the trailing + and the + between two stops open the stop
 * palette, a stop opens in the inspector, Delete drops it at once (Undo in
 * the toast), a drag (or ⇤ / ⇥) moves it, ⧉ duplicates it, ⏸ makes it a
 * halt point, a shelf chip dropped on the line is tacked on at the gap it
 * lands in (a saved stop as one group) — with one difference: nothing has
 * flown, so EVERY stop can change. All of it goes through the same flight
 * routes a live line uses (`/api/flights/{id}/steps…`); the server takes
 * them on a grounded flight. */

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { toast } from "../../lib/toast";
import { newStep, type Flight, type FlightPlan, type FlightStep, type StepKindDef } from "../../lib/flights";
import type { DiagramNode } from "../../lib/flightDiagram";
import { dropStop, moveStop, patchStop, setHalt, tackOn, tackOnPlan } from "../../lib/flightsApi";
import { undoPayload } from "../../lib/stopEditing";
import { groundedDiagram } from "../../lib/groundedLines";
import type { FlywayCanvasProps } from "./FlywayCanvas";
import { StopInspector, StopPalette } from "./StopParts";
import { inspectorDef, orderedPlans } from "./useSessionLine";

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
  const [draft, setDraft] = useState<FlightStep | null>(null);
  const [busy, setBusy] = useState(false);
  const [selectIndex, setSelectIndex] = useState<number | null>(null);

  const diagram = useMemo(() => groundedDiagram(flight, kinds), [flight, kinds]);
  const kindOf = useMemo(() => new Map(kinds.map((k) => [k.kind, k])), [kinds]);
  const stops = diagram.nodes.filter((n) => n.kind === "stop");
  const end = flight.steps.length;
  const sel = selected ? diagram.nodes.find((n) => n.id === selected) || null : null;
  const latest = useRef(flight);
  latest.current = flight;

  // A stop just tacked on opens in the inspector once the flight has it.
  useEffect(() => {
    if (selectIndex === null) return;
    const n = stops.find((s) => s.index === selectIndex && s.step);
    if (!n) return;
    setSelected(n.id);
    setDraft(n.step!);
    setSelectIndex(null);
  }, [selectIndex, stops]);

  useEffect(() => {
    if (selected && !sel) {
      setSelected(null);
      setDraft(null);
    }
  }, [selected, sel]);

  const where = (at: number | null) => (at !== null && at < end ? at : undefined);

  const tackOne = async (def: StepKindDef, at: number | null) => {
    setBusy(true);
    setPaletteAt(null);
    try {
      const step = newStep(def, flight.steps.map((s) => s.id));
      if (await tackOn(flight.id, step, where(at))) setSelectIndex(where(at) ?? end);
    } finally {
      setBusy(false);
    }
  };

  const tackPlan = async (plan: Pick<FlightPlan, "id" | "name">, at: number | null) => {
    setBusy(true);
    setPaletteAt(null);
    try {
      if (await tackOnPlan(flight.id, plan.id, where(at))) toast("Added “" + plan.name + "”");
    } finally {
      setBusy(false);
    }
  };

  const removeStop = async (step: FlightStep, index: number): Promise<boolean> => {
    const id = flight.id;
    if (!(await dropStop(id, step.id))) return false;
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

  const onSelect = (n: DiagramNode) => {
    setPaletteAt(null);
    if (selected === n.id) {
      setSelected(null);
      setDraft(null);
      return;
    }
    setSelected(n.id);
    setDraft(n.step ? { ...n.step, config: { ...n.step.config } } : null);
  };

  /** The trailing +'s palette, open or shut (the ⋯'s "Add a stop…"). */
  const togglePalette = (open?: boolean) => {
    setSelected(null);
    setDraft(null);
    const shown = paletteAt !== null && paletteAt >= end;
    setPaletteAt(open ?? !shown ? end : null);
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

  const canvas: Pick<
    FlywayCanvasProps,
    "selected" | "onSelect" | "onOpen" | "onInsert" | "onRemove" | "onMove" | "onDuplicate" | "onToggleHalt" | "onDropStop"
  > = {
    selected: paletteAt !== null && paletteAt >= end ? "add" : selected,
    onSelect,
    onOpen: (n) => {
      if (n.kind === "add") togglePalette();
    },
    onInsert: (at) => setPaletteAt(paletteAt === at ? null : at),
    onRemove: (n) => n.step && n.index !== undefined && void removeStop(n.step, n.index),
    onMove: (from, to) => {
      const step = flight.steps[from];
      if (step && to >= 0 && to < flight.steps.length && to !== from) void moveStop(flight.id, step.id, to);
    },
    onDuplicate: (n) => {
      if (!n.step || n.index === undefined) return;
      void tackOn(flight.id, n.step, where(n.index + 1));
    },
    onToggleHalt: (n) => {
      const step = n.index !== undefined ? flight.steps[n.index] : undefined;
      if (step) void setHalt(flight.id, step.id, !step.halt);
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

  const palette: ReactNode =
    paletteAt !== null ? (
      <StopPalette
        kinds={kinds}
        title={paletteAt >= end ? "Add a stop" : "Add a stop before “" + (flight.steps[paletteAt]?.title || "") + "”"}
        onPick={(k) => void tackOne(k, paletteAt)}
        onClose={() => setPaletteAt(null)}
        extra={ordered.length > 0 ? savedChips : undefined}
      />
    ) : null;

  const dirtyStop = !!draft && !!sel?.step && JSON.stringify(draft) !== JSON.stringify(sel.step);
  const inspector: ReactNode =
    sel && draft ? (
      <StopInspector
        step={draft}
        def={inspectorDef(kindOf.get(draft.kind), draft)}
        onChange={(p) => setDraft((d) => (d ? { ...d, ...p } : d))}
        actions={
          <>
            <button
              type="button"
              className="btn-primary fl-btn"
              disabled={!dirtyStop || busy}
              onClick={async () => {
                setBusy(true);
                try {
                  await patchStop(flight.id, draft.id, { title: draft.title, config: draft.config, on_fail: draft.on_fail });
                } finally {
                  setBusy(false);
                }
              }}
            >
              Save stop
            </button>
            <button
              type="button"
              className="test-btn fl-btn danger"
              disabled={busy}
              onClick={async () => {
                setBusy(true);
                try {
                  if (sel.step && sel.index !== undefined && (await removeStop(sel.step, sel.index))) {
                    setSelected(null);
                    setDraft(null);
                  }
                } finally {
                  setBusy(false);
                }
              }}
            >
              Remove stop
            </button>
          </>
        }
      />
    ) : null;

  return {
    diagram,
    busy,
    togglePalette,
    editing: palette !== null || inspector !== null,
    canvas,
    palette,
    inspector,
  };
}
