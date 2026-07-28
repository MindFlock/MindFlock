/** Settings → Mobile (partial 103 + loadMobile, section 21): /m URLs + QR,
 * the tailscale-mode toggle, and the restart-to-apply flow. */

import { useCallback, useEffect, useState } from "react";
import { api } from "../../../api/client";
import { useConfig } from "../../../state/queries";
import { useServerRestart } from "../useServerRestart";
import type { ScreenProps } from "../SettingsDialog";

interface MobilePayload {
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
    try {
      await api("/api/settings", { json: { general: { serve_mode: on ? "tailscale" : "local" } } });
    } catch {
      /* revert via reload */
    }
    setModeBusy(false);
    load();
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
            <label
              className="set-row set-switch-row"
              title="Bind the server to all interfaces so phones on your tailnet can reach it"
            >
              <span className="set-label">Tailscale mode</span>
              <span className="ca-switch">
                <input
                  type="checkbox"
                  checked={data.serve_mode === "tailscale"}
                  disabled={modeBusy}
                  onChange={(e) => setMode(e.target.checked)}
                />
                <span className="ca-slider" />
              </span>
            </label>
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
          </>
        )}
      </div>
    </>
  );
}
