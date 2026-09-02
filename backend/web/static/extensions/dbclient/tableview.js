/** The table view (surface "table", multi-instance; also embedded in the
 * explorer's table detail): DATA | STRUCTURE | DDL, unified with the query.
 *
 * DATA leads with the SQL bar: the SELECT behind the page, regenerated
 * (buildTableSql) every time a sort click, filter, page turn or page-size
 * change reshapes it — the buttons alter the query and the query is SHOWN.
 * The text is editable: Ctrl+Enter (or Run) with the generated text merely
 * reloads the page; with edited text it runs as a custom query through
 * POST /query, whose result set renders read-only in the same grid until the
 * Table button (or any sort/filter/page action) returns to the managed page.
 * The managed page itself still loads through /table-data with structured,
 * bound parameters — the SQL bar mirrors it, it is never the loader.
 *
 * Below the bar, the editable page grid: header-click sort, a per-column
 * filter row, checkbox selection, double-click editing with Set-NULL,
 * Insert / Delete selected / Save, CSV/JSON export through a plain anchor
 * GET, and a footer with prev/next + "rows X–Y" (" of ~N" only when the
 * server had a cheap estimate). Views and tables without a primary key render
 * read-only with a badge saying why — the backend refuses to write them
 * anyway (no where-all-columns fallback in v1), so the UI says so up front.
 *
 * Save is a two-step conversation with POST /rows: preview:true returns the
 * generated SQL, which is shown in a confirm bar; confirming resends with
 * preview:false. Any op the server marks stale (the row changed underneath
 * us) rolls back the whole batch server-side; the pane says so and refreshes.
 *
 * Identity comes from host.ctx ({connId, database, schema, table}); with no
 * ctx (a pane opened from the palette) a picker asks for one first. */

import { el, button, option, svgIcon, confirmBar, dismissConfirm, makeNotice, showOverlay, debounce, errMsg, fmtNum, fmtBytes, copyText } from "./ui.js";
import { createGrid, RENDER_CAP } from "./grid.js";
import { buildTableSql, classifyStatement, tableLabel } from "./sql.js";
import { connUrl, connectionById, fetchTable, pkColumns, createScopePicker } from "./explorer.js";

/** The page index the server calls the first page. ONE place to flip if the
 * backend counts from 0. */
const FIRST_PAGE = 1;
const PAGE_SIZES = [50, 100, 500];

/** The stale ops in a /rows execute reply, whichever of the doc-compatible
 * shapes the server chose (a top-level `stale` list, or per-op flags). */
export function staleOps(res) {
  if (!res || typeof res !== "object") return [];
  if (Array.isArray(res.stale)) return res.stale;
  const ops = Array.isArray(res.results) ? res.results : Array.isArray(res.operations) ? res.operations : [];
  return ops.filter((o) => o && o.stale);
}

