/** One session's line, editable: the session diagram (lib/flightDiagram
 * `sessionDiagram`) with everything its boxes do — the trailing + opens the
 * stop palette (and "Fly a flyway — all of its stops"), the + between two
 * pending stops inserts one there, a stop opens in the inspector (a pending
 * one is edited in place; done and current ones are read-only), Delete drops
 * a pending stop, and the live flight's ⋯ / perch parts go inside its boxes.
 *
 * A pending stop also moves (dragged along the line, ⇤ / ⇥: `POST
 * /api/flights/{id}/steps/{sid}/move {at}`), duplicates (⧉: the same stop
 * tacked on right after it), and is removed at once — the toast's Undo tacks
 * it back where it was while that is still ahead of the flight. A stop chip
 * dropped on the line from the shelf is tacked on at the gap it lands in.
 * ⏸ Halt here makes a pending stop (or the current one) a halt point —
 * `PATCH /api/flights/{id}/steps/{sid} {halt}` — and Carry on past here
 * takes it off (docs/flights.md "Halt points").
 *
 * Two homes share it: the pane's Flight tab (SessionFlight.tsx) and every
 * line in the Sky (Sky.tsx). Tacking a stop onto a session with no flight yet
 * starts an ad-hoc one that takes over the fast-track's remaining rungs
 * (`POST /api/flights/start {title, steps, absorb_fasttrack}`), so the
 * ladder's boxes turn into the flight's.
 *
 * The palette is the registry's (`GET /api/flights/kinds`), so Split and Join
 * show under "The flock does" once the server lists them; a Split in the
 * inspector offers "+ Join" — one click puts a Join right after it — and
 * hides its `count` once its prompts name more than one worker. */

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { Instance } from "../../api/types";
import { toast } from "../../lib/toast";
import { isLive, newStep, type Flight, type FlightPlan, type FlightRow, type FlightStep, type StepKindDef } from "../../lib/flights";
import { sessionDiagram, type DiagramNode } from "../../lib/flightDiagram";
import { dropStop, moveStop, patchStop, setHalt, tackOn, tackOnAll, tackOnNew, takeOff } from "../../lib/flightsApi";
import { undoPayload } from "../../lib/stopEditing";
import type { FlywayCanvasProps } from "./FlywayCanvas";
import { currentNodeId, liveBoxParts, useCancelAsk } from "./FlightStrip";
import { StopInspector, StopPalette } from "./StopParts";

export interface LineSource {
  rows: readonly Instance[];
  flights: readonly Flight[] | null | undefined;
  kinds: readonly StepKindDef[];
  plans: readonly FlightPlan[];
}

export interface SessionLineOpts {
  /** Draw the parent above and the workers below (default true). */
  family?: boolean;
  /** A parent / child box pressed: go to that session. */
  onSession(title: string): void;
}

/** How many workers a Split's `prompts` name (one per non-empty line). */
export function promptLines(v: unknown): number {
  const list = Array.isArray(v) ? v.map(String) : String(v ?? "").split("\n");
  return list.filter((l) => l.trim()).length;
}

/** A stop's kind as its inspector draws it: a Split whose prompts name more
 * than one worker has no `count` to ask (each line is a worker). */
export function inspectorDef(def: StepKindDef | undefined, step: FlightStep): StepKindDef | undefined {
  if (!def || step.kind !== "split" || promptLines(step.config?.prompts) <= 1) return def;
  return { ...def, fields: def.fields.filter((f) => f.name !== "count") };
}

/** Built-ins first, then the owner's own. */
export const orderedPlans = (plans: readonly FlightPlan[]) => [...plans.filter((p) => p.builtin), ...plans.filter((p) => !p.builtin)];

