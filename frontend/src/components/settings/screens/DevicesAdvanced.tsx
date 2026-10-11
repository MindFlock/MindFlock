/** Settings → Your devices → Advanced: the older ways to trust another
 * machine, kept for the cases pairing by code doesn't cover, each saying when
 * you would still use it.
 *
 *  - Trusted Tailscale accounts (general.tailnet_trusted_logins, moved here
 *    from Security): a browser on a ticked account's own device skips the
 *    token — for a phone or laptop that doesn't run MindFlock at all.
 *  - Allow remote control (general.remote_control, moved from Security) and
 *    pair by pasted token (POST /api/devices/{device}/connect — the former
 *    sidebar "Connect…" dialog, now an inline form): for a MindFlock too old
 *    to join your devices, or one you don't own.
 *
 * No native prompt/confirm: the desktop app has neither. */

import { useEffect, useState } from "react";
import { api } from "../../../api/client";
import { toast } from "../../../lib/toast";
import { queryClient, refreshInstances, useDevices } from "../../../state/queries";
import { useSettings } from "../useSettings";

/** GET /api/settings/tailnet-trust (backend.web.core.tailnet_trust.status). */
interface TailnetTrust {
  available: boolean;
  self_login: string;
  self_tagged: boolean;
  logins: string[];
  shared_link_supported: boolean;
}

/** "Trusted Tailscale accounts": one checkbox per login that owns an untagged
 * device on this tailnet — a checked login's own devices skip the token. */
