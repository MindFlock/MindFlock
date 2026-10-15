/** The background-job chip's actions (lib/backgroundJobs): stop a stuck job,
 * and say how long a job should take (lib/jobExpectation). */

import { instApi } from "../api/client";
import { refreshInstances } from "../state/queries";
import { displayName } from "../state/store";
import { errMsg } from "./format";
import { formatSpan } from "./jobExpectation";
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

/** Say how long a job should take (`secs`), or clear that (null):
 * `POST /api/instances/{title}/jobs/{job_id}/expect {duration}` /
 * `DELETE …/expect`. The job is not called stuck while inside that window. */
export async function expectBackgroundJob(title: string, jobId: string, secs: number | null): Promise<boolean> {
  const path = "/jobs/" + encodeURIComponent(jobId) + "/expect";
  try {
    if (secs == null) await instApi<{ ok: boolean }>(title, path, { method: "DELETE" });
    else await instApi<{ ok: boolean; expect_s: number; eta: number }>(title, path, { method: "POST", json: { duration: secs } });
  } catch (e) {
    toast("Couldn't change how long the job in " + displayName(title) + " should take — " + errMsg(e));
    return false;
  }
  toast(
    secs == null
      ? "Cleared the job's expected time in " + displayName(title)
      : "The job in " + displayName(title) + " is expected to take ~" + formatSpan(secs)
  );
  void refreshInstances();
  return true;
}
