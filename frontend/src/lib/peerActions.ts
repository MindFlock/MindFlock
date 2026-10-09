/** The "Join with a code…" action: open Work with someone with its join box
 * focused (and, when given, prefilled). Kept apart from lib/peer.ts, which
 * stays free of the store so its wording tests run without one.
 *
 * Reused by the "Paste a code" router (lib/deviceActions.ts): an mfp1:/mfp2:
 * invite whose join failed reopens here, prefilled. */

import { useUi } from "../state/store";
import { PEER_SCREEN } from "./peer";

/** The DOM event PeerLinks listens for (detail: {code?: string}). */
export const PEER_JOIN_EVENT = "mf-peer-join";

let pending: { code: string } | null = null;

/** Consumed by PeerLinks when it mounts: a join the palette asked for before
 * the screen existed. */
export function takePendingPeerJoin(): { code: string } | null {
  const p = pending;
  pending = null;
  return p;
}

export function openJoinWithCode(code = ""): void {
  pending = { code };
  useUi.getState().openDialogFor("settings", PEER_SCREEN);
  // Already open on that screen: it hears this instead of mounting again.
  document.dispatchEvent(new CustomEvent(PEER_JOIN_EVENT, { detail: { code } }));
}
