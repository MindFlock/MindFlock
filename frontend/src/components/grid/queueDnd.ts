/** Pure drag-and-drop arithmetic for the Queue tab's item list — kept out of
 * the component so the slot math (the part that silently corrupts an order
 * when wrong) is unit-testable without a DOM. */

/** The insertion slot a hover over row `rowIndex` means: top half of the row
 * is "before it" (slot = rowIndex), bottom half is "after it" (rowIndex + 1).
 * Slots run 0..items.length. */
export function hoverSlot(rowIndex: number, offsetY: number, rowHeight: number): number {
  return offsetY < rowHeight / 2 ? rowIndex : rowIndex + 1;
}

/** Final index the dragged item (at `from`) lands at when dropped into
 * insertion slot `slot`, or null when the drop is a no-op (dropping an item
 * onto its own edges). Removing the item first shifts every later slot down
 * by one — the classic off-by-one this function exists to own. */
export function dropIndex(from: number, slot: number): number | null {
  const to = from < slot ? slot - 1 : slot;
  return to === from ? null : to;
}

/** The list with the item moved from `from` to `to` (a new array). */
export function reorderItems<T>(items: T[], from: number, to: number): T[] {
  const next = items.slice();
  const [moved] = next.splice(from, 1);
  next.splice(to, 0, moved);
  return next;
}
