/** Other MindFlock devices on the tailnet, as the New Session dialog needs them.
 *
 * A session can be started on ANY connected device: the server forwards a
 * short allow-list of the dialog's requests to it under
 * `/api/devices/<device>/fwd/…` (see backend/web/core/remote.py), so the
 * folder suggestions, the browser, the agent list and the create itself all
 * answer from the device the session will live on. `""` is this device. */

import { api } from "../api/client";
import type { Device, DevicesResponse } from "../api/types";

/** The path that reaches `path` on `device` (`""` = here, unchanged). */
export function devicePath(device: string, path: string): string {
  return device ? `/api/devices/${encodeURIComponent(device)}/fwd${path}` : path;
}

/** `api()` aimed at `device` — the same call, answered over there. */
export function deviceApi(device: string) {
  return <T = unknown>(path: string, opts?: RequestInit & { json?: unknown }): Promise<T> =>
    api<T>(devicePath(device, path), opts);
}

/** A session's title as THIS server knows it: a remote device's sessions are
 * namespaced `<device>::<title>` in the merged list. */
export function deviceTitle(device: string, title: string): string {
  return device ? `${device}::${title}` : title;
}

/** The devices a session can be started on right now: paired, permitted, and
 * answering. Unreachable or unpaired ones live in the sidebar, where they can
 * be fixed; a picker that offered them would only fail at Create. */
export function startableDevices(resp: DevicesResponse | undefined | null): Device[] {
  return (resp?.devices || []).filter((d) => d.connected && !!d.device);
}

/** What to call a device in a picker: its hostname, unless another device on
 * the tailnet shares it, then the (unique) MagicDNS label. */
export function deviceLabel(d: Device, all: Device[], selfHost = ""): string {
  const key = d.device;
  const host = d.host || "";
  if (!host) return key;
  const clash = host === selfHost || all.filter((o) => (o.host || "") === host).length > 1;
  return clash ? key : host;
}
