/** Customize — which bars the sidebar shows (Usage, Tickets, …, Assistant,
 * Prompts), in what order. Opened from the sidebar footer's ⚙ Customize and
 * the palette. Saved prompts are a bar like the Assistant: switch it on here,
 * use it in the sidebar, manage the list from its "Manage".
 *
 * Esc closes the dialog unless something inside claimed it first
 * (`defaultPrevented`) — the Modal shell's listener sits on window, which a
 * keydown reaches after every document listener, so a child's claim is always
 * made before it reads it. */

import { useUi } from "../../state/store";
import { Modal } from "../Modal";
import { SidebarBarsPicker } from "./SidebarBarsPicker";

export function CustomizeDialog() {
  const open = useUi((s) => s.openDialog === "customize");
  const closeDialog = useUi((s) => s.closeDialog);

  if (!open) return null;

  return (
    <Modal
      id="customize-dialog"
      role="dialog"
      aria-modal="true"
      aria-labelledby="customize-title"
      onClose={closeDialog}
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
    </Modal>
  );
}
