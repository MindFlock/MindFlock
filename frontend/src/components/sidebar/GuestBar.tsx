/** "Guest link live · Kill" — the rail's kill switch for guest links
 * (docs/people.md). While any guest link exists, or the public tunnel is up
 * or starting, the owner sees it here without opening Settings, and one
 * click (DELETE /api/guests/tunnel) revokes every link and closes the public
 * address; a guest's open terminal closes on its next frame. Never shown to
 * a guest (config.guest) — they have no links of their own to see. Polled
 * every 10 s: the state is tiny and the bar is the owner's reassurance. */

import { useEffect, useState } from "react";
import { api } from "../../api/client";
import type { GuestLink, GuestTunnel } from "../../api/types";
import { toast } from "../../lib/toast";
import { useConfig } from "../../state/queries";
import { killedText } from "../settings/screens/peer/GuestLinks";

const POLL_MS = 10_000;

/** What the bar says. Exported for tests. */
export function guestBarText(links: number, tunnel: GuestTunnel | null): string {
  const who = links === 1 ? "1 guest link" : links + " guest links";
  if (tunnel?.state === "up") return who + " · public link up";
  if (tunnel?.state === "starting") return who + " · public link starting…";
  return who;
}

export function GuestBar() {
  const { data: config } = useConfig();
  const [links, setLinks] = useState<GuestLink[]>([]);
  const [tunnel, setTunnel] = useState<GuestTunnel | null>(null);
  const [busy, setBusy] = useState(false);
  const owner = !!config && !config.guest;

  useEffect(() => {
    if (!owner) return;
    let live = true;
    const tick = () =>
      api<{ links: GuestLink[]; tunnel: GuestTunnel }>("/api/guests")
        .then((r) => {
          if (!live) return;
          setLinks(r.links || []);
          setTunnel(r.tunnel || null);
        })
        .catch(() => {
          /* a 403 on a gate-off tailnet caller, a blip: nothing to show */
        });
    void tick();
    const t = setInterval(() => void tick(), POLL_MS);
    return () => {
      live = false;
      clearInterval(t);
    };
  }, [owner]);

  const show = owner && (links.length > 0 || tunnel?.state === "up" || tunnel?.state === "starting");
  if (!show) return null;

  const kill = async () => {
    setBusy(true);
    try {
      const r = await api<{ revoked: number }>("/api/guests/tunnel", { method: "DELETE" });
      setLinks([]);
      setTunnel(null);
      toast(killedText(r.revoked || 0));
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div id="guest-bar" role="group" aria-label="Guest links">
      <span className="gb-text">{guestBarText(links.length, tunnel)}</span>
      <button
        type="button"
        className="gb-kill"
        disabled={busy}
        title="Revoke every guest link and close the public address — open guest terminals close at once"
        onClick={() => void kill()}
      >
        ⏻ Kill
      </button>
    </div>
  );
}
