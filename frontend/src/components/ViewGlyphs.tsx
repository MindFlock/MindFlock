/** The glyphs of the two views (lib/viewMode): the grid's four panes here,
 * the sky's bird is `flights/Bird`. Worn by the Grid · Sky toggle wherever it
 * is drawn — the sidebar's View row, and the top bar when the sidebar is
 * hidden or the window is phone-narrow. */

/** The grid view's glyph: four panes. */
export function GridGlyph({ className = "tb-view-icon" }: { className?: string }) {
  return (
    <svg className={className} width="12" height="12" viewBox="0 0 12 12" aria-hidden="true" focusable="false">
      <rect x="0.75" y="0.75" width="4.5" height="4.5" rx="1" fill="none" stroke="currentColor" strokeWidth="1.3" />
      <rect x="6.75" y="0.75" width="4.5" height="4.5" rx="1" fill="none" stroke="currentColor" strokeWidth="1.3" />
      <rect x="0.75" y="6.75" width="4.5" height="4.5" rx="1" fill="none" stroke="currentColor" strokeWidth="1.3" />
      <rect x="6.75" y="6.75" width="4.5" height="4.5" rx="1" fill="none" stroke="currentColor" strokeWidth="1.3" />
    </svg>
  );
}
