/** "Tailscale on this device": the top of Settings → Devices (and the Mobile
 * screen's no-Tailscale state). Everything here is GET /api/tailscale/health
 * (backend/tailscale_cli.py) — signed in as whom, to which tailnet, as which
 * device, and each problem with its one fix, worst first — plus the one
 * write, Sign in (POST /api/tailscale/login), which hands back Tailscale's
 * sign-in URL as a link and a QR without waiting for the person to finish;
 * the card then polls health until the device is running.
 *
 * The same problem list feeds the doctor's tailscale row, so the two never
 * disagree. Platform guidance lives in the issues (the server knows the OS):
 * the Mac app instead of the headless formula, and on WSL with only Windows
 * Tailscale, why that isn't enough and the two ways out. */

import { useCallback, useEffect, useState } from "react";
import { api } from "../../../api/client";
import { copyText } from "../../../lib/clipboard";
import { toast } from "../../../lib/toast";
import "./tailscaleCard.css";

export interface TailscaleIssue {
  id: string;
  level: "fail" | "warn" | "info";
  message: string;
  fix?: string;
  docs?: string;
}

export interface TailscaleHealth {
  installed: boolean;
  path?: string;
  /** native | app-bundle | wsl-windows-host */
  kind?: string;
  os?: string;
  windows_host?: string;
  backend_state?: string;
  running?: boolean;
  auth_url?: string;
  auth_qr_svg?: string;
  tailnet?: string;
  user?: string;
  tagged?: boolean;
  tags?: string[];
  device?: { name: string; dns: string; ips: string[]; os: string; id: string };
  magicdns?: boolean | null;
  https?: boolean | null;
  key_expiry?: { at: string; days: number | null; expired: boolean; warn: boolean };
  issues: TailscaleIssue[];
  admin?: { machines: string; dns: string };
}

export interface LoginResult {
  ok: boolean;
  state?: string;
  auth_url?: string;
  auth_qr_svg?: string;
  error?: string;
  fix?: string;
}

const POLL_MS = 3000;

/** "Signed in as" for the card: a tagged device belongs to no login. */
export function signedInAs(h: TailscaleHealth): string {
  if (h.tagged) return "a tagged device (" + (h.tags || []).join(", ") + ")";
  return h.user || "";
}

/** One status word for the header. */
export function stateLabel(h: TailscaleHealth): string {
  if (!h.installed) return h.kind === "wsl-windows-host" ? "On Windows only" : "Not installed";
  switch (h.backend_state) {
    case "Running":
      return "Connected";
    case "NeedsLogin":
    case "NoState":
      return "Not signed in";
    case "Stopped":
      return "Turned off";
    case "NeedsMachineAuth":
      return "Waiting for approval";
    case "":
    case undefined:
      return "Not answering";
    default:
      return h.backend_state;
  }
}

/** Whether the Sign in / Turn on button applies. */
export function canSignIn(h: TailscaleHealth): boolean {
  return (
    h.installed &&
    (h.backend_state === "NeedsLogin" || h.backend_state === "NoState" || h.backend_state === "Stopped")
  );
}

