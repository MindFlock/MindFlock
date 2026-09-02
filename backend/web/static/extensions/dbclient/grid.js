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
  const colgroup = el("colgroup");
  const table = el("table", { class: "dbc-table" }, colgroup, thead, tbody);
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
  /** The row a plain checkbox tick last landed on — the anchor a shift+click
   * extends FROM. Held as the row object, not an index, so an insert, a sort
   * or a page turn can only make it stale (indexOf -> -1, treated as a plain
   * tick), never make it point at the wrong row. */
  let anchorRow = null;
  /** Column name -> px, set by dragging a header edge. Keyed by NAME, not
   * index, so a width survives a reload, a sort, a page change and a re-render
   * — and a result set with different columns simply has none, and lays itself
   * out content-first again. */
  const widths = new Map();
  /** Measured "wide enough to read the name" width per column — the default,
   * recomputed on every render because it depends on the font and the names. */
  let defaults = new Map();
  /** The two fixed helper columns (checkbox, row number), measured the same
   * way: under fixed layout every column needs a width or the browser hands
   * them an equal share. */
  let helperPx = null;
  let colFor = new Map(); // column name -> its <col>

  const emitChange = () => {
    if (o.onChange) o.onChange();
  };

  // --- column widths ---------------------------------------------------------
  // A column starts exactly wide enough to READ ITS NAME (name + type + the pk
  // key), because that is what a column IS until you look at it: a header you
  // cannot read is worse than a value you cannot, and the value is one drag or
  // one double-click away. Sizing to content instead — the browser's default —
  // spends the width on whichever column happens to hold a long URL and
  // ellipsises the headers of all the others.
  //
  // So every column is measured and pinned on each load, and the table runs in
  // fixed layout from the first paint: dragging one edge then moves one edge
  // (auto layout re-solves the whole grid on every mousemove), and the filler
  // column takes whatever is left over.

  const MIN_COL_PX = 48;
  const MAX_COL_PX = 1200;
  /** A default never exceeds this — a 60-character column name is not a reason
   * to give one column the whole window. */
  const MAX_DEFAULT_PX = 320;
  /** Room for the grip so it never sits on top of the last letter. */
  const GRIP_PX = 9;
  /** What the filter row's select + input need to stay usable; a column narrows
   * below it only when the user drags it there. */
  const FILTER_ROW_PX = 150;
  /** Cells before the first data column: the select box (optional) and "#". */
  const leadCells = () => (o.selectable ? 2 : 1);

  /** The width of what a cell actually CONTAINS, ignoring how wide the cell
   * happens to be.
   *
   * `scrollWidth` cannot answer this — for a cell wider than its content it
   * returns the cell — and neither can a Range over the whole cell, which would
   * union in the absolutely-positioned grip sitting at the right edge. So: sum
   * the flow children (with their margins) and any text nodes.
   */
  function contentWidth(cell) {
    let w = 0;
    for (const node of cell.childNodes) {
      if (node.nodeType === 1) {
        if (node.classList.contains("dbc-col-resize")) continue;
        const cs = getComputedStyle(node);
        w +=
          node.getBoundingClientRect().width +
          (parseFloat(cs.marginLeft) || 0) +
          (parseFloat(cs.marginRight) || 0);
      } else if (node.nodeType === 3 && node.textContent.trim()) {
        const range = document.createRange();
        range.selectNode(node);
        w += range.getBoundingClientRect().width;
      }
    }
    const cs = getComputedStyle(cell);
    return w + (parseFloat(cs.paddingLeft) || 0) + (parseFloat(cs.paddingRight) || 0);
  }

  /** Measure every header cell and keep it as that column's default width.
   * Called after each render; a grid that is not on screen yet measures zero,
   * and then simply keeps the auto layout until the next one. */
  function measureDefaults() {
    const cells = [...thead.querySelectorAll("tr:first-child > th")];
    if (!cells.length || !cells[0].getBoundingClientRect().width) return;
    const lead = leadCells();
    // The row-number column is the one helper whose CONTENT outgrows its
    // header: "#" is 8px wide and "1,234" is not, so it is measured against the
    // widest number on the page rather than against its own title.
    let rownum = Math.ceil(contentWidth(cells[lead - 1]));
    for (const tr of tbody.rows) {
      const cell = tr.cells[lead - 1];
      if (cell) rownum = Math.max(rownum, cell.scrollWidth + 2);
    }
    helperPx = {
      sel: o.selectable ? Math.ceil(contentWidth(cells[0])) + 2 : 0,
      rownum,
    };
    defaults = new Map();
    shown.forEach((c, i) => {
      const th = cells[lead + i];
      if (!th) return;
      const want = Math.ceil(contentWidth(th)) + GRIP_PX;
      const floor = filtersShown ? FILTER_ROW_PX : MIN_COL_PX;
      defaults.set(c.name, Math.max(floor, Math.min(MAX_DEFAULT_PX, want)));
    });
    renderCols();
  }

  /** A user's width if they set one, else the measured default. */
  function widthOf(name) {
    return widths.has(name) ? widths.get(name) : defaults.get(name);
  }

  function renderCols() {
    colgroup.replaceChildren();
    colFor = new Map();
    const sized = defaults.size > 0;
    if (o.selectable) {
      colgroup.appendChild(
        el("col", sized && helperPx ? { style: "width:" + helperPx.sel + "px" } : null)
      );
    }
    colgroup.appendChild(
      el("col", sized && helperPx ? { style: "width:" + helperPx.rownum + "px" } : null)
    );
    for (const c of shown) {
      const w = widthOf(c.name);
      const col = el("col", w ? { style: "width:" + w + "px" } : null);
      colFor.set(c.name, col);
      colgroup.appendChild(col);
    }
    // The filler. Under fixed layout the table still stretches to fill its
    // scroller (min-width: 100%), and without somewhere for that slack to go it
    // is shared across the columns — so dragging one edge 200px moved it 60 and
    // nudged every other column. This one column takes all of it, and collapses
    // to nothing the moment the real columns are wider than the view.
    colgroup.appendChild(el("col", { class: "dbc-col-filler" }));
    table.classList.toggle("cols-fixed", sized);
    // A DEFINITE width, which fixed layout needs before it will honour a <col>
    // at all: with `width: auto` the browser first solves the table by content
    // and then scales the columns to fit it, so a 200px drag came out as a 60px
    // nudge. 100% of the scroller, with the table free to grow past it (the
    // spec takes the max of the specified width and the sum of the columns) so
    // widening a column scrolls instead of squeezing its neighbours.
    table.style.width = sized ? "100%" : "";
  }

  function setColWidth(name, px) {
    const w = Math.max(MIN_COL_PX, Math.min(MAX_COL_PX, Math.round(px)));
    widths.set(name, w);
    const col = colFor.get(name);
    if (col) col.style.width = w + "px";
  }

  /** Widen (or narrow) a column to its widest value ON THIS PAGE — the escape
   * hatch from a name-width default when it is the values you want to read. A
   * cell is nowrap and clipped, so its scrollWidth is the full text width even
   * when the ellipsis is showing, which is exactly the number wanted here. */
  function autoFitColumn(name) {
    const idx = shown.findIndex((c) => c.name === name);
    if (idx < 0) return;
    const at = leadCells() + idx;
    let px = defaults.get(name) || MIN_COL_PX;
    for (const tr of tbody.rows) {
      const cell = tr.cells[at];
      if (cell) px = Math.max(px, cell.scrollWidth + 6);
    }
    setColWidth(name, px);
  }

  function resizeHandle(c, th) {
    const grip = el("span", {
      class: "dbc-col-resize",
      title: "Drag to resize this column · double-click to fit its widest value",
    });
    grip.addEventListener("pointerdown", (ev) => {
      if (ev.button !== 0) return;
      ev.preventDefault();
      ev.stopPropagation(); // never a sort click
      const startX = ev.clientX;
      const startW = widthOf(c.name) || Math.round(th.getBoundingClientRect().width);
      const move = (e) => setColWidth(c.name, startW + (e.clientX - startX));
      const up = () => {
        document.removeEventListener("pointermove", move);
        document.removeEventListener("pointerup", up);
        document.removeEventListener("pointercancel", up);
        root.classList.remove("resizing");
      };
      document.addEventListener("pointermove", move);
      document.addEventListener("pointerup", up);
      document.addEventListener("pointercancel", up);
      root.classList.add("resizing");
    });
    // The th's click sorts; the grip's must not reach it (pointerdown alone
    // does not stop the click that follows the mouseup).
    grip.addEventListener("click", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
    });
    grip.addEventListener("dblclick", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      autoFitColumn(c.name);
    });
    return grip;
  }

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
      th.appendChild(resizeHandle(c, th));
      tr.appendChild(th);
    }
    tr.appendChild(el("th", { class: "dbc-filler", "aria-hidden": "true" }));
    thead.appendChild(tr);
    if (o.filterable && filtersShown) thead.appendChild(renderFilterRow());
    renderCols();
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
    tr.appendChild(el("th", { class: "dbc-filler", "aria-hidden": "true" }));
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
      // Shift+click extends from the last plainly-ticked row to this one, the
      // gesture every list with checkboxes has — ticking twenty rows for a
      // delete should not be twenty clicks. The range takes the state the
      // clicked box just took, so shift+click UNticks a run just as readily.
      // On `click`, not `change`: only the mouse event carries shiftKey, and it
      // runs with `checked` already flipped and before the change handler that
      // records it. The anchor SURVIVES the extend (shift+click again to
      // re-extend from the same start), and a shift+click with no anchor is an
      // ordinary tick.
      box.addEventListener("click", (ev) => {
        const idx = rows.indexOf(r);
        const anchor = anchorRow ? rows.indexOf(anchorRow) : -1;
        if (!ev.shiftKey || anchor < 0 || idx < 0) {
          anchorRow = r;
          return;
        }
        const lo = Math.min(anchor, idx);
        const hi = Math.max(anchor, idx);
        for (let i = lo; i <= hi; i++) setSelected(rows[i], box.checked);
        // Shift+click inside a table drags a text selection along with it.
        const sel = window.getSelection();
        if (sel) sel.removeAllRanges();
      });
      r.box = box;
      tr.appendChild(el("td", { class: "dbc-sel" }, box));
    }
    tr.appendChild(el("td", { class: "dbc-rownum", text: r.inserted ? "+" : String(startIndex + i) }));
    shown.forEach((c, colIdx) => {
      const td = el("td", { class: "dbc-cell" });
      renderCell(td, r, colIdx);
      // Attached unconditionally: cellEditable gates at click time, so a
      // later setEditable(true) works on rows rendered while read-only.
      td.addEventListener("dblclick", () => openEditor(td, r, colIdx));
      tr.appendChild(td);
    });
    tr.appendChild(el("td", { class: "dbc-filler" }));
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
    anchorRow = null; // new rows, no shift+click anchor to extend from
    // The rebuild below replaces the filter row; if the user is mid-typing in
    // one of its inputs (the debounced reload path), losing focus after every
    // keystroke-pause makes filters untypeable — put the caret back.
    let refocus = null;
    const active = document.activeElement;
    if (active && thead.contains(active) && active.classList.contains("dbc-filter-val")) {
      const cells = [...thead.querySelectorAll(".dbc-filter-cell")];
      refocus = { idx: cells.indexOf(active.closest(".dbc-filter-cell")), pos: active.selectionStart };
    }
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
    // A width belongs to a column that is still here; anything else is a
    // leftover from an unrelated result set.
    const names = new Set(shown.map((c) => c.name));
    for (const name of [...widths.keys()]) if (!names.has(name)) widths.delete(name);
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
    // After the rows exist: the header cells are laid out, so their names can
    // be measured and every column pinned to the width of its own.
    measureDefaults();
    syncSelectAll();
    if (refocus && refocus.idx >= 0) {
      const cell = thead.querySelectorAll(".dbc-filter-cell")[refocus.idx];
      const input = cell && cell.querySelector("input.dbc-filter-val");
      if (input) {
        input.focus();
        try {
          input.setSelectionRange(refocus.pos, refocus.pos);
        } catch (e) {
          /* selection is a nicety */
        }
      }
    }
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
    // The filter row's select + input need more than a short name does, so the
    // defaults have a floor while it is showing (a width the user set stands).
    measureDefaults();
  }

  function destroy() {
    closeEditor(false);
    root.remove();
  }

  /** Flip editing on/off after creation (read-only tables, ad-hoc results).
   * Off also closes any open editor — a cell must not stay editable into a
   * result set that can no longer be saved. */
  function setEditable(on) {
    if (!on) closeEditor(false);
    o.editable = !!on;
  }

  return {
    el: root,
    setData,
    setSort,
    setFilters,
    showFilters,
    setEditable,
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
