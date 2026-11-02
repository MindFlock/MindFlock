/** The Sky's selection bar (docs/flights.md "As built: round 11 selection"):
 * what a marquee (or Ctrl/⌘+A) selected and what can be done with it. Drawn
 * by SkyView over the plane, screen-sized, just above the selection.
 *
 * Lines: Move (a hint — dragging any selected line's bar moves them all),
 * Tidy these, Remove (a session's line closes its session, a grounded line is
 * deleted), Clear. Stops of one line: Group into a stop and Save to the shelf
 * (both ask a name inline), Remove, Clear. Remove goes at once with ONE Undo
 * for the lot (the toast and Ctrl+Z, lib/skyUndo); it asks inline only when
 * the selection holds something running — a session whose agent works or
 * asks, the stop a running flight is at — and the question names it (round
 * 14, lib/skySelect `removalNeedsAsk`). No window.confirm / prompt: Electron
 * has none. The intake lane can be among the lines (round 12): it moves and
 * tidies with them, and Remove leaves it alone. */

import { optimisticDelete, patchFlightCache, withoutSteps } from "../../lib/flightOptimism";
import { useEffect, useRef, useState, type JSX } from "react";
import type { Flight, FlightStep, IntakeSourceRef, StepKindDef } from "../../lib/flights";
import { isLive, planFromSteps } from "../../lib/flights";
import { flightStops } from "../../lib/flightDiagram";
import { deleteFlight, dropGroup, dropStop, groupStops, savePlan, startGrounded, tackOn, tackOnNew, tackOnSteps } from "../../lib/flightsApi";
import { groundedIdOf, isGroundedKey } from "../../lib/groundedLines";
import { closeSession } from "../../lib/sessionActions";
import { INTAKE_KEY, type Pos } from "../../lib/skyLayout";
import { consecutive, removalNeedsAsk, selectedIndexes, sessionRuns, stepIndexesOf, stopSelection, stopsRun, type SkySelection } from "../../lib/skySelect";
import { pushUndo, undoAction } from "../../lib/skyUndo";
import { errMsg } from "../../lib/format";
import { toast } from "../../lib/toast";
import { queryClient } from "../../state/queries";
import { reopenClosed, runningWord, sessionOpen } from "./SkyLine";
import { InlineConfirm } from "../InlineConfirm";
import { TextField } from "./controls";
import "./SkySelection.css";

/** What the bar is asking: nothing, the Remove confirm, or a name (for Group
 * or Save). SkyView holds it, so Delete / Backspace can ask too. */
export type SelectionAsk = "" | "remove" | "group" | "save";

/** One stop or group a Remove took off, enough to put it back: its first
 * step index, its steps, and a group's name ("" = a lone stop). */
export interface TakenStops {
  index: number;
  steps: FlightStep[];
  group: string;
}

/** What the selected stop nodes stand for on `steps`, first index first —
 * the order Undo tacks them back in. */
export function takenStops(nodes: Iterable<string>, steps: readonly FlightStep[]): TakenStops[] {
  const out: TakenStops[] = [];
  for (const n of nodes) {
    const idx = stepIndexesOf(n, steps);
    if (!idx.length) continue;
    const group = n.startsWith("group:") ? steps[idx[0]].group?.name || "" : "";
    out.push({ index: idx[0], steps: group ? idx.map((i) => steps[i]) : [steps[idx[0]]], group });
  }
  return out.sort((a, b) => a.index - b.index);
}

/** Where Undo tacks each taken item back (in `takenStops` order): its old
 * index while that is still ahead of the flight's `cursor` and inside the
 * line as it grows back (`length` + what went back already), else the end
 * (undefined) — and once one goes to the end, so do the rest, so they keep
 * their order and none lands inside another. */
export function putBackAt(taken: readonly Pick<TakenStops, "index" | "steps">[], flight: { cursor: number; length: number }): Array<number | undefined> {
  let len = flight.length;
  let atEnd = false;
  return taken.map((t) => {
    atEnd ||= !(t.index > flight.cursor && t.index < len);
    len += t.steps.length;
    return atEnd ? undefined : t.index;
  });
}

/** A flight as the page knows it now (an Undo pressed minutes later). */
const flightNow = (id: string): Flight | null => (queryClient.getQueryData<Flight[] | null>(["flights"]) || []).find((f) => f.id === id) || null;

