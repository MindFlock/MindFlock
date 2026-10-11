/** Sidebar session filter (port of initSidebarFilter, app.js section 9).
 *
 * Shown when Customize → "Filter box" is on (the default). Off, "/" summons it
 * for one search: it goes away again when it loses focus empty. */

import { useUi } from "../../state/store";

export function SessionFilter() {
  const filter = useUi((s) => s.filter);
  const setFilter = useUi((s) => s.setFilter);
  const peek = useUi((s) => s.filterPeek && !s.showFilter);
  const setFilterPeek = useUi((s) => s.setFilterPeek);
  return (
    <div id="sidebar-search">
      <input
        id="session-filter"
        type="text"
        placeholder="Filter by name or branch…  ( / )"
        autoComplete="off"
        spellCheck={false}
        aria-label="Filter sessions"
        value={filter}
        autoFocus={peek}
        onChange={(e) => setFilter(e.target.value.trim())}
        onBlur={(e) => {
          if (peek && !e.target.value.trim()) setFilterPeek(false);
        }}
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            setFilter("");
            setFilterPeek(false);
            (e.target as HTMLInputElement).blur();
          }
        }}
      />
      <button
        id="session-filter-clear"
        type="button"
        title="Clear filter"
        aria-label="Clear filter"
        onClick={(e) => {
          setFilter("");
          (
            (e.currentTarget.previousElementSibling as HTMLInputElement) || null
          )?.focus();
        }}
      >
        ✕
      </button>
    </div>
  );
}
