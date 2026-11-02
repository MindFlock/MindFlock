/** Round 18 (docs/flights.md "Round 18"): the stops draw as a CHAIN of
 * free-floating tiles under a small label (its handle, name and ⋯) — no card,
 * no "No session yet" box: headless (an empty socket on its first tile —
 * wire a session's out port there) or headed by the trigger tile of the chain
 * that hands off to it (`head`, lib/groundedLines `triggerHeadFor`). What
 * follows is the round 11–13 story, as the row was.
 *
 * A grounded flight on the Sky (docs/flights.md "Grounded flights"): stops
 * on the board with no session yet. They do nothing until one is attached.
 * Its own line, laid out under `"flight:" + id`: a title bar (the bird, its
 * name — the flight's `plan_name`, renamed inline: a click on it or F2 →
 * `renameFlight` — and a ⋯), a dashed
 * head box where the session will sit ("No session yet — drop a Session chip
 * here, or ⋯ Fly on a session"), then its stops on the canvas, every one
 * editable (useGroundedLine).
 *
 * The ⋯: **Add a stop…** (the palette), **Fly on a session…** (the host's
 * line picker → `attachGrounded`), **Save to the shelf** (a line that came
 * from a saved stop updates it, else a new saved stop named after the line),
 * **Remove** (at once, round 14: a grounded line never runs, so nothing to
 * ask — the host's `onRemove` deletes it with an Undo toast and a Ctrl+Z
 * entry). Delete or Backspace with the line itself focused removes it too. The host places it, drags
 * it by the bar (`onDragStart`) and gives it room from `onMeasure`, as it
 * does a session's line. `draggable` false (the sky's click mode): the bar
 * never starts a move. `registerDelete` hands the host this line's Delete
 * (docs/flights.md "As built: round 12 sky"): a stop or group box leaves
 * through the canvas's Remove, anything else (the head, the bar, the line)
 * removes the line. `merging`: dragged over a session's line, which
 * takes its stops on release (docs/flights.md "As built: round 13 sky").
 *
 * A chain with `flight.edits_default` EDITS an intake source's default stops
 * (the intake lane's stops chip made it). It is a pending edit, so it reads as
 * a draft at rest (GroundedLine.css: a dashed accent outline round the chain,
 * its bar always shown): the bar names the source ("Jira — payments",
 * lib/groundedLines `editorTitle`) with a "Default · <kind>" tag, and the
 * caption under it says "New items from <source> fly these stops". It has no
 * ▶ (lib/groundedLines `chainRunFor`) and no Run on a session…, and it is not
 * renamed. Its bar's **Save as default** (pressable once its stops differ from
 * the source's default — `defaultChanged`; "No changes" till then) writes its
 * steps as the source's own default (`setIntakeDefault`), **Reset to default**
 * (only while the source has its own) puts the source back on our built-in
 * stops — each takes the editor off the board with an Undo toast — and the
 * quiet **×** Discard (also ⋯, Delete) just takes it off. Its stops edit as
 * any grounded chain's. */

import { optimisticDelete } from "../../lib/flightOptimism";
import { useEffect, useMemo, useRef, useState, type JSX, type KeyboardEvent, type PointerEvent as ReactPointerEvent } from "react";
import type { Flight, FlightPlan, FlightStep, StepKindDef } from "../../lib/flights";
import { birdPerch, type DiagramNode } from "../../lib/flightDiagram";
import { chainGraph, type ChainHead, type GraphNode } from "../../lib/skyGraph";
import { attachGrounded, controlFlight, DEFAULT_SESSION, deleteFlight, getIntakeDefault, renameFlight, savePlan, setIntakeDefault, startGrounded } from "../../lib/flightsApi";
import { chainRunFor, defaultChanged, editorLabel, editorTitle, isDefaultEditor, isFreeRun, isRemoveKey, lastRunWords, planToSave, sourcePlanId } from "../../lib/groundedLines";
import { lodFor, nearLines, type SkyLineLayout } from "../../lib/skyLayout";
import { errMsg } from "../../lib/format";
import { toast } from "../../lib/toast";
import { OverflowMenu, type MenuItem } from "../OverflowMenu";
import { nodeById, removableBox } from "../../lib/skySelect";
import { pushUndo, undoAction } from "../../lib/skyUndo";
import { pillAimer, useChainMenu } from "./useChainMenu";
import { SkyChain } from "./SkyChain";
import { ChainPanels } from "./SkyWires";
import { HEAD, useChainEntry, useNodeMoves, useTourHooks, type ChainEntry } from "./useSkyWiring";
import { useGroundedLine } from "./useGroundedLine";
import { Bird } from "./Bird";
import "./SkyLine.css";
import "./GroundedLine.css";

