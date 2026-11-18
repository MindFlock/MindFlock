/** todo — the To-do extension's entry module (Addon API v3).
 *
 * The host imports this lazily and calls activate(api) once. The manifest
 * (backend/web/addons/todo) declares the bar, the commands and the two
 * surfaces; this module registers their renderers and the one code-backed
 * command:
 *
 *   todo.open     → the To-do window (surface "list", a pane; declarative)
 *   todo.add      → the quick-add dialog (surface "add"; declarative)
 *   todo.history  → the To-do window on its History tab
 *
 * The manifest stays at api_version 1: the host level-2 members
 * (api.ui.focusSession / focusedSession) are feature-detected.
 *
 * House idiom: default-export AND register on window.mindflockExtensions so
 * either loading path finds activate(). */

import { renderList, requestTab } from "./view.js";
import { renderAdd } from "./add.js";

const extension = {
  activate(api) {
    api.ui.registerSurface("list", (host) => renderList(api, host));
    api.ui.registerSurface("add", (host) => renderAdd(api, host));
    api.commands.register("todo.history", () => {
      // An open window only gets revealed (no re-render): tell it directly;
      // a new one reads the request (and the ctx) when it mounts.
      requestTab("history");
      api.ui.openPane("list", { ctx: { tab: "history" } });
    });
  },
};

window.mindflockExtensions = window.mindflockExtensions || {};
window.mindflockExtensions.todo = extension;
export default extension;
