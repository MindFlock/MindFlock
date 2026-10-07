/** Settings → Mobile (partial 103 + loadMobile, section 21): /m URLs + QR,
 * the tailscale-mode toggle, the restart-to-apply flow, and the shared phone
 * link (one Tailscale Service URL that whichever device is up answers). */

import { useCallback, useEffect, useState } from "react";
import { api } from "../../../api/client";
import { useConfig } from "../../../state/queries";
import { useServerRestart } from "../useServerRestart";
import type { ScreenProps } from "../SettingsDialog";

interface SharedLinkState {
  enabled?: boolean;
  name?: string;
  service?: string;
  url?: string;
  advertised?: boolean;
  approved?: boolean;
  tagged?: boolean;
  error?: string;
  devices?: Array<{ device: string; host: string; reachable: boolean }>;
}

interface MobilePayload {
  shared?: SharedLinkState;
  serve_mode?: string;
  local_only?: boolean;
  qr_svg?: string;
  note?: string;
  urls?: Array<{ label: string; url: string }>;
  token?: string;
}

export function Mobile(_: ScreenProps) {
  const { data: config } = useConfig();
  const [data, setData] = useState<MobilePayload | null>(null);
  const [error, setError] = useState("");
  const { restarting, restart } = useServerRestart();
  const [modeBusy, setModeBusy] = useState(false);
  const tailscale = config?.caps?.tailscale !== false;

  const load = useCallback(async () => {
    if (!tailscale) return;
    try {
      setData(await api<MobilePayload>("/api/mobile"));
      setError("");
    } catch {
      setError("Could not load mobile info.");
    }
  }, [tailscale]);

  useEffect(() => {
    load();
  }, [load]);

  const setMode = async (on: boolean) => {
    setModeBusy(true);
    let restarting = false;
    try {
      const res = await api<{ restarting?: boolean }>("/api/settings", {
        json: { general: { serve_mode: on ? "tailscale" : "local" } },
      });
      restarting = !!res?.restarting;
    } catch {
      /* revert via reload */
    }
    setModeBusy(false);
    // Turning tailscale mode on restarts the server by itself (which interface
    // uvicorn binds is fixed at boot). It's already going down — wait it out
    // and refresh the URLs/QR, rather than letting the reload below fail
    // against a port that is mid-re-exec.
    if (restarting) restart({ alreadyRequested: true, onBack: load });
    else load();
  };

  // Refresh the QR/URLs in place once the server is back — the new serve mode
  // changes them. No page reload here: that would close Settings, and this
  // flow is "apply one toggle", not "restart everything".
  const onRestart = () => restart({ onBack: load });

  // Saved choice not live yet (an explicit choice only — "" never nags).
  const pending = !!(data?.serve_mode && (data.serve_mode === "tailscale") === !!data.local_only);

  return (
    <>
      <h3 className="set-section-title">Mobile</h3>
      <div className="caps-gate" data-caps-gate="tailscale">
        <p>
          Install <strong>Tailscale</strong> to get access to these features — it puts your
          phone and this machine on a private network, so you can drive your sessions from
          anywhere.
        </p>
        <p>
          Get it at{" "}
          <a href="https://tailscale.com/download" target="_blank" rel="noopener noreferrer">
            tailscale.com/download
          </a>{" "}
          (sign in on both devices), then reopen this screen — the QR code and phone URLs
          appear here.
        </p>
      </div>
      <p className="set-hint">
        Open MindFlock on your phone. Scan the QR from a device on your Tailscale network, or
        use one of the URLs below.
      </p>
      <div id="mobile-body">
        {!tailscale ? null : error ? (
          <p className="error">{error}</p>
        ) : !data ? (
          <p className="set-hint">Loading…</p>
        ) : (
          <>
            <div
              className="set-row set-switch-row"
              title="Bind the server to all interfaces so phones on your tailnet can reach it"
            >
              <span className="set-label">Tailscale mode</span>
              {/* label wraps only the switch, so clicking the row text no longer flips it */}
              <label className="ca-switch">
                <input
                  type="checkbox"
                  checked={data.serve_mode === "tailscale"}
                  disabled={modeBusy}
                  onChange={(e) => setMode(e.target.checked)}
                />
                <span className="ca-slider" />
              </label>
            </div>
            {pending && (
              <button type="button" className="test-btn" disabled={restarting} onClick={onRestart}>
                {restarting ? "Restarting…" : "Restart server to apply"}
              </button>
            )}
            {data.qr_svg && (
              // Server-generated segno SVG (trusted).
              <div id="mobile-qr" dangerouslySetInnerHTML={{ __html: data.qr_svg }} />
            )}
            {data.note && <p className="set-hint">{data.note}</p>}
            {(data.urls || []).map((u) => (
              <div className="mobile-url-row" key={u.url}>
                <span className="set-label">{u.label}</span>
                <a href={u.url} target="_blank" rel="noopener noreferrer">
                  {u.url}
                </a>
              </div>
            ))}
            {data.token && (
              <label className="set-row">
                <span className="set-label">Access token</span>
                <input readOnly value={data.token} onClick={(e) => (e.target as HTMLInputElement).select()} />
              </label>
            )}
            <SharedLink shared={data.shared || {}} onChanged={load} />
          </>
        )}
      </div>
    </>
  );
}

