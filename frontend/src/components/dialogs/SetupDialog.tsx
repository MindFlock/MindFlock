/** The Setup modal: the first-run checklist (components/setup/SetupChecklist —
 * the SAME component the empty grid's card renders) behind a Close button.
 *
 * It opens from the "Setup 3/5" chip, the sidebar's doctor chip, the command
 * palette ("Open Setup checklist") and the desktop app's `?setup=1`. It never
 * opens by itself any more: the old rule (a failing doctor AND sessions > 0)
 * was a third first-run surface on top of the welcome tour and the grid card.
 *
 * The doctor model (DoctorList, useDoctorWarn, useDoctorAutoShow) moved to
 * components/setup and is re-exported here for its existing importers
 * (Settings → Doctor, the sidebar). GitHub has no test of its own here: the
 * checklist's Connect GitHub button opens that dialog, whose Test is the one. */

import { useUi } from "../../state/store";
import { Modal } from "../Modal";
import { SetupChecklist } from "../setup/SetupChecklist";

export {
  DoctorList,
  useDoctorAutoShow,
  useDoctorWarn,
  type DoctorCheckItem,
  type DoctorPayload,
} from "../setup/DoctorList";
export { consumeSetupIntent } from "../setup/setupModel";
export { SetupChecklist } from "../setup/SetupChecklist";

export function SetupDialog() {
  const open = useUi((s) => s.openDialog === "setup");
  const closeDialog = useUi((s) => s.closeDialog);

  if (!open) return null;

  return (
    <Modal id="setup-dialog" onClose={closeDialog}>
      <div id="setup-panel-dlg" role="dialog" aria-modal="true" aria-labelledby="setup-title">
        <div className="ws-head">
          {/* The progress line is the checklist's own first line. */}
          <h2 id="setup-title">Setup</h2>
          <button type="button" id="setup-close" onClick={closeDialog}>
            Close
          </button>
        </div>
        <div id="setup-dialog-body">
          <SetupChecklist />
        </div>
      </div>
    </Modal>
  );
}