export interface GroundedLineProps {
  flight: Flight;
  kinds: readonly StepKindDef[];
  plans: readonly FlightPlan[];
  layout: SkyLineLayout;
  focused: boolean;
  onFocus(): void;
  zoom: number;
  onDragStart?(e: ReactPointerEvent): void;
  onMeasure?(w: number, h: number): void;
  /** ⋯ Fly on a session…: the host asks which line. */
  onFlyOn(): void;
  /** A Session chip is being dragged over this line. */
  sessionOver?: boolean;
  /** In the Sky's selection, as a whole line. */
  selected?: boolean;
  /** Its stop boxes in the Sky's selection (node ids). */
  highlighted?: ReadonlySet<string>;
  /** Shift+click on a box: the host's multi-selection (SkySelection). */
  onShiftSelect?(n: DiagramNode): void;
  /** False in the sky's click mode: the bar never starts a move. */
  draggable?: boolean;
  /** The host's handle on this line's Delete (SkyLine's `registerDelete`). */
  registerDelete?(fn: ((node: string) => boolean) | null): void;
  /** Dragged by its bar over a session's line: the drop merges it there. */
  merging?: boolean;
  /** Remove the line (the host: delete, toast with Undo, a Ctrl+Z entry
   * that makes it again where it sat). Unset: deleted here, undoable. */
  onRemove?(): Promise<unknown> | void;
  /** Round 18: what starts this chain — a trigger tile when another chain
   * hands off to it (lib/groundedLines `triggerHeadFor`); none: no head, an
   * empty socket on its first tile. */
  head?: ChainHead | null;
  /** The host's handle on this chain (SkyLine's `registerChain`). */
  registerChain?(e: ChainEntry | null): void;
  /** A port pressed: the host runs the wire drag. */
  onPortDown?(node: GraphNode, side: "in" | "out", e: React.PointerEvent<HTMLElement>): void;
  /** The tiles a wire being drawn could land on, and the one under it. */
  targets?: ReadonlySet<string>;
  over?: string | null;
}

/** A chain with nothing to start it: no head tile. */
export const NO_HEAD: ChainHead = { kind: "none", title: "", sub: "", color: "var(--muted)" };

/** A grounded line's name as typed: trimmed, at most 120 characters (the
 * server's cap); "" = nothing to save. */
export const cleanFlightName = (s: string) => s.trim().slice(0, 120);

/** The name a grounded line shows. */
/** A stops-only chain's name: the one it was given, else what it holds — its
 * first stop's title (and how many more): "Commit", "Loop ×3 + 2 more". The
 * server's placeholder "Stops" counts as no name. */
export const groundedName = (f: Pick<Flight, "plan_name"> & { steps?: readonly { title: string }[] }) => {
  const given = (f.plan_name || "").trim();
  if (given && given !== "Stops") return given;
  const steps = f.steps || [];
  if (!steps.length) return "Stops";
  return steps[0].title + (steps.length > 1 ? " + " + (steps.length - 1) + " more" : "");
};

