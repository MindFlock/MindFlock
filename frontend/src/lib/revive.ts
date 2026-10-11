/** Restart an offline agent: `POST /api/instances/{title}/revive`, the same
 * lazy revive the server does when a terminal attaches or a prompt is sent
 * (it resumes the conversation after a crash, starts fresh after a clean
 * quit). The Offline chip's Restart action (lib/sessionState). */

import { instApi } from "../api/client";
import { refreshInstances } from "../state/queries";
import { errMsg } from "./format";
import { toast } from "./toast";
import { displayName } from "../state/store";
import { forceActivity } from "./sessionState";

export async function reviveSession(title: string): Promise<boolean> {
  try {
    await instApi<{ title: string; revived: boolean }>(title, "/revive", { method: "POST" });
  } catch (e) {
    toast("Couldn't restart " + displayName(title) + " — " + errMsg(e));
    return false;
  }
  // The next activity poll says what it is doing; until then it is no longer
  // offline, so the row stops offering Restart twice.
  forceActivity(title, "idle");
  toast("Restarting " + displayName(title) + "…");
  void refreshInstances();
  return true;
}