export function useSessionLine(title: string, src: LineSource, opts: SessionLineOpts) {
  const { rows, flights, kinds, plans } = src;
  const family = opts.family !== false;
  const [selected, setSelected] = useState<string | null>(null);
  const [paletteAt, setPaletteAt] = useState<number | null>(null);
  const [draft, setDraft] = useState<FlightStep | null>(null);
  const [busy, setBusy] = useState(false);
  const [selectIndex, setSelectIndex] = useState<number | null>(null);

  const row = rows.find((r) => r.title === title);
  const flRow = row?.flight as FlightRow | null | undefined;
  const flight = flRow && isLive(flRow) ? flights?.find((f) => f.id === flRow.id) || null : null;
  const diagram = useMemo(() => sessionDiagram(rows, title, { flights, kinds, family }), [rows, title, flights, kinds, family]);
  const cancel = useCancelAsk(flight);
  const parts = liveBoxParts(flight, true, cancel.ask);
  const kindOf = useMemo(() => new Map(kinds.map((k) => [k.kind, k])), [kinds]);
  const stops = diagram.nodes.filter((n) => n.kind === "stop");
  const end = flight ? flight.steps.length : 0;
  const sel = selected ? diagram.nodes.find((n) => n.id === selected) || null : null;
  // The flight as it is now, for an Undo pressed after it has moved on.
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

  // The inspector follows the box; a box that left the line closes it.
  useEffect(() => {
    if (selected && !sel) {
      setSelected(null);
      setDraft(null);
    }
  }, [selected, sel]);

  const tackOne = async (def: StepKindDef, at: number | null) => {
    const step = newStep(def, flight ? flight.steps.map((s) => s.id) : []);
    setBusy(true);
    setPaletteAt(null);
    try {
      if (flight) {
        const where = at !== null && at < end ? at : undefined;
        if (await tackOn(flight.id, step, where)) setSelectIndex(where ?? end);
      } else {
        const f = await tackOnNew(title, [step]);
        if (f) {
          toast("Took off: " + (f.plan_name || "a new flight"));
          setSelectIndex(f.steps.length - 1);
        }
      }
    } finally {
      setBusy(false);
    }
  };

  /** Take a pending stop off the line — at once, with Undo in the toast. */
  const removeStop = async (step: FlightStep, index: number): Promise<boolean> => {
    if (!flight) return false;
    const id = flight.id;
    if (!(await dropStop(id, step.id))) return false;
    toast("Removed “" + step.title + "” from the line", {
      action: {
        label: "Undo",
        ariaLabel: "Undo: put “" + step.title + "” back on the line",
        onClick: () => {
          const now = latest.current?.id === id ? latest.current : null;
          const back = undoPayload(step, index, now);
          void tackOn(id, back.step, back.at);
        },
      },
    });
    return true;
  };

  // "+ Join" on a Split: a Join right after it, one click.
  const joinDef = kindOf.get("join");
  const splitAt = sel?.step?.kind === "split" && sel.index !== undefined ? sel.index : -1;
  const offerJoin = !!flight && !!joinDef && splitAt >= 0 && flight.steps[splitAt + 1]?.kind !== "join";
  const addJoin = async () => {
    if (!flight || !joinDef) return;
    const at = splitAt + 1;
    setBusy(true);
    try {
      await tackOn(flight.id, newStep(joinDef, flight.steps.map((s) => s.id)), at < end ? at : undefined);
    } finally {
      setBusy(false);
    }
  };

  const flyFlyway = async (plan: FlightPlan) => {
    setBusy(true);
    setPaletteAt(null);
    try {
      if (flight) {
        if (await tackOnAll(flight.id, plan.steps)) toast("Added “" + plan.name + "”'s " + plan.steps.length + " stops");
      } else await takeOff({ plan_id: plan.id, title });
    } finally {
      setBusy(false);
    }
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

  /** The trailing +'s palette, open or shut (the line's ⋯ "Fly a flyway…"). */
  const togglePalette = (open?: boolean) => {
    setSelected(null);
    setDraft(null);
    const shown = paletteAt !== null && paletteAt >= end;
    setPaletteAt(open ?? !shown ? end : null);
  };

  const ordered = orderedPlans(plans);
  const flywayChips = (
    <div className="fl-palette-flyways">
      <div className="fl-palette-head">Fly a flyway — all of its stops</div>
      <div className="fl-chiprow">
        {ordered.map((p) => (
          <button key={p.id} type="button" className="fl-pick" disabled={busy} title={p.steps.map((s) => s.title).join(" → ")} onClick={() => void flyFlyway(p)}>
            {p.name}
          </button>
        ))}
      </div>
    </div>
  );

  const editable = !!sel?.editable && !!draft && !!flight;
  const dirtyStop = !!draft && !!sel?.step && JSON.stringify(draft) !== JSON.stringify(sel.step);

  const canvas: Pick<
    FlywayCanvasProps,
    "selected" | "onSelect" | "onOpen" | "onInsert" | "onRemove" | "onMove" | "onDuplicate" | "onToggleHalt" | "onDropStop" | "renderInBox" | "sizeOf" | "reveal"
  > = {
    selected: paletteAt !== null && paletteAt >= end ? "add" : selected,
    onSelect,
    onOpen: (n) => {
      if (n.kind === "add") togglePalette();
      else if (n.session && n.session !== title) opts.onSession(n.session);
    },
    onInsert: flight ? (at) => setPaletteAt(paletteAt === at ? null : at) : undefined,
    onRemove: flight ? (n) => n.editable && n.step && n.index !== undefined && void removeStop(n.step, n.index) : undefined,
    // Indexes among the line's stops are the flight's (every stop is on it).
    onMove: flight
      ? (from, to) => {
          const step = flight.steps[from];
          const n = stops.find((s) => s.index === from);
          if (step && n?.editable && to >= diagram.insertFrom && to < flight.steps.length) void moveStop(flight.id, step.id, to);
        }
      : undefined,
    onDuplicate: flight
      ? (n) => {
          if (!n.editable || !n.step || n.index === undefined) return;
          const at = n.index + 1;
          void tackOn(flight.id, n.step, at < end ? at : undefined);
        }
      : undefined,
    // A stop the flight has not flown yet: one still ahead, or where it is.
    onToggleHalt: flight
      ? (n) => {
          const step = n.index !== undefined ? flight.steps[n.index] : undefined;
          const st = step ? flight.step_states?.[step.id]?.state : undefined;
          if (step && n.index! >= flight.cursor && st !== "done" && st !== "skipped") void setHalt(flight.id, step.id, !step.halt);
        }
      : undefined,
    onDropStop: (kind, at) => {
      const def = kindOf.get(kind);
      if (def) void tackOne(def, at);
      else toast("This line has no “" + kind + "” stop to tack on");
    },
    renderInBox: parts.renderInBox,
    sizeOf: parts.sizeOf,
    reveal: currentNodeId(diagram, flight),
  };

  const palette: ReactNode =
    paletteAt !== null ? (
      <StopPalette
        kinds={kinds}
        title={paletteAt >= end ? "Tack on a stop" : "Add a stop before “" + (flight?.steps[paletteAt]?.title || "") + "”"}
        onPick={(k) => void tackOne(k, paletteAt)}
        onClose={() => setPaletteAt(null)}
        extra={paletteAt >= end && ordered.length > 0 ? flywayChips : undefined}
      />
    ) : null;

  const inspector: ReactNode =
    sel && draft ? (
      <StopInspector
        step={draft}
        def={inspectorDef(kindOf.get(draft.kind), draft)}
        readOnly={!editable}
        onChange={(p) => setDraft((d) => (d ? { ...d, ...p } : d))}
        note={
          <>
            {!editable && (
              <p className="fl-note muted">{sel.state === "done" || sel.state === "skipped" ? "This stop has flown — it can't change now." : "The flight is at this stop — it can't change while it is here."}</p>
            )}
            {draft.kind === "split" && (
              <p className="fl-note muted fl-split-hint">Prompts: one worker per line — each line starts a worker. One line (or none) and Count says how many.</p>
            )}
            {offerJoin && (
              <div className="fl-chiprow fl-join-offer">
                <button type="button" className="fl-pick" disabled={busy} title="Put a Join right after this Split: the line waits until every worker is done" onClick={() => void addJoin()}>
                  + Join
                </button>
              </div>
            )}
          </>
        }
        actions={
          editable ? (
            <>
              <button
                type="button"
                className="btn-primary fl-btn"
                disabled={!dirtyStop || busy}
                onClick={async () => {
                  setBusy(true);
                  try {
                    await patchStop(flight!.id, draft.id, { title: draft.title, config: draft.config, on_fail: draft.on_fail });
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
          ) : undefined
        }
      />
    ) : null;

  return {
    row,
    flight,
    diagram,
    busy,
    setBusy,
    ordered,
    flyFlyway,
    togglePalette,
    /** Something is open under the line (the palette or the inspector). */
    editing: palette !== null || inspector !== null,
    canvas,
    palette,
    inspector,
    cancel,
  };
}
