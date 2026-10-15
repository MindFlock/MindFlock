/** The pane header's "⋯": the session's ONE menu (lib/sessionMenu — the
 * sidebar row's ⋯ draws the same model), grouped Work · Ship · Session ·
 * More · Close/Delete. Every header shows Terminal and Map, so those are the
 * row menu's only; what a calm header (the first hour, or a pane too narrow
 * for its row of controls) leaves out of the row — the history view — is in
 * its More fold, beside Copy entire history; Queue prompt… and Fast-track…
 * are in it always.
 *
 * Its own small popover rather than the shared OverflowMenu: the pane header
 * scrolls sideways (overflow-x: auto), which clips anything absolutely
 * positioned inside it, so this one is position: fixed against the button —
 * and portalled to <body>. Inside the pane, `container-type` (Pane.css) makes
 * the pane the containing block and stacking context of a fixed child, so in
 * a 2×2 the pane below painted over the menu's lower half and the pinned
 * Task bar over its first item.
 *
 * Placed by lib/menuPlace, like the row's menu: measured hidden for one
 * layout pass, then hung under the ⋯ (above it with no room below), and
 * nudged up as More ▸ unfolds or Delete arms, so all of it stays on screen;
 * on a window too short for it, its items scroll and Close / Delete stay
 * pinned at its foot (SessionMenu.css). */

import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { placeMenu, refitMenu, type MenuPlacement } from "../../lib/menuPlace";

/** The menu's height with nothing scrolled away: a clamped child (the
 * session items scroll inside it) counts at its full height. */
function naturalHeight(el: HTMLElement): number {
  let h = el.offsetHeight;
  for (const c of Array.from(el.children)) h += c.scrollHeight - c.clientHeight;
  return h;
}

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
  const [place, setPlace] = useState<MenuPlacement | null>(null);
  const btn = useRef<HTMLButtonElement | null>(null);
  const menu = useRef<HTMLDivElement | null>(null);

  // Measure, then place: drawn hidden at 0,0 for one layout pass first.
  useLayoutEffect(() => {
    setPlace(null);
    const el = menu.current;
    if (!open || !el || !btn.current) return;
    const r = btn.current.getBoundingClientRect();
    let placed: MenuPlacement | null = null;
    const fit = () => {
      const w = el.offsetWidth;
      const h = naturalHeight(el);
      placed = placed
        ? refitMenu(placed, w, h, window.innerWidth, window.innerHeight)
        : placeMenu({ x: r.right, y: r.bottom + 4, flipY: r.top - 4, align: "end" }, w, h, window.innerWidth, window.innerHeight);
      setPlace(placed);
    };
    fit();
    // More ▸ unfolding or Delete arming grows it: keep it on screen.
    const ro = typeof ResizeObserver === "function" ? new ResizeObserver(fit) : null;
    for (const c of Array.from(el.querySelectorAll(":scope > *, .session-menu > *"))) ro?.observe(c);
    return () => ro?.disconnect();
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
      {open &&
        createPortal(
          <div
            ref={menu}
            className="pane-more-menu"
            role="menu"
            style={
              place
                ? { left: place.left, top: place.top, maxHeight: place.maxHeight }
                : { left: 0, top: 0, visibility: "hidden" }
            }
            onMouseDown={(e) => e.stopPropagation()}
          >
            {children(() => setOpen(false))}
          </div>,
          document.body
        )}
    </>
  );
}