export function TailnetTrustRows() {
  const s = useSettings();
  const [info, setInfo] = useState<TailnetTrust | null>(null);
  const stored = s.get("general", "tailnet_trusted_logins");
  const trusted = Array.isArray(stored) ? (stored as string[]) : [];

  useEffect(() => {
    let live = true;
    api<TailnetTrust>("/api/settings/tailnet-trust")
      .then((r) => live && setInfo(r || null))
      .catch(() => live && setInfo(null));
    return () => {
      live = false;
    };
  }, []);

  // A trusted login that no longer owns a device here still shows, so it can
  // be unticked.
  const choices = [...new Set([...(info?.logins || []), ...trusted])].sort();
  const toggle = (login: string, on: boolean) => {
    const next = new Set(trusted);
    if (on) next.add(login);
    else next.delete(login);
    s.saveField("general", "tailnet_trusted_logins", [...next]);
  };

  return (
    <div
      className="set-row"
      id="tailnet-trust-row"
      data-control="trusted-accounts"
      title="Requests from these Tailscale accounts' own (untagged) devices need no access token."
    >
      <span className="set-label">Trusted Tailscale accounts</span>
      <span className="set-hint devices-when">
        Still useful for a phone or laptop that doesn't run MindFlock: signed in to Tailscale as a
        ticked account, its browser opens this device without the access token.
      </span>
      {info === null ? (
        <span className="muted">Checking Tailscale…</span>
      ) : !info.available && !choices.length ? (
        <span className="muted" id="tailnet-trust-unavailable">
          Tailscale isn't running on this device.
        </span>
      ) : (
        <div className="tailnet-trust-logins">
          {choices.map((login) => (
            <label className="check" key={login}>
              <input
                type="checkbox"
                data-login={login}
                checked={trusted.includes(login)}
                onChange={(e) => toggle(login, e.target.checked)}
              />
              {login}
              {login === info.self_login ? <span className="muted"> (this device's owner)</span> : null}
            </label>
          ))}
        </div>
      )}
      <span className="set-hint" id="tailnet-trust-hint">
        Tagged devices and devices shared in from other accounts still need the token.
        {info && !info.shared_link_supported
          ? " On this OS that only covers this device's own address — requests through the shared phone link still ask for the token."
          : ""}
      </span>
    </div>
  );
}

/** "Allow remote control": whether a MindFlock that pasted this device's
 * access token may drive its sessions. Your devices always can. */
export function RemoteControlRow() {
  const s = useSettings();
  const remote = String(s.get("general", "remote_control") ?? "");
  return (
    <label
      className="set-row"
      id="remote-control-row"
      title="Whether MindFlock devices you paired by access token may list and drive this device's sessions. Your devices always can."
    >
      <span className="set-label">Allow remote control</span>
      <span className="set-hint devices-when">
        Still useful for a MindFlock too old to join your devices, or one you don't own: with this
        on, a MindFlock that holds this device's access token (Settings → Security) shows its
        sessions in its sidebar and drives them.
      </span>
      <select
        data-group="general"
        data-field="remote_control"
        value={remote}
        onChange={(e) => s.saveField("general", "remote_control", e.target.value)}
      >
        <option value="">Off (default) — only your devices can control this one</option>
        <option value="on">On — devices paired by token can control it too</option>
      </select>
      <span className="set-hint" id="remote-control-hint">
        To cut off one of your own devices, remove it from the roster above instead.
      </span>
    </label>
  );
}

/** "Pair by pasted token": the other half of Allow remote control, for a
 * MindFlock on your tailnet that isn't one of your devices and wants a token
 * (the former sidebar "Connect…" dialog, inline). The server checks the
 * token against that device before persisting it. */
export function PairByToken({ focusDevice }: { focusDevice?: string }) {
  const { data } = useDevices();
  const rows = (data?.devices || []).filter((d) => d.reachable && !d.member && d.needs_token);
  const [device, setDevice] = useState("");
  const [token, setToken] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  // The sidebar's Connect… lands here aimed at one device.
  useEffect(() => {
    if (focusDevice) setDevice(focusDevice);
  }, [focusDevice]);
  const chosen = device || rows[0]?.device || "";

  const submit = async () => {
    if (!chosen || !token.trim() || busy) return;
    setErr("");
    setBusy(true);
    try {
      const devices = await api(`/api/devices/${encodeURIComponent(chosen)}/connect`, {
        json: { token: token.trim() },
      });
      queryClient.setQueryData(["devices"], devices);
      setToken("");
      toast("Connected to " + chosen);
      refreshInstances();
    } catch (e) {
      setErr((e as Error).message || "could not connect");
    } finally {
      setBusy(false);
    }
  };

  return (
    <form
      className="set-row"
      id="pair-by-token"
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
    >
      <span className="set-label">Pair by pasted token</span>
      <span className="set-hint devices-when">
        Still useful for someone else's MindFlock on your tailnet, or one too old to join: paste
        its access token (Settings → Security there, or <code>mindflock token</code>) to see and
        drive its sessions. It needs Allow remote control on. Your own computers join above
        instead — no token to paste.
      </span>
      {rows.length ? (
        <div className="devices-actions">
          <select
            id="pair-by-token-device"
            value={chosen}
            disabled={busy}
            onChange={(e) => setDevice(e.target.value)}
          >
            {rows.map((d) => (
              <option key={d.device} value={d.device}>
                {d.host || d.device}
              </option>
            ))}
          </select>
          <input
            type="password"
            id="device-token-input"
            autoComplete="off"
            spellCheck={false}
            placeholder="Paste the access token"
            value={token}
            disabled={busy}
            onChange={(e) => setToken(e.target.value)}
          />
          <button type="submit" className="test-btn" id="pair-by-token-connect" disabled={busy || !token.trim()}>
            {busy ? "Connecting…" : "Connect"}
          </button>
        </div>
      ) : (
        <span className="muted" id="pair-by-token-none">
          No MindFlock on your tailnet is asking for a token right now.
        </span>
      )}
      {err && (
        <span id="device-dialog-err" className="dialog-err">
          {err}
        </span>
      )}
    </form>
  );
}

/** The fold. Opens by itself when the sidebar's Connect… aimed at a device
 * that can only pair by token. */
export function DevicesAdvanced({ focusDevice }: { focusDevice?: string }) {
  const { data } = useDevices();
  const legacy = !!focusDevice && (data?.devices || []).some((d) => d.device === focusDevice && !d.member && d.needs_token);
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (legacy) setOpen(true);
  }, [legacy]);
  return (
    <details className="devices-advanced" id="devices-advanced" open={open} onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary className="set-section-title">Advanced — other ways to trust a machine</summary>
      <p className="set-hint set-block-hint">
        Pairing by code (above) covers your own computers. These remain for what it doesn't.
      </p>
      <TailnetTrustRows />
      <RemoteControlRow />
      <PairByToken focusDevice={legacy ? focusDevice : undefined} />
    </details>
  );
}
