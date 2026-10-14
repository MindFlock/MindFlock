/** workflow — the Workflow Optimizer extension's entry module (Addon API v3).
 *
 * The host imports this lazily and calls activate(api) once. The manifest
 * (backend/web/addons/workflow) declares the bar, the commands and the two
 * surfaces; this module registers their renderer:
 *
 *   workflow.open    → the dashboard dialog (surface "main", declarative)
 *   workflow.window  → the same dashboard as a grid window (surface "board")
 *
 * House idiom: default-export AND register on window.mindflockExtensions so
 * either loading path finds activate(). */

import { renderBoard } from "./view.js";

const extension = {
  activate(api) {
    api.ui.registerSurface("main", (host) => renderBoard(api, host, { compact: false }));
    api.ui.registerSurface("board", (host) => renderBoard(api, host, { compact: true }));
  },
};

window.mindflockExtensions = window.mindflockExtensions || {};
window.mindflockExtensions.workflow = extension;
export default extension;
