/** Restart an offline agent: `POST /api/instances/{title}/revive`, the same
 * lazy revive the server does when a terminal attaches or a prompt is sent
 * (it resumes the conversation after a crash, starts fresh after a clean
 * quit; an agent that died mid-turn is also queued a "continue"). The
 * Offline chip's Restart action (lib/sessionState).
 *
 * A session HELD offline after a crash (lib/heldOffline, backend
 * core/held.py) comes back only through this call: it releases the hold,
 * then launches. Its surfaces say "Start" (`startSession`), never Restart. */

import { instApi } from "../api/client";
import { refreshInstances } from "../state/queries";
import { errMsg } from "./format";
import { toast } from "./toast";
import { displayName } from "../state/store";
import { forceActivity } from "./sessionState";
import { isMemoryCancel, sleptLine, withMemoryConfirm } from "./memoryConfirm";

/** The server's answer to a revive. */
export interface ReviveAnswer {
  title: string;
  revived: boolean;
  continued?: boolean;
  /** The machine's memory is low ("" / absent when it is fine). While it is,
   * the server asks first (409 memory_confirm, lib/memoryConfirm) and this
   * says it once the person chose Start anyway. */
  memory_warning?: string;
}

async function revive(title: string, verb: "Restart" | "Start"): Promise<ReviveAnswer | null> {
  let r: ReviveAnswer;
  try {
    r = await withMemoryConfirm(
      displayName(title),
      (anyway) => instApi<ReviveAnswer>(title, "/revive", anyway ? { json: { start_anyway: true } } : { method: "POST" }),
      (a) => toast(sleptLine(a))
    );
  } catch (e) {
    if (isMemoryCancel(e)) return null;
    toast("Couldn't " + verb.toLowerCase() + " " + displayName(title) + " — " + errMsg(e));
    return null;
  }
  // The next activity poll says what it is doing; until then it is no longer
  // offline, so the row stops offering Restart twice.
  forceActivity(title, "idle");
  // It died mid-turn: a "continue where you left off" waits at the front of
  // its prompt queue, sent once the agent is back at its prompt.
  let line =
    (verb === "Start" ? "Starting " : "Restarting ") +
    displayName(title) +
    (r?.continued ? " — it will continue where it left off" : "") +
    "…";
  if (r?.memory_warning) line += " (" + r.memory_warning + ")";
  toast(line);
  void refreshInstances();
  return r || { title, revived: true };
}

export async function reviveSession(title: string): Promise<boolean> {
  return (await revive(title, "Restart")) !== null;
}

/** Start a session held offline after a crash: the same call, said as
 * Start. Resolves to the server's answer (its `memory_warning` too), or null
 * when it was refused (the reason is toasted). */
export function startSession(title: string): Promise<ReviveAnswer | null> {
  return revive(title, "Start");
}
