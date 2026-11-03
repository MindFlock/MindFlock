/** The empty Grid, once setup is done: what the Grid is for (the Grid in one
 * line, docs/vocabulary.md), what a session is, and New session — instead of
 * a bare "No sessions yet". TerminalGrid mounts it inside its clickable
 * `#empty` (the whole area still opens New session); the keys shown are the
 * ones that work here (a browser tab keeps Ctrl+N for a new window). */

import { useUi } from "../../state/store";
import { usableShortcuts } from "../palette/ShortcutsSheet";
import { GRID_LINE, VIEWS_LINE, VISION_BREATH } from "../../lib/onboarding";
import "./Vision.css";

export function GridEmpty() {
  return (
    <div className="grid-empty-card">
      <h2>No sessions yet</h2>
      <p>{GRID_LINE}</p>
      <p>
        {VISION_BREATH[1]} {VIEWS_LINE}
      </p>
      <div className="empty-actions">
        <button
          type="button"
          className="btn-primary grid-empty-new"
          onClick={(e) => {
            e.stopPropagation();
            useUi.getState().openDialogFor("new-session");
          }}
        >
          New session
        </button>
      </div>
      <div className="empty-keys">{usableShortcuts("new").join(" / ")}</div>
    </div>
  );
}
