/** One row of a Customize page — the same shape on both pages: a ⠿ grip to
 * drag it, a switch, the item's name, one line saying what it is (or when it
 * shows), and ↑ ↓ for the keyboard. A drop lands the dragged row where this
 * one is. DOM only (no window.prompt / confirm: the desktop app has none). */

import { useState, type ReactNode } from "react";

/** The drag payload type: rows only accept rows from their own list. */
const ROW_MIME = "application/x-mf-customize-row";

export function CustomizeRow({
  id,
  list,
  label,
  note,
  on,
  locked,
  lockedWhy,
  onToggle,
  onMove,
  onDropBefore,
  first,
  last,
}: {
  id: string;
  /** Which list the row is in: a drop from another list is ignored. */
  list: string;
  label: string;
  note: ReactNode;
  on: boolean;
  /** Can't be switched off (Agent). */
  locked?: boolean;
  lockedWhy?: string;
  onToggle(): void;
  onMove(dir: -1 | 1): void;
  /** A row of the same list was dropped on this one. */
  onDropBefore(dragged: string): void;
  first: boolean;
  last: boolean;
}) {
  const [over, setOver] = useState(false);
  const inputId = "cz-" + list + "-" + id;
  return (
    <li
      className={"cz-row" + (on ? "" : " is-off") + (over ? " drop-over" : "")}
      data-item={id}
      onDragOver={(e) => {
        if (!e.dataTransfer.types.includes(ROW_MIME + "/" + list)) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
        setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => {
        setOver(false);
        const dragged = e.dataTransfer.getData(ROW_MIME + "/" + list);
        if (!dragged) return;
        e.preventDefault();
        if (dragged !== id) onDropBefore(dragged);
      }}
    >
      <span
        className="cz-grip"
        draggable
        title="Drag to reorder"
        aria-hidden="true"
        onDragStart={(e) => {
          e.dataTransfer.setData(ROW_MIME + "/" + list, id);
          e.dataTransfer.effectAllowed = "move";
        }}
      >
        ⠿
      </span>
      <label className="cz-switch" htmlFor={inputId} title={locked ? lockedWhy : on ? "Shown — click to hide" : "Hidden — click to show"}>
        <input
          id={inputId}
          type="checkbox"
          role="switch"
          checked={on}
          disabled={locked}
          onChange={onToggle}
        />
        <span className="cz-track" aria-hidden="true" />
      </label>
      <span className="cz-text">
        <label className="cz-name" htmlFor={inputId}>
          {label}
        </label>
        <span className="cz-note">{note}</span>
      </span>
      <span className="cz-move">
        <button type="button" aria-label={"Move " + label + " up"} title="Move up" disabled={first} onClick={() => onMove(-1)}>
          ↑
        </button>
        <button type="button" aria-label={"Move " + label + " down"} title="Move down" disabled={last} onClick={() => onMove(1)}>
          ↓
        </button>
      </span>
    </li>
  );
}
