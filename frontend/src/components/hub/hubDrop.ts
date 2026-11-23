/** Shared files dropped on a session's rail row (components/hub, lib/hub):
 * their codes go into that session's prompt — typed in, not sent — the same
 * as "Send to session… → Point the agent at it". A dragged selection carries
 * every item in it. Wraps the row's own drag props (rowDnd: reordering
 * rows), which ignore every foreign drag. */

import type { DragEvent } from "react";
import { HUB_MIME, dragItems, putInPrompt } from "../../lib/hub";

interface RowDnd {
  onDragOver(ev: DragEvent): void;
  onDragLeave(ev: DragEvent): void;
  onDrop(ev: DragEvent): void;
}

const isHub = (ev: DragEvent) => Array.from(ev.dataTransfer?.types || []).includes(HUB_MIME);

export function withHubDrop<T extends RowDnd>(title: string, dnd: T, enabled = true): T {
  if (!enabled) return dnd;
  return {
    ...dnd,
    onDragOver: (ev: DragEvent) => {
      if (!isHub(ev)) return dnd.onDragOver(ev);
      ev.preventDefault();
      ev.dataTransfer.dropEffect = "copy";
      (ev.currentTarget as HTMLElement).classList.add("hub-drop-target");
    },
    onDragLeave: (ev: DragEvent) => {
      const el = ev.currentTarget as HTMLElement;
      if (!el.contains(ev.relatedTarget as Node)) el.classList.remove("hub-drop-target");
      dnd.onDragLeave(ev);
    },
    onDrop: (ev: DragEvent) => {
      if (!isHub(ev)) return dnd.onDrop(ev);
      ev.preventDefault();
      ev.stopPropagation();
      (ev.currentTarget as HTMLElement).classList.remove("hub-drop-target");
      const items = dragItems(ev.dataTransfer);
      if (items.length) void putInPrompt(title, items, "", { focus: true });
    },
  };
}
