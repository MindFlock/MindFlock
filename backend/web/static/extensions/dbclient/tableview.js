/** The table pane (surface "table", multi-instance): DATA | STRUCTURE | DDL.
 *
 * DATA is the editable page grid: header-click sort, a per-column filter row,
 * checkbox selection, double-click editing with Set-NULL, Insert / Delete
 * selected / Save, CSV/JSON export through a plain anchor GET, and a footer
 * with prev/next + "rows X–Y" (" of ~N" only when the server had a cheap
 * estimate). Views and tables without a primary key render read-only with a
 * badge saying why — the backend refuses to write them anyway (no
 * where-all-columns fallback in v1), so the UI says so up front.
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
  const onFilter = debounce((filters) => {
    st.filters = filters;
    st.page = FIRST_PAGE;
    loadData();
  }, 350);

  function build() {
    const ident = (st.schema ? st.schema + "." : "") + st.table;
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
      guardDirty(() => {
        st.pageSize = Number(sizeSel.value) || PAGE_SIZES[0];
        st.page = FIRST_PAGE;
        loadData();
      })
    );
    refreshBtn = button("Refresh", { kind: "icon", icon: "refresh", title: "Reload this page", onClick: () => guardDirty(loadData) });
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
      exportCsv,
      exportJson
    );
    prevBtn = button("Previous page", { kind: "icon", icon: "arrow-left", title: "Previous page", disabled: true, onClick: () => page(-1) });
    nextBtn = button("Next page", { kind: "icon", icon: "arrow-right", title: "Next page", disabled: true, onClick: () => page(1) });
    footerText = el("span", { class: "dbc-footer-text muted", text: "Loading…" });
    const footer = el("div", { class: "dbc-footer" }, prevBtn, footerText, nextBtn);
    dataBody = el("div", { class: "dbc-tab-body data" }, toolbar, notice.el, confirmSlot, grid.el, footer);

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
    const a = el(
      "a",
      {
        class: "dbc-btn",
        href: connUrl(st.connId, "/export?" + qs.toString()),
        download: st.table + "." + format,
        title: "Download the whole table as " + format.toUpperCase(),
      },
      svgIcon("download"),
      el("span", { text: format.toUpperCase() })
    );
    return a;
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
    const ro = !!st.readOnlyWhy;
    roBadge.hidden = !ro;
    roBadge.lastChild.textContent = st.readOnlyWhy;
    roBadge.title = ro ? "Editing is disabled: " + st.readOnlyWhy : "";
    insertBtn.disabled = ro;
    deleteBtn.disabled = ro;
    saveBtn.hidden = ro;
    insertBtn.hidden = ro;
    deleteBtn.hidden = ro;
    root.classList.toggle("read-only", ro);
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
    prevBtn.disabled = on || st.page <= FIRST_PAGE;
    nextBtn.disabled = on || !st.hasMore;
    syncButtons();
  }

  async function loadData() {
    if (disposed || !grid) return;
    dismissConfirm(confirmSlot);
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
      if (disposed) return;
      notice.set(errMsg(err), "error");
      footerText.textContent = "Load failed.";
      setBusy(false);
      return;
    }
    if (disposed) return;
    if (!res || typeof res !== "object" || res.ok === false) {
      notice.set((res && res.error) || "Load failed.", "error");
      footerText.textContent = "Load failed.";
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
    grid.setFilters(st.filters);
    const to = from + rows.length - 1;
    footerText.textContent =
      (rows.length ? "rows " + fmtNum(from) + "–" + fmtNum(to) : st.page > FIRST_PAGE ? "no more rows" : "no rows") +
      (st.total !== null ? " of ~" + fmtNum(st.total) : "");
    setBusy(false);
  }

  function page(delta) {
    guardDirty(() => {
      const next = st.page + delta;
      if (next < FIRST_PAGE || (delta > 0 && !st.hasMore)) return;
      st.page = next;
      loadData();
    });
  }

  /** none → asc → desc → none, single column. */
  function cycleSort(column) {
    guardDirty(() => {
      if (!st.sort || st.sort.column !== column) st.sort = { column, dir: "asc" };
      else if (st.sort.dir === "asc") st.sort = { column, dir: "desc" };
      else st.sort = null;
      st.page = FIRST_PAGE;
      loadData();
    });
  }

  /** Run `fn` now, or after the user agrees to drop pending edits. */
  async function guardDirty(fn) {
    if (!grid || grid.dirtyCount() === 0) return fn();
    const n = grid.dirtyCount();
    const yes = await confirmBar(confirmSlot, {
      danger: true,
      title: "Discard " + n + " unsaved change" + (n === 1 ? "" : "s") + "?",
      confirmLabel: "Discard",
    });
    if (disposed || !yes) return;
    grid.clearChanges();
    fn();
  }

  // --- save -----------------------------------------------------------------
  async function save() {
    if (!grid || st.busy || st.readOnlyWhy) return;
    const c = grid.changes();
    const operations = [
      ...c.inserts.map((x) => ({ action: "insert", values: x.values })),
      ...c.updates.map((x) => ({ action: "update", values: x.values, where_pk: x.where })),
      ...c.deletes.map((x) => ({ action: "delete", where_pk: x.where })),
    ];
    if (!operations.length) return;
    grid.closeEditor(true);
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
    dispose() {
      disposed = true;
      onFilter.cancel();
      if (confirmSlot) dismissConfirm(confirmSlot);
      if (grid) grid.destroy();
      root.remove();
    },
  };
}
