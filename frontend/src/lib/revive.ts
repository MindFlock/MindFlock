/** Restart an offline agent: `POST /api/instances/{title}/revive`, the same
 * lazy revive the server does when a terminal attaches or a prompt is sent
 * (it resumes the conversation after a crash, starts fresh after a clean
 * quit; an agent that died mid-turn is also queued a "continue"). The
 * Offline chip's Restart action (lib/sessionState). */

import { instApi } from "../api/client";
import { refreshInstances } from "../state/queries";
import { errMsg } from "./format";
import { toast } from "./toast";
import { displayName } from "../state/store";
import { forceActivity } from "./sessionState";

export async function reviveSession(title: string): Promise<boolean> {
  let r: { title: string; revived: boolean; continued?: boolean; memory_warning?: string };
  try {
    r = await instApi(title, "/revive", { method: "POST" });
  } catch (e) {
    toast("Couldn't restart " + displayName(title) + " — " + errMsg(e));
    return false;
  }
  // The next activity poll says what it is doing; until then it is no longer
  // offline, so the row stops offering Restart twice.
  forceActivity(title, "idle");
  // It died mid-turn: a "continue where you left off" waits at the front of
  // its prompt queue, sent once the agent is back at its prompt.
  let line = "Restarting " + displayName(title) + (r?.continued ? " — it will continue where it left off" : "") + "…";
  if (r?.memory_warning) line += " (" + r.memory_warning + ")";
  toast(line);
  void refreshInstances();
  return true;
}
