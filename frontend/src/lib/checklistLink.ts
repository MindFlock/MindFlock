/** "Behaviors" on a session's row menu and its pane ⋯: that branch's
 * behaviors, on the session's route in the Sky (docs/behaviors.md §4). Verify
 * is a stop on the route now, not a place: this flies the Sky there
 * (`openBehaviors`), which opens the Behaviors card in its stop's place — or
 * the Behaviors board when no session frame shows them. */

import { openBehaviors } from "./behaviorsFocus";

export function openChecklist(branch: string | null | undefined): void {
  const b = String(branch || "").trim();
  if (!b) return;
  openBehaviors({ branch: b });
}
