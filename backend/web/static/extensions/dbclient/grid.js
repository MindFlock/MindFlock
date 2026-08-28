/** The shared data grid: a dense table with a sticky header, an optional
 * checkbox column, header-click sorting, a per-column filter row, double-click
 * cell editing with a Set-NULL control, and the value-codec renderings
 * (NULL vs "", bytes chips, truncated chips with a magnifier).
 *
 * The grid keeps the page's rows and their pending edits in memory and exposes
 * them as change sets; it never talks to the server. tableview.js turns
 * changes() into the /rows operations and querypad.js uses a read-only
 * instance for results. State lives in closures + the DOM, which is what the
 * host's keep-alive contract preserves across grid drags.
 *
 * Rendering is a plain full re-render on setData — a page is at most a few
 * hundred rows × the render cap of columns, and the cap exists precisely so a
 * 900-column table does not turn into 450k table cells. */

import { el, svgIcon, fmtBytes, option } from "./ui.js";

/** The explicit-null marker the backend's inbound codec understands. Frozen
 * so identity checks (v === SQL_NULL) work and JSON.stringify yields the wire
 * shape verbatim. */
export const SQL_NULL = Object.freeze({ $null: true });

/** Columns beyond this many are not rendered (a notice says so). */
export const RENDER_CAP = 200;

/** Filter operators the /table-data endpoint accepts, in menu order. */
export const FILTER_OPS = [
  ["contains", "contains"],
  ["eq", "="],
  ["ne", "≠"],
  ["gt", ">"],
  ["lt", "<"],
  ["null", "is NULL"],
  ["notnull", "not NULL"],
];

export function isBytes(v) {
  return !!v && typeof v === "object" && v.$type === "bytes";
}

export function isTruncated(v) {
  return !!v && typeof v === "object" && v.$type === "truncated";
}

/** The text a plain (non-marker) value shows as. */
export function displayText(v) {
  if (v === null || v === undefined) return "";
  if (typeof v === "string") return v;
  if (typeof v === "object") {
    if (isTruncated(v)) return String(v.text || "");
    if (isBytes(v)) return "<bytes " + fmtBytes(v.len) + ">";
    try {
      return JSON.stringify(v);
    } catch (e) {
      return String(v);
    }
  }
  return String(v);
}

/** Normalize a columns array ({name,type} objects or bare names). */
export function normalizeColumns(columns) {
  return (columns || []).map((c) =>
    typeof c === "string" ? { name: c, type: "" } : { name: String(c.name), type: c.type || "", ...c }
  );
}

/** Rows may arrive as arrays (positional) or objects keyed by column name. */
export function rowValues(row, columns) {
  if (Array.isArray(row)) return row;
  if (row && typeof row === "object") return columns.map((c) => (c.name in row ? row[c.name] : null));
  return [];
}

/** opts: {editable, selectable, filterable, sortable, renderCap, onSort(colName),
 * onFilter(filters), onInspect(value), onChange()}. */
