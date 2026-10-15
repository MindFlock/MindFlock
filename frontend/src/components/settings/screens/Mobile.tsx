/** Settings → Your phone (screen key "mobile"): getting this MindFlock onto
 * your phone, as four rows in order, each with its own live state:
 *
 *   1 ✓/✗ Tailscale on this computer   running? (GET /api/tailscale/health)
 *   2  •  Tailscale on your phone        the app's QR (we can't see the phone)
 *   3 ✓/✗ MindFlock reachable            not bound to 127.0.0.1 (GET /api/mobile)
 *   4  •  Scan to open MindFlock         THE QR — drawn only once 1 and 3 are ✓
 *
 * A ✗ row says what is wrong in one plain line and, when there is one, the
 * command that fixes it in ONE Copy box (no backticked shell in the prose).
 * The QR never shows while it would open a URL that hangs, so there is never
 * a "scan the QR below" pointing at the wrong QR. Nothing here is a knob: the
 * Tailscale-mode switch ("Make reachable"), the shared phone link and the
 * access token live on Settings → Your devices / Security. */

import { useCallback, useEffect, useState, type ReactNode } from "react";
import { api } from "../../../api/client";
import { copyText } from "../../../lib/clipboard";
import { toast } from "../../../lib/toast";
import { useConfig } from "../../../state/queries";
import type { ScreenProps } from "../SettingsDialog";
import { TailscaleCard, type TailscaleHealth } from "./TailscaleCard";

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

export type RowState = "ok" | "fail" | "wait";

/** Row 1, from the Tailscale health payload: its state, one plain line, the
 * one command that fixes it (or ""), and whether the full Tailscale card
 * (install guidance, Sign in) belongs under it. */
export function computerRow(
  h: TailscaleHealth | null,
  opts: { caps: boolean; error?: boolean }
): { state: RowState; line: string; fix: string; card: boolean } {
  if (!h) {
    if (!opts.caps || opts.error)
      return { state: "fail", line: "Tailscale isn't on this computer yet.", fix: "", card: true };
    return { state: "wait", line: "Checking Tailscale on this computer…", fix: "", card: false };
  }
  const fix = (h.issues || []).find((i) => i.fix)?.fix || "";
  if (!h.installed) return { state: "fail", line: "Tailscale isn't on this computer yet.", fix: "", card: true };
  if (h.running || h.backend_state === "Running") {
    const who = h.tagged ? "" : h.user ? " — signed in as " + h.user : "";
    return { state: "ok", line: "Tailscale is running on this computer" + who + ".", fix: "", card: false };
  }
  if (h.backend_state === "NeedsLogin" || h.backend_state === "NoState")
    return { state: "fail", line: "Tailscale isn't signed in on this computer.", fix: "", card: true };
  if (h.backend_state === "NeedsMachineAuth")
    return { state: "fail", line: "Tailscale is waiting for an admin to approve this computer.", fix: "", card: true };
  return {
    state: "fail",
    line: fix ? "Tailscale is installed but not running. Start it:" : "Tailscale is installed but not running.",
    fix,
    card: !fix,
  };
}

/** Row 3, from GET /api/mobile: reachable from the phone, or only from here. */
export function reachableRow(data: MobilePayload | null, opts: { caps: boolean }): RowState {
  if (!opts.caps) return "wait";
  if (!data) return "wait";
  return data.local_only ? "fail" : "ok";
}

const GLYPH: Record<RowState, string> = { ok: "✓", fail: "✗", wait: "•" };

/** One command and its Copy button — the only place a command appears. */
function CopyBox({ text }: { text: string }) {
  return (
    <div className="sl-snippet mobile-fix">
      <pre>{text}</pre>
      <button
        type="button"
        className="test-btn"
        onClick={() => copyText(text).then((ok) => toast(ok ? "Command copied" : "Copy failed"))}
      >
        Copy
      </button>
    </div>
  );
}

function Row({
  n,
  state,
  title,
  id,
  children,
}: {
  n: number;
  state: RowState | "none";
  title: string;
  id: string;
  children?: ReactNode;
}) {
  return (
    <li className={"mobile-row st-" + state} data-phone-step={id} data-state={state}>
      <div className="mobile-row-head">
        <span className="mobile-row-n" aria-hidden="true">
          {n}
        </span>
        {state !== "none" && (
          <span className={"mobile-row-glyph st-" + state} aria-label={state === "ok" ? "done" : state === "fail" ? "not yet" : "checking"}>
            {GLYPH[state]}
          </span>
        )}
        <span className="mobile-row-title">{title}</span>
      </div>
      {children ? <div className="mobile-row-body set-hint">{children}</div> : null}
    </li>
  );
}

