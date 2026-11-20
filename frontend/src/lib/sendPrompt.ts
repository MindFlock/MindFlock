/** Send a prompt to a session's agent, and the one-press "Carry on".
 *
 * `sendPrompt` is the Sky agent tile's Send a prompt… (flights/useChainMenu),
 * here in lib/ so the rail can use it too without importing a Sky component
 * (and its stylesheets) — a component import moves CSS in the bundle. */

import { api } from "../api/client";
import { errMsg } from "./format";
import { heldQueuedText, heldWasQueued, isHeldRefusal } from "./heldOffline";
import { toast } from "./toast";
import { windowName } from "./windowName";

/** Send `text` to a session's agent (POST /api/instances/{title}/send — typed
 * and submitted as if at its terminal). True when it went. */
export async function sendPrompt(title: string, text: string): Promise<boolean> {
  const t = text.trim();
  if (!t) return false;
  try {
    await api("/api/instances/" + encodeURIComponent(title) + "/send", { method: "POST", json: { text: t, dialog_safe: true } });
    toast("Sent to " + windowName(title));
    return true;
  } catch (err) {
    // Held offline after a crash: the server queued it, sent after Start.
    if (isHeldRefusal(err)) {
      toast(heldQueuedText(windowName(title), heldWasQueued(err)));
      return true;
    }
    toast("Couldn't send: " + errMsg(err));
    return false;
  }
}

/** What Carry on types to an agent that went idle on uncommitted changes
 * (lib/sessionState `uncommitted`): finish, or say what stops it. */
export const CARRY_ON_PROMPT =
  "Carry on with your task — you stopped with uncommitted changes. Finish the work, or tell me what is blocking you.";

/** Carry on: tell an idle agent to finish what it left uncommitted. */
export function carryOn(title: string): Promise<boolean> {
  return sendPrompt(title, CARRY_ON_PROMPT);
}
