/** One session's line, editable: the session diagram (lib/flightDiagram
 * `sessionDiagram`) with everything its boxes do — the trailing + opens the
 * stop palette (the kinds, then the saved stops: one tacks on as a group),
 * the + between two pending stops inserts one there, a click selects a box,
 * and ✎ / Enter / a double-click / the menu's Edit open its DIALOG
 * (StopDialog: a pending stop is edited there; a flown one, or the one the
 * flight is at, reads there), and the live flight's ⋯ / perch parts go
 * inside its boxes.
 *
 * A pending stop also moves (dragged along the line, ⇤ / ⇥: `POST
 * /api/flights/{id}/steps/{sid}/move {at}`), duplicates (⧉: the same stop
 * tacked on right after it). ANY stop comes off at once (round 11): one still
 * ahead, one that has flown, or the one the flight is at — then the flight
 * pauses right before the stop that followed — and the toast's Undo tacks it
 * back where it was while that is still ahead of the flight. A stop chip
 * dropped on the line from the shelf is tacked on at the gap it lands in; a
 * saved stop dropped there is tacked on as one group (`tackOnPlan`). ⏸ Halt
 * here makes a pending stop (or the current one) a halt point — `PATCH
 * /api/flights/{id}/steps/{sid} {halt}` — and Carry on past here takes it off
 * (docs/flights.md "Halt points").
 *
 * GROUPS (a stop with sub-steps, round 11) draw as one box; the line keeps
 * which ones it shows open (`expandedGroups`, ▸ / ▾). A group's dialog lists
 * its sub-steps (each opens its own), renames it, Ungroups it, saves it to
 * the shelf (the saved stop it came from, or a new one) and removes it.
 *
 * The line folds its flown stops into one pill only on request
 * (`foldFlown`, off by default: flown stops stay as boxes).
 *
 * Two homes share it: the pane's Flight tab (SessionFlight.tsx) and every
 * line in the Sky (SkyLine.tsx). Its `diagram` and `canvas` come already
 * counted the canvas's way (lib/flightDiagram `drawnCanvas`: a group box or
 * the flown pill is one place), ready to spread onto FlywayCanvas. Tacking a
 * stop onto a session with no flight yet starts an ad-hoc one that takes over
 * the fast-track's remaining rungs (`POST /api/flights/start {title, steps,
 * absorb_fasttrack}`), so the ladder's boxes turn into the flight's; a saved
 * stop on such a line takes off as that saved stop's flight (`takeOff`).
 *
 * The palette is the registry's (`GET /api/flights/kinds`), so Split and Join
 * show under "The flock does" once the server lists them; a Split's dialog
 * offers "+ Join" — one click puts a Join right after it — and hides its
 * `count` once its prompts name more than one worker. */

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { Instance } from "../../api/types";
import { toast } from "../../lib/toast";
import { isLive, newStep, planFromSteps, type Flight, type FlightPlan, type FlightRow, type FlightStep, type StepKindDef } from "../../lib/flights";
import { drawnCanvas, sessionDiagram, type DiagramNode } from "../../lib/flightDiagram";
import {
  dropGroup,
  dropStop,
  moveStop,
  patchStop,
  patchStopGroup,
  savePlan,
  setHalt,
  stepDone,
  stepRetry,
  stepSkip,
  tackOn,
  tackOnNew,
  tackOnPlan,
  tackOnSteps,
  takeOff,
  ungroupStops,
} from "../../lib/flightsApi";
import { groupHaltStep, undoPayload, type StopDrop } from "../../lib/stopEditing";
import { errMsg } from "../../lib/format";
import type { FlywayCanvasProps } from "./FlywayCanvas";
import { currentNodeId, liveBoxParts, useCancelAsk } from "./FlightStrip";
import { StopPalette } from "./StopParts";
import { StopDialog } from "./StopDialog";

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

/** The toast after a stop came off: removing the one the flight was at
 * pauses the flight right before the stop that followed (or at the end). */
export function removedToast(title: string, wasHere: boolean, next: string | null | undefined): string {
  if (!wasHere) return "Removed “" + title + "” from the line";
  return "Removed “" + title + "” — the flight is paused " + (next ? "before “" + next + "”" : "at the end");
}

/** The saved stop a group's "Save to the shelf" writes: the plan it came
 * from (`group.plan_id`, when the shelf still has it — updated in place),
 * else a new one. */
export function shelfPlanFor(name: string, steps: readonly FlightStep[], planId: string, plans: readonly FlightPlan[]): FlightPlan {
  const was = planId ? plans.find((p) => p.id === planId) : undefined;
  const fresh = planFromSteps(name, steps, was?.trigger);
  return was ? { ...was, ...fresh, builtin: was.builtin, session: was.session, description: was.description, id: was.id, updated: was.updated } : { ...fresh, id: "", updated: 0 };
}

