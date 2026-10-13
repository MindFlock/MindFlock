/** The stuck-job chip's action (lib/backgroundJobs). */

import { instApi } from "../api/client";
import { refreshInstances } from "../state/queries";
import { displayName } from "../state/store";
import { errMsg } from "./format";
import { toast } from "./toast";

/** Stop one background job: `POST /api/instances/{title}/background/{pid}/stop`. */
export async function stopBackgroundJob(title: string, pid: number): Promise<boolean> {
  try {
    await instApi<{ stopped: boolean }>(title, "/background/" + pid + "/stop", { method: "POST" });
  } catch (e) {
    toast("Couldn't stop the background job in " + displayName(title) + " — " + errMsg(e));
    return false;
  }
  toast("Stopped the stuck background job in " + displayName(title));
  void refreshInstances();
  return true;
}
