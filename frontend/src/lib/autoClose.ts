/** Finished sub-sessions close themselves (backend core/auto_close.py): a
 * worker an agent spawned that reported done or failed, sits idle, and has
 * its work merged (or none of its own) is CLOSED — worktree kept, in Recently
 * closed — once its grace window passes. This module holds the app's side:
 *
 * - the toast for `system.subsessions_closed` ("Closed 2 finished workers of
 *   lead — Undo"); Undo reopens them and marks them Keep open, so the next
 *   pass leaves them be (POST /api/auto-close/undo);
 * - the ⋯ menu's Keep open switch (POST /api/instances/<t>/keep-open);
 * - the row line for an orphan: a worker whose parent was closed while its
 *   work is not merged ("parent closed — review or close"). */

import type { Instance } from "../api/types";
import { api, instApi } from "../api/client";
import { refreshInstances, refreshRecentlyClosed } from "../state/queries";
import { displayName } from "../state/store";
import { errorPop } from "./errorPop";
import { errMsg } from "./format";
import { toast } from "./toast";

/** The server's event for one pass's closes under one parent. */
export const AUTO_CLOSED_EVENT = "system.subsessions_closed";

/** That event's data. */
export interface AutoClosedData {
  parent?: string;
  count?: number;
  titles?: string[];
  entries?: { title?: string; id?: string }[];
  detail?: string;
}

/** "Closed 2 finished workers of “lead”" — names as the rail shows them. */
export function autoClosedToastText(d: AutoClosedData, nameOf: (t: string) => string = displayName): string {
  const titles = (d.titles || []).filter(Boolean);
  const n = titles.length || Number(d.count) || 0;
  const what = n === 1 ? "finished worker “" + nameOf(titles[0] || "") + "”" : n + " finished workers";
  const of = d.parent ? " of “" + nameOf(d.parent) + "”" : "";
  return "Closed " + what + of;
}

/** The Recently closed entry ids the toast's Undo reopens. */
export function autoClosedIds(d: AutoClosedData): string[] {
  return (d.entries || []).map((e) => String(e?.id || "")).filter(Boolean);
}

/** The toast's Undo: reopen them, and keep them open from now on. */
export async function undoAutoClose(ids: string[]): Promise<string[]> {
  if (!ids.length) return [];
  try {
    const r = await api<{ reopened?: string[]; failed?: Record<string, string> }>("/api/auto-close/undo", {
      method: "POST",
      json: { ids },
    });
    const back = r.reopened || [];
    const failed = Object.values(r.failed || {});
    if (failed.length) errorPop("Some couldn't be reopened", failed.join("; "));
    else if (back.length) toast("Reopened " + back.map(displayName).join(", ") + " · kept open");
    return back;
  } catch (err) {
    errorPop("Undo failed", errMsg(err));
    return [];
  } finally {
    void refreshRecentlyClosed();
    void refreshInstances();
  }
}

/** Show the toast for one `system.subsessions_closed`. */
export function autoClosedToast(d: AutoClosedData): void {
  const ids = autoClosedIds(d);
  const text = autoClosedToastText(d);
  toast(text, {
    action: ids.length
      ? { label: "Undo", onClick: () => void undoAutoClose(ids), ariaLabel: "Undo: reopen — " + text }
      : undefined,
    title: "They are in Recently closed. Undo reopens them and keeps them open.",
    duration: 9000,
  });
  void refreshRecentlyClosed();
}

/** Whether the session is one the sweep may close (an agent's worker). */
export function isAutoClosable(inst: Partial<Instance> | undefined): boolean {
  return !!inst?.auto_close && !inst.device;
}

/** The row's orphan line, or null: "parent closed — review or close". */
export function orphanLine(inst: Partial<Instance> | undefined): { text: string; title: string } | null {
  const of = inst?.auto_close?.orphan_of;
  if (!of || inst?.device) return null;
  return {
    text: "parent closed — review or close",
    title:
      "“" + displayName(of) + "”, the session that started this worker, was closed while this worker's " +
      "work was not merged. Review it, merge or ship it, or close it.",
  };
}

/** The row's "closes in 9 min" line for a session an agent on ANOTHER device
 * started here (core.remote_origin), while the sweep has it in its grace
 * window — with a one-click Keep beside it (`setKeepOpen`). Null otherwise:
 * a person's session, one kept open, one not finished yet. `now` in ms. */
export function closingLine(
  inst: Partial<Instance> | undefined,
  now: number = Date.now()
): { text: string; title: string } | null {
  const ac = inst?.auto_close;
  if (!ac || ac.keep_open || !ac.closes_at || inst?.device) return null;
  if (inst?.origin?.by !== "agent") return null;
  const mins = Math.max(0, Math.ceil((ac.closes_at * 1000 - now) / 60000));
  return {
    text: mins <= 0 ? "closes now" : "closes in " + mins + " min",
    title:
      "Done and idle: started by an agent on " + (inst.origin.device_name || "another device") +
      ", it closes by itself (its folder is kept; its last reply stays in Recently closed). " +
      "Keep leaves it open.",
  };
}

/** Turn a session's Keep open on or off. */
export async function setKeepOpen(title: string, keep: boolean): Promise<void> {
  try {
    await instApi(title, "/keep-open", { method: "POST", json: { keep } });
    toast(
      keep
        ? displayName(title) + " stays open when it finishes"
        : displayName(title) + " closes by itself once it is finished and merged"
    );
  } catch (err) {
    errorPop("Keep open failed", errMsg(err));
  } finally {
    void refreshInstances();
  }
}