type LineCanvasProps = Pick<
  FlywayCanvasProps,
  | "selected"
  | "onSelect"
  | "onEdit"
  | "onOpen"
  | "onInsert"
  | "onRemove"
  | "onMove"
  | "onDuplicate"
  | "onToggleHalt"
  | "onToggleGroup"
  | "onDropStop"
  | "renderInBox"
  | "sizeOf"
  | "reveal"
>;

export function useSessionLine(title: string, src: LineSource, opts: SessionLineOpts) {
  const { rows, flights, kinds, plans } = src;
  const family = opts.family !== false;
  const [selected, setSelected] = useState<string | null>(null);
  const [paletteAt, setPaletteAt] = useState<number | null>(null);
  // The open dialog (a node id), and the group's to go back to from a
  // sub-step's.
  const [dialog, setDialog] = useState<{ id: string; back?: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [selectIndex, setSelectIndex] = useState<number | null>(null);
  const [foldFlown, setFoldFlown] = useState(false);
  const [expandedGroups, setExpandedGroups] = useState<ReadonlySet<string>>(() => new Set());

  const row = rows.find((r) => r.title === title);
  const flRow = row?.flight as FlightRow | null | undefined;
  const flight = flRow && isLive(flRow) ? flights?.find((f) => f.id === flRow.id) || null : null;
  // The line in the flight's own indexes (what every handler below speaks).
  const line = useMemo(() => sessionDiagram(rows, title, { flights, kinds, family, foldFlown, expandedGroups }), [rows, title, flights, kinds, family, foldFlown, expandedGroups]);
  const cancel = useCancelAsk(flight);
  const parts = liveBoxParts(flight, true, cancel.ask);
  const kindOf = useMemo(() => new Map(kinds.map((k) => [k.kind, k])), [kinds]);
  const stops = line.nodes.filter((n) => n.kind === "stop");
  const end = flight ? flight.steps.length : 0;
  // The flight as it is now, for an Undo pressed after it has moved on.
  const latest = useRef(flight);
  latest.current = flight;

  /** A node by id: the line's own, or a sub-step of one of its group boxes. */
  const find = (id: string | null | undefined): DiagramNode | null => {
    if (!id) return null;
    for (const n of line.nodes) {
      if (n.id === id) return n;
      const sub = n.group?.nodes.find((s) => s.id === id);
      if (sub) return sub;
    }
    return null;
  };
  /** The line's own node for one the canvas hands back. */
  const own = (n: DiagramNode) => find(n.id) || n;
  const open = dialog ? find(dialog.id) : null;

  // A stop just tacked on from the palette opens in its dialog once the
  // flight has it.
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
    if (dialog && !open) setDialog(null);
    if (selected && !find(selected)) setSelected(null);
  }); // eslint-disable-line react-hooks/exhaustive-deps

  const tackOne = async (def: StepKindDef, at: number | null, openIt = false) => {
    const step = newStep(def, flight ? flight.steps.map((s) => s.id) : []);
    setBusy(true);
    setPaletteAt(null);
    try {
      if (flight) {
        const where = at !== null && at < end ? at : undefined;
        if ((await tackOn(flight.id, step, where)) && openIt) setSelectIndex(where ?? end);
      } else {
        const f = await tackOnNew(title, [step]);
        if (f) {
          toast("Took off: " + (f.plan_name || "a new flight"));
          if (openIt) setSelectIndex(f.steps.length - 1);
        }
      }
    } finally {
      setBusy(false);
    }
  };

  /** A saved stop onto the line, as ONE group (before stop `at`, null = the
   * end). A line with no flight takes off as that saved stop's flight. */
  const tackPlan = async (plan: Pick<FlightPlan, "id" | "name"> & { steps?: FlightStep[] }, at: number | null) => {
    setBusy(true);
    setPaletteAt(null);
    try {
      if (flight) {
        const where = at !== null && at < end ? at : undefined;
        if (await tackOnPlan(flight.id, plan.id, where)) toast("Tacked on “" + plan.name + "”" + (plan.steps ? " — " + plan.steps.length + (plan.steps.length === 1 ? " stop" : " stops") : ""));
      } else await takeOff({ plan_id: plan.id, title });
    } finally {
      setBusy(false);
    }
  };

  /** Take a stop off the line — at once, with Undo in the toast. The one the
   * flight is at, too: the flight pauses before the stop that followed. */
  const removeStop = async (step: FlightStep, index: number): Promise<boolean> => {
    if (!flight) return false;
    const id = flight.id;
    const wasHere = index === flight.cursor;
    const next = flight.steps[index + 1]?.title;
    if (!(await dropStop(id, step.id))) return false;
    toast(removedToast(step.title, wasHere, next), {
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

  /** Take a whole group off the line (`?group=1`), with Undo: its steps
   * tacked back as a group of the same name. */
  const removeGroup = async (n: DiagramNode): Promise<boolean> => {
    const g = n.group;
    if (!flight || !g || n.index === undefined || !g.steps.length) return false;
    const id = flight.id;
    const first = n.index;
    const wasHere = flight.cursor >= first && flight.cursor < first + g.total;
    const next = flight.steps[first + g.total]?.title;
    if (!(await dropGroup(id, g.steps[0].id))) return false;
    toast(removedToast(g.name, wasHere, next), {
      action: {
        label: "Undo",
        ariaLabel: "Undo: put “" + g.name + "” back on the line",
        onClick: () => {
          const now = latest.current?.id === id ? latest.current : null;
          const back = undoPayload(g.steps[0], first, now);
          void tackOnSteps(id, g.steps, back.at, g.name);
        },
      },
    });
    return true;
  };

  const removeNode = async (n: DiagramNode): Promise<boolean> => {
    if (n.kind === "group") return removeGroup(n);
    return n.step && n.index !== undefined ? removeStop(n.step, n.index) : false;
  };

  // "+ Join" on a Split: a Join right after it, one click.
  const joinDef = kindOf.get("join");
  const splitAt = open?.step?.kind === "split" && open.index !== undefined ? open.index : -1;
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
        if (await tackOnPlan(flight.id, plan.id)) toast("Tacked on “" + plan.name + "” — " + plan.steps.length + " stops");
      } else await takeOff({ plan_id: plan.id, title });
    } finally {
      setBusy(false);
    }
  };

  /** A click: select the box (again: let go of it). */
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

  /** The trailing +'s palette, open or shut (the line's ⋯ "Fly a flyway…"). */
  const togglePalette = (open?: boolean) => {
    setDialog(null);
    const shown = paletteAt !== null && paletteAt >= end;
    setPaletteAt(open ?? !shown ? end : null);
  };

  const toggleGroup = (gid: string) =>
    setExpandedGroups((cur) => {
      const next = new Set(cur);
      if (next.has(gid)) next.delete(gid);
      else next.add(gid);
      return next;
    });

  /** ⏸ on a stop not yet flown (one still ahead, or where the flight is); on
   * a group, its first sub-step not yet flown. */
  const toggleHalt = (n: DiagramNode) => {
    if (!flight) return;
    const target = n.kind === "group" ? groupHaltStep(n) : n;
    const i = target?.index;
    const step = i !== undefined ? flight.steps[i] : undefined;
    const st = step ? flight.step_states?.[step.id]?.state : undefined;
    if (step && i! >= flight.cursor && st !== "done" && st !== "skipped") void setHalt(flight.id, step.id, !step.halt);
  };

  const ordered = orderedPlans(plans);
  const savedStops = (at: number | null) => (
    <div className="fl-palette-flyways">
      <div className="fl-palette-head">Saved stops — tack one on as a stop with sub-steps</div>
      <div className="fl-chiprow">
        {ordered.map((p) => (
          <button key={p.id} type="button" className="fl-pick" disabled={busy} title={p.steps.map((s) => s.title).join(" → ")} onClick={() => void tackPlan(p, at)}>
            {p.name}
          </button>
        ))}
      </div>
    </div>
  );

  const raw: LineCanvasProps = {
    selected: paletteAt !== null && paletteAt >= end ? "add" : dialog?.id || selected,
    onSelect: (n) => onSelect(own(n)),
    onEdit: (n) => onEdit(own(n)),
    onOpen: (n) => {
      if (n.kind === "add") togglePalette();
      else if (n.kind === "flown") setFoldFlown(false);
      else if (n.session && n.session !== title) opts.onSession(n.session);
    },
    onInsert: flight ? (at) => setPaletteAt(paletteAt === at ? null : at) : undefined,
    onRemove: flight ? (n) => void removeNode(own(n)) : undefined,
    // Indexes among the line's stops are the flight's (drawnCanvas maps the
    // canvas's places onto them).
    onMove: flight
      ? (from, to) => {
          const step = flight.steps[from];
          const n = stops.find((s) => s.index === from);
          if (step && n?.editable && to >= line.insertFrom && to < flight.steps.length) void moveStop(flight.id, step.id, to);
        }
      : undefined,
    onDuplicate: flight
      ? (n) => {
          n = own(n);
          if (!n.editable || !n.step || n.index === undefined) return;
          const at = n.index + 1;
          void tackOn(flight.id, n.step, at < end ? at : undefined);
        }
      : undefined,
    // A stop the flight has not flown yet: one still ahead, or where it is.
    onToggleHalt: flight ? (n) => toggleHalt(own(n)) : undefined,
    onToggleGroup: toggleGroup,
    onDropStop: (drop: StopDrop, at) => {
      if ("planId" in drop) {
        const plan = plans.find((p) => p.id === drop.planId);
        if (plan || flight) void tackPlan(plan || { id: drop.planId, name: "the saved stop" }, at);
        else toast("That saved stop isn't on this server any more");
        return;
      }
      const def = kindOf.get(drop.kind);
      if (def) void tackOne(def, at);
      else toast("This line has no “" + drop.kind + "” stop to tack on");
    },
    renderInBox: parts.renderInBox,
    sizeOf: parts.sizeOf,
    reveal: currentNodeId(line, flight),
  };
  // The canvas counts a group box (and the flown pill) as one place.
  const shown = drawnCanvas(line, raw);

  const palette: ReactNode =
    paletteAt !== null ? (
      <StopPalette
        kinds={kinds}
        title={paletteAt >= end ? "Tack on a stop" : "Add a stop before “" + (flight?.steps[paletteAt]?.title || "") + "”"}
        onPick={(k) => void tackOne(k, paletteAt, true)}
        onClose={() => setPaletteAt(null)}
        extra={ordered.length > 0 ? savedStops(paletteAt >= end ? null : paletteAt) : undefined}
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
    const changeable = g.nodes.find((s) => s.editable && s.step);
    inspector = (
      <StopDialog
        key={open.id}
        node={open}
        kindOf={kindOf}
        busy={busy}
        note={open.editable ? undefined : open.state === "done" || open.state === "skipped" ? "This stop has flown." : "The flight is in this stop — only its sub-steps still ahead can change."}
        onClose={close}
        onRename={
          flight && changeable
            ? (name) =>
                act(async () => {
                  if (await patchStopGroup(flight.id, changeable.step!.id, { name })) setDialog(null);
                })
            : undefined
        }
        onOpenSub={(s) => setDialog({ id: s.id, back: open.id })}
        onRemoveSub={flight ? (s) => void act(() => removeNode(s)) : undefined}
        onUngroup={
          flight
            ? () =>
                void act(async () => {
                  if (await ungroupStops(flight.id, g.id)) setDialog(null);
                })
            : undefined
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
        onRemove={
          flight
            ? () =>
                void act(async () => {
                  if (await removeGroup(open)) setDialog(null);
                })
            : undefined
        }
      />
    );
  } else if (open && open.kind === "stop") {
    const here = !!flight && open.index === flight.cursor && !open.editable;
    const editable = !!flight && !!open.editable && !!open.step;
    const flown = open.state === "done" || open.state === "skipped" || open.state === "failed";
    const rec = flight && open.step ? flight.step_states?.[open.step.id] : undefined;
    const why = editable ? "" : flown ? "This stop has flown — it can't change now." : here ? "The flight is at this stop — it can't change while it is here." : "It can't change here.";
    const def = open.step ? inspectorDef(kindOf.get(open.step.kind), open.step) : undefined;
    inspector = (
      <StopDialog
        key={open.id}
        node={open}
        def={def}
        busy={busy}
        note={[why, open.reason || rec?.note || ""].filter(Boolean).join(" ")}
        onClose={close}
        onSave={
          editable
            ? (patch) =>
                act(async () => {
                  if (await patchStop(flight!.id, open.step!.id, patch)) close();
                })
            : undefined
        }
        onDuplicate={
          editable
            ? () => {
                const at = open.index! + 1;
                void tackOn(flight!.id, open.step!, at < end ? at : undefined);
                close();
              }
            : undefined
        }
        onRemove={
          flight && open.step
            ? () =>
                void act(async () => {
                  if (await removeNode(open)) close();
                })
            : undefined
        }
        onToggleHalt={here && open.step && !flown ? () => toggleHalt(open) : undefined}
        onDone={here && open.state === "perched" ? () => void act(() => stepDone(flight!.id, open.step!.id)) : undefined}
        onRetry={here && open.state === "halted" ? () => void act(() => stepRetry(flight!.id, open.step!.id)) : undefined}
        onSkip={here ? () => void act(() => stepSkip(flight!.id, open.step!.id)) : undefined}
        extra={
          <>
            {open.step?.kind === "split" && (
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
      />
    );
  }

  return {
    row,
    flight,
    /** The line as the canvas counts it (drawn places). */
    diagram: shown.diagram,
    busy,
    setBusy,
    ordered,
    flyFlyway,
    togglePalette,
    /** Fold the flown stops into one pill (off by default). */
    foldFlown,
    setFoldFlown,
    /** The groups this line draws open, and ▸ / ▾. */
    expandedGroups,
    toggleGroup,
    /** Something is open under the line (the palette or a stop's dialog). */
    editing: palette !== null || inspector !== null,
    canvas: shown.canvas,
    palette,
    /** The open stop's dialog (StopDialog), or null. */
    inspector,
    cancel,
  };
}
