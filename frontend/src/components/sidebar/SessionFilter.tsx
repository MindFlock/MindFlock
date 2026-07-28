/** Sidebar session filter (port of initSidebarFilter, app.js section 9). */

import { useUi } from "../../state/store";

export function SessionFilter() {
  const filter = useUi((s) => s.filter);
  const setFilter = useUi((s) => s.setFilter);
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
        onChange={(e) => setFilter(e.target.value.trim())}
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            setFilter("");
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
