/** The hub's reference syntax and drag type, on their own so the terminal
 * paste (lib/clipboard) can expand references without importing lib/hub
 * (which imports lib/clipboard). */

import { api } from "../api/client";

/** A hub item being dragged (its reference rides along as text/plain). */
export const HUB_MIME = "application/x-mf-hub-ref";
/** A reference in text: not glued to a word or path before it — the same
 * rule as the server's (core/hub.py HANDLE_RE). */
export const REF_RE = /(?<![A-Za-z0-9_./:@-])hub:([A-Za-z0-9]{4,8})(?![A-Za-z0-9_])/i;

/** Text bound for an agent's terminal with its references expanded the way
 * a send would (path, name and kind, labelled as hub content). Unchanged
 * when it holds none, or when the server can't (a guest link: no hub). */
export async function expandRefs(text: string): Promise<{ text: string; note: string }> {
  if (!REF_RE.test(text)) return { text, note: "" };
  try {
    const r = await api<{ text: string; note: string }>("/api/hub/expand", { json: { text } });
    return { text: r.text, note: r.note || "" };
  } catch {
    return { text, note: "" };
  }
}
