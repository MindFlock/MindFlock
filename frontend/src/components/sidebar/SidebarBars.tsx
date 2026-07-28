/** The movable sidebar sections. Each customizable bar (Usage, Ticket
 * Ingestion, PR Review, Assistant) renders inside a <BarSlot> that carries the
 * drag affordance (a grip on the left, since the bars themselves are wall-to-
 * wall buttons/toggles that would otherwise swallow the drag gesture) and the
 * accent insertion cue. Reordering happens in Sidebar, which owns the shared
 * section-drag state so a bar can be dropped above OR below the session list.
 *
 * Section drags are tagged with SECTION_MIME so the session ROW drag (plain
 * text titles) and the section drag never interfere. */

import { type DragEvent, type ReactNode } from "react";
import type { DialogName } from "../../state/store";
import { OverallUsage } from "../usage/OverallUsage";
import { AutomationBar } from "./AutomationBar";
import { GitIssueBar } from "./GitIssueBar";
import { PrReviewBar } from "./PrReviewBar";

/** DataTransfer type that marks a drag as a section (bar) move. */
export const SECTION_MIME = "application/x-mf-section";

interface ContentCbs {
  onOpenChat(): void;
  onOpenTodo(): void;
  openDialogFor(name: DialogName, target?: string | null): void;
}

/** The inner content for a given bar key (visibility is each bar's own call —
 * AutomationBar/PrReviewBar return null when unavailable, collapsing the slot
 * via `.bar-slot:empty`). */
export function barContent(key: string, cbs: ContentCbs): ReactNode {
  switch (key) {
    case "usage":
      return <OverallUsage />;
    case "ingestion":
      return <AutomationBar />;
    case "pr-review":
      return <PrReviewBar />;
    case "issue-handling":
      return <GitIssueBar />;
    case "assistant":
      return (
        <div
          id="assistant-bar"
          title="A personal AI assistant (powered by your chosen provider) — answers questions and manages your todo list"
        >
          <span className="as-label">Assistant</span>
          <span className="as-actions">
            <button
              id="assistant-chat-btn"
              className="as-toggle"
              title="Open a chat window with your personal assistant"
              onClick={cbs.onOpenChat}
            >
              Chat
            </button>
            <button
              id="assistant-todo-btn"
              className="as-toggle"
              title="Show the todo list (drag to reorder)"
              onClick={cbs.onOpenTodo}
            >
              Todo
            </button>
            <button
              id="assistant-agent-btn"
              className="as-toggle"
              title="Edit the assistant's standing instructions (its agent file) — shape how it behaves"
              onClick={() => cbs.openDialogFor("assistant-agent")}
            >
              Agent
            </button>
          </span>
        </div>
      );
    default:
      return null;
  }
}

interface SlotProps {
  barKey: string;
  /** This slot is the one currently being dragged. */
  dragging: boolean;
  /** Insertion cue to draw on this slot, if any. */
  cue: "above" | "below" | null;
  onStart(key: string): void;
  onEnd(): void;
  onOver(key: string, cue: "above" | "below"): void;
  onLeave(key: string): void;
  onDropSection(dragKey: string, targetKey: string, before: boolean): void;
  children: ReactNode;
}

export function BarSlot({
  barKey,
  dragging,
  cue,
  onStart,
  onEnd,
  onOver,
  onLeave,
  onDropSection,
  children,
}: SlotProps) {
  return (
    <div
      className={
        "bar-slot" + (dragging ? " dragging" : "") + (cue ? ` drop-${cue}` : "")
      }
      data-bar={barKey}
      draggable
      onDragStart={(ev: DragEvent) => {
        // Grab the grip (or any non-control area) — never hijack a toggle.
        if ((ev.target as HTMLElement).closest("input, select, textarea")) {
          ev.preventDefault();
          return;
        }
        ev.dataTransfer.setData(SECTION_MIME, barKey);
        ev.dataTransfer.effectAllowed = "move";
        onStart(barKey);
      }}
      onDragEnd={onEnd}
      onDragOver={(ev: DragEvent) => {
        if (!ev.dataTransfer.types.includes(SECTION_MIME)) return;
        ev.preventDefault();
        ev.dataTransfer.dropEffect = "move";
        if (dragging) return;
        const rect = (ev.currentTarget as HTMLElement).getBoundingClientRect();
        onOver(barKey, ev.clientY - rect.top < rect.height / 2 ? "above" : "below");
      }}
      onDragLeave={(ev: DragEvent) => {
        if (!(ev.currentTarget as HTMLElement).contains(ev.relatedTarget as Node))
          onLeave(barKey);
      }}
      onDrop={(ev: DragEvent) => {
        if (!ev.dataTransfer.types.includes(SECTION_MIME)) return;
        ev.preventDefault();
        const rect = (ev.currentTarget as HTMLElement).getBoundingClientRect();
        onDropSection(
          ev.dataTransfer.getData(SECTION_MIME),
          barKey,
          ev.clientY - rect.top < rect.height / 2
        );
        onLeave(barKey);
      }}
    >
      <span className="bar-grip" title="Drag to move this section">⠿</span>
      {children}
    </div>
  );
}
