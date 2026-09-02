/** The one modal every extension's dialog surfaces share (store DialogName
 * "extension"; the target string "<ext>:<surface>[:<ref>]" rides in
 * dialogTarget). Standard modal anatomy — .modal backdrop, panel, .ws-head
 * with a close button, its own Escape listener, backdrop-click close — around
 * a body the extension renders into a host-owned keep-alive container. The
 * body is adopted (appendChild) while THIS target stays open and disposed on
 * close: dialogs are transient, unlike panes. */

import { useEffect, useRef, useSyncExternalStore } from "react";
import { useUi } from "../state/store";
import {
  extDialogView,
  hostVersion,
  mountExtDialog,
  releaseDialogTarget,
  subscribeHost,
} from "./host";

export function ExtensionDialog() {
  const open = useUi((s) => s.openDialog === "extension");
  const target = useUi((s) => s.dialogTarget);
  const closeDialog = useUi((s) => s.closeDialog);
  // Re-render on host changes: activation finishing, setTitle, errors.
  useSyncExternalStore(subscribeHost, hostVersion);
  const bodyRef = useRef<HTMLDivElement | null>(null);

  // Adopt the keep-alive body for as long as this target stays open. The
  // cleanup detaches AND releases (disposes) the runtime — the one surface
  // kind where unmount and dispose coincide, because closing a dialog is the
  // explicit close. Extensions needing sticky dialog state keep it in module
  // state (the explorer tree cache pattern), not in the DOM.
  useEffect(() => {
    if (!open || !target) return;
    const el = bodyRef.current;
    const detach = el ? mountExtDialog(target, el) : undefined;
    return () => {
      detach?.();
      releaseDialogTarget(target);
    };
  }, [open, target]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        closeDialog();
        e.preventDefault();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, closeDialog]);

  if (!open || !target) return null;

  const view = extDialogView(target);
  return (
    <div
      id="ext-dialog"
      className="modal"
      onClick={(e) => {
        if (e.target === e.currentTarget) closeDialog();
      }}
    >
      <div id="ext-dialog-panel">
        <div className="ws-head">
          <h2>{view.title || view.label}</h2>
          <button type="button" id="ext-dialog-close" aria-label="Close" onClick={closeDialog}>
            ✕
          </button>
        </div>
        <div id="ext-dialog-body">
          {view.status === "loading" && (
            <p className="ext-surface-status muted">Loading {view.label}…</p>
          )}
          {view.status === "error" && (
            <div className="ext-surface-status ext-surface-error">
              <p>
                {view.label} failed: {view.error || "no reason recorded"}
              </p>
              <p className="muted">See Settings → Extensions for details.</p>
            </div>
          )}
          {/* The adoption target keeps NO React children — the extension's DOM
              is appended imperatively, and React must never reconcile around
              nodes it does not own. */}
          <div className="ext-surface-mount" ref={bodyRef} />
        </div>
      </div>
    </div>
  );
}
