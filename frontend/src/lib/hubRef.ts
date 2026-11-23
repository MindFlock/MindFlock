/** Shared files' code syntax and drag type, on their own so the terminal
 * paste (lib/clipboard) can expand codes without importing lib/hub (which
 * imports lib/clipboard). */

import { ApiError, api, instApi } from "../api/client";

/** Shared files being dragged (their codes ride along as text/plain). */
export const HUB_MIME = "application/x-mf-hub-ref";
/** The code prefixes: `file:` (shown) and the first codes' `hub:` (read for
 * good). */
export const CODE_PREFIXES = ["file:", "hub:"] as const;
/** A code in text: not glued to a word or path before it — the same rule as
 * the server's (core/hub.py HANDLE_RE). */
export const REF_RE = /(?<![A-Za-z0-9_./:@-])(?:file|hub):([A-Za-z0-9]{4,8})(?![A-Za-z0-9_])/i;

/** What a drag carries: one item or the whole selection. */
export interface DragItem {
  id: string;
  ref: string;
  name: string;
}

/** Put items on a drag: HUB_MIME holds them (`{id, ref, name, items}` — the
 * first item's fields too, for a reader that takes one), text/plain their
 * codes (a terminal or any text box takes those). */
export function setDragItems(dt: DataTransfer, items: DragItem[]): void {
  const list = items.map((i) => ({ id: i.id, ref: i.ref, name: i.name }));
  if (!list.length) return;
  dt.setData(HUB_MIME, JSON.stringify({ ...list[0], items: list }));
  dt.setData("text/plain", list.map((i) => i.ref).join(" "));
  dt.effectAllowed = "copy";
}

/** The items a drop carries (several when a selection was dragged). */
export function dragItems(dt: DataTransfer | null | undefined): DragItem[] {
  if (!dt) return [];
  try {
    const v = JSON.parse(dt.getData(HUB_MIME)) as Partial<DragItem> & { items?: DragItem[] };
    const list = Array.isArray(v?.items) && v.items.length ? v.items : v?.ref ? [v as DragItem] : [];
    return list.filter((i) => typeof i?.ref === "string" && REF_RE.test(i.ref));
  } catch {
    return [];
  }
}

/** Text bound for an agent's terminal with its codes expanded the way a send
 * would (path, name and kind, labelled as shared content). Unchanged when it
 * holds none, or when the server can't (a guest link: no shared files). */
export async function expandRefs(text: string): Promise<{ text: string; note: string }> {
  if (!REF_RE.test(text)) return { text, note: "" };
  try {
    const r = await api<{ text: string; note: string }>("/api/hub/expand", { json: { text } });
    return { text: r.text, note: r.note || "" };
  } catch {
    return { text, note: "" };
  }
}

/** Type text holding a code into a session's agent WITHOUT pressing Enter,
 * through the server (POST …/send, submit false, dialog_safe): the server
 * expands the codes and re-checks the agent's screen at that moment, so the
 * text is never typed into a permission prompt or a menu that opened
 * meanwhile — it is refused instead (409 in_dialog), nothing typed. */
export async function typeIntoAgent(session: string, text: string): Promise<{ ok: boolean; note: string }> {
  try {
    const r = await instApi<{ hub_note?: string }>(session, "/send", {
      json: { text, submit: false, dialog_safe: true },
    });
    return { ok: true, note: r?.hub_note || "" };
  } catch (err) {
    const e = err as ApiError;
    if (e?.status === 409 && (e.body as { in_dialog?: boolean } | null)?.in_dialog)
      return { ok: false, note: "The agent is asking something on screen — answer it first; nothing was typed" };
    return { ok: false, note: "Couldn't type it into the agent: " + (e?.message || "error") };
  }
}
