/** The shelf's last row: the stop kinds as chips (the registry's, read with
 * `useFlightKinds`), the flock's stops first and the perches last in their
 * gold, split by a thin divider as the stop palette groups them. DRAG a chip
 * onto a line: it carries the kind under `STOP_MIME` (and its name as plain
 * text) and the sky owns the drop — the gap it lands in lights up and the stop
 * is tacked on there; dropped into a flyway row it joins that flyway (saved
 * at once), or on the empty row it starts one. CLICK one (or tap it on a
 * phone, where nothing drags) → `onTack(kind)`: the host tacks it onto the
 * focused line. Without `onTack` a chip only drags. Styles live in
 * FlywayShelf.css. */

import type { DragEvent, JSX } from "react";
import { STOP_MIME, type StepKindDef } from "../../lib/flights";
import { useFlightKinds } from "../../state/queries";
import { StopIcon } from "./Bird";
import { kindName } from "./StopParts";
import "./FlywayShelf.css";

/** The chips' order: the flock's stops as the registry lists them, then the
 * perches. */
export function stopChipOrder<K extends Pick<StepKindDef, "actor">>(kinds: readonly K[]): K[] {
  return [...kinds.filter((k) => k.actor !== "you"), ...kinds.filter((k) => k.actor === "you")];
}

/** What a stop chip's drag carries: the kind under STOP_MIME, and its name as
 * plain text for anything else it is dropped on. */
export function stopDragData(def: Pick<StepKindDef, "kind" | "label">): Array<[string, string]> {
  return [
    [STOP_MIME, def.kind],
    ["text/plain", kindName(def)],
  ];
}

function onStopDrag(e: DragEvent, def: StepKindDef) {
  for (const [type, value] of stopDragData(def)) e.dataTransfer.setData(type, value);
  e.dataTransfer.effectAllowed = "copy";
}

function StopChip({ def, onTack }: { def: StepKindDef; onTack?(kind: string): void }) {
  const name = kindName(def);
  const perch = def.actor === "you";
  const how = onTack ? "Click to tack it onto the focused line, or drag it onto a line" : "Drag onto a line";
  const props = {
    className: "sky-stop-chip" + (perch ? " perch" : "") + (onTack ? "" : " drag-only"),
    "data-kind": def.kind,
    draggable: true,
    title: (def.summary ? def.summary + "\n" : "") + how,
    onDragStart: (e: DragEvent) => onStopDrag(e, def),
  };
  const body = (
    <>
      <span className="sky-stop-icon">
        <StopIcon kind={perch ? "perch" : def.kind} size={14} />
      </span>
      <span className="sky-stop-name">{name}</span>
    </>
  );
  return (
    <span role="listitem" className="sky-stop-item">
      {onTack ? (
        <button type="button" {...props} aria-label={"Tack “" + name + "” onto the focused line"} onClick={() => onTack(def.kind)}>
          {body}
        </button>
      ) : (
        <span {...props}>{body}</span>
      )}
    </span>
  );
}

export function StopShelf({ onTack }: { onTack?(kind: string): void }): JSX.Element {
  const kinds = useFlightKinds().data;
  const ordered = stopChipOrder(kinds || []);
  const flock = ordered.filter((k) => k.actor !== "you");
  const perches = ordered.filter((k) => k.actor === "you");
  return (
    <div className="sky-stops" role="group" aria-label="Stops">
      <span className="sky-stops-head">
        <strong>Stops</strong> <span className="sky-stops-hint">— drag onto a line, or into a flyway</span>
      </span>
      {kinds !== undefined && ordered.length === 0 ? (
        <p className="sky-stops-empty muted">The stop list didn't load — this server may not know Flights yet.</p>
      ) : (
        <div className="sky-stops-chips" role="list">
          {flock.map((k) => (
            <StopChip key={k.kind} def={k} onTack={onTack} />
          ))}
          {flock.length > 0 && perches.length > 0 && <span className="sky-stops-divider" aria-hidden="true" />}
          {perches.map((k) => (
            <StopChip key={k.kind} def={k} onTack={onTack} />
          ))}
        </div>
      )}
    </div>
  );
}