export function createGrid(opts = {}) {
  const o = {
    editable: false,
    selectable: false,
    filterable: false,
    sortable: false,
    renderCap: RENDER_CAP,
    onSort: null,
    onFilter: null,
    onInspect: null,
    onChange: null,
    ...opts,
  };

  const notice = el("div", { class: "dbc-grid-notice", hidden: true });
  const thead = el("thead");
  const tbody = el("tbody");
  const table = el("table", { class: "dbc-table" }, thead, tbody);
  const scroller = el("div", { class: "dbc-grid-scroll" }, table);
  const empty = el("div", { class: "dbc-grid-empty", hidden: true, text: "No rows" });
  const root = el("div", { class: "dbc-grid" }, notice, scroller, empty);

  let columns = []; // normalized, full list
  let shown = []; // columns actually rendered (render cap)
  let rows = []; // [{values, edits: Map<colIdx, v>, deleted, inserted, selected, tr}]
  let pk = [];
  let sort = null; // {column, dir}
  let filters = []; // [{column, op, value}]
  let filtersShown = false;
  let startIndex = 1;
  let editor = null; // {td, row, colIdx, input, done}
  let selectAllBox = null;

  const emitChange = () => {
    if (o.onChange) o.onChange();
  };

  // --- header ---------------------------------------------------------------

  function renderHead() {
    thead.replaceChildren();
    const tr = el("tr");
    if (o.selectable) {
      selectAllBox = el("input", {
        type: "checkbox",
        title: "Select all rows on this page",
        onChange: () => {
          for (const r of rows) setSelected(r, selectAllBox.checked);
          emitChange();
        },
      });
      tr.appendChild(el("th", { class: "dbc-sel" }, selectAllBox));
    }
    tr.appendChild(el("th", { class: "dbc-rownum", text: "#" }));
    for (const c of shown) {
      const th = el(
        "th",
        {
          class: "dbc-th" + (o.sortable ? " sortable" : "") + (pk.includes(c.name) ? " pk" : ""),
          title: c.type ? c.name + " · " + c.type : c.name,
          dataset: { col: c.name },
        },
        pk.includes(c.name) ? svgIcon("key", "dbc-pk-icon") : null,
        el("span", { class: "dbc-col-name", text: c.name }),
        c.type ? el("span", { class: "dbc-col-type", text: c.type }) : null
      );
      if (sort && sort.column === c.name) {
        th.dataset.sort = sort.dir;
        th.appendChild(svgIcon(sort.dir === "desc" ? "sort-desc" : "sort-asc", "dbc-sort-icon"));
      }
      if (o.sortable && o.onSort) th.addEventListener("click", () => o.onSort(c.name));
      tr.appendChild(th);
    }
    thead.appendChild(tr);
    if (o.filterable && filtersShown) thead.appendChild(renderFilterRow());
  }

  function renderFilterRow() {
    const tr = el("tr", { class: "dbc-filter-row" });
    if (o.selectable) tr.appendChild(el("th", { class: "dbc-sel" }));
    tr.appendChild(el("th", { class: "dbc-rownum" }, svgIcon("filter")));
    for (const c of shown) {
      const cur = filters.find((f) => f.column === c.name) || { op: "contains", value: "" };
      const sel = el("select", { class: "dbc-filter-op", title: "Operator" });
      for (const [v, label] of FILTER_OPS) sel.appendChild(option(v, label, v === cur.op));
      const input = el("input", {
        class: "dbc-filter-val",
        type: "text",
        placeholder: "filter",
        value: cur.value || "",
        disabled: cur.op === "null" || cur.op === "notnull",
      });
      const fire = () => {
        input.disabled = sel.value === "null" || sel.value === "notnull";
        collectFilters();
        if (o.onFilter) o.onFilter(filters.slice());
      };
      sel.addEventListener("change", fire);
      input.addEventListener("input", fire);
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") fire();
      });
      tr.appendChild(el("th", { class: "dbc-filter-cell" }, sel, input));
    }
    return tr;
  }

  function collectFilters() {
    const out = [];
    const cells = thead.querySelectorAll(".dbc-filter-cell");
    cells.forEach((cell, i) => {
      const c = shown[i];
      if (!c) return;
      const op = cell.querySelector("select").value;
      const value = cell.querySelector("input").value;
      if (op === "null" || op === "notnull") out.push({ column: c.name, op, value: null });
      else if (value !== "") out.push({ column: c.name, op, value });
    });
    filters = out;
  }

  // --- rows -----------------------------------------------------------------

  function renderBody() {
    tbody.replaceChildren();
    const frag = document.createDocumentFragment();
    rows.forEach((r, i) => frag.appendChild(renderRow(r, i)));
    tbody.appendChild(frag);
    empty.hidden = rows.length > 0;
  }

  function renderRow(r, i) {
    const tr = el("tr");
    r.tr = tr;
    if (o.selectable) {
      const box = el("input", {
        type: "checkbox",
        checked: !!r.selected,
        onChange: () => {
          r.selected = box.checked;
          tr.classList.toggle("selected", r.selected);
          syncSelectAll();
          emitChange();
        },
      });
      r.box = box;
      tr.appendChild(el("td", { class: "dbc-sel" }, box));
    }
    tr.appendChild(el("td", { class: "dbc-rownum", text: r.inserted ? "+" : String(startIndex + i) }));
    shown.forEach((c, colIdx) => {
      const td = el("td", { class: "dbc-cell" });
      renderCell(td, r, colIdx);
      if (o.editable) td.addEventListener("dblclick", () => openEditor(td, r, colIdx));
      tr.appendChild(td);
    });
    updateRowClass(r);
    return tr;
  }

  function updateRowClass(r) {
    if (!r.tr) return;
    r.tr.classList.toggle("deleted", !!r.deleted);
    r.tr.classList.toggle("inserted", !!r.inserted);
    r.tr.classList.toggle("selected", !!r.selected);
    r.tr.classList.toggle("edited", r.edits.size > 0);
  }

  function renderCell(td, r, colIdx) {
    td.replaceChildren();
    td.className = "dbc-cell";
    td.removeAttribute("title");
    const edited = r.edits.has(colIdx);
    const orig = r.values[colIdx];
    const v = edited ? r.edits.get(colIdx) : orig;
    if (edited) {
      td.classList.add("dirty");
      td.title = r.inserted ? "new value" : "was: " + (orig === null ? "NULL" : displayText(orig));
    }
    if (r.inserted && !edited) {
      td.classList.add("unset");
      td.appendChild(el("span", { class: "dbc-unset", text: "default" }));
      return;
    }
    if (v === null || v === undefined || v === SQL_NULL) {
      td.appendChild(el("span", { class: "dbc-null", text: "NULL" }));
      return;
    }
    if (isBytes(v)) {
      td.classList.add("bytes");
      td.appendChild(el("span", { class: "dbc-chip", title: "Binary value — read only" }, "bytes · " + fmtBytes(v.len)));
      return;
    }
    if (isTruncated(v)) {
      td.classList.add("truncated");
      td.appendChild(el("span", { class: "dbc-cell-text", text: String(v.text || "") }));
      td.appendChild(
        el(
          "button",
          {
            type: "button",
            class: "dbc-magnify",
            title: "Show the first 8 KB of this " + fmtBytes(v.len) + " value (read only)",
            onClick: (e) => {
              e.stopPropagation();
              if (o.onInspect) o.onInspect(v);
            },
          },
          svgIcon("search")
        )
      );
      return;
    }
    if (typeof v === "number") td.classList.add("num");
    if (typeof v === "boolean") td.classList.add("bool");
    if (v === "") {
      // An empty string is not NULL — say so instead of showing a blank cell.
      td.classList.add("empty-str");
      td.appendChild(el("span", { class: "dbc-empty-str", text: '""' }));
      return;
    }
    td.textContent = displayText(v);
  }

  function setSelected(r, on) {
    r.selected = on;
    if (r.box) r.box.checked = on;
    if (r.tr) r.tr.classList.toggle("selected", on);
  }

  function syncSelectAll() {
    if (!selectAllBox) return;
    const n = rows.filter((r) => r.selected).length;
    selectAllBox.checked = n > 0 && n === rows.length;
    selectAllBox.indeterminate = n > 0 && n < rows.length;
  }

  // --- cell editor ----------------------------------------------------------

  function cellEditable(r, colIdx) {
    if (!o.editable || r.deleted) return false;
    const v = r.edits.has(colIdx) ? r.edits.get(colIdx) : r.values[colIdx];
    // Bytes are not editable in v1; editing a truncated cell would write the
    // head back over the full value, so it is read-only too.
    return !isBytes(v) && !isTruncated(v);
  }

  function openEditor(td, r, colIdx) {
    if (!cellEditable(r, colIdx)) return;
    closeEditor(true);
    const cur = r.edits.has(colIdx) ? r.edits.get(colIdx) : r.values[colIdx];
    const isNull = cur === null || cur === undefined || cur === SQL_NULL;
    const input = el("input", {
      class: "dbc-edit-input",
      type: "text",
      value: isNull ? "" : displayText(cur),
      placeholder: isNull ? "NULL" : "",
      "aria-label": "Edit " + shown[colIdx].name,
    });
    const nullBtn = el(
      "button",
      { type: "button", class: "dbc-null-btn", title: "Set NULL" },
      svgIcon("null"),
      el("span", { text: "NULL" })
    );
    // mousedown would blur the input (committing the typed text) before the
    // click lands; keep focus so the click means NULL and nothing else.
    nullBtn.addEventListener("mousedown", (e) => e.preventDefault());
    nullBtn.addEventListener("click", () => finish(SQL_NULL));
    const wrap = el("div", { class: "dbc-editor" }, input, nullBtn);
    td.replaceChildren(wrap);
    td.classList.add("editing");
    editor = { td, row: r, colIdx, input, done: false };

    const finish = (value) => {
      if (!editor || editor.input !== input || editor.done) return;
      editor.done = true;
      setEdit(r, colIdx, value, td);
      editor = null;
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        finish(input.value);
      } else if (e.key === "Escape") {
        e.preventDefault();
        cancelEditor();
      } else if (e.key === "Tab") {
        e.preventDefault();
        finish(input.value);
        const next = shown[colIdx + (e.shiftKey ? -1 : 1)];
        if (next) {
          const nextTd = r.tr && r.tr.querySelectorAll("td.dbc-cell")[colIdx + (e.shiftKey ? -1 : 1)];
          if (nextTd) openEditor(nextTd, r, colIdx + (e.shiftKey ? -1 : 1));
        }
      }
    });
    input.addEventListener("blur", () => {
      // Deferred: a click on the NULL button keeps focus (see above), so a
      // real blur means the user left the cell — commit what is typed.
      setTimeout(() => finish(input.value), 0);
    });
    input.focus();
    input.select();
  }

  function cancelEditor() {
    if (!editor) return;
    const { td, row, colIdx } = editor;
    editor.done = true;
    editor = null;
    renderCell(td, row, colIdx);
  }

  function closeEditor(commit) {
    if (!editor) return;
    if (commit) {
      const { td, row, colIdx, input } = editor;
      editor.done = true;
      editor = null;
      setEdit(row, colIdx, input.value, td);
    } else cancelEditor();
  }

  function setEdit(r, colIdx, value, td) {
    const orig = r.values[colIdx];
    let same = false;
    if (!r.inserted) {
      if (value === SQL_NULL) same = orig === null || orig === undefined;
      else same = orig !== null && orig !== undefined && !isBytes(orig) && !isTruncated(orig) && displayText(orig) === value;
    }
    if (same) r.edits.delete(colIdx);
    else r.edits.set(colIdx, value);
    if (td) renderCell(td, r, colIdx);
    updateRowClass(r);
    emitChange();
  }

  // --- public API -----------------------------------------------------------

  function setData(data) {
    closeEditor(false);
    columns = normalizeColumns(data.columns);
    pk = Array.isArray(data.pk) ? data.pk.map(String) : [];
    startIndex = typeof data.startIndex === "number" ? data.startIndex : 1;
    if (columns.length > o.renderCap) {
      shown = columns.slice(0, o.renderCap);
      notice.textContent =
        "Showing the first " + o.renderCap + " of " + columns.length + " columns — narrow the table with a query to see the rest.";
      notice.hidden = false;
    } else {
      shown = columns;
      notice.hidden = true;
      notice.textContent = "";
    }
    rows = (data.rows || []).map((raw) => ({
      values: rowValues(raw, columns),
      edits: new Map(),
      deleted: false,
      inserted: false,
      selected: false,
      tr: null,
    }));
    renderHead();
    renderBody();
    syncSelectAll();
    emitChange();
  }

  function insertRow() {
    if (!o.editable) return null;
    closeEditor(true);
    const r = {
      values: new Array(columns.length).fill(undefined),
      edits: new Map(),
      deleted: false,
      inserted: true,
      selected: false,
      tr: null,
    };
    rows.unshift(r);
    const tr = renderRow(r, 0);
    tbody.insertBefore(tr, tbody.firstChild);
    // Row numbers shift down by one for the rest of the page.
    rows.forEach((row, i) => {
      if (!row.inserted && row.tr) {
        const cell = row.tr.querySelector(".dbc-rownum");
        if (cell) cell.textContent = String(startIndex + i);
      }
    });
    empty.hidden = true;
    emitChange();
    scroller.scrollTop = 0;
    const firstTd = tr.querySelector("td.dbc-cell");
    if (firstTd) openEditor(firstTd, r, 0);
    return r;
  }

  /** Mark the selected rows deleted (inserted ones just vanish). If every
   * selected row is already marked, un-mark them instead. */
  function deleteSelected() {
    if (!o.editable) return;
    closeEditor(true);
    const sel = rows.filter((r) => r.selected);
    if (!sel.length) return;
    const allDeleted = sel.every((r) => r.deleted || r.inserted);
    for (const r of sel) {
      if (r.inserted) {
        rows = rows.filter((x) => x !== r);
        if (r.tr) r.tr.remove();
        continue;
      }
      r.deleted = !allDeleted;
      setSelected(r, false);
      updateRowClass(r);
    }
    syncSelectAll();
    empty.hidden = rows.length > 0;
    emitChange();
  }

  const wire = (v) => (v === SQL_NULL ? SQL_NULL : v === undefined ? SQL_NULL : v);

  function whereFor(r) {
    const where = {};
    for (const name of pk) {
      const idx = columns.findIndex((c) => c.name === name);
      const v = idx >= 0 ? r.values[idx] : null;
      where[name] = v === null || v === undefined ? SQL_NULL : v;
    }
    return where;
  }

  /** Pending changes as {inserts, updates, deletes}; values are wire-ready
   * (strings as typed, SQL_NULL for explicit NULL). */
  function changes() {
    const inserts = [];
    const updates = [];
    const deletes = [];
    for (const r of rows) {
      if (r.inserted) {
        const values = {};
        for (const [idx, v] of r.edits) values[columns[idx].name] = wire(v);
        inserts.push({ row: r, values });
      } else if (r.deleted) {
        deletes.push({ row: r, where: whereFor(r) });
      } else if (r.edits.size) {
        const values = {};
        for (const [idx, v] of r.edits) values[columns[idx].name] = wire(v);
        updates.push({ row: r, values, where: whereFor(r) });
      }
    }
    return { inserts, updates, deletes };
  }

  function dirtyCount() {
    const c = changes();
    return c.inserts.length + c.updates.length + c.deletes.length;
  }

  function clearChanges() {
    closeEditor(false);
    rows = rows.filter((r) => !r.inserted);
    for (const r of rows) {
      r.edits.clear();
      r.deleted = false;
    }
    renderBody();
    emitChange();
  }

  function selectedCount() {
    return rows.filter((r) => r.selected).length;
  }

  function setSort(s) {
    sort = s && s.column ? { column: s.column, dir: s.dir === "desc" ? "desc" : "asc" } : null;
    renderHead();
  }

  function setFilters(f) {
    filters = Array.isArray(f) ? f.slice() : [];
    renderHead();
  }

  function showFilters(on) {
    filtersShown = !!on;
    if (!filtersShown) filters = [];
    renderHead();
  }

  function destroy() {
    closeEditor(false);
    root.remove();
  }

  return {
    el: root,
    setData,
    setSort,
    setFilters,
    showFilters,
    filtersVisible: () => filtersShown,
    getFilters: () => filters.slice(),
    insertRow,
    deleteSelected,
    changes,
    dirtyCount,
    clearChanges,
    selectedCount,
    rowCount: () => rows.filter((r) => !r.inserted).length,
    columnCount: () => columns.length,
    closeEditor,
    destroy,
  };
}
