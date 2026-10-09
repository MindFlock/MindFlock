/** "Turn the gate on" — the button in Settings → Devices' gate warning.
 *
 * The warning shows while this device's access gate is off and it's reachable
 * beyond this machine (`gate_warning` in GET /api/fleet). Turning the gate on
 * is `general.auth_mode = "on"` — the same save Settings → Security makes —
 * but with the gate on every request needs the token, from this machine too,
 * so this browser signs itself in FIRST (`POST /api/auth` with the token,
 * which sets its cookie) or the very next poll would land on the login page.
 *
 * The token comes back only to a caller allowed to see it (this machine, or a
 * browser already holding it); anyone else gets `token: null`, skips the
 * sign-in, and the save itself refuses them (403) — that error is what the
 * button shows. */

import { api } from "../api/client";

type Call = <T = unknown>(path: string, opts?: { json?: unknown }) => Promise<T>;

/** What the success toast says happens to the owner's other clients. */
export const GATE_ON_NOTE =
  "Access gate on. Your other devices sign in with your devices' key, so they keep working; " +
  "a phone signs in again by scanning the QR in Settings → Mobile.";

/** Sign this browser in, then turn the gate on. Throws (with the server's
 * message) when either step is refused. */
export async function turnGateOn(call: Call = api): Promise<void> {
  const t = await call<{ token?: string | null }>("/api/settings/auth-token");
  if (t?.token) await call("/api/auth", { json: { token: t.token } });
  await call("/api/settings", { json: { general: { auth_mode: "on" } } });
}
