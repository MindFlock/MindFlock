/** The Explorer dialog (surface "main") and the dbclient data layer.
 *
 * Left: connection tree — connections → [databases → [schemas →]] then
 * "Tables (n)" / "Views (n)" groups whose tables unfold into their columns,
 * one lazy /tree call per scope level and one /table call per unfolded
 * table. Databases and schemas carry a size badge where the engine has cheap
 * statistics. Right: a context panel that follows the selection (connection
 * list + New connection, the connection form, and for a table the UNIFIED
 * table view embedded in place — SQL bar + grid + STRUCTURE/DDL — with an
 * "Open as window" action for the same view as a grid window).
 *
 * Dialog bodies are transient (the host disposes them on close), so the state
 * worth keeping — which nodes are expanded, every fetched level, the current
 * selection — lives in MODULE variables keyed by connection id. Reopening the
 * dialog repaints from those caches instantly and refreshes in the background.
 *
 * The data layer at the top (connections, drivers, tree levels, table info)
 * is shared with the two panes, which import it from here: the tree cache the
 * explorer fills is the same one the query pad's database/schema selectors
 * read, so nothing is fetched twice. */

import {
  el,
  svgIcon,
  button,
  option,
  confirmBar,
  dismissConfirm,
  makeNotice,
  errMsg,
  fmtNum,
  fmtBytes,
  copyText,
  qualifiedName,
  tableLabel,
} from "./ui.js";

export const BASE = "/api/dbclient";

export const ENGINES = [
  ["sqlite", "SQLite"],
  ["postgres", "PostgreSQL"],
  ["mysql", "MySQL"],
];

export function engineLabel(engine) {
  const hit = ENGINES.find(([id]) => id === engine);
  return hit ? hit[1] : String(engine || "");
}

// ---------------------------------------------------------------------------
// Data layer (module-level caches, shared with the panes)
// ---------------------------------------------------------------------------

let connCache = null; // last GET /connections result (masked profiles)
let connPromise = null;
let driversCache = null;
/** connId → {expanded: Set<nodeKey>, levels: Map<levelKey, {level, items}>} */
const treeCache = new Map();
/** connId\u0001db\u0001schema\u0001table → table_info */
const tableInfoCache = new Map();

export function connUrl(connId, suffix) {
  return BASE + "/connections/" + encodeURIComponent(connId) + (suffix || "");
}

/** Endpoints may return a bare list or {<key>: [...]}; accept both. */
function asList(res, key) {
  if (Array.isArray(res)) return res;
  if (res && Array.isArray(res[key])) return res[key];
  return [];
}

export function listConnections(api, force) {
  if (connCache && !force) return Promise.resolve(connCache);
  if (!connPromise) {
    connPromise = api
      .request(BASE + "/connections")
      .then((res) => {
        connCache = asList(res, "connections");
        return connCache;
      })
      .finally(() => {
        connPromise = null;
      });
  }
  return connPromise;
}

export function cachedConnections() {
  return connCache || [];
}

export function invalidateConnections() {
  connCache = null;
}

export async function connectionById(api, id) {
  let c = (await listConnections(api)).find((x) => x.id === id);
  if (!c) c = (await listConnections(api, true)).find((x) => x.id === id);
  return c || null;
}

export async function listDrivers(api, force) {
  if (driversCache && !force) return driversCache;
  driversCache = asList(await api.request(BASE + "/drivers"), "drivers");
  return driversCache;
}

export function cachedDrivers() {
  return driversCache || [];
}

/** Ask the server to install an engine's driver into its own environment.
 * Resolves to the server's report ({ok, error, output, install_hint, …}) —
 * a failed install is a report, not a throw, so the caller can show what the
 * installer said. The driver cache is refreshed either way: a partial success
 * (installed but not importable) must not leave a stale "available" behind. */
export async function installDriver(api, engine) {
  let res;
  try {
    res = await api.request(BASE + "/drivers/install", { json: { engine } });
  } finally {
    driversCache = null;
  }
  await listDrivers(api, true).catch(() => {});
  return res && typeof res === "object" ? res : { ok: false, error: "unexpected reply" };
}

export function levelKey(database, schema) {
  return (database || "") + "\u0001" + (schema || "");
}

export function treeFor(connId) {
  let t = treeCache.get(connId);
  if (!t) {
    t = { expanded: new Set(), levels: new Map() };
    treeCache.set(connId, t);
  }
  return t;
}

/** One lazy tree level: {level: "databases"|"schemas"|"tables", items}. */
export async function fetchTree(api, connId, scope = {}, force) {
  const t = treeFor(connId);
  const key = levelKey(scope.database, scope.schema);
  if (!force && t.levels.has(key)) return t.levels.get(key);
  const qs = new URLSearchParams();
  if (scope.database) qs.set("database", scope.database);
  if (scope.schema) qs.set("schema", scope.schema);
  const q = qs.toString();
  const res = await api.request(connUrl(connId, "/tree" + (q ? "?" + q : "")));
  const lvl = {
    level: (res && res.level) || "tables",
    items: Array.isArray(res && res.items) ? res.items : [],
  };
  t.levels.set(key, lvl);
  return lvl;
}

/** Forget the fetched level for `scope` and everything beneath it. */
export function dropTreeLevels(connId, scope = {}) {
  const t = treeCache.get(connId);
  if (!t) return;
  if (!scope.database && !scope.schema) {
    t.levels.clear();
    return;
  }
  if (scope.schema) {
    // A schema level has no descendant levels, and a bare-name prefix test
    // would also take out "auth_archive" when refreshing "auth".
    t.levels.delete(levelKey(scope.database, scope.schema));
    return;
  }
  const prefix = (scope.database || "") + "\u0001";
  for (const key of [...t.levels.keys()]) {
    if (key === prefix || key.startsWith(prefix)) t.levels.delete(key);
  }
}

/** Forget cached table_info under `scope` — a refresh must reach the column
 * leaves too, or the tree repaints stale columns from this cache. Every
 * prefix ends on a separator, so name-prefix siblings never match. */
export function dropTableInfo(connId, scope = {}) {
  const prefix = scope.schema
    ? [connId, scope.database || "", scope.schema, ""].join("\u0001")
    : scope.database
      ? [connId, scope.database, ""].join("\u0001")
      : connId + "\u0001";
  for (const key of [...tableInfoCache.keys()]) {
    if (key.startsWith(prefix)) tableInfoCache.delete(key);
  }
}

