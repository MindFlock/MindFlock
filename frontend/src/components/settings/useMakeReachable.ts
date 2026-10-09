/** "Make reachable": the one fix for a device none of your others can reach
 * (bound to 127.0.0.1). It turns Tailscale mode on (general.serve_mode) and
 * the access gate on (general.auth_mode) in ONE save — never a non-local
 * bind with the gate off, which would open this machine to the whole LAN
 * as well as the tailnet — and then waits out the restart the server does
 * by itself (POST /api/settings answers `restarting: true`; see
 * useServerRestart's alreadyRequested).
 *
 * Before the gate goes on, this browser trades the device's own token for
 * its sign-in cookie (GET /api/settings/auth-token answers this machine;
 * POST /api/auth sets the cookie), so the screen that pressed the button is
 * still signed in when the server comes back.
 *
 * "Match my other devices" uses the same save, plus the shared phone link's
 * name (general.shared_link) when the others answer one this device doesn't. */

import { useCallback, useState } from "react";
import { api } from "../../api/client";
import { useServerRestart } from "./useServerRestart";

export interface MakeReachableResult {
  restarting?: boolean;
  /** Settings → Mobile's shared-link status, when the save named a link:
   * its checklist (`steps`) is what still needs doing on this device. */
  shared_link?: { steps?: { id: string; title: string; state: string; reason: string }[] };
}

/** The general.* fields one apply saves (exported for tests). */
export function reachableFields(opts: { reach: boolean; sharedLink?: string }): Record<string, string> {
  const g: Record<string, string> = {};
  if (opts.reach) {
    g.serve_mode = "tailscale";
    g.auth_mode = "on";
  }
  if (opts.sharedLink) g.shared_link = opts.sharedLink;
  return g;
}

/** Keep this browser signed in across the gate turning on. Best-effort: a
 * browser that isn't on this machine already holds a credential. */
export async function keepSignedIn(): Promise<void> {
  try {
    const r = await api<{ token?: string | null }>("/api/settings/auth-token");
    if (r?.token) await api("/api/auth", { json: { token: r.token } });
  } catch {
    /* already signed in, or not ours to sign in */
  }
}

export function useMakeReachable(onBack?: () => void) {
  const { restarting, timedOut, restart } = useServerRestart();
  const [saving, setSaving] = useState(false);

  const apply = useCallback(
    async (opts: { reach: boolean; sharedLink?: string }): Promise<MakeReachableResult | null> => {
      const general = reachableFields(opts);
      if (!Object.keys(general).length) return null;
      setSaving(true);
      try {
        if (opts.reach) await keepSignedIn();
        const res = await api<MakeReachableResult>("/api/settings", { json: { general } });
        if (res?.restarting) restart({ alreadyRequested: true, onBack });
        else onBack?.();
        return res;
      } finally {
        setSaving(false);
      }
    },
    [restart, onBack]
  );

  return { busy: saving || restarting, restarting, timedOut, apply };
}