/** The name a new group or saved stop gets unless the owner types one. */
export const NEW_STOPS = "New stops";

export interface SkySelectionProps {
  selection: SkySelection;
  /** Where the bar's top-left sits (viewport px). */
  x: number;
  y: number;
  ask: SelectionAsk;
  setAsk(a: SelectionAsk): void;
  /** A line's shown name. */
  nameOf(id: string): string;
  /** The flight a line's stops are on (a session's, a grounded line's own). */
  flightOf(id: string): Flight | null;
  kinds: readonly StepKindDef[];
  /** Tidy these: their spots forgotten. */
  onTidy(ids: string[]): void;
  onClear(): void;
  /** A session line's row (is its agent working or asking?). */
  rowOf?(id: string): { activity?: string; asking?: unknown } | undefined;
  /** Where a line sits now (plane px): a grounded line put back lands there. */
  posOf?(id: string): Pos | undefined;
  /** Make a removed grounded line again from its stops, pinned at `at`. */
  restoreGrounded?(steps: FlightStep[], name: string, at: Pos | undefined, editsDefault?: IntakeSourceRef): Promise<unknown>;
}

const plural = (n: number, one: string, many = one + "s") => n + " " + (n === 1 ? one : many);

/** Why Group can't run on these steps of `f` (or "" when it can). */
export function groupHint(f: Flight | null, idx: readonly number[], kinds?: readonly StepKindDef[]): string {
  if (!f || !idx.length) return "These stops aren't on a flight any more";
  if (!consecutive(idx)) return "Pick stops that sit next to each other";
  const stops = flightStops(f, kinds);
  if (idx.some((i) => !stops[i]?.editable)) return "A stop that has flown, or is flying, can't join a group";
  const gid = f.steps[idx[0]]?.group?.id;
  if (gid && f.steps.every((s, i) => (s.group?.id === gid) === idx.includes(i))) return "These already are one stop";
  return "";
}