function CopyRow({ text }: { text: string }) {
  return (
    <div className="sl-snippet">
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

function Issue({ issue }: { issue: TailscaleIssue }) {
  return (
    <li className={"ts-issue ts-" + issue.level} data-issue={issue.id}>
      <p>{issue.message}</p>
      {issue.fix && <CopyRow text={issue.fix} />}
      {issue.id === "wsl_windows_only" && <WslOptions />}
      {issue.docs && (
        <a className="sl-admin-link" href={issue.docs} target="_blank" rel="noopener noreferrer">
          {issue.docs.includes("login.tailscale.com/admin")
            ? "Open the admin console"
            : "Tailscale's guide"}{" "}
          ↗
        </a>
      )}
    </li>
  );
}

/** WSL with Tailscale only on the Windows side. The command above is the
 * path MindFlock is tested on (a second node inside WSL); mirrored
 * networking is the alternative, said with its caveat. */
function WslOptions() {
  return (
    <div className="ts-wsl" data-wsl-options>
      <p className="set-hint">
        <strong>Recommended:</strong> run Tailscale inside WSL too (the command above). It needs
        systemd on in <code>/etc/wsl.conf</code> (<code>[boot] systemd=true</code>, then{" "}
        <code>wsl --shutdown</code>). It joins your tailnet as its own device, named after this
        computer with <code>-wsl</code>; sign in as the same account.
      </p>
      <p className="set-hint">
        <strong>Alternative:</strong> WSL's mirrored networking (
        <code>networkingMode=mirrored</code> in <code>.wslconfig</code>) can let the Windows node's
        address reach MindFlock, with one device instead of two. Tailscale itself recommends
        installing only on Windows, but MindFlock hasn't been tested that way: other devices may
        not reach it, and joining Your devices checks the caller's tailnet address.
      </p>
    </div>
  );
}

/** The card's markup, from a health payload — no effects, so it renders in
 * tests through react-dom/server. */
export function TailscaleCardView({
  health,
  login,
  busy,
  onSignIn,
}: {
  health: TailscaleHealth | null;
  login?: LoginResult | null;
  busy?: boolean;
  onSignIn?: () => void;
}) {
  if (!health) {
    return (
      <section className="ts-card" id="tailscale-card">
        <h3 className="set-section-title">Tailscale on this device</h3>
        <p className="set-hint">Checking Tailscale…</p>
      </section>
    );
  }
  const h = health;
  const who = signedInAs(h);
  const dev = h.device;
  const ipv4 = (dev?.ips || []).find((a) => !a.includes(":"));
  const authUrl = login?.auth_url || h.auth_url || "";
  const authQr = login?.auth_qr_svg || h.auth_qr_svg || "";
  const label = stateLabel(h);
  return (
    <section className="ts-card" id="tailscale-card" data-state={h.backend_state || ""}>
      <div className="ts-head">
        <h3 className="set-section-title">Tailscale on this device</h3>
        <span className={"ts-state" + (h.running ? " ts-ok" : "")}>{label}</span>
      </div>
      {h.installed && h.running && (
        <dl className="ts-facts">
          {who && (
            <div>
              <dt>Signed in as</dt>
              <dd>{who}</dd>
            </div>
          )}
          {h.tailnet && (
            <div>
              <dt>Tailnet</dt>
              <dd>{h.tailnet}</dd>
            </div>
          )}
          {dev && (dev.dns || dev.name) && (
            <div>
              <dt>This device</dt>
              <dd>
                {dev.dns || dev.name}
                {ipv4 ? <code className="ts-ip">{ipv4}</code> : null}
              </dd>
            </div>
          )}
        </dl>
      )}
      {h.installed && h.kind === "app-bundle" && (
        <p className="set-hint">Using the Tailscale app's built-in command line.</p>
      )}
      {canSignIn(h) && (
        <div className="ts-signin">
          <button type="button" className="test-btn" disabled={busy} onClick={onSignIn}>
            {busy
              ? "Starting…"
              : h.backend_state === "Stopped"
                ? "Turn on Tailscale"
                : "Sign in to Tailscale"}
          </button>
          {authUrl && (
            <div className="ts-auth">
              <p className="set-hint">
                Open this link to sign in (or scan it with a phone that is signed in to Tailscale).
                This card updates once you finish:
              </p>
              <a href={authUrl} target="_blank" rel="noopener noreferrer" className="ts-auth-link">
                {authUrl}
              </a>
              {authQr && (
                // Server-generated segno SVG (trusted), like the Mobile QR.
                <div className="qr-card ts-qr" dangerouslySetInnerHTML={{ __html: authQr }} />
              )}
            </div>
          )}
          {login && !login.ok && login.error && (
            <div className="ts-login-error">
              <p className="error">{login.error}</p>
              {login.fix && <CopyRow text={login.fix} />}
            </div>
          )}
        </div>
      )}
      {h.issues.length > 0 && (
        <ul className="ts-issues">
          {h.issues.map((i) => (
            <Issue key={i.id} issue={i} />
          ))}
        </ul>
      )}
    </section>
  );
}

/** The live card: loads health when shown, polls while a sign-in is pending. */
export function TailscaleCard({ active = true }: { active?: boolean }) {
  const [health, setHealth] = useState<TailscaleHealth | null>(null);
  const [login, setLogin] = useState<LoginResult | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async (refresh = false) => {
    try {
      setHealth(await api<TailscaleHealth>("/api/tailscale/health" + (refresh ? "?refresh=1" : "")));
    } catch {
      /* keep the last answer; the card is advisory */
    }
  }, []);

  useEffect(() => {
    if (active) void load();
  }, [active, load]);

  // A sign-in in flight: watch for Running (the link was opened elsewhere).
  const pending = !!(login?.ok && health && !health.running);
  useEffect(() => {
    if (!active || !pending) return;
    const t = setInterval(() => {
      if (!document.hidden) void load(true);
    }, POLL_MS);
    return () => clearInterval(t);
  }, [active, pending, load]);

  const signIn = async () => {
    setBusy(true);
    try {
      setLogin(await api<LoginResult>("/api/tailscale/login", { method: "POST" }));
    } catch (e) {
      const body = (e as { body?: LoginResult }).body;
      setLogin(body && typeof body === "object" ? body : { ok: false, error: (e as Error).message });
    } finally {
      setBusy(false);
      void load(true);
    }
  };

  return <TailscaleCardView health={health} login={login} busy={busy} onSignIn={signIn} />;
}
