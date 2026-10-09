/** Opening Settings → Devices aimed at something, and the one "Paste a code"
 * router. Kept apart from lib/fleet.ts, which stays free of the store and
 * of fetches so its wording tests run without either.
 *
 *  - openDevicesFor({device}) — the sidebar's "Add to my devices…": the
 *    screen opens with that computer's row highlighted.
 *  - openDevicesFor({paste}) — a code to join with, prefilled in the paste
 *    box (the person still presses Join: it says whose settings win first).
 *  - routePastedCode(text) — any code, wherever it was pasted: one of your
 *    devices' codes goes to Devices, someone's mfp1:/mfp2: invite is joined
 *    as a peer link (lib/peer.ts joinPeerCode) and Work with someone opens
 *    on it, with the safety number to compare. */

import { api } from "../api/client";
import { useUi } from "../state/store";
import { routeCode } from "./fleet";
import { joinPeerCode, PEER_SCREEN } from "./peer";
import { openJoinWithCode } from "./peerActions";
import { toast } from "./toast";

/** The DOM event Devices listens for while it is already open. */
export const DEVICES_FOCUS_EVENT = "mf-devices-focus";

export interface DevicesFocus {
  /** A candidate's device key: highlight its row. */
  device?: string;
  /** Prefill (and focus) the "Paste a code" box. */
  paste?: string;
  /** Just focus the paste box. */
  focusPaste?: boolean;
}

let pending: DevicesFocus | null = null;

/** Consumed by Devices when it mounts: what the opener asked for before the
 * screen existed. */
export function takePendingDevicesFocus(): DevicesFocus | null {
  const p = pending;
  pending = null;
  return p;
}

export function openDevicesFor(focus: DevicesFocus = {}): void {
  pending = focus;
  useUi.getState().openDialogFor("settings", "devices");
  // Already open on that screen: it hears this instead of mounting again.
  document.dispatchEvent(new CustomEvent(DEVICES_FOCUS_EVENT, { detail: focus }));
}

/** Join someone's peer-link invite right away, then show the link (and the
 * safety number to compare) on Work with someone. A failure reopens the
 * join box there, prefilled, with the server's sentence. */
export async function joinPeerInvite(code: string): Promise<boolean> {
  toast("Joining…", { duration: 4000 });
  try {
    const link = await joinPeerCode(code);
    toast(
      (link.reconnected ? "Reconnected to " : "Paired with ") +
        (link.peer_name || "your peer") +
        (link.sas ? " — compare the safety number " + link.sas + " with them" : ""),
      { duration: 10000 }
    );
    useUi.getState().openDialogFor("settings", PEER_SCREEN);
    return true;
  } catch (e) {
    toast(e instanceof Error ? e.message : String(e), { duration: 8000 });
    openJoinWithCode(code);
    return false;
  }
}

/** Route any pasted code by its format. Returns where it went ("" when the
 * text is no code MindFlock knows). */
export function routePastedCode(text: string): "peer" | "device" | "" {
  const r = routeCode(text);
  if (r.kind === "peer") {
    void joinPeerInvite(r.code);
    return "peer";
  }
  if (r.kind === "device") {
    openDevicesFor({ paste: text.trim() });
    return "device";
  }
  return "";
}

/** Approve a join request from wherever it showed (the bell, a toast): one
 * waiting on another of your devices (`via`) is answered there by this
 * device's server, carrying the 6-digit code shown here. Toasts the result;
 * resolves to whether it went through. */
export async function approveJoinRequest(r: { id: string; via?: string; code?: string; host?: string }): Promise<boolean> {
  try {
    const res = await api<{ sync_error?: string }>("/api/fleet/requests/" + encodeURIComponent(r.id) + "/approve", {
      json: { via: r.via || "", code: r.code || "" },
    });
    toast(
      (r.host || "The device") +
        " is joining your devices" +
        (res?.sync_error ? " — but settings sync didn't start here: " + res.sync_error : ""),
      { duration: 6000 }
    );
    return true;
  } catch (e) {
    toast(e instanceof Error ? e.message : String(e), { duration: 6000 });
    return false;
  }
}
