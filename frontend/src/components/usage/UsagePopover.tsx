/** Ports the shared usage popover from 050-usage-cost.js (_ensureUsagePop /
 * _positionPop / openUsagePop / closeUsagePop / _usageTableEl / _usageNoteEl).
 * A body-mounted, fixed-position dropdown shared by every usage trigger
 * (per-session chips + the overall summary) so it never gets clipped by
 * pane/sidebar overflow. Positions below the anchor when there's room, else
 * above; clamped to the viewport. Closes on outside click, Escape, scroll
 * (capture) and resize — same as the vanilla document/window listeners. */

import { useEffect, useLayoutEffect, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import type { UsageRowData } from "./usageModel";

// The vanilla pop is a shared singleton — opening one usage dropdown closes
// any other. Triggers stopPropagation on their click (as vanilla does), so
// the outside-click listener can't do it; this module-level latch can.
let closeCurrent: (() => void) | null = null;

/** Whether Escape should put focus back on the opener: focus is in the pop,
 * on the opener, or nowhere (the body). Exported for tests. */
export function escapeRefocus(active: Element | null, pop: Element | null, anchor: Element): boolean {
  if (!active || active === anchor) return true;
  if (pop && pop.contains(active)) return true;
  return active.tagName === "BODY";
}

export interface UsagePopoverProps {
  anchor: HTMLElement;
  onClose: () => void;
  children: ReactNode;
  /** An extra class on the pop (one opened over a notice rides above it). */
  className?: string;
}

export function UsagePopover({ anchor, onClose, children, className }: UsagePopoverProps) {
  const popRef = useRef<HTMLDivElement | null>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  // Enforce the one-popover-at-a-time invariant of the vanilla singleton.
  useEffect(() => {
    const close = () => onCloseRef.current();
    if (closeCurrent) closeCurrent();
    closeCurrent = close;
    return () => {
      if (closeCurrent === close) closeCurrent = null;
    };
  }, []);

  // Mirror the vanilla "pop-open" class on the anchor (caret emphasis).
  useEffect(() => {
    anchor.classList.add("pop-open");
    return () => anchor.classList.remove("pop-open");
  }, [anchor]);

  // Position after every render — content changes (period/tab clicks) resize
  // the pop, and vanilla re-ran _positionPop after each re-render too.
  useLayoutEffect(() => {
    const pop = popRef.current;
    if (!pop) return;
    const r = anchor.getBoundingClientRect();
    let top = r.bottom + 4;
    const h = pop.offsetHeight;
    // Below if room, else above (still clamped to the viewport top).
    if (top + h > window.innerHeight - 8 && r.top - 4 - h >= 8) {
      top = r.top - 4 - h;
    }
    pop.style.top = top + "px";
    let left = r.right - pop.offsetWidth;
    const maxLeft = window.innerWidth - pop.offsetWidth - 8;
    if (left > maxLeft) left = maxLeft;
    if (left < 8) left = 8;
    pop.style.left = left + "px";
  });

  useEffect(() => {
    const onDocClick = (e: MouseEvent) => {
      const t = e.target as Node | null;
      if (!t) return;
      if (popRef.current && popRef.current.contains(t)) return;
      if (anchor.contains(t)) return; // the anchor's own handler toggles
      onCloseRef.current();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      // Claimed: the bulk bar's Escape (sidebar/BulkBar) skips a handled
      // key, so closing this doesn't also end select mode.
      e.preventDefault();
      // Focus in the pop (or on the opener, or nowhere) goes back to the
      // opener; focus somewhere else (a terminal) stays where it is.
      if (escapeRefocus(document.activeElement, popRef.current, anchor)) anchor.focus();
      onCloseRef.current();
    };
    const close = () => onCloseRef.current();
    document.addEventListener("click", onDocClick);
    document.addEventListener("keydown", onKey);
    window.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    return () => {
      document.removeEventListener("click", onDocClick);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", close, true);
      window.removeEventListener("resize", close);
    };
  }, [anchor]);

  return createPortal(
    <div ref={popRef} className={"usage-pop" + (className ? " " + className : "")} style={{ top: 0, left: 0 }}>
      {children}
    </div>,
    document.body,
  );
}

/** [label, value] table (port of _usageTableEl). An optional third entry on a
 * row is an extra class on the value cell ("wrap" for prose rows). */
export function UsagePopTable({ rows }: { rows: UsageRowData[] }) {
  return (
    <table className="usage-pop-tbl">
      <tbody>
        {rows.map((r, i) => (
          <tr key={i}>
            <td>{r[0]}</td>
            <td className={"num" + (r[2] ? " " + r[2] : "")}>{r[1]}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** Fine-print footer note (port of _usageNoteEl). */
export function UsagePopNote({ text }: { text: string }) {
  return <div className="usage-pop-note">{text}</div>;
}
