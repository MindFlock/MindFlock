/** optimizer — the Usage Optimizer extension's entry module (Addon API v3).
 *
 * The host imports this lazily and calls activate(api) once. The manifest
 * (backend/web/addons/optimizer) declares the bar, the commands and the two
 * surfaces; this module registers the renderers and the one code-backed
 * command:
 *
 *   optimizer.open    → the dashboard dialog (surface "main", declarative)
 *   optimizer.window  → the same dashboard as a grid window (surface "board")
 *   optimizer.sample  → POST /api/optimizer/sample (record a sample right now)
 *
 * House idiom: default-export AND register on window.mindflockExtensions so
 * either loading path finds activate(). */

import { renderBoard } from "./view.js";

let api = null;

const extension = {
  activate(a) {
    api = a;
    api.ui.registerSurface("main", (host) => renderBoard(api, host, { compact: false }));
    api.ui.registerSurface("board", (host) => renderBoard(api, host, { compact: true }));
    api.commands.register("optimizer.sample", async () => {
      try {
        const r = await api.request("/api/optimizer/sample", { json: {} });
        api.ui.toast(
          r && r.ok
            ? "Usage sample recorded (" + (r.written || 0) + " provider" + (r.written === 1 ? "" : "s") + ")"
            : "Could not record a sample"
        );
      } catch (err) {
        api.log.error("sample failed", err);
        api.ui.toast("Could not record a sample");
      }
    });
  },
};

window.mindflockExtensions = window.mindflockExtensions || {};
window.mindflockExtensions.optimizer = extension;
export default extension;