export function GroundedLine(p: GroundedLineProps): JSX.Element {
  const { flight } = p;
  const line = useGroundedLine(flight, { kinds: p.kinds, plans: p.plans });
  const [busy, setBusy] = useState(false);
  // The name being edited (null: shown), and where the bar's press began (a
  // click on the name renames; a drag that started there moves the line).
  const [naming, setNaming] = useState<string | null>(null);
  const pressAt = useRef<{ x: number; y: number } | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const measureRef = useRef(p.onMeasure);
  measureRef.current = p.onMeasure;
  // An intake source's default stops, edited here (the lane's stops chip):
  // never run — Save as default, Reset to default or Discard on its bar.
  const ed = isDefaultEditor(flight) ? flight.edits_default! : null;
  const edLabel = ed ? editorLabel(flight) : "";
  const name = ed ? "Default stops · " + edLabel : groundedName(flight);
  // Its bar names the source ("Jira — payments"); the kind rides the tag.
  const edTitle = editorTitle(edLabel);
  const edHint = "New items from " + (edTitle.name || "this source") + " fly these stops";
  const lod = lodFor(p.zoom || 1);
  // Whether the source has its own default now (Reset to default only then),
  // and the steps it flies now — Save as default only once these differ.
  const [ownDefault, setOwnDefault] = useState(false);
  const [baseSteps, setBaseSteps] = useState<FlightStep[] | null>(null);
  const edKind = ed?.kind;
  const edTarget = ed?.target;
  useEffect(() => {
    if (!edKind || !edTarget) return;
    let live = true;
    void getIntakeDefault({ kind: edKind, target: edTarget }).then((d) => {
      if (!live) return;
      setOwnDefault(!!d?.own);
      setBaseSteps(d ? d.steps : null);
    });
    return () => {
      live = false;
    };
  }, [edKind, edTarget]);
  // Unknown (not read yet, or unreadable): Save stays pressable.
  const changed = !baseSteps || defaultChanged(flight.steps, baseSteps);

  useEffect(() => {
    const el = rootRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    let last = "";
    const read = () => {
      const w = el.offsetWidth;
      const h = el.offsetHeight;
      const k = w + "x" + h;
      if (!w || !h || k === last) return;
      last = k;
      measureRef.current?.(w, h);
    };
    const ro = new ResizeObserver(read);
    ro.observe(el);
    read();
    return () => ro.disconnect();
  }, []);

  const save = async () => {
    const plan = planToSave(name, flight.steps, p.plans as FlightPlan[]);
    try {
      const saved = await savePlan(plan);
      toast((plan.id ? "Updated “" : "Saved “") + saved.name + "” on the shelf");
    } catch (err) {
      toast("Saving to the shelf failed: " + errMsg(err), { error: true });
    }
  };

  /** Save as default / Reset to default: write the source's default, take the
   * editor off the board, and say so with an Undo that puts back the saved
   * stop the source had (`previous_plan_id`). */
  const writeDefault = async (reset: boolean) => {
    if (!ed || busy) return;
    setBusy(true);
    try {
      const ref = { kind: ed.kind, target: ed.target };
      const res = await setIntakeDefault(reset ? { ...ref, plan_id: "" } : { ...ref, steps: flight.steps });
      if (!res) return;
      await optimisticDelete(flight.id, () => deleteFlight(flight.id));
      const label = res.label || edLabel;
      const prev = res.previous_plan_id || "";
      const entry = pushUndo({
        label: (reset ? "Reset the default stops for " : "Save the default stops for ") + label,
        undo: () => setIntakeDefault({ ...ref, plan_id: prev }),
      });
      toast(reset ? label + " is back on the built-in stops" : "New items from " + label + " fly these stops", {
        action: undoAction(entry, "Undo: " + label + " back on the stops it had"),
      });
    } finally {
      setBusy(false);
    }
  };
  /** Discard: the editor leaves the board; the default stays as it was. */
  const discard = async () => {
    if (busy) return;
    setBusy(true);
    try {
      await optimisticDelete(flight.id, () => deleteFlight(flight.id));
    } finally {
      setBusy(false);
    }
  };

  // Remove: at once, never asked — a grounded line has nothing in the air.
  const remove = async () => {
    if (ed) return discard();
    if (busy) return;
    setBusy(true);
    try {
      if (p.onRemove) await p.onRemove();
      else if (await optimisticDelete(flight.id, () => deleteFlight(flight.id))) {
        const steps = flight.steps;
        const entry = pushUndo({ label: "Remove “" + name + "”", undo: () => startGrounded({ steps, ...(flight.plan_name ? { name: flight.plan_name } : {}) }) });
        toast("Removed “" + name + "”", { action: undoAction(entry, "Undo: put “" + name + "” back on the board") });
      }
    } finally {
      setBusy(false);
    }
  };

  const updates = !!sourcePlanId(flight.steps, p.plans);
  const items: MenuItem[] = [
    { key: "add", label: "Add a stop…", title: "One stop, or a saved stop, at the end of the line", onSelect: () => line.togglePalette(true) },
    ...(ed ? [] : [{ key: "fly", label: "Run on a session…", title: "Pick a session (or a new one): these stops run in it", onSelect: p.onFlyOn }]),
    {
      key: "save",
      label: "Save to the shelf",
      title: updates ? "Update the saved stop these came from" : "Keep these stops on the shelf as a saved stop",
      className: "skl-mi-group",
      onSelect: () => void save(),
    },
    ed
      ? { key: "remove", label: "Discard", danger: true, className: "skl-mi-group", title: "Take this editor off the board — the default stops stay as they are", onSelect: () => void discard() }
      : { key: "remove", label: "Remove", danger: true, className: "skl-mi-group", title: "Take these stops off the board (Ctrl+Z puts them back)", onSelect: () => void remove() },
  ];

  // The edit ends once: Enter or Esc hands the focus back to the line, and
  // the input's blur that follows must not save it again.
  const named = useRef(false);
  const startRename = () => {
    if (ed) return;
    named.current = false;
    setNaming(name);
  };
  /** Enter or blur: the new name, if it changed; Esc: as it was. */
  const endRename = (save: boolean) => {
    if (named.current) return;
    named.current = true;
    const next = cleanFlightName(naming ?? "");
    setNaming(null);
    rootRef.current?.focus({ preventScroll: true });
    if (save && next && next !== flight.plan_name) void renameFlight(flight.id, next);
  };
  const onNameClick = (e: React.MouseEvent) => {
    const at = pressAt.current;
    pressAt.current = null;
    if (at && Math.hypot(e.clientX - at.x, e.clientY - at.y) >= 4) return;
    startRename();
  };

  const onBarDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    pressAt.current = { x: e.clientX, y: e.clientY };
    if ((e.target as Element).closest("button, summary, a, input, [data-no-drag]")) return;
    rootRef.current?.focus({ preventScroll: true });
    if (p.draggable === false) return;
    p.onDragStart?.(e);
  };
  // Delete from the sky: a stop or group box through the canvas's Remove;
  // anything else on the line asks to remove the line.
  const deleteBox = (node: string): boolean => {
    const n = node && node !== HEAD ? nodeById(line.graphDiagram.nodes, node) : null;
    if (n && (n.kind === "stop" || n.kind === "group")) {
      if (!removableBox(n) || !line.graphCanvas.onRemove) return false;
      line.graphCanvas.onRemove(n);
      return true;
    }
    void remove();
    return true;
  };
  const deleteRef = useRef(deleteBox);
  deleteRef.current = deleteBox;
  const register = p.registerDelete;
  useEffect(() => {
    if (!register) return;
    register((node) => deleteRef.current(node));
    return () => register(null);
  }, [register]);
  // Delete / Backspace on the line itself (not a stop on it, not a field).
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.target !== e.currentTarget) return;
    if (e.key === "F2") {
      e.preventDefault();
      e.stopPropagation();
      startRename();
      return;
    }
    if (!isRemoveKey(e.key)) return;
    e.preventDefault();
    e.stopPropagation();
    void remove();
  };

  // Round 18: the stops are a CHAIN of free-floating tiles (lib/skyGraph):
  // headless (an empty socket on the first tile: wire a session's out port
  // there) or headed by the trigger tile of the chain that hands off to it.
  const wrapRef = useRef<HTMLDivElement>(null);
  const moves = useNodeMoves(flight.id, line.graphDiagram, "flight:" + flight.id);
  const head = p.head || NO_HEAD;
  const graph = useMemo(() => chainGraph(moves.diagram, { head, key: "flight:" + flight.id, flightId: flight.id }), [moves.diagram, head, flight.id]);
  useChainEntry(p.registerChain, graph, wrapRef, line.paletteAfter, line.paletteParallel);
  useTourHooks(wrapRef, { ".sky-socket": "sky-grounded" });
  const gc = line.graphCanvas;
  // Round 18: a tile's right-click menu; a Loop's pill dragged onto a stop re-aims it.
  const chainMenu = useChainMenu(gc, flight.id, { steps: flight.steps, kinds: p.kinds });
  const onPillDown = pillAimer(graph, gc.onLoopTo);
  const shift = useRef(false);
  // A chain with no session runs on its own in a folder — nothing is started
  // for it (only a stop that needs a session gets one, made in that folder).
  const running = isFreeRun(flight);
  // No ▶ on a default-stops editor (it never runs), nor on a chain another starts.
  const run = chainRunFor(flight, head.kind, () => ({
    running,
    label: running ? "Stop this run" : "Run these stops (⋯ Run on a session… to pick where)",
    onRun: () =>
      void attachGrounded(flight.id, { title: DEFAULT_SESSION }).then((f) => {
        if (!f) return;
        toast(f.session ? "Running “" + name + "” in a new session, " + f.session : "Running “" + name + "”");
      }),
    onStop: () => void controlFlight(flight.id, "cancel"),
  }));

  return (
    <div
      ref={rootRef}
      className={
        "skl skl-chainline skl-grounded" +
        (head.kind === "trigger" ? " is-triggered" : "") +
        (p.focused ? " is-focused" : "") +
        (p.selected ? " is-selected" : "") +
        (p.sessionOver ? " is-session-over" : "") +
        (p.merging ? " is-merging" : "") +
        (line.editing ? " is-editing" : "") +
        (ed ? " is-default-editor" : "")
      }
      data-flight={flight.id}
      data-edits-default={ed ? ed.kind + ":" + ed.target : undefined}
      data-lod={lod}
      style={{ "--sky-z": p.zoom || 1, "--sky-lines": nearLines(p.zoom || 1) } as React.CSSProperties}
      role="group"
      tabIndex={0}
      data-sky-box
      aria-label={name + (ed ? " — new items from this source fly these" : head.kind === "trigger" ? " — " + head.title : " — stops with no session yet")}
      onKeyDown={onKey}
      onPointerDownCapture={() => {
        if (!p.focused) p.onFocus();
      }}
    >
      {/* The chain's label: its handle (drag, also onto a session's chain to
          give it the stops), its name (click to rename) and its ⋯. */}
      <div
        className="skl-bar skl-label"
        data-tour="sky-grounded"
        title={p.draggable === false ? "Click to focus these stops (drag mode moves them: V)" : "Drag to move these stops — onto a session's chain to give them to it"}
        onPointerDown={onBarDown}
      >
        <Bird size={14} />
        {ed ? (
          <>
            <span className="skl-name skl-default-name" title={"Default stops for " + edLabel + ". New items from this source fly these."}>
              {edTitle.name}
            </span>
            <span className="fl-box-chip skl-chip skl-default-tag" title={"You are editing the stops new items from " + edLabel + " fly"}>
              {edTitle.kind ? "Default · " + edTitle.kind : "Default"}
            </span>
          </>
        ) : naming === null ? (
          <span className="skl-name skl-name-edit" title="Click to rename (F2)" onClick={onNameClick}>
            {name}
          </span>
        ) : (
          <input
            className="fl-input skl-name-input"
            aria-label={"Rename “" + name + "”"}
            value={naming}
            maxLength={120}
            autoFocus
            onChange={(e) => setNaming(e.target.value)}
            onFocus={(e) => e.target.select()}
            onBlur={() => endRename(true)}
            onKeyDown={(e) => {
              e.stopPropagation();
              if (e.key === "Enter") {
                e.preventDefault();
                endRename(true);
              } else if (e.key === "Escape") {
                e.preventDefault();
                endRename(false);
              }
            }}
          />
        )}
        {/* A block on its own is just a block (the owner: it must not look as if
            it waits for a session) — only a chain another chain starts says so. */}
        {head.kind === "trigger" && (
          <span className="fl-box-chip skl-chip tone-working" title={head.title + " — " + head.sub}>
            Started by a chain
          </span>
        )}
        {/* No Run label up here: the chain's ▶ sits on the left of its first
            stop, where a run starts (n8n's trigger button) — SkyChain `run`. */}
        {running && (
          <span className={"fl-box-chip skl-chip " + (flight.state === "waiting_you" ? "tone-you" : flight.state === "running" ? "tone-working" : "tone-paused")} title={flight.repo_root ? "Running in " + flight.repo_root : undefined}>
            {flight.state === "waiting_you" ? "Waiting on you" : flight.state === "running" ? "Running" : "Paused"}
          </span>
        )}
        {!running && flight.last_run && (() => {
          const w = lastRunWords(flight.last_run);
          return (
            <span className={"fl-box-chip skl-chip " + (flight.last_run.state === "done" ? "tone-ready" : "tone-muted")} title={w.title}>
              {w.label}
            </span>
          );
        })()}
        {/* An editor is a pending edit: its bar stays shown, Save as default
            the one primary (pressable once the stops differ from the source's
            default), Reset a quiet secondary, Discard a quiet ×. */}
        {ed && (
          <span className="skl-default-acts" data-no-drag>
            <button
              type="button"
              className="btn-primary fl-btn skl-default-save"
              disabled={busy || !flight.steps.length || !changed}
              title={changed ? edHint : "These are the stops " + edLabel + " flies now — change one to save"}
              onClick={() => void writeDefault(false)}
            >
              {changed ? "Save as default" : "No changes"}
            </button>
            {ownDefault && (
              <button type="button" className="test-btn fl-btn skl-default-reset" disabled={busy} title={"Put " + edLabel + " back on our built-in stops"} onClick={() => void writeDefault(true)}>
                Reset to default
              </button>
            )}
            <button type="button" className="fl-icon-btn skl-default-discard" disabled={busy} aria-label="Discard this edit" title="Discard — take this editor off the board; the default stops stay as they are" onClick={() => void discard()}>
              ×
            </button>
          </span>
        )}
        <span className="skl-menu" data-no-drag>
          <OverflowMenu label={"Actions for " + name} items={items} />
        </span>
      </div>
      {ed && <div className="skl-default-hint">{edHint}</div>}
      <div ref={wrapRef} className="skl-chain-wrap" onPointerDownCapture={(e) => (shift.current = e.shiftKey)}>
        <SkyChain
          graph={graph}
          zoom={p.zoom}
          selected={gc.selected}
          highlighted={p.highlighted}
          targets={p.targets}
          over={p.over}
          bird={birdPerch(moves.diagram)}
          onSelect={(n) => (shift.current && p.onShiftSelect ? p.onShiftSelect(n) : gc.onSelect?.(n))}
          onEdit={gc.onEdit}
          onOpen={gc.onOpen}
          onRemove={gc.onRemove}
          onLoopPill={(via) => gc.onEdit?.(via)}
          onPillDown={onPillDown}
          onMenu={chainMenu.onMenu}
          kinds={p.kinds}
          onNodeMove={moves.move}
          onPortDown={p.onPortDown}
          onPlus={(after) => (after && after.id !== HEAD ? line.paletteAfter(after.node, null) : line.togglePalette(true))}
          run={run}
        />
        {chainMenu.menu}
          <ChainPanels graph={graph} anchor={line.anchor} anchorPos={line.anchorPos}>
          {line.palette}
          {line.inspector}
        </ChainPanels>
      </div>
    </div>
  );
}
