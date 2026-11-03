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
import { useQuery } from "@tanstack/react-query";
import type { Instance } from "../../api/types";
import { toast } from "../../lib/toast";
import { optimistic, optimisticDelete, withSteps, withoutSteps } from "../../lib/flightOptimism";
import { isShown, newStep, planFromSteps, type Flight, type IntakeDefault, type IntakeSourceRef, type SubjectKind, type FlightPlan, type FlightRow, type FlightStep, type StepKindDef, type StepRunState } from "../../lib/flights";
import { LOOP_AGENT, drawnCanvas, findNode, firstStepOf, loopBackTo, loopChoices, loopSlotAfter, loopStepTo, receiptOf, sessionDiagram, type DiagramNode, type LoopChoice } from "../../lib/flightDiagram";
import {
  dropGroup,
  deleteFlight,
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
  tackOnParallel,
  tackOnPlan,
  tackOnSteps,
  takeOff,
  intakeSourceOfFlight,
  setIntakeDefault,
  ungroupStops,
} from "../../lib/flightsApi";
import { groupHaltStep, undoPayload, type StopDrop } from "../../lib/stopEditing";
import { pushUndo, takeUndo, undoAction } from "../../lib/skyUndo";
import { removalNeedsAsk } from "../../lib/skySelect";
import { InlineConfirm } from "../InlineConfirm";
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
  /** The sky (round 14): a stop the flight is at while it runs asks before
   * it comes off (`removeAsk`, drawn by the line); everything else goes at
   * once. Off (the pane's Flight tab): every stop goes at once, as before. */
  askRunning?: boolean;
}

/** The flight's step indexes a box stands for: a stop its own, a group all
 * of its sub-steps. */
