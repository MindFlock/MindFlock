/** dbclient — the Database Client extension's entry module (Addon API v3).
 *
 * The host imports this file lazily on first use and calls activate(api) once
 * (frontend/src/extensions/host.ts). Everything the extension adds is declared
 * in the backend manifest (backend/web/addons/dbclient); this module only
 * registers the three surface renderers and the command handlers:
 *
 *   dbclient.explorer        → the Explorer dialog (surface "main")
 *   dbclient.add-connection  → the same dialog opened on the new-connection form
 *   dbclient.sql             → reveal the newest open query pane, else open one
 *   dbclient.new-query       → always a fresh query pane
 *
 * The first two are also declarative in the manifest — the host can open the
 * dialog before this module has loaded — and the handlers do exactly what the
 * declaration does, so a click behaves the same either way.
 *
 * Registration follows the house idiom (static/addons/notify.js): the module
 * both default-exports its {activate} object and registers it on
 * window.mindflockExtensions.dbclient. */

import { tableLabel } from "./sql.js";
import { renderExplorer } from "./explorer.js";
import { renderQueryPad } from "./querypad.js";
import { renderTableView } from "./tableview.js";

let api = null;

/** Open query panes, ref → title, in open order (a Map keeps insertion order,
 * so the last key is the newest). Filled by openQuery, drained by each pane's
 * dispose — the host mints the refs, so this is the only place that knows
 * which query panes exist. */
const queryPanes = new Map();

/** "<ext>:<surface>:<ref>" → ref (the remainder after the second colon; refs
 * are opaque and may themselves contain colons). */
function refOf(key) {
  const a = key.indexOf(":");
  const b = a < 0 ? -1 : key.indexOf(":", a + 1);
  return b < 0 ? "" : key.slice(b + 1);
}

/** What the surface renderers get besides the host: the api plus the cross-
 * surface verbs (open a pane from the explorer, retitle/track query panes). */
const shared = {
  get api() {
    return api;
  },
  openExplorer(ref) {
    api.ui.openDialog("main", ref);
  },
  /** A fresh query pane. ctx: {connId?, database?, schema?, sql?}. */
  openQuery(ctx) {
    const title = "SQL";
    const key = api.ui.openPane("query", { title, ctx: ctx || {} });
    if (!key) return "";
    const ref = refOf(key);
    if (ref && !queryPanes.has(ref)) queryPanes.set(ref, title);
    return key;
  },
  /** dbclient.sql: reveal the newest open query pane (same surface + ref =
   * reveal, per the host contract), or open one when none is open. */
  focusOrOpenQuery() {
    const refs = [...queryPanes.keys()];
    if (!refs.length) return shared.openQuery({});
    const ref = refs[refs.length - 1];
    return api.ui.openPane("query", { ref, title: queryPanes.get(ref) || "SQL" });
  },
  /** The pane sets its own chrome title via host.setTitle; it tells us too so
   * a later reveal (which re-applies a title) keeps the connection name. */
  setQueryTitle(ref, title) {
    if (ref && queryPanes.has(ref)) queryPanes.set(ref, title);
  },
  releaseQueryRef(ref) {
    if (ref) queryPanes.delete(ref);
  },
  /** A table pane. ctx: {connId, database?, schema?, table, kind?}. */
  openTable(ctx) {
    const title = tableLabel(ctx.schema, ctx.table);
    return api.ui.openPane("table", { title, ctx });
  },
  /** The same table view, embedded in a caller-owned container (the
   * explorer's detail panel) instead of a pane. The host shim is chrome-less:
   * there is no pane title to set and no window to close — the caller
   * disposes the returned handle when it repaints. Wired here, not imported
   * by explorer.js, so the module graph stays acyclic. */
  embedTable(container, ctx) {
    return renderTableView(shared, {
      el: container,
      surfaceId: "table",
      ref: "",
      ctx: ctx || {},
      setTitle() {},
      close() {},
    });
  },
};

const extension = {
  activate(hostApi) {
    api = hostApi;
    api.ui.registerSurface("main", (host) => renderExplorer(shared, host));
    api.ui.registerSurface("query", (host) => renderQueryPad(shared, host));
    api.ui.registerSurface("table", (host) => renderTableView(shared, host));

    api.commands.register("dbclient.explorer", () => shared.openExplorer());
    api.commands.register("dbclient.add-connection", () => shared.openExplorer("new"));
    api.commands.register("dbclient.sql", () => shared.focusOrOpenQuery());
    api.commands.register("dbclient.new-query", () => shared.openQuery({}));
  },
};

window.mindflockExtensions = window.mindflockExtensions || {};
window.mindflockExtensions.dbclient = extension;

export default extension;
