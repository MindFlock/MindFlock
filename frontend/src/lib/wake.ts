/** Wake a sleeping agent: `POST /api/instances/{title}/wake` (backend
 * core/sleep.py) — the Sleeping chip's Wake action. It relaunches the agent,
 * resuming its conversation. Opening the session's terminal or sending it
 * anything wakes it too, so this is only the explicit button. */

import { instApi } from "../api/client";
import { refreshInstances } from "../state/queries";
import { errMsg } from "./format";
import { toast } from "./toast";
import { displayName } from "../state/store";
import { forceActivity } from "./sessionState";

export async function wakeSession(title: string): Promise<boolean> {
  try {
    await instApi<{ title: string; woke: boolean }>(title, "/wake", { method: "POST" });
  } catch (e) {
    toast("Couldn't wake " + displayName(title) + " — " + errMsg(e));
    return false;
  }
  // Back at its prompt until the next poll says otherwise.
  forceActivity(title, "idle");
  toast("Waking " + displayName(title) + "…");
  void refreshInstances();
  return true;
}