export function dropTreeCache(connId) {
  treeCache.delete(connId);
  dropTableInfo(connId);
}

export function itemName(item) {
  if (typeof item === "string") return item;
  return String(item && item.name != null ? item.name : "");
}

/** table_info for one table: {columns, indexes, ddl, kind}. */
export async function fetchTable(api, connId, scope, force) {
  const key = [connId, scope.database || "", scope.schema || "", scope.table].join("\u0001");
  if (!force && tableInfoCache.has(key)) return tableInfoCache.get(key);
  const qs = new URLSearchParams();
  if (scope.database) qs.set("database", scope.database);
  if (scope.schema) qs.set("schema", scope.schema);
  qs.set("table", scope.table);
  const res = await api.request(connUrl(connId, "/table?" + qs.toString()));
  const info = {
    columns: Array.isArray(res && res.columns) ? res.columns : [],
    indexes: Array.isArray(res && res.indexes) ? res.indexes : [],
    ddl: (res && res.ddl) || "",
    kind: (res && res.kind) || "table",
  };
  tableInfoCache.set(key, info);
  return info;
}

/** Primary-key column names from table_info (pk may be a flag or an ordinal). */
export function pkColumns(info) {
  return (info.columns || []).filter((c) => c.pk).map((c) => String(c.name));
}

// ---------------------------------------------------------------------------
// Scope picker (connection → database → schema [→ table]) — shared by the
// query pad toolbar and the table pane's fallback picker
// ---------------------------------------------------------------------------

/** opts: {connId, database, schema, table, withTable, onChange(scope),
 * onError(err)}. Selects appear only for the levels the connection's
 * hierarchy actually has (sqlite shows just the connection). */
export function createScopePicker(api, opts = {}) {
  const connSel = el("select", { class: "dbc-select dbc-scope-conn", title: "Connection" });
  const dbSel = el("select", { class: "dbc-select dbc-scope-db", title: "Database", hidden: true });
  const schemaSel = el("select", { class: "dbc-select dbc-scope-schema", title: "Schema", hidden: true });
  const tableSel = el("select", { class: "dbc-select dbc-scope-table", title: "Table", hidden: true });
  const root = el("span", { class: "dbc-scope" }, connSel, dbSel, schemaSel, opts.withTable ? tableSel : null);

  let gen = 0; // drops out-of-order responses when selects change quickly
  let disposed = false;
  let conn = null;
  const want = { database: opts.database, schema: opts.schema, table: opts.table };

  const fill = (sel, items, preferred) => {
    sel.replaceChildren();
    const names = items.map(itemName).filter(Boolean);
    for (const n of names) sel.appendChild(option(n, n));
    const pick = preferred.find((p) => p && names.includes(p));
    sel.value = pick || names[0] || "";
    sel.hidden = false;
    return sel.value;
  };

  const emit = () => {
    if (!disposed && opts.onChange) opts.onChange(scope());
  };
  const fail = (err) => {
    if (!disposed && opts.onError) opts.onError(err);
  };

  function scope() {
    return {
      connId: connSel.value || null,
      conn,
      engine: conn ? conn.engine : null,
      connName: conn ? conn.name || conn.id : "",
      database: dbSel.hidden ? null : dbSel.value || null,
      schema: schemaSel.hidden ? null : schemaSel.value || null,
      table: opts.withTable && !tableSel.hidden ? tableSel.value || null : null,
    };
  }

  async function onConn() {
    const my = ++gen;
    conn = cachedConnections().find((c) => c.id === connSel.value) || null;
    dbSel.hidden = schemaSel.hidden = tableSel.hidden = true;
    if (!conn) return emit();
    try {
      const lvl = await fetchTree(api, conn.id, {});
      if (my !== gen || disposed) return;
      if (lvl.level === "databases") {
        fill(dbSel, lvl.items, [want.database, conn.database]);
        want.database = undefined;
        return onDb(my);
      }
      if (lvl.level === "schemas") {
        fill(schemaSel, lvl.items, [want.schema, "public"]);
        want.schema = undefined;
        return onSchema(my);
      }
      if (opts.withTable) fill(tableSel, lvl.items, [want.table]);
      emit();
    } catch (err) {
      if (my === gen) fail(err);
    }
  }

  async function onDb(my) {
    if (my === undefined) my = ++gen;
    schemaSel.hidden = tableSel.hidden = true;
    try {
      const lvl = await fetchTree(api, conn.id, { database: dbSel.value });
      if (my !== gen || disposed) return;
      if (lvl.level === "schemas") {
        fill(schemaSel, lvl.items, [want.schema, "public"]);
        want.schema = undefined;
        return onSchema(my);
      }
      if (opts.withTable) fill(tableSel, lvl.items, [want.table]);
      emit();
    } catch (err) {
      if (my === gen) fail(err);
    }
  }

  async function onSchema(my) {
    if (my === undefined) my = ++gen;
    tableSel.hidden = true;
    if (!opts.withTable) return emit();
    try {
      const lvl = await fetchTree(api, conn.id, {
        database: dbSel.hidden ? undefined : dbSel.value,
        schema: schemaSel.value,
      });
      if (my !== gen || disposed) return;
      fill(tableSel, lvl.items, [want.table]);
      emit();
    } catch (err) {
      if (my === gen) fail(err);
    }
  }

  connSel.addEventListener("change", () => onConn());
  dbSel.addEventListener("change", () => onDb());
  schemaSel.addEventListener("change", () => onSchema());
  tableSel.addEventListener("change", emit);

  async function init() {
    try {
      const list = await listConnections(api);
      if (disposed) return;
      connSel.replaceChildren();
      for (const c of list) connSel.appendChild(option(c.id, c.name || c.id));
      if (opts.connId && list.some((c) => c.id === opts.connId)) connSel.value = opts.connId;
      if (!list.length) {
        connSel.appendChild(option("", "No connections"));
        connSel.disabled = true;
        return emit();
      }
      await onConn();
    } catch (err) {
      fail(err);
    }
  }
  init();

  return {
    el: root,
    scope,
    reload: init,
    dispose() {
      disposed = true;
      root.remove();
    },
  };
}

// ---------------------------------------------------------------------------
// Explorer dialog
// ---------------------------------------------------------------------------

/** Sticky across dialog opens: what is selected and the filter text. */
const explorerState = { selected: null, filter: "" };