export function SkySelectionBar(p: SkySelectionProps): JSX.Element | null {
  const [name, setName] = useState(NEW_STOPS);
  const [busy, setBusy] = useState(false);
  const one = stopSelection(p.selection);
  const lines = [...p.selection.lines];
  // What Remove takes: every line but the intake lane.
  const removable = lines.filter((id) => id !== INTAKE_KEY);
  const flight = one ? p.flightOf(one.line) : null;
  const idx = one && flight ? selectedIndexes(one.nodes, flight.steps) : [];
  const hint = one ? groupHint(flight, idx, p.kinds) : "";

  // A fresh question starts from the default name, selected in its field.
  const askRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (p.ask !== "group" && p.ask !== "save") return;
    setName(NEW_STOPS);
    const input = askRef.current?.querySelector("input");
    input?.focus({ preventScroll: true });
    input?.select();
  }, [p.ask]);

  const run = async (what: () => Promise<void>) => {
    setBusy(true);
    try {
      await what();
    } finally {
      setBusy(false);
      p.setAsk("");
      p.onClear();
    }
  };

  // Remove asks only when the selection holds something running, and the
  // question names it: the sessions whose agents work or ask, the stop the
  // flight is at.
  const runningLines = removable.filter((id) => !isGroundedKey(id) && sessionRuns(p.rowOf?.(id)));
  const runningStop = one && flight && stopsRun(idx, flight) ? flight.steps[flight.cursor]?.title || "a stop" : "";
  const asks = one
    ? removalNeedsAsk({ kind: "stops", indexes: idx, flight })
    : removalNeedsAsk({ kind: "selection", items: removable.map((id) => (isGroundedKey(id) ? { kind: "grounded" as const } : { kind: "session" as const, row: p.rowOf?.(id) })) });

  const removeLines = () =>
    run(async () => {
      const closed: string[] = [];
      const gone: Array<{ steps: FlightStep[]; name: string; at: Pos | undefined; ed?: IntakeSourceRef }> = [];
      for (const id of removable) {
        if (isGroundedKey(id)) {
          const f = p.flightOf(id);
          const at = p.posOf?.(id);
          if (await optimisticDelete(groundedIdOf(id), () => deleteFlight(groundedIdOf(id)))) gone.push({ steps: f?.steps || [], name: f?.plan_name || "", at, ed: f?.edits_default });
        } else {
          await closeSession(id);
          if (!sessionOpen(id)) closed.push(id);
        }
      }
      const n = gone.length + closed.length;
      if (!n) return;
      // ONE entry puts the lot back: the grounded lines where they sat, the
      // sessions from Recently closed.
      const entry = pushUndo({
        label: "Remove " + plural(n, "line"),
        undo: async () => {
          for (const g of gone) if (g.steps.length) await (p.restoreGrounded ? p.restoreGrounded(g.steps, g.name, g.at, g.ed) : startGrounded({ steps: g.steps, ...(g.name ? { name: g.name } : {}), ...(g.ed ? { edits_default: g.ed } : {}) }));
          await reopenClosed(closed);
        },
      });
      const said = [closed.length ? "closed " + plural(closed.length, "session") : "", gone.length ? "removed " + plural(gone.length, "line") + " of stops" : ""].filter(Boolean).join(", ");
      toast(said.charAt(0).toUpperCase() + said.slice(1), { action: undoAction(entry, "Undo: put " + plural(n, "line") + " back") });
    });

  const removeStops = () =>
    run(async () => {
      if (!one || !flight) return;
      const fid = flight.id;
      const title = isGroundedKey(one.line) ? "" : one.line;
      const taken = takenStops(one.nodes, flight.steps);
      const out: TakenStops[] = [];
      // Every taken stop leaves the board at once; any the server refuses come back.
      const rollback = patchFlightCache(fid, withoutSteps(new Set(taken.flatMap((t) => t.steps.map((s) => s.id)))));
      for (const t of taken) if (await (t.group ? dropGroup(fid, t.steps[0].id) : dropStop(fid, t.steps[0].id))) out.push(t);
      if (out.length < taken.length) rollback();
      if (!out.length) return;
      const n = out.length;
      const entry = pushUndo({
        label: "Remove " + plural(n, "stop"),
        undo: async () => {
          const now = flightNow(fid);
          // The flight went with its last stop: the stops come back as a
          // grounded line, or as a new flight on the session.
          if (!now || (!isLive(now) && now.state !== "grounded")) {
            const steps = out.flatMap((t) => t.steps);
            if (title) return tackOnNew(title, steps);
            return p.restoreGrounded ? p.restoreGrounded(steps, flight.plan_name || "", p.posOf?.(one.line)) : startGrounded({ steps });
          }
          const ats = putBackAt(out, { cursor: now.cursor, length: now.steps.length });
          for (let i = 0; i < out.length; i++) {
            const t = out[i];
            if (t.group) await tackOnSteps(fid, t.steps, ats[i], t.group);
            else await tackOn(fid, t.steps[0], ats[i]);
          }
        },
      });
      toast("Removed " + plural(n, "stop"), { action: undoAction(entry, "Undo: put " + plural(n, "stop") + " back") });
    });

  const group = () =>
    run(async () => {
      if (!flight || hint) return;
      await groupStops(flight.id, idx.map((i) => flight.steps[i].id), name.trim() || NEW_STOPS);
    });

  const save = () =>
    run(async () => {
      if (!flight || !idx.length) return;
      const plan = planFromSteps(name.trim() || NEW_STOPS, idx.map((i) => flight.steps[i]));
      try {
        const saved = await savePlan({ ...plan, id: "", updated: 0 });
        toast("Saved “" + saved.name + "” on the shelf");
      } catch (err) {
        toast("Saving to the shelf failed: " + errMsg(err), { error: true });
      }
    });

  /** Remove, pressed or Delete: at once, unless something in it runs. */
  const removeNow = () => (one ? removeStops() : removeLines());
  const askRemove = () => {
    if (asks) p.setAsk("remove");
    else void removeNow();
  };
  // Delete / Backspace on the sky asks the same way.
  useEffect(() => {
    if (p.ask === "remove" && !asks && !busy) void removeNow();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [p.ask]);

  if (!lines.length && !one) return null;

  const what = one ? plural(one.nodes.length, "stop") + " selected" : plural(lines.length, "line") + " selected";
  const sessions = removable.filter((id) => !isGroundedKey(id));
  const groundedLines = removable.filter(isGroundedKey);
  const names = (ids: string[]) => ids.slice(0, 4).map(p.nameOf).join(", ") + (ids.length > 4 ? " and " + (ids.length - 4) + " more" : "");

  return (
    <div
      className={"sky-selbar" + (p.ask ? " is-asking" : "")}
      data-tour="sky-selbar"
      role="toolbar"
      aria-label={what}
      style={{ transform: `translate(${Math.round(p.x)}px, ${Math.round(p.y)}px)` }}
      onPointerDown={(e) => e.stopPropagation()}
      onDoubleClick={(e) => e.stopPropagation()}
    >
      <div className="sky-selbar-row">
        <span className="sky-selbar-count">{what}</span>
        {one ? (
          <>
            <button type="button" className="test-btn fl-btn" disabled={busy || !!hint} title={hint || "Make these one stop with sub-steps"} onClick={() => p.setAsk("group")}>
              Group into a stop
            </button>
            <button type="button" className="test-btn fl-btn" disabled={busy || !idx.length} title="Keep these stops on the shelf as a saved stop" onClick={() => p.setAsk("save")}>
              Save to the shelf
            </button>
            <button type="button" className="test-btn fl-btn sky-selbar-danger" disabled={busy || !flight} title="Take these stops off the line" onClick={askRemove}>
              Remove
            </button>
          </>
        ) : (
          <>
            <span className="sky-selbar-hint" title="Drag the bar of any selected line: they all move together">
              ✥ Drag a bar to move them
            </span>
            <button type="button" className="test-btn fl-btn" disabled={busy} title="Forget where you put them: back to their places in the rows" onClick={() => p.onTidy(lines)}>
              Tidy these
            </button>
            {removable.length > 0 && (
              <button type="button" className="test-btn fl-btn sky-selbar-danger" disabled={busy} title="Close their sessions, remove their stops (Ctrl+Z puts them back)" onClick={askRemove}>
                Remove
              </button>
            )}
          </>
        )}
        <button type="button" className="linklike sky-selbar-clear" title="Clear the selection (Esc)" onClick={p.onClear}>
          Clear
        </button>
      </div>
      {one && hint && hint !== "These stops aren't on a flight any more" && <p className="sky-selbar-note muted">{hint}</p>}
      {(p.ask === "group" || p.ask === "save") && one && (
        <div ref={askRef} className="sky-selbar-ask">
          <TextField value={name} onChange={setName} label={p.ask === "group" ? "The new stop's name" : "The saved stop's name"} placeholder={NEW_STOPS} onEnter={() => void (p.ask === "group" ? group() : save())} />
          <button type="button" className="btn-primary fl-btn" disabled={busy} onClick={() => void (p.ask === "group" ? group() : save())}>
            {p.ask === "group" ? "Group" : "Save"}
          </button>
          <button type="button" className="linklike" onClick={() => p.setAsk("")}>
            Cancel
          </button>
        </div>
      )}
      {p.ask === "remove" && asks && !one && removable.length > 0 && (
        <InlineConfirm
          title={"Remove " + plural(removable.length, "line") + "? " + names(runningLines) + (runningLines.length === 1 ? " " + runningWord(p.rowOf?.(runningLines[0])) : " are running") + "."}
          body={
            <>
              {sessions.length > 0 && (
                <span className="sky-selbar-says">
                  {plural(sessions.length, "session")} {sessions.length === 1 ? "closes" : "close"} ({names(sessions)}): {sessions.length === 1 ? "its agent stops and it moves" : "their agents stop and they move"} to Recently closed.
                </span>
              )}
              {groundedLines.length > 0 && (
                <span className="sky-selbar-says">
                  {plural(groundedLines.length, "line")} of stops with no session {groundedLines.length === 1 ? "leaves" : "leave"} the board ({names(groundedLines)}). A saved stop on the shelf stays.
                </span>
              )}
            </>
          }
          confirmLabel="Remove"
          busy={busy}
          onConfirm={() => void removeLines()}
          onCancel={() => p.setAsk("")}
        />
      )}
      {p.ask === "remove" && asks && one && (
        <InlineConfirm
          title={"Remove " + plural(one.nodes.length, "stop") + "? The flight is at “" + runningStop + "” right now."}
          body={"They leave “" + p.nameOf(one.line) + "”, and the flight pauses right before what follows. Ctrl+Z puts them back."}
          confirmLabel="Remove"
          busy={busy}
          onConfirm={() => void removeStops()}
          onCancel={() => p.setAsk("")}
        />
      )}
    </div>
  );
}