export function nodeIndexes(n: Pick<DiagramNode, "kind" | "index" | "group">): number[] {
  if (n.index === undefined) return [];
  if (n.kind !== "group") return [n.index];
  return Array.from({ length: n.group?.total || 0 }, (_, k) => n.index! + k);
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

/** A source's default stops, promoted from one ticket's line (docs/flights.md
 * "default stops"): our built-in stops are every intake source's default, and
 * editing ONE ticket's line never changes it — only this explicit choice in
 * the line's ⋯ does. It changes what NEW items from that source fly; the
 * ticket whose line it was keeps flying as it is. */

/** The subjects that came from intake (a flight Intake started). */
export const INTAKE_SUBJECTS: readonly SubjectKind[] = ["ticket", "pr", "issue"];

/** The ⋯ item applies only to a flight Intake started. */
export const isIntakeSubject = (kind: SubjectKind | undefined | null): boolean => !!kind && INTAKE_SUBJECTS.includes(kind);

/** A source kind in words: one item, many items. */
export function intakeWords(kind: IntakeSourceRef["kind"]): { one: string; many: string } {
  if (kind === "prs") return { one: "PR", many: "PRs" };
  if (kind === "issues") return { one: "issue", many: "issues" };
  return { one: "ticket", many: "tickets" };
}

/** The source's name for a sentence: the lane's label without its kind
 * prefix ("Tickets · Shortcut" → "Shortcut"), so "every ticket from
 * Shortcut" doesn't say the kind twice. A label with no such prefix stays. */
export function intakeSourceName(src: Pick<IntakeDefault, "kind" | "label" | "target">): string {
  const label = (src.label || "").trim();
  const m = /^(tickets|prs|pull requests|issues)\s*[·:—-]\s*(.+)$/i.exec(label);
  return (m ? m[2].trim() : label) || src.target;
}

/** The ⋯ item's label, its tooltip, and the toast once it is done. */
export function promoteWords(src: Pick<IntakeDefault, "kind" | "label" | "target">): { label: string; title: string; done: string } {
  const w = intakeWords(src.kind);
  const name = intakeSourceName(src);
  return {
    label: "Use these stops for every " + w.one + " from " + name,
    title: "Changes what NEW " + w.many + " from " + name + " run: each one starts with this chain's stops. This " + w.one + " keeps running as it is.",
    done: "New " + w.many + " from " + name + " run this chain",
  };
}

/** Make one flight's stops its source's default (`from_flight`), with Undo
 * in the toast and on Ctrl+Z: the source back to the stops it flew before
 * (`previous_plan_id`; "" = our built-in). False when the server refused. */
export async function promoteStops(flightId: string): Promise<boolean> {
  const res = await setIntakeDefault({ from_flight: flightId });
  if (!res) return false;
  const words = promoteWords(res);
  const back = { kind: res.kind, target: res.target, plan_id: res.previous_plan_id };
  // One undo, two roads: the toast's button and Ctrl+Z in the sky.
  const promoted = pushUndo({ label: words.label, undo: () => setIntakeDefault(back) });
  toast(words.done, { action: undoAction(promoted, "Undo: new " + intakeWords(res.kind).many + " from " + intakeSourceName(res) + " run the chain they did before") });
  return true;
}

/** The line's "Use these stops for every …" (null: not from intake, or its
 * source is gone). The source is fetched once per flight id, not per render. */
export function usePromoteStops(flight: Pick<Flight, "id" | "subject"> | null | undefined): { label: string; title: string; run(): Promise<boolean> } | null {
  const id = flight?.id || "";
  const intake = !!id && isIntakeSubject(flight?.subject?.kind);
  const q = useQuery({
    queryKey: ["intake-source-of-flight", id],
    queryFn: () => intakeSourceOfFlight(id),
    enabled: intake,
    staleTime: Infinity,
  });
  const src = intake ? q.data : null;
  if (!src) return null;
  const words = promoteWords(src);
  return { label: words.label, title: words.title, run: () => promoteStops(id) };
}

/** The toast after a stop came off: removing the one the flight was at
 * pauses the flight right before the stop that followed (or at the end). */
export function removedToast(title: string, wasHere: boolean, next: string | null | undefined): string {
  if (!wasHere) return "Removed “" + title + "” from the chain";
  return "Removed “" + title + "” — the chain is paused " + (next ? "before “" + next + "”" : "at the end");
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
  | "onLoopFrom"
  | "onLoopTo"
  | "renderInBox"
  | "sizeOf"
  | "reveal"
>;


/** What a flying or flown stop did, for its read-only dialog: the flight's
 * history on it (newest last), its note, a command's output, and for a ship
 * stop the fast-track's own word (its lane runs in the server, not in the
 * terminal, so this is where to look). */
/** A screenshot stop's shots (`detail.shots`: [{name, url, width}]), only the
 * well-formed ones. */
export function shotsOf(detail: Record<string, unknown> | null | undefined): Array<{ name: string; url: string; width: number }> {
  const raw = detail?.shots;
  if (!Array.isArray(raw)) return [];
  return raw
    .map((s) => (s && typeof s === "object" ? (s as Record<string, unknown>) : null))
    .filter((s): s is Record<string, unknown> => !!s && typeof s.url === "string" && !!s.url)
    .map((s) => ({ name: String(s.name || ""), url: String(s.url), width: Number(s.width) || 0 }));
}

export function stopLog(n: DiagramNode, flight: Flight | null, rec: StepRunState | undefined, row: Instance | undefined): ReactNode {
  if (!flight || !n.step) return null;
  const id = n.step.id;
  const hist = (flight.history || []).filter((h) => h.step === id);
  const out = String((rec?.detail as Record<string, unknown> | undefined)?.output || "");
  const ap = n.step.kind === "ship" ? row?.autopilot : null;
  // Round 15: the receipt leads, the shots follow.
  const receipt = receiptOf(rec?.detail);
  const shots = shotsOf(rec?.detail);
  if (!hist.length && !rec?.note && !out && !ap && !receipt && !shots.length) return null;
  const when = (t: number) => {
    const d = new Date(t * 1000);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  };
  return (
    <section className="stop-dialog-log" aria-label="What this stop did">
      <h3 className="fl-group-head">What it did</h3>
      {receipt && (
        <p className="stop-dialog-receipt" data-receipt>
          {receipt}
        </p>
      )}
      {shots.length > 0 && (
        <div className="stop-dialog-shots" role="list" aria-label="Screenshots">
          {shots.map((s, i) => (
            <a key={s.url + i} className="stop-dialog-shot" role="listitem" href={s.url} target="_blank" rel="noreferrer" title={(s.name || "shot") + (s.width ? " · " + s.width + "px" : "") + " — open"}>
              <img src={s.url} alt={s.name || "screenshot"} loading="lazy" />
              <span className="stop-dialog-shot-name">{s.name || "shot"}{s.width ? " · " + s.width : ""}</span>
            </a>
          ))}
        </div>
      )}
      {ap && (
        <p className="fl-note">
          Fast-track: <b>{ap.state}</b>
          {ap.step ? " · " + ap.step : ""}
          {ap.reason ? " — " + ap.reason : ""}
        </p>
      )}
      {hist.length > 0 && (
        <ol className="stop-dialog-hist">
          {hist.slice(-12).map((h, i) => (
            <li key={i}>
              <span className="muted">{when(h.at)}</span> <b>{h.event}</b>
              {h.note ? " — " + h.note : ""}
            </li>
          ))}
        </ol>
      )}
      {rec?.note && !hist.some((h) => h.note === rec.note) && <p className="fl-note">{rec.note}</p>}
      {out && <pre className="stop-dialog-out">{out.split("\n").slice(-40).join("\n")}</pre>}
    </section>
  );
}

export function useSessionLine(title: string, src: LineSource, opts: SessionLineOpts) {
  const { rows, flights, kinds, plans } = src;
  const family = opts.family !== false;
  const [selected, setSelected] = useState<string | null>(null);
  const [paletteAt, setPaletteAt] = useState<number | null>(null);
  // Round 18: the palette opened from a wire dropped on empty sky — the node
  // it hangs off (its panel opens beside it) and the spot the new stop takes.
  const [paletteHang, setPaletteHang] = useState<{ node: string | null; pos: { x: number; y: number } | null; withStep?: string } | null>(null);
  // The open dialog (a node id), and the group's to go back to from a
  // sub-step's.
  const [dialog, setDialog] = useState<{ id: string; back?: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [selectIndex, setSelectIndex] = useState<number | null>(null);
  const [foldFlown, setFoldFlown] = useState(false);
  const [expandedGroups, setExpandedGroups] = useState<ReadonlySet<string>>(() => new Set());

  const row = rows.find((r) => r.title === title);
  const flRow = row?.flight as FlightRow | null | undefined;
  const flight = flRow && isShown(flRow) ? flights?.find((f) => f.id === flRow.id) || null : null;
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

  /** A node by id: the line's own, a sub-step of one of its group boxes, or
   * a Loop on its return wire. */
  const find = (id: string | null | undefined): DiagramNode | null => findNode(line, id);
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
    const fresh = newStep(def, flight ? flight.steps.map((s) => s.id) : []);
    // Dropped at a spot on the sky: it is drawn there (round 18).
    const step = paletteHang?.pos ? { ...fresh, pos: paletteHang.pos } : fresh;
    const withStep = paletteHang?.withStep;
    setBusy(true);
    setPaletteAt(null);
    setPaletteHang(null);
    try {
      if (flight) {
        const where = at !== null && at < end ? at : undefined;
        // Shown at once; the server's flight replaces the guess (lib/flightOptimism).
        // In parallel with a stop (round 20): into its group, else before `where`.
        const fid = flight.id;
        const go = withStep ? () => tackOnParallel(fid, step, withStep) : () => tackOn(fid, step, where);
        if ((await optimistic(fid, withSteps([step], where), go)) && openIt) setSelectIndex(where ?? end);
      } else {
        // Round 19: building a chain on a session doesn't fly it — it waits,
        // ready, for ▶ Run or the session's next prompt.
        const f = await tackOnNew(title, [step], "prompt");
        if (f) {
          toast("Ready: " + (f.plan_name || "a new chain") + " — it runs on ▶ Run or your next prompt");
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
        const steps = plan.steps || plans.find((p) => p.id === plan.id)?.steps;
        const fid = flight.id;
        const go = () => tackOnPlan(fid, plan.id, where);
        if (await (steps?.length ? optimistic(fid, withSteps(steps, where), go) : go())) toast("Tacked on “" + plan.name + "”" + (plan.steps ? " — " + plan.steps.length + (plan.steps.length === 1 ? " stop" : " stops") : ""));
      } else await takeOff({ plan_id: plan.id, title, start_on: "prompt" });
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
    // Gone at once; a refusal puts it back (lib/flightOptimism).
    if (!(await optimistic(id, withoutSteps(new Set([step.id])), () => dropStop(id, step.id)))) return false;
    // One undo, two roads: the toast's button and Ctrl+Z in the sky (lib/skyUndo).
    const entry = pushUndo({
      label: "Remove “" + step.title + "”",
      undo: () => {
        const now = latest.current?.id === id ? latest.current : null;
        const back = undoPayload(step, index, now);
        return tackOn(id, back.step, back.at);
      },
    });
    toast(removedToast(step.title, wasHere, next), {
      action: {
        label: "Undo",
        ariaLabel: "Undo: put “" + step.title + "” back on the chain",
        // The same entry Ctrl+Z pops: whichever comes first runs it.
        onClick: () => {
          if (takeUndo(entry)) void entry.undo();
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
    if (!(await optimistic(id, withoutSteps(new Set(g.steps.map((s) => s.id))), () => dropGroup(id, g.steps[0].id)))) return false;
    const entry = pushUndo({
      label: "Remove “" + g.name + "”",
      undo: () => {
        const now = latest.current?.id === id ? latest.current : null;
        const back = undoPayload(g.steps[0], first, now);
        return tackOnSteps(id, g.steps, back.at, g.name);
      },
    });
    toast(removedToast(g.name, wasHere, next), {
      action: {
        label: "Undo",
        ariaLabel: "Undo: put “" + g.name + "” back on the chain",
        // The same entry Ctrl+Z pops: whichever comes first runs it.
        onClick: () => {
          if (takeUndo(entry)) void entry.undo();
        },
      },
    });
    return true;
  };

  // A flight that LANDED stays drawn on the line (lib/flightDiagram
  // landedFlightOf) but its stops are read-only: removing one of them clears
  // that finished chain off the session (the owner couldn't delete a finished
  // Commit). Its record goes; there is nothing to undo into.
  const landedId = !flight && line.mode === "flight" && line.flightId ? line.flightId : "";
  const clearLanded = async (): Promise<boolean> => {
    if (!landedId) return false;
    const ok = await optimisticDelete(landedId, () => deleteFlight(landedId));
    if (ok) toast("Cleared the finished chain");
    return ok;
  };

  const removeNode = async (n: DiagramNode): Promise<boolean> => {
    if (!flight && landedId) return clearLanded();
    if (n.kind === "group") return removeGroup(n);
    return n.step && n.index !== undefined ? removeStop(n.step, n.index) : false;
  };

  // Delete / the ⋯ / 🗑 on the sky: at once, unless the flight is in the air
  // at this very stop — then the line asks first (lib/skySelect
  // `removalNeedsAsk`).
  const [removeAsk, setRemoveAsk] = useState<DiagramNode | null>(null);
  const requestRemove = (n: DiagramNode) => {
    if (opts.askRunning && removalNeedsAsk({ kind: "stops", indexes: nodeIndexes(n), flight })) setRemoveAsk(n);
    else void removeNode(n);
  };
  const askName = removeAsk ? (removeAsk.kind === "group" ? removeAsk.group?.name : removeAsk.step?.title) || "this stop" : "";
  const removeAskNode: ReactNode = removeAsk ? (
    <div className="skl-close-confirm skl-remove-confirm" data-no-drag>
      <InlineConfirm
        title={"Remove “" + askName + "”?"}
        body={"The chain is at it right now: it pauses before what follows. Undo puts it back."}
        confirmLabel="Remove"
        busy={busy}
        onConfirm={() => {
          const n = removeAsk;
          setRemoveAsk(null);
          void act(() => removeNode(n));
        }}
        onCancel={() => setRemoveAsk(null)}
      />
    </div>
  ) : null;

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
      } else await takeOff({ plan_id: plan.id, title, start_on: "prompt" });
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
    setPaletteHang(null);
    const shown = paletteAt !== null && paletteAt >= end;
    setPaletteAt(open ?? !shown ? end : null);
  };

  /** Round 18: the palette for a stop right after `after` (null: right after
   * the session — the first slot that can change), the new stop drawn at
   * `pos` (chain-local) when given. A wire dropped on empty sky. */
  const paletteAfter = (after: DiagramNode | null, pos?: { x: number; y: number } | null) => {
    setDialog(null);
    const slot = after ? loopSlotAfter(after) : line.insertFrom;
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
      <div className="fl-palette-head">Saved chains — tack one on as a group</div>
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
    // A landed chain's tiles come off too: that clears the finished chain.
    onRemove: flight ? (n) => requestRemove(own(n)) : landedId ? () => void clearLanded() : undefined,
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
    // A loop drawn by hand (only on a flight: the Loop needs stops to repeat).
    // The session box as `to` (its agent tile on the Sky): back to the AGENT
    // — `back_to: "agent"`, the agent runs again (round 21).
    onLoopFrom: flight
      ? (from, to) => {
          const def = kindOf.get("loop");
          const target = to.kind === "session" ? LOOP_AGENT : firstStepOf(to);
          const at = loopSlotAfter(from);
          if (!def) return void toast("This server has no Loop stop");
          if (!target || at === undefined) return;
          const said = target === LOOP_AGENT ? "Loops back to the agent — it runs again after “" + from.title + "”" : "Loops back to “" + target.title + "”";
          void tackOn(flight.id, loopStepTo(def, flight.steps.map((s) => s.id), target), at < end ? at : undefined).then((ok) => ok && toast(said));
        }
      : undefined,
    onLoopTo: flight
      ? (loop, to) => {
          const back = loopBackTo(to);
          if (!loop.step || !back || loop.step.config?.back_to === back) return;
          const said = back === LOOP_AGENT ? "Loops back to the agent now" : "Loops back to “" + (firstStepOf(to)?.title || to.title) + "” now";
          void patchStop(flight.id, loop.step.id, { config: { ...loop.step.config, back_to: back } }).then((ok) => ok && toast(said));
        }
      : undefined,
    onDropStop: (drop: StopDrop, at) => {
      if ("planId" in drop) {
        const plan = plans.find((p) => p.id === drop.planId);
        if (plan || flight) void tackPlan(plan || { id: drop.planId, name: "the saved chain" }, at);
        else toast("That saved chain isn't on this server any more");
        return;
      }
      const def = kindOf.get(drop.kind);
      if (def) void tackOne(def, at);
      else toast("This chain has no “" + drop.kind + "” stop to tack on");
    },
    renderInBox: parts.renderInBox,
    sizeOf: parts.sizeOf,
    reveal: currentNodeId(line, flight),
  };
  // The canvas counts a group box (and the flown pill) as one place.
  const shown = drawnCanvas(line, raw);
  // Round 21: a stop's ⋯ "Loop back to…" (the Sky's chain menu): the agent
  // first, then every stop at or before it — the line in flight indexes.
  const sessionBox = line.nodes.find((n) => n.kind === "session" && n.row === "main") || null;
  const loopChoicesOf = (n: DiagramNode): LoopChoice[] =>
    flight ? loopChoices(own(n), line.nodes.filter((m) => m.row === "main"), { session: sessionBox, insertFrom: line.insertFrom }) : [];

  const palette: ReactNode =
    paletteAt !== null ? (
      <StopPalette
        kinds={kinds}
        title={paletteAt >= end ? "Tack on a stop" : "Add a stop before “" + (flight?.steps[paletteAt]?.title || "") + "”"}
        onPick={(k) => void tackOne(k, paletteAt, true)}
        onClose={() => {
          setPaletteAt(null);
          setPaletteHang(null);
        }}
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
        note={open.editable ? undefined : open.state === "done" || open.state === "skipped" ? "This stop has finished." : "The chain is in this stop — only its stops still ahead can change."}
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
    const why = editable ? "" : flown ? "This stop has finished — it can't change now." : here ? "The chain is at this stop — it can't change while it is here." : "It can't change here.";
    const def = open.step ? inspectorDef(kindOf.get(open.step.kind), open.step) : undefined;
    inspector = (
      <StopDialog
        key={open.id}
        node={open}
        def={def}
        lineSteps={flight?.steps}
        loopAgent
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
            {!editable && stopLog(open, flight, rec, row)}
            {open.step?.kind === "split" && (
              <p className="fl-note muted fl-split-hint">Prompts: one worker per line — each line starts a worker. One line (or none) and Count says how many.</p>
            )}
            {offerJoin && (
              <div className="fl-chiprow fl-join-offer">
                <button type="button" className="fl-pick" disabled={busy} title="Put a Join right after this Split: the chain waits until every worker is done" onClick={() => void addJoin()}>
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
    /** A landed flight drawn on the line (read-only), "" when none. */
    landedId,
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
    /** Round 18: the line in the FLIGHT's indexes and its handlers (the Sky's
     * chain speaks them: lib/skyGraph `chainGraph` / `wireIntent`). */
    graphDiagram: line,
    graphCanvas: { ...raw, loopChoicesOf },
    paletteAfter,
    paletteParallel,
    /** Where the open palette / dialog hangs: a node id, "" = the end of the
     * line, null = nothing open; `pos` a spot asked for (chain-local). */
    anchor: inspector !== null && dialog ? dialog.id : palette !== null ? (paletteHang?.node ?? "") : null,
    anchorPos: palette !== null ? paletteHang?.pos ?? null : null,
    palette,
    /** The open stop's dialog (StopDialog), or null. */
    inspector,
    /** Asking before the stop the flight is at comes off (`askRunning`), or null. */
    removeAsk: removeAskNode,
    cancel,
  };
}
