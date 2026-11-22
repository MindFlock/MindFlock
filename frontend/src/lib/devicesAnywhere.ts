/** Reach my devices from anywhere (docs/devices-anywhere.md): the shape of
 * `GET /api/devices/anywhere` and the pure helpers Settings → Your devices
 * uses for the line under the switch. */

export interface AnywhereStatus {
  on: boolean;
  /** off | waiting | starting | on | failed | needs_github */
  state: string;
  /** The plain status line (the server words it). */
  text: string;
  /** Offer Retry. */
  retry: boolean;
  /** cloudflared is missing: offer the doctor's one-click install. */
  install: boolean;
  installed: boolean;
  tunnel: string;
  github: { state: string; text: string };
  published_at: number | null;
  found: string[];
  via_tunnel: string[];
}

export const ANYWHERE_LABEL = "Reach my devices from anywhere (no Tailscale needed)";

export const ANYWHERE_HINT =
  "Your computers find each other through a private, encrypted tunnel and your own GitHub " +
  "(a private repo holds their addresses, encrypted with your devices' key). Nothing to install " +
  "or sign up for. Tailscale, when it's there, is still used first. New — off by default.";

/** A live long code for a new device that isn't on this network
 * (`GET/POST /api/devices/anywhere/invite`, docs/devices-anywhere.md §7). */
export interface AnywhereInvite {
  code: string;
  /** Epoch seconds. */
  expires_at: number;
  /** This computer's name, as the new one will show it. */
  host: string;
  /** The code as a QR (inline SVG), when the server can draw one. */
  qr_svg?: string | null;
}

export interface AnywhereInviteState {
  invite: AnywhereInvite | null;
  /** Why the last code stopped working ("" when it didn't). */
  gone: string;
}

export const FAR_ADD_LABEL = "It isn't on my network";
export const FAR_ADD_HINT =
  "For a computer without Tailscale, anywhere: a long one-time code it pastes. Works through this " +
  "computer's private tunnel.";
export const FAR_TURN_ON_TITLE = "Turn on “Reach my devices from anywhere” here?";
export const FAR_TURN_ON_BODY =
  "The new computer reaches this one through a private, encrypted tunnel (Cloudflare carries it but " +
  "can't read it). It stays on afterwards so your devices keep finding each other — turn it off any time " +
  "under the switch above.";
export const FAR_REACHABLE_LABEL = "Let my devices reach this computer from anywhere";
export const FAR_REACHABLE_HINT =
  "Turns on “Reach my devices from anywhere” on this computer — needed unless both computers use Tailscale. " +
  "Leave it ticked unless you know you don't want it.";

/** The `mfd1:` code inside pasted text (lowercased), or "". */
export function extractDeviceCode(text: string): string {
  const m = /mfd1:[a-z2-7]+-[a-z2-7]{4}/i.exec(String(text || "").slice(0, 4096));
  return m ? m[0].toLowerCase() : "";
}

/** "Works once · expires in 9:41" for the live code. */
export function farInviteLine(inv: AnywhereInvite, nowS: number): string {
  const left = Math.max(0, Math.round(inv.expires_at - nowS));
  const m = Math.floor(left / 60);
  const s = String(left % 60).padStart(2, "0");
  return left > 0 ? `Works once · expires in ${m}:${s}` : "This code expired — make a new one";
}

/** The line under the switch: the server's words, plus who it reaches. */
export function anywhereLine(st: AnywhereStatus | null): string {
  if (!st) return "";
  if (!st.on) return "Off";
  const n = st.via_tunnel?.length || 0;
  if (st.state === "on" && n > 0) {
    return `${st.text} · ${n === 1 ? "1 device" : `${n} devices`} reached this way`;
  }
  return st.text;
}

/** Whether the line is a problem (shown as a warning). */
export function anywhereWarn(st: AnywhereStatus | null): boolean {
  return !!st && st.on && (st.state === "failed" || st.state === "needs_github");
}

/** Poll faster while something is changing. */
export function anywherePollMs(st: AnywhereStatus | null): number {
  if (st && st.on && (st.state === "starting" || (st.state === "on" && !st.published_at))) return 2000;
  return 15000;
}
