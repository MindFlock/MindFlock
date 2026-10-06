/** Paste a saved prompt into a session — the one code path behind the sidebar
 * Prompts bar and the Prompts dialog, so both say the same thing and guard
 * the same way.
 *
 * Pasting never sends: POST /send with submit:false types the text into the
 * agent's input and leaves it there for your Enter. dialog_safe makes the
 * server re-check the agent live and refuse (409, nothing typed) when it sits
 * on a permission/limit prompt, where typed text would ANSWER the dialog — the
 * target may be a session you can't see. Same contract as every other UI paste
 * (lib/flockActions.ts). */

import { instApi } from "../api/client";
import { errorPop } from "./errorPop";
import { toast } from "./toast";
import { windowName } from "./windowName";
import { ALL_RUNNING, pasteIntoAll, pastedAllToast } from "./promptTargets";

// One paste at a time, app-wide: a second click while "All" is still fanning
// out would paste the text twice into every session.
let busy = false;

/** Paste `prompt` into `target` (a session title, or ALL_RUNNING for every
 * title in `running`). Resolves true when at least one session got it. */
export async function pastePrompt(target: string, running: string[], prompt: string): Promise<boolean> {
  if (!target) {
    toast("Choose a session to paste into first, then click a prompt");
    return false;
  }
  if (busy) return false;
  busy = true;
  const send = (t: string) =>
    instApi(t, "/send", { json: { text: prompt, submit: false, dialog_safe: true } });
  try {
    if (target === ALL_RUNNING) {
      const titles = running.slice();
      const { ok, failed } = await pasteIntoAll(titles, send);
      if (failed.length) {
        errorPop(
          `Couldn't paste into ${failed.length} of ${titles.length} sessions`,
          failed.map((f) => windowName(f.title) + ": " + f.error).join(" · ")
        );
      }
      if (!ok.length) return false;
      toast(pastedAllToast(ok.length));
      return true;
    }
    try {
      await send(target);
      toast("Pasted into " + windowName(target) + " — press Enter there to send");
      return true;
    } catch (err) {
      toast("Paste failed: " + ((err as Error).message || ""));
      return false;
    }
  } finally {
    busy = false;
  }
}
