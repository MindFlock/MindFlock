/** The sidebar row's session menu as a popover — opened from the row's ⋮
 * (hung under it) or a right-click / Shift+F10 / the Menu key on the row (at
 * the pointer). The items are SessionMenu's, the same model the pane header's
 * ⋯ draws.
 *
 * Portalled to <body> and position: fixed, placed by lib/menuPlace so all of
 * it is on screen: the sidebar list scrolls, and the pane-menu clipping bug
 * (#210) was a popover drawn inside a box that clipped or painted over it. */

import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { placeMenu, refitMenu, type MenuAnchor, type MenuPlacement } from "../../lib/menuPlace";

export interface RowMenuOpen extends MenuAnchor {
  /** Opened from the keyboard: focus goes to its first item. */
  keyboard: boolean;
}

const ITEMS = "button:not(:disabled)";

export function RowMenu({
  id,
  at,
  label,
  opener,
  onClose,
  children,
}: {
  id: string;
  at: RowMenuOpen;
  label: string;
  /** The row's ⋮: a press on it toggles the menu, so it isn't "outside". */
  opener: HTMLElement | null;
  /** `refocus`: hand focus back to the ⋮ (Escape). */
  onClose(refocus: boolean): void;
  children: ReactNode;
}) {
  const box = useRef<HTMLDivElement | null>(null);
  const [place, setPlace] = useState<MenuPlacement | null>(null);
  const placed = useRef<MenuPlacement | null>(null);
  const focused = useRef(false);
  const close = useRef(onClose);
  close.current = onClose;

  // Measure, then place: drawn hidden at 0,0 for one layout pass first.
  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    placed.current = null;
    focused.current = false;
    const fit = () => {
      const w = el.offsetWidth;
      // Its full height, borders included, however much of it scrolls.
      const h = el.offsetHeight - el.clientHeight + el.scrollHeight;
      const next = placed.current
        ? refitMenu(placed.current, w, h, window.innerWidth, window.innerHeight)
        : placeMenu(at, w, h, window.innerWidth, window.innerHeight);
      placed.current = next;
      setPlace(next);
    };
    fit();
    // More ▸ unfolding or Delete arming grows it: keep it on screen.
    const ro = typeof ResizeObserver === "function" ? new ResizeObserver(fit) : null;
    if (el.firstElementChild) ro?.observe(el.firstElementChild);
    return () => ro?.disconnect();
  }, [at]);

  // Opened from the keyboard: its first item takes focus, once per opening.
  useEffect(() => {
    if (!place || !at.keyboard || focused.current) return;
    focused.current = true;
    box.current?.querySelector<HTMLElement>(ITEMS)?.focus();
  }, [place, at]);

  useEffect(() => {
    // Capture phase: row controls stop their own mousedown/scroll bubbling.
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (box.current?.contains(t) || opener?.contains(t)) return;
      close.current(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      e.preventDefault();
      close.current(true);
    };
    // The list scrolling under a fixed menu would leave it pointing at
    // nothing; a scroll inside the menu itself is fine.
    const onScroll = (e: Event) => {
      if (box.current?.contains(e.target as Node)) return;
      close.current(false);
    };
    const onResize = () => close.current(false);
    document.addEventListener("mousedown", onDown, true);
    document.addEventListener("keydown", onKey, true);
    document.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", onResize);
    window.addEventListener("blur", onResize);
    return () => {
      document.removeEventListener("mousedown", onDown, true);
      document.removeEventListener("keydown", onKey, true);
      document.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", onResize);
      window.removeEventListener("blur", onResize);
    };
  }, [opener]);

  // ↑/↓ walk the items, Home/End jump to the ends.
  const onKeyDown = (e: ReactKeyboardEvent) => {
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) return;
    const items = Array.from(box.current?.querySelectorAll<HTMLElement>(ITEMS) ?? []);
    if (!items.length) return;
    e.preventDefault();
    const i = items.indexOf(document.activeElement as HTMLElement);
    const n = items.length;
    const next =
      e.key === "Home" ? 0 : e.key === "End" ? n - 1 : e.key === "ArrowDown" ? (i + 1) % n : (i - 1 + n) % n;
    items[next].focus();
  };

  return createPortal(
    <div
      ref={box}
      id={id}
      className="row-menu"
      role="menu"
      aria-label={label}
      style={
        place
          ? { left: place.left, top: place.top, maxHeight: place.maxHeight }
          : { left: 0, top: 0, visibility: "hidden" }
      }
      onKeyDown={onKeyDown}
      // A press inside never reaches the row (select, rename, drag).
      onMouseDown={(e) => e.stopPropagation()}
      onClick={(e) => e.stopPropagation()}
      onDoubleClick={(e) => e.stopPropagation()}
      onContextMenu={(e) => {
        e.preventDefault();
        e.stopPropagation();
      }}
    >
      {children}
    </div>,
    document.body
  );
}
