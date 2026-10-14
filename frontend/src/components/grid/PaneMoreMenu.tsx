/** The pane header's "⋯": everything a session's header has that isn't the
 * one thing you came for. A calm header (the first hour, or a pane too narrow
 * for its row of controls) shows name · state · Agent / Diff · Commit… and
 * folds the rest — Terminal, Map, Queue, Fast-track, the history view — in
 * here; every header lists Checklist… here when the session has a branch.
 *
 * Its own small popover rather than the shared OverflowMenu: the pane header
 * scrolls sideways (overflow-x: auto), which clips anything absolutely
 * positioned inside it, so this one is position: fixed against the button. */

import { useEffect, useLayoutEffect, useRef, useState } from "react";

export interface PaneMoreItem {
  key: string;
  label: string;
  title?: string;
  /** A small badge after the label (the Queue's count). */
  badge?: string;
  run(): void;
}

export function PaneMoreMenu({
  items,
  btnRef,
}: {
  items: PaneMoreItem[];
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

  if (!items.length) return null;
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
        aria-label="More for this session"
        title="More — the other tabs and actions for this session"
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
          {items.map((it) => (
            <button
              key={it.key}
              type="button"
              role="menuitem"
              data-more={it.key}
              title={it.title}
              onClick={(e) => {
                e.stopPropagation();
                setOpen(false);
                it.run();
              }}
            >
              {it.label}
              {it.badge && <span className="queue-tab-badge">{it.badge}</span>}
            </button>
          ))}
        </div>
      )}
    </>
  );
}