/** Group nodes ("Tables (n)" / "Views (n)") are expanded by DEFAULT — the
 * grouping is orientation, not another click on the way to the data — so what
 * sticks is the exception: the groups someone folded away. Keyed by node key,
 * module-level like the tree cache. */
const collapsedGroups = new Set();

/** {isNew, draft, error, ok} while the connection form is showing. Reset on
 * every open so Escape means "forget it". */
let form = null;

function nodeKey(connId, database, schema, table) {
  return [connId, database || "", schema || "", table || ""].join("\u0001");
}

function defaultsFor(engine) {
  if (engine === "postgres") return { host: "localhost", port: "5432", user: "postgres", database: "postgres" };
  if (engine === "mysql") return { host: "localhost", port: "3306", user: "root", database: "" };
  return {};
}

function makeId(name, engine) {
  const slug = String(name || "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return (slug || engine || "db") + "-" + Date.now().toString(36).slice(-4);
}

function connSummary(c) {
  if (c.engine === "sqlite") return c.file || "(no file)";
  const hp = (c.host || "localhost") + (c.port ? ":" + c.port : "");
  return (c.user ? c.user + "@" : "") + hp + (c.database ? "/" + c.database : "");
}

export function renderExplorer(shared, host) {
  const api = shared.api;
  let disposed = false;
  const pending = new Set(); // connId|levelKey (or tbl|nodeKey) currently loading
  const errors = new Map(); // connId|levelKey (or tbl|nodeKey) → message
  const nodesByKey = new Map();
  /** The table view embedded in the detail panel ({dispose}), if one is up.
   * Every repaint of the panel replaces its DOM, so the instance must be
   * disposed alongside or its grid keeps listeners on detached nodes. */
  let embedded = null;
  /** True while renderTree is on the stack: its loop kicks ensureLevel /
   * ensureTableInfo for expanded-but-unfetched nodes, and those helpers
   * repaint immediately when called from a click — but a nested repaint from
   * inside the loop would append a second full copy of the tree. Declared
   * with the rest of the state, ABOVE the first renderTree() call (the
   * startForm/root TDZ lesson). */
  let treePainting = false;

  function disposeEmbed() {
    if (!embedded) return;
    try {
      embedded.dispose();
    } catch (e) {
      /* a broken embed must not take the panel down */
    }
    embedded = null;
  }

  form = null;

  // --- left: tree ---------------------------------------------------------
  const filterInput = el("input", {
    type: "search",
    class: "dbc-input dbc-filter-box",
    placeholder: "Filter tables…",
    value: explorerState.filter,
    "aria-label": "Filter tables",
    onInput: () => {
      explorerState.filter = filterInput.value.trim().toLowerCase();
      renderTree();
    },
  });
  const tree = el("div", { class: "dbc-tree", role: "tree" });
  const side = el(
    "div",
    { class: "dbc-explorer-side" },
    el(
      "div",
      { class: "dbc-side-tools" },
      filterInput,
      button("Reload", { kind: "icon", icon: "refresh", title: "Reload connections and open levels", onClick: refreshAll }),
      button("New connection", { kind: "icon", icon: "plus", title: "New connection", onClick: () => startForm(null) })
    ),
    tree
  );

  // --- right: context panel -----------------------------------------------
  const panel = el("div", { class: "dbc-panel" });
  const confirmSlot = el("div", { class: "dbc-confirm-slot" });
  const main = el("div", { class: "dbc-explorer-main" }, confirmSlot, panel);
  const root = el("div", { class: "dbc-explorer" }, side, main);
  host.el.appendChild(root);

  // ref "new" jumps straight to the connection form. AFTER root exists:
  // startForm repaints via root.isConnected, and calling it any earlier hits
  // the const's temporal dead zone (the broken-Add-connection bug).
  if (host.ref === "new") startForm(null);

  renderTree();
  renderMain();
  // Paint from cache first (above), then refresh the connection list. The
  // main panel repaints only when the list actually changed: an unconditional
  // renderMain here disposed and rebuilt the embedded table view (fetches,
  // scroll, pending edits) on every dialog open.
  const beforeRefresh = JSON.stringify(cachedConnections());
  listConnections(api, true)
    .then(() => {
      if (disposed) return;
      renderTree();
      if (JSON.stringify(cachedConnections()) !== beforeRefresh) renderMain();
    })
    .catch((err) => {
      if (!disposed) showTreeError(errMsg(err));
    });
  listDrivers(api).then(() => !disposed && form && renderMain()).catch(() => {});

  function showTreeError(msg) {
    tree.replaceChildren(el("div", { class: "dbc-node-status error", text: "Could not load connections: " + msg }));
  }

  // --- tree model -----------------------------------------------------------

  function buildNodes() {
    const out = [];
    for (const conn of cachedConnections()) {
      const t = treeFor(conn.id);
      const n = {
        kind: "conn",
        connId: conn.id,
        conn,
        engine: conn.engine,
        name: conn.name || conn.id,
        depth: 0,
        key: nodeKey(conn.id),
        lkey: levelKey(),
        expandable: true,
        parent: null,
      };
      out.push(n);
      walk(n, out, t);
    }
    return out;
  }

  function walk(node, out, t) {
    if (!explorerState.filter && !t.expanded.has(node.key)) return;
    const lvl = t.levels.get(node.lkey);
    if (!lvl) return;
    if (lvl.level === "databases" || lvl.level === "schemas") {
      for (const item of lvl.items) {
        const name = itemName(item);
        if (!name) continue;
        const sizeBytes = item && typeof item.size_bytes === "number" ? item.size_bytes : null;
        const n =
          lvl.level === "databases"
            ? {
                kind: "database",
                connId: node.connId,
                engine: node.engine,
                database: name,
                name,
                sizeBytes,
                depth: node.depth + 1,
                key: nodeKey(node.connId, name),
                lkey: levelKey(name),
                expandable: true,
                parent: node,
              }
            : {
                kind: "schema",
                connId: node.connId,
                engine: node.engine,
                database: node.database,
                schema: name,
                name,
                sizeBytes,
                depth: node.depth + 1,
                key: nodeKey(node.connId, node.database, name),
                lkey: levelKey(node.database, name),
                expandable: true,
                parent: node,
              };
        out.push(n);
        walk(n, out, t);
      }
      return;
    }
    // The tables level renders grouped, the way an IDE's tree does: a
    // "Tables (n)" group and — only when the schema has any — a "Views (n)"
    // group, each holding its members as children.
    const tables = [];
    const views = [];
    for (const item of lvl.items) {
      if (!itemName(item)) continue;
      const kind = item && item.kind ? String(item.kind) : "table";
      (kind === "table" ? tables : views).push(item);
    }
    pushGroup(node, out, t, "tables", "Tables", tables);
    if (views.length) pushGroup(node, out, t, "views", "Views", views);
  }

  function pushGroup(parent, out, t, groupId, label, items) {
    const g = {
      kind: "group",
      groupId,
      connId: parent.connId,
      engine: parent.engine,
      database: parent.database,
      schema: parent.schema,
      name: label,
      count: items.length,
      depth: parent.depth + 1,
      key: parent.key + "\u0002" + groupId,
      expandable: true,
      parent,
    };
    out.push(g);
    // While filtering, collapsed state is ignored — the keep-set prunes below.
    if (!explorerState.filter && collapsedGroups.has(g.key)) return;
    for (const item of items) {
      const n = tableNode(item, g);
      out.push(n);
      pushColumns(n, out, t);
    }
  }

  function tableNode(item, group) {
    const name = itemName(item);
    const kind = item && item.kind ? String(item.kind) : "table";
    return {
      kind: kind === "view" ? "view" : "table",
      tableKind: kind,
      approxRows: item && typeof item.approx_rows === "number" ? item.approx_rows : null,
      connId: group.connId,
      engine: group.engine,
      database: group.database,
      schema: group.schema,
      table: name,
      name,
      depth: group.depth + 1,
      // The 4-part key tables have always had, so the selection and the
      // expanded set survive the grouping.
      key: nodeKey(group.connId, group.database, group.schema, name),
      expandable: true,
      parent: group,
    };
  }

  /** An expanded table's columns as leaf nodes (name · type, key icon on the
   * primary key), from the same table_info cache the detail panel fills. */
  function pushColumns(tn, out, t) {
    if (!t.expanded.has(tn.key)) return;
    const info = tableInfoCache.get(tableCacheKey(tn));
    if (!info) return; // renderTree shows the loading/error row instead
    for (const c of info.columns) {
      out.push({
        kind: "column",
        name: String(c.name),
        colType: c.type || "",
        pk: !!c.pk,
        connId: tn.connId,
        engine: tn.engine,
        database: tn.database,
        schema: tn.schema,
        table: tn.table,
        depth: tn.depth + 1,
        key: tn.key + "\u0002col\u0002" + c.name,
        expandable: false,
        parent: tn,
      });
    }
  }

  function tableCacheKey(n) {
    return [n.connId, n.database || "", n.schema || "", n.table].join("\u0001");
  }

  function isExpanded(n) {
    if (n.kind === "group") return !collapsedGroups.has(n.key);
    return treeFor(n.connId).expanded.has(n.key);
  }

  function pendKey(n) {
    return n.connId + "|" + n.lkey;
  }

  function renderTree() {
    if (disposed || treePainting) return;
    treePainting = true;
    try {
      renderTreeNow();
    } finally {
      treePainting = false;
    }
  }

  function renderTreeNow() {
    tree.replaceChildren();
    nodesByKey.clear();
    const conns = cachedConnections();
    if (!conns.length) {
      tree.appendChild(
        el(
          "div",
          { class: "dbc-tree-empty" },
          el("p", { text: "No connections yet." }),
          button("New connection", { kind: "primary", icon: "plus", onClick: () => startForm(null) })
        )
      );
      return;
    }
    let nodes = buildNodes();
    for (const n of nodes) nodesByKey.set(n.key, n);
    const filter = explorerState.filter;
    if (filter) {
      // Match real objects only — never the "Tables"/"Views" group labels or
      // column names, which would light up half the tree for any short filter.
      const MATCHABLE = new Set(["table", "view", "database", "schema"]);
      const keep = new Set();
      for (const n of nodes) {
        if (MATCHABLE.has(n.kind) && n.name.toLowerCase().includes(filter)) {
          for (let p = n; p; p = p.parent) keep.add(p.key);
        }
      }
      nodes = nodes.filter(
        (n) =>
          keep.has(n.key) ||
          n.kind === "conn" ||
          // A kept, expanded table keeps its column children.
          (n.kind === "column" && n.parent && keep.has(n.parent.key))
      );
    }
    const frag = document.createDocumentFragment();
    for (const n of nodes) {
      frag.appendChild(renderNode(n));
      if (!n.expandable || !isExpanded(n)) continue;
      if (n.kind === "table" || n.kind === "view") {
        const pk = tablePendKey(n);
        if (pending.has(pk)) {
          frag.appendChild(statusRow(n, "Loading…"));
        } else if (errors.has(pk)) {
          frag.appendChild(statusRow(n, errors.get(pk), true, () => ensureTableInfo(n, true)));
        } else if (!tableInfoCache.has(tableCacheKey(n))) {
          // Expanded but nothing fetched yet (reopen after a cache drop) —
          // kick the load; the pending guard stops a second one.
          ensureTableInfo(n);
          frag.appendChild(statusRow(n, "Loading…"));
        }
        continue;
      }
      if (n.kind === "group") {
        if (!n.count && !filter) frag.appendChild(statusRow(n, "empty"));
        continue;
      }
      const pk = pendKey(n);
      if (pending.has(pk)) {
        frag.appendChild(statusRow(n, "Loading…"));
      } else if (errors.has(pk)) {
        frag.appendChild(statusRow(n, errors.get(pk), true, () => ensureLevel(n, true)));
      } else if (!treeFor(n.connId).levels.has(n.lkey)) {
        // Expanded but nothing fetched yet (reopen after a cache drop) —
        // kick the load; the pending guard above stops a second one.
        ensureLevel(n);
        frag.appendChild(statusRow(n, "Loading…"));
      } else if (!treeFor(n.connId).levels.get(n.lkey).items.length && !filter) {
        frag.appendChild(statusRow(n, "empty"));
      }
    }
    tree.appendChild(frag);
  }

  function statusRow(parent, text, isError, retry) {
    const row = el(
      "div",
      { class: "dbc-node-status" + (isError ? " error" : ""), style: { "--depth": parent.depth + 1 } },
      el("span", { text })
    );
    if (retry) row.appendChild(button("Retry", { onClick: retry }));
    return row;
  }

  function iconFor(n) {
    if (n.kind === "conn") return n.engine === "sqlite" ? "database" : "server";
    if (n.kind === "database") return "database";
    if (n.kind === "schema") return "schema";
    if (n.kind === "view") return "view";
    if (n.kind === "group") return n.groupId === "views" ? "view" : "table";
    if (n.kind === "column") return n.pk ? "key" : "columns";
    return "table";
  }

  function isSelected(n) {
    const s = explorerState.selected;
    return !!s && s.key === n.key;
  }

  function renderNode(n) {
    // While filtering, every fetched child renders regardless of collapsed
    // state — the chevron says so, or it would claim "collapsed" over a row
    // visibly showing its children.
    const expanded = isExpanded(n) || (!!explorerState.filter && n.expandable);
    const selectable = n.kind !== "group";
    const row = el("div", {
      class:
        "dbc-node kind-" + n.kind + (n.kind === "column" && n.pk ? " pk" : "") + (selectable && isSelected(n) ? " selected" : ""),
      role: "treeitem",
      "aria-expanded": n.expandable ? String(expanded) : undefined,
      "aria-selected": selectable ? String(isSelected(n)) : undefined,
      style: { "--depth": n.depth },
      dataset: { key: n.key },
      onClick: () => {
        // Groups toggle; a column stands for its table; a scope node both
        // selects and unfolds; a table only selects (its chevron unfolds the
        // columns — a click should not fire a fetch you didn't ask for).
        if (n.kind === "group") return toggle(n);
        if (n.kind === "column") return select(n.parent);
        select(n);
        if ((n.kind === "conn" || n.kind === "database" || n.kind === "schema") && !expanded) expand(n);
      },
    });
    if (n.expandable) {
      row.appendChild(
        el(
          "button",
          {
            type: "button",
            class: "dbc-chevron",
            title: expanded ? "Collapse" : "Expand",
            "aria-label": expanded ? "Collapse" : "Expand",
            onClick: (e) => {
              e.stopPropagation();
              toggle(n);
            },
          },
          svgIcon(expanded ? "chevron-down" : "chevron-right")
        )
      );
    } else {
      row.appendChild(el("span", { class: "dbc-chevron-spacer" }));
    }
    row.appendChild(svgIcon(iconFor(n), "dbc-node-icon"));
    row.appendChild(
      el("span", {
        class: "dbc-node-name",
        text: n.name,
        title: n.kind === "column" ? n.name + (n.colType ? " · " + n.colType : "") + (n.pk ? " · primary key" : "") : n.name,
      })
    );
    if (n.kind === "conn") {
      row.appendChild(el("span", { class: "dbc-node-badge", text: engineLabel(n.engine) }));
      if (n.conn.read_only) row.appendChild(svgIcon("lock", "dbc-node-lock"));
    } else if (n.kind === "group") {
      row.appendChild(el("span", { class: "dbc-node-badge count", text: fmtNum(n.count) }));
    } else if (n.kind === "view") {
      row.appendChild(el("span", { class: "dbc-node-badge", text: "view" }));
    } else if (n.kind === "table" && n.approxRows !== null) {
      row.appendChild(el("span", { class: "dbc-node-badge rows", text: "~" + fmtNum(n.approxRows), title: "approximate row count" }));
    } else if (n.kind === "column" && n.colType) {
      row.appendChild(el("span", { class: "dbc-node-badge coltype mono", text: n.colType }));
    }
    if ((n.kind === "database" || n.kind === "schema") && n.sizeBytes != null) {
      row.appendChild(
        el("span", { class: "dbc-node-badge size", text: fmtBytes(n.sizeBytes), title: "approximate size on disk" })
      );
    }
    if (n.kind === "conn" || n.kind === "database" || n.kind === "schema") {
      row.appendChild(
        el(
          "button",
          {
            type: "button",
            class: "dbc-node-act",
            title: "New query here",
            "aria-label": "New query on " + n.name,
            onClick: (e) => {
              e.stopPropagation();
              shared.openQuery({ connId: n.connId, database: n.database, schema: n.schema });
            },
          },
          svgIcon("code")
        )
      );
      row.appendChild(
        el(
          "button",
          {
            type: "button",
            class: "dbc-node-act",
            title: "Refresh",
            "aria-label": "Refresh " + n.name,
            onClick: (e) => {
              e.stopPropagation();
              refreshNode(n);
            },
          },
          svgIcon("refresh")
        )
      );
    }
    return row;
  }

  function scopeOf(n) {
    return { database: n.database, schema: n.schema };
  }

  function ensureLevel(n, force) {
    const t = treeFor(n.connId);
    const pk = pendKey(n);
    if (pending.has(pk)) return;
    if (!force && t.levels.has(n.lkey)) {
      renderTree();
      return;
    }
    pending.add(pk);
    errors.delete(pk);
    renderTree();
    fetchTree(api, n.connId, scopeOf(n), force)
      .catch((err) => {
        errors.set(pk, errMsg(err));
      })
      .finally(() => {
        pending.delete(pk);
        if (!disposed) renderTree();
      });
  }

  function tablePendKey(n) {
    return "tbl|" + n.key;
  }

  /** Load an expanded table's table_info for its column children (the same
   * cache renderTableDetail reads, so neither fetches twice). */
  function ensureTableInfo(n, force) {
    const pk = tablePendKey(n);
    if (pending.has(pk)) return;
    if (!force && tableInfoCache.has(tableCacheKey(n))) {
      renderTree();
      return;
    }
    pending.add(pk);
    errors.delete(pk);
    renderTree();
    fetchTable(api, n.connId, { database: n.database, schema: n.schema, table: n.table }, force)
      .catch((err) => {
        errors.set(pk, errMsg(err));
      })
      .finally(() => {
        pending.delete(pk);
        if (!disposed) renderTree();
      });
  }

  function expand(n) {
    if (n.kind === "group") {
      collapsedGroups.delete(n.key);
      return renderTree();
    }
    treeFor(n.connId).expanded.add(n.key);
    if (n.kind === "table" || n.kind === "view") ensureTableInfo(n);
    else ensureLevel(n);
  }

  function toggle(n) {
    if (n.kind === "group") {
      if (collapsedGroups.has(n.key)) collapsedGroups.delete(n.key);
      else collapsedGroups.add(n.key);
      return renderTree();
    }
    const t = treeFor(n.connId);
    if (t.expanded.has(n.key)) {
      t.expanded.delete(n.key);
      renderTree();
    } else expand(n);
  }

  function refreshNode(n) {
    dropTreeLevels(n.connId, scopeOf(n));
    // The column leaves under this scope come from tableInfoCache — a refresh
    // that left it warm would repaint stale columns under fresh tables.
    dropTableInfo(n.connId, scopeOf(n));
    treeFor(n.connId).expanded.add(n.key);
    ensureLevel(n, true);
  }

  function refreshAll() {
    invalidateConnections();
    for (const [id, t] of treeCache) {
      t.levels.clear();
      dropTableInfo(id);
    }
    errors.clear();
    listConnections(api, true)
      .then(() => {
        if (disposed) return;
        renderTree();
        renderMain();
      })
      .catch((err) => !disposed && showTreeError(errMsg(err)));
  }

  /** Repainting the panel disposes the embedded table view, edits and all —
   * so leaving a dirty embed needs a yes first. window.confirm (not the async
   * confirm bar): the callers are synchronous click handlers and the row they
   * clicked must not change selection when the answer is no. */
  function confirmDiscardEmbed() {
    const dirty = embedded && embedded.dirtyCount ? embedded.dirtyCount() : 0;
    if (!dirty) return true;
    const name = (explorerState.selected && explorerState.selected.name) || "this table";
    return window.confirm(
      "Discard " + dirty + " unsaved change" + (dirty === 1 ? "" : "s") + " in " + name + "?"
    );
  }

  function select(n) {
    if (explorerState.selected && explorerState.selected.key === n.key && !form) return;
    if (!confirmDiscardEmbed()) return;
    explorerState.selected = {
      key: n.key,
      kind: n.kind,
      connId: n.connId,
      engine: n.engine,
      database: n.database,
      schema: n.schema,
      table: n.table,
      name: n.name,
      tableKind: n.tableKind,
    };
    form = null;
    renderTree();
    renderMain();
  }

  function selectConn(conn) {
    if (!confirmDiscardEmbed()) return;
    explorerState.selected = { key: nodeKey(conn.id), kind: "conn", connId: conn.id, engine: conn.engine, name: conn.name || conn.id };
    form = null;
    renderTree();
    renderMain();
  }

  // --- right panel ------------------------------------------------------------

  function renderMain() {
    if (disposed) return;
    disposeEmbed();
    panel.classList.remove("has-embed");
    panel.replaceChildren();
    if (form) return renderForm();
    const s = explorerState.selected;
    const conn = s && cachedConnections().find((c) => c.id === s.connId);
    if (!s || !conn) return renderHome();
    if (s.kind === "conn") return renderConnDetail(conn);
    if (s.kind === "table" || s.kind === "view") return renderTableDetail(conn, s);
    return renderScopeDetail(conn, s);
  }

  function panelHead(title, icon, actions) {
    return el(
      "div",
      { class: "dbc-panel-head" },
      icon ? svgIcon(icon, "dbc-panel-icon") : null,
      el("h3", { class: "dbc-panel-title", text: title }),
      el("span", { class: "dbc-spacer" }),
      actions || null
    );
  }

  function renderHome() {
    const conns = cachedConnections();
    panel.appendChild(
      panelHead("Connections", "database", button("New connection", { kind: "primary", icon: "plus", onClick: () => startForm(null) }))
    );
    if (!conns.length) {
      panel.appendChild(
        el("p", { class: "dbc-hint", text: "Add a connection to browse its tables, edit rows and run SQL. Profiles are stored in ~/.mindflock/dbclient.json." })
      );
      return;
    }
    const list = el("div", { class: "dbc-card-list" });
    for (const c of conns) list.appendChild(connCard(c, false));
    panel.appendChild(list);
  }

  function connCard(c, detailed) {
    const head = el(
      "div",
      { class: "dbc-card-head" },
      svgIcon(c.engine === "sqlite" ? "database" : "server"),
      el("span", { class: "dbc-card-title", text: c.name || c.id }),
      el("span", { class: "dbc-badge", text: engineLabel(c.engine) }),
      c.read_only ? el("span", { class: "dbc-badge ro", title: "Read-only connection" }, svgIcon("lock"), "read only") : null
    );
    const actions = el(
      "div",
      { class: "dbc-card-actions" },
      detailed
        ? null
        : button("Browse", {
            icon: "chevron-right",
            onClick: () => {
              selectConn(c);
              expand(nodesByKey.get(nodeKey(c.id)) || { connId: c.id, key: nodeKey(c.id), lkey: levelKey(), expandable: true });
            },
          }),
      button("New query", { icon: "code", onClick: () => shared.openQuery({ connId: c.id, database: c.database || undefined }) }),
      button("Edit", { icon: "edit", onClick: () => startForm(c) }),
      detailed ? button("Test", { icon: "check", onClick: () => testConn(c.id) }) : null,
      button("Delete", { kind: "icon", icon: "trash", title: "Delete connection", class: "danger-hover", onClick: () => deleteConn(c) })
    );
    const card = el(
      "div",
      { class: "dbc-card" + (detailed ? " detailed" : "") },
      head,
      el("div", { class: "dbc-card-sub mono", text: connSummary(c) }),
      actions
    );
    if (detailed) {
      card.appendChild(el("div", { class: "dbc-card-note" }, "id: ", el("code", { text: c.id })));
    }
    return card;
  }

  function renderConnDetail(conn) {
    panel.appendChild(panelHead(conn.name || conn.id, conn.engine === "sqlite" ? "database" : "server"));
    panel.appendChild(connCard(conn, true));
    panel.appendChild(el("div", { class: "dbc-notice-slot" }, connNotice.el));
  }
  const connNotice = makeNotice();

  function renderScopeDetail(conn, s) {
    const label = s.kind === "schema" ? "Schema" : "Database";
    panel.appendChild(
      panelHead(
        s.name,
        s.kind === "schema" ? "schema" : "database",
        button("New query", {
          kind: "primary",
          icon: "code",
          onClick: () => shared.openQuery({ connId: conn.id, database: s.database, schema: s.schema }),
        })
      )
    );
    panel.appendChild(el("p", { class: "dbc-hint", text: label + " on " + (conn.name || conn.id) + ". Expand it in the tree to see its tables." }));
  }

  /** A selected table IS the unified table view, embedded where the column
   * summary used to be: the SQL bar + grid (with STRUCTURE and DDL one tab
   * over), exactly what "Open as window" opens as a grid window. The embed
   * host shim is chrome-less — no pane title to set, and "close" means
   * selecting something else, which renderMain's disposeEmbed handles. */
  function renderTableDetail(conn, s) {
    const qualified = tableLabel(s.schema, s.table, conn.engine);
    const ctx = { connId: conn.id, database: s.database, schema: s.schema, table: s.table, kind: s.tableKind };
    panel.appendChild(
      panelHead(
        qualified,
        s.kind === "view" ? "view" : "table",
        el(
          "span",
          { class: "dbc-actions" },
          button("Open as window", {
            kind: "primary",
            icon: "open",
            title: "Open this table as a grid window, beside your sessions",
            onClick: () => shared.openTable(ctx),
          }),
          button("New query", {
            icon: "code",
            onClick: () =>
              shared.openQuery({
                connId: conn.id,
                database: s.database,
                schema: s.schema,
                sql: "SELECT * FROM " + qualifiedName(conn.engine, s.schema, s.table) + " LIMIT 100",
              }),
          })
        )
      )
    );
    const body = el("div", { class: "dbc-embed" });
    panel.appendChild(body);
    panel.classList.add("has-embed");
    embedded = shared.embedTable(body, ctx);
  }

  // --- connection form ------------------------------------------------------

  function startForm(existing) {
    if (!confirmDiscardEmbed()) return;
    if (existing) {
      form = {
        isNew: false,
        draft: {
          id: existing.id,
          name: existing.name || "",
          engine: existing.engine || "sqlite",
          host: existing.host || "",
          port: existing.port == null ? "" : String(existing.port),
          user: existing.user || "",
          password: existing.password || "",
          database: existing.database || "",
          file: existing.file || "",
          read_only: !!existing.read_only,
        },
        error: "",
        ok: "",
        install: null,
      };
    } else {
      form = {
        isNew: true,
        draft: { id: "", name: "", engine: "sqlite", host: "", port: "", user: "", password: "", database: "", file: "", read_only: false },
        error: "",
        ok: "",
        install: null,
      };
    }
    if (root.isConnected) renderMain();
  }

  function field(label, input) {
    const id = "dbc-f-" + label.toLowerCase().replace(/[^a-z0-9]+/g, "-") + "-" + Math.random().toString(36).slice(2, 6);
    input.id = id;
    return el("label", { class: "dbc-field", for: id }, el("span", { class: "dbc-field-label", text: label }), input);
  }

  /** The "driver missing" note above the form's fields. When the server can
   * install the driver into its own environment (can_install) the note leads
   * with a button; the paste-into-a-shell command stays as the fallback, and
   * is the ONLY thing shown when the server refuses (a system-managed Python,
   * or no uv/pip) — a button that cannot work would be worse than a command. */
  function driverNote(drv) {
    const st = installState();
    const body = el("div", { class: "dbc-driver-body" });
    body.appendChild(
      el("div", {
        text: st.running
          ? "Installing " + (drv.driver || "the driver") + " into the server's environment…"
          : "The " +
            engineLabel(drv.engine) +
            " driver" +
            (drv.driver ? " (" + drv.driver + ")" : "") +
            " is not installed in the server's environment.",
      })
    );
    if (drv.can_install) {
      body.appendChild(
        el(
          "div",
          { class: "dbc-driver-actions" },
          button(st.running ? "Installing…" : "Install driver", {
            kind: "primary",
            icon: "download",
            disabled: !!st.running,
            title: "Install " + (drv.driver || "the driver") + " into the environment that runs MindFlock",
            onClick: () => runInstall(drv),
          }),
          st.running ? null : el("span", { class: "dbc-hint inline", text: "— no shell needed" })
        )
      );
    } else if (drv.install_blocked) {
      body.appendChild(el("div", { class: "dbc-hint", text: "Cannot install from here: " + drv.install_blocked + "." }));
    }
    if (st.error) body.appendChild(el("div", { class: "dbc-driver-error", text: st.error }));
    if (st.output) body.appendChild(el("pre", { class: "dbc-driver-out", text: st.output }));
    if (drv.install_hint && !st.running) {
      body.appendChild(
        el(
          "div",
          { class: "dbc-driver-hint" },
          el("code", { class: "mono", text: drv.install_hint }),
          button("Copy", { kind: "icon", icon: "copy", title: "Copy install command", onClick: () => copyText(drv.install_hint) })
        )
      );
    }
    return el("div", { class: "dbc-driver-note" }, svgIcon("alert"), body);
  }

  function installState() {
    if (!form.install) form.install = { running: false, error: "", output: "" };
    return form.install;
  }

  /** Run the install, then repaint: on success the refreshed driver cache makes
   * the note disappear by itself; on failure the installer's own output is what
   * the note shows, because "pip said no" is the only useful next step. */
  async function runInstall(drv) {
    if (!form) return;
    const st = installState();
    if (st.running) return;
    st.running = true;
    st.error = st.output = "";
    form.error = form.ok = "";
    renderMain();
    let res;
    try {
      res = await installDriver(api, drv.engine);
    } catch (err) {
      res = { ok: false, error: errMsg(err) };
    }
    if (disposed || !form) return;
    // Re-read: the form object may have been replaced while we were awaiting.
    const now = installState();
    now.running = false;
    if (res.ok) {
      api.ui.toast((res.driver || "Driver") + (res.already ? " already installed" : " installed"));
      form.ok = "Driver installed — Test now checks the connection for real.";
    } else {
      now.error = res.error || "the install failed";
      now.output = res.output || "";
    }
    renderMain();
  }

  function renderForm() {
    const d = form.draft;
    const bind = (key, attrs) =>
      el("input", {
        class: "dbc-input",
        type: "text",
        value: d[key],
        ...attrs,
        onInput: (e) => {
          d[key] = e.target.value;
        },
      });

    panel.appendChild(panelHead(form.isNew ? "New connection" : "Edit connection", "edit"));

    const strip = el("div", { class: "dbc-seg-strip", role: "radiogroup", "aria-label": "Engine" });
    for (const [id, label] of ENGINES) {
      strip.appendChild(
        el("button", {
          type: "button",
          class: "dbc-seg" + (d.engine === id ? " active" : ""),
          role: "radio",
          "aria-checked": String(d.engine === id),
          text: label,
          onClick: () => {
            if (d.engine === id) return;
            d.engine = id;
            // Fill the engine's usual defaults into fields the user left blank.
            for (const [k, v] of Object.entries(defaultsFor(id))) if (!d[k]) d[k] = v;
            renderMain();
          },
        })
      );
    }
    panel.appendChild(strip);

    const drv = cachedDrivers().find((x) => x.engine === d.engine);
    if (drv && drv.available === false) panel.appendChild(driverNote(drv));

    const fields = el("div", { class: "dbc-fields" });
    fields.appendChild(field("Name", bind("name", { placeholder: "My database", autofocus: true })));
    if (d.engine === "sqlite") {
      fields.appendChild(field("File path", bind("file", { placeholder: "/path/to/database.sqlite", spellcheck: false })));
    } else {
      fields.appendChild(
        el(
          "div",
          { class: "dbc-field-row" },
          field("Host", bind("host", { placeholder: "localhost", spellcheck: false })),
          field("Port", bind("port", { inputMode: "numeric", placeholder: d.engine === "mysql" ? "3306" : "5432", class: "dbc-input narrow" }))
        )
      );
      fields.appendChild(field("User", bind("user", { spellcheck: false, autocomplete: "off" })));
      fields.appendChild(
        field(
          "Password",
          bind("password", {
            type: "password",
            autocomplete: "new-password",
            placeholder: form.isNew ? "" : "(unchanged)",
          })
        )
      );
      fields.appendChild(field("Database", bind("database", { spellcheck: false })));
    }
    const ro = el("input", {
      type: "checkbox",
      checked: d.read_only,
      onChange: (e) => {
        d.read_only = e.target.checked;
      },
    });
    fields.appendChild(
      el("label", { class: "dbc-check" }, ro, el("span", { text: "Read-only connection" }), el("span", { class: "dbc-hint inline", text: "— enforced at connect time by the engine" }))
    );
    panel.appendChild(fields);

    const notice = makeNotice();
    if (form.error) notice.set(form.error, "error");
    else if (form.ok) notice.set(form.ok, "ok");
    panel.appendChild(notice.el);

    panel.appendChild(
      el(
        "div",
        { class: "dbc-form-actions" },
        button("Cancel", {
          onClick: () => {
            form = null;
            renderMain();
          },
        }),
        el("span", { class: "dbc-spacer" }),
        !form.isNew
          ? button("Delete", {
              kind: "danger",
              icon: "trash",
              onClick: () => {
                const c = cachedConnections().find((x) => x.id === d.id);
                if (c) deleteConn(c);
              },
            })
          : null,
        button(form.isNew ? "Save & test" : "Test", {
          icon: "check",
          title: form.isNew ? "Save the profile, then open a connection to check it" : "Open a connection to check the saved profile",
          onClick: () => saveForm(true),
        }),
        button("Save", { kind: "primary", icon: "save", onClick: () => saveForm(false) })
      )
    );
  }

  async function saveForm(thenTest) {
    if (!form) return;
    const d = form.draft;
    form.error = form.ok = "";
    if (!d.name.trim()) {
      form.error = "Give the connection a name.";
      return renderMain();
    }
    if (d.engine === "sqlite" && !d.file.trim()) {
      form.error = "SQLite needs a file path.";
      return renderMain();
    }
    if (!d.id) d.id = makeId(d.name, d.engine);
    const port = d.port === "" ? null : Number(d.port);
    if (port !== null && !(Number.isInteger(port) && port > 0 && port < 65536)) {
      form.error = "Port must be a whole number between 1 and 65535.";
      return renderMain();
    }
    const payload = {
      id: d.id,
      name: d.name.trim(),
      engine: d.engine,
      host: d.engine === "sqlite" ? "" : d.host.trim(),
      port: d.engine === "sqlite" ? null : port,
      user: d.engine === "sqlite" ? "" : d.user,
      password: d.engine === "sqlite" ? "" : d.password,
      database: d.engine === "sqlite" ? "" : d.database.trim(),
      file: d.engine === "sqlite" ? d.file.trim() : "",
      read_only: !!d.read_only,
    };
    try {
      await api.request(BASE + "/connections", { json: payload });
    } catch (err) {
      form.error = errMsg(err);
      return renderMain();
    }
    invalidateConnections();
    // Cached levels belong to the old profile — the server drops its pools
    // on every write, and we drop ours.
    dropTreeCache(d.id);
    const wasNew = form.isNew;
    form.isNew = false;
    try {
      await listConnections(api, true);
    } catch (err) {
      form.error = errMsg(err);
      return renderMain();
    }
    if (disposed) return;
    if (thenTest) {
      form.ok = wasNew ? "Saved. Testing…" : "Testing…";
      renderMain();
      const r = await runTest(d.id);
      if (disposed || !form) return;
      if (r.ok) form.ok = "Connected" + (r.server ? " · " + r.server : "") + (wasNew ? " · saved" : "");
      else form.error = r.error || "connection failed";
      renderTree();
      return renderMain();
    }
    const conn = cachedConnections().find((c) => c.id === d.id);
    api.ui.toast("Saved connection " + payload.name);
    if (conn) selectConn(conn);
    else {
      form = null;
      renderTree();
      renderMain();
    }
  }

  async function runTest(id) {
    try {
      const res = await api.request(connUrl(id, "/test"), { method: "POST" });
      return res && typeof res === "object" ? res : { ok: false, error: "unexpected reply" };
    } catch (err) {
      return { ok: false, error: errMsg(err) };
    }
  }

  async function testConn(id) {
    connNotice.set("Testing…", "info");
    const r = await runTest(id);
    if (disposed) return;
    if (r.ok) connNotice.set("Connected" + (r.server ? " · " + r.server : ""), "ok");
    else connNotice.set(r.error || "connection failed", "error");
  }

  async function deleteConn(c) {
    const yes = await confirmBar(confirmSlot, {
      danger: true,
      title: 'Delete connection "' + (c.name || c.id) + '"?',
      body: "Only the saved profile is removed; the database itself is untouched.",
      confirmLabel: "Delete",
    });
    if (!yes || disposed) return;
    try {
      await api.request(connUrl(c.id), { method: "DELETE" });
    } catch (err) {
      api.ui.toast("Delete failed: " + errMsg(err));
      return;
    }
    invalidateConnections();
    dropTreeCache(c.id);
    if (explorerState.selected && explorerState.selected.connId === c.id) explorerState.selected = null;
    form = null;
    try {
      await listConnections(api, true);
    } catch (e) {
      /* the list repaints empty; the reload button recovers */
    }
    if (disposed) return;
    renderTree();
    renderMain();
  }

  return {
    dispose() {
      disposed = true;
      disposeEmbed();
      dismissConfirm(confirmSlot);
      root.remove();
    },
  };
}
