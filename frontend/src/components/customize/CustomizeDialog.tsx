/** Customize — which bars the sidebar shows (Usage, Tickets, …, Assistant,
 * Prompts), in what order. Opened from the sidebar footer's ⚙ Customize and
 * the palette. Saved prompts are a bar like the Assistant: switch it on here,
 * use it in the sidebar, manage the list from its "Manage".
 *
 * Esc closes the dialog unless something inside claimed it first
 * (`defaultPrevented`). The listener sits on window, which a keydown reaches
 * after every document listener, so a child's claim is always made before
 * this reads it. */

import { useEffect } from "react";
import { useUi } from "../../state/store";
import { SidebarBarsPicker } from "./SidebarBarsPicker";

export function CustomizeDialog() {
  const open = useUi((s) => s.openDialog === "customize");
  const closeDialog = useUi((s) => s.closeDialog);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || e.defaultPrevented) return;
      e.preventDefault();
      closeDialog();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, closeDialog]);

  if (!open) return null;

  return (
    <div
      id="customize-dialog"
      className="modal"
      role="dialog"
      aria-modal="true"
      aria-labelledby="customize-title"
      onClick={(e) => {
        if (e.target === e.currentTarget) closeDialog();
      }}
    >
      <div id="customize-panel">
        <div className="ws-head">
          <h2 id="customize-title">Customize</h2>
          <span className="ik-subtitle">Choose what the sidebar shows</span>
          <button type="button" id="customize-close" onClick={closeDialog}>
            Close
          </button>
        </div>
        <div id="customize-body" data-tab="sidebar">
          <SidebarBarsPicker />
        </div>
      </div>
    </div>
  );
}