export function Mobile(p: ScreenProps) {
  const { data: config } = useConfig();
  const [data, setData] = useState<MobilePayload | null>(null);
  const [error, setError] = useState("");
  const [health, setHealth] = useState<TailscaleHealth | null>(null);
  const [healthErr, setHealthErr] = useState(false);
  const caps = config?.caps?.tailscale !== false;

  const load = useCallback(async () => {
    try {
      setHealth(await api<TailscaleHealth>("/api/tailscale/health"));
      setHealthErr(false);
    } catch {
      setHealthErr(true);
    }
    if (!caps) return;
    try {
      setData(await api<MobilePayload>("/api/mobile"));
      setError("");
    } catch {
      setError("Could not load the phone URLs.");
    }
  }, [caps]);

  useEffect(() => {
    if (p.active) void load();
  }, [p.active, load]);

  const one = computerRow(health, { caps, error: healthErr });
  const three = reachableRow(data, { caps });
  const ready = one.state === "ok" && three === "ok";

  return (
    <>
      <h3 className="set-section-title">Your phone</h3>
      <p className="set-hint set-block-hint">
        Your phone reaches this computer over Tailscale, a private network between your own devices.
      </p>
      <ol className="mobile-steps" id="mobile-steps">
        <Row n={1} state={one.state} title="Tailscale on this computer" id="computer">
          <p id="mobile-computer-line">{one.line}</p>
          {one.fix && <CopyBox text={one.fix} />}
          {/* The card reads the same GET /api/tailscale/health: install
              guidance per OS, and Sign in. Folded when the row already says
              the one fix. */}
          {one.state !== "ok" &&
            (one.card ? (
              <TailscaleCard active={p.active} />
            ) : one.state === "fail" ? (
              <details className="mobile-ts-details">
                <summary>More about Tailscale on this computer</summary>
                <TailscaleCard active={p.active} />
              </details>
            ) : null)}
        </Row>
        <PhoneAppRow app={data?.phone_app} />
        <Row n={3} state={three} title="Make MindFlock reachable from your phone" id="reachable">
          {three === "fail" ? (
            // Bound to 127.0.0.1: nothing the phone can open. The one fix is
            // the Tailscale-mode switch, which lives on Your devices (it
            // restarts MindFlock once, so it asks there first).
            <div id="mobile-local-only" role="alert">
              <p>MindFlock only listens on this computer, so your phone can't reach it yet.</p>
              <button type="button" className="test-btn" id="mobile-open-devices" onClick={() => p.gotoScreen("devices")}>
                Turn on Make reachable…
              </button>
            </div>
          ) : three === "ok" ? (
            <p>Your other devices on Tailscale can reach MindFlock.</p>
          ) : error ? (
            <p className="error">{error}</p>
          ) : (
            <p>{caps ? "Checking…" : "Waits for Tailscale on this computer (1)."}</p>
          )}
        </Row>
        <Row n={4} state="none" title="Scan to open MindFlock" id="mindflock">
          <div id="mobile-body" className="mobile-body">
            {!ready || !data ? (
              <p id="mobile-waiting">The QR to open MindFlock appears here once 1 and 3 are ✓.</p>
            ) : (
              <>
                {data.qr_svg && (
                  // Server-generated segno SVG (trusted). The ONE MindFlock QR:
                  // it encodes the shared phone link once this device
                  // advertises it, else this device's own URL.
                  <div id="mobile-qr" dangerouslySetInnerHTML={{ __html: data.qr_svg }} />
                )}
                {data.note && <p>{data.note}</p>}
                <UrlList urls={data.urls || []} />
                <p id="mobile-sign-in-hint">
                  Scanning signs the phone in. Opening a link by hand asks for the access token (Settings →
                  Security).
                </p>
              </>
            )}
          </div>
        </Row>
      </ol>
    </>
  );
}

/** Row 2: Tailscale on the phone, signed in to the same account — without it
 * the MindFlock QR (row 4) opens a URL that just hangs. Its QR is the app
 * download, said so on its face so it is never taken for MindFlock's. */
export function PhoneAppRow({ app }: { app?: MobilePayload["phone_app"] }) {
  const url = app?.url || "https://tailscale.com/download";
  return (
    <Row n={2} state="none" title="Tailscale on your phone" id="tailscale">
      <p>
        Install the Tailscale app on your phone and sign in
        {app?.login ? (
          <>
            {" "}
            as <strong>{app.login}</strong>
          </>
        ) : (
          " with the same account as this computer"
        )}
        . This QR gets the app, or open{" "}
        <a href={url} target="_blank" rel="noopener noreferrer">
          {url.replace(/^https:\/\//, "")}
        </a>{" "}
        on the phone.
      </p>
      {app?.qr_svg && (
        // Server-generated segno SVG (trusted): the app-store link, not MindFlock.
        <div className="qr-card ts-app-qr" dangerouslySetInnerHTML={{ __html: app.qr_svg }} />
      )}
    </Row>
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
