/** "Checklist…" on a session's row menu and its pane ⋯: Verify's checklist
 * for that session's branch. The Verify dialog takes the branch as its
 * target (`openDialogFor("verify", branch)`) and opens on that branch's
 * checklist — or offers to write one. */

import { useUi } from "../state/store";

export function openChecklist(branch: string | null | undefined): void {
  const b = String(branch || "").trim();
  if (!b) return;
  useUi.getState().openDialogFor("verify", b);
}
