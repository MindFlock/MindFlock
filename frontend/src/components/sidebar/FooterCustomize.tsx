/** Footer "⚙ Customize": the door to the Customize dialog (which bars the
 * sidebar shows — customize/SidebarBarsPicker.tsx — and what a pane's header
 * holds, its Pane header page). Sits between the session
 * count and the shortcuts link. */

import { useUi } from "../../state/store";

export function FooterCustomize() {
  const openDialogFor = useUi((s) => s.openDialogFor);
  return (
    <div id="foot-customize">
      <button
        id="foot-customize-btn"
        type="button"
        className="foot-link"
        title="Choose which bars the sidebar shows (Prompts, Assistant, Tickets…) and what each pane's header holds"
        onClick={() => openDialogFor("customize")}
      >
        ⚙ Customize
      </button>
    </div>
  );
}
