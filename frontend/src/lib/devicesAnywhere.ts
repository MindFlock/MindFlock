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
