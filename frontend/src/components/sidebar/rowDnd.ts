/** Drag-to-reorder behavior of ONE rail row, shared verbatim by session rows
 * (SidebarRow) and window rows (WindowList) — one object class, so the
 * assistant drags exactly like a session. Returns the <li>'s drag props;
 * `key` is the row's order key: a session title, or a window's NUL-prefixed
 * sentinel (windowKey).
 *
 * The payload rides a custom MIME with a text/plain copy beside it. The
 * custom type is what gates and what onDrop reads: a window's key starts
 * with U+0000, and text/plain is the one type browsers hand to the OS, where
 * a NUL is a string terminator — the copy exists so dragging a session row
 * into a text editor still pastes its title, not so the app can rely on it.
 * Gating on ROW_MIME also keeps every FOREIGN drag out: a section (bar)
 * drag must bubble to the sessions-block drop target, and text dragged in
 * from another app must not be spliced into the persisted order as a
 * phantom row. */

import type { DragEvent } from "react";
import { trackDrag } from "../grid/tearOff";

export const ROW_MIME = "application/x-mf-row";

export interface RowDndCallbacks {
  onDragState(key: string | null): void;
  onDropCue(key: string, cue: "above" | "below" | null): void;
  onDropRow(dragKey: string, targetKey: string, before: boolean): void;
  /** A session row only, where pop-outs are supported: the drag ended
   * OUTSIDE the window (grid/tearOff) — tear the session off into a window
   * of its own, around this screen point (`grabX` px from the row's left). */
  onTearOff?(pt: { x?: number; y?: number }, grabX: number): void;
}

/** The row drag in flight, when it may be torn off. Module-level: the props
 * are rebuilt on every render, and the drag's start re-renders the row. One
 * drag at a time. */
let tear: { key: string; track: ReturnType<typeof trackDrag>; grabX: number } | null = null;

export function rowDndProps(key: string, cbs: RowDndCallbacks, draggable = true) {
  return {
    draggable,
    onDragStart: (ev: DragEvent) => {
      ev.dataTransfer.setData(ROW_MIME, key);
      ev.dataTransfer.setData("text/plain", key);
      ev.dataTransfer.effectAllowed = "move" as const;
      cbs.onDragState(key);
      tear = null;
      if (cbs.onTearOff) {
        const rect = (ev.currentTarget as HTMLElement).getBoundingClientRect();
        tear = { key, track: trackDrag(), grabX: Math.min(ev.clientX - rect.left, 80) };
      }
    },
    onDragEnd: (ev: DragEvent) => {
      cbs.onDragState(null);
      cbs.onDropCue(key, null);
      const t = tear;
      tear = null;
      if (!t || t.key !== key) return;
      const out = t.track.end({ dropEffect: ev.dataTransfer.dropEffect, screenX: ev.screenX, screenY: ev.screenY });
      if (out) cbs.onTearOff?.(out, t.grabX);
    },
    onDragOver: (ev: DragEvent) => {
      if (!ev.dataTransfer.types.includes(ROW_MIME)) return;
      ev.preventDefault();
      const rect = (ev.currentTarget as HTMLElement).getBoundingClientRect();
      cbs.onDropCue(key, ev.clientY - rect.top < rect.height / 2 ? "above" : "below");
    },
    onDragLeave: (ev: DragEvent) => {
      if (!(ev.currentTarget as HTMLElement).contains(ev.relatedTarget as Node))
        cbs.onDropCue(key, null);
    },
    onDrop: (ev: DragEvent) => {
      if (!ev.dataTransfer.types.includes(ROW_MIME)) return;
      ev.preventDefault();
      const rect = (ev.currentTarget as HTMLElement).getBoundingClientRect();
      cbs.onDropRow(
        ev.dataTransfer.getData(ROW_MIME),
        key,
        ev.clientY - rect.top < rect.height / 2
      );
      cbs.onDropCue(key, null);
    },
  };
}
