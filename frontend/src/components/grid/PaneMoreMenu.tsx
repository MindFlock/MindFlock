/** The pane header's "⋯": the session's ONE menu (lib/sessionMenu — the
 * sidebar row's ⋯ draws the same model), grouped Work · Ship · Session ·
 * More · Close/Delete. What a calm header (the first hour, or a pane too
 * narrow for its row of controls) leaves out of the row — Terminal, Map, the
 * history view — is in its More fold; Queue prompt… and Fast-track… are in
 * it always.
 *
 * Its own small popover rather than the shared OverflowMenu: the pane header
 * scrolls sideways (overflow-x: auto), which clips anything absolutely
 * positioned inside it, so this one is position: fixed against the button. */

import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";

export function PaneMoreMenu({
  children,
  btnRef,
}: {
  /** The menu's body, given a close() for after an item ran. */
  children: (close: () => void) => ReactNode;
  /** Also handed to the fast-track picker as its anchor while ⏩ is folded. */
  btnRef?: (el: HTMLButtonElement | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ top: number; right: number } | null>(null);
  const btn = useRef<HTMLButtonElement | null>(null);
  const menu = useRef<HTMLDivElement | null>(null);

  useLayoutEffect(() => {
    if (!open || !btn.current) return;
    const r = btn.current.getBoundingClientRect();
    setPos({ top: r.bottom + 4, right: Math.max(4, window.innerWidth - r.right) });
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (menu.current?.contains(t) || btn.current?.contains(t)) return;
      setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      setOpen(false);
    };
    const onScroll = () => setOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey, true);
    window.addEventListener("resize", onScroll);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey, true);
      window.removeEventListener("resize", onScroll);
    };
  }, [open]);

  return (
    <>
      <button
        ref={(el) => {
          btn.current = el;
          btnRef?.(el);
        }}
        type="button"
        className={"act pane-more" + (open ? " open" : "")}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="Actions for this session"
        title="Actions for this session — the same menu as its sidebar row's ⋯"
        onMouseDown={(e) => e.stopPropagation()}
        onClick={(e) => {
          e.stopPropagation();
          setOpen((v) => !v);
        }}
      >
        ⋯
      </button>
      {open && pos && (
        <div
          ref={menu}
          className="pane-more-menu"
          role="menu"
          style={{ top: pos.top, right: pos.right }}
          onMouseDown={(e) => e.stopPropagation()}
        >
          {children(() => setOpen(false))}
        </div>
      )}
    </>
  );
}
