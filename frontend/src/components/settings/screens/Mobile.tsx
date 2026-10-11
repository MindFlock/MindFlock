/** Settings → Your phone (screen key "mobile"): getting this MindFlock onto
 * your phone, as three steps in order — Tailscale on this computer, Tailscale
 * on the phone, then ONE QR (or the /m link) to open it. Nothing here is a
 * knob: the Tailscale-mode switch ("Make reachable"), the shared phone link
 * and the access token all live on Settings → Your devices / Security, and
 * this screen points there when one of them stands in the way.
 *
 * The screen never hides: without Tailscale the first step shows what to
 * install (the same problem list as the doctor's tailscale row), with the
 * QR step waiting below it. */

import { useCallback, useEffect, useState } from "react";
import { api } from "../../../api/client";
import { useConfig } from "../../../state/queries";
import type { ScreenProps } from "../SettingsDialog";
import { TailscaleCard } from "./TailscaleCard";

interface MobilePayload {
  serve_mode?: string;
  local_only?: boolean;
  qr_svg?: string;
  qr_target?: string;
  note?: string;
  urls?: Array<{ label: string; url: string }>;
  /** Step 2 on the phone: the Tailscale app, signed in as `login`. */
  phone_app?: { url: string; qr_svg?: string; login?: string };
}

export function Mobile(p: ScreenProps) {
  const { data: config } = useConfig();
  const [data, setData] = useState<MobilePayload | null>(null);
  const [error, setError] = useState("");
  const tailscale = config?.caps?.tailscale !== false;

  const load = useCallback(async () => {
    if (!tailscale) return;
    try {
      setData(await api<MobilePayload>("/api/mobile"));
      setError("");
    } catch {
      setError("Could not load the phone URLs.");
    }
  }, [tailscale]);

  useEffect(() => {
    if (p.active) void load();
  }, [p.active, load]);

  return (
    <>
      <h3 className="set-section-title">Your phone</h3>
      <p className="set-hint set-block-hint">
        Your phone reaches this computer over Tailscale, a private network between your own
        devices. Three steps, in order:
      </p>
      <ol className="mobile-phone-steps set-hint" id="mobile-computer-steps">
        <li data-phone-step="computer" data-state={tailscale ? "ok" : "fail"}>
          {tailscale ? (
            <>Tailscale is installed on this computer.</>
          ) : (
            <>
              <strong>Install Tailscale on this computer</strong> and sign in. What that means
              here depends on the OS (the Mac app, or Tailscale inside WSL when it's only on
              Windows) — the card below knows, and says what's wrong if anything is.
              {/* The card reads GET /api/tailscale/health: the same checks as
                  the doctor's tailscale row, so the two never disagree. */}
              <TailscaleCard active={p.active} />
              <p>Then reopen this screen — the QR code and phone URLs appear below.</p>
            </>
          )}
        </li>
      </ol>
      <PhoneSteps app={data?.phone_app} />
      <div id="mobile-body" className="mobile-body">
        {!tailscale ? (
          <p className="set-hint" id="mobile-waiting">
            The QR code and the <code>/m</code> link show here once Tailscale is set up on this
            computer.
          </p>
        ) : error ? (
          <p className="error">{error}</p>
        ) : !data ? (
          <p className="set-hint">Loading…</p>
        ) : (
          <>
            {data.local_only && (
              // Bound to 127.0.0.1: nothing the phone can open. The one fix is
              // the Tailscale-mode switch, which lives on Your devices.
              <div className="devices-warn" id="mobile-local-only" role="alert">
                <p>
                  This computer only listens on itself — your phone can't reach it yet. Turn on{" "}
                  <strong>Make reachable</strong> in Your devices (Tailscale mode with the access
                  gate on; MindFlock restarts once).
                </p>
                <button type="button" className="test-btn" id="mobile-open-devices" onClick={() => p.gotoScreen("devices")}>
                  Open Your devices
                </button>
              </div>
            )}
            {data.qr_svg && (
              // Server-generated segno SVG (trusted). The ONE MindFlock QR: it
              // encodes the shared phone link once this device advertises it,
              // else this device's own URL.
              <div id="mobile-qr" dangerouslySetInnerHTML={{ __html: data.qr_svg }} />
            )}
            {data.note && <p className="set-hint">{data.note}</p>}
            <UrlList urls={data.urls || []} />
            <p className="set-hint" id="mobile-sign-in-hint">
              Scanning signs the phone in. To open the link by hand it asks for the access token
              (Settings → Security). One URL for all your computers: Your devices → One link for
              all devices.
            </p>
          </>
        )}
      </div>
    </>
  );
}

/** Phone setup in order: Tailscale on the phone first (signed in to the same
 * account — without it the MindFlock QR opens a URL that just hangs), then
 * the MindFlock QR below. */
export function PhoneSteps({ app }: { app?: MobilePayload["phone_app"] }) {
  const url = app?.url || "https://tailscale.com/download";
  return (
    <ol className="mobile-phone-steps set-hint" id="mobile-phone-steps" start={2}>
      <li data-phone-step="tailscale">
        Install Tailscale on your phone and sign in
        {app?.login ? (
          <>
            {" "}
            as <strong>{app.login}</strong>
          </>
        ) : (
          " with the same account as this computer"
        )}
        . Scan to get the app, or open{" "}
        <a href={url} target="_blank" rel="noopener noreferrer">
          {url.replace(/^https:\/\//, "")}
        </a>{" "}
        on the phone.
        {app?.qr_svg && (
          // Server-generated segno SVG (trusted): the app-store link, not MindFlock.
          <div className="qr-card ts-app-qr" dangerouslySetInnerHTML={{ __html: app.qr_svg }} />
        )}
      </li>
      <li data-phone-step="mindflock">
        Open MindFlock on the phone: scan the QR below, or use one of the URLs.
      </li>
    </ol>
  );
}

/** Label/URL pairs in one grid, so every label shares a column and long URLs
 * wrap inside their own cell (stacked under the label when narrow). */
export function UrlList({ urls }: { urls: Array<{ label: string; url: string }> }) {
  if (!urls.length) return null;
  return (
    <dl className="mobile-urls">
      {urls.map((u) => (
        <div className="mobile-url-row" key={u.url}>
          <dt className="set-label">{u.label}</dt>
          <dd>
            <a href={u.url} target="_blank" rel="noopener noreferrer">
              {u.url}
            </a>
          </dd>
        </div>
      ))}
    </dl>
  );
}
