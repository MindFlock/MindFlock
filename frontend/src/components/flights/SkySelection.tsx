/** The Sky's selection bar (docs/flights.md "As built: round 11 selection"):
 * what a marquee (or Ctrl/⌘+A) selected and what can be done with it. Drawn
 * by SkyView over the plane, screen-sized, just above the selection.
 *
 * Lines: Move (a hint — dragging any selected line's bar moves them all),
 * Tidy these, Remove… (inline-confirmed: a session's line closes its session,
 * a grounded line is deleted), Clear. Stops of one line: Group into a stop
 * and Save to the shelf (both ask a name inline), Remove (at once for up to
 * three, inline-confirmed past that), Clear. No window.confirm / prompt:
 * Electron has none. */

import { useEffect, useRef, useState, type JSX } from "react";
import type { Flight, StepKindDef } from "../../lib/flights";
import { planFromSteps } from "../../lib/flights";
import { flightStops } from "../../lib/flightDiagram";
import { deleteFlight, dropGroup, dropStop, groupStops, savePlan } from "../../lib/flightsApi";
import { groundedIdOf, isGroundedKey } from "../../lib/groundedLines";
import { closeSession } from "../../lib/sessionActions";
import { consecutive, removals, selectedIndexes, stopSelection, type SkySelection } from "../../lib/skySelect";
import { errMsg } from "../../lib/format";
import { toast } from "../../lib/toast";
import { InlineConfirm } from "../InlineConfirm";
import { TextField } from "./controls";
import "./SkySelection.css";

/** What the bar is asking: nothing, the Remove confirm, or a name (for Group
 * or Save). SkyView holds it, so Delete / Backspace can ask too. */
export type SelectionAsk = "" | "remove" | "group" | "save";

/** Stops removed past this many ask first. */
export const REMOVE_AT_ONCE = 3;

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

  const removeLines = () =>
    run(async () => {
      let gone = 0;
      for (const id of lines) {
        if (isGroundedKey(id)) {
          if (await deleteFlight(groundedIdOf(id))) gone++;
        } else await closeSession(id);
      }
      if (gone) toast("Removed " + plural(gone, "line") + " of stops");
    });

  const removeStops = () =>
    run(async () => {
      if (!one || !flight) return;
      let n = 0;
      for (const r of removals(one.nodes, flight.steps)) if (await (r.group ? dropGroup(flight.id, r.step) : dropStop(flight.id, r.step))) n++;
      if (n) toast("Removed " + plural(n, "stop"));
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

  /** Remove, pressed or Delete: up to three stops go at once. */
  const askRemove = () => {
    if (one && one.nodes.length <= REMOVE_AT_ONCE) void removeStops();
    else p.setAsk("remove");
  };
  // Delete / Backspace on the sky asks the same way.
  useEffect(() => {
    if (p.ask === "remove" && one && one.nodes.length <= REMOVE_AT_ONCE && !busy) void removeStops();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [p.ask]);

  if (!lines.length && !one) return null;

  const what = one ? plural(one.nodes.length, "stop") + " selected" : plural(lines.length, "line") + " selected";
  const sessions = lines.filter((id) => !isGroundedKey(id));
  const groundedLines = lines.filter(isGroundedKey);
  const names = (ids: string[]) => ids.slice(0, 4).map(p.nameOf).join(", ") + (ids.length > 4 ? " and " + (ids.length - 4) + " more" : "");

  return (
    <div
      className={"sky-selbar" + (p.ask ? " is-asking" : "")}
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
            <button type="button" className="test-btn fl-btn sky-selbar-danger" disabled={busy} title="Close their sessions, remove their stops" onClick={() => p.setAsk("remove")}>
              Remove…
            </button>
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
      {p.ask === "remove" && !one && (
        <InlineConfirm
          title={"Remove " + plural(lines.length, "line") + "?"}
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
      {p.ask === "remove" && one && one.nodes.length > REMOVE_AT_ONCE && (
        <InlineConfirm
          title={"Remove " + plural(one.nodes.length, "stop") + "?"}
          body={"They leave “" + p.nameOf(one.line) + "”. A stop that is flying halts the flight right before what follows."}
          confirmLabel="Remove"
          busy={busy}
          onConfirm={() => void removeStops()}
          onCancel={() => p.setAsk("")}
        />
      )}
    </div>
  );
}
