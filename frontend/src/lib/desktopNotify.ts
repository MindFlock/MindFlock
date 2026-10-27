/** OS notifications in the desktop app, for the few things that need you
 * while MindFlock is minimized or behind another window: a computer asking
 * to join your devices (it waits ten minutes, then expires), someone
 * arriving on a peer link (compare the safety number), a joint task proposed
 * to you, a mirror that failed, someone unlinking you, and an update.
 *
 * The desktop shell exposes `window.mfnotify` (electron/preload.js): show()
 * raises a native Notification in the main process; clicking it focuses the
 * window and reports the notification's `target` back here, which opens the
 * right screen. In a plain browser there is no bridge and nothing happens —
 * the in-window toast and the bell already cover a tab that is open. Only
 * shown while the window isn't focused: a focused one has the toast.
 *
 * A session asking you something — a dialog on its screen (activity →
 * clarify) or a question in its reply (`session.question`) — gets one too,
 * "[3] name needs you: Question" with the question as the body; its click
 * (`session:<title>`) selects the session and opens the ask pop-up on it.
 * The browser addon (static/addons/notify.js) leaves these to the shell. */

import { baseName, PEER_SCREEN } from "./peer";
import { openSettings } from "./openSettings";
import { slotNumber, windowName } from "./windowName";
import { selectSession } from "./sessionActions";
import { openAsk } from "./ask";

interface MfNotify {
  show(o: { title: string; body: string; target: string; version?: string }): Promise<unknown> | void;
  onClick(cb: (target: string) => void): (() => void) | void;
}

function bridge(): MfNotify | null {
  if (typeof window === "undefined") return null;
  const w = window as unknown as { mfnotify?: MfNotify };
  return w.mfnotify && typeof w.mfnotify.show === "function" ? w.mfnotify : null;
}

/** The click target of a session's notification. */
export const SESSION_TARGET = "session:";

/** "[3] name" — the session's rail slot and name, as every notification
 * leads (docs/web-ui.md). */
function slotted(title: string): string {
  const n = slotNumber(title);
  return (n ? "[" + n + "] " : "") + windowName(title);
}

function clip(text: string, max = 200): string {
  const t = text.replace(/\s+/g, " ").trim();
  return t.length > max ? t.slice(0, max - 1).trimEnd() + "…" : t;
}

/** The notification for an event, or null when it doesn't get one. `target`
 * names the screen the click opens ("devices", "peer", "update", or
 * "session:<title>"). `env`: the envelope's session and new value, for the
 * session events. */
export function desktopNoteFor(
  event: string,
  data: Record<string, unknown> | null | undefined,
  env?: { session?: string; new?: string | null }
): { title: string; body: string; target: string; version?: string } | null {
  const d = data || {};
  switch (event) {
    case "session.activity_changed": {
      // A dialog came up (the activity's other moves are not news here).
      const title = String(env?.session || "");
      if (env?.new !== "clarify" || !title) return null;
      const text = String(d.question || d.text || "").trim();
      return {
        title: slotted(title) + " needs you: Question",
        body: text ? clip(text) : "It's waiting on a dialog — answer it in MindFlock",
        target: SESSION_TARGET + title,
      };
    }
    case "session.question": {
      // Its reply ended with a question in plain words.
      const title = String(env?.session || d.title || "");
      if (!title) return null;
      const text = String(d.text || "").trim();
      return {
        title: slotted(title) + " needs you: Question",
        body: text ? clip(text) : "It asked you a question in its reply",
        target: SESSION_TARGET + title,
      };
    }
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
        body: d.sas ? "Compare the safety number " + String(d.sas) + " with them" : "People",
        target: "peer",
      };
    }
    case "peer.joint_proposed": {
      const name = String(d.peer_name || "Someone");
      const brief = String(d.brief || "").trim();
      return {
        title: name + " proposes a joint task",
        body: brief ? (brief.length > 120 ? brief.slice(0, 119).trimEnd() + "…" : brief) : "Accept or decline it in People",
        target: "peer",
      };
    }
    case "peer.mirror_failed": {
      const name = String(d.peer_name || "your peer");
      const repo = String(d.repo_name || "").trim() || baseName(String(d.repo_path || "")) || "your repo";
      return {
        title: "Couldn't mirror " + name + "'s work into " + repo,
        body: String(d.error || "").trim() || "See the Mirror row in People",
        target: "peer",
      };
    }
    case "peer.link_removed": {
      // Only their unlink: your own Remove happened in front of you.
      if (d.by !== "peer") return null;
      const name = String(d.peer_name || "Your peer");
      const kept = String(d.kept_folder || "").trim();
      return {
        title: name + " unlinked",
        body: kept ? "Your shared folder is kept at " + kept : "You're no longer connected",
        target: "peer",
      };
    }
    case "update.available": {
      // {latest, tag, here, behind: [{key, host, version}], count, detail}
      // (backend.web.core.fleet_update): where the Update button is decides
      // the click — Devices when other devices are behind.
      const v = String(d.latest || d.version || "").replace(/^v/i, "");
      const behind = Array.isArray(d.behind) ? d.behind.length : 0;
      return {
        title: "MindFlock " + (v ? v + " " : "") + "is available",
        body: String(d.detail || "") || (behind ? "Update your devices in Settings → Your devices" : "Open MindFlock to update"),
        target: behind ? "devices" : "update",
        // The shell shows one notice per release (its own update toast
        // included), keyed by this.
        version: v,
      };
    }
    default:
      return null;
  }
}

/** One notification per session per SESSION_NOTE_GAP_MS: a dialog and the
 * question that follows it, or a redraw re-announcing it, is one ping. */
export const SESSION_NOTE_GAP_MS = 30_000;
const sessionNotedAt = new Map<string, number>();

/** Raise the OS notification for an event — only in the desktop app, and
 * only while its window isn't the one you're looking at. Returns whether
 * one was shown. */
export function desktopNotify(
  event: string,
  data: Record<string, unknown> | null | undefined,
  env?: { session?: string; new?: string | null }
): boolean {
  const b = bridge();
  if (!b) return false;
  if (typeof document !== "undefined" && document.hasFocus()) return false;
  const n = desktopNoteFor(event, data, env);
  if (!n) return false;
  if (n.target.startsWith(SESSION_TARGET)) {
    const now = Date.now();
    if (now - (sessionNotedAt.get(n.target) || 0) < SESSION_NOTE_GAP_MS) return false;
    sessionNotedAt.set(n.target, now);
  }
  try {
    void b.show(n);
    return true;
  } catch {
    return false;
  }
}

/** Open what a clicked notification is about. */
export function openNotifyTarget(target: string): void {
  if (target.startsWith(SESSION_TARGET)) {
    const title = target.slice(SESSION_TARGET.length);
    if (!title) return;
    selectSession(title, { noKeyboard: true });
    openAsk(title);
  } else if (target === "devices") openSettings("devices");
  else if (target === "peer") openSettings(PEER_SCREEN);
  else if (target === "update") openSettings("advanced");
}

/** Listen for notification clicks (returns the unsubscribe). */
export function installDesktopNotifyClicks(): () => void {
  const b = bridge();
  if (!b || typeof b.onClick !== "function") return () => {};
  const off = b.onClick((target) => openNotifyTarget(String(target || "")));
  return typeof off === "function" ? off : () => {};
}