const DEFAULT_SHARED_NAME = "mindflock";

/** One phone URL for every device: each device that turns this on (with the
 * same name) advertises the same Tailscale Service, and Tailscale routes the
 * phone to whichever is up. Takes effect on save — no restart. */
function SharedLink({ shared, onChanged }: { shared: SharedLinkState; onChanged: () => void }) {
  const [name, setName] = useState(shared.name || DEFAULT_SHARED_NAME);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    if (shared.name) setName(shared.name);
  }, [shared.name]);

  const save = async (value: string) => {
    setBusy(true);
    setError("");
    try {
      await api("/api/settings", { json: { general: { shared_link: value } } });
    } catch (e) {
      setError((e as Error).message || "Could not save.");
    }
    setBusy(false);
    onChanged();
  };

  const on = !!shared.enabled;
  const svc = shared.service || "svc:" + (name || DEFAULT_SHARED_NAME);
  const others = shared.devices || [];
  return (
    <>
      <h3 className="set-section-title">One link for all devices</h3>
      <p className="set-hint">
        Turn this on with the same name on each of your machines. The phone then keeps one URL,
        and it opens on whichever machine is running, so it still works when this one is off.
      </p>
      <div className="set-row set-switch-row" title="Advertise this device as a host of the shared Tailscale Service">
        <span className="set-label">Shared link</span>
        <label className="ca-switch">
          <input
            type="checkbox"
            checked={on}
            disabled={busy}
            onChange={(e) => save(e.target.checked ? name || DEFAULT_SHARED_NAME : "")}
          />
          <span className="ca-slider" />
        </label>
      </div>
      <label className="set-row">
        <span className="set-label">Name</span>
        <input
          value={name}
          disabled={busy}
          placeholder={DEFAULT_SHARED_NAME}
          onChange={(e) => setName(e.target.value.trim().toLowerCase())}
          onBlur={() => on && name && name !== shared.name && save(name)}
          onKeyDown={(e) => e.key === "Enter" && on && name && name !== shared.name && save(name)}
        />
        <span className="set-hint">Use the same name on every device.</span>
      </label>
      {error && <p className="error">{error}</p>}
      {on && (
        <>
          {shared.url && (
            <div className="mobile-url-row">
              <span className="set-label">Shared URL</span>
              <a href={shared.url} target="_blank" rel="noopener noreferrer">
                {shared.url}
              </a>
            </div>
          )}
          {shared.error ? (
            <>
              <p className="error">{shared.error}</p>
              <button type="button" className="test-btn" disabled={busy} onClick={() => save(name || DEFAULT_SHARED_NAME)}>
                Try again
              </button>
            </>
          ) : (
            <p className="set-hint">
              {shared.advertised ? "✓ This device is offering the link." : "This device is not offering the link yet."}
            </p>
          )}
          <p className="set-hint">
            {shared.tagged ? "✓ This device is tagged." : "✗ This device has no tag. Tailscale only lets tagged devices host a service."}
          </p>
          <p className="set-hint">
            {shared.approved
              ? "✓ Tailscale has approved this device for " + svc + "."
              : "Waiting for Tailscale to approve this device for " + svc + "."}
          </p>
          <p className="set-hint">
            {others.length
              ? "Also on this link: " +
                others.map((d) => d.host + (d.reachable ? "" : " (offline)")).join(", ") +
                "."
              : "No other device on this link yet. Turn it on, with the same name, on each machine."}
          </p>
          {!(shared.tagged && shared.approved) && (
            <div className="set-hint">
              One-time setup in the Tailscale admin console:
              <ol>
                <li>
                  Under <strong>Services</strong>, define <code>{svc}</code> with port{" "}
                  <code>tcp:443</code>.
                </li>
                <li>
                  Tag each MindFlock machine, e.g. <code>tag:mindflock</code> (Machines → ⋯ → Edit
                  ACL tags).
                </li>
                <li>
                  So hosts are approved without a click, add this to the access policy:
                  <pre>{`"tagOwners": { "tag:mindflock": ["autogroup:admin"] },
"autoApprovers": { "services": { "${svc}": ["tag:mindflock"] } }`}</pre>
                </li>
              </ol>
              Sign-in carries across devices that are paired under Remote control: the QR above
              carries their access tokens too.
            </div>
          )}
        </>
      )}
    </>
  );
}