export function renderTableView(shared, host) {
  const api = shared.api;
  const ctx = host.ctx && typeof host.ctx === "object" ? host.ctx : {};
  let disposed = false;

  const st = {
    connId: ctx.connId || null,
    database: ctx.database || null,
    schema: ctx.schema || null,
    table: ctx.table || null,
    conn: null,
    info: null,
    pk: [],
    kind: "table",
    readOnlyWhy: "",
    page: FIRST_PAGE,
    pageSize: PAGE_SIZES[0],
    sort: null, // {column, dir}
    filters: [],
    hasMore: false,
    total: null,
    rowsOnPage: 0,
    busy: false,
    /** {sql} while an edited query's result set is on the grid instead of the
     * managed page — cleared by loadData (every managed action). */
    custom: null,
  };

  const root = el("div", { class: "dbc-pane dbc-tableview" });
  host.el.appendChild(root);

  if (!st.connId || !st.table) renderPicker();
  else start();

  // --- fallback picker ----------------------------------------------------
  function renderPicker() {
    let picker = null;
    const openBtn = button("Open", {
      kind: "primary",
      disabled: true,
      onClick: () => {
        const s = picker.scope();
        if (!s.connId || !s.table) return;
        st.connId = s.connId;
        st.database = s.database;
        st.schema = s.schema;
        st.table = s.table;
        picker.dispose();
        box.remove();
        start();
      },
    });
    picker = createScopePicker(api, {
      withTable: true,
      onChange: (s) => {
        openBtn.disabled = !(s.connId && s.table);
      },
      onError: (err) => pickNotice.set(errMsg(err), "error"),
    });
    const pickNotice = makeNotice();
    const box = el(
      "div",
      { class: "dbc-picker" },
      el("p", { class: "dbc-hint", text: "Choose a table to browse." }),
      el("div", { class: "dbc-picker-row" }, picker.el, openBtn),
      pickNotice.el
    );
    root.appendChild(box);
  }

  // --- pane chrome --------------------------------------------------------
  let grid = null;
  let tabs = null;
  let dataBody = null;
  let structBody = null;
  let ddlBody = null;
  let notice = null;
  let confirmSlot = null;
  let footerText = null;
  let prevBtn = null;
  let nextBtn = null;
  let saveBtn = null;
  let insertBtn = null;
  let deleteBtn = null;
  let filterBtn = null;
  let sizeSel = null;
  let roBadge = null;
  let refreshBtn = null;
  let exportCsv = null;
  let exportJson = null;
  let sqlInput = null;
  let runBtn = null;
  let tableBtn = null;
  /** The last buildTableSql output — the yardstick for "has the user edited". */
  let lastGenerated = "";
  /** Monotonic request token: only the newest load/run may render, so a slow
   * page response can never repaint over a newer one (or a custom result). */
  let loadGen = 0;
  const onFilter = debounce((filters) => {
    // Through guardDirty like every managed action — typing a filter must
    // not silently reload over unsaved edits.
    guardDirty(() => {
      st.filters = filters;
      st.page = FIRST_PAGE;
      loadData();
    });
  }, 350);

  function build() {
    // st.conn is still null on the first build (the connection loads after),
    // and the engine-less label — every engine's default schema hidden — is the
    // right answer for a title either way.
    const ident = tableLabel(st.schema, st.table, st.conn && st.conn.engine);
    host.setTitle(ident);
    roBadge = el("span", { class: "dbc-badge ro", hidden: true }, svgIcon("lock"), el("span"));

    const tabDefs = [
      ["data", "DATA"],
      ["structure", "STRUCTURE"],
      ["ddl", "DDL"],
    ];
    tabs = el("div", { class: "dbc-tabs", role: "tablist" });
    for (const [key, label] of tabDefs) {
      tabs.appendChild(
        el("button", {
          type: "button",
          class: "dbc-tab" + (key === "data" ? " active" : ""),
          role: "tab",
          dataset: { tab: key },
          text: label,
          onClick: () => showTab(key),
        })
      );
    }
    tabs.appendChild(el("span", { class: "dbc-ident mono", text: ident, title: ident + " · " + (st.conn ? st.conn.name || st.conn.id : "") }));
    tabs.appendChild(roBadge);

    // DATA
    notice = makeNotice();
    confirmSlot = el("div", { class: "dbc-confirm-slot" });
    grid = createGrid({
      editable: true,
      selectable: true,
      filterable: true,
      sortable: true,
      renderCap: RENDER_CAP,
      onSort: cycleSort,
      onFilter,
      onInspect: (v) =>
        showOverlay(root, {
          title: "Cell value (read only)",
          note: "first " + fmtBytes(String(v.text || "").length) + " of " + fmtBytes(v.len),
          text: String(v.text || ""),
        }),
      onChange: syncButtons,
    });
    sizeSel = el("select", { class: "dbc-select", title: "Rows per page" });
    for (const n of PAGE_SIZES) sizeSel.appendChild(option(String(n), String(n) + " / page", n === st.pageSize));
    sizeSel.addEventListener("change", () =>
      guardDirty(
        () => {
          st.pageSize = Number(sizeSel.value) || PAGE_SIZES[0];
          st.page = FIRST_PAGE;
          loadData();
        },
        // Declined: the select must snap back to the size still in force.
        () => {
          sizeSel.value = String(st.pageSize);
        }
      )
    );
    refreshBtn = button("Refresh", {
      kind: "icon",
      icon: "refresh",
      title: "Reload this page (re-runs a custom SELECT as is)",
      // Only a custom statement that RETURNED ROWS is re-run verbatim; a
      // custom write must never be silently executed again — refreshing
      // after one means "show me the table now".
      onClick: () => guardDirty(() => (st.custom && st.custom.isQuery ? runCustom(st.custom.sql) : loadData())),
    });
    filterBtn = button("Filter", {
      icon: "filter",
      title: "Show or hide the per-column filter row",
      onClick: () => {
        const on = !grid.filtersVisible();
        grid.showFilters(on);
        filterBtn.classList.toggle("active", on);
        if (!on && st.filters.length) {
          st.filters = [];
          st.page = FIRST_PAGE;
          loadData();
        }
      },
    });
    insertBtn = button("Insert", { icon: "plus", title: "Add a row (saved with Save)", onClick: () => grid.insertRow() });
    deleteBtn = button("Delete selected", { icon: "trash", title: "Mark the selected rows for deletion (saved with Save)", onClick: () => grid.deleteSelected() });
    saveBtn = button("Save", { kind: "primary", icon: "save", title: "Preview the generated SQL, then apply", disabled: true, onClick: save });
    exportCsv = exportLink("csv");
    exportJson = exportLink("json");
    const toolbar = el(
      "div",
      { class: "dbc-toolbar" },
      refreshBtn,
      sizeSel,
      filterBtn,
      el("span", { class: "dbc-sep" }),
      insertBtn,
      deleteBtn,
      saveBtn,
      el("span", { class: "dbc-spacer" }),
      // ONE control with two formats: as two separate buttons they were the
      // first things the toolbar broke onto a second line, which read as two
      // unrelated actions stacked up.
      el("span", { class: "dbc-export" }, svgIcon("download"), exportCsv, exportJson)
    );
    prevBtn = button("Previous page", { kind: "icon", icon: "arrow-left", title: "Previous page", disabled: true, onClick: () => page(-1) });
    nextBtn = button("Next page", { kind: "icon", icon: "arrow-right", title: "Next page", disabled: true, onClick: () => page(1) });
    footerText = el("span", { class: "dbc-footer-text muted", text: "Loading…" });
    const footer = el("div", { class: "dbc-footer" }, prevBtn, footerText, nextBtn);

    // The SQL bar: the query behind the page, visibly rewritten by every sort/
    // filter/page action; editable, Ctrl+Enter to run (see the module header).
    sqlInput = el("textarea", {
      class: "dbc-sql dbc-sqlbar mono",
      spellcheck: false,
      rows: 1,
      "aria-label": "The SQL behind this page — edit and press Ctrl+Enter to run a custom query",
      onInput: () => {
        autosizeSql();
        markSqlState();
      },
      onKeyDown: (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
          e.preventDefault();
          runSql();
        }
      },
    });
    runBtn = button("Run", {
      kind: "icon",
      icon: "play",
      title: "Run the SQL above (Ctrl+Enter) — an edited query runs as a custom, read-only result",
      onClick: runSql,
    });
    tableBtn = button("Table", {
      kind: "icon",
      icon: "table",
      title: "Back to the managed table page (regenerates the query)",
      onClick: resetToManaged,
    });
    tableBtn.hidden = true;
    const sqlRow = el("div", { class: "dbc-sqlbar-row" }, sqlInput, tableBtn, runBtn);

    dataBody = el("div", { class: "dbc-tab-body data" }, sqlRow, toolbar, notice.el, confirmSlot, grid.el, footer);

    // STRUCTURE / DDL
    structBody = el("div", { class: "dbc-tab-body structure", hidden: true });
    ddlBody = el("div", { class: "dbc-tab-body ddl", hidden: true });

    root.replaceChildren(tabs, dataBody, structBody, ddlBody);
  }

  function showTab(key) {
    for (const b of tabs.querySelectorAll(".dbc-tab")) b.classList.toggle("active", b.dataset.tab === key);
    dataBody.hidden = key !== "data";
    structBody.hidden = key !== "structure";
    ddlBody.hidden = key !== "ddl";
  }

  function exportLink(format) {
    const qs = new URLSearchParams();
    if (st.database) qs.set("database", st.database);
    if (st.schema) qs.set("schema", st.schema);
    qs.set("table", st.table);
    qs.set("format", format);
    // A real anchor: the browser streams the GET to disk with the session
    // cookie attached — no blob buffering for a possibly large table.
    return el(
      "a",
      {
        class: "dbc-btn dbc-export-fmt",
        href: connUrl(st.connId, "/export?" + qs.toString()),
        download: st.table + "." + format,
        title: "Download the whole table as " + format.toUpperCase(),
      },
      el("span", { text: format.toUpperCase() })
    );
  }

  // --- data -----------------------------------------------------------------
  async function start() {
    root.replaceChildren(el("p", { class: "dbc-hint dbc-pad", text: "Loading " + st.table + "…" }));
    try {
      st.conn = await connectionById(api, st.connId);
      if (disposed) return;
      if (!st.conn) throw new Error("connection " + st.connId + " no longer exists");
      st.info = await fetchTable(api, st.connId, { database: st.database, schema: st.schema, table: st.table }, true);
    } catch (err) {
      if (disposed) return;
      root.replaceChildren(el("div", { class: "dbc-notice error dbc-pad", text: "Could not open " + st.table + ": " + errMsg(err) }));
      return;
    }
    st.pk = pkColumns(st.info);
    st.kind = st.info.kind || (ctx.kind ? String(ctx.kind) : "table");
    if (st.conn.read_only) st.readOnlyWhy = "read-only connection";
    else if (st.kind !== "table") st.readOnlyWhy = st.kind + " — read only";
    else if (!st.pk.length) st.readOnlyWhy = "no primary key — read only";
    else st.readOnlyWhy = "";

    build();
    applyReadOnly();
    // Wide tables: cap the page so 200 rendered columns × rows stays sane.
    if (st.info.columns.length > RENDER_CAP) {
      st.pageSize = PAGE_SIZES[0];
      sizeSel.value = String(st.pageSize);
      sizeSel.disabled = true;
      sizeSel.title = "Page size is fixed at 50 for tables with more than " + RENDER_CAP + " columns";
    }
    renderStructure();
    renderDdl();
    await loadData();
  }

  function applyReadOnly() {
    const why = st.readOnlyWhy || (st.custom ? "custom query — results are read only" : "");
    const ro = !!why;
    roBadge.hidden = !ro;
    roBadge.lastChild.textContent = why;
    roBadge.title = ro ? "Editing is disabled: " + why : "";
    insertBtn.disabled = ro;
    deleteBtn.disabled = ro;
    saveBtn.hidden = ro;
    insertBtn.hidden = ro;
    deleteBtn.hidden = ro;
    // A custom result set's columns are not the table's — the per-column
    // filter row (and its collected filters) only make sense on the page.
    filterBtn.disabled = !!st.custom;
    if (grid) grid.setEditable(!ro);
    root.classList.toggle("read-only", ro);
    root.classList.toggle("custom-sql", !!st.custom);
  }

  function syncButtons() {
    if (!grid) return;
    const dirty = grid.dirtyCount();
    saveBtn.disabled = st.busy || dirty === 0;
    saveBtn.replaceChildren(svgIcon("save"), el("span", { text: dirty ? "Save (" + dirty + ")" : "Save" }));
    deleteBtn.disabled = !!st.readOnlyWhy || grid.selectedCount() === 0;
  }

  function setBusy(on) {
    st.busy = on;
    root.classList.toggle("busy", on);
    refreshBtn.disabled = on;
    if (runBtn) runBtn.disabled = on;
    if (tableBtn) tableBtn.disabled = on;
    // Pages belong to the managed query; a custom result set has no pages.
    prevBtn.disabled = on || !!st.custom || st.page <= FIRST_PAGE;
    nextBtn.disabled = on || !!st.custom || !st.hasMore;
    syncButtons();
  }

  async function loadData() {
    if (disposed || !grid) return;
    dismissConfirm(confirmSlot);
    // Every managed action lands here, so this is where a custom result set
    // hands the grid back to the page — and where the SQL bar is regenerated
    // to show exactly the query the buttons just built.
    const wasCustom = !!st.custom;
    st.custom = null;
    applyReadOnly();
    syncSql();
    const my = ++loadGen;
    setBusy(true);
    footerText.textContent = "Loading…";
    const body = {
      database: st.database || null,
      schema: st.schema || null,
      table: st.table,
      page: st.page,
      page_size: st.pageSize,
      sort: st.sort ? [{ column: st.sort.column, dir: st.sort.dir }] : [],
      filters: st.filters.map((f) => ({ column: f.column, op: f.op, value: f.value })),
    };
    let res;
    try {
      res = await api.request(connUrl(st.connId, "/table-data"), { json: body });
    } catch (err) {
      if (disposed || my !== loadGen) return;
      notice.set(errMsg(err), "error");
      footerText.textContent = "Load failed.";
      loadFailedAfterCustom(wasCustom);
      setBusy(false);
      return;
    }
    if (disposed || my !== loadGen) return;
    if (!res || typeof res !== "object" || res.ok === false) {
      notice.set((res && res.error) || "Load failed.", "error");
      footerText.textContent = "Load failed.";
      loadFailedAfterCustom(wasCustom);
      setBusy(false);
      return;
    }
    notice.clear();
    const rows = Array.isArray(res.rows) ? res.rows : [];
    const columns = Array.isArray(res.columns) && res.columns.length ? res.columns : st.info.columns;
    if (Array.isArray(res.pk) && res.pk.length) st.pk = res.pk.map(String);
    if (res.kind && res.kind !== st.kind) {
      st.kind = String(res.kind);
      if (!st.conn.read_only && st.kind !== "table") {
        st.readOnlyWhy = st.kind + " — read only";
        applyReadOnly();
      }
    }
    st.hasMore = !!res.has_more;
    st.total = typeof res.total_approx === "number" ? res.total_approx : null;
    st.rowsOnPage = rows.length;
    const from = (st.page - FIRST_PAGE) * st.pageSize + 1;
    grid.setData({ columns, rows, pk: st.pk, startIndex: from });
    grid.setSort(st.sort);
    // A custom run hides the filter row (its columns are not the table's);
    // coming back with live filters must show them again or the page would
    // be silently filtered with the Filter button unlit.
    if (st.filters.length && !grid.filtersVisible()) {
      grid.showFilters(true);
      filterBtn.classList.add("active");
    }
    grid.setFilters(st.filters);
    const to = from + rows.length - 1;
    footerText.textContent =
      (rows.length ? "rows " + fmtNum(from) + "–" + fmtNum(to) : st.page > FIRST_PAGE ? "no more rows" : "no rows") +
      (st.total !== null ? " of ~" + fmtNum(st.total) : "");
    setBusy(false);
  }

  /** A managed load that failed right after leaving custom mode must not
   * leave the custom rows sitting in a grid whose editing UI is re-armed —
   * clear them; the footer already says the load failed. */
  function loadFailedAfterCustom(wasCustom) {
    if (wasCustom) grid.setData({ columns: st.info.columns, rows: [], pk: st.pk, startIndex: 1 });
  }

  function page(delta) {
    guardDirty(() => {
      const next = st.page + delta;
      if (next < FIRST_PAGE || (delta > 0 && !st.hasMore)) return;
      st.page = next;
      loadData();
    });
  }

  /** none → asc → desc → none, single column. A sort click always returns to
   * the managed page (loadData clears custom), so on a custom result set it
   * only applies when the clicked column is a real table column. */
  function cycleSort(column) {
    if (st.custom && !(st.info.columns || []).some((c) => c.name === column)) return;
    guardDirty(() => {
      if (!st.sort || st.sort.column !== column) st.sort = { column, dir: "asc" };
      else if (st.sort.dir === "asc") st.sort = { column, dir: "desc" };
      else st.sort = null;
      st.page = FIRST_PAGE;
      loadData();
    });
  }

  // --- the SQL bar ----------------------------------------------------------
  function generatedSql() {
    const columnTypes = {};
    for (const c of (st.info && st.info.columns) || []) columnTypes[c.name] = c.type || "";
    return buildTableSql({
      engine: st.conn ? st.conn.engine : "",
      schema: st.schema,
      table: st.table,
      filters: st.filters,
      sort: st.sort,
      limit: st.pageSize,
      offset: (st.page - FIRST_PAGE) * st.pageSize,
      columnTypes,
    });
  }

  /** Regenerate the visible SQL from the structured state (managed mode). */
  function syncSql() {
    if (!sqlInput) return;
    lastGenerated = generatedSql();
    sqlInput.value = lastGenerated;
    autosizeSql();
    markSqlState();
  }

  function autosizeSql() {
    sqlInput.style.height = "auto";
    sqlInput.style.height = Math.min(sqlInput.scrollHeight, 132) + "px";
  }

  /** Edited text gets the tint + the way back; pristine text stays chrome. */
  function markSqlState() {
    const edited = sqlInput.value.trim() !== lastGenerated.trim();
    sqlInput.classList.toggle("edited", edited);
    tableBtn.hidden = !edited && !st.custom;
  }

  function runSql() {
    if (!grid || st.busy) return;
    const text = sqlInput.value.trim();
    if (!text) return;
    // Unedited text: Run is just Refresh. Edited text: a custom query.
    if (text === lastGenerated.trim() && !st.custom) return guardDirty(loadData);
    guardDirty(() => runCustom(text));
  }

  /** Run edited SQL through POST /query; the result set replaces the page
   * (read-only) until any managed action calls loadData. The server owns the
   * guards — single statement, needs_confirm for a no-WHERE write. */
  async function runCustom(sql, confirm) {
    dismissConfirm(confirmSlot);
    const my = ++loadGen;
    setBusy(true);
    footerText.textContent = "Running…";
    const body = { sql };
    if (st.database) body.database = st.database;
    if (st.schema) body.schema = st.schema;
    if (confirm) body.confirm = true;
    let res;
    try {
      res = await api.request(connUrl(st.connId, "/query"), { json: body });
    } catch (err) {
      if (disposed || my !== loadGen) return;
      notice.set(errMsg(err), "error");
      footerText.textContent = "Query failed.";
      setBusy(false);
      return;
    }
    if (disposed || my !== loadGen) return;
    if (res && res.needs_confirm) {
      setBusy(false);
      const cls = classifyStatement(sql, {
        dialect: st.conn && st.conn.engine === "mysql" ? "mysql" : "standard",
      });
      const yes = await confirmBar(confirmSlot, {
        danger: true,
        title:
          (cls.verb || "This statement") +
          (cls.hasWhere ? " needs confirmation" : " has no WHERE clause — it will affect every row"),
        pre: sql,
        confirmLabel: "Run anyway",
      });
      if (disposed) return;
      if (!yes) {
        footerText.textContent = "Cancelled.";
        return;
      }
      return runCustom(sql, true);
    }
    if (!res || typeof res !== "object" || res.ok === false) {
      notice.set((res && res.error) || "The statement failed.", "error");
      footerText.textContent = "Query failed.";
      setBusy(false);
      return;
    }
    notice.clear();
    const cols = Array.isArray(res.columns) ? res.columns : [];
    st.custom = { sql, isQuery: cols.length > 0 };
    // The filter row's columns belong to the table, not this result set.
    if (grid.filtersVisible()) {
      grid.showFilters(false);
      filterBtn.classList.remove("active");
    }
    applyReadOnly();
    const rows = Array.isArray(res.rows) ? res.rows : [];
    grid.setData({ columns: cols, rows, pk: [], startIndex: 1 });
    grid.setSort(null);
    const ms = typeof res.elapsed_ms === "number" ? fmtNum(Math.round(res.elapsed_ms)) + " ms" : "";
    if (cols.length) {
      const n = typeof res.row_count === "number" ? res.row_count : rows.length;
      footerText.textContent =
        fmtNum(n) +
        (n === 1 ? " row" : " rows") +
        (ms ? " · " + ms : "") +
        (res.truncated ? " · truncated at " + fmtNum(n) : "") +
        " · custom query";
    } else {
      const affected = typeof res.affected === "number" ? res.affected : null;
      footerText.textContent =
        (affected === null ? "OK" : "affected " + fmtNum(affected)) + (ms ? " · " + ms : "") + " · custom query";
    }
    markSqlState();
    setBusy(false);
  }

  function resetToManaged() {
    if (st.busy) return;
    guardDirty(loadData);
  }

  /** Run `fn` now, or after the user agrees to drop pending edits;
   * `onDecline` runs when they refuse (to undo optimistic control state). */
  async function guardDirty(fn, onDecline) {
    if (!grid || grid.dirtyCount() === 0) return fn();
    const n = grid.dirtyCount();
    const yes = await confirmBar(confirmSlot, {
      danger: true,
      title: "Discard " + n + " unsaved change" + (n === 1 ? "" : "s") + "?",
      confirmLabel: "Discard",
    });
    if (disposed) return;
    if (!yes) {
      if (onDecline) onDecline();
      return;
    }
    grid.clearChanges();
    fn();
  }

  // --- save -----------------------------------------------------------------
  async function save() {
    if (!grid || st.busy || st.readOnlyWhy) return;
    // Commit the open cell editor FIRST or its typed text misses the batch.
    grid.closeEditor(true);
    const c = grid.changes();
    const operations = [
      ...c.inserts.map((x) => ({ action: "insert", values: x.values })),
      ...c.updates.map((x) => ({ action: "update", values: x.values, where_pk: x.where })),
      ...c.deletes.map((x) => ({ action: "delete", where_pk: x.where })),
    ];
    if (!operations.length) return;
    notice.clear();
    const base = { database: st.database || null, schema: st.schema || null, table: st.table, operations };
    setBusy(true);
    let preview;
    try {
      preview = await api.request(connUrl(st.connId, "/rows"), { json: { ...base, preview: true } });
    } catch (err) {
      if (disposed) return;
      notice.set(errMsg(err), "error");
      setBusy(false);
      return;
    }
    if (disposed) return;
    setBusy(false);
    if (!preview || preview.ok === false) {
      notice.set((preview && preview.error) || "Could not generate the statements.", "error");
      return;
    }
    const statements = Array.isArray(preview.statements) ? preview.statements : [];
    const pre = statements
      .map((s) => {
        const params = s && s.params;
        const hasParams = params && (Array.isArray(params) ? params.length : Object.keys(params).length);
        return (s && s.sql ? s.sql : "") + (hasParams ? "\n  -- params: " + JSON.stringify(params) : "");
      })
      .join("\n\n");
    const summary = [
      c.inserts.length ? c.inserts.length + " insert" + (c.inserts.length === 1 ? "" : "s") : "",
      c.updates.length ? c.updates.length + " update" + (c.updates.length === 1 ? "" : "s") : "",
      c.deletes.length ? c.deletes.length + " delete" + (c.deletes.length === 1 ? "" : "s") : "",
    ]
      .filter(Boolean)
      .join(", ");
    const yes = await confirmBar(confirmSlot, {
      danger: c.deletes.length > 0,
      title: "Apply " + summary + " to " + st.table + "? One transaction; the SQL that will run:",
      pre: pre || "(no statements returned)",
      confirmLabel: "Apply",
    });
    if (disposed || !yes) return;
    setBusy(true);
    let res;
    try {
      res = await api.request(connUrl(st.connId, "/rows"), { json: { ...base, preview: false } });
    } catch (err) {
      if (disposed) return;
      notice.set(errMsg(err), "error");
      setBusy(false);
      return;
    }
    if (disposed) return;
    setBusy(false);
    const stale = staleOps(res);
    if (stale.length) {
      notice.set(
        stale.length + " row" + (stale.length === 1 ? " was" : "s were") + " modified elsewhere — nothing was saved; the page was refreshed. Re-apply your edits on the fresh rows.",
        "warn"
      );
      grid.clearChanges();
      await loadData();
      return;
    }
    if (!res || res.ok === false) {
      notice.set((res && res.error) || "Save failed.", "error");
      return;
    }
    api.ui.toast("Saved " + summary + " to " + st.table);
    grid.clearChanges();
    await loadData();
  }

  // --- structure / ddl ------------------------------------------------------
  function renderStructure() {
    const info = st.info;
    const cols = el(
      "table",
      { class: "dbc-cols-table" },
      el(
        "thead",
        null,
        el(
          "tr",
          null,
          el("th", { text: "" }),
          el("th", { text: "Column" }),
          el("th", { text: "Type" }),
          el("th", { text: "Nullable" }),
          el("th", { text: "Default" }),
          el("th", { text: "Auto" })
        )
      )
    );
    const tb = el("tbody");
    for (const c of info.columns) {
      tb.appendChild(
        el(
          "tr",
          null,
          el("td", { class: "dbc-pk-cell" }, c.pk ? svgIcon("key", "dbc-pk-icon") : null),
          el("td", { class: "mono", text: c.name }),
          el("td", { class: "mono muted", text: c.type || "" }),
          el("td", { class: "muted", text: c.nullable === false ? "NOT NULL" : "NULL" }),
          el("td", { class: "mono muted", text: c.default == null ? "" : String(c.default) }),
          el("td", { class: "muted", text: c.autoinc ? "yes" : "" })
        )
      );
    }
    cols.appendChild(tb);

    const idx = el(
      "table",
      { class: "dbc-cols-table" },
      el("thead", null, el("tr", null, el("th", { text: "Index" }), el("th", { text: "Columns" }), el("th", { text: "Unique" })))
    );
    const itb = el("tbody");
    for (const i of info.indexes) {
      itb.appendChild(
        el(
          "tr",
          null,
          el("td", { class: "mono", text: i.name || "" }),
          el("td", { class: "mono muted", text: Array.isArray(i.columns) ? i.columns.join(", ") : String(i.columns || "") }),
          el("td", { class: "muted", text: i.unique ? "yes" : "" })
        )
      );
    }
    if (!info.indexes.length) itb.appendChild(el("tr", null, el("td", { class: "muted", colSpan: 3, text: "No indexes" })));
    idx.appendChild(itb);

    structBody.replaceChildren(
      el("div", { class: "dbc-struct" },
        el("h4", { class: "dbc-struct-title", text: "Columns (" + info.columns.length + ")" }),
        el("div", { class: "dbc-cols-wrap" }, cols),
        el("h4", { class: "dbc-struct-title", text: "Indexes (" + info.indexes.length + ")" }),
        el("div", { class: "dbc-cols-wrap" }, idx),
        st.pk.length ? el("p", { class: "dbc-hint", text: "Primary key: " + st.pk.join(", ") }) : el("p", { class: "dbc-hint", text: "No primary key." })
      )
    );
  }

  function renderDdl() {
    const ddl = st.info.ddl || "";
    ddlBody.replaceChildren(
      el(
        "div",
        { class: "dbc-toolbar" },
        el("span", { class: "dbc-hint", text: ddl ? "As reported by the engine." : "The engine returned no DDL for this object." }),
        el("span", { class: "dbc-spacer" }),
        button("Copy", { icon: "copy", disabled: !ddl, onClick: () => copyText(ddl).then((ok) => api.ui.toast(ok ? "DDL copied" : "Copy failed")) })
      ),
      el("pre", { class: "dbc-code dbc-ddl-pre", text: ddl || "" })
    );
  }

  return {
    /** Unsaved grid edits, for the embed host's discard confirm. */
    dirtyCount() {
      return grid ? grid.dirtyCount() : 0;
    },
    dispose() {
      disposed = true;
      onFilter.cancel();
      if (confirmSlot) dismissConfirm(confirmSlot);
      if (grid) grid.destroy();
      root.remove();
    },
  };
}
