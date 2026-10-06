/** Footer "Customize": the door to the Customize dialog (Sidebar · Prompts).
 * Sits between the session count and the shortcuts link. It used to
 * be a popover holding only the bar checklist; that list is now the dialog's
 * Sidebar tab (customize/SidebarBarsPicker.tsx), so this is a plain button. */

import { useUi } from "../../state/store";

export function FooterCustomize() {
  const openDialogFor = useUi((s) => s.openDialogFor);
  return (
    <div id="foot-customize">
      <button
        id="foot-customize-btn"
        type="button"
        className="foot-link"
        title="Sidebar bars and saved prompts"
        onClick={() => openDialogFor("customize")}
      >
        Customize
      </button>
    </div>
  );
}
