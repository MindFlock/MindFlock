/** OS notifications in the desktop app, for the few things that need you
 * while MindFlock is minimized or behind another window: a computer asking
 * to join your devices (it waits ten minutes, then expires), someone
 * arriving on a peer link (compare the safety number), and an update.
 *
 * The desktop shell exposes `window.mfnotify` (electron/preload.js): show()
 * raises a native Notification in the main process; clicking it focuses the
 * window and reports the notification's `target` back here, which opens the
 * right screen. In a plain browser there is no bridge and nothing happens —
 * the in-window toast and the bell already cover a tab that is open. Only
 * shown while the window isn't focused: a focused one has the toast. */

import { useUi } from "../state/store";
import { PEER_SCREEN } from "./peer";

interface MfNotify {
  show(o: { title: string; body: string; target: string }): Promise<unknown> | void;
  onClick(cb: (target: string) => void): (() => void) | void;
}

function bridge(): MfNotify | null {
  if (typeof window === "undefined") return null;
  const w = window as unknown as { mfnotify?: MfNotify };
  return w.mfnotify && typeof w.mfnotify.show === "function" ? w.mfnotify : null;
}

/** The notification for an event, or null when it doesn't get one. `target`
 * names the screen the click opens ("devices", "peer", "update"). */
export function desktopNoteFor(
  event: string,
  data: Record<string, unknown> | null | undefined
): { title: string; body: string; target: string } | null {
  const d = data || {};
  switch (event) {
    case "device.join_requested": {
      const host = String(d.host || d.device || "A computer");
      const via = d.via ? " (asked " + String(d.via_host || d.via) + ")" : "";
      return {
        title: host + " wants to join your devices",
        body: (d.code ? "Code " + String(d.code) + " — check it shows the same, then Approve" : "Approve it in MindFlock") + via,
        target: "devices",
      };
    }
    case "peer.link_added": {
      const name = String(d.peer_name || "Someone");
      return {
        title: name + (d.repaired ? " reconnected" : " joined your invite"),
        body: d.sas ? "Compare the safety number " + String(d.sas) + " with them" : "Work with someone",
        target: "peer",
      };
    }
    case "update.available": {
      const v = String(d.version || d.latest || "");
      const where = d.host || d.device ? " on " + String(d.host || d.device) : "";
      return {
        title: "MindFlock update" + (v ? " " + v : "") + " available" + where,
        body: String(d.detail || "Open MindFlock to update"),
        target: "update",
      };
    }
    default:
      return null;
  }
}

/** Raise the OS notification for an event — only in the desktop app, and
 * only while its window isn't the one you're looking at. Returns whether
 * one was shown. */
export function desktopNotify(event: string, data: Record<string, unknown> | null | undefined): boolean {
  const b = bridge();
  if (!b) return false;
  if (typeof document !== "undefined" && document.hasFocus()) return false;
  const n = desktopNoteFor(event, data);
  if (!n) return false;
  try {
    void b.show(n);
    return true;
  } catch {
    return false;
  }
}

/** Open what a clicked notification is about. */
export function openNotifyTarget(target: string): void {
  const ui = useUi.getState();
  if (target === "devices") ui.openDialogFor("settings", "devices");
  else if (target === "peer") ui.openDialogFor("settings", PEER_SCREEN);
  else if (target === "update") ui.openDialogFor("settings", "advanced");
}

/** Listen for notification clicks (returns the unsubscribe). */
export function installDesktopNotifyClicks(): () => void {
  const b = bridge();
  if (!b || typeof b.onClick !== "function") return () => {};
  const off = b.onClick((target) => openNotifyTarget(String(target || "")));
  return typeof off === "function" ? off : () => {};
}
