/** Where a floating menu goes so ALL of it is on screen (the sidebar row's
 * session menu — components/sidebar/RowMenu.tsx). Pure, so the clamping is
 * testable without a layout engine.
 *
 * Two anchors: a button (the row's ⋮ — the menu hangs under it, right edges
 * aligned, the way the pane header's ⋯ popover does) and a pointer (a
 * right-click — the menu's top-left corner at the pointer). Either flips above
 * its anchor when there is no room below, a pointer menu flips to the left of
 * the pointer when there is no room to its right, and as a last resort the
 * menu is clamped into the viewport and scrolls (maxHeight). */

export interface MenuAnchor {
  /** The menu's LEFT edge for a pointer ("start"); its RIGHT edge for a button ("end"). */
  x: number;
  /** Where the menu's top goes when it opens downwards. */
  y: number;
  /** Where its bottom goes when it flips up (the button's top, or the pointer). */
  flipY: number;
  align: "start" | "end";
}

export interface MenuPlacement {
  left: number;
  top: number;
  maxHeight: number;
}

/** Kept clear between the menu and the window's edge. */
export const MENU_MARGIN = 6;

export function placeMenu(
  a: MenuAnchor,
  w: number,
  h: number,
  vw: number,
  vh: number,
  m: number = MENU_MARGIN
): MenuPlacement {
  const maxHeight = Math.max(0, vh - 2 * m);
  const hh = Math.min(h, maxHeight);
  let left = a.align === "end" ? a.x - w : a.x;
  // A pointer menu with no room on the right opens to the pointer's left.
  if (a.align === "start" && left + w > vw - m && a.x - w >= m) left = a.x - w;
  left = Math.max(m, Math.min(left, vw - m - w));
  let top: number;
  if (a.y + hh <= vh - m) top = a.y;
  else if (a.flipY - hh >= m) top = a.flipY - hh;
  else top = Math.max(m, vh - m - hh);
  return { left: Math.round(left), top: Math.round(top), maxHeight };
}

/** Re-fit after the menu's own size changed (More ▸ unfolded, Delete armed):
 * keep it where it is, nudged only as far as it must go to stay on screen —
 * never flipped across the anchor under the pointer. */
export function refitMenu(
  prev: MenuPlacement,
  w: number,
  h: number,
  vw: number,
  vh: number,
  m: number = MENU_MARGIN
): MenuPlacement {
  const maxHeight = Math.max(0, vh - 2 * m);
  const hh = Math.min(h, maxHeight);
  return {
    left: Math.round(Math.max(m, Math.min(prev.left, vw - m - w))),
    top: Math.round(Math.max(m, Math.min(prev.top, vh - m - hh))),
    maxHeight,
  };
}

/** A popover hung under a top-bar button (the mic's options): its left edge
 * under the button's, flipped to end at the button's right edge when it
 * would cross the window's right side, then kept `m` from both sides; its
 * top just under the button and its height capped to what is left of the
 * window (it scrolls inside). `w`: the popover's width. */
export function placeUnder(
  btn: { left: number; right: number; bottom: number },
  w: number,
  vw: number,
  vh: number,
  m = 8
): MenuPlacement {
  let left = btn.left;
  if (left + w > vw - m) left = btn.right - w;
  left = Math.max(m, Math.min(left, vw - m - w));
  const top = btn.bottom + 6;
  return { left: Math.round(left), top: Math.round(top), maxHeight: Math.max(0, Math.round(vh - top - 2 * m)) };
}
