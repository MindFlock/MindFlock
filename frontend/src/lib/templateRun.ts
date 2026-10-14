/** Run now: start a FRESH session from a template, its prompt already sent
 * (POST /api/templates/{name}/run — the server creates it through the same
 * path as the New dialog and names it "<template>-<mon><day>"), then focus
 * it. One click from the command palette ("Run template: …") and from each
 * card in the Templates dialog. */

import { api } from "../api/client";
import { refreshInstances } from "../state/queries";
import { selectSession } from "./sessionActions";
import { toast } from "./toast";
import { errMsg } from "./format";
import { ensureTemplates } from "./templates";

/** Starts the template's session and focuses it. Resolves to the new
 * session's title, or "" when the server refused (the reason is toasted). */
export async function runTemplateNow(name: string): Promise<string> {
  try {
    const made = await api<{ title?: string }>("/api/templates/" + encodeURIComponent(name) + "/run", {
      method: "POST",
    });
    const title = String(made?.title || "");
    toast(`Started “${title || name}” from template “${name}”`);
    await refreshInstances();
    if (title) selectSession(title);
    // The card's "last run" moves.
    void ensureTemplates(true).catch(() => {});
    return title;
  } catch (err) {
    toast(`Couldn't run “${name}”: ${errMsg(err)}`);
    return "";
  }
}
